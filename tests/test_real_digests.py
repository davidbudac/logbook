"""Regression against real compactor output: the sanitized digests in
tests/fixtures/real_digests/ replayed through every layer that takes a digest
as *input* — trigger decisions, markdown rendering, the digest's own code list
and the structured ingest prompt.

These digests cannot drive the compactor (they are its output, and the events
behind them are unrecoverable), so nothing here compacts anything. What they
buy is shape: storms collapsed into one group, `noise` classes, 67-group days,
a rendering long enough to trip the prompt cap, and days with no events at all
— none of which the hand-built cases in tests/fixtures/cases/ reproduce.

Goldens live under golden/real/<name>/ and follow the corpus rules: rewrite
with DBWIKI_UPDATE_GOLDENS=1, and treat a change as a behaviour change.

Pure layers only: no Elasticsearch, no orchestrator, no model call."""

import json
import re

import fixtures as fx
import pytest

from dbwiki.compactor import Compactor
from dbwiki.digest_md import render_md
from dbwiki.structured import MAX_DIGEST_CHARS, build_prompt, digest_codes
from dbwiki.trigger import decide, digest_needs_escalation

MANIFEST = fx.manifest()["real_digests"]
ENTRIES = {e["name"]: e for e in MANIFEST["digests"]}
NAMES = sorted(ENTRIES)

# every delta type the compactor emits, keyed by the exact fields it writes
DELTA_SCHEMA = {
    "first_ever_code": {"type", "source", "value", "first_seen"},
    "new_service": {"type", "source", "value"},
    "new_client_program": {"type", "source", "value"},
    "rate_anomaly": {"type", "source", "counter", "count", "window_hours",
                     "rate_per_hour", "baseline_median_per_hour"},
    "silence": {"type", "source", "detail"},
}

# addresses and hostnames are only readable off the fields that label them —
# Oracle version strings (19.0.0.0) are not addresses
_ADDR_FIELD = re.compile(r"IP Address: ([\d.]+)")
_HOST_FIELD = re.compile(r"HOST=([^)\"\s]+)")
_DOC_NETS = ("192.0.2.", "198.51.100.", "203.0.113.")   # RFC 5737
_PLACEHOLDER_HOSTS = {"host-a.localdomain", "host-b.localdomain"}


@pytest.fixture(params=NAMES)
def name(request):
    return request.param


def digest(name: str) -> dict:
    return fx.load_real_digest(name)


def decision(d: dict):
    """No ledger entry: the first sight of this window, which is the decision
    the live run actually made."""
    return decide(d, ledger_entry=None, window_to=d["window"]["to"])


# ---- manifest ----------------------------------------------------------------

def test_manifest_matches_the_real_digest_files():
    assert NAMES == fx.real_digest_names()
    for name, entry in ENTRIES.items():
        d = digest(name)
        assert entry["db"] == d["db"] and entry["day"] == d["window"]["day"]
        assert entry["from"].startswith("wiki/digests/")
        assert entry["outcome"] in ("wake", "skip")
        assert entry["model_tier"] in ("cheap", "strong")
        assert entry["covers"]


def test_the_set_covers_both_outcomes_and_both_tiers():
    """Same definition of done as the synthetic corpus: a real digest that
    fires and a real digest that deliberately does not, at each tier."""
    outcomes = {e["outcome"] for e in ENTRIES.values()}
    tiers = {e["model_tier"] for e in ENTRIES.values()}
    assert outcomes == {"wake", "skip"} and tiers == {"cheap", "strong"}
    delta_types = {dl["type"] for n in NAMES for dl in digest(n)["deltas"]}
    assert delta_types == set(DELTA_SCHEMA)


# ---- sanitization ------------------------------------------------------------

def test_no_real_address_or_hostname_survives(name):
    """Shape guard, so a later copy cannot smuggle in a host or an address the
    substitution map never heard of."""
    text = (fx.REAL_DIGEST_DIR / f"{name}.json").read_text()
    assert all(a.startswith(_DOC_NETS) for a in _ADDR_FIELD.findall(text))
    assert set(_HOST_FIELD.findall(text)) <= _PLACEHOLDER_HOSTS


def test_every_documented_substitution_was_applied():
    """The scrub is only reproducible if the map is written down: each key
    must be gone from the corpus and each replacement must be in use."""
    corpus = "".join((fx.REAL_DIGEST_DIR / f"{n}.json").read_text()
                     for n in NAMES)
    applied = MANIFEST["sanitization"]["applied"]
    assert applied
    for original, replacement in applied.items():
        assert original not in corpus
        assert replacement in corpus


# ---- schema ------------------------------------------------------------------

def test_real_digest_parses_with_the_current_schema(name):
    """These files were written by the live compactor: if a schema tweak lands
    without a migration, this is where it shows — no shims allowed."""
    d = digest(name)
    assert set(d) == {"db", "window", "generated_by", "pattern_versions",
                      "sources", "deltas", "totals", "notable"}
    assert set(d["window"]) == {"from", "to", "day"}
    assert set(d["totals"]) == {"events", "notable_events", "notable_groups"}
    for section in d["sources"].values():
        assert {"total_events", "by_class", "routine_counters", "notable"} <= set(section)
        for g in section["notable"]:
            assert set(g) == {"rule", "class", "count", "first_ts", "last_ts",
                              "codes", "message", "template", "es_samples"}
    for dl in d["deltas"]:
        assert set(dl) == DELTA_SCHEMA[dl["type"]]


def test_totals_agree_with_the_sections(name):
    d = digest(name)
    groups = [g for s in d["sources"].values() for g in s["notable"]]
    assert d["totals"]["events"] == sum(s["total_events"]
                                        for s in d["sources"].values())
    assert d["totals"]["notable_groups"] == len(groups)
    assert d["totals"]["notable_events"] == sum(g["count"] for g in groups)


