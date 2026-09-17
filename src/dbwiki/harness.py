"""Pluggable agent harness.

Contract: an adapter is invoked with (task prompt, wiki repo path, model tier)
and must exit having made its wiki edits and written the result JSON to
`.agent-result.json` in the wiki repo root. The orchestrator validates and
commits; the agent never touches git.

Adapters:
  codex  — `codex exec` (reads AGENTS.md natively)
  claude — `claude -p`   (reads CLAUDE.md -> AGENTS.md pointer)
  ollama — `codex exec --oss --local-provider ollama` (same agentic harness,
           local model; a constrained non-roaming loop remains a design
           fallback if local models prove untrustworthy in the repo)
  pi     — `pi -p` (reads AGENTS.md natively; any provider from
           `pi --list-models`, e.g. `unsloth` for local models). Tools are
           allowlisted to file access only — no bash — so the agent cannot
           touch git or roam outside the repo. Web tools (`web_search`,
           `fetch_content`) come from the `pi-web-access` extension
           (registered in the user's `~/.pi/agent/settings.json`, zero-config)
           and are granted only when `web=True`: `--no-extensions` is dropped
           so pi discovers the installed extension itself, and the tools
           allowlist is extended to match.

`run_text` is the second, non-agentic entry point: a one-shot pi call with no
tools and no file access at all, whose stdout *is* the answer. It serves the
structured ingest mode (structured.py), where the model only proposes and
deterministic code writes.

`run_web_text` is the third: a one-shot claude call whose only tools are
WebSearch and WebFetch, run in a throwaway temporary directory so no file of
the wiki is reachable. It serves the practitioner-caveat stage
(research_caveats.py), the one stage that talks to the web from this box.

pi exits 0 on failures that produced no answer at all — an unreachable
provider, or a reasoning model that spent its whole context window thinking.
Both are caught here (`pi_stream_error`, `pi_no_answer`) rather than left to
surface downstream as a malformed proposal, which is a claim about the model's
judgement for a run where the model never spoke.

Telemetry: `run_agent(..., telemetry=d)` fills `d` in place with adapter,
model, duration, exit code, timeout flag, stdout size, and best-effort token/
cost usage — on failure too. Usage parsing is per-adapter and deliberately
lossy: anything unrecognized is the string "unknown", never an exception and
never an estimate.

It also fills `steps`: one `Step` per assistant turn and tool call, parsed
from the adapters that stream them (`pi --mode json`, `codex exec --json`).
A step carries a 200-character preview of what went in — the assistant's text,
the tool's command or path — never tool output and never the raw stream. That
preview is the one place adapter text leaves this module; it goes to Langfuse,
which already carries the whole prompt, and never to the ledger, whose
scalar-only shape `orchestrate.telemetry_fields` enforces by naming its keys.
"""

import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TypedDict

RESULT_FILE = ".agent-result.json"
UNKNOWN = "unknown"
PREVIEW_CHARS = 200
MAX_STEPS = 200


class Step(TypedDict):
    """One assistant turn or tool call inside an agent run. `name` is the tool
    name for a tool step ("read", "command_execution") and "assistant" for an
    assistant turn; `usage` is that turn's tokens where the adapter reports
    them per turn, else None."""

    seq: int
    kind: str  # "assistant" | "tool"
    name: str
    preview: str
    usage: dict | None


class HarnessError(Exception):
    pass


class NoResultError(HarnessError):
    """Agent finished (exit 0) but wrote no result JSON. The orchestrator may
    synthesize a result from git status if the agent did make edits."""


class InvalidResultError(HarnessError):
    """Agent wrote a result file that is not JSON. Its own class so the retry
    in orchestrate can recognize it by type instead of by message text — the
    message stays what it was, since health.categorize and the ledger's
    category inference read it."""


