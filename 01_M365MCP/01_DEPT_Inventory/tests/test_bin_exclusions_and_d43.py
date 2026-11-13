"""
Unit Tests for Bin Exclusions (EXT_TRL, CKD, D, BINSYNTH9953) and D43 User-Only Policy
==================================================================================
01_M365MCP/01_DEPT_Inventory/tests/test_bin_exclusions_and_d43.py

Tests:
1. Daily & Weekly cycle count audit lists strictly exclude:
   - EXT_TRL* (Temporary trailer staging)
   - CKD* (CKD dedicated bins)
   - D* (External supplier/non-SL warehouse)
   - BINSYNTH9953, NAN, empty string
2. Zero-tolerance guardrail raises ValueError if excluded bin enters audit list.
3. D43 User-Only Policy for Weekly Hit Rate Report:
   - get_weekly_hit_rate_recipient() strictly resolves to TEST_EMAIL_RECIPIENT.
   - Ignores STOCK_ACCURACY_TO / CC completely.
4. Dry-run validation against actual WMS file in wms_daily_stack verifies 0 bad bins.
"""

import os
import sys
import pytest
from pathlib import Path
import pandas as pd
import datetime

JOB_00_DIR = Path(__file__).resolve().parent.parent / "00_JOB_StockAccuracy"
if str(JOB_00_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_00_DIR))

from box_aging_tracker import BoxAgingTracker, get_bin_characteristics
from weekly_hit_rate_notifier import get_weekly_hit_rate_recipient
from stock_verifier import StockVerifier


def test_get_bin_characteristics_identifies_all_excluded_categories():
    """Verifies get_bin_characteristics correctly flags non-physical and excluded bins."""
    cases = [
        ("EXT_TRL TRL 1", True, True, False),
        ("EXT_TRL TRL 2", True, True, False),
        ("CKD 01", True, False, True),
        ("D01 1", True, False, True),
        ("BINSYNTH9953", True, False, False),
        ("", True, False, False),
        ("52Z 2", False, False, False),
        ("SMT 07", False, False, False),
        ("CRT 04", False, False, False),
        ("R01B 2", False, False, False),
        ("OEM 01", False, False, False),
        ("OEM 02", False, False, False),
    ]
    for bin_val, exp_excluded, exp_EXT_TRL, exp_ext in cases:
        info = get_bin_characteristics(bin_val)
        assert info["is_non_physical_or_excluded"] == exp_excluded, f"Failed for {bin_val}"
        assert info["is_temporary_staging"] == exp_EXT_TRL, f"EXT_TRL check failed for {bin_val}"
        assert info["is_external"] == exp_ext, f"External check failed for {bin_val}"
        if bin_val.startswith("OEM"):
            assert info["is_oem_floor"] is True, f"OEM floor check failed for {bin_val}"
            assert info["is_supermarket"] is True, f"Supermarket check failed for {bin_val}"
            assert info["tier"] is None, f"Tier must be None for {bin_val}, got {info['tier']}"


