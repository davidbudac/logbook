"""One `git` wrapper for every module that drives a repository (orchestrator,
analyst queue, research exchange, researcher). Stdlib only, on purpose: the
researcher package (ADR-0002) imports the exchange protocol and must not drag
the ES/compactor/wiki side of `dbwiki` in behind it."""

import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

#: Ceiling on one git invocation. Generous: every local call here is
#: measured in milliseconds on this wiki, so the number is only ever reached
#: by something that has stopped making progress.
TIMEOUT_S = 120.0

#: The one call that talks to a network. Shorter, because the portal serves
#: it from a request thread: a hung remote must bound one click rather than
#: accumulate stuck threads with no ceiling.
PUSH_TIMEOUT_S = 60.0


def _run(repo: Path, args: tuple[str, ...], timeout: float | None,
         input: bytes | None = None) -> subprocess.CompletedProcess:
    """One git invocation, output as bytes. A timeout expiring is a
    RuntimeError like any other git failure, not a `TimeoutExpired`, so every
    caller's handling covers it."""
    try:
        return subprocess.run(["git", "-C", str(repo), *args],
                              capture_output=True, input=input,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)}: no answer after "
                           f"{timeout}s") from None


def _failed(args: tuple[str, ...],
            p: subprocess.CompletedProcess) -> RuntimeError:
    return RuntimeError(f"git {' '.join(args)}: "
                        f"{p.stderr.decode(errors='replace').strip()}")


def git(repo: Path, *args: str, check: bool = True,
        timeout: float | None = TIMEOUT_S) -> str:
    """`git` as text. A timeout is a RuntimeError (see `_run`); so is output
    that is not UTF-8 — a file name git prints verbatim — rather than a
    `UnicodeDecodeError` no caller expects. Callers that must survive such
    names read bytes instead (`git_bytes`, `changed_paths`, `ls_tree`)."""
    p = _run(repo, args, timeout)
    if check and p.returncode != 0:
        raise _failed(args, p)
    try:
        text = p.stdout.decode()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"git {' '.join(args)}: output is not UTF-8 "
                           f"({exc.reason} at byte {exc.start})") from None
    # universal newlines, as the text-mode `subprocess.run` this replaced gave
    return text.replace("\r\n", "\n").replace("\r", "\n")


def git_bytes(repo: Path, *args: str, input: bytes | None = None,
              timeout: float | None = TIMEOUT_S) -> bytes:
    """`git` for blob content: stdout as bytes, so a page that is not UTF-8
    reaches the caller instead of raising in the decode. Bounded by the same
    `TIMEOUT_S` as `git`: `cat_blobs`, `Tree.at` and `ls_tree` run while a
    stage holds the lock, and a hung git must not hold it forever."""
    p = _run(repo, args, timeout, input)
    if p.returncode != 0:
        raise _failed(args, p)
    return p.stdout


def changed_paths(repo: Path) -> tuple[str, ...]:
    """Every path `git status` reports as changed or untracked.

    `-z` rather than the line format: NUL-separated records are never quoted,
    so a path holding a space or a non-ASCII byte arrives as the bytes the
    filesystem holds instead of git's octal escaping of them, which no caller
    could open. A rename is one record naming the new path followed by the
    original path as its own field, and both are returned: the old path still
    carries a staged deletion a caller has to see. -uall so files in a new
    directory are listed individually rather than collapsed to `dir/`.

    Read as bytes and decoded with `surrogateescape`, as `ls_tree` does: a
    name that is not UTF-8 used to raise `UnicodeDecodeError` here — which
    no caller catches — and so defeated `-z` for exactly the names it is
    for. Now it reaches the caller, and round-trips back to the same bytes
    when it is opened or handed to git again."""
    fields = git_bytes(repo, "status", "--porcelain=v1", "-z",
                       "-uall").decode(errors="surrogateescape").split("\0")
    paths = []
    i = 0
    while i < len(fields):
        record, i = fields[i], i + 1
        if len(record) < 4:
            continue
        paths.append(record[3:])
        if record[0] in "RC" or record[1] in "RC":
            paths.append(fields[i])
            i += 1
    return tuple(paths)


def ls_tree(repo: Path, revision: str) -> tuple[str, ...]:
    """Every path in the tree at `revision`, repo-relative.

    `-z` for `changed_paths`' reason: NUL-separated names are never quoted, so
    a path holding a space or a non-ASCII byte arrives as the bytes git holds
    rather than as octal escaping no caller could open. Read as bytes and
    decoded with `surrogateescape`, so a name that is not UTF-8 reaches the
    caller instead of raising here."""
    out = git_bytes(repo, "ls-tree", "-r", "-z", "--name-only", revision)
    return tuple(name.decode(errors="surrogateescape")
                 for name in out.split(b"\0") if name)


