"""Structured (non-roaming) ingest: the prompt the local model sees, strict
validation of its JSON, the retry-once path, and the deterministic writers —
whose output must be lint-clean and idempotent by construction."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from dbwiki import structured
from dbwiki.digest_md import render_md
from dbwiki.harness import HarnessError
from dbwiki.incidents import (ActionRecord, Status, append_action,
                              read_incident, render_action)
from dbwiki.lint import blocking, lint_wiki
from dbwiki.orchestrate import Orchestrator
from dbwiki.lock import Held
from dbwiki.structured import (ProposalError, apply_proposal, build_prompt,
                               parse_proposal, propose)

DAY = "2026-07-12"
REL_MD = f"digests/cdb1/{DAY}.md"

DIGEST = {
    "db": "cdb1",
    "window": {"from": f"{DAY}T00:00:00Z", "to": "2026-07-13T00:00:00Z",
               "day": DAY},
    "generated_by": "dbwiki-compactor/test",
    "pattern_versions": {"alert": 1},
    "sources": {"alert": {
        "total_events": 12, "by_class": {"error": 12}, "routine_counters": {},
        "notable": [{"rule": "ora_error", "class": "error", "count": 12,
                     "first_ts": f"{DAY}T01:00:00Z", "last_ts": f"{DAY}T04:00:00Z",
                     "codes": ["ORA-00600"], "message": "ORA-00600: internal error",
                     "template": "ORA-#: internal error", "es_samples": []}]}},
    "deltas": [{"type": "first_ever_code", "value": "TNS-12543",
                "source": "listener", "first_seen": f"{DAY}T00:00:32Z"}],
    "totals": {"events": 12, "notable_events": 12, "notable_groups": 1},
    "notable": True,
}

OPEN_INCIDENT = ("---\ntype: incident\nstatus: open\ndb: cdb1\n"
                 "opened: 2026-07-11T00:00:00Z\n---\n\n"
                 "# cdb1 loses its standby link\n\nOngoing.\n\n"
                 "## Evidence\n\n- 2026-07-11: digests/cdb1/2026-07-11.md\n")

NOW = "2026-07-27T18:00:00Z"


@pytest.fixture
def wiki(tmp_path):
    """A minimal wiki: index, log, and the digests this day's proposal cites."""
    root = tmp_path / "wiki"
    (root / "digests" / "cdb1").mkdir(parents=True)
    (root / "digests" / "cdb1" / f"{DAY}.md").write_text(render_md(DIGEST))
    (root / "digests" / "cdb1" / f"{DAY}.json").write_text(json.dumps(DIGEST))
    (root / "digests" / "cdb1" / "2026-07-11.md").write_text("# older digest\n")
    (root / "index.md").write_text("---\ntype: index\n---\n\n# Logbook\n\n"
                                   "## Databases\n\n(pages appear as ingestion runs)\n")
    (root / "log.md").write_text("---\ntype: log\n---\n\n# Log\n")
    return root


@pytest.fixture
def wiki_with_incident(wiki):
    (wiki / "incidents").mkdir()
    (wiki / "incidents" / "2026-07-11-cdb1-standby-gap.md").write_text(OPEN_INCIDENT)
    return wiki


def raw(**over) -> dict:
    p = {"schema_version": 1,
         "summary": "ORA-00600 storm on cdb1",
         "notable": True,
         "journal_entry": "Twelve ORA-00600 errors hit cdb1 between 01:00 and "
                          "04:00 UTC. No recovery evidence in the window.",
         "error_updates": [{"code": "ORA-00600", "note": "12 hits overnight"}],
         "incident": {"action": "none", "slug": None, "title": None,
                      "body": None, "existing_page": None},
         "flags": []}
    p.update(over)
    return p


def parsed(**over) -> dict:
    """Proposals always reach apply_proposal through the validator."""
    return parse_proposal(json.dumps(raw(**over)))


# ---- prompt -------------------------------------------------------------------

def test_prompt_carries_the_digest_and_the_dedup_context(wiki_with_incident):
    (wiki_with_incident / "errors").mkdir()
    (wiki_with_incident / "errors" / "ORA-00600.md").write_text(
        "---\ntype: error-class\n---\n\n# ORA-00600\n")
    p = build_prompt("cdb1", DIGEST, wiki_with_incident)
    assert "ORA-00600: internal error" in p          # digest body
    assert "12 events" in p or "12 total" in p       # rendered digest header
    assert REL_MD in p
    # dedup context: the open incident and which error pages already exist
    assert "incidents/2026-07-11-cdb1-standby-gap.md" in p
    assert "cdb1 loses its standby link" in p and "status: open" in p
    assert "errors/ORA-00600.md already exists" in p
    assert "errors/TNS-12543.md does not exist yet" in p  # from a delta
    # the response contract, spelled out
    assert '"schema_version": 1' in p and "NOTHING else" in p


def test_prompt_truncates_a_huge_digest(wiki):
    big = json.loads(json.dumps(DIGEST))
    big["sources"]["alert"]["notable"][0]["message"] = "x" * 40000
    p = build_prompt("cdb1", big, wiki)
    assert "digest truncated" in p and len(p) < 20000


def test_prompt_without_incidents_or_codes(wiki):
    quiet = json.loads(json.dumps(DIGEST))
    quiet["sources"]["alert"]["notable"] = []
    quiet["deltas"] = []
    p = build_prompt("cdb1", quiet, wiki)
    assert "- (none)" in p and "- (no codes)" in p


