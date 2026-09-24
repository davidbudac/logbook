"""Issue 14 (verification 2026-09-23): lifecycle rules the transition table
did not state.

- a merge may only fold a page into an *active* one, or A→B then B→A closes
  both with no `Resolve` anywhere (ADR-0003: Resolve is the only path to
  `resolved` a human did not take by merging a duplicate away);
- `extend` only ever pushes `until` later;
- past fixes follow the incident's status (and its merge lineage), not only
  a later `resolve` record;
- a resolve's `--no-error-pages` is part of the record, so a later backfill
  honours it;
- `set_frontmatter` removes a block-style key with its indented children.
"""

import datetime as dt

import pytest

from dbwiki import lifecycle as lc
from dbwiki import past_fixes as pf
from dbwiki.incidents import (ActionMalformed, ActionRecord, ErrorAbsent,
                              Outcome, Status, parse_action, read_incident,
                              render_action)
from dbwiki.pagetext import frontmatter, set_frontmatter
from dbwiki.transaction import Actor, Tree

A = "incidents/2026-08-05-cdb1-a.md"
B = "incidents/2026-08-05-cdb1-b.md"
ACTOR = Actor("dba@example.com")
PAGE = """---
type: incident
db: cdb1
status: open
opened: 2026-08-05
---

# TNS storm on cdb1

Linked: [[errors/TNS-12564]]
"""
INDEX = ("---\ntype: index\n---\n\n# Index\n\n## Open incidents\n\n"
         "- [[incidents/2026-08-05-cdb1-a]] — A\n"
         "- [[incidents/2026-08-05-cdb1-b]] — B\n")
ERR = "---\ntype: error-class\n---\n\n# TNS-12564\n"


def apply(files, path, command, at):
    proposal = lc.build(Tree.of(files, "0" * 40), path, command, ACTOR, at)
    return files | {k: v for k, v in proposal.files.items() if v is not None}


def pair():
    return {A: PAGE, B: PAGE.replace("TNS storm", "dup"), "index.md": INDEX,
            "errors/TNS-12564.md": ERR}


# -- 1. merge cycle ---------------------------------------------------------------

def test_a_merge_into_a_resolved_page_is_refused():
    files = apply(pair(), A, lc.Merge(into="2026-08-05-cdb1-b"),
                  "2026-08-30T10:00:00Z")
    with pytest.raises(lc.MergeRefused, match="not active"):
        apply(files, B, lc.Merge(into="2026-08-05-cdb1-a"),
              "2026-08-30T11:00:00Z")


def test_a_merge_into_a_monitoring_page_is_still_allowed():
    files = apply(pair(), B, lc.StartMonitoring(
        signal=ErrorAbsent("TNS-12564"), until="2026-09-10T00:00:00Z",
        intent="w", summary="s"), "2026-08-30T09:00:00Z")
    files = apply(files, A, lc.Merge(into="2026-08-05-cdb1-b"),
                  "2026-08-30T10:00:00Z")
    assert read_incident(files[B], B).status is Status.MONITORING


def test_the_statuses_a_merge_may_keep_are_the_active_ones():
    assert lc.MERGE_KEEPS == frozenset({Status.OPEN, Status.MONITORING})


def test_a_merged_page_names_the_page_that_superseded_it():
    files = apply(pair(), A, lc.Merge(into="2026-08-05-cdb1-b"),
                  "2026-08-30T10:00:00Z")
    assert read_incident(files[A], A).superseded_by == B
    assert read_incident(files[B], B).superseded_by is None
    files = apply(files, A, lc.Reopen(reason="not a duplicate after all"),
                  "2026-08-30T11:00:00Z")
    assert read_incident(files[A], A).superseded_by is None


# -- 2. extend never shrinks --------------------------------------------------------

def monitored():
    return apply({A: PAGE, "index.md": INDEX}, A, lc.StartMonitoring(
        signal=ErrorAbsent("TNS-12564"), until="2026-09-10T00:00:00Z",
        intent="w", summary="s"), "2026-08-30T10:00:00Z")


@pytest.mark.parametrize("until", ["2026-08-30T12:00:00Z",
                                   "2026-09-10T00:00:00Z"])
def test_extend_refuses_an_until_that_is_not_later_than_the_current_one(until):
    with pytest.raises(ActionMalformed, match=r"\Auntil:"):
        apply(monitored(), A, lc.ExtendMonitoring(until=until, intent="x"),
              "2026-09-01T10:00:00Z")


def test_extend_refuses_an_until_that_is_already_past():
    """The window ran out on 09-10; pushing it to 09-11 on 09-12 would open a
    window that is closed before it is written."""
    with pytest.raises(ActionMalformed, match=r"\Auntil:"):
        apply(monitored(), A,
              lc.ExtendMonitoring(until="2026-09-11T00:00:00Z", intent="x"),
              "2026-09-12T10:00:00Z")


def test_extend_to_a_later_until_is_still_allowed():
    files = apply(monitored(), A, lc.ExtendMonitoring(
        until="2026-09-20T00:00:00Z", intent="x"), "2026-09-01T10:00:00Z")
    assert read_incident(files[A], A).monitoring.until == "2026-09-20T00:00:00Z"


# -- 3. past fixes follow status ------------------------------------------------------

TODAY = dt.date(2026, 9, 23)