def _note(telemetry: dict | None, **facts) -> None:
    """Write telemetry facts in place. Never raises — a telemetry problem must
    not turn a successful agent run into a failed one."""
    if telemetry is None:
        return
    try:
        telemetry.update(facts)
    except Exception:  # noqa: BLE001 — deliberately swallowed
        pass


def _text(raw) -> str:
    """TimeoutExpired carries bytes on POSIX even in text mode."""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""


def _capped(steps: list[Step]) -> tuple[list[Step], bool]:
    """At most MAX_STEPS records, dropping the middle of a longer run. `seq`
    keeps the original numbering, so the gap is visible rather than silent."""
    if len(steps) <= MAX_STEPS:
        return steps, False
    half = MAX_STEPS // 2
    return steps[:half] + steps[-half:], True


def _run(cmd: list[str], cwd: Path, timeout: int, adapter: str = "",
         telemetry: dict | None = None) -> subprocess.CompletedProcess:
    """Run the adapter, recording timing/exit/usage/step telemetry as it goes.
    The captured output is parsed here and discarded — only counts and step
    previews reach `telemetry`, never the stream itself."""
    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        out = _text(e.stdout)
        steps, cut = _capped(parse_steps(adapter, out))
        _note(telemetry, duration_s=round(time.monotonic() - t0, 3),
              timed_out=True, stdout_bytes=len(out), steps=steps,
              steps_truncated=cut,
              usage=parse_usage(adapter, out + _text(e.stderr)))
        raise HarnessError(f"agent timed out after {timeout}s: {cmd[0]}") from e
    steps, cut = _capped(parse_steps(adapter, proc.stdout or ""))
    _note(telemetry, duration_s=round(time.monotonic() - t0, 3),
          exit_code=proc.returncode, stdout_bytes=len(proc.stdout or ""),
          steps=steps, steps_truncated=cut,
          usage=parse_usage(adapter, (proc.stdout or "") + (proc.stderr or "")))
    return proc


def _int(raw) -> int | str:
    try:
        return int(str(raw).replace(",", "").replace("_", ""))
    except (TypeError, ValueError):
        return UNKNOWN


def _float(raw) -> float | str:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return UNKNOWN


def _json_objects(out: str):
    """Every JSON object in the output, in order: the whole text first (the
    normal `--output-format json` case), then line by line (stream-json)."""
    for blob in (out, *out.splitlines()):
        blob = blob.strip()
        if not blob.startswith("{"):
            continue
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def _usage_claude(out: str) -> dict | str:
    """`claude -p --output-format json` ends with a result object carrying
    `usage` and `total_cost_usd`. Input tokens include the cache counters —
    they are input tokens the run actually consumed."""
    for obj in reversed(list(_json_objects(out))):
        u = obj.get("usage")
        cost = obj.get("total_cost_usd", obj.get("cost_usd"))
        if not isinstance(u, dict) and cost is None:
            continue
        u = u if isinstance(u, dict) else {}
        got = [u.get(k) for k in ("input_tokens", "cache_creation_input_tokens",
                                  "cache_read_input_tokens")]
        ins = [v for v in got if isinstance(v, (int, float))]
        return {"input_tokens": int(sum(ins)) if ins else UNKNOWN,
                "output_tokens": _int(u.get("output_tokens")),
                "cost_usd": _float(cost) if cost is not None else UNKNOWN}
    return UNKNOWN


_TOKENS_RE = {
    "input_tokens": re.compile(r"\binput[ _]?(?:tokens)?\s*[:=]\s*([\d,]+)", re.I),
    "output_tokens": re.compile(r"\boutput[ _]?(?:tokens)?\s*[:=]\s*([\d,]+)", re.I),
}


