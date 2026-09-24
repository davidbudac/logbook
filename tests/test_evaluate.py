"""`dbwiki eval`: the dataset built from the real digest fixtures, and the
scores one item earns — that a bad answer, a hallucinated error code and an
incident update with nothing open are all measured rather than raised, and
that the runner leaves no wiki behind."""

import json
from types import SimpleNamespace

import pytest
from fixtures import make_config

from dbwiki import evaluate, structured

GOOD = "standby_first_contact"


@pytest.fixture
def cfg():
    return make_config(agents={"pi": {"cheap": "c", "strong": "s"}})


@pytest.fixture
def items():
    return evaluate.dataset_items()


def item_by_id(items, item_id: str) -> dict:
    return next(i for i in items if i["id"] == item_id)


def raw(**over) -> dict:
    p = {"schema_version": 1,
         "summary": "quiet window",
         "notable": False,
         "journal_entry": "Nothing in this window needed an on-call DBA.",
         "error_updates": [],
         "incident": {"action": "none", "slug": None, "title": None,
                      "body": None, "existing_page": None},
         "flags": []}
    p.update(over)
    return p


def calls(monkeypatch, *answers):
    """The seam of test_structured.calls: the real propose/parse/retry chain
    runs, only the model's text is ours."""
    seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.25,
                             exit_code=0, timed_out=False,
                             usage={"input_tokens": 900, "output_tokens": 120,
                                    "cost_usd": "unknown"})
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(structured, "generate", _gen)
    return seen


# ---- dataset ------------------------------------------------------------------

def test_dataset_joins_every_fixture_to_its_golden_decision(items):
    assert len(items) == 7
    assert [i["id"] for i in items] == sorted(i["id"] for i in items)
    root = evaluate.fixtures_dir()
    for item in items:
        stem = item["id"]
        assert (root / "real_digests" / f"{stem}.json").exists()
        decision = json.loads(
            (root / "golden" / "real" / stem / "decision.json").read_text())
        assert item["expected"]["outcome"] == decision["outcome"]
        assert item["expected"]["model_tier"] == decision["model_tier"]
        digest = item["input"]["digest"]
        assert item["input"]["db"] == digest["db"]
        assert item["expected"]["codes"] == structured.digest_codes(digest)
        assert item["metadata"]["source"] == f"tests/fixtures/real_digests/{stem}.json"


# ---- one item -----------------------------------------------------------------

def test_a_good_proposal_scores_clean_and_leaves_no_wiki(monkeypatch, items, cfg):
    item = item_by_id(items, GOOD)
    codes = item["expected"]["codes"]
    calls(monkeypatch, json.dumps(raw(
        error_updates=[{"code": c, "note": "seen today"} for c in codes])))
    roots = []
    build = evaluate._throwaway_wiki

    def spy(root, db, digest):
        roots.append(build(root, db, digest))
        return roots[-1]

    monkeypatch.setattr(evaluate, "_throwaway_wiki", spy)

    proposal, verdict = evaluate.evaluate_item(item, cfg)

    assert proposal is not None
    assert verdict["error"] is None
    assert verdict["parsed_ok"] and verdict["apply_ok"]
    assert verdict["incident_consistent"]
    assert verdict["codes_allowlisted"] == 1.0
    assert verdict["duration_s"] == 0.25
    assert (verdict["input_tokens"], verdict["output_tokens"]) == (900, 120)
    assert roots and not roots[0].exists()


def test_a_code_the_digest_does_not_carry_costs_the_allowlist_score(
        monkeypatch, items, cfg):
    item = item_by_id(items, GOOD)
    codes = item["expected"]["codes"] + ["ZZZ-99999"]
    calls(monkeypatch, json.dumps(raw(
        error_updates=[{"code": c, "note": "seen today"} for c in codes])))

    _, verdict = evaluate.evaluate_item(item, cfg)

    assert 0.0 <= verdict["codes_allowlisted"] < 1.0
    assert verdict["parsed_ok"] and verdict["apply_ok"]


def test_two_bad_answers_are_a_verdict_not_an_exception(monkeypatch, items, cfg):
    calls(monkeypatch, "I cannot answer that.")

    proposal, verdict = evaluate.evaluate_item(item_by_id(items, GOOD), cfg)

    assert proposal is None
    assert verdict["parsed_ok"] is False and verdict["apply_ok"] is False
    assert "invalid structured proposal" in verdict["error"]
    assert verdict["duration_s"] == 0.25
    # never measured, so never averaged in as a perfect 0 flags / 0.0 codes
    assert verdict["flags"] is None
    assert verdict["codes_allowlisted"] is None
    assert verdict["incident_consistent"] is None


def test_updating_an_incident_that_is_not_open_is_inconsistent(
        monkeypatch, items, cfg):
    calls(monkeypatch, json.dumps(raw(incident={
        "action": "update", "slug": None, "title": None,
        "body": "More of the same tonight.",
        "existing_page": "incidents/2026-01-15-testcdb_s-standby-gap.md"})))

    _, verdict = evaluate.evaluate_item(item_by_id(items, GOOD), cfg)

    assert verdict["parsed_ok"] and verdict["apply_ok"]
    assert verdict["incident_consistent"] is False
    assert verdict["flags"] >= 1


