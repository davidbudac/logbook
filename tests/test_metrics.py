"""Workstream 5b: optional fleet-metric source and the AWR summary contract.

Metric sources ride the existing compaction pipeline (`metric_fixture_config`
enables the profile that config/dbwiki.yaml ships commented out), so these
tests exercise the real Compactor through FakeES: routine counters, the one
document-carried notability signal, replay, gaps, and db identity. The AWR half
is file-in / digest-out, with no ES at all.

Metric goldens live under tests/fixtures/golden/<case>/ like the log corpus."""

import copy
import json

import fixtures as fx
import pytest

from dbwiki.awr import (
    AWR_SCHEMA_VERSION,
    AwrContractError,
    awr_digest,
    emit_awr_digest,
    load_awr_summary,
    validate_awr_summary,
)
from dbwiki.compactor import Compactor
from dbwiki.config import Config
from dbwiki.digest_md import render_md
from dbwiki.normalize import normalize

GOLDEN_CASES = ["metrics_routine_window", "metrics_collection_failure"]
ALL_CASES = GOLDEN_CASES + ["metrics_late_replay", "metrics_missing_interval",
                            "metrics_db_identity"]


@pytest.fixture
def cfg(tmp_path):
    return fx.metric_fixture_config(tmp_path)


def seeded(cfg, name) -> dict:
    case = fx.load_metric_case(name)
    fx.seed_registry(cfg, case)
    return case


# ---- the source profile is optional and off by default -----------------------

def _raw(**extra) -> dict:
    return {"elasticsearch": {"url": "http://x:9200"}, "wiki_repo": "wiki",
            "state_dir": ".state", "digest_dir": "wiki/digests",
            "sources": {"alert": {"index_patterns": ["a-*"],
                                  "timestamp_field": "@timestamp",
                                  "db_fields": ["oracle.database.name"],
                                  "patterns_file": "patterns/alert.yaml"}},
            **extra}


def _metrics_profile(**over) -> dict:
    return {"metrics": {"index_patterns": [".ds-logs-oracle.metrics-*"],
                        "timestamp_field": "@timestamp",
                        "db_fields": ["oracle.database.name"],
                        "patterns_file": "patterns/metrics.yaml", **over}}


def test_the_shipped_config_has_no_enabled_metric_source(tmp_path):
    """Disabled by default: the real config/dbwiki.yaml must not add a source."""
    cfg = fx.fixture_config(tmp_path)
    assert cfg.metric_sources == []
    assert sorted(cfg.sources) == ["alert", "dataguard", "listener"]
    assert sorted(Compactor(cfg).libs) == ["alert", "dataguard", "listener"]


@pytest.mark.parametrize("raw", [
    _raw(),                                              # no metric_sources
    _raw(metric_sources={}),                             # empty section
    _raw(metric_sources=_metrics_profile(enabled=False)),  # explicitly off
    _raw(metric_sources=_metrics_profile()),             # enabled key absent
])
def test_a_disabled_metric_source_changes_nothing(raw, tmp_path):
    cfg = Config(raw, tmp_path)
    assert cfg.metric_sources == []
    assert list(cfg.sources) == ["alert"]


def test_an_enabled_metric_source_becomes_an_ordinary_source(tmp_path):
    cfg = Config(_raw(metric_sources=_metrics_profile(enabled=True)), tmp_path)
    assert cfg.metric_sources == ["metrics"]
    assert list(cfg.sources) == ["alert", "metrics"]
    assert cfg.source_kind("metrics") == "metric"
    assert cfg.source_kind("alert") == "log"
    assert cfg.db_query_fields("metrics") == ["oracle.database.name"]
    assert cfg.patterns_path("metrics") == tmp_path / "patterns/metrics.yaml"


def test_a_metric_source_may_not_shadow_a_log_source(tmp_path):
    raw = _raw(metric_sources={"alert": {**_metrics_profile()["metrics"],
                                         "enabled": True}})
    with pytest.raises(ValueError, match="collides"):
        Config(raw, tmp_path)