def judged(files, path):
    inc = read_incident(files[path], path)
    records = list(inc.actions.records)
    return [(r.kind, pf.verdict_for(r, records[i + 1:], (), db="cdb1",
                                    today=TODAY, held_after_days=14,
                                    closed=not inc.is_active)[0])
            for i, r in enumerate(records)]


def test_a_note_after_resolve_is_judged_as_closed_not_open():
    files = apply(pair(), A, lc.Resolve(summary="restarted listener"),
                  "2026-08-01T10:00:00Z")
    files = apply(files, A, lc.RecordAction(
        intent="post-incident hardening", summary="raised EXPIRE_TIME",
        outcome=Outcome.SUCCEEDED), "2026-08-02T10:00:00Z")
    assert judged(files, A) == [("resolve", pf.Verdict.HELD),
                                ("record-action", pf.Verdict.HELD)]


def test_an_open_incident_still_reads_open():
    files = apply(pair(), A, lc.RecordAction(intent="i", summary="s"),
                  "2026-08-02T10:00:00Z")
    assert judged(files, A) == [("record-action", pf.Verdict.OPEN)]


def write(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)


def test_gather_follows_a_merge_to_the_page_that_kept_the_incident(tmp_path):
    files = apply(pair(), A, lc.RecordAction(intent="fix", summary="bounced"),
                  "2026-08-02T10:00:00Z")
    files = apply(files, A, lc.Merge(into="2026-08-05-cdb1-b"),
                  "2026-08-03T10:00:00Z")
    write(tmp_path, files)
    fixes = pf.gather(tmp_path, TODAY)["TNS-12564"].fixes
    assert [(f.action, f.verdict) for f in fixes] == [
        ("bounced", pf.Verdict.OPEN)], "the kept page is still open"
    files = apply(files, B, lc.Resolve(summary="stable"),
                  "2026-08-04T10:00:00Z")
    write(tmp_path, files)
    fixes = {f.action: f.verdict
             for f in pf.gather(tmp_path, TODAY)["TNS-12564"].fixes}
    assert fixes["bounced"] is pf.Verdict.HELD


# -- 4. backfill honours --no-error-pages ---------------------------------------------

def test_a_resolve_without_error_pages_says_so_in_its_record():
    files = apply(pair(), A, lc.Resolve(summary="not a fix",
                                        update_error_pages=False),
                  "2026-08-01T10:00:00Z")
    record = read_incident(files[A], A).actions.records[-1]
    assert record.error_pages is False
    assert "error_pages: false\n" in files[A]


def test_a_default_resolve_writes_no_error_pages_key():
    files = apply(pair(), A, lc.Resolve(summary="fixed"),
                  "2026-08-01T10:00:00Z")
    assert "error_pages" not in files[A]
    assert read_incident(files[A], A).actions.records[-1].error_pages is True


def test_error_pages_false_round_trips_and_is_resolve_only():
    rec = ActionRecord(at="2026-08-01T10:00:00Z", kind="resolve", actor="a@b",
                       intent="i", summary="s", status_after=Status.RESOLVED,
                       error_pages=False)
    assert parse_action(render_action(rec)) == rec
    with pytest.raises(ActionMalformed, match=r"\Aerror_pages:"):
        ActionRecord(at="2026-08-01T10:00:00Z", kind="record-action",
                     actor="a@b", intent="i", summary="s",
                     status_after=Status.OPEN, error_pages=False)


def test_error_pages_must_be_a_boolean_in_the_yaml():
    section = ("## Action 2026-08-01T10:00:00Z\n\n```yaml\nkind: resolve\n"
               "actor: a@b\nintent: i\nsummary: s\nstatus_after: resolved\n"
               "outcome: succeeded\nerror_pages: nope\n```\n")
    with pytest.raises(ActionMalformed, match=r"\Aerror_pages:"):
        parse_action(section)


# -- 5. block-style frontmatter keys --------------------------------------------------

BLOCK = PAGE.replace("status: open\n", (
    "status: monitoring\nmonitoring:\n  kind: error_absent\n"
    "  code: TNS-12564\n  start: '2026-08-29T00:00:00Z'\n"
    "  until: '2026-09-02T00:00:00Z'\n"))


def test_deleting_a_block_key_takes_its_children_with_it():
    out = set_frontmatter(BLOCK, {"monitoring": None})
    assert "  kind:" not in out
    assert "monitoring" not in frontmatter(out)
    assert frontmatter(out)["opened"] == dt.date(2026, 8, 5)


def test_replacing_a_block_key_takes_its_children_with_it():
    out = set_frontmatter(BLOCK, {"monitoring": "{kind: 'manual'}"})
    assert frontmatter(out)["monitoring"] == {"kind": "manual"}


def test_a_block_sequence_at_column_zero_goes_with_its_key():
    text = "---\ntype: x\ntags:\n- a\n- b\nkeep: 1\n---\nbody\n"
    assert set_frontmatter(text, {"tags": None}) == \
        "---\ntype: x\nkeep: 1\n---\nbody\n"


def test_a_resolve_on_a_block_style_window_leaves_readable_frontmatter():
    files = apply({A: BLOCK, "index.md": INDEX, "errors/TNS-12564.md": ERR},
                  A, lc.Resolve(summary="stable"), "2026-08-30T14:22:10Z")
    fm = frontmatter(files[A])
    assert fm["status"] == "resolved"
    assert "monitoring" not in fm
