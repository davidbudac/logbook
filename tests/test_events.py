"""The run-event contract, over fixture `.state/` logs.

Every line written here is shaped like the real ones: the key names and value
shapes come from `health.RunRecord.to_event`, `cli._digest_facts`,
`health.record_agent_run`, `health._record_run_start` and
`trigger.TriggerDecision.to_dict`. No key is invented, because a fixture that
carries a key no writer writes proves the loader handles fiction.

Nothing here touches the live `.state/`: every test builds its own under
`tmp_path`.
"""

import dataclasses
import json

import pytest

from dbwiki import events, health
from dbwiki.events import Kind, RunEvent, Usage, Visibility

DAY = "2026-08-30"
T0 = "2026-08-30T00:00:00Z"
T1 = "2026-08-30T09:00:00Z"

POISON_KEYS = ("prompt", "stdout", "message", "input", "output")
POISON = "SECRET-PROMPT-BODY"


def state_dir(tmp_path, *, run_health=(), agent_runs=(), run_starts=(),
              ledger=None):
    """A `.state/` holding exactly the lines given. A source with no lines
    gets no file, which is how a fresh install looks."""
    root = tmp_path / ".state"
    root.mkdir(parents=True, exist_ok=True)
    if run_health:
        (root / health.HEALTH_LOG).write_text(
            "".join(json.dumps(line) + "\n" for line in run_health))
    if agent_runs:
        (root / health.AGENT_LOG).write_text(
            "".join(json.dumps(line) + "\n" for line in agent_runs))
    if run_starts:
        path = root / health.RUN_STARTS_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(line) + "\n" for line in run_starts))
    if ledger is not None:
        (root / "ingest_ledger.json").write_text(
            json.dumps({"schema_version": 1, "entries": ledger}))
    return root


def db_entry(db, **over):
    """One `dbs[]` entry as `cmd_run` builds it: `rec.add_db` facts plus
    `_digest_facts`."""
    entry = {
        "db": db,
        "watermark_before": T0,
        "watermark_after": T1,
        "decision": "wake",
        "decision_reasons": ["first_ever_code"],
        "model_tier": "strong",
        "window": {"from": T0, "to": T1, "day": DAY},
        "events": 1204,
        "notable_events": 12,
        "notable_groups": 2,
        "notable": True,
        "deltas": ["first_ever_code"],
        "digest": f"wiki/digests/{db}/{DAY}.json",
        "content_hash": "3f9a1c2b",
        "validation": "ok",
        "outcome": "ingested",
        "commit": "ab12cd3",
        "telemetry": {"run_id": "aaaaaaaaaaaa", "task": "ingest",
                      "adapter": "pi", "model": "qwen3-30b",
                      "model_tier": "strong", "duration_s": 41.2,
                      "timed_out": False, "usage": "unknown",
                      "pages_touched": 3, "incidents_opened": 0,
                      "incidents_updated": 1, "validation_ok": True,
                      "rolled_back": False, "lint_findings": 0,
                      "attempts": 1, "digest_bytes": 18422},
    }
    entry.update(over)
    return entry


def run_line(run_id, *, command="run", started=T1, outcome="ok", dbs=(),
             facts=None):
    """One `run_health.jsonl` line as `RunRecord.to_event` writes it."""
    return {
        "schema_version": 1,
        "run_id": run_id,
        "command": command,
        "started": started,
        "finished": started,
        "duration_s": 61.4,
        "outcome": outcome,
        "error_category": None,
        "dbs": list(dbs),
        "facts": {"consolidation": False, "adapter": "pi", "dbs": len(dbs),
                  **(facts or {})},
    }


