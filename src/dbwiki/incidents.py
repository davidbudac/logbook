"""What an incident is, and how a human action is written into its page.

Two jobs, one owner, because they protect the same decision: what an incident
page may say.

Everything here is pure over text. No git, no Path except the one disk walker,
no lifecycle rules: whether a transition is allowed belongs to
`lifecycle.TRANSITIONS`. This module can say what a page means and render a
page that means something; it never decides what should happen next.

Reading policy, deliberate: an unrecognised `status:` reads as OPEN, never as
resolved. An attention queue must fail loud. `Incident.unknown_status` carries
the raw value so `lint`'s `incident-status-invalid` rule turns the typo into a
blocking error and `lifecycle.build` refuses to transition such a page.
"""

import datetime as dt
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import yaml

from .pagetext import (FM_RE, fm_flow_map, frontmatter, nl, oneline,
                       sections, set_frontmatter)
from .patterns import ORA_CODE_RE
from .validate import instant, is_safe_id, one_line

INCIDENT_DIR = "incidents"

ACTION_HEAD_RE = re.compile(r"\A##\s+Action\s+(\d\S*)\s*\Z")
ISO_Z_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
ERROR_LINK_RE = re.compile(r"\[\[errors/([^\]|#]+)\]\]")
DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
EVIDENCE_RE = re.compile(
    r"\Adigests/([\w.-]+)/(\d{4}-\d{2}-\d{2})\.(json|md)\Z")

ACTION_KINDS = ("record-action", "start-monitoring", "extend-monitoring",
                "resolve", "reopen", "merge")

ACTION_FIELDS = ("kind", "actor", "intent", "summary", "status_after",
                 "ticket", "outcome", "rollback", "window", "evidence",
                 "error_pages")

MAX_PATTERN = 200

#: How `lifecycle` words a `merge` record's summary: `duplicate of
#: incidents/<kept>`. `Incident.superseded_by` reads the lineage back from it.
MERGE_SUMMARY = "duplicate of "


class Status(StrEnum):
    """The whole vocabulary. `resolved` is terminal; everything else is active.
    StrEnum so `str(status)` renders the wiki literal (`yaml.safe_dump`
    refuses enum members; the codec converts with `str()` first)."""

    OPEN = "open"
    MONITORING = "monitoring"
    RESOLVED = "resolved"

    @property
    def label(self) -> str:
        """Badge text shared by daily HTML, the CLI, and the portal:
        `Open incident`, `Monitoring`, `Resolved`."""
        return _LABELS[self]


_LABELS = {Status.OPEN: "Open incident",
           Status.MONITORING: "Monitoring",
           Status.RESOLVED: "Resolved"}

ACTIVE = frozenset({Status.OPEN, Status.MONITORING})


def parse_status(raw: object) -> Status | None:
    """A frontmatter `status:` value as a member, or None when it is not one.
    The only place a string is compared against the vocabulary."""
    try:
        return Status(str(raw))
    except ValueError:
        return None


def is_active(status: Status) -> bool:
    """True for everything but `resolved`. One predicate, four callers."""
    return status in ACTIVE


class Outcome(StrEnum):
    """What the operator says happened. `pending` is the honest default right
    after an action; `rejected` is how a declined recommendation is recorded
    as an ordinary action."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


class ActionMalformed(ValueError):
    """An `## Action` section (or a record under construction) that cannot be
    a valid record. Raised by `ActionRecord.__post_init__` and `parse_action`;
    `parse_actions` collects these so `lint` reports every bad section."""


class ActionCollision(ActionMalformed):
    """The page already records a *different* valid action at this record's
    `at`. `at` is the section's identity, so writing the new one would
    replace the earlier act on the page while `log.md` kept both lines: an
    audit record lost. Raised by `append_action`; a caller with its own
    clock (the CLI) may retry one second on, a caller replaying a pinned
    `at` gets the refusal. A subclass of ActionMalformed, so every surface
    that already refuses a malformed record refuses this the same way."""


def _instant(name: str, value: str) -> str:
    """`validate.instant` as this module's refusal: the shape *and* a day
    that exists. `2026-02-30T10:00:00Z` has the shape, and once written as
    `updated:` it made PyYAML raise in every reader of the page."""
    try:
        return instant(value)
    except ValueError as exc:
        raise ActionMalformed(f"{name}: {value!r} is not a real "
                              f"YYYY-MM-DDTHH:MM:SSZ instant") from exc


def _one_line(name: str, value: str) -> str:
    """`validate.one_line` as this module's refusal. Every character
    `str.splitlines` breaks on, not just `\\n` and `\\r`: YAML folds
    `\\x85`, U+2028 and U+2029 as line breaks too, so any of them in a scalar
    changes the value on the way back in."""
    try:
        return one_line(value, name)
    except ValueError as exc:
        raise ActionMalformed(str(exc)) from exc


def _not_a_digest(raw: object) -> ActionMalformed:
    """One wording for the refusal, shared by the parse and the constructor
    so the two cannot drift apart in an operator's terminal."""
    return ActionMalformed(f"evidence: {raw!r} is not a digest path; cite "
                           f"digests/<db>/YYYY-MM-DD.md or .json")


