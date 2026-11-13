from unittest.mock import patch
import sys
from pathlib import Path
import pandas as pd
import openpyxl

WORKTREE_00 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKTREE_00))

import db_manager
import hit_rate_calculator
from box_aging_tracker import (
    BoxAgingTracker,
    parse_floor_buffer_prefixes,
    get_bin_characteristics,
)
from stock_verifier import StockVerifier


def test_b8_pcs_only_rows_counted_in_hit_rate_and_cooldown(tmp_path):
    """#187 B-8: PCS-counted rows (counted_qty=NULL, is_discrepancy=1) are counted, while NULL/NULL stays uncounted."""
    db_file = tmp_path / "test_b8.db"
    db_manager.init_database(db_file)

    top_bins = [
        {"BIN": "CRT 101", "Parts_List": "TEST-1001", "Label_QTY": 1, "Risk_Score": 150, "WMS_Barcodes": ["BC1"]},
        {"BIN": "SMT 102", "Parts_List": "TEST-1002", "Label_QTY": 2, "Risk_Score": 140, "WMS_Barcodes": ["BC2", "BC3"]},
        {"BIN": "SMT 103", "Parts_List": "TEST-1003", "Label_QTY": 3, "Risk_Score": 130, "WMS_Barcodes": ["BC4"]},
    ]
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261028_DAILY", "run_date": "2026-10-28", "mode": "Daily"},
        top_bins=top_bins,
        db_path=db_file,
    )

    conn = db_manager.get_connection(db_file)
    cur = conn.cursor()
    # 1) CRT 101: PCS-only mismatch -> counted_qty=NULL, is_discrepancy=1
    cur.execute(
        """
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at,
            book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
            timing_diff_excluded, data_origin, is_confirmed, created_at
        ) VALUES ('20261028_DAILY_001_CRT 101', '20261028_DAILY', 'CRT 101', 'TEST-1001',
                  '2026-10-28 08:10:00', 1, 1, NULL, NULL, 1, 0, 'PHYSICAL', 1, '2026-10-28 09:00:00')
        """
    )
    # 2) SMT 102: Normal match -> counted_qty=2, is_discrepancy=0
    cur.execute(
        """
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at,
            book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
            timing_diff_excluded, data_origin, is_confirmed, created_at
        ) VALUES ('20261028_DAILY_002_SMT 102', '20261028_DAILY', 'SMT 102', 'TEST-1002',
                  '2026-10-28 08:12:00', 2, 2, 2, 0, 0, 0, 'PHYSICAL', 1, '2026-10-28 09:00:00')
        """
    )
    # 3) SMT 103: Unaudited row -> counted_qty=NULL, is_discrepancy=NULL (must NOT be counted)
    cur.execute(
        """
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at,
            book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
            timing_diff_excluded, data_origin, is_confirmed, created_at
        ) VALUES ('20261028_DAILY_003_SMT 103', '20261028_DAILY', 'SMT 103', 'TEST-1003',
                  NULL, 3, 3, NULL, NULL, NULL, 0, 'PHYSICAL', 0, '2026-10-28 09:00:00')
        """
    )
    conn.commit()
    conn.close()

    metrics = hit_rate_calculator.compute_hit_rate_for_run("20261028_DAILY", db_path=db_file)
    assert metrics["sample_size_model"] == 2
    assert metrics["discrepancy_count_model"] == 1
    assert metrics["hit_rate_model"] == 0.5
    assert "1 uncounted bins" in metrics["notes"]

    cd_map = db_manager.get_audit_cooldown_history("2026-10-29", max_business_days=3, db_path=db_file)
    assert set(cd_map.keys()) == {"CRT 101", "SMT 102"}
    assert cd_map["CRT 101"]["counted_qty"] is None
    assert cd_map["CRT 101"]["is_discrepancy"] == 1

    # Ensure BoxAgingTracker D99 cooldown handles counted_qty=None without TypeError
    tracker = BoxAgingTracker()
    tracker.db_path = db_file
    df_next = pd.DataFrame([
        {
            "Barcode": "BC1",
            "Mat.No": "TEST-1001",
            "Mat.Desc": "HARNESS",
            "BIN": "CRT 101",
            "Qty.": 60,
            "GR_Date": pd.to_datetime("2026-10-01").date(),
            "Lot_Date": pd.to_datetime("2026-10-01").date(),
            "Warehouse_Dwell_Days": 28,
            "Bin_Dwell_Days": 5,
        }
    ])
    res = tracker.detect_mis_scans(df_next, mode="Daily", run_date="2026-10-29")
    assert any(om["Bin"] == "CRT 101" and om["Actual QTY"] == "PCS Mismatch" for om in res["open_mismatches"])


