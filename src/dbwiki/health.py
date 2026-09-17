"""Run-health recording, the `dbwiki health` assessment, and retry planning.

Three concerns, one module, because they share one record shape:

* **record** — every command that does work appends one compact JSON line to
  `.state/run_health.jsonl` (`run_recorder` / `record_run`): windows, event
  counts, digest path/hash, trigger outcome, watermark before/after, adapter/
  validation/commit results, and a stable `error_category`. Facts only — never
  a raw log message, never a prompt body. Recording is best-effort: a failure
  warns once on stderr and is otherwise invisible to the command.
* **assess** — `assess()` reads that log plus the ingest ledger, the
  watermarks, the wiki tree and one read-only ES probe per source, and answers
  "is the pipeline healthy, and if not, why".
* **retry** — `plan_retries()` lists failed ledger entries that can be safely
  re-ingested through the normal orchestrator path.

Two distinctions this module exists to protect (see DESIGN.md):

* a quiet *source* is not a dead *collector*. The probe deliberately ignores
  db filters and looks across the whole cluster, so one quiet source beside a
  live one is `source_silent`, while nothing recent anywhere is
  `collection_failure` — the 2026-07-13 blackout incident came from calling
  the second thing the first.
* absence of events is never recovery. Recovery evidence is `resumed_flow`
  (events observed after a gap), `operator_annotation`
  (`.state/recovery_annotation.txt`), or `unknown`. Health labels it; nothing
  here closes an incident.
"""

import datetime as dt
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import requests

from dbwiki.harness import pi_model_id

HEALTH_SCHEMA_VERSION = 1
HEALTH_LOG = "run_health.jsonl"
AGENT_LOG = "agent_runs.jsonl"
ADVISORY_LOG = "advisory_runs.jsonl"
RUN_STARTS_LOG = "elk/run_starts.jsonl"
ANNOTATION_FILE = "recovery_annotation.txt"
MAX_EVENTS = 2000           # log cap: oldest lines are dropped on write
MAX_AGENT_EVENTS = 5000     # agent-run log cap, same drop-oldest policy
MAX_ADVISORY_EVENTS = 2000
MAX_RUN_START_EVENTS = 2000  # same drop-oldest policy as MAX_EVENTS
DEFAULT_STALE_HOURS = 26    # a daily cycle plus slack
DEFAULT_ANALYST_STALE_HOURS = 26  # ADR-0001 analyst.stale_hours default

#: per-stage staleness budgets (`health.stage_stale_hours` overrides them).
#: run/report are daily; lint, research and review run weekly, so a week plus
#: slack.
DEFAULT_STAGE_STALE_HOURS = {"run": 26, "report": 26, "lint": 192,
                             "research": 192, "review": 192}
#: which `last_success` kinds prove a stage ran. `run` is the daily tick:
#: it compacts every time and ingests when a window warrants it.
STAGE_SUCCESS_KINDS = {"run": ("compaction", "ingestion"),
                       "report": ("report",), "lint": ("lint",),
                       "research": ("research",), "review": ("review",)}

#: fallback local model-server root when neither the config nor pi's own
#: provider table names one. Probe paths are appended to this root, so it
#: never carries the `/v1` suffix an OpenAI-compatible base URL has.
DEFAULT_MODEL_SERVER_URL = "http://localhost:1234"
#: pi's provider table: where pi itself reads base URL and key from
PI_MODELS_JSON = Path.home() / ".pi/agent/models.json"
DEFAULT_MIN_CONTEXT = 32768   # docs/scheduling.md: 8k truncates every prompt
DEP_TIMEOUT = 3.0             # seconds; a hung dependency must not hang health
MIN_FREE_BYTES = 1 << 30      # 1 GiB under state_dir
MAX_UNPUSHED = 10             # unpushed wiki commits before it is a problem

# stable failure categories; every recorded failure carries exactly one
CATEGORIES = ("es_unreachable", "unsupported_schema", "harness_error",
              "agent_timeout", "no_result", "validation_failed", "dirty_tree",
              "commit_failed", "wiki_missing", "lock_busy", "stage_stale",
              "dependency_failure", "unknown")

# categories a plain rerun can plausibly fix (dirty_tree/wiki_missing need an
# operator; es_unreachable/unsupported_schema need the window recompacted)
RETRYABLE = ("validation_failed", "harness_error", "agent_timeout", "no_result")

COLLECTION_STATES = ("ok", "source_silent", "collection_failure", "unknown")


class WikiMissingError(RuntimeError):
    """The wiki repository this checkout points at does not exist."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        d = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _hours_between(a: str | None, b: str | None) -> float | None:
    da, db = _parse(a), _parse(b)
    if da is None or db is None:
        return None
    return round((db - da).total_seconds() / 3600, 1)


def categorize(exc: BaseException) -> str:
    """Stable failure category for an exception. Imports are local: orchestrate
    imports this module for its own recording, so the reverse must stay lazy."""
    from .harness import HarnessError, NoResultError
    from .lock import LockBusyError
    from .normalize import UnsupportedSchemaError
    from .orchestrate import ValidationError
    if isinstance(exc, WikiMissingError):
        return "wiki_missing"
    if isinstance(exc, LockBusyError):
        return "lock_busy"
    if isinstance(exc, requests.RequestException):
        return "es_unreachable"
    if isinstance(exc, UnsupportedSchemaError):
        return "unsupported_schema"
    if isinstance(exc, NoResultError):
        return "no_result"
    if isinstance(exc, HarnessError):
        return "agent_timeout" if "timed out" in str(exc) else "harness_error"
    if isinstance(exc, ValidationError):
        return "validation_failed"
    if isinstance(exc, RuntimeError):
        msg = str(exc)
        if "uncommitted changes" in msg:
            return "dirty_tree"
        if msg.startswith("git "):
            return "commit_failed"
    return "unknown"


def category_of_entry(entry: dict) -> str:
    """Failure category of a failed ledger entry: the recorded one, else
    inferred from its problem strings (entries predating that field)."""
    if entry.get("error_category"):
        return entry["error_category"]
    text = " ".join(entry.get("problems", []))
    if "timed out" in text:
        return "agent_timeout"
    if "wrote no .agent-result.json" in text:
        return "no_result"
    if "exited" in text or "invalid result JSON" in text:
        return "harness_error"
    return "validation_failed" if text else "unknown"


def new_run_id() -> str:
    """Short hex run ID. Shared with harness/ledger telemetry (WS6), so it is
    minted here once per command invocation and passed around."""
    return uuid4().hex[:12]


_warned = False


#: per-path line-count estimate, so an append does not have to read the log
#: to know whether it needs trimming. Seeded once per process per log and
#: corrected from disk on every trim; another process appending behind our
#: back only delays a trim, and the trim itself always counts for real.
_line_counts: dict[str, int] = {}


def _count_lines(path: Path) -> int:
    try:
        return path.read_bytes().count(b"\n")
    except OSError:
        return 0


def _append_capped(path: Path, event: dict, max_events: int) -> None:
    """Append one JSON line, trimming the log to its cap only when it has
    drifted past cap + 10% slack.

    The previous version re-read the whole file after every append and, over
    cap, rewrote it in place — O(file) per line on a 5000-line log, and a
    crash or a concurrent run in the window between truncate and write left
    the ELK shipper tailing a half-written log, or an empty one. Now the
    append is the only write on the common path, and a trim publishes the
    shortened log with one atomic rename. At most `max_events` lines survive
    a trim, exactly as before."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    key = str(path)
    n = _line_counts[key] = (_line_counts[key] + 1 if key in _line_counts
                             else _count_lines(path))
    if n <= max_events + max(max_events // 10, 1):
        return
    lines = path.read_text().splitlines()
    _line_counts[key] = len(lines)
    if len(lines) <= max_events:
        return                      # the estimate was stale; nothing to do
    from .state import atomic_write_text
    atomic_write_text(path, "\n".join(lines[-max_events:]) + "\n")
    _line_counts[key] = max_events


def record_run(state_dir: Path, event: dict) -> None:
    """Append one event as a JSON line to `.state/run_health.jsonl`, keeping
    the last MAX_EVENTS lines. Best-effort: a recording failure warns once on
    stderr and never propagates — telemetry must not break the pipeline."""
    global _warned
    try:
        _append_capped(Path(state_dir) / HEALTH_LOG, event, MAX_EVENTS)
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        if not _warned:
            _warned = True
            print(f"warning: run-health recording failed: {e}", file=sys.stderr)


def record_agent_run(state_dir: Path, fields: dict, tele: dict | None = None,
                     db: str | None = None, mode: str | None = None
                     ) -> str | None:
    """Append one line per attempted agent stage to `.state/agent_runs.jsonl`
    (the ELK shipper tails it — see elk/README.md). Input is the ledger
    telemetry block plus the raw harness telemetry; the token/cost `usage`
    dict is flattened into scalar fields so the line is directly indexable.
    Same discipline as record_run: facts and counts only, best-effort, and a
    failure never propagates.

    Returns the line's `event_id`, or None when nothing was written. The
    Langfuse export seeds its trace id from it (`observability.trace_id`), so
    the portal can name a stage's trace from the ledger line alone."""
    global _warned
    event_id = uuid4().hex[:12]
    try:
        event = {k: v for k, v in fields.items() if k != "usage"}
        event["event_id"] = event_id
        event["at"] = _now()
        if db is not None:
            event["db"] = db
        if mode is not None:
            event["mode"] = mode
        for key in ("exit_code", "stdout_bytes", "prompt_bytes"):
            if isinstance((tele or {}).get(key), (int, float)):
                event[key] = tele[key]
        usage = fields.get("usage")
        known = False
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "cost_usd"):
                if isinstance(usage.get(key), (int, float)):
                    event[key] = usage[key]
                    known = known or key != "cost_usd"
        event["usage_known"] = known
        _append_capped(Path(state_dir) / AGENT_LOG, event, MAX_AGENT_EVENTS)
        return event_id
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        if not _warned:
            _warned = True
            print(f"warning: run-health recording failed: {e}", file=sys.stderr)