@dataclass(frozen=True)
class EvidenceRef:
    """A digest an action cites: which database, which day, which rendering.

    Evidence is what the compactor observed. An `errors/` page is a
    conclusion the wiki already drew, so citing one would let an action rest
    on its own reasoning; only a digest can back it. Split into fields rather
    than kept as a path string because the portal renders `db` and `day` as
    their own columns, and because ADR-0003's phase-2.6 `evidence_ref` grows
    from here. The wire form stays the bare path, so the bytes on the page do
    not move.

    `EVIDENCE_RE` is deliberately no looser than `lint.DIGEST_REF_RE`: every
    ref accepted here is one lint's `digest-missing` rule will scan for once
    the record is rendered into the page. `db` must also be a
    `validate.safe_id` (issue 09): `[\\w.-]+` alone let `..` through, and
    `digests/../2026-08-05.md` names a file outside the digest tree."""

    db: str
    day: str
    suffix: str = "md"

    def __post_init__(self) -> None:
        if not EVIDENCE_RE.match(str(self)) or not is_safe_id(self.db):
            raise _not_a_digest(str(self))
        try:
            dt.date.fromisoformat(self.day)
        except ValueError as exc:
            raise ActionMalformed(
                f"evidence: {self.day!r} is not a real day") from exc

    def __str__(self) -> str:
        return f"digests/{self.db}/{self.day}.{self.suffix}"

    @staticmethod
    def parse(raw: str) -> "EvidenceRef":
        """A raw operator string into a ref, naming the whole string when it
        is not one: the operator typed a path, not a `db` and a `day`, and a
        message about either half alone would not show them their typo."""
        match = EVIDENCE_RE.match(raw.strip())
        if match is None:
            raise _not_a_digest(raw)
        return EvidenceRef(*match.groups())


@dataclass(frozen=True)
class ErrorAbsent:
    """No occurrence of `code` anywhere in the window."""

    code: str

    def __post_init__(self) -> None:
        _one_line("code", self.code)


@dataclass(frozen=True)
class EventPresent:
    """A log line matching `pattern` appears in the window.

    `pattern` is capped at MAX_PATTERN characters. It lives on one
    frontmatter line inside a flow mapping beside kind, start and until, so
    the cap is what keeps that line readable in a diff, and every real
    recovery signal is a handful of alternatives. It also bounds how much
    nested quantification an operator can express, though the defence that
    actually holds is `monitoring.PATTERN_TIMEOUT` behind it.

    Whether the pattern compiles is deliberately not checked here. A pattern
    that will not compile comes back from `evaluate` as a contradiction
    naming it, which is more use to the operator than a page whose
    `monitoring:` window silently reads as unreadable."""

    pattern: str

    def __post_init__(self) -> None:
        _one_line("pattern", self.pattern)
        if len(self.pattern) > MAX_PATTERN:
            raise ActionMalformed(
                f"pattern: {len(self.pattern)} characters is over the "
                f"{MAX_PATTERN}-character limit")


@dataclass(frozen=True)
class FlowResumed:
    """`source` produced its expected volume again during the window."""

    source: str

    def __post_init__(self) -> None:
        _one_line("source", self.source)


@dataclass(frozen=True)
class Manual:
    """A human will judge; `description` is what they promised to check.
    Deterministic evaluation (0.7) reports `insufficient` and stops."""

    description: str

    def __post_init__(self) -> None:
        _one_line("description", self.description)


RecoverySignal = ErrorAbsent | EventPresent | FlowResumed | Manual