def agent_line(event_id, run_id, *, task="ingest", at=T1, **over):
    """One `agent_runs.jsonl` line as `record_agent_run` writes it."""
    line = {
        "run_id": run_id,
        "task": task,
        "adapter": "pi",
        "model": "qwen3-30b",
        "model_tier": "strong",
        "duration_s": 41.2,
        "timed_out": False,
        "pages_touched": 3,
        "incidents_opened": 0,
        "incidents_updated": 1,
        "validation_ok": True,
        "rolled_back": False,
        "lint_findings": 0,
        "attempts": 1,
        "digest_bytes": 18422,
        "event_id": event_id,
        "at": at,
        "db": "cdb1",
        "mode": "structured",
        "exit_code": 0,
        "stdout_bytes": 4096,
        "prompt_bytes": 8192,
        "input_tokens": 12000,
        "output_tokens": 900,
        "cost_usd": 0.0,
        "usage_known": True,
    }
    line.update(over)
    return line


def start_line(run_id, *, command="run", started=T1):
    """One `elk/run_starts.jsonl` line as `_record_run_start` writes it."""
    return {"event_id": f"{run_id}-start", "run_id": run_id,
            "command": command, "started": started, "pid": 4242,
            "run_host": "dbhost", "schema_version": 1}


def only(loaded, kind):
    return [ev for ev in loaded.events if ev.kind is kind]


def coverage_of(loaded, name):
    return next(row for row in loaded.coverage if row.name == name)


def test_dbs_fan_out_to_deterministic_children(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1"), db_entry("cdb2"),
                                      db_entry("orcl")])])
    loaded = events.load_events(root)

    assert len(only(loaded, Kind.RUN)) == 1
    children = only(loaded, Kind.DB_RUN)
    assert [ev.event_id for ev in children] == [
        "aaaaaaaaaaaa:cdb1", "aaaaaaaaaaaa:cdb2", "aaaaaaaaaaaa:orcl"]
    assert {ev.run_id for ev in children} == {"aaaaaaaaaaaa"}
    assert [ev.at for ev in children] == [T1, T1, T1]
    assert children[0].digest_path == f"wiki/digests/cdb1/{DAY}.json"
    assert children[0].reason_codes == ("first_ever_code",)
    assert children[0].validation_ok is True


def test_a_retry_line_repeating_one_database_keeps_distinct_ids(tmp_path):
    """`cmd_retry` records one `dbs[]` entry per digest, so a database repeats
    within one line and the digest's day is what tells the rows apart."""
    root = state_dir(tmp_path, run_health=[
        run_line("255d41b65c49", command="retry", dbs=[
            db_entry("cdb1", digest="digests/cdb1/2026-08-14.json",
                     outcome="failed"),
            db_entry("cdb1", digest="digests/cdb1/2026-08-23.json"),
            db_entry("cdb1", digest="digests/cdb1/2026-08-26.json")])])
    loaded = events.load_events(root)

    assert [ev.event_id for ev in only(loaded, Kind.DB_RUN)] == [
        "255d41b65c49:cdb1",
        "255d41b65c49:cdb1:2026-08-23",
        "255d41b65c49:cdb1:2026-08-26"]
    assert events.load_events(root) == loaded


def test_a_repeated_database_with_no_digest_falls_back_to_its_position(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("255d41b65c49", command="retry",
                 dbs=[{"db": "cdb1"}, {"db": "cdb1"}])])

    assert [ev.event_id for ev in only(events.load_events(root), Kind.DB_RUN)] \
        == ["255d41b65c49:cdb1", "255d41b65c49:cdb1#1"]


def test_loading_twice_yields_identical_events(tmp_path):
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")]),
                    run_line("bbbbbbbbbbbb", started=T0)],
        agent_runs=[agent_line("111111111111", "aaaaaaaaaaaa")],
        run_starts=[start_line("aaaaaaaaaaaa")])

    assert events.load_events(root) == events.load_events(root)


def test_events_are_newest_first(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", started="2026-07-27T02:00:00Z"),
        run_line("cccccccccccc", started="2026-08-30T09:00:00Z"),
        run_line("bbbbbbbbbbbb", started="2026-08-12T18:02:33Z")])

    assert [ev.run_id for ev in events.load_events(root).events] == [
        "cccccccccccc", "bbbbbbbbbbbb", "aaaaaaaaaaaa"]


