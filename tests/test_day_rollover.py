"""End of day: the last half hour must reach a digest (verification
2026-09-23, #04), and a late burst must be able to fire a rate anomaly.

The day's last `run` tick is at 23:30 and every tick compacts 00:00 -> now,
so 23:30-24:00 used to land in no digest at all. The first tick after
midnight now recompacts yesterday's full day and ingests it like any tick's
digest, before today's; a failure holds the watermark back so the next tick
retries, and the ledger makes the retry idempotent.

Compactor and Orchestrator are faked at the module attributes `cmd_run`
imports them from (as in test_cli_run.py); the clock is `cli._utcnow`."""

import datetime as dt
import json
from collections import Counter
from types import SimpleNamespace

import pytest

import fixtures as fx
from dbwiki import cli, compactor, orchestrate
from dbwiki.compactor import Compactor as RealCompactor
from dbwiki.harness import HarnessError
from dbwiki.health import HEALTH_LOG
from dbwiki.state import Registry

YDAY, DAY = "2026-09-22", "2026-09-23"
MIDNIGHT = f"{DAY}T00:00:00Z"


def _minutes(t0: str, t1: str) -> int:
    p = lambda s: dt.datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return int((p(t1) - p(t0)).total_seconds() // 60)


class FakeCompactor:
    """Compactor without ES: a digest per (db, window) whose event total is
    the window length in minutes, so a longer window is new content."""

    content_hash = staticmethod(RealCompactor.content_hash)

    def __init__(self, cfg):
        self.cfg = cfg
        self.es = None
        self.state = cfg.store

    def discover_dbs(self, t0, t1, sources=None) -> list[str]:
        return list(self.cfg.dbs)

    def compact(self, db, t0, t1, day, sources=None, persist=True) -> dict:
        self.cfg.calls.append(("compact", db, t0, t1, day))
        if (exc := self.cfg.compact_fails.get((db, day))):
            raise exc
        n = _minutes(t0, t1)
        groups = [{"rule": "ora_error", "template": "ORA-00600", "count": 1,
                   "class": "error", "codes": ["ORA-600"]}]
        return {"db": db, "window": {"from": t0, "to": t1, "day": day},
                "deltas": [], "notable": True,
                "totals": {"events": n, "notable_events": 1, "notable_groups": 1},
                "sources": {"alert": {"total_events": n, "notable": groups}}}

    def digest_paths(self, db, day, suffix=""):
        base = self.cfg.wiki_repo / "digests" / db / f"{day}{suffix}"
        return base.with_suffix(".json"), base.with_suffix(".md")

    def emit(self, digest, suffix=""):
        jp, mp = self.digest_paths(digest["db"], digest["window"]["day"], suffix)
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(digest))
        return jp, mp


class FakeOrchestrator:
    """Records ingests; a successful one writes the ledger entry the real
    `Orchestrator.ingest` writes (status, window_to, content_hash)."""

    def __init__(self, cfg, lock=None):
        self.cfg = cfg
        self.adapter = "fake"
        self.telemetry_errors = []
        self.last_telemetry = {"adapter": "fake"}
        ledger = cfg.ledger
        self.state = SimpleNamespace(
            get_ledger=lambda: ledger,
            merge_ledger_entry=lambda k, v: ledger.setdefault(k, {}).update(v))

    def record_decision(self, jp, decision) -> None:
        self.state.merge_ledger_entry(str(jp.relative_to(self.cfg.wiki_repo)),
                                      {"last_decision": decision.to_dict()})

    def ingest(self, db, jp, run_id=None, decision=None) -> dict:
        """The real `ingest`'s contract: the decision lands after the
        attempt, whatever its outcome."""
        try:
            return self._ingest(db, jp)
        finally:
            if decision is not None:
                self.record_decision(jp, decision)

    def _ingest(self, db, jp) -> dict:
        digest = json.loads(jp.read_text())
        day = digest["window"]["day"]
        self.cfg.calls.append(("ingest", db, day))
        if self.cfg.ingest_fails.get((db, day), 0) > 0:
            self.cfg.ingest_fails[(db, day)] -= 1
            raise HarnessError("codex exited 3: boom")
        self.cfg.ledger.setdefault(str(jp.relative_to(self.cfg.wiki_repo)), {}).update(
            status="ingested", window_to=digest["window"]["to"],
            content_hash=RealCompactor.content_hash(digest))
        return {"notable": True, "summary": f"{db} {day}"}

    def report(self, day, window, ingested, suffix="", health=None, run_id=None):
        self.cfg.calls.append(("report", day, sorted(i["db"] for i in ingested)))
        return {}

    def render_html(self, day) -> str:
        self.cfg.calls.append(("render_html", day))
        return "sha"


