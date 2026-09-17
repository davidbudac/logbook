"""Orchestrator safety rails: result validation, the clean-tree guard, and
rollback behavior — the parts that run unattended and own git."""

import json
import re
import subprocess
from types import SimpleNamespace

import pytest

from dbwiki import advisory, transaction
from dbwiki.gitutil import changed_paths
from dbwiki.harness import HarnessError, InvalidResultError, NoResultError
from dbwiki.incidents import (ActionRecord, ErrorAbsent, MonitoringWindow,
                              Status, read_incident, render_action)
from dbwiki.lifecycle import RESOLUTION_HEAD
from dbwiki.lock import Held
from dbwiki.orchestrate import Orchestrator, ValidationError
from dbwiki.pagetext import day_block_replace, sections


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args],
                   check=True, capture_output=True)


@pytest.fixture
def wiki(tmp_path):
    repo = tmp_path / "wiki"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "log.md").write_text("# log\n")
    (repo / "changelog.md").write_text("# changelog\n")
    (repo / "digests").mkdir()
    (repo / "digests" / ".keep").write_text("")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def orch(wiki, tmp_path):
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          agents={}, report={}, research={})
    return Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def result(**over):
    base = {"task": "ingest", "summary": "s", "notable": False,
            "pages_touched": ["log.md"]}
    base.update(over)
    return base


# ---- _validate -------------------------------------------------------------

def changed(wiki) -> tuple[str, ...]:
    """What the stage would publish: the transaction's path set."""
    return transaction.stray_paths(wiki)


def test_validate_requires_exact_log_md_path(wiki, orch):
    # a file merely *containing* "log.md" must not satisfy the check
    (wiki / "changelog.md").write_text("# changelog\nedited\n")
    problems = orch._validate(result(pages_touched=["changelog.md"]), "ingest",
                              changed(wiki))
    assert any("log.md not updated" in p for p in problems)

    (wiki / "log.md").write_text("# log\nentry\n")
    problems = orch._validate(result(pages_touched=["log.md", "changelog.md"]),
                              "ingest", changed(wiki))
    assert problems == []


def test_validate_flags_missing_pages_and_digest_edits(wiki, orch):
    (wiki / "log.md").write_text("# log\nentry\n")
    problems = orch._validate(
        result(pages_touched=["log.md", "nope.md", "digests/cdb1/x.json"]),
        "ingest", changed(wiki))
    assert any("does not exist" in p for p in problems)
    assert any("touched a digest" in p for p in problems)

    (wiki / "digests" / "stray.json").write_text("{}")
    problems = orch._validate(result(), "ingest", changed(wiki))
    assert any("digests/ modified by agent" in p for p in problems)


def test_validate_flags_no_changes_and_wrong_task(wiki, orch):
    problems = orch._validate(result(task="report"), "ingest", changed(wiki))
    assert any("result.task" in p for p in problems)
    assert any("no changes at all" in p for p in problems)


# ---- clean-tree guard + rollback --------------------------------------------

def test_machine_output_is_not_a_stray_edit(wiki, orch):
    assert changed(wiki) == ()
    (wiki / "digests" / "cdb1").mkdir()
    (wiki / "digests" / "cdb1" / "2026-07-10.json").write_text("{}")
    (wiki / ".agent-result.json").write_text("{}")
    assert changed(wiki) == ()


def test_a_stage_refuses_to_run_over_a_stray_edit(wiki, orch, monkeypatch):
    (wiki / "notes.md").write_text("human work in progress\n")
    assert changed(wiki) == ("notes.md",)
    fake_agent(monkeypatch, {}, {"task": "lint", "summary": "s",
                                 "pages_touched": []})
    with pytest.raises(RuntimeError, match="notes.md"):
        orch.lint()
    assert (wiki / "notes.md").read_text() == "human work in progress\n"


def test_restore_reverts_edits_but_preserves_digests(wiki, orch):
    base = transaction.head(wiki)
    (wiki / "log.md").write_text("# log\nagent garbage\n")
    (wiki / "half-page.md").write_text("partial agent output\n")
    (wiki / "digests" / "new.json").write_text("{}")
    transaction.restore(wiki, transaction.stray_paths(wiki), base)
    assert (wiki / "log.md").read_text() == "# log\n"
    assert not (wiki / "half-page.md").exists()
    assert (wiki / "digests" / "new.json").exists()


# ---- result synthesis --------------------------------------------------------

def test_synthesize_result_from_git_status(wiki, orch):
    (wiki / "log.md").write_text("# log\nentry\n")
    (wiki / "incidents").mkdir()
    (wiki / "incidents" / "2026-07-10-cdb1.md").write_text("# incident\n")
    (wiki / "digests" / "d.json").write_text("{}")  # must be excluded
    res = orch._synthesize_result("ingest", "cdb1", True,
                                  NoResultError("no result file"))
    assert res["task"] == "ingest" and res["notable"] is True
    assert "log.md" in res["pages_touched"]
    assert not any(p.startswith("digests/") for p in res["pages_touched"])
    assert res["incidents_updated"] == ["incidents/2026-07-10-cdb1.md"]
    assert res["flags"]


def test_synthesize_result_returns_none_without_edits(wiki, orch):
    assert orch._synthesize_result(
        "ingest", "cdb1", False, NoResultError("x")) is None


# ---- porcelain parsing --------------------------------------------------------

def test_changed_paths_reports_both_ends_of_a_rename(wiki):
    git(wiki, "mv", "changelog.md", "renamed.md")
    (wiki / "new file.md").write_text("x\n")
    paths = changed_paths(wiki)
    assert "renamed.md" in paths
    assert "changelog.md" in paths
    assert "new file.md" in paths


# ---- research stage -----------------------------------------------------------

ERROR_PAGE = ("---\ntype: error-class\nupdated: 2026-07-10T00:00:00Z\n---\n\n"
              "# ORA-12543\n\n## Occurrences\n\n| t | db |\n")
SOURCE_PAGE = ("---\ntype: source\nstatus: approved\ntier: official\n"
               "domains: [docs.oracle.com]\nfetchable: true\nadded: 2026-01-01\n"
               "last_reviewed: 2026-01-01\nreview_after_days: 180\n---\n\n"
               "# Oracle docs\n")


@pytest.fixture
def research_wiki(wiki):
    (wiki / "errors").mkdir()
    (wiki / "errors" / "ORA-12543.md").write_text(ERROR_PAGE)
    (wiki / "sources").mkdir()
    (wiki / "sources" / "oracle-docs.md").write_text(SOURCE_PAGE)
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "seed research pages")
    return wiki


def fake_agent(monkeypatch, edits: dict, result: dict, usage=None):
    """Stand in for run_agent: apply the agent's edits, fill the telemetry
    out-param the way a real adapter run would, return its result."""
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=1.5,
                             exit_code=0, timed_out=False, stdout_bytes=42,
                             usage=usage if usage is not None else "unknown")
        for rel, text in edits.items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return result
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)


def researched(body: str) -> str:
    return ERROR_PAGE + f"\n## Reference\n\n{body}\n"


def head_message(repo) -> str:
    return subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%s"],
                          capture_output=True, text=True, check=True).stdout


def head_sha(repo) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def subjects_since(repo, sha) -> list[str]:
    """Subjects of the commits a stage added, oldest first."""
    out = subprocess.run(["git", "-C", str(repo), "log", f"{sha}..HEAD",
                          "--reverse", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def revs_since(repo, sha) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "rev-list", "--reverse",
                          f"{sha}..HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def file_at(repo, ref, rel) -> str:
    return subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{rel}"],
                          capture_output=True, text=True, check=True).stdout


def commit_body(repo, ref) -> str:
    return subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B",
                           ref],
                          capture_output=True, text=True, check=True).stdout


def commit_paths(repo, ref) -> set[str]:
    out = subprocess.run(["git", "-C", str(repo), "show", "--name-only",
                          "--format=", ref],
                         capture_output=True, text=True, check=True).stdout
    return {line for line in out.splitlines() if line}


def porcelain(repo) -> str:
    return subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout


