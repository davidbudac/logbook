"""Structured fleet reports: the prompt a routine window produces, strict
validation of the report JSON, the deterministic page writer, and the routing
rule — routine windows go structured, notable ones stay agentic."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from fixtures.incident_pages import incident_page

from dbwiki import structured
from dbwiki.harness import HarnessError
from dbwiki.lint import blocking, lint_wiki
from dbwiki.orchestrate import Orchestrator
from dbwiki.lock import Held
from dbwiki.structured import (MAX_MATERIAL_DIGEST, MAX_MATERIAL_INCIDENTS,
                               ProposalError, all_open_incidents,
                               apply_report, build_escalated_report_prompt,
                               build_report_prompt, parse_escalated_report_proposal,
                               parse_report_proposal, propose_report)

DAY = "2026-07-27"
WINDOW = (f"{DAY}T00:00:00Z", f"{DAY}T20:19:00Z")
NOW = f"{DAY}T20:22:03Z"
HEALTH = ["- sources: alert=ok(0.0h) listener=ok(0.0h)",
          "- recovery evidence: none — no recovery event in the window"]

INGESTED = [{"db": "cdb1", "notable": False, "summary": "quiet day on cdb1",
             "trigger": "routine: no notable groups"},
            {"db": "cdb2", "notable": False, "summary": "quiet day on cdb2"}]

OPEN_INCIDENT = incident_page("cdb1", "cdb1 dataguard transport failure",
                              opened="2026-07-12T00:00:00Z", body="Ongoing.")


@pytest.fixture
def wiki(tmp_path):
    """A wiki as it looks after two routine ingests: journals, digests, an
    index and a log — plus one incident nobody has closed."""
    root = tmp_path / "wiki"
    for db in ("cdb1", "cdb2"):
        (root / "digests" / db).mkdir(parents=True)
        (root / "digests" / db / f"{DAY}.md").write_text(f"# {db} digest\n")
        j = root / "databases" / db / "journal"
        j.mkdir(parents=True)
        (j / "2026-07.md").write_text(
            f"---\ntype: journal\ndb: {db}\n---\n\n# {db} — journal 2026-07\n")
    (root / "incidents").mkdir()
    (root / "incidents" / "2026-07-12-cdb1-dataguard.md").write_text(OPEN_INCIDENT)
    (root / "index.md").write_text(
        "---\ntype: index\n---\n\n# Logbook\n\n## Journals\n\n"
        "- [[databases/cdb1/journal/2026-07]] — cdb1 journal\n"
        "- [[databases/cdb2/journal/2026-07]] — cdb2 journal\n\n"
        "## Open incidents\n\n- [[incidents/2026-07-12-cdb1-dataguard]] — open\n")
    (root / "log.md").write_text("---\ntype: log\n---\n\n# Log\n")
    return root


def raw(**over) -> dict:
    p = {"schema_version": 1,
         "summary": "fleet quiet: cdb1 and cdb2 routine",
         "overview": "Both databases reported routine windows. Nothing in the "
                     "window changed the state of the open dataguard incident.",
         "items": [{"db": "cdb1", "status_line": "routine, no error groups"},
                   {"db": "cdb2", "status_line": "routine, listener only"}],
         "open_incident_notes": [{"incident": 1,
                                  "note": "no new evidence this window"}],
         "flags": []}
    p.update(over)
    return p


def parsed(**over) -> dict:
    """Proposals always reach apply_report through the validator."""
    return parse_report_proposal(json.dumps(raw(**over)))


# ---- prompt -------------------------------------------------------------------

def test_prompt_carries_items_health_and_numbered_incidents(wiki):
    p = build_report_prompt(DAY, WINDOW, INGESTED, wiki, health=HEALTH)
    assert f"{WINDOW[0]} -> {WINDOW[1]}" in p and f"day {DAY}" in p
    assert "- cdb1: routine — quiet day on cdb1 (trigger: routine" in p
    assert "- cdb2: routine — quiet day on cdb2" in p
    # collection health keeps its warning: telemetry is not database state
    assert "never report a collection gap as a database outage" in p
    assert "never treat absent events as recovery" in p
    assert HEALTH[0] in p
    # open incidents are numbered, and that number is how the model refers back
    assert "1. cdb1 dataguard transport failure — db cdb1 (status: open)" in p
    assert "by its number" in p
    # the response contract, spelled out
    assert '"schema_version": 1' in p and "NOTHING else" in p
    assert "do NOT name file paths" in p


def test_prompt_without_health_or_incidents(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    p = build_report_prompt(DAY, WINDOW, [], root)
    assert "Collection health" not in p
    assert "Open incidents (any database):\n- (none)" in p
    assert "none — leave the list empty" in p


def test_prompt_and_writer_number_incidents_the_same_way(wiki):
    (wiki / "incidents" / "2026-07-20-cdb2-listener.md").write_text(
        incident_page("cdb2", "cdb2 listener flapping",
                      opened="2026-07-12T00:00:00Z", body="Ongoing."))
    p = build_report_prompt(DAY, WINDOW, INGESTED, wiki)
    assert "1. cdb1 dataguard transport failure" in p
    assert "2. cdb2 listener flapping" in p
    assert [i.title for i in all_open_incidents(wiki)] == [
        "cdb1 dataguard transport failure", "cdb2 listener flapping"]


def test_resolved_incidents_are_not_listed(wiki):
    (wiki / "incidents" / "2026-07-12-cdb1-dataguard.md").write_text(
        incident_page("cdb1", "cdb1 dataguard transport failure",
                      status="resolved", opened="2026-07-12T00:00:00Z",
                      body="Ongoing."))
    assert all_open_incidents(wiki) == []
    assert "- (none)" in build_report_prompt(DAY, WINDOW, INGESTED, wiki)


# ---- parse_report_proposal ----------------------------------------------------

def test_parses_fenced_and_prose_wrapped_json():
    body = json.dumps(raw())
    for text in (body, f"```json\n{body}\n```",
                 f"Here you go:\n\n{body}\n\nHope that helps."):
        got = parse_report_proposal(text)
        assert got["summary"] == "fleet quiet: cdb1 and cdb2 routine"
        assert [i["db"] for i in got["items"]] == ["cdb1", "cdb2"]
        assert got["open_incident_notes"][0]["incident"] == 1


def test_missing_optional_blocks_default_to_empty():
    got = parse_report_proposal(json.dumps(
        {"schema_version": 1, "summary": "quiet", "overview": "Nothing here."}))
    assert got["items"] == [] and got["open_incident_notes"] == []
    assert got["flags"] == []


BAD = [
    ("no json here at all", "response"),
    ('{"schema_version": 1, "summary": "x"', "response"),
    (json.dumps(raw(schema_version=2)), "schema_version"),
    (json.dumps(raw(summary="")), "summary"),
    (json.dumps(raw(overview=7)), "overview"),
    (json.dumps(raw(items={"db": "cdb1"})), "items"),
    (json.dumps(raw(items=["cdb1"])), "items[0]"),
    (json.dumps(raw(items=[{"status_line": "s"}])), "items[0].db"),
    (json.dumps(raw(items=[{"db": "cdb1"}])), "items[0].status_line"),
    (json.dumps(raw(items=[{"db": "cdb1", "status_line": "x" * 301}])),
     "items[0].status_line"),
    (json.dumps(raw(open_incident_notes="none")), "open_incident_notes"),
    (json.dumps(raw(open_incident_notes=[{"note": "n"}])),
     "open_incident_notes[0].incident"),
    (json.dumps(raw(open_incident_notes=[{"incident": "1", "note": "n"}])),
     "open_incident_notes[0].incident"),
    (json.dumps(raw(open_incident_notes=[{"incident": True, "note": "n"}])),
     "open_incident_notes[0].incident"),
    (json.dumps(raw(open_incident_notes=[{"incident": 0, "note": "n"}])),
     "open_incident_notes[0].incident"),
    (json.dumps(raw(open_incident_notes=[{"incident": 1}])),
     "open_incident_notes[0].note"),
    (json.dumps(raw(flags=[1])), "flags[0]"),
]


def test_long_summary_is_cut_and_flagged():
    p = structured.parse_report_proposal(json.dumps(raw(summary="ab " * 100)))
    assert len(p["summary"]) <= structured.MAX_SUMMARY
    assert p["flags"] == [f"summary was 299 characters, "
                          f"cut to {len(p['summary'])}"]


@pytest.mark.parametrize("text,field", BAD, ids=[f"{i}-{f}" for i, (_, f)
                                                 in enumerate(BAD)])
def test_each_bad_field_is_rejected_by_name(text, field):
    with pytest.raises(ProposalError, match=field.replace("[", r"\[")
                       .replace("]", r"\]").replace(".", r"\.")):
        parse_report_proposal(text)


# ---- apply_report -------------------------------------------------------------

def apply(wiki, proposal, *, ingested=None, suffix="", health=None):
    return apply_report(wiki, DAY, WINDOW,
                        INGESTED if ingested is None else ingested,
                        proposal, NOW, suffix=suffix, health=health)


def test_routine_window_writes_the_report_index_and_log(wiki):
    res = apply(wiki, parsed(), suffix="-2019", health=HEALTH)
    assert res["task"] == "report" and res["day"] == DAY
    assert res["notable"] is False and res["mode"] == "structured"
    assert res["pages_touched"] == [f"reports/{DAY}-2019.md", "index.md",
                                    "log.md"]
    assert res["flags"] == []

    page = (wiki / f"reports/{DAY}-2019.md").read_text()
    assert page.startswith(f"---\ntype: report\nwindow_start: {WINDOW[0]}\n"
                           f"window_end: {WINDOW[1]}\ngenerated: {NOW}\n---\n")
    assert f"# Fleet report — {DAY} 00:00Z to 20:19Z" in page
    assert "Both databases reported routine windows." in page
    assert "## Summary\n\n| db | state | headline | evidence |\n|---|---|---|---|" in page
    assert ("| cdb1 | routine | routine, no error groups | "
            "[[databases/cdb1/journal/2026-07]], digests/cdb1/2026-07-27.md |"
            in page)
    assert "| cdb2 | routine | routine, listener only |" in page
    assert "## Collection health (telemetry, not database state)" in page
    assert HEALTH[0] in page
    assert ("## Open items\n\n- [[incidents/2026-07-12-cdb1-dataguard]] — "
            "cdb1 dataguard transport failure (cdb1, status: open) — "
            "no new evidence this window\n" in page)

    assert ("- [[reports/2026-07-27-2019]] — fleet report, 2026-07-27 "
            "00:00Z–20:19Z: fleet quiet") in (wiki / "index.md").read_text()
    assert (wiki / "log.md").read_text().endswith(
        f"[{NOW}] report — reports/{DAY}-2019.md: "
        f"fleet quiet: cdb1 and cdb2 routine\n")


def test_health_section_is_dropped_when_there_is_no_health_block(wiki):
    apply(wiki, parsed())
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "Collection health" not in page
    assert "## Summary" in page and "## Open items" in page


def test_invented_database_is_dropped_and_a_skipped_one_falls_back(wiki):
    res = apply(wiki, parsed(items=[
        {"db": "cdb1", "status_line": "routine"},
        {"db": "cdb9", "status_line": "invented"}]))
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "cdb9" not in page
    assert "| cdb2 | routine | quiet day on cdb2 |" in page
    assert any("cdb9" in f and "dropped" in f for f in res["flags"])
    assert any("no status line for cdb2" in f for f in res["flags"])


def test_duplicate_item_keeps_the_first_line(wiki):
    res = apply(wiki, parsed(items=[
        {"db": "cdb1", "status_line": "first"},
        {"db": "cdb1", "status_line": "second"},
        {"db": "cdb2", "status_line": "other"}]))
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "first" in page and "second" not in page
    assert any("duplicate report item for cdb1" in f for f in res["flags"])
    assert page.count("| cdb1 |") == 1


def test_note_for_an_unlisted_incident_is_dropped_to_flags(wiki):
    res = apply(wiki, parsed(open_incident_notes=[
        {"incident": 1, "note": "kept"}, {"incident": 4, "note": "invented"}]))
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "kept" in page and "invented" not in page
    assert any("open incident 4" in f for f in res["flags"])


def test_window_without_ingests_still_renders_a_table(wiki):
    res = apply(wiki, parsed(items=[]), ingested=[])
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "| (none) | — | no database was ingested in this window | — |" in page
    assert res["notable"] is False and res["pages_touched"]


def test_evidence_cites_only_pages_that_exist(wiki):
    res = apply(wiki, parsed(items=[{"db": "cdb3", "status_line": "new db"}]),
                ingested=[{"db": "cdb3", "notable": False, "summary": "new"}])
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "| cdb3 | routine | new db | (no page yet) |" in page
    assert res["flags"] == []


def test_notable_item_is_reported_as_notable(wiki):
    res = apply(wiki, parsed(items=[{"db": "cdb1", "status_line": "ORA-00600"}]),
                ingested=[{"db": "cdb1", "notable": True, "summary": "bad"}])
    assert res["notable"] is True
    assert "| cdb1 | NOTABLE | ORA-00600 |" in (wiki / f"reports/{DAY}.md").read_text()


def test_model_prose_cannot_smuggle_links_dead_digests_or_headings(wiki):
    res = apply(wiki, parsed(
        overview="## Injected heading\nSee [[incidents/made-up]] and "
                 "digests/cdb9/2026-01-01.md for detail.",
        items=[{"db": "cdb1", "status_line": "pipe | and [[errors/ORA-1]]"},
               {"db": "cdb2", "status_line": "fine"}]))
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "## Injected heading" not in page and "Injected heading" in page
    assert "[[incidents/made-up]]" not in page
    assert "digests/cdb9/2026-01-01.md" not in page and "(unknown digest)" in page
    assert r"pipe \| and errors/ORA-1" in page
    assert any("digests/cdb9" in f for f in res["flags"])


def test_a_resolvable_link_in_ordinary_prose_is_flattened(wiki):
    apply(wiki, parsed(overview="History in [[databases/cdb1/journal/2026-07]]."))
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "History in databases/cdb1/journal/2026-07." in page


def test_everything_written_is_lint_clean(wiki):
    apply(wiki, parsed(), suffix="-2019", health=HEALTH)
    assert blocking(lint_wiki(wiki)) == []


def test_a_second_report_the_same_day_gets_its_own_page_and_index_line(wiki):
    apply(wiki, parsed(), suffix="-1200")
    apply(wiki, parsed(summary="second look"), suffix="-2019")
    index = (wiki / "index.md").read_text()
    assert f"[[reports/{DAY}-1200]]" in index and f"[[reports/{DAY}-2019]]" in index
    assert (wiki / "log.md").read_text().count("] report — ") == 2
    assert blocking(lint_wiki(wiki)) == []


# ---- orchestrator routing -----------------------------------------------------

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
    # pinned adapter/mode: fixture_config would load the live config and could
    # reach a real model
    cfg = SimpleNamespace(
        wiki_repo=repo, state_dir=tmp_path / "state", report={}, research={},
        agents={"adapter": "claude", "mode": "structured",
                "claude": {"cheap": "sonnet", "strong": "opus"},
                "pi": {"provider": "lmstudio", "cheap": "gemma",
                       "strong": "gemma"}, "timeout_seconds": 60})
    return Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def fake_model(monkeypatch, *answers):
    seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.5,
                             exit_code=0, timed_out=False, usage="unknown")
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(structured, "generate", _gen)
    return seen


def fake_agent(monkeypatch, orch_module_result=None):
    prompts = []

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        prompts.append(prompt)
        (wiki / "reports").mkdir(exist_ok=True)
        (wiki / f"reports/{DAY}.md").write_text(
            "---\ntype: report\n---\n\n# agentic report\n")
        (wiki / "log.md").write_text("---\ntype: log\n---\n\n# Log\n\nagentic\n")
        return orch_module_result or {
            "task": "report", "notable": True, "summary": "agentic summary",
            "pages_touched": [f"reports/{DAY}.md", "log.md"]}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    return prompts


def test_routine_window_goes_structured_and_records_the_mode(orch, repo,
                                                             monkeypatch):
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch)
    res = orch.report(DAY, WINDOW, INGESTED, suffix="-2019", health=HEALTH)
    assert prompts == []                       # no agent ran
    assert res["mode"] == "structured" and res["task"] == "report"
    assert "Task: report." not in seen[0] and '"schema_version": 1' in seen[0]
    assert (repo / f"reports/{DAY}-2019.md").exists()
    head = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                          capture_output=True, text=True, check=True).stdout
    assert head.startswith(f"report: {DAY}-2019 — fleet quiet")
    assert orch.last_telemetry["mode"] == "structured"
    assert orch.last_telemetry["validation_ok"] is True
    assert orch.last_telemetry["model_tier"] == "cheap"


def test_notable_window_stays_agentic(orch, repo, monkeypatch):
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch)
    notable = [{"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"}]
    res = orch.report(DAY, WINDOW, notable)
    assert seen == []                          # the local model never ran
    assert "Task: report." in prompts[0] and "historical-context step" in prompts[0]
    assert res["summary"] == "agentic summary" and "mode" not in res
    assert "mode" not in orch.last_telemetry
    assert orch.last_telemetry["model_tier"] == "strong"


def test_agentic_mode_reports_agentically_even_when_routine(orch, repo,
                                                            monkeypatch):
    orch.cfg.agents["mode"] = "agentic"
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch, {
        "task": "report", "notable": False, "summary": "agentic routine",
        "pages_touched": [f"reports/{DAY}.md", "log.md"]})
    orch.report(DAY, WINDOW, INGESTED)
    assert seen == [] and "Task: report." in prompts[0]
    assert "mode" not in orch.last_telemetry


def test_two_bad_proposals_roll_the_report_back(orch, repo, monkeypatch):
    seen = fake_model(monkeypatch, "no json", "still no json")
    fake_agent(monkeypatch)
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        orch.report(DAY, WINDOW, INGESTED)
    assert len(seen) == 2
    assert not (repo / "reports").exists()


# ---- ADR-0001: analyst.enabled delegates an escalated window ------------------
#
# analyst.enabled forces the structured placeholder for a notable window
# regardless of agents.mode / agents.escalated_report (the on-prem node has
# no cloud-LLM credentials to run the agentic path with), and additionally
# enqueues the plain agentic prompt as a queue/pending/ request instead of
# ever invoking the agentic adapter locally.

NOTABLE = [{"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"}]


def test_analyst_enabled_writes_placeholder_and_enqueues_never_runs_agent(
        orch, repo, monkeypatch):
    orch.cfg.analyst = {"enabled": True}
    seen = fake_model(monkeypatch, json.dumps(escalated_raw()))
    prompts = fake_agent(monkeypatch)  # must never be called
    res = orch.report(DAY, WINDOW, NOTABLE)
    assert prompts == []
    assert res["task"] == "report" and res["mode"] == "structured"
    assert (repo / f"reports/{DAY}.md").exists()
    pending = list((repo / "queue" / "pending").glob("*.json"))
    assert len(pending) == 1
    request = json.loads(pending[0].read_text())
    assert request["kind"] == "report" and request["day"] == DAY
    assert request["suffix"] == "" and request["notable_dbs"] == ["cdb1"]
    assert "Task: report." in request["prompt"]
    assert "historical-context step" in request["prompt"]
    subjects = subprocess.run(["git", "-C", str(repo), "log", "-2", "--format=%s"],
                              capture_output=True, text=True, check=True
                              ).stdout.splitlines()
    # enqueue precedes the placeholder attempt (its rollback resets to the
    # enqueue commit), so the report commit sits on top of the queue commit
    assert subjects[0].startswith("report:")
    assert subjects[1].startswith("queue: enqueue report")


def test_analyst_enabled_leaves_a_routine_window_unaffected(orch, repo, monkeypatch):
    orch.cfg.analyst = {"enabled": True}
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch)
    orch.report(DAY, WINDOW, INGESTED, suffix="-2019", health=HEALTH)
    assert prompts == []
    assert not (repo / "queue").exists()


def test_analyst_disabled_is_unaffected_default(orch, repo, monkeypatch):
    """No `analyst` key at all (the fixture's cfg) behaves exactly like
    `analyst.enabled: false` — a notable window stays agentic."""
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch)
    orch.report(DAY, WINDOW, NOTABLE)
    assert seen == [] and prompts != []
    assert not (repo / "queue").exists()


def test_analyst_enqueue_survives_placeholder_failure(orch, repo, monkeypatch):
    """The request must outlive a failed placeholder: delegation exists
    precisely because the local model is fallible, so its failure cannot be
    allowed to also cancel the analyst's deep report (the rollback resets to
    the enqueue commit, not past it)."""
    orch.cfg.analyst = {"enabled": True}
    seen = fake_model(monkeypatch, "no json", "still no json")
    fake_agent(monkeypatch)
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        orch.report(DAY, WINDOW, NOTABLE)
    assert not (repo / "reports").exists()
    pending = list((repo / "queue" / "pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text())["day"] == DAY


# ---- escalated report: Material assembly ---------------------------------------
#
# agents.escalated_report: structured opts a notable window into the same
# one-shot-JSON contract as a routine window, but with a deterministically
# assembled "Material" section standing in for the wiki-reading step an agent
# would otherwise do. These tests cover build_escalated_report_prompt only —
# apply_report/orchestrator coverage follows below.

NOTABLE_DIGEST_MD = (
    "# Digest: cdb1 — " + DAY + "\n\n"
    "- window: `2026-07-27T00:00:00Z` -> `2026-07-27T20:19:00Z`\n\n"
    "## Deltas (never seen before / anomalies)\n\n"
    "- **first-ever error code** `TNS-12543` (listener, first seen "
    "2026-07-27T00:00:32Z)\n\n"
    "## alert — 500 events\n\n"
    "classes: error: 500\n\n"
    "### Notable\n\n"
    "- **[error] ora_error** ×500 (`ORA-00600`) — 2026-07-27T01:00:00Z\n"
    "  > ORA-00600: internal error, arguments: [x]\n\n"
    "### Routine (counters)\n\n"
    "- `ORA-nothing`: 10\n\n"
    "## listener — 50 events\n\n"
    "classes: info: 50\n\n"
    "### Routine (counters)\n\n"
    "- `conn`: 50\n"
)

ESCALATED_INGESTED = [
    {"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"},
    {"db": "cdb2", "notable": False, "summary": "quiet day on cdb2"},
]


@pytest.fixture
def escalated_wiki(wiki):
    """`wiki` (see the module fixture above) plus a real notable digest .md
    for cdb1 and the error-class page its notable group's code can cite."""
    (wiki / "digests" / "cdb1" / f"{DAY}.md").write_text(NOTABLE_DIGEST_MD)
    (wiki / "errors").mkdir()
    (wiki / "errors" / "ORA-00600.md").write_text(
        "---\ntype: error-class\n---\n\n# ORA-00600\n\n## Occurrences\n\n"
        "| day | db | note | evidence |\n|---|---|---|---|\n"
        "| 2026-06-01 | cdb1 | first seen | digests/cdb1/2026-06-01.md |\n")
    return wiki