# ---- database history in the prompt -------------------------------------------

@pytest.fixture
def wiki_with_history(wiki_with_incident):
    root = wiki_with_incident
    (root / "databases" / "cdb1" / "journal").mkdir(parents=True)
    (root / "databases" / "cdb1" / "journal" / "2026-07.md").write_text(
        "---\ntype: journal\ndb: cdb1\n---\n\n# cdb1 journal 2026-07\n\n"
        "## 2026-07-11 \u2014 cdb1 2026-07-11: standby transport stalled\n\n"
        "The standby fell behind.\n")
    return root


def test_prompt_carries_the_database_history(wiki_with_history):
    p = build_prompt("cdb1", DIGEST, wiki_with_history)
    assert ("Database history for cdb1 (last 90 days; context from earlier "
            "days, not evidence for today):") in p
    assert "Journal (days with events):" in p
    assert "- 2026-07-11 \u2014 cdb1 2026-07-11: standby transport stalled" in p


def test_the_history_block_does_not_repeat_the_open_incidents_above_it(
        wiki_with_history):
    """Defect 4: `build_prompt` already prints every open incident for the db
    right above the block, and cdb1 has fourteen of them in the live wiki."""
    p = build_prompt("cdb1", DIGEST, wiki_with_history)
    assert p.count("incidents/2026-07-11-cdb1-standby-gap.md") == 1
    assert "Resolved incidents:" not in p


def test_the_history_block_sits_between_the_error_pages_and_the_digest(
        wiki_with_history):
    head, _, tail = build_prompt(
        "cdb1", DIGEST, wiki_with_history).partition("Database history for")
    assert "Error pages for this digest's codes:" in head
    assert "Digest (" not in head and tail.count("Digest (") == 1


def test_history_days_zero_removes_the_block_and_nothing_else(
        wiki_with_history):
    full = build_prompt("cdb1", DIGEST, wiki_with_history)
    block = full.partition("Database history for")[2].partition(
        "\n\nDigest (")[0]
    assert full.replace(f"Database history for{block}\n\n", "") == build_prompt(
        "cdb1", DIGEST, wiki_with_history, history_days=0)


def test_a_wiki_that_remembers_nothing_leaves_no_gap(wiki):
    p = build_prompt("cdb1", DIGEST, wiki)
    assert "Database history for" not in p
    assert "errors/TNS-12543.md does not exist yet\n\nDigest (" in p


def test_the_template_rules_the_history_block_as_context_not_evidence():
    assert ('- The "Database history" block is what this database did on '
            "earlier days. Use it to say whether today's events are new, "
            "recurring, or follow a recorded change; never cite it as "
            "evidence for today and never restate it as a fact of this "
            "digest.") in structured.INGEST_TEMPLATE


# ---- parse_proposal -----------------------------------------------------------

def test_parses_clean_fenced_and_prose_wrapped_json():
    body = json.dumps(raw())
    for text in (body,
                 f"```json\n{body}\n```",
                 f"Sure! Here is my analysis:\n\n{body}\n\nHope that helps.",
                 f"```\n{body}\n```\nTrailing chatter."):
        got = parse_proposal(text)
        assert got["summary"] == "ORA-00600 storm on cdb1"
        assert got["error_updates"][0]["code"] == "ORA-00600"
        assert got["incident"]["action"] == "none"


def test_missing_optional_blocks_default_to_empty():
    got = parse_proposal(json.dumps({
        "schema_version": 1, "summary": "quiet day", "notable": False,
        "journal_entry": "Nothing of note."}))
    assert got["error_updates"] == [] and got["flags"] == []
    assert got["incident"]["action"] == "none"


BAD = [
    ("no json here at all", "response"),
    ('{"schema_version": 1, "summary": "x"', "response"),
    ('{"summary": "x"}', "schema_version"),
    (json.dumps(raw(schema_version=2)), "schema_version"),
    (json.dumps(raw(summary=7)), "summary"),
    (json.dumps(raw(summary="")), "summary"),
    (json.dumps(raw(notable="yes")), "notable"),
    (json.dumps(raw(journal_entry="  ")), "journal_entry"),
    (json.dumps(raw(error_updates={"code": "ORA-00600"})), "error_updates"),
    (json.dumps(raw(error_updates=["ORA-00600"])), "error_updates[0]"),
    (json.dumps(raw(error_updates=[{"note": "n"}])), "error_updates[0].code"),
    (json.dumps(raw(error_updates=[{"code": "oops", "note": "n"}])),
     "error_updates[0].code"),
    (json.dumps(raw(error_updates=[{"code": "ORA-00600"}])),
     "error_updates[0].note"),
    (json.dumps(raw(incident="open")), "incident"),
    (json.dumps(raw(incident={"action": "close"})), "incident.action"),
    (json.dumps(raw(incident={"action": "open", "title": "t", "body": "b"})),
     "incident.slug"),
    (json.dumps(raw(incident={"action": "open", "slug": "Not A Slug",
                              "title": "t", "body": "b"})), "incident.slug"),
    (json.dumps(raw(incident={"action": "open", "slug": "s", "body": "b"})),
     "incident.title"),
    (json.dumps(raw(incident={"action": "open", "slug": "s", "title": "t"})),
     "incident.body"),
    (json.dumps(raw(incident={"action": "update", "body": "b"})),
     "incident.existing_page"),
    (json.dumps(raw(incident={"action": "none", "slug": 3})), "incident.slug"),
    (json.dumps(raw(flags="oops")), "flags"),
    (json.dumps(raw(flags=[1])), "flags[0]"),
]


