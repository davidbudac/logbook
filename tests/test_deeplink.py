"""Resolving a durable reference into this deployment's link, or into an
honest refusal.

Two things are worth proving here and neither is "the URL looks right". The
first is the state ladder: every refusal an operator can meet must be reachable
from a config a deployment could actually have. The second is that no value
reaching this module from data can change the *shape* of the URL it lands in,
which is asserted against the template table rather than against a fixed
string, so a new template inherits the check.
"""

import datetime as dt
import inspect
import re
import string
import urllib.parse

import pytest
import yaml

from dbwiki import deeplink
from dbwiki.config import Config
from dbwiki import observability
from dbwiki.deeplink import MAX_WINDOW_H, DeepLink, LinkState, Resolver
from dbwiki.evidence_ref import (Entity, EvidenceRef, Signature, Window,
                                 parse_refs, render_ref)

KIBANA = "https://kibana.example.invalid"
LANGFUSE = "http://dbhost.example.net:3000"
LOOPBACK = "http://localhost:3000"

DAY = "2026-08-30"
START = f"{DAY}T10:00:00Z"
END = f"{DAY}T10:20:00Z"

LINKS = {
    "kibana": {"base": KIBANA + "/",
               "data_views": {"oracle-logs": "dv-oracle",
                              "dbwiki": "dv-dbwiki"}},
    "langfuse": {"base": LANGFUSE, "project": "dbwiki"},
}

PARAM_RE = re.compile(r"[?&#]([A-Za-z_][A-Za-z0-9_]*)=")


def ref(**overrides) -> EvidenceRef:
    args = {"schema_version": 1, "kind": "elastic_filter",
            "template_id": "oracle-error-context-v1",
            "environment": "production", "data_view": "oracle-logs",
            "entity": Entity("database", "cdb1"),
            "signature": Signature("oracle_error", "ORA-12543"),
            "window": Window(START, END),
            "representative_document_id": "AZ83bZHL6JT2v6-J7TTb",
            "summary": "ORA-12543 transport failures on cdb1"}
    return EvidenceRef(**(args | overrides))


AWR_REF = ref(signature=Signature("rule", "awr_top_wait"),
              representative_document_id="", summary="")

RESOLVER = Resolver.from_config(LINKS)


def test_every_template_names_exactly_the_placeholders_it_allowlists():
    for template_id, template in deeplink.TEMPLATES.items():
        named = {name for _, name, _, _
                 in string.Formatter().parse(template.url) if name}
        assert named == set(template.placeholders), template_id


def test_a_template_whose_url_names_an_unlisted_placeholder_fails_at_import():
    rogue = {"rogue-v1": deeplink.Template(
        "kibana", "{base}/app/{secret}", frozenset({"base"}))}
    with pytest.raises(ValueError, match="rogue-v1"):
        deeplink._check_templates(rogue)


def test_an_unmapped_template_is_missing_and_names_the_id():
    link = RESOLVER.logs(ref(template_id="oracle-error-context-v9"))
    assert link.state is LinkState.MISSING
    assert link.url is None
    assert "oracle-error-context-v9" in link.note


def test_an_unconfigured_deployment_is_unavailable_and_says_which_key():
    link = Resolver.from_config({}).logs(ref())
    assert link.state is LinkState.UNAVAILABLE
    assert link.note == "no links.kibana.base is configured"


def test_a_data_view_the_deployment_does_not_map_is_unavailable():
    link = RESOLVER.logs(ref(data_view="postgres-logs"))
    assert link.state is LinkState.UNAVAILABLE
    assert "postgres-logs" in link.note


def test_a_window_older_than_the_stated_retention_is_expired():
    resolver = Resolver.from_config(
        {**LINKS, "kibana": {**LINKS["kibana"], "retention_days": 7}})
    later = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    link = resolver.logs(ref(), now=later)
    assert link.state is LinkState.EXPIRED
    assert link.url is None
    assert "7" in link.note and "31 days ago" in link.note