def test_run_id_groups_a_folded_in_analyst_line_under_the_older_run(tmp_path):
    """ADR-0001 `queue.fold_results` replays the enqueuing tick's `run_id`
    days later, so one `run_id` spans lines written far apart and `event_id`
    is what stays unique."""
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", started="2026-08-25T09:00:00Z",
                             dbs=[db_entry("cdb1")])],
        agent_runs=[agent_line("111111111111", "aaaaaaaaaaaa",
                               at="2026-08-25T09:04:00Z"),
                    agent_line("222222222222", "aaaaaaaaaaaa", task="report",
                               at="2026-08-29T22:10:00Z")])
    loaded = events.load_events(root)

    ids = [ev.event_id for ev in loaded.events]
    assert len(ids) == len(set(ids))
    grouped = [ev for ev in loaded.events if ev.run_id == "aaaaaaaaaaaa"]
    assert len(grouped) == 4
    assert {ev.kind for ev in grouped} == {Kind.RUN, Kind.DB_RUN, Kind.STAGE}
    stages = only(loaded, Kind.STAGE)
    assert [ev.task for ev in stages] == ["report", "ingest"]


def test_unlisted_producer_keys_never_reach_an_event(tmp_path):
    poison = {key: POISON for key in POISON_KEYS}
    entry = db_entry("cdb1", **poison)
    entry["telemetry"] = {**entry["telemetry"], **poison}
    line = run_line("aaaaaaaaaaaa", dbs=[entry],
                    facts={**poison, "telemetry_errors": [POISON]})
    line.update(poison)
    root = state_dir(tmp_path, run_health=[line],
                     agent_runs=[agent_line("111111111111", "aaaaaaaaaaaa",
                                            **poison)])
    loaded = events.load_events(root)

    assert len(loaded.events) == 3
    encoded = json.dumps([dataclasses.asdict(ev) for ev in loaded.events],
                         default=str)
    assert POISON not in encoded
    for key in POISON_KEYS:
        assert f'"{key}":' not in encoded
    assert '"output_tokens":' in encoded


def test_ledger_decisions_keep_evidence_and_normalize_the_digest_key(tmp_path):
    root = state_dir(tmp_path, ledger={
        f"digests/cdb1/{DAY}.json": {
            "status": "ingested",
            "last_decision": {
                "schema_version": 1, "db": "cdb1",
                "window": {"from": T0, "to": T1, "day": DAY},
                "outcome": "wake",
                "reasons": [
                    {"code": "first_ever_code",
                     "evidence": {"type": "first_ever_code", "code": "ORA-600",
                                  "count": 3, "sources": ["alert"]}},
                    {"code": "notable_class",
                     "evidence": {"rule": "error_burst", "class": "error",
                                  "count": 12}}],
                "model_tier": "strong", "content_hash": "3f9a1c2b",
                "explanation": "cdb1: wake (strong tier) — first_ever_code."}}})

    got = events.decisions(root)
    assert set(got) == {f"digests/cdb1/{DAY}.json"}
    reasons = got[f"digests/cdb1/{DAY}.json"]
    assert [r.code for r in reasons] == ["first_ever_code", "notable_class"]
    assert reasons[0].evidence == {
        "type": "first_ever_code", "code": "ORA-600", "count": 3}
    assert reasons[1].evidence == {
        "rule": "error_burst", "class": "error", "count": 12}


def test_digest_key_normalizes_both_writers_spellings():
    assert events.digest_key(f"wiki/digests/cdb1/{DAY}.json") == \
        f"digests/cdb1/{DAY}.json"
    assert events.digest_key(f"digests/cdb1/{DAY}.json") == \
        f"digests/cdb1/{DAY}.json"
    assert events.digest_key("nothing/at/all.json") == "nothing/at/all.json"


