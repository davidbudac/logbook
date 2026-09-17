"""Past fixes: which action records count as a fix, how one is judged, the
table that is rendered from them, the splice that owns the section, and the
per-page transaction the stage publishes. No model and no network anywhere in
this file, because the stage has neither."""

import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import pytest
from fixtures.incident_pages import incident_page

from dbwiki import past_fixes as pf
from dbwiki.incidents import ActionRecord, Outcome, Status, render_action
from dbwiki.lint import blocking, lint_wiki
from dbwiki.lock import Held
from dbwiki.orchestrate import Orchestrator

TODAY = dt.date(2026, 9, 30)
CODE = "ORA-1653"
REL = f"errors/{CODE}.md"

ERROR_PAGE = (
    "---\ntype: error-class\nupdated: 2026-09-01T00:00:00Z\n---\n\n"
    f"# {CODE}\n\n## Occurrences\n\n"
    "| day | db | note | evidence |\n|---|---|---|---|\n"
    "| 2026-08-01 | cdb1 | seen | digests/cdb1/2026-08-01.md |\n\n"
    "## Resolution history\n\n"
    "| resolved | db | incident | remediation | evidence |\n|---|---|---|---|---|\n")


def action(at: str, *, kind: str = "record-action", summary: str = "did a thing",
           outcome: str = "pending", intent: str = "fix the tablespace",
           rollback: str = "", ticket: str = "",
           status_after: str = "open") -> str:
    return render_action(ActionRecord(
        at=at, kind=kind, actor="dba@example.com", intent=intent,
        summary=summary, status_after=Status(status_after), ticket=ticket,
        outcome=Outcome(outcome), rollback=rollback))


def record(**over) -> ActionRecord:
    base = {"at": "2026-08-20T09:00:00Z", "kind": "record-action",
            "actor": "dba@example.com", "intent": "fix it",
            "summary": "did a thing", "status_after": Status.OPEN}
    base.update(over)
    return ActionRecord(**base)


def occurrence(day: str, db: str = "cdb1"):
    from dbwiki.readmodel import Occurrence
    return Occurrence(CODE, day, db, "seen", f"digests/{db}/{day}.md")


@pytest.fixture
def wiki(tmp_path):
    w = tmp_path / "wiki"
    (w / "errors").mkdir(parents=True)
    (w / "incidents").mkdir()
    (w / "errors" / f"{CODE}.md").write_text(ERROR_PAGE)
    return w


def incident(wiki, slug: str, *actions: str, db: str = "cdb1",
             status: str = "open", codes: tuple[str, ...] = (CODE,)) -> None:
    text = incident_page(db, slug, status=status, error_codes=codes)
    (wiki / "incidents" / f"{slug}.md").write_text(
        text + "".join(f"\n{a}" for a in actions))


# ---- which records are fixes --------------------------------------------------

def test_merge_and_extend_monitoring_are_not_fixes():
    assert not pf.is_fix(record(kind="merge", summary="duplicate of x"))
    assert not pf.is_fix(record(kind="extend-monitoring",
                                summary="window extended to 2026-09-01"))


def test_merge_bookkeeping_is_not_a_fix_though_it_is_a_record_action():
    """The kept page of a merge carries an ordinary `record-action` whose
    intent is filing, not fixing. It is the one record-action to drop."""
    keeper = record(intent=pf.MERGE_INTENT, summary="merged x into this page")
    assert not pf.is_fix(keeper)
    assert pf.is_fix(record(intent="restore the standby"))


@pytest.mark.parametrize("kind", sorted(pf.FIX_KINDS))
def test_every_fix_kind_counts(kind):
    assert pf.is_fix(record(kind=kind))


def test_gather_drops_merge_and_bookkeeping_rows(wiki):
    incident(wiki, "2026-08-20-cdb1-full",
             action("2026-08-20T09:00:00Z", summary="added a datafile"),
             action("2026-08-20T10:00:00Z", kind="merge",
                    summary="duplicate of 2026-08-19-cdb1-full"),
             action("2026-08-20T11:00:00Z", intent=pf.MERGE_INTENT,
                    summary="merged 2026-08-19-cdb1-full into this page"))
    history = pf.gather(wiki, TODAY)[CODE]
    assert [f.action for f in history.fixes] == ["added a datafile"]


