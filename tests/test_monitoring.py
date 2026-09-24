"""Deterministic monitoring evaluation: one recovery signal, the digests
covering its window, and the verdict that falls out.

Digests are built by hand to the shape `Compactor.compact` assembles (see
compactor.py), and the monitoring pages go through the real
`incidents.set_status` codec, so a change to either shape breaks these tests
rather than quietly changing what an operator is told. No Elasticsearch, no
LLM, no network: the one live read is faked and asserted on.
"""

import datetime as dt
import json
import subprocess
import time
from functools import partial

import fixtures as fx
import pytest
from fixtures.incident_pages import incident_page

from dbwiki import gitutil, monitoring, transaction
from dbwiki.incidents import (ErrorAbsent, EventPresent, FlowResumed, Manual,
                              MonitoringWindow, Status, read_incident,
                              set_status)
from dbwiki.monitoring import (ERROR, INSUFFICIENT, MAX_WINDOW_DAYS, MET,
                               MONITORING_SCHEMA_VERSION, NOT_MET,
                               PatternTimeout, closure_case, evaluate,
                               evaluate_all, load_window_digests,
                               match_messages, read_facts, window_day_count,
                               window_days)

DB = "cdb1"
SLUG = "2026-08-30-cdb1-transport"
PATH = f"incidents/{SLUG}.md"
CODE = "TNS-12564"
START = "2026-08-30T14:00:00Z"
UNTIL = "2026-09-02T00:00:00Z"
INSIDE = "2026-08-31T09:00:00Z"
BEFORE = "2026-08-30T06:00:00Z"
DURING = "2026-09-01T12:00:00Z"
AFTER = "2026-09-02T01:00:00Z"


def group(*, codes=(), count=3, message="TNS-12564: destination unreachable",
          rule="tns_error", first_ts=INSIDE, last_ts=None):
    return {"rule": rule, "class": "error", "count": count,
            "first_ts": first_ts, "last_ts": last_ts or first_ts,
            "codes": list(codes), "message": message, "template": message,
            "es_samples": []}


def digest(day, *, notable=(), total=0, deltas=(), source="alert", db=DB):
    return {
        "db": db,
        "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T23:59:00Z",
                   "day": day},
        "generated_by": "dbwiki-compactor/test",
        "pattern_versions": {source: 1},
        "sources": {source: {"total_events": total, "by_class": {},
                             "routine_counters": {}, "notable": list(notable),
                             "dropped_notable_groups": {}}},
        "deltas": list(deltas),
        "totals": {"events": total, "notable_events": 0,
                   "notable_groups": len(notable)},
        "notable": bool(notable or deltas),
        "_path": f"digests/{db}/{day}.json",
    }


def incident(signal, *, start=START, until=UNTIL, status=Status.MONITORING,
             db=DB, path=PATH):
    text = set_status(
        incident_page(db, "dataguard transport failure",
                      opened="2026-08-30T00:00:00Z", body="Watching."),
        status, updated=start,
        monitoring=MonitoringWindow(signal, start, until))
    return read_incident(text, path)


WINDOW_DAYS = ("2026-08-30", "2026-08-31", "2026-09-01")


def full(on=None):
    """A digest for every day of the default window. A verdict that rests on
    absence needs the whole window examined, so a test aiming at one has to
    supply it rather than rely on a single day."""
    on = on or {}
    return [on.get(d, digest(d)) for d in WINDOW_DAYS]


def verdict(signal, digests, *, now=DURING, match=match_messages, **kw):
    return evaluate(incident(signal, **kw), digests, now=now, match=match)


def test_window_days_covers_every_day_the_window_touches():
    assert window_days(MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)) == [
        "2026-08-30", "2026-08-31", "2026-09-01"]


def test_a_second_past_midnight_reaches_into_the_next_day():
    days = window_days(MonitoringWindow(ErrorAbsent(CODE), START,
                                        "2026-09-02T00:00:01Z"))
    assert days[-1] == "2026-09-02"


def test_a_window_inside_one_day_is_one_day():
    assert window_days(MonitoringWindow(ErrorAbsent(CODE), START,
                                        "2026-08-30T18:00:00Z")) == \
        ["2026-08-30"]


def test_an_absurd_until_is_capped_rather_than_walked():
    window = MonitoringWindow(ErrorAbsent(CODE), START, "2999-01-01T00:00:00Z")
    assert len(window_days(window)) == MAX_WINDOW_DAYS
    assert window_day_count(window) > MAX_WINDOW_DAYS


def test_a_window_exactly_at_the_cap_is_not_truncated():
    until = (dt.date.fromisoformat(START[:10])
             + dt.timedelta(days=MAX_WINDOW_DAYS)).isoformat()
    window = MonitoringWindow(ErrorAbsent(CODE), START, f"{until}T00:00:00Z")
    assert window_day_count(window) == MAX_WINDOW_DAYS
    assert len(window_days(window)) == MAX_WINDOW_DAYS


def test_a_digest_outside_the_window_is_not_examined():
    facts = verdict(ErrorAbsent(CODE), [digest("2026-09-02"),
                                        digest("2026-08-31")])
    assert [o["day"] for o in facts.observed] == ["2026-08-31"]


