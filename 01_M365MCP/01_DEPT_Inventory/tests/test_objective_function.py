import pytest
from datetime import datetime
import sys
from pathlib import Path

# Add HQ and Job dirs to path
INV_DIR = Path(__file__).resolve().parent.parent
HQ_DIR = INV_DIR.parent / "00_HQ_TaskManager"
JOB01_DIR = INV_DIR / "01_JOB_GR_GI_Error"
JOB02_DIR = INV_DIR / "02_JOB_Backlog_Clear"

for p in [HQ_DIR, JOB01_DIR, JOB02_DIR]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from wms_mapping import (
    calc_material_sloc_accuracy,
    is_in_guard_window,
    evaluate_settled_state_accuracy,
    ALLOWED_BALANCE_SLOCS
)


def test_calc_material_sloc_accuracy_zero_pair_excluded():
    """Verifies that WMS=0 and SAP=0 returns None (excluded from ZTCODE_STOCK_REC denominator)."""
    score_excluded = calc_material_sloc_accuracy(0.0, 0.0, exclude_zero_pair=True)
    assert score_excluded is None

    score_included = calc_material_sloc_accuracy(0.0, 0.0, exclude_zero_pair=False)
    assert score_included == 100.0


def test_calc_material_sloc_accuracy_standard_cases():
    """Verifies official ZTCODE_STOCK_REC formula matches real production cases."""
    # Case 1: SAP > WMS: (1 - |36 - 66| / 66) * 100 = 54.545%
    s1 = calc_material_sloc_accuracy(36.0, 66.0)
    assert abs(s1 - 54.545) < 0.01

    # Case 2: WMS > SAP: (1 - |1100 - 561| / 561) * 100 = 3.922%
    s2 = calc_material_sloc_accuracy(1100.0, 561.0)
    assert abs(s2 - 3.922) < 0.01

    # Case 3: SAP = 0, WMS > 0 -> 0.0%
    s3 = calc_material_sloc_accuracy(50.0, 0.0)
    assert s3 == 0.0

    # Case 4: WMS = 0, SAP > 0 -> 0.0%
    s4 = calc_material_sloc_accuracy(0.0, 50.0)
    assert s4 == 0.0

    # Case 5: Exact match -> 100.0%
    s5 = calc_material_sloc_accuracy(120.0, 120.0)
    assert s5 == 100.0


def test_guard_window_detection():
    """Verifies that guard window strictly identifies 05:30 to 06:15 EST."""
    dt_inside_1 = datetime(2026, 10, 26, 5, 30, 0)
    dt_inside_2 = datetime(2026, 10, 26, 6, 0, 0)
    dt_inside_3 = datetime(2026, 10, 26, 6, 15, 0)
    dt_outside_1 = datetime(2026, 10, 26, 5, 29, 59)
    dt_outside_2 = datetime(2026, 10, 26, 6, 15, 1)
    dt_outside_3 = datetime(2026, 10, 26, 14, 0, 0)

    assert is_in_guard_window(dt_inside_1) is True
    assert is_in_guard_window(dt_inside_2) is True
    assert is_in_guard_window(dt_inside_3) is True
    assert is_in_guard_window(dt_outside_1) is False
    assert is_in_guard_window(dt_outside_2) is False
    assert is_in_guard_window(dt_outside_3) is False


def test_d18_settled_state_outside_guard_window_allows_temporary_intermediate_dip():
    """
    D18: Outside guard window (e.g. 14:00), lending 50 EA from SL01 to SL00 causes
    a temporary dip in intermediate state, but downstream resolution restores SL01
    so final state has no drops and mean improves. This must be APPROVED.
    """
    wms = {"SL00": 50.0, "SL01": 100.0}
    sap_init = {"SL00": 0.0, "SL01": 100.0}       # SL00: 0%, SL01: 100% (Mean=50%)
    sap_inter = {"SL00": 50.0, "SL01": 50.0}      # SL00: 100%, SL01: 0% (SL01 temporary dip!)
    sap_final = {"SL00": 50.0, "SL01": 100.0}     # SL00: 100%, SL01: 100% (Mean=100%)

    dt_outside = datetime(2026, 10, 26, 14, 0, 0)
    approved, details = evaluate_settled_state_accuracy(
        wms_stock_by_sloc=wms,
        sap_initial_by_sloc=sap_init,
        sap_intermediate_by_sloc=sap_inter,
        sap_final_by_sloc=sap_final,
        target_slocs={"SL00", "SL01"},
        allow_temporary_mismatch=True,
        current_time=dt_outside
    )

    assert approved is True
    assert details["mean_init"] == 50.0
    assert details["mean_final"] == 100.0
    assert details["guard_active"] is False
    assert len(details["final_sloc_drops"]) == 0
    assert len(details["inter_sloc_drops"]) == 1 # SL01 dipped in intermediate