def test_material_carries_only_deltas_and_notable_groups(escalated_wiki):
    p = build_escalated_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    assert "## Deltas (never seen before / anomalies)" in p
    assert "TNS-12543" in p
    assert "### Notable" in p and "ORA-00600" in p
    # routine counters and per-source event-count headers are noise, dropped
    assert "### Routine (counters)" not in p
    assert "ORA-nothing" not in p and "conn" not in p
    assert "## listener — 50 events" not in p
    assert "classes: error: 500" not in p


def test_material_cites_the_error_page_for_a_notable_groups_code(escalated_wiki):
    p = build_escalated_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    assert "Error history: [[errors/ORA-00600]]" in p
    assert "first seen" in p                     # the error page's own content


def test_material_prior_report_is_the_lexically_last_same_day(escalated_wiki):
    (escalated_wiki / "reports").mkdir()
    (escalated_wiki / f"reports/{DAY}-0600.md").write_text(
        "---\ntype: report\n---\n\n# earlier\n\nEARLIER-MARKER\n")
    (escalated_wiki / f"reports/{DAY}-1200.md").write_text(
        "---\ntype: report\n---\n\n# later\n\nLATER-MARKER\n")
    p = build_escalated_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    assert f"Prior report this day ([[reports/{DAY}-1200]])" in p
    assert "LATER-MARKER" in p and "EARLIER-MARKER" not in p


