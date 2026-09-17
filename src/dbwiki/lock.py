"""Single-flight lock for the commands that mutate the wiki working tree.

Two dbwiki commands sharing one wiki tree destroy each other's work: a manual
`dbwiki research` once ran while a cron tick's report agent was still writing,
saw the report's uncommitted file as a foreign change, failed its own
path-allowlist validation and rolled the tree back — deleting the report out
from under the agent that was still writing it, and recording the failure
against the innocent run.

Deliberately boring: one `fcntl.flock` on `<state_dir>/orchestrator.lock`, held
for the whole command. flock dies with the process, so there is no timeout that
can strand a lock and no lock server to run. The file is never deleted — it is
the lock, and its contents ({pid, command, since}) exist only so the *other*
process can say who is holding it.

Waiting is the caller's choice: cron passes `--lock-wait 3600` (a tick that
waits out a long report is fine), while an interactive command fails fast by
default so the operator learns immediately that a run is already going.
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

LOCK_FILE = "orchestrator.lock"
POLL_S = 0.25  # retry interval while waiting; short enough to feel immediate


class LockBusyError(RuntimeError):
    """Another dbwiki command holds the wiki lock (health category
    `lock_busy`, deliberately not retryable: a rerun once the other command
    finishes is the operator's call, not the retry loop's)."""


def _holder(fh) -> str:
    """Who holds the lock, read back from the lock file. Diagnostics only, so
    an empty or half-written file degrades to the message without the names
    rather than to a second failure."""
    try:
        fh.seek(0)
        d = json.loads(fh.read() or "{}")
        return (f"another dbwiki command holds the lock "
                f"(pid {d['pid']}, {d['command']}, since {d['since']})")
    except Exception:  # noqa: BLE001 — never fail while reporting a failure
        return "another dbwiki command holds the lock"


@dataclass
class Held:
    """Proof that the caller is inside `single_flight`. `transaction.commit`
    takes one and refuses when `active` is False, so "the caller holds the
    lock" is a checked argument rather than a docstring promise. Never
    constructed by callers.

    `lock_wait_s` is the time spent waiting, which the `run` tick notes in its
    run-health event so ticks piling up behind a slow agent are visible."""

    state_dir: Path
    command: str
    lock_wait_s: float
    active: bool = True


@contextmanager
def single_flight(state_dir: Path, command: str, wait_s: float = 0):
    """Hold the wiki lock for the body, or raise LockBusyError.

    `wait_s` is how long to keep retrying before giving up (0 = fail fast).
    Yields a `Held` token; it is deactivated on exit.
    """
    path = Path(state_dir) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    fh = path.open("a+")
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                left = wait_s - (time.monotonic() - t0)
                if left <= 0:
                    raise LockBusyError(_holder(fh)) from None
                time.sleep(min(POLL_S, left))
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "command": command,
                             "since": datetime.now(timezone.utc)
                             .strftime("%Y-%m-%dT%H:%M:%SZ")}))
        fh.flush()
        held = Held(Path(state_dir), command, round(time.monotonic() - t0, 3))
        try:
            yield held
        finally:
            held.active = False
    finally:
        fh.close()  # closing the last fd on the open file releases the flock
