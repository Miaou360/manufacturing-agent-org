"""
Unit Tests for D87, D89, and D98 Rules (05_JOB_SL01_R_Bins)
===========================================================
Tests:
1. D87-2: Business days calculation strictly excluding Sunday.
2. D87-3: Re-appearing barcode reset (first_seen = scan_date, days = 0, episode++, history archive).
3. D87-4 & D98-2: SL02 reference icons (🔴 REFILLED_OTHER_MATERIAL, 🟣 DUP_ELSEWHERE, 🟠 SAME_MATERIAL_REMAINS, -)
   and D98-2 sorting (days_in_sl01_r DESC -> qty DESC -> mat_no ASC).
4. D87-5 & D89: Excel formatting (no summary cards, multi-page fitToHeight=0, fitToWidth=1, print_title_rows='4:4', 12 cols).
5. D98-1: Single dwell threshold >= 2 business days regardless of signal (🔴/🟣 >= 1 day exception abolished).
6. D98-3: Strict 🟣 cross-bin pair rule (same part + same qty + same Lot + strictly 1 other SL02 R-bin, persisted across snapshots, no row deletion in SL01_R_BIN_CROSS_PAIR).
"""

import sys
import sqlite3
import pytest
import openpyxl
import pandas as pd
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
JOB_DIR = TEST_DIR.parent
PROJECT_ROOT = JOB_DIR.parent.parent
HQ_DIR = PROJECT_ROOT / "00_HQ_TaskManager"

if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))
if str(JOB_DIR) in sys.path:
    sys.path.remove(str(JOB_DIR))
sys.path.insert(0, str(JOB_DIR))

import db_manager
import sl01_r_bins_operator


@pytest.fixture
def test_db(monkeypatch, tmp_path):
    """Sets up an isolated temporary SQLite DB for testing."""
    test_db_path = tmp_path / "test_d87_sl01_r.db"
    monkeypatch.setattr(db_manager, "DB_PATH", test_db_path)
    db_manager.init_db(test_db_path)
    return test_db_path


def test_d87_2_business_days_calculation():
    """Verifies lag calculation strictly excluding Sunday."""
    assert db_manager.calculate_business_days("2026-10-23", "2026-10-23") == 0
    assert db_manager.calculate_business_days("2026-10-23", "2026-10-24") == 1
    assert db_manager.calculate_business_days("2026-10-24", "2026-10-26") == 1
    assert db_manager.calculate_business_days("2026-10-26", "2026-10-27") == 1
    assert db_manager.calculate_business_days("2026-10-24", "2026-10-27") == 2
    assert db_manager.calculate_business_days("2026-10-23", "2026-10-26") == 2
    assert db_manager.calculate_business_days("2026-10-26", "2026-10-23") == 0