def _usage_codex(out: str) -> dict | str:
    """`codex exec --json` reports usage per turn in a `turn.completed` event;
    the totals are summed over the run's turns. `cached_input_tokens` and
    `reasoning_output_tokens` are already inside `input_tokens`/`output_tokens`
    (OpenAI semantics), so adding them would double count.

    The older trailing plain-text line (`Token usage: total=… input=…
    output=…`) stays as a fallback: ollama runs through this same parser and
    its harness build may not emit the event stream. Codex reports no price,
    so cost stays unknown either way."""
    turns = [u for ev in _stream_events(out)
             if ev.get("type") == "turn.completed"
             and isinstance(u := ev.get("usage"), dict)]
    if turns:
        def total(key) -> int | str:
            vals = [v for u in turns if isinstance(v := u.get(key), (int, float))]
            return int(sum(vals)) if vals else UNKNOWN
        return {"input_tokens": total("input_tokens"),
                "output_tokens": total("output_tokens"),
                "cost_usd": UNKNOWN}
    found = {k: (m[-1] if (m := rx.findall(out)) else None)
             for k, rx in _TOKENS_RE.items()}
    if not any(found.values()):
        return UNKNOWN
    return {"input_tokens": _int(found["input_tokens"]),
            "output_tokens": _int(found["output_tokens"]),
            "cost_usd": UNKNOWN}


def _usage_none(out: str) -> str:
    """Adapters with no recognizable usage output."""
    return UNKNOWN


def _stream_events(out: str):
    """One typed JSON event per stdout line — the shape both `pi --mode json`
    and `codex exec --json` emit."""
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            yield obj


def _pi_usage_of(msg: dict) -> dict | None:
    u = msg.get("usage")
    return u if isinstance(u, dict) and msg.get("role") == "assistant" else None


def _usage_pi(out: str) -> dict | str:
    """pi --mode json carries per-message usage {input, output, cacheRead,
    cacheWrite, reasoning, cost.total}. `agent_end` lists every message of the
    run, so assistant usages are summed there (multi-turn agentic runs); a
    stream cut short falls back to the last assistant usage seen. Cache tokens
    count as input, reasoning tokens as output — same stance as claude's
    parser: tokens the run actually consumed."""
    events = list(_stream_events(out))
    usages = []
    for ev in reversed(events):
        if ev.get("type") == "agent_end" and isinstance(ev.get("messages"), list):
            usages = [u for m in ev["messages"]
                      if isinstance(m, dict) and (u := _pi_usage_of(m))]
            break
    if not usages:
        for ev in reversed(events):
            msg = ev.get("message")
            if isinstance(msg, dict) and (u := _pi_usage_of(msg)):
                usages = [u]
                break
    if not usages:
        return UNKNOWN

    def total(*keys) -> int | str:
        vals = [v for u in usages for k in keys
                if isinstance(v := u.get(k), (int, float))]
        return int(sum(vals)) if vals else UNKNOWN

    cost = [v for u in usages if isinstance(u.get("cost"), dict)
            and isinstance(v := u["cost"].get("total"), (int, float))]
    return {"input_tokens": total("input", "cacheRead", "cacheWrite"),
            "output_tokens": total("output", "reasoning"),
            "cost_usd": round(sum(cost), 6) if cost else UNKNOWN}


def pi_final_text(out: str) -> str | None:
    """The final assistant answer inside a pi --mode json event stream, or
    None when the output is not such a stream (plain text mode, garbage)."""
    for ev in reversed(list(_stream_events(out))):
        msg = ev.get("message")
        if ev.get("type") == "agent_end" and isinstance(ev.get("messages"), list):
            for m in reversed(ev["messages"]):
                if isinstance(m, dict) and m.get("role") == "assistant":
                    msg = m
                    break
        if not (isinstance(msg, dict) and msg.get("role") == "assistant"):
            continue
        parts = [p.get("text") for p in msg.get("content") or []
                 if isinstance(p, dict) and p.get("type") == "text"]
        if any(isinstance(t, str) for t in parts):
            return "".join(t for t in parts if isinstance(t, str))
    return None


