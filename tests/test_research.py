"""Deterministic half of the research task: which error pages need research,
which sources are due for review, and what may be cited."""

import datetime as dt

import pytest
from fixtures.incident_pages import incident_page

from dbwiki.research import (approved_domains, cited_sources, cited_urls,
                             select_error_candidates, sources_due_review,
                             unapproved_urls, url_domain)


def page(path, fm: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}\n---\n\n{body}")


def source(wiki, slug, *, status="approved", domains="[docs.oracle.com]",
           last_reviewed="2026-07-25", review_after_days=180) -> None:
    page(wiki / "sources" / f"{slug}.md",
         f"type: source\nstatus: {status}\ntier: official\n"
         f"domains: {domains}\nfetchable: true\nadded: 2026-01-01\n"
         f"last_reviewed: {last_reviewed}\nreview_after_days: {review_after_days}",
         f"# {slug}\n")


@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "errors").mkdir(parents=True)
    (w / "sources").mkdir()
    (w / "incidents").mkdir()
    return w


# ---- approved_domains -------------------------------------------------------

def test_approved_domains_skips_deprecated(wiki):
    source(wiki, "oracle-docs")
    source(wiki, "oracle-base", domains="[oracle-base.com]")
    source(wiki, "some-blog", status="deprecated", domains="[blog.example.com]")
    assert approved_domains(wiki) == {"docs.oracle.com", "oracle-base.com"}


def test_approved_domains_empty_without_sources(wiki):
    assert approved_domains(wiki) == set()


# ---- URL extraction + domain matching ---------------------------------------

def test_cited_urls_strips_citation_punctuation():
    text = ("Cause per the docs (source: sources/oracle-docs; "
            "url: https://docs.oracle.com/en/error/ORA-12543.html; "
            "accessed: 2026-07-25) and [a link](https://oracle-base.com/dg).")
    assert cited_urls(text) == {"https://docs.oracle.com/en/error/ORA-12543.html",
                                "https://oracle-base.com/dg"}
    assert cited_sources(text) == {"oracle-docs"}


def test_subdomain_of_approved_domain_is_approved():
    domains = {"oracle.com"}
    assert url_domain("https://Blogs.Oracle.com/post") == "blogs.oracle.com"
    text = "see https://blogs.oracle.com/post and https://docs.oracle.com/ok"
    assert unapproved_urls(text, domains) == set()


def test_sibling_and_lookalike_domains_stay_unapproved():
    domains = {"docs.oracle.com"}
    text = ("see https://blogs.oracle.com/post "
            "and https://docs.oracle.com.evil.net/x "
            "and https://notdocs.oracle.com/y "
            "and https://docs.oracle.com/ok")
    assert unapproved_urls(text, domains) == {
        "https://blogs.oracle.com/post",
        "https://docs.oracle.com.evil.net/x",
        "https://notdocs.oracle.com/y",
    }


def test_domain_match_ignores_port_and_case():
    assert unapproved_urls("https://DOCS.oracle.com:443/x", {"docs.oracle.com"}) == set()


# ---- sources_due_review -----------------------------------------------------

def test_sources_due_review_uses_interval(wiki):
    source(wiki, "fresh", last_reviewed="2026-07-01", review_after_days=180)
    source(wiki, "stale", last_reviewed="2026-01-01", review_after_days=30)
    source(wiki, "never", last_reviewed="", review_after_days=180)
    source(wiki, "dead", status="deprecated", last_reviewed="2020-01-01")
    due = [p.stem for p in sources_due_review(wiki, dt.date(2026, 7, 25))]
    assert due == ["never", "stale"]


# ---- select_error_candidates ------------------------------------------------

def test_selects_pages_without_researched_date(wiki):
    page(wiki / "errors" / "ORA-600.md",
         "type: error-class\nupdated: 2026-07-10T00:00:00Z")
    page(wiki / "errors" / "ORA-1110.md",
         "type: error-class\nresearched: 2026-07-20\nupdated: 2026-07-21T00:00:00Z")
    assert [p.stem for p in select_error_candidates(wiki, 5)] == ["ORA-600"]