def test_material_incidents_capped_at_three_in_stable_path_order(escalated_wiki):
    assert MAX_MATERIAL_INCIDENTS == 3
    for slug, marker in [("b-second", "SECOND-BODY-MARKER"),
                         ("c-third", "THIRD-BODY-MARKER"),
                         ("d-fourth", "FOURTH-BODY-MARKER")]:
        (escalated_wiki / "incidents" / f"2026-07-13-cdb1-{slug}.md").write_text(
            f"---\ntype: incident\nstatus: open\ndb: cdb1\n---\n\n# {slug}\n\n"
            f"{marker}.\n")
    p = build_escalated_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    # 4 open incidents exist; the numbered list (from build_report_prompt) still
    # shows all 4, but the Material only carries full bodies for the first 3
    assert p.count("Open incident: [[incidents/") == 3
    assert "SECOND-BODY-MARKER" in p and "THIRD-BODY-MARKER" in p
    assert "FOURTH-BODY-MARKER" not in p
    assert "4. " in p                            # still numbered in the base list


def test_material_skips_a_notable_db_with_no_digest_file(tmp_path):
    root = tmp_path / "wiki"
    root.mkdir()
    ingested = [{"db": "cdb9", "notable": True, "summary": "s"}]
    p = build_escalated_report_prompt(DAY, WINDOW, ingested, root)
    assert "nothing to show" in p
    assert "Notable ingest" not in p