def test_d87_3_reappearing_barcode_reset(test_db):
    """
    Verifies that when a previously CLEARED barcode re-appears:
    - first_seen_date is reset to current scan_date
    - days_in_sl01_r resets to 0
    - episode is incremented to 2
    - previous observation is archived in SL01_R_BIN_OBSERVATION_HISTORY
    """
    barcode = "BC_REAPPEAR_TEST"
    rec_day1 = [{
        "barcode": barcode,
        "mat_no": "99000007",
        "mat_desc": "TEST BRACKET",
        "bin": "R01A 1",
        "gi_loc": "Main parts",
        "qty": 10.0,
        "lot_no": "L1",
    }]

    db_manager.upsert_observations("2026-10-23", rec_day1, db_path=test_db)
    obs1 = {r["barcode"]: r for r in db_manager.get_all_active_observations(db_path=test_db)}
    assert obs1[barcode]["status"] == "ACTIVE"
    assert obs1[barcode]["episode"] == 1
    assert obs1[barcode]["first_seen_date"] == "2026-10-23"
    assert obs1[barcode]["days_in_sl01_r"] == 0

    db_manager.upsert_observations("2026-10-24", [], db_path=test_db)
    active_day2 = db_manager.get_all_active_observations(db_path=test_db)
    assert len(active_day2) == 0

    with sqlite3.connect(test_db) as conn:
        row = conn.execute("SELECT status, cleared_date, episode FROM SL01_R_BIN_OBSERVATION WHERE barcode = ?", (barcode,)).fetchone()
        assert row[0] == "CLEARED"
        assert row[1] == "2026-10-24"
        assert row[2] == 1

    rec_day4 = [{
        "barcode": barcode,
        "mat_no": "99000007",
        "mat_desc": "TEST BRACKET",
        "bin": "R01A 1",
        "gi_loc": "Main parts",
        "qty": 10.0,
        "lot_no": "L1",
    }]
    new_c, upd_c, clr_c = db_manager.upsert_observations("2026-10-26", rec_day4, db_path=test_db)
    assert upd_c == 1

    obs_day4 = {r["barcode"]: r for r in db_manager.get_all_active_observations(db_path=test_db)}
    assert obs_day4[barcode]["status"] == "ACTIVE"
    assert obs_day4[barcode]["episode"] == 2
    assert obs_day4[barcode]["first_seen_date"] == "2026-10-26"
    assert obs_day4[barcode]["days_in_sl01_r"] == 0
    assert obs_day4[barcode]["prev_first_seen_date"] == "2026-10-23"
    assert obs_day4[barcode]["prev_cleared_date"] == "2026-10-24"

    with sqlite3.connect(test_db) as conn:
        hist_rows = conn.execute(
            "SELECT episode, first_seen_date, cleared_date, days_in_sl01_r FROM SL01_R_BIN_OBSERVATION_HISTORY WHERE barcode = ?",
            (barcode,)
        ).fetchall()
        assert len(hist_rows) == 1
        assert hist_rows[0][0] == 1
        assert hist_rows[0][1] == "2026-10-23"
        assert hist_rows[0][2] == "2026-10-24"


def test_d87_4_and_d98_2_signals_and_sorting(test_db):
    """
    Verifies SL02 signal reference evaluation and D98-2 sorting:
    - Icons (🔴, 🟣, 🟠, -) are preserved as reference indicators.
    - Sorting order is strictly: days_in_sl01_r DESC, qty DESC, mat_no ASC.
    """
    records = [
        {
            "barcode": "BC_NORMAL",
            "mat_no": "99000004",
            "mat_desc": "PART A",
            "bin": "R01A 1",
            "gi_loc": "Main parts",
            "qty": 5.0,
            "sl02_signal": "-",
            "sl02_details": "",
        },
        {
            "barcode": "BC_REFILLED",
            "mat_no": "99000005",
            "mat_desc": "PART B",
            "bin": "R01E 2",
            "gi_loc": "Main parts",
            "qty": 10.0,
            "sl02_signal": "🔴 REFILLED_OTHER_MATERIAL",
            "sl02_details": "99000043 (20.0 EA)",
        },
        {
            "barcode": "BC_REMAINS",
            "mat_no": "99000006",
            "mat_desc": "PART C",
            "bin": "R01B 3",
            "gi_loc": "Main parts",
            "qty": 15.0,
            "sl02_signal": "🟠 SAME_MATERIAL_REMAINS",
            "sl02_details": "99000006 (30.0 EA)",
        },
    ]

    db_manager.upsert_observations("2026-10-23", records, db_path=test_db)
    db_manager.upsert_observations("2026-10-26", records, db_path=test_db)

    lagging = db_manager.get_active_lagging_observations(db_path=test_db)
    assert len(lagging) == 3

    # D98-2: All have days=2, so sorted by qty DESC: 15.0 -> 10.0 -> 5.0
    assert lagging[0]["barcode"] == "BC_REMAINS"
    assert "SAME_MATERIAL_REMAINS" in lagging[0]["sl02_signal"]

    assert lagging[1]["barcode"] == "BC_REFILLED"
    assert "REFILLED" in lagging[1]["sl02_signal"]

    assert lagging[2]["barcode"] == "BC_NORMAL"
    assert lagging[2]["sl02_signal"] == "-"


