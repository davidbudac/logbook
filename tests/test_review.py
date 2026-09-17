"""The weekly attention review: what it selects, what it packs, what it
publishes, and which file each writer is allowed to touch.

Pure where the module is pure. `select` gets a `Snapshot` built by hand — not
`readmodel.build`, which wants git — because what is under test is the
selection, and every detector is a threshold over values the snapshot already
carries. The one thing on disk is `tmp_path/.state`, because the review's
state split, its idempotency key and its audit trail are files, and a test
that faked them would be testing its own restatement of the rules.

Every threshold fixture states one input just over its threshold and one just
under, and asserts the over-case fires exactly its own code: a detector that
quietly fires a second kind is a detector whose caps mean something else.
"""

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dbwiki import alerts, delivery, health, review
from dbwiki.incidents import (Actions, ActionRecord, ErrorAbsent, Incident,
                              MonitoringWindow, Status)
from dbwiki.readmodel import Occurrence, Snapshot
from dbwiki.review import (ACKS_FILE, AUDIT_EVENTS, AUDIT_LOG, BANDS,
                           DETECTORS, REVIEW_DIR, REVIEW_SCHEMA_VERSION,
                           REVIEWS_DIR, STATE_FILE, Rules, Selection,
                           acknowledge, fingerprint, iso_week, list_reviews,
                           load_acks, load_review, load_state, save_acks,
                           save_state, select, suppress)

DB = "cdb1"
OTHER = "cdb2"
REV = "b3f" + "0" * 37
NOW = "2026-09-07T00:00:00Z"
REVIEW_ID = "2026-W37"
CODE = "ORA-00600"


@pytest.fixture(autouse=True)
def no_real_smtp(monkeypatch):
    """No test in this module may reach a relay. Every send goes through a
    `delivery._open_smtp` this fixture has already replaced."""
    import smtplib

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a real SMTP connection")

    monkeypatch.setattr(smtplib, "SMTP", refuse)
    monkeypatch.setattr(delivery, "_open_smtp", refuse)


def ago(days: float = 0, hours: float = 0) -> str:
    return review._shift_days(NOW, -(days + hours / 24.0))


def inc(slug: str, *, db: str = DB, status: Status = Status.OPEN,
        opened: str = "", updated: str = "",
        monitoring: MonitoringWindow | None = None,
        actions: tuple[str, ...] = ()) -> Incident:
    records = tuple(
        ActionRecord(at=at, kind="record-action", actor="alice",
                     intent="stop the bleeding", summary="restarted apply",
                     status_after=status)
        for at in actions)
    return Incident(path=f"incidents/{slug}.md", db=db, title=f"{slug} title",
                    status=status, opened=opened or ago(1), updated=updated,
                    monitoring=monitoring, actions=Actions(records=records))


def window(until: str) -> MonitoringWindow:
    return MonitoringWindow(signal=ErrorAbsent(CODE), start=ago(30),
                            until=until)


def occ(code: str, day: str, db: str = DB) -> Occurrence:
    return Occurrence(code=code, day=day, db=db, note="",
                      evidence=f"digests/{db}/{day}.md")


def day(days: float) -> str:
    return ago(days)[:10]


def snapshot(incidents: tuple[Incident, ...] = (),
             occurrences: tuple[Occurrence, ...] = (),
             inventory: frozenset[str] = frozenset(),
             text: dict[str, str] | None = None) -> Snapshot:
    text = text or {}
    return Snapshot(revision=REV, built_at=NOW,
                    inventory=inventory | frozenset(text), pages={},
                    text=text, links={}, backlinks={},
                    incidents={i.slug: i for i in incidents},
                    occurrences=occurrences, research={}, resolutions={},
                    journals={}, touches_by_run={},
                    touches_by_incident={}, report_of_day={},
                    dbs=(DB, OTHER))


def run_select(snap, *, facts=None, prior=None, acks=None, alert_state=None,
               now: str = NOW, **rules) -> Selection:
    return select(snap, facts or {}, prior or {}, acks or {},
                  alert_state or {}, now=now, rules=Rules(**rules),
                  review_id=iso_week(now))


def alert_state(*entries: dict) -> dict:
    return {"fingerprints": {f"fp{i}": e for i, e in enumerate(entries)}}


def _stale_open(over: bool):
    age = 15 if over else 13
    return snapshot((inc("i-stale", opened=ago(age), updated=ago(age)),)), {}, {}


def _window_elapsed(over: bool):
    hours = 25 if over else 23
    return snapshot((inc("i-mon", status=Status.MONITORING,
                         monitoring=window(ago(hours=hours))),)), {}, {}


def _recovery_met(over: bool):
    snap = snapshot((inc("i-met", status=Status.MONITORING),))
    verdict = "met" if over else "not_met"
    return snap, {"i-met": {"verdict": verdict,
                            "evaluated_at": ago(1)}}, {}


def _occurrences_up(over: bool):
    rows = tuple(occ(CODE, day(d)) for d in (1, 2, 3, 4)[:4 if over else 3])
    rows += (occ(CODE, day(20)),)
    return snapshot((), rows), {}, {"recurring_min_occurrences": 99}


def _repeat_occurrences(over: bool):
    rows = tuple(occ(CODE, day(d)) for d in (1, 2, 3, 4)[:4 if over else 3])
    return snapshot((), rows), {}, {"worsening_min_increase": 99}


def _multi_db(over: bool):
    rows = (occ(CODE, day(2)),)
    if over:
        rows += (occ(CODE, day(3), OTHER),)
    return snapshot((), rows), {}, {}


def _no_action(over: bool):
    age = 22 if over else 20
    return snapshot((inc("i-quiet", status=Status.MONITORING, opened=ago(60),
                         actions=(ago(age),)),)), {}, {}


def _never_actioned(over: bool):
    age = 22 if over else 20
    return snapshot((inc("i-untouched", status=Status.MONITORING,
                         opened=ago(age)),)), {}, {}


#: reason code -> the fixture that trips it, and the kind it belongs to.
TRIPS = {
    "stale_open": (_stale_open, review.Kind.STALE_OPEN),
    "window_elapsed": (_window_elapsed, review.Kind.MONITORING_OVERDUE),
    "recovery_met": (_recovery_met, review.Kind.MONITORING_OVERDUE),
    "occurrences_up": (_occurrences_up, review.Kind.WORSENING),
    "repeat_occurrences": (_repeat_occurrences, review.Kind.RECURRING),
    "multi_db": (_multi_db, review.Kind.CROSS_DB),
    "no_action": (_no_action, review.Kind.UNFOLLOWED),
    "never_actioned": (_never_actioned, review.Kind.UNFOLLOWED),
}


