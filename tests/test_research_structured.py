"""Structured research: deterministic URL/fetch, the cheap-model
cause/action contract and its retry, and the deterministic apply_research
writer — mirrors tests/test_structured.py's shape for the ingest/report
writers. Never hits the network: requests.get is always mocked."""

import json

import pytest
from fixtures import make_config

from dbwiki import research_structured as rs
from dbwiki.harness import HarnessError
from dbwiki.lint import blocking, lint_wiki
from dbwiki.research import unapproved_urls
from dbwiki.structured import ProposalError

TODAY = __import__("datetime").date(2026, 7, 29)

SOURCE_PAGE = ("---\ntype: source\nstatus: approved\ntier: official\n"
              "domains: [docs.oracle.com]\nfetchable: true\nadded: 2026-01-01\n"
              "last_reviewed: 2026-07-01\nreview_after_days: 180\n---\n\n"
              "# Oracle documentation\n")

ERROR_PAGE = ("---\ntype: error-class\nupdated: 2026-07-10T00:00:00Z\n---\n\n"
             "# ORA-16607\n\n## Occurrences\n\n"
             "| day | db | note | evidence |\n|---|---|---|---|\n"
             "| 2026-07-10 | cdb1 | seen | digests/cdb1/2026-07-10.md |\n")


# ---- docs_url ----------------------------------------------------------------

@pytest.mark.parametrize("code,url", [
    ("ORA-16607", "https://docs.oracle.com/en/error-help/db/ora-16607/"),
    ("TNS-12543", "https://docs.oracle.com/en/error-help/db/tns-12543/"),
])
def test_docs_url_maps_code_to_the_oracle_error_help_page(code, url):
    assert rs.docs_url(code) == url


# ---- fetchable_source ----------------------------------------------------------

@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "sources").mkdir(parents=True)
    (w / "errors").mkdir()
    return w


def test_fetchable_source_picks_approved_fetchable_oracle_docs(wiki):
    (wiki / "sources" / "oracle-docs.md").write_text(SOURCE_PAGE)
    slug, domains = rs.fetchable_source(wiki)
    assert slug == "oracle-docs"
    assert "docs.oracle.com" in domains


def test_fetchable_source_none_without_a_qualifying_source(wiki):
    assert rs.fetchable_source(wiki) is None
    # not fetchable
    (wiki / "sources" / "asktom.md").write_text(
        SOURCE_PAGE.replace("fetchable: true", "fetchable: false"))
    assert rs.fetchable_source(wiki) is None


def test_fetchable_source_ignores_non_approved_or_wrong_domain(wiki):
    (wiki / "sources" / "blog.md").write_text(
        SOURCE_PAGE.replace("docs.oracle.com", "blog.example.com"))
    (wiki / "sources" / "deprecated.md").write_text(
        SOURCE_PAGE.replace("status: approved", "status: deprecated"))
    assert rs.fetchable_source(wiki) is None


# ---- fetch_page ------------------------------------------------------------

class _Resp:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def test_fetch_page_extracts_readable_text(monkeypatch):
    html = ("<html><head><style>.x{}</style></head><body>"
           "<nav>skip nav</nav><header>skip header</header>"
           "<h1>ORA-16607</h1><p>Cause: something failed.</p>"
           "<script>var x = 1;</script>"
           "<footer>skip footer</footer></body></html>")

    def fake_get(url, timeout=None, headers=None):
        assert "User-Agent" in headers
        return _Resp(200, html)

    monkeypatch.setattr(rs.requests, "get", fake_get)
    text = rs.fetch_page("https://docs.oracle.com/en/error-help/db/ora-16607/")
    assert "ORA-16607" in text
    assert "Cause: something failed." in text
    for skipped in ("skip nav", "skip header", "skip footer", "var x = 1"):
        assert skipped not in text


def test_fetch_page_caps_length(monkeypatch):
    big = "<p>" + ("word " * 10000) + "</p>"
    monkeypatch.setattr(rs.requests, "get", lambda *a, **k: _Resp(200, big))
    text = rs.fetch_page("https://docs.oracle.com/x")
    assert len(text) <= rs.MAX_PAGE_CHARS


def test_fetch_page_none_on_404(monkeypatch):
    monkeypatch.setattr(rs.requests, "get", lambda *a, **k: _Resp(404, "nope"))
    assert rs.fetch_page("https://docs.oracle.com/missing") is None


def test_fetch_page_none_on_request_exception(monkeypatch):
    import requests

    def boom(*a, **k):
        raise requests.RequestException("connection refused")

    monkeypatch.setattr(rs.requests, "get", boom)
    assert rs.fetch_page("https://docs.oracle.com/x") is None


# ---- parse_research_proposal / propose_research ---------------------------

def test_parse_research_proposal_requires_cause_and_action():
    got = rs.parse_research_proposal(
        json.dumps({"cause": " bad checksum ", "action": "- restart\n"}))
    assert got == {"cause": "bad checksum", "action": "- restart"}


@pytest.mark.parametrize("bad", [
    json.dumps({"action": "a"}),
    json.dumps({"cause": "c"}),
    json.dumps({"cause": "", "action": "a"}),
    json.dumps({"cause": 1, "action": "a"}),
    "not json",
    "[]",
])
def test_parse_research_proposal_rejects_bad_input(bad):
    with pytest.raises(ProposalError):
        rs.parse_research_proposal(bad)


