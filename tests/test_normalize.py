"""Normalization must tolerate both index layouts in one window: the legacy
flat shape (`db_name`, `oracle.alert_message`, `listener.*`, `dataguard.*`)
and the ECS shape the 2026-07-16 rollover introduced (`oracle.database.name`,
top-level `message`, `oracle.listener.*`, `oracle.dataguard.*`)."""

import pytest

from dbwiki.compactor import Compactor
from dbwiki.normalize import UnsupportedSchemaError, db_of, field_paths, normalize

ECS_FIELDS = ["oracle.database.name", "db_name"]


def hit(source_doc, index="idx-1", doc_id="d1"):
    return {"_index": index, "_id": doc_id, "_source": source_doc}


# ---- db field fallback ---------------------------------------------------

def test_db_of_legacy_flat_field():
    assert db_of({"db_name": "cdb1"}) == "cdb1"


def test_db_of_ecs_nested_field():
    src = {"oracle": {"database": {"name": "cdb1_stby"}}}
    assert db_of(src, ECS_FIELDS) == "cdb1_stby"


def test_db_of_flat_dotted_key():
    assert db_of({"oracle.database.name": "dgnonc"}, ECS_FIELDS) == "dgnonc"


def test_db_of_field_order_wins():
    src = {"db_name": "old", "oracle": {"database": {"name": "new"}}}
    assert db_of(src, ECS_FIELDS) == "new"
    assert db_of(src, ["db_name", "oracle.database.name"]) == "old"


def test_db_of_neither_field():
    assert db_of({"message": "hello"}, ECS_FIELDS) == ""


def test_normalize_mixed_window_layouts():
    old = normalize("alert", hit({"db_name": "cdb1", "@timestamp": "t"}), ECS_FIELDS)
    new = normalize("alert", hit({"oracle": {"database": {"name": "cdb1"}}}), ECS_FIELDS)
    unknown = normalize("alert", hit({"@timestamp": "t"}), ECS_FIELDS)
    assert old["db"] == new["db"] == "cdb1"
    assert unknown["db"] == "unknown"


# ---- per-source payload fallbacks ---------------------------------------

def test_alert_legacy_layout_unchanged():
    ev = normalize("alert", hit({
        "db_name": "cdb1",
        "oracle": {"alert_message": "ORA-00600: internal error",
                   "msg_type": "INCIDENT_ERROR", "msg_id": "x", "msg_level": 1},
    }))
    assert ev["db"] == "cdb1"
    assert ev["message"] == "ORA-00600: internal error"
    assert ev["msg_type"] == "INCIDENT_ERROR"


def test_alert_ecs_layout():
    ev = normalize("alert", hit({
        "oracle": {"database": {"name": "cdb1"}, "instance": {"name": "cdb1"}},
        "message": "Fatal NI connect error 12170.",
        "log": {"level": "ERROR"},
        "event": {"original": "plain text, no xml"},
    }), ECS_FIELDS)
    assert ev["db"] == "cdb1"
    assert ev["message"] == "Fatal NI connect error 12170."
    assert ev["msg_type"] == "ERROR"


def test_alert_xml_recovery_still_wins_over_ecs_message():
    ev = normalize("alert", hit({
        "db_name": "cdb1",
        "oracle": {"alert_message": {}},
        "event": {"original": "<msg><txt>from xml</txt></msg>"},
        "message": "from ecs",
    }))
    assert ev["message"] == "from xml"


def test_listener_ecs_db_from_service_name():
    from dbwiki.normalize import canon_service_db
    assert canon_service_db("cdb1.world") == "cdb1"
    assert canon_service_db("CDB1_DGMGRL.world") == "cdb1"
    assert canon_service_db("cdb1_stby_DGMGRL") == "cdb1_stby"
    assert canon_service_db("ADDRESS=(PROTOCOL=TCP") == ""
    assert canon_service_db("LISTENER") == ""
    ev = normalize("listener", hit({"service": {"name": "CDB1_DGMGRL.world"},
                                    "message": "m"}), ECS_FIELDS)
    assert ev["db"] == "cdb1"


def test_service_name_like_judges_the_canonical_base():
    from dbwiki.normalize import service_name_like
    for good in ("emrep", "cdb1_stby", "pdb1.example.com", "CDB1_DGMGRL.world"):
        assert service_name_like(good)
    for junk in ("ADDRESS=(PROTOCOL=TCP", "LISTENER", "10.1.4.21", ""):
        assert not service_name_like(junk)


def test_db_filter_includes_service_variants():
    from dbwiki.config import Config
    cfg = Config.__new__(Config)
    cfg.sources = {"listener": {"db_fields": ["oracle.database.name"],
                                "db_service_field": "service.name"}}
    (clause,) = cfg.db_filter("listener", "cdb1")
    should = clause["bool"]["should"]
    assert {"term": {"oracle.database.name": "cdb1"}} in should
    (terms,) = [c["terms"]["service.name"] for c in should if "terms" in c]
    for v in ("cdb1", "cdb1.world", "CDB1.world", "cdb1_DGMGRL.world"):
        assert v in terms


