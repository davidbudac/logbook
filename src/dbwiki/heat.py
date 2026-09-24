"""Daily message counts per database, the model behind the board's heat maps.

A sibling of `readmodel` rather than a part of it. `readmodel.build` keeps
digests out of the snapshot on purpose (its docstring argues the case: 787 of
982 files stay closed because a digest is evidence a page links to, not
navigation), and the heat maps want the opposite trade — every digest the
window covers, read for three numbers each and nothing else. Folding that into
`Snapshot` would make every request pay for the one screen that asks.

Pinned the same way `Snapshot` is: `build` reads through one
`transaction.Tree.batch_at` at the snapshot's revision, never the working
tree, so a map labelled A holds only A's bytes. The window is always the whole
`WINDOW_DAYS`; the endpoint slices it, which is what lets one cached value
serve the 14-, 30- and 90-day toggle without a rebuild per choice.

The arithmetic is deliberately git-free. `window_days`, `digest_path`,
`host_of`, `parse_counts` and `rows_of` are pure, `rows_of` takes the tree it
reads through, and `build` is the only function here that knows a wiki is a
checkout. That is what lets the tests drive the whole model through
`Tree.of` with no repository at all.
"""

import datetime as dt
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import readmodel, transaction
from .pagetext import frontmatter

#: The classes the maps are drawn for, in drawing order. A subset of
#: `patterns.NOTABLE_CLASSES`, which also holds `lifecycle` and `dataguard`:
#: those two are a database saying what it did, and the operator scanning a
#: heat map is asking what went wrong. `unmatched` earns a map of its own
#: rather than being folded into `error` because a line no pattern classified
#: is a fact about the pattern set, not about the database.
#:
#: The tuple is the wire's `classes` list, so the page draws what the server
#: counted instead of keeping its own copy of this list.
CLASSES: tuple[str, ...] = ("error", "warning", "unmatched")

#: Every window is built whole and sliced by the request, so the board's
#: 14/30/90 toggle costs a slice rather than a rebuild. 90 is the longest span
#: the board offers, and the whole window is still one `cat-file --batch` per
#: revision.
WINDOW_DAYS = 90

#: ``Host `lab-dg1.localdomain` `` as the live database pages say it. Not
#: anchored to a line start: `databases/cdb1.md` opens the sentence with it and
#: `databases/cdb1_stby.md` reaches it mid-sentence, and both mean the same
#: thing. The capitalised word and the immediate backtick are the whole guard,
#: which is what keeps a `## Hosts — moved` heading that lists two historical
#: addresses from being read as a claim about where the database runs now.
HOST_LINE_RE = re.compile(r"\bHost\s+`([^`]+)`")


@dataclass(frozen=True)
class HeatRow:
    """One database's row of the maps: who it runs on, and one entry per day
    per class.

    `host` is `""` and not None for a database whose page does not say where
    it runs, matching `fleet_row_json`'s refusal to put null on the wire for a
    missing string: the band is drawn from this value and an absent one is a
    band label, not a second kind of nothing.

    `counts` maps class to a tuple as long as `Heat.days` and aligned with it,
    where `None` is "the wiki holds no digest for that database that day" and
    `0` is "a digest said zero". Those are different facts — nothing ran
    versus nothing happened — and the map draws them differently, which is why
    the tuple carries an optional int rather than a count with a sentinel."""

    db: str
    host: str
    counts: Mapping[str, tuple[int | None, ...]]


@dataclass(frozen=True)
class Heat:
    """The whole window at one revision, immutable and safe to cache.

    `revision` and `built_at` carry the same meaning they carry on
    `Snapshot`, so `wire.provenance_json` reads either value and the page
    derives one stale badge for the whole board.

    `days` is oldest first and ends on the day `build` was handed, so the last
    column is today and a slice off the tail is the most recent N days.
    `rows` is sorted by `(host == "", host, db)`: hosts group alphabetically
    and the databases whose host is unknown fall into one band at the end,
    rather than sorting under an empty string at the top where they would
    read as a host called nothing."""

    revision: str
    built_at: str
    days: tuple[str, ...]
    rows: tuple[HeatRow, ...]


def window_days(today: str) -> tuple[str, ...]:
    """The `WINDOW_DAYS` ISO days ending on `today`, oldest first.

    Calendar days and not timestamps: a digest is written per day and named
    per day, so the axis is the same unit the evidence is."""
    last = dt.date.fromisoformat(today)
    return tuple((last - dt.timedelta(days=offset)).isoformat()
                 for offset in reversed(range(WINDOW_DAYS)))


