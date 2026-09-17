"""The JSON dialect of the workbench. Every key name the browser sees is
spelled in this module and nowhere else on the server; `api.py` never writes
a dict literal with a wire key in it.

Encoding is domain value -> dict, so no transport type escapes upward into
`api.py`'s arguments except `WriteRequest`, which is the parsed request.
Decoding is bytes -> `WriteRequest`; a shape error is `BadRequest` (400),
which the server maps before anything domain-shaped runs.

Nothing here reads a file, runs git, or knows a rule. Pure functions over
frozen values, testable with `Tree.of` fixtures and no server.
"""

import json
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from .. import (advisory, alerts, events, harness, incident_action, incidents,
                review)
from ..deeplink import DeepLink
from ..heat import Heat
from ..incident_action import Action, Field, Presentation
from ..incidents import ActionProblem, ActionRecord, Incident, MonitoringWindow
from ..lint import Finding
from ..links import Link, LinkBoard, Section
from ..markdown import Heading, Rendered
from ..monitoring import ClosureCase
from ..readmodel import (Hit, JournalEntry, Origin, Research, Resolution,
                         Snapshot)
from ..transaction import (BaseMoved, Commit, Committed, LintBlocked,
                           NothingToDo, Preview, Proposal, TreeDirty)
from .identity import Principal, Role

SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")
ISO_Z_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

#: The `pid` out of `lock._holder`'s sentence. Matched as text rather than
#: imported, because the rewrite below is cosmetic and a message that changes
#: shape must degrade to "shown verbatim", never to an exception.
HOLDER_PID_RE = re.compile(r"\(pid (\d+),")

#: What a portal-versus-portal lock collision says instead of naming its own
#: pid back at the operator.
SELF_HOLDER = "another workbench request is publishing"


def _self_held(message: str) -> bool:
    pid = HOLDER_PID_RE.search(message)
    return pid is not None and int(pid.group(1)) == os.getpid()

WRITE_KEYS = ("verb", "fields", "base", "at")

TOOL_KEYS = ("tool", "at", "question")

#: The longest question an advisory body may carry. A boundary cap and not a
#: prompt budget: the row's own `max_answer_chars` is what shapes the answer,
#: and this is only the length past which a body stops being a question.
MAX_QUESTION = 2000

INBOX_KEYS = ("fingerprint", "action", "days", "at")

#: What an inbox POST may ask for. One route dispatching on a body field, the
#: `advisory_start` precedent, because both acts write the same file about the
#: same finding and a second route would be a second spelling of one thing.
ACTIONS = ("acknowledge", "suppress")

#: field annotation -> the transport shape the page must send. Keyed by the
#: annotation `incident_action._coerce` dispatches on, so a control cannot be
#: drawn expecting one shape and coerced expecting another. A table rather
#: than a branch chain because both compound keys are hashable and compare
#: equal across writings: `tuple[str, ...]` builds a fresh alias each time,
#: and the `RecoverySignal` union normalises to the same value.
TRANSPORT_OF: dict[object, str] = {
    bool: "boolean",
    tuple[str, ...]: "list",
    incidents.RecoverySignal: "object",
}


class BadRequest(ValueError):
    """The body is not the shape the endpoint takes. Carries `error` (stable:
    `not_json`, `not_object`, `bad_verb`, `bad_fields`, `bad_base`, `bad_at`,
    `bad_tool`, `bad_question`, `bad_fingerprint`, `bad_action`, `bad_days`,
    `unknown_key`, `identity_is_server_side`) for the JSON error body."""

    def __init__(self, error: str, message: str):
        self.error = error
        super().__init__(message)


def encode(value: object) -> bytes:
    """`json.dumps(value, sort_keys=True, ensure_ascii=False)` as UTF-8.
    Sorted keys, so two renders of one response are byte-identical: the rule
    `daily_html` already keeps for pages."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode()


def decode_object(raw: bytes) -> dict:
    """A JSON object from a request body, or BadRequest (`not_json`,
    `not_object`). An empty body is BadRequest, not `{}`."""
    try:
        value = json.loads(raw)
    except ValueError:
        raise BadRequest("not_json", "the body is not JSON") from None
    if not isinstance(value, dict):
        raise BadRequest(
            "not_object",
            f"the body is a JSON {type(value).__name__}, not an object")
    return value


@dataclass(frozen=True)
class WriteRequest:
    """A preview or commit body, shape-checked and nothing more.

    `verb` and `fields` are exactly the two operator-supplied arguments of
    `incident_action.decode`; the decode itself happens in `api.py`, so a
    domain refusal (unknown verb, bad signal) is a 422 and not a 400. `base`
    and `at` are optional for a preview, where the server mints them, and
    mandatory for a commit: the published bytes must be the previewed bytes.

    Invariant: the body has no `actor` key. Identity is the provider's, and a
    body that asserts one is refused with `identity_is_server_side`, so a
    Stage 4 client cannot even try."""

    verb: str
    fields: Mapping[str, object]
    base: str | None
    at: str | None

    @classmethod
    def from_json(cls, body: Mapping[str, object]) -> "WriteRequest":
        """Top-level keys allowed: `verb` (a string), `fields` (an object),
        `base` (40 lowercase hex), `at` (ISO Z). Anything else is
        `unknown_key`.

        `actor` is checked before the unknown-key sweep, so a client that
        asserts an identity is told that identity is server-side rather than
        that it misspelled a field. Neither `base` nor `at` is required here:
        a preview mints both, and `api.commit` is what insists on the echo."""
        if "actor" in body:
            raise BadRequest("identity_is_server_side",
                             "actor: identity is the server's, never the "
                             "body's")
        for key in body:
            if key not in WRITE_KEYS:
                raise BadRequest("unknown_key",
                                 f"unknown key {key!r}; a write body carries "
                                 + ", ".join(WRITE_KEYS))
        verb = body.get("verb")
        if not isinstance(verb, str):
            raise BadRequest("bad_verb", "verb: expected a string")
        fields = body.get("fields", {})
        if not isinstance(fields, dict):
            raise BadRequest("bad_fields", "fields: expected an object of "
                                           "named values")
        base = body.get("base")
        if base is not None and not (isinstance(base, str)
                                     and SHA_RE.match(base)):
            raise BadRequest("bad_base",
                             f"base: {base!r} is not a full 40-hex wiki sha")
        at = body.get("at")
        if at is not None and not (isinstance(at, str) and ISO_Z_RE.match(at)):
            raise BadRequest("bad_at",
                             f"at: {at!r} is not YYYY-MM-DDTHH:MM:SSZ")
        return cls(verb=verb, fields=fields, base=base, at=at)


@dataclass(frozen=True)
class ToolRequest:
    """An advisory start body: which tool, the caller's `at`, and the
    operator's question.

    `at` is the click's identity, not a timestamp the server may improve on:
    `advisory.run_id_for` hashes it, so a page that re-posts after a network
    failure converges on the run it already started instead of paying twice.
    It is optional, and `api.advisory_start` mints one when it is absent, the
    rule a preview already keeps for its own `at`.

    `question` is free text for the row that takes one and `""` for every
    other row. It is hashed into `run_id` beside `at`, so re-posting one
    question converges and asking a second one at the same `at` is a second
    run. Whether a row may carry it is `advisory.asks`' answer and is checked
    in `api.advisory_start`, not here: this class knows shapes, not rows.

    `WriteRequest`'s invariant holds here too: the body has no `actor` key."""

    tool: str
    at: str | None
    question: str = ""

    @classmethod
    def from_json(cls, body: Mapping[str, object]) -> "ToolRequest":
        """Top-level keys allowed: `tool` (a non-empty string), `at` (ISO Z),
        `question` (a non-empty string of at most `MAX_QUESTION` characters).
        Anything else is `unknown_key`, and `actor` is checked first, for
        `WriteRequest.from_json`'s reason.

        An absent `question` is `""`, which is the shape a row that takes no
        question posts; a present but empty one is `bad_question`, because a
        client that sent the key meant to ask something."""
        if "actor" in body:
            raise BadRequest("identity_is_server_side",
                             "actor: identity is the server's, never the "
                             "body's")
        for key in body:
            if key not in TOOL_KEYS:
                raise BadRequest("unknown_key",
                                 f"unknown key {key!r}; an advisory body "
                                 f"carries " + ", ".join(TOOL_KEYS))
        tool = body.get("tool")
        if not isinstance(tool, str) or not tool:
            raise BadRequest("bad_tool", "tool: expected a tool id")
        at = body.get("at")
        if at is not None and not (isinstance(at, str) and ISO_Z_RE.match(at)):
            raise BadRequest("bad_at",
                             f"at: {at!r} is not YYYY-MM-DDTHH:MM:SSZ")
        if "question" not in body:
            return cls(tool=tool, at=at, question="")
        question = body["question"]
        if not isinstance(question, str) or not question:
            raise BadRequest("bad_question",
                             "question: expected something to ask")
        if len(question) > MAX_QUESTION:
            raise BadRequest("bad_question",
                             f"question: {len(question)} characters is more "
                             f"than the {MAX_QUESTION} a question may be")
        return cls(tool=tool, at=at, question=question)


