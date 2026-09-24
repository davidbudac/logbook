"""Deterministic wiki lint (provenance v1): one clean wiki, then one
deliberately broken page per rule id. No LLM, no network — fixture wikis
only, since wiki/ is a separate repo."""

import pytest
from fixtures.incident_pages import incident_page

from dbwiki import prompts
from dbwiki.evidence_ref import (Entity, EvidenceRef, Signature, Window,
                                 render_ref)
from dbwiki.incidents import (ActionRecord, ErrorAbsent, MonitoringWindow,
                              Status, append_action, set_status)
from dbwiki.lint import PROVENANCE_SCHEMA_VERSION, RULES, blocking, lint_wiki

SOURCE_PAGE = ("---\ntype: source\nstatus: approved\ntier: official\n"
               "domains: [docs.oracle.com]\nfetchable: true\nadded: 2026-01-01\n"
               "last_reviewed: 2026-07-25\nreview_after_days: 180\n---\n\n"
               "# Oracle docs\n")
CITATION = ("(source: sources/oracle-docs; "
            "url: https://docs.oracle.com/en/ORA-12543; accessed: 2026-07-25)")

MOJIBAKE = b"---\ntype: concept\n---\n\n# \xff\xfe not utf-8\n"

INCIDENT_REL = "incidents/2026-07-27-cdb1-listener-flap.md"
UPDATED = "2026-07-27T18:00:00Z"
WINDOW = MonitoringWindow(ErrorAbsent("ORA-12543"),
                          "2026-07-27T18:00:00Z", "2026-07-28T18:00:00Z")
ACTION = ActionRecord(at="2026-07-27T18:00:00Z", kind="start-monitoring",
                      actor="dba@example.com", intent="watch for a recurrence",
                      summary="restarted the listener on host1",
                      status_after=Status.MONITORING, window=WINDOW,
                      notes="Config reload was not enough.\n\nFull restart.")


def write(wiki, rel, text):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def write_raw(wiki, rel, data: bytes):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def error_page(body: str = "", fm: str = "") -> str:
    return (f"---\ntype: error-class\nadded: 2026-07-01\n"
            f"updated: 2026-07-10T00:00:00Z\n{fm}---\n\n# ORA-12543\n\n{body}")


@pytest.fixture
def wiki(tmp_path):
    """A clean wiki: index links the error page, which cites an approved
    source and an existing digest."""
    w = tmp_path / "wiki"
    write(w, "index.md", "# Index\n\n- [[errors/ORA-12543]]\n")
    write(w, "log.md", "# log\n")
    write(w, "sources/oracle-docs.md", SOURCE_PAGE)
    write(w, "digests/cdb1/2026-07-10.json", "{}")
    write(w, "digests/cdb1/2026-07-10.md", "# digest\n")
    write(w, "errors/ORA-12543.md", error_page(
        "## Occurrences\n\nseen on cdb1 (digests/cdb1/2026-07-10.md)\n\n"
        f"## Reference\n\nHost unreachable. {CITATION}\n"))
    return w


def rules_for(findings, rel=None):
    return sorted(f.rule for f in findings if rel is None or f.file == rel)


# ---- clean baseline ----------------------------------------------------------

def test_clean_wiki_has_no_findings(wiki):
    assert lint_wiki(wiki) == []
    assert PROVENANCE_SCHEMA_VERSION == 1


def test_missing_wiki_root_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="wiki root does not exist"):
        lint_wiki(tmp_path / "nope")


def test_findings_are_json_safe(wiki):
    write(wiki, "errors/ORA-1.md", error_page("[[errors/nowhere]]\n"))
    d = lint_wiki(wiki)[0].to_dict()
    assert set(d) == {"file", "rule", "severity", "message", "hint", "suppressed"}
    assert d["severity"] in ("error", "warning") and d["suppressed"] is False
    assert all(isinstance(v, (str, bool)) for v in d.values())


