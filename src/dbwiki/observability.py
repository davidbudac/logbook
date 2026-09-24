"""Optional Langfuse trace export — one trace per attempted agent stage.

Trace export (docs/langfuse.md): the orchestrator's `_telemetry`
choke point — through which every agent stage passes, agentic and structured,
success and failure — additionally records each run as a Langfuse trace with
a single generation-type observation — with one child per streamed turn or
tool call when the adapter gave them (`tele["steps"]`) — plus boolean/numeric
scores for validation, rollback and lint findings. The generation links the Langfuse
prompt version of the static instruction block the stage ran under
(`promptreg`), so a score breakdown can be read per wording.
`session_id` is the pipeline run_id,
so the stages of one scheduler tick group into one Langfuse session and
correlate with the wiki commit's `Run-ID:` trailer and the
`.state/agent_runs.jsonl` / ELK telemetry.

Off by default (`langfuse.enabled: false` in config/dbwiki.yaml) and inert
without the `langfuse` package (`uv sync`). Same discipline
as every other telemetry path here: best-effort, one stderr warning per
process kind of failure, and never a reason to fail — or block — a run.

Content stance: unlike the ledger and ELK telemetry (counts and identifiers
only), this export deliberately carries content — the exact prompt as the
observation's input and the agent's result JSON as its output. That is
Langfuse's point: content-level debugging the counts-only stores cannot do.
It means digest excerpts with real alert-log lines reach the configured
Langfuse host, so point `langfuse.host` somewhere you trust with them
(docs/langfuse.md).

The observation is recorded after the run finishes, so the Langfuse-side
span duration is meaningless (~0s); the run's real `duration_s` is in the
trace metadata and scores, same value the ledger carries.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

# process-wide state: the client is built once, and each distinct failure
# mode (`warn_once` key) warns once rather than once per agent run
_client = None
_client_failed = False
_warned: set[str] = set()
_release_sha: str | None = None
_release_read = False

#: fields copied from the ledger telemetry block into trace metadata when
#: present — identifiers and counts only, mirroring health.record_agent_run
_META_FIELDS = ("run_id", "task", "adapter", "model", "model_tier",
                "duration_s", "timed_out", "attempts", "validation_ok",
                "rolled_back", "lint_findings", "pages_touched",
                "incidents_opened", "incidents_updated", "digest_bytes",
                "telemetry_error")


def warn_once(key: str, msg: str) -> None:
    """`warning: <msg>` on stderr, the first time `key` fails in this process
    only. The one warn-once every best-effort telemetry path shares (this
    export, `health`'s recorders), so a failure repeating every agent run is
    one line, not a flood."""
    if key in _warned:
        return
    _warned.add(key)
    print(f"warning: {msg}", file=sys.stderr)


def _warn_once(key: str, msg: str) -> None:
    warn_once(f"langfuse:{key}", f"langfuse export: {msg}")


def _release() -> str | None:
    """Short git sha of *this application checkout* — the Langfuse `release`,
    so a quality regression can be pinned to the commit that shipped it. Read
    once per process from the directory this module lives in (the wiki repo is
    a different tree with its own history). None outside a checkout, or when
    git is unavailable: a missing release is not a reason to skip the
    export."""
    global _release_sha, _release_read
    if _release_read:
        return _release_sha
    _release_read = True
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=Path(__file__).resolve().parent,
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            _release_sha = out.stdout.strip() or None
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _warn_once("release", f"git sha unavailable: {type(e).__name__}: {e}")
    return _release_sha


def _get_client(lf_cfg: dict):
    """Build (once) the Langfuse client, or None when disabled/broken. Keys
    come from config or the SDK's own env vars (LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY); the env wins inside the SDK when config omits
    them, same stance as DBWIKI_ES_PASSWORD."""
    global _client, _client_failed
    if _client is not None or _client_failed:
        return _client
    try:
        from langfuse import Langfuse
    except ImportError:
        _client_failed = True
        _warn_once("import", "langfuse.enabled is true but the langfuse "
                   "package is not installed (uv sync); "
                   "traces will not be exported")
        return None
    try:
        _client = Langfuse(
            public_key=lf_cfg.get("public_key"),
            secret_key=lf_cfg.get("secret_key"),
            host=lf_cfg.get("host"),
            environment=lf_cfg.get("environment"),
            release=_release(),
        )
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _client_failed = True
        _warn_once("init", f"client init failed: {type(e).__name__}: {e}")
        return None
    return _client


def _tokens(u) -> dict | None:
    """Langfuse `usage_details` from an adapter usage dict, or None where the
    adapter reported "unknown" — never estimated, same rule as stats.py."""
    if not isinstance(u, dict):
        return None
    usage = {}
    for src, dst in (("input_tokens", "input"), ("output_tokens", "output")):
        if isinstance(u.get(src), (int, float)):
            usage[dst] = int(u[src])
    return usage or None


def _usage(fields: dict) -> tuple[dict | None, dict | None]:
    """(usage_details, cost_details) from the ledger block's `usage`."""
    u = fields.get("usage")
    cost = {"total": float(u["cost_usd"])} \
        if isinstance(u, dict) and isinstance(u.get("cost_usd"), (int, float)) \
        else None
    return _tokens(u), cost


def _add_steps(obs, steps) -> None:
    """One child observation per harness step (`tele["steps"]`): assistant
    turns as generations, tool calls as tools, each carrying the ≤200-char
    preview as input and its per-turn usage when the adapter gave one. The
    steps are what the agent CLI streamed while the stage ran, so a 450 s
    black box reads as its turns; a broken step list costs the children only,
    never the stage's own observation or its scores."""
    if not isinstance(steps, list):
        return
    try:
        for step in steps:
            if not isinstance(step, dict):
                continue
            child = obs.start_observation(
                name=str(step.get("name") or step.get("kind") or "step"),
                as_type=("generation" if step.get("kind") == "assistant"
                         else "tool"),
                input=step.get("preview"),
                usage_details=_tokens(step.get("usage")))
            child.end()
    except Exception as e:  # noqa: BLE001 — children are best-effort
        _warn_once("steps", f"step observations: {type(e).__name__}: {e}")


def trace_id(event_id: str) -> str:
    """The Langfuse trace id of the stage whose ledger line carries
    `event_id`: `Langfuse.create_trace_id(seed=event_id)`, spelled out so the
    portal names a trace without the SDK. Pure, so the exporter and the
    workbench cannot disagree."""
    return hashlib.sha256(event_id.encode("utf-8")).digest()[:16].hex()


def record_agent_run(cfg, fields: dict, tele: dict | None = None, *,
                     db: str | None = None, mode: str | None = None,
                     prompt: str | None = None,
                     result: dict | None = None,
                     event_id: str | None = None) -> None:
    """Export one attempted agent stage as a Langfuse trace. `fields` is the
    ledger telemetry block (orchestrate.telemetry_fields), `tele` the raw
    harness telemetry; both are the exact dicts health.record_agent_run gets.
    `prompt`/`result` become the observation's input/output — the ledger
    block itself never carries either. `event_id` is the ledger line's id and
    seeds the trace id (`trace_id`); without one the SDK picks a random id
    and nothing can link to the trace by name. Never raises."""
    try:
        lf_cfg = cfg.langfuse
        if not lf_cfg.get("enabled"):
            return
        client = _get_client(lf_cfg)
        if client is None:
            return
        tele = tele or {}

        meta = {k: v for k in _META_FIELDS
                if (v := fields.get(k)) is not None}
        if db is not None:
            meta["db"] = db
        if mode is not None:
            meta["mode"] = mode
        for key in ("exit_code", "stdout_bytes", "prompt_bytes"):
            if isinstance(tele.get(key), (int, float)):
                meta[key] = tele[key]
        # the harness caps the step list; say so on the parent rather than
        # letting the children read as the whole run
        if tele.get("steps_truncated"):
            meta["steps_truncated"] = True

        task = fields.get("task", "agent")
        ok = fields.get("validation_ok")
        usage, cost = _usage(fields)
        tags = [t for t in (task, fields.get("adapter"),
                            fields.get("model_tier"), mode) if t]

        from .promptreg import lookup, register
        hit = lookup(cfg, task, mode, fields.get("model_tier"))
        prompt_version = register(client, *hit) if hit else None

        from langfuse import propagate_attributes
        with propagate_attributes(
                trace_name=f"dbwiki-{task}",
                session_id=fields.get("run_id"),
                environment=lf_cfg.get("environment"),
                tags=tags, metadata={"db": db} if db else None):
            obs = client.start_observation(
                name=task, as_type="generation",
                trace_context=({"trace_id": trace_id(event_id)}
                               if event_id else None),
                model=fields.get("model") or tele.get("model"),
                input=prompt,
                prompt=prompt_version,
                metadata=meta,
                level="ERROR" if ok is False else "DEFAULT",
                status_message=None if ok is not False
                else ("rolled back" if fields.get("rolled_back")
                      else "validation failed"),
                usage_details=usage, cost_details=cost)
            try:
                _add_steps(obs, tele.get("steps"))
                if isinstance(result, dict):
                    obs.update(output=result)
                if isinstance(ok, bool):
                    obs.score_trace(name="validation_ok", value=float(ok),
                                    data_type="BOOLEAN")
                if isinstance(fields.get("rolled_back"), bool):
                    obs.score_trace(name="rolled_back",
                                    value=float(fields["rolled_back"]),
                                    data_type="BOOLEAN")
                if isinstance(fields.get("lint_findings"), int):
                    obs.score_trace(name="lint_findings",
                                    value=float(fields["lint_findings"]),
                                    data_type="NUMERIC")
                if isinstance(fields.get("duration_s"), (int, float)):
                    obs.score_trace(name="duration_s",
                                    value=float(fields["duration_s"]),
                                    data_type="NUMERIC")
            finally:
                obs.end()
        # the CLI is short-lived; hand the events over before the process
        # exits rather than trusting atexit through cron
        client.flush()
    except Exception as e:  # noqa: BLE001 — deliberately swallowed
        _warn_once("record", f"{type(e).__name__}: {e}")