@dataclass(frozen=True)
class InboxRequest:
    """An acknowledge or a suppress: which finding, which act, and for how
    long.

    `days` belongs to `suppress` alone and is refused on an `acknowledge`
    rather than dropped: a duration the server silently discards reads back to
    the client as a suppression that happened.

    `at` is optional and `api.inbox_act` mints one when it is absent, the rule
    a preview already keeps for its own `at`. It is not a click identity here
    the way `ToolRequest`'s is: `review.acknowledge` and `review.suppress` are
    idempotent over the item itself, so a re-post converges without needing to
    be recognised.

    `WriteRequest`'s invariant holds here too: the body has no `actor` key."""

    fingerprint: str
    action: str
    days: int | None
    at: str | None

    @classmethod
    def from_json(cls, body: Mapping[str, object]) -> "InboxRequest":
        """Top-level keys allowed: `fingerprint` (a non-empty string),
        `action` (one of `ACTIONS`), `days` (a positive int, suppress only),
        `at` (ISO Z). Anything else is `unknown_key`, and `actor` is checked
        first, for `WriteRequest.from_json`'s reason."""
        if "actor" in body:
            raise BadRequest("identity_is_server_side",
                             "actor: identity is the server's, never the "
                             "body's")
        for key in body:
            if key not in INBOX_KEYS:
                raise BadRequest("unknown_key",
                                 f"unknown key {key!r}; an inbox body carries "
                                 + ", ".join(INBOX_KEYS))
        fingerprint = body.get("fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            raise BadRequest("bad_fingerprint",
                             "fingerprint: expected the fingerprint of a "
                             "finding a published review holds")
        action = body.get("action")
        if action not in ACTIONS:
            raise BadRequest("bad_action",
                             f"action: {action!r} is not one of "
                             + ", ".join(ACTIONS))
        days = body.get("days")
        if action == "acknowledge" and days is not None:
            raise BadRequest("bad_days",
                             "days: an acknowledgement has no duration; it "
                             "records that the finding was read")
        if action == "suppress" and (isinstance(days, bool)
                                     or not isinstance(days, int)
                                     or days <= 0):
            raise BadRequest("bad_days",
                             f"days: {days!r} is not a positive number of "
                             f"days to suppress for")
        at = body.get("at")
        if at is not None and not (isinstance(at, str) and ISO_Z_RE.match(at)):
            raise BadRequest("bad_at",
                             f"at: {at!r} is not YYYY-MM-DDTHH:MM:SSZ")
        return cls(fingerprint=fingerprint, action=action, days=days, at=at)


def principal_json(p: Principal) -> dict:
    """`{"email", "name", "source", "roles": [...]}`."""
    return {"email": p.actor.email, "name": p.actor.name,
            "source": p.actor.source,
            "roles": sorted(str(role) for role in p.roles)}


def field_json(verb: str, field: Field, pres: Presentation) -> dict:
    """One form control: `{"name", "type", "widget", "required", "default",
    "help", "choices", "cli_flag"}`.

    `type` is the transport shape, never a python type name: whether the page
    must send `""`, `[]`, `{}` or a bool. It is not redundant with `widget`,
    which says only how to draw the control; `TRANSPORT_OF` is keyed by the
    annotation `incident_action._coerce` dispatches on, so what the page
    posts cannot be a shape the decoder refuses.

    `default` renders through `incident_action.plain`, the JSON shape
    `decode` takes back, so the page pre-fills without knowing a python type;
    a required field has no default and carries None. `cli_flag` is the
    resolved argv spelling, so `update_error_pages` reads `--no-error-pages`
    and the form and the pasteable CLI line name one flag."""
    return {"name": field.name,
            "type": TRANSPORT_OF.get(field.type, "string"),
            "widget": str(pres.widget),
            "required": field.required,
            "default": incident_action.plain(field.default),
            "help": pres.help,
            "choices": list(pres.choices),
            "cli_flag": incident_action.flag_of(verb, field.name)}


def allowed_json(verb: str, role: Role, permitted: bool,
                 fields: tuple[dict, ...]) -> dict:
    """One row of an incident's `allowed`: `{"verb", "permitted", "role",
    "fields"}`. There is no schema endpoint; this is where a form's shape
    comes from, so a verb's fields arrive with the reason the verb is
    offered."""
    return {"verb": verb, "permitted": permitted, "role": str(role),
            "fields": list(fields)}


def window_json(window: MonitoringWindow | None) -> dict | None:
    """`{"kind", <payload key>, "start", "until"}` via
    `incidents.signal_to_yaml`; None stays None. One spelling of a signal on
    the wire, the same one `decode` accepts back."""
    if window is None:
        return None
    return {**incidents.signal_to_yaml(window.signal),
            "start": window.start, "until": window.until}


def action_json(record: ActionRecord) -> dict:
    """Every `ActionRecord` field by its own name; `outcome` and
    `status_after` as their strings, `window` through `window_json`,
    `evidence` as a list of `EvidenceRef.__str__`, which is the bare digest
    path the page itself was written with."""
    return {"at": record.at,
            "kind": record.kind,
            "actor": record.actor,
            "intent": record.intent,
            "summary": record.summary,
            "status_after": str(record.status_after),
            "ticket": record.ticket,
            "outcome": str(record.outcome),
            "rollback": record.rollback,
            "window": window_json(record.window),
            "evidence": [str(ref) for ref in record.evidence],
            "notes": record.notes}


def problem_json(problem: ActionProblem) -> dict:
    """`{"heading", "message"}`."""
    return {"heading": problem.heading, "message": problem.message}


def finding_json(finding: Finding) -> dict:
    """`Finding.to_dict()`, unchanged."""
    return finding.to_dict()


def commit_json(commit: Commit) -> dict:
    """`{"sha", "short", "at", "author", "subject", "actor"}`; an empty
    `actor` is an out-of-band commit, which the page labels as such."""
    return {"sha": commit.sha, "short": commit.short, "at": commit.at,
            "author": commit.author, "subject": commit.subject,
            "actor": commit.actor}


def closure_json(case: ClosureCase | None, facts: Mapping | None) -> dict | None:
    """`{"verdict", "evaluated_at", "source_revision", "stale", "for",
    "against", "digests", "facts"}`; `facts` is the raw file, for the "show
    me the evidence" disclosure. None when there is no facts file.

    The wire key is `for` and the field behind it is `supporting`: the page
    reads a for-and-against pair and `for` is the word beside `against` on
    the screen, while `supporting` is the word that does not collide with
    python's keyword inside `monitoring`."""
    if case is None:
        return None
    return {"verdict": case.verdict,
            "evaluated_at": case.evaluated_at,
            "source_revision": case.source_revision,
            "stale": case.stale,
            "for": list(case.supporting),
            "against": list(case.against),
            "digests": list(case.digests),
            "facts": facts}


def queue_row_json(inc: Incident, *, verdict: str | None, dirty: bool,
                   allowed: tuple[str, ...],
                   last_seen: str | None) -> dict:
    """One queue line: `{"slug", "path", "db", "title", "error_codes",
    "status", "label", "unknown_status", "opened", "updated", "verdict",
    "dirty", "allowed", "last_seen"}`, where `allowed` is verb names only.
    The forms ride on the incident view, not on the queue.

    `error_codes` is here because the queue pivots by error code, and it is
    the key `incident_json` already spells for one page, so both screens read
    one incident's codes under one name.

    `last_seen` is the day this incident's codes were last seen on its
    database, which `readmodel.last_seen` reads off the snapshot, and null
    when no occurrence row joins.

    The day goes on the wire and no `quiet` boolean does, for the reason
    `provenance_json` sends no `stale` one: the page holds today already, so
    it derives the span itself against a threshold it can name, and a server
    that derived a second one could disagree with it."""
    return {"slug": inc.slug,
            "path": inc.path,
            "db": inc.db,
            "title": inc.title,
            "error_codes": list(inc.error_codes),
            "status": str(inc.status),
            "label": inc.status.label,
            "unknown_status": inc.unknown_status,
            "opened": inc.opened,
            "updated": inc.updated,
            "verdict": verdict,
            "dirty": dirty,
            "allowed": list(allowed),
            "last_seen": last_seen}


def queue_json(*, revision: str, strays: tuple[str, ...],
               rows: tuple[dict, ...]) -> dict:
    """The attention queue: `{"revision", "strays", "incidents"}`.

    The list is `strays` and never `dirty`. `dirty` is the per-row boolean
    `queue_row_json` answers about one page; one word for both is what made
    this key move twice between agents."""
    return {"revision": revision, "strays": list(strays),
            "incidents": list(rows)}


def provenance_json(snap: Snapshot | Heat, *, head: str) -> dict:
    """`{"revision", "built_at", "head"}`, merged flat into the top level of
    every revision-backed envelope rather than nested under a name of its own.

    It takes a `Snapshot` or the `Heat` window built beside one, because both
    are pinned to a revision and both carry the built time that pinning
    happened at. One triple for both keeps the board reading a single
    staleness vocabulary whichever model answered.

    The page derives the stale badge as `revision !== head`, which is the
    comparison and the key pair the preview screen already draws, so a second
    staleness vocabulary never enters the dialect. There is deliberately no
    server-computed `stale` boolean: two answers to one question can disagree,
    and the page holds both operands anyway.

    `source_revision` stays the closure's word for the revision a monitoring
    verdict was evaluated at (`closure_json`) and is not reused here."""
    return {"revision": snap.revision, "built_at": snap.built_at, "head": head}


def fleet_row_json(*, db: str, open_: int, monitoring: int, journal_day: str,
                   journal_headline: str, errors_30d: int,
                   page: str) -> dict:
    """One database's fleet line: `{"db", "open", "monitoring",
    "journal_day", "journal_headline", "errors_30d", "page"}`.

    A database with no journal entry sends `""` for both journal keys rather
    than null, because the page renders an absent value as a dash and a null
    would need its own arm to reach the same cell.

    The trailing underscore on `open_` is a parameter name dodging the
    builtin; the key is `open`."""
    return {"db": db,
            "open": open_,
            "monitoring": monitoring,
            "journal_day": journal_day,
            "journal_headline": journal_headline,
            "errors_30d": errors_30d,
            "page": page}


def fleet_json(*, prov: dict, rows: tuple[dict, ...]) -> dict:
    """The fleet view: `{**prov, "dbs"}`, one row per database in the order
    `api.fleet` listed them."""
    return {**prov, "dbs": list(rows)}


