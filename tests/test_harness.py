"""Adapter command construction, the pi adapter's safety rails, and the
best-effort usage/telemetry capture (WS6)."""

import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from dbwiki import harness
from dbwiki.config import Config
from dbwiki.harness import (HarnessError, NoResultError, _claude_cmd,
                            _claude_web_cmd, _codex_cmd, _pi_cmd, _pi_text_cmd,
                            parse_usage, run_agent, run_text, run_web_text)

WIKI = Path("/tmp/wiki")


def test_pi_cmd_is_hermetic_and_bashless():
    cmd = _pi_cmd("do it", WIKI, "google/gemma-4-12b-qat", "lmstudio")
    assert cmd[0] == "pi"
    assert "-p" in cmd and "--no-session" in cmd
    assert "--no-extensions" in cmd and "--no-skills" in cmd
    tools = cmd[cmd.index("--tools") + 1]
    assert "bash" not in tools.split(",")
    assert {"read", "edit", "write"} <= set(tools.split(","))
    assert cmd[cmd.index("--provider") + 1] == "lmstudio"
    assert cmd[cmd.index("--model") + 1] == "google/gemma-4-12b-qat"
    assert cmd[-1] == "do it"


def test_pi_cmd_without_provider_or_model():
    cmd = _pi_cmd("p", WIKI, None, None)
    assert "--provider" not in cmd and "--model" not in cmd


def test_pi_cmd_web_false_is_the_legacy_hermetic_shape():
    cmd = _pi_cmd("do it", WIKI, "google/gemma-4-12b-qat", "lmstudio")
    assert "--no-extensions" in cmd
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert "web_search" not in tools and "fetch_content" not in tools


def test_pi_cmd_web_true_drops_no_extensions_and_grants_web_tools():
    cmd = _pi_cmd("do it", WIKI, "google/gemma-4-12b-qat", "lmstudio", web=True)
    assert cmd[0] == "pi"
    assert "-p" in cmd and "--no-session" in cmd and "--no-skills" in cmd
    # web=True lets pi discover the pi-web-access extension from the user's
    # own settings — no hardcoded extension path, so --no-extensions must
    # be absent rather than swapped for some explicit enable flag
    assert "--no-extensions" not in cmd
    tools = cmd[cmd.index("--tools") + 1].split(",")
    assert {"read", "edit", "write", "web_search", "fetch_content"} <= set(tools)
    assert "bash" not in tools
    assert cmd[cmd.index("--provider") + 1] == "lmstudio"
    assert cmd[cmd.index("--model") + 1] == "google/gemma-4-12b-qat"
    assert cmd[-1] == "do it"


def test_pi_adapter_grants_web_tools_instead_of_raising(monkeypatch, tmp_path):
    """pi is web-capable via the pi-web-access extension: run_agent must build
    the web-enabled command rather than refuse the run."""
    seen_cmd = {}

    def fake_run(cmd, *a, **kw):
        seen_cmd["cmd"] = cmd
        (tmp_path / harness.RESULT_FILE).write_text("{}")
        return proc(0, "")

    monkeypatch.setattr(harness, "_spawn", fake_run)
    assert run_agent("pi", "p", tmp_path, None, 5, web=True) == {}
    assert "--no-extensions" not in seen_cmd["cmd"]
    tools = seen_cmd["cmd"][seen_cmd["cmd"].index("--tools") + 1]
    assert "web_search" in tools and "fetch_content" in tools


def test_unknown_adapter_fails():
    with pytest.raises(HarnessError, match="unknown adapter"):
        run_agent("gpt", "p", WIKI, None, 5)


def test_removed_ollama_adapter_is_a_clear_error_naming_the_supported_ones(
        monkeypatch):
    def never(*a, **k):
        raise AssertionError("no agent CLI may be spawned")
    monkeypatch.setattr(harness, "_spawn", never)
    with pytest.raises(harness.UnknownAdapterError,
                       match="supported adapters: codex, claude, pi") as e:
        run_agent("ollama", "p", WIKI, None, 5)
    assert "ollama adapter was removed" in str(e.value)
    assert isinstance(e.value, ValueError)


def test_claude_and_codex_cmds_unchanged():
    assert _codex_cmd("p", WIKI, None)[0:2] == ["codex", "exec"]
    c = _claude_cmd("p", WIKI, "sonnet")
    assert c[0] == "claude" and "--model" in c


def test_codex_cmd_requests_the_json_event_stream_for_usage():
    """`--json` belongs to `exec`, right after the subcommand; the result JSON
    is still read from the file, so stdout is free to be the event stream."""
    cmd = _codex_cmd("p", WIKI, None)
    assert cmd[0:3] == ["codex", "exec", "--json"]
    assert _codex_cmd("p", WIKI, None, web=True)[0:4] == [
        "codex", "--search", "exec", "--json"]
    assert cmd[-1] == "p"