def cat_blobs(repo: Path, revision: str,
              paths: Iterable[str]) -> dict[str, bytes]:
    """The content of many paths at one revision, in one subprocess.

    One `cat-file --batch` where a read per path is one `git show` each; that
    saving is the whole reason this exists. Bytes out: decoding is the
    caller's job, as it is for `git_bytes`. A path absent at `revision` is
    absent from the result rather than an error, so a caller may ask for an
    inventory it is not certain of — which is also why an unborn HEAD needs no
    special case, git answering `missing` for every request.

    Responses arrive in request order, so the parse walks the requests with an
    offset into stdout and never reads the name git echoes back: a request
    text can hold spaces, which would make both a name split and a header
    split wrong. Hence `endswith` for the missing marker."""
    wanted = list(paths)
    if not wanted:
        return {}
    stdin = b"".join(f"{revision}:{path}\0".encode(errors="surrogateescape")
                     for path in wanted)
    out = git_bytes(repo, "cat-file", "--batch", "-z", input=stdin)
    blobs: dict[str, bytes] = {}
    at = 0
    for path in wanted:
        end = out.index(b"\n", at)
        header, at = out[at:end], end + 1
        if header.endswith(b" missing"):
            continue
        size = int(header.rsplit(b" ", 1)[1])
        blobs[path] = out[at:at + size]
        at += size + 1
    return blobs


class PullFailed(RuntimeError):
    """`pull_rebase` gave up. The repository is as it was before the call:
    same HEAD, same working tree, no rebase in progress, no stash entry."""


def rebase_in_progress(repo: Path) -> bool:
    """A stopped or interrupted rebase: the state directory either backend
    leaves under the git dir until `--continue` or `--abort`."""
    for name in ("rebase-merge", "rebase-apply"):
        path = Path(git(repo, "rev-parse", "--git-path", name).strip())
        if (path if path.is_absolute() else repo / path).exists():
            return True
    return False


def _stash_top(repo: Path) -> str:
    return git(repo, "rev-parse", "-q", "--verify", "refs/stash",
               check=False).strip()


def _undo_pull(repo: Path, head: str, stash_before: str) -> None:
    """Put `repo` back at `head` with its uncommitted changes, whichever way
    the rebase ended. A rebase that stopped is aborted, which restores HEAD
    and re-applies git's own autostash. A rebase that finished but whose
    autostash would not re-apply has left the tree full of conflict markers
    and the autostash in the stash list: reset to `head` — the commit the
    stash was taken from, so it applies cleanly there — then apply and drop
    it."""
    if rebase_in_progress(repo):
        git(repo, "rebase", "--abort")
    stash = _stash_top(repo)
    if stash and stash != stash_before:
        git(repo, "reset", "-q", "--hard", head)
        git(repo, "stash", "apply", "-q", "--index", stash)
        if _stash_top(repo) == stash:
            git(repo, "stash", "drop", "-q")
    elif git(repo, "rev-parse", "HEAD").strip() != head:
        # finished, nothing stashed, yet a failure: still not ours to keep
        git(repo, "reset", "-q", "--keep", head)


def pull_rebase(repo: Path, *, strategy_option: str | None = None) -> int:
    """Fetch, then rebase local commits onto the upstream branch with
    `--autostash`, so uncommitted changes (machine output a tick keeps in the
    tree) ride along. Returns how many upstream commits came in; 0 when
    there were none, in which case nothing in the tree is touched at all.

    `strategy_option` is passed as `-X`, and reads inverted: during a rebase
    upstream is "ours" and the replayed local commits are "theirs".

    All or nothing. Any failure — a remote that does not answer within
    `PUSH_TIMEOUT_S`, no upstream, a conflict `-X` does not settle, an
    autostash that will not re-apply — is undone (`_undo_pull`) and raised
    as `PullFailed`: a caller that carries on must never find the repository
    mid-rebase or holding a half-applied stash. A rebase already in progress
    is someone else's and is refused without being touched."""
    if rebase_in_progress(repo):
        raise PullFailed(f"git pull in {repo}: a rebase is already in "
                         f"progress; left alone for a human")
    try:
        head = git(repo, "rev-parse", "HEAD").strip()
        git(repo, "fetch", "-q", timeout=PUSH_TIMEOUT_S)
        upstream = git(repo, "rev-parse", "--verify", "@{u}").strip()
        behind = int(git(repo, "rev-list", "--count",
                         f"{head}..{upstream}").strip())
    except RuntimeError as exc:
        raise PullFailed(str(exc)) from None  # nothing touched yet
    if not behind:
        return 0
    stash_before = _stash_top(repo)
    try:
        git(repo, "rebase", "-q", "--autostash",
            *(("-X", strategy_option) if strategy_option else ()), upstream)
        if _stash_top(repo) != stash_before:
            raise RuntimeError("uncommitted changes conflict with the pulled "
                               "commits")
    except RuntimeError as exc:
        try:
            _undo_pull(repo, head, stash_before)
        except RuntimeError as undo:
            raise PullFailed(f"{exc}; undoing the pull failed too, the "
                             f"repository needs a human: {undo}") from None
        raise PullFailed(f"{exc}; pull undone") from None
    return behind


def push_best_effort(repo: Path) -> bool:
    """One push attempt; a failure warns and returns False. Used where the
    caller's own commit is the durable record and a dead remote must not turn
    a successful local write into a failure — the next successful push carries
    everything."""
    try:
        git(repo, "push", timeout=PUSH_TIMEOUT_S)
        return True
    except RuntimeError as exc:
        print(f"dbwiki: push failed (will retry next run): {exc}",
              file=sys.stderr)
        return False
