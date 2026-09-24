"""Everything that leaves the box through research crosses one boundary:
redact, then leak-check, then (only then) truncate — issue 07,
verification 2026-09-23.

- The caveats stage (ADR-0005) used to send the page's Reference text as
  "public Oracle text"; once offload de-maps a researcher's answer into
  that section it carries real names. Its prompt is now redacted and leak
  checked like an offload request, and a leak skips the page (fail closed).
- The offload request builder cut notes and the current reference before
  redacting, so a name or an address cut in half no longer matched.
- Source-review requests promised a leak check they never ran."""

import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import pytest
from fixtures import make_config

from dbwiki import readmodel
from dbwiki import research_caveats as rc
from dbwiki.lock import Held
from dbwiki.orchestrate import Orchestrator
from dbwiki.redact import Redactor, RedactionLeak, Vocabulary
from dbwiki.research_offload import (apply_research_result,
                                     build_research_request,
                                     build_source_review_request, leak_records)

DOCS = "https://docs.oracle.com/en/error-help/db/ora-12541/"
CITATION = f"(source: sources/oracle-docs; url: {DOCS}; accessed: 2026-07-28)"
SOURCE_PAGE = ("---\ntype: source\nstatus: approved\ntier: official\n"
               "domains: [docs.oracle.com]\nfetchable: true\nadded: 2026-01-01\n"
               "last_reviewed: 2026-01-01\nreview_after_days: 180\n---\n\n"
               "# Oracle docs\n")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def named_page(code: str) -> str:
    """An error page whose Reference names the estate, the way an offload
    fold-in writes it after de-mapping the researcher's pseudonyms."""
    return (f"---\ntype: error-class\nupdated: 2026-07-10T00:00:00Z\n"
            f"researched: 2026-07-28\n---\n\n# {code}\n\n## Reference\n\n"
            f"**Cause:** No listener is running on lab-dg1.localdomain for prodfin9.\n"
            f"{CITATION}.\n\n"
            f"**Action:** Start the listener on lab-dg1.localdomain (10.20.30.40).\n"
            f"{CITATION}.\n")


@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    for d in ("errors", "sources", "databases", "hosts", "incidents", "digests"):
        (w / d).mkdir(parents=True)
    (w / "sources" / "oracle-docs.md").write_text(SOURCE_PAGE)
    (w / "databases" / "prodfin9.md").write_text("---\ntype: database\n---\n# prodfin9\n")
    (w / "hosts" / "lab-dg1.md").write_text(
        "---\ntype: host\n---\n# lab-dg1\n\nFQDN lab-dg1.localdomain, IP 10.20.30.40\n")
    (w / "errors" / "ORA-12541.md").write_text(named_page("ORA-12541"))
    (w / "log.md").write_text("# log\n")
    (w / "digests" / ".keep").write_text("")
    git(w, "init")
    git(w, "config", "user.email", "t@t")
    git(w, "config", "user.name", "t")
    git(w, "add", "-A")
    git(w, "commit", "-m", "init")
    return w


def cfg_for(wiki, tmp_path, **research):
    return make_config(tmp_path, wiki_repo=wiki, state_dir=tmp_path / "state",
                       research={"caveats": {"enabled": True}, **research})


SECRETS = ("lab-dg1", "localdomain", "prodfin9", "10.20.30.40")


# ---- caveats: the prompt is redacted and leak checked ------------------------------