def heat_row_json(*, db: str, host: str,
                  counts: Mapping[str, Sequence[int | None]]) -> dict:
    """One database's row of the heat maps: `{"db", "host", "counts"}`.

    `counts` maps message class to one entry per day, aligned with the
    envelope's `days` and in the same order. `null` is on the wire here, which
    `fleet_row_json` refuses for an absent journal, and the difference is the
    point: there `""` and a value are both things the page prints, while here
    a day with no digest and a day whose digest counted zero are two different
    facts drawn two different ways. A zero cell says the database was quiet, a
    null cell says nobody was watching, and `0` cannot carry the second.

    `host` is `""` when the database page does not say, matching
    `fleet_row_json`: the page groups the rows into bands and an unknown host
    is the label of the last band, not a missing field."""
    return {"db": db,
            "host": host,
            "counts": {name: list(values) for name, values in counts.items()}}


def heat_json(*, prov: dict, days: Sequence[str], classes: Sequence[str],
              rows: tuple[dict, ...]) -> dict:
    """The heat maps: `{**prov, "days", "classes", "rows"}`.

    `days` is the shared x axis, oldest first and ending on the server's
    today, spelled once for the whole board rather than per row: every row is
    the same span, and a date repeated per database is a date two rows could
    disagree about.

    `classes` names the maps and their order, so the page draws what the
    server counted instead of holding its own list of message classes that a
    new compactor class would silently leave out of the board."""
    return {**prov,
            "days": list(days),
            "classes": list(classes),
            "rows": list(rows)}


def ownership_json(origin: Origin, commit: Commit | None) -> dict:
    """Who last wrote a page: `{"origin", "commit"}`, `commit` through
    `commit_json` or None.

    Merged flat into whatever row carries it rather than nested under an
    `ownership` key, because `origin` and `commit` are read together as one
    chip and a level between them would buy the page nothing.

    `origin` is `readmodel.origin_of`'s heuristic over the commit's trailer,
    subject and path, never the git author: every commit in the live wiki
    carries one author address."""
    return {"origin": str(origin),
            "commit": commit_json(commit) if commit is not None else None}


def journal_entry_json(entry: JournalEntry) -> dict:
    """One journal section: `{"day", "headline", "path"}`.

    No `db`: every journal list on the wire hangs off an envelope that already
    names one database, and a key repeated per row is a key two answers can
    disagree about."""
    return {"day": entry.day, "headline": entry.headline, "path": entry.path}


def error_row_json(*, code: str, count: int, last_day: str,
                   researched: str, resolved: int) -> dict:
    """One aggregated error line: `{"code", "count", "last_day",
    "researched", "resolved", "page"}`, counted over an error page's
    `## Occurrences` rows for one database.

    A row and not an occurrence: the database view answers "what keeps
    happening here", and the occurrence-level detail belongs to the error
    page itself.

    `page` is derived from the code rather than carried from the snapshot,
    because these rows are aggregated out of that page's own occurrence
    table: a row exists only where the page does. `researched` is the date on
    it or `""`, which is what lets the table say which of the codes a
    database keeps hitting nobody has looked up yet.

    `resolved` is how many `## Resolution history` rows that error page
    carries, across every database and not just this one. A count and not a
    list: the row is one line of a table the operator scans for what to look
    at next, and "somebody has fixed this three times" is the whole of what
    that scan needs. The rows themselves are on the error code's own screen,
    which is one click away. It is fleet-wide because the table is read as
    "is this code understood anywhere", the same question `researched`
    answers about the reference block, and a per-database count would say
    nothing about a fix somebody landed on a neighbour."""
    return {"code": code, "count": count, "last_day": last_day,
            "researched": researched, "resolved": resolved,
            "page": f"errors/{code}.md"}


def research_json(code: str, *, exists: Callable[[str], bool],
                  research: Mapping[str, Research],
                  resolutions: Mapping[str, tuple[Resolution, ...]]) -> dict:
    """One error code as the incident screen reads it: `{"code", "path",
    "exists", "researched", "cause", "action", "citations", "notes",
    "resolutions"}`, the citations a list of `{"source", "url", "accessed"}`
    in the order the page cites them, empty on an unresearched code.

    A row is built for every code, researched or not, and the unresearched
    one carries three empty strings rather than being left out. The gap is
    the finding: an incident whose codes nobody has looked up is exactly what
    the operator needs to see, and a shorter list would hide it.

    `exists` is separate from `research` because they answer different
    questions. A code can have a page and no `## Reference` block; a code
    whose page the wiki does not hold at all cannot be opened, and the screen
    draws that differently.

    `notes` is what practitioners outside Oracle's documentation say about
    the code, one `{"source", "url", "accessed", "text"}` per note in page
    order and an empty list on a code that carries none. Each note names its
    own source rather than sharing the row's `citations`, because a note is a
    claim from one page and a reader who cannot tell which page said it has
    been given a rumour. A note with no citation carries three empty strings,
    the shape `citations` already uses for a source named without a URL.

    `resolutions` is every time somebody closed an incident against this
    code, newest day first, one `{"day", "db", "incident", "path",
    "remediation", "evidence"}` row apiece and an empty list on a code
    nothing has been resolved against. It sits beside the cause and the
    action because it answers the third question the operator asks of a code
    they have just been handed: not only what it means and what to do, but
    what actually worked the last time.

    A row carries `path` beside `incident` because the screen links to the
    case file, and the slug-to-path rule is the server's. A page that
    rebuilt `incidents/<slug>.md` in JavaScript would be a second answer to
    where an incident lives, free to drift from this one."""
    path = f"errors/{code}.md"
    found = research.get(code)
    return {"code": code,
            "path": path,
            "exists": exists(path),
            "researched": found.researched if found else "",
            "cause": found.cause if found else "",
            "action": found.action if found else "",
            "citations": [{"source": c.source, "url": c.url,
                           "accessed": c.accessed}
                          for c in (found.citations if found else ())],
            "notes": [{"source": n.citation.source if n.citation else "",
                       "url": n.citation.url if n.citation else "",
                       "accessed": n.citation.accessed if n.citation else "",
                       "text": n.text}
                      for n in (found.notes if found else ())],
            "resolutions": [{"day": r.day,
                             "db": r.db,
                             "incident": r.incident,
                             "path": f"incidents/{r.incident}.md",
                             "remediation": r.remediation,
                             "evidence": r.evidence}
                            for r in resolutions.get(code, ())]}


def db_incident_row_json(inc: Incident, *, origin: Origin,
                         commit: Commit | None) -> dict:
    """One incident on the database view: `queue_row_json`'s keys without
    `verdict`, `dirty` and `allowed`, plus `ownership_json` merged flat.

    Those three are missing on purpose. They are working-tree and
    live-monitoring facts, and this row is read out of a committed revision:
    answering `dirty: false` here would be a claim about a checkout nobody
    looked at."""
    return {"slug": inc.slug,
            "path": inc.path,
            "db": inc.db,
            "title": inc.title,
            "status": str(inc.status),
            "label": inc.status.label,
            "unknown_status": inc.unknown_status,
            "opened": inc.opened,
            "updated": inc.updated,
            **ownership_json(origin, commit)}


def heading_json(h: Heading) -> dict:
    """One heading of a rendered body: `{"level", "text", "slug"}`.

    `slug` is already namespaced by the render's `heading_prefix`, so it is
    the id the body carries and the outline links to. The page never rebuilds
    it: a second slug rule in JavaScript is a second answer to "where does
    this heading live"."""
    return {"level": h.level, "text": h.text, "slug": h.slug}


def page_body_json(*, path: str, type_: str, title: str, rendered: Rendered,
                   origin: Origin, commit: Commit | None,
                   backlinks: tuple[str, ...]) -> dict:
    """One rendered page: `{"path", "type", "title", "html", "headings",
    **ownership_json, "backlinks"}`.

    `html` is the server-rendered body. The page inserts it as markup, and
    what makes that safe is the renderer plus the header, not the page:
    `dbwiki.markdown` escapes every text run, attribute-escapes and
    scheme-allowlists every href and passes no raw HTML through, and
    `script-src` is a sha256 of the page's own block, so markup that got past
    the renderer still could not run script.

    `backlinks` is bare paths, not rows: the page renders each as an internal
    address and has nothing else to say about one it did not already fetch.

    The trailing underscore on `type_` is a parameter name dodging the
    builtin; the key is `type`, the `open_` precedent."""
    return {"path": path,
            "type": type_,
            "title": title,
            "html": rendered.html,
            "headings": [heading_json(h) for h in rendered.headings],
            **ownership_json(origin, commit),
            "backlinks": list(backlinks)}


def link_json(link: DeepLink) -> dict:
    """One deep link: `{"state", "url", "label", "description", "note"}`.

    `state` is the plain word, the `status` precedent, and `url` is null in
    every state but `available`, so the page cannot render a dead anchor by
    forgetting to look. `note` is why it is not available and is empty when
    there is nothing to say."""
    return {"state": str(link.state), "url": link.url, "label": link.label,
            "description": link.description, "note": link.note}


def page_json(*, prov: dict, body: dict, links: tuple[dict, ...]) -> dict:
    """One page's whole view: `{**prov, **page_body_json(...), "links"}`.

    Flat, like every other snapshot-backed envelope: the body's keys are the
    ones the screen reads directly, and the database view carries the same
    `page_body_json` dict nested under its own `page` key, so one shape is
    drawn by one painter on both screens.

    `links` is `link_json` per deep link the page's `evidence_ref` blocks
    resolve to, in block order, the filter ahead of its document. Every state
    is listed and not only the available ones: the page draws a refusal and
    its reason, and a link silently dropped would read as a page that cites
    nothing."""
    return {**prov, **body, "links": list(links)}


def db_json(*, prov: dict, db: str, page: dict | None,
            incidents: tuple[dict, ...], errors: tuple[dict, ...],
            journal: tuple[dict, ...]) -> dict:
    """One database's whole view: `{**prov, "db", "page", "incidents",
    "errors", "journal"}`, each list in the order `api.database` built it.

    `page` is `page_body_json` for the standing `databases/<db>.md`, rendered
    body included, or None when the snapshot holds no readable page for the
    database."""
    return {**prov, "db": db, "page": page, "incidents": list(incidents),
            "errors": list(errors), "journal": list(journal)}


