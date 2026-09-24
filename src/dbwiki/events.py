"""The run-event contract behind the portal's automation-health views (2.4).

One flat frozen `RunEvent` carries every row shape the pipeline records, tagged
by `kind`. A single shape is the point: `wire`, the `CLASSIFICATION` table and
any future shipper each want one row to serialize, and a three-class hierarchy
would push a `match` into all three.

Reading is a **projection through a table**, never a copy. `_PROJECT` maps a
source name to `{producer key: (field name, coerce)}`, and `_project` reads
only the keys that table names. A producer that starts writing `prompt`,
`stdout`, `message`, `input`, `output` or `facts.telemetry_errors` therefore
has no code path that carries it into an event: there is no `**line`, no
`dict(entry)` and no wholesale nested-dict copy anywhere in this module. The
one free-text field is `error`, and both writers that reach it bound it
themselves: 300 characters for a database's failure (`cli.py:625`) and 200 for
a telemetry-capture failure (`orchestrate.py:165`).

Reading is pure. Nothing here opens a socket, runs a subprocess or writes:
`health.assess` does all three and is deliberately not reused.

Event ids are the join key and are unique by construction. A run_health line
carries no `event_id` of its own and one `run_id` yields exactly one such line,
so a RUN is identified by its `run_id`; its `dbs[]` fan out to
`f"{run_id}:{db}"`, or to the spelling `_fanout_id` falls back on where a
command repeats a database in one line, deterministic either way so that
re-reading the same file converges instead of duplicating; STAGE and START
lines keep the `event_id` their writer
minted (`health.py:247`, `health.py:394`). The degenerate line a telemetry
capture failure writes carries no `event_id` at all
(`orchestrate.py:163-167`), so it is identified by its run and task and two
such failures for one stage of one run converge to one row — the honest limit
of what that writer records. The one theoretical collision is a
`uuid4().hex[:12]` stage id that equals some 12-hex run id; it is not defended
against in code, because a guard would trade a real invariant (ids are a pure
function of the line) for a birthday-odds case.

`run_id` is never a join key on its own. The ADR-0001 analyst fold-in replays
the enqueuing tick's `run_id` days later, so one `run_id` legitimately spans
lines written far apart, and grouping by it is a grouping, not an identity.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date as _date
from datetime import timedelta as _timedelta
from enum import StrEnum
from pathlib import Path

from . import health, stats
from .state import StateStore

EVENT_SCHEMA_VERSION = 1


class Kind(StrEnum):
    """Which recorder wrote the line an event was projected from. RUN and
    DB_RUN both come from one `run_health.jsonl` line, the second by fan-out
    over its nested `dbs[]`."""

    RUN = "run"
    DB_RUN = "db_run"
    STAGE = "stage"
    START = "start"


class Visibility(StrEnum):
    """Who may see a field. Everything the recorders write is counts and
    identifiers (`health.py`, "facts only"), so VIEWER is the norm and
    OPERATOR marks the exceptions."""

    VIEWER = "viewer"
    OPERATOR = "operator"


@dataclass(frozen=True)
class Usage:
    """Tokens and cost for one agent stage, or the absence of them.

    `known=False` means the adapter reported no token counts, and nothing here
    ever fills the gap: no cost is derived from tokens and no token count is
    derived from a cost. This is `stats.py:12-16`'s rule ("unknown is never
    estimated") restated at the boundary that builds the value, so a consumer
    cannot sum an unknown-bearing set without first looking at `known`.

    `cost_known` is the same rule for the other currency, and it is a second
    flag because `known` cannot stand for it: `usage_known` is the writer's
    answer about *tokens* (`health.py:265` sets it from any of the three
    counters, and never from `cost_usd` alone). Without this flag a codex line
    that reported tokens and no price is indistinguishable from a free local
    model's true measured `0.0`, and a set summed over both would present the
    codex spend as a measured zero."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    known: bool = False
    cost_known: bool = False


@dataclass(frozen=True)
class Reason:
    """One `trigger.TriggerDecision` reason as the ledger stored it.

    `evidence` is projected to named scalar keys only. Upstream it is counts,
    codes and delta dicts (`trigger.py:96-129`) with no free text, and this
    keeps that true for a reader rather than trusting it."""

    code: str
    evidence: Mapping[str, object]


@dataclass(frozen=True)
class RunEvent:
    """One recorded row of pipeline work, whatever recorded it.

    Every field defaults, so a loader names only what its source actually
    carries and a field that is meaningless for a kind reads as `""`, `None`
    or `()` rather than as a missing attribute.

    The tick facts (`report_path` through `dry_run`, and a research run's
    `mode`) are hoisted flat on purpose. A `Mapping` bag for them would be
    exactly the hole that `CLASSIFICATION` completeness exists to close: an
    unclassified key could ship inside the bag without any test noticing."""

    schema_version: int = EVENT_SCHEMA_VERSION
    kind: Kind = Kind.RUN
    event_id: str = ""
    run_id: str = ""
    at: str = ""
    finished: str = ""
    duration_s: float | None = None
    command: str = ""
    task: str = ""
    db: str = ""
    outcome: str = ""
    error_category: str = ""
    error: str = ""
    decision: str = ""
    reason_codes: tuple[str, ...] = ()
    model_tier: str = ""
    adapter: str = ""
    model: str = ""
    mode: str = ""
    attempts: int | None = None
    timed_out: bool | None = None
    validation_ok: bool | None = None
    rolled_back: bool | None = None
    pages_touched: int | None = None
    digest_path: str = ""
    commit: str = ""
    usage: Usage | None = None
    report_path: str = ""
    report_error_category: str = ""
    html: str = ""
    alerted: int | None = None
    recovered: int | None = None
    monitoring: int | None = None
    db_count: int | None = None
    consolidation: bool | None = None
    dry_run: bool | None = None


