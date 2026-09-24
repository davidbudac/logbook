"""The action-record codec: the bytes an `## Action` section may be, and every
shape it refuses.

`render_action` and `parse_action` are one contract read from both ends, so
the round trip is pinned as a matrix over the field space rather than as a
handful of hand-written examples. The refusals get one case per rule,
matching only the field name the message opens with. Last comes the collision
this whole section shape exists to survive: an ingest rewriting the same page
collapses blank-line runs across every byte of it, including a record's notes.
"""

import re
from itertools import product

import pytest

from dbwiki.incidents import (ACTION_KINDS, MAX_PATTERN, ActionCollision,
                              ActionMalformed, ActionRecord, Actions,
                              ErrorAbsent, EventPresent, EvidenceRef,
                              FlowResumed, Manual,
                              MonitoringWindow, Outcome, Status, append_action,
                              parse_action, parse_actions, render_action,
                              set_status, signal_from_yaml)
from dbwiki.pagetext import day_block_replace, frontmatter, sections

AT = "2026-08-30T14:22:10Z"
START = "2026-08-30T14:30:00Z"
UNTIL = "2026-08-31T14:30:00Z"
UPDATED = "2026-08-30T14:35:00Z"
LATER = "2026-08-31T09:00:00Z"
ACTOR = "dba@example.com"
INTENT = "stop the standby gap from growing"
SUMMARY = "restarted the apply process on cdb1"

PAGE = ("---\ntype: incident\nstatus: open\ndb: cdb1\n"
        "opened: 2026-08-05T09:00:00Z\n---\n\n# ORA-00600 on cdb1\n")
BARE_PAGE = "---\ntype: incident\ndb: cdb1\n---\n\n# ORA-00600 on cdb1\n"

SIGNALS = (ErrorAbsent("ORA-00600"),
           EventPresent("the DBA's grep: media recovery applied"),
           FlowResumed("cdb1 alert log"),
           Manual("watch the gap over the night, then decide"))


def record_with(**overrides) -> ActionRecord:
    return ActionRecord(**({"at": AT, "kind": "record-action", "actor": ACTOR,
                            "intent": INTENT, "summary": SUMMARY,
                            "status_after": Status.OPEN} | overrides))


def block_with(**fields) -> str:
    values = {"kind": "record-action", "actor": ACTOR, "intent": INTENT,
              "summary": SUMMARY, "status_after": "open"} | fields
    return "".join(f"{key}: {value}\n" for key, value in values.items()
                   if value is not None)


def action_section(block: str, at: str = AT) -> str:
    return f"## Action {at}\n\n```yaml\n{block}```\n"


def fm_lines(text: str) -> list[str]:
    return text.partition("---\n")[2].partition("\n---\n")[0].split("\n")


def page_body(text: str) -> str:
    return text.partition("---\n")[2].partition("\n---\n")[2]


STATUS_AFTER = {"record-action": Status.OPEN,
                "start-monitoring": Status.MONITORING,
                "extend-monitoring": Status.MONITORING,
                "resolve": Status.RESOLVED,
                "reopen": Status.OPEN,
                "merge": Status.RESOLVED}

TICKETS = ("", "INC-4471")
ROLLBACKS = ("", "restore the service to cdb1 and re-enable the apply")
EVIDENCE = ((), ("digests/cdb1/2026-08-30.md", "digests/cdb1/2026-08-29.json"))
NOTES = ("",
         "the gap closed within the hour",
         "réplica secundaria al día; 日本語のログも確認済み ✅",
         "the standby caught up.\n\nkept the trace file for tomorrow.")