def test_the_fixture_profile_matches_the_commented_config_example():
    """The enabled profile these tests use is the one operators are told to
    uncomment — keep the two from drifting."""
    for name in ("dbwiki.yaml", "dbwiki.yaml.example"):
        path = fx.REPO_ROOT / "config" / name
        if not path.exists():        # a fresh clone has only the example
            continue
        text = path.read_text()
        assert "#metric_sources:" in text
        assert "#    enabled: false" in text
        for pattern in fx.METRIC_SOURCE["index_patterns"]:
            assert pattern in text
        assert fx.METRIC_SOURCE["patterns_file"] in text


# ---- normalization -----------------------------------------------------------

def _events(cfg, name) -> list[dict]:
    return fx.normalized_events(cfg, fx.load_metric_case(name))


def test_metric_documents_normalize_to_flat_numeric_events(cfg):
    events = {(e["metric_kind"], e["metric_name"], e["metric_target"]): e
              for e in _events(cfg, "metrics_routine_window")}

    cpu = events[("sysmetric", "Host CPU Utilization (%)", "")]
    assert cpu["metric_value"] == 0.99
    assert cpu["metric_unit"] == "% Busy/(Idle+Busy)"
    assert cpu["metric_values"] == {"value": 0.99}
    assert cpu["db"] == "cdb1" and cpu["metric_ts"] == cpu["ts"]

    tbs = events[("tablespace", "used_pct", "SYSTEM")]
    assert tbs["metric_value"] == 68.82 and tbs["metric_unit"] == "pct"
    assert tbs["metric_values"] == {"total_blocks": 1758211,
                                    "used_blocks": 1210004, "used_pct": 68.82}
    assert tbs["message"] == "tablespace SYSTEM used_pct=68.82"

    hlt = events[("health", "sessions.utilization_pct", "")]
    assert hlt["db_role"] == "PRIMARY" and hlt["instance_status"] == "OPEN"
    assert hlt["instance_available"] is True
    assert hlt["metric_value"] == 25.42
    assert hlt["metric_values"]["processes.utilization_pct"] == 30.0
    assert hlt["metric_values"]["sessions.limit"] == 472

    fra = events[("fra", "used_pct", "")]
    assert fra["metric_value"] == 99.96
    assert fra["metric_values"]["limit_bytes"] == 21474836480


def test_a_failed_collection_document_carries_its_own_signal(cfg):
    fails = [e for e in _events(cfg, "metrics_collection_failure")
             if e["outcome"] == "failure"]
    assert len(fails) == 3
    e = fails[0]
    assert e["level"] == "ERROR" and e["instance_available"] is False
    assert e["message"] == "database connection or metrics query failed"
    assert e["metric_kind"] == "health" and e["metric_value"] is None


def test_dataguard_metric_documents_normalize_their_lag(cfg):
    dg = [e for e in fx.normalized_events(
        cfg, {**fx.load_metric_case("metrics_db_identity"), "db": "cdb1_stby"})
        if e["metric_kind"] == "dataguard"]
    assert len(dg) == 1
    assert dg[0]["metric_name"] == "apply_lag_seconds"
    assert dg[0]["metric_value"] == 4519.0 and dg[0]["metric_unit"] == "s"
    assert dg[0]["metric_values"]["transport_lag_seconds"] == 4519.0


def test_the_log_branches_are_untouched_by_the_metric_branch():
    """`kind` defaults to log, so every existing caller behaves as before."""
    hit = {"_index": "i", "_id": "1",
           "_source": {"@timestamp": "2026-07-12T00:00:00Z", "db_name": "cdb1",
                       "oracle": {"alert_message": "ORA-00600 internal"}}}
    assert normalize("alert", hit) == normalize("alert", hit, kind="log")
    assert "metric_kind" not in normalize("alert", hit)


# ---- compaction end to end ---------------------------------------------------

