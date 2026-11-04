"""
Unit Tests for User Decision D32: Remaining SAP Deficits Tracking & Reporting
=============================================================================
Tests:
(a) Database tracking: records deficits, retrieves them, calculates consecutive days with Sunday preservation
(b) Deficit extraction: identifies WMS > SAP deficits post-transfer across warehouse locations (SL00..SL04, SL49)
(c) Excel generation: produces multi-tab workbook with Unresolved_COGI and Remaining_SAP_Deficits
(d) Email generation: renders executive HTML section with SL49 external badge
"""

import sys
import pytest
from pathlib import Path
import pandas as pd

JOB_02_DIR = Path(__file__).resolve().parent.parent / "02_JOB_Backlog_Clear"
if str(JOB_02_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_02_DIR))

from db_manager import (
    init_db,
    record_unresolved_sap_deficits,
    get_unresolved_sap_deficits
)
from backlog_resolver import extract_remaining_sap_deficits
from notifier import generate_unresolved_excel, build_email_html


def test_d32_db_recording_and_consecutive_days(tmp_path):
    """
    (a) Tests DB recording and consecutive days logic:
    - Day 1 (Friday 2026-10-23): consecutive = 1
    - Day 2 (Saturday 2026-10-24): consecutive = 2
    - Day 3 (Monday 2026-10-26): consecutive = 3 (Sunday gap does not break streak)
    - Day 4 (Thursday 2026-10-29): consecutive = 1 (broken streak resets to 1)
    """
    db_file = tmp_path / "test_backlog.db"
    init_db(db_file)

    item_sl01 = {
        "Material": "99000021-V1",
        "Material_Desc": "CONNECTOR",
        "Storage_Loc": "SL01",
        "SAP_Stock": 10.0,
        "WMS_Stock": 153.0,
        "Deficit_Qty": 143.0,
        "Accuracy_Before": 0.0654,
        "Accuracy_After": 0.0654,
        "Recent_Doc": "ZTCODE_GI_ERR",
        "is_sl49": 0,
        "is_sustained_deficit": 1
    }
    item_sl49 = {
        "Material": "99000036-V2",
        "Material_Desc": "BRACKET",
        "Storage_Loc": "SL49",
        "SAP_Stock": 0.0,
        "WMS_Stock": 50.0,
        "Deficit_Qty": 50.0,
        "Accuracy_Before": 0.0,
        "Accuracy_After": 0.0,
        "Recent_Doc": "",
        "is_sl49": 1,
        "is_sustained_deficit": 0
    }

    # Day 1: 2026-10-23 (Friday)
    record_unresolved_sap_deficits("2026-10-23", [item_sl01, item_sl49], db_path=db_file)
    res_d1 = get_unresolved_sap_deficits("2026-10-23", db_path=db_file)
    assert len(res_d1) == 2
    r_sl01 = next(r for r in res_d1 if r["Storage_Loc"] == "SL01")
    r_sl49 = next(r for r in res_d1 if r["Storage_Loc"] == "SL49")
    assert r_sl01["consecutive_days"] == 1
    assert r_sl01["first_detected_date"] == "2026-10-23"
    assert r_sl01["is_sustained_deficit"] == 1
    assert r_sl49["is_sl49"] == 1
    assert r_sl49["is_sustained_deficit"] == 0

    # Day 2: 2026-10-24 (Saturday)
    record_unresolved_sap_deficits("2026-10-24", [item_sl01], db_path=db_file)
    res_d2 = get_unresolved_sap_deficits("2026-10-24", db_path=db_file)
    assert len(res_d2) == 1
    assert res_d2[0]["consecutive_days"] == 2
    assert res_d2[0]["first_detected_date"] == "2026-10-23"

    # Day 3: 2026-10-26 (Monday - Sunday was 10-25)
    record_unresolved_sap_deficits("2026-10-26", [item_sl01], db_path=db_file)
    res_d3 = get_unresolved_sap_deficits("2026-10-26", db_path=db_file)
    assert len(res_d3) == 1
    assert res_d3[0]["consecutive_days"] == 3
    assert res_d3[0]["first_detected_date"] == "2026-10-23"

    # Day 4: 2026-10-29 (Thursday - missing Tuesday and Wednesday)
    record_unresolved_sap_deficits("2026-10-29", [item_sl01], db_path=db_file)
    res_d4 = get_unresolved_sap_deficits("2026-10-29", db_path=db_file)
    assert len(res_d4) == 1
    assert res_d4[0]["consecutive_days"] == 1  # Reset to 1!
    assert res_d4[0]["first_detected_date"] == "2026-10-29"