class Store:
    def __init__(self, watermarks: dict):
        self.w = dict(watermarks)

    def get_watermarks(self) -> dict:
        return dict(self.w)

    def set_watermark(self, db, t1) -> None:
        self.w[db] = t1


@pytest.fixture
def tick(tmp_path, monkeypatch):
    cfg = fx.make_config(tmp_path, wiki_repo=tmp_path / "wiki",
                         state_dir=tmp_path / "state")
    # the fakes' knobs ride on the config they are handed
    vars(cfg).update(dbs=["cdb1"], calls=[], compact_fails={}, ingest_fails={},
                     ledger={}, store=Store({"cdb1": f"{YDAY}T23:30:00Z"}),
                     clock=None)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "_health_note", lambda c: None)
    monkeypatch.setattr(cli, "_alerts", lambda *a, **k: {})
    monkeypatch.setattr(cli, "_utcnow", lambda: cfg.clock)
    monkeypatch.setattr(compactor, "Compactor", FakeCompactor)
    monkeypatch.setattr(orchestrate, "Orchestrator", FakeOrchestrator)
    return cfg


def run(cfg, at: str, *argv) -> int:
    cfg.calls.clear()
    cfg.clock = dt.datetime.fromisoformat(at.replace("Z", "+00:00"))
    return cli.main(["run", *argv])


def calls(cfg, kind: str) -> list[tuple]:
    return [c[1:] for c in cfg.calls if c[0] == kind]


def db_facts(cfg) -> dict:
    ev = json.loads((cfg.state_dir / HEALTH_LOG).read_text().splitlines()[-1])
    return {d["db"]: d for d in ev["dbs"]}


# ---- the catch-up ------------------------------------------------------------------

def test_first_tick_after_midnight_ingests_all_of_yesterday_first(tick):
    assert run(tick, f"{DAY}T00:15:30Z") == 0
    assert calls(tick, "compact") == [
        ("cdb1", f"{YDAY}T00:00:00Z", MIDNIGHT, YDAY),
        ("cdb1", MIDNIGHT, f"{DAY}T00:15:00Z", DAY)]
    assert calls(tick, "ingest") == [("cdb1", YDAY), ("cdb1", DAY)]
    assert tick.store.w["cdb1"] == f"{DAY}T00:15:00Z"
    # yesterday's digest is the full day now, under its usual ledger key
    y = json.loads((tick.wiki_repo / "digests" / "cdb1" / f"{YDAY}.json").read_text())
    assert y["window"] == {"from": f"{YDAY}T00:00:00Z", "to": MIDNIGHT, "day": YDAY}
    assert tick.ledger[f"digests/cdb1/{YDAY}.json"]["last_decision"]["outcome"] == "wake"
    facts = db_facts(tick)["cdb1"]
    assert facts["catchup"]["day"] == YDAY
    assert facts["catchup"]["outcome"] == "ingested"
    assert facts["watermark_after"] == f"{DAY}T00:15:00Z"
    # today's report is about today
    assert calls(tick, "report") == [(DAY, ["cdb1"])]


