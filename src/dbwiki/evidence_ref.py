"""The durable markdown evidence reference: a bounded logical query a curated
page carries so the exact Elastic context survives outside portal state.

Schema v1 is `docs/notes/incident-remediation-workflow.md`'s YAML verbatim. Nothing
here holds a URL, a host, a credential or a raw query language: the reference
names a `template_id`, an environment, a data view, an entity, a signature and
a bounded window, and `deeplink.Resolver` turns that into whatever the current
deployment's Kibana is. That split is the whole point. A page written today
still resolves after the Kibana base URL changes, and a page committed on a
laptop names no production host.

Distinct from `incidents.EvidenceRef`, which is the digest citation an action
makes (`digests/<db>/YYYY-MM-DD.md`): that one says which observation backs a
human act, this one says which bounded query shows the observation.

Pure over text, like `incidents`: no git, no Path, no clock. `parse_refs`
follows `incidents.parse_actions` and answers `(records, problems)` rather
than raising, because one malformed block on an incident page must not stop
the portal rendering the page or lint reporting the rest of it.
"""

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass

import yaml

EVIDENCE_REF_SCHEMA_VERSION = 1

#: The `kind:` vocabulary.
KINDS = frozenset({"elastic_filter"})

REF_FIELDS = ("schema_version", "kind", "template_id", "environment",
              "data_view", "entity", "signature", "window",
              "representative_document_id", "summary")

ISO_Z_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

MAX_SUMMARY = 200

_DIGEST_TEMPLATE_ID = "oracle-error-context-v1"
_DIGEST_DATA_VIEW = "oracle-logs"


class EvidenceRefMalformed(ValueError):
    """A block that claims to be an `evidence_ref` and cannot be one. Raised
    by `EvidenceRef.__post_init__` and by the parse; `parse_refs` collects
    these so `lint` reports every bad block on the page."""


@dataclass(frozen=True)
class Entity:
    """What the evidence is about. `type` is `database` today."""

    type: str
    id: str


@dataclass(frozen=True)
class Signature:
    """What was seen. `type` is `oracle_error` for a code, `rule` for a
    pattern-library rule name, which is what an AWR group has instead."""

    type: str
    value: str


@dataclass(frozen=True)
class Window:
    """The bounded time range, `[start, end]`, both `YYYY-MM-DDTHH:MM:SSZ`.

    The YAML keys are `from` and `to`, per the document. The python names
    differ because `from` is a keyword, and a field nobody can name without
    `getattr` is worse than the small asymmetry `render_ref` absorbs."""

    start: str
    end: str


@dataclass(frozen=True)
class RefProblem:
    """A block that looks like an evidence reference and is not one. `lint`
    maps these to `evidence-ref-malformed`, the `ActionProblem` precedent."""

    where: str
    message: str


@dataclass(frozen=True)
class EvidenceRef:
    """One bounded logical query, validated at construction.

    Validation lives in `__post_init__` rather than only in the parse so a ref
    built in code cannot be worse than a ref read off a page: the field's type
    is the constructor's contract, `incidents.EvidenceRef`'s discipline.

    `template_id` is deliberately *not* checked against `deeplink.TEMPLATES`.
    The deployment's template vocabulary lives outside the markdown on purpose
    so pages stay portable, and a template this deployment does not map is a
    resolver state (`LinkState.MISSING`) rather than a page that fails to
    parse.

    Round-trip invariant: `parse_refs(render_ref(r)) == ((r,), ())`. Empty
    optionals are omitted from the YAML and come back as their defaults."""

    schema_version: int
    kind: str
    template_id: str
    environment: str
    data_view: str
    entity: Entity
    signature: Signature
    window: Window
    representative_document_id: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != EVIDENCE_REF_SCHEMA_VERSION:
            raise EvidenceRefMalformed(
                f"schema_version: {self.schema_version!r} is not "
                f"{EVIDENCE_REF_SCHEMA_VERSION}")
        if self.kind not in KINDS:
            raise EvidenceRefMalformed(f"kind: {self.kind!r} is not one of "
                                       + ", ".join(sorted(KINDS)))
        named = [("template_id", self.template_id),
                 ("environment", self.environment),
                 ("data_view", self.data_view),
                 ("entity.type", self.entity.type),
                 ("entity.id", self.entity.id),
                 ("signature.type", self.signature.type),
                 ("signature.value", self.signature.value)]
        for name, value in named:
            if not str(value).strip():
                raise EvidenceRefMalformed(f"{name}: must not be empty")
        for name, value in (("window.from", self.window.start),
                            ("window.to", self.window.end)):
            if not ISO_Z_RE.match(value):
                raise EvidenceRefMalformed(
                    f"{name}: {value!r} is not YYYY-MM-DDTHH:MM:SSZ")
            try:
                dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                raise EvidenceRefMalformed(
                    f"{name}: {value!r} is not a real instant") from exc
        if self.window.end < self.window.start:
            raise EvidenceRefMalformed(
                f"window.to: {self.window.end!r} is earlier than window.from "
                f"{self.window.start!r}")
        if len(self.summary) > MAX_SUMMARY:
            raise EvidenceRefMalformed(
                f"summary: {len(self.summary)} characters is over the "
                f"{MAX_SUMMARY}-character limit")


