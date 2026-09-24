"""The on-prem pull before the analyst fold-in (ADR-0001, issue 15 item 5).

Nothing on the on-prem side used to pull: with `analyst.enabled` and
`report.push` both on, every on-prem push was rejected non-fast-forward after
the analyst's first push, and `fold_results` never saw `queue/results/`. The
tick now rebases onto the remote first, with the analyst's side winning a
same-path conflict. Real repositories throughout — a bare "remote", the
on-prem clone the tick runs in, an analyst clone — because the whole point
is what git does with them; the tick's ES and agent edges are
test_cli_run's fakes."""

import json
import subprocess
from pathlib import Path

import pytest
from test_cli_run import DAY, event, tick  # noqa: F401 — `tick` is a fixture

from dbwiki import cli, gitutil, queue
from dbwiki.lock import Held

LOCK = Held(Path("/nonexistent"), "test", 0.0)
WINDOW = (f"{DAY}T00:00:00Z", f"{DAY}T12:00:00Z")
REPORT = f"reports/{DAY}.md"


def git(repo, *args, check=True) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True).stdout


def clone(bare: Path, path: Path) -> Path:
    subprocess.run(["git", "clone", "-q", str(bare), str(path)], check=True,
                   capture_output=True)
    git(path, "config", "user.email", "test@test")
    git(path, "config", "user.name", "test")
    return path


def report_page(body: str) -> str:
    return f"---\ntype: report\n---\n\n# Report {DAY}\n\n{body}\n"


