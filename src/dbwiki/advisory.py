"""Bounded advisory tools: what a tool may read, what a click may spend, and
the one runner the portal reaches a model through.

Two rules shape the module.

A tool is a registry row, not a function. A `ToolSpec` names the evidence
kinds it may read; `pack` reaches evidence only through `SOURCES`, the one
dispatch table, and stamps each section's kind from the table key rather than
from the reader. Over-reading is therefore unrepresentable rather than
discouraged, and `{section.kind for section in pack.sections} <= set(
spec.sources)` is a fact about the code with a test that says so. Every cap is
named in `SOURCES` and nowhere else.

The agent narrates; deterministic code selects. `harness.run_text` runs pi
with `--no-tools --no-context-files`, so the model cannot read or write
anything: it answers over the material this module packed. Nothing here takes
the wiki lock, writes the wiki, or reads `transaction.head` — an advisory run
has no authority over anything but its own file under `.state/advisory/`.

The text/manifest split is at the type. `PackSection.text` is the prompt and
never leaves this module; `EvidencePack.manifest` — kind, path, chars,
truncated — is the only view `wire` can encode, so a prompt body on the wire
is unrepresentable rather than merely absent.

This deliberately reopens the workbench's "a proposal nobody accepted is not
a run", narrowly: a ceiling needs a spend history, so `.state/
advisory_runs.jsonl` records identifiers, counts, duration and usage. It
carries no prompt and no answer text — it is a cost report, not a report on
the operator's second thoughts. The answer lives only in the run file, kept by
count.
"""

import datetime as dt
import hashlib
import json
import string
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from . import harness, health, monitoring, state, structured
from .incidents import Incident, Status
from .portal import identity
from .readmodel import Snapshot
from .structured import (MAX_MATERIAL_DIGEST, MAX_MATERIAL_ERROR_PAGE,
                         MAX_MATERIAL_ERROR_PAGES, MAX_MATERIAL_INCIDENT)
from .transaction import Tree

SCHEMA_VERSION = 1

RUNS_DIR = "advisory"

DEFAULT_RETAIN = 200

#: Concurrent model calls this process will hold at once. Two adapter
#: subprocesses contend for one resident model on the live box, so the
#: default is deliberately small and configurable.
DEFAULT_MAX_CONCURRENT = 2

DEFAULT_TIMEOUT_S = 120

DEFAULT_WINDOW_H = 24
DEFAULT_MAX_RUNS = 20

MAX_ERROR_CHARS = 300


class Tier(StrEnum):
    """Which `agents.pi` model tier a tool's call resolves to. There is no
    adapter knob: `harness.run_text` is pi-hardwired, so tier and provider are
    the only model choices a row has."""

    CHEAP = "cheap"
    STRONG = "strong"


class SourceKind(StrEnum):
    """The evidence kinds a tool may name. A kind is a *sort* of evidence, not
    a path: which paths one yields is `SOURCES`' reader's business, and the
    caps that bound it are its `PackRule`'s."""

    INCIDENT = "incident"
    ERROR_PAGE = "error_page"
    DIGEST = "digest"
    CLOSURE = "closure"
    ACTIONS = "actions"
    NEIGHBOURS = "neighbours"
    OCCURRENCES = "occurrences"
    JOURNAL = "journal"


@dataclass(frozen=True)
class Target:
    """What a tool is being asked about, and everything a reader may look at.

    Frozen and read-only: the tree is pinned at `revision`, the snapshot is
    the read model at that revision, and `facts` is the monitoring file as
    `monitoring.read_facts` handed it over. A reader that wants something not
    on this value cannot have it, which is the other half of `SOURCES`'
    boundedness.

    `key` is the target's identity inside `run_id` — the incident slug for the
    one kind 3.1 ships. `subject` is the incident itself; the classmethod owns
    the name `incident` so a caller reads `Target.incident(...)`.

    `question` is the operator's free text, `""` for a row that does not take
    one. It rides here rather than on the runner's signature because it is
    something the tool is being asked *about*, exactly like the subject: that
    makes it one more value `words()` fills, so a row that wants it names it
    in its instruction and `_check_tools` keeps checking the placeholder.
    """

    kind: str
    key: str
    revision: str
    subject: Incident
    tree: Tree
    snapshot: Snapshot | None
    facts: Mapping[str, object] | None
    question: str = ""

    @classmethod
    def incident(cls, subject: Incident, *, tree: Tree, revision: str,
                 snapshot: Snapshot | None = None,
                 facts: Mapping[str, object] | None = None,
                 question: str = "") -> "Target":
        """The one target kind 3.1 ships."""
        return cls(kind="incident", key=subject.slug, revision=revision,
                   subject=subject, tree=tree, snapshot=snapshot, facts=facts,
                   question=question)

    def words(self) -> dict[str, str]:
        """The values a `ToolSpec.instruction` may name: `slug`, `db`,
        `title`, `status`, `question`. Checked against every row at import, so
        an instruction cannot name a placeholder no target fills."""
        return {"slug": self.subject.slug, "db": self.subject.db,
                "title": self.subject.title,
                "status": str(self.subject.status),
                "question": self.question}


@dataclass(frozen=True)
class PackRule:
    """How much of one evidence kind a pack may carry, and where it comes
    from.

    `read` yields `(path, body)` candidates in pack order; `pack` applies
    `max_items` and `max_chars` and stamps the kind. A reader never decides
    how much of itself is packed, and never names its own kind, so the two
    facts a boundedness proof needs are both held here.

    `heading` heads a derived section — one with no wiki path behind it, like
    the closure case — and is unused for a section that carries a path.
    """

    max_items: int
    max_chars: int
    heading: str
    read: Callable[[Target], Iterable[tuple[str, str]]]


@dataclass(frozen=True)
class PackSection:
    """One packed section. `text` is prompt material and never crosses the
    wire: `wire` is handed `EvidencePack.manifest`, whose rows have no text
    field at all."""

    kind: SourceKind
    path: str
    heading: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class PackEntry:
    """One manifest row: what the operator is told a click would read. The
    whole wire view of a pack."""

    kind: SourceKind
    path: str
    heading: str
    chars: int
    truncated: bool


