"""Fixture-corpus regression: every case in tests/fixtures/manifest.json is
compacted offline through FakeES and compared against checked-in goldens
(normalized events, digest JSON, rendered markdown, trigger decision), plus
the deterministic lint findings of two hand-written broken wiki pages.

Determinism and idempotence live here too: the same case compacted twice must
produce byte-identical output and must not inflate registry state.

Goldens are rewritten by `DBWIKI_UPDATE_GOLDENS=1 uv run pytest
tests/test_fixtures.py`; a golden change requires a `fixture_version` bump in
the manifest (see tests/fixtures/__init__.py)."""

import json

import fixtures as fx
import pytest

from dbwiki.compactor import Compactor
from dbwiki.digest_md import render_md
from dbwiki.lint import lint_wiki
from dbwiki.normalize import UnsupportedSchemaError
from dbwiki.structured import _digest_material, build_prompt
from dbwiki.trigger import decide

MANIFEST = fx.manifest()
CASES = {c["name"]: c for c in MANIFEST["cases"]}
GOLDEN_CASES = [n for n, c in CASES.items() if c["outcome"] != "error"]
ERROR_CASES = [n for n, c in CASES.items() if c["outcome"] == "error"]


@pytest.fixture
def cfg(tmp_path):
    return fx.fixture_config(tmp_path)


def seeded(cfg, name) -> dict:
    case = fx.load_case(name)
    fx.seed_registry(cfg, case)
    return case


def decision(case, digest):
    t = case.get("trigger") or {}
    return decide(digest, ledger_entry=fx.ledger_entry(case, digest),
                  window_to=case["window"]["to"],
                  manual=t.get("manual", False),
                  consolidation=t.get("consolidation", False))


# ---- manifest ----------------------------------------------------------------

def test_manifest_matches_the_case_files():
    on_disk = sorted(p.stem for p in fx.CASE_DIR.glob("*.json"))
    assert sorted(CASES) == on_disk
    assert MANIFEST["fixture_version"] == 7
    assert "synthetic" in MANIFEST["note"]
    for name, entry in CASES.items():
        case = fx.load_case(name)
        assert case["name"] == name
        assert entry["outcome"] in ("wake", "skip", "error")
        assert entry["covers"] and entry["title"]


def test_every_incident_class_has_a_positive_and_a_negative_case():
    """Definition of done: each class of evidence needs a case that fires and
    a case that deliberately does not."""
    outcomes = {n: c["outcome"] for n, c in CASES.items()}
    assert "wake" in outcomes.values() and "skip" in outcomes.values()
    assert outcomes["silence_day"] == "wake"
    assert outcomes["empty_no_baseline"] == "skip"       # silence, negative
    assert outcomes["partial_window_anomaly"] == "wake"
    assert outcomes["routine_traffic"] == "skip"         # rate anomaly, negative
    assert outcomes["ora600_internal_error"] == "wake"
    assert outcomes["dataguard_transport_error"] == "wake"  # error, no delta
    assert {c["layout"] for c in CASES.values()} >= {"legacy", "ecs", "mixed"}


def test_the_after_change_delta_has_a_case_in_each_direction():
    """The window has a direction and only a golden pins it: in
    `lifecycle_changes` the change precedes the error, in
    `error_before_change` it follows."""
    def delta_types(name: str) -> set:
        return {d["type"] for d in fx.golden_digest(name)["deltas"]}
    assert "after_change" in delta_types("lifecycle_changes")
    assert delta_types("error_before_change") == set()


RELOAD_CASE = {
    "db": "cdb1",
    "window": {"from": "2026-07-19T00:00:00Z", "to": "2026-07-20T00:00:00Z",
               "day": "2026-07-19"},
    "hits": {"alert": [], "dataguard": [], "listener": [
        {"_index": ".ds-logs-oracle.listener-default-2026.07.19-000020",
         "_id": "rl-1",
         "_source": {"@timestamp": "2026-07-19T03:30:00Z",
                     "service": {"name": "cdb1.world"},
                     "oracle": {"listener": {"command": "reload",
                                             "return_code": 0}},
                     "message": "19-JUL-2026 03:30:00 * reload * 0"}}]}}