def test_an_occurrence_in_the_window_refutes_error_absent():
    facts = verdict(ErrorAbsent(CODE),
                    [digest("2026-08-31", notable=[group(codes=[CODE],
                                                         count=7)])])
    assert facts.verdict == NOT_MET
    assert facts.contradictions == (f"2026-08-31: {CODE} × 7",)
    assert facts.observed[0]["count"] == 7


def test_an_occurrence_before_the_window_started_does_not_refute_it():
    d = digest("2026-08-30", notable=[group(codes=[CODE], first_ts=BEFORE,
                                            last_ts=BEFORE)])
    facts = verdict(ErrorAbsent(CODE), full({"2026-08-30": d}), now=AFTER)
    assert facts.verdict == MET
    assert facts.observed[0]["count"] == 0


def test_a_group_straddling_the_start_still_refutes_it():
    d = digest("2026-08-30", notable=[group(codes=[CODE], first_ts=BEFORE,
                                            last_ts=INSIDE)])
    assert verdict(ErrorAbsent(CODE), [d]).verdict == NOT_MET


def test_a_clean_closed_window_meets_error_absent():
    other = digest("2026-08-31", notable=[group(codes=["ORA-1"])])
    facts = verdict(ErrorAbsent(CODE), full({"2026-08-31": other}), now=AFTER)
    assert facts.verdict == MET
    assert facts.contradictions == ()


def test_a_clean_open_window_is_not_yet_enough():
    facts = verdict(ErrorAbsent(CODE), full(), now=DURING)
    assert facts.verdict == INSUFFICIENT
    assert facts.contradictions == ()


