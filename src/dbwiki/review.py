"""The weekly attention review: what an operator should look at, chosen
deterministically, packed, optionally narrated by a model, and published to
`.state/review/`.

`select` is pure in `trigger.decide`'s style — an injected `now`, `{code,
evidence}` reason rows, a derived explanation — so a re-run over the same
inputs produces the same bytes and `--explain` agrees with what ran. Three
module-level tables carry what would otherwise be branches: `DETECTORS` is the
whole selection authority, `_ALERT_SHADOW` says which alert categories already
put a kind's subject in front of the operator, and `PACK` names every evidence
cap.

There is no lock anywhere in this module. The review reads the wiki at a
pinned head and never touches its working tree, and it writes only under
`.state/review/`, where the two writers are separated by file rather than
serialized: `state.json` and `reviews/<id>.json` are the cron stage's,
`acks.json` is the portal's, and they are merged at read.
"""

import hashlib
import json
import datetime as dt
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from . import (alerts, delivery, health, monitoring, readmodel, structured,
               transaction)
from .incidents import Status, signal_to_yaml
from .state import atomic_write_text
from .structured import ProposalError

REVIEW_SCHEMA_VERSION = 1

REVIEW_DIR = "review"
STATE_FILE = "state.json"
ACKS_FILE = "acks.json"
REVIEWS_DIR = "reviews"
AUDIT_LOG = "review/log.jsonl"
MAX_REVIEW_EVENTS = 2000

AUDIT_EVENTS = frozenset({"selected", "published", "synthesis_failed",
                          "delivered", "delivery_failed", "acknowledged",
                          "suppressed"})

#: Ordered: the index is the primary sort key.
BANDS = ("high", "normal")

#: New-first, which is the order an operator reads them in.
MOVEMENTS = ("new", "changed", "carried")
NEW, CHANGED, CARRIED = MOVEMENTS

#: Always present in a published selection, so the file's shape never
#: depends on the week.
CHANGE_BUCKETS = (*MOVEMENTS, "resolved", "acknowledged", "suppressed",
                  "capped")

#: The portal's allowlist is pinned against this, so a count added here
#: cannot be one the page never shows.
COUNT_NAMES = ("selected", "high", "normal", "shadowed", *CHANGE_BUCKETS)


class Kind(StrEnum):
    STALE_OPEN = "stale_open"
    MONITORING_OVERDUE = "monitoring_overdue"
    WORSENING = "worsening"
    RECURRING = "recurring"
    CROSS_DB = "cross_db"
    UNFOLLOWED = "unfollowed"


def _parse(ts: str) -> dt.datetime | None:
    """A frontmatter date (`YYYY-MM-DD`) or an ISO Z instant, as UTC."""
    text = str(ts or "")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt).replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _hours_between(then: str, now: str) -> float | None:
    a, b = _parse(then), _parse(now)
    if a is None or b is None:
        return None
    return round((b - a).total_seconds() / 3600.0, 2)


def _days_between(then: str, now: str) -> float | None:
    hours = _hours_between(then, now)
    return None if hours is None else round(hours / 24.0, 2)


def _shift_days(now: str, days: float) -> str:
    base = _parse(now) or dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (base + dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_week(now: str) -> str:
    """`2026-W36` — the review's identity and its idempotency key."""
    year, week, _ = dt.date.fromisoformat(str(now)[:10]).isocalendar()
    return f"{year}-W{week:02d}"


def _week_monday(review_id: str) -> dt.date | None:
    try:
        year, week = review_id.split("-W")
        return dt.date.fromisocalendar(int(year), int(week), 1)
    except (ValueError, AttributeError):
        return None


@dataclass(frozen=True)
class Finding:
    """One thing to look at, identified by `(kind, slug, db, code)` — which is
    exactly what `fingerprint` hashes.

    `reasons` is a tuple because one code can fire on several pieces of
    evidence (three occurrence rows under one `repeat_occurrences`), the way
    `trigger` emits several `notable_class` rows. `slug` names an incident for
    the incident-shaped kinds and an error code for the occurrence-shaped
    ones; `path` carries the wiki page either way."""

    fingerprint: str
    kind: str
    code: str
    slug: str
    db: str
    title: str
    path: str
    severity: str
    reasons: tuple[dict, ...]
    evidence_hash: str
    movement: str
    explanation: str

    def to_dict(self) -> dict:
        return {"fingerprint": self.fingerprint, "kind": self.kind,
                "code": self.code, "slug": self.slug, "db": self.db,
                "title": self.title, "path": self.path,
                "severity": self.severity, "reasons": list(self.reasons),
                "evidence_hash": self.evidence_hash,
                "movement": self.movement, "explanation": self.explanation}


@dataclass(frozen=True)
class Selection:
    """One week's deterministic answer. `to_dict` is the only serialization,
    so identical inputs give identical published bytes."""

    schema_version: int
    review_id: str
    generated_at: str
    source_revision: str
    window: dict
    findings: tuple[Finding, ...]
    changes: dict
    counts: dict
    explanation: str

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version,
                "review_id": self.review_id,
                "generated_at": self.generated_at,
                "source_revision": self.source_revision,
                "window": dict(self.window),
                "findings": [f.to_dict() for f in self.findings],
                "changes": {k: list(v) for k, v in sorted(self.changes.items())},
                "counts": dict(self.counts),
                "explanation": self.explanation}


@dataclass(frozen=True)
class _Candidate:
    kind: str
    code: str
    slug: str
    db: str
    title: str
    path: str
    severity: str
    reasons: tuple[dict, ...]


@dataclass(frozen=True)
class DetectorRule:
    """One row of the selection authority: what a kind is called, what its
    `slug` names, which alert group already speaks for it, and the function
    that yields its candidates."""

    kind: str
    label: str
    subject: str
    shadow_group: str
    detect: Callable[["_Context", "Rules"], Iterable[_Candidate]]