@pytest.mark.parametrize("code", sorted(TRIPS))
def test_a_threshold_fires_exactly_its_own_code(code):
    build, kind = TRIPS[code]
    snap, facts, rules = build(True)
    sel = run_select(snap, facts=facts, **rules)
    assert {f.code for f in sel.findings} == {code}
    assert {f.kind for f in sel.findings} == {str(kind)}


@pytest.mark.parametrize("code", sorted(TRIPS))
def test_just_under_a_threshold_fires_nothing(code):
    build, _ = TRIPS[code]
    snap, facts, rules = build(False)
    assert run_select(snap, facts=facts, **rules).findings == ()


@pytest.mark.parametrize("rule", DETECTORS, ids=lambda r: r.kind)
def test_every_detector_row_has_a_fixture(rule):
    assert any(str(kind) == rule.kind for _, kind in TRIPS.values())


def busy_snapshot() -> Snapshot:
    """Enough of everything that several detectors fire on several databases."""
    incidents = (
        inc("i-open-1", opened=ago(40), updated=ago(40)),
        inc("i-open-2", db=OTHER, opened=ago(20), updated=ago(20)),
        inc("i-mon", status=Status.MONITORING, opened=ago(30),
            monitoring=window(ago(hours=300))),
        inc("i-untouched", db=OTHER, status=Status.MONITORING, opened=ago(50)),
    )
    rows = tuple(occ(CODE, day(d)) for d in (1, 2, 3, 4, 5))
    rows += (occ(CODE, day(3), OTHER), occ("TNS-12564", day(2)))
    return snapshot(incidents, rows, frozenset({f"errors/{CODE}.md"}))


def test_identical_inputs_give_identical_bytes():
    snap = busy_snapshot()
    first = json.dumps(run_select(snap).to_dict(), sort_keys=True)
    second = json.dumps(run_select(snap).to_dict(), sort_keys=True)
    assert first == second


def test_input_ordering_does_not_move_the_answer():
    snap = busy_snapshot()
    shuffled = Snapshot(
        revision=snap.revision, built_at=snap.built_at,
        inventory=snap.inventory, pages={}, text={}, links={}, backlinks={},
        incidents=dict(reversed(list(snap.incidents.items()))),
        occurrences=tuple(reversed(snap.occurrences)), research={}, resolutions={},
        journals={}, touches_by_run={}, touches_by_incident={},
        report_of_day={}, dbs=snap.dbs)
    assert (json.dumps(run_select(snap).to_dict(), sort_keys=True)
            == json.dumps(run_select(shuffled).to_dict(), sort_keys=True))


def test_every_changes_bucket_is_always_present():
    sel = run_select(snapshot())
    assert set(sel.changes) == {"new", "carried", "changed", "resolved",
                                "acknowledged", "suppressed", "capped"}
    assert set(sel.counts) == set(sel.changes) | {"selected", "high",
                                                  "normal", "shadowed"}


def test_the_counts_are_exactly_the_exported_vocabulary():
    """`COUNT_NAMES` is what the portal pins its allowlist against, so a count
    `select` writes without a name there is a number no page can show."""
    assert set(run_select(busy_snapshot()).counts) == set(review.COUNT_NAMES)


def test_every_high_precedes_every_normal_and_kinds_tie_break_by_table():
    sel = run_select(busy_snapshot())
    keys = [(BANDS.index(f.severity), review._KIND_ORDER[f.kind])
            for f in sel.findings]
    assert keys == sorted(keys)
    assert {f.severity for f in sel.findings} == {"high", "normal"}


def crowded() -> Snapshot:
    incidents = tuple(inc(f"i-{n}", opened=ago(20), updated=ago(20))
                      for n in range(6))
    incidents += (inc("i-other", db=OTHER, opened=ago(20), updated=ago(20)),)
    return snapshot(incidents)


def test_per_db_cap_lets_another_database_through():
    sel = run_select(crowded(), per_db_cap=4, cap=99)
    assert len(sel.findings) == 5
    assert [f.db for f in sel.findings].count(DB) == 4
    assert OTHER in {f.db for f in sel.findings}
    assert len(sel.changes["capped"]) == 2


def test_cap_truncates_and_everything_dropped_lands_in_capped():
    sel = run_select(crowded(), per_db_cap=99, cap=3)
    assert len(sel.findings) == 3 and len(sel.changes["capped"]) == 4
    assert sel.changes["capped"] == tuple(sorted(sel.changes["capped"]))
    assert not set(sel.changes["capped"]) & {f.fingerprint
                                             for f in sel.findings}


def test_movement_reads_new_carried_and_changed_against_the_prior():
    snap = snapshot((inc("i-stale", opened=ago(20), updated=ago(20)),))
    first = run_select(snap)
    assert [f.movement for f in first.findings] == ["new"]
    found = first.findings[0]
    prior = {found.fingerprint: {"evidence_hash": found.evidence_hash,
                                 "last_seen": "2026-W36"}}
    assert [f.movement for f in run_select(snap, prior=prior).findings] \
        == ["carried"]
    moved = {found.fingerprint: {"evidence_hash": "0" * 12,
                                 "last_seen": "2026-W36"}}
    assert [f.movement for f in run_select(snap, prior=moved).findings] \
        == ["changed"]


def test_a_prior_fingerprint_nothing_produced_is_resolved():
    sel = run_select(snapshot(), prior={"deadbeef1234": {"evidence_hash": "x"}})
    assert sel.changes["resolved"] == ("deadbeef1234",)
    assert sel.counts["resolved"] == 1


def stale_one() -> tuple[Snapshot, str]:
    snap = snapshot((inc("i-stale", opened=ago(20), updated=ago(20)),))
    return snap, fingerprint(review.Kind.STALE_OPEN, "i-stale", DB,
                             "stale_open")


def acks_of(fp: str, **fields) -> dict:
    return {"items": {fp: {"acknowledged_at": None, "suppressed_until": None,
                           "actor": "alice", "updated_at": NOW, **fields}}}


def test_a_live_suppression_excludes_and_an_expired_one_does_not():
    snap, fp = stale_one()
    live = run_select(snap, acks=acks_of(fp, suppressed_until=ago(-3)))
    assert live.findings == () and live.changes["suppressed"] == (fp,)
    expired = run_select(snap, acks=acks_of(fp, suppressed_until=ago(3)))
    assert [f.fingerprint for f in expired.findings] == [fp]


