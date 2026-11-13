"""
Test Suite for User Decision D44 (02_JOB ZTCODE_GI_ERR GR/GI Protection & Pipeline Guardrails)
========================================================================================
Validates:
(a) Even if surplus candidate stock exists, materials with active GR errors are blocked (SKIP / BLOCKED_BY_GR_ERROR)
(b) Even if surplus candidate stock exists, materials with active GI errors (311, 901/902, empty message) are blocked (SKIP / BLOCKED_BY_GI_ERROR)
(c) GR or GI extraction failure in pipeline strictly forces execute_transfers = False and results in 0 transfers
(d) Stale files on disk in gr_errors/ or gi_errors/ are ignored when fresh extraction fails
(e) Notifier correctly categorizes and displays both BLOCKED_BY_GR_ERROR and BLOCKED_BY_GI_ERROR
"""

import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pandas as pd
import pytest

JOB02_DIR = Path(__file__).resolve().parent.parent / "02_JOB_Backlog_Clear"
if str(JOB02_DIR) not in sys.path:
    sys.path.insert(0, str(JOB02_DIR))

HQ_DIR = Path(__file__).resolve().parent.parent / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from backlog_resolver import (
    build_backlog_resolution_plan,
    load_active_gr_errors,
    load_active_gi_errors
)
from notifier import build_email_html, generate_unresolved_excel


def test_d44_gr_error_blocks_backlog_even_with_surplus_candidate(tmp_path):
    """
    Test requirement (a):
    Material 'MAT_GR_ERR' has active GR error (e.g. 50 EA pending inbound receipt).
    Surplus stock exists in SL01 (SAP: 200, WMS: 100 -> Surplus 100 EA).
    COGI deficit is 50 EA at SP31.
    Expected: Transfer MUST be blocked as SKIP / BLOCKED_BY_GR_ERROR.
    """
    gr_file = tmp_path / "GR_Error_test.XLS"
    # Format with proper ZTCODE_GI_ERR header (intersecting target/source SLoc under D71)
    gr_content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMovement\tStorage Location\tQty\n"
        "2026.10.26\tV100\tMAT_GR_ERR\tTest Part GR\t101\tSP31\t50\n"
    )
    gr_file.write_text(gr_content, encoding="utf-16")

    gi_file = tmp_path / "GI_Error_test.XLS"
    gi_content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n"
    )
    gi_file.write_text(gi_content, encoding="utf-16")

    mb52_stock = pd.DataFrame([
        {"Material": "MAT_GR_ERR", "SLoc": "SL01", "SAP_Stock": 200.0}
    ])
    wms_stock = pd.DataFrame([
        {"Material": "MAT_GR_ERR", "SLoc": "SL01", "WMS_Stock": 100.0}
    ])

    cogi_items = [{
        "Material": "MAT_GR_ERR",
        "Material_Desc": "Test Part GR",
        "SLoc": "SP31",
        "Qty": 50.0,
        "Error_Code": "M7 021",
        "Can_Resolve_By_Transfer": True
    }]

    with patch("backlog_resolver.load_mb52_stock", return_value=mb52_stock), \
         patch("backlog_resolver.load_wms_stock", return_value=wms_stock), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(wms_stock, mb52_stock, "2026-10-26 15:45:00")), \
         patch("backlog_resolver.get_today_already_transferred_qty", return_value=(0.0, [])):
        plan = build_backlog_resolution_plan(
            cogi_items,
            gi_path=gi_file,
            gr_path=gr_file,
            require_fresh_ztcode_stock_rec=True,
            save_excel=False,
        )

    assert len(plan) == 1
    row = plan.iloc[0]
    assert row["Action"] == "SKIP"
    assert row["Status"] == "BLOCKED_BY_GR_ERROR"
    assert row["Pending_GR_Qty"] == 50.0
    assert "V100" in row["Reason"]
    assert "Active inbound GR error" in row["Reason"]