def test_research_commits_when_citations_are_approved(research_wiki, orch, monkeypatch):
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched(
            "Host unreachable per the error reference. (source: sources/oracle-docs; "
            "url: https://docs.oracle.com/en/ORA-12543; accessed: 2026-07-25)"),
        "log.md": "# log\n[2026-07-25T09:00Z] research — 1 page\n",
    }, {"task": "research", "summary": "looked up ORA-12543",
        "pages_touched": ["errors/ORA-12543.md"], "flags": []})
    result = orch.research(["errors/ORA-12543.md"], [])
    assert result["summary"] == "looked up ORA-12543"
    assert head_message(research_wiki).startswith("research — ")
    assert changed_paths(research_wiki) == ()
    assert "## Reference" in (research_wiki / "errors" / "ORA-12543.md").read_text()


def test_research_rolls_back_unapproved_domain(research_wiki, orch, monkeypatch):
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched(
            "Cause. (source: sources/oracle-docs; "
            "url: https://blogs.oracle.com/hot-take; accessed: 2026-07-25)"),
    }, {"task": "research", "summary": "s",
        "pages_touched": ["errors/ORA-12543.md"], "flags": []})
    with pytest.raises(ValidationError, match="blogs.oracle.com"):
        orch.research(["errors/ORA-12543.md"], [])
    assert (research_wiki / "errors" / "ORA-12543.md").read_text() == ERROR_PAGE


def test_research_rolls_back_path_outside_allowlist(research_wiki, orch, monkeypatch):
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched("Meaning only, no URL."),
        "incidents/2026-07-25-cdb1-x.md": "---\ntype: incident\n---\n\n# nope\n",
    }, {"task": "research", "summary": "s",
        "pages_touched": ["errors/ORA-12543.md",
                          "incidents/2026-07-25-cdb1-x.md"], "flags": []})
    with pytest.raises(ValidationError, match="outside errors/"):
        orch.research(["errors/ORA-12543.md"], [])
    assert not (research_wiki / "incidents").exists()
    assert (research_wiki / "errors" / "ORA-12543.md").read_text() == ERROR_PAGE


def test_research_rolls_back_new_source_page(research_wiki, orch, monkeypatch):
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched(
            "Cause. (source: sources/asktom; url: https://asktom.oracle.com/x; "
            "accessed: 2026-07-25)"),
        "sources/asktom.md": SOURCE_PAGE.replace("docs.oracle.com",
                                                 "asktom.oracle.com"),
    }, {"task": "research", "summary": "s",
        "pages_touched": ["errors/ORA-12543.md", "sources/asktom.md"], "flags": []})
    with pytest.raises(ValidationError, match="created a new source page"):
        orch.research(["errors/ORA-12543.md"], [])
    assert not (research_wiki / "sources" / "asktom.md").exists()


def test_research_may_update_an_existing_source_page(research_wiki, orch, monkeypatch):
    reviewed = SOURCE_PAGE.replace("last_reviewed: 2026-01-01",
                                   "last_reviewed: 2026-07-25")
    fake_agent(monkeypatch, {"sources/oracle-docs.md": reviewed}, {
        "task": "research", "summary": "reviewed oracle-docs",
        "pages_touched": ["sources/oracle-docs.md"], "flags": []})
    orch.research([], ["sources/oracle-docs.md"])
    assert "last_reviewed: 2026-07-25" in \
        (research_wiki / "sources" / "oracle-docs.md").read_text()


def test_research_dry_run_touches_nothing(research_wiki, orch, capsys):
    assert orch.research(["errors/ORA-12543.md"], [], dry_run=True) == {}
    assert "Task: research." in capsys.readouterr().out
    assert changed_paths(research_wiki) == ()


def test_agent_edit_failing_deterministic_lint_rolls_back(research_wiki, orch,
                                                          monkeypatch):
    """A provenance-v1 error finding on a touched page is a validation problem:
    the stage rolls back before committing (docs/provenance.md)."""
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched("See [[errors/ORA-99999]] for context."),
    }, {"task": "research", "summary": "s",
        "pages_touched": ["errors/ORA-12543.md"], "flags": []})
    with pytest.raises(ValidationError, match="wikilink-broken"):
        orch.research(["errors/ORA-12543.md"], [])
    assert (research_wiki / "errors" / "ORA-12543.md").read_text() == ERROR_PAGE


def test_lint_exceptions_let_a_grandfathered_page_through(research_wiki, orch,
                                                          monkeypatch):
    (research_wiki / ".lint-exceptions").write_text(
        "errors/ORA-12543.md\twikilink-broken\n")
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "grandfather ORA-12543")
    fake_agent(monkeypatch, {
        "errors/ORA-12543.md": researched("See [[errors/ORA-99999]] for context."),
        "log.md": "# log\nresearch\n",
    }, {"task": "research", "summary": "s",
        "pages_touched": ["errors/ORA-12543.md"], "flags": []})
    orch.research(["errors/ORA-12543.md"], [])
    assert head_message(research_wiki).startswith("research — ")


def test_research_uses_per_task_adapter_and_model(research_wiki, orch, monkeypatch):
    orch.cfg.agents = {"claude": {"cheap": "sonnet", "strong": "opus"}}
    orch.cfg.research = {"adapter": "claude"}
    seen = {}

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        seen.update(adapter=adapter, model=model, telemetry=telemetry)
        (wiki / "errors" / "ORA-12543.md").write_text(researched("Meaning."))
        return {"task": "research", "summary": "s",
                "pages_touched": ["errors/ORA-12543.md"], "flags": []}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    orch.research(["errors/ORA-12543.md"], [])
    assert seen["adapter"] == "claude" and seen["model"] == "sonnet"
    # the stage always passes a telemetry out-param for the adapter to fill
    assert isinstance(seen["telemetry"], dict)


@pytest.mark.parametrize("model, tier", [
    ("opus", "strong"), ("sonnet", "cheap"), ("haiku", "override")])
def test_research_telemetry_tier_follows_model_override(
        research_wiki, orch, monkeypatch, model, tier):
    orch.cfg.agents = {"claude": {"cheap": "sonnet", "strong": "opus"}}
    orch.cfg.research = {"adapter": "claude", "model": model}

    def _run(adapter, prompt, wiki, m, timeout, web=False, provider=None,
             telemetry=None):
        (wiki / "errors" / "ORA-12543.md").write_text(researched("Meaning."))
        return {"task": "research", "summary": "s",
                "pages_touched": ["errors/ORA-12543.md"], "flags": []}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    orch.research(["errors/ORA-12543.md"], [])
    assert orch.last_telemetry["model_tier"] == tier


# ---- research.mode: structured -------------------------------------------------

def fake_generate(monkeypatch, *answers):
    """Stand in for structured.generate, the pi text call propose_research
    (via structured._propose) routes through — never hits the network."""
    from dbwiki import structured
    seen = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.4,
                             exit_code=0, timed_out=False,
                             usage={"input_tokens": 10, "output_tokens": 5,
                                   "cost_usd": 0.0})
        return answers[min(len(seen) - 1, len(answers) - 1)]

    monkeypatch.setattr(structured, "generate", _gen)
    return seen


def fake_fetch(monkeypatch, ok_codes: set):
    """Stand in for research_structured.fetch_page: succeeds only for codes
    in `ok_codes`, mirroring a real fetch failure (404/timeout/etc.) for the
    rest — never hits the network."""
    from dbwiki import research_structured

    def _fetch(url, timeout=20):
        for code in ok_codes:
            if code.lower() in url:
                return f"Oracle documentation text about {code}."
        return None

    monkeypatch.setattr(research_structured, "fetch_page", _fetch)


def test_structured_research_happy_path_commits_and_validates(research_wiki, orch,
                                                               monkeypatch):
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-12543"})
    fake_generate(monkeypatch, json.dumps(
        {"cause": "TNS destination host unreachable.",
         "action": "- Check the listener\n- Verify the host is reachable"}))
    result = orch.research(["errors/ORA-12543.md"], [])
    assert result["task"] == "research"
    assert "errors/ORA-12543.md" in result["pages_touched"]
    assert "log.md" in result["pages_touched"]
    assert result["flags"] == []
    assert head_message(research_wiki).startswith("research — ")
    assert changed_paths(research_wiki) == ()
    page = (research_wiki / "errors" / "ORA-12543.md").read_text()
    assert "researched:" in page
    assert "## Reference" in page
    assert orch.last_telemetry["mode"] == "structured"
    assert orch.last_telemetry["validation_ok"] is True