def hit_json(hit: Hit) -> dict:
    """One search result: `{"path", "type", "title", "db", "snippet"}`.

    No score and no rank number. The order of the list is the ranking, and a
    number beside each row would invite the page to sort by it and disagree
    with the model that produced it."""
    return {"path": hit.path, "type": hit.type, "title": hit.title,
            "db": hit.db, "snippet": hit.snippet}


def search_json(*, prov: dict, query: str, hits: tuple[dict, ...]) -> dict:
    """The search view: `{**prov, "query", "hits"}`. `query` echoes what was
    searched, so a page reloaded at its own address can put the term back in
    the box without parsing it out of a URL twice."""
    return {**prov, "query": query, "hits": list(hits)}


def board_link_json(link: Link) -> dict:
    """One row of the operator's link board: `{"name", "what", "url", "tag"}`.

    `what` is a string and never absent, so the page asks whether it is empty
    rather than whether the key is there. `tag` crosses as its word because
    `links.Tag` is a `StrEnum` and the page draws a chip per tag: a number here
    would make the page keep its own table of what the number meant.

    Spelled `board_link_json` and not `link_json` because `link_json` is
    already the deep-link encoder, and both would otherwise be "the link one".
    The board's own encoder is `links_json`, so the word order says which of
    the two any call is: this one takes a link, that one takes a board."""
    return {"name": link.name, "what": link.what,
            "url": link.url, "tag": link.tag.value}


def link_section_json(section: Section) -> dict:
    """One group of links: `{"title", "note", "links"}`. `note` is empty rather
    than absent for the same reason `what` is."""
    return {"title": section.title, "note": section.note,
            "links": [board_link_json(link) for link in section.links]}


def links_json(board: LinkBoard) -> dict:
    """The link board: `{"title", "intro", "sections"}`.

    No `revision` and no `generated_at`. The board is a config file the
    operator wrote, not a view of the wiki or of `.state/`, so there is no
    revision it was read at and nothing here goes stale between requests.

    An empty `sections` is the whole signal for "no board configured". The
    server does not send a flag beside it, because two spellings of the same
    fact are two things the page can be caught disagreeing about."""
    return {"title": board.title, "intro": board.intro,
            "sections": [link_section_json(s) for s in board.sections]}


def coverage_json(c: events.Coverage) -> dict:
    """How much history one `.state/` log still holds: `{"name", "lines",
    "cap", "oldest", "newest", "truncated"}`.

    `truncated` crosses the wire because the logs drop their oldest lines at
    the cap, so a full file means history is already gone rather than about to
    be, and a run list presented without that is presented as the whole
    record."""
    return {"name": c.name,
            "lines": c.lines,
            "cap": c.cap,
            "oldest": c.oldest,
            "newest": c.newest,
            "truncated": c.truncated}


def freshness_json(*, task: str, at: str, age_h: float | None,
                   threshold_h: float, stale: bool) -> dict:
    """One staleness row: `{"task", "at", "age_h", "threshold_h", "stale"}`.

    A task nothing in coverage proves ran carries `at: ""` and `age_h: null`,
    and `stale` is then `False`: that is `health._stage_staleness`'s own rule,
    which omits a stage that never succeeded, because "never ran" on a fresh
    install or a pipeline that never enabled research is not a regression.

    `threshold_h` rides beside `stale` so the page can say what budget the
    answer was measured against instead of restating one of its own."""
    return {"task": task,
            "at": at,
            "age_h": age_h,
            "threshold_h": threshold_h,
            "stale": stale}


def usage_json(u: events.Usage | None) -> dict | None:
    """One agent stage's spend: `{"input_tokens", "output_tokens",
    "cost_usd", "known", "cost_known"}`, or the literal None when the line
    recorded no usage at all.

    `known` is on the wire for `advisory_usage_json`'s reason: a cost nobody
    measured is never rendered as 0, and a page that summed an unknown-bearing
    set without reading this flag would report a number no adapter gave.
    `cost_known` is the same guarantee for the price, which `known` does not
    cover: it answers for the token counts alone (`events.Usage`)."""
    if u is None:
        return None
    return {"input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cost_usd": u.cost_usd,
            "known": u.known,
            "cost_known": u.cost_known}


def reason_json(*, code: str, evidence: Mapping[str, object]) -> dict:
    """One reason: `{"code", "evidence"}`.

    Both a run's reasons and a review finding's, because the shape is the
    same one — a code and the named scalars behind it — and a review-specific
    sibling would be two spellings of one row. `evidence` is what
    `events._evidence` kept for a run and what `_evidence` below kept for a
    review, and is `{}` wherever the join that would carry it is not
    honest."""
    return {"code": code, "evidence": dict(evidence)}


def stage_json(s: events.Stage) -> dict:
    """One row of a run's timeline: `{"name", "status", "started",
    "finished", "duration_s", "agentic", "detail"}`.

    `detail`'s keys are `events`', which names them — `reason`, `inferred`,
    the per-stage counts and paths — so nothing here re-spells them. The page
    reads `inferred`, which says the status was derived from the shape of the
    children rather than read off a recorded fact, and renders the rest as
    text."""
    return {"name": s.name,
            "status": s.status,
            "started": s.started,
            "finished": s.finished,
            "duration_s": s.duration_s,
            "agentic": s.agentic,
            "detail": dict(s.detail)}


def run_head_json(*, run_id: str, command: str, started: str, finished: str,
                  outcome: str, error_category: str, dbs_ingested: int,
                  dbs_skipped: int) -> dict:
    """The identity and counts of one run: `{"run_id", "command", "started",
    "finished", "outcome", "error_category", "dbs_ingested", "dbs_skipped"}`.

    Spelled once and merged into both envelopes, so the row an operator picks
    off the list and the header of the run they open cannot disagree.

    `dbs_ingested` counts DB_RUN children whose `outcome` is `ingested`;
    `dbs_skipped` counts those whose `decision` is `skip` (a deliberate skip,
    not an unchanged window)."""
    return {"run_id": run_id,
            "command": command,
            "started": started,
            "finished": finished,
            "outcome": outcome,
            "error_category": error_category,
            "dbs_ingested": dbs_ingested,
            "dbs_skipped": dbs_skipped}


def run_row_json(*, head: dict, stages: tuple[dict, ...]) -> dict:
    """One line of the run list: `{**run_head_json(...), "stages"}`. The
    timeline rides on the row because it is always seven stages
    (`events.STAGE_NAMES`) and the list draws them as the row's own shape,
    not as a second request per run."""
    return {**head, "stages": list(stages)}


def totals_json(t: events.Totals) -> dict:
    """What one set of stages measured: `{"stages", "rolled_back", "tokens",
    "token_stages", "seconds", "timed_stages", "cost_usd", "priced_stages",
    "unpriced_stages"}`.

    Nine keys and no derived one. Every sum crosses beside the count of lines
    that reported it, so the page renders "0.00 over 84 priced of 89" and
    cannot render a bare total. There is no `known` flag: `Usage` needs one
    because a line is one measurement, and a set is a fraction."""
    return {"stages": t.stages,
            "rolled_back": t.rolled_back,
            "tokens": t.tokens,
            "token_stages": t.token_stages,
            "seconds": t.seconds,
            "timed_stages": t.timed_stages,
            "cost_usd": t.cost_usd,
            "priced_stages": t.priced_stages,
            "unpriced_stages": t.unpriced_stages}


def day_json(d: events.Day) -> dict:
    """One UTC calendar day of loop work: `{"day", "runs", "failed",
    "totals"}`.

    A day nothing ran on crosses as a row of zeros rather than as an absence,
    because `events.days` draws the gap and a list that skipped it would read
    as a denser loop than there was."""
    return {"day": d.day, "runs": d.runs, "failed": d.failed,
            "totals": totals_json(d.totals)}


def open_alert_json(a: alerts.OpenAlert | None) -> dict | None:
    """What is open about one failure group: `{"first_seen", "last_seen",
    "count"}`, or the literal None.

    `usage_json`'s shape rule: absence is None and never a row of zeros —
    here because None means "no alert is open under this fingerprint", which
    a zero count would read as "it recovered".

    No `fingerprint`: the group that carries this already spells it, and two
    spellings of one identity could disagree. `count` is assessments that saw
    the finding, which is `alerts.OpenAlert`'s name for it and not the group's
    own `count`."""
    if a is None:
        return None
    return {"first_seen": a.first_seen, "last_seen": a.last_seen,
            "count": a.count}


def failure_group_json(g: alerts.Recurrence) -> dict:
    """One failure that kept coming back: `{"fingerprint", "category", "db",
    "count", "days", "first_seen", "last_seen", "commands", "sample_error",
    "alert"}`.

    `count` is failures the logs still hold and `alert.count` is assessments
    that saw the finding; both cross so neither has to stand for the other.
    `sample_error` is the newest recorded message, verbatim, on the terms
    `db_run_json` already carries that field on: drawn as text, never parsed
    and never a key."""
    return {"fingerprint": g.fingerprint,
            "category": g.category,
            "db": g.db,
            "count": g.count,
            "days": g.days,
            "first_seen": g.first_seen,
            "last_seen": g.last_seen,
            "commands": list(g.commands),
            "sample_error": g.sample_error,
            "alert": open_alert_json(g.alert)}


def compare_json(*, head: dict, totals: dict) -> dict:
    """The run before this one: `{**run_head_json(...), "totals"}` —
    `run_row_json`'s shape with a measure vector where the timeline goes.

    The head is the head builder's own output, so the compare column and that
    run's own screen cannot disagree about its counts. No difference of any
    kind is computed: subtracting two cost sums with different
    measured/unmeasured splits produces a number no adapter reported, and two
    columns say the same thing without minting one."""
    return {**head, "totals": totals}


