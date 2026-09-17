"""The analyst queue (ADR-0001): a directory in the wiki repo, `queue/`,
that is the *only* channel between the on-prem node and the analyst node.
Plain functions over a wiki-repo `Path` — no Orchestrator, no config object —
because both sides (the on-prem `report()` enqueue call and the analyst's
`dbwiki analyst` claim loop) need it, and neither owns the other.

```
queue/
├── pending/<kind>-<day><suffix>-<run_id>.json    # awaiting an analyst
├── claimed/…                                     # an analyst is working on it
├── failed/…                                      # gave up after MAX_ATTEMPTS
└── results/<run_id>-<event_id>.json              # telemetry riding back
```

Git is the lock (ADR-0001, "Claim protocol"). Two races are handled, and
handled differently, because they resolve differently:

* **claim() vs claim()** — two analysts (or two `dbwiki analyst` invocations)
  racing for the same request. The loser's commit and the winner's commit
  both write a *different* `claimed_by`/`claimed_at` into the *same* target
  path (`claimed/<file>`) — a real content conflict a generic `git pull
  --rebase` cannot auto-resolve. The loser instead discards its local claim
  commit, resyncs to the pushed-ahead remote, and rechecks: gone from
  `pending/` means it lost and skips; still there (a push that failed for an
  unrelated reason) means it retries against the resynced tree.
* **complete()/fail_request() vs an on-prem commit** — different paths
  almost always (`reports/`, `queue/` vs `digests/`, `errors/`, `incidents/`,
  `index.md`, `log.md`), so a plain `git pull --rebase` merges cleanly. The
  one path both sides can touch, `reports/<day><suffix>.md`, is same-day
  supersede by design (ADR-0001, Consequences: "a same-path race... resolves
  as last-push-wins"), so a same-path conflict there is resolved by keeping
  the side that is rebasing (`-X theirs` — during a rebase the replayed
  local commits are git's "theirs") rather than raising it as an error.

Every write is additive from the on-prem side's point of view: nothing here
ever touches `digests/`, `errors/`, `incidents/`, `index.md`, `reports/`
content on the *claim* path, or `.state/` — the wiki content itself (the
report page) is written by the caller (Orchestrator.run_queued_report) before
`complete()` commits it alongside the queue bookkeeping.
"""

import datetime as dt
import json
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .lock import Held

SCHEMA_VERSION = 1
MAX_CLAIM_ATTEMPTS = 3      # loser-backs-off retries before claim() gives up
MAX_REQUEST_ATTEMPTS = 2    # analyst attempts before a request moves to failed/
MAX_PUSH_ATTEMPTS = 3       # rebase-and-retry rounds for complete()/fail_request()

# substrings `git push` prints for a plain non-fast-forward rejection (the
# recoverable race) — anything else (auth, network, no remote) is not a race
# and must propagate rather than trigger a resync-and-retry loop
_REJECTED_MARKERS = ("[rejected]", "non-fast-forward", "fetch first")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git(wiki: Path, *args: str, check: bool = True):
    from .gitutil import git as _run_git
    return _run_git(wiki, *args, check=check)


def _push_best_effort(wiki: Path) -> bool:
    from .gitutil import push_best_effort
    return push_best_effort(wiki)


def _rel(wiki: Path, p: Path) -> str:
    return str(p.relative_to(wiki))


def _is_push_rejected(exc: RuntimeError) -> bool:
    msg = str(exc)
    return any(m in msg for m in _REJECTED_MARKERS)


def _dirs(wiki: Path) -> dict[str, Path]:
    root = wiki / "queue"
    return {k: root / k for k in ("pending", "claimed", "failed", "results")}


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")


