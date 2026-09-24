"""The database's own history as the ingest prompt sees it: which days and
incidents fall in the window, which journal days the digests say were quiet,
how the five lists are ordered and capped, and the block they render to. No
model and no network — `gather` is a pure read of a wiki tree."""

import datetime as dt
import json

import pytest
from fixtures.incident_pages import incident_page

from dbwiki import db_history as dh
from dbwiki.changes import LIFECYCLE
from dbwiki.incidents import ActionRecord, Outcome, Status, render_action
from dbwiki.structured import MAX_LINE

TODAY = dt.date(2026, 9, 13)
DB = "cdb1"
CODE = "ORA-1653"

ERROR_PAGE = (
    "---\ntype: error-class\nupdated: 2026-09-01T00:00:00Z\n---\n\n"
    f"# {CODE}\n\n## Occurrences\n\n"
    "| day | db | note | evidence |\n|---|---|---|---|\n\n"
    "## Resolution history\n\n"
    "| resolved | db | incident | remediation | evidence |\n"
    "|---|---|---|---|---|\n")


@pytest.fixture
def wiki(tmp_path):
    root = tmp_path / "wiki"
    (root / "incidents").mkdir(parents=True)
    (root / "errors").mkdir()
    (root / "digests" / DB).mkdir(parents=True)
    (root / "databases" / DB / "journal").mkdir(parents=True)
    return root


def journal(wiki, month: str, *entries: tuple[str, str], db: str = DB) -> None:
    body = "".join(f"\n## {day} — {headline}\n\nprose.\n"
                   for day, headline in entries)
    (wiki / "databases" / db / "journal" / f"{month}.md").write_text(
        f"---\ntype: journal\ndb: {db}\n---\n\n# {db} journal {month}\n{body}")


def group(rule: str, ts: str, message: str, count: int = 1) -> dict:
    return {"rule": rule, "class": LIFECYCLE, "count": count, "first_ts": ts,
            "last_ts": ts, "message": message, "template": message}


def digest(wiki, day: str, *, notable: bool = False,
           groups: tuple[dict, ...] = (), db: str = DB,
           totals: dict | None = None) -> None:
    (wiki / "digests" / db / f"{day}.json").write_text(json.dumps({
        "db": db, "notable": notable,
        "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z",
                   "day": day},
        **({"totals": totals} if totals is not None else {}),
        "sources": {"alert": {"notable": list(groups)}}}))


def action(at: str, *, kind: str = "record-action", summary: str = "did a thing",
           outcome: str = "succeeded", status_after: str = "open") -> str:
    return render_action(ActionRecord(
        at=at, kind=kind, actor="dba@example.com", intent="fix it",
        summary=summary, status_after=Status(status_after),
        outcome=Outcome(outcome)))


def incident(wiki, slug: str, *actions: str, db: str = DB,
             status: str = "open", opened: str = "2026-08-01T00:00:00Z",
             updated: str = "", title: str = "Something broke",
             codes: tuple[str, ...] = (CODE,)) -> None:
    text = incident_page(db, title, status=status, opened=opened,
                         updated=updated, error_codes=codes)
    (wiki / "incidents" / f"{slug}.md").write_text(
        text + "".join(f"\n{a}" for a in actions))


def gather(wiki, *, codes: tuple[str, ...] = (), days: int = 90):
    return dh.gather(wiki, DB, today=TODAY, codes=codes, days=days)


# ---- nothing to say -----------------------------------------------------------

def test_an_empty_wiki_has_no_history_and_renders_nothing(tmp_path):
    history = dh.gather(tmp_path / "gone", DB, today=TODAY, codes=(CODE,),
                        days=90)
    assert history.is_empty()
    assert dh.render(history) == ""


def test_days_zero_reads_nothing_and_renders_nothing(wiki):
    journal(wiki, "2026-09", ("2026-09-12", "cdb1 2026-09-12: a bad night"))
    incident(wiki, "2026-09-01-cdb1-x")
    history = gather(wiki, codes=(CODE,), days=0)
    assert history.is_empty() and history.days == 0
    assert dh.render(history) == ""


