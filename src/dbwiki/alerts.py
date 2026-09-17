"""Failure-only alerts: a state file, not a state machine.

Sized for the actual deployment — one cron host running `dbwiki run` — so the
whole mechanism is one small versioned `.state/alerts.json` keyed by failure
fingerprint (`sha` of error category + db + source).

* **Consumes health, never infers.** Every fingerprint comes from a WS4
  assessment dict (`health.assess`): its blockers, per-source collection
  states, stale watermarks, failed ledger entries and digest backlog. Nothing
  here probes Elasticsearch, reads the ledger, or decides what a failure is.
* **Failures only.** A fingerprint that is no longer present is dropped from
  state and listed under `recovered` (so the run-health event can carry the
  count). There is no success alert, and a healthy tick sends nothing and
  rewrites nothing.
* **One alert per fingerprint.** A persisting identical fingerprint updates
  `last_seen`/`count` and stays silent. A *material escalation* — the category
  for the same (db, source) changing to a different one, e.g. `source_silent`
  -> `collection_failure` — alerts again; the replaced fingerprint is
  deliberately not reported as recovery, because worse news is not good news.
* **Deferred:** configurable re-alert persistence intervals (a still-broken
  fingerprint re-alerting after N hours). Single-shot dedupe first; add them
  only once it demonstrably fails an operator (see
  CHANGELOG.md).
* **Transport stays dumb.** `Sink` is the seam: the built-ins write to
  stderr, append to a JSONL file, or POST the alert dict to a webhook, and
  `alerts.sinks` fans out to several at once. No sink retries, formats for a
  particular receiver, or decides anything — delivery failures are counted,
  and a dead transport never fails the run it was reporting on.

An alert carries facts and a command — run id, category, db/source, the window
or last-event fact behind it, the last successful stage, and the exact recovery
command. Never a log message, a prompt, a credential, or an assessment problem
string (which quotes exception text).
"""

import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from . import events
from .health import RETRYABLE, _now

ALERT_SCHEMA_VERSION = 1
ALERT_STATE = "alerts.json"
# the webhook URL is a deployment secret more often than not, so the
# environment wins over the checked-in config value
WEBHOOK_URL_ENV = "DBWIKI_ALERT_WEBHOOK_URL"

# where the log shippers run; named in the collection-failure recovery hint.
# Deployment-specific, so the environment names it; the default is generic.
COLLECTOR_HOST = os.environ.get("DBWIKI_COLLECTOR_HOST", "the collector host")

_HINTS = {
    "collection_failure":
        f"restart the collector on {COLLECTOR_HOST} (filebeat/logstash ships "
        f"nothing to Elasticsearch), then: dbwiki health",
    "source_silent":
        f"check the {{source}} shipper on {COLLECTOR_HOST}, then: dbwiki health",
    "es_unreachable":
        "check the Elasticsearch endpoint configured in config/dbwiki.yaml, "
        "then: dbwiki health",
    "unsupported_schema":
        "compare sources.*.db_fields in config/dbwiki.yaml with the current ES "
        "mapping, then: dbwiki health",
    "wiki_missing": "restore the wiki/ checkout (clone the wiki repo beside "
                    "config/), then: dbwiki health",
    "dirty_tree": "commit or stash the non-digest changes in wiki/, then: "
                  "dbwiki health",
    "commit_failed": "fix the wiki repository by hand (lock/remote), then: "
                     "dbwiki retry",
    "stale_watermark": "dbwiki run",
    "digest_backlog": "dbwiki run --consolidate",
    "stage_stale": "check .state/cron.log and the crontab; run "
                   "`uv run dbwiki {detail}` by hand",
}

_DEP_HINTS = {
    "model_server": "start the local model server (unsloth studio: `unsloth "
                    "start`); see docs/scheduling.md \"What the local stages "
                    "need to be up\"",
    "adapter_missing": "install/authenticate the adapter and make sure cron's "
                       "PATH includes it (docs/scheduling.md)",
    "git_identity": "git -C wiki config user.email <you@example.com>",
    "wiki_no_upstream": "git -C wiki push -u origin HEAD (or set report.push "
                        "false)",
    "unpushed": "cd wiki && git push",
    "disk": "free space under .state/",
    "portal": "systemctl --user restart dbwiki-portal (or `uv run dbwiki "
              "portal serve`); until then use `dbwiki incident ...`",
}


