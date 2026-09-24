"""Deterministic monitoring evaluation: what the digests say about the
recovery signal a human armed on an incident.

Facts, never a decision. `Resolve` stays the DBA's (ADR-0003); this module
only answers "does the evidence so far meet the signal, refute it, or say
nothing yet?" and writes the answer to `.state/monitoring/<slug>.json`.
Machine-owned on purpose: gathering evidence must never dirty the wiki tree
nor compete with a human edit.

`evaluate` is pure over the digests it is handed, in the style of
`trigger.decide`: identical inputs, identical bytes, so a tick and a later
replay agree. The one live read is the supplementary ES probe that
`evaluate_all` may attach for a window day no digest covers yet. It is
injected, best-effort, and never overturns what the digests already settled.

Window semantics are `[start, until)`, `until` exclusive. `evaluate` owns the
verdict table; the one rule worth stating twice is that a verdict resting on
absence (`error_absent` met, `event_present` or `flow_resumed` refuted) is
only published when every window day was actually examined.
"""

import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .incidents import (ActionMalformed, ErrorAbsent, EventPresent,
                        FlowResumed, Incident, Manual, MonitoringWindow,
                        Status, load_incidents, signal_from_yaml,
                        signal_to_yaml)
from .state import atomic_write_text

MONITORING_SCHEMA_VERSION = 1

MONITORING_DIR = "monitoring"

DIGEST_DIR = "digests"

Verdict = Literal["met", "not_met", "insufficient", "error"]

MET: Verdict = "met"
NOT_MET: Verdict = "not_met"
INSUFFICIENT: Verdict = "insufficient"
ERROR: Verdict = "error"

MAX_WINDOW_DAYS = 92

#: A wedge-breaker, not a performance budget: one legitimate pattern over the
#: full 92-day window measures 15.6ms, so this only bounds how long a wedged
#: one can hold the single-flight lock.
PATTERN_TIMEOUT = 5.0

_PROBE_CODE_RE = re.compile(r"\A[A-Z]{2,8}-\d{1,6}\Z")


@dataclass(frozen=True)
class MonitoringFacts:
    """One evaluation of one incident's recovery signal, versioned and
    replayable.

    `observed` holds one small dict per digest day examined, plus the ES probe
    when there was one: enough for an operator to see which evidence produced
    the verdict without opening a digest. `contradictions` holds the reasons
    the signal is not met, so `verdict == MET` implies no contradictions."""

    schema_version: int
    incident: str
    db: str
    signal: dict
    window: dict
    observed: tuple[dict, ...]
    verdict: Verdict
    contradictions: tuple[str, ...]
    evaluated_at: str
    source_revision: str

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "incident": self.incident,
            "db": self.db,
            "signal": self.signal,
            "window": self.window,
            "observed": list(self.observed),
            "verdict": self.verdict,
            "contradictions": list(self.contradictions),
            "evaluated_at": self.evaluated_at,
            "source_revision": self.source_revision,
        }


def _sec(ts: object) -> str:
    """An ISO-8601 Z timestamp cut to whole seconds. Digest timestamps carry
    milliseconds and window bounds do not, and `'...:00.816Z' < '...:00Z'`
    string-compares the wrong way round; at second granularity they compare
    exactly."""
    return str(ts or "")[:19]


def window_day_count(window: MonitoringWindow) -> int:
    """How many calendar days `[start, until)` truly spans, cap or no cap. A
    day counts while its own midnight is before `until`, so a window ending at
    midnight does not reach into the day after it."""
    span = (dt.date.fromisoformat(window.until[:10])
            - dt.date.fromisoformat(window.start[:10])).days
    return span + 1 if window.until[11:] > "00:00:00Z" else span


def window_days(window: MonitoringWindow) -> list[str]:
    """The days `window_day_count` counts, oldest first, at most
    MAX_WINDOW_DAYS of them. Compare the two to see whether the cap bit."""
    first = dt.date.fromisoformat(window.start[:10])
    return [(first + dt.timedelta(days=i)).isoformat()
            for i in range(min(window_day_count(window), MAX_WINDOW_DAYS))]