def append_agent_run_event(state_dir: Path, event: dict) -> None:
    """Append one already-built agent-run event verbatim (ADR-0001 fold-in:
    `queue.fold_results` reads a record the analyst node wrote — because it
    cannot reach the on-prem `.state/` directly — and hands it here as-is,
    unlike `record_agent_run`, which builds the event fields itself from a
    live run). Same capped-append and best-effort discipline as
    `record_agent_run`."""
    global _warned
    try:
        _append_capped(Path(state_dir) / AGENT_LOG, event, MAX_AGENT_EVENTS)
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        if not _warned:
            _warned = True
            print(f"warning: run-health recording failed: {e}", file=sys.stderr)


def append_advisory_run(state_dir: Path, event: dict) -> None:
    """Append one already-built advisory-run event to
    `.state/advisory_runs.jsonl` — the spend history `advisory.spend` reads a
    ceiling off, and its own log because an operator's click is not an agent
    stage and must not be counted as one by anything tailing `AGENT_LOG`.
    Same capped-append and best-effort discipline as `append_agent_run_event`:
    a recording failure warns once and never reaches the caller."""
    global _warned
    try:
        _append_capped(Path(state_dir) / ADVISORY_LOG, event,
                       MAX_ADVISORY_EVENTS)
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        if not _warned:
            _warned = True
            print(f"warning: run-health recording failed: {e}", file=sys.stderr)


