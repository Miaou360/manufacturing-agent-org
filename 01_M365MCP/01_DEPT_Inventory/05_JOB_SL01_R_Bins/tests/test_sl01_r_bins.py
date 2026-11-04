"""
Unit Tests for 05_JOB_SL01_R_Bins
=================================
Tests observation tracking, SL01 R-bin filtering, and lag calculation.
"""

import os
import sys
import sqlite3
import tempfile
import pytest
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

TEST_DIR = Path(__file__).resolve().parent
JOB_DIR = TEST_DIR.parent
PROJECT_ROOT = JOB_DIR.parent.parent
HQ_DIR = PROJECT_ROOT / "00_HQ_TaskManager"

if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))
if str(JOB_DIR) in sys.path:
    sys.path.remove(str(JOB_DIR))
sys.path.insert(0, str(JOB_DIR))

from wms_mapping import map_wms_gi_loc_to_sap
import db_manager
import sl01_r_bins_operator


@pytest.fixture
def test_db(monkeypatch, tmp_path):
    """Sets up a temporary SQLite DB for isolated testing."""
    test_db_path = tmp_path / "test_sl01_r.db"
    monkeypatch.setattr(db_manager, "DB_PATH", test_db_path)
    db_manager.init_db()
    return test_db_path


def test_wms_mapping_for_sl01_r():
    """Verifies that only 'Main parts' maps to SL01, others map to different SLocs."""
    assert map_wms_gi_loc_to_sap("Main parts") == "SL01"
    assert map_wms_gi_loc_to_sap("MAIN PARTS") == "SL01"
    assert map_wms_gi_loc_to_sap("CKD") == "SL02"
    assert map_wms_gi_loc_to_sap("Receipt goods SL") == "SL00"
    assert map_wms_gi_loc_to_sap("Raw material") == "SL03"


def test_upsert_and_lag_calculation(test_db):
    """Tests new item creation and lag calculation across dates."""
    records_day1 = [
        {
            "barcode": "BARCODE_001",
            "mat_no": "99000026",
            "mat_desc": "HEADLAMP HOUSING",
            "bin": "R01B 1",
            "gi_loc": "Main parts",
            "qty": 12.0,
            "lot_no": "LOT123",
        },
        {
            "barcode": "BARCODE_002",
            "mat_no": "99000035",
            "mat_desc": "BEZEL LH",
            "bin": "R01B 5",
            "gi_loc": "Main parts",
            "qty": 20.0,
            "lot_no": "LOT456",
        }
    ]

    # Day 1: 2026-10-23
    new_c, upd_c, clr_c = db_manager.upsert_observations("2026-10-23", records_day1, is_backfill=False)
    assert new_c == 2
    assert upd_c == 0

    active_day1 = db_manager.get_all_active_observations()
    assert len(active_day1) == 2
    assert all(r["days_in_sl01_r"] == 0 for r in active_day1)

    # Day 4: 2026-10-26 (BARCODE_001 still in R-bin, BARCODE_002 moved out, BARCODE_003 new)
    records_day4 = [
        {
            "barcode": "BARCODE_001",
            "mat_no": "99000026",
            "mat_desc": "HEADLAMP HOUSING",
            "bin": "R01B 1",
            "gi_loc": "Main parts",
            "qty": 12.0,
            "lot_no": "LOT123",
        },
        {
            "barcode": "BARCODE_003",
            "mat_no": "99000041",
            "mat_desc": "BRACKET RH",
            "bin": "R01C 1",
            "gi_loc": "Main parts",
            "qty": 50.0,
            "lot_no": "LOT789",
        }
    ]

    new_c, upd_c, clr_c = db_manager.upsert_observations("2026-10-26", records_day4, is_backfill=False)
    assert new_c == 1
    assert upd_c == 1
    assert clr_c == 1  # BARCODE_002 cleared

    # Check lag for BARCODE_001: 2026-10-23 (Fri) -> 2026-10-26 (Mon) = 2 business days (Sat+Mon, Sun excluded)
    lagging = db_manager.get_active_lagging_observations(min_days=1)
    assert len(lagging) == 1
    assert lagging[0]["barcode"] == "BARCODE_001"
    assert lagging[0]["days_in_sl01_r"] == 2
    assert lagging[0]["first_seen_date"] == "2026-10-23"

    # Check that BARCODE_003 has 0 days lag
    all_act = {r["barcode"]: r for r in db_manager.get_all_active_observations()}
    assert all_act["BARCODE_003"]["days_in_sl01_r"] == 0
    assert "BARCODE_002" not in all_act  # Cleared


