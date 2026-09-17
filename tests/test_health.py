"""Run-health recording, the `dbwiki health` assessment, and retry planning.

Offline throughout: FakeES replays hand-built hits, `run_agent` is
monkeypatched, and every state/wiki path lives under tmp_path. The two
distinctions under test are the ones the 2026-07-13 blackout incident got
wrong — source silence vs collection failure vs unknown, and the refusal to
read recovery out of absent events."""

import json
import shutil
import subprocess
from types import SimpleNamespace

import fixtures as fx
import pytest
import requests

from dbwiki import cli, gitutil, health, orchestrate, transaction
from dbwiki.compactor import Compactor
from dbwiki.harness import HarnessError, NoResultError
from dbwiki.health import (CATEGORIES, assess, categorize, category_of_entry,
                           format_health, health_lines, new_run_id,
                           plan_retries, read_events, run_recorder)
from dbwiki.normalize import UnsupportedSchemaError
from dbwiki.orchestrate import Orchestrator, ValidationError
from dbwiki.lock import Held, LockBusyError

NOW = "2026-07-27T00:00:00Z"
DEAD = "2026-07-12T11:12:48Z"     # the real collector's last event
FRESH = "2026-07-26T20:00:00Z"
DAY = "2026-07-10"
DIGEST_REL = f"digests/cdb1/{DAY}.json"


# ---- helpers -----------------------------------------------------------------

def ts_hits(*timestamps) -> list[dict]:
    return [{"_index": "i", "_id": f"h{n}", "_source": {"@timestamp": t}}
            for n, t in enumerate(timestamps)]


def fake_es(cfg, **by_source):
    return fx.FakeES(cfg, {s: ts_hits(*v) for s, v in by_source.items()})