def test_claude_cmd_requests_json_output_for_usage():
    c = _claude_cmd("p", WIKI, None)
    assert c[c.index("--output-format") + 1] == "json"
    assert c[-1] == "p"  # the prompt stays last


# ---- usage parsers -----------------------------------------------------------

CLAUDE_JSON = json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "duration_ms": 9876, "num_turns": 3, "result": "done",
    "session_id": "s-1", "total_cost_usd": 0.0421,
    "usage": {"input_tokens": 120, "cache_creation_input_tokens": 30,
              "cache_read_input_tokens": 50, "output_tokens": 410},
})

CODEX_OUT = ("codex\nthinking...\nToken usage: total=15678 input=12345 "
             "(+ 0 cached) output=3333\n")


def _codex_turn(inp, out, cached=0, reasoning=0):
    return {"type": "turn.completed",
            "usage": {"input_tokens": inp, "cached_input_tokens": cached,
                      "cache_write_input_tokens": 0, "output_tokens": out,
                      "reasoning_output_tokens": reasoning}}


CODEX_JSON = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "item.completed",
     "item": {"id": "item_1", "type": "agent_message", "text": "reading"}},
    _codex_turn(14766, 5),
    {"type": "turn.started"},
    _codex_turn(2000, 300, cached=1500, reasoning=200),
])


def test_claude_usage_counts_cache_tokens_as_input():
    u = parse_usage("claude", CLAUDE_JSON)
    assert u == {"input_tokens": 200, "output_tokens": 410, "cost_usd": 0.0421}


def test_claude_usage_takes_the_last_object_in_stream_json():
    out = ('{"type":"assistant","usage":{"input_tokens":1,"output_tokens":1}}\n'
           + CLAUDE_JSON)
    assert parse_usage("claude", out)["output_tokens"] == 410


def test_claude_usage_without_cost_reports_tokens_only():
    out = json.dumps({"usage": {"input_tokens": 7, "output_tokens": 9}})
    assert parse_usage("claude", out) == {"input_tokens": 7, "output_tokens": 9,
                                          "cost_usd": "unknown"}


def test_codex_usage_from_the_summary_line_has_no_cost():
    assert parse_usage("codex", CODEX_OUT) == {
        "input_tokens": 12345, "output_tokens": 3333, "cost_usd": "unknown"}


def test_codex_usage_takes_the_last_line_and_strips_thousands_separators():
    out = "input=1 output=2\ninput=1,234 output=5,678\n"
    u = parse_usage("codex", out)
    assert u["input_tokens"] == 1234 and u["output_tokens"] == 5678


def test_codex_usage_sums_the_json_turns():
    assert parse_usage("codex", CODEX_JSON) == {
        "input_tokens": 16766, "output_tokens": 305, "cost_usd": "unknown"}


def test_codex_usage_does_not_double_count_cached_or_reasoning_tokens():
    """cached_input_tokens and reasoning_output_tokens are already inside the
    input/output totals; adding them would inflate every codex run."""
    out = json.dumps(_codex_turn(1000, 100, cached=900, reasoning=80))
    assert parse_usage("codex", out) == {
        "input_tokens": 1000, "output_tokens": 100, "cost_usd": "unknown"}


def test_codex_json_turns_win_over_the_text_line():
    assert parse_usage("codex", CODEX_OUT + CODEX_JSON)["input_tokens"] == 16766


def test_codex_usage_is_unknown_without_turns_or_a_text_line():
    out = json.dumps({"type": "item.completed",
                      "item": {"type": "agent_message", "text": "OK"}})
    assert parse_usage("codex", out) == "unknown"


def test_pi_reports_no_usage():
    assert parse_usage("pi", CODEX_OUT) == "unknown"


@pytest.mark.parametrize("adapter", ["claude", "codex", "pi", "nope"])
@pytest.mark.parametrize("out", ["", "\x00\x01 garbage {not json ]]",
                                 "{broken json", "tokens used: 1234"])
def test_garbage_output_is_unknown_never_an_exception(adapter, out):
    u = parse_usage(adapter, out)
    assert u == "unknown" or isinstance(u, dict)


def test_a_raising_parser_still_yields_unknown(monkeypatch):
    monkeypatch.setitem(harness.USAGE_PARSERS, "codex",
                        lambda out: (_ for _ in ()).throw(RuntimeError("boom")))
    assert parse_usage("codex", CODEX_OUT) == "unknown"


# ---- telemetry out-param -----------------------------------------------------

def proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def writing(wiki, body: str, stdout: str = "", returncode: int = 0):
    """A fake `harness._spawn` that writes the result JSON the way an adapter
    would — run_agent deletes any pre-existing one before starting."""
    def _run(*a, **kw):
        (wiki / ".agent-result.json").write_text(body)
        return proc(returncode, stdout)
    return _run


