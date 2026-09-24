"""`dbwiki eval history`: the replay's honesty and its arithmetic — which days
the window admits, that the tree an arm reads has had the future taken out of
it while the operator's own wiki is left alone, which tokens of the history
block count as repeated, that the two arms differ in exactly the block, and
that the report says which days changed their mind. The model is never called:
`structured.propose` is the seam every test replaces."""

import datetime as dt
import json

import pytest
from fixtures import make_config
from fixtures.incident_pages import incident_page

from dbwiki import db_history, evaluate, history_eval as he, structured

DB = "cdb1"
CODE = "ORA-1653"
TODAY = dt.date(2026, 9, 14)
DAY = "2026-09-10"
HIST_DAY = "2026-09-05"
OLD = "2026-09-01-cdb1-tablespace"
NEW = f"{DAY}-cdb1-storm"
HEADLINE = "cdb1: the users tablespace filled again"

INDEX = ("---\ntype: index\n---\n\n# Logbook\n\n## Databases\n\n"
         f"- [[databases/{DB}]] — {DB}\n\n## Incidents\n\n"
         f"- [[incidents/{OLD}]] — Tablespace kept filling\n"
         f"- [[incidents/{NEW}]] — Tonight's storm\n")

PROPOSAL = {
    "schema_version": 1,
    "summary": f"{CODE} again, as in incidents/{OLD}",
    "notable": True,
    "journal_entry": (f"The growth that filled the users tablespace on "
                      f"{HIST_DAY} came back tonight. It is the same shape as "
                      f"incidents/{OLD}, which was resolved a week ago."),
    "error_updates": [{"code": CODE, "note": "nine hits overnight"}],
    "incident": {"action": "none", "slug": None, "title": None,
                 "body": None, "existing_page": None},
    "flags": [],
}


def digest(day: str, *, notable: bool = True, db: str = DB) -> dict:
    return {
        "db": db,
        "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:59Z",
                   "day": day},
        "generated_by": "dbwiki-compactor/test",
        "pattern_versions": {"alert": 1},
        "sources": {"alert": {
            "total_events": 9, "by_class": {"error": 9},
            "routine_counters": {},
            "notable": [{"rule": "ora_error", "class": "error", "count": 9,
                         "first_ts": f"{day}T01:00:00Z",
                         "last_ts": f"{day}T04:00:00Z", "codes": [CODE],
                         "message": f"{CODE}: unable to extend",
                         "template": f"{CODE}: unable to extend",
                         "es_samples": []}]}},
        "deltas": [],
        "totals": {"events": 9, "notable_events": 9, "notable_groups": 1},
        "notable": notable,
    }


def write_digest(root, day: str, **over) -> None:
    (root / "digests" / DB / f"{day}.json").write_text(
        json.dumps(digest(day, **over)))


@pytest.fixture
def wiki(tmp_path):
    """A wiki with one older resolved incident, one opened on the replayed
    day, a journal day inside the history window, and the digests for both."""
    root = tmp_path / "wiki"
    (root / "digests" / DB).mkdir(parents=True)
    (root / "databases" / DB / "journal").mkdir(parents=True)
    (root / "incidents").mkdir()
    write_digest(root, HIST_DAY)
    write_digest(root, DAY)
    (root / "databases" / DB / "journal" / "2026-09.md").write_text(
        f"---\ntype: journal\ndb: {DB}\n---\n\n# {DB} journal 2026-09\n\n"
        f"## {HIST_DAY} — {HEADLINE}\n\nIt filled and we extended it.\n")
    (root / "incidents" / f"{OLD}.md").write_text(
        incident_page(DB, "Tablespace kept filling", status="resolved",
                      opened="2026-09-01T00:00:00Z", error_codes=(CODE,)))
    (root / "incidents" / f"{NEW}.md").write_text(
        incident_page(DB, "Tonight's storm", status="open",
                      opened=f"{DAY}T02:00:00Z", error_codes=(CODE,)))
    (root / "index.md").write_text(INDEX)
    (root / "log.md").write_text("---\ntype: log\n---\n\n# Log\n")
    return root