class BrokenES:
    """ES that cannot be reached — every probe raises, as requests would."""

    def search(self, index, body):
        raise requests.ConnectionError("connection refused")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Real config (real sources/patterns) with every writable path in tmp and
    a wiki git repo holding one digest."""
    # what is on PATH is not what these tests exercise, and a CI runner has
    # none of the adapters: pretend every binary is installed unless a test
    # says otherwise
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    c = fx.fixture_config(tmp_path)
    # these tests drive the agentic ingest path with a fake run_agent; the
    # live config's ingest mode must not rewire them (see test_structured.py).
    # No pi route either, so `assess` runs no LM Studio probe: the dependency
    # tests below hand it a fake getter, and nothing here may touch a socket.
    c.agents = {**c.agents, "mode": "agentic", "adapter": "codex"}
    c.research = {**c.research, "mode": "agentic", "adapter": None}
    # a tmp wiki has no remote; push stays off unless a test asks for it
    c.report = {**c.report, "push": False}
    wiki = c.wiki_repo
    (wiki / "digests" / "cdb1").mkdir(parents=True)
    (wiki / "log.md").write_text("# log\n")
    digest = fx.golden_digest("routine_traffic")
    (wiki / DIGEST_REL).write_text(json.dumps(digest, indent=1))
    git(wiki, "init")
    git(wiki, "config", "user.email", "test@test")
    git(wiki, "config", "user.name", "test")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    c.state_dir.mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def orch(cfg):
    return Orchestrator(cfg, lock=Held(cfg.state_dir, "test", 0.0))


def agent(monkeypatch, *, raises=None, result=None, edits=None):
    """Stand in for run_agent: raise, or apply edits and return a result.
    Telemetry is filled before any failure, exactly as the harness does."""
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=0.5,
                             exit_code=0 if raises is None else 1,
                             timed_out=False, stdout_bytes=8, usage="unknown")
        if raises is not None:
            raise raises
        for rel, text in (edits or {}).items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return result
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)


GOOD_RESULT = {"task": "ingest", "summary": "ingested", "notable": False,
               "pages_touched": ["log.md"]}
GOOD_EDITS = {"log.md": "# log\n[2026-07-27] ingest cdb1\n"}


# ---- categories --------------------------------------------------------------

def test_categorize_maps_every_failure_kind():
    cases = {
        "es_unreachable": requests.ConnectionError("refused"),
        "unsupported_schema": UnsupportedSchemaError("no db field"),
        "no_result": NoResultError("pi finished but wrote no .agent-result.json"),
        "agent_timeout": HarnessError("agent timed out after 900s: pi"),
        "harness_error": HarnessError("pi exited 1: boom"),
        "validation_failed": ValidationError("log.md not updated"),
        "dirty_tree": RuntimeError("wiki working tree has uncommitted changes "
                                   "outside digests/ (notes.md)"),
        "commit_failed": RuntimeError("git commit -m x: fatal: cannot lock ref"),
        "wiki_missing": health.WikiMissingError("/repo/wiki"),
        "unknown": KeyError("something else"),
    }
    assert {k: categorize(v) for k, v in cases.items()} == \
        {k: k for k in cases}
    assert set(cases) <= set(CATEGORIES)


def test_category_of_entry_falls_back_to_problem_text():
    assert category_of_entry({"error_category": "agent_timeout"}) == "agent_timeout"
    assert category_of_entry(
        {"problems": ["pi finished but wrote no .agent-result.json; last..."]}
    ) == "no_result"
    assert category_of_entry({"problems": ["log.md not updated"]}) == "validation_failed"
    assert category_of_entry({}) == "unknown"


def test_run_ids_are_short_and_distinct():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 12 for i in ids)


# ---- recording ---------------------------------------------------------------

def test_recorder_writes_one_event_line(cfg):
    with run_recorder(cfg.state_dir, "compact") as rec:
        rec.note(sources=["alert"])
        rec.add_db("cdb1", events=11, digest=DIGEST_REL, content_hash="abc",
                   watermark_before=None, watermark_after=NOW)
    events = read_events(cfg.state_dir)
    assert len(events) == 1
    e = events[0]
    assert e["schema_version"] == health.HEALTH_SCHEMA_VERSION
    assert (e["command"], e["outcome"], e["error_category"]) == ("compact", "ok", None)
    assert len(e["run_id"]) == 12 and e["started"] <= e["finished"]
    assert e["dbs"] == [{"db": "cdb1", "events": 11, "digest": DIGEST_REL,
                         "content_hash": "abc", "watermark_before": None,
                         "watermark_after": NOW}]
    assert e["facts"] == {"sources": ["alert"]}


def test_recorder_writes_a_start_marker(cfg):
    with run_recorder(cfg.state_dir, "compact") as rec:
        run_id, started = rec.run_id, rec.started
    lines = (cfg.state_dir / "elk" / "run_starts.jsonl").read_text().splitlines()
    assert len(lines) == 1
    start = json.loads(lines[0])
    assert start["event_id"] == f"{run_id}-start"
    assert start["run_id"] == run_id
    assert start["command"] == "compact"
    assert start["started"] == started
    assert isinstance(start["pid"], int) and start["run_host"]
    assert start["schema_version"] == health.HEALTH_SCHEMA_VERSION


def test_start_marker_is_written_even_when_the_run_fails(cfg):
    with pytest.raises(HarnessError):
        with run_recorder(cfg.state_dir, "ingest") as rec:
            run_id = rec.run_id
            raise HarnessError("agent timed out after 900s: pi")
    starts = (cfg.state_dir / "elk" / "run_starts.jsonl").read_text().splitlines()
    assert len(starts) == 1 and json.loads(starts[0])["run_id"] == run_id
    # the normal run_health failure event is still produced, unaffected
    e = read_events(cfg.state_dir)[-1]
    assert (e["outcome"], e["error_category"]) == ("failed", "agent_timeout")


def test_recorder_records_the_category_then_reraises(cfg):
    with pytest.raises(HarnessError):
        with run_recorder(cfg.state_dir, "ingest") as rec:
            rec.add_db("cdb1", digest=DIGEST_REL)
            raise HarnessError("agent timed out after 900s: pi")
    e = read_events(cfg.state_dir)[-1]
    assert (e["outcome"], e["error_category"]) == ("failed", "agent_timeout")
    assert e["dbs"][0]["digest"] == DIGEST_REL


def test_event_log_is_capped(cfg, monkeypatch):
    """The cap has 10% slack, so the log is trimmed periodically rather than
    rewritten on every append; the newest events are always the survivors."""
    monkeypatch.setattr(health, "MAX_EVENTS", 5)
    for i in range(20):
        with run_recorder(cfg.state_dir, "compact") as rec:
            rec.note(i=i)
    seen = [e["facts"]["i"] for e in read_events(cfg.state_dir)]
    assert len(seen) <= 6                       # cap + slack
    assert seen == list(range(20 - len(seen), 20))


def test_a_capped_append_leaves_no_partial_file(cfg, monkeypatch):
    """The ELK shipper tails these logs: a trim must publish the shortened
    log in one rename, never truncate it in place."""
    monkeypatch.setattr(health, "MAX_EVENTS", 5)
    log = cfg.state_dir / health.HEALTH_LOG
    for i in range(30):
        with run_recorder(cfg.state_dir, "compact") as rec:
            rec.note(i=i)
        lines = log.read_text().splitlines()
        assert lines and all(json.loads(ln)["command"] == "compact"
                             for ln in lines)
        assert len(lines) <= 6
        assert list(cfg.state_dir.glob("*.tmp")) == []
        assert list(cfg.state_dir.glob(".*.tmp")) == []


def test_recording_failure_does_not_break_the_command(cfg, capsys, monkeypatch):
    monkeypatch.setattr(health, "_warned", False)
    cfg.state_dir.chmod(0o500)
    try:
        with run_recorder(cfg.state_dir, "compact") as rec:
            rec.note(done=True)  # the command's own work still completes
    finally:
        cfg.state_dir.chmod(0o700)
    assert "run-health recording failed" in capsys.readouterr().err
    assert read_events(cfg.state_dir) == []


def test_interrupted_compaction_is_recorded_as_es_unreachable(cfg):
    """A scan that dies mid-window (ES gone) leaves a categorized event."""
    case = fx.load_case("routine_traffic")
    comp = fx.build_compactor(cfg, case)
    real_scan = comp.es.scan

    def dying_scan(*a, **kw):
        for n, hit in enumerate(real_scan(*a, **kw)):
            if n >= 2:
                raise requests.ConnectionError("connection reset mid-scan")
            yield hit

    comp.es.scan = dying_scan
    w = case["window"]
    with pytest.raises(requests.ConnectionError):
        with run_recorder(cfg.state_dir, "compact") as rec:
            rec.note(window=w)
            comp.compact(case["db"], w["from"], w["to"], w["day"])
    e = read_events(cfg.state_dir)[-1]
    assert (e["command"], e["error_category"]) == ("compact", "es_unreachable")


# ---- failure categories through the real orchestrator ------------------------

def ingest_failure(cfg, orch, monkeypatch, **agent_kw):
    agent(monkeypatch, **agent_kw)
    with pytest.raises(Exception) as exc:  # noqa: PT011 — category is the assertion
        with run_recorder(cfg.state_dir, "ingest"):
            orch.ingest("cdb1", cfg.wiki_repo / DIGEST_REL)
    return read_events(cfg.state_dir)[-1], exc.value


def test_adapter_timeout_is_recorded_and_ledgered(cfg, orch, monkeypatch):
    e, _ = ingest_failure(cfg, orch, monkeypatch,
                          raises=HarnessError("agent timed out after 900s: pi"))
    assert e["error_category"] == "agent_timeout"
    entry = orch.state.get_ledger()[DIGEST_REL]
    assert entry["status"] == "failed" and entry["error_category"] == "agent_timeout"
    assert entry["content_hash"]  # retry can verify the digest it failed on


def test_invalid_agent_result_is_validation_failed(cfg, orch, monkeypatch):
    e, err = ingest_failure(cfg, orch, monkeypatch, edits=GOOD_EDITS,
                            result={"task": "ingest", "summary": "s"})
    assert isinstance(err, ValidationError)
    assert e["error_category"] == "validation_failed"
    assert orch.state.get_ledger()[DIGEST_REL]["error_category"] == "validation_failed"


def test_missing_result_json_without_edits_is_no_result(cfg, orch, monkeypatch):
    e, _ = ingest_failure(cfg, orch, monkeypatch,
                          raises=NoResultError("pi finished but wrote no "
                                               ".agent-result.json"))
    assert e["error_category"] == "no_result"


def test_failed_commit_is_recorded(cfg, orch, monkeypatch):
    real_git = gitutil.git

    def _git(wiki, *args, check=True):
        if args[0] == "commit" and any(a.startswith("ingest:") for a in args):
            raise RuntimeError(f"git {' '.join(args)}: fatal: cannot lock ref")
        return real_git(wiki, *args, check=check)

    monkeypatch.setattr("dbwiki.gitutil.git", _git)
    e, _ = ingest_failure(cfg, orch, monkeypatch, edits=GOOD_EDITS,
                          result=GOOD_RESULT)
    assert e["error_category"] == "commit_failed"


def test_dirty_tree_is_recorded_and_blocks_health(cfg, orch, monkeypatch):
    (cfg.wiki_repo / "notes.md").write_text("human work in progress\n")
    e, _ = ingest_failure(cfg, orch, monkeypatch, edits=GOOD_EDITS,
                          result=GOOD_RESULT)
    assert e["error_category"] == "dirty_tree"
    h = assess(cfg, es=fake_es(cfg, alert=[DEAD]), now=NOW)
    assert any("dirty" in b and "notes.md" in b for b in h["blockers"])
    assert h["exit_code"] == 1


def test_machine_output_is_not_a_dirty_tree(cfg):
    """`dbwiki render-daily` writes html/ and the orchestrator ignores it
    (orchestrate.MACHINE_DIRS); health used to call the same tree a blocker
    and alert on it."""
    for rel in ("html/index.html", "digests/cdb1/2026-07-11.json"):
        p = cfg.wiki_repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("machine output\n")
    h = assess(cfg, es=fake_es(cfg, alert=[FRESH]), now=NOW)
    assert h["wiki"]["stray"] == [] and h["blockers"] == []
    assert transaction.stray_paths(cfg.wiki_repo) == ()


# ---- collection state --------------------------------------------------------

def test_collection_failure_when_no_source_has_recent_events(cfg):
    h = assess(cfg, now=NOW,
               es=fake_es(cfg, alert=[DEAD], listener=[DEAD], dataguard=[DEAD]))
    assert {p["source"]: p["state"] for p in h["sources"]} == {
        "alert": "collection_failure", "listener": "collection_failure",
        "dataguard": "collection_failure"}
    assert all(p["age_hours"] > 340 for p in h["sources"])
    assert any("collection failure" in p for p in h["problems"])


def test_one_quiet_source_beside_a_live_one_is_source_silent(cfg):
    h = assess(cfg, now=NOW,
               es=fake_es(cfg, alert=[FRESH], listener=[DEAD], dataguard=[DEAD]))
    assert {p["source"]: p["state"] for p in h["sources"]} == {
        "alert": "ok", "listener": "source_silent", "dataguard": "source_silent"}
    assert not any("collection failure" in p for p in h["problems"])


def test_unreachable_es_makes_every_source_unknown(cfg):
    h = assess(cfg, es=BrokenES(), now=NOW)
    assert {p["state"] for p in h["sources"]} == {"unknown"}
    assert {p["detail"] for p in h["sources"]} == {"es_unreachable"}
    assert h["exit_code"] == 2  # unreachable ES *and* no local state


def test_unreachable_es_with_local_state_is_only_unhealthy(cfg, orch):
    orch.state.set_watermark("cdb1", "2026-07-26T00:00:00Z")
    h = assess(cfg, es=BrokenES(), now=NOW)
    assert h["exit_code"] == 1


# ---- recovery evidence -------------------------------------------------------

def test_absent_events_are_never_recovery(cfg):
    h = assess(cfg, now=NOW,
               es=fake_es(cfg, alert=[DEAD], listener=[DEAD], dataguard=[DEAD]))
    assert h["recovery_evidence"]["kind"] == "unknown"
    assert "not recovery" in h["recovery_evidence"]["detail"]


def test_resumed_flow_is_evidence(cfg):
    h = assess(cfg, now=NOW, es=fake_es(
        cfg, alert=["2026-07-01T00:00:00Z", FRESH], listener=[DEAD]))
    assert h["recovery_evidence"]["kind"] == "resumed_flow"
    assert "gap" in h["recovery_evidence"]["detail"]


def test_operator_annotation_wins(cfg):
    (cfg.state_dir / health.ANNOTATION_FILE).write_text(
        "2026-07-27 collector restarted on lab-dg1 by dba-oncall\n")
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[DEAD]))
    assert h["recovery_evidence"]["kind"] == "operator_annotation"
    assert "lab-dg1" in h["recovery_evidence"]["detail"]


# ---- assessment --------------------------------------------------------------

def test_stale_watermark_is_a_problem(cfg, orch):
    orch.state.set_watermark("cdb1", "2026-07-12T00:00:00Z")
    orch.state.set_watermark("cdb2", "2026-07-26T23:00:00Z")
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH]))
    marks = {m["db"]: m["stale"] for m in h["watermarks"]}
    assert marks == {"cdb1": True, "cdb2": False}
    assert any("stale watermark: cdb1" in p for p in h["problems"])


def test_missing_wiki_is_a_reported_category_not_a_crash(tmp_path):
    c = fx.fixture_config(tmp_path)          # wiki_repo does not exist
    # the same neutralization the `cfg` fixture applies: the live config's
    # structured mode would make `assess` probe a real LM Studio on
    # localhost:1234, and no test may open a socket
    c.agents = {**c.agents, "mode": "agentic", "adapter": "codex"}
    c.research = {**c.research, "mode": "agentic", "adapter": None}
    c.state_dir.mkdir(parents=True)
    orchestrate.StateStore(c.state_dir).set_ledger_entry(
        DIGEST_REL, {"status": "failed", "at": "2026-07-26T00:00:00Z",
                     "error_category": "validation_failed", "problems": ["x"]})
    h = assess(c, now=NOW, es=fake_es(c, alert=[DEAD]),
               get_json=probe(models()))     # belt and braces: no live getter
    assert h["wiki"]["present"] is False
    assert any("wiki repo missing" in b for b in h["blockers"])
    assert h["failures"][0]["digest_present"] == "wiki_missing"
    assert h["exit_code"] == 1


def test_json_snapshot_shape(cfg, orch, monkeypatch, capsys):
    agent(monkeypatch, edits=GOOD_EDITS, result=GOOD_RESULT)
    orch.ingest("cdb1", cfg.wiki_repo / DIGEST_REL)
    # cmd_health builds its own client, so the real one is stubbed at the class
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr("dbwiki.es.ES.__init__", lambda self, *a, **kw: None)
    monkeypatch.setattr("dbwiki.es.ES.search",
                        lambda self, index, body: {"hits": {"hits": ts_hits(DEAD)}})
    rc = cli.cmd_health(SimpleNamespace(json=True))
    h = json.loads(capsys.readouterr().out)
    assert rc == h["exit_code"] == 1
    assert set(h) == {
        "schema_version", "generated_at", "stale_hours", "healthy", "exit_code",
        "wiki", "last_success", "stale_stages", "dependencies", "watermarks",
        "failures", "backlog", "sources", "recovery_evidence", "blockers",
        "problems", "events_recorded", "queue", "quiet", "exchange"}
    assert h["last_success"]["ingestion"]["from"] == "ledger"
    assert h["last_success"]["report"] is None
    assert {p["state"] for p in h["sources"]} == {"collection_failure"}


def test_human_output_names_every_section(cfg):
    text = format_health(assess(cfg, now=NOW, es=fake_es(cfg, alert=[DEAD])))
    for section in ("collection", "last success", "watermarks",
                    "failed ingests", "digest backlog", "blockers"):
        assert f"\n{section}" in text
    assert "UNHEALTHY" in text and "collection_failure" in text


def test_health_lines_are_short_and_labelled(cfg):
    lines = health_lines(assess(cfg, now=NOW, es=fake_es(cfg, alert=[DEAD])))
    assert 2 <= len(lines) <= 4
    assert lines[0].startswith("- sources: alert=collection_failure")
    assert "recovery evidence: unknown" in lines[1]


def test_report_prompt_carries_health_and_survives_its_absence(cfg, orch,
                                                               monkeypatch):
    prompts = []

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        prompts.append(prompt)
        (wiki / "log.md").write_text(f"# log\nreport {len(prompts)}\n")
        (wiki / "reports").mkdir(exist_ok=True)
        (wiki / f"reports/{DAY}.md").write_text(f"# report {len(prompts)}\n")
        return {"task": "report", "summary": "s", "notable": False,
                "pages_touched": ["log.md", f"reports/{DAY}.md"]}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    window = (f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z")
    orch.report(DAY, window, [], health=["- sources: alert=collection_failure"])
    assert "Collection health" in prompts[0]
    assert "never report a collection gap as a database outage" in prompts[0]
    orch.report(DAY, window, [])  # absent health info must not break report()
    assert "Collection health" not in prompts[1]


# ---- retry -------------------------------------------------------------------

def seed_failure(orch, category="harness_error", content_hash=None, digest=None):
    d = digest or json.loads((orch.wiki / DIGEST_REL).read_text())
    orch.state.set_ledger_entry(DIGEST_REL, {
        "status": "failed", "at": "2026-07-26T12:00:00Z",
        "content_hash": content_hash or Compactor.content_hash(d),
        "error_category": category, "problems": ["pi exited 1"]})


def test_dry_run_prints_the_plan_and_writes_nothing(cfg, orch, monkeypatch, capsys):
    seed_failure(orch)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    rc = cli.cmd_retry(SimpleNamespace(db=None, dry_run=True, lock=None))
    out = capsys.readouterr().out
    assert rc == 0 and f"retry {DIGEST_REL}" in out and "harness_error" in out
    assert orch.state.get_ledger()[DIGEST_REL]["status"] == "failed"
    assert read_events(cfg.state_dir) == []


def test_retry_reingests_and_records(cfg, orch, monkeypatch, capsys):
    agent(monkeypatch, raises=HarnessError("pi exited 1: boom"))
    with pytest.raises(HarnessError):
        orch.ingest("cdb1", cfg.wiki_repo / DIGEST_REL)
    assert orch.state.get_ledger()[DIGEST_REL]["status"] == "failed"

    agent(monkeypatch, edits=GOOD_EDITS, result=GOOD_RESULT)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_retry(SimpleNamespace(db=None, dry_run=False,
                                  lock=Held(cfg.state_dir, "t", 0.0))) == 0
    entry = orch.state.get_ledger()[DIGEST_REL]
    assert entry["status"] == "ingested" and entry["commit"]
    e = read_events(cfg.state_dir)[-1]
    assert e["command"] == "retry" and e["outcome"] == "ok"
    assert e["dbs"][0]["outcome"] == "ingested"
    assert e["dbs"][0]["retry_of"] == "harness_error"
    assert "retry ok" in capsys.readouterr().out


def test_retry_preserves_unrelated_wiki_changes(cfg, orch, monkeypatch):
    """The retry goes through the normal ingest path, so an unrelated
    uncommitted edit stops it rather than being rolled away."""
    seed_failure(orch)
    (cfg.wiki_repo / "notes.md").write_text("human work in progress\n")
    agent(monkeypatch, edits=GOOD_EDITS, result=GOOD_RESULT)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.cmd_retry(SimpleNamespace(db=None, dry_run=False,
                                  lock=Held(cfg.state_dir, "t", 0.0))) == 1
    assert (cfg.wiki_repo / "notes.md").exists()
    assert read_events(cfg.state_dir)[-1]["dbs"][0]["error_category"] == "dirty_tree"


def test_non_retryable_category_is_skipped(cfg, orch):
    seed_failure(orch, category="dirty_tree")
    plan, skipped = plan_retries(cfg, orch.state)
    assert plan == []
    assert skipped[0]["reason"] == "not retryable (dirty_tree)"


def test_hash_mismatch_refuses(cfg, orch):
    seed_failure(orch, content_hash="0000deadbeef0000")
    plan, skipped = plan_retries(cfg, orch.state)
    assert plan == []
    assert "content hash changed" in skipped[0]["reason"]


def test_missing_digest_is_skipped_not_retried(cfg, orch):
    seed_failure(orch)
    (cfg.wiki_repo / DIGEST_REL).unlink()
    plan, skipped = plan_retries(cfg, orch.state)
    assert plan == [] and skipped[0]["reason"] == "digest file missing"


def test_plan_filters_by_db(cfg, orch):
    seed_failure(orch)
    plan, _ = plan_retries(cfg, orch.state, db="cdb2")
    assert plan == []
    plan, _ = plan_retries(cfg, orch.state, db="cdb1")
    assert [p["digest"] for p in plan] == [DIGEST_REL]


# ---- agent-run recording (elk/ shipper source) --------------------------------

def test_record_agent_run_flattens_usage_and_adds_context(tmp_path):
    fields = {"run_id": "r1", "task": "ingest", "adapter": "pi",
              "model": "gemma", "model_tier": "cheap", "duration_s": 1.5,
              "usage": {"input_tokens": 610, "output_tokens": 6,
                        "cost_usd": 0.0}}
    tele = {"exit_code": 0, "stdout_bytes": 999, "prompt_bytes": 1234}
    event_id = health.record_agent_run(tmp_path, fields, tele, db="cdb1",
                                       mode="structured")
    ev = json.loads((tmp_path / health.AGENT_LOG).read_text())
    assert ev["event_id"] == event_id
    assert ev["input_tokens"] == 610 and ev["output_tokens"] == 6
    assert ev["usage_known"] is True and "usage" not in ev
    assert ev["db"] == "cdb1" and ev["mode"] == "structured"
    assert ev["prompt_bytes"] == 1234 and ev["exit_code"] == 0
    assert ev["event_id"] and ev["at"].endswith("Z")
    assert fields["usage"]["input_tokens"] == 610  # caller's dict untouched


def test_record_agent_run_keeps_steps_out_of_the_ledger_line(tmp_path):
    """The harness now captures per-turn steps with text previews. The agent-run
    log is scalars only (elk/ indexes it), so it takes named keys off the raw
    telemetry and must not grow a step list or a preview."""
    tele = {"exit_code": 0, "steps": [{"seq": 0, "kind": "tool", "name": "read",
                                       "preview": "PREVIEW-TEXT",
                                       "usage": None}],
            "steps_truncated": False}
    health.record_agent_run(tmp_path, {"run_id": "r3", "task": "ingest",
                                       "usage": "unknown"}, tele)
    line = (tmp_path / health.AGENT_LOG).read_text()
    assert "steps" not in json.loads(line)
    assert "PREVIEW-TEXT" not in line


def test_record_agent_run_with_unknown_usage(tmp_path):
    health.record_agent_run(tmp_path, {"run_id": "r2", "task": "lint",
                                       "usage": "unknown"})
    ev = json.loads((tmp_path / health.AGENT_LOG).read_text())
    assert ev["usage_known"] is False
    assert "input_tokens" not in ev and "cost_usd" not in ev


def test_agent_run_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "MAX_AGENT_EVENTS", 3)
    for i in range(5):
        health.record_agent_run(tmp_path, {"run_id": f"r{i}", "task": "t",
                                           "usage": "unknown"})
    lines = (tmp_path / health.AGENT_LOG).read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[-1])["run_id"] == "r4"


# ---- analyst queue block (ADR-0001) --------------------------------------------

from dbwiki import queue as queue_mod  # noqa: E402 — grouped with this section

QWINDOW = ("2026-07-27T18:00:00Z", "2026-07-27T20:15:00Z")


def enqueue(wiki, run_id="r1", day="2026-07-27"):
    return queue_mod.enqueue_request(
        wiki, run_id=run_id, kind="report", day=day, suffix="",
        window=QWINDOW, notable_dbs=["cdb1"], prompt="Task: report.\n",
        push=False, lock=Held(wiki, "test", 0.0))


def test_queue_block_absent_when_queue_dir_does_not_exist(cfg):
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH]))
    assert h["queue"] == {
        "pending_count": 0, "oldest_pending_hours": None, "claimed_count": 0,
        "stale_claimed": [], "failed_count": 0, "failed_by_category": {}}
    assert h["exit_code"] == 0


def test_pending_backlog_is_informational_only(cfg):
    enqueue(cfg.wiki_repo, run_id="r1")
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH]))
    assert h["queue"]["pending_count"] == 1
    assert h["queue"]["oldest_pending_hours"] is not None
    assert h["exit_code"] == 0                    # unaffected: no ingest/watermark issue
    assert not any("analyst queue" in p for p in h["problems"])


def test_stale_claimed_request_is_a_problem(cfg):
    enqueue(cfg.wiki_repo, run_id="r1")
    claimed = queue_mod.claim(cfg.wiki_repo, "analyst-host", push=False,
                              lock=Held(cfg.state_dir, "test", 0.0))
    # backdate the claim past the default 26h threshold, as a crashed
    # analyst would leave it — health never mints its own claims
    path = cfg.wiki_repo / "queue" / "claimed" / claimed["_file"]
    rec = json.loads(path.read_text())
    rec["claimed_at"] = "2026-07-25T00:00:00Z"    # ~60h before NOW
    path.write_text(json.dumps(rec))
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH]))
    assert len(h["queue"]["stale_claimed"]) == 1
    assert h["queue"]["stale_claimed"][0]["claimed_by"] == "analyst-host"
    assert any("analyst queue: claimed" in p for p in h["problems"])
    assert h["exit_code"] == 1


def test_analyst_stale_hours_overrides_the_default(cfg):
    enqueue(cfg.wiki_repo, run_id="r1")
    claimed = queue_mod.claim(cfg.wiki_repo, "analyst-host", push=False,
                              lock=Held(cfg.state_dir, "test", 0.0))
    # one hour old — well under the 26h default, but over an operator-lowered
    # threshold
    path = cfg.wiki_repo / "queue" / "claimed" / claimed["_file"]
    rec = json.loads(path.read_text())
    rec["claimed_at"] = "2026-07-26T23:00:00Z"
    path.write_text(json.dumps(rec))
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH],
                                        dataguard=[FRESH]))
    assert h["queue"]["stale_claimed"] == []      # default 26h: not stale yet
    cfg.analyst = {"stale_hours": 0.5}
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH],
                                        dataguard=[FRESH]))
    assert len(h["queue"]["stale_claimed"]) == 1
    assert h["exit_code"] == 1


def test_failed_requests_are_a_problem_with_categories(cfg):
    enqueue(cfg.wiki_repo, run_id="r1")
    claimed = queue_mod.claim(cfg.wiki_repo, "analyst-host", push=False,
                              lock=Held(cfg.state_dir, "test", 0.0))
    queue_mod.fail_request(cfg.wiki_repo, claimed, "agent_timeout", push=False,
                           lock=Held(cfg.state_dir, "test", 0.0))
    reclaimed = queue_mod.claim(cfg.wiki_repo, "analyst-host", push=False,
                              lock=Held(cfg.state_dir, "test", 0.0))
    queue_mod.fail_request(cfg.wiki_repo, reclaimed, "agent_timeout", push=False,
                           lock=Held(cfg.state_dir, "test", 0.0))
    h = assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH]))
    assert h["queue"]["failed_count"] == 1
    assert h["queue"]["failed_by_category"] == {"agent_timeout": 1}
    assert any("analyst queue: 1 failed" in p for p in h["problems"])
    assert h["exit_code"] == 1


def test_format_health_names_the_analyst_queue_section(cfg):
    enqueue(cfg.wiki_repo, run_id="r1")
    text = format_health(assess(cfg, now=NOW, es=fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH])))
    assert "\nanalyst queue" in text
    assert "pending: 1" in text


# ---- quiet incidents ---------------------------------------------------------

from fixtures.incident_pages import incident_page  # noqa: E402 — grouped with this section

QUIET_DAY = "2026-06-17"          # 40 days before NOW
LOUD_DAY = "2026-07-26"           # the day before NOW


def live_es(cfg):
    return fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH])


def incident_with_code(wiki, slug, *, code, day, status="open", db="cdb1"):
    """One open case and the error page that says when its code was last seen.
    The `## Occurrences` table is the only error-to-database-to-day join the
    wiki holds, so a quiet case needs both halves written.

    The pages are committed because an uncommitted one is a dirty wiki tree,
    which is a blocker in its own right and would hide what these tests
    assert."""
    (wiki / "incidents").mkdir(exist_ok=True)
    codes = (code,) if code else ()
    (wiki / "incidents" / f"{slug}.md").write_text(
        incident_page(db, slug, status=status, error_codes=codes))
    if code:
        (wiki / "errors").mkdir(exist_ok=True)
        (wiki / "errors" / f"{code}.md").write_text(
            f"---\ntype: error\n---\n\n# {code}\n\n## Occurrences\n\n"
            f"| Day | DB | Note | Evidence |\n|---|---|---|---|\n"
            f"| {day} | {db} | seen | digests/{db}/{day}.json |\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", f"case {slug}")


def test_an_open_incident_whose_code_stopped_appearing_is_quiet(cfg):
    incident_with_code(cfg.wiki_repo, "2026-06-01-cdb1-undo",
                       code="ORA-1555", day=QUIET_DAY)
    h = assess(cfg, now=NOW, es=live_es(cfg))
    assert h["quiet"]["count"] == 1
    assert h["quiet"]["oldest"] == [{"slug": "2026-06-01-cdb1-undo",
                                     "last_seen": QUIET_DAY}]


def test_an_incident_whose_code_was_seen_yesterday_is_not_quiet(cfg):
    incident_with_code(cfg.wiki_repo, "2026-07-20-cdb1-undo",
                       code="ORA-1555", day=LOUD_DAY)
    h = assess(cfg, now=NOW, es=live_es(cfg))
    assert h["quiet"] == {"count": 0, "oldest": []}


def test_a_resolved_incident_is_never_quiet(cfg):
    incident_with_code(cfg.wiki_repo, "2026-01-02-cdb1-old",
                       code="ORA-1555", day="2026-01-03", status="resolved")
    h = assess(cfg, now=NOW, es=live_es(cfg))
    assert h["quiet"]["count"] == 0


def test_an_open_incident_linking_no_code_is_not_quiet(cfg):
    incident_with_code(cfg.wiki_repo, "2026-06-01-cdb1-mystery",
                       code=None, day=QUIET_DAY)
    (cfg.wiki_repo / "errors").mkdir(exist_ok=True)
    h = assess(cfg, now=NOW, es=live_es(cfg))
    assert h["quiet"] == {"count": 0, "oldest": []}


def test_format_health_prints_the_quiet_count_and_the_oldest_cases(cfg):
    text = format_health(assess(cfg, now=NOW, es=live_es(cfg)))
    assert "  quiet: 0\n" in text
    incident_with_code(cfg.wiki_repo, "2026-06-01-cdb1-undo",
                       code="ORA-1555", day=QUIET_DAY)
    text = format_health(assess(cfg, now=NOW, es=live_es(cfg)))
    assert "quiet: 1 open incident(s) with no linked code seen for 14+ days" in text
    assert f"  2026-06-01-cdb1-undo (last seen {QUIET_DAY})" in text


def test_a_quiet_incident_moves_neither_problems_nor_the_exit_code(cfg):
    before = assess(cfg, now=NOW, es=live_es(cfg))
    incident_with_code(cfg.wiki_repo, "2026-06-01-cdb1-undo",
                       code="ORA-1555", day=QUIET_DAY)
    after = assess(cfg, now=NOW, es=live_es(cfg))
    assert after["quiet"]["count"] == 1
    assert after["problems"] == before["problems"] == []
    assert after["exit_code"] == before["exit_code"] == 0


# ---- stage staleness ---------------------------------------------------------

OLD = "2026-07-01T00:00:00Z"          # 26 days before NOW


def seed_success(cfg, command, finished, **facts):
    """One successful run-health event, as `run_recorder` would have left it."""
    health.record_run(cfg.state_dir, {
        "schema_version": 1, "run_id": new_run_id(), "command": command,
        "started": finished, "finished": finished, "outcome": "ok",
        "error_category": None, "dbs": [], "facts": facts})


def fresh_es(cfg):
    return fake_es(cfg, alert=[FRESH], listener=[FRESH], dataguard=[FRESH])


def test_a_stage_that_stopped_succeeding_is_a_problem(cfg):
    """"reports failing for a week" used to read as healthy: last_success was
    printed but never compared to now."""
    seed_success(cfg, "report", OLD, report="reports/2026-07-01.md")
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    assert [s["task"] for s in h["stale_stages"]] == ["report"]
    s = h["stale_stages"][0]
    assert s["at"] == OLD and s["age_h"] == 624.0 and s["threshold_h"] == 26
    assert any("stale stage: report" in p for p in h["problems"])
    assert h["exit_code"] == 1 and h["healthy"] is False


def test_a_stage_that_never_ran_is_not_a_problem(cfg):
    """A fresh install (or one that never enabled research) must not be told
    it is broken."""
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    assert h["stale_stages"] == [] and h["exit_code"] == 0


def test_a_recent_success_is_not_stale(cfg):
    seed_success(cfg, "report", "2026-07-26T18:00:00Z",
                 report="reports/2026-07-26.md")
    assert assess(cfg, now=NOW, es=fresh_es(cfg))["stale_stages"] == []


def test_a_failed_tick_still_credits_the_stages_that_worked(cfg):
    """`cmd_run` marks the whole tick failed when any single db fails, while
    every other db still commits and the report still lands. Reading stage
    success out of the tick's `outcome` froze compaction/ingestion/report on
    the first chronically broken db and then fired permanent `stage_stale`
    problems at a working pipeline."""
    health.record_run(cfg.state_dir, {
        "schema_version": 1, "run_id": new_run_id(), "command": "run",
        "started": FRESH, "finished": FRESH, "outcome": "failed",
        "error_category": "agent_timeout",
        "dbs": [{"db": "cdb1", "events": 12, "watermark_after": FRESH,
                 "validation": "ok", "outcome": "ingested", "commit": "abc1234"},
                {"db": "cdb2", "error_category": "agent_timeout",
                 "error": "the adapter never answered"}],
        "facts": {"report": f"reports/{DAY}.md"}})
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    got = {k: (v or {}).get("at") for k, v in h["last_success"].items()}
    assert got["compaction"] == got["ingestion"] == got["report"] == FRESH
    assert h["last_success"]["ingestion"]["detail"] == "cdb1"   # not cdb2
    assert h["stale_stages"] == []
    assert not any("stale stage" in p for p in h["problems"])


def test_lint_and_research_get_a_weekly_budget(cfg):
    """192h, not 26: lint and research are weekly, and a daily threshold on
    them would alert every week by design."""
    seed_success(cfg, "lint", "2026-07-24T00:00:00Z", findings=0)
    seed_success(cfg, "research", "2026-07-01T00:00:00Z", pages=3)
    stale = {s["task"]: s for s in
             assess(cfg, now=NOW, es=fresh_es(cfg))["stale_stages"]}
    assert set(stale) == {"research"}
    assert stale["research"]["threshold_h"] == 192


def test_stage_stale_hours_is_configurable(cfg):
    cfg.health = {**cfg.health, "stage_stale_hours": {"report": 1}}
    seed_success(cfg, "report", "2026-07-26T20:00:00Z",
                 report="reports/2026-07-26.md")
    stale = assess(cfg, now=NOW, es=fresh_es(cfg))["stale_stages"]
    assert [(s["task"], s["threshold_h"]) for s in stale] == [("report", 1.0)]


def test_the_stale_stage_hint_names_the_command(cfg):
    seed_success(cfg, "report", OLD, report="reports/2026-07-01.md")
    text = format_health(assess(cfg, now=NOW, es=fresh_es(cfg)))
    assert "STALE STAGE report" in text
    assert "uv run dbwiki report" in text and ".state/cron.log" in text


# ---- dependencies ------------------------------------------------------------

PI_CFG = {"provider": "unsloth", "cheap": "nemotron-30b",
          "strong": "qwen3.8-27b", "base_url": "http://unsloth.test:8888"}


def pi_cfg(cfg, **over):
    """A config whose structured stages reach the local model server."""
    cfg.agents = {**cfg.agents, "mode": "structured", "pi": {**PI_CFG, **over}}
    return cfg


PORTAL_URL = "http://127.0.0.1:8765/api/health"
REVISION = "6765a05e3c1d4b8f9a2e7c0d5b6a1f3e8d4c2b90"


def portal_cfg(cfg, **over):
    """A config with a `portal:` block, which is what the probe keys on."""
    cfg.portal = {**cfg.portal, "bind": "127.0.0.1:8765", **over}
    cfg.portal_configured = True
    return cfg


def pi_table(monkeypatch, tmp_path, providers: dict):
    """Stand in for pi's own provider table (`~/.pi/agent/models.json`)."""
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"providers": providers}))
    monkeypatch.setattr(health, "PI_MODELS_JSON", path)
    return path


