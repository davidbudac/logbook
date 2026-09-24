"""The incident state machine: which transitions are legal, and exactly which
files each one writes.

`build` is pure over a `Tree.of` dict, so a test states a whole wiki as a
literal and reads the whole transaction back out of one `Proposal`. No git and
no disk: what is pinned here is the mapping from (status, command) to bytes.

The legal and illegal case lists together cover every cell of the three-status
by six-command grid, and one test asserts that they do, so a seventh command
cannot be added without a row here.
"""

import pytest

import fixtures as fx
from dbwiki import lifecycle as lc
from dbwiki.incidents import (ActionMalformed, ErrorAbsent, MonitoringWindow,
                              Outcome, Status, parse_actions, read_incident,
                              set_status)
from dbwiki.lifecycle import (ExtendMonitoring, Merge, MergeRefused,
                              RecordAction, Reopen, Resolve, StartMonitoring,
                              TransitionError)
from dbwiki.lint import lint_wiki
from dbwiki.pagetext import sections
from dbwiki.transaction import Actor, Tree
from fixtures.incident_pages import incident_page

PATH = "incidents/2026-08-05-cdb1_stby-tns-12564.md"
TARGET = PATH[:-3]
SLUG = TARGET.removeprefix("incidents/")
DB = "cdb1_stby"
CODE = "TNS-12564"
TITLE = "TNS-12564 connect failures on cdb1_stby"
LABEL = "TNS-12564 fatal NI connect errors on the standby"

AT = "2026-08-30T14:22:10Z"
DAY = "2026-08-30"
LATER = "2026-08-30T18:40:00Z"
LATEST = "2026-08-30T19:05:00Z"
OPENED = "2026-08-29T09:00:00Z"
UNTIL = "2026-09-02T00:00:00Z"
FURTHER = "2026-09-05T00:00:00Z"

ACTOR = Actor("dba@example.com", "DBA", "flag")
SIGNAL = ErrorAbsent(CODE)
WINDOW = MonitoringWindow(SIGNAL, OPENED, UNTIL)

INTENT = "stop the standby losing its connection every night"
SUMMARY = "restarted the standby listener"
FIXED = "restarted the listener and rebuilt the tnsnames entry"

LOG = "---\ntype: log\n---\n\n# Log\n"

INDEX = f"""---
type: index
---

# Logbook

Rebuilt 2026-07-27 (previous checkout lost; digests regenerated from ES).

## Databases

- [[databases/cdb1]] — cdb1
- [[databases/{DB}]] — {DB}

## Open incidents

- [[incidents/2026-07-12-cdb1-dataguard-transport-failure]] — Dataguard transport failures
- [[{TARGET}]] — {LABEL}

## Journals

- [[databases/cdb1/journal/2026-08]] — cdb1 journal 2026-08

## Reports

- [[reports/2026-08-29]] — fleet report, 2026-08-29 06:00–18:00

## Sources (approved external references)

- [[sources/oracle-support]] — My Oracle Support

## Error classes

- [[errors/{CODE}]] — {CODE}
- [[errors/ORA-12514]] — ORA-12514
"""

BULLET = f"- [[{TARGET}]] — {LABEL}\n"
UNLISTED_INDEX = INDEX.replace(BULLET, "")
RESOLVED_INDEX = UNLISTED_INDEX + f"\n## {lc.INDEX_RESOLVED}\n\n{BULLET}"

ERROR_PAGE = f"""---
type: error-class
updated: 2026-08-05T22:15:49Z
---

# {CODE}

## Occurrences

| day | db | note | evidence |
|---|---|---|---|
| 2026-08-05 | {DB} | first-ever connect failure | digests/{DB}/2026-08-05.md |
"""

#: The other page for the same db and the same day: the duplicate the wiki
#: keeps, into which `PATH` is folded. Its days bracket the dropped page's, so
#: a carried `## Update` has somewhere to land in the middle of them.
KEPT_PATH = "incidents/2026-08-05-cdb1_stby-tns-12564-again.md"
KEPT_TARGET = KEPT_PATH[:-3]
KEPT_SLUG = KEPT_TARGET.removeprefix("incidents/")
KEPT_TITLE = "TNS-12564 connect failures on cdb1_stby, opened twice"

