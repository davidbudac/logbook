"""The revision-pinned read model behind the portal's navigation views (2.1).

A `Snapshot` is immutable and built from exactly one revision: paths from
`gitutil.ls_tree`, text from one `transaction.Tree.batch_at`, ownership from
one `transaction.last_commits`. Digest pages stay in the inventory for link
resolution and are never read, the `lint_wiki` rule that keeps 787 of 982
files closed.

It never serves the incident work queue. The queue reads the working tree and
its path order is the report pipeline's positional identity; this model
carries incidents for navigation only and never numbers them.
"""

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path

from . import gitutil, lint, transaction
from .incidents import Incident, load_incidents_from
from .pagetext import frontmatter, oneline, sections
from .transaction import Commit

ERROR_PAGE_RE = re.compile(r"\Aerrors/([^/]+)\.md\Z")
JOURNAL_PAGE_RE = re.compile(r"\Adatabases/([^/]+)/journal/\d{4}-\d{2}\.md\Z")
#: The incident pages, with the slug captured. One definition rather than a
#: second grouped copy beside it: every existing caller tests the match for
#: truth and is unaffected by the group.
INCIDENT_PAGE_RE = re.compile(r"\Aincidents/([^/]+)\.md\Z")
DB_PAGE_RE = re.compile(r"\Adatabases/([^/]+)\.md\Z")
REPORT_PAGE_RE = re.compile(
    r"\Areports/(\d{4}-\d{2}-\d{2})(?:-(\d{4}))?\.md\Z")

CAUSE_RE = re.compile(r"\A\*\*Cause:\*\*\s*")
ACTION_RE = re.compile(r"\A\*\*Action:\*\*\s*")
NOTE_RE = re.compile(r"\A\*\*Practitioner note:\*\*\s*")
CITATION_RE = re.compile(
    r"\s*\(source:\s*(?P<source>[^;)]+?)"
    r"(?:;\s*url:\s*<?(?P<url>[^;>)]+?)>?)?"
    r"(?:;\s*accessed:\s*(?P<accessed>[^;)]+?))?\s*\)\.?\s*\Z")

OCCURRENCE_ROW_RE = re.compile(
    r"\A\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]*?)\s*\|\s*(.*?)\s*\|"
    r"\s*([^|]*?)\s*\|\Z")

RESOLUTION_ROW_RE = re.compile(
    r"\A\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]*?)\s*\|"
    r"\s*\[\[incidents/([^\]|]+?)\]\]\s*\|\s*(.*?)\s*\|"
    r"\s*([^|]*?)\s*\|\Z")

JOURNAL_HEAD_RE = re.compile(
    r"\A##\s+(\d{4}-\d{2}-\d{2})\s+—\s*(.*?)\s*\Z")

SUBJECT_PREFIX_RE = re.compile(r"\A(\S+?)\s*[:—]")

SNIPPET_CONTEXT = 30

#: How far back the incident-touch join walks the wiki's history, in days.
#: A bound on one git log and never on what an operator may ask to see: the
#: agents view slices its own 24, 48 or 168 hours out of this. Thirty days of
#: this wiki is a walk of about forty milliseconds.
TOUCH_WINDOW_DAYS = 30


class Origin(StrEnum):
    """Who effectively wrote a page, which the git author cannot say: all
    1052 live commits share one author. Derived from the last commit's `Actor:`
    trailer, then its subject prefix, then the path."""

    MACHINE = "machine"
    AGENT = "agent"
    OPERATOR = "operator"
    HAND = "hand"


PREFIX_ORIGIN: Mapping[str, Origin] = {
    "digest": Origin.MACHINE,
    "html": Origin.MACHINE,
    "ingest": Origin.AGENT,
    "report": Origin.AGENT,
    "research": Origin.AGENT,
    "lint": Origin.AGENT,
}


def origin_of(path: str, commit: Commit | None) -> Origin:
    """Precedence: the `Actor:` trailer (OPERATOR), then the subject prefix
    through `PREFIX_ORIGIN`, then `transaction.is_machine(path)` (MACHINE),
    then HAND. A page with no commit is HAND.

    `commit.author` is never read: every commit in the live wiki carries the
    same author email, so the field cannot separate two writers.

    The `:` or em dash delimiter is required rather than matching the bare
    word: a hand-written subject beginning "report" is not an agent commit. A
    prefix that carries the delimiter but is absent from the table falls
    through to the path and then to HAND, so a new agent's commits read as
    whatever their path already says instead of as a wrong attribution."""
    if commit is None:
        return Origin.HAND
    if commit.actor:
        return Origin.OPERATOR
    match = SUBJECT_PREFIX_RE.match(commit.subject)
    if match is not None:
        origin = PREFIX_ORIGIN.get(match.group(1).casefold())
        if origin is not None:
            return origin
    return Origin.MACHINE if transaction.is_machine(path) else Origin.HAND


