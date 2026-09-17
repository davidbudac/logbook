"""The daily DBA summary page: tier assignment, link emission, escaping,
determinism, index regeneration, and the CLI/orchestrator wiring.

A fake wiki day is built in tmp_path from the sanitized live digests in
tests/fixtures/real_digests/, so the tiering rules are exercised against real
compactor output — a storm day, a full-day silence, a 14-delta day and a
routine listener-only day. No Elasticsearch, no LLM, no network."""

import datetime as dt
import json
import subprocess
from types import SimpleNamespace

import fixtures as fx
import pytest
from fixtures.incident_pages import incident_page

from dbwiki import daily_html as dh, orchestrate, transaction
from dbwiki.daily_html import (MAX_DEEP_LINKS, available_days, day_digests,
                               day_stats, delta_lines, digest_headline,
                               ledger_summaries, render_daily, render_index,
                               report_rel, tier_of, tiers, write_daily)
from dbwiki.deeplink import Resolver
from dbwiki.incidents import (ErrorAbsent, MonitoringWindow, Status,
                              set_status)
from dbwiki.lock import Held
from dbwiki.monitoring import MONITORING_SCHEMA_VERSION, MonitoringFacts
from dbwiki.orchestrate import Orchestrator

DAY = "2026-07-28"
LINK_BASE = "https://example.invalid/wiki/blob/main"

# real digests placed on a single fake day, one per tier we want to see
PLACED = {
    "cdb1": "fra_transport_storm",          # notable, storm    -> attention
    "quiet1": "full_day_silence",           # notable, silence  -> attention
    "newdb": "standby_first_contact",       # deltas only(*)    -> see below
    "busy": "rate_anomaly_new_codes",       # deltas + notable  -> attention
    "listenonly": "listener_only_routine_day",   # plain        -> routine
    "empty": "empty_partial_window",        # plain, 0 events   -> routine
}

OPEN_INCIDENT = incident_page("cdb1", "cdb1 dataguard transport failure",
                              opened="2026-07-12T00:00:00Z", body="Still open.")
RESOLVED_INCIDENT = incident_page("listenonly", "listenonly listener flap",
                                  status="resolved", body="Done.")
FUTURE_INCIDENT = incident_page("busy", "busy something later",
                                opened="2026-08-02T00:00:00Z", body="Later.")


def _place(root, db, name, day=DAY, *, notable=None, deltas=None):
    """Write one real digest into `wiki/digests/<db>/<day>.json` under a new
    db name and day, optionally overriding the two tiering inputs."""
    d = fx.load_real_digest(name)
    d = json.loads(json.dumps(d))  # deep copy
    d["db"] = db
    d["window"] = {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:00Z",
                   "day": day}
    if notable is not None:
        d["notable"] = notable
    if deltas is not None:
        d["deltas"] = deltas
    p = root / "digests" / db / f"{day}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=1))
    (p.with_suffix(".md")).write_text(f"# Digest: {db} — {day}\n")
    return d


@pytest.fixture
def wiki(tmp_path):
    """A wiki holding one full day: six databases across all three tiers, two
    journals, one open incident, one resolved one, one opened in the future,
    and an intra-day fleet report."""
    root = tmp_path / "wiki"
    for db, name in PLACED.items():
        # `standby_first_contact` is notable in the corpus; this fake day wants
        # a deltas-only database, so its notability is cleared here.
        _place(root, db, name, notable=False if db == "newdb" else None)
    for db in ("cdb1", "busy"):
        j = root / "databases" / db / "journal"
        j.mkdir(parents=True)
        (j / "2026-07.md").write_text(f"---\ntype: journal\ndb: {db}\n---\n\n"
                                      f"# {db} — journal 2026-07\n")
    inc = root / "incidents"
    inc.mkdir()
    (inc / "2026-07-12-cdb1-dataguard.md").write_text(OPEN_INCIDENT)
    (inc / "2026-07-01-listenonly-flap.md").write_text(RESOLVED_INCIDENT)
    (inc / "2026-08-02-busy-later.md").write_text(FUTURE_INCIDENT)
    rep = root / "reports"
    rep.mkdir()
    (rep / f"{DAY}-1015.md").write_text("---\ntype: report\n---\n\n# fleet\n")
    return root