def test_no_digest_covering_the_window_never_meets_a_signal():
    facts = verdict(ErrorAbsent(CODE), [], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert facts.contradictions == (
        "no digest covers 2026-08-30, 2026-08-31, 2026-09-01",)


def test_one_missing_window_day_keeps_a_clean_closed_window_insufficient():
    facts = verdict(ErrorAbsent(CODE),
                    [digest("2026-08-30"), digest("2026-08-31")], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert facts.contradictions == ("no digest covers 2026-09-01",)


def test_a_coverage_gap_never_softens_a_real_occurrence():
    d = digest("2026-08-31", notable=[group(codes=[CODE])])
    assert verdict(ErrorAbsent(CODE), [d], now=AFTER).verdict == NOT_MET


def test_a_window_past_the_cap_can_never_be_met():
    until = "2027-06-01T00:00:00Z"
    days = window_days(MonitoringWindow(ErrorAbsent(CODE), START, until))
    facts = verdict(ErrorAbsent(CODE), [digest(d) for d in days],
                    now="2027-07-01T00:00:00Z", until=until)
    assert facts.verdict == INSUFFICIENT
    assert "evaluation limit" in facts.contradictions[0]


def test_a_padded_code_matches_the_digests_normalised_spelling():
    d = digest("2026-08-31", notable=[group(codes=["TNS-513"], count=40)])
    facts = verdict(ErrorAbsent("TNS-00513"), full({"2026-08-31": d}),
                    now=AFTER)
    assert facts.verdict == NOT_MET
    assert facts.contradictions == ("2026-08-31: TNS-00513 × 40",)


def test_an_unpadded_code_matches_a_padded_digest_entry():
    d = digest("2026-08-31", notable=[group(codes=["ORA-00600"])])
    assert verdict(ErrorAbsent("ORA-600"), full({"2026-08-31": d}),
                   now=AFTER).verdict == NOT_MET


def test_a_first_ever_code_delta_counts_when_no_group_survives():
    d = digest("2026-08-31", deltas=[{"type": "first_ever_code",
                                      "source": "alert", "value": CODE,
                                      "first_seen": INSIDE}])
    facts = verdict(ErrorAbsent(CODE), [d], now=AFTER)
    assert facts.verdict == NOT_MET
    assert facts.contradictions == (f"2026-08-31: {CODE} × 1",)


def test_a_first_ever_code_delta_from_before_the_window_is_ignored():
    d = digest("2026-08-30", deltas=[{"type": "first_ever_code",
                                      "source": "alert", "value": CODE,
                                      "first_seen": BEFORE}])
    assert verdict(ErrorAbsent(CODE), full({"2026-08-30": d}),
                   now=AFTER).verdict == MET


def test_an_event_in_the_same_second_as_start_is_inside_the_window():
    d = digest("2026-08-30", notable=[group(codes=[CODE], first_ts=START,
                                            last_ts=START)])
    assert verdict(ErrorAbsent(CODE),
                   full({"2026-08-30": d})).verdict == NOT_MET


def test_an_event_in_the_same_second_as_until_is_outside_it():
    d = digest("2026-09-01", notable=[group(codes=[CODE], first_ts=UNTIL,
                                            last_ts=UNTIL)])
    assert verdict(ErrorAbsent(CODE), full({"2026-09-01": d}),
                   now=AFTER).verdict == MET


def test_now_exactly_at_until_closes_the_window():
    assert verdict(ErrorAbsent(CODE), full(), now=UNTIL).verdict == MET


def test_millisecond_digest_timestamps_compare_against_whole_second_bounds():
    ts = "2026-08-30T14:00:00.816Z"
    d = digest("2026-08-30",
               notable=[group(codes=[CODE], first_ts=ts, last_ts=ts)])
    assert verdict(ErrorAbsent(CODE), [d]).verdict == NOT_MET


def test_a_matching_message_meets_event_present_while_the_window_is_open():
    d = digest("2026-08-31",
               notable=[group(message="MRP0: Managed Recovery starting")])
    facts = verdict(EventPresent("Managed Recovery"), [d], now=DURING)
    assert facts.verdict == MET
    assert facts.observed[0]["group"] == "tns_error"


def test_no_match_on_a_closed_window_refutes_event_present():
    d = digest("2026-08-31", notable=[group()])
    facts = verdict(EventPresent("Managed Recovery"), full({"2026-08-31": d}),
                    now=AFTER)
    assert facts.verdict == NOT_MET
    assert "Managed Recovery" in facts.contradictions[0]


def test_no_match_on_an_open_window_is_not_yet_enough():
    d = digest("2026-08-31", notable=[group()])
    assert verdict(EventPresent("Managed Recovery"), full({"2026-08-31": d}),
                   now=DURING).verdict == INSUFFICIENT


def test_a_missing_day_stops_event_present_being_refuted():
    facts = verdict(EventPresent("Managed Recovery"),
                    [digest("2026-08-30"), digest("2026-08-31")], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert facts.contradictions == ("no digest covers 2026-09-01",)


def test_a_match_from_before_the_window_does_not_count():
    d = digest("2026-08-30", notable=[group(message="MRP0 started",
                                            first_ts=BEFORE, last_ts=BEFORE)])
    assert verdict(EventPresent("MRP0"), full({"2026-08-30": d}),
                   now=AFTER).verdict == NOT_MET


def test_a_pattern_that_does_not_compile_is_reported_not_raised():
    facts = verdict(EventPresent("MRP0("), [digest("2026-08-31")], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert "does not compile" in facts.contradictions[0]


CATASTROPHIC = "(a+)+$"

TIMED_OUT = "the pattern ran out of time on 3 digest messages"


def timing_out(pattern, messages):
    raise PatternTimeout(TIMED_OUT)


def test_a_pattern_that_cannot_finish_is_an_error_verdict_not_a_wedge():
    """The tick evaluates under the single-flight lock, so a pattern that
    never returns used to hold every later tick as well. It comes back as a
    verdict now, and the record still says which signal produced it."""
    facts = verdict(EventPresent(CATASTROPHIC),
                    full({"2026-08-31": digest("2026-08-31",
                                               notable=[group()])}),
                    now=AFTER, match=timing_out)
    assert facts.verdict == ERROR
    assert facts.contradictions == (TIMED_OUT,)
    assert facts.incident == SLUG
    assert facts.db == DB
    assert facts.signal == {"kind": "event_present", "pattern": CATASTROPHIC}
    assert facts.window == {"start": START, "until": UNTIL}


def test_every_digest_is_matched_in_one_call_to_the_injected_matcher():
    """`evaluate` stays a pure function of its inputs by asking an injected
    matcher, exactly as it does for `probe`. One call covers the whole
    evaluation, so a 92-day window pays for one child process, not 92."""
    calls = []

    def matcher(pattern, messages):
        calls.append((pattern, list(messages)))
        return [False, True]

    facts = verdict(
        EventPresent("MRP0"),
        full({"2026-08-30": digest("2026-08-30",
                                   notable=[group(message="one", rule="r1")]),
              "2026-08-31": digest("2026-08-31",
                                   notable=[group(message="two",
                                                  rule="r2")])}),
        now=AFTER, match=matcher)

    assert calls == [("MRP0", ["one", "two"])]
    assert facts.verdict == MET
    assert [(o["matched"], o["group"]) for o in facts.observed] == [
        (False, ""), (True, "r2"), (False, "")]


def test_a_catastrophic_pattern_is_killed_at_its_timeout():
    """`re` cannot be interrupted, not by a signal and not by a thread, so
    the search runs in a child the parent can kill. In process this pattern
    did not return within a minute."""
    started = time.monotonic()
    with pytest.raises(PatternTimeout) as caught:
        match_messages(CATASTROPHIC, ["a" * 40 + "!"], timeout=0.5)
    assert time.monotonic() - started < 5
    assert str(caught.value) == ("pattern '(a+)+$' did not finish matching "
                                 "1 digest message within 0.5s")


def test_match_messages_says_which_messages_the_pattern_matched():
    assert match_messages("MRP0", ["MRP0 started",
                                   "TNS-12564: destination unreachable",
                                   "background MRP0 exiting"]) == (
        True, False, True)


def test_no_messages_at_all_are_answered_without_starting_a_child(monkeypatch):
    def unreachable(*args, **kwargs):
        raise AssertionError("a child process was started for no messages")

    monkeypatch.setattr(monitoring.subprocess, "Popen", unreachable)
    assert match_messages("MRP0", []) == ()


def test_one_window_day_with_events_meets_flow_resumed():
    facts = verdict(FlowResumed("alert"),
                    [digest("2026-08-31", total=0),
                     digest("2026-09-01", total=12)], now=DURING)
    assert facts.verdict == MET
    assert [o["events"] for o in facts.observed] == [0, 12]


def test_events_before_the_window_start_are_not_recovery():
    """Listener died 03:00, monitoring armed at 14:00: the start day's
    pre-window events used to meet flow_resumed although the source never
    came back."""
    before = group(first_ts="2026-08-30T00:01:00Z",
                   last_ts="2026-08-30T02:59:00Z", count=900)
    facts = verdict(FlowResumed("alert"),
                    full({"2026-08-30": digest("2026-08-30", total=900,
                                               notable=[before])}),
                    now=AFTER)
    assert facts.verdict == NOT_MET
    assert [o["events"] for o in facts.observed] == [0, 0, 0]


def test_in_window_events_on_the_start_day_meet_flow_resumed():
    after = group(first_ts="2026-08-30T15:00:00Z",
                  last_ts="2026-08-30T16:00:00Z", count=7)
    facts = verdict(FlowResumed("alert"),
                    [digest("2026-08-30", total=7, notable=[after])],
                    now=DURING)
    assert facts.verdict == MET
    assert facts.observed[0]["events"] == 7


def test_start_day_events_that_cannot_be_placed_do_not_refute():
    """Routine lines keep no timestamp in a digest: on a partly-covered day
    they may be inside the window, so silence cannot be concluded — but they
    do not prove recovery either."""
    facts = verdict(FlowResumed("alert"),
                    full({"2026-08-30": digest("2026-08-30", total=40)}),
                    now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert any("cannot be placed" in c for c in facts.contradictions)


def test_silence_across_a_closed_window_refutes_flow_resumed():
    facts = verdict(FlowResumed("alert"), full(), now=AFTER)
    assert facts.verdict == NOT_MET
    assert "alert produced no events" in facts.contradictions[0]


def test_silence_across_an_open_window_is_not_yet_enough():
    assert verdict(FlowResumed("alert"), full(), now=DURING).verdict == \
        INSUFFICIENT


def test_a_missing_day_stops_flow_resumed_being_refuted():
    facts = verdict(FlowResumed("alert"), [digest("2026-08-30")], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert "no digest covers" in facts.contradictions[0]


def test_a_source_no_digest_collects_is_unknown_not_silent():
    facts = verdict(FlowResumed("dataguar"), full(), now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert "not a source in any digest" in facts.contradictions[0]


def test_manual_is_never_settled_by_machine():
    facts = verdict(Manual("standby has caught up"),
                    [digest("2026-08-31", total=99)], now=AFTER)
    assert facts.verdict == INSUFFICIENT
    assert facts.observed == ()
    assert "a human judges" in facts.contradictions[0]


def test_the_record_carries_the_signal_window_and_identity():
    facts = verdict(ErrorAbsent(CODE), [digest("2026-08-31")])
    assert facts.schema_version == MONITORING_SCHEMA_VERSION
    assert facts.incident == SLUG
    assert facts.db == DB
    assert facts.signal == {"kind": "error_absent", "code": CODE}
    assert facts.window == {"start": START, "until": UNTIL}
    assert facts.evaluated_at == DURING


def test_the_record_survives_a_json_round_trip_with_every_field():
    facts = verdict(ErrorAbsent(CODE), full())
    assert json.loads(json.dumps(facts.to_dict())) == facts.to_dict()
    assert set(facts.to_dict()) == {
        "schema_version", "incident", "db", "signal", "window", "observed",
        "verdict", "contradictions", "evaluated_at", "source_revision"}


def test_an_incident_with_no_window_cannot_be_evaluated():
    inc = read_incident(incident_page(DB, "still open"), PATH)
    with pytest.raises(ValueError, match="no monitoring window"):
        evaluate(inc, [], now=DURING)


@pytest.fixture
def wiki(tmp_path):
    """A wiki holding one monitoring incident and two of its three window
    days, the newest of them missing."""
    root = tmp_path / "wiki"
    (root / "incidents").mkdir(parents=True)
    (root / "incidents" / f"{SLUG}.md").write_text(
        set_status(incident_page(DB, "dataguard transport failure",
                                 opened="2026-08-30T00:00:00Z"),
                   Status.MONITORING, updated=START,
                   monitoring=MonitoringWindow(ErrorAbsent(CODE), START,
                                               UNTIL)))
    for day in ("2026-08-30", "2026-08-31"):
        p = root / "digests" / DB / f"{day}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(digest(day)))
    return root


def test_load_window_digests_reads_the_window_days_and_tags_the_path(wiki):
    window = MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)
    loaded = load_window_digests(wiki, DB, window)
    assert [d["window"]["day"] for d in loaded] == ["2026-08-30", "2026-08-31"]
    assert loaded[0]["_path"] == f"digests/{DB}/2026-08-30.json"


def test_an_unreadable_digest_is_missing_evidence_not_a_crash(wiki):
    (wiki / "digests" / DB / "2026-08-31.json").write_text("{ not json")
    window = MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)
    assert [d["window"]["day"]
            for d in load_window_digests(wiki, DB, window)] == ["2026-08-30"]


class FakeES:
    """Records every count call. The first answers `hits`, the rest zero, so
    a test asserts one total whatever the config's source count is."""

    def __init__(self, hits=0, fail=False):
        self.hits = hits
        self.fail = fail
        self.calls = []

    def count(self, index, query):
        self.calls.append((index, query))
        if self.fail:
            raise RuntimeError("elasticsearch is down")
        return self.hits if len(self.calls) == 1 else 0


@pytest.fixture
def cfg(tmp_path):
    return fx.fixture_config(tmp_path)


def test_evaluate_all_publishes_one_facts_file_per_monitoring_incident(
        cfg, wiki, tmp_path):
    state = tmp_path / "state"
    facts = evaluate_all(cfg, wiki, state, now=DURING)
    assert [f.incident for f in facts] == [SLUG]
    assert read_facts(state, SLUG)["verdict"] == INSUFFICIENT
    assert read_facts(state, "no-such-incident") is None


def test_evaluate_all_ignores_incidents_that_are_not_monitoring(
        cfg, wiki, tmp_path):
    (wiki / "incidents" / "2026-08-29-cdb1-open.md").write_text(
        incident_page(DB, "still open"))
    (wiki / "incidents" / "2026-08-28-cdb1-done.md").write_text(
        incident_page(DB, "over", status="resolved"))
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING)
    assert [f.incident for f in facts] == [SLUG]


def test_a_page_that_stopped_monitoring_loses_its_stale_verdict(
        cfg, wiki, tmp_path):
    state = tmp_path / "state"
    evaluate_all(cfg, wiki, state, now=DURING)
    (state / "monitoring" / "gone.json").write_text("{}")
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(set_status(page.read_text(), Status.RESOLVED,
                               updated=AFTER))
    assert evaluate_all(cfg, wiki, state, now=AFTER) == []
    assert sorted((state / "monitoring").glob("*.json")) == []


def test_evaluate_all_rewrites_the_same_bytes_on_a_second_run(
        cfg, wiki, tmp_path):
    state = tmp_path / "state"
    evaluate_all(cfg, wiki, state, now=DURING)
    first = (state / "monitoring" / f"{SLUG}.json").read_text()
    evaluate_all(cfg, wiki, state, now=DURING)
    assert (state / "monitoring" / f"{SLUG}.json").read_text() == first


def test_the_probe_answers_for_the_newest_window_day_with_no_digest(
        cfg, wiki, tmp_path):
    es = FakeES(hits=4)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)[0]
    assert es.calls
    assert facts.verdict == NOT_MET
    probe = facts.observed[-1]
    assert probe["via"] == "elasticsearch"
    assert probe == {"day": "2026-09-01", "code": CODE, "count": 4,
                     "via": "elasticsearch", "from": "2026-09-01T00:00:00Z",
                     "to": DURING}


def test_a_quiet_probe_leaves_the_window_open_rather_than_met(
        cfg, wiki, tmp_path):
    es = FakeES(hits=0)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)[0]
    assert facts.verdict == INSUFFICIENT
    assert facts.observed[-1]["count"] == 0
    assert facts.contradictions == ("no digest covers 2026-09-01",)


def test_a_failing_probe_leaves_the_digest_evidence_to_speak(
        cfg, wiki, tmp_path):
    es = FakeES(fail=True)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)[0]
    assert es.calls
    assert facts.verdict == INSUFFICIENT
    assert [o["day"] for o in facts.observed] == ["2026-08-30", "2026-08-31"]


def test_the_probe_covers_today_not_the_windows_last_day(cfg, wiki, tmp_path):
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(set_status(
        page.read_text(), Status.MONITORING, updated=START,
        monitoring=MonitoringWindow(ErrorAbsent(CODE), START,
                                    "2026-09-05T00:00:00Z")))
    es = FakeES(hits=2)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)[0]
    assert facts.observed[-1]["day"] == "2026-09-01"
    assert facts.observed[-1]["to"] == DURING
    assert facts.verdict == NOT_MET