def test_acked_and_carried_drops_while_acked_and_changed_stays():
    snap, fp = stale_one()
    acked = acks_of(fp, acknowledged_at=ago(2))
    hashed = run_select(snap).findings[0].evidence_hash
    carried = run_select(snap, acks=acked,
                         prior={fp: {"evidence_hash": hashed}})
    assert carried.findings == () and carried.changes["acknowledged"] == (fp,)
    changed = run_select(snap, acks=acked, prior={fp: {"evidence_hash": "x"}})
    assert [f.movement for f in changed.findings] == ["changed"]


A_WEEK_ON = review._shift_days(NOW, 7)


def test_a_week_of_aging_alone_is_carried_not_changed():
    """`age_days` and `overdue_hours` are derived from `now`, so hashing them
    would make every age-based finding read `changed` every week whether or
    not anything happened."""
    snap, fp = stale_one()
    first = run_select(snap, unfollowed_days=60).findings[0]
    later = run_select(snap, now=A_WEEK_ON, unfollowed_days=60,
                       prior={fp: {"evidence_hash": first.evidence_hash}})
    assert [f.movement for f in later.findings] == ["carried"]
    assert (later.findings[0].reasons[0]["evidence"]["age_days"]
            > first.reasons[0]["evidence"]["age_days"]), \
        "the age is still in the published evidence, only out of the hash"


def test_an_acknowledged_finding_stays_dropped_a_week_later():
    """The whole point of the ack model: what an operator looked at does not
    come back next week under no change but the calendar."""
    snap, fp = stale_one()
    first = run_select(snap, unfollowed_days=60).findings[0]
    later = run_select(snap, now=A_WEEK_ON, unfollowed_days=60,
                       acks=acks_of(fp, acknowledged_at=ago(2)),
                       prior={fp: {"evidence_hash": first.evidence_hash}})
    assert later.findings == () and later.changes["acknowledged"] == (fp,)


def test_a_page_someone_touched_still_survives_the_ack_drop():
    """`updated` is in the hash on purpose: a human wrote on the page, so the
    acknowledgement no longer speaks for what the finding says."""
    snap, fp = stale_one()
    first = run_select(snap, unfollowed_days=60).findings[0]
    touched = snapshot((inc("i-stale", opened=ago(20), updated=ago(15)),))
    later = run_select(touched, now=A_WEEK_ON, unfollowed_days=60,
                       acks=acks_of(fp, acknowledged_at=ago(2)),
                       prior={fp: {"evidence_hash": first.evidence_hash}})
    assert [f.movement for f in later.findings] == ["changed"]


def shadow_snapshot() -> Snapshot:
    """One wiki-group and one es-group candidate on each of two databases."""
    return snapshot((
        inc("i-open-1", opened=ago(20), updated=ago(20)),
        inc("i-open-2", db=OTHER, opened=ago(20), updated=ago(20)),
        inc("i-mon-1", status=Status.MONITORING, opened=ago(30),
            monitoring=window(ago(hours=300)), actions=(ago(1),)),
        inc("i-mon-2", db=OTHER, status=Status.MONITORING, opened=ago(30),
            monitoring=window(ago(hours=300)), actions=(ago(1),)),
    ))


SHADOW_CASES = [(group, category)
                for group, categories in sorted(review._ALERT_SHADOW.items())
                for category in sorted(categories)]


@pytest.mark.parametrize("group,category", SHADOW_CASES,
                         ids=[f"{g}-{c}" for g, c in SHADOW_CASES])
def test_a_live_alert_shadows_only_its_group_on_its_database(group, category):
    state = alert_state({"category": category, "db": DB, "source": "-"})
    sel = run_select(shadow_snapshot(), alert_state=state)
    silenced = {"wiki": review.Kind.STALE_OPEN,
                "es": review.Kind.MONITORING_OVERDUE}[group]
    gone = [(f.kind, f.db) for f in sel.findings]
    assert (str(silenced), DB) not in gone
    assert (str(silenced), OTHER) in gone
    assert sel.counts["shadowed"] == 1


def test_a_fleet_wide_alert_shadows_its_group_everywhere():
    state = alert_state({"category": "collection_failure", "db": "-",
                         "source": "alert"})
    sel = run_select(shadow_snapshot(), alert_state=state)
    assert {f.kind for f in sel.findings} == {str(review.Kind.STALE_OPEN)}
    assert sel.counts["shadowed"] == 2


def test_every_shadow_category_is_one_alerts_can_actually_mint():
    """A typo in a shadow category is a rule that silently never fires, so the
    vocabulary is derived from `alerts.findings` rather than restated."""
    broken = {
        "wiki": {"path": "/w", "present": False, "git": False,
                 "stray": ["notes/a.md"]},
        "sources": [{"state": "collection_failure", "source": "alert",
                     "latest_event": None, "age_hours": 99.0},
                    {"state": "source_silent", "source": "listener",
                     "latest_event": None, "age_hours": 9.0}],
        "watermarks": [{"db": DB, "stale": True, "watermark": NOW,
                        "age_hours": 40.0}],
        "backlog": [{"db": DB, "digest": "digests/cdb1/x.md",
                     "decision": "wake"}],
        "stale_stages": [{"task": "run", "at": NOW, "age_h": 40.0,
                          "threshold_h": 26}],
    }
    dark = {"sources": [{"state": "unknown", "source": "alert",
                         "latest_event": None, "age_hours": None}]}
    minted = {f.category for f in alerts.findings(broken)}
    minted |= {f.category for f in alerts.findings(dark)}
    named = set().union(*review._ALERT_SHADOW.values())
    assert named <= minted


def cfg_with(review_block: dict):
    return type("Cfg", (), {"review": review_block})()


def test_rules_default_when_the_block_is_absent():
    assert Rules.resolve(SimpleNamespace()) == Rules()


@pytest.mark.parametrize("block,key", [
    ({"synthesis_tier": "medium"}, "review.synthesis_tier"),
    ({"cap": 0}, "review.cap"),
    ({"per_db_cap": -1}, "review.per_db_cap"),
    ({"keep_reviews": 0}, "review.keep_reviews"),
    ({"history_weeks": 0}, "review.history_weeks"),
    ({"window_days": 0}, "review.window_days"),
    ({"stale_open_days": -1}, "review.stale_open_days"),
    ({"monitoring_overdue_hours": -1}, "review.monitoring_overdue_hours"),
])
def test_a_refusal_names_the_key(block, key):
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        Rules.resolve(cfg_with(block))


def test_rules_read_the_block():
    rules = Rules.resolve(cfg_with({"cap": 3, "synthesis_tier": "strong",
                                    "enabled": False}))
    assert rules.cap == 3 and rules.synthesis_tier == "strong"
    assert rules.enabled is False


