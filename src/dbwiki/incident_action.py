"""The operator boundary: one grammar, one decoder, one audited publish.

`incident_cli` (argv) and `dbwiki.portal` (JSON) are two surfaces over the
same six verbs. Everything both of them would otherwise spell twice lives
here: which fields a verb takes, how named values become a
`lifecycle.Command`, how a command renders back into words, and what the run
record says about a transaction. `incident_cli` keeps argparse and printing;
`portal` keeps HTTP and HTML.

The schema is derived, never restated. `command_fields` reads the `lifecycle`
dataclasses, so `required` is "the field has no default" and a field added to
a command becomes a flag, a JSON key and a form control with no other edit.
What a dataclass cannot carry — the help sentence, the choice list, the argv
spelling — lives in `PRESENTATION` beside it, keyed by `(verb, field)`. The
widget vocabulary stays out of `lifecycle`, whose docstring commits it to
being the only code that decides transitions.

`Action` is the unit: page, verb, command, actor, base, `at`, pinned into one
frozen value. Two equal `Action`s produce byte-identical proposals against the
same tree. That is the retry contract the CLI prints as `retry_line` and the
portal echoes across the preview/commit gap.

Nothing here reads a file or runs git except `publish`, which is the single
audited writer for both surfaces.
"""

import dataclasses
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, get_args, get_origin

from . import gitutil, incidents, lifecycle, transaction
from .incidents import Outcome, RecoverySignal, signal_from_yaml, signal_to_yaml
from .lock import Holder, LockBusyError, single_flight
from .validate import instant

SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

class Widget(StrEnum):
    """What a field holds, and therefore how each surface renders it: an
    argparse kwarg set, an HTML control, a JSON type. Presentation only —
    `decode` coerces from the dataclass annotation, so a widget cannot
    change what a value means."""

    LINE = "line"        # one line of text
    TEXT = "text"        # multi-line free text
    CHOICE = "choice"    # one of `Presentation.choices`
    LIST = "list"        # repeatable single-line entries
    SIGNAL = "signal"    # an `incidents.RecoverySignal` as a flat mapping
    INSTANT = "instant"  # YYYY-MM-DDTHH:MM:SSZ
    FLAG = "flag"        # boolean


@dataclass(frozen=True)
class Field:
    """One command field as `command_fields` derives it from the dataclass.

    `name` is the dataclass field, the JSON key, the argparse dest and the
    form control name; there is no renaming layer. `type` is the annotation,
    which is what `decode` coerces against. `required` is "no default", and
    `default` is that default (None when required)."""

    name: str
    type: Any
    required: bool
    default: Any


@dataclass(frozen=True)
class Presentation:
    """What the dataclass cannot say about a field: the one help sentence
    both `--help` and the HTML label use, the widget, the choice list, and
    the argv spelling when it is not `--<name with dashes>` (only
    `update_error_pages`, which argv spells as the negative
    `--no-error-pages`)."""

    help: str
    widget: Widget
    choices: tuple[str, ...] = ()
    cli_flag: str = ""


#: verb -> the `lifecycle.Command` it builds. The six verbs are the CLI's
#: words, and the portal uses the same ones: one word per act across argv,
#: JSON and the run-health stream.
VERBS: dict[str, type] = {
    "record-action": lifecycle.RecordAction,
    "monitor": lifecycle.StartMonitoring,
    "extend": lifecycle.ExtendMonitoring,
    "resolve": lifecycle.Resolve,
    "reopen": lifecycle.Reopen,
    "merge": lifecycle.Merge,
}

#: command type -> verb. Derived, never a second list.
VERB_OF: dict[type, str] = {command: verb for verb, command in VERBS.items()}


def _signal_keys() -> dict[str, str]:
    keys = {}
    for cls in get_args(RecoverySignal):
        shape = signal_to_yaml(cls(""))
        keys[shape["kind"]] = next(k for k in shape if k != "kind")
    return keys


#: signal kind -> the payload key `incidents.signal_from_yaml` expects, read
#: off `signal_to_yaml` so no kind is named twice.
SIGNAL_KEYS: dict[str, str] = _signal_keys()

_INTENT = Presentation("what the operator set out to achieve", Widget.LINE)
_SUMMARY = Presentation("what was done, one line", Widget.LINE)
_TICKET = Presentation("ticket the work was raised under", Widget.LINE)
_OUTCOME = Presentation("what came of it (default pending)", Widget.CHOICE,
                        choices=tuple(str(o) for o in Outcome))