@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_metric_case_matches_goldens(cfg, name):
    case = seeded(cfg, name)
    digest = fx.compact_case(cfg, case)
    stable = fx.stable_digest(digest)
    fx.assert_golden(f"{name}/events.json", fx.normalized_events(cfg, case))
    fx.assert_golden(f"{name}/digest.json", stable)
    fx.assert_golden(f"{name}/digest.md", render_md(stable), text=True)


def test_a_routine_metric_window_is_counters_only(cfg):
    case = seeded(cfg, "metrics_routine_window")
    section = fx.compact_case(cfg, case)["sources"]["metrics"]
    assert section["total_events"] == 28
    assert section["by_class"] == {"routine": 28}
    assert section["notable"] == []
    assert section["routine_counters"] == {
        "sysmetric:Host CPU Utilization (%)": 4,
        "sysmetric:Database Wait Time Ratio": 4,
        "sysmetric:Executions Per Sec": 4,
        "tablespace:SYSTEM": 4, "tablespace:USERS": 4,
        "fra_utilization": 4, "health:PRIMARY": 4}


def test_a_full_fra_is_not_notable_on_its_own(cfg):
    """The fixture's FRA sits at 99.96% used — the deterministic rules must
    still call it a counter. Interpreting it is the agent's job."""
    case = seeded(cfg, "metrics_routine_window")
    digest = fx.compact_case(cfg, case)
    assert digest["notable"] is False and digest["deltas"] == []
    fra = [e for e in fx.normalized_events(cfg, case)
           if e["metric_kind"] == "fra"]
    assert {e["metric_value"] for e in fra} == {99.96}


def test_one_window_spanning_a_data_stream_rollover(cfg):
    """Two backing indices of the same stream in one window. (A legacy
    non-ECS metric layout is NOT covered: `.ds-logs-oracle.metrics-*` is
    ECS-only — probed 2026-07-27, no `db_name` field exists in its mapping —
    so a mixed-layout metric case would be fiction.)"""
    events = _events(cfg, "metrics_routine_window")
    assert {e["index"].rsplit("-", 1)[1] for e in events} == {"000001", "000002"}
    assert {e["db"] for e in events} == {"cdb1"}


def test_a_collection_failure_is_the_only_notable_metric_class(cfg):
    case = seeded(cfg, "metrics_collection_failure")
    digest = fx.compact_case(cfg, case)
    section = digest["sources"]["metrics"]
    assert section["by_class"] == {"routine": 14, "error": 3}
    assert [(g["rule"], g["class"], g["count"]) for g in section["notable"]] == [
        ("metric_collection_failure", "error", 3)]
    assert digest["notable"] is True


# ---- replay, gaps, identity --------------------------------------------------

@pytest.mark.parametrize("name", ALL_CASES)
def test_compacting_a_metric_case_twice_is_identical(cfg, name):
    case = seeded(cfg, name)
    first = fx.compact_case(cfg, case)
    second = fx.compact_case(cfg, case)
    assert fx.stable_digest(second) == fx.stable_digest(first)
    assert Compactor.content_hash(second) == Compactor.content_hash(first)


def test_late_and_replayed_metric_documents_are_idempotent(cfg):
    """Documents arrive out of order and two are delivered twice. Scan order
    follows the timestamp, and the replayed pair is counted as delivered —
    the digest and its content hash must not depend on either."""
    case = seeded(cfg, "metrics_late_replay")
    digest = fx.compact_case(cfg, case)
    assert digest["sources"]["metrics"]["total_events"] == 16

    events = fx.normalized_events(cfg, case)
    assert [e["ts"] for e in events] == sorted(e["ts"] for e in events)

    shuffled = copy.deepcopy(case)
    shuffled["hits"]["metrics"] = list(reversed(shuffled["hits"]["metrics"]))
    other = fx.compact_case(cfg, shuffled)
    assert Compactor.content_hash(other) == Compactor.content_hash(digest)
    assert fx.stable_digest(other) == fx.stable_digest(digest)