@dataclass(frozen=True)
class Rules:
    """The one boundary where `review:` yaml becomes typed thresholds. Every
    threshold feeds exactly one reason code; every cap is named here."""

    enabled: bool = True
    window_days: int = 28
    stale_open_days: int = 14
    stale_open_high_days: int = 30
    monitoring_overdue_hours: float = 24.0
    monitoring_overdue_high_hours: float = 168.0
    worsening_min_increase: int = 3
    worsening_high_increase: int = 10
    recurring_min_occurrences: int = 4
    recurring_high_occurrences: int = 10
    recurring_window_days: int = 14
    cross_db_min_databases: int = 2
    cross_db_high_databases: int = 4
    unfollowed_days: int = 21
    unfollowed_high_days: int = 45
    cap: int = 12
    per_db_cap: int = 4
    keep_reviews: int = 26
    history_weeks: int = 12
    synthesis_enabled: bool = True
    synthesis_tier: str = "cheap"

    @staticmethod
    def resolve(cfg) -> "Rules":
        """`cfg.review` as typed thresholds, refusing at resolve and naming
        the key: an unknown `synthesis_tier`, a non-positive cap or window, a
        negative threshold. A refusal here fails the stage's start rather than
        publishing a review nobody can explain."""
        raw = getattr(cfg, "review", {}) or {}
        base = Rules()
        values: dict = {}
        for name, default in (("enabled", base.enabled),
                              ("synthesis_enabled", base.synthesis_enabled)):
            values[name] = bool(raw.get(name, default))
        tier = str(raw.get("synthesis_tier", base.synthesis_tier))
        if tier not in ("cheap", "strong"):
            raise ValueError(f"review.synthesis_tier: {tier!r} is not a tier; "
                             f"the tiers are cheap, strong")
        values["synthesis_tier"] = tier
        for name in ("cap", "per_db_cap", "keep_reviews", "history_weeks",
                     "window_days"):
            value = int(raw.get(name, getattr(base, name)))
            if value <= 0:
                raise ValueError(f"review.{name}: {value} is not a positive "
                                 f"number")
            values[name] = value
        for name in ("stale_open_days", "stale_open_high_days",
                     "worsening_min_increase", "worsening_high_increase",
                     "recurring_min_occurrences", "recurring_high_occurrences",
                     "recurring_window_days", "cross_db_min_databases",
                     "cross_db_high_databases", "unfollowed_days",
                     "unfollowed_high_days"):
            value = int(raw.get(name, getattr(base, name)))
            if value < 0:
                raise ValueError(f"review.{name}: {value} is negative")
            values[name] = value
        for name in ("monitoring_overdue_hours",
                     "monitoring_overdue_high_hours"):
            value = float(raw.get(name, getattr(base, name)))
            if value < 0:
                raise ValueError(f"review.{name}: {value} is negative")
            values[name] = value
        if values["recurring_window_days"] > values["window_days"]:
            raise ValueError(
                f"review.recurring_window_days: "
                f"{values['recurring_window_days']} is longer than "
                f"review.window_days ({values['window_days']}), which reads "
                f"no occurrence outside it")
        return Rules(**values)


def fingerprint(kind: str, slug: str, db: str, code: str) -> str:
    """`sha256("kind|slug|db|code")[:12]`. A different namespace from
    `alerts.fingerprint`, so the two sets never intersect."""
    key = f"{kind}|{slug}|{db or '-'}|{code}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


#: Kept in the evidence the operator reads, held out of `evidence_hash`.
_VOLATILE_EVIDENCE = frozenset({"age_days", "overdue_hours", "evaluated_at"})


def _evidence_hash(reasons: tuple[dict, ...]) -> str:
    stable = [{**reason,
               "evidence": {k: v
                            for k, v in (reason.get("evidence") or {}).items()
                            if k not in _VOLATILE_EVIDENCE}}
              for reason in reasons]
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class _Context:
    """What every detector reads: the snapshot, the per-incident monitoring
    facts, the window, and the occurrence indexes, built once here rather than
    once per detector."""

    snap: object
    facts: Mapping[str, dict | None]
    now: str
    window_from: str
    window_to: str
    by_code_db: Mapping[tuple[str, str], tuple]
    by_code: Mapping[str, tuple]
    last_action: Mapping[str, str]

    @staticmethod
    def build(snap, facts: Mapping[str, dict | None], *, now: str,
              rules: "Rules") -> "_Context":
        window_from = _shift_days(now, -rules.window_days)
        low, high = window_from[:10], now[:10]
        by_code_db: dict[tuple[str, str], list] = {}
        by_code: dict[str, list] = {}
        for occ in snap.occurrences:
            if not (low <= occ.day <= high):
                continue
            by_code_db.setdefault((occ.code, occ.db), []).append(occ)
            by_code.setdefault(occ.code, []).append(occ)
        last_action = {}
        for slug, inc in snap.incidents.items():
            ats = [r.at for r in inc.actions.records if r.at]
            if ats:
                last_action[slug] = max(ats)
        return _Context(
            snap=snap, facts=facts, now=now, window_from=window_from,
            window_to=now,
            by_code_db={k: tuple(v) for k, v in sorted(by_code_db.items())},
            by_code={k: tuple(v) for k, v in sorted(by_code.items())},
            last_action=last_action)

    def error_path(self, code: str) -> str:
        path = f"errors/{code}.md"
        return path if self.snap.exists(path) else ""


def _band(value: float, high: float) -> str:
    return "high" if value >= high else "normal"


def _detect_stale_open(ctx: _Context, rules: Rules) -> Iterable[_Candidate]:
    for slug, inc in sorted(ctx.snap.incidents.items()):
        if inc.status is not Status.OPEN:
            continue
        base = inc.updated or inc.opened
        age = _days_between(base, ctx.now)
        if age is None or age < rules.stale_open_days:
            continue
        yield _Candidate(
            kind=Kind.STALE_OPEN, code="stale_open", slug=slug, db=inc.db,
            title=inc.title, path=inc.path,
            severity=_band(age, rules.stale_open_high_days),
            reasons=({"code": "stale_open",
                      "evidence": {"opened": inc.opened,
                                   "updated": inc.updated,
                                   "age_days": age}},))