def pi_stream_error(out: str) -> str | None:
    """The provider-level error a pi --mode json stream settled on, or None if
    it reached a model (or is not such a stream).

    pi exits 0 even when it never reached a model at all: an unreachable
    provider produces empty assistant messages with stopReason "error", then
    an `auto_retry_end` reporting the exhausted retries. Without this check
    that empty answer flows on to the caller and surfaces downstream as a
    malformed *model response* — which is how three days of "LM Studio is not
    running" read as `schema_version: expected 1, got None` instead."""
    for ev in reversed(list(_stream_events(out))):
        if ev.get("type") == "auto_retry_end":
            if ev.get("success"):
                return None
            return f"provider unreachable: {ev.get('finalError') or 'retries exhausted'}"
        if ev.get("type") == "agent_end" and not ev.get("willRetry"):
            msgs = [m for m in ev.get("messages") or [] if isinstance(m, dict)]
            errs = [m.get("errorMessage") for m in msgs
                    if m.get("stopReason") == "error"]
            # only decisive when *every* message errored: a run that recovered
            # mid-flight still produced an answer worth returning
            if msgs and len(errs) == len(msgs):
                return f"provider unreachable: {errs[-1] or 'reported an error'}"
            return None
    return None


def pi_no_answer(out: str) -> str | None:
    """Why a pi stream that *did* reach a model still carries no answer text,
    or None when it has one (or is not an event stream at all).

    The common cause is a reasoning model spending the whole context window on
    thinking: stopReason "length", zero text parts. Only meaningful where the
    text is the deliverable (`run_text`) — an agentic run legitimately ends on
    a tool call with nothing left to say."""
    events = list(_stream_events(out))
    if not events or pi_final_text(out) is not None:
        return None
    stop, used = None, None
    for ev in reversed(events):
        msg = ev.get("message")
        if ev.get("type") == "agent_end" and isinstance(ev.get("messages"), list):
            msg = next((m for m in reversed(ev["messages"])
                        if isinstance(m, dict) and m.get("role") == "assistant"),
                       None)
        if isinstance(msg, dict) and msg.get("stopReason"):
            stop = msg["stopReason"]
            u = msg.get("usage")
            used = u.get("totalTokens") if isinstance(u, dict) else None
            break
    if stop == "length":
        budget = f" ({used} tokens)" if isinstance(used, int) else ""
        return (f"the model hit its context limit{budget} before writing any "
                f"answer — raise the served context length or shorten the prompt")
    return f"the model returned no answer text (stopReason {stop!r})"


# ollama runs through the codex harness, so it gets codex's usage line for free
USAGE_PARSERS = {"claude": _usage_claude, "codex": _usage_codex,
                 "ollama": _usage_codex, "pi": _usage_pi}


_PREVIEW_KEYS = ("command", "path", "file_path", "changes", "query", "url",
                 "pattern", "tool", "server", "arguments", "text")


def _readable(value) -> str:
    """The most human part of an adapter's tool input: a named field where the
    adapter uses one, the compact JSON otherwise."""
    if isinstance(value, dict):
        for key in _PREVIEW_KEYS:
            if key in value and (got := _readable(value[key])).strip():
                return got
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, list):
        return " ".join(_readable(v) for v in value)
    return "" if value is None else str(value)


def _preview(value) -> str:
    return " ".join(_readable(value).split())[:PREVIEW_CHARS]


def _step(steps: list[Step], kind: str, name: str, value,
          usage: dict | None = None) -> None:
    steps.append({"seq": len(steps), "kind": kind, "name": name,
                  "preview": _preview(value), "usage": usage})


def _turn_usage(raw, ins: tuple, outs: tuple) -> dict | None:
    """Per-turn tokens in the same shape and with the same stance as
    `parse_usage`: cache and reasoning counted where they are not already
    inside the totals."""
    if not isinstance(raw, dict):
        return None

    def total(keys) -> int | str:
        vals = [v for k in keys if isinstance(v := raw.get(k), (int, float))]
        return int(sum(vals)) if vals else UNKNOWN

    return {"input_tokens": total(ins), "output_tokens": total(outs)}