def test_structured_research_fetch_failure_flags_and_others_still_succeed(
        research_wiki, orch, monkeypatch):
    (research_wiki / "errors" / "ORA-99999.md").write_text(ERROR_PAGE.replace(
        "ORA-12543", "ORA-99999"))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "seed a second error page")
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-12543"})  # ORA-99999 fails to fetch
    fake_generate(monkeypatch, json.dumps(
        {"cause": "Cause text.", "action": "Action text."}))
    result = orch.research(["errors/ORA-12543.md", "errors/ORA-99999.md"], [])
    assert "errors/ORA-12543.md" in result["pages_touched"]
    assert "errors/ORA-99999.md" not in result["pages_touched"]
    assert any("no fetchable reference for ORA-99999" in f
              for f in result["flags"])
    assert (research_wiki / "errors" / "ORA-99999.md").read_text() == \
        ERROR_PAGE.replace("ORA-12543", "ORA-99999")
    assert "## Reference" in (research_wiki / "errors" / "ORA-12543.md").read_text()
    assert head_message(research_wiki).startswith("research — ")


def three_page_research(research_wiki, orch, monkeypatch) -> list[str]:
    for code in ("ORA-99999", "ORA-88888"):
        (research_wiki / "errors" / f"{code}.md").write_text(
            ERROR_PAGE.replace("ORA-12543", code))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "seed two more error pages")
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-12543", "ORA-99999", "ORA-88888"})
    fake_generate(monkeypatch, json.dumps(
        {"cause": "Cause text.", "action": "Action text."}))
    return ["errors/ORA-12543.md", "errors/ORA-99999.md", "errors/ORA-88888.md"]


def test_structured_research_publishes_one_commit_per_page(research_wiki, orch,
                                                            monkeypatch):
    pages = three_page_research(research_wiki, orch, monkeypatch)
    start = head_sha(research_wiki)
    result = orch.research(pages, [], run_id="run-per-page")
    assert result["pages_touched"] == pages + ["log.md"]
    assert subjects_since(research_wiki, start) == [
        f"research — structured research: {rel}" for rel in pages]
    first = revs_since(research_wiki, start)[0]
    assert commit_paths(research_wiki, first) == {"errors/ORA-12543.md",
                                                  "log.md"}
    assert commit_paths(research_wiki, "HEAD") == {"errors/ORA-88888.md",
                                                   "log.md"}
    assert "Run-ID: run-per-page" in commit_body(research_wiki, first)
    log_then = file_at(research_wiki, first, "log.md")
    assert "errors/ORA-12543.md" in log_then
    assert "errors/ORA-99999.md" not in log_then
    page_then = file_at(research_wiki, first, "errors/ORA-12543.md")
    assert "## Reference" in page_then and "researched:" in page_then
    assert porcelain(research_wiki) == ""


def test_structured_research_killed_midway_keeps_the_pages_it_finished(
        research_wiki, orch, monkeypatch):
    """The reason the loop publishes per page: a run that dies leaves the
    finished pages committed and nothing for an operator to clean up."""
    from dbwiki import research_structured
    pages = three_page_research(research_wiki, orch, monkeypatch)
    real = research_structured.propose_research
    calls = []

    def _propose(prompt, cfg, *, telemetry=None):
        calls.append(prompt)
        if len(calls) == 3:
            raise HarnessError("the adapter died mid-run")
        return real(prompt, cfg, telemetry=telemetry)

    monkeypatch.setattr(research_structured, "propose_research", _propose)
    start = head_sha(research_wiki)
    with pytest.raises(HarnessError):
        orch.research(pages, [])
    assert subjects_since(research_wiki, start) == [
        f"research — structured research: {rel}" for rel in pages[:2]]
    assert porcelain(research_wiki) == ""
    assert (research_wiki / "errors" / "ORA-88888.md").read_text() == \
        ERROR_PAGE.replace("ORA-12543", "ORA-88888")
    assert orch.last_telemetry["rolled_back"] is False
    assert orch.last_telemetry["validation_ok"] is False


def test_structured_research_restores_the_page_an_interrupt_caught(
        research_wiki, orch, monkeypatch):
    """The kill this feature exists for is Ctrl-C, which is a BaseException:
    the page in flight has to come back or the wiki is left dirty anyway."""
    from dbwiki import research_structured
    pages = three_page_research(research_wiki, orch, monkeypatch)
    real = research_structured.apply_research
    applied = []

    def _apply(wiki_root, rel_page, proposal, url, source_slug, today):
        real(wiki_root, rel_page, proposal, url, source_slug, today)
        applied.append(rel_page)
        if len(applied) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(research_structured, "apply_research", _apply)
    start = head_sha(research_wiki)
    with pytest.raises(KeyboardInterrupt):
        orch.research(pages, [])
    assert len(subjects_since(research_wiki, start)) == 2
    assert porcelain(research_wiki) == ""
    assert (research_wiki / "errors" / "ORA-88888.md").read_text() == \
        ERROR_PAGE.replace("ORA-12543", "ORA-88888")


def test_structured_research_lint_blocked_page_is_flagged_not_fatal(
        research_wiki, orch, monkeypatch):
    broken = (ERROR_PAGE.replace("ORA-12543", "ORA-77777")
              + "\nSee [[errors/ORA-00000]] for context.\n")
    (research_wiki / "errors" / "ORA-77777.md").write_text(broken)
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "seed a page with a broken wikilink")
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-77777", "ORA-12543"})
    fake_generate(monkeypatch, json.dumps(
        {"cause": "Cause text.", "action": "Action text."}))
    start = head_sha(research_wiki)
    result = orch.research(["errors/ORA-77777.md", "errors/ORA-12543.md"], [])
    assert result["pages_touched"] == ["errors/ORA-12543.md", "log.md"]
    assert any("lint blocked ORA-77777: wikilink-broken" in f
              for f in result["flags"])
    assert "1 lint blocked" in result["summary"]
    assert (research_wiki / "errors" / "ORA-77777.md").read_text() == broken
    assert subjects_since(research_wiki, start) == [
        "research — structured research: errors/ORA-12543.md"]
    assert porcelain(research_wiki) == ""


def fake_generate_usages(monkeypatch, *usages):
    """`fake_generate` with one usage value per call, so a window can mix
    priced and unpriced calls. A value of "unknown" is a call that reported
    no usage dict at all."""
    from dbwiki import structured
    calls = []

    def _gen(prompt, cfg, *, escalate=False, telemetry=None):
        usage = usages[min(len(calls), len(usages) - 1)]
        calls.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="pi", model="gemma", duration_s=0.4,
                             exit_code=0, timed_out=False, usage=usage)
        return json.dumps({"cause": "Cause text.", "action": "Action text."})

    monkeypatch.setattr(structured, "generate", _gen)
    return calls


def two_page_research(research_wiki, orch, monkeypatch) -> list[str]:
    (research_wiki / "errors" / "ORA-99999.md").write_text(ERROR_PAGE.replace(
        "ORA-12543", "ORA-99999"))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "seed a second error page")
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-12543", "ORA-99999"})
    return ["errors/ORA-12543.md", "errors/ORA-99999.md"]


def test_structured_research_costs_sum_when_every_call_priced_itself(
        research_wiki, orch, monkeypatch):
    pages = two_page_research(research_wiki, orch, monkeypatch)
    fake_generate_usages(
        monkeypatch,
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
        {"input_tokens": 20, "output_tokens": 7, "cost_usd": 0.02})
    orch.research(pages, [])
    assert orch.last_telemetry["usage"] == {
        "input_tokens": 30, "output_tokens": 12, "cost_usd": 0.03}


def test_structured_research_refuses_to_publish_a_partial_cost_sum(
        research_wiki, orch, monkeypatch):
    """A sum over the priced subset would read as the window's total."""
    pages = two_page_research(research_wiki, orch, monkeypatch)
    fake_generate_usages(
        monkeypatch,
        {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01},
        {"input_tokens": 20, "output_tokens": 7, "cost_usd": "unknown"})
    orch.research(pages, [])
    assert orch.last_telemetry["usage"] == {
        "input_tokens": 30, "output_tokens": 12, "cost_usd": "unknown"}


