import sys
import json
import sqlite3
from pathlib import Path
import pytest
import pandas as pd

base_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base_dir))
sys.path.insert(0, str(base_dir.parent.parent / "00_HQ_TaskManager"))

from gr_gi_resolver import resolve_material_errors_unified, run_cascade


def test_d97_case1_99000028_today_data(tmp_path):
    """
    BINSYNTH9952: 99000028 today's live data (2026-10-28 08:00/08:15):
    - 311 GI error: SL00 -> SL01 320 EA, Message='Insufficient stock'
    - Current SAP: SL01=352, SL02=640, SP31=74
    - Current WMS: SL01=654, SL02=640
    - Prev SAP: SL01=384, SL02=640, SP31=58
    - Prev WMS: SL01=686, SL02=640
    Expected:
    - SL01 -> SL00 320 EA
    - After roundtrip: SL01=352, SL00=0
    - Action='TRANSFER_POSTING', Status='ROUNDTRIP_CLEAR'
    - 1 row in GI_STOCK_GAP_SIGNALS (in scratch tmp_path DB)
    """
    scratch_db = tmp_path / "test_d97_signals.db"
    gi_rows = pd.DataFrame([{
        "Material Number": "99000028",
        "Mvt": "311",
        "FromSt": "SL00",
        "TOSt": "SL01",
        "Qty": "320.0",
        "Date": "2026.10.28",
        "Message": "Insufficient stock"
    }])
    stock_comp = pd.DataFrame([
        {"Material": "99000028", "SLoc": "SL01", "SAP_Stock": 352.0, "WMS_Stock": 654.0, "Surplus": -302.0},
        {"Material": "99000028", "SLoc": "SL02", "SAP_Stock": 640.0, "WMS_Stock": 640.0, "Surplus": 0.0},
        {"Material": "99000028", "SLoc": "SP31", "SAP_Stock": 74.0, "WMS_Stock": 0.0, "Surplus": 74.0},
    ])
    prev_mb52 = pd.DataFrame([
        {"Material": "99000028", "SLoc": "SL01", "SAP_Stock": 384.0},
        {"Material": "99000028", "SLoc": "SL02", "SAP_Stock": 640.0},
        {"Material": "99000028", "SLoc": "SP31", "SAP_Stock": 58.0},
    ])
    prev_wms = pd.DataFrame([
        {"Material": "99000028", "SLoc": "SL01", "WMS_Stock": 686.0},
        {"Material": "99000028", "SLoc": "SL02", "WMS_Stock": 640.0},
    ])

    plan = resolve_material_errors_unified(
        mat="99000028",
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.28",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-28 07:02:12",
        run_id="TEST_RUN_99000028",
        signal_db_path=scratch_db
    )

    transfers = [p for p in plan if p.get("Action") == "TRANSFER_POSTING"]
    assert len(transfers) == 1
    t = transfers[0]
    assert t["From_SLoc"] == "SL01"
    assert t["To_SLoc"] == "SL00"
    assert t["Transfer_Qty"] == 320.0
    assert t["Status"] == "ROUNDTRIP_CLEAR_VIA_DEST_D113"

    # Verify settled state after SL01 -> SL00 320 and cascade
    inter_state = {"SL01": 352.0 - 320.0, "SL00": 320.0, "SL02": 640.0, "SP31": 74.0}
    settled_state, cleared = run_cascade(
        inter_state,
        [{"src": "SL00", "dst": "SL01", "qty": 320.0, "mvt": "311"}]
    )
    assert len(cleared) == 1
    assert settled_state["SL01"] == 352.0
    assert settled_state["SL00"] == 0.0

    # Verify 1 row in GI_STOCK_GAP_SIGNALS
    with sqlite3.connect(str(scratch_db)) as conn:
        rows = conn.execute(
            "SELECT run_id, material, mvt, error_from, error_to, error_qty, roundtrip_sloc, roundtrip_qty, sap_total, wms_total, total_gap, signal_type FROM GI_STOCK_GAP_SIGNALS"
        ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r[0] == "TEST_RUN_99000028"
    assert r[1] == "99000028"
    assert r[2] == "311"
    assert r[3] == "SL00"
    assert r[4] == "SL01"
    assert r[5] == 320.0
    assert r[6] == "SL01"
    assert r[7] == 320.0
    assert r[8] == 1066.0
    assert r[9] == 1294.0
    assert r[10] == 228.0
    assert r[11] == "ROUNDTRIP_CLEAR"


def test_d97_case2_312_99000012_and_99000018_today_data(tmp_path):
    """
    Test 2: 312 errors 99000012 (60 EA) & 99000018 (108 EA) on today's data:
    Both have persistent warehouse deficit (WMS > SAP) in SL01 and SAP >= error_qty.
    Both become ROUNDTRIP_CLEAR (SL01 -> SP31 60 EA and SL01 -> SP31 108 EA).
    """
    scratch_db = tmp_path / "test_d97_312_signals.db"

    # 1) 99000012 (60 EA)
    gi_229301 = pd.DataFrame([{
        "Material Number": "99000012",
        "Mvt": "312",
        "FromSt": "SL01",
        "TOSt": "SP31",
        "Qty": "60.0",
        "Date": "2026.10.27",
        "Message": "Deficit of SL Unrestricted-use 60 EA : 99000012 P001 SP31"
    }])
    stock_229301 = pd.DataFrame([
        {"Material": "99000012", "SLoc": "SL01", "SAP_Stock": 701.0, "WMS_Stock": 768.0, "Surplus": -67.0},
    ])
    prev_mb52_229301 = pd.DataFrame([{"Material": "99000012", "SLoc": "SL01", "SAP_Stock": 701.0}])
    prev_wms_229301 = pd.DataFrame([{"Material": "99000012", "SLoc": "SL01", "WMS_Stock": 768.0}])

    plan_1 = resolve_material_errors_unified(
        mat="99000012",
        rows_df=gi_229301,
        stock_comparison=stock_229301,
        protected_gr_materials=set(),
        today_str="2026.10.28",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms_229301,
        prev_mb52_df=prev_mb52_229301,
        prev_snapshot_time="2026-10-28 07:02:12",
        run_id="TEST_RUN_312",
        signal_db_path=scratch_db
    )
    t1 = [p for p in plan_1 if p.get("Action") == "TRANSFER_POSTING"]
    assert len(t1) == 1
    assert t1[0]["From_SLoc"] == "SL01"
    assert t1[0]["To_SLoc"] == "SP31"
    assert t1[0]["Transfer_Qty"] == 60.0
    assert t1[0]["Status"] == "ROUNDTRIP_CLEAR"

    # 2) 99000018 (108 EA)
    gi_280440 = pd.DataFrame([{
        "Material Number": "99000018",
        "Mvt": "312",
        "FromSt": "SL01",
        "TOSt": "SP31",
        "Qty": "108.0",
        "Date": "2026.10.27",
        "Message": "Deficit of SL Unrestricted-use 108 EA : 99000018 P001 SP31"
    }])
    stock_280440 = pd.DataFrame([
        {"Material": "99000018", "SLoc": "SL01", "SAP_Stock": 570.0, "WMS_Stock": 678.0, "Surplus": -108.0},
        {"Material": "99000018", "SLoc": "SL02", "SAP_Stock": 4212.0, "WMS_Stock": 4212.0, "Surplus": 0.0},
    ])
    prev_mb52_280440 = pd.DataFrame([
        {"Material": "99000018", "SLoc": "SL01", "SAP_Stock": 570.0},
        {"Material": "99000018", "SLoc": "SL02", "SAP_Stock": 4212.0},
    ])
    prev_wms_280440 = pd.DataFrame([
        {"Material": "99000018", "SLoc": "SL01", "WMS_Stock": 678.0},
        {"Material": "99000018", "SLoc": "SL02", "WMS_Stock": 4212.0},
    ])

    plan_2 = resolve_material_errors_unified(
        mat="99000018",
        rows_df=gi_280440,
        stock_comparison=stock_280440,
        protected_gr_materials=set(),
        today_str="2026.10.28",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms_280440,
        prev_mb52_df=prev_mb52_280440,
        prev_snapshot_time="2026-10-28 07:02:12",
        run_id="TEST_RUN_312",
        signal_db_path=scratch_db
    )
    t2 = [p for p in plan_2 if p.get("Action") == "TRANSFER_POSTING"]
    assert len(t2) == 1
    assert t2[0]["From_SLoc"] == "SL01"
    assert t2[0]["To_SLoc"] == "SP31"
    assert t2[0]["Transfer_Qty"] == 108.0
    assert t2[0]["Status"] == "ROUNDTRIP_CLEAR"

    with sqlite3.connect(str(scratch_db)) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM GI_STOCK_GAP_SIGNALS").fetchone()[0]
    assert cnt == 2


def test_d97_case3_reject_when_other_warehouse_accuracy_drops():
    """
    Test 3: Synthetic case where roundtrip SL01 -> SL02 -> SL01 leaves residual stock in SL02
    or triggers a chained error that degrades SL02 accuracy (from 100% to 0%).
    Expected: Rejected by evaluate_settled_state_accuracy (0 transfers).
    """
    # Error 1: SL02 -> SL01 100 EA (deficit at SL02, where SL02 has SAP=50, WMS=50 -> 100% matched!)
    # If SL01 sends 100 EA to SL02, SL02 becomes 150, clears 100 EA to SL01, leaving SL02 at 50,
    # EXCEPT there is a second error at SL02: SL02 -> SP31 50 EA!
    # So sending 100 EA from SL01 -> SL02 clears BOTH SL02->SL01 (100) AND SL02->SP31 (50),
    # draining SL02 from 50 (100% matched with WMS=50) down to 0 (0% accuracy!).
    gi_rows = pd.DataFrame([
        {
            "Material Number": "SYN_DROP",
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SL01",
            "Qty": "100.0",
            "Date": "2026.10.28",
            "Message": "Insufficient stock"
        },
        {
            "Material Number": "SYN_DROP",
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SP31",
            "Qty": "100.0",
            "Date": "2026.10.28",
            "Message": "Insufficient stock"
        },
    ])
    # Wait: initially SL02 has SAP=50, WMS=50 (100% accuracy). Neither 100 EA error can auto-clear.
    # If SL01 (SAP=200, WMS=300) sends 50 EA to SL02, SL02 reaches 100, clears SL02->SL01 (100),
    # leaving SL02 with SAP=0 while WMS=50 (SL02 accuracy drops from 100% -> 0%!).
    stock_comp = pd.DataFrame([
        {"Material": "SYN_DROP", "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 350.0, "Surplus": -150.0},
        {"Material": "SYN_DROP", "SLoc": "SL02", "SAP_Stock": 50.0, "WMS_Stock": 50.0, "Surplus": 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat="SYN_DROP",
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.28",
        prev_month_str="2026.09.30"
    )
    transfers = [p for p in plan if p.get("Action") == "TRANSFER_POSTING"]
    assert len(transfers) == 0


def test_d97_case4_reject_when_error_not_cleared_insufficient_qty():
    """
    Test 4: Synthetic case where SL01 has SAP=200 (< 320 required to clear SL00->SL01 320 EA).
    Sending <= 200 EA does not clear the 320 EA error, and partial rebalancing has 0 accuracy gain.
    Expected: 0 transfers.
    """
    gi_rows = pd.DataFrame([{
        "Material Number": "SYN_INSUFF",
        "Mvt": "311",
        "FromSt": "SL00",
        "TOSt": "SL01",
        "Qty": "320.0",
        "Date": "2026.10.28",
        "Message": "Insufficient stock"
    }])
    stock_comp = pd.DataFrame([
        {"Material": "SYN_INSUFF", "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 500.0, "Surplus": -300.0},
        {"Material": "SYN_INSUFF", "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat="SYN_INSUFF",
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.28",
        prev_month_str="2026.09.30"
    )
    transfers = [p for p in plan if p.get("Action") == "TRANSFER_POSTING"]
    assert len(transfers) == 0

