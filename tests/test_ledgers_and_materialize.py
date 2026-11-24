"""Tests for append-only snapshot ledgers, provenance, mode=ro reads, and materialization."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ledgers.snapshot_store import SnapshotStore
from ontology.registry import load_registry
from simulation.synthetic_plant import feed_synthetic_plant


def test_append_only_provenance_and_readonly_enforcement(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    store = SnapshotStore(db_path)
    store.init_db()

    r1 = store.append_snapshot(
        "sloc_master",
        [{"sloc": "SL10", "facility": "Facility North", "count_eligible": True}],
        source_name="seed_v1",
        captured_at="2026-11-23T06:00:00",
    )
    r2 = store.append_snapshot(
        "sloc_master",
        [{"sloc": "SL10", "facility": "Facility North", "count_eligible": False}],
        source_name="seed_v2",
        captured_at="2026-11-23T07:00:00",
    )
    assert len(r1.file_sha256) == 64
    assert r1.file_sha256 != r2.file_sha256

    prov = store.list_provenance("sloc_master")
    assert len(prov) == 2
    assert [p["source_name"] for p in prov] == ["seed_v1", "seed_v2"]

    ro = store.open_ro()
    try:
        raw_count = ro.execute("SELECT COUNT(*) FROM sloc_master").fetchone()[0]
        assert raw_count == 2
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("DELETE FROM sloc_master")
    finally:
        ro.close()


def test_synthetic_plant_feeds_and_materializes_all_simulated_objects(tmp_path: Path) -> None:
    db_path = tmp_path / "plant.db"
    store = SnapshotStore(db_path)
    store.init_db()
    feed_synthetic_plant(store, run_date="2026-11-23", seed=7)

    registry = load_registry(validate=True)
    ro = store.open_ro()
    try:
        counts = registry.materialize(ro)
        expected_names = {s.name for s in registry.simulated_objects()}
        assert set(counts) == expected_names
        assert all(v > 0 for v in counts.values())

        sl90 = ro.execute(
            'SELECT sloc, count_eligible FROM temp."StorageLocation" WHERE sloc = ?',
            ("SL90",),
        ).fetchone()
        assert sl90 is not None
        assert sl90["count_eligible"] == 0

        sl90_bins = ro.execute(
            'SELECT DISTINCT bin_code FROM temp."Box" WHERE sloc = ? AND status = ?',
            ("SL90", "IN_STORAGE"),
        ).fetchall()
        assert len(sl90_bins) == 2
    finally:
        ro.close()
