"""
Unit tests for Backlog Strict Warehouse Accuracy Protection Guardrail (D71 & D111)
=================================================================================
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from backlog_resolver import build_backlog_resolution_plan


def _run_plan(cogi, mb52, wms):
    with patch("backlog_resolver.load_mb52_stock", return_value=mb52), \
         patch("backlog_resolver.load_wms_stock", return_value=wms), \
         patch("backlog_resolver.load_active_gr_errors", return_value={}), \
         patch("backlog_resolver.load_active_gi_errors", return_value={}), \
         patch("backlog_resolver.load_previous_snapshot_pair", return_value=(wms, mb52, "2026-10-26 15:45:00")), \
         patch("backlog_resolver.get_today_already_transferred_qty", return_value=(0.0, [])):
        return build_backlog_resolution_plan(cogi, save_excel=False)


class TestBacklogAccuracyGuardrail(unittest.TestCase):

    def test_matched_warehouse_protection(self):
        """User Case 3: SL01 has 100 in WMS and 100 in SAP, SP31 backlog 50."""
        mb52 = pd.DataFrame([{"Material": "MAT_001", "SLoc": "SL01", "SAP_Stock": 100.0}])
        wms = pd.DataFrame([{"Material": "MAT_001", "SLoc": "SL01", "WMS_Stock": 100.0}])
        cogi = [{
            "Material": "MAT_001", "Material_Desc": "Part 001", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "SKIP", "Must not transfer stock from 100% matched warehouse")
        self.assertEqual(row["Status"], "WAREHOUSE_BALANCED_WOULD_DEGRADE")
        self.assertIn("balanced", row["Reason"].lower())

    def test_full_surplus_resolution(self):
        """SL01 has 150 in SAP, 100 in WMS (Surplus 50), Backlog 50."""
        mb52 = pd.DataFrame([{"Material": "MAT_002", "SLoc": "SL01", "SAP_Stock": 150.0}])
        wms = pd.DataFrame([{"Material": "MAT_002", "SLoc": "SL01", "WMS_Stock": 100.0}])
        cogi = [{
            "Material": "MAT_002", "Material_Desc": "Part 002", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "TRANSFER_POSTING")
        self.assertEqual(row["Status"], "APPROVED_FULL_RESOLUTION")
        self.assertEqual(row["From_SLoc"], "SL01")
        self.assertEqual(row["Transfer_Qty"], 50.0)

    def test_partial_surplus_rebalancing(self):
        """SL01 has 130 in SAP, 100 in WMS (Surplus 30), Backlog 50."""
        mb52 = pd.DataFrame([{"Material": "MAT_003", "SLoc": "SL01", "SAP_Stock": 130.0}])
        wms = pd.DataFrame([{"Material": "MAT_003", "SLoc": "SL01", "WMS_Stock": 100.0}])
        cogi = [{
            "Material": "MAT_003", "Material_Desc": "Part 003", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "TRANSFER_POSTING")
        self.assertEqual(row["Status"], "PARTIAL_ACCURACY_REBALANCING")
        self.assertEqual(row["From_SLoc"], "SL01")
        self.assertEqual(row["Transfer_Qty"], 30.0)

    def test_deficit_warehouse_blocked(self):
        """SL01 has 80 in SAP, 100 in WMS (Surplus -20), Backlog 50."""
        mb52 = pd.DataFrame([{"Material": "MAT_004", "SLoc": "SL01", "SAP_Stock": 80.0}])
        wms = pd.DataFrame([{"Material": "MAT_004", "SLoc": "SL01", "WMS_Stock": 100.0}])
        cogi = [{
            "Material": "MAT_004", "Material_Desc": "Part 004", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "SKIP")
        self.assertEqual(row["Status"], "WAREHOUSE_SAP_SHORT_PERSISTENT")

    def test_multi_warehouse_sources_from_surplus_only(self):
        """SL01 is matched (100 == 100), SL02 has surplus (150 > 100)."""
        mb52 = pd.DataFrame([
            {"Material": "MAT_005", "SLoc": "SL01", "SAP_Stock": 100.0},
            {"Material": "MAT_005", "SLoc": "SL02", "SAP_Stock": 150.0}
        ])
        wms = pd.DataFrame([
            {"Material": "MAT_005", "SLoc": "SL01", "WMS_Stock": 100.0},
            {"Material": "MAT_005", "SLoc": "SL02", "WMS_Stock": 100.0}
        ])
        cogi = [{
            "Material": "MAT_005", "Material_Desc": "Part 005", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "TRANSFER_POSTING")
        self.assertEqual(row["From_SLoc"], "SL02", "Must take from SL02 (surplus), not SL01 (matched)")
        self.assertEqual(row["Transfer_Qty"], 50.0)

    def test_non_operation_slocs_excluded_as_source(self):
        """SL06 (excluded non-operation SLoc) has SAP stock 100, and must never be used as transfer source."""
        mb52 = pd.DataFrame([
            {"Material": "MAT_006", "SLoc": "SL06", "SAP_Stock": 100.0}
        ])
        wms = pd.DataFrame([
            {"Material": "MAT_006", "SLoc": "SL01", "WMS_Stock": 0.0}
        ])
        cogi = [{
            "Material": "MAT_006", "Material_Desc": "Part 006", "SLoc": "SP31",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "SKIP", "Must not draw stock from excluded SLoc SL06")
        self.assertIn(row["Status"], ["TRUE_SHORTAGE", "NO_SAP_SOURCE_BUT_WMS_HAS_STOCK"])
        self.assertNotEqual(row.get("From_SLoc"), "SL06")

    def test_inter_warehouse_matched_protection(self):
        """Matched warehouse protection applies across ALL destinations, including other warehouses (target SL02)."""
        mb52 = pd.DataFrame([
            {"Material": "MAT_007", "SLoc": "SL01", "SAP_Stock": 100.0},
            {"Material": "MAT_007", "SLoc": "SL02", "SAP_Stock": 0.0}
        ])
        wms = pd.DataFrame([
            {"Material": "MAT_007", "SLoc": "SL01", "WMS_Stock": 100.0},
            {"Material": "MAT_007", "SLoc": "SL02", "WMS_Stock": 0.0}
        ])
        cogi = [{
            "Material": "MAT_007", "Material_Desc": "Part 007", "SLoc": "SL02",
            "Qty": 50.0, "Error_Code": "Deficit", "Can_Resolve_By_Transfer": True
        }]

        plan = _run_plan(cogi, mb52, wms)
        self.assertEqual(len(plan), 1)
        row = plan.iloc[0]
        self.assertEqual(row["Action"], "SKIP", "Must preserve SL01 even if target is another warehouse SL02")
        self.assertEqual(row["Status"], "WAREHOUSE_BALANCED_WOULD_DEGRADE")


if __name__ == "__main__":
    unittest.main()