def signal_to_yaml(signal: RecoverySignal) -> dict[str, str]:
    """`{"kind": "error_absent", "code": "TNS-12564"}` and friends. Flat and
    all-string so one dict serves the fenced yaml block and the frontmatter
    flow mapping."""
    match signal:
        case ErrorAbsent(code=code):
            return {"kind": "error_absent", "code": code}
        case EventPresent(pattern=pattern):
            return {"kind": "event_present", "pattern": pattern}
        case FlowResumed(source=source):
            return {"kind": "flow_resumed", "source": source}
        case Manual(description=description):
            return {"kind": "manual", "description": description}
        case _:
            raise ActionMalformed(
                f"window: {type(signal).__name__} is not a recovery signal")


def signal_from_yaml(data: Mapping[str, object]) -> RecoverySignal:
    """Inverse of `signal_to_yaml`. Raises ActionMalformed on an unknown kind
    or missing payload; this is a boundary, nothing downstream re-checks."""
    kind = str(data.get("kind", ""))
    match kind:
        case "error_absent":
            build, key = ErrorAbsent, "code"
        case "event_present":
            build, key = EventPresent, "pattern"
        case "flow_resumed":
            build, key = FlowResumed, "source"
        case "manual":
            build, key = Manual, "description"
        case _:
            raise ActionMalformed(f"window: unknown signal kind {kind!r}")
    payload = data.get(key)
    if payload is None or str(payload) == "":
        raise ActionMalformed(
            f"window: signal kind {kind!r} needs a non-empty {key}")
    return build(str(payload))


@dataclass(frozen=True)
class MonitoringWindow:
    """The observation window in effect, `[start, until)`: what we wait for
    and until when. One value, so a window without a signal is unrepresentable.

    Lives in frontmatter as one flow mapping. Frontmatter owns current state;
    the action log owns history. They agree when the action is written and
    diverge legitimately after an `ExtendMonitoring`."""

    signal: RecoverySignal
    start: str
    until: str

    def __post_init__(self) -> None:
        _instant("start", self.start)
        _instant("until", self.until)
        if self.until <= self.start:
            raise ActionMalformed(
                f"until: {self.until!r} is not later than start "
                f"{self.start!r}")

    def to_frontmatter(self) -> str:
        """`{kind: '...', ..., start: '...', until: '...'}` via
        `pagetext.fm_flow_map`, fixed key order."""
        return fm_flow_map({**signal_to_yaml(self.signal),
                            "start": self.start, "until": self.until})

    @staticmethod
    def from_frontmatter(value: object) -> "MonitoringWindow | None":
        """Parse the `monitoring:` value. None when absent or unreadable;
        lint's `incident-status-invalid` reports a `monitoring` status with
        no readable window, so the silence here is never the last word."""
        if not isinstance(value, Mapping):
            return None
        try:
            return MonitoringWindow(signal_from_yaml(value),
                                    str(value.get("start", "")),
                                    str(value.get("until", "")))
        except ActionMalformed:
            return None