def test_d19_guard_window_strictly_blocks_temporary_intermediate_dip():
    """
    D19: Inside guard window (e.g. 05:45 EST), the exact same temporary intermediate
    dip must be REJECTED to prevent 06:00 ZTCODE_STOCK_REC aggregation from capturing the dip.
    """
    wms = {"SL00": 50.0, "SL01": 100.0}
    sap_init = {"SL00": 0.0, "SL01": 100.0}
    sap_inter = {"SL00": 50.0, "SL01": 50.0}      # Temporary dip in SL01
    sap_final = {"SL00": 50.0, "SL01": 100.0}     # Final is 100%

    dt_inside = datetime(2026, 10, 26, 5, 45, 0)
    approved, details = evaluate_settled_state_accuracy(
        wms_stock_by_sloc=wms,
        sap_initial_by_sloc=sap_init,
        sap_intermediate_by_sloc=sap_inter,
        sap_final_by_sloc=sap_final,
        target_slocs={"SL00", "SL01"},
        allow_temporary_mismatch=True,
        current_time=dt_inside
    )

    assert approved is False
    assert details["guard_active"] is True
    assert len(details["inter_sloc_drops"]) == 1
    assert details["inter_sloc_drops"][0][0] == "SL01"
    assert "GUARD_WINDOW_ACTIVE" in details["reason"]


def test_final_state_sloc_drop_always_blocked_even_outside_guard_window():
    """
    Always required: If final state itself drops an individual SLoc, it is REJECTED
    at all times, even outside guard window.
    """
    wms = {"SL00": 50.0, "SL01": 100.0}
    sap_init = {"SL00": 0.0, "SL01": 100.0}
    sap_final = {"SL00": 50.0, "SL01": 80.0} # SL01 permanently drops from 100% to 75%

    dt_outside = datetime(2026, 10, 26, 14, 0, 0)
    approved, details = evaluate_settled_state_accuracy(
        wms_stock_by_sloc=wms,
        sap_initial_by_sloc=sap_init,
        sap_final_by_sloc=sap_final,
        target_slocs={"SL00", "SL01"},
        current_time=dt_outside
    )

    assert approved is False
    assert len(details["final_sloc_drops"]) == 1
    assert details["final_sloc_drops"][0][0] == "SL01"
    assert "FINAL_STATE_SLOC_DROP" in details["reason"]


def test_d20_sp31_source_excluded_from_sloc_scoring():
    """
    D20: Moving stock from SP31 (external) into SL01 only improves SL01,
    while SP31 is not in target_slocs and therefore does not lower any score.
    """
    wms = {"SL01": 500.0}
    sap_init = {"SL01": 200.0} # Score = (1 - 300/200) clamped = 0%
    sap_final = {"SL01": 500.0} # Score = 100%

    dt_any = datetime(2026, 10, 26, 14, 0, 0)
    approved, details = evaluate_settled_state_accuracy(
        wms_stock_by_sloc=wms,
        sap_initial_by_sloc=sap_init,
        sap_final_by_sloc=sap_final,
        target_slocs={"SL01"},
        current_time=dt_any
    )

    assert approved is True
    assert details["mean_init"] == 0.0
    assert details["mean_final"] == 100.0


def test_gr_gi_resolver_integration_adopts_objective_function():
    """Verifies that gr_gi_resolver uses evaluate_settled_state_accuracy to approve valid transfers."""
    import pandas as pd
    from gr_gi_resolver import resolve_material_errors_unified

    mat = "99000008"
    # GI error: 311 from SL00 to SP31 with qty 100. Deficit in SL00.
    # GI error: 311 from SL02 to SP31 with qty 100. Deficit in SL02.
    gi_rows = pd.DataFrame([{
        "Material Number": mat,
        "Mvt": "311",
        "FromSt": "SL02",
        "TOSt": "SP31",
        "Qty": "100.0",
        "Doc. Date": "2026.10.26",
        "Pstng Date": "2026.10.26",
        "Message": "Deficit at SL02"
    }])

    # Stock comparison: SL02 has deficit (WMS 100, SAP 0). SL01 has genuine surplus (WMS 100, SAP 200).
    stock_df = pd.DataFrame([
        {"Material": mat, "SLoc": "SL02", "SAP_Stock": 0.0, "WMS_Stock": 100.0, "Surplus": -100.0},
        {"Material": mat, "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 100.0, "Surplus": 100.0},
    ])

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_df,
        protected_gr_materials=set(),
        today_str="2026.10.26",
        prev_month_str="2026.09.30",
        prev_wms_df=stock_df,
        prev_mb52_df=stock_df,
        prev_snapshot_time="2026-10-26 15:30:00"
    )

    # Transfer from SL01 to SL02 of 100 should be approved because:
    # Initial: SL02=0%, SL01=100% (Mean 50%)
    # Intermediate: SL01 lends 100 (SAP 100, WMS 100 -> 100%), SL02 gets 100 (SAP 100, WMS 100 -> 100%)
    # Final: SL02 issues 100 to SP31 (SAP 0, WMS 0 -> excluded 0/0). SL01 remains 100%. Mean = 100%.
    transfers = [p for p in plan if p.get("Action") == "TRANSFER_POSTING"]
    assert len(transfers) >= 1
    t = transfers[0]
    assert t["From_SLoc"] == "SL01"
    assert t["To_SLoc"] == "SL02"
    assert t["Transfer_Qty"] == 100.0
    assert t["Status"] == "READY_FOR_EXECUTION"