def test_listener_ecs_layout():
    detail = ("12-JUL-2026 11:12:59 * (CONNECT_DATA=(CID=(PROGRAM=emagent)"
              "(HOST=app01)(USER=oracle))) * establish * cdb1.world * 0")
    ev = normalize("listener", hit({
        "oracle": {"database": {"name": "cdb1"},
                   "listener": {"command": "establish", "return_code": 0}},
        "service": {"name": "cdb1.world"},
        "message": detail,
    }), ECS_FIELDS)
    assert ev["db"] == "cdb1"
    assert ev["operation"] == "establish"
    assert ev["return_code"] == 0
    assert ev["service"] == "cdb1.world"
    assert ev["program"] == "emagent"


@pytest.mark.parametrize("raw, code", [
    (0, 0), ("0", 0), (" 12514 ", 12514), (12, 12), ("-1", -1),
    ("", None), ("abc", None), ("1.5", None), (True, None), (None, None),
    ("\u0663", None), ("--1", None), ({}, None)])
def test_a_listener_return_code_is_an_int_whatever_type_it_shipped_as(
        raw, code):
    """A string `"0"` used to read as no code at all (issue 16 note)."""
    ev = normalize("listener", hit({
        "oracle": {"database": {"name": "cdb1"},
                   "listener": {"command": "establish", "return_code": raw}},
        "message": "* establish * cdb1.world * 0",
    }), ECS_FIELDS)
    assert ev["return_code"] == code


def test_listener_legacy_layout_unchanged():
    ev = normalize("listener", hit({
        "db_name": "cdb1",
        "listener": {"detail": "d", "operation": "service_died",
                     "return_code": 12, "service_name": "svc"},
    }))
    assert ev["operation"] == "service_died"
    assert ev["return_code"] == 12
    assert ev["service"] == "svc"


def test_dataguard_ecs_layout():
    ev = normalize("dataguard", hit({
        "oracle": {"database": {"name": "cdb1"},
                   "dataguard": {"event_type": "connection_error",
                                 "member": "cdb1_stby"}},
        "error": {"code": "ORA-12543"},
        "log": {"level": "ERROR"},
        "message": "Failed to connect to remote database cdb1_stby.",
    }), ECS_FIELDS)
    assert ev["db"] == "cdb1"
    assert ev["event_type"] == "connection_error"
    assert ev["severity"] == "ERROR"
    assert ev["error_code"] == "ORA-12543"
    assert ev["member"] == "cdb1_stby"
    assert "Failed to connect" in ev["message"]


def test_dataguard_legacy_layout_unchanged():
    ev = normalize("dataguard", hit({
        "db_name": "cdb1",
        "dataguard": {"message": "m", "event_type": "info",
                      "severity": "INFO", "error_code": "", "member": "x"},
    }))
    assert ev["event_type"] == "info"
    assert ev["severity"] == "INFO"


# ---- unsupported-schema diagnostic --------------------------------------

class FakeES:
    """count/search stub for the schema check."""
    def __init__(self, total, carrying, sample=None):
        self.totals = iter([total, carrying])
        self.sample = sample

    def count(self, index, query):
        return next(self.totals)

    def search(self, index, body):
        hits = [hit(self.sample, index="ds-new-000001")] if self.sample else []
        return {"hits": {"hits": hits}}


class FakeCfg:
    sources = {"alert": {"index_patterns": ["idx-*"], "timestamp_field": "@timestamp"}}

    def source(self, name):
        return self.sources[name]

    def db_value_fields(self, name):
        return ["oracle.database.name", "db_name"]


def make_checker(es):
    comp = object.__new__(Compactor)
    comp.cfg = FakeCfg()
    comp.es = es
    comp._schema_ok = set()
    return comp


def test_schema_check_quiet_window_passes():
    make_checker(FakeES(total=0, carrying=0))._check_schema(
        "alert", "2026-07-17T00:00:00Z", "2026-07-18T00:00:00Z")


def test_schema_check_known_layout_passes():
    make_checker(FakeES(total=10, carrying=10))._check_schema(
        "alert", "2026-07-17T00:00:00Z", "2026-07-18T00:00:00Z")


def test_schema_check_unknown_layout_fails_loudly():
    comp = make_checker(FakeES(
        total=10, carrying=0,
        sample={"host": {"name": "h1"}, "fleet_db": "fleetdb0001"}))
    with pytest.raises(UnsupportedSchemaError) as e:
        comp._check_schema("alert", "2026-07-17T00:00:00Z", "2026-07-18T00:00:00Z")
    msg = str(e.value)
    assert "ds-new-000001" in msg          # names the index
    assert "oracle.database.name" in msg   # names the fields that were tried
    assert "fleet_db" in msg               # names the fields actually seen


def test_field_paths_flattens_nested_docs():
    assert field_paths({"a": {"b": 1}, "c": 2}) == ["a.b", "c"]