@dataclass(frozen=True)
class ActionRecord:
    """One human act, recorded as `## Action <at>` plus a fenced yaml block
    plus free text.

    `at` is the record's identity: the heading, the log.md timestamp, and
    what makes a replayed transaction converge. A retry reusing `at` rewrites
    identical bytes; a retry with a fresh `at` is a second record; a
    *different* record reusing `at` is an `ActionCollision`.

    Validation happens once, here (per boundary-discipline). `notes` is
    stripped on construction, so the round trip holds for every constructible
    record rather than only for the tidy ones. `evidence` is parsed the same
    way: the field's type is the constructor's contract, and after
    construction every entry is an `EvidenceRef`, never a string. Raises
    ActionMalformed when: `at` is not a real `YYYY-MM-DDTHH:MM:SSZ` instant;
    `kind` is not in ACTION_KINDS; `actor`, `intent` or `summary` is empty;
    any field except `notes` carries any `str.splitlines` break; `notes`
    carries a break other than `\\n` (CRLF is normalised to `\\n` first, like
    the strip); an `evidence` entry is not
    `digests/<db>/YYYY-MM-DD.md` or `.json`; or `notes` contains a line
    starting with `## ` (splits the section), a line whose `lstrip()` starts
    with ``` (an indented fence still closes the block), or a run of three
    newlines (`day_block_replace` collapses those page-wide on a later
    ingest, silently rewriting the record).

    `error_pages` is a resolve's `update_error_pages` choice (issue 14):
    written as `error_pages: false` only when the operator declined the
    `## Resolution history` rows, so `backfill-resolutions` can honour the
    same choice later, and every record written before it existed reads as
    the default `True`.

    Round-trip invariant: `parse_action(render_action(r)) == r`. Empty
    optionals are omitted from the yaml and come back as their defaults."""

    at: str
    kind: str
    actor: str
    intent: str
    summary: str
    status_after: Status
    ticket: str = ""
    outcome: Outcome = Outcome.PENDING
    rollback: str = ""
    window: MonitoringWindow | None = None
    evidence: tuple[EvidenceRef | str, ...] = ()
    notes: str = ""
    error_pages: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.error_pages, bool):
            raise ActionMalformed(
                f"error_pages: {self.error_pages!r} is not true or false")
        if not self.error_pages and self.kind != "resolve":
            raise ActionMalformed(
                f"error_pages: only a resolve writes error-page rows, so "
                f"only a resolve can decline them, not {self.kind!r}")
        object.__setattr__(self, "notes",
                           self.notes.replace("\r\n", "\n").strip())
        object.__setattr__(self, "evidence", tuple(
            e if isinstance(e, EvidenceRef) else EvidenceRef.parse(e)
            for e in self.evidence))
        _instant("at", self.at)
        if self.kind not in ACTION_KINDS:
            raise ActionMalformed(f"kind: {self.kind!r} is not one of "
                                  + ", ".join(ACTION_KINDS))
        for name in ("actor", "intent", "summary"):
            if not getattr(self, name).strip():
                raise ActionMalformed(f"{name}: must not be empty")
        scalars = [("kind", self.kind), ("actor", self.actor),
                   ("intent", self.intent), ("summary", self.summary),
                   ("status_after", str(self.status_after)),
                   ("ticket", self.ticket), ("outcome", str(self.outcome)),
                   ("rollback", self.rollback)]
        for name, value in scalars:
            _one_line(name, value)
        lines = self.notes.split("\n")
        for line in lines:
            if line.splitlines() != ([line] if line else []):
                raise ActionMalformed(
                    f"notes: {line!r} carries a line break other than "
                    f"\\n, which a page reader would split on")
        for line in lines:
            if line.startswith("## "):
                raise ActionMalformed(
                    f"notes: {line!r} would split the section")
            if line.lstrip().startswith("```"):
                raise ActionMalformed(
                    f"notes: {line!r} would close the yaml fence")
        if "\n\n\n" in self.notes:
            raise ActionMalformed(
                "notes: a blank line pair is collapsed by the next ingest")

    def log_line(self, path: str) -> str:
        """The mandatory `log.md` line, `[<at>] incident — <path>: <kind> by
        <actor> — <summary>`, through `pagetext.oneline`. The `at` in it is
        what makes `log_append` idempotent on a retry."""
        return oneline(f"[{self.at}] incident — {path}: {self.kind} by "
                       f"{self.actor} — {self.summary}")


@dataclass(frozen=True)
class ActionProblem:
    """A section that looks like an action record and is not one. `lint` maps
    these to `action-malformed`; the portal renders the raw section."""

    heading: str
    message: str


@dataclass(frozen=True)
class Actions:
    """Everything `## Action` in one page: what parsed and what did not. Two
    fields rather than a raising parser: the portal wants `records`, lint
    wants `problems`, and a daily render must not fail on one bad block."""

    records: tuple[ActionRecord, ...] = ()
    problems: tuple[ActionProblem, ...] = ()


def render_action(record: ActionRecord) -> str:
    """The exact section text, ending in one newline:

        ## Action 2026-08-30T14:22:10Z

        ```yaml
        kind: resolve
        actor: dba@example.com
        ...
        ```

        free-text notes

    Canonical bytes: field order, `sort_keys=False`, block style, unbounded
    width, empty fields omitted, enums and signals flattened to strings."""
    window = record.window
    values: dict[str, object] = {
        "kind": record.kind,
        "actor": record.actor,
        "intent": record.intent,
        "summary": record.summary,
        "status_after": str(record.status_after),
        "ticket": record.ticket,
        "outcome": str(record.outcome),
        "rollback": record.rollback,
        "window": None if window is None else {
            **signal_to_yaml(window.signal),
            "start": window.start, "until": window.until},
        "evidence": [str(e) for e in record.evidence],
        "error_pages": None,
    }
    fields = {k: values[k] for k in ACTION_FIELDS if values[k]}
    if not record.error_pages:
        fields["error_pages"] = False
    block = yaml.safe_dump(fields,
                           sort_keys=False, default_flow_style=False,
                           width=10**6, allow_unicode=True)
    section = f"## Action {record.at}\n\n```yaml\n{block}```\n"
    if record.notes:
        return section + f"\n{record.notes}\n"
    return section