_ROLLBACK = Presentation("how to undo it", Widget.LINE)
_EVIDENCE = Presentation("a digest backing the action "
                         "(digests/<db>/<day>.md); repeatable", Widget.LIST)
_NOTES = Presentation("free text kept verbatim under the record", Widget.TEXT)
_SIGNAL = Presentation("what recovery looks like: error_absent:CODE, "
                       "event_present:REGEX, flow_resumed:SOURCE, "
                       "manual:TEXT", Widget.SIGNAL)
_UNTIL = Presentation("when the window closes", Widget.INSTANT)
_REASON = Presentation("why the incident is coming back open", Widget.LINE)
_RESIDUAL_RISK = Presentation("what is still not safe; labelled at the top "
                              "of the notes", Widget.TEXT)
_UPDATE_ERROR_PAGES = Presentation("leave the linked error pages' Resolution "
                                   "history alone", Widget.FLAG,
                                   cli_flag="--no-error-pages")
_INTO = Presentation("the incident that keeps the evidence; this page is "
                     "folded into it", Widget.LINE)

#: (verb, field name) -> presentation. Exactly one row per field
#: `command_fields` derives for that verb, asserted by a test, so a field
#: added to a command dataclass fails there rather than reaching an operator
#: as an unlabelled control. Rows share a `Presentation` where the sentence is
#: the same; the key is per verb so one verb can reword a field without
#: touching the others.
PRESENTATION: dict[tuple[str, str], Presentation] = {
    ("record-action", "intent"): _INTENT,
    ("record-action", "summary"): _SUMMARY,
    ("record-action", "ticket"): _TICKET,
    ("record-action", "outcome"): _OUTCOME,
    ("record-action", "rollback"): _ROLLBACK,
    ("record-action", "evidence"): _EVIDENCE,
    ("record-action", "notes"): _NOTES,

    ("monitor", "signal"): _SIGNAL,
    ("monitor", "until"): _UNTIL,
    ("monitor", "intent"): _INTENT,
    ("monitor", "summary"): _SUMMARY,
    ("monitor", "ticket"): _TICKET,
    ("monitor", "evidence"): _EVIDENCE,
    ("monitor", "notes"): _NOTES,

    ("extend", "until"): _UNTIL,
    ("extend", "intent"): _INTENT,
    ("extend", "notes"): _NOTES,

    ("resolve", "summary"): _SUMMARY,
    ("resolve", "residual_risk"): _RESIDUAL_RISK,
    ("resolve", "ticket"): _TICKET,
    ("resolve", "evidence"): _EVIDENCE,
    ("resolve", "notes"): _NOTES,
    ("resolve", "update_error_pages"): _UPDATE_ERROR_PAGES,

    ("reopen", "reason"): _REASON,
    ("reopen", "evidence"): _EVIDENCE,
    ("reopen", "notes"): _NOTES,

    ("merge", "into"): _INTO,
    ("merge", "notes"): _NOTES,
}

#: outcome type -> the `validation` fact the run record carries. The same
#: words the portal's HTTP error bodies use, so one vocabulary spans the CLI
#: message, the audit line and the wire.
VALIDATION: dict[type, str] = {
    transaction.Committed: "ok",
    transaction.NothingToDo: "ok",
    transaction.BaseMoved: "base_moved",
    transaction.LintBlocked: "lint_blocked",
    transaction.TreeDirty: "tree_dirty",
}


class FieldError(ValueError):
    """Operator input that cannot become a command: an unknown verb, an
    unknown or missing field, a value of the wrong type, an outcome outside
    the enum, a signal that is not `<kind>:<payload>`.

    Carries `field` (empty for a verb-level problem) so a surface can point
    at the control that is wrong instead of restating the whole request. A
    `ValueError` like every other builder refusal, so `incident_cli`'s single
    `except ValueError` -> exit 2 keeps working unchanged."""

    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(message)