def known_agent_event_ids(state_dir: Path) -> set[str]:
    """`event_id`s already present in `.state/agent_runs.jsonl` — the fold-in
    idempotency key (ADR-0001, Consequences: replaying wiki history must not
    resurrect stale queue entries as duplicate telemetry). Deliberately the
    event id, not the run id: an analyst result reuses the enqueuing tick's
    `run_id` for correlation, and that tick's own ingest/placeholder entries
    already carry it — keying on run_id would drop every analyst result as a
    false duplicate."""
    path = Path(state_dir) / AGENT_LOG
    if not path.exists():
        return set()
    out: set[str] = set()
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            eid = json.loads(ln).get("event_id")
        except json.JSONDecodeError:
            continue
        if eid:
            out.add(eid)
    return out


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict]:
    """JSON lines, oldest first, `limit` keeping the newest N (`None` = all,
    `0` = none). Unparseable lines are skipped: a truncated write must not
    break a reader."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    if limit is None:
        return out
    return out[-limit:] if limit else []   # limit=0 is "none", not "all"


def read_events(state_dir: Path) -> list[dict]:
    """Recorded events, oldest first. Unparseable lines are skipped: a
    truncated write must not break `dbwiki health`."""
    return _read_jsonl(Path(state_dir) / HEALTH_LOG)


def read_agent_runs(state_dir: Path, limit: int | None = None) -> list[dict]:
    """Agent-stage events from `.state/agent_runs.jsonl`, oldest first — one
    line per attempted stage, written by `record_agent_run`. This is the only
    telemetry source that sees report/lint/research: the ingest ledger is
    keyed by digest path, so non-ingest stages have no row there. `dbwiki
    stats` reads it through here."""
    return _read_jsonl(Path(state_dir) / AGENT_LOG, limit)


@dataclass
class RunRecord:
    """One command's health event, filled in as the command runs. `add_db`
    returns the per-db fact dict so a caller can keep adding to it (validation
    result, commit sha) as the stages complete."""

    command: str
    run_id: str = field(default_factory=new_run_id)
    started: str = field(default_factory=_now)
    outcome: str = "ok"
    error_category: str | None = None
    dbs: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def add_db(self, db: str, **facts) -> dict:
        entry = {"db": db, **facts}
        self.dbs.append(entry)
        return entry

    def note(self, **facts) -> None:
        self.facts.update(facts)

    def fail(self, exc: BaseException) -> None:
        self.outcome = "failed"
        self.error_category = categorize(exc)

    def to_event(self) -> dict:
        return {
            "schema_version": HEALTH_SCHEMA_VERSION,
            "run_id": self.run_id,
            "command": self.command,
            "started": self.started,
            "finished": _now(),
            "duration_s": round(time.monotonic() - self._t0, 1),
            "outcome": self.outcome,
            "error_category": self.error_category,
            "dbs": self.dbs,
            "facts": self.facts,
        }


def _record_run_start(state_dir: Path, rec: RunRecord) -> None:
    """Append the in-flight marker to `.state/elk/run_starts.jsonl` (loop
    observability: run_health only records finish events, so a run that
    hangs or dies before finishing would otherwise be invisible). Best-effort,
    same discipline as record_run — a failure must never break the run."""
    global _warned
    try:
        event = {"event_id": f"{rec.run_id}-start", "run_id": rec.run_id,
                 "command": rec.command, "started": rec.started,
                 "pid": os.getpid(), "run_host": socket.gethostname(),
                 "schema_version": HEALTH_SCHEMA_VERSION}
        _append_capped(Path(state_dir) / RUN_STARTS_LOG, event, MAX_RUN_START_EVENTS)
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        if not _warned:
            _warned = True
            print(f"warning: run-health recording failed: {e}", file=sys.stderr)


@contextmanager
def run_recorder(state_dir: Path, command: str):
    """Time one command, categorize whatever escapes it, and always record.
    Exceptions are re-raised unchanged after being categorized."""
    rec = RunRecord(command)
    _record_run_start(state_dir, rec)
    try:
        yield rec
    except BaseException as e:
        rec.fail(e)
        record_run(state_dir, rec.to_event())
        raise
    record_run(state_dir, rec.to_event())


def _latest_event(es, cfg, source: str) -> str | None:
    """Newest @timestamp for a source across ALL databases — no db filter, so
    one db going quiet can never look like a collection failure."""
    scfg = cfg.source(source)
    ts = scfg["timestamp_field"]
    res = es.search(",".join(scfg["index_patterns"]),
                    {"size": 1, "sort": [{ts: "desc"}], "_source": [ts]})
    hits = res.get("hits", {}).get("hits", [])
    return hits[0].get("_source", {}).get(ts) if hits else None


def _edge_event(es, cfg, source: str, cutoff: str, recent: bool) -> str | None:
    """First event at/after `cutoff` (recent=True) or last one before it."""
    scfg = cfg.source(source)
    ts = scfg["timestamp_field"]
    rng = {"gte": cutoff} if recent else {"lt": cutoff}
    res = es.search(",".join(scfg["index_patterns"]), {
        "size": 1, "sort": [{ts: "asc" if recent else "desc"}],
        "_source": [ts], "query": {"bool": {"filter": [{"range": {ts: rng}}]}}})
    hits = res.get("hits", {}).get("hits", [])
    return hits[0].get("_source", {}).get(ts) if hits else None


def collection_states(cfg, es, now: str, stale_hours: float) -> list[dict]:
    """Per-source collection state. `ok` = events within stale_hours;
    `source_silent` = this source is quiet but another one is live;
    `collection_failure` = nothing recent anywhere we could probe;
    `unknown` = the probe itself failed (ES unreachable)."""
    probes = []
    for name in cfg.sources:
        try:
            latest = _latest_event(es, cfg, name)
            probes.append({"source": name, "latest_event": latest,
                           "age_hours": _hours_between(latest, now),
                           "state": None})
        except Exception as e:  # noqa: BLE001 — a probe failure is a state
            probes.append({"source": name, "latest_event": None,
                           "age_hours": None, "state": "unknown",
                           "detail": categorize(e)})
    def fresh(p: dict) -> bool:
        return p["age_hours"] is not None and p["age_hours"] <= stale_hours
    any_fresh = any(fresh(p) for p in probes)
    for p in probes:
        if p["state"] == "unknown":
            continue
        p["state"] = "ok" if fresh(p) else \
            ("source_silent" if any_fresh else "collection_failure")
    return probes


def recovery_evidence(cfg, es, states: list[dict], now: str,
                      stale_hours: float) -> dict:
    """Explicit recovery evidence only. An operator note wins over inference;
    otherwise flow that resumed after a >= stale_hours gap counts; otherwise
    `unknown`. Never derived from absence, and never closes an incident."""
    note = Path(cfg.state_dir) / ANNOTATION_FILE
    text = note.read_text().strip() if note.exists() else ""
    if text:
        return {"kind": "operator_annotation", "detail": text[:300]}
    cutoff = (_parse(now) - dt.timedelta(hours=stale_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    for p in states:
        if p["state"] != "ok":
            continue
        try:
            first = _edge_event(es, cfg, p["source"], cutoff, True)
            prev = _edge_event(es, cfg, p["source"], cutoff, False)
        except Exception:  # noqa: BLE001 — no evidence, not an error
            continue
        gap = _hours_between(prev, first)
        if gap is not None and gap >= stale_hours:
            return {"kind": "resumed_flow",
                    "detail": f"{p['source']}: events resumed at {first} after "
                              f"a {gap}h gap (last event before: {prev})"}
    return {"kind": "unknown",
            "detail": "no recovery evidence observed; absence of events is not "
                      "recovery — incidents stay open until evidence arrives"}


def _get_json(url: str, timeout: float = DEP_TIMEOUT,
              headers: dict | None = None):
    """Bounded GET of a JSON endpoint. Its own function so `assess` can be
    handed a fake one — no test may reach a real model server."""
    r = requests.get(url, timeout=timeout, headers=headers or None)
    r.raise_for_status()
    return r.json()


def _problem(detail: str, message: str) -> dict:
    """One dependency finding. `detail` is the stable key alerts hint on;
    `message` is for humans and carries the specifics."""
    return {"detail": detail, "message": message}


def uses_pi(cfg) -> bool:
    """Does any configured route reach the local model server? Structured
    stages always do, and any adapter may be pointed at it explicitly."""
    agents = getattr(cfg, "agents", {}) or {}
    research = getattr(cfg, "research", {}) or {}
    return bool(agents.get("mode") == "structured"
                or agents.get("adapter") == "pi"
                or agents.get("escalated_report") == "structured"
                or research.get("mode") == "structured"
                or research.get("adapter") == "pi")


def _pi_provider(provider: str | None) -> dict:
    """The entry pi itself would use for `provider` from its own provider
    table (`~/.pi/agent/models.json`), or {} when there is none.

    Health probes the server pi will actually talk to, so the base URL and
    key come from pi's table unless the dbwiki config overrides them — one
    place to point at a different local server, and cron needs no extra
    environment. Never raises: an unreadable table is simply no override."""
    if not provider:
        return {}
    try:
        table = json.loads(PI_MODELS_JSON.read_text())
        entry = (table.get("providers") or {}).get(provider)
        return entry if isinstance(entry, dict) else {}
    except Exception:  # noqa: BLE001 — an unreadable table is not a finding
        return {}


def _server_root(base: str) -> str:
    """Server root from a base URL that may already carry the OpenAI `/v1`
    suffix. Probe paths are appended to the root, so `http://h/v1` and
    `http://h` both have to end up as `http://h`."""
    base = base.rstrip("/")
    return base[:-3].rstrip("/") if base.endswith("/v1") else base


def _model_server(cfg, get_json) -> dict:
    """Is the local model server up, does it hold the configured models, and
    is the served context big enough? Both failures have already caused a
    silent multi-day outage (docs/scheduling.md, "What the local stages need
    to be up"), and until now nothing in dbwiki reported either.

    Which server that is follows `agents.pi`: `base_url`/`api_key_env` when
    the config names them, otherwise pi's own provider table (see
    `_pi_provider`). Servers that demand a key — unsloth studio does, LM
    Studio does not — are probed with it; without one the probe would only
    ever see 401 and cry "unreachable".

    Same logic as n8n/scripts/watchdog.py — which keeps its own copy because
    it also runs where dbwiki does not — except that the model ids are read
    from the config object instead of scraped out of the YAML."""
    pi = (getattr(cfg, "agents", {}) or {}).get("pi") or {}
    entry = _pi_provider(pi.get("provider"))
    base = _server_root(str(pi.get("base_url") or entry.get("baseUrl")
                            or DEFAULT_MODEL_SERVER_URL))
    key = (os.environ.get(pi["api_key_env"]) if pi.get("api_key_env")
           else None) or entry.get("apiKey")
    headers = {"Authorization": f"Bearer {key}"} if key else None
    want = sorted({pi_model_id(m)
                   for m in (pi.get("cheap"), pi.get("strong")) if m})
    min_context = int((getattr(cfg, "health", {}) or {})
                      .get("min_context", DEFAULT_MIN_CONTEXT))
    out = {"checked": True, "url": base, "provider": pi.get("provider"),
           "reachable": False, "configured_models": want, "loaded": [],
           "state_known": False, "notes": [], "problems": []}
    models = None
    for path in ("/api/v0/models", "/v1/models"):   # LM Studio's own API first
        try:
            data = get_json(f"{base}{path}", DEP_TIMEOUT, headers)
        except Exception as e:  # noqa: BLE001 — an unreachable server is a finding
            out["error"] = f"{type(e).__name__}: {e}"[:200]
            continue
        models = data.get("data", data) if isinstance(data, dict) else data
        out["reachable"] = True
        out["state_known"] = path.endswith("/api/v0/models")
        out.pop("error", None)
        if not out["state_known"]:
            out["notes"].append("/api/v0 unavailable; loaded state unknown")
        break
    if not out["reachable"]:
        out["problems"].append(_problem(
            "model_server_unreachable",
            f"the local model server is not reachable at {base} "
            f"(every structured stage fails as harness_error)"))
        return out

    all_ids = [m.get("id") for m in (models or []) if isinstance(m, dict)]
    for m in models or []:
        if not isinstance(m, dict) or m.get("state") not in (None, "loaded"):
            continue
        out["loaded"].append({
            "id": m.get("id"), "state": m.get("state"),
            "context_length": m.get("loaded_context_length")
                              or m.get("context_length")})
    loaded_ids = [m["id"] for m in out["loaded"]]
    # The server JIT-loads on the first request and evicts idle models after
    # the TTL, so "not loaded" is normal; "not on disk" is not.
    for w in want:
        if all_ids and w not in all_ids:
            out["problems"].append(_problem(
                "model_server_model_missing",
                f"configured model {w!r} is not served by "
                f"{pi.get('provider') or 'the local model server'} "
                f"({', '.join(str(i) for i in all_ids)})"))
        elif w not in loaded_ids:
            out["notes"].append(f"{w} not loaded right now "
                                f"(JIT loads it on the next request)")
    for m in out["loaded"]:
        if (want and m["id"] not in want) or m["context_length"] is None:
            continue
        if m["context_length"] < min_context:
            out["problems"].append(_problem(
                "model_server_context",
                f"{m['id']} is loaded with context {m['context_length']} < "
                f"{min_context} (prompts stop on `length` before answering)"))
    return out


def _portal(cfg, get_json) -> dict:
    """Is the incident workbench answering at `cfg.portal["bind"]`?

    `GET http://<bind>/api/health` through the injected `get_json` with
    `DEP_TIMEOUT`. Lock-free on both sides: health must answer while a tick
    holds the lock, and the probe is a read.

    Checked only when the config names a `portal` block
    (`cfg.portal_configured`), never on `PORTAL_DEFAULTS` alone: `bind` has a
    default, so keying the probe off it would report every Stage 0 install as
    having a dead workbench.

    Returns `{"checked", "url", "reachable", "revision", "actor",
    "problems"}`. Unreachable is `_problem("portal_unreachable", ...)`, a
    detail under the existing `dependency_failure` rather than a new
    category, exactly like `model_server_unreachable`. A reachable portal
    whose `revision` differs from the wiki's HEAD is not a problem: the
    portal reads HEAD per request and the health snapshot is older by a
    subprocess.

    `dependencies` runs it behind that predicate, so a config with no portal
    block prints nothing at all about the workbench, not even a "not checked"
    line. `alerts._DEP_HINTS["portal"]` turns a `portal_unreachable` finding
    into the restart command."""
    from .config import PORTAL_DEFAULTS
    bind = str((getattr(cfg, "portal", {}) or {}).get("bind")
               or PORTAL_DEFAULTS["bind"])
    url = f"http://{bind}/api/health"
    out = {"checked": True, "url": url, "reachable": False, "revision": None,
           "actor": None, "problems": []}
    try:
        body = get_json(url, DEP_TIMEOUT, None)
    except Exception as e:  # noqa: BLE001 — an unreachable workbench is a finding
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        out["problems"].append(_problem(
            "portal_unreachable",
            f"the incident workbench is not reachable at {url} "
            f"(nothing else is affected; incident work needs the CLI)"))
        return out
    if not isinstance(body, dict):
        out["problems"].append(_problem(
            "portal_unreachable",
            f"something other than the incident workbench is answering "
            f"at {url}"))
        return out
    out["reachable"] = True
    out["revision"] = body.get("revision")
    out["actor"] = body.get("actor")
    return out


#: adapter name -> the binary cron must find on PATH
_ADAPTER_BINARIES = {"codex": "codex", "claude": "claude", "pi": "pi",
                     "ollama": "ollama"}


def _adapters(cfg) -> dict:
    """Binaries the configured stages need. Cron's PATH is not the shell's:
    a missing adapter kills notable reports and lint with FileNotFoundError
    while the same command works by hand (docs/scheduling.md)."""
    agents = getattr(cfg, "agents", {}) or {}
    research = getattr(cfg, "research", {}) or {}
    need = {"git"}                       # every commit and probe shells out
    # lint is always agentic, so the agentic adapter is always needed; a
    # research override runs from the same cron environment
    for name in (agents.get("adapter"), research.get("adapter")):
        if name in _ADAPTER_BINARIES:
            need.add(_ADAPTER_BINARIES[name])
    if uses_pi(cfg):
        need.add("pi")
    found = {n: shutil.which(n) for n in sorted(need)}
    missing = sorted(n for n, p in found.items() if not p)
    return {"required": sorted(need), "found": found, "missing": missing,
            "problems": [_problem(f"adapter_missing:{n}",
                                  f"{n} is not on PATH")
                         for n in missing]}


def _git_out(repo: Path, *args: str) -> str:
    """`git -C repo …` stdout, or "" for any failure. Never raises: a broken
    remote is a finding, not a crash."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=DEP_TIMEOUT)
    except Exception:  # noqa: BLE001 — missing git / timeout is a finding
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