def test_d32_extract_remaining_sap_deficits():
    """
    (b) Tests extraction of remaining SAP deficits from stock matrix and plan_df:
    - Item 1: WMS 100, SAP 20 -> Deficit 80. Approved transfer of 30 into SL01 -> Post Deficit = 50.
    - Item 2: WMS 50, SAP 50 -> Deficit 0 (Excluded).
    - Item 3: WMS 40, SAP 10 in SL49 -> Deficit 30, is_sl49 = 1.
    """
    stock_matrix = pd.DataFrame([
        {"Material": "MAT-01", "SLoc": "SL01", "SAP_Stock": 20.0, "WMS_Stock": 100.0, "Surplus": -80.0},
        {"Material": "MAT-02", "SLoc": "SL01", "SAP_Stock": 50.0, "WMS_Stock": 50.0, "Surplus": 0.0},
        {"Material": "MAT-03", "SLoc": "SL49", "SAP_Stock": 10.0, "WMS_Stock": 40.0, "Surplus": -30.0},
        {"Material": "MAT-04", "SLoc": "SP31", "SAP_Stock": 10.0, "WMS_Stock": 90.0, "Surplus": -80.0},  # Excluded: line, not warehouse
    ])

    plan_df = pd.DataFrame([
        {
            "Material": "MAT-01",
            "Material_Desc": "Part 1",
            "Action": "TRANSFER_POSTING",
            "From_SLoc": "SP31",
            "Target_SLoc": "SL01",
            "Transfer_Qty": 30.0,
            "Error_Code": "M3 897"
        }
    ])
    plan_df.attrs["stock_matrix"] = stock_matrix

    deficits = extract_remaining_sap_deficits(plan_df=plan_df, run_date="2026-10-26")

    assert len(deficits) == 2  # MAT-01 and MAT-03 (MAT-02 zero deficit, MAT-04 SP31 excluded)

    d_mat1 = next(d for d in deficits if d["Material"] == "MAT-01")
    assert d_mat1["Deficit_Qty"] == 50.0  # 100 - (20 + 30) = 50.0
    assert d_mat1["Accuracy_Before"] == 0.2
    assert d_mat1["Accuracy_After"] == 0.5
    assert d_mat1["is_sl49"] == 0

    d_mat3 = next(d for d in deficits if d["Material"] == "MAT-03")
    assert d_mat3["Deficit_Qty"] == 30.0
    assert d_mat3["is_sl49"] == 1


def test_d32_deficit_persistence_with_previous_snapshot():
    """
    (b2) Tests deficit persistence check against previous snapshot:
    - MAT-01 had WMS 90, SAP 10 in previous snapshot -> Sustained (is_sustained_deficit=1)
    - MAT-03 had WMS 10, SAP 20 in previous snapshot (no deficit) -> Transient/Pending (is_sustained_deficit=0)
    """
    stock_matrix = pd.DataFrame([
        {"Material": "MAT-01", "SLoc": "SL01", "SAP_Stock": 20.0, "WMS_Stock": 100.0, "Surplus": -80.0},
        {"Material": "MAT-03", "SLoc": "SL49", "SAP_Stock": 10.0, "WMS_Stock": 40.0, "Surplus": -30.0},
    ])
    plan_df = pd.DataFrame()
    plan_df.attrs["stock_matrix"] = stock_matrix

    # Previous snapshot
    prev_wms = pd.DataFrame([
        {"Material": "MAT-01", "SLoc": "SL01", "WMS_Stock": 90.0},
        {"Material": "MAT-03", "SLoc": "SL49", "WMS_Stock": 10.0},
    ])
    prev_mb52 = pd.DataFrame([
        {"Material": "MAT-01", "SLoc": "SL01", "SAP_Stock": 10.0},
        {"Material": "MAT-03", "SLoc": "SL49", "SAP_Stock": 20.0},
    ])
    plan_df.attrs["prev_wms_df"] = prev_wms
    plan_df.attrs["prev_mb52_df"] = prev_mb52

    deficits = extract_remaining_sap_deficits(plan_df=plan_df, run_date="2026-10-26")
    d_mat1 = next(d for d in deficits if d["Material"] == "MAT-01")
    d_mat3 = next(d for d in deficits if d["Material"] == "MAT-03")

    assert d_mat1["is_sustained_deficit"] == 1
    assert d_mat3["is_sustained_deficit"] == 0