def test_a_gap_between_samples_does_not_fabricate_silence(cfg):
    """12h without a sample inside a window that has samples is not silence:
    the silence rule needs an empty window, not a quiet stretch."""
    case = seeded(cfg, "metrics_missing_interval")
    digest = fx.compact_case(cfg, case)
    assert digest["deltas"] == []
    assert digest["sources"]["metrics"]["total_events"] == 21


def test_but_a_genuinely_empty_metric_window_still_reports_silence(cfg):
    """Same registry history, no documents at all — the ordinary silence rule
    fires, because metric sources use the ordinary delta machinery."""
    case = seeded(cfg, "metrics_missing_interval")
    empty = {**case, "hits": {"metrics": []}}
    deltas = fx.compact_case(cfg, empty)["deltas"]
    assert [(d["type"], d["source"]) for d in deltas] == [("silence", "metrics")]


def test_documents_of_another_db_are_never_misattributed(cfg):
    case = seeded(cfg, "metrics_db_identity")
    section = fx.compact_case(cfg, case)["sources"]["metrics"]
    assert section["total_events"] == 7          # the cdb1 round only
    assert section["routine_counters"]["health:PRIMARY"] == 1

    comp = fx.build_compactor(cfg, case)
    w = case["window"]
    assert comp.discover_dbs(w["from"], w["to"], ["metrics"]) == [
        "cdb1", "cdb1_stby", "emcdb"]


def test_a_document_without_db_identity_lands_in_unknown(cfg):
    case = fx.load_metric_case("metrics_db_identity")
    hit = next(h for h in case["hits"]["metrics"] if h["_id"] == "orphan")
    ev = normalize("metrics", hit, cfg.db_value_fields("metrics"), "metric")
    assert ev["db"] == "unknown"
    assert ev["metric_name"] == "Current Logons Count"
    # and it is not silently attributed to any real db
    assert all(e["id"] != "orphan" for e in fx.normalized_events(cfg, case))


def test_an_identity_less_document_is_not_a_schema_drift(cfg):
    """Other documents in the window do carry the db field, so the drift
    diagnostic must stay quiet — compacting a db with no documents of its own
    simply yields an empty section."""
    case = seeded(cfg, "metrics_db_identity")
    quiet = {**case, "db": "nosuchdb"}
    assert fx.compact_case(cfg, quiet)["sources"]["metrics"]["total_events"] == 0


# ---- AWR summary contract ----------------------------------------------------

VALID = fx.AWR_DIR / "valid.json"


def summary() -> dict:
    return json.loads(VALID.read_text())


def test_a_valid_awr_file_round_trips_to_a_golden_digest():
    loaded = load_awr_summary(VALID)
    assert loaded["collected"]["file"] == "valid.json"
    digest = fx.stable_digest(awr_digest(loaded))
    digest["generated_by"] = "dbwiki-awr/(fixture)"
    fx.assert_golden("awr_summary/digest.json", digest)
    fx.assert_golden("awr_summary/digest.md", render_md(digest), text=True)


def test_the_awr_digest_has_the_compactor_digest_shape():
    d = awr_digest(summary())
    assert set(d) >= {"db", "window", "generated_by", "pattern_versions",
                      "sources", "deltas", "totals", "notable"}
    assert d["db"] == "cdb1"
    assert d["window"] == {"from": "2026-07-12T10:00:00Z",
                           "to": "2026-07-12T11:00:00Z", "day": "2026-07-12"}
    assert d["generated_by"].startswith("dbwiki-awr/")
    assert d["awr_schema_version"] == AWR_SCHEMA_VERSION
    assert d["awr"]["snapshot"]["begin_id"] == 4711
    assert [g["rule"] for g in d["sources"]["awr"]["notable"]] == [
        "awr_window", "awr_top_wait", "awr_top_wait", "awr_top_wait",
        "awr_top_sql", "awr_top_sql"]


def test_an_awr_digest_never_wakes_an_agent_by_itself():
    """v1 applies no thresholds to AWR numbers."""
    d = awr_digest(summary())
    assert d["notable"] is False and d["deltas"] == []


