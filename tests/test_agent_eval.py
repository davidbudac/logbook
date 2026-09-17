"""Adapter-free agent evaluation. Saved candidate outputs (wiki edits plus the
`.agent-result.json` an adapter would have written) are replayed into a
temporary git wiki through a fake `run_agent` — the pattern from
tests/test_orchestrate.py — scored deterministically, and driven through the
real orchestrator, so an invalid candidate is *proven* to roll back.

Score card (no LLM, no network, no narrative-quality judgment):
  result_schema    — the orchestrator's own result validation
  provenance_lint  — deterministic provenance v1 over the touched pages
  touched_paths    — declared `pages_touched` versus git status
  incident_dedup   — no second incident page for a db+day that already has one

`incident_dedup`, and the declared-but-unchanged half of `touched_paths`, are
not orchestrator rails yet; the evaluation adds them through a test-only
Orchestrator subclass so they take the same rollback path a real rail would.
The undeclared-change half is a rail (`_validate`), which is why a candidate
hiding a page now scores `result_schema` as well."""

import json
import re
import subprocess
from types import SimpleNamespace

import fixtures as fx
import pytest

from dbwiki.compactor import Compactor
from dbwiki.digest_md import render_md
from dbwiki import transaction
from dbwiki.gitutil import changed_paths
from dbwiki.lock import Held
from dbwiki.orchestrate import Orchestrator, ValidationError

DB = "cdb1"
DAY = "2026-07-13"
DIGEST_CASE = "silence_day"  # the fixture digest every candidate ingests
LEDGER_KEY = f"digests/{DB}/{DAY}.json"

# incidents/<day>-<db>-<slug>.md — the wiki's incident naming convention
INCIDENT_RE = re.compile(r"^incidents/(\d{4}-\d{2}-\d{2})-([a-z0-9_]+?)-")


def git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def head_message(repo) -> str:
    return git(repo, "log", "-1", "--format=%s").strip()


# ---- deterministic score card ------------------------------------------------

def _incident_key(rel: str):
    m = INCIDENT_RE.match(rel)
    return (m.group(1), m.group(2)) if m else None


def touched_path_problems(wiki, result: dict) -> list[str]:
    """`pages_touched` must be exactly what git says changed (digests and the
    result file excluded — the agent never owns those)."""
    declared = set(result.get("pages_touched", []))
    actual = set(transaction.stray_paths(wiki))
    return ([f"pages_touched declares a page it did not change: {r}"
             for r in sorted(declared - actual)]
            + [f"changed a page without declaring it: {r}"
               for r in sorted(actual - declared)])


def incident_dedup_problems(wiki) -> list[str]:
    """A day+database that already has an incident page must be updated, not
    given a second page."""
    tracked = [p for p in git(wiki, "ls-files").splitlines()
               if p.startswith("incidents/")]
    existing: dict[tuple, str] = {}
    for rel in sorted(tracked):
        key = _incident_key(rel)
        if key:
            existing.setdefault(key, rel)
    problems = []
    for rel in transaction.stray_paths(wiki):
        if rel in tracked or not rel.startswith("incidents/"):
            continue
        prior = existing.get(_incident_key(rel) or ())
        if prior:
            day, db = _incident_key(rel)
            problems.append(f"second incident page for {db} {day}: {rel} "
                            f"(update {prior} instead)")
    return problems


class EvalOrchestrator(Orchestrator):
    """Orchestrator plus the fixture-evaluation checks. Records the full score
    card on every validation and fails the stage on the categories that are
    contractual, so rollback is exercised by the real code path."""

    score: dict[str, list[str]] | None = None

    def _validate(self, result: dict, task: str, changed) -> list[str]:
        self.score = {
            "result_schema": super()._validate(result, task, changed),
            "provenance_lint": self._blocked(
                self._propose(transaction.head(self.wiki), task)),
            "touched_paths": touched_path_problems(self.wiki, result),
            "incident_dedup": incident_dedup_problems(self.wiki),
        }
        # provenance_lint is added by the stage itself (orchestrate.ingest)
        return [p for cat in ("result_schema", "touched_paths",
                              "incident_dedup") for p in self.score[cat]]


# ---- temporary wiki + candidate replay ---------------------------------------

@pytest.fixture
def wiki(tmp_path):
    repo = tmp_path / "wiki"
    for rel, text in fx.agent_wiki_files().items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(repo, "init")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "seed wiki")
    # the digest under evaluation is real compactor output for cdb1 2026-07-13
    digest = fx.golden_digest(DIGEST_CASE)
    d = repo / "digests" / DB
    d.mkdir(parents=True)
    (d / f"{DAY}.json").write_text(json.dumps(digest, indent=1))
    (d / f"{DAY}.md").write_text(render_md(digest))
    return repo