def test_long_summary_is_cut_at_a_word_and_flagged():
    words = " ".join(f"w{i}" for i in range(80))
    p = structured.parse_proposal(json.dumps(raw(summary=words)))
    assert len(p["summary"]) <= structured.MAX_SUMMARY
    assert p["summary"].endswith("…") and " w" in p["summary"]
    assert not p["summary"][:-1].endswith("w")  # no half word before the cut
    assert p["flags"] == [f"summary was {len(words)} characters, "
                          f"cut to {len(p['summary'])}"]
    assert structured.parse_proposal(
        json.dumps(raw(summary="x" * 200)))["flags"] == []


@pytest.mark.parametrize("text,field", BAD, ids=[f"{i}-{f}" for i, (_, f)
                                                 in enumerate(BAD)])
def test_each_bad_field_is_rejected_by_name(text, field):
    with pytest.raises(ProposalError, match=field.replace("[", r"\[")
                       .replace("]", r"\]").replace(".", r"\.")):
        parse_proposal(text)


# ---- propose: retry once ------------------------------------------------------

def calls(monkeypatch, *answers):
    seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.5,
                             exit_code=0, timed_out=False, usage="unknown")
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(structured, "generate", _gen)
    return seen


def test_propose_retries_once_quoting_the_validation_error(monkeypatch):
    seen = calls(monkeypatch, "not json", json.dumps(raw()))
    tele = {}
    got = propose("PROMPT", SimpleNamespace(agents={}), telemetry=tele)
    assert got["summary"] == "ORA-00600 storm on cdb1"
    assert len(seen) == 2
    assert seen[0] == "PROMPT"
    assert "was rejected" in seen[1] and "response" in seen[1]
    assert tele["adapter"] == "pi"


def test_propose_gives_up_after_the_second_bad_answer(monkeypatch):
    seen = calls(monkeypatch, "nope", json.dumps(raw(notable="yes")))
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        propose("PROMPT", SimpleNamespace(agents={}))
    assert len(seen) == 2


def test_propose_accepts_the_first_answer_without_a_retry(monkeypatch):
    seen = calls(monkeypatch, json.dumps(raw()))
    tele = {}
    assert propose("PROMPT", SimpleNamespace(agents={}),
                   telemetry=tele)["notable"] is True
    assert len(seen) == 1
    assert tele["attempts"] == 1


def test_a_retried_proposal_records_two_attempts(monkeypatch):
    """A structured stage that always needs its retry costs twice as much;
    `dbwiki stats` can only see that if the attempt count is recorded."""
    calls(monkeypatch, "not json", json.dumps(raw()))
    tele = {}
    propose("PROMPT", SimpleNamespace(agents={}), telemetry=tele)
    assert tele["attempts"] == 2


def test_a_failed_retry_still_records_two_attempts(monkeypatch):
    calls(monkeypatch, "nope", "still not json")
    tele = {}
    with pytest.raises(HarnessError):
        propose("PROMPT", SimpleNamespace(agents={}), telemetry=tele)
    assert tele["attempts"] == 2


# ---- apply_proposal -----------------------------------------------------------

def apply(wiki, proposal, now=NOW, digest=DIGEST):
    return apply_proposal(wiki, "cdb1", digest, REL_MD, proposal, now)


def test_routine_day_writes_journal_profile_index_and_log(wiki):
    res = apply(wiki, parsed(notable=False, error_updates=[]))
    assert res["task"] == "ingest" and res["db"] == "cdb1"
    assert res["notable"] is False and res["mode"] == "structured"
    assert res["pages_touched"] == ["databases/cdb1.md",
                                    "databases/cdb1/journal/2026-07.md",
                                    "index.md", "log.md"]
    assert res["incidents_opened"] == [] and res["incidents_updated"] == []

    journal = (wiki / "databases/cdb1/journal/2026-07.md").read_text()
    assert journal.startswith("---\ntype: journal\ndb: cdb1\n---\n")
    assert f"## {DAY} — ORA-00600 storm on cdb1" in journal
    assert f"evidence: {REL_MD}" in journal
    assert "type: database" in (wiki / "databases/cdb1.md").read_text()

    index = (wiki / "index.md").read_text()
    assert "[[databases/cdb1]]" in index
    assert "[[databases/cdb1/journal/2026-07]]" in index
    assert "(pages appear as ingestion runs)" not in index  # placeholder gone
    assert (wiki / "log.md").read_text().endswith(
        f"[{NOW}] ingest — cdb1 {DAY}: ORA-00600 storm on cdb1\n")


def test_second_day_appends_to_the_same_journal_without_new_frontmatter(wiki):
    apply(wiki, parsed())
    day2 = json.loads(json.dumps(DIGEST))
    day2["window"]["day"] = "2026-07-13"
    (wiki / "digests" / "cdb1" / "2026-07-13.md").write_text("# d\n")
    res = apply_proposal(wiki, "cdb1", day2, "digests/cdb1/2026-07-13.md",
                         parsed(summary="quiet"), "2026-07-28T09:00:00Z")
    journal = (wiki / "databases/cdb1/journal/2026-07.md").read_text()
    assert journal.count("type: journal") == 1
    assert f"## {DAY} — " in journal and "## 2026-07-13 — quiet" in journal
    assert "databases/cdb1.md" not in res["pages_touched"]  # stub already there


