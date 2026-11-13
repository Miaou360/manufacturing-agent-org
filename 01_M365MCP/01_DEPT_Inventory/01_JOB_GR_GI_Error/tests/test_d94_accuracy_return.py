import sys
from pathlib import Path
import pytest
import pandas as pd

# Paths
base_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base_dir))
sys.path.insert(0, str(base_dir.parent.parent / "00_HQ_TaskManager"))

from gr_gi_resolver import resolve_material_errors_unified

def test_d94_case1_312_full_return_flush():
    """
    BINSYNTH9952 (Thread 125 Case 1): 99000014 (312) 1,211 approved -> SL02 reaches 710 (100% matched with WMS).
    - 312 error: 710 EA from SP31 to SL02 (dst = SL02)
    - SL02: SAP=1,211, WMS=710 (Surplus=501)
    - SP31: SAP=0, WMS=0
    q* = 1,211 + 710 - 710 = 1,211 EA.
    Expected: Proposes SL02 -> SP31 1,211 EA as READY_FOR_EXECUTION.
    After return, SL02 has 710 EA, matching WMS 100%!
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000014', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '710.0', 'Message': 'Deficit of SL Unrestricted-use 710 EA : 99000014 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comp = pd.DataFrame([
        {'Material': '99000014', 'SLoc': 'SL02', 'SAP_Stock': 1211.0, 'WMS_Stock': 710.0, 'Surplus': 501.0},
        {'Material': '99000014', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0, 'Surplus': 0.0},
    ])
    prev_wms_df = pd.DataFrame([
        {'Material': '99000014', 'SLoc': 'SL02', 'WMS_Stock': 710.0}
    ])
    prev_mb52_df = pd.DataFrame([
        {'Material': '99000014', 'SLoc': 'SL02', 'SAP_Stock': 1211.0}
    ])

    plan = resolve_material_errors_unified(
        mat='99000014',
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30',
        prev_wms_df=prev_wms_df,
        prev_mb52_df=prev_mb52_df,
        prev_snapshot_time='2026-10-27 18:02:15'
    )

    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 1, f"Expected 1 transfer, got {len(transfers)}"
    t = transfers[0]
    assert t['From_SLoc'] == 'SL02'
    assert t['To_SLoc'] == 'SP31'
    assert t['Transfer_Qty'] == 1211.0
    assert t['Status'] == 'READY_FOR_EXECUTION'
    print("D94 Case 1 Passed: 99000014 SL02 -> SP31 1,211 EA is READY_FOR_EXECUTION!")

def test_d94_case2_311_warehouse_flush():
    """
    Test 2 (Thread 125 Case 2): Virtual 311 warehouse transfer error SL01 -> SL02 100 EA.
    - SLoc SL02 is destination (dst = SL02) of the 311 error.
    - SLoc SL02 has SAP=150, WMS=100 (Surplus=50).
    - SLoc SL01 has SAP=0, WMS=0 (Need 100 to clear 311).
    q* = 150 + 100 - 100 = 150 EA from SL02 -> SL01.
    Expected: Proposes SL02 -> SL01 150 EA as READY_FOR_EXECUTION.
    After clearing, SL02 gets 100 EA back, ending at 100 EA == WMS (100% matched)!
    """
    gi_rows = pd.DataFrame([{
        'Material': 'MAT_311', 'Mvt': '311', 'FromSt': 'SL01', 'TOSt': 'SL02',
        'Qty': '100.0', 'Message': 'Deficit of SL Unrestricted-use 100 EA : MAT_311 P001 SL01',
        'Date': '2026.10.27'
    }])
    stock_comp = pd.DataFrame([
        {'Material': 'MAT_311', 'SLoc': 'SL02', 'SAP_Stock': 150.0, 'WMS_Stock': 100.0, 'Surplus': 50.0},
        {'Material': 'MAT_311', 'SLoc': 'SL01', 'SAP_Stock': 0.0, 'WMS_Stock': 50.0, 'Surplus': -50.0},
    ])
    prev_wms_df = pd.DataFrame([
        {'Material': 'MAT_311', 'SLoc': 'SL02', 'WMS_Stock': 100.0},
        {'Material': 'MAT_311', 'SLoc': 'SL01', 'WMS_Stock': 50.0}
    ])
    prev_mb52_df = pd.DataFrame([
        {'Material': 'MAT_311', 'SLoc': 'SL02', 'SAP_Stock': 150.0},
        {'Material': 'MAT_311', 'SLoc': 'SL01', 'SAP_Stock': 0.0}
    ])

    plan = resolve_material_errors_unified(
        mat='MAT_311',
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30',
        prev_wms_df=prev_wms_df,
        prev_mb52_df=prev_mb52_df,
        prev_snapshot_time='2026-10-27 18:02:15'
    )

    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 1, f"Expected 1 transfer, got {len(transfers)}"
    t = transfers[0]
    assert t['From_SLoc'] == 'SL02'
    assert t['To_SLoc'] == 'SL01'
    assert t['Transfer_Qty'] == 150.0
    assert t['Status'] == 'READY_FOR_EXECUTION'
    print("D94 Case 2 Passed: Virtual 311 SL02 -> SL01 150 EA is READY_FOR_EXECUTION!")

def test_d94_case3_not_destination_no_qstar():
    """
    Test 3 (Thread 125 Case 3): Source warehouse S is NOT the destination of error at D.
    - Error is 311 SL01 -> SP31 50 EA (dst = SP31, not SL02).
    - SL02 has SAP=100, WMS=100 (Surplus=0, no inflow expected).
    Expected: q* is not generated. SL02 cannot transfer without surplus.
    """
    gi_rows = pd.DataFrame([{
        'Material': 'MAT_NOT_DST', 'Mvt': '311', 'FromSt': 'SL01', 'TOSt': 'SP31',
        'Qty': '50.0', 'Message': 'Deficit of SL Unrestricted-use 50 EA',
        'Date': '2026.10.27'
    }])
    stock_comp = pd.DataFrame([
        {'Material': 'MAT_NOT_DST', 'SLoc': 'SL02', 'SAP_Stock': 100.0, 'WMS_Stock': 100.0, 'Surplus': 0.0},
        {'Material': 'MAT_NOT_DST', 'SLoc': 'SL01', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0, 'Surplus': 0.0},
        {'Material': 'MAT_NOT_DST', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='MAT_NOT_DST',
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, "Expected 0 transfers when S is not destination and has no surplus"
    print("D94 Case 3 Passed: S not destination correctly generates no transfer.")

def test_d94_case4_qstar_out_of_bounds():
    """
    Test 4 (Thread 125 Case 4): q* < net_needed or q* > avail_s must not be generated.
    - 312 error: SP31 -> SL02 1,000 EA (net_needed = 1,000 EA).
    - SL02: SAP=500, WMS=800 (Deficit -300).
      q* = SAP (500) + Inflow (1,000) - WMS (800) = 700 EA.
      Since q* (700) < net_needed (1,000), q* is insufficient to clear the error -> NOT added as q*.
    """
    gi_rows = pd.DataFrame([{
        'Material': 'MAT_OOB', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '1000.0', 'Message': 'Deficit of SL Unrestricted-use 1000 EA : MAT_OOB P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comp = pd.DataFrame([
        {'Material': 'MAT_OOB', 'SLoc': 'SL02', 'SAP_Stock': 500.0, 'WMS_Stock': 800.0, 'Surplus': -300.0},
        {'Material': 'MAT_OOB', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='MAT_OOB',
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers when q* < net_needed, got {len(transfers)}"
    print("D94 Case 4 Passed: q* < net_needed correctly not approved.")
