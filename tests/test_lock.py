"""The single-flight lock: only one
wiki-mutating dbwiki command at a time, fail-fast by default, waiting on
request. No agent, no ES, no wiki — just the lock file and the CLI's handling
of a busy one."""

import datetime as dt
import json
import os
import threading
import time

import pytest
from fixtures import make_config

from dbwiki import cli
from dbwiki.health import HEALTH_LOG, categorize
from dbwiki.lock import LOCK_FILE, Holder, LockBusyError, single_flight

DAY = dt.date.today().isoformat()


# ---- the lock itself -----------------------------------------------------------

def test_an_uncontended_lock_is_immediate_and_names_its_holder(tmp_path):
    state = tmp_path / "state"           # created on demand
    with single_flight(state, "run") as lock:
        assert lock.lock_wait_s < 1
        held = json.loads((state / LOCK_FILE).read_text())
    assert held == {"pid": os.getpid(), "command": "run", "since": held["since"]}
    assert (state / LOCK_FILE).exists()  # never deleted; the file *is* the lock


def test_a_second_holder_fails_fast_and_says_who_is_holding_it(tmp_path):
    with single_flight(tmp_path, "run"):
        with pytest.raises(LockBusyError) as e:
            with single_flight(tmp_path, "research"):
                pytest.fail("the second holder must not get in")
    msg = str(e.value)
    assert "another dbwiki command holds the lock" in msg
    assert f"pid {os.getpid()}" in msg and "run" in msg


def test_a_busy_lock_carries_its_holder_as_fields_not_only_as_a_sentence(
        tmp_path):
    """A caller that needs the pid reads `holder.pid`; nobody parses the
    sentence back."""
    with single_flight(tmp_path, "run"):
        since = json.loads((tmp_path / LOCK_FILE).read_text())["since"]
        with pytest.raises(LockBusyError) as e:
            with single_flight(tmp_path, "research"):
                pass
    assert e.value.holder == Holder(pid=os.getpid(), command="run",
                                    since=since)
    assert str(e.value) == (f"another dbwiki command holds the lock "
                            f"(pid {os.getpid()}, run, since {since})")


def test_a_waiting_caller_acquires_once_the_holder_releases(tmp_path):
    """The waiter gets in only after the holder lets go — driven by an event,
    not by a sleep the waiter has to beat: a loaded runner must not be able to
    turn "it waited" into a flake."""
    held, release, acquired = (threading.Event() for _ in range(3))
    waiter_lock = []

    def holder():
        with single_flight(tmp_path, "run"):
            held.set()
            release.wait(10)

    def waiter():
        with single_flight(tmp_path, "research", wait_s=10) as lock:
            waiter_lock.append(lock)
        acquired.set()

    h = threading.Thread(target=holder)
    h.start()
    try:
        assert held.wait(2)
        with pytest.raises(LockBusyError):   # wait_s=0 while it is held
            with single_flight(tmp_path, "research"):
                pass
        w = threading.Thread(target=waiter)
        w.start()
        try:
            assert not acquired.wait(0.2)    # cannot get in while it is held
            release.set()
            assert acquired.wait(5)          # gets in once the holder lets go
        finally:
            w.join(5)
        assert waiter_lock[0].lock_wait_s >= 0
        assert json.loads((tmp_path / LOCK_FILE).read_text())["command"] \
            == "research"
    finally:
        release.set()
        h.join(5)


def test_the_lock_is_released_after_an_exception_inside_the_with(tmp_path):
    with pytest.raises(ZeroDivisionError):
        with single_flight(tmp_path, "run"):
            1 / 0
    with single_flight(tmp_path, "research") as lock:  # would raise if stranded
        assert lock.lock_wait_s < 1


def test_an_unreadable_lock_file_still_produces_a_busy_message(tmp_path):
    with single_flight(tmp_path, "run"):
        (tmp_path / LOCK_FILE).write_text("not json at all")
        with pytest.raises(LockBusyError, match="holds the lock") as e:
            with single_flight(tmp_path, "research"):
                pass
    assert e.value.holder is None
    assert str(e.value) == "another dbwiki command holds the lock"


