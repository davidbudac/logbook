"""gitutil robustness (issue 22): every call is bounded by a timeout, and
output that is not UTF-8 is either carried through (`changed_paths`) or a
RuntimeError — never an exception type no caller catches.

A real non-UTF-8 file name cannot be created on APFS, so the git output is
faked at `subprocess.run`; the rest runs against a real repository."""

import subprocess

import pytest

from dbwiki import gitutil


def fake_run(monkeypatch, stdout: bytes, returncode: int = 0, seen=None):
    def _run(cmd, **kw):
        if seen is not None:
            seen.append(kw)
        return subprocess.CompletedProcess(cmd, returncode, stdout, b"boom")
    monkeypatch.setattr(gitutil.subprocess, "run", _run)


def test_git_output_that_is_not_utf8_is_a_runtime_error(monkeypatch, tmp_path):
    fake_run(monkeypatch, b"caf\xe9.md\0")
    with pytest.raises(RuntimeError, match="not UTF-8"):
        gitutil.git(tmp_path, "ls-files", "-z")


def test_changed_paths_carries_a_non_utf8_name_through(monkeypatch, tmp_path):
    fake_run(monkeypatch, b"?? caf\xe9.md\0 M log.md\0")
    paths = gitutil.changed_paths(tmp_path)
    assert paths == ("caf\udce9.md", "log.md")
    # and the name round-trips to the bytes the filesystem holds
    assert paths[0].encode(errors="surrogateescape") == b"caf\xe9.md"


def test_git_bytes_is_bounded_by_the_default_timeout(monkeypatch, tmp_path):
    seen = []
    fake_run(monkeypatch, b"", seen=seen)
    gitutil.git_bytes(tmp_path, "cat-file", "--batch", input=b"")
    assert seen[0]["timeout"] == gitutil.TIMEOUT_S


def test_a_hung_git_bytes_call_is_a_runtime_error(monkeypatch, tmp_path):
    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(gitutil.subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="no answer after"):
        gitutil.git_bytes(tmp_path, "show", "HEAD:x.md")


def test_git_keeps_text_mode_behaviour(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    gitutil.git(repo, "init", "-q")
    (repo / "a.md").write_bytes(b"one\r\ntwo\n")
    assert gitutil.git(repo, "status", "--porcelain") == "?? a.md\n"
    assert gitutil.git(repo, "rev-parse", "--verify", "nope",
                       check=False) == ""
    with pytest.raises(RuntimeError, match="rev-parse"):
        gitutil.git(repo, "rev-parse", "--verify", "nope")
    gitutil.git(repo, "add", "a.md")
    # universal newlines, as text-mode subprocess gave
    assert gitutil.git(repo, "show", ":a.md") == "one\ntwo\n"
    assert gitutil.git_bytes(repo, "show", ":a.md") == b"one\r\ntwo\n"
