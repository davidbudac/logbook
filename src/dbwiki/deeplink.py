"""Turning a durable `evidence_ref.EvidenceRef` into a link for *this*
deployment, or into an honest reason why there is no link.

Lives in `src/dbwiki/` rather than `portal/` because `daily_html` consumes it
and the generated daily page must never import the portal.

The shape is a registry, not a branch chain: `TEMPLATES` maps a `template_id`
to a URL and the exact set of placeholders that URL may name, checked at
import, and one private substitution refuses any other key. That is what makes
"no emitted URL carries a parameter the template does not name" a property of
the table rather than a promise in a review comment.

Every value is URL-encoded. A database name, an Oracle code and a run id all
reach here from data, so a `&` or a quote in one of them must not be able to
add a parameter to the query it lands in.
"""

import datetime as dt
import string
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from .evidence_ref import EvidenceRef
from .observability import trace_id

#: The widest window a deep link may open.
MAX_WINDOW_H = 168

_DBWIKI_DATA_VIEW = "dbwiki"


class LinkState(StrEnum):
    """The five states `docs/notes/incident-remediation-workflow.md` requires a link
    to be able to be in. `DENIED` is the Stage 4 RBAC stub and has no producer
    today; a test asserts this module never names it outside this enum, so it
    cannot quietly acquire one."""

    AVAILABLE = "available"
    DENIED = "denied"
    EXPIRED = "expired"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class DeepLink:
    """One offered drilldown, or one refusal with its reason.

    `AVAILABLE` means "this deployment is configured for the target, and the
    data says a target should exist". It never means "verified present".
    Nothing here probes: `events_only` removed the Langfuse read API (commit
    `4cd8e4d`), and the portal's CSP forbids the page reaching any origin but
    its own (`portal/server.py`), so a deep link is a plain anchor and the
    operator finds out by clicking.

    `url` is None in every state but `AVAILABLE`, so a caller cannot render a
    dead anchor by forgetting to check the state. `description` is the
    human-readable sentence shown before navigation and stays meaningful when
    the target has expired, which is the point of keeping the summary and the
    window in the markdown."""

    state: LinkState
    url: str | None
    label: str
    description: str
    note: str = ""


@dataclass(frozen=True)
class Template:
    """One allowlisted destination shape. `placeholders` is the exact set of
    names `url` may interpolate, checked against the URL at import."""

    family: str
    url: str
    placeholders: frozenset[str]


TEMPLATES: Mapping[str, Template] = MappingProxyType({
    "oracle-error-context-v1": Template(
        "kibana",
        "{base}/app/discover#/?_g=(time:(from:'{from}',to:'{to}'))"
        "&_a=(index:'{data_view}',query:(language:kuery,query:'{query}'))",
        frozenset({"base", "data_view", "query", "from", "to"})),
    "kibana-doc-v1": Template(
        "kibana",
        "{base}/app/discover#/doc/{data_view}/{index}?id={id}",
        frozenset({"base", "data_view", "index", "id"})),
    "dbwiki-run-v1": Template(
        "kibana",
        "{base}/app/discover#/?_a=(index:'{data_view}',"
        "query:(language:kuery,query:'{query}'))",
        frozenset({"base", "data_view", "query"})),
    "langfuse-session-v1": Template(
        "langfuse",
        "{base}/project/{project}/sessions/{session_id}",
        frozenset({"base", "project", "session_id"})),
    "langfuse-trace-v1": Template(
        "langfuse",
        "{base}/project/{project}/traces/{trace_id}",
        frozenset({"base", "project", "trace_id"})),
})


def _check_templates(templates: Mapping[str, Template]) -> None:
    """Every row's URL names exactly its declared placeholders. Import-time,
    because a template that interpolates something nobody allowlisted is a bug
    no runtime path would reach until an operator clicked it."""
    for template_id, template in templates.items():
        named = {name for _, name, _, _ in
                 string.Formatter().parse(template.url) if name}
        if named != set(template.placeholders):
            raise ValueError(
                f"{template_id}: the url names {sorted(named)} but the "
                f"template allowlists {sorted(template.placeholders)}")


_check_templates(TEMPLATES)


def _fill(template: Template, *, base: str, values: Mapping[str, str]) -> str:
    """The one substitution. Every value is percent-encoded with nothing safe,
    so no data can add a parameter or a fragment; the base is encoded with
    `:/` safe, because a base is a URL and not a value. Holding the base out
    of `values` is what keeps that asymmetry legible without a comment beside
    every call. A key the template does not allowlist is a refusal, and a key
    it needs and nobody supplied is a `KeyError` from `format_map`."""
    unknown = set(values) - template.placeholders
    if unknown:
        raise KeyError(f"{sorted(unknown)} is not allowlisted by the template")
    filled = {name: urllib.parse.quote(str(value), safe="")
              for name, value in values.items()}
    filled["base"] = urllib.parse.quote(base.rstrip("/"), safe=":/")
    return template.url.format_map(filled)