def _mapping(where: str, raw: object) -> Mapping:
    if not isinstance(raw, Mapping):
        raise EvidenceRefMalformed(f"{where}: {raw!r} is not a mapping")
    return raw


def _text(data: Mapping, key: str) -> str:
    value = data.get(key)
    return "" if value is None else str(value)


def _ref_from_yaml(raw: object) -> EvidenceRef:
    """The `evidence_ref:` payload into a ref. Raises EvidenceRefMalformed
    naming the offending field, `incidents.parse_action`'s wording. A key
    outside REF_FIELDS is one such failure: a typo like `data_veiw:` must not
    silently resolve against the wrong index."""
    data = _mapping("evidence_ref", raw)
    for key in data:
        if key not in REF_FIELDS:
            raise EvidenceRefMalformed(
                f"{key}: is not an evidence_ref field; expected "
                + ", ".join(REF_FIELDS))
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise EvidenceRefMalformed(
            f"schema_version: {version!r} is not a whole number")
    entity = _mapping("entity", data.get("entity"))
    signature = _mapping("signature", data.get("signature"))
    window = _mapping("window", data.get("window"))
    return EvidenceRef(
        schema_version=version,
        kind=_text(data, "kind"),
        template_id=_text(data, "template_id"),
        environment=_text(data, "environment"),
        data_view=_text(data, "data_view"),
        entity=Entity(_text(entity, "type"), _text(entity, "id")),
        signature=Signature(_text(signature, "type"),
                            _text(signature, "value")),
        window=Window(_text(window, "from"), _text(window, "to")),
        representative_document_id=_text(data, "representative_document_id"),
        summary=_text(data, "summary"))


def _yaml_blocks(text: str) -> list[str]:
    """Every fenced ```yaml block's body, in page order. An unclosed fence
    ends at the page end, the way a markdown renderer reads it."""
    out, opened = [], None
    lines = text.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if opened is None:
            if stripped == "```yaml":
                opened = i + 1
        elif stripped == "```":
            out.append("\n".join(lines[opened:i]))
            opened = None
    if opened is not None:
        out.append("\n".join(lines[opened:]))
    return out


def parse_refs(text: str) -> tuple[tuple[EvidenceRef, ...],
                                   tuple[RefProblem, ...]]:
    """Every evidence reference on a page, in page order, plus what claimed to
    be one and was not.

    A fenced yaml block whose top-level mapping is not exactly the single key
    `evidence_ref` is skipped in silence, not reported: `## Action` blocks
    share these pages, and a page carrying one is not thereby malformed. A
    block that *does* claim the key and cannot be a reference costs itself one
    `RefProblem` and nothing else, so one typo never hides the refs around
    it. `where` numbers the `evidence_ref` blocks alone, which is what an
    operator counts when they go looking."""
    refs: list[EvidenceRef] = []
    problems: list[RefProblem] = []
    seen = 0
    for block in _yaml_blocks(text):
        try:
            data = yaml.safe_load(block)
        except Exception:  # noqa: BLE001 - a bad date raises ValueError
            continue
        if not isinstance(data, Mapping) or set(data) != {"evidence_ref"}:
            continue
        seen += 1
        try:
            refs.append(_ref_from_yaml(data["evidence_ref"]))
        except EvidenceRefMalformed as exc:
            problems.append(RefProblem(f"evidence_ref block {seen}",
                                       str(exc)))
    return tuple(refs), tuple(problems)