def test_the_dataclass_carries_the_day_it_was_built_for(wiki):
    assert gather(wiki).today == "2026-09-13"


# ---- journal ------------------------------------------------------------------

def test_a_day_whose_digest_says_quiet_is_dropped(wiki):
    journal(wiki, "2026-09", ("2026-09-10", "cdb1 2026-09-10: nothing much"))
    digest(wiki, "2026-09-10", notable=False)
    assert gather(wiki).journal == ()


def test_the_same_quiet_day_is_kept_once_it_carries_a_change(wiki):
    journal(wiki, "2026-09", ("2026-09-10", "cdb1 2026-09-10: nothing much"))
    digest(wiki, "2026-09-10", notable=False, groups=(
        group("parameter_change", "2026-09-10T08:00:00Z",
              "ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;"),))
    assert [e.day for e in gather(wiki).journal] == ["2026-09-10"]


def test_a_day_with_no_events_is_quiet_though_its_digest_says_notable(wiki):
    """A silence delta sets `notable`, so the emptiest day of all arrives
    marked notable with a journal line that says only that nothing came."""
    journal(wiki, "2026-09", ("2026-09-03", "zero events, zero notable "
                              "alerts; silence"))
    digest(wiki, "2026-09-03", notable=True, totals={"events": 0})
    assert gather(wiki).journal == ()


def test_a_day_with_no_events_is_quiet_even_carrying_a_change(wiki):
    journal(wiki, "2026-09", ("2026-09-03", "silence"))
    digest(wiki, "2026-09-03", notable=True, totals={"events": 0}, groups=(
        group("parameter_change", "2026-09-03T08:00:00Z", "ALTER SYSTEM SET x=1;"),))
    assert gather(wiki).journal == ()


def test_a_day_with_events_is_read_the_way_it_always_was(wiki):
    journal(wiki, "2026-09", ("2026-09-03", "a real night"))
    digest(wiki, "2026-09-03", notable=True, totals={"events": 1448})
    assert [e.day for e in gather(wiki).journal] == ["2026-09-03"]


def test_a_digest_whose_totals_are_malformed_is_not_called_quiet(wiki):
    journal(wiki, "2026-09", ("2026-09-03", "unreadable but real"))
    (wiki / "digests" / DB / "2026-09-03.json").write_text(json.dumps({
        "db": DB, "notable": False, "totals": "not a mapping",
        "window": {"day": "2026-09-03"}, "sources": {}}))
    assert [e.day for e in gather(wiki).journal] == ["2026-09-03"]


def test_a_notable_day_is_kept(wiki):
    journal(wiki, "2026-09", ("2026-09-10", "cdb1 2026-09-10: ORA-1653"))
    digest(wiki, "2026-09-10", notable=True)
    assert [e.day for e in gather(wiki).journal] == ["2026-09-10"]


def test_a_day_with_no_digest_file_keeps_its_entry(wiki):
    journal(wiki, "2026-09", ("2026-09-10", "cdb1 2026-09-10: nothing much"))
    assert [e.day for e in gather(wiki).journal] == ["2026-09-10"]


def test_an_unparseable_digest_keeps_the_entry(wiki):
    journal(wiki, "2026-09", ("2026-09-10", "cdb1 2026-09-10: nothing much"))
    (wiki / "digests" / DB / "2026-09-10.json").write_text("{not json")
    assert [e.day for e in gather(wiki).journal] == ["2026-09-10"]


def test_journal_days_outside_the_window_are_dropped_at_both_edges(wiki):
    journal(wiki, "2026-06", ("2026-06-14", "too old"))
    journal(wiki, "2026-09", ("2026-06-15", "first day in window"),
            ("2026-09-12", "last day in window"),
            ("2026-09-13", "today is not history"))
    assert [e.day for e in gather(wiki).journal] == ["2026-09-12", "2026-06-15"]