def test_awr_digest_hash_is_stable_and_content_sensitive():
    base = Compactor.content_hash(awr_digest(summary()))
    assert base == Compactor.content_hash(awr_digest(summary()))

    moved = summary()  # same content, different snapshot window
    moved["snapshot"]["begin_time"] = "2026-07-12T12:00:00Z"
    moved["snapshot"]["end_time"] = "2026-07-12T13:00:00Z"
    assert Compactor.content_hash(awr_digest(moved)) == base

    for mutate in (
        lambda s: s["top_waits"][0].__setitem__("time_s", 813.0),
        lambda s: s["top_waits"][0].__setitem__("pct_db_time", 44.4),
        lambda s: s["top_waits"][0].__setitem__("event", "db file scattered read"),
        lambda s: s["top_sql"][0].__setitem__("execs", 1201),
        lambda s: s["top_sql"][0].__setitem__("elapsed_s", 402.2),
        lambda s: s.__setitem__("db_time_s", 1832.5),
        lambda s: s["load_profile"].__setitem__("executes_per_s", 120.5),
        lambda s: s["snapshot"].__setitem__("end_id", 4713),
        lambda s: s["top_sql"].pop(),
    ):
        s = summary()
        mutate(s)
        assert Compactor.content_hash(awr_digest(s)) != base, mutate


VIOLATIONS = [
    ("awr_schema_version", lambda s: s.__setitem__("awr_schema_version", 2)),
    ("db", lambda s: s.pop("db")),
    ("db", lambda s: s.__setitem__("db", "")),
    ("instance", lambda s: s.__setitem__("instance", 1)),
    ("snapshot", lambda s: s.pop("snapshot")),
    ("snapshot.begin_id", lambda s: s["snapshot"].__setitem__("begin_id", "4711")),
    ("snapshot.end_id", lambda s: s["snapshot"].__setitem__("end_id", 4711)),
    ("snapshot.begin_time", lambda s: s["snapshot"].pop("begin_time")),
    ("snapshot.begin_time", lambda s: s["snapshot"].__setitem__("begin_time", "yesterday")),
    ("snapshot.end_time", lambda s: s["snapshot"].__setitem__("end_time", "2026-07-12T09:00:00Z")),
    ("snapshot.extra", lambda s: s["snapshot"].__setitem__("extra", 1)),
    ("db_time_s", lambda s: s.pop("db_time_s")),
    ("db_time_s", lambda s: s.__setitem__("db_time_s", "1832.4")),
    ("db_time_s", lambda s: s.__setitem__("db_time_s", -1)),
    ("elapsed_s", lambda s: s.__setitem__("elapsed_s", 0)),
    ("load_profile", lambda s: s.__setitem__("load_profile", [])),
    ("load_profile.executes_per_s", lambda s: s["load_profile"].__setitem__("executes_per_s", "120")),
    ("top_waits", lambda s: s.pop("top_waits")),
    ("top_waits", lambda s: s.__setitem__("top_waits", [{"event": "x", "time_s": 1.0, "pct_db_time": 1.0}] * 21)),
    ("top_waits[0]", lambda s: s["top_waits"].__setitem__(0, "db file sequential read")),
    ("top_waits[0].event", lambda s: s["top_waits"][0].pop("event")),
    ("top_waits[0].time_s", lambda s: s["top_waits"][0].__setitem__("time_s", None)),
    ("top_waits[0].pct_db_time", lambda s: s["top_waits"][0].__setitem__("pct_db_time", 144.0)),
    ("top_waits[0].waits", lambda s: s["top_waits"][0].__setitem__("waits", 9.5)),
    ("top_waits[0].class", lambda s: s["top_waits"][0].__setitem__("class", "User I/O")),
    ("top_sql", lambda s: s.pop("top_sql")),
    ("top_sql[0].sql_id", lambda s: s["top_sql"][0].pop("sql_id")),
    ("top_sql[0].elapsed_s", lambda s: s["top_sql"][0].__setitem__("elapsed_s", "402")),
    ("top_sql[0].execs", lambda s: s["top_sql"][0].__setitem__("execs", 1200.5)),
    ("top_sql[0].execs", lambda s: s["top_sql"][0].__setitem__("execs", -1)),
    ("top_sql[0].sql_text", lambda s: s["top_sql"][0].__setitem__("sql_text", "select 1 from dual")),
    ("top_sql[0].statement", lambda s: s["top_sql"][0].__setitem__("statement", "select 1 from dual")),
    ("top_sql[0].plan_hash_value", lambda s: s["top_sql"][0].__setitem__("plan_hash_value", 123)),
    ("collected.tool", lambda s: s["collected"].__setitem__("tool", 7)),
    ("awr_html", lambda s: s.__setitem__("awr_html", "<html>")),
]