def test_gr_gi_resolver_interim_guardrail_sl00():
    """Verifies that gr_gi_resolver skips transfers to SL00 with SKIPPED_PENDING_INTERFACE_SL00 (028-1)."""
    import pandas as pd
    from gr_gi_resolver import resolve_material_errors_unified

    mat = "99000008"
    # GI error: 311 from SL00 to SP31 with qty 100. Deficit in SL00.
    gi_rows = pd.DataFrame([{
        "Material Number": mat,
        "Mvt": "311",
        "FromSt": "SL00",
        "TOSt": "SP31",
        "Qty": "100.0",
        "Doc. Date": "2026.10.26",
        "Pstng Date": "2026.10.26",
        "Message": "Deficit at SL00"
    }])

    stock_df = pd.DataFrame([
        {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 100.0, "Surplus": -100.0},
        {"Material": mat, "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 100.0, "Surplus": 100.0},
    ])

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_df,
        protected_gr_materials=set(),
        today_str="2026.10.26",
        prev_month_str="2026.09.30",
        prev_wms_df=stock_df,
        prev_mb52_df=stock_df,
        prev_snapshot_time="2026-10-26 15:30:00"
    )

    # Must be SKIPPED with SKIPPED_PENDING_INTERFACE_SL00
    sl00_items = [p for p in plan if p.get("To_SLoc") == "SL00"]
    assert len(sl00_items) >= 1
    item = sl00_items[0]
    assert item["Action"] == "SKIP"
    assert item["Status"] == "SKIPPED_PENDING_INTERFACE_SL00"


def test_backlog_resolver_integration_sp31_source_allowed():
    """Verifies that backlog_resolver allows SP31 as candidate source under D20."""
    import pandas as pd
    from unittest.mock import patch
    from backlog_resolver import build_backlog_resolution_plan

    mat = "99000013-01"
    cogi_items = [{
        "Material": mat,
        "Material_Desc": "TEST PART",
        "SLoc": "SL01",
        "Qty": 100.0,
        "Error_Code": "M7 021",
        "Can_Resolve_By_Transfer": True,
        "Entry_Date": "2026.10.26"
    }]

    # Stock: SL01 deficit (WMS 200, SAP 100), SP31 has 500 SAP stock
    mock_mb52 = pd.DataFrame([
        {"Material": mat, "SLoc": "SL01", "SAP_Stock": 100.0},
        {"Material": mat, "SLoc": "SP31", "SAP_Stock": 500.0},
    ])
    mock_wms = pd.DataFrame([
        {"Material": mat, "SLoc": "SL01", "WMS_Stock": 200.0},
        {"Material": mat, "SLoc": "SP31", "WMS_Stock": 0.0},
    ])

    with patch("backlog_resolver.load_mb52_stock", return_value=mock_mb52), \
         patch("backlog_resolver.load_wms_stock", return_value=mock_wms), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(mock_wms, mock_mb52, "2026-10-26 15:30:00")), \
         patch("backlog_resolver.load_active_gr_errors", return_value={}):
        plan_df = build_backlog_resolution_plan(cogi_items)

    assert not plan_df.empty
    approved = plan_df[plan_df["Action"] == "TRANSFER_POSTING"]
    assert len(approved) == 1
    row = approved.iloc[0]
    assert row["From_SLoc"] == "SP31"
    assert row["To_SLoc"] == "SL01"
    assert row["Transfer_Qty"] == 100.0
    assert row["Status"] == "APPROVED_FULL_RESOLUTION"