def test_the_probe_is_not_consulted_when_the_newest_day_has_a_digest(
        cfg, wiki, tmp_path):
    (wiki / "digests" / DB / "2026-09-01.json").write_text(
        json.dumps(digest("2026-09-01")))
    es = FakeES(hits=4)
    evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)
    assert es.calls == []


@pytest.mark.parametrize("signal", [EventPresent("MRP0"),
                                    FlowResumed("alert"),
                                    Manual("a human looks")])
def test_only_error_absent_is_worth_a_live_count(cfg, wiki, tmp_path, signal):
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(set_status(page.read_text(), Status.MONITORING,
                               updated=START,
                               monitoring=MonitoringWindow(signal, START,
                                                           UNTIL)))
    es = FakeES(hits=4)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)
    assert [f.incident for f in facts] == [SLUG]
    assert es.calls == []


def test_a_code_that_is_not_code_shaped_never_reaches_a_query(
        cfg, wiki, tmp_path):
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(set_status(
        page.read_text(), Status.MONITORING, updated=START,
        monitoring=MonitoringWindow(ErrorAbsent('" OR *'), START, UNTIL)))
    es = FakeES(hits=4)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)
    assert facts[0].signal == {"kind": "error_absent", "code": '" OR *'}
    assert es.calls == []


