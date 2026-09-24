"""Suite-wide guard: no test may open a socket to a live dependency.

`dbwiki health` probes the local model server and Elasticsearch, and a test
that reaches either passes or fails by what happens to be running on the
developer's machine — `test_missing_wiki_is_a_reported_category_not_a_crash`
was connecting to localhost:1234 on every run until 2026-08. Loopback in
general stays allowed: the webhook-sink tests serve a real HTTP server on
127.0.0.1.

The same reasoning covers the agent CLIs (`no_real_agent_cli` below) and
pi's provider table: the probe reads `base_url` and
the API key out of the developer's own `~/.pi/agent/models.json`, so every
test gets a path that does not exist instead.
"""

import inspect
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from dbwiki import health

#: The model servers (LM Studio 1234, unsloth studio 8888), Elasticsearch,
#: and the incident workbench (8765): a test that forgets get_json= or binds a
#: fixed port must never reach a developer's live server.
#: Only these — the guard is a tripwire for an un-faked dependency probe, not
#: a general network ban.
FORBIDDEN_PORTS = {1234, 8888, 9200, 8765}


@pytest.fixture(autouse=True)
def no_live_dependency_sockets(monkeypatch):
    connect = socket.socket.connect

    def guarded(self, address):
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        if port in FORBIDDEN_PORTS:
            raise AssertionError(
                f"a test tried to connect to {address}: the model-server "
                f"probe and Elasticsearch must be faked (pass get_json=/es=), "
                f"never reached")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def no_developer_pi_provider_table(monkeypatch, tmp_path):
    """The health probe resolves base URL and API key from pi's provider
    table. Point it at nothing so no test inherits the developer's."""
    monkeypatch.setattr(health, "PI_MODELS_JSON", tmp_path / "no-pi-models.json")


#: The executables an agent run spawns (`harness._codex_cmd`, `_claude_cmd`,
#: `_pi_cmd`, `_pi_text_cmd`, `_claude_web_cmd`), plus `ollama` and LM
#: Studio's `lms` for the local model servers. Matched on argv[0]'s basename.
AGENT_CLIS = frozenset({"claude", "codex", "ollama", "pi", "lms"})

_POPEN_INIT = subprocess.Popen.__init__
_POPEN_SIG = inspect.signature(_POPEN_INIT)


def _under_tmp(path: str) -> bool:
    resolved = Path(path).resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    return tmp in resolved.parents


def _program(bound: inspect.BoundArguments) -> str:
    """The program a Popen call would exec: `executable=` when given, else
    argv[0], else the first word of a `shell=True` command string."""
    argv = bound.arguments.get("args")
    shell = bound.arguments.get("shell", False)
    exe = bound.arguments.get("executable")
    if exe is not None and not shell:
        return os.fsdecode(exe)
    if isinstance(argv, (str, bytes, os.PathLike)):
        words = os.fsdecode(argv).split() if shell else [os.fsdecode(argv)]
        return words[0] if words else ""
    argv = list(argv or ())
    return os.fsdecode(argv[0]) if argv else ""


@pytest.fixture(autouse=True)
def no_real_agent_cli(monkeypatch):
    """No test may start a real agent CLI. One test once reached the real
    `codex exec` (a paid, minutes-long run that edits a wiki) because its
    fake sat on the wrong seam.

    How it decides: every process start in Python goes through
    `subprocess.Popen.__init__` (`subprocess.run`, `check_output`,
    `harness._spawn`, asyncio's subprocess transport), so the guard wraps
    that one method. It takes the program to be exec'd (`executable=`, else
    argv[0], else the first word of a `shell=True` string) and refuses when
    its basename is in `AGENT_CLIS`, unless the program, resolved through the
    child's PATH the way exec would, lies under the temp directory. That is
    where a test puts a deliberate stand-in script (tmp_path is under it), so
    a fake `codex` in tmp_path still runs. `git`, `sh`, `sys.executable` and
    every other program pass untouched, and so does an agent name later in
    argv (`sh -c "... codex ..."` is a test script, not a CLI start).

    Tests that fake the spawn by monkeypatching `harness._spawn`, `_run`,
    `run_text` or a module's `subprocess.run`/`Popen` replace a layer above
    this one and never reach it, so they keep working unchanged."""
    def guarded(self, *args, **kwargs):
        try:
            bound = _POPEN_SIG.bind(self, *args, **kwargs)
        except TypeError:  # let Popen raise its own error
            return _POPEN_INIT(self, *args, **kwargs)
        prog = _program(bound)
        if Path(prog).name in AGENT_CLIS:
            env = bound.arguments.get("env") or os.environ
            found = (prog if os.sep in prog
                     else shutil.which(prog, path=env.get("PATH", os.defpath)))
            if not (found and _under_tmp(found)):
                raise AssertionError(
                    f"a test tried to start the real agent CLI {prog!r} "
                    f"(resolves to {found!r}): fake the spawn (monkeypatch "
                    f"harness._spawn, harness._run or harness.run_text) or "
                    f"point it at a stand-in script under tmp_path")
        return _POPEN_INIT(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded)
