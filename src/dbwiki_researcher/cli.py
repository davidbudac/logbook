"""`dbwiki-researcher [--once | --watch]` — the researcher loop (ADR-0002).

```
pull exchange → reclaim stale claims → claim oldest request → build agent prompt from the request
  → run agent with web=True in a scratch dir holding only the request
  → parse .agent-result.json → validate against the result contract
  → results/<run_id>-<key>.json, delete claimed request → commit, push
failure: attempts+1; after MAX_REQUEST_ATTEMPTS → requests/failed/
```

Config is a 10-line YAML (`researcher.yaml` in the cwd, or `--config`):

```
exchange:
  path: /srv/exchange        # a clone of the exchange repo (its only credential)
adapter: codex               # codex | claude | pi
model: null                  # adapter default when null
provider: null               # pi only
timeout_seconds: 600
poll_seconds: 300            # --watch sleep between empty polls
name: researcher-1           # claimed_by stamp
stale_hours: 26              # an older claim is a dead run's: back to pending
```

The researcher never sees a hostname, IP, database or service name: every
such thing in a request is a pseudonym (`DB_A`, `HOST_B`, …) the on-prem side
de-maps on the way back. The prompt says so, and tells the agent to echo
pseudonyms verbatim."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

from dbwiki import exchange as ex
from dbwiki import prompts
from dbwiki.harness import HarnessError, run_agent

RESULT_MAX_FIELD_CHARS = 4000
RESULT_MAX_REFERENCES = 8
DEFAULT_TIMEOUT = 600
DEFAULT_POLL = 300


def load_config(path: Path | None) -> dict:
    p = Path(path) if path else Path("researcher.yaml")
    if not p.is_file():
        raise SystemExit(f"researcher config not found: {p} (see --config)")
    raw = yaml.safe_load(p.read_text()) or {}
    xpath = (raw.get("exchange") or {}).get("path")
    if not xpath:
        raise SystemExit("researcher config: exchange.path is required")
    root = Path(xpath)
    if not root.is_absolute():
        root = p.parent / root
    return {
        "exchange": root, "adapter": raw.get("adapter") or "codex",
        "model": raw.get("model"), "provider": raw.get("provider"),
        "timeout": int(raw.get("timeout_seconds") or DEFAULT_TIMEOUT),
        "poll": int(raw.get("poll_seconds") or DEFAULT_POLL),
        "name": raw.get("name") or "researcher",
        "push": bool(raw.get("push", True)),
        "stale_hours": float(raw.get("stale_hours") or ex.STALE_CLAIM_HOURS),
    }


def _approved(request: dict) -> list[dict]:
    return [s for s in (request.get("sources") or []) if s.get("status") == "approved"]


# The prompt text. Mustache `{{field}}`s rather than str.format, because the
# result shapes are literal JSON; `_fill` substitutes them in one pass, so a
# request value that happens to contain `{{today}}` is sent as written.
_PREAMBLE = prompts.load("researcher/preamble")
_RESEARCH = prompts.load("researcher/research")
_SOURCE_REVIEW = prompts.load("researcher/source-review")
_FIELD_RE = re.compile(r"\{\{(\w+)\}\}")


def _fill(template: str, **fields) -> str:
    """`template` with every `{{name}}` replaced by `str(fields[name])`; a
    field the template names but the caller did not pass is a KeyError."""
    return _FIELD_RE.sub(lambda m: str(fields[m.group(1)]), template)


def build_prompt(request: dict, *, today: str | None = None) -> str:
    """The agent's prompt for one request. The text is
    `config/prompts/researcher/{preamble,research,source-review}.md`; this
    only fills its `{{field}}`s."""
    kind = request.get("kind")
    today = today or dt.date.today().isoformat()
    head = _fill(_PREAMBLE, sources="\n".join(
        f"- {s['slug']}: {', '.join(s.get('domains') or [])}"
        for s in _approved(request))) + "\n\n"
    if kind == "research":
        body = _fill(_RESEARCH, code=request.get("code"), today=today,
                     max_chars=RESULT_MAX_FIELD_CHARS,
                     max_refs=RESULT_MAX_REFERENCES)
    else:
        body = _fill(_SOURCE_REVIEW, slug=request.get("slug"), today=today,
                     url=request.get("url") or "n/a",
                     domains=", ".join(request.get("domains") or []),
                     last_reviewed=request.get("last_reviewed") or "never",
                     previous_notes=request.get("previous_notes") or "(none)")
    return head + body + "\n"


def validate_result(request: dict, result: dict) -> list[str]:
    """The result contract, checked against the request itself: right kind
    and code/slug, prose sizes, references only on approved sources' domains.
    The on-prem side re-validates against the wiki; this catches the cheap
    mistakes before they cost a round trip."""
    kind = request.get("kind")
    problems: list[str] = []
    if result.get("kind", kind) != kind:
        problems.append(f"kind={result.get('kind')!r}, expected {kind!r}")
    if kind == "research":
        if result.get("code", request.get("code")) != request.get("code"):
            problems.append(f"code={result.get('code')!r}, expected {request.get('code')!r}")
        for key in ("cause", "action"):
            v = result.get(key)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"{key}: missing or empty")
            elif len(v) > RESULT_MAX_FIELD_CHARS:
                problems.append(f"{key}: {len(v)} chars > {RESULT_MAX_FIELD_CHARS}")
            elif p := ex.prose_link_problem(v):
                problems.append(f"{key}: {p} (no links in prose; cite under references)")
        refs = result.get("references")
        if not isinstance(refs, list) or not refs:
            problems.append("references: missing or empty")
            refs = []
        if len(refs) > RESULT_MAX_REFERENCES:
            problems.append(f"references: {len(refs)} > {RESULT_MAX_REFERENCES}")
        approved = {s["slug"]: [str(d).lower() for d in (s.get("domains") or [])]
                    for s in _approved(request)}
        for i, ref in enumerate(refs):
            if not isinstance(ref, dict):
                problems.append(f"references[{i}]: not an object")
                continue
            slug = str(ref.get("source") or "").removeprefix("sources/")
            if slug not in approved:
                problems.append(f"references[{i}]: source {slug!r} is not approved")
                continue
            if p := ex.citation_url_problem(ref.get("url"), approved[slug]):
                problems.append(f"references[{i}]: url {str(ref.get('url'))[:120]!r} "
                                f"not on {slug} domains ({p})")
            try:
                dt.date.fromisoformat(str(ref.get("accessed") or "")[:10])
            except ValueError:
                problems.append(f"references[{i}]: accessed is not a date")
        for key in ("related_codes", "flags"):
            v = result.get(key)
            if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                problems.append(f"{key}: must be a list of strings")
    elif kind == "source-review":
        if result.get("slug", request.get("slug")) != request.get("slug"):
            problems.append(f"slug={result.get('slug')!r}, expected {request.get('slug')!r}")
        if not isinstance(result.get("still_valid"), bool):
            problems.append("still_valid: must be a boolean")
        try:
            dt.date.fromisoformat(str(result.get("checked") or "")[:10])
        except ValueError:
            problems.append("checked: not a date")
        notes = result.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
            problems.append("notes: must be a string of at most 2000 chars")
        ps = result.get("proposed_status")
        if ps is not None and ps not in ("approved", "deprecated"):
            problems.append("proposed_status: approved|deprecated")
    else:
        problems.append(f"unknown request kind {kind!r}")
    return problems


def _shape(request: dict, result: dict) -> dict:
    """The result record as the on-prem side expects it (kind/code|slug from
    the request, never from the agent; agent-side keys it does not know
    dropped)."""
    if request.get("kind") == "research":
        keep = ("cause", "action", "references", "related_codes", "flags")
        out = {"schema_version": 1, "kind": "research", "code": request.get("code")}
    else:
        keep = ("still_valid", "notes", "checked", "proposed_status")
        out = {"schema_version": 1, "kind": "source-review", "slug": request.get("slug")}
    out.update({k: result[k] for k in keep if k in result})
    return out


def run_one(cfg: dict, request: dict, *, agent=None) -> dict:
    """Run the agent for one claimed request in a scratch directory holding
    only `request.json`; return the shaped, validated result. Raises
    HarnessError / ValueError on failure (the caller decides requeue vs fail)."""
    agent = agent or run_agent
    scratch = Path(tempfile.mkdtemp(prefix="dbwiki-researcher-"))
    try:
        (scratch / "request.json").write_text(json.dumps(
            {k: v for k, v in request.items() if not k.startswith("_")}, indent=1))
        prompt = build_prompt(request)
        tele: dict = {}
        result = agent(cfg["adapter"], prompt, scratch, cfg.get("model"),
                       cfg["timeout"], web=True, provider=cfg.get("provider"),
                       telemetry=tele)
        problems = validate_result(request, result)
        if problems:
            raise ValueError("result rejected: " + "; ".join(problems))
        shaped = _shape(request, result)
        shaped["telemetry"] = {**tele, "task": "research", "adapter": cfg["adapter"],
                               "model": tele.get("model") or cfg.get("model"),
                               "researcher": cfg["name"]}
        return shaped
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def tick(cfg: dict, *, agent=None, kind: str | None = None) -> str:
    """Claim → run → complete/fail one request. Returns a one-line outcome
    (also printed): `idle`, `done <key>`, `requeued <key>`, `failed <key>`."""
    root = cfg["exchange"]
    request = ex.claim(root, cfg["name"], kind=kind, push=cfg["push"],
                       stale_hours=cfg.get("stale_hours", ex.STALE_CLAIM_HOURS))
    if request is None:
        return "idle"
    key = ex.request_key(request)
    if bad := ex.validate_request(request):
        # a forged or corrupt request: its ids would become file names and
        # prompt text; fail it for good without running the agent
        print(f"dbwiki-researcher: invalid request {request.get('_file')}: "
              f"{'; '.join(bad)[:300]}", file=sys.stderr)
        ex.fail_request(root, request, "invalid-request", push=cfg["push"], max_attempts=1)
        return f"failed {key}"
    try:
        result = run_one(cfg, request, agent=agent)
    except (HarnessError, ValueError, OSError) as exc:
        category = "validation" if isinstance(exc, ValueError) else "harness"
        print(f"dbwiki-researcher: {request.get('kind')} {key}: {category}: "
              f"{str(exc)[:300]}", file=sys.stderr)
        back = ex.fail_request(root, request, category, push=cfg["push"])
        return f"{'failed' if back['_terminal'] else 'requeued'} {key}"
    ex.complete(root, request, result, push=cfg["push"])
    return f"done {key}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dbwiki-researcher",
                                description="ADR-0002 researcher: claim anonymized "
                                            "research requests, answer them with a "
                                            "web-capable agent, push results")
    p.add_argument("--config", help="researcher.yaml (default: ./researcher.yaml)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="one request, then exit (default)")
    g.add_argument("--watch", action="store_true",
                   help="loop: drain the queue, sleep poll_seconds, repeat")
    p.add_argument("--kind", choices=["research", "source-review"])
    p.add_argument("--drain", action="store_true",
                   help="with --once: keep going until the queue is empty")
    args = p.parse_args(argv)
    cfg = load_config(Path(args.config) if args.config else None)
    if not ex.is_exchange(cfg["exchange"]):
        print(f"exchange.path {cfg['exchange']} is not a git clone", file=sys.stderr)
        return 2
    while True:
        outcome = tick(cfg, kind=args.kind)
        print(outcome)
        if args.watch:
            if outcome == "idle":
                time.sleep(cfg["poll"])
            continue
        if args.drain and outcome != "idle":
            continue
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