def _calls(monkeypatch, *answers):
    from dbwiki import structured
    seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append((prompt, escalate))
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.4,
                             exit_code=0, timed_out=False, usage="unknown")
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(structured, "generate", _gen)
    return seen


def test_propose_research_accepts_first_answer_without_retry(monkeypatch):
    seen = _calls(monkeypatch, json.dumps({"cause": "c", "action": "a"}))
    tele = {}
    got = rs.propose_research("PROMPT", make_config(), telemetry=tele)
    assert got == {"cause": "c", "action": "a"}
    assert len(seen) == 1
    assert seen[0][1] is False  # never escalates
    assert tele["adapter"] == "pi"


def test_propose_research_retries_once_then_succeeds(monkeypatch):
    seen = _calls(monkeypatch, "not json",
                 json.dumps({"cause": "c", "action": "a"}))
    got = rs.propose_research("PROMPT", make_config())
    assert got["cause"] == "c"
    assert len(seen) == 2
    assert "rejected" in seen[1][0]


def test_propose_research_gives_up_after_second_bad_answer(monkeypatch):
    _calls(monkeypatch, "nope", "still nope")
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        rs.propose_research("PROMPT", make_config())


# ---- apply_research ------------------------------------------------------------

@pytest.fixture
def research_wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "errors").mkdir(parents=True)
    (w / "errors" / "ORA-16607.md").write_text(ERROR_PAGE)
    (w / "sources").mkdir()
    (w / "sources" / "oracle-docs.md").write_text(SOURCE_PAGE)
    (w / "digests" / "cdb1").mkdir(parents=True)
    (w / "digests" / "cdb1" / "2026-07-10.md").write_text("# digest\n")
    return w


def test_apply_research_sets_researched_and_reference(research_wiki):
    proposal = {"cause": "The checksum in the header does not match.",
               "action": "- Check the file system\n- Restore from backup"}
    rs.apply_research(research_wiki, "errors/ORA-16607.md", proposal,
                      "https://docs.oracle.com/en/error-help/db/ora-16607/",
                      "oracle-docs", TODAY)
    text = (research_wiki / "errors" / "ORA-16607.md").read_text()

    assert "researched: 2026-07-29" in text
    assert "## Reference" in text
    assert "**Cause:**" in text and "**Action:**" in text
    assert ("(source: sources/oracle-docs; url: "
           "https://docs.oracle.com/en/error-help/db/ora-16607/; "
           "accessed: 2026-07-29)") in text

    # Occurrences untouched
    assert "| 2026-07-10 | cdb1 | seen | digests/cdb1/2026-07-10.md |" in text

    # passes the citation allowlist and the deterministic provenance lint
    assert unapproved_urls(text, {"docs.oracle.com"}) == set()
    lint_findings = lint_wiki(research_wiki, only_paths=["errors/ORA-16607.md"])
    assert blocking(lint_findings) == []


def test_apply_research_log_line_appended(research_wiki):
    (research_wiki / "log.md").write_text("# log\n")
    proposal = {"cause": "cause text", "action": "action text"}
    rs.apply_research(research_wiki, "errors/ORA-16607.md", proposal,
                      "https://docs.oracle.com/en/error-help/db/ora-16607/",
                      "oracle-docs", TODAY)
    log = (research_wiki / "log.md").read_text()
    assert "research (structured)" in log
    assert "errors/ORA-16607.md" in log


def test_apply_research_is_idempotent_by_replacement(research_wiki):
    proposal = {"cause": "first cause", "action": "first action"}
    rs.apply_research(research_wiki, "errors/ORA-16607.md", proposal,
                      "https://docs.oracle.com/en/error-help/db/ora-16607/",
                      "oracle-docs", TODAY)
    first = (research_wiki / "errors" / "ORA-16607.md").read_text()
    assert first.count("## Reference") == 1

    proposal2 = {"cause": "revised cause", "action": "revised action"}
    rs.apply_research(research_wiki, "errors/ORA-16607.md", proposal2,
                      "https://docs.oracle.com/en/error-help/db/ora-16607/",
                      "oracle-docs", TODAY)
    second = (research_wiki / "errors" / "ORA-16607.md").read_text()
    assert second.count("## Reference") == 1
    assert "revised cause" in second and "first cause" not in second
    assert "| 2026-07-10 | cdb1 | seen | digests/cdb1/2026-07-10.md |" in second


def test_apply_research_flattens_stray_headings_and_links(research_wiki):
    """Model prose is never trusted verbatim: a stray '#' heading would
    otherwise fool the lint's Reference-block reader into thinking the
    section ended early."""
    proposal = {"cause": "## sneaky heading\nSee [[errors/ORA-99999]] too.",
               "action": "fine"}
    rs.apply_research(research_wiki, "errors/ORA-16607.md", proposal,
                      "https://docs.oracle.com/en/error-help/db/ora-16607/",
                      "oracle-docs", TODAY)
    text = (research_wiki / "errors" / "ORA-16607.md").read_text()
    assert "\n## sneaky heading" not in text
    assert "[[errors/ORA-99999]]" not in text
