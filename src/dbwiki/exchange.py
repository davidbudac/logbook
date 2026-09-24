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

import datetime as dt
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .queue import (_hours_since, _is_push_rejected, _now, _push_best_effort,
                    _push_rebase_retry, _read_records, _write_record)
from .queue import _git as _qgit
from .validate import safe_id

SCHEMA_VERSION = 1
MAX_CLAIM_ATTEMPTS = 3
MAX_REQUEST_ATTEMPTS = 2
#: A claim older than this is a dead researcher's; `reclaim_stale` returns
#: it to pending/. The analyst queue's 26h (queue.STALE_CLAIM_HOURS), kept
#: rather than scaled to research's weekly cadence: a live claim lasts one
#: agent run (`timeout_seconds`, 600 by default), so a day is already a wide
#: margin, and the on-prem side folds results in only once a week anyway —
#: any budget under that week reaches the same fold-in. `dbwiki health`
#: flags a claim stale at the same age (`research.exchange.stale_hours`).
STALE_CLAIM_HOURS = 26

LAYOUT = {"pending": "requests/pending", "claimed": "requests/claimed",
          "failed": "requests/failed", "results": "results"}
KINDS = ("research", "source-review")


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


def validate_request(rec: dict) -> list[str]:
    """The fields of a request that become file names or prompt text: a
    known kind, and a run_id and code/slug that are safe ids (no `/`, no
    `..`, no leading dot — validate.safe_id). Both sides read records the
    other side wrote, so both check. Problems, empty when clean."""
    problems = []
    if rec.get("kind") not in KINDS:
        problems.append(f"kind {rec.get('kind')!r} is not one of {', '.join(KINDS)}")
    for what, value in (("run_id", rec.get("run_id")),
                        ("code" if rec.get("kind") == "research" else "slug",
                         rec.get("code") or rec.get("slug"))):
        try:
            safe_id(value, what)
        except ValueError as exc:
            problems.append(str(exc))
    return problems


def request_filename(rec: dict) -> str:
    """`<kind>-<key>-<run_id>.json`; raises ValueError for an unsafe id."""
    return (f"{rec.get('kind')}-{safe_id(request_key(rec), 'key')}-"
            f"{safe_id(rec.get('run_id'), 'run_id')}.json")


def result_filename(rec: dict) -> str:
    """`<run_id>-<key>.json`; raises ValueError for an unsafe id — a forged
    `run_id: ../../x` must not choose where the result is written."""
    return f"{safe_id(rec.get('run_id'), 'run_id')}-{safe_id(request_key(rec), 'key')}.json"


# ---- the result contract both sides check (issue 10) --------------------------------------
#
# Output written by a model (the researcher's cause/action and references, a
# caveat note, a structured proposal) may carry exactly one kind of link: an
# `https://` citation URL on an approved source's domain, in its own field.
# Anything link-shaped in prose would reach the page past the approved-source
# rail (which only sees `http(s)://`), and anything but a plain URL in a URL
# field would be written raw into the citation.

_URL_FORBIDDEN_RE = re.compile(r"[\s<>()\[\]{}\"'`\\|^]")
_PROSE_LINK_RE = re.compile(
    r"\]\(|\]\["                                   # [text](url), [text][ref]
    r"|(?m:^\s*\[[^\]\n]+\]:)"                       # [ref]: url
    r"|[A-Za-z][A-Za-z0-9+.-]*:\s*//"                # any scheme://
    r"|(?<![A-Za-z0-9:/])//[A-Za-z0-9]"              # scheme-relative //host
    r"|(?<![A-Za-z0-9.-])www\d{0,3}\."               # GFM bare-www autolink
    r"|<[A-Za-z/!?]"                                 # HTML, <autolink>
    r"|[A-Za-z0-9._%+-]@[A-Za-z0-9-]+\.[A-Za-z]"      # e-mail autolink
    r"|\b(?:javascript|vbscript|data|file):", re.I)


def citation_url_problem(url, domains) -> str | None:
    """None when `url` is a plain `https://` URL whose host is one of
    `domains` or a subdomain of one; else what is wrong with it. No
    whitespace or line break, no `<>()[]{}"'` (markdown or HTML smuggled
    into a citation), no userinfo, ASCII only."""
    if not isinstance(url, str) or not url:
        return "missing"
    if not url.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in url):
        return "not a plain ASCII URL"
    if _URL_FORBIDDEN_RE.search(url):
        return "contains whitespace, quotes or markdown/HTML characters"
    if not url.startswith("https://"):
        return "not an https:// URL"
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return "carries userinfo"
    host = (parts.hostname or "").lower()
    if not host:
        return "has no host"
    if not any(host == d or host.endswith("." + d) for d in domains):
        return f"host {host!r} is not an approved domain of the cited source"
    return None