def test_a_db_run_joins_the_ledger_only_after_normalization(tmp_path):
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")])],
        ledger={f"digests/cdb1/{DAY}.json": {
            "last_decision": {"outcome": "wake",
                              "reasons": [{"code": "routine_only",
                                           "evidence": {}}]}}})
    child = only(events.load_events(root), Kind.DB_RUN)[0]
    table = events.decisions(root)

    assert child.digest_path not in table
    assert events.digest_key(child.digest_path) in table


LEDGER = {
    "digests/cdb1/2026-08-27.json": {
        "last_decision": {"outcome": "skip",
                          "reasons": [{"code": "routine_only",
                                       "evidence": {}}]}},
    "digests/cdb2/2026-08-28.json": {
        "last_decision": {"outcome": "wake",
                          "reasons": [{"code": "notable_class",
                                       "evidence": {"class": "error"}}]}},
    "digests/orcl/2026-08-29.json": {
        "status": "ingested", "commit": "ab12cd3",
        "last_decision": {"outcome": "wake", "reasons": []}},
    "digests/orcl/2026-08-30.json": {
        "status": "failed", "problems": ["validation: broken link"],
        "last_decision": {"outcome": "wake", "reasons": []}},
}


def test_backlog_is_only_the_undecided_entry(tmp_path):
    root = state_dir(tmp_path, ledger=LEDGER)

    assert events.backlog(root) == (
        {"digest": "digests/cdb2/2026-08-28.json", "db": "cdb2",
         "decision": "wake"},)


@pytest.mark.parametrize("entry,expected", [
    ({}, False),
    ({"last_decision": {"outcome": "skip"}}, True),
    ({"last_decision": {"outcome": "wake"}}, False),
    ({"last_decision": {"outcome": "force_consolidation"}}, False),
    ({"status": "ingested", "last_decision": {"outcome": "skip"}}, False),
    ({"status": "failed", "last_decision": {"outcome": "skip"}}, False),
    ({"status": "claimed", "last_decision": {"outcome": "skip"}}, True),
    ({"last_decision": None}, False),
    ({"last_decision": ["skip"]}, False),
])
def test_deliberate_skip_truth_table(entry, expected):
    assert health.deliberate_skip(entry) is expected


def timeline_of(root, run_id):
    loaded = events.load_events(root)
    run = next(ev for ev in loaded.events
               if ev.run_id == run_id and ev.kind in (Kind.RUN, Kind.START))
    children = [ev for ev in loaded.events
                if ev.run_id == run_id and ev is not run]
    return events.timeline(run, children)


def test_timeline_always_reports_all_seven_stages(tmp_path):
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa",
                             dbs=[db_entry("cdb1"),
                                  db_entry("cdb2", decision="skip",
                                           decision_reasons=["routine_only"],
                                           notable=False, model_tier="cheap",
                                           outcome=None, commit=None,
                                           validation=None)],
                             facts={"html": "ab12cd3"})],
        agent_runs=[agent_line("111111111111", "aaaaaaaaaaaa")])
    stages = timeline_of(root, "aaaaaaaaaaaa")

    assert [s.name for s in stages] == list(events.STAGE_NAMES)
    assert {s.status for s in stages} <= set(events.STATUSES)
    by_name = {s.name: s for s in stages}
    assert by_name["tick"].status == "succeeded"
    assert by_name["tick"].started == T1
    assert by_name["tick"].duration_s == 61.4
    assert by_name["discover/compact"].status == "succeeded"
    assert by_name["decide"].detail["decisions"] == {"skip": 1, "wake": 1}
    assert by_name["ingest"].status == "succeeded"
    assert by_name["ingest"].detail["ingested"] == 1
    assert by_name["ingest"].detail["attempts"] == 1
    assert by_name["render"].status == "succeeded"
    assert by_name["report"].status == "skipped"
    assert "reason" in by_name["report"].detail
    assert by_name["alerts"].status == "skipped"
    assert [s.agentic for s in stages] == [False, False, False, True, True,
                                           False, False]