def render_ref(ref: EvidenceRef) -> str:
    """The canonical fenced block, ending in one newline.

    Canonical bytes: the document's key order, `sort_keys=False`, block style,
    unbounded width, and the empty optionals omitted, `incidents.render_action`
    exactly. One dump of the whole `{"evidence_ref": {...}}` mapping rather
    than a dump plus a re-indent, so the nesting is PyYAML's and not ours."""
    values: dict[str, object] = {
        "schema_version": ref.schema_version,
        "kind": ref.kind,
        "template_id": ref.template_id,
        "environment": ref.environment,
        "data_view": ref.data_view,
        "entity": {"type": ref.entity.type, "id": ref.entity.id},
        "signature": {"type": ref.signature.type,
                      "value": ref.signature.value},
        "window": {"from": ref.window.start, "to": ref.window.end},
    }
    if ref.representative_document_id:
        values["representative_document_id"] = ref.representative_document_id
    if ref.summary:
        values["summary"] = ref.summary
    block = yaml.safe_dump({"evidence_ref": values}, sort_keys=False,
                           default_flow_style=False, width=10**6,
                           allow_unicode=True)
    return f"```yaml\n{block}```\n"


def _instant(raw: object, fallback: str) -> str:
    """A digest timestamp normalised to `YYYY-MM-DDTHH:MM:SSZ`. The live
    compactor writes millisecond precision (`2026-01-15T09:31:39.327Z`), which
    the schema does not carry; an absent or unreadable stamp falls back rather
    than making the whole group unreferenceable."""
    text = str(raw or "")
    if not text:
        return fallback
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_digest_group(db: str, day: str, group: Mapping[str, object], *,
                      environment: str = "production") -> EvidenceRef:
    """The reference for one notable group of one day's digest.

    This is what gives 2.6 real data on day one: every digest the compactor
    has already written yields references without anyone editing a page.

    `environment` is a keyword rather than a digest field because a digest
    records what was observed, never which deployment observed it; the caller
    holds that fact and the default is the one deployment that exists.

    The signature is the group's first Oracle code when it has one and its
    rule name otherwise, which is the AWR arm: `awr._group` emits notable
    groups with no codes and no `es_samples`, so those references carry a
    `rule` signature and no representative document.

    Total over any mapping a digest file can hold. Every field is read
    defensively and falls back rather than raising, because the caller is
    `daily_html` on the tick's render path: one odd group in one digest must
    not cost the day its page. The only remaining `EvidenceRefMalformed` comes
    from `day`, which is the caller's own filename."""
    codes = [str(c) for c in (group.get("codes") or []) if str(c)]
    signature = (Signature("oracle_error", codes[0]) if codes
                 else Signature("rule", str(group.get("rule") or "unknown")))
    samples = list(group.get("es_samples") or [])
    first = samples[0] if samples and isinstance(samples[0], Mapping) else {}
    document = str(first.get("id") or "")
    start = _instant(group.get("first_ts"), f"{day}T00:00:00Z")
    end = _instant(group.get("last_ts"), f"{day}T23:59:59Z")
    try:
        count = int(group.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    summary = f"{count} × {signature.value} on {db} on {day}"[:MAX_SUMMARY]
    return EvidenceRef(
        schema_version=EVIDENCE_REF_SCHEMA_VERSION,
        kind="elastic_filter",
        template_id=_DIGEST_TEMPLATE_ID,
        environment=environment,
        data_view=_DIGEST_DATA_VIEW,
        entity=Entity("database", db),
        signature=signature,
        window=Window(min(start, end), max(start, end)),
        representative_document_id=document,
        summary=summary)
