import pytest

from dbwiki.patterns import (extract_codes, message_template, PatternLibrary,
                             Rule)
from dbwiki.normalize import normalize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def alert_event(message):
    return {"message": message, "msg_type": "UNKNOWN", "msg_id": ""}


def test_extract_codes_normalizes_padding():
    assert extract_codes("ORA-00600: internal error, ORA-600 again, TNS-12541") \
        == ["ORA-600", "TNS-12541"]


def test_message_template_keeps_codes_strips_numbers():
    t = message_template("Archived Log entry 4711 added; ORA-12543 to host 10.0.0.1")
    assert "ORA-12543" in t
    assert "4711" not in t


def test_alert_classification():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = {"message": "ORA-00600: internal error code", "msg_type": "UNKNOWN", "msg_id": ""}
    assert lib.classify(ev).name == "internal_error"
    ev = {"message": "Thread 1 advanced to log sequence 42 (LGWR switch)",
          "msg_type": "UNKNOWN", "msg_id": ""}
    assert lib.classify(ev).klass == "routine"
    ev = {"message": "CREATE TABLESPACE dave_test DATAFILE ...", "msg_type": "UNKNOWN", "msg_id": ""}
    assert lib.classify(ev).name == "tablespace_ddl"


def test_listener_classification():
    lib = PatternLibrary.load(ROOT / "patterns" / "listener.yaml")
    ok = {"message": "... * establish * cdb1.world * 0", "operation": "establish",
          "return_code": 0, "service": "cdb1.world", "program": "sqlplus"}
    rule = lib.classify(ok)
    assert rule.klass == "routine"
    assert rule.counter_key(ok) == "establish cdb1.world sqlplus"
    fail = dict(ok, return_code=12514)
    assert lib.classify(fail).klass == "error"


def test_alert_normalize_xml_fallback():
    hit = {"_index": "i", "_id": "x", "_source": {
        "@timestamp": "2026-07-11T00:00:00Z", "db_name": "cdb1",
        "oracle": {"alert_message": {}, "msg_type": "NOTIFICATION"},
        "event": {"original": "<msg ...><txt>ORA-01555: snapshot too old\n</txt></msg>"}}}
    ev = normalize("alert", hit)
    assert ev["message"] == "ORA-01555: snapshot too old"


@pytest.mark.parametrize("message,name,counter", [
    ("TABLE SYS.WRP$_REPORTS_TIME_BANDS: ADDED INTERVAL PARTITION SYS_P845 (6092) "
     "VALUES LESS THAN (TO_DATE(' 2026-09-09 00:00:00', 'SYYYY-MM-DD HH24:MI:SS', "
     "'NLS_CALENDAR=GREGORIAN'))", "interval_partition_added", "interval_partition"),
    ("PDB1(3):TABLE SYS.WRI$_OPTSTAT_HISTHEAD_HISTORY: ADDED INTERVAL PARTITION "
     "SYS_P846 (6092) VALUES LESS THAN (TO_DATE(' 2026-09-09 00:00:00', "
     "'SYYYY-MM-DD HH24:MI:SS', 'NLS_CALENDAR=GREGORIAN'))",
     "interval_partition_added", "interval_partition"),
    ("Opening scheduler window", "scheduler_window", "scheduler_window"),
    ("PDB1(3):Closing scheduler window", "scheduler_window", "scheduler_window"),
    ("Closing scheduler window\nClosing Resource Manager plan via scheduler window",
     "scheduler_window", "scheduler_window"),
    ("PDB1(3):Setting Resource Manager plan SCHEDULER[0x4D55]:DEFAULT_MAINTENANCE_PLAN via scheduler window",
     "scheduler_window", "scheduler_window"),
    ("Setting Resource Manager plan SCHEDULER[0x4D55]:DEFAULT_MAINTENANCE_PLAN via scheduler window",
     "scheduler_window", "scheduler_window"),
    ('Begin automatic SQL Tuning Advisor run for special tuning task  "SYS_AUTO_SQL_TUNING_TASK"',
     "sql_tuning_advisor", "sql_tuning_advisor"),
    ('End automatic SQL Tuning Advisor run for special tuning task  "SYS_AUTO_SQL_TUNING_TASK"',
     "sql_tuning_advisor", "sql_tuning_advisor"),
    ('PDB1(3):Begin automatic SQL Tuning Advisor run for special tuning task  "SYS_AUTO_SQL_TUNING_TASK"',
     "sql_tuning_advisor", "sql_tuning_advisor"),
    ("rfs (PID:2924): Selected LNO:11 for T-1.S-442 dbid 1214129087 branch 1240864944",
     "rfs_selected_lno", "rfs_selected_lno"),
    ("PDB1(3):rfs (PID:2924): Selected LNO:11 for T-1.S-442 dbid 1214129087 branch 1240864944",
     "rfs_selected_lno", "rfs_selected_lno"),
])
def test_maintenance_chatter_is_routine(message, name, counter):
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event(message)
    rule = lib.classify(ev)
    assert rule.name == name
    assert rule.klass == "routine"
    assert rule.counter_key(ev) == counter