def models(*entries):
    """An /api/v0/models payload: (id, state, context) triples."""
    return {"data": [{"id": i, "state": st, "loaded_context_length": ctx}
                     for i, st, ctx in entries]}


def probe(payload=None, raises=None):
    """A fake HTTP getter — no test may reach a real model server."""
    seen = []

    def _get(url, timeout=3.0, headers=None):
        seen.append((url, timeout, headers))
        if raises is not None:
            raise raises
        return payload

    _get.seen = seen
    return _get


def deps_of(cfg, get_json):
    return assess(cfg, now=NOW, es=fresh_es(cfg),
                  get_json=get_json)["dependencies"]


def test_model_server_is_not_probed_when_nothing_uses_pi(cfg):
    get = probe(raises=AssertionError("must not be probed"))
    ms = deps_of(cfg, get)["model_server"]
    assert ms["checked"] is False and get.seen == []


def test_an_unreachable_model_server_is_a_dependency_failure(cfg):
    get = probe(raises=requests.ConnectionError("connection refused"))
    pi_cfg(cfg)
    h = assess(cfg, now=NOW, es=fresh_es(cfg), get_json=get)
    ms = h["dependencies"]["model_server"]
    assert ms["reachable"] is False and ms["url"] == PI_CFG["base_url"]
    assert [p["detail"] for p in ms["problems"]] == ["model_server_unreachable"]
    assert [(p["check"], p["detail"]) for p in h["dependencies"]["problems"]] \
        == [("model_server", "model_server_unreachable")]
    assert any("dependency: model_server" in p for p in h["problems"])
    assert h["exit_code"] == 1                    # a failure, not a blocker
    # both endpoints are tried, LM Studio's own API first
    assert [u for u, _, _ in get.seen] == [
        f"{PI_CFG['base_url']}/api/v0/models", f"{PI_CFG['base_url']}/v1/models"]


