"""Orchestrator: runs agent stages against the wiki repo, validates their
result, and owns all git operations. Plain deterministic Python.

Every stage carries a `run_id` (minted by health.new_run_id when the caller
does not pass one) into the commit trailer and — for ingest — the ledger
entry, so a wiki commit, a run-health event and a telemetry row can be
correlated after the fact."""

import datetime as dt
import json
import time
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from . import prompts, transaction
from .config import Config
from .errors import ValidationError  # re-exported: it lived here first
from .gitutil import changed_paths, git as _run_git, push_best_effort
from .harness import (HarnessError, InvalidResultError, NoResultError, UNKNOWN,
                      run_agent)
from .health import (append_agent_run_event, categorize, exchange_stale_hours,
                     known_agent_event_ids, new_run_id, record_agent_run)
from .incidents import ACTION_HEAD_RE, INCIDENT_DIR, parse_actions
from .lock import Held
from .pagetext import frontmatter, sections
from .state import StateStore
from .transaction import (BaseMoved, Committed, LintBlocked, NothingToDo,
                          TreeDirty)
from .trigger import TriggerDecision, digest_needs_escalation


def _rejected(problems: Sequence[str], lint_findings: int = 0) -> ValidationError:
    return ValidationError("; ".join(problems), problems=problems,
                           lint_findings=lint_findings)


def _lint_blocked(findings: Sequence) -> ValidationError:
    return _rejected([f"{f.file}: {f.rule}: {f.message}" for f in findings],
                     len(findings))


class WikiMoved(RuntimeError):
    """`transaction.commit` refused because the wiki changed under the lock
    (`BaseMoved`, `TreeDirty`). `_refused` has already restored the stage's
    own paths, so the stage wrapper does not roll back a second time."""

    restored = True


def _problems(exc: BaseException) -> list[str]:
    """What a failed stage's ledger entry lists as its problems."""
    if isinstance(exc, ValidationError):
        return exc.problems
    return [str(exc)[:500] or type(exc).__name__]


def _git(wiki: Path, *args: str, check: bool = True) -> str:
    return _run_git(wiki, *args, check=check)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


#: `Orchestrator._settle`: seconds between two snapshots of the tree, and
#: how many rounds it waits for two equal ones before rolling back anyway.
SETTLE_S = 0.5
SETTLE_ROUNDS = 10

#: The wiki pages every stage may update, whatever it is.
SHARED_PAGES = ("index.md", "log.md")

#: Where each agentic stage may write beyond `SHARED_PAGES` — its workflow
#: in the wiki's AGENTS.md. Anything else (AGENTS.md, CLAUDE.md, README.md,
#: queue/, a page at the root) is refused. Research is not listed: it keeps
#: its own, stricter rails in `_research_problems`.
STAGE_DIRS = {
    "ingest": ("databases/", "errors/", "incidents/", "hosts/", "services/",
               "concepts/"),
    "report": ("reports/",),
    "lint": ("databases/", "errors/", "incidents/", "hosts/", "services/",
             "concepts/", "reports/"),
}

RESOLUTION_SECTION = "Resolution history"
LIFECYCLE_HINT = "use `dbwiki incident`"

#: The agentic prompt's version of the rule `structured.INGEST_TEMPLATE`
#: spells out: the history block is context from earlier days, and a roaming
#: agent that can read the wiki itself needs telling just as plainly.
#: Text in config/prompts/agentic-history-rule.md.
HISTORY_RULE = prompts.load("agentic-history-rule")


def _action_ats(text: str) -> list[str]:
    """The `at` of every `## Action` heading, parseable body or not — removal
    has to be caught on a record lint already rejects as malformed."""
    return [m.group(1) for heading, _ in sections(text)
            if (m := ACTION_HEAD_RE.match(heading))]


def _resolution_rows(text: str) -> list[str]:
    """The `## Resolution history` table's lines, stripped."""
    for heading, body in sections(text):
        if heading[3:].strip() == RESOLUTION_SECTION:
            return [ln.strip() for ln in body.splitlines()
                    if ln.strip().startswith("|")]
    return []


def _human_owned_diff(rel: str, before: str, after: str) -> list[str]:
    """What an agent changed on an incident page that ADR-0003 reserves to
    the lifecycle service. Empty means the edit was additive.

    Action records compare as parsed `ActionRecord`s rather than as bytes:
    `pagetext.day_block_replace` collapses blank-line runs page-wide when
    structured ingest replaces a `## Update <day>` block, so a byte
    comparison would refuse the ingest writer's own legitimate output."""
    old, new = frontmatter(before), frontmatter(after)
    problems = []
    if old.get("status") != new.get("status"):
        problems.append(f"{rel}: status is human-owned: {old.get('status')} "
                        f"-> {new.get('status')} ({LIFECYCLE_HINT})")
    if old.get("monitoring") != new.get("monitoring"):
        problems.append(f"{rel}: monitoring: is human-owned ({LIFECYCLE_HINT})")
    kept = set(_action_ats(after))
    problems += [f"{rel}: `## Action {at}` was removed; action records are "
                 f"human-owned" for at in _action_ats(before) if at not in kept]
    now = {record.at: record for record in parse_actions(after).records}
    problems += [f"{rel}: `## Action {record.at}` was altered; action records "
                 f"are human-owned"
                 for record in parse_actions(before).records
                 if record.at in now and now[record.at] != record]
    surviving = set(_resolution_rows(after))
    problems += [f"{rel}: a `## {RESOLUTION_SECTION}` row was removed or "
                 f"altered: {row}"
                 for row in _resolution_rows(before) if row not in surviving]
    return problems


def _dirty_tree(stray: tuple[str, ...]) -> RuntimeError:
    """Refuse to run an agent over uncommitted edits that are not ours: a
    failed run ends in a restore, which returns those paths to base. Dirty
    machine output (digests/, html/) is fine — digests are committed before
    the agent starts and html/ is regenerated every tick."""
    shown = ", ".join(stray[:5]) + ("…" if len(stray) > 5 else "")
    return RuntimeError(
        f"wiki working tree has uncommitted changes outside "
        f"{'/, '.join(d.rstrip('/') for d in transaction.MACHINE_DIRS)}/ "
        f"({shown}); commit or stash them before running agents")


def telemetry_fields(task: str, run_id: str, tele: dict, *, model_tier: str,
                     mode: str, digest_bytes: int | None = None,
                     result: dict | None = None, validation_ok: bool,
                     rolled_back: bool, lint_findings: int = 0) -> dict:
    """Additive, non-sensitive ledger telemetry for one attempted agent run:
    identifiers, counts, duration and usage only. Never a prompt body, a log
    message, a page body, or a credential (see DESIGN.md). `mode` names the
    path that ran (agentic, structured, offload, caveats, history)."""
    r = result or {}
    fields = {
        "run_id": run_id,
        "task": task,
        "adapter": tele.get("adapter"),
        "model": tele.get("model"),
        "model_tier": model_tier,
        "mode": mode,
        "duration_s": tele.get("duration_s"),
        "timed_out": bool(tele.get("timed_out")),
        "usage": tele.get("usage", "unknown"),
        "pages_touched": len(r.get("pages_touched") or []),
        "incidents_opened": len(r.get("incidents_opened") or []),
        "incidents_updated": len(r.get("incidents_updated") or []),
        "validation_ok": validation_ok,
        "rolled_back": rolled_back,
        "lint_findings": lint_findings,
        # set by _run_agent_with_retry, and by structured._propose for the
        # structured path's own retry (default 1 = no retry taken)
        "attempts": tele.get("attempts", 1),
    }
    if digest_bytes is not None:
        fields["digest_bytes"] = digest_bytes
    return fields


def _agentic_report_prompt(day: str, window: tuple[str, str], ingested: list[dict],
                           suffix: str, health: list[str] | None) -> str:
    """The plain-text agentic report prompt (no wiki-reading Material pack —
    the agent is expected to read the wiki itself). Used two ways: to run the
    agentic adapter directly (a notable window, `escalated_report` not
    structured, no analyst node), and, unrun, as the self-contained request
    payload handed to the analyst node when `analyst.enabled` delegates the
    window instead (ADR-0001) — the analyst's agent gets exactly this prompt,
    the wiki checkout supplies everything else."""
    items = "\n".join(
        f"- {i['db']}: {'NOTABLE' if i.get('notable') else 'routine'} — "
        f"{i.get('summary', '')}"
        + (f" (trigger: {i['trigger']})" if i.get("trigger") else "")
        for i in ingested) or "- (none ingested)"
    return (
        f"Task: report.\n"
        f"Window: {window[0]} -> {window[1]}. Report file: "
        f"reports/{day}{suffix}.md\n"
        f"Databases ingested in this window:\n{items}\n"
        + (f"Collection health (telemetry, not database state — never "
           f"report a collection gap as a database outage, and never treat "
           f"absent events as recovery):\n" + "\n".join(health) + "\n"
           if health else "")
        + f"Follow the `report` workflow in AGENTS.md exactly. Remember the "
        f"historical-context step: for every notable item, check its "
        f"error-class pages and past incidents and say whether we have "
        f"seen it before and what fixed it then.\n"
        f'Write the result JSON to .agent-result.json with "task": "report".'
    )