def test_later_ticks_the_same_day_do_not_catch_up_again(tick):
    run(tick, f"{DAY}T00:15:00Z")
    assert run(tick, f"{DAY}T02:15:00Z") == 0
    assert calls(tick, "compact") == [("cdb1", MIDNIGHT, f"{DAY}T02:15:00Z", DAY)]
    assert "catchup" not in db_facts(tick)["cdb1"]


def test_a_db_without_a_watermark_is_not_caught_up(tick):
    tick.store.w.clear()
    assert run(tick, f"{DAY}T00:15:00Z") == 0
    assert [c[3] for c in calls(tick, "compact")] == [DAY]


def test_a_tick_exactly_at_midnight_is_not_a_gap(tick):
    tick.store.w["cdb1"] = MIDNIGHT
    run(tick, f"{DAY}T02:15:00Z")
    assert [c[3] for c in calls(tick, "compact")] == [DAY]


def test_a_watermark_days_old_catches_up_only_yesterday_and_says_so(tick, capsys):
    tick.store.w["cdb1"] = "2026-09-19T12:00:00Z"
    assert run(tick, f"{DAY}T00:15:00Z") == 0
    assert [c[3] for c in calls(tick, "compact")] == [YDAY, DAY]
    assert "older than 2026-09-22" in capsys.readouterr().err
    assert db_facts(tick)["cdb1"]["catchup"]["uncovered_from"] == "2026-09-19T12:00:00Z"


# ---- failure and idempotency ------------------------------------------------------

def test_a_failed_catch_up_holds_the_watermark_and_the_next_tick_retries(tick, capsys):
    tick.ingest_fails = {("cdb1", YDAY): 1}
    assert run(tick, f"{DAY}T00:15:00Z") == 1
    assert calls(tick, "ingest") == [("cdb1", YDAY), ("cdb1", DAY)], \
        "today's work still runs"
    assert tick.store.w["cdb1"] == f"{YDAY}T23:30:00Z"
    facts = db_facts(tick)["cdb1"]
    assert facts["catchup"]["error_category"] == "harness_error"
    assert facts["watermark_after"] == f"{YDAY}T23:30:00Z"
    assert "catch-up 2026-09-22 failed" in capsys.readouterr().err

    assert run(tick, f"{DAY}T02:15:00Z") == 0
    assert calls(tick, "ingest")[0] == ("cdb1", YDAY)
    assert tick.store.w["cdb1"] == f"{DAY}T02:15:00Z"

    run(tick, f"{DAY}T04:15:00Z")
    assert [c[3] for c in calls(tick, "compact")] == [DAY]


def test_a_retried_catch_up_never_ingests_yesterday_twice(tick):
    # the catch-up ingest lands, then today's compaction dies: the watermark
    # never moved, so the next tick catches up again — and must skip
    tick.compact_fails = {("cdb1", DAY): RuntimeError("elasticsearch went away")}
    assert run(tick, f"{DAY}T00:15:00Z") == 1
    assert calls(tick, "ingest") == [("cdb1", YDAY)]
    assert tick.store.w["cdb1"] == f"{YDAY}T23:30:00Z"

    tick.compact_fails = {}
    assert run(tick, f"{DAY}T02:15:00Z") == 0
    assert calls(tick, "ingest") == [("cdb1", DAY)]
    assert db_facts(tick)["cdb1"]["catchup"]["decision_reasons"] == ["already_ingested"]
    assert tick.store.w["cdb1"] == f"{DAY}T02:15:00Z"


def test_a_failed_catch_up_compaction_is_contained(tick):
    tick.compact_fails = {("cdb1", YDAY): RuntimeError("es down for yesterday")}
    assert run(tick, f"{DAY}T00:15:00Z") == 1
    assert calls(tick, "ingest") == [("cdb1", DAY)]
    assert tick.store.w["cdb1"] == f"{YDAY}T23:30:00Z"
    assert "es down" in db_facts(tick)["cdb1"]["catchup"]["error"]