class FakeState:
    def __init__(self, entries):
        self.entries = entries

    def get_ledger(self):
        return self.entries


LEDGER = FakeState({
    f"digests/cdb1/{DAY}.json": {
        "status": "ingested", "notable": True,
        "summary": "Dataguard transport failures for cdb1_stby (ORA-12154)."},
    f"digests/listenonly/{DAY}.json": {
        "status": "ingested", "notable": False,
        "summary": "Routine listener traffic only."},
    f"digests/busy/{DAY}.json": {  # failed entries carry no headline
        "status": "failed", "summary": "should never be shown"},
    f"digests/cdb1/2026-07-27.json": {
        "status": "ingested", "summary": "yesterday, not this page"},
})


# ---- reading the day ---------------------------------------------------------

def test_day_digests_reads_every_db_that_has_one(wiki):
    assert sorted(day_digests(wiki, DAY)) == sorted(PLACED)


def test_day_digests_skips_unreadable_digest(wiki):
    (wiki / "digests" / "broken").mkdir()
    (wiki / "digests" / "broken" / f"{DAY}.json").write_text("{not json")
    assert "broken" not in day_digests(wiki, DAY)
    assert len(day_digests(wiki, DAY)) == len(PLACED)


def test_ledger_summaries_only_ingested_entries_of_that_day():
    got = ledger_summaries(LEDGER, DAY)
    assert set(got) == {"cdb1", "listenonly"}
    assert got["cdb1"]["summary"].startswith("Dataguard transport failures")


def test_ledger_summaries_without_state_is_empty():
    assert ledger_summaries(None, DAY) == {}


def test_report_rel_prefers_consolidated_then_latest_partial(wiki):
    assert report_rel(wiki, DAY) == f"reports/{DAY}-1015.md"
    (wiki / "reports" / f"{DAY}-2215.md").write_text("# later\n")
    assert report_rel(wiki, DAY) == f"reports/{DAY}-2215.md"
    (wiki / "reports" / f"{DAY}.md").write_text("# final\n")
    assert report_rel(wiki, DAY) == f"reports/{DAY}.md"
    assert report_rel(wiki, "2026-01-01") is None


# ---- tiering -----------------------------------------------------------------

def test_tier_of_follows_notable_then_deltas():
    assert tier_of({"notable": True, "deltas": []}) == "attention"
    assert tier_of({"notable": True, "deltas": [{"type": "silence"}]}) == "attention"
    assert tier_of({"notable": False,
                    "deltas": [{"type": "new_service"}]}) == "watch"
    assert tier_of({"notable": False, "deltas": []}) == "routine"


def test_tiers_over_the_fake_day(wiki):
    t = tiers(day_digests(wiki, DAY))
    assert t["attention"] == ["busy", "cdb1", "quiet1"]
    assert t["watch"] == ["newdb"]
    assert t["routine"] == ["empty", "listenonly"]


def test_every_real_digest_lands_in_exactly_one_tier():
    seen = {tier_of(fx.load_real_digest(n)) for n in fx.real_digest_names()}
    assert seen <= {"attention", "watch", "routine"}


# ---- headlines ---------------------------------------------------------------

def test_digest_headline_from_notable_groups_single_source():
    d = {"sources": {"dataguard": {"notable": [
        {"rule": "transport_error", "count": 254},
        {"rule": "connection_error", "count": 121}]}},
        "deltas": [], "totals": {"events": 400}}
    assert digest_headline(d) == "254× transport_error, 121× connection_error (dataguard)"