def test_iso_week_names_the_review():
    assert iso_week(NOW) == REVIEW_ID
    assert iso_week("2026-01-01T12:00:00Z") == "2026-W01"


def test_state_files_round_trip(tmp_path):
    assert load_state(tmp_path)["history"] == {}
    assert load_acks(tmp_path)["items"] == {}
    assert load_review(tmp_path, REVIEW_ID) is None
    assert list_reviews(tmp_path) == ()

    save_state(tmp_path, {"schema_version": REVIEW_SCHEMA_VERSION,
                          "last_review_id": REVIEW_ID,
                          "history": {"abc": {"last_seen": REVIEW_ID}}})
    assert load_state(tmp_path)["last_review_id"] == REVIEW_ID
    save_acks(tmp_path, {"schema_version": REVIEW_SCHEMA_VERSION,
                         "items": {"abc": {"actor": "alice"}}})
    assert load_acks(tmp_path)["items"]["abc"]["actor"] == "alice"

    root = tmp_path / REVIEW_DIR / REVIEWS_DIR
    root.mkdir(parents=True)
    for rid in ("2026-W35", "2026-W36"):
        (root / f"{rid}.json").write_text(
            json.dumps({"schema_version": REVIEW_SCHEMA_VERSION,
                        "review_id": rid}))
    assert list_reviews(tmp_path) == ("2026-W36", "2026-W35")
    assert load_review(tmp_path, "2026-W35")["review_id"] == "2026-W35"


@pytest.mark.parametrize("rel,load", [
    (f"{REVIEW_DIR}/{STATE_FILE}", load_state),
    (f"{REVIEW_DIR}/{ACKS_FILE}", load_acks),
    (f"{REVIEW_DIR}/{REVIEWS_DIR}/{REVIEW_ID}.json",
     lambda d: load_review(d, REVIEW_ID)),
])
def test_a_future_schema_version_refuses_and_names_the_path(tmp_path, rel, load):
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": REVIEW_SCHEMA_VERSION + 1}))
    with pytest.raises(ValueError, match="newer than the version") as caught:
        load(tmp_path)
    assert str(path) in str(caught.value)


def audit_rows(state_dir) -> list[dict]:
    path = Path(state_dir) / AUDIT_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_acknowledging_twice_writes_nothing_the_second_time(tmp_path):
    item = acknowledge(tmp_path, "abc123", actor="alice", now=NOW)
    assert item["acknowledged_at"] == NOW
    before = (tmp_path / REVIEW_DIR / ACKS_FILE).read_bytes()
    again = acknowledge(tmp_path, "abc123", actor="bob",
                        now="2026-09-08T00:00:00Z")
    assert again["acknowledged_at"] == NOW and again["actor"] == "alice"
    assert (tmp_path / REVIEW_DIR / ACKS_FILE).read_bytes() == before
    assert [r["event"] for r in audit_rows(tmp_path)] == ["acknowledged"]


def test_suppression_only_ever_extends_forward(tmp_path):
    long = suppress(tmp_path, "abc123", actor="alice", now=NOW, days=14)
    before = (tmp_path / REVIEW_DIR / ACKS_FILE).read_bytes()
    short = suppress(tmp_path, "abc123", actor="alice", now=NOW, days=3)
    assert short["suppressed_until"] == long["suppressed_until"]
    assert (tmp_path / REVIEW_DIR / ACKS_FILE).read_bytes() == before
    longer = suppress(tmp_path, "abc123", actor="alice", now=NOW, days=30)
    assert longer["suppressed_until"] > long["suppressed_until"]
    assert [r["event"] for r in audit_rows(tmp_path)] == ["suppressed",
                                                          "suppressed"]


def test_a_non_positive_suppression_names_the_key(tmp_path):
    with pytest.raises(ValueError, match="days"):
        suppress(tmp_path, "abc123", actor="alice", now=NOW, days=0)


def test_the_portal_writers_touch_only_the_acks_file(tmp_path, monkeypatch):
    written: list[str] = []
    real = review.atomic_write_text
    monkeypatch.setattr(review, "atomic_write_text",
                        lambda p, t: (written.append(str(p)), real(p, t))[1])
    acknowledge(tmp_path, "abc123", actor="alice", now=NOW)
    suppress(tmp_path, "abc123", actor="alice", now=NOW, days=7)
    assert set(written) == {str(tmp_path / REVIEW_DIR / ACKS_FILE)}


def test_the_audit_events_are_exactly_the_seven():
    assert AUDIT_EVENTS == {"selected", "published", "synthesis_failed",
                            "delivered", "delivery_failed", "acknowledged",
                            "suppressed"}


def test_an_unlisted_audit_event_raises(tmp_path):
    with pytest.raises(ValueError, match="is not one of"):
        review._audit(tmp_path, "publishd", at=NOW)


SENTINEL = "PACKED-BODY-4d71ae"


def packed():
    """A selection over a snapshot whose every readable page carries the
    sentinel, so "no manifest row carries a body" is one assertion."""
    incidents = (
        inc("i-open-1", opened=ago(40), updated=ago(40)),
        inc("i-open-2", db=OTHER, opened=ago(20), updated=ago(20)),
    )
    rows = tuple(occ(CODE, day(d)) for d in (1, 2, 3, 4, 5))
    rows += (occ(CODE, day(3), OTHER),)
    text = {f"incidents/{i.slug}.md": f"# {i.title}\n\n{SENTINEL}\n"
            for i in incidents}
    text[f"errors/{CODE}.md"] = f"# {CODE}\n\n{SENTINEL}\n"
    snap = snapshot(incidents, rows, text=text)
    sel = run_select(snap)
    return snap, sel, review.pack(snap, sel, rules=Rules())


def test_the_pack_table_covers_every_kind():
    assert set(review.PACK) == set(review.PackKind)


def test_every_section_stays_inside_its_rule():
    _, _, pack = packed()
    per_kind: dict[str, int] = {}
    for section in pack.sections:
        rule = review.PACK[review.PackKind(section.kind)]
        assert len(section.text) <= rule.max_chars
        per_kind[section.kind] = per_kind.get(section.kind, 0) + 1
    for kind, count in per_kind.items():
        assert count <= review.PACK[review.PackKind(kind)].max_items


def test_no_manifest_row_carries_a_packed_body():
    _, _, pack = packed()
    assert SENTINEL in pack.prompt()
    assert SENTINEL not in json.dumps(pack.manifest)
    assert all("text" not in row for row in pack.manifest)


