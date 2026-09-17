"""`pagetext`: the one owner of frontmatter parsing and `## ` section surgery.

The helpers moved here from `structured.py` and `research.py` keep their old
behaviour, and the golden-fixture diff guard is what pins that. What this file
pins is the contract the incident lifecycle writes against: a one-pass
frontmatter rewrite, the flow mapping the `monitoring:` window lives in, and
the label-preserving move that retires an index entry.
"""

import yaml

from dbwiki.pagetext import (fm_flow_map, frontmatter,
                             section_drop_if_empty, section_move_line,
                             section_remove_line, sections, set_frontmatter)

PAGE = ("---\ntype: incident\nstatus: open\ndb: cdb1\n"
        "opened: 2026-08-05T09:00:00Z\n---\n\n"
        "# ORA-600 on cdb1\n\n## Evidence\n\n- 2026-08-05: digest\n")

INDEX = ("---\ntype: index\n---\n\n# Logbook\n\n"
         "## Open incidents\n\n- [[incidents/a]] — A\n- [[incidents/b]] — B\n\n"
         "## Reports\n\n- [[reports/2026-08-05]] — r\n")


def test_set_frontmatter_appends_a_key_the_block_does_not_have():
    out = set_frontmatter(PAGE, {"updated": "2026-08-06T10:00:00Z"})
    assert list(frontmatter(out)) == ["type", "status", "db", "opened",
                                      "updated"]
    assert out.endswith("# ORA-600 on cdb1\n\n## Evidence\n\n"
                        "- 2026-08-05: digest\n")


def test_set_frontmatter_replaces_a_key_at_its_own_position():
    out = set_frontmatter(PAGE, {"status": "resolved"})
    assert frontmatter(out)["status"] == "resolved"
    assert list(frontmatter(out)) == list(frontmatter(PAGE))


def test_set_frontmatter_deletes_the_key_a_none_names():
    out = set_frontmatter(PAGE, {"opened": None})
    assert "opened:" not in out
    assert frontmatter(out) == {"type": "incident", "status": "open",
                                "db": "cdb1"}


def test_set_frontmatter_sets_deletes_and_appends_in_one_pass():
    out = set_frontmatter(PAGE, {"status": "resolved", "opened": None,
                                 "updated": "2026-08-06T10:00:00Z"})
    assert list(frontmatter(out)) == ["type", "status", "db", "updated"]
    assert frontmatter(out)["status"] == "resolved"


def test_set_frontmatter_appends_new_keys_in_updates_order():
    out = set_frontmatter(PAGE, {"monitoring": "{kind: 'manual'}",
                                 "updated": "2026-08-06T10:00:00Z"})
    assert list(frontmatter(out))[-2:] == ["monitoring", "updated"]


def test_set_frontmatter_leaves_a_page_without_frontmatter_alone():
    plain = "# just a note\n\nnothing to rewrite here\n"
    assert set_frontmatter(plain, {"updated": "2026-08-06T10:00:00Z"}) == plain


def test_set_frontmatter_with_nothing_to_do_returns_the_same_bytes():
    assert set_frontmatter(PAGE, {}) == PAGE


def test_set_frontmatter_twice_is_byte_identical():
    updates = {"status": "resolved", "opened": None,
               "updated": "2026-08-06T10:00:00Z"}
    once = set_frontmatter(PAGE, updates)
    assert set_frontmatter(once, updates) == once


def test_fm_flow_map_quotes_every_scalar_and_keeps_insertion_order():
    out = fm_flow_map({"kind": "error_absent", "code": "TNS-12564",
                       "start": "2026-08-05T09:00:00Z"})
    assert out == ("{kind: 'error_absent', code: 'TNS-12564', "
                   "start: '2026-08-05T09:00:00Z'}")


def test_fm_flow_map_keeps_an_iso_timestamp_a_string_through_yaml():
    fields = {"start": "2026-08-05T09:00:00Z", "until": "2026-08-06T09:00:00Z"}
    assert yaml.safe_load(fm_flow_map(fields)) == fields


def test_fm_flow_map_survives_a_comma_and_a_brace_in_a_value():
    fields = {"description": "check dg, then {the} listener"}
    assert yaml.safe_load(fm_flow_map(fields)) == fields


def test_fm_flow_map_escapes_a_single_quote():
    out = fm_flow_map({"description": "the DBA's call"})
    assert "'the DBA''s call'" in out
    assert yaml.safe_load(out) == {"description": "the DBA's call"}


def test_a_flow_map_written_into_frontmatter_reads_back_unchanged():
    fields = {"kind": "manual", "description": "watch, then decide",
              "start": "2026-08-05T09:00:00Z"}
    page = set_frontmatter(PAGE, {"monitoring": fm_flow_map(fields)})
    assert frontmatter(page)["monitoring"] == fields


def test_sections_returns_every_heading_and_its_body_in_page_order():
    assert sections(INDEX) == [
        ("## Open incidents", "\n- [[incidents/a]] — A\n- [[incidents/b]] — B\n"),
        ("## Reports", "\n- [[reports/2026-08-05]] — r"),
    ]


def test_sections_skips_the_frontmatter_and_the_title():
    assert [h for h, _ in sections(PAGE)] == ["## Evidence"]


