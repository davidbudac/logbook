"""Regression fixture corpus: raw ES-style hits in, digest/markdown/decision
goldens out. No Elasticsearch, no LLM, no network.

A *case* is a JSON file under `cases/` holding the raw hits of one (db,
window) plus the registry state behind it; its goldens live under
`golden/<case>/`. `FakeES` replays a case's hits through a small subset of
the ES query language, so `Compactor.compact()` runs end to end offline —
including the `_check_schema` drift diagnostic.

Case *inputs* are synthetic, and have to be: a digest is compactor output, so
the events behind a real one cannot be reconstructed. `real_digests/` holds the
other half — sanitized digests captured from the live fleet, replayed through
the layers that take a digest as input (see manifest.json `real_digests`).

Goldens: `DBWIKI_UPDATE_GOLDENS=1 uv run pytest tests/test_fixtures.py`
rewrites them. A golden change means behavior changed — bump
`fixture_version` in manifest.json and review the diff.

Two side corpora use the same machinery but not manifest.json, because the
config they need is off by default: `metric_cases/` (fleet metrics, driven by
`metric_fixture_config`) and `awr/` (AWR summary files). Both are exercised by
tests/test_metrics.py; their goldens live under `golden/` like the rest.
"""

import copy
import difflib
import json
import os
from pathlib import Path

from dbwiki.compactor import Compactor
import yaml

from dbwiki.config import Config
from dbwiki.es import ES
from dbwiki.normalize import _dig, normalize  # _dig: same field lookup ES does
from dbwiki.state import Registry

FIXTURES = Path(__file__).resolve().parent
CASE_DIR = FIXTURES / "cases"
REAL_DIGEST_DIR = FIXTURES / "real_digests"
METRIC_CASE_DIR = FIXTURES / "metric_cases"
AWR_DIR = FIXTURES / "awr"
GOLDEN_DIR = FIXTURES / "golden"
LINT_WIKI = FIXTURES / "wiki_lint"
AGENT_WIKI = FIXTURES / "wiki_agent"
AGENT_CASE_DIR = FIXTURES / "agent_cases"
REPO_ROOT = FIXTURES.parents[1]

# `generated_by` carries the package version, which must not churn goldens
GENERATED_BY = "dbwiki-compactor/(fixture)"

UPDATE_GOLDENS = os.environ.get("DBWIKI_UPDATE_GOLDENS") == "1"


# ---- manifest and cases ------------------------------------------------------

def manifest() -> dict:
    return json.loads((FIXTURES / "manifest.json").read_text())


def load_case(name: str) -> dict:
    return json.loads((CASE_DIR / f"{name}.json").read_text())


# ---- real digests ------------------------------------------------------------

def real_digest_names() -> list[str]:
    return sorted(p.stem for p in REAL_DIGEST_DIR.glob("*.json"))


def load_real_digest(name: str) -> dict:
    """A sanitized digest from the live fleet, byte-for-byte as the compactor
    wrote it apart from the substitutions in manifest.json `real_digests`.
    Key order is part of the fixture: render_md walks `sources` and
    `routine_counters` in insertion order."""
    return json.loads((REAL_DIGEST_DIR / f"{name}.json").read_text())


# ---- fleet-metric cases (config `metric_sources`, disabled in the real config)

METRIC_SOURCE = {
    "index_patterns": [".ds-logs-oracle.metrics-*"],
    "timestamp_field": "@timestamp",
    "db_fields": ["oracle.database.name"],
    "patterns_file": "patterns/metrics.yaml",
    "kind": "metric",
}


def load_metric_case(name: str) -> dict:
    """A metric case lives in its own directory: `cases/` is the log corpus
    pinned by manifest.json, and metric sources are off in the real config."""
    return json.loads((METRIC_CASE_DIR / f"{name}.json").read_text())


def metric_fixture_config(tmp_path: Path):
    """`fixture_config` with the metrics source enabled — the same profile the
    commented-out `metric_sources:` block in config/dbwiki.yaml describes."""
    cfg = fixture_config(tmp_path)
    cfg.sources = {**cfg.sources, "metrics": dict(METRIC_SOURCE)}
    cfg.metric_sources = ["metrics"]
    return cfg