def test_journal_entries_are_newest_first_across_month_files(wiki):
    journal(wiki, "2026-07", ("2026-07-02", "july"))
    journal(wiki, "2026-08", ("2026-08-02", "august"))
    journal(wiki, "2026-09", ("2026-09-02", "september"))
    assert [e.day for e in gather(wiki).journal] == [
        "2026-09-02", "2026-08-02", "2026-07-02"]


def test_journal_is_capped(wiki):
    journal(wiki, "2026-09", *((f"2026-09-{d:02d}", f"day {d}")
                               for d in range(1, 13)))
    got = gather(wiki).journal
    assert len(got) == dh.MAX_JOURNAL
    assert got[0].day == "2026-09-12"


def test_another_databases_journal_is_not_read(wiki):
    (wiki / "databases" / "cdb2" / "journal").mkdir(parents=True)
    journal(wiki, "2026-09", ("2026-09-10", "cdb2 had a night"), db="cdb2")
    assert gather(wiki).journal == ()


# ---- changes ------------------------------------------------------------------

def test_changes_come_from_the_windowed_digests_newest_day_first(wiki):
    digest(wiki, "2026-09-12", groups=(
        group("db_mount_open", "2026-09-12T02:00:00Z", "ALTER DATABASE OPEN"),))
    digest(wiki, "2026-09-11", groups=(
        group("parameter_change", "2026-09-11T09:00:00Z",
              "ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;", count=2),))
    digest(wiki, "2026-06-14", groups=(
        group("instance_startup", "2026-06-14T01:00:00Z", "too old"),))
    assert [c.rule for c in gather(wiki).changes] == ["db_mount_open",
                                                      "parameter_change"]


def test_one_restart_folds_to_one_line_per_rule(wiki):
    """Eleven lifecycle groups, four rules, one line each: the restart that
    used to spend the whole change budget now spends four lines of it."""
    digest(wiki, "2026-08-31", notable=True, totals={"events": 1448}, groups=(
        group("instance_shutdown", "2026-08-31T08:43:37Z",
              "Shutting down ORACLE instance (immediate)"),
        group("instance_shutdown", "2026-08-31T08:43:40Z",
              "Shutting down instance: further logons disabled"),
        group("db_mount_open", "2026-08-31T08:43:46Z",
              "alter pluggable database all close immediate"),
        group("db_mount_open", "2026-08-31T08:43:58Z",
              "ALTER DATABASE CLOSE NORMAL"),
        group("instance_shutdown", "2026-08-31T08:44:26Z",
              "Instance shutdown complete"),
        group("instance_startup", "2026-08-31T19:14:20Z",
              "Starting ORACLE instance (normal)"),
        group("db_mount_open", "2026-08-31T19:14:29Z", "ALTER DATABASE   MOUNT"),
        group("db_mount_open", "2026-08-31T19:14:33Z",
              "Database mounted in Exclusive Mode"),
        group("parameter_change", "2026-08-31T19:14:48Z",
              "ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;"),
        group("parameter_change", "2026-08-31T19:14:49Z",
              "ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;"),
        group("redo_config_change", "2026-08-31T19:15:00Z",
              "ALTER DATABASE ADD LOGFILE", count=2)))
    folded = gather(wiki).changes
    assert [(c.rule, c.count) for c in folded] == [
        ("instance_shutdown", 3), ("db_mount_open", 4), ("instance_startup", 1),
        ("parameter_change", 2), ("redo_config_change", 2)]
    assert folded[1].headlines == (
        "alter pluggable database all close immediate",
        "ALTER DATABASE CLOSE NORMAL", "ALTER DATABASE   MOUNT",
        "Database mounted in Exclusive Mode")
    assert folded[3].headlines == (
        "ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;",), \
        "the same line twice is one headline"


def test_two_rules_on_one_day_stay_two_lines(wiki):
    digest(wiki, "2026-09-11", groups=(
        group("db_mount_open", "2026-09-11T02:00:00Z", "ALTER DATABASE OPEN"),
        group("parameter_change", "2026-09-11T01:00:00Z",
              "ALTER SYSTEM SET x=1;")))
    assert [(c.day, c.rule) for c in gather(wiki).changes] == [
        ("2026-09-11", "parameter_change"), ("2026-09-11", "db_mount_open")]