def test_structured_research_without_usage_keeps_the_unknown_sentinel(
        research_wiki, orch, monkeypatch):
    pages = two_page_research(research_wiki, orch, monkeypatch)
    fake_generate_usages(monkeypatch, "unknown")
    orch.research(pages, [])
    assert orch.last_telemetry["usage"] == "unknown"


def test_structured_research_refuses_without_a_fetchable_source(research_wiki,
                                                                 orch):
    orch.cfg.research = {"mode": "structured"}
    (research_wiki / "sources" / "oracle-docs.md").write_text(
        SOURCE_PAGE.replace("fetchable: true", "fetchable: false"))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "make the source unfetchable")
    with pytest.raises(RuntimeError, match="fetchable"):
        orch.research(["errors/ORA-12543.md"], [])


def test_structured_research_bad_model_answer_twice_rolls_back(research_wiki, orch,
                                                                monkeypatch):
    orch.cfg.research = {"mode": "structured"}
    fake_fetch(monkeypatch, {"ORA-12543"})
    fake_generate(monkeypatch, "garbage 1", "garbage 2")
    with pytest.raises(HarnessError):
        orch.research(["errors/ORA-12543.md"], [])
    assert (research_wiki / "errors" / "ORA-12543.md").read_text() == ERROR_PAGE
    assert orch.last_telemetry["rolled_back"] is True
    assert orch.last_telemetry["validation_ok"] is False


def test_structured_research_ignored_when_review_sources_present(research_wiki,
                                                                  orch, monkeypatch):
    """review_sources is not handled by structured mode: any workload there
    takes the whole call agentic, mode setting or not."""
    orch.cfg.research = {"mode": "structured"}
    called = {}

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        called["ran"] = True
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=1.0,
                             exit_code=0, timed_out=False, usage="unknown")
        (wiki / "sources" / "oracle-docs.md").write_text(
            SOURCE_PAGE.replace("last_reviewed: 2026-01-01",
                                "last_reviewed: 2026-07-27"))
        return {"task": "research", "summary": "reviewed",
                "pages_touched": ["sources/oracle-docs.md"], "flags": []}

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    orch.research([], ["sources/oracle-docs.md"])
    assert called.get("ran") is True
    assert orch.last_telemetry.get("mode") != "structured"


def test_structured_research_dry_run_touches_nothing(research_wiki, orch, capsys):
    orch.cfg.research = {"mode": "structured"}
    assert orch.research(["errors/ORA-12543.md"], [], dry_run=True) == {}
    assert "Task: research (structured)." in capsys.readouterr().out
    assert changed_paths(research_wiki) == ()


# ---- a forgotten `notable` never costs a rollback -----------------------------

DAY = "2026-07-27"
WINDOW = (f"{DAY}T00:00:00Z", f"{DAY}T20:19:00Z")


def report_edits() -> dict:
    return {f"reports/{DAY}.md": "---\ntype: report\n---\n\n# report\n",
            "log.md": "# log\n[report] window done\n"}


def digest(notable: bool) -> dict:
    """Minimal digest with the keys ingest and the content hash read."""
    groups = [{"rule": "ora-error", "template": "ORA-00600", "count": 3,
               "class": "error"}] if notable else []
    return {"schema_version": 1, "db": "cdb1",
            "window": {"from": WINDOW[0], "to": WINDOW[1], "day": DAY},
            "deltas": [], "notable": notable,
            "sources": {"alert": {"total_events": 3, "notable": groups}}}


def seed_digest(wiki, notable: bool):
    p = wiki / "digests" / "cdb1" / f"{DAY}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(digest(notable)))
    return p


@pytest.mark.parametrize("notable", [True, False])
def test_report_defaults_a_missing_notable_to_the_window_notability(
        wiki, orch, monkeypatch, notable):
    """The orchestrator derives the window's notability from what was ingested,
    so an agent that forgets the key still gets its report committed."""
    fake_agent(monkeypatch, report_edits(),
               {"task": "report", "summary": "window summary",
                "pages_touched": [f"reports/{DAY}.md", "log.md"]})
    res = orch.report(DAY, WINDOW,
                      [{"db": "cdb1", "notable": notable, "summary": "s"}])
    assert res["notable"] is notable
    assert head_message(wiki).startswith(f"report: {DAY} — window summary")
    assert changed_paths(wiki) == ()


def test_report_keeps_the_agents_own_notable_claim(wiki, orch, monkeypatch):
    fake_agent(monkeypatch, report_edits(),
               {"task": "report", "summary": "s", "notable": True,
                "pages_touched": [f"reports/{DAY}.md", "log.md"]})
    res = orch.report(DAY, WINDOW, [{"db": "cdb1", "notable": False,
                                     "summary": "s"}])
    assert res["notable"] is True


def test_report_still_fails_without_a_summary(wiki, orch, monkeypatch):
    """Only `notable` is defaulted: the keys the orchestrator cannot synthesize
    stay hard requirements."""
    fake_agent(monkeypatch, report_edits(),
               {"task": "report", "pages_touched": [f"reports/{DAY}.md",
                                                    "log.md"]})
    with pytest.raises(ValidationError, match="missing key 'summary'"):
        orch.report(DAY, WINDOW, [{"db": "cdb1", "notable": True,
                                   "summary": "s"}])
    assert not (wiki / "reports" / f"{DAY}.md").exists()


@pytest.mark.parametrize("notable", [True, False])
def test_ingest_defaults_a_missing_notable_to_the_digests(wiki, orch,
                                                          monkeypatch, notable):
    """Same rail on ingest: the digest states notability, not the agent."""
    orch.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    digest_json = seed_digest(wiki, notable)
    fake_agent(monkeypatch, {"log.md": "# log\n[ingest] cdb1\n"},
               {"task": "ingest", "summary": "ingested cdb1",
                "pages_touched": ["log.md"]})
    res = orch.ingest("cdb1", digest_json)
    assert res["notable"] is notable
    ledger = orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]
    assert ledger["status"] == "ingested" and ledger["notable"] is notable


# ---- feedback retry (agents.feedback_retries) --------------------------------

def sequenced_agent(monkeypatch, steps: list):
    """Stand in for run_agent across a *sequence* of calls: `steps[i]` is
    either an exception instance to raise on call i, or an
    (edits, result) pair applied/returned the way `fake_agent` does. Returns
    the list of prompts seen, in call order, so a test can inspect the
    feedback text appended to a retry."""
    calls: list[str] = []

    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        calls.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=1.5,
                             exit_code=0, timed_out=False, stdout_bytes=42,
                             usage="unknown")
        step = steps[len(calls) - 1]
        if isinstance(step, BaseException):
            raise step
        edits, result = step
        for rel, text in edits.items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        return result

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    return calls


def ingest_setup(wiki, orch):
    orch.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    return seed_digest(wiki, False)


def test_ingest_retries_once_on_validation_failure_then_succeeds(wiki, orch,
                                                                  monkeypatch):
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        ({"notes.md": "attempt 1, no log.md\n"},
         {"task": "ingest", "summary": "bad", "pages_touched": []}),
        ({"log.md": "# log\n[ingest] cdb1\n"},
         {"task": "ingest", "summary": "fixed",
          "pages_touched": ["log.md", "notes.md"]}),
    ])
    res = orch.ingest("cdb1", digest_json)
    assert res["summary"] == "fixed"
    assert len(calls) == 2
    assert "rejected by validation" in calls[1]
    assert "log.md not updated" in calls[1]
    # a retry never rolls back, so attempt 1's notes.md is still in the tree
    # and attempt 2 has to declare it (undeclared change)
    assert "undeclared change: notes.md" in calls[1]
    assert orch.last_telemetry["attempts"] == 2
    assert orch.last_telemetry["validation_ok"] is True
    assert orch.last_telemetry["rolled_back"] is False
    ledger = orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]
    assert ledger["status"] == "ingested"