EARLY_DAY = "2026-08-03"
KEPT_DAY = "2026-08-04"
DROPPED_DAY = "2026-08-05"
LATE_DAY = "2026-08-06"
LATEST_DAY = "2026-08-07"

KEPT_BODY = f"""## Evidence

- {KEPT_DAY}: digests/{DB}/{KEPT_DAY}.md — the kept page's own wording

## Update {KEPT_DAY}

what the ingest wrote the day the incident opened

## Update {LATE_DAY}

what the ingest wrote two days later"""

DROPPED_BODY = f"""## Evidence

- {KEPT_DAY}: digests/{DB}/{KEPT_DAY}.md — the duplicate worded that day its own way
- {DROPPED_DAY}: digests/{DB}/{DROPPED_DAY}.md — the day only the duplicate cites

## Update {DROPPED_DAY}

the duplicate's account of a day the kept page never wrote up"""

TWICE_BODY = f"""## Evidence

- {DROPPED_DAY}: digests/{DB}/{DROPPED_DAY}.md — the duplicate cited the day
- {DROPPED_DAY}: digests/{DB}/{DROPPED_DAY}.md — and cited it again, reworded"""

SAME_DAY_BODY = f"""## Update {KEPT_DAY}

the duplicate's rewording of a day the kept page already wrote up"""


def update_body(day):
    return f"""## Update {day}

the duplicate's account of {day}"""


def kept_page(status=Status.OPEN, *, window=None, db=DB, raw_status=None,
              body=KEPT_BODY):
    text = incident_page(db, KEPT_TITLE, status=raw_status or str(status),
                         opened=OPENED, body=body, error_codes=(CODE,))
    return text if window is None else set_status(text, status, updated=OPENED,
                                                  monitoring=window)


KEPT = kept_page()

NOTE = RecordAction(intent=INTENT, summary=SUMMARY)
START = StartMonitoring(signal=SIGNAL, until=UNTIL, intent=INTENT,
                        summary=SUMMARY)
EXTEND = ExtendMonitoring(until=FURTHER,
                          intent="give the standby one more night")
RESOLVE = Resolve(summary=FIXED)
REOPEN = Reopen(reason="the listener failed again overnight")
MERGE = Merge(into=KEPT_SLUG)

INCIDENT_AND_LOG = {PATH, "log.md"}
WITH_INDEX = INCIDENT_AND_LOG | {"index.md"}
WITH_ERROR_PAGE = WITH_INDEX | {f"errors/{CODE}.md"}
WITH_KEPT = WITH_INDEX | {KEPT_PATH}


def incident(status=Status.OPEN, *, window=None, codes=(CODE,),
             raw_status=None, body=""):
    text = incident_page(DB, TITLE, status=raw_status or str(status),
                         opened=OPENED, body=body, error_codes=codes)
    return text if window is None else set_status(text, status, updated=OPENED,
                                                  monitoring=window)


def wiki(status=Status.OPEN, *, window=None, codes=(CODE,), raw_status=None,
         index=INDEX, error_page=ERROR_PAGE, log=LOG, body="", kept=KEPT):
    files = {PATH: incident(status, window=window, codes=codes,
                            raw_status=raw_status, body=body)}
    for rel, content in (("index.md", index), ("log.md", log),
                         (f"errors/{CODE}.md", error_page), (KEPT_PATH, kept)):
        if content is not None:
            files[rel] = content
    return files


def build(files, command, at=AT):
    return lc.build(Tree.of(files), PATH, command, ACTOR, at)


def applied(files, proposal):
    return {**files, **proposal.files}


def written(proposal):
    return read_incident(proposal.files[PATH], PATH)


def record_of(proposal):
    return parse_actions(proposal.files[PATH]).records[-1]


def kept_record_of(proposal):
    return parse_actions(proposal.files[KEPT_PATH]).records[-1]


def merged_kept(**kw):
    """The kept page as one merge of `DROPPED_BODY` leaves it."""
    return build(wiki(body=DROPPED_BODY, **kw), MERGE).files[KEPT_PATH]


def blocks(text):
    return {heading: body for heading, body in sections(text)}


def update_days(text):
    return [heading.removeprefix("## Update ") for heading in blocks(text)
            if heading.startswith("## Update ")]


