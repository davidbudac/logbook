"""The single incident reader: the status vocabulary, what one page means, the
disk walk every consumer now shares, and the two rails that guard the
vocabulary (lint, and the ingest writer's refusal to update a resolved page)."""

import pytest
from fixtures.incident_pages import incident_page
from test_structured import apply, parsed, wiki as structured_wiki

from dbwiki.incidents import (ActionRecord, ErrorAbsent, MonitoringWindow,
                              Status, append_action, by_error_code,
                              link_codes, load_incidents, load_incidents_from,
                              parse_status, read_incident, set_status)
from dbwiki.lint import lint_wiki

UPDATED = "2026-07-27T18:00:00Z"
WINDOW = MonitoringWindow(ErrorAbsent("ORA-00600"),
                          "2026-07-27T18:00:00Z", "2026-07-28T18:00:00Z")
FIRST_ACTION = ActionRecord(at="2026-07-27T17:00:00Z", kind="record-action",
                            actor="dba@example.com",
                            intent="clear the blocking session",
                            summary="killed session 412 on cdb1",
                            status_after=Status.OPEN)
SECOND_ACTION = ActionRecord(at=UPDATED, kind="start-monitoring",
                             actor="dba@example.com",
                             intent="watch for a recurrence",
                             summary="no new ORA-00600 since 16:40",
                             status_after=Status.MONITORING, window=WINDOW,
                             notes="Kept the trace file.\n\nSee the SR.")


@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "incidents").mkdir(parents=True)
    return w


def write(wiki, rel, text):
    p = wiki / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def rules_for(findings, rel):
    return sorted(f.rule for f in findings if f.file == rel)


def test_parse_status_returns_the_member_for_each_literal():
    assert parse_status("open") is Status.OPEN
    assert parse_status("monitoring") is Status.MONITORING
    assert parse_status("resolved") is Status.RESOLVED


def test_parse_status_returns_none_for_a_typo_and_for_a_missing_value():
    assert parse_status("closed") is None
    assert parse_status(None) is None


def test_every_status_has_a_badge_label():
    assert Status.OPEN.label == "Open incident"
    assert Status.MONITORING.label == "Monitoring"
    assert Status.RESOLVED.label == "Resolved"


def test_an_unknown_status_reads_as_open_and_keeps_the_raw_value():
    inc = read_incident(incident_page("cdb1", "T", status="closed"),
                        "incidents/a.md")
    assert inc.status is Status.OPEN
    assert inc.unknown_status == "closed"
    assert inc.is_active


def test_a_missing_status_reads_as_open_with_an_empty_unknown_status():
    inc = read_incident("---\ntype: incident\ndb: cdb1\n---\n\n# T\n",
                        "incidents/a.md")
    assert inc.status is Status.OPEN
    assert inc.unknown_status == ""


def test_a_readable_status_leaves_unknown_status_unset():
    inc = read_incident(incident_page("cdb1", "T", status="resolved"),
                        "incidents/a.md")
    assert inc.status is Status.RESOLVED and inc.unknown_status is None
    assert not inc.is_active


def test_read_incident_returns_the_window_and_the_actions_the_writers_wrote():
    page = set_status(incident_page("cdb1", "T"), Status.MONITORING,
                      updated=UPDATED, monitoring=WINDOW)
    page = append_action(append_action(page, FIRST_ACTION), SECOND_ACTION)
    inc = read_incident(page, "incidents/a.md")
    assert inc.status is Status.MONITORING and inc.monitoring == WINDOW
    assert inc.actions.records == (FIRST_ACTION, SECOND_ACTION)
    assert inc.actions.problems == ()


def test_error_codes_keep_page_order_and_drop_repeats():
    text = incident_page("cdb1", "T", body="[[errors/ORA-600]] again "
                         "[[errors/TNS-12564]] and [[errors/ORA-600]]")
    assert read_incident(text, "incidents/a.md").error_codes == \
        ("ORA-600", "TNS-12564")


def test_an_incident_exists_on_and_after_the_day_it_was_opened():
    inc = read_incident(incident_page("cdb1", "T", opened="2026-07-12T00:00:00Z"),
                        "incidents/a.md")
    assert inc.existed_on("2026-07-13")
    assert inc.existed_on("2026-07-12")
    assert not inc.existed_on("2026-07-11")