@dataclass(frozen=True)
class PageInfo:
    """One curated page's navigable surface: what it calls itself, which
    database it belongs to, its `## ` headings, and who last wrote it."""

    path: str
    type: str
    title: str
    db: str
    headings: tuple[str, ...]
    origin: Origin
    commit: Commit | None


@dataclass(frozen=True)
class Occurrence:
    """One row of an error page's `## Occurrences` table: this code was seen
    on this day on this database, with this digest as evidence."""

    code: str
    day: str
    db: str
    note: str
    evidence: str


@dataclass(frozen=True)
class Citation:
    """Where one research paragraph came from: the `sources/<slug>` page it
    was read through, the URL that was fetched, and the day it was fetched.
    `url` and `accessed` are `""` when the citation names only the source,
    the shape a hand-written `(source: sources/x).` takes."""

    source: str
    url: str
    accessed: str


@dataclass(frozen=True)
class Note:
    """One `**Practitioner note:**` paragraph `research_caveats.apply_caveats`
    wrote under `## Reference`: something an approved source says that
    Oracle's own cause and action do not.

    `citation` is a field and not part of `text` for the reason `Research`
    keeps its citations as fields: the writer puts the
    `(source: ...; url: ...; accessed: ...)` parenthesis on the paragraph's
    second physical line, and a screen that printed it inline would show the
    reader a URL in the middle of a sentence. A note is a claim from one
    named page, so its provenance travels with it rather than joining the
    page-level list, where a reader could not tell which sentence it backs.

    `citation` is None on a paragraph that carries none. The writer always
    writes one, so that shape comes from a hand edit, and the prose is worth
    showing without it."""

    text: str
    citation: Citation | None


@dataclass(frozen=True)
class Research:
    """What `research_structured.apply_research` wrote onto one error page:
    the day it was researched, the cause and action paragraphs, and the
    citations those paragraphs carried.

    `cause` and `action` are the prose alone. The writer puts the
    `(source: ...; url: ...; accessed: ...)` citation on the paragraph's last
    line, and a screen that printed it inline would show the reader a URL in
    angle brackets in the middle of a sentence; `citations` carries the same
    facts as fields, each distinct citation once in order of appearance, so
    the screen can draw one source line under the card.

    `notes` is every `**Practitioner note:**` paragraph the section carries,
    in page order, each with its own citation. Those paragraphs have a writer
    behind them, `research_caveats.apply_caveats`, so their shape is known
    and a screen can draw them as a list. Still not the whole section: a
    paragraph a human wrote by hand, the unbolded `Practitioner caveat:` on
    `errors/ORA-1013.md` among them, is prose of no known shape and stays on
    the page alone. The page is one click away and carries every word.

    `researched` is `""` on a page whose frontmatter has no `researched:`
    key. That cannot happen through the writer, which sets the key and the
    section in one pass, but it can through a hand edit, and the portal
    prefers an empty date to a page it refuses to describe."""

    code: str
    path: str
    researched: str
    cause: str
    action: str
    citations: tuple[Citation, ...]
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class Resolution:
    """One row of an error page's `## Resolution history` table: on this day,
    on this database, this incident was resolved by doing this, and here is
    what it left behind.

    `incident` is the bare slug and not the `[[incidents/<slug>]]` wikilink
    the row spells, because every consumer wants the slug: the portal builds
    a case-file path from it and the workbench builds its own hash link.
    A screen given the wikilink would have to unwrap it again.

    `evidence` is whatever the resolving verb had to hand, a digest path on a
    resolution that named one and the timestamp it closed at otherwise, so it
    is a string to show rather than a link to follow.

    Written by `lifecycle.build`'s Resolve arm, one row per `[[errors/<code>]]`
    the incident links. The cells arrive through `pagetext.oneline`, so a
    literal pipe inside one arrives backslash-escaped and stays that way here;
    the row is a fact about the page as written, not a re-rendering of it."""

    code: str
    day: str
    db: str
    incident: str
    remediation: str
    evidence: str


