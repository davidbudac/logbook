"""The only code that decides which incident transitions are allowed, and
which files each one touches.

`TRANSITIONS` is the domain model: a dict keyed by `(Status, command type)`
whose value is the resulting status. An absent key is an illegal transition,
so adding a state or a command means adding rows, and a row nobody wrote is a
`TransitionError` rather than a fall-through.

`build` is pure. It reads through a `transaction.Tree` and returns a
`transaction.Proposal`; it opens no file, runs no git, and takes `at` as its
clock. Same (base, incident, command, actor, at) in, same bytes out.

Every command produces exactly one `ActionRecord`, except `Merge`, which
produces one per page: the page being folded away and the page that keeps it
are each read on their own, so each needs its own durable account of the act.
Resolving an incident is not a status write with a note attached; it is an
act that happens to move the status. There is no way to change a status
without a signed, dated account of why.
"""

import re
from dataclasses import dataclass

from .incidents import (ActionMalformed, ActionRecord, Incident,
                        MonitoringWindow, Outcome, RecoverySignal, Status,
                        append_action, incident_path, read_incident,
                        set_status)
from .pagetext import (log_append, nl, oneline, section_append,
                       section_drop_if_empty, section_line_replace,
                       section_move_line, sections)
from .transaction import Actor, Proposal, Tree

INDEX_OPEN = "Open incidents"
INDEX_RESOLVED = "Resolved incidents"

#: The error page's durable record of a confirmed fix (wiki/AGENTS.md).
RESOLUTION_SECTION = "Resolution history"
RESOLUTION_HEAD = ("| resolved | db | incident | remediation | evidence |\n"
                   "|---|---|---|---|---|")

#: Where a merge parks the evidence it carries over, and how it reads the two
#: shapes it carries. Readers, not writers, so they may be broader than the
#: ingest writer's own per-day regex.
EVIDENCE_SECTION = "Evidence"
EVIDENCE_HEAD_RE = re.compile(rf"(?i)\A##\s+{EVIDENCE_SECTION}\s*\Z")
EVIDENCE_LINE_RE = re.compile(r"\A- (\d{4}-\d{2}-\d{2}):\s*(digests/\S+)")
UPDATE_HEAD_RE = re.compile(r"\A##\s+Update\s+(\d{4}-\d{2}-\d{2})\s*\Z")


class TransitionError(ValueError):
    """The command is not legal from the incident's current status, or the
    page's status is off-vocabulary. Carries both so the CLI and the portal
    can say "cannot extend monitoring on an open incident"."""

    def __init__(self, status: Status | str, command_type: type):
        self.status = status
        self.command_type = command_type
        super().__init__(f"{command_type.__name__} is not valid from "
                         f"status {status!r}")


class MergeRefused(ValueError):
    """The two pages are not a duplicate pair: the same page named twice, a
    kept page the tree does not hold, one belonging to another database, or
    one whose own status is off-vocabulary. Carries the dropped `path` and the
    kept `into` path so the portal can point at the control that named the
    wrong page, and is a `ValueError` like every other builder refusal so
    `incident_cli`'s single `except ValueError` -> exit 2 keeps working."""

    def __init__(self, path: str, into: str, message: str):
        self.path = path
        self.into = into
        super().__init__(message)


@dataclass(frozen=True)
class RecordAction:
    """A DBA did something and is writing it down. Status unchanged (design
    document Q2: a timeline event). Legal from every status, `resolved`
    included: a post-incident note must not require reopening, or people
    will hand-edit the page instead."""

    intent: str
    summary: str
    ticket: str = ""
    outcome: Outcome = Outcome.PENDING
    rollback: str = ""
    evidence: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class StartMonitoring:
    """Begin (or re-arm) an observation window with an expected recovery
    signal. Legal from `open` and from `monitoring`; from `monitoring` it
    replaces the window because a second remediation is being watched and
    the evidence basis resets deliberately."""

    signal: RecoverySignal
    until: str
    intent: str
    summary: str
    ticket: str = ""
    evidence: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class ExtendMonitoring:
    """Push `until` out, keeping the signal and the original start. A
    separate command because it may not change the signal; changing it
    mid-window would invalidate the evidence already gathered."""

    until: str
    intent: str
    notes: str = ""


@dataclass(frozen=True)
class Resolve:
    """A human confirms the incident is over. The only path to `resolved`.

    `update_error_pages` (design document Q5) defaults on: the linked error
    pages' `## Resolution history` rows land in the same transaction. Evidence
    may be empty; the action record itself is the dated operator note that
    `wiki/AGENTS.md` accepts as recovery evidence, and the row then cites the
    record's `at`."""

    summary: str
    residual_risk: str = ""
    ticket: str = ""
    evidence: tuple[str, ...] = ()
    notes: str = ""
    update_error_pages: bool = True


