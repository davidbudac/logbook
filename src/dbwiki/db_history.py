"""The wiki's memory of one database, assembled for a prompt.

The ingest model judges one day with no file access, so whatever it is to know
about the days before today has to arrive in the prompt. Five lists answer
that: what the operators changed, what the journal recorded, which incidents
have already been closed, what was actually done to them, and whether the
fixes for today's codes held. The open incidents are not among them; both
prompts print those for themselves, directly above this block.

They are read straight off the same parsers the portal's read model uses —
`readmodel.parse_journal`, `incidents.load_incidents`, `past_fixes.gather`,
`changes.recent` — rather than out of a `readmodel.Snapshot`. Two reasons, and
either alone decides it. A Snapshot is a whole-wiki git-pinned build (ls-tree,
batch read, git log) and paying that per database per tick to produce four
short lists is the wrong cost. And the ready-made readers that would have
saved the work, `advisory.SOURCES`, all take an incident as their subject:
ingest has a digest and no incident, so there is nothing to hand them. Hence
no `advisory` import here, and no Snapshot.

Everything is context, never evidence. The block says so in its own heading
and the prompt rule says so again, because the failure this module could
cause is a model restating last month's incident as a fact of today's digest.

The module sits above `structured` the way `advisory` does, and takes
`MAX_LINE` and `_cap` from it; `structured.build_prompt` imports back into
here from inside the function, which is what keeps that an edge and not a
cycle.
"""

import datetime as dt
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import changes, past_fixes
from .incidents import Incident, load_incidents
from .pagetext import oneline
from .readmodel import JournalEntry, parse_journal
from .structured import MAX_LINE, _cap

MAX_HISTORY_CHARS = 5000   # the whole block, through structured._cap
MAX_FIXES = 6         # Past fixes for today's codes
MAX_ACTIONS = 8       # Operator actions
MAX_INCIDENTS = 5     # Resolved incidents
MAX_CHANGES = 12      # Changes, folded one line per day and rule
MAX_JOURNAL = 7       # Journal (days with events)

#: Action records that say what a human did about an incident.
#: `start-monitoring`, `extend-monitoring` and `merge` are bookkeeping, and a
#: prompt line spent on one is a prompt line not spent on a fix. So is the
#: `record-action` a merge writes on the surviving page, which
#: `past_fixes.is_fix` already knows how to tell apart.
OPERATOR_KINDS = frozenset({"record-action", "resolve", "reopen"})


@dataclass(frozen=True)
class ChangeLine:
    """Every change one rule made on one day, folded into one prompt line.

    A single shutdown-plus-startup fires eight to eleven lifecycle groups, so
    unfolded one restart spends the whole change budget and the rest of the
    window never reaches the model."""

    day: str
    rule: str
    count: int
    headlines: tuple[str, ...]


@dataclass(frozen=True)
class IncidentSummary:
    """One incident as a single prompt line's worth of fact. `resolved` is the
    day it was last resolved, which the page's status cannot say: a reopened
    incident is open again and still carries the day the first fix ran out."""

    path: str
    title: str
    status: str
    opened: str
    resolved: str | None


@dataclass(frozen=True)
class OperatorAction:
    """One human act on one incident. `incident` is the slug, not the path:
    the line already reads long and the path adds nothing the slug does not."""

    day: str
    incident: str
    kind: str
    outcome: str
    summary: str


@dataclass(frozen=True)
class DbHistory:
    """What the wiki remembers about `db` over the `days` before `today`.

    Frozen and complete: `render` is a pure function of this, so the same tree
    produces the same block and a test can assert that byte for byte."""

    db: str
    today: str
    days: int
    changes: tuple[ChangeLine, ...]
    journal: tuple[JournalEntry, ...]
    incidents: tuple[IncidentSummary, ...]
    actions: tuple[OperatorAction, ...]
    fixes: tuple[past_fixes.PastFix, ...]

    def is_empty(self) -> bool:
        return not (self.changes or self.journal or self.incidents
                    or self.actions or self.fixes)


