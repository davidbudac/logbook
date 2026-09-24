"""Issue 12 (verification 2026-09-23): what may reach an incident page.

A value that looks like an instant but is not one (`2026-02-30T10:00:00Z`),
or a value that looks like one line but carries a character YAML and
`str.splitlines` break on, was written into the page and then broke every
reader of it: the queue, the daily page and lint. Each boundary now refuses
such values by name, and the readers treat whatever YAML cannot construct as
malformed rather than crashing on it."""

import random

import pytest

from dbwiki import evidence_ref, incident_action, lifecycle, lint, pagetext
from dbwiki.incidents import (ActionMalformed, ActionRecord, ErrorAbsent,
                              EventPresent, EvidenceRef, FlowResumed, Manual,
                              MonitoringWindow, Status, append_action,
                              link_codes, load_incidents_from, parse_actions,
                              read_incident)
from dbwiki.transaction import Actor, Tree

AT = "2026-08-30T14:22:10Z"
BAD_DAY = "2026-02-30T10:00:00Z"
PATH = "incidents/2026-08-05-cdb1-a.md"
SLUG = "2026-08-05-cdb1-a"
BASE = "0" * 40
ACTOR = Actor("dba@example.com")
PAGE = ("---\ntype: incident\ndb: cdb1\nstatus: open\nopened: 2026-08-05\n"
        "---\n\n# TNS storm on cdb1\n\nLinked: [[errors/TNS-12564]]\n")

#: Every character `str.splitlines` breaks on besides `\n`. YAML treats the
#: C1/Unicode ones as line breaks too; the rest end a line in every page
#: reader that splits with `splitlines`.
BREAKS = ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028",
          "\u2029"]


def record(**overrides) -> ActionRecord:
    return ActionRecord(**({"at": AT, "kind": "record-action",
                            "actor": "dba@example.com", "intent": "i",
                            "summary": "s", "status_after": Status.OPEN}
                           | overrides))


# -- instants ---------------------------------------------------------------

def test_a_record_refuses_an_at_that_has_the_shape_but_no_such_day():
    with pytest.raises(ActionMalformed, match=r"\Aat:"):
        record(at=BAD_DAY)


def test_an_action_heading_on_a_day_that_never_happened_is_a_problem():
    page = PAGE + (f"\n## Action {BAD_DAY}\n\n```yaml\nkind: record-action\n"
                   "actor: a@b\nintent: i\nsummary: s\nstatus_after: open\n"
                   "```\n")
    actions = parse_actions(page)
    assert actions.records == ()
    assert "at:" in actions.problems[0].message


def test_decode_refuses_an_impossible_at_by_field():
    with pytest.raises(incident_action.FieldError) as info:
        incident_action.decode(slug=SLUG, verb="record-action",
                               fields={"intent": "x", "summary": "y"},
                               actor=ACTOR, base=BASE, at=BAD_DAY)
    assert info.value.field == "at"


def test_build_refuses_an_impossible_at_before_writing_updated():
    with pytest.raises(ActionMalformed):
        lifecycle.build(Tree.of({PATH: PAGE}, BASE), PATH,
                        lifecycle.RecordAction(intent="x", summary="y"),
                        ACTOR, BAD_DAY)


@pytest.mark.parametrize("until", [BAD_DAY, "2026-09-31T00:00:00Z",
                                   "2026-09-02T24:00:00Z"])
def test_a_window_refuses_an_until_that_is_not_a_real_instant(until):
    with pytest.raises(ActionMalformed, match=r"\Auntil:"):
        MonitoringWindow(ErrorAbsent("TNS-12564"), AT, until)


# -- frontmatter YAML cannot construct ----------------------------------------

@pytest.mark.parametrize("line", ["opened: 2026-02-30",
                                  f"updated: {BAD_DAY}"])
def test_frontmatter_reads_an_impossible_date_as_its_text(line):
    text = PAGE.replace("opened: 2026-08-05", line)
    fm = pagetext.frontmatter(text)
    assert fm["type"] == "incident"
    key, _, value = line.partition(": ")
    assert fm[key] == value