def test_ingest_fail_then_fail_rolls_back_with_two_attempts(wiki, orch,
                                                             monkeypatch):
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        ({"notes.md": "attempt 1, still no log.md\n"},
         {"task": "ingest", "summary": "bad1", "pages_touched": []}),
        ({"notes2.md": "attempt 2, still no log.md\n"},
         {"task": "ingest", "summary": "bad2", "pages_touched": []}),
    ])
    with pytest.raises(ValidationError, match="log.md not updated"):
        orch.ingest("cdb1", digest_json)
    assert len(calls) == 2
    assert "rejected by validation" in calls[1]
    assert orch.last_telemetry["attempts"] == 2
    assert orch.last_telemetry["validation_ok"] is False
    assert orch.last_telemetry["rolled_back"] is True
    assert not (wiki / "notes.md").exists()
    assert not (wiki / "notes2.md").exists()
    ledger = orch.state.get_ledger()[f"digests/cdb1/{DAY}.json"]
    assert ledger["status"] == "failed"


def test_ingest_feedback_retries_absent_is_one_attempt(wiki, orch, monkeypatch):
    """Default (feedback_retries absent -> 0): current behavior unchanged,
    exactly one attempt even though a second, fixing step is queued."""
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        ({"notes.md": "attempt 1, no log.md\n"},
         {"task": "ingest", "summary": "bad", "pages_touched": []}),
        ({"log.md": "# log\nshould never run\n"},
         {"task": "ingest", "summary": "should not be reached",
          "pages_touched": ["log.md"]}),
    ])
    with pytest.raises(ValidationError, match="log.md not updated"):
        orch.ingest("cdb1", digest_json)
    assert len(calls) == 1
    assert orch.last_telemetry["attempts"] == 1
    assert orch.last_telemetry["rolled_back"] is True


def test_ingest_feedback_retries_zero_explicit_is_one_attempt(wiki, orch,
                                                               monkeypatch):
    orch.cfg.agents["feedback_retries"] = 0
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        ({"notes.md": "attempt 1, no log.md\n"},
         {"task": "ingest", "summary": "bad", "pages_touched": []}),
    ])
    with pytest.raises(ValidationError, match="log.md not updated"):
        orch.ingest("cdb1", digest_json)
    assert len(calls) == 1
    assert orch.last_telemetry["attempts"] == 1


def test_ingest_retries_on_no_result_error_then_succeeds(wiki, orch,
                                                          monkeypatch):
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        NoResultError("codex finished but wrote no .agent-result.json"),
        ({"log.md": "# log\n[ingest] cdb1\n"},
         {"task": "ingest", "summary": "fixed", "pages_touched": ["log.md"]}),
    ])
    res = orch.ingest("cdb1", digest_json)
    assert res["summary"] == "fixed"
    assert len(calls) == 2
    assert "valid .agent-result.json" in calls[1]
    assert orch.last_telemetry["attempts"] == 2
    assert orch.last_telemetry["rolled_back"] is False


def test_ingest_retries_on_invalid_result_json_then_succeeds(wiki, orch,
                                                              monkeypatch):
    """The retry recognizes an unparsable result file by type, not by reading
    its message — while the message itself stays what health.categorize and
    the ledger's category inference already key on."""
    assert isinstance(InvalidResultError("x"), HarnessError)
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        InvalidResultError("invalid result JSON: Expecting value: line 1 "
                          "column 1 (char 0)"),
        ({"log.md": "# log\n[ingest] cdb1\n"},
         {"task": "ingest", "summary": "fixed", "pages_touched": ["log.md"]}),
    ])
    res = orch.ingest("cdb1", digest_json)
    assert res["summary"] == "fixed"
    assert len(calls) == 2
    assert "valid .agent-result.json" in calls[1]
    assert orch.last_telemetry["attempts"] == 2


def test_ingest_timeout_is_never_retried(wiki, orch, monkeypatch):
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        HarnessError("agent timed out after 900s: codex"),
        ({"log.md": "# log\nshould never run\n"},
         {"task": "ingest", "summary": "should not be reached",
          "pages_touched": ["log.md"]}),
    ])
    with pytest.raises(HarnessError, match="timed out"):
        orch.ingest("cdb1", digest_json)
    assert len(calls) == 1
    assert orch.last_telemetry["attempts"] == 1
    assert orch.last_telemetry["rolled_back"] is True


def test_ingest_nonzero_exit_is_never_retried(wiki, orch, monkeypatch):
    orch.cfg.agents["feedback_retries"] = 1
    digest_json = ingest_setup(wiki, orch)
    calls = sequenced_agent(monkeypatch, [
        HarnessError("codex exited 3: boom"),
        ({"log.md": "# log\nshould never run\n"},
         {"task": "ingest", "summary": "should not be reached",
          "pages_touched": ["log.md"]}),
    ])
    with pytest.raises(HarnessError, match="exited 3"):
        orch.ingest("cdb1", digest_json)
    assert len(calls) == 1
    assert orch.last_telemetry["attempts"] == 1


# ---- NoResultError: keep the work, synthesize the missing result -------------
#
# The live failure: the strong-tier report agent writes its whole analysis to
# the wiki and then stops without .agent-result.json, and the stage used to
# roll every page of it back (report NoResult — ~$13/day of opus
# discarded). Synthesis was already wired for ingest only.

def no_result_agent(monkeypatch, edits: dict):
    """An agent run that made its edits and then failed to write the result
    JSON — exit 0, files on disk, nothing to parse."""
    def _run(adapter, prompt, wiki, model, timeout, web=False, provider=None,
             telemetry=None):
        if telemetry is not None:
            telemetry.update(adapter=adapter, model=model, duration_s=440.0,
                             exit_code=0, timed_out=False, stdout_bytes=99,
                             usage="unknown")
        for rel, text in edits.items():
            p = wiki / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        raise NoResultError(f"{adapter} finished but wrote no "
                            f".agent-result.json; last output: …prose…")
    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)


def test_report_synthesizes_a_result_when_only_the_json_is_missing(
        wiki, orch, monkeypatch):
    no_result_agent(monkeypatch, {
        **report_edits(),
        "index.md": "# index\n\n- reports\n",
    })
    res = orch.report(DAY, WINDOW, [{"db": "cdb1", "notable": True,
                                     "summary": "s"}])
    assert res["summary"].startswith("(result synthesized")
    assert res["notable"] is True          # the window's notability, not a claim
    assert res["flags"]                    # says the agent omitted the JSON
    assert head_message(wiki).startswith(f"report: {DAY} — (result synthesized")
    assert (wiki / "reports" / f"{DAY}.md").exists()
    assert changed_paths(wiki) == ()
    assert orch.last_telemetry["validation_ok"] is True
    assert orch.last_telemetry["rolled_back"] is False


def test_report_synthesis_still_needs_the_report_page(wiki, orch, monkeypatch):
    """Synthesis is not a bypass: the stage's own validation still runs, so an
    agent that only produced prose fails exactly as it did before."""
    no_result_agent(monkeypatch, {"log.md": "# log\n[report] only prose\n"})
    with pytest.raises(ValidationError, match="report file missing"):
        orch.report(DAY, WINDOW, [{"db": "cdb1", "notable": True,
                                   "summary": "s"}])
    assert changed_paths(wiki) == ()


def test_report_with_no_edits_at_all_still_raises_no_result(wiki, orch,
                                                             monkeypatch):
    sequenced_agent(monkeypatch, [
        NoResultError("codex finished but wrote no .agent-result.json")])
    with pytest.raises(NoResultError):
        orch.report(DAY, WINDOW, [{"db": "cdb1", "notable": True,
                                   "summary": "s"}])
    assert changed_paths(wiki) == ()
    assert orch.last_telemetry["rolled_back"] is True


def test_lint_synthesizes_a_result_when_only_the_json_is_missing(
        wiki, orch, monkeypatch):
    no_result_agent(monkeypatch, {"changelog.md": "# changelog\ntidied\n"})
    res = orch.lint()
    assert res["summary"].startswith("(result synthesized")
    assert head_message(wiki).startswith("lint — (result synthesized")
    assert changed_paths(wiki) == ()
    assert orch.last_telemetry["validation_ok"] is True


def test_lint_with_no_edits_at_all_still_raises_no_result(wiki, orch,
                                                           monkeypatch):
    sequenced_agent(monkeypatch, [
        NoResultError("codex finished but wrote no .agent-result.json")])
    with pytest.raises(NoResultError):
        orch.lint()
    assert orch.last_telemetry["rolled_back"] is True


