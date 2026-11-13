import sys
from pathlib import Path
import pytest
import pandas as pd

# Paths
base_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base_dir))
sys.path.insert(0, str(base_dir.parent.parent / "00_HQ_TaskManager"))

from gr_gi_resolver import resolve_material_errors_unified

def test_312_pump_priming_standard():
    """
    BINSYNTH9952: Standard 99000002 case (D70):
    - SL02: SAP=28, WMS=52 (Deficit -24, no persistent surplus)
    - SP31: SAP=19, WMS=19 (Deficit 5 for 24 EA 312 error)
    - 312 Error: 24 EA from SP31 to SL02
    Expected: Proposes 5 EA transfer SL02 -> SP31, unlocks 24 EA back to SL02.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 1, f"Expected 1 transfer, got {len(transfers)}"
    t = transfers[0]
    assert t['From_SLoc'] == 'SL02'
    assert t['To_SLoc'] == 'SP31'
    assert t['Transfer_Qty'] == 5.0
    assert '312 reverse pump-priming transfer' in t['Reason']
    print("BINSYNTH9952 Passed: 5 EA SL02 -> SP31 successfully proposed.")

def test_312_pump_priming_break_not_312():
    """
    Test 2 (Updated for D97): Movement-type agnostic pump-priming:
    - When 311 has dst == SL02 (FromSt=SP31, TOSt=SL02), D97 allows pump-priming (SL02 -> SP31 5 EA).
    - When unsupported movement type (e.g. 261) or dst != SL02, it is rejected.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '311', 'FromSt': 'SP31', 'TOSt': 'SL02',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 1, f"Expected 1 transfer under D97 for 311 returning to SL02, got {len(transfers)}"
    assert transfers[0]['From_SLoc'] == 'SL02'
    assert transfers[0]['To_SLoc'] == 'SP31'
    assert transfers[0]['Transfer_Qty'] == 5.0
    print("Test 2 Passed: 311 movement returning to SL02 approved under D97.")

def test_312_pump_priming_break_wrong_destination():
    """
    Test 3: Breaking condition (b): 312 error returns to SL01, not SL02.
    - SL02 has deficit (-24), SP31 returns to SL01.
    Expected: REJECTED because SL02 does not receive the returned stock.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL01', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SL01', 'SAP_Stock': 10.0, 'WMS_Stock': 10.0, 'Surplus': 0.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    # SL02 should not be the source
    sl02_transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING' and p.get('From_SLoc') == 'SL02']
    assert len(sl02_transfers) == 0, f"Expected 0 transfers from SL02, got {len(sl02_transfers)}"
    print("Test 3 Passed: 312 returning to different location properly rejected.")

def test_312_pump_priming_break_insufficient_physical_stock():
    """
    Test 4: Breaking condition (c): SL02 physical SAP stock is less than needed (e.g. SAP=2, needed=5).
    Expected: REJECTED because SL02 cannot physically send 5 EA.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 2.0, 'WMS_Stock': 52.0, 'Surplus': -50.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers when SAP < needed, got {len(transfers)}"
    print("Test 4 Passed: Insufficient physical stock properly rejected.")

def test_312_pump_priming_break_negative_net_gain():
    """
    Test 5: Breaking condition (d): 312 return quantity < priming quantity (strictly negative net gain < 0).
    - SP31 has an outbound 311 (2 EA to SL01) and a 312 (8 EA back to SL02).
    - Sending 10 EA to SP31 returns only 8 EA to SL02 (net -2 EA in SL02).
    Expected: REJECTED by D97 net_gain >= 0 guard.
    """
    gi_rows = pd.DataFrame([
        {
            'Material': '99000002', 'Mvt': '311', 'FromSt': 'SP31', 'TOSt': 'SL01',
            'Qty': '2.0', 'Message': 'Deficit of SL Unrestricted-use 2 EA : 99000002 P001 SP31',
            'Date': '2026.10.27'
        },
        {
            'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
            'Qty': '8.0', 'Message': 'Deficit of SL Unrestricted-use 8 EA : 99000002 P001 SP31',
            'Date': '2026.10.27'
        }
    ])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers when net gain <= 0, got {len(transfers)}"
    print("Test 5 Passed: Negative net gain properly rejected.")