@pytest.fixture
def cfg():
    return make_config(agents={"pi": {"cheap": "c", "strong": "s"},
                               "history_days": 90})


@pytest.fixture
def item(wiki):
    return he.wiki_items(wiki, DB, 5, today=dt.date(2026, 9, 11))[-1]


def propose(monkeypatch, proposal: dict | None = None) -> list[str]:
    """The one seam every test uses: the real prompt is built and the real
    proposal applied, only the model's judgment is ours."""
    seen: list[str] = []

    def _propose(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.25,
                             exit_code=0, timed_out=False,
                             usage={"input_tokens": 900, "output_tokens": 120,
                                    "cost_usd": "unknown"})
        return json.loads(json.dumps(proposal or PROPOSAL))

    monkeypatch.setattr(structured, "propose", _propose)
    return seen


def block(wiki, *, days: int = 90) -> str:
    return db_history.render(db_history.gather(
        wiki, DB, today=dt.date.fromisoformat(DAY), codes=(CODE,), days=days))


# ---- the window ---------------------------------------------------------------

def test_the_window_admits_its_oldest_day_and_never_today(wiki):
    write_digest(wiki, "2026-09-09")
    write_digest(wiki, "2026-09-12")
    write_digest(wiki, TODAY.isoformat())

    items = he.wiki_items(wiki, DB, 4, today=TODAY)

    assert [i["id"] for i in items] == ["2026-09-10", "2026-09-12"]


def test_a_day_with_no_digest_is_simply_missing_from_the_list(wiki):
    items = he.wiki_items(wiki, DB, 10, today=TODAY)

    assert [i["id"] for i in items] == [HIST_DAY, DAY]


def test_a_digest_that_will_not_parse_is_skipped_not_raised(wiki):
    (wiki / "digests" / DB / "2026-09-12.json").write_text("{ not json")

    assert [i["id"] for i in he.wiki_items(wiki, DB, 4, today=TODAY)] == [DAY]


def test_an_item_carries_the_tier_the_real_tick_would_have_used(wiki):
    item = he.wiki_items(wiki, DB, 10, today=TODAY)[-1]

    assert item["input"] == {"db": DB, "digest": digest(DAY),
                             "wiki": str(wiki)}
    assert item["expected"] == {"model_tier": "strong", "codes": [CODE]}
    assert item["metadata"] == {"source": f"digests/{DB}/{DAY}.json"}


# ---- the tree an arm reads ----------------------------------------------------

def listing(root) -> list[tuple[str, float]]:
    return sorted((p.relative_to(root).as_posix(), p.stat().st_mtime)
                  for p in root.rglob("*"))


def test_the_copy_loses_the_incident_the_day_itself_opened(wiki, tmp_path):
    copy = he.throwaway_copy(wiki, tmp_path / "copy", DB, DAY)

    assert not (copy / "incidents" / f"{NEW}.md").exists()
    assert (copy / "incidents" / f"{OLD}.md").exists()


def test_the_pruned_page_loses_its_index_bullet_and_the_other_keeps_its_own(
        wiki, tmp_path):
    copy = he.throwaway_copy(wiki, tmp_path / "copy", DB, DAY)

    index = (copy / "index.md").read_text()
    assert f"[[incidents/{NEW}]]" not in index
    assert f"[[incidents/{OLD}]]" in index
    assert f"[[databases/{DB}]]" in index


def test_the_source_wiki_is_only_ever_read(wiki, tmp_path):
    before = listing(wiki)

    he.throwaway_copy(wiki, tmp_path / "copy", DB, DAY)

    assert listing(wiki) == before


def test_a_wiki_with_no_incidents_and_no_index_copies_anyway(tmp_path):
    src = tmp_path / "bare"
    (src / "digests" / DB).mkdir(parents=True)

    copy = he.throwaway_copy(src, tmp_path / "copy", DB, DAY)

    assert (copy / "digests" / DB).is_dir()