#: Every `RunEvent` field to who may see it.
CLASSIFICATION: Mapping[str, Visibility] = {
    "schema_version": Visibility.VIEWER,
    "kind": Visibility.VIEWER,
    "event_id": Visibility.VIEWER,
    "run_id": Visibility.VIEWER,
    "at": Visibility.VIEWER,
    "finished": Visibility.VIEWER,
    "duration_s": Visibility.VIEWER,
    "command": Visibility.VIEWER,
    "task": Visibility.VIEWER,
    "db": Visibility.VIEWER,
    "outcome": Visibility.VIEWER,
    "error_category": Visibility.VIEWER,
    "error": Visibility.OPERATOR,
    "decision": Visibility.VIEWER,
    "reason_codes": Visibility.VIEWER,
    "model_tier": Visibility.VIEWER,
    "adapter": Visibility.VIEWER,
    "model": Visibility.VIEWER,
    "mode": Visibility.VIEWER,
    "attempts": Visibility.VIEWER,
    "timed_out": Visibility.VIEWER,
    "validation_ok": Visibility.VIEWER,
    "rolled_back": Visibility.VIEWER,
    "pages_touched": Visibility.VIEWER,
    "digest_path": Visibility.VIEWER,
    "commit": Visibility.VIEWER,
    "usage": Visibility.VIEWER,
    "report_path": Visibility.VIEWER,
    "report_error_category": Visibility.VIEWER,
    "html": Visibility.VIEWER,
    "alerted": Visibility.VIEWER,
    "recovered": Visibility.VIEWER,
    "monitoring": Visibility.VIEWER,
    "db_count": Visibility.VIEWER,
    "consolidation": Visibility.VIEWER,
    "dry_run": Visibility.VIEWER,
}

MAX_EVIDENCE_CHARS = 200


def _mapping(value: object) -> Mapping[str, object]:
    """`value` as a mapping, or an empty one. Every nested read goes through
    here, so a line whose `facts` or `dbs[]` entry is a list, a string or
    `null` costs itself and never the load."""
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    """A recorded string. `None` reads as absent rather than as "None"."""
    return "" if value is None else str(value)


def _num(value: object) -> float | None:
    """A recorded real number, or `None`. `bool` is excluded: `True` is an
    `int` to Python and a flag to every writer here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(value: object) -> int | None:
    """A recorded whole count, or `None`, with `bool` excluded as in `_num`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _flag(value: object) -> bool | None:
    """A recorded boolean, or `None` when the writer recorded something else.
    A truthiness coercion here would turn an error string into `True`."""
    return value if isinstance(value, bool) else None