# ---- frontmatter-malformed --------------------------------------------------

def test_unterminated_frontmatter(wiki):
    write(wiki, "errors/ORA-1.md", "---\ntype: error-class\n\n# ORA-1\n")
    assert "frontmatter-malformed" in rules_for(lint_wiki(wiki), "errors/ORA-1.md")


def test_invalid_yaml_frontmatter(wiki):
    write(wiki, "errors/ORA-1.md", "---\ntype: [unclosed\n---\n\n# ORA-1\n")
    assert "frontmatter-malformed" in rules_for(lint_wiki(wiki), "errors/ORA-1.md")


def test_unknown_page_type(wiki):
    write(wiki, "errors/ORA-1.md", "---\ntype: mystery\n---\n\n# ORA-1\n")
    f = [f for f in lint_wiki(wiki) if f.rule == "frontmatter-malformed"]
    assert f and f[0].file == "errors/ORA-1.md" and "mystery" in f[0].message


def test_page_without_frontmatter_is_allowed(wiki):
    write(wiki, "index.md", "# Index\n\n- [[errors/ORA-12543]]\n- [[concepts/x]]\n")
    write(wiki, "concepts/x.md", "# plain page, no frontmatter\n")
    assert lint_wiki(wiki) == []


# ---- frontmatter-contradictory ----------------------------------------------