def _wiki_remote(cfg, wiki: dict) -> dict:
    """Is wiki history actually leaving this machine, and can git commit at
    all? A wiki that commits locally for a month looks perfectly healthy from
    the inside."""
    out = {"push": bool((getattr(cfg, "report", {}) or {}).get("push")),
           "problems": []}
    if not wiki.get("git"):
        return out                       # already a blocker; nothing to add
    repo = Path(cfg.wiki_repo)
    out["user_email"] = _git_out(repo, "config", "user.email")
    if not out["user_email"]:
        out["problems"].append(_problem(
            "git_identity", "the wiki repo has no git user.email; every "
            "commit fails"))
    if not out["push"]:
        return out                       # local-only by configuration
    out["upstream"] = _git_out(repo, "rev-parse", "--abbrev-ref", "@{u}") or None
    if not out["upstream"]:
        out["problems"].append(_problem(
            "wiki_no_upstream", "report.push is on but the wiki branch has no "
            "upstream; nothing is being pushed"))
        return out
    count = _git_out(repo, "rev-list", "--count", "@{u}..HEAD")
    out["unpushed"] = int(count) if count.isdigit() else None
    if (out["unpushed"] or 0) > MAX_UNPUSHED:
        out["problems"].append(_problem(
            "unpushed", f"{out['unpushed']} wiki commits are not pushed "
            f"(> {MAX_UNPUSHED})"))
    return out


def _disk(state_dir: Path) -> dict:
    """Free space under `.state/`. Everything here appends: telemetry logs,
    digests, the wiki repo — a full disk fails all of it at once."""
    out = {"path": str(state_dir), "problems": []}
    try:
        usage = shutil.disk_usage(str(state_dir))
    except Exception as e:  # noqa: BLE001 — an unreadable path is a finding
        out["error"] = f"{type(e).__name__}: {e}"[:200]
        return out
    out["free_bytes"] = usage.free
    out["free_gib"] = round(usage.free / (1 << 30), 2)
    if usage.free < MIN_FREE_BYTES:
        out["problems"].append(_problem(
            "disk", f"only {out['free_gib']} GiB free under {state_dir}"))
    return out