def test_research_synthesizes_a_result_when_only_the_json_is_missing(
        research_wiki, orch, monkeypatch):
    no_result_agent(monkeypatch, {
        "errors/ORA-12543.md": researched(
            "Host unreachable per the error reference. (source: "
            "sources/oracle-docs; url: https://docs.oracle.com/en/ORA-12543; "
            "accessed: 2026-07-25)"),
        "log.md": "# log\n[2026-07-25T09:00Z] research — 1 page\n",
    })
    res = orch.research(["errors/ORA-12543.md"], [])
    assert res["summary"].startswith("(result synthesized")
    assert head_message(research_wiki).startswith("research — (result synthesized")
    assert "## Reference" in (research_wiki / "errors" / "ORA-12543.md").read_text()
    assert changed_paths(research_wiki) == ()


def test_research_synthesis_still_obeys_the_path_allowlist(research_wiki, orch,
                                                            monkeypatch):
    """The research rails outlive the missing result file: a synthesized
    result whose edits leave errors//sources/ still rolls back."""
    no_result_agent(monkeypatch, {
        "errors/ORA-12543.md": researched("Meaning only, no URL."),
        "incidents/2026-07-25-cdb1-x.md": "---\ntype: incident\n---\n\n# nope\n",
    })
    with pytest.raises(ValidationError, match="outside errors/"):
        orch.research(["errors/ORA-12543.md"], [])
    assert not (research_wiki / "incidents").exists()


def test_a_base_that_moved_at_publish_time_leaves_the_tree_clean(wiki, orch,
                                                                 monkeypatch):
    """`BaseMoved`/`TreeDirty` used to raise over the agent's writes, so every
    later tick refused on the debris of this one."""
    digest_json = ingest_setup(wiki, orch)
    fake_agent(monkeypatch, {"log.md": "# log\n[ingest] cdb1\n",
                             "notes.md": "half an ingest\n"},
               {"task": "ingest", "summary": "s",
                "pages_touched": ["log.md", "notes.md"]})
    real_head, calls = transaction.head, []

    def moved(repo):
        calls.append(repo)
        return real_head(repo) if len(calls) == 1 else "0" * 40

    monkeypatch.setattr(transaction, "head", moved)
    with pytest.raises(RuntimeError, match="wiki changed under the lock"):
        orch.ingest("cdb1", digest_json)
    monkeypatch.undo()
    assert changed_paths(wiki) == ()
    assert not (wiki / "notes.md").exists()
    assert (wiki / "log.md").read_text() == "# log\n"


def test_a_file_a_human_staged_survives_the_rollback(wiki, orch, monkeypatch):
    """`clean -fd` never removed an index entry, and `restore` does. A human
    who `git add`ed during the agent's window told git the file is theirs."""
    digest_json = ingest_setup(wiki, orch)

    def _run(adapter, prompt, repo, model, timeout, web=False, provider=None,
             telemetry=None):
        (repo / "notes.md").write_text("agent scribble\n")
        (repo / "human.md").write_text("mine, staged mid-run\n")
        git(repo, "add", "human.md")
        raise HarnessError("codex exited 3: boom")

    monkeypatch.setattr("dbwiki.orchestrate.run_agent", _run)
    with pytest.raises(HarnessError, match="exited 3"):
        orch.ingest("cdb1", digest_json)
    assert (wiki / "human.md").read_text() == "mine, staged mid-run\n"
    assert not (wiki / "notes.md").exists()


def test_a_checkout_with_no_git_identity_cannot_commit_as_a_placeholder(orch, monkeypatch):
    def refuse(wiki):
        raise RuntimeError("no actor")
    monkeypatch.setattr("dbwiki.transaction.resolve_actor", refuse)
    with pytest.raises(RuntimeError, match="no actor"):
        orch.actor


def test_an_advisory_click_times_out_well_before_an_agentic_stage(orch):
    """`advisory.DEFAULT_TIMEOUT_S` is never `agents.timeout_seconds`: that
    budget is for a stage nobody is waiting on, and an operator watching a
    spinner is."""
    assert orch.cfg.agents == {}          # so `orch.timeout` is the default
    assert advisory.DEFAULT_TIMEOUT_S < orch.timeout


# ---- structured fallback when the agentic escalated report dies --------------
#
# The live failure: `codex` hit its usage limit and every notable window
# rolled back as harness_error in 2.4s, so the wiki got no report at all —
# while the structured escalated path was running fine on the local model.

NOTABLE_INGESTED = [{"db": "cdb1", "notable": True,
                     "summary": "ORA-00600 storm"}]


def escalated_proposal(**over) -> str:
    """A proposal the escalated contract accepts for a window where cdb1 is
    the only notable database."""
    p = {"schema_version": 1,
         "summary": "cdb1 raised an ORA-00600 storm",
         "overview": "cdb1 raised repeated ORA-00600 through the window. "
                     "Nothing else in the fleet moved.",
         "items": [{"db": "cdb1", "status_line": "ORA-00600 ×3"}],
         "open_incident_notes": [],
         "flags": [],
         "notable_analysis": [{"db": "cdb1",
                               "analysis": "Same signature as the June "
                                           "storm on this database."}]}
    p.update(over)
    return json.dumps(p)


def agent_runs(orch) -> list[dict]:
    return [json.loads(ln) for ln in
            (orch.cfg.state_dir / "agent_runs.jsonl").read_text().splitlines()]


def test_report_falls_back_to_structured_when_the_agentic_adapter_dies(
        wiki, orch, monkeypatch):
    sequenced_agent(monkeypatch, [
        HarnessError("codex exited 1: usage limit reached")])
    seen = fake_generate(monkeypatch, escalated_proposal())
    res = orch.report(DAY, WINDOW, NOTABLE_INGESTED)
    assert seen and "notable_analysis" in seen[0]
    assert (wiki / "reports" / f"{DAY}.md").exists()
    assert head_message(wiki).startswith(f"report: {DAY}")
    assert changed_paths(wiki) == ()
    assert any("fallback: structured after agentic harness_error" in f
               and "usage limit reached" in f for f in res["flags"])
    assert res["mode"] == "structured"
    assert orch.last_telemetry["mode"] == "structured"
    assert orch.last_telemetry["model_tier"] == "strong"
    assert orch.last_telemetry["validation_ok"] is True
    assert orch.last_telemetry["rolled_back"] is False
    dead, fallback = agent_runs(orch)
    assert dead["mode"] == "agentic" and dead["rolled_back"] is True
    assert fallback["mode"] == "structured"
    assert fallback["validation_ok"] is True
    assert fallback["rolled_back"] is False


def test_report_no_result_synthesis_never_reaches_the_structured_fallback(
        wiki, orch, monkeypatch):
    no_result_agent(monkeypatch, report_edits())
    seen = fake_generate(monkeypatch, escalated_proposal())
    res = orch.report(DAY, WINDOW, NOTABLE_INGESTED)
    assert seen == []
    assert res["summary"].startswith("(result synthesized")


def test_report_fallback_none_keeps_the_harness_error(wiki, orch, monkeypatch):
    orch.cfg.agents["escalated_report_fallback"] = "none"
    sequenced_agent(monkeypatch, [
        HarnessError("codex exited 1: usage limit reached")])
    seen = fake_generate(monkeypatch, escalated_proposal())
    with pytest.raises(HarnessError, match="usage limit reached"):
        orch.report(DAY, WINDOW, NOTABLE_INGESTED)
    assert seen == []
    assert not (wiki / "reports" / f"{DAY}.md").exists()
    assert orch.last_telemetry["rolled_back"] is True
    assert changed_paths(wiki) == ()


def test_report_raises_when_the_structured_fallback_also_fails(wiki, orch,
                                                                monkeypatch):
    sequenced_agent(monkeypatch, [
        HarnessError("codex exited 1: usage limit reached")])
    seen = fake_generate(monkeypatch, "no json", "still no json")
    with pytest.raises(HarnessError, match="invalid structured proposal"):
        orch.report(DAY, WINDOW, NOTABLE_INGESTED)
    assert len(seen) == 2
    assert not (wiki / "reports" / f"{DAY}.md").exists()
    assert changed_paths(wiki) == ()
    assert orch.last_telemetry["rolled_back"] is True
    assert orch.last_telemetry["mode"] == "structured"