class Orchestrator:
    def __init__(self, cfg: Config, lock: Held | None = None):
        self.cfg = cfg
        self.wiki = cfg.wiki_repo
        self.state = StateStore(cfg.state_dir)
        self.lock = lock
        self._actor: transaction.Actor | None = None
        a = cfg.agents
        self.adapter = a.get("adapter", "codex")
        self.timeout = a.get("timeout_seconds", 900)
        # telemetry of the most recent stage, and any capture failures — read
        # by the CLI into the run-health event; never a reason to fail a stage
        self.last_telemetry: dict = {}
        self.telemetry_errors: list[str] = []

    @property
    def actor(self) -> transaction.Actor:
        """Who the stage commits as, resolved once at the first commit rather
        than in `__init__`: a checkout with no `user.email` is a `dbwiki
        health` finding, not a reason a dry run cannot construct an
        Orchestrator."""
        if self._actor is None:
            self._actor = transaction.resolve_actor(self.wiki)
        return self._actor

    @staticmethod
    def _run_id(run_id: str | None) -> str:
        return run_id or new_run_id()

    def _telemetry(self, task: str, run_id: str, tele: dict, *,
                   mode: str, db: str | None = None,
                   prompt: str | None = None, **kw) -> dict:
        """Build the ledger telemetry block, tolerating its own failure: a
        broken capture warns on stderr and yields a marker entry rather than
        aborting an otherwise-good ingest. Every call — one per attempted
        agent stage, success or failure — is also appended to the agent-run
        log (`health.record_agent_run`) and, when `langfuse.enabled`,
        exported as a Langfuse trace (observability.record_agent_run); `db`
        and `prompt` exist for those records only and never reach the ledger
        block, while `mode` lands in all three. Unlike the ledger, the Langfuse export carries content:
        the prompt and result JSON go to the configured host
        (docs/langfuse.md)."""
        try:
            fields = telemetry_fields(task, run_id, tele, mode=mode, **kw)
        except Exception as e:  # noqa: BLE001 — telemetry never blocks the run
            msg = f"{task}: {type(e).__name__}: {e}"[:200]
            self.telemetry_errors.append(msg)
            print(f"warning: telemetry capture failed: {msg}", file=sys.stderr)
            fields = {"run_id": run_id, "task": task, "mode": mode,
                      "telemetry_error": msg}
        self.last_telemetry = fields
        event_id = record_agent_run(self.cfg.state_dir, fields, tele,
                             db=db, mode=mode)
        from .observability import record_agent_run as export_trace
        export_trace(self.cfg, fields, tele, db=db, mode=mode, prompt=prompt,
                     result=kw.get("result"), event_id=event_id)
        return fields

    def _model(self, escalate: bool, adapter: str | None = None) -> str | None:
        tiers = self.cfg.agents.get(adapter or self.adapter, {}) or {}
        return tiers.get("strong" if escalate else "cheap")

    def _tier_of(self, model: str | None, adapter: str | None = None) -> str:
        """The tier label a model resolves to under `adapter`'s tier table, so
        telemetry names what was actually run: a `research.model` override
        that happens to be the strong tier is logged as strong, one that is
        neither tier as `override`."""
        tiers = self.cfg.agents.get(adapter or self.adapter, {}) or {}
        for tier in ("strong", "cheap"):
            if model and tiers.get(tier) == model:
                return tier
        return "override"

    def _provider(self, adapter: str | None = None) -> str | None:
        """Model provider for the adapter (pi only, e.g. `unsloth`)."""
        return (self.cfg.agents.get(adapter or self.adapter, {}) or {}).get("provider")

    def _push(self) -> None:
        """Best-effort push: the commit already exists locally, so a dead
        remote/network must not turn a successful run into a failure — the
        next successful push carries everything."""
        if self.cfg.report.get("push"):
            push_best_effort(self.wiki)

    def _commit_machine_dir(self, directory: str, message: str) -> bool:
        """Commit `directory` and nothing else, then push. True when a commit
        was made.

        The pathspec on both the staged-diff check and the commit is what
        keeps a human's staged work out of machine output, the same ownership
        rule `transaction.commit` applies to a proposal's paths: `git commit
        -- <dir>` records that directory's working-tree content whatever else
        the index is holding, and leaves those index entries where they are.
        A pipeline lock does not stop a human staging a file mid-run."""
        _git(self.wiki, "add", "--", directory)
        if not _git(self.wiki, "diff", "--cached", "--name-only", "--",
                    directory).strip():
            return False
        _git(self.wiki, "commit", "-m", message, "--", directory)
        self._push()
        return True

    def _commit_digests(self) -> None:
        if not (self.wiki / "digests").is_dir():
            return
        self._commit_machine_dir("digests", "digest: compactor output")

    def _propose(self, base: str, message: str,
                 run_id: str | None = None) -> transaction.Proposal:
        """The agent's edits, snapshotted as the transaction that would
        publish them. Built twice per stage: once inside the retry loop's
        `validate` (an early look the agent can still act on) and once for
        real, from the same working tree."""
        return transaction.capture(
            self.wiki, base, self.actor, message,
            trailers=(("Run-ID", run_id),) if run_id else ())

    def _blocked(self, proposal: transaction.Proposal) -> list[str]:
        """Deterministic provenance lint (docs/provenance.md) over the paths
        this stage would change, judged in the candidate worktree `commit`
        lints — so retry feedback and the commit verdict cannot disagree."""
        return [f"{f.file}: {f.rule}: {f.message}"
                for f in transaction.preview(self.wiki, proposal).blocked()]

    def _incident_problems(self, proposal: transaction.Proposal,
                           base: str) -> list[str]:
        """ADR-0003's ownership rail over the incident pages this stage would
        publish: an agent may add to one, never restate it.

        Status, the `monitoring:` window, the `## Action` records and the
        `## Resolution history` rows are the lifecycle service's to write, so
        each is compared against `base` and any difference refuses the whole
        publication. Appending a `## Update <day>`, an evidence line or an
        occurrence row, and bumping `updated:`, are what the ingest writers
        do and stay allowed. A page absent at `base` is a new incident, which
        is the ingest writer's job and has no human state to protect.

        Reads `base` through `transaction.Tree`: by the time this runs the
        agent has already written over the file on disk. Structural lint
        (`_blocked`) cannot see this — it judges the resulting page, not who
        was allowed to make the transition."""
        tree = transaction.Tree.at(self.wiki, base)
        problems: list[str] = []
        for rel in proposal.paths:
            if not (rel.startswith(f"{INCIDENT_DIR}/") and rel.endswith(".md")):
                continue
            if (before := tree.read(rel)) is None:
                continue
            after = proposal.files[rel]
            if after is None:
                problems.append(f"{rel}: an agent may not delete an incident "
                                f"page ({LIFECYCLE_HINT})")
                continue
            problems += _human_owned_diff(rel, before, after)
        return problems

    def _publish(self, proposal: transaction.Proposal) -> transaction.Outcome:
        return transaction.commit(self.wiki, proposal, lock=self.lock,
                                  push=bool(self.cfg.report.get("push")))

    def _snapshot(self) -> tuple:
        """The changed paths and their size/mtime: equal twice in a row
        means nothing is writing into the tree any more."""
        snap = []
        for rel in changed_paths(self.wiki):
            try:
                st = (self.wiki / rel).lstat()
                snap.append((rel, st.st_size, st.st_mtime_ns))
            except OSError:
                snap.append((rel, None, None))
        return tuple(sorted(snap, key=lambda s: s[0]))

    def _settle(self) -> None:
        """Wait, briefly and boundedly, until the working tree stops changing.
        The harness kills a timed-out adapter's whole process group, but a
        tool that put itself in its own session escapes that kill; restoring
        while it still writes leaves debris every later tick refuses on."""
        before = self._snapshot()
        for _ in range(SETTLE_ROUNDS):
            time.sleep(SETTLE_S)
            now = self._snapshot()
            if now == before:
                return
            before = now
        print("warning: the wiki was still changing when the rollback ran",
              file=sys.stderr)

    def _rollback(self, base: str, settle: bool = False) -> None:
        """Return the agent's writes to `base`, leaving anything a human
        staged during the run where they put it. `settle` first waits for the
        tree to stop changing — after a timeout, when the agent's processes
        were killed rather than finished."""
        if settle:
            self._settle()
        staged = set(transaction.staged_paths(self.wiki))
        self._restore([p for p in transaction.stray_paths(self.wiki)
                       if p not in staged], base)

    def _restore(self, paths: Sequence[str], base: str) -> list[str]:
        """Return `paths` to HEAD as it is *now*, which is `base` unless
        something committed during the run. Restoring to a stale `base`
        would check its old content out over a newer commit — staging a
        revert of it — and delete what that commit added. Returns the paths
        a commit since `base` touched, for the caller to report."""
        now = _git(self.wiki, "rev-parse", "HEAD").strip()
        moved: list[str] = []
        if now != base:
            touched = set(_git(self.wiki, "diff", "--name-only", "-z", base,
                               now, check=False).split("\0"))
            moved = sorted(p for p in paths if p in touched)
            print(f"dbwiki: HEAD moved during the run ({base[:12]} -> "
                  f"{now[:12]}); restoring to the new HEAD"
                  + (f", which also changed {', '.join(moved)}" if moved
                     else ""), file=sys.stderr)
        transaction.restore(self.wiki, paths, now)
        return moved

    def _refused(self, proposal: transaction.Proposal, base: str,
                 refused: transaction.Outcome) -> WikiMoved:
        """A publish `commit` refused wrote nothing, but the agent's edits are
        still in the tree; leave them and every later tick refuses on this
        tick's debris. A path a human staged in the meantime stays theirs."""
        staged = set(transaction.staged_paths(self.wiki))
        moved = self._restore([p for p in proposal.paths if p not in staged],
                              base)
        return WikiMoved(f"wiki changed under the lock: {refused}"
                         + (f"; the newer commit also changed "
                            f"{', '.join(moved)}" if moved else ""))

    @contextmanager
    def _stage(self, base: str, tele: dict,
               fail: Callable[[BaseException, bool], None]) -> Iterator[None]:
        """The one failure rail around an agentic stage — ingest, report,
        lint, the analyst's queued report and agentic research — from the
        moment the agent may write until the publish. Whatever raises in
        there ends the same way:

        1. the agent's writes go back to `base` (`_rollback`, which after a
           timeout first waits for the tree to settle); a `WikiMoved` has
           restored its own paths already;
        2. `fail(exc, rolled_back)` records what the stage records for a
           failure — the ledger entry `dbwiki retry` reads, the telemetry
           block, the queue requeue. A failure in there is printed and never
           replaces `exc`;
        3. `exc` propagates.

        Each stage used to catch `HarnessError` by hand and nothing else, so
        a malformed result, a writer tripping over a directory or a git error
        left the agent's edits in the tree and no ledger entry, and every
        later tick refused on "uncommitted changes" until someone cleaned the
        wiki by hand. `BaseException`, because the interrupt this matters for
        is an operator's Ctrl-C."""
        try:
            yield
        except BaseException as exc:
            rolled_back = True
            if not getattr(exc, "restored", False):
                try:
                    self._rollback(base, settle=bool(tele.get("timed_out")))
                except Exception as err:  # noqa: BLE001 — never mask exc
                    rolled_back = False
                    print(f"dbwiki: rollback failed: {type(err).__name__}: "
                          f"{err}", file=sys.stderr)
            try:
                fail(exc, rolled_back)
            except Exception as err:  # noqa: BLE001 — never mask exc
                print(f"dbwiki: recording the failure failed: "
                      f"{type(err).__name__}: {err}", file=sys.stderr)
            raise

    def _digests_clean(self) -> bool:
        return not any(p.startswith("digests/")
                       for p in changed_paths(self.wiki))

    def render_html(self, day: str) -> str:
        """Render the day's HTML summary + index and commit them. Machine
        output like digests: written by the orchestrator, never by an agent,
        and never linted (lint_wiki walks *.md only). Best-effort — a
        rendering failure must not fail a run whose wiki work already
        succeeded.

        Pending digests are committed first: a tick where every database was
        skipped commits nothing else, and the page's `digests/<db>/<day>.md`
        links have to resolve on the remote, not just on disk."""
        from .daily_html import DEFAULT_LINK_BASE, write_daily
        from .deeplink import Resolver
        try:
            self._commit_digests()
            write_daily(self.wiki, day, state=self.state,
                        link_base=self.cfg.report.get("link_base",
                                                      DEFAULT_LINK_BASE),
                        links=Resolver.from_config(
                            self.cfg.portal.get("links") or {}))
            if not self._commit_machine_dir("html",
                                            f"html: daily summary {day}"):
                return "(nothing to commit)"
        except Exception as exc:  # noqa: BLE001 — a summary page is not the run
            print(f"dbwiki: daily html render failed: {exc}", file=sys.stderr)
            return "(render failed)"
        return _git(self.wiki, "rev-parse", "--short", "HEAD").strip()

    def _validate(self, result: dict, task: str,
                  changed: Sequence[str]) -> list[str]:
        """The agent's result dict against what the stage would publish:
        `changed` is the transaction's path set (`Proposal.paths`), or the
        live tree's stray paths while the retry loop is still deciding."""
        problems = []
        if result.get("task") != task:
            problems.append(f"result.task={result.get('task')!r}, expected {task!r}")
        required = ("summary", "pages_touched") if task in ("lint", "research") \
            else ("summary", "pages_touched", "notable")
        for key in required:
            if key not in result:
                problems.append(f"result missing key {key!r}")
        for rel in result.get("pages_touched", []):
            if PurePosixPath(rel).is_absolute() or ".." in PurePosixPath(rel).parts:
                problems.append(f"pages_touched entry is not a wiki path: {rel}")
                continue
            p = self.wiki / rel
            if not p.exists():
                problems.append(f"pages_touched entry does not exist: {rel}")
            if rel.startswith("digests/"):
                problems.append(f"agent claims to have touched a digest: {rel}")
            if rel.startswith("html/"):
                problems.append(f"agent claims to have touched generated html: {rel}")
        declared = {str(rel).strip().removeprefix("./")
                    for rel in result.get("pages_touched", [])}
        problems += [f"undeclared change: {rel}" for rel in changed
                     if rel not in declared
                     and not transaction.is_machine(rel)
                     and rel not in ("index.md", "log.md")]
        if not self._digests_clean():
            problems.append("digests/ modified by agent")
        if not changed and task in ("ingest", "report"):
            problems.append("agent made no changes at all")
        if "log.md" not in changed and task in ("ingest", "report"):
            problems.append("log.md not updated")
        problems += self._path_problems(task, changed)
        problems += self._deletion_problems(task, changed)
        return problems

    def _deletion_problems(self, task: str,
                           changed: Sequence[str]) -> list[str]:
        """`index.md` and `log.md` must survive a run with content: a
        deletion used to count as "log.md updated", and both are exempt from
        the undeclared-change check. Deleting any other page is the lint
        stage's alone (merging a duplicate), inside its own directories —
        `_path_problems` holds it there."""
        problems = []
        for rel in changed:
            page = self.wiki / rel
            if rel in SHARED_PAGES:
                if not page.is_file() or not page.read_bytes().strip():
                    problems.append(f"{rel} was deleted or emptied")
            elif (not page.exists() and not page.is_symlink()
                  and task != "lint"):
                problems.append(f"{task} may not delete a page: {rel}")
        return problems

    @staticmethod
    def _path_problems(task: str, changed: Sequence[str]) -> list[str]:
        """Paths a stage may not touch at all: anything hidden (a dotfile
        such as `.lint-exceptions`, which would let an agent suppress the lint
        that judges it, or anything under a dot-directory), and — for the
        stages in `STAGE_DIRS` — anything outside its own directories and
        `SHARED_PAGES`."""
        allowed = STAGE_DIRS.get(task)
        problems = []
        for rel in changed:
            if any(part.startswith(".") for part in PurePosixPath(rel).parts):
                problems.append(f"{task} may not touch a hidden path: {rel}")
            elif (allowed is not None and rel not in SHARED_PAGES
                  and not rel.startswith(allowed)):
                problems.append(
                    f"{task} changed a path outside "
                    f"{', '.join(allowed + SHARED_PAGES)}: {rel}")
        return problems

    def _research_problems(self) -> list[str]:
        """Research-specific rails: only errors/, sources/, index.md and log.md
        may change; new source pages need a human commit; and every URL cited
        in a changed error page must sit on an approved source's domain (read
        from the post-run tree, so same-run source edits are honored)."""
        from .research import approved_domains, unapproved_urls
        problems = []
        tracked = set(_git(self.wiki, "ls-files", "-z").split("\0"))
        domains = approved_domains(self.wiki)
        for rel in transaction.stray_paths(self.wiki):
            if rel in ("index.md", "log.md"):
                continue
            if not (rel.startswith("errors/") or rel.startswith("sources/")):
                problems.append(f"research changed a path outside errors/, "
                                f"sources/, index.md, log.md: {rel}")
                continue
            if rel.startswith("sources/") and rel not in tracked:
                problems.append(f"research created a new source page: {rel} "
                                f"(new sources enter via a human commit)")
                continue
            page = self.wiki / rel
            if not rel.startswith("errors/") or not page.exists():
                continue
            bad = unapproved_urls(page.read_text(), domains)
            if bad:
                problems.append(f"{rel} cites URLs outside the approved-source "
                                f"domains: {', '.join(sorted(bad))}")
        return problems

    def _fail_ledger(self, key: str, content_hash: str, category: str,
                     problems: list[str], telemetry: dict | None = None) -> None:
        """Record a failed ingest attempt. `content_hash` and `error_category`
        are what `dbwiki retry` needs to decide whether the failure is safely
        replayable (health.plan_retries); `telemetry` carries the WS6 cost/
        quality block. All additive ledger fields."""
        self.state.set_ledger_entry(key, {
            "status": "failed", "at": _now(), "content_hash": content_hash,
            "error_category": category, "problems": problems,
            **(telemetry or {})})

    def _run_agent_with_retry(self, adapter: str, prompt: str, model: str | None,
                              timeout: int, *, validate: Callable[[dict], list[str]],
                              tele: dict, web: bool = False,
                              provider: str | None = None) -> dict:
        """Run an agentic stage via `run_agent`, retrying once when
        `agents.feedback_retries` (default 0) allows it. Two things trigger a
        retry: `validate(result)` returning problems, or `run_agent` raising
        the two recoverable harness failures — `NoResultError` (agent exited
        0 but wrote no result file) and `InvalidResultError` (it wrote one that
        is not JSON, or not the result contract's shape).
        Every other HarnessError (timeout, nonzero exit, unknown adapter) is
        fatal immediately, first attempt or retry, exactly as with no retry
        configured.

        A retry does NOT roll back: it re-runs with the ORIGINAL prompt plus
        a feedback block appended (the specific problems, or the missing-
        result complaint) and the agent's prior edits still in the tree, so
        it amends them in place instead of starting over.

        `validate` exists here only to decide whether to retry; it is the
        same validation the caller runs afterward for real (rollback/ledger/
        telemetry), so this is an early look, not a second source of truth —
        the caller re-derives nothing, it reuses its existing checks both
        times.

        Returns the last attempt's result even when it is still bad (caller's
        own validation then rolls back as usual); raises only for a fatal or
        retry-exhausted harness failure. `tele` is `run_agent`'s telemetry
        out-param, overwritten by each attempt in place — the final attempt's
        duration/usage/etc. are what the caller's telemetry sees, which is
        fine, we only add `tele['attempts']` for the caller to thread through
        `telemetry_fields`."""
        retries = self.cfg.agents.get("feedback_retries", 0)
        attempt = 1
        cur_prompt = prompt
        while True:
            tele["attempts"] = attempt
            try:
                result = run_agent(adapter, cur_prompt, self.wiki, model,
                                   timeout, web=web, provider=provider,
                                   telemetry=tele)
            except HarnessError as e:
                retryable = isinstance(e, (NoResultError, InvalidResultError))
                if not retryable or attempt > retries:
                    raise
                cur_prompt = (
                    f"{prompt}\n"
                    f"your run produced no valid .agent-result.json; you "
                    f"MUST write ONE valid JSON object to .agent-result.json\n")
                attempt += 1
                continue
            problems = validate(result)
            if not problems or attempt > retries:
                return result
            listing = "\n".join(f"- {p}" for p in problems)
            cur_prompt = (
                f"{prompt}\n"
                f"Your previous run was rejected by validation:\n{listing}\n"
                f"Your edits are still present in the working tree. Fix "
                f"exactly these problems (edit files as needed) and write "
                f"the result JSON to .agent-result.json again.\n")
            attempt += 1

    def _synthesize_result(self, task: str, db: str | None, notable: bool,
                           err: NoResultError) -> dict | None:
        """Agent edited the wiki but omitted the result JSON: build one
        deterministically from git status instead of losing the work. Every
        agentic stage takes this path; it is safe because the caller still
        runs its own validation over the synthesized result, so a run that
        never did its job (no report page, no touched log.md) fails there
        exactly as it did when the whole run was thrown away."""
        changed = list(transaction.stray_paths(self.wiki))
        if not changed:
            return None
        incidents = [p for p in changed if p.startswith("incidents/")]
        return {
            "task": task, "db": db, "notable": notable,
            "summary": f"(result synthesized from git status; agent omitted "
                       f"{task} result JSON) pages changed: {', '.join(changed[:8])}",
            "pages_touched": changed,
            "incidents_opened": [], "incidents_updated": incidents,
            "flags": [f"agent omitted result JSON: {str(err)[:200]}"],
        }

    def _structured_ingest(self, db: str, digest: dict, prompt: str,
                           rel_md: str, escalate: bool, tele: dict) -> dict:
        """Structured mode (DESIGN.md 2a fallback): the model returns one JSON
        proposal and deterministic writers make every edit. The rails around it
        are unchanged — validation, lint, rollback and the ledger do not care
        which path produced the result."""
        from .structured import apply_proposal, propose
        proposal = propose(prompt, self.cfg, escalate=escalate, telemetry=tele)
        return apply_proposal(self.wiki, db, digest, rel_md, proposal, _now())

    def _structured_report(self, day: str, window: tuple[str, str],
                           ingested: list[dict], prompt: str, suffix: str,
                           health: list[str] | None, tele: dict, *,
                           notable_dbs: set[str] | None = None) -> dict:
        """Structured mode for a routine window, or — when `notable_dbs` is
        given — an escalated window opted into `agents.escalated_report:
        structured` (see report()). The proposal carries prose only;
        apply_report reads the open incidents the prompt numbered, so nothing
        may write to the wiki between the two calls. `notable_dbs` both picks
        the strong tier (escalate=True) and turns on the extended
        notable_analysis contract in propose_report."""
        from .structured import apply_report, propose_report
        proposal = propose_report(prompt, self.cfg,
                                  escalate=notable_dbs is not None,
                                  notable_dbs=notable_dbs, telemetry=tele)
        return apply_report(self.wiki, day, window, ingested, proposal, _now(),
                            suffix=suffix, health=health)

    def record_decision(self, digest_json: Path,
                        decision: TriggerDecision) -> None:
        """Merge the trigger decision into the digest's ledger entry beside
        whatever the entry already holds (a result, a failure or nothing)."""
        self.state.merge_ledger_entry(str(digest_json.relative_to(self.wiki)),
                                      {"last_decision": decision.to_dict()})

    def ingest(self, db: str, digest_json: Path, dry_run: bool = False,
               run_id: str | None = None,
               decision: TriggerDecision | None = None) -> dict:
        """Ingest one digest. `decision`, the trigger's verdict that woke it,
        is recorded after the attempt, whatever its outcome: the attempt
        replaces the ledger entry whole, so the decision goes in last. A dry
        run records nothing."""
        try:
            return self._ingest(db, digest_json, dry_run, run_id)
        finally:
            if decision is not None and not dry_run:
                self.record_decision(digest_json, decision)

    def _ingest(self, db: str, digest_json: Path, dry_run: bool,
                run_id: str | None) -> dict:
        from .compactor import Compactor
        from .structured import DEFAULT_HISTORY_DAYS
        run_id = self._run_id(run_id)
        digest_bytes = len(digest_json.read_bytes())
        digest = json.loads(digest_json.read_text())
        content_hash = Compactor.content_hash(digest)
        prior = self.state.get_ledger().get(str(digest_json.relative_to(self.wiki)), {})
        if prior.get("status") == "ingested" and prior.get("content_hash") == content_hash:
            return {"task": "ingest", "db": db, "skipped": "content unchanged",
                    "notable": prior.get("notable"), "summary": prior.get("summary")}
        rel_json = digest_json.relative_to(self.wiki)
        rel_md = rel_json.with_suffix(".md")
        escalate = digest_needs_escalation(digest)
        w = digest["window"]
        # structured mode covers ingest and routine reports; lint and research
        # stay agentic
        structured = self.cfg.agents.get("mode") == "structured"
        history_days = self.cfg.agents.get("history_days",
                                           DEFAULT_HISTORY_DAYS)
        if structured:
            from .structured import build_prompt
            prompt = build_prompt(db, digest, self.wiki,
                                  history_days=history_days)
        else:
            from . import db_history
            from .structured import digest_codes
            prompt = (
                f"Task: ingest.\n"
                f"Database: {db}. Window: {w['from']} -> {w['to']} (day {w['day']}).\n"
                f"Digest to ingest: {rel_md} (machine twin: {rel_json}).\n"
                f"{'This digest contains incident-grade events; work carefully.' if escalate else 'Routine-looking digest.'}\n"
                f"Follow the `ingest` workflow and all rules in AGENTS.md exactly.\n"
                f"Write the result JSON to .agent-result.json in the repo root, "
                f'with "task": "ingest" and "db": "{db}".'
            )
            history = db_history.render(db_history.gather(
                self.wiki, db, today=dt.date.fromisoformat(w["day"]),
                codes=digest_codes(digest), days=history_days))
            if history:
                prompt += f"\n\n{history}\n\n{HISTORY_RULE}"
        if dry_run:
            print(prompt)
            return {}
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        base = transaction.head(self.wiki)
        ledger_key = str(rel_json)
        tier = "strong" if escalate else "cheap"
        tele: dict = {}

        def block(**kw) -> dict:
            return self._telemetry("ingest", run_id, tele, model_tier=tier,
                                   digest_bytes=digest_bytes, db=db,
                                   mode="structured" if structured
                                   else "agentic", prompt=prompt, **kw)

        def validate(result: dict) -> list[str]:
            if isinstance(result, dict):
                result.setdefault("notable", digest["notable"])
            proposal = self._propose(base, "ingest")
            return (self._validate(result, "ingest", proposal.paths)
                    + self._incident_problems(proposal, base)
                    + self._blocked(proposal))

        result: dict | None = None

        def failed(exc: BaseException, rolled_back: bool) -> None:
            self._fail_ledger(ledger_key, content_hash, categorize(exc),
                              _problems(exc),
                              block(result=result, validation_ok=False,
                                    rolled_back=rolled_back,
                                    lint_findings=getattr(exc, "lint_findings",
                                                          0)))

        with self._stage(base, tele, failed):
            try:
                if structured:
                    result = self._structured_ingest(db, digest, prompt,
                                                     rel_md.as_posix(),
                                                     escalate, tele)
                else:
                    result = self._run_agent_with_retry(
                        self.adapter, prompt, self._model(escalate),
                        self.timeout, provider=self._provider(), tele=tele,
                        validate=validate)
            except NoResultError as e:
                result = self._synthesize_result("ingest", db,
                                                 digest["notable"], e)
                if result is None:
                    raise
            # the digest states notability, not the agent
            # (test_ingest_defaults_a_missing_notable_to_the_digests)
            result.setdefault("notable", digest["notable"])
            proposal = self._propose(
                base,
                f"ingest: {db} {w['day']} — {result.get('summary', '')[:100]}",
                run_id)
            if problems := (self._validate(result, "ingest", proposal.paths)
                            + self._incident_problems(proposal, base)):
                raise _rejected(problems)
            match self._publish(proposal):
                case Committed(sha=sha):
                    pass
                case NothingToDo():
                    sha = None
                case LintBlocked(findings=findings):
                    raise _lint_blocked(findings)
                case BaseMoved() | TreeDirty() as refused:
                    raise self._refused(proposal, base, refused)
        self.state.set_ledger_entry(ledger_key, {
            "status": "ingested", "at": _now(), "commit": sha,
            "window_to": w["to"], "content_hash": content_hash,
            "notable": result.get("notable"),
            "summary": result.get("summary"),
            "incidents": result.get("incidents_opened", [])
                        + result.get("incidents_updated", []),
            "flags": result.get("flags", []),
            **block(result=result, validation_ok=True, rolled_back=False),
        })
        return result

    def report(self, day: str, window: tuple[str, str], ingested: list[dict],
               suffix: str = "", health: list[str] | None = None,
               run_id: str | None = None) -> dict:
        """`health` is the optional collection-health block (health.health_lines);
        absent health info simply drops the section.

        A routine window (nothing notable ingested) goes through structured
        mode when it is configured: the local model proposes prose and
        deterministic writers render the page. A notable window stays agentic
        by default whatever the mode — the historical-context step is the
        report's whole value and needs a model that can read the wiki — unless
        `agents.escalated_report: structured` opts it into the structured path
        too, with a strong-tier model and a deterministically assembled
        "Material" context pack standing in for that wiki-reading step (see
        structured.build_escalated_report_prompt). When an agentic escalated
        attempt dies in the harness rather than answering badly, that same
        structured path runs once as a fallback and the result carries a
        `fallback: structured …` flag (`agents.escalated_report_fallback`).

        ADR-0001: when `analyst.enabled` is true and the window escalates,
        the on-prem node never invokes the agentic adapter itself — it has no
        cloud-LLM credentials to invoke it with. Instead it forces the
        structured placeholder path (regardless of `agents.mode` /
        `agents.escalated_report`, both of which choose only between the
        agentic and structured paths for windows this node *does* run
        locally) and additionally enqueues the plain agentic prompt as a
        request for the analyst node to run later; the analyst's report
        supersedes the placeholder in place (same report path, same-day
        supersede)."""
        run_id = self._run_id(run_id)
        escalate = any(i.get("notable") for i in ingested)
        analyst_cfg = self.cfg.analyst
        delegate = escalate and bool(analyst_cfg.get("enabled"))
        escalated_structured = escalate and (
            delegate or self.cfg.agents.get("escalated_report") == "structured")
        structured = delegate or (
            self.cfg.agents.get("mode") == "structured"
            and (not escalate or escalated_structured))
        notable_dbs = ({i["db"] for i in ingested if i.get("notable")}
                      if escalated_structured else None)
        from .structured import DEFAULT_HISTORY_DAYS
        history_days = self.cfg.agents.get("history_days", DEFAULT_HISTORY_DAYS)
        if structured:
            if escalated_structured:
                from .structured import build_escalated_report_prompt
                # no suffix in the prompt: the model never names the file
                prompt = build_escalated_report_prompt(
                    day, window, ingested, self.wiki, health=health,
                    history_days=history_days)
            else:
                from .structured import build_report_prompt
                prompt = build_report_prompt(day, window, ingested, self.wiki,
                                             health=health)
        else:
            prompt = _agentic_report_prompt(day, window, ingested, suffix, health)
        tier = "strong" if escalate else "cheap"
        tele: dict = {}
        report_rel = f"reports/{day}{suffix}.md"
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        if delegate:
            # enqueue before attempting the placeholder: the analyst request
            # must survive a placeholder failure (its rollback resets to the
            # commit this makes), or delegation would die with the local
            # small model — the opposite of the ADR's graceful degradation
            from .queue import enqueue_request
            enqueue_request(
                self.wiki, run_id=run_id, kind="report", day=day, suffix=suffix,
                window=window, notable_dbs=sorted(notable_dbs or []),
                prompt=_agentic_report_prompt(day, window, ingested, suffix, health),
                push=bool(self.cfg.report.get("push")), lock=self.lock)
        base = transaction.head(self.wiki)

        def block(**kw) -> dict:
            return self._telemetry("report", run_id, tele, model_tier=tier,
                                   mode="structured" if structured
                                   else "agentic", prompt=prompt, **kw)

        def validate(result: dict) -> list[str]:
            # `escalate` is the window's notability, derived from what was
            # ingested, so the orchestrator knows it independently of the
            # agent's claim: an agent that forgets the key must not cost a
            # rollback of a good report.
            if isinstance(result, dict):
                result.setdefault("notable", escalate)
            proposal = self._propose(base, "report")
            problems = (self._validate(result, "report", proposal.paths)
                        + self._incident_problems(proposal, base)
                        + self._blocked(proposal))
            if not (self.wiki / report_rel).exists():
                problems.append(f"report file missing: {report_rel}")
            return problems

        result: dict | None = None

        def failed(exc: BaseException, rolled_back: bool) -> None:
            block(result=result, validation_ok=False, rolled_back=rolled_back,
                  lint_findings=getattr(exc, "lint_findings", 0))

        with self._stage(base, tele, failed):
            try:
                if structured:
                    result = self._structured_report(day, window, ingested,
                                                     prompt, suffix, health,
                                                     tele,
                                                     notable_dbs=notable_dbs)
                else:
                    result = self._run_agent_with_retry(
                        self.adapter, prompt, self._model(escalate),
                        self.timeout, provider=self._provider(), tele=tele,
                        validate=validate)
            except NoResultError as e:
                # the report page is already on disk and paid for; only the
                # result JSON is missing (see CHANGELOG, report NoResult).
                # Synthesize it and let the normal validation below judge the
                # work — a run that wrote no report page still fails there,
                # exactly as before.
                result = self._synthesize_result("report", None, escalate, e)
                if result is None:
                    raise
            except HarnessError as e:
                fallback = self.cfg.agents.get("escalated_report_fallback",
                                               "structured")
                if (structured or not escalate or fallback != "structured"
                        or isinstance(e, InvalidResultError)):
                    raise
                self._rollback(base, settle=bool(tele.get("timed_out")))
                block(validation_ok=False, rolled_back=True)
                # the rebinds are load-bearing: block() and the Langfuse
                # export read `structured`, `prompt` and `tele` at call time,
                # so the second attempt's ledger row must describe the
                # structured run and not the dead agentic one. `tele` starts
                # fresh because the agentic dict still holds that run's
                # exit_code/timed_out.
                from .structured import build_escalated_report_prompt
                structured = True
                prompt = build_escalated_report_prompt(
                    day, window, ingested, self.wiki, health=health,
                    history_days=history_days)
                tele = {}
                result = self._structured_report(
                    day, window, ingested, prompt, suffix, health, tele,
                    notable_dbs={i["db"] for i in ingested if i.get("notable")})
                result["flags"].append(
                    f"fallback: structured after agentic harness_error: "
                    f"{str(e)[:200]}")
            result.setdefault("notable", escalate)
            proposal = self._propose(
                base,
                f"report: {day}{suffix} — {result.get('summary', '')[:100]}",
                run_id)
            problems = (self._validate(result, "report", proposal.paths)
                        + self._incident_problems(proposal, base))
            if not (self.wiki / report_rel).exists():
                problems.append(f"report file missing: {report_rel}")
            if problems:
                raise _rejected(problems)
            match self._publish(proposal):
                case Committed() | NothingToDo():
                    pass
                case LintBlocked(findings=findings):
                    raise _lint_blocked(findings)
                case BaseMoved() | TreeDirty() as refused:
                    raise self._refused(proposal, base, refused)
        block(result=result, validation_ok=True, rolled_back=False)
        return result

    def run_queued_report(self, request: dict) -> dict:
        """ADR-0001 claim protocol step 4: run one claimed analyst request
        through the same rails as report()'s agentic branch —
        `_run_agent_with_retry`, `_validate`, the candidate lint and the
        restore to base — reused exactly, not reimplemented.
        `request["prompt"]` was already assembled on-prem
        (`_agentic_report_prompt`, traveling inside the queued request); this
        method never builds a prompt itself and never touches the structured
        path — only the analyst node has the cloud-LLM credentials this
        needs.

        Unlike `report()`, the commit is not this method's job: on success
        the report page this leaves in the working tree, the claimed
        request's deletion, and a telemetry event file are one commit made by
        `queue.complete`; on a terminal failure the wiki edits are rolled
        back and `queue.fail_request` requeues or fails the request — both
        carry `request['run_id']` (not a fresh one) so the eventual
        `.state/agent_runs.jsonl` line correlates with the on-prem
        placeholder's earlier commit."""
        from .queue import complete, fail_request
        run_id = request["run_id"]
        day, suffix = request["day"], request["suffix"]
        report_rel = f"reports/{day}{suffix}.md"
        push = bool(self.cfg.report.get("push"))
        tele: dict = {}
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        base = transaction.head(self.wiki)

        def validate(result: dict) -> list[str]:
            if isinstance(result, dict):
                result.setdefault("notable", True)
            proposal = self._propose(base, "report")
            problems = (self._validate(result, "report", proposal.paths)
                        + self._incident_problems(proposal, base)
                        + self._blocked(proposal))
            if not (self.wiki / report_rel).exists():
                problems.append(f"report file missing: {report_rel}")
            return problems

        result: dict | None = None

        def failed(exc: BaseException, rolled_back: bool) -> None:
            fields = self._telemetry("report", run_id, tele, model_tier="strong",
                                     mode="agentic",
                                     prompt=request["prompt"], result=result,
                                     validation_ok=False,
                                     rolled_back=rolled_back,
                                     lint_findings=getattr(exc, "lint_findings",
                                                           0))
            fail_request(self.wiki, request, categorize(exc),
                         telemetry_record=fields, push=push, lock=self.lock)

        with self._stage(base, tele, failed):
            try:
                result = self._run_agent_with_retry(
                    self.adapter, request["prompt"], self._model(True),
                    self.timeout, provider=self._provider(), tele=tele,
                    validate=validate)
            except NoResultError as e:
                result = self._synthesize_result("report", None, True, e)
                if result is None:
                    raise
            result.setdefault("notable", True)
            proposal = self._propose(base, f"report: {day}{suffix} (analyst)")
            lint = self._blocked(proposal)
            problems = (self._validate(result, "report", proposal.paths)
                        + self._incident_problems(proposal, base) + lint)
            if not (self.wiki / report_rel).exists():
                problems.append(f"report file missing: {report_rel}")
            if problems:
                raise _rejected(problems, len(lint))
        fields = self._telemetry("report", run_id, tele, model_tier="strong",
                                 mode="agentic", prompt=request["prompt"],
                                 result=result, validation_ok=True,
                                 rolled_back=False)
        complete(self.wiki, request, fields, push=push, lock=self.lock)
        return result

    def lint(self, run_id: str | None = None) -> dict:
        run_id = self._run_id(run_id)
        prompt = (
            "Task: lint.\n"
            "Follow the `lint` workflow in AGENTS.md: fix mechanical issues, "
            "flag judgment calls.\n"
            'Write the result JSON to .agent-result.json with "task": "lint".'
        )
        tele: dict = {}
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        base = transaction.head(self.wiki)

        def validate(result: dict) -> list[str]:
            proposal = self._propose(base, "lint")
            return (self._validate(result, "lint", proposal.paths)
                    + self._incident_problems(proposal, base)
                    + self._blocked(proposal))

        result: dict | None = None

        def failed(exc: BaseException, rolled_back: bool) -> None:
            self._telemetry("lint", run_id, tele, model_tier="cheap",
                            mode="agentic", prompt=prompt, result=result,
                            validation_ok=False, rolled_back=rolled_back,
                            lint_findings=getattr(exc, "lint_findings", 0))

        with self._stage(base, tele, failed):
            try:
                result = self._run_agent_with_retry(
                    self.adapter, prompt, self._model(False), self.timeout,
                    provider=self._provider(), tele=tele, validate=validate)
            except NoResultError as e:
                result = self._synthesize_result("lint", None, False, e)
                if result is None:
                    raise
            proposal = self._propose(
                base, f"lint — {result.get('summary', '')[:100]}", run_id)
            if problems := (self._validate(result, "lint", proposal.paths)
                            + self._incident_problems(proposal, base)):
                raise _rejected(problems)
            match self._publish(proposal):
                case Committed() | NothingToDo():
                    pass
                case LintBlocked(findings=findings):
                    raise _lint_blocked(findings)
                case BaseMoved() | TreeDirty() as refused:
                    raise self._refused(proposal, base, refused)
        self._telemetry("lint", run_id, tele, model_tier="cheap", mode="agentic",
                        prompt=prompt, result=result, validation_ok=True,
                        rolled_back=False)
        return result

    def _structured_research(self, pages: list[str], tele: dict, run_id: str,
                             researched: list[str]) -> dict:
        """Structured mode for research (DESIGN.md 2d): no web-capable agent
        at all. Deterministic code picks the one approved+fetchable source
        and fetches each page's Oracle error-help URL itself; a cheap local
        text call (research_structured.propose_research) only turns the
        fetched text into `{cause, action}` JSON, and apply_research owns
        every file edit. A page whose fetch fails is left untouched and
        recorded in `flags` instead of costing the whole run — the other
        pages still get researched and committed.

        Every page is its own transaction: its own `base`, its own proposal,
        the stage's own `_validate`/`_research_problems` rails, its own
        publish. The run therefore commits as it goes, one commit per page
        with that page's log.md line inside it. A batch that published once
        at the end left a killed run's whole finished prefix sitting in the
        working tree. A 45-page run killed at page 40 left 40 researched
        pages plus log.md uncommitted, which `dbwiki health` reads as a dirty
        wiki and every later stage then refuses until an operator commits by
        hand. Committing per page bounds that debris to the single page in
        flight, and the loop restores even that on its way out.

        A page the lint blocks is restored to its own base and flagged, the
        way an unfetchable one is, so one bad page costs its own commit and
        nothing else. A moved base or a dirty tree still stops the run: those
        say the wiki changed under the lock, which no later page can fix.

        `researched` is the caller's list, appended to as each page lands,
        so a run that raises still tells `research` how much of the wiki it
        published. The page in flight is restored on the way out of a
        `BaseException`, not an `Exception`: the failure this method exists
        for is an operator's Ctrl-C, which arrives as KeyboardInterrupt, and
        transaction.commit catches the same width one layer down.

        Unlike _structured_ingest/_structured_report (one proposal, one
        writer call), this loops over possibly many pages, so the result
        dict is assembled here rather than handed back by a single writer;
        `tele` sums each page's duration/usage crudely, since telemetry is
        never precise enough to be worth more than that for a bounded,
        low-volume workload. Cost is the exception: it sums to a number only
        when every call that returned usage priced itself, because a sum over
        a subset of the window would read as the window's total. Otherwise
        the key carries `harness.UNKNOWN`, as a single unpriced call does."""
        from .research_structured import (apply_research, build_research_prompt,
                                          docs_url, fetchable_source, fetch_page,
                                          propose_research)
        source = fetchable_source(self.wiki)
        if source is None:
            raise RuntimeError(
                "research.mode: structured needs an approved source page "
                "with fetchable: true and docs.oracle.com among its domains "
                "(see sources/*.md) — refusing rather than fetching from "
                "nowhere")
        slug, _domains = source
        today = dt.date.today()
        flags: list[str] = []
        unfetchable = lint_blocked = calls = 0
        duration = 0.0
        in_tok = out_tok = cost = 0.0
        have_usage = False
        cost_known = True
        for rel in pages:
            code = Path(rel).stem
            url = docs_url(code)
            text = fetch_page(url)
            if text is None:
                flags.append(f"no fetchable reference for {code}")
                unfetchable += 1
                continue
            base = transaction.head(self.wiki)
            try:
                call_tele: dict = {}
                answer = propose_research(build_research_prompt(code, text),
                                          self.cfg, telemetry=call_tele)
                calls += 1
                if isinstance(call_tele.get("duration_s"), (int, float)):
                    duration += call_tele["duration_s"]
                tele.setdefault("adapter", call_tele.get("adapter"))
                tele.setdefault("model", call_tele.get("model"))
                usage = call_tele.get("usage")
                if isinstance(usage, dict):
                    have_usage = True
                    if isinstance(usage.get("input_tokens"), (int, float)):
                        in_tok += usage["input_tokens"]
                    if isinstance(usage.get("output_tokens"), (int, float)):
                        out_tok += usage["output_tokens"]
                    if isinstance(usage.get("cost_usd"), (int, float)):
                        cost += usage["cost_usd"]
                    else:
                        cost_known = False
                apply_research(self.wiki, rel, answer, url, slug, today)
                page_result = {"task": "research",
                               "summary": f"structured research: {rel}",
                               "pages_touched": [rel, "log.md"]}
                proposal = self._propose(
                    base, f"research — structured research: {rel}", run_id)
                problems = (self._validate(page_result, "research",
                                           proposal.paths)
                            + self._research_problems())
                if problems:
                    raise ValidationError("; ".join(problems))
                match self._publish(proposal):
                    case Committed() | NothingToDo():
                        researched.append(rel)
                    case LintBlocked(findings=findings):
                        self._rollback(base)
                        flags += [f"lint blocked {code}: {f.rule}: {f.message}"
                                  for f in findings]
                        lint_blocked += 1
                    case BaseMoved() | TreeDirty() as refused:
                        raise self._refused(proposal, base, refused)
            except BaseException:
                self._rollback(base)
                raise
        tele.update(
            duration_s=round(duration, 3) if calls else None,
            exit_code=0, timed_out=False,
            usage=({"input_tokens": int(in_tok), "output_tokens": int(out_tok),
                    "cost_usd": round(cost, 6) if cost_known else UNKNOWN}
                   if have_usage else UNKNOWN))
        touched = researched + (["log.md"] if researched else [])
        summary = f"structured research: {len(researched)} page(s) researched"
        if unfetchable:
            summary += f", {unfetchable} unfetchable"
        if lint_blocked:
            summary += f", {lint_blocked} lint blocked"
        return {"task": "research", "summary": summary[:200],
                "pages_touched": touched, "flags": flags}

    def _exchange_root(self) -> Path:
        """`research.exchange.path`: a clone of the exchange repo, resolved
        against the project root. Refuses to run offload against nothing."""
        from .exchange import is_exchange
        rcfg = self.cfg.research
        xcfg = rcfg.get("exchange") or {}
        path = xcfg.get("path")
        if not path:
            raise RuntimeError("research.mode: offload needs research.exchange.path "
                               "(a clone of the exchange repo)")
        root = Path(path)
        if not root.is_absolute():
            root = Path(self.cfg.root) / root
        if not is_exchange(root):
            raise RuntimeError(f"research.exchange.path {root} is not a git clone "
                               f"of the exchange repo")
        return root

    def _fold_exchange(self, xroot: Path, *, push: bool, today: dt.date,
                       flags: list[str]) -> list[str]:
        """Apply every result the researcher pushed: de-map pseudonyms with the
        request's on-prem mapping, validate, write through the deterministic
        writer, run the research rails + lint, commit per result with the
        request's Run-ID. Anything unapplicable goes to the exchange's
        failed/ with a reason (never silently dropped). Returns the wiki
        paths written."""
        from . import exchange as ex
        from .research_offload import (ResultRejected, clean_telemetry,
                                       fold_result)
        touched: list[str] = []
        files = ex.result_files(xroot)
        if not files:
            return touched
        known = known_agent_event_ids(self.cfg.state_dir)
        for path in files:
            base = transaction.head(self.wiki)
            try:
                res = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                ex.reject_result(xroot, path, f"unreadable result: {exc}")
                flags.append(f"exchange: rejected {path.name}: unreadable")
                continue
            # one result's failure (bad ids, no mapping, a writer error) is
            # that result's rejection, never the whole fold's (issue 09)
            try:
                rel, subject, more = fold_result(self.wiki, self.cfg.state_dir, res, today)
            except ResultRejected as exc:
                transaction.restore(self.wiki, transaction.stray_paths(self.wiki), base)
                ex.reject_result(xroot, path, str(exc))
                flags.append(f"exchange: rejected {path.name}: {str(exc)[:120]}")
                continue
            flags += more
            run_id = str(res.get("run_id"))
            proposal = self._propose(base, subject, run_id)
            problems = self._research_problems()
            if not problems:
                match self._publish(proposal):
                    case Committed() | NothingToDo():
                        pass
                    case LintBlocked(findings=findings):
                        problems = [f"{f.file}: {f.rule}: {f.message}"
                                    for f in findings]
                    case BaseMoved() | TreeDirty() as refused:
                        raise self._refused(proposal, base, refused)
            if problems:
                transaction.restore(self.wiki, proposal.paths, base)
                ex.reject_result(xroot, path, "; ".join(problems))
                flags.append(f"exchange: rejected {path.name}: {problems[0][:120]}")
                continue
            touched += [rel, "log.md"]
            # self-reported by the researcher: schema-checked (issue 10)
            tele = clean_telemetry(res.get("telemetry"))
            if tele:
                event_id = str(tele.get("event_id") or f"{run_id}-{path.stem}")
                if event_id not in known:
                    append_agent_run_event(self.cfg.state_dir, {
                        **tele, "event_id": event_id, "run_id": run_id,
                        "task": "research", "mode": "offload", "at": _now()})
                    known.add(event_id)
            ex.consume_result(xroot, path)
        ex.commit_batch(xroot, f"exchange: fold {len(files)} result(s)", "fold", push=push)
        return sorted(set(touched))

    def _offload_research(self, pages: list[str], review_sources: list[str],
                          run_id: str, tele: dict) -> dict:
        """`research.mode: offload` (ADR-0002): no agent runs here. Fold in
        results the researcher already pushed, then build, redact, leak-check
        and enqueue a request per page / per due source. A leak refuses that
        one request (recorded in .state/redaction_leaks.jsonl and the flags);
        the others still go out. The wiki is only ever written by the fold-in
        path — this method's own commit is the exchange's."""
        from . import exchange as ex
        from .incidents import by_error_code, load_incidents
        from .redact import RedactionLeak, build_vocabulary
        from .research_offload import (build_research_request,
                                       build_source_review_request, record_leak)
        rcfg = self.cfg.research
        push = bool((rcfg.get("exchange") or {}).get("push", True))
        xroot = self._exchange_root()
        today = dt.date.today()
        flags: list[str] = []
        t0 = time.monotonic()
        if push:
            ex.pull(xroot)
        # a dead researcher's claim goes back to pending/ from this side too:
        # a researcher that never claims again would otherwise leave it
        # claimed, and so in flight, for good
        if reclaimed := ex.reclaim_stale(xroot, push=push,
                                         stale_hours=exchange_stale_hours(self.cfg)):
            flags.append(f"exchange: reclaimed {len(reclaimed)} stale claim(s)")
        touched = self._fold_exchange(xroot, push=push, today=today, flags=flags)
        vocab = build_vocabulary(self.cfg, self.wiki, self.cfg.state_dir)
        incident_index = by_error_code(load_incidents(self.wiki))
        # a page already in flight (pending or claimed) is not re-enqueued:
        # candidates keep coming back until a result folds in, and re-issuing
        # the request every run would only churn mappings and supersedes
        in_flight = {(r.get("kind"), ex.request_key(r))
                     for r in ex.pending_requests(xroot) + ex.claimed_requests(xroot)}
        enqueued = skipped = 0
        leaks = 0
        folded_now = set(touched)
        for rel in pages:
            page = self.wiki / rel
            # candidates were picked before this run's fold-in: a page whose
            # result just landed is researched now and must not go out again
            if ("research", page.stem) in in_flight or rel in folded_now:
                skipped += 1
                continue
            try:
                req, red = build_research_request(self.cfg, self.wiki, page, run_id,
                                                  today=today, vocab=vocab,
                                                  incident_index=incident_index,
                                                  state_dir=self.cfg.state_dir)
            except RedactionLeak as exc:
                leaks += 1
                record_leak(self.cfg.state_dir, run_id, page.stem, exc.hits)
                flags.append(f"redaction-leak: {rel} not enqueued")
                continue
            red.save(self.cfg.state_dir)
            ex.enqueue(xroot, req, commit=False)
            enqueued += 1
        for rel in review_sources:
            if ("source-review", Path(rel).stem) in in_flight:
                skipped += 1
                continue
            req = build_source_review_request(self.wiki, self.wiki / rel, run_id,
                                              vocab=vocab)
            ex.enqueue(xroot, req, commit=False)
            enqueued += 1
        if enqueued:
            ex.commit_batch(xroot, f"exchange: enqueue {enqueued} request(s)", run_id, push=push)
        tele.update(adapter="exchange", model=None, duration_s=round(time.monotonic() - t0, 3),
                    exit_code=0, timed_out=False, usage="unknown")
        summary = (f"offload research: {enqueued} request(s) enqueued, "
                   f"{len([t for t in touched if t != 'log.md'])} result(s) folded")
        if skipped:
            summary += f", {skipped} skipped (in flight or just folded)"
        if leaks:
            summary += f", {leaks} refused (redaction leak)"
        return {"task": "research", "summary": summary[:200],
                "pages_touched": touched, "flags": flags}

    def research(self, pages: list[str], review_sources: list[str] | None = None,
                 dry_run: bool = False, run_id: str | None = None) -> dict:
        """Look up external cause/fix knowledge for error pages and re-review
        source pages. The agentic path needs a web-capable adapter, hence the
        per-task adapter/model override in config.

        `research.mode: structured` (DESIGN.md 2d) skips the agent entirely
        for error-page lookups — see _structured_research. It also publishes
        for itself, one commit per page inside its own loop, so it returns
        through its own branch and the proposal/validate/publish tail below
        belongs to the agentic path alone. That branch owns no rollback
        either: the loop restores the page it was on, and `_rollback(base)`
        here would checkout a run-start revision that per-page commits have
        already moved past, reverting pages the run published. Its
        `rolled_back` reports whether anything landed, because a run that
        died at page 40 of 45 did not roll back, it published 39 commits.
        Source-page review
        judges whether a live site changed enough to go stale, which is
        exactly the browsing structured mode has none of, so any
        `review_sources` workload always takes the whole call agentic, same
        as ingest/report falling back to agentic for what structured mode
        cannot cover."""
        run_id = self._run_id(run_id)
        review_sources = review_sources or []
        rcfg = self.cfg.research
        offload = rcfg.get("mode") == "offload"
        structured = rcfg.get("mode") == "structured" and bool(pages) \
            and not review_sources
        if offload:
            if dry_run:
                print("Task: research (offload) — would fold exchange results, "
                      "then enqueue:\n" + "\n".join(f"- {p}" for p in pages + review_sources)
                      + "\n(use `dbwiki redact` to see the exact request JSON)")
                return {}
            tele: dict = {}
            if stray := transaction.stray_paths(self.wiki):
                raise _dirty_tree(stray)
            self._commit_digests()
            base = transaction.head(self.wiki)
            try:
                result = self._offload_research(pages, review_sources, run_id, tele)
            except Exception:
                self._rollback(base)
                self._telemetry("research", run_id, tele, model_tier="cheap",
                                mode="offload", validation_ok=False, rolled_back=True)
                raise
            self._telemetry("research", run_id, tele, model_tier="cheap", mode="offload",
                            result=result, validation_ok=True, rolled_back=False)
            return result
        tier = "cheap"
        # structured research builds its per-page prompts inside
        # _structured_research, so the run as a whole has none
        prompt: str | None = None
        if not structured:
            adapter = rcfg.get("adapter") or self.adapter
            model = rcfg.get("model") or self._model(False, adapter)
            tier = self._tier_of(model, adapter)
            page_list = "\n".join(f"- {p}" for p in pages) or "- (none)"
            source_list = "\n".join(f"- {p}" for p in review_sources) or "- (none)"
            prompt = (
                f"Task: research.\n"
                f"Error pages to research:\n{page_list}\n"
                f"Source pages due for review:\n{source_list}\n"
                f"Follow the `research` workflow in AGENTS.md exactly. External "
                f"knowledge goes only into the `## Reference` section — never touch "
                f"Occurrences or Resolution history, and never present an external "
                f"claim as an observed fact.\n"
                f"Cite only sources under sources/ with status: approved; do not "
                f"create new source pages (propose them in the result JSON flags).\n"
                f'Write the result JSON to .agent-result.json with "task": "research".'
            )
        if dry_run:
            if structured:
                page_list = "\n".join(f"- {p}" for p in pages) or "- (none)"
                print(f"Task: research (structured).\n"
                     f"Error pages to research:\n{page_list}\n")
            else:
                print(prompt)
            return {}
        tele: dict = {}
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        base = transaction.head(self.wiki)

        def block(**kw) -> dict:
            return self._telemetry("research", run_id, tele, model_tier=tier,
                                   mode="structured" if structured
                                   else "agentic", prompt=prompt, **kw)

        def validate(result: dict) -> list[str]:
            proposal = self._propose(base, "research")
            return (self._validate(result, "research", proposal.paths)
                    + self._research_problems() + self._blocked(proposal))

        if structured:
            researched: list[str] = []
            try:
                result = self._structured_research(pages, tele, run_id,
                                                   researched)
            except BaseException:
                block(validation_ok=False, rolled_back=not researched)
                raise
            block(result=result, validation_ok=True, rolled_back=False)
            return result

        result = None

        def failed(exc: BaseException, rolled_back: bool) -> None:
            block(result=result, validation_ok=False, rolled_back=rolled_back,
                  lint_findings=getattr(exc, "lint_findings", 0))

        with self._stage(base, tele, failed):
            try:
                result = self._run_agent_with_retry(
                    adapter, prompt, model, self.timeout, web=True,
                    provider=self._provider(adapter), tele=tele,
                    validate=validate)
            except NoResultError as e:
                result = self._synthesize_result("research", None, False, e)
                if result is None:
                    raise
            proposal = self._propose(
                base, f"research — {result.get('summary', '')[:100]}", run_id)
            problems = (self._validate(result, "research", proposal.paths)
                        + self._research_problems())
            if problems:
                raise _rejected(problems)
            match self._publish(proposal):
                case Committed() | NothingToDo():
                    pass
                case LintBlocked(findings=findings):
                    raise _lint_blocked(findings)
                case BaseMoved() | TreeDirty() as refused:
                    raise self._refused(proposal, base, refused)
        block(result=result, validation_ok=True, rolled_back=False)
        return result

    def caveats(self, pages: list[str], run_id: str | None = None) -> dict:
        """The practitioner-caveat pass (research_caveats.py): for each
        already-researched error page, ask a web-capable claude call for up
        to three notes that add what Oracle's own text does not say, and
        write them into `## Reference` through the deterministic writer.

        What leaves the box is the error code, the Oracle cause and action
        text the page already publishes, and the approved source domains. The
        model runs with WebSearch and WebFetch and no file access, in a
        throwaway directory (harness.run_web_text), so it reads no wiki page
        and writes none; every byte of the page edit is written here.

        Every page is its own transaction, the way _structured_research runs
        its loop: its own `base`, its own proposal, the stage's
        `_validate`/`_research_problems` rails, its own commit carrying that
        page's log.md line. A page whose answer is unusable twice costs its
        own page and nothing else. The model is being asked to search the
        open web, so an unusable answer is an ordinary outcome, not a reason
        to abandon the pages behind it: that page is restored to its own base
        and recorded in `flags`, and the loop moves on. A page the lint
        blocks is treated the same way. A moved base or a dirty tree still
        stops the run, because those say the wiki changed under the lock,
        which no later page can fix.

        The page in flight is restored on the way out of a BaseException, not
        an Exception: the kill this matters for is an operator's Ctrl-C.

        Telemetry is recorded once per run, summing each call's duration and
        usage the way _structured_research does, cost included only when
        every call that reported usage priced itself."""
        from .readmodel import parse_research
        from .redact import RedactionLeak
        from .research_caveats import (apply_caveats,
                                       approved_fetchable_sources,
                                       outbound_caveats_prompt, propose_caveats)
        from .research_offload import record_leak
        from .structured import ProposalError
        run_id = self._run_id(run_id)
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        sources = approved_fetchable_sources(self.wiki)
        if not sources:
            raise RuntimeError(
                "research --caveats needs a source page with status: approved "
                "and fetchable: true (see sources/*.md) — refusing rather "
                "than searching the whole web")
        today = dt.date.today()
        tele: dict = {}
        published: list[str] = []
        flags: list[str] = []
        notes = failed = calls = 0
        duration = 0.0
        in_tok = out_tok = cost = 0.0
        have_usage = False
        cost_known = True

        def record(**kw) -> None:
            tele.update(
                duration_s=round(duration, 3) if calls else None,
                exit_code=0, timed_out=False,
                usage=({"input_tokens": int(in_tok),
                        "output_tokens": int(out_tok),
                        "cost_usd": round(cost, 6) if cost_known else UNKNOWN}
                       if have_usage else UNKNOWN))
            self._telemetry("research", run_id, tele, mode="caveats",
                            model_tier="cheap", **kw)

        try:
            for rel in pages:
                code = Path(rel).stem
                page = self.wiki / rel
                found = (parse_research(code, rel, page.read_text())
                         if page.exists() else None)
                if found is None:
                    flags.append(f"no research on {rel}: nothing to annotate")
                    failed += 1
                    continue
                base = transaction.head(self.wiki)
                try:
                    call_tele: dict = {}
                    # redacted + leak-checked: offload may have put real
                    # names into this Reference (issue 07)
                    prompt, red = outbound_caveats_prompt(
                        self.cfg, code, found.cause, found.action, sources)
                    answer = red.demap_obj(propose_caveats(
                        prompt, self.cfg, telemetry=call_tele))
                    calls += 1
                    if isinstance(call_tele.get("duration_s"), (int, float)):
                        duration += call_tele["duration_s"]
                    tele.setdefault("adapter", call_tele.get("adapter"))
                    tele.setdefault("model", call_tele.get("model"))
                    usage = call_tele.get("usage")
                    if isinstance(usage, dict):
                        have_usage = True
                        if isinstance(usage.get("input_tokens"), (int, float)):
                            in_tok += usage["input_tokens"]
                        if isinstance(usage.get("output_tokens"), (int, float)):
                            out_tok += usage["output_tokens"]
                        if isinstance(usage.get("cost_usd"), (int, float)):
                            cost += usage["cost_usd"]
                        else:
                            cost_known = False
                    apply_caveats(self.wiki, rel, answer, today)
                    page_result = {"task": "research",
                                   "summary": f"caveats: {rel}",
                                   "pages_touched": [rel, "log.md"]}
                    proposal = self._propose(
                        base, f"research — caveats: {rel}", run_id)
                    problems = (self._validate(page_result, "research",
                                               proposal.paths)
                                + self._research_problems())
                    if problems:
                        raise ValidationError("; ".join(problems))
                    match self._publish(proposal):
                        case Committed() | NothingToDo():
                            published.append(rel)
                            notes += len(answer.get("notes") or [])
                        case LintBlocked(findings=findings):
                            self._rollback(base)
                            flags += [f"lint blocked {code}: {f.rule}: "
                                      f"{f.message}" for f in findings]
                            failed += 1
                        case BaseMoved() | TreeDirty() as refused:
                            raise self._refused(proposal, base, refused)
                except RedactionLeak as e:
                    # fail closed: nothing sent; the hits stay on-prem
                    record_leak(self.cfg.state_dir, run_id, code, e.hits)
                    flags.append(f"redaction-leak: {rel} not sent")
                    failed += 1
                except (HarnessError, ProposalError, ValidationError) as e:
                    transaction.restore(self.wiki, [rel, "log.md"], base)
                    flags.append(f"caveats failed for {code}: {e}"[:200])
                    failed += 1
                except BaseException:
                    transaction.restore(self.wiki, [rel, "log.md"], base)
                    raise
        except BaseException:
            record(validation_ok=False, rolled_back=not published)
            raise
        summary = f"caveats: {len(published)} page(s), {notes} note(s)"
        if failed:
            summary += f", {failed} failed"
        result = {"task": "research", "summary": summary[:200],
                  "pages_touched": published + (["log.md"] if published else []),
                  "flags": flags}
        record(result=result, validation_ok=True, rolled_back=False)
        return result

    def past_fixes(self, dry_run: bool = False,
                   run_id: str | None = None) -> dict:
        """The past-fixes pass (past_fixes.py): regenerate the `## Past fixes`
        table on every error page from the incident corpus.

        The `caveats` loop with the model call taken out, and it is the model
        call that made most of that machinery necessary. What is left is worth
        keeping: every page is its own transaction with its own `base`, its
        own proposal, the stage's `_validate`/`_research_problems` rails, and
        its own commit carrying that page's `log.md` line, so a page the lint
        blocks costs its own page and nothing else.

        No selection and no `limit`, because there is nothing to spend. The
        stage walks every error page and writes only the ones whose bytes
        actually move; a page whose section is already correct costs one read
        and never reaches git at all. That is what makes a daily cron line
        reasonable: a fix recorded today is on the error page by tomorrow.

        Telemetry records `mode="history"` with `attempts=0` — the honest
        reading of a stage that asks nothing of a model — so `dbwiki stats`
        shows the run happened without pricing a call that never occurred."""
        from . import past_fixes as history
        run_id = self._run_id(run_id)
        hcfg = self.cfg.research.get("history") or {}
        held_after_days = int(hcfg.get("held_after_days",
                                       history.HELD_AFTER_DAYS))
        today = dt.date.today()
        histories = history.gather(self.wiki, today, held_after_days)
        if dry_run:
            for code in sorted(histories):
                print(f"errors/{code}.md:\n{history.render(histories[code])}\n")
            if not histories:
                print("no incident carries a fix for any error page")
            return {}
        if stray := transaction.stray_paths(self.wiki):
            raise _dirty_tree(stray)
        self._commit_digests()
        tele: dict = {"attempts": 0, "duration_s": None, "exit_code": 0,
                      "timed_out": False, "usage": UNKNOWN}
        published: list[str] = []
        flags: list[str] = []
        written = failed = 0

        def record(**kw) -> None:
            self._telemetry("research", run_id, tele, mode="history",
                            model_tier="none", **kw)

        try:
            for page in sorted((self.wiki / "errors").glob("*.md")):
                rel = f"errors/{page.name}"
                code = page.stem
                base = transaction.head(self.wiki)
                try:
                    if not history.apply(self.wiki, rel, histories.get(code),
                                         today):
                        continue
                    page_result = {"task": "research",
                                   "summary": f"past fixes: {rel}",
                                   "pages_touched": [rel, "log.md"]}
                    proposal = self._propose(
                        base, f"research — past fixes: {rel}", run_id)
                    problems = (self._validate(page_result, "research",
                                               proposal.paths)
                                + self._research_problems())
                    if problems:
                        raise ValidationError("; ".join(problems))
                    match self._publish(proposal):
                        case Committed() | NothingToDo():
                            published.append(rel)
                            found = histories.get(code)
                            written += len(found.fixes) if found else 0
                        case LintBlocked(findings=findings):
                            self._rollback(base)
                            flags += [f"lint blocked {code}: {f.rule}: "
                                      f"{f.message}" for f in findings]
                            failed += 1
                        case BaseMoved() | TreeDirty() as refused:
                            raise self._refused(proposal, base, refused)
                except ValidationError as e:
                    transaction.restore(self.wiki, [rel, "log.md"], base)
                    flags.append(f"past fixes failed for {code}: {e}"[:200])
                    failed += 1
                except BaseException:
                    transaction.restore(self.wiki, [rel, "log.md"], base)
                    raise
        except BaseException:
            record(validation_ok=False, rolled_back=not published)
            raise
        summary = f"past fixes: {len(published)} page(s), {written} fix(es)"
        if failed:
            summary += f", {failed} failed"
        result = {"task": "research", "summary": summary[:200],
                  "pages_touched": published + (["log.md"] if published else []),
                  "flags": flags}
        record(result=result, validation_ok=True, rolled_back=False)
        return result
