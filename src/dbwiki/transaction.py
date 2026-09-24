"""Publishing a set of wiki pages as one Git commit, or not at all.

One value carries everything. A `Proposal` is the full desired content of a
named set of paths, derived from a named base revision by a named actor.

- Deterministic: bytes are a function of (base, command, actor, at); preview
  and commit publish the same value.
- Explicit pathspec: `proposal.files` keys are the pathspec. `restore`
  reverts exactly those paths.
- Idempotent: absolute content, so writing it twice converges; `commit`
  recognises its own interrupted run by comparing bytes.
- Optimistic: `commit` refuses unless HEAD still equals `proposal.base`.
- Validated before touching the live tree: lint runs in a throwaway
  worktree at base, overlaid with uncommitted machine output and the
  proposal, for `preview` and `commit` alike. One verdict.

Locking belongs to the caller. Every mutating command already holds
`lock.single_flight` for its whole duration (`cli._with_lock`), and a second
`flock` on the same file in the same process conflicts with the first, so a
lock taken here would fail against the orchestrator. `commit` takes the
`Held` token the context manager yields and refuses when it is not active,
which makes the precondition checkable instead of a comment.
"""

import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import gitutil
from .lint import Finding, blocking, linkers, lint_wiki, resolve_link
from .lock import Held

#: Wiki directories holding machine output rather than curated pages.
MACHINE_DIRS = ("digests/", "html/")

#: The agent's result file, written into the wiki root; never curated content.
AGENT_RESULT = ".agent-result.json"


def is_machine(rel: str) -> bool:
    return rel.startswith(MACHINE_DIRS) or rel == AGENT_RESULT


class LockNotHeld(RuntimeError):
    """`commit` was called without an active `Held` token."""


class ProposalError(ValueError):
    """A `Proposal` names a path it may not write, or has no message. Raised
    at construction, because a path set can arrive from an HTTP body."""


@dataclass(frozen=True)
class Actor:
    """Who is publishing. A value now, an authenticated identity later.

    `source` is the seam: Stage 0 mints `flag`, `config` or `git`; Stage 4's
    provider mints `authenticated` and nothing downstream changes. The email
    lands in three places that must agree (commit `--author`, the `Actor:`
    trailer, the record's `actor:` field); they agree because the Actor
    travels inside the Proposal."""

    email: str
    name: str = ""
    source: str = "git"

    def __post_init__(self) -> None:
        if "@" not in self.email or any(c in self.email for c in "<>\n"):
            raise ValueError(f"not a usable actor email: {self.email!r}")
        if any(c in self.name for c in "<>\n"):
            raise ValueError(f"not a usable actor name: {self.name!r}")

    def git_author(self) -> str:
        """`Name <email>`. A nameless actor borrows the email's local part:
        git refuses `<email>` alone with `empty ident name`, so a wiki whose
        `user.name` is unset could publish nothing at all."""
        return f"{self.name or self.email.partition('@')[0]} <{self.email}>"


def resolve_actor(wiki: Path, *, configured: str | None = None,
                  override: str | None = None) -> Actor:
    """The one identity seam for the CLI. Precedence: `override` (`--actor`),
    then `configured` (`portal.operator_email`), then `git config user.email`
    in the wiki checkout. Raises when none resolves: an unattributable action
    must not be written (the precondition `health`'s `git_identity` finding
    already asserts)."""
    name = gitutil.git(wiki, "config", "user.name", check=False).strip()
    for email, source in ((override, "flag"), (configured, "config")):
        if email and email.strip():
            return Actor(email.strip(), name, source)
    email = gitutil.git(wiki, "config", "user.email", check=False).strip()
    if not email:
        raise RuntimeError(
            f"no actor: pass --actor, set portal.operator_email, or set "
            f"git config user.email in {wiki}")
    return Actor(email, name, "git")


