"""The heat model, driven with no git and no wiki on disk.

`heat` splits its arithmetic away from the checkout on purpose, so this file
takes it at its word: every test here builds a `readmodel.Snapshot` by hand
and reads through `transaction.Tree.of`, and nothing starts a subprocess. What
that buys is that the rules worth pinning — a missing digest and a quiet
digest being two different answers, a class the digest never mentions still
counting as observed, the unknown-host band sorting last — are asserted
against the code that decides them rather than against a repository that
happens to be shaped the right way.

The one thing this file cannot prove is that `build` asks git for the right
paths. `tests/test_portal_api.py` covers that end over a real wiki.
"""

import json

import pytest

from dbwiki import heat, readmodel
from dbwiki.transaction import Tree

TODAY = "2026-08-31"


def snapshot(*, dbs: tuple[str, ...], text: dict) -> readmodel.Snapshot:
    """A snapshot carrying only what `rows_of` reads.

    `rows_of` asks a snapshot for two things, the database list and the page
    bodies, and a fixture that filled the other ten fields with plausible
    values would suggest they matter here. They are empty because the model
    under test never looks at them."""
    return readmodel.Snapshot(
        revision="0" * 40, built_at=f"{TODAY}T09:00:00Z",
        inventory=frozenset(), pages={}, text=text, links={}, backlinks={},
        incidents={}, occurrences=(), research={}, resolutions={}, journals={},
        touches_by_run={}, touches_by_incident={},
        report_of_day={}, dbs=dbs)


def digest(**by_class: int) -> str:
    """A one-source digest sidecar saying exactly these counts."""
    return json.dumps({"db": "cdb1", "sources": {"alert":
                                                 {"by_class": by_class}}})


def test_a_database_page_names_its_host_under_the_frontmatter_host_key():
    assert heat.host_of("---\ntype: database\nhost: lab-dg1.localdomain\n"
                        "---\n\n# cdb1\n") == "lab-dg1.localdomain"


def test_a_database_page_names_its_host_under_the_frontmatter_hostname_key():
    """No live page carries either key today; every one of them says its host
    in prose. Both spellings are read anyway because the frontmatter is where
    an operator would reach to correct a page the prose rule misreads, and a
    database is not going to move because the page said `hostname`."""
    assert heat.host_of("---\ntype: database\nhostname: lab-dg2.localdomain\n"
                        "---\n\n# cdb2\n") == "lab-dg2.localdomain"


def test_a_frontmatter_host_outranks_the_prose_the_page_opens_with():
    page = ("---\ntype: database\nhost: keyed.localdomain\n---\n\n# cdb1\n\n"
            "Host `prose.localdomain` (192.0.2.121).\n")
    assert heat.host_of(page) == "keyed.localdomain"


def test_a_database_page_names_its_host_in_the_prose_the_live_pages_use():
    page = "# cdb1\n\nHost `lab-dg1.localdomain` (192.0.2.121).\n"
    assert heat.host_of(page) == "lab-dg1.localdomain"


def test_a_host_reached_mid_sentence_counts_the_way_an_opening_one_does():
    """`databases/cdb1_stby.md` says it this way, and a pattern anchored to
    the line start would read that page as having no host at all."""
    page = ("# cdb1_stby\n\nThe physical standby of cdb1. Host "
            "`lab-dg2.localdomain` (192.0.2.122).\n")
    assert heat.host_of(page) == "lab-dg2.localdomain"


def test_a_page_that_names_no_host_at_all_answers_the_empty_string():
    assert heat.host_of("---\ntype: database\n---\n\n# cdb3\n\nThin.\n") == ""


def test_a_digest_sums_one_class_across_every_source_it_holds():
    """The operator scanning a row asks how loud the day was, not which log
    was loud, so alert, listener and dataguard add up."""
    text = json.dumps({"sources": {
        "alert": {"by_class": {"error": 3, "warning": 2}},
        "listener": {"by_class": {"error": 1, "unmatched": 84}},
        "dataguard": {"by_class": {"error": 2}}}})
    assert heat.parse_counts(text) == {"error": 6, "warning": 2,
                                       "unmatched": 84}


def test_a_digest_keeps_a_class_the_maps_do_not_draw():
    """`CLASSES` is applied by `rows_of` and not here: the parse is a reading
    of the sidecar, and dropping `routine` at this depth would make the
    function answer a question about the board rather than about the file."""
    assert heat.parse_counts(digest(routine=73, error=3)) == {"routine": 73,
                                                              "error": 3}


def test_a_digest_with_no_sources_at_all_sums_to_nothing_rather_than_None():
    """An empty mapping is still a mapping: the compactor wrote a sidecar, so
    the day was observed, and every class reads zero at the row."""
    assert heat.parse_counts(json.dumps({"sources": {}})) == {}