def test_188_4_floor_buffer_prefixes_and_stagnant_label():
    """#188 4: FLOOR_BUFFER_BIN_PREFIXES parses comma/semicolon cleanly and keeps SMT distinct from non-SMT floor."""
    all_pfxs, non_smt_pfxs, primary = parse_floor_buffer_prefixes("FLR, SMT ; GEN")
    assert "SMT" in all_pfxs and "FLR" in all_pfxs and "GEN" in all_pfxs
    assert "SMT" not in non_smt_pfxs and "SMD" not in non_smt_pfxs
    assert non_smt_pfxs[:2] == ("FLR", "GEN")
    assert primary == "FLR"

    c_smt = get_bin_characteristics("SMT 9947", floor_buffer_raw="FLR,SMT")
    assert c_smt["is_supermarket"] is True
    assert c_smt["is_oem_floor"] is False

    c_flr = get_bin_characteristics("FLR 155", floor_buffer_raw="FLR,SMT")
    assert c_flr["is_supermarket"] is True
    assert c_flr["is_oem_floor"] is True
    assert c_flr["tier"] is None


def test_188_5_total_pcs_scoped_and_cart_pcs_check(tmp_path):
    """#188 5: Total PCS populated ONLY for CRT*/PRO* + small-part bins; blank for other bins; Cart Action = Cart PCS Check."""
    tracker = BoxAgingTracker()
    tracker.cooldown_history = {}
    df = pd.DataFrame([
        # 1) CRT cart bin -> Total PCS = 60, Action = Cart PCS Check
        {
            "Barcode": "BC-CRT-1",
            "Mat.No": "TEST-2001",
            "Mat.Desc": "HOUSING ASSY",
            "BIN": "CRT 9926",
            "Qty.": 60,
            "GR_Date": pd.to_datetime("2026-10-01").date(),
            "Lot_Date": pd.to_datetime("2026-10-01").date(),
            "Warehouse_Dwell_Days": 28,
            "Bin_Dwell_Days": 5,
        },
        # 2) Small part Tier 1 bin (SCREW / 20-* / Qty >= 500) -> Total PCS = 3000
        {
            "Barcode": "BC-SM-1",
            "Mat.No": "20-9001",
            "Mat.Desc": "SCREW TAPPING",
            "BIN": "70Z 2",
            "Qty.": 1500,
            "GR_Date": pd.to_datetime("2026-10-01").date(),
            "Lot_Date": pd.to_datetime("2026-10-01").date(),
            "Warehouse_Dwell_Days": 28,
            "Bin_Dwell_Days": 10,
        },
        {
            "Barcode": "BC-SM-2",
            "Mat.No": "20-9001",
            "Mat.Desc": "SCREW TAPPING",
            "BIN": "70Z 2",
            "Qty.": 1500,
            "GR_Date": pd.to_datetime("2026-10-25").date(),
            "Lot_Date": pd.to_datetime("2026-10-25").date(),
            "Warehouse_Dwell_Days": 4,
            "Bin_Dwell_Days": 2,
        },
        # 3) Regular SMT bin (non-small part, Qty=40 < 500) -> Total PCS = ""
        {
            "Barcode": "BC-SMT-1",
            "Mat.No": "TEST-3001",
            "Mat.Desc": "BRACKET MAIN",
            "BIN": "SMT 9947",
            "Qty.": 40,
            "GR_Date": pd.to_datetime("2026-10-01").date(),
            "Lot_Date": pd.to_datetime("2026-10-01").date(),
            "Warehouse_Dwell_Days": 28,
            "Bin_Dwell_Days": 6,
        },
    ])
    res = tracker.detect_mis_scans(df, mode="Daily", random_slots=0, run_date="2026-10-29")
    bins_by_name = {b["BIN"]: b for b in (res["top_bins"] + res["extended_bins"])}
    assert bins_by_name["CRT 9926"]["Action_Required"] == "Cart PCS Check"
    assert bins_by_name["CRT 9926"]["Total_PCS"] == 60
    # Full bin sum (1500 + 1500 = 3000) even when 1 box is fresh!
    assert bins_by_name["70Z 2"]["Total_PCS"] == 3000
    assert bins_by_name["SMT 9947"]["Total_PCS"] == ""

    with patch("box_aging_tracker.get_daily_field_instructions", return_value=[]):
        xlsx_path = tracker.export_audit_excel(res, output_path=tmp_path / "audit.xlsx", mode="Daily", dry_run=True)
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb["Cycle_Count_Target_Bins"]
    hdr_row = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Rank")
    headers = [ws.cell(hdr_row, c).value for c in range(1, 7)]
    assert headers == ["Rank", "Bin #", "Part #", "Label QTY", "Total PCS", "Counted QTY"]
    excel_rows = {ws.cell(r, 2).value: ws.cell(r, 5).value for r in range(hdr_row + 1, ws.max_row + 1)}
    assert excel_rows["CRT 9926"] == 60
    assert excel_rows["70Z 2"] == 3000
    assert excel_rows["SMT 9947"] in (None, "")

    verifier = StockVerifier()
    html = verifier.generate_html_report({**res, "plant": "TEST_PLANT", "audit_excel_path": xlsx_path}, mode="Daily")
    assert "Total PCS" in html
    assert "Cart PCS Check" in html
    assert "3,000 pcs" in html
    assert "60 pcs" in html