def test_sections_of_a_page_with_no_section_is_empty():
    assert sections("---\ntype: index\n---\n\n# Logbook\n") == []


def test_section_remove_line_returns_the_line_it_dropped():
    out, removed = section_remove_line(INDEX, "Open incidents",
                                       "- [[incidents/a]]")
    assert removed == "- [[incidents/a]] — A"
    assert "- [[incidents/a]]" not in out
    assert "- [[incidents/b]] — B" in out


def test_section_remove_line_reports_none_when_no_line_has_the_prefix():
    assert section_remove_line(INDEX, "Open incidents",
                               "- [[incidents/z]]") == (INDEX, None)


def test_section_remove_line_reports_none_when_the_section_is_missing():
    assert section_remove_line(INDEX, "Resolved incidents",
                               "- [[") == (INDEX, None)


def test_section_remove_line_keeps_the_section_after_its_last_entry_goes():
    out, _ = section_remove_line(INDEX, "Open incidents", "- [[incidents/a]]")
    out, _ = section_remove_line(out, "Open incidents", "- [[incidents/b]]")
    assert "## Open incidents\n\n## Reports\n" in out
    assert out.endswith("- [[reports/2026-08-05]] — r\n")


def test_section_remove_line_twice_is_byte_identical():
    once, _ = section_remove_line(INDEX, "Open incidents", "- [[incidents/a]]")
    assert section_remove_line(once, "Open incidents",
                               "- [[incidents/a]]") == (once, None)


EMPTIED = INDEX + "\n## Resolved incidents\n"

MIDDLE = ("---\ntype: index\n---\n\n# Logbook\n\n"
          "## Open incidents\n\n- [[incidents/a]] — A\n\n"
          "## Resolved incidents\n\n"
          "## Reports\n\n- [[reports/2026-08-05]] — r\n")


def test_section_drop_if_empty_takes_an_emptied_last_section_off_the_page():
    out = section_drop_if_empty(EMPTIED, "Resolved incidents")
    assert out == INDEX
    assert out.endswith("- [[reports/2026-08-05]] — r\n")


def test_section_drop_if_empty_closes_the_gap_between_the_neighbours():
    out = section_drop_if_empty(MIDDLE, "Resolved incidents")
    assert out == MIDDLE.replace("## Resolved incidents\n\n", "")
    assert [h for h, _ in sections(out)] == ["## Open incidents",
                                             "## Reports"]
    assert "\n\n\n" not in out


def test_section_drop_if_empty_keeps_a_section_that_still_has_an_entry():
    assert section_drop_if_empty(INDEX, "Open incidents") == INDEX


def test_section_drop_if_empty_keeps_a_section_holding_a_placeholder_line():
    page = INDEX + "\n## Resolved incidents\n\n(none yet)\n"
    assert section_drop_if_empty(page, "Resolved incidents") == page


def test_section_drop_if_empty_is_a_no_op_when_the_section_is_absent():
    assert section_drop_if_empty(INDEX, "Resolved incidents") == INDEX


def test_section_drop_if_empty_twice_is_byte_identical():
    once = section_drop_if_empty(EMPTIED, "Resolved incidents")
    assert section_drop_if_empty(once, "Resolved incidents") == once


MOVE = dict(frm="Open incidents", to="Resolved incidents",
            prefix="- [[incidents/a]]",
            fallback="- [[incidents/a]] — a (resolved)")


def test_section_move_line_carries_the_existing_label_bytes_over():
    out = section_move_line(INDEX, **MOVE)
    assert "- [[incidents/a]] — A" in out
    assert MOVE["fallback"] not in out
    assert sections(out)[-1] == ("## Resolved incidents",
                                 "\n- [[incidents/a]] — A")


def test_section_move_line_creates_the_destination_at_the_end_of_the_page():
    out = section_move_line(INDEX, **MOVE)
    assert [h for h, _ in sections(out)] == ["## Open incidents", "## Reports",
                                             "## Resolved incidents"]


def test_section_move_line_writes_the_destination_header_when_it_creates_it():
    out = section_move_line(INDEX, header="| incident | resolved |", **MOVE)
    assert ("## Resolved incidents\n\n| incident | resolved |\n"
            "- [[incidents/a]] — A\n") in out


def test_section_move_line_appends_the_fallback_when_the_source_never_had_it():
    out = section_move_line(INDEX, frm="Open incidents",
                            to="Resolved incidents",
                            prefix="- [[incidents/z]]",
                            fallback="- [[incidents/z]] — z (resolved)")
    assert sections(out)[-1] == ("## Resolved incidents",
                                 "\n- [[incidents/z]] — z (resolved)")


def test_section_move_line_twice_is_byte_identical():
    once = section_move_line(INDEX, **MOVE)
    assert section_move_line(once, **MOVE) == once


def test_section_move_line_on_the_fallback_path_twice_is_byte_identical():
    move = dict(frm="Open incidents", to="Resolved incidents",
                prefix="- [[incidents/z]]",
                fallback="- [[incidents/z]] — z (resolved)")
    once = section_move_line(INDEX, **move)
    assert section_move_line(once, **move) == once
