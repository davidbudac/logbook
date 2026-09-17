"""The research exchange (ADR-0002): a separate git repository holding only
queue state, the *only* channel between the on-prem node and the researcher.
Same claim protocol and the same low-level primitives as the analyst queue
(`queue.py`: record files, `git` as the lock, loser-backs-off claim races,
rebase-retry pushes), parametrised on the exchange root and on a request key
of `(kind, code|slug)` instead of `(kind, day)`.

```
<exchange>/
├── requests/pending/<kind>-<key>-<run_id>.json   # awaiting a researcher
├── requests/claimed/…                            # a researcher is on it
├── requests/failed/…                             # gave up / rejected, with a reason
└── results/<run_id>-<key>.json                   # the researcher's answer
```

Neither side ever writes wiki content here. The on-prem side enqueues
redacted requests (research_offload.build_*_request) and consumes results;
the researcher claims requests and writes results. Every record is JSON the
schemas in research_offload.py describe."""

import json
import sys
from pathlib import Path

from .queue import (_is_push_rejected, _now, _push_best_effort,
                    _push_rebase_retry, _read_records, _write_record)
from .queue import _git as _qgit

SCHEMA_VERSION = 1
MAX_CLAIM_ATTEMPTS = 3
MAX_REQUEST_ATTEMPTS = 2

LAYOUT = {"pending": "requests/pending", "claimed": "requests/claimed",
          "failed": "requests/failed", "results": "results"}


def _git(root: Path, *args: str, check: bool = True):
    return _qgit(root, *args, check=check)


def _dirs(root: Path) -> dict[str, Path]:
    return {k: root / v for k, v in LAYOUT.items()}


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root))


def _commit(root: Path, message: str, run_id: str) -> str:
    """Stage everything and commit with the usual `Run-ID:` trailer. Returns
    the short sha, or `""` when the tree already held that content — both
    sides race with git elsewhere, and a no-op commit must not be an error.

    The exchange repo holds queue records, never wiki pages, so it publishes
    with plain git rather than through `transaction`: there is no curated
    content to lint, no machine output to overlay, and the researcher package
    that imports this module must not pull the wiki side of `dbwiki` in
    (ADR-0002)."""
    _git(root, "add", "-A")
    if not _git(root, "diff", "--cached", "--name-only").strip():
        return ""
    _git(root, "commit", "-m", f"{message}\n\nRun-ID: {run_id}")
    return _git(root, "rev-parse", "--short", "HEAD").strip()


def request_key(rec: dict) -> str:
    return str(rec.get("code") or rec.get("slug") or "unknown")


def request_filename(rec: dict) -> str:
    return f"{rec.get('kind')}-{request_key(rec)}-{rec.get('run_id')}.json"


def result_filename(rec: dict) -> str:
    return f"{rec.get('run_id')}-{request_key(rec)}.json"


def is_exchange(root: Path) -> bool:
    return (Path(root) / ".git").exists() or (Path(root) / "requests").is_dir()


def pull(root: Path) -> None:
    """Best-effort `pull --rebase`; a dead remote is not a reason to skip the
    local work (both sides commit locally first, always)."""
    try:
        _git(root, "pull", "--rebase")
    except RuntimeError as exc:
        print(f"dbwiki: exchange pull failed (working from local state): {exc}",
              file=sys.stderr)


def pending_requests(root: Path) -> list[dict]:
    return [{"_file": n, **r} for n, r in _read_records(_dirs(root)["pending"])]


def claimed_requests(root: Path) -> list[dict]:
    return [{"_file": n, **r} for n, r in _read_records(_dirs(root)["claimed"])]


def failed_requests(root: Path) -> list[dict]:
    return [{"_file": n, **r} for n, r in _read_records(_dirs(root)["failed"])]


def result_files(root: Path) -> list[Path]:
    d = _dirs(root)["results"]
    return sorted(d.glob("*.json")) if d.is_dir() else []


def enqueue(root: Path, request: dict, *, push: bool = True,
            commit: bool = True) -> str:
    """Write one pending request; any older pending request for the same
    `(kind, key)` is deleted in the same commit (superseded). Returns the
    commit sha, or the filename when `commit=False` (batch mode: the caller
    commits once for several requests via `commit_batch`)."""
    pending = _dirs(root)["pending"]
    fname = request_filename(request)
    kind, key = request.get("kind"), request_key(request)
    superseded = [n for n, r in _read_records(pending)
                  if r.get("kind") == kind and request_key(r) == key and n != fname]
    _write_record(pending / fname, request)
    for n in superseded:
        _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, pending / n))
    if not commit:
        return fname
    sha = _commit(root, f"exchange: enqueue {kind} {key}"
                        + (f" (supersedes {len(superseded)})" if superseded else ""),
                  str(request.get("run_id")))
    if push:
        _push_best_effort(root)
    return sha


def commit_batch(root: Path, message: str, run_id: str, *, push: bool = True) -> str:
    sha = _commit(root, message, run_id)
    if push and sha:
        _push_best_effort(root)
    return sha