def test_a_lifecycle_rule_with_no_regex_still_stamps_a_line_it_can_quote(cfg):
    """`listener_reload` matches on a field, so it has no matched line, and it
    is the most recent change before many an error. A stamp with an empty
    headline renders as a dangling dash and tells the reader nothing."""
    comp = fx.build_compactor(cfg, RELOAD_CASE)
    w = RELOAD_CASE["window"]
    section = comp._compact_source("listener", "cdb1", w["from"], w["to"],
                                   comp.state.registry("cdb1"), w["day"], 86400.0)
    assert [s.headline for s in section["_stamps"]] == \
        ["19-JUL-2026 03:30:00 * reload * 0"]


# ---- goldens -----------------------------------------------------------------

@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_case_matches_goldens(cfg, name):
    case = seeded(cfg, name)
    digest = fx.compact_case(cfg, case)
    stable = fx.stable_digest(digest)
    fx.assert_golden(f"{name}/events.json", fx.normalized_events(cfg, case))
    fx.assert_golden(f"{name}/digest.json", stable)
    fx.assert_golden(f"{name}/digest.md", render_md(stable), text=True)
    fx.assert_golden(f"{name}/decision.json", decision(case, digest).to_dict())


@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_manifest_outcome_matches_the_golden_decision(cfg, name):
    case = seeded(cfg, name)
    outcome = decision(case, fx.compact_case(cfg, case)).outcome
    expected = CASES[name]["outcome"]
    assert (outcome == expected or
            (expected == "wake" and outcome == "force_consolidation"))


def test_golden_deltas_use_the_current_rate_anomaly_schema():
    """Old-schema digests must never reach render_md (see
    CHANGELOG.md), so no golden may carry the retired keys."""
    for name in GOLDEN_CASES:
        digest = json.loads((fx.GOLDEN_DIR / name / "digest.json").read_text())
        for d in digest["deltas"]:
            if d["type"] != "rate_anomaly":
                continue
            assert set(d) == {"type", "source", "counter", "count",
                              "window_hours", "rate_per_hour",
                              "baseline_median_per_hour"}


# ---- caps, layouts, and the schema diagnostic --------------------------------

def test_storm_hits_both_compactor_caps(cfg):
    case = seeded(cfg, "storm_caps")
    section = fx.compact_case(cfg, case)["sources"]["listener"]
    assert len(section["notable"]) == 20            # max_groups_per_rule
    assert section["dropped_notable_groups"] == {"establish_fail": 3}
    biggest = max(section["notable"], key=lambda g: g["count"])
    assert biggest["count"] == 6 and len(biggest["es_samples"]) == 5


def test_unmatched_groups_hit_their_own_cap(cfg):
    case = seeded(cfg, "unmatched_garbage")
    section = fx.compact_case(cfg, case)["sources"]["alert"]
    assert len(section["notable"]) == 50            # max_unmatched_groups
    assert section["dropped_notable_groups"] == {"unmatched": 3}
    assert set(section["by_class"]) == {"unmatched"}


def test_mixed_layout_window_attributes_every_event(cfg):
    case = seeded(cfg, "routine_traffic")
    events = fx.normalized_events(cfg, case)
    assert {e["db"] for e in events} == {"cdb1"}
    assert len(events) == sum(len(v) for v in case["hits"].values())


@pytest.mark.parametrize("name", ERROR_CASES)
def test_unsupported_layout_fails_loudly(cfg, name):
    case = seeded(cfg, name)
    expect = case["expect_error"]
    with pytest.raises(UnsupportedSchemaError) as e:
        fx.compact_case(cfg, case)
    assert type(e.value).__name__ == expect["type"]
    for fragment in expect["message_contains"]:
        assert fragment in str(e.value)