def test_only_real_wiki_pages_are_citable():
    snap, _, pack = packed()
    assert pack.paths
    assert all(snap.exists(path) for path in pack.paths)
    derived = {review.PackKind.SUMMARY, review.PackKind.CHANGES}
    assert all(s.path == "" for s in pack.sections
               if s.kind in {str(k) for k in derived})


def cfg_for_model():
    return SimpleNamespace(agents={"pi": {"cheap": "gemma-3",
                                          "strong": "qwen"},
                                   "timeout_seconds": 60},
                           wiki_repo=None, review={})


def answer(refs) -> str:
    return json.dumps({"summary": "two databases are drifting apart",
                       "themes": [{"title": "one theme",
                                   "detail": "what the material shows",
                                   "evidence_refs": list(refs)}],
                       "evidence_refs": list(refs)})


def test_a_proposal_citing_only_packed_paths_validates(monkeypatch):
    _, _, pack = packed()
    refs = sorted(pack.paths)[:1]
    monkeypatch.setattr("dbwiki.structured.generate",
                        lambda *a, **kw: answer(refs))
    out = review.synthesize(pack, cfg_for_model(), rules=Rules())
    assert out.evidence_refs == tuple(refs)
    assert out.themes[0]["evidence_refs"] == tuple(refs)
    assert out.model_tier == "cheap"
    assert json.loads(json.dumps(out.to_dict()))["summary"] == out.summary


def test_a_citation_outside_the_pack_costs_the_retry(monkeypatch):
    from dbwiki.harness import HarnessError
    from dbwiki.structured import ProposalError

    _, _, pack = packed()
    bad = answer(["incidents/nothing-like-this.md"])
    with pytest.raises(ProposalError, match="is not a path the Material"):
        review._synthesis_parser(pack.paths)(bad)

    calls = []
    monkeypatch.setattr("dbwiki.structured.generate",
                        lambda *a, **kw: (calls.append(1), bad)[1])
    telemetry: dict = {}
    with pytest.raises(HarnessError):
        review.synthesize(pack, cfg_for_model(), rules=Rules(),
                          telemetry=telemetry)
    assert len(calls) == 2 and telemetry["attempts"] == 2


@pytest.mark.parametrize("obj,message", [
    ({"themes": []}, "summary"),
    ({"summary": "x" * 4000, "themes": []}, "summary"),
    ({"summary": "ok", "themes": "no"}, "themes"),
    ({"summary": "ok", "themes": [{"detail": "d"}]}, "themes\\[0\\].title"),
    ({"summary": "ok", "themes": [{"title": "t", "detail": "d",
                                   "evidence_refs": [7]}]},
     "expected a list of strings"),
    ({"summary": "ok",
      "themes": [{"title": "t", "detail": "d"}] * 13}, "themes: 13 > 12"),
])
def test_a_malformed_proposal_names_its_field(obj, message):
    from dbwiki.structured import ProposalError
    _, _, pack = packed()
    with pytest.raises(ProposalError, match=message):
        review._synthesis_parser(pack.paths)(json.dumps(obj))


def test_an_oversized_page_is_truncated_and_says_so():
    incidents = (inc("i-long", opened=ago(40), updated=ago(40)),)
    body = "# long\n\n" + "x" * 9000
    snap = snapshot(incidents, text={"incidents/i-long.md": body})
    sel = run_select(snap)
    pack = review.pack(snap, sel, rules=Rules())
    page = next(s for s in pack.sections
                if s.kind == str(review.PackKind.INCIDENT_PAGE))
    rule = review.PACK[review.PackKind.INCIDENT_PAGE]
    assert page.truncated and len(page.text) == rule.max_chars


def incident_md(slug: str, db: str, opened: str) -> str:
    return (f"---\ntype: incident\nstatus: open\ndb: {db}\n"
            f"opened: {opened}\nupdated: {opened}\n---\n\n"
            f"# {slug} on {db}\n\nthe standby stopped applying redo.\n")


def error_md(code: str, rows: tuple[tuple[str, str], ...]) -> str:
    table = "".join(f"| {d} | {db} | seen | digests/{db}/{d}.md |\n"
                    for d, db in rows)
    return (f"---\ntype: error\n---\n\n# {code}\n\n## Occurrences\n\n"
            f"| Day | DB | Note | Evidence |\n|---|---|---|---|\n{table}")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """A one-commit wiki holding two stale incidents and one error page whose
    occurrence table spans both databases. The developer's own git config is
    neutralized, or the revision the snapshot pins is not reproducible."""
    import os
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "wiki"
    pages = {
        "incidents/i-open-1.md": incident_md("i-open-1", DB, ago(40)),
        "incidents/i-open-2.md": incident_md("i-open-2", OTHER, ago(20)),
        f"errors/{CODE}.md": error_md(CODE, tuple(
            (day(d), DB) for d in (1, 2, 3, 4, 5)) + ((day(3), OTHER),)),
    }
    for rel, text in pages.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "dba@example.com")
    git(root, "config", "user.name", "DBA")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def cfg(tmp_path, wiki):
    return SimpleNamespace(state_dir=tmp_path / ".state", wiki_repo=wiki,
                           review={}, agents={"pi": {"cheap": "gemma-3",
                                                     "strong": "qwen"},
                                              "timeout_seconds": 60})


def fake_propose(monkeypatch, *, raises=None):
    calls: list[str] = []

    def propose(prompt, cfg, parse, **kw):
        calls.append(prompt)
        if raises is not None:
            raise raises
        return {"summary": "two databases are drifting apart",
                "themes": (), "evidence_refs": ()}

    monkeypatch.setattr("dbwiki.structured._propose", propose)
    return calls


def published(cfg, review_id: str = REVIEW_ID) -> dict:
    return load_review(cfg.state_dir, review_id)


def test_run_publishes_one_review_a_week_and_force_republishes(cfg,
                                                               monkeypatch):
    calls = fake_propose(monkeypatch)
    first = review.run(cfg, now=NOW)
    assert first.published and first.skipped == "" and first.selected > 0
    assert first.synthesis_ok and first.synthesis_error == ""

    second = review.run(cfg, now=NOW)
    assert second.skipped == "exists" and not second.published
    assert len(calls) == 1
    assert list_reviews(cfg.state_dir) == (REVIEW_ID,)
    rows = [r for r in audit_rows(cfg.state_dir) if r["event"] == "published"]
    assert len(rows) == 1

    third = review.run(cfg, now=NOW, force=True)
    assert third.published and len(calls) == 2
    rows = [r for r in audit_rows(cfg.state_dir) if r["event"] == "published"]
    assert len(rows) == 2