def test_error_page_created_then_appended(wiki):
    apply(wiki, parsed())
    page = wiki / "errors" / "ORA-00600.md"
    text = page.read_text()
    assert "type: error-class" in text and f"updated: {NOW}" in text
    assert "## Occurrences" in text
    assert f"| {DAY} | cdb1 | 12 hits overnight | {REL_MD} |" in text

    day2 = json.loads(json.dumps(DIGEST))
    day2["window"]["day"] = "2026-07-13"
    (wiki / "digests" / "cdb1" / "2026-07-13.md").write_text("# d\n")
    apply_proposal(wiki, "cdb1", day2, "digests/cdb1/2026-07-13.md",
                   parsed(error_updates=[{"code": "ORA-00600",
                                          "note": "two more"}]),
                   "2026-07-28T09:00:00Z")
    text = page.read_text()
    assert text.count("## Occurrences") == 1
    assert text.count("| 2026-07-") == 2
    assert "updated: 2026-07-28T09:00:00Z" in text  # bumped, not duplicated
    assert text.count("updated:") == 1


def test_code_absent_from_the_digest_is_dropped_to_flags(wiki):
    res = apply(wiki, parsed(error_updates=[
        {"code": "ORA-00600", "note": "real"},
        {"code": "ORA-04031", "note": "invented"}]))
    assert (wiki / "errors" / "ORA-00600.md").exists()
    assert not (wiki / "errors" / "ORA-04031.md").exists()
    assert any("ORA-04031" in f and "dropped" in f for f in res["flags"])
    assert "errors/ORA-04031.md" not in res["pages_touched"]


def test_delta_only_code_is_still_a_digest_code(wiki):
    res = apply(wiki, parsed(error_updates=[{"code": "TNS-12543",
                                             "note": "first ever"}]))
    assert (wiki / "errors" / "TNS-12543.md").exists()
    assert res["flags"] == []


def test_incident_open_writes_the_page_and_links_the_index(wiki):
    res = apply(wiki, parsed(incident={
        "action": "open", "slug": "ora-600-storm",
        "title": "cdb1 ORA-00600 storm", "body": "Twelve internal errors.",
        "existing_page": None}))
    rel = f"incidents/{DAY}-cdb1-ora-600-storm.md"
    text = (wiki / rel).read_text()
    assert "type: incident" in text and "status: open" in text
    assert f"opened: {NOW}" in text and "db: cdb1" in text
    assert "# cdb1 ORA-00600 storm" in text
    assert f"- {DAY}: {REL_MD}" in text
    assert res["incidents_opened"] == [rel] and res["incidents_updated"] == []
    assert f"[[incidents/{DAY}-cdb1-ora-600-storm]]" in (wiki / "index.md").read_text()


def test_incident_update_appends_dated_evidence_and_never_closes(wiki_with_incident):
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    res = apply(wiki_with_incident, parsed(incident={
        "action": "update", "slug": None, "title": None,
        "body": "Still failing today.", "existing_page": rel}))
    text = (wiki_with_incident / rel).read_text()
    assert f"## Update {DAY}" in text and "Still failing today." in text
    assert f"evidence: {REL_MD}" in text
    assert read_incident(text, rel).status is Status.OPEN
    assert res["incidents_updated"] == [rel] and res["incidents_opened"] == []


#: The live compactor normalises the padding away before a digest is
#: written, so the code a proposal may name is the page's own spelling.
CODED = json.loads(json.dumps(DIGEST))
CODED["sources"]["alert"]["notable"][0]["codes"] = ["ORA-600"]
CODE_UPDATE = [{"code": "ORA-600", "note": "12 hits overnight"}]


def test_incident_open_links_the_error_page_the_same_proposal_created(wiki):
    apply(wiki, parsed(
        error_updates=CODE_UPDATE,
        incident={"action": "open", "slug": "ora-600-storm",
                  "title": "cdb1 ORA-600 storm", "existing_page": None,
                  "body": "Twelve ORA-600 errors, then ORA-600 again."}),
        digest=CODED)
    rel = f"incidents/{DAY}-cdb1-ora-600-storm.md"
    text = (wiki / rel).read_text()
    assert "# cdb1 ORA-600 storm" in text
    assert "Twelve [[errors/ORA-600]] errors, then ORA-600 again." in text
    assert read_incident(text, rel).error_codes == ("ORA-600",)


def test_incident_update_links_the_codes_its_block_names(wiki_with_incident):
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    apply(wiki_with_incident, parsed(
        error_updates=CODE_UPDATE,
        incident={"action": "update", "slug": None, "title": None,
                  "body": "ORA-600 again overnight.", "existing_page": rel}),
        digest=CODED)
    text = (wiki_with_incident / rel).read_text()
    assert "[[errors/ORA-600]] again overnight." in text
    assert f"evidence: {REL_MD}" in text
    assert read_incident(text, rel).error_codes == ("ORA-600",)


def test_incident_body_leaves_a_code_with_no_page_as_text(wiki):
    apply(wiki, parsed(
        error_updates=[],
        incident={"action": "open", "slug": "unknown-code", "title": "T",
                  "body": "ORA-7445 killed a process.", "existing_page": None}))
    text = (wiki / f"incidents/{DAY}-cdb1-unknown-code.md").read_text()
    assert "ORA-7445 killed a process." in text
    assert "[[errors/" not in text