def test_one_rule_across_two_days_stays_two_lines(wiki):
    digest(wiki, "2026-09-11", groups=(
        group("db_mount_open", "2026-09-11T02:00:00Z", "ALTER DATABASE OPEN"),))
    digest(wiki, "2026-09-12", groups=(
        group("db_mount_open", "2026-09-12T02:00:00Z", "ALTER DATABASE OPEN"),))
    assert [(c.day, c.count) for c in gather(wiki).changes] == [
        ("2026-09-12", 1), ("2026-09-11", 1)]


def test_a_change_with_no_message_contributes_no_headline(wiki):
    digest(wiki, "2026-09-11", groups=(
        group("db_mount_open", "2026-09-11T02:00:00Z", "\n  \n"),
        group("db_mount_open", "2026-09-11T03:00:00Z", "ALTER DATABASE OPEN")))
    line, = gather(wiki).changes
    assert line.count == 2 and line.headlines == ("ALTER DATABASE OPEN",)


def test_a_folded_line_prefers_the_recorded_headline(wiki):
    digest(wiki, "2026-09-11", groups=(
        dict(group("db_mount_open", "2026-09-11T02:00:00Z",
                   "Stopping background process MMON\n"
                   "alter pluggable database all close immediate"),
             headline="alter pluggable database all close immediate"),))
    assert gather(wiki).changes[0].headlines == (
        "alter pluggable database all close immediate",)


def test_changes_are_capped(wiki):
    for d in range(1, 13):
        digest(wiki, f"2026-09-{d:02d}", groups=(
            group("parameter_change", f"2026-09-{d:02d}T01:00:00Z", "a"),
            group("db_mount_open", f"2026-09-{d:02d}T02:00:00Z", "b")))
    assert len(gather(wiki).changes) == dh.MAX_CHANGES


# ---- incidents ----------------------------------------------------------------

def test_only_resolved_incidents_are_carried_newest_first(wiki):
    incident(wiki, "2026-09-01-cdb1-old-done", opened="2026-09-01T00:00:00Z",
             status="resolved", title="old and done")
    incident(wiki, "2026-09-10-cdb1-newer-done", opened="2026-09-10T00:00:00Z",
             status="resolved", title="newer and done")
    incident(wiki, "2026-09-11-cdb1-open", opened="2026-09-11T00:00:00Z",
             title="newest but still open")
    assert [i.title for i in gather(wiki).incidents] == [
        "newer and done", "old and done"]


def test_a_monitoring_incident_is_still_active_and_dropped(wiki):
    incident(wiki, "2026-09-01-cdb1-watch", opened="2026-09-01T00:00:00Z",
             status="monitoring", title="being watched")
    assert gather(wiki).incidents == ()


def test_an_incident_opened_before_the_window_but_updated_inside_it_is_kept(wiki):
    incident(wiki, "2026-01-02-cdb1-ancient", opened="2026-01-02T00:00:00Z",
             updated="2026-09-11T00:00:00Z", status="resolved",
             title="ancient but touched")
    assert [i.title for i in gather(wiki).incidents] == ["ancient but touched"]


def test_an_incident_untouched_since_before_the_window_is_dropped(wiki):
    incident(wiki, "2026-01-02-cdb1-ancient", opened="2026-01-02T00:00:00Z",
             updated="2026-02-01T00:00:00Z", status="resolved")
    assert gather(wiki).incidents == ()


def test_another_databases_incident_is_dropped(wiki):
    incident(wiki, "2026-09-01-cdb2-x", db="cdb2", status="resolved",
             opened="2026-09-01T00:00:00Z")
    assert gather(wiki).incidents == ()


def test_resolved_is_the_newest_resolve_record(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             status="resolved",
             *(action("2026-09-03T10:00:00Z", kind="resolve",
                      status_after="resolved"),
               action("2026-09-05T10:00:00Z", kind="reopen"),
               action("2026-09-08T10:00:00Z", kind="resolve",
                      status_after="resolved")))
    summary, = gather(wiki).incidents
    assert summary.resolved == "2026-09-08"
    assert summary.status == "resolved"