# ---- fake Elasticsearch ------------------------------------------------------

def _field_value(src: dict, field: str):
    """Value of a possibly-dotted ES field in a `_source`, tried nested then
    as a flat key. `.keyword` sub-fields read the parent field."""
    field = field.removesuffix(".keyword")
    v = _dig(src, *field.split("."))
    return src.get(field) if v is None else v


def _matches(src: dict, clause: dict) -> bool:
    """Evaluate one ES query clause against a `_source`. Only the clause kinds
    the compactor builds are supported; anything else is a fixture bug."""
    if "bool" in clause:
        b = clause["bool"]
        if not all(_matches(src, c) for c in b.get("filter", [])):
            return False
        should = b.get("should", [])
        return not should or any(_matches(src, c) for c in should)
    if "range" in clause:
        (field, spec), = clause["range"].items()
        v = _field_value(src, field)
        if v is None:
            return False
        if "gte" in spec and v < spec["gte"]:
            return False
        return not ("lt" in spec and v >= spec["lt"])
    if "term" in clause:
        (field, val), = clause["term"].items()
        return _field_value(src, field) == val
    if "terms" in clause:
        (field, vals), = clause["terms"].items()
        return _field_value(src, field) in vals
    if "exists" in clause:
        return _field_value(src, clause["exists"]["field"]) not in (None, "")
    raise AssertionError(f"FakeES: unsupported query clause {clause}")


class FakeES:
    """Tests-only stand-in for `es.ES` with exactly the surface the Compactor
    uses: `scan`, `count`, `search`, `dbs_in_window`. Hits come from a fixture
    case, keyed by source; the source is resolved from the index patterns the
    caller passes, the same way the real client addresses indices."""

    def __init__(self, cfg, hits_by_source: dict[str, list[dict]]):
        self.cfg = cfg
        self.pools = {}
        for name in cfg.sources:
            key = ",".join(cfg.source(name)["index_patterns"])
            self.pools[key] = self._sorted(hits_by_source.get(name, []))
        # registered so a trace-less case finds nothing rather than tripping
        # the unknown-index guard
        tl = cfg.trace_lookup
        if tl.get("enabled"):
            self.pools[",".join(tl["index_patterns"])] = \
                self._sorted(hits_by_source.get("trace", []))

    @staticmethod
    def _sorted(hits: list[dict]) -> list[dict]:
        # stable ES-like order: @timestamp asc, then a tiebreaker
        return sorted(hits, key=lambda h: (h["_source"].get("@timestamp", ""),
                                           h.get("_id", "")))

    def _pool(self, index: str) -> list[dict]:
        if index not in self.pools:
            raise AssertionError(f"FakeES: no fixture pool for index {index!r}")
        return self.pools[index]

    def _hits(self, index: str, query: dict) -> list[dict]:
        return [h for h in self._pool(index) if _matches(h["_source"], query)]

    def count(self, index: str, query: dict) -> int:
        return len(self._hits(index, query))

    def search(self, index: str, body: dict) -> dict:
        hits = self._hits(index, body.get("query", {"bool": {}}))
        for spec in reversed(body.get("sort", [])):  # single-field specs only
            (f, order), = spec.items()
            if isinstance(order, dict):     # {"order": "asc", "unmapped_type": …}
                order = order["order"]
            hits = sorted(hits, key=lambda h: _field_value(h["_source"], f) or "",
                          reverse=(order == "desc"))
        return {"hits": {"hits": hits[:body.get("size", 10)]}}

    def scan(self, index_patterns: list[str], query: dict, ts_field: str,
             page_size: int = 2000):
        index = ",".join(index_patterns)
        yield from self._hits(index, query)

    def dbs_in_window(self, index_patterns: list[str], db_fields, ts_field: str,
                      t0: str, t1: str) -> dict[str, int]:
        index = ",".join(index_patterns)
        if isinstance(db_fields, str):
            db_fields = [db_fields]
        found: dict[str, int] = {}
        for hit in self._hits(index, ES.window_query(ts_field, t0, t1)):
            for f in db_fields:
                v = _field_value(hit["_source"], f)
                if isinstance(v, str) and v:
                    found[v] = found.get(v, 0) + 1
                    break
        return found