def runs_json(*, generated_at: str, coverage: tuple[dict, ...],
              freshness: tuple[dict, ...], rows: tuple[dict, ...],
              backlog: tuple[dict, ...], pending: tuple[dict, ...],
              trend: tuple[dict, ...], failures: tuple[dict, ...]) -> dict:
    """The automation-health view: `{"generated_at", "coverage", "freshness",
    "runs", "backlog", "pending", "trend", "failures"}`.

    No `revision` key. This envelope reads `.state/` and not the wiki, so
    claiming wiki provenance for it would be a lie, and the page must not be
    able to draw a stale badge against a revision nothing here was read at.

    `coverage` is what it carries instead: the ragged left edge of
    `run_starts.jsonl` — younger than the health log it is joined against — is
    a fact on the wire rather than a gap the reader has to notice. It bounds
    `trend` and `failures` too: both are aggregates of the same events the
    `runs` list is drawn from, which is why they ride here rather than on a
    route of their own with a second story about what is missing."""
    return {"generated_at": generated_at,
            "coverage": list(coverage),
            "freshness": list(freshness),
            "runs": list(rows),
            "backlog": list(backlog),
            "pending": list(pending),
            "trend": list(trend),
            "failures": list(failures)}


def agent_stage_json(ev: events.RunEvent, trace: DeepLink) -> dict:
    """One agent stage as the ledger recorded it: `{"event_id", "task", "db",
    "model", "model_tier", "mode", "adapter", "started", "duration_s", "ok",
    "rolled_back", "usage", "trace"}`.

    `trace` is `link_json` of the stage's Langfuse trace
    (`Resolver.stage_trace`), so every surface that lists a stage offers the
    same door to what the agent was told and said.

    Not `stage_json`, which is the seven-box run timeline (`events.Stage`) and
    a different thing that happens to share the word: that one is derived from
    the shape of a run's children, this one is a line somebody wrote.

    `ok` is `validation_ok` verbatim, so it is `true`, `false` or the literal
    None, and never a boolean standing for an absent verdict. `usage` is
    `usage_json`, None where the line measured nothing."""
    return {"event_id": ev.event_id,
            "task": ev.task,
            "db": ev.db,
            "model": ev.model,
            "model_tier": ev.model_tier,
            "mode": ev.mode,
            "adapter": ev.adapter,
            "started": ev.at,
            "duration_s": ev.duration_s,
            "ok": ev.validation_ok,
            "rolled_back": ev.rolled_back,
            "usage": usage_json(ev.usage),
            "trace": link_json(trace)}


def touched_incident_json(*, slug: str, title: str, status: str, db: str,
                          commits: tuple[str, ...]) -> dict:
    """One incident a tick wrote: `{"slug", "title", "status", "db",
    "commits"}`.

    `commits` is every commit of this tick that landed on the page, because a
    tick may write one page more than once and a single sha would silently
    name one of them. `title`, `status` and `db` are the snapshot's, so a chip
    on the agents screen and the incident it opens read the same words.

    A slug the snapshot does not hold still crosses, with an empty title: the
    tick did write that page, and the wiki no longer holding it is a fact
    about the wiki rather than a reason to drop the tick's work."""
    return {"slug": slug, "title": title, "status": status, "db": db,
            "commits": list(commits)}


def tick_json(*, run_id: str, command: str, started: str, finished: str,
              stages: tuple[dict, ...], incidents: tuple[dict, ...],
              totals: dict) -> dict:
    """One loop tick: `{"run_id", "command", "started", "finished", "stages",
    "incidents", "totals"}`.

    `totals` is `totals_json` over this tick's own stages rather than a pair
    of bespoke sums, so the house rule holds without being restated: every sum
    ships beside the count of lines that reported it, and the tick row, the
    model table and the run screen all measure a set of stages the one way.

    `started` and `finished` are the first and last stage the ledger holds for
    this run, and never the RUN event's clock. The two logs are capped
    separately, so a tick whose RUN line has aged out still has a span, and a
    span taken from a line this list did not draw could disagree with the bars
    under it.

    `command` is the RUN or START event's, or `""` when neither is still
    held — an unnamed tick that did recorded work, which is what the caps
    leave behind."""
    return {"run_id": run_id,
            "command": command,
            "started": started,
            "finished": finished,
            "stages": list(stages),
            "incidents": list(incidents),
            "totals": totals}


def model_roll_json(r: events.ModelRoll) -> dict:
    """What one model did: `{"model", "adapters", "tiers", "ok", "failed",
    "totals"}`.

    `ok` and `failed` deliberately do not sum to `totals.stages`: a line that
    recorded no verdict is in neither, which `events.ModelRoll` explains, and
    the page draws the two counts rather than a rate it would have to pick a
    denominator for.

    `tiers` is a count per recorded `model_tier` and not one label, because
    the tier is a routing decision per stage: the live ledger runs one model
    `cheap` on lint and `strong` on report."""
    return {"model": r.model,
            "adapters": list(r.adapters),
            "tiers": dict(r.tiers),
            "ok": r.ok,
            "failed": r.failed,
            "totals": totals_json(r.totals)}


def agents_json(*, prov: dict, generated_at: str, window_hours: int,
                coverage: tuple[dict, ...], ticks: tuple[dict, ...],
                models: tuple[dict, ...], totals: dict) -> dict:
    """The agent-activity view: `{**prov, "generated_at", "window_hours",
    "coverage", "ticks", "models", "totals"}`.

    The one envelope on this server that carries both provenance and coverage,
    because it is the one that joins the two records: `ticks` and `models` are
    read out of `.state/`, which the caps bound, and the incidents on each
    tick are read out of the wiki at a revision. A reader has to be able to
    say which half an absence came from.

    `window_hours` echoes what was asked for, so a page reloaded at its own
    address puts its toggle back without parsing a URL twice — `search_json`'s
    reason for echoing the query.

    `totals` measures every stage in the window, so the table below it never
    has to be summed to answer "what did the loop cost tonight"."""
    return {**prov,
            "generated_at": generated_at,
            "window_hours": window_hours,
            "coverage": list(coverage),
            "ticks": list(ticks),
            "models": list(models),
            "totals": totals}


def backlog_row_json(*, digest: str, db: str, decision: str) -> dict:
    """One ledger entry that is real debt: `{"digest", "db", "decision"}`,
    from `events.backlog`, which owns the skip-versus-debt rule through
    `health.deliberate_skip`."""
    return {"digest": digest, "db": db, "decision": decision}


def pending_row_json(*, run_id: str, command: str, started: str) -> dict:
    """One start with no finish: `{"run_id", "command", "started"}`.

    There is no `stale` or `dead` key. `events.pending` cannot separate "still
    running" from "killed", and the page holds `started` beside
    `generated_at`, so a server-side verdict would be a guess with a name."""
    return {"run_id": run_id, "command": command, "started": started}


def db_run_json(*, event_id: str, db: str, decision: str,
                reasons: tuple[dict, ...], outcome: str, error: str,
                error_category: str, model_tier: str, commit: str,
                digest: str, digest_page: str,
                usage: dict | None) -> dict:
    """One database's work inside one run: `{"event_id", "db", "decision",
    "reasons", "outcome", "error", "error_category", "model_tier", "commit",
    "digest", "digest_page", "usage"}`.

    `digest` is the spelling the tick recorded, verbatim, because
    `RunEvent.digest_path` keeps it that way and rewriting it here would hide
    which writer wrote the row. `digest_page` is the wiki page that digest
    belongs to (`digests/<db>/<day>.md`), or `""` when the recorded path names
    no `digests` component at all.

    `usage` is `usage_json`'s dict or None, never a zero."""
    return {"event_id": event_id,
            "db": db,
            "decision": decision,
            "reasons": list(reasons),
            "outcome": outcome,
            "error": error,
            "error_category": error_category,
            "model_tier": model_tier,
            "commit": commit,
            "digest": digest,
            "digest_page": digest_page,
            "usage": usage}


def run_json(*, run: dict, stages: tuple[dict, ...],
             dbs: tuple[dict, ...], agents: tuple[dict, ...],
             links: tuple[dict, ...], totals: dict,
             compare: dict | None) -> dict:
    """One run's whole view: `{"run", "stages", "dbs", "agents", "links",
    "totals", "compare"}`, the head nested under `run` because the screen
    draws a header and lists and the lists are what it scrolls.

    `agents` is one `agent_stage_json` per STAGE line of the run, oldest
    first: the seven-box timeline says what happened, this says what each
    agent call was and links its trace.

    `links` is the run's specialist tools, the trace ahead of the ELK history:
    `.state/` drops its oldest lines at the caps, so the history link is how a
    run older than the caps is still reachable.

    `compare` is None when the logs hold no earlier run of the same command.
    That is an answer and not a 404 — `coverage` on the run list says how far
    back the logs go — and it is None rather than an empty head, because a row
    of zeros would read as a run that did nothing.

    Still no `revision`, for `runs_json`'s reason."""
    return {"run": run, "stages": list(stages), "dbs": list(dbs),
            "agents": list(agents), "links": list(links), "totals": totals,
            "compare": compare}


def _references(inc: Incident, closure: dict | None, link_base: str,
                exists: Callable[[str], bool]) -> list[dict]:
    """The incident page, its error pages, then the digests the closure
    cites: one flat list, deduplicated by path, first mention winning.

    Flat rather than a dict of dicts because the page renders one list of
    links and a grouping it would immediately flatten is a shape nobody
    wants. `label` is chosen per kind and never parsed out of a file: the
    page uses `inc.title`, an error uses its code, a digest uses its path.
    Deterministic, and there is no reading that can fail."""
    rows = [(inc.path, "page", inc.title)]
    rows += [(f"errors/{code}.md", "error", code)
             for code in inc.error_codes]
    rows += [(path, "digest", path)
             for path in (closure or {}).get("digests", ())]
    seen: dict[str, dict] = {}
    for path, kind, label in rows:
        if path not in seen:
            seen[path] = {"path": path, "kind": kind, "label": label,
                          "exists": exists(path),
                          "url": f"{link_base.rstrip('/')}/{path}"}
    return list(seen.values())


