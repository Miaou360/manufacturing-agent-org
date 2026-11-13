import os
import sys
import time
from pathlib import Path

BASE_01 = Path(__file__).resolve().parent.parent
BASE_00 = BASE_01.parent / "00_JOB_StockAccuracy"
HQ_DIR = BASE_01.parent.parent / "00_HQ_TaskManager"
for _p in (str(BASE_01), str(BASE_00), str(HQ_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import wms_crawler
import sync_snapshot_collector
import persistence_guard


def test_wms_crawler_detects_in_place_overwrite(tmp_path):
    """Verify that when Stock List_YYYYMMDD.xlsx already exists and is overwritten in-place, _detect_completed_download detects it via mtime."""
    existing = tmp_path / "Stock List_20261028.xlsx"
    existing.write_bytes(b"old_content" * 100)
    old_mtime = time.time() - 1800.0
    os.utime(existing, (old_mtime, old_mtime))

    before_mtimes = {str(existing): os.path.getmtime(existing)}

    # Before overwrite: should NOT be detected
    detected, _ = wms_crawler._detect_completed_download(tmp_path, before_mtimes)
    assert detected is None

    # Simulate Chrome overwriting Stock List_20261028.xlsx in-place with newer mtime
    new_mtime = time.time()
    existing.write_bytes(b"new_content" * 100)
    os.utime(existing, (new_mtime, new_mtime))

    detected_after, from_dl = wms_crawler._detect_completed_download(tmp_path, before_mtimes)
    assert detected_after is not None
    assert detected_after.resolve() == existing.resolve()
    assert from_dl is False


def test_sync_snapshot_collector_renames_raw_stock_list_and_preserves_history(tmp_path):
    """Verify _ensure_canonical_wms_filename renames Stock List_YYYYMMDD.xlsx to WMS_StockList_HL_... and preserves mtime."""
    raw_file = tmp_path / "Stock List_20261028.xlsx"
    raw_file.write_bytes(b"x" * 60000)
    fixed_mtime = 1793221444.0  # 2026-10-28 17:04:04 EDT
    os.utime(raw_file, (fixed_mtime, fixed_mtime))

    renamed = sync_snapshot_collector._ensure_canonical_wms_filename(raw_file)
    assert not raw_file.exists()
    assert renamed.exists()
    assert renamed.name.startswith("WMS_StockList_HL_20261028_")
    assert abs(renamed.stat().st_mtime - fixed_mtime) < 1.0


def test_quarantine_stale_crdownloads_moves_old_files_to_scratch(tmp_path):
    """Verify .crdownload older than 1 hour is moved to parent/scratch (not deleted) while recent .crdownload stays."""
    save_dir = tmp_path / "wms_parts_stack"
    save_dir.mkdir(parents=True)

    stale_cr = save_dir / "downloads.htm (1).crdownload"
    stale_cr.write_bytes(b"stale_data" * 50)
    now_ts = time.time()
    old_mtime = now_ts - 7200.0  # 2 hours old
    os.utime(stale_cr, (old_mtime, old_mtime))

    fresh_cr = save_dir / "Stock List_20261028.xlsx.crdownload"
    fresh_cr.write_bytes(b"fresh_data" * 50)
    fresh_mtime = now_ts - 600.0  # 10 minutes old
    os.utime(fresh_cr, (fresh_mtime, fresh_mtime))

    moved = wms_crawler._quarantine_stale_crdownloads(save_dir, max_age_seconds=3600.0, now_ts=now_ts)
    assert len(moved) == 1
    assert not stale_cr.exists()
    assert moved[0].exists()
    assert moved[0].parent == tmp_path / "scratch"
    assert fresh_cr.exists()


def test_get_active_crdownloads_ignores_pre_click_mtime(tmp_path):
    """Verify _get_active_crdownloads ignores .crdownload with mtime before click_start_time."""
    save_dir = tmp_path / "wms_parts_stack"
    save_dir.mkdir(parents=True)

    click_time = time.time()
    pre_click_cr = save_dir / "pre_click.crdownload"
    pre_click_cr.write_bytes(b"old")
    os.utime(pre_click_cr, (click_time - 30.0, click_time - 30.0))

    assert wms_crawler._get_active_crdownloads(save_dir, click_start_time=click_time) == []

    post_click_cr = save_dir / "post_click.crdownload"
    post_click_cr.write_bytes(b"active")
    os.utime(post_click_cr, (click_time + 1.0, click_time + 1.0))

    active = wms_crawler._get_active_crdownloads(save_dir, click_start_time=click_time)
    assert len(active) == 1
    assert Path(active[0]).name == "post_click.crdownload"