def test_inferred_stages_say_so_and_recorded_ones_do_not(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")],
                 facts={"html": "ab12cd3", "report": f"reports/{DAY}.md",
                        "alerted": 2, "recovered": 1})])
    by_name = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}

    inferred = {name for name, stage in by_name.items()
                if stage.detail.get("inferred")}
    assert inferred == {"discover/compact", "decide", "ingest"}
    assert by_name["report"].detail == {"path": f"reports/{DAY}.md"}
    assert by_name["alerts"].detail == {"alerted": 2, "recovered": 1}


def test_a_stage_with_no_recorded_clock_invents_none(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")])])

    for stage in timeline_of(root, "aaaaaaaaaaaa")[1:]:
        assert stage.started == ""
        assert stage.finished == ""
        assert stage.duration_s is None


def test_render_failed_is_a_warning_not_a_failure(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")],
                 facts={"html": "(render failed)"})])
    by_name = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}

    assert by_name["render"].status == "warning"
    assert by_name["tick"].status == "succeeded"


def test_nothing_to_commit_renders_as_a_skip(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", facts={"html": "(nothing to commit)"})])
    by_name = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}

    assert by_name["render"].status == "skipped"
    assert by_name["render"].detail["reason"] == "nothing to commit"
    assert by_name["discover/compact"].detail["reason"] == \
        "no databases discovered"


def test_one_failing_database_is_a_warning_not_a_failed_ingest(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", outcome="failed",
                 dbs=[db_entry("cdb1"),
                      db_entry("cdb2", validation="failed", outcome=None,
                               commit=None, error_category="validation",
                               error="broken link on databases/cdb2.md")])])
    by_name = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}

    assert by_name["ingest"].status == "warning"
    assert by_name["ingest"].detail["failed"] == 1
    assert by_name["ingest"].detail["ingested"] == 1
    assert by_name["tick"].status == "failed"


def test_a_database_that_dies_in_compaction_fails_discovery(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", outcome="failed",
                 dbs=[{"db": "cdb1", "error_category": "es_unreachable",
                       "error": "connection refused"}])])
    by_name = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}

    assert by_name["discover/compact"].status == "failed"
    assert by_name["decide"].status == "skipped"
    assert by_name["ingest"].status == "skipped"


def test_an_all_skip_tick_attempted_no_ingest(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa",
                 dbs=[db_entry("cdb1", decision="skip", outcome=None,
                               commit=None, validation=None,
                               decision_reasons=["routine_only"])])])
    ingest = {s.name: s for s in timeline_of(root, "aaaaaaaaaaaa")}["ingest"]

    assert ingest.status == "skipped"
    assert ingest.detail["reason"] == "every database was a deliberate skip"


def test_a_lint_command_skips_six_stages_naming_the_command(tmp_path):
    root = state_dir(tmp_path, run_health=[
        run_line("dddddddddddd", command="lint",
                 facts={"findings": 0, "blocking": 0, "pages": 195})])
    stages = timeline_of(root, "dddddddddddd")

    assert [s.name for s in stages] == list(events.STAGE_NAMES)
    assert stages[0].status == "succeeded"
    assert [s.status for s in stages[1:]] == ["skipped"] * 6
    assert all(s.detail["reason"] ==
               "the lint command does not run this stage" for s in stages[1:])


def test_a_start_yields_seven_pending_stages(tmp_path):
    root = state_dir(tmp_path, run_starts=[start_line("eeeeeeeeeeee")])
    stages = timeline_of(root, "eeeeeeeeeeee")

    assert [s.name for s in stages] == list(events.STAGE_NAMES)
    assert [s.status for s in stages] == ["pending"] * 7
    assert all(s.detail["reason"] == "run has not finished" for s in stages)
    assert stages[0].started == T1
    assert stages[0].duration_s is None


def test_pending_is_a_start_with_no_finish(tmp_path):
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", started=T1)],
        run_starts=[start_line("aaaaaaaaaaaa", started=T1),
                    start_line("ffffffffffff", started="2026-08-30T11:00:00Z")])
    loaded = events.load_events(root)

    assert [ev.run_id for ev in events.pending(loaded.events)] == \
        ["ffffffffffff"]
    assert events.pending(loaded.events)[0].event_id == "ffffffffffff-start"


