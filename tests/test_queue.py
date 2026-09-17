"""ADR-0001 queue: enqueue/claim/supersede/complete/fail/fold-in over a plain
wiki-repo path. `test_claim_push_lock_race...` proves the claim-vs-claim race
(two analysts, one request) resolves to exactly one winner even though the
two commits are a genuine same-path content conflict a plain `git pull
--rebase` cannot auto-resolve — see the module docstring in `queue.py`."""

import json
import subprocess
from pathlib import Path

import pytest

from dbwiki import queue
from dbwiki.lock import Held

WINDOW = ("2026-08-05T18:00:00Z", "2026-08-05T20:15:00Z")
LOCK = Held(Path("/nonexistent"), "test", 0.0)


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check,
                          capture_output=True, text=True)


def init_wiki(tmp_path: Path, name: str = "wiki") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@test")
    git(repo, "config", "user.name", "test")
    (repo / "log.md").write_text("# log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def wiki(tmp_path):
    return init_wiki(tmp_path)


def enqueue(wiki, run_id, day, suffix="", prompt="the prompt", notable_dbs=()):
    return queue.enqueue_request(
        wiki, run_id=run_id, kind="report", day=day, suffix=suffix,
        window=WINDOW, notable_dbs=list(notable_dbs), prompt=prompt,
        push=False, lock=LOCK)


def head_message(repo) -> str:
    return git(repo, "log", "-1", "--format=%B").stdout


# ---- enqueue -------------------------------------------------------------------

def test_enqueue_writes_pending_and_commits_with_run_id_trailer(wiki):
    sha = enqueue(wiki, "r1", "2026-08-05", suffix="-1815")
    assert sha
    files = list((wiki / "queue" / "pending").glob("*.json"))
    assert len(files) == 1
    rec = json.loads(files[0].read_text())
    assert rec == {
        "schema_version": 1, "kind": "report", "run_id": "r1",
        "day": "2026-08-05", "suffix": "-1815",
        "window": {"from": WINDOW[0], "to": WINDOW[1]}, "notable_dbs": [],
        "created_at": rec["created_at"], "attempts": 0, "prompt": "the prompt"}
    assert "Run-ID: r1" in head_message(wiki)


def test_enqueue_supersedes_older_pending_same_day(wiki):
    enqueue(wiki, "r1", "2026-08-05", suffix="-1815")
    enqueue(wiki, "r2", "2026-08-05", suffix="-2015")
    files = list((wiki / "queue" / "pending").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["run_id"] == "r2"
    assert "supersedes 1 pending" in head_message(wiki)


def test_enqueue_leaves_a_different_day_or_kind_alone(wiki):
    enqueue(wiki, "r1", "2026-08-04")
    enqueue(wiki, "r2", "2026-08-05")
    assert len(list((wiki / "queue" / "pending").glob("*.json"))) == 2


# ---- claim -----------------------------------------------------------------------

def test_claim_moves_pending_to_claimed_and_stamps(wiki):
    enqueue(wiki, "r1", "2026-08-05", notable_dbs=["cdb1"])
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    assert claimed["run_id"] == "r1"
    assert claimed["claimed_by"] == "analyst-host"
    assert claimed["claimed_at"]
    assert claimed["prompt"] == "the prompt"
    assert not list((wiki / "queue" / "pending").glob("*.json"))
    assert (wiki / "queue" / "claimed" / claimed["_file"]).exists()
    assert "queue: claim report 2026-08-05" in head_message(wiki)


def test_claim_returns_none_when_nothing_pending(wiki):
    assert queue.claim(wiki, "analyst-host", lock=LOCK, push=False) is None


def test_claim_filters_by_kind(wiki):
    enqueue(wiki, "r1", "2026-08-05")
    assert queue.claim(wiki, "analyst-host", lock=LOCK, kind="lint", push=False) is None
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, kind="report", push=False)
    assert claimed["run_id"] == "r1"


def test_claim_supersedes_an_older_pending_duplicate_in_the_claim_commit(wiki):
    enqueue(wiki, "r1", "2026-08-05", suffix="-1815")
    # a stale pending duplicate for the same day, as if an earlier enqueue's
    # own supersede-delete had not yet landed
    stale = wiki / "queue" / "pending" / "report-2026-08-05-1015-r0.json"
    stale.write_text(json.dumps({
        "schema_version": 1, "kind": "report", "run_id": "r0",
        "day": "2026-08-05", "suffix": "-1015",
        "window": {"from": WINDOW[0], "to": WINDOW[1]}, "notable_dbs": [],
        "created_at": "2026-08-05T10:16:00Z", "attempts": 0, "prompt": "p0"}))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "seed a stale pending duplicate")
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    assert claimed["run_id"] == "r1"
    assert not stale.exists()


@pytest.fixture
def ticking_clock(monkeypatch):
    """`_now()` advancing one second per call. Real `_now()` has one-second
    resolution, so back-to-back enqueues in a test usually share a timestamp
    — which silently ties every created_at comparison and lets a filename
    sort decide the outcome instead. Ordering tests must pin the clock or
    they assert nothing (and then fail on whichever CI runner is slow enough
    to cross a second boundary)."""
    stamps = iter(f"2026-08-09T12:00:{s:02d}Z" for s in range(60))
    monkeypatch.setattr(queue, "_now", lambda: next(stamps))