def _steps_pi(out: str) -> list[Step]:
    """pi ends every assistant turn with a `message_end`, whose content items
    are the turn's `text` and its `toolCall`s ({name, arguments}). The
    `toolResult` messages that follow are the other half of a call already
    recorded, not steps of their own. The message's usage lands on its first
    step so a multi-part turn cannot count its tokens twice."""
    steps: list[Step] = []
    for ev in _stream_events(out):
        msg = ev.get("message")
        if ev.get("type") != "message_end" or not isinstance(msg, dict):
            continue
        if msg.get("role") != "assistant":
            continue
        usage = _turn_usage(msg.get("usage"),
                            ("input", "cacheRead", "cacheWrite"),
                            ("output", "reasoning"))
        for part in msg.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                _step(steps, "assistant", "assistant", part.get("text"), usage)
            elif part.get("type") == "toolCall":
                _step(steps, "tool", str(part.get("name") or "tool"),
                      part.get("arguments"), usage)
            else:
                continue
            usage = None
    return steps


_CODEX_TOOL_ITEMS = ("command_execution", "file_change", "mcp_tool_call",
                     "web_search")


def _steps_codex(out: str) -> list[Step]:
    """codex reports each finished item as `item.completed` and closes a turn
    with `turn.completed`, which is where the turn's usage is — so it lands on
    the last step of that turn. Error items are the run's failure, already
    carried by the exit code and the harness error."""
    steps: list[Step] = []
    for ev in _stream_events(out):
        if ev.get("type") == "turn.completed":
            usage = _turn_usage(ev.get("usage"), ("input_tokens",),
                                ("output_tokens",))
            if usage and steps:
                steps[-1]["usage"] = usage
            continue
        item = ev.get("item")
        if ev.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "agent_message":
            _step(steps, "assistant", "assistant", item.get("text"))
        elif kind == "reasoning":
            _step(steps, "assistant", "assistant",
                  "reasoning: " + _preview(item.get("text")))
        elif kind in _CODEX_TOOL_ITEMS:
            _step(steps, "tool", kind, item)
    return steps


def _steps_none(out: str) -> list[Step]:
    """claude `--output-format json` prints one final object and no turn
    stream, so its runs stay flat until `stream-json` earns its keep."""
    return []


STEP_PARSERS = {"codex": _steps_codex, "ollama": _steps_codex,
                "pi": _steps_pi}


def parse_steps(adapter: str, out: str) -> list[Step]:
    """The assistant turns and tool calls inside one agent run, in order.
    Adapters that stream nothing parseable give an empty list; like
    `parse_usage`, this never raises."""
    try:
        return STEP_PARSERS.get(adapter, _steps_none)(out or "")
    except Exception:  # noqa: BLE001 — telemetry is never a failure mode
        return []


def parse_usage(adapter: str, out: str) -> dict | str:
    """Best-effort {input_tokens, output_tokens, cost_usd} from adapter output.
    Unparseable output — including garbage — is the string "unknown"; so is any
    individual field the adapter did not report. Never raises."""
    try:
        return USAGE_PARSERS.get(adapter, _usage_none)(out or "")
    except Exception:  # noqa: BLE001 — telemetry is never a failure mode
        return UNKNOWN


def _codex_cmd(prompt: str, wiki: Path, model: str | None,
               oss: bool = False, web: bool = False) -> list[str]:
    # --json makes stdout the event stream carrying per-turn usage
    # (_usage_codex) and per-item steps (_steps_codex); the result JSON is
    # still read from the file the agent writes, so stdout is free to be it.
    cmd = ["codex", *(["--search"] if web else []), "exec", "--json",
           "-C", str(wiki),
           "-s", "workspace-write", "--skip-git-repo-check", "--ephemeral",
           "--color", "never"]
    if oss:
        cmd += ["--oss", "--local-provider", "ollama"]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    return cmd


