"""The compactor: (db, window) -> digest. Deterministic, no LLM.

Pipeline per source: pull window from ES -> normalize -> classify against the
pattern library -> routine classes become counters, notable classes become
storm-collapsed groups -> deltas vs the per-DB registry -> one combined digest
JSON + MD per (db, window).

Enabled `metric_sources` (config.py) are ordinary sources here — same
watermarks, digest sections, deltas and replay semantics; only the normalizer
branch and the pattern file differ."""

import datetime as dt
import statistics
import sys
from collections import Counter, deque
from pathlib import Path

from . import __version__, trace
from .changes import (LIFECYCLE, MAX_MESSAGE, ChangeStamp, after_change,
                      changes_of, headline)
from .config import Config
from .es import ES
from .normalize import (UnsupportedSchemaError, field_paths, normalize,
                        service_name_like)
from .patterns import (PatternLibrary, extract_codes, extract_trace_paths,
                       message_template)
from .state import Registry, StateStore

MSG_KEEP = 800  # verbatim chars kept for a group's representative message
MAX_STAMPS = 500  # lifecycle events stamped per source, oldest kept
ANOMALY_MIN_WINDOW_S = 3600  # per-hour rates from sub-hour windows are noise


def _window_seconds(t0: str, t1: str) -> float:
    def parse(ts: str) -> dt.datetime:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return max((parse(t1) - parse(t0)).total_seconds(), 0.0)


class _Group:
    __slots__ = ("rule", "klass", "template", "count", "first_ts", "last_ts",
                 "message", "samples", "codes", "context", "headline")

    def __init__(self, rule: str, klass: str, template: str, ev: dict,
                 context: dict | None = None, headline: str = ""):
        self.rule = rule
        self.klass = klass
        self.template = template
        self.count = 0
        self.first_ts = ev["ts"]
        self.last_ts = ev["ts"]
        self.message = ev["message"][:MSG_KEEP]
        self.samples: list[dict] = []
        self.codes = extract_codes(ev["message"])
        self.context = context
        self.headline = headline

    def add(self, ev: dict, max_samples: int) -> None:
        self.count += 1
        if ev["ts"] < self.first_ts:
            self.first_ts = ev["ts"]
        if ev["ts"] > self.last_ts:
            self.last_ts = ev["ts"]
        if len(self.samples) < max_samples:
            self.samples.append({"index": ev["index"], "id": ev["id"], "ts": ev["ts"]})

    def to_dict(self) -> dict:
        return {
            "rule": self.rule, "class": self.klass, "count": self.count,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
            "codes": self.codes, "message": self.message,
            "template": self.template, "es_samples": self.samples,
            **({"context": self.context} if self.context is not None else {}),
            **({"headline": self.headline} if self.headline else {}),
        }


