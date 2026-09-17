import json
from pathlib import Path

import pytest

from dbwiki.state import (REGISTRY_SCHEMA_VERSION, Registry, StateStore,
                          atomic_write_text)


WINDOW = ("2026-07-10T00:00:00Z", "2026-07-11T00:00:00Z")


def test_note_is_idempotent_across_reruns(tmp_path):
    path = tmp_path / "cdb1.json"
    for _ in range(3):  # same window compacted three times
        reg = Registry(path)
        reg.note_code("alert", "ORA-600", "2026-07-10T10:00:00Z")
        reg.note_code("alert", "ORA-600", "2026-07-10T12:00:00Z")
        reg.save()
    reg = Registry(path)
    assert reg.codes["alert"]["ORA-600"] == {"first_seen": "2026-07-10T10:00:00Z",
                                             "last_seen": "2026-07-10T12:00:00Z"}


def test_legacy_count_field_is_dropped_on_load(tmp_path):
    path = tmp_path / "cdb1.json"
    path.write_text(json.dumps({"schema_version": 2, "codes": {"alert": {"ORA-600": {
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z", "count": 47}}}}))
    reg = Registry(path)
    assert "count" not in reg.codes["alert"]["ORA-600"]
    reg.save()
    on_disk = json.loads(path.read_text())
    assert "count" not in on_disk["codes"]["alert"]["ORA-600"]


def test_new_in_window_uses_first_seen(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-600", "2026-07-10T10:00:00Z")
    reg.note_code("alert", "ORA-1555", "2026-07-09T10:00:00Z")
    assert reg.new_codes_in_window("alert", *WINDOW) == ["ORA-600"]


# ---- per-source code keying --------------------------------------------------

def test_codes_are_tracked_per_source(tmp_path):
    """A code long known in one log is new the first time another log carries
    it — that is the signal per-source keying exists to keep."""
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-600", "2026-01-01T00:00:00Z")
    reg.note_code("dataguard", "ORA-600", "2026-07-10T10:00:00Z")
    assert reg.new_codes_in_window("alert", *WINDOW) == []
    assert reg.new_codes_in_window("dataguard", *WINDOW) == ["ORA-600"]
    assert sorted(reg.codes) == ["alert", "dataguard"]


def test_a_code_is_first_ever_only_once_per_source(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "ORA-600", "2026-07-10T10:00:00Z")
    reg.note_code("alert", "ORA-600", "2026-07-10T12:00:00Z")
    assert reg.new_codes_in_window("alert", *WINDOW) == ["ORA-600"]
    # next window: the code is no longer new
    assert reg.new_codes_in_window(
        "alert", "2026-07-11T00:00:00Z", "2026-07-12T00:00:00Z") == []


def test_empty_code_never_creates_a_source_bucket(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.note_code("alert", "", "2026-07-10T10:00:00Z")
    assert reg.codes == {}


def test_v1_global_codes_migrate_into_the_grandfather_bucket(tmp_path):
    """The v1 map had no source attribution, so it migrates whole into
    legacy_codes: on the first post-migration run the code must not delta as
    first-ever for the source that happens to see it next."""
    path = tmp_path / "cdb1.json"
    v1 = {"schema_version": 1,
          "codes": {"ORA-3113": {"first_seen": "2026-05-02T08:00:00Z",
                                 "last_seen": "2026-07-01T08:00:00Z"}},
          "services": {}, "programs": {}, "daily_counts": {}}
    path.write_text(json.dumps(v1))

    reg = Registry(path)
    assert reg.codes == {}
    assert reg.legacy_codes["ORA-3113"]["first_seen"] == "2026-05-02T08:00:00Z"

    reg.note_code("dataguard", "ORA-3113", "2026-07-10T10:00:00Z")
    assert reg.new_codes_in_window("dataguard", *WINDOW) == []
    reg.save()

    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == 2
    assert on_disk["codes"] == {"dataguard": {
        "ORA-3113": {"first_seen": "2026-07-10T10:00:00Z",
                     "last_seen": "2026-07-10T10:00:00Z"}}}
    assert on_disk["legacy_codes"] == v1["codes"]
    assert json.loads((tmp_path / "cdb1.json.bak").read_text()) == v1

    # grandfathering survives a reload, and holds for every source
    reg = Registry(path)
    reg.note_code("alert", "ORA-3113", "2026-07-10T11:00:00Z")
    assert reg.new_codes_in_window("alert", *WINDOW) == []


def test_known_dbs_ignores_the_migration_backups(tmp_path):
    """The v1 -> v2 migration drops a `<db>.json.bak` beside every registry;
    it must not read back as a database named `<db>.json`."""
    store = StateStore(tmp_path)
    reg_dir = tmp_path / "registry"
    reg_dir.mkdir()
    (reg_dir / "cdb1.json").write_text(json.dumps({"schema_version": 1, "codes": {}}))
    Registry(reg_dir / "cdb1.json").save()
    assert (reg_dir / "cdb1.json.bak").exists()
    assert store.known_dbs() == ["cdb1"]


def test_unversioned_registry_codes_are_grandfathered_too(tmp_path):
    path = tmp_path / "cdb1.json"
    path.write_text(json.dumps({"codes": {"ORA-600": {
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z"}}}))
    reg = Registry(path)
    reg.note_code("alert", "ORA-600", "2026-07-10T10:00:00Z")
    assert reg.new_codes_in_window("alert", *WINDOW) == []


def test_shorter_window_never_overwrites_longer(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.set_day_counts("2026-07-10", "alert", 1000, {"log_switch": 100}, 86400)
    # a partial ad-hoc re-run must not degrade the full-day record
    reg.set_day_counts("2026-07-10", "alert", 20, {"log_switch": 2}, 7200)
    assert reg.daily_counts["2026-07-10"]["alert"]["total"] == 1000
    # an equal-or-longer window (full-day re-run, growing run tick) does update
    reg.set_day_counts("2026-07-10", "alert", 1200, {"log_switch": 120}, 86400)
    assert reg.daily_counts["2026-07-10"]["alert"]["total"] == 1200


def test_baseline_pairs_with_legacy_full_day_default(tmp_path):
    reg = Registry(tmp_path / "r.json")
    # entry written before window coverage was tracked: assume a full day
    reg.daily_counts["2026-07-08"] = {"alert": {"total": 5, "counters": {"c": 5}}}
    reg.set_day_counts("2026-07-09", "alert", 10, {"c": 10}, 43200)
    assert reg.baseline("2026-07-10", "alert", "c", 7) == \
        [(5, 86400.0), (10, 43200)]


def test_active_days_before(tmp_path):
    reg = Registry(tmp_path / "r.json")
    reg.set_day_counts("2026-07-07", "alert", 10, {}, 86400)
    reg.set_day_counts("2026-07-08", "alert", 0, {}, 86400)
    reg.set_day_counts("2026-07-09", "alert", 10, {}, 86400)
    assert reg.active_days_before("2026-07-10", "alert", 3) == 2


def test_legacy_watermarks_load_as_flat_mapping(tmp_path):
    (tmp_path / "watermarks.json").write_text(
        json.dumps({"cdb1": "2026-07-10T00:00:00Z"}))
    store = StateStore(tmp_path)
    assert store.get_watermarks() == {"cdb1": "2026-07-10T00:00:00Z"}


def test_legacy_ledger_loads_as_flat_mapping(tmp_path):
    (tmp_path / "ingest_ledger.json").write_text(
        json.dumps({"a/b.json": {"status": "ok"}}))
    store = StateStore(tmp_path)
    assert store.get_ledger() == {"a/b.json": {"status": "ok"}}


def test_watermarks_resave_migrates_shape_and_backs_up_once(tmp_path):
    path = tmp_path / "watermarks.json"
    path.write_text(json.dumps({"cdb1": "2026-07-10T00:00:00Z"}))
    store = StateStore(tmp_path)
    store.set_watermark("cdb2", "2026-07-11T00:00:00Z")

    on_disk = json.loads(path.read_text())
    assert on_disk == {"schema_version": 1, "watermarks": {
        "cdb1": "2026-07-10T00:00:00Z", "cdb2": "2026-07-11T00:00:00Z"}}
    bak = tmp_path / "watermarks.json.bak"
    assert json.loads(bak.read_text()) == {"cdb1": "2026-07-10T00:00:00Z"}

    # a second write must not clobber an existing backup
    bak.write_text("sentinel")
    store.set_watermark("cdb3", "2026-07-12T00:00:00Z")
    assert bak.read_text() == "sentinel"


def test_ledger_entries_survive_migration(tmp_path):
    path = tmp_path / "ingest_ledger.json"
    path.write_text(json.dumps({"a/b.json": {"status": "ok"}}))
    store = StateStore(tmp_path)
    store.set_ledger_entry("c/d.json", {"status": "pending"})

    assert store.get_ledger() == {
        "a/b.json": {"status": "ok"}, "c/d.json": {"status": "pending"}}
    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == 1
    assert on_disk["entries"]["a/b.json"] == {"status": "ok"}
    assert (tmp_path / "ingest_ledger.json.bak").exists()


def test_registry_resave_migrates_shape_and_backs_up_once(tmp_path):
    path = tmp_path / "cdb1.json"
    path.write_text(json.dumps({"codes": {"ORA-600": {
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z"}}}))
    reg = Registry(path)
    reg.save()

    on_disk = json.loads(path.read_text())
    assert on_disk["schema_version"] == 2
    assert "codes" in on_disk and "services" in on_disk
    bak = tmp_path / "cdb1.json.bak"
    assert "schema_version" not in json.loads(bak.read_text())

    # a second save must not clobber an existing backup
    bak.write_text("sentinel")
    reg.save()
    assert bak.read_text() == "sentinel"


def test_new_watermarks_and_ledger_round_trip_with_version(tmp_path):
    store = StateStore(tmp_path)
    store.set_watermark("cdb1", "2026-07-10T00:00:00Z")
    store.set_ledger_entry("a/b.json", {"status": "ok"})

    assert store.get_watermarks() == {"cdb1": "2026-07-10T00:00:00Z"}
    assert store.get_ledger() == {"a/b.json": {"status": "ok"}}
    assert json.loads((tmp_path / "watermarks.json").read_text())["schema_version"] == 1
    assert json.loads((tmp_path / "ingest_ledger.json").read_text())["schema_version"] == 1
    assert not (tmp_path / "watermarks.json.bak").exists()


def test_too_new_watermarks_schema_version_raises(tmp_path):
    (tmp_path / "watermarks.json").write_text(
        json.dumps({"schema_version": 99, "watermarks": {}}))
    store = StateStore(tmp_path)
    with pytest.raises(ValueError, match="99"):
        store.get_watermarks()


def test_too_new_registry_schema_version_raises(tmp_path):
    path = tmp_path / "cdb1.json"
    path.write_text(json.dumps({"schema_version": 99, "codes": {}}))
    with pytest.raises(ValueError, match="99"):
        Registry(path)


def test_merge_ledger_entry_preserves_existing_fields(tmp_path):
    store = StateStore(tmp_path)
    store.set_ledger_entry("a/b.json", {"status": "ingested", "commit": "abc123"})
    store.merge_ledger_entry("a/b.json", {"last_decision": {"outcome": "wake"}})
    assert store.get_ledger() == {"a/b.json": {
        "status": "ingested", "commit": "abc123",
        "last_decision": {"outcome": "wake"}}}


def test_merge_ledger_entry_creates_entry_when_absent(tmp_path):
    store = StateStore(tmp_path)
    store.merge_ledger_entry("c/d.json", {"last_decision": {"outcome": "skip"}})
    assert store.get_ledger() == {"c/d.json": {"last_decision": {"outcome": "skip"}}}


# ---- atomic writes -----------------------------------------------------------

def test_atomic_write_creates_parents_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "a" / "b" / "f.json"
    atomic_write_text(p, '{"x": 1}')
    assert json.loads(p.read_text()) == {"x": 1}
    assert list(p.parent.iterdir()) == [p]


def test_atomic_write_replaces_in_one_step(tmp_path):
    p = tmp_path / "f.json"
    atomic_write_text(p, "old")
    atomic_write_text(p, "new")
    assert p.read_text() == "new"
    assert sorted(q.name for q in tmp_path.iterdir()) == ["f.json"]


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    atomic_write_text(p, "good")
    real = Path.write_text

    def boom(self, text, *a, **kw):
        real(self, text[:4])          # a half-written tmp file, then a failure
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_text(p, "half a file")
    monkeypatch.undo()
    assert p.read_text() == "good"
    assert sorted(q.name for q in tmp_path.iterdir()) == ["f.json"]


def test_digest_twins_are_written_atomically(tmp_path):
    """The ingest stage hashes these files and the ELK shipper tails the
    directory: neither may ever see a partial digest."""
    import fixtures as fx
    from dbwiki.compactor import Compactor

    cfg = fx.fixture_config(tmp_path)
    digest = fx.golden_digest("routine_traffic")
    jp, mp = Compactor(cfg).emit(digest)
    assert json.loads(jp.read_text())["db"] == digest["db"]
    assert mp.read_text().strip()
    assert sorted(q.name for q in jp.parent.iterdir()) == [jp.name, mp.name]


STAMP = "2026-07-10T10:00:00Z"


def test_a_v1_registry_parks_its_global_codes_as_legacy(tmp_path):
    """v1's `codes` map carried no source attribution, so it cannot be split by
    source after the fact; it is kept whole as grandfathered knowledge."""
    path = tmp_path / "cdb1.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "codes": {"ORA-600": {"first_seen": STAMP, "last_seen": STAMP}},
        "services": {}, "programs": {}, "daily_counts": {}}))
    reg = Registry(path)
    assert reg.codes == {}
    assert reg.legacy_codes == {"ORA-600": {"first_seen": STAMP,
                                            "last_seen": STAMP}}
    reg.save()
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == REGISTRY_SCHEMA_VERSION
    assert saved["codes"] == {}
    assert saved["legacy_codes"] == {"ORA-600": {"first_seen": STAMP,
                                                 "last_seen": STAMP}}
    assert Registry(path).legacy_codes == reg.legacy_codes


def test_the_legacy_count_field_is_dropped_on_load(tmp_path):
    """A per-event counter inflates on every re-compaction of the same window."""
    path = tmp_path / "cdb1.json"
    entry = {"first_seen": STAMP, "last_seen": STAMP, "count": 9}
    path.write_text(json.dumps({
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "codes": {"alert": {"ORA-600": dict(entry)}},
        "legacy_codes": {"ORA-1": dict(entry)},
        "services": {"cdb1.world": dict(entry)},
        "programs": {"oracle@host": dict(entry)},
        "daily_counts": {}}))
    reg = Registry(path)
    for held in (reg.codes["alert"]["ORA-600"], reg.legacy_codes["ORA-1"],
                 reg.services["cdb1.world"], reg.programs["oracle@host"]):
        assert held == {"first_seen": STAMP, "last_seen": STAMP}
