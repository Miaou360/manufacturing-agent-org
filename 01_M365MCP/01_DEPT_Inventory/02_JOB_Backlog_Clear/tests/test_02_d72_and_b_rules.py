import sys
from pathlib import Path
import unittest
import pandas as pd

JOB_DIR = Path(__file__).resolve().parent.parent
HQ_DIR = JOB_DIR.parent.parent / "00_HQ_TaskManager"
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(HQ_DIR))

import backlog_resolver
from persistence_guard import PersistenceResult

class TestD72AndBRules(unittest.TestCase):
    def test_d72_mutual_backlog_exclusion_and_sp31_priority(self):
        # Material has backlog at both SP31 and SL49
        cogi_items = [
            {'Material': 'MAT_MUTUAL', 'Material_Desc': 'Test Part', 'SLoc': 'SL49', 'Qty': 2.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
            {'Material': 'MAT_MUTUAL', 'Material_Desc': 'Test Part', 'SLoc': 'SP31', 'Qty': 5.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
        ]

        mb52_stock = pd.DataFrame([
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SP31', 'SAP_Stock': 0.0},
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SL49', 'SAP_Stock': 0.0},
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SL01', 'SAP_Stock': 20.0},
        ])
        wms_stock = pd.DataFrame([
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SP31', 'WMS_Stock': 0.0},
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SL49', 'WMS_Stock': 0.0},
            {'Material': 'MAT_MUTUAL', 'SLoc': 'SL01', 'WMS_Stock': 0.0},
        ])

        backlog_resolver.check_transfer_persistence = lambda **kwargs: PersistenceResult(
            is_persistent=True, persistent_qty=20.0, source_persistent_surplus=20.0, dest_persistent_deficit=5.0, reason="OK", prev_snapshot_time="20261027_060000"
        )
        backlog_resolver.load_mb52_stock = lambda: mb52_stock
        backlog_resolver.load_wms_stock = lambda: wms_stock
        backlog_resolver.load_active_gr_errors = lambda path=None: {}
        backlog_resolver.load_active_gi_errors = lambda path=None: {}
        backlog_resolver.get_today_already_transferred_qty = lambda m, s, d=None: (0.0, [])

        plan = backlog_resolver.build_backlog_resolution_plan(cogi_items, save_excel=False)

        self.assertEqual(plan.iloc[0]['Target_SLoc'], 'SP31', "SP31 must be sorted first per D72!")
        self.assertEqual(plan.iloc[0]['From_SLoc'], 'SL01', "Source must be SL01, not SL49!")
        self.assertEqual(plan.iloc[1]['Target_SLoc'], 'SL49', "SL49 must be sorted second!")
        self.assertEqual(plan.iloc[1]['From_SLoc'], 'SL01', "Source for SL49 must be SL01, not SP31!")

    def test_b4_no_source_wms_stock_vs_true_shortage(self):
        cogi_items = [
            {'Material': 'MAT_WMS_EXISTS', 'Material_Desc': 'Test WMS Exists', 'SLoc': 'SP31', 'Qty': 10.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
            {'Material': 'MAT_TRUE_SHORT', 'Material_Desc': 'Test True Short', 'SLoc': 'SP31', 'Qty': 15.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
        ]

        mb52_stock = pd.DataFrame([
            {'Material': 'MAT_WMS_EXISTS', 'SLoc': 'SP31', 'SAP_Stock': 0.0},
            {'Material': 'MAT_TRUE_SHORT', 'SLoc': 'SP31', 'SAP_Stock': 0.0},
        ])
        wms_stock = pd.DataFrame([
            {'Material': 'MAT_WMS_EXISTS', 'SLoc': 'SL01', 'WMS_Stock': 50.0}, # Physical stock exists!
            {'Material': 'MAT_TRUE_SHORT', 'SLoc': 'SP31', 'WMS_Stock': 0.0},  # Zero physical stock
        ])

        backlog_resolver.load_mb52_stock = lambda: mb52_stock
        backlog_resolver.load_wms_stock = lambda: wms_stock
        backlog_resolver.load_active_gr_errors = lambda path=None: {}
        backlog_resolver.load_active_gi_errors = lambda path=None: {}
        backlog_resolver.get_today_already_transferred_qty = lambda m, s, d=None: (0.0, [])

        plan = backlog_resolver.build_backlog_resolution_plan(cogi_items, save_excel=False)

        row_wms = plan[plan['Material'] == 'MAT_WMS_EXISTS'].iloc[0]
        row_true = plan[plan['Material'] == 'MAT_TRUE_SHORT'].iloc[0]
        self.assertEqual(row_wms['Status'], 'NO_SAP_SOURCE_BUT_WMS_HAS_STOCK')
        self.assertEqual(row_true['Status'], 'TRUE_SHORTAGE')

    def test_b6_already_transferred_today_deduction(self):
        cogi_items = [
            {'Material': 'MAT_DEDUCT', 'Material_Desc': 'Test Deduct', 'SLoc': 'SP31', 'Qty': 8.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
        ]

        mb52_stock = pd.DataFrame([
            {'Material': 'MAT_DEDUCT', 'SLoc': 'SP31', 'SAP_Stock': 0.0},
            {'Material': 'MAT_DEDUCT', 'SLoc': 'SL01', 'SAP_Stock': 20.0},
        ])
        wms_stock = pd.DataFrame([
            {'Material': 'MAT_DEDUCT', 'SLoc': 'SP31', 'WMS_Stock': 0.0},
            {'Material': 'MAT_DEDUCT', 'SLoc': 'SL01', 'WMS_Stock': 0.0},
        ])

        backlog_resolver.load_mb52_stock = lambda: mb52_stock
        backlog_resolver.load_wms_stock = lambda: wms_stock
        backlog_resolver.load_active_gr_errors = lambda path=None: {}
        backlog_resolver.load_active_gi_errors = lambda path=None: {}
        # Mock that 8.0 EA was already transferred today at 06:20
        backlog_resolver.get_today_already_transferred_qty = lambda m, s, d=None: (8.0, ['4990000011'])

        plan = backlog_resolver.build_backlog_resolution_plan(cogi_items, save_excel=False)
        row = plan.iloc[0]
        self.assertEqual(row['Action'], 'SKIP')
        self.assertEqual(row['Status'], 'RESOLVABLE_BY_COGI_SAVE')
        self.assertIn('4990000011', row['Reason'])

    def test_b3_candidate_guardrail_breakdown(self):
        cogi_items = [
            {'Material': 'MAT_PERSIST_SHORT', 'Material_Desc': 'Test Persist Short', 'SLoc': 'SP31', 'Qty': 5.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True},
        ]

        mb52_stock = pd.DataFrame([
            {'Material': 'MAT_PERSIST_SHORT', 'SLoc': 'SP31', 'SAP_Stock': 0.0},
            {'Material': 'MAT_PERSIST_SHORT', 'SLoc': 'SL01', 'SAP_Stock': 10.0},
        ])
        wms_stock = pd.DataFrame([
            {'Material': 'MAT_PERSIST_SHORT', 'SLoc': 'SP31', 'WMS_Stock': 0.0},
            {'Material': 'MAT_PERSIST_SHORT', 'SLoc': 'SL01', 'WMS_Stock': 20.0}, # WMS > SAP
        ])

        backlog_resolver.load_mb52_stock = lambda: mb52_stock
        backlog_resolver.load_wms_stock = lambda: wms_stock
        backlog_resolver.load_active_gr_errors = lambda path=None: {}
        backlog_resolver.load_active_gi_errors = lambda path=None: {}
        backlog_resolver.get_today_already_transferred_qty = lambda m, s, d=None: (0.0, [])

        plan = backlog_resolver.build_backlog_resolution_plan(cogi_items, save_excel=False)
        row = plan.iloc[0]
        self.assertEqual(row['Status'], 'WAREHOUSE_SAP_SHORT_PERSISTENT')
        self.assertEqual(row['Best_Cand_SLoc'], 'SL01')
        self.assertEqual(row['Best_Cand_SAP'], 10.0)
        self.assertEqual(row['Best_Cand_WMS'], 20.0)

if __name__ == "__main__":
    unittest.main()