# ---- run ----------------------------------------------------------------------

def test_run_evaluates_every_item_and_prints_a_table(monkeypatch, items, cfg,
                                                     capsys):
    calls(monkeypatch, json.dumps(raw()))

    results = evaluate.run(items, cfg, model="gemma-4", run_name="test-run")

    assert len(results) == 7
    assert [i["id"] for i, _, _ in results] == [i["id"] for i in items]
    assert all(v["parsed_ok"] and v["apply_ok"] for _, _, v in results)
    out = capsys.readouterr().out
    header = next(ln for ln in out.splitlines() if "parsed_ok" in ln)
    assert all(field in header for field, _ in evaluate.SCORES)
    for item in items:
        assert item["id"] in out
    summary = next(ln for ln in out.splitlines() if ln.strip().startswith("ALL"))
    assert "7/7" in summary


def test_the_run_config_pins_the_model_and_leaves_the_caller_alone(cfg):
    cfg.wiki_repo = "/some/wiki"
    pinned = evaluate._pinned(cfg, "gemma-4", "unsloth")

    assert pinned.agents["pi"] == {"cheap": "gemma-4", "strong": "gemma-4",
                                   "provider": "unsloth"}
    # structured.generate hands wiki_repo to the model call as its cwd; an
    # eval runs nowhere near the operator's wiki, and on a checkout without
    # one the subprocess dies before the model is ever asked
    assert pinned.wiki_repo is None
    assert cfg.agents["pi"] == {"cheap": "c", "strong": "s"}
    assert cfg.wiki_repo == "/some/wiki"


def verdict_row(ok: bool, flags: int) -> tuple:
    return (None, None, {
        "parsed_ok": ok, "apply_ok": ok,
        "codes_allowlisted": 1.0 if ok else None,
        "incident_consistent": True if ok else None,
        "flags": flags if ok else None, "duration_s": None,
        "input_tokens": None, "output_tokens": None,
        "error": None if ok else "E"})


def summary_cells(capsys) -> list[str]:
    line = next(ln for ln in capsys.readouterr().out.splitlines()
                if ln.strip().startswith("ALL"))
    return line.split()


def test_a_failing_model_does_not_average_better(capsys):
    """Model A parses half the items with 4 flags, model B all of them with 4:
    A used to average 2 flags against B's 4 because the failure counted as a
    clean zero."""
    evaluate._print_summary([verdict_row(True, 4), verdict_row(False, 0)])
    a = summary_cells(capsys)
    evaluate._print_summary([verdict_row(True, 4), verdict_row(True, 4)])
    b = summary_cells(capsys)
    flags = 1 + [f for f, _ in evaluate.SCORES].index("flags")
    assert a[flags] == b[flags] == "4"
    assert a[1] == "1/2" and b[1] == "2/2"


class DyingClient:
    """A Langfuse client whose experiment runs the first item, then dies."""

    def __init__(self, items):
        self.items = items

    def get_dataset(self, name):
        return SimpleNamespace(items=[SimpleNamespace(
            id=i["id"], input=i["input"], expected_output=i["expected"],
            metadata=i["metadata"]) for i in self.items[:-1]])

    def run_experiment(self, *, data, task, **kw):
        task(item=data[0])
        raise RuntimeError("connection reset")

    def flush(self):
        pass


def test_an_experiment_that_dies_midway_still_measures_every_item(
        monkeypatch, items, cfg, capsys):
    """The local fallback used to run only when the experiment ran nothing,
    so a server that dropped after one item left a summary over one item."""
    calls(monkeypatch, json.dumps(raw()))
    monkeypatch.setattr(evaluate, "_client", lambda lf: DyingClient(items))
    results = evaluate.run(items, cfg, model="gemma-4", run_name="t")
    assert sorted(i["id"] for i, _, _ in results) == \
        sorted(i["id"] for i in items)
    out = capsys.readouterr().out
    summary = next(ln for ln in out.splitlines() if ln.strip().startswith("ALL"))
    assert f"{len(items)}/{len(items)}" in summary


def test_the_consistency_score_strips_a_dot_slash_prefix_not_characters(
        monkeypatch, tmp_path):
    """`lstrip("./")` stripped characters, so `../incidents/x.md` scored as
    `incidents/x.md` while the writer (`structured._existing_incident`,
    `removeprefix`) refused it: the score and the writer disagreed."""
    page = "incidents/2026-01-15-x.md"
    monkeypatch.setattr(evaluate.structured, "all_open_incidents",
                        lambda wiki: [SimpleNamespace(path=page)])

    def consistent(existing):
        return evaluate._incident_consistent(
            {"incident": {"action": "update", "existing_page": existing}},
            tmp_path)

    assert consistent(page) and consistent("./" + page)
    assert not consistent("../" + page)


def test_eval_warnings_go_through_the_shared_warn_once(monkeypatch, capsys):
    """One warn-once per process (python-workarounds #18): eval's keys live
    in `observability`'s set, namespaced, and the text is unchanged."""
    from dbwiki import observability
    monkeypatch.setattr(observability, "_warned", set())
    evaluate._warn_once("sync", "first")
    evaluate._warn_once("sync", "second")
    assert capsys.readouterr().err == "warning: eval: first\n"
    assert observability._warned == {"eval:sync"}