def _detect_monitoring_overdue(ctx: _Context,
                               rules: Rules) -> Iterable[_Candidate]:
    for slug, inc in sorted(ctx.snap.incidents.items()):
        if inc.status is not Status.MONITORING:
            continue
        window = inc.monitoring
        overdue = (_hours_between(window.until, ctx.now)
                   if window is not None else None)
        if overdue is not None and overdue > rules.monitoring_overdue_hours:
            yield _Candidate(
                kind=Kind.MONITORING_OVERDUE, code="window_elapsed", slug=slug,
                db=inc.db, title=inc.title, path=inc.path,
                severity=_band(overdue, rules.monitoring_overdue_high_hours),
                reasons=({"code": "window_elapsed",
                          "evidence": {
                              "until": window.until,
                              "signal": signal_to_yaml(window.signal),
                              "overdue_hours": overdue}},))
        verdict = (ctx.facts.get(slug) or {})
        if not isinstance(verdict, dict) or verdict.get("verdict") != "met":
            continue
        yield _Candidate(
            kind=Kind.MONITORING_OVERDUE, code="recovery_met", slug=slug,
            db=inc.db, title=inc.title, path=inc.path, severity="high",
            reasons=({"code": "recovery_met",
                      "evidence": {"verdict": verdict.get("verdict"),
                                   "evaluated_at": verdict.get(
                                       "evaluated_at")}},))


def _detect_worsening(ctx: _Context, rules: Rules) -> Iterable[_Candidate]:
    midpoint = _shift_days(ctx.now, -rules.window_days / 2)[:10]
    for (code, db), rows in ctx.by_code_db.items():
        recent = sum(1 for o in rows if o.day >= midpoint)
        prior = len(rows) - recent
        increase = recent - prior
        if increase < rules.worsening_min_increase:
            continue
        yield _Candidate(
            kind=Kind.WORSENING, code="occurrences_up", slug=code, db=db,
            title=f"{code} on {db}", path=ctx.error_path(code),
            severity=_band(increase, rules.worsening_high_increase),
            reasons=({"code": "occurrences_up",
                      "evidence": {"code": code, "db": db, "recent": recent,
                                   "prior": prior, "increase": increase,
                                   "window_days": rules.window_days}},))


def _detect_recurring(ctx: _Context, rules: Rules) -> Iterable[_Candidate]:
    low = _shift_days(ctx.now, -rules.recurring_window_days)[:10]
    for (code, db), all_rows in ctx.by_code_db.items():
        rows = [o for o in all_rows if o.day >= low]
        if len(rows) < rules.recurring_min_occurrences:
            continue
        days = sorted({o.day for o in rows})
        shown = days[:rules.recurring_high_occurrences]
        reasons = [{"code": "repeat_occurrences",
                    "evidence": {"code": code, "db": db, "count": len(rows),
                                 "days": shown,
                                 "window_days": rules.recurring_window_days}}]
        reasons += [{"code": "repeat_occurrences",
                     "evidence": {"code": code, "db": db, "day": day}}
                    for day in shown]
        yield _Candidate(
            kind=Kind.RECURRING, code="repeat_occurrences", slug=code, db=db,
            title=f"{code} on {db}", path=ctx.error_path(code),
            severity=_band(len(rows), rules.recurring_high_occurrences),
            reasons=tuple(reasons))


def _detect_cross_db(ctx: _Context, rules: Rules) -> Iterable[_Candidate]:
    for code, rows in ctx.by_code.items():
        dbs = sorted({o.db for o in rows if o.db})
        if len(dbs) < rules.cross_db_min_databases:
            continue
        yield _Candidate(
            kind=Kind.CROSS_DB, code="multi_db", slug=code, db="-",
            title=f"{code} across {len(dbs)} databases",
            path=ctx.error_path(code),
            severity=_band(len(dbs), rules.cross_db_high_databases),
            reasons=({"code": "multi_db",
                      "evidence": {"code": code, "databases": dbs,
                                   "count": len(dbs)}},))


def _detect_unfollowed(ctx: _Context, rules: Rules) -> Iterable[_Candidate]:
    for slug, inc in sorted(ctx.snap.incidents.items()):
        if not inc.is_active:
            continue
        last = ctx.last_action.get(slug, "")
        code = "no_action" if last else "never_actioned"
        age = _days_between(last or inc.opened, ctx.now)
        if age is None or age < rules.unfollowed_days:
            continue
        yield _Candidate(
            kind=Kind.UNFOLLOWED, code=code, slug=slug, db=inc.db,
            title=inc.title, path=inc.path,
            severity=_band(age, rules.unfollowed_high_days),
            reasons=({"code": code,
                      "evidence": {"last_action_at": last, "age_days": age,
                                   "records": len(inc.actions.records)}},))


#: One row per kind: the whole selection authority. Order is the tie-break
#: order inside a severity band, so a seventh detector is a row and not a
#: branch anywhere else in this module.
DETECTORS: tuple[DetectorRule, ...] = (
    DetectorRule(Kind.STALE_OPEN, "open and untouched", "incident", "wiki",
                 _detect_stale_open),
    DetectorRule(Kind.MONITORING_OVERDUE, "monitoring window is up",
                 "incident", "es", _detect_monitoring_overdue),
    DetectorRule(Kind.WORSENING, "getting worse", "error_code", "es",
                 _detect_worsening),
    DetectorRule(Kind.RECURRING, "keeps coming back", "error_code", "es",
                 _detect_recurring),
    DetectorRule(Kind.CROSS_DB, "on several databases", "error_code", "es",
                 _detect_cross_db),
    DetectorRule(Kind.UNFOLLOWED, "nobody has acted", "incident", "wiki",
                 _detect_unfollowed),
)

_RULE_OF_KIND: Mapping[str, DetectorRule] = {r.kind: r for r in DETECTORS}
_KIND_ORDER: Mapping[str, int] = {r.kind: i for i, r in enumerate(DETECTORS)}

#: Categories `alerts.py` already puts in front of the operator, grouped by
#: what they invalidate: a review finding derived from the same broken thing
#: would be the same problem said twice. A category in neither group shadows
#: nothing, because over-suppressing is worse than saying something twice.
_ALERT_SHADOW: Mapping[str, frozenset[str]] = {
    "es": frozenset({"stale_watermark", "collection_failure", "source_silent",
                     "es_unreachable", "digest_backlog", "stage_stale"}),
    "wiki": frozenset({"wiki_missing", "dirty_tree"}),
}


def _explain_finding(rule: DetectorRule, cand: _Candidate) -> str:
    where = cand.db if cand.db != "-" else "the fleet"
    return (f"{cand.slug} on {where}: {rule.label} "
            f"({cand.code}, {cand.severity}).")