def gather(wiki_root: Path, db: str, *, today: dt.date,
           codes: Sequence[str], days: int) -> DbHistory:
    """Read the five lists for one database, each ordered and capped.

    The window is `today - days <= day < today` — today's own digest is the
    evidence this history is context for, so it is never in here. `fixes` is
    the exception and carries no window at all: a fix from six months ago that
    held is exactly the thing the model wants to know about today's code.

    `days <= 0` disables the block and touches no disk."""
    root = Path(wiki_root)
    if days <= 0:
        return DbHistory(db=db, today=today.isoformat(), days=days,
                         changes=(), journal=(), incidents=(), actions=(),
                         fixes=())
    start = (today - dt.timedelta(days=days)).isoformat()
    end = today.isoformat()

    def in_window(day: str) -> bool:
        return start <= day < end

    mine = _incidents_of(root, db)
    return DbHistory(
        db=db, today=today.isoformat(), days=days,
        changes=_changes(root, db, today, days),
        journal=_journal(root, db, in_window),
        incidents=_summaries(mine, in_window),
        actions=_actions(mine, in_window),
        fixes=_fixes(root, db, codes, today))


def _changes(root: Path, db: str, today: dt.date,
             days: int) -> tuple[ChangeLine, ...]:
    """The window's changes, folded per day and rule, newest day first.

    Within a day the rules keep the order their first change arrived in, and
    each line carries the distinct headlines in that same order — a restart
    reads as one line naming the close, the mount and the open rather than as
    eleven lines the reader has to reassemble."""
    counts: dict[tuple[str, str], int] = {}
    first_ts: dict[tuple[str, str], str] = {}
    heads: dict[tuple[str, str], dict[str, None]] = {}
    for c in sorted(changes.recent(root, db, today=today, days=days),
                    key=lambda c: (c.ts, c.rule)):
        key = (c.day, c.rule)
        counts[key] = counts.get(key, 0) + c.count
        first_ts.setdefault(key, c.ts)
        if c.message:
            heads.setdefault(key, {})[c.message] = None
    lines = [ChangeLine(day=day, rule=rule, count=count,
                        headlines=tuple(heads.get((day, rule), {})))
             for (day, rule), count in counts.items()]
    lines.sort(key=lambda ln: first_ts[(ln.day, ln.rule)])
    lines.sort(key=lambda ln: ln.day, reverse=True)
    return tuple(lines[:MAX_CHANGES])


def _journal(root: Path, db: str,
             in_window: Callable[[str], bool]) -> tuple[JournalEntry, ...]:
    entries: list[JournalEntry] = []
    for page in sorted((root / "databases" / db / "journal").glob("*.md")):
        try:
            text = page.read_text()
        except OSError:
            continue
        entries.extend(
            e for e in parse_journal(db, page.relative_to(root).as_posix(),
                                     text)
            if in_window(e.day) and not _quiet(root, db, e.day))
    entries.sort(key=lambda e: e.path)
    entries.sort(key=lambda e: e.day, reverse=True)
    return tuple(entries[:MAX_JOURNAL])


def _quiet(root: Path, db: str, day: str) -> bool:
    """Whether the digest for `day` says the day had nothing to report.

    The digest is the one honest judge of that — `notable` is what the
    compactor and the last ingest agreed on, and a lifecycle change is an
    event even when nothing was notable. Never the headline text: a headline
    saying "quiet day" is prose, and prose is not a filter.

    A day the fleet shipped no events at all for is the emptiest kind of
    quiet, and `notable` will not say so: a silence delta sets `notable`, so
    the digest for a blackout day is marked notable and its journal line says
    only that nothing arrived. `totals` is read defensively, because a digest
    malformed enough to lose that key must not be called quiet by accident.

    A day with no digest, or one that will not open or parse, is not quiet.
    Its journal entry is then the only surviving record of the day, and
    dropping it would lose it silently."""
    try:
        digest = json.loads((root / "digests" / db / f"{day}.json").read_text())
        if digest.get("totals", {}).get("events") == 0:
            return True
        return not digest.get("notable") and not changes.of_digest(digest)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _incidents_of(root: Path, db: str) -> list[Incident]:
    """Every incident page for this database.

    A wiki whose first incident has not been opened yet survives
    `load_incidents` today only because `Path.glob` swallows the missing
    directory. That is an implementation detail of the interpreter, not a
    promise this module wants to rest a nightly prompt on, so the directory
    is checked here."""
    if not (root / "incidents").is_dir():
        return []
    return [i for i in load_incidents(root) if i.db == db]


def _summaries(mine: list[Incident],
               in_window: Callable[[str], bool]) -> tuple[IncidentSummary, ...]:
    """Incidents opened or touched in the window that are no longer active,
    newest first. Stacked stable sorts, least significant key first, as
    `advisory._by_shared_codes` orders its neighbours.

    The active ones are deliberately absent: both prompts already print every
    open incident for this database above the block, and a second copy of
    fourteen lines is budget spent restating what the model just read."""
    found = [i for i in mine if not i.is_active
             and (in_window(i.opened[:10]) or in_window(i.updated[:10]))]
    found.sort(key=lambda i: i.path)
    found.sort(key=lambda i: i.opened, reverse=True)
    return tuple(_summary(i) for i in found[:MAX_INCIDENTS])


