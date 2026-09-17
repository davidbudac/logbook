"""Trigger decisions: deterministic wake/skip/force_consolidation records
built from digest notability + prior ledger state. Digests are built by hand
to match the shape `Compactor.compact` assembles (see compactor.py)."""

from dbwiki.compactor import Compactor
from dbwiki.orchestrate import Orchestrator
from dbwiki.trigger import TriggerDecision, decide, digest_needs_escalation

DB = "cdb1"
T0 = "2026-07-10T00:00:00Z"
T1 = "2026-07-11T00:00:00Z"
DAY = "2026-07-10"


def group(rule="ORA-600", klass="error", count=3, template="ORA-600 <n>"):
    return {"rule": rule, "class": klass, "count": count, "template": template,
            "first_ts": T0, "last_ts": T0, "codes": [], "message": "m",
            "es_samples": []}


def make_digest(deltas=None, sources=None, db=DB, t0=T0, t1=T1, day=DAY):
    deltas = deltas or []
    sources = sources or {"alert": {"total_events": 10, "notable": []}}
    return {
        "db": db,
        "window": {"from": t0, "to": t1, "day": day},
        "generated_by": "dbwiki-compactor/test",
        "pattern_versions": {},
        "sources": sources,
        "deltas": deltas,
        "totals": {"events": sum(s["total_events"] for s in sources.values()),
                   "notable_events": 0, "notable_groups": 0},
        "notable": bool(deltas) or any(s["notable"] for s in sources.values()),
    }


# ---- decide: outcomes -------------------------------------------------------

def test_routine_only_skip():
    d = decide(make_digest(), ledger_entry=None, window_to=T1)
    assert d.outcome == "skip"
    assert [r["code"] for r in d.reasons] == ["routine_only"]
    assert d.model_tier == "cheap"