@dataclass(frozen=True)
class Tree:
    """A read-only snapshot of the wiki at one revision.

    Builders read through this and never touch the working tree, so a
    proposal is derived from the revision it claims, not from whatever a
    human left on disk between preview and commit. `read` returns None for a
    path absent at that revision."""

    revision: str
    read: Callable[[str], str | None]

    @classmethod
    def at(cls, wiki: Path, revision: str) -> "Tree":
        """Blob reads via `git show <rev>:<path>`, memoised per path."""
        cache: dict[str, str | None] = {}

        def read(path: str) -> str | None:
            if path not in cache:
                try:
                    blob = gitutil.git_bytes(wiki, "show", f"{revision}:{path}")
                    cache[path] = blob.decode(errors="surrogateescape")
                except RuntimeError:
                    cache[path] = None
            return cache[path]

        return cls(revision, read)

    @classmethod
    def batch_at(cls, wiki: Path, revision: str,
                 paths: Iterable[str]) -> "Tree":
        """`at` for a path set known in advance: one `cat-file --batch` for
        all of them rather than one `git show` each, which is what makes
        reading the whole wiki per revision affordable. A path absent at
        `revision` reads as None, exactly as `at` gives it."""
        blobs = gitutil.cat_blobs(wiki, revision, paths)
        return cls.of({path: blob.decode(errors="surrogateescape")
                       for path, blob in blobs.items()}, revision)

    @classmethod
    def of(cls, files: Mapping[str, str], revision: str = "0" * 40) -> "Tree":
        """An in-memory tree, so `lifecycle.build` is testable without git."""
        snapshot = dict(files)
        return cls(revision, snapshot.get)


@dataclass(frozen=True)
class Proposal:
    """The whole transaction as data: what the wiki should say, and why.

    `files` maps wiki-relative POSIX path to complete desired content, or to
    None to delete. `base` rides inside so a caller cannot commit a proposal
    against a base it was not built for; the caller still asserts it by
    obtaining it from `head()` or the browser round trip and handing it to
    the builder. `notes` are operator advisories that do not affect bytes.

    Construction validates every path: not absolute, no `..`, not machine
    output; and a non-empty message. Nothing downstream re-checks."""

    base: str
    actor: Actor
    message: str
    files: Mapping[str, str | None]
    trailers: tuple[tuple[str, str], ...] = ()
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ProposalError("a proposal needs a commit message")
        for rel in self.files:
            path = PurePosixPath(rel)
            if not path.parts:
                raise ProposalError("a proposal names an empty path")
            if path.is_absolute():
                raise ProposalError(f"path is absolute: {rel}")
            if ".." in path.parts:
                raise ProposalError(f"path leaves the wiki: {rel}")
            if is_machine(rel):
                raise ProposalError(f"path is machine output: {rel}")

    @property
    def paths(self) -> tuple[str, ...]:
        """Sorted, so every git invocation gets a stable pathspec."""
        return tuple(sorted(self.files))

    def commit_message(self) -> str:
        """Subject, blank line, `Actor: <email>`, then `trailers` in order."""
        trailers = [f"Actor: {self.actor.email}"]
        trailers += [f"{k}: {v}" for k, v in self.trailers]
        return f"{self.message}\n\n" + "\n".join(trailers) + "\n"


@dataclass(frozen=True)
class Preview:
    """What committing this proposal would do, computed without touching the
    working tree."""

    base: str
    paths: tuple[str, ...]
    diff: str
    findings: tuple[Finding, ...]
    strays: tuple[str, ...]
    notes: tuple[str, ...] = ()

    def blocked(self) -> tuple[Finding, ...]:
        """`lint.blocking(findings)`, derived, never stored."""
        return tuple(blocking(list(self.findings)))


@dataclass(frozen=True)
class Committed:
    sha: str
    paths: tuple[str, ...]
    pushed: bool


@dataclass(frozen=True)
class NothingToDo:
    """Every file already holds the proposed content at `base`. A replayed
    transaction whose commit already landed does not come here; it gets
    `BaseMoved`."""

    base: str


@dataclass(frozen=True)
class BaseMoved:
    """HEAD is no longer `expected`. Nothing was written. The caller rebuilds
    against `actual` and previews again; the portal returns 409."""

    expected: str
    actual: str


@dataclass(frozen=True)
class LintBlocked:
    """Blocking lint findings over the proposed paths. The live tree was
    never written."""

    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class TreeDirty:
    """Uncommitted curated changes this proposal did not author. Nothing was
    written. `paths` names them so the operator can commit or restore."""

    paths: tuple[str, ...]


Outcome = Committed | NothingToDo | BaseMoved | LintBlocked | TreeDirty


@dataclass(frozen=True)
class Commit:
    """One commit that touched a page: a read-side value, not an outcome.

    `actor` is the `Actor:` trailer when the commit carries one, which every
    commit made through `commit()` does, and `""` otherwise. That empty case
    is exactly what distinguishes a transaction from an out-of-band hand edit
    somebody pushed, which the incident view has to show as such."""

    sha: str          # full
    short: str
    at: str           # committer date, ISO-8601 Z, whole seconds
    author: str       # author email
    subject: str
    actor: str        # `Actor:` trailer value, or ""



def head(wiki: Path) -> str:
    """Full sha of HEAD. Exists so the caller can obtain a base to assert; no
    function in this module calls it to invent one for itself."""
    return gitutil.git(wiki, "rev-parse", "HEAD").strip()