def test_a_configured_model_missing_from_the_server_is_a_problem(cfg):
    get = probe(models(("nemotron-30b", "loaded", 32768)))
    ms = deps_of(pi_cfg(cfg), get)["model_server"]
    assert [p["detail"] for p in ms["problems"]] == ["model_server_model_missing"]
    assert "qwen3.8-27b" in ms["problems"][0]["message"]


def test_a_thinking_suffix_on_a_tier_is_not_part_of_the_model_id(cfg):
    """`nemotron-30b:off` is pi's thinking level on the served model, so
    the server is asked about `nemotron-30b` and its context is checked."""
    pi_cfg(cfg)
    cfg.agents["pi"]["cheap"] = "nemotron-30b:off"
    get = probe(models(("nemotron-30b", "loaded", 8192),
                       ("qwen3.8-27b", "not-loaded", None)))
    ms = deps_of(cfg, get)["model_server"]
    assert ms["configured_models"] == ["nemotron-30b", "qwen3.8-27b"]
    assert [p["detail"] for p in ms["problems"]] == ["model_server_context"]


def test_a_small_loaded_context_is_a_problem(cfg):
    get = probe(models(("nemotron-30b", "loaded", 8192),
                       ("qwen3.8-27b", "not-loaded", None)))
    ms = deps_of(pi_cfg(cfg), get)["model_server"]
    assert [p["detail"] for p in ms["problems"]] == ["model_server_context"]
    assert "8192" in ms["problems"][0]["message"]