def test_pending_is_empty_when_every_start_finished(tmp_path):
    root = state_dir(tmp_path,
                     run_health=[run_line("aaaaaaaaaaaa")],
                     run_starts=[start_line("aaaaaaaaaaaa")])

    assert events.pending(events.load_events(root).events) == ()


def test_classification_covers_every_field_both_ways():
    names = {f.name for f in dataclasses.fields(RunEvent)}

    assert set(events.CLASSIFICATION) == names
    assert all(isinstance(v, Visibility)
               for v in events.CLASSIFICATION.values())
    assert events.CLASSIFICATION["error"] is Visibility.OPERATOR
    assert {name for name, vis in events.CLASSIFICATION.items()
            if vis is Visibility.OPERATOR} == {"error"}


def test_coverage_reports_every_log_including_missing_ones(tmp_path):
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", started="2026-07-27T02:00:00Z"),
                    run_line("bbbbbbbbbbbb", started="2026-08-30T09:00:00Z",
                             dbs=[db_entry("cdb1")])],
        run_starts=[start_line("bbbbbbbbbbbb", started="2026-08-12T18:02:33Z")])
    loaded = events.load_events(root)

    assert [row.name for row in loaded.coverage] == \
        ["run_health", "agent_runs", "run_starts"]
    assert [row.cap for row in loaded.coverage] == [
        health.MAX_EVENTS, health.MAX_AGENT_EVENTS, health.MAX_RUN_START_EVENTS]

    runs = coverage_of(loaded, "run_health")
    assert (runs.lines, runs.truncated) == (2, False)
    assert (runs.oldest, runs.newest) == ("2026-07-27T02:00:00Z",
                                          "2026-08-30T09:00:00Z")

    starts = coverage_of(loaded, "run_starts")
    assert (starts.lines, starts.oldest) == (1, "2026-08-12T18:02:33Z")
    assert starts.oldest > runs.oldest

    agents = coverage_of(loaded, "agent_runs")
    assert (agents.lines, agents.oldest, agents.newest, agents.truncated) == \
        (0, "", "", False)


def test_truncated_flips_at_the_cap(tmp_path):
    lines = [start_line(f"{n:012x}", started=f"2026-08-30T{n % 24:02d}:00:00Z")
             for n in range(health.MAX_RUN_START_EVENTS)]
    below = state_dir(tmp_path / "below", run_starts=lines[:-1])
    at_cap = state_dir(tmp_path / "at", run_starts=lines)

    assert coverage_of(events.load_events(below), "run_starts").truncated \
        is False
    assert coverage_of(events.load_events(at_cap), "run_starts").truncated \
        is True


def test_an_unparseable_line_costs_itself_and_not_the_load(tmp_path):
    root = state_dir(tmp_path, run_health=[run_line("aaaaaaaaaaaa")])
    with (root / health.HEALTH_LOG).open("a") as fh:
        fh.write("{truncated wri\n")
    loaded = events.load_events(root)

    assert [ev.run_id for ev in loaded.events] == ["aaaaaaaaaaaa"]
    assert coverage_of(loaded, "run_health").lines == 1


def test_unknown_usage_is_never_filled_in(tmp_path):
    root = state_dir(tmp_path, agent_runs=[
        agent_line("111111111111", "aaaaaaaaaaaa", usage_known=True),
        agent_line("222222222222", "aaaaaaaaaaaa", task="report",
                   usage_known=False, input_tokens=12000, output_tokens=900,
                   cost_usd=None),
        {"run_id": "aaaaaaaaaaaa", "task": "lint",
         "telemetry_error": "lint: KeyError: 'usage'"}])
    by_id = {ev.event_id: ev for ev in events.load_events(root).events}

    assert by_id["111111111111"].usage == Usage(12000, 900, 0.0, True, True)
    unknown = by_id["222222222222"].usage
    assert unknown.known is False
    assert unknown.cost_usd == 0.0
    assert by_id["aaaaaaaaaaaa:lint:unidentified"].usage is None


