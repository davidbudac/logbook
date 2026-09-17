"""What we did last time, read off our own incident corpus.

`research` tells an operator what an ORA code means according to Oracle and a
few approved sites. It never reads our own history, and the history is already
there: every operator act on an incident is an `## Action <ts>` record carrying
a summary, an outcome, a rollback and a ticket. Only one kind of record ever
reached an error page — a `resolve`, as one `## Resolution history` row written
in the resolve transaction — so what was *tried*, what was *rejected*, and
whether a fix *held* were invisible to the person opening `errors/ORA-1653.md`
during the next incident.

`## Past fixes` is that view. No model call and no web: it is a pure function
of the incident corpus and the error page's own `## Occurrences` table, so the
same inputs produce the same bytes and the transaction returns `NothingToDo`
when nothing changed. That is what lets it be regenerated wholesale on every
run with no frontmatter watermark and no `limit`.

Two sections, two owners, and they must not be confused. `## Resolution
history` is lifecycle-owned and append-only, written at the moment of
resolving; nothing here touches it. `## Past fixes` is derived and disposable:
it is rewritten from the corpus every run and removed outright when the corpus
no longer justifies it.

Everything is pure over text. `apply` is the only function that touches disk.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .incidents import ActionRecord, Outcome, by_error_code, load_incidents
from .lifecycle import RESOLUTION_SECTION
from .pagetext import log_append, nl, oneline
from .readmodel import Occurrence, parse_occurrences
from .research import URL_RE

HELD_AFTER_DAYS = 14

PAST_FIXES_SECTION = "Past fixes"
PAST_FIXES_HEAD = f"## {PAST_FIXES_SECTION}"
TABLE_HEAD = ("| when | db | incident | what was done | outcome | held |\n"
              "|---|---|---|---|---|---|")

#: Record kinds that carry fix text. `extend-monitoring` has a generated
#: summary and `merge` a generated `duplicate of <kept>`; neither says
#: anything about a fix.
FIX_KINDS = frozenset({"record-action", "resolve", "reopen",
                       "start-monitoring"})

#: Merge bookkeeping is written as an ordinary `record-action` on the page
#: that survives the merge, with this intent. It is filing, not a fix.
MERGE_INTENT = "keep one page for this db and day"


class Verdict(StrEnum):
    """What happened *after* a fix, which is the question the operator is
    actually asking. Six states, computed once by `verdict_for`, so no caller
    reassembles it from an outcome plus a status plus a date comparison."""

    OPEN = "open"
    REOPENED = "reopened"
    RECURRED = "recurred"
    HELD = "held"
    TOO_SOON = "too soon"
    NA = "n/a"


#: Presentation order for equal counts in the summary line, so a page's
#: summary is byte-stable across runs that change nothing.
_VERDICT_ORDER = {v: i for i, v in enumerate(Verdict)}


@dataclass(frozen=True)
class PastFix:
    """One row: one action record on one incident, judged."""

    code: str
    incident: str
    db: str
    day: str
    kind: str
    action: str
    outcome: Outcome
    verdict: Verdict
    verdict_day: str | None = None

    def held_cell(self, held_after_days: int) -> str:
        """The `held` column. A reopen and a recurrence both carry the day
        they happened, which is the whole value of the column: `reopened
        2026-07-20` says the fix lasted six days."""
        match self.verdict:
            case Verdict.HELD:
                return f"held ({held_after_days}d)"
            case Verdict.REOPENED | Verdict.RECURRED if self.verdict_day:
                return f"{self.verdict} {self.verdict_day}"
            case _:
                return str(self.verdict)


@dataclass(frozen=True)
class FixHistory:
    """Every judged fix for one error code, newest first.

    `held_after_days` travels with the history because it is what the `held`
    column prints, and a page must not need the config to be rendered."""

    code: str
    fixes: tuple[PastFix, ...] = ()
    held_after_days: int = HELD_AFTER_DAYS

    def summary_line(self) -> str:
        """`5 fixes across 3 incidents on 2 databases: 3 held, 1 reopened,
        1 recurred`. The verdict counts sum to the fix count, so a reader can
        check the line against the table under it."""
        counts: dict[Verdict, int] = {}
        for fix in self.fixes:
            counts[fix.verdict] = counts.get(fix.verdict, 0) + 1
        tail = ", ".join(
            f"{n} {verdict}" for verdict, n in
            sorted(counts.items(), key=lambda kv: (-kv[1],
                                                   _VERDICT_ORDER[kv[0]])))
        incidents = len({f.incident for f in self.fixes})
        dbs = len({f.db for f in self.fixes})
        return (f"{_plural(len(self.fixes), 'fix', 'fixes')} across "
                f"{_plural(incidents, 'incident')} on "
                f"{_plural(dbs, 'database')}: {tail}")


def _plural(n: int, one: str, many: str = "") -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _cell(text: str) -> str:
    """One table cell from operator prose: URLs stripped, whitespace
    collapsed, `|` escaped.

    The URLs go because `orchestrate._research_problems` runs
    `research.unapproved_urls` over the whole changed error page, so a link an
    operator pasted into a summary would block this stage's commit on a rail
    meant for cited research. The page keeps the fact and drops the link."""
    return oneline(URL_RE.sub("", text))


def _action_cell(record: ActionRecord) -> str:
    """What was done, with the rollback and the ticket the work was raised
    under. Both belong on the row: the next operator wants to know how to back
    the fix out before they want to know anything else about it."""
    extras = [f"{label}: {_cell(value)}"
              for label, value in (("rollback", record.rollback),
                                   ("ticket", record.ticket))
              if value.strip()]
    summary = _cell(record.summary)
    return f"{summary} ({'; '.join(extras)})" if extras else summary


def is_fix(record: ActionRecord) -> bool:
    """Whether this record says something about a fix. Merge bookkeeping is
    the one `record-action` that does not (see MERGE_INTENT)."""
    if record.kind not in FIX_KINDS:
        return False
    return not (record.kind == "record-action"
                and record.intent.strip() == MERGE_INTENT)


def verdict_for(record: ActionRecord, later: list[ActionRecord],
                occurrences: tuple[Occurrence, ...], *, db: str,
                today: dt.date,
                held_after_days: int) -> tuple[Verdict, str | None]:
    """The one place a fix is judged, from the record, the records that follow
    it on the same incident, and the error page's occurrence table.

    Order matters and each step is a stronger signal than the one under it. A
    rejected or failed outcome — and a `reopen`, which is the record of a fix
    coming undone rather than a fix — is not a fix whose durability is worth
    asking about. A later `reopen` is a human saying it came back, which beats
    anything the occurrence table implies. A fix on an incident nobody has
    resolved yet is still in flight. Only then does recurrence on the same
    database count, and only then can silence be read as the fix holding."""
    if record.kind == "reopen" or record.outcome in (Outcome.REJECTED,
                                                     Outcome.FAILED):
        return Verdict.NA, None
    reopen = next((r for r in later if r.kind == "reopen"), None)
    if reopen is not None:
        return Verdict.REOPENED, reopen.at[:10]
    if record.kind != "resolve" and not any(r.kind == "resolve" for r in later):
        return Verdict.OPEN, None
    day = record.at[:10]
    recurred = min((o.day for o in occurrences if o.db == db and o.day > day),
                   default=None)
    if recurred is not None:
        return Verdict.RECURRED, recurred
    if (today - dt.date.fromisoformat(day)).days >= held_after_days:
        return Verdict.HELD, None
    return Verdict.TOO_SOON, None


def gather(wiki_root, today: dt.date,
           held_after_days: int = HELD_AFTER_DAYS, *,
           codes: Sequence[str] | None = None) -> dict[str, FixHistory]:
    """Every error page's fix history, from one `load_incidents` walk.

    Keyed by error code, and only for codes that have at least one fix: a code
    with none needs its section removed, which is the caller's business and
    needs no history object to say so.

    `codes` narrows the walk to the pages it names, because one ingest tick
    asks about the handful of codes in today's digest and reading — and
    judging — every error page in the estate to answer that is work nobody
    wants. None is the stage's own case: every page, judged.

    Every row's `[[incidents/<slug>]]` resolves because `load_incidents` is
    the only way an incident gets in here, and it yields one Incident per
    `incidents/*.md` page it could read. That is the whole defence against
    `lint`'s `wikilink-broken` rule, which would otherwise block this stage's
    commit on a link to a page nobody can open."""
    root = Path(wiki_root)
    wanted = None if codes is None else set(codes)
    linked = by_error_code(load_incidents(root))
    out: dict[str, FixHistory] = {}
    for page in sorted((root / "errors").glob("*.md")):
        code = page.stem
        if wanted is not None and code not in wanted:
            continue
        occurrences = parse_occurrences(code, page.read_text())
        rows: list[tuple[str, str, PastFix]] = []
        for inc in linked.get(code, []):
            records = sorted(inc.actions.records, key=lambda r: r.at)
            for i, record in enumerate(records):
                if not is_fix(record):
                    continue
                verdict, day = verdict_for(
                    record, records[i + 1:], occurrences, db=inc.db,
                    today=today, held_after_days=held_after_days)
                rows.append((record.at, inc.slug, PastFix(
                    code=code, incident=inc.slug, db=inc.db,
                    day=record.at[:10], kind=record.kind,
                    action=_action_cell(record), outcome=record.outcome,
                    verdict=verdict, verdict_day=day)))
        if rows:
            rows.sort(key=lambda r: r[:2], reverse=True)
            out[code] = FixHistory(code, tuple(fix for _, _, fix in rows),
                                   held_after_days)
    return out


def render(history: FixHistory) -> str:
    """The section body: the summary sentence, then the table, newest first.

    No heading — `splice_section` owns where the section sits."""
    rows = "\n".join(
        f"| {fix.day} | {oneline(fix.db)} | [[incidents/{fix.incident}]] "
        f"| {fix.action} | {fix.outcome} "
        f"| {fix.held_cell(history.held_after_days)} |"
        for fix in history.fixes)
    return f"{history.summary_line()}.\n\n{TABLE_HEAD}\n{rows}"


def _span(lines: list[str], heading: str) -> tuple[int, int] | None:
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip().casefold() == heading.casefold()), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return start, end


def splice_section(text: str, body: str | None) -> str:
    """Make `## Past fixes` say exactly `body`, or take it off the page when
    `body` is None.

    The section is owned wholesale, so this replaces it heading and all rather
    than editing rows. Not `pagetext.day_block_replace`, which collapses runs
    of blank lines across the whole page: this section lands next to
    `## Reference` paragraphs and hand-written human sections, and their line
    spacing is theirs.

    A new section goes immediately after `## Resolution history` — the two
    read together — or at the end of the page when there is none. Sections
    after the target never move.

    Twice: identical bytes, because the second pass removes exactly what the
    first wrote and puts the same block back in the same place."""
    lines = nl(text).rstrip("\n").split("\n")
    span = _span(lines, PAST_FIXES_HEAD)
    if span is None:
        if body is None:
            return nl(text)
        resolution = _span(lines, f"## {RESOLUTION_SECTION}")
        at = resolution[1] if resolution is not None else len(lines)
    else:
        at, end = span
        lines = lines[:at] + lines[end:]
    head, tail = lines[:at], lines[at:]
    while head and not head[-1].strip():
        head.pop()
    block = [] if body is None else \
        [""] + [PAST_FIXES_HEAD, ""] + body.rstrip("\n").split("\n")
    return nl("\n".join(head + block + ([""] + tail if tail else [])))


def apply(wiki_root, rel_page: str, history: FixHistory | None,
          today: dt.date) -> bool:
    """Write one error page's `## Past fixes` section, returning whether the
    page changed.

    An unchanged page is the common case — the corpus moves rarely and this
    stage runs daily — so it costs one read and nothing else: no write, no
    `log.md` line, and therefore no commit for the transaction to resolve as
    `NothingToDo`. An empty history removes the section; the wiki never
    carries a heading with nothing under it.

    No frontmatter change. This section is derived, and a `researched:`-style
    watermark would claim a review that nobody performed."""
    root = Path(wiki_root)
    page = root / rel_page
    text = page.read_text()
    fixes = history.fixes if history is not None else ()
    after = splice_section(text, render(history) if fixes else None)
    if after == text:
        return False
    page.write_text(after)
    log_path = root / "log.md"
    log_line = (f"[{today.isoformat()}] research (history) — {rel_page}: "
                f"{len(fixes)} fix(es)")
    log_path.write_text(log_append(
        log_path.read_text() if log_path.exists() else None, log_line))
    return True
