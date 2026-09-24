"""Human feedback as Langfuse scores: queue on the act, send from cron.

`validation_ok` says a proposal passed the deterministic checks. Whether a DBA
agreed with the page is a different fact, and the only quality signal that
makes "acceptance by model" or "by prompt version" mean anything. This module
carries it from the act to the trace (docs/langfuse.md, "Human feedback").

Two halves, split by who may hold a Langfuse key:

* **queue** — `queue_action` (every committed `dbwiki incident` / portal act,
  from `incident_action.publish`) and `queue_review_decision` (the review
  inbox's acknowledge/suppress) append score records to
  `.state/pending_scores.jsonl`. No network and no key: the portal never gets
  Langfuse credentials. Best-effort like every recorder here: a failure warns
  once (`observability.warn_once`) and never blocks or fails the act.
* **send** — `send` (`dbwiki scores send`, cron) posts the queue through the
  Langfuse client's `create_score` and drops what it handed over. A no-op
  without `langfuse.enabled` or without keys.

Which trace a score lands on. One `Run-ID` covers every stage of a tick, so
the run alone does not name a trace. The rule: find the newest commit on the
page, at the base the DBA acted on, that carries a `Run-ID:` trailer (a human
commit has none, so a second act still scores the tick behind it); its
subject names the stage the way `Orchestrator._propose` spells it
(`ingest: <db> <day> — ...`, `report`, `lint`, ...). The `agent_runs.jsonl`
line with that run id and task — narrowed by the db the subject names, then to
the stage that validated — is the stage that wrote the page, and its
`event_id` seeds the trace id (`observability.trace_id`). When no single line
answers (aged out of the capped ledger, or two candidates), the score goes on
the Langfuse *session* of the run instead. A page no tick ever wrote queues
nothing. A review decision scores the review synthesis stage (`task: review`,
`review_id` of the newest review holding the finding); a review with no
synthesis stage has no model output to score and queues nothing.

Idempotency. Every record carries `key`, a stable hash that is also the
Langfuse `score_id`, which Langfuse upserts on:

* `human_action:<kind>` is keyed by the act (the incident commit, or the
  review decision and its instant), so each act counts once;
* `human_accepted` is keyed by (trace or session, page or finding), so a later
  verdict on the same thing replaces the earlier rather than adding to it.

A crash between posting and dropping re-posts the same ids on the next send,
which Langfuse treats as an update, never a second score.
"""

import datetime as dt
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import health, observability
from .observability import warn_once

PENDING_LOG = "pending_scores.jsonl"
#: drop-oldest cap, the `health._append_capped` policy: without Langfuse the
#: queue is never drained, and it must not grow without bound
MAX_PENDING = 5000
DEFAULT_BACKLOG_HOURS = 24
#: how far back a page's history is walked for the machine write it carries
HISTORY_LIMIT = 50

ACCEPTED = "human_accepted"
ACTION = "human_action:"

#: incident verb -> `human_accepted`. Every act that keeps working the page
#: accepts it as a real incident; `merge` folds it away as a duplicate the
#: tick should not have opened.
ACCEPTS = {"record-action": 1, "monitor": 1, "extend": 1, "resolve": 1,
           "reopen": 1, "merge": 0}
#: review decision -> `human_accepted`: a finding looked at, or one the
#: operator wants hidden.
REVIEW_ACCEPTS = {"acknowledge": 1, "suppress": 0}

_TASK_RE = re.compile(r"\A\s*([a-z][a-z_-]*)")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _warn(key: str, msg: str) -> None:
    warn_once(f"scores:{key}", f"human feedback score: {msg}")


@dataclass(frozen=True)
class Target:
    """What a score lands on: the trace of the stage `event_id` names, or,
    without one, the Langfuse session of `run_id`."""

    run_id: str
    stage: str | None
    event_id: str | None

    @property
    def trace_id(self) -> str | None:
        return observability.trace_id(self.event_id) if self.event_id else None

    @property
    def session_id(self) -> str | None:
        return None if self.event_id else (self.run_id or None)

    @property
    def ref(self) -> str:
        return (f"trace:{self.trace_id}" if self.event_id
                else f"session:{self.run_id}")


# -- attribution ---------------------------------------------------------------

def machine_write(wiki: Path, path: str,
                  revision: str) -> tuple[str, str] | None:
    """`(run_id, subject)` of the newest commit touching `path` at
    `revision` that carries a `Run-ID:` trailer, or None."""
    from . import gitutil
    fmt = "%x1e%s%x00%(trailers:key=Run-ID,valueonly)"
    out = gitutil.git(wiki, "log", "-n", str(HISTORY_LIMIT),
                      f"--format={fmt}", revision, "--", path)
    for record in out.split("\x1e")[1:]:
        subject, _, run_id = record.partition("\x00")
        run_id = run_id.strip()
        if run_id:
            return run_id.splitlines()[0].strip(), subject
    return None