def test_min_context_is_configurable(cfg):
    cfg.health = {**cfg.health, "min_context": 4096}
    get = probe(models(("nemotron-30b", "loaded", 8192),
                       ("qwen3.8-27b", "loaded", 8192)))
    assert deps_of(pi_cfg(cfg), get)["model_server"]["problems"] == []


def test_a_healthy_model_server_reports_jit_eviction_as_a_note(cfg):
    """JIT loads on the next request, so "not loaded" is normal."""
    get = probe(models(("nemotron-30b", "loaded", 32768),
                       ("qwen3.8-27b", "not-loaded", None)))
    pi_cfg(cfg)
    h = assess(cfg, now=NOW, es=fresh_es(cfg), get_json=get)
    ms = h["dependencies"]["model_server"]
    assert ms["reachable"] is True and ms["problems"] == []
    assert [m["id"] for m in ms["loaded"]] == ["nemotron-30b"]
    assert any("qwen3.8-27b not loaded" in n for n in ms["notes"])
    assert h["exit_code"] == 0


def test_without_api_v0_the_line_reports_served_context_not_load_state(cfg):
    """/v1/models is a catalogue, not a load report: printing all of it as
    "loaded" would be a claim the server never made."""
    def get(url, timeout=3.0, headers=None):
        if url.endswith("/api/v0/models"):
            raise requests.HTTPError("404")
        return {"data": [{"id": "nemotron-30b", "context_length": 98176},
                         {"id": "qwen3.8-27b"}]}

    pi_cfg(cfg)
    h = assess(cfg, now=NOW, es=fresh_es(cfg), get_json=get)
    assert h["dependencies"]["model_server"]["state_known"] is False
    line = next(ln for ln in format_health(h).splitlines() if "model svr" in ln)
    assert "served ctx: nemotron-30b@98176" in line and "qwen3.8-27b@" not in line


