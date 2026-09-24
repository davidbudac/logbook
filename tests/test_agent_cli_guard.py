"""The conftest guard `no_real_agent_cli`: a real agent CLI never starts from
a test, a stand-in script under tmp_path does, and everything else passes."""

import subprocess
import sys

import pytest

from dbwiki import harness


@pytest.mark.parametrize("cmd", [
    ["codex", "exec", "--json", "hi"],
    ["claude", "-p", "hi"],
    ["pi", "-p", "hi"],
    ["ollama", "list"],
    ["lms", "ls"],
    ["/usr/local/bin/codex", "exec"],
])
def test_real_agent_cli_is_refused(cmd, tmp_path):
    with pytest.raises(AssertionError, match="real agent CLI"):
        subprocess.run(cmd, capture_output=True)


def test_shell_string_naming_an_agent_first_is_refused():
    with pytest.raises(AssertionError, match="real agent CLI"):
        subprocess.run("codex exec hi", shell=True, capture_output=True)


def test_harness_spawn_is_refused_before_anything_starts(tmp_path):
    with pytest.raises(AssertionError, match="real agent CLI"):
        harness._spawn(["codex", "exec", "hi"], cwd=tmp_path, timeout=5)


def test_stand_in_script_under_tmp_path_runs(tmp_path):
    fake = tmp_path / "codex"
    fake.write_text("#!/bin/sh\necho fake-codex \"$@\"\n")
    fake.chmod(0o755)
    by_path = subprocess.run([str(fake), "exec"], capture_output=True, text=True)
    assert by_path.stdout == "fake-codex exec\n"
    by_name = subprocess.run(["codex", "exec"], capture_output=True, text=True,
                             env={"PATH": str(tmp_path)})
    assert by_name.stdout == "fake-codex exec\n"


def test_other_programs_pass():
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0
    out = subprocess.run([sys.executable, "-c", "print('codex')"],
                         capture_output=True, text=True)
    assert out.stdout == "codex\n"
    assert subprocess.run("echo pi", shell=True, capture_output=True,
                          text=True).stdout == "pi\n"