def test_evaluating_never_writes_under_the_wiki(cfg, wiki, tmp_path):
    subprocess.run(["git", "-C", str(wiki), "init", "-q"], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(wiki), "config", k, v], check=True)
    subprocess.run(["git", "-C", str(wiki), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(wiki), "commit", "-qm", "init"],
                   check=True)
    before = transaction.stray_paths(wiki)

    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING)

    assert transaction.stray_paths(wiki) == before
    assert gitutil.changed_paths(wiki) == ()
    assert facts[0].source_revision == transaction.head(wiki)


OTHER = "2026-08-29-cdb2-transport"


def second_incident(wiki, db="cdb2", slug=OTHER):
    """A second monitoring incident, sorting before the fixture's own, so a
    failure on it is a failure the fixture's incident is evaluated after."""
    (wiki / "incidents" / f"{slug}.md").write_text(set_status(
        incident_page(db, "transport failure",
                      opened="2026-08-30T00:00:00Z"),
        Status.MONITORING, updated=START,
        monitoring=MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)))


def test_one_incident_that_raises_does_not_cost_the_others_a_verdict(
        cfg, wiki, tmp_path):
    """A digest whose `sources` is a list crashes the evaluator. Unguarded it
    took every later incident's verdict with it, and skipped the prune."""
    state = tmp_path / "state"
    (state / "monitoring").mkdir(parents=True)
    (state / "monitoring" / "gone.json").write_text("{}")
    second_incident(wiki)
    bad = wiki / "digests" / "cdb2" / "2026-08-30.json"
    bad.parent.mkdir(parents=True)
    bad.write_text(json.dumps({"window": {"day": "2026-08-30"},
                               "sources": ["not a mapping"]}))

    facts = {f.incident: f for f in evaluate_all(cfg, wiki, state, now=DURING)}

    assert facts[OTHER].verdict == ERROR
    assert "AttributeError" in facts[OTHER].contradictions[0]
    assert read_facts(state, OTHER) is None
    assert facts[SLUG].verdict == INSUFFICIENT
    assert read_facts(state, SLUG)["verdict"] == INSUFFICIENT
    assert not (state / "monitoring" / "gone.json").exists()