def test_an_impossible_date_keeps_the_incident_in_the_queue():
    text = PAGE.replace("opened: 2026-08-05", f"updated: {BAD_DAY}")
    inc = read_incident(text, PATH)
    assert inc.updated == BAD_DAY
    assert [i.path for i in load_incidents_from({PATH: text}.get, [PATH])] \
        == [PATH]


def test_lint_reports_an_impossible_date_as_malformed_frontmatter():
    text = PAGE.replace("opened: 2026-08-05", f"updated: {BAD_DAY}")
    findings = lint._frontmatter_findings(PATH, text)
    assert [f.rule for f in findings] == ["frontmatter-malformed"]
    assert "YAML" in findings[0].message


def test_lint_wiki_does_not_crash_on_an_impossible_date(tmp_path):
    (tmp_path / "incidents").mkdir()
    (tmp_path / PATH).write_text(
        PAGE.replace("opened: 2026-08-05", "opened: 2026-02-30"))
    rules = {f.rule for f in lint.lint_wiki(tmp_path) if f.file == PATH}
    assert "frontmatter-malformed" in rules


def test_an_action_window_with_an_impossible_unquoted_date_is_a_problem():
    page = PAGE + (f"\n## Action {AT}\n\n```yaml\nkind: start-monitoring\n"
                   "actor: a@b\nintent: i\nsummary: s\n"
                   "status_after: monitoring\nwindow:\n  kind: manual\n"
                   f"  description: d\n  start: {AT}\n  until: {BAD_DAY}\n"
                   "```\n")
    actions = parse_actions(page)
    assert actions.records == ()
    assert len(actions.problems) == 1


def test_an_evidence_ref_scan_skips_a_block_yaml_cannot_construct():
    page = PAGE + f"\n```yaml\nwhen: {BAD_DAY}\n```\n"
    assert evidence_ref.parse_refs(page) == ((), ())


# -- one line ------------------------------------------------------------------

@pytest.mark.parametrize("brk", BREAKS + ["\n"])
@pytest.mark.parametrize("field", ["actor", "intent", "summary", "ticket",
                                   "rollback"])
def test_a_record_refuses_every_line_break_in_a_scalar(field, brk):
    with pytest.raises(ActionMalformed, match=rf"\A{field}:"):
        record(**{field: f"first{brk}second"})


@pytest.mark.parametrize("brk", [b for b in BREAKS if b != "\r"])
def test_a_record_refuses_a_line_break_in_notes_other_than_newline(brk):
    with pytest.raises(ActionMalformed, match=r"\Anotes:"):
        record(notes=f"first{brk}second")


def test_crlf_notes_are_normalised_so_the_round_trip_holds():
    r = record(notes="first\r\nsecond")
    assert r.notes == "first\nsecond"
    assert parse_actions(append_action(PAGE, r)).records == (r,)


def test_a_lone_carriage_return_in_notes_is_refused():
    with pytest.raises(ActionMalformed, match=r"\Anotes:"):
        record(notes="first\rsecond")


@pytest.mark.parametrize("build", [ErrorAbsent, EventPresent, FlowResumed,
                                   Manual])
@pytest.mark.parametrize("brk", BREAKS + ["\n"])
def test_a_signal_payload_must_be_one_line(build, brk):
    with pytest.raises(ActionMalformed):
        build(f"check shipping{brk}---")


def test_a_monitor_request_with_a_newline_in_its_signal_is_refused():
    with pytest.raises(incident_action.FieldError, match=r"\Asignal:"):
        incident_action.decode(
            slug=SLUG, verb="monitor", actor=ACTOR, base=BASE, at=AT,
            fields={"signal": {"kind": "manual",
                               "description": "check\n---\nsign off"},
                    "until": "2026-09-02T00:00:00Z", "intent": "watch",
                    "summary": "restarted"})


