"""AWR summary input: bounded per-(db, snapshot window) aggregates from a
file, validated against a versioned contract and turned into a digest.

v1 is files only — no live Oracle, no AWR HTML, no SQL text. `docs/awr-input.md`
is the contract; this module is its executable half:

  load_awr_summary(path) -> dict     read + validate, errors name the field
  awr_digest(summary)    -> dict     digest-shaped, Compactor.content_hash-able
  emit_awr_digest(cfg, d)            write it beside the compactor's digests

Threshold-free like `patterns/metrics.yaml`: the digest carries the numbers and
`notable: false`. An AWR window is evidence for correlation, never on its own a
reason to wake an agent — see docs/provenance.md (performance evidence may be
cited only as `derived` evidence with matching db and overlapping window)."""

import datetime as dt
import json
from pathlib import Path

from . import __version__

AWR_SCHEMA_VERSION = 1
MAX_TOP_N = 20  # rows kept per top-N list; the input must already be bounded

# keys that would carry SQL text into the wiki; rejected with their own message
_SQL_TEXT_KEYS = ("sql_text", "text", "sql_fulltext", "statement", "sql")


class AwrContractError(ValueError):
    """An AWR summary file violates the v1 contract. The message names the
    offending field."""


def _fail(where: str, field: str, problem: str):
    raise AwrContractError(f"{where}: field {field!r} {problem}")


def _num(where: str, obj: dict, field: str, path: str, *, minimum=0.0) -> float:
    v = obj.get(field)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        _fail(where, path, f"must be a number, got {type(v).__name__}")
    if v < minimum:
        _fail(where, path, f"must be >= {minimum}, got {v}")
    return float(v)


def _str(where: str, obj: dict, field: str, path: str) -> str:
    v = obj.get(field)
    if not isinstance(v, str) or not v.strip():
        _fail(where, path, f"must be a non-empty string, got {type(v).__name__}")
    return v


def _iso(where: str, obj: dict, field: str, path: str) -> str:
    v = _str(where, obj, field, path)
    try:
        dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        _fail(where, path, f"must be an ISO-8601 timestamp, got {v!r}")
    return v


def _dict(where: str, obj: dict, field: str, path: str) -> dict:
    v = obj.get(field)
    if not isinstance(v, dict):
        _fail(where, path, f"must be an object, got {type(v).__name__}")
    return v


def _list(where: str, obj: dict, field: str, path: str) -> list:
    v = obj.get(field)
    if not isinstance(v, list):
        _fail(where, path, f"must be a list, got {type(v).__name__}")
    if len(v) > MAX_TOP_N:
        _fail(where, path, f"must hold at most {MAX_TOP_N} rows, got {len(v)}")
    return v


def _only(where: str, obj: dict, allowed, path: str) -> None:
    for k in obj:
        if k not in allowed:
            _fail(where, f"{path}{k}" if path else k,
                  f"is not part of AWR contract v{AWR_SCHEMA_VERSION}")