def test_a_page_resolved_without_a_resolve_record_has_no_resolved_day(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             status="resolved")
    summary, = gather(wiki).incidents
    assert summary.resolved is None and summary.status == "resolved"


def test_incidents_are_capped(wiki):
    for d in range(1, 12):
        incident(wiki, f"2026-09-{d:02d}-cdb1-x", opened=f"2026-09-{d:02d}T00:00:00Z",
                 status="resolved", title=f"incident {d}")
    assert len(gather(wiki).incidents) == dh.MAX_INCIDENTS


# ---- operator actions ---------------------------------------------------------

def test_actions_are_newest_first_and_windowed(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             *(action("2026-06-14T10:00:00Z", summary="before the window"),
               action("2026-09-02T10:00:00Z", summary="early"),
               action("2026-09-09T10:00:00Z", summary="late")))
    assert [a.summary for a in gather(wiki).actions] == ["late", "early"]


def test_bookkeeping_kinds_are_not_operator_actions(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             *(action("2026-09-02T10:00:00Z", kind="start-monitoring",
                      summary="watching"),
               action("2026-09-03T10:00:00Z", kind="record-action",
                      summary="fixed it")))
    assert [a.kind for a in gather(wiki).actions] == ["record-action"]


def test_merge_bookkeeping_is_not_an_operator_action(wiki):
    merged = render_action(ActionRecord(
        at="2026-09-04T10:00:00Z", kind="record-action", actor="dbwiki",
        intent="keep one page for this db and day",
        summary="merged incidents/2026-09-01-cdb1-y into this page",
        status_after=Status("open"), outcome=Outcome("succeeded")))
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             *(merged, action("2026-09-03T10:00:00Z", summary="fixed it")))
    assert [a.summary for a in gather(wiki).actions] == ["fixed it"]


def test_an_action_in_the_window_is_kept_though_its_incident_is_not(wiki):
    incident(wiki, "2026-01-02-cdb1-ancient", opened="2026-01-02T00:00:00Z",
             updated="2026-02-01T00:00:00Z",
             *(action("2026-09-09T10:00:00Z", summary="still working it"),))
    history = gather(wiki)
    assert history.incidents == ()
    assert [a.summary for a in history.actions] == ["still working it"]


def test_an_action_summary_is_one_capped_line(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             *(action("2026-09-09T10:00:00Z", summary="a " * 400),))
    summary = gather(wiki).actions[0].summary
    assert len(summary) == MAX_LINE and "\n" not in summary


def test_actions_are_capped(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             *(action(f"2026-09-{d:02d}T10:00:00Z", summary=f"act {d}")
               for d in range(1, 13)))
    assert len(gather(wiki).actions) == dh.MAX_ACTIONS


# ---- past fixes ---------------------------------------------------------------

def with_fixes(wiki) -> None:
    (wiki / "errors" / f"{CODE}.md").write_text(ERROR_PAGE)
    (wiki / "errors" / "ORA-00600.md").write_text(
        ERROR_PAGE.replace(CODE, "ORA-00600"))
    incident(wiki, "2026-03-02-cdb1-tablespace", opened="2026-03-02T00:00:00Z",
             status="resolved", codes=(CODE,),
             *(action("2026-03-03T10:00:00Z", kind="resolve",
                      summary="added a datafile", status_after="resolved"),))
    incident(wiki, "2026-03-02-cdb2-tablespace", db="cdb2",
             opened="2026-03-02T00:00:00Z", status="resolved",
             codes=(CODE,),
             *(action("2026-03-04T10:00:00Z", kind="resolve",
                      summary="cdb2 got a datafile too",
                      status_after="resolved"),))
    incident(wiki, "2026-03-05-cdb1-internal", opened="2026-03-05T00:00:00Z",
             status="resolved", codes=("ORA-00600",),
             *(action("2026-03-06T10:00:00Z", kind="resolve",
                      summary="raised an SR", status_after="resolved"),))