def test_researched_before_added(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(fm="researched: 2026-06-01\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "frontmatter-contradictory"]
    assert f and "researched" in f[0].message and f[0].severity == "error"


def test_researched_after_updated_is_not_a_contradiction(wiki):
    """A page is routinely updated after being researched — research.py's own
    staleness model depends on `updated` moving forward independently."""
    write(wiki, "errors/ORA-12543.md", error_page(fm="researched: 2026-07-20\n"))
    assert lint_wiki(wiki) == []


def test_approved_source_without_domains(wiki):
    write(wiki, "sources/oracle-docs.md",
          SOURCE_PAGE.replace("domains: [docs.oracle.com]\n", ""))
    rules = rules_for(lint_wiki(wiki), "sources/oracle-docs.md")
    assert "frontmatter-contradictory" in rules


def test_monitoring_without_a_window_is_reported(wiki):
    write(wiki, INCIDENT_REL, incident_page("cdb1", "a", status="monitoring"))
    f = [f for f in lint_wiki(wiki) if f.rule == "incident-status-invalid"]
    assert len(f) == 1 and f[0].file == INCIDENT_REL
    assert "no `monitoring:` key" in f[0].message and f[0].severity == "error"


def test_monitoring_with_an_unreadable_window_is_reported_as_unreadable(wiki):
    page = set_status(incident_page("cdb1", "a"), Status.MONITORING,
                      updated=UPDATED, monitoring=WINDOW)
    write(wiki, INCIDENT_REL,
          page.replace(WINDOW.to_frontmatter(), "{kind: 'error_absent'}"))
    f = [f for f in lint_wiki(wiki) if f.rule == "incident-status-invalid"]
    assert len(f) == 1 and "unreadable" in f[0].message


def test_monitoring_with_a_readable_window_is_accepted(wiki):
    write(wiki, INCIDENT_REL,
          set_status(incident_page("cdb1", "a"), Status.MONITORING,
                     updated=UPDATED, monitoring=WINDOW))
    assert rules_for(lint_wiki(wiki), INCIDENT_REL) == ["page-orphaned"]


def test_action_malformed_fires_once_per_bad_section_and_names_the_heading(wiki):
    write(wiki, INCIDENT_REL, incident_page("cdb1", "a", body=(
        "## Action 2026-07-27T18:00:00Z\n\nWhat I did, in prose only.\n\n"
        "## Action 2026-07-27T19:00:00Z\n\n"
        "```yaml\nkind: record-action\n```\n")))
    f = [f for f in lint_wiki(wiki) if f.rule == "action-malformed"]
    assert len(f) == 2 and f[0].file == f[1].file == INCIDENT_REL
    assert f[0].message.startswith("## Action 2026-07-27T18:00:00Z: ")
    assert f[1].message.startswith("## Action 2026-07-27T19:00:00Z: ")
    assert f[0].severity == f[1].severity == "error"


def test_a_page_whose_actions_all_parse_reports_no_action_malformed(wiki):
    page = set_status(incident_page("cdb1", "a"), Status.MONITORING,
                      updated=UPDATED, monitoring=WINDOW)
    write(wiki, INCIDENT_REL, append_action(page, ACTION))
    assert rules_for(lint_wiki(wiki), INCIDENT_REL) == ["page-orphaned"]


GOOD_REF = render_ref(EvidenceRef(
    schema_version=1, kind="elastic_filter",
    template_id="oracle-error-context-v1", environment="production",
    data_view="oracle-logs", entity=Entity("database", "cdb1"),
    signature=Signature("oracle_error", "ORA-12543"),
    window=Window("2026-07-27T18:00:00Z", "2026-07-27T19:00:00Z")))

BAD_REF = GOOD_REF.replace("kind: elastic_filter", "kind: sql_query")

ERROR_REL = "errors/ORA-12543.md"


def test_evidence_ref_malformed_fires_once_per_bad_block(wiki):
    write(wiki, INCIDENT_REL,
          incident_page("cdb1", "a", body=f"{GOOD_REF}\n{BAD_REF}"))
    f = [f for f in lint_wiki(wiki) if f.rule == "evidence-ref-malformed"]
    assert len(f) == 1 and f[0].file == INCIDENT_REL
    assert f[0].message.startswith("evidence_ref block 2: kind:")
    assert f[0].severity == "error"


def test_the_rule_runs_on_every_curated_page_and_not_incidents_only(wiki):
    """The document places these blocks on incident, error, action and
    resolution pages, so the rule cannot be an incident-only arm."""
    write(wiki, ERROR_REL,
          f"---\ntype: error-class\n---\n\n# ORA-12543\n\n{BAD_REF}")
    f = [f for f in lint_wiki(wiki) if f.rule == "evidence-ref-malformed"]
    assert [finding.file for finding in f] == [ERROR_REL]


def test_a_page_whose_references_parse_reports_nothing(wiki):
    write(wiki, INCIDENT_REL, incident_page("cdb1", "a", body=GOOD_REF))
    assert rules_for(lint_wiki(wiki), INCIDENT_REL) == ["page-orphaned"]


# ---- wikilink-broken --------------------------------------------------------

def test_broken_wikilink(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("see [[errors/ORA-99999]]\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "wikilink-broken"]
    assert len(f) == 1
    assert f[0].file == "errors/ORA-12543.md" and "ORA-99999" in f[0].message


def test_wikilink_resolves_by_basename_alias_and_anchor(wiki):
    write(wiki, "concepts/dg-pair.md", "---\ntype: concept\n---\n\n# pair\n")
    write(wiki, "errors/ORA-12543.md", error_page(
        "[[dg-pair]] and [[concepts/dg-pair|the pair]] and [[dg-pair#lag]]\n"))
    assert [f.rule for f in lint_wiki(wiki)] == []


def test_ambiguous_basename_does_not_resolve(wiki):
    write(wiki, "concepts/dupe.md", "---\ntype: concept\n---\n\n# a\n")
    write(wiki, "hosts/dupe.md", "---\ntype: host\n---\n\n# b\n")
    write(wiki, "errors/ORA-12543.md", error_page("[[dupe]]\n"))
    assert "wikilink-broken" in rules_for(lint_wiki(wiki), "errors/ORA-12543.md")


# ---- page-orphaned ----------------------------------------------------------

def test_orphan_page_is_a_warning(wiki):
    write(wiki, "concepts/lonely.md", "---\ntype: concept\n---\n\n# lonely\n")
    f = [f for f in lint_wiki(wiki) if f.rule == "page-orphaned"]
    assert len(f) == 1 and f[0].file == "concepts/lonely.md"
    assert f[0].severity == "warning" and blocking(f) == []


def test_inbound_link_or_index_reachability_clears_orphanhood(wiki):
    # reachable transitively from index.md via the error page
    write(wiki, "errors/ORA-12543.md", error_page("[[concepts/deep]]\n"))
    write(wiki, "concepts/deep.md", "---\ntype: concept\n---\n\n# deep\n")
    assert [f.rule for f in lint_wiki(wiki)] == []
    # inbound link from an unreachable page is enough on its own
    write(wiki, "errors/ORA-12543.md", error_page(""))
    write(wiki, "hosts/h1.md", "---\ntype: host\n---\n\n[[concepts/deep]]\n")
    assert rules_for(lint_wiki(wiki), "concepts/deep.md") == []
    assert rules_for(lint_wiki(wiki), "hosts/h1.md") == ["page-orphaned"]


def test_reports_sources_digests_and_entry_points_are_exempt(wiki):
    write(wiki, "reports/2026-07-10.md", "---\ntype: report\n---\n\n# report\n")
    assert [f.rule for f in lint_wiki(wiki)] == []


def test_self_link_does_not_clear_orphanhood(wiki):
    write(wiki, "concepts/lonely.md",
          "---\ntype: concept\n---\n\n[[concepts/lonely]]\n")
    assert rules_for(lint_wiki(wiki), "concepts/lonely.md") == ["page-orphaned"]


# ---- digest-missing ---------------------------------------------------------

def test_missing_digest_path(wiki):
    write(wiki, "errors/ORA-12543.md",
          error_page("evidence: digests/cdb1/2026-07-99.md\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "digest-missing"]
    assert len(f) == 1 and "2026-07-99.md" in f[0].message
    assert f[0].severity == "error"


def test_missing_digest_wikilink_is_not_a_broken_link(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("[[digests/cdb1/2026-07-99]]\n"))
    assert rules_for(lint_wiki(wiki), "errors/ORA-12543.md") == ["digest-missing"]


def test_existing_digest_json_reference_passes(wiki):
    write(wiki, "errors/ORA-12543.md",
          error_page("machine twin: digests/cdb1/2026-07-10.json\n"))
    assert lint_wiki(wiki) == []


# ---- citation-malformed -----------------------------------------------------

def test_reference_url_without_citation_shape(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\nCause: see https://docs.oracle.com/en/ORA-12543\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "without a well-formed citation" in f[0].message


def test_citation_to_unknown_source_page(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\nCause. (source: sources/asktom; "
        "url: https://docs.oracle.com/x; accessed: 2026-07-25)\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "not a source page" in f[0].message


def test_citation_with_invalid_accessed_date(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\nCause. (source: sources/oracle-docs; "
        "url: https://docs.oracle.com/x; accessed: yesterday)\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "invalid accessed date" in f[0].message


def test_citation_off_approved_domains(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\nCause. (source: sources/oracle-docs; "
        "url: https://blogs.oracle.com/hot-take; accessed: 2026-07-25)\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "blogs.oracle.com" in f[0].message


def test_deprecated_source_domain_is_unapproved(wiki):
    write(wiki, "sources/oracle-docs.md",
          SOURCE_PAGE.replace("status: approved", "status: deprecated"))
    assert "citation-malformed" in rules_for(lint_wiki(wiki), "errors/ORA-12543.md")


def test_citation_wrapped_across_lines_is_judged_whole(wiki):
    """Agents wrap prose at ~80 columns; a citation split across physical
    lines inside one bullet/paragraph is still one logical citation."""
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\n"
        "- Cause: listener not running (source: sources/oracle-docs;\n"
        "  url: <https://docs.oracle.com/en/error-help/db/ora-12543/>;\n"
        "  accessed: 2026-07-28)\n"))
    assert [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"] == []


def test_wrapped_citation_off_approved_domains_is_still_caught(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\n"
        "Cause. (source: sources/oracle-docs;\n"
        "url: https://evil.example.com/x; accessed: 2026-07-28)\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "evil.example.com" in f[0].message


def test_blank_line_separates_reference_items(wiki):
    """A bare URL paragraph does not borrow the citation of the previous
    item across a blank line."""
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\n"
        "Cause. (source: sources/oracle-docs; "
        "url: https://docs.oracle.com/x; accessed: 2026-07-28)\n\n"
        "Also see https://docs.oracle.com/en/other\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "without a well-formed citation" in f[0].message


def test_new_bullet_does_not_continue_the_previous_item(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\n"
        "- Cause. (source: sources/oracle-docs; "
        "url: https://docs.oracle.com/x; accessed: 2026-07-28)\n"
        "- Uncited claim about https://docs.oracle.com/en/other\n"))
    f = [f for f in lint_wiki(wiki) if f.rule == "citation-malformed"]
    assert len(f) == 1 and "Uncited claim" in f[0].message


def test_prose_without_external_claim_passes(wiki):
    write(wiki, "errors/ORA-12543.md", error_page(
        "## Reference\n\nMeaning: the listener could not reach the host.\n"))
    assert lint_wiki(wiki) == []


def test_urls_outside_the_reference_section_are_v2(wiki):
    """External claims in observed sections stay with the agent lint (v2)."""
    write(wiki, "errors/ORA-12543.md",
          error_page("## Occurrences\n\nper https://blogs.oracle.com/x\n"))
    assert lint_wiki(wiki) == []


def test_an_undecodable_page_costs_only_itself_a_finding(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("[[errors/ORA-99999]]\n"))
    write(wiki, "concepts/lonely.md", "---\ntype: concept\n---\n\n# lonely\n")
    before = lint_wiki(wiki)
    write_raw(wiki, "concepts/mojibake.md", MOJIBAKE)
    findings = lint_wiki(wiki)
    assert rules_for(findings, "concepts/mojibake.md") == ["encoding-invalid"]
    assert [f for f in findings if f.file != "concepts/mojibake.md"] == before
    bad = next(f for f in findings if f.file == "concepts/mojibake.md")
    assert bad.severity == "error" and "not valid UTF-8" in bad.message


def test_a_wikilink_to_an_undecodable_page_still_resolves(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("[[concepts/mojibake]]\n"))
    write_raw(wiki, "concepts/mojibake.md", MOJIBAKE)
    assert rules_for(lint_wiki(wiki)) == ["encoding-invalid"]


def test_only_paths_without_the_undecodable_page_does_not_report_it(wiki):
    write_raw(wiki, "concepts/mojibake.md", MOJIBAKE)
    assert lint_wiki(wiki, only_paths=["index.md"]) == []
    assert rules_for(lint_wiki(wiki, only_paths=["concepts/mojibake.md"])) == \
        ["encoding-invalid"]


# ---- exceptions file --------------------------------------------------------

def test_exception_suppresses_one_rule(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("[[errors/ORA-99999]]\n"))
    write(wiki, ".lint-exceptions",
          "# grandfathered before provenance v1\n"
          "errors/ORA-12543.md\twikilink-broken\n")
    findings = lint_wiki(wiki)
    assert [f.rule for f in findings] == ["wikilink-broken"]
    assert findings[0].suppressed is True and blocking(findings) == []


def test_exception_without_rule_suppresses_all(wiki):
    write(wiki, "errors/ORA-12543.md",
          "---\ntype: mystery\n---\n\n[[errors/ORA-99999]]\n")
    write(wiki, ".lint-exceptions", "errors/ORA-12543.md\n")
    findings = lint_wiki(wiki)
    assert len(findings) >= 2 and all(f.suppressed for f in findings)
    assert blocking(findings) == []


def test_exception_does_not_leak_to_other_rules_or_pages(wiki):
    write(wiki, "errors/ORA-12543.md",
          error_page("[[errors/ORA-99999]]\n", fm="researched: 2026-06-01\n"))
    write(wiki, ".lint-exceptions", "errors/ORA-12543.md\twikilink-broken\n")
    assert [f.rule for f in blocking(lint_wiki(wiki))] == \
        ["frontmatter-contradictory"]


# ---- only_paths -------------------------------------------------------------

def test_only_paths_restricts_reporting_not_resolution(wiki):
    write(wiki, "errors/ORA-12543.md", error_page("[[errors/ORA-99999]]\n"))
    write(wiki, "concepts/lonely.md", "---\ntype: concept\n---\n\n# lonely\n")
    assert rules_for(lint_wiki(wiki, only_paths=["errors/ORA-12543.md"])) == \
        ["wikilink-broken"]
    assert rules_for(lint_wiki(wiki, only_paths=["concepts/lonely.md"])) == \
        ["page-orphaned"]
    assert lint_wiki(wiki, only_paths=["log.md"]) == []
    # a page linked only from a path outside only_paths is still not an orphan
    assert lint_wiki(wiki, only_paths=["sources/oracle-docs.md"]) == []


def test_only_paths_ignores_non_page_entries(wiki):
    assert lint_wiki(wiki, only_paths=["digests/cdb1/2026-07-10.json"]) == []


# ---- rule table -------------------------------------------------------------

def test_every_rule_has_a_severity_and_hint():
    assert set(RULES) == {"frontmatter-malformed", "frontmatter-contradictory",
                          "incident-status-invalid", "action-malformed",
                          "evidence-ref-malformed",
                          "wikilink-broken", "page-orphaned", "digest-missing",
                          "citation-malformed", "encoding-invalid",
                          "generated-section-drift"}
    assert all(sev in ("error", "warning") and hint
               for sev, hint in RULES.values())


# ---- generated AGENTS.md sections -------------------------------------------

def _drift(wiki, **kw):
    return [f for f in lint_wiki(wiki, **kw)
            if f.rule == "generated-section-drift"]


def test_agents_md_without_markers_has_no_drift_finding(wiki):
    write(wiki, "AGENTS.md", "# Agents\n\nNo generated section yet.\n")
    assert _drift(wiki) == []


def test_agents_md_in_step_with_its_prompt_file_is_clean(wiki):
    section = prompts.section("shared/evidence-rules")
    write(wiki, "AGENTS.md", f"# Agents\n\n{section}\n\n## After\n")
    assert _drift(wiki) == []


def test_drifted_generated_section_is_a_non_blocking_warning(wiki):
    section = prompts.section("shared/evidence-rules")
    body = prompts.load("shared/evidence-rules")
    edited = section.replace(body, body + "\nA hand edit.")
    write(wiki, "AGENTS.md", f"# Agents\n\n{edited}\n")
    found = _drift(wiki)
    assert [(f.file, f.severity) for f in found] == [("AGENTS.md", "warning")]
    assert "shared/evidence-rules" in found[0].message
    assert "dbwiki.prompts sync" in found[0].hint
    assert blocking(found) == []
    assert _drift(wiki, only_paths=["index.md"]) == []
    assert len(_drift(wiki, only_paths=["AGENTS.md"])) == 1


def test_unclosed_generated_marker_is_drift(wiki):
    write(wiki, "AGENTS.md",
          "# Agents\n\n<!-- BEGIN generated: shared/evidence-rules -->\nx\n")
    assert len(_drift(wiki)) == 1


def test_only_the_root_agents_md_is_checked(wiki):
    write(wiki, "concepts/AGENTS.md",
          "<!-- BEGIN generated: shared/evidence-rules -->\nx\n"
          "<!-- END generated -->\n")
    assert _drift(wiki) == []