def test_report_generation(test_db, tmp_path, monkeypatch):
    """Tests Excel report generation."""
    monkeypatch.setattr(sl01_r_bins_operator, "REPORTS_DIR", tmp_path)

    # Insert test lagging observation
    records = [
        {
            "barcode": "TEST_BARCODE_LAG",
            "mat_no": "99000026",
            "mat_desc": "TEST ITEM",
            "bin": "R01B 1",
            "gi_loc": "Main parts",
            "qty": 12.0,
        }
    ]
    db_manager.upsert_observations("2026-10-23", records)
    # Simulate scan 3 days later
    db_manager.upsert_observations("2026-10-26", records)

    rep_path = sl01_r_bins_operator.generate_portrait_excel_report("2026-10-26")
    assert rep_path.exists()
    assert rep_path.stat().st_size > 5000

    # Verify canonical copy
    canonical = tmp_path / "SL01_R_Bins_Lag_Report_latest.xlsx"
    assert canonical.exists()


def test_fallback_mode_freezes_observations_and_renders_warning(test_db, tmp_path, monkeypatch):
    """
    Verifies that:
    1. In fallback mode, upsert_observations is NOT called, freezing DB observations.
    2. Excel report includes the amber warning banner in Row 2.
    3. Email subject and body contain the WMS fallback warning.
    4. When fallback > 24h (expired), alert-only email is constructed without list attachment.
    """
    monkeypatch.setattr(sl01_r_bins_operator, "REPORTS_DIR", tmp_path)

    # 1. Establish baseline observation
    initial_records = [
        {
            "barcode": "BARCODE_FROZEN",
            "mat_no": "99000026",
            "mat_desc": "TEST ITEM",
            "bin": "R01B 1",
            "gi_loc": "Main parts",
            "qty": 10.0,
        }
    ]
    db_manager.upsert_observations("2026-10-23", initial_records)
    obs_before = db_manager.get_all_active_observations()
    assert len(obs_before) == 1
    assert obs_before[0]["barcode"] == "BARCODE_FROZEN"

    # Create dummy WMS excel with a new barcode
    dummy_wms = tmp_path / "WMS_StockList_HL_20261025_041500.xlsx"
    df = pd.DataFrame([{
        "Mat.No": "99000043",
        "Desc": "NEW CANDIDATE",
        "GI Loc": "Main parts",
        "Qty": 5.0,
        "BIN": "R01A 1",
        "Barcode": "BARCODE_NEW_UNSEEN",
        "Lot No": "LOT001",
        "Vendor Code": "V01",
        "Vendor Name": "VENDOR"
    }])
    df.to_excel(dummy_wms, index=False)

    # 2. Run daily scan with is_fallback=True
    tot, stale, qty, s_date = sl01_r_bins_operator.run_daily_scan(
        snapshot_file=dummy_wms,
        is_fallback=True
    )
    obs_after = db_manager.get_all_active_observations()

    # DB MUST BE FROZEN: BARCODE_NEW_UNSEEN was NOT added!
    assert len(obs_after) == 1
    assert obs_after[0]["barcode"] == "BARCODE_FROZEN"
    assert not any(r["barcode"] == "BARCODE_NEW_UNSEEN" for r in obs_after)

    # 3. Generate portrait report in fallback mode
    rep_path = sl01_r_bins_operator.generate_portrait_excel_report(
        scan_date_str="2026-10-26",
        is_fallback=True,
        fallback_file_time="2026-10-25 04:15:00"
    )
    import openpyxl
    wb = openpyxl.load_workbook(rep_path)
    ws = wb["SL01_R_Bin_Lag"]
    row2_val = str(ws["A2"].value)
    assert "⚠️ [FALLBACK] WMS Live Download Failed" in row2_val
    assert "2026-10-25 04:15:00" in row2_val

    # 4. Check email dispatch in fallback mode
    dispatched_messages = []
    def mock_dispatch(to_email, cc_email, subject, html_body, attachment=None):
        dispatched_messages.append({
            "to": to_email,
            "subject": subject,
            "body": html_body,
            "attachment": attachment
        })
        return True

    monkeypatch.setattr(sl01_r_bins_operator, "_dispatch_email", mock_dispatch)

    # Test fallback within 24h
    sl01_r_bins_operator.send_outlook_notification(
        report_file=rep_path,
        scan_date_str="2026-10-26",
        test_mode=True,
        override_to=("tester" + "@" + "example.local"),
        is_fallback=True,
        fallback_file_time="2026-10-25 04:15:00",
        fallback_expired=False
    )
    assert len(dispatched_messages) == 1
    assert "⚠️ [WMS Fallback]" in dispatched_messages[0]["subject"]
    assert "WMS Live Download Failed" in dispatched_messages[0]["body"]
    assert "frozen" in dispatched_messages[0]["body"]
    assert dispatched_messages[0]["attachment"] == rep_path

    # Test fallback expired (> 24h)
    sl01_r_bins_operator.send_outlook_notification(
        report_file=None,
        scan_date_str="2026-10-26",
        test_mode=True,
        override_to=("tester" + "@" + "example.local"),
        is_fallback=True,
        fallback_file_time="2026-10-24 04:15:00",
        fallback_expired=True
    )
    assert len(dispatched_messages) == 2
    assert "[WMS Download Failed]" in dispatched_messages[1]["subject"]
    assert "older than 24 hours" in dispatched_messages[1]["body"]
    assert "suppressed" in dispatched_messages[1]["body"]
    assert dispatched_messages[1]["attachment"] is None