def test_the_openai_endpoint_is_the_fallback(cfg):
    calls = []

    def get(url, timeout=3.0, headers=None):
        calls.append(url)
        if url.endswith("/api/v0/models"):
            raise requests.HTTPError("404")
        return {"data": [{"id": "nemotron-30b"}, {"id": "qwen3.8-27b"}]}

    ms = deps_of(pi_cfg(cfg), get)["model_server"]
    assert ms["reachable"] is True and ms["problems"] == []
    assert any("loaded state unknown" in n for n in ms["notes"])
    assert len(calls) == 2


def test_a_v1_base_url_is_probed_at_the_server_root(cfg):
    """An OpenAI-compatible base URL carries /v1; the probe paths add their
    own, so the suffix has to come off before they are appended."""
    get = probe(raises=requests.ConnectionError("refused"))
    deps_of(pi_cfg(cfg, base_url="http://unsloth.test:8888/v1"), get)
    assert [u for u, _, _ in get.seen] == [
        "http://unsloth.test:8888/api/v0/models",
        "http://unsloth.test:8888/v1/models"]


def test_the_server_and_key_come_from_pis_own_provider_table(cfg, monkeypatch,
                                                             tmp_path):
    """With no base_url in the dbwiki config, the probe follows pi: same
    server, same key, so repointing pi repoints the probe."""
    pi_table(monkeypatch, tmp_path, {"unsloth": {
        "baseUrl": "http://from-pi.test:8888/v1", "apiKey": "sk-from-pi"}})
    get = probe(raises=requests.ConnectionError("refused"))
    cfg.agents = {**cfg.agents, "mode": "structured",
                  "pi": {k: v for k, v in PI_CFG.items() if k != "base_url"}}
    ms = deps_of(cfg, get)["model_server"]
    assert ms["url"] == "http://from-pi.test:8888"
    assert {h["Authorization"] for _, _, h in get.seen} == {"Bearer sk-from-pi"}