# ---- building a case ---------------------------------------------------------

def config_path() -> Path:
    """The live config on a deployment checkout, the shipped example on a
    fresh clone (config/dbwiki.yaml is deployment-specific and untracked in
    the public tree). Both carry the same sources and compactor blocks, which
    is all the goldens depend on."""
    live = REPO_ROOT / "config" / "dbwiki.yaml"
    return live if live.exists() else REPO_ROOT / "config" / "dbwiki.yaml.example"


#: The keys `Config` requires; everything else in the YAML is optional.
MINIMAL_RAW = {
    "elasticsearch": {"url": "http://localhost:9200"},
    "wiki_repo": "wiki",
    "state_dir": ".state",
    "digest_dir": "wiki/digests",
    "sources": {},
}


#: `make_config`'s root when a test names none: a path that never exists,
#: so a test that forgot to point wiki_repo/state_dir somewhere real fails on
#: its first write instead of writing into the checkout.
NO_ROOT = Path("/nonexistent-dbwiki-test-root")


def make_config(root: Path = NO_ROOT, **overrides) -> Config:
    """A real `Config` for a unit test: the minimal YAML with `overrides`
    laid over it as top-level keys (`agents={...}`, `research={...}`,
    `wiki_repo=tmp_path / "wiki"`, ...). Paths resolve against `root` the way
    the YAML's do, and an absolute one is kept as given.

    Production code reads config through Config's attributes — every
    optional block is always a dict — so a test builds one of these rather
    than a partial SimpleNamespace the code would have to hedge against."""
    return Config({**copy.deepcopy(MINIMAL_RAW), **overrides}, Path(root))


def fixture_config(tmp_path: Path):
    """The project's real config (real pattern libraries — pattern edits are
    supposed to churn goldens) with every writable path redirected to tmp."""
    cfg = Config(yaml.safe_load(config_path().read_text()), REPO_ROOT)
    # the trace lookup is opt-in (commented out) in the example and on in the
    # live config; the corpus was recorded with it on, so pin it either way
    cfg.trace_lookup = {
        "enabled": True, "index_patterns": [".ds-logs-oracle.trace-*"],
        "timestamp_field": "@timestamp", "path_field": "oracle.trace.file",
        "slack_hours": 1, "max_paths": 6, "max_docs_per_path": 5,
        "max_excerpt_chars": 1500, "max_total_chars": 6000}
    cfg.state_dir = tmp_path / "state"
    cfg.digest_dir = tmp_path / "digests"
    cfg.wiki_repo = tmp_path / "wiki"
    # context rows are on in the real config; off here so they never churn a
    # corpus golden, and a test that wants them sets context_lines itself
    cfg.compactor = {**cfg.compactor, "context_lines": 0}
    # the live config names the workbench service; a test box has none
    cfg.portal_configured = False
    return cfg


def seed_registry(cfg, case: dict) -> None:
    """Write the per-db registry the case's deltas are measured against:
    already-known codes/services/programs and the daily counts that form the
    rate-anomaly baseline and the silence history.

    `codes_by_source` seeds per-source knowledge; the flat `codes` key seeds
    the grandfathered v1 global map (known everywhere, never first-ever again)."""
    spec = case.get("registry") or {}
    reg = Registry(cfg.state_dir / "registry" / f"{case['db']}.json")
    reg.legacy_codes.update(spec.get("codes", {}))
    for source, codes in (spec.get("codes_by_source") or {}).items():
        reg.codes.setdefault(source, {}).update(codes)
    reg.services.update(spec.get("services", {}))
    reg.programs.update(spec.get("programs", {}))
    for d in spec.get("daily_counts", []):
        reg.set_day_counts(d["day"], d["source"], d["total"],
                           d.get("counters", {}), d.get("seconds", 86400.0))
    reg.save()