def _codes(value: object) -> tuple[str, ...]:
    """A recorded list of bare codes. Non-string elements are dropped, which
    is what keeps `decision_reasons` a code list even if a writer ever put the
    full reason dicts back (`cli.cmd_run` flattens the evidence away today)."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _validated(value: object) -> bool | None:
    """`dbs[].validation` is the word "ok" or "failed" (`cli.cmd_run`)
    while `agent_runs.validation_ok` is a boolean. Both land in
    `RunEvent.validation_ok`; anything else is no answer, not a `False`."""
    if value == "ok":
        return True
    if value == "failed":
        return False
    return None


_Rule = tuple[str, Callable[[object], object]]

_PROJECT: Mapping[str, Mapping[str, _Rule]] = {
    "run_health": {
        "run_id": ("run_id", _text),
        "command": ("command", _text),
        "started": ("at", _text),
        "finished": ("finished", _text),
        "duration_s": ("duration_s", _num),
        "outcome": ("outcome", _text),
        "error_category": ("error_category", _text),
    },
    "run_health.facts": {
        "adapter": ("adapter", _text),
        "report": ("report_path", _text),
        "report_error_category": ("report_error_category", _text),
        "html": ("html", _text),
        "alerted": ("alerted", _count),
        "recovered": ("recovered", _count),
        "monitoring": ("monitoring", _count),
        "dbs": ("db_count", _count),
        "consolidation": ("consolidation", _flag),
        "mode": ("mode", _text),
        "dry_run": ("dry_run", _flag),
    },
    "run_health.dbs": {
        "db": ("db", _text),
        "decision": ("decision", _text),
        "decision_reasons": ("reason_codes", _codes),
        "model_tier": ("model_tier", _text),
        "adapter": ("adapter", _text),
        "validation": ("validation_ok", _validated),
        "outcome": ("outcome", _text),
        "error_category": ("error_category", _text),
        "error": ("error", _text),
        "commit": ("commit", _text),
        "digest": ("digest_path", _text),
    },
    "agent_runs": {
        "event_id": ("event_id", _text),
        "run_id": ("run_id", _text),
        "at": ("at", _text),
        "duration_s": ("duration_s", _num),
        "task": ("task", _text),
        "db": ("db", _text),
        "adapter": ("adapter", _text),
        "model": ("model", _text),
        "model_tier": ("model_tier", _text),
        "mode": ("mode", _text),
        "attempts": ("attempts", _count),
        "timed_out": ("timed_out", _flag),
        "validation_ok": ("validation_ok", _flag),
        "rolled_back": ("rolled_back", _flag),
        "pages_touched": ("pages_touched", _count),
        "telemetry_error": ("error", _text),
    },
    "run_starts": {
        "event_id": ("event_id", _text),
        "run_id": ("run_id", _text),
        "command": ("command", _text),
        "started": ("at", _text),
    },
}


def _project(table: Mapping[str, _Rule],
             line: Mapping[str, object]) -> dict[str, object]:
    """The `RunEvent` keyword arguments `table` can read out of `line`. A key
    the table does not name is never touched."""
    out: dict[str, object] = {}
    for key, (name, coerce) in table.items():
        if key in line:
            out[name] = coerce(line[key])
    return out


def _usage(line: Mapping[str, object]) -> Usage | None:
    """The stage's `Usage`, or `None` when the line predates usage recording
    or is the degenerate `{run_id, task, telemetry_error}` line a capture
    failure writes (`orchestrate.py:163-167`).

    `usage_known` is the writer's own answer (`health.py:263`) and is taken as
    given. Absent counts read as zero and are never inferred from each other:
    a line whose tokens are present while `usage_known` is false still yields
    `cost_usd == 0.0`.

    `cost_known` is read off the raw line before that coercion, because after
    it the absence and a measured zero are the same float. `_num` excludes
    `bool` and answers `None` for the `"unknown"` string the harness writes,
    so the flag is true only where the writer put a real number there."""
    if "usage_known" not in line:
        return None
    cost = _num(line.get("cost_usd"))
    return Usage(input_tokens=_count(line.get("input_tokens")) or 0,
                 output_tokens=_count(line.get("output_tokens")) or 0,
                 cost_usd=cost or 0.0,
                 known=bool(line.get("usage_known")),
                 cost_known=cost is not None)


def _fanout_id(parent: str, db: str, entry: Mapping[str, object],
               index: int, taken: set[str]) -> str:
    """The deterministic id of one DB_RUN.

    `f"{parent}:{db}"` wherever it identifies the row, which is every tick:
    `cmd_run` records one entry per database. `cmd_retry` records one entry per
    *digest* and so repeats a database within one line (20 ids, 27 events on
    the live log), where the digest's day tells the rows apart and the position
    in `dbs[]` is the last resort. Every spelling is a pure function of the
    line, so re-reading a file converges instead of duplicating."""
    day = Path(digest_key(_text(entry.get("digest")))).stem
    options = [f"{parent}:{db}"]
    if day:
        options.append(f"{parent}:{db}:{day}")
    options.append(f"{parent}:{db}#{index}")
    return next(option for option in options if option not in taken)


def _run_events(line: Mapping[str, object]) -> list[RunEvent]:
    """One `run_health.jsonl` line as its RUN plus one DB_RUN per `dbs[]`
    entry. The children inherit the run's clock because `dbs[]` records none
    of its own, and inventing one would be a guess."""
    run_fields = _project(_PROJECT["run_health"], line)
    run_fields.update(_project(_PROJECT["run_health.facts"],
                               _mapping(line.get("facts"))))
    run = RunEvent(kind=Kind.RUN, event_id=str(run_fields.get("run_id", "")),
                   **run_fields)
    out = [run]
    entries = line.get("dbs")
    if not isinstance(entries, (list, tuple)):
        return out
    taken: set[str] = set()
    for index, entry in enumerate(entries):
        db_fields = _project(_PROJECT["run_health.dbs"], _mapping(entry))
        event_id = _fanout_id(run.event_id, str(db_fields.get("db", "")),
                              _mapping(entry), index, taken)
        taken.add(event_id)
        out.append(RunEvent(kind=Kind.DB_RUN, event_id=event_id,
                            run_id=run.run_id, at=run.at,
                            finished=run.finished, command=run.command,
                            **db_fields))
    return out


def _stage_events(line: Mapping[str, object]) -> list[RunEvent]:
    """One `agent_runs.jsonl` line as its STAGE event.

    A line with no `event_id` is identified by its run and task instead. The
    only writer of one today is the telemetry-capture failure path, which
    writes `{run_id, task, telemetry_error}` and mints no id
    (`orchestrate.py:163-167`); leaving such an event unidentified would break
    the module's one join key for the one line that reports a recording bug."""
    fields = _project(_PROJECT["agent_runs"], line)
    if not fields.get("event_id"):
        fields["event_id"] = (f"{fields.get('run_id', '')}:"
                              f"{fields.get('task', '')}:unidentified")
    return [RunEvent(kind=Kind.STAGE, usage=_usage(line), **fields)]