def test_the_published_file_carries_the_whole_envelope(cfg, monkeypatch):
    fake_propose(monkeypatch)
    review.run(cfg, now=NOW)
    doc = published(cfg)
    assert set(doc) == {"schema_version", "review_id", "generated_at",
                        "source_revision", "window", "findings", "changes",
                        "counts", "explanation", "synthesis",
                        "synthesis_error", "pack_manifest", "deliveries"}
    assert doc["synthesis_error"] == ""
    assert [(r["channel"], r["recipient"], r["status"])
            for r in doc["deliveries"]] == [("inbox", "inbox", "delivered")]
    assert len(doc["source_revision"]) == 40
    assert doc["synthesis"]["summary"].startswith("two databases")


def test_synthesis_failure_still_publishes(cfg, monkeypatch):
    from dbwiki.harness import HarnessError
    fake_propose(monkeypatch, raises=HarnessError("the adapter never answered"))
    outcome = review.run(cfg, now=NOW)
    assert outcome.published and not outcome.synthesis_ok
    assert outcome.synthesis_error == "harness_error"
    doc = published(cfg)
    assert doc["synthesis"] is None and doc["findings"]
    assert doc["synthesis_error"] == "harness_error"
    failed = [r for r in audit_rows(cfg.state_dir)
              if r["event"] == "synthesis_failed"]
    assert len(failed) == 1 and failed[0]["category"] == "harness_error"


def test_a_failed_model_call_is_still_recorded_as_an_agent_run(cfg,
                                                               monkeypatch):
    from dbwiki.harness import HarnessError
    fake_propose(monkeypatch, raises=HarnessError("no"))
    review.run(cfg, now=NOW)
    log = (cfg.state_dir / "agent_runs.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in log]
    assert [e["task"] for e in events] == ["review"]
    assert events[0]["validation_ok"] is False
    assert events[0]["review_id"] == REVIEW_ID


def test_the_cron_writer_never_touches_the_acks_file(cfg, monkeypatch):
    fake_propose(monkeypatch)
    written: list[str] = []
    real = review.atomic_write_text
    monkeypatch.setattr(review, "atomic_write_text",
                        lambda p, t: (written.append(str(p)), real(p, t))[1])
    review.run(cfg, now=NOW)
    root = cfg.state_dir / REVIEW_DIR
    assert set(written) == {str(root / STATE_FILE),
                            str(root / REVIEWS_DIR / f"{REVIEW_ID}.json")}
    assert not (root / ACKS_FILE).exists()


def test_the_skipping_modes_publish_nothing(cfg, monkeypatch):
    calls = fake_propose(monkeypatch)
    cfg.review = {"enabled": False}
    assert review.run(cfg, now=NOW).skipped == "disabled"
    cfg.review = {}
    assert review.run(cfg, now=NOW, deliver_only=True).skipped == "deliver_only"
    dry = review.run(cfg, now=NOW, dry_run=True)
    assert dry.skipped == "dry_run" and not dry.published
    assert list_reviews(cfg.state_dir) == () and calls == []


def test_a_dry_run_says_so_on_its_selected_row(cfg, monkeypatch):
    """The row stays — the run really did read the wiki — but a reader
    counting the trail has to be able to tell which run published nothing."""
    fake_propose(monkeypatch)
    review.run(cfg, now=NOW, dry_run=True)
    review.run(cfg, now=NOW)
    dry, real = [r for r in audit_rows(cfg.state_dir)
                 if r["event"] == "selected"]
    assert dry["dry_run"] is True
    assert "dry_run" not in real


def test_reviews_prune_to_keep_reviews(cfg, monkeypatch):
    fake_propose(monkeypatch)
    cfg.review = {"keep_reviews": 3}
    root = cfg.state_dir / REVIEW_DIR / REVIEWS_DIR
    root.mkdir(parents=True)
    old = [f"2026-W{n:02d}" for n in range(30, 36)]
    for review_id in old:
        (root / f"{review_id}.json").write_text(
            json.dumps({"schema_version": REVIEW_SCHEMA_VERSION}))
    review.run(cfg, now=NOW)
    assert list_reviews(cfg.state_dir) == (REVIEW_ID, "2026-W35", "2026-W34")


def test_publishing_forgets_every_fingerprint_nothing_produced(cfg,
                                                               monkeypatch):
    fake_propose(monkeypatch)
    save_state(cfg.state_dir, {
        "schema_version": REVIEW_SCHEMA_VERSION, "last_review_id": "2026-W20",
        "history": {"ancient00000": {"last_seen": "2026-W20"},
                    "recent000000": {"last_seen": "2026-W35"}}})
    review.run(cfg, now=NOW)
    state = load_state(cfg.state_dir)
    assert not {"ancient00000", "recent000000"} & set(state["history"])
    assert state["last_review_id"] == REVIEW_ID


def shadow_every_wiki_finding(cfg) -> None:
    """A live fleet-wide `dirty_tree`, which is what `alerts.py` mints for a
    wiki checkout with stray files. It shadows the wiki group everywhere."""
    (cfg.state_dir / alerts.ALERT_STATE).write_text(json.dumps(
        {"schema_version": alerts.ALERT_SCHEMA_VERSION,
         "fingerprints": {"a1": {"category": "dirty_tree", "db": "-",
                                 "source": "-", "first_seen": NOW,
                                 "last_seen": NOW, "count": 1}},
         "recovered": []}))


def test_a_shadowed_finding_is_carried_until_history_weeks_ages_it_out(
        cfg, monkeypatch):
    """The only path to `_prune_history` through `run`. A candidate a live
    alert speaks for is still produced, so it is never resolved, and it is not
    a finding, so no branch moves its `last_seen`."""
    fake_propose(monkeypatch)
    cfg.review = {"history_weeks": 4}
    fp = fingerprint(review.Kind.STALE_OPEN, "i-open-1", DB, "stale_open")
    review.run(cfg, now=NOW)
    assert load_state(cfg.state_dir)["history"][fp]["last_seen"] == REVIEW_ID

    shadow_every_wiki_finding(cfg)
    review.run(cfg, now=later(7))
    doc = published(cfg, iso_week(later(7)))
    assert doc["counts"]["shadowed"]
    assert fp not in {f["fingerprint"] for f in doc["findings"]}
    assert fp not in doc["changes"]["resolved"]
    assert load_state(cfg.state_dir)["history"][fp]["last_seen"] == REVIEW_ID

    review.run(cfg, now=later(7 * 5))
    assert fp not in load_state(cfg.state_dir)["history"]


def test_a_shadowed_finding_is_not_re_announced_when_the_alert_clears(
        cfg, monkeypatch):
    """`carried`, and above all not `new`: the review was deferring to the
    alert the whole time, not reporting the problem gone. It reads `carried`
    rather than `changed` because nothing under the finding moved but the
    clock, which `_VOLATILE_EVIDENCE` holds out of the hash."""
    fake_propose(monkeypatch)
    fp = fingerprint(review.Kind.STALE_OPEN, "i-open-1", DB, "stale_open")
    review.run(cfg, now=NOW)
    shadow_every_wiki_finding(cfg)
    review.run(cfg, now=later(7))
    (cfg.state_dir / alerts.ALERT_STATE).unlink()
    review.run(cfg, now=later(14))
    back = next(f for f in published(cfg, iso_week(later(14)))["findings"]
                if f["fingerprint"] == fp)
    assert back["movement"] == "carried"


class Relay:
    """The injected SMTP factory and the connection it hands back."""

    def __init__(self, raises=None):
        self.raises = raises
        self.sent: list[str] = []

    def __call__(self, policy):
        return self

    def __enter__(self):
        if self.raises is not None:
            raise self.raises
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, message):
        self.sent.append(message["To"])


