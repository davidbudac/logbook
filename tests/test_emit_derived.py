"""elk/scripts/emit_derived.py: both ingest-ledger shapes, problem reporting
and the exit code, and when a queue snapshot is worth emitting. Imported by
path like test_cron_next.py — the elk/ scripts tree is a standalone
stdlib-only script (bare python3 from cron, no venv), not part of `dbwiki`."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "elk" / "scripts" / "emit_derived.py"
spec = importlib.util.spec_from_file_location("emit_derived", SCRIPT)
emit_derived = importlib.util.module_from_spec(spec)
spec.loader.exec_module(emit_derived)

REAL_REPO = SCRIPT.resolve().parents[2]

ENTRIES = {
    "digests/cdb1/2026-08-01.json": {
        "at": "2026-08-01T02:15:00Z", "status": "ingested", "run_id": "r1",
        "commit": "abc1234", "task": "ingest",
    },
    "digests/cdb2/2026-08-02.json": {
        "at": "2026-08-02T02:15:00Z", "status": "failed", "run_id": "r2",
        "task": "ingest",
    },
}


@pytest.fixture
def repo(tmp_path):
    """A temporary repo the emitter is pointed at, with the one config it
    always reads present and valid, so a test's own breakage is the only
    problem reported."""
    (tmp_path / ".state" / "elk").mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "schedule.json").write_text(json.dumps(
        {"entries": [{"entry": "tick", "cron": "15 */2 * * *",
                      "command": "dbwiki run", "node": "onprem"}]}))
    (tmp_path / "config" / "dbwiki.yaml").write_text("wiki_repo: wiki\n")
    emit_derived._use_repo(tmp_path)
    yield tmp_path
    emit_derived._use_repo(REAL_REPO)


def docs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_legacy_flat_ledger_emits_every_entry(repo):
    """The live .state/ingest_ledger.json is still the unwrapped shape
    dbwiki.state tolerates; reading only `entries` shipped nothing."""
    (repo / ".state" / "ingest_ledger.json").write_text(json.dumps(ENTRIES))
    problems: list[str] = []
    assert emit_derived.emit_ledger({}, problems) == 2
    assert problems == []
    emitted = docs(emit_derived.LEDGER_FILE)
    assert {d["digest_path"] for d in emitted} == set(ENTRIES)
    assert {d["db"] for d in emitted} == {"cdb1", "cdb2"}
    assert {d["day"] for d in emitted} == {"2026-08-01", "2026-08-02"}


def test_wrapped_ledger_emits_every_entry(repo):
    (repo / ".state" / "ingest_ledger.json").write_text(
        json.dumps({"schema_version": 1, "entries": ENTRIES}))
    problems: list[str] = []
    assert emit_derived.emit_ledger({}, problems) == 2
    assert problems == []
    assert {d["digest_path"] for d in docs(emit_derived.LEDGER_FILE)} == set(ENTRIES)


def test_wrapped_ledger_never_ships_the_version_marker_as_a_digest(repo):
    (repo / ".state" / "ingest_ledger.json").write_text(
        json.dumps({"schema_version": 1, "entries": ENTRIES}))
    emit_derived.emit_ledger({}, [])
    paths = {d["digest_path"] for d in docs(emit_derived.LEDGER_FILE)}
    assert "schema_version" not in paths


def test_unparsable_ledger_is_reported_and_exits_1(repo, capsys):
    (repo / ".state" / "ingest_ledger.json").write_text("{ truncated")
    assert emit_derived.main() == 1
    err = capsys.readouterr().err
    assert "emit_derived: ingest_ledger.json: unparsable" in err
    # the streams that did work are still emitted
    assert len(docs(emit_derived.SCHEDULE_FILE)) == 1


def test_unparsable_run_health_line_is_reported(repo):
    (repo / ".state" / "run_health.jsonl").write_text(
        json.dumps({"run_id": "r1", "command": "run",
                    "dbs": [{"db": "cdb1", "decision": "wake"}]}) + "\n{ torn\n")
    problems: list[str] = []
    assert emit_derived.emit_db_runs({}, problems) == 1  # the good line still ships
    assert problems == ["run_health.jsonl: 1 unparsable line(s) skipped"]


def test_missing_schedule_config_is_a_problem(repo):
    (repo / "config" / "schedule.json").unlink()
    problems: list[str] = []
    assert emit_derived.emit_schedule(problems) == 0
    assert problems and problems[0].startswith("schedule.json: unreadable")


def test_absent_queue_emits_no_snapshot(repo):
    """No wiki/queue/ means the analyst was never enabled — a zeroed snapshot
    every 30 minutes forever is noise, not data."""
    problems: list[str] = []
    assert emit_derived.emit_queue_state(problems) == 0
    assert problems == []
    assert docs(emit_derived.QUEUE_STATE_FILE) == []


def test_queue_snapshot_emitted_once_the_queue_exists(repo):
    pending = repo / "wiki" / "queue" / "pending"
    pending.mkdir(parents=True)
    (pending / "2026-08-01T00-00-00.json").write_text(
        json.dumps({"day": "2026-08-01", "created_at": "2026-08-01T00:00:00Z"}))
    problems: list[str] = []
    assert emit_derived.emit_queue_state(problems) == 1
    assert problems == []
    doc = docs(emit_derived.QUEUE_STATE_FILE)[0]
    assert doc["pending_count"] == 1 and doc["claimed_count"] == 0


def test_unreadable_queue_record_is_reported(repo):
    failed = repo / "wiki" / "queue" / "failed"
    failed.mkdir(parents=True)
    (failed / "bad.json").write_text("{ torn")
    problems: list[str] = []
    assert emit_derived.emit_queue_state(problems) == 1
    assert problems and problems[0].startswith("queue/failed/bad.json:")


def test_a_repo_with_nothing_written_yet_exits_0(repo, capsys):
    """Missing-because-never-created is not a problem."""
    assert emit_derived.main() == 0
    assert capsys.readouterr().err == ""