def dependencies(cfg, wiki: dict, *, get_json=None) -> dict:
    """Everything the stages need that the pipeline itself never reports: the
    local model server, the adapter binaries, the wiki's remote and git
    identity, disk, and the incident workbench. Each check is bounded
    (DEP_TIMEOUT), swallows its own errors and contributes `problems`
    entries — `dbwiki health` must still answer when one dependency hangs.

    `problems` is the flattened list; every entry names the `check` it came
    from and a stable `detail` that `alerts.recovery_hint` turns into the
    exact next command."""
    get_json = get_json or _get_json
    checks = {
        "model_server": (lambda: _model_server(cfg, get_json)
                         if uses_pi(cfg) else
                         {"checked": False, "problems": [],
                          "reason": "no configured route uses pi"}),
        "adapters": (lambda: _adapters(cfg)),
        "wiki_remote": (lambda: _wiki_remote(cfg, wiki)),
        "disk": (lambda: _disk(Path(cfg.state_dir))),
        "portal": (lambda: _portal(cfg, get_json)
                   if getattr(cfg, "portal_configured", False) else
                   {"checked": False, "problems": [],
                    "reason": "no portal block in the config"}),
    }
    out: dict = {"problems": []}
    for name, run in checks.items():
        try:
            out[name] = run()
        except Exception as e:  # noqa: BLE001 — a check never fails the report
            out[name] = {"error": f"{type(e).__name__}: {e}"[:200],
                         "problems": []}
        for p in out[name].get("problems", []):
            out["problems"].append({"check": name, **p})
    return out


def _compacted(d: dict) -> bool:
    """Did this database's compaction succeed? Every writer fills the digest
    facts (`events`, ...) and the watermark in one `add_db` call, and a db that
    died in compaction has neither."""
    return "events" in d or "watermark_after" in d


def _ingested(d: dict) -> bool:
    """Did this database's ingest stage succeed? `skipped` counts: an
    unchanged digest the orchestrator declined to re-ingest is the stage
    working, not failing."""
    return d.get("outcome") in ("ingested", "skipped") or bool(d.get("commit"))


def _last_successes(events: list[dict], ledger: dict) -> dict:
    """Last successful compaction / ingestion / report / lint / research /
    review, from the health log, with the ledger as the fallback for ingests
    that predate it. `_stage_staleness` turns these into problems when they get old.

    Success is read out of the per-db and per-stage *facts*, never out of the
    event's overall outcome: `cmd_run` marks the whole tick failed the moment
    one database (or the report) fails, while every other database still
    commits and the report still lands. Trusting `outcome` here froze
    compaction/ingestion/report on the first chronically broken database and
    then fired permanent `stage_stale` problems at a working pipeline."""
    got: dict[str, dict] = {}

    def note(kind: str, at: str | None, src: str, detail: str = "") -> None:
        if at and (kind not in got or at > got[kind]["at"]):
            got[kind] = {"at": at, "from": src, "detail": detail}

    for e in events:
        cmd, fin = e.get("command"), e.get("finished")
        dbs, facts = e.get("dbs") or [], e.get("facts") or {}
        if compacted := [d for d in dbs if _compacted(d)]:
            note("compaction", fin, cmd,
                 ", ".join(str(d["db"]) for d in compacted[:5]))
        if ingested := [d for d in dbs if _ingested(d)]:
            note("ingestion", fin, cmd,
                 ", ".join(str(d["db"]) for d in ingested[:5]))
        if facts.get("report"):
            note("report", fin, cmd, facts["report"])
        # lint and research are single-shot — no per-item facts to read a
        # partial success out of, so their outcome *is* the stage's
        if e.get("outcome") != "ok":
            continue
        if cmd == "lint":
            note("lint", fin, cmd,
                 f"{facts.get('findings', '')} finding(s)".strip())
        if cmd == "research":
            note("research", fin, cmd, str(facts.get("pages", "")))
        if cmd == "review":
            note("review", fin, cmd, str(facts.get("selected", "")))
    for rel, entry in ledger.items():
        if entry.get("status") == "ingested":
            note("ingestion", entry.get("at"), "ledger", rel)
    return {k: got.get(k) for k in
            ("compaction", "ingestion", "report", "lint", "research",
             "review")}


def _stage_staleness(last_success: dict, now: str, thresholds: dict) -> list[dict]:
    """Stages whose last success is older than their budget. Reporting
    `last_success` was never enough on its own: "reports failing for a week"
    read as healthy because nothing ever compared that timestamp to now.

    A stage that has *never* succeeded is deliberately absent — on a fresh
    install, or one that never enabled research, "never ran" is not a
    regression, and health that cries wolf on day one is ignored on day
    thirty."""
    from .alerts import recovery_hint  # alerts imports us; keep this lazy
    out = []
    for stage, kinds in STAGE_SUCCESS_KINDS.items():
        ats = [(last_success.get(k) or {}).get("at") for k in kinds]
        ats = [a for a in ats if a]
        if not ats:
            continue
        at = max(ats)
        age = _hours_between(at, now)
        limit = float(thresholds.get(stage, DEFAULT_STAGE_STALE_HOURS[stage]))
        if age is None or age <= limit:
            continue
        out.append({"task": stage, "at": at, "age_h": age,
                    "threshold_h": limit,
                    "hint": recovery_hint("stage_stale", detail=stage)})
    return out


def _wiki_state(cfg) -> dict:
    """Wiki presence and blocking dirt. The wiki is a separate repo that may
    simply be absent from a checkout (it is, here) — that is a reported
    category, not a crash."""
    wiki = Path(cfg.wiki_repo)
    st = {"path": str(wiki), "present": wiki.is_dir(),
          "git": (wiki / ".git").is_dir(), "stray": []}
    if not st["git"]:
        return st
    from .transaction import stray_paths
    try:
        st["stray"] = list(stray_paths(wiki))
    except Exception as e:  # noqa: BLE001 — a broken repo is a finding
        st["stray"] = []
        st["error"] = str(e)[:200]
    return st


def _exchange_state(cfg, now: str, stale_hours: float) -> dict:
    """Research exchange telemetry (ADR-0002): same shape as the analyst
    queue block plus the on-prem `redaction-leak` counter. All-empty and
    `configured: false` unless `research.exchange.path` points at a clone —
    installs that never offload must see no change."""
    empty = {"configured": False, "pending_count": 0, "oldest_pending_hours": None,
             "claimed_count": 0, "stale_claimed": [], "failed_count": 0,
             "failed_by_category": {}, "results_waiting": 0, "redaction_leaks": 0}
    rcfg = getattr(cfg, "research", {}) or {}
    path = (rcfg.get("exchange") or {}).get("path")
    state_dir = Path(getattr(cfg, "state_dir", ".state"))
    from .research_offload import leak_records
    leaks = len(leak_records(state_dir))
    if not path:
        return {**empty, "redaction_leaks": leaks}
    root = Path(path)
    if not root.is_absolute():
        root = Path(getattr(cfg, "root", Path.cwd())) / root
    from . import exchange as ex
    if not ex.is_exchange(root):
        return {**empty, "redaction_leaks": leaks}
    pending = ex.pending_requests(root)
    claimed = ex.claimed_requests(root)
    failed = ex.failed_requests(root)
    oldest = min((p.get("created_at") for p in pending), default=None)
    stale = []
    for c in claimed:
        age = _hours_between(c.get("claimed_at"), now)
        if age is not None and age > stale_hours:
            stale.append({"file": c["_file"], "claimed_by": c.get("claimed_by"),
                          "claimed_at": c.get("claimed_at"), "age_hours": age})
    by_cat: dict[str, int] = {}
    for f in failed:
        cat = f.get("error_category") or "unknown"
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {"configured": True, "pending_count": len(pending),
            "oldest_pending_hours": _hours_between(oldest, now) if oldest else None,
            "claimed_count": len(claimed), "stale_claimed": stale,
            "failed_count": len(failed), "failed_by_category": by_cat,
            "results_waiting": len(ex.result_files(root)),
            "redaction_leaks": leaks}