@dataclass(frozen=True)
class EvidencePack:
    """What one tool packed at one revision."""

    sections: tuple[PackSection, ...]

    @property
    def manifest(self) -> tuple[PackEntry, ...]:
        """The sections as the wire may see them."""
        return tuple(PackEntry(kind=s.kind, path=s.path, heading=s.heading,
                               chars=len(s.text), truncated=s.truncated)
                     for s in self.sections)

    @property
    def text(self) -> str:
        """The material, each section headed by its path or its rule's
        heading, in pack order — `structured._material_section`'s shape, so
        the material one prompt carries is the material every other prompt in
        this codebase carries."""
        return "\n".join(structured._material_section(s.heading, s.text)
                         for s in self.sections)

    @property
    def paths(self) -> tuple[str, ...]:
        """The wiki paths the material actually contained, in pack order:
        what an answer may cite. A derived section has no path and is not
        citable."""
        return tuple(s.path for s in self.sections if s.path)

    @property
    def chars(self) -> int:
        """Total packed characters, the number a ceiling conversation is
        actually about."""
        return sum(len(s.text) for s in self.sections)


@dataclass(frozen=True)
class Budget:
    """One row's spend ceiling. `max_cost_usd` is legal config because pi
    prices its own calls; None is "count only"."""

    window_h: int
    max_runs: int
    max_cost_usd: float | None


@dataclass(frozen=True)
class Spend:
    """What a window already cost.

    `cost_usd` sums `cost_usd` where the ledger holds a real number, and
    `measured_runs` counts the runs that carried one. A run is unmeasured
    exactly where `cost_usd` is absent — never where `usage_known` is false,
    which `health.record_agent_run` derives from token counts alone and which
    a run with a real cost can carry.
    """

    runs: int
    measured_runs: int
    cost_usd: float

    def plus_reserved(self, reservations: int) -> "Spend":
        """This window's spend as admission must see it: runs the runner has
        admitted but whose ledger line does not exist yet count as spent."""
        return replace(self, runs=self.runs + reservations)

    @property
    def unmeasured_runs(self) -> int:
        """Runs in the window whose cost nobody reported. Never zero spend:
        it is spend nobody can add up."""
        return self.runs - self.measured_runs


@dataclass(frozen=True)
class ToolSpec:
    """One advisory tool.

    `sources` is the whole read authority of the row: `pack` iterates exactly
    these kinds through `SOURCES` and has no other route to evidence.

    `instruction` is the fixed preamble, a template over `Target.words()` and
    `max_answer_chars`, ending with the line that introduces the material;
    `build_prompt` appends the pack and nothing else.

    `target_field` names the action-record field a tool's answer is written
    for (`""` for a tool that answers rather than fills).

    `max_answer_chars` is advice in the prompt, not a truncation; the operator
    edits every answer before anything is published.
    """

    id: str
    label: str
    sources: tuple[SourceKind, ...]
    instruction: str
    max_answer_chars: int
    target_field: str
    role: identity.Role
    tier: Tier
    budget: Budget
    enabled: bool
    model: str | None
    timeout_s: int


class RunStatus(StrEnum):
    """Where one run is. `refused` is a start a ceiling or a full slot
    declined, recorded so the operator has an addressable answer for a click
    that spent nothing; `interrupted` is a run whose process died, derived
    rather than written (`AdvisoryRun.as_of`)."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"
    INTERRUPTED = "interrupted"


TERMINAL = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED,
                      RunStatus.REFUSED, RunStatus.INTERRUPTED})


@dataclass(frozen=True)
class Answer:
    """What the model said and what it cost.

    `cites` is a post-hoc scan (`cited`), not a claim the model makes: the
    packed paths the answer names, in pack order, so a path the model invented
    is dropped rather than shown. `usage` is the harness's dict or the literal
    `harness.UNKNOWN`; nothing here turns an unmeasured cost into a zero.
    """

    text: str
    cites: tuple[str, ...]
    model: str | None
    usage: Mapping[str, object] | str


@dataclass(frozen=True)
class AdvisoryRun:
    """One advisory run, the whole of `.state/advisory/<run_id>.json`.

    `run_id` is `run_id_for(tool, target, at, question)` with `at` from the
    caller, exactly as a preview mints the identity of a record: a second POST
    of the same click converges on the same run instead of paying twice, which
    is what makes the page's network-failure retry free.

    `question` is the operator's free text, `""` for a row that does not take
    one. Kept on the record because it is half of what the run answered: an
    answer read back without the question that produced it is not readable.

    `evidence_revision` is the revision the pack was read at. Not `revision`,
    which would claim the answer is a projection of the wiki, and not
    `source_revision`, which is the closure's word for the revision a
    deterministic evaluator judged at.

    `boot_id` is the process that owns the run. A `queued` or `running`
    record under a foreign boot is a run whose process died, and `as_of`
    reads it as `interrupted` — a fact derived at read time, so a crash needs
    no shutdown hook to stay honest.
    """

    schema_version: int
    run_id: str
    tool: str
    target: str
    at: str
    question: str
    status: RunStatus
    boot_id: str
    started: str
    finished: str
    duration_s: float | None
    evidence_revision: str
    context: tuple[PackEntry, ...]
    answer: Answer | None
    error: str

    def as_of(self, boot_id: str) -> "AdvisoryRun":
        """This run as the process `boot_id` should read it: unchanged, or
        `interrupted` when it is still in flight under a boot that is gone.
        Idempotent — an already-interrupted record answers itself."""
        if self.status in TERMINAL or self.boot_id == boot_id:
            return self
        return replace(self, status=RunStatus.INTERRUPTED)

    def to_dict(self) -> dict:
        """The JSON body, `state.atomic_write_text`'s house shape."""
        answer = self.answer
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "tool": self.tool,
            "target": self.target,
            "at": self.at,
            "question": self.question,
            "status": str(self.status),
            "boot_id": self.boot_id,
            "started": self.started,
            "finished": self.finished,
            "duration_s": self.duration_s,
            "evidence_revision": self.evidence_revision,
            "context": [{"kind": str(e.kind), "path": e.path,
                         "heading": e.heading, "chars": e.chars,
                         "truncated": e.truncated} for e in self.context],
            "answer": None if answer is None else {
                "text": answer.text,
                "cites": list(answer.cites),
                "model": answer.model,
                "usage": (dict(answer.usage)
                          if isinstance(answer.usage, Mapping)
                          else answer.usage)},
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, obj: Mapping[str, object]) -> "AdvisoryRun":
        """The inverse. Refuses a `schema_version` newer than this module's,
        `state._check_version`'s rule."""
        run_id = str(obj.get("run_id", ""))
        state._check_version(dict(obj), Path(f"{RUNS_DIR}/{run_id}.json"),
                             SCHEMA_VERSION)
        answer = obj.get("answer")
        raw = obj.get("context")
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=run_id,
            tool=str(obj.get("tool", "")),
            target=str(obj.get("target", "")),
            at=str(obj.get("at", "")),
            question=str(obj.get("question", "")),
            status=RunStatus(str(obj.get("status", ""))),
            boot_id=str(obj.get("boot_id", "")),
            started=str(obj.get("started", "")),
            finished=str(obj.get("finished", "")),
            duration_s=obj.get("duration_s"),
            evidence_revision=str(obj.get("evidence_revision", "")),
            context=tuple(PackEntry(kind=SourceKind(str(e["kind"])),
                                    path=str(e.get("path", "")),
                                    heading=str(e.get("heading", "")),
                                    chars=int(e.get("chars", 0)),
                                    truncated=bool(e.get("truncated")))
                          for e in (raw if isinstance(raw, list) else ())),
            answer=None if not isinstance(answer, Mapping) else Answer(
                text=str(answer.get("text", "")),
                cites=tuple(str(c) for c in answer.get("cites") or ()),
                model=answer.get("model"),
                usage=answer.get("usage") or harness.UNKNOWN),
            error=str(obj.get("error", "")))