def test_telemetry_is_filled_on_a_successful_run(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        writing(tmp_path, '{"task": "ingest"}', CODEX_OUT))
    tele = {}
    assert run_agent("codex", "p", tmp_path, "gpt-x", 5,
                     telemetry=tele) == {"task": "ingest"}
    assert tele["adapter"] == "codex" and tele["model"] == "gpt-x"
    assert tele["exit_code"] == 0 and tele["timed_out"] is False
    assert tele["stdout_bytes"] == len(CODEX_OUT)
    assert isinstance(tele["duration_s"], float)
    assert tele["usage"]["input_tokens"] == 12345


def test_telemetry_is_filled_on_a_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        lambda *a, **kw: proc(3, CODEX_OUT, "fatal"))
    tele = {}
    with pytest.raises(HarnessError, match="exited 3"):
        run_agent("codex", "p", tmp_path, None, 5, telemetry=tele)
    assert tele["exit_code"] == 3 and tele["timed_out"] is False
    assert tele["usage"]["output_tokens"] == 3333


def test_telemetry_is_filled_when_no_result_json_is_written(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(0, "done"))
    tele = {}
    with pytest.raises(NoResultError):
        run_agent("claude", "p", tmp_path, "sonnet", 5, telemetry=tele)
    assert tele["exit_code"] == 0 and tele["usage"] == "unknown"


def test_telemetry_is_filled_on_timeout_including_partial_usage(monkeypatch,
                                                                tmp_path):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("codex", 5,
                                        output=CODEX_OUT.encode(), stderr=b"")
    monkeypatch.setattr(harness, "_spawn", boom)
    tele = {}
    with pytest.raises(HarnessError, match="timed out"):
        run_agent("codex", "p", tmp_path, None, 5, telemetry=tele)
    assert tele["timed_out"] is True and tele["exit_code"] is None
    assert tele["usage"]["input_tokens"] == 12345
    assert tele["duration_s"] is not None


def test_telemetry_never_carries_adapter_output(monkeypatch, tmp_path):
    """Only counts leave the harness — stdout itself must not be in there."""
    monkeypatch.setattr(harness, "_spawn",
                        writing(tmp_path, "{}", "SECRET-CREDENTIAL " + CODEX_OUT))
    tele = {}
    run_agent("codex", "p", tmp_path, None, 5, telemetry=tele)
    assert "SECRET-CREDENTIAL" not in json.dumps(tele)


def test_run_agent_works_without_a_telemetry_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", writing(tmp_path, "{}"))
    assert run_agent("codex", "p", tmp_path, None, 5) == {}


# ---- text mode (structured ingest) -------------------------------------------

def test_pi_text_cmd_has_no_tools_and_no_context_files():
    cmd = _pi_text_cmd("say it", "google/gemma-4-12b-qat", "lmstudio")
    assert cmd[0] == "pi" and "-p" in cmd
    assert "--no-tools" in cmd and "--no-context-files" in cmd
    assert "--tools" not in cmd  # not even the read-only allowlist
    assert cmd[cmd.index("--provider") + 1] == "lmstudio"
    assert cmd[-1] == "say it"


def test_run_text_returns_stdout_and_fills_telemetry(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(0, '{"a": 1}'))
    tele = {}
    out = run_text("p", "gemma", 5, provider="lmstudio", cwd=tmp_path,
                   telemetry=tele)
    assert out == '{"a": 1}'
    assert tele["adapter"] == "pi" and tele["model"] == "gemma"
    assert tele["exit_code"] == 0 and tele["usage"] == "unknown"


def test_run_text_raises_on_a_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(2, "", "boom"))
    with pytest.raises(HarnessError, match="pi exited 2"):
        run_text("p", None, 5, cwd=tmp_path)


# ---- run_web_text (claude, web tools only) -----------------------------------

WEB_JSON = json.dumps({"type": "result", "subtype": "success",
                       "result": '{"notes": []}', "total_cost_usd": 0.01,
                       "usage": {"input_tokens": 10, "output_tokens": 4}})


def test_claude_web_cmd_grants_only_the_web_tools():
    cmd = _claude_web_cmd("find it", "sonnet")
    assert cmd[0:4] == ["claude", "-p", "--output-format", "json"]
    assert cmd[cmd.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[-1] == "find it"
    assert "--permission-mode" not in cmd
    assert "--model" not in _claude_web_cmd("find it", None)


def test_run_web_text_runs_outside_the_wiki_in_a_temp_dir(monkeypatch, tmp_path):
    """A file tool the model should not have must still find nothing: the
    call's cwd is a throwaway directory, never the wiki."""
    seen = {}

    def fake_run(cmd, *a, **kw):
        seen["cwd"] = kw["cwd"]
        seen["existed"] = Path(kw["cwd"]).is_dir()
        return proc(0, WEB_JSON)

    monkeypatch.setattr(harness, "_spawn", fake_run)
    tele = {}
    assert run_web_text("p", "sonnet", 5, telemetry=tele) == '{"notes": []}'
    box = Path(seen["cwd"])
    assert seen["existed"] is True
    assert not box.is_dir()
    assert box != tmp_path and tmp_path not in box.parents
    assert "wiki" not in str(box)
    assert tele["adapter"] == "claude" and tele["model"] == "sonnet"
    assert tele["usage"]["cost_usd"] == 0.01


def test_run_web_text_raises_on_a_nonzero_exit(monkeypatch):
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(3, "", "boom"))
    with pytest.raises(HarnessError, match="claude exited 3"):
        run_web_text("p", None, 5)


