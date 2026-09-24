"""Failure-only alerts: fingerprinting, dedupe, escalation, recovery, sinks.

`evaluate()` is pure, so most of this is assessment dict in, alerts out —
the dict shapes are the ones `health.assess` produces (tests/test_health.py).
The wiring tests drive `cmd_run`/`cmd_health` with a fake sink; nothing here
touches a network, an agent, or the real `.state/`."""

import json
import re
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import fixtures as fx
import pytest

from dbwiki import alerts, cli, events
from dbwiki.alerts import (ALERT_SCHEMA_VERSION, ALERT_STATE, Alert, FileSink,
                           MultiSink, StderrSink, WebhookSink, dispatch,
                           evaluate, fingerprint, load_state, make_sink,
                           save_state)
from dbwiki import health as health_module
from dbwiki.health import HEALTH_LOG

NOW = "2026-07-27T00:00:00Z"
LATER = "2026-07-27T01:00:00Z"
LATEST = "2026-07-27T02:00:00Z"
DEAD = "2026-07-12T11:12:48Z"
DIGEST_REL = "digests/cdb1/2026-07-10.json"


# ---- assessment fixtures -----------------------------------------------------

OK_WIKI = {"path": "/repo/wiki", "present": True, "git": True, "stray": []}


def health(*, sources=(("alert", "ok"),), watermarks=(), failures=(),
           backlog=(), wiki=None, stale_stages=(), dep_problems=()) -> dict:
    """A `health.assess()`-shaped dict, healthy unless told otherwise."""
    bad = bool(failures or backlog or (wiki and wiki != OK_WIKI)
               or stale_stages or dep_problems
               or any(st != "ok" for _, st in sources)
               or any(w[3] for w in watermarks))
    return {
        "schema_version": 1, "generated_at": NOW, "stale_hours": 26,
        "healthy": not bad, "exit_code": 1 if bad else 0,
        "wiki": wiki or OK_WIKI,
        "last_success": {
            "compaction": {"at": "2026-07-26T23:00:00Z", "from": "run",
                           "detail": "cdb1"},
            "ingestion": {"at": "2026-07-26T22:00:00Z", "from": "run",
                          "detail": "cdb1"},
            "report": None, "research": None},
        "watermarks": [{"db": db, "watermark": wm, "age_hours": age,
                        "stale": stale} for db, wm, age, stale in watermarks],
        "failures": [{"digest": d, "db": db, "at": DEAD, "category": cat,
                      "retryable": retry, "problems": ["pi exited 1: boom"],
                      "digest_present": "present"}
                     for d, db, cat, retry in failures],
        "backlog": [{"digest": d, "db": db, "decision": None,
                     "digest_present": "present"} for d, db in backlog],
        "sources": [{"source": s, "latest_event": DEAD,
                     "age_hours": 363.0 if st != "ok" else 1.0, "state": st}
                    for s, st in sources],
        "stale_stages": [{"task": t, "at": DEAD, "age_h": 363.0,
                          "threshold_h": 26} for t in stale_stages],
        "dependencies": {"problems": [{"check": c, "detail": d,
                                       "message": f"{d} is unhappy"}
                                      for c, d in dep_problems]},
        "recovery_evidence": {"kind": "unknown", "detail": "no evidence"},
        "blockers": [], "problems": [], "events_recorded": 3,
        "queue": {"pending_count": 0, "oldest_pending_hours": None,
                 "claimed_count": 0, "stale_claimed": [],
                 "failed_count": 0, "failed_by_category": {}},
    }


HEALTHY = health()
SILENT = health(sources=[("alert", "ok"), ("listener", "source_silent")])
DOWN = health(sources=[("alert", "collection_failure"),
                       ("listener", "collection_failure")])
FAILED = health(failures=[(DIGEST_REL, "cdb1", "agent_timeout", True)])


def fresh() -> dict:
    return load_state("/nonexistent/alerts.json")


class FakeSink:
    """Collects what it is handed; `boom` makes every send raise."""

    def __init__(self, boom=False):
        self.sent, self.boom = [], boom

    def send(self, alert: dict) -> None:
        if self.boom:
            raise RuntimeError("smtp unreachable")
        self.sent.append(alert)


# ---- fingerprints ------------------------------------------------------------

def test_fingerprint_is_stable_and_dimension_aware():
    assert fingerprint("source_silent", "-", "listener") == \
        fingerprint("source_silent", "", "listener")
    assert len({fingerprint("source_silent", "-", "listener"),
                fingerprint("source_silent", "-", "alert"),
                fingerprint("collection_failure", "-", "listener"),
                fingerprint("stale_watermark", "cdb1", "-")}) == 4