def recovery_hint(category: str, db: str = "-", source: str = "-", *,
                  detail: str | None = None) -> str:
    """The exact next action for a category: a command where one exists, the
    physical action otherwise. Retryable failure categories are read from
    `health.RETRYABLE` rather than re-listed. `detail` names *which*
    dependency or stage the finding is about, so one category can still give
    one specific command."""
    if category in RETRYABLE:
        return f"dbwiki retry --db {db}" if db != "-" else "dbwiki retry"
    if category == "dependency_failure":
        head = (detail or "").split(":")[0]
        return next((h for k, h in _DEP_HINTS.items() if head.startswith(k)),
                    "dbwiki health")
    return _HINTS.get(category, "dbwiki health").format(source=source,
                                                        detail=detail or "")


def fingerprint(category: str, db: str, source: str, detail: str = "") -> str:
    """Stable short sha of (error_category, db, source[, detail]). Absent
    dimensions are "-", so a source-wide failure and a per-db one never
    collide. `detail` is appended only when present, which keeps every
    fingerprint minted before it existed unchanged — a missing adapter and an
    unreachable model server are both `dependency_failure`, but they are not
    the same alert."""
    key = f"{category}|{db or '-'}|{source or '-'}"
    if detail:
        key += f"|{detail}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class _Finding:
    """One current problem from the assessment, already fingerprinted.
    `window` holds the bounded fact behind it — never a problem string."""

    category: str
    db: str
    source: str
    window: dict = field(default_factory=dict)
    detail: str = ""                # which dependency/stage, when it applies

    @property
    def sha(self) -> str:
        return fingerprint(self.category, self.db, self.source, self.detail)

    @property
    def key(self) -> tuple:
        return (self.db, self.source)


def findings(h: dict) -> list[_Finding]:
    """Every current problem/blocker/failed category in an assessment, read
    from its structured fields (never by parsing `problems`). Duplicates —
    two failed digests of one db with one category — collapse onto the first;
    the assessment's lists are sorted, so which one that is stays stable."""
    out: list[_Finding] = []
    wiki = h.get("wiki") or {}       # absent wiki *info* concludes nothing
    if wiki and not (wiki.get("present") and wiki.get("git")):
        out.append(_Finding("wiki_missing", "-", "-",   # absent or not a repo
                            {"path": wiki.get("path"), "git": wiki.get("git")}))
    if wiki.get("stray"):
        out.append(_Finding("dirty_tree", "-", "-", {"stray": wiki["stray"][:5]}))
    sources = h.get("sources", [])
    for p in sources:
        if p["state"] in ("collection_failure", "source_silent"):
            out.append(_Finding(p["state"], "-", p["source"],
                                {"latest_event": p["latest_event"],
                                 "age_hours": p["age_hours"]}))
    if sources and all(p["state"] == "unknown" for p in sources):
        out.append(_Finding("es_unreachable", "-", "-",
                            {"sources_probed": len(sources)}))
    for m in h.get("watermarks", []):
        if m["stale"]:
            out.append(_Finding("stale_watermark", m["db"], "-",
                                {"watermark": m["watermark"],
                                 "age_hours": m["age_hours"]}))
    for f in h.get("failures", []):
        out.append(_Finding(f["category"], f["db"], "-",
                            {"digest": f["digest"], "at": f["at"],
                             "retryable": f["retryable"]}))
    for b in h.get("backlog", []):
        out.append(_Finding("digest_backlog", b["db"], "-",
                            {"digest": b["digest"], "decision": b["decision"]}))
    for s in h.get("stale_stages") or []:
        # one per stage: `report` stopping and `research` stopping are
        # different failures with different next commands
        out.append(_Finding("stage_stale", "-", "-",
                            {"task": s["task"], "at": s["at"],
                             "age_h": s["age_h"],
                             "threshold_h": s["threshold_h"]},
                            detail=s["task"]))
    for p in (h.get("dependencies") or {}).get("problems", []):
        out.append(_Finding("dependency_failure", "-", "-",
                            {"check": p["check"], "detail": p["detail"]},
                            detail=p["detail"]))
    seen, uniq = set(), []
    for f in out:
        if f.sha not in seen:
            seen.add(f.sha)
            uniq.append(f)
    return uniq