@pytest.mark.parametrize("text, why", [
    ("{not json at all", "the blob does not parse"),
    ("[1, 2, 3]", "the top level is a list"),
    ('"a string"', "the top level is a string"),
    ("null", "the top level is null"),
    (json.dumps({"db": "cdb1"}), "there is no sources key"),
    (json.dumps({"sources": []}), "sources is not a mapping"),
    (json.dumps({"sources": {"alert": 7}}), "a source is not a mapping"),
    (json.dumps({"sources": {"alert": {"by_class": 7}}}),
     "by_class is not a mapping"),
    (json.dumps({"sources": {"alert": {"by_class": {"error": "3"}}}}),
     "a count is a string"),
    (json.dumps({"sources": {"alert": {"by_class": {"error": 1.5}}}}),
     "a count is a float"),
    (json.dumps({"sources": {"alert": {"by_class": {"error": True}}}}),
     "a count is a bool"),
])
def test_a_digest_the_reader_cannot_believe_answers_None(text, why):
    """None and not zero: guessing zero would draw a day nobody can say
    anything about as a quiet day."""
    assert heat.parse_counts(text) is None, why


def test_the_window_is_the_whole_span_ending_on_the_day_it_was_handed():
    days = heat.window_days(TODAY)
    assert len(days) == heat.WINDOW_DAYS
    assert days[-1] == TODAY


def test_the_window_runs_oldest_first_with_no_day_missing_between():
    days = heat.window_days(TODAY)
    assert days == tuple(sorted(days))
    assert len(set(days)) == len(days)
    assert days[0] == "2026-06-03"


def test_a_day_with_no_digest_reads_None_beside_a_silent_digest_reading_zero():
    """The distinction the whole model exists for. Both cells are empty of
    errors, and only one of them means the database was watched that day."""
    days = ("2026-08-29", "2026-08-30", "2026-08-31")
    snap = snapshot(dbs=("cdb1",), text={})
    tree = Tree.of({heat.digest_path("cdb1", "2026-08-29"): digest(error=3),
                    heat.digest_path("cdb1", "2026-08-31"): digest(
                        routine=12)})

    row = heat.rows_of(snap, tree, days)[0]
    assert row.counts["error"] == (3, None, 0)
    assert row.counts["warning"] == (0, None, 0)


def test_a_digest_the_reader_cannot_believe_reads_the_way_a_missing_one_does():
    days = ("2026-08-30", "2026-08-31")
    snap = snapshot(dbs=("cdb1",), text={})
    tree = Tree.of({heat.digest_path("cdb1", "2026-08-30"): "{truncated",
                    heat.digest_path("cdb1", "2026-08-31"): digest(error=1)})

    row = heat.rows_of(snap, tree, days)[0]
    assert row.counts["error"] == (None, 1)


def test_every_row_carries_one_entry_per_day_under_exactly_the_drawn_classes():
    """The page draws a grid, so a row whose arrays disagree with the axis is
    a row it cannot align. And `routine` is in the sidecar and never on the
    board."""
    days = heat.window_days(TODAY)
    snap = snapshot(dbs=("cdb1", "cdb2"), text={})
    tree = Tree.of({heat.digest_path("cdb1", TODAY): digest(routine=73,
                                                            error=3)})

    for row in heat.rows_of(snap, tree, days):
        assert set(row.counts) == set(heat.CLASSES)
        for name in heat.CLASSES:
            assert len(row.counts[name]) == len(days)


def test_the_rows_group_by_host_with_the_unknown_band_last():
    """`(host == "", host, db)`: hosts alphabetically, and the databases
    nobody has said where they run fall into one band at the end instead of
    sorting under an empty string at the top, where they would read as a host
    called nothing."""
    snap = snapshot(
        dbs=("cdb1", "cdb1_stby", "cdb2", "orphan"),
        text={"databases/cdb1.md": "# cdb1\n\nHost `lab-dg1.localdomain`.\n",
              "databases/cdb1_stby.md": "---\nhost: lab-dg2.localdomain\n"
                                        "---\n\n# cdb1_stby\n",
              "databases/cdb2.md": "# cdb2\n\nHost `lab-dg1.localdomain`.\n",
              "databases/orphan.md": "# orphan\n\nNo host here.\n"})

    rows = heat.rows_of(snap, Tree.of({}), (TODAY,))
    assert [(row.host, row.db) for row in rows] == [
        ("lab-dg1.localdomain", "cdb1"),
        ("lab-dg1.localdomain", "cdb2"),
        ("lab-dg2.localdomain", "cdb1_stby"),
        ("", "orphan")]


def test_a_database_whose_page_the_snapshot_never_read_still_gets_a_row():
    """The counts are the point and the host is only how they are grouped, so
    a thin or unparseable page costs the row its band and not its existence."""
    snap = snapshot(dbs=("cdb1",), text={})
    tree = Tree.of({heat.digest_path("cdb1", TODAY): digest(error=2)})

    rows = heat.rows_of(snap, tree, (TODAY,))
    assert [(row.db, row.host) for row in rows] == [("cdb1", "")]
    assert rows[0].counts["error"] == (2,)
