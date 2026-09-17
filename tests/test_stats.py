"""WS6 agent cost/quality telemetry: run-ID correlation, the enriched ledger
entry, its redaction guarantees, telemetry-failure isolation, and `dbwiki
stats` over the agent-run log (its primary source, the only one that sees
report/lint/research) with the ledger behind it for pre-telemetry ingests.

Offline throughout — the adapter is a fake `run_agent` that fills the
telemetry out-param the way the real harness does, and every wiki/state path
lives under tmp_path."""

import itertools
import json
import subprocess
from types import SimpleNamespace

import fixtures as fx
import pytest

from dbwiki import cli, orchestrate
from dbwiki.harness import HarnessError
from dbwiki.health import read_events
from dbwiki.orchestrate import Orchestrator, ValidationError
from dbwiki.stats import MIN_SAMPLE, collect, format_stats
from dbwiki.lock import Held

DB = "cdb1"
CASE = "ora600_internal_error"      # a digest with real ORA messages in it
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cost_usd": 0.021}


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def cfg(tmp_path):
    """Fixture config whose digest_dir is the wiki's, so the CLI and the
    orchestrator address the same digest file."""
    c = fx.fixture_config(tmp_path)
    c.digest_dir = c.wiki_repo / "digests"
    wiki = c.wiki_repo
    (wiki / "digests" / DB).mkdir(parents=True)
    (wiki / "log.md").write_text("# log\n")
    (wiki / DIGEST_REL).write_text(json.dumps(fx.golden_digest(CASE), indent=1))
    git(wiki, "init")
    git(wiki, "config", "user.email", "test@test")
    git(wiki, "config", "user.name", "test")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    c.state_dir.mkdir(parents=True, exist_ok=True)
    return c


DAY = fx.golden_digest(CASE)["window"]["day"]
DIGEST_REL = f"digests/{DB}/{DAY}.json"


@pytest.fixture
def orch(cfg):
    cfg.agents = {"adapter": "claude", "claude": {"cheap": "sonnet",
                                                  "strong": "opus"}}
    return Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))


GOOD_RESULT = {"task": "ingest", "summary": "ORA-600 incident opened",
               "notable": True, "pages_touched": ["log.md"],
               "incidents_opened": ["incidents/x.md"],
               "incidents_updated": ["incidents/y.md", "incidents/z.md"],
               "flags": ["needs a human"]}
GOOD_EDITS = {"log.md": "# log\n[2026-07-27] ingest cdb1\n"}


def agent(monkeypatch, *, result=GOOD_RESULT, edits=None, raises=None,
          usage=USAGE, timed_out=False, prompts=None):
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if prompts is not None:
            prompts.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=4.25,
                             exit_code=1 if raises else 0, timed_out=timed_out,
                             stdout_bytes=64, usage=usage)
        if raises is not None:
            raise raises
        for rel, text in (GOOD_EDITS if edits is None else edits).items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return result
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)


def commit_body(wiki) -> str:
    return git(wiki, "log", "-1", "--format=%B")


# ---- run-ID correlation ------------------------------------------------------

def test_run_id_reaches_the_ledger_and_the_commit_trailer(cfg, orch, monkeypatch):
    agent(monkeypatch)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="feedfacecafe")
    assert orch.state.get_ledger()[DIGEST_REL]["run_id"] == "feedfacecafe"
    body = commit_body(cfg.wiki_repo)
    assert body.splitlines()[0].startswith(f"ingest: {DB} {DAY}")
    assert "\nRun-ID: feedfacecafe\n" in body


def test_a_stage_without_a_run_id_mints_one(cfg, orch, monkeypatch):
    agent(monkeypatch)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL)
    rid = orch.state.get_ledger()[DIGEST_REL]["run_id"]
    assert rid and f"Run-ID: {rid}" in commit_body(cfg.wiki_repo)