def test_an_incident_with_no_opened_date_is_shown_on_every_day():
    inc = read_incident("---\ntype: incident\nstatus: open\n---\n\n# T\n",
                        "incidents/a.md")
    assert inc.opened == "" and inc.existed_on("2020-01-01")


def test_load_incidents_returns_pages_in_path_order(wiki):
    for name in ("2026-07-20-c.md", "2026-07-01-a.md", "2026-07-10-b.md"):
        write(wiki, f"incidents/{name}", incident_page("cdb1", name))
    assert [i.path for i in load_incidents(wiki)] == [
        "incidents/2026-07-01-a.md", "incidents/2026-07-10-b.md",
        "incidents/2026-07-20-c.md"]


def test_load_incidents_skips_pages_that_are_not_incidents(wiki):
    write(wiki, "incidents/real.md", incident_page("cdb1", "real"))
    write(wiki, "incidents/no-frontmatter.md", "# just a note\n")
    write(wiki, "incidents/wrong-type.md",
          "---\ntype: report\n---\n\n# a report\n")
    assert [i.path for i in load_incidents(wiki)] == ["incidents/real.md"]


def test_load_incidents_reads_a_page_whose_actions_do_not_all_parse(wiki):
    page = append_action(incident_page("cdb1", "a"), FIRST_ACTION)
    write(wiki, "incidents/a.md",
          page + "\n## Action 2026-07-27T19:00:00Z\n\nProse, no fence.\n")
    [inc] = load_incidents(wiki)
    assert inc.actions.records == (FIRST_ACTION,)
    assert len(inc.actions.problems) == 1
    assert inc.actions.problems[0].heading == "## Action 2026-07-27T19:00:00Z"


def test_load_incidents_from_keeps_the_order_of_the_paths_given():
    pages = {"incidents/a.md": incident_page("cdb1", "a"),
             "incidents/b.md": incident_page("cdb1", "b")}
    got = load_incidents_from(pages.get, ["incidents/b.md",
                                          "incidents/a.md"])
    assert [i.path for i in got] == ["incidents/b.md", "incidents/a.md"]


def test_load_incidents_from_skips_a_path_the_reader_has_nothing_for():
    pages = {"incidents/a.md": incident_page("cdb1", "a")}
    got = load_incidents_from(pages.get, ["incidents/gone.md",
                                          "incidents/a.md"])
    assert [i.path for i in got] == ["incidents/a.md"]


def test_load_incidents_from_skips_pages_that_are_not_incidents():
    pages = {"incidents/real.md": incident_page("cdb1", "real"),
             "incidents/no-frontmatter.md": "# just a note\n",
             "incidents/wrong-type.md": "---\ntype: report\n---\n\n# r\n"}
    got = load_incidents_from(pages.get, sorted(pages))
    assert [i.path for i in got] == ["incidents/real.md"]


def test_load_incidents_survives_a_page_whose_bytes_are_not_utf8(wiki):
    """One byte that is not UTF-8 must not empty the queue, and must not
    renumber it either: the page keeps its place in path order."""
    write(wiki, "incidents/a.md", incident_page("cdb1", "a"))
    (wiki / "incidents/b.md").write_bytes(
        incident_page("cdb1", "b").encode() + b"\nraw \xff byte\n")
    write(wiki, "incidents/c.md", incident_page("cdb1", "c"))
    assert [i.path for i in load_incidents(wiki)] == [
        "incidents/a.md", "incidents/b.md", "incidents/c.md"]


def test_by_error_code_groups_by_code_and_keeps_page_order(wiki):
    write(wiki, "incidents/a.md",
          incident_page("cdb1", "a", error_codes=("ORA-600",)))
    write(wiki, "incidents/b.md",
          incident_page("cdb1", "b", error_codes=("ORA-600", "TNS-12564")))
    index = by_error_code(load_incidents(wiki))
    assert [i.path for i in index["ORA-600"]] == ["incidents/a.md",
                                                  "incidents/b.md"]
    assert [i.path for i in index["TNS-12564"]] == ["incidents/b.md"]


@pytest.mark.parametrize("status", ["open", "monitoring", "resolved"])
def test_lint_accepts_every_status_in_the_vocabulary(wiki, status):
    """A monitoring page must also say what it is waiting for, so the page
    goes through `set_status`, the only writer allowed to set that pair."""
    rel = "incidents/a.md"
    member = Status(status)
    write(wiki, rel, set_status(
        incident_page("cdb1", "a"), member, updated=UPDATED,
        monitoring=WINDOW if member is Status.MONITORING else None))
    assert "incident-status-invalid" not in rules_for(lint_wiki(wiki), rel)


