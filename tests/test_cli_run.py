"""`dbwiki run` failure isolation.

One database whose adapter is down (LM Studio stopped, `codex` missing from
PATH, a timeout) used to end the whole tick: the databases after it were
skipped, and so were the daily render and the alerts — the two things that
make an outage visible at all. These tests pin the containment and the
non-zero exit code. No ES, no agent, no git: Compactor and Orchestrator are
faked at the module attributes `cmd_run` imports them from."""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
from fixtures import make_config

from dbwiki import cli, compactor, events, orchestrate, trigger
from dbwiki.compactor import Compactor as RealCompactor
from dbwiki.harness import HarnessError
from dbwiki.health import HEALTH_LOG
from dbwiki.orchestrate import ValidationError

DAY = dt.datetime.now(dt.timezone.utc).date().isoformat()


def digest(db: str, day: str, notable: bool = True) -> dict:
    groups = [{"rule": "ora-error", "template": "ORA-00600", "count": 3,
               "class": "error"}] if notable else []
    return {"schema_version": 1, "db": db,
            "window": {"from": f"{day}T00:00:00Z", "to": f"{day}T12:00:00Z",
                       "day": day},
            "deltas": [], "notable": notable,
            "totals": {"events": 3, "notable_events": 3 if notable else 0,
                       "notable_groups": len(groups)},
            "sources": {"alert": {"total_events": 3, "notable": groups}}}


class FakeCompactor:
    """Compactor without ES or a registry. `content_hash` is the real one:
    `cli._digest_facts` reaches for it on whatever class the module holds."""

    content_hash = staticmethod(RealCompactor.content_hash)

    def __init__(self, cfg):
        self.cfg = cfg
        self.es = None
        self.state = SimpleNamespace(get_watermarks=lambda: {},
                                     set_watermark=lambda db, t1: None)

    def discover_dbs(self, t0, t1, sources=None) -> list[str]:
        return list(self.cfg.dbs)

    def compact(self, db, t0, t1, day, sources=None, persist=True) -> dict:
        if exc := self.cfg.compact_fails.get(db):
            raise exc
        return digest(db, day, notable=db not in self.cfg.routine)

    def emit(self, digest_, suffix=""):
        jp = (self.cfg.wiki_repo / "digests" / digest_["db"]
              / f"{digest_['window']['day']}{suffix}.json")
        jp.parent.mkdir(parents=True, exist_ok=True)
        jp.write_text(json.dumps(digest_))
        return jp, jp.with_suffix(".md")


class FakeOrchestrator:
    """Orchestrator without an agent or git: it records what the tick asked
    of it and raises whatever the test staged for a stage."""

    def __init__(self, cfg, lock=None):
        self.cfg = cfg
        self.adapter = "fake"
        self.telemetry_errors = []
        self.last_telemetry = {"adapter": "fake", "duration_s": 1.0}
        self.ledger: dict = {}
        self.state = SimpleNamespace(
            get_ledger=lambda: self.ledger,
            merge_ledger_entry=lambda k, v: self.ledger.setdefault(k, {}).update(v))

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
        self.cfg.calls.append(("ingest", db))
        if exc := self.cfg.ingest_fails.get(db):
            raise exc
        self.ledger.setdefault(str(jp.relative_to(self.cfg.wiki_repo)), {})[
            "commit"] = f"sha-{db}"
        return {"task": "ingest", "db": db, "notable": True,
                "summary": f"{db} ingested"}

    def report(self, day, window, ingested, suffix="", health=None,
               run_id=None) -> dict:
        self.cfg.calls.append(("report", sorted(i["db"] for i in ingested)))
        if self.cfg.report_fails is not None:
            raise self.cfg.report_fails
        return {"task": "report", "summary": "report ok", "notable": True}

    def render_html(self, day) -> str:
        self.cfg.calls.append(("render_html", day))
        return "sha-html"