def _gaps(window: MonitoringWindow, days: list[str],
          covered: set[str]) -> tuple[str, ...]:
    """Why the evidence may be incomplete: window days no digest covers, and a
    window too long to walk. A verdict that rests on absence is only honest
    when this is empty, so `evaluate` returns `insufficient` instead.

    A tick whose compaction failed for one database leaves exactly this hole,
    and without it three clean days out of six would read as `met`."""
    gaps: list[str] = []
    missing = [d for d in days if d not in covered]
    if missing:
        shown = ", ".join(missing[:3])
        if len(missing) > 3:
            shown += f", and {len(missing) - 3} more"
        gaps.append(f"no digest covers {shown}")
    if window_day_count(window) > len(days):
        gaps.append(f"the window runs past the {MAX_WINDOW_DAYS}-day "
                    f"evaluation limit, so its later days were not examined")
    return tuple(gaps)


def _day(digest: dict) -> str:
    return str((digest.get("window") or {}).get("day") or "")


def _groups(digest: dict) -> list[dict]:
    """Every notable group in the digest. The only per-code and per-message
    evidence a digest carries; routine counters keep no code and no text."""
    return [g for s in (digest.get("sources") or {}).values()
            for g in (s.get("notable") or [])]


def _spans(group: dict, window: MonitoringWindow) -> bool:
    """Whether the group's `[first_ts, last_ts]` reaches into the window. A
    group missing either timestamp counts as spanning: the compactor always
    writes both, so the absence is corruption and corruption must not read as
    a clean window."""
    first, last = _sec(group.get("first_ts")), _sec(group.get("last_ts"))
    if not first or not last:
        return True
    return first < _sec(window.until) and last >= _sec(window.start)


def _code_spellings(code: str) -> tuple[str, ...]:
    """Every form one Oracle code takes. The log writes `TNS-00513` and
    `patterns.extract_codes` normalizes it to `TNS-513` before it reaches a
    digest, while nothing normalizes what a DBA typed. Matching one spelling
    would let a padded code read as absent while the error sat in every
    digest of the window."""
    prefix, _, number = code.partition("-")
    digits = number.lstrip("0") or "0"
    return tuple(dict.fromkeys(
        (code, f"{prefix}-{digits}", f"{prefix}-{digits.zfill(5)}")))


def _code_count(digest: dict, code: str, window: MonitoringWindow) -> int:
    """Events carrying `code` that the digest saw inside the window.

    The count is a group total, so a group straddling a bound contributes all
    of its events; the digest keeps no per-event timestamps to do better.

    A `first_ever_code` delta whose `first_seen` falls in the window counts as
    one occurrence even when no group survives to carry it: the code may have
    been classified routine or dropped by the storm cap, and "the code was
    seen" is the fact `error_absent` turns on."""
    spellings = set(_code_spellings(code))
    n = sum(int(g.get("count") or 0) for g in _groups(digest)
            if spellings & set(g.get("codes") or []) and _spans(g, window))
    if n:
        return n
    for delta in digest.get("deltas") or []:
        if (delta.get("type") == "first_ever_code"
                and delta.get("value") in spellings
                and _sec(window.start) <= _sec(delta.get("first_seen"))
                < _sec(window.until)):
            return 1
    return 0


def _error_absent(code: str, digests: Sequence[dict], window: MonitoringWindow,
                  closed: bool, probe: dict | None, gaps: tuple[str, ...]
                  ) -> tuple[str, tuple[dict, ...], tuple[str, ...]]:
    observed: list[dict] = []
    contradictions: list[str] = []
    for d in digests:
        count = _code_count(d, code, window)
        observed.append({"day": _day(d), "digest": d.get("_path", ""),
                         "code": code, "count": count})
        if count:
            contradictions.append(f"{_day(d)}: {code} × {count}")
    if probe is not None:
        observed.append(probe)
        if probe.get("count"):
            contradictions.append(
                f"{probe['day']}: {code} × {probe['count']} (live count, no "
                f"digest for that day yet)")
    if contradictions:
        return NOT_MET, tuple(observed), tuple(contradictions)
    if gaps:
        return INSUFFICIENT, tuple(observed), gaps
    if closed:
        return MET, tuple(observed), ()
    return INSUFFICIENT, tuple(observed), ()


