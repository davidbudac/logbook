"""Langfuse export (observability.py): off by default, inert without the
package, prompt/result exported as observation input/output, and never a
failure mode. The langfuse SDK itself is stubbed — these tests verify what
we hand it, not the SDK."""

import hashlib
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from dbwiki import observability


@pytest.fixture(autouse=True)
def reset_module_state():
    from dbwiki import promptreg
    promptreg._cache = {}
    observability._client = None
    observability._client_failed = False
    observability._warned = set()
    observability._release_sha = None
    observability._release_read = False
    yield
    promptreg._cache = {}
    observability._release_read = False
    observability._client = None
    observability._client_failed = False
    observability._warned = set()


class StubObservation:
    def __init__(self, calls, **kwargs):
        self.kwargs = kwargs
        self.calls = calls
        self.updates = []
        self.scores = []
        self.children = []
        self.ended = False

    def start_observation(self, **kwargs):
        child = StubObservation(self.calls, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kw):
        self.updates.append(kw)
        return self

    def score_trace(self, **kw):
        self.scores.append(kw)

    def end(self, **kw):
        self.ended = True
        return self


class StubPrompt:
    def __init__(self, name, prompt):
        self.name, self.prompt = name, prompt


class StubClient:
    def __init__(self, **kwargs):
        self.init_kwargs = kwargs
        self.observations = []
        self.flushed = 0
        self.created_prompts = []

    def start_observation(self, **kwargs):
        obs = StubObservation(self, **kwargs)
        self.observations.append(obs)
        return obs

    def get_prompt(self, name, **kwargs):
        raise ValueError("no such prompt")

    def create_prompt(self, *, name, prompt, **kwargs):
        self.created_prompts.append(name)
        return StubPrompt(name, prompt)

    def flush(self):
        self.flushed += 1


@pytest.fixture
def stub_langfuse(monkeypatch):
    """Install a fake `langfuse` module and hand back its created clients."""
    clients = []
    mod = ModuleType("langfuse")

    def make_client(**kwargs):
        c = StubClient(**kwargs)
        clients.append(c)
        return c

    propagated = []

    @contextmanager
    def propagate_attributes(**kwargs):
        propagated.append(kwargs)
        yield

    mod.Langfuse = make_client
    mod.propagate_attributes = propagate_attributes
    monkeypatch.setitem(sys.modules, "langfuse", mod)
    return SimpleNamespace(clients=clients, propagated=propagated)


def cfg(**lf):
    return SimpleNamespace(langfuse=lf)


def fields(**over):
    base = {"run_id": "r1", "task": "ingest", "adapter": "claude",
            "model": "sonnet", "model_tier": "cheap", "duration_s": 12.5,
            "timed_out": False, "attempts": 1, "validation_ok": True,
            "rolled_back": False, "lint_findings": 0, "pages_touched": 2,
            "incidents_opened": 0, "incidents_updated": 1,
            "usage": {"input_tokens": 100, "output_tokens": 20,
                      "cost_usd": 0.01}}
    base.update(over)
    return base


def test_disabled_or_absent_block_does_nothing(stub_langfuse):
    observability.record_agent_run(cfg(), fields())
    observability.record_agent_run(cfg(enabled=False), fields())
    observability.record_agent_run(SimpleNamespace(), fields())  # bare double
    assert stub_langfuse.clients == []


def test_missing_package_warns_once_never_raises(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "langfuse", None)  # import -> ImportError
    observability.record_agent_run(cfg(enabled=True), fields())
    observability.record_agent_run(cfg(enabled=True), fields())
    err = capsys.readouterr().err
    assert err.count("langfuse export") == 1
    assert "not installed" in err


