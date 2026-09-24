"""The revision-pinned read model, over a real git-backed wiki.

Nothing here fakes git: the whole point of `readmodel.build` is that its three
primitives agree about one revision, and a stub tree would prove only that the
parsing works on bytes nobody committed. The one monkeypatch is the
broken-page case, where the failure has to come from inside the per-page parse.
"""

import dataclasses
import os
import subprocess

import pytest

from dbwiki import readmodel, transaction
from dbwiki.incidents import read_incident
from dbwiki.readmodel import Origin
from fixtures.incident_pages import incident_page

NOW = "2026-08-31T09:00:00Z"
EMAIL = "dba@example.com"

SLUG = "2026-08-05-cdb1-tns-12564"
INCIDENT = f"incidents/{SLUG}.md"
ERROR = "errors/TNS-12564.md"
JOURNAL = "databases/cdb1/journal/2026-08.md"
OLD_JOURNAL = "databases/cdb1/journal/2026-07.md"
DIGEST = "digests/cdb1/2026-08-30.md"

INDEX = """\
---
type: index
---

# Logbook

## Open incidents

- [[incidents/2026-08-05-cdb1-tns-12564]] connect failures
- [[incidents/2026-08-05-cdb1-tns-12564|the same page again]]
- [[errors/TNS-12564]]
- [[dup]]
"""

INCIDENT_PAGE = """\
---
type: incident
db: cdb1
status: open
title: TNS-12564 connect failures
opened: 2026-08-05T00:00:00Z
---

# a heading the frontmatter title outranks

## Timeline

The standby lost its listener overnight.
"""

ERROR_PAGE = """\
---
type: error-class
---

# TNS-12564

## Occurrences

| day | db | note | evidence |
|---|---|---|---|
| 2026-08-28 | cdb1 | listener refused | digests/cdb1/2026-08-28.md |
| not-a-day | cdb1 | never happened | digests/cdb1/2026-08-29.md |
| 2026-08-30 | cdb2 | again after the restart | digests/cdb2/2026-08-30.md |

## Resolution history

| resolved | db | incident | remediation | evidence |
|---|---|---|---|---|
| 2026-08-06 | cdb1 | [[incidents/2026-08-05-cdb1-tns-12564]] | restarted the listener | digests/cdb1/2026-08-06.md |
| not-a-day | cdb1 | [[incidents/2026-08-05-cdb1-tns-12564]] | never happened | 2026-08-07T00:00:00Z |
| 2026-08-20 | cdb1 | [[incidents/2026-08-05-cdb1-tns-12564]] | raised the listener queue | 2026-08-20T09:00:00Z |
| 2026-08-20 | cdb1 | [[incidents/2026-08-19-cdb1-tns-12564]] | pinned the static registration | 2026-08-20T11:00:00Z |

## Past fixes

3 fixes across 2 incidents on 1 database: 1 held, 1 reopened, 1 n/a.

| when | db | incident | what was done | outcome | held |
|---|---|---|---|---|---|
| 2026-08-20 | cdb1 | [[incidents/2026-08-19-cdb1-tns-12564]] | pinned the static registration (ticket: CHG-7) | succeeded | held (14d) |
| not-a-day | cdb1 | [[incidents/2026-08-19-cdb1-tns-12564]] | never happened | failed | n/a |
| 2026-08-12 | cdb1 | [[incidents/2026-08-05-cdb1-tns-12564]] | lsnrctl reload \\| grep READY came back empty | failed | n/a |
| 2026-08-06 | cdb1 | [[incidents/2026-08-05-cdb1-tns-12564]] | restarted the listener | succeeded | reopened 2026-08-12 |
"""

JOURNAL_PAGE = """\
---
type: journal
db: cdb1
---

# cdb1 journal 2026-08

## 2026-08-12 — patched the listener

Rolled the listener config forward.

## 2026-08-30 — standby resynced

Caught up after the outage.

## Notes

A heading no journal writer wrote.
"""

OLD_JOURNAL_PAGE = """\
---
type: journal
db: cdb1
---

# cdb1 journal 2026-07

## 2026-07-19 — first sighting

The listener dropped once and recovered on its own.
"""