def validate_awr_summary(summary, where: str = "awr summary") -> dict:
    """Check `summary` against contract v1 and return it. Every failure names
    the field; nothing is coerced or defaulted away silently."""
    if not isinstance(summary, dict):
        raise AwrContractError(
            f"{where}: must be a JSON object, got {type(summary).__name__}")
    v = summary.get("awr_schema_version")
    if v != AWR_SCHEMA_VERSION:
        _fail(where, "awr_schema_version",
              f"must be {AWR_SCHEMA_VERSION}, got {v!r}")
    _only(where, summary, {
        "awr_schema_version", "db", "instance", "snapshot", "db_time_s",
        "elapsed_s", "load_profile", "top_waits", "top_sql", "collected"}, "")

    _str(where, summary, "db", "db")
    if "instance" in summary:
        _str(where, summary, "instance", "instance")

    snap = _dict(where, summary, "snapshot", "snapshot")
    _only(where, snap, {"begin_id", "end_id", "begin_time", "end_time"},
          "snapshot.")
    for f in ("begin_id", "end_id"):
        if not isinstance(snap.get(f), int) or isinstance(snap.get(f), bool):
            _fail(where, f"snapshot.{f}", "must be an integer snap id")
    if snap["end_id"] <= snap["begin_id"]:
        _fail(where, "snapshot.end_id", "must be greater than snapshot.begin_id")
    t0 = _iso(where, snap, "begin_time", "snapshot.begin_time")
    t1 = _iso(where, snap, "end_time", "snapshot.end_time")
    if t1 <= t0:
        _fail(where, "snapshot.end_time",
              "must be later than snapshot.begin_time")

    _num(where, summary, "db_time_s", "db_time_s")
    _num(where, summary, "elapsed_s", "elapsed_s", minimum=0.001)

    load = _dict(where, summary, "load_profile", "load_profile") \
        if "load_profile" in summary else {}
    for k, val in load.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            _fail(where, f"load_profile.{k}",
                  f"must be a numeric scalar, got {type(val).__name__}")

    for i, w in enumerate(_list(where, summary, "top_waits", "top_waits")):
        p = f"top_waits[{i}]"
        if not isinstance(w, dict):
            _fail(where, p, f"must be an object, got {type(w).__name__}")
        _only(where, w, {"event", "time_s", "pct_db_time", "waits"}, f"{p}.")
        _str(where, w, "event", f"{p}.event")
        _num(where, w, "time_s", f"{p}.time_s")
        pct = _num(where, w, "pct_db_time", f"{p}.pct_db_time")
        if pct > 100:
            _fail(where, f"{p}.pct_db_time", f"must be <= 100, got {pct}")
        if "waits" in w and (isinstance(w["waits"], bool)
                             or not isinstance(w["waits"], int)):
            _fail(where, f"{p}.waits", "must be an integer count")

    for i, s in enumerate(_list(where, summary, "top_sql", "top_sql")):
        p = f"top_sql[{i}]"
        if not isinstance(s, dict):
            _fail(where, p, f"must be an object, got {type(s).__name__}")
        for banned in _SQL_TEXT_KEYS:
            if banned in s:
                _fail(where, f"{p}.{banned}",
                      "must not be present: AWR summaries carry sql_id only, "
                      "never SQL text")
        _only(where, s, {"sql_id", "elapsed_s", "execs"}, f"{p}.")
        _str(where, s, "sql_id", f"{p}.sql_id")
        _num(where, s, "elapsed_s", f"{p}.elapsed_s")
        e = s.get("execs")
        if not isinstance(e, int) or isinstance(e, bool) or e < 0:
            _fail(where, f"{p}.execs", "must be a non-negative integer")

    if "collected" in summary:
        c = _dict(where, summary, "collected", "collected")
        for k, val in c.items():
            if not isinstance(val, str):
                _fail(where, f"collected.{k}",
                      f"must be a string, got {type(val).__name__}")
    return summary


def load_awr_summary(path) -> dict:
    """Read and validate one AWR summary file. Errors name both the file and
    the offending field. The file's own path is recorded under `collected.file`
    so an emitted digest stays self-describing."""
    p = Path(path)
    try:
        raw = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise AwrContractError(f"{p}: not valid JSON: {e}") from None
    summary = validate_awr_summary(raw, where=str(p))
    summary["collected"] = {**summary.get("collected", {}), "file": p.name}
    return summary


def _group(rule: str, template: str, count: int, message: str,
           t0: str, t1: str) -> dict:
    """A compactor-shaped notable group. `es_samples` stays empty: an AWR
    summary's provenance is the file, recorded on the digest itself."""
    return {"rule": rule, "class": "performance", "count": count,
            "first_ts": t0, "last_ts": t1, "codes": [], "message": message,
            "template": template, "es_samples": []}