def test_exports_full_trace(stub_langfuse):
    observability.record_agent_run(
        cfg(enabled=True, host="http://lf:3000"), fields(),
        {"exit_code": 0, "stdout_bytes": 40, "prompt_bytes": 300},
        db="cdb1", mode="agentic",
        prompt="the prompt", result={"summary": "s"})
    (client,) = stub_langfuse.clients
    assert client.init_kwargs["host"] == "http://lf:3000"
    (obs,) = client.observations
    assert obs.kwargs["input"] == "the prompt"
    assert obs.updates == [{"output": {"summary": "s"}}]
    assert obs.kwargs["name"] == "ingest"
    assert obs.kwargs["as_type"] == "generation"
    assert obs.kwargs["model"] == "sonnet"
    assert obs.kwargs["usage_details"] == {"input": 100, "output": 20}
    assert obs.kwargs["cost_details"] == {"total": 0.01}
    meta = obs.kwargs["metadata"]
    assert meta["db"] == "cdb1" and meta["mode"] == "agentic"
    assert meta["run_id"] == "r1" and meta["prompt_bytes"] == 300
    assert obs.ended and client.flushed == 1
    (attrs,) = stub_langfuse.propagated
    assert attrs["session_id"] == "r1"
    assert attrs["trace_name"] == "dbwiki-ingest"
    assert set(attrs["tags"]) == {"ingest", "claude", "cheap", "agentic"}
    scores = {s["name"]: s["value"] for s in obs.scores}
    assert scores == {"validation_ok": 1.0, "rolled_back": 0.0,
                      "lint_findings": 0.0, "duration_s": 12.5}


def test_the_ledger_event_id_seeds_the_trace_id(stub_langfuse):
    observability.record_agent_run(cfg(enabled=True), fields(),
                                   event_id="c70a15098407")
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.kwargs["trace_context"] == {
        "trace_id": observability.trace_id("c70a15098407")}
    assert observability.trace_id("c70a15098407") \
        == hashlib.sha256(b"c70a15098407").hexdigest()[:32]


def test_without_an_event_id_the_sdk_picks_the_trace_id(stub_langfuse):
    observability.record_agent_run(cfg(enabled=True), fields())
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.kwargs["trace_context"] is None


def test_absent_prompt_and_result_export_as_empty(stub_langfuse):
    # structured research passes no prompt; failures may have no result —
    # the observation still lands, just without input/output
    observability.record_agent_run(cfg(enabled=True), fields())
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.kwargs["input"] is None
    assert obs.updates == []


def test_release_and_environment_reach_the_trace(stub_langfuse, monkeypatch):
    monkeypatch.setattr(observability, "_release_sha", "deadbee")
    monkeypatch.setattr(observability, "_release_read", True)
    observability.record_agent_run(cfg(enabled=True, environment="onprem"),
                                   fields())
    # release is a client-level constant in SDK 4.x; environment rides both
    (client,) = stub_langfuse.clients
    assert client.init_kwargs["release"] == "deadbee"
    assert client.init_kwargs["environment"] == "onprem"
    (attrs,) = stub_langfuse.propagated
    assert attrs["environment"] == "onprem"


def test_release_is_the_app_sha_read_once(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw["cwd"]))
        return SimpleNamespace(returncode=0, stdout="abc1234\n")

    monkeypatch.setattr(observability.subprocess, "run", fake_run)
    assert observability._release() == "abc1234"
    assert observability._release() == "abc1234"
    assert len(calls) == 1
    cmd, cwd = calls[0]
    assert cmd == ["git", "rev-parse", "--short", "HEAD"]
    assert cwd.name == "dbwiki"


def test_release_absent_outside_a_checkout(monkeypatch, stub_langfuse):
    monkeypatch.setattr(observability.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(returncode=128,
                                                         stdout=""))
    observability.record_agent_run(cfg(enabled=True), fields())
    assert stub_langfuse.clients[0].init_kwargs["release"] is None


def test_generation_links_the_prompt_version(stub_langfuse):
    observability.record_agent_run(cfg(enabled=True), fields(),
                                   mode="structured", prompt="the prompt")
    client = stub_langfuse.clients[0]
    assert client.created_prompts == ["dbwiki/ingest-structured"]
    (obs,) = client.observations
    assert obs.kwargs["prompt"].name == "dbwiki/ingest-structured"


def test_stage_without_a_static_block_links_no_prompt(stub_langfuse):
    # research offload has no static block, and this cfg double has no wiki
    observability.record_agent_run(cfg(enabled=True),
                                   fields(task="research"), mode="offload")
    client = stub_langfuse.clients[0]
    assert client.created_prompts == []
    assert client.observations[0].kwargs["prompt"] is None


