"""The `## Deltas` block of the markdown twin: what each delta says to the
DBA who reads it, and to the model that reads it after him."""

from dbwiki.digest_md import render_md

DAY = "2026-07-19"
RESIZE = "ALTER DATABASE DATAFILE '/u01/oradata/CDB1/users01.dbf' RESIZE 8G"
SEEN = f"{DAY}T03:41:22Z"


def digest(delta: dict) -> dict:
    return {"db": "cdb1",
            "window": {"from": f"{DAY}T00:00:00Z", "to": "2026-07-20T00:00:00Z",
                       "day": DAY},
            "generated_by": "dbwiki-compactor/test",
            "sources": {}, "deltas": [delta], "changes": [],
            "totals": {"events": 1, "notable_events": 1, "notable_groups": 1},
            "notable": True}


def after_change(gap_s: int = 4213, codes: tuple = ("ORA-1555",)) -> dict:
    return {"type": "after_change", "source": "alert", "rule": "ora_error",
            "codes": list(codes), "first_ts": SEEN, "gap_s": gap_s,
            "change_ts": f"{DAY}T02:31:09Z", "change_rule": "datafile_change",
            "change": RESIZE}


def delta_line(delta: dict) -> str:
    return next(line for line in render_md(digest(delta)).splitlines()
                if line.startswith("- **after a change**"))


def test_the_after_a_change_line_names_the_codes_the_gap_and_the_change():
    assert delta_line(after_change()) == (
        f"- **after a change**: `ORA-1555` (ora_error, alert) first seen "
        f"{SEEN}, 70 min after datafile_change at 02:31:09Z — {RESIZE}")


def test_a_group_with_no_codes_drops_the_codes_segment():
    assert delta_line(after_change(codes=())) == (
        f"- **after a change**: (ora_error, alert) first seen "
        f"{SEEN}, 70 min after datafile_change at 02:31:09Z — {RESIZE}")


def test_a_gap_of_more_than_two_hours_reads_in_hours():
    assert " 3.5 h after datafile_change " in delta_line(after_change(gap_s=12600))