DELIVERY_BLOCK = {"external_enabled": True, "smtp_host": "relay.example.com",
                  "from_address": "dbwiki@example.com",
                  "recipients": [{"id": "ops", "address": "ops@example.com",
                                  "channels": ["email"]}]}


def relay_for(cfg, monkeypatch, raises=None) -> Relay:
    cfg.delivery = DELIVERY_BLOCK
    relay = Relay(raises)
    monkeypatch.setattr(delivery, "_open_smtp", relay)
    return relay


def delivery_rows(state_dir) -> list[dict]:
    return [r for r in audit_rows(state_dir)
            if r["event"] in ("delivered", "delivery_failed")]


def test_a_dead_relay_is_counted_and_never_fails_the_run(cfg, monkeypatch):
    """The review is already published when delivery runs. Losing it because
    a relay was down would be the wrong trade, exactly as for synthesis."""
    fake_propose(monkeypatch)
    relay_for(cfg, monkeypatch, OSError("connection refused"))
    outcome = review.run(cfg, now=NOW)
    assert outcome.published and outcome.delivered == 1 and outcome.failed == 1
    rows = published(cfg)["deliveries"]
    assert {r["recipient"]: r["status"] for r in rows} == {
        "inbox": "delivered", "ops": "failed"}
    assert [r["detail"] for r in rows if r["status"] == "failed"] \
        == ["connection refused"]


def test_exactly_one_audit_row_per_attempt(cfg, monkeypatch):
    fake_propose(monkeypatch)
    relay_for(cfg, monkeypatch, OSError("connection refused"))
    review.run(cfg, now=NOW)
    rows = delivery_rows(cfg.state_dir)
    assert [r["event"] for r in rows] == ["delivered", "delivery_failed"]
    assert [r["recipient"] for r in rows] == ["inbox", "ops"]
    assert rows[1]["error"] == "smtp_failed" and rows[1]["key"].startswith(
        f"{REVIEW_ID}|")
    assert "error" not in rows[0]


def test_deliver_only_resends_exactly_the_missing_keys(cfg, monkeypatch):
    fake_propose(monkeypatch)
    dead = relay_for(cfg, monkeypatch, OSError("connection refused"))
    review.run(cfg, now=NOW)
    assert dead.sent == []

    live = Relay()
    monkeypatch.setattr(delivery, "_open_smtp", live)
    outcome = review.run(cfg, now=NOW, deliver_only=True)
    assert outcome.skipped == "deliver_only"
    assert outcome.delivered == 1 and outcome.failed == 0
    assert live.sent == ["ops@example.com"]

    rows = delivery_rows(cfg.state_dir)
    assert [(r["event"], r["recipient"]) for r in rows] == [
        ("delivered", "inbox"), ("delivery_failed", "ops"),
        ("delivered", "ops")]
    assert {r["recipient"]: r["status"] for r in published(cfg)["deliveries"]} \
        == {"inbox": "delivered", "ops": "delivered"}

    again = review.run(cfg, now=NOW, deliver_only=True)
    assert again.delivered == 0 and live.sent == ["ops@example.com"]
    assert len(delivery_rows(cfg.state_dir)) == 3


def test_a_default_run_delivers_the_inbox_and_nothing_else(cfg, monkeypatch):
    fake_propose(monkeypatch)
    outcome = review.run(cfg, now=NOW)
    assert outcome.delivered == 1 and outcome.failed == 0
    assert [r["event"] for r in delivery_rows(cfg.state_dir)] == ["delivered"]


def test_delivery_disabled_leaves_the_run_as_3_3_left_it(cfg, monkeypatch):
    fake_propose(monkeypatch)
    cfg.delivery = {"enabled": False}
    outcome = review.run(cfg, now=NOW)
    doc = published(cfg)
    assert outcome.published and outcome.delivered == 0 and outcome.failed == 0
    assert doc["deliveries"] == []
    assert delivery_rows(cfg.state_dir) == []
    assert set(doc) == {"schema_version", "review_id", "generated_at",
                        "source_revision", "window", "findings", "changes",
                        "counts", "explanation", "synthesis",
                        "synthesis_error", "pack_manifest", "deliveries"}


def test_a_misconfigured_delivery_refuses_the_run_before_anything_publishes(
        cfg, monkeypatch):
    fake_propose(monkeypatch)
    cfg.delivery = {"external_enabled": True,
                    "recipients": [{"id": "ops", "address": "ops@example.com",
                                    "channels": ["email"]}]}
    with pytest.raises(ValueError, match=r"delivery\.smtp_host"):
        review.run(cfg, now=NOW)
    assert list_reviews(cfg.state_dir) == ()


def test_a_review_success_ages_into_one_stage_stale_finding(tmp_path):
    from dbwiki import health
    health.record_run(tmp_path, {
        "schema_version": 1, "run_id": "r1", "command": "review",
        "started": ago(9), "finished": ago(9), "outcome": "ok",
        "error_category": None, "dbs": [], "facts": {"selected": 7}})
    last = health._last_successes(health.read_events(tmp_path), {})
    assert last["review"]["at"] == ago(9) and last["review"]["detail"] == "7"

    stale = health._stage_staleness(last, NOW, health.DEFAULT_STAGE_STALE_HOURS)
    assert [s["task"] for s in stale] == ["review"]
    assert stale[0]["age_h"] == 216.0 and stale[0]["threshold_h"] == 192

    found = alerts.findings({"stale_stages": stale})
    assert [(f.category, f.detail) for f in found] == [("stage_stale",
                                                        "review")]