def test_d87_5_excel_formatting(test_db, tmp_path):
    """
    Verifies D87, D89, D98 Excel report requirements:
    1. No summary KPI cards (row 4 is table header, data starts at row 5).
    2. Multi-page print setup: fitToHeight = 0, fitToWidth = 1.
    3. Repeating table header: print_title_rows = '4:4'.
    4. 12 columns formatted in 100% English.
    5. Signal column contains icon only (D89).
    6. Sorted by Days DESC -> Qty DESC (D98-2).
    7. One-line English legend below the table (D89/D98).
    """
    records = [
        {
            "barcode": "BC_TEST_01",
            "mat_no": "99000009",
            "mat_desc": "HOUSING LH",
            "bin": "R01A 2",
            "gi_loc": "Main parts",
            "qty": 24.0,
            "lot_no": "LOT_A",
            "sl02_signal": "🔴 REFILLED_OTHER_MATERIAL",
            "sl02_details": "99000038 (12.0 EA)",
            "sl02_other_bin": "",
        },
        {
            "barcode": "BC_TEST_02",
            "mat_no": "99000035",
            "mat_desc": "BEZEL RH",
            "bin": "R01B 5",
            "gi_loc": "Main parts",
            "qty": 16.0,
            "lot_no": "LOT_B",
            "sl02_signal": "🟠 SAME_MATERIAL_REMAINS",
            "sl02_details": "99000035 (32.0 EA)",
            "sl02_other_bin": "",
        }
    ]

    db_manager.upsert_observations("2026-10-23", records, db_path=test_db)
    db_manager.upsert_observations("2026-10-26", records, db_path=test_db)

    rep_path = sl01_r_bins_operator.generate_portrait_excel_report(
        scan_date_str="2026-10-26",
        output_dir=tmp_path,
        db_path=test_db
    )
    assert rep_path.exists()

    wb = openpyxl.load_workbook(rep_path)
    ws = wb["SL01_R_Bin_Lag"]

    assert ws.page_setup.fitToWidth == 1
    assert ws.page_setup.fitToHeight == 0
    assert ws.print_title_rows in ("4:4", "$4:$4")

    assert "SL01 R-Bin Movement Lag Monitoring Report" in str(ws["A1"].value)
    assert "Scan Date" in str(ws["A2"].value)
    assert ws["A3"].value is None

    headers = [cell.value for cell in ws[4] if cell.value is not None]
    expected_headers = [
        "No.", "Signal", "Bin", "Part", "Description",
        "Qty", "Lot", "First Seen", "Days", "Barcode",
        "SL02 in Bin", "Other Bin"
    ]
    assert headers == expected_headers
    assert len(headers) == 12

    row5_vals = [ws.cell(row=5, column=col).value for col in range(1, 13)]
    assert row5_vals[0] == 1
    assert row5_vals[1] == "🔴"
    assert row5_vals[2] == "R01A 2"
    assert row5_vals[3] == "99000009"
    assert row5_vals[8] == 2
    assert "99000038" in str(row5_vals[10])

    legend_val = ws.cell(row=8, column=1).value
    assert "🔴 bin refilled with other part" in str(legend_val)
    assert "🟣 same part/qty/lot in 1 other R bin (SL02)" in str(legend_val)
    assert "🟠 same part remains in bin" in str(legend_val)