def test_material_skips_a_notable_digest_with_nothing_to_extract(tmp_path):
    root = tmp_path / "wiki"
    (root / "digests" / "cdb1").mkdir(parents=True)
    (root / f"digests/cdb1/{DAY}.md").write_text(
        "# Digest: cdb1 — " + DAY + "\n\n## alert — 3 events\n\nclasses: info: 3\n")
    ingested = [{"db": "cdb1", "notable": True, "summary": "s"}]
    p = build_escalated_report_prompt(DAY, WINDOW, ingested, root)
    assert "Notable ingest" not in p
    assert "nothing to show" in p


def test_material_digest_excerpt_is_capped(tmp_path):
    root = tmp_path / "wiki"
    (root / "digests" / "cdb1").mkdir(parents=True)
    text = "## Deltas (never seen before / anomalies)\n\n" + ("- filler line\n" * 2000)
    assert len(text) > MAX_MATERIAL_DIGEST
    (root / f"digests/cdb1/{DAY}.md").write_text(text)
    ingested = [{"db": "cdb1", "notable": True, "summary": "s"}]
    p = build_escalated_report_prompt(DAY, WINDOW, ingested, root)
    assert "[truncated]" in p


def test_material_prior_report_is_capped(tmp_path):
    root = tmp_path / "wiki"
    (root / "reports").mkdir(parents=True)
    (root / f"reports/{DAY}-0100.md").write_text(
        "---\ntype: report\n---\n\n" + ("y" * 5000))
    p = build_escalated_report_prompt(DAY, WINDOW, [], root)
    assert "[truncated]" in p