class UnknownTool(LookupError):
    """No such tool id. The portal answers 404 `unknown_tool`."""


class ToolDisabled(RuntimeError):
    """A row this deployment turned off. The portal answers 503
    `tool_disabled`."""


class Refused(RuntimeError):
    """A start that spent nothing: a ceiling, or no free slot. Carries the
    stable `reason`, the `spend` and the `ceiling` the portal renders, and the
    `run` it recorded so the refusal is addressable."""

    def __init__(self, reason: str, *, run: "AdvisoryRun",
                 spend: Spend | None = None, ceiling: Budget | None = None,
                 retry_after_s: float | None = None):
        self.reason = reason
        self.run = run
        self.spend = spend
        self.ceiling = ceiling
        self.retry_after_s = retry_after_s
        super().__init__(reason)


class _Denied(Exception):
    """What `_admit` raises from inside the lock, where there is no record to
    point at yet. `start` writes the `refused` record and turns this into the
    `Refused` a caller sees, so `Refused.run` is never None."""

    def __init__(self, reason: str, *, spend: Spend, ceiling: Budget,
                 retry_after_s: float | None = None):
        self.reason = reason
        self.spend = spend
        self.ceiling = ceiling
        self.retry_after_s = retry_after_s
        super().__init__(reason)


def _read_incident(target: Target) -> Iterable[tuple[str, str]]:
    """The incident page at the pinned revision."""
    body = target.tree.read(target.subject.path)
    if body:
        yield target.subject.path, body


def _read_error_pages(target: Target) -> Iterable[tuple[str, str]]:
    """`errors/<code>.md` for each code the incident's frontmatter names, in
    frontmatter order.

    Deliberately not the occurrence join. The frontmatter list is what an
    incident page itself claims to be about, which is the narrower and more
    accountable answer; a tool that wants the wider join asks for
    `NEIGHBOURS`, which owns it and ranks by shared codes."""
    for code in target.subject.error_codes:
        path = f"errors/{code}.md"
        body = target.tree.read(path)
        if body:
            yield path, body


def _read_digests(target: Target) -> Iterable[tuple[str, str]]:
    """The rendered digest pages the closure case cites, newest first,
    through `monitoring.closure_case(...).digests` so the per-kind
    `observed[*]` shapes stay monitoring's business, each reduced to
    `structured._digest_material`'s deltas and notable blocks. A digest with
    neither is not yielded: a section with an empty body is a path the model
    could cite for no evidence."""
    cases = () if target.facts is None else _closure(target).digests
    for path in reversed(cases):
        body = structured._digest_material(target.tree.read(path) or "")
        if body:
            yield path, body


def _closure(target: Target) -> monitoring.ClosureCase:
    return monitoring.closure_case(target.facts or {}, revision=target.revision)


def _read_closure(target: Target) -> Iterable[tuple[str, str]]:
    """The closure case as prose: the verdict, when it was evaluated, and the
    for and against lines `monitoring.ClosureCase` already derived. Derived,
    so it carries no path and is not citable — the digests it cites are."""
    if target.facts is None:
        return
    case = _closure(target)
    lines = [f"verdict: {case.verdict}",
             f"evaluated at: {case.evaluated_at or 'unknown'}"]
    if case.stale:
        lines.append("the verdict was evaluated at an earlier revision than "
                     "the one this material was read at")
    lines.append("")
    lines.append("arguing for closing:")
    lines += [f"- {line}" for line in case.supporting] or ["- (nothing)"]
    lines.append("")
    lines.append("arguing against closing:")
    lines += [f"- {line}" for line in case.against] or ["- (nothing)"]
    yield "", "\n".join(lines)


def _read_actions(target: Target) -> Iterable[tuple[str, str]]:
    """The action records on the page, newest first, one line each. Derived
    from `Incident.actions.records`, whose problems are deliberately not
    packed: a record the parser refused is not evidence."""
    records = sorted(target.subject.actions.records,
                     key=lambda r: r.at, reverse=True)
    if not records:
        return
    yield "", "\n".join(
        f"- {r.at} {r.kind} by {r.actor}, {r.outcome}, left the incident "
        f"{r.status_after}: {r.summary}" for r in records)


def _read_neighbours(target: Target) -> Iterable[tuple[str, str]]:
    """Other incidents that share an error code with this one, through the
    `## Occurrences` tables (`readmodel.Occurrence`) and never
    `Incident.error_codes`, which is empty on all 25 live incidents.

    One incident's codes are the occurrence rows recorded for its database on
    the day it was opened, plus whatever its frontmatter names; a neighbour is
    another incident whose codes intersect, ranked by shared codes then by
    recency then by slug so the order is a pure function of the snapshot. Each
    neighbour is one citable line naming its page, database, day and the
    shared codes.

    Each neighbour is its own section, headed by and citable as its page, so
    an answer that names one comes back with the link the operator wants; the
    other joins in this module aggregate several pages into one line and stay
    derived, because citing one of their sources would misattribute the rest.

    A subject the snapshot joins nothing to yields one derived section reading
    "no similar incidents found", so the tool degrades in words the operator
    can see rather than by shrinking to silence."""
    snapshot = target.snapshot
    if snapshot is None:
        return
    mine = _codes_of(snapshot, target.subject)
    found = False
    for incident in _by_shared_codes(snapshot, target.subject, mine):
        found = True
        shared = ", ".join(sorted(_codes_of(snapshot, incident) & mine))
        yield incident.path, (
            f"{incident.title} on {incident.db}, opened "
            f"{incident.opened[:10] or 'an unrecorded day'}, {incident.status}"
            f"; shares {shared}")
    if not found:
        yield "", "no similar incidents found"