LEGAL = [
    pytest.param(Status.OPEN, None, NOTE, INDEX, INCIDENT_AND_LOG,
                 Status.OPEN, None, id="open-record-action"),
    pytest.param(Status.MONITORING, WINDOW, NOTE, INDEX, INCIDENT_AND_LOG,
                 Status.MONITORING, WINDOW, id="monitoring-record-action"),
    pytest.param(Status.RESOLVED, None, NOTE, RESOLVED_INDEX,
                 INCIDENT_AND_LOG, Status.RESOLVED, None,
                 id="resolved-record-action"),
    pytest.param(Status.OPEN, None, START, INDEX, INCIDENT_AND_LOG,
                 Status.MONITORING, MonitoringWindow(SIGNAL, AT, UNTIL),
                 id="open-start-monitoring"),
    pytest.param(Status.MONITORING, WINDOW, START, INDEX, INCIDENT_AND_LOG,
                 Status.MONITORING, MonitoringWindow(SIGNAL, AT, UNTIL),
                 id="monitoring-start-monitoring"),
    pytest.param(Status.MONITORING, WINDOW, EXTEND, INDEX, INCIDENT_AND_LOG,
                 Status.MONITORING, MonitoringWindow(SIGNAL, OPENED, FURTHER),
                 id="monitoring-extend-monitoring"),
    pytest.param(Status.OPEN, None, RESOLVE, INDEX, WITH_ERROR_PAGE,
                 Status.RESOLVED, None, id="open-resolve"),
    pytest.param(Status.MONITORING, WINDOW, RESOLVE, INDEX, WITH_ERROR_PAGE,
                 Status.RESOLVED, None, id="monitoring-resolve"),
    pytest.param(Status.RESOLVED, None, REOPEN, RESOLVED_INDEX, WITH_INDEX,
                 Status.OPEN, None, id="resolved-reopen"),
    pytest.param(Status.OPEN, None, MERGE, INDEX, WITH_KEPT,
                 Status.RESOLVED, None, id="open-merge"),
    pytest.param(Status.MONITORING, WINDOW, MERGE, INDEX, WITH_KEPT,
                 Status.RESOLVED, None, id="monitoring-merge"),
]

ILLEGAL = [
    pytest.param(Status.OPEN, None, EXTEND, id="open-extend-monitoring"),
    pytest.param(Status.OPEN, None, REOPEN, id="open-reopen"),
    pytest.param(Status.MONITORING, WINDOW, REOPEN, id="monitoring-reopen"),
    pytest.param(Status.RESOLVED, None, START, id="resolved-start-monitoring"),
    pytest.param(Status.RESOLVED, None, EXTEND,
                 id="resolved-extend-monitoring"),
    pytest.param(Status.RESOLVED, None, RESOLVE, id="resolved-resolve"),
    pytest.param(Status.RESOLVED, None, MERGE, id="resolved-merge"),
]


@pytest.mark.parametrize("status,window,command,index,paths,after,left", LEGAL)
def test_a_legal_transition_writes_exactly_these_files(status, window, command,
                                                       index, paths, after,
                                                       left):
    proposal = build(wiki(status, window=window, index=index), command)
    page = written(proposal)
    assert set(proposal.files) == paths
    assert page.status is after
    assert page.monitoring == left


@pytest.mark.parametrize("status,window,command", ILLEGAL)
def test_an_illegal_transition_is_refused_before_anything_is_composed(
        status, window, command):
    with pytest.raises(TransitionError) as exc:
        build(wiki(status, window=window), command)
    assert exc.value.status is status
    assert exc.value.command_type is type(command)


def test_the_case_lists_cover_every_cell_of_the_table():
    legal = {(p.values[0], type(p.values[2])) for p in LEGAL}
    illegal = {(p.values[0], type(p.values[2])) for p in ILLEGAL}
    assert legal == set(lc.TRANSITIONS)
    assert legal | illegal == {(s, t) for s in Status for t in lc.KINDS}


def test_allowed_lists_the_table_row_in_order():
    assert lc.allowed(Status.OPEN) == (RecordAction, StartMonitoring, Resolve,
                                       Merge)
    assert lc.allowed(Status.MONITORING) == (RecordAction, StartMonitoring,
                                             ExtendMonitoring, Resolve, Merge)
    assert lc.allowed(Status.RESOLVED) == (RecordAction, Reopen)


@pytest.mark.parametrize("status,window,command,index,paths,after,left", LEGAL)
def test_next_status_agrees_with_what_build_writes(status, window, command,
                                                   index, paths, after, left):
    assert lc.next_status(status, command) is after