def _start_events(line: Mapping[str, object]) -> list[RunEvent]:
    """One `elk/run_starts.jsonl` line as its START event."""
    return [RunEvent(kind=Kind.START,
                     **_project(_PROJECT["run_starts"], line))]


@dataclass(frozen=True)
class _Source:
    """One `.state/` log: where it lives, what caps it, and how a line of it
    becomes events."""

    name: str
    relpath: str
    cap: int
    build: Callable[[Mapping[str, object]], list[RunEvent]]


_SOURCES: tuple[_Source, ...] = (
    _Source("run_health", health.HEALTH_LOG, health.MAX_EVENTS, _run_events),
    _Source("agent_runs", health.AGENT_LOG, health.MAX_AGENT_EVENTS,
            _stage_events),
    _Source("run_starts", health.RUN_STARTS_LOG, health.MAX_RUN_START_EVENTS,
            _start_events),
)


@dataclass(frozen=True)
class Coverage:
    """How much history one log still holds.

    `lines` counts the lines that parsed, so it is what the caps actually
    bound and what the events were built from. `truncated` is `lines >= cap`:
    the logs drop oldest at the cap (`health.py:51-53`), so a full file means
    history has already been lost, not that it is about to be."""

    name: str
    lines: int
    cap: int
    oldest: str
    newest: str
    truncated: bool


@dataclass(frozen=True)
class Loaded:
    """Every event the three logs hold, newest first, beside one `Coverage`
    row per log so a view can say what the events do not cover."""

    events: tuple[RunEvent, ...]
    coverage: tuple[Coverage, ...]


def _newest_first(events: Sequence[RunEvent]) -> tuple[RunEvent, ...]:
    """Newest `at` first, ties broken by ascending `event_id`.

    Sorting by id first and by `at` second exploits Python's stable sort, so
    the order is a pure function of the events: a RUN sorts ahead of the
    DB_RUN children that share its clock, and two loads of the same files
    compare equal."""
    return tuple(sorted(sorted(events, key=lambda e: e.event_id),
                        key=lambda e: e.at, reverse=True))


def load_events(state_dir: Path) -> Loaded:
    """Every event in `.state/`, newest first, plus per-log coverage.

    Lines are read through `health._read_jsonl`, which owns the
    skip-unparseable-lines rule; a second copy of it here could disagree with
    what `dbwiki health` sees. A log that does not exist still gets a
    `Coverage` row, with `lines=0` and an empty span, because "this file was
    never written" and "this file is empty" are the same answer to a view and
    a missing row would read as a bug.

    `oldest`/`newest` come from the events a log produced rather than from its
    raw lines, so they are always timestamps some event actually carries."""
    events: list[RunEvent] = []
    coverage: list[Coverage] = []
    for source in _SOURCES:
        lines = health._read_jsonl(Path(state_dir) / source.relpath)
        produced = [ev for line in lines for ev in source.build(_mapping(line))]
        stamps = sorted(ev.at for ev in produced if ev.at)
        coverage.append(Coverage(name=source.name, lines=len(lines),
                                 cap=source.cap,
                                 oldest=stamps[0] if stamps else "",
                                 newest=stamps[-1] if stamps else "",
                                 truncated=len(lines) >= source.cap))
        events.extend(produced)
    return Loaded(events=_newest_first(events), coverage=tuple(coverage))


def stages(state_dir: Path) -> tuple[RunEvent, ...]:
    """Every STAGE event `.state/agent_runs.jsonl` holds, newest first.

    `load_events`' agent-log arm alone, for a caller that wants the ledger's
    stages and nothing else. Not a second parse: the `_Source` row that builds
    them is the row `load_events` uses, so a change to how an agent line
    becomes a STAGE event reaches both readers.

    It exists because reading all three logs to answer what one of them holds
    is most of the cost. On the live `.state/` the health log is a fifth of
    the lines and five sixths of the events — 160 ms of 235 — and the incident
    screen would pay that on every open to draw a list of ticks.

    No `Coverage`: a caller that has to state what bounds its answer wants
    `load_events`, which is why the count is not returned here to be
    half-stated somewhere."""
    source = next(s for s in _SOURCES if s.relpath == health.AGENT_LOG)
    lines = health._read_jsonl(Path(state_dir) / source.relpath)
    return _newest_first([ev for line in lines
                          for ev in source.build(_mapping(line))])


def digest_key(path: str) -> str:
    """A digest path normalized to the spelling the ingest ledger uses.

    The two writers disagree by one component. `cli._rel` makes a DB_RUN's
    `digest` relative to `cfg.root`, so it reads `wiki/digests/<db>/<day>.json`,
    while the ledger key is `jp.relative_to(cfg.wiki_repo)` and reads
    `digests/<db>/<day>.json`. Any join between the two must normalize both
    sides through here. `RunEvent.digest_path` deliberately keeps the source
    spelling verbatim, so nothing silently rewrites what a tick recorded.

    The tail is taken from the last `digests` component, so a checkout nested
    under another `digests` directory still normalizes to the ledger key. A
    path with no such component is returned unchanged."""
    parts = str(path).split("/")
    for index in range(len(parts) - 1, -1, -1):
        if parts[index] == "digests":
            return "/".join(parts[index:])
    return str(path)