def _codes_of(snapshot: Snapshot, incident: Incident) -> set[str]:
    """The error codes one incident is about: whatever its frontmatter names,
    plus every code the occurrence tables recorded for its database on the day
    it was opened. The second half is what makes the join work at all — the
    frontmatter list is empty on all 25 live incidents."""
    day = incident.opened[:10]
    codes = {o.code for o in snapshot.occurrences_of(db=incident.db)
             if o.day == day}
    return codes | set(incident.error_codes)


def _by_shared_codes(snapshot: Snapshot, subject: Incident,
                     mine: set[str]) -> list[Incident]:
    matched = [inc for inc in snapshot.incidents.values()
               if inc.path != subject.path and _codes_of(snapshot, inc) & mine]
    matched.sort(key=lambda inc: inc.slug)
    matched.sort(key=lambda inc: inc.opened, reverse=True)
    matched.sort(key=lambda inc: -len(_codes_of(snapshot, inc) & mine))
    return matched


def _read_occurrences(target: Target) -> Iterable[tuple[str, str]]:
    """The `## Occurrences` rows for this incident's database, newest day
    first, one line each: the working error-to-database-to-day join the wiki
    holds. One derived section, carrying no path, because the rows come from
    many error pages and citing one of them would misattribute the rest."""
    snapshot = target.snapshot
    if snapshot is None:
        return
    rows = sorted(snapshot.occurrences_of(db=target.subject.db),
                  key=lambda o: (o.day, o.code), reverse=True)
    if not rows:
        return
    yield "", "\n".join(
        f"- {o.day} {o.code} on {o.db} ({o.evidence})"
        + (f": {o.note}" if o.note else "") for o in rows)


def _read_journal(target: Target) -> Iterable[tuple[str, str]]:
    """The database's journal entries, newest day first, one line each,
    naming the month file each was written in.

    One derived section rather than one section per entry, because the rule's
    `max_items` is 1: a per-entry section would pack the newest entry and drop
    the rest of the journal, and a journal of one day is not the history this
    reader exists to hand over. The lines name their month files but the
    section carries no path, for `_read_occurrences`' reason — the entries
    come from several month files and citing one would misattribute the
    rest."""
    snapshot = target.snapshot
    if snapshot is None:
        return
    entries = snapshot.journals.get(target.subject.db, ())
    if not entries:
        return
    yield "", "\n".join(f"- {e.day} {e.headline} ({e.path})" for e in entries)


#: kind -> how much of it a pack may carry and where it comes from. Every cap
#: in this module is here; `pack` reads no evidence except through this table,
#: so a tool's `sources` is its whole read authority.
#:
#: The page, error-page and digest caps are `structured`'s material caps
#: verbatim, and are not this module's to choose: they are what the ingest
#: prompts already fit a database's evidence into, and a second set of numbers
#: here would be a second opinion about how much material a model can read.
SOURCES: Mapping[SourceKind, PackRule] = {
    SourceKind.INCIDENT: PackRule(
        max_items=1, max_chars=MAX_MATERIAL_INCIDENT,
        heading="the incident page", read=_read_incident),
    SourceKind.ERROR_PAGE: PackRule(
        max_items=MAX_MATERIAL_ERROR_PAGES, max_chars=MAX_MATERIAL_ERROR_PAGE,
        heading="error pages", read=_read_error_pages),
    SourceKind.DIGEST: PackRule(
        max_items=3, max_chars=MAX_MATERIAL_DIGEST,
        heading="digests", read=_read_digests),
    SourceKind.CLOSURE: PackRule(
        max_items=1, max_chars=4000,
        heading="the monitoring window, as the last tick evaluated it",
        read=_read_closure),
    SourceKind.ACTIONS: PackRule(
        max_items=1, max_chars=4000,
        heading="what the operators recorded on this page",
        read=_read_actions),
    SourceKind.NEIGHBOURS: PackRule(
        max_items=5, max_chars=400,
        heading="incidents sharing an error code, through the occurrence "
                "tables",
        read=_read_neighbours),
    SourceKind.OCCURRENCES: PackRule(
        max_items=1, max_chars=3000,
        heading="every recorded occurrence on this database",
        read=_read_occurrences),
    SourceKind.JOURNAL: PackRule(
        max_items=1, max_chars=2000,
        heading="the database journal", read=_read_journal),
}


#: The rules every instruction below repeats in its own voice, and the line
#: that introduces the material. `draft-note` spells its own copy instead of
#: reusing this one; its prompt is pinned whole by a test, and the reason for
#: that is on its row below.
_RULES = (
    "Rules:\n"
    "- State only what the Material below shows. Never claim a cause, a fix "
    "or a recovery it does not show.\n"
    "- Events stopping is not recovery: say the errors stopped, never that "
    "the problem is solved.\n"
    "- Name a path only as the Material spells it, and only when it is the "
    "evidence for the claim; never invent one.\n"
    "- No [[wikilinks]], no headings, no URLs.\n"
    "\n"
    "Material (assembled deterministically; each section is headed by the "
    "wiki path it was read from, or by what derived it):")

#: The ceiling every row ships with. A deployment tightens it per row in
#: `advisory.tools.<id>`; `max_cost_usd: null` is a count ceiling only, which
#: is the honest default while nobody has measured what a click costs here.
DEFAULT_BUDGET = Budget(window_h=DEFAULT_WINDOW_H, max_runs=DEFAULT_MAX_RUNS,
                        max_cost_usd=None)