@dataclass(frozen=True)
class Action:
    """One operator act, fully pinned.

    Invariants, checked once here so neither surface re-checks:
    - `path` is `incidents/<name>.md`, so it cannot leave the incident
      directory;
    - `base` is a full 40-hex sha the caller obtained from
      `transaction.head` or from a read response, never invented here;
    - `at` is a real `YYYY-MM-DDTHH:MM:SSZ` instant (`validate.instant`:
      `2026-02-30T10:00:00Z` is refused), the record's identity: re-running the
      same `Action` rewrites identical bytes, a fresh `at` is a second
      record;
    - `command` came from `decode`, so its field shapes are already checked.
    """

    path: str
    verb: str
    command: lifecycle.Command
    actor: transaction.Actor
    base: str
    at: str

    def __post_init__(self) -> None:
        if not self.path.startswith(f"{incidents.INCIDENT_DIR}/"):
            raise FieldError("", f"not an incident page: {self.path}")
        if not SHA_RE.match(self.base):
            raise FieldError(
                "base", f"base: {self.base!r} is not a full 40-hex wiki sha")
        try:
            instant(self.at)
        except ValueError:
            raise FieldError(
                "at", f"at: {self.at!r} is not a real YYYY-MM-DDTHH:MM:SSZ "
                      f"instant") from None

    @property
    def slug(self) -> str:
        """The page stem: the incident's identity in the audit record, in
        `.state/monitoring/<slug>.json` and in the portal's URL."""
        return Path(self.path).stem

    def proposal(self, tree: transaction.Tree) -> transaction.Proposal:
        """`lifecycle.build` against the tree this action asserts.

        Raises `ValueError` when `tree.revision != self.base`: building
        against a tree other than the asserted base is how a preview comes to
        show bytes the commit will not write. Otherwise the exceptions are
        `lifecycle.build`'s own, every one a `ValueError` subclass."""
        if tree.revision != self.base:
            raise FieldError("base", f"tree is at {tree.revision}, not the "
                                     f"asserted base {self.base}")
        return lifecycle.build(tree, self.path, self.command, self.actor,
                               self.at)


@dataclass(frozen=True)
class LockBusy:
    """Another dbwiki command held `orchestrator.lock` for longer than the
    caller was willing to wait. Nothing was written and nothing was even
    attempted.

    A `publish` outcome rather than an exception, so both surfaces answer it
    from the same `match` as a moved base: the CLI exits 1 printing `holder`,
    the portal answers 423. `holder` is the sentence naming the pid, command
    and start time read back from the lock file; `held_by` is those same
    facts as fields, None when the lock file could not say."""

    holder: str
    held_by: Holder | None = None


#: Everything `publish` can answer. The transaction's five, plus the refusal
#: only a locking caller can meet.
Published = transaction.Outcome | LockBusy


def command_fields(command_type: type) -> tuple[Field, ...]:
    """The schema of one command, in dataclass field order.

    Derived from `dataclasses.fields`: `required` is "no default and no
    default factory", `default` is the declared default. Raises TypeError for
    anything not in `VERBS.values()`."""
    if command_type not in VERB_OF:
        raise TypeError(f"{command_type!r} is not an incident command")
    specs = []
    for f in dataclasses.fields(command_type):
        required = (f.default is dataclasses.MISSING
                    and f.default_factory is dataclasses.MISSING)
        specs.append(Field(name=f.name, type=f.type, required=required,
                           default=None if required else f.default))
    return tuple(specs)


def flag_of(verb: str, name: str) -> str:
    """The argv spelling of one field: `PRESENTATION`'s `cli_flag` when it
    has one, else `--<name with dashes>`."""
    return (PRESENTATION[(verb, name)].cli_flag
            or "--" + name.replace("_", "-"))


def decode(*, slug: str, verb: str, fields: Mapping[str, Any],
           actor: transaction.Actor, base: str, at: str) -> Action:
    """Named operator input -> a fully pinned `Action`. The one decoder.

    `fields` is plain data: argparse's namespace flattened by `incident_cli`,
    or the JSON object the portal received. Values carry the JSON shape of
    their declared type: `str` for text and instants, a list of strings for
    `tuple[str, ...]`, a bool for `bool`, the `Outcome` string for `Outcome`,
    and for `RecoverySignal` either the canonical mapping
    (`{"kind": "error_absent", "code": "TNS-12564"}`) or the `<kind>:<payload>`
    string the shell affords. An absent optional field is left to the
    command's own default.

    Refuses, with the offending field named: a verb outside `VERBS`; a field
    the verb's command has no home for (silently dropping a typo'd key loses
    the operator's notes without telling them); a required field that is
    absent; a value of the wrong shape; an outcome outside the enum; a signal
    that is neither the mapping nor `<kind>:<payload>`.

    Everything deeper belongs to `incidents.ActionRecord` and
    `lifecycle.build` and is not repeated here: this checks the shape of the
    transport, the domain checks the meaning. Blank required text is left to
    `ActionRecord`, which already refuses an empty `intent` by name."""
    command_type = VERBS.get(verb)
    if command_type is None:
        raise FieldError("", f"unknown verb {verb!r}; one of "
                             + ", ".join(VERBS))
    specs = {spec.name: spec for spec in command_fields(command_type)}
    for name in fields:
        if name not in specs:
            raise FieldError(name, f"{verb} has no field {name!r}; its fields "
                                   f"are " + ", ".join(specs))
    values = {}
    for name, spec in specs.items():
        if fields.get(name) is None:
            if spec.required:
                raise FieldError(name, f"{verb} needs {name}")
            continue
        values[name] = _coerce(spec, fields[name])
    return Action(path=incidents.incident_path(slug), verb=verb,
                  command=command_type(**values), actor=actor, base=base,
                  at=at)