def _evidence(value: object) -> dict[str, object]:
    """A reason's evidence reduced to named scalars, strings bounded.

    Upstream evidence is counts, codes and delta dicts (`trigger.py:96-129`),
    never free text, and a nested structure would be a new shape for every
    consumer to handle. Dropping non-scalars keeps the promise checkable
    instead of assumed."""
    out: dict[str, object] = {}
    for key, item in _mapping(value).items():
        if not isinstance(key, str):
            continue
        if isinstance(item, str):
            out[key] = item[:MAX_EVIDENCE_CHARS]
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out[key] = item
        elif isinstance(item, bool):
            out[key] = item
    return out


def decisions(state_dir: Path) -> Mapping[str, tuple[Reason, ...]]:
    """Normalized digest key -> the reasons behind the ledger's stored
    decision, in the order `trigger.decide` built them.

    This is the only place full reason evidence survives: `cli.py:599` writes
    bare codes into the run-health event and flattens the evidence away.

    The ledger holds one `last_decision` per digest (`state.merge_ledger_entry`
    overwrites it every tick), so joining this to a DB_RUN is honest only for
    the newest run that touched that digest. An older run's row must not be
    decorated with what a later tick decided."""
    out: dict[str, tuple[Reason, ...]] = {}
    ledger = _mapping(StateStore(Path(state_dir)).get_ledger())
    for rel, raw in sorted(ledger.items()):
        last = _mapping(_mapping(raw).get("last_decision"))
        if not last:
            continue
        reasons = last.get("reasons")
        items = reasons if isinstance(reasons, (list, tuple)) else ()
        out[digest_key(rel)] = tuple(
            Reason(code=_text(_mapping(item).get("code")),
                   evidence=_evidence(_mapping(item).get("evidence")))
            for item in items if isinstance(item, Mapping))
    return out


def pending(events: Sequence[RunEvent]) -> tuple[RunEvent, ...]:
    """The START events whose `run_id` never produced a RUN event: runs that
    hung, were killed, or are in flight right now.

    `run_starts.jsonl` exists precisely because run_health records only finish
    events (`health.py:387-401`), so this difference is the only evidence a
    dead tick leaves. It cannot separate "still running" from "dead"; a caller
    that wants that must weigh `at` against the clock itself."""
    finished = {ev.run_id for ev in events if ev.kind is Kind.RUN}
    return _newest_first([ev for ev in events if ev.kind is Kind.START
                          and ev.run_id not in finished])


def backlog(state_dir: Path) -> tuple[Mapping[str, str], ...]:
    """Ledger entries that are real debt: neither ingested, nor failed, nor
    deliberately skipped, sorted by digest.

    `health.deliberate_skip` is the one owner of the skip rule, so this and
    `dbwiki health` cannot drift into two answers about the same entry.
    `failed` entries are debt of a different kind and belong to
    `health.plan_retries`, which is why they are excluded here as `assess`
    excludes them.

    `db` is the digest path's parent directory name, matching `assess`."""
    rows = []
    ledger = _mapping(StateStore(Path(state_dir)).get_ledger())
    for rel, raw in sorted(ledger.items()):
        entry = _mapping(raw)
        if entry.get("status") in ("ingested", "failed"):
            continue
        if health.deliberate_skip(entry):
            continue
        outcome = _mapping(entry.get("last_decision")).get("outcome")
        rows.append({"digest": rel, "db": Path(rel).parent.name,
                     "decision": _text(outcome)})
    return tuple(rows)


@dataclass(frozen=True)
class Totals:
    """What one set of STAGE events measured, with the sample behind every
    measure that has one.

    Three currencies because there is only one honest way to say what the loop
    cost: `seconds` is reported by 624/626 live lines, `tokens` by most, and
    `cost_usd` by twelve. Each dense count rides beside its sum so a consumer
    cannot present a partial sum as a total (`wire.py:1085-1087`), and none of
    the three is ever derived from another — `Usage`'s rule, restated for a
    set instead of a line.

    `unpriced_stages` counts stages whose line carried a usage block with no
    `cost_usd` in it. It is deliberately not `stages - priced_stages`, which
    would also sweep in the stages that recorded no usage at all: those
    measured nothing rather than measured no price, and the page never has to
    subtract to find out which it is looking at.

    There is no failure count. A STAGE line carries neither `outcome` nor
    `error_category` (`_PROJECT["agent_runs"]` projects neither), so any such
    field would be a structural zero with a name that lies. `rolled_back` is
    the one quality verdict those lines do record; the failures live on the
    RUN events, counted by `Day.failed` and grouped by `alerts.recurring`."""

    stages: int = 0
    rolled_back: int = 0
    tokens: int = 0
    token_stages: int = 0
    seconds: float = 0.0
    timed_stages: int = 0
    cost_usd: float = 0.0
    priced_stages: int = 0
    unpriced_stages: int = 0


