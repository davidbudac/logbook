"""`dbwiki eval` — one local model against a fixed dataset of real digests.

Seven digests from `tests/fixtures/real_digests/`, each joined to the golden
decision the compactor made for it, run through the real structured-ingest
chain (`build_prompt` -> `propose` -> `apply_proposal`) on a throwaway wiki.
The model judges; nothing here is written to the operator's wiki, and nothing
is graded by a second model — every score is a mechanical fact about the
proposal.

Three rules run through this module:

* **one registry, two consumers.** `SCORES` names every scored field once;
  the printed table and the Langfuse evaluators are both generated from it,
  so a score can never exist in one and be missing from the other.
* **one task closure.** `run` does the per-item work in exactly one place,
  called either by `client.run_experiment` or by the local loop, so an
  experiment run and a langfuse-less run cannot measure different things.
* **unknown is never estimated.** Token counts and duration come out of the
  harness telemetry as it reported them; "unknown" and absent both become
  None and are left out of the averages, same rule as stats.py.

Langfuse is optional and never a failure mode: no keys, no package, or a
broken server all degrade to the local table with one stderr warning.
"""

import copy
import json
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import structured
from .digest_md import render_md
from .observability import warn_once

DATASET_NAME = "dbwiki-ingest"

#: every scored field, once. (name, langfuse data type) — the table columns
#: and the evaluators are both built from this tuple.
SCORES = (
    ("parsed_ok", "BOOLEAN"),
    ("apply_ok", "BOOLEAN"),
    ("codes_allowlisted", "NUMERIC"),
    ("incident_consistent", "BOOLEAN"),
    ("flags", "NUMERIC"),
    ("duration_s", "NUMERIC"),
    ("input_tokens", "NUMERIC"),
    ("output_tokens", "NUMERIC"),
)

_INDEX_MD = ("---\ntype: index\n---\n\n# Logbook\n\n## Databases\n\n"
             "(pages appear as ingestion runs)\n")
_LOG_MD = "---\ntype: log\n---\n\n# Log\n"

def _warn_once(key: str, msg: str) -> None:
    warn_once(f"eval:{key}", f"eval: {msg}")


# ---- dataset ------------------------------------------------------------------

def fixtures_dir() -> Path:
    """`tests/fixtures/`, derived from this file rather than the cwd — the CLI
    is run from a crontab and from the wiki repo, never from the source tree."""
    return Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def dataset_items(fixtures: Path | None = None) -> list[dict]:
    """The `dbwiki-ingest` items: every real digest fixture joined to the
    golden decision for the same name. `expected` is what the deterministic
    pipeline already decided (outcome, tier) plus the codes the digest carries
    — the allowlist the model's `error_updates` are graded against."""
    root = Path(fixtures) if fixtures is not None else fixtures_dir()
    items = []
    for path in sorted((root / "real_digests").glob("*.json")):
        digest = json.loads(path.read_text())
        decision = json.loads(
            (root / "golden" / "real" / path.stem / "decision.json").read_text())
        items.append({
            "id": path.stem,
            "input": {"db": digest["db"], "digest": digest},
            "expected": {"outcome": decision["outcome"],
                         "model_tier": decision["model_tier"],
                         "codes": structured.digest_codes(digest)},
            "metadata": {"source": f"tests/fixtures/real_digests/{path.name}"},
        })
    return items


# ---- one item -----------------------------------------------------------------