@pytest.fixture
def tick(tmp_path, monkeypatch):
    """A `dbwiki run` tick with every outside edge faked. Returns the config,
    which also carries the fakes' knobs: tests set `dbs`, `compact_fails`,
    `ingest_fails`, `report_fails`, and read `calls` plus the run-health
    event back off it."""
    cfg = make_config(tmp_path, wiki_repo=tmp_path / "wiki",
                      state_dir=tmp_path / "state")
    vars(cfg).update(dbs=["db1", "db2"], routine=(), calls=[], compact_fails={},
                     ingest_fails={}, report_fails=None)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(cli, "_health_note", lambda c: None)
    monkeypatch.setattr(cli, "_alerts",
                        lambda *a, **k: cfg.calls.append(("alerts",)) or {})
    monkeypatch.setattr(compactor, "Compactor", FakeCompactor)
    monkeypatch.setattr(orchestrate, "Orchestrator", FakeOrchestrator)
    return cfg


def event(cfg) -> dict:
    """The tick's run-health event."""
    return json.loads((cfg.state_dir / HEALTH_LOG).read_text().splitlines()[-1])


def db_facts(cfg) -> dict:
    return {d["db"]: d for d in event(cfg)["dbs"]}


# ---- containment ---------------------------------------------------------------

def test_a_dead_adapter_on_one_db_still_ingests_reports_renders_and_alerts(
        tick, capsys):
    tick.ingest_fails = {"db1": HarnessError("codex exited 3: boom")}
    assert cli.main(["run"]) == 1
    assert ("ingest", "db2") in tick.calls
    assert ("report", ["db2"]) in tick.calls
    assert ("render_html", DAY) in tick.calls
    assert ("alerts",) in tick.calls
    assert "db1: ingest failed: codex exited 3" in capsys.readouterr().err
    facts = db_facts(tick)
    assert facts["db1"]["error_category"] == "harness_error"
    assert "codex exited 3" in facts["db1"]["error"]
    assert facts["db1"]["telemetry"]["adapter"] == "fake"
    assert facts["db2"]["validation"] == "ok"
    assert facts["db2"]["outcome"] == "ingested"
    assert event(tick)["outcome"] == "failed"


def test_a_timeout_on_the_first_db_does_not_skip_the_second(tick):
    tick.ingest_fails = {"db1": HarnessError("agent timed out after 900s: codex")}
    assert cli.main(["run"]) == 1
    assert db_facts(tick)["db1"]["error_category"] == "agent_timeout"
    assert db_facts(tick)["db2"]["outcome"] == "ingested"


def test_validation_failure_keeps_its_facts_and_reason(tick, capsys):
    tick.ingest_fails = {"db1": ValidationError("log.md not updated")}
    assert cli.main(["run"]) == 1
    facts = db_facts(tick)["db1"]
    assert facts["validation"] == "failed"
    assert facts["error_category"] == "validation_failed"
    assert "db1: ingest failed: log.md not updated" in capsys.readouterr().err


def test_a_db_that_dies_in_compaction_is_recorded_and_the_tick_continues(
        tick, capsys):
    tick.compact_fails = {"db1": RuntimeError("elasticsearch went away")}
    assert cli.main(["run"]) == 1
    assert ("ingest", "db2") in tick.calls
    assert "db1: compact failed: elasticsearch went away" in capsys.readouterr().err
    facts = db_facts(tick)["db1"]
    assert facts["error_category"] == "unknown"
    assert "elasticsearch went away" in facts["error"]
    assert "telemetry" not in facts  # never reached the agent


def test_a_failed_report_still_renders_and_alerts(tick, capsys):
    tick.report_fails = HarnessError("codex exited 1: no model")
    assert cli.main(["run"]) == 1
    assert ("render_html", DAY) in tick.calls
    assert ("alerts",) in tick.calls
    assert "report failed: harness_error" in capsys.readouterr().err
    ev = event(tick)
    assert ev["outcome"] == "failed"
    assert ev["facts"]["report_error_category"] == "harness_error"
    assert "report" not in ev["facts"]


def test_every_db_failing_still_renders_and_alerts(tick):
    tick.ingest_fails = {"db1": HarnessError("boom1"), "db2": HarnessError("boom2")}
    assert cli.main(["run"]) == 1
    assert ("render_html", DAY) in tick.calls
    assert ("alerts",) in tick.calls
    assert set(db_facts(tick)) == {"db1", "db2"}


# ---- the happy path stays exactly as it was ------------------------------------