def test_every_problem_kind_becomes_one_fingerprint():
    h = health(sources=[("alert", "collection_failure"),
                        ("listener", "source_silent")],
               watermarks=[("cdb1", DEAD, 363.0, True),
                           ("cdb2", NOW, 0.0, False)],
               failures=[(DIGEST_REL, "cdb1", "agent_timeout", True)],
               backlog=[("digests/cdb2/2026-07-11.json", "cdb2")],
               wiki={"path": "/repo/wiki", "present": True, "git": True,
                     "stray": ["notes.md"]})
    got = {(f.category, f.db, f.source) for f in alerts.findings(h)}
    assert got == {("dirty_tree", "-", "-"),
                   ("collection_failure", "-", "alert"),
                   ("source_silent", "-", "listener"),
                   ("stale_watermark", "cdb1", "-"),
                   ("agent_timeout", "cdb1", "-"),
                   ("digest_backlog", "cdb2", "-")}


def test_healthy_assessment_has_no_findings():
    assert alerts.findings(HEALTHY) == []


def test_absent_wiki_and_unreachable_es_are_findings():
    h = health(wiki={"path": "/repo/wiki", "present": False, "git": False,
                     "stray": []},
               sources=[("alert", "unknown"), ("listener", "unknown")])
    got = {(f.category, f.source) for f in alerts.findings(h)}
    assert got == {("wiki_missing", "-"), ("es_unreachable", "-")}


def test_repeated_failures_of_one_db_collapse_to_one_fingerprint():
    h = health(failures=[(DIGEST_REL, "cdb1", "agent_timeout", True),
                         ("digests/cdb1/2026-07-11.json", "cdb1",
                          "agent_timeout", True)])
    assert [f.window["digest"] for f in alerts.findings(h)] == [DIGEST_REL]


# ---- evaluate ----------------------------------------------------------------

def test_first_failure_alerts_once():
    to_send, state = evaluate(DOWN, fresh(), NOW, run_id="r1")
    assert {a.category for a in to_send} == {"collection_failure"}
    assert {a.source for a in to_send} == {"alert", "listener"}
    assert all(a.reason == "new" and a.escalated_from is None for a in to_send)
    assert state["schema_version"] == ALERT_SCHEMA_VERSION
    assert len(state["fingerprints"]) == 2
    e = state["fingerprints"][to_send[0].fingerprint]
    assert e["first_seen"] == e["last_seen"] == e["alerted_at"] == NOW
    assert (e["count"], e["category"], e["source"]) == (1, "collection_failure",
                                                        "alert")


def test_identical_failure_is_one_alert_across_many_ticks():
    state, sent = fresh(), []
    for tick in range(5):
        to_send, state = evaluate(FAILED, state, f"2026-07-27T0{tick}:00:00Z")
        sent += to_send
    assert len(sent) == 1
    entry = next(iter(state["fingerprints"].values()))
    assert (entry["count"], entry["first_seen"], entry["last_seen"]) == \
        (5, "2026-07-27T00:00:00Z", "2026-07-27T04:00:00Z")
    assert entry["alerted_at"] == "2026-07-27T00:00:00Z"  # not re-alerted
    assert state["recovered"] == []


def test_escalation_realerts_and_is_not_recovery():
    _, state = evaluate(SILENT, fresh(), NOW)
    to_send, state = evaluate(
        health(sources=[("alert", "ok"), ("listener", "collection_failure")]),
        state, LATER)
    assert [(a.category, a.reason, a.escalated_from) for a in to_send] == \
        [("collection_failure", "escalation", "source_silent")]
    assert state["recovered"] == []              # worse news is not recovery
    assert [e["category"] for e in state["fingerprints"].values()] == \
        ["collection_failure"]


def test_recovery_drops_the_fingerprint_and_sends_nothing():
    _, state = evaluate(DOWN, fresh(), NOW)
    to_send, state = evaluate(HEALTHY, state, LATER)
    assert to_send == []
    assert state["fingerprints"] == {}
    assert {r["category"] for r in state["recovered"]} == {"collection_failure"}
    assert all(r["first_seen"] == NOW and r["recovered_at"] == LATER
               for r in state["recovered"])
    to_send, state = evaluate(HEALTHY, state, LATEST)
    assert (to_send, state["recovered"]) == ([], [])  # reported once


def test_concurrent_fingerprints_are_independent():
    both = health(sources=[("alert", "ok"), ("listener", "source_silent")],
                  failures=[(DIGEST_REL, "cdb1", "agent_timeout", True)])
    to_send, state = evaluate(both, fresh(), NOW)
    assert len(to_send) == 2 and len(state["fingerprints"]) == 2
    # the ingest failure clears; the silent source persists silently
    to_send, state = evaluate(SILENT, state, LATER)
    assert to_send == []
    assert [r["category"] for r in state["recovered"]] == ["agent_timeout"]
    assert [e["category"] for e in state["fingerprints"].values()] == \
        ["source_silent"]
    # a new, unrelated failure still alerts
    to_send, state = evaluate(
        health(sources=[("alert", "ok"), ("listener", "source_silent")],
               watermarks=[("cdb2", DEAD, 363.0, True)]), state, LATEST)
    assert [(a.category, a.db, a.reason) for a in to_send] == \
        [("stale_watermark", "cdb2", "new")]


