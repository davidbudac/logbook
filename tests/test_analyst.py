"""`dbwiki analyst` (ADR-0001): claim + run one queued report request through
the same Orchestrator rails as every other agent stage (validate, lint,
rollback), and the queue bookkeeping (complete/fail_request) that follows.
`run_agent` is stubbed — no real cloud adapter — the same way test_orchestrate
and test_structured_report stub it."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from dbwiki import cli, queue
from dbwiki.harness import HarnessError, NoResultError
from dbwiki.lock import Held

WINDOW = ("2026-08-05T18:00:00Z", "2026-08-05T20:15:00Z")


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def head_message(repo) -> str:
    return git(repo, "log", "-1", "--format=%B").stdout


@pytest.fixture
def wiki(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "log.md").write_text("# log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def cfg(wiki, tmp_path):
    # pinned adapter/config: no live network, no real cloud model
    return SimpleNamespace(
        wiki_repo=wiki, state_dir=tmp_path / "state", report={}, research={},
        agents={"adapter": "claude",
               "claude": {"cheap": "sonnet", "strong": "opus"},
               "timeout_seconds": 60})


def enqueue(wiki, *, run_id="r1", day="2026-08-05", suffix=""):
    return queue.enqueue_request(
        wiki, run_id=run_id, kind="report", day=day, suffix=suffix,
        window=WINDOW, notable_dbs=["cdb1"],
        prompt="Task: report.\nWindow: notable.\n", push=False,
        lock=Held(wiki, "test", 0.0))


def fake_agent(monkeypatch, edits: dict, result: dict):
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=1.0,
                             exit_code=0, timed_out=False, usage="unknown")
        for rel, text in edits.items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return result
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)


# ---- nothing to do -------------------------------------------------------------

def test_nothing_to_claim_is_a_clean_no_op(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["analyst"]) == 0
    assert "nothing to claim" in capsys.readouterr().out


# ---- success path ---------------------------------------------------------------

def test_claims_runs_and_completes_a_report_request(cfg, wiki, monkeypatch, capsys):
    enqueue(wiki, run_id="r1", day="2026-08-05")
    fake_agent(monkeypatch, {
        "reports/2026-08-05.md": "---\ntype: report\n---\n\n# analyst report\n",
        "log.md": "# log\n[report] analyst\n",
    }, {"task": "report", "notable": True, "summary": "analyst summary",
        "pages_touched": ["reports/2026-08-05.md", "log.md"]})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    rc = cli.main(["analyst"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"] == "analyst summary"
    assert (wiki / "reports" / "2026-08-05.md").read_text() == \
        "---\ntype: report\n---\n\n# analyst report\n"
    # the claim protocol's step-5 commit: claimed request gone, no leftovers
    assert not list((wiki / "queue" / "claimed").glob("*.json"))
    assert not list((wiki / "queue" / "pending").glob("*.json"))
    results = list((wiki / "queue" / "results").glob("*.json"))
    assert len(results) == 1
    rec = json.loads(results[0].read_text())
    assert rec["run_id"] == "r1" and rec["validation_ok"] is True
    assert rec["rolled_back"] is False and rec["model_tier"] == "strong"
    assert head_message(wiki).startswith("report: 2026-08-05")
    assert "Run-ID: r1" in head_message(wiki)


def test_once_and_kind_flags_are_accepted(cfg, wiki, monkeypatch):
    enqueue(wiki, run_id="r1", day="2026-08-05")
    fake_agent(monkeypatch, {
        "reports/2026-08-05.md": "---\ntype: report\n---\n\n# r\n",
        "log.md": "# log\n[report] analyst\n",
    }, {"task": "report", "notable": True, "summary": "s",
        "pages_touched": ["reports/2026-08-05.md", "log.md"]})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["analyst", "--once", "--kind", "report"]) == 0


def test_kind_filter_ignores_a_request_of_a_different_kind(cfg, wiki, monkeypatch,
                                                            capsys):
    enqueue(wiki, run_id="r1", day="2026-08-05")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["analyst", "--kind", "lint"]) == 0
    assert "nothing to claim" in capsys.readouterr().out
    assert len(queue.pending_requests(wiki)) == 1  # untouched


# ---- the strong-tier agent that forgets the result file -------------------------

def test_a_report_with_no_result_json_is_synthesized_not_thrown_away(
        cfg, wiki, monkeypatch):
    """The analyst node is where the long opus report runs, so it is where the
    missing .agent-result.json costs the most (report NoResult). The
    pages are on disk; synthesize the result and complete the request."""
    enqueue(wiki, run_id="r1", day="2026-08-05")

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=440.0,
                             exit_code=0, timed_out=False, usage="unknown")
        (wiki / "reports").mkdir(exist_ok=True)
        (wiki / "reports" / "2026-08-05.md").write_text(
            "---\ntype: report\n---\n\n# analyst report\n")
        (wiki / "log.md").write_text("# log\n[report] analyst\n")
        raise NoResultError("claude finished but wrote no .agent-result.json")

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["analyst"]) == 0
    assert (wiki / "reports" / "2026-08-05.md").exists()
    assert queue.pending_requests(wiki) == [] and queue.failed_requests(wiki) == []
    rec = json.loads(list((wiki / "queue" / "results").glob("*.json"))[0]
                     .read_text())
    assert rec["validation_ok"] is True and rec["rolled_back"] is False


# ---- rollback / requeue / terminal-fail paths -----------------------------------

def test_validation_failure_rolls_back_and_requeues_below_max_attempts(
        cfg, wiki, monkeypatch, capsys):
    enqueue(wiki, run_id="r1", day="2026-08-05")
    # log.md never touched -> _validate rejects it
    fake_agent(monkeypatch, {
        "reports/2026-08-05.md": "---\ntype: report\n---\n\n# bad\n"},
        {"task": "report", "notable": True, "summary": "bad",
         "pages_touched": ["reports/2026-08-05.md"]})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    rc = cli.main(["analyst"])
    assert rc == 1
    assert "FAILED validation" in capsys.readouterr().err
    assert not (wiki / "reports" / "2026-08-05.md").exists()  # rolled back
    assert not list((wiki / "queue" / "claimed").glob("*.json"))
    pending = queue.pending_requests(wiki)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["claimed_by"] is None
    # a requeue is not yet a terminal fact: no telemetry event written
    assert not list((wiki / "queue" / "results").glob("*.json"))


def test_harness_error_rolls_back_and_moves_to_failed_after_max_attempts(
        cfg, wiki, monkeypatch, capsys):
    enqueue(wiki, run_id="r1", day="2026-08-05")

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=0.5,
                             exit_code=124, timed_out=True, usage="unknown")
        raise HarnessError("agent timed out after 60s: claude")

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    assert cli.main(["analyst"]) == 1   # attempt 1: rolled back, requeued
    assert "FAILED:" in capsys.readouterr().err
    assert queue.failed_requests(wiki) == []
    assert len(queue.pending_requests(wiki)) == 1

    assert cli.main(["analyst"]) == 1   # attempt 2: terminal -> failed/
    failed = queue.failed_requests(wiki)
    assert len(failed) == 1
    assert failed[0]["error_category"] == "agent_timeout"
    assert queue.pending_requests(wiki) == []
    results = list((wiki / "queue" / "results").glob("*.json"))
    assert len(results) == 1
    rec = json.loads(results[0].read_text())
    assert rec["rolled_back"] is True and rec["validation_ok"] is False
    assert rec["error_category"] == "agent_timeout"