def test_round_trip_holds_for_random_text_through_a_page():
    plain = list("ab #:-'\"|`{}[]!&*?%@\t\\") + [" ", "é", "\u00a0", "\n"]
    alphabet = plain * 6 + BREAKS
    rng = random.Random(1)

    def s(k):
        return "".join(rng.choice(alphabet) for _ in range(rng.randint(1, k)))

    built = 0
    for _ in range(3000):
        try:
            r = record(actor="x" + s(4), intent="i" + s(6),
                       summary="s" + s(8), notes="n" + s(10))
        except ActionMalformed:
            continue
        built += 1
        assert parse_actions(append_action(PAGE, r)).records == (r,), r
    assert built > 100


# -- bytes outside the record, title, link_codes --------------------------------

@pytest.mark.parametrize("odd", ["\x0c", "\u2028", "\r\n", "\x85"])
def test_append_action_keeps_every_byte_outside_its_section(odd):
    page = PAGE + f"\n## Update 2026-08-05\n\n```\nalert{odd}log\n```\n"
    out = append_action(page, record())
    assert out.startswith(page.rstrip("\n"))
    assert append_action(out, record()) == out


def test_the_title_is_never_read_from_a_yaml_comment():
    text = ("---\ntype: incident\n# owner: dba team\nstatus: open\n---\n\n"
            "# Real title\n")
    assert read_incident(text, PATH).title == "Real title"


def test_link_codes_leaves_a_markdown_link_alone():
    text = ("---\ntype: incident\n---\n"
            "see [ORA-600 note](https://support.example/ORA-600) now\n")
    assert link_codes(text, lambda p: True) == text


def test_link_codes_still_links_a_bare_mention_beside_a_link():
    text = ("---\ntype: incident\n---\n"
            "see [note](https://support.example/ORA-600) and ORA-600\n")
    assert link_codes(text, lambda p: True).endswith(
        "and [[errors/ORA-600]]\n")


def test_a_resolution_row_keeps_the_db_one_cell():
    inc = read_incident(PAGE.replace("db: cdb1", "db: 'cdb1 | x'"), PATH)
    _, row = lifecycle.resolution_row(
        inc, record(kind="resolve", status_after=Status.RESOLVED))
    assert row.count(" | ") == 4


def test_past_fixes_gather_survives_an_error_page_that_is_not_utf8(tmp_path):
    import datetime as dt

    from dbwiki import past_fixes
    (tmp_path / "incidents").mkdir()
    (tmp_path / "errors").mkdir()
    page = append_action(PAGE, record(summary="restarted the listener"))
    (tmp_path / PATH).write_text(page)
    (tmp_path / "errors/TNS-12564.md").write_bytes(
        b"---\ntype: error-class\n---\n\n# TNS-12564 \xff\n")
    history = past_fixes.gather(tmp_path, dt.date(2026, 9, 23))
    assert [f.incident for f in history["TNS-12564"].fixes] == [SLUG]
    assert past_fixes.apply(tmp_path, "errors/TNS-12564.md",
                            history["TNS-12564"], dt.date(2026, 9, 23))
    assert b"# TNS-12564 \xff\n" in (tmp_path / "errors/TNS-12564.md").read_bytes()


@pytest.mark.parametrize("raw", ["digests/../2026-08-05.md",
                                 "digests/./2026-08-05.md",
                                 "digests/.hidden/2026-08-05.md",
                                 "digests/a..b/2026-08-05.json"])
def test_an_evidence_path_cannot_step_out_of_its_database_directory(raw):
    """Issue 09 (incidents part)."""
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        EvidenceRef.parse(raw)
    with pytest.raises(ActionMalformed, match=r"\Aevidence:"):
        record(evidence=(raw,))


def test_an_ordinary_database_directory_is_still_evidence():
    assert str(EvidenceRef.parse("digests/cdb1_stby/2026-08-05.md")) == \
        "digests/cdb1_stby/2026-08-05.md"
