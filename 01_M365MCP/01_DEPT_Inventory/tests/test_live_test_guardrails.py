"""
Unit Tests for Live-Test Safety Guardrails & Failure Path Verification
======================================================================
Verifies:
1. --no-post / execute_transfers=False strictly bypasses all MB1B transfer functions (0 calls).
2. --no-post / execute_transfers=False strictly bypasses COGI save functions (0 calls).
3. Freshness Guardrail: WMS crawl failure forces can_execute_transfers = False.
4. Freshness Guardrail: Time difference > 300s forces can_execute_transfers = False.
5. Freshness Guardrail: MB52 export failure forces can_execute_transfers = False.
"""

import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd

TEST_DIR = Path(__file__).resolve().parent
INVENTORY_DIR = TEST_DIR.parent
PROJECT_ROOT = INVENTORY_DIR.parent
JOB_01_DIR = INVENTORY_DIR / "01_JOB_GR_GI_Error"
JOB_02_DIR = INVENTORY_DIR / "02_JOB_Backlog_Clear"
HQ_DIR = PROJECT_ROOT / "00_HQ_TaskManager"

for p in [str(HQ_DIR), str(JOB_01_DIR), str(JOB_02_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

import sync_snapshot_collector


def test_failure_wms_crawl_failed_sets_can_execute_transfers_false(tmp_path):
    """Failure Path: WMS crawl failure falling back to 04:00 forces can_execute_transfers = False."""
    mb52_file = tmp_path / "MB52_test.XLS"
    mb52_file.write_text("dummy mb52", encoding="utf-8")
    wms_fallback_file = tmp_path / "WMS_fallback.xlsx"
    wms_fallback_file.write_text("dummy wms fallback", encoding="utf-8")

    # Set identical mtime
    now = time.time()
    os.utime(str(mb52_file), (now, now))
    os.utime(str(wms_fallback_file), (now, now))

    with patch("sync_snapshot_collector.export_sap_mb52", return_value=(mb52_file, True)), \
         patch("sync_snapshot_collector.export_wms_stock", return_value=(wms_fallback_file, False)):  # is_fresh=False!
        results = sync_snapshot_collector.collect_simultaneous_snapshots(max_freshness_seconds=300.0)

    assert results["is_wms_fresh"] is False
    assert results["can_execute_transfers"] is False


def test_failure_time_difference_exceeds_300s_blocks_transfers(tmp_path):
    """Failure Path: Time difference between WMS and MB52 > 300s forces can_execute_transfers = False."""
    mb52_file = tmp_path / "MB52_test.XLS"
    mb52_file.write_text("dummy mb52", encoding="utf-8")
    wms_file = tmp_path / "WMS_test.xlsx"
    wms_file.write_text("dummy wms", encoding="utf-8")

    now = time.time()
    # MB52 at T, WMS at T - 400s (delta = 400s > 300s)
    os.utime(str(mb52_file), (now, now))
    os.utime(str(wms_file), (now - 400.0, now - 400.0))

    with patch("sync_snapshot_collector.export_sap_mb52", return_value=(mb52_file, True)), \
         patch("sync_snapshot_collector.export_wms_stock", return_value=(wms_file, True)):
        results = sync_snapshot_collector.collect_simultaneous_snapshots(max_freshness_seconds=300.0)

    assert results["is_wms_fresh"] is True
    assert results["time_diff_sec"] == 400.0
    assert results["can_execute_transfers"] is False


def test_failure_mb52_collection_failed_blocks_transfers(tmp_path):
    """Failure Path: MB52 extraction failure (None or Stale Fallback) forces can_execute_transfers = False."""
    wms_file = tmp_path / "WMS_test.xlsx"
    wms_file.write_text("dummy wms", encoding="utf-8")
    stale_mb52 = tmp_path / "MB52_stale.XLS"
    stale_mb52.write_text("dummy stale mb52", encoding="utf-8")

    now = time.time()
    os.utime(str(wms_file), (now, now))
    os.utime(str(stale_mb52), (now, now)) # Even if delta is 0!

    # Case 1: MB52 returned None
    with patch("sync_snapshot_collector.export_sap_mb52", return_value=(None, False)), \
         patch("sync_snapshot_collector.export_wms_stock", return_value=(wms_file, True)):
        results1 = sync_snapshot_collector.collect_simultaneous_snapshots(max_freshness_seconds=300.0)

    assert results1["SAP_MB52"] is None
    assert results1["is_sap_fresh"] is False
    assert results1["can_execute_transfers"] is False

    # Case 2: MB52 returned stale fallback file (is_sap_fresh = False)
    with patch("sync_snapshot_collector.export_sap_mb52", return_value=(stale_mb52, False)), \
         patch("sync_snapshot_collector.export_wms_stock", return_value=(wms_file, True)):
        results2 = sync_snapshot_collector.collect_simultaneous_snapshots(max_freshness_seconds=300.0)

    assert results2["SAP_MB52"] == stale_mb52
    assert results2["is_sap_fresh"] is False
    assert results2["can_execute_transfers"] is False


def test_success_fresh_simultaneous_snapshots_allows_transfers(tmp_path):
    """Success Path: Fresh WMS and Fresh MB52 (<300s) authorizes transfers."""
    mb52_file = tmp_path / "MB52_test.XLS"
    mb52_file.write_text("dummy mb52", encoding="utf-8")
    wms_file = tmp_path / "WMS_test.xlsx"
    wms_file.write_text("dummy wms", encoding="utf-8")

    now = time.time()
    os.utime(str(mb52_file), (now, now))
    os.utime(str(wms_file), (now - 45.0, now - 45.0))  # 45s delta

    with patch("sync_snapshot_collector.export_sap_mb52", return_value=(mb52_file, True)), \
         patch("sync_snapshot_collector.export_wms_stock", return_value=(wms_file, True)):
        results = sync_snapshot_collector.collect_simultaneous_snapshots(max_freshness_seconds=300.0)

    assert results["is_wms_fresh"] is True
    assert results["is_sap_fresh"] is True
    assert results["time_diff_sec"] == 45.0
    assert results["can_execute_transfers"] is True


def test_01_job_execute_transfers_false_strictly_bypasses_mb1b(tmp_path):
    """Safety Guardrail: In 01_JOB, execute_transfers=False guarantees 0 calls to execute_batch_postings."""
    import hourly_gi_pipeline

    dummy_gi = tmp_path / "dummy_gi.XLS"
    dummy_gi.write_text("Material Number\tFromSt\tTOSt\tQty\tMessage\n99000026\tSL01\tSP31\t10\tDeficit", encoding="utf-16")
    dummy_gr = tmp_path / "dummy_gr.XLS"
    dummy_gr.write_text("Material Number\tSLoc\tQty\tMessage\n", encoding="utf-16")

    dummy_plan = pd.DataFrame([{
        'Material': '99000026', 'Action': 'TRANSFER_POSTING', 'Status': 'READY_FOR_EXECUTION',
        'From_SLoc': 'SL01', 'To_SLoc': 'SP31', 'Transfer_Qty': 10.0
    }])

    with patch("hourly_gi_pipeline.ensure_sap_open"), \
         patch("hourly_gi_pipeline.collect_ztcode_gi_err_errors", return_value={'GI_Error': dummy_gi, 'GR_Error': dummy_gr}), \
         patch("hourly_gi_pipeline.collect_simultaneous_snapshots", return_value={'can_execute_transfers': True}), \
         patch("hourly_gi_pipeline.resolve_gi_errors", return_value=dummy_plan), \
         patch("hourly_gi_pipeline.execute_batch_postings") as mock_mb1b, \
         patch("sap_lock.sap_session_lock"):

        res = hourly_gi_pipeline._run_pipeline_core(execute_transfers=False, send_email=False)

    assert mock_mb1b.call_count == 0, f"Expected 0 calls to execute_batch_postings, got {mock_mb1b.call_count}"


def test_01_job_gr_extraction_failure_strictly_disables_transfers_and_ignores_stale_files(tmp_path):
    """
    Guardrail Verification (Thread 048 / Commit ⑥):
    If fresh GR extraction fails in this cycle (returns None),
    execute_transfers MUST be forced to False (0 transfers executed),
    and resolve_gi_errors must NOT glob or pick up stale files on disk.
    """
    import hourly_gi_pipeline

    dummy_gi = tmp_path / "GI_Error_fresh.XLS"
    dummy_gi.write_text("Material Number\tFromSt\tTOSt\tQty\tMessage\n99000026\tSL01\tSP31\t10\tDeficit", encoding="utf-16")

    # Caller requests execute_transfers=True
    with patch("hourly_gi_pipeline.ensure_sap_open"), \
         patch("hourly_gi_pipeline.collect_ztcode_gi_err_errors", return_value={'GI_Error': dummy_gi, 'GR_Error': None}), \
         patch("hourly_gi_pipeline.collect_simultaneous_snapshots", return_value={'can_execute_transfers': True}), \
         patch("hourly_gi_pipeline.resolve_gi_errors") as mock_resolve, \
         patch("hourly_gi_pipeline.execute_batch_postings") as mock_mb1b, \
         patch("sap_lock.sap_session_lock"):

        # Mock resolve_gi_errors returning a plan with TRANSFER_POSTING
        mock_resolve.return_value = pd.DataFrame([{
            'Material': '99000026', 'Action': 'TRANSFER_POSTING', 'Status': 'READY_FOR_EXECUTION',
            'From_SLoc': 'SL01', 'To_SLoc': 'SP31', 'Transfer_Qty': 10.0
        }])

        res = hourly_gi_pipeline._run_pipeline_core(execute_transfers=True, send_email=False)

    # 1. Verify execute_batch_postings was NEVER called (0 transfers executed)
    assert mock_mb1b.call_count == 0, f"Transfers were executed ({mock_mb1b.call_count}) despite GR extraction failure!"

    # 2. Verify resolve_gi_errors was called with gr_path=None (not an old file from disk)
    assert mock_resolve.call_count == 1
    call_kwargs = mock_resolve.call_args.kwargs
    assert call_kwargs.get("gr_path") is None, f"Expected gr_path=None, got {call_kwargs.get('gr_path')}"
    assert call_kwargs.get("gi_path") == dummy_gi


def test_01_job_skipped_no_message_preservation(tmp_path):
    """
    Guardrail Verification (Thread 048):
    Rows in ZTCODE_GI_ERR with empty or NaN Message (e.g. 99000029, 99000039)
    must be preserved in the resolution plan as SKIPPED_NO_MESSAGE (Action: SKIP),
    not silently dropped.
    """
    from gr_gi_resolver import resolve_gi_errors

    gi_file = tmp_path / "GI_Error_test.XLS"
    gi_content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n"
        "2026.10.26\tV1\t99000029\tDesc1\t311\tSL00\tSL02\t200\n"
        "2026.10.26\tV2\t99000039\tDesc2\t311\tSL01\tSP31\t50\n"
        "2026.10.26\tV3\t99000008\tDesc3\t311\tSL01\tSP31\t10\tDeficit in SP31\n"
    )
    gi_file.write_text(gi_content, encoding="utf-16")

    gr_file = tmp_path / "GR_Error_test.XLS"
    gr_content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tSLoc\tQty\tMessage\n"
        "2026.10.26\tV\t99999999\tDesc\t101\tSL00\t10\tErr\n"
    )
    gr_file.write_text(gr_content, encoding="utf-16")

    plan_df = resolve_gi_errors(gi_path=gi_file, gr_path=gr_file, output_dir=tmp_path)

    assert plan_df is not None and not plan_df.empty
    skipped = plan_df[plan_df['Status'] == 'SKIPPED_NO_MESSAGE']
    assert len(skipped) == 2, f"Expected 2 SKIPPED_NO_MESSAGE rows, got {len(skipped)}"
    assert set(skipped['Material']) == {'99000029', '99000039'}
    for _, row in skipped.iterrows():
        assert row['Action'] == 'SKIP'


