"""The Langfuse definitions this repo owns and pushes: prompt versions, and
the model price rows `dbwiki langfuse sync` upserts (docs/langfuse.md,
the Langfuse prompt-versioning notes in docs/langfuse.md).

The repo is the source of truth (the blocks live in `config/prompts/`, read
by `dbwiki.prompts`). This module never fetches a prompt to run it: it
pushes the block a stage is about to run — the ingest template, the report
contracts, the research contract, the wiki's `AGENTS.md` for the agentic
stages — into Langfuse as a `production`-labelled prompt version, and hands
the resulting prompt client back so `observability` can link the generation to
it. A prompt edited in the Langfuse UI therefore changes nothing about a run;
only a commit does.

`PROMPTS` is the whole mapping, keyed by the `(task, mode)` pair the ledger
telemetry already carries. Escalated structured reports run a different
contract and different rules from routine ones, and the only thing that tells
them apart in the telemetry is `model_tier == "strong"` (orchestrate pins the
strong tier for every escalated report), so `lookup` folds that into the
synthetic task `report-escalated`.

Registration is idempotent by comparison, not by the API: `create_prompt` with
byte-identical text creates a new version every time (probed against the live
server 2026-09-09), so the latest `production` version is fetched and compared
first. Results are cached per process. Nothing here raises — a failure means
the generation carries no prompt link, never that a run fails.
"""

import base64
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

from . import observability
from .research_structured import _RESEARCH_CONTRACT
from .structured import (INGEST_TEMPLATE, _ESCALATED_REPORT_CONTRACT,
                         _ESCALATED_RULES, _REPORT_CONTRACT)

#: every task that can run through an agentic adapter; they all run under the
#: wiki's AGENTS.md, which is the whole static instruction block for them
AGENTIC_TASKS = ("ingest", "report", "lint", "research")

_AGENTS_MD = "dbwiki/agents-md"

# the escalated report replaces the routine contract and appends its own rules
# (structured.build_escalated_report_prompt), so both halves are the version
# that window ran under
_ESCALATED_TEXT = _ESCALATED_REPORT_CONTRACT + "\n" + _ESCALATED_RULES


def _agents_md(cfg) -> str | None:
    root = cfg.wiki_repo
    if root is None:                     # an eval's copy (evaluate._pinned)
        return None
    try:
        return (Path(root) / "AGENTS.md").read_text()
    except OSError:
        return None


#: (task, mode) -> (Langfuse prompt name, text for this config)
PROMPTS: dict[tuple[str, str], tuple[str, object]] = {
    ("ingest", "structured"):
        ("dbwiki/ingest-structured", lambda cfg: INGEST_TEMPLATE),
    ("report", "structured"):
        ("dbwiki/report-structured", lambda cfg: _REPORT_CONTRACT),
    ("report-escalated", "structured"):
        ("dbwiki/report-escalated", lambda cfg: _ESCALATED_TEXT),
    ("research", "structured"):
        ("dbwiki/research-structured", lambda cfg: _RESEARCH_CONTRACT),
    **{(task, "agentic"): (_AGENTS_MD, _agents_md) for task in AGENTIC_TASKS},
}

# (name, text) -> prompt client, so a stage registers at most once per process
# and a text change inside one process still registers
_cache: dict[tuple[str, str], object] = {}


def lookup(cfg, task: str | None, mode: str | None,
           model_tier: str | None = None) -> tuple[str, str] | None:
    """The `(prompt name, text)` this stage runs under, or None for a stage
    with no static block we version (research `offload`, or an agentic stage
    whose wiki has no AGENTS.md)."""
    if task == "report" and mode == "structured" and model_tier == "strong":
        task = "report-escalated"
    entry = PROMPTS.get((task or "", mode or ""))
    if entry is None:
        return None
    name, text_fn = entry
    try:
        text = text_fn(cfg)
    except Exception:  # noqa: BLE001 — a prompt link is never a failure mode
        return None
    return (name, text) if text else None


def register(client, name: str, text: str):
    """The Langfuse prompt client for this exact text, creating a new
    `production` version only when the stored one differs. None on any
    failure."""
    key = (name, text)
    if key in _cache:
        return _cache[key]
    try:
        try:
            stored = client.get_prompt(name, label="production", fallback=None)
        except Exception:  # noqa: BLE001 — unknown prompt, or an unreachable server
            stored = None
        if stored is not None and getattr(stored, "prompt", None) == text:
            prompt = stored
        else:
            prompt = client.create_prompt(name=name, prompt=text, type="text",
                                          labels=["production"])
    except Exception:  # noqa: BLE001 — telemetry never fails a run
        return None
    _cache[key] = prompt
    return prompt


#: Langfuse prices are per token; the config says per million, because that
#: is how every vendor's price list is written
PER_MILLION = 1e6