@dataclass
class Alert:
    """What a sink receives. Facts and one command — see the module docstring
    for what deliberately never appears here."""

    run_id: str
    fingerprint: str
    category: str
    db: str
    source: str
    reason: str                     # new | escalation
    escalated_from: str | None
    window: dict
    first_seen: str
    at: str
    last_success: dict
    recovery_hint: str

    def to_dict(self) -> dict:
        return asdict(self)


def _last_success(h: dict) -> dict:
    """`{stage: timestamp or None}` — the timestamps only, no detail strings."""
    return {k: (v or {}).get("at") for k, v in (h.get("last_success") or {}).items()}


def evaluate(health: dict, state: dict, now: str,
             run_id: str = "-") -> tuple[list[Alert], dict]:
    """Pure: an assessment plus the previous alert state in, the alerts to send
    plus the next state out. Nothing is sent, written, or probed here.

    Alerts fire for a fingerprint absent from `state`, which is also how a
    material escalation surfaces (the category is part of the fingerprint);
    `reason` says which of the two it was. Fingerprints that disappeared are
    dropped, and listed under `recovered` unless a *new* category arrived for
    the same (db, source) — that is the escalation, not a recovery."""
    prior = dict((state or {}).get("fingerprints") or {})
    current = findings(health)
    cur_by_key: dict[tuple, set] = {}
    for f in current:
        cur_by_key.setdefault(f.key, set()).add(f.category)
    prior_by_key: dict[tuple, set] = {}
    for e in prior.values():
        prior_by_key.setdefault((e.get("db", "-"), e.get("source", "-")),
                                set()).add(e.get("category"))

    def replaced(key: tuple) -> list[str]:
        return sorted(prior_by_key.get(key, set()) - cur_by_key.get(key, set()))

    def arrived(key: tuple) -> set:
        return cur_by_key.get(key, set()) - prior_by_key.get(key, set())

    to_send, fps = [], {}
    for f in current:
        old = prior.get(f.sha)
        if old:  # still broken, identically: bookkeeping only
            fps[f.sha] = {**old, "last_seen": now,
                          "count": int(old.get("count", 0)) + 1}
            continue
        was = replaced(f.key)
        fps[f.sha] = {"first_seen": now, "last_seen": now, "count": 1,
                      "alerted_at": now, "category": f.category,
                      "db": f.db, "source": f.source,
                      **({"detail": f.detail} if f.detail else {})}
        to_send.append(Alert(
            run_id=run_id, fingerprint=f.sha, category=f.category, db=f.db,
            source=f.source, reason="escalation" if was else "new",
            escalated_from=was[0] if was else None, window=f.window,
            first_seen=now, at=now, last_success=_last_success(health),
            recovery_hint=recovery_hint(f.category, f.db, f.source,
                                        detail=f.detail or None)))

    recovered = [
        {"fingerprint": sha, "category": e.get("category"), "db": e.get("db"),
         "source": e.get("source"), "first_seen": e.get("first_seen"),
         "last_seen": e.get("last_seen"), "count": e.get("count"),
         "recovered_at": now}
        for sha, e in sorted(prior.items())
        if sha not in fps and not arrived((e.get("db", "-"), e.get("source", "-")))
    ]
    return to_send, {"schema_version": ALERT_SCHEMA_VERSION,
                     "fingerprints": fps, "recovered": recovered}


def load_state(path: Path) -> dict:
    """Versioned state, tolerating an absent file and a legacy (unversioned)
    bare fingerprint mapping — same convention as state.py."""
    path = Path(path)
    empty = {"schema_version": ALERT_SCHEMA_VERSION, "fingerprints": {},
             "recovered": []}
    if not path.exists():
        return empty
    d = json.loads(path.read_text())
    v = d.get("schema_version", 0)
    if v > ALERT_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version {v} is newer than the version "
                         f"this code supports ({ALERT_SCHEMA_VERSION})")
    if "schema_version" not in d:
        return {**empty, "fingerprints": d}  # legacy: bare flat mapping
    return {**empty, "fingerprints": d.get("fingerprints", {}),
            "recovered": d.get("recovered", [])}