def _coerce(spec: Field, raw: Any) -> Any:
    """One JSON-shaped value as its declared type. Dispatches on the
    annotation rather than on the widget, so how a surface draws a field can
    never change what the field means."""
    if spec.type is bool:
        if not isinstance(raw, bool):
            raise FieldError(spec.name,
                             f"{spec.name}: {raw!r} is not true or false")
        return raw
    if spec.type is Outcome:
        try:
            return Outcome(str(raw))
        except ValueError:
            raise FieldError(spec.name, f"{spec.name}: {raw!r} is not one of "
                             + ", ".join(str(o) for o in Outcome)) from None
    if get_origin(spec.type) is tuple:
        if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
            raise FieldError(spec.name, f"{spec.name}: expected a list of "
                                        f"paths, not {type(raw).__name__}")
        for item in raw:
            if not isinstance(item, str):
                raise FieldError(spec.name,
                                 f"{spec.name}: {item!r} is not a path")
        return tuple(raw)
    if spec.type == RecoverySignal:
        return _signal(raw)
    if not isinstance(raw, str):
        raise FieldError(spec.name, f"{spec.name}: expected text, not "
                                    f"{type(raw).__name__}")
    return raw


def _signal(raw: Any) -> RecoverySignal:
    """The canonical mapping, or the `<kind>:<payload>` string the shell
    affords. Splits on the first colon only: an `event_present` pattern
    carries colons of its own."""
    if isinstance(raw, Mapping):
        try:
            return signal_from_yaml(raw)
        except incidents.ActionMalformed as e:
            raise FieldError("signal", f"signal: {e}") from None
    if not isinstance(raw, str):
        raise FieldError("signal", "signal: expected <kind>:<payload> or a "
                                   "{kind, payload} mapping")
    kind, _, payload = raw.partition(":")
    if kind not in SIGNAL_KEYS or not payload:
        raise FieldError("signal", f"signal: {raw!r} is not <kind>:<payload>; "
                                   f"kind is one of "
                                   + ", ".join(sorted(SIGNAL_KEYS)))
    try:
        return signal_from_yaml({"kind": kind, SIGNAL_KEYS[kind]: payload})
    except incidents.ActionMalformed as e:
        raise FieldError("signal", f"signal: {e}") from None


def encode(action: Action) -> dict[str, Any]:
    """The inverse of `decode`'s `fields`: JSON-safe, canonical and minimal.

    Each value goes through `plain`, so signals render as their
    `signal_to_yaml` mapping, `evidence` as a list, `outcome` as its string
    and flags as booleans; a value equal to its field's default is omitted,
    so the echo is the canonical form of what was asked for. Round trip:
    rebuilding an `Action` from `encode(a)` and the same slug, actor, base
    and `at` yields `a` for every constructible action."""
    out = {}
    for spec in command_fields(type(action.command)):
        value = getattr(action.command, spec.name)
        if not spec.required and value == spec.default:
            continue
        out[spec.name] = plain(value)
    return out


def plain(value: Any) -> Any:
    """One command value as the JSON shape `decode` takes back: an `Outcome`
    as its string, a signal as its `signal_to_yaml` mapping, a tuple as a
    list of strings, anything else as it stands.

    Public because a surface needs it for one value at a time as well as for
    a whole command: `portal.wire` renders a field's declared *default* with
    it, and a default the page pre-fills has to be the same shape the page
    posts back."""
    if isinstance(value, Outcome):
        return str(value)
    if isinstance(value, get_args(RecoverySignal)):
        return signal_to_yaml(value)
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return value


def to_argv(action: Action) -> list[str]:
    """The words after `dbwiki` that reproduce this action:
    `incident <verb> <slug> --actor ... <verb flags> --base ... --at ...
    --commit`.

    Lives here rather than in `incident_cli` because the portal prints it
    too: the workbench shows the exact CLI line beside every preview, which
    is what makes "the CLI is the fallback" a copy-paste rather than a
    promise. `--flag=value` when the value opens with a dash, so a pasted
    line parses back into these same flags rather than into two.

    Walks `encode`, not the command, so the line carries exactly the fields
    the normalised echo carries: whatever the portal sends back on commit is
    what the pasted line publishes."""
    fields = encode(action)
    words = ["incident", action.verb, action.slug, "--actor",
             action.actor.email]
    for name, value in fields.items():
        words += _echoed(action.verb, name, value)
    return words + ["--base", action.base, "--at", action.at, "--commit"]