def test_digest_headline_names_the_source_when_groups_are_mixed():
    d = {"sources": {"dataguard": {"notable": [{"rule": "transport_error",
                                                "count": 254}]},
                     "alert": {"notable": [{"rule": "ora_error", "count": 300}]}},
         "deltas": [], "totals": {"events": 600}}
    assert digest_headline(d) == "300× ora_error (alert), 254× transport_error (dataguard)"


def test_digest_headline_for_silence_is_labelled_telemetry():
    d = fx.load_real_digest("full_day_silence")
    h = digest_headline(d)
    assert h.startswith(dh.SILENCE_NOTE)
    assert "recover" not in h.lower() and "outage" not in h.lower()


def test_digest_headline_for_a_quiet_day():
    assert digest_headline(fx.load_real_digest("empty_partial_window")) == \
        "0 events, nothing notable"


def test_delta_lines_group_by_type_and_label_silence(wiki):
    lines = delta_lines(fx.load_real_digest("rate_anomaly_new_codes"))
    # 12 first_ever_code deltas, 5 distinct codes -> exactly one line
    codes = [l for l in lines if l.startswith("first-ever error codes:")]
    assert codes == ["first-ever error codes: ORA-19815, ORA-2097, ORA-25530, "
                     "ORA-3137, TNS-12599"]
    assert any(l.startswith("rate anomaly: archived_log (alert)") for l in lines)
    assert any(l.startswith("new client program: SQLcl") for l in lines)
    silence = delta_lines(fx.load_real_digest("full_day_silence"))
    assert silence == [f"{dh.SILENCE_NOTE}: no alert, listener, dataguard "
                       f"events in the window"]


def test_delta_lines_say_which_codes_followed_which_change():
    def after_change(**over) -> dict:
        return {"type": "after_change", "source": "alert", "rule": "ora_error",
                "codes": ["ORA-1555"], "first_ts": "2026-07-19T03:41:22Z",
                "gap_s": 4213, "change_ts": "2026-07-19T02:31:09Z",
                "change_rule": "datafile_change", "change": "RESIZE 8G", **over}

    assert delta_lines({"deltas": [after_change()]}) == \
        ["after a change: ORA-1555 70 min after datafile_change"]
    assert delta_lines({"deltas": [after_change(codes=[])]}) == \
        ["after a change: ora_error 70 min after datafile_change"]


# ---- rendering ---------------------------------------------------------------

@pytest.fixture
def page(wiki):
    return render_daily(wiki, DAY, state=LEDGER, link_base=LINK_BASE)


def test_page_is_self_contained_html_with_no_external_assets(page):
    assert page.startswith("<!DOCTYPE html>")
    assert "<style>" in page and "<script" not in page
    assert "http://" not in page
    for marker in ("src=", "@import", "cdn"):
        assert marker not in page
    assert "prefers-color-scheme: dark" in page


def test_all_three_tier_sections_are_always_present(page):
    for heading in ("Needs attention", "Worth a look", "Routine"):
        assert f">{heading} <" in page


def test_empty_tier_says_so_rather_than_disappearing(wiki):
    # a wiki with one plain routine db: the two upper tiers must still show
    root = wiki.parent / "quiet-wiki"
    _place(root, "listenonly", "listener_only_routine_day")
    (root / "incidents").mkdir()
    (root / "reports").mkdir()
    p = render_daily(root, DAY, link_base=LINK_BASE)
    assert "Nothing needs attention today." in p
    assert "Nothing new to look at today." in p
    assert "listenonly" in p


def test_notable_dbs_are_in_the_attention_tier(page):
    attention = page.split(">Needs attention <")[1].split(">Worth a look <")[0]
    for db in ("cdb1", "busy", "quiet1"):
        assert f"<h3>{db}</h3>" in attention
    assert "<h3>newdb</h3>" not in attention


def test_deltas_only_db_is_in_the_watch_tier_with_its_deltas(page):
    watch = page.split(">Worth a look <")[1].split(">Routine <")[0]
    assert "<h3>newdb</h3>" in watch
    assert "first-ever error code" in watch
    assert "new listener service: testcdb_s" in watch
    assert "<h3>cdb1</h3>" not in watch