def test_one_dead_probe_ends_the_runs_probing(cfg, wiki, tmp_path):
    """ES down cost one full probe timeout per monitoring incident, every
    tick. One failure is the whole run's answer."""
    second_incident(wiki)
    es = FakeES(fail=True)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=DURING, es=es)
    assert len(facts) == 2
    assert len(es.calls) == 1


def test_a_quiet_probe_never_closes_a_coverage_gap(cfg, wiki, tmp_path):
    """A zero count from a deleted or expired index is not evidence the day
    was clean. Only a digest covers a window day."""
    es = FakeES(hits=0)
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=AFTER, es=es)[0]
    assert facts.verdict == INSUFFICIENT
    assert facts.contradictions == ("no digest covers 2026-09-01",)
    assert facts.observed[-1]["count"] == 0


def test_a_digest_that_is_not_an_object_is_missing_evidence(wiki):
    (wiki / "digests" / DB / "2026-08-31.json").write_text("[1, 2]")
    window = MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)
    assert [d["window"]["day"]
            for d in load_window_digests(wiki, DB, window)] == ["2026-08-30"]


def test_a_db_that_could_leave_the_digest_directory_reads_nothing(wiki,
                                                                  tmp_path):
    """`digests/<db>/<day>.json` interpolates a value taken off page
    frontmatter, so it is a boundary."""
    (tmp_path / "2026-08-30.json").write_text(json.dumps(digest("2026-08-30")))
    window = MonitoringWindow(ErrorAbsent(CODE), START, UNTIL)
    assert load_window_digests(wiki, "../..", window) == []


def test_a_timed_out_pattern_is_published_not_left_to_the_last_verdict(
        cfg, wiki, tmp_path, monkeypatch):
    """An evaluation that raises writes nothing, so its last good verdict
    stands. A timeout is not that: it is a durable fact about the operator's
    own pattern, and `dbwiki incident show` has to show it."""
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(set_status(
        incident_page(DB, "dataguard transport failure",
                      opened="2026-08-30T00:00:00Z"),
        Status.MONITORING, updated=START,
        monitoring=MonitoringWindow(EventPresent(CATASTROPHIC), START, UNTIL)))
    monkeypatch.setattr(monitoring, "evaluate",
                        partial(monitoring.evaluate, match=timing_out))
    state = tmp_path / "state"

    facts = evaluate_all(cfg, wiki, state, now=DURING)

    assert facts[0].verdict == ERROR
    assert read_facts(state, SLUG)["verdict"] == ERROR


