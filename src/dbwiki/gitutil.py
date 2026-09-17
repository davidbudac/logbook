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


def git(repo: Path, *args: str, check: bool = True,
        timeout: float | None = TIMEOUT_S) -> str:
    """A timeout expiring is a RuntimeError like any other git failure, not a
    `TimeoutExpired`, so every existing caller's handling covers it."""
    try:
        p = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)}: no answer after "
                           f"{timeout}s") from None
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def git_bytes(repo: Path, *args: str, input: bytes | None = None) -> bytes:
    """`git` for blob content: stdout as bytes, so a page that is not UTF-8
    reaches the caller instead of raising in the decode."""
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       input=input)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: "
                           f"{p.stderr.decode(errors='replace').strip()}")
    return p.stdout


def changed_paths(repo: Path) -> tuple[str, ...]:
    """Every path `git status` reports as changed or untracked.

    `-z` rather than the line format: NUL-separated records are never quoted,
    so a path holding a space or a non-ASCII byte arrives as the bytes the
    filesystem holds instead of git's octal escaping of them, which no caller
    could open. A rename is one record naming the new path followed by the
    original path as its own field, and both are returned: the old path still
    carries a staged deletion a caller has to see. -uall so files in a new
    directory are listed individually rather than collapsed to `dir/`."""
    fields = git(repo, "status", "--porcelain=v1", "-z", "-uall").split("\0")
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
