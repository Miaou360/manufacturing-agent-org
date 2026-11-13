"""
Unit tests for GI Error Warehouse Accuracy Maximization Flush
=============================================================
Tests:
1. Material 99000014: GI error 1,290 EA (SL02 -> SP31), SAP SL02 = 1,211 EA, WMS = 0 EA.
   - Must generate MB1B 311 transfer of 1,211 EA from SL02 to SP31 (Action: TRANSFER_POSTING, Status: READY_FOR_EXECUTION).
   - Must mark original GI error row as SKIP (Status: SHORTAGE_DEFICIT_PRESERVED, shortage: 79 EA).
   - Achieves 100% accuracy in warehouse SL02 (0 EA == 0 EA).
2. Material 99000033: GI error 3,040 EA (SL01 -> SP31), SAP SL01 = 1,592 EA, WMS = 0 EA.
   - Must generate MB1B 311 transfer of 1,592 EA from SL01 to SP31.
   - Must mark original GI error row as SKIP (shortage: 1,448 EA).
3. Matched Warehouse Room (e.g. SL01 SAP 100 == WMS 100):
   - Never flushed/transferred to line if no surplus.
"""

import sys
import unittest
from pathlib import Path
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from gr_gi_resolver import resolve_material_errors_unified, WAREHOUSE_SLOCS