def test_first_ever_code_delta_is_labelled_with_the_source_that_saw_it(cfg):
    """A code seen only in the alert log yields exactly one `first_ever_code`
    delta, labelled `alert` — registry codes are keyed per source, so the
    dataguard delta pass no longer sees the alert log's codes."""
    case = seeded(cfg, "ora600_internal_error")
    deltas = [d for d in fx.compact_case(cfg, case)["deltas"]
              if d["type"] == "first_ever_code"]
    assert [d["source"] for d in deltas] == ["alert"]
    assert {d["value"] for d in deltas} == {"ORA-600"}


def test_grandfathered_v1_code_never_deltas_as_first_ever(cfg):
    """fra_space_pressure seeds ORA-16038 the pre-v2 way (one global map, no
    source): it must stay known for every source after the migration, while a
    genuinely new code in the same window still deltas."""
    case = seeded(cfg, "fra_space_pressure")
    codes = {d["value"] for d in fx.compact_case(cfg, case)["deltas"]
             if d["type"] == "first_ever_code"}
    assert codes == {"ORA-19815"}


def test_fake_es_discovers_dbs_in_window(cfg):
    case = seeded(cfg, "routine_traffic")
    comp = fx.build_compactor(cfg, case)
    w = case["window"]
    assert comp.discover_dbs(w["from"], w["to"], ["alert", "dataguard"]) == ["cdb1"]


def test_fake_es_rejects_an_unknown_index(cfg):
    comp = fx.build_compactor(cfg, seeded(cfg, "routine_traffic"))
    with pytest.raises(AssertionError, match="no fixture pool"):
        comp.es.count("nope-*", {"bool": {}})


# ---- determinism and idempotence --------------------------------------------

@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_compacting_twice_is_identical(cfg, name):
    """Second pass runs against the registry the first pass persisted — the
    deltas, digest and content hash must not move."""
    case = seeded(cfg, name)
    first = fx.compact_case(cfg, case)
    second = fx.compact_case(cfg, case)
    assert fx.stable_digest(second) == fx.stable_digest(first)
    assert Compactor.content_hash(second) == Compactor.content_hash(first)


@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_deciding_twice_is_identical(cfg, name):
    case = seeded(cfg, name)
    digest = fx.compact_case(cfg, case)
    assert decision(case, digest).to_dict() == decision(case, digest).to_dict()


@pytest.mark.parametrize("name", GOLDEN_CASES)
def test_recompaction_does_not_inflate_registry_state(cfg, name):
    case = seeded(cfg, name)
    reg_path = cfg.state_dir / "registry" / f"{case['db']}.json"
    fx.compact_case(cfg, case)
    after_first = reg_path.read_text()
    fx.compact_case(cfg, case)
    assert reg_path.read_text() == after_first


def test_partial_rerun_never_degrades_a_full_day_record(cfg):
    """`set_day_counts` monotonicity end to end: a 2h re-tick over a day that
    already has full-day coverage leaves the full-day record alone."""
    full = seeded(cfg, "late_replayed_events")       # 24h window, day 2026-07-12
    fx.compact_case(cfg, full)
    reg_path = cfg.state_dir / "registry" / "cdb1.json"
    before = json.loads(reg_path.read_text())["daily_counts"]["2026-07-12"]["alert"]
    assert before["seconds"] == 86400.0

    partial = seeded(cfg, "partial_window_anomaly")  # 2h window, same day
    fx.compact_case(cfg, partial)
    after = json.loads(reg_path.read_text())["daily_counts"]["2026-07-12"]["alert"]
    assert after == before


