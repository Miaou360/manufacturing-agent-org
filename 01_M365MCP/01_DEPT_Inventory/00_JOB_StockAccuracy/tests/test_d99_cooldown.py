import sys
import json
from pathlib import Path
import pandas as pd

WORKTREE_00 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE_00))

import db_manager
from box_aging_tracker import BoxAgingTracker


def test_d99_business_days_excl_sunday():
    # Tue 10/27 -> Wed 10/28 = 1d
    assert db_manager.calculate_business_days_excl_sunday("2026-10-27", "2026-10-28") == 1
    # Tue 10/27 -> Thu 10/29 = 2d
    assert db_manager.calculate_business_days_excl_sunday("2026-10-27", "2026-10-29") == 2
    # Sat 10/24 -> Mon 10/26 = 1d (Sun 10/25 excluded)
    assert db_manager.calculate_business_days_excl_sunday("2026-10-24", "2026-10-26") == 1
    # Fri 10/23 -> Tue 10/27 = 3d (Sat, Mon, Tue)
    assert db_manager.calculate_business_days_excl_sunday("2026-10-23", "2026-10-27") == 3
    # Thu 10/22 -> Tue 10/27 = 4d (> 3d cooldown expired)
    assert db_manager.calculate_business_days_excl_sunday("2026-10-22", "2026-10-27") == 4