def save_state(path: Path, state: dict) -> None:
    """Write only when the content changed, so a run over a healthy pipeline
    leaves the file (and its mtime) untouched — and creates none at all when
    there is nothing to remember."""
    path = Path(path)
    text = json.dumps(state, indent=1, sort_keys=True)
    if path.exists():
        if path.read_text() == text:
            return
    elif not state["fingerprints"] and not state["recovered"]:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _atext(value: object) -> str:
    """A string out of the state file. `None` reads as absent rather than as
    the word "None"."""
    return "" if value is None else str(value)


def _acount(value: object) -> int:
    """A whole count out of the state file, or 0. `bool` is excluded: `True`
    is an `int` to Python and a flag to every writer here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


@dataclass(frozen=True)
class OpenAlert:
    """One entry of `.state/alerts.json` as a reader outside this module may
    have it.

    `count` is *assessments that saw the finding*, not failures — `evaluate`
    increments it once per assessment the fingerprint was still present in, so
    it reads 43 for a fingerprint whose runs failed 23 times. It never reaches
    a caller without this name on it.

    The file is bookkeeping and not history: `evaluate` drops a fingerprint
    the moment the finding clears, so what is here is what is open *now*, and
    "absent" means "not open", never "never happened"."""

    fingerprint: str
    first_seen: str = ""
    last_seen: str = ""
    count: int = 0


#: Which key of an `alerts.json` fingerprint entry becomes which `OpenAlert`
#: field. `events._PROJECT`'s form: no `**entry`, no `dict(entry)`, so a key a
#: future `evaluate` starts writing has no path out of this module.
_OPEN_KEYS: Mapping[str, tuple[str, Callable[[object], object]]] = {
    "first_seen": ("first_seen", _atext),
    "last_seen": ("last_seen", _atext),
    "count": ("count", _acount),
}

#: Every other key `evaluate` writes into an entry, with why it stops here.
#: The completeness test runs `evaluate` and asserts its entry keys are
#: exactly `_OPEN_KEYS | _OPEN_DROPPED`, so a new field is a decision and not
#: a silent omission.
_OPEN_DROPPED: Mapping[str, str] = {
    "alerted_at": "when a sink was told, which is the dispatcher's "
                  "bookkeeping; the operator's question is since when, and "
                  "`first_seen` answers it",
    "category": "the group already spells it; two spellings could disagree",
    "db": "same",
    "source": "always '-' for every fingerprint a run-derived group can mint, "
              "so carrying it would suggest a dimension this join cannot vary",
    "detail": "free text out of the assessment; the group carries its own "
              "`sample_error` and two free-text fields on one row is one too "
              "many",
}


def open_alerts(state_dir: Path) -> Mapping[str, OpenAlert]:
    """Every currently-open fingerprint, by fingerprint.

    `load_state` already tolerates an absent file and the legacy bare-mapping
    shape; this projects what it returns and adds nothing. A file read, not a
    probe — safe from a request handler.

    `recovered` is deliberately not read. It holds only the generation that
    cleared at the last dispatch, so presenting it as history would be the
    fabrication this module exists to avoid."""
    held = load_state(Path(state_dir) / ALERT_STATE).get("fingerprints") or {}
    out: dict[str, OpenAlert] = {}
    for sha, raw in held.items():
        entry = raw if isinstance(raw, Mapping) else {}
        out[str(sha)] = OpenAlert(
            fingerprint=str(sha),
            **{name: coerce(entry[key])
               for key, (name, coerce) in _OPEN_KEYS.items() if key in entry})
    return out


@dataclass(frozen=True)
class Recurrence:
    """One failure that happened more than once, as the run logs recorded it.

    Keyed by `fingerprint(category, db, "-")` — this module's *existing*
    namespace, not a second one. `source` is always "-" because a `RunEvent`
    carries no source dimension; a source-scoped finding therefore has no
    run-derived group, and `alert` being None means "no open alert under a
    db-scoped fingerprint", never "recovered".

    `count` is failures observed in what the logs still hold. `alert.count` is
    assessments that saw the finding. They differ, they measure different
    things, and both cross so neither has to stand for the other.

    `sample_error` is the newest `RunEvent.error` in the group, verbatim. It
    is OPERATOR-classified free text and is here on exactly the terms
    `wire.db_run_json` already carries it on: one field, drawn as text, never
    a key, never a heading, never parsed. It earns its place — live, the free
    text splits `harness_error` into "provider unreachable" and "invalid
    structured proposal", which the category cannot."""

    fingerprint: str
    category: str
    db: str
    count: int
    days: int
    first_seen: str
    last_seen: str
    commands: tuple[str, ...]
    sample_error: str
    alert: OpenAlert | None