def test_replayed_duplicate_ids_do_not_move_registry_timestamps(cfg):
    case = seeded(cfg, "late_replayed_events")
    fx.compact_case(cfg, case)
    codes = json.loads(
        (cfg.state_dir / "registry" / "cdb1.json").read_text())["codes"]["alert"]
    # seeded before the window, so a replay inside it may only extend last_seen
    assert codes["ORA-3113"]["first_seen"] == "2026-05-02T08:00:00Z"
    assert codes["ORA-3113"]["last_seen"] == "2026-07-12T03:00:00Z"


def test_explain_style_compaction_persists_nothing(cfg):
    """`persist=False` (dbwiki run --explain) must leave no registry trace."""
    case = fx.load_case("ora600_internal_error")     # deliberately not seeded
    digest = fx.compact_case(cfg, case, persist=False)
    assert digest["notable"]
    assert not (cfg.state_dir / "registry" / "cdb1.json").exists()


# ---- deterministic wiki lint -------------------------------------------------

def test_broken_fixture_pages_match_the_lint_golden():
    findings = [f.to_dict() for f in lint_wiki(fx.LINT_WIKI)]
    fx.assert_golden("lint/findings.json", findings)
    files = {f["file"] for f in findings}
    assert files == {"errors/ORA-12543.md",
                     "incidents/2026-07-13-cdb1-telemetry-blackout.md"}
    assert {f["rule"] for f in findings} == {
        "frontmatter-contradictory", "wikilink-broken", "digest-missing",
        "citation-malformed"}


# ---- context lines -----------------------------------------------------------

CTX_ROUTINE = "Thread 1 advanced to log sequence {n} (LGWR switch)"
CTX_ERROR = ("ORA-00600: internal error code, arguments: [kdsgrp1], [], [], "
             "[], [], [], [], []")


def ctx_hit(n: int, message: str) -> dict:
    return {"_index": "oracle-logs-alert-2026.07", "_id": f"x{n:02d}",
            "_source": {"@timestamp": f"2026-07-11T04:{n:02d}:00Z",
                        "db_name": "cdb1",
                        "oracle": {"alert_message": message,
                                   "msg_type": "NOTIFICATION", "msg_id": "",
                                   "msg_level": 16, "con_id": "0"}}}


def ctx_case(messages: list[str]) -> dict:
    """A legacy-layout alert window whose events are exactly `messages`, one a
    minute, in the order given."""
    return {
        "name": "context_lines", "db": "cdb1",
        "window": {"from": "2026-07-11T00:00:00Z",
                   "to": "2026-07-12T00:00:00Z", "day": "2026-07-11"},
        "hits": {"alert": [ctx_hit(i, m) for i, m in enumerate(messages)],
                 "listener": [], "dataguard": []},
    }


def ctx_of(digest: dict) -> dict:
    return digest["sources"]["alert"]["notable"][0]["context"]