def test_d99_cooldown_rules_and_open_mismatches(tmp_path):
    db_file = tmp_path / "test_d99.db"
    db_manager.init_database(db_file)

    # Create prior audit run on 2026-10-28 with:
    # 1) SMT 9942: Matched (is_discrepancy=0), barcodes ["BC1", "BC2"] -> unchanged on 10/29 => EXCLUDED (D99-1)
    # 2) SMT 9943: Matched (is_discrepancy=0), barcodes ["BC3"] -> changed on 10/29 to ["BC3", "BC4"] => RETURNED (D99-1)
    # 3) SMT 9944: Mismatched (is_discrepancy=1), barcodes ["BC5", "BC6"], book_qty_at_count=5, effective_book_qty=4, counted_qty=1 -> unchanged on 10/29 => EXCLUDED + Open_Mismatches (Diff=-3) (D99-2)
    # 4) SMT 9945: Mismatched (is_discrepancy=1), barcodes ["BC7", "BC8"] -> changed on 10/29 to ["BC7"] => RETURNED (D99-2)
    # 5) CRT 05: Timing diff excluded (timing_diff_excluded=1, is_discrepancy_vs_label=1) -> treated as MATCH (D99)
    top_28 = [
        {"BIN": "SMT 9942", "Parts_List": "TEST-1001", "Label_QTY": 2, "Risk_Score": 180, "WMS_Barcodes": ["BC1", "BC2"]},
        {"BIN": "SMT 9943", "Parts_List": "TEST-1002", "Label_QTY": 1, "Risk_Score": 175, "WMS_Barcodes": ["BC3"]},
        {"BIN": "SMT 9944", "Parts_List": "TEST-1003", "Label_QTY": 5, "Risk_Score": 170, "WMS_Barcodes": ["BC5", "BC6"]},
        {"BIN": "SMT 9945", "Parts_List": "TEST-1004", "Label_QTY": 2, "Risk_Score": 165, "WMS_Barcodes": ["BC7", "BC8"]},
        {"BIN": "CRT 05", "Parts_List": "TEST-1005", "Label_QTY": 1, "Risk_Score": 160, "WMS_Barcodes": ["BC9"]},
    ]
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261028_DAILY", "run_date": "2026-10-28", "mode": "Daily"},
        top_bins=top_28,
        db_path=db_file
    )

    conn = db_manager.get_connection(db_file)
    cur = conn.cursor()
    results_28 = [
        ("20261028_DAILY_001_SMT 9942", "SMT 9942", "TEST-1001", 2, 2, 2, 0, 0),
        ("20261028_DAILY_002_SMT 9943", "SMT 9943", "TEST-1002", 1, 1, 1, 0, 0),
        ("20261028_DAILY_003_SMT 9944", "SMT 9944", "TEST-1003", 5, 4, 1, 1, 0),
        ("20261028_DAILY_004_SMT 9945", "SMT 9945", "TEST-1004", 2, 2, 1, 1, 0),
        ("20261028_DAILY_005_CRT 05", "CRT 05", "TEST-1005", 1, 0, 0, 0, 1),
    ]
    for aid, bname, mat, lq, eff_q, cq, is_d, t_ex in results_28:
        cur.execute("""
            INSERT INTO AUDIT_RESULT (
                assignment_id, run_id, bin, mat_no, counted_at,
                book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
                timing_diff_excluded, data_origin, is_confirmed, created_at
            ) VALUES (?, '20261028_DAILY', ?, ?, '2026-10-28 08:30:20', ?, ?, ?, ?, ?, ?, 'PHYSICAL', 1, '2026-10-28 11:00:00')
        """, (aid, bname, mat, lq, eff_q, cq, cq - eff_q, is_d, t_ex))
    conn.commit()
    conn.close()

    cd_map = db_manager.get_audit_cooldown_history("2026-10-29", max_business_days=3, db_path=db_file)
    assert set(cd_map.keys()) == {"SMT 9942", "SMT 9943", "SMT 9944", "SMT 9945", "CRT 05"}
    assert cd_map["CRT 05"]["is_discrepancy"] == 0  # Timing diff treated as match
    assert cd_map["SMT 9944"]["label_qty"] == 4.0  # effective_book_qty used

    # Build synthetic stock df for 2026-10-29
    rows = [
        # SMT 9942 unchanged
        {"Barcode": "BC1", "Mat.No": "TEST-1001", "Mat.Desc": "PART 1", "BIN": "SMT 9942", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        {"Barcode": "BC2", "Mat.No": "TEST-1001", "Mat.Desc": "PART 1", "BIN": "SMT 9942", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        # SMT 9943 changed (+BC4)
        {"Barcode": "BC3", "Mat.No": "TEST-1002", "Mat.Desc": "PART 2", "BIN": "SMT 9943", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        {"Barcode": "BC4", "Mat.No": "TEST-1002", "Mat.Desc": "PART 2", "BIN": "SMT 9943", "Qty.": 10, "GR Date": "20261028", "Lot.No": "261028", "GR_Date": pd.to_datetime("2026-10-28").date(), "Lot_Date": pd.to_datetime("2026-10-28").date(), "Warehouse_Dwell_Days": 1, "Bin_Dwell_Days": 1},
        # SMT 9944 unchanged (open mismatch)
        {"Barcode": "BC5", "Mat.No": "TEST-1003", "Mat.Desc": "PART 3", "BIN": "SMT 9944", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        {"Barcode": "BC6", "Mat.No": "TEST-1003", "Mat.Desc": "PART 3", "BIN": "SMT 9944", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        # SMT 9945 changed (-BC8)
        {"Barcode": "BC7", "Mat.No": "TEST-1004", "Mat.Desc": "PART 4", "BIN": "SMT 9945", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
        # CRT 05 unchanged
        {"Barcode": "BC9", "Mat.No": "TEST-1005", "Mat.Desc": "PART 5", "BIN": "CRT 05", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 58, "Bin_Dwell_Days": 10},
    ]
    df_stock = pd.DataFrame(rows)

    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    res = tracker.detect_mis_scans(df_stock, mode="Daily", run_date="2026-10-29")

    selected_top_ext = {b["BIN"] for b in res["top_bins"] + res["extended_bins"]}
    # SMT 9942, SMT 9944, CRT 05 must be excluded; SMT 9943 and SMT 9945 must be returned!
    assert "SMT 9942" not in selected_top_ext
    assert "SMT 9944" not in selected_top_ext
    assert "CRT 05" not in selected_top_ext
    assert "SMT 9943" in selected_top_ext
    assert "SMT 9945" in selected_top_ext

    # Open_Mismatches must contain SMT 9944 only (CRT 05 is timing diff = match, SMT 9945 changed)
    open_mm_bins = [m["Bin"] for m in res["open_mismatches"]]
    assert open_mm_bins == ["SMT 9944"]
    assert res["open_mismatches"][0]["Label QTY"] == 4
    assert res["open_mismatches"][0]["Actual QTY"] == 1
    assert res["open_mismatches"][0]["Diff QTY"] == -3

    # Save ledger and verify AUDIT_FEATURE_SNAPSHOT records cooldown_excluded=1
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261029_DAILY", "run_date": "2026-10-29", "mode": "Daily"},
        top_bins=res["top_bins"],
        extended_bins=res["extended_bins"],
        suspects_df=res["suspects"],
        random_bins=res["random_bins"],
        db_path=db_file
    )
    conn = db_manager.get_connection(db_file)
    excl_rows = conn.execute(
        "SELECT DISTINCT bin, cooldown_excluded, cooldown_reason FROM AUDIT_FEATURE_SNAPSHOT WHERE run_id='20261029_DAILY' AND cooldown_excluded=1"
    ).fetchall()
    conn.close()
    excl_map = {r["bin"]: r["cooldown_reason"] for r in excl_rows}
    assert set(excl_map.keys()) == {"SMT 9942", "SMT 9944", "CRT 05"}
    assert "COOLDOWN_OPEN_MISMATCH" in excl_map["SMT 9944"]
    assert "COOLDOWN_MATCH" in excl_map["SMT 9942"]
    assert "COOLDOWN_MATCH" in excl_map["CRT 05"]



def test_203_open_mismatches_inplace_pcs_adjustment_and_adjusted_status(tmp_path):
    """
    Thread #203 regression test:
    1) Bin with identical box barcodes on D+1 whose total PCS changed in-place
       (e.g., CRT 9926: 4 boxes, 24 pcs -> 4 boxes, 64 pcs; 57Z 1: 6 boxes, 24,000 -> 23,500 pcs)
       must NOT remain in Open_Mismatches and must be logged as PCS adjusted in cooldown_returned_details.
    2) Bin whose barcodes and PCS remain identical AND has wms_adjustment_status='NOT_ADJUSTED'
       remains in Open_Mismatches.
    3) Bin whose wms_adjustment_status='ADJUSTED' while barcodes/PCS match is excluded via
       COOLDOWN_ADJUSTED_MATCH and NOT placed in Open_Mismatches.
    """
    db_file = tmp_path / "test_203_pcs.db"
    db_manager.init_database(db_file)

    top_29 = [
        {"BIN": "CRT 9926", "Parts_List": "TEST-2031", "Label_QTY": 4, "Risk_Score": 160, "WMS_Barcodes": ["BC203_A1", "BC203_A2", "BC203_A3", "BC203_A4"]},
        {"BIN": "57Z 1", "Parts_List": "TEST-2032", "Label_QTY": 2, "Risk_Score": 155, "WMS_Barcodes": ["BC203_B1", "BC203_B2"]},
        {"BIN": "SMT 99", "Parts_List": "TEST-2033", "Label_QTY": 2, "Risk_Score": 150, "WMS_Barcodes": ["BC203_C1", "BC203_C2"]},
        {"BIN": "SMT 9951", "Parts_List": "TEST-2034", "Label_QTY": 2, "Risk_Score": 145, "WMS_Barcodes": ["BC203_D1", "BC203_D2"]},
    ]
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261029_DAILY", "run_date": "2026-10-29", "mode": "Daily"},
        top_bins=top_29,
        db_path=db_file,
    )

    conn = db_manager.get_connection(db_file)
    cur = conn.cursor()
    # Populate WMS_STOCK_DAILY_AGG for 2026-10-29 (mirroring morning snapshot ingestion)
    agg_rows = [
        ("2026-10-29", "CRT 9926", "TEST-2031", 4, 24.0, "2026-10-01", "2026-10-01", "2026-10-29 06:51:00"),
        ("2026-10-29", "57Z 1", "TEST-2032", 2, 8000.0, "2026-09-15", "2026-09-15", "2026-10-29 06:51:00"),
        ("2026-10-29", "SMT 99", "TEST-2033", 2, 20.0, "2026-09-15", "2026-09-15", "2026-10-29 06:51:00"),
        ("2026-10-29", "SMT 9951", "TEST-2034", 2, 20.0, "2026-09-15", "2026-09-15", "2026-10-29 06:51:00"),
    ]
    cur.executemany(
        """
        INSERT INTO WMS_STOCK_DAILY_AGG
        (snapshot_date, bin, mat_no, box_count, total_qty, oldest_gr_date, newest_gr_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        agg_rows,
    )
    results_29 = [
        # CRT 9926: PCS mismatch (counted_qty=None, is_discrepancy=1) -> adjusted in-place 24 -> 64 pcs
        ("20261029_DAILY_001_CRT 9926", "CRT 9926", "TEST-2031", 4, 4, None, None, 1, "NOT_ADJUSTED"),
        # 57Z 1: short 500 pcs -> adjusted in-place 8000 -> 7500 pcs
        ("20261029_DAILY_002_57Z 1", "57Z 1", "TEST-2032", 2, 2, 1, -1, 1, "NOT_ADJUSTED"),
        # SMT 99: truly unadjusted (same barcodes AND same 20 pcs)
        ("20261029_DAILY_003_SMT 99", "SMT 99", "TEST-2033", 2, 2, 1, -1, 1, "NOT_ADJUSTED"),
        # SMT 9951: explicitly marked ADJUSTED in DB
        ("20261029_DAILY_004_SMT 9951", "SMT 9951", "TEST-2034", 2, 2, 2, 0, 1, "ADJUSTED"),
    ]
    for aid, bname, mat, lq, eff_q, cq, dq, is_d, adj_st in results_29:
        cur.execute(
            """
            INSERT INTO AUDIT_RESULT (
                assignment_id, run_id, bin, mat_no, counted_at,
                book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
                timing_diff_excluded, wms_adjustment_status, data_origin, is_confirmed, created_at
            ) VALUES (?, '20261029_DAILY', ?, ?, '2026-10-29 07:30:00', ?, ?, ?, ?, ?, 0, ?, 'PHYSICAL', 1, '2026-10-29 09:00:00')
            """,
            (aid, bname, mat, lq, eff_q, cq, dq, is_d, adj_st),
        )
    conn.commit()
    conn.close()

    cd_map = db_manager.get_audit_cooldown_history("2026-10-30", max_business_days=3, db_path=db_file)
    assert cd_map["CRT 9926"]["audited_total_pcs"] == 24.0
    assert cd_map["57Z 1"]["audited_total_pcs"] == 8000.0
    assert cd_map["SMT 99"]["audited_total_pcs"] == 20.0
    assert cd_map["SMT 9951"]["wms_adjustment_status"] == "ADJUSTED"

    # Build 2026-10-30 WMS stock: same barcodes for all 4 bins, but PCS changed on CRT 9926 (64) & 57Z 1 (7500)
    rows_30 = [
        {"Barcode": "BC203_A1", "Mat.No": "TEST-2031", "Mat.Desc": "BRACKET A", "BIN": "CRT 9926", "Qty.": 16, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_A2", "Mat.No": "TEST-2031", "Mat.Desc": "BRACKET A", "BIN": "CRT 9926", "Qty.": 16, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_A3", "Mat.No": "TEST-2031", "Mat.Desc": "BRACKET A", "BIN": "CRT 9926", "Qty.": 16, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_A4", "Mat.No": "TEST-2031", "Mat.Desc": "BRACKET A", "BIN": "CRT 9926", "Qty.": 16, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_B1", "Mat.No": "TEST-2032", "Mat.Desc": "SCREW B", "BIN": "57Z 1", "Qty.": 4000, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_B2", "Mat.No": "TEST-2032", "Mat.Desc": "SCREW B", "BIN": "57Z 1", "Qty.": 3500, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_C1", "Mat.No": "TEST-2033", "Mat.Desc": "COVER C", "BIN": "SMT 99", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_C2", "Mat.No": "TEST-2033", "Mat.Desc": "COVER C", "BIN": "SMT 99", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_D1", "Mat.No": "TEST-2034", "Mat.Desc": "COVER D", "BIN": "SMT 9951", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
        {"Barcode": "BC203_D2", "Mat.No": "TEST-2034", "Mat.Desc": "COVER D", "BIN": "SMT 9951", "Qty.": 10, "GR Date": "20260901", "Lot.No": "260901", "GR_Date": pd.to_datetime("2026-09-01").date(), "Lot_Date": pd.to_datetime("2026-09-01").date(), "Warehouse_Dwell_Days": 59, "Bin_Dwell_Days": 12},
    ]
    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    res = tracker.detect_mis_scans(pd.DataFrame(rows_30), mode="Daily", run_date="2026-10-30")

    open_bins = [m["Bin"] for m in res["open_mismatches"]]
    assert open_bins == ["SMT 99"]

    returned_map = {r["BIN"]: r["Change"] for r in res["cooldown_returned_bins"]}
    assert "CRT 9926" in returned_map and "PCS adjusted (24->64 pcs, 4 boxes)" == returned_map["CRT 9926"]
    assert "57Z 1" in returned_map and "PCS adjusted (8000->7500 pcs, 2 boxes)" == returned_map["57Z 1"]

    excl_map = {r["BIN"]: r["Reason"] for r in res["cooldown_excluded_bins"]}
    assert "SMT 9951" in excl_map and "COOLDOWN_ADJUSTED_MATCH" in excl_map["SMT 9951"]
    assert "SMT 99" in excl_map and "COOLDOWN_OPEN_MISMATCH" in excl_map["SMT 99"]