NO_STATUS = incident().replace("status: open\n", "")


@pytest.mark.parametrize("page,expected", [
    pytest.param(incident(raw_status="opne"), "opne", id="typo"),
    pytest.param(incident(raw_status="closed"), "closed", id="off-vocabulary"),
    pytest.param(NO_STATUS, "", id="no-status-key"),
])
def test_an_off_vocabulary_status_is_refused_with_the_raw_value(page, expected):
    with pytest.raises(TransitionError) as exc:
        build({**wiki(), PATH: page}, NOTE)
    assert exc.value.status == expected


def test_a_path_the_tree_does_not_hold_is_not_a_transition_error():
    with pytest.raises(ValueError) as exc:  # noqa: PT011 — category is the assertion
        lc.build(Tree.of(wiki()), "incidents/nope.md", NOTE, ACTOR, AT)
    assert not isinstance(exc.value, TransitionError)
    assert "incidents/nope.md" in str(exc.value)


def test_extending_a_monitoring_page_with_no_readable_window_is_refused():
    files = wiki(Status.MONITORING)
    with pytest.raises(ActionMalformed) as exc:
        build(files, EXTEND)
    assert str(exc.value).startswith("window:")


@pytest.mark.parametrize("status,window,command,kind,outcome,summary", [
    (Status.OPEN, None, NOTE, "record-action", Outcome.PENDING, SUMMARY),
    (Status.OPEN, None, RecordAction(intent=INTENT, summary=SUMMARY,
                                     outcome="rejected"),
     "record-action", Outcome.REJECTED, SUMMARY),
    (Status.OPEN, None, START, "start-monitoring", Outcome.PENDING, SUMMARY),
    (Status.MONITORING, WINDOW, EXTEND, "extend-monitoring", Outcome.PENDING,
     f"extended the monitoring window to {FURTHER}"),
    (Status.OPEN, None, RESOLVE, "resolve", Outcome.SUCCEEDED, FIXED),
    (Status.RESOLVED, None, REOPEN, "reopen", Outcome.FAILED,
     "the listener failed again overnight"),
    (Status.OPEN, None, MERGE, "merge", Outcome.SUCCEEDED,
     f"duplicate of {KEPT_TARGET}"),
])
def test_one_command_becomes_one_record(status, window, command, kind, outcome,
                                        summary):
    proposal = build(wiki(status, window=window, index=RESOLVED_INDEX),
                     command)
    records = parse_actions(proposal.files[PATH]).records
    assert len(records) == 1
    assert (records[0].kind, records[0].outcome) == (kind, outcome)
    assert records[0].summary == summary
    assert records[0].at == AT
    assert records[0].actor == ACTOR.email


def test_residual_risk_becomes_a_labelled_first_paragraph_of_the_notes():
    command = Resolve(summary=FIXED, residual_risk="the host may swap again",
                      notes="watched a full day of clean connects")
    record = record_of(build(wiki(), command))
    assert record.notes == ("Residual risk: the host may swap again\n\n"
                            "watched a full day of clean connects")


def test_residual_risk_alone_is_the_whole_note():
    command = Resolve(summary=FIXED, residual_risk="the host may swap again")
    assert record_of(build(wiki(), command)).notes == (
        "Residual risk: the host may swap again")


def test_the_ticket_and_rollback_ride_into_the_record():
    command = RecordAction(intent=INTENT, summary=SUMMARY, ticket="INC-4471",
                           rollback="stop the listener and fail back",
                           evidence=(f"digests/{DB}/2026-08-30.md",))
    record = record_of(build(wiki(), command))
    assert record.ticket == "INC-4471"
    assert record.rollback == "stop the listener and fail back"
    assert [str(e) for e in record.evidence] == [f"digests/{DB}/2026-08-30.md"]


def test_the_log_line_names_the_incident_the_kind_and_the_actor():
    proposal = build(wiki(), RESOLVE)
    assert proposal.files["log.md"].endswith(
        f"[{AT}] incident — {PATH}: resolve by {ACTOR.email} — {FIXED}\n")


def test_log_md_is_created_when_the_tree_does_not_hold_it():
    proposal = build(wiki(log=None), NOTE)
    assert proposal.files["log.md"].startswith("---\ntype: log\n---\n")
    assert f"[{AT}] incident — {PATH}" in proposal.files["log.md"]