def test_312_pump_priming_break_cond_b_locked():
    """
    Test 6: Breaking condition (b-1): Error reason contains lock / valuation lock error.
    - Message: 'Valuation data locked by user USER01'
    Expected: REJECTED because pump-priming cannot resolve locked/customizing/accounting errors.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Valuation data locked by user USER01 : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers for locked error, got {len(transfers)}"
    print("Test 6 Passed: Valuation lock error properly rejected (Condition b).")

def test_312_pump_priming_break_cond_b_period_closed():
    """
    Test 7: Breaking condition (b-2): Error reason is period closed / accounting error.
    - Message: 'Posting only possible in periods 2026/09 and 2026/08 in company code 3200'
    Expected: REJECTED because not a pure stock shortage.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Posting only possible in periods 2026/09 and 2026/08 in company code 3200',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers for period closed error, got {len(transfers)}"
    print("Test 7 Passed: Period closed error properly rejected (Condition b).")

def test_312_pump_priming_break_cond_c_gap_too_small():
    """
    Test 8: Breaking condition (c-1): Current warehouse gap (WMS - SAP) is too small to explain 312 return.
    - Error is 24 EA, test_q = 5 EA -> net return needed = 19 EA.
    - SL02 WMS=35, SAP=28 -> gap is only 7 EA (< 19 EA).
    Expected: REJECTED because physical warehouse stock does not explain the full 312 reverse return.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 35.0, 'Surplus': -7.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers when gap < net required, got {len(transfers)}"
    print("Test 8 Passed: Small warehouse gap properly rejected (Condition c-1).")

def test_312_pump_priming_break_cond_c_prev_gap_non_positive():
    """
    Test 9: Breaking condition (c-2): Persistence check - previous snapshot gap <= 0 (transient illusion).
    - Previous snapshot: SL02 WMS=28, SAP=28 (gap = 0).
    Expected: REJECTED because gap is not persistent across snapshots.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000002', 'Mvt': '312', 'FromSt': 'SL02', 'TOSt': 'SP31',
        'Qty': '24.0', 'Message': 'Deficit of SL Unrestricted-use 5 EA : 99000002 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0, 'WMS_Stock': 52.0, 'Surplus': -24.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0, 'WMS_Stock': 19.0, 'Surplus': 0.0},
    ])
    prev_mb52 = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'SAP_Stock': 28.0},
        {'Material': '99000002', 'SLoc': 'SP31', 'SAP_Stock': 19.0},
    ])
    prev_wms = pd.DataFrame([
        {'Material': '99000002', 'SLoc': 'SL02', 'WMS_Stock': 28.0},  # gap was 0 in prev snapshot!
        {'Material': '99000002', 'SLoc': 'SP31', 'WMS_Stock': 19.0},
    ])
    plan = resolve_material_errors_unified(
        mat='99000002',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30',
        prev_mb52_df=prev_mb52,
        prev_wms_df=prev_wms,
        prev_snapshot_time='2026-10-27 09:02:14'
    )
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"Expected 0 transfers when previous snapshot gap <= 0, got {len(transfers)}"
    print("Test 9 Passed: Non-persistent gap properly rejected (Condition c-2).")

if __name__ == "__main__":
    test_312_pump_priming_standard()
    test_312_pump_priming_break_not_312()
    test_312_pump_priming_break_wrong_destination()
    test_312_pump_priming_break_insufficient_physical_stock()
    test_312_pump_priming_break_negative_net_gain()
    test_312_pump_priming_break_cond_b_locked()
    test_312_pump_priming_break_cond_b_period_closed()
    test_312_pump_priming_break_cond_c_gap_too_small()
    test_312_pump_priming_break_cond_c_prev_gap_non_positive()
    print("\n[SUCCESS] ALL 9 UNIT TESTS (INCLUDING COND B & C) PASSED SUCCESSFULLY!")