def _failed(ev: events.RunEvent) -> bool:
    return ev.kind in (events.Kind.RUN, events.Kind.DB_RUN) \
        and bool(ev.outcome == "failed" or ev.error_category)


def recurring(evs: Sequence[events.RunEvent],
              state_dir: Path) -> tuple[Recurrence, ...]:
    """Failures in `evs` grouped by `(error_category, db)`, most frequent
    first, then by fingerprint so ties are stable.

    A RUN or DB_RUN event is a failure when `outcome == "failed"` or it
    carries an `error_category`; a missing category groups under "unknown"
    rather than being dropped, so the count and the run list agree. STAGE
    events never group: they carry neither, and their 205 live `rolled_back`
    lines would swamp 151 categorised failures with a different kind of
    thing. They are counted on the time axis instead (`events.Totals`).

    A deliberate skip is not a failure and never reaches here: it carries no
    outcome and no category, which is `health.deliberate_skip`'s rule as the
    recorder already applied it — this does not restate it.

    `days` buckets through `events._bucket`, so a group's spread and the daily
    trend beside it on the same screen are the same calendar.

    An open alert whose runs have aged out of the capped logs gets no row:
    there is no run-derived group to decorate, and minting one out of the
    state file would present dispatcher bookkeeping as recorded history.

    Window-bounded by whatever `evs` holds, which is what `coverage` on the
    same envelope describes. There is no `since` parameter: a second
    truncation rule would be a second story about what is missing
    (`api.runs`' reason)."""
    groups: dict[tuple[str, str], list[events.RunEvent]] = {}
    for ev in evs:
        if _failed(ev):
            groups.setdefault((ev.error_category or "unknown",
                               ev.db or "-"), []).append(ev)
    held = open_alerts(state_dir)
    rows = []
    for (category, db), members in groups.items():
        stamps = sorted(ev.at for ev in members if ev.at)
        errors = sorted((ev.at, ev.error) for ev in members if ev.error)
        sha = fingerprint(category, db, "-")
        rows.append(Recurrence(
            fingerprint=sha, category=category, db=db, count=len(members),
            days=len({day for ev in members if (day := events._bucket(ev.at))}),
            first_seen=stamps[0] if stamps else "",
            last_seen=stamps[-1] if stamps else "",
            commands=tuple(sorted({ev.command for ev in members
                                   if ev.command})),
            sample_error=errors[-1][1] if errors else "",
            alert=held.get(sha)))
    return tuple(sorted(rows, key=lambda r: (-r.count, r.fingerprint)))


class Sink(Protocol):
    """Where an alert goes. Email/webhook/desktop transport is out of scope by
    design: implement this one method and select it in `alerts.sink`."""

    def send(self, alert: dict) -> None: ...


class StderrSink:
    """One JSON line per alert on stderr — visible in cron mail."""

    def send(self, alert: dict) -> None:
        print("ALERT " + json.dumps(alert, sort_keys=True, default=str),
              file=sys.stderr)


@dataclass
class FileSink:
    """Append one JSON line per alert to `path` (JSONL, never rotated here)."""

    path: Path

    def send(self, alert: dict) -> None:
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(alert, sort_keys=True, default=str) + "\n")


@dataclass
class WebhookSink:
    """POST one alert as a JSON object to `url` (stdlib urllib — no new
    dependency, no client library). No retries by design: `dispatch` already
    isolates and counts a sink failure, and a retry loop inside a cron run
    would delay the pipeline for a transport that is nobody's source of
    truth. The receiving end decides what an alert means; this only delivers
    the same dict every other sink gets."""

    url: str
    timeout: float = 5.0

    def send(self, alert: dict) -> None:
        body = json.dumps(alert, sort_keys=True, default=str).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            r.read()          # drain, so the connection closes cleanly