def test_report_never_falls_back_when_the_agentic_result_is_unparsable(
        wiki, orch, monkeypatch):
    """An adapter that answered badly ran; only a dead harness earns the
    second attempt."""
    sequenced_agent(monkeypatch, [
        InvalidResultError("invalid result JSON: Expecting value: line 1 "
                           "column 1 (char 0)")])
    seen = fake_generate(monkeypatch, escalated_proposal())
    with pytest.raises(InvalidResultError):
        orch.report(DAY, WINDOW, NOTABLE_INGESTED)
    assert seen == []
    assert not (wiki / "reports" / f"{DAY}.md").exists()
    assert orch.last_telemetry["rolled_back"] is True
    assert changed_paths(wiki) == ()


# ---- machine commits are path-scoped ----------------------------------------

def _committed_files(repo) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "show", "--name-only",
                          "--format=", "HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def _html_orch(wiki, tmp_path):
    cfg = SimpleNamespace(wiki_repo=wiki, state_dir=tmp_path / "state",
                          agents={}, report={}, research={}, portal={})
    return Orchestrator(cfg, lock=Held(tmp_path / "state", "test", 0.0))


def test_commit_digests_leaves_a_staged_human_file_out_of_the_commit(wiki, orch):
    (wiki / "human-note.md").write_text("mine, not ready\n")
    git(wiki, "add", "human-note.md")
    (wiki / "digests" / "cdb1").mkdir()
    (wiki / "digests" / "cdb1" / "2026-09-08.md").write_text("# digest\n")

    orch._commit_digests()

    assert _committed_files(wiki) == ["digests/cdb1/2026-09-08.md"]
    assert transaction.staged_paths(wiki) == ("human-note.md",)
    assert (wiki / "human-note.md").read_text() == "mine, not ready\n"


def test_commit_digests_does_nothing_when_only_a_human_file_is_staged(wiki, orch):
    before = transaction.head(wiki)
    (wiki / "human-note.md").write_text("mine, not ready\n")
    git(wiki, "add", "human-note.md")

    orch._commit_digests()

    assert transaction.head(wiki) == before
    assert transaction.staged_paths(wiki) == ("human-note.md",)


def _files_since(repo, base) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "log", "--name-only",
                          "--format=", f"{base}..HEAD"],
                         capture_output=True, text=True, check=True).stdout
    return out.split()


def test_render_html_leaves_a_staged_human_file_out_of_the_commit(wiki, tmp_path):
    base = transaction.head(wiki)
    (wiki / "human-note.md").write_text("mine, not ready\n")
    git(wiki, "add", "human-note.md")

    sha = _html_orch(wiki, tmp_path).render_html("2026-09-08")

    assert sha not in ("(nothing to commit)", "(render failed)")
    assert all(p.startswith("html/") for p in _files_since(wiki, base))
    assert transaction.staged_paths(wiki) == ("human-note.md",)
    assert (wiki / "human-note.md").read_text() == "mine, not ready\n"


# ---- human-owned incident state (ADR-0003) ----------------------------------

INCIDENT_REL = "incidents/2026-09-01-cdb1-listener-wedged.md"
_WINDOW = MonitoringWindow(ErrorAbsent("TNS-12564"),
                           "2026-09-01T10:00:00Z", "2026-09-02T10:00:00Z")
_ACTION = ActionRecord(at="2026-09-01T10:00:00Z", kind="start-monitoring",
                       actor="dba@example.com",
                       intent="watch for the error to stop",
                       summary="bounced the listener, watching",
                       status_after=Status.MONITORING, window=_WINDOW)
_RESOLUTION_ROW = (f"| 2026-09-01 | cdb1 | [[{INCIDENT_REL[:-3]}]] "
                   f"| bounced the listener | digests/cdb1/2026-09-01.md |")
INCIDENT_PAGE = (
    "---\n"
    "type: incident\n"
    "status: monitoring\n"
    "db: cdb1\n"
    "opened: 2026-09-01T09:00:00Z\n"
    "updated: 2026-09-01T10:00:00Z\n"
    f"monitoring: {_WINDOW.to_frontmatter()}\n"
    "---\n"
    "\n# Listener wedged on cdb1\n"
    "\n## Evidence\n"
    "\n- 2026-09-01: digests/cdb1/2026-09-01.md\n"
    "\n" + render_action(_ACTION) +
    "\n## Resolution history\n"
    "\n" + RESOLUTION_HEAD + "\n" + _RESOLUTION_ROW + "\n")


@pytest.fixture
def incident_wiki(wiki):
    page = wiki / INCIDENT_REL
    page.parent.mkdir(parents=True)
    page.write_text(INCIDENT_PAGE)
    cited = wiki / "digests" / "cdb1" / "2026-09-01.md"
    cited.parent.mkdir(parents=True, exist_ok=True)
    cited.write_text("# cdb1 2026-09-01\n")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "incident opened, monitoring started")
    return wiki


def rail(orch, wiki, text: str) -> list[str]:
    """The incident-ownership rail's verdict on one agent rewrite of the page."""
    base = transaction.head(wiki)
    (wiki / INCIDENT_REL).write_text(text)
    return orch._incident_problems(orch._propose(base, "ingest"), base)


def test_the_base_incident_page_parses_as_the_rail_reads_it(incident_wiki):
    parsed = read_incident(INCIDENT_PAGE, INCIDENT_REL)
    assert parsed.status is Status.MONITORING
    assert parsed.actions.records == (_ACTION,)
    assert parsed.actions.problems == ()


def test_agent_may_not_flip_incident_status(incident_wiki, orch):
    problems = rail(orch, incident_wiki,
                    INCIDENT_PAGE.replace("status: monitoring",
                                          "status: resolved"))
    assert problems == [
        f"{INCIDENT_REL}: status is human-owned: monitoring -> resolved "
        f"(use `dbwiki incident`)"]


def test_agent_may_not_rewrite_the_monitoring_window(incident_wiki, orch):
    extended = MonitoringWindow(_WINDOW.signal, _WINDOW.start,
                                "2026-09-09T10:00:00Z")
    problems = rail(orch, incident_wiki,
                    INCIDENT_PAGE.replace(
                        f"monitoring: {_WINDOW.to_frontmatter()}\n",
                        f"monitoring: {extended.to_frontmatter()}\n"))
    assert problems == [
        f"{INCIDENT_REL}: monitoring: is human-owned (use `dbwiki incident`)"]


def test_agent_may_not_delete_an_action_record(incident_wiki, orch):
    stripped = INCIDENT_PAGE.replace(render_action(_ACTION), "")
    problems = rail(orch, incident_wiki, stripped)
    assert problems == [
        f"{INCIDENT_REL}: `## Action {_ACTION.at}` was removed; "
        f"action records are human-owned"]


def test_agent_may_not_alter_an_action_record(incident_wiki, orch):
    problems = rail(orch, incident_wiki,
                    INCIDENT_PAGE.replace(
                        "summary: bounced the listener, watching",
                        "summary: the listener recovered on its own"))
    assert problems == [
        f"{INCIDENT_REL}: `## Action {_ACTION.at}` was altered; "
        f"action records are human-owned"]


def test_agent_may_not_drop_a_resolution_history_row(incident_wiki, orch):
    problems = rail(orch, incident_wiki,
                    INCIDENT_PAGE.replace(_RESOLUTION_ROW + "\n", ""))
    assert problems == [
        f"{INCIDENT_REL}: a `## Resolution history` row was removed or "
        f"altered: {_RESOLUTION_ROW}"]


def test_agent_may_not_delete_an_incident_page(incident_wiki, orch):
    base = transaction.head(incident_wiki)
    (incident_wiki / INCIDENT_REL).unlink()
    problems = orch._incident_problems(orch._propose(base, "ingest"), base)
    assert problems == [f"{INCIDENT_REL}: an agent may not delete an "
                        f"incident page (use `dbwiki incident`)"]


def test_an_update_append_is_the_allowed_agent_edit(incident_wiki, orch):
    appended = (INCIDENT_PAGE.replace("updated: 2026-09-01T10:00:00Z",
                                      "updated: 2026-09-02T08:00:00Z")
                + "\n## Update 2026-09-02\n"
                "\nThe error has not recurred since the bounce.\n"
                "\nevidence: digests/cdb1/2026-09-02.md\n")
    assert rail(orch, incident_wiki, appended) == []


