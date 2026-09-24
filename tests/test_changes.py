"""The `changes` view: derivation, the recorded key, and the cross-day reader."""

import datetime as dt
import json

from dbwiki import structured
from dbwiki.changes import (LIFECYCLE, MAX_MESSAGE, Change, ChangeStamp,
                            after_change, changes_of, of_digest, recent)
from dbwiki.compactor import Compactor

LONG_ALTER = "ALTER SYSTEM SET event='" + "x" * 400 + "' SCOPE=SPFILE;"


def group(rule: str, klass: str, ts: str, message: str, count: int = 1,
          headline: str | None = None) -> dict:
    return {"rule": rule, "class": klass, "count": count, "first_ts": ts,
            "last_ts": ts, "message": message, "template": message,
            **({"headline": headline} if headline is not None else {})}


def digest(day: str, sources: dict, **extra) -> dict:
    return {"db": "cdb1", "window": {"from": f"{day}T00:00:00Z",
                                     "to": f"{day}T23:59:59Z", "day": day},
            "sources": {n: {"notable": groups} for n, groups in sources.items()},
            **extra}


def test_max_message_tracks_the_constant_it_cannot_import():
    assert MAX_MESSAGE == structured.MAX_LINE


def test_only_lifecycle_groups_become_changes_ordered_by_ts():
    d = digest("2026-07-19", {
        "alert": [
            group("ora_error", "error", "2026-07-19T01:00:00Z", "ORA-1555: too old"),
            group("parameter_change", LIFECYCLE, "2026-07-19T04:00:00Z",
                  "ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;", count=2),
        ],
        "dataguard": [
            group("instance_startup", LIFECYCLE, "2026-07-19T02:00:00Z",
                  "Starting ORACLE instance (normal)"),
        ]})
    got = changes_of(d)
    assert [c.rule for c in got] == ["instance_startup", "parameter_change"]
    assert [c.ts for c in got] == ["2026-07-19T02:00:00Z", "2026-07-19T04:00:00Z"]
    assert [c.day for c in got] == ["2026-07-19"] * 2
    assert got[1].count == 2
    assert got[1].message == "ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;"


def test_a_multi_line_group_message_collapses_to_its_first_line():
    d = digest("2026-07-19", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-07-19T03:00:00Z",
              "\n  \nALTER DATABASE OPEN\nCompleted: ALTER DATABASE OPEN\n")]})
    assert changes_of(d)[0].message == "ALTER DATABASE OPEN"


LIVE_PDB_CLOSE = ("Stopping background process MMON\n"
                  "alter pluggable database all close immediate")


def test_the_headline_the_compactor_recorded_wins_over_the_first_line():
    """The rule matched the second line, so the second line names the change.
    Without the key the digest would say "Stopping background process MMON",
    which is not what the operator did."""
    d = digest("2026-08-31", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-08-31T08:43:46Z",
              LIVE_PDB_CLOSE,
              headline="alter pluggable database all close immediate")]})
    assert changes_of(d)[0].message == \
        "alter pluggable database all close immediate"


def test_a_digest_written_before_the_headline_key_falls_back_to_line_one():
    d = digest("2026-08-31", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-08-31T08:43:46Z",
              LIVE_PDB_CLOSE)]})
    assert changes_of(d)[0].message == "Stopping background process MMON"


def test_an_empty_headline_falls_back_too():
    d = digest("2026-08-31", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-08-31T08:43:46Z",
              LIVE_PDB_CLOSE, headline="")]})
    assert changes_of(d)[0].message == "Stopping background process MMON"


def test_an_overlong_headline_is_truncated():
    d = digest("2026-07-19", {"alert": [
        group("parameter_change", LIFECYCLE, "2026-07-19T03:00:00Z",
              "first line", headline=LONG_ALTER)]})
    assert changes_of(d)[0].message == LONG_ALTER[:MAX_MESSAGE]