def page_history(wiki: Path, path: str, *,
                 limit: int = 20) -> tuple[Commit, ...]:
    """The last `limit` commits touching `path`, newest first, at HEAD.

    Lives here rather than in `gitutil` because it knows the `Actor:` trailer
    convention `Proposal.commit_message` writes, and `gitutil` is
    deliberately domain-free.

    One `git log` with a NUL-separated `--format` including
    `%(trailers:key=Actor,valueonly)`, so a subject holding a newline cannot
    desynchronise the parse. No `--follow`: its answer depends on similarity
    detection, so the history shown for one page would differ between git
    versions, and an incident page that moves is a different incident.

    A path with no history, or one that never existed, is `()` rather than an
    error: the read model must not fail on a page created in the working tree
    and not yet committed. Read-only, lock-free, safe to call
    concurrently."""
    fmt = ("%x1e%H%x00%h%x00%cI%x00%ae%x00%s%x00"
           "%(trailers:key=Actor,valueonly)")
    try:
        out = gitutil.git(wiki, "log", "-n", str(limit), f"--format={fmt}",
                          "--", path)
    except RuntimeError:
        return ()
    history = []
    for record in out.split("\x1e")[1:]:
        sha, short, at, author, subject, actor = record.split("\x00")
        when = datetime.fromisoformat(at).astimezone(timezone.utc)
        history.append(Commit(sha, short, when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              author, subject, actor.strip()))
    return tuple(history)


def last_commits(wiki: Path, revision: str | None = None
                 ) -> dict[str, Commit]:
    """The newest commit that touched each path, over the whole history, or
    over the history of `revision` when one is given: a snapshot pinned to a
    revision must not credit its pages to commits that revision has never
    seen (`readmodel.build`).

    One `git log --name-only` walk, newest first, first mention of a path
    winning, where `page_history` per page would be one subprocess each. Same
    NUL-separated `--format` and same `Actor:` trailer as `page_history`, and
    no `--follow` for the reason given there.

    Within a record the format output and the name list are separated by a
    newline rather than by the NUL that separates every other field, so the
    first name carries that newline as a prefix. A repository with no commits,
    or a revision git cannot walk, answers `{}`, the way `page_history`
    answers `()`."""
    fmt = ("%x1e%H%x00%h%x00%cI%x00%ae%x00%s%x00"
           "%(trailers:key=Actor,valueonly)")
    walk = (revision, "--") if revision is not None else ()
    try:
        out = gitutil.git_bytes(wiki, "log", "-z", "--name-only",
                                f"--format={fmt}", *walk)
    except RuntimeError:
        return {}
    latest: dict[str, Commit] = {}
    for record in out.decode(errors="surrogateescape").split("\x1e")[1:]:
        sha, short, at, author, subject, actor, *names = record.split("\x00")
        when = datetime.fromisoformat(at).astimezone(timezone.utc)
        touched = Commit(sha, short, when.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         author, subject, actor.strip())
        if names:
            names[0] = names[0].removeprefix("\n")
        for name in names:
            if name:
                latest.setdefault(name, touched)
    return latest


def stray_paths(wiki: Path) -> tuple[str, ...]:
    """Changed curated paths: `gitutil.changed_paths` minus machine dirs and
    the agent result file, sorted. Empty means the tree is publishable.
    Returning paths lets the caller decide (the orchestrator raises, `commit`
    classifies, the CLI prints)."""
    return tuple(sorted(p for p in gitutil.changed_paths(wiki)
                        if not is_machine(p)))


def staged_paths(wiki: Path) -> tuple[str, ...]:
    """Paths a `git add` has put in the index, sorted.

    The agent never stages anything (wiki/AGENTS.md: it does not touch git),
    so an index entry is a human's claim on the file. A rollback subtracts
    these: `restore` deletes an added path, and deleting work somebody staged
    during the agent's window is not a rollback, it is data loss."""
    return tuple(sorted(
        p for p in gitutil.git(wiki, "diff", "--cached", "--name-only", "-z")
        .split("\0") if p))


def capture(wiki: Path, base: str, actor: Actor, message: str,
            trailers: tuple[tuple[str, str], ...] = ()) -> Proposal:
    """Snapshot the working tree's curated changes as a Proposal.

    The migration seam for writers that mutate the tree in place and cannot
    be rewritten in Stage 0: the ingest agent and `structured.apply_proposal`.
    Deleted paths capture as None. `commit` writing the content back is a
    no-op by content, so nothing is copied twice."""
    return Proposal(base=base, actor=actor, message=message, trailers=trailers,
                    files={rel: _read(wiki / rel) for rel in stray_paths(wiki)})


