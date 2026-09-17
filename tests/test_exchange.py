"""ADR-0002 exchange + `research.mode: offload`: enqueue/claim/complete/fail
over the exchange repo, and the on-prem round — fold results (validate,
de-map, write through the deterministic writer, rails, commit per result),
then enqueue redacted requests, refusing on a leak. Real git repos in tmp;
no agent, no network."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbwiki import exchange as ex
from dbwiki.orchestrate import Orchestrator
from dbwiki.lock import Held
from dbwiki.research_offload import (apply_research_result, leak_records,
                                     validate_research_result,
                                     validate_source_review_result)

FIX = Path(__file__).parent / "fixtures" / "redact"


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def init_repo(path: Path, seed: dict[str, str] | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "test@test")
    git(path, "config", "user.name", "test")
    for rel, text in (seed or {"README.md": "# x\n"}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    git(path, "add", "-A")
    git(path, "commit", "-m", "init")
    return path


@pytest.fixture
def xroot(tmp_path):
    return init_repo(tmp_path / "exchange", {"README.md": "# exchange\n"})


@pytest.fixture
def wiki(tmp_path):
    """The redact fixture wiki, as a git repo, plus the log.md/index.md the
    writers and the lint expect."""
    w = tmp_path / "wiki"
    shutil.copytree(FIX / "wiki", w)
    (w / "log.md").write_text("# log\n")
    (w / "index.md").write_text("---\ntype: index\n---\n\n# index\n\n- [[errors/ORA-12154]]\n"
                                "- [[errors/ORA-1013]]\n- [[databases/cdb1]]\n"
                                "- [[databases/cdb1_stby]]\n- [[hosts/ol9-em241]]\n"
                                "- [[sources/oracle-docs]]\n- [[sources/some-blog]]\n"
                                "- [[incidents/2026-07-12-cdb1-dataguard-transport-failure]]\n")
    return init_repo(w, {})


@pytest.fixture
def cfg(wiki, xroot, tmp_path):
    state = tmp_path / "state"
    shutil.copytree(FIX / "state", state)
    return SimpleNamespace(
        root=tmp_path, wiki_repo=wiki, state_dir=state, report={},
        raw={"elasticsearch": {"url": "http://es-node1.localdomain:9200"}, "redact": {}},
        sources={"alert": {"index_patterns": ["oracle-logs-alert-*"]}},
        research={"mode": "offload", "estate": {"version": "19c"},
                  "exchange": {"path": str(xroot), "push": False}},
        agents={"adapter": "claude", "claude": {"cheap": "sonnet", "strong": "opus"},
                "timeout_seconds": 60})


def req(kind="research", key="ORA-12154", run_id="r1", created="2026-08-16T10:00:00Z"):
    r = {"schema_version": 1, "kind": kind, "run_id": run_id, "created_at": created,
         "attempts": 0}
    r["code" if kind == "research" else "slug"] = key
    return r


def good_result(run_id="r1", code="ORA-12154"):
    return {"schema_version": 1, "kind": "research", "run_id": run_id, "code": code,
            "cause": "The connect identifier for DB_A could not be resolved by any naming method.",
            "action": "Check tnsnames.ora on the primary for the DB_A alias.",
            "references": [{"source": "oracle-docs",
                            "url": "https://docs.oracle.com/en/error-help/db/ora-12154/",
                            "accessed": "2026-08-16"}],
            "related_codes": ["ORA-12514"], "flags": [],
            "telemetry": {"adapter": "codex", "model": "gpt-5", "duration_s": 12.5,
                          "usage": {"input_tokens": 100, "output_tokens": 50}}}


# ---- exchange primitives ------------------------------------------------------------------

def test_enqueue_supersedes_same_key_only(xroot):
    ex.enqueue(xroot, req(run_id="r1"), push=False)
    ex.enqueue(xroot, req(run_id="r2", created="2026-08-16T11:00:00Z"), push=False)
    ex.enqueue(xroot, req(key="ORA-600", run_id="r3"), push=False)
    ex.enqueue(xroot, req(kind="source-review", key="oracle-docs", run_id="r4"), push=False)
    names = sorted(p.name for p in (xroot / "requests" / "pending").glob("*.json"))
    assert names == ["research-ORA-12154-r2.json", "research-ORA-600-r3.json",
                     "source-review-oracle-docs-r4.json"]
    assert "supersedes 1" in git(xroot, "log", "-4", "--format=%s").stdout
    assert "Run-ID: r2" in git(xroot, "log", "-4", "--format=%B").stdout


def test_claim_oldest_then_complete_writes_result_and_clears_claim(xroot):
    ex.enqueue(xroot, req(key="ORA-600", run_id="r3", created="2026-08-16T12:00:00Z"), push=False)
    ex.enqueue(xroot, req(run_id="r1"), push=False)
    got = ex.claim(xroot, "researcher-1", push=False)
    assert got["code"] == "ORA-12154" and got["claimed_by"] == "researcher-1"
    assert (xroot / "requests" / "claimed" / got["_file"]).exists()
    ex.complete(xroot, got, {"cause": "c", "action": "a", "references": []}, push=False)
    assert not (xroot / "requests" / "claimed" / got["_file"]).exists()
    res = json.loads((xroot / "results" / "r1-ORA-12154.json").read_text())
    assert res["run_id"] == "r1" and res["kind"] == "research" and res["cause"] == "c"
    # next claim gets the other one; kind filter respected
    assert ex.claim(xroot, "x", kind="source-review", push=False) is None
    assert ex.claim(xroot, "x", push=False)["code"] == "ORA-600"


def test_fail_requeues_then_fails(xroot):
    ex.enqueue(xroot, req(), push=False)
    got = ex.claim(xroot, "r", push=False)
    back = ex.fail_request(xroot, got, "harness", push=False)
    assert back["_terminal"] is False and back["attempts"] == 1
    assert ex.pending_requests(xroot)[0]["claimed_by"] is None
    got = ex.claim(xroot, "r", push=False)
    back = ex.fail_request(xroot, got, "harness", push=False)
    assert back["_terminal"] is True
    assert ex.failed_requests(xroot)[0]["error_category"] == "harness"
    assert ex.pending_requests(xroot) == []


def test_reject_result_moves_to_failed_with_reason(xroot):
    (xroot / "results").mkdir()
    p = xroot / "results" / "r9-ORA-1.json"
    p.write_text(json.dumps({"kind": "research", "run_id": "r9", "code": "ORA-1"}))
    git(xroot, "add", "-A"); git(xroot, "commit", "-m", "res")
    ex.reject_result(xroot, p, "no such page")
    ex.commit_batch(xroot, "fold", "r9", push=False)
    assert not p.exists()
    f = ex.failed_requests(xroot)
    assert f[0]["_file"] == "result-r9-ORA-1.json" and f[0]["reason"] == "no such page"
    assert f[0]["error_category"] == "result-rejected"


def test_claim_race_one_winner(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True,
                   capture_output=True)
    a = init_repo(tmp_path / "a", {"README.md": "x\n"})
    git(a, "remote", "add", "origin", str(origin)); git(a, "push", "-u", "origin", "main")
    ex.enqueue(a, req(), push=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(tmp_path / "b")], check=True)
    b = tmp_path / "b"
    git(b, "config", "user.email", "t@t"); git(b, "config", "user.name", "t")
    won_a = ex.claim(a, "A", push=True)
    won_b = ex.claim(b, "B", push=True)
    assert (won_a is None) != (won_b is None)


# ---- result validation ---------------------------------------------------------------------

def test_validate_research_result(wiki):
    assert validate_research_result(good_result(), wiki) == []
    bad = good_result(code="ORA-99999")
    assert any("no error page" in p for p in validate_research_result(bad, wiki))
    bad = good_result(); bad["references"][0]["url"] = "https://evil.example/x"
    assert any("not on sources/oracle-docs domains" in p for p in validate_research_result(bad, wiki))
    bad = good_result(); bad["references"][0]["source"] = "some-blog"
    assert any("not an approved source" in p for p in validate_research_result(bad, wiki))
    bad = good_result(); bad["references"] = []
    assert any("references: missing" in p for p in validate_research_result(bad, wiki))
    bad = good_result(); bad["cause"] = "x" * 4001
    assert any("4001 chars" in p for p in validate_research_result(bad, wiki))
    bad = good_result(); bad["kind"] = "report"
    assert validate_research_result(bad, wiki)[0].startswith("kind=")


def test_validate_source_review_result(wiki):
    ok = {"kind": "source-review", "slug": "oracle-docs", "still_valid": True,
          "checked": "2026-08-16", "notes": "fine"}
    assert validate_source_review_result(ok, wiki) == []
    assert validate_source_review_result({**ok, "slug": "nope"}, wiki)
    assert validate_source_review_result({**ok, "still_valid": "yes"}, wiki)
    assert validate_source_review_result({**ok, "checked": "soon"}, wiki)


def test_apply_research_result_writes_reference_and_log(wiki):
    import datetime as dt
    res = good_result()
    res["cause"] = "the connect identifier for cdb1_stby could not be resolved"
    apply_research_result(wiki, "errors/ORA-1013.md", res, dt.date(2026, 8, 16))
    text = (wiki / "errors" / "ORA-1013.md").read_text()
    assert "researched: 2026-08-16" in text
    assert "**Cause:** the connect identifier for cdb1_stby could not be resolved" in text
    assert "(source: sources/oracle-docs; url: <https://docs.oracle.com/en/error-help/db/ora-12154/>; accessed: 2026-08-16)." in text
    assert "Related codes (per the sources above): ORA-12514." in text
    assert "research (offload) — errors/ORA-1013.md" in (wiki / "log.md").read_text()
    # idempotent: a second apply replaces, does not pile up
    apply_research_result(wiki, "errors/ORA-1013.md", res, dt.date(2026, 8, 17))
    assert (wiki / "errors" / "ORA-1013.md").read_text().count("## Reference") == 1


# ---- the on-prem round --------------------------------------------------------------------

def test_offload_round_enqueues_redacted_requests(cfg, wiki, xroot):
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    result = orch.research(["errors/ORA-12154.md", "errors/ORA-1013.md"],
                           ["sources/oracle-docs.md"], run_id="run-1")
    assert result["task"] == "research"
    assert "3 request(s) enqueued" in result["summary"]
    pending = ex.pending_requests(xroot)
    assert sorted(p["_file"] for p in pending) == [
        "research-ORA-1013-run-1.json", "research-ORA-12154-run-1.json",
        "source-review-oracle-docs-run-1.json"]
    dumped = json.dumps(pending)
    assert not any(t in dumped.lower() for t in ("cdb1", "poug", "192.168", "localdomain"))
    assert (cfg.state_dir / "redaction" / "run-1-ORA-12154.json").exists()
    assert (cfg.state_dir / "redaction" / "run-1-ORA-1013.json").exists()
    assert "Run-ID: run-1" in git(xroot, "log", "-1", "--format=%B").stdout
    # nothing was written to the wiki by the enqueue side
    assert git(wiki, "status", "--porcelain").stdout == ""
    # a second run: same candidates are in flight, nothing re-enqueued
    result = orch.research(["errors/ORA-12154.md"], [], run_id="run-2")
    assert "0 request(s) enqueued, 0 result(s) folded, 1 skipped (in flight or just folded)" in result["summary"]
    assert len(ex.pending_requests(xroot)) == 3


def test_offload_round_folds_a_result_through_rails(cfg, wiki, xroot):
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    orch.research(["errors/ORA-12154.md"], [], run_id="run-1")
    # the researcher claims and answers with pseudonyms; on-prem de-maps
    got = ex.claim(xroot, "researcher", push=False)
    mapping = json.loads((cfg.state_dir / "redaction" / "run-1-ORA-12154.json").read_text())["mapping"]
    stby = next(k for k, v in mapping.items() if v == "cdb1_stby")
    res = good_result(run_id="run-1")
    res["cause"] = f"The connect identifier for {stby} could not be resolved."
    ex.complete(xroot, got, res, push=False)
    ex.enqueue(xroot, {"schema_version": 1, "kind": "source-review", "run_id": "run-1",
                       "slug": "oracle-docs", "created_at": "2026-08-16T10:00:00Z", "attempts": 0},
               push=False)
    got = ex.claim(xroot, "researcher", kind="source-review", push=False)
    ex.complete(xroot, got, {"slug": "oracle-docs", "still_valid": False,
                             "checked": "2026-08-16", "notes": "page moved", "proposed_status": "deprecated"},
                push=False)

    # the CLI picks candidates before the fold: a page whose result lands in
    # this very run is not re-enqueued
    result = orch.research(["errors/ORA-12154.md"], [], run_id="run-3")
    assert "0 request(s) enqueued, 2 result(s) folded, 1 skipped (in flight or just folded)" in result["summary"], \
        (result, ex.failed_requests(xroot))
    assert set(result["pages_touched"]) == {"errors/ORA-12154.md", "sources/oracle-docs.md", "log.md"}
    page = (wiki / "errors" / "ORA-12154.md").read_text()
    assert "The connect identifier for cdb1_stby could not be resolved." in page
    assert "researched: 2026-" in page and page.count("## Reference") == 1
    src = (wiki / "sources" / "oracle-docs.md").read_text()
    assert "last_reviewed: 2026-08-16" in src and "status: approved" in src
    assert any("NOT valid" in f and "deprecated" in f for f in result["flags"])
    log = git(wiki, "log", "--format=%s%n%b").stdout
    assert "research (offload) — errors/ORA-12154.md" in log and "Run-ID: run-1" in log
    assert ex.result_files(xroot) == [] and ex.failed_requests(xroot) == []
    assert git(wiki, "status", "--porcelain").stdout == ""
    # telemetry rode back into agent_runs.jsonl
    runs = (cfg.state_dir / "agent_runs.jsonl").read_text()
    assert '"mode": "offload"' in runs and '"model": "gpt-5"' in runs


def test_offload_rejects_bad_results_and_keeps_going(cfg, wiki, xroot):
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    orch.research(["errors/ORA-12154.md"], [], run_id="run-1")
    got = ex.claim(xroot, "r", push=False)
    bad = good_result(run_id="run-1")
    bad["references"][0]["url"] = "https://blog.example.net/ora-12154"   # not approved
    ex.complete(xroot, got, bad, push=False)
    (xroot / "results" / "ghost-ORA-1013.json").write_text(json.dumps(good_result("ghost", "ORA-1013")))
    git(xroot, "add", "-A"); git(xroot, "commit", "-m", "ghost")
    result = orch.research([], [], run_id="run-2")
    assert "0 result(s) folded" in result["summary"]
    failed = {f["_file"]: f["reason"] for f in ex.failed_requests(xroot)}
    assert "not on sources/oracle-docs domains" in failed["result-run-1-ORA-12154.json"]
    assert "unknown run_id" in failed["result-ghost-ORA-1013.json"]
    assert ex.result_files(xroot) == []
    assert "researched" not in (wiki / "errors" / "ORA-1013.md").read_text().split("# ORA-1013")[0]
    assert git(wiki, "status", "--porcelain").stdout == ""


def test_offload_refuses_on_leak_and_records_it(cfg, wiki, xroot, monkeypatch):
    import dbwiki.research_offload as ro
    monkeypatch.setattr(ro.Redactor, "redact_obj", lambda self, obj: obj)
    orch = Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))
    result = orch.research(["errors/ORA-12154.md"], [], run_id="run-1")
    assert "1 refused (redaction leak)" in result["summary"]
    assert ex.pending_requests(xroot) == []
    recs = leak_records(cfg.state_dir)
    assert recs[0]["run_id"] == "run-1" and recs[0]["code"] == "ORA-12154" and "cdb1" in recs[0]["hits"]
    from dbwiki.health import _exchange_state
    x = _exchange_state(cfg, "2026-08-16T12:00:00Z", 26)
    assert x["configured"] and x["redaction_leaks"] == 1 and x["pending_count"] == 0


def test_offload_needs_an_exchange(cfg):
    cfg.research["exchange"] = {}
    with pytest.raises(RuntimeError, match="research.exchange.path"):
        Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0)).research(["errors/ORA-1013.md"], [], run_id="r")