def test_routine_dbs_are_collapsed_in_a_details_element(page):
    routine = page.split(">Routine <")[1]
    assert '<details class="routine">' in routine
    assert "<summary>2 databases with nothing new" in routine
    assert ">listenonly<" in routine and ">empty<" in routine
    assert "open" not in routine.split("<summary>")[0].split("<details")[1]


def test_silence_is_labelled_as_telemetry_not_database_state(page):
    assert dh.SILENCE_NOTE in page
    assert "NOT database state" in page
    for word in ("recovered", "outage", "back to normal"):
        assert word not in page.lower()


def test_ledger_summary_wins_over_the_derived_headline(page):
    assert "Dataguard transport failures for cdb1_stby (ORA-12154)." in page


def test_derived_headline_used_when_the_ledger_has_no_entry(wiki):
    p = render_daily(wiki, DAY, state=None, link_base=LINK_BASE)
    assert "Dataguard transport failures" not in p
    assert "× transport_error" in p


def test_failed_ledger_entries_never_supply_a_headline(page):
    assert "should never be shown" not in page


# ---- incidents ---------------------------------------------------------------

def test_open_incident_is_listed_in_the_attention_tier(page):
    attention = page.split(">Needs attention <")[1].split(">Worth a look <")[0]
    assert "Open incident" in attention
    assert "cdb1 dataguard transport failure" in attention
    assert "status: open" in attention


def test_resolved_incident_is_not_listed(page):
    assert "listener flap" not in page


def test_incident_opened_after_the_day_is_not_listed(page):
    assert "busy something later" not in page


def test_incident_appears_again_on_a_later_day(wiki):
    _place(wiki, "cdb1", "listener_only_routine_day", day="2026-07-29")
    later = render_daily(wiki, "2026-07-29", link_base=LINK_BASE)
    assert "cdb1 dataguard transport failure" in later


MONITORED_SLUG = "2026-07-10-newdb-flap"
MONITORED_UNTIL = "2026-07-31T00:00:00Z"


def _monitored(wiki):
    """A monitoring incident on the fake day, written through the real
    frontmatter mutator so the page shape is the one `set_status` produces."""
    (wiki / "incidents" / f"{MONITORED_SLUG}.md").write_text(set_status(
        incident_page("newdb", "newdb transport flap",
                      opened="2026-07-10T00:00:00Z"),
        Status.MONITORING, updated="2026-07-28T09:00:00Z",
        monitoring=MonitoringWindow(ErrorAbsent("TNS-12564"),
                                    "2026-07-28T09:00:00Z", MONITORED_UNTIL)))


def _publish_facts(state_dir, contradictions=()):
    facts = MonitoringFacts(
        schema_version=MONITORING_SCHEMA_VERSION, incident=MONITORED_SLUG,
        db="newdb", signal={"kind": "error_absent", "code": "TNS-12564"},
        window={"start": "2026-07-28T09:00:00Z", "until": MONITORED_UNTIL},
        observed=(), verdict="not_met", contradictions=contradictions,
        evaluated_at="2026-08-01T00:00:00Z", source_revision="")
    p = state_dir / "monitoring" / f"{MONITORED_SLUG}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(facts.to_dict()))
    return SimpleNamespace(get_ledger=lambda: {}, dir=state_dir)


def test_monitoring_card_carries_the_last_verdict_and_its_first_reason(
        wiki, tmp_path):
    _monitored(wiki)
    state = _publish_facts(tmp_path / "state",
                           contradictions=("2026-07-29: TNS-12564 × 3",))
    page = render_daily(wiki, DAY, state=state, link_base=LINK_BASE)
    assert ">Monitoring</span>" in page
    assert (f"monitoring: error_absent TNS-12564 until {MONITORED_UNTIL} "
            "— not_met (2026-07-29: TNS-12564 × 3)") in page


