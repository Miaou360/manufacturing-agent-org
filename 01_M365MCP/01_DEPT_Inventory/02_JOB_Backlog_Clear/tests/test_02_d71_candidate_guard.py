import sys
from pathlib import Path
import unittest
import pandas as pd

JOB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JOB_DIR))

import backlog_resolver as br
import notifier

class TestD71CandidateGuard(unittest.TestCase):
    def test_d71_gr_error_candidate_block(self):
        print("=== BINSYNTH9952: 99000023 with SL00 having active GR error ===")
        cogi_items = [
            {
                'Material': '99000023',
                'Material_Desc': 'TEST LENS',
                'SLoc': 'SP31',
                'Qty': 1200.0,
                'Error_Code': 'M7 021',
                'Can_Resolve_By_Transfer': True
            }
        ]

        stock_matrix = pd.DataFrame([
            {'Material': '99000023', 'SLoc': 'SL00', 'SAP_Stock': 1.0, 'WMS_Stock': 400.0},
            {'Material': '99000023', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SL01', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SL02', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SL03', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SL04', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SL49', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
        ])

        active_gr_errors = {
            '99000023': {
                'Material': '99000023',
                'Total_GR_Qty': 1800.0,
                'Count': 1,
                'Vendors': {'V040'},
                'SLocs': {'SL00'}
            }
        }

        active_gi_errors = {}

        plan_df = br.build_backlog_resolution_plan(
            cogi_items=cogi_items,
            stock_matrix=stock_matrix,
            active_gr_errors=active_gr_errors,
            active_gi_errors=active_gi_errors,
            save_excel=False
        )

        row = plan_df.iloc[0]
        self.assertEqual(row['Action'], 'SKIP')
        self.assertEqual(row['Status'], 'BLOCKED_BY_GR_ERROR')
        self.assertEqual(row['Best_Cand_SLoc'], 'SL00')
        self.assertTrue('BLOCKED' in row['Reason'] or 'blocked' in row['Reason'])

    def test_path_guard_in_notifier(self):
        print("\n=== TEST 2: Path Guard in notifier.py ===")
        cogi_items = [
            {'Material': '99000023', 'Material_Desc': 'TEST', 'SLoc': 'SP31', 'Qty': 10.0, 'Error_Code': 'M7 021', 'Can_Resolve_By_Transfer': True}
        ]
        stock_matrix = pd.DataFrame([
            {'Material': '99000023', 'SLoc': 'SL00', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
            {'Material': '99000023', 'SLoc': 'SP31', 'SAP_Stock': 0.0, 'WMS_Stock': 0.0},
        ])
        plan_df = br.build_backlog_resolution_plan(cogi_items=cogi_items, stock_matrix=stock_matrix, save_excel=False)
        
        test_out = notifier.generate_unresolved_excel(
            plan_df=plan_df,
            run_date="20261027",
            is_test=True
        )
        self.assertTrue(test_out.name.startswith("[TEST]_"), f"Filename should start with [TEST]_, got {test_out.name}")
        self.assertIn("scratch", str(test_out), f"Test output should be in scratch, got {test_out}")
        
        # Clean up scratch test file if created
        if test_out.exists():
            test_out.unlink()

if __name__ == "__main__":
    unittest.main()