@dataclass(frozen=True)
class JournalEntry:
    """One `## <day> — <headline>` section of a database journal month."""

    db: str
    day: str
    headline: str
    path: str


@dataclass(frozen=True)
class Hit:
    """One search result. `snippet` is already collapsed to a single line."""

    path: str
    type: str
    title: str
    db: str
    snippet: str


@dataclass(frozen=True)
class Touch:
    """One incident page written by one loop tick.

    `run_id` is the `Run-ID:` trailer of the commit that wrote the page, which
    is the only thing joining the wiki's history to `.state/agent_runs.jsonl`:
    every machine commit carries it and the ledger keys every stage by it.

    `at` is a `datetime` and not the ISO string `transaction.Commit` carries,
    because this value exists to be compared against a window edge — the
    agents view asks for the last 24, 48 or 168 hours out of the 30 days built
    here — and that comparison is what parsing at this boundary buys. The wire
    formats it back."""

    run_id: str
    slug: str
    commit: str
    at: datetime


@dataclass(frozen=True)
class Snapshot:
    """Everything the navigation views read, pinned to one revision.

    Frozen and safe to share across threads: nothing here is computed lazily
    or mutated after `build` returns. Two builds at one revision with the same
    pinned `now` compare equal, which is what makes the model cacheable by
    revision, so every collection is an immutable or deterministically ordered
    value: tuples, plain dicts built in sorted order, and one frozenset. Dicts
    make the value unhashable; nothing needs to hash it.

    `inventory` holds every path at the revision, digests included, because a
    `[[link]]` to a digest resolves against what the wiki holds rather than
    against what this model read. `pages` and `text` cover the curated pages
    only.

    `incidents` is keyed by `Incident.slug` rather than being a sequence,
    because the positional reading of an incident list belongs to the report
    pipeline: `apply_report` resolves the model's numbers against
    `load_incidents` in path order at one revision. A mapping makes a silent
    re-numbering here unrepresentable, and views that want an order sort at
    render.

    `journals` is newest day first across every month file a database has,
    not within each file, so the first entry is the latest thing anybody wrote
    about that database whichever month it landed in.

    `research` is keyed by error code and holds only the pages that carry a
    `## Reference` block, so `code in snapshot.research` is the whole of
    "does the wiki know what this error means". The error pages are read for
    `occurrences` already, which is why carrying the answer costs one more
    parse rather than another pass over the tree.

    `resolutions` is keyed the same way and holds only the codes some incident
    has been resolved against, so `snapshot.resolutions.get(code, ())` answers
    "has anybody fixed this before, and what did they do" for a reader who has
    just been handed the code. It rides along with `research` for the same
    reason: the error page is already open in this loop, so the answer costs
    one more parse of text in hand rather than another pass over the tree.

    `touches_by_run` and `touches_by_incident` are the same `Touch` set keyed
    the two ways it is read: "what did this tick write" and "which ticks wrote
    this page". Both are here rather than a list a view joins itself, because
    a second grouping written at each call site is the same pass twice with
    two chances to disagree about which commits count.

    The set is bounded to `TOUCH_WINDOW_DAYS` and never to what an operator
    asked for. The bound is the cost of one git log; the window is a fact
    about when somebody is looking, and `api.agents` slices it out of this the
    way `api.heat` slices its span out of one built window.

    `report_of_day` follows `daily_html.report_rel`'s precedence rule, the
    consolidated `reports/<day>.md` beating the latest
    `reports/<day>-HHMM.md`. `build` computes it over the inventory instead of
    importing that function, which answers from the working tree."""

    revision: str
    built_at: str
    inventory: frozenset[str]
    pages: Mapping[str, PageInfo]
    text: Mapping[str, str]
    links: Mapping[str, tuple[str, ...]]
    backlinks: Mapping[str, tuple[str, ...]]
    incidents: Mapping[str, Incident]
    occurrences: tuple[Occurrence, ...]
    research: Mapping[str, Research]
    resolutions: Mapping[str, tuple[Resolution, ...]]
    journals: Mapping[str, tuple[JournalEntry, ...]]
    touches_by_run: Mapping[str, tuple[Touch, ...]]
    touches_by_incident: Mapping[str, tuple[Touch, ...]]
    report_of_day: Mapping[str, str]
    dbs: tuple[str, ...]

    def exists(self, path: str) -> bool:
        """Whether the wiki holds `path` at this revision. Answers for a
        digest too, which is why it reads `inventory` and not `pages`."""
        return path in self.inventory

    def db_incidents(self, db: str) -> tuple[Incident, ...]:
        """Every incident whose frontmatter names `db`, in path order."""
        return tuple(inc for inc in self.incidents.values() if inc.db == db)

    def occurrences_of(self, *, code: str | None = None,
                       db: str | None = None) -> tuple[Occurrence, ...]:
        """The occurrence rows matching both filters, in snapshot order."""
        return tuple(o for o in self.occurrences
                     if (code is None or o.code == code)
                     and (db is None or o.db == db))

    def search(self, query: str, *, limit: int = 50) -> tuple[Hit, ...]:
        """Casefolded substring search over path, title, type, db and
        headings, then over bodies. Every field hit outranks every body hit;
        within a group, path order.

        A scan rather than an index, on purpose: 195 curated pages fit in the
        snapshot already, and an index would be a second thing to keep
        consistent with the revision this value is pinned to."""
        needle = query.casefold()
        if not needle:
            return ()
        fields: list[Hit] = []
        bodies: list[Hit] = []
        for path, info in self.pages.items():
            if any(needle in value.casefold() for value in
                   (path, info.title, info.type, info.db, *info.headings)):
                fields.append(Hit(path, info.type, info.title, info.db,
                                  oneline(info.title)))
                continue
            body = self.text[path]
            at = body.casefold().find(needle)
            if at < 0:
                continue
            start = max(0, at - SNIPPET_CONTEXT)
            end = at + len(needle) + SNIPPET_CONTEXT
            bodies.append(Hit(path, info.type, info.title, info.db,
                              oneline(body[start:end])))
        return tuple((fields + bodies)[:limit])


