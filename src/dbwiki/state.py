"""Machinery state: per-DB registries (what we've seen before), watermarks,
and the digest ingest ledger. All small JSON files under .state/.

Every file carries a top-level "schema_version" (registries version
independently of the rest — see REGISTRY_SCHEMA_VERSION). A file written in
an older schema, or without one at all, is read in its old shape; the first
time it's rewritten it is migrated and the original is preserved once as
<name>.json.bak."""

import json
import os
from pathlib import Path

SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 2  # v2 keys `codes` by source (v1 had one global map)


def _load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def atomic_write_text(path: Path, text: str) -> None:
    """Write through a sibling tmp file and `os.replace`, so a reader never
    sees a half-written file and a crash leaves the previous one intact.
    Everything here is read while it is being written — the agent stages and
    the ELK shipper read digests and state files on their own schedule — so
    this is the only way anything in this project writes a whole file. The
    tmp name carries the pid: two writers each rename their own complete
    file, and neither can publish the other's half."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)   # a full disk must not leave litter
        raise


def _save(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True))


def _check_version(obj: dict, path: Path, current: int = SCHEMA_VERSION) -> None:
    v = obj.get("schema_version", 0)
    if v > current:
        raise ValueError(
            f"{path}: schema_version {v} is newer than the version this "
            f"code supports ({current})")


def _backup_if_legacy(path: Path, current: int = SCHEMA_VERSION) -> None:
    """Before a file written in an older schema (unversioned, or an earlier
    version) is rewritten in the current shape, preserve the original once as
    <name>.json.bak."""
    if not path.exists() or _load(path, {}).get("schema_version", 0) >= current:
        return
    bak = path.with_name(path.name + ".bak")
    if not bak.exists():
        bak.write_text(path.read_text())


def _load_wrapped(path: Path, key: str) -> dict:
    """Load a versioned {"schema_version": N, key: {...}} file, or a legacy
    bare flat mapping (no schema_version) as-is."""
    d = _load(path, {})
    if "schema_version" not in d:
        return d  # legacy: bare flat mapping
    _check_version(d, path)
    return d.get(key, {})


def _save_wrapped(path: Path, key: str, mapping: dict) -> None:
    _backup_if_legacy(path)
    _save(path, {"schema_version": SCHEMA_VERSION, key: mapping})


class Registry:
    """Cumulative per-DB memory used for delta detection.

    codes/services/programs record first_seen/last_seen so 'new in window W'
    is deterministic on re-runs. daily_counts keeps per-day per-source event
    counts, per-counter routine counts, and the window coverage in seconds
    behind them (rate-anomaly baseline).

    codes are keyed by source ({source: {code: {...}}}): the same ORA code is
    tracked separately per log, so a code long known in the alert log is still
    news the first time it turns up in the dataguard log. legacy_codes holds
    the v1 global map, which carried no source attribution — see
    new_codes_in_window."""

    def __init__(self, path: Path):
        self.path = path
        d = _load(path, {})
        _check_version(d, path, REGISTRY_SCHEMA_VERSION)
        if d.get("schema_version", 0) < REGISTRY_SCHEMA_VERSION:
            self.codes: dict = {}
            self.legacy_codes: dict = d.get("codes", {})
        else:
            self.codes = d.get("codes", {})
            self.legacy_codes = d.get("legacy_codes", {})
        self.services: dict = d.get("services", {})
        self.programs: dict = d.get("programs", {})
        self.daily_counts: dict = d.get("daily_counts", {})
        for coll in (*self.codes.values(), self.legacy_codes,
                     self.services, self.programs):
            for v in coll.values():
                v.pop("count", None)

    def save(self, keep_days: int = 60) -> None:
        days = sorted(self.daily_counts)
        for day in days[:-keep_days]:
            del self.daily_counts[day]
        _backup_if_legacy(self.path, REGISTRY_SCHEMA_VERSION)
        _save(self.path, {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "codes": self.codes,
            "legacy_codes": self.legacy_codes,
            "services": self.services,
            "programs": self.programs,
            "daily_counts": self.daily_counts,
        })

    @staticmethod
    def _note(d: dict, key: str, ts: str) -> None:
        """Idempotent across window re-runs: only min/max timestamps, no
        counters (a per-event counter would inflate every re-compaction)."""
        if not key:
            return
        cur = d.get(key)
        if cur is None:
            d[key] = {"first_seen": ts, "last_seen": ts}
        else:
            if ts < cur["first_seen"]:
                cur["first_seen"] = ts
            if ts > cur["last_seen"]:
                cur["last_seen"] = ts

    def note_code(self, source: str, code: str, ts: str) -> None:
        if not code:
            return  # never materialize an empty bucket for a source
        self._note(self.codes.setdefault(source, {}), code, ts)

    def note_service(self, svc: str, ts: str) -> None:
        self._note(self.services, svc, ts)

    def note_program(self, prog: str, ts: str) -> None:
        self._note(self.programs, prog, ts)

    def new_in_window(self, d: dict, t0: str, t1: str) -> list[str]:
        return sorted(k for k, v in d.items() if t0 <= v["first_seen"] < t1)

    def new_codes_in_window(self, source: str, t0: str, t1: str) -> list[str]:
        """Codes first seen in `source` within [t0, t1). Anything inherited
        from the v1 global map is grandfathered for every source and can never
        be first-ever again: that knowledge has no source attribution, so
        without this the migration itself would fake a wave of deltas."""
        return [c for c in self.new_in_window(self.codes.get(source, {}), t0, t1)
                if c not in self.legacy_codes]

    def set_day_counts(self, day: str, source: str, total: int, counters: dict,
                       window_seconds: float) -> None:
        """Record a day's counts for a source. A shorter window never
        overwrites a longer one, so an ad-hoc partial re-run cannot degrade
        a full-day record (intra-day `run` ticks grow monotonically)."""
        cur = self.daily_counts.get(day, {}).get(source)
        if cur and cur.get("seconds", 86400.0) > window_seconds:
            return
        self.daily_counts.setdefault(day, {})[source] = {
            "total": total, "counters": counters, "seconds": window_seconds}

    def baseline(self, day: str, source: str, counter: str,
                 n_days: int) -> list[tuple[int, float]]:
        """(count, window_seconds) pairs for up to n_days strictly before
        `day`. Entries written before coverage was tracked count as full days."""
        prior = sorted(d for d in self.daily_counts if d < day)[-n_days:]
        out = []
        for d in prior:
            sc = self.daily_counts[d].get(source)
            if sc:
                out.append((sc["counters"].get(counter, 0),
                            sc.get("seconds", 86400.0)))
        return out

    def active_days_before(self, day: str, source: str, n: int) -> int:
        """How many of the last n known days before `day` had events for source."""
        prior = sorted(d for d in self.daily_counts if d < day)[-n:]
        return sum(1 for d in prior if self.daily_counts[d].get(source, {}).get("total", 0) > 0)


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = state_dir

    def registry(self, db: str) -> Registry:
        return Registry(self.dir / "registry" / f"{db}.json")

    def known_dbs(self) -> list[str]:
        reg_dir = self.dir / "registry"
        if not reg_dir.exists():
            return []
        return sorted(p.stem for p in reg_dir.glob("*.json"))

    # watermarks: last compacted window end, per db
    def get_watermarks(self) -> dict:
        return _load_wrapped(self.dir / "watermarks.json", "watermarks")

    def set_watermark(self, db: str, window_end: str) -> None:
        w = self.get_watermarks()
        w[db] = window_end
        _save_wrapped(self.dir / "watermarks.json", "watermarks", w)

    # ingest ledger: digest path -> status
    def get_ledger(self) -> dict:
        return _load_wrapped(self.dir / "ingest_ledger.json", "entries")

    def set_ledger_entry(self, digest_rel: str, entry: dict) -> None:
        led = self.get_ledger()
        led[digest_rel] = entry
        _save_wrapped(self.dir / "ingest_ledger.json", "entries", led)

    def merge_ledger_entry(self, digest_rel: str, patch: dict) -> None:
        """Merge `patch` into the existing entry (creating it if absent)
        instead of replacing it — lets a trigger decision ride beside an
        ingest-result entry (or a bare skip record) without clobbering it."""
        led = self.get_ledger()
        entry = dict(led.get(digest_rel, {}))
        entry.update(patch)
        led[digest_rel] = entry
        _save_wrapped(self.dir / "ingest_ledger.json", "entries", led)