class Compactor:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.es = ES(cfg.es_url, cfg.es_user, cfg.es_password)
        self.state = StateStore(cfg.state_dir)
        self.libs = {name: PatternLibrary.load(cfg.patterns_path(name))
                     for name in cfg.sources}
        c = cfg.compactor
        self.page_size = c.get("page_size", 2000)
        self.max_groups = c.get("max_groups_per_rule", 20)
        self.max_samples = c.get("max_samples_per_group", 5)
        self.max_unmatched = c.get("max_unmatched_groups", 50)
        self.anomaly_factor = c.get("rate_anomaly_factor", 10)
        self.anomaly_min_count = c.get("rate_anomaly_min_count", 10)
        self.baseline_days = c.get("rate_baseline_days", 7)
        self.silence_windows = c.get("silence_windows", 3)
        self.silence_min_hours = c.get("silence_min_hours", 20)
        self.after_change_hours = c.get("after_change_hours", 2.0)
        self.context_lines = c.get("context_lines", 0)
        self.context_max_groups = c.get("context_max_groups", 6)
        tl = cfg.trace_lookup if cfg.trace_lookup_enabled else None
        if tl is not None and not (tl.get("index_patterns") and tl.get("path_field")):
            print("trace_lookup enabled but missing index_patterns/path_field; "
                  "ignoring it (see config/dbwiki.yaml.example)", file=sys.stderr)
            tl = None
        self.trace_lookup = tl
        self._schema_ok: set[tuple] = set()  # (source, t0, t1) windows already checked

    def discover_dbs(self, t0: str, t1: str, sources: list[str] | None = None) -> list[str]:
        found: set[str] = set(self.state.known_dbs())
        for name in (sources or self.cfg.sources):
            s = self.cfg.source(name)
            dbs = self.es.dbs_in_window(
                s["index_patterns"], self.cfg.db_query_fields(name),
                s["timestamp_field"], t0, t1)
            if not dbs:
                self._check_schema(name, t0, t1)
            found |= set(dbs)
        return sorted(found)

    def _check_schema(self, name: str, t0: str, t1: str) -> None:
        """A source window that looks empty may really be a field-layout drift
        (2026-07-16: `db_name` -> `oracle.database.name`). If events exist but
        none carries any configured db field, fail loudly with a diagnostic
        instead of silently reporting zero events."""
        key = (name, t0, t1)
        if key in self._schema_ok:
            return
        scfg = self.cfg.source(name)
        index = ",".join(scfg["index_patterns"])
        fields = self.cfg.db_value_fields(name)
        if scfg.get("db_service_field"):
            fields = fields + [scfg["db_service_field"]]
        window = ES.window_query(scfg["timestamp_field"], t0, t1)
        if self.es.count(index, window) == 0:
            self._schema_ok.add(key)
            return  # genuinely no events in the window
        carrying = ES.window_query(
            scfg["timestamp_field"], t0, t1,
            extra=[{"bool": {"should": [{"exists": {"field": f}} for f in fields],
                             "minimum_should_match": 1}}])
        if self.es.count(index, carrying) > 0:
            self._schema_ok.add(key)
            return  # events carry db identity; the queried db is just quiet
        hits = self.es.search(index, {"size": 1, "query": window})["hits"]["hits"]
        seen_index = hits[0]["_index"] if hits else index
        seen = ", ".join(field_paths(hits[0]["_source"])) if hits else "(none)"
        raise UnsupportedSchemaError(
            f"source {name!r}: window {t0} -> {t1} has events in {seen_index} "
            f"but none carries any configured db field {fields}; "
            f"sample document fields: {seen}. Update `db_fields` for this "
            f"source in config/dbwiki.yaml to match the new index layout.")

    def compact(self, db: str, t0: str, t1: str, day: str,
                sources: list[str] | None = None, persist: bool = True) -> dict:
        """Build the digest dict for one db and window [t0, t1). `day` is the
        calendar day the window belongs to (registry bookkeeping key).
        `persist=False` (used by `dbwiki run --explain`) skips the registry
        save, so a what-if compaction leaves no trace on disk."""
        reg = self.state.registry(db)
        win_s = _window_seconds(t0, t1)
        src_sections = {}
        all_deltas: list[dict] = []
        stamps: list[ChangeStamp] = []
        notable_total = 0
        for name in (sources or list(self.cfg.sources)):
            section = self._compact_source(name, db, t0, t1, reg, day, win_s)
            src_sections[name] = section
            all_deltas.extend(section.pop("deltas"))
            stamps.extend(section.pop("_stamps"))
            notable_total += sum(g["count"] for g in section["notable"])
        # a cross-source delta, so it waits for every section: an alert-log
        # change and a dataguard error belong to the same database
        all_deltas.extend(after_change(stamps, src_sections,
                                       hours=self.after_change_hours))
        if persist:
            reg.save()

        digest = {
            "db": db,
            "window": {"from": t0, "to": t1, "day": day},
            "generated_by": f"dbwiki-compactor/{__version__}",
            "pattern_versions": {n: self.libs[n].version for n in src_sections},
            "sources": src_sections,
            "deltas": all_deltas,
            "totals": {
                "events": sum(s["total_events"] for s in src_sections.values()),
                "notable_events": notable_total,
                "notable_groups": sum(len(s["notable"]) for s in src_sections.values()),
            },
            "notable": bool(all_deltas) or any(s["notable"] for s in src_sections.values()),
        }
        # a view of `notable`, so deliberately outside content_hash
        digest["changes"] = [c.to_dict() for c in changes_of(digest)]
        return digest

    def _compact_source(self, name: str, db: str, t0: str, t1: str,
                        reg: Registry, day: str, win_s: float) -> dict:
        scfg = self.cfg.source(name)
        lib = self.libs[name]
        db_fields = self.cfg.db_value_fields(name)
        query = ES.window_query(scfg["timestamp_field"], t0, t1,
                                extra=self.cfg.db_filter(name, db))
        counters: Counter = Counter()
        class_counts: Counter = Counter()
        groups: dict[tuple, _Group] = {}
        dropped_groups: Counter = Counter()
        # path -> {mentions, rules, codes}, in first-mention order
        trace_paths: dict[str, dict] = {}
        want_traces = self.trace_lookup is not None \
            and self.cfg.source_kind(name) == "log"
        stamps: list[ChangeStamp] = []
        dq: deque = deque(maxlen=self.context_lines)
        pending: list[_Group] = []
        before: list[dict] = []
        total = 0

        for hit in self.es.scan(scfg["index_patterns"], query,
                                scfg["timestamp_field"], self.page_size):
            ev = normalize(name, hit, db_fields, self.cfg.source_kind(name))
            rule = lib.classify(ev)
            total += 1
            class_counts[rule.klass] += 1
            if rule.klass == LIFECYCLE and len(stamps) < MAX_STAMPS:
                # `listener_reload` and the other rules that match on a field
                # have no regex and so no matched line; a delta that named a
                # change it could not quote would read as a dangling dash
                line = rule.matched_line(ev) or headline(ev["message"])
                stamps.append(ChangeStamp(ev["ts"], rule.name,
                                          line[:MAX_MESSAGE]))

            codes = extract_codes(ev["message"])
            for code in codes:
                reg.note_code(name, code, ev["ts"])
            if name == "listener":
                if service_name_like(ev.get("service", "")):
                    reg.note_service(ev["service"], ev["ts"])
                if ev.get("program"):
                    reg.note_program(ev["program"], ev["ts"])

            if self.context_lines:
                line = {"ts": ev["ts"], "class": rule.klass,
                        "message": ev["message"].split("\n", 1)[0][:200]}
                for g in pending:
                    g.context["after"].append(line)
                pending = [g for g in pending
                           if len(g.context["after"]) < self.context_lines]
                before = list(dq)
                dq.append(line)

            if not rule.notable:
                if rule.klass == "routine":
                    counters[rule.counter_key(ev)] += 1
                continue

            if want_traces and rule.klass == "error":
                self._note_trace_paths(trace_paths, ev, rule)

            key = (rule.name, message_template(ev["message"]))
            grp = groups.get(key)
            if grp is None:
                cap = self.max_unmatched if rule.name == "unmatched" else self.max_groups
                n_for_rule = sum(1 for k in groups if k[0] == rule.name)
                if n_for_rule >= cap:
                    dropped_groups[rule.name] += 1
                    continue
                ctx = {"before": before, "after": []} if self.context_lines else None
                head = (rule.matched_line(ev)[:MAX_MESSAGE]
                        if rule.klass == LIFECYCLE else "")
                grp = groups[key] = _Group(rule.name, rule.klass, key[1], ev,
                                           ctx, head)
                if ctx is not None:
                    pending.append(grp)
            grp.add(ev, self.max_samples)

        if total == 0:
            self._check_schema(name, t0, t1)
        deltas = self._deltas(name, db, reg, day, t0, t1, total, counters, win_s)
        reg.set_day_counts(day, name, total, dict(counters), win_s)

        notable = sorted((g.to_dict() for g in groups.values()),
                         key=lambda g: (g["class"] != "error", g["first_ts"]))
        for g in notable[self.context_max_groups:]:
            g.pop("context", None)
        evidence = self._trace_evidence(trace_paths, notable, t0, t1)
        return {
            "total_events": total,
            "by_class": dict(class_counts),
            "routine_counters": dict(counters.most_common()),
            "notable": notable,
            "dropped_notable_groups": dict(dropped_groups),
            # key absent when there is nothing to say, so digests, goldens and
            # content hashes from before the feature stay exactly as they were
            **({"trace_evidence": evidence} if evidence else {}),
            "deltas": deltas,
            # popped by `compact`: a stamp is input to a cross-source
            # delta, never a digest key
            "_stamps": stamps,
        }

    def _note_trace_paths(self, acc: dict[str, dict], ev: dict, rule) -> None:
        """Capped by `max_paths` on first appearance: a storm of distinct
        incident dumps must not become a storm of ES lookups."""
        cap = self.trace_lookup.get("max_paths", 6)
        for path in extract_trace_paths(ev["message"]):
            meta = acc.get(path)
            if meta is None:
                if len(acc) >= cap:
                    continue
                meta = acc[path] = {"mentions": 0, "rules": [], "codes": []}
            meta["mentions"] += 1
            if rule.name not in meta["rules"]:
                meta["rules"].append(rule.name)

    def _trace_evidence(self, paths: dict[str, dict], notable: list[dict],
                        t0: str, t1: str) -> list[dict]:
        """The codes handed to the excerpter are the window's error-class
        codes, not the referring event's own: Oracle writes "ORA-00600 …" and
        "Errors in file …" as two adjacent alert lines, so the line naming the
        file carries no code at all and would prioritize nothing."""
        if not paths:
            return []
        codes = list(dict.fromkeys(
            c for g in notable if g["class"] == "error" for c in g["codes"]))
        for meta in paths.values():
            meta["codes"] = codes
        return trace.trace_evidence(self.es, self.trace_lookup, paths, t0, t1)

    def _deltas(self, source: str, db: str, reg: Registry, day: str, t0: str,
                t1: str, total: int, counters: Counter, win_s: float) -> list[dict]:
        deltas: list[dict] = []
        if source == "alert" or source == "dataguard":
            # per-source: a code known in the alert log for years is still news
            # the first time it appears in the dataguard log, and vice versa
            for code in reg.new_codes_in_window(source, t0, t1):
                deltas.append({"type": "first_ever_code", "source": source,
                               "value": code,
                               "first_seen": reg.codes[source][code]["first_seen"]})
        if source == "listener":
            for svc in reg.new_in_window(reg.services, t0, t1):
                if not service_name_like(svc):
                    continue  # artifacts registered before the filter existed
                deltas.append({"type": "new_service", "source": source, "value": svc})
            for prog in reg.new_in_window(reg.programs, t0, t1):
                deltas.append({"type": "new_client_program", "source": source, "value": prog})

        # rate anomaly on routine counters: per-hour rates, so a partial-day
        # window is comparable with the full-day baselines behind it
        if win_s >= ANOMALY_MIN_WINDOW_S:
            hours = win_s / 3600
            for counter, n in counters.items():
                if n < self.anomaly_min_count:
                    continue
                base = [c / (s / 3600) for c, s in
                        reg.baseline(day, source, counter, self.baseline_days)
                        if s > 0]
                if len(base) >= 3:
                    med = statistics.median(base)
                    rate = n / hours
                    if med > 0 and rate > self.anomaly_factor * med:
                        deltas.append({
                            "type": "rate_anomaly", "source": source,
                            "counter": counter, "count": n,
                            "window_hours": round(hours, 2),
                            "rate_per_hour": round(rate, 1),
                            "baseline_median_per_hour": round(med, 1)})

        # silence: only a near-full-day window with no events counts — a
        # quiet 2h night tick is normal, a silent day after active days isn't
        if total == 0 and win_s >= self.silence_min_hours * 3600 \
                and reg.active_days_before(day, source, self.silence_windows) \
                == self.silence_windows:
            deltas.append({"type": "silence", "source": source,
                           "detail": f"no {source} events from {db} in window"})
        return deltas

    @staticmethod
    def content_hash(digest: dict) -> str:
        """Hash of the digest's semantic content: stable across ticks whose
        windows differ but whose events don't (drives run-tick dedupe).

        The excerpt *text* is deliberately outside the hash: tuning the
        excerpt heuristic must not re-ingest the fleet. `docs_found` is in it,
        so a trace file that filebeat ships one chunk at a time re-ingests its
        window once per new chunk, up to `max_docs_per_path`."""
        import hashlib
        import json
        core = {
            "deltas": digest["deltas"],
            "sources": {
                n: {"total": s["total_events"],
                    "notable": [(g["rule"], g["template"], g["count"])
                                for g in s["notable"]],
                    **({"trace": [(e["path"], e["docs_found"])
                                  for e in s["trace_evidence"]]}
                       if s.get("trace_evidence") else {})}
                for n, s in digest["sources"].items()
            },
        }
        return hashlib.sha256(
            json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]

    def digest_paths(self, db: str, day: str, suffix: str = "") -> tuple[Path, Path]:
        base = self.cfg.digest_dir / db / f"{day}{suffix}"
        return base.with_suffix(".json"), base.with_suffix(".md")

    def emit(self, digest: dict, suffix: str = "") -> tuple[Path, Path]:
        """Write the digest twins. Both go through `state.atomic_write_text`:
        the ingest stage hashes and reads these files, and the ELK shipper
        tails the directory, so neither may ever observe a partial digest —
        and a crash between the two writes leaves the previous pair intact
        rather than a truncated one."""
        import json

        from .digest_md import render_md
        from .state import atomic_write_text
        jp, mp = self.digest_paths(digest["db"], digest["window"]["day"], suffix)
        atomic_write_text(jp, json.dumps(digest, indent=1))
        atomic_write_text(mp, render_md(digest))
        return jp, mp
