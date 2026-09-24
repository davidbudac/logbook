"""Storm-collapse grouping in the compactor (verification 2026-09-23, #05).

A group is one (rule, message template) with a count. Three ways it used to
lose evidence: two long messages that differ only past the template width
merged and kept the first event's codes; hex addresses survived the digit
fold, so an ORA-7445 storm filled the group cap on its own; and the drop
counter counted events, not groups, while dropped events vanished from
`totals.notable_events`."""

import fixtures as fx

from dbwiki.compactor import Compactor
from dbwiki.patterns import message_template

DB = "cdb1"
T0, T1, DAY = "2026-09-23T00:00:00Z", "2026-09-23T03:00:00Z", "2026-09-23"


def hit(ts: str, msg: str, n=[0]) -> dict:
    n[0] += 1
    return {"_index": "oracle-logs-alert-x", "_id": f"g{n[0]}",
            "_source": {"@timestamp": ts, "db_name": DB,
                        "oracle": {"alert_message": msg, "msg_type": "UNKNOWN"}}}


def compact(tmp_path, hits: list[dict], **compactor) -> dict:
    cfg = fx.fixture_config(tmp_path)
    cfg.trace_lookup = {}
    cfg.compactor = {**cfg.compactor, **compactor}
    comp = Compactor(cfg)
    comp.es = fx.FakeES(cfg, {"alert": hits})
    return comp.compact(DB, T0, T1, DAY, sources=["alert"])


# ---- codes are part of a group's identity ---------------------------------------

def test_groups_that_differ_only_past_the_template_width_keep_their_codes(tmp_path):
    prefix = ("Errors in file /u01/app/oracle/diag/rdbms/cdb1/cdb1/trace/"
              "cdb1_j000_12345.trc:\n" + "x" * 90 + "\n")
    d = compact(tmp_path, [
        hit("2026-09-23T01:00:00Z", prefix + "ORA-12012: error on auto execute of job"),
        hit("2026-09-23T02:00:00Z", prefix + "ORA-01578: ORACLE data block corrupted")])
    groups = d["sources"]["alert"]["notable"]
    assert sorted(c for g in groups for c in g["codes"]) == ["ORA-12012", "ORA-1578"]
    (corrupt,) = [g for g in groups if "ORA-1578" in g["codes"]]
    assert corrupt["count"] == 1
    assert "ORACLE data block corrupted" in corrupt["message"]


def test_identical_events_still_collapse_into_one_group(tmp_path):
    d = compact(tmp_path, [hit(f"2026-09-23T01:{i:02d}:00Z",
                               "ORA-04031: unable to allocate 4160 bytes")
                           for i in range(30)])
    (g,) = d["sources"]["alert"]["notable"]
    assert g["count"] == 30 and g["codes"] == ["ORA-4031"]


# ---- hex addresses fold like digits --------------------------------------------

def test_hex_addresses_fold_into_the_template():
    a = message_template("ORA-07445: exception encountered: core dump "
                         "[kghalo()+1234] [SIGSEGV] [ADDR:0x7FFE3A] [PC:0x1AF0]")
    b = message_template("ORA-07445: exception encountered: core dump "
                         "[kghalo()+99] [SIGSEGV] [ADDR:0x7edc9f] [PC:0xBEEF01]")
    assert a == b
    assert "ADDR:0x#" in a and "ORA-07445" in a


def test_hex_folding_leaves_words_and_codes_alone():
    t = message_template("ORA-00600: internal error code, arguments: [kcbz_check] "
                         "[0xDEAD] [abc0x12] [12]")
    assert "ORA-00600" in t and "[0x#]" in t and "[#]" in t
    assert "abc0x#" not in t