def test_a_db_that_could_leave_the_digest_directory_is_named_in_the_verdict(
        cfg, wiki, tmp_path):
    page = wiki / "incidents" / f"{SLUG}.md"
    page.write_text(page.read_text().replace(f"db: {DB}", "db: ../../etc"))
    facts = evaluate_all(cfg, wiki, tmp_path / "state", now=AFTER)[0]
    assert facts.verdict == INSUFFICIENT
    assert "../../etc" in facts.contradictions[0]


REV = "3f9a1c2b"


def raw_facts(signal, observed, **kw):
    """A facts dict as a partial write or an older schema could leave one, so
    a malformed case does not have to be squeezed through `evaluate`."""
    return {"verdict": INSUFFICIENT, "signal": signal,
            "window": {"start": START, "until": UNTIL},
            "observed": list(observed), "contradictions": [],
            "evaluated_at": DURING, "source_revision": "", **kw}


def case(facts, *, revision=""):
    """The closure case for one evaluation, built from the dict `read_facts`
    hands the portal rather than from the record itself."""
    return closure_case(facts.to_dict(), revision=revision)


def test_a_clean_closed_window_argues_for_closing_on_every_day():
    c = case(verdict(ErrorAbsent(CODE), full(), now=AFTER))
    assert c.verdict == MET
    assert c.supporting == tuple(
        f"{day}: {CODE} absent (digests/{DB}/{day}.md)"
        for day in WINDOW_DAYS)
    assert c.against == ()
    assert c.digests == tuple(f"digests/{DB}/{day}.md"
                              for day in WINDOW_DAYS)


def test_a_closure_line_lands_a_reader_on_a_page_and_not_on_a_blob():
    """The two spellings answer two questions. `observed` cites the
    compacted digest because that is the file the evaluator read; the closure
    case names the page the compactor renders beside it, because the case
    exists so a human can check the verdict and the portal turns the cited
    digests into links. The compacted file is still in the facts the portal
    discloses, so nothing is hidden."""
    facts = verdict(ErrorAbsent(CODE), full(), now=AFTER).to_dict()
    day = WINDOW_DAYS[0]
    assert facts["observed"][0]["digest"] == f"digests/{DB}/{day}.json"

    c = closure_case(facts, revision="")

    assert c.digests[0] == f"digests/{DB}/{day}.md"
    assert c.supporting[0].endswith(f"(digests/{DB}/{day}.md)")


def test_an_occurrence_argues_against_closing_and_supports_nothing():
    d = digest("2026-08-31", notable=[group(codes=[CODE], count=7)])
    c = case(verdict(ErrorAbsent(CODE), full({"2026-08-31": d}), now=AFTER))
    assert c.verdict == NOT_MET
    assert c.against == (f"2026-08-31: {CODE} × 7",)
    assert [line[:10] for line in c.supporting] == ["2026-08-30",
                                                    "2026-09-01"]


def test_a_window_day_no_digest_covers_is_named_after_the_contradictions():
    d = digest("2026-08-30", notable=[group(codes=[CODE], count=7,
                                            first_ts="2026-08-30T18:00:00Z")])
    c = case(verdict(ErrorAbsent(CODE), [d, digest("2026-08-31")],
                     now=AFTER))
    assert c.against == (f"2026-08-30: {CODE} × 7",
                         "2026-09-01: no digest examined")


def test_a_matched_notable_group_argues_for_closing():
    c = case(verdict(EventPresent("destination unreachable"),
                     full({"2026-08-31": digest("2026-08-31",
                                                notable=[group()])}),
                     now=AFTER))
    assert c.verdict == MET
    assert c.supporting == (
        f"2026-08-31: tns_error matched (digests/{DB}/2026-08-31.md)",)
    assert c.against == ()
    assert len(c.digests) == len(WINDOW_DAYS)


def test_a_sighting_early_in_the_window_leaves_the_later_days_out_of_the_case():
    """`event_present` and `flow_resumed` go met on the first sighting,
    normally with window days still ahead of them. `evaluate` is explicit
    that a positive sighting stands on its own and only a verdict resting on
    absence needs the whole window examined, so days nobody looked at argue
    against nothing, and `ClosureCase`'s met-implies-no-against invariant
    holds for the sighting signals too."""
    c = case(verdict(EventPresent("destination unreachable"),
                     [digest(WINDOW_DAYS[0], notable=[group()])], now=AFTER))
    assert c.verdict == MET
    assert c.against == ()
    assert c.supporting == (
        f"{WINDOW_DAYS[0]}: tns_error matched "
        f"(digests/{DB}/{WINDOW_DAYS[0]}.md)",)


def test_events_on_the_source_argue_for_closing():
    c = case(verdict(FlowResumed("alert"),
                     full({"2026-08-31": digest("2026-08-31", total=12)}),
                     now=AFTER))
    assert c.verdict == MET
    assert c.supporting == (f"2026-08-31: alert carried 12 events "
                            f"(digests/{DB}/2026-08-31.md)",)
    assert c.against == ()