def test_material_incident_body_is_capped(tmp_path):
    root = tmp_path / "wiki"
    (root / "incidents").mkdir(parents=True)
    (root / "incidents" / "2026-07-01-cdb1-big.md").write_text(
        "---\ntype: incident\nstatus: open\ndb: cdb1\n---\n\n# big\n\n" + ("x" * 5000))
    p = build_escalated_report_prompt(DAY, WINDOW, [], root)
    assert "[truncated]" in p


def test_material_error_pages_capped_at_five_and_chars(tmp_path):
    root = tmp_path / "wiki"
    (root / "digests" / "cdb1").mkdir(parents=True)
    codes = [f"ORA-{n:05d}" for n in range(1, 7)]           # 6 distinct codes
    codes_str = ", ".join(f"`{c}`" for c in codes)
    (root / f"digests/cdb1/{DAY}.md").write_text(
        f"### Notable\n\n- **[error] x** ×1 ({codes_str})\n")
    (root / "errors").mkdir()
    for c in codes:
        (root / f"errors/{c}.md").write_text(
            f"---\ntype: error-class\n---\n\n# {c}\n\n" + "z" * 3000)
    ingested = [{"db": "cdb1", "notable": True, "summary": "s"}]
    p = build_escalated_report_prompt(DAY, WINDOW, ingested, root)
    assert p.count("Error history: [[errors/ORA-") == 5   # capped, not 6
    assert "[truncated]" in p                              # each page > 2000 chars


