"""Trigger and registry correctness (verification 2026-09-23, #16).

A rate anomaly must not wake the agent every tick just because the window
grew; registry timestamps are instants, not strings; ORA-0600x is not an
internal error; listener service names match whatever case they are logged
in; an establish line with no parsed return code is judged by its text; and
a fleet bigger than one terms page is said out loud."""

import datetime as dt
from pathlib import Path

import pytest

import fixtures as fx

from dbwiki.compactor import Compactor
from dbwiki.config import Config
from dbwiki.patterns import PatternLibrary
from dbwiki.state import Registry, instant_key
from dbwiki.trigger import decide

ROOT = Path(__file__).resolve().parents[1]
DB = "cdb1"


def alert_hit(ts: str, msg: str, n=[0]) -> dict:
    n[0] += 1
    return {"_index": "oracle-logs-alert-x", "_id": f"t{n[0]}",
            "_source": {"@timestamp": ts, "db_name": DB,
                        "oracle": {"alert_message": msg, "msg_type": "UNKNOWN"}}}


def compactor(tmp_path, hits: list[dict]) -> Compactor:
    cfg = fx.fixture_config(tmp_path)
    cfg.trace_lookup = {}
    comp = Compactor(cfg)
    comp.es = fx.FakeES(cfg, {"alert": hits})
    return comp


# ---- 1. a rate anomaly's window-dependent numbers stay out of the hash --------

def test_same_events_at_a_later_tick_are_content_unchanged(tmp_path):
    reg = Registry(tmp_path / "state" / "registry" / f"{DB}.json")
    for d in ("2026-09-20", "2026-09-21", "2026-09-22"):
        reg.set_day_counts(d, "alert", 24, {"log_switch": 24}, 86400.0)
    reg.save()
    hits = [alert_hit(f"2026-09-23T01:{i:02d}:00Z",
                      "Thread 1 advanced to log sequence 42") for i in range(50)]
    comp = compactor(tmp_path, hits)
    d1 = comp.compact(DB, "2026-09-23T00:00:00Z", "2026-09-23T02:15:00Z", "2026-09-23")
    d2 = comp.compact(DB, "2026-09-23T00:00:00Z", "2026-09-23T04:15:00Z", "2026-09-23")
    assert any(x["type"] == "rate_anomaly" for x in d1["deltas"])
    assert Compactor.content_hash(d1) == Compactor.content_hash(d2)
    led = {"status": "ingested", "window_to": "2026-09-23T02:15:00Z",
           "content_hash": Compactor.content_hash(d1)}
    assert decide(d2, ledger_entry=led,
                  window_to="2026-09-23T04:15:00Z").outcome == "skip"


def test_a_different_anomaly_still_changes_the_hash():
    base = {"deltas": [], "sources": {}}
    a = {**base, "deltas": [{"type": "rate_anomaly", "source": "alert",
                             "counter": "log_switch", "count": 50,
                             "window_hours": 2.25, "rate_per_hour": 22.2,
                             "baseline_median_per_hour": 1.0}]}
    b = {**base, "deltas": [dict(a["deltas"][0], counter="archived_log")]}
    assert Compactor.content_hash(a) != Compactor.content_hash(b)


# ---- 2. registry timestamps compare as instants ----------------------------------

def test_instant_key_orders_mixed_formats_by_time():
    assert instant_key("2026-09-23T00:00:00Z") < instant_key("2026-09-23T00:00:00.500Z")
    assert instant_key("2026-09-23T00:00:00Z") == instant_key("2026-09-23T00:00:00.000Z")
    assert instant_key("2026-09-23T02:00:00+02:00") == instant_key("2026-09-23T00:00:00Z")
    # unparsable values sort after every instant and among themselves as text
    assert instant_key("2026-09-23T00:00:00Z") < instant_key("garbage")
    assert instant_key("") < instant_key("x")


def test_first_ever_code_in_the_first_second_of_the_window(tmp_path):
    hits = [alert_hit("2026-09-23T00:00:00.500Z", "ORA-04031: unable to allocate")]
    comp = compactor(tmp_path, hits)
    # ES range-filters dates as dates; FakeES compares strings and would
    # drop this very hit, so hand the scan over directly
    comp.es.scan = lambda *a, **k: iter(hits)
    d = comp.compact(DB, "2026-09-23T00:00:00Z", "2026-09-23T03:00:00Z", "2026-09-23")
    assert [(x["type"], x["value"]) for x in d["deltas"]] == [
        ("first_ever_code", "ORA-4031")]


