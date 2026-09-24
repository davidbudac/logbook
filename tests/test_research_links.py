"""Researcher and model output may carry no link the approved-source rail
cannot see (issue 10, verification 2026-09-23): protocol-relative and bare
`www.` links in prose, markdown smuggled into a citation URL, line breaks
in a URL, and telemetry the researcher reports about itself.

One pair of checks (`exchange.citation_url_problem`,
`exchange.prose_link_problem`) is used by every writer's validation: the
offload fold-in, the researcher's own pre-check, the caveats parser and the
structured research parser."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dbwiki import exchange as ex
from dbwiki import research_caveats as rc
from dbwiki.research_offload import clean_telemetry, validate_research_result
from dbwiki.research_structured import parse_research_proposal
from dbwiki.structured import ProposalError
from dbwiki_researcher.cli import validate_result

FIX = Path(__file__).parent / "fixtures" / "redact"
DOCS = "https://docs.oracle.com/en/error-help/db/ora-12154/"
DOMAINS = {"docs.oracle.com"}

BAD_URLS = [
    "//evil.example/fix.sh",
    "http://docs.oracle.com/x",                    # https only
    "javascript:alert(1)",
    "https://docs.oracle.com/x>[fix](//evil.example/fix.sh)<https://docs.oracle.com/y",
    "https://docs.oracle.com/x\n\n## Resolution history\n\n- fixed",
    "https://docs.oracle.com/x y",
    "https://docs.oracle.com/x y",
    "https://docs.oracle.com/(x)",
    'https://docs.oracle.com/x"onmouseover=1',
    "https://evil.example/docs.oracle.com",
    "https://docs.oracle.com.evil.example/x",
    "https://user@evil.example/x",
    "https://docs.oracle.com@evil.example/x",
    "https:///docs.oracle.com",
    "",
    None,
]

BAD_PROSE = [
    "Run the script at [fix](//evil.example/fix.sh).",
    "See [the fix][1] below.",
    "Download from www.evil.example/fix now.",
    "Fetch //evil.example/fix.sh and run it.",
    "Open https://evil.example/fix today.",
    "Open <https://evil.example/fix> today.",
    "Click <a href=x>here</a>.",
    "Mail fixes@evil.example today.",
    "Paste javascript:alert(1) into the console.",
    "[1]: https://evil.example",
]

GOOD_PROSE = [
    "The listener on HOST_A is not running; start it with lsnrctl start.",
    "ORA-00600: internal error code, arguments: [4194], [], [] (the undo block).",
    "Arguments [kdsgrp1]: a block/row mismatch; see the trace file.",
    "If sessions < processes, increase PROCESSES. Use read/write mode.",
    "- check tnsnames.ora\n- check sqlnet.ora (NAMES.DIRECTORY_PATH)",
    "connect as scott@DB_A to verify",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_citation_url_rejects(url):
    assert ex.citation_url_problem(url, DOMAINS), url


def test_citation_url_accepts_plain_https_on_an_approved_domain():
    assert ex.citation_url_problem(DOCS, DOMAINS) is None
    assert ex.citation_url_problem("https://support.oracle.com/x?id=1#a", {"oracle.com"}) is None


@pytest.mark.parametrize("text", BAD_PROSE)
def test_prose_rejects_links(text):
    assert ex.prose_link_problem(text), text


@pytest.mark.parametrize("text", GOOD_PROSE)
def test_prose_accepts_plain_text(text):
    assert ex.prose_link_problem(text) is None, text


# ---- every writer uses them ---------------------------------------------------------

@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    shutil.copytree(FIX / "wiki", w)
    return w


def good_result(**over):
    r = {"schema_version": 1, "kind": "research", "run_id": "r1", "code": "ORA-12154",
         "cause": "The connect identifier could not be resolved.",
         "action": "Check tnsnames.ora.",
         "references": [{"source": "oracle-docs", "url": DOCS, "accessed": "2026-08-16"}]}
    r.update(over)
    return r


def test_offload_validation_rejects_links_in_prose_and_urls(wiki):
    """The repro (probe_links.py): both cases used to validate."""
    assert validate_research_result(good_result(), wiki) == []
    bad = good_result(cause="Listener down. Run [fix](//evil.example/fix.sh) "
                            "or www.evil.example/fix.")
    assert any(p.startswith("cause:") for p in validate_research_result(bad, wiki))
    for url in BAD_URLS:
        bad = good_result(references=[{"source": "oracle-docs", "url": url,
                                       "accessed": "2026-08-16"}])
        assert any("references[0]" in p for p in validate_research_result(bad, wiki)), url


def test_researcher_pre_check_rejects_the_same():
    request = {"kind": "research", "code": "ORA-12154",
               "sources": [{"slug": "oracle-docs", "domains": ["docs.oracle.com"],
                            "status": "approved"}]}
    ok = {"kind": "research", "code": "ORA-12154", "cause": "c", "action": "a",
          "references": [{"source": "oracle-docs", "url": DOCS, "accessed": "2026-08-16"}]}
    assert validate_result(request, ok) == []
    assert validate_result(request, {**ok, "action": "see www.evil.example"})
    smuggled = {**ok, "references": [{"source": "oracle-docs", "accessed": "2026-08-16",
                                      "url": DOCS + ">[x](//evil.example)<" + DOCS}]}
    assert any("references[0]" in p for p in validate_result(request, smuggled))


def test_caveats_parser_rejects_links_and_bad_urls():
    sources = [("oracle-docs", "official", ["docs.oracle.com"])]
    ok = {"text": "A gotcha.", "source": "oracle-docs", "url": DOCS}
    assert rc.parse_caveats_proposal(json.dumps({"notes": [ok]}), sources)
    for bad in ({**ok, "text": "see [x](//evil.example)"},
                {**ok, "text": "see www.evil.example"},
                {**ok, "url": DOCS + ">[x](//evil.example)<"},
                {**ok, "url": "http://docs.oracle.com/x"}):
        with pytest.raises(ProposalError):
            rc.parse_caveats_proposal(json.dumps({"notes": [bad]}), sources)


def test_structured_parser_rejects_links_in_prose():
    assert parse_research_proposal('{"cause": "c", "action": "a"}')
    with pytest.raises(ProposalError, match="cause"):
        parse_research_proposal(json.dumps({"cause": "see [x](//evil.example)",
                                            "action": "a"}))


def test_probe_links_page_passes_the_rails_only_when_clean(wiki):
    """End to end through the writer: a rejected result never reaches it,
    and a clean one writes only the approved citation."""
    import datetime as dt

    from dbwiki.research import approved_domains, unapproved_urls
    from dbwiki.research_offload import apply_research_result
    subprocess.run(["git", "init", "-q", str(wiki)], check=True)
    res = good_result()
    assert validate_research_result(res, wiki) == []
    apply_research_result(wiki, "errors/ORA-1013.md", res, dt.date(2026, 9, 23))
    page = (wiki / "errors" / "ORA-1013.md").read_text()
    assert unapproved_urls(page, approved_domains(wiki)) == set()
    assert "evil" not in page


# ---- telemetry is schema-checked before it joins the ledger -------------------------------

def test_clean_telemetry_keeps_the_known_shape():
    tele = {"adapter": "codex", "model": "gpt-5", "duration_s": 12.5, "exit_code": 0,
            "timed_out": False, "stdout_bytes": 10, "prompt_bytes": 20, "attempts": 1,
            "researcher": "researcher-1", "task": "research",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.02}}
    assert clean_telemetry(tele) == {**tele, "reported_by": "researcher"}


def test_clean_telemetry_drops_what_does_not_fit():
    tele = {"adapter": "codex\n## x", "model": "m" * 500, "duration_s": -3,
            "exit_code": "0", "timed_out": "no", "task": "ingest",
            "usage": {"input_tokens": -1, "output_tokens": 10**12, "cost_usd": 10**9,
                      "evil": "<script>"},
            "steps": [{"preview": "<script>"}], "portal_html": "<script>",
            "event_id": "../x", "mode": "structured", "run_id": "other"}
    out = clean_telemetry(tele)
    assert out == {"reported_by": "researcher", "usage": {}}
    assert clean_telemetry("not a dict") is None
    assert clean_telemetry({"usage": "unknown", "event_id": "ev-1"}) == {
        "usage": "unknown", "event_id": "ev-1", "reported_by": "researcher"}