def digest_path(db: str, day: str) -> str:
    """The digest sidecar for one database on one day.

    One definition, used both to decide what to ask git for and to look the
    answer up again, so the path set and the reader cannot drift apart."""
    return f"digests/{db}/{day}.json"


def host_of(page: str) -> str:
    """The host a database page says it runs on, or `""`.

    Frontmatter wins, under either `host` or `hostname`, because a key is a
    deliberate statement and prose is incidental. Failing that, the first
    ``Host `name` `` line of the body, which is how every live database page
    opens and the only place most of them say it. A page that says neither
    lands in the unknown-host band rather than raising: a heat map that
    refuses to draw a database because its page is thin is worse than one that
    draws it under a plain label."""
    front = frontmatter(page)
    for key in ("host", "hostname"):
        value = front.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    match = HOST_LINE_RE.search(page)
    return match.group(1) if match is not None else ""


def parse_counts(text: str) -> dict[str, int] | None:
    """One digest sidecar's `sources.<source>.by_class`, summed over sources.

    A day's alert, listener and dataguard logs are three views of the same
    database on the same day, and the operator scanning a row is asking how
    loud that day was, not which file was loud. So the sources add.

    Every class the digest names survives here; `CLASSES` is applied by
    `rows_of`. Keeping the parse total is what makes a class the digest simply
    did not mention read as `0` at the row rather than as a missing digest:
    the sidecar exists, so the day was observed.

    None means the blob could not be believed at all — not JSON, not an
    object, no `sources` mapping, or a `by_class` count that is not an int.
    That is the same answer a missing sidecar gets, because a digest the
    reader cannot parse is a day nobody can say anything about, and guessing
    zero would draw it as a quiet day."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return None
    counts: dict[str, int] = {}
    for source in sources.values():
        if not isinstance(source, dict):
            return None
        by_class = source.get("by_class", {})
        if not isinstance(by_class, dict):
            return None
        for name, count in by_class.items():
            if not isinstance(count, int) or isinstance(count, bool):
                return None
            counts[name] = counts.get(name, 0) + count
    return counts


def rows_of(snap: readmodel.Snapshot, tree: transaction.Tree,
            days: tuple[str, ...]) -> tuple[HeatRow, ...]:
    """One row per database in `snap.dbs`, read through `tree`.

    Pure over the tree it is handed, so a test drives the whole shape —
    the None-versus-zero rule, the class filter, the band order — through
    `Tree.of` and never starts a subprocess.

    A database page the snapshot could not read leaves the host unknown rather
    than dropping the row, the same tolerance `readmodel.build` applies: the
    counts are the point and the host is only how they are grouped."""
    rows = []
    for db in snap.dbs:
        parsed = {}
        for day in days:
            blob = tree.read(digest_path(db, day))
            parsed[day] = None if blob is None else parse_counts(blob)
        rows.append(HeatRow(
            db=db,
            host=host_of(snap.text.get(f"databases/{db}.md", "")),
            counts={name: tuple(None if parsed[day] is None
                                else parsed[day].get(name, 0)
                                for day in days)
                    for name in CLASSES}))
    return tuple(sorted(rows, key=lambda row: (row.host == "", row.host,
                                               row.db)))


def build(wiki: Path, snap: readmodel.Snapshot, *,
          now: Callable[[], str]) -> Heat:
    """The whole `WINDOW_DAYS` window at `snap.revision`.

    The path set is filtered by `snap.exists` before git is asked, so the
    batch requests only what the revision holds: the live wiki's 18 databases
    over 90 days are 1620 candidate paths and 673 real ones.
    `Tree.batch_at` would answer None for the rest anyway, at the cost of
    putting them all through the pipe, and `snap.exists` is a frozenset
    lookup.

    `now` is the same clock `readmodel.build` was handed, so `built_at` and
    the last column of `days` agree with the snapshot beside them. It is read
    once and not twice: a build that straddles midnight would otherwise stamp
    a window whose last column is yesterday with a `built_at` of today, and
    the pair is what the page dates the axis from."""
    stamp = now()
    days = window_days(stamp[:10])
    paths = [digest_path(db, day) for db in snap.dbs for day in days
             if snap.exists(digest_path(db, day))]
    tree = transaction.Tree.batch_at(wiki, snap.revision, paths)
    return Heat(revision=snap.revision, built_at=stamp, days=days,
                rows=rows_of(snap, tree, days))