def test_the_live_probe_cites_its_source_and_no_digest():
    """The probe is the one observed entry with no digest path. It still
    names a window day, so the gap rule does not repeat the coverage line the
    verdict already carries."""
    probe = {"day": "2026-09-01", "code": CODE, "count": 0,
             "via": "elasticsearch", "from": "2026-09-01T00:00:00Z",
             "to": DURING}
    facts = evaluate(incident(ErrorAbsent(CODE)),
                     [digest("2026-08-30"), digest("2026-08-31")],
                     now=DURING, probe=probe)
    c = closure_case(facts.to_dict(), revision="")
    assert c.supporting[-1] == (f"2026-09-01: {CODE} absent "
                                f"(via elasticsearch)")
    assert c.digests == (f"digests/{DB}/2026-08-30.md",
                         f"digests/{DB}/2026-08-31.md")
    assert c.against == ("no digest covers 2026-09-01",)


def test_a_manual_signal_leaves_the_whole_case_to_a_human():
    """The uniform gap rule would bury the one honest line under one missing
    day per window day. A `manual` signal keeps no per-day entries, so it has
    no per-day coverage that can be missing."""
    c = case(verdict(Manual("standby has caught up"), full(), now=AFTER))
    assert c.supporting == ()
    assert c.against == (
        "manual: standby has caught up; only a human judges this",)


def test_staleness_is_whether_the_verdict_answers_the_revision_shown():
    facts = evaluate(incident(ErrorAbsent(CODE)), full(), now=AFTER,
                     source_revision=REV).to_dict()
    fresh = closure_case(facts, revision=REV)
    assert (fresh.stale, fresh.source_revision, fresh.evaluated_at) == \
        (False, REV, AFTER)
    assert closure_case(facts, revision="0000000").stale is True


def test_a_file_with_no_usable_verdict_reads_as_insufficient():
    c = closure_case({}, revision=REV)
    assert c.verdict == INSUFFICIENT
    assert c.against == ("the facts file carries no verdict",)
    assert (c.supporting, c.digests) == ((), ())
    assert closure_case({"verdict": "fine"}, revision=REV).verdict == \
        INSUFFICIENT


def test_an_entry_of_the_wrong_shape_for_its_kind_supports_nothing():
    entry = {"day": "2026-08-30", "digest": f"digests/{DB}/2026-08-30.json",
             "matched": True, "group": "tns_error"}
    c = closure_case(raw_facts({"kind": "error_absent", "code": CODE},
                               [entry]), revision="")
    assert c.supporting == ()
    assert c.digests == (f"digests/{DB}/2026-08-30.md",)
    assert c.against == ("2026-08-31: no digest examined",
                         "2026-09-01: no digest examined")


def test_a_digest_spelled_some_other_way_comes_through_as_itself():
    """The translation is for the one spelling the evaluator writes. A facts
    file carrying anything else is malformed, and the rule this module keeps
    for a malformed file is that it comes through as itself rather than as
    an exception or as a path nobody can check."""
    entries = [{"day": WINDOW_DAYS[0], "digest": f"digests/{DB}/notes.txt",
                "code": CODE, "count": 0},
               {"day": WINDOW_DAYS[1], "digest": ".", "code": CODE,
                "count": 0},
               {"day": WINDOW_DAYS[2], "digest": 5, "code": CODE,
                "count": 0}]

    c = closure_case(raw_facts({"kind": "error_absent", "code": CODE},
                               entries), revision="")

    assert c.digests == (f"digests/{DB}/notes.txt", ".", "5")
    assert c.supporting == (
        f"{WINDOW_DAYS[0]}: {CODE} absent (digests/{DB}/notes.txt)",
        f"{WINDOW_DAYS[1]}: {CODE} absent (.)",
        f"{WINDOW_DAYS[2]}: {CODE} absent (5)")
    assert c.against == ()


def test_an_observed_entry_that_is_not_a_dict_supports_nothing():
    c = closure_case(raw_facts({"kind": "flow_resumed", "source": "alert"},
                               ["2026-08-30", 7, None]), revision="")
    assert c.supporting == ()
    assert c.digests == ()
    assert len(c.against) == len(WINDOW_DAYS)


def test_an_unknown_signal_kind_argues_neither_way():
    c = closure_case(raw_facts({"kind": "vibes"},
                               [{"day": "2026-08-30", "count": 0}]),
                     revision="")
    assert (c.supporting, c.against) == ((), ())


def test_a_window_that_will_not_rebuild_adds_no_coverage_gaps():
    c = closure_case(raw_facts({"kind": "error_absent", "code": CODE}, [],
                               window={"start": "yesterday", "until": ""}),
                     revision="")
    assert c.against == ()


def test_a_dead_probe_says_so_once(cfg, wiki, tmp_path, capsys):
    """Probing stops at the first failure; the operator reading the tick's
    stderr learns why the verdicts rest on digests alone."""
    second_incident(wiki)
    evaluate_all(cfg, wiki, tmp_path / "state", now=DURING,
                 es=FakeES(fail=True))
    err = [ln for ln in capsys.readouterr().err.splitlines()
           if "monitoring probe" in ln]
    assert len(err) == 1
    assert "elasticsearch is down" in err[0]