def build_compactor(cfg, case: dict) -> Compactor:
    comp = Compactor(cfg)
    comp.es = FakeES(cfg, case["hits"])
    return comp


def compact_case(cfg, case: dict, persist: bool = True,
                 sources: list[str] | None = None) -> dict:
    comp = build_compactor(cfg, case)
    w = case["window"]
    return comp.compact(case["db"], w["from"], w["to"], w["day"],
                        sources=sources or case.get("sources"), persist=persist)


def normalized_events(cfg, case: dict, sources: list[str] | None = None) -> list[dict]:
    """The normalized events the compactor sees, in scan order — the same
    filter/normalize path, so this golden pins schema handling itself."""
    es = FakeES(cfg, case["hits"])
    out = []
    for name in (sources or case.get("sources") or list(cfg.sources)):
        scfg = cfg.source(name)
        w = case["window"]
        query = ES.window_query(scfg["timestamp_field"], w["from"], w["to"],
                                extra=cfg.db_filter(name, case["db"]))
        for hit in es.scan(scfg["index_patterns"], query,
                          scfg["timestamp_field"]):
            out.append(normalize(name, hit, cfg.db_value_fields(name),
                                 cfg.source_kind(name)))
    return out


def stable_digest(digest: dict) -> dict:
    """Digest with the version-bearing `generated_by` normalized away."""
    d = copy.deepcopy(digest)
    d["generated_by"] = GENERATED_BY
    return d


def ledger_entry(case: dict, digest: dict) -> dict | None:
    """The case's prior ledger entry, with `"@content_hash"` resolved to the
    digest's actual hash (how a replay of an already-ingested window looks)."""
    entry = (case.get("trigger") or {}).get("ledger_entry")
    if not entry:
        return None
    entry = dict(entry)
    if entry.get("content_hash") == "@content_hash":
        entry["content_hash"] = Compactor.content_hash(digest)
    return entry


# ---- agent evaluation candidates ---------------------------------------------

def agent_case_names() -> list[str]:
    return sorted(p.name for p in AGENT_CASE_DIR.iterdir() if p.is_dir())


def load_agent_case(name: str) -> dict:
    """A saved candidate agent output: the wiki edits it made, the result JSON
    it wrote, and what the evaluation must conclude about it."""
    d = AGENT_CASE_DIR / name
    edits = {p.relative_to(d / "edits").as_posix(): p.read_text()
             for p in sorted((d / "edits").rglob("*")) if p.is_file()}
    return {"name": name, "edits": edits,
            "result": json.loads((d / "result.json").read_text()),
            "expect": json.loads((d / "expect.json").read_text())}


def agent_wiki_files() -> dict[str, str]:
    """The clean base wiki tree candidates are replayed on top of."""
    return {p.relative_to(AGENT_WIKI).as_posix(): p.read_text()
            for p in sorted(AGENT_WIKI.rglob("*.md"))}


# ---- goldens -----------------------------------------------------------------

def golden_digest(case_name: str) -> dict:
    return json.loads((GOLDEN_DIR / case_name / "digest.json").read_text())

def _dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def assert_golden(rel: str, actual, *, text: bool = False) -> None:
    """Compare `actual` against `golden/<rel>`, or rewrite it when
    DBWIKI_UPDATE_GOLDENS=1. Mismatches report a unified diff."""
    path = GOLDEN_DIR / rel
    body = actual if text else _dumps(actual)
    if UPDATE_GOLDENS:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return
    if not path.exists():
        raise AssertionError(
            f"missing golden {rel}; create it with "
            f"DBWIKI_UPDATE_GOLDENS=1 uv run pytest tests/test_fixtures.py")
    expected = path.read_text()
    if body != expected:
        diff = "".join(difflib.unified_diff(
            expected.splitlines(keepends=True), body.splitlines(keepends=True),
            fromfile=f"golden/{rel}", tofile="actual", n=2))
        raise AssertionError(
            f"golden mismatch: {rel}\n{diff}\n"
            f"If this change is intended: rerun with DBWIKI_UPDATE_GOLDENS=1, "
            f"bump manifest.json fixture_version, and review the diff.")