BARE_PAGE = """\
---
type: note
---

A page with neither a frontmatter title nor a level-one heading.
"""

MENTION_PAGE = """\
---
type: note
---

# Standby rotation

## Rotation

The overnight rotation moved to cdb2 while the primary was down.
"""

RESEARCHED = "errors/ORA-1013.md"

RESEARCHED_PAGE = """\
---
type: error-class
researched: 2026-07-28
---

# ORA-1013

## Reference

**Cause:** the user interrupted an Oracle operation by entering CTRL-C,
forcing the operation to end
(source: sources/oracle-docs; url: <https://docs.oracle.com/en/error-help/db/ora-01013/>; accessed: 2026-07-28).

**Action:** continue with the next operation
(source: sources/oracle-docs; url: <https://docs.oracle.com/en/error-help/db/ora-01013/>; accessed: 2026-07-28).

Practitioner caveat: the cancelled statement may have finished anyway
(source: sources/jonathan-lewis; url: <https://example.invalid/>; accessed: 2026-07-28).
"""

PAGES = {
    "index.md": INDEX,
    INCIDENT: INCIDENT_PAGE,
    ERROR: ERROR_PAGE,
    RESEARCHED: RESEARCHED_PAGE,
    JOURNAL: JOURNAL_PAGE,
    OLD_JOURNAL: OLD_JOURNAL_PAGE,
    "notes/bare.md": BARE_PAGE,
    "notes/mention.md": MENTION_PAGE,
    "notes/dup.md": "---\ntype: note\n---\n\n# One dup\n",
    "archive/dup.md": "---\ntype: note\n---\n\n# Another dup\n",
    "databases/cdb1.md": "---\ntype: database\ndb: cdb1\n---\n\n# cdb1\n",
    "databases/cdb2.md": "---\ntype: database\ndb: cdb2\n---\n\n# cdb2\n",
    "reports/2026-08-29-0800.md": "# morning\n",
    "reports/2026-08-29-1500.md": "# afternoon\n",
    "reports/2026-08-30-0900.md": "# partial\n",
    "reports/2026-08-30.md": "# consolidated\n",
    DIGEST: "# cdb1 2026-08-30\n",
}


