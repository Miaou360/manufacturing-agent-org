import sys
import datetime
from pathlib import Path
import pandas as pd

WORKTREE_00 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE_00))

import db_manager
from box_aging_tracker import BoxAgingTracker


def test_d101_full_bin_label_qty_and_parts(tmp_path):
    """
    D101: Risk score selection uses suspect barcodes, while Label_QTY and Parts_List
    reflect ALL boxes and ALL materials present in that bin in the snapshot,
    and AUDIT_ASSIGNMENT stores label_qty_basis='ALL_BIN_BARCODES'.
    """
    db_file = tmp_path / "test_d101.db"
    db_manager.init_database(db_file)

    today = datetime.date.today()
    old_gr = today - datetime.timedelta(days=45)
    fresh_gr = today - datetime.timedelta(days=1)

    # Bin 51Z 1 has 2 old small-part suspect boxes (TEST-2001) + 2 fresh non-suspect boxes (TEST-2002)
    rows = [
        {
            "Barcode": "BC-SUSP-1", "Mat.No": "TEST-2001", "Mat.Desc": "SCREW ASSY A",
            "BIN": "51Z 1", "Qty.": 500, "GR Date": old_gr.strftime("%Y%m%d"),
            "Lot No": old_gr.strftime("%y%m%d"), "GR_Date": old_gr, "Lot_Date": old_gr,
            "Warehouse_Dwell_Days": 45, "Bin_Dwell_Days": 15
        },
        {
            "Barcode": "BC-SUSP-2", "Mat.No": "TEST-2001", "Mat.Desc": "SCREW ASSY A",
            "BIN": "51Z 1", "Qty.": 500, "GR Date": old_gr.strftime("%Y%m%d"),
            "Lot No": old_gr.strftime("%y%m%d"), "GR_Date": old_gr, "Lot_Date": old_gr,
            "Warehouse_Dwell_Days": 45, "Bin_Dwell_Days": 15
        },
        {
            "Barcode": "BC-NORM-3", "Mat.No": "TEST-2002", "Mat.Desc": "HOUSING MAIN",
            "BIN": "51Z 1", "Qty.": 10, "GR Date": fresh_gr.strftime("%Y%m%d"),
            "Lot No": fresh_gr.strftime("%y%m%d"), "GR_Date": fresh_gr, "Lot_Date": fresh_gr,
            "Warehouse_Dwell_Days": 1, "Bin_Dwell_Days": 1
        },
        {
            "Barcode": "BC-NORM-4", "Mat.No": "TEST-2002", "Mat.Desc": "HOUSING MAIN",
            "BIN": "51Z 1", "Qty.": 10, "GR Date": fresh_gr.strftime("%Y%m%d"),
            "Lot No": fresh_gr.strftime("%y%m%d"), "GR_Date": fresh_gr, "Lot_Date": fresh_gr,
            "Warehouse_Dwell_Days": 1, "Bin_Dwell_Days": 1
        },
    ]
    df_stock = pd.DataFrame(rows)

    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    tracker.cooldown_history = {}
    res = tracker.detect_mis_scans(df_stock, mode="Daily", run_date=today.strftime("%Y-%m-%d"))

    assert len(res["top_bins"]) == 1
    b_item = res["top_bins"][0]
    assert b_item["BIN"] == "51Z 1"
    # Suspect barcodes = 2, but D101 full-bin Label_QTY = 4 and Parts_List includes both TEST-2001 and TEST-2002
    assert b_item["Suspect_Barcode_Count"] == 2
    assert b_item["Label_QTY"] == 4
    assert b_item["Parts_List"] == "TEST-2001, TEST-2002"
    assert b_item["Label_Qty_Basis"] == "ALL_BIN_BARCODES"

    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "TEST_D101_DAILY", "run_date": today.strftime("%Y-%m-%d"), "mode": "Daily"},
        top_bins=res["top_bins"],
        extended_bins=res["extended_bins"],
        suspects_df=res["suspects"],
        random_bins=res["random_bins"],
        db_path=db_file
    )
    conn = db_manager.get_connection(db_file)
    row = conn.execute("SELECT label_qty, materials_json, label_qty_basis FROM AUDIT_ASSIGNMENT WHERE run_id='TEST_D101_DAILY' AND bin='51Z 1'").fetchone()
    conn.close()
    assert row["label_qty"] == 4
    assert row["label_qty_basis"] == "ALL_BIN_BARCODES"
    assert "TEST-2002" in row["materials_json"]


