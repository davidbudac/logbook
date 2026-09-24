"""`deploy/serve_wiki_html.sh` at boot (issue 24).

The unit starts before tailscaled has an address, and the script used to fall
back to 127.0.0.1 when `tailscale ip` failed, serving where no tailscale peer
could ever reach it until someone restarted it by hand. It now waits for the
address and fails loudly when none comes. `tailscale` and `python3` are
fakes on PATH, so nothing is served and nothing is asked of a real tailnet.
"""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "serve_wiki_html.sh"
TAILNET_IP = "100.64.0.7"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    """A checkout holding the script and a `wiki/html`, and a bin directory
    whose `tailscale` answers after `ready_after` failed calls (a file
    holds the count) and whose `python3` prints its argv instead of
    serving."""
    (tmp_path / "deploy").mkdir()
    shutil.copy(SCRIPT, tmp_path / "deploy" / SCRIPT.name)
    (tmp_path / "wiki" / "html").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "tailscale-calls"
    _exe(bin_dir / "tailscale",
         f'n=$(cat "{calls}" 2>/dev/null || echo 0); n=$((n + 1))\n'
         f'echo "$n" > "{calls}"\n'
         f'[ "$n" -gt "${{READY_AFTER:-0}}" ] || exit 1\n'
         f'echo {TAILNET_IP}\n')
    _exe(bin_dir / "python3", 'echo "python3 $*"\n')

    def run(*args, ready_after=0, tries=5, **env):
        result = subprocess.run(
            ["bash", str(tmp_path / "deploy" / SCRIPT.name), *args],
            capture_output=True, text=True, timeout=30,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin",
                 "READY_AFTER": str(ready_after),
                 "DBWIKI_HTML_WAIT_TRIES": str(tries),
                 "DBWIKI_HTML_WAIT_S": "0", **env})
        count = int(calls.read_text()) if calls.exists() else 0
        return result, count

    return run


def test_it_waits_for_a_tailscale_address_that_arrives_late(box):
    result, calls = box(ready_after=3)
    assert result.returncode == 0, result.stderr
    assert calls == 4
    assert f"--bind {TAILNET_IP}" in result.stdout
    assert f"http://{TAILNET_IP}:8766/" in result.stdout


def test_no_address_is_a_loud_failure_and_never_a_loopback_server(box):
    result, calls = box(ready_after=99, tries=3)
    assert result.returncode == 1
    assert calls == 3
    assert "no tailscale IPv4 after 3 tries" in result.stderr
    assert "python3" not in result.stdout, "nothing was served"


def test_an_explicit_bind_never_asks_tailscale(box):
    result, calls = box("127.0.0.1", "9000")
    assert result.returncode == 0, result.stderr
    assert calls == 0
    assert "python3 -m http.server 9000 --bind 127.0.0.1" in result.stdout