def test_a_blank_group_message_yields_an_empty_change_message():
    d = digest("2026-07-19", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-07-19T03:00:00Z", "\n   \n")]})
    assert changes_of(d)[0].message == ""


def test_an_overlong_message_is_truncated():
    d = digest("2026-07-19", {"alert": [
        group("parameter_change", LIFECYCLE, "2026-07-19T03:00:00Z", LONG_ALTER)]})
    message = changes_of(d)[0].message
    assert len(message) == MAX_MESSAGE
    assert message == LONG_ALTER[:MAX_MESSAGE]


def test_a_change_survives_a_round_trip_through_its_dict():
    c = Change(day="2026-07-19", ts="2026-07-19T03:00:00Z", rule="parameter_change",
               count=2, message="ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;")
    assert Change.from_dict(c.to_dict()) == c
    assert set(c.to_dict()) == {"day", "ts", "rule", "count", "message"}


def test_of_digest_prefers_the_recorded_key_over_the_groups():
    recorded = Change(day="2026-07-19", ts="2026-07-19T09:00:00Z",
                      rule="restricted_session", count=1,
                      message="ALTER SYSTEM ENABLE RESTRICTED SESSION")
    d = digest("2026-07-19",
               {"alert": [group("parameter_change", LIFECYCLE,
                                "2026-07-19T03:00:00Z", "ALTER SYSTEM SET x=1;")]},
               changes=[recorded.to_dict()])
    assert of_digest(d) == (recorded,)


def test_of_digest_keeps_a_recorded_empty_list_empty():
    d = digest("2026-07-19",
               {"alert": [group("parameter_change", LIFECYCLE,
                                "2026-07-19T03:00:00Z", "ALTER SYSTEM SET x=1;")]},
               changes=[])
    assert of_digest(d) == ()


def test_of_digest_derives_when_the_key_is_absent():
    d = digest("2026-07-19", {"alert": [
        group("parameter_change", LIFECYCLE, "2026-07-19T03:00:00Z",
              "ALTER SYSTEM SET x=1;")]})
    assert of_digest(d) == changes_of(d)


# ---- after_change ------------------------------------------------------------

STARTUP = ChangeStamp(ts="2026-07-19T02:14:05Z", rule="instance_startup",
                      headline="Starting ORACLE instance (normal)")
RESIZE = ChangeStamp(
    ts="2026-07-19T02:31:09Z", rule="datafile_change",
    headline="ALTER DATABASE DATAFILE '/u01/oradata/CDB1/users01.dbf' RESIZE 8G")
ORA_1555 = "2026-07-19T03:41:22Z"


def error_group(rule: str, ts: str, codes: list | None = None,
                klass: str = "error") -> dict:
    return {"rule": rule, "class": klass, "first_ts": ts, "codes": codes or []}


def sections(**by_source: list) -> dict:
    return {name: {"notable": groups} for name, groups in by_source.items()}


def test_after_change_names_the_latest_change_before_the_error():
    got = after_change([STARTUP, RESIZE], sections(
        alert=[error_group("ora_error", ORA_1555, ["ORA-1555"])]), hours=2)
    assert got == [{"type": "after_change", "source": "alert",
                    "rule": "ora_error", "codes": ["ORA-1555"],
                    "first_ts": ORA_1555, "gap_s": 4213,
                    "change_ts": RESIZE.ts, "change_rule": "datafile_change",
                    "change": RESIZE.headline}]


def test_a_change_after_the_error_never_matches():
    later = ChangeStamp(ts="2026-07-19T04:05:33Z", rule="db_mount_open",
                        headline="alter pluggable database all close immediate")
    assert after_change([later], sections(
        alert=[error_group("ora_error", ORA_1555)]), hours=2) == []


def test_the_window_bound_is_inclusive_at_exactly_hours():
    exact = ChangeStamp(ts="2026-07-19T01:41:22Z", rule="parameter_change",
                        headline="ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;")
    seen = sections(alert=[error_group("ora_error", ORA_1555)])
    assert after_change([exact], seen, hours=2)[0]["gap_s"] == 7200
    assert after_change([exact], seen, hours=1.999) == []


def test_only_error_and_dataguard_groups_get_an_after_change_delta():
    assert after_change([RESIZE], sections(alert=[
        error_group("cannot_allocate_log", ORA_1555, klass="warning"),
        error_group("parameter_change", ORA_1555, klass=LIFECYCLE),
        error_group("mystery_line", ORA_1555, klass="unmatched")]),
        hours=2) == []


def test_every_dataguard_group_gets_an_after_change_delta():
    """A role change or a gap right after somebody touched the database is the
    same timing fact as an ORA error there — every dataguard-class group, not
    only the transport errors the broker log classifies as `error`."""
    mrp = "2026-07-19T02:40:00Z"
    got = after_change([RESIZE], sections(
        alert=[error_group("dg_mrp_lifecycle", mrp, klass="dataguard")],
        dataguard=[error_group("role_change", ORA_1555, klass="dataguard")]),
        hours=2)
    assert [(d["source"], d["rule"], d["gap_s"]) for d in got] == [
        ("alert", "dg_mrp_lifecycle", 531),
        ("dataguard", "role_change", 4213)]


def test_the_cap_keeps_the_earliest_groups_and_drops_the_rest():
    groups = [error_group(f"rule_{i}", f"2026-07-19T03:0{i}:00Z")
              for i in range(5)]
    got = after_change([RESIZE], sections(alert=groups), hours=2, cap=2)
    assert [d["rule"] for d in got] == ["rule_0", "rule_1"]


def test_a_window_of_zero_hours_or_less_produces_no_deltas():
    seen = sections(alert=[error_group("ora_error", ORA_1555)])
    assert after_change([RESIZE], seen, hours=0) == []
    assert after_change([RESIZE], seen, hours=-1) == []


def test_a_timestamp_that_will_not_parse_is_skipped_rather_than_raised():
    broken = ChangeStamp(ts="not a timestamp", rule="parameter_change",
                         headline="ALTER SYSTEM SET x=1;")
    seen = sections(alert=[error_group("ora_error", ORA_1555)])
    assert after_change([broken], seen, hours=2) == []
    assert after_change([broken, RESIZE], seen, hours=2)[0]["change_rule"] == \
        "datafile_change"
    assert after_change([RESIZE], sections(
        alert=[error_group("ora_error", "whenever")]), hours=2) == []


# ---- recent() ----------------------------------------------------------------

TODAY = dt.date(2026, 7, 20)


def write_digest(root, day: str, body) -> None:
    path = root / "digests" / "cdb1" / f"{day}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body if isinstance(body, str) else json.dumps(body))


def lifecycle_digest(day: str, rule: str, **extra) -> dict:
    return digest(day, {"alert": [group(rule, LIFECYCLE, f"{day}T03:00:00Z",
                                        f"ALTER SYSTEM SET {rule}=1;")]}, **extra)


def test_recent_reads_the_window_newest_day_first(tmp_path):
    write_digest(tmp_path, "2026-07-19", lifecycle_digest("2026-07-19", "yesterday"))
    derived = lifecycle_digest("2026-07-18", "derived")
    write_digest(tmp_path, "2026-07-18", derived)
    recorded = lifecycle_digest("2026-07-17", "ignored")
    recorded["changes"] = [Change(day="2026-07-17", ts="2026-07-17T05:00:00Z",
                                  rule="recorded", count=3,
                                  message="ALTER DATABASE OPEN").to_dict()]
    write_digest(tmp_path, "2026-07-17", recorded)

    got = recent(tmp_path, "cdb1", today=TODAY, days=3)
    assert [c.day for c in got] == ["2026-07-19", "2026-07-18", "2026-07-17"]
    assert [c.rule for c in got] == ["yesterday", "derived", "recorded"]
    assert got[2].count == 3


def test_recent_includes_the_oldest_day_and_excludes_today(tmp_path):
    write_digest(tmp_path, "2026-07-20", lifecycle_digest("2026-07-20", "today"))
    write_digest(tmp_path, "2026-07-18", lifecycle_digest("2026-07-18", "edge"))
    write_digest(tmp_path, "2026-07-17", lifecycle_digest("2026-07-17", "before"))

    got = recent(tmp_path, "cdb1", today=TODAY, days=2)
    assert [c.rule for c in got] == ["edge"], \
        "today - days is in the window; today itself and anything older is not"


def test_recent_skips_missing_and_broken_digests(tmp_path):
    write_digest(tmp_path, "2026-07-19", "{not json at all")
    write_digest(tmp_path, "2026-07-18", {"window": {"day": "2026-07-18"}})
    write_digest(tmp_path, "2026-07-17", lifecycle_digest("2026-07-17", "intact"))

    got = recent(tmp_path, "cdb1", today=TODAY, days=5)
    assert [c.rule for c in got] == ["intact"]


def test_recent_reads_nothing_when_days_is_zero(tmp_path):
    write_digest(tmp_path, "2026-07-19", lifecycle_digest("2026-07-19", "yesterday"))
    assert recent(tmp_path, "cdb1", today=TODAY, days=0) == ()
    assert recent(tmp_path, "cdb1", today=TODAY, days=-1) == ()


# ---- the key is a view, not content ------------------------------------------

def test_the_changes_key_never_moves_the_content_hash():
    d = digest("2026-07-19", {"alert": [
        group("parameter_change", LIFECYCLE, "2026-07-19T03:00:00Z",
              "ALTER SYSTEM SET x=1;")]}, deltas=[])
    d["sources"]["alert"]["total_events"] = 4
    before = Compactor.content_hash(d)
    d["changes"] = [c.to_dict() for c in changes_of(d)]
    assert d["changes"], "the fixture has a lifecycle group to record"
    assert Compactor.content_hash(d) == before


def test_the_headline_key_never_moves_the_content_hash():
    """`content_hash` drives run-tick dedupe off (rule, template, count).
    Recording which line the rule matched changes none of the three, so a
    recompaction must not re-ingest the fleet."""
    d = digest("2026-08-31", {"alert": [
        group("db_mount_open", LIFECYCLE, "2026-08-31T08:43:46Z",
              LIVE_PDB_CLOSE)]}, deltas=[])
    d["sources"]["alert"]["total_events"] = 4
    before = Compactor.content_hash(d)
    d["sources"]["alert"]["notable"][0]["headline"] = \
        "alter pluggable database all close immediate"
    assert Compactor.content_hash(d) == before