def test_healthy_state_stays_empty():
    to_send, state = evaluate(HEALTHY, fresh(), NOW)
    assert (to_send, state["fingerprints"], state["recovered"]) == ([], {}, [])


# ---- alert content -----------------------------------------------------------

def test_alert_carries_run_id_facts_and_an_exact_recovery_command():
    to_send, _ = evaluate(FAILED, fresh(), NOW, run_id="abc123def456")
    a = to_send[0].to_dict()
    assert set(a) == {"run_id", "fingerprint", "category", "db", "source",
                      "reason", "escalated_from", "window", "first_seen", "at",
                      "last_success", "recovery_hint"}
    assert (a["run_id"], a["category"], a["db"]) == ("abc123def456",
                                                     "agent_timeout", "cdb1")
    assert a["recovery_hint"] == "dbwiki retry --db cdb1"
    assert a["window"] == {"digest": DIGEST_REL, "at": DEAD, "retryable": True}
    assert a["last_success"] == {"compaction": "2026-07-26T23:00:00Z",
                                 "ingestion": "2026-07-26T22:00:00Z",
                                 "report": None, "research": None}


def test_alerts_carry_no_message_or_prompt_text():
    h = health(failures=[(DIGEST_REL, "cdb1", "agent_timeout", True)],
               sources=[("alert", "collection_failure")])
    h["problems"] = ["failed ingest: pi exited 1: boom"]
    h["blockers"] = ["wiki working tree dirty outside digests/ (secret.md)"]
    text = json.dumps([a.to_dict() for a in evaluate(h, fresh(), NOW)[0]])
    for leak in ("pi exited 1", "boom", "secret.md", "prompt", "password"):
        assert leak not in text


@pytest.mark.parametrize("category,db,source,hint", [
    ("collection_failure", "-", "alert", "restart the collector on the collector host"),
    ("source_silent", "-", "listener", "check the listener shipper on the collector host"),
    ("wiki_missing", "-", "-", "restore the wiki/ checkout"),
    ("dirty_tree", "-", "-", "commit or stash"),
    ("validation_failed", "cdb1", "-", "dbwiki retry --db cdb1"),
    ("stale_watermark", "cdb1", "-", "dbwiki run"),
    ("digest_backlog", "cdb1", "-", "dbwiki run --consolidate"),
    ("unknown", "cdb1", "-", "dbwiki health"),
])
def test_every_category_has_a_recovery_hint(category, db, source, hint):
    assert alerts.recovery_hint(category, db, source).startswith(hint)


# ---- state file --------------------------------------------------------------

def test_state_file_is_versioned_and_absent_state_loads(tmp_path):
    path = tmp_path / ALERT_STATE
    assert load_state(path)["fingerprints"] == {}
    _, state = evaluate(DOWN, load_state(path), NOW)
    save_state(path, state)
    assert json.loads(path.read_text())["schema_version"] == ALERT_SCHEMA_VERSION
    assert load_state(path)["fingerprints"] == state["fingerprints"]


def test_legacy_unversioned_state_is_read_as_a_bare_mapping(tmp_path):
    path = tmp_path / ALERT_STATE
    fp = fingerprint("collection_failure", "-", "alert")
    path.write_text(json.dumps({fp: {"first_seen": DEAD, "last_seen": DEAD,
                                     "count": 3, "alerted_at": DEAD,
                                     "category": "collection_failure",
                                     "db": "-", "source": "alert"}}))
    to_send, state = evaluate(DOWN, load_state(path), NOW)
    assert [a.source for a in to_send] == ["listener"]   # the known one is quiet
    assert state["fingerprints"][fp]["count"] == 4


def test_newer_state_schema_refuses(tmp_path):
    path = tmp_path / ALERT_STATE
    path.write_text(json.dumps({"schema_version": ALERT_SCHEMA_VERSION + 1,
                                "fingerprints": {}}))
    with pytest.raises(ValueError, match="newer than the version"):
        load_state(path)


def test_clean_ticks_do_not_churn_the_state_file(tmp_path):
    path = tmp_path / ALERT_STATE
    save_state(path, evaluate(HEALTHY, load_state(path), NOW)[1])
    assert not path.exists()                  # nothing to remember yet
    save_state(path, evaluate(DOWN, load_state(path), NOW)[1])
    save_state(path, evaluate(HEALTHY, load_state(path), LATER)[1])
    save_state(path, evaluate(HEALTHY, load_state(path), LATEST)[1])
    before = path.read_bytes()
    save_state(path, evaluate(HEALTHY, load_state(path), "2026-07-28T00:00:00Z")[1])
    assert path.read_bytes() == before        # byte-identical across ticks


# ---- sinks -------------------------------------------------------------------

def test_file_sink_appends_jsonl(tmp_path):
    sink = FileSink(tmp_path / "out" / "alerts.jsonl")
    for a in evaluate(DOWN, fresh(), NOW)[0]:
        sink.send(a.to_dict())
    lines = [json.loads(ln) for ln in sink.path.read_text().splitlines()]
    assert [l["source"] for l in lines] == ["alert", "listener"]