def test_claim_oldest_day_first_across_a_multi_day_backlog(wiki, ticking_clock):
    # the newer day is enqueued FIRST, so enqueue order and day order disagree
    # — an analyst node coming back to a backlog built while it was down
    enqueue(wiki, "r2", "2026-08-06")
    enqueue(wiki, "r1", "2026-08-05")
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    assert claimed["day"] == "2026-08-05"
    assert queue.claim(wiki, "analyst-host", lock=LOCK, push=False)["day"] == "2026-08-06"


def test_claim_day_order_holds_whatever_the_enqueue_order(wiki, ticking_clock):
    for run_id, day in [("r3", "2026-08-07"), ("r1", "2026-08-05"),
                        ("r4", "2026-08-08"), ("r2", "2026-08-06")]:
        enqueue(wiki, run_id, day)
    drained = []
    while (c := queue.claim(wiki, "analyst-host", lock=LOCK, push=False)):
        drained.append(c["day"])
    assert drained == ["2026-08-05", "2026-08-06", "2026-08-07", "2026-08-08"]


def test_supersede_within_a_day_still_takes_the_newest(wiki, ticking_clock):
    # day picks the group; created_at still picks the winner inside it
    enqueue(wiki, "old", "2026-08-05", suffix="-1015")
    enqueue(wiki, "new", "2026-08-05", suffix="-1815")
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    assert claimed["run_id"] == "new"
    assert queue.pending_requests(wiki) == []


# ---- listings ----------------------------------------------------------------

def test_pending_claimed_failed_listings(wiki):
    enqueue(wiki, "r1", "2026-08-05")
    assert len(queue.pending_requests(wiki)) == 1
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    assert queue.pending_requests(wiki) == []
    assert [c["_file"] for c in queue.claimed_requests(wiki)] == [claimed["_file"]]
    queue.fail_request(wiki, claimed, "agent_timeout", lock=LOCK, push=False)
    queue.fail_request(wiki, queue.claim(wiki, "analyst-host", lock=LOCK,
                                         push=False),
                       "agent_timeout", lock=LOCK, push=False)
    assert len(queue.failed_requests(wiki)) == 1
    assert queue.claimed_requests(wiki) == []


# ---- complete / fail -----------------------------------------------------------

def test_complete_commits_report_deletion_and_telemetry(wiki):
    enqueue(wiki, "r1", "2026-08-05")
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    (wiki / "reports").mkdir()
    (wiki / "reports" / "2026-08-05.md").write_text(
        "---\ntype: report\n---\n\n# r\n")
    sha = queue.complete(wiki, claimed, {"run_id": "r1", "task": "report",
                                        "validation_ok": True,
                                        "rolled_back": False},
                         lock=LOCK, push=False)
    assert sha
    assert not (wiki / "queue" / "claimed" / claimed["_file"]).exists()
    results = list((wiki / "queue" / "results").glob("*.json"))
    assert len(results) == 1
    rec = json.loads(results[0].read_text())
    assert rec["run_id"] == "r1" and rec["validation_ok"] is True and rec["event_id"]
    assert (wiki / "reports" / "2026-08-05.md").exists()
    assert "Run-ID: r1" in head_message(wiki)


def test_fail_request_requeues_below_max_attempts_without_a_telemetry_file(wiki):
    enqueue(wiki, "r1", "2026-08-05")
    claimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    updated = queue.fail_request(wiki, claimed, "agent_timeout", lock=LOCK,
                                 telemetry_record={"run_id": "r1"}, push=False)
    assert updated["attempts"] == 1
    assert updated["_terminal"] is False
    assert not (wiki / "queue" / "claimed" / claimed["_file"]).exists()
    pending = queue.pending_requests(wiki)
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["claimed_by"] is None and pending[0]["claimed_at"] is None
    # requeue is not yet a terminal fact — no telemetry event written
    assert not list((wiki / "queue" / "results").glob("*.json"))


def test_fail_request_moves_to_failed_with_category_after_max_attempts(wiki):
    enqueue(wiki, "r1", "2026-08-05")
    queue.fail_request(wiki, queue.claim(wiki, "analyst-host", lock=LOCK,
                                         push=False),
                       "agent_timeout", lock=LOCK, push=False)
    reclaimed = queue.claim(wiki, "analyst-host", lock=LOCK, push=False)
    updated = queue.fail_request(
        wiki, reclaimed, "validation_failed", lock=LOCK,
        telemetry_record={"run_id": "r1", "validation_ok": False}, push=False)
    assert updated["attempts"] == 2
    assert updated["_terminal"] is True
    failed = queue.failed_requests(wiki)
    assert len(failed) == 1
    assert failed[0]["error_category"] == "validation_failed"
    assert queue.pending_requests(wiki) == []
    results = list((wiki / "queue" / "results").glob("*.json"))
    assert len(results) == 1
    assert json.loads(results[0].read_text())["error_category"] == "validation_failed"