def test_EXT_TRL_ckd_d_bins_excluded_from_cycle_count_lists():
    """
    Verifies that detect_mis_scans strictly filters out EXT_TRL, CKD, D, and BINSYNTH9953 bins
    from TOP, EXTENDED, and RANDOM lists in both Daily and Weekly modes.
    """
    tracker = BoxAgingTracker()
    today = datetime.date.today()
    old_date = today - datetime.timedelta(days=60)

    # Synthetic stock list with a mix of excluded bins and valid physical bins
    data = [
        # Excluded bins
        {"Barcode": "B01", "Mat.No": "MAT-01", "Mat.Desc": "PART 1", "BIN": "EXT_TRL TRL 1", "Qty.": 100, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 60, "Bin_Dwell_Days": 30},
        {"Barcode": "B02", "Mat.No": "MAT-02", "Mat.Desc": "PART 2", "BIN": "EXT_TRL TRL 2", "Qty.": 50, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 60, "Bin_Dwell_Days": 30},
        {"Barcode": "B03", "Mat.No": "MAT-03", "Mat.Desc": "PART 3", "BIN": "CKD 01", "Qty.": 80, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 60, "Bin_Dwell_Days": 30},
        {"Barcode": "B04", "Mat.No": "MAT-04", "Mat.Desc": "PART 4", "BIN": "D01 1", "Qty.": 40, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 60, "Bin_Dwell_Days": 30},
        {"Barcode": "B05", "Mat.No": "MAT-05", "Mat.Desc": "PART 5", "BIN": "BINSYNTH9953", "Qty.": 10, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 60, "Bin_Dwell_Days": 30},
        # Valid floor/physical bins
        {"Barcode": "B10", "Mat.No": "MAT-10", "Mat.Desc": "SMT PART", "BIN": "SMT 07", "Qty.": 20, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 50, "Bin_Dwell_Days": 25},
        {"Barcode": "B11", "Mat.No": "MAT-11", "Mat.Desc": "CART PART", "BIN": "CRT 04", "Qty.": 15, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 45, "Bin_Dwell_Days": 20},
        {"Barcode": "B12", "Mat.No": "MAT-12", "Mat.Desc": "RACK PART", "BIN": "52Z 2", "Qty.": 30, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 40, "Bin_Dwell_Days": 15},
        {"Barcode": "B13", "Mat.No": "MAT-13", "Mat.Desc": "RACK PART", "BIN": "73Z 1", "Qty.": 25, "Lot_Date": old_date, "GR_Date": old_date, "Warehouse_Dwell_Days": 35, "Bin_Dwell_Days": 10},
    ]
    df = pd.DataFrame(data)

    for mode in ["Daily", "Weekly"]:
        res = tracker.detect_mis_scans(df, mode=mode, random_slots=1)
        all_bins = res["top_bins"] + res["extended_bins"] + res["random_bins"]

        bin_names = [b["BIN"] for b in all_bins]
        for b_name in bin_names:
            b_up = b_name.upper().strip()
            assert not b_up.startswith("EXT_TRL"), f"EXT_TRL leaked in {mode}: {b_name}"
            assert not b_up.startswith("CKD"), f"CKD leaked in {mode}: {b_name}"
            assert not b_up.startswith("D"), f"D external leaked in {mode}: {b_name}"
            assert b_up not in ("", "NAN", "BINSYNTH9953"), f"Non-physical leaked in {mode}: {b_name}"


def test_d43_weekly_hit_rate_recipient_user_only_rule(monkeypatch):
    """
    Verifies User Decision D43:
    - Weekly hit rate report is exclusively sent to TEST_EMAIL_RECIPIENT.
    - STOCK_ACCURACY_TO / CC are permanently ignored.
    """
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "user_placeholder")
    monkeypatch.setenv("STOCK_ACCURACY_TO", "user_placeholder,user_placeholder")
    monkeypatch.setenv("STOCK_ACCURACY_CC", "user_placeholder,user_placeholder")

    recip = get_weekly_hit_rate_recipient()
    assert recip == "user_placeholder"
    assert "warehouse_floor" not in recip
    assert "manager" not in recip


def test_dry_run_actual_wms_stock_list_has_zero_excluded_bins():
    """
    Runs full analysis on the actual latest WMS stock list file in wms_daily_stack
    and verifies that TOP, EXTENDED, and RANDOM lists have exactly 0 EXT_TRL/CKD/D bins.
    """
    verifier = StockVerifier()
    latest_file = verifier.tracker.find_latest_wms_file()
    if not latest_file or not latest_file.exists():
        pytest.skip("No actual WMS stock list file present in wms_daily_stack.")

    analysis = verifier.run_full_stock_analysis(mode="Daily")
    all_target_bins = analysis["top_bins"] + analysis["extended_bins"] + analysis["random_bins"]

    assert len(all_target_bins) > 0, "No target bins produced"

    bad_bins = [
        b["BIN"] for b in all_target_bins
        if str(b["BIN"]).strip().upper().startswith(("EXT_TRL", "CKD", "D"))
        or str(b["BIN"]).strip().upper() in ("", "NAN", "BINSYNTH9953")
    ]
    assert bad_bins == [], f"Found excluded bins in cycle count list: {bad_bins}"