def test_explain_shows_the_catch_up_and_writes_nothing(tick, capsys):
    assert run(tick, f"{DAY}T00:15:00Z", "--explain", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert [d["window"]["day"] for d in out] == [YDAY, DAY]
    assert calls(tick, "ingest") == []
    assert tick.store.w["cdb1"] == f"{YDAY}T23:30:00Z"
    assert not (tick.wiki_repo / "digests").exists()


# ---- a late burst fires a rate anomaly -------------------------------------------

def alert_hit(ts: str, msg: str, n=[0]) -> dict:
    n[0] += 1
    return {"_index": "oracle-logs-alert-x", "_id": f"r{n[0]}",
            "_source": {"@timestamp": ts, "db_name": "cdb1",
                        "oracle": {"alert_message": msg, "msg_type": "UNKNOWN"}}}


@pytest.fixture
def burst(tmp_path):
    """24 log switches a day for three days (1/h), then 40 of them between
    20:00 and 20:40 on a day that was otherwise at the baseline rate."""
    cfg = fx.fixture_config(tmp_path)
    cfg.trace_lookup = {}
    reg = Registry(cfg.state_dir / "registry" / "cdb1.json")
    for d in ("2026-09-19", "2026-09-20", "2026-09-21"):
        reg.set_day_counts(d, "alert", 24, {"log_switch": 24}, 86400.0)
    reg.save()
    msg = "Thread 1 advanced to log sequence 42"
    hits = [alert_hit(f"{YDAY}T{h:02d}:05:00Z", msg) for h in range(17)]
    hits += [alert_hit(f"{YDAY}T20:{m:02d}:00Z", msg) for m in range(40)]
    comp = RealCompactor(cfg)
    comp.es = fx.FakeES(cfg, {"alert": hits})
    return comp


def anomalies(digest) -> list[dict]:
    return [d for d in digest["deltas"] if d["type"] == "rate_anomaly"]


def test_a_late_burst_fires_over_the_trailing_window(burst):
    # 57 events over 21.25h is 2.7/h, under 10x the baseline; the last 3h
    # hold 40 of them, 13.3/h
    d = burst.compact("cdb1", f"{YDAY}T00:00:00Z", f"{YDAY}T21:15:00Z", YDAY)
    (a,) = anomalies(d)
    assert (a["count"], a["window_hours"], a["rate_per_hour"]) == (40, 3.0, 13.3)


def test_the_burst_stays_in_later_digests_of_the_day(burst):
    first = burst.compact("cdb1", f"{YDAY}T00:00:00Z", f"{YDAY}T21:15:00Z", YDAY)
    full = burst.compact("cdb1", f"{YDAY}T00:00:00Z", MIDNIGHT, YDAY)
    assert anomalies(full) == anomalies(first)
    # ...but not in a digest of a window that does not start at midnight
    part = burst.compact("cdb1", f"{YDAY}T22:00:00Z", MIDNIGHT, YDAY,
                         persist=False)
    assert anomalies(part) == []


def test_the_whole_window_still_fires_when_it_is_the_stronger(tmp_path):
    from dbwiki.compactor import Compactor
    comp = object.__new__(Compactor)
    comp.anomaly_factor, comp.anomaly_min_count, comp.baseline_days = 10, 10, 7
    comp.silence_windows, comp.silence_min_hours = 3, 20
    reg = Registry(tmp_path / "r.json")
    for d in ("2026-07-07", "2026-07-08", "2026-07-09"):
        reg.set_day_counts(d, "alert", 24, {"log_switch": 24}, 86400.0)
    out = comp._deltas("alert", "cdb1", reg, "2026-07-10", "2026-07-10T00:00:00Z",
                       "2026-07-10T06:00:00Z", 300, Counter(log_switch=300),
                       6 * 3600.0, recent=(Counter(log_switch=20), 3 * 3600.0))
    (a,) = [d for d in out if d["type"] == "rate_anomaly"]
    assert (a["count"], a["window_hours"]) == (300, 6.0)