def stage_of(state_dir: Path, run_id: str, subject: str) -> Target:
    """The stage of `run_id` that the commit `subject` records (module
    docstring). Falls back to a session target when the ledger cannot name
    exactly one line."""
    match = _TASK_RE.match(subject)
    task = match.group(1) if match else None
    lines = [e for e in health.read_agent_runs(state_dir)
             if e.get("run_id") == run_id and e.get("task") == task
             and e.get("event_id")]
    words = set(subject.replace(":", " ").split())
    if len(lines) > 1:
        lines = [e for e in lines if e.get("db") in words] or lines
    if len(lines) > 1:
        lines = [e for e in lines if e.get("validation_ok") is True] or lines
    event_id = str(lines[0]["event_id"]) if len(lines) == 1 else None
    return Target(run_id=run_id, stage=task, event_id=event_id)


def attribute(wiki: Path, state_dir: Path, path: str,
              revision: str) -> Target | None:
    found = machine_write(wiki, path, revision)
    if found is None:
        return None
    return stage_of(state_dir, *found)


def review_target(state_dir: Path, fingerprint: str) -> Target | None:
    """The synthesis stage of the newest review holding `fingerprint`."""
    from . import review  # review imports us (queue_review_decision)
    for review_id in review.list_reviews(state_dir):
        doc = review.load_review(state_dir, review_id) or {}
        if any(f.get("fingerprint") == fingerprint
               for f in doc.get("findings") or ()):
            break
    else:
        return None
    lines = [e for e in health.read_agent_runs(state_dir)
             if e.get("task") == "review" and e.get("review_id") == review_id
             and e.get("event_id")]
    if not lines:
        return None
    return Target(run_id=str(lines[-1].get("run_id") or ""), stage="review",
                  event_id=str(lines[-1]["event_id"]))


# -- records -------------------------------------------------------------------

def _record(target: Target, *, name: str, value: int, data_type: str,
            key: str, source: str, subject: str, kind: str, at: str,
            **extra) -> dict:
    return {"key": key, "name": name, "value": value, "data_type": data_type,
            "trace_id": target.trace_id, "session_id": target.session_id,
            "run_id": target.run_id or None, "stage": target.stage,
            "event_id": target.event_id, "source": source,
            "subject": subject, "kind": kind, "at": at,
            "queued_at": _now(), **extra}


def incident_records(target: Target, *, slug: str, verb: str, at: str,
                     surface: str, commit: str) -> list[dict]:
    """The score records one committed incident act produces."""
    common = dict(source="incident", subject=slug, kind=verb, at=at,
                  surface=surface, commit=commit)
    out = []
    if verb in ACCEPTS:
        out.append(_record(target, name=ACCEPTED, value=ACCEPTS[verb],
                           data_type="BOOLEAN",
                           key=_key("incident", target.ref, slug, ACCEPTED),
                           **common))
    out.append(_record(target, name=f"{ACTION}{verb}", value=1,
                       data_type="NUMERIC",
                       key=_key("incident", commit, f"{ACTION}{verb}"),
                       **common))
    return out


def review_records(target: Target, *, fingerprint: str, decision: str,
                   at: str) -> list[dict]:
    """The score records one review-inbox decision produces."""
    common = dict(source="review", subject=fingerprint, kind=decision, at=at,
                  surface="portal")
    return [
        _record(target, name=ACCEPTED, value=REVIEW_ACCEPTS[decision],
                data_type="BOOLEAN",
                key=_key("review", target.ref, fingerprint, ACCEPTED),
                **common),
        _record(target, name=f"{ACTION}{decision}", value=1,
                data_type="NUMERIC",
                key=_key("review", fingerprint, decision, at), **common)]


def _append(state_dir: Path, record: dict) -> None:
    health._append_capped(Path(state_dir) / PENDING_LOG, record, MAX_PENDING)


def queue_action(wiki: Path, state_dir: Path, action, commit: str,
                 surface: str) -> None:
    """Queue the scores for one committed incident act. Never raises."""
    try:
        target = attribute(Path(wiki), Path(state_dir), action.path,
                           action.base)
        if target is None:
            return
        for rec in incident_records(target, slug=action.slug,
                                    verb=action.verb, at=action.at,
                                    surface=surface, commit=commit):
            _append(Path(state_dir), rec)
    except Exception as e:  # noqa: BLE001 — feedback never fails an act
        _warn("queue", f"queueing failed: {type(e).__name__}: {e}")


def queue_review_decision(state_dir: Path, fingerprint: str, decision: str,
                          at: str) -> None:
    """Queue the scores for one review-inbox decision. Never raises."""
    try:
        target = review_target(Path(state_dir), fingerprint)
        if target is None:
            return
        for rec in review_records(target, fingerprint=fingerprint,
                                  decision=decision, at=at):
            _append(Path(state_dir), rec)
    except Exception as e:  # noqa: BLE001 — feedback never fails an act
        _warn("queue", f"queueing failed: {type(e).__name__}: {e}")


