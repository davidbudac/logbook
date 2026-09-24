"""Configuration loading. Everything path-like is resolved against the project root
(the directory containing config/dbwiki.yaml), so the CLI works from anywhere.

`metric_sources:` holds optional fleet-metric profiles with the same shape as
`sources:` plus `enabled:` (default false). An *enabled* metric source is
merged into `sources` with `kind: metric`, so watermarks, digests, deltas,
replay and health probes treat it exactly like a log source — there is no
parallel pipeline. Disabled (or absent) metric sources change nothing."""

import os
from pathlib import Path

import yaml

from .harness import check_adapter

#: The incident workbench (`dbwiki portal serve`). Absent block -> the server
#: still runs on these, and `dbwiki health` does not probe it: an
#: unconfigured workbench is not a problem, it is Stage 0. `push` is absent
#: here because it follows `report.push` unless the block overrides it.
PORTAL_DEFAULTS = {
    "bind": "127.0.0.1:8765",   # loopback only in Stage 1; serve() refuses the rest
    "operator_email": "",       # empty -> the wiki's git config user.email
    "lock_wait_s": 3.0,         # a commit's cap on waiting for the lock -> 423
    "links": {},
}


class Config:
    def __init__(self, raw: dict, root: Path):
        self.raw: dict = raw
        self.root: Path = root
        es = raw["elasticsearch"]
        self.es_url = es["url"].rstrip("/")
        self.es_user = es.get("username", "elastic")
        self.es_password = os.environ.get("DBWIKI_ES_PASSWORD") or es.get("password", "")
        self.wiki_repo: Path = root / raw["wiki_repo"]
        self.state_dir: Path = root / raw["state_dir"]
        self.digest_dir: Path = root / raw["digest_dir"]
        self.sources: dict[str, dict] = dict(raw["sources"])
        self.metric_sources: list[str] = []
        for name, s in (raw.get("metric_sources") or {}).items():
            if not s.get("enabled", False):
                continue
            if name in self.sources:
                raise ValueError(
                    f"metric_sources: {name!r} collides with a log source name")
            self.sources[name] = {**s, "kind": "metric"}
            self.metric_sources.append(name)
        # Trace-file lookup (src/dbwiki/trace.py): a *lookup* target keyed by
        # path, deliberately not an entry under `sources:` — everything that
        # iterates cfg.sources assumes a compacted, watermarked, health-probed
        # source, and the bursty trace stream is none of those.
        self.trace_lookup: dict = raw.get("trace_lookup") or {}
        # Every optional block is always a dict: absent and `key:` (null) both
        # read as {}. Callers index these directly — `cfg.agents.get(...)` —
        # and never hedge with getattr; a test builds a real Config
        # (tests/fixtures `make_config`) instead of a partial double.
        self.compactor: dict = raw.get("compactor") or {}
        self.agents: dict = raw.get("agents") or {}
        self.report: dict = raw.get("report") or {}
        self.research: dict = raw.get("research") or {}
        # an unsupported adapter (e.g. the removed `ollama`) fails here, at
        # load, naming the supported ones — not as a failed run in cron
        for where, block in (("agents", self.agents), ("research", self.research)):
            if block.get("adapter") is not None:
                check_adapter(block["adapter"], f"{where}.adapter")
        self.health: dict = raw.get("health") or {}
        self.alerts: dict = raw.get("alerts") or {}
        # ADR-0001: absent block -> enabled false -> escalated reports run
        # the agentic adapter locally instead of being enqueued for an analyst.
        self.analyst: dict = raw.get("analyst") or {}
        # absent block -> enabled false -> nothing exported, langfuse unimported
        self.langfuse: dict = raw.get("langfuse") or {}
        self.advisory: dict = raw.get("advisory") or {}
        self.review: dict = raw.get("review") or {}
        self.delivery: dict = raw.get("delivery") or {}
        # the incident workbench; `push` follows report.push unless the
        # block says otherwise, so a wiki with no remote does not grow one
        # push attempt per operator click
        self.portal: dict = {**PORTAL_DEFAULTS, **(raw.get("portal") or {})}
        self.portal.setdefault("push", bool(self.report.get("push")))
        self.portal_configured: bool = bool(raw.get("portal"))

    @property
    def trace_lookup_enabled(self) -> bool:
        return bool(self.trace_lookup.get("enabled", False))

    def source(self, name: str) -> dict:
        return self.sources[name]

    def source_kind(self, name: str) -> str:
        """`log` (alert/listener/dataguard text events) or `metric` (numeric
        samples) — picks the normalizer branch."""
        return self.sources[name].get("kind", "log")

    def db_query_fields(self, source: str) -> list[str]:
        """Exact ES field names (term filters / aggregations) that may carry
        the database name, tried in order. `db_fields` lists them verbatim;
        the legacy single `db_field` keeps its historical `.keyword` suffix."""
        s = self.sources[source]
        if "db_fields" in s:
            return list(s["db_fields"])
        f = s.get("db_field")
        return [f"{f}.keyword"] if f else []

    def db_value_fields(self, source: str) -> list[str]:
        """The same fields as dotted `_source` paths (no `.keyword`)."""
        return [f.removesuffix(".keyword") for f in self.db_query_fields(source)]

    def db_filter(self, source: str, db: str) -> list[dict]:
        """ES filter clauses selecting one database's events for a source.
        Matches any configured db field; when `db_service_field` is set
        (ECS listener docs carry only a TNS service name, e.g. `cdb1.world`
        or `CDB1_DGMGRL.world`), the known spelling variants of the service
        name match too. Service names are good enough to attribute events to
        a known db, but not to discover new dbs — see Compactor.discover_dbs.

        A `terms` filter on a keyword field is case-sensitive, and the case
        a service is logged in need not match the db name's (`CDB1` vs
        `cdb1.world`, `.WORLD`): db name, `_DGMGRL` suffix and domain are
        each tried as given, lower- and upper-case."""
        s = self.sources[source]
        terms: list[dict] = [{"term": {f: db}} for f in self.db_query_fields(source)]
        svc = s.get("db_service_field")
        if svc:
            domains = s.get("db_service_domains", ["world"])

            def cases(x: str) -> list[str]:
                return list(dict.fromkeys((x, x.upper(), x.lower())))

            variants: dict[str, None] = {}
            for suffix in ("", "_DGMGRL", "_dgmgrl"):
                for cased in cases(db):
                    base = cased + suffix
                    variants[base] = None
                    for d in domains:
                        variants.update((f"{base}.{dc}", None) for dc in cases(d))
            terms.append({"terms": {svc: list(variants)}})
        return [{"bool": {"should": terms, "minimum_should_match": 1}}] if terms else []

    def patterns_path(self, source: str) -> Path:
        return self.root / self.sources[source]["patterns_file"]


def find_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / "config" / "dbwiki.yaml").exists():
            return cand
    raise FileNotFoundError("config/dbwiki.yaml not found in cwd or any parent")


def load_config(root: Path | None = None) -> Config:
    root = root or find_root()
    raw = yaml.safe_load((root / "config" / "dbwiki.yaml").read_text())
    return Config(raw, root)