def parse_occurrences(code: str, text: str) -> tuple[Occurrence, ...]:
    """The `## Occurrences` table of one `errors/<code>.md` page, in page
    order. The header row, the `|---|` separator and any malformed row fail
    the day pattern and are skipped rather than raised.

    This table is the only error-to-database-to-day join in the wiki.
    `Incident.error_codes` names the codes an incident is about, now that
    `incidents.link_codes` writes them as wikilinks, but an incident is one
    episode and this table is every day the code was seen.

    The row regex is owned here rather than imported from
    `research_offload.occurrence_rows`, which answers redaction-facing dicts.
    Reusing it would pull `redact` and `research` into the portal's read path
    to save one regex."""
    rows = []
    for heading, body in sections(text):
        if heading[3:].strip().casefold() != "occurrences":
            continue
        for line in body.splitlines():
            match = OCCURRENCE_ROW_RE.match(line.strip())
            if match is not None:
                rows.append(Occurrence(code, *match.groups()))
    return tuple(rows)


#: The span of quiet after which a reader is nudged to close an incident. A
#: reading convention and never a state the wiki stores: no page carries it,
#: nothing transitions on it, and moving the number changes what the next
#: health report says rather than rewriting a line of history.
#:
#: The workbench page cannot import this one, so `ui.page()` substitutes it
#: into the script at render time; the two never drift.
QUIET_DAYS = 14


def last_seen(inc: Incident, occurrences: Iterable[Occurrence]) -> str | None:
    """The latest day any code this incident links was seen on this
    incident's database, or None when no row joins.

    The join is `occ.db == inc.db` and `occ.code in inc.error_codes`, over the
    only error-to-database-to-day table the wiki holds; `parse_occurrences`
    says why that table is the one place that fact lives. Both halves of the
    join are needed. The same code on another database is that database's
    episode, and this incident is an episode on one of them.

    An incident that links no code answers None and never its own `opened`
    day. "Nobody told us what this is about" and "it has been quiet since the
    day it opened" are different facts, and a reader who could not tell them
    apart would close the wrong page.

    The answer is the `YYYY-MM-DD` string the row spells, not a date object
    and not a span in days. A span needs a today to subtract from, and today
    is a fact about when somebody is looking rather than about the revision
    this model is pinned to; `api.fleet` keeps its 30-day window out of here
    for the same reason. `QUIET_DAYS` above is the threshold a reader compares
    that span against once it has one.

    Rows arrive as any iterable rather than as the `Snapshot` that holds them,
    because nothing in the answer needs the rest of a snapshot. Both callers
    pass `snap.occurrences`, and the signature says so: a reader does not have
    to go and check what else this function reaches for."""
    codes = frozenset(inc.error_codes)
    return max((occ.day for occ in occurrences
                if occ.db == inc.db and occ.code in codes), default=None)