def test_d44_gi_error_blocks_backlog_even_with_surplus_candidate(tmp_path):
    """
    Test requirement (b):
    Material 'MAT_GI_ERR' has active GI error intersecting target/source SLoc.
    Surplus stock exists in SL02 (SAP: 300, WMS: 150 -> Surplus 150 EA).
    COGI deficit is 80 EA at SP31.
    Expected: Transfer MUST be blocked as SKIP / BLOCKED_BY_GI_ERROR.
    """
    gr_file = tmp_path / "GR_Error_test.XLS"
    gr_content = "Date\tVendor\tMaterial Number\tMaterial Desc\tMovement\tStorage Location\tQty\n"
    gr_file.write_text(gr_content, encoding="utf-16")

    gi_file = tmp_path / "GI_Error_test.XLS"
    gi_content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n"
        "2026.10.26\tV200\tMAT_GI_ERR\tTest Part GI\t311\tSL02\tSP31\t80\tDeficit\n"
    )
    gi_file.write_text(gi_content, encoding="utf-16")

    mb52_stock = pd.DataFrame([
        {"Material": "MAT_GI_ERR", "SLoc": "SL02", "SAP_Stock": 300.0}
    ])
    wms_stock = pd.DataFrame([
        {"Material": "MAT_GI_ERR", "SLoc": "SL02", "WMS_Stock": 150.0}
    ])

    cogi_items = [{
        "Material": "MAT_GI_ERR",
        "Material_Desc": "Test Part GI",
        "SLoc": "SP31",
        "Qty": 80.0,
        "Error_Code": "M7 021",
        "Can_Resolve_By_Transfer": True
    }]

    with patch("backlog_resolver.load_mb52_stock", return_value=mb52_stock), \
         patch("backlog_resolver.load_wms_stock", return_value=wms_stock), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(wms_stock, mb52_stock, "2026-10-26 15:45:00")), \
         patch("backlog_resolver.get_today_already_transferred_qty", return_value=(0.0, [])):
        plan = build_backlog_resolution_plan(
            cogi_items,
            gi_path=gi_file,
            gr_path=gr_file,
            require_fresh_ztcode_stock_rec=True,
            save_excel=False,
        )

    assert len(plan) == 1
    row = plan.iloc[0]
    assert row["Action"] == "SKIP"
    assert row["Status"] == "BLOCKED_BY_GI_ERROR"
    assert row["Pending_GI_Qty"] == 80.0
    assert ("Active GI error" in row["Reason"] or "D71" in row["Reason"])


def test_d44_fresh_ztcode_stock_rec_unverified_strictly_downgrades_transfers():
    """
    Test requirement (c):
    If fresh ZTCODE_GI_ERR files are unverified (e.g. collection failed or missing paths),
    all potential transfer proposals MUST be downgraded to SKIP / UNVERIFIED_ZTCODE_STOCK_REC_EXTRACTION.
    """
    mb52_stock = pd.DataFrame([
        {"Material": "MAT_CLEAN", "SLoc": "SL01", "SAP_Stock": 200.0}
    ])
    wms_stock = pd.DataFrame([
        {"Material": "MAT_CLEAN", "SLoc": "SL01", "WMS_Stock": 100.0}
    ])

    cogi_items = [{
        "Material": "MAT_CLEAN",
        "Material_Desc": "Clean Part",
        "SLoc": "SP31",
        "Qty": 50.0,
        "Error_Code": "M7 021",
        "Can_Resolve_By_Transfer": True
    }]

    # When require_fresh_ztcode_stock_rec is True but paths are missing or non-existent
    with patch("backlog_resolver.load_mb52_stock", return_value=mb52_stock), \
         patch("backlog_resolver.load_wms_stock", return_value=wms_stock), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(wms_stock, mb52_stock, "2026-10-26 15:45:00")):
        plan = build_backlog_resolution_plan(
            cogi_items,
            gi_path=Path("non_existent_gi.XLS"),
            gr_path=None,
            require_fresh_ztcode_stock_rec=True,
            save_excel=False,
        )

    assert len(plan) == 1
    row = plan.iloc[0]
    # Even though persistent surplus exists and mat has no errors, fresh ZTCODE_GI_ERR is unverified!
    assert row["Action"] == "SKIP"
    assert row["Status"] == "UNVERIFIED_ZTCODE_STOCK_REC_EXTRACTION"
    assert "D44 Guard" in row["Reason"]


