import sys
from pathlib import Path
import pytest
import pandas as pd

# Paths
base_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(base_dir))
sys.path.insert(0, str(base_dir.parent.parent / "00_HQ_TaskManager"))

from gr_gi_resolver import resolve_material_errors_unified

def test_d92_no_gi_self_block():
    """
    Test D92: 01_JOB candidate transfers must NOT be blocked by their own GI errors.
    Material 99000022:
    - 312 GI error: 105 EA from SP31 to SL01 (Deficit at SP31: 14 EA needed)
    - SL01: SAP=105, WMS=91 (Surplus=14)
    - SP31: SAP=91, WMS=0 (Need 14 to reach 105)
    Candidate transfer: SL01 -> SP31 14 EA.
    Expected: NOT blocked by active_gi_errors, becomes READY_FOR_EXECUTION.
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000022', 'Mvt': '312', 'FromSt': 'SL01', 'TOSt': 'SP31',
        'Qty': '105.0', 'Message': 'Deficit of SL Unrestricted-use 14 EA : 99000022 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000022', 'SLoc': 'SL01', 'SAP_Stock': 105.0, 'WMS_Stock': 91.0, 'Surplus': 14.0},
        {'Material': '99000022', 'SLoc': 'SP31', 'SAP_Stock': 91.0, 'WMS_Stock': 0.0, 'Surplus': 91.0},
    ])
    
    # Active GI error map simulating ZTCODE_GI_ERR containing 99000022
    active_gi_errors = {
        '99000022': {
            'FromSLocs': {'SL01'}, 'ToSLocs': {'SP31'},
            'SrcSLocs': {'SP31'}, 'DstSLocs': {'SL01'},
            'Total_GI_Qty': 105.0, 'Count': 1
        }
    }
    
    prev_wms_df = pd.DataFrame([
        {'Material': '99000022', 'SLoc': 'SL01', 'WMS_Stock': 91.0}
    ])
    prev_mb52_df = pd.DataFrame([
        {'Material': '99000022', 'SLoc': 'SL01', 'SAP_Stock': 105.0}
    ])

    # Run resolver with active_gi_errors present
    plan = resolve_material_errors_unified(
        mat='99000022',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30',
        prev_wms_df=prev_wms_df,
        prev_mb52_df=prev_mb52_df,
        prev_snapshot_time='2026-10-27 18:02:15',
        active_gi_errors=active_gi_errors
    )
    
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 1, f"D92 failed: Expected 1 transfer, got {len(transfers)}: {[p.get('Status') for p in plan]}"
    t = transfers[0]
    assert t['From_SLoc'] == 'SL01'
    assert t['To_SLoc'] == 'SP31'
    assert t['Transfer_Qty'] == 14.0
    assert t['Status'] in ('READY_FOR_EXECUTION', 'PARTIAL_ACCURACY_REBALANCING')
    print(f"D92 Test Passed: 99000022 SL01 -> SP31 14 EA is {t['Status']}!")

def test_d92_preserves_gr_error_block():
    """
    Test D92: Confirm that active_gr_errors STILL blocks intersecting candidate transfers (D71/D73).
    """
    gi_rows = pd.DataFrame([{
        'Material': '99000022', 'Mvt': '312', 'FromSt': 'SL01', 'TOSt': 'SP31',
        'Qty': '105.0', 'Message': 'Deficit of SL Unrestricted-use 14 EA : 99000022 P001 SP31',
        'Date': '2026.10.27'
    }])
    stock_comparison = pd.DataFrame([
        {'Material': '99000022', 'SLoc': 'SL01', 'SAP_Stock': 105.0, 'WMS_Stock': 91.0, 'Surplus': 14.0},
        {'Material': '99000022', 'SLoc': 'SP31', 'SAP_Stock': 91.0, 'WMS_Stock': 0.0, 'Surplus': 91.0},
    ])
    
    # Active GR error map on SL01
    active_gr_errors = {
        '99000022': {
            'SLocs': {'SL01'},
            'Total_GR_Qty': 500.0,
            'Vendors': {'100234'}
        }
    }
    
    plan = resolve_material_errors_unified(
        mat='99000022',
        rows_df=gi_rows,
        stock_comparison=stock_comparison,
        protected_gr_materials=set(),
        today_str='2026.10.27',
        prev_month_str='2026.09.30',
        active_gr_errors=active_gr_errors
    )
    
    # Transfer from SL01 must be BLOCKED because SL01 intersects with active_gr_errors!
    transfers = [p for p in plan if p.get('Action') == 'TRANSFER_POSTING']
    assert len(transfers) == 0, f"D71/D73 failed: Expected 0 transfers due to GR block, got {len(transfers)}"
    print("D92 Test Passed: active_gr_errors still correctly blocks SL01 candidate!")