def prose_link_problem(text) -> str | None:
    """None when model prose carries nothing a markdown renderer would turn
    into a link (inline/reference links, any `scheme://` or `//host`, bare
    `www.`, `<tag>`/`<autolink>`, e-mail addresses, script schemes); else
    the offending snippet. Plain `<` before a space, `[args]` and `a/b`
    stay legal: Oracle prose needs them."""
    if not isinstance(text, str):
        return None
    m = _PROSE_LINK_RE.search(text)
    return f"link-like text {text[max(0, m.start() - 10):m.end() + 10]!r}" if m else None


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
    rec = dict(rec) if isinstance(rec, dict) else {"raw": path.name}
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


def reclaim_stale(root: Path, *, push: bool = True,
                  stale_hours: float = STALE_CLAIM_HOURS,
                  now: dt.datetime | None = None) -> list[str]:
    """Return every claim older than `stale_hours` to `requests/pending/` —
    its researcher died before `complete` or `fail_request` — in one commit,
    and return the file names; queue.reclaim_stale's rules exactly. The dead
    run counts as an attempt, so a request that keeps killing its researcher
    ends in `failed/` (`stale_claim`); a claim whose `claimed_at` does not
    parse is left alone, `dbwiki health` names it.

    Both sides run this — the researcher's `claim` and the on-prem research
    round — so a reclaim only stands once it reaches the remote. With `push`,
    a push that fails for any reason drops the reclaim commit again instead
    of rebasing it later over whatever the other side did meanwhile: the
    same claim reclaimed there already (the dead run would count twice), or
    reclaimed and claimed afresh (a modify/delete conflict). Whichever side
    pulls next sees the current state and decides again, so running it
    twice, or from both sides at once, moves a claim at most once."""
    # the clock `claimed_at` is stamped with, so the two cannot disagree
    now = now or dt.datetime.strptime(_now(), "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)
    dirs = _dirs(root)
    before = _git(root, "rev-parse", "HEAD").strip()
    moved = []
    for name, rec in _read_records(dirs["claimed"]):
        age = _hours_since(rec.get("claimed_at"), now)
        if age is None or age <= stale_hours:
            continue
        record = dict(rec)
        record["attempts"] = int(record.get("attempts", 0)) + 1
        if record["attempts"] >= MAX_REQUEST_ATTEMPTS:
            record["error_category"] = "stale_claim"
            target = dirs["failed"] / name
        else:
            record["claimed_by"] = None
            record["claimed_at"] = None
            target = dirs["pending"] / name
        _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, dirs["claimed"] / name))
        (dirs["claimed"] / name).unlink(missing_ok=True)  # an untracked claim
        _write_record(target, record)
        moved.append(name)
    if not moved or not _commit(root, f"exchange: reclaim {len(moved)} stale "
                                      f"claim(s)", "reclaim"):
        return []
    if push:
        try:
            _git(root, "push")
        except RuntimeError as exc:
            _git(root, "reset", "-q", "--hard", before)
            print(f"dbwiki: exchange reclaim not pushed, dropped until the "
                  f"next pull: {exc}", file=sys.stderr)
            return []
    return moved


def claim(root: Path, claimed_by: str, *, kind: str | None = None,
          push: bool = True,
          stale_hours: float = STALE_CLAIM_HOURS) -> dict | None:
    """Pull, pick the oldest pending request, move it pending/ -> claimed/
    with a claim stamp, commit, push. Loser-backs-off on a rejected push
    exactly like queue.claim. Returns the record (with `_file`) or None.

    Claims older than `stale_hours` go back to pending/ first
    (`reclaim_stale`), so a dead researcher's request is picked up by the
    next claim — possibly this one — rather than waiting for a human."""
    if push:
        pull(root)
    reclaim_stale(root, push=push, stale_hours=stale_hours)
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
    target = dirs["results"] / result_filename(request)       # validates first
    _git(root, "rm", "-q", "--ignore-unmatch", _rel(root, dirs["claimed"] / request["_file"]))
    result = {**result, "run_id": request.get("run_id"), "kind": request.get("kind"),
              "completed_at": _now()}
    _write_record(target, result)
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
