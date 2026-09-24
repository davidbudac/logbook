"""`dbwiki incident`, driven end to end against a real git wiki.

What is under test is the whole path from an argv list to a commit: the
option grammar, the preview and its copy-pastable retry line, the
single-flight lock, the run-health facts, and the five exit codes a refused
transaction maps to. Nothing here fakes git or lint, because a stub would pin
the CLI's opinion of the outcome union rather than the outcome the wiki
actually produces.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys

import pytest

from dbwiki import (cli, gitutil, incident_action, incident_cli, lifecycle,
                    lint, transaction)
from dbwiki.health import HEALTH_LOG
from dbwiki.incidents import (ErrorAbsent, EventPresent, MonitoringWindow,
                              Outcome, Status, parse_actions, read_incident)
from dbwiki.lock import single_flight
from dbwiki.pagetext import frontmatter, sections
from fixtures.incident_pages import incident_page

SLUG = "2026-08-05-cdb1_stby-tns-12564"
PATH = f"incidents/{SLUG}.md"
TARGET = PATH[:-3]
DB = "cdb1_stby"
CODE = "TNS-12564"
TITLE = "TNS-12564 connect failures on cdb1_stby"
LABEL = "fatal NI connect errors on the standby"
BULLET = f"- [[{TARGET}]] {LABEL}"
DIGEST = f"digests/{DB}/2026-08-30.md"
EMAIL = "dba@example.com"

AT = "2026-08-30T14:22:10Z"
LATER = "2026-08-30T18:40:00Z"
UNTIL = "2026-09-02T00:00:00Z"
FURTHER = "2026-09-05T00:00:00Z"

INTENT = "stop the standby losing its connection every night"
SUMMARY = "restarted the standby listener"
FIXED = "rebuilt the tnsnames entry and restarted the listener"

CONFIG = """\
elasticsearch: {url: "http://localhost:9200"}
sources: {}
wiki_repo: wiki
state_dir: .state
digest_dir: digests
report: {push: false}
"""

INDEX = f"""---
type: index
---

# Logbook

## Open incidents

{BULLET}
"""

LOG = "---\ntype: log\n---\n\n# Log\n"
ERROR_PAGE = f"---\ntype: error-class\n---\n\n# {CODE}\n"
DATABASE_PAGE = "---\ntype: database\n---\n\n# cdb1\n"

#: The second page the ingest opened for the same db and the same day, and the
#: one line of evidence only it carries: what `merge` exists to fold away.
KEPT_SLUG = f"{SLUG}-again"
KEPT_PATH = f"incidents/{KEPT_SLUG}.md"
KEPT_TARGET = KEPT_PATH[:-3]
KEPT_TITLE = "TNS-12564 connect failures on cdb1_stby, opened twice"
KEPT_BULLET = f"- [[{KEPT_TARGET}]] the page that keeps it"
DAY = AT[:10]
KEPT_DAY = "2026-08-29"
KEPT_DIGEST = f"digests/{DB}/{KEPT_DAY}.md"

DUPLICATE_BODY = f"""## Evidence

- {DAY}: {DIGEST} — the day only the duplicate cites

## Update {DAY}

the duplicate's account of the day it was opened for"""

KEPT_BODY = f"""## Evidence

- {KEPT_DAY}: {KEPT_DIGEST} — the kept page's own day"""

RECORD = ["incident", "record-action", SLUG, "--intent", INTENT,
          "--summary", SUMMARY, "--at", AT]
MONITOR = ["incident", "monitor", SLUG, "--signal", f"error_absent:{CODE}",
           "--until", UNTIL, "--intent", INTENT, "--summary", SUMMARY,
           "--at", AT]
RESOLVE = ["incident", "resolve", SLUG, "--summary", FIXED, "--at", LATER]
MERGE = ["incident", "merge", SLUG, "--into", KEPT_SLUG, "--at", LATER]

PUBLISH_WITH = "preview only. publish with: "