def test_incident_update_of_a_missing_page_is_flagged_and_skipped(wiki):
    res = apply(wiki, parsed(incident={
        "action": "update", "slug": None, "title": None, "body": "b",
        "existing_page": "incidents/nope.md"}))
    assert not (wiki / "incidents").exists()
    assert res["incidents_updated"] == []
    assert any("nope.md" in f and "skipped" in f for f in res["flags"])


def test_incident_update_outside_incidents_is_refused(wiki):
    res = apply(wiki, parsed(incident={
        "action": "update", "slug": None, "title": None, "body": "b",
        "existing_page": "../../etc/passwd"}))
    assert any("skipped" in f for f in res["flags"])
    assert res["pages_touched"] == ["databases/cdb1.md",
                                    "databases/cdb1/journal/2026-07.md",
                                    "errors/ORA-00600.md", "index.md", "log.md"]


def test_open_incident_page_that_already_exists_gets_evidence_not_a_duplicate(wiki):
    inc = parsed(incident={"action": "open", "slug": "dupe", "title": "T",
                           "body": "First.", "existing_page": None})
    apply(wiki, inc)
    rel = f"incidents/{DAY}-cdb1-dupe.md"
    res = apply_proposal(wiki, "cdb1", DIGEST, REL_MD,
                         parsed(incident={"action": "open", "slug": "dupe",
                                          "title": "T", "body": "Second.",
                                          "existing_page": None}),
                         "2026-07-28T09:00:00Z")
    text = (wiki / rel).read_text()
    assert text.count("type: incident") == 1
    assert "Second." in text and res["incidents_updated"] == [rel]
    assert any("already exists" in f for f in res["flags"])


def test_a_second_open_the_same_day_lands_on_the_page_already_open(wiki):
    apply(wiki, parsed(incident={"action": "open", "slug": "ora-600-storm",
                                 "title": "T", "body": "First.",
                                 "existing_page": None}))
    rel = f"incidents/{DAY}-cdb1-ora-600-storm.md"
    res = apply(wiki, parsed(incident={"action": "open",
                                       "slug": "internal-errors-everywhere",
                                       "title": "T2", "body": "Second.",
                                       "existing_page": None}),
                now="2026-07-27T20:00:00Z")
    assert sorted(p.name for p in (wiki / "incidents").iterdir()) == [
        f"{DAY}-cdb1-ora-600-storm.md"]
    assert "Second." in (wiki / rel).read_text()
    assert res["incidents_updated"] == [rel] and res["incidents_opened"] == []
    assert any(f"incident for cdb1 on {DAY} already open: {rel}" in f
               for f in res["flags"])


def test_the_lexically_first_of_several_same_day_pages_wins(wiki):
    (wiki / "incidents").mkdir()
    for slug in ("b-later", "a-earlier"):
        (wiki / "incidents" / f"{DAY}-cdb1-{slug}.md").write_text(
            OPEN_INCIDENT.replace("2026-07-11T00:00:00Z", f"{DAY}T06:00:00Z"))
    res = apply(wiki, parsed(incident={"action": "open", "slug": "third-name",
                                       "title": "T", "body": "Third.",
                                       "existing_page": None}))
    assert res["incidents_updated"] == [f"incidents/{DAY}-cdb1-a-earlier.md"]
    assert res["incidents_opened"] == []


def test_a_resolved_incident_that_day_does_not_block_a_new_open(wiki):
    (wiki / "incidents").mkdir()
    (wiki / "incidents" / f"{DAY}-cdb1-old-news.md").write_text(
        OPEN_INCIDENT.replace("status: open", "status: resolved"))
    rel = f"incidents/{DAY}-cdb1-fresh-trouble.md"
    res = apply(wiki, parsed(incident={"action": "open",
                                       "slug": "fresh-trouble",
                                       "title": "T", "body": "New.",
                                       "existing_page": None}))
    assert res["incidents_opened"] == [rel] and res["incidents_updated"] == []
    assert (wiki / rel).exists()


def test_an_open_incident_on_another_day_does_not_block(wiki_with_incident):
    rel = f"incidents/{DAY}-cdb1-todays-trouble.md"
    res = apply(wiki_with_incident,
                parsed(incident={"action": "open", "slug": "todays-trouble",
                                 "title": "T", "body": "New.",
                                 "existing_page": None}))
    assert res["incidents_opened"] == [rel] and res["incidents_updated"] == []


def test_an_open_incident_on_another_db_does_not_block(wiki):
    (wiki / "incidents").mkdir()
    (wiki / "incidents" / f"{DAY}-emcdb-noisy.md").write_text(
        OPEN_INCIDENT.replace("db: cdb1", "db: emcdb"))
    rel = f"incidents/{DAY}-cdb1-todays-trouble.md"
    res = apply(wiki, parsed(incident={"action": "open",
                                       "slug": "todays-trouble", "title": "T",
                                       "body": "New.", "existing_page": None}))
    assert res["incidents_opened"] == [rel]


@pytest.mark.parametrize("slug", [
    "oracle-internal-errors",
    f"{DAY}-cdb1-oracle-internal-errors",
    f"cdb1-{DAY}-oracle-internal-errors",
    "cdb1-oracle-internal-errors",
])
def test_the_day_and_db_the_path_already_carries_are_cut_from_the_slug(wiki, slug):
    res = apply(wiki, parsed(incident={"action": "open", "slug": slug,
                                       "title": "T", "body": "B",
                                       "existing_page": None}))
    assert res["incidents_opened"] == [
        f"incidents/{DAY}-cdb1-oracle-internal-errors.md"]