def _num(v):
    """The value if it is a real number, else None. Bools are not numbers, and
    the harness's "unknown" is not a measurement."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return v


def _telemetry(tele: dict) -> dict:
    """Duration and token counts as the harness reported them. `propose` fills
    its telemetry dict before it raises, so this is read on the failure path
    too."""
    usage = tele.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {"duration_s": _num(tele.get("duration_s")),
            "input_tokens": _num(usage.get("input_tokens")),
            "output_tokens": _num(usage.get("output_tokens"))}


def _throwaway_wiki(root: Path, db: str, digest: dict) -> Path:
    """The smallest tree `build_prompt` and `apply_proposal` need: this day's
    digest plus the index and log they append to. Nothing else exists, so a
    proposal is graded against the digest alone and no run can see what an
    earlier run wrote."""
    day = digest["window"]["day"]
    (root / "digests" / db).mkdir(parents=True)
    (root / "digests" / db / f"{day}.md").write_text(render_md(digest))
    (root / "digests" / db / f"{day}.json").write_text(json.dumps(digest))
    (root / "index.md").write_text(_INDEX_MD)
    (root / "log.md").write_text(_LOG_MD)
    return root


def _codes_allowlisted(proposal: dict, digest: dict) -> float:
    """Share of the proposal's error codes the digest actually carries. A
    proposal naming no codes scores 1.0: it invented nothing."""
    proposed = [u["code"] for u in proposal["error_updates"]]
    if not proposed:
        return 1.0
    allowed = structured.digest_codes(digest)
    return sum(1 for c in proposed if c in allowed) / len(proposed)


def _incident_consistent(proposal: dict, wiki: Path) -> bool:
    """False only when the model asks to update an incident page that is not
    open in this wiki. `apply_proposal` flags that instead of raising, so
    without this score a hallucinated incident reference reads as a clean run."""
    inc = proposal["incident"]
    if inc["action"] != "update":
        return True
    # same normalization apply_proposal resolves the page with, so the score
    # and the writer cannot disagree about what the model named
    target = (inc["existing_page"] or "").strip().removeprefix("./")
    return any(i.path == target for i in structured.all_open_incidents(wiki))


def empty_verdict() -> dict:
    """The verdict before anything was measured. The scores that only exist
    once a proposal parsed start as None, not as 0 / 0.0 / False: a failed
    item averaged in as "0 flags, 0.0 codes allowlisted" made a model that
    fails half the time look better than one that always answers."""
    return {"parsed_ok": False, "apply_ok": False, "codes_allowlisted": None,
            "incident_consistent": None, "flags": None, "duration_s": None,
            "input_tokens": None, "output_tokens": None, "error": None}


def evaluate_item(item: dict, cfg) -> tuple[dict | None, dict]:
    """One dataset item through the real ingest chain, scored. Returns the
    proposal (None when the model never produced a valid one) and the verdict.

    Never raises: a model that answers garbage twice, a harness that times out
    and an apply that blows up are all results of the experiment, not reasons
    to lose the six items after this one."""
    verdict = empty_verdict()
    proposal = None
    tele: dict = {}
    try:
        db = item["input"]["db"]
        digest = item["input"]["digest"]
        day = digest["window"]["day"]
        with tempfile.TemporaryDirectory() as tmp:
            wiki = _throwaway_wiki(Path(tmp), db, digest)
            proposal = structured.propose(
                structured.build_prompt(db, digest, wiki), cfg,
                escalate=item["expected"]["model_tier"] == "strong",
                telemetry=tele)
            verdict["parsed_ok"] = True
            verdict["flags"] = len(proposal["flags"])
            verdict["codes_allowlisted"] = _codes_allowlisted(proposal, digest)
            # before the apply, which may open the very incident being scored
            verdict["incident_consistent"] = _incident_consistent(proposal, wiki)
            result = structured.apply_proposal(
                wiki, db, digest, f"digests/{db}/{day}.md", proposal,
                digest["window"]["to"])
            verdict["apply_ok"] = True
            # the rails add their own flags (a dropped code, a broken link),
            # so the applied count supersedes the proposal's; an apply that
            # raised leaves the proposal's count standing
            verdict["flags"] = len(result["flags"])
    except Exception as e:  # noqa: BLE001 — a failed item is a measurement
        verdict["error"] = f"{type(e).__name__}: {e}"
    verdict.update(_telemetry(tele))
    return proposal, verdict


# ---- table --------------------------------------------------------------------

_HEAD = ("item", *(f for f, _ in SCORES), "error")
# rows stream as items finish, so the widths cannot be fitted to the data:
# the id column takes the longest fixture name and the rest their header.
_WIDTHS = (26, *(max(len(f), 7) for f, _ in SCORES), 0)


def _cell(v) -> str:
    if v is None:
        return "-"
    return f"{v:g}" if isinstance(v, float) else str(v)


def _line(cells) -> str:
    return "  " + "  ".join(c.ljust(w) for c, w in zip(cells, _WIDTHS)).rstrip()


def _print_header(model: str, run_name: str, n: int) -> None:
    print(f"eval {DATASET_NAME} — {n} item(s), model {model}, run {run_name}")
    print()
    print(_line(_HEAD))
    print("  " + "  ".join("-" * w for w in _WIDTHS).rstrip(), flush=True)


def _print_row(item: dict, verdict: dict) -> None:
    print(_line((item["id"],
                 *(_cell(verdict.get(f)) for f, _ in SCORES),
                 (verdict["error"] or "")[:60])), flush=True)


def _print_summary(results: list) -> None:
    cells = ["ALL"]
    for field, data_type in SCORES:
        vals = [v[field] for _, _, v in results if v[field] is not None]
        if not vals:
            cells.append("-")
        elif data_type == "BOOLEAN":
            cells.append(f"{sum(1 for v in vals if v)}/{len(vals)}")
        else:
            cells.append(_cell(round(sum(vals) / len(vals), 3)))
    errors = sum(1 for _, _, v in results if v["error"])
    cells.append(f"{errors} error(s)")
    print(_line(cells), flush=True)


# ---- langfuse -----------------------------------------------------------------

# observability._get_client caches one client for the export path and marks it
# permanently failed on the first error; an eval run is a foreground command
# that should build its own and warn on its own terms, so the few lines below
# are duplicated deliberately rather than imported from another module's
# private helper.
def _client(lf_cfg: dict):
    """The Langfuse client for this run, or None when disabled, uninstalled or
    misconfigured. Never raises."""
    if not lf_cfg.get("enabled"):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        _warn_once("import", "langfuse.enabled is true but the langfuse "
                   "package is not installed (uv sync); "
                   "this run will only print the table")
        return None
    try:
        return Langfuse(public_key=lf_cfg.get("public_key"),
                        secret_key=lf_cfg.get("secret_key"),
                        host=lf_cfg.get("host"),
                        environment=lf_cfg.get("environment"))
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _warn_once("init", f"client init failed: {type(e).__name__}: {e}")
        return None


def _evaluator(field: str, data_type: str):
    """One Langfuse evaluator reading one field out of the verdict the task
    returned. Built from SCORES so the experiment scores exactly what the
    table prints; a field the run could not measure is skipped, never zeroed."""
    def evaluate(*, input, output, expected_output=None, metadata=None,
                 **kwargs):
        value = (output or {}).get(field)
        if value is None:
            return None
        from langfuse import Evaluation
        return Evaluation(name=field, value=float(value), data_type=data_type,
                          comment=(output or {}).get("error"))
    evaluate.__name__ = f"evaluate_{field}"
    return evaluate


EVALUATORS = [_evaluator(field, data_type) for field, data_type in SCORES]


def _app_sha() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=fixtures_dir().parents[1], capture_output=True,
                             text=True, timeout=10)
    except Exception:  # noqa: BLE001 — a missing sha is metadata, not an error
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _remote_data(client, name: str, items: list[dict]):
    """The dataset's own items, so the experiment's runs link to the dataset in
    the Langfuse UI. Falls back to the local items whenever the server cannot
    supply them — an unpublished dataset must not stop the eval."""
    ids = {i["id"] for i in items}
    try:
        fetched = [it for it in client.get_dataset(name).items
                   if getattr(it, "id", None) in ids]
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _warn_once("dataset", f"could not read dataset {name!r} "
                   f"({type(e).__name__}: {e}); running the local items")
        return items
    if not fetched:
        _warn_once("dataset", f"dataset {name!r} has none of these items "
                   f"(run `dbwiki eval sync`); running the local items")
        return items
    return fetched


def _run_experiment(client, *, name, run_name, data, task, metadata) -> None:
    try:
        client.run_experiment(name=name, run_name=run_name, data=data,
                              task=task, evaluators=EVALUATORS,
                              metadata=metadata,
                              # one local model server, minutes per item
                              max_concurrency=1)
        # the CLI is short-lived; hand the events over before it exits
        client.flush()
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _warn_once("experiment", f"{type(e).__name__}: {e}")


# ---- run ----------------------------------------------------------------------

def _as_item(item) -> dict:
    """Our own item, or a Langfuse DatasetItem, as one Item dict. The
    experiment hands the task whatever the server stored, and the rest of this
    module only knows the local shape."""
    if isinstance(item, dict):
        return item
    return {"id": getattr(item, "id", ""),
            "input": getattr(item, "input", None) or {},
            "expected": getattr(item, "expected_output", None) or {},
            "metadata": getattr(item, "metadata", None) or {}}


def _pinned(cfg, model: str, provider: str | None):
    """`cfg` with both pi tiers pinned to one model, and no wiki. Config is a
    plain object shared with the caller, so the agents dict is rebuilt rather
    than mutated — an eval must not change the ingest config of the process
    that ran it.

    `wiki_repo` is cleared because `structured.generate` passes it as the
    model call's working directory: an eval that must not touch the
    operator's wiki has no business running inside it, and on a checkout
    where that directory does not exist (a worktree, a fresh clone) the
    subprocess dies before the model is ever asked. The call is one-shot
    text with no file access, so it needs no particular directory."""
    out = copy.copy(cfg)
    agents = dict(cfg.agents)
    pi = dict(agents.get("pi") or {})
    pi["cheap"] = pi["strong"] = model
    if provider is not None:
        pi["provider"] = provider
    agents["pi"] = pi
    out.agents = agents
    out.wiki_repo = None
    return out


def run(items: list[dict], cfg, *, model: str, provider: str | None = None,
        name: str = DATASET_NAME, run_name: str | None = None) -> list[tuple]:
    """Evaluate every item with `model`, printing the table as it goes, and
    return one (item, proposal, verdict) triple per item.

    With Langfuse enabled the same work runs inside `run_experiment`, so the
    traces and the printed table come from one execution of one closure. The
    experiment is the transport, never the measurement: if it cannot run, the
    items are evaluated locally and the table is identical."""
    cfg = _pinned(cfg, model, provider)
    run_name = run_name or f"{model} {datetime.now(UTC):%Y-%m-%d}"
    results: list[tuple] = []
    _print_header(model, run_name, len(items))

    def task(*, item, **kwargs):
        it = _as_item(item)
        proposal, verdict = evaluate_item(it, cfg)
        results.append((it, proposal, verdict))
        _print_row(it, verdict)
        return {**verdict, "proposal": proposal}

    client = _client(cfg.langfuse)
    if client is not None:
        _run_experiment(client, name=name, run_name=run_name,
                        data=_remote_data(client, name, items), task=task,
                        metadata={"model": model, "provider": provider or "",
                                  "app_sha": _app_sha()})
    # the experiment already called the task for every item it ran. Whatever
    # it did not run — it never started, died midway, or the remote dataset
    # lacks an item — runs locally, so the summary covers every item asked for
    done = {str(it.get("id")) for it, _, _ in results}
    missing = [item for item in items if str(item["id"]) not in done]
    if results and missing:
        _warn_once("partial", f"the experiment ran {len(done)} of "
                   f"{len(items)} item(s); running the other {len(missing)} "
                   f"locally")
    for item in missing:
        task(item=item)
    _print_summary(results)
    return results


def sync(items: list[dict], cfg, *, name: str = DATASET_NAME) -> int:
    """Publish the dataset to Langfuse and return the number of items written.

    Keyed on the fixture name, so re-running it after a fixture changes
    updates that item in place instead of growing a second copy of the
    dataset."""
    client = _client(cfg.langfuse)
    if client is None:
        _warn_once("sync", "no langfuse client; nothing was published")
        return 0
    try:
        client.create_dataset(
            name=name,
            description="Real Oracle log digests from tests/fixtures/"
                        "real_digests, with the golden ingest decision each one "
                        "already has.")
        for item in items:
            client.create_dataset_item(dataset_name=name, id=item["id"],
                                       input=item["input"],
                                       expected_output=item["expected"],
                                       metadata=item["metadata"])
        client.flush()
    except Exception as e:  # noqa: BLE001 — telemetry is never a failure mode
        _warn_once("sync", f"{type(e).__name__}: {e}")
        return 0
    return len(items)