@pytest.mark.parametrize("stdout", [
    "not json at all",
    json.dumps({"type": "result", "usage": {"input_tokens": 1}}),
    json.dumps({"type": "result", "result": {"notes": []}}),
])
def test_run_web_text_raises_without_result_text(monkeypatch, stdout):
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(0, stdout))
    with pytest.raises(HarnessError, match="no result text"):
        run_web_text("p", None, 5)


# ---- pi --mode json usage ----------------------------------------------------

def _pi_msg(inp=100, out=10, reasoning=0, cache=0, cost=0.0, role="assistant"):
    return {"role": role, "content": [{"type": "text", "text": "OK"}],
            "usage": {"input": inp, "output": out, "cacheRead": cache,
                      "cacheWrite": 0, "reasoning": reasoning,
                      "totalTokens": inp + out,
                      "cost": {"input": 0, "output": 0, "total": cost}}}


PI_JSON = "\n".join(json.dumps(e) for e in [
    {"type": "session"},
    {"type": "message_end", "message": _pi_msg(role="user")},
    {"type": "message_end", "message": _pi_msg(610, 6)},
    {"type": "agent_end", "messages": [_pi_msg(role="user"), _pi_msg(610, 6)]},
])


def test_pi_usage_comes_from_the_agent_end_event():
    assert parse_usage("pi", PI_JSON) == {
        "input_tokens": 610, "output_tokens": 6, "cost_usd": 0.0}


def test_pi_usage_sums_assistant_turns_and_counts_cache_and_reasoning():
    out = json.dumps({"type": "agent_end", "messages": [
        _pi_msg(100, 10, reasoning=5, cache=40, cost=0.01),
        _pi_msg(200, 20, cost=0.02),
    ]})
    assert parse_usage("pi", out) == {
        "input_tokens": 340, "output_tokens": 35, "cost_usd": 0.03}


def test_pi_usage_falls_back_to_the_last_message_when_stream_is_cut():
    out = json.dumps({"type": "message_end", "message": _pi_msg(50, 5)})
    u = parse_usage("pi", out)
    assert u["input_tokens"] == 50 and u["output_tokens"] == 5


def test_pi_usage_is_unknown_for_non_stream_output():
    assert parse_usage("pi", CODEX_OUT) == "unknown"
    assert parse_usage("pi", '{"a": 1}') == "unknown"


def test_pi_final_text_extracts_the_answer_and_run_text_returns_it(
        monkeypatch, tmp_path):
    from dbwiki.harness import pi_final_text
    assert pi_final_text(PI_JSON) == "OK"
    assert pi_final_text("plain text answer") is None
    monkeypatch.setattr(harness, "_spawn", lambda *a, **kw: proc(0, PI_JSON))
    tele = {}
    assert run_text("p", "gemma", 5, cwd=tmp_path, telemetry=tele) == "OK"
    assert tele["usage"]["input_tokens"] == 610
    assert tele["prompt_bytes"] == 1


# ---- steps: assistant turns and tool calls ----------------------------------
# PI_TOOL_JSON is a real `pi --mode json` run, captured 2026-09-09 against the
# local unsloth provider ("Read README.md with the read tool, then reply OK")
# and trimmed to the events that carry structure: the message_update deltas,
# ids, timestamps and model names are dropped, the file content anonymised.

PI_TOOL_JSON = "\n".join(json.dumps(e) for e in [
    {"type": "session"},
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_end", "message": {
        "role": "user", "content": [{"type": "text", "text": "read it"}]}},
    {"type": "message_end", "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "I should use the read tool."},
            {"type": "toolCall", "id": "call-1", "name": "read",
             "arguments": {"path": "README.md"}},
        ],
        "usage": {"input": 1030, "output": 106, "cacheRead": 0,
                  "cacheWrite": 0, "reasoning": 0, "totalTokens": 1136,
                  "cost": {"total": 0}},
        "stopReason": "toolUse"}},
    {"type": "tool_execution_start", "toolCallId": "call-1",
     "toolName": "read", "args": {"path": "README.md"}},
    {"type": "tool_execution_end", "toolCallId": "call-1", "toolName": "read",
     "result": {"content": [{"type": "text", "text": "TOOL-OUTPUT-BODY"}]},
     "isError": False},
    {"type": "message_end", "message": {
        "role": "toolResult", "toolCallId": "call-1", "toolName": "read",
        "content": [{"type": "text", "text": "TOOL-OUTPUT-BODY"}],
        "isError": False}},
    {"type": "turn_start"},
    {"type": "message_end", "message": {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "Now reply."},
            {"type": "text", "text": "OK"},
        ],
        "usage": {"input": 31, "output": 39, "cacheRead": 1137,
                  "cacheWrite": 0, "reasoning": 0, "totalTokens": 1207,
                  "cost": {"total": 0}},
        "stopReason": "stop"}},
    {"type": "agent_end", "messages": []},
    {"type": "agent_settled"},
])