def test_d102_crt_pro_smt_bin_dwell_basis(tmp_path):
    """
    D102: CRT*, PRO*, SMT* bins evaluate stagnation/dwell using bin_dwell_days
    (elapsed time in bin) rather than warehouse GR_Date dwell (wh_dwell_days).
    - Freshly staged CRT 01 (wh_dwell=40d, bin_dwell=1d) does NOT trigger REPACK_STAGNANT or FIFO_INVERSION.
    - Stale CRT 02 (wh_dwell=15d, bin_dwell=3d) DOES trigger REPACK_STAGNANT (+40pt).
    - Freshly staged PRO 01 (wh_dwell=35d, bin_dwell=1d) does NOT trigger REPACK_STAGNANT.
    - Stale SMT 9942 (wh_dwell=20d, bin_dwell=4d) DOES trigger SMT_STAGNANT (+45pt), whereas fresh SMT 9943 (wh_dwell=20d, bin_dwell=1d) does not.
    """
    db_file = tmp_path / "test_d102.db"
    db_manager.init_database(db_file)

    today = datetime.date.today()
    old_lot = today - datetime.timedelta(days=40)
    new_lot = today - datetime.timedelta(days=2)

    rows = [
        # Latest lot reference in rack so old_lot has a >30d lot gap
        {
            "Barcode": "BC-REF-NEW", "Mat.No": "TEST-3001", "Mat.Desc": "BRACKET X",
            "BIN": "R01A 3", "Qty.": 20, "GR Date": new_lot.strftime("%Y%m%d"),
            "Lot No": new_lot.strftime("%y%m%d"), "GR_Date": new_lot, "Lot_Date": new_lot,
            "Warehouse_Dwell_Days": 2, "Bin_Dwell_Days": 2
        },
        # CRT 01: wh_dwell=40d, bin_dwell=1d (just entered cart 2h ago) -> should NOT be suspect
        {
            "Barcode": "BC-CRT-FRESH", "Mat.No": "TEST-3001", "Mat.Desc": "BRACKET X",
            "BIN": "CRT 01", "Qty.": 20, "GR Date": old_lot.strftime("%Y%m%d"),
            "Lot No": old_lot.strftime("%y%m%d"), "GR_Date": old_lot, "Lot_Date": old_lot,
            "Warehouse_Dwell_Days": 40, "Bin_Dwell_Days": 1
        },
        # CRT 02: wh_dwell=15d, bin_dwell=3d (lingering in cart 3 days) -> REPACK_STAGNANT
        {
            "Barcode": "BC-CRT-STALE", "Mat.No": "TEST-3002", "Mat.Desc": "BRACKET Y",
            "BIN": "CRT 02", "Qty.": 20, "GR Date": (today - datetime.timedelta(days=15)).strftime("%Y%m%d"),
            "Lot No": (today - datetime.timedelta(days=15)).strftime("%y%m%d"),
            "GR_Date": today - datetime.timedelta(days=15), "Lot_Date": today - datetime.timedelta(days=15),
            "Warehouse_Dwell_Days": 15, "Bin_Dwell_Days": 3
        },
        # PRO 01: wh_dwell=35d, bin_dwell=1d -> should NOT be suspect
        {
            "Barcode": "BC-PRO-FRESH", "Mat.No": "TEST-3003", "Mat.Desc": "BRACKET Z",
            "BIN": "PRO 01", "Qty.": 20, "GR Date": old_lot.strftime("%Y%m%d"),
            "Lot No": old_lot.strftime("%y%m%d"), "GR_Date": old_lot, "Lot_Date": old_lot,
            "Warehouse_Dwell_Days": 35, "Bin_Dwell_Days": 1
        },
        # SMT 9942: wh_dwell=20d, bin_dwell=4d -> SMT_STAGNANT
        {
            "Barcode": "BC-SMT-STALE", "Mat.No": "TEST-3004", "Mat.Desc": "MODULE A",
            "BIN": "SMT 9942", "Qty.": 20, "GR Date": (today - datetime.timedelta(days=20)).strftime("%Y%m%d"),
            "Lot No": (today - datetime.timedelta(days=20)).strftime("%y%m%d"),
            "GR_Date": today - datetime.timedelta(days=20), "Lot_Date": today - datetime.timedelta(days=20),
            "Warehouse_Dwell_Days": 20, "Bin_Dwell_Days": 4
        },
        # SMT 9943: wh_dwell=20d, bin_dwell=1d -> should NOT be suspect
        {
            "Barcode": "BC-SMT-FRESH", "Mat.No": "TEST-3004", "Mat.Desc": "MODULE A",
            "BIN": "SMT 9943", "Qty.": 20, "GR Date": (today - datetime.timedelta(days=20)).strftime("%Y%m%d"),
            "Lot No": (today - datetime.timedelta(days=20)).strftime("%y%m%d"),
            "GR_Date": today - datetime.timedelta(days=20), "Lot_Date": today - datetime.timedelta(days=20),
            "Warehouse_Dwell_Days": 20, "Bin_Dwell_Days": 1
        },
    ]
    df_stock = pd.DataFrame(rows)

    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    tracker.cooldown_history = {}
    res = tracker.detect_mis_scans(df_stock, mode="Daily", run_date=today.strftime("%Y-%m-%d"))

    selected_bins = {b["BIN"]: b for b in res["top_bins"] + res["extended_bins"]}
    assert "CRT 01" not in selected_bins
    assert "PRO 01" not in selected_bins
    assert "SMT 9943" not in selected_bins
    assert "CRT 02" in selected_bins
    assert "SMT 9942" in selected_bins
    assert selected_bins["CRT 02"]["Risk_Score"] >= 40
    assert selected_bins["SMT 9942"]["Risk_Score"] >= 45