def test_an_underscore_in_the_db_name_still_matches_the_slug_prefix(wiki):
    res = apply_proposal(wiki, "cdb1_stby", DIGEST, REL_MD,
                         parsed(incident={"action": "open",
                                          "slug": f"cdb1-stby-{DAY}-swapping",
                                          "title": "T", "body": "B",
                                          "existing_page": None}), NOW)
    assert res["incidents_opened"] == [f"incidents/{DAY}-cdb1_stby-swapping.md"]


def test_a_slug_that_is_nothing_but_the_day_and_db_survives(wiki):
    res = apply(wiki, parsed(incident={"action": "open", "slug": f"cdb1-{DAY}",
                                       "title": "T", "body": "B",
                                       "existing_page": None}))
    assert res["incidents_opened"] == [f"incidents/{DAY}-cdb1-{DAY}.md"]


def test_model_prose_cannot_smuggle_links_or_dead_digests(wiki):
    res = apply(wiki, parsed(
        journal_entry="See [[errors/ORA-99999]] and digests/cdb1/1999-01-01.md "
                      "for context.\n## sneaky heading",
        error_updates=[{"code": "ORA-00600", "note": "pipe | in a cell"}]))
    journal = (wiki / "databases/cdb1/journal/2026-07.md").read_text()
    assert "[[" not in journal and "## sneaky" not in journal
    assert "errors/ORA-99999" in journal  # flattened to plain text
    assert "1999-01-01" not in journal
    assert any("does not exist" in f for f in res["flags"])
    assert r"pipe \| in a cell" in (wiki / "errors" / "ORA-00600.md").read_text()
    assert blocking(lint_wiki(wiki)) == []


def test_everything_written_is_lint_clean(wiki_with_incident):
    apply(wiki_with_incident, parsed(
        error_updates=[{"code": "ORA-00600", "note": "n"},
                       {"code": "TNS-12543", "note": "m"}],
        incident={"action": "open", "slug": "ora-600-storm",
                  "title": "cdb1 ORA-00600 storm", "body": "Body.",
                  "existing_page": None},
        flags=["watch the standby"]))
    findings = lint_wiki(wiki_with_incident)
    assert blocking(findings) == []


def test_applying_the_same_proposal_twice_changes_nothing(wiki):
    first = apply(wiki, parsed())
    before = {p.name: p.read_text() for p in wiki.rglob("*.md")}
    second = apply(wiki, parsed())
    assert first["pages_touched"]
    assert second["pages_touched"] == []
    assert {p.name: p.read_text() for p in wiki.rglob("*.md")} == before


# ---- same-day supersede -------------------------------------------------------
# The digest is cumulative per day, so a same-day re-ingest replaces that
# day's writes instead of piling up reworded duplicates.

def test_same_day_reingest_replaces_the_journal_entry(wiki):
    apply(wiki, parsed(summary="first wording",
                       journal_entry="Morning view of the day."))
    apply(wiki, parsed(summary="second wording",
                       journal_entry="Full-day view, supersedes the morning."))
    journal = (wiki / "databases/cdb1/journal/2026-07.md").read_text()
    assert journal.count(f"## {DAY} — ") == 1
    assert "second wording" in journal and "first wording" not in journal
    assert "supersedes the morning" in journal
    # a different day still appends
    day2 = json.loads(json.dumps(DIGEST))
    day2["window"]["day"] = "2026-07-13"
    (wiki / "digests" / "cdb1" / "2026-07-13.md").write_text("# d\n")
    apply_proposal(wiki, "cdb1", day2, "digests/cdb1/2026-07-13.md",
                   parsed(summary="next day"), "2026-07-28T09:00:00Z")
    journal = (wiki / "databases/cdb1/journal/2026-07.md").read_text()
    assert f"## {DAY} — second wording" in journal
    assert "## 2026-07-13 — next day" in journal


def test_same_day_reingest_collapses_preexisting_duplicate_journal_entries(wiki):
    apply(wiki, parsed(summary="one"))
    j = wiki / "databases/cdb1/journal/2026-07.md"
    j.write_text(j.read_text()
                 + f"\n## {DAY} — two\n\nReworded.\n\nevidence: {REL_MD}\n"
                 + f"\n## {DAY} — three\n\nReworded again.\n\nevidence: {REL_MD}\n"
                 + "\n## 2026-07-13 — other day\n\nUntouched.\n\n"
                   "evidence: digests/cdb1/2026-07-11.md\n")
    apply(wiki, parsed(summary="final"))
    journal = j.read_text()
    assert journal.count(f"## {DAY} — ") == 1
    assert f"## {DAY} — final" in journal
    assert "— one" not in journal and "— two" not in journal
    assert "## 2026-07-13 — other day" in journal and "Untouched." in journal
    # the collapsed entry sits where the day's first entry stood
    assert journal.index(f"## {DAY} — final") < journal.index("## 2026-07-13")


