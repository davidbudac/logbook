"""Delta detection: rate anomalies and silence must behave sensibly for both
full-day windows and the partial intra-day windows the `run` tick produces,
`first_ever_code` must be scoped to the source that saw the code, and
`new_service` must fire on service names only — never on the connect-string
fragments upstream sometimes files under `service.name`."""

import json
from collections import Counter

import fixtures as fx

from dbwiki.compactor import Compactor
from dbwiki.state import Registry, StateStore

DAY = 86400.0
BASELINE_DAYS = ("2026-07-07", "2026-07-08", "2026-07-09")


def make_compactor() -> Compactor:
    comp = object.__new__(Compactor)
    comp.anomaly_factor = 10
    comp.anomaly_min_count = 10
    comp.baseline_days = 7
    comp.silence_windows = 3
    comp.silence_min_hours = 20
    return comp


def make_registry(tmp_path, count=24, seconds=DAY) -> Registry:
    """Baseline: 24 log switches per full day => 1/h median."""
    reg = Registry(tmp_path / "r.json")
    for d in BASELINE_DAYS:
        reg.set_day_counts(d, "alert", count, {"log_switch": count}, seconds)
    return reg


def deltas(comp, reg, total, counters, t0, t1, win_s):
    return comp._deltas("alert", "cdb1", reg, "2026-07-10", t0, t1,
                        total, Counter(counters), win_s)


def test_rate_anomaly_fires_on_full_day_spike(tmp_path):
    comp, reg = make_compactor(), make_registry(tmp_path)
    out = deltas(comp, reg, 500, {"log_switch": 500},
                 "2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z", DAY)
    (d,) = [d for d in out if d["type"] == "rate_anomaly"]
    assert d["counter"] == "log_switch"
    assert d["rate_per_hour"] > 10 * d["baseline_median_per_hour"]


def test_rate_anomaly_fires_on_partial_window_spike(tmp_path):
    # 50 events in 2h = 25/h against a 1/h full-day baseline: anomalous even
    # though 50 < any full-day total (raw-count comparison would miss it)
    comp, reg = make_compactor(), make_registry(tmp_path)
    out = deltas(comp, reg, 50, {"log_switch": 50},
                 "2026-07-10T00:00:00Z", "2026-07-10T02:00:00Z", 7200)
    assert any(d["type"] == "rate_anomaly" for d in out)


def test_rate_anomaly_quiet_partial_window_does_not_fire(tmp_path):
    # 2h at exactly the baseline rate: nothing anomalous
    comp, reg = make_compactor(), make_registry(tmp_path)
    out = deltas(comp, reg, 2, {"log_switch": 2},
                 "2026-07-10T00:00:00Z", "2026-07-10T02:00:00Z", 7200)
    assert not any(d["type"] == "rate_anomaly" for d in out)


def test_rate_anomaly_skips_sub_hour_windows_and_tiny_counts(tmp_path):
    comp, reg = make_compactor(), make_registry(tmp_path)
    # 30-minute window: skipped regardless of rate
    out = deltas(comp, reg, 100, {"log_switch": 100},
                 "2026-07-10T00:00:00Z", "2026-07-10T00:30:00Z", 1800)
    assert not any(d["type"] == "rate_anomaly" for d in out)
    # high rate but below rate_anomaly_min_count: skipped
    out = deltas(comp, reg, 9, {"log_switch": 9},
                 "2026-07-10T00:00:00Z", "2026-07-10T01:00:00Z", 3600)
    assert not any(d["type"] == "rate_anomaly" for d in out)


def test_silence_fires_only_for_near_full_day_windows(tmp_path):
    comp, reg = make_compactor(), make_registry(tmp_path)
    # quiet 2h night tick after three active days: NOT silence
    out = deltas(comp, reg, 0, {}, "2026-07-10T00:00:00Z",
                 "2026-07-10T02:00:00Z", 7200)
    assert not any(d["type"] == "silence" for d in out)
    # a whole silent day after three active days: silence
    out = deltas(comp, reg, 0, {}, "2026-07-10T00:00:00Z",
                 "2026-07-11T00:00:00Z", DAY)
    assert any(d["type"] == "silence" for d in out)


def test_silence_requires_recent_activity(tmp_path):
    comp = make_compactor()
    reg = Registry(tmp_path / "r.json")
    reg.set_day_counts("2026-07-08", "alert", 0, {}, DAY)
    reg.set_day_counts("2026-07-09", "alert", 10, {}, DAY)
    out = deltas(comp, reg, 0, {}, "2026-07-10T00:00:00Z",
                 "2026-07-11T00:00:00Z", DAY)
    assert not any(d["type"] == "silence" for d in out)


# ---- first_ever_code is per source -------------------------------------------

CODE_T0, CODE_T1 = "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z"


def first_ever(deltas_: list[dict]) -> list[tuple[str, str]]:
    return [(d["source"], d["value"]) for d in deltas_
            if d["type"] == "first_ever_code"]


def code_deltas(comp, reg, source: str) -> list[dict]:
    return comp._deltas(source, "cdb1", reg, "2026-07-11", CODE_T0, CODE_T1,
                        5, Counter(), DAY)