def parse_action(section: str) -> ActionRecord:
    """One `## Action` section back into a record. Raises ActionMalformed
    naming the offending field. A key outside ACTION_FIELDS is one such
    failure: a typo like `sumary:` must not silently drop a human sentence."""
    lines = section.split("\n")
    head = ACTION_HEAD_RE.match(lines[0])
    if head is None:
        raise ActionMalformed(
            f"at: {lines[0]!r} is not `## Action <ISO timestamp>`")
    at = head.group(1)
    opened = next((i for i in range(1, len(lines))
                   if lines[i].strip() == "```yaml"), None)
    if opened is None:
        raise ActionMalformed(
            f"yaml block: `## Action {at}` has no ```yaml fence")
    closed = next((i for i in range(opened + 1, len(lines))
                   if lines[i].strip() == "```"), None)
    if closed is None:
        raise ActionMalformed(
            f"yaml block: the ```yaml fence under `## Action {at}` is "
            f"never closed")
    try:
        data = yaml.safe_load("\n".join(lines[opened + 1:closed]))
    except Exception as exc:  # noqa: BLE001 - YAML raises more than YAMLError
        # An unquoted `2026-02-30` is valid YAML syntax that the timestamp
        # constructor then refuses with a bare ValueError.
        raise ActionMalformed("yaml block: not valid yaml: "
                              + (str(exc).splitlines() or [""])[0]) from exc
    if not isinstance(data, Mapping):
        raise ActionMalformed("yaml block: not a mapping")
    for key in data:
        if key not in ACTION_FIELDS:
            raise ActionMalformed(f"{key}: is not an action field; expected "
                                  + ", ".join(ACTION_FIELDS))
    status = parse_status(data.get("status_after"))
    if status is None:
        raise ActionMalformed(
            f"status_after: {data.get('status_after')!r} is not one of "
            + ", ".join(str(s) for s in Status))
    raw_outcome = data.get("outcome")
    try:
        outcome = (Outcome.PENDING if raw_outcome is None
                   else Outcome(str(raw_outcome)))
    except ValueError as exc:
        raise ActionMalformed(f"outcome: {raw_outcome!r} is not one of "
                              + ", ".join(str(o) for o in Outcome)) from exc
    window = None
    if "window" in data:
        raw_window = data["window"]
        if not isinstance(raw_window, Mapping):
            raise ActionMalformed(f"window: {raw_window!r} is not a mapping")
        window = MonitoringWindow(signal_from_yaml(raw_window),
                                  str(raw_window.get("start", "")),
                                  str(raw_window.get("until", "")))
    raw_evidence = data.get("evidence", [])
    if not isinstance(raw_evidence, list):
        raise ActionMalformed(f"evidence: {raw_evidence!r} is not a list")
    error_pages = data.get("error_pages", True)
    if not isinstance(error_pages, bool):
        raise ActionMalformed(
            f"error_pages: {error_pages!r} is not true or false")

    def text(key: str) -> str:
        value = data.get(key)
        return "" if value is None else str(value)

    return ActionRecord(
        at=at, kind=text("kind"), actor=text("actor"), intent=text("intent"),
        summary=text("summary"), status_after=status, ticket=text("ticket"),
        outcome=outcome, rollback=text("rollback"), window=window,
        evidence=tuple(str(e) for e in raw_evidence),
        notes="\n".join(lines[closed + 1:]).strip(),
        error_pages=error_pages)


def parse_actions(text: str) -> Actions:
    """Every `## Action` section of a page, in page order, which is
    chronological by construction: `append_action` appends at the end."""
    records, problems = [], []
    for heading, body in sections(text):
        if not ACTION_HEAD_RE.match(heading):
            continue
        try:
            records.append(parse_action(heading + "\n" + body))
        except ActionMalformed as exc:
            problems.append(ActionProblem(heading, str(exc)))
    return Actions(tuple(records), tuple(problems))