def test_incident_update_same_day_is_replaced_not_appended(wiki_with_incident):
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    upd = {"action": "update", "slug": None, "title": None,
           "existing_page": rel}
    apply(wiki_with_incident, parsed(incident={**upd, "body": "Morning state."}))
    apply(wiki_with_incident, parsed(incident={**upd, "body": "Whole-day state."}))
    text = (wiki_with_incident / rel).read_text()
    assert text.count(f"## Update {DAY}") == 1
    assert "Whole-day state." in text and "Morning state." not in text
    # a different day appends a second section — the cross-day timeline lives
    day2 = json.loads(json.dumps(DIGEST))
    day2["window"]["day"] = "2026-07-13"
    (wiki_with_incident / "digests" / "cdb1" / "2026-07-13.md").write_text("# d\n")
    apply_proposal(wiki_with_incident, "cdb1", day2,
                   "digests/cdb1/2026-07-13.md",
                   parsed(incident={**upd, "body": "Next day."}),
                   "2026-07-28T09:00:00Z")
    text = (wiki_with_incident / rel).read_text()
    assert f"## Update {DAY}" in text and "## Update 2026-07-13" in text


def test_incident_update_collapses_preexisting_same_day_updates(wiki_with_incident):
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    page = wiki_with_incident / rel
    page.write_text(page.read_text() + "".join(
        f"\n## Update {DAY}\n\nRewording {n}.\n\nevidence: {REL_MD}\n"
        for n in range(3)))
    apply(wiki_with_incident, parsed(incident={
        "action": "update", "slug": None, "title": None,
        "body": "The one that stays.", "existing_page": rel}))
    text = page.read_text()
    assert text.count(f"## Update {DAY}") == 1
    assert "The one that stays." in text and "Rewording" not in text


def test_error_occurrence_row_same_day_same_db_is_replaced(wiki):
    apply(wiki, parsed(error_updates=[{"code": "ORA-00600",
                                       "note": "12 hits overnight"}]))
    apply_proposal(wiki, "cdb1", DIGEST, REL_MD,
                   parsed(error_updates=[{"code": "ORA-00600",
                                          "note": "54 hits by evening"}]),
                   "2026-07-27T22:00:00Z")
    # another database's same-day row on the shared page survives
    apply_proposal(wiki, "cdb2", DIGEST, REL_MD,
                   parsed(error_updates=[{"code": "ORA-00600",
                                          "note": "seen on cdb2 too"}]),
                   "2026-07-27T23:00:00Z")
    text = (wiki / "errors" / "ORA-00600.md").read_text()
    assert f"| {DAY} | cdb1 | 54 hits by evening | {REL_MD} |" in text
    assert "12 hits overnight" not in text
    assert f"| {DAY} | cdb2 | seen on cdb2 too | {REL_MD} |" in text
    assert text.count(f"| {DAY} | cdb1 | ") == 1
    assert text.count("updated:") == 1
    assert "updated: 2026-07-27T23:00:00Z" in text


def test_incident_open_duplicate_same_day_evidence_bullet_is_replaced(wiki):
    def opener(body):
        return parsed(incident={"action": "open", "slug": "dupe", "title": "T",
                                "body": body, "existing_page": None})
    apply(wiki, opener("First."))
    apply(wiki, opener("Second."))
    apply(wiki, opener("Third."))
    text = (wiki / f"incidents/{DAY}-cdb1-dupe.md").read_text()
    bullets = [ln for ln in text.splitlines() if ln.startswith(f"- {DAY}:")]
    assert bullets == [f"- {DAY}: {REL_MD} — Third."]
    assert "Second." not in text


def test_same_day_agentic_heading_is_left_alone(wiki):
    """The replace regex only matches this writer's own `## <day> — ` format;
    an agentic-mode section for the same day is never clobbered."""
    j = wiki / "databases" / "cdb1" / "journal"
    j.mkdir(parents=True)
    (j / "2026-07.md").write_text(
        "---\ntype: journal\ndb: cdb1\n---\n\n# cdb1 — journal 2026-07\n"
        f"\n## {DAY}\n\nRicher agentic prose for the same day.\n")
    apply(wiki, parsed(summary="structured view"))
    journal = (j / "2026-07.md").read_text()
    assert "Richer agentic prose for the same day." in journal
    assert f"## {DAY}\n" in journal                      # agentic heading intact
    assert f"## {DAY} — structured view" in journal      # appended beside it


# ---- orchestrator integration -------------------------------------------------

def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


@pytest.fixture
def repo(wiki):
    git(wiki, "init")
    git(wiki, "config", "user.email", "test@test")
    git(wiki, "config", "user.name", "test")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "init")
    return wiki


@pytest.fixture
def orch(repo, tmp_path):
    cfg = SimpleNamespace(
        wiki_repo=repo, state_dir=tmp_path / "state", report={}, research={},
        agents={"adapter": "pi", "mode": "structured",
                "pi": {"provider": "lmstudio", "cheap": "gemma",
                       "strong": "gemma"}, "timeout_seconds": 60})
    return Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def test_structured_ingest_commits_and_records_the_mode(orch, repo, monkeypatch):
    seen = calls(monkeypatch, json.dumps(raw()))
    result = orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert result["mode"] == "structured" and result["notable"] is True
    # the model got the structured prompt, not the AGENTS.md task prompt
    assert "Task: ingest." not in seen[0] and '"schema_version": 1' in seen[0]
    assert (repo / "errors" / "ORA-00600.md").exists()
    head = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                          capture_output=True, text=True, check=True).stdout
    assert head.startswith(f"ingest: cdb1 {DAY} — ")
    entry = orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]
    assert entry["status"] == "ingested" and entry["mode"] == "structured"
    assert entry["adapter"] == "pi" and entry["validation_ok"] is True
    assert entry["pages_touched"] == len(result["pages_touched"])