def test_escalated_prompt_replaces_the_routine_contract(escalated_wiki):
    routine = build_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    assert structured._REPORT_CONTRACT in routine
    p = build_escalated_report_prompt(DAY, WINDOW, ESCALATED_INGESTED, escalated_wiki)
    assert structured._ESCALATED_REPORT_CONTRACT in p
    assert structured._REPORT_CONTRACT not in p


# ---- escalated report: parse ---------------------------------------------------

def escalated_raw(**over) -> dict:
    p = {"notable_analysis": [
        {"db": "cdb1", "analysis": "This matches the ORA-00600 group seen in "
                                   "June per errors/ORA-00600.md."}]}
    p.update(over)
    return raw(**p)


def test_parse_escalated_accepts_notable_analysis_and_the_routine_fields():
    got = parse_escalated_report_proposal(json.dumps(escalated_raw()), {"cdb1"})
    assert got["notable_analysis"] == [
        {"db": "cdb1", "analysis": "This matches the ORA-00600 group seen in "
                                   "June per errors/ORA-00600.md."}]
    assert got["items"][0]["db"] == "cdb1"       # base contract untouched


def test_parse_escalated_missing_analysis_is_rejected():
    # omitting the field entirely is the thin-report failure mode: an
    # escalated proposal must analyse every notable db or cost the retry
    with pytest.raises(ProposalError, match="missing entry for notable"):
        parse_escalated_report_proposal(json.dumps(raw()), {"cdb1"})


