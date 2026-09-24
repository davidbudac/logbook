"""`dbwiki review` at the CLI edge: the recorder, the failure path, the
lock-free contract, and the schedule mirror.

The review's own selection rules are pinned in `tests/test_review.py`. What is
under test here is the wiring: that `--explain` costs no model call and no
write, that a real run leaves one `review` run-health event carrying the
outcome counts, that a failure inside `review.run` becomes a category and an
exit code rather than a traceback, and that the command stays out of `LOCKED`.

The wiki is a real one-commit git repo because `review.run` pins a revision
through `transaction.head`; every date is relative to the clock the command
reads, since `cmd_review` derives `now` itself.
"""

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
from fixtures import make_config

from dbwiki import cli, review
from dbwiki.harness import HarnessError
from dbwiki.health import HEALTH_LOG

SCHEDULE = json.loads(
    (Path(__file__).resolve().parents[1] / "config" / "schedule.json").read_text())
SCHEDULING_MD = (Path(__file__).resolve().parents[1]
                 / "docs" / "scheduling.md").read_text()

DB = "cdb1"


def ago(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc)
            - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def wiki(tmp_path, monkeypatch):
    """One committed incident, open and untouched for 60 days: over the
    `stale_open` threshold and over the `unfollowed` one, so a run selects
    exactly two findings from one page."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    root = tmp_path / "wiki"
    page = root / "incidents" / "i-stale.md"
    page.parent.mkdir(parents=True)
    opened = ago(60)
    page.write_text(f"---\ntype: incident\nstatus: open\ndb: {DB}\n"
                    f"opened: {opened}\nupdated: {opened}\n---\n\n"
                    f"# i-stale on {DB}\n\nthe standby stopped applying redo.\n")
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "dba@example.com")
    git(root, "config", "user.name", "DBA")
    git(root, "add", "-A")
    git(root, "commit", "-m", "init")
    return root


@pytest.fixture
def cfg(tmp_path, wiki, monkeypatch):
    cfg = make_config(tmp_path, state_dir=tmp_path / ".state", wiki_repo=wiki,
                      agents={"pi": {"cheap": "gemma-3", "strong": "qwen"},
                              "timeout_seconds": 60})
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    return cfg


def fake_propose(monkeypatch) -> list[str]:
    calls: list[str] = []

    def propose(prompt, cfg, parse, **kw):
        calls.append(prompt)
        return {"summary": "one incident nobody has touched", "themes": (),
                "evidence_refs": ()}

    monkeypatch.setattr("dbwiki.structured._propose", propose)
    return calls


def event(cfg) -> dict:
    return json.loads((cfg.state_dir / HEALTH_LOG).read_text().splitlines()[-1])


def test_explain_prints_the_selection_and_touches_nothing(cfg, monkeypatch,
                                                          capsys):
    def refuse(*a, **k):
        raise AssertionError("--explain must not call the model")

    monkeypatch.setattr("dbwiki.structured._propose", refuse)
    assert cli.main(["review", "--explain"]) == 0
    out = capsys.readouterr().out
    assert "selected" in out and "stale_open" in out
    assert not (cfg.state_dir / review.REVIEW_DIR).exists()
    assert not (cfg.state_dir / HEALTH_LOG).exists()


def test_a_run_publishes_and_records_its_counts(cfg, monkeypatch, capsys):
    calls = fake_propose(monkeypatch)
    assert cli.main(["review"]) == 0
    assert len(calls) == 1
    review_id = review.iso_week(dt.datetime.now(dt.timezone.utc).isoformat())
    assert review.load_review(cfg.state_dir, review_id) is not None
    assert f"{review_id}: 2 selected, published, synthesis ok" \
        in capsys.readouterr().out
    ev = event(cfg)
    assert ev["command"] == "review" and ev["outcome"] == "ok"
    assert ev["facts"]["review_id"] == review_id
    assert ev["facts"]["published"] is True and ev["facts"]["selected"] == 2
    assert ev["facts"]["synthesis_ok"] is True


def test_a_second_run_in_the_same_week_publishes_nothing(cfg, monkeypatch,
                                                         capsys):
    calls = fake_propose(monkeypatch)
    assert cli.main(["review"]) == 0
    capsys.readouterr()
    assert cli.main(["review"]) == 0
    assert len(calls) == 1
    assert "not published (exists)" in capsys.readouterr().out
    assert event(cfg)["facts"]["skipped"] == "exists"


def test_a_failure_is_categorized_recorded_and_exits_one(cfg, monkeypatch,
                                                         capsys):
    def boom(*a, **k):
        raise HarnessError("the adapter never answered")

    monkeypatch.setattr(review, "run", boom)
    assert cli.main(["review"]) == 1
    assert "REVIEW FAILED harness_error: the adapter never answered" \
        in capsys.readouterr().err
    ev = event(cfg)
    assert ev["command"] == "review" and ev["outcome"] == "failed"
    assert ev["error_category"] == "harness_error"


def dead_relay(cfg, monkeypatch):
    from dbwiki import delivery

    def refuse(policy):
        raise OSError("connection refused")

    cfg.delivery = {"external_enabled": True, "smtp_host": "relay.example.com",
                    "from_address": "dbwiki@example.com",
                    "recipients": [{"id": "ops", "address": "ops@example.com",
                                    "channels": ["email"]}]}
    monkeypatch.setattr(delivery, "_open_smtp", refuse)


def test_a_failed_delivery_is_printed_and_exits_one(cfg, monkeypatch, capsys):
    """Published, but the email did not go: the operator (and whatever runs
    the command) must hear about it, with the command that retries it."""
    fake_propose(monkeypatch)
    dead_relay(cfg, monkeypatch)
    assert cli.main(["review"]) == 1
    captured = capsys.readouterr()
    review_id = review.iso_week(dt.datetime.now(dt.timezone.utc).isoformat())
    assert f"{review_id}: 2 selected, published, synthesis ok" in captured.out
    assert "1 delivery attempt(s) failed" in captured.err
    assert f"dbwiki review --deliver-only --review-id {review_id}" \
        in captured.err
    ev = event(cfg)
    assert ev["outcome"] == "ok" and ev["facts"]["failed"] == 1


def test_deliver_only_takes_a_review_id(cfg, monkeypatch, capsys):
    fake_propose(monkeypatch)
    dead_relay(cfg, monkeypatch)
    cli.main(["review"])
    review_id = review.iso_week(dt.datetime.now(dt.timezone.utc).isoformat())
    from dbwiki import delivery

    class Live:
        sent: list = []

        def __call__(self, policy):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_message(self, message):
            self.sent.append(message["To"])

    live = Live()
    monkeypatch.setattr(delivery, "_open_smtp", live)
    capsys.readouterr()
    assert cli.main(["review", "--deliver-only", "--review-id",
                     review_id]) == 0
    assert live.sent == ["ops@example.com"]


@pytest.mark.parametrize("argv,message", [
    (["review", "--review-id", "2026-W01"], "--deliver-only"),
    (["review", "--deliver-only", "--review-id", "../etc"], "not YYYY-Www"),
    (["review", "--deliver-only", "--review-id", "1999-W01"],
     "no published review"),
])
def test_a_bad_review_id_exits_two(cfg, argv, message, capsys):
    assert cli.main(argv) == 2
    assert message in capsys.readouterr().err
    assert not (cfg.state_dir / HEALTH_LOG).exists()


def test_review_takes_no_lock():
    """`review` reads the wiki at a pinned head and writes only
    `.state/review/`, so it must never enter `LOCKED` — an entry there would
    hold the single-flight lock for the whole stage, including its model call,
    and block every tick behind a review that needs no exclusion at all."""
    assert "review" not in cli.LOCKED


def schedule_entry(name: str) -> dict:
    return next(e for e in SCHEDULE["entries"] if e["entry"] == name)


def test_the_schedule_entry_matches_the_documented_crontab_line():
    entry = schedule_entry("review")
    assert entry["command"] == "dbwiki review" and entry["node"] == "onprem"
    assert f"{entry['cron']}    cd " in SCHEDULING_MD
    assert f"uv run {entry['command']} >> .state/cron.log" in SCHEDULING_MD


def test_the_review_entry_passes_no_lock_wait():
    """`--lock-wait` on a lock-free command would be a claim about it that is
    false; `docs/scheduling.md` says so in prose, this pins it."""
    assert "--lock-wait" not in schedule_entry("review")["command"]