def test_resolve_moves_the_bullet_and_keeps_the_label_a_human_gave_it():
    index = build(wiki(), RESOLVE).files["index.md"]
    assert f"## {lc.INDEX_RESOLVED}\n\n{BULLET}" in index
    assert BULLET not in index.partition(f"## {lc.INDEX_RESOLVED}")[0]


def test_resolve_falls_back_to_the_page_title_when_the_index_never_listed_it():
    index = build(wiki(index=UNLISTED_INDEX), RESOLVE).files["index.md"]
    assert f"- [[{TARGET}]] — {TITLE}\n" in index


def test_reopen_moves_the_bullet_back_and_keeps_the_label():
    files = wiki(Status.RESOLVED, index=RESOLVED_INDEX)
    index = build(files, REOPEN).files["index.md"]
    assert BULLET in index.partition(f"## {lc.INDEX_RESOLVED}")[0]
    assert BULLET not in index.partition(f"## {lc.INDEX_RESOLVED}")[2]


SOLE_OPEN_INDEX = INDEX.replace(
    "- [[incidents/2026-07-12-cdb1-dataguard-transport-failure]] — "
    "Dataguard transport failures\n", "")

OLDER_BULLET = "- [[incidents/2026-06-01-cdb1-ora-600]] — an older ORA-600\n"


def test_reopening_the_only_resolved_incident_takes_the_section_away():
    files = wiki(Status.RESOLVED, index=RESOLVED_INDEX)
    index = build(files, REOPEN).files["index.md"]
    assert f"## {lc.INDEX_RESOLVED}" not in index
    assert index.endswith("- [[errors/ORA-12514]] — ORA-12514\n")


def test_reopening_one_of_two_resolved_incidents_keeps_the_section():
    files = wiki(Status.RESOLVED, index=RESOLVED_INDEX + OLDER_BULLET)
    index = build(files, REOPEN).files["index.md"]
    assert f"## {lc.INDEX_RESOLVED}\n\n{OLDER_BULLET}" in index
    assert BULLET in index.partition(f"## {lc.INDEX_RESOLVED}")[0]


def test_resolving_the_last_open_incident_keeps_the_open_section():
    index = build(wiki(index=SOLE_OPEN_INDEX), RESOLVE).files["index.md"]
    assert f"## {lc.INDEX_OPEN}\n\n## Journals" in index


def test_an_absent_index_yields_a_note_and_no_file():
    proposal = build(wiki(index=None), RESOLVE)
    assert "index.md" not in proposal.files
    assert any("index.md is absent" in note for note in proposal.notes)


def test_where_the_resolved_section_lands_in_a_realistic_index():
    fx.assert_golden("lifecycle/index_after_resolve.md",
                     build(wiki(), RESOLVE).files["index.md"], text=True)


ROW_PREFIX = f"| {DAY} | {DB} | [[{TARGET}]] | "


def test_resolve_writes_one_resolution_history_row_under_one_header():
    page = build(wiki(), RESOLVE).files[f"errors/{CODE}.md"]
    assert page.count(lc.RESOLUTION_HEAD) == 1
    assert page.count(f"## {lc.RESOLUTION_SECTION}") == 1
    assert f"{ROW_PREFIX}{FIXED} | {AT} |\n" in page


def test_the_row_cites_the_evidence_when_the_command_carries_any():
    command = Resolve(summary=FIXED,
                      evidence=(f"digests/{DB}/2026-08-30.md",
                                f"digests/{DB}/2026-08-29.md"))
    page = build(wiki(), command).files[f"errors/{CODE}.md"]
    assert (f"{ROW_PREFIX}{FIXED} | digests/{DB}/2026-08-30.md, "
            f"digests/{DB}/2026-08-29.md |\n") in page


def test_a_second_resolve_on_the_same_day_rewrites_the_row():
    files = wiki()
    files = applied(files, build(files, RESOLVE))
    files = applied(files, build(files, REOPEN, at=LATER))
    again = Resolve(summary="raised the listener queue size")
    page = applied(files, build(files, again, at=LATEST))[f"errors/{CODE}.md"]
    assert page.count(ROW_PREFIX) == 1
    assert page.count(lc.RESOLUTION_HEAD) == 1
    assert f"{ROW_PREFIX}raised the listener queue size | {LATEST} |\n" in page