def test_a_page_under_incidents_that_is_not_an_incident_contributes_nothing(wiki):
    """Every row links `[[incidents/<slug>]]`, and `lint`'s wikilink-broken
    rule blocks a commit that names a page it cannot open. Rows only ever come
    from pages `load_incidents` accepted, which is what keeps the links live."""
    (wiki / "incidents" / "README.md").write_text(
        f"---\ntype: note\n---\n\n# notes\n\n[[errors/{CODE}]]\n")
    assert pf.gather(wiki, TODAY) == {}


def test_a_code_with_no_incident_has_no_history(wiki):
    incident(wiki, "2026-08-20-cdb1-other",
             action("2026-08-20T09:00:00Z"), codes=("ORA-600",))
    assert CODE not in pf.gather(wiki, TODAY)


def test_codes_narrows_the_walk_to_the_pages_named(wiki):
    (wiki / "errors" / "ORA-600.md").write_text(ERROR_PAGE.replace(CODE, "ORA-600"))
    incident(wiki, "2026-08-20-cdb1-full", action("2026-08-20T09:00:00Z"))
    incident(wiki, "2026-08-21-cdb1-internal", action("2026-08-21T09:00:00Z"),
             codes=("ORA-600",))
    assert set(pf.gather(wiki, TODAY)) == {CODE, "ORA-600"}
    assert set(pf.gather(wiki, TODAY, codes=[CODE])) == {CODE}
    assert pf.gather(wiki, TODAY, codes=["ORA-99999"]) == {}


def test_a_narrowed_code_keeps_the_history_the_full_walk_gives_it(wiki):
    incident(wiki, "2026-08-20-cdb1-full",
             action("2026-08-20T09:00:00Z", summary="added a datafile"))
    assert pf.gather(wiki, TODAY, codes=[CODE]) == {
        CODE: pf.gather(wiki, TODAY)[CODE]}


# ---- the verdict --------------------------------------------------------------

def verdict(rec, later=(), occurrences=(), *, db="cdb1", days=14):
    return pf.verdict_for(rec, list(later), tuple(occurrences), db=db,
                          today=TODAY, held_after_days=days)


RESOLVE = record(at="2026-08-20T12:00:00Z", kind="resolve",
                 summary="rebuilt the index", outcome=Outcome.SUCCEEDED,
                 status_after=Status.RESOLVED)


def test_verdict_open_while_the_incident_is_unresolved():
    assert verdict(record()) == (pf.Verdict.OPEN, None)


def test_verdict_na_for_a_rejected_or_failed_outcome():
    assert verdict(record(outcome=Outcome.REJECTED)) == (pf.Verdict.NA, None)
    assert verdict(record(outcome=Outcome.FAILED)) == (pf.Verdict.NA, None)


def test_verdict_na_for_the_reopen_record_itself():
    reopen = record(kind="reopen", summary="it came back",
                    outcome=Outcome.FAILED)
    assert verdict(reopen) == (pf.Verdict.NA, None)


def test_verdict_reopened_carries_the_day_it_came_back():
    reopen = record(at="2026-08-25T08:00:00Z", kind="reopen",
                    summary="it came back", outcome=Outcome.FAILED)
    assert verdict(RESOLVE, [reopen]) == (pf.Verdict.REOPENED, "2026-08-25")


def test_a_reopen_beats_a_recurrence_and_an_unresolved_incident():
    reopen = record(at="2026-08-25T08:00:00Z", kind="reopen",
                    summary="it came back", outcome=Outcome.FAILED)
    assert verdict(record(), [reopen], [occurrence("2026-08-22")]) == \
        (pf.Verdict.REOPENED, "2026-08-25")


