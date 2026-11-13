"""
Unit tests for D91: WMS_ZERO_FLUSH 2-Snapshot Persistence Guard
==============================================================
Rules verified:
1. WMS=0.0 in both current and previous snapshot -> Flushes phantom SAP stock to SP31 (READY_FOR_EXECUTION)
2. WMS=0.0 in current, but > 0.0 in previous -> SKIP with WMS_ZERO_NOT_PERSISTENT (suspected interface delay)
3. WMS=0.0 in current, but previous snapshot absent -> SKIP with WMS_ZERO_NOT_PERSISTENT (first run safety hold)
"""

import sys
import unittest
from pathlib import Path
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from gr_gi_resolver import resolve_material_errors_unified


class TestD91WMSZeroFlushPersistence(unittest.TestCase):

    def test_d91_flush_passed_when_wms_zero_persists(self):
        """Case 1: 99000001 actual - WMS=0 confirmed in both current and prev snapshots."""
        mat = "99000001"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.27",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL01",
            "TOSt": "SP31",
            "Qty": "3,000.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1254.0, "WMS_Stock": 0.0, "Surplus": 1254.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0}
        ])

        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.27",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-27 15:32:13"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        self.assertEqual(len(transfers), 1, "Should generate 1 MB1B transfer posting")
        t_row = transfers.iloc[0]
        self.assertEqual(t_row["From_SLoc"], "SL01")
        self.assertEqual(t_row["To_SLoc"], "SP31")
        self.assertEqual(t_row["Transfer_Qty"], 1254.0)
        self.assertEqual(t_row["Status"], "READY_FOR_EXECUTION")
        self.assertEqual(t_row["Posting_Type"], "WMS_ZERO_FLUSH")
        self.assertEqual(t_row["Persistence_Checked"], True)
        self.assertEqual(t_row["Prev_Snapshot_Time"], "2026-10-27 15:32:13")
        self.assertEqual(t_row["Persistent_Qty"], 0.0)

    def test_d91_flush_blocked_when_prev_wms_positive(self):
        """Case 2: Current WMS=0, but previous snapshot had 500 EA -> Interface delay guard triggers."""
        mat = "99000001"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.27",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL01",
            "TOSt": "SP31",
            "Qty": "3,000.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1254.0, "WMS_Stock": 0.0, "Surplus": 1254.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0}
        ])

        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 500.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.27",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-27 15:32:13"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        self.assertEqual(len(transfers), 0, "Transfer must be blocked if previous WMS was positive")
        
        skips = df[df["Status"] == "WMS_ZERO_NOT_PERSISTENT"]
        self.assertEqual(len(skips), 1)
        s_row = skips.iloc[0]
        self.assertEqual(s_row["Action"], "SKIP")
        self.assertEqual(s_row["Persistence_Checked"], True)
        self.assertEqual(s_row["Persistent_Qty"], 500.0)
        self.assertIn("potential interface lag", s_row["Reason"])

    def test_d91_flush_blocked_when_no_prev_snapshot(self):
        """Case 3: Current WMS=0, but no previous snapshot exists -> Hold until 2nd snapshot."""
        mat = "99000001"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.27",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL01",
            "TOSt": "SP31",
            "Qty": "3,000.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1254.0, "WMS_Stock": 0.0, "Surplus": 1254.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.27",
            prev_month_str="2026.09.30",
            prev_wms_df=None,
            prev_snapshot_time=None
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        self.assertEqual(len(transfers), 0, "Transfer must be blocked if no previous snapshot")
        
        skips = df[df["Status"] == "WMS_ZERO_NOT_PERSISTENT"]
        self.assertEqual(len(skips), 1)
        s_row = skips.iloc[0]
        self.assertEqual(s_row["Action"], "SKIP")
        self.assertEqual(s_row["Persistence_Checked"], False)
        self.assertIn("prior snapshot is unavailable", s_row["Reason"])


if __name__ == '__main__':
    unittest.main()