def test_a_new_incident_page_has_no_base_to_protect(incident_wiki, orch):
    base = transaction.head(incident_wiki)
    fresh = incident_wiki / "incidents" / "2026-09-02-cdb1-new.md"
    fresh.write_text("---\ntype: incident\nstatus: open\ndb: cdb1\n---\n\n# New\n")
    assert orch._incident_problems(orch._propose(base, "ingest"), base) == []


def test_validate_refuses_a_change_the_agent_did_not_declare(incident_wiki, orch):
    (incident_wiki / INCIDENT_REL).write_text(
        INCIDENT_PAGE + "\n## Update 2026-09-02\n\nquietly.\n")
    (incident_wiki / "log.md").write_text("# log\n[ingest] cdb1\n")
    problems = orch._validate(result(pages_touched=["log.md"]), "ingest",
                              changed(incident_wiki))
    assert problems == [f"undeclared change: {INCIDENT_REL}"]


def test_validate_does_not_ask_for_index_and_log_to_be_declared(wiki, orch):
    (wiki / "log.md").write_text("# log\n[ingest] cdb1\n")
    (wiki / "index.md").write_text("---\ntype: index\n---\n\n# Logbook\n")
    assert orch._validate(result(pages_touched=[]), "ingest",
                          changed(wiki)) == []


def test_ingest_refuses_the_reviews_reproduction(incident_wiki, orch, monkeypatch):
    """The 2026-09-05 review's finding #2, end to end: an agent resolves an
    incident, updates log.md, and declares only log.md."""
    digest_json = ingest_setup(incident_wiki, orch)
    fake_agent(monkeypatch,
               {INCIDENT_REL: INCIDENT_PAGE.replace("status: monitoring",
                                                    "status: resolved"),
                "log.md": "# log\n[ingest] cdb1 resolved\n"},
               {"task": "ingest", "summary": "cdb1 recovered",
                "pages_touched": ["log.md"]})
    with pytest.raises(ValidationError) as exc:
        orch.ingest("cdb1", digest_json)
    assert "status is human-owned" in str(exc.value)
    assert f"undeclared change: {INCIDENT_REL}" in str(exc.value)
    assert (incident_wiki / INCIDENT_REL).read_text() == INCIDENT_PAGE


def test_a_structured_update_replay_does_not_disturb_the_action_record(
        incident_wiki, orch):
    """`pagetext.day_block_replace` collapses blank-line runs page-wide when
    structured ingest rewrites a `## Update <day>` block, which moves bytes
    inside the `## Action` section. The rail compares parsed records, so the
    ingest writer's own second pass over a day still validates."""
    day = "2026-09-02"
    first = day_block_replace(
        INCIDENT_PAGE, re.compile(rf"\A## Update {day}\s*\Z"),
        f"\n## Update {day}\n\nfirst wording\n\nevidence: digests/cdb1/{day}.md\n")
    (incident_wiki / "digests" / "cdb1" / f"{day}.md").write_text("# cdb1\n")
    assert rail(orch, incident_wiki, first) == []

    replayed = day_block_replace(
        first, re.compile(rf"\A## Update {day}\s*\Z"),
        f"\n## Update {day}\n\nreworded\n\nevidence: digests/cdb1/{day}.md\n")
    assert render_action(_ACTION) in INCIDENT_PAGE
    assert sections(replayed) != sections(first)   # the collapse really happens
    assert rail(orch, incident_wiki, replayed) == []


# ---- research --caveats --------------------------------------------------------

DOCS_URL = "https://docs.oracle.com/en/error-help/db/ora-01013/"
CITATION = f"(source: sources/oracle-docs; url: {DOCS_URL}; accessed: 2026-07-28)"


def caveat_page(code: str) -> str:
    return (f"---\ntype: error-class\nupdated: 2026-07-10T00:00:00Z\n"
            f"researched: 2026-07-28\n---\n\n# {code}\n\n## Reference\n\n"
            f"**Cause:** the operation was interrupted\n{CITATION}.\n\n"
            f"**Action:** continue with the next operation\n{CITATION}.\n")


def fake_web_answers(monkeypatch, answers: dict) -> list[str]:
    """Stand in for harness.run_web_text, the one call this stage makes: the
    canned answer is chosen by the error code the prompt names, so a page can
    be given an answer that fails validation twice."""
    from dbwiki import research_caveats
    seen = []

    def _run(prompt, model, timeout, *, telemetry=None):
        seen.append(prompt)
        if telemetry is not None:
            telemetry.update(adapter="claude", model=model, duration_s=0.7,
                             exit_code=0, timed_out=False,
                             usage={"input_tokens": 20, "output_tokens": 8,
                                    "cost_usd": 0.01})
        for code, answer in answers.items():
            if code in prompt:
                return answer
        return '{"notes": []}'

    monkeypatch.setattr(research_caveats, "run_web_text", _run)
    return seen


def caveat_wiki_pages(research_wiki, orch, monkeypatch) -> list[str]:
    for code in ("ORA-1", "ORA-2", "ORA-3"):
        (research_wiki / "errors" / f"{code}.md").write_text(caveat_page(code))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "seed three researched error pages")
    good = json.dumps({"notes": [
        {"text": "A gotcha the docs omit.", "source": "oracle-docs",
         "url": DOCS_URL}]})
    fake_web_answers(monkeypatch, {"ORA-1": good, "ORA-2": good,
                                   "ORA-3": "not a proposal at all"})
    return ["errors/ORA-1.md", "errors/ORA-2.md", "errors/ORA-3.md"]


def test_caveats_commits_per_page_and_contains_a_bad_answer(
        research_wiki, orch, monkeypatch):
    """A page whose answer never validates costs that page and nothing else:
    it is restored, flagged, and the pages behind it still publish."""
    pages = caveat_wiki_pages(research_wiki, orch, monkeypatch)
    start = head_sha(research_wiki)
    result = orch.caveats(pages, run_id="run-caveats")

    assert subjects_since(research_wiki, start) == [
        "research — caveats: errors/ORA-1.md",
        "research — caveats: errors/ORA-2.md"]
    assert result["pages_touched"] == ["errors/ORA-1.md", "errors/ORA-2.md",
                                       "log.md"]
    assert result["summary"] == "caveats: 2 page(s), 2 note(s), 1 failed"
    assert any("caveats failed for ORA-3" in f for f in result["flags"])
    assert commit_paths(research_wiki, "HEAD") == {"errors/ORA-2.md", "log.md"}
    assert "Run-ID: run-caveats" in commit_body(research_wiki, "HEAD")

    page = (research_wiki / "errors" / "ORA-1.md").read_text()
    assert "**Practitioner note:** A gotcha the docs omit." in page
    assert "caveats_reviewed:" in page
    assert (research_wiki / "errors" / "ORA-3.md").read_text() == \
        caveat_page("ORA-3")
    assert file_at(research_wiki, "HEAD", "errors/ORA-3.md") == \
        caveat_page("ORA-3")
    assert porcelain(research_wiki) == ""
    assert orch.last_telemetry["mode"] == "caveats"
    assert orch.last_telemetry["validation_ok"] is True


def test_caveats_refuses_without_an_approved_fetchable_source(research_wiki,
                                                              orch):
    (research_wiki / "sources" / "oracle-docs.md").write_text(
        SOURCE_PAGE.replace("fetchable: true", "fetchable: false"))
    git(research_wiki, "add", "-A")
    git(research_wiki, "commit", "-m", "make the source unfetchable")
    with pytest.raises(RuntimeError, match="fetchable: true"):
        orch.caveats(["errors/ORA-12543.md"])


def test_caveats_skips_a_page_that_carries_no_research(research_wiki, orch,
                                                       monkeypatch):
    fake_web_answers(monkeypatch, {})
    start = head_sha(research_wiki)
    result = orch.caveats(["errors/ORA-12543.md"])
    assert result["summary"] == "caveats: 0 page(s), 0 note(s), 1 failed"
    assert any("no research on errors/ORA-12543.md" in f
               for f in result["flags"])
    assert subjects_since(research_wiki, start) == []
    assert porcelain(research_wiki) == ""