def touch_json(*, run_id: str, at: str, commits: tuple[str, ...],
               stages: tuple[dict, ...]) -> dict:
    """One loop tick that wrote this incident page: `{"run_id", "at",
    "commits", "stages"}`.

    `stages` is the tick's whole ledger row set and not a sentence about it.
    The screen words "ingest cdb1_stby · Nemotron strong · 363 s" from these
    keys with the same function the agents screen uses, so one wording exists
    rather than one here and another there — and a summary minted on this side
    would be prose no other consumer could take apart.

    `at` is the newest of this tick's commits on the page, which is when the
    work landed; `commits` carries every one of them, because a tick that
    rewrote the page twice wrote it twice."""
    return {"run_id": run_id, "at": at, "commits": list(commits),
            "stages": list(stages)}


def incident_json(inc: Incident, *, revision: str, body: str,
                  allowed: tuple[dict, ...], closure: dict | None,
                  history: tuple[Commit, ...], strays: tuple[str, ...],
                  link_base: str, exists: Callable[[str], bool],
                  links: tuple[dict, ...], touched_by: tuple[dict, ...],
                  research: Mapping[str, Research],
                  resolutions: Mapping[str, tuple[Resolution, ...]],
                  last_seen: str | None) -> dict:
    """The whole read model for one incident.

    `head` repeats `revision` under the name the preview and commit responses
    also use, so the page can compare its base against the wiki without
    knowing which response it is holding.

    `references` is one flat list of absolute URLs against `report.link_base`
    covering the page itself, each error page it links, and each digest the
    closure cites, deduplicated by path. Existence rides on each row rather
    than pruning it, so a link the wiki does not hold degrades in place.
    `links` is the outward half, addressing the specialist tools the page's
    `evidence_ref` blocks name.

    `research` is one `research_json` row per `error_codes` entry, in the
    page's own order, so the screen draws what the wiki knows about each code
    beside the chip that names it. It repeats `path` and `exists` from
    `references` on purpose: `references` is the outward link list and drops
    to one row per path, while this list is per code and must stay parallel
    to `error_codes` for a card to line up with its chip.

    `resolutions` is not a key of its own. It is threaded into every
    `research_json` row, so each card carries the history of the code it
    names and the screen never has to join two lists by code itself.

    `touched_by` is the loop ticks that wrote this page, newest first, joined
    from the `Run-ID:` trailer on each commit. It is the incident-side half of
    the agents screen and answers the question `history` cannot: `history` says
    the page changed, this says which tick changed it and what that tick ran.
    `last_seen` is the key `queue_row_json` spells under the same name and
    for the reason argued there, so one incident reads the same on the queue
    and on its own page.

    `dirty` is derived here from `strays`: the incident page is one of them."""
    return {"revision": revision,
            "head": revision,
            "slug": inc.slug,
            "path": inc.path,
            "db": inc.db,
            "title": inc.title,
            "status": str(inc.status),
            "label": inc.status.label,
            "unknown_status": inc.unknown_status,
            "opened": inc.opened,
            "updated": inc.updated,
            "error_codes": list(inc.error_codes),
            "research": [research_json(code, exists=exists, research=research,
                                       resolutions=resolutions)
                         for code in inc.error_codes],
            "window": window_json(inc.monitoring),
            "actions": [action_json(r) for r in inc.actions.records],
            "problems": [problem_json(p) for p in inc.actions.problems],
            "allowed": list(allowed),
            "closure": closure,
            "history": [commit_json(c) for c in history],
            "touched_by": list(touched_by),
            "references": _references(inc, closure, link_base, exists),
            "links": list(links),
            "strays": list(strays),
            "dirty": inc.path in strays,
            "last_seen": last_seen,
            "body": body}


def preview_json(action: Action, proposal: Proposal, pv: Preview, *,
                 head: str, status_after: str, principal: Principal,
                 requires: tuple[str, ...]) -> dict:
    """The preview response: the normalised echo of the action
    (`incident_action.encode` plus `verb`, `base`, `at`), `head` beside
    `base` so a stale base is visible before the commit, `diff`, `findings`,
    `blocked` (`bool(pv.blocked())`), `strays`, `notes`, `paths`, `message`,
    `requires`, and `cli` — `incident_action.retry_line`, the pasteable
    fallback for exactly this transaction, actor, base and `at` pinned.

    Flat, with no `action` envelope: every key here is one the confirm screen
    reads directly, and a nested echo would make the page reach through a
    level to compare the `base` it holds.

    A stray does not block: it is a fact about the checkout rather than about
    this proposal, the same call the CLI makes when it exits 0 with a
    `stray:` line."""
    return {"verb": action.verb,
            "fields": incident_action.encode(action),
            "base": action.base,
            "at": action.at,
            "head": head,
            "actor": principal_json(principal),
            "status_after": status_after,
            "message": proposal.message,
            "paths": list(pv.paths),
            "diff": pv.diff,
            "findings": [finding_json(f) for f in pv.findings],
            "blocked": bool(pv.blocked()),
            "strays": list(pv.strays),
            "notes": list(pv.notes),
            "nothing_to_do": not pv.paths,
            "requires": list(requires),
            "cli": incident_action.retry_line(action)}


def outcome_json(outcome: incident_action.Published, *, base: str, at: str,
                 retry_after_s: float) -> dict:
    """The commit response body per outcome type; the status code is
    `api.HTTP_OF`'s business:

        Committed   -> {"sha", "paths", "pushed", "base", "at"}
        NothingToDo -> {"nothing_to_do": true, "base", "at"}
        BaseMoved   -> error_json("base_moved", …, expected, actual, …)
        TreeDirty   -> error_json("tree_dirty", …, paths, …)
        LintBlocked -> error_json("lint_blocked", …, findings, …)
        LockBusy    -> error_json("lock_busy", …, holder, retry_after_s, …)

    Every write response carries `base` and `at`, so a client that lost its
    state can converge from the response alone. `pushed` is read off
    `Committed`, never passed in. `retry_after_s` is keyword-only and
    required, so a 423 cannot ship a silently wrong `0`.

    An outcome type with no row raises rather than falling through to an
    empty body, which would reach the page as a 200 that said nothing."""
    pinned = {"base": base, "at": at}
    match outcome:
        case Committed(sha=sha, paths=paths, pushed=pushed):
            return {"sha": sha, "paths": list(paths), "pushed": pushed,
                    **pinned}
        case NothingToDo():
            return {"nothing_to_do": True, **pinned}
        case BaseMoved(expected=expected, actual=actual):
            return error_json("base_moved",
                              f"the wiki moved from {expected[:12]} to "
                              f"{actual[:12]}; nothing was written",
                              expected=expected, actual=actual, **pinned)
        case TreeDirty(paths=paths):
            return error_json("tree_dirty",
                              "the checkout holds uncommitted edits to paths "
                              "this commit writes; nothing was written",
                              paths=list(paths), **pinned)
        case LintBlocked(findings=findings):
            return error_json("lint_blocked",
                              "lint blocked the commit; nothing was written",
                              findings=[finding_json(f) for f in findings],
                              **pinned)
        case incident_action.LockBusy(holder=holder):
            return error_json("lock_busy", holder, holder=holder,
                              retry_after_s=retry_after_s, **pinned)
    raise TypeError(f"{type(outcome).__name__} is not a publish outcome")


def advisory_context_json(entry: "advisory.PackEntry") -> dict:
    """One line of what a tool read: `{"kind", "path", "heading", "chars",
    "truncated"}`.

    There is no text key, and there must never be one. This encodes an
    `advisory.PackEntry`, which is the manifest half of the pack and carries
    no body at all; the prompt lives on `advisory.PackSection` and never
    leaves that module, so a prompt on the wire is a shape nobody can build
    rather than a mistake nobody has made yet.

    `chars` is what the page shows instead: how much of the incident a click
    would hand a model, which is the number a cost conversation is about."""
    return {"kind": str(entry.kind),
            "path": entry.path,
            "heading": entry.heading,
            "chars": entry.chars,
            "truncated": entry.truncated}