def test_no_stated_retention_never_produces_expired():
    long_ago = ref(window=Window("2001-01-01T00:00:00Z",
                                 "2001-01-01T01:00:00Z"))
    assert RESOLVER.retention_days is None
    assert RESOLVER.logs(long_ago).state is LinkState.AVAILABLE


def test_a_configured_deployment_is_available_with_no_note():
    link = RESOLVER.logs(ref())
    assert link.state is LinkState.AVAILABLE
    assert link.note == ""
    assert link.url.startswith(KIBANA + "/app/discover")
    assert link.label == "Open exact logs"


def test_the_description_survives_expiry_and_still_names_the_evidence():
    resolver = Resolver.from_config(
        {**LINKS, "kibana": {**LINKS["kibana"], "retention_days": 1}})
    later = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    expired = resolver.logs(ref(), now=later)
    assert "ORA-12543" in expired.description
    assert "cdb1" in expired.description
    assert START in expired.description and END in expired.description


def test_denied_has_no_producer_and_the_module_never_names_it():
    assert LinkState.DENIED == "denied"
    source = inspect.getsource(deeplink)
    assert "LinkState.DENIED" not in source, (
        "denied is the Stage 4 RBAC stub; giving it a producer is a change "
        "this test must see")


def test_an_awr_reference_has_no_document_and_still_has_its_logs():
    assert RESOLVER.logs(AWR_REF).state is LinkState.AVAILABLE
    document = RESOLVER.document(AWR_REF)
    assert document.state is LinkState.MISSING
    assert document.url is None
    assert "no representative document" in document.note


def test_a_reference_with_a_document_resolves_both_actions():
    document = RESOLVER.document(ref())
    assert document.state is LinkState.AVAILABLE
    assert "AZ83bZHL6JT2v6-J7TTb" in document.url


HOSTILE = 'a b&c"d'


@pytest.mark.parametrize("link", [
    lambda: RESOLVER.logs(ref(entity=Entity("database", HOSTILE))),
    lambda: RESOLVER.logs(ref(signature=Signature("oracle_error", HOSTILE))),
    lambda: RESOLVER.document(ref(representative_document_id=HOSTILE)),
    lambda: RESOLVER.trace(run_id=HOSTILE),
    lambda: RESOLVER.run_history(run_id=HOSTILE),
])
def test_no_value_reaches_a_url_unencoded(link):
    url = link().url
    assert HOSTILE not in url
    assert "&" not in url.replace("&_a=", "")
    assert '"' not in url and " " not in url
    assert "%26" in url and "%20" in url and "%22" in url


@pytest.mark.parametrize("template_id, link", [
    ("oracle-error-context-v1",
     lambda: RESOLVER.logs(ref(entity=Entity("database", HOSTILE)))),
    ("kibana-doc-v1",
     lambda: RESOLVER.document(ref(representative_document_id=HOSTILE))),
    ("dbwiki-run-v1", lambda: RESOLVER.run_history(run_id=HOSTILE)),
    ("langfuse-session-v1", lambda: RESOLVER.trace(run_id=HOSTILE)),
])
def test_an_emitted_url_carries_no_parameter_the_template_does_not_spell(
        template_id, link):
    template = deeplink.TEMPLATES[template_id]
    assert set(PARAM_RE.findall(link().url)) \
        == set(PARAM_RE.findall(template.url))


