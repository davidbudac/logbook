#!/usr/bin/env python3
"""Emit the derived JSONL streams filebeat ships to ELK. Stdlib only.

Two dbwiki state files cannot be tailed directly:

  .state/ingest_ledger.json   full-file rewrite every run — no append to tail
  .state/run_health.jsonl     tailable, but its dbs[] array (one entry per db
                              per run) is what per-db dashboards aggregate on,
                              and ES ingest pipelines cannot split one doc
                              into many

So this script (idempotent; run from cron every 30 min, see elk/README.md)
derives four append-only files under .state/elk/ for filebeat:

  db_runs.jsonl        one doc per (run, db) from run_health's dbs[]
  ledger_events.jsonl  one doc per observed ledger entry change,
                       keyed by (path, at, status, run_id)
  schedule.jsonl       one doc per config/schedule.json entry per snapshot,
                       deduped on an unchanged next_run_at
  queue_state.jsonl    one doc per snapshot of the analyst git-queue

Every doc carries a deterministic event_id which filebeat sends as _id, so
re-emission after a lost state file — or filebeat re-reading after the
rotation below — deduplicates in ES instead of double-counting.

State (which events were already emitted) lives in .state/elk/emitter_state.json.
Output files are rotated in place once they exceed MAX_LINES; ES drops the
re-shipped duplicates by _id.

A source that exists but cannot be read or parsed is a problem, not a reason
to ship nothing quietly: every reader collects such problems, main() prints
one `emit_derived: <source>: <reason>` line per problem on stderr and exits 1
— after emitting whatever the other sources did produce. A file that was
simply never created (no run_health.jsonl yet, no analyst queue) is not a
problem. Cron redirects both streams into .state/elk/emitter.log.
"""

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path


def _use_repo(repo: Path) -> None:
    """(Re)point every path at `repo`. Called once at import for the repo this
    script lives in; tests call it again with a temporary tree."""
    global REPO, STATE_DIR, OUT_DIR, STATE_FILE, DB_RUNS_FILE, LEDGER_FILE
    global SCHEDULE_FILE, QUEUE_STATE_FILE, SCHEDULE_CONFIG, CONFIG_FILE
    REPO = Path(repo)
    STATE_DIR = REPO / ".state"
    OUT_DIR = STATE_DIR / "elk"
    STATE_FILE = OUT_DIR / "emitter_state.json"
    DB_RUNS_FILE = OUT_DIR / "db_runs.jsonl"
    LEDGER_FILE = OUT_DIR / "ledger_events.jsonl"
    SCHEDULE_FILE = OUT_DIR / "schedule.jsonl"
    QUEUE_STATE_FILE = OUT_DIR / "queue_state.jsonl"
    SCHEDULE_CONFIG = REPO / "config" / "schedule.json"
    CONFIG_FILE = REPO / "config" / "dbwiki.yaml"


_use_repo(Path(__file__).resolve().parents[2])

MAX_LINES = 20000
KEEP_LINES = 10000
MAX_STATE_IDS = 40000
RECENT_IDS_WINDOW = 500      # how far back to look for schedule dedup
DEFAULT_ANALYST_STALE_HOURS = 26  # mirrors dbwiki.health's default

DB_ENTRY_FIELDS = [
    "db", "decision", "events", "notable", "notable_events", "notable_groups",
    "model_tier", "commit", "content_hash", "digest",
    "watermark_before", "watermark_after", "window", "telemetry",
]
LEDGER_FIELDS = [
    "status", "at", "commit", "run_id", "adapter", "model", "model_tier",
    "mode", "task", "duration_s", "digest_bytes", "pages_touched",
    "incidents_opened", "incidents_updated", "lint_findings", "notable",
    "rolled_back", "timed_out", "validation_ok", "usage", "summary",
    "flags", "incidents", "content_hash", "window_to",
]


# ---- cron next-run calculator -------------------------------------------------