def test_sharp_drop_renders_warning_banner_without_blocking(test_db, tmp_path, monkeypatch):
    """
    Thread #175 Item 5: When active SL01 R-bin count drops sharply (e.g. 20 -> 0, -100.0%),
    DB CLEARED updates and email/Excel dispatch MUST proceed normally (non-blocking),
    while rendering a prominent top warning banner in both Excel Row 3 and Email HTML.
    """
    monkeypatch.setattr(sl01_r_bins_operator, "REPORTS_DIR", tmp_path)

    day1_records = [
        {
            "barcode": f"BAR_DROP_{idx:03d}",
            "mat_no": f"92100{idx:03d}",
            "mat_desc": f"COMPONENT {idx}",
            "bin": f"R02A {idx}",
            "gi_loc": "Main parts",
            "qty": 10.0,
            "lot_no": f"L{idx:03d}",
        }
        for idx in range(1, 21)
    ]
    db_manager.upsert_observations("2026-10-28", day1_records, is_backfill=False, db_path=test_db)
    db_manager.log_scan_run(
        run_id="20261028_041500",
        scan_date="2026-10-28",
        source_file="WMS_StockList_HL_20261028_041500.xlsx",
        total_active=20,
        stale_count=0,
        stale_qty=0.0,
        email_status="SENT",
        db_path=test_db,
    )

    new_c, upd_c, clr_c = db_manager.upsert_observations("2026-10-29", [], is_backfill=False, db_path=test_db)
    assert clr_c == 20
    assert len(db_manager.get_all_active_observations(db_path=test_db)) == 0

    delta = db_manager.get_active_count_delta("2026-10-29", db_path=test_db)
    assert delta["prev_active_count"] == 20
    assert delta["curr_active_count"] == 0
    assert delta["cleared_today_count"] == 20
    assert delta["drop_pct"] == 100.0
    assert delta["is_sharp_drop"] is True

    rep_path = sl01_r_bins_operator.generate_portrait_excel_report("2026-10-29", db_path=test_db)
    import openpyxl
    wb = openpyxl.load_workbook(rep_path)
    ws = wb["SL01_R_Bin_Lag"]
    row3_val = str(ws["A3"].value or "")
    assert "⚠️ Sharp Drop Notice" in row3_val
    assert "-100.0%" in row3_val
    assert "20 → 0 pallets" in row3_val

    dispatched = []
    monkeypatch.setattr(
        sl01_r_bins_operator,
        "_dispatch_email",
        lambda to_e, cc_e, subj, body, attachment=None: dispatched.append({"subject": subj, "body": body}) or True,
    )
    ok = sl01_r_bins_operator.send_outlook_notification(
        report_file=rep_path,
        scan_date_str="2026-10-29",
        test_mode=True,
        override_to=("tester" + "@" + "example.local"),
        db_path=test_db,
    )
    assert ok is True
    assert len(dispatched) == 1
    assert "Active SL01 R-Bin Count Dropped -100.0%" in dispatched[0]["body"]
    assert "20 &rarr; 0 pallets" in dispatched[0]["body"]


def test_normal_drop_does_not_render_sharp_drop_banner(test_db, tmp_path, monkeypatch):
    """Routine clearance (< 50% drop, e.g. 10 -> 8 = -20.0%) does not emit sharp-drop warning."""
    monkeypatch.setattr(sl01_r_bins_operator, "REPORTS_DIR", tmp_path)
    day1 = [
        {
            "barcode": f"BAR_NORM_{idx:02d}",
            "mat_no": f"92101{idx:03d}",
            "mat_desc": "PART",
            "bin": f"R03B {idx}",
            "gi_loc": "Main parts",
            "qty": 5.0,
        }
        for idx in range(1, 11)
    ]
    db_manager.upsert_observations("2026-10-28", day1, is_backfill=False, db_path=test_db)
    db_manager.upsert_observations("2026-10-29", day1[:8], is_backfill=False, db_path=test_db)

    delta = db_manager.get_active_count_delta("2026-10-29", db_path=test_db)
    assert delta["drop_pct"] == 20.0
    assert delta["is_sharp_drop"] is False

    rep_path = sl01_r_bins_operator.generate_portrait_excel_report("2026-10-29", db_path=test_db)
    import openpyxl
    wb = openpyxl.load_workbook(rep_path)
    ws = wb["SL01_R_Bin_Lag"]
    assert not ws["A3"].value

    dispatched = []
    monkeypatch.setattr(
        sl01_r_bins_operator,
        "_dispatch_email",
        lambda to_e, cc_e, subj, body, attachment=None: dispatched.append({"subject": subj, "body": body}) or True,
    )
    ok = sl01_r_bins_operator.send_outlook_notification(
        report_file=rep_path,
        scan_date_str="2026-10-29",
        test_mode=True,
        override_to=("tester" + "@" + "example.local"),
        db_path=test_db,
    )
    assert ok is True
    assert "Active SL01 R-Bin Count Dropped" not in dispatched[0]["body"]