class TestGIAccuracyFlush(unittest.TestCase):

    def test_99000014_warehouse_flush(self):
        mat = "99000014"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SP31",
            "Qty": "1,290.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 1211.0, "WMS_Stock": 0.0, "Surplus": 1211.0}
        ])

        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-26 05:45:00"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        skips = df[df["Action"] == "SKIP"]

        self.assertEqual(len(transfers), 1, "Should generate exactly 1 MB1B transfer posting")
        t_row = transfers.iloc[0]
        self.assertEqual(t_row["From_SLoc"], "SL02")
        self.assertEqual(t_row["To_SLoc"], "SP31")
        self.assertEqual(t_row["Transfer_Qty"], 1211.0)
        self.assertEqual(t_row["Status"], "READY_FOR_EXECUTION")
        self.assertEqual(t_row["Posting_Type"], "WMS_ZERO_FLUSH")
        self.assertIn("achieve 100% stock accuracy", t_row["Reason"])

        self.assertEqual(len(skips), 1, "Should preserve original GI error row as SKIP")
        s_row = skips.iloc[0]
        self.assertEqual(s_row["Status"], "SHORTAGE_DEFICIT_PRESERVED")
        self.assertEqual(s_row["Posting_Type"], "WMS_ZERO_FLUSH_REMAINDER")
        self.assertIn("79.0 EA", s_row["Reason"])

    def test_99000033_warehouse_flush(self):
        mat = "99000033"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL01",
            "TOSt": "SP31",
            "Qty": "3,040.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1592.0, "WMS_Stock": 0.0, "Surplus": 1592.0}
        ])

        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-26 05:45:00"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        skips = df[df["Action"] == "SKIP"]

        self.assertEqual(len(transfers), 1)
        t_row = transfers.iloc[0]
        self.assertEqual(t_row["From_SLoc"], "SL01")
        self.assertEqual(t_row["To_SLoc"], "SP31")
        self.assertEqual(t_row["Transfer_Qty"], 1592.0)
        self.assertEqual(t_row["Status"], "READY_FOR_EXECUTION")
        self.assertEqual(t_row["Posting_Type"], "WMS_ZERO_FLUSH")

        self.assertEqual(len(skips), 1)
        s_row = skips.iloc[0]
        self.assertEqual(s_row["Status"], "SHORTAGE_DEFICIT_PRESERVED")
        self.assertEqual(s_row["Posting_Type"], "WMS_ZERO_FLUSH_REMAINDER")
        self.assertIn("1,448.0 EA", s_row["Reason"])

    def test_matched_warehouse_not_flushed(self):
        mat = "TEST_MATCHED"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL01",
            "TOSt": "SP31",
            "Qty": "150.000",
            "Message": "Insufficient stock"
        }])

        # SL01 has SAP 100, WMS 100 (100% matched, Surplus 0)
        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 100.0, "WMS_Stock": 100.0, "Surplus": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        skips = df[df["Action"] == "SKIP"]

        self.assertEqual(len(transfers), 0, "Matched warehouse must not be flushed without surplus")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips.iloc[0]["Status"], "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD")

    def test_flush_blocked_if_variance_worsens(self):
        """If moving to TOSt would increase total plant discrepancy, flush must be blocked."""
        mat = "TEST_VAR_BLOCK"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SP31",
            "Qty": "500.000",
            "Message": "Insufficient stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 100.0, "WMS_Stock": 0.0, "Surplus": 100.0}
        ])
        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-26 05:45:00"
        )
        df = pd.DataFrame(plan)
        self.assertEqual(len(df[df["Action"] == "TRANSFER_POSTING"]), 1)

    def test_inter_warehouse_flush_sl02_to_sl01(self):
        """Inter-warehouse transfer error (SL02 -> SL01): flush applies across all destinations."""
        mat = "INTER_WH_01"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SL01",
            "Qty": "300.000",
            "Message": "Deficit of SL Stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 200.0, "WMS_Stock": 0.0, "Surplus": 200.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 200.0, "Surplus": -200.0}
        ])
        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0},
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 200.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_snapshot_time="2026-10-26 05:45:00"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        self.assertEqual(len(transfers), 1, "Must generate transfer posting between warehouse rooms")
        t = transfers.iloc[0]
        self.assertEqual(t["From_SLoc"], "SL02")
        self.assertEqual(t["To_SLoc"], "SL01")
        self.assertEqual(t["Transfer_Qty"], 200.0)
        self.assertEqual(t["Posting_Type"], "WMS_ZERO_FLUSH")

    def test_cascade_source_protects_matched_warehouse(self):
        """Inter-warehouse cascade: SL01 is 100% matched (100 == 100), SL03 has surplus (150 > 50).
        Must take from SL03, strictly preserving SL01."""
        mat = "CASCADE_WH_02"
        rows_df = pd.DataFrame([{
            "Date": "2026.10.23",
            "Material Number": mat,
            "Mvt": "311",
            "FromSt": "SL02",
            "TOSt": "SP31",
            "Qty": "50.000",
            "Message": "Deficit of SL Stock"
        }])

        stock_comparison = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 100.0, "WMS_Stock": 100.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL03", "SAP_Stock": 150.0, "WMS_Stock": 50.0, "Surplus": 100.0}
        ])
        prev_wms_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0},
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 100.0},
            {"Material": mat, "SLoc": "SL03", "WMS_Stock": 50.0}
        ])
        prev_mb52_df = pd.DataFrame([
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 100.0},
            {"Material": mat, "SLoc": "SL03", "SAP_Stock": 150.0}
        ])

        plan = resolve_material_errors_unified(
            mat=mat,
            rows_df=rows_df,
            stock_comparison=stock_comparison,
            protected_gr_materials=set(),
            today_str="2026.10.26",
            prev_month_str="2026.09.30",
            prev_wms_df=prev_wms_df,
            prev_mb52_df=prev_mb52_df,
            prev_snapshot_time="2026-10-26 05:45:00"
        )

        df = pd.DataFrame(plan)
        transfers = df[df["Action"] == "TRANSFER_POSTING"]
        self.assertEqual(len(transfers), 1)
        t = transfers.iloc[0]
        self.assertEqual(t["From_SLoc"], "SL03", "Must draw from surplus warehouse SL03, NOT 100% matched SL01")
        self.assertEqual(t["To_SLoc"], "SL02")
        self.assertEqual(t["Transfer_Qty"], 50.0)


if __name__ == "__main__":
    unittest.main()