def test_an_error_page_the_tree_does_not_hold_yields_a_note_and_no_file():
    proposal = build(wiki(error_page=None), RESOLVE)
    assert set(proposal.files) == WITH_INDEX
    assert proposal.notes == (f"errors/{CODE}.md is absent; no "
                              f"{lc.RESOLUTION_SECTION} row was written for "
                              f"{CODE}",)


def test_update_error_pages_off_leaves_the_error_page_alone():
    proposal = build(wiki(), Resolve(summary=FIXED, update_error_pages=False))
    assert set(proposal.files) == WITH_INDEX
    assert proposal.notes == ()


def test_an_incident_linking_two_codes_writes_a_row_on_each():
    files = wiki(codes=(CODE, "ORA-12514"))
    files["errors/ORA-12514.md"] = ERROR_PAGE.replace(CODE, "ORA-12514")
    proposal = build(files, RESOLVE)
    assert set(proposal.files) == WITH_ERROR_PAGE | {"errors/ORA-12514.md"}
    for rel in (f"errors/{CODE}.md", "errors/ORA-12514.md"):
        assert ROW_PREFIX in proposal.files[rel]


@pytest.mark.parametrize("status,window,command,index,paths,after,left", LEGAL)
def test_the_same_inputs_build_the_same_proposal(status, window, command,
                                                 index, paths, after, left):
    files = wiki(status, window=window, index=index)
    first, second = build(files, command), build(files, command)
    assert first == second
    assert first.files == second.files
    assert first.message == second.message


@pytest.mark.parametrize("status,window,command", [
    pytest.param(Status.OPEN, None, NOTE, id="record-action"),
    pytest.param(Status.OPEN, None, START, id="start-monitoring"),
    pytest.param(Status.MONITORING, WINDOW, EXTEND, id="extend-monitoring"),
])
def test_replaying_a_status_preserving_command_writes_nothing(status, window,
                                                              command):
    files = wiki(status, window=window)
    first = build(files, command)
    assert build(applied(files, first), command, at=AT).files == {}


def test_replaying_a_resolve_against_the_same_base_rebuilds_the_same_bytes():
    files = wiki()
    assert build(files, RESOLVE).files == build(files, RESOLVE).files


def test_the_message_names_the_kind_the_slug_and_the_summary_on_one_line():
    message = build(wiki(), RESOLVE).message
    assert message == f"incident: resolve {TARGET.split('/')[1]} — {FIXED}"
    assert "\n" not in message


def test_a_long_summary_is_cut_to_sixty_characters_in_the_message():
    long = "restarted the listener, rebuilt tnsnames, and watched the standby"
    message = build(wiki(), Resolve(summary=long)).message
    assert message.endswith(long[:60])
    assert "\n" not in message


def test_the_proposal_carries_the_base_and_the_actor_it_was_built_from():
    proposal = lc.build(Tree.of(wiki(), "9" * 40), PATH, NOTE, ACTOR, AT)
    assert proposal.base == "9" * 40
    assert proposal.actor is ACTOR


def test_the_command_kinds_are_the_action_kinds():
    from dbwiki.incidents import ACTION_KINDS
    assert set(lc.KINDS.values()) == set(ACTION_KINDS)
    assert len(lc.KINDS) == len(ACTION_KINDS)


def test_a_merge_writes_a_record_on_both_pages():
    proposal = build(wiki(), MERGE)
    dropped, kept = record_of(proposal), kept_record_of(proposal)
    assert (dropped.kind, dropped.summary) == ("merge",
                                               f"duplicate of {KEPT_TARGET}")
    assert (kept.kind, kept.summary) == ("record-action",
                                         f"merged {TARGET} into this page")
    assert dropped.at == kept.at == AT
    assert dropped.actor == kept.actor == ACTOR.email
    assert dropped.outcome is kept.outcome is Outcome.SUCCEEDED


def test_the_dropped_page_resolves_with_no_window_and_names_the_kept_page():
    proposal = build(wiki(Status.MONITORING, window=WINDOW), MERGE)
    page = written(proposal)
    assert page.status is Status.RESOLVED
    assert page.monitoring is None
    assert record_of(proposal).notes == f"Superseded by [[{KEPT_TARGET}]]."