def test_verdict_recurred_on_the_earliest_later_day_on_the_same_db():
    got = verdict(RESOLVE, [], [occurrence("2026-08-20"),
                                occurrence("2026-08-29"),
                                occurrence("2026-08-24")])
    assert got == (pf.Verdict.RECURRED, "2026-08-24")


def test_a_recurrence_on_another_database_is_not_this_fix_coming_back():
    assert verdict(RESOLVE, [], [occurrence("2026-08-24", db="cdb2")]) == \
        (pf.Verdict.HELD, None)


def test_verdict_held_once_the_quiet_span_has_elapsed():
    assert verdict(RESOLVE) == (pf.Verdict.HELD, None)


def test_verdict_too_soon_inside_the_quiet_span():
    assert verdict(RESOLVE, days=60) == (pf.Verdict.TOO_SOON, None)


def test_a_record_action_is_judged_by_the_resolve_that_follows_it():
    fix = record(at="2026-08-18T09:00:00Z", summary="added a datafile")
    assert verdict(fix, [RESOLVE]) == (pf.Verdict.HELD, None)


# ---- the cell -----------------------------------------------------------------

def test_the_cell_strips_urls_and_escapes_pipes(wiki):
    incident(wiki, "2026-09-01-cdb1-full", action(
        "2026-09-01T09:00:00Z",
        summary="followed https://support.oracle.com/note/1.html and ran "
                "alter database | datafile resize",
        rollback="shrink it back (see http://wiki.internal/x)"))
    fix = pf.gather(wiki, TODAY)[CODE].fixes[0]
    assert fix.action == (
        r"followed and ran alter database \| datafile resize "
        r"(rollback: shrink it back (see ))")
    assert "http" not in fix.action


def test_the_cell_carries_the_rollback_and_the_ticket(wiki):
    incident(wiki, "2026-09-01-cdb1-full",
             action("2026-09-01T09:00:00Z", summary="autoextend on",
                    rollback="autoextend off", ticket="OPS-412"))
    fix = pf.gather(wiki, TODAY)[CODE].fixes[0]
    assert fix.action == "autoextend on (rollback: autoextend off; ticket: OPS-412)"


# ---- render -------------------------------------------------------------------

def test_render_is_the_summary_then_the_table_newest_first(wiki):
    incident(wiki, "2026-07-12-cdb1-transport",
             action("2026-07-14T10:00:00Z", kind="resolve",
                    summary="re-enabled log_archive_dest_state_2",
                    outcome="succeeded", status_after="resolved"),
             action("2026-07-20T11:00:00Z", kind="reopen",
                    summary="transport failed again", outcome="failed"),
             status="open")
    incident(wiki, "2026-09-09-cdb1-allocate",
             action("2026-09-09T08:00:00Z", kind="resolve",
                    summary="autoextend on for CHAOS_TS",
                    rollback="autoextend off", outcome="succeeded",
                    status_after="resolved"),
             status="resolved")
    history = pf.gather(wiki, TODAY)[CODE]
    assert pf.render(history) == (
        "3 fixes across 2 incidents on 1 database: 1 reopened, 1 held, 1 n/a.\n"
        "\n"
        "| when | db | incident | what was done | outcome | held |\n"
        "|---|---|---|---|---|---|\n"
        "| 2026-09-09 | cdb1 | [[incidents/2026-09-09-cdb1-allocate]] "
        "| autoextend on for CHAOS_TS (rollback: autoextend off) "
        "| succeeded | held (14d) |\n"
        "| 2026-07-20 | cdb1 | [[incidents/2026-07-12-cdb1-transport]] "
        "| transport failed again | failed | n/a |\n"
        "| 2026-07-14 | cdb1 | [[incidents/2026-07-12-cdb1-transport]] "
        "| re-enabled log_archive_dest_state_2 | succeeded "
        "| reopened 2026-07-20 |")