SIGSEGV = ("Exception [type: SIGSEGV, Address not mapped to object] "
           "[ADDR:0x7FFF3E9BCFF8] [PC:0x92C1575, qmxtrProcCorrOpn()+21] "
           "[flags: 0x0, count: 1]")


@pytest.mark.parametrize("message", [SIGSEGV, "PDB1(3):" + SIGSEGV])
def test_sigsegv_exception_is_an_error(message):
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    rule = lib.classify(alert_event(message))
    assert rule.name == "process_exception"
    assert rule.klass == "error"


def test_internal_error_survives_the_maintenance_rules():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event("ORA-00600: internal error code, arguments: [17147]")
    assert lib.classify(ev).name == "internal_error"


def test_parse_error_warning_stays_notable():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event("WARNING: too many parse errors, count=303 SQL hash=0xcd3c142b")
    rule = lib.classify(ev)
    assert rule.notable
    assert rule.klass not in {"routine", "noise"}


def test_maintenance_rules_stay_below_ora_error():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event("PDB1(3):TABLE SYS.WRP$_REPORTS: ADDED INTERVAL PARTITION "
                     "SYS_P845 (6092) failed ORA-01654: unable to extend index")
    rule = lib.classify(ev)
    assert rule.name == "ora_error"
    assert rule.klass == "error"


def test_alert_library_is_at_version_two():
    assert PatternLibrary.load(ROOT / "patterns" / "alert.yaml").version == 2


@pytest.mark.parametrize("message,name", [
    ("ALTER DATABASE DATAFILE '/u01/oradata/CDB1/users01.dbf' RESIZE 8G",
     "datafile_change"),
    ("ALTER DATABASE TEMPFILE '/u01/oradata/CDB1/temp02.dbf' AUTOEXTEND ON NEXT 128M",
     "datafile_change"),
    ("ALTER DATABASE RECOVER MANAGED STANDBY DATABASE DISCONNECT FROM SESSION",
     "dg_role_change"),
    ("ALTER DATABASE ACTIVATE STANDBY DATABASE", "dg_role_change"),
    ("ALTER DATABASE CONVERT TO PHYSICAL STANDBY", "dg_role_change"),
    ("ALTER SYSTEM ENABLE RESTRICTED SESSION", "restricted_session"),
    ("ALTER SYSTEM DISABLE RESTRICTED SESSION", "restricted_session"),
])
def test_administrative_acts_are_lifecycle(message, name):
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    rule = lib.classify(alert_event(message))
    assert rule.name == name
    assert rule.klass == "lifecycle"