#: Every row is checked at import by `_check_tools`. `draft-note`'s
#: instruction is pinned whole as a golden string by `tests/test_advisory.py`,
#: so rewriting its wording is a decision rather than a refactor.
TOOLS: Mapping[str, ToolSpec] = {
    "explain-incident": ToolSpec(
        id="explain-incident",
        label="Explain this incident",
        sources=(SourceKind.INCIDENT, SourceKind.ERROR_PAGE,
                 SourceKind.ACTIONS, SourceKind.CLOSURE),
        instruction=(
            "You are explaining an open operations incident to the engineer "
            "who has just picked it up: incident {slug} on {db}, {title}, "
            "currently {status}.\n"
            "\n"
            "Write at most {max_answer_chars} characters of plain prose, in "
            "two or three short paragraphs: what the page says happened, what "
            "has been done about it, and what the evidence does not settle.\n"
            "\n" + _RULES),
        max_answer_chars=1200,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.STRONG,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "ask-incident": ToolSpec(
        id="ask-incident",
        label="Interview this incident",
        sources=(SourceKind.INCIDENT, SourceKind.ERROR_PAGE,
                 SourceKind.ACTIONS, SourceKind.CLOSURE, SourceKind.DIGEST,
                 SourceKind.NEIGHBOURS),
        instruction=(
            "You are answering one question an engineer has asked about "
            "incident {slug} on {db}, {title}, currently {status}.\n"
            "\n"
            "The question is:\n"
            "{question}\n"
            "\n"
            "Write at most {max_answer_chars} characters of plain prose "
            "answering that question from the Material alone. Answer the "
            "question that was asked and nothing beside it, and say plainly "
            "when the Material does not settle it — \"the material does not "
            "say\" is a complete answer and a better one than a guess.\n"
            "\n" + _RULES),
        max_answer_chars=1200,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.STRONG,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "similar-incidents": ToolSpec(
        id="similar-incidents",
        label="Find similar incidents",
        sources=(SourceKind.INCIDENT, SourceKind.OCCURRENCES,
                 SourceKind.NEIGHBOURS),
        instruction=(
            "You are telling an engineer whether incident {slug} on {db}, "
            "{title}, has happened before on this fleet.\n"
            "\n"
            "Write at most {max_answer_chars} characters of plain prose. Name "
            "each incident the Material lists as similar, say what it shares "
            "with this one, and say plainly when the Material lists none — "
            "\"no similar incidents found\" is a complete answer and a better "
            "one than a resemblance nobody recorded.\n"
            "\n" + _RULES),
        max_answer_chars=900,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.CHEAP,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "summarize-evidence": ToolSpec(
        id="summarize-evidence",
        label="Summarise the evidence",
        sources=(SourceKind.INCIDENT, SourceKind.CLOSURE, SourceKind.DIGEST,
                 SourceKind.ACTIONS),
        instruction=(
            "You are summarising the evidence gathered on incident {slug} on "
            "{db}, {title}, for an engineer deciding what to do next.\n"
            "\n"
            "Write at most {max_answer_chars} characters of plain prose: what "
            "the digests and the monitoring window show, and where they "
            "disagree with each other or with the page.\n"
            "\n" + _RULES),
        max_answer_chars=1200,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.CHEAP,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "draft-note": ToolSpec(
        id="draft-note",
        label="Draft the summary line",
        sources=(SourceKind.INCIDENT, SourceKind.ERROR_PAGE,
                 SourceKind.DIGEST),
        instruction=(
            "You are drafting the summary line of an action record on an "
            "operations wiki, for incident {slug} on {db}: {title}.\n"
            "\n"
            "Reply with ONE line of plain prose and NOTHING else: no "
            "preamble, no bullet, no markdown, no quotation marks.\n"
            "\n"
            "Write it the way the operator would: past tense, at most "
            "{max_answer_chars} characters. Say what happened and what was "
            "done about it.\n"
            "\n"
            "Rules:\n"
            "- State only what the Material below shows. Never claim a cause, "
            "a fix or a recovery it does not show.\n"
            "- Events stopping is not recovery: say the errors stopped, never "
            "that the problem is solved.\n"
            "- You may name at most one path from the Material, spelled "
            "exactly as the Material spells it, when it is the evidence for "
            "the claim.\n"
            "- No [[wikilinks]], no headings, no URLs.\n"
            "\n"
            "Material (assembled deterministically; every path in it is a "
            "page the wiki holds at this revision):"),
        max_answer_chars=structured.MAX_SUMMARY,
        target_field="summary",
        role=identity.DRAFT_ROLE,
        tier=Tier.CHEAP,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "next-checks": ToolSpec(
        id="next-checks",
        label="Suggest the next checks",
        sources=(SourceKind.INCIDENT, SourceKind.ERROR_PAGE,
                 SourceKind.CLOSURE, SourceKind.JOURNAL),
        instruction=(
            "You are suggesting what an engineer should look at next on "
            "incident {slug} on {db}, {title}, currently {status}.\n"
            "\n"
            "Write at most {max_answer_chars} characters as a short list of "
            "checks, one per line, each naming what to look at and what would "
            "settle it. Suggest a check; never state its outcome, and never "
            "recommend an action on the database itself.\n"
            "\n" + _RULES),
        max_answer_chars=900,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.CHEAP,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
    "explain-closure": ToolSpec(
        id="explain-closure",
        label="Explain the monitoring facts",
        sources=(SourceKind.CLOSURE, SourceKind.DIGEST),
        instruction=(
            "You are describing the monitoring window on incident {slug} on "
            "{db}, {title}, to the engineer reading the closure evidence "
            "beside this narration.\n"
            "\n"
            "Begin with what argues against closing. State every such line "
            "the Material holds, one per sentence, before you write anything "
            "else; if it holds none, say that in one sentence. Then say, in "
            "at most two sentences, what argues for closing and what the "
            "window has not covered.\n"
            "\n"
            "Write at most {max_answer_chars} characters of plain prose.\n"
            "\n"
            "Never say what should happen to this incident next. Whether it "
            "may change state, and under which rule, is settled elsewhere and "
            "is not yours to advise on: you are describing the facts the last "
            "evaluation recorded, and nothing else.\n"
            "\n" + _RULES),
        max_answer_chars=900,
        target_field="",
        role=identity.DRAFT_ROLE,
        tier=Tier.CHEAP,
        budget=DEFAULT_BUDGET,
        enabled=True,
        model=None,
        timeout_s=DEFAULT_TIMEOUT_S),
}


def _check_sources(sources: Mapping[SourceKind, PackRule]) -> None:
    """Every kind has a rule and every rule has a kind. Import-time, because a
    kind a tool could name and `pack` could not read is a bug no request would
    reach until an operator clicked that tool."""
    missing = set(SourceKind) - set(sources)
    extra = set(sources) - set(SourceKind)
    if missing or extra:
        raise ValueError(
            f"SOURCES: no rule for {sorted(missing)}, and a rule for "
            f"{sorted(extra)}, which is not a kind")


def _probe_words() -> set[str]:
    """The placeholder names a real `Target` fills, read off a real `Target`
    rather than restated: a `words()` that grew a key nobody may name, or lost
    one an instruction names, has to fail this check."""
    probe = Target.incident(
        Incident(path="incidents/probe.md", db="db", title="t",
                 status=Status.OPEN),
        tree=Tree.of({}), revision="0" * 40)
    return set(probe.words())


def _placeholders(instruction: str) -> set[str]:
    """The names a `str.format` template fills, through
    `string.Formatter().parse`. One reader, because two questions are asked of
    an instruction's placeholders — whether it names one no target fills, and
    whether it names `question` — and two parsers would let those two answers
    drift apart."""
    return {name for _, name, _, _ in
            string.Formatter().parse(instruction) if name}


def asks(spec: ToolSpec) -> bool:
    """Whether this row takes the operator's free text, which is exactly
    whether its instruction names `{question}`.

    The single definition of "this row asks something". Derived from the
    registry rather than from an id, so a second free-text row is a row and
    not an edit to a condition in `api.py` and another in the page."""
    return "question" in _placeholders(spec.instruction)


def _check_tools(tools: Mapping[str, ToolSpec]) -> None:
    """Every row's id matches its key, its sources are `SOURCES` keys, and its
    instruction names only placeholders a target fills. The
    `deeplink._check_templates` idiom."""
    allowed = _probe_words() | {"max_answer_chars"}
    for tool_id, spec in tools.items():
        if spec.id != tool_id:
            raise ValueError(f"{tool_id}: the row calls itself {spec.id!r}")
        unknown = set(spec.sources) - set(SOURCES)
        if unknown:
            raise ValueError(f"{tool_id}: {sorted(unknown)} is not a source "
                             f"kind `pack` can read")
        named = _placeholders(spec.instruction)
        if named - allowed:
            raise ValueError(
                f"{tool_id}: the instruction names {sorted(named - allowed)}, "
                f"which no target fills")


_check_sources(SOURCES)
_check_tools(TOOLS)


def pack(spec: ToolSpec, target: Target) -> EvidencePack:
    """Everything `spec` may read about `target`, capped.

    Iterates `spec.sources` in order, dispatches each kind through `SOURCES`,
    applies that rule's `max_items` and `max_chars` (through
    `structured._cap`, so a cut section says `[truncated]` and the model never
    mistakes a chopped page for a whole one), and stamps the section's kind
    from the table key. An empty body is skipped rather than packed: a slot
    spent on a page nobody can read is a slot the model does not get.

    Reads only through `target`, which is pinned at one revision, and makes no
    network call and takes no lock. This is what the manifest endpoint runs
    for real."""
    sections: list[PackSection] = []
    for kind in spec.sources:
        rule = SOURCES[kind]
        packed = 0
        for path, body in rule.read(target):
            if packed >= rule.max_items:
                break
            text = structured._cap(body, rule.max_chars) if body else ""
            if not text:
                continue
            sections.append(PackSection(
                kind=kind, path=path, heading=path or rule.heading, text=text,
                truncated=len(body.strip()) > rule.max_chars))
            packed += 1
    return EvidencePack(tuple(sections))


def cited(text: str, paths: Sequence[str]) -> tuple[str, ...]:
    """The paths from `paths` that appear in `text`, in pack order. A
    post-hoc scan rather than a field the model fills: scanning for paths the
    material actually contained is more robust than trusting a model to fill a
    `sources` array."""
    return tuple(path for path in paths if path in text)


def build_prompt(spec: ToolSpec, target: Target, packed: EvidencePack) -> str:
    """`spec.instruction` filled from the target, then the material. One
    concatenation and nothing else, so what the model was asked is
    reconstructable from a row and a revision."""
    head = spec.instruction.format_map(
        {**target.words(), "max_answer_chars": spec.max_answer_chars})
    return f"{head}\n\n{packed.text}"


def spend(state_dir: Path, tool: str, *, window_h: int, now: str) -> Spend:
    """What `tool` has cost inside `window_h` hours of `now`, read off
    `.state/advisory_runs.jsonl`.

    Per tool, because the budget that will be checked against it lives on the
    row: counting another row's runs against a ceiling this row declares would
    be a ceiling nobody wrote down.

    The `at` this reads is the ledger's own, minted when the run finished, and
    never the caller's `run.at`: that one rides in on a request body, and a
    window keyed on it would stop counting for anybody who posted a date in
    1999.

    Fails closed on a clock nobody can read. A line whose `at` will not parse
    is outside the window, the ledger readers' usual tolerance; but a `now`
    that will not parse leaves no window to be outside of, and every line the
    tool wrote counts, because a ceiling that silently zeroes itself on a
    malformed clock is a ceiling that is not there."""
    since = _instant(now)
    if since is not None:
        since -= dt.timedelta(hours=window_h)
    runs = measured = 0
    cost = 0.0
    try:
        lines = (Path(state_dir) / health.ADVISORY_LOG).read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("tool") != tool:
            continue
        at = _instant(str(row.get("at", "")))
        if since is not None and (at is None or at < since):
            continue
        runs += 1
        if _number(row.get("cost_usd")) is not None:
            measured += 1
            cost += float(row["cost_usd"])
    return Spend(runs=runs, measured_runs=measured, cost_usd=cost)


def _number(value: object) -> float | None:
    """A real number, which `True` is not: json carries booleans as numbers to
    `isinstance` and a flag must never be added up as a cost."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _instant(stamp: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check(current: Spend, budget: Budget) -> str:
    """`""` when a run may start, else the stable reason it may not:

        max_runs       the count ceiling; always enforceable
        cost_unknown   the window holds runs nobody priced, so a dollar
                       ceiling cannot be honestly applied to it
        max_cost_usd   the measured spend already reaches the ceiling

    Count first, dollars second. `cost_unknown` refuses loudly rather than
    treating unpriced runs as free, because unmeasured spend is not zero
    spend; a deployment that wants clicks to keep working through it sets
    `max_cost_usd: null` and keeps the count ceiling."""
    if current.runs >= budget.max_runs:
        return "max_runs"
    if budget.max_cost_usd is None:
        return ""
    if current.unmeasured_runs:
        return "cost_unknown"
    if current.cost_usd >= budget.max_cost_usd:
        return "max_cost_usd"
    return ""


@dataclass(frozen=True)
class Settings:
    """The `advisory:` block, resolved. Every default lives here rather than
    in `config.py`, whose `advisory` attribute is a bare `raw.get`."""

    enabled: bool
    max_concurrent: int
    retain: int
    tools: Mapping[str, ToolSpec]


def resolve(cfg) -> Settings:
    """`cfg.advisory` as typed settings, with `TOOLS` as the base and the
    block's `tools:` entries overriding per row (`enabled`, `tier`,
    `timeout_s`, `window_h`, `max_runs`, `max_cost_usd`).

    Each row's `model` is resolved here from `agents.pi[tier]`, so a run never
    reads config and the run record names the model that was actually asked.

    Refuses at resolve, naming the key: an unknown tool id
    (`advisory.tools.<id>`), an unknown tier (`advisory.tools.<id>.tier` or
    `advisory.tier`). A refusal here fails the server's start, which is where
    a misconfiguration should be found, rather than the first click."""
    raw = getattr(cfg, "advisory", None) or {}
    overrides = raw.get("tools") or {}
    unknown = sorted(set(overrides) - set(TOOLS))
    if unknown:
        raise ValueError(f"advisory.tools.{unknown[0]}: no such tool; the "
                         f"tools are {', '.join(sorted(TOOLS))}")
    pi = (getattr(cfg, "agents", None) or {}).get("pi") or {}
    rows = {}
    for tool_id, base in TOOLS.items():
        over = overrides.get(tool_id) or {}
        key = f"advisory.tools.{tool_id}"
        tier = base.tier
        if "tier" in raw:
            tier = _tier(raw["tier"], "advisory.tier")
        if "tier" in over:
            tier = _tier(over["tier"], f"{key}.tier")
        cost = _pick(over, raw, "max_cost_usd", base.budget.max_cost_usd)
        rows[tool_id] = replace(
            base,
            tier=tier,
            model=pi.get(str(tier)),
            enabled=bool(over.get("enabled", base.enabled)),
            timeout_s=int(_pick(over, raw, "timeout_s", base.timeout_s)),
            budget=Budget(
                window_h=int(_pick(over, raw, "window_h",
                                   base.budget.window_h)),
                max_runs=int(_pick(over, raw, "max_runs",
                                   base.budget.max_runs)),
                max_cost_usd=None if cost is None else float(cost)))
    return Settings(
        enabled=bool(raw.get("enabled", True)),
        max_concurrent=int(raw.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        retain=int(raw.get("retain", DEFAULT_RETAIN)),
        tools=rows)


def _pick(over: Mapping, block: Mapping, key: str, fallback):
    """A row's override, else the block's top-level default for every row,
    else what `TOOLS` shipped."""
    if key in over:
        return over[key]
    return block.get(key, fallback)


def _tier(value: object, key: str) -> Tier:
    try:
        return Tier(str(value))
    except ValueError:
        raise ValueError(
            f"{key}: {value!r} is not a tier; the tiers are "
            f"{', '.join(t.value for t in Tier)}") from None


def new_boot_id() -> str:
    """This process's identity for the life of the process."""
    return uuid.uuid4().hex[:12]


def run_id_for(tool: str, target: str, at: str, question: str = "") -> str:
    """`sha256(f"{tool}|{target}|{at}")[:12]`, and
    `sha256(f"{tool}|{target}|{at}|{question}")[:12]` when there is a
    question. Deterministic in the caller's `at`, which is what makes a
    re-POST idempotent, and in the question, so two different questions asked
    at one `at` are two runs rather than one answering the first.

    The empty question is left out of the hashed string rather than appended
    as `|`: the identity of a run that asks nothing is what it always was, so
    no id already on disk moves."""
    key = f"{tool}|{target}|{at}"
    if question:
        key = f"{key}|{question}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ToolStatus:
    """One manifest row: the spec, what a click would read right now, what the
    window has already spent, and the reason the row would refuse (`""` when
    it would run)."""

    spec: ToolSpec
    packed: EvidencePack
    spent: Spend
    reason: str


class Runner:
    """The portal's second mutable member, and its only path to a model.

    Admission is one critical section: `spend()` over the ledger, `check()`
    against the row's budget, and counting the reservation happen under one
    `threading.Lock`, with `BoundedSemaphore(max_concurrent)` acquired
    non-blocking inside it, so a third click is refused rather than queued.

    The model call runs outside both locks, on a daemon thread that writes the
    terminal record and one ledger line in a `finally`, so a request thread
    never waits on a model.

    Nothing here reads `transaction.head`, takes `lock.single_flight` or
    writes the wiki. An advisory run's whole authority is its own file.
    """

    def __init__(self, settings: Settings, *, state_dir: Path,
                 provider: str | None = None,
                 wiki: Path | None = None,
                 now: Callable[[], str] | None = None,
                 boot_id: str | None = None):
        """`provider` and `wiki` are the two things `harness.run_text` wants
        that a spec does not carry: which pi provider serves the model, and
        the working directory the adapter is started in."""
        self.settings = settings
        self.state_dir = Path(state_dir)
        self.runs_dir = self.state_dir / RUNS_DIR
        self.provider = provider
        self.wiki = wiki
        self.now = now or _now
        self.boot_id = boot_id or new_boot_id()
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(
            max(1, settings.max_concurrent))
        self._reserved: dict[str, int] = {}

    @classmethod
    def from_config(cls, cfg) -> "Runner":
        """`resolve(cfg)` plus the state directory, the pi provider and the
        wiki checkout. Raises whatever `resolve` refuses, at server start."""
        agents = getattr(cfg, "agents", None) or {}
        return cls(resolve(cfg), state_dir=cfg.state_dir,
                   provider=(agents.get("pi") or {}).get("provider"),
                   wiki=cfg.wiki_repo)

    def spec(self, tool: str) -> ToolSpec:
        """The resolved row, or `UnknownTool` / `ToolDisabled`."""
        row = self.settings.tools.get(tool)
        if row is None:
            raise UnknownTool(tool)
        if not (self.settings.enabled and row.enabled):
            raise ToolDisabled(tool)
        return row

    def manifest(self, target: Target) -> tuple[ToolStatus, ...]:
        """Every row, packed for real and priced against the ledger, in
        `TOOLS` order. No model call: this is what lets the panel show what a
        click would read and what is left of the budget *before* the click."""
        now = self.now()
        rows = []
        for tool_id in TOOLS:
            row = self.settings.tools[tool_id]
            spent = spend(self.state_dir, tool_id,
                          window_h=row.budget.window_h, now=now)
            reason = ("tool_disabled"
                      if not (self.settings.enabled and row.enabled)
                      else check(spent, row.budget))
            rows.append(ToolStatus(spec=row, packed=pack(row, target),
                                   spent=spent, reason=reason))
        return tuple(rows)

    def start(self, spec: ToolSpec, target: Target, *, at: str) -> AdvisoryRun:
        """Admit one run and return its `queued` record in milliseconds.

        In order: `sweep` (so a dead boot's records are honest before anything
        is counted), the idempotency read (a non-refused record for this
        `run_id` is returned as it stands, and nothing is spent), admission
        under the lock, `pack`, the `queued` record with its manifest, then
        the worker thread.

        A refusal writes a `refused` record and raises `Refused`. It appends
        no ledger line: the ledger is a cost report, and a click that spent
        nothing has no cost to report."""
        self.sweep()
        run_id = run_id_for(spec.id, target.key, at, target.question)
        standing = self.read(run_id)
        if standing is not None and standing.status is not RunStatus.REFUSED:
            return standing
        now = self.now()
        blank = AdvisoryRun(
            schema_version=SCHEMA_VERSION, run_id=run_id, tool=spec.id,
            target=target.key, at=at, question=target.question,
            status=RunStatus.QUEUED,
            boot_id=self.boot_id, started=now, finished="", duration_s=None,
            evidence_revision=target.revision, context=(), answer=None,
            error="")
        try:
            self._admit(spec, now)
        except _Denied as denial:
            run = self._write(replace(blank, status=RunStatus.REFUSED,
                                      finished=now, duration_s=0.0,
                                      error=denial.reason))
            raise Refused(denial.reason, run=run, spend=denial.spend,
                          ceiling=denial.ceiling,
                          retry_after_s=denial.retry_after_s) from None
        packed = pack(spec, target)
        run = self._write(replace(blank, context=packed.manifest))
        threading.Thread(target=self._work, daemon=True,
                         args=(spec, target, run, packed)).start()
        return run

    def read(self, run_id: str) -> AdvisoryRun | None:
        """The record as this process should read it (`as_of`), or None. The
        poll endpoint's whole body."""
        run = self._load(self.runs_dir / f"{run_id}.json")
        return None if run is None else run.as_of(self.boot_id)

    def _load(self, path: Path) -> AdvisoryRun | None:
        try:
            return AdvisoryRun.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def sweep(self) -> None:
        """Rewrite every record left in flight by a boot that is gone, then
        prune to `settings.retain` newest by `started`.

        Idempotent by construction: the second sweep finds no foreign in-
        flight record and no surplus file, and rewrites nothing — a sweep that
        changed bytes every time would make every restart look like activity.
        """
        kept: list[tuple[str, Path]] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            run = self._load(path)
            if run is None:
                continue
            honest = run.as_of(self.boot_id)
            if honest != run:
                self._write(honest)
            kept.append((honest.started, path))
        kept.sort(reverse=True)
        for _, path in kept[self.settings.retain:]:
            path.unlink(missing_ok=True)

    def _work(self, spec: ToolSpec, target: Target, run: AdvisoryRun,
              packed: EvidencePack) -> None:
        """The worker: mark `running`, call `harness.run_text` outside every
        lock, and write one ledger line plus the terminal record in a
        `finally` — including when the call raises something nobody predicted,
        because a run left `running` forever is worse than one recorded as
        failed. The ledger line lands first: a poller that sees a terminal
        record may read the ledger next, and a crash between the two writes
        must err on the side of a counted run, never a free one.

        `harness.run_text` is looked up on the module at call time rather than
        bound as a default, so a `monkeypatch.setattr` reaches this path the
        way it reaches every other caller."""
        run = self._write(replace(run, status=RunStatus.RUNNING))
        clock = time.monotonic()
        telemetry: dict = {}
        try:
            try:
                answer = harness.run_text(
                    build_prompt(spec, target, packed), spec.model,
                    spec.timeout_s, provider=self.provider, cwd=self.wiki,
                    telemetry=telemetry).strip()
            except (harness.HarnessError, OSError) as e:
                run = replace(run, status=RunStatus.FAILED,
                              error=structured._cap(str(e), MAX_ERROR_CHARS))
            else:
                run = replace(run, status=RunStatus.SUCCEEDED, answer=Answer(
                    text=answer, cites=cited(answer, packed.paths),
                    model=telemetry.get("model") or spec.model,
                    usage=telemetry.get("usage", harness.UNKNOWN)))
        except BaseException as e:  # noqa: BLE001 — a run left `running` forever is worse
            run = replace(run, status=RunStatus.FAILED,
                          error=structured._cap(f"{type(e).__name__}: {e}",
                                                MAX_ERROR_CHARS))
        finally:
            if run.status not in TERMINAL:
                run = replace(run, status=RunStatus.FAILED,
                              error=run.error or "the worker did not answer")
            run = replace(run, finished=self.now(),
                          duration_s=round(time.monotonic() - clock, 3))
            self._ledger(run, spec)
            self._write(run)
            self._release(spec)

    def _admit(self, spec: ToolSpec, now: str) -> None:
        """The critical section: spend, check, reserve. Raises `_Denied` with
        the reason, which `start` turns into the `Refused` a caller sees once
        there is a record to point at; holds the lock for the length of one
        capped-log read and no model call."""
        with self._lock:
            spent = spend(self.state_dir, spec.id,
                          window_h=spec.budget.window_h, now=now)
            queued = spent.plus_reserved(self._reserved.get(spec.id, 0))
            reason = check(queued, spec.budget)
            if reason:
                raise _Denied(reason, spend=queued, ceiling=spec.budget)
            if not self._slots.acquire(blocking=False):
                raise _Denied("busy", spend=queued, ceiling=spec.budget,
                              retry_after_s=float(spec.timeout_s))
            self._reserved[spec.id] = self._reserved.get(spec.id, 0) + 1

    def _release(self, spec: ToolSpec) -> None:
        """Give the slot and the reservation back. Called once per admitted
        run, from the worker's `finally`."""
        with self._lock:
            self._reserved[spec.id] = max(
                0, self._reserved.get(spec.id, 0) - 1)
            self._slots.release()

    def _write(self, run: AdvisoryRun) -> AdvisoryRun:
        """Persist one record through `state.atomic_write_text`, the per-
        entity precedent `.state/monitoring/<slug>.json` sets."""
        state.atomic_write_text(
            self.runs_dir / f"{run.run_id}.json",
            json.dumps(run.to_dict(), indent=1, sort_keys=True))
        return run

    def _ledger(self, run: AdvisoryRun, spec: ToolSpec) -> None:
        """One line per terminal run through `health.append_advisory_run`:
        identifiers, counts, duration and flattened usage. Never a prompt and
        never an answer.

        `at` is the terminal time this process minted, the rule
        `health.record_agent_run` keeps, because `spend` filters its window on
        it and `run.at` arrives in a request body: a caller who posted a date
        in 1999 would otherwise write a line no window counts and walk through
        the count ceiling. The caller's key is kept as `requested_at`, which
        nothing budgets on."""
        answer = run.answer
        event = {"run_id": run.run_id, "tool": run.tool, "target": run.target,
                 "at": run.finished, "requested_at": run.at,
                 "status": str(run.status), "boot_id": run.boot_id,
                 "evidence_revision": run.evidence_revision,
                 "started": run.started, "finished": run.finished,
                 "duration_s": run.duration_s,
                 "sections": len(run.context),
                 "context_chars": sum(e.chars for e in run.context),
                 "answer_chars": len(answer.text) if answer else 0,
                 "cites": len(answer.cites) if answer else 0,
                 "model": (answer.model if answer else None) or spec.model,
                 "tier": str(spec.tier)}
        usage = answer.usage if answer else None
        known = False
        if isinstance(usage, Mapping):
            for key in ("input_tokens", "output_tokens", "cost_usd"):
                value = _number(usage.get(key))
                if value is not None:
                    event[key] = usage[key]
                    known = known or key != "cost_usd"
        event["usage_known"] = known
        health.append_advisory_run(self.state_dir, event)