def restore(wiki: Path, paths: Sequence[str], base: str) -> None:
    """Return exactly `paths` to their content at `base`: `git checkout
    <base> -- <path>` when tracked there, delete otherwise. Never
    `checkout -- .`, never `clean -fd`. Twice: identical result. Halfway:
    re-running finishes, because each path's target content is absolute."""
    tracked = [rel for rel in paths if _exists_at(wiki, base, rel)]
    added = [rel for rel in paths if rel not in tracked]
    if tracked:
        gitutil.git(wiki, "checkout", base, "--", *tracked)
    if added:
        gitutil.git(wiki, "rm", "--cached", "--quiet", "--ignore-unmatch",
                    "--", *added)
    for rel in added:
        (wiki / rel).unlink(missing_ok=True)
        _prune_dirs(wiki, (wiki / rel).parent)


def _read(path: Path) -> str | None:
    """A file's bytes as text, or None when nothing is there.

    `surrogateescape`, paired with `_place`, so bytes that are not UTF-8 make
    the round trip instead of reading as absent. Absent is the answer only for
    a path that is really gone: a symlink, a directory or anything else that
    is not a regular file has no content to propose, and calling it absent
    captures a deletion nothing ever performs, leaving a permanent stray."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if not stat.S_ISREG(mode):
        raise ProposalError(f"not a regular file: {path}")
    return path.read_bytes().decode(errors="surrogateescape")


def _exists_at(wiki: Path, revision: str, rel: str) -> bool:
    try:
        gitutil.git(wiki, "cat-file", "-e", f"{revision}:{rel}")
        return True
    except RuntimeError:
        return False


def _prune_dirs(wiki: Path, start: Path) -> None:
    """Remove the directories a deleted path leaves empty, up to the wiki
    root; a leftover empty directory reads as debris of a rolled-back run."""
    d = start
    while d != wiki and wiki in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent



def _differing(wiki: Path, proposal: Proposal) -> tuple[str, ...]:
    """The proposal's paths whose content differs from `base`."""
    tree = Tree.at(wiki, proposal.base)
    return tuple(rel for rel in proposal.paths
                 if proposal.files[rel] != tree.read(rel))


def _unowned(wiki: Path, proposal: Proposal) -> tuple[str, ...]:
    """Dirty curated paths `commit` refuses: everything uncommitted we did not
    author, plus our own paths holding bytes other than the ones we are about
    to write. A path already holding exactly our content is the debris of our
    own interrupted run, so it is adopted rather than refused."""
    return tuple(rel for rel in stray_paths(wiki)
                 if rel not in proposal.files
                 or _read(wiki / rel) != proposal.files[rel])