def _queue_state(wiki_repo: Path, now: str, stale_hours: float) -> dict:
    """Analyst queue telemetry (ADR-0001): oldest pending age, claimed
    entries older than `stale_hours` (a crashed analyst — re-claimable by
    hand-moving the file back to `pending/`), and the failed/ count by error
    category. Reported as telemetry about the queue itself, never as
    database state, and absent (all-empty) whenever `queue/` does not exist
    — most installs never enable `analyst.enabled` and must see no change."""
    if not (wiki_repo / "queue").is_dir():
        return {"pending_count": 0, "oldest_pending_hours": None,
                "claimed_count": 0, "stale_claimed": [],
                "failed_count": 0, "failed_by_category": {}}
    from . import queue as queue_mod
    pending = queue_mod.pending_requests(wiki_repo)
    claimed = queue_mod.claimed_requests(wiki_repo)
    failed = queue_mod.failed_requests(wiki_repo)
    oldest = min((p.get("created_at") for p in pending), default=None)
    stale_claimed = []
    for c in claimed:
        age = _hours_between(c.get("claimed_at"), now)
        if age is not None and age > stale_hours:
            stale_claimed.append({"file": c["_file"], "claimed_by": c.get("claimed_by"),
                                  "claimed_at": c.get("claimed_at"), "age_hours": age})
    by_category: dict[str, int] = {}
    for f in failed:
        cat = f.get("error_category") or "unknown"
        by_category[cat] = by_category.get(cat, 0) + 1
    return {
        "pending_count": len(pending),
        "oldest_pending_hours": _hours_between(oldest, now) if oldest else None,
        "claimed_count": len(claimed),
        "stale_claimed": stale_claimed,
        "failed_count": len(failed),
        "failed_by_category": by_category,
    }


def _quiet_state(wiki_repo: Path, now: str) -> dict:
    """Open incidents whose linked error codes have stopped appearing in the
    digests: how many there are, and the three that have been quiet longest.

    Quiet is derived here and stored nowhere. No page carries it, no
    frontmatter key names it, and nothing transitions on it. An operator who
    moves `readmodel.QUIET_DAYS` changes what today's report says and rewrites
    no history. It rides beside the analyst queue in the rendered report and
    stays out of `h["queue"]`: that block is analyst-request-queue telemetry
    under ADR-0001, a different domain from the incident attention queue, and
    a reader who merged the two would be counting requests and cases in one
    number.

    The shape is `{"count": int, "oldest": [{"slug", "last_seen"}, ...]}`,
    oldest day first and at most three. A count plus a sample rather than the
    whole list, because this is a nudge printed under a health report rather
    than a work queue: the portal already lists every incident, and a report
    that named 25 of them would bury the sections around it.

    All-empty whenever the wiki is absent or holds no `incidents/` or
    `errors/` directory. Most installs and every test fixture have no error
    pages at all, and the join this reads lives only in those pages, so
    "nothing to say" is the honest answer there rather than a crash.

    An incident that links no code is not counted. `readmodel.last_seen`
    answers None for it, and "nobody told us what this is about" is not "it
    has been quiet"; saying the second would be reporting a fact the wiki does
    not hold.

    The error pages are read once into one list of rows, never once per
    incident: the live wiki has 25 open incidents over 60-odd error pages, and
    the per-incident read would be a few hundred opens for one line of
    output."""
    empty: dict = {"count": 0, "oldest": []}
    if not wiki_repo.is_dir():
        return empty
    errors_dir = wiki_repo / "errors"
    if not (wiki_repo / "incidents").is_dir() or not errors_dir.is_dir():
        return empty
    today = _parse(now)
    if today is None:
        return empty
    from . import readmodel
    from .incidents import Status, load_incidents

    occurrences: list = []
    for path in sorted(errors_dir.glob("*.md")):
        try:
            text = path.read_bytes().decode(errors="surrogateescape")
        except OSError:
            continue
        occurrences.extend(readmodel.parse_occurrences(path.stem, text))

    quiet = []
    for inc in load_incidents(wiki_repo):
        if inc.status is not Status.OPEN:
            continue
        day = readmodel.last_seen(inc, occurrences)
        if day is None:
            continue
        quiet_for = (today.date() - dt.date.fromisoformat(day)).days
        if quiet_for >= readmodel.QUIET_DAYS:
            quiet.append({"slug": inc.slug, "last_seen": day})
    quiet.sort(key=lambda q: (q["last_seen"], q["slug"]))
    return {"count": len(quiet), "oldest": quiet[:3]}


def _digest_presence(cfg, wiki: dict, rel: str) -> str:
    """`present` / `missing` / `wiki_missing`. The digests live inside the wiki
    repo, so when that repo is absent the file's absence says nothing."""
    if not wiki["present"]:
        return "wiki_missing"
    return "present" if (Path(cfg.wiki_repo) / rel).exists() else "missing"


def deliberate_skip(entry: Mapping) -> bool:
    """Whether a ledger entry that was never ingested is a decision rather
    than a debt.

    Most of the ledger is exactly this case: 211 of 451 live entries carry a
    `last_decision` and no `status` at all, because `cmd_run` merges the
    decision in and moves on when the trigger says skip (`cli.py:604`). Such
    an entry has been answered — nothing is owed — and counting it as backlog
    would report the pipeline's normal quiet as a permanent debt.

    An `ingested` or `failed` status settles the entry on its own terms and is
    never a deliberate skip; `failed` is retry work (`plan_retries`), not
    backlog. Otherwise the answer is the stored decision's outcome.

    One owner on purpose: `assess` and `events.backlog` must not be able to
    disagree about the same entry."""
    if entry.get("status") in ("ingested", "failed"):
        return False
    last = entry.get("last_decision")
    return isinstance(last, Mapping) and last.get("outcome") == "skip"


