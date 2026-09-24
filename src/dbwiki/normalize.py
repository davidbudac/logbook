"""Normalize raw ES hits into flat events the pattern rules can classify.

A normalized event always has: ts, db, source, index, id, message.
Source-specific fields are added flat (msg_type, operation, return_code, ...).
Field access is defensive: mappings differ between index generations and
`oracle.alert_message` is sometimes a string, sometimes an empty object.

Two index layouts are supported per source, tried in order:
  legacy — flat `db_name` plus per-source objects (`oracle.alert_message`,
           `listener.*`, `dataguard.*`);
  ECS    — `oracle.database.name` plus ECS-shaped fields (top-level `message`,
           `oracle.listener.*`, `oracle.dataguard.*`, `service.name`,
           `error.code`, `log.level`).
Which db fields are tried comes from config (`db_fields`); the payload
fallbacks are fixed chains below. A window whose events carry none of the
configured db fields raises UnsupportedSchemaError (from the compactor) rather
than silently producing zero events.

`kind="metric"` (config `metric_sources`) selects the metrics branch instead:
numeric samples, not text. Only ECS-shaped metric documents are evidenced
(`.ds-logs-oracle.metrics-*`, probed 2026-07-27: no legacy flat layout exists
for metrics), so there is no legacy chain here — just the same defensive
lookups."""

import html
import re

_TXT_RE = re.compile(r"<txt>(.*?)</txt>", re.S)
_CONNECT_RE = re.compile(r"PROGRAM=([^)]*)")
_HOST_RE = re.compile(r"\(CID=[^)]*\(HOST=([^)]*)\)")
_USER_RE = re.compile(r"USER=([^)]*)")

LEGACY_DB_FIELDS = ("db_name",)

# Event fields that identify a machine or a database (ADR-0002). Anything
# listed here must be fed into the redaction vocabulary before a request may
# leave the on-prem box (redact.py consumes this tuple; a test asserts every
# `ev[...]` key set below is classified as either identity or benign, so a
# new extractor cannot silently add an unredacted identifier).
IDENTITY_FIELDS = ("db", "index", "id", "service", "client_host", "os_user",
                   "member", "metric_target")
# Fields normalize() sets that carry no machine/database identity: message
# text is redacted by pattern/vocabulary sweep, not by field name.
BENIGN_FIELDS = ("ts", "source", "message", "msg_type", "msg_id", "level",
                 "con_id", "operation", "return_code", "program",
                 "event_type", "severity", "error_code", "metric_kind",
                 "metric_name", "metric_unit", "metric_value",
                 "metric_values", "metric_ts", "db_role", "instance_status",
                 "instance_available", "outcome", "error_message")


class UnsupportedSchemaError(RuntimeError):
    """Events exist in the window but none carries any configured db field."""


def _s(v) -> str:
    """Coerce a possibly-missing / non-string field to str."""
    if v is None or isinstance(v, (dict, list)):
        return ""
    return str(v)


_CODE_RE = re.compile(r"-?[0-9]+")