def test_a_monitoring_card_without_facts_says_nothing_extra(wiki, tmp_path):
    _monitored(wiki)
    without_state = render_daily(wiki, DAY, link_base=LINK_BASE)
    empty_state = render_daily(wiki, DAY, link_base=LINK_BASE,
                               state=SimpleNamespace(get_ledger=lambda: {},
                                                     dir=tmp_path / "state"))
    assert ">Monitoring</span>" in without_state
    assert "monitoring: " not in without_state
    assert "monitoring: " not in empty_state


def test_an_open_incident_never_carries_a_monitoring_line(wiki, tmp_path):
    state = _publish_facts(tmp_path / "state")
    page = render_daily(wiki, DAY, state=state, link_base=LINK_BASE)
    assert "monitoring: " not in page


# ---- links -------------------------------------------------------------------

def test_links_point_at_link_base_and_only_at_pages_that_exist(page):
    assert f'href="{LINK_BASE}/digests/cdb1/{DAY}.md"' in page
    assert f'href="{LINK_BASE}/databases/cdb1/journal/2026-07.md"' in page
    assert f'href="{LINK_BASE}/incidents/2026-07-12-cdb1-dataguard.md"' in page
    assert f'href="{LINK_BASE}/reports/{DAY}-1015.md"' in page
    # quiet1 has a digest but no journal page -> plain text, never a dead link
    assert f"{LINK_BASE}/databases/quiet1/journal" not in page
    assert '<span class="nolink">journal 2026-07</span>' in page


RESOLVER = Resolver.from_config({
    "kibana": {"base": "https://kibana.example.invalid",
               "data_views": {"oracle-logs": "dv-oracle"}}})


def test_a_page_rendered_without_a_resolver_is_byte_for_byte_what_it_was(wiki):
    """The default path is the one every existing deployment runs. A resolver
    nobody configured must not move a single byte of it."""
    assert render_daily(wiki, DAY, link_base=LINK_BASE, links=None) \
        == render_daily(wiki, DAY, link_base=LINK_BASE)


def test_a_configured_resolver_adds_an_anchor_to_the_attention_cards(wiki):
    plain = render_daily(wiki, DAY, link_base=LINK_BASE)
    linked = render_daily(wiki, DAY, link_base=LINK_BASE, links=RESOLVER)
    assert "kibana.example.invalid" not in plain
    assert 'href="https://kibana.example.invalid/app/discover' in linked
    digests = day_digests(wiki, DAY)
    attention = tiers(digests)["attention"]
    assert linked.count(">Open exact logs (") == sum(
        min(MAX_DEEP_LINKS,
            sum(len(s.get("notable") or [])
                for s in (digests[db].get("sources") or {}).values()))
        for db in attention)


def test_a_storm_card_links_its_biggest_groups_and_not_all_of_them(wiki):
    """A live storm digest carries dozens of notable groups. A link row longer
    than the card it sits under is not a drilldown, and the digest itself is
    one link away with every group in it."""
    digests = day_digests(wiki, DAY)
    biggest = max(tiers(digests)["attention"],
                  key=lambda db: sum(len(s.get("notable") or []) for s
                                     in (digests[db].get("sources") or {}).values()))
    groups = [g for s in (digests[biggest].get("sources") or {}).values()
              for g in (s.get("notable") or [])]
    assert len(groups) > MAX_DEEP_LINKS
    card = render_daily(wiki, DAY, link_base=LINK_BASE,
                        links=RESOLVER).split(f">{biggest}<")[1]
    assert card.split("</article>")[0].count(">Open exact logs (") \
        == MAX_DEEP_LINKS


def test_an_unresolvable_reference_leaves_the_page_exactly_as_it_was(wiki):
    """Available links only. A daily page is generated once and read later, so
    a state badge here would describe the moment of rendering."""
    unmapped = Resolver.from_config({"kibana": {"base": "https://k.invalid"}})
    assert render_daily(wiki, DAY, link_base=LINK_BASE, links=unmapped) \
        == render_daily(wiki, DAY, link_base=LINK_BASE)