def test_fixes_are_limited_to_todays_codes_and_this_database(wiki):
    with_fixes(wiki)
    fixes = gather(wiki, codes=(CODE,)).fixes
    assert [(f.code, f.db) for f in fixes] == [(CODE, DB)]


def test_a_second_code_brings_its_own_fixes(wiki):
    with_fixes(wiki)
    fixes = gather(wiki, codes=(CODE, "ORA-00600")).fixes
    assert [f.code for f in fixes] == ["ORA-00600", CODE]


def test_no_codes_means_no_fixes(wiki):
    with_fixes(wiki)
    assert gather(wiki, codes=()).fixes == ()


def test_a_fix_older_than_the_window_is_still_carried(wiki):
    with_fixes(wiki)
    assert [f.day for f in gather(wiki, codes=(CODE,)).fixes] == ["2026-03-03"]


def test_fixes_are_capped(wiki):
    (wiki / "errors" / f"{CODE}.md").write_text(ERROR_PAGE)
    incident(wiki, "2026-03-02-cdb1-tablespace", opened="2026-03-02T00:00:00Z",
             codes=(CODE,),
             *(action(f"2026-03-{d:02d}T10:00:00Z", summary=f"try {d}")
               for d in range(2, 12)))
    assert len(gather(wiki, codes=(CODE,)).fixes) == dh.MAX_FIXES


# ---- render -------------------------------------------------------------------

@pytest.fixture
def full(wiki):
    journal(wiki, "2026-09", ("2026-09-12", "cdb1 2026-09-12: ORA-1653 storm"))
    digest(wiki, "2026-09-12", notable=True, groups=(
        group("parameter_change", "2026-09-12T08:03:11Z",
              "ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=BOTH;",
              count=2),))
    (wiki / "errors" / f"{CODE}.md").write_text(ERROR_PAGE)
    incident(wiki, "2026-09-01-cdb1-tablespace", opened="2026-09-01T00:00:00Z",
             status="resolved", title="Tablespace full", codes=(CODE,),
             *(action("2026-09-04T09:00:00Z", kind="resolve",
                      summary="added a datafile", status_after="resolved"),))
    return wiki


def test_the_block_names_every_list_it_carries(full):
    block = dh.render(gather(full, codes=(CODE,)))
    assert block.splitlines()[0] == (
        "Database history for cdb1 (last 90 days; context from earlier days, "
        "not evidence for today):")
    assert "Changes (from digests):" in block
    assert ("- 2026-09-12 parameter_change ×2 — ALTER SYSTEM SET "
            "log_archive_dest_state_2='ENABLE' SCOPE=BOTH;") in block
    assert "Journal (days with events):" in block
    assert "- 2026-09-12 — cdb1 2026-09-12: ORA-1653 storm" in block
    assert "Resolved incidents:" in block
    assert ("- incidents/2026-09-01-cdb1-tablespace.md — Tablespace full "
            "(resolved 2026-09-04)") in block
    assert "Operator actions:" in block
    assert ("- 2026-09-04 resolve (succeeded) on 2026-09-01-cdb1-tablespace: "
            "added a datafile") in block
    assert "Past fixes for today's codes:" in block
    assert f"- {CODE} · 2026-09-04 · added a datafile · succeeded · " in block


def test_an_incident_line_falls_back_to_its_status(wiki):
    incident(wiki, "2026-09-01-cdb1-x", opened="2026-09-01T00:00:00Z",
             status="resolved", title="Fixed, undated")
    assert ("- incidents/2026-09-01-cdb1-x.md — Fixed, undated (resolved)"
            in dh.render(gather(wiki)))


def test_a_label_with_nothing_under_it_is_left_out(wiki):
    journal(wiki, "2026-09", ("2026-09-12", "cdb1 2026-09-12: a bad night"))
    block = dh.render(gather(wiki))
    assert "Journal (days with events):" in block
    for label in ("Changes", "Resolved incidents", "Operator actions",
                  "Past fixes"):
        assert label not in block