def _reported(value: object) -> bool:
    """A real number an adapter reported. `True` is not one: json carries
    booleans to `isinstance` as numbers, and a flag summed into a spend line
    is a cost nobody paid."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def advisory_usage_json(usage: Mapping[str, object] | str) -> dict | str:
    """What one advisory call cost: `usage_json`'s key set —
    `{"input_tokens", "output_tokens", "cost_usd", "known"}` — over the
    harness's own dict, or the literal `harness.UNKNOWN` when the harness
    reported nothing at all.

    Not `usage_json` itself, and the difference is the whole reason this
    exists. `usage_json` encodes an `events.Usage`, whose fields are numbers,
    so a field no adapter reported has already become 0 by the time it gets
    here. This line is printed beside one operator's click, and a per-click
    cost line that prints $0.00 for a cost nobody measured is a lie the
    operator has no way to catch. So every field the adapter did not report
    stays the literal `harness.UNKNOWN` it arrived as, and the page renders a
    word rather than a number.

    `known` is `health.record_agent_run`'s rule, token counts only: a line
    carrying a real cost and no tokens is `known: false` and still a real
    cost, because what is unknown there is how the cost was arrived at."""
    if not isinstance(usage, Mapping):
        return harness.UNKNOWN
    reported = {key: usage.get(key) for key in
                ("input_tokens", "output_tokens", "cost_usd")}
    return {**{key: value if _reported(value) else harness.UNKNOWN
               for key, value in reported.items()},
            "known": any(_reported(reported[key])
                         for key in ("input_tokens", "output_tokens"))}


def advisory_answer_json(answer: "advisory.Answer") -> dict:
    """What the model said: `{"text", "cites", "model", "usage"}`.

    `cites` is the packed paths the answer names, in pack order, the post-hoc
    scan `advisory.cited` performs, under the word the answer panel uses.
    `model` is the row's resolved model, so a tier
    re-pointed in config is visible in the answer it produced."""
    return {"text": answer.text,
            "cites": list(answer.cites),
            "model": answer.model,
            "usage": advisory_usage_json(answer.usage)}


def advisory_run_json(run: "advisory.AdvisoryRun") -> dict:
    """One advisory run: `{"run_id", "tool", "target", "at", "question",
    "status", "started", "finished", "duration_s", "evidence_revision",
    "context", "answer", "error"}`. The whole body of the poll endpoint, and
    of the 202 a start answers with.

    `question` is on the wire because it is half of the run's identity —
    `advisory.run_id_for` hashes it — and because the page threads each answer
    under the question that produced it rather than under the tool.

    No provenance triple and no `revision`. `provenance_json`'s keys say a
    payload is a projection of the wiki at one revision, and an answer is
    not: it is what a model said about the material this run packed.
    `evidence_revision` is the one revision claim there is to make — the
    revision the pack was read at — and it is spelled the way
    `advisory.AdvisoryRun` spells it.

    `context` is the manifest and never a body: `advisory_context_json` has
    no text key, so what a run read crosses the wire as its inventory.

    `boot_id` stays off the wire. It is how `AdvisoryRun.as_of` decides a run
    under a dead process is `interrupted`, which the operator reads as
    `status`; the process identity behind that verdict is the server's
    business."""
    return {"run_id": run.run_id,
            "tool": run.tool,
            "target": run.target,
            "at": run.at,
            "question": run.question,
            "status": str(run.status),
            "started": run.started,
            "finished": run.finished,
            "duration_s": run.duration_s,
            "evidence_revision": run.evidence_revision,
            "context": [advisory_context_json(e) for e in run.context],
            "answer": (None if run.answer is None
                       else advisory_answer_json(run.answer)),
            "error": run.error}


def advisory_ceiling_json(*, budget: "advisory.Budget",
                          spent: "advisory.Spend", reason: str) -> dict:
    """What one row may spend and what it already has: `{"window_h",
    "max_runs", "max_cost_usd", "runs", "measured_runs", "unmeasured_runs",
    "cost_usd", "reason"}`.

    One dict for the ceiling and the spend, because the page draws them as
    one sentence ("3 of 20 in the last 24 hours") and a level between them
    would buy it nothing.

    `unmeasured_runs` is on the wire rather than left to the page to subtract:
    it is runs whose cost nobody reported, which is not zero spend, and a page
    that only saw `cost_usd` would present a partial sum as the total.

    `reason` is `advisory.check`'s stable word (`max_runs`, `cost_unknown`,
    `max_cost_usd`, or `tool_disabled`) and `""` when a click would run, so a
    greyed-out button carries the reason it is grey."""
    return {"window_h": budget.window_h,
            "max_runs": budget.max_runs,
            "max_cost_usd": budget.max_cost_usd,
            "runs": spent.runs,
            "measured_runs": spent.measured_runs,
            "unmeasured_runs": spent.unmeasured_runs,
            "cost_usd": spent.cost_usd,
            "reason": reason}


def advisory_tool_json(status: "advisory.ToolStatus") -> dict:
    """One row of the manifest: `{"id", "label", "role", "tier", "enabled",
    "target_field", "asks", "max_answer_chars", "sources", "context",
    "context_chars", "ceiling"}`.

    `asks` is `advisory.asks`, so the page draws a question box for exactly
    the rows whose instruction names one and never for a row it recognises by
    id.

    `sources` is the plain words of the evidence kinds the row may read, the
    `status` and `state` precedent, and it is the row's whole read authority
    rather than a summary of it. `context` is what those kinds actually packed
    for this incident, so the panel says what a click would read *before* the
    click, and `context_chars` is the total the ceiling conversation is about.

    `target_field` is the action-record field the answer is written for, `""`
    for a tool that answers rather than fills; the page lands one answer in a
    form control by reading it, and leaves the rest in the panel.

    `max_answer_chars` is the length the prompt asks for, not a truncation the
    server applies: `advisory.ToolSpec` says why, and the page shows it as the
    shape of the answer to expect."""
    spec = status.spec
    return {"id": spec.id,
            "label": spec.label,
            "role": str(spec.role),
            "tier": str(spec.tier),
            "enabled": spec.enabled,
            "target_field": spec.target_field,
            "asks": advisory.asks(spec),
            "max_answer_chars": spec.max_answer_chars,
            "sources": [str(kind) for kind in spec.sources],
            "context": [advisory_context_json(e)
                        for e in status.packed.manifest],
            "context_chars": status.packed.chars,
            "ceiling": advisory_ceiling_json(budget=spec.budget,
                                             spent=status.spent,
                                             reason=status.reason)}


def advisory_tools_json(*, slug: str, evidence_revision: str,
                        rows: tuple[dict, ...]) -> dict:
    """The advisory panel for one incident: `{"slug", "evidence_revision",
    "tools"}`, in `advisory.TOOLS` order.

    `evidence_revision` and not `revision`, the key `advisory_run_json` also
    refuses: every row here was packed at one revision, and naming it the way
    the run record names it is what lets the page see that the answer it is
    holding read the material this manifest is describing."""
    return {"slug": slug, "evidence_revision": evidence_revision,
            "tools": list(rows)}


MAX_EVIDENCE_KEYS = 12

MAX_EVIDENCE_ITEMS = 32

MAX_EVIDENCE_CHARS = 200

#: Every name `review.select` counts under, taken from `review.py` rather
#: than respelled: a count added there reaches the page without an edit here.
COUNT_NAMES = review.COUNT_NAMES

_INBOX_ROW = ("review_id", "generated_at", "source_revision", "counts",
              "synthesis_error")


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: object) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _rtext(value: object) -> str:
    """A recorded string. `None` reads as absent rather than as "None"."""
    return "" if value is None else str(value)


def _stamp(value: object) -> str | None:
    """A recorded instant, or the literal None. Not `_rtext`: an item nobody
    has acknowledged has no `acknowledged_at`, and `""` would reach the page
    as an acknowledgement with no time on it."""
    return value if isinstance(value, str) and value else None


def _rcount(value: object) -> int | None:
    """A recorded whole count, or None. `bool` is excluded: `True` is an `int`
    to Python and a flag to every writer here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _rflag(value: object) -> bool | None:
    """A recorded boolean, or None where the writer recorded something else.
    A truthiness coercion here would turn an error string into `True`."""
    return value if isinstance(value, bool) else None


def _strings(value: object) -> list[str]:
    """A recorded list of bare strings, each bounded and the list capped.
    Anything else in it is dropped.

    Bounded rather than passed through: these are fingerprints and wiki
    paths, and an unbounded list of unbounded strings under a key the browser
    reads is the shape a prompt would arrive in."""
    return [item[:MAX_EVIDENCE_CHARS]
            for item in _rows(value)[:MAX_EVIDENCE_ITEMS]
            if isinstance(item, str)]