@pytest.mark.parametrize("field,mutate", VIOLATIONS,
                         ids=[f"{f}-{i}" for i, (f, _) in enumerate(VIOLATIONS)])
def test_each_contract_violation_names_its_field(field, mutate):
    s = summary()
    mutate(s)
    with pytest.raises(AwrContractError) as e:
        validate_awr_summary(s, where="fixture")
    assert f"'{field}'" in str(e.value), str(e.value)


def test_sql_text_is_rejected_with_its_own_message():
    s = summary()
    s["top_sql"][0]["sql_text"] = "select * from big_table"
    with pytest.raises(AwrContractError, match="never SQL text"):
        validate_awr_summary(s)


def test_a_bad_file_names_the_file_and_the_field(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text(json.dumps({**summary(), "db_time_s": "lots"}))
    with pytest.raises(AwrContractError) as e:
        load_awr_summary(p)
    assert str(p) in str(e.value) and "'db_time_s'" in str(e.value)

    p.write_text("{not json")
    with pytest.raises(AwrContractError, match="not valid JSON"):
        load_awr_summary(p)


def test_awr_digest_revalidates_its_input():
    with pytest.raises(AwrContractError, match="'top_sql'"):
        awr_digest({**summary(), "top_sql": "none"})


# ---- emitting ----------------------------------------------------------------

def test_emit_refuses_when_the_wiki_repo_is_absent(cfg):
    digest = awr_digest(summary())
    assert not cfg.wiki_repo.exists()
    with pytest.raises(FileNotFoundError, match="refusing to emit"):
        emit_awr_digest(cfg, digest)
    assert not cfg.digest_dir.exists()


def test_emit_writes_beside_the_compactor_digests(cfg):
    cfg.wiki_repo.mkdir(parents=True)
    jp, mp = emit_awr_digest(cfg, awr_digest(summary()))
    assert jp.name == "2026-07-12-awrT1000-1100.json"
    assert jp.parent == cfg.digest_dir / "cdb1"
    assert json.loads(jp.read_text())["awr"]["snapshot"]["end_id"] == 4712
    assert "AWR snapshot 4711 -> 4712" in mp.read_text()


def test_the_cli_validates_prints_and_refuses_to_emit(cfg, tmp_path, capsys,
                                                     monkeypatch):
    from dbwiki import cli
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cfg)

    assert cli.main(["awr", "--file", str(VALID)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["db"] == "cdb1" and printed["notable"] is False

    assert cli.main(["awr", "--file", str(VALID), "--emit"]) == 1
    assert "refusing to emit" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**summary(), "top_waits": [{"event": "x"}]}))
    assert cli.main(["awr", "--file", str(bad)]) == 2
    assert "'top_waits[0].time_s'" in capsys.readouterr().err

    cfg.wiki_repo.mkdir(parents=True)
    assert cli.main(["awr", "--file", str(VALID), "--emit"]) == 0
    assert "AWR rows ->" in capsys.readouterr().out
    assert (cfg.digest_dir / "cdb1" / "2026-07-12-awrT1000-1100.md").exists()