@pytest.fixture
def nodes(tmp_path):
    """(remote, onprem, analyst): the on-prem clone sits where the `tick`
    fixture's config expects the wiki, and holds one pushed escalated window
    — the structured placeholder report plus the queued request."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)],
                   check=True)
    onprem = clone(remote, tmp_path / "wiki")
    (onprem / "log.md").write_text("# log\n")
    (onprem / "reports").mkdir()
    (onprem / REPORT).write_text(report_page("structured placeholder"))
    (onprem / "html").mkdir()
    (onprem / "html" / f"{DAY}.html").write_text("<p>old</p>\n")
    git(onprem, "add", "-A")
    git(onprem, "commit", "-m", "init")
    git(onprem, "push", "-q", "-u", "origin", "main")
    queue.enqueue_request(onprem, run_id="r1", kind="report", day=DAY,
                          suffix="", window=WINDOW, notable_dbs=["db1"],
                          prompt="the prompt", lock=LOCK, push=True)
    return remote, onprem, clone(remote, tmp_path / "analyst")


def analyst_completes(analyst: Path, body: str = "agentic analysis") -> None:
    """The analyst node's whole round: claim, write the report, complete —
    each step pushed, as `dbwiki analyst` does."""
    request = queue.claim(analyst, "analyst-host", lock=LOCK, push=True)
    (analyst / REPORT).write_text(report_page(body))
    queue.complete(analyst, request, {"run_id": "r1", "task": "report",
                                      "validation_ok": True}, lock=LOCK,
                   push=True)


def onprem_commits_locally(onprem: Path, rel: str, text: str) -> None:
    """An on-prem commit the remote rejects: the analyst pushed first."""
    (onprem / rel).write_text(text)
    git(onprem, "add", "--", rel)
    git(onprem, "commit", "-q", "-m", f"on-prem: {rel}")
    assert not gitutil.push_best_effort(onprem)


def enable(tick, *, analyst: bool = True, push: bool = True):  # noqa: F811
    tick.analyst = {"enabled": analyst}
    tick.report = {"push": push}


def remote_file(remote: Path, rel: str) -> str:
    return git(remote, "show", f"main:{rel}")


# ---- the tick -----------------------------------------------------------------

def test_the_tick_pulls_the_analysts_result_folds_it_and_pushes_again(
        nodes, tick):  # noqa: F811
    remote, onprem, analyst = nodes
    analyst_completes(analyst)
    onprem_commits_locally(onprem, "log.md", "# log\n\n- on-prem entry\n")
    enable(tick)

    assert cli.main(["run"]) == 0

    facts = event(tick)["facts"]
    assert facts["analyst_pulled"] >= 1
    assert facts["analyst_results_folded"] == 1
    runs = [json.loads(line) for line in
            (tick.state_dir / "agent_runs.jsonl").read_text().splitlines()]
    assert [r["run_id"] for r in runs] == ["r1"]
    # the fold commit and the on-prem commit the remote used to reject both
    # reached the remote: the push is a fast-forward again
    assert git(onprem, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
    assert "on-prem entry" in remote_file(remote, "log.md")
    assert not git(remote, "ls-tree", "main", "queue/results/").strip()
    assert "agentic analysis" in remote_file(remote, REPORT)


def test_a_report_both_sides_edited_keeps_the_analysts_version(
        nodes, tick):  # noqa: F811
    remote, onprem, analyst = nodes
    analyst_completes(analyst, "agentic analysis")
    onprem_commits_locally(onprem, REPORT, report_page("a later placeholder"))
    enable(tick)

    assert cli.main(["run"]) == 0

    assert "agentic analysis" in (onprem / REPORT).read_text()
    assert "agentic analysis" in remote_file(remote, REPORT)
    assert git(onprem, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
    assert "analyst_pull_error" not in event(tick)["facts"]


def test_an_unreachable_remote_is_a_warning_and_the_tick_runs_on(
        nodes, tick, capsys):  # noqa: F811
    _, onprem, _ = nodes
    git(onprem, "remote", "set-url", "origin", str(onprem.parent / "gone.git"))
    (onprem / "html" / f"{DAY}.html").write_text("<p>this tick</p>\n")
    head = git(onprem, "rev-parse", "HEAD")
    enable(tick)

    assert cli.main(["run"]) == 0

    ev = event(tick)
    assert "gone.git" in ev["facts"]["analyst_pull_error"]
    assert ("render_html", DAY) in tick.calls
    assert "pull failed" in capsys.readouterr().err
    assert git(onprem, "rev-parse", "HEAD") == head
    assert (onprem / "html" / f"{DAY}.html").read_text() == "<p>this tick</p>\n"
    assert not gitutil.rebase_in_progress(onprem)


@pytest.mark.parametrize("analyst_on, push", [(False, True), (True, False)])
def test_no_pull_unless_the_analyst_is_on_and_the_tick_pushes(
        nodes, tick, monkeypatch, analyst_on, push):  # noqa: F811
    _, onprem, analyst = nodes
    analyst_completes(analyst)
    head = git(onprem, "rev-parse", "HEAD")
    monkeypatch.setattr(queue, "pull_before_fold", lambda *a, **k: pytest.fail(
        "pulled with the analyst off or push off"))
    enable(tick, analyst=analyst_on, push=push)

    assert cli.main(["run"]) == 0

    facts = event(tick)["facts"]
    assert "analyst_pulled" not in facts and "analyst_pull_error" not in facts
    assert git(onprem, "rev-parse", "HEAD") == head


# ---- the git mechanics --------------------------------------------------------

def test_minus_x_ours_in_a_rebase_keeps_the_upstream_side(nodes):
    """The naming inversion this whole change leans on, pinned against real
    git rather than assumed: while local commits are replayed onto upstream,
    upstream is "ours"."""
    _, onprem, analyst = nodes
    analyst_completes(analyst, "upstream wins")
    onprem_commits_locally(onprem, REPORT, report_page("local loses"))
    assert queue.pull_before_fold(onprem) >= 1
    assert "upstream wins" in (onprem / REPORT).read_text()


def test_uncommitted_machine_output_survives_the_pull(nodes):
    _, onprem, analyst = nodes
    analyst_completes(analyst)
    (onprem / "html" / f"{DAY}.html").write_text("<p>this tick</p>\n")
    (onprem / "digests").mkdir()
    (onprem / "digests" / "new.json").write_text("{}\n")
    assert queue.pull_before_fold(onprem) >= 1
    assert (onprem / "html" / f"{DAY}.html").read_text() == "<p>this tick</p>\n"
    assert (onprem / "digests" / "new.json").exists()
    assert list((onprem / "queue" / "results").glob("*.json"))
    assert not git(onprem, "stash", "list")


def test_nothing_to_pull_touches_nothing(nodes):
    _, onprem, _ = nodes
    (onprem / "html" / f"{DAY}.html").write_text("<p>this tick</p>\n")
    before = git(onprem, "status", "--porcelain")
    assert queue.pull_before_fold(onprem) == 0
    assert git(onprem, "status", "--porcelain") == before


def test_a_conflict_the_rebase_cannot_settle_is_aborted_and_undone(nodes):
    """-X ours settles content conflicts only; a modify/delete stops the
    rebase. It is aborted, and the tree is what it was."""
    _, onprem, analyst = nodes
    analyst_completes(analyst)
    git(onprem, "rm", "-q", "--", REPORT)
    git(onprem, "commit", "-q", "-m", "on-prem deletes the report")
    (onprem / "html" / f"{DAY}.html").write_text("<p>this tick</p>\n")
    head, status = git(onprem, "rev-parse", "HEAD"), git(onprem, "status",
                                                         "--porcelain")
    with pytest.raises(gitutil.PullFailed, match="pull undone"):
        queue.pull_before_fold(onprem)
    assert not gitutil.rebase_in_progress(onprem)
    assert git(onprem, "rev-parse", "HEAD") == head
    assert git(onprem, "status", "--porcelain") == status
    assert not git(onprem, "stash", "list")


def test_uncommitted_edits_the_pull_conflicts_with_are_put_back(nodes):
    """The rebase itself succeeds, but git cannot re-apply the autostash on
    top of it and keeps it in the stash list with the tree full of conflict
    markers. The pull is undone rather than left like that."""
    _, onprem, analyst = nodes
    analyst_completes(analyst)
    (onprem / REPORT).write_text(report_page("uncommitted on-prem edit"))
    head, status = git(onprem, "rev-parse", "HEAD"), git(onprem, "status",
                                                         "--porcelain")
    with pytest.raises(gitutil.PullFailed, match="uncommitted"):
        queue.pull_before_fold(onprem)
    assert git(onprem, "rev-parse", "HEAD") == head
    assert git(onprem, "status", "--porcelain") == status
    assert "uncommitted on-prem edit" in (onprem / REPORT).read_text()
    assert not git(onprem, "stash", "list")


def test_a_rebase_already_in_progress_is_left_alone(nodes):
    _, onprem, _ = nodes
    Path(git(onprem, "rev-parse", "--absolute-git-dir").strip(),
         "rebase-merge").mkdir()
    with pytest.raises(gitutil.PullFailed, match="already in progress"):
        queue.pull_before_fold(onprem)
    assert gitutil.rebase_in_progress(onprem)