def test_offload_fold_then_caveats_prompt_carries_no_real_name(tmp_path):
    """The repro: a researcher echoes pseudonyms, the fold de-maps them into
    the page, and the caveats prompt built from that page must not carry
    the real names back out."""
    w = tmp_path / "w"
    (w / "errors").mkdir(parents=True)
    (w / "errors" / "ORA-12541.md").write_text("---\ntype: error-class\n---\n# ORA-12541\n")
    v = Vocabulary(source_domains={"docs.oracle.com"})
    v.add("HOST", "lab-dg1.localdomain")
    v.add("DB", "prodfin9")
    red = Redactor(v, "r1", key="r1-ORA-12541")
    red.redact("listener on lab-dg1.localdomain for prodfin9 refused")
    res = {"kind": "research", "code": "ORA-12541", "run_id": "r1",
           "cause": "No listener is running on HOST_A for DB_A.",
           "action": "Start the listener on HOST_A.",
           "references": [{"source": "oracle-docs", "url": DOCS, "accessed": "2026-09-23"}]}
    apply_research_result(w, "errors/ORA-12541.md", red.demap_obj(res), dt.date(2026, 9, 23))
    found = readmodel.parse_research("ORA-12541", "errors/ORA-12541.md",
                                     (w / "errors" / "ORA-12541.md").read_text())
    assert "prodfin9" in found.cause          # the page itself carries the name
    prompt, back = rc.outbound_caveats_prompt(
        make_config(), "ORA-12541", found.cause, found.action,
        [("oracle-docs", "1", ["docs.oracle.com"])], vocab=v)
    assert "prodfin9" not in prompt and "lab-dg1" not in prompt
    assert "Cause: No listener is running on HOST_A for DB_A." in prompt
    assert back.demap("DB_A") == "prodfin9"


def test_outbound_prompt_builds_the_vocabulary_from_the_wiki(wiki, tmp_path):
    found = readmodel.parse_research("ORA-12541", "errors/ORA-12541.md",
                                     (wiki / "errors" / "ORA-12541.md").read_text())
    prompt, _ = rc.outbound_caveats_prompt(
        cfg_for(wiki, tmp_path), "ORA-12541", found.cause, found.action,
        rc.approved_fetchable_sources(wiki))
    for s in SECRETS:
        assert s not in prompt, s
    assert "docs.oracle.com" in prompt and "ORA-12541" in prompt


def test_outbound_prompt_fails_closed(wiki, tmp_path, monkeypatch):
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    with pytest.raises(RedactionLeak) as ei:
        rc.outbound_caveats_prompt(cfg_for(wiki, tmp_path), "ORA-12541",
                                   "on prodfin9", "restart", rc.approved_fetchable_sources(wiki))
    assert "prodfin9" in ei.value.hits


def fake_web(monkeypatch, answer: str) -> list[str]:
    seen: list[str] = []

    def _run(prompt, model, timeout, *, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="claude", model=model, duration_s=0.1,
                             exit_code=0, timed_out=False, usage="unknown")
        return answer

    monkeypatch.setattr(rc, "run_web_text", _run)
    return seen


def test_caveats_stage_sends_only_the_redacted_prompt(wiki, tmp_path, monkeypatch):
    note = {"text": "On DB_A the listener log shows why.", "source": "oracle-docs",
            "url": DOCS}
    seen = fake_web(monkeypatch, json.dumps({"notes": [note]}))
    cfg = cfg_for(wiki, tmp_path)
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    result = orch.caveats(["errors/ORA-12541.md"], run_id="run-c")
    assert result["summary"] == "caveats: 1 page(s), 1 note(s)", result
    assert len(seen) == 1
    for s in SECRETS:
        assert s not in seen[0], s
    assert "HOST_A" in seen[0] and "DB_A" in seen[0]
    # a pseudonym the model echoed comes back as the real name on-prem
    page = (wiki / "errors" / "ORA-12541.md").read_text()
    assert "**Practitioner note:** On prodfin9 the listener log shows why." in page