def _parse(instant: str) -> dt.datetime:
    return dt.datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=dt.timezone.utc)


def _stamp(moment: dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _describe(ref: EvidenceRef) -> str:
    body = (f"{ref.signature.value} on {ref.entity.id}, "
            f"{ref.window.start} to {ref.window.end}, "
            f"{ref.environment} {ref.data_view}")
    return f"{ref.summary}. {body}" if ref.summary else body


@dataclass(frozen=True)
class Resolver:
    """What this deployment can offer, built once from `portal.links`.

    Stateless and cheap: every method is a pure function of the config it
    holds plus its argument, so callers rebuild one per request rather than
    threading a long-lived object through.

    `retention_days` defaults to None, meaning no retention is claimed and
    `EXPIRED` is never produced. Retention is a deployment fact this code
    cannot observe, and a guessed default would stamp `expired` on links that
    resolve fine. It is the rule `stats` already owns for unknown token counts
    and `health` owns for quiet sources: absence is never evidence. An
    operator who states a retention gets the state; one who does not gets a
    link and finds out by clicking.

    The clock is consulted only when a retention *is* stated, which is what
    lets `daily_html` stay a pure function of disk plus config."""

    kibana_base: str = ""
    data_views: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))
    retention_days: int | None = None
    langfuse_base: str = ""
    langfuse_project: str = ""

    @classmethod
    def from_config(cls, links: Mapping[str, object]) -> "Resolver":
        """`{"kibana": {base, data_views, retention_days}, "langfuse":
        {base, project}}`, every key optional.

        Every default lives here rather than in `config.PORTAL_DEFAULTS`
        because `Config.portal`'s merge is one level deep: a `portal:` block
        that names `links` replaces that whole mapping, so a default written
        into `PORTAL_DEFAULTS["links"]` would vanish the moment an operator
        configured anything. `cfg.portal.get("links")` can also be absent
        entirely, and no sub-key may be a `KeyError`.

        `langfuse.base` is the *browser-facing* Langfuse host and is
        deliberately never `cfg.langfuse["host"]`, which is the loopback
        address the exporter posts to (`docs/langfuse.md`). Loopback in an
        operator's browser is the operator's own machine, which is not where
        the traces are."""
        kibana = links.get("kibana") if isinstance(links, Mapping) else None
        langfuse = links.get("langfuse") if isinstance(links, Mapping) else None
        kibana = kibana if isinstance(kibana, Mapping) else {}
        langfuse = langfuse if isinstance(langfuse, Mapping) else {}
        views = kibana.get("data_views")
        retention = kibana.get("retention_days")
        return cls(
            kibana_base=str(kibana.get("base") or ""),
            data_views=MappingProxyType(
                {str(k): str(v) for k, v
                 in (views if isinstance(views, Mapping) else {}).items()}),
            retention_days=None if retention is None else int(retention),
            langfuse_base=str(langfuse.get("base") or ""),
            langfuse_project=str(langfuse.get("project") or ""))

    def logs(self, ref: EvidenceRef, *,
             now: dt.datetime | None = None) -> DeepLink:
        """The bounded filter: the signature plus its neighbours, which is
        what makes an investigation useful. The primary action, ahead of
        `document`, per the document's own preference for context over a
        fragile single-event link."""
        label, description = "Open exact logs", _describe(ref)
        template = TEMPLATES.get(ref.template_id)
        if template is None:
            return DeepLink(
                LinkState.MISSING, None, label, description,
                f"this deployment maps no template {ref.template_id!r}")
        refusal = self._kibana_refusal(ref.data_view, label, description)
        if refusal is not None:
            return refusal
        expired = self._expired(ref, label, description, now)
        if expired is not None:
            return expired
        start, note = self._clamped(ref)
        query = f'"{ref.entity.id}" and "{ref.signature.value}"'
        return DeepLink(
            LinkState.AVAILABLE,
            _fill(template, base=self.kibana_base,
                  values={"data_view": self.data_views[ref.data_view],
                          "query": query, "from": start,
                          "to": ref.window.end}),
            label, description, note)

    def document(self, ref: EvidenceRef, *,
                 now: dt.datetime | None = None) -> DeepLink:
        """The one representative document, offered beside `logs` and never
        instead of it. A reference with no document id is `MISSING`: the AWR
        arm builds notable groups from a report file and there are no log
        documents to open, while its `logs` link stays perfectly good.

        The `index` placeholder is the reference's logical data view name.
        The markdown carries no concrete index, on purpose, and Kibana's doc
        route reads that segment as the pattern the id is looked up under, so
        the logical name is the only honest value here."""
        label = "Open the exact document"
        description = (f"The representative document "
                       f"{ref.representative_document_id or 'none recorded'}, "
                       f"{ref.environment} {ref.data_view}")
        if not ref.representative_document_id:
            return DeepLink(
                LinkState.MISSING, None, label, description,
                "the reference records no representative document; an "
                "AWR-derived group never has one")
        refusal = self._kibana_refusal(ref.data_view, label, description)
        if refusal is not None:
            return refusal
        expired = self._expired(ref, label, description, now)
        if expired is not None:
            return expired
        return DeepLink(
            LinkState.AVAILABLE,
            _fill(TEMPLATES["kibana-doc-v1"], base=self.kibana_base,
                  values={"data_view": self.data_views[ref.data_view],
                          "index": ref.data_view,
                          "id": ref.representative_document_id}),
            label, description)

    def trace(self, *, run_id: str, task: str = "",
              mode: str = "") -> DeepLink:
        """The Langfuse session for one pipeline run: `session_id` is the
        run id, which `observability` already exports it as.

        Never `EXPIRED`. The retention this resolver knows about is Kibana's,
        and claiming it for Langfuse would be a guess about a second store."""
        label = "Open exact trace"
        detail = ", ".join(part for part in (task, mode) if part)
        description = (f"The Langfuse session for run {run_id}"
                       + (f" ({detail})" if detail else ""))
        for value, key in ((self.langfuse_base, "base"),
                           (self.langfuse_project, "project")):
            if not value:
                return DeepLink(LinkState.UNAVAILABLE, None, label,
                                description,
                                f"no links.langfuse.{key} is configured")
        return DeepLink(
            LinkState.AVAILABLE,
            _fill(TEMPLATES["langfuse-session-v1"], base=self.langfuse_base,
                  values={"project": self.langfuse_project,
                          "session_id": run_id}),
            label, description)

    def stage_trace(self, *, event_id: str, task: str = "",
                    db: str = "") -> DeepLink:
        """The Langfuse trace of one agent stage, by the ledger line's
        `event_id`: the exporter seeds the trace id from it
        (`observability.trace_id`), so the id is computed, not looked up.

        A stage exported before the seeding shipped has a random trace id,
        so this link opens nothing for it. Kibana's retention is not
        Langfuse's, so `EXPIRED` is never claimed, for `trace`'s reason."""
        label = "Open this stage's trace"
        detail = " ".join(part for part in (task, db) if part)
        description = (f"The Langfuse trace of stage {event_id}"
                       + (f" ({detail})" if detail else ""))
        for value, key in ((self.langfuse_base, "base"),
                           (self.langfuse_project, "project")):
            if not value:
                return DeepLink(LinkState.UNAVAILABLE, None, label,
                                description,
                                f"no links.langfuse.{key} is configured")
        return DeepLink(
            LinkState.AVAILABLE,
            _fill(TEMPLATES["langfuse-trace-v1"], base=self.langfuse_base,
                  values={"project": self.langfuse_project,
                          "trace_id": trace_id(event_id)}),
            label, description)

    def run_history(self, *, run_id: str) -> DeepLink:
        """Everything the `logs-dbwiki.*` streams still hold for one run. The
        path past the JSONL caps: `.state/` drops its oldest lines on write,
        ELK does not, so a run the workbench can no longer show is still here
        when the deployment ships its telemetry."""
        label = "Open the run's ELK history"
        description = (f"Every dbwiki log line Elasticsearch holds for run "
                       f"{run_id}")
        refusal = self._kibana_refusal(_DBWIKI_DATA_VIEW, label, description)
        if refusal is not None:
            return refusal
        return DeepLink(
            LinkState.AVAILABLE,
            _fill(TEMPLATES["dbwiki-run-v1"], base=self.kibana_base,
                  values={"data_view": self.data_views[_DBWIKI_DATA_VIEW],
                          "query": f'run_id:"{run_id}"'}),
            label, description)

    def _kibana_refusal(self, data_view: str, label: str,
                        description: str) -> DeepLink | None:
        if not self.kibana_base:
            return DeepLink(LinkState.UNAVAILABLE, None, label, description,
                            "no links.kibana.base is configured")
        if data_view not in self.data_views:
            return DeepLink(
                LinkState.UNAVAILABLE, None, label, description,
                f"no links.kibana.data_views entry for {data_view!r}")
        return None

    def _expired(self, ref: EvidenceRef, label: str, description: str,
                 now: dt.datetime | None) -> DeepLink | None:
        if self.retention_days is None:
            return None
        moment = now or dt.datetime.now(dt.timezone.utc)
        age = (moment - _parse(ref.window.end)).days
        if age <= self.retention_days:
            return None
        return DeepLink(
            LinkState.EXPIRED, None, label, description,
            f"links.kibana.retention_days is {self.retention_days} and the "
            f"window ended {age} days ago")

    def _clamped(self, ref: EvidenceRef) -> tuple[str, str]:
        end = _parse(ref.window.end)
        start = _parse(ref.window.start)
        if end - start <= dt.timedelta(hours=MAX_WINDOW_H):
            return ref.window.start, ""
        clamped = end - dt.timedelta(hours=MAX_WINDOW_H)
        return _stamp(clamped), (
            f"the window is wider than the {MAX_WINDOW_H}-hour cap; the link "
            f"starts at {_stamp(clamped)}, not {ref.window.start}")