def _scalar(value: object) -> object | None:
    """One evidence value the wire may carry: a boolean, a bounded string or
    a finite real number. Anything else is absent.

    Finite because `json.dumps` writes a `NaN` or an `Infinity` as a bare
    word, which is not JSON and which the page's `JSON.parse` refuses: one
    unusable number in one reason would cost the whole response."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:MAX_EVIDENCE_CHARS]
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return None


def _evidence(value: object) -> dict:
    """A reason's evidence as a flat mapping of scalars and lists of scalars.

    The detectors write these key names, so allowlisting the names would be a
    second copy of `review.DETECTORS` that has to be edited whenever a
    detector grows a fact. The *shape* is allowlisted instead: a value nested
    deeper than one list is dropped, every string is bounded, and the mapping
    is capped at `MAX_EVIDENCE_KEYS` in sorted order, so a detector that ever
    writes a large or nested blob costs itself rather than the page."""
    out: dict[str, object] = {}
    raw = _map(value)
    for key in sorted(name for name in raw if isinstance(name, str)):
        if len(out) >= MAX_EVIDENCE_KEYS:
            break
        item = raw[key]
        name = key[:MAX_EVIDENCE_CHARS]
        if isinstance(item, (list, tuple)):
            out[name] = [one for one in
                         (_scalar(x) for x in list(item)[:MAX_EVIDENCE_ITEMS])
                         if one is not None]
            continue
        one = _scalar(item)
        if one is not None:
            out[name] = one
    return out


def _counts(value: object) -> dict:
    """The selection's counts: whole numbers under `COUNT_NAMES` and nothing
    else. Allowlisted by name, unlike a reason's evidence: this vocabulary is
    `review.select`'s own and is closed, so a name outside it is a key the
    browser would read that `_REVIEW_KEYS` never named."""
    out = {}
    for key in COUNT_NAMES:
        number = _rcount(_map(value).get(key))
        if number is not None:
            out[key] = number
    return out


def _changes(value: object) -> dict:
    """Each `review.CHANGE_BUCKETS` bucket as a list of fingerprints, always
    every bucket, the way a published selection always carries every one."""
    raw = _map(value)
    return {key: _strings(raw.get(key)) for key in review.CHANGE_BUCKETS}


def _window(value: object) -> dict:
    """The days a selection looked over: `{"from", "to", "days"}`."""
    raw = _map(value)
    return {"from": _rtext(raw.get("from")), "to": _rtext(raw.get("to")),
            "days": _rcount(raw.get("days"))}


_Rule = tuple[str, Callable[[object], object]]

#: Allowlist: sub-table -> wire key -> (the file's key, the coercion). Nothing
#: is copied wholesale, so a field a future `review.py` writes has no path onto
#: the wire until it gets a row here.
_REVIEW_KEYS: Mapping[str, Mapping[str, _Rule]] = {
    "review": {
        "review_id": ("review_id", _rtext),
        "generated_at": ("generated_at", _rtext),
        "source_revision": ("source_revision", _rtext),
        "window": ("window", _window),
        "counts": ("counts", _counts),
        "changes": ("changes", _changes),
        "explanation": ("explanation", _rtext),
        "synthesis_error": ("synthesis_error", _rtext),
    },
    "finding": {
        "fingerprint": ("fingerprint", _rtext),
        "kind": ("kind", _rtext),
        "code": ("code", _rtext),
        "slug": ("slug", _rtext),
        "db": ("db", _rtext),
        "title": ("title", _rtext),
        "path": ("path", _rtext),
        "severity": ("severity", _rtext),
        "movement": ("movement", _rtext),
        "explanation": ("explanation", _rtext),
    },
    "reason": {
        "code": ("code", _rtext),
        "evidence": ("evidence", _evidence),
    },
    "synthesis": {
        "summary": ("summary", _rtext),
        "evidence_refs": ("evidence_refs", _strings),
        "model_tier": ("model_tier", _rtext),
    },
    "theme": {
        "title": ("title", _rtext),
        "detail": ("detail", _rtext),
        "evidence_refs": ("evidence_refs", _strings),
    },
    "ack": {
        "acknowledged_at": ("acknowledged_at", _stamp),
        "suppressed_until": ("suppressed_until", _stamp),
        "actor": ("actor", _rtext),
    },
    "pack_manifest": {
        "kind": ("kind", _rtext),
        "heading": ("heading", _rtext),
        "path": ("path", _rtext),
        "chars": ("chars", _rcount),
        "truncated": ("truncated", _rflag),
    },
    #: One attempt to put one week in front of one recipient: which channel
    #: carried it, how much of the review that recipient is shown, whether it
    #: landed, and when. A `failed` row carries the category and never the
    #: relay's own words.
    "delivery": {
        "key": ("key", _rtext),
        "channel": ("channel", _rtext),
        "recipient": ("recipient", _rtext),
        "content_class": ("content_class", _rtext),
        "status": ("status", _rtext),
        "at": ("at", _stamp),
        "error": ("error", _rtext),
    },
}

#: Keys `review.py` writes that deliberately never cross as themselves, with
#: why. Keyed by table, so a key excused for one shape is not excused for all.
_REVIEW_DROPPED: Mapping[str, Mapping[str, str]] = {
    "review": {
        "schema_version": "the file's version, which the reader checks and "
                          "the page has no use for",
        "findings": "carried a row at a time through the `finding` table",
        "synthesis": "carried through the `synthesis` table, or the literal "
                     "null where no note landed",
        "pack_manifest": "carried a row at a time through the "
                         "`pack_manifest` table",
        "deliveries": "carried a row at a time through the `delivery` table",
    },
    "finding": {
        "evidence_hash": "how `select` decides movement; the page reads the "
                         "`movement` word that hash produced",
        "reasons": "carried a row at a time through the `reason` table",
    },
    "synthesis": {
        "themes": "carried a row at a time through the `theme` table",
    },
    "ack": {
        "updated_at": "`review.acknowledge`'s own bookkeeping; the page reads "
                      "the two instants the operator set",
    },
    "reason": {},
    "theme": {},
    "pack_manifest": {},
    "delivery": {
        "detail": "the relay's own words about a failure, which is raw text "
                  "from outside this deployment; the page draws the category "
                  "beside it and never the text",
    },
}


def _project(table: str, source: object) -> dict:
    """One sub-table applied to one mapping out of a review file."""
    raw = _map(source)
    return {key: coerce(raw.get(name))
            for key, (name, coerce) in _REVIEW_KEYS[table].items()}


def review_fingerprints(review_file: Mapping) -> frozenset[str]:
    """Every fingerprint one review file selected.

    A reader and not an encoder, and here rather than in `api.py` for the
    reason the module exists: `findings` and `fingerprint` are two names the
    browser reads, and a second spelling of either is the drift
    `_REVIEW_KEYS` is meant to make impossible."""
    key = _REVIEW_KEYS["finding"]["fingerprint"][0]
    return frozenset(_rtext(_map(row).get(key))
                     for row in _rows(_map(review_file).get("findings")))


def ack_json(item: Mapping | None) -> dict:
    """What the operator has said about one finding: `{"acknowledged_at",
    "suppressed_until", "actor"}`.

    Present on every finding and never null, so the page reads one shape: a
    finding nobody has touched carries the same three keys with nothing in
    them. `review.acknowledge` and `review.suppress` are the only writers of
    the file behind it, and this endpoint never rewrites the review."""
    return _project("ack", item)


def review_finding_json(finding: Mapping, ack: Mapping | None) -> dict:
    """One thing to look at: the deterministic row, its `reasons`, and the
    `ack` merged in at read.

    Spelled `review_finding_json` because `finding_json` is already the lint
    finding's encoder and the two share no field.

    `evidence_hash` is not here; `_REVIEW_DROPPED` says why."""
    row = _project("finding", finding)
    row["reasons"] = [reason_json(**_project("reason", one))
                      for one in _rows(_map(finding).get("reasons"))]
    row["ack"] = ack_json(ack)
    return row


def synthesis_json(synthesis: Mapping | None) -> dict | None:
    """The model's covering note: `{"summary", "themes", "evidence_refs",
    "model_tier"}`, or None where no note landed.

    None is a fact about the model and not a gap in the review: the inbox item
    is the deterministic selection, `review_json`'s `synthesis_error` names
    the category the failure was classified as, and the page never hides the
    findings for it."""
    if not isinstance(synthesis, Mapping):
        return None
    row = _project("synthesis", synthesis)
    row["themes"] = [_project("theme", one)
                     for one in _rows(synthesis.get("themes"))]
    return row


def inbox_row_json(review: Mapping) -> dict:
    """One week on the list: `{"review_id", "generated_at", "source_revision",
    "counts", "synthesis_error", "synthesized"}`.

    No findings. The inbox is a list of weeks, and a list that already carried
    every finding would leave the review screen nothing to fetch.

    `synthesized` is whether a covering note landed, which is a different
    question from `synthesis_error`: a deployment with synthesis switched off
    publishes neither a note nor a failure."""
    projected = _project("review", review)
    return {**{key: projected[key] for key in _INBOX_ROW},
            "synthesized": isinstance(_map(review).get("synthesis"), Mapping)}


def inbox_json(*, generated_at: str, rows: tuple[dict, ...]) -> dict:
    """The weekly reviews this deployment still holds: `{"generated_at",
    "reviews"}`, newest first.

    No `revision` key, the `runs_json` rule: this reads `.state/review/` and
    not the wiki, so there is no revision to name and the page must not be
    able to draw a stale badge against one. Each row's `source_revision` is a
    claim about the week it sits on, not about this list."""
    return {"generated_at": generated_at, "reviews": list(rows)}


def review_json(review: Mapping, *, acks: Mapping) -> dict:
    """One week: the selection, its findings with the operator's ack state
    merged in, the covering note, and an inventory of what the pack read.

    `source_revision`, and never `revision` or `evidence_revision`. Three
    words for three claims. `revision` (`provenance_json`) says a payload is a
    projection of the wiki at one revision, which a selection is not.
    `evidence_revision` (`advisory_run_json`) is the revision an LLM run
    packed its material at. `source_revision` is the revision a *deterministic*
    evaluator judged at, which is `closure_json`'s word; the selection is
    deterministic, so it keeps the third.

    The acks are merged here at read and the review file is never rewritten,
    which is the whole point of `review.py`'s split: `state.json` and
    `reviews/<id>.json` are the cron stage's, `acks.json` is the portal's.

    `pack_manifest` is an inventory and never a body, `advisory_context_json`'s
    rule: it says how much of the week a model was handed, which is the number
    a cost conversation is about. `deliveries` is one row per attempt to put
    this week in front of one recipient, saying where it went and whether it
    landed."""
    items = _map(_map(acks).get("items"))
    return {
        **_project("review", review),
        "findings": [review_finding_json(
            one, items.get(_rtext(_map(one).get("fingerprint"))))
            for one in _rows(_map(review).get("findings"))],
        "synthesis": synthesis_json(_map(review).get("synthesis")),
        "pack_manifest": [_project("pack_manifest", one)
                          for one in _rows(_map(review).get("pack_manifest"))],
        "deliveries": [_project("delivery", one)
                       for one in _rows(_map(review).get("deliveries"))],
    }


def inbox_act_json(*, fingerprint: str, ack: dict) -> dict:
    """What an acknowledge or a suppress answered: `{"fingerprint", "ack"}`.

    The finding's whole row is deliberately not re-sent. The endpoint changed
    one item in `acks.json` and nothing about the published review, and
    answering with the review's row would invite the page to believe the
    deterministic half moved too."""
    return {"fingerprint": fingerprint, "ack": ack}


def error_json(error: str, message: str, **extra) -> dict:
    """`{"error", "message", **extra}`, the shape of every non-2xx body.

    Rewrites one sentence: a `lock_busy` holder naming this process becomes
    "another workbench request is publishing". `lock._holder` formats
    `pid <self>` for a portal-versus-portal collision, and shipping that
    verbatim reads as the portal blocking itself. The `holder` extra is
    rewritten with it, because that is the copy the page puts on screen and
    two spellings of one refusal is worse than either. A holder sentence this
    regex cannot read passes through unchanged, because failing to reformat a
    diagnostic must never fail the response that carries it."""
    if error == "lock_busy" and _self_held(message):
        message = SELF_HOLDER
        if extra.get("holder") is not None:
            extra["holder"] = SELF_HOLDER
    return {"error": error, "message": message, **extra}