def _read_records(dir_path: Path) -> list[tuple[str, dict]]:
    """(filename, record) pairs, oldest-created first. A corrupt request file
    is skipped rather than raised — one bad file must not wedge the whole
    queue for every other pending/claimed/failed entry."""
    if not dir_path.is_dir():
        return []
    out = []
    for p in sorted(dir_path.glob("*.json")):
        try:
            out.append((p.name, json.loads(p.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    out.sort(key=lambda item: item[1].get("created_at", ""))
    return out


def _publish(wiki: Path, message: str, run_id: str, lock: "Held",
             push: bool = False) -> str:
    """Commit whatever the caller just wrote into the working tree, with the
    usual `Run-ID:` trailer. Returns the short sha, or `""` when the tree
    already held that content — callers here race with git elsewhere, and a
    no-op commit must not be an error.

    Everything the queue writes goes through `transaction.commit`, so the
    queue bookkeeping and the report page beside it land under one base
    assertion, one explicit pathspec and one lint, like every other wiki
    commit. `transaction` is imported lazily because `exchange.py` reaches
    this module from the researcher package (ADR-0002), which must not drag
    the wiki side of `dbwiki` in behind it."""
    from .transaction import (Committed, NothingToDo, capture, commit, head,
                              resolve_actor)
    proposal = capture(wiki, head(wiki), resolve_actor(wiki), message,
                       trailers=(("Run-ID", run_id),))
    match commit(wiki, proposal, lock=lock, push=push):
        case Committed(sha=sha):
            return sha
        case NothingToDo():
            return ""
        case refused:
            raise RuntimeError(f"queue: {message}: the wiki transaction "
                               f"refused: {refused}")


def _push_rebase_retry(wiki: Path) -> None:
    """Push; on a plain rejection, `pull --rebase -X theirs` and retry, up to
    MAX_PUSH_ATTEMPTS. `-X theirs` implements the accepted same-day-supersede
    semantic for the one path both nodes may touch (reports/<day>.md): if a
    same-path conflict does occur, the side doing the rebasing keeps its own
    (just-produced, presumably newer) content rather than raising for a human
    to merge by hand. NB the naming inversion: during a rebase the commits
    being replayed are the *local* ones and git calls the upstream side
    "ours", so keeping local content requires `theirs`. A non-rejection
    failure (dead network, no remote, auth) is not a race and is left to
    propagate — see gitutil.push_best_effort's warn-and-continue for the case
    where that is instead the desired behavior (best-effort callers pass
    push=False and never reach here)."""
    for _ in range(MAX_PUSH_ATTEMPTS):
        try:
            _git(wiki, "push")
            return
        except RuntimeError as exc:
            if not _is_push_rejected(exc):
                raise
            _git(wiki, "pull", "--rebase", "-X", "theirs")
    _push_best_effort(wiki)  # exhausted retries: warn, don't fail the run


def _supersede_targets(wiki: Path, kind: str, day: str, keep: str) -> list[str]:
    """Filenames (not the one named `keep`) still pending for (kind, day) —
    an older analysis request a newer one subsumes (ADR-0001: "a 20:15 window
    subsumes the 18:15 one")."""
    pending = _dirs(wiki)["pending"]
    return [name for name, rec in _read_records(pending)
            if rec.get("kind") == kind and rec.get("day") == day
            and name != keep]


def enqueue_request(wiki: Path, *, run_id: str, kind: str, day: str,
                    suffix: str, window: tuple[str, str],
                    notable_dbs: list[str], prompt: str, lock: "Held",
                    push: bool = True) -> str:
    """Write a new pending analysis request and commit it (on-prem side of
    ADR-0001's "structured-first, agentic supersede"). Any older pending
    request for the same (kind, day) is deleted in the same commit — claim()
    would supersede it anyway, but doing it here keeps `dbwiki health`'s
    pending-age reading honest between analyst runs.

    `prompt` is the full agentic prompt text the on-prem node already
    assembled (the request is self-contained: everything else the analyst's
    agent needs is the wiki checkout itself). Never a credential, never a
    digest excerpt beyond what the prompt already carries."""
    fname = f"{kind}-{day}{suffix}-{run_id}.json"
    pending = _dirs(wiki)["pending"]
    record = {
        "schema_version": SCHEMA_VERSION, "kind": kind, "run_id": run_id,
        "day": day, "suffix": suffix,
        "window": {"from": window[0], "to": window[1]},
        "notable_dbs": sorted(notable_dbs), "created_at": _now(),
        "attempts": 0, "prompt": prompt,
    }
    superseded = _supersede_targets(wiki, kind, day, keep=fname)
    _write_record(pending / fname, record)
    for name in superseded:
        (pending / name).unlink(missing_ok=True)
    return _publish(wiki, f"queue: enqueue {kind} {day}{suffix}"
                          + (f" (supersedes {len(superseded)} pending)"
                             if superseded else ""), run_id, lock, push=push)


def pending_requests(wiki: Path) -> list[dict]:
    """Pending requests, oldest-queued first, each with its filename under
    `_file` (needed to locate/claim/delete it; not part of the schema)."""
    return [{"_file": name, **rec} for name, rec in _read_records(_dirs(wiki)["pending"])]


def claimed_requests(wiki: Path) -> list[dict]:
    return [{"_file": name, **rec} for name, rec in _read_records(_dirs(wiki)["claimed"])]


def failed_requests(wiki: Path) -> list[dict]:
    return [{"_file": name, **rec} for name, rec in _read_records(_dirs(wiki)["failed"])]


def _select_claim_target(wiki: Path, kind: str | None = None):
    """The (kind, day) group for the **oldest day** pending, plus the
    filenames of that group's older, now-superseded members. `None` when
    pending/ is empty (or, with `kind` given, has nothing of that kind —
    phase 1 only ever queues `report`, but `dbwiki analyst --kind` is
    reserved for phase 2).

    The ADR specifies supersede within one (kind, day) but leaves cross-day
    ordering unspecified; ordering by day drains a multi-day backlog oldest
    report first, which is the order an operator reads them in. `created_at`
    breaks ties between kinds sharing a day.

    Day, not enqueue time. The two agree whenever ticks queue days in
    chronological order, and diverge exactly when they don't — an analyst
    node that was down while the on-prem side kept ticking comes back to a
    backlog whose enqueue order says nothing useful. This ordered by
    `created_at` until 2026-08-10; the test that was supposed to pin the
    behaviour only passed because `_now()` has one-second resolution, so two
    enqueues in the same second tied and fell through to a filename sort that
    happens to be day-ordered. It failed the moment CI was slow enough to
    straddle a second."""
    pending = _dirs(wiki)["pending"]
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for name, rec in _read_records(pending):
        if kind is not None and rec.get("kind") != kind:
            continue
        groups.setdefault((rec.get("kind"), rec.get("day")), []).append((name, rec))
    if not groups:
        return None
    best = None
    for members in groups.values():
        members.sort(key=lambda m: m[1].get("created_at", ""))
        newest_name, newest_rec = members[-1]
        older = [name for name, _ in members[:-1]]
        rank = (str(newest_rec.get("day") or ""),
                str(newest_rec.get("created_at") or ""))
        if best is None or rank < best[0]:
            best = (rank, newest_name, newest_rec, older)
    return best[1:]


def claim(wiki: Path, claimed_by: str, *, lock: "Held", kind: str | None = None,
         push: bool = True) -> dict | None:
    """Claim protocol steps 1-3 (ADR-0001): pull, pick the oldest pending day
    and the newest request within it — deleting older pending duplicates for
    that day in the same commit — move it pending/ -> claimed/, stamp
    claimed_by/claimed_at, commit, push. Returns the claimed record
    (with `_file` set),
    or `None` when there is nothing to claim (queue empty, nothing of `kind`,
    or this attempt lost every race). See the module docstring for how the
    claim-vs-claim race is resolved without a generic (and here
    unresolvable) merge."""
    if push:
        _git(wiki, "pull", "--rebase")
    for _ in range(MAX_CLAIM_ATTEMPTS):
        picked = _select_claim_target(wiki, kind)
        if picked is None:
            return None
        fname, record, superseded = picked
        before = _git(wiki, "rev-parse", "HEAD").strip()
        pending_dir, claimed_dir = _dirs(wiki)["pending"], _dirs(wiki)["claimed"]
        record = dict(record)
        record["claimed_by"] = claimed_by
        record["claimed_at"] = _now()
        (pending_dir / fname).unlink(missing_ok=True)
        for name in superseded:
            (pending_dir / name).unlink(missing_ok=True)
        _write_record(claimed_dir / fname, record)
        sha = _publish(wiki, f"queue: claim {record['kind']} {record['day']}"
                             f"{record['suffix']}", record["run_id"], lock)
        if not sha:
            return None  # picked file vanished between selection and staging
        if not push:
            return {"_file": fname, **record}
        try:
            _git(wiki, "push")
            return {"_file": fname, **record}
        except RuntimeError as exc:
            if not _is_push_rejected(exc):
                raise
            _git(wiki, "reset", "--hard", before)
            _git(wiki, "fetch")
            _git(wiki, "reset", "--hard", "@{u}")
            if not (pending_dir / fname).exists():
                return None  # someone else's claim landed first
            continue  # still pending after resync (unrelated rejection); retry
    return None


def complete(wiki: Path, request: dict, telemetry_record: dict, *, lock: "Held",
            push: bool = True) -> str:
    """Claim protocol step 5: one commit carries the report the caller
    already wrote into the working tree, the claimed request's deletion, and
    a telemetry event file for the on-prem fold-in (fold_results). The
    request's own `run_id` — not a fresh one — names the event, so a wiki
    commit, the eventual `.state/agent_runs.jsonl` line and the structured
    placeholder's earlier commit all correlate."""
    claimed = _dirs(wiki)["claimed"] / request["_file"]
    claimed.unlink(missing_ok=True)
    event_id = uuid4().hex[:12]
    results = _dirs(wiki)["results"]
    _write_record(results / f"{request['run_id']}-{event_id}.json",
                  {**telemetry_record, "event_id": event_id, "at": _now()})
    sha = _publish(wiki, f"report: {request['day']}{request['suffix']} "
                         f"(analyst)", request["run_id"], lock)
    if push:
        _push_rebase_retry(wiki)
    return sha


def fail_request(wiki: Path, request: dict, error_category: str, *, lock: "Held",
                 telemetry_record: dict | None = None, push: bool = True,
                 max_attempts: int = MAX_REQUEST_ATTEMPTS) -> dict:
    """Claim protocol step 6: increment `attempts`; below `max_attempts`,
    return the request to pending/ (clearing the claim stamp) so a later
    `dbwiki analyst` run picks it up again; at `max_attempts`, move it to
    failed/ with the error category instead — a terminal outcome, which is
    the only case that also writes a `queue/results/` telemetry event (a
    requeued attempt is not yet a fact worth folding into agent_runs.jsonl).
    Wiki-edit rollback itself is the caller's job (transaction.restore) —
    this function only ever touches queue/."""
    claimed = _dirs(wiki)["claimed"] / request["_file"]
    record = dict(request)
    record.pop("_file", None)
    record["attempts"] = int(record.get("attempts", 0)) + 1
    terminal = record["attempts"] >= max_attempts
    claimed.unlink(missing_ok=True)
    if terminal:
        record["error_category"] = error_category
        target = _dirs(wiki)["failed"] / request["_file"]
        _write_record(target, record)
        if telemetry_record is not None:
            event_id = uuid4().hex[:12]
            results = _dirs(wiki)["results"]
            _write_record(results / f"{request['run_id']}-{event_id}.json",
                          {**telemetry_record, "event_id": event_id,
                           "at": _now(), "error_category": error_category})
    else:
        record["claimed_by"] = None
        record["claimed_at"] = None
        target = _dirs(wiki)["pending"] / request["_file"]
        _write_record(target, record)
    sha = _publish(wiki, f"queue: {'fail' if terminal else 'requeue'} "
                         f"{record['kind']} {record['day']}{record['suffix']} "
                         f"({error_category})", request["run_id"], lock)
    if push:
        _push_rebase_retry(wiki)
    record["_terminal"] = terminal
    record["_commit"] = sha
    return record


def fold_results(wiki: Path, state_dir: Path, *, push: bool = True) -> int:
    """Fold `queue/results/*.json` — telemetry the analyst could not write to
    the on-prem `.state/` directly — into `.state/agent_runs.jsonl`, delete
    the folded files, and commit. Called at the start of every `dbwiki run`
    tick, before compaction. Idempotent: a result whose `event_id` is already
    in `agent_runs.jsonl` is deleted without being re-appended (ADR-0001,
    Consequences — replaying wiki history must not resurrect stale queue
    entries as duplicate telemetry; the key is the event id, not the run id,
    because an analyst result deliberately shares its run_id with the
    enqueuing tick's own entries). Quiet (returns 0, no commit, no git
    calls beyond the read) when `queue/` does not exist: an install that
    never sets `analyst.enabled` must see zero behavior change."""
    results = _dirs(wiki)["results"]
    if not results.is_dir():
        return 0
    files = sorted(results.glob("*.json"))
    if not files:
        return 0
    from .health import append_agent_run_event, known_agent_event_ids
    known = known_agent_event_ids(state_dir)
    folded = 0
    for p in files:
        try:
            event = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue  # left for a human; must not wedge the tick
        event_id = event.get("event_id")
        if not (event_id and event_id in known):
            append_agent_run_event(state_dir, event)
            folded += 1
            if event_id:
                known.add(event_id)
        _git(wiki, "rm", "-q", "--ignore-unmatch", _rel(wiki, p))
    if not _git(wiki, "diff", "--cached", "--name-only").strip():
        return 0
    _git(wiki, "commit", "-m", f"queue: fold {len(files)} analyst result(s)")
    if push:
        _push_best_effort(wiki)
    return folded