def totals(evs: Sequence[RunEvent]) -> Totals:
    """The measure vector over every STAGE event in `evs`.

    Non-STAGE events are ignored rather than refused: every caller hands this
    a mixed set (a run's children, a day's slice) and filtering at each call
    site would be the same line written three times."""
    stages = [ev for ev in evs if ev.kind is Kind.STAGE]
    usages = [ev.usage for ev in stages if ev.usage is not None]
    counted = [u for u in usages if u.known]
    priced = [u for u in usages if u.cost_known]
    timed = [ev.duration_s for ev in stages if ev.duration_s is not None]
    return Totals(
        stages=len(stages),
        rolled_back=sum(1 for ev in stages if ev.rolled_back),
        tokens=sum(u.input_tokens + u.output_tokens for u in counted),
        token_stages=len(counted),
        seconds=sum(timed),
        timed_stages=len(timed),
        cost_usd=sum(u.cost_usd for u in priced),
        priced_stages=len(priced),
        unpriced_stages=sum(1 for u in usages if not u.cost_known))


@dataclass(frozen=True)
class Day:
    """One UTC calendar day of loop work. `runs` and `failed` count RUN events
    started that day; `totals` measures the STAGE events recorded that day.

    The two are counted from different logs with different caps, which is why
    they are separate fields rather than folded into `Totals`: `coverage`
    already tells the reader the two logs hold different spans, and a single
    merged count would hide that."""

    day: str
    runs: int
    failed: int
    totals: Totals


def _bucket(at: str) -> str:
    """The UTC calendar day an event belongs to, or `""` when it belongs to
    none. `at` is second-granularity UTC (`%Y-%m-%dT%H:%M:%SZ`), so the day is
    a ten-character slice and needs no clock, no timezone and no `datetime`
    round trip — only the parse below, which is what refuses a slice that is
    not a date."""
    day = str(at)[:10]
    try:
        _date.fromisoformat(day)
    except ValueError:
        return ""
    return day


def days(evs: Sequence[RunEvent]) -> tuple[Day, ...]:
    """One `Day` per UTC calendar day from the oldest event to the newest,
    oldest first, **including days on which nothing was recorded**.

    A gap is a fact — the loop did not run on 2026-08-28 — and a table that
    skipped it would draw a five-week span as thirty adjacent rows and read as
    a denser loop than there was. This is the same reason `coverage` crosses
    the wire rather than being trimmed away.

    Bucketed by day and never by week: the logs hold five weeks, which is six
    week-buckets, and six points is not a trend.

    An event with no readable `at` is dropped: it belongs to no day, and
    putting it in the newest one would attribute work to a day that did not do
    it."""
    dated = [(day, ev) for ev in evs if (day := _bucket(ev.at))]
    if not dated:
        return ()
    first = _date.fromisoformat(min(day for day, _ in dated))
    last = _date.fromisoformat(max(day for day, _ in dated))
    held: dict[str, list[RunEvent]] = {}
    for day, ev in dated:
        held.setdefault(day, []).append(ev)
    out = []
    for step in range((last - first).days + 1):
        day = (first + _timedelta(days=step)).isoformat()
        same = held.get(day, ())
        runs = [ev for ev in same if ev.kind is Kind.RUN]
        out.append(Day(day=day, runs=len(runs),
                       failed=sum(1 for ev in runs if ev.outcome == "failed"),
                       totals=totals(same)))
    return tuple(out)


@dataclass(frozen=True)
class ModelRoll:
    """What one model did across a set of STAGE events.

    Keyed by the model name alone and not by adapter as well. The same weights
    reached through two adapters is one model doing the work, which is the
    question this answers; `adapters` carries the routes it arrived by, so a
    reader who wants that split can still see it.

    `ok` and `failed` are the two arms of `validation_ok`, and they do not add
    up to `totals.stages`: a line that recorded no verdict is in neither, and
    inventing a third bucket named after the absence would put the same fact
    on the wire twice. `rolled_back` stays on `totals`, which is where every
    other set of stages carries it, rather than being copied up here.

    `tiers` counts the lines each `model_tier` was recorded under rather than
    naming one, because the tier is a routing decision per stage: the live
    ledger runs one model as `cheap` on lint and `strong` on report, and a
    single label would have to pick a winner."""

    model: str
    adapters: tuple[str, ...]
    tiers: Mapping[str, int]
    ok: int
    failed: int
    totals: Totals


def by_model(evs: Sequence[RunEvent]) -> tuple[ModelRoll, ...]:
    """One `ModelRoll` per model named by a STAGE event in `evs`, busiest
    first and then by name, so the table's order is the operator's question
    ("what is doing the work") and is stable between two reads of one set.

    A stage whose line named no model rolls up under `""`. It is a real row —
    something ran and recorded no model — and dropping it would make the
    stage counts here disagree with `totals(evs)` for no visible reason.

    The partition is `stats.group(..., by="model_only")`, the grouping
    `dbwiki stats` rolls its runs up through (incident-workbench #31), so a
    model's stage count here is the sum of its `--by model` rows' `n` over
    the same lines. The measure stays this module's: the CLI's has no tokens
    and folds a rollback into `failed`, which is the split `ModelRoll` keeps
    apart."""
    rows = [{"model": ev.model, "stage": ev} for ev in evs
            if ev.kind is Kind.STAGE]
    rolled = stats.group(rows, by="model_only", agg=lambda held: {
        "roll": _model_roll([row["stage"] for row in held])})
    return tuple(sorted((group["roll"] for group in rolled),
                        key=lambda r: (-r.totals.stages, r.model)))