def test_a_group_captures_the_lines_on_either_side_of_it(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    msgs = ([CTX_ROUTINE.format(n=4700 + i) for i in range(4)] + [CTX_ERROR]
            + [CTX_ROUTINE.format(n=4800 + i) for i in range(4)])
    section = fx.compact_case(cfg, ctx_case(msgs))["sources"]["alert"]
    assert len(section["notable"]) == 1
    ctx = section["notable"][0]["context"]
    assert [c["message"] for c in ctx["before"]] == msgs[1:4]
    assert [c["message"] for c in ctx["after"]] == msgs[5:8]
    assert [c["class"] for c in ctx["before"]] == ["routine"] * 3
    assert [c["class"] for c in ctx["after"]] == ["routine"] * 3
    assert ctx["before"][0]["ts"] == "2026-07-11T04:01:00Z"
    assert CTX_ERROR not in [c["message"] for c in ctx["before"] + ctx["after"]], \
        "the representative event never appears in its own context"


def test_a_group_at_the_start_of_the_window_has_no_before_lines(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    msgs = [CTX_ERROR] + [CTX_ROUTINE.format(n=4700 + i) for i in range(3)]
    ctx = ctx_of(fx.compact_case(cfg, ctx_case(msgs)))
    assert ctx["before"] == []
    assert [c["message"] for c in ctx["after"]] == msgs[1:]


def test_the_last_group_in_the_window_has_a_short_after(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    msgs = [CTX_ROUTINE.format(n=4700 + i) for i in range(3)] + [CTX_ERROR]
    ctx = ctx_of(fx.compact_case(cfg, ctx_case(msgs)))
    assert len(ctx["before"]) == 3
    assert ctx["after"] == [], "nothing follows the window's last event"

    trailing = CTX_ROUTINE.format(n=4900)
    ctx = ctx_of(fx.compact_case(cfg, ctx_case(msgs + [trailing])))
    assert [c["message"] for c in ctx["after"]] == [trailing]


def test_only_the_leading_groups_keep_context_and_errors_come_first(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3, "context_max_groups": 2}
    unmatched = [f"zzz unparsed vendor payload {c}" for c in "abc"]
    msgs = ([CTX_ROUTINE.format(n=4700)] + unmatched + [CTX_ERROR]
            + [CTX_ROUTINE.format(n=4800)])
    notable = fx.compact_case(cfg, ctx_case(msgs))["sources"]["alert"]["notable"]
    assert len(notable) == 4
    assert notable[0]["class"] == "error"
    assert [("context" in g) for g in notable] == [True, True, False, False], \
        "the error group keeps its context even though it happened last"


def test_no_context_key_reaches_the_digest_when_the_feature_is_off(cfg):
    assert cfg.compactor["context_lines"] == 0, "the corpus goldens rely on it"
    digest = fx.compact_case(cfg, seeded(cfg, "ora600_internal_error"))
    assert '"context"' not in json.dumps(digest), \
        "context_lines: 0 must leave the digest as it was before the feature"


def test_context_never_moves_the_content_hash(cfg):
    case = seeded(cfg, "ora600_internal_error")
    off = fx.compact_case(cfg, case)
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    on = fx.compact_case(cfg, case)
    assert "context" in on["sources"]["alert"]["notable"][0]
    assert Compactor.content_hash(on) == Compactor.content_hash(off)

    for g in on["sources"]["alert"]["notable"]:
        g["context"]["before"] = []
        g["context"]["after"] = [{"ts": "2026-07-11T05:00:00Z",
                                  "class": "routine", "message": "invented"}]
    assert Compactor.content_hash(on) == Compactor.content_hash(off), \
        "context sits outside the hash, so retuning it re-ingests nothing"


def test_render_md_shows_the_context_block_under_the_group(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    before, after = CTX_ROUTINE.format(n=4711), CTX_ROUTINE.format(n=4712)
    md = render_md(fx.compact_case(cfg, ctx_case([before, CTX_ERROR, after])))
    assert "  - context:" in md
    assert "      >> this event" in md
    assert f"      > [routine] 2026-07-11T04:00:00Z {before}" in md
    assert f"      > [routine] 2026-07-11T04:02:00Z {after}" in md


def test_the_ingest_prompt_carries_the_context_rule_and_block(cfg, tmp_path):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    neighbour = CTX_ROUTINE.format(n=4711)
    digest = fx.compact_case(cfg, ctx_case([neighbour, CTX_ERROR]))
    (tmp_path / "wiki").mkdir(exist_ok=True)
    prompt = build_prompt("cdb1", digest, tmp_path / "wiki")
    assert "never count its lines as events" in prompt, \
        "the reading rule travels with the block"
    assert "  - context:" in prompt
    assert ">> this event" in prompt
    assert neighbour in prompt


def test_the_escalated_material_keeps_the_context_block(cfg):
    cfg.compactor = {**cfg.compactor, "context_lines": 3}
    neighbour = CTX_ROUTINE.format(n=4711)
    material = _digest_material(
        render_md(fx.compact_case(cfg, ctx_case([neighbour, CTX_ERROR]))))
    assert "  - context:" in material
    assert ">> this event" in material
    assert neighbour in material
