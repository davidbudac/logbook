"""ADR-0002 researcher: claim → agent over a scratch dir holding only the
request → validate → result pushed to the exchange; failures requeue then
fail. Fake agent, real exchange repo in tmp; import footprint stays lean."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dbwiki import exchange as ex
from dbwiki.harness import HarnessError
from dbwiki_researcher import cli as rc


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture
def xroot(tmp_path):
    root = tmp_path / "exchange"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "README.md").write_text("# exchange\n")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def cfg(xroot):
    return {"exchange": xroot, "adapter": "codex", "model": None, "provider": None,
            "timeout": 60, "poll": 1, "name": "researcher-1", "push": False}


def research_request(run_id="r1", code="ORA-12154"):
    return {"schema_version": 1, "kind": "research", "run_id": run_id, "code": code,
            "created_at": "2026-08-16T10:00:00Z", "attempts": 0,
            "estate": {"product": "Oracle Database", "version": "19c"},
            "synopsis": {"message_templates": ["Failed to connect to remote database DB_A"],
                         "co_occurring_codes": ["ORA-16607"]},
            "sources": [{"slug": "oracle-docs", "domains": ["docs.oracle.com"],
                         "status": "approved", "fetchable": True},
                        {"slug": "some-blog", "domains": ["example.org"],
                         "status": "deprecated", "fetchable": False}],
            "instructions": "pseudonyms…"}


def review_request(run_id="r2"):
    return {"schema_version": 1, "kind": "source-review", "run_id": run_id,
            "slug": "oracle-docs", "created_at": "2026-08-16T10:00:00Z", "attempts": 0,
            "url": "https://docs.oracle.com/", "domains": ["docs.oracle.com"],
            "status": "approved", "last_reviewed": "2026-01-01", "review_after_days": 180,
            "previous_notes": "fine"}


GOOD = {"task": "research", "kind": "research", "code": "ORA-12154",
        "cause": "DB_A's connect identifier could not be resolved.",
        "action": "- check tnsnames.ora\n- check names.directory_path",
        "references": [{"source": "oracle-docs",
                        "url": "https://docs.oracle.com/en/error-help/db/ora-12154/",
                        "accessed": "2026-08-16"}],
        "related_codes": ["ORA-12514"], "flags": [], "extra": "dropped"}


def fake_agent(result, *, capture=None):
    def _agent(adapter, prompt, scratch, model, timeout, web=False, provider=None,
               telemetry=None):
        if capture is not None:
            capture.update(adapter=adapter, prompt=prompt, scratch=Path(scratch),
                           files=sorted(p.name for p in Path(scratch).iterdir()),
                           web=web, model=model)
        if isinstance(result, Exception):
            raise result
        if telemetry is not None:
            telemetry.update(model="gpt-5", duration_s=3.2,
                             usage={"input_tokens": 10, "output_tokens": 5})
        return result
    return _agent


# ---- lean ----------------------------------------------------------------------------------

def test_import_footprint_is_lean():
    """A fresh interpreter importing the researcher must not pull the ES/
    compactor/orchestrator side of dbwiki (nor `requests`) in behind it."""
    code = ("import sys, dbwiki_researcher.cli; "
            "print(sorted(m for m in sys.modules if m.startswith('dbwiki') or m == 'requests'))")
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True,
                         text=True).stdout
    # dbwiki.validate is stdlib-only: the id/slug checks both sides share;
    # dbwiki.prompts is stdlib-only too: it reads the prompt text files
    assert out.strip() == ("['dbwiki', 'dbwiki.exchange', 'dbwiki.harness', "
                           "'dbwiki.prompts', 'dbwiki.queue', 'dbwiki.validate', "
                           "'dbwiki_researcher', 'dbwiki_researcher.cli']"), out


# ---- prompt / validation -------------------------------------------------------------------

def test_prompt_lists_only_approved_sources_and_explains_pseudonyms():
    p = rc.build_prompt(research_request())
    assert "oracle-docs: docs.oracle.com" in p and "some-blog" not in p
    assert "PSEUDONYMS" in p and "DB_A" in p and "ORA-12154" in p
    assert ".agent-result.json" in p
    q = rc.build_prompt(review_request())
    assert "review the approved source `oracle-docs`" in q and "still_valid" in q


def test_validate_result_against_request():
    req = research_request()
    assert rc.validate_result(req, GOOD) == []
    bad = {**GOOD, "references": [{"source": "some-blog", "url": "https://example.org/x",
                                    "accessed": "2026-08-16"}]}
    assert any("not approved" in p for p in rc.validate_result(req, bad))
    bad = {**GOOD, "references": [{"source": "oracle-docs", "url": "https://evil.net/",
                                    "accessed": "2026-08-16"}]}
    assert any("not on oracle-docs domains" in p for p in rc.validate_result(req, bad))
    assert any("code=" in p for p in rc.validate_result(req, {**GOOD, "code": "ORA-1"}))
    assert any("cause" in p for p in rc.validate_result(req, {**GOOD, "cause": ""}))
    rr = review_request()
    assert rc.validate_result(rr, {"slug": "oracle-docs", "still_valid": True,
                                   "checked": "2026-08-16"}) == []
    assert rc.validate_result(rr, {"slug": "oracle-docs", "still_valid": "y",
                                   "checked": "2026-08-16", "proposed_status": "gone"})


# ---- run_one / tick ---------------------------------------------------------------------------

def test_run_one_scratch_holds_only_the_request_and_shapes_result(cfg):
    cap: dict = {}
    out = rc.run_one(cfg, {"_file": "x.json", **research_request()},
                     agent=fake_agent(GOOD, capture=cap))
    assert cap["files"] == ["request.json"] and cap["web"] is True
    assert not cap["scratch"].exists()                       # cleaned up
    assert "_file" not in json.dumps(out)
    assert out["kind"] == "research" and out["code"] == "ORA-12154"
    assert "extra" not in out and out["related_codes"] == ["ORA-12514"]
    assert out["telemetry"]["model"] == "gpt-5" and out["telemetry"]["researcher"] == "researcher-1"


def test_tick_claims_runs_and_completes(cfg, xroot):
    ex.enqueue(xroot, research_request(), push=False)
    assert rc.tick(cfg, agent=fake_agent(GOOD)) == "done ORA-12154"
    res = json.loads((xroot / "results" / "r1-ORA-12154.json").read_text())
    assert res["run_id"] == "r1" and res["cause"].startswith("DB_A")
    assert ex.claimed_requests(xroot) == [] and ex.pending_requests(xroot) == []
    assert rc.tick(cfg, agent=fake_agent(GOOD)) == "idle"


def test_tick_source_review(cfg, xroot):
    ex.enqueue(xroot, review_request(), push=False)
    ans = {"task": "research", "kind": "source-review", "slug": "oracle-docs",
           "still_valid": False, "notes": "moved", "checked": "2026-08-16",
           "proposed_status": "deprecated"}
    assert rc.tick(cfg, agent=fake_agent(ans), kind="source-review") == "done oracle-docs"
    res = json.loads((xroot / "results" / "r2-oracle-docs.json").read_text())
    assert res["still_valid"] is False and res["proposed_status"] == "deprecated"


def test_tick_requeues_then_fails_on_bad_results(cfg, xroot, capsys):
    ex.enqueue(xroot, research_request(), push=False)
    bad = {**GOOD, "references": []}
    assert rc.tick(cfg, agent=fake_agent(bad)) == "requeued ORA-12154"
    assert ex.pending_requests(xroot)[0]["attempts"] == 1
    assert rc.tick(cfg, agent=fake_agent(HarnessError("boom"))) == "failed ORA-12154"
    failed = ex.failed_requests(xroot)
    assert failed[0]["error_category"] == "harness" and failed[0]["attempts"] == 2
    err = capsys.readouterr().err
    assert "validation: result rejected" in err and "harness: boom" in err


def test_main_once_and_config(tmp_path, xroot, monkeypatch):
    (tmp_path / "researcher.yaml").write_text(
        f"exchange:\n  path: {xroot}\nadapter: codex\nname: r9\npush: false\n")
    ex.enqueue(xroot, research_request(), push=False)
    monkeypatch.setattr(rc, "run_agent", fake_agent(GOOD))
    monkeypatch.chdir(tmp_path)
    assert rc.main(["--once"]) == 0
    assert json.loads((xroot / "results" / "r1-ORA-12154.json").read_text())["telemetry"]["researcher"] == "r9"
    with pytest.raises(SystemExit):
        rc.load_config(tmp_path / "missing.yaml")