def _model_roll(stages: Sequence[RunEvent]) -> ModelRoll:
    """One model's STAGE events, all naming the same `model`, measured."""
    tiers: dict[str, int] = {}
    for ev in stages:
        tiers[ev.model_tier] = tiers.get(ev.model_tier, 0) + 1
    return ModelRoll(
        model=stages[0].model,
        adapters=tuple(sorted({ev.adapter for ev in stages})),
        tiers=dict(sorted(tiers.items())),
        ok=sum(1 for ev in stages if ev.validation_ok is True),
        failed=sum(1 for ev in stages if ev.validation_ok is False),
        totals=totals(stages))


def previous_run(evs: Sequence[RunEvent], run: RunEvent) -> RunEvent | None:
    """The run this one should be read against: the newest RUN event that
    started strictly before `run.at`, ran the same `command`, and carries a
    *different* `run_id`.

    Same `command` because a `run` tick and a `lint` invocation measure
    different work and a comparison between them means nothing; the live logs
    hold ten commands, 255 of 325 lines being `run`.

    Different `run_id` because `run_id` is a grouping and not an identity: an
    analyst fold-in (ADR-0001 `queue.fold_results`) re-records under an old
    id, so a candidate sharing this run's id is this run recorded twice and a
    comparison against it would draw a fold-in as a regression.

    None when the logs no longer hold one. That is an answer — `coverage` says
    how far back the answer goes — and never a 404."""
    earlier = [ev for ev in evs
               if ev.kind is Kind.RUN and ev.command == run.command
               and ev.run_id != run.run_id and ev.at and ev.at < run.at]
    return max(earlier, key=lambda ev: (ev.at, ev.event_id), default=None)


STAGE_NAMES = ("tick", "discover/compact", "decide", "ingest", "report",
               "render", "alerts")

STATUSES = ("succeeded", "warning", "failed", "skipped", "pending")

_AGENTIC = frozenset({"ingest", "report"})


@dataclass(frozen=True)
class Stage:
    """One row of a run's stage timeline.

    `started`, `finished` and `duration_s` are empty wherever the tick records
    no per-stage clock, which is everywhere but `tick` itself. A plausible
    interpolation would be indistinguishable from a measurement in the page.

    `detail["inferred"]` marks a status derived from the shape of what was
    recorded rather than read off a recorded fact. `detail["reason"]` says why
    a stage is `skipped` or `pending`."""

    name: str
    status: str
    started: str
    finished: str
    duration_s: float | None
    agentic: bool
    detail: Mapping[str, object]


def _stage(name: str, status: str, detail: Mapping[str, object],
           *, started: str = "", finished: str = "",
           duration_s: float | None = None) -> Stage:
    return Stage(name=name, status=status, started=started, finished=finished,
                 duration_s=duration_s, agentic=name in _AGENTIC,
                 detail=detail)


def _tick_stage(run: RunEvent) -> Stage:
    """The run itself. The only stage with a clock, because it is the only one
    the recorder times (`health.RunRecord.to_event`)."""
    if run.outcome == "ok":
        status, detail = "succeeded", {}
    elif run.outcome == "failed":
        status = "failed"
        detail = {"error_category": run.error_category} if run.error_category \
            else {}
    else:
        status = "pending"
        detail = {"reason": "no outcome recorded", "inferred": True}
    return _stage("tick", status, detail, started=run.at,
                  finished=run.finished, duration_s=run.duration_s)


def _discover_stage(dbs: Sequence[RunEvent]) -> Stage:
    """Compaction, inferred from the DB_RUN children. A child that carries an
    `error` but no `decision` died inside compaction, before `decide` was ever
    called (`cli.py:589-595`)."""
    if not dbs:
        return _stage("discover/compact", "skipped",
                      {"reason": "no databases discovered", "inferred": True})
    broken = [db.db for db in dbs if db.error and not db.decision]
    status = "failed" if broken else "succeeded"
    detail: dict[str, object] = {"databases": len(dbs), "inferred": True}
    if broken:
        detail["failed"] = tuple(broken)
    return _stage("discover/compact", status, detail)


def _decide_stage(dbs: Sequence[RunEvent]) -> Stage:
    """The trigger decisions, inferred from which children reached one."""
    decided = [db for db in dbs if db.decision]
    if not decided:
        return _stage("decide", "skipped",
                      {"reason": "no database reached a decision",
                       "inferred": True})
    counts: dict[str, int] = {}
    for db in decided:
        counts[db.decision] = counts.get(db.decision, 0) + 1
    return _stage("decide", "succeeded",
                  {"decisions": dict(sorted(counts.items())),
                   "inferred": True})


