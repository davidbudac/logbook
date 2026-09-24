"""Human feedback as Langfuse scores (`scores.py`, langfuse issue 02).

Two halves, split on purpose: the surfaces an operator acts through (the
`dbwiki incident` CLI, the portal, the review inbox) only *queue* a score
record in `.state/pending_scores.jsonl` and never hold a Langfuse key; the
sender (`dbwiki scores send`, cron) posts them. What is under test here is
the queue a real action leaves behind, which trace it names, and that the
sender posts each record once and survives a partial failure. The Langfuse
client is a fake: nothing here reaches a network or a real agent CLI.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fixtures import make_config

from dbwiki import cli, health, observability, review, scores
from dbwiki.health import AGENT_LOG
from test_cli_incident import (DB, EMAIL, MERGE, RECORD, SLUG,
                               duplicate_pair, git)
from test_cli_incident import root as cli_root  # noqa: F401 — fixture
from test_portal_api import (COMMIT, OPEN_SLUG, PREVIEW, Client, record_body,
                             serve)
from test_portal_api import root as portal_root  # noqa: F401 — fixture
from test_health import FRESH, fake_es
from test_health import cfg as health_cfg  # noqa: F401 — fixture

RUN = "a1b2c3d4e5f6"
INGEST_EVENT = "e1e1e1e1e1e1"
OTHER_DB_EVENT = "e2e2e2e2e2e2"
REPORT_EVENT = "e3e3e3e3e3e3"
NOW = "2026-09-24T12:00:00Z"


@pytest.fixture(autouse=True)
def fresh_warnings():
    observability._warned = set()
    yield
    observability._warned = set()


def machine_commit(wiki: Path, subject: str, run_id: str = RUN) -> None:
    """Rewrite the wiki's only commit as a loop tick's: the subject names the
    stage the way `Orchestrator._propose` spells it, with a `Run-ID:`
    trailer."""
    git(wiki, "commit", "--amend", "-q", "-m",
        f"{subject}\n\nRun-ID: {run_id}")


def ledger(state_dir: Path, *lines: dict) -> None:
    path = state_dir / AGENT_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def tick_ledger(state_dir: Path, db: str) -> None:
    """One tick's stages: an ingest for the page's db, one for another db,
    and the report — all under `RUN`, which is the whole attribution
    problem."""
    ledger(state_dir,
           {"run_id": RUN, "task": "ingest", "db": db,
            "event_id": INGEST_EVENT, "validation_ok": True},
           {"run_id": RUN, "task": "ingest", "db": "cdb9",
            "event_id": OTHER_DB_EVENT, "validation_ok": True},
           {"run_id": RUN, "task": "report", "event_id": REPORT_EVENT,
            "validation_ok": True})


def pending(state_dir: Path) -> list[dict]:
    return scores.read_pending(state_dir)


def by_name(records: list[dict]) -> dict[str, dict]:
    return {r["name"]: r for r in records}


# -- queueing from the CLI --------------------------------------------------

def test_a_cli_action_on_a_machine_written_page_queues_both_scores(cli_root):
    wiki = cli_root / "wiki"
    machine_commit(wiki, f"ingest: {DB} 2026-08-30 — opened the incident")
    tick_ledger(cli_root / ".state", DB)
    assert cli.main([*RECORD, "--commit"]) == 0

    rows = by_name(pending(cli_root / ".state"))
    assert set(rows) == {"human_accepted", "human_action:record-action"}
    accepted = rows["human_accepted"]
    assert accepted["value"] == 1 and accepted["data_type"] == "BOOLEAN"
    assert accepted["trace_id"] == observability.trace_id(INGEST_EVENT), \
        "the trace of the stage that wrote the page, not the tick's others"
    assert accepted["session_id"] is None
    assert accepted["stage"] == "ingest" and accepted["run_id"] == RUN
    assert accepted["event_id"] == INGEST_EVENT
    assert accepted["surface"] == "cli" and accepted["subject"] == SLUG
    act = rows["human_action:record-action"]
    assert act["value"] == 1 and act["data_type"] == "NUMERIC"
    assert act["trace_id"] == accepted["trace_id"]
    assert accepted["key"] != act["key"]
    assert EMAIL not in json.dumps(rows), "identifiers only, never an actor"


def test_a_second_action_still_scores_the_machine_write_behind_it(cli_root):
    """The DBA's own first commit carries no Run-ID; the second act looks
    past it to the tick that wrote the page."""
    wiki = cli_root / "wiki"
    machine_commit(wiki, f"ingest: {DB} 2026-08-30 — opened")
    tick_ledger(cli_root / ".state", DB)
    assert cli.main([*RECORD, "--commit"]) == 0
    later = [*RECORD[:-1], "2026-08-30T15:00:00Z", "--commit"]
    assert cli.main(later) == 0
    rows = pending(cli_root / ".state")
    assert len(rows) == 4
    assert {r["trace_id"] for r in rows} \
        == {observability.trace_id(INGEST_EVENT)}
    accepted = [r for r in rows if r["name"] == "human_accepted"]
    assert accepted[0]["key"] == accepted[1]["key"], \
        "one verdict per page per trace: the later act upserts the earlier"
    acts = [r for r in rows if r["name"].startswith("human_action:")]
    assert acts[0]["key"] != acts[1]["key"], "every act is its own count"


def test_a_merge_rejects_the_page_the_machine_should_not_have_opened(cli_root):
    wiki = cli_root / "wiki"
    duplicate_pair(cli_root)
    machine_commit(wiki, f"ingest: {DB} 2026-08-30 — opened twice")
    tick_ledger(cli_root / ".state", DB)
    assert cli.main([*MERGE, "--commit"]) == 0
    rows = by_name(pending(cli_root / ".state"))
    assert rows["human_accepted"]["value"] == 0
    assert "human_action:merge" in rows


def test_a_stage_the_ledger_no_longer_holds_falls_back_to_the_session(
        cli_root):
    machine_commit(cli_root / "wiki", f"ingest: {DB} 2026-08-30 — opened")
    assert cli.main([*RECORD, "--commit"]) == 0
    rows = pending(cli_root / ".state")
    assert rows and all(r["trace_id"] is None for r in rows)
    assert all(r["session_id"] == RUN for r in rows)
    assert all(r["stage"] == "ingest" for r in rows), \
        "the subject still names the stage; only its trace is unknown"


def test_two_candidate_stages_fall_back_to_the_session(cli_root):
    machine_commit(cli_root / "wiki", "report: 2026-08-30")
    ledger(cli_root / ".state",
           {"run_id": RUN, "task": "report", "event_id": "r1r1r1r1r1r1",
            "validation_ok": True},
           {"run_id": RUN, "task": "report", "event_id": "r2r2r2r2r2r2",
            "validation_ok": True})
    assert cli.main([*RECORD, "--commit"]) == 0
    rows = pending(cli_root / ".state")
    assert rows and {r["session_id"] for r in rows} == {RUN}
    assert {r["trace_id"] for r in rows} == {None}


def test_a_page_no_tick_ever_wrote_queues_nothing(cli_root):
    assert cli.main([*RECORD, "--commit"]) == 0
    assert pending(cli_root / ".state") == []


def test_a_queue_that_cannot_be_written_never_fails_the_action(
        cli_root, monkeypatch, capsys):
    machine_commit(cli_root / "wiki", f"ingest: {DB} 2026-08-30 — opened")
    tick_ledger(cli_root / ".state", DB)

    def refuse(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(scores, "_append", refuse)
    assert cli.main([*RECORD, "--commit"]) == 0
    err = capsys.readouterr().err
    assert err.count("warning: ") == 1 and "score" in err
    assert "## Action" in (cli_root / "wiki" / f"incidents/{SLUG}.md"
                           ).read_text()


# -- queueing from the portal -----------------------------------------------

def test_a_portal_commit_queues_the_same_scores_without_a_key(portal_root,
                                                              monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    wiki = portal_root / "wiki"
    machine_commit(wiki, "ingest: cdb1 2026-08-30 — opened")
    tick_ledger(portal_root / ".state", "cdb1")
    from dbwiki.config import load_config
    srv, thread = serve(load_config(portal_root))
    try:
        api = Client(srv.server_address)
        _, pv = api.post(PREVIEW, record_body())
        status, _ = api.post(COMMIT, record_body(base=pv["base"],
                                                 at=pv["at"]))
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=10)
    assert status == 200
    rows = by_name(pending(portal_root / ".state"))
    assert rows["human_accepted"]["surface"] == "portal"
    assert rows["human_accepted"]["subject"] == OPEN_SLUG
    assert rows["human_accepted"]["trace_id"] \
        == observability.trace_id(INGEST_EVENT)
    assert rows["human_accepted"]["at"] == pv["at"]


# -- queueing from the review inbox -----------------------------------------

FP = review.fingerprint("stale_open", "i-open-1", DB, "stale_open")
REVIEW_ID = "2026-W39"
REVIEW_EVENT = "rvrvrvrvrvrv"


@pytest.fixture
def inbox(tmp_path):
    """A published review holding `FP`, and the ledger line of the synthesis
    stage that narrated it."""
    state = tmp_path / ".state"
    reviews = state / review.REVIEW_DIR / review.REVIEWS_DIR
    reviews.mkdir(parents=True)
    (reviews / f"{REVIEW_ID}.json").write_text(json.dumps(
        {"schema_version": review.REVIEW_SCHEMA_VERSION,
         "review_id": REVIEW_ID, "findings": [{"fingerprint": FP}]}))
    ledger(state, {"task": "review", "review_id": REVIEW_ID,
                   "event_id": REVIEW_EVENT, "validation_ok": True})
    return state


def test_an_acknowledge_accepts_and_a_suppress_rejects_the_review(inbox):
    review.acknowledge(inbox, FP, actor=EMAIL, now=NOW)
    review.acknowledge(inbox, FP, actor=EMAIL, now=NOW)   # idempotent no-op
    review.suppress(inbox, FP, actor=EMAIL, now=NOW, days=7)
    rows = pending(inbox)
    assert [(r["name"], r["value"]) for r in rows] == [
        ("human_accepted", 1), ("human_action:acknowledge", 1),
        ("human_accepted", 0), ("human_action:suppress", 1)]
    assert {r["trace_id"] for r in rows} \
        == {observability.trace_id(REVIEW_EVENT)}
    assert {r["stage"] for r in rows} == {"review"}
    assert {r["subject"] for r in rows} == {FP}
    assert rows[0]["key"] == rows[2]["key"], \
        "the later verdict on the same finding replaces the earlier"


def test_a_review_nothing_narrated_queues_nothing(inbox):
    (inbox / AGENT_LOG).unlink()
    review.acknowledge(inbox, FP, actor=EMAIL, now=NOW)
    assert pending(inbox) == []


def test_the_review_synthesis_is_exported_under_its_ledger_event(
        tmp_path, monkeypatch):
    """The review stage used to reach the ledger only; a decision needs a
    trace to land on, so the synthesis is exported like every other stage,
    seeded from the same `event_id`."""
    exported = []
    monkeypatch.setattr(observability, "record_agent_run",
                        lambda cfg, fields, tele=None, **kw:
                        exported.append((fields, kw)))
    monkeypatch.setattr(review, "synthesize",
                        lambda *a, **kw: SimpleNamespace(
                            to_dict=lambda: {"summary": "s"}))
    review._synthesis_stage(None, make_config(), rules=review.Rules(),
                            state_dir=tmp_path, review_id=REVIEW_ID, now=NOW)
    line = health.read_agent_runs(tmp_path)[-1]
    assert len(exported) == 1
    fields, kw = exported[0]
    assert fields["task"] == "review" and kw["event_id"] == line["event_id"]
    assert kw["result"] == {"summary": "s"}


# -- the sender ---------------------------------------------------------------

class FakeLangfuse:
    """`create_score` and `flush`, recorded. `fail_on` names score ids whose
    `create_score` raises; `flush_raises` makes the hand-over fail."""

    def __init__(self, fail_on=(), flush_raises=False, on_create=None):
        self.scores: list[dict] = []
        self.fail_on = set(fail_on)
        self.flush_raises = flush_raises
        self.on_create = on_create
        self.flushed = 0

    def create_score(self, **kw):
        if kw["score_id"] in self.fail_on:
            raise RuntimeError("ingestion refused")
        if self.on_create:
            self.on_create(kw)
        self.scores.append(kw)

    def flush(self):
        if self.flush_raises:
            raise RuntimeError("connection reset")
        self.flushed += 1


def cfg_for(state_dir, **langfuse):
    return make_config(state_dir=state_dir,
                       langfuse={"enabled": True, **langfuse})


def queued(state_dir, n: int) -> list[dict]:
    """`n` incident acts, each queued the way `queue_action` would."""
    target = scores.Target(run_id=RUN, stage="ingest", event_id=INGEST_EVENT)
    for i in range(n):
        for rec in scores.incident_records(
                target, slug=f"slug-{i}", verb="resolve",
                at=f"2026-09-24T10:0{i}:00Z", surface="cli",
                commit=f"c{i}"):
            scores._append(state_dir, rec)
    return pending(state_dir)


def test_send_posts_each_pending_score_once_and_empties_the_queue(tmp_path):
    rows = queued(tmp_path, 2)
    fake = FakeLangfuse()
    out = scores.send(cfg_for(tmp_path, environment="onprem"), client=fake)
    assert (out.sent, out.failed, out.pending) == (4, 0, 0)
    assert [s["score_id"] for s in fake.scores] == [r["key"] for r in rows]
    first = fake.scores[0]
    assert first["name"] == "human_accepted" and first["value"] == 1.0
    assert first["data_type"] == "BOOLEAN"
    assert first["trace_id"] == observability.trace_id(INGEST_EVENT)
    assert first["session_id"] is None
    assert first["environment"] == "onprem"
    assert first["metadata"]["stage"] == "ingest"
    assert fake.flushed == 1
    assert pending(tmp_path) == []

    again = scores.send(cfg_for(tmp_path), client=fake)
    assert again.sent == 0 and len(fake.scores) == 4, "never posted twice"


def test_a_duplicate_key_is_sent_once_with_its_latest_value(tmp_path):
    target = scores.Target(run_id=RUN, stage="review",
                           event_id=REVIEW_EVENT)
    for decision in ("acknowledge", "suppress"):
        for rec in scores.review_records(target, fingerprint=FP,
                                         decision=decision, at=NOW):
            scores._append(tmp_path, rec)
    fake = FakeLangfuse()
    out = scores.send(cfg_for(tmp_path), client=fake)
    accepted = [s for s in fake.scores if s["name"] == "human_accepted"]
    assert len(accepted) == 1 and accepted[0]["value"] == 0.0
    assert out.pending == 0 and pending(tmp_path) == []


def test_a_session_level_score_names_the_session_not_a_trace(tmp_path):
    target = scores.Target(run_id=RUN, stage="report", event_id=None)
    for rec in scores.incident_records(target, slug="s", verb="monitor",
                                       at=NOW, surface="portal", commit="c"):
        scores._append(tmp_path, rec)
    fake = FakeLangfuse()
    scores.send(cfg_for(tmp_path), client=fake)
    assert {s["session_id"] for s in fake.scores} == {RUN}
    assert {s["trace_id"] for s in fake.scores} == {None}


def test_a_partial_failure_keeps_exactly_the_unsent_ones(tmp_path, capsys):
    rows = queued(tmp_path, 2)
    bad = rows[1]["key"]
    fake = FakeLangfuse(fail_on={bad})
    out = scores.send(cfg_for(tmp_path), client=fake)
    assert (out.sent, out.failed, out.pending) == (3, 1, 1)
    assert [r["key"] for r in pending(tmp_path)] == [bad]
    assert "warning: " in capsys.readouterr().err

    retry = FakeLangfuse()
    out = scores.send(cfg_for(tmp_path), client=retry)
    assert [s["score_id"] for s in retry.scores] == [bad]
    assert out.pending == 0


def test_a_failed_flush_keeps_everything_for_an_idempotent_resend(tmp_path):
    rows = queued(tmp_path, 1)
    out = scores.send(cfg_for(tmp_path),
                      client=FakeLangfuse(flush_raises=True))
    assert out.sent == 0 and out.failed == 2 and out.pending == 2
    assert pending(tmp_path) == rows
    retry = FakeLangfuse()
    scores.send(cfg_for(tmp_path), client=retry)
    assert [s["score_id"] for s in retry.scores] == [r["key"] for r in rows], \
        "the same score ids again: Langfuse upserts, nothing is counted twice"


def test_a_score_queued_while_sending_is_kept(tmp_path):
    queued(tmp_path, 1)
    late = scores.Target(run_id="ffffffffffff", stage="ingest",
                         event_id="late00000000")
    late_rows = scores.incident_records(late, slug="late", verb="resolve",
                                        at=NOW, surface="portal", commit="z")

    def append_late(kw):
        if not late_rows[0].get("_done"):
            late_rows[0]["_done"] = True
            for rec in late_rows:
                scores._append(tmp_path, {k: v for k, v in rec.items()
                                          if k != "_done"})

    scores.send(cfg_for(tmp_path), client=FakeLangfuse(on_create=append_late))
    assert [r["subject"] for r in pending(tmp_path)] == ["late", "late"]


def test_send_is_a_no_op_without_langfuse(tmp_path, monkeypatch):
    queued(tmp_path, 1)
    before = (tmp_path / scores.PENDING_LOG).read_bytes()

    def no_client(*a, **kw):
        raise AssertionError("no client may be built")

    monkeypatch.setattr(observability, "_get_client", no_client)
    off = scores.send(make_config(state_dir=tmp_path))
    assert off.skipped == "disabled" and off.sent == 0
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    keyless = scores.send(cfg_for(tmp_path))
    assert keyless.skipped == "no_keys" and keyless.pending == 2
    assert (tmp_path / scores.PENDING_LOG).read_bytes() == before


def test_the_cli_sends_through_the_configured_client(tmp_path, monkeypatch,
                                                     capsys):
    queued(tmp_path, 1)
    fake = FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(observability, "_get_client", lambda lf: fake)
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: cfg_for(tmp_path))
    assert cli.main(["scores", "send"]) == 0
    assert len(fake.scores) == 2
    assert "2 sent" in capsys.readouterr().out
    assert cli.main(["scores", "send"]) == 0
    assert capsys.readouterr().out == "", "nothing pending is silent"


def test_the_cli_is_silent_and_green_without_langfuse(tmp_path, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(cli, "load_config",
                        lambda *a, **kw: make_config(state_dir=tmp_path))
    assert cli.main(["scores", "send"]) == 0
    assert capsys.readouterr() == ("", "")


def test_the_cli_exits_1_when_a_score_failed(tmp_path, monkeypatch, capsys):
    queued(tmp_path, 1)
    monkeypatch.setattr(observability, "_get_client",
                        lambda lf: FakeLangfuse(flush_raises=True))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setattr(cli, "load_config", lambda *a, **kw: cfg_for(tmp_path))
    assert cli.main(["scores", "send"]) == 1


def test_the_send_is_scheduled():
    schedule = json.loads((Path(__file__).resolve().parents[1] / "config"
                           / "schedule.json").read_text())
    rows = [e for e in schedule["entries"] if e["entry"] == "scores-send"]
    assert len(rows) == 1 and rows[0]["command"] == "dbwiki scores send"
    assert rows[0]["node"] == "onprem"


# -- health -------------------------------------------------------------------

LATER = "2999-01-01T00:00:00Z"


def test_the_backlog_counts_only_past_its_age_and_only_when_enabled(tmp_path):
    rows = queued(tmp_path, 1)
    soon = scores.backlog(cfg_for(tmp_path), now=rows[0]["queued_at"])
    assert soon["pending"] == 2 and soon["stale"] == 0
    late = scores.backlog(cfg_for(tmp_path, score_backlog_hours=24), now=LATER)
    assert late["stale"] == 2 and late["threshold_h"] == 24
    assert late["enabled"] is True
    assert late["oldest"] == rows[0]["queued_at"]
    off = scores.backlog(make_config(state_dir=tmp_path),
                         now=LATER)
    assert off["enabled"] is False and off["stale"] == 0


def test_health_shows_a_stale_backlog_as_a_warning_not_a_problem(
        health_cfg):
    before = health.assess(health_cfg, es=fake_es(health_cfg, alert=[FRESH]),
                           now=LATER)
    health_cfg.langfuse = {"enabled": True}
    queued(health_cfg.state_dir, 1)
    h = health.assess(health_cfg, es=fake_es(health_cfg, alert=[FRESH]),
                      now=LATER)
    assert h["scores"]["stale"] == 2
    assert h["problems"] == before["problems"], "a warning, never red"
    assert h["exit_code"] == before["exit_code"]
    text = health.format_health(h)
    assert "WARNING" in text and "dbwiki scores send" in text

    health_cfg.langfuse = {}
    quiet = health.format_health(health.assess(
        health_cfg, es=fake_es(health_cfg, alert=[FRESH]), now=LATER))
    assert "scores send" not in quiet