def git(repo, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def commit_value(subject: str, actor: str = "") -> transaction.Commit:
    return transaction.Commit("f" * 40, "fffffff", "2026-08-30T00:00:00Z",
                              EMAIL, subject, actor)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """A wiki whose history gives each origin arm one page: the error page is
    an `ingest:` commit, the index carries an `Actor:` trailer, `notes/bare.md`
    is a `digest:` commit on a curated path, and everything else keeps the
    plain `init` subject."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "wiki"
    for rel, text in PAGES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", EMAIL)
    git(root, "config", "user.name", "DBA")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")

    (root / ERROR).write_text(ERROR_PAGE + "\n<!-- rewritten -->\n")
    git(root, "commit", "-am", "ingest: TNS-12564 occurrences")
    (root / "index.md").write_text(INDEX + "\n<!-- tidied -->\n")
    git(root, "commit", "-am", f"tidy the index\n\nActor: {EMAIL}")
    (root / "notes/bare.md").write_text(BARE_PAGE + "\n<!-- rebuilt -->\n")
    git(root, "commit", "-am", "digest: rebuild the bare page")
    return root


@pytest.fixture
def snap(wiki):
    return readmodel.build(wiki, transaction.head(wiki), now=lambda: NOW)


def test_page_fields(snap):
    incident = snap.pages[INCIDENT]
    assert incident.type == "incident"
    assert incident.title == "TNS-12564 connect failures"
    assert incident.db == "cdb1"
    assert incident.headings == ("Timeline",)


def test_title_falls_back_to_heading_then_stem(snap):
    assert snap.pages[ERROR].title == "TNS-12564"
    assert snap.pages["notes/bare.md"].title == "bare"
    assert snap.pages["notes/bare.md"].db == ""


@pytest.mark.parametrize("path,commit,expected", [
    ("index.md", commit_value("digest: cdb1", actor=EMAIL), Origin.OPERATOR),
    ("index.md", commit_value("digest: cdb1"), Origin.MACHINE),
    ("index.md", commit_value("ingest: cdb1"), Origin.AGENT),
    ("index.md", commit_value("research — TNS-12564"), Origin.AGENT),
    (DIGEST, commit_value("init"), Origin.MACHINE),
    ("index.md", commit_value("report the standby outage"), Origin.HAND),
    ("index.md", commit_value("chore: whatever"), Origin.HAND),
    (DIGEST, commit_value("chore: whatever"), Origin.MACHINE),
    ("index.md", None, Origin.HAND),
])
def test_origin_table(path, commit, expected):
    assert readmodel.origin_of(path, commit) is expected


def test_origin_end_to_end(snap):
    assert snap.pages[ERROR].origin is Origin.AGENT
    assert snap.pages["index.md"].origin is Origin.OPERATOR
    assert snap.pages["notes/bare.md"].origin is Origin.MACHINE
    assert snap.pages[INCIDENT].origin is Origin.HAND
    assert snap.pages[INCIDENT].commit.subject == "init"


def test_links_resolve_and_deduplicate(snap):
    assert snap.links["index.md"] == (INCIDENT, ERROR)
    assert snap.backlinks[ERROR] == ("index.md",)
    assert snap.backlinks[INCIDENT] == (ERROR, "index.md")


def test_ambiguous_stem_does_not_resolve(snap):
    assert "notes/dup.md" not in snap.backlinks
    assert "archive/dup.md" not in snap.backlinks


def test_occurrences_skip_the_malformed_row(snap):
    assert snap.occurrences_of(code="TNS-12564") == (
        readmodel.Occurrence("TNS-12564", "2026-08-28", "cdb1",
                             "listener refused",
                             "digests/cdb1/2026-08-28.md"),
        readmodel.Occurrence("TNS-12564", "2026-08-30", "cdb2",
                             "again after the restart",
                             "digests/cdb2/2026-08-30.md"))
    assert [o.day for o in snap.occurrences_of(db="cdb2")] == ["2026-08-30"]


def test_journal_entries_are_newest_first_across_months(snap):
    """The month files sort before the entries, so a per-file sort would put
    July's newest ahead of August's; `fleet`'s "latest journal line" reads
    element zero."""
    assert snap.journals["cdb1"] == (
        readmodel.JournalEntry("cdb1", "2026-08-30", "standby resynced",
                               JOURNAL),
        readmodel.JournalEntry("cdb1", "2026-08-12", "patched the listener",
                               JOURNAL),
        readmodel.JournalEntry("cdb1", "2026-07-19", "first sighting",
                               OLD_JOURNAL))


def test_report_of_day_prefers_the_consolidated_page(snap):
    assert snap.report_of_day == {
        "2026-08-29": "reports/2026-08-29-1500.md",
        "2026-08-30": "reports/2026-08-30.md"}


def test_a_page_names_the_digest_sidecars_its_changes_are_read_from(snap):
    """A digest reads its own sidecar; a day's report reads every database's
    sidecar for that day, by the exact name `daily_html.day_digests` globs;
    every other page reads none."""
    held = dataclasses.replace(snap, inventory=snap.inventory | {
        "digests/cdb1/2026-08-30.json", "digests/cdb2/2026-08-30.json",
        "digests/cdb1/2026-08-29.json", "digests/cdb1/2026-08-30-1200.json"})
    day = ("digests/cdb1/2026-08-30.json", "digests/cdb2/2026-08-30.json")
    assert held.change_sidecars(DIGEST) == ("digests/cdb1/2026-08-30.json",)
    assert held.change_sidecars("reports/2026-08-30.md") == day
    assert held.change_sidecars("reports/2026-08-30-0900.md") == day
    assert held.change_sidecars("digests/cdb2/2026-08-28.md") == (), \
        "a digest the wiki holds no sidecar for"
    assert held.change_sidecars(INCIDENT) == ()
    assert held.change_sidecars("databases/cdb1.md") == ()


def test_incidents_and_dbs(snap):
    assert set(snap.incidents) == {SLUG}
    assert snap.db_incidents("cdb1")[0].path == INCIDENT
    assert snap.db_incidents("cdb2") == ()
    assert snap.dbs == ("cdb1", "cdb2")


def test_digests_are_linkable_but_unread(snap):
    assert DIGEST in snap.inventory
    assert snap.exists(DIGEST)
    assert DIGEST not in snap.pages
    assert DIGEST not in snap.text


def test_search_ranks_field_hits_before_body_hits(snap):
    hits = snap.search("cdb2")
    assert [h.path for h in hits] == ["databases/cdb2.md", ERROR,
                                      "notes/mention.md"]
    assert "rotation moved to cdb2" in hits[2].snippet
    assert snap.search("cdb2", limit=1) == hits[:1]
    assert snap.search("") == ()


def test_a_broken_page_costs_only_itself(wiki, monkeypatch):
    real = readmodel.PageInfo

    def refuse(**fields):
        if fields["path"] == "index.md":
            raise ValueError("this page will not parse")
        return real(**fields)

    monkeypatch.setattr(readmodel, "PageInfo", refuse)
    snap = readmodel.build(wiki, transaction.head(wiki), now=lambda: NOW)
    assert "index.md" not in snap.pages
    assert "index.md" not in snap.text
    assert "index.md" in snap.inventory
    assert snap.backlinks == {INCIDENT: (ERROR,)}
    assert set(snap.incidents) == {SLUG}
    assert snap.pages[ERROR].title == "TNS-12564"


def test_two_builds_at_one_revision_are_equal(wiki):
    revision = transaction.head(wiki)
    assert (readmodel.build(wiki, revision, now=lambda: NOW)
            == readmodel.build(wiki, revision, now=lambda: NOW))


def test_snapshot_is_pinned_to_its_revision(wiki, snap):
    assert snap.revision == transaction.head(wiki)
    assert snap.built_at == NOW


def test_an_older_snapshot_names_the_commits_of_its_own_revision(wiki):
    """Issue 20: `build` read `last_commits` at HEAD, so a snapshot pinned
    to an older revision (a cached build, a base the operator previewed
    against) credited its pages to commits that revision had never seen,
    and the origin with them."""
    before_bare = git(wiki, "rev-parse", "HEAD~1").strip()
    older = readmodel.build(wiki, before_bare, now=lambda: NOW)
    assert older.pages["notes/bare.md"].commit.subject == "init"
    assert older.pages["notes/bare.md"].origin is Origin.HAND
    assert older.pages["index.md"].origin is Origin.OPERATOR

    first = git(wiki, "rev-list", "--max-parents=0", "HEAD").strip()
    oldest = readmodel.build(wiki, first, now=lambda: NOW)
    assert {info.commit.sha for info in oldest.pages.values()} == {first}
    assert oldest.pages[ERROR].origin is Origin.HAND


def test_occurrence_rows_parse_the_table_structured_writes():
    """Three readers follow one format whose owner is `structured._TABLE_HEAD`
    (the header the ingest contract tells the agent to write). If that header
    gains or reorders a column, this must fail before an operator sees an
    empty error page."""
    from dbwiki import structured
    table = (f"## Occurrences\n\n{structured._TABLE_HEAD}\n"
             "| 2026-08-30 | cdb1 | listener drops | digests/cdb1/2026-08-30.md |\n")
    assert readmodel.parse_occurrences("ORA-600", table) == (
        readmodel.Occurrence("ORA-600", "2026-08-30", "cdb1",
                             "listener drops", "digests/cdb1/2026-08-30.md"),)


CITE = ("(source: sources/oracle-docs; url: "
        "<https://docs.oracle.com/en/error-help/db/ora-01013/>; "
        "accessed: 2026-07-28).")


def test_the_snapshot_carries_the_research_of_the_pages_that_have_it(snap):
    assert set(snap.research) == {"ORA-1013"}
    found = snap.research["ORA-1013"]
    assert (found.code, found.path) == ("ORA-1013", RESEARCHED)
    assert found.researched == "2026-07-28"
    assert found.cause == ("the user interrupted an Oracle operation by "
                           "entering CTRL-C, forcing the operation to end")
    assert found.action == "continue with the next operation"
    assert "Practitioner caveat" not in found.cause + found.action
    assert found.citations == (readmodel.Citation(
        source="sources/oracle-docs",
        url="https://docs.oracle.com/en/error-help/db/ora-01013/",
        accessed="2026-07-28"),), \
        "the citation both paragraphs share is carried once, as fields"


def test_a_citation_is_split_off_the_end_of_the_paragraph():
    prose, cite = readmodel.split_citation(
        "contact cannot be made with the remote party " + CITE)
    assert prose == "contact cannot be made with the remote party"
    assert cite == readmodel.Citation(
        "sources/oracle-docs",
        "https://docs.oracle.com/en/error-help/db/ora-01013/", "2026-07-28")


def test_a_citation_naming_only_its_source_still_counts():
    assert readmodel.split_citation(
        "check the listener is up (source: sources/oracle-docs).") == \
        ("check the listener is up",
         readmodel.Citation("sources/oracle-docs", "", ""))


def test_a_parenthesis_that_is_not_a_citation_stays_in_the_prose():
    para = "the job (a scheduler one) was stopped (see the trace)."
    assert readmodel.split_citation(para) == (para, None)


def test_two_paragraphs_citing_different_pages_carry_both_citations():
    page = ("---\ntype: error-class\n---\n\n# ORA-60\n\n## Reference\n\n"
            "**Cause:** two sessions deadlocked\n"
            "(source: sources/oracle-docs; url: <https://a/>; "
            "accessed: 2026-07-28).\n\n"
            "**Action:** look at the trace\n"
            "(source: sources/jonathan-lewis; url: <https://b/>; "
            "accessed: 2026-07-29).\n")
    found = readmodel.parse_research("ORA-60", "errors/ORA-60.md", page)
    assert [c.source for c in found.citations] == \
        ["sources/oracle-docs", "sources/jonathan-lewis"]


NOTED_PAGE = ("---\ntype: error-class\nresearched: 2026-08-17\n---\n\n"
              "# ORA-60\n\n## Reference\n\n"
              "**Cause:** two sessions deadlocked\n"
              "(source: sources/oracle-docs; url: https://a/; "
              "accessed: 2026-07-28).\n\n"
              "**Action:** look at the trace\n"
              "(source: sources/oracle-docs; url: https://a/; "
              "accessed: 2026-07-28).\n\n"
              "**Practitioner note:** the deadlock graph names the two "
              "statements\n"
              "(source: sources/jonathan-lewis; url: https://b/; "
              "accessed: 2026-08-17).\n\n"
              "**Practitioner note:** bitmap indexes on a table two sessions "
              "update at once\nare the common real cause\n"
              "(source: sources/oracle-base; url: https://c/; "
              "accessed: 2026-08-16).\n")


def test_the_practitioner_notes_are_carried_in_page_order():
    """The writer appends one paragraph per note, so their order on the page
    is the order it proposed them in and the card reads them the same way."""
    found = readmodel.parse_research("ORA-60", "errors/ORA-60.md", NOTED_PAGE)
    assert found.notes == (
        readmodel.Note(
            text="the deadlock graph names the two statements",
            citation=readmodel.Citation("sources/jonathan-lewis",
                                        "https://b/", "2026-08-17")),
        readmodel.Note(
            text="bitmap indexes on a table two sessions update at once are "
                 "the common real cause",
            citation=readmodel.Citation("sources/oracle-base",
                                        "https://c/", "2026-08-16")))


def test_the_notes_do_not_join_the_cause_and_action_citations():
    """A note's provenance belongs to the note. A page-level list that grew
    by two would claim the cause was read out of pages it never was."""
    found = readmodel.parse_research("ORA-60", "errors/ORA-60.md", NOTED_PAGE)
    assert [c.source for c in found.citations] == ["sources/oracle-docs"]
    assert found.cause == "two sessions deadlocked"
    assert found.action == "look at the trace"


def test_a_note_without_a_citation_keeps_its_whole_text():
    page = ("---\ntype: error-class\n---\n\n# ORA-60\n\n## Reference\n\n"
            "**Cause:** two sessions deadlocked.\n\n"
            "**Practitioner note:** somebody typed this one by hand.\n")
    found = readmodel.parse_research("ORA-60", "errors/ORA-60.md", page)
    assert found.notes == (readmodel.Note(
        text="somebody typed this one by hand.", citation=None),)


def test_a_hand_written_practitioner_caveat_is_not_a_note(snap):
    """The paragraph on the live ORA-1013 page is prose of no known shape.
    Only the writer's `**Practitioner note:**` prefix is a note."""
    found = snap.research["ORA-1013"]
    assert found.notes == ()
    assert found.citations == (readmodel.Citation(
        source="sources/oracle-docs",
        url="https://docs.oracle.com/en/error-help/db/ora-01013/",
        accessed="2026-07-28"),), \
        "the caveat's own source is not promoted to a page-level citation"


def test_an_unresearched_error_page_is_absent_from_the_research(snap):
    assert "TNS-12564" not in snap.research


def test_a_reference_section_without_a_cause_is_not_research():
    page = ("---\ntype: error-class\n---\n\n# ORA-60\n\n## Reference\n\n"
            "Someone opened the section and wrote nothing under it.\n")
    assert readmodel.parse_research("ORA-60", "errors/ORA-60.md", page) is None


def test_a_page_with_no_reference_section_is_not_research():
    assert readmodel.parse_research("ORA-60", "errors/ORA-60.md",
                                    ERROR_PAGE) is None


def test_research_without_a_researched_key_reads_as_an_empty_date():
    page = ("---\ntype: error-class\n---\n\n# ORA-60\n\n## Reference\n\n"
            "**Cause:** two sessions deadlocked.\n")
    found = readmodel.parse_research("ORA-60", "errors/ORA-60.md", page)
    assert found.researched == "" and found.action == ""
    assert found.cause == "two sessions deadlocked."
    assert found.citations == ()


SECOND = "2026-08-19-cdb1-tns-12564"


def test_resolutions_are_newest_first_with_ties_keeping_page_order(snap):
    """`lifecycle.build` appends, so page order buries the last thing that
    worked under every older attempt; two resolutions closed on one day keep
    the order the page spells them."""
    assert snap.resolutions["TNS-12564"] == (
        readmodel.Resolution("TNS-12564", "2026-08-20", "cdb1", SLUG,
                             "raised the listener queue",
                             "2026-08-20T09:00:00Z"),
        readmodel.Resolution("TNS-12564", "2026-08-20", "cdb1", SECOND,
                             "pinned the static registration",
                             "2026-08-20T11:00:00Z"),
        readmodel.Resolution("TNS-12564", "2026-08-06", "cdb1", SLUG,
                             "restarted the listener",
                             "digests/cdb1/2026-08-06.md"))


def test_a_malformed_resolution_row_costs_only_itself(snap):
    assert [r.remediation for r in snap.resolutions["TNS-12564"]] == [
        "raised the listener queue", "pinned the static registration",
        "restarted the listener"]


def test_the_header_and_separator_rows_are_not_resolutions():
    from dbwiki import lifecycle
    table = (f"## {lifecycle.RESOLUTION_SECTION}\n\n"
             f"{lifecycle.RESOLUTION_HEAD}\n"
             "| 2026-08-20 | cdb1 | [[incidents/2026-08-19-cdb1-tns-12564]] "
             "| pinned the static registration | 2026-08-20T11:00:00Z |\n")
    assert readmodel.parse_resolutions("TNS-12564", table) == (
        readmodel.Resolution("TNS-12564", "2026-08-20", "cdb1", SECOND,
                             "pinned the static registration",
                             "2026-08-20T11:00:00Z"),)


def test_a_row_naming_a_page_outside_incidents_is_not_a_resolution():
    table = ("## Resolution history\n\n"
             "| 2026-08-20 | cdb1 | [[errors/TNS-12564]] | nothing | at |\n")
    assert readmodel.parse_resolutions("TNS-12564", table) == ()


def test_an_error_page_with_no_resolution_history_yields_nothing(snap):
    assert readmodel.parse_resolutions("ORA-1013", RESEARCHED_PAGE) == ()
    assert "ORA-1013" not in snap.resolutions
    assert set(snap.resolutions) == {"TNS-12564"}


def test_past_fixes_keep_the_order_the_writer_spelled_them(snap):
    """`past_fixes.render` already writes newest first with same-day ties
    ordered by record time, which the table no longer carries: re-sorting
    here by day could only lose that order, never improve it."""
    assert snap.past_fixes["TNS-12564"] == (
        readmodel.PastFixRow("TNS-12564", "2026-08-20", "cdb1", SECOND,
                             "pinned the static registration (ticket: CHG-7)",
                             "succeeded", "held (14d)"),
        readmodel.PastFixRow("TNS-12564", "2026-08-12", "cdb1", SLUG,
                             "lsnrctl reload \\| grep READY came back empty",
                             "failed", "n/a"),
        readmodel.PastFixRow("TNS-12564", "2026-08-06", "cdb1", SLUG,
                             "restarted the listener", "succeeded",
                             "reopened 2026-08-12"))


def test_an_error_page_with_no_past_fixes_yields_nothing(snap):
    assert readmodel.parse_past_fixes("ORA-1013", RESEARCHED_PAGE) == ()
    assert set(snap.past_fixes) == {"TNS-12564"}


def test_past_fixes_read_back_what_the_writer_wrote():
    """Round trip against `past_fixes.render` and `splice_section`, so the
    row pattern here cannot drift from the table the stage writes."""
    from dbwiki import past_fixes as pf
    from dbwiki.incidents import Outcome
    history = pf.FixHistory("TNS-12564", (
        pf.PastFix("TNS-12564", SECOND, "cdb1", "2026-08-20", "resolve",
                   "pinned it \\| twice (rollback: unpin)",
                   Outcome.SUCCEEDED, pf.Verdict.RECURRED, "2026-08-25"),
        pf.PastFix("TNS-12564", SLUG, "cdb1", "2026-08-06", "record-action",
                   "restarted the listener", Outcome.PENDING,
                   pf.Verdict.OPEN)))
    page = pf.splice_section(RESEARCHED_PAGE, pf.render(history))
    assert readmodel.parse_past_fixes("TNS-12564", page) == (
        readmodel.PastFixRow("TNS-12564", "2026-08-20", "cdb1", SECOND,
                             "pinned it \\| twice (rollback: unpin)",
                             "succeeded", "recurred 2026-08-25"),
        readmodel.PastFixRow("TNS-12564", "2026-08-06", "cdb1", SLUG,
                             "restarted the listener", "pending", "open"))


RUN_ID = "9f2c1a0b7de4"
SECOND_RUN = "1111aaaa2222"


@pytest.fixture
def touched(wiki):
    """The same wiki with three more commits on the incident page: one machine
    tick that carries a `Run-ID:` trailer, a hand edit that carries none, and
    a second tick. The hand edit sits between them so a join that ignored the
    trailer would have to attribute it to one of the two."""
    page = wiki / INCIDENT
    page.write_text(INCIDENT_PAGE + "\n<!-- first tick -->\n")
    git(wiki, "commit", "-am",
        f"ingest: cdb1 TNS-12564\n\nRun-ID: {RUN_ID}\nActor: agent")
    page.write_text(INCIDENT_PAGE + "\n<!-- by hand -->\n")
    git(wiki, "commit", "-am", "fix the timeline wording")
    page.write_text(INCIDENT_PAGE + "\n<!-- second tick -->\n")
    git(wiki, "commit", "-am",
        f"ingest: cdb1 TNS-12564 again\n\nRun-ID: {SECOND_RUN}\nActor: agent")
    return wiki


@pytest.fixture
def touch_snap(touched):
    return readmodel.build(touched, transaction.head(touched),
                           now=lambda: NOW)


def test_a_commit_without_a_run_id_trailer_is_not_a_touch(touch_snap):
    assert set(touch_snap.touches_by_run) == {RUN_ID, SECOND_RUN}
    assert [t.slug for t in touch_snap.touches_by_run[RUN_ID]] == [SLUG]


def test_both_ticks_reach_the_page_they_wrote(touch_snap):
    rows = touch_snap.touches_by_incident[SLUG]
    assert {t.run_id for t in rows} == {RUN_ID, SECOND_RUN}
    assert all(t.slug == SLUG for t in rows)
    assert all(len(t.commit) == 40 for t in rows)


def test_touches_are_newest_commit_first(touch_snap):
    rows = touch_snap.touches_by_incident[SLUG]
    assert [t.run_id for t in rows] == [SECOND_RUN, RUN_ID]
    assert rows[0].at >= rows[1].at


def test_a_wiki_no_tick_has_written_carries_no_touch(snap):
    assert snap.touches_by_run == {}
    assert snap.touches_by_incident == {}


def test_only_incident_pages_are_touches(touched):
    """A tick that also rewrites an error page produces one touch, not two:
    the join answers which *incidents* a tick affected."""
    (touched / ERROR).write_text(ERROR_PAGE + "\n<!-- also this -->\n")
    (touched / INCIDENT).write_text(INCIDENT_PAGE + "\n<!-- and this -->\n")
    git(touched, "commit", "-am", "ingest: both pages\n\nRun-ID: cafe1234beef")
    snap = readmodel.build(touched, transaction.head(touched),
                           now=lambda: NOW)
    assert [t.slug for t in snap.touches_by_run["cafe1234beef"]] == [SLUG]


def test_the_touch_window_bounds_the_walk(touched, monkeypatch):
    """`since` is the only bound on the join. Zero days leaves the whole
    history behind the clock the snapshot is built with."""
    monkeypatch.setattr(readmodel, "TOUCH_WINDOW_DAYS", 0)
    snap = readmodel.build(touched, transaction.head(touched),
                           now=lambda: "2036-01-01T00:00:00Z")
    assert snap.touches_by_run == {}
def _incident(codes, db="cdb1"):
    return read_incident(
        incident_page(db, "TNS-12564 connect failures", error_codes=codes),
        INCIDENT)


def test_an_incident_naming_no_error_code_has_no_last_seen():
    rows = (readmodel.Occurrence("TNS-12564", "2026-08-28", "cdb1", "", ""),)
    assert readmodel.last_seen(_incident(()), rows) is None, \
        "a page that names no code says nothing about when it went quiet"


def test_a_code_seen_only_on_another_database_has_no_last_seen():
    rows = (readmodel.Occurrence("TNS-12564", "2026-08-28", "cdb2", "", ""),)
    assert readmodel.last_seen(_incident(("TNS-12564",)), rows) is None, \
        "the same code on a neighbour is the neighbour's episode"


def test_the_latest_day_across_the_incidents_codes_wins():
    rows = (readmodel.Occurrence("TNS-12564", "2026-08-28", "cdb1", "", ""),
            readmodel.Occurrence("ORA-00600", "2026-08-30", "cdb1", "", ""),
            readmodel.Occurrence("ORA-00600", "2026-07-01", "cdb1", "", ""))
    inc = _incident(("TNS-12564", "ORA-00600"))
    assert readmodel.last_seen(inc, rows) == "2026-08-30", \
        "the incident went quiet when the last of its codes did"


def test_the_last_seen_day_is_the_string_the_row_carries():
    rows = (readmodel.Occurrence("TNS-12564", "2026-08-28", "cdb1", "", ""),)
    answer = readmodel.last_seen(_incident(("TNS-12564",)), rows)
    assert answer == "2026-08-28" and isinstance(answer, str), \
        "the day travels as the YYYY-MM-DD the page spells, not as a date"