def append_action(text: str, record: ActionRecord) -> str:
    """Write the rendered record as the page's `## Action <record.at>`
    section: replace the section with that exact heading, else append at the
    end. Twice: identical bytes.

    Replacing is for convergence, never for overwriting another act: a
    section at the same `at` that parses as a record *different* from
    `record` raises `ActionCollision` (issue 13: two acts in one second kept
    only the second). A section that does not parse is still replaced, which
    is how a hand-spaced or half-written heading is repaired.

    The scan is here rather than through `pagetext.day_block_replace`, which
    collapses `\\n{3,}` page-wide on its match path: replaying a record onto a
    page whose Evidence holds a fenced excerpt with a blank-line pair rewrote
    the excerpt. `ActionRecord` can police its own notes; it cannot police
    bytes a human wrote elsewhere on the page. Every byte outside the
    record's own section survives."""
    head = re.compile(rf"\A##\s+Action\s+{re.escape(record.at)}\s*\Z")
    # `\n` only: `splitlines` would also split on `\f`, U+2028 or a CRLF's
    # `\r` elsewhere on the page and the join would rewrite them as `\n`.
    lines = text.rstrip("\n").split("\n")
    section = render_action(record).strip("\n").split("\n")
    heads = [i for i, ln in enumerate(lines)
             if ln.startswith("## ") and head.match(ln)]
    if not heads:
        return nl("\n".join(lines + [""] + section))
    spans = [(i, next((j for j in range(i + 1, len(lines))
                       if lines[j].startswith("## ")), len(lines)))
             for i in heads]
    for start, end in spans:
        try:
            held = parse_action("\n".join(lines[start:end]))
        except ActionMalformed:
            continue
        if held != record:
            raise ActionCollision(
                f"at: {record.at} already records a different action "
                f"({held.kind} by {held.actor}: {held.summary!r}); an act "
                f"needs an `at` of its own, so retry at a later second")
    dropped = {j for start, end in spans for j in range(start, end)}
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == spans[0][0]:
            while out and not out[-1].strip():
                out.pop()
            out += ([""] if out else []) + section + [""]
        if i not in dropped:
            out.append(line)
    return nl("\n".join(out))


def set_status(text: str, status: Status, *, updated: str,
               monitoring: MonitoringWindow | None = None) -> str:
    """Rewrite the incident frontmatter for a status change in one pass:
    `status:`, `updated:`, and `monitoring:` (set when a window is given,
    deleted when not, so a resolved incident cannot keep a stale window).
    The only frontmatter mutator an incident writer may use."""
    return set_frontmatter(text, {
        "status": str(status), "updated": updated,
        "monitoring": monitoring.to_frontmatter() if monitoring else None})


@dataclass(frozen=True)
class Incident:
    """One incident page, parsed. The single reader for every consumer.

    Flat on purpose: four call sites want only path, db, title and status;
    the place that must not confuse states is `lifecycle.TRANSITIONS`, keyed
    by `Status` already.

    Invariant: `unknown_status is not None` implies `status is Status.OPEN`.
    That is the fail-loud reading policy made visible."""

    path: str
    db: str
    title: str
    status: Status
    opened: str = ""
    updated: str = ""
    monitoring: MonitoringWindow | None = None
    error_codes: tuple[str, ...] = ()
    unknown_status: str | None = None
    actions: Actions = field(default_factory=Actions)

    @property
    def slug(self) -> str:
        """`incidents/2026-08-05-cdb1-x.md` -> `2026-08-05-cdb1-x`; the id the
        CLI and the API address an incident by."""
        return Path(self.path).stem

    @property
    def is_active(self) -> bool:
        return is_active(self.status)

    @property
    def superseded_by(self) -> str | None:
        """The page a merge folded this one into (`incidents/<kept>.md`), or
        None: never merged, or reopened since. Merge lineage for readers that
        must follow an incident across pages (`past_fixes`).

        Read off the action log rather than a frontmatter key, because the
        `merge` record already carries it (`duplicate of <kept>`, written by
        `lifecycle` and nothing else) and so does every page merged before
        this property existed; a second copy in frontmatter could only
        disagree with it. A later `reopen` undoes the merge."""
        kept = None
        for record in self.actions.records:
            if record.kind == "merge":
                kept = record.summary.removeprefix(MERGE_SUMMARY).strip()
            elif record.kind == "reopen":
                kept = None
        return incident_path(kept) if kept else None

    def existed_on(self, day: str) -> bool:
        """Whether this incident already existed on `day` (YYYY-MM-DD). No
        readable `opened` counts as existing, so a backfilled daily page shows
        it rather than hiding it."""
        opened = self.opened[:10]
        return not (DAY_RE.match(opened) and opened > day)


