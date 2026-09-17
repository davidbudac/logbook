"""`dbwiki-researcher [--once | --watch]` — the researcher loop (ADR-0002).

```
pull exchange → claim oldest request → build agent prompt from the request
  → run agent with web=True in a scratch dir holding only the request
  → parse .agent-result.json → validate against the result contract
  → results/<run_id>-<key>.json, delete claimed request → commit, push
failure: attempts+1; after MAX_REQUEST_ATTEMPTS → requests/failed/
```

Config is a 10-line YAML (`researcher.yaml` in the cwd, or `--config`):

```
exchange:
  path: /srv/exchange        # a clone of the exchange repo (its only credential)
adapter: codex               # codex | claude | pi | ollama
model: null                  # adapter default when null
provider: null               # pi only
timeout_seconds: 600
poll_seconds: 300            # --watch sleep between empty polls
name: researcher-1           # claimed_by stamp
```

The researcher never sees a hostname, IP, database or service name: every
such thing in a request is a pseudonym (`DB_A`, `HOST_B`, …) the on-prem side
de-maps on the way back. The prompt says so, and tells the agent to echo
pseudonyms verbatim."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

from dbwiki import exchange as ex
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
    }


def _approved(request: dict) -> list[dict]:
    return [s for s in (request.get("sources") or []) if s.get("status") == "approved"]


def build_prompt(request: dict) -> str:
    kind = request.get("kind")
    today = dt.date.today().isoformat()
    head = (
        "You are the research agent of a database-operations wiki. The file "
        "`request.json` in this directory is your entire input; there is nothing "
        "else to read here and no repository to explore.\n\n"
        "PSEUDONYMS: every token shaped like DB_A, HOST_B, IP_A, SVC_A, USER_A, "
        "PATH_A or IDX_A stands for something deliberately hidden from you. Do "
        "not guess what it is, do not search for it; if you must refer to it, "
        "echo it exactly as written.\n\n"
        "WEB ACCESS: you may only fetch pages on these approved sources (their "
        "subdomains included):\n"
        + "\n".join(f"- {s['slug']}: {', '.join(s.get('domains') or [])}"
                    for s in _approved(request))
        + "\nCite nothing else. Do not browse elsewhere.\n\n"
    )
    if kind == "research":
        code = request.get("code")
        body = (
            f"TASK: research Oracle error {code}. Read the approved documentation "
            f"for it (Oracle's error-help page for {code} is the natural start), "
            f"consider the estate facts and the redacted synopsis in request.json "
            f"(message samples, co-occurring codes, occurrence counts, current "
            f"reference text), and produce:\n"
            f"- cause: plain factual prose, what the error means and what causes it "
            f"in a setup like the one described; no markdown headings, no URLs, "
            f"at most {RESULT_MAX_FIELD_CHARS} characters;\n"
            f"- action: plain factual prose (a short \"- \" bulleted list as one "
            f"string is fine), what to check and do; same limits;\n"
            f"- references: one object per page you actually read and relied on: "
            f"{{\"source\": <slug>, \"url\": <exact URL>, \"accessed\": \"{today}\"}} "
            f"(at most {RESULT_MAX_REFERENCES}; every claim must be traceable to "
            f"one of them);\n"
            f"- related_codes: other error codes the sources tie to this one "
            f"(may be empty);\n"
            f"- flags: free-text notes for a human, e.g. \"propose source: <domain>\" "
            f"if a valuable page sits on an unapproved site (may be empty).\n\n"
            f"Never restate the occurrences back, never invent a step the sources "
            f"do not support, never mention pseudonyms you did not need.\n\n"
            f"Write the result to `.agent-result.json` in this directory, exactly:\n"
            f'{{"task": "research", "kind": "research", "code": "{code}", '
            f'"cause": "...", "action": "...", "references": [...], '
            f'"related_codes": [...], "flags": [...]}}\n'
        )
    else:
        slug = request.get("slug")
        body = (
            f"TASK: review the approved source `{slug}` "
            f"(url: {request.get('url') or 'n/a'}; domains: "
            f"{', '.join(request.get('domains') or [])}). It was last reviewed "
            f"on {request.get('last_reviewed') or 'never'}. Previous notes:\n"
            f"{request.get('previous_notes') or '(none)'}\n\n"
            f"Check whether the site is still live, still authoritative for "
            f"Oracle Database error/administration content, and still where the "
            f"domains say. Then write `.agent-result.json` exactly:\n"
            f'{{"task": "research", "kind": "source-review", "slug": "{slug}", '
            f'"still_valid": true|false, "notes": "<=2000 chars", '
            f'"checked": "{today}", "proposed_status": "approved|deprecated" '
            f"(optional; only when you recommend a change)}}\n"
        )
    return head + body


def _url_host(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).hostname or "").lower()


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
            host = _url_host(str(ref.get("url") or ""))
            if not host or not any(host == d or host.endswith("." + d) for d in approved[slug]):
                problems.append(f"references[{i}]: url {ref.get('url')!r} not on {slug} domains")
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
    request = ex.claim(root, cfg["name"], kind=kind, push=cfg["push"])
    if request is None:
        return "idle"
    key = ex.request_key(request)
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
