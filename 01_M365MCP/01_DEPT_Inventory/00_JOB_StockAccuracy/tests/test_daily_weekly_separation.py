import sys
from pathlib import Path
import unittest
import pandas as pd

JOB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(JOB_DIR))

import box_aging_tracker

class TestDailyWeeklySeparation(unittest.TestCase):
    def test_bin_characteristics(self):
        # SCR 9939 is Plant 3
        scr_info = box_aging_tracker.get_bin_characteristics("SCR 9939")
        self.assertTrue(scr_info["is_plant3"])
        self.assertFalse(scr_info["is_plant1"])
        self.assertIsNone(scr_info["tier"])

        # R bin is Plant 3
        r_info = box_aging_tracker.get_bin_characteristics("R01B 2")
        self.assertTrue(r_info["is_plant3"])
        self.assertFalse(r_info["is_plant1"])
        self.assertEqual(r_info["tier"], 2)

        # Plant 1 floor bin
        p1_floor = box_aging_tracker.get_bin_characteristics("55Z 1")
        self.assertTrue(p1_floor["is_plant1"])
        self.assertFalse(p1_floor["is_plant3"])
        self.assertEqual(p1_floor["tier"], 1)

        # Plant 1 high rack
        p1_high = box_aging_tracker.get_bin_characteristics("53Z 3")
        self.assertTrue(p1_high["is_plant1"])
        self.assertFalse(p1_high["is_plant3"])
        self.assertEqual(p1_high["tier"], 3)

        # OEM floor bin (D84: Plant 1 floor bin, no tier, treated like SMT)
        oem_info = box_aging_tracker.get_bin_characteristics("OEM 02")
        self.assertTrue(oem_info["is_plant1"])
        self.assertFalse(oem_info["is_plant3"])
        self.assertIsNone(oem_info["tier"])
        self.assertTrue(oem_info["is_supermarket"])
        self.assertTrue(oem_info["is_oem_floor"])

        print("✅ Bin characteristics test passed.")

    def test_daily_vs_weekly_intersection_zero(self):
        # Locate sample WMS file
        wms_dir = JOB_DIR / "wms_daily_stack"
        wms_files = sorted(wms_dir.glob("WMS_StockList_HL_*.xlsx"))
        if not wms_files:
            self.skipTest(f"No WMS files found in {wms_dir}")

        wms_file = wms_files[-1]
        tracker = box_aging_tracker.BoxAgingTracker()
        df = pd.read_excel(wms_file)
        
        barcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), None)
        df = df.dropna(subset=[barcode_col]).copy()
        df["Barcode"] = df[barcode_col].astype(str).str.strip()
        df["Mat.No"] = df["Mat.No"].astype(str).str.strip()
        df["BIN"] = df["BIN"].astype(str).str.strip()
        df["Mat.Desc"] = df["Mat.Desc"].astype(str).str.strip()
        df["Lot_Date"] = None
        df["Warehouse_Dwell_Days"] = 0
        df["Bin_Dwell_Days"] = 1

        # 1. Run Daily
        daily_res = tracker.detect_mis_scans(df, mode="Daily", run_date="2026-10-27")
        daily_bins = daily_res["top_bins"] + daily_res["extended_bins"] + daily_res["random_bins"]
        daily_codes = {b["BIN"] for b in daily_bins}
        self.assertEqual(len(daily_bins), 43)

        # 2. Run Weekly
        weekly_res = tracker.detect_mis_scans(df, mode="Weekly", run_date="2026-10-27")
        weekly_bins = weekly_res["top_bins"] + weekly_res["extended_bins"] + weekly_res["random_bins"]
        weekly_codes = {b["BIN"] for b in weekly_bins}
        self.assertEqual(len(weekly_bins), 55)

        # 3. Assert Intersection is EXACTLY ZERO
        overlap = daily_codes & weekly_codes
        self.assertEqual(len(overlap), 0, f"Overlap found between Daily and Weekly: {overlap}")
        print(f"✅ Intersection test passed: Daily ({len(daily_codes)}) & Weekly ({len(weekly_codes)}) Overlap = 0!")

        # 4. Assert Daily has 0 Plant 3 / SCR bins
        for b in daily_bins:
            b_up = b["BIN"].upper()
            self.assertFalse(b_up.startswith("R") or b_up.startswith("SCR"))

        # 5. Assert Weekly has 0 Plant 1 Floor bins (including OEM)
        for b in weekly_bins:
            b_up = b["BIN"].upper()
            self.assertFalse(b_up.startswith(box_aging_tracker.OEM_FLOOR_PREFIXES), f"OEM floor bin found in Weekly: {b_up}")
            info = box_aging_tracker.get_bin_characteristics(b_up)
            if info["is_plant1"]:
                tier = info.get("tier") or 0
                is_p1_floor = (tier in [1, 2]) or b_up.startswith((("SMT", "CRT", "PRO", "DOLLY") + box_aging_tracker.OEM_FLOOR_PREFIXES)) or (tier == 0)
                self.assertFalse(is_p1_floor, f"Plant 1 floor bin found in Weekly: {b_up}")

        print("✅ All Zero-Tolerance assertions for Daily (0 Plant 3) and Weekly (0 Plant 1 Floor) passed!")

if __name__ == "__main__":
    unittest.main()