@dataclass
class MultiSink:
    """Fan every alert out to all configured sinks. One dead transport must
    never silence the others, so each is tried and their failures are raised
    together afterwards — `dispatch` counts and warns about that exactly as
    it does for a single failing sink."""

    sinks: list

    def send(self, alert: dict) -> None:
        errors = []
        for s in self.sinks:
            try:
                s.send(alert)
            except Exception as e:  # noqa: BLE001 — one dead sink is not all of them
                errors.append(f"{type(s).__name__}: {e}")
        if errors:
            raise RuntimeError("; ".join(errors))


def _sink(name: str, cfg, a: dict) -> Sink:
    """One named sink. An unknown name (or a webhook without a URL) raises:
    silently degrading to stderr means an operator who configured delivery
    gets none and is never told."""
    if name == "stderr":
        return StderrSink()
    if name == "file":
        return FileSink(Path(a.get("file") or Path(cfg.state_dir) / "alerts.jsonl"))
    if name == "webhook":
        url = os.environ.get(WEBHOOK_URL_ENV) or a.get("webhook_url")
        if not url:
            raise ValueError(f"alerts sink 'webhook' needs alerts.webhook_url "
                             f"(or the {WEBHOOK_URL_ENV} environment variable)")
        return WebhookSink(str(url), float(a.get("webhook_timeout_seconds", 5.0)))
    raise ValueError(f"unknown alerts sink {name!r} (stderr | file | webhook)")


def make_sink(cfg) -> Sink:
    """The configured sink(s): `alerts.sinks: [file, webhook]` (a list, which
    wins when present) or the single `alerts.sink: <name>`, defaulting to
    stderr. Raises ValueError on a name it does not know — `dispatch` turns
    that into a warned, counted sink error, so a config typo is loud but
    still never fails the run that reported the failure."""
    a = getattr(cfg, "alerts", None) or {}
    names = [str(n) for n in (a.get("sinks") or [a.get("sink") or "stderr"])]
    sinks = [_sink(n, cfg, a) for n in names]
    return sinks[0] if len(sinks) == 1 else MultiSink(sinks)


def enabled(cfg) -> bool:
    """Alerts are off unless `alerts.enabled` is set — disabled by default."""
    return bool((getattr(cfg, "alerts", None) or {}).get("enabled"))


def dispatch(cfg, health: dict, run_id: str, *, now: str | None = None,
             sink: Sink | None = None) -> dict:
    """Evaluate one assessment, send what is new, persist the next state, and
    return the run-health facts (`alerted`, `recovered`, `sink_errors`). A sink
    exception is warned about and counted, never raised: a broken alert
    transport must not fail the run it was reporting on. The same holds for a
    sink that cannot even be built (an unknown name, a webhook with no URL) —
    with one difference: nothing was delivered, so the state is deliberately
    *not* written and the pending findings still alert once the config is
    fixed."""
    now = now or _now()
    path = Path(cfg.state_dir) / ALERT_STATE
    to_send, new_state = evaluate(health, load_state(path), now, run_id)
    try:
        sink = sink or make_sink(cfg)
    except Exception as e:  # noqa: BLE001 — a config error must not fail the run
        print(f"warning: alert sink unavailable: {e}", file=sys.stderr)
        # `recovered: 0`, not the count: nothing was delivered and the state
        # was deliberately not persisted, so those recoveries are still
        # pending — reporting them now would re-report them every tick
        return {"alerted": 0, "recovered": 0, "sink_errors": 1}
    errors = 0
    for a in to_send:
        try:
            sink.send(a.to_dict())
        except Exception as e:  # noqa: BLE001 — deliberately isolated
            errors += 1
            print(f"warning: alert sink failed: {e}", file=sys.stderr)
    save_state(path, new_state)
    facts = {"alerted": len(to_send), "recovered": len(new_state["recovered"])}
    return {**facts, "sink_errors": errors} if errors else facts
