"""Deterministic trigger decisions: given a digest and the prior ledger
state, answer why an agent would wake, skip, or be forced to consolidate —
and at which model tier. Pure and side-effect-free; it composes existing
compactor/notability output rather than recomputing it.

Must NOT import orchestrate.py — orchestrate imports this module, so the
reverse would cycle. Importing Compactor.content_hash from compactor.py is
fine (compactor never imports orchestrate or trigger)."""

from dataclasses import dataclass

from .compactor import Compactor

TRIGGER_SCHEMA_VERSION = 1

# delta types that force the strong model tier (incident-grade evidence)
_ESCALATING_DELTA_TYPES = ("first_ever_code", "silence")
# delta types that become their own trigger reason (one per delta present)
_DELTA_REASON_TYPES = ("first_ever_code", "silence", "rate_anomaly",
                       "new_service", "new_client_program", "after_change")


def digest_needs_escalation(digest: dict) -> bool:
    """True iff the digest carries incident-grade evidence that warrants the
    strong model tier: a first-ever error code, a silence delta, or any
    error-class notable group. Moved here from Orchestrator (which still
    delegates to it) so the trigger decision and the agent prompt agree."""
    if any(d["type"] in _ESCALATING_DELTA_TYPES for d in digest["deltas"]):
        return True
    return any(g["class"] == "error"
               for s in digest["sources"].values() for g in s["notable"])


@dataclass
class TriggerDecision:
    schema_version: int
    db: str
    window: dict  # {from, to, day}
    outcome: str  # wake | skip | force_consolidation
    reasons: list  # [{"code": str, "evidence": ...}, ...]
    model_tier: str  # cheap | strong
    content_hash: str
    explanation: str  # one short sentence, deterministically derived

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "db": self.db,
            "window": self.window,
            "outcome": self.outcome,
            "reasons": self.reasons,
            "model_tier": self.model_tier,
            "content_hash": self.content_hash,
            "explanation": self.explanation,
        }


def _explain(db: str, outcome: str, reasons: list, model_tier: str) -> str:
    codes = ", ".join(sorted({r["code"] for r in reasons})) or "none"
    if outcome == "skip":
        return f"{db}: skip — {codes}."
    if outcome == "force_consolidation":
        return f"{db}: force_consolidation ({model_tier} tier) — {codes}."
    return f"{db}: wake ({model_tier} tier) — {codes}."


def decide(digest: dict, *, ledger_entry: dict | None, window_to: str,
          consolidation: bool = False, manual: bool = False) -> TriggerDecision:
    """digest + prior ledger entry + window/mode flags -> TriggerDecision.
    Identical inputs always yield an identical result (no timestamps, no
    randomness) — reruns and --explain must agree with what actually ran.

    Check order (first match wins for the dedupe skips):
    1. already ingested at this exact window_to -> skip/already_ingested.
    2. same content already ingested (window moved, nothing new) ->
       skip/content_unchanged.
    3. notable -> wake, one reason per delta and per notable group.
    4. routine + consolidation tick -> force_consolidation.
    5. routine + manual -> wake/manual.
    6. otherwise -> skip/routine_only.
    """
    db = digest["db"]
    w = digest["window"]
    window = {"from": w["from"], "to": w["to"], "day": w["day"]}
    content_hash = Compactor.content_hash(digest)
    model_tier = "strong" if digest_needs_escalation(digest) else "cheap"
    prior = ledger_entry or {}

    def build(outcome: str, reasons: list) -> TriggerDecision:
        return TriggerDecision(
            schema_version=TRIGGER_SCHEMA_VERSION, db=db, window=window,
            outcome=outcome, reasons=reasons, model_tier=model_tier,
            content_hash=content_hash,
            explanation=_explain(db, outcome, reasons, model_tier))

    if prior.get("status") == "ingested" and prior.get("window_to") == window_to:
        return build("skip", [{"code": "already_ingested",
                               "evidence": {"status": prior.get("status"),
                                            "window_to": prior.get("window_to")}}])
    if prior.get("status") == "ingested" and prior.get("content_hash") == content_hash:
        return build("skip", [{"code": "content_unchanged",
                               "evidence": {"content_hash": content_hash}}])

    notable = digest["notable"]
    reasons = []
    if notable:
        for d in digest["deltas"]:
            if d["type"] in _DELTA_REASON_TYPES:
                reasons.append({"code": d["type"], "evidence": d})
        for s in digest["sources"].values():
            for g in s["notable"]:
                reasons.append({"code": "notable_class",
                                "evidence": {"rule": g["rule"], "class": g["class"],
                                             "count": g["count"]}})

    if notable:
        outcome = "wake"
    elif manual:
        outcome = "wake"
    elif consolidation:
        outcome = "force_consolidation"
    else:
        outcome = "skip"
        reasons = [{"code": "routine_only", "evidence": {}}]

    if manual:
        reasons = reasons + [{"code": "manual", "evidence": {}}]
    if outcome == "force_consolidation":
        reasons = reasons + [{"code": "consolidation", "evidence": {}}]

    return build(outcome, reasons)