def test_the_operators_note_rides_under_the_supersede_line():
    command = Merge(into=KEPT_SLUG, notes="closed the duplicate SR too")
    assert record_of(build(wiki(), command)).notes == (
        f"Superseded by [[{KEPT_TARGET}]].\n\nclosed the duplicate SR too")


def test_the_kept_page_keeps_its_own_status_and_its_own_window():
    proposal = build(wiki(kept=kept_page(Status.MONITORING, window=WINDOW)),
                     MERGE)
    page = read_incident(proposal.files[KEPT_PATH], KEPT_PATH)
    assert page.status is Status.MONITORING
    assert page.monitoring == WINDOW
    assert page.updated == AT
    record = kept_record_of(proposal)
    assert record.status_after is Status.MONITORING
    assert record.window == WINDOW
    assert record.notes == (f"Folded [[{TARGET}]] in as a duplicate of the "
                            f"same db and day.")


def test_only_the_dropped_page_reaches_log_md():
    proposal = build(wiki(), MERGE)
    added = [line for line in
             proposal.files["log.md"].removeprefix(LOG).splitlines() if line]
    assert added == [f"[{AT}] incident — {PATH}: merge by {ACTOR.email} — "
                     f"duplicate of {KEPT_TARGET}"]


def test_merge_moves_the_index_bullet_exactly_as_resolve_moves_it():
    index = build(wiki(), MERGE).files["index.md"]
    assert index == build(wiki(), RESOLVE).files["index.md"]
    assert f"## {lc.INDEX_RESOLVED}\n\n{BULLET}" in index
    assert BULLET not in index.partition(f"## {lc.INDEX_RESOLVED}")[0]


def test_merge_leaves_the_linked_error_pages_resolution_history_alone():
    """A duplicate folding away is not a confirmed fix. Only a resolve writes
    a `## Resolution history` row."""
    proposal = build(wiki(), MERGE)
    assert set(proposal.files) == WITH_KEPT
    assert lc.RESOLUTION_SECTION not in proposal.files[KEPT_PATH]
    assert proposal.notes == ()


def test_a_second_merge_of_the_same_pair_is_refused_by_the_table():
    files = wiki()
    files = applied(files, build(files, MERGE))
    with pytest.raises(TransitionError) as exc:
        build(files, MERGE, at=LATER)
    assert exc.value.status is Status.RESOLVED
    assert exc.value.command_type is Merge


@pytest.mark.parametrize("command,files,into", [
    pytest.param(Merge(into=SLUG), wiki(), PATH, id="into-itself"),
    pytest.param(MERGE, wiki(kept=None), KEPT_PATH, id="no-such-page"),
    pytest.param(MERGE, wiki(kept=kept_page(db="cdb1")), KEPT_PATH,
                 id="another-db"),
    pytest.param(MERGE, wiki(kept=kept_page(raw_status="opne")), KEPT_PATH,
                 id="off-vocabulary-status"),
])
def test_a_pair_that_is_not_a_duplicate_pair_is_refused_naming_both_pages(
        command, files, into):
    with pytest.raises(MergeRefused) as exc:
        build(files, command)
    assert (exc.value.path, exc.value.into) == (PATH, into)
    assert PATH in str(exc.value) and into in str(exc.value)


def test_a_merge_refusal_is_a_value_error_and_not_a_transition_error():
    """`incident_cli` maps one `except ValueError` to exit 2, and the portal
    tells a refused transition from a refused pair by the type."""
    with pytest.raises(ValueError) as exc:  # noqa: PT011 — category is the assertion
        build(wiki(kept=None), MERGE)
    assert not isinstance(exc.value, TransitionError)


def test_evidence_the_kept_page_lacks_is_carried_once():
    page = merged_kept()
    line = (f"- {DROPPED_DAY}: digests/{DB}/{DROPPED_DAY}.md — the day only "
            f"the duplicate cites")
    assert page.count(line) == 1
    assert line in blocks(page)["## Evidence"]


def test_evidence_the_kept_page_already_cites_keeps_the_kept_pages_wording():
    page = merged_kept()
    assert (f"- {KEPT_DAY}: digests/{DB}/{KEPT_DAY}.md — the kept page's own "
            f"wording") in page
    assert "the duplicate worded that day its own way" not in page