@pytest.mark.parametrize("message,name,klass", [
    ("ALTER TABLESPACE users ADD DATAFILE '/u01/oradata/CDB1/users02.dbf' SIZE 4G",
     "tablespace_ddl", "lifecycle"),
    ("ALTER DATABASE COMMIT TO SWITCHOVER TO PHYSICAL STANDBY WITH SESSION SHUTDOWN",
     "dg_role_transition", "dataguard"),
])
def test_the_older_rules_keep_the_lines_they_already_owned(message, name, klass):
    """First match wins, so `datafile_change` and `dg_role_change` only ever
    see what the rules above them leave. Both of these stay notable, and both
    keep the rule name the fleet's history was written against.

    `dg_role_change`'s regex no longer spells out switchover and failover:
    `dg_role_transition` above it claims every line naming either, so those
    alternatives could not fire and the regex was describing a reach it did
    not have."""
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    rule = lib.classify(alert_event(message))
    assert rule.name == name
    assert rule.klass == klass


def test_dg_role_change_no_longer_claims_a_reach_it_never_had():
    dg = next(r for r in PatternLibrary.load(
        ROOT / "patterns" / "alert.yaml").rules if r.name == "dg_role_change")
    assert dg.regex.pattern == (
        "^ALTER DATABASE (RECOVER MANAGED STANDBY|"
        "ACTIVATE (PHYSICAL |LOGICAL )?STANDBY|CONVERT TO)")


# ---- the line a rule actually matched -----------------------------------------

MOUNT_OPEN = Rule({"name": "db_mount_open", "class": "lifecycle",
                   "regex": r"ALTER DATABASE\s+(MOUNT|OPEN|CLOSE|DISMOUNT)"
                            r"|Database mounted|alter pluggable database"})


@pytest.mark.parametrize("message,expected", [
    ("Stopping background process MMON\n"
     "alter pluggable database all close immediate",
     "alter pluggable database all close immediate"),
    ("Dispatchers and shared servers shutdown\n\n"
     "Data Pump shutdown on PDB: 1 in progress\n"
     "ALTER DATABASE CLOSE NORMAL\nStopping Emon pool",
     "ALTER DATABASE CLOSE NORMAL"),
    ("ALTER DATABASE   MOUNT", "ALTER DATABASE   MOUNT"),
])
def test_matched_line_is_the_line_that_names_the_change(message, expected):
    """One ES alert document is many alert-log lines, and the change is named
    on the line the regex hit, not on the line the document opens with."""
    assert MOUNT_OPEN.matched_line(alert_event(message)) == expected


def test_matched_line_reads_the_live_shutdown_document_through_the_library():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event("Stopping background process SMCO\n"
                     "Shutting down instance: further logons disabled")
    rule = lib.classify(ev)
    assert rule.name == "instance_shutdown"
    assert rule.matched_line(ev) == \
        "Shutting down instance: further logons disabled"


def test_matched_line_reads_the_live_pdb_close_document_through_the_library():
    lib = PatternLibrary.load(ROOT / "patterns" / "alert.yaml")
    ev = alert_event("Stopping background process MMON\n"
                     "alter pluggable database all close immediate")
    rule = lib.classify(ev)
    assert rule.name == "db_mount_open"
    assert rule.matched_line(ev) == \
        "alter pluggable database all close immediate"


def test_matched_line_is_empty_without_a_regex_a_value_or_a_match():
    assert Rule({"name": "x", "class": "unmatched"}).matched_line(
        alert_event("anything")) == ""
    assert MOUNT_OPEN.matched_line({"msg_type": "UNKNOWN"}) == ""
    assert MOUNT_OPEN.matched_line(alert_event("Thread 1 advanced")) == ""


def test_matched_line_reads_the_field_the_rule_tests():
    rule = Rule({"name": "svc", "class": "warning", "field": "service",
                 "regex": "cdb1"})
    ev = {"message": "cdb1 in the message", "service": "one\ncdb1.world\ntwo"}
    assert rule.matched_line(ev) == "cdb1.world"