def _explain_selection(review_id: str, window: dict, counts: dict) -> str:
    return (f"{review_id}: {counts['selected']} selected "
            f"({counts['high']} high, {counts['normal']} normal) over "
            f"{window['days']} days — {counts['new']} new, "
            f"{counts['changed']} changed, {counts['carried']} carried; "
            f"{counts['resolved']} resolved, {counts['suppressed']} "
            f"suppressed, {counts['acknowledged']} acknowledged, "
            f"{counts['capped']} capped, {counts['shadowed']} shadowed.")


def _shadowed(cand: _Candidate, alert_state: Mapping) -> bool:
    """True when a live alert already speaks for this candidate's database
    under its kind's shadow group. A fleet-wide alert (`db: "-"`) shadows
    every database."""
    group = _RULE_OF_KIND[cand.kind].shadow_group
    categories = _ALERT_SHADOW[group]
    for entry in (alert_state.get("fingerprints") or {}).values():
        if entry.get("category") not in categories:
            continue
        db = entry.get("db", "-")
        if db == "-" or db == cand.db:
            return True
    return False


def select(snap, facts, prior, acks, alert_state, *, now: str,
           rules: Rules, review_id: str) -> Selection:
    """The week's findings, from the snapshot and the state around it. Pure:
    no I/O, no clock, no randomness — `now` is an ISO-Z string and every
    detector iterates a sorted sequence, so two calls over equal inputs
    produce equal bytes.

    Order matters: alert exclusion, then live suppressions, then movement
    against `prior`, then the acknowledgement drop, then the sort, then
    `per_db_cap` and `cap`. An acknowledged finding whose evidence *changed*
    survives: the evidence moved after the operator looked, which is exactly
    when they want to see it again. Getting a week older is not that move,
    which is what `_VOLATILE_EVIDENCE` is for."""
    ctx = _Context.build(snap, facts, now=now, rules=rules)
    window = {"from": ctx.window_from, "to": ctx.window_to,
              "days": rules.window_days}
    items = dict((acks or {}).get("items") or {})

    candidates: list[_Candidate] = []
    for rule in DETECTORS:
        candidates.extend(rule.detect(ctx, rules))
    produced = {fingerprint(c.kind, c.slug, c.db, c.code) for c in candidates}

    buckets: dict[str, list[str]] = {k: [] for k in CHANGE_BUCKETS}
    shadowed = 0
    kept: list[tuple[_Candidate, str, str, str]] = []
    for cand in candidates:
        if _shadowed(cand, alert_state or {}):
            shadowed += 1
            continue
        fp = fingerprint(cand.kind, cand.slug, cand.db, cand.code)
        item = items.get(fp) or {}
        until = item.get("suppressed_until") or ""
        if until and until > now:
            buckets["suppressed"].append(fp)
            continue
        evidence_hash = _evidence_hash(cand.reasons)
        was = (prior or {}).get(fp)
        if was is None:
            movement = NEW
        elif was.get("evidence_hash") == evidence_hash:
            movement = CARRIED
        else:
            movement = CHANGED
        if item.get("acknowledged_at") and movement == CARRIED:
            buckets["acknowledged"].append(fp)
            continue
        kept.append((cand, fp, evidence_hash, movement))

    kept.sort(key=lambda row: (BANDS.index(row[0].severity),
                               _KIND_ORDER[row[0].kind], row[0].db,
                               row[0].slug, row[0].code))

    per_db: dict[str, int] = {}
    survivors: list[tuple[_Candidate, str, str, str]] = []
    for row in kept:
        db = row[0].db
        if per_db.get(db, 0) >= rules.per_db_cap:
            buckets["capped"].append(row[1])
            continue
        per_db[db] = per_db.get(db, 0) + 1
        survivors.append(row)
    for row in survivors[rules.cap:]:
        buckets["capped"].append(row[1])
    survivors = survivors[:rules.cap]

    findings = []
    for cand, fp, evidence_hash, movement in survivors:
        rule = _RULE_OF_KIND[cand.kind]
        buckets[movement].append(fp)
        findings.append(Finding(
            fingerprint=fp, kind=cand.kind, code=cand.code, slug=cand.slug,
            db=cand.db, title=cand.title, path=cand.path,
            severity=cand.severity, reasons=cand.reasons,
            evidence_hash=evidence_hash, movement=movement,
            explanation=_explain_finding(rule, cand)))
    buckets["resolved"] = [fp for fp in (prior or {}) if fp not in produced]

    changes = {k: tuple(sorted(v)) for k, v in buckets.items()}
    counts = {"selected": len(findings),
              "high": sum(1 for f in findings if f.severity == "high"),
              "normal": sum(1 for f in findings if f.severity == "normal"),
              "shadowed": shadowed,
              **{k: len(v) for k, v in changes.items()}}
    unpinned = set(counts) ^ set(COUNT_NAMES)
    if unpinned:
        raise ValueError(f"review counts: {sorted(unpinned)} is in the counts "
                         f"or in COUNT_NAMES but not in both")
    return Selection(
        schema_version=REVIEW_SCHEMA_VERSION, review_id=review_id,
        generated_at=now, source_revision=getattr(snap, "revision", ""),
        window=window, findings=tuple(findings), changes=changes,
        counts=counts,
        explanation=_explain_selection(review_id, window, counts))


def _dir(state_dir) -> Path:
    return Path(state_dir) / REVIEW_DIR


def _check_version(obj: dict, path: Path) -> None:
    version = obj.get("schema_version", 0)
    if version > REVIEW_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version {version} is newer than the "
                         f"version this code supports "
                         f"({REVIEW_SCHEMA_VERSION})")


def load_state(state_dir) -> dict:
    """The cron stage's own bookkeeping. An absent file is the empty
    envelope; an unreadable one raises, unlike `monitoring.read_facts` — those
    are per-incident facts a page must not die on, this is the review's memory
    of what it already showed."""
    path = _dir(state_dir) / STATE_FILE
    empty = {"schema_version": REVIEW_SCHEMA_VERSION, "last_review_id": "",
             "history": {}}
    if not path.exists():
        return empty
    data = json.loads(path.read_text())
    _check_version(data, path)
    return {**empty, **data}