def _claude_cmd(prompt: str, wiki: Path, model: str | None,
                web: bool = False) -> list[str]:
    tools = "Read,Glob,Grep,Write,Edit,MultiEdit"
    if web:
        tools += ",WebSearch,WebFetch"
    # the result JSON is still read from the file the agent writes
    cmd = ["claude", "-p", "--output-format", "json",
           "--permission-mode", "acceptEdits", "--allowedTools", tools]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


def _pi_cmd(prompt: str, wiki: Path, model: str | None,
            provider: str | None, web: bool = False) -> list[str]:
    # pi's `bash` stays disabled so the agent cannot run git. web=True drops
    # --no-extensions so pi discovers the installed pi-web-access extension
    # from the user's own settings rather than a hardcoded path. --mode json
    # makes stdout the event stream carrying per-message usage (_usage_pi);
    # the result JSON is still read from the file.
    cmd = ["pi", "-p", "--mode", "json", "--no-session"]
    tools = "read,grep,find,ls,edit,write"
    if web:
        tools += ",web_search,fetch_content"
    else:
        cmd.append("--no-extensions")
    cmd += ["--no-skills", "--tools", tools]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh",
                      "max")


def pi_model_id(model: str) -> str:
    """The served model id under a pi model string: pi accepts
    `provider/id:<thinking level>` and the suffix is its own, so anything
    that asks the server about the model (health) must drop it."""
    head, sep, tail = model.rpartition(":")
    return head if sep and tail in PI_THINKING_LEVELS else model


def _pi_text_cmd(prompt: str, model: str | None,
                 provider: str | None) -> list[str]:
    # text completion, not an agent: no context files (AGENTS.md included)
    # and no tools, so the model can only answer. run_text extracts the answer
    # text back out of the --mode json stream.
    cmd = ["pi", "-p", "--mode", "json", "--no-session", "--no-extensions",
           "--no-skills", "--no-context-files", "--no-tools"]
    if provider:
        cmd += ["--provider", provider]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


def run_text(prompt: str, model: str | None, timeout: int,
             provider: str | None = None, cwd: Path | None = None,
             telemetry: dict | None = None) -> str:
    """One-shot pi text call: returns the model's answer, fills `telemetry`
    with the same shape `run_agent` does (adapter `pi`). No result file, no
    wiki edits — the caller owns everything the answer implies. pi runs in
    --mode json (for token usage); the answer text is extracted from the event
    stream, falling back to raw stdout when it isn't one."""
    _note(telemetry, adapter="pi", model=model, duration_s=None,
          exit_code=None, timed_out=False, stdout_bytes=0,
          prompt_bytes=len(prompt.encode("utf-8", "replace")), usage=UNKNOWN,
          steps=[], steps_truncated=False)
    proc = _run(_pi_text_cmd(prompt, model, provider), Path(cwd or Path.cwd()),
                timeout, "pi", telemetry)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise HarnessError(f"pi exited {proc.returncode}: {tail}")
    out = proc.stdout or ""
    if err := (pi_stream_error(out) or pi_no_answer(out)):
        raise HarnessError(f"pi produced no answer — {err}")
    text = pi_final_text(out)
    return out if text is None else text


def _claude_web_cmd(prompt: str, model: str | None) -> list[str]:
    # web research, not an agent over the wiki: WebSearch/WebFetch are the
    # only tools, so there is no Read, Glob or Write for a prompt injected
    # from a fetched page to reach for. run_web_text pairs this with a
    # throwaway cwd, which is what keeps the wiki out of reach even if a
    # future claude release granted a file tool by default.
    cmd = ["claude", "-p", "--output-format", "json",
           "--allowedTools", "WebSearch,WebFetch"]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    return cmd