def test_lint_reports_a_status_outside_the_vocabulary(wiki):
    rel = "incidents/a.md"
    write(wiki, rel, incident_page("cdb1", "a", status="closed"))
    findings = lint_wiki(wiki)
    assert "incident-status-invalid" in rules_for(findings, rel)
    assert any("'closed'" in f.message for f in findings
               if f.rule == "incident-status-invalid")


def test_lint_reports_an_incident_page_with_no_status(wiki):
    rel = "incidents/a.md"
    write(wiki, rel, "---\ntype: incident\ndb: cdb1\n---\n\n# a\n")
    findings = lint_wiki(wiki)
    assert "incident-status-invalid" in rules_for(findings, rel)
    assert any("no `status:` key" in f.message for f in findings
               if f.rule == "incident-status-invalid")


def test_an_update_targeting_a_resolved_incident_is_flagged_and_skipped(
        structured_wiki):
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    before = incident_page("cdb1", "cdb1 loses its standby link",
                           status="resolved", body="Fixed.")
    write(structured_wiki, rel, before)
    res = apply(structured_wiki, parsed(incident={
        "action": "update", "slug": None, "title": None,
        "body": "Still failing today.", "existing_page": rel}))
    assert (structured_wiki / rel).read_text() == before
    assert res["incidents_updated"] == []
    assert any("is resolved" in f and "skipped" in f for f in res["flags"])


def test_unquoted_frontmatter_dates_read_back_as_iso_z():
    page = ("---\ntype: incident\nstatus: open\ndb: cdb1\n"
            "opened: 2026-08-30T14:35:00Z\nupdated: 2026-08-30\n---\n\n# t\n")
    inc = read_incident(page, "incidents/x.md")
    assert inc.opened == "2026-08-30T14:35:00Z"
    assert inc.updated == "2026-08-30"


PAGES = {"errors/ORA-600.md", "errors/ORA-1013.md"}
HELD = PAGES.__contains__


def test_link_codes_links_the_first_bare_mention_of_a_code_that_has_a_page():
    body = ("---\ntype: incident\n---\n\n# ORA-600 on cdb1\n\n"
            "ORA-00600 fired at 07:53, then ORA-600 again at 08:10, and "
            "ORA-1013 once.\n")
    out = link_codes(body, HELD)
    assert "[[errors/ORA-600]] fired at 07:53" in out
    assert "then ORA-600 again" in out
    assert "[[errors/ORA-1013]] once" in out


def test_link_codes_leaves_the_frontmatter_and_a_code_with_no_page_alone():
    body = ("---\ntype: incident\ntitle: ORA-7445 storm\n---\n\n"
            "# t\n\nORA-7445 and ORA-600.\n")
    out = link_codes(body, HELD)
    assert "title: ORA-7445 storm" in out
    assert "ORA-7445 and [[errors/ORA-600]]." in out


def test_link_codes_skips_links_code_spans_fences_tables_and_evidence():
    body = ("# t\n\nAlready [[errors/ORA-600]] linked.\n\n"
            "A span `ORA-1013` and a table:\n\n"
            "| day | note |\n|---|---|\n| 2026-08-01 | ORA-1013 |\n\n"
            "```\nORA-1013 in a fence\n```\n\n"
            "evidence: digests/cdb1/2026-08-01.md ORA-1013\n\n"
            "## Evidence\n\n- 2026-08-01: digests/cdb1/2026-08-01.md "
            "ORA-1013\n")
    assert link_codes(body, HELD) == body


def test_link_codes_is_idempotent():
    body = "# t\n\nORA-600 then ORA-600 then ORA-1013.\n"
    once = link_codes(body, HELD)
    assert link_codes(once, HELD) == once
    assert once.count("[[errors/ORA-600]]") == 1


def test_link_codes_reads_back_as_error_codes_on_the_incident():
    page = ("---\ntype: incident\nstatus: open\ndb: cdb1\n---\n\n"
            "# t\n\nProcess aborted with ORA-1013 after ORA-00600.\n")
    inc = read_incident(link_codes(page, HELD), "incidents/x.md")
    assert inc.error_codes == ("ORA-1013", "ORA-600")