def round_trip_records():
    """Every shape a record can render as, crossed: signal variant, and each
    optional present and absent. `kind` and `outcome` walk the shapes instead
    of joining the cross product, because the codec never reads either one;
    the coverage test below is what keeps that honest, and it holds every pair
    of them.

    They walk at different rates so every pair appears whatever the two
    lengths are. Stepping both off the same counter covered all of them only
    while the counts were coprime, and a sixth `kind` ended that.

    A record the codec cannot round-trip is a page a human wrote and the wiki
    then silently rewrote."""
    outcomes = tuple(Outcome)
    shapes = product((None,) + SIGNALS, TICKETS, ROLLBACKS, EVIDENCE, NOTES)
    for i, (signal, ticket, rollback, evidence, notes) in enumerate(shapes):
        kind = ACTION_KINDS[i % len(ACTION_KINDS)]
        yield ActionRecord(
            at=AT, kind=kind, actor=ACTOR, intent=INTENT, summary=SUMMARY,
            status_after=STATUS_AFTER[kind], ticket=ticket,
            outcome=outcomes[i // len(ACTION_KINDS) % len(outcomes)],
            rollback=rollback,
            window=None if signal is None else MonitoringWindow(signal, START,
                                                                UNTIL),
            evidence=evidence, notes=notes)


def record_id(record: ActionRecord) -> str:
    signal = ("no-window" if record.window is None
              else type(record.window.signal).__name__)
    carried = "".join(name for name, value in (
        ("t", record.ticket), ("r", record.rollback),
        ("e", record.evidence), ("n", record.notes)) if value)
    return f"{record.kind}-{record.outcome}-{signal}-{carried or 'bare'}"


ROUND_TRIP = tuple(round_trip_records())


@pytest.mark.parametrize("record", ROUND_TRIP, ids=record_id)
def test_a_rendered_record_parses_back_to_itself_and_to_the_same_bytes(record):
    once = render_action(record)
    assert parse_action(once) == record
    assert render_action(parse_action(once)) == once


def test_the_round_trip_matrix_covers_every_kind_outcome_and_signal():
    assert ({(r.kind, r.outcome) for r in ROUND_TRIP}
            == set(product(ACTION_KINDS, Outcome)))
    assert {None if r.window is None else r.window.signal
            for r in ROUND_TRIP} == {None, *SIGNALS}


def test_a_merge_records_wiki_link_survives_the_round_trip():
    """The matrix crosses every kind with every optional, `merge` included,
    but its notes are plain sentences. What a merge actually writes is a
    `[[page]]` link saying where the incident went, and losing that link would
    leave the dropped page pointing nowhere."""
    record = record_with(kind="merge", status_after=Status.RESOLVED,
                         outcome=Outcome.SUCCEEDED,
                         notes="Superseded by [[incidents/2026-08-30-cdb1]].")
    assert parse_action(render_action(record)) == record


def test_notes_are_stripped_on_construction_so_the_round_trip_holds():
    record = record_with(notes="  \n  the gap closed within the hour  \n\n ")
    assert record.notes == "the gap closed within the hour"
    assert parse_action(render_action(record)) == record


@pytest.mark.parametrize("field, value", [
    ("at", "2026-08-30 14:22:10"),
    ("at", "2026-08-30T14:22:10"),
    ("at", "2026-08-30T14:22:10Z "),
    ("at", ""),
    ("kind", "record_action"),
    ("kind", "Record-Action"),
    ("kind", ""),
    ("actor", ""),
    ("actor", "   "),
    ("intent", ""),
    ("summary", "\t"),
])
def test_a_record_refuses_a_field_it_cannot_mean(field, value):
    with pytest.raises(ActionMalformed, match=rf"\A{field}:"):
        record_with(**{field: value})


@pytest.mark.parametrize("break_", ["\n", "\r"])
@pytest.mark.parametrize("field", ["actor", "intent", "summary", "ticket",
                                   "rollback"])
def test_a_record_refuses_a_second_line_in_a_scalar_field(field, break_):
    with pytest.raises(ActionMalformed, match=rf"\A{field}:"):
        record_with(**{field: f"first{break_}second"})


@pytest.mark.parametrize("break_", ["\n", "\r"])
def test_a_record_refuses_a_second_line_in_an_evidence_entry(break_):
    """The digest-path rule already refuses this, because `EVIDENCE_RE`
    anchors on `\\Z` rather than `$` and matches from the start. Kept as its
    own case because the stake is not the path form: a second line in an
    entry is a second line in the yaml block, and the page it would write is
    one no reader can parse back."""
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        record_with(evidence=("digests/cdb1/2026-08-30.md",
                              f"digests/cdb1/2026-08-29.md{break_}and more"))


@pytest.mark.parametrize("raw", [
    "errors/NOPE.md",
    "digests/nope.md",
    "digests/cdb1/2026-08-30.txt",
    "digests/cdb1/30-08-2026.md",
    "see digests/cdb1/2026-08-30.md",
])
def test_a_record_refuses_evidence_that_is_not_a_digest_path(raw):
    """An `errors/` page is a conclusion the wiki drew, not something the
    compactor observed, so it can never back an action."""
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        record_with(evidence=(raw,))


def test_a_record_refuses_an_evidence_day_that_never_happened():
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        record_with(evidence=("digests/cdb1/2026-02-30.md",))


def test_an_evidence_ref_built_by_hand_is_held_to_the_same_form():
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        EvidenceRef("cdb1/nested", "2026-08-30")


def test_evidence_parses_into_its_database_and_its_day_however_it_arrives():
    """The constructor takes raw operator strings and refs a caller parsed
    already; after construction there is only the ref."""
    record = record_with(evidence=("digests/cdb1/2026-08-30.md",
                                   EvidenceRef("cdb1", "2026-08-29", "json")))
    assert record.evidence == (EvidenceRef("cdb1", "2026-08-30", "md"),
                               EvidenceRef("cdb1", "2026-08-29", "json"))


def test_evidence_renders_as_the_bare_path_and_parses_back_to_the_ref():
    record = record_with(evidence=("digests/cdb1/2026-08-30.md",
                                   "digests/cdb1/2026-08-29.json"))
    section = render_action(record)
    assert ("evidence:\n- digests/cdb1/2026-08-30.md\n"
            "- digests/cdb1/2026-08-29.json\n") in section
    assert parse_action(section) == record


@pytest.mark.parametrize("notes", [
    "the fix landed\n## Update 2026-08-30\nand then it recurred",
    "## Action 2026-08-30T15:00:00Z",
    "the fix landed\n```\nand then it recurred",
    "the fix landed\n```yaml\nand then it recurred",
    "the fix landed\n    ```\nand then it recurred",
    "the fix landed\n\n\nand then it recurred",
])
def test_a_record_refuses_notes_that_would_rewrite_the_page(notes):
    with pytest.raises(ActionMalformed, match=r"\Anotes:"):
        record_with(notes=notes)


@pytest.mark.parametrize("field, start, until", [
    ("start", "2026-08-30 14:30:00", UNTIL),
    ("start", "", UNTIL),
    ("until", START, "2026-08-31T14:30"),
    ("until", START, "yesterday"),
    ("start", "2026-02-30T00:00:00Z", UNTIL),
    ("until", START, "2026-09-31T00:00:00Z"),
    ("until", START, "2026-08-31T24:30:00Z"),
])
def test_a_window_refuses_a_timestamp_that_is_not_iso_z(field, start, until):
    with pytest.raises(ActionMalformed, match=rf"\A{field}:"):
        MonitoringWindow(Manual("watch the gap"), start, until)


@pytest.mark.parametrize("until", [START, "2026-08-30T14:29:59Z"])
def test_a_window_refuses_an_until_that_does_not_follow_its_start(until):
    with pytest.raises(ActionMalformed, match=r"\Auntil:"):
        MonitoringWindow(Manual("watch the gap"), START, until)


@pytest.mark.parametrize("data", [
    {"kind": "error_gone", "code": "ORA-00600"},
    {"kind": "", "code": "ORA-00600"},
    {"code": "ORA-00600"},
])
def test_a_signal_refuses_a_kind_outside_the_union(data):
    with pytest.raises(ActionMalformed, match=r"\Awindow:"):
        signal_from_yaml(data)


@pytest.mark.parametrize("data", [
    {"kind": "error_absent"},
    {"kind": "error_absent", "code": ""},
    {"kind": "error_absent", "code": None},
    {"kind": "event_present", "pattern": ""},
    {"kind": "flow_resumed", "source": ""},
    {"kind": "manual", "description": ""},
    {"kind": "manual", "code": "ORA-00600"},
])
def test_a_signal_refuses_a_missing_or_empty_payload(data):
    with pytest.raises(ActionMalformed, match=r"\Awindow:"):
        signal_from_yaml(data)


@pytest.mark.parametrize("first_line", [
    "## Update 2026-08-30",
    "## Action",
    "# Action 2026-08-30T14:22:10Z",
    "some prose",
])
def test_a_section_whose_first_line_is_not_an_action_heading_is_malformed(
        first_line):
    with pytest.raises(ActionMalformed, match=r"\Aat:"):
        parse_action(first_line + "\n\n```yaml\n" + block_with() + "```\n")


def test_an_action_heading_whose_timestamp_is_not_iso_z_is_malformed():
    with pytest.raises(ActionMalformed, match=r"\Aat:"):
        parse_action(action_section(block_with(), at="2026-08-30"))


@pytest.mark.parametrize("heading", ["## Action items", "## Action plan",
                                     "## Actions"])
def test_a_prose_heading_that_opens_with_action_is_not_a_record(heading):
    page = PAGE + f"\n{heading}\n\n- chase the SR\n"
    assert parse_actions(page) == Actions()


@pytest.mark.parametrize("section", [
    f"## Action {AT}\n\n" + block_with(),
    f"## Action {AT}\n\n```yaml\n" + block_with(),
    f"## Action {AT}\n\n```yaml\njust a sentence\n```\n",
    f"## Action {AT}\n\n```yaml\nkind: [record-action\n```\n",
], ids=["no-fence", "unclosed-fence", "not-a-mapping", "not-valid-yaml"])
def test_a_section_without_a_readable_yaml_block_is_malformed(section):
    with pytest.raises(ActionMalformed):
        parse_action(section)


def test_a_key_outside_the_schema_is_malformed_and_names_that_key():
    with pytest.raises(ActionMalformed, match=r"\Asumary:"):
        parse_action(action_section(block_with(sumary="a typo")))


def test_the_at_key_belongs_in_the_heading_and_not_in_the_yaml_block():
    with pytest.raises(ActionMalformed, match=r"\Aat:"):
        parse_action(action_section(block_with(at=AT)))


@pytest.mark.parametrize("field, block", [
    ("status_after", block_with(status_after="closed")),
    ("status_after", block_with(status_after=None)),
    ("outcome", block_with(outcome="maybe")),
    ("evidence", block_with(evidence="digests/cdb1/2026-08-30.md")),
    ("window", block_with(window="2026-08-30")),
    ("window", block_with(window="{kind: error_gone, code: ORA-00600}")),
    ("window", block_with(window="{kind: manual, start: '" + START
                                 + "', until: '" + UNTIL + "'}")),
    ("until", block_with(window="{kind: manual, description: watch it, "
                                "start: '" + START + "', until: 'nope'}")),
])
def test_parse_action_names_the_field_it_cannot_read(field, block):
    with pytest.raises(ActionMalformed, match=rf"\A{field}:"):
        parse_action(action_section(block))


def test_parse_actions_returns_the_records_in_page_order():
    first = record_with(at="2026-08-30T09:00:00Z", summary="killed session 412")
    second = record_with(at="2026-08-30T10:00:00Z", summary="restarted apply",
                         status_after=Status.MONITORING)
    page = append_action(append_action(PAGE, first), second)
    assert parse_actions(page) == Actions((first, second))


def test_parse_actions_collects_a_bad_section_instead_of_raising():
    good = record_with(at="2026-08-30T09:00:00Z")
    page = append_action(PAGE, good) + (
        "\n## Action 2026-08-30T10:00:00Z\n\nProse, no fence.\n")
    actions = parse_actions(page)
    assert actions.records == (good,)
    assert [p.heading for p in actions.problems] == [
        "## Action 2026-08-30T10:00:00Z"]
    assert actions.problems[0].message


def test_parse_actions_ignores_every_section_that_is_not_an_action():
    page = (PAGE + "\n## Evidence\n\n- digests/2026-08-30-cdb1.md\n"
            "\n## Update 2026-08-30\n\nthe ingest wrote this\n")
    assert parse_actions(page) == Actions()
    record = record_with()
    assert parse_actions(append_action(page, record)).records == (record,)


def test_append_action_adds_the_section_to_a_page_that_has_none():
    record = record_with(notes="the gap closed within the hour")
    assert append_action(PAGE, record) == (PAGE.rstrip("\n") + "\n\n"
                                           + render_action(record))


def test_append_action_twice_is_byte_identical():
    once = append_action(PAGE, record_with())
    assert append_action(once, record_with()) == once


def test_a_different_record_with_the_same_at_is_refused_not_overwritten():
    """Issue 13: `at` is the section's identity, so a second, different act
    in the same second used to replace the first on the page while `log.md`
    kept both. The page is the audit record; it refuses instead."""
    first = record_with(summary="restarted the apply process")
    second = record_with(summary="failed the service over to the standby",
                         outcome=Outcome.FAILED, notes="the SR has the trace.")
    once = append_action(PAGE, first)
    with pytest.raises(ActionCollision, match=r"\Aat:") as info:
        append_action(once, second)
    assert isinstance(info.value, ActionMalformed)
    assert parse_actions(once).records == (first,)


def test_replaying_the_same_record_onto_a_page_holding_it_converges():
    first = record_with(notes="the gap held.")
    once = append_action(append_action(PAGE, first),
                         record_with(at=LATER, summary="checked again"))
    assert append_action(once, first) == once


def test_a_hand_spaced_action_heading_is_rewritten_in_place():
    record = record_with()
    hand_written = PAGE + f"\n##   Action   {AT}\n\nno yaml block at all\n"
    assert append_action(hand_written, record) == append_action(PAGE, record)


def test_two_records_with_different_timestamps_both_survive():
    first = record_with(at="2026-08-30T09:00:00Z", notes="the gap held.")
    second = record_with(at="2026-08-30T10:00:00Z")
    page = append_action(append_action(PAGE, first), second)
    assert render_action(first) in page
    assert render_action(second) in page
    assert parse_actions(page).records == (first, second)


def test_set_status_appends_the_three_keys_to_a_page_that_has_none():
    window = MonitoringWindow(ErrorAbsent("ORA-00600"), START, UNTIL)
    out = set_status(BARE_PAGE, Status.MONITORING, updated=UPDATED,
                     monitoring=window)
    assert fm_lines(out) == ["type: incident", "db: cdb1",
                             "status: monitoring", f"updated: {UPDATED}",
                             f"monitoring: {window.to_frontmatter()}"]
    assert page_body(out) == page_body(BARE_PAGE)


def test_set_status_replaces_the_keys_a_page_has_at_their_own_position():
    window = MonitoringWindow(ErrorAbsent("ORA-00600"), START, UNTIL)
    once = set_status(PAGE, Status.MONITORING, updated=UPDATED,
                      monitoring=window)
    later = MonitoringWindow(Manual("watch the gap"), START, LATER)
    out = set_status(once, Status.OPEN, updated=LATER, monitoring=later)
    assert fm_lines(out) == ["type: incident", "status: open", "db: cdb1",
                             "opened: 2026-08-05T09:00:00Z",
                             f"updated: {LATER}",
                             f"monitoring: {later.to_frontmatter()}"]
    assert page_body(out) == page_body(PAGE)


def test_set_status_deletes_the_window_when_it_is_called_without_one():
    window = MonitoringWindow(ErrorAbsent("ORA-00600"), START, UNTIL)
    once = set_status(PAGE, Status.MONITORING, updated=UPDATED,
                      monitoring=window)
    out = set_status(once, Status.RESOLVED, updated=LATER)
    assert fm_lines(out) == ["type: incident", "status: resolved", "db: cdb1",
                             "opened: 2026-08-05T09:00:00Z",
                             f"updated: {LATER}"]
    assert "monitoring" not in out
    assert page_body(out) == page_body(PAGE)


def test_set_status_twice_is_byte_identical():
    window = MonitoringWindow(ErrorAbsent("ORA-00600"), START, UNTIL)
    once = set_status(PAGE, Status.MONITORING, updated=UPDATED,
                      monitoring=window)
    assert set_status(once, Status.MONITORING, updated=UPDATED,
                      monitoring=window) == once


@pytest.mark.parametrize("signal", SIGNALS, ids=lambda s: type(s).__name__)
def test_a_window_written_into_frontmatter_reads_back_equal(signal):
    window = MonitoringWindow(signal, START, UNTIL)
    page = set_status(PAGE, Status.MONITORING, updated=UPDATED,
                      monitoring=window)
    read = MonitoringWindow.from_frontmatter(frontmatter(page)["monitoring"])
    assert read == window


def test_a_window_whose_timestamps_were_written_unquoted_reads_as_no_window():
    page = ("---\ntype: incident\nstatus: monitoring\ndb: cdb1\n"
            "monitoring: {kind: 'manual', description: 'watch the gap', "
            + f"start: {START}, until: {UNTIL}" + "}\n---\n\n# T\n")
    assert frontmatter(page)["monitoring"]["kind"] == "manual"
    assert MonitoringWindow.from_frontmatter(
        frontmatter(page)["monitoring"]) is None


@pytest.mark.parametrize("value", [
    None,
    "monitoring",
    ["kind", "manual"],
    {"kind": "manual"},
    {"kind": "error_gone", "code": "ORA-00600", "start": START, "until": UNTIL},
    {"kind": "manual", "description": "watch it", "start": START},
    {"kind": "manual", "description": "watch it", "start": UNTIL,
     "until": START},
])
def test_an_unreadable_monitoring_value_reads_as_no_window(value):
    assert MonitoringWindow.from_frontmatter(value) is None


def test_a_pattern_at_the_length_limit_is_still_a_signal():
    assert EventPresent("a" * MAX_PATTERN).pattern == "a" * MAX_PATTERN


def test_a_pattern_one_character_over_the_limit_is_refused():
    with pytest.raises(ActionMalformed, match=r"\Apattern:"):
        EventPresent("a" * (MAX_PATTERN + 1))


def test_an_over_long_pattern_in_frontmatter_reads_as_no_window():
    """Fail shut: an unreadable window on a `monitoring` page is what lint's
    `incident-status-invalid` rule reports, so the operator hears about the
    pattern rather than the page quietly monitoring nothing."""
    assert MonitoringWindow.from_frontmatter(
        {"kind": "event_present", "pattern": "a" * (MAX_PATTERN + 1),
         "start": START, "until": UNTIL}) is None


DAY = "2026-08-30"
JOURNAL_SUMMARY = "the standby gap on cdb1"


def test_an_ingest_rewrite_of_the_page_leaves_every_action_byte_for_byte():
    """`day_block_replace` collapses runs of three newlines page-wide on its
    match path, so a blank line inside a record's notes is exactly what a
    same-day ingest could silently rewrite."""
    update_re = re.compile(rf"\A## Update {re.escape(DAY)}\s*\Z")
    journal_re = re.compile(rf"\A## {re.escape(DAY)} — ")
    journal_head = f"## {DAY} — {JOURNAL_SUMMARY}"
    first = record_with(at="2026-08-30T09:00:00Z", summary="killed session 412",
                        notes="the standby caught up.\n\nkept the trace file.")
    second = record_with(at="2026-08-30T10:00:00Z", summary="restarted apply",
                         status_after=Status.MONITORING,
                         window=MonitoringWindow(ErrorAbsent("ORA-00600"),
                                                 START, UNTIL),
                         notes="watching the alert log until tomorrow.")
    page = (PAGE + f"\n## Update {DAY}\n\nthe first ingest wrote this\n"
            + f"\n{journal_head}\n\nthe first journal entry\n")
    page = append_action(append_action(page, first), second)

    out = day_block_replace(
        page, update_re, f"\n## Update {DAY}\n\nthe second ingest wrote this\n")
    out = day_block_replace(
        out, journal_re, f"\n{journal_head}\n\nthe second journal entry\n")

    assert parse_actions(out) == Actions((first, second))
    assert render_action(first) in out
    assert render_action(second) in out
    assert [heading for heading, _ in sections(out)] == [
        f"## Update {DAY}", journal_head,
        "## Action 2026-08-30T09:00:00Z", "## Action 2026-08-30T10:00:00Z"]
    assert "the second ingest wrote this" in out
    assert "the second journal entry" in out
    assert "the first ingest wrote this" not in out


EXCERPT = ("## Evidence\n\n```text\nORA-00600 [kdsgrp1] on cdb1\n\n\n"
           "ORA-00600 [kdsgrp1] again, four hours later\n```\n\nkept verbatim\n")


def test_replaying_a_record_leaves_a_fenced_excerpt_byte_identical():
    """Going through `day_block_replace` collapsed `\\n{3,}` across the whole
    page on its match path, so a replay rewrote an excerpt it never named.
    `ActionRecord` can police its own notes; it cannot police the page."""
    page = PAGE + "\n" + EXCERPT
    once = append_action(page, record_with())
    assert EXCERPT in once
    assert append_action(once, record_with()) == once
    assert EXCERPT in append_action(once, record_with())


def test_a_record_between_two_sections_is_replaced_in_place():
    """In place, and only over a section that is not already a different
    record (issue 13): here a half-written one the operator is re-recording."""
    page = (PAGE + "\n## Action 2026-08-30T09:00:00Z\n\nwrote this by hand\n"
            + "\n## Evidence\n\n- digests/2026-08-30-cdb1.md\n")
    again = append_action(page, record_with(at="2026-08-30T09:00:00Z",
                                            summary="a different summary"))
    assert again.endswith("## Evidence\n\n- digests/2026-08-30-cdb1.md\n")
    assert [h for h, _ in sections(again)] == [h for h, _ in sections(page)]
    assert parse_actions(again).records[0].summary == "a different summary"