def parse_resolutions(code: str, text: str) -> tuple[Resolution, ...]:
    """The `## Resolution history` table of one `errors/<code>.md` page,
    newest day first, ties keeping page order. The header row, the `|---|`
    separator and any malformed row fail the pattern and are skipped rather
    than raised.

    Newest first and not page order, unlike `parse_occurrences`: the reader of
    this table is looking for the last thing that worked, and `lifecycle.build`
    appends, so page order would bury it under every older attempt. The sort is
    stable, so two incidents resolved on one day stay in the order the page
    spells them and nothing reorders under a rebuild.

    The incident cell is a `[[incidents/<slug>]]` wikilink and the regex takes
    the slug out of it. A row naming a page under any other directory is not a
    resolution this table can speak for, and is skipped."""
    rows = []
    for heading, body in sections(text):
        if heading[3:].strip().casefold() != "resolution history":
            continue
        for line in body.splitlines():
            match = RESOLUTION_ROW_RE.match(line.strip())
            if match is not None:
                rows.append(Resolution(code, *match.groups()))
    return tuple(sorted(rows, key=lambda r: r.day, reverse=True))


def parse_research(code: str, path: str, text: str) -> Research | None:
    """The `## Reference` block of one `errors/<code>.md` page, or None when
    the page carries no research yet.

    `**Cause:**` is what decides. A page with the heading and no cause line
    is a section some other writer opened, and reading it as research would
    put an empty card on the incident screen claiming the code is understood.

    Each field is its whole paragraph collapsed to one line with the
    trailing `(source: ...; url: ...; accessed: ...)` citation split off
    into `citations` by `split_citation`. The citation is the reason a
    reader believes the sentence in front of it, so it is kept, but as
    fields rather than as text: `research_structured.apply_research` writes
    it on its own physical line and `oneline` would otherwise fuse the two.
    A paragraph with no citation contributes none. A second `**Cause:**`
    paragraph is ignored, the first winning, the same first-one-wins rule
    `frontmatter` applies to a repeated key.

    Every `**Practitioner note:**` paragraph is collected the same way and
    all of them are kept, in page order, because each is a different thing
    somebody found; keeping only the first would be dropping evidence rather
    than resolving a contradiction. Their citations stay on the notes and
    never join `citations`, which remains what the cause and action cite.

    A paragraph of any other shape, the hand-written `Practitioner caveat:`
    among them, is left on the page. See `Research`."""
    for heading, body in sections(text):
        if heading[3:].strip().casefold() != "reference":
            continue
        found: dict[str, str] = {}
        cited: list[Citation] = []
        notes: list[Note] = []
        for para in re.split(r"\n\s*\n", body):
            for key, pattern in (("cause", CAUSE_RE), ("action", ACTION_RE)):
                match = pattern.match(para.strip())
                if match is not None and key not in found:
                    prose, citation = split_citation(
                        oneline(para.strip()[match.end():]))
                    found[key] = prose
                    if citation is not None and citation not in cited:
                        cited.append(citation)
            note = NOTE_RE.match(para.strip())
            if note is not None:
                prose, citation = split_citation(
                    oneline(para.strip()[note.end():]))
                notes.append(Note(text=prose, citation=citation))
        if "cause" not in found:
            continue
        return Research(code=code, path=path,
                        researched=str(frontmatter(text).get("researched")
                                       or ""),
                        cause=found["cause"], action=found.get("action", ""),
                        citations=tuple(cited), notes=tuple(notes))
    return None


def split_citation(para: str) -> tuple[str, Citation | None]:
    """A one-line research paragraph as `(prose, citation)`: the text before
    the trailing `(source: ...; url: ...; accessed: ...)` parenthesis, and
    that parenthesis as a `Citation`, or `(para, None)` when the paragraph
    ends in no citation.

    Only a trailing citation counts, because that is where the writer puts
    it; a parenthesis in the middle of the prose is the prose's own. The
    closing period the writer appends after the parenthesis goes with it.
    `url` and `accessed` are optional so a hand-written `(source: sources/x).`
    still reads as a citation."""
    match = CITATION_RE.search(para)
    if match is None:
        return para, None
    return (para[:match.start()].rstrip(),
            Citation(source=match.group("source").strip(),
                     url=(match.group("url") or "").strip(),
                     accessed=(match.group("accessed") or "").strip()))