def _code(v) -> int | None:
    """A return code as an int, or None. The collector converts the listener
    code to an integer, but a pipeline that skips the convert ships the same
    number as a string; `"0"` read as None would fall through to the
    message-text rules instead of `establish_ok`. Only a plain (optionally
    signed) digit string converts; a bool is not a code."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and _CODE_RE.fullmatch(v.strip()):
        return int(v)
    return None


def _dig(src: dict, *path, default=None):
    cur = src
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _obj(v) -> dict:
    """Coerce a possibly-missing / non-dict field to a dict."""
    return v if isinstance(v, dict) else {}


_DG_SUFFIX_RE = re.compile(r"_dgmgrl$", re.I)
_DB_NAME_RE = re.compile(r"^[A-Za-z][\w$]*$")


def canon_service_db(service: str) -> str:
    """Database name implied by a TNS service name: `CDB1_DGMGRL.world` ->
    `cdb1`. Returns "" for values that are not name-like (ECS listener docs
    also carry raw connect strings and the listener's own name)."""
    base = _DG_SUFFIX_RE.sub("", service.split(".")[0])
    if not _DB_NAME_RE.match(base) or base.upper() == "LISTENER":
        return ""
    return base.lower()


def service_name_like(service: str) -> bool:
    """Is this a plausible TNS service name? Judged on the canonical base, so
    domain-qualified names (`pdb1.example.com`) pass. False for the listener's
    own name and for connect-string fragments (`ADDRESS=(PROTOCOL=TCP`) that
    upstream kv-parsing sometimes files under `service.name`."""
    return canon_service_db(service) != ""


# Where an ES document names the machine it came from (ECS `host.*`, the
# shipper's own `agent.hostname`, pre-ECS Beats `beat.hostname`). The
# compactor records every value it sees in the per-db registry, and the
# redaction vocabulary learns them (`observed_hosts`, ADR-0002).
HOST_FIELDS = ("host.name", "host.hostname", "agent.hostname", "beat.hostname")
# values that sit in host fields and descriptors but name no machine of the
# estate, or are words whose redaction would eat research text (`oracle`
# would take "Oracle Database" with it, `standby` every Data Guard line)
HOST_STOPLIST = frozenset({
    "localhost", "localhost.localdomain", "localhost4", "localhost6",
    "localhost4.localdomain4", "localhost6.localdomain6", "localdomain",
    "unknown", "none", "null", "nil", "undefined", "n/a", "__jdbc__", "jdbc",
    "host", "hostname", "server", "client", "default", "local", "oracle",
    "database", "listener", "primary", "standby", "backup", "archive",
    "grid", "asm", "data", "reco", "redo", "admin", "root", "test", "prod",
    "dev", "node", "master", "docker", "container", "kubernetes", "filebeat",
    "logstash", "elastic", "agent", "www"})
_HOSTNAME_RE = re.compile(r"[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?"
                          r"(?:\.[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?)*")
_HOST_KV_RE = re.compile(r"\(\s*HOST\s*=\s*([^)\s]+)\s*\)", re.I)
_DIAG_HOST_RE = re.compile(r"diag/(?:tnslsnr|crs)/([^/\s]+)/")


def host_name_like(value) -> str:
    """The canonical (lower-case, no trailing dot) spelling of a value that
    plausibly names a machine, or "" for anything that does not: too short
    (< 3), no letter (a number, an IPv4/IPv6 address), a release (`19c`,
    `23ai`), not DNS/NetBIOS-shaped, or on HOST_STOPLIST. Addresses are left
    to the redactor's IP patterns, which catch every one by shape."""
    if not isinstance(value, str):
        return ""
    v = value.strip().rstrip(".").lower()
    if len(v) < 3 or len(v) > 253 or v in HOST_STOPLIST:
        return ""
    if not re.search(r"[a-z]", v) or re.fullmatch(r"[0-9]+[a-z]{1,2}", v):
        return ""
    if not _HOSTNAME_RE.fullmatch(v):
        return ""
    return v


def observed_hosts(hit: dict, ev: dict | None = None) -> list[str]:
    """Machine names one ES document carries, guarded by `host_name_like`,
    deduplicated in first-seen order: the HOST_FIELDS (nested or flat keys,
    and a plain-string `host` as Logstash writes it), the host segment of a
    listener/CRS diag path in `log.file.path`, every `(HOST=…)` in the
    message (tnsnames descriptors in alert-log NI errors, listener connect
    data) and the listener's parsed `client_host`. Nothing here queries ES:
    it reads what the compactor already fetched."""
    src = _obj(hit.get("_source"))
    raw: list = [src["host"]] if isinstance(src.get("host"), str) else []
    for f in HOST_FIELDS:
        raw.append(_dig(src, *f.split(".")))
        raw.append(src.get(f))
    path = _s(_dig(src, "log", "file", "path")) or _s(src.get("log.file.path"))
    raw.extend(_DIAG_HOST_RE.findall(path))
    ev = ev or {}
    raw.extend(_HOST_KV_RE.findall(_s(ev.get("message"))))
    raw.append(ev.get("client_host"))
    return list(dict.fromkeys(h for h in map(host_name_like, raw) if h))


def db_of(src: dict, db_fields=LEGACY_DB_FIELDS) -> str:
    """First non-empty value among the configured db fields; each field is a
    dotted path tried both nested ({"oracle": {"database": {"name": ...}}})
    and flat ({"oracle.database.name": ...})."""
    for field in db_fields:
        v = _s(_dig(src, *field.split("."))) or _s(src.get(field))
        if v:
            return v
    return ""


def field_paths(src: dict, prefix: str = "", limit: int = 40) -> list[str]:
    """Sorted dotted leaf paths of a document (for schema diagnostics)."""
    out: list[str] = []

    def walk(d: dict, p: str) -> None:
        for k, v in d.items():
            q = f"{p}.{k}" if p else k
            if isinstance(v, dict):
                walk(v, q)
            else:
                out.append(q)

    walk(src, prefix)
    return sorted(out)[:limit]


# Metric kinds as they appear in `.ds-logs-oracle.metrics-*` (probed
# 2026-07-27): oracle.metric.type -> (object under `oracle` holding the
# numbers, the primary numeric field inside it, the sub-entity name field,
# unit). `sysmetric` is the self-describing kind (name/unit/value on
# oracle.metric itself) and is handled separately.
_METRIC_KINDS = {
    "tablespace": ("tablespace", "used_pct", "name", "pct"),
    "fra": ("fra", "used_pct", None, "pct"),
    "health": ("metrics", "sessions.utilization_pct", None, "pct"),
    "dataguard": ("dataguard", "apply_lag_seconds", None, "s"),
}


def _numbers(obj: dict, prefix: str = "") -> dict:
    """Flat {dotted path: number} of an object's numeric leaves. Booleans are
    not measurements and are excluded."""
    out: dict[str, float] = {}
    for k, v in sorted(obj.items()):
        q = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_numbers(v, q))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[q] = v
    return out