@dataclass(frozen=True)
class Reopen:
    """The condition came back, or closure was wrong. `resolved` -> `open`,
    never straight to `monitoring`: a stale signal must not carry over."""

    reason: str
    evidence: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Merge:
    """Fold a duplicate incident into the page that keeps it.

    `into` is the kept page's slug, bare or as a path; `incidents.incident_path`
    normalises it. There is no `summary` field because the act names itself,
    the way `ExtendMonitoring`'s summary is derived rather than typed.

    The kept page absorbs what the duplicate evidences and it does not have,
    and nothing else: not the duplicate's status, not its window, and no
    `## Resolution history` row on the linked error pages. A duplicate folding
    away is not a confirmed fix, and only `Resolve` writes those.

    `(RESOLVED, Merge)` has no row in `TRANSITIONS`, so merging the same pair
    a second time is a `TransitionError` off the table rather than a second
    record claiming the page is a duplicate twice over."""

    into: str
    notes: str = ""


Command = (RecordAction | StartMonitoring | ExtendMonitoring | Resolve
           | Reopen | Merge)


TRANSITIONS: dict[tuple[Status, type], Status] = {
    (Status.OPEN, RecordAction): Status.OPEN,
    (Status.MONITORING, RecordAction): Status.MONITORING,
    (Status.RESOLVED, RecordAction): Status.RESOLVED,

    (Status.OPEN, StartMonitoring): Status.MONITORING,
    (Status.MONITORING, StartMonitoring): Status.MONITORING,

    (Status.MONITORING, ExtendMonitoring): Status.MONITORING,

    (Status.OPEN, Resolve): Status.RESOLVED,
    (Status.MONITORING, Resolve): Status.RESOLVED,

    (Status.RESOLVED, Reopen): Status.OPEN,

    (Status.OPEN, Merge): Status.RESOLVED,
    (Status.MONITORING, Merge): Status.RESOLVED,
}

KINDS: dict[type, str] = {
    RecordAction: "record-action",
    StartMonitoring: "start-monitoring",
    ExtendMonitoring: "extend-monitoring",
    Resolve: "resolve",
    Reopen: "reopen",
    Merge: "merge",
}

_INDEX_MOVE: dict[type, tuple[str, str]] = {
    Resolve: (INDEX_OPEN, INDEX_RESOLVED),
    Reopen: (INDEX_RESOLVED, INDEX_OPEN),
    Merge: (INDEX_OPEN, INDEX_RESOLVED),
}

#: Index sections `section_append` creates the first time they are needed, and
#: that come off the page again when their last entry leaves. `## Open
#: incidents` is deliberately not one: it is the attention queue, and an empty
#: queue is a fact worth showing.
INDEX_ON_DEMAND = frozenset({INDEX_RESOLVED})


def next_status(status: Status, command: Command) -> Status:
    """The status after `command`, or raise TransitionError. The one lookup:
    `build` calls it, the portal calls it to grey out buttons, and 0.7 calls
    it to ask whether a `Resolve` would even be legal."""
    key = (status, type(command))
    if key not in TRANSITIONS:
        raise TransitionError(status, type(command))
    return TRANSITIONS[key]


def allowed(status: Status) -> tuple[type, ...]:
    """Command types legal from `status`, in table order. Derived from
    TRANSITIONS, never a second list."""
    return tuple(command for state, command in TRANSITIONS if state == status)


def resolution_row(incident: Incident,
                   record: ActionRecord) -> tuple[str, str]:
    """The `## Resolution history` row one resolve puts on an error page, as
    `(prefix, row)`. Pure over the two things the row describes.

    The prefix is `| <day> | <db> | [[<incident>]] | `: the day the record
    carries, the incident's db, and its own page as a wikilink. That is the
    row's identity, not decoration. `section_line_replace` matches on it, so
    resolving the same incident a second time rewrites the row it already
    wrote instead of leaving the error page carrying two accounts of one fix.
    Every column after the prefix is the resolve's own account, and the
    evidence cell falls back to the timestamp when the record cites none, so
    the column is never empty.

    It is a function rather than three lines inside `build` because two
    writers produce this row: `build`, when a resolve is published, and
    `incident_cli.cmd_incident_backfill_resolutions`, which writes the rows
    for resolves that landed before error pages recorded them. Those rows must
    be byte-identical or the backfill would append beside the row a later
    resolve then rewrites, and the divergence would only show as a duplicated
    line on the error page."""
    target = incident.path[:-3]
    prefix = f"| {record.at[:10]} | {incident.db} | [[{target}]] | "
    evidence = oneline(", ".join(str(e) for e in record.evidence))
    row = (f"{prefix}{oneline(record.summary)} | "
           f"{evidence or record.at} |")
    return prefix, row