def test_pi_steps_are_the_tool_calls_and_the_answer_in_order():
    steps = harness.parse_steps("pi", PI_TOOL_JSON)
    assert [(s["seq"], s["kind"], s["name"], s["preview"]) for s in steps] == [
        (0, "tool", "read", "README.md"),
        (1, "assistant", "assistant", "OK"),
    ]


def test_pi_steps_carry_the_turn_usage():
    steps = harness.parse_steps("pi", PI_TOOL_JSON)
    assert steps[0]["usage"] == {"input_tokens": 1030, "output_tokens": 106}
    assert steps[1]["usage"] == {"input_tokens": 1168, "output_tokens": 39}


def test_pi_steps_never_carry_tool_output():
    """The preview is what went *in* to a tool. A tool result is somebody
    else's file content and stays out of telemetry entirely."""
    assert "TOOL-OUTPUT-BODY" not in json.dumps(harness.parse_steps(
        "pi", PI_TOOL_JSON))


def test_pi_a_turn_calling_two_tools_counts_its_usage_once():
    out = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "usage": {"input": 10, "output": 2},
        "content": [
            {"type": "toolCall", "name": "read", "arguments": {"path": "a.md"}},
            {"type": "toolCall", "name": "edit", "arguments": {"path": "b.md"}},
        ]}})
    steps = harness.parse_steps("pi", out)
    assert [s["name"] for s in steps] == ["read", "edit"]
    assert steps[0]["usage"] == {"input_tokens": 10, "output_tokens": 2}
    assert steps[1]["usage"] is None


def test_pi_steps_are_empty_for_a_stream_that_never_reached_a_model():
    assert harness.parse_steps("pi", PI_DEAD_PROVIDER) == []


CODEX_STEPS_JSON = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {
        "id": "item_1", "type": "reasoning", "text": "I will look at the log."}},
    {"type": "item.completed", "item": {
        "id": "item_2", "type": "command_execution",
        "command": "rg ORA-00600 alert.log", "aggregated_output": "SECRET-HIT",
        "exit_status": 0}},
    {"type": "item.completed", "item": {
        "id": "item_3", "type": "file_change",
        "changes": [{"path": "pages/cdb1.md", "kind": "update"}]}},
    {"type": "item.completed", "item": {
        "id": "item_4", "type": "web_search", "query": "ORA-00600 kkqctdrvsq"}},
    {"type": "item.completed", "item": {
        "id": "item_5", "type": "mcp_tool_call", "server": "wiki",
        "tool": "lookup", "arguments": {"page": "cdb1"}}},
    {"type": "item.completed", "item": {
        "id": "item_6", "type": "agent_message", "text": "OK"}},
    _codex_turn(14766, 5),
])


def test_codex_steps_map_each_item_type_to_a_step():
    steps = harness.parse_steps("codex", CODEX_STEPS_JSON)
    assert [(s["kind"], s["name"]) for s in steps] == [
        ("assistant", "assistant"),
        ("tool", "command_execution"),
        ("tool", "file_change"),
        ("tool", "web_search"),
        ("tool", "mcp_tool_call"),
        ("assistant", "assistant"),
    ]
    assert steps[0]["preview"] == "reasoning: I will look at the log."
    assert steps[1]["preview"] == "rg ORA-00600 alert.log"
    assert steps[2]["preview"] == "pages/cdb1.md"
    assert steps[3]["preview"] == "ORA-00600 kkqctdrvsq"
    assert steps[4]["preview"] == "lookup"
    assert steps[5]["preview"] == "OK"


def test_codex_steps_never_carry_command_output():
    assert "SECRET-HIT" not in json.dumps(
        harness.parse_steps("codex", CODEX_STEPS_JSON))


def test_codex_turn_usage_lands_on_the_last_step_of_that_turn():
    steps = harness.parse_steps("codex", CODEX_STEPS_JSON)
    assert steps[-1]["usage"] == {"input_tokens": 14766, "output_tokens": 5}
    assert all(s["usage"] is None for s in steps[:-1])