def test_write_daily_threads_the_resolver_into_every_page_it_writes(wiki):
    written = write_daily(wiki, DAY, link_base=LINK_BASE, links=RESOLVER)
    assert "kibana.example.invalid" in written[0].read_text()


def test_missing_fleet_report_is_stated_not_linked(wiki):
    (wiki / "reports" / f"{DAY}-1015.md").unlink()
    p = render_daily(wiki, DAY, link_base=LINK_BASE)
    assert "no fleet report for this day" in p
    assert "/reports/" not in p


def test_day_navigation_links_only_existing_neighbours(wiki):
    (wiki / "html").mkdir()
    (wiki / "html" / "2026-07-27.html").write_text("<!-- prev -->")
    p = render_daily(wiki, DAY, link_base=LINK_BASE)
    assert 'href="2026-07-27.html"' in p
    assert 'href="index.html"' in p
    assert 'class="disabled">later' in p
    (wiki / "html" / "2026-07-29.html").write_text("<!-- next -->")
    p2 = render_daily(wiki, DAY, link_base=LINK_BASE)
    assert 'href="2026-07-29.html"' in p2
    assert 'class="disabled"' not in p2


# ---- escaping and determinism ------------------------------------------------

def test_interpolated_text_is_escaped(wiki):
    state = FakeState({f"digests/cdb1/{DAY}.json": {
        "status": "ingested",
        "summary": '<script>alert("x")</script> & <b>bold</b>'}})
    (wiki / "incidents" / "2026-07-12-cdb1-dataguard.md").write_text(
        incident_page("cdb1", "<img src=x onerror=y> incident",
                      opened="2026-07-12T00:00:00Z", body="Still open."))
    p = render_daily(wiki, DAY, state=state, link_base=LINK_BASE)
    assert "<script>" not in p
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; " in p
    assert "<img src=x" not in p
    assert "&lt;img src=x onerror=y&gt; incident" in p


def test_render_is_deterministic(wiki):
    a = render_daily(wiki, DAY, state=LEDGER, link_base=LINK_BASE)
    b = render_daily(wiki, DAY, state=LEDGER, link_base=LINK_BASE)
    assert a == b
    assert render_index(wiki) == render_index(wiki)


def test_render_carries_no_timestamp(page):
    assert "generated at" not in page.lower()
    assert "2026-07-28T" not in page


# ---- index -------------------------------------------------------------------

def test_index_lists_days_newest_first_with_mini_stats(wiki):
    _place(wiki, "cdb1", "listener_only_routine_day", day="2026-07-27")
    write_daily(wiki, "2026-07-27", link_base=LINK_BASE)
    write_daily(wiki, DAY, state=LEDGER, link_base=LINK_BASE)
    idx = (wiki / "html" / "index.html").read_text()
    assert idx.index(f'>{DAY}</a>') < idx.index('>2026-07-27</a>')
    assert f'href="{DAY}.html"' in idx
    assert "<b>6</b> databases" in idx        # the fake day's six digests
    assert "<b>3</b> notable" in idx
    assert "<b>1</b> open incident" in idx  # singular: counts are pluralized


def test_index_is_regenerated_on_every_render(wiki):
    write_daily(wiki, DAY, link_base=LINK_BASE)
    assert "2026-07-29" not in (wiki / "html" / "index.html").read_text()
    _place(wiki, "cdb1", "listener_only_routine_day", day="2026-07-29")
    write_daily(wiki, "2026-07-29", link_base=LINK_BASE)
    idx = (wiki / "html" / "index.html").read_text()
    assert "2026-07-29" in idx and DAY in idx


def test_empty_index_says_so(tmp_path):
    root = tmp_path / "bare"
    (root / "incidents").mkdir(parents=True)
    assert "No daily summaries have been rendered yet." in render_index(root)