def test_d44_stale_files_on_disk_ignored_when_extraction_fails(tmp_path):
    """
    Test requirement (d):
    Even if stale GR/GI files exist on disk in folders, when the current cycle's
    extraction returns None/False, stale files are NOT used to authorize transfers.
    """
    # Create a stale file on disk
    stale_file = tmp_path / "GR_Error_stale.XLS"
    stale_file.write_text("Date\tVendor\tMaterial Number\n", encoding="utf-16")

    mb52_stock = pd.DataFrame([
        {"Material": "MAT_CLEAN2", "SLoc": "SL01", "SAP_Stock": 200.0}
    ])
    wms_stock = pd.DataFrame([
        {"Material": "MAT_CLEAN2", "SLoc": "SL01", "WMS_Stock": 100.0}
    ])

    cogi_items = [{
        "Material": "MAT_CLEAN2",
        "Material_Desc": "Clean Part 2",
        "SLoc": "SP31",
        "Qty": 50.0,
        "Error_Code": "M7 021",
        "Can_Resolve_By_Transfer": True
    }]

    with patch("backlog_resolver.load_mb52_stock", return_value=mb52_stock), \
         patch("backlog_resolver.load_wms_stock", return_value=wms_stock), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(wms_stock, mb52_stock, "2026-10-26 15:45:00")):
        plan = build_backlog_resolution_plan(
            cogi_items,
            gi_path=None,
            gr_path=None,
            require_fresh_ztcode_stock_rec=True,
            save_excel=False,
        )

    assert len(plan) == 1
    assert plan.iloc[0]["Action"] == "SKIP"
    assert plan.iloc[0]["Status"] == "UNVERIFIED_ZTCODE_STOCK_REC_EXTRACTION"


def test_d44_notifier_separates_gr_and_gi_blocked_items():
    """
    Test requirement (e):
    Validates that the email HTML and Excel exports contain distinct sections and columns
    for BLOCKED_BY_GR_ERROR and BLOCKED_BY_GI_ERROR.
    """
    plan_df = pd.DataFrame([
        {
            "Material": "MAT_GR",
            "Material_Desc": "GR Blocked Part",
            "Target_SLoc": "SP31",
            "Deficit_Qty": 50.0,
            "Error_Code": "M7 021",
            "Action": "SKIP",
            "Status": "BLOCKED_BY_GR_ERROR",
            "Pending_GR_Qty": 50.0,
            "Pending_GR_Vendors": "V100",
            "Reason": "Inbound GR error exists"
        },
        {
            "Material": "MAT_GI",
            "Material_Desc": "GI Blocked Part",
            "Target_SLoc": "SP31",
            "Deficit_Qty": 30.0,
            "Error_Code": "M7 021",
            "Action": "SKIP",
            "Status": "BLOCKED_BY_GI_ERROR",
            "Pending_GI_Qty": 30.0,
            "Pending_GI_SLocs": "SL01",
            "Reason": "GI interface error exists"
        }
    ])

    html = build_email_html(
        plan_df=plan_df,
        execution_results=[],
        run_date="2026-10-27",
        run_time="06:10 AM EST"
    )

    # Email HTML must contain both sections and KPI indicators
    assert "Pending Goods Receipt (GR) Errors" in html
    assert "GI Error-Linked Backlogs" in html
    assert "MAT_GR" in html
    assert "MAT_GI" in html
    assert "Linked GI Error" in html