def test_a_clean_tick_returns_zero_and_reports(tick):
    assert cli.main(["run"]) == 0
    assert ("report", ["db1", "db2"]) in tick.calls
    ev = event(tick)
    assert ev["outcome"] == "ok" and ev["error_category"] is None
    assert ev["facts"]["report"].startswith(f"reports/{DAY}-")
    assert ev["facts"]["html"] == "sha-html"


def test_a_routine_tick_skips_the_report_and_still_returns_zero(tick):
    tick.routine = ("db1", "db2")
    assert cli.main(["run"]) == 0
    assert not any(c[0] == "report" for c in tick.calls)
    assert ("render_html", DAY) in tick.calls


def monitoring_incident(cfg, db: str, slug: str) -> None:
    """One incident under observation over today, and a digest for its db that
    the evaluator cannot read."""
    from dbwiki.incidents import (ErrorAbsent, MonitoringWindow, Status,
                                  set_status)
    from fixtures.incident_pages import incident_page
    tomorrow = (dt.date.fromisoformat(DAY) + dt.timedelta(days=1)).isoformat()
    start, until = f"{DAY}T00:00:00Z", f"{tomorrow}T00:00:00Z"
    page = cfg.wiki_repo / "incidents" / f"{slug}.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(set_status(
        incident_page(db, "transport failure", opened=start),
        Status.MONITORING, updated=start,
        monitoring=MonitoringWindow(ErrorAbsent("TNS-12564"), start, until)))
    bad = cfg.wiki_repo / "digests" / db / f"{DAY}.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"window": {"day": DAY}, "sources": ["not one"]}))


def test_an_incident_the_evaluator_cannot_read_is_noted_not_silent(tick):
    """Containment must not become silence: the tick still renders and exits
    0, and the run record still names the incident nobody got a verdict for."""
    slug = "2026-08-30-cdb9-transport"
    monitoring_incident(tick, "cdb9", slug)
    assert cli.main(["run"]) == 0
    assert ("render_html", DAY) in tick.calls
    facts = event(tick)["facts"]
    assert facts["monitoring"] == 1
    assert slug in facts["monitoring_error"]


def test_stage_names_are_the_order_the_tick_runs_them(tick, monkeypatch):
    """`events.STAGE_NAMES` is the tick's own order, not a list kept in sync by
    hand. Every stage but `tick` itself is an observable call here."""
    real_decide = trigger.decide

    class Recording(FakeCompactor):
        def compact(self, db, t0, t1, day, sources=None, persist=True):
            self.cfg.calls.append(("discover/compact", db))
            return super().compact(db, t0, t1, day, sources, persist)

    monkeypatch.setattr(compactor, "Compactor", Recording)
    monkeypatch.setattr(trigger, "decide", lambda *a, **k: (
        tick.calls.append(("decide",)) or real_decide(*a, **k)))
    tick.dbs = ["db1"]
    assert cli.main(["run"]) == 0
    ran = [name for name, *_ in tick.calls]
    assert ran == ["discover/compact", "decide", "ingest", "report",
                   "render_html", "alerts"]
    assert [n.replace("render_html", "render") for n in ran] \
        == [n for n in events.STAGE_NAMES if n != "tick"]


def test_an_explicit_day_window_never_advances_the_watermark_past_now():
    from dbwiki.cli import _day_window, _watermark_after
    t0, t1 = _day_window("2026-09-08")
    assert t1 == "2026-09-09T00:00:00Z"
    assert _watermark_after(t1, now="2026-09-08T06:47:27Z") == "2026-09-08T06:47:27Z"
    assert _watermark_after(t1, now="2026-09-10T00:00:00Z") == t1, \
        "a past day keeps its own window end"


def test_a_failed_health_probe_drops_the_block_and_says_so(monkeypatch, capsys):
    """Issue 18 (python-workarounds): the report's collection-health block is
    optional, so a probe failure still yields None, but no longer silently —
    one stderr warning per process, through `observability.warn_once`."""
    from dbwiki import health, observability

    def boom(cfg, **kw):
        raise RuntimeError("es down")

    monkeypatch.setattr(health, "assess", boom)
    monkeypatch.setattr(observability, "_warned", set())
    assert cli._health_note(make_config()) is None
    assert cli._health_note(make_config()) is None
    err = capsys.readouterr().err
    assert err.count("warning: collection-health block dropped") == 1
    assert "RuntimeError: es down" in err