def parse_journal(db: str, path: str, text: str) -> tuple[JournalEntry, ...]:
    """One `databases/<db>/journal/<YYYY-MM>.md` month, newest day first.

    Sections are `## <YYYY-MM-DD> — <headline>` as `structured.apply_proposal`
    writes them, em dash included; a heading of any other shape belongs to
    another writer and is skipped. Days that tie keep page order."""
    entries = [JournalEntry(db, match.group(1), match.group(2), path)
               for heading, _ in sections(text)
               if (match := JOURNAL_HEAD_RE.match(heading)) is not None]
    return tuple(sorted(entries, key=lambda e: e.day, reverse=True))


def instant(stamp: str) -> datetime:
    """An ISO stamp the wiki or the ledger writes, as an aware UTC datetime.
    Both spellings arrive here — `...Z` from `health._now`, `+02:00` from git
    — and `Z` is the one `fromisoformat` reads as an offset already."""
    return datetime.fromisoformat(str(stamp)).astimezone(timezone.utc)


def read_touches(wiki: Path, revision: str,
                 since: datetime) -> tuple[Touch, ...]:
    """Every incident page a loop tick wrote at or after `since`, newest
    commit first.

    A commit with no `Run-ID:` trailer produces no touch. That is a human
    edit, and this answers "which tick wrote this page": attributing a hand
    edit to a blank id would put a row in the join that no ledger stage can
    ever meet. Nothing else read this trailer before; the orchestrator has
    written it on every machine commit since the wiki began.

    `transaction.last_commits`' record format, for its reasons: `%x1e` between
    records, so a subject holding a newline cannot be read as a record break;
    `-z --name-only`, so a path is never quoted; and the first name of each
    record carrying the newline that ends the format output. A repository with
    no commits, or one git refuses to walk, answers `()` the way
    `last_commits` answers `{}`."""
    fmt = "%x1e%H%x00%cI%x00%(trailers:key=Run-ID,valueonly)"
    try:
        out = gitutil.git_bytes(
            wiki, "log", "-z", "--name-only", f"--format={fmt}",
            f"--since={since.isoformat()}", revision, "--", "incidents")
    except RuntimeError:
        return ()
    touches: list[Touch] = []
    for record in out.decode(errors="surrogateescape").split("\x1e")[1:]:
        sha, at, run_id, *names = record.split("\x00")
        run_id = run_id.strip()
        if not run_id:
            continue
        when = instant(at)
        if names:
            names[0] = names[0].removeprefix("\n")
        for name in names:
            page = INCIDENT_PAGE_RE.match(name)
            if page is not None:
                touches.append(Touch(run_id=run_id, slug=page.group(1),
                                     commit=sha, at=when))
    return tuple(touches)