def test_parse_escalated_no_notable_dbs_accepts_absent_field():
    got = parse_escalated_report_proposal(json.dumps(raw()), set())
    assert got["notable_analysis"] == []


def test_parse_escalated_rejects_an_unknown_db():
    with pytest.raises(ProposalError, match=r"notable_analysis\[0\]\.db"):
        parse_escalated_report_proposal(json.dumps(escalated_raw(
            notable_analysis=[{"db": "cdb9", "analysis": "x"}])), {"cdb1"})


def test_parse_escalated_rejects_a_duplicate_db():
    with pytest.raises(ProposalError, match=r"notable_analysis\[1\]\.db"):
        parse_escalated_report_proposal(json.dumps(escalated_raw(
            notable_analysis=[{"db": "cdb1", "analysis": "a"},
                              {"db": "cdb1", "analysis": "b"}])), {"cdb1"})


def test_parse_escalated_rejects_a_malformed_entry():
    with pytest.raises(ProposalError, match=r"notable_analysis\[0\]"):
        parse_escalated_report_proposal(json.dumps(escalated_raw(
            notable_analysis=["cdb1"])), {"cdb1"})


def test_parse_report_proposal_never_carries_notable_analysis():
    got = parse_report_proposal(json.dumps(raw()))
    assert "notable_analysis" not in got