def _metric_event(ev: dict, src: dict) -> dict:
    """Metrics branch: db identity + role, metric kind/name, numeric value(s)
    and the sample timestamp, flat like the other branches. No thresholds are
    applied here — the only notability signal is the one the document itself
    carries (`event.outcome: failure`, i.e. the collector could not read the
    database)."""
    ora = _obj(src.get("oracle"))
    metric = _obj(ora.get("metric"))
    kind = _s(metric.get("type"))
    obj_key, primary, target_field, unit = _METRIC_KINDS.get(
        kind, ("metric", "value", None, ""))
    values = _numbers(_obj(ora.get(obj_key)))
    name = _s(metric.get("name")) or primary
    target = _s(_dig(ora, obj_key, target_field)) if target_field else ""

    ev["metric_kind"] = kind
    ev["metric_name"] = name
    ev["metric_target"] = target
    ev["metric_unit"] = _s(metric.get("unit")) or unit
    ev["metric_value"] = values.get(primary)
    ev["metric_values"] = values
    # probed layout: the sample time is the document timestamp itself
    ev["metric_ts"] = ev["ts"]
    ev["db_role"] = _s(_dig(ora, "database", "role"))
    ev["instance_status"] = _s(_dig(ora, "instance", "status"))
    available = _dig(ora, "instance", "available")
    ev["instance_available"] = available if isinstance(available, bool) else None
    ev["outcome"] = _s(_dig(src, "event", "outcome"))
    ev["level"] = _s(_dig(src, "log", "level"))
    ev["error_message"] = _s(_dig(src, "error", "message"))

    if ev["error_message"]:
        ev["message"] = ev["error_message"]
    else:
        head = " ".join(p for p in (kind, target, name) if p)
        val = ev["metric_value"]
        ev["message"] = head if val is None else f"{head}={val}"
    return ev


def normalize(source: str, hit: dict, db_fields=LEGACY_DB_FIELDS,
              kind: str = "log") -> dict:
    src = hit.get("_source", {})
    ev = {
        "ts": _s(src.get("@timestamp")),
        "db": db_of(src, db_fields) or "unknown",
        "source": source,
        "index": hit.get("_index", ""),
        "id": hit.get("_id", ""),
        "message": "",
    }
    if kind == "metric":
        return _metric_event(ev, src)
    if source == "alert":
        ora = _obj(src.get("oracle"))
        msg = _s(ora.get("alert_message"))
        if not msg.strip():
            # upstream sometimes emits alert_message as {}; recover the text
            # from the raw <msg>...<txt> XML in event.original
            m = _TXT_RE.search(_s(_dig(src, "event", "original")))
            if m:
                msg = html.unescape(m.group(1)).strip()
        if not msg.strip():
            msg = _s(src.get("message"))  # ECS: plain top-level message
        ev["message"] = msg
        ev["msg_type"] = _s(ora.get("msg_type")) or _s(_dig(src, "log", "level"))
        ev["msg_id"] = _s(ora.get("msg_id"))
        ev["level"] = ora.get("msg_level")
        ev["con_id"] = _s(ora.get("con_id"))
    elif source == "listener":
        lsn = _obj(src.get("listener"))
        ecs = _obj(_dig(src, "oracle", "listener"))
        detail = _s(lsn.get("detail")) or _s(src.get("message")) \
            or _s(_dig(src, "event", "original"))
        ev["message"] = detail
        ev["operation"] = _s(lsn.get("operation")) or _s(ecs.get("command"))
        rc = _code(lsn.get("return_code"))
        ev["return_code"] = rc if rc is not None \
            else _code(ecs.get("return_code"))
        ev["service"] = _s(lsn.get("service_name")) or _s(_dig(src, "service", "name"))
        if ev["db"] == "unknown" and ev["service"]:
            # ECS listener docs carry no db field; derive it from the service
            ev["db"] = canon_service_db(ev["service"]) or "unknown"
        m = _CONNECT_RE.search(detail)
        ev["program"] = m.group(1) if m else ""
        m = _HOST_RE.search(detail)
        ev["client_host"] = m.group(1) if m else ""
        m = _USER_RE.search(detail)
        ev["os_user"] = m.group(1) if m else ""
    elif source == "dataguard":
        dg = _obj(src.get("dataguard"))
        ecs = _obj(_dig(src, "oracle", "dataguard"))
        ev["message"] = _s(dg.get("message")) or _s(src.get("message")) \
            or _s(_dig(src, "event", "original"))
        ev["event_type"] = _s(dg.get("event_type")) or _s(ecs.get("event_type"))
        ev["severity"] = _s(dg.get("severity")) or _s(_dig(src, "log", "level"))
        ev["error_code"] = _s(dg.get("error_code")) or _s(_dig(src, "error", "code"))
        ev["member"] = _s(dg.get("member")) or _s(ecs.get("member"))
    else:
        ev["message"] = _s(src.get("message")) or _s(_dig(src, "event", "original"))
    return ev