def test_lock_busy_is_its_own_health_category(tmp_path):
    assert categorize(LockBusyError("busy")) == "lock_busy"


# ---- the CLI wiring ------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = make_config(tmp_path, wiki_repo=tmp_path / "wiki",
                    digest_dir=tmp_path / "wiki" / "digests",
                    state_dir=tmp_path / "state")
    monkeypatch.setattr(cli, "load_config", lambda: c)
    return c


def event(cfg) -> dict:
    return json.loads((cfg.state_dir / HEALTH_LOG).read_text().splitlines()[-1])


def test_a_busy_lock_fails_the_command_with_an_event_not_a_traceback(
        cfg, capsys):
    with single_flight(cfg.state_dir, "run"):
        assert cli.main(["ingest", "--db", "cdb1", "--date", DAY]) == 1
    err = capsys.readouterr().err
    assert "another dbwiki command holds the lock" in err and "run" in err
    ev = event(cfg)
    assert ev["command"] == "ingest"
    assert ev["outcome"] == "failed" and ev["error_category"] == "lock_busy"


def test_lock_wait_lets_a_cron_line_wait_for_the_holder(cfg, capsys):
    """`--lock-wait N` is what the crontab passes: the tick waits out a long
    agent instead of failing the way an interactive command should."""
    held = threading.Event()

    def holder():
        with single_flight(cfg.state_dir, "run"):
            held.set()
            time.sleep(0.3)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(2)
        # the digest does not exist, so the command fails *after* the lock —
        # which is the point: it waited, then got in
        assert cli.main(["ingest", "--db", "cdb1", "--date", DAY,
                         "--lock-wait", "5"]) == 1
        assert "digest not found" in capsys.readouterr().err
    finally:
        t.join(5)


def test_health_alert_is_locked_but_plain_health_is_not(cfg, monkeypatch,
                                                       capsys):
    """`health --alert` rewrites `.state/alerts.json` whole, so it must not
    run beside a tick's own dispatch. Plain `health` stays lock-free — reading
    health while a run holds the lock is exactly when it is wanted."""
    ran = []
    monkeypatch.setattr(cli, "cmd_health", lambda args: ran.append(args) or 0)
    with single_flight(cfg.state_dir, "run"):
        assert cli.main(["health", "--alert"]) == 1
        assert ran == []                       # never reached the command
        assert cli.main(["health"]) == 0       # unaffected
    assert "another dbwiki command holds the lock" in capsys.readouterr().err
    assert len(ran) == 1
    ev = event(cfg)
    assert ev["command"] == "health" and ev["error_category"] == "lock_busy"


def test_health_alert_accepts_lock_wait_like_every_locked_command(
        cfg, monkeypatch, capsys):
    """Issue 10 (python-workarounds): every command in `cli.LOCKED` declares
    `--lock-wait`, `health` included, so `_with_lock` reads the flag instead
    of guessing whether the command has one."""
    ran = []
    monkeypatch.setattr(cli, "cmd_health", lambda args: ran.append(args) or 0)
    assert cli.main(["health", "--alert", "--lock-wait", "5"]) == 0
    assert ran and ran[0].lock_wait == 5
    for name in cli.LOCKED:
        with pytest.raises(SystemExit) as exc:
            cli.main([name, "--help"])
        assert exc.value.code == 0
        assert "--lock-wait" in capsys.readouterr().out, name


def test_read_only_invocations_never_take_the_lock(cfg, capsys):
    with single_flight(cfg.state_dir, "run"):
        assert cli.main(["ingest", "--db", "cdb1", "--date", DAY,
                         "--dry-run"]) == 1
    assert "digest not found" in capsys.readouterr().err  # got past the lock


def test_the_env_var_supplies_the_default_wait(cfg, monkeypatch):
    monkeypatch.setenv("DBWIKI_LOCK_WAIT", "42")
    assert cli._lock_wait_default() == 42
    monkeypatch.setenv("DBWIKI_LOCK_WAIT", "not a number")
    assert cli._lock_wait_default() == 0
    monkeypatch.delenv("DBWIKI_LOCK_WAIT")
    assert cli._lock_wait_default() == 0