def test_first_ever_code_wakes_and_escalates():
    delta = {"type": "first_ever_code", "source": "alert", "value": "ORA-600",
             "first_seen": T0}
    d = decide(make_digest(deltas=[delta]), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "strong"
    assert d.reasons == [{"code": "first_ever_code", "evidence": delta}]


def test_silence_wakes_and_escalates():
    delta = {"type": "silence", "source": "alert", "detail": "no alert events"}
    d = decide(make_digest(deltas=[delta]), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "strong"
    assert d.reasons == [{"code": "silence", "evidence": delta}]


def test_rate_anomaly_wakes_but_stays_cheap_without_error_group():
    delta = {"type": "rate_anomaly", "source": "alert", "counter": "log_switch",
             "count": 500, "window_hours": 24.0, "rate_per_hour": 20.8,
             "baseline_median_per_hour": 1.0}
    d = decide(make_digest(deltas=[delta]), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "cheap"
    assert d.reasons == [{"code": "rate_anomaly", "evidence": delta}]


def test_rate_anomaly_escalates_when_error_group_also_present():
    delta = {"type": "rate_anomaly", "source": "alert", "counter": "log_switch",
             "count": 500, "window_hours": 24.0, "rate_per_hour": 20.8,
             "baseline_median_per_hour": 1.0}
    sources = {"alert": {"total_events": 10, "notable": [group(klass="error")]}}
    d = decide(make_digest(deltas=[delta], sources=sources), ledger_entry=None, window_to=T1)
    assert d.model_tier == "strong"


def test_after_change_wakes_but_never_escalates_on_its_own():
    """The group it names is an error group, which escalates by itself when it
    is really there; a delta about timing adds no evidence of its own."""
    delta = {"type": "after_change", "source": "alert", "rule": "ora_error",
             "codes": ["ORA-1653"], "first_ts": T0, "gap_s": 900,
             "change_ts": T0, "change_rule": "parameter_change",
             "change": "ALTER SYSTEM SET db_cache_size=4G SCOPE=BOTH;"}
    d = decide(make_digest(deltas=[delta]), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "cheap"
    assert d.reasons == [{"code": "after_change", "evidence": delta}]


def test_error_class_group_wakes_and_escalates():
    sources = {"alert": {"total_events": 10, "notable": [group(klass="error")]}}
    d = decide(make_digest(sources=sources), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "strong"
    assert d.reasons == [{"code": "notable_class",
                          "evidence": {"rule": "ORA-600", "class": "error", "count": 3}}]


def test_lifecycle_and_dataguard_groups_wake_but_stay_cheap():
    sources = {
        "alert": {"total_events": 10, "notable": [group(rule="startup", klass="lifecycle")]},
        "dataguard": {"total_events": 5, "notable": [group(rule="dg-role", klass="dataguard")]},
    }
    d = decide(make_digest(sources=sources), ledger_entry=None, window_to=T1)
    assert d.outcome == "wake"
    assert d.model_tier == "cheap"
    codes = [r["code"] for r in d.reasons]
    assert codes == ["notable_class", "notable_class"]
    classes = [r["evidence"]["class"] for r in d.reasons]
    assert classes == ["lifecycle", "dataguard"]


def test_unchanged_content_skip():
    digest = make_digest()
    prior = {"status": "ingested", "window_to": "2026-07-10T12:00:00Z",
             "content_hash": Compactor.content_hash(digest)}
    d = decide(digest, ledger_entry=prior, window_to=T1)
    assert d.outcome == "skip"
    assert [r["code"] for r in d.reasons] == ["content_unchanged"]


def test_already_ingested_skip():
    digest = make_digest()
    prior = {"status": "ingested", "window_to": T1, "content_hash": "different"}
    d = decide(digest, ledger_entry=prior, window_to=T1)
    assert d.outcome == "skip"
    assert [r["code"] for r in d.reasons] == ["already_ingested"]


def test_already_ingested_takes_precedence_over_content_unchanged():
    digest = make_digest()
    prior = {"status": "ingested", "window_to": T1,
             "content_hash": Compactor.content_hash(digest)}
    d = decide(digest, ledger_entry=prior, window_to=T1)
    assert [r["code"] for r in d.reasons] == ["already_ingested"]


def test_manual_wakes_a_routine_digest():
    d = decide(make_digest(), ledger_entry=None, window_to=T1, manual=True)
    assert d.outcome == "wake"
    assert [r["code"] for r in d.reasons] == ["manual"]


def test_manual_does_not_bypass_dedupe_skips():
    digest = make_digest()
    prior = {"status": "ingested", "window_to": T1}
    d = decide(digest, ledger_entry=prior, window_to=T1, manual=True)
    assert d.outcome == "skip"
    assert [r["code"] for r in d.reasons] == ["already_ingested"]


def test_consolidation_forces_ingest_of_routine_digest():
    d = decide(make_digest(), ledger_entry=None, window_to=T1, consolidation=True)
    assert d.outcome == "force_consolidation"
    assert [r["code"] for r in d.reasons] == ["consolidation"]


def test_consolidation_does_not_override_notable_wake():
    delta = {"type": "silence", "source": "alert", "detail": "no alert events"}
    d = decide(make_digest(deltas=[delta]), ledger_entry=None, window_to=T1,
              consolidation=True)
    assert d.outcome == "wake"


# ---- digest_needs_escalation ------------------------------------------------

def test_digest_needs_escalation_true_for_first_ever_code():
    delta = {"type": "first_ever_code", "source": "alert", "value": "ORA-600",
             "first_seen": T0}
    assert digest_needs_escalation(make_digest(deltas=[delta]))


def test_digest_needs_escalation_true_for_silence():
    delta = {"type": "silence", "source": "alert", "detail": "x"}
    assert digest_needs_escalation(make_digest(deltas=[delta]))


def test_digest_needs_escalation_true_for_error_group():
    sources = {"alert": {"total_events": 1, "notable": [group(klass="error")]}}
    assert digest_needs_escalation(make_digest(sources=sources))


def test_digest_needs_escalation_true_for_process_exception_group():
    sources = {"alert": {"total_events": 1,
                         "notable": [group(rule="process_exception", klass="error")]}}
    assert digest_needs_escalation(make_digest(sources=sources))


def test_digest_needs_escalation_false_for_routine():
    assert not digest_needs_escalation(make_digest())


def test_digest_needs_escalation_false_for_non_error_group():
    sources = {"alert": {"total_events": 1, "notable": [group(klass="lifecycle")]}}
    assert not digest_needs_escalation(make_digest(sources=sources))


def test_orchestrator_digest_needs_escalation_delegates():
    delta = {"type": "silence", "source": "alert", "detail": "x"}
    digest = make_digest(deltas=[delta])
    assert Orchestrator.digest_needs_escalation(digest) == digest_needs_escalation(digest)


# ---- determinism -------------------------------------------------------------

def test_decide_is_deterministic():
    delta = {"type": "first_ever_code", "source": "alert", "value": "ORA-600",
             "first_seen": T0}
    digest = make_digest(deltas=[delta])
    d1 = decide(digest, ledger_entry=None, window_to=T1)
    d2 = decide(digest, ledger_entry=None, window_to=T1)
    assert d1.to_dict() == d2.to_dict()


# ---- golden ------------------------------------------------------------------

def test_golden_wake_decision_to_dict():
    delta = {"type": "first_ever_code", "source": "alert", "value": "ORA-600",
             "first_seen": T0}
    sources = {"alert": {"total_events": 12, "notable": [group()]}}
    digest = make_digest(deltas=[delta], sources=sources)
    d = decide(digest, ledger_entry=None, window_to=T1)
    assert isinstance(d, TriggerDecision)
    expected = {
        "schema_version": 1,
        "db": "cdb1",
        "window": {"from": T0, "to": T1, "day": DAY},
        "outcome": "wake",
        "reasons": [
            {"code": "first_ever_code", "evidence": delta},
            {"code": "notable_class",
             "evidence": {"rule": "ORA-600", "class": "error", "count": 3}},
        ],
        "model_tier": "strong",
        "content_hash": Compactor.content_hash(digest),
        "explanation": "cdb1: wake (strong tier) — first_ever_code, notable_class.",
    }
    assert d.to_dict() == expected