def test_stale_when_cited_source_reviewed_later(wiki):
    source(wiki, "oracle-docs", last_reviewed="2026-07-24")
    page(wiki / "errors" / "ORA-12543.md",
         "type: error-class\nresearched: 2026-07-01\nupdated: 2026-07-01T00:00:00Z",
         "## Reference\n\nX (source: sources/oracle-docs; url: "
         "https://docs.oracle.com/a; accessed: 2026-07-01)\n")
    assert [p.stem for p in select_error_candidates(wiki, 5)] == ["ORA-12543"]

    source(wiki, "oracle-docs", last_reviewed="2026-06-01")
    assert select_error_candidates(wiki, 5) == []


def test_stale_when_cited_source_deprecated(wiki):
    source(wiki, "oracle-docs", status="deprecated", last_reviewed="2026-06-01")
    page(wiki / "errors" / "ORA-12543.md",
         "type: error-class\nresearched: 2026-07-01\nupdated: 2026-07-01T00:00:00Z",
         "## Reference\n\nX (source: sources/oracle-docs; url: "
         "https://docs.oracle.com/a; accessed: 2026-07-01)\n")
    assert [p.stem for p in select_error_candidates(wiki, 5)] == ["ORA-12543"]


def test_open_incident_pages_come_first_then_recency(wiki):
    for code, updated in (("ORA-1", "2026-07-01T00:00:00Z"),
                          ("ORA-2", "2026-07-20T00:00:00Z"),
                          ("ORA-3", "2026-07-10T00:00:00Z")):
        page(wiki / "errors" / f"{code}.md", f"type: error-class\nupdated: {updated}")
    (wiki / "incidents" / "2026-07-01-cdb1-x.md").write_text(
        incident_page("cdb1", "x", error_codes=("ORA-1",)))
    (wiki / "incidents" / "2026-07-02-cdb1-y.md").write_text(
        incident_page("cdb1", "y", status="resolved", error_codes=("ORA-3",)))
    assert [p.stem for p in select_error_candidates(wiki, 5)] == \
        ["ORA-1", "ORA-2", "ORA-3"]


def test_open_incident_count_outranks_recency(wiki):
    for code, updated in (("ORA-1", "2026-07-01T00:00:00Z"),
                          ("ORA-2", "2026-07-20T00:00:00Z"),
                          ("ORA-3", "2026-07-10T00:00:00Z")):
        page(wiki / "errors" / f"{code}.md", f"type: error-class\nupdated: {updated}")
    (wiki / "incidents" / "2026-07-01-cdb1-a.md").write_text(
        incident_page("cdb1", "a", error_codes=("ORA-1",)))
    (wiki / "incidents" / "2026-07-02-cdb1-b.md").write_text(
        incident_page("cdb1", "b", error_codes=("ORA-1",)))
    (wiki / "incidents" / "2026-07-03-cdb1-c.md").write_text(
        incident_page("cdb1", "c", error_codes=("ORA-2",)))
    assert [p.stem for p in select_error_candidates(wiki, 5)] == \
        ["ORA-1", "ORA-2", "ORA-3"]


def test_limit_caps_the_workload(wiki):
    for code in ("ORA-1", "ORA-2", "ORA-3"):
        page(wiki / "errors" / f"{code}.md",
             "type: error-class\nupdated: 2026-07-01T00:00:00Z")
    assert len(select_error_candidates(wiki, 2)) == 2


def test_web_flag_grants_search_tools():
    from dbwiki.harness import _claude_cmd, _codex_cmd
    from pathlib import Path
    wiki = Path("/tmp/w")
    claude = _claude_cmd("p", wiki, None, web=True)
    tools = claude[claude.index("--allowedTools") + 1]
    assert "WebSearch" in tools and "WebFetch" in tools
    assert "WebSearch" not in _claude_cmd("p", wiki, None)[
        _claude_cmd("p", wiki, None).index("--allowedTools") + 1]
    assert "--search" in _codex_cmd("p", wiki, None, web=True)
    assert "--search" not in _codex_cmd("p", wiki, None)