def test_caveats_stage_skips_a_page_whose_prompt_leaks(wiki, tmp_path, monkeypatch):
    seen = fake_web(monkeypatch, '{"notes": []}')
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    cfg = cfg_for(wiki, tmp_path)
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    result = orch.caveats(["errors/ORA-12541.md"], run_id="run-c")
    assert seen == [], "a leaking prompt must never reach the model"
    assert result["summary"] == "caveats: 0 page(s), 0 note(s), 1 failed"
    assert any(f.startswith("redaction-leak: errors/ORA-12541.md") for f in result["flags"])
    assert not any("prodfin9" in f for f in result["flags"]), "hits stay in the leak log"
    rec = leak_records(cfg.state_dir)
    assert rec[0]["run_id"] == "run-c" and rec[0]["code"] == "ORA-12541"
    assert "prodfin9" in rec[0]["hits"]
    assert (wiki / "errors" / "ORA-12541.md").read_text() == named_page("ORA-12541")


def test_caveats_dry_run_prints_the_redacted_prompt(wiki, tmp_path, capsys):
    from dbwiki.cli import _run_caveats
    from fixtures.incident_pages import incident_page
    (wiki / "incidents" / "2026-07-01-prodfin9-a.md").write_text(
        incident_page("prodfin9", "a", error_codes=("ORA-12541",)))
    cfg = cfg_for(wiki, tmp_path)
    assert _run_caveats(SimpleNamespace(limit=None, dry_run=True), cfg, None, wiki) == 0
    out = capsys.readouterr().out
    assert "You are researching Oracle error ORA-12541" in out
    prompt = out[out.index("You are researching"):]
    for s in SECRETS:
        assert s not in prompt, s


# ---- offload request: redact first, truncate after -----------------------------------

def test_request_redacts_before_truncating(wiki, tmp_path):
    note1 = "x" * 289 + " on prodfin9"                # the cut at 300 falls inside the name
    note2 = "y" * 285 + " from 10.20.30.41"          # ... and inside the address
    (wiki / "errors" / "ORA-1.md").write_text(
        "---\ntype: error-class\n---\n# ORA-1\n\n## Occurrences\n\n"
        "| day | db | note | evidence |\n|---|---|---|---|\n"
        f"| 2026-09-01 | x | {note1} | - |\n| 2026-09-02 | x | {note2} | - |\n\n"
        "## Reference\n\n" + "z" * 3990 + " on prodfin9.\n")
    cfg = make_config()
    req, _ = build_research_request(cfg, wiki, wiki / "errors" / "ORA-1.md", "r1",
                                    state_dir=tmp_path / "none")
    dumped = json.dumps(req)
    assert "prodfin" not in dumped and "10.20.30." not in dumped
    ctx = req["synopsis"]["context"]
    assert ctx[0].endswith(" on DB_A") and ctx[1].endswith(" from IP_A")
    assert all(len(c) <= 300 for c in ctx)
    assert len(req["synopsis"]["current_reference"]) <= 4000


# ---- source-review request: the promised leak check ------------------------------------

def test_source_review_request_redacts_its_notes(wiki):
    (wiki / "sources" / "some-blog.md").write_text(
        "---\ntype: source\nstatus: approved\ndomains: [blog.example.org]\n"
        "url: https://blog.example.org/\n---\n\n# blog\n\n"
        "Useful for the prodfin9 listener issue on lab-dg1.localdomain.\n")
    req = build_source_review_request(wiki, wiki / "sources" / "some-blog.md", "r2")
    dumped = json.dumps(req)
    for s in SECRETS:
        assert s not in dumped, s
    assert "blog.example.org" in dumped and "listener issue" in req["previous_notes"]


def test_source_review_request_drops_notes_it_cannot_clean(wiki, monkeypatch):
    (wiki / "sources" / "some-blog.md").write_text(
        "---\ntype: source\nstatus: approved\ndomains: [blog.example.org]\n---\n\n"
        "# blog\n\nprodfin9 notes\n")
    monkeypatch.setattr(Redactor, "redact", lambda self, text: text)
    req = build_source_review_request(wiki, wiki / "sources" / "some-blog.md", "r2")
    assert req["previous_notes"] == ""
    assert "prodfin9" not in json.dumps(req)

