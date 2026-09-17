#!/usr/bin/env python3
"""Liveness checks for the things `dbwiki` itself cannot report. Stdlib only.

docs/scheduling.md ("Watching it"): a stopped local model server is the single
most likely cause of a run of harness_error ingests, and nothing in dbwiki
reports it; config/schedule.json and the crontab can silently drift. This
script covers exactly those gaps and nothing that `dbwiki health` already
does.

    python3 n8n/scripts/watchdog.py            # JSON on stdout
    python3 n8n/scripts/watchdog.py --fix      # ... and reload the model when
                                               # it is unloaded or short of context
    exit 0  healthy (warnings allowed)
    exit 1  at least one problem

Runs from anywhere (cron, n8n over ssh, by hand). Checks:

  model_server  server reachable, configured models available, any loaded
                one serves context >= --min-context (JIT: not-loaded is a warning)
  tick          the last `run` round in .state/run_health.jsonl is not older
                than the schedule says it should be (+ grace)
  failures      the last N `run` rounds did not all fail
  crontab       every on-prem entry of config/schedule.json is installed
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "elk" / "scripts"))
from emit_derived import _section_scalar, next_run  # noqa: E402  (the schedule stream's own parsers)

STATE = ROOT / ".state"
SCHEDULE = ROOT / "config" / "schedule.json"
CONFIG = ROOT / "config" / "dbwiki.yaml"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


PI_MODELS_JSON = Path.home() / ".pi/agent/models.json"


def _get_json(url: str, timeout: float, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _post_json(url: str, payload: dict, timeout: float, headers: dict | None = None):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode().strip() or "null")


def _configured_provider() -> str | None:
    """agents.pi.provider from config/dbwiki.yaml, same scrape as
    `_configured_models` (no PyYAML here)."""
    try:
        text = CONFIG.read_text()
    except OSError:
        return None
    seen_pi = False
    for line in text.splitlines():
        if re.match(r"^\s*pi:\s*(#.*)?$", line):
            seen_pi = True
            continue
        if seen_pi:
            if re.match(r"^\s*[a-z_]+:\s*(#.*)?$", line):   # next block starts
                break
            if m := re.match(r"^\s*provider:\s*([^\s#]+)", line):
                return m.group(1).strip("'\"")
    return None


def _pi_endpoint(provider: str | None) -> tuple[str | None, str | None]:
    """(server root, API key) that pi itself uses for `provider`, from its own
    provider table. dbwiki's health check reads the same file for the same
    reason: probe the server the stages actually talk to, and carry the key a
    server like unsloth studio demands. Never raises."""
    if not provider:
        return None, None
    try:
        entry = (json.loads(PI_MODELS_JSON.read_text()).get("providers")
                 or {}).get(provider) or {}
    except Exception:
        return None, None
    base = str(entry.get("baseUrl") or "").rstrip("/")
    if base.endswith("/v1"):                 # probe paths add their own
        base = base[:-3].rstrip("/")
    return base or None, entry.get("apiKey")


class Endpoint(NamedTuple):
    base: str
    headers: dict
    provider: str | None


def _endpoint(base: str | None) -> Endpoint:
    """The one server root and auth header the run uses. Resolved once so
    `--fix` can never POST at a different server than the check read."""
    provider = _configured_provider()
    pi_base, key = _pi_endpoint(provider)
    root = (base or pi_base or "http://localhost:1234").rstrip("/")
    return Endpoint(root, {"Authorization": f"Bearer {key}"} if key else {}, provider)


def _configured_models() -> list[str]:
    """agents.pi.{cheap,strong} from config/dbwiki.yaml without PyYAML: the
    `cheap:` / `strong:` (or legacy `model:`) lines after the `pi:` line."""
    try:
        text = CONFIG.read_text()
    except OSError:
        return []
    seen_pi, out = False, []
    for line in text.splitlines():
        if re.match(r"^\s*pi:\s*(#.*)?$", line):
            seen_pi = True
            continue
        if seen_pi:
            if re.match(r"^\s*[a-z_]+:\s*(#.*)?$", line):   # next block starts
                break
            m = re.match(r"^\s*(cheap|strong|model):\s*([^\s#]+)", line)
            if m and m.group(2) not in ("null", "~"):
                out.append(_served_id(m.group(2).strip("'\"")))
    return list(dict.fromkeys(out))


#: pi's thinking-level suffixes (harness.PI_THINKING_LEVELS, kept in step by
#: hand: this script imports nothing from the package)
_PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh",
                       "max")


def _served_id(model: str) -> str:
    """The id the server lists under a pi model string: `provider/id:<level>`
    is pi's own suffix. Left on, the cheap tier `...:off` was "not served" on
    every tick from 2026-09-13 and the reload path never fired."""
    head, sep, tail = model.rpartition(":")
    return head if sep and tail in _PI_THINKING_LEVELS else model


# ---- checks -----------------------------------------------------------------

def _served_models(models) -> list[dict]:
    """One record per listed model, across both server dialects. unsloth
    studio reports load state as the boolean `loaded`, LM Studio as the string
    `state`, and plain /v1/models reports neither — None means the server did
    not say, which is not the same as "not loaded". Reading only `state` put
    every unsloth model in the loaded list and made the not-loaded branch dead.
    `context_length` is the served context; `max_context_length` is not."""
    out = []
    for m in models or []:
        if not isinstance(m, dict):
            continue
        loaded = (m["loaded"] if isinstance(m.get("loaded"), bool)
                  else None if m.get("state") is None else m.get("state") == "loaded")
        ctx = m.get("loaded_context_length") or m.get("context_length")
        out.append({"id": m.get("id"), "loaded": loaded,
                    "quant": m.get("quant"), "context_length": ctx})
    return out


def check_model_server(ep: Endpoint, min_context: int,
                       timeout: float) -> tuple[dict, list[dict]]:
    """The operator-facing check, plus the normalized listing beside it: --fix
    needs the load state and `quant` of models the check deliberately leaves
    out of its report."""
    base, headers = ep.base, ep.headers
    out = {"url": base, "provider": ep.provider, "reachable": False,
           "loaded": [], "problems": []}
    models, fell_back = None, False
    try:                                     # LM Studio's own REST API first (has state)
        data = _get_json(f"{base}/api/v0/models", timeout, headers)
        models = data.get("data", data) if isinstance(data, dict) else data
        out["reachable"] = True
    except Exception:
        try:                                 # OpenAI-compatible fallback
            data = _get_json(f"{base}/v1/models", timeout, headers)
            models = data.get("data", [])
            out["reachable"] = True
            fell_back = True
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
    if not out["reachable"]:
        out["problems"].append(f"model server not reachable at {base}")
        return out, []
    served = _served_models(models)
    if fell_back:
        # unsloth's /v1/models does report load state, so which URL answered
        # says nothing about what is known: read that off the records.
        out["note"] = ("/api/v0 unavailable; loaded state unknown"
                       if all(s["loaded"] is None for s in served)
                       else "/api/v0 unavailable; load state from the listing")
    for s in served:
        if s["loaded"] is False:
            continue
        out["loaded"].append({"id": s["id"], "loaded": s["loaded"],
                              "context_length": s["context_length"]})
    want = _configured_models()
    out["configured_models"] = want
    all_ids = [s["id"] for s in served]
    loaded_ids = [m["id"] for m in out["loaded"]]
    # The server JIT-loads on first request and evicts idle models after the
    # TTL, so "nothing loaded" is normal; "not on disk" is not.
    for w in want:
        if all_ids and w not in all_ids:
            out["problems"].append(f"configured model {w!r} is not served ({all_ids})")
        elif w not in loaded_ids:
            out.setdefault("warnings", []).append(
                f"{w} not loaded right now (JIT loads it on the next request)")
    for m in out["loaded"]:
        if want and m["id"] not in want:
            continue
        if m["context_length"] is not None and m["context_length"] < min_context:
            out["problems"].append(
                f"{m['id']} loaded with context {m['context_length']} < {min_context}"
                " (docs/scheduling.md: per-model default config sets 32768)")
    return out, served


# ---- --fix: reload the model the checks found wanting ------------------------

#: The two verdicts a reload can repair. Every other verdict is either fine or
#: a guess, and the studio holds one resident model — reloading on a guess
#: evicts a working one.
RELOADABLE = ("context_too_small", "not_loaded")
POLL_SECONDS = 2.0


class Verdict(NamedTuple):
    model: str
    verdict: str
    context: int | None
    quant: str | None


def verdict_for(model: str, report: dict, served: list[dict],
                min_context: int) -> Verdict:
    """What the server is doing with one configured model. `unknown` is the
    honest answer when the listing reports no load state (plain /v1/models) or
    no context: --fix acts on facts, never on a guess."""
    if not report.get("reachable"):
        return Verdict(model, "unreachable", None, None)
    m = next((s for s in served if s["id"] == model), None)
    if m is None:
        return Verdict(model, "not_served", None, None)
    ctx, quant = m["context_length"], m["quant"]
    if m["loaded"] is False:
        return Verdict(model, "not_loaded", ctx, quant)
    if m["loaded"] is None or ctx is None:
        return Verdict(model, "unknown", ctx, quant)
    return Verdict(model, "healthy" if ctx >= min_context else "context_too_small",
                   ctx, quant)


def _await_ready(ep: Endpoint, deadline_s: float, timeout: float) -> None:
    """Poll load-progress until the studio says `ready`. A poll that raises is
    not fatal: the server refuses connections while it maps 25 GB in, so only
    the deadline decides."""
    deadline = time.monotonic() + deadline_s
    last = "no poll succeeded"
    while True:
        try:
            phase = _get_json(f"{ep.base}/api/inference/load-progress",
                              timeout, ep.headers).get("phase")
            if phase == "ready":
                return
            last = f"phase {phase!r}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if time.monotonic() >= deadline:
            raise TimeoutError(f"load not ready within {deadline_s:g}s ({last})")
        time.sleep(POLL_SECONDS)


def apply_fix(ep: Endpoint, report: dict, served: list[dict], *,
              min_context: int, max_seq_length: int, variant: str | None,
              fix_timeout: float, timeout: float) -> tuple[dict, dict]:
    """Reload the configured model when its verdict says a reload repairs it.

    Returns the `fix` record and the model_server check to report: after a
    reload attempt that is a fresh read of the server's own listing, because
    the load POST and the progress endpoint both claim success on their own
    say-so. `reloaded` is true only when that fresh read proves it."""
    models = report.get("configured_models") or _configured_models()
    verdicts = [verdict_for(m, report, served, min_context) for m in models]
    wanted = [v for v in verdicts if v.verdict in RELOADABLE]
    target = wanted[0] if wanted else (verdicts[0] if verdicts else
                                       Verdict("", "unknown", None, None))
    fix = {"model": target.model, "verdict": target.verdict, "reloaded": False,
           "context_before": target.context}

    if len(wanted) > 1:
        error = (f"{len(wanted)} configured models want a reload "
                 f"({', '.join(v.model for v in wanted)}) and the studio holds "
                 "one resident model: reloaded none, pick one by hand")
    elif not wanted:
        return fix, report
    elif not (variant or target.quant):
        error = (f"the listing reports no `quant` for {target.model} and no "
                 "--variant was given: refusing to guess the gguf variant")
    else:
        error = None

    if error:
        fix["error"] = error
        report["problems"].append(f"--fix did not reload: {error}")
        return fix, report

    started = time.monotonic()
    try:
        _post_json(f"{ep.base}/api/inference/load", {
            "model_path": target.model,
            "gguf_variant": variant or target.quant,
            "max_seq_length": max_seq_length,
            "force_reload": True,
            "load_request_id": uuid.uuid4().hex,
        }, fix_timeout, ep.headers)      # the studio can hold the POST open for the load
        _await_ready(ep, fix_timeout, timeout)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    fix["elapsed_s"] = round(time.monotonic() - started, 1)

    fresh, fresh_served = check_model_server(ep, min_context, timeout)
    after = verdict_for(target.model, fresh, fresh_served, min_context)
    fix["context_after"] = after.context
    if error is None and after.verdict != "healthy":
        error = (f"reload finished but {target.model} is {after.verdict} "
                 f"(context {after.context}, want >= {min_context})")
    if error:
        fix["error"] = error
        fresh["problems"].append(f"--fix failed to reload {target.model}: {error}")
    fix["reloaded"] = error is None
    return fix, fresh


def _run_rounds(limit: int) -> list[dict]:
    p = STATE / "run_health.jsonl"
    if not p.exists():
        return []
    rounds = []
    for line in p.read_text().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("command") == "run":
            rounds.append(d)
    return rounds[-limit:]


def check_tick(grace_min: int) -> dict:
    entries = json.loads(SCHEDULE.read_text())["entries"]
    tick = next(e for e in entries if e["entry"] == "tick")
    out = {"cron": tick["cron"], "problems": []}
    rounds = _run_rounds(1)
    if not rounds:
        out["problems"].append("no `run` round in .state/run_health.jsonl")
        return out
    last = _parse_ts(rounds[-1].get("finished")) or _parse_ts(rounds[-1].get("started"))
    if last is None:
        out["problems"].append("last run round has no parseable timestamp")
        return out
    # the schedule stream computes fire times in local time — do the same
    local = last.astimezone()
    expected = next_run(tick["cron"], local)
    now = _now()
    out.update(last_finished=last.isoformat(timespec="seconds"),
               last_run_id=rounds[-1].get("run_id"),
               last_outcome=rounds[-1].get("outcome"),
               next_expected=expected.isoformat(timespec="minutes"),
               age_min=round((now - last).total_seconds() / 60))
    if now > expected.astimezone(dt.timezone.utc) + dt.timedelta(minutes=grace_min):
        out["problems"].append(
            f"tick is stale: last round {out['age_min']} min ago, a round was due "
            f"{expected.strftime('%Y-%m-%d %H:%M %Z')} (+{grace_min} min grace)")
    return out


def check_failures(n: int) -> dict:
    rounds = _run_rounds(n)
    out = {"window": n, "outcomes": [r.get("outcome") for r in rounds],
           "error_categories": [r.get("error_category") for r in rounds
                                if r.get("outcome") != "ok"],
           "problems": []}
    if len(rounds) >= n and all(r.get("outcome") != "ok" for r in rounds):
        out["problems"].append(
            f"last {n} run rounds all failed: {out['error_categories']}")
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def check_crontab() -> dict:
    out = {"missing": [], "problems": [], "warnings": []}
    try:
        cp = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        lines = [_norm(l) for l in cp.stdout.splitlines()
                 if l.strip() and not l.lstrip().startswith("#")]
    except (OSError, subprocess.TimeoutExpired) as e:
        out["warnings"].append(f"crontab -l failed: {e}")
        return out
    entries = json.loads(SCHEDULE.read_text())["entries"]
    for e in entries:
        if e.get("node", "onprem") != "onprem":
            continue
        cron = _norm(e["cron"])
        cmd = e["command"]
        cmd_alt = cmd.replace("dbwiki ", "uv run dbwiki ", 1)
        if not any(l.startswith(cron) and (cmd in l or cmd_alt in l) for l in lines):
            out["missing"].append(e["entry"])
    if out["missing"]:
        out["warnings"].append(
            f"schedule.json entries not in crontab: {out['missing']} "
            "(docs/scheduling.md says the two must be kept in sync by hand)")
    return out


def _min_context_default() -> int:
    """health.min_context from config/dbwiki.yaml — the same key `dbwiki
    health` reads, so both agree on what a usable context is."""
    try:
        return int(_section_scalar(CONFIG.read_text(), "health", "min_context", 32768))
    except (OSError, TypeError, ValueError):
        return 32768


def main() -> int:
    ap = argparse.ArgumentParser(description="dbwiki liveness watchdog")
    ap.add_argument("--base-url", default=None,
                    help="local model server root; default: whatever pi's "
                         "provider table has for agents.pi.provider")
    ap.add_argument("--min-context", type=int, default=_min_context_default())
    ap.add_argument("--timeout", type=float, default=3.0)
    ap.add_argument("--grace-min", type=int, default=20,
                    help="minutes past the due time before the tick counts as stale")
    ap.add_argument("--fail-streak", type=int, default=3,
                    help="consecutive failed run rounds that count as a problem")
    ap.add_argument("--fix", action="store_true",
                    help="reload the configured model when it is unloaded or "
                         "serving less than --min-context, then re-check")
    ap.add_argument("--context", type=int, default=98176,
                    help="context to request when --fix reloads the model")
    ap.add_argument("--fix-timeout", type=float, default=300,
                    help="seconds to wait for a --fix reload to report ready")
    ap.add_argument("--variant", default=None,
                    help="gguf variant for --fix; default: the `quant` the "
                         "server reports for the model")
    args = ap.parse_args()

    ep = _endpoint(args.base_url)
    model_server, served = check_model_server(ep, args.min_context, args.timeout)
    fix = None
    if args.fix:
        fix, model_server = apply_fix(
            ep, model_server, served, min_context=args.min_context,
            max_seq_length=args.context, variant=args.variant,
            fix_timeout=args.fix_timeout, timeout=args.timeout)

    checks = {
        "model_server": model_server,
        "tick": check_tick(args.grace_min),
        "failures": check_failures(args.fail_streak),
        "crontab": check_crontab(),
    }
    problems = [f"{k}: {p}" for k, c in checks.items() for p in c.pop("problems", [])]
    warnings = [f"{k}: {w}" for k, c in checks.items() for w in c.pop("warnings", [])]
    report = {"ts": _now().isoformat(timespec="seconds"), "ok": not problems,
              "problems": problems, "warnings": warnings, "checks": checks}
    if fix is not None:
        report["fix"] = fix
    print(json.dumps(report, indent=1))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