def test_day_stats_and_available_days(wiki):
    assert day_stats(wiki, DAY) == {"day": DAY, "dbs": 6, "notable": 3,
                                    "incidents": 1}
    (wiki / "html").mkdir()
    (wiki / "html" / "index.html").write_text("")
    (wiki / "html" / "2026-07-27.html").write_text("")
    assert available_days(wiki) == ["2026-07-27"]
    assert available_days(wiki, DAY) == ["2026-07-27", DAY]


def test_write_daily_returns_both_paths(wiki):
    paths = write_daily(wiki, DAY, state=LEDGER, link_base=LINK_BASE)
    assert [p.name for p in paths] == [f"{DAY}.html", "index.html"]
    assert all(p.exists() for p in paths)


def test_write_daily_refreshes_the_previous_days_next_link(wiki):
    _place(wiki, "cdb1", "listener_only_routine_day", day="2026-07-27")
    write_daily(wiki, "2026-07-27", link_base=LINK_BASE)
    prev = wiki / "html" / "2026-07-27.html"
    assert 'class="disabled">later' in prev.read_text()
    paths = write_daily(wiki, DAY, link_base=LINK_BASE)
    assert [p.name for p in paths] == [f"{DAY}.html", "2026-07-27.html",
                                       "index.html"]
    assert f'href="{DAY}.html"' in prev.read_text()


# ---- orchestrator / CLI wiring ----------------------------------------------

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def git_wiki(wiki):
    git(wiki, "init")
    git(wiki, "config", "user.email", "test@test")
    git(wiki, "config", "user.name", "test")
    (wiki / "log.md").write_text("# log\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    return wiki


@pytest.fixture
def orch(git_wiki, tmp_path):
    # pinned namespace, never fixture_config(): the orchestrator must not pick
    # up the real adapter/mode or the real state dir
    cfg = SimpleNamespace(wiki_repo=git_wiki, state_dir=tmp_path / "state",
                          agents={}, report={"link_base": LINK_BASE},
                          research={}, portal={})
    return Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def _log(repo):
    return subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                          capture_output=True, text=True).stdout


def test_render_html_writes_and_commits_the_day_and_index(orch, git_wiki):
    sha = orch.render_html(DAY)
    assert sha not in ("(nothing to commit)", "(render failed)")
    assert (git_wiki / "html" / f"{DAY}.html").exists()
    assert (git_wiki / "html" / "index.html").exists()
    assert f"html: daily summary {DAY}" in _log(git_wiki)
    assert LINK_BASE in (git_wiki / "html" / f"{DAY}.html").read_text()
    from dbwiki.gitutil import changed_paths
    assert changed_paths(git_wiki) == ()


def test_render_html_commits_pending_digests_first(orch, git_wiki):
    _place(git_wiki, "cdb1", "listener_only_routine_day", day="2026-07-29")
    orch.render_html("2026-07-29")
    log = _log(git_wiki)
    assert "digest: compactor output" in log
    assert "html: daily summary 2026-07-29" in log


def test_render_html_is_idempotent(orch, git_wiki):
    orch.render_html(DAY)
    assert orch.render_html(DAY) == "(nothing to commit)"