def test_an_unreadable_incident_page_is_skipped_not_raised(wiki, tmp_path):
    (wiki / "incidents" / "broken.md").write_text("---\nstatus: [\n---\n")

    copy = he.throwaway_copy(wiki, tmp_path / "copy", DB, DAY)

    assert (copy / "incidents" / "broken.md").exists()


# ---- what the model could repeat ----------------------------------------------

def test_the_tokens_are_the_days_pages_and_codes_the_block_names(wiki):
    tokens = he.history_tokens(block(wiki), DAY)

    assert HIST_DAY in tokens
    assert f"incidents/{OLD}" in tokens
    assert f"incidents/{OLD}.md" not in tokens


def test_a_day_not_earlier_than_the_replayed_day_is_not_history(wiki):
    tokens = he.history_tokens(block(wiki), "2026-09-03")

    assert HIST_DAY not in tokens
    assert "2026-09-01" in tokens


def test_an_error_code_in_the_block_is_a_token(wiki):
    assert he.history_tokens(f"- {CODE} held\n", DAY) == frozenset({CODE})


def test_a_code_todays_digest_carries_is_not_credited_to_history(wiki):
    """The block lists past fixes *for today's codes*, so every one of them
    is also in the digest: a proposal restating today's code repeats the
    digest, not the history."""
    block_text = f"Past fixes for today's codes:\n- {CODE} · 2026-03-01 · held\n"
    assert he.history_tokens(block_text, DAY, today_codes=[CODE]) == \
        frozenset({"2026-03-01"})
    # a padded spelling of the same code is the same code
    assert he.history_tokens(block_text, DAY, today_codes=["ORA-01653"]) \
        == frozenset({"2026-03-01"})


# ---- one day, one arm ---------------------------------------------------------

def test_the_none_arm_sees_no_history_and_repeats_none_of_it(
        monkeypatch, item, cfg):
    propose(monkeypatch)

    result = he.run_item(item, cfg, arm="none", history_days=0)

    assert result.history_chars == 0
    assert result.history_refs == ()
    assert result.verdict["parsed_ok"] and result.verdict["apply_ok"]
    assert result.verdict["error"] is None
    assert (result.notable, result.incident_action) == (True, "none")
    assert result.verdict["duration_s"] == 0.25


def test_the_history_arm_sees_the_block_and_its_repeats_are_counted(
        monkeypatch, item, cfg):
    propose(monkeypatch)

    result = he.run_item(item, cfg, arm="history", history_days=90)

    assert result.history_chars > 0
    assert HIST_DAY in result.history_refs
    assert f"incidents/{OLD}" in result.history_refs
    assert result.verdict["parsed_ok"] and result.verdict["apply_ok"]


def test_only_the_history_arm_puts_the_block_in_the_prompt(
        monkeypatch, item, cfg):
    seen = propose(monkeypatch)

    he.run_item(item, cfg, arm="none", history_days=0)
    he.run_item(item, cfg, arm="history", history_days=90)

    assert f"Database history for {DB}" not in seen[0]
    assert f"Database history for {DB}" in seen[1]


def test_a_failed_arm_is_a_measurement_not_an_exception(monkeypatch, item,
                                                        cfg):
    def _boom(prompt, cfg, *, escalate=False, telemetry=None):
        raise RuntimeError("the harness timed out")

    monkeypatch.setattr(structured, "propose", _boom)

    result = he.run_item(item, cfg, arm="history", history_days=90)

    assert result.proposal is None
    assert result.notable is None and result.incident_action is None
    assert (result.journal_entry, result.summary, result.history_refs) \
        == ("", "", ())
    assert result.history_chars > 0
    assert "the harness timed out" in result.verdict["error"]
    assert result.verdict["flags"] is None
    assert result.verdict["codes_allowlisted"] is None


def test_an_arm_writes_nothing_into_the_wiki_it_replays(monkeypatch, item, cfg,
                                                        wiki):
    propose(monkeypatch)
    before = listing(wiki)

    he.run_item(item, cfg, arm="history", history_days=90)

    assert listing(wiki) == before