def _echoed(verb: str, name: str, value: Any) -> list[str]:
    """The words one encoded field contributes to `to_argv`.

    A flag is emitted bare, because `encode` omits a value equal to its
    default and a `FLAG` field's `cli_flag` spells the non-default
    (`--no-error-pages` for `update_error_pages=False`)."""
    flag = flag_of(verb, name)
    match PRESENTATION[(verb, name)].widget:
        case Widget.FLAG:
            return [flag]
        case Widget.LIST:
            return [word for item in value for word in _word(flag, item)]
        case Widget.SIGNAL:
            value = f"{value['kind']}:{value[SIGNAL_KEYS[value['kind']]]}"
    return _word(flag, value)


def _word(flag: str, value: Any) -> list[str]:
    """`--flag value`, or `--flag=value` when the value opens with a dash: as
    two words argparse reads `--intent --commit` as two flags, and the pasted
    line then means something the preview never showed."""
    text = str(value)
    return [f"{flag}={text}"] if text.startswith("-") else [flag, text]


def retry_line(action: Action) -> str:
    """`to_argv` shell-quoted into one pasteable command. `shlex.split` reads
    it back into the same flags, and the actor is the resolved one, so a
    paste cannot re-resolve to the pasting machine's identity."""
    return " ".join(shlex.quote(word)
                    for word in ["dbwiki", *to_argv(action)])


def publish(wiki: Path, state_dir: Path, action: Action,
            proposal: transaction.Proposal, *, lock_wait_s: float,
            push: bool, surface: str) -> Published:
    """Commit under audit. The one place an operator action's run record is
    written, so the CLI and the portal cannot drift.

    Takes `lock.single_flight(state_dir, f"{surface}:{verb}:{slug}",
    lock_wait_s)` itself, commits with `push=False`, releases the lock, and
    only then pushes best-effort when `push` is set. Pushing outside the lock
    is what keeps a hung remote from stalling the cron tick; the lock guards
    the tree, and a push does not touch it. `single_flight` is never nested:
    a second flock on the same file in one process conflicts with the first,
    so no caller may already hold it.

    Facts on every arm: `{incident, command, actor, base, at, commit, pushed,
    validation, surface}`. `surface` is `"cli"` or `"portal"`;
    `actor.source` cannot tell them apart, because both resolve to `config`
    or `git`.

    A refusal is never `rec.fail`. `BaseMoved`, `LintBlocked`, `TreeDirty`
    and `LockBusy` are two writers racing under optimistic concurrency, not a
    broken pipeline; recording them as failures turns `dbwiki health` red
    every time an operator publishes during a tick.

    Nothing is printed and nothing is serialised inside the recorder: a
    `BrokenPipeError` from a closed pipe (CLI) or a dropped client (portal)
    raised out of the `with` is what makes `run_recorder` categorise a run
    whose commit already landed as failed.

    A committed act also queues its human-feedback scores
    (`scores.queue_action`), after the recorder closes: best-effort, never a
    reason the act fails, and no Langfuse key is needed here."""
    from .health import run_recorder
    facts = dict(incident=action.slug, command=action.verb,
                 actor=proposal.actor.email, base=proposal.base, at=action.at,
                 surface=surface)
    with run_recorder(state_dir, "incident") as rec:
        try:
            with single_flight(state_dir,
                               f"{surface}:{action.verb}:{action.slug}",
                               lock_wait_s) as lock:
                outcome = transaction.commit(wiki, proposal, lock=lock,
                                             push=False)
        except LockBusyError as e:
            rec.note(**facts, commit=None, pushed=False,
                     validation="lock_busy")
            return LockBusy(str(e), e.holder)
        committed = isinstance(outcome, transaction.Committed)
        pushed = bool(committed and push and gitutil.push_best_effort(wiki))
        if committed:
            outcome = dataclasses.replace(outcome, pushed=pushed)
        rec.note(**facts, commit=outcome.sha if committed else None,
                 pushed=pushed, validation=VALIDATION[type(outcome)])
    if committed:
        from .scores import queue_action
        queue_action(wiki, state_dir, action, outcome.sha, surface)
    return outcome