def build(tree: Tree, path: str, command: Command, actor: Actor,
          at: str) -> Proposal:
    """Compose the whole transaction for one command against one incident.

    Pure. `tree` supplies every byte it reads and `at` supplies the clock.
    Reads the incident from `tree` rather than taking an `Incident`, so a
    caller cannot build against a status the page no longer has.

    Raises TransitionError before composing anything when the transition is
    illegal or the page's status is off-vocabulary (`unknown_status`). Raises
    `MergeRefused` when a `Merge` names a page that is not this one's
    duplicate, after the transition is checked, so an illegal merge is still
    refused as an illegal transition. Raises `incidents.ActionMalformed` when
    the free text cannot become a record, or when an `ExtendMonitoring`
    reaches a `monitoring` page whose window is unreadable. Raises
    `ValueError` when `tree` holds no page at `path`.

    Files, per command:

    - every command: the incident page (`append_action`, plus `set_status`
      when the status or window changes) and `log.md` (one line).
    - StartMonitoring / ExtendMonitoring: the `monitoring:` frontmatter.
    - Resolve: `index.md`, moving the bullet from `## Open incidents` to
      `## Resolved incidents` via `pagetext.section_move_line`, keeping its
      label; plus one `## Resolution history` row per `[[errors/<code>]]` the
      incident links, when `update_error_pages`, keyed on
      `| <day> | <db> | [[<incident>]] | ` so a second resolve rewrites the
      row. A linked error page absent from `tree` yields a `Proposal.notes`
      advisory and no row; creating error pages is the ingest writer's job,
      and an absent `index.md` is treated the same way.
    - Reopen: `index.md`, the same move back, and `## Resolved incidents`
      comes off the page when that bullet was its last entry
      (`INDEX_ON_DEMAND`).
    - Merge: `index.md`, moved exactly as a `Resolve` moves it, plus the kept
      page, which takes the carry-over and a second record of its own and
      keeps its own status and window (`_merged_into`). Only the dropped
      page's line reaches `log.md`; a merge is one act, however many pages
      record it.

    Does not check that cited digests exist; lint's `digest-missing` owns
    that and preview runs it before publication.

    Twice: identical Proposal. Dies halfway: nothing written, this touches
    no disk."""
    text = tree.read(path)
    if text is None:
        raise ValueError(f"no incident page at {path} in {tree.revision}")
    incident = read_incident(text, path)
    if incident.unknown_status is not None:
        raise TransitionError(incident.unknown_status, type(command))
    record = _action_of(incident, command, actor, at,
                        next_status(incident.status, command))
    target = path[:-3]
    notes: list[str] = []
    files: dict[str, str | None] = {
        path: set_status(append_action(text, record), record.status_after,
                         updated=at, monitoring=record.window),
        "log.md": log_append(tree.read("log.md"), record.log_line(path)),
    }
    if isinstance(command, Merge):
        kept_path, kept_text = _merged_into(tree, incident, text, command,
                                            actor, at)
        files[kept_path] = kept_text
    if move := _INDEX_MOVE.get(type(command)):
        frm, to = move
        index = tree.read("index.md")
        if index is None:
            notes.append(
                f"index.md is absent; no bullet was moved to ## {to}")
        else:
            moved = section_move_line(
                index, frm=frm, to=to, prefix=f"- [[{target}]]",
                fallback=f"- [[{target}]] — {incident.title}")
            files["index.md"] = (section_drop_if_empty(moved, frm)
                                 if frm in INDEX_ON_DEMAND else moved)
    if isinstance(command, Resolve) and command.update_error_pages:
        prefix, row = resolution_row(incident, record)
        for code in incident.error_codes:
            rel = f"errors/{code}.md"
            error_page = tree.read(rel)
            if error_page is None:
                notes.append(f"{rel} is absent; no {RESOLUTION_SECTION} row "
                             f"was written for {code}")
                continue
            files[rel] = section_line_replace(error_page, RESOLUTION_SECTION,
                                              prefix=prefix, line=row,
                                              header=RESOLUTION_HEAD)
    return Proposal(
        base=tree.revision, actor=actor,
        message=(f"incident: {record.kind} {incident.slug} — "
                 f"{record.summary[:60]}"),
        files={rel: content for rel, content in files.items()
               if content != tree.read(rel)},
        notes=tuple(notes))