def test_claude_and_unknown_adapters_have_no_steps():
    """claude -p --output-format json prints one final object, not a turn
    stream; there is nothing to nest."""
    assert harness.parse_steps("claude", CLAUDE_JSON) == []
    assert harness.parse_steps("gpt", PI_TOOL_JSON) == []


@pytest.mark.parametrize("adapter", ["claude", "codex", "pi", "nope"])
@pytest.mark.parametrize("out", ["", "\x00\x01 garbage {not json ]]",
                                 "{broken json", '{"type": 7}'])
def test_garbage_output_yields_no_steps_and_never_an_exception(adapter, out):
    assert harness.parse_steps(adapter, out) == []


def test_a_raising_step_parser_still_yields_an_empty_list(monkeypatch):
    monkeypatch.setitem(harness.STEP_PARSERS, "pi",
                        lambda out: (_ for _ in ()).throw(RuntimeError("boom")))
    assert harness.parse_steps("pi", PI_TOOL_JSON) == []


def _pi_many(n):
    return "\n".join(json.dumps({"type": "message_end", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": f"turn {i}"}]}})
        for i in range(n))


def test_steps_are_capped_by_dropping_the_middle(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        writing(tmp_path, "{}", _pi_many(250)))
    tele = {}
    run_agent("pi", "p", tmp_path, "m", 5, telemetry=tele)
    assert len(tele["steps"]) == 200
    assert tele["steps_truncated"] is True
    # seq keeps the original numbering, so the gap is visible in the trace
    assert tele["steps"][99]["seq"] == 99
    assert tele["steps"][100]["seq"] == 150
    assert tele["steps"][-1]["preview"] == "turn 249"


def test_a_short_run_is_not_marked_truncated(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        writing(tmp_path, "{}", PI_TOOL_JSON))
    tele = {}
    run_agent("pi", "p", tmp_path, "m", 5, telemetry=tele)
    assert tele["steps_truncated"] is False
    assert [s["name"] for s in tele["steps"]] == ["read", "assistant"]


def test_previews_are_capped_at_200_characters(monkeypatch, tmp_path):
    long = "x" * 5000
    out = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "content": [{"type": "text", "text": long}]}})
    monkeypatch.setattr(harness, "_spawn", writing(tmp_path, "{}", out))
    tele = {}
    run_agent("pi", "p", tmp_path, "m", 5, telemetry=tele)
    assert len(tele["steps"][0]["preview"]) == 200