@contextmanager
def _candidate(wiki: Path, proposal: Proposal) -> Iterator[Path]:
    """Context manager. Materialise `proposal.base`, then the working tree's
    uncommitted machine output (a cited digest the compactor wrote and no
    tick has committed yet), then the proposal, in a detached worktree under
    a temp directory. Yields the worktree path; removes it in `finally`.
    Runs `git worktree prune` first so a worktree stranded by a crash is
    reclaimed."""
    gitutil.git(wiki, "worktree", "prune")
    tmp = Path(tempfile.mkdtemp(prefix="dbwiki-candidate-"))
    wt = tmp / "wt"
    gitutil.git(wiki, "worktree", "add", "--detach", str(wt), proposal.base)
    try:
        overlay = {rel: _read(wiki / rel) for rel in gitutil.changed_paths(wiki)
                   if rel.startswith(MACHINE_DIRS)}
        for rel, content in {**overlay, **proposal.files}.items():
            _place(wt / rel, content)
        yield wt
    finally:
        gitutil.git(wiki, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


def _place(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode(errors="surrogateescape"))


def _stageable(wiki: Path, paths: Sequence[str]) -> list[str]:
    """The paths `git add` can match: on disk, or still an index entry.

    A path a `git mv` already removed from the index is gone from the working
    tree as well, so `add` matches nothing for it and refuses the whole
    pathspec. Its deletion is staged already, and the `commit` pathspec
    carries it either way."""
    indexed = set(gitutil.git(wiki, "ls-files", "-z", "--",
                              *paths).split("\0"))
    return [rel for rel in paths
            if rel in indexed or (wiki / rel).is_file()]


_BROKEN_LINK_RE = re.compile(r"\A\[\[(.+)\]\] matches no page\Z")


def _lint(wt: Path, proposal: Proposal,
          changed: Sequence[str]) -> list[Finding]:
    """Lint over `changed` in the candidate worktree `wt`, plus the pages
    that linked to a page this proposal deletes (or renames away): those
    links break on somebody else's page, which linting `changed` alone never
    reads. On such a page only the links this proposal broke are reported,
    so debt it already carried does not block an unrelated deletion."""
    gone = {rel for rel in changed
            if proposal.files[rel] is None and rel.endswith(".md")}
    extra = sorted(linkers(wt, gone) - set(changed)) if gone else []
    findings = lint_wiki(wt, only_paths=list(changed) + extra)
    if not extra:
        return findings
    kept = set(changed)
    inventory = {p.relative_to(wt).as_posix() for p in wt.rglob("*.md")
                 if ".git" not in p.relative_to(wt).parts} | gone

    def broke(f: Finding) -> bool:
        m = _BROKEN_LINK_RE.match(f.message)
        return bool(m) and resolve_link(m.group(1), inventory) in gone

    return [f for f in findings if f.file in kept or broke(f)]


def preview(wiki: Path, proposal: Proposal) -> Preview:
    """What the commit would produce. Never writes the live tree, never takes
    the lock, never pushes; safe to call concurrently from the portal.
    Reports strays instead of refusing on them, so the operator sees both the
    diff and why it will not land."""
    changed = _differing(wiki, proposal)
    diff = ""
    findings: tuple[Finding, ...] = ()
    if changed:
        with _candidate(wiki, proposal) as wt:
            gitutil.git(wt, "add", "--all", "--", *changed)
            diff = gitutil.git(wt, "diff", "--cached", proposal.base, "--",
                               *changed)
            findings = tuple(_lint(wt, proposal, changed))
    return Preview(base=proposal.base, paths=changed, diff=diff,
                   findings=findings, strays=_unowned(wiki, proposal),
                   notes=proposal.notes)


def commit(wiki: Path, proposal: Proposal, *, lock: Held | None,
           push: bool = True) -> Outcome:
    """Publish the proposal, or leave the tree exactly as it was.

    1. `lock.active` is False -> raise LockNotHeld.
    2. HEAD != `proposal.base` -> `BaseMoved`, nothing written.
    3. Classify `stray_paths`. A stray path that is one of ours and already
       holds the exact bytes we are about to write is the debris of our own
       interrupted run: adopt it. Any other stray -> `TreeDirty`.
    4. Every file already equals its content at base -> `NothingToDo`.
    5. Lint in `_candidate`; `lint.blocking` non-empty -> `LintBlocked`. The
       live tree is still untouched.
    6. Write the files whose bytes differ; delete the None ones.
    7. `git add --all -- <paths git can match>` (which stages the deletions
       too), then `git commit --author <actor> -m <message + trailers> --
       <paths>`, so the commit carries that pathspec and nothing else the
       index holds. Any failure here, an interrupt included, restores the
       proposal's paths to base and re-raises.
    8. `gitutil.push_best_effort`; `Committed.pushed` says whether it went.

    Runs twice: the second call finds HEAD moved and returns `BaseMoved`
    (the transaction already landed), or adopts at step 3 and finishes.
    Dies between 6 and 7: the paths hold the proposed content and HEAD is
    still base; the identical retry converges via step 3. A retry built with
    a fresh `at` is a different proposal and lands in `TreeDirty` naming the
    debris; the CLI prints `--at` in every preview so the converging retry
    is the easy one to type."""
    if lock is None or not lock.active:
        raise LockNotHeld(f"commit to {wiki} without the single-flight lock")
    if (actual := head(wiki)) != proposal.base:
        return BaseMoved(proposal.base, actual)
    if strays := _unowned(wiki, proposal):
        return TreeDirty(strays)
    changed = _differing(wiki, proposal)
    if not changed:
        return NothingToDo(proposal.base)
    with _candidate(wiki, proposal) as wt:
        findings = blocking(_lint(wt, proposal, changed))
    if findings:
        return LintBlocked(tuple(findings))
    try:
        for rel in changed:
            _place(wiki / rel, proposal.files[rel])
            if proposal.files[rel] is None:
                _prune_dirs(wiki, (wiki / rel).parent)
        if stage := _stageable(wiki, changed):
            gitutil.git(wiki, "add", "--all", "--", *stage)
        gitutil.git(wiki, "commit", "--author", proposal.actor.git_author(),
                    "-m", proposal.commit_message(), "--", *changed)
    except BaseException:
        restore(wiki, proposal.paths, proposal.base)
        raise
    return Committed(gitutil.git(wiki, "rev-parse", "--short", "HEAD").strip(),
                     changed, gitutil.push_best_effort(wiki) if push else False)