def test_a_price_nobody_reported_is_told_apart_from_a_measured_zero(tmp_path):
    """A codex line reports tokens and no `cost_usd`; a local pi line reports
    a true `0.0`. After the coercion both read `cost_usd == 0.0`, so summing
    them without `cost_known` would count the codex spend as measured zero."""
    codex = agent_line("111111111111", "aaaaaaaaaaaa", adapter="codex")
    del codex["cost_usd"]
    root = state_dir(tmp_path, agent_runs=[
        codex,
        agent_line("222222222222", "aaaaaaaaaaaa", task="report",
                   cost_usd=0.0),
        agent_line("333333333333", "aaaaaaaaaaaa", task="lint",
                   cost_usd="unknown")])
    by_id = {ev.event_id: ev for ev in events.load_events(root).events}

    priced = by_id["222222222222"].usage
    assert priced.cost_known is True and priced.cost_usd == 0.0
    for event_id in ("111111111111", "333333333333"):
        usage = by_id[event_id].usage
        assert usage.known is True, "the tokens are still reported"
        assert usage.cost_known is False and usage.cost_usd == 0.0


def test_a_telemetry_capture_failure_is_identified_and_says_why(tmp_path):
    """`orchestrate._telemetry` writes `{run_id, task, telemetry_error}` and
    mints no `event_id`, so the id comes from the run and the task and the
    bounded message lands in the one free-text field."""
    root = state_dir(tmp_path, agent_runs=[
        {"run_id": "aaaaaaaaaaaa", "task": "lint",
         "telemetry_error": "lint: KeyError: 'usage'"}])
    stage = only(events.load_events(root), Kind.STAGE)[0]

    assert stage.event_id == "aaaaaaaaaaaa:lint:unidentified"
    assert stage.error == "lint: KeyError: 'usage'"
    assert events.CLASSIFICATION["error"] is Visibility.OPERATOR


def test_an_empty_state_dir_loads_nothing_and_still_reports_coverage(tmp_path):
    loaded = events.load_events(tmp_path / "missing")

    assert loaded.events == ()
    assert len(loaded.coverage) == 3
    assert all(row.lines == 0 for row in loaded.coverage)
    assert events.backlog(tmp_path / "missing") == ()
    assert events.decisions(tmp_path / "missing") == {}


RUN_ID = "aaaaaaaaaaaa"


def test_a_measure_crosses_beside_the_stages_that_reported_it(tmp_path):
    """Three lines that measured three different amounts: one priced, one
    that reported tokens and no price, and one that recorded no usage block at
    all. The third is in neither price count, because a stage that measured
    nothing did not measure "no price"."""
    priced = agent_line("111111111111", RUN_ID, cost_usd=0.42,
                        rolled_back=True)
    unpriced = agent_line("222222222222", RUN_ID, task="report",
                          adapter="codex")
    del unpriced["cost_usd"]
    silent = agent_line("333333333333", RUN_ID, task="lint")
    for key in ("input_tokens", "output_tokens", "cost_usd", "usage_known"):
        del silent[key]
    lines = [priced, unpriced, silent]
    loaded = events.load_events(state_dir(
        tmp_path, run_health=[run_line(RUN_ID)], agent_runs=lines))

    measured = events.totals(loaded.events)
    assert measured.stages == len(lines), "the run and its dbs are not stages"
    assert measured.rolled_back == 1
    assert measured.tokens == sum(line["input_tokens"] + line["output_tokens"]
                                  for line in (priced, unpriced))
    assert measured.token_stages == 2
    assert measured.seconds == sum(line["duration_s"] for line in lines)
    assert measured.timed_stages == len(lines)
    assert measured.cost_usd == priced["cost_usd"]
    assert measured.priced_stages == 1
    assert measured.unpriced_stages == 1