class PatternTimeout(RuntimeError):
    """A recovery pattern that did not finish searching in the time allowed.

    Not a bug: it is a property of the regex the operator armed and the
    message it met, and the child carrying the search was killed, so nothing
    is left running."""


_WORKER = """
import json
import re
import sys

job = json.load(sys.stdin)
rx = re.compile(job["pattern"])
json.dump([bool(rx.search(m)) for m in job["messages"]], sys.stdout)
"""

Matcher = Callable[[str, Sequence[str]], Sequence[bool]]


def match_messages(pattern: str, messages: Sequence[str], *,
                   timeout: float = PATTERN_TIMEOUT) -> tuple[bool, ...]:
    """Which of `messages` the pattern matches, decided in a child process
    the parent can kill.

    `re` has no timeout and does not run a signal handler mid-match, so a
    catastrophically backtracking pattern is interruptible only as a process.
    The compile stays here in the parent so `re.error` still reaches the
    caller as itself; only the search crosses the boundary, and it crosses as
    one job for the whole evaluation.

    What the child writes back is external data, so a non-zero exit and a
    result of the wrong length are both refused as a plain `RuntimeError`.
    `PatternTimeout` subclasses it and `_event_present` catches only the
    subclass, so a timeout becomes a published verdict while a broken child
    propagates to `evaluate_all` and leaves the last good verdict standing."""
    re.compile(pattern)
    if not messages:
        return ()
    proc = subprocess.Popen([sys.executable, "-c", _WORKER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    job = json.dumps({"pattern": pattern, "messages": list(messages)})
    try:
        out, err = proc.communicate(job, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        plural = "" if len(messages) == 1 else "s"
        raise PatternTimeout(
            f"pattern {pattern!r} did not finish matching {len(messages)} "
            f"digest message{plural} within {timeout:g}s") from None
    if proc.returncode:
        raise RuntimeError(f"matching {pattern!r} exited "
                           f"{proc.returncode}: {err.strip()}")
    matched = json.loads(out)
    if len(matched) != len(messages):
        raise RuntimeError(f"matching {pattern!r} answered for "
                           f"{len(matched)} of {len(messages)} messages")
    return tuple(bool(m) for m in matched)


def _event_present(pattern: str, digests: Sequence[dict],
                   window: MonitoringWindow, closed: bool,
                   gaps: tuple[str, ...], match: Matcher
                   ) -> tuple[str, tuple[dict, ...], tuple[str, ...]]:
    spanning = [[g for g in _groups(d) if _spans(g, window)] for d in digests]
    try:
        flags = match(pattern, [str(g.get("message") or "")
                                for groups in spanning for g in groups])
    except re.error as exc:
        return INSUFFICIENT, (), (f"pattern {pattern!r} does not compile: "
                                  f"{exc}",)
    except PatternTimeout as exc:
        return ERROR, (), (str(exc),)
    observed: list[dict] = []
    found = False
    at = 0
    for d, groups in zip(digests, spanning):
        rule = next((str(g.get("rule") or "") for g, hit
                     in zip(groups, flags[at:at + len(groups)]) if hit), "")
        at += len(groups)
        observed.append({"day": _day(d), "digest": d.get("_path", ""),
                         "matched": bool(rule), "group": rule})
        found = found or bool(rule)
    if found:
        return MET, tuple(observed), ()
    if gaps:
        return INSUFFICIENT, tuple(observed), gaps
    if closed:
        return NOT_MET, tuple(observed), (
            f"no notable message matched {pattern!r} on any of the "
            f"{len(digests)} days examined",)
    return INSUFFICIENT, tuple(observed), ()


def _digest_bounds(digest: dict) -> tuple[str, str]:
    """`[from, to]` the digest covers, at second granularity; the calendar
    day's bounds when the digest does not say (the wider, safer reading)."""
    w = digest.get("window") or {}
    day = _day(digest)
    try:
        nxt = (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()
    except ValueError:
        return _sec(w.get("from")), _sec(w.get("to"))
    return (_sec(w.get("from") or f"{day}T00:00:00Z"),
            _sec(w.get("to") or f"{nxt}T00:00:00Z"))


def _events_in_window(section: dict, digest: dict,
                      window: MonitoringWindow) -> tuple[int, int]:
    """`(events inside the window, events that cannot be placed)` for one
    source section of one digest.

    A digest the window covers whole counts its `total_events`. On a day the
    window only partly covers (the start or the end day) the total includes
    events outside it — a listener that died at 03:00 still has its 00:00-03:00
    lines in the start day's total — so only notable groups, the one thing
    with timestamps, are placed: a group inside counts whole, a group
    straddling a boundary counts one (at least one of its events is inside),
    and a group wholly outside counts nothing. Whatever the groups do not
    account for (routine lines keep no timestamp) cannot be placed."""
    total = int(section.get("total_events") or 0)
    start, until = _sec(window.start), _sec(window.until)
    lo, hi = _digest_bounds(digest)
    if lo and hi and lo >= start and hi <= until:
        return total, 0
    inside = placed = 0
    for g in section.get("notable") or []:
        first, last = _sec(g.get("first_ts")), _sec(g.get("last_ts"))
        if not first or not last:
            continue
        n = int(g.get("count") or 0)
        placed += n
        if first >= start and last < until:
            inside += n
        elif last >= start and first < until:
            inside += 1
    return inside, max(total - placed, 0)


def _flow_resumed(source: str, digests: Sequence[dict],
                  window: MonitoringWindow, closed: bool,
                  gaps: tuple[str, ...]
                  ) -> tuple[str, tuple[dict, ...], tuple[str, ...]]:
    observed: list[dict] = []
    unplaced: list[str] = []
    found = False
    for d in digests:
        section = (d.get("sources") or {}).get(source) or {}
        events, loose = _events_in_window(section, d, window)
        observed.append({"day": _day(d), "digest": d.get("_path", ""),
                         "source": source, "events": events})
        found = found or events > 0
        if loose:
            unplaced.append(f"{_day(d)}: {loose} {source} event(s) that day "
                            f"cannot be placed before or after the window "
                            f"boundary, so silence cannot be concluded")
    if found:
        return MET, tuple(observed), ()
    seen = any(source in (d.get("sources") or {}) for d in digests)
    if digests and not seen:
        return INSUFFICIENT, tuple(observed), (
            f"{source} is not a source in any digest examined, so its silence "
            f"says nothing",)
    if gaps or unplaced:
        return INSUFFICIENT, tuple(observed), tuple(unplaced) + gaps
    if closed:
        return NOT_MET, tuple(observed), (
            f"{source} produced no events on any of the {len(digests)} days "
            f"examined",)
    return INSUFFICIENT, tuple(observed), ()


def evaluate(incident: Incident, digests: Sequence[dict], *, now: str,
             source_revision: str = "", probe: dict | None = None,
             match: Matcher = match_messages) -> MonitoringFacts:
    """One incident plus the digests already loaded for it -> one fact record.

    Pure: `now` is the clock and `digests` the evidence, so a replay of the
    same inputs writes the same bytes. Digests whose day falls outside the
    window are ignored here as well as at load time, because a caller may hand
    over whatever it has.

    `match` is the injected seam for the regex, asked once for the whole
    evaluation, so a test hands in a fake rather than spawning anything.

    `probe` is the supplementary live observation `evaluate_all` attaches for
    a window day no digest covers yet. Only `error_absent` reads it, and only
    `evaluate_all` builds one. A probe never closes a coverage gap, because a
    zero count from an expired index reads the same as a quiet day; a probe
    that counts something still refutes the signal.

    Verdicts. `error_absent`: any occurrence inside the window refutes it; no
    occurrence with the window closed meets it; otherwise there is not enough
    yet. `event_present`: a match meets it the moment it appears; no match
    with the window closed refutes it. `flow_resumed`: one window day with
    events for the source *inside the window* meets it (on a partly-covered
    start or end day only timestamped notable groups can be placed, see
    `_events_in_window`); every day at zero with the window closed refutes
    it. `manual` is never settled here, by design.

    A positive sighting stands on its own, but a verdict that rests on
    absence needs the whole window examined, so any coverage gap (`_gaps`)
    downgrades it to `insufficient` and says which days are missing.

    Two limits inherited from what a digest keeps. `event_present` searches
    the notable groups' messages, so a pattern aimed at a line the library
    classifies routine (`RFS[`, `Media Recovery Log`) can never match. And a
    day's digest is only whole once the first tick after midnight has
    re-compacted it (`cli._catchup_day`; the last same-day tick runs at
    23:30): until then, or when that catch-up failed or the tick was down for
    more than a day, a window's final minutes may be in no digest, and `met`
    means "nothing in the evidence collected"."""
    window = incident.monitoring
    if window is None:
        raise ValueError(f"{incident.slug}: no monitoring window to evaluate")
    days = window_days(window)
    covering = [d for d in digests if _day(d) in set(days)]
    closed = _sec(now) >= _sec(window.until)
    gaps = _gaps(window, days, {_day(d) for d in covering})
    if not is_digest_db(incident.db):
        gaps = (f"db {incident.db!r} is not a digest directory name, so no "
                f"digest was read for this incident",) + gaps

    match window.signal:
        case ErrorAbsent(code=code):
            verdict, observed, contradictions = _error_absent(
                code, covering, window, closed, probe, gaps)
        case EventPresent(pattern=pattern):
            verdict, observed, contradictions = _event_present(
                pattern, covering, window, closed, gaps, match)
        case FlowResumed(source=source):
            verdict, observed, contradictions = _flow_resumed(
                source, covering, window, closed, gaps)
        case Manual(description=description):
            verdict, observed = INSUFFICIENT, ()
            contradictions = (
                f"manual: {description}; only a human judges this",)
        case _:
            raise ValueError(f"{incident.slug}: "
                             f"{type(window.signal).__name__} is not a "
                             f"recovery signal")

    return MonitoringFacts(
        schema_version=MONITORING_SCHEMA_VERSION,
        incident=incident.slug,
        db=incident.db,
        signal=signal_to_yaml(window.signal),
        window={"start": window.start, "until": window.until},
        observed=observed,
        verdict=verdict,
        contradictions=contradictions,
        evaluated_at=now,
        source_revision=source_revision,
    )


def is_digest_db(db: str) -> bool:
    """Whether `db` may be interpolated into `digests/<db>/<day>.json`. A `db`
    is read off page frontmatter, so it is a boundary: one directory name, no
    traversal out of the wiki's digest tree."""
    return bool(db) and "/" not in db and ".." not in db


def load_window_digests(wiki_root: Path, db: str,
                        window: MonitoringWindow) -> list[dict]:
    """`digests/<db>/<day>.json` for every day the window touches, oldest
    first, each carrying its wiki-relative path as `_path`. A day with no
    digest, one that will not parse, and one that parses to something other
    than an object are all simply absent: the evaluator reads that as missing
    evidence, which is the honest reading. So is every day of a `db` that
    could leave the digest directory; `evaluate` names it in the verdict."""
    root = Path(wiki_root)
    out: list[dict] = []
    if not is_digest_db(db):
        return out
    for day in window_days(window):
        rel = f"{DIGEST_DIR}/{db}/{day}.json"
        try:
            digest = json.loads((root / rel).read_text())
            digest["_path"] = rel
        except (OSError, TypeError, ValueError):
            continue
        out.append(digest)
    return out


def _probe(cfg, es, db: str, code: str, t0: str, t1: str) -> dict | None:
    """Count events carrying `code` for `db` in `[t0, t1)` across every
    configured source. None for a code that is not code-shaped, which is a
    probe not worth making rather than a probe that failed.

    Raises whatever the ES client raises; `_Probes` owns turning that into
    "the digests speak alone"."""
    from .es import ES
    if not _PROBE_CODE_RE.match(code):
        return None
    total = 0
    for name in cfg.sources:
        s = cfg.source(name)
        phrase = " OR ".join(f'"{c}"' for c in _code_spellings(code))
        query = ES.window_query(
            s["timestamp_field"], t0, t1,
            extra=cfg.db_filter(name, db)
            + [{"query_string": {"query": phrase, "default_field": "*"}}])
        total += es.count(",".join(s["index_patterns"]), query)
    return {"day": t0[:10], "code": code, "count": total,
            "via": "elasticsearch", "from": t0, "to": t1}


def _probe_for(cfg, es, incident: Incident, digests: Sequence[dict],
               now: str) -> dict | None:
    """The probe `evaluate` may fold in: the newest window day a live count
    can still speak to, when no digest covers it.

    That day is the last one the clock has reached, not the window's last
    calendar day. Probing the latter would only ever fire on a closed window,
    which is when the gap it fills matters least."""
    window = incident.monitoring
    if es is None or window is None:
        return None
    if not isinstance(window.signal, ErrorAbsent):
        return None
    days = window_days(window)
    target = min(days[-1], now[:10]) if days else ""
    if target not in days or target in {_day(d) for d in digests}:
        return None
    t0 = max(window.start, f"{target}T00:00:00Z")
    t1 = min(window.until, now)
    if t1 <= t0:
        return None
    return _probe(cfg, es, incident.db, window.signal.code, t0, t1)


class _Probes:
    """One `evaluate_all` run's live-count budget.

    Best-effort by contract: a probe that fails yields None and the digests
    speak alone, because a monitoring verdict must not depend on ES being
    reachable. The tick's health assessment is what reports an ES outage.

    The first failure ends the run's probing, so an outage costs one timeout
    rather than one per monitoring incident."""

    def __init__(self, cfg, es, now: str):
        self.cfg, self.es, self.now = cfg, es, now

    def of(self, incident: Incident, digests: Sequence[dict]) -> dict | None:
        if self.es is None:
            return None
        try:
            return _probe_for(self.cfg, self.es, incident, digests, self.now)
        except Exception as exc:  # noqa: BLE001 — a probe is best-effort
            self.es = None
            print(f"warning: monitoring probe failed, verdicts rest on digests "
                  f"alone for this run: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return None


def _facts_path(state_dir: Path, slug: str) -> Path:
    return Path(state_dir) / MONITORING_DIR / f"{slug}.json"


def _revision(wiki_root: Path) -> str:
    from . import transaction
    try:
        return transaction.head(Path(wiki_root))
    except (OSError, RuntimeError):
        return ""


def evaluate_all(cfg, wiki_root: Path, state_dir: Path, *, now: str,
                 es=None) -> list[MonitoringFacts]:
    """Evaluate every monitoring incident and publish the results under
    `<state_dir>/monitoring/`.

    Idempotent: the same wiki and the same `now` rewrite the same bytes, and
    an incident that stopped monitoring loses its file, so the directory holds
    exactly the incidents under observation right now. Writes nothing under
    `wiki/`; the tick asserts that.

    One incident is one unit of work. An incident whose evaluation raises
    writes no facts, so its last good file stands, and comes back with verdict
    `ERROR` naming what happened: a single malformed digest must not cost
    every other incident its verdict, nor skip the prune. A pattern that timed
    out is the exception that is written: it is a durable fact about the
    operator's own signal rather than a failure to gather evidence, so
    `dbwiki incident show` must show it."""
    root, state = Path(wiki_root), Path(state_dir)
    revision = _revision(root)
    probes = _Probes(cfg, es, now)
    out: list[MonitoringFacts] = []
    for incident in load_incidents(root):
        if incident.status is not Status.MONITORING or not incident.monitoring:
            continue
        try:
            digests = load_window_digests(root, incident.db,
                                          incident.monitoring)
            facts = evaluate(incident, digests, now=now,
                             source_revision=revision,
                             probe=probes.of(incident, digests))
            atomic_write_text(
                _facts_path(state, facts.incident),
                json.dumps(facts.to_dict(), indent=1, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            facts = _failed(incident, exc, now=now, source_revision=revision)
        out.append(facts)
    _prune(state, {f.incident for f in out})
    return out


def _failed(incident: Incident, exc: BaseException, *, now: str,
            source_revision: str) -> MonitoringFacts:
    """The record for an incident whose evaluation raised. No facts file is
    written for it, so the last good verdict stands rather than being replaced
    by a lie; this is what the caller sees instead."""
    window = incident.monitoring
    return MonitoringFacts(
        schema_version=MONITORING_SCHEMA_VERSION,
        incident=incident.slug,
        db=incident.db,
        signal=signal_to_yaml(window.signal) if window else {},
        window={"start": window.start, "until": window.until} if window else {},
        observed=(),
        verdict=ERROR,
        contradictions=(f"evaluation failed: {type(exc).__name__}: {exc}",),
        evaluated_at=now,
        source_revision=source_revision,
    )


def _prune(state_dir: Path, keep: set[str]) -> None:
    """Drop facts for incidents nobody is monitoring any more, so a resolved
    incident cannot leave a stale verdict for a reader to trust."""
    d = Path(state_dir) / MONITORING_DIR
    if not d.is_dir():
        return
    for p in d.glob("*.json"):
        if p.stem not in keep:
            p.unlink(missing_ok=True)


def read_facts(state_dir: Path, slug: str) -> dict | None:
    """The last published evaluation for one incident, or None when there is
    none. Unreadable is None too: a corrupt state file must not cost the
    operator a page."""
    try:
        return json.loads(_facts_path(Path(state_dir), slug).read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class ClosureCase:
    """The last published evaluation of one incident, split into what argues
    for closing and what argues against, as human lines.

    It describes and never decides. Whether closing is legal is
    `lifecycle.TRANSITIONS`'; whether the closer must say what is still not
    safe is the portal's confirmation policy. Every line here can be checked
    against `.state/monitoring/<slug>.json` by hand, reading each digest as
    the rendered page `_rendered` names rather than the compacted file
    `observed` cites.

    Invariants: `verdict == MET` implies `against == ()`, inherited from
    `MonitoringFacts` (met means no contradictions, and coverage is complete
    by construction when met). `digests` lists the rendered digest page for
    every wiki-relative path `observed` cites, deduplicated in window-day
    order, so a reader can link the evidence without knowing the per-kind
    entry shapes. `stale` is whether `source_revision` differs from the
    revision the caller is showing: the verdict answers an older question."""

    verdict: Verdict
    evaluated_at: str
    source_revision: str
    stale: bool
    supporting: tuple[str, ...]   # "2026-08-28: TNS-12564 absent (digests/…)"
    against: tuple[str, ...]      # contradictions, then coverage gaps
    digests: tuple[str, ...]


def _rendered(path: str) -> str:
    """The digest page that sits beside the compacted file `observed` cites.

    `observed[*].digest` names `digests/<db>/<day>.json`, correctly, because
    that is the file the evaluator read. The closure case is the other
    question: it exists so a *reader* can check the verdict, and a reader
    checks it in the `.md` the compactor renders beside that file rather
    than in a blob of JSON. The compacted file is still in the raw facts the
    portal hands the page beside this case, so nothing is hidden. Anything
    not spelled `.json` is left alone, because a malformed facts file must
    come through this the way it came in."""
    if not path.endswith(".json"):
        return path
    return path.removesuffix(".json") + ".md"


def _cite(entry: dict, fact: str) -> str | None:
    """One supporting line: the day, what the evidence says, and where a
    reader checks it by hand. The ES probe entry carries no digest path, so
    it cites the live source it came from instead; an entry citing neither is
    nothing a reader could check, so it supports nothing."""
    day, path, via = entry.get("day"), entry.get("digest"), entry.get("via")
    if not day or not (path or via):
        return None
    where = _rendered(str(path)) if path else f"via {via}"
    return f"{day}: {fact} ({where})"


def _absent_line(entry: dict) -> str | None:
    code = entry.get("code")
    if not code or entry.get("count") != 0:
        return None
    return _cite(entry, f"{code} absent")


def _present_line(entry: dict) -> str | None:
    group = entry.get("group")
    if not entry.get("matched") or not group:
        return None
    return _cite(entry, f"{group} matched")


def _resumed_line(entry: dict) -> str | None:
    source, events = entry.get("source"), entry.get("events")
    if not source or not isinstance(events, int) or events <= 0:
        return None
    return _cite(entry, f"{source} carried {events} events")


_SUPPORTING: dict[str, Callable[[dict], str | None]] = {
    "error_absent": _absent_line,
    "event_present": _present_line,
    "flow_resumed": _resumed_line,
}


def _seq(value: object) -> tuple[object, ...]:
    """Whatever a malformed file left where a list belongs, as something safe
    to iterate."""
    return tuple(value) if isinstance(value, (list, tuple)) else ()


def _case_days(signal: Mapping[str, object], window: object) -> list[str]:
    """The days a case can be missing evidence for, rebuilt from the file's
    own signal and window. A window that will not rebuild yields none: a
    malformed file must not manufacture gaps nobody can check."""
    if not isinstance(window, Mapping):
        return []
    try:
        return window_days(MonitoringWindow(
            signal_from_yaml(signal), str(window.get("start", "")),
            str(window.get("until", ""))))
    except ActionMalformed:
        return []


def closure_case(facts: Mapping[str, object], *, revision: str) -> ClosureCase:
    """The for-and-against case, pure over the dict `read_facts` returns.

    The derivation lives here because the shape of `observed[*]` varies by
    signal kind (`error_absent` -> day/digest/code/count, `event_present` ->
    day/digest/matched/group, `flow_resumed` -> day/digest/source/events,
    `manual` -> nothing), and that variance is this module's private
    knowledge. Deriving it in `portal/` would put a rule about evidence into
    the shell.

    Never raises on a partial or malformed file, the same rule `read_facts`
    keeps: an unknown entry shape contributes nothing, and a dict with no
    `verdict` reads as `insufficient` with one `against` line saying so.

    `against` is `contradictions` in file order, then one line per window day
    (via `window_days`) that `observed` does not cover; `supporting` is one
    line per observed day whose entry is consistent with the signal. For a
    `manual` signal both are empty except one `against` line saying a human
    judges.

    A met verdict gets no coverage lines. `event_present` and `flow_resumed`
    go met on the first sighting, normally with window days still ahead of
    them, and `evaluate` is explicit that a positive sighting stands on its
    own while only a verdict resting on absence needs the whole window
    examined. Listing the unexamined days under `against` would argue against
    a verdict the evidence already settled, and would break the invariant
    `ClosureCase` states."""
    signal = facts.get("signal")
    signal = signal if isinstance(signal, Mapping) else {}
    line = _SUPPORTING.get(str(signal.get("kind", "")))
    entries = [e for e in _seq(facts.get("observed")) if isinstance(e, dict)]
    covered = {str(e.get("day", "")) for e in entries}
    stated = facts.get("verdict")
    known = stated in (MET, NOT_MET, INSUFFICIENT, ERROR)
    against = [] if known else ["the facts file carries no verdict"]
    against += [str(c) for c in _seq(facts.get("contradictions"))]
    if line is not None and stated != MET:
        against += [f"{day}: no digest examined"
                    for day in _case_days(signal, facts.get("window"))
                    if day not in covered]
    written_at = str(facts.get("source_revision") or "")
    return ClosureCase(
        verdict=str(stated) if known else INSUFFICIENT,
        evaluated_at=str(facts.get("evaluated_at") or ""),
        source_revision=written_at,
        stale=written_at != revision,
        supporting=tuple(filter(None, map(line, entries))) if line else (),
        against=tuple(against),
        digests=tuple(dict.fromkeys(_rendered(str(e["digest"]))
                                    for e in entries if e.get("digest"))),
    )