def save_state(state_dir, state: dict) -> None:
    atomic_write_text(_dir(state_dir) / STATE_FILE,
                      json.dumps(state, indent=1, sort_keys=True))


def load_acks(state_dir) -> dict:
    """The portal's side of the split: what an operator acknowledged or
    suppressed. Never written by the cron stage."""
    path = _dir(state_dir) / ACKS_FILE
    empty = {"schema_version": REVIEW_SCHEMA_VERSION, "items": {}}
    if not path.exists():
        return empty
    data = json.loads(path.read_text())
    _check_version(data, path)
    return {**empty, **data}


def save_acks(state_dir, acks: dict) -> None:
    atomic_write_text(_dir(state_dir) / ACKS_FILE,
                      json.dumps(acks, indent=1, sort_keys=True))


def load_review(state_dir, review_id: str) -> dict | None:
    path = _dir(state_dir) / REVIEWS_DIR / f"{review_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    _check_version(data, path)
    return data


def list_reviews(state_dir) -> tuple[str, ...]:
    """Published review ids, newest first."""
    root = _dir(state_dir) / REVIEWS_DIR
    if not root.is_dir():
        return ()
    return tuple(sorted((p.stem for p in root.glob("*.json")), reverse=True))


def _audit(state_dir, event: str, **fields) -> None:
    """One audit row. An event name outside `AUDIT_EVENTS` raises: a typo in
    an event name is a hole in the trail nobody notices."""
    if event not in AUDIT_EVENTS:
        raise ValueError(f"review audit event {event!r} is not one of "
                         f"{', '.join(sorted(AUDIT_EVENTS))}")
    row = {"event": event, "review_id": fields.pop("review_id", ""),
           "at": fields.pop("at", None), **fields}
    _append(state_dir, row)


def _append(state_dir, row: dict) -> None:
    health._append_capped(Path(state_dir) / AUDIT_LOG, row, MAX_REVIEW_EVENTS)


def acknowledge(state_dir, fingerprint: str, *, actor: str, now: str) -> dict:
    """Mark one finding as looked at. Idempotent: a second call leaves
    `acks.json` byte-identical and appends no audit row."""
    acks = load_acks(state_dir)
    items = acks["items"]
    item = dict(items.get(fingerprint) or _empty_item())
    if item.get("acknowledged_at"):
        return item
    item.update({"acknowledged_at": now, "actor": actor, "updated_at": now})
    items[fingerprint] = item
    save_acks(state_dir, acks)
    _audit(state_dir, "acknowledged", at=now, fingerprint=fingerprint,
           actor=actor)
    return item


def suppress(state_dir, fingerprint: str, *, actor: str, now: str,
             days: int) -> dict:
    """Hide one finding until `days` from now. Suppression only ever extends
    forward, so a shorter re-suppression is a no-op that writes nothing."""
    if days <= 0:
        raise ValueError(f"review suppress days: {days} is not a positive "
                         f"number of days")
    until = _shift_days(now, days)
    acks = load_acks(state_dir)
    items = acks["items"]
    item = dict(items.get(fingerprint) or _empty_item())
    if (item.get("suppressed_until") or "") >= until:
        return item
    item.update({"suppressed_until": until, "actor": actor, "updated_at": now})
    items[fingerprint] = item
    save_acks(state_dir, acks)
    _audit(state_dir, "suppressed", at=now, fingerprint=fingerprint,
           actor=actor, until=until)
    return item


def _empty_item() -> dict:
    return {"acknowledged_at": None, "suppressed_until": None, "actor": "",
            "updated_at": ""}


class PackKind(StrEnum):
    SUMMARY = "summary"
    FINDING = "finding"
    INCIDENT_PAGE = "incident_page"
    OCCURRENCES = "occurrences"
    CHANGES = "changes"


@dataclass(frozen=True)
class PackRule:
    """How much of one evidence kind a pack may carry. A reader never decides
    how much of itself is packed."""

    max_items: int
    max_chars: int
    heading: str


#: kind -> how much of it a pack may carry. Every cap the pack obeys is here
#: and nowhere else. A separate table from `advisory.SOURCES` on purpose: the
#: review's subject is a selection rather than an incident, so sharing the
#: table would share caps that mean different things.
PACK: Mapping[PackKind, PackRule] = {
    PackKind.SUMMARY: PackRule(
        max_items=1, max_chars=2000,
        heading="the selection, as the deterministic pass produced it"),
    PackKind.FINDING: PackRule(
        max_items=12, max_chars=800, heading="one finding"),
    PackKind.INCIDENT_PAGE: PackRule(
        max_items=5, max_chars=3000, heading="the incident pages behind them"),
    PackKind.OCCURRENCES: PackRule(
        max_items=1, max_chars=3000,
        heading="the occurrence rows behind the selected error codes"),
    PackKind.CHANGES: PackRule(
        max_items=1, max_chars=1500,
        heading="what moved since the last review"),
}


def _check_pack(table: Mapping[PackKind, PackRule]) -> None:
    missing = set(PackKind) - set(table)
    if missing:
        raise ValueError(f"PACK: no rule for {sorted(missing)}")


_check_pack(PACK)


@dataclass(frozen=True)
class PackSection:
    """One packed section. `text` is prompt material and never crosses the
    wire: `manifest` has no text field at all. `path` is set only for a real
    wiki page the model may cite, so a derived section is uncitable by
    construction."""

    kind: str
    heading: str
    path: str
    text: str
    truncated: bool


@dataclass(frozen=True)
class EvidencePack:
    """What one review packed. Bounded and compact, and never a raw log line:
    occurrence rows are already summaries, page text is capped, and digests
    are not read at all."""

    sections: tuple[PackSection, ...]

    @property
    def paths(self) -> frozenset[str]:
        """The citable wiki paths — the synthesis' whole allowlist."""
        return frozenset(s.path for s in self.sections if s.path)

    @property
    def manifest(self) -> tuple[dict, ...]:
        """The sections as anything but the prompt may see them."""
        return tuple({"kind": s.kind, "heading": s.heading, "path": s.path,
                      "chars": len(s.text), "truncated": s.truncated}
                     for s in self.sections)

    def prompt(self) -> str:
        body = "\n".join(structured._material_section(s.path or s.heading, s.text)
                         for s in self.sections)
        return _SYNTHESIS_RULES + "\n" + body