def test_a_day_nothing_ran_on_is_a_row_and_not_a_missing_one(tmp_path):
    """A gap is a fact about the loop. A table that skipped it would draw the
    span as adjacent rows and read as a denser loop than there was."""
    loaded = events.load_events(state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", started="2026-08-28T09:00:00Z"),
                    run_line("bbbbbbbbbbbb", started="2026-08-30T02:00:00Z"),
                    run_line("cccccccccccc", started="2026-08-30T09:00:00Z",
                             outcome="failed")],
        agent_runs=[agent_line("111111111111", "cccccccccccc",
                               at="2026-08-30T09:00:10Z"),
                    agent_line("222222222222", "cccccccccccc", at="")]))

    rows = events.days(loaded.events)
    assert [row.day for row in rows] == ["2026-08-28", "2026-08-29",
                                         "2026-08-30"]
    gap = rows[1]
    assert (gap.runs, gap.failed) == (0, 0)
    assert gap.totals == events.Totals()
    assert (rows[0].runs, rows[0].failed) == (1, 0)
    assert (rows[2].runs, rows[2].failed) == (2, 1)
    assert rows[2].totals.stages == 1, "the dateless line belongs to no day"
    assert sum(row.totals.stages for row in rows) == 1


def test_an_event_no_day_can_be_read_off_leaves_the_calendar_alone(tmp_path):
    loaded = events.load_events(state_dir(
        tmp_path, agent_runs=[agent_line("111111111111", RUN_ID, at="2026-08"),
                              agent_line("222222222222", RUN_ID, at="not a "
                                                                   "date")]))

    assert events.days(loaded.events) == ()


def test_a_run_is_compared_against_the_last_one_of_its_own_command(tmp_path):
    """The fold-in replays an old `run_id` days later, so a candidate sharing
    this run's id is this run recorded twice; comparing against it would draw
    a fold-in as a regression."""
    loaded = events.load_events(state_dir(tmp_path, run_health=[
        run_line("aaaaaaaaaaaa", started="2026-08-28T09:00:00Z"),
        run_line("bbbbbbbbbbbb", command="lint",
                 started="2026-08-29T09:00:00Z"),
        run_line("cccccccccccc", started="2026-08-29T10:00:00Z"),
        run_line("cccccccccccc", started="2026-08-30T09:00:00Z")]))
    runs = [ev for ev in loaded.events if ev.kind is Kind.RUN]
    newest = max(runs, key=lambda ev: ev.at)
    oldest = min(runs, key=lambda ev: ev.at)

    before = events.previous_run(loaded.events, newest)
    assert before is not None
    assert before.run_id == "aaaaaaaaaaaa", \
        "the lint ran a different command; the folded-in line is this run"
    assert events.previous_run(loaded.events, oldest) is None


def test_the_narrow_stage_reader_answers_what_the_whole_load_does(tmp_path):
    """`events.stages` skips two logs to save the incident screen 160 ms of
    every open. It has to be the same answer, or the screen that uses it and
    the screen that uses `load_events` would disagree about one tick."""
    root = state_dir(
        tmp_path,
        run_health=[run_line("aaaaaaaaaaaa", dbs=[db_entry("cdb1")]),
                    run_line("bbbbbbbbbbbb", dbs=[db_entry("cdb2")])],
        agent_runs=[agent_line("111111111111", "aaaaaaaaaaaa",
                               at="2026-08-25T09:04:00Z"),
                    agent_line("222222222222", "bbbbbbbbbbbb", task="report",
                               at="2026-08-29T22:10:00Z")],
        run_starts=[start_line("cccccccccccc")])
    assert events.stages(root) == tuple(
        only(events.load_events(root), Kind.STAGE))


def test_the_narrow_stage_reader_is_empty_where_the_log_is_absent(tmp_path):
    """A fresh install has no agent log. That is an empty answer and never a
    raise, the rule `load_events` follows for every source it cannot find."""
    assert events.stages(state_dir(tmp_path)) == ()