# ---- fold-in -----------------------------------------------------------------

def seed_result(wiki, run_id, event_id, **extra):
    results = wiki / "queue" / "results"
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{run_id}-{event_id}.json").write_text(json.dumps(
        {"run_id": run_id, "event_id": event_id, "task": "report", **extra}))
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", f"seed result {run_id}-{event_id}")


def test_fold_results_appends_deletes_and_commits(wiki, tmp_path):
    state_dir = tmp_path / "state"
    seed_result(wiki, "r1", "e1")
    n = queue.fold_results(wiki, state_dir, push=False)
    assert n == 1
    assert not list((wiki / "queue" / "results").glob("*.json"))
    lines = (state_dir / "agent_runs.jsonl").read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["run_id"] == "r1"
    assert "queue: fold 1 analyst result" in head_message(wiki)


def test_fold_results_is_idempotent_on_event_id(wiki, tmp_path):
    state_dir = tmp_path / "state"
    seed_result(wiki, "r1", "e1")
    assert queue.fold_results(wiki, state_dir, push=False) == 1
    # the same event resurfacing (a replayed/reset wiki branch resurrecting
    # a stale queue entry) must not double-count...
    seed_result(wiki, "r1", "e1")
    assert queue.fold_results(wiki, state_dir, push=False) == 0
    # ...but a *different* event sharing the run_id must fold: an analyst
    # result deliberately reuses the enqueuing tick's run_id, whose own
    # entries are already in agent_runs.jsonl — run_id is not the key
    seed_result(wiki, "r1", "e2")
    assert queue.fold_results(wiki, state_dir, push=False) == 1
    lines = (state_dir / "agent_runs.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_fold_results_quiet_when_queue_dir_absent(wiki, tmp_path):
    state_dir = tmp_path / "state"
    assert queue.fold_results(wiki, state_dir, push=False) == 0
    assert not state_dir.exists()


def test_fold_results_skips_a_corrupt_result_file(wiki, tmp_path):
    state_dir = tmp_path / "state"
    results = wiki / "queue" / "results"
    results.mkdir(parents=True)
    (results / "garbage.json").write_text("not json")
    git(wiki, "add", "-A")
    git(wiki, "commit", "-m", "seed a corrupt result")
    assert queue.fold_results(wiki, state_dir, push=False) == 0
    assert not (state_dir / "agent_runs.jsonl").exists()


# ---- the claim-vs-claim push-lock race ----------------------------------------

def test_claim_push_lock_race_resolves_to_exactly_one_winner(tmp_path, monkeypatch):
    """Two analysts race for the same request. Both build a local claim
    commit for `claimed/<file>` with different `claimed_by` content before
    either reaches the remote — a genuine same-path conflict. The test
    forces the interleaving deterministically (via a hook on the push call)
    rather than relying on OS thread scheduling."""
    bare = tmp_path / "wiki.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                   check=True, capture_output=True)
    origin = init_wiki(tmp_path, "origin")
    git(origin, "remote", "add", "origin", str(bare))
    git(origin, "push", "-u", "origin", "main")
    queue.enqueue_request(origin, run_id="r1", kind="report", day="2026-08-05",
                          suffix="", window=WINDOW, notable_dbs=[],
                          prompt="the prompt", push=True, lock=LOCK)

    clone_a = tmp_path / "clone-a"
    clone_b = tmp_path / "clone-b"
    for c in (clone_a, clone_b):
        subprocess.run(["git", "clone", str(bare), str(c)],
                       check=True, capture_output=True)
        git(c, "config", "user.email", "test@test")
        git(c, "config", "user.name", "test")

    real_git = queue._git
    state = {"triggered": False, "winner_a": "unset"}

    def hook(wiki_path, *args, check=True):
        if (wiki_path == clone_b and args and args[0] == "push"
                and not state["triggered"]):
            # right before clone_b's push: clone_b's own local claim commit
            # already exists. Let clone_a fully claim-and-push first, so
            # clone_b's push below hits a genuine rejection.
            state["triggered"] = True
            state["winner_a"] = queue.claim(clone_a, "analyst-a", lock=LOCK, push=True)
        return real_git(wiki_path, *args, check=check)

    monkeypatch.setattr(queue, "_git", hook)
    winner_b = queue.claim(clone_b, "analyst-b", lock=LOCK, push=True)

    assert state["winner_a"] is not None and state["winner_a"]["claimed_by"] == "analyst-a"
    assert winner_b is None  # clone_b detected it lost and backed off

    check = tmp_path / "checker"
    subprocess.run(["git", "clone", str(bare), str(check)],
                   check=True, capture_output=True)
    assert list((check / "queue" / "pending").glob("*.json")) == []
    claimed_files = list((check / "queue" / "claimed").glob("*.json"))
    assert len(claimed_files) == 1
    assert json.loads(claimed_files[0].read_text())["claimed_by"] == "analyst-a"