def _section(kind: PackKind, text: str, *, path: str = "",
             heading: str = "") -> PackSection:
    rule = PACK[kind]
    return PackSection(kind=str(kind), heading=heading or rule.heading,
                       path=path, text=text[:rule.max_chars],
                       truncated=len(text) > rule.max_chars)


def pack(snap, selection: Selection, *, rules: Rules) -> EvidencePack:
    """The selection plus the bounded evidence under it, in `PACK` order."""
    window = selection.window
    summary = "\n".join([
        selection.explanation,
        f"window: {window['from']} to {window['to']} ({window['days']} days)",
        *(f"{k}: {v}" for k, v in sorted(selection.counts.items()))])
    sections = [_section(PackKind.SUMMARY, summary)]

    for finding in selection.findings[:PACK[PackKind.FINDING].max_items]:
        path = finding.path if snap.exists(finding.path) else ""
        sections.append(_section(
            PackKind.FINDING,
            f"{finding.explanation}\n{finding.title}\n"
            + json.dumps(list(finding.reasons), sort_keys=True),
            path=path,
            heading=f"{finding.severity} {finding.kind}/{finding.code}"))

    pages: list[str] = []
    for finding in selection.findings:
        subject = _RULE_OF_KIND[finding.kind].subject
        if subject != "incident" or finding.path in pages:
            continue
        body = snap.text.get(finding.path)
        if body is None:
            continue
        pages.append(finding.path)
        sections.append(_section(PackKind.INCIDENT_PAGE, body,
                                 path=finding.path))
        if len(pages) >= PACK[PackKind.INCIDENT_PAGE].max_items:
            break

    codes = {f.slug for f in selection.findings
             if _RULE_OF_KIND[f.kind].subject == "error_code"}
    low, high = window["from"][:10], window["to"][:10]
    rows = sorted(f"{o.day}  {o.db}  {o.code}" for o in snap.occurrences
                  if o.code in codes and low <= o.day <= high)
    if rows:
        sections.append(_section(PackKind.OCCURRENCES, "\n".join(rows)))

    changes = "\n".join(f"{name}: {len(fps)} — {', '.join(fps) or 'none'}"
                        for name, fps in sorted(selection.changes.items()))
    sections.append(_section(PackKind.CHANGES, changes))
    return EvidencePack(tuple(sections))


_SYNTHESIS_RULES = (
    "You are writing the covering note for one week's attention review for "
    "an Oracle DBA.\n"
    "Rules:\n"
    "- State only what the Material below shows. Never claim a cause, a fix "
    "or a recovery it does not show.\n"
    "- Name a path only as the Material spells it, and only when it is the "
    "evidence for the claim; never invent one.\n"
    "- No [[wikilinks]], no headings, no URLs.\n"
    "\n"
    "Answer with ONE JSON object and nothing else:\n"
    '{"summary": "...", "themes": [{"title": "...", "detail": "...", '
    '"evidence_refs": ["<path>"]}], "evidence_refs": ["<path>"]}\n')


@dataclass(frozen=True)
class Synthesis:
    """The model's covering note, already checked against the pack."""

    summary: str
    themes: tuple[dict, ...]
    evidence_refs: tuple[str, ...]
    model_tier: str

    def to_dict(self) -> dict:
        return {"summary": self.summary,
                "themes": [{**t, "evidence_refs": list(t["evidence_refs"])}
                           for t in self.themes],
                "evidence_refs": list(self.evidence_refs),
                "model_tier": self.model_tier}


def _refs(value, field: str, paths: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(r, str)
                                              for r in value):
        raise ProposalError(f"{field}: expected a list of strings")
    for ref in value:
        if ref not in paths:
            raise ProposalError(f"{field}: {ref!r} is not a path the "
                                f"Material carries")
    return tuple(value)


def _synthesis_parser(paths: frozenset[str]):
    """A `structured._propose` parser that checks rather than believes: a
    citation outside `paths` is a `ProposalError`, so it costs the retry
    rather than being silently dropped. That is the right severity here and
    not in `advisory`, where a live operator can re-click and read what they
    got; a cron run publishes to an inbox nobody is watching."""

    def parse(text: str) -> dict:
        try:
            obj = json.loads(structured._first_json_object(text))
        except json.JSONDecodeError as exc:
            raise ProposalError(f"response: not valid JSON ({exc})") from exc
        if not isinstance(obj, dict):
            raise ProposalError("response: expected a JSON object")
        summary = obj.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ProposalError("summary: missing or empty")
        limit = PACK[PackKind.SUMMARY].max_chars
        if len(summary) > limit:
            raise ProposalError(f"summary: {len(summary)} chars > {limit}")
        raw_themes = obj.get("themes") or []
        if not isinstance(raw_themes, list):
            raise ProposalError("themes: expected a list")
        if len(raw_themes) > PACK[PackKind.FINDING].max_items:
            raise ProposalError(f"themes: {len(raw_themes)} > "
                                f"{PACK[PackKind.FINDING].max_items}")
        themes = []
        for i, theme in enumerate(raw_themes):
            if not isinstance(theme, dict):
                raise ProposalError(f"themes[{i}]: expected an object")
            for key in ("title", "detail"):
                if not isinstance(theme.get(key), str) or not theme[key].strip():
                    raise ProposalError(f"themes[{i}].{key}: missing or empty")
            themes.append({
                "title": theme["title"].strip(),
                "detail": theme["detail"].strip(),
                "evidence_refs": _refs(theme.get("evidence_refs") or [],
                                       f"themes[{i}].evidence_refs", paths)})
        return {"summary": summary.strip(), "themes": tuple(themes),
                "evidence_refs": _refs(obj.get("evidence_refs") or [],
                                       "evidence_refs", paths)}

    return parse