def test_d102_ghost_stock_exempt_from_bin_dwell(tmp_path):
    """
    Thread 152: Ghost/Missing GI deficit score (CRITICAL_GHOST_SUSPECT) must be exempt from D102
    so that a box in SMT/CRT/PRO with bin_dwell_days=1 still retains its CRITICAL_GHOST_SUSPECT
    points (+65pt) while only dwell/stagnation rules use bin_dwell_days.
    """
    db_file = tmp_path / "test_d102_ghost.db"
    db_manager.init_database(db_file)

    today = datetime.date.today()
    gr_13d = today - datetime.timedelta(days=13)

    rows = [
        {
            "Barcode": "BC-GHOST-SMT", "Mat.No": "TEST-4001", "Mat.Desc": "SENSOR ASSY",
            "BIN": "SMT 9944", "Qty.": 24, "GR Date": gr_13d.strftime("%Y%m%d"),
            "Lot No": gr_13d.strftime("%y%m%d"), "GR_Date": gr_13d, "Lot_Date": gr_13d,
            "Warehouse_Dwell_Days": 13, "Bin_Dwell_Days": 1
        }
    ]
    df_stock = pd.DataFrame(rows)

    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    tracker.cooldown_history = {}
    tracker.ontology = {
        "TEST-4001": {"turnover_profile": {"trimmed_mean_days": 8.5, "mover_class": "NORMAL"}}
    }
    tracker.missing_gi_map = {
        "TEST-4001": {
            "diff_flag": "UNDER",
            "diff_box": 8.0,
            "anomaly_diff_box": 0.0,
            "is_unit_offset_item": False,
            "target_bins": "5A 1"
        }
    }
    tracker.chronic_feed = {
        "TEST-4001": {
            "current_diff_qty": -192.0,
            "consecutive_days": 5,
            "current_financial_exposure": 1500.0
        }
    }

    res = tracker.detect_mis_scans(df_stock, mode="Daily", run_date=today.strftime("%Y-%m-%d"))
    assert len(res["top_bins"]) == 1
    b_item = res["top_bins"][0]
    assert b_item["BIN"] == "SMT 9944"
    # CRITICAL_GHOST_SUSPECT (65) + SMT_SUPERMARKET_BUFFER (10) + CHRONIC_SYSTEM_MISMATCH (30) = 105
    assert b_item["Risk_Score"] == 105
    bd = res["suspects"].iloc[0]["Score_Breakdown"]
    assert bd.get("CRITICAL_GHOST_SUSPECT") == 65
    assert bd.get("SMT_SUPERMARKET_BUFFER") == 10
    assert "SMT_STAGNANT" not in bd