# -- the queue on disk ---------------------------------------------------------

def _lines(path: Path) -> list[tuple[str, dict]]:
    """(raw line, record) pairs, oldest first; unparseable lines skipped."""
    if not path.exists():
        return []
    out = []
    for ln in path.read_text().splitlines():
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("key"):
            out.append((ln, rec))
    return out


def read_pending(state_dir: Path) -> list[dict]:
    return [rec for _, rec in _lines(Path(state_dir) / PENDING_LOG)]


def _drop(path: Path, sent: set[str]) -> None:
    """Remove exactly the lines that were sent, under the append lock, so a
    record queued while the send was in flight survives the rewrite."""
    from .state import atomic_write_text
    with health._log_lock(path):
        keep = [ln for ln in path.read_text().splitlines()
                if ln.strip() and ln not in sent]
        atomic_write_text(path, "".join(f"{ln}\n" for ln in keep))
        health._line_counts.pop(str(path), None)


# -- the sender ----------------------------------------------------------------

@dataclass(frozen=True)
class SendOutcome:
    sent: int
    failed: int
    pending: int
    skipped: str = ""


def _has_keys(lf_cfg: dict) -> bool:
    return bool((lf_cfg.get("public_key")
                 or os.environ.get("LANGFUSE_PUBLIC_KEY"))
                and (lf_cfg.get("secret_key")
                     or os.environ.get("LANGFUSE_SECRET_KEY")))


def _stamp(at) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return None


def send(cfg, *, client=None) -> SendOutcome:
    """Post every pending score once. Without `langfuse.enabled` or without
    keys it touches nothing (`skipped` says which). A `create_score` that
    raises leaves its record queued; a failed `flush` leaves all of them, and
    the next send re-posts the same `score_id`s (an upsert). Only what was
    handed over is dropped. Repeated keys are sent once, latest value."""
    path = Path(cfg.state_dir) / PENDING_LOG
    lf_cfg = cfg.langfuse
    rows = _lines(path)
    latest: dict[str, dict] = {}
    raw: dict[str, set[str]] = {}
    for ln, rec in rows:
        latest.pop(rec["key"], None)          # re-insert: file order of last
        latest[rec["key"]] = rec
        raw.setdefault(rec["key"], set()).add(ln)
    if not lf_cfg.get("enabled"):
        return SendOutcome(0, 0, len(latest), "disabled")
    if client is None:
        if not _has_keys(lf_cfg):
            return SendOutcome(0, 0, len(latest), "no_keys")
        client = observability._get_client(lf_cfg)
        if client is None:
            return SendOutcome(0, 0, len(latest), "no_client")
    if not latest:
        return SendOutcome(0, 0, 0)

    posted: list[str] = []
    for key, rec in latest.items():
        try:
            client.create_score(
                name=rec["name"], value=float(rec["value"]),
                trace_id=rec.get("trace_id"),
                session_id=rec.get("session_id"),
                score_id=key, data_type=rec.get("data_type"),
                comment=f"{rec.get('kind')} on {rec.get('subject')}",
                metadata={k: rec.get(k) for k in
                          ("source", "subject", "kind", "stage", "run_id",
                           "event_id", "surface", "commit")
                          if rec.get(k) is not None},
                timestamp=_stamp(rec.get("at")),
                environment=lf_cfg.get("environment"))
        except Exception as e:  # noqa: BLE001 — one bad record, not the batch
            _warn("send", f"create_score failed: {type(e).__name__}: {e}")
            continue
        posted.append(key)
    try:
        client.flush()
    except Exception as e:  # noqa: BLE001 — resend is an idempotent upsert
        _warn("flush", f"flush failed: {type(e).__name__}: {e}")
        return SendOutcome(0, len(latest), len(latest))
    if posted:
        _drop(path, set().union(*(raw[k] for k in posted)))
    return SendOutcome(len(posted), len(latest) - len(posted),
                       len(latest) - len(posted))


# -- health --------------------------------------------------------------------

def backlog(cfg, now: str) -> dict:
    """The unsent queue, for `dbwiki health`: how many, how many older than
    `langfuse.score_backlog_hours` (default 24), and the oldest. `stale` is
    only counted when Langfuse is enabled — without it nothing drains the
    queue, and that is not a problem."""
    lf_cfg = cfg.langfuse
    enabled = bool(lf_cfg.get("enabled"))
    hours = float(lf_cfg.get("score_backlog_hours", DEFAULT_BACKLOG_HOURS))
    recs = read_pending(Path(cfg.state_dir))
    stamps = sorted(str(r.get("queued_at") or "") for r in recs)
    stale = sum(1 for s in stamps
                if (health._hours_between(s, now) or 0) > hours) \
        if enabled else 0
    return {"enabled": enabled, "pending": len(recs), "stale": stale,
            "oldest": stamps[0] if stamps else None, "threshold_h": hours}