def test_first_and_last_seen_are_min_and_max_by_instant(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-600", "2026-09-23T00:00:00.500Z")
    reg.note_code("alert", "ORA-600", "2026-09-23T00:00:00Z")
    reg.note_code("alert", "ORA-600", "2026-09-23T01:00:00+02:00")   # 23:00Z the day before
    reg.note_code("alert", "ORA-600", "2026-09-23T00:00:01Z")
    seen = reg.codes["alert"]["ORA-600"]
    assert seen == {"first_seen": "2026-09-23T01:00:00+02:00",
                    "last_seen": "2026-09-23T00:00:01Z"}
    assert reg.new_codes_in_window("alert", "2026-09-22T23:00:00Z",
                                   "2026-09-23T00:00:00Z") == ["ORA-600"]
    assert reg.new_codes_in_window("alert", "2026-09-23T00:00:00Z",
                                   "2026-09-24T00:00:00Z") == []


# ---- 3. ORA-0600x is not ORA-600 ---------------------------------------------------

@pytest.mark.parametrize("message,internal", [
    ("ORA-00600: internal error code, arguments: [kcbz]", True),
    ("ORA-0600: internal error code", True),
    ("ORA-600: internal error code", True),
    ("ORA-07445: exception encountered: core dump", True),
    ("ORA-7445: exception encountered", True),
    ("ORA-06002: NETASY: port read failure", False),
    ("ORA-06000: NETASY: port open failure", False),
    ("ORA-06009: NETASY: dcb allocation failure", False),
    ("ORA-074451: not a code we know", False),
])
def test_internal_error_matches_600_and_7445_only(message, internal):
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    rule = lib.classify({"message": message, "msg_id": "", "msg_type": ""})
    assert (rule.name == "internal_error") is internal


# ---- 4. listener service names in any case ---------------------------------------

def listener_cfg() -> Config:
    cfg = Config.__new__(Config)
    cfg.sources = {"listener": {"db_fields": ["oracle.database.name"],
                                "db_service_field": "service.name"}}
    return cfg


def service_variants(db: str) -> list[str]:
    (clause,) = listener_cfg().db_filter("listener", db)
    (terms,) = [c["terms"]["service.name"] for c in clause["bool"]["should"]
                if "terms" in c]
    return terms


@pytest.mark.parametrize("db,logged", [
    ("CDB1", "cdb1.world"), ("CDB1", "cdb1"), ("cdb1", "CDB1.WORLD"),
    ("cdb1", "CDB1_DGMGRL.world"), ("Cdb1", "cdb1_dgmgrl.WORLD"),
])
def test_service_variants_cover_every_case(db, logged):
    assert logged in service_variants(db)


def test_service_variants_keep_the_old_spellings_and_have_no_duplicates():
    v = service_variants("cdb1")
    assert {"cdb1", "cdb1.world", "CDB1", "CDB1.world", "cdb1_DGMGRL",
            "cdb1_DGMGRL.world", "CDB1_DGMGRL", "CDB1_DGMGRL.world"} <= set(v)
    assert v[0] == "cdb1" and len(v) == len(set(v))


# ---- 5. establish without a parsed return code -------------------------------------

@pytest.mark.parametrize("message,klass", [
    ("12-JUL-2026 11:12:59 * (CONNECT_DATA=(SID=cdb1)) * establish * cdb1 * 0", "routine"),
    ("12-JUL-2026 11:12:59 * (CONNECT_DATA=(SID=cdb1)) * establish * cdb1 * 0\n", "routine"),
    ("12-JUL-2026 11:12:59 * (CONNECT_DATA=(SID=x)) * establish * x * 12514", "error"),
    ("12-JUL-2026 11:12:59 * (CONNECT_DATA=(SID=x)) * establish * x", "error"),
])
def test_establish_with_no_return_code_is_judged_by_the_line(message, klass):
    lib = PatternLibrary.load(ROOT / "patterns" / "listener.yaml")
    ev = {"message": message, "operation": "establish", "return_code": None,
          "service": "cdb1", "program": ""}
    assert lib.classify(ev).klass == klass


def test_establish_with_a_return_code_ignores_the_text():
    lib = PatternLibrary.load(ROOT / "patterns" / "listener.yaml")
    ev = {"message": "* establish * cdb1 * 0", "operation": "establish",
          "return_code": 12514, "service": "cdb1", "program": ""}
    assert lib.classify(ev).klass == "error"
    ok = lib.classify(dict(ev, message="* establish * cdb1 * 12514", return_code=0))
    assert ok.name == "establish_ok"


# ---- 6. a truncated terms aggregation is not silent --------------------------------

def test_dbs_in_window_warns_when_the_bucket_page_is_full(capsys):
    from dbwiki.es import ES
    es = ES("http://x", "u", "p")
    es.search = lambda index, body: {"aggregations": {
        "db0": {"sum_other_doc_count": 7,
                "buckets": [{"key": "cdb1", "doc_count": 3}]}}}
    assert es.dbs_in_window(["i"], ["db_name"], "@timestamp",
                            "2026-09-23T00:00:00Z", "2026-09-23T01:00:00Z") == {"cdb1": 3}
    err = capsys.readouterr().err
    assert "db_name" in err and "7" in err and "truncated" in err


def test_dbs_in_window_is_quiet_when_every_db_fits(capsys):
    from dbwiki.es import ES
    es = ES("http://x", "u", "p")
    es.search = lambda index, body: {"aggregations": {
        "db0": {"sum_other_doc_count": 0,
                "buckets": [{"key": "cdb1", "doc_count": 3}]}}}
    es.dbs_in_window(["i"], ["db_name"], "@timestamp", "a", "b")
    assert capsys.readouterr().err == ""


def test_instant_key_is_timezone_aware():
    assert isinstance(instant_key("2026-09-23T00:00:00")[1], dt.datetime)