def test_telemetry_has_steps_even_when_the_adapter_streams_nothing(
        monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", writing(tmp_path, "{}", CODEX_OUT))
    tele = {}
    run_agent("codex", "p", tmp_path, None, 5, telemetry=tele)
    assert tele["steps"] == [] and tele["steps_truncated"] is False


def test_steps_are_captured_on_a_timeout_too(monkeypatch, tmp_path):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired("pi", 5,
                                        output=PI_TOOL_JSON.encode(), stderr=b"")
    monkeypatch.setattr(harness, "_spawn", boom)
    tele = {}
    with pytest.raises(HarnessError, match="timed out"):
        run_agent("pi", "p", tmp_path, None, 5, telemetry=tele)
    assert [s["name"] for s in tele["steps"]] == ["read", "assistant"]


def test_pi_cmds_request_json_mode_for_usage():
    for cmd in (_pi_cmd("p", WIKI, None, None), _pi_text_cmd("p", None, None)):
        assert cmd[cmd.index("--mode") + 1] == "json"


# ---- pi settled-in-error detection -------------------------------------------
# pi exits 0 with an empty answer when the provider is unreachable. Left
# undetected that reads downstream as a malformed model response, not a dead
# backend — the 2026-08-07..09 outage, where LM Studio was simply not running.

def _pi_err_msg(err="Connection error."):
    return {"role": "assistant", "content": [], "provider": "lmstudio",
            "usage": {"input": 0, "output": 0, "totalTokens": 0},
            "stopReason": "error", "errorMessage": err}


PI_DEAD_PROVIDER = "\n".join(json.dumps(e) for e in [
    {"type": "agent_start"},
    {"type": "message_end", "message": _pi_err_msg()},
    {"type": "agent_end", "messages": [_pi_err_msg()], "willRetry": True},
    {"type": "auto_retry_start", "attempt": 3, "maxAttempts": 3},
    {"type": "agent_end", "messages": [_pi_err_msg()], "willRetry": False},
    {"type": "auto_retry_end", "success": False, "attempt": 3,
     "finalError": "Connection error."},
    {"type": "agent_settled"},
])


def test_pi_stream_error_names_the_dead_provider():
    from dbwiki.harness import pi_stream_error
    assert pi_stream_error(PI_DEAD_PROVIDER) == \
        "provider unreachable: Connection error."


def test_pi_stream_error_is_none_for_a_healthy_stream():
    from dbwiki.harness import pi_stream_error
    assert pi_stream_error(PI_JSON) is None
    assert pi_stream_error("plain text answer") is None
    assert pi_stream_error("") is None


def test_pi_stream_error_is_none_when_retries_recovered():
    from dbwiki.harness import pi_stream_error
    out = PI_DEAD_PROVIDER.replace('"success": false', '"success": true')
    assert pi_stream_error(out) is None


def test_pi_stream_error_ignores_a_run_that_errored_but_still_answered():
    from dbwiki.harness import pi_stream_error
    out = json.dumps({"type": "agent_end", "willRetry": False,
                      "messages": [_pi_err_msg(), _pi_msg(10, 2)]})
    assert pi_stream_error(out) is None


def test_run_text_raises_instead_of_returning_the_empty_stream(
        monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        lambda *a, **kw: proc(0, PI_DEAD_PROVIDER))
    with pytest.raises(HarnessError, match="provider unreachable"):
        run_text("p", "lfm2.5-2.6b", 5, provider="lmstudio", cwd=tmp_path)


def test_run_agent_pi_raises_no_model_rather_than_no_result(
        monkeypatch, tmp_path):
    # without the check this is a NoResultError ("wrote no .agent-result.json"),
    # which blames the agent for a backend that was never reachable
    monkeypatch.setattr(harness, "_spawn",
                        lambda *a, **kw: proc(0, PI_DEAD_PROVIDER))
    with pytest.raises(HarnessError, match="provider unreachable") as e:
        run_agent("pi", "p", tmp_path, "lfm2.5-2.6b", 5, provider="lmstudio")
    assert not isinstance(e.value, NoResultError)


# ---- pi answered nothing because it ran out of context -----------------------
# A reasoning model can spend the whole window thinking and stop before it
# writes a single character of the answer. That is a truncated *run*, not a bad
# proposal, and it must not be reported as one.

def _pi_truncated(total=8192):
    msg = {"role": "assistant",
           "content": [{"type": "thinking", "thinking": "let me consider..."}],
           "usage": {"input": 6588, "output": 1604, "reasoning": 1604,
                     "totalTokens": total},
           "stopReason": "length"}
    return "\n".join(json.dumps(e) for e in [
        {"type": "agent_start"},
        {"type": "message_end", "message": msg},
        {"type": "agent_end", "messages": [msg], "willRetry": False},
        {"type": "agent_settled"},
    ])


def test_pi_no_answer_reports_the_context_limit_with_the_token_count():
    from dbwiki.harness import pi_no_answer
    err = pi_no_answer(_pi_truncated())
    assert "context limit" in err and "8192 tokens" in err


def test_pi_no_answer_is_none_when_there_is_an_answer():
    from dbwiki.harness import pi_no_answer
    assert pi_no_answer(PI_JSON) is None
    assert pi_no_answer("plain text answer") is None
    assert pi_no_answer("") is None


def test_truncation_is_not_mistaken_for_an_unreachable_provider():
    from dbwiki.harness import pi_stream_error
    assert pi_stream_error(_pi_truncated()) is None


def test_run_text_raises_on_a_truncated_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        lambda *a, **kw: proc(0, _pi_truncated()))
    with pytest.raises(HarnessError, match="context limit"):
        run_text("p", "lfm2.5-2.6b", 5, provider="lmstudio", cwd=tmp_path)


def test_run_agent_tolerates_a_pi_turn_that_ends_without_text(
        monkeypatch, tmp_path):
    # agentic runs end on tool calls all the time; only run_text needs an answer
    def fake_run(*a, **kw):
        (tmp_path / harness.RESULT_FILE).write_text('{"summary": "did the work"}')
        return proc(0, _pi_truncated())

    monkeypatch.setattr(harness, "_spawn", fake_run)
    assert run_agent("pi", "p", tmp_path, "m", 5) == {"summary": "did the work"}


def test_run_agent_non_pi_adapters_are_untouched(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn",
                        lambda *a, **kw: proc(0, PI_DEAD_PROVIDER))
    with pytest.raises(NoResultError):
        run_agent("codex", "p", tmp_path, None, 5)


def test_codex_takes_search_before_the_subcommand():
    """`--search` is a top-level codex flag: codex-cli 0.147 rejects
    `codex exec --search` with "unexpected argument"."""
    assert _codex_cmd("p", WIKI, None, web=True)[0:3] == ["codex", "--search",
                                                          "exec"]
    assert "--search" not in _codex_cmd("p", WIKI, None)


def test_claude_cmd_is_a_one_shot_print_run():
    c = _claude_cmd("p", WIKI, None)
    assert c[0] == "claude" and c[1] == "-p"


# ---- process group: a timeout ends the whole adapter tree ---------------------
# Real processes, no fakes: what is under test is the kill itself. The shell
# stands in for the `codex` launcher, the backgrounded subshell for a tool
# subprocess the agent started.