def _action_of(incident: Incident, command: Command, actor: Actor, at: str,
               status_after: Status) -> ActionRecord:
    """The `ActionRecord` a command becomes. One mapping, so the yaml block,
    the log line and the Resolution-history row describe the same act.

    `Resolve`, `Reopen` and `Merge` carry no `intent` of their own; the act
    names its own purpose, and the operator's reasoning is in `summary` and
    `notes`."""
    match command:
        case RecordAction():
            fields = dict(intent=command.intent, summary=command.summary,
                          ticket=command.ticket,
                          outcome=command.outcome,
                          rollback=command.rollback,
                          evidence=command.evidence, notes=command.notes)
        case StartMonitoring():
            fields = dict(intent=command.intent, summary=command.summary,
                          ticket=command.ticket, outcome=Outcome.PENDING,
                          evidence=command.evidence, notes=command.notes)
        case ExtendMonitoring():
            fields = dict(
                intent=command.intent, outcome=Outcome.PENDING,
                summary=f"extended the monitoring window to {command.until}",
                notes=command.notes)
        case Resolve():
            fields = dict(intent="close the incident", summary=command.summary,
                          ticket=command.ticket, outcome=Outcome.SUCCEEDED,
                          evidence=command.evidence,
                          notes=_notes_with_risk(command))
        case Reopen():
            fields = dict(intent="reopen the incident", summary=command.reason,
                          outcome=Outcome.FAILED, evidence=command.evidence,
                          notes=command.notes)
        case Merge():
            kept = incident_path(command.into)[:-3]
            fields = dict(
                intent="fold a duplicate into the page that keeps it",
                summary=f"duplicate of {kept}", outcome=Outcome.SUCCEEDED,
                notes=_notes_with_supersede(kept, command.notes))
        case _:
            raise TypeError(f"{type(command).__name__} is not a command")
    return ActionRecord(at=at, kind=KINDS[type(command)], actor=actor.email,
                        status_after=status_after,
                        window=_window_after(incident, command, at), **fields)


def _merged_into(tree: Tree, dropped: Incident, dropped_text: str,
                 command: Merge, actor: Actor, at: str) -> tuple[str, str]:
    """The kept page's path, and the bytes a merge leaves it holding.

    Refuses a pair that is not one: a page cannot fold into itself, into a
    page this revision does not hold, into another database's page, or into
    one whose own status is off-vocabulary, which would park carried evidence
    under a status no reader can classify.

    The carry-over runs before `append_action`, so the record lands at the end
    of the page as it does everywhere else, and the status write repeats the
    kept page's own status and window: a merge is a fact about the duplicate,
    never a transition of the page that survives it."""
    path = incident_path(command.into)
    target = dropped.path[:-3]
    if path == dropped.path:
        raise MergeRefused(dropped.path, path,
                           f"{dropped.path} cannot be merged into itself")
    text = tree.read(path)
    if text is None:
        raise MergeRefused(dropped.path, path,
                           f"no incident page at {path} in {tree.revision}; "
                           f"{dropped.path} was not merged")
    kept = read_incident(text, path)
    if kept.db != dropped.db:
        raise MergeRefused(dropped.path, path,
                           f"{dropped.path} is {dropped.db!r} and {path} is "
                           f"{kept.db!r}; a merge folds one database's "
                           f"duplicate pages together")
    if kept.unknown_status is not None:
        raise MergeRefused(dropped.path, path,
                           f"{path} carries an off-vocabulary status "
                           f"{kept.unknown_status!r}; repair it before "
                           f"{dropped.path} is merged into it")
    record = ActionRecord(
        at=at, kind=KINDS[RecordAction], actor=actor.email,
        intent="keep one page for this db and day",
        summary=f"merged {target} into this page", status_after=kept.status,
        outcome=Outcome.SUCCEEDED, window=kept.monitoring,
        notes=f"Folded [[{target}]] in as a duplicate of the same db and day.")
    carried = _carry_updates(_carry_evidence(text, dropped_text), dropped_text)
    return path, set_status(append_action(carried, record), kept.status,
                            updated=at, monitoring=kept.monitoring)


def _evidence_lines(text: str) -> list[tuple[tuple[str, str], str]]:
    """Every `- <day>: digests/...` line of `## Evidence`, in page order,
    keyed by the day and the digest it cites. The rest of the line is the
    author's wording, which is what a key must not include."""
    out = []
    for heading, body in sections(text):
        if EVIDENCE_HEAD_RE.match(heading):
            out += [(match.groups(), line)
                    for line in body.splitlines()
                    if (match := EVIDENCE_LINE_RE.match(line))]
    return out