def test_the_block_is_capped(wiki):
    journal(wiki, "2026-09", *((f"2026-09-{d:02d}", "x" * 500)
                               for d in range(1, 13)))
    for d in range(1, 13):
        digest(wiki, f"2026-09-{d:02d}", groups=(
            group("parameter_change", f"2026-09-{d:02d}T01:00:00Z", "y" * 500),
            group("db_mount_open", f"2026-09-{d:02d}T02:00:00Z", "z" * 500)))
    block = dh.render(gather(wiki))
    assert max(len(ln) for ln in block.splitlines()) <= MAX_LINE
    assert block.endswith("[truncated]")
    assert len(block) <= dh.MAX_HISTORY_CHARS + len("\n[truncated]")


def test_the_valuable_lists_come_before_the_ones_the_cap_may_eat(full):
    """`_cap` truncates the tail, so the lists that answer "has this been
    fixed before" lead and the journal, the most restateable of the five,
    goes last."""
    incident(full, "2026-09-02-cdb1-redo", opened="2026-09-02T00:00:00Z",
             status="resolved", title="Redo stall", codes=(CODE,))
    block = dh.render(gather(full, codes=(CODE,)))
    labels = [ln[:-1] for ln in block.splitlines() if ln.endswith(":")
              and not ln.startswith("- ")]
    assert labels[1:] == ["Past fixes for today's codes", "Operator actions",
                          "Resolved incidents", "Changes (from digests)",
                          "Journal (days with events)"]


def test_the_caps_are_the_ones_the_order_was_chosen_for():
    assert (dh.MAX_HISTORY_CHARS, dh.MAX_FIXES, dh.MAX_ACTIONS,
            dh.MAX_INCIDENTS, dh.MAX_CHANGES, dh.MAX_JOURNAL) == \
        (5000, 6, 8, 5, 12, 7)


def test_a_folded_change_line_joins_its_headlines_with_semicolons(wiki):
    digest(wiki, "2026-08-31", groups=(
        group("db_mount_open", "2026-08-31T08:43:46Z",
              "alter pluggable database all close immediate"),
        group("db_mount_open", "2026-08-31T08:43:58Z",
              "ALTER DATABASE CLOSE NORMAL"),
        group("db_mount_open", "2026-08-31T19:14:33Z",
              "Database mounted in Exclusive Mode")))
    assert ("- 2026-08-31 db_mount_open ×3 — alter pluggable database all "
            "close immediate; ALTER DATABASE CLOSE NORMAL; Database mounted "
            "in Exclusive Mode") in dh.render(gather(wiki))


def test_a_headline_ending_in_a_semicolon_does_not_stutter(wiki):
    """Oracle terminates its own statements, and `SCOPE=MEMORY;; ALTER` reads
    as a corrupted line. The separator collapses onto the semicolon the line
    already carries, and no character of either statement is lost."""
    first = "ALTER SYSTEM SET log_archive_dest_state_2='DEFER' SCOPE=MEMORY;"
    second = "ALTER SYSTEM SET log_archive_dest_state_2='ENABLE' SCOPE=MEMORY;"
    digest(wiki, "2026-09-09", groups=(
        group("parameter_change", "2026-09-09T08:00:00Z", first),
        group("parameter_change", "2026-09-09T09:00:00Z", second)))
    rendered = dh.render(gather(wiki))
    assert ";;" not in rendered
    assert f"×2 — {first} {second}" in rendered


def test_a_change_line_with_no_headline_left_drops_the_dash(wiki):
    digest(wiki, "2026-08-31", groups=(
        group("db_mount_open", "2026-08-31T08:43:46Z", "   "),))
    assert "- 2026-08-31 db_mount_open ×1\n" in dh.render(gather(wiki)) + "\n"


def test_the_same_tree_renders_the_same_bytes_twice(full):
    assert dh.render(gather(full, codes=(CODE,))) == \
        dh.render(gather(full, codes=(CODE,)))