def synthesize(pack: EvidencePack, cfg, *, rules: Rules,
               telemetry: dict | None = None) -> Synthesis:
    """One covering note through `structured._propose`'s retry-once contract,
    with every citation checked against `pack.paths`.

    The call inherits `agents.timeout_seconds` through `structured.generate`
    rather than carrying a review-specific one. `advisory` needs its own
    because it blocks an HTTP handler thread; a cron stage has no handler to
    block, so the pipeline's timeout is the honest default and one fewer
    knob. Raises whatever `_propose` raises — `run` catches it, because a
    synthesis failure must never cost the inbox."""
    answer = structured._propose(pack.prompt(), cfg, _synthesis_parser(pack.paths),
                      escalate=rules.synthesis_tier == "strong",
                      telemetry=telemetry)
    return Synthesis(summary=answer["summary"], themes=answer["themes"],
                     evidence_refs=answer["evidence_refs"],
                     model_tier=rules.synthesis_tier)


def explain(selection: Selection) -> str:
    """The selection as plain text, in `run --explain`'s register: no model
    call, no writes, nothing the evidence does not carry."""
    window = selection.window
    lines = [selection.explanation,
             f"window  {window['from']} .. {window['to']} "
             f"({window['days']} days)",
             f"revision  {selection.source_revision}", ""]
    for finding in selection.findings:
        lines.append(f"{finding.severity:6} {finding.kind}/{finding.code}  "
                     f"{finding.db}  {finding.slug} — {finding.explanation}")
    if not selection.findings:
        lines.append("nothing selected")
    lines.append("")
    for name in CHANGE_BUCKETS:
        fingerprints = selection.changes.get(name) or ()
        if fingerprints:
            lines.append(f"{name}: {', '.join(fingerprints)}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RunOutcome:
    """What one `dbwiki review` tick did. `skipped` names why nothing was
    published, and is empty when something was."""

    review_id: str
    published: bool
    skipped: str
    selected: int
    synthesis_ok: bool
    synthesis_error: str
    delivered: int
    failed: int

    def counts(self) -> dict:
        return {"review_id": self.review_id, "published": self.published,
                "skipped": self.skipped, "selected": self.selected,
                "synthesis_ok": self.synthesis_ok,
                "synthesis_error": self.synthesis_error,
                "delivered": self.delivered, "failed": self.failed}


def _agent_fields(review_id: str, rules: Rules, tele: dict,
                  ok: bool) -> dict:
    """`orchestrate.telemetry_fields`' names for the one model call a review
    makes, so `dbwiki stats` groups it beside every other agent stage."""
    return {"task": "review", "review_id": review_id,
            "model_tier": rules.synthesis_tier, "model": tele.get("model"),
            "adapter": tele.get("adapter"),
            "duration_s": tele.get("duration_s"),
            "timed_out": bool(tele.get("timed_out")),
            "usage": tele.get("usage", "unknown"),
            "attempts": tele.get("attempts", 1), "validation_ok": ok}


def _synthesis_stage(evidence, cfg, *, rules: Rules, state_dir,
                     review_id: str, now: str) -> tuple[Synthesis | None, str]:
    """One synthesis attempt with its spend recorded either way: the
    `agent_runs.jsonl` line is about the attempt, not the outcome, so a
    failed call still lands its duration and usage in the ledger."""
    telemetry: dict = {}
    synthesis, error = None, ""
    try:
        synthesis = synthesize(evidence, cfg, rules=rules,
                               telemetry=telemetry)
    except Exception as exc:  # noqa: BLE001 — a failed synthesis is a category, not a raise
        error = health.categorize(exc)
        _audit(state_dir, "synthesis_failed", review_id=review_id, at=now,
               category=error)
    health.record_agent_run(
        state_dir, _agent_fields(review_id, rules, telemetry,
                                 synthesis is not None), telemetry)
    return synthesis, error


def _prune_history(history: dict, review_id: str, weeks: int) -> dict:
    edge = _week_monday(review_id)
    if edge is None:
        return history
    cutoff = edge - dt.timedelta(weeks=weeks)

    def seen_since_cutoff(entry: dict) -> bool:
        monday = _week_monday(str(entry.get("last_seen") or ""))
        return monday is not None and monday >= cutoff

    return {fp: entry for fp, entry in history.items()
            if seen_since_cutoff(entry)}


def _next_state(state: dict, selection: Selection, review_id: str,
                rules: Rules) -> dict:
    """What the next review remembers. `select` stays pure and reports what it
    saw; the stage that writes the state decides what survives.

    A fingerprint this review reported resolved is forgotten here, so
    `resolved` means "resolved since the review I last published" — the only
    reading an operator can act on — and the same finding coming back next
    week reads as `new`, which is what it is.

    An entry the review produced but did not show keeps its old `last_seen`
    and its `evidence_hash` on purpose. A candidate shadowed by a live alert
    is the case that reaches this: it is not resolved, so it stays, and when
    the alert clears the finding is not re-announced as `new` — honest,
    because the underlying problem persisted the whole time and the review was
    only deferring. `history_weeks` is what eventually ages such an entry
    out."""
    history = {fp: entry for fp, entry in (state.get("history") or {}).items()
               if fp not in set(selection.changes.get("resolved") or ())}
    for name in ("capped", "suppressed", "acknowledged"):
        for fp in selection.changes.get(name) or ():
            if fp in history:
                history[fp] = {**history[fp], "last_seen": review_id}
    for finding in selection.findings:
        prior = history.get(finding.fingerprint) or {}
        history[finding.fingerprint] = {
            "first_seen": prior.get("first_seen") or review_id,
            "last_seen": review_id, "evidence_hash": finding.evidence_hash,
            "kind": finding.kind, "code": finding.code, "slug": finding.slug,
            "db": finding.db}
    return {"schema_version": REVIEW_SCHEMA_VERSION,
            "last_review_id": review_id,
            "history": _prune_history(history, review_id,
                                      rules.history_weeks)}


def _prune_reviews(state_dir, keep: int) -> None:
    for review_id in list_reviews(state_dir)[keep:]:
        (_dir(state_dir) / REVIEWS_DIR / f"{review_id}.json").unlink(
            missing_ok=True)


class _ReviewAudit:
    """`delivery.Audit` against `review/log.jsonl`. `delivery` decides what to
    send and this module says what a row is called and where it lands, which
    is the whole reason the seam is a Protocol.

    Holds nothing but the directory, so the cron stage and the portal may each
    build one. Both write through `_append_capped`, which appends, so neither
    rewrites the other's line."""

    def __init__(self, state_dir):
        self._state_dir = Path(state_dir)

    def delivered_keys(self, review_id: str) -> frozenset[str]:
        path = self._state_dir / AUDIT_LOG
        if not path.exists():
            return frozenset()
        keys = set()
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (row.get("event") == "delivered"
                    and row.get("review_id") == review_id and row.get("key")):
                keys.add(str(row["key"]))
        return frozenset(keys)

    def record(self, attempt) -> None:
        failed = attempt.status == delivery.FAILED
        _audit(self._state_dir,
               "delivery_failed" if failed else "delivered",
               review_id=attempt.delivery.review_id, at=attempt.at,
               key=attempt.delivery.key, channel=attempt.delivery.channel,
               recipient=attempt.delivery.recipient,
               content_class=attempt.delivery.content_class,
               **({"error": attempt.error, "detail": attempt.detail}
                  if failed else {}))


def _deliver(state_dir, document: dict, policy: delivery.Policy, *,
             now: str) -> tuple:
    """Deliver one review and fold the attempts into its published file.

    The rows come back here to be written rather than being written by a sink,
    so `reviews/<id>.json` keeps exactly one writer. Only transport failure is
    swallowed, and `dispatch` has already turned it into a counted row."""
    attempts = delivery.dispatch(document, policy, _ReviewAudit(state_dir),
                                 now=now)
    before = list(document.get("deliveries") or [])
    rows = delivery.merge(before, attempts)
    if rows != before:
        document["deliveries"] = rows
        atomic_write_text(
            _dir(state_dir) / REVIEWS_DIR / f"{document['review_id']}.json",
            json.dumps(document, indent=1, sort_keys=True))
    return attempts


def _delivery_counts(attempts) -> tuple[int, int]:
    delivered = sum(1 for a in attempts if a.status == delivery.DELIVERED)
    return delivered, len(attempts) - delivered


def _assemble(cfg, *, now: str, rules: Rules):
    """The select stage's five inputs and its answer, plus the snapshot the
    pack reads. Read-only: it builds the snapshot at `transaction.head`,
    reads `.state`, and writes nothing."""
    state_dir = Path(cfg.state_dir)
    revision = transaction.head(Path(cfg.wiki_repo))
    snap = readmodel.build(Path(cfg.wiki_repo), revision, now=lambda: now)
    facts = {slug: monitoring.read_facts(state_dir, slug)
             for slug, incident in sorted(snap.incidents.items())
             if incident.is_active}
    state = load_state(state_dir)
    selection = select(snap, facts, state.get("history") or {},
                       load_acks(state_dir),
                       alerts.load_state(state_dir / alerts.ALERT_STATE),
                       now=now, rules=rules, review_id=iso_week(now))
    return snap, state, selection


def selection_for(cfg, *, now: str, rules: Rules | None = None) -> Selection:
    """The week's selection, assembled and returned without publishing
    anything. `run`'s select stage and `--explain` are the same code, so what
    `--explain` prints is what a run would select. No audit row either:
    `selected` is a row about a run, and this is not one."""
    return _assemble(cfg, now=now,
                     rules=rules or Rules.resolve(cfg))[2]


def run(cfg, *, now: str, force: bool = False, deliver_only: bool = False,
        dry_run: bool = False) -> RunOutcome:
    """One weekly tick: select, pack, synthesize, publish, deliver — five
    stages that fail alone.

    `iso_week(now)` is the idempotency key. An existing review for this week
    short-circuits before anything is selected, so a second cron tick costs
    one file read and no model call; `force` republishes over it. Synthesis
    failure is caught, categorized and published anyway: the inbox item is
    the deterministic selection, and losing it because a model was down would
    be the wrong trade. The snapshot is built at `transaction.head`, which
    takes no lock and never touches the wiki's working tree."""
    rules = Rules.resolve(cfg)
    policy = delivery.Policy.resolve(cfg)
    review_id = iso_week(now)
    state_dir = Path(cfg.state_dir)
    if not rules.enabled:
        return RunOutcome(review_id, False, "disabled", 0, False, "", 0, 0)

    existing = load_review(state_dir, review_id)
    if deliver_only:
        attempts = _deliver(state_dir, existing or {}, policy, now=now)
        delivered, failed = _delivery_counts(attempts)
        return RunOutcome(review_id, False, "deliver_only",
                          len((existing or {}).get("findings") or []),
                          bool((existing or {}).get("synthesis")),
                          str((existing or {}).get("synthesis_error") or ""),
                          delivered, failed)
    if existing is not None and not force:
        return RunOutcome(review_id, False, "exists",
                          len(existing.get("findings") or []),
                          bool(existing.get("synthesis")),
                          str(existing.get("synthesis_error") or ""), 0, 0)

    snap, state, selection = _assemble(cfg, now=now, rules=rules)
    revision = selection.source_revision
    _audit(state_dir, "selected", review_id=review_id, at=now,
           selected=selection.counts["selected"],
           shadowed=selection.counts["shadowed"], source_revision=revision,
           **({"dry_run": True} if dry_run else {}))

    evidence = pack(snap, selection, rules=rules)
    synthesis, synthesis_error = None, ""
    if rules.synthesis_enabled and not dry_run:
        synthesis, synthesis_error = _synthesis_stage(
            evidence, cfg, rules=rules, state_dir=state_dir,
            review_id=review_id, now=now)
    if dry_run:
        return RunOutcome(review_id, False, "dry_run",
                          selection.counts["selected"], synthesis is not None,
                          synthesis_error, 0, 0)

    document = {**selection.to_dict(),
                "synthesis": synthesis.to_dict() if synthesis else None,
                "synthesis_error": synthesis_error,
                "pack_manifest": list(evidence.manifest),
                "deliveries": []}
    atomic_write_text(_dir(state_dir) / REVIEWS_DIR / f"{review_id}.json",
                      json.dumps(document, indent=1, sort_keys=True))
    save_state(state_dir, _next_state(state, selection, review_id, rules))
    _prune_reviews(state_dir, rules.keep_reviews)
    _audit(state_dir, "published", review_id=review_id, at=now,
           selected=selection.counts["selected"],
           synthesis_ok=synthesis is not None)
    delivered, failed = _delivery_counts(
        _deliver(state_dir, document, policy, now=now))
    return RunOutcome(review_id, True, "", selection.counts["selected"],
                      synthesis is not None, synthesis_error, delivered,
                      failed)