def _ingest_stage(dbs: Sequence[RunEvent],
                  stages: Sequence[RunEvent]) -> Stage:
    """The agent ingests, inferred from the DB_RUN outcomes and decorated with
    the STAGE lines whose `task` is `ingest`.

    One database failing never ends the tick (`cli.py:615`), so errors beside
    successes are a `warning` and only errors with no success at all are a
    `failed`. A tick where every decision was a skip attempted nothing, which
    is not a failure of this stage."""
    attempted = [db for db in dbs
                 if db.outcome in ("ingested", "skipped")
                 or (db.error and db.decision and db.decision != "skip")]
    decided = [db for db in dbs if db.decision]
    if not attempted:
        reason = ("every database was a deliberate skip" if decided
                  and all(db.decision == "skip" for db in decided)
                  else "no database reached the ingest stage")
        return _stage("ingest", "skipped",
                      {"reason": reason, "inferred": True})
    failed = [db for db in attempted if db.error]
    succeeded = [db for db in attempted if not db.error]
    if failed and not succeeded:
        status = "failed"
    elif failed:
        status = "warning"
    else:
        status = "succeeded"
    lines = [ev for ev in stages if ev.task == "ingest"]
    detail: dict[str, object] = {
        "attempted": len(attempted),
        "ingested": sum(1 for db in succeeded if db.outcome == "ingested"),
        "unchanged": sum(1 for db in succeeded if db.outcome == "skipped"),
        "failed": len(failed),
        "inferred": True,
    }
    if lines:
        detail["attempts"] = sum(ev.attempts or 1 for ev in lines)
        detail["timed_out"] = sum(1 for ev in lines if ev.timed_out)
    return _stage("ingest", status, detail)


def _report_stage(run: RunEvent) -> Stage:
    """The daily report, read straight off the tick's own facts
    (`cli.py:630-641`)."""
    if run.report_path:
        return _stage("report", "succeeded", {"path": run.report_path})
    if run.report_error_category:
        return _stage("report", "failed",
                      {"error_category": run.report_error_category})
    return _stage("report", "skipped",
                  {"reason": "nothing ingested, or nothing notable and not a "
                             "consolidation tick"})


def _render_stage(run: RunEvent) -> Stage:
    """The daily HTML page. `(render failed)` is a `warning` and never a
    `failed`: `render_html` is best-effort by design, and a summary page is
    not the run (`orchestrate.py:271-273`)."""
    if not run.html:
        return _stage("render", "skipped",
                      {"reason": f"not recorded by the "
                                 f"{run.command or 'unknown'} command"})
    if run.html == "(render failed)":
        return _stage("render", "warning", {"reason": "best-effort render "
                                                      "failed; the run is "
                                                      "unaffected"})
    if run.html == "(nothing to commit)":
        return _stage("render", "skipped", {"reason": "nothing to commit"})
    return _stage("render", "succeeded", {"commit": run.html})


def _alerts_stage(run: RunEvent) -> Stage:
    """The alert dispatch, read off the counts `alerts.dispatch` returned."""
    if run.alerted is None:
        return _stage("alerts", "skipped",
                      {"reason": "alerts are off, or the tick never reached "
                                 "them"})
    detail: dict[str, object] = {"alerted": run.alerted}
    if run.recovered is not None:
        detail["recovered"] = run.recovered
    return _stage("alerts", "succeeded", detail)


def timeline(run: RunEvent,
             children: Sequence[RunEvent]) -> tuple[Stage, ...]:
    """The seven-stage view of one run, always all seven and always in
    `STAGE_NAMES` order.

    `run` is a RUN or a START event; `children` are the DB_RUN and STAGE events
    that share its `run_id`. A START has no facts at all yet, so every stage is
    `pending`. A command other than `run` has only the stages `cmd_run`
    performs, so every stage but `tick` is `skipped` naming the command rather
    than claiming the stage failed.

    Nothing here guesses a duration or invents a clock. A status that had to be
    derived from the shape of the children rather than read off a recorded fact
    says so in `detail["inferred"]`."""
    if run.kind is Kind.START:
        detail: Mapping[str, object] = {"reason": "run has not finished"}
        return tuple(_stage(name, "pending", detail,
                            started=run.at if name == "tick" else "")
                     for name in STAGE_NAMES)
    tick = _tick_stage(run)
    if run.command != "run":
        reason = (f"the {run.command or 'unknown'} command does not run this "
                  f"stage")
        return (tick,) + tuple(_stage(name, "skipped", {"reason": reason})
                               for name in STAGE_NAMES[1:])
    dbs = [ev for ev in children if ev.kind is Kind.DB_RUN]
    stages = [ev for ev in children if ev.kind is Kind.STAGE]
    return (tick, _discover_stage(dbs), _decide_stage(dbs),
            _ingest_stage(dbs, stages), _report_stage(run),
            _render_stage(run), _alerts_stage(run))


__all__ = ["CLASSIFICATION", "EVENT_SCHEMA_VERSION", "MAX_EVIDENCE_CHARS",
           "STAGE_NAMES", "STATUSES", "Coverage", "Day", "Kind", "Loaded",
           "Reason", "RunEvent", "Stage", "Totals", "Usage", "Visibility",
           "backlog", "days", "decisions", "digest_key", "load_events",
           "pending", "previous_run", "timeline", "totals"]