def git(repo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A project root whose wiki holds one open incident, its error page, an
    index listing it and a digest it may cite.

    The git environment is cut off from the developer's: one test turns the
    wiki's `user.email` off to assert the no-actor refusal, and a global
    config would silently supply one.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dbwiki.yaml").write_text(CONFIG)
    wiki = tmp_path / "wiki"
    (wiki / "incidents").mkdir(parents=True)
    (wiki / "errors").mkdir()
    (wiki / "digests" / DB).mkdir(parents=True)
    (wiki / PATH).write_text(incident_page(DB, TITLE, error_codes=(CODE,)))
    (wiki / f"errors/{CODE}.md").write_text(ERROR_PAGE)
    (wiki / DIGEST).write_text(f"# {DB} 2026-08-30\n")
    (wiki / "index.md").write_text(INDEX)
    (wiki / "log.md").write_text(LOG)
    git(wiki, "init", "-b", "main")
    git(wiki, "config", "user.email", EMAIL)
    git(wiki, "config", "user.name", "DBA")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def page(root) -> str:
    return (root / "wiki" / PATH).read_text()


def incident(root):
    return read_incident(page(root), PATH)


def status(root) -> str:
    return git(root / "wiki", "status", "--porcelain", "-uall")


def tracked(root) -> dict[str, bytes]:
    """Every tracked file's bytes: the whole-wiki form of "nothing was
    written"."""
    wiki = root / "wiki"
    return {rel: (wiki / rel).read_bytes()
            for rel in git(wiki, "ls-files").split()}


def event(root) -> dict:
    return json.loads(
        (root / ".state" / HEALTH_LOG).read_text().splitlines()[-1])


def retry_line(capsys) -> str:
    """The preview's publish line. Taken by its prefix rather than as the
    last physical line, because a genuinely multi-line `--notes` value is
    quoted and wraps."""
    return capsys.readouterr().out.split(PUBLISH_WITH)[-1]


def retry_argv(capsys) -> list[str]:
    """That line read back the way an operator's paste would deliver it."""
    return shlex.split(retry_line(capsys))[1:]


def blocks(text: str) -> dict[str, str]:
    return {heading: body for heading, body in sections(text)}


def duplicate_pair(root) -> None:
    """The wiki as a re-ingest left it: two open pages for the same db and
    day, both listed in the index, each citing a day the other does not."""
    wiki = root / "wiki"
    (wiki / PATH).write_text(incident_page(DB, TITLE, error_codes=(CODE,),
                                           body=DUPLICATE_BODY))
    (wiki / KEPT_PATH).write_text(incident_page(DB, KEPT_TITLE,
                                                error_codes=(CODE,),
                                                body=KEPT_BODY))
    (wiki / KEPT_DIGEST).write_text(f"# {DB} {KEPT_DAY}\n")
    (wiki / "index.md").write_text(f"{INDEX}{KEPT_BULLET}\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "a second page for the same day")


def unrelated_commit(root) -> None:
    wiki = root / "wiki"
    (wiki / "databases").mkdir()
    (wiki / "databases" / "cdb1.md").write_text(DATABASE_PAGE)
    (wiki / "index.md").write_text(
        INDEX + "\n## Databases\n\n- [[databases/cdb1]] cdb1\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "an unrelated page")


def test_the_presentation_table_covers_exactly_the_derived_fields():
    """`PRESENTATION`'s load-bearing claim: one row per field the `lifecycle`
    dataclasses actually declare. A field added to a command and not to the
    table fails here rather than reaching an operator as an unlabelled
    control, and a row for a field that no longer exists fails too."""
    assert set(incident_action.VERBS.values()) == set(lifecycle.KINDS)
    assert set(incident_action.PRESENTATION) == {
        (verb, spec.name)
        for verb, command in incident_action.VERBS.items()
        for spec in incident_action.command_fields(command)}


def test_every_flag_argparse_builds_parks_on_its_command_field():
    """The other half: the dest argparse derives from the flag is the
    `lifecycle.Command` field of the same name, including the one field argv
    spells as a negative."""
    for verb, command in incident_action.VERBS.items():
        for spec in incident_action.command_fields(command):
            built = argparse.ArgumentParser().add_argument(
                incident_action.flag_of(verb, spec.name),
                **incident_cli._flag_kwargs(verb, spec))
            assert built.dest == spec.name, (verb, spec.name)
            assert built.required == spec.required, (verb, spec.name)


def test_the_required_fields_are_the_ones_the_hand_table_named():
    """A golden list, because the schema now derives from one source: a
    default added to a command field would quietly make an operator's flag
    optional, and nothing derived from the same dataclass could notice. These
    are the required/optional tuples the codec replaced, verbatim."""
    assert {verb: tuple(spec.name for spec
                        in incident_action.command_fields(command)
                        if spec.required)
            for verb, command in incident_action.VERBS.items()} == {
        "record-action": ("intent", "summary"),
        "monitor": ("signal", "until", "intent", "summary"),
        "extend": ("until", "intent"),
        "resolve": ("summary",),
        "reopen": ("reason",),
        "merge": ("into",)}


def test_the_signal_help_names_every_kind_the_codec_knows():
    """The help string spells the kinds out; `SIGNAL_KEYS` derives them. This
    is what keeps the two from drifting when a variant is added."""
    help_text = incident_action.PRESENTATION[("monitor", "signal")].help
    assert set(incident_action.SIGNAL_KEYS) == {"error_absent", "event_present",
                                                "flow_resumed", "manual"}
    for kind in incident_action.SIGNAL_KEYS:
        assert f"{kind}:" in help_text


#: One fully populated command per verb, every optional set to something other
#: than its default so `encode` has work to do on the way back.
ROUND_TRIP: dict[str, dict] = {
    "record-action": {"intent": INTENT, "summary": SUMMARY,
                      "ticket": "CHG-4471", "outcome": "succeeded",
                      "rollback": "restore the old tnsnames entry",
                      "evidence": [DIGEST], "notes": "queue depth was 32"},
    "monitor": {"signal": f"event_present:{CODE}: connect timed out",
                "until": UNTIL, "intent": INTENT, "summary": SUMMARY,
                "ticket": "CHG-4471", "evidence": [DIGEST], "notes": "watch it"},
    "extend": {"until": FURTHER, "intent": INTENT, "notes": "one more night"},
    "resolve": {"summary": FIXED, "residual_risk": "the standby is still slow",
                "ticket": "CHG-4471", "evidence": [DIGEST], "notes": "done",
                "update_error_pages": False},
    "reopen": {"reason": "the listener failed again", "evidence": [DIGEST],
               "notes": "third time"},
}


def test_every_verb_round_trips_through_the_codec():
    """`decode(encode(a)) == a`, which is what lets the portal echo a
    normalised command across the preview/commit gap and get the same bytes
    back. Pure over the tables; no wiki."""
    actor = transaction.Actor(EMAIL, "DBA", "git")
    base = "a" * 40
    for verb, fields in ROUND_TRIP.items():
        action = incident_action.decode(slug=SLUG, verb=verb, fields=fields,
                                        actor=actor, base=base, at=AT)
        again = incident_action.decode(slug=SLUG, verb=verb,
                                       fields=incident_action.encode(action),
                                       actor=actor, base=base, at=AT)
        assert again == action, verb


def test_the_codec_names_the_field_it_cannot_take():
    actor = transaction.Actor(EMAIL, "DBA", "git")
    with pytest.raises(incident_action.FieldError) as caught:
        incident_action.decode(slug=SLUG, verb="record-action",
                               fields={"intent": INTENT, "summary": SUMMARY,
                                       "untill": UNTIL},
                               actor=actor, base="a" * 40, at=AT)
    assert caught.value.field == "untill"


def test_the_fixture_wiki_lints_clean(root):
    """Asserted first: every commit test below would exit 4 for the wrong
    reason if the wiki it starts from already had findings."""
    assert lint.lint_wiki(root / "wiki") == []


def test_a_preview_writes_nothing_and_prints_how_to_publish(root, capsys):
    before = transaction.head(root / "wiki")
    assert cli.main(RECORD) == 0
    out = capsys.readouterr().out
    assert status(root) == ""
    assert transaction.head(root / "wiki") == before
    assert f"+## Action {AT}" in out
    assert out.splitlines()[-1].startswith(
        "preview only. publish with: dbwiki incident record-action")


def test_a_preview_records_no_run_health_event(root):
    assert cli.main(RECORD) == 0
    assert not (root / ".state" / HEALTH_LOG).exists()


def test_a_preview_lint_would_block_exits_one_and_still_shows_the_diff(
        root, capsys):
    assert cli.main(RECORD + ["--evidence", "digests/cdb1/2026-01-01.md"]) == 1
    out = capsys.readouterr().out
    assert f"+## Action {AT}" in out and "digest-missing" in out
    assert "preview only. publish with:" in out
    assert status(root) == ""


def test_the_retry_line_publishes_and_running_it_again_changes_nothing(
        root, capsys):
    assert cli.main(RECORD + ["--ticket", "CHG-4471", "--outcome", "succeeded",
                              "--evidence", DIGEST]) == 0
    argv = retry_argv(capsys)
    assert cli.main(argv) == 0
    landed = transaction.head(root / "wiki")

    argv[argv.index("--base") + 1] = landed
    assert cli.main(argv) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert transaction.head(root / "wiki") == landed
    record = parse_actions(page(root)).records[-1]
    assert record.ticket == "CHG-4471"
    assert [str(e) for e in record.evidence] == [DIGEST]
    assert record.outcome is Outcome.SUCCEEDED


def test_notes_come_off_disk_and_ride_the_retry_line_as_one_line(root, capsys):
    """The file is resolved before the line is built, so the line publishes
    from a machine that never had it, and stripped, so the trailing newline
    every text file ends with cannot split the line in two."""
    notes = root / "notes.txt"
    notes.write_text("the listener log named the wrong host\n")
    assert cli.main(RECORD + ["--notes-file", str(notes)]) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    assert line.startswith(PUBLISH_WITH)
    assert "--notes-file" not in line
    notes.unlink()
    assert cli.main(shlex.split(line.removeprefix(PUBLISH_WITH))[1:]) == 0
    assert (parse_actions(page(root)).records[-1].notes
            == "the listener log named the wrong host")


def test_an_unreadable_notes_file_refuses_instead_of_raising(root, capsys):
    assert cli.main(RECORD + ["--notes-file", str(root / "nope.txt")]) == 2
    assert "nope.txt" in capsys.readouterr().err


def test_committing_writes_the_action_the_log_line_and_the_trailer(
        root, capsys):
    assert cli.main(RECORD + ["--commit"]) == 0
    printed = capsys.readouterr().out.split()
    assert printed[0] == git(root / "wiki", "rev-parse", "--short",
                             "HEAD").strip()
    assert PATH in printed and "log.md" in printed
    assert f"Actor: {EMAIL}" in git(root / "wiki", "log", "-1", "--pretty=%B")
    record = parse_actions(page(root)).records[-1]
    assert (record.at, record.kind, record.actor) == (AT, "record-action",
                                                      EMAIL)
    assert record.summary == SUMMARY
    assert record.log_line(PATH) in (root / "wiki" / "log.md").read_text()
    assert incident(root).status is Status.OPEN


def test_monitoring_moves_the_status_and_pins_the_window(root):
    assert cli.main(MONITOR + ["--commit"]) == 0
    inc = incident(root)
    assert inc.status is Status.MONITORING
    assert inc.monitoring == MonitoringWindow(ErrorAbsent(CODE), AT, UNTIL)
    assert frontmatter(page(root))["monitoring"]["kind"] == "error_absent"


def test_a_signal_payload_may_carry_colons_of_its_own(root):
    pattern = "TNS-12564: connect timed out"
    assert cli.main(["incident", "monitor", SLUG, "--at", AT, "--commit",
                     "--signal", f"event_present:{pattern}", "--until", UNTIL,
                     "--intent", INTENT, "--summary", SUMMARY]) == 0
    assert incident(root).monitoring.signal == EventPresent(pattern)


def test_resolving_moves_the_index_bullet_and_writes_the_error_page_row(root):
    assert cli.main(MONITOR + ["--commit"]) == 0
    assert cli.main(RESOLVE + ["--commit"]) == 0
    text = page(root)
    assert read_incident(text, PATH).status is Status.RESOLVED
    assert "monitoring" not in frontmatter(text)
    index = blocks((root / "wiki" / "index.md").read_text())
    assert BULLET not in index["## Open incidents"]
    assert BULLET in index["## Resolved incidents"]
    assert (f"| {LATER[:10]} | {DB} | [[{TARGET}]] | {FIXED} | {LATER} |"
            in (root / "wiki" / f"errors/{CODE}.md").read_text())


def test_extending_keeps_the_signal_and_the_start_and_moves_until(root):
    assert cli.main(MONITOR + ["--commit"]) == 0
    assert cli.main(["incident", "extend", SLUG, "--until", FURTHER,
                     "--intent", "give the standby one more night",
                     "--at", LATER, "--commit"]) == 0
    assert incident(root).monitoring == MonitoringWindow(
        ErrorAbsent(CODE), AT, FURTHER)


def test_reopening_returns_the_bullet_and_the_status_to_open(root):
    assert cli.main(RESOLVE + ["--commit"]) == 0
    assert cli.main(["incident", "reopen", SLUG, "--at", FURTHER, "--commit",
                     "--reason", "the listener failed again overnight"]) == 0
    assert incident(root).status is Status.OPEN
    index = blocks((root / "wiki" / "index.md").read_text())
    assert BULLET in index["## Open incidents"]
    assert "## Resolved incidents" not in index


def test_merging_folds_the_duplicate_into_the_page_that_keeps_it(root, capsys):
    """The whole verb through argv: preview, publish by pasting the retry
    line, and both pages, the index and the log as the wiki then holds them."""
    duplicate_pair(root)
    assert cli.main(MERGE) == 0
    assert status(root) == ""
    argv = retry_argv(capsys)
    assert argv[argv.index("--into") + 1] == KEPT_SLUG
    assert cli.main(argv) == 0

    assert incident(root).status is Status.RESOLVED
    kept_text = (root / "wiki" / KEPT_PATH).read_text()
    kept = read_incident(kept_text, KEPT_PATH)
    assert kept.status is Status.OPEN
    dropped_record = parse_actions(page(root)).records[-1]
    kept_record = parse_actions(kept_text).records[-1]
    assert (dropped_record.kind, kept_record.kind) == ("merge", "record-action")
    assert dropped_record.notes == f"Superseded by [[{KEPT_TARGET}]]."
    assert f"- {DAY}: {DIGEST}" in blocks(kept_text)["## Evidence"]
    assert f"- {KEPT_DAY}: {KEPT_DIGEST}" in blocks(kept_text)["## Evidence"]
    assert "the duplicate's account of the day it was opened for" in kept_text

    index = blocks((root / "wiki" / "index.md").read_text())
    assert BULLET not in index["## Open incidents"]
    assert BULLET in index["## Resolved incidents"]
    assert KEPT_BULLET in index["## Open incidents"]
    log = (root / "wiki" / "log.md").read_text()
    assert log.count(f"[{LATER}] incident") == 1
    assert dropped_record.log_line(PATH) in log


def test_pasting_the_merge_retry_line_a_second_time_writes_nothing(root,
                                                                   capsys):
    """The dropped page is `resolved` by then, and `(resolved, Merge)` has no
    row in the table, so the paste is refused as an illegal transition rather
    than folding the same page away twice."""
    duplicate_pair(root)
    assert cli.main(MERGE) == 0
    argv = retry_argv(capsys)
    assert cli.main(argv) == 0
    landed, before = transaction.head(root / "wiki"), tracked(root)

    argv[argv.index("--base") + 1] = landed
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert "Merge is not valid from status" in err and "resolved" in err
    assert tracked(root) == before
    assert transaction.head(root / "wiki") == landed


def test_merging_into_a_page_of_another_db_refuses_and_names_both_pages(
        root, capsys):
    duplicate_pair(root)
    other = "incidents/2026-08-30-cdb1-parse-errors.md"
    (root / "wiki" / other).write_text(incident_page("cdb1", "parse errors"))
    assert cli.main(["incident", "merge", SLUG, "--into", other,
                     "--at", LATER, "--commit"]) == 2
    err = capsys.readouterr().err
    assert PATH in err and other in err
    assert incident(root).status is Status.OPEN


def test_no_error_pages_rides_the_retry_line_bare_and_is_honoured(
        root, capsys):
    before = (root / "wiki" / f"errors/{CODE}.md").read_bytes()
    assert cli.main(RESOLVE + ["--no-error-pages"]) == 0
    argv = retry_argv(capsys)
    assert "--no-error-pages" in argv
    assert cli.main(argv) == 0
    assert incident(root).status is Status.RESOLVED
    assert (root / "wiki" / f"errors/{CODE}.md").read_bytes() == before


def test_a_moved_base_refuses_and_says_how_to_rebuild(root, capsys):
    assert cli.main(RECORD) == 0
    argv = retry_argv(capsys)
    before = tracked(root)
    unrelated_commit(root)
    assert cli.main(argv) == 3
    err = capsys.readouterr().err
    assert "base moved" in err and "rebuild with --base" in err
    assert (root / "wiki" / PATH).read_bytes() == before[PATH]


def test_a_missing_digest_blocks_the_commit_and_writes_nothing(root, capsys):
    before = tracked(root)
    assert cli.main(RECORD + ["--evidence", "digests/cdb1/2026-01-01.md",
                              "--commit"]) == 4
    err = capsys.readouterr().err
    assert "digest-missing" in err
    assert "lint blocked the commit; nothing was written" in err
    assert tracked(root) == before
    assert event(root)["outcome"] == "ok"
    assert event(root)["facts"]["validation"] == "lint_blocked"


def test_evidence_that_is_not_a_digest_path_refuses_and_writes_nothing(
        root, capsys):
    """Exit 2, beside the illegal transition below rather than beside the
    missing digest above. Whether a path can be evidence at all is a question
    about the command; whether that digest exists is lint's, and it answers
    at exit 4 over a tree this one never reaches."""
    before = tracked(root)
    assert cli.main(RECORD + ["--evidence", "errors/NOPE.md",
                              "--commit"]) == 2
    err = capsys.readouterr().err
    assert "errors/NOPE.md" in err and "not a digest path" in err
    assert status(root) == ""
    assert tracked(root) == before
    assert not (root / ".state" / HEALTH_LOG).exists()


def test_extending_an_open_incident_names_the_illegal_transition(root, capsys):
    assert cli.main(["incident", "extend", SLUG, "--until", FURTHER,
                     "--intent", INTENT, "--at", AT]) == 2
    err = capsys.readouterr().err
    assert "ExtendMonitoring is not valid from status" in err
    assert "open" in err


def test_an_unknown_signal_kind_names_the_ones_the_codec_knows(root, capsys):
    assert cli.main(["incident", "monitor", SLUG, "--signal", "error_gone:X",
                     "--until", UNTIL, "--intent", INTENT,
                     "--summary", SUMMARY, "--at", AT]) == 2
    assert ("error_absent, event_present, flow_resumed, manual"
            in capsys.readouterr().err)


def test_an_unattributable_action_refuses_before_anything_is_built(
        root, capsys):
    git(root / "wiki", "config", "--unset", "user.email")
    assert cli.main(RECORD) == 2
    assert "no actor" in capsys.readouterr().err


def test_the_actor_flag_overrides_the_repo_and_must_be_a_usable_email(
        root, capsys):
    assert cli.main(RECORD + ["--actor", "oncall@example.com",
                              "--commit"]) == 0
    assert parse_actions(page(root)).records[-1].actor == "oncall@example.com"
    assert ("Actor: oncall@example.com"
            in git(root / "wiki", "log", "-1", "--pretty=%B"))
    assert cli.main(RECORD + ["--actor", "not-an-email"]) == 2
    assert "not a usable actor email" in capsys.readouterr().err


def test_a_stray_file_previews_as_a_warning_and_then_refuses_the_commit(
        root, capsys):
    """Exit 5. The preview says so without refusing, because a stray is a
    fact about the tree rather than about this proposal."""
    (root / "wiki" / "scratch.md").write_text(
        "---\ntype: journal\n---\n\n# scratch\n")
    assert cli.main(RECORD) == 0
    assert ("stray: scratch.md is uncommitted and not ours; commit or "
            "restore it before --commit") in capsys.readouterr().out
    assert cli.main(RECORD + ["--commit"]) == 5
    assert ("wiki tree dirty; nothing was written. commit or restore: "
            "scratch.md") in capsys.readouterr().err
    assert event(root)["outcome"] == "ok"
    assert event(root)["facts"]["validation"] == "tree_dirty"


def test_a_busy_lock_exits_one_and_names_the_holder(root, capsys):
    """`incident` is not in `cli.LOCKED`: `incident_action.publish` takes the
    lock itself, around the commit alone, so it can release before pushing.
    The exit code and the holder's message are what they always were."""
    assert "incident" not in cli.LOCKED
    with single_flight(root / ".state", "run"):
        assert cli.main(RECORD + ["--commit", "--lock-wait", "0"]) == 1
    assert "another dbwiki command holds the lock" in capsys.readouterr().err
    assert status(root) == ""


def test_a_busy_lock_is_a_validation_not_a_failed_run(root, capsys):
    """A click that meets a tick is optimistic-concurrency traffic, not
    pipeline ill-health; recording it as a failure would turn `dbwiki health`
    red every time an operator publishes during a run. This is the one arm
    that changed when the lock moved into `publish`."""
    with single_flight(root / ".state", "run"):
        assert cli.main(RECORD + ["--commit", "--lock-wait", "0"]) == 1
    capsys.readouterr()
    ev = event(root)
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"]["validation"] == "lock_busy"
    assert ev["facts"]["commit"] is None and ev["facts"]["pushed"] is False
    assert ev["facts"]["surface"] == "cli"


def test_a_push_that_fails_after_the_commit_still_reports_committed(
        root, monkeypatch):
    """`publish` commits with `push=False`, releases the lock, then pushes: a
    dead remote must not undo a landed commit, and the tick must never wait on
    it. `Committed.pushed` and the audit fact both say what actually
    happened."""
    wiki = root / "wiki"
    actor = transaction.resolve_actor(wiki)
    action = incident_action.decode(
        slug=SLUG, verb="record-action",
        fields={"intent": INTENT, "summary": SUMMARY}, actor=actor,
        base=transaction.head(wiki), at=AT)
    proposal = action.proposal(transaction.Tree.at(wiki, action.base))
    monkeypatch.setattr(gitutil, "push_best_effort", lambda repo: False)

    published = incident_action.publish(wiki, root / ".state", action,
                                        proposal, lock_wait_s=0, push=True,
                                        surface="cli")
    assert isinstance(published, transaction.Committed)
    assert published.pushed is False
    ev = event(root)
    assert ev["outcome"] == "ok"
    assert ev["facts"]["validation"] == "ok" and ev["facts"]["pushed"] is False
    assert parse_actions(page(root)).records[-1].at == AT


def test_everything_read_only_still_works_while_a_tick_holds_the_lock(root):
    """Reading an incident, and rehearsing an action on it, is exactly when an
    operator needs the wiki and exactly when a tick is likeliest to be writing
    it. Nothing here takes the lock: `publish` is the only thing that does."""
    with single_flight(root / ".state", "run"):
        assert cli.main(["incident", "list"]) == 0
        assert cli.main(["incident", "show", SLUG]) == 0
        assert cli.main(RECORD) == 0


def test_a_commit_records_the_incident_facts(root):
    base = transaction.head(root / "wiki")
    assert cli.main(RECORD + ["--commit"]) == 0
    ev = event(root)
    assert ev["command"] == "incident"
    assert ev["facts"] == {
        "incident": SLUG, "command": "record-action", "actor": EMAIL,
        "base": base, "at": AT, "validation": "ok", "pushed": False,
        "surface": "cli",
        "commit": git(root / "wiki", "rev-parse", "--short", "HEAD").strip()}


def test_a_moved_base_is_recorded_as_validation_not_as_a_failure(root, capsys):
    assert cli.main(RECORD) == 0
    argv = retry_argv(capsys)
    unrelated_commit(root)
    assert cli.main(argv) == 3
    ev = event(root)
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"]["validation"] == "base_moved"
    assert ev["facts"]["commit"] is None


def test_show_prints_the_window_the_action_and_what_is_allowed(root, capsys):
    assert cli.main(MONITOR + ["--commit"]) == 0
    capsys.readouterr()
    assert cli.main(["incident", "show", SLUG]) == 0
    out = capsys.readouterr().out
    assert f"status: monitoring ({Status.MONITORING.label})" in out
    assert f"window: error_absent {CODE} from {AT} until {UNTIL}" in out
    assert f"  {AT} start-monitoring {EMAIL} [pending] {SUMMARY}" in out
    assert "allowed: record-action, monitor, extend, resolve, merge" in out


def test_an_off_vocabulary_status_still_shows_and_reads_as_open(root, capsys):
    (root / "wiki" / PATH).write_text(
        page(root).replace("status: open", "status: clsoed"))
    assert cli.main(["incident", "show", SLUG]) == 0
    out = capsys.readouterr().out
    assert f"status: open ({Status.OPEN.label})" in out
    assert "status is not in the vocabulary: 'clsoed'" in out
    assert "allowed: record-action, monitor, resolve, merge" in out


def test_a_published_verdict_reaches_both_the_list_and_the_page(root, capsys):
    """`.state/monitoring/<slug>.json` is `dbwiki run`'s output; both read
    views surface it so an operator sees an answered window without opening
    the page."""
    assert cli.main(MONITOR + ["--commit"]) == 0
    facts = root / ".state" / "monitoring" / f"{SLUG}.json"
    facts.parent.mkdir(parents=True, exist_ok=True)
    facts.write_text(json.dumps({
        "verdict": "not_met",
        "contradictions": [f"2026-08-31: {CODE} x 4", "and again on 09-01"]}))
    capsys.readouterr()
    assert cli.main(["incident", "list"]) == 0
    assert capsys.readouterr().out.startswith("monitoring  not_met")
    assert cli.main(["incident", "show", SLUG]) == 0
    assert (f"monitoring: not_met (first contradiction: "
            f"2026-08-31: {CODE} x 4)" in capsys.readouterr().out)


def test_show_on_a_missing_slug_exits_two(root, capsys):
    assert cli.main(["incident", "show", "no-such-incident"]) == 2
    assert ("no incident page at incidents/no-such-incident.md"
            in capsys.readouterr().err)


def test_every_slug_form_addresses_the_same_incident(root, capsys):
    printed = []
    for form in (SLUG, PATH, str(root / "wiki" / PATH)):
        assert cli.main(["incident", "show", form]) == 0
        printed.append(capsys.readouterr().out)
    assert printed[0].startswith(f"{PATH}\n")
    assert printed[0] == printed[1] == printed[2]


def test_list_holds_the_attention_queue_and_all_holds_the_history(
        root, capsys):
    assert cli.main(["incident", "list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("open ") and SLUG in out and TITLE in out

    assert cli.main(RESOLVE + ["--commit"]) == 0
    capsys.readouterr()
    assert cli.main(["incident", "list"]) == 0
    assert capsys.readouterr().out == "no active incidents\n"
    assert cli.main(["incident", "list", "--all"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("resolved ") and SLUG in out


class BrokenStdout:
    """A closed pipe, the way `| head` leaves one."""

    def write(self, *_args):
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self):
        pass


def test_the_retry_line_pins_the_resolved_actor(root, capsys):
    """Echoing `--actor` only when it was typed let the pasted line re-resolve
    to whatever identity the pasting machine had."""
    assert cli.main(RECORD) == 0
    argv = retry_argv(capsys)
    assert argv[argv.index("--actor") + 1] == EMAIL
    assert cli.main(argv) == 0
    assert parse_actions(page(root)).records[-1].actor == EMAIL


def test_a_flag_value_that_looks_like_a_flag_survives_the_retry_line(root,
                                                                     capsys):
    """`--intent --commit` reads back as two flags; `--intent=--commit` is one
    shell word and one argparse value."""
    assert cli.main(["incident", "record-action", SLUG, "--intent=--commit",
                     "--summary", SUMMARY, "--at", AT]) == 0
    argv = retry_argv(capsys)
    assert "--intent=--commit" in argv
    assert cli.main(argv) == 0
    assert parse_actions(page(root)).records[-1].intent == "--commit"


def test_a_broken_pipe_after_the_commit_is_not_a_failed_run(root, monkeypatch):
    """The commit has landed by the time anything is printed; a write that
    cannot reach a reader must not turn it into a failed run."""
    monkeypatch.setattr(sys, "stdout", BrokenStdout())
    with pytest.raises(BrokenPipeError):
        cli.main(RECORD + ["--commit"])
    monkeypatch.undo()
    ev = event(root)
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"]["commit"] == git(root / "wiki", "rev-parse", "--short",
                                        "HEAD").strip()
    assert ev["facts"]["pushed"] is False
    assert parse_actions(page(root)).records[-1].at == AT


BARE_BODY = (f"The listener aborted with {CODE} overnight, then {CODE} "
             f"again at 04:10.")


def bare_codes(root) -> None:
    """The wiki as the structured writer left it before it learned to link:
    the incident names its code in prose and nothing on the page says which
    error page that is."""
    wiki = root / "wiki"
    (wiki / PATH).write_text(incident_page(DB, TITLE, body=BARE_BODY))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "an incident that only names its code")


def test_link_codes_dry_run_names_each_page_and_writes_nothing(root, capsys):
    bare_codes(root)
    before = tracked(root)
    head = transaction.head(root / "wiki")
    assert cli.main(["incident", "link-codes", "--dry-run"]) == 0
    assert capsys.readouterr().out == f"{PATH}: {CODE}\n"
    assert tracked(root) == before
    assert transaction.head(root / "wiki") == head
    assert status(root) == ""


def test_link_codes_publishes_one_commit_and_then_finds_nothing(root, capsys):
    bare_codes(root)
    assert cli.main(["incident", "link-codes"]) == 0
    assert incident(root).error_codes == (CODE,)
    assert f"[[errors/{CODE}]] overnight, then {CODE} again" in page(root)
    wiki = root / "wiki"
    assert (git(wiki, "log", "-1", "--pretty=%s").strip()
            == incident_cli.LINK_CODES_MESSAGE)
    assert f"Actor: {EMAIL}" in git(wiki, "log", "-1", "--pretty=%b")
    capsys.readouterr()
    assert cli.main(["incident", "link-codes"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert status(root) == ""


def test_link_codes_leaves_a_code_the_wiki_has_no_page_for_alone(root, capsys):
    wiki = root / "wiki"
    (wiki / PATH).write_text(incident_page(DB, TITLE, body="ORA-7445 hit it."))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "an incident naming a code with no page")
    assert cli.main(["incident", "link-codes", "--dry-run"]) == 0
    assert "nothing to do" in capsys.readouterr().out


#: The row a resolve puts on the error page it links, with no evidence cited
#: so the evidence cell falls back to the record's own timestamp.
RESOLVED_ROW = f"| {LATER[:10]} | {DB} | [[{TARGET}]] | {FIXED} | {LATER} |"


def unrecorded_resolution(root) -> str:
    """The wiki as it stood before a resolve touched error pages: the incident
    is resolved and the error page it links says nothing about it. Returns the
    sha the resolve was built against, which is what lets a test rebuild the
    same resolve through `lifecycle.build` and compare bytes.

    `--no-error-pages` now leaves `error_pages: false` in the record (issue
    14), which backfill honours; a resolve from before error pages were
    touched carried no such key, so the key is taken back out."""
    base = transaction.head(root / "wiki")
    assert cli.main(RESOLVE + ["--no-error-pages", "--commit"]) == 0
    wiki = root / "wiki"
    (wiki / PATH).write_text(page(root).replace("error_pages: false\n", ""))
    git(wiki, "commit", "-qam", "a resolve from before error pages")
    return base


def test_backfill_resolutions_honours_a_resolve_that_declined_error_pages(
        root, capsys):
    assert cli.main(RESOLVE + ["--no-error-pages", "--commit"]) == 0
    capsys.readouterr()
    assert cli.main(["incident", "backfill-resolutions", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"note: {PATH} was resolved with --no-error-pages" in out
    assert "nothing to do" in out
    assert lifecycle.RESOLUTION_SECTION not in (
        root / "wiki" / f"errors/{CODE}.md").read_text()


def test_backfill_resolutions_dry_run_names_each_row_and_writes_nothing(
        root, capsys):
    unrecorded_resolution(root)
    capsys.readouterr()
    before = tracked(root)
    head = transaction.head(root / "wiki")
    assert cli.main(["incident", "backfill-resolutions", "--dry-run"]) == 0
    assert capsys.readouterr().out == (f"errors/{CODE}.md: {SLUG} "
                                       f"({LATER[:10]})\n")
    assert tracked(root) == before
    assert transaction.head(root / "wiki") == head
    assert status(root) == ""


def test_backfill_resolutions_publishes_one_commit_and_then_finds_nothing(
        root, capsys):
    unrecorded_resolution(root)
    capsys.readouterr()
    wiki = root / "wiki"
    rel = f"errors/{CODE}.md"
    assert cli.main(["incident", "backfill-resolutions"]) == 0
    written = (wiki / rel).read_text()
    assert lifecycle.RESOLUTION_HEAD in written
    assert RESOLVED_ROW in written
    assert (git(wiki, "log", "-1", "--pretty=%s").strip()
            == incident_cli.BACKFILL_MESSAGE)
    assert f"Actor: {EMAIL}" in git(wiki, "log", "-1", "--pretty=%b")
    capsys.readouterr()
    assert cli.main(["incident", "backfill-resolutions"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert (wiki / rel).read_text() == written
    assert status(root) == ""


def test_backfill_resolutions_notes_an_error_page_the_wiki_does_not_hold(
        root, capsys):
    """Creating error pages is the ingest writer's job, so a link to a page
    the wiki has not got is an advisory and the other rows still land."""
    wiki = root / "wiki"
    (wiki / "errors" / "ORA-600.md").write_text(
        "---\ntype: error-class\n---\n\n# ORA-600\n")
    (wiki / PATH).write_text(incident_page(DB, TITLE,
                                           error_codes=(CODE, "ORA-600")))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "an incident linking two error pages")
    unrecorded_resolution(root)
    git(wiki, "rm", "-q", "errors/ORA-600.md")
    git(wiki, "commit", "-m", "the second error page went away")
    capsys.readouterr()
    assert cli.main(["incident", "backfill-resolutions"]) == 0
    assert "note: errors/ORA-600.md is absent" in capsys.readouterr().out
    assert RESOLVED_ROW in (wiki / f"errors/{CODE}.md").read_text()
    assert not (wiki / "errors" / "ORA-600.md").exists()


def test_backfill_resolutions_skips_a_resolved_page_with_no_resolve_record(
        root, capsys):
    wiki = root / "wiki"
    (wiki / PATH).write_text(incident_page(DB, TITLE, status="resolved",
                                           error_codes=(CODE,)))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "a status somebody edited by hand")
    assert cli.main(["incident", "backfill-resolutions", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"note: {PATH} is resolved and holds no resolve record " \
           f"(last action: none)" in out
    assert "nothing to do" in out
    assert lifecycle.RESOLUTION_SECTION not in (
        wiki / f"errors/{CODE}.md").read_text()


def test_backfill_resolutions_skips_a_page_a_merge_closed(root, capsys):
    """The common case, not a defect: a merge folds a duplicate away, which
    is not a confirmed fix and owes the error page no row. The note names the
    kind so an operator can tell it from a hand-edited status."""
    duplicate_pair(root)
    assert cli.main(MERGE + ["--commit"]) == 0
    assert incident(root).status is Status.RESOLVED
    capsys.readouterr()
    assert cli.main(["incident", "backfill-resolutions"]) == 0
    assert (f"note: {PATH} is resolved and holds no resolve record "
            f"(last action: merge)") in capsys.readouterr().out
    assert lifecycle.RESOLUTION_SECTION not in (
        root / "wiki" / f"errors/{CODE}.md").read_text()


def test_a_backfilled_row_is_the_bytes_a_resolve_would_have_written(root):
    """The anti-drift assertion. Both writers go through
    `lifecycle.resolution_row`, so the backfilled error page must be byte for
    byte the page `lifecycle.build` would have committed for the same resolve.
    Anything less and a later resolve would key on a different prefix and
    append a second row instead of rewriting the first."""
    base = unrecorded_resolution(root)
    assert cli.main(["incident", "backfill-resolutions"]) == 0
    wiki = root / "wiki"
    rel = f"errors/{CODE}.md"
    proposal = lifecycle.build(transaction.Tree.at(wiki, base), PATH,
                               lifecycle.Resolve(summary=FIXED),
                               transaction.Actor(EMAIL, "DBA", "git"), LATER)
    assert (wiki / rel).read_text() == proposal.files[rel]


def test_two_unpinned_acts_in_one_second_both_reach_the_page(
        root, monkeypatch, capsys):
    """Issue 13: with no `--at`, the CLI's clock may hand two scripted
    commands the same second. The second is stamped one second on rather
    than refused (or, before, silently replacing the first on the page)."""
    monkeypatch.setattr(incident_cli, "_now", lambda: AT)
    unpinned = RECORD[:RECORD.index("--at")]
    assert cli.main(unpinned + ["--commit"]) == 0
    assert cli.main(["incident", "resolve", SLUG, "--summary", FIXED,
                     "--commit"]) == 0
    records = incident(root).actions.records
    assert [(r.kind, r.at) for r in records] == [
        ("record-action", AT), ("resolve", "2026-08-30T14:22:11Z")]


def test_a_pinned_at_that_collides_is_refused_with_exit_two(root, capsys):
    assert cli.main(RECORD + ["--commit"]) == 0
    capsys.readouterr()
    assert cli.main(["incident", "resolve", SLUG, "--summary", FIXED,
                     "--at", AT, "--commit"]) == 2
    assert "at:" in capsys.readouterr().err
    assert [r.kind for r in incident(root).actions.records] == [
        "record-action"]