def test_render_html_never_raises(orch, git_wiki, monkeypatch):
    monkeypatch.setattr(dh, "write_daily",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert orch.render_html(DAY) == "(render failed)"


def test_html_is_invisible_to_the_stray_check(orch, git_wiki):
    (git_wiki / "html").mkdir(exist_ok=True)
    (git_wiki / "html" / f"{DAY}.html").write_text("stray")
    (git_wiki / "digests" / "cdb1" / f"{DAY}.json").write_text("{}")
    assert transaction.stray_paths(git_wiki) == ()
    (git_wiki / "log.md").write_text("# log\nagent edit\n")
    with pytest.raises(RuntimeError, match="digests/, html/"):
        raise orchestrate._dirty_tree(transaction.stray_paths(git_wiki))


def test_restore_preserves_generated_html(orch, git_wiki):
    base = transaction.head(git_wiki)
    (git_wiki / "html").mkdir(exist_ok=True)
    (git_wiki / "html" / f"{DAY}.html").write_text("rendered")
    (git_wiki / "incidents" / "stray.md").write_text("agent junk")
    transaction.restore(git_wiki, transaction.stray_paths(git_wiki), base)
    assert (git_wiki / "html" / f"{DAY}.html").read_text() == "rendered"
    assert not (git_wiki / "incidents" / "stray.md").exists()


def test_validate_rejects_an_agent_claiming_to_touch_html(orch, git_wiki):
    (git_wiki / "html").mkdir(exist_ok=True)
    (git_wiki / "html" / f"{DAY}.html").write_text("x")
    (git_wiki / "log.md").write_text("# log\nentry\n")
    problems = orch._validate(
        {"task": "ingest", "summary": "s", "notable": False,
         "pages_touched": ["log.md", f"html/{DAY}.html"]}, "ingest",
        transaction.stray_paths(git_wiki))
    assert any("touched generated html" in p for p in problems)


def test_lint_ignores_html(orch, git_wiki):
    (git_wiki / "html").mkdir(exist_ok=True)
    (git_wiki / "html" / f"{DAY}.html").write_text("<!DOCTYPE html>\n")
    (git_wiki / "log.md").write_text("# log\nentry\n")
    proposal = orch._propose(transaction.head(git_wiki), "lint")
    assert proposal.paths == ("log.md",)
    assert orch._blocked(proposal) == []


def test_lint_wiki_walks_markdown_only(git_wiki):
    from dbwiki.lint import lint_wiki
    write_daily(git_wiki, DAY, link_base=LINK_BASE)
    files = {f.file for f in lint_wiki(git_wiki)}
    assert not any(f.startswith("html/") for f in files)


def test_delta_line_caps_long_code_lists():
    d = {"deltas": [{"type": "first_ever_code", "source": "alert",
                     "value": f"ORA-{i:05d}"} for i in range(9)]}
    line, = delta_lines(d)
    assert line.startswith("first-ever error codes: ORA-00000")
    assert line.endswith("(+3 more)")


def test_run_renders_html_even_when_nothing_was_ingested(tmp_path, monkeypatch):
    """The `run` tick must refresh the operator's page on every pass, not only
    when a database woke an agent up."""
    from dbwiki import cli, compactor, orchestrate
    calls = []
    cfg = SimpleNamespace(state_dir=tmp_path / "state", root=tmp_path,
                          wiki_repo=tmp_path / "wiki", alerts={})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "_alerts", lambda *a, **k: {})
    monkeypatch.setattr(compactor, "Compactor", lambda c: SimpleNamespace(
        discover_dbs=lambda *a, **k: [], es=None))
    monkeypatch.setattr(orchestrate, "Orchestrator", lambda c, lock=None: SimpleNamespace(
        adapter="none", telemetry_errors=[], last_telemetry={},
        state=SimpleNamespace(get_ledger=lambda: {}),
        render_html=lambda day: calls.append(day) or "abc1234"))
    assert cli.main(["run"]) == 0
    assert calls == [dt.datetime.now(dt.timezone.utc).date().isoformat()]


def test_cli_render_daily_writes_the_page(tmp_path, wiki, monkeypatch):
    from dbwiki import cli
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          report={"link_base": LINK_BASE}, portal={})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["render-daily", "--day", DAY]) == 0
    assert (wiki / "html" / f"{DAY}.html").exists()
    assert (wiki / "html" / "index.html").exists()


def test_cli_render_daily_rejects_a_bad_day(tmp_path, wiki, monkeypatch, capsys):
    from dbwiki import cli
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          report={}, portal={})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    assert cli.main(["render-daily", "--day", "yesterday"]) == 2
    assert "YYYY-MM-DD" in capsys.readouterr().err


def test_cli_render_daily_is_silent_on_success(tmp_path, wiki, monkeypatch,
                                               capsys):
    from dbwiki import cli
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          report={}, portal={})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    cli.main(["render-daily", "--day", DAY])
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