def test_an_ora7445_storm_is_one_group_and_leaves_room_for_other_errors(tmp_path):
    storm = [hit(f"2026-09-23T01:{i % 60:02d}:{i // 60:02d}Z",
                 f"ORA-07445: exception encountered: core dump [kghalo()+{i}] "
                 f"[SIGSEGV] [ADDR:0x7F{i:04X}E] [PC:0x{i * 7919:X}]")
             for i in range(60)]
    later = [hit("2026-09-23T02:30:00Z", "ORA-01578: ORACLE data block corrupted")]
    d = compact(tmp_path, storm + later)
    s = d["sources"]["alert"]
    codes = {c for g in s["notable"] for c in g["codes"]}
    assert codes == {"ORA-7445", "ORA-1578"}
    assert not s["dropped_notable_groups"]


# ---- the drop counter counts groups; dropped events stay in the totals --------

def test_dropped_groups_are_counted_once_and_their_events_are_totalled(tmp_path):
    # 20 distinct no-code error groups fill the cap (max_groups_per_rule: 20),
    # then one more no-code template 50 times
    fill = [hit(f"2026-09-23T01:{i:02d}:00Z", f"Errors in file /t/cdb1_{'ab'[i % 2]}"
                f"{chr(97 + i)}.trc (incident=1):") for i in range(20)]
    extra = [hit(f"2026-09-23T02:{i:02d}:00Z",
                 "Errors in file /t/zz.trc (incident=9):") for i in range(50)]
    d = compact(tmp_path, fill + extra)
    s = d["sources"]["alert"]
    rule = s["notable"][0]["rule"]
    assert len(s["notable"]) == 20
    assert s["dropped_notable_groups"] == {rule: 1}
    assert s["dropped_notable_events"] == {rule: 50}
    assert d["totals"]["notable_events"] == 70


def test_nothing_dropped_means_no_drop_keys_beyond_the_empty_map(tmp_path):
    d = compact(tmp_path, [hit("2026-09-23T01:00:00Z", "ORA-01578: corrupted")])
    s = d["sources"]["alert"]
    assert s["dropped_notable_groups"] == {}
    assert "dropped_notable_events" not in s and "code_counts" not in s


# ---- at the cap, a new code still gets a group -------------------------------

def test_a_group_carrying_a_code_not_yet_shown_is_kept_past_the_cap(tmp_path):
    fill = [hit(f"2026-09-23T01:{i:02d}:00Z", f"ORA-{1000 + i}: boom")
            for i in range(20)]
    corrupt = [hit(f"2026-09-23T02:{i:02d}:00Z",
                   "ORA-01578: ORACLE data block corrupted") for i in range(50)]
    d = compact(tmp_path, fill + corrupt)
    s = d["sources"]["alert"]
    (g,) = [g for g in s["notable"] if "ORA-1578" in g["codes"]]
    assert g["count"] == 50
    assert not s["dropped_notable_groups"]
    assert d["totals"]["notable_events"] == 70


def test_new_code_overflow_is_bounded_and_counted_per_code(tmp_path):
    # 20 codes fill the cap, 20 more take the new-code overflow (one cap's
    # worth); the next 5 new codes are dropped but still counted per code
    hits = [hit(f"2026-09-23T01:{i:02d}:00Z", f"ORA-{1000 + i}: boom")
            for i in range(45)]
    hits += [hit("2026-09-23T02:00:00Z", "ORA-1044: boom")]
    d = compact(tmp_path, hits)
    s = d["sources"]["alert"]
    rule = s["notable"][0]["rule"]
    assert len(s["notable"]) == 40
    assert s["dropped_notable_groups"] == {rule: 5}
    assert s["dropped_notable_events"] == {rule: 6}
    assert s["code_counts"]["ORA-1044"] == 2      # both events dropped
    assert s["code_counts"]["ORA-1000"] == 1
    assert len(s["code_counts"]) == 45
    assert d["totals"]["notable_events"] == 46


def test_the_markdown_names_dropped_groups_events_and_codes(tmp_path):
    from dbwiki.digest_md import render_md
    hits = [hit(f"2026-09-23T01:{i:02d}:00Z", f"ORA-{1000 + i}: boom")
            for i in range(41)]
    md = render_md(compact(tmp_path, hits))
    assert "distinct groups dropped: ora_error: 1 (1 events)" in md
    assert "codes only in dropped groups: `ORA-1040` ×1" in md