def test_cli_ingest_shares_one_run_id_across_event_ledger_and_commit(
        cfg, orch, monkeypatch):
    agent(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_ingest(SimpleNamespace(digest=None, db=DB, date=DAY,
                                          dry_run=False, adapter=None,
                                          lock=Held(cfg.state_dir, "t", 0.0))) == 0
    rid = read_events(cfg.state_dir)[-1]["run_id"]
    assert orch.state.get_ledger()[DIGEST_REL]["run_id"] == rid
    assert f"Run-ID: {rid}" in commit_body(cfg.wiki_repo)


def test_report_commit_carries_the_trailer_too(cfg, orch, monkeypatch):
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        (wiki / "log.md").write_text("# log\nreport\n")
        (wiki / "reports").mkdir(exist_ok=True)
        (wiki / f"reports/{DAY}.md").write_text("# report\n")
        return {"task": "report", "summary": "s", "notable": False,
                "pages_touched": ["log.md", f"reports/{DAY}.md"]}
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    orch.report(DAY, (f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z"), [],
                run_id="abc123")
    body = commit_body(cfg.wiki_repo)
    assert body.startswith(f"report: {DAY} — ") and "Run-ID: abc123" in body


# ---- ledger enrichment -------------------------------------------------------

def test_successful_ingest_records_the_full_telemetry_block(cfg, orch,
                                                            monkeypatch):
    agent(monkeypatch)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r1")
    e = orch.state.get_ledger()[DIGEST_REL]
    assert e["status"] == "ingested" and e["commit"]
    assert e["task"] == "ingest" and e["adapter"] == "claude"
    # this digest carries error-class groups, so the strong tier was selected
    assert e["model_tier"] == "strong" and e["model"] == "opus"
    assert e["duration_s"] == 4.25 and e["timed_out"] is False
    assert e["usage"] == USAGE
    assert e["digest_bytes"] == len((cfg.wiki_repo / DIGEST_REL).read_bytes())
    assert e["pages_touched"] == 1
    assert e["incidents_opened"] == 1 and e["incidents_updated"] == 2
    assert e["validation_ok"] is True and e["rolled_back"] is False
    assert e["lint_findings"] == 0
    assert e["flags"] == ["needs a human"]


def test_validation_failure_is_ledgered_with_telemetry_and_rollback(cfg, orch,
                                                                    monkeypatch):
    agent(monkeypatch, result={**GOOD_RESULT, "pages_touched": ["ghost.md"]})
    with pytest.raises(ValidationError):
        orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r2")
    e = orch.state.get_ledger()[DIGEST_REL]
    assert e["status"] == "failed" and e["error_category"] == "validation_failed"
    assert e["run_id"] == "r2" and e["task"] == "ingest"
    assert e["validation_ok"] is False and e["rolled_back"] is True
    assert e["usage"] == USAGE and e["duration_s"] == 4.25
    assert e["digest_bytes"] > 0


def test_timed_out_run_is_ledgered_as_timed_out(cfg, orch, monkeypatch):
    agent(monkeypatch, raises=HarnessError("agent timed out after 900s: claude"),
          usage="unknown", timed_out=True)
    with pytest.raises(HarnessError):
        orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r3")
    e = orch.state.get_ledger()[DIGEST_REL]
    assert e["error_category"] == "agent_timeout"
    assert e["timed_out"] is True and e["usage"] == "unknown"
    assert e["rolled_back"] is True and e["validation_ok"] is False


def test_lint_findings_are_counted_on_a_blocked_ingest(cfg, orch, monkeypatch):
    agent(monkeypatch, edits={
        "log.md": "# log\nentry\n",
        "errors/ORA-600.md": "---\ntype: error-class\n---\n\n"
                             "# ORA-600\n\nSee [[errors/ORA-99999]].\n",
    }, result={**GOOD_RESULT,
               "pages_touched": ["log.md", "errors/ORA-600.md"]})
    with pytest.raises(ValidationError, match="wikilink-broken"):
        orch.ingest(DB, cfg.wiki_repo / DIGEST_REL)
    assert orch.state.get_ledger()[DIGEST_REL]["lint_findings"] >= 1


# ---- redaction ---------------------------------------------------------------

def test_ledger_holds_no_prompt_text_and_no_digest_message(cfg, orch,
                                                           monkeypatch):
    """The ledger is telemetry, not evidence: neither the prompt the agent got
    nor any raw log line out of the digest may end up in it."""
    prompts = []
    agent(monkeypatch, prompts=prompts)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r4")
    raw = (cfg.state_dir / "ingest_ledger.json").read_text()
    assert prompts and prompts[0] not in raw
    for fragment in ("Follow the `ingest` workflow", "AGENTS.md",
                     "Write the result JSON"):
        assert fragment not in raw
    digest = json.loads((cfg.wiki_repo / DIGEST_REL).read_text())
    messages = [g["message"] for s in digest["sources"].values()
                for g in s["notable"]]
    assert messages, "fixture must carry raw log messages to be a real check"
    for msg in messages:
        assert msg not in raw
        assert msg[:40] not in raw


# ---- telemetry failure isolation ---------------------------------------------

def test_a_raising_telemetry_step_does_not_block_the_commit(cfg, orch,
                                                            monkeypatch, capsys):
    def boom(*a, **kw):
        raise RuntimeError("telemetry exploded")
    monkeypatch.setattr(orchestrate, "telemetry_fields", boom)
    agent(monkeypatch)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r5")
    e = orch.state.get_ledger()[DIGEST_REL]
    assert e["status"] == "ingested" and e["commit"]
    assert e["telemetry_error"].startswith("ingest: RuntimeError")
    assert e["run_id"] == "r5"          # the correlation survives the miss
    assert "warning: telemetry capture failed" in capsys.readouterr().err
    assert orch.telemetry_errors


def test_a_telemetry_miss_surfaces_in_the_health_event(cfg, orch, monkeypatch):
    monkeypatch.setattr(orchestrate, "telemetry_fields",
                        lambda *a, **kw: 1 / 0)
    agent(monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_ingest(SimpleNamespace(digest=None, db=DB, date=DAY,
                                          dry_run=False, adapter=None,
                                          lock=Held(cfg.state_dir, "t", 0.0))) == 0
    facts = read_events(cfg.state_dir)[-1]["facts"]
    assert any("ZeroDivisionError" in t for t in facts["telemetry_errors"])


# ---- stats aggregation -------------------------------------------------------

def entry(**over) -> dict:
    base = {"status": "ingested", "task": "ingest", "adapter": "claude",
            "model": "sonnet", "model_tier": "cheap", "duration_s": 10.0,
            "usage": {"input_tokens": 100, "output_tokens": 10,
                      "cost_usd": 0.01},
            "pages_touched": 2, "rolled_back": False, "lint_findings": 0}
    base.update(over)
    return base


def ledger(*entries) -> dict:
    return {f"digests/cdb1/2026-07-{n:02d}.json": e
            for n, e in enumerate(entries, start=1)}


def test_aggregation_math_over_one_group():
    s = collect(ledger(
        entry(duration_s=10.0),
        entry(duration_s=20.0, pages_touched=4, lint_findings=2),
        entry(duration_s=30.0, status="failed", rolled_back=True,
              usage={"input_tokens": 5, "output_tokens": 1, "cost_usd": 0.04}),
    ))
    g, = s["groups"]
    assert (g["task"], g["adapter"], g["model_tier"]) == ("ingest", "claude",
                                                          "cheap")
    assert g["n"] == 3 and g["accepted"] == 2 and g["failed"] == 1
    assert g["median_duration_s"] == 20.0 and g["duration_known_n"] == 3
    assert g["total_cost_usd"] == 0.06 and g["median_cost_usd"] == 0.01
    assert g["cost_known_n"] == 3
    assert g["cost_per_accepted_usd"] == 0.03
    assert g["rollback_rate"] == round(1 / 3, 3)
    assert g["mean_pages_touched"] == round(8 / 3, 2)
    assert g["lint_defect_rate"] == round(1 / 3, 3)
    assert s["runs"] == 3 and s["totals"]["n"] == 3


def test_groups_split_by_task_adapter_and_tier():
    s = collect(ledger(entry(), entry(model_tier="strong"),
                       entry(adapter="codex"), entry(task="report")))
    keys = {(g["task"], g["adapter"], g["model_tier"]) for g in s["groups"]}
    assert keys == {("ingest", "claude", "cheap"), ("ingest", "claude", "strong"),
                    ("ingest", "codex", "cheap"), ("report", "claude", "cheap")}
    assert collect(ledger(entry(), entry(task="report")),
                   task="report")["runs"] == 1


def test_unknown_cost_is_reported_not_estimated():
    s = collect(ledger(entry(usage="unknown"), entry(usage="unknown"),
                       entry(usage={"input_tokens": 1, "output_tokens": 1,
                                    "cost_usd": 0.05})))
    g, = s["groups"]
    assert g["n"] == 3 and g["cost_known_n"] == 1
    assert g["total_cost_usd"] == 0.05 and g["median_cost_usd"] == 0.05
    # no cost anywhere -> the aggregate says so rather than reading as zero
    none = collect(ledger(entry(usage="unknown")))["groups"][0]
    assert none["total_cost_usd"] == "unknown"
    assert none["cost_per_accepted_usd"] == "unknown"
    assert none["cost_known_n"] == 0


def test_old_format_entries_still_load_and_show_as_unknown():
    """Pre-WS6 entries carry status and nothing else this module wants."""
    old = {"status": "ingested", "at": "2026-07-10T00:00:00Z",
           "commit": "abc1234", "content_hash": "h", "notable": True,
           "summary": "s", "incidents": [], "flags": []}
    old_failed = {"status": "failed", "at": "2026-07-11T00:00:00Z",
                  "problems": ["boom"]}
    s = collect({"digests/cdb1/2026-07-10.json": old,
                 "digests/cdb1/2026-07-11.json": old_failed})
    g, = s["groups"]
    assert (g["task"], g["adapter"], g["model_tier"]) == ("unknown",) * 3
    assert g["n"] == 2 and g["accepted"] == 1 and g["failed"] == 1
    for field in ("median_duration_s", "total_cost_usd", "rollback_rate",
                  "mean_pages_touched", "lint_defect_rate"):
        assert g[field] == "unknown"
    assert "unknown" in format_stats(s)


def test_skip_only_entries_are_not_counted_as_runs():
    led = ledger(entry())
    led["digests/cdb1/2026-07-09.json"] = {
        "last_decision": {"outcome": "skip", "reasons": []}}
    assert collect(led)["runs"] == 1


# ---- cheap vs strong ---------------------------------------------------------

def tier_ledger(cheap_n: int, strong_n: int) -> dict:
    return ledger(*([entry(model_tier="cheap")] * cheap_n
                    + [entry(model_tier="strong", usage={
                        "input_tokens": 1, "output_tokens": 1,
                        "cost_usd": 0.2})] * strong_n))


def test_small_samples_get_no_verdict():
    cmp = collect(tier_ledger(MIN_SAMPLE - 1, MIN_SAMPLE))["comparison"]["ingest"]
    assert cmp["verdict"].startswith("insufficient sample")
    assert f"cheap n={MIN_SAMPLE - 1}" in cmp["verdict"]
    assert cmp["cheap"]["n"] == MIN_SAMPLE - 1 and cmp["strong"]["n"] == MIN_SAMPLE
    assert "insufficient sample" in format_stats(
        collect(tier_ledger(MIN_SAMPLE - 1, MIN_SAMPLE)))


def test_a_verdict_appears_once_both_tiers_clear_the_minimum():
    s = collect(tier_ledger(MIN_SAMPLE, MIN_SAMPLE))
    cmp = s["comparison"]["ingest"]
    assert "insufficient" not in cmp["verdict"]
    assert "acceptance cheap=1.0 strong=1.0" in cmp["verdict"]
    assert "cost/accepted cheap=0.01 strong=0.2" in cmp["verdict"]
    assert cmp["min_sample"] == MIN_SAMPLE


# ---- CLI ---------------------------------------------------------------------

def test_stats_cli_prints_a_table_and_json(cfg, orch, monkeypatch, capsys):
    agent(monkeypatch)
    orch.ingest(DB, cfg.wiki_repo / DIGEST_REL, run_id="r6")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    assert cli.cmd_stats(SimpleNamespace(json=False, task=None)) == 0
    text = capsys.readouterr().out
    assert "agent telemetry — 1 attempted run(s)" in text
    assert "ingest" in text and "claude" in text and "strong" in text
    assert "cheap vs strong" in text and "insufficient sample" in text

    assert cli.cmd_stats(SimpleNamespace(json=True, task="ingest")) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["runs"] == 1 and s["task_filter"] == "ingest"
    assert s["groups"][0]["total_cost_usd"] == USAGE["cost_usd"]


def test_stats_on_an_empty_ledger_says_so(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_stats(SimpleNamespace(json=False, task=None)) == 0
    assert "no attempted agent runs" in capsys.readouterr().out


# ---- agent-run events (the primary source) -----------------------------------

_EVENT_IDS = itertools.count()


def event(**over) -> dict:
    """One `.state/agent_runs.jsonl` line as record_agent_run writes it."""
    base = {"event_id": f"e{next(_EVENT_IDS)}", "run_id": "r1",
            "task": "report", "adapter": "pi", "model": "lfm2.5-2.6b",
            "model_tier": "cheap", "mode": "structured", "duration_s": 12.0,
            "timed_out": False, "pages_touched": 1, "validation_ok": True,
            "rolled_back": False, "lint_findings": 0, "attempts": 1,
            "usage_known": True, "input_tokens": 900, "output_tokens": 120,
            "cost_usd": 0.03, "at": "2026-07-27T00:00:00Z"}
    base.update(over)
    return base


def test_non_ingest_stages_are_visible_with_their_cost():
    """The whole point: report/lint/research have no ledger row, so before
    this they were invisible — and they are where the money goes."""
    s = collect({}, events=[
        event(task="ingest", db="cdb1", cost_usd=0.01),
        event(task="report", cost_usd=0.05),
        event(task="lint", mode="agentic", adapter="codex", cost_usd=0.20),
    ])
    assert s["runs"] == 3
    assert {g["task"] for g in s["groups"]} == {"ingest", "report", "lint"}
    assert s["totals"]["total_cost_usd"] == 0.26
    report, = [g for g in s["groups"] if g["task"] == "report"]
    assert report["n"] == 1 and report["total_cost_usd"] == 0.05
    assert "report" in format_stats(s) and "lint" in format_stats(s)


def test_mode_is_a_grouping_dimension():
    s = collect({}, events=[event(task="report", mode="structured"),
                            event(task="report", mode="agentic",
                                  model_tier="strong")])
    assert {(g["task"], g["mode"], g["model_tier"]) for g in s["groups"]} == \
        {("report", "structured", "cheap"), ("report", "agentic", "strong")}


def test_a_rolled_back_stage_is_not_accepted():
    s = collect({}, events=[event(validation_ok=False, rolled_back=True),
                            event(validation_ok=True, rolled_back=False)])
    assert s["totals"]["accepted"] == 1 and s["totals"]["failed"] == 1
    assert s["totals"]["rollback_rate"] == 0.5


def test_attempts_and_usage_known_ratio_are_reported():
    s = collect({}, events=[event(attempts=2), event(attempts=1),
                            event(usage_known=False, cost_usd=None)])
    t = s["totals"]
    assert t["mean_attempts"] == round(4 / 3, 2) and t["max_attempts"] == 2
    assert t["attempts_known_n"] == 3
    assert t["usage_known_rate"] == round(2 / 3, 3)
    assert t["cost_known_n"] == 2


def test_repeated_event_ids_are_counted_once():
    """A folded-in analyst result (ADR-0001) may be shipped twice."""
    e = event(task="report", event_id="dup1")
    assert collect({}, events=[e, dict(e)])["runs"] == 1


def test_a_ledger_ingest_the_log_covers_is_not_double_counted():
    led = ledger(entry(run_id="r7", task="ingest"))
    ev = event(task="ingest", run_id="r7", db="cdb1", cost_usd=0.01)
    s = collect(led, events=[ev])
    assert s["runs"] == 1 and s["totals"]["total_cost_usd"] == 0.01


def test_legacy_ingest_without_an_agent_run_line_still_appears():
    """Pre-telemetry history has no run_id and no agent-run line at all."""
    old = {"status": "ingested", "at": "2026-07-10T00:00:00Z", "commit": "abc"}
    s = collect({"digests/cdb1/2026-07-10.json": old},
                events=[event(task="report")])
    tasks = {g["task"]: g for g in s["groups"]}
    assert set(tasks) == {"unknown", "report"}
    assert tasks["unknown"]["n"] == 1 and tasks["unknown"]["mode"] == "unknown"
    assert tasks["unknown"]["usage_known_rate"] == 0.0
    assert s["runs"] == 2


def test_task_filter_applies_to_both_sources():
    led = ledger(entry(run_id="r9", task="ingest"))
    s = collect(led, task="report", events=[event(task="report"),
                                            event(task="lint")])
    assert s["runs"] == 1 and s["task_filter"] == "report"


def test_cheap_vs_strong_still_gates_on_the_sample():
    s = collect({}, events=[event(model_tier="cheap")] * MIN_SAMPLE
                + [event(model_tier="strong")] * (MIN_SAMPLE - 1))
    assert s["comparison"]["report"]["verdict"].startswith("insufficient sample")


def test_stats_cli_reads_the_agent_run_log(cfg, monkeypatch, capsys):
    lines = [json.dumps(event(task="report", event_id="e1")),
             json.dumps(event(task="lint", event_id="e2", adapter="codex",
                              mode="agentic", cost_usd=0.4))]
    (cfg.state_dir / "agent_runs.jsonl").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_stats(SimpleNamespace(json=True, task=None)) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["runs"] == 2
    assert {g["task"] for g in s["groups"]} == {"report", "lint"}
    assert s["totals"]["total_cost_usd"] == 0.43