def consume_result(root: Path, path: Path) -> None:
    """Stage the deletion of a folded result (batched; see commit_batch)."""
    _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, path))


def reject_result(root: Path, path: Path, reason: str) -> None:
    """A result the on-prem side cannot apply (unknown run_id, no such page,
    unapproved citation, failed lint…) is moved to requests/failed/ with the
    reason, so nothing is silently dropped and `dbwiki health` counts it."""
    try:
        rec = json.loads(path.read_text())
    except (OSError, ValueError):
        rec = {"raw": path.name}
    rec = dict(rec)
    rec["error_category"] = "result-rejected"
    rec["reason"] = reason[:500]
    rec["rejected_at"] = _now()
    _write_record(_dirs(root)["failed"] / f"result-{path.name}", rec)
    _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, path))


def _select_claim_target(root: Path, kind: str | None = None):
    """Oldest-created pending request (of `kind`, when given), plus the
    older pending duplicates of the same `(kind, key)` it supersedes."""
    groups: dict[tuple, list[tuple[str, dict]]] = {}
    for name, rec in _read_records(_dirs(root)["pending"]):
        if kind is not None and rec.get("kind") != kind:
            continue
        groups.setdefault((rec.get("kind"), request_key(rec)), []).append((name, rec))
    if not groups:
        return None
    best = None
    for members in groups.values():
        members.sort(key=lambda m: m[1].get("created_at", ""))
        newest_name, newest_rec = members[-1]
        older = [n for n, _ in members[:-1]]
        rank = str(newest_rec.get("created_at") or "")
        if best is None or rank < best[0]:
            best = (rank, newest_name, newest_rec, older)
    return best[1:]


def claim(root: Path, claimed_by: str, *, kind: str | None = None,
          push: bool = True) -> dict | None:
    """Pull, pick the oldest pending request, move it pending/ -> claimed/
    with a claim stamp, commit, push. Loser-backs-off on a rejected push
    exactly like queue.claim. Returns the record (with `_file`) or None."""
    if push:
        pull(root)
    dirs = _dirs(root)
    for _ in range(MAX_CLAIM_ATTEMPTS):
        picked = _select_claim_target(root, kind)
        if picked is None:
            return None
        fname, record, superseded = picked
        before = _git(root, "rev-parse", "HEAD").strip()
        record = dict(record)
        record["claimed_by"] = claimed_by
        record["claimed_at"] = _now()
        _git(root, "rm", "-q", _rel(root, dirs["pending"] / fname))
        for n in superseded:
            _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, dirs["pending"] / n))
        _write_record(dirs["claimed"] / fname, record)
        sha = _commit(root, f"exchange: claim {record.get('kind')} "
                            f"{request_key(record)}", str(record.get("run_id")))
        if not sha:
            return None
        if not push:
            return {"_file": fname, **record}
        try:
            _git(root, "push")
            return {"_file": fname, **record}
        except RuntimeError as exc:
            if not _is_push_rejected(exc):
                raise
            _git(root, "reset", "--hard", before)
            _git(root, "fetch")
            _git(root, "reset", "--hard", "@{u}")
            if not (dirs["pending"] / fname).exists():
                return None
            continue
    return None


def complete(root: Path, request: dict, result: dict, *, push: bool = True) -> str:
    """Write the result, delete the claimed request, one commit, push with
    rebase-retry. The result carries the request's run_id (correlation with
    the on-prem redaction mapping and the eventual wiki commit)."""
    dirs = _dirs(root)
    _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, dirs["claimed"] / request["_file"]))
    result = {**result, "run_id": request.get("run_id"), "kind": request.get("kind"),
              "completed_at": _now()}
    _write_record(dirs["results"] / result_filename(request), result)
    sha = _commit(root, f"exchange: result {request.get('kind')} {request_key(request)}",
                  str(request.get("run_id")))
    if push:
        _push_rebase_retry(root)
    return sha


def fail_request(root: Path, request: dict, error_category: str, *,
                 push: bool = True, max_attempts: int = MAX_REQUEST_ATTEMPTS) -> dict:
    """attempts+1; below max back to pending/ (claim cleared), at max to
    failed/ with the category."""
    dirs = _dirs(root)
    record = dict(request)
    record.pop("_file", None)
    record["attempts"] = int(record.get("attempts", 0)) + 1
    terminal = record["attempts"] >= max_attempts
    _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, dirs["claimed"] / request["_file"]))
    if terminal:
        record["error_category"] = error_category
        _write_record(dirs["failed"] / request["_file"], record)
    else:
        record["claimed_by"] = None
        record["claimed_at"] = None
        _write_record(dirs["pending"] / request["_file"], record)
    sha = _commit(root, f"exchange: {'fail' if terminal else 'requeue'} "
                        f"{record.get('kind')} {request_key(record)} ({error_category})",
                  str(record.get("run_id")))
    if push:
        _push_rebase_retry(root)
    record["_terminal"] = terminal
    record["_commit"] = sha
    return record