# the harness owns this shape (docs/langfuse.md); the integration
# between the two is by contract, so these steps are hand-built
def steps(n=2):
    return [{"seq": 1, "kind": "assistant", "name": "turn 1",
             "preview": "thinking about the digest",
             "usage": {"input_tokens": 900, "output_tokens": 30}},
            {"seq": 2, "kind": "tool", "name": "Read",
             "preview": "wiki/errors/ORA-00600.md", "usage": None}][:n]


def test_steps_become_child_observations(stub_langfuse):
    observability.record_agent_run(cfg(enabled=True), fields(),
                                   {"steps": steps()})
    (obs,) = stub_langfuse.clients[0].observations
    turn, tool = obs.children
    assert turn.kwargs["name"] == "turn 1"
    assert turn.kwargs["as_type"] == "generation"
    assert turn.kwargs["input"] == "thinking about the digest"
    assert turn.kwargs["usage_details"] == {"input": 900, "output": 30}
    assert tool.kwargs["name"] == "Read"
    assert tool.kwargs["as_type"] == "tool"
    assert tool.kwargs["usage_details"] is None
    assert turn.ended and tool.ended


def test_truncated_step_list_says_so_on_the_parent(stub_langfuse):
    observability.record_agent_run(
        cfg(enabled=True), fields(),
        {"steps": steps(1), "steps_truncated": True})
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.kwargs["metadata"]["steps_truncated"] is True
    assert len(obs.children) == 1


def test_no_steps_leaves_the_trace_flat(stub_langfuse):
    observability.record_agent_run(cfg(enabled=True), fields(), {})
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.children == []
    assert "steps_truncated" not in obs.kwargs["metadata"]


def test_broken_steps_cost_the_children_only(stub_langfuse, capsys):
    observability.record_agent_run(cfg(enabled=True), fields(),
                                   {"steps": ["not a dict", {"kind": "tool"}]})
    (obs,) = stub_langfuse.clients[0].observations
    assert [c.kwargs["name"] for c in obs.children] == ["tool"]
    assert {s["name"] for s in obs.scores} == {
        "validation_ok", "rolled_back", "lint_findings", "duration_s"}
    assert capsys.readouterr().err == ""


def test_child_explosion_warns_once_and_keeps_the_scores(stub_langfuse,
                                                         monkeypatch, capsys):
    def boom(self, **kwargs):
        raise RuntimeError("child boom")

    monkeypatch.setattr(StubObservation, "start_observation", boom)
    observability.record_agent_run(cfg(enabled=True), fields(),
                                   {"steps": steps()})
    (obs,) = stub_langfuse.clients[0].observations
    assert len(obs.scores) == 4 and obs.ended
    err = capsys.readouterr().err
    assert err.count("langfuse export") == 1 and "child boom" in err


def test_failure_marks_level_error(stub_langfuse):
    observability.record_agent_run(
        cfg(enabled=True),
        fields(validation_ok=False, rolled_back=True, usage="unknown"))
    (obs,) = stub_langfuse.clients[0].observations
    assert obs.kwargs["level"] == "ERROR"
    assert obs.kwargs["status_message"] == "rolled back"
    # "unknown" usage is omitted, never estimated
    assert obs.kwargs["usage_details"] is None
    assert obs.kwargs["cost_details"] is None


def test_client_reused_across_runs(stub_langfuse):
    c = cfg(enabled=True)
    observability.record_agent_run(c, fields())
    observability.record_agent_run(c, fields(task="report"))
    assert len(stub_langfuse.clients) == 1
    assert len(stub_langfuse.clients[0].observations) == 2


def test_sdk_explosion_is_swallowed(stub_langfuse, capsys):
    class Boom(StubClient):
        def start_observation(self, **kwargs):
            raise RuntimeError("boom")

    stub_langfuse.clients  # keep fixture; replace factory with a bomb
    sys.modules["langfuse"].Langfuse = lambda **kw: Boom(**kw)
    observability.record_agent_run(cfg(enabled=True), fields())
    err = capsys.readouterr().err
    assert "langfuse export" in err and "boom" in err