def test_an_unknown_or_unreadable_pi_table_is_not_a_finding(cfg, monkeypatch,
                                                           tmp_path):
    """A provider pi does not list falls back to the default server, with no
    key and no exception."""
    pi_table(monkeypatch, tmp_path, {"lmstudio": {"baseUrl": "http://x:1234"}})
    get = probe(raises=requests.ConnectionError("refused"))
    cfg.agents = {**cfg.agents, "mode": "structured",
                  "pi": {k: v for k, v in PI_CFG.items() if k != "base_url"}}
    ms = deps_of(cfg, get)["model_server"]
    assert ms["url"] == health.DEFAULT_MODEL_SERVER_URL
    assert {h for _, _, h in get.seen} == {None}


def test_api_key_env_overrides_the_key_from_pis_table(cfg, monkeypatch,
                                                      tmp_path):
    """The repo keeps real credentials in the environment; naming an env var
    has to win over whatever pi's table holds."""
    pi_table(monkeypatch, tmp_path, {"unsloth": {"apiKey": "sk-from-pi"}})
    monkeypatch.setenv("DBWIKI_PI_KEY", "sk-from-env")
    get = probe(raises=requests.ConnectionError("refused"))
    deps_of(pi_cfg(cfg, api_key_env="DBWIKI_PI_KEY"), get)
    assert {h["Authorization"] for _, _, h in get.seen} == {"Bearer sk-from-env"}


def test_an_unset_api_key_env_falls_back_to_pis_table(cfg, monkeypatch,
                                                      tmp_path):
    pi_table(monkeypatch, tmp_path, {"unsloth": {"apiKey": "sk-from-pi"}})
    monkeypatch.delenv("DBWIKI_PI_KEY", raising=False)
    get = probe(raises=requests.ConnectionError("refused"))
    deps_of(pi_cfg(cfg, api_key_env="DBWIKI_PI_KEY"), get)
    assert {h["Authorization"] for _, _, h in get.seen} == {"Bearer sk-from-pi"}


def test_the_portal_is_not_probed_without_a_portal_block(cfg):
    cfg.portal_configured = False
    get = probe(raises=AssertionError("must not be probed"))
    h = assess(cfg, now=NOW, es=fresh_es(cfg), get_json=get)
    assert h["dependencies"]["portal"]["checked"] is False and get.seen == []
    # this test's own tmp_path carries "portal", and the disk line prints it,
    # so the claim has to be line-shaped: no line is about the workbench.
    assert [ln for ln in format_health(h).splitlines()
            if "portal" in ln and not ln.startswith("  disk")] == []


def test_a_reachable_portal_carries_its_revision_and_actor(cfg):
    get = probe({"ok": True, "bind": "127.0.0.1:8765", "revision": REVISION,
                 "actor": "dba@example.com"})
    h = assess(portal_cfg(cfg), now=NOW, es=fresh_es(cfg), get_json=get)
    pt = h["dependencies"]["portal"]
    assert pt["reachable"] is True and pt["problems"] == []
    assert get.seen == [(PORTAL_URL, health.DEP_TIMEOUT, None)]
    assert pt["url"] == PORTAL_URL
    assert pt["revision"] == REVISION and pt["actor"] == "dba@example.com"
    assert f"  portal      up {PORTAL_URL} [wiki {REVISION[:7]} as " \
           f"dba@example.com]" in format_health(h)