def assess(cfg, *, es=None, now: str | None = None, get_json=None) -> dict:
    """One dict describing pipeline health. Read-only throughout: it probes ES
    and the local dependencies, reads `.state/` and runs read-only git
    commands in the wiki, and writes nothing. `get_json` overrides the
    dependency probe's HTTP getter (tests hand it a fake)."""
    from .state import StateStore
    now = now or _now()
    hcfg = getattr(cfg, "health", {}) or {}
    stale_hours = float(hcfg.get("stale_hours", DEFAULT_STALE_HOURS))
    stage_hours = {**DEFAULT_STAGE_STALE_HOURS,
                   **(hcfg.get("stage_stale_hours") or {})}
    if es is None:
        from .es import ES
        es = ES(cfg.es_url, cfg.es_user, cfg.es_password)

    state = StateStore(Path(cfg.state_dir))
    events = read_events(cfg.state_dir)
    ledger = state.get_ledger()
    watermarks = state.get_watermarks()
    wiki = _wiki_state(cfg)
    sources = collection_states(cfg, es, now, stale_hours)

    marks = [{"db": db, "watermark": wm, "age_hours": _hours_between(wm, now),
              "stale": (_hours_between(wm, now) or 0) > stale_hours}
             for db, wm in sorted(watermarks.items())]

    failures, backlog = [], []
    for rel, entry in sorted(ledger.items()):
        db = Path(rel).parent.name
        if entry.get("status") == "failed":
            cat = category_of_entry(entry)
            failures.append({
                "digest": rel, "db": db, "at": entry.get("at"),
                "category": cat, "retryable": cat in RETRYABLE,
                "problems": [p[:200] for p in entry.get("problems", [])][:3],
                "digest_present": _digest_presence(cfg, wiki, rel)})
        elif entry.get("status") != "ingested":
            if deliberate_skip(entry):
                continue
            outcome = (entry.get("last_decision") or {}).get("outcome")
            backlog.append({"digest": rel, "db": db, "decision": outcome,
                            "digest_present": _digest_presence(cfg, wiki, rel)})

    problems, blockers = [], []
    if not wiki["present"]:
        blockers.append(f"wiki repo missing: {wiki['path']} "
                        f"(agent stages and digest files are unavailable)")
    elif not wiki["git"]:
        blockers.append(f"wiki repo is not a git repository: {wiki['path']}")
    if wiki["stray"]:
        shown = ", ".join(wiki["stray"][:5]) + ("…" if len(wiki["stray"]) > 5 else "")
        blockers.append(f"wiki working tree dirty outside digests/ ({shown}); "
                        f"agent stages refuse to run")
    for p in sources:
        if p["state"] == "collection_failure":
            problems.append(f"collection failure: no {p['source']} events "
                            f"anywhere since {p['latest_event']}")
        elif p["state"] == "source_silent":
            problems.append(f"source silent: no {p['source']} events since "
                            f"{p['latest_event']} while other sources are live")
    for m in marks:
        if m["stale"]:
            problems.append(f"stale watermark: {m['db']} at {m['watermark']} "
                            f"({m['age_hours']}h > {stale_hours}h)")
    for f in failures:
        problems.append(f"failed ingest: {f['digest']} ({f['category']}, "
                        f"{'retryable' if f['retryable'] else 'needs an operator'})")
    for b in backlog:
        problems.append(f"digest backlog: {b['digest']} never ingested")

    analyst_stale_hours = float((getattr(cfg, "analyst", {}) or {})
                                .get("stale_hours", DEFAULT_ANALYST_STALE_HOURS))
    queue = _queue_state(Path(cfg.wiki_repo), now, analyst_stale_hours)
    quiet = _quiet_state(Path(cfg.wiki_repo), now)
    # pending age is informational only (an idle analyst is an accepted
    # degraded mode, ADR-0001) — only stale claims and outright failures
    # carry the severity of a retryable failed ingest
    for c in queue["stale_claimed"]:
        problems.append(f"analyst queue: claimed {c['file']} by "
                        f"{c['claimed_by']} is stale ({c['age_hours']}h > "
                        f"{analyst_stale_hours}h; a crashed analyst — "
                        f"re-claimable by hand-moving it back to pending/)")
    if queue["failed_count"]:
        cats = ", ".join(f"{k}={v}" for k, v in
                         sorted(queue["failed_by_category"].items()))
        problems.append(f"analyst queue: {queue['failed_count']} failed "
                        f"request(s) ({cats})")

    exchange = _exchange_state(cfg, now, analyst_stale_hours)
    for c in exchange["stale_claimed"]:
        problems.append(f"research exchange: claimed {c['file']} by "
                        f"{c['claimed_by']} is stale ({c['age_hours']}h > "
                        f"{analyst_stale_hours}h; a crashed researcher — "
                        f"re-claimable by hand-moving it back to requests/pending/)")
    if exchange["failed_count"]:
        cats = ", ".join(f"{k}={v}" for k, v in
                         sorted(exchange["failed_by_category"].items()))
        problems.append(f"research exchange: {exchange['failed_count']} failed "
                        f"request(s)/result(s) ({cats})")
    if exchange["redaction_leaks"]:
        problems.append(f"redaction-leak: {exchange['redaction_leaks']} research "
                        f"request(s) refused (see .state/redaction_leaks.jsonl; "
                        f"extend redact.terms or the vocabulary)")
    last_success = _last_successes(events, ledger)
    stale_stages = _stage_staleness(last_success, now, stage_hours)
    for s in stale_stages:
        problems.append(f"stale stage: {s['task']} last succeeded {s['at']} "
                        f"({s['age_h']}h > {s['threshold_h']:g}h)")
    deps = dependencies(cfg, wiki, get_json=get_json)
    for d in deps["problems"]:
        problems.append(f"dependency: {d['check']}: {d['message']}")

    es_unknown = all(p["state"] == "unknown" for p in sources)
    if es_unknown:
        problems.append("elasticsearch unreachable: collection state unknown "
                        "(no conclusion about databases may be drawn from it)")
    no_local_state = not watermarks and not ledger and not events
    exit_code = 2 if (es_unknown and no_local_state) else \
        (1 if (problems or blockers) else 0)
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "generated_at": now,
        "stale_hours": stale_hours,
        "healthy": exit_code == 0,
        "exit_code": exit_code,
        "wiki": wiki,
        "last_success": last_success,
        "stale_stages": stale_stages,
        "dependencies": deps,
        "watermarks": marks,
        "failures": failures,
        "backlog": backlog,
        "sources": sources,
        "recovery_evidence": recovery_evidence(cfg, es, sources, now, stale_hours),
        "blockers": blockers,
        "problems": problems,
        "events_recorded": len(events),
        "queue": queue,
        "quiet": quiet,
        "exchange": exchange,
    }


def health_lines(h: dict) -> list[str]:
    """2–4 factual lines for the report prompt's "Collection health" block."""
    srcs = " ".join(f"{p['source']}={p['state']}"
                    + (f"({p['age_hours']}h)" if p["age_hours"] is not None else "")
                    for p in h["sources"])
    lines = [f"- sources: {srcs or '(none configured)'}",
             f"- recovery evidence: {h['recovery_evidence']['kind']} — "
             f"{h['recovery_evidence']['detail'][:160]}"]
    stale = [m["db"] for m in h["watermarks"] if m["stale"]]
    if stale:
        lines.append(f"- stale watermarks: {', '.join(stale)}")
    if h["failures"] or h["backlog"] or h["blockers"]:
        lines.append(f"- failed ingests: {len(h['failures'])}; "
                     f"backlog: {len(h['backlog'])}; "
                     f"blockers: {'; '.join(h['blockers'])[:120] or 'none'}")
    return lines[:4]


def _dependency_lines(deps: dict, hint) -> list[str]:
    """The dependency block: one state line per check, then every problem with
    its recovery command."""
    out = []
    ms = deps.get("model_server") or {}
    if ms.get("checked"):
        state = "up" if ms.get("reachable") else "UNREACHABLE"
        # Without /api/v0 there is no load state, only a catalogue; saying
        # "loaded" of all six would be a claim the server never made. What
        # /v1 does carry — the served context — is the half worth printing.
        known = ms.get("state_known")
        what = "loaded" if known else "served ctx"
        shown = [m for m in ms.get("loaded") or []
                 if known or m.get("context_length") is not None]
        loaded = (f"{what}: " + ", ".join(f"{m['id']}@{m['context_length']}"
                                          for m in shown)) if shown else \
            ("none loaded" if known else f"{what} unknown")
        who = f"{ms['provider']} " if ms.get("provider") else ""
        out.append(f"  model svr   {state} {who}{ms.get('url', '')} [{loaded}]")
        out += [f"    note: {n}" for n in ms.get("notes") or []]
    elif ms:
        out.append(f"  model svr   not checked ({ms.get('reason', '-')})")
    ad = deps.get("adapters") or {}
    if ad.get("required"):
        out.append(f"  adapters    {' '.join(n + ('=ok' if ad['found'].get(n) else '=MISSING') for n in ad['required'])}")
    wr = deps.get("wiki_remote") or {}
    if wr:
        push = "push=on" if wr.get("push") else "push=off"
        up = wr.get("upstream") or "-"
        out.append(f"  wiki remote {push} upstream={up} "
                   f"unpushed={wr.get('unpushed', '-')} "
                   f"user.email={wr.get('user_email') or '(unset)'}")
    dk = deps.get("disk") or {}
    if dk.get("free_gib") is not None:
        out.append(f"  disk        {dk['free_gib']} GiB free under {dk['path']}")
    pt = deps.get("portal") or {}
    if pt.get("checked"):
        state = "up" if pt.get("reachable") else "UNREACHABLE"
        who = (f" [wiki {str(pt['revision'])[:7]} as {pt['actor']}]"
               if pt.get("revision") and pt.get("actor") else "")
        out.append(f"  portal      {state} {pt.get('url', '')}{who}")
    for p in deps.get("problems") or []:
        out.append(f"  PROBLEM {p['check']}: {p['message']}")
        out.append(hint("dependency_failure", detail=p["detail"]))
    return out