def _iso(value: object) -> str:
    """A frontmatter date as the ISO Z string that was written. An unquoted
    `2026-08-30T14:35:00Z` comes back from yaml as a `datetime`; the page
    said a string and readers compare strings."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value or "")


def read_incident(text: str, path: str) -> Incident:
    """Parse one page. Pure: the disk walker and `lifecycle.build` both come
    through here, so a page means the same thing whichever read it."""
    fm = frontmatter(text)
    parsed = parse_status(fm.get("status"))
    unknown = None
    if parsed is None:
        unknown = str(fm["status"]) if "status" in fm else ""
    fm_end = m.end() if (m := FM_RE.match(text)) else 0
    heading = next((ln[2:].strip() for ln in text[fm_end:].splitlines()
                    if ln.startswith("# ")), Path(path).stem)
    return Incident(
        path=path,
        db=str(fm.get("db") or ""),
        title=str(fm["title"]) if fm.get("title") else heading,
        status=parsed if parsed is not None else Status.OPEN,
        opened=_iso(fm.get("opened")),
        updated=_iso(fm.get("updated")),
        monitoring=MonitoringWindow.from_frontmatter(fm.get("monitoring")),
        error_codes=tuple(dict.fromkeys(ERROR_LINK_RE.findall(text))),
        unknown_status=unknown,
        actions=parse_actions(text),
    )


def incident_path(slug: str) -> str:
    """`incidents/<name>.md` from a bare slug, a wiki-relative page path, or
    an absolute one. The file name is the incident's identity, so an operator
    may paste whatever their shell completed.

    Taking only `Path(slug).name` is the traversal boundary a URL segment
    leans on as well as a shell argument: `../../etc/passwd` collapses to
    `incidents/passwd.md`, which simply does not exist. It belongs to the
    module that owns what an incident is, not to either of its two front
    ends."""
    name = Path(slug).name
    return f"{INCIDENT_DIR}/{name.removesuffix('.md')}.md"


def load_incidents_from(read: Callable[[str], str | None],
                        paths: Sequence[str]) -> list[Incident]:
    """Every incident page `paths` names that `read` can supply, in the order
    given.

    The reader-and-paths shape is structurally what `transaction.Tree`
    offers, without the `incidents -> transaction` import Stage 0 rejected,
    so the portal can read the queue at a revision through the same three
    rules the disk walker applies: skip a page whose frontmatter is empty,
    skip one whose `type` is not `incident`, keep the order given.

    A path `read` answers None for is skipped rather than raising: it is
    absent at that revision, or is a file the disk walker cannot open, and
    one such page under `incidents/` must not empty the whole queue."""
    out = []
    for path in paths:
        text = read(path)
        if text is None:
            continue
        fm = frontmatter(text)
        if not fm or str(fm.get("type")) != "incident":
            continue
        out.append(read_incident(text, path))
    return out


def load_incidents(wiki_root: Path) -> list[Incident]:
    """Every `incidents/*.md`, sorted by path.

    Path order is load-bearing: `structured` numbers this list in the report
    prompt and `apply_report` resolves the model's numbers positionally. Pages
    that fail to parse as a page are skipped; a bad status is not a parse
    failure (see `unknown_status`).

    The reader decodes with `errors="surrogateescape"`, which is what
    `transaction._read` does for the same reason: one byte that is not UTF-8
    leaves that page in the queue under its own number instead of raising and
    emptying it. A file that cannot be opened at all reads as absent, so the
    queue survives that too."""
    root = Path(wiki_root)

    def read(path: str) -> str | None:
        try:
            return (root / path).read_bytes().decode(
                errors="surrogateescape")
        except OSError:
            return None

    return load_incidents_from(
        read, sorted(p.relative_to(root).as_posix()
                     for p in (root / INCIDENT_DIR).glob("*.md")))


def active(incidents: Iterable[Incident]) -> list[Incident]:
    """Filter to the attention queue, order preserved."""
    return [i for i in incidents if i.is_active]


def by_error_code(incidents: Iterable[Incident]) -> dict[str, list[Incident]]:
    """Index for "which incidents mention ORA-600", so `research_offload`
    walks the wiki once instead of once per code. A derived view."""
    index: dict[str, list[Incident]] = {}
    for inc in incidents:
        for code in inc.error_codes:
            index.setdefault(code, []).append(inc)
    return index


#: A wikilink, a markdown link `[text](url)` or a backtick code span. All are
#: single-line constructs, so masking per line is the whole of it. A code
#: inside a markdown link's text or url is the author's own link already, and
#: a wikilink spliced into it breaks both.
LINK_OR_CODE_RE = re.compile(
    r"\[\[[^\]]*\]\]|\[[^\]\n]*\]\([^)\n]*\)|`[^`]*`")


def page_code(code: str) -> str:
    """The page spelling of an error code as `ORA_CODE_RE` matched it:
    `ORA-00600` and `ORA-600` both name `errors/ORA-600.md`, because the
    ingest writer strips the zero padding before it names a page
    (`patterns.extract_codes`) and the page it created carries the stripped
    spelling in its path."""
    prefix, _, number = code.partition("-")
    return f"{prefix}-{int(number)}"


def link_codes(text: str, exists: Callable[[str], bool]) -> str:
    """Turn the first bare mention of each error code in a page body into a
    `[[errors/<CODE>]]` wikilink, so `Incident.error_codes` finally answers
    for pages the structured writer wrote in plain prose.

    `exists` decides whether a code has a page. A code with none is left as
    text: `lint`'s `wikilink-broken` rule blocks the commit on a dead link,
    so linking a code the wiki cannot show would trade an invisible fact for
    an unpublishable page.

    What is never rewritten, and why each: the frontmatter (a link there is
    not a link); anything already inside `[[...]]` or a backtick span (the
    page already said what it meant); a fenced block (the same reason, over
    more lines); a table row (an occurrence table is a record, and a cell
    that grew a link would change the bytes a machine writer converges on);
    a heading (`read_incident` reads the `# ` line as the incident's title
    and `readmodel.PageInfo` reads the `## ` lines as its headings, so a
    wikilink there would leak page syntax into every title the portal and the
    CLI print); an `evidence:` line or a `## Evidence` bullet (evidence
    cites a digest, never a conclusion the wiki already drew — `EvidenceRef`
    refuses an `errors/` path for that reason); and a mention preceded by
    `errors/`, which is a flattened wikilink `structured._prose` already
    reduced to its label.

    Idempotent: a code the body already links is left alone wherever else it
    appears, so the second pass over its own output changes nothing."""
    match = FM_RE.match(text)
    cut = match.end() if match else 0
    head, body = text[:cut], text[cut:]
    linked = set(ERROR_LINK_RE.findall(body))
    section = ""
    fenced = False
    out: list[str] = []
    for line in body.split("\n"):
        bare = line.lstrip()
        if bare.startswith("```"):
            fenced = not fenced
        elif line.startswith("## "):
            section = line[3:].strip().casefold()
        skip = (fenced or bare.startswith("```") or bare.startswith("|")
                or line.startswith("#")
                or bare.casefold().startswith("evidence:")
                or (section == "evidence" and bare.startswith("- ")))
        out.append(line if skip else _link_line(line, exists, linked))
    return head + "\n".join(out)


def _link_line(line: str, exists: Callable[[str], bool],
               linked: set[str]) -> str:
    """One line's first-mention rewrites. `linked` is read and extended, so
    the caller's "already spoken for" set spans the whole body."""
    masked = [m.span() for m in LINK_OR_CODE_RE.finditer(line)]
    out: list[str] = []
    at = 0
    for m in ORA_CODE_RE.finditer(line):
        if any(start <= m.start() < end for start, end in masked):
            continue
        if line[:m.start()].endswith("errors/"):
            continue
        code = page_code(m.group(0))
        if code in linked or not exists(f"errors/{code}.md"):
            continue
        linked.add(code)
        out.append(line[at:m.start()])
        out.append(f"[[errors/{code}]]")
        at = m.end()
    out.append(line[at:])
    return "".join(out)