def test_the_summary_counts_sum_to_the_row_count(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    incident(wiki, "2026-09-11-cdb2-b", action("2026-09-11T08:00:00Z"),
             db="cdb2")
    history = pf.gather(wiki, TODAY)[CODE]
    assert history.summary_line() == \
        "2 fixes across 2 incidents on 2 databases: 2 open"


def test_the_commonest_verdict_leads_the_summary():
    """Count first, then the order the states are declared in, so a run that
    changes nothing renders the same line."""
    def fixes(verdict, n):
        return [pf.PastFix(CODE, f"i-{verdict}-{i}", "cdb1", "2026-09-01",
                           "resolve", "did it", Outcome.SUCCEEDED, verdict)
                for i in range(n)]

    history = pf.FixHistory(CODE, tuple(fixes(pf.Verdict.HELD, 3)
                                        + fixes(pf.Verdict.REOPENED, 1)
                                        + fixes(pf.Verdict.RECURRED, 1)))
    assert history.summary_line() == ("5 fixes across 5 incidents on "
                                      "1 database: 3 held, 1 reopened, "
                                      "1 recurred")


def test_one_fix_reads_in_the_singular(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    assert pf.gather(wiki, TODAY)[CODE].summary_line() == \
        "1 fix across 1 incident on 1 database: 1 open"


# ---- splice_section -----------------------------------------------------------

BODY = "1 fix across 1 incident on 1 database: 1 open.\n\n| a |"
TAIL = "## Notes\n\n- a human's own list\n"


def test_the_section_lands_after_resolution_history():
    text = pf.splice_section(ERROR_PAGE + "\n" + TAIL, BODY)
    assert text.index("## Resolution history") < text.index(pf.PAST_FIXES_HEAD)
    assert text.index(pf.PAST_FIXES_HEAD) < text.index("## Notes")
    assert text.endswith(TAIL)


def test_the_section_is_appended_when_there_is_no_resolution_history():
    page = ERROR_PAGE.split("## Resolution history")[0]
    text = pf.splice_section(page, BODY)
    assert text.endswith(f"{pf.PAST_FIXES_HEAD}\n\n{BODY}\n")


def test_splicing_twice_writes_identical_bytes():
    once = pf.splice_section(ERROR_PAGE + "\n" + TAIL, BODY)
    assert pf.splice_section(once, BODY) == once
    assert once.count(pf.PAST_FIXES_HEAD) == 1


def test_a_new_body_replaces_the_old_one_in_place():
    once = pf.splice_section(ERROR_PAGE + "\n" + TAIL, BODY)
    twice = pf.splice_section(once, "2 fixes.\n\n| b |")
    assert "1 fix across" not in twice
    assert twice.count(pf.PAST_FIXES_HEAD) == 1
    assert twice.endswith(TAIL)


def test_an_empty_history_removes_the_section_and_restores_the_page():
    page = ERROR_PAGE + "\n" + TAIL
    once = pf.splice_section(page, BODY)
    assert pf.splice_section(once, None) == page
    assert pf.splice_section(page, None) == page


def test_the_sections_before_and_after_keep_their_own_line_spacing():
    page = ERROR_PAGE + "\n\n\n" + TAIL
    text = pf.splice_section(page, BODY)
    assert text.endswith(TAIL)
    assert "| 2026-08-01 | cdb1 | seen | digests/cdb1/2026-08-01.md |" in text


# ---- apply --------------------------------------------------------------------

def test_apply_writes_the_section_and_one_log_line(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    history = pf.gather(wiki, TODAY)
    assert pf.apply(wiki, REL, history[CODE], TODAY) is True
    assert pf.PAST_FIXES_HEAD in (wiki / REL).read_text()
    assert (f"[2026-09-30] research (history) — {REL}: 1 fix(es)"
            in (wiki / "log.md").read_text())


def test_apply_is_a_no_op_the_second_time(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    history = pf.gather(wiki, TODAY)
    pf.apply(wiki, REL, history[CODE], TODAY)
    before = (wiki / REL).read_text()
    log_before = (wiki / "log.md").read_text()
    assert pf.apply(wiki, REL, history[CODE], dt.date(2026, 10, 1)) is False
    assert (wiki / REL).read_text() == before
    assert (wiki / "log.md").read_text() == log_before


def test_apply_never_writes_an_empty_section(wiki):
    assert pf.apply(wiki, REL, None, TODAY) is False
    assert not (wiki / "log.md").exists()


def test_apply_removes_the_section_once_the_history_empties(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    pf.apply(wiki, REL, pf.gather(wiki, TODAY)[CODE], TODAY)
    (wiki / "incidents" / "2026-09-10-cdb1-a.md").unlink()
    assert pf.apply(wiki, REL, pf.gather(wiki, TODAY).get(CODE), TODAY) is True
    assert (wiki / REL).read_text() == ERROR_PAGE


def test_apply_leaves_the_frontmatter_alone(wiki):
    incident(wiki, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    pf.apply(wiki, REL, pf.gather(wiki, TODAY)[CODE], TODAY)
    assert (wiki / REL).read_text().startswith(
        "---\ntype: error-class\nupdated: 2026-09-01T00:00:00Z\n---\n")


def test_the_written_page_passes_the_deterministic_lint(wiki):
    (wiki / "digests" / "cdb1").mkdir(parents=True)
    (wiki / "digests" / "cdb1" / "2026-08-01.md").write_text("# digest\n")
    incident(wiki, "2026-09-10-cdb1-a",
             action("2026-09-10T08:00:00Z",
                    summary="see https://support.oracle.com/x"))
    pf.apply(wiki, REL, pf.gather(wiki, TODAY)[CODE], TODAY)
    assert blocking(lint_wiki(wiki, only_paths=[REL])) == []


# ---- the stage ----------------------------------------------------------------

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def porcelain(repo) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


def subjects(repo) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return out.splitlines()


@pytest.fixture
def stage(tmp_path):
    repo = tmp_path / "wiki"
    (repo / "errors").mkdir(parents=True)
    (repo / "incidents").mkdir()
    (repo / "digests" / "cdb1").mkdir(parents=True)
    (repo / "digests" / "cdb1" / "2026-08-01.md").write_text("# digest\n")
    (repo / "log.md").write_text("# log\n")
    (repo / "errors" / f"{CODE}.md").write_text(ERROR_PAGE)
    (repo / "errors" / "ORA-600.md").write_text(
        ERROR_PAGE.replace(CODE, "ORA-600"))
    git(repo, "init")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    cfg = SimpleNamespace(wiki_repo=repo, state_dir=tmp_path / "state",
                          agents={}, report={}, research={})
    return repo, Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def test_the_stage_commits_one_page_at_a_time(stage):
    repo, orch = stage
    incident(repo, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    incident(repo, "2026-09-10-cdb1-b", action("2026-09-10T09:00:00Z"),
             codes=("ORA-600",))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    result = orch.past_fixes()
    assert result["pages_touched"] == ["errors/ORA-1653.md", "errors/ORA-600.md",
                                       "log.md"]
    assert "past fixes: 2 page(s), 2 fix(es)" in result["summary"]
    assert subjects(repo)[:2] == [
        "research — past fixes: errors/ORA-600.md",
        "research — past fixes: errors/ORA-1653.md"]
    assert porcelain(repo) == ""
    assert orch.last_telemetry["attempts"] == 0
    assert orch.last_telemetry["usage"] == "unknown"


def test_a_second_run_changes_nothing_and_commits_nothing(stage):
    repo, orch = stage
    incident(repo, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    orch.past_fixes()
    before = len(subjects(repo))
    result = orch.past_fixes()
    assert result["pages_touched"] == []
    assert len(subjects(repo)) == before
    assert porcelain(repo) == ""


def test_a_lint_blocked_page_is_rolled_back_and_flagged(stage):
    """The link a row carries must resolve. A page whose own body already
    carries a dead one costs itself and nothing behind it."""
    repo, orch = stage
    (repo / "errors" / "ORA-600.md").write_text(
        ERROR_PAGE.replace(CODE, "ORA-600")
        + "\nSee [[incidents/2026-01-01-cdb1-gone]] for context.\n")
    incident(repo, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    incident(repo, "2026-09-10-cdb1-b", action("2026-09-10T09:00:00Z"),
             codes=("ORA-600",))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    before = (repo / "errors" / "ORA-600.md").read_text()
    result = orch.past_fixes()
    assert result["pages_touched"] == ["errors/ORA-1653.md", "log.md"]
    assert any("lint blocked ORA-600: wikilink-broken" in f
               for f in result["flags"])
    assert "1 failed" in result["summary"]
    assert (repo / "errors" / "ORA-600.md").read_text() == before
    assert porcelain(repo) == ""


def test_an_interrupt_restores_the_page_in_flight(stage, monkeypatch):
    repo, orch = stage
    incident(repo, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    incident(repo, "2026-09-10-cdb1-b", action("2026-09-10T09:00:00Z"),
             codes=("ORA-600",))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    real = pf.apply
    applied = []

    def _apply(wiki_root, rel_page, history, today):
        changed = real(wiki_root, rel_page, history, today)
        applied.append(rel_page)
        if len(applied) == 2:
            raise KeyboardInterrupt
        return changed

    monkeypatch.setattr(pf, "apply", _apply)
    with pytest.raises(KeyboardInterrupt):
        orch.past_fixes()
    assert subjects(repo)[0] == "research — past fixes: errors/ORA-1653.md"
    assert porcelain(repo) == ""
    assert orch.last_telemetry["rolled_back"] is False


def test_the_stage_refuses_to_run_over_a_stray_edit(stage):
    repo, orch = stage
    (repo / "errors" / "ORA-600.md").write_text("---\ntype: error-class\n---\n")
    with pytest.raises(RuntimeError, match="uncommitted"):
        orch.past_fixes()


# ---- the CLI verb -------------------------------------------------------------

def cli_args(**over):
    base = {"dry_run": False}
    base.update(over)
    return SimpleNamespace(**base)


def test_cli_refuses_while_the_config_key_is_off(wiki, capsys):
    from dbwiki.cli import _run_history
    cfg = SimpleNamespace(research={"history": {"enabled": False}},
                          wiki_repo=wiki, state_dir=wiki / ".state")
    assert _run_history(cli_args(), cfg, None) == 1
    assert "research.history.enabled" in capsys.readouterr().err


def test_cli_runs_by_default_when_the_config_block_is_absent(stage, capsys):
    from dbwiki.cli import _run_history
    repo, orch = stage
    incident(repo, "2026-09-10-cdb1-a", action("2026-09-10T08:00:00Z"))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    cfg = SimpleNamespace(research={}, wiki_repo=repo,
                          state_dir=repo.parent / "state")
    assert _run_history(cli_args(), cfg, orch) == 0
    assert json.loads(capsys.readouterr().out)["pages_touched"] == [REL, "log.md"]


def test_cli_dry_run_prints_the_rows_and_writes_nothing(stage, capsys):
    from dbwiki.cli import _run_history
    repo, orch = stage
    incident(repo, "2026-09-10-cdb1-a",
             action("2026-09-10T08:00:00Z", summary="added a datafile"))
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    cfg = SimpleNamespace(research={}, wiki_repo=repo,
                          state_dir=repo.parent / "state")
    assert _run_history(cli_args(dry_run=True), cfg, orch) == 0
    out = capsys.readouterr().out
    assert f"{REL}:" in out
    assert "1 fix across 1 incident on 1 database: 1 open." in out
    assert "| added a datafile |" in out
    assert porcelain(repo) == ""
    assert pf.PAST_FIXES_HEAD not in (repo / REL).read_text()


def test_the_configured_quiet_span_reaches_the_page(stage):
    repo, orch = stage
    orch.cfg.research = {"history": {"held_after_days": 3}}
    incident(repo, "2026-09-01-cdb1-a",
             action("2026-09-01T08:00:00Z", kind="resolve",
                    summary="rebuilt it", outcome="succeeded",
                    status_after="resolved"),
             status="resolved")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed incidents")
    orch.past_fixes()
    assert "| held (3d) |" in (repo / REL).read_text()