@pytest.fixture
def orch(wiki, tmp_path):
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          agents={}, report={}, research={})
    return EvalOrchestrator(cfg, lock=Held(tmp_path / "state", "eval", 0.0))


def replay(orch, wiki, case, monkeypatch) -> tuple[str, str]:
    """Apply the candidate's edits as the agent would have, then let the
    orchestrator validate/commit or roll back."""
    def _run(adapter, prompt, w, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:  # what a real adapter run reports back
            telemetry.update(adapter=adapter, model=model, duration_s=2.0,
                             exit_code=0, timed_out=False, stdout_bytes=17,
                             usage={"input_tokens": 900, "output_tokens": 120,
                                    "cost_usd": 0.004})
        for rel, text in case["edits"].items():
            p = w / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return case["result"]

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    try:
        orch.ingest(DB, wiki / "digests" / DB / f"{DAY}.json")
        return "commit", ""
    except ValidationError as e:
        return "rollback", str(e)


# ---- evaluation --------------------------------------------------------------

@pytest.mark.parametrize("name", fx.agent_case_names())
def test_candidate_scores_and_outcome_match_the_fixture(orch, wiki, monkeypatch,
                                                        name):
    case = fx.load_agent_case(name)
    expect = case["expect"]
    outcome, problems = replay(orch, wiki, case, monkeypatch)
    assert outcome == expect["outcome"], problems
    for fragment in expect["problems_contain"]:
        assert fragment in problems
    flagged = {cat for cat, ps in orch.score.items() if ps}
    assert flagged == set(expect["scored"]), orch.score


def test_score_card_covers_every_required_category(orch, wiki, monkeypatch):
    replay(orch, wiki, fx.load_agent_case("good_ingest"), monkeypatch)
    assert set(orch.score) == {"result_schema", "provenance_lint",
                               "touched_paths", "incident_dedup"}


def test_good_candidate_commits_and_records_the_ledger(orch, wiki, monkeypatch):
    case = fx.load_agent_case("good_ingest")
    outcome, _ = replay(orch, wiki, case, monkeypatch)
    assert outcome == "commit"
    assert head_message(wiki).startswith(f"ingest: {DB} {DAY}")
    assert changed_paths(wiki) == ()
    entry = orch.state.get_ledger()[LEDGER_KEY]
    assert entry["status"] == "ingested" and entry["notable"] is True
    assert entry["content_hash"] == \
        Compactor.content_hash(fx.golden_digest(DIGEST_CASE))
    rel = f"incidents/{DAY}-cdb1-telemetry-blackout.md"
    assert (wiki / rel).read_text() == case["edits"][rel]


@pytest.mark.parametrize("name", [n for n in fx.agent_case_names()
                                  if n.startswith("bad_")])
def test_bad_candidate_rolls_back_and_preserves_the_tree(orch, wiki, monkeypatch,
                                                         name):
    before = {rel: (wiki / rel).read_text() for rel in fx.agent_wiki_files()}
    outcome, _ = replay(orch, wiki, fx.load_agent_case(name), monkeypatch)
    assert outcome == "rollback"
    assert {rel: (wiki / rel).read_text() for rel in before} == before
    assert changed_paths(wiki) == ()
    assert (wiki / "digests" / DB / f"{DAY}.json").exists()  # digests survive
    assert head_message(wiki) == "digest: compactor output"
    assert orch.state.get_ledger()[LEDGER_KEY]["status"] == "failed"


def test_every_scored_category_has_a_failing_candidate():
    """The gate is only as good as its counter-examples: each category the
    score card reports must be exercised by at least one bad candidate."""
    scored = {c for name in fx.agent_case_names()
              for c in fx.load_agent_case(name)["expect"]["scored"]}
    assert scored == {"result_schema", "provenance_lint", "touched_paths",
                      "incident_dedup"}


# ---- scorer unit checks ------------------------------------------------------

def test_touched_path_scorer_is_quiet_on_an_exact_match(wiki):
    (wiki / "log.md").write_text("# log\nentry\n")
    assert touched_path_problems(wiki, {"pages_touched": ["log.md"]}) == []
    assert touched_path_problems(wiki, {"pages_touched": []}) == \
        ["changed a page without declaring it: log.md"]


def test_incident_dedup_scorer_allows_updating_the_existing_page(wiki):
    rel = f"incidents/{DAY}-cdb1-telemetry-blackout.md"
    (wiki / rel).write_text((wiki / rel).read_text() + "\nupdated in place\n")
    assert incident_dedup_problems(wiki) == []
    # ... and a new page for a *different* day is not a duplicate either
    (wiki / "incidents" / "2026-07-14-cdb1-standby-gap.md").write_text(
        "---\ntype: incident\n---\n\n# gap\n")
    assert incident_dedup_problems(wiki) == []