# ---- the run ------------------------------------------------------------------

def test_the_run_measures_every_day_under_every_arm_and_writes_the_report(
        monkeypatch, wiki, cfg, tmp_path, capsys):
    propose(monkeypatch)
    items = he.wiki_items(wiki, DB, 10, today=TODAY)
    out = tmp_path / "reports" / "history.md"

    rows = he.run(items, cfg, model="gemma-4", days=10, out=out)

    assert [(row[0]["id"], row[1].arm, row[2].arm) for row in rows] \
        == [(HIST_DAY, "none", "history"), (DAY, "none", "history")]
    assert [r.history_days for r in rows[0][1:]] == [0, 90]
    err = capsys.readouterr().err
    assert all(field in err for field, _ in evaluate.SCORES)
    assert f"{DAY} history" in err
    assert out.read_text().startswith(f"# History eval — {DB}")


# ---- the report ---------------------------------------------------------------

def arm(name: str, *, notable: bool, action: str, refs=(), entry="prose.",
        summary="a summary") -> he.ArmResult:
    return he.ArmResult(
        arm=name, history_days=0 if name == "none" else 90, proposal={},
        verdict={"parsed_ok": True, "apply_ok": True, "codes_allowlisted": 1.0,
                 "incident_consistent": True, "flags": 0, "duration_s": 0.25,
                 "input_tokens": 900, "output_tokens": 120, "error": None},
        notable=notable, incident_action=action, journal_entry=entry,
        summary=summary, history_refs=refs, history_chars=len(refs) * 10)


def rows(wiki):
    return [
        ({"id": HIST_DAY, "input": {"db": DB, "wiki": str(wiki)}},
         arm("none", notable=True, action="none"),
         arm("history", notable=True, action="none", refs=("2026-09-01",))),
        ({"id": DAY, "input": {"db": DB, "wiki": str(wiki)}},
         arm("none", notable=True, action="none"),
         arm("history", notable=True, action="open", refs=("2026-09-01",))),
    ]


def test_the_report_names_every_day_and_marks_only_the_ones_that_differ(wiki):
    text = he.render_report(rows(wiki), db=DB, model="gemma-4", days=10)

    assert f"## {HIST_DAY}\n" in text
    assert f"## {DAY} **differs**" in text
    assert "**Journal — history**" in text
    assert "| history | 2 | 2/2 | 2/2 |" in text


def test_the_report_ends_with_the_days_history_changed(wiki):
    text = he.render_report(rows(wiki), db=DB, model="gemma-4", days=10)

    tail = text.split("## Days where history changed the decision")[-1]
    assert f"- {DAY} — incident none -> open" in tail
    assert HIST_DAY not in tail


def test_a_replay_with_nothing_in_it_still_renders(wiki):
    text = he.render_report([], db=DB, model="gemma-4", days=10)

    assert "- window: last 10 day(s), 0 item(s)" in text
    assert "- (none)" in text


def failed(name: str) -> he.ArmResult:
    return he.ArmResult(
        arm=name, history_days=90, proposal=None,
        verdict={"parsed_ok": False, "apply_ok": False,
                 "codes_allowlisted": None, "incident_consistent": None,
                 "flags": None, "duration_s": None, "input_tokens": None,
                 "output_tokens": None, "error": "HarnessError: x"},
        notable=None, incident_action=None, journal_entry="", summary="",
        history_refs=(), history_chars=0)


def test_a_failed_arm_is_not_a_changed_decision(wiki):
    """`notable None` against `True` is a parse failure, not history
    changing anything."""
    rows_ = [({"id": DAY, "input": {"db": DB, "wiki": str(wiki)}},
              arm("none", notable=True, action="open"), failed("history"))]
    text = he.render_report(rows_, db=DB, model="gemma-4", days=10)
    tail = text.split("## Days where history changed the decision")[-1]
    assert DAY not in tail and "- (none)" in tail
    assert f"## {DAY} **differs**" not in text
    assert "| flags | 0 | - |" in text