def test_02_job_execute_transfers_false_strictly_bypasses_mb1b_and_cogi_save(tmp_path):
    """Safety Guardrail: In 02_JOB, execute_transfers=False guarantees 0 calls to execute_backlog_batch and post_and_save_cogi."""
    sys.modules.pop("db_manager", None)
    if str(JOB_02_DIR) in sys.path:
        sys.path.remove(str(JOB_02_DIR))
    sys.path.insert(0, str(JOB_02_DIR))
    import backlog_pipeline

    dummy_cogi_items = [{'Material': '99000003', 'Qty': 100.0, 'SLoc': 'SP31'}]
    dummy_plan = pd.DataFrame([{
        'Material': '99000003', 'Action': 'TRANSFER_POSTING', 'Status': 'APPROVED_FULL_RESOLUTION',
        'From_SLoc': 'SL00', 'To_SLoc': 'SP31', 'Transfer_Qty': 100.0, 'Deficit_Qty': 100.0
    }])

    test_db = tmp_path / "test_backlog_history.db"

    dummy_gi = tmp_path / "dummy_gi.XLS"
    dummy_gi.write_text("Material Number\tFromSt\tTOSt\tQty\tMessage\n99000003\tSL01\tSP31\t100\tDeficit", encoding="utf-16")
    dummy_gr = tmp_path / "dummy_gr.XLS"
    dummy_gr.write_text("Material Number\tSLoc\tQty\tMessage\n", encoding="utf-16")

    with patch("backlog_pipeline.run_precursor_gi_pipeline"), \
         patch("ztcode_gi_collector.collect_ztcode_gi_err_errors", return_value={'GI_Error': dummy_gi, 'GR_Error': dummy_gr}), \
         patch("backlog_pipeline.export_cogi_errors", return_value=(Path("dummy_cogi.xls"), dummy_cogi_items)), \
         patch("sync_snapshot_collector.collect_simultaneous_snapshots", return_value={'can_execute_transfers': True}), \
         patch("backlog_pipeline.build_backlog_resolution_plan", return_value=dummy_plan), \
         patch("backlog_resolver.extract_remaining_sap_deficits", return_value=[]), \
         patch("db_manager.record_unresolved_sap_deficits", return_value=0), \
         patch("db_manager.log_daily_metric", return_value=1), \
         patch("backlog_pipeline.execute_backlog_batch") as mock_mb1b, \
         patch("backlog_pipeline.post_and_save_cogi") as mock_cogi_save, \
         patch("backlog_pipeline.refresh_and_extract_initial_backlog") as mock_refresh_save, \
         patch("backlog_pipeline.log_cogi_snapshot"), \
         patch("sap_lock.sap_session_lock"):

        res = backlog_pipeline._execute_backlog_cycle_core(
            execute_transfers=False,
            send_email=False,
            pre_refresh=True,
            db_path=test_db,
            output_dir=tmp_path,
            save_plan_excel=False
        )

    assert mock_mb1b.call_count == 0, f"Expected 0 calls to execute_backlog_batch, got {mock_mb1b.call_count}"
    assert mock_cogi_save.call_count == 0, f"Expected 0 calls to post_and_save_cogi, got {mock_cogi_save.call_count}"
    assert mock_refresh_save.call_count == 0, f"Expected 0 calls to refresh_and_extract_initial_backlog, got {mock_refresh_save.call_count}"