def _update_sections(text: str) -> list[tuple[str, str]]:
    """`(day, whole section)` for every `## Update <day>` block, in page
    order, which is day order on every page the ingest writer maintains."""
    return [(match.group(1), f"{heading}\n{body}")
            for heading, body in sections(text)
            if (match := UPDATE_HEAD_RE.match(heading))]


def _carry_evidence(kept: str, dropped: str) -> str:
    """Every evidence line the duplicate cites and the kept page does not,
    appended to the kept page's `## Evidence` section, which `section_append`
    creates when it has none.

    Keyed on (day, digest) and recomputed as the lines fold, so the kept
    page's own wording for a day it already cites survives untouched and a
    duplicate citing one digest twice yields one line.

    Evidence lines rather than one `## Update <today>` wrapper: the ingest
    writer owns the `## Update <day>` heading and replaces it wholesale, so
    carried evidence parked under today's heading on a still-open page would
    be gone the next time today's digest is ingested."""
    seen = {key for key, _ in _evidence_lines(kept)}
    for key, line in _evidence_lines(dropped):
        if key not in seen:
            seen.add(key)
            kept = section_append(kept, EVIDENCE_SECTION, line)
    return kept


def _carry_updates(kept: str, dropped: str) -> str:
    """Every `## Update <day>` section of the duplicate whose day the kept
    page has none of its own for, carried verbatim. Keyed on the day, so the
    kept page's own account of a day it already wrote up wins."""
    days = {day for day, _ in _update_sections(kept)}
    for day, section in _update_sections(dropped):
        if day not in days:
            days.add(day)
            kept = _insert_update(kept, day, section)
    return kept


def _insert_update(text: str, day: str, section: str) -> str:
    """`section` spliced in before the first `## Update` of a later day, after
    the last update when there is no later one, and at the end of a page that
    carries no updates at all.

    In day order rather than appended, because the run of `## Update <day>`
    sections is the incident's chronology: an 08-11 block after an 09-08 one
    makes the merged page misread. ISO days sort lexically, which is why the
    comparison needs no date parsing."""
    lines = text.rstrip("\n").splitlines()
    heads = [i for i, line in enumerate(lines) if line.startswith("## ")]
    updates = [(i, match.group(1)) for i in heads
               if (match := UPDATE_HEAD_RE.match(lines[i]))]
    at = next((i for i, other in updates if other > day), len(lines))
    if at == len(lines) and updates:
        at = next((i for i in heads if i > updates[-1][0]), len(lines))
    head = lines[:at]
    while head and not head[-1].strip():
        head.pop()
    return nl("\n".join(head + ([""] if head else [])
                        + section.strip("\n").splitlines()
                        + [""] + lines[at:]))


def _notes_with_supersede(kept_target: str, notes: str) -> str:
    """The dropped page's notes: where the incident went, then whatever the
    operator added, as its own paragraph. One blank line, never two, because
    a run of three newlines is what the next ingest would collapse."""
    notes = notes.strip()
    supersede = f"Superseded by [[{kept_target}]]."
    return f"{supersede}\n\n{notes}" if notes else supersede


def _notes_with_risk(command: Resolve) -> str:
    """`residual_risk` has no field of its own in the record, so it becomes a
    labelled first paragraph of `notes`, the record's only multi-line field.
    Stripped on both sides, because a blank-line pair in notes is what the
    next ingest would collapse."""
    risk, notes = command.residual_risk.strip(), command.notes.strip()
    if not risk:
        return notes
    return f"Residual risk: {risk}\n\n{notes}" if notes else f"Residual risk: {risk}"


def _window_after(incident: Incident, command: Command,
                  at: str) -> MonitoringWindow | None:
    """The window in effect after the command. StartMonitoring: a new window
    from `at`. ExtendMonitoring: the existing signal and start with the new
    `until`. Resolve, Reopen and Merge: None. Otherwise unchanged."""
    match command:
        case StartMonitoring(signal=signal, until=until):
            return MonitoringWindow(signal, at, until)
        case ExtendMonitoring(until=until):
            window = incident.monitoring
            if window is None:
                raise ActionMalformed(
                    f"window: {incident.path} carries no readable monitoring "
                    f"window to extend")
            return MonitoringWindow(window.signal, window.start, until)
        case Resolve() | Reopen() | Merge():
            return None
        case _:
            return incident.monitoring