def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    """One cron field (`*`, `*/n`, `a,b`, `a-b`, `a-b/n`, `n/step`) -> the set
    of matching values. Cheap and permissive: this only ever parses the
    handful of expressions in config/schedule.json, not arbitrary user input."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
        else:
            base, step = part, 1
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(base)
            end = hi if step != 1 else start  # "n/step" -> n, n+step, ... hi
        values.update(v for v in range(start, end + 1, step) if lo <= v <= hi)
    return values


def _parse_cron(expr: str):
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron expression must have 5 fields: {expr!r}")
    minute, hour, dom, month, dow = fields
    return {
        "minutes": _parse_field(minute, 0, 59),
        "hours": _parse_field(hour, 0, 23),
        "doms": _parse_field(dom, 1, 31),
        "months": _parse_field(month, 1, 12),
        "dows": _parse_field(dow, 0, 6),
        "dom_star": dom.strip() == "*",
        "dow_star": dow.strip() == "*",
    }


def _day_matches(t: dt.datetime, f: dict) -> bool:
    """Standard cron dom/dow semantics: when both fields are restricted, a
    day matching *either* one qualifies (OR); a `*` field never constrains."""
    cron_dow = (t.weekday() + 1) % 7  # python Mon=0..Sun=6 -> cron Sun=0..Sat=6
    dom_ok = t.day in f["doms"]
    dow_ok = cron_dow in f["dows"]
    if f["dom_star"] and f["dow_star"]:
        return True
    if f["dom_star"]:
        return dow_ok
    if f["dow_star"]:
        return dom_ok
    return dom_ok or dow_ok


def next_run(cron_expr: str, after: dt.datetime, max_days: int = 4 * 366) -> dt.datetime:
    """First local time strictly after `after` that `cron_expr` fires at,
    keeping `after`'s tzinfo (fixed-offset, so this ignores DST transitions —
    fine for the every-few-hours-to-monthly schedules this repo uses)."""
    f = _parse_cron(cron_expr)
    t = (after + dt.timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(max_days + 1):
        if t.month in f["months"] and _day_matches(t, f):
            for h in sorted(f["hours"]):
                for m in sorted(f["minutes"]):
                    cand = t.replace(hour=h, minute=m)
                    if cand >= t:
                        return cand
        t = (t + dt.timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError(f"cron expression never fires within {max_days} days: {cron_expr!r}")


# ---- naive config helpers (stdlib only — no PyYAML) ---------------------------

def _top_level_scalar(text: str, key: str, default=None):
    m = re.search(rf"^{re.escape(key)}:\s*(\S+)", text, re.MULTILINE)
    if not m:
        return default
    return m.group(1).split("#")[0].strip() or default


def _section_scalar(text: str, section: str, key: str, default=None):
    sec = re.search(rf"^{re.escape(section)}:\s*\n((?:[ \t]+.*\n?)*)", text, re.MULTILINE)
    if not sec:
        return default
    m = re.search(rf"^[ \t]+{re.escape(key)}:\s*(\S+)", sec.group(1), re.MULTILINE)
    if not m:
        return default
    return m.group(1).split("#")[0].strip() or default


def _wiki_repo() -> Path:
    try:
        text = CONFIG_FILE.read_text()
    except OSError:
        return REPO / "wiki"
    return REPO / _top_level_scalar(text, "wiki_repo", "wiki")


def _analyst_stale_hours() -> float:
    try:
        text = CONFIG_FILE.read_text()
    except OSError:
        return DEFAULT_ANALYST_STALE_HOURS
    return float(_section_scalar(text, "analyst", "stale_hours",
                                 DEFAULT_ANALYST_STALE_HOURS))


# ---- time helpers (mirrors dbwiki.health, duplicated to stay stdlib-only) -----

def _now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(ts) -> dt.datetime | None:
    if not ts:
        return None
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _hours_between(a, b) -> float | None:
    da, db = _parse_ts(a), _parse_ts(b)
    if da is None or db is None:
        return None
    return round((db - da).total_seconds() / 3600, 1)


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    for key in ("db_runs", "ledger"):
        ids = state.get(key, [])
        if len(ids) > MAX_STATE_IDS:
            state[key] = ids[-MAX_STATE_IDS:]
    STATE_FILE.write_text(json.dumps(state))


def rotate(path: Path) -> None:
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    if len(lines) > MAX_LINES:
        path.write_text("\n".join(lines[-KEEP_LINES:]) + "\n")


def append_docs(path: Path, docs: list[dict]) -> None:
    if not docs:
        return
    with path.open("a") as fh:
        for doc in docs:
            fh.write(json.dumps(doc, sort_keys=True, default=str) + "\n")


def emit_db_runs(state: dict, problems: list[str]) -> int:
    src = STATE_DIR / "run_health.jsonl"
    if not src.exists():
        return 0  # nothing has run yet — not a problem
    try:
        text = src.read_text()
    except OSError as e:
        problems.append(f"run_health.jsonl: unreadable ({e})")
        return 0
    seen = set(state.get("db_runs", []))
    docs = []
    bad_lines = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            bad_lines += 1
            continue
        run_id = run.get("run_id")
        if not run_id or run_id in seen:
            continue
        for entry in run.get("dbs") or []:
            db = entry.get("db")
            if not db:
                continue
            doc = {
                "event_id": f"{run_id}:{db}",
                "run_id": run_id,
                "command": run.get("command"),
                "run_outcome": run.get("outcome"),
                "error_category": run.get("error_category"),
                "started": run.get("started"),
            }
            for field in DB_ENTRY_FIELDS:
                if field in entry:
                    doc[field] = entry[field]
            # backfill runs record notable as a count; run ticks as a bool.
            if "notable" in doc:
                doc["notable"] = bool(doc["notable"])
            # dbs[] repeats one reason code per matching event group; the
            # unique set is what dashboards slice on, the count keeps volume.
            reasons = entry.get("decision_reasons") or []
            doc["decision_reasons"] = sorted(set(reasons))
            doc["decision_reason_count"] = len(reasons)
            docs.append(doc)
        seen.add(run_id)
    if bad_lines:
        problems.append(f"run_health.jsonl: {bad_lines} unparsable line(s) skipped")
    append_docs(DB_RUNS_FILE, docs)
    state["db_runs"] = list(seen)
    return len(docs)


def _ledger_entries(ledger: dict) -> dict:
    """digest path -> entry, from either ledger shape: the versioned
    {"schema_version": N, "entries": {...}} file, or the legacy bare flat
    mapping that dbwiki.state._load_wrapped still tolerates (and that the live
    .state/ingest_ledger.json still is). Duplicated instead of imported —
    this script stays stdlib-only so cron runs it without the venv."""
    if "schema_version" in ledger and "entries" in ledger:
        return ledger.get("entries") or {}
    return {k: v for k, v in ledger.items() if k != "schema_version"}


def emit_ledger(state: dict, problems: list[str]) -> int:
    src = STATE_DIR / "ingest_ledger.json"
    if not src.exists():
        return 0  # nothing ingested yet — not a problem
    try:
        ledger = json.loads(src.read_text())
    except OSError as e:
        problems.append(f"ingest_ledger.json: unreadable ({e})")
        return 0
    except json.JSONDecodeError as e:
        problems.append(f"ingest_ledger.json: unparsable ({e})")
        return 0
    seen = set(state.get("ledger", []))
    docs = []
    for path, entry in _ledger_entries(ledger).items():
        key = f"{path}|{entry.get('at')}|{entry.get('status')}|{entry.get('run_id')}"
        event_id = hashlib.sha1(key.encode()).hexdigest()[:20]
        if event_id in seen:
            continue
        doc = {"event_id": event_id, "digest_path": path}
        # digests/<db>/<day>.json
        parts = Path(path).parts
        if len(parts) >= 3 and parts[0] == "digests":
            doc["db"] = parts[1]
            doc["day"] = Path(parts[2]).stem
        for field in LEDGER_FIELDS:
            if field in entry:
                doc[field] = entry[field]
        # entries that were only ever skipped carry a last_decision but no
        # ingest record; give them an explicit status for the dashboards.
        doc.setdefault("status", "not_ingested")
        decision = entry.get("last_decision") or {}
        doc["decision_outcome"] = decision.get("outcome")
        doc["decision_explanation"] = decision.get("explanation")
        doc["decision_reason_codes"] = sorted(
            {r.get("code") for r in decision.get("reasons") or [] if r.get("code")}
        )
        docs.append(doc)
        seen.add(event_id)
    append_docs(LEDGER_FILE, docs)
    state["ledger"] = list(seen)
    return len(docs)


def _recent_event_ids(path: Path, limit: int = RECENT_IDS_WINDOW) -> set[str]:
    """event_ids already in the last `limit` lines of `path` — schedule
    dedup lives in the file itself (not emitter_state.json) so an unchanged
    next_run_at simply never gets re-appended, keeping the file from
    growing every 30 minutes for entries that fire monthly."""
    if not path.exists():
        return set()
    ids = set()
    for ln in path.read_text().splitlines()[-limit:]:
        try:
            eid = json.loads(ln).get("event_id")
        except json.JSONDecodeError:
            continue
        if eid:
            ids.add(eid)
    return ids


def emit_schedule(problems: list[str]) -> int:
    try:
        cfg = json.loads(SCHEDULE_CONFIG.read_text())
    except OSError as e:
        problems.append(f"schedule.json: unreadable ({e})")  # checked in: absence is a breakage
        return 0
    except json.JSONDecodeError as e:
        problems.append(f"schedule.json: unparsable ({e})")
        return 0
    now_local = dt.datetime.now().astimezone()
    at = _now_utc()
    seen = _recent_event_ids(SCHEDULE_FILE)
    docs = []
    for entry in cfg.get("entries") or []:
        cron_expr = entry.get("cron")
        name = entry.get("entry")
        if not cron_expr or not name:
            continue
        try:
            nxt = next_run(cron_expr, now_local)
        except ValueError as e:
            problems.append(f"schedule.json: entry {name}: {e}")
            continue
        event_id = hashlib.sha1(f"{name}|{nxt.isoformat()}".encode()).hexdigest()[:20]
        if event_id in seen:
            continue
        doc = {
            "event_id": event_id, "at": at, "entry": name, "cron_expr": cron_expr,
            "command": entry.get("command"), "node": entry.get("node"),
            "description": entry.get("description"),
            "next_run_at": nxt.isoformat(), "schema_version": 1,
        }
        try:
            nxt2 = next_run(cron_expr, nxt)
            doc["interval_s"] = int((nxt2 - nxt).total_seconds())
        except ValueError:
            pass  # omit interval_s if the second lookup is awkward
        docs.append(doc)
        seen.add(event_id)
    append_docs(SCHEDULE_FILE, docs)
    return len(docs)


def emit_queue_state(problems: list[str]) -> int:
    queue_root = _wiki_repo() / "queue"
    if not queue_root.is_dir():
        # analyst never enabled: a zeroed snapshot every 30 minutes forever is
        # noise, not data. Absence is not a problem either.
        return 0
    stale_hours = _analyst_stale_hours()
    at = _now_utc()

    def records(sub: str) -> list[dict]:
        d = queue_root / sub
        if not d.is_dir():
            return []
        out = []
        for p in sorted(d.glob("*.json")):
            try:
                out.append(json.loads(p.read_text()))
            except (OSError, json.JSONDecodeError) as e:
                problems.append(f"queue/{sub}/{p.name}: {e}")
                continue
        return out

    pending, claimed, failed = records("pending"), records("claimed"), records("failed")
    oldest = min((p.get("created_at") for p in pending), default=None)
    stale_claimed = sum(
        1 for c in claimed
        if (_hours_between(c.get("claimed_at"), at) or 0) > stale_hours)
    by_category: dict[str, int] = {}
    for f in failed:
        cat = f.get("error_category") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
    counts = {
        "pending_count": len(pending), "claimed_count": len(claimed),
        "failed_count": len(failed),
        "oldest_pending_hours": _hours_between(oldest, at) if oldest else None,
        "stale_claimed_count": stale_claimed,
        "failed_by_category": by_category,
    }
    event_id = hashlib.sha1(f"queue|{at}".encode()).hexdigest()[:20]
    doc = {"event_id": event_id, "at": at, "schema_version": 1, **counts}
    append_docs(QUEUE_STATE_FILE, [doc])
    return 1


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    state = load_state()
    n_runs = emit_db_runs(state, problems)
    n_ledger = emit_ledger(state, problems)
    save_state(state)
    n_schedule = emit_schedule(problems)
    n_queue = emit_queue_state(problems)
    rotate(DB_RUNS_FILE)
    rotate(LEDGER_FILE)
    rotate(SCHEDULE_FILE)
    rotate(QUEUE_STATE_FILE)
    if n_runs or n_ledger or n_schedule:
        print(f"emitted {n_runs} db-run docs, {n_ledger} ledger docs, "
              f"{n_schedule} schedule docs, {n_queue} queue-state doc(s)")
    for problem in problems:
        print(f"emit_derived: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