def _summary(incident: Incident) -> IncidentSummary:
    resolved = max((r.at for r in incident.actions.records
                    if r.kind == "resolve"), default="")
    return IncidentSummary(path=incident.path, title=incident.title,
                           status=str(incident.status),
                           opened=incident.opened,
                           resolved=resolved[:10] or None)


def _actions(mine: list[Incident],
             in_window: Callable[[str], bool]) -> tuple[OperatorAction, ...]:
    """Every operator act in the window, newest first, over every incident of
    this database — including the ones whose own dates fall outside it. An act
    performed last week on an incident opened last year is this week's news."""
    rows = [(r.at, i.slug, r.kind,
             OperatorAction(day=r.at[:10], incident=i.slug, kind=r.kind,
                            outcome=str(r.outcome),
                            summary=oneline(r.summary)[:MAX_LINE]))
            for i in mine for r in i.actions.records
            if r.kind in OPERATOR_KINDS and past_fixes.is_fix(r)
            and in_window(r.at[:10])]
    rows.sort(key=lambda row: row[1:3])
    rows.sort(key=lambda row: row[0], reverse=True)
    return tuple(row[3] for row in rows[:MAX_ACTIONS])


def _fixes(root: Path, db: str, codes: Sequence[str],
           today: dt.date) -> tuple[past_fixes.PastFix, ...]:
    if not codes or not (root / "errors").is_dir():
        return ()
    rows = [fix
            for history in past_fixes.gather(root, today, codes=codes).values()
            for fix in history.fixes if fix.db == db]
    rows.sort(key=lambda f: (f.day, f.code, f.incident), reverse=True)
    return tuple(rows[:MAX_FIXES])


def _join_headlines(headlines: Sequence[str]) -> str:
    """Headlines as one clause list.

    Oracle writes its own statement terminator, so a plain `"; ".join` stutters
    into `SCOPE=BOTH;; ALTER SYSTEM …`, which reads as a corrupted line rather
    than as two statements. The separator collapses onto a semicolon the
    headline already carries instead of trimming the headline, so every
    character of the alert-log line survives."""
    out = ""
    for head in headlines:
        if out:
            out += " " if out.endswith(";") else "; "
        out += head
    return out


def _change_line(line: ChangeLine) -> str:
    tail = _join_headlines(line.headlines)
    return (f"- {line.day} {line.rule} ×{line.count}"
            + (f" — {tail}" if tail else ""))


def _journal_line(entry: JournalEntry) -> str:
    return f"- {entry.day} — {entry.headline}"


def _incident_line(summary: IncidentSummary) -> str:
    state = f"resolved {summary.resolved}" if summary.resolved \
        else summary.status
    return f"- {summary.path} — {summary.title} ({state})"


def _action_line(action: OperatorAction) -> str:
    return (f"- {action.day} {action.kind} ({action.outcome}) "
            f"on {action.incident}: {action.summary}")


def _fix_line(fix: past_fixes.PastFix) -> str:
    return (f"- {fix.code} · {fix.day} · {fix.action} · {fix.outcome} · "
            f"{fix.held_cell(past_fixes.HELD_AFTER_DAYS)}")


def render(history: DbHistory) -> str:
    """The prompt block, or `""` when there is nothing to say.

    A label whose list is empty is omitted outright rather than printed over
    `(none)`: an absent section says nothing, and `- (none)` is a line the
    model has to read and then discard.

    The order is the cap's order. `_cap` truncates the tail, so the lists that
    answer "has this been fixed before, and by whom" come first and the
    journal goes last: it is the most restateable of the five and the least
    actionable, and it is the one the model can most afford to lose."""
    if history.is_empty():
        return ""
    lines = [f"Database history for {history.db} (last {history.days} days; "
             "context from earlier days, not evidence for today):"]
    for label, rendered in (
            ("Past fixes for today's codes",
             [_fix_line(f) for f in history.fixes]),
            ("Operator actions", [_action_line(a) for a in history.actions]),
            ("Resolved incidents",
             [_incident_line(i) for i in history.incidents]),
            ("Changes (from digests)",
             [_change_line(c) for c in history.changes]),
            ("Journal (days with events)",
             [_journal_line(e) for e in history.journal])):
        if rendered:
            lines.append(f"{label}:")
            lines.extend(rendered)
    return _cap("\n".join(line[:MAX_LINE] for line in lines),
                MAX_HISTORY_CHARS)