def build(wiki: Path, revision: str, *,
          now: Callable[[], str]) -> Snapshot:
    """The snapshot at `revision`, in exactly four subprocess-backed calls:
    `gitutil.ls_tree`, one `transaction.Tree.batch_at` over the readable
    paths, one `transaction.last_commits`, one `read_touches`. Everything
    after those is pure parsing over the text they returned.

    `read_touches` is the fourth because the incident-touch join is a fact
    about history rather than about the tree, and no other call here can
    answer it: `last_commits` keeps only the newest commit per path, and a
    page five ticks wrote tonight has five touches.

    Keeping digests in the inventory while leaving them out of the read set is
    why those first two are separate primitives: a `[[link]]` must resolve
    against every page the wiki holds, while reading the digests would pull in
    the 787 files of 982 no view ever renders. The read set is `.md` minus
    `transaction.is_machine`, which is the one definition of machine output;
    a second copy of `MACHINE_DIRS` here could drift from the one `commit`
    refuses on.

    A page that will not parse costs itself and never the snapshot: it drops
    out of `pages`, `text` and everything derived from them, and the build
    completes. It stays in `inventory`, so links to it still resolve, which is
    how `lint_wiki` already treats a page whose encoding it cannot read.
    Nothing is logged and no problem list is collected; 2.1b has no consumer
    for one."""
    inventory = frozenset(gitutil.ls_tree(wiki, revision))
    readable = tuple(sorted(path for path in inventory
                            if path.endswith(".md")
                            and not transaction.is_machine(path)))
    tree = transaction.Tree.batch_at(wiki, revision, readable)
    commits = transaction.last_commits(wiki)
    built_at = now()
    touches = read_touches(wiki, revision,
                           instant(built_at)
                           - timedelta(days=TOUCH_WINDOW_DAYS))
    by_run: dict[str, list[Touch]] = {}
    by_incident: dict[str, list[Touch]] = {}
    for touch in touches:
        by_run.setdefault(touch.run_id, []).append(touch)
        by_incident.setdefault(touch.slug, []).append(touch)

    pages: dict[str, PageInfo] = {}
    text: dict[str, str] = {}
    occurrences: list[Occurrence] = []
    research: dict[str, Research] = {}
    resolutions: dict[str, tuple[Resolution, ...]] = {}
    months: dict[str, list[JournalEntry]] = {}
    for path in readable:
        body = tree.read(path)
        if body is None:
            continue
        try:
            fm = frontmatter(body)
            heading = next((ln[2:].strip() for ln in body.splitlines()
                            if ln.startswith("# ")), Path(path).stem)
            info = PageInfo(
                path=path,
                type=str(fm.get("type") or ""),
                title=str(fm["title"]) if fm.get("title") else heading,
                db=str(fm.get("db") or ""),
                headings=tuple(ln[3:].strip() for ln in body.splitlines()
                               if ln.startswith("## ")),
                origin=origin_of(path, commits.get(path)),
                commit=commits.get(path))
            error = ERROR_PAGE_RE.match(path)
            rows = parse_occurrences(error.group(1), body) if error else ()
            found = (parse_research(error.group(1), path, body)
                     if error else None)
            resolved = (parse_resolutions(error.group(1), body)
                        if error else ())
            journal = JOURNAL_PAGE_RE.match(path)
            entries = (parse_journal(journal.group(1), path, body)
                       if journal else ())
        except Exception:  # noqa: BLE001 — an unparseable page is a skip, not a dead read model
            continue
        pages[path] = info
        text[path] = body
        occurrences.extend(rows)
        if found is not None:
            research[found.code] = found
        if resolved:
            resolutions[resolved[0].code] = resolved
        for entry in entries:
            months.setdefault(entry.db, []).append(entry)

    pages_md = frozenset(p for p in inventory if p.endswith(".md"))
    links: dict[str, tuple[str, ...]] = {}
    for path in pages:
        targets: dict[str, None] = {}
        for raw in lint.WIKILINK_RE.findall(text[path]):
            target = lint.resolve_link(raw, pages_md)
            if target is not None:
                targets[target] = None
        links[path] = tuple(targets)
    inverse: dict[str, list[str]] = {}
    for path, targets in links.items():
        for target in targets:
            inverse.setdefault(target, []).append(path)

    consolidated: dict[str, str] = {}
    partial: dict[str, str] = {}
    for path in sorted(inventory):
        report = REPORT_PAGE_RE.match(path)
        if report is not None:
            day, at = report.groups()
            (partial if at else consolidated)[day] = path

    return Snapshot(
        revision=revision,
        built_at=built_at,
        inventory=inventory,
        pages=pages,
        text=text,
        links=links,
        backlinks={target: tuple(sorted(sources))
                   for target, sources in sorted(inverse.items())},
        incidents={inc.slug: inc for inc in load_incidents_from(
            text.get, [p for p in pages if INCIDENT_PAGE_RE.match(p)])},
        occurrences=tuple(occurrences),
        research=dict(sorted(research.items())),
        resolutions=dict(sorted(resolutions.items())),
        journals={db: tuple(sorted(entries, key=lambda e: e.day,
                                   reverse=True))
                  for db, entries in sorted(months.items())},
        touches_by_run={run: tuple(rows)
                        for run, rows in sorted(by_run.items())},
        touches_by_incident={slug: tuple(rows)
                             for slug, rows in sorted(by_incident.items())},
        report_of_day={day: consolidated.get(day) or partial[day]
                       for day in sorted(set(consolidated) | set(partial))},
        dbs=tuple(sorted(m.group(1) for path in inventory
                         if (m := DB_PAGE_RE.match(path)))))