def test_a_code_known_in_one_source_is_first_ever_in_the_other(tmp_path):
    """A known alert-log code turning up in the dataguard log is signal: each
    source gets its own first_ever_code delta, once."""
    comp = make_compactor()
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-16198", "2026-07-11T04:00:00Z")
    reg.note_code("dataguard", "ORA-16198", "2026-07-11T05:00:00Z")
    assert first_ever(code_deltas(comp, reg, "alert")) == [("alert", "ORA-16198")]
    assert first_ever(code_deltas(comp, reg, "dataguard")) == \
        [("dataguard", "ORA-16198")]


def test_a_code_only_in_the_alert_log_never_deltas_for_dataguard(tmp_path):
    comp = make_compactor()
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-600", "2026-07-11T04:00:00Z")
    assert first_ever(code_deltas(comp, reg, "dataguard")) == []


def test_listener_codes_never_leak_into_a_first_ever_code_delta(tmp_path):
    # listener messages carry ORA/TNS codes too, but only alert and dataguard
    # emit first_ever_code — under one global map they used to leak into both
    comp = make_compactor()
    reg = Registry(tmp_path / "r.json")
    reg.note_code("listener", "ORA-12514", "2026-07-11T04:00:00Z")
    assert first_ever(code_deltas(comp, reg, "alert")) == []
    assert first_ever(code_deltas(comp, reg, "dataguard")) == []


def test_same_code_in_two_sources_deltas_in_each_end_to_end(tmp_path):
    """Whole path: the compactor must attribute each noted code to the source
    it scanned, so one ORA code in both logs yields two labelled deltas."""
    cfg = fx.fixture_config(tmp_path)
    case = {
        "db": "cdb1",
        "window": {"from": CODE_T0, "to": CODE_T1, "day": "2026-07-11"},
        "sources": ["alert", "dataguard"],
        "hits": {
            "alert": [
                {"_index": "oracle-logs-alert-2026.07", "_id": "x-a1",
                 "_source": {"@timestamp": "2026-07-11T04:00:00Z",
                             "db_name": "cdb1",
                             "oracle": {"alert_message": "ORA-16198: LGWR received "
                                        "timeout error from standby",
                                        "msg_type": "ERROR", "msg_level": 1}}}],
            "dataguard": [
                {"_index": ".ds-logs-oracle.dataguard-default-2026.07.11-000020",
                 "_id": "x-d1",
                 "_source": {"@timestamp": "2026-07-11T05:00:00Z",
                             "oracle": {"database": {"name": "cdb1"},
                                        "dataguard": {"event_type": "transport_error",
                                                      "member": "cdb1_stby"}},
                             "error": {"code": "ORA-16198"},
                             "log": {"level": "ERROR"},
                             "message": "Error ORA-16198 received from LGWR while "
                                        "transporting redo to standby cdb1_stby"}}],
        },
    }
    digest = fx.compact_case(cfg, case)
    assert first_ever(digest["deltas"]) == [("alert", "ORA-16198"),
                                            ("dataguard", "ORA-16198")]

    # the next window: known in both sources now, so neither deltas again
    later = json.loads(json.dumps(case).replace("2026-07-11", "2026-07-12"))
    assert first_ever(fx.compact_case(cfg, later)["deltas"]) == []


# ---- new_service noise -------------------------------------------------------

# observed in production: upstream kv-parses a listener connect string and
# files the fragment under service.name
ARTIFACT = "ADDRESS=(PROTOCOL=TCP"
T0, T1 = "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z"


def listener_case(services: list[str]) -> dict:
    return {
        "db": "cdb1",
        "window": {"from": T0, "to": T1, "day": "2026-07-11"},
        "sources": ["listener"],
        "hits": {"listener": [
            {"_index": ".ds-logs-oracle.listener-default-2026.07.11-000020",
             "_id": f"svc-{i}",
             "_source": {
                 "@timestamp": f"2026-07-11T0{i}:00:00Z",
                 "service": {"name": svc},
                 "oracle": {"database": {"name": "cdb1"},
                            "listener": {"command": "establish",
                                         "return_code": 0}},
                 "message": (f"11-JUL-2026 0{i}:00:00 * (CONNECT_DATA="
                             f"(SERVICE_NAME={svc})(CID=(PROGRAM=jdbcapp)"
                             "(HOST=app07)(USER=appsvc))) * establish * 0")}}
            for i, svc in enumerate(services)]},
    }


def new_services(deltas_: list[dict]) -> list[str]:
    return sorted(d["value"] for d in deltas_ if d["type"] == "new_service")


def test_only_name_like_services_are_registered(tmp_path):
    cfg = fx.fixture_config(tmp_path)
    digest = fx.compact_case(
        cfg, listener_case([ARTIFACT, "cdb1.world", "cdb1_stby", "LISTENER"]))
    reg = StateStore(cfg.state_dir).registry("cdb1")
    assert sorted(reg.services) == ["cdb1.world", "cdb1_stby"]
    assert new_services(digest["deltas"]) == ["cdb1.world", "cdb1_stby"]


def test_artifact_already_in_the_registry_never_deltas(tmp_path):
    # pre-existing state written before the registration filter existed: the
    # read path must ignore it rather than the state being migrated
    comp = make_compactor()
    reg = Registry(tmp_path / "r.json")
    for svc in (ARTIFACT, "emrep", "pdb1.example.com"):
        reg.note_service(svc, "2026-07-11T09:00:00Z")
    out = comp._deltas("listener", "cdb1", reg, "2026-07-11", T0, T1,
                       5, Counter(), DAY)
    assert new_services(out) == ["emrep", "pdb1.example.com"]