def format_health(h: dict) -> str:
    """Compact human report: one line per fact, sections in triage order, and
    the exact recovery command under every failure, blocker and dependency
    problem — the alert payload has carried that hint since WS5a, while the
    command an operator actually runs printed the diagnosis and left them to
    guess the cure."""
    from .alerts import recovery_hint  # alerts imports us; keep this lazy
    from .readmodel import QUIET_DAYS

    def hint(category: str, db: str = "-", source: str = "-",
             detail: str | None = None) -> str:
        return f"    -> {recovery_hint(category, db, source, detail=detail)}"

    out = [f"pipeline health {h['generated_at']} "
           f"[{'HEALTHY' if h['healthy'] else 'UNHEALTHY'}] "
           f"stale_hours={h['stale_hours']:g}", ""]

    out.append("collection")
    for p in h["sources"]:
        age = f"{p['age_hours']}h ago" if p["age_hours"] is not None else "unknown"
        detail = f" ({p['detail']})" if p.get("detail") else ""
        out.append(f"  {p['source']:<11} {p['state']:<19} "
                   f"last event {p['latest_event'] or '(none)'} [{age}]{detail}")
    out.append(f"  recovery evidence: {h['recovery_evidence']['kind']} — "
               f"{h['recovery_evidence']['detail']}")

    out += ["", "last success"]
    stale_by_task = {s["task"]: s for s in h.get("stale_stages") or []}
    for kind, v in h["last_success"].items():
        out.append(f"  {kind:<11} " + (f"{v['at']} (from {v['from']}) "
                                       f"{v['detail']}".rstrip() if v else "never"))
    for s in stale_by_task.values():
        out.append(f"  STALE STAGE {s['task']}: last success {s['at']} "
                   f"({s['age_h']}h > {s['threshold_h']:g}h)")
        out.append(hint("stage_stale", detail=s["task"]))

    out += ["", "dependencies"]
    out += _dependency_lines(h.get("dependencies") or {}, hint) or ["  (none checked)"]

    out += ["", "watermarks"]
    out += [f"  {m['db']:<11} {m['watermark']} "
            f"[{m['age_hours']}h{' STALE' if m['stale'] else ''}]"
            for m in h["watermarks"]] or ["  (none recorded)"]

    out += ["", "failed ingests"]
    if h["failures"]:
        for f in h["failures"]:
            out.append(f"  {f['digest']} {f['category']} "
                       f"[{'retryable' if f['retryable'] else 'operator'}] "
                       f"digest={f['digest_present']} at {f['at']}")
            out.append(hint(f["category"], f["db"]))
    else:
        out.append("  (none)")

    out += ["", "digest backlog"]
    out += [f"  {b['digest']} decision={b['decision']} digest={b['digest_present']}"
            for b in h["backlog"]] or ["  (none)"]

    out += ["", "analyst queue"]
    q = h["queue"]
    out.append(f"  pending: {q['pending_count']} (oldest "
               f"{q['oldest_pending_hours']}h)" if q["oldest_pending_hours"]
               is not None else f"  pending: {q['pending_count']}")
    quiet = h.get("quiet") or {"count": 0, "oldest": []}
    out.append(f"  quiet: {quiet['count']} open incident(s) with no linked "
               f"code seen for {QUIET_DAYS}+ days" if quiet["count"]
               else "  quiet: 0")
    out += [f"  {i['slug']} (last seen {i['last_seen']})"
            for i in quiet["oldest"]]
    out += [f"  stale claim: {c['file']} by {c['claimed_by']} [{c['age_hours']}h]"
            for c in q["stale_claimed"]]
    if q["failed_count"]:
        cats = ", ".join(f"{k}={v}" for k, v in sorted(q["failed_by_category"].items()))
        out.append(f"  failed: {q['failed_count']} ({cats})")

    x = h.get("exchange") or {}
    if x.get("configured") or x.get("redaction_leaks"):
        out += ["", "research exchange"]
        out.append(f"  pending: {x['pending_count']} (oldest "
                   f"{x['oldest_pending_hours']}h)" if x.get("oldest_pending_hours")
                   is not None else f"  pending: {x.get('pending_count', 0)}")
        out.append(f"  claimed: {x.get('claimed_count', 0)}, results waiting: "
                   f"{x.get('results_waiting', 0)}, redaction leaks: {x.get('redaction_leaks', 0)}")
        out += [f"  stale claim: {c['file']} by {c['claimed_by']} [{c['age_hours']}h]"
                for c in x.get("stale_claimed", [])]
        if x.get("failed_count"):
            cats = ", ".join(f"{k}={v}" for k, v in sorted(x["failed_by_category"].items()))
            out.append(f"  failed: {x['failed_count']} ({cats})")

    out += ["", "blockers"]
    if h["blockers"]:
        for b in h["blockers"]:
            out.append(f"  {b}")
            # the blocker strings are built in `assess` right above, from the
            # same wiki dict — "dirty" is the only one of the three that is
            # not about the repo being absent
            out.append(hint("dirty_tree" if "dirty" in b else "wiki_missing"))
    else:
        out.append("  (none)")
    if h["problems"]:
        out += ["", f"{len(h['problems'])} problem(s); "
                    f"{h['events_recorded']} run-health events recorded"]
    return "\n".join(out)


def plan_retries(cfg, state, db: str | None = None) -> tuple[list[dict], list[dict]]:
    """(retryable, skipped) for the failed ledger entries. An entry is
    retryable when its digest JSON still exists, its content hash still matches
    what failed, and its failure category is one a rerun can fix. Re-ingestion
    itself goes through `Orchestrator.ingest`, which dedupes, validates, rolls
    back, and leaves unrelated changes alone — nothing here writes."""
    from .compactor import Compactor
    plan, skipped = [], []
    wiki_present = Path(cfg.wiki_repo).is_dir()
    for rel, entry in sorted(state.get_ledger().items()):
        if entry.get("status") != "failed":
            continue
        edb = Path(rel).parent.name
        if db and edb != db:
            continue
        cat = category_of_entry(entry)
        item = {"digest": rel, "db": edb, "at": entry.get("at"),
                "category": cat, "content_hash": entry.get("content_hash")}
        if cat not in RETRYABLE:
            skipped.append({**item, "reason": f"not retryable ({cat})"})
            continue
        if not wiki_present:
            skipped.append({**item, "reason": "wiki_missing"})
            continue
        path = Path(cfg.wiki_repo) / rel
        if not path.exists():
            skipped.append({**item, "reason": "digest file missing"})
            continue
        try:
            actual = Compactor.content_hash(json.loads(path.read_text()))
        except Exception as e:  # noqa: BLE001 — unreadable digest is a skip
            skipped.append({**item, "reason": f"unreadable digest: {e}"})
            continue
        if item["content_hash"] and actual != item["content_hash"]:
            skipped.append({**item, "reason":
                            f"content hash changed since the failure "
                            f"({item['content_hash']} -> {actual}); recompact"})
            continue
        plan.append({**item, "content_hash": actual})
    return plan, skipped