def test_stderr_sink_writes_one_json_line(capsys):
    StderrSink().send(evaluate(FAILED, fresh(), NOW)[0][0].to_dict())
    err = capsys.readouterr().err
    assert err.startswith("ALERT {")
    assert json.loads(err[len("ALERT "):])["category"] == "agent_timeout"


def test_make_sink_follows_config(tmp_path):
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={})
    assert isinstance(make_sink(cfg), StderrSink)
    cfg.alerts = {"sink": "file", "file": str(tmp_path / "a.jsonl")}
    assert make_sink(cfg).path == tmp_path / "a.jsonl"
    cfg.alerts = {"sink": "file"}
    assert make_sink(cfg).path == tmp_path / "alerts.jsonl"


def test_an_unknown_sink_name_is_a_config_error(tmp_path):
    """Silently degrading to stderr means an operator who configured delivery
    gets none and is never told."""
    cfg = fx.make_config(tmp_path, state_dir=tmp_path,
                         alerts={"sink": "carrier-pigeon"})
    with pytest.raises(ValueError, match="unknown alerts sink"):
        make_sink(cfg)


def test_the_pipeline_alert_path_still_cannot_email(tmp_path):
    """The 3.4 delivery verdict, held after the alerts.py byte-pin retired
    (the pin could not survive recurrence landing in this module): mail is
    `delivery.py`'s alone. This module knows no mail transport, and an
    email-shaped sink name is a config error rather than a feature."""
    source = Path(alerts.__file__).read_text(encoding="utf-8")
    assert "smtplib" not in source
    for name in ("email", "smtp"):
        cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"sink": name})
        with pytest.raises(ValueError, match="unknown alerts sink"):
            make_sink(cfg)


def test_a_webhook_without_a_url_is_a_config_error(tmp_path, monkeypatch):
    monkeypatch.delenv(alerts.WEBHOOK_URL_ENV, raising=False)
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"sink": "webhook"})
    with pytest.raises(ValueError, match="webhook_url"):
        make_sink(cfg)


def test_a_misconfigured_sink_is_counted_not_raised(tmp_path, capsys):
    """dispatch must survive it — and must not remember the findings as
    alerted, or fixing the config would never produce the alert."""
    cfg = fx.make_config(tmp_path, state_dir=tmp_path,
                         alerts={"enabled": True, "sink": "smoke-signal"})
    assert dispatch(cfg, DOWN, "r1", now=NOW) == \
        {"alerted": 0, "recovered": 0, "sink_errors": 1}
    assert "alert sink unavailable" in capsys.readouterr().err
    assert not (tmp_path / ALERT_STATE).exists()


def test_sinks_list_wins_over_the_single_sink_key(tmp_path):
    cfg = fx.make_config(tmp_path, state_dir=tmp_path,
                         alerts={"sink": "stderr", "sinks": ["file", "webhook"],
                                 "webhook_url": "http://localhost:1/hook"})
    sink = make_sink(cfg)
    assert isinstance(sink, MultiSink)
    assert [type(s) for s in sink.sinks] == [FileSink, WebhookSink]
    assert sink.sinks[1].url == "http://localhost:1/hook"


