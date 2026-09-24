"""What the operators did to the database, read back off the digest itself.

Nothing here decides what a change *is*. The pattern library already does:
every rule whose class is `lifecycle` — `ALTER SYSTEM SET`, mount/open,
tablespace and datafile DDL, redo config, instance startup and shutdown — is
an administrative act by construction. `changes_of` is the only derivation in
the codebase, so the `changes` key the compactor writes, the `## Changes`
section the markdown renders and the cross-day reader can never disagree about
what counted as a change.

The module deliberately imports nothing from `dbwiki`. `compactor` and
`digest_md` both need it, and `structured` -> `digest_md` -> here would close
an import cycle and drag the prompt builder into the compactor. The one
constant that would otherwise come from `structured`, `MAX_LINE`, is restated
below and pinned to it by a test.
"""

import datetime as dt
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LIFECYCLE = "lifecycle"
# The classes a change can be followed by: errors, and every dataguard group.
# A role transition, an MRP stop or a gap right after an operator act is the
# same timing fact as an ORA error there, and the alert log's dataguard rules
# are all class `dataguard`, so an error-only filter never saw them.
AFTER_CHANGE_CLASSES = frozenset({"error", "dataguard"})
MAX_MESSAGE = 300   # structured.MAX_LINE, which this module may not import


@dataclass(frozen=True)
class Change:
    day: str
    ts: str
    rule: str
    count: int
    message: str

    def to_dict(self) -> dict:
        return {"day": self.day, "ts": self.ts, "rule": self.rule,
                "count": self.count, "message": self.message}

    @classmethod
    def from_dict(cls, d: dict) -> "Change":
        return cls(day=d["day"], ts=d["ts"], rule=d["rule"],
                   count=d["count"], message=d["message"])


def headline(message: str) -> str:
    """The first line that says anything, for a rule that matched on a field
    rather than a regex and so has no line of its own to point at."""
    for line in message.splitlines():
        line = line.strip()
        if line:
            return line[:MAX_MESSAGE]
    return ""


def _message(group: dict) -> str:
    """The line that names the change.

    The compactor records `headline`: the line the rule's regex actually
    matched, which in a document of many alert-log lines is the one that says
    what happened. Digests written before that key existed fall back to the
    first non-empty line, which is what they have always rendered."""
    head = str(group.get("headline") or "").strip()
    return head[:MAX_MESSAGE] if head else headline(group["message"])


def changes_of(digest: dict) -> tuple[Change, ...]:
    """Derive: every lifecycle-class group in every source."""
    day = digest["window"]["day"]
    found = [Change(day=day, ts=g["first_ts"], rule=g["rule"],
                    count=g["count"], message=_message(g))
             for section in digest["sources"].values()
             for g in section["notable"]
             if g["class"] == LIFECYCLE]
    return tuple(sorted(found, key=lambda c: (c.ts, c.rule, c.message)))


def of_digest(digest: dict) -> tuple[Change, ...]:
    """The recorded `changes` when the digest has the key, derived otherwise —
    digests written before the key existed still render and read back."""
    if "changes" in digest:
        return tuple(Change.from_dict(c) for c in digest["changes"])
    return changes_of(digest)


def recent(wiki_root: Path, db: str, *, today: dt.date,
           days: int) -> tuple[Change, ...]:
    """Changes over `today - days <= day < today`, newest day first.

    The days are named, not globbed: a digest file may carry a suffix, and a
    consolidation tick's file must not be mistaken for another day. A digest
    that will not open or will not parse is skipped rather than raised — a
    single bad file on disk must never take down the prompt that reads it.
    """
    if days <= 0:
        return ()
    out: list[Change] = []
    for back in range(1, days + 1):
        day = (today - dt.timedelta(days=back)).isoformat()
        path = wiki_root / "digests" / db / f"{day}.json"
        try:
            out.extend(of_digest(json.loads(path.read_text())))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return tuple(out)


@dataclass(frozen=True)
class ChangeStamp:
    """One lifecycle event, not a group.

    `Change` is the group: nine `ALTER SYSTEM SET` lines collapse into one
    row whose `ts` is the oldest of them. Timing needs the opposite — the
    change an error followed is usually a later occurrence — so the compactor
    stamps every lifecycle event as it scans and hands the list here."""
    ts: str
    rule: str
    headline: str


def _parsed(ts: str) -> dt.datetime | None:
    """None rather than a raise: one malformed timestamp must never take down
    the compaction that carries it. A stamp without an offset is read as UTC,
    so a source that drops the `Z` cannot make the subtraction raise either."""
    try:
        parsed = dt.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def after_change(stamps: Sequence[ChangeStamp], sections: Mapping[str, dict],
                 *, hours: float, cap: int = 10) -> list[dict]:
    """Error and dataguard groups that began shortly after somebody changed
    something.

    That an operator's change caused tonight's error is the one inference we
    least want a model making on its own: the digest puts both in front of it
    and only prose connects them. Both timestamps are already here, so the
    delta states the timing and leaves the model nothing to do but repeat it.

    The *latest* qualifying event wins, not the first, because what the
    operator did immediately before the error is what a DBA would look at."""
    if hours <= 0:
        return []
    window = hours * 3600
    known = [(t, s) for s in stamps if (t := _parsed(s.ts)) is not None]
    out: list[dict] = []
    for name, section in sections.items():
        for g in section["notable"]:
            first = (_parsed(g["first_ts"])
                     if g["class"] in AFTER_CHANGE_CLASSES else None)
            if first is None:
                continue
            near = [(t, s) for t, s in known
                    if 0 <= (first - t).total_seconds() <= window]
            if not near:
                continue
            t, s = max(near, key=lambda c: c[0])
            out.append({"type": "after_change", "source": name, "rule": g["rule"],
                        "codes": g["codes"], "first_ts": g["first_ts"],
                        "gap_s": int((first - t).total_seconds()),
                        "change_ts": s.ts, "change_rule": s.rule,
                        "change": s.headline})
    out.sort(key=lambda d: (d["first_ts"], d["source"], d["rule"]))
    return out[:cap]