def test_a_window_wider_than_the_cap_is_clamped_and_the_note_says_so():
    wide = ref(window=Window("2026-08-01T00:00:00Z", "2026-08-30T00:00:00Z"))
    link = RESOLVER.logs(wide)
    assert link.state is LinkState.AVAILABLE
    assert link.note
    start = urllib.parse.unquote(
        re.search(r"from:'([^']+)'", link.url).group(1))
    end = urllib.parse.unquote(re.search(r"to:'([^']+)'", link.url).group(1))
    span = (dt.datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
            - dt.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ"))
    assert span == dt.timedelta(hours=MAX_WINDOW_H)


def test_a_window_inside_the_cap_is_left_alone():
    link = RESOLVER.logs(ref())
    assert link.note == ""
    assert urllib.parse.quote(START, safe="") in link.url


LANGFUSE_CONFIG = f"""\
elasticsearch: {{url: "http://127.0.0.1:9200"}}
sources: {{}}
wiki_repo: wiki
state_dir: .state
digest_dir: digests
langfuse: {{enabled: true, host: "{LOOPBACK}"}}
portal:
  links:
    langfuse: {{base: "{LANGFUSE}", project: dbwiki}}
"""


def test_the_trace_uses_the_browser_facing_base_and_never_the_export_host(
        tmp_path):
    cfg = Config(yaml.safe_load(LANGFUSE_CONFIG), tmp_path)
    assert cfg.langfuse["host"] == LOOPBACK
    link = Resolver.from_config(cfg.portal["links"]).trace(run_id="9f2c1a0b")
    assert link.state is LinkState.AVAILABLE
    assert link.url.startswith(LANGFUSE)
    assert "localhost" not in link.url and "127.0.0.1" not in link.url
    assert link.url.endswith("/project/dbwiki/sessions/9f2c1a0b")


def test_a_trace_is_unavailable_without_a_base_or_a_project():
    assert Resolver.from_config({}).trace(run_id="r").note \
        == "no links.langfuse.base is configured"
    partial = Resolver.from_config({"langfuse": {"base": LANGFUSE}})
    assert partial.trace(run_id="r").note \
        == "no links.langfuse.project is configured"


def test_a_trace_is_never_expired_whatever_kibanas_retention_says():
    resolver = Resolver.from_config(
        {**LINKS, "kibana": {**LINKS["kibana"], "retention_days": 1}})
    assert resolver.trace(run_id="r").state is LinkState.AVAILABLE


def test_a_stage_trace_is_the_seeded_id_under_the_browser_facing_base():
    link = RESOLVER.stage_trace(event_id="c70a15098407", task="ingest",
                                db="cdb1")
    assert link.state is LinkState.AVAILABLE
    assert link.url == (f"{LANGFUSE}/project/dbwiki/traces/"
                        f"{observability.trace_id('c70a15098407')}")
    assert link.description == ("The Langfuse trace of stage c70a15098407 "
                                "(ingest cdb1)")
    assert Resolver.from_config({}).stage_trace(event_id="e").note \
        == "no links.langfuse.base is configured"


def test_the_run_history_targets_the_dbwiki_data_view():
    link = RESOLVER.run_history(run_id="9f2c1a0b")
    assert link.state is LinkState.AVAILABLE
    assert "dv-dbwiki" in link.url
    assert link.label == "Open the run's ELK history"


BIND_ONLY_CONFIG = """\
elasticsearch: {url: "http://127.0.0.1:9200"}
sources: {}
wiki_repo: wiki
state_dir: .state
digest_dir: digests
portal: {bind: "127.0.0.1:9000"}
"""


def test_a_portal_block_naming_only_bind_still_answers_every_method(tmp_path):
    """`Config.portal`'s merge is shallow: this block names one key and the
    siblings survive only because `PORTAL_DEFAULTS` supplies them. Every
    `links` sub-key default lives in the resolver instead, so an absent block
    and an empty one answer the same way."""
    cfg = Config(yaml.safe_load(BIND_ONLY_CONFIG), tmp_path)
    assert cfg.portal["links"] == {}
    resolver = Resolver.from_config(cfg.portal.get("links") or {})
    assert resolver == Resolver.from_config({})
    answers = [resolver.logs(ref()), resolver.document(ref()),
               resolver.trace(run_id="r"), resolver.run_history(run_id="r")]
    assert all(isinstance(link, DeepLink) for link in answers)
    assert all(link.url is None for link in answers)
    assert all(link.note for link in answers)


def test_a_reference_read_off_a_page_resolves_the_same_as_one_built_in_code():
    written = render_ref(ref())
    parsed, problems = parse_refs(written)
    assert problems == ()
    assert RESOLVER.logs(parsed[0]) == RESOLVER.logs(ref())