def test_a_timeout_kills_the_adapters_children_too(tmp_path):
    late = tmp_path / "late.txt"
    cmd = ["sh", "-c", f"(sleep 1; echo late > {late}) & sleep 60"]
    with pytest.raises(HarnessError, match="timed out"):
        harness._run(cmd, tmp_path, 0.3, "pi", {})
    time.sleep(1.5)
    assert not late.exists()


def test_a_normal_exit_does_not_leave_a_background_writer_running(tmp_path):
    late = tmp_path / "late.txt"
    cmd = ["sh", "-c", f"(sleep 1; echo late > {late}) >/dev/null 2>&1 & "
                       f"echo done"]
    proc = harness._run(cmd, tmp_path, 10, "pi", {})
    assert proc.returncode == 0 and proc.stdout == "done\n"
    time.sleep(1.5)
    assert not late.exists()


def test_a_timeout_still_reports_the_output_so_far(tmp_path):
    tele = {}
    with pytest.raises(HarnessError, match="timed out"):
        harness._run(["sh", "-c", "echo partial; sleep 60"], tmp_path, 0.5,
                     "pi", tele)
    assert tele["timed_out"] is True and tele["stdout_bytes"] == len("partial\n")


# ---- result shape (issue 02) ---------------------------------------------------
# Valid JSON that is not the contract's shape used to reach the stage, which
# died on `.get` with no rollback and no ledger entry.

@pytest.mark.parametrize("body", [
    "[]", '"ok"', "null", "5",
    '{"summary": 5}',
    '{"pages_touched": "log.md"}',
    '{"pages_touched": ["log.md", 3]}',
    '{"notable": "yes"}',
    '{"flags": {"a": 1}}',
])
def test_a_result_of_the_wrong_shape_is_an_invalid_result(monkeypatch, tmp_path,
                                                          body):
    monkeypatch.setattr(harness, "_spawn", writing(tmp_path, body))
    with pytest.raises(harness.InvalidResultError, match="invalid result JSON"):
        run_agent("codex", "p", tmp_path, None, 5)
    assert not (tmp_path / harness.RESULT_FILE).exists()


def test_a_result_file_that_is_not_utf8_is_an_invalid_result(monkeypatch,
                                                             tmp_path):
    def _run(*a, **kw):
        (tmp_path / harness.RESULT_FILE).write_bytes(b'{"summary": "\xff"}')
        return proc(0, "")
    monkeypatch.setattr(harness, "_spawn", _run)
    with pytest.raises(harness.InvalidResultError):
        run_agent("codex", "p", tmp_path, None, 5)


def test_a_null_field_reads_as_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_spawn", writing(
        tmp_path, '{"task": "ingest", "db": null, "notable": null, '
                  '"pages_touched": ["log.md"], "flags": null}'))
    assert run_agent("codex", "p", tmp_path, None, 5) == {
        "task": "ingest", "pages_touched": ["log.md"]}


def test_a_well_formed_result_passes_through_unchanged(monkeypatch, tmp_path):
    full = {"task": "ingest", "db": "cdb1", "notable": True, "summary": "s",
            "pages_touched": ["log.md"], "incidents_opened": [],
            "incidents_updated": ["incidents/a.md"], "flags": ["f"],
            "extra": {"kept": 1}}
    monkeypatch.setattr(harness, "_spawn", writing(tmp_path, json.dumps(full)))
    assert run_agent("codex", "p", tmp_path, None, 5) == full


def _raw_config(**blocks) -> dict:
    return {"elasticsearch": {"url": "http://es.invalid:9200"},
            "wiki_repo": "wiki", "state_dir": "state", "digest_dir": "digests",
            "sources": {}, **blocks}


@pytest.mark.parametrize("block", ["agents", "research"])
def test_a_config_naming_ollama_fails_at_load_naming_the_supported_adapters(
        tmp_path, block):
    with pytest.raises(ValueError, match=rf"{block}\.adapter: unknown adapter "
                       r"'ollama'; supported adapters: codex, claude, pi"):
        Config(_raw_config(**{block: {"adapter": "ollama"}}), tmp_path)


def test_supported_or_absent_adapters_load(tmp_path):
    for name in harness.ADAPTERS:
        Config(_raw_config(agents={"adapter": name},
                           research={"adapter": name}), tmp_path)
    Config(_raw_config(research={"adapter": None}), tmp_path)


@pytest.mark.parametrize("name", ["dbwiki.yaml", "dbwiki.yaml.example"])
def test_the_shipped_configs_name_a_supported_adapter(tmp_path, name):
    path = Path(__file__).parents[1] / "config" / name
    if not path.exists():
        # The deployment's own config is private: the public export and a
        # fresh clone carry only the example.
        pytest.skip(f"{name} is not in this checkout")
    raw = yaml.safe_load(path.read_text())
    assert "ollama" not in raw["agents"]
    Config(raw, tmp_path)