def test_d98_1_single_dwell_threshold_and_d98_2_sorting(test_db):
    """
    Verifies D98-1 Single Dwell Threshold (>= 2 business days for ALL signals, 🔴/🟣 >= 1d abolished)
    and D98-2 sorting (days_in_sl01_r DESC -> qty DESC -> mat_no ASC).
    """
    records_day1 = [
        {"barcode": "BC_RED", "mat_no": "M1", "mat_desc": "D1", "bin": "R01A 1", "gi_loc": "Main parts", "qty": 10.0, "sl02_signal": "🔴 REFILLED_OTHER_MATERIAL"},
        {"barcode": "BC_PURPLE", "mat_no": "M2", "mat_desc": "D2", "bin": "R01E 2", "gi_loc": "Main parts", "qty": 40.0, "sl02_signal": "DUP_ELSEWHERE", "sl02_other_bin": "R01C 1"},
        {"barcode": "BC_AMBER", "mat_no": "M3", "mat_desc": "D3", "bin": "R01B 3", "gi_loc": "Main parts", "qty": 30.0, "sl02_signal": "🟠 SAME_MATERIAL_REMAINS"},
        {"barcode": "BC_NONE", "mat_no": "M4", "mat_desc": "D4", "bin": "R01A 4", "gi_loc": "Main parts", "qty": 20.0, "sl02_signal": "EMPTY_OR_NO_SL02"},
    ]

    # Day 0: 2026-10-23 (Fri) -> lag = 0
    db_manager.upsert_observations("2026-10-23", records_day1, db_path=test_db)
    assert len(db_manager.get_active_lagging_observations(db_path=test_db)) == 0

    # Day 1: 2026-10-24 (Sat) -> lag = 1 business day -> D98-1: NONE shown (all require >= 2 days)
    db_manager.upsert_observations("2026-10-24", records_day1, db_path=test_db)
    assert len(db_manager.get_active_lagging_observations(db_path=test_db)) == 0

    # Day 2: 2026-10-26 (Mon) -> lag = 2 business days -> ALL 4 shown, sorted by qty DESC (40 -> 30 -> 20 -> 10)
    db_manager.upsert_observations("2026-10-26", records_day1, db_path=test_db)
    lag2 = db_manager.get_active_lagging_observations(db_path=test_db)
    assert [r["barcode"] for r in lag2] == ["BC_PURPLE", "BC_AMBER", "BC_NONE", "BC_RED"]