def test_two_bad_proposals_fail_the_ingest_and_roll_back(orch, repo, monkeypatch):
    seen = calls(monkeypatch, "no json", "still no json")
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert len(seen) == 2
    assert not (repo / "databases").exists()
    entry = orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]
    assert entry["status"] == "failed"
    assert entry["error_category"] == "harness_error"
    assert entry["mode"] == "structured" and entry["rolled_back"] is True


JOURNAL_MONTH = (
    "---\ntype: journal\ndb: cdb1\n---\n\n# cdb1 journal 2026-07\n\n"
    "## 2026-07-11 \u2014 cdb1 2026-07-11: standby transport stalled\n\n"
    "The standby fell behind.\n")


def commit_history(repo):
    (repo / "databases" / "cdb1" / "journal").mkdir(parents=True)
    (repo / "databases" / "cdb1" / "journal" / "2026-07.md").write_text(
        JOURNAL_MONTH)
    (repo / "incidents").mkdir(exist_ok=True)
    (repo / "incidents" / "2026-07-11-cdb1-standby-gap.md").write_text(
        OPEN_INCIDENT)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "history")


def agentic(orch, monkeypatch) -> list[str]:
    orch.cfg.agents["mode"] = "agentic"
    prompts: list[str] = []

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        prompts.append(prompt)
        (wiki / "log.md").write_text("# log\nagentic\n")
        return {"task": "ingest", "db": "cdb1", "notable": False,
                "summary": "s", "pages_touched": ["log.md"]}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    return prompts


def test_the_agentic_prompt_carries_the_history_and_the_context_rule(
        orch, repo, monkeypatch):
    commit_history(repo)
    prompts = agentic(orch, monkeypatch)
    orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert "Database history for cdb1 (last 90 days;" in prompts[0]
    assert "- 2026-07-11 \u2014 cdb1 2026-07-11: standby transport stalled" \
        in prompts[0]
    assert prompts[0].endswith(
        'The "Database history" block is context from earlier days: use it to '
        "judge whether today is new, recurring, or follows a recorded change, "
        "and never cite it as evidence for today.")


def test_the_agentic_prompt_is_unchanged_when_there_is_no_history(
        orch, repo, monkeypatch):
    prompts = agentic(orch, monkeypatch)
    orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert "Database history" not in prompts[0]
    assert prompts[0].endswith('with "task": "ingest" and "db": "cdb1".')


def test_history_days_zero_in_the_config_empties_the_structured_block(
        orch, repo, monkeypatch):
    commit_history(repo)
    orch.cfg.agents["history_days"] = 0
    seen = calls(monkeypatch, json.dumps(raw()))
    orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert "Database history for" not in seen[0]


def test_history_days_zero_in_the_config_empties_the_agentic_block(
        orch, repo, monkeypatch):
    commit_history(repo)
    orch.cfg.agents["history_days"] = 0
    prompts = agentic(orch, monkeypatch)
    orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert "Database history" not in prompts[0]


def test_agentic_mode_is_untouched_by_the_structured_switch(orch, repo, monkeypatch):
    orch.cfg.agents["mode"] = "agentic"
    prompts = []

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        prompts.append(prompt)
        (wiki / "log.md").write_text("# log\nagentic\n")
        return {"task": "ingest", "db": "cdb1", "notable": False,
                "summary": "s", "pages_touched": ["log.md"]}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    res = orch.ingest("cdb1", repo / "digests" / "cdb1" / f"{DAY}.json")
    assert "Task: ingest." in prompts[0] and "mode" not in res
    assert "mode" not in orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]


def test_an_ingest_update_leaves_the_human_action_records_untouched(wiki):
    """A live ingest shares the page with the incident lifecycle. The second
    apply is the dangerous one: it matches an existing `## Update` heading and
    so takes `day_block_replace`'s page-wide blank-line collapse."""
    rel = "incidents/2026-07-11-cdb1-standby-gap.md"
    first = ActionRecord(at="2026-07-11T09:00:00Z", kind="record-action",
                         actor="dba@example.com",
                         intent="restart managed recovery",
                         summary="apply process was down on cdb1sb",
                         status_after=Status.OPEN)
    second = ActionRecord(at="2026-07-11T15:30:00Z", kind="start-monitoring",
                          actor="dba@example.com",
                          intent="watch the gap close",
                          summary="apply lag back under a minute",
                          status_after=Status.MONITORING,
                          notes="Gap was 4200 blocks at 15:00.\n\n"
                                "Watching until tomorrow morning.")
    (wiki / "incidents").mkdir()
    (wiki / rel).write_text(
        append_action(append_action(OPEN_INCIDENT, first), second))

    upd = {"action": "update", "slug": None, "title": None,
           "existing_page": rel}
    apply(wiki, parsed(incident={**upd, "body": "Morning state."}))
    apply(wiki, parsed(incident={**upd, "body": "Whole-day state."}))

    text = (wiki / rel).read_text()
    assert render_action(first) in text and render_action(second) in text
    assert read_incident(text, rel).actions.records == (first, second)
    assert text.count(f"## Update {DAY}") == 1
    assert "Whole-day state." in text and f"evidence: {REL_MD}" in text