def test_propose_report_escalated_contract_only_when_notable_dbs_given(monkeypatch):
    cfg = SimpleNamespace(agents={"pi": {"cheap": "gemma", "strong": "gemma"}})
    escalate_seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        escalate_seen.append(escalate)
        return json.dumps(escalated_raw())

    monkeypatch.setattr(structured, "generate", _gen)
    got = propose_report("prompt", cfg, escalate=True, notable_dbs={"cdb1"})
    assert got["notable_analysis"][0]["db"] == "cdb1"
    assert escalate_seen == [True]

    def _gen_routine(prompt, cfg, *, escalate=False, telemetry=None):
        return json.dumps(raw())

    monkeypatch.setattr(structured, "generate", _gen_routine)
    got2 = propose_report("prompt", cfg)
    assert "notable_analysis" not in got2


# ---- escalated report: writer ---------------------------------------------------

def test_apply_report_renders_notable_items_before_the_summary_table(wiki):
    proposal = parse_escalated_report_proposal(json.dumps(escalated_raw(
        notable_analysis=[{"db": "cdb1",
                          "analysis": "Prior occurrence noted in "
                                      "[[incidents/2026-07-12-cdb1-dataguard]] "
                                      "and an invented [[incidents/made-up]]."}])),
        {"cdb1"})
    res = apply(wiki, proposal, ingested=[
        {"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"},
        {"db": "cdb2", "notable": False, "summary": "quiet day on cdb2"}])
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "## Notable items" in page and "### cdb1" in page
    assert page.index("## Notable items") < page.index("## Summary")
    assert "### cdb2" not in page                # only the notable db gets a section
    # a resolvable wikilink is kept live; an unresolvable one is flattened
    assert "[[incidents/2026-07-12-cdb1-dataguard]]" in page
    assert "[[incidents/made-up]]" not in page
    assert "incidents/made-up" in page           # flattened to plain text
    assert res["mode"] == "structured"


def test_proposal_without_notable_analysis_omits_the_section(wiki):
    apply(wiki, parsed())
    page = (wiki / f"reports/{DAY}.md").read_text()
    assert "## Notable items" not in page


def test_notable_items_output_is_lint_clean(wiki):
    proposal = parse_escalated_report_proposal(json.dumps(escalated_raw()), {"cdb1"})
    apply(wiki, proposal, ingested=[
        {"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"}])
    assert blocking(lint_wiki(wiki)) == []


# ---- escalated report: orchestrator routing -------------------------------------

NOTABLE_WINDOW = [{"db": "cdb1", "notable": True, "summary": "ORA-00600 storm"}]


def test_escalated_window_goes_structured_when_opted_in(orch, repo, monkeypatch):
    orch.cfg.agents["escalated_report"] = "structured"
    seen = fake_model(monkeypatch, json.dumps(escalated_raw()))
    prompts = fake_agent(monkeypatch)
    res = orch.report(DAY, WINDOW, NOTABLE_WINDOW)
    assert prompts == []                         # the agentic adapter never ran
    assert res["mode"] == "structured" and res["notable"] is True
    assert '"notable_analysis"' in seen[0]        # the escalated contract, not routine
    assert orch.last_telemetry["mode"] == "structured"
    assert orch.last_telemetry["model_tier"] == "strong"
    assert orch.last_telemetry["validation_ok"] is True


def test_escalated_window_stays_agentic_when_escalated_report_is_agentic(orch, repo,
                                                                         monkeypatch):
    orch.cfg.agents["escalated_report"] = "agentic"
    seen = fake_model(monkeypatch, json.dumps(raw()))
    prompts = fake_agent(monkeypatch)
    orch.report(DAY, WINDOW, NOTABLE_WINDOW)
    assert seen == [] and "Task: report." in prompts[0]
    assert "mode" not in orch.last_telemetry


def test_escalated_window_unknown_db_rolls_back(orch, repo, monkeypatch):
    orch.cfg.agents["escalated_report"] = "structured"
    seen = fake_model(monkeypatch, json.dumps(escalated_raw(
        notable_analysis=[{"db": "cdb9", "analysis": "x"}])))
    fake_agent(monkeypatch)
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        orch.report(DAY, WINDOW, NOTABLE_WINDOW)
    assert len(seen) == 2                        # retried once, then rolled back
    assert not (repo / "reports").exists()
    assert orch.last_telemetry["rolled_back"] is True
    assert orch.last_telemetry["mode"] == "structured"