def awr_digest(summary: dict) -> dict:
    """Digest-shaped dict for one AWR snapshot window.

    Shaped so `Compactor.content_hash` works and hashes everything semantic:
    it reads `deltas` plus, per source section, `total_events` and each
    notable group's (rule, template, count) — so every number that came out of
    AWR appears in a group template or count.

    `notable` is always false: v1 applies no thresholds to AWR numbers, so an
    AWR window never wakes an agent by itself."""
    validate_awr_summary(summary)
    snap = summary["snapshot"]
    t0, t1 = snap["begin_time"], snap["end_time"]
    load = summary.get("load_profile", {})
    waits, sqls = summary["top_waits"], summary["top_sql"]

    scalars = " ".join(f"{k}={v}" for k, v in sorted(load.items()))
    groups = [_group(
        "awr_window",
        f"snap={snap['begin_id']}-{snap['end_id']} "
        f"db_time_s={summary['db_time_s']} elapsed_s={summary['elapsed_s']}"
        + (f" {scalars}" if scalars else ""),
        int(round(summary["db_time_s"])),
        f"AWR snapshot {snap['begin_id']} -> {snap['end_id']}: "
        f"{summary['db_time_s']}s DB time in {summary['elapsed_s']}s elapsed",
        t0, t1)]
    for w in waits:
        n = w.get("waits")
        groups.append(_group(
            "awr_top_wait",
            f"event={w['event']} time_s={w['time_s']} "
            f"pct_db_time={w['pct_db_time']}" + (f" waits={n}" if n else ""),
            int(round(w["time_s"])),
            f"{w['event']}: {w['time_s']}s ({w['pct_db_time']}% of DB time)"
            + (f", {n} waits" if n else ""),
            t0, t1))
    for s in sqls:
        groups.append(_group(
            "awr_top_sql",
            f"sql_id={s['sql_id']} elapsed_s={s['elapsed_s']}",
            s["execs"],
            f"sql_id {s['sql_id']}: {s['elapsed_s']}s elapsed, "
            f"{s['execs']} executions",
            t0, t1))

    rows = len(groups)
    section = {
        "total_events": rows,  # aggregate rows carried, not log events
        "by_class": {"performance": rows},
        "routine_counters": {"awr_snapshots": 1, "top_waits": len(waits),
                             "top_sql": len(sqls)},
        "notable": groups,
        "dropped_notable_groups": {},
    }
    return {
        "db": summary["db"],
        "window": {"from": t0, "to": t1, "day": t0[:10]},
        "generated_by": f"dbwiki-awr/{__version__}",
        "awr_schema_version": AWR_SCHEMA_VERSION,
        "pattern_versions": {},
        "sources": {"awr": section},
        "deltas": [],
        "totals": {"events": rows, "notable_events": rows,
                   "notable_groups": rows},
        "notable": False,
        "awr": {"snapshot": snap, "instance": summary.get("instance", ""),
                "collected": summary.get("collected", {})},
    }


def digest_paths(cfg, digest: dict) -> tuple[Path, Path]:
    """`<digest_dir>/<db>/<day>-awrT<hhmm>-<hhmm>.{json,md}` — the compactor's
    layout with an `awr` suffix, so one day can hold both the log digest and
    several AWR windows."""
    w = digest["window"]
    suffix = f"-awrT{w['from'][11:13]}{w['from'][14:16]}" \
             f"-{w['to'][11:13]}{w['to'][14:16]}"
    base = cfg.digest_dir / digest["db"] / f"{w['day']}{suffix}"
    return base.with_suffix(".json"), base.with_suffix(".md")


def emit_awr_digest(cfg, digest: dict) -> tuple[Path, Path]:
    """Write the digest beside the compactor's. Refuses when the wiki repo is
    absent: digests are the wiki's raw sources, not free-standing files."""
    from .digest_md import render_md
    if not cfg.wiki_repo.exists():
        raise FileNotFoundError(
            f"wiki repo not found at {cfg.wiki_repo}; refusing to emit an AWR "
            f"digest (digests live inside the wiki)")
    jp, mp = digest_paths(cfg, digest)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(digest, indent=1))
    mp.write_text(render_md(digest))
    return jp, mp