def run_web_text(prompt: str, model: str | None, timeout: int, *,
                 telemetry: dict | None = None) -> str:
    """One-shot claude call with web search and fetch and nothing else:
    returns the answer text from the `--output-format json` envelope and
    fills `telemetry` the way `run_agent` does for the claude adapter (usage
    and cost come from the same `_usage_claude` parser).

    The call runs in a fresh temporary directory that is deleted when it
    returns, so the process never has the wiki as its working directory and
    no file tool, granted or inherited, can read a page of it. Nothing but
    the prompt leaves the box; the caller owns everything the answer implies,
    exactly as with `run_text`.

    A non-zero exit, an unparseable envelope and an envelope whose `result`
    is missing or not a string are all `HarnessError`: the stage that calls
    this validates a JSON proposal, and an answer that never arrived is a
    harness failure rather than a bad proposal."""
    _note(telemetry, adapter="claude", model=model, duration_s=None,
          exit_code=None, timed_out=False, stdout_bytes=0,
          prompt_bytes=len(prompt.encode("utf-8", "replace")), usage=UNKNOWN,
          steps=[], steps_truncated=False)
    with tempfile.TemporaryDirectory() as box:
        proc = _run(_claude_web_cmd(prompt, model), Path(box), timeout,
                    "claude", telemetry)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise HarnessError(f"claude exited {proc.returncode}: {tail}")
    for obj in reversed(list(_json_objects(proc.stdout or ""))):
        if isinstance(obj.get("result"), str):
            return obj["result"]
    tail = (proc.stdout or proc.stderr or "")[-2000:]
    raise HarnessError(f"claude produced no result text: {tail}")


def run_agent(adapter: str, prompt: str, wiki: Path, model: str | None,
              timeout: int, web: bool = False, provider: str | None = None,
              telemetry: dict | None = None) -> dict:
    """Run the adapter and return the parsed result JSON. `web` grants the
    agent web search/fetch (research task only). `provider` selects the pi
    model provider (e.g. `unsloth`); other adapters ignore it.

    `telemetry`, when given, is filled in place with adapter/model/duration_s/
    exit_code/timed_out/stdout_bytes/usage/steps — including on every failure
    path, and never raising from telemetry code itself."""
    _note(telemetry, adapter=adapter, model=model, duration_s=None,
          exit_code=None, timed_out=False, stdout_bytes=0,
          prompt_bytes=len(prompt.encode("utf-8", "replace")), usage=UNKNOWN,
          steps=[], steps_truncated=False)
    result_path = wiki / RESULT_FILE
    result_path.unlink(missing_ok=True)

    if adapter == "codex":
        cmd = _codex_cmd(prompt, wiki, model, web=web)
    elif adapter == "ollama":
        cmd = _codex_cmd(prompt, wiki, model, oss=True, web=web)
    elif adapter == "claude":
        cmd = _claude_cmd(prompt, wiki, model, web=web)
    elif adapter == "pi":
        cmd = _pi_cmd(prompt, wiki, model, provider, web=web)
    else:
        raise HarnessError(f"unknown adapter: {adapter}")

    proc = _run(cmd, cwd=wiki, timeout=timeout, adapter=adapter,
                telemetry=telemetry)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-2000:]
        raise HarnessError(f"{adapter} exited {proc.returncode}: {tail}")
    if adapter == "pi" and (err := pi_stream_error(proc.stdout or "")) is not None:
        raise HarnessError(f"pi produced no answer — {err}")
    if not result_path.exists():
        tail = (proc.stdout or "")[-2000:]
        raise NoResultError(f"{adapter} finished but wrote no {RESULT_FILE}; "
                            f"last output: {tail}")
    try:
        return json.loads(result_path.read_text())
    except json.JSONDecodeError as e:
        raise InvalidResultError(f"invalid result JSON: {e}") from e
    finally:
        result_path.unlink(missing_ok=True)