def test_d98_3_strict_lot_single_bin_cross_pair_and_no_delete(test_db, tmp_path):
    """
    Verifies D98-3:
    1. Only pairs with same mat_no, same qty, AND same lot_no in strictly 1 other SL02 R-bin are candidate pairs.
    2. Multiple matching SL02 R-bins (>= 2) or mismatched Lot are NOT recorded as strict pairs.
    3. Existing rows in SL01_R_BIN_CROSS_PAIR are never deleted (is_strict_lot_single flag used).
    4. Persistence across snapshots works and combines with 🔴 into REFILLED_AND_DUP.
    """
    wms_file = tmp_path / "WMS_StockList_HL_20261023_041500.xlsx"
    df = pd.DataFrame([
        # Case A: Strict match (same mat, qty, lot in exactly 1 other SL02 R-bin R01A 5)
        {"Mat.No": "99000040", "Desc": "STRICT PART", "GI Loc": "Main parts", "Qty": 50.0, "BIN": "R01C 2", "Barcode": "BC_STRICT_1", "Lot No": "260821"},
        {"Mat.No": "99000040", "Desc": "STRICT PART", "GI Loc": "CKD", "Qty": 50.0, "BIN": "R01A 5", "Barcode": "BC_SL02_1", "Lot No": "260821"},
        {"Mat.No": "99000043", "Desc": "OTHER PART", "GI Loc": "CKD", "Qty": 10.0, "BIN": "R01C 2", "Barcode": "BC_SL02_REF", "Lot No": "260101"},
        # Case B: Same mat & qty, but DIFFERENT Lot in SL02 -> excluded
        {"Mat.No": "99000021-V1", "Desc": "DIFF LOT PART", "GI Loc": "Main parts", "Qty": 144.0, "BIN": "R01D 3", "Barcode": "BC_DIFF_LOT", "Lot No": "260821"},
        {"Mat.No": "99000021-V1", "Desc": "DIFF LOT PART", "GI Loc": "CKD", "Qty": 144.0, "BIN": "R01D 2", "Barcode": "BC_SL02_DIFF", "Lot No": "260820"},
        # Case C: Same mat, qty, AND lot, but in 2 other SL02 R-bins -> excluded (not strictly 1)
        {"Mat.No": "99000035", "Desc": "TWO BINS PART", "GI Loc": "Main parts", "Qty": 20.0, "BIN": "R01B 5", "Barcode": "BC_TWO_BINS", "Lot No": "260718"},
        {"Mat.No": "99000035", "Desc": "TWO BINS PART", "GI Loc": "CKD", "Qty": 20.0, "BIN": "R01C 5", "Barcode": "BC_SL02_2A", "Lot No": "260718"},
        {"Mat.No": "99000035", "Desc": "TWO BINS PART", "GI Loc": "CKD", "Qty": 20.0, "BIN": "R01C 3", "Barcode": "BC_SL02_2B", "Lot No": "260718"},
    ])
    df.to_excel(wms_file, index=False)

    recs = sl01_r_bins_operator.extract_sl01_r_records_from_wms(wms_file)
    by_bc = {r["barcode"]: r for r in recs}

    assert by_bc["BC_STRICT_1"]["candidate_cross_bins"] == ["R01A 5"]
    assert by_bc["BC_DIFF_LOT"]["candidate_cross_bins"] == []
    assert by_bc["BC_TWO_BINS"]["candidate_cross_bins"] == []

    # Pre-insert a legacy non-strict row into SL01_R_BIN_CROSS_PAIR on 2026-10-23 to verify NO deletion
    with sqlite3.connect(test_db) as conn:
        conn.execute("""
            INSERT INTO SL01_R_BIN_CROSS_PAIR (scan_date, barcode, mat_no, qty, sl01_bin, sl02_bin, is_strict_lot_single)
            VALUES ('2026-10-23', 'BC_DIFF_LOT', '99000021-V1', 144.0, 'R01D 3', 'R01D 2', 0)
        """)
        conn.commit()

    # Day 0: 2026-10-23 (Fri)
    db_manager.upsert_observations("2026-10-23", recs, db_path=test_db)
    with sqlite3.connect(test_db) as conn:
        pair_rows = conn.execute("SELECT barcode, lot_no, sl02_bin, is_strict_lot_single FROM SL01_R_BIN_CROSS_PAIR ORDER BY pair_id").fetchall()
        # Legacy row preserved (is_strict_lot_single=0), plus 1 new strict row for BC_STRICT_1
        assert len(pair_rows) == 2
        assert pair_rows[0] == ("BC_DIFF_LOT", "", "R01D 2", 0)
        assert pair_rows[1] == ("BC_STRICT_1", "260821", "R01A 5", 1)

    # Day 2: 2026-10-26 (Mon, 2 business days) -> BC_STRICT_1 persists and becomes REFILLED_AND_DUP; BC_DIFF_LOT does NOT become DUP
    db_manager.upsert_observations("2026-10-26", recs, db_path=test_db)
    lag_items = {r["barcode"]: r for r in db_manager.get_active_lagging_observations(db_path=test_db)}
    assert len(lag_items) == 3
    assert lag_items["BC_STRICT_1"]["sl02_signal"] == "REFILLED_AND_DUP"
    assert lag_items["BC_STRICT_1"]["sl02_other_bin"] == "R01A 5"
    assert lag_items["BC_DIFF_LOT"]["sl02_signal"] == "EMPTY_OR_NO_SL02"
    assert lag_items["BC_DIFF_LOT"]["sl02_other_bin"] == ""