def test_an_unreachable_portal_is_a_dependency_failure(cfg):
    get = probe(raises=requests.ConnectionError("connection refused"))
    h = assess(portal_cfg(cfg), now=NOW, es=fresh_es(cfg), get_json=get)
    pt = h["dependencies"]["portal"]
    assert pt["reachable"] is False
    assert [p["detail"] for p in pt["problems"]] == ["portal_unreachable"]
    assert [(p["check"], p["detail"]) for p in h["dependencies"]["problems"]] \
        == [("portal", "portal_unreachable")]
    assert any("dependency: portal" in p for p in h["problems"])
    assert h["exit_code"] == 1
    out = format_health(h)
    assert f"  portal      UNREACHABLE {PORTAL_URL}" in out
    assert "systemctl --user restart dbwiki-portal" in out


def test_a_portal_address_answering_with_something_else_is_unreachable(cfg):
    """Something is listening but it is not the workbench. The operator's next
    move is identical, so the detail is too and only the message differs."""
    get = probe(["not", "a", "workbench"])
    pt = deps_of(portal_cfg(cfg), get)["portal"]
    assert pt["reachable"] is False
    assert [p["detail"] for p in pt["problems"]] == ["portal_unreachable"]
    refused = probe(raises=requests.ConnectionError("connection refused"))
    nothing_there = deps_of(portal_cfg(cfg), refused)["portal"]
    assert pt["problems"][0]["message"] != \
        nothing_there["problems"][0]["message"]


def test_a_hung_portal_does_not_fail_the_assessment(cfg):
    get = probe(raises=requests.Timeout("timed out"))
    h = assess(portal_cfg(cfg), now=NOW, es=fresh_es(cfg), get_json=get)
    pt = h["dependencies"]["portal"]
    assert [p["detail"] for p in pt["problems"]] == ["portal_unreachable"]
    assert "Timeout" in pt["error"]
    assert h["exit_code"] == 1 and h["generated_at"] == NOW


def test_a_missing_adapter_binary_is_a_dependency_failure(cfg, monkeypatch):
    monkeypatch.setattr(health.shutil, "which",
                        lambda n: None if n == "codex" else f"/usr/bin/{n}")
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    assert h["dependencies"]["adapters"]["missing"] == ["codex"]
    assert [(p["check"], p["detail"]) for p in h["dependencies"]["problems"]] \
        == [("adapters", "adapter_missing:codex")]
    assert h["exit_code"] == 1
    assert "cron's PATH" in format_health(h)


def test_structured_mode_also_needs_the_pi_binary(cfg, monkeypatch):
    monkeypatch.setattr(health.shutil, "which",
                        lambda n: None if n == "pi" else f"/usr/bin/{n}")
    get = probe(models(("lfm2.5-2.6b", "loaded", 32768),
                       ("qwen3.8-27b", "loaded", 32768)))
    deps = deps_of(pi_cfg(cfg), get)
    assert deps["adapters"]["missing"] == ["pi"]


def test_a_wiki_with_no_upstream_is_a_problem_only_when_push_is_on(cfg):
    assert health._wiki_remote(cfg, {"git": True})["problems"] == []
    cfg.report = {**cfg.report, "push": True}
    wr = health._wiki_remote(cfg, {"git": True})
    assert [p["detail"] for p in wr["problems"]] == ["wiki_no_upstream"]


def test_an_unset_git_identity_is_a_problem(cfg, monkeypatch):
    monkeypatch.setattr(health, "_git_out", lambda repo, *a: "")
    wr = health._wiki_remote(cfg, {"git": True})
    assert [p["detail"] for p in wr["problems"]] == ["git_identity"]


def test_a_pile_of_unpushed_commits_is_a_problem(cfg, monkeypatch):
    cfg.report = {**cfg.report, "push": True}
    replies = {("config", "user.email"): "test@test",
               ("rev-parse", "--abbrev-ref", "@{u}"): "origin/main"}
    monkeypatch.setattr(health, "_git_out",
                        lambda repo, *a: replies.get(a, "42"))
    wr = health._wiki_remote(cfg, {"git": True})
    assert wr["unpushed"] == 42
    assert [p["detail"] for p in wr["problems"]] == ["unpushed"]


def test_a_few_unpushed_commits_are_reported_not_flagged(cfg, monkeypatch):
    cfg.report = {**cfg.report, "push": True}
    replies = {("config", "user.email"): "test@test",
               ("rev-parse", "--abbrev-ref", "@{u}"): "origin/main"}
    monkeypatch.setattr(health, "_git_out",
                        lambda repo, *a: replies.get(a, "3"))
    wr = health._wiki_remote(cfg, {"git": True})
    assert wr["unpushed"] == 3 and wr["problems"] == []


def test_a_nearly_full_disk_is_a_problem(cfg, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage",
                        lambda p: SimpleNamespace(total=10, used=9,
                                                  free=100 * 1024 * 1024))
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    assert h["dependencies"]["disk"]["free_gib"] == 0.1
    assert [p["detail"] for p in h["dependencies"]["problems"]] == ["disk"]
    assert "free space under .state/" in format_health(h)


def test_a_check_that_explodes_never_breaks_the_report(cfg, monkeypatch):
    monkeypatch.setattr(health, "_adapters",
                        lambda c: (_ for _ in ()).throw(OSError("boom")))
    h = assess(cfg, now=NOW, es=fresh_es(cfg))
    assert "OSError" in h["dependencies"]["adapters"]["error"]
    assert h["exit_code"] == 0        # a broken check is not a finding


def test_format_health_prints_the_dependency_section_and_hints(cfg):
    get = probe(raises=requests.ConnectionError("refused"))
    pi_cfg(cfg)
    text = format_health(assess(cfg, now=NOW, es=fresh_es(cfg), get_json=get))
    assert "\ndependencies" in text
    assert "model svr   UNREACHABLE unsloth " in text
    assert "start the local model server" in text
    assert "adapters    codex=ok" in text and "wiki remote" in text


def test_every_failed_ingest_line_carries_its_recovery_command(cfg, orch,
                                                               monkeypatch):
    ingest_failure(cfg, orch, monkeypatch, raises=HarnessError("pi exited 1"))
    text = format_health(assess(cfg, now=NOW, es=fresh_es(cfg)))
    assert "-> dbwiki retry --db cdb1" in text


def test_a_dirty_tree_blocker_carries_its_recovery_command(cfg):
    (cfg.wiki_repo / "notes.md").write_text("human work in progress\n")
    text = format_health(assess(cfg, now=NOW, es=fresh_es(cfg)))
    assert "-> commit or stash the non-digest changes" in text


def test_categorize_answers_every_category_but_the_assessment_only_two():
    """`stage_stale` and `dependency_failure` are assessment-only: no exception
    maps to them, though alerts fingerprint and hint them like any other."""
    answers = {categorize(e) for e in (
        health.WikiMissingError("no wiki here"),
        LockBusyError("pid 1 holds the lock"),
        requests.ConnectionError("no route to host"),
        UnsupportedSchemaError("the mapping moved"),
        NoResultError("wrote no .agent-result.json"),
        HarnessError("agent timed out after 900s"),
        HarnessError("codex exited 3"),
        ValidationError("log.md not updated"),
        RuntimeError("the wiki has uncommitted changes"),
        RuntimeError("git commit refused"),
        ValueError("something nobody classified"),
    )}
    assert answers == set(CATEGORIES) - {"stage_stale", "dependency_failure"}


def test_a_busy_lock_is_categorized_before_the_plain_runtime_arm():
    assert issubclass(LockBusyError, RuntimeError)
    assert categorize(LockBusyError("another dbwiki command holds it")) \
        == "lock_busy"