def test_188_6_daily_slot_reduction_tracker(tmp_path):
    """#188 6: Rank-band (1-5 / 6-15 / 16-25 / 26-40 / RANDOM) tracker + >=10d/5d gate + Weekly check."""
    db_file = tmp_path / "test_188_6.db"
    db_manager.init_database(db_file)
    top_bins = [
        {"BIN": f"SMT {i:02d}", "Parts_List": f"TEST-{i:04d}", "Label_QTY": 2, "Risk_Score": 200 - i}
        for i in range(1, 31)
    ]
    rand_bins = [{"BIN": "74Z 1", "Parts_List": "TEST-9001", "Label_QTY": 1, "Risk_Score": 0}]
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261029_DAILY", "run_date": "2026-10-29", "mode": "Daily"},
        top_bins=top_bins[:15],
        extended_bins=top_bins[15:],
        random_bins=rand_bins,
        db_path=db_file,
    )
    conn = db_manager.get_connection(db_file)
    cur = conn.cursor()
    for rank_i in range(1, 31):
        bname = f"SMT {rank_i:02d}"
        aid = f"20261029_DAILY_{rank_i:03d}_{bname}"
        is_disc = 1 if rank_i <= 10 else 0
        cur.execute(
            """
            INSERT INTO AUDIT_RESULT (
                assignment_id, run_id, bin, mat_no, counted_at,
                book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
                timing_diff_excluded, data_origin, is_confirmed, created_at
            ) VALUES (?, '20261029_DAILY', ?, 'TEST-0001', '2026-10-29 08:00:00', 2, 2, ?, ?, ?, 0, 'PHYSICAL', 1, '2026-10-29 09:00:00')
            """,
            (aid, bname, 1 if is_disc else 2, -1 if is_disc else 0, is_disc),
        )
    cur.execute(
        """
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at,
            book_qty_at_count, effective_book_qty, counted_qty, diff_qty, is_discrepancy,
            timing_diff_excluded, data_origin, is_confirmed, created_at
        ) VALUES ('20261029_DAILY_031_74Z 1', '20261029_DAILY', '74Z 1', 'TEST-9001', '2026-10-29 08:00:00', 1, 1, 1, 0, 0, 0, 'PHYSICAL', 1, '2026-10-29 09:00:00')
        """
    )
    conn.commit()
    conn.close()

    tracker = hit_rate_calculator.compute_daily_slot_reduction_tracker(run_id="20261029_DAILY", db_path=db_file)
    assert tracker["n_daily_runs"] == 1
    assert tracker["proposal_ready"] is False
    assert tracker["target_run"]["bands"]["1-5"]["hit_rate"] == 1.0
    assert tracker["target_run"]["bands"]["6-15"]["hit_rate"] == 0.5
    assert tracker["target_run"]["bands"]["26-40"]["hit_rate"] == 0.0
    txt = hit_rate_calculator.format_slot_reduction_tracker_report(tracker)
    assert "데이터 축적 중" in txt