def test_a_fresh_review_success_is_not_stale(tmp_path):
    from dbwiki import health
    health.record_run(tmp_path, {
        "schema_version": 1, "run_id": "r1", "command": "review",
        "started": ago(1), "finished": ago(1), "outcome": "ok",
        "error_category": None, "dbs": [], "facts": {"selected": 0}})
    last = health._last_successes(health.read_events(tmp_path), {})
    assert health._stage_staleness(last, NOW,
                                   health.DEFAULT_STAGE_STALE_HOURS) == []


def test_explain_renders_every_finding_and_bucket(monkeypatch):
    monkeypatch.setattr("dbwiki.structured._propose",
                        lambda *a, **kw: pytest.fail("explain called a model"))
    sel = run_select(crowded(), per_db_cap=4, cap=99,
                     prior={"deadbeef1234": {"evidence_hash": "x"}})
    text = review.explain(sel)
    for finding in sel.findings:
        assert finding.explanation in text
        assert f"{finding.kind}/{finding.code}" in text
    for name in review.CHANGE_BUCKETS:
        present = bool(sel.changes[name])
        assert (f"{name}:" in text) is present
    assert sel.explanation in text and sel.source_revision in text


def test_explain_says_so_when_nothing_was_selected():
    assert "nothing selected" in review.explain(run_select(snapshot()))


def test_selection_for_writes_nothing_at_all(cfg, monkeypatch):
    """`--explain` reads the wiki and `.state` and touches neither."""
    def refuse(*a, **kw):
        raise AssertionError("--explain wrote something")

    monkeypatch.setattr("dbwiki.state.atomic_write_text", refuse)
    monkeypatch.setattr(review, "atomic_write_text", refuse)
    monkeypatch.setattr("dbwiki.health._append_capped", refuse)
    monkeypatch.setattr("dbwiki.structured._propose", refuse)
    sel = review.selection_for(cfg, now=NOW)
    assert sel.review_id == REVIEW_ID and sel.findings
    assert review.explain(sel)
    assert not (cfg.state_dir / REVIEW_DIR).exists()


def test_explain_prints_what_a_run_would_select(cfg, monkeypatch):
    fake_propose(monkeypatch)
    before = review.selection_for(cfg, now=NOW)
    review.run(cfg, now=NOW)
    doc = published(cfg)
    assert [f["fingerprint"] for f in doc["findings"]] \
        == [f.fingerprint for f in before.findings]


def later(days: float) -> str:
    return review._shift_days(NOW, days)


def rewrite(wiki: Path, rel: str, text: str, subject: str) -> None:
    (wiki / rel).write_text(text)
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", subject)


def test_a_resolved_fingerprint_is_reported_once_and_then_forgotten(
        cfg, wiki, monkeypatch):
    """`resolved` means "resolved since the review I last published". Keeping
    the entry until `history_weeks` would re-report the same good news for
    twelve weeks."""
    fake_propose(monkeypatch)
    fp = fingerprint(review.Kind.STALE_OPEN, "i-open-1", DB, "stale_open")
    review.run(cfg, now=NOW)
    assert fp in {f["fingerprint"] for f in published(cfg)["findings"]}

    rewrite(wiki, "incidents/i-open-1.md",
            incident_md("i-open-1", DB, ago(40)).replace("status: open",
                                                         "status: resolved"),
            "close it")
    review.run(cfg, now=later(7))
    assert fp in published(cfg, iso_week(later(7)))["changes"]["resolved"]
    assert fp not in load_state(cfg.state_dir)["history"]

    review.run(cfg, now=later(14))
    assert fp not in published(cfg, iso_week(later(14)))["changes"]["resolved"]


def test_a_finding_that_comes_back_reads_as_new(cfg, wiki, monkeypatch):
    fake_propose(monkeypatch)
    fp = fingerprint(review.Kind.STALE_OPEN, "i-open-1", DB, "stale_open")
    review.run(cfg, now=NOW)
    rewrite(wiki, "incidents/i-open-1.md",
            incident_md("i-open-1", DB, ago(40)).replace("status: open",
                                                         "status: resolved"),
            "close it")
    review.run(cfg, now=later(7))
    rewrite(wiki, "incidents/i-open-1.md", incident_md("i-open-1", DB, ago(40)),
            "reopen it")
    review.run(cfg, now=later(14))
    back = next(f for f in published(cfg, iso_week(later(14)))["findings"]
                if f["fingerprint"] == fp)
    assert back["movement"] == "new"


def test_a_recurring_window_longer_than_the_review_window_is_refused():
    with pytest.raises(ValueError,
                       match=r"review\.recurring_window_days.*"
                             r"review\.window_days"):
        Rules.resolve(cfg_with({"window_days": 7, "recurring_window_days": 14}))
    assert Rules.resolve(cfg_with({"window_days": 14,
                                   "recurring_window_days": 14})).window_days == 14


def test_volatile_evidence_is_exactly_the_clock_driven_keys():
    """Held out of `evidence_hash`, so a finding whose only change is that it
    got a week older reads `carried` rather than `changed` and an acked
    age-based kind does not re-noise. `updated`, `last_action_at`, `until` and
    the occurrence counts stay in the hash."""
    assert review._VOLATILE_EVIDENCE == frozenset(
        {"age_days", "overdue_hours", "evaluated_at"})


def test_the_alert_shadow_never_covers_a_merely_failed_stage():
    """`harness_error` and `dependency_failure` are out on purpose: they mean a
    stage failed, not that the evidence under a finding is fiction, and the
    live box carries a fleet-wide `dependency_failure`."""
    shadowed = set().union(*review._ALERT_SHADOW.values())
    assert "harness_error" not in shadowed
    assert "dependency_failure" not in shadowed


def test_two_writers_on_the_audit_log_lose_no_line(tmp_path):
    """Append-only, which is why both the cron stage and the portal may write
    it: neither rewrites the other's line."""
    path = tmp_path / AUDIT_LOG

    def write(who):
        for i in range(50):
            health._append_capped(path, {"who": who, "i": i},
                                  review.MAX_REVIEW_EVENTS)

    writers = [threading.Thread(target=write, args=(w,))
               for w in ("cron", "portal")]
    for t in writers:
        t.start()
    for t in writers:
        t.join()
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert {(r["who"], r["i"]) for r in rows} == {
        (w, i) for w in ("cron", "portal") for i in range(50)}