# ---- trigger -----------------------------------------------------------------

def test_decision_matches_the_golden(name):
    fx.assert_golden(f"real/{name}/decision.json",
                     decision(digest(name)).to_dict())


def test_decision_matches_the_manifest(name):
    d = decision(digest(name))
    assert d.outcome == ENTRIES[name]["outcome"]
    assert d.model_tier == ENTRIES[name]["model_tier"]


def test_escalation_agrees_with_the_decision_tier(name):
    d = digest(name)
    assert digest_needs_escalation(d) == (decision(d).model_tier == "strong")


def test_an_already_ingested_window_is_skipped_as_unchanged(name):
    """The dedupe path over real content hashes: the same digest replayed
    after ingestion must skip, whatever it says."""
    d = digest(name)
    prior = {"status": "ingested", "window_to": "2000-01-01T00:00:00Z",
             "content_hash": Compactor.content_hash(d)}
    got = decide(d, ledger_entry=prior, window_to=d["window"]["to"])
    assert got.outcome == "skip"
    assert [r["code"] for r in got.reasons] == ["content_unchanged"]


def test_deciding_twice_is_identical(name):
    d = digest(name)
    assert decision(d).to_dict() == decision(d).to_dict()


def test_content_hash_ignores_the_window(name):
    """Why the dedupe above works: a re-tick whose window moved but whose
    events did not must hash the same."""
    d = digest(name)
    moved = json.loads(json.dumps(d))
    moved["window"] = {"from": "2027-01-01T00:00:00Z",
                       "to": "2027-01-02T00:00:00Z", "day": "2027-01-01"}
    assert Compactor.content_hash(moved) == Compactor.content_hash(d)


def test_no_two_real_days_share_a_content_hash():
    assert len({Compactor.content_hash(digest(n)) for n in NAMES}) == len(NAMES)


# ---- rendering ---------------------------------------------------------------

def test_rendered_markdown_matches_the_golden(name):
    fx.assert_golden(f"real/{name}/digest.md", render_md(digest(name)),
                     text=True)


def test_rendering_survives_a_day_with_nothing_in_it():
    md = render_md(digest("empty_partial_window"))
    assert "0 total, 0 notable in 0 groups" in md
    assert "## Deltas" not in md and "### Notable" not in md


def test_the_storm_stays_one_group_with_capped_samples():
    """1,753 fatal-NI events, one group, five samples — the collapse the
    synthetic storm case models, as the fleet actually produced it."""
    section = digest("fra_transport_storm")["sources"]["alert"]
    storm = max(section["notable"], key=lambda g: g["count"])
    assert storm["rule"] == "tns_error" and storm["count"] == 1753
    assert len(storm["es_samples"]) == 5
    assert "host-a.localdomain" in storm["message"]


def test_the_unmatched_cap_hit_is_rendered():
    md = render_md(digest("rate_anomaly_new_codes"))
    assert "_group cap hit, distinct groups dropped: unmatched: 69_" in md


# ---- structured ingest inputs ------------------------------------------------

def test_digest_codes_are_exactly_the_codes_the_digest_shows(name):
    d = digest(name)
    codes = digest_codes(d)
    assert len(codes) == len(set(codes))
    carried = {c for s in d["sources"].values() for g in s["notable"]
               for c in g["codes"]}
    carried |= {dl["value"] for dl in d["deltas"]
                if dl["type"] == "first_ever_code"}
    assert set(codes) == carried


def test_prompt_carries_the_digest_its_codes_and_the_contract(name, tmp_path):
    d = digest(name)
    p = build_prompt(d["db"], d, tmp_path)          # empty wiki: no dedup context
    assert f"digests/{d['db']}/{d['window']['day']}.md" in p
    assert render_md(d)[:400] in p                  # the digest body, truncated or not
    assert '"schema_version": 1' in p and "NOTHING else" in p
    assert "- (none)" in p                          # no open incidents
    for code in digest_codes(d):
        assert f"errors/{code}.md does not exist yet" in p


def test_prompt_lists_no_codes_for_a_quiet_day(tmp_path):
    d = digest("listener_only_routine_day")
    p = build_prompt(d["db"], d, tmp_path)
    assert "- (no codes)" in p
    assert "leave the list empty" in p


def test_a_real_digest_trips_the_prompt_truncation(tmp_path):
    """23k rendered characters against MAX_DIGEST_CHARS=12000 — the cap is not
    hypothetical, one ordinary fleet day exceeds it."""
    d = digest("rate_anomaly_new_codes")
    assert len(render_md(d)) > MAX_DIGEST_CHARS
    p = build_prompt(d["db"], d, tmp_path)
    assert "_(digest truncated for this prompt)_" in p
    for code in digest_codes(d):                    # the code list is not cut
        assert code in p.split("Digest (")[0]


def test_the_open_incident_context_reaches_the_prompt(tmp_path):
    """A digest is only half the prompt; the other half is what the wiki
    already says about this db."""
    d = digest("fra_transport_storm")
    (tmp_path / "incidents").mkdir()
    (tmp_path / "incidents" / "2026-07-12-cdb1-fra-full.md").write_text(
        "---\ntype: incident\nstatus: open\ndb: cdb1\n---\n\n"
        "# cdb1 recovery area is full\n")
    (tmp_path / "errors").mkdir()
    (tmp_path / "errors" / "ORA-19815.md").write_text("# ORA-19815\n")
    p = build_prompt("cdb1", d, tmp_path)
    assert "incidents/2026-07-12-cdb1-fra-full.md — cdb1 recovery area is full" in p
    assert "errors/ORA-19815.md already exists" in p
    assert "errors/TNS-12543.md does not exist yet" in p