def test_the_environment_wins_over_a_configured_webhook_url(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv(alerts.WEBHOOK_URL_ENV, "http://env.example/hook")
    cfg = fx.make_config(tmp_path, state_dir=tmp_path,
                         alerts={"sink": "webhook",
                                 "webhook_url": "http://config.example/hook"})
    assert make_sink(cfg).url == "http://env.example/hook"


def test_webhook_sink_posts_one_json_alert(tmp_path):
    """Against a real local HTTP server (loopback, port 0) — offline, but not
    a mocked-out urlopen: the request itself is what could be wrong."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append((self.headers["Content-Type"], json.loads(body)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):    # keep pytest output clean
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    try:
        alert = evaluate(FAILED, fresh(), NOW)[0][0].to_dict()
        WebhookSink(f"http://127.0.0.1:{srv.server_port}/hook").send(alert)
    finally:
        srv.server_close()
    assert len(got) == 1
    ctype, sent = got[0]
    assert ctype == "application/json"
    assert sent["category"] == "agent_timeout" and sent["db"] == "cdb1"
    assert sent["recovery_hint"] == "dbwiki retry --db cdb1"


def test_webhook_sink_raises_on_an_unreachable_endpoint():
    sink = WebhookSink("http://127.0.0.1:9/hook", timeout=1.0)   # discard port
    with pytest.raises(urllib.error.URLError):
        sink.send({"category": "agent_timeout"})


def test_multisink_delivers_to_the_healthy_sink_and_reports_the_dead_one(tmp_path):
    good, bad = FakeSink(), FakeSink(boom=True)
    with pytest.raises(RuntimeError, match="smtp unreachable"):
        MultiSink([bad, good]).send({"category": "agent_timeout"})
    assert good.sent == [{"category": "agent_timeout"}]   # dead sink first


def test_multisink_failure_is_isolated_by_dispatch(tmp_path, capsys):
    good = FakeSink()
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    facts = dispatch(cfg, DOWN, "r1", now=NOW,
                     sink=MultiSink([FakeSink(boom=True), good]))
    # one dead transport means the alert is not delivered everywhere: it is
    # retried later (a duplicate on the healthy sink beats a lost alert)
    assert facts == {"alerted": 0, "recovered": 0, "sink_errors": 2}
    assert len(good.sent) == 2
    assert "alert sink failed: FakeSink: smtp unreachable" in capsys.readouterr().err


def test_sink_exception_is_isolated(tmp_path, capsys):
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    facts = dispatch(cfg, DOWN, "r1", now=NOW, sink=FakeSink(boom=True))
    assert facts == {"alerted": 0, "recovered": 0, "sink_errors": 2}
    assert "alert sink failed: smtp unreachable" in capsys.readouterr().err
    # still remembered — as undelivered, with a retry time, never as alerted
    held = load_state(tmp_path / ALERT_STATE)["fingerprints"]
    assert len(held) == 2
    for e in held.values():
        assert "alerted_at" not in e
        assert e["undelivered"]["failures"] == 1
        assert e["undelivered"]["retry_at"] > NOW


def test_a_failed_send_is_retried_once_the_sink_is_back(tmp_path):
    """Webhook down on tick 1, back on tick 2: the alert still arrives, once,
    with its original first_seen and reason."""
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    dispatch(cfg, FAILED, "r1", now=NOW, sink=FakeSink(boom=True))
    ok = FakeSink()
    assert dispatch(cfg, FAILED, "r2", now=LATER, sink=ok) == \
        {"alerted": 1, "recovered": 0}
    assert [(a["category"], a["reason"], a["first_seen"], a["run_id"])
            for a in ok.sent] == [("agent_timeout", "new", NOW, "r2")]
    e = next(iter(load_state(tmp_path / ALERT_STATE)["fingerprints"].values()))
    assert e["alerted_at"] == LATER and "undelivered" not in e
    assert dispatch(cfg, FAILED, "r3", now=LATEST, sink=ok) == \
        {"alerted": 0, "recovered": 0}
    assert len(ok.sent) == 1


def test_retries_of_an_undelivered_alert_back_off(tmp_path):
    """A permanently dead sink is retried with growing gaps (capped), not on
    every tick, so a dead webhook costs one timeout per backoff step."""
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    dead = FakeSink(boom=True)
    attempts = []

    class Counting:
        def send(self, a):
            attempts.append(a["run_id"])
            dead.send(a)

    for hour in range(0, 48):
        now = f"2026-07-{27 + hour // 24}T{hour % 24:02d}:00:00Z"
        dispatch(cfg, FAILED, f"h{hour}", now=now, sink=Counting())
    # 30 min base, doubling, capped at 12 h: hours 0,1,2,4,8,16,28,40
    assert attempts == ["h0", "h1", "h2", "h4", "h8", "h16", "h28", "h40"]
    e = next(iter(load_state(tmp_path / ALERT_STATE)["fingerprints"].values()))
    assert e["undelivered"]["failures"] == 8


def test_an_escalation_keeps_its_reason_across_a_failed_send(tmp_path):
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    dispatch(cfg, SILENT, "r1", now=NOW, sink=FakeSink())
    worse = health(sources=[("alert", "ok"), ("listener", "collection_failure")])
    dispatch(cfg, worse, "r2", now=LATER, sink=FakeSink(boom=True))
    ok = FakeSink()
    dispatch(cfg, worse, "r3", now=LATEST, sink=ok)
    assert [(a["category"], a["reason"], a["escalated_from"]) for a in ok.sent] \
        == [("collection_failure", "escalation", "source_silent")]


def test_an_undelivered_alert_that_clears_is_not_reported_recovered(tmp_path):
    """Nobody was told it broke, so its recovery is not news either."""
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    dispatch(cfg, FAILED, "r1", now=NOW, sink=FakeSink(boom=True))
    assert dispatch(cfg, HEALTHY, "r2", now=LATER, sink=FakeSink()) == \
        {"alerted": 0, "recovered": 0}
    assert load_state(tmp_path / ALERT_STATE)["fingerprints"] == {}


def test_unrelated_global_findings_are_not_escalations_of_each_other():
    """dirty_tree clearing while stage_stale appears is one recovery and one
    new alert — both are ('-', '-') findings but different kinds."""
    dirty = health(wiki={**OK_WIKI, "stray": ["x.md"]})
    _, state = evaluate(dirty, fresh(), NOW)
    to_send, state = evaluate(health(stale_stages=["report"]), state, LATER)
    assert [(a.category, a.reason, a.escalated_from) for a in to_send] == \
        [("stage_stale", "new", None)]
    assert [r["category"] for r in state["recovered"]] == ["dirty_tree"]


def test_dispatch_returns_run_health_facts(tmp_path):
    cfg = fx.make_config(tmp_path, state_dir=tmp_path, alerts={"enabled": True})
    sink = FakeSink()
    assert dispatch(cfg, DOWN, "r1", now=NOW, sink=sink) == \
        {"alerted": 2, "recovered": 0}
    assert dispatch(cfg, DOWN, "r2", now=LATER, sink=sink) == \
        {"alerted": 0, "recovered": 0}
    assert dispatch(cfg, HEALTHY, "r3", now=LATEST, sink=sink) == \
        {"alerted": 0, "recovered": 2}
    assert len(sink.sent) == 2 and {a["run_id"] for a in sink.sent} == {"r1"}


# ---- wiring ------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    c = fx.fixture_config(tmp_path)
    c.state_dir.mkdir(parents=True, exist_ok=True)
    c.alerts = {"enabled": True, "sink": "file",
                "file": str(tmp_path / "alerts.jsonl")}
    return c


def health_cmd(cfg, monkeypatch, sink, h):
    """`dbwiki health --alert` with the assessment and sink stubbed."""
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr("dbwiki.health.assess", lambda c, **kw: h)
    monkeypatch.setattr(alerts, "make_sink", lambda c: sink)
    return cli.cmd_health(SimpleNamespace(json=False, alert=True))


def test_health_alert_flag_dispatches_and_persists(cfg, monkeypatch, capsys):
    sink = FakeSink()
    assert health_cmd(cfg, monkeypatch, sink, DOWN) == 1  # exit code from health
    assert [a["category"] for a in sink.sent] == ["collection_failure"] * 2
    assert len(sink.sent[0]["run_id"]) == 12
    assert "alerts: 2 sent, 0 recovered" in capsys.readouterr().err
    assert (cfg.state_dir / ALERT_STATE).exists()
    health_cmd(cfg, monkeypatch, sink, DOWN)             # second tick: silent
    assert len(sink.sent) == 2


def test_alerts_are_disabled_by_default(cfg, monkeypatch, capsys):
    cfg.alerts = {}                       # the shipped config
    sink = FakeSink()
    health_cmd(cfg, monkeypatch, sink, DOWN)
    assert sink.sent == []
    assert not (cfg.state_dir / ALERT_STATE).exists()
    assert "alerts: disabled" in capsys.readouterr().err


def test_example_config_ships_alerts_off():
    """The template a new deployment copies must default to silent; the live
    config/dbwiki.yaml is operator state and may enable alerts."""
    import yaml
    from dbwiki.config import Config
    raw = yaml.safe_load(
        (fx.REPO_ROOT / "config" / "dbwiki.yaml.example").read_text())
    assert alerts.enabled(Config(raw, fx.REPO_ROOT)) is False


def test_run_records_alert_counts_in_the_health_event(cfg, monkeypatch):
    """`cmd_run` over a dead cluster: one health event carrying the counts,
    and the alert itself reaching the sink."""
    from dbwiki.health import read_events
    sink = FakeSink()
    monkeypatch.setattr(cli, "load_config", lambda: cfg)
    monkeypatch.setattr(alerts, "make_sink", lambda c: sink)
    monkeypatch.setattr("dbwiki.compactor.Compactor.discover_dbs",
                        lambda self, t0, t1, sources=None: [])
    monkeypatch.setattr("dbwiki.health.assess", lambda c, **kw: FAILED)
    assert cli.cmd_run(SimpleNamespace(consolidate=False, explain=False)) == 0
    e = read_events(cfg.state_dir)[-1]
    assert (e["command"], e["facts"]["alerted"], e["facts"]["recovered"]) == \
        ("run", 1, 0)
    assert [a["run_id"] for a in sink.sent] == [e["run_id"]]
    assert sink.sent[0]["recovery_hint"] == "dbwiki retry --db cdb1"


def test_alerting_failure_never_fails_the_run(cfg, monkeypatch, capsys):
    def boom(c, **kw):
        raise RuntimeError("assessment exploded")

    monkeypatch.setattr("dbwiki.health.assess", boom)
    assert cli._alerts(cfg, "r1") == {}
    assert "warning: alerting failed: assessment exploded" in \
        capsys.readouterr().err


# ---- stale stages and dependencies -------------------------------------------

STALE = health(stale_stages=["report"])
DEPS = health(dep_problems=[("model_server", "model_server_unreachable"),
                            ("adapters", "adapter_missing:codex")])


def test_a_stale_stage_becomes_one_finding_per_stage():
    got = [(f.category, f.db, f.source, f.detail)
           for f in alerts.findings(health(stale_stages=["report", "research"]))]
    assert got == [("stage_stale", "-", "-", "report"),
                   ("stage_stale", "-", "-", "research")]


def test_dependency_problems_become_findings_with_their_detail():
    got = [(f.category, f.detail) for f in alerts.findings(DEPS)]
    assert got == [("dependency_failure", "model_server_unreachable"),
                   ("dependency_failure", "adapter_missing:codex")]


def test_two_dependency_details_are_two_alerts():
    """A missing adapter and a dead model server are both
    `dependency_failure`, and need entirely different next commands."""
    to_send, _ = evaluate(DEPS, fresh(), NOW)
    assert len({a.fingerprint for a in to_send}) == 2
    assert sorted(a.recovery_hint for a in to_send) == [
        "install/authenticate the adapter and make sure cron's PATH includes "
        "it (docs/scheduling.md)",
        "start the local model server (unsloth studio: `unsloth start`); see "
        "docs/scheduling.md \"What the local stages need to be up\""]


def test_a_stale_stage_alert_names_the_command_to_run():
    to_send, _ = evaluate(STALE, fresh(), NOW)
    a, = to_send
    assert a.category == "stage_stale" and a.window["task"] == "report"
    assert "uv run dbwiki report" in a.recovery_hint
    assert ".state/cron.log" in a.recovery_hint


def test_a_recovered_dependency_drops_its_own_fingerprint_only():
    _, state = evaluate(DEPS, fresh(), NOW)
    to_send, state = evaluate(
        health(dep_problems=[("adapters", "adapter_missing:codex")]),
        state, LATER)
    assert to_send == []
    assert [r["category"] for r in state["recovered"]] == ["dependency_failure"]
    assert len(state["fingerprints"]) == 1


def test_a_detail_free_fingerprint_is_unchanged():
    """Existing alert state must not re-fire: adding `detail` may only change
    the shas of findings that carry one."""
    assert fingerprint("source_silent", "-", "listener") == \
        fingerprint("source_silent", "-", "listener", "")
    assert fingerprint("dependency_failure", "-", "-", "disk") != \
        fingerprint("dependency_failure", "-", "-")


def test_the_portal_hint_names_the_restart_and_both_fallbacks():
    """A dead workbench must not read as "you cannot touch this incident":
    the CLI closes one without the portal running."""
    hint = alerts.recovery_hint("dependency_failure",
                                detail="portal_unreachable")
    assert "systemctl --user restart dbwiki-portal" in hint
    assert "dbwiki portal serve" in hint
    assert "dbwiki incident" in hint


def test_an_unknown_dependency_detail_still_gets_a_hint():
    assert alerts.recovery_hint("dependency_failure",
                                detail="something_new") == "dbwiki health"


# ---- recurrence --------------------------------------------------------------

FIRST_DAY = "2026-07-25T09:00:00Z"
SECOND_DAY = "2026-07-26T09:00:00Z"
UNREACHABLE = "pi produced no answer — provider unreachable: 111.222.3.4"
REFUSED = "elasticsearch said 503 twice"


def db_row(db, **over):
    """One `dbs[]` entry as `cmd_run` writes it (`cli._digest_facts`)."""
    entry = {"db": db, "decision": "wake",
             "decision_reasons": ["first_ever_code"], "model_tier": "strong",
             "digest": f"wiki/digests/{db}/2026-07-25.json",
             "validation": "ok", "outcome": "ingested", "commit": "ab12cd3"}
    entry.update(over)
    return entry


def tick(run_id, started, *, command="run", outcome="ok",
         error_category=None, dbs=()):
    """One `run_health.jsonl` line as `RunRecord.to_event` writes it."""
    return {"schema_version": 1, "run_id": run_id, "command": command,
            "started": started, "finished": started, "duration_s": 61.4,
            "outcome": outcome, "error_category": error_category,
            "dbs": list(dbs),
            "facts": {"consolidation": False, "adapter": "pi",
                      "dbs": len(dbs)}}


def broken(db, error, **over):
    return db_row(db, outcome="failed", validation="failed", commit="",
                  error_category="harness_error", error=error, **over)


SKIPPED = db_row("cdb2", decision="skip",
                 decision_reasons=["unchanged_window"], outcome="skipped",
                 commit="")

TICKS = [
    tick("aaaaaaaaaaaa", FIRST_DAY, dbs=[broken("cdb1", UNREACHABLE), SKIPPED]),
    tick("bbbbbbbbbbbb", SECOND_DAY, dbs=[broken("cdb1", REFUSED)]),
    tick("cccccccccccc", "2026-07-26T10:00:00Z", command="retry",
         outcome="failed"),
    tick("dddddddddddd", "2026-07-26T11:00:00Z", outcome="failed",
         error_category="lock_busy"),
]

HARNESS_FP = fingerprint("harness_error", "cdb1", "-")


def logged(tmp_path, lines=TICKS, entries=None):
    """A `.state/` holding those run-health lines and that alert state, and
    the events they load as."""
    state = tmp_path / ".state"
    state.mkdir(parents=True, exist_ok=True)
    (state / HEALTH_LOG).write_text(
        "".join(json.dumps(line) + "\n" for line in lines))
    if entries is not None:
        (state / ALERT_STATE).write_text(json.dumps(
            {"schema_version": ALERT_SCHEMA_VERSION, "fingerprints": entries,
             "recovered": []}))
    return state, events.load_events(state).events


def test_a_failure_group_is_minted_in_the_namespace_that_already_exists(
        tmp_path):
    """The key `(error_category, db)` *is* `fingerprint(cat, db, "-")`, so the
    group and any open alert about it are the same identity and no second
    namespace is minted."""
    state, evs = logged(tmp_path, entries={
        HARNESS_FP: {"first_seen": FIRST_DAY, "last_seen": SECOND_DAY,
                     "count": 43, "alerted_at": FIRST_DAY,
                     "category": "harness_error", "db": "cdb1",
                     "source": "-"}})
    rows = {row.fingerprint: row for row in alerts.recurring(evs, state)}

    assert list(rows)[0] == HARNESS_FP, "most frequent first"
    worst = rows[HARNESS_FP]
    assert (worst.category, worst.db, worst.count) == ("harness_error",
                                                       "cdb1", 2)
    assert worst.days == 2 and worst.commands == ("run",)
    assert (worst.first_seen, worst.last_seen) == (FIRST_DAY, SECOND_DAY)
    assert worst.sample_error == REFUSED, "the newest, verbatim"
    assert worst.alert == alerts.OpenAlert(HARNESS_FP, FIRST_DAY, SECOND_DAY,
                                           43)
    assert {row.db for row in rows.values()} == {"cdb1", "-"}, \
        "the skipped database recorded no failure"


def test_a_failure_nobody_categorised_is_counted_and_never_dropped(tmp_path):
    state, evs = logged(tmp_path)
    rows = {(row.category, row.db): row for row in alerts.recurring(evs, state)}

    assert set(rows) == {("harness_error", "cdb1"), ("unknown", "-"),
                         ("lock_busy", "-")}
    unknown = rows[("unknown", "-")]
    assert unknown.count == 1 and unknown.commands == ("retry",)
    assert unknown.fingerprint == fingerprint("unknown", "-", "-")


def test_a_group_no_alert_is_open_about_says_nothing_about_recovery(tmp_path):
    """`evaluate` drops a fingerprint the moment the finding clears, so an
    absent entry means "not open now" and never "this recovered"."""
    state, evs = logged(tmp_path, entries={})
    assert {row.alert for row in alerts.recurring(evs, state)} == {None}


def test_an_open_alert_whose_runs_aged_out_of_the_logs_gets_no_row(tmp_path):
    state, evs = logged(tmp_path, lines=[tick("aaaaaaaaaaaa", FIRST_DAY)],
                        entries={HARNESS_FP: {"first_seen": FIRST_DAY,
                                              "last_seen": SECOND_DAY,
                                              "count": 43}})
    assert alerts.recurring(evs, state) == ()


def test_an_open_alert_is_read_through_the_absent_and_the_legacy_shape(
        tmp_path):
    assert alerts.open_alerts(tmp_path) == {}
    (tmp_path / ALERT_STATE).write_text(json.dumps(
        {HARNESS_FP: {"first_seen": FIRST_DAY, "last_seen": SECOND_DAY,
                      "count": 43, "alerted_at": FIRST_DAY,
                      "category": "harness_error", "db": "cdb1",
                      "source": "-"}}))

    assert alerts.open_alerts(tmp_path) == {
        HARNESS_FP: alerts.OpenAlert(HARNESS_FP, FIRST_DAY, SECOND_DAY, 43)}


def test_every_key_an_alert_entry_holds_is_carried_or_dropped_on_purpose():
    """Run the writer and read its own keys: a field a future `evaluate`
    starts recording arrives here as a failure rather than as silence."""
    _, state = evaluate(
        health(failures=[(DIGEST_REL, "cdb1", "harness_error", True)],
               dep_problems=[("adapters", "adapter_missing:codex")]),
        fresh(), NOW)
    written = {key for entry in state["fingerprints"].values() for key in entry}
    # ...and the keys `dispatch` adds when a send fails
    with tempfile.TemporaryDirectory() as d:
        dispatch(fx.make_config(Path(d), state_dir=Path(d)), FAILED, "r1", now=NOW,
                 sink=FakeSink(boom=True))
        written |= {key for entry in load_state(Path(d) / ALERT_STATE)[
            "fingerprints"].values() for key in entry}

    assert written == set(alerts._OPEN_KEYS) | set(alerts._OPEN_DROPPED)
    assert not set(alerts._OPEN_KEYS) & set(alerts._OPEN_DROPPED)


def test_every_dependency_detail_health_writes_reaches_a_hint():
    """`dependency_failure` covers unrelated breakages, so its hint is chosen
    by the finding's `detail` prefix. Every detail `health.dependencies` can
    write must find one instead of falling back to `dbwiki health`."""
    source = Path(health_module.__file__).read_text()
    details = set(re.findall(r'_problem\(\s*f?"([a-z_]+)', source))
    assert len(details) >= 9, details
    for detail in sorted(details):
        assert alerts.recovery_hint("dependency_failure", detail=detail) \
            != "dbwiki health", detail