class _Api:
    """The Langfuse public API over urllib — `dbwiki langfuse sync` only, and
    only for what the SDK does not expose. Errors raise: this is an operator
    command run by hand, not the export path that must never fail a run."""

    def __init__(self, host: str, public_key: str, secret_key: str):
        self.host = host.rstrip("/")
        self.auth = base64.b64encode(
            f"{public_key}:{secret_key}".encode()).decode()

    def request(self, method: str, path: str, payload: dict | None = None):
        req = urllib.request.Request(
            f"{self.host}{path}", method=method,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": f"Basic {self.auth}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {path}: HTTP {e.code} {detail}") from e
        except OSError as e:
            raise RuntimeError(f"{method} {path}: {type(e).__name__}: {e}") from e
        return json.loads(body) if body else None


def api(lf_cfg: dict) -> _Api:
    """The API client for the configured host, with the same keys the SDK
    reads: config first, then the environment."""
    host = lf_cfg.get("host")
    public = lf_cfg.get("public_key") or os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = lf_cfg.get("secret_key") or os.environ.get("LANGFUSE_SECRET_KEY")
    if not host:
        raise RuntimeError("langfuse.host is not set in config/dbwiki.yaml")
    if not (public and secret):
        raise RuntimeError("no Langfuse keys: set LANGFUSE_PUBLIC_KEY and "
                           "LANGFUSE_SECRET_KEY, or langfuse.public_key / "
                           "langfuse.secret_key in config/dbwiki.yaml")
    return _Api(host, public, secret)


def _custom_models(client: _Api) -> dict[str, dict]:
    """Every model row this project owns, by name. Langfuse ships ~100
    built-in rows; those are matched by name too but never touched — a custom
    row of the same name takes precedence over a built-in one."""
    out: dict[str, dict] = {}
    page = 1
    while True:
        body = client.request("GET", f"/api/public/models?limit=100&page={page}")
        for row in (body or {}).get("data") or []:
            if not row.get("isLangfuseManaged"):
                out.setdefault(row["modelName"], row)
        pages = ((body or {}).get("meta") or {}).get("totalPages") or 1
        if page >= pages:
            return out
        page += 1


def _row(spec: dict) -> dict:
    """The API payload for one `langfuse.models:` entry. Every field is
    required: a price silently defaulting to zero is how a cost chart lies."""
    missing = [k for k in ("name", "match", "input_per_million",
                           "output_per_million") if spec.get(k) is None]
    if missing:
        raise RuntimeError(f"langfuse.models entry {spec.get('name', spec)!r} "
                           f"is missing {', '.join(missing)}")
    return {"modelName": str(spec["name"]), "matchPattern": str(spec["match"]),
            "unit": "TOKENS",
            "inputPrice": float(spec["input_per_million"]) / PER_MILLION,
            "outputPrice": float(spec["output_per_million"]) / PER_MILLION}


def _unchanged(stored: dict, want: dict) -> bool:
    if (stored.get("matchPattern") != want["matchPattern"]
            or stored.get("unit") != want["unit"]):
        return False
    return all(math.isclose(stored.get(k) or 0.0, want[k], rel_tol=1e-9,
                            abs_tol=1e-15)
               for k in ("inputPrice", "outputPrice"))


def sync_models(lf_cfg: dict) -> list[str]:
    """Upsert `langfuse.models:` into the server's model table, so generations
    from the local model and from codex get a cost instead of a blank. Model
    rows are immutable in the API (create and delete, no update), so a changed
    price is a delete followed by a create. One report line per configured
    model; running it twice changes nothing the second time."""
    specs = lf_cfg.get("models") or []
    if not specs:
        return ["models: none configured (langfuse.models is empty)"]
    client = api(lf_cfg)
    stored = _custom_models(client)
    lines = []
    for spec in specs:
        want = _row(spec)
        name = want["modelName"]
        have = stored.get(name)
        if have and _unchanged(have, want):
            lines.append(f"model {name}: unchanged")
            continue
        if have:
            client.request("DELETE", f"/api/public/models/{have['id']}")
        client.request("POST", "/api/public/models", want)
        lines.append(
            f"model {name}: {'replaced' if have else 'created'} "
            f"(match {want['matchPattern']}, "
            f"{spec['input_per_million']}/{spec['output_per_million']} USD "
            f"per 1M tokens)")
    return lines


def sync_prompts(cfg) -> list[str]:
    """Register every prompt in the table, so the versions exist before a run
    needs them (and a fresh Langfuse project is not one run behind)."""
    client = observability._get_client(cfg.langfuse)
    if client is None:
        raise RuntimeError("no Langfuse client: check `uv sync`, "
                           "langfuse.host and the keys")
    lines = []
    for name, text in all_prompts(cfg):
        prompt = register(client, name, text)
        version = getattr(prompt, "version", None) if prompt else None
        lines.append(f"prompt {name}: "
                     + (f"v{version}" if version else "FAILED"))
    client.flush()
    return lines


def all_prompts(cfg) -> list[tuple[str, str]]:
    """Every distinct `(name, text)` in the table, so `dbwiki langfuse sync`
    can create the versions before any run needs them."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for task, mode in PROMPTS:
        hit = lookup(cfg, task, mode)
        if hit is None or hit[0] in seen:
            continue
        seen.add(hit[0])
        out.append(hit)
    return out