def test_two_dropped_lines_for_one_day_and_digest_carry_as_one():
    """The keys are recomputed as the lines fold, so a duplicate page citing
    the same digest twice does not hand the kept page two lines."""
    page = build(wiki(body=TWICE_BODY), MERGE).files[KEPT_PATH]
    assert blocks(page)["## Evidence"].count(f"- {DROPPED_DAY}: ") == 1
    assert "and cited it again, reworded" not in page


def test_an_update_section_for_a_day_the_kept_page_lacks_is_carried_verbatim():
    assert (f"## Update {DROPPED_DAY}\n\nthe duplicate's account of a day the "
            f"kept page never wrote up\n") in merged_kept()


def test_an_update_section_for_a_day_the_kept_page_has_is_not_carried():
    page = build(wiki(body=SAME_DAY_BODY), MERGE).files[KEPT_PATH]
    assert page.count(f"## Update {KEPT_DAY}") == 1
    assert "the duplicate's rewording" not in page


@pytest.mark.parametrize("day,order", [
    pytest.param(EARLY_DAY, [EARLY_DAY, KEPT_DAY, LATE_DAY], id="before-all"),
    pytest.param(DROPPED_DAY, [KEPT_DAY, DROPPED_DAY, LATE_DAY], id="between"),
    pytest.param(LATEST_DAY, [KEPT_DAY, LATE_DAY, LATEST_DAY], id="after-all"),
])
def test_a_carried_update_lands_in_day_order_among_the_kept_pages_own(day,
                                                                      order):
    """The run of `## Update <day>` sections is the page's chronology, so an
    appended one would make the merged page misread."""
    page = build(wiki(body=update_body(day)), MERGE).files[KEPT_PATH]
    assert update_days(page) == order


def test_a_kept_page_with_no_updates_takes_the_carried_one_at_the_end():
    files = wiki(body=DROPPED_BODY,
                 kept=kept_page(body=f"## Evidence\n\n- {KEPT_DAY}: "
                                     f"digests/{DB}/{KEPT_DAY}.md"))
    page = build(files, MERGE).files[KEPT_PATH]
    assert [heading for heading, _ in sections(page)] == [
        "## Evidence", "## Errors", f"## Update {DROPPED_DAY}",
        f"## Action {AT}"]


def test_the_carried_bytes_land_before_the_kept_pages_own_record():
    page = merged_kept()
    assert [heading for heading, _ in sections(page)] == [
        "## Evidence", f"## Update {KEPT_DAY}", f"## Update {DROPPED_DAY}",
        f"## Update {LATE_DAY}", "## Errors", f"## Action {AT}"]


LINT_INDEX = f"""---
type: index
---

# Logbook

## Open incidents

- [[{TARGET}]] — {LABEL}
- [[{KEPT_TARGET}]] — {KEPT_TITLE}

## Error classes

- [[errors/{CODE}]] — {CODE}
"""


def on_disk(root, files):
    """One `Tree.of` dict written out as a wiki, plus the digests its pages
    cite: `lint_wiki` walks a directory, and `digest-missing` reads it."""
    for rel, text in files.items():
        page = root / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text)
    for day in (KEPT_DAY, DROPPED_DAY):
        (root / "digests" / DB).mkdir(parents=True, exist_ok=True)
        (root / "digests" / DB / f"{day}.md").write_text(f"# {DB} {day}\n")
    return root


def test_the_bytes_a_merge_writes_lint_clean(tmp_path):
    """Lint runs in preview, before publication, so what a merge writes has to
    pass it: `action-malformed` over both new records, and `wikilink-broken`
    over the two page links their notes carry."""
    files = wiki(body=DROPPED_BODY, index=LINT_INDEX)
    assert lint_wiki(on_disk(tmp_path / "before", files)) == []
    merged = applied(files, build(files, MERGE))
    assert lint_wiki(on_disk(tmp_path / "after", merged)) == []


def test_a_second_act_in_the_same_second_is_refused_not_overwritten():
    """Issue 13: a scripted record-action then resolve in one second used to
    leave only the resolve on the page while `log.md` listed both."""
    files = applied(wiki(), build(wiki(), NOTE))
    with pytest.raises(ActionMalformed, match=r"\Aat:"):
        build(files, RESOLVE)
    assert [r.kind for r in parse_actions(files[PATH]).records] == [
        "record-action"]


def test_the_same_act_replayed_at_the_same_second_changes_nothing():
    files = applied(wiki(), build(wiki(), NOTE))
    assert build(files, NOTE).files == {}