def test_d32_generate_unresolved_excel_multi_tab(tmp_path):
    """
    (c) Tests multi-tab Excel export:
    - Tab 1: Unresolved_COGI
    - Tab 2: Remaining_SAP_Deficits
    """
    plan_df = pd.DataFrame([
        {
            "Material": "MAT-01",
            "Material_Desc": "Part 1",
            "Action": "SKIP",
            "Status": "ACTION_REQUIRED",
            "Target_SLoc": "SP31",
            "Deficit_Qty": 15.0,
            "Error_Code": "Deficit Error",
            "Pending_GR_Qty": 0,
            "Pending_GR_Vendors": "-",
            "Reason": "Unresolved backlog"
        }
    ])

    remaining_deficits = [
        {
            "Material": "MAT-02",
            "Material_Desc": "Part 2",
            "Storage_Loc": "SL01",
            "SAP_Stock": 10.0,
            "WMS_Stock": 60.0,
            "Deficit_Qty": 50.0,
            "Accuracy_Before": 0.1667,
            "Accuracy_After": 0.1667,
            "Recent_Doc": "ZTCODE_STOCK_REC",
            "first_detected_date": "2026-10-26",
            "consecutive_days": 2,
            "is_sl49": 0,
            "is_sustained_deficit": 1
        },
        {
            "Material": "MAT-03",
            "Material_Desc": "Part 3",
            "Storage_Loc": "SL49",
            "SAP_Stock": 5.0,
            "WMS_Stock": 25.0,
            "Deficit_Qty": 20.0,
            "Accuracy_Before": 0.2,
            "Accuracy_After": 0.2,
            "Recent_Doc": "",
            "first_detected_date": "2026-10-25",
            "consecutive_days": 2,
            "is_sl49": 1,
            "is_sustained_deficit": 0
        }
    ]

    import notifier
    old_dir = notifier.REPORTS_DIR
    notifier.REPORTS_DIR = tmp_path
    try:
        excel_path = generate_unresolved_excel(
            plan_df=plan_df,
            run_date="20261026",
            remaining_deficits=remaining_deficits
        )
        assert excel_path.exists()

        excel_file = pd.ExcelFile(excel_path)
        assert "Unresolved_COGI" in excel_file.sheet_names
        assert "Remaining_SAP_Deficits" in excel_file.sheet_names

        df_cogi = pd.read_excel(excel_path, sheet_name="Unresolved_COGI")
        assert len(df_cogi) == 1
        assert df_cogi.iloc[0]["Material"] == "MAT-01"

        df_def = pd.read_excel(excel_path, sheet_name="Remaining_SAP_Deficits")
        assert len(df_def) == 2
        assert "SL49 (Quality DMA · Not a cycle-count target)" in df_def["Location Category"].values
        assert "Standard Warehouse" in df_def["Location Category"].values
        assert any("SUSTAINED" in str(v) for v in df_def["Sustained Deficit Status"].values)
        assert any("TRANSIENT" in str(v) for v in df_def["Sustained Deficit Status"].values)
    finally:
        notifier.REPORTS_DIR = old_dir


def test_d32_build_email_html_contains_deficit_section():
    """
    (d) Tests HTML email rendering of Section 2.5:
    - Contains Section Header
    - Displays deficit items
    - Renders SL49 품질 DMA badge
    - Renders Sustained / Transient deficit badges
    """
    remaining_deficits = [
        {
            "Material": "99000021-V1",
            "Material_Desc": "CONNECTOR",
            "Storage_Loc": "SL01",
            "SAP_Stock": 10.0,
            "WMS_Stock": 153.0,
            "Deficit_Qty": 143.0,
            "Accuracy_After": 0.065,
            "consecutive_days": 3,
            "is_sl49": 0,
            "is_sustained_deficit": 1
        },
        {
            "Material": "99000036-V2",
            "Material_Desc": "BRACKET",
            "Storage_Loc": "SL49",
            "SAP_Stock": 0.0,
            "WMS_Stock": 50.0,
            "Deficit_Qty": 50.0,
            "Accuracy_After": 0.0,
            "consecutive_days": 1,
            "is_sl49": 1,
            "is_sustained_deficit": 0
        }
    ]

    html = build_email_html(
        plan_df=pd.DataFrame(),
        execution_results=[],
        run_date="2026-10-26",
        run_time="08:15 AM EST",
        remaining_deficits=remaining_deficits
    )

    assert "Remaining SAP Deficits" in html
    assert "99000021-V1" in html
    assert "143.0" in html
    assert "3d" in html
    assert "SL49 Quality DMA" in html
    assert "Sustained shortage" in html
    assert "New / pending interface" in html

