"""
Tests for D112 (#209 / #210): R1-only English "Field Instructions" in 00_JOB Daily Cycle Count.
"""

from pathlib import Path
import sys
import openpyxl
import pandas as pd
import pytest

JOB_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = JOB_DIR.parent
for p in (str(JOB_DIR), str(DEPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import db_manager
from box_aging_tracker import BoxAgingTracker
from stock_verifier import StockVerifier
from audit_result_collector import ingest_audit_results_from_excel
from report_labels import assert_no_hangul


def _seed_run_and_results(conn, run_id: str, run_date: str, mode: str, rows: list):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at, issued)
        VALUES (?, ?, ?, 'v1.0_rules', ?, 1)
        """,
        (run_id, run_date, mode, f"{run_date} 06:55:00"),
    )
    for idx, r in enumerate(rows, 1):
        aid = f"{run_id}_{idx:03d}_{r['bin']}"
        cur.execute(
            """
            INSERT INTO AUDIT_ASSIGNMENT (
                assignment_id, run_id, bin, rank, list_type, selection_method,
                bin_risk_score, action_required, primary_reason, materials_json, label_qty, plant, tier, created_at
            ) VALUES (?, ?, ?, ?, 'TOP', 'RULE_SCORE', 80.0, 'Ghost Stock Audit', 'Test', '["PART-A101"]', ?, 'Plant 1', 1, ?)
            """,
            (aid, run_id, r["bin"], idx, r["label_qty"], f"{run_date} 06:55:00"),
        )
        if r.get("has_result", True):
            cur.execute(
                """
                INSERT INTO AUDIT_RESULT (
                    assignment_id, run_id, bin, mat_no, counted_at,
                    book_qty_at_count, effective_book_qty, counted_qty, diff_qty,
                    cause_code, is_confirmed, data_origin, is_discrepancy,
                    timing_diff_excluded, notes, field_notes, created_at
                ) VALUES (?, ?, ?, 'PART-A101', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    aid,
                    run_id,
                    r["bin"],
                    f"{run_date} 07:20:00",
                    float(r["label_qty"]),
                    float(r.get("eff_book", r["label_qty"])),
                    r.get("counted_qty"),
                    r.get("diff_qty"),
                    r.get("cause_code"),
                    r.get("data_origin", "PHYSICAL"),
                    r.get("is_discrepancy"),
                    r.get("timing_diff_excluded", 0),
                    r.get("notes"),
                    r.get("field_notes"),
                    f"{run_date} 08:00:00",
                ),
            )
    conn.commit()


def test_r1_repeat_short_detection_sorting_and_cap(tmp_path):
    db_file = tmp_path / "d112_r1.db"
    db_manager.init_database(db_file)
    conn = db_manager.get_connection(db_file)
    try:
        # Older run outside last-3 window (10/27): OLD 01 short 2x on 10/26-10/27 should drop out once 10/28-10/30 complete
        _seed_run_and_results(
            conn,
            "20261027_DAILY",
            "2026-10-27",
            "Daily",
            [
                {"bin": "SMT 99", "label_qty": 10, "counted_qty": 2.0, "diff_qty": -8.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261028_DAILY",
            "2026-10-28",
            "Daily",
            [
                {"bin": "FLR 129", "label_qty": 21, "counted_qty": 12.0, "diff_qty": -9.0, "is_discrepancy": 1},
                {"bin": "SMT 9947", "label_qty": 15, "counted_qty": 14.0, "diff_qty": -1.0, "is_discrepancy": 1},
                {"bin": "SMT 9949", "label_qty": 52, "eff_book": 44.0, "counted_qty": 31.0, "diff_qty": -21.0, "is_discrepancy": 1},
                {"bin": "SMT 99", "label_qty": 5, "counted_qty": 5.0, "diff_qty": 0.0, "is_discrepancy": 0},
                # Timing-excluded GI offset must NOT count as short
                {"bin": "CRT 9929", "label_qty": 22, "eff_book": 0.0, "counted_qty": 0.0, "diff_qty": -22.0, "is_discrepancy": 0, "timing_diff_excluded": 1},
                # Surplus (+boxes) must NOT count as short
                {"bin": "56Z 2", "label_qty": 5, "counted_qty": 6.0, "diff_qty": 1.0, "is_discrepancy": 1},
                # Extra bin #5 & #6 to verify max-5 cap & tie-break sorting
                {"bin": "71Z 1", "label_qty": 10, "counted_qty": 5.0, "diff_qty": -5.0, "is_discrepancy": 1},
                {"bin": "72Z 1", "label_qty": 10, "counted_qty": 8.0, "diff_qty": -2.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261029_DAILY",
            "2026-10-29",
            "Daily",
            [
                {"bin": "FLR 129", "label_qty": 16, "counted_qty": 11.0, "diff_qty": -5.0, "is_discrepancy": 1},
                {"bin": "SMT 9947", "label_qty": 10, "counted_qty": 8.0, "diff_qty": -2.0, "is_discrepancy": 1},
                {"bin": "60Z 1", "label_qty": 5, "counted_qty": 3.0, "diff_qty": -2.0, "is_discrepancy": 1},
                {"bin": "CRT 9929", "label_qty": 12, "counted_qty": 0.0, "diff_qty": -12.0, "is_discrepancy": 1},
                {"bin": "56Z 2", "label_qty": 5, "counted_qty": 7.0, "diff_qty": 2.0, "is_discrepancy": 1},
                # PCS surplus (+20 pcs extra loose) must NOT count as short
                {"bin": "SMT 9950", "label_qty": 7, "counted_qty": 7.0, "diff_qty": None, "is_discrepancy": 1, "field_notes": "box count OK, +20 pcs extra loose"},
                {"bin": "71Z 1", "label_qty": 9, "counted_qty": 3.0, "diff_qty": -6.0, "is_discrepancy": 1},
                {"bin": "72Z 1", "label_qty": 9, "counted_qty": 8.0, "diff_qty": -1.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261030_DAILY",
            "2026-10-30",
            "Daily",
            [
                {"bin": "FLR 129", "label_qty": 12, "counted_qty": 3.0, "diff_qty": -9.0, "is_discrepancy": 1},
                {"bin": "SMT 9947", "label_qty": 15, "counted_qty": 7.0, "diff_qty": -8.0, "is_discrepancy": 1},
                {"bin": "SMT 9949", "label_qty": 13, "counted_qty": 0.0, "diff_qty": -13.0, "is_discrepancy": 1},
                # PCS loose-pick shortage (box count OK, diff_qty=None, notes mark pcs short) -> qualifies 60Z 1 (2 of 3)!
                {"bin": "60Z 1", "label_qty": 3, "counted_qty": 3.0, "diff_qty": None, "is_discrepancy": 1, "field_notes": "box count OK, 100 pcs short (small parts loose taken)"},
                # One-off empty bin (R3) or one-off PCS short (R5) must NOT trigger R1
                {"bin": "CRT 9928", "label_qty": 12, "counted_qty": 0.0, "diff_qty": -12.0, "is_discrepancy": 1},
                {"bin": "75Z 1", "label_qty": 4, "counted_qty": 4.0, "diff_qty": None, "is_discrepancy": 1, "field_notes": "box count OK, qty short"},
                # UNAUDITED row (is_discrepancy=None) must be ignored (Rule 9)
                {"bin": "SMT 9950", "label_qty": 7, "counted_qty": None, "diff_qty": None, "is_discrepancy": None},
            ],
        )
        # Weekly run + uncounted future Daily run must NOT pollute the 3-Daily-run window
        _seed_run_and_results(
            conn,
            "20261031_WEEKLY",
            "2026-10-31",
            "Weekly",
            [{"bin": "R52Z 4", "label_qty": 8, "counted_qty": 4.0, "diff_qty": -4.0, "is_discrepancy": 1}],
        )
        _seed_run_and_results(
            conn,
            "20261101_DAILY",
            "2026-11-01",
            "Daily",
            [{"bin": "FLR 129", "label_qty": 3, "has_result": False}],
        )
    finally:
        conn.close()

    lines = db_manager.get_daily_field_instructions(target_date_str="2026-11-02", db_path=db_file)
    assert len(lines) == 5, f"Expected max 5 lines (out of 6 qualifying bins), got {len(lines)}: {lines}"
    assert_no_hangul({f"line_{i}": ln for i, ln in enumerate(lines)})

    # Order check:
    # 1) FLR 129 (k=3, latest_short=9)
    # 2) SMT 9947  (k=3, latest_short=8)
    # 3) SMT 9949  (k=2, latest_short=13)
    # 4) 71Z 1    (k=2, latest_short=6)
    # 5) 60Z 1   (k=2, latest_short=0.5) -- wait: 72Z 1 has latest_short=1.0 > 0.5, so 72Z 1 is #5 and 60Z 1 is #6 (capped)!
    assert lines[0] == (
        "FLR 129: short 3 of the last 3 counts (21→12, 16→11, 12→3). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time."
    )
    assert lines[1] == (
        "SMT 9947: short 3 of the last 3 counts (15→14, 10→8, 15→7). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time."
    )
    assert lines[2] == (
        "SMT 9949: short 2 of the last 3 counts (52→31, 13→0). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time."
    )
    assert lines[3].startswith("71Z 1: short 2 of the last 3 counts (10→5, 9→3).")
    assert lines[4].startswith("72Z 1: short 2 of the last 3 counts (10→8, 9→8).")

    # Requesting max_lines=10 reveals 60Z 1 at #6 with PCS short notation, and confirms CRT 9929 / 56Z 2 / SMT 9950 / 75Z 1 excluded
    all_lines = db_manager.get_daily_field_instructions(target_date_str="2026-11-02", max_lines=10, db_path=db_file)
    assert len(all_lines) == 6
    assert all_lines[5] == (
        "60Z 1: short 2 of the last 3 counts (5→3 boxes; 3 boxes, 100 pcs short). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time."
    )


def test_html_excel_placement_omission_and_collector_roundtrip(tmp_path):
    db_file = tmp_path / "d112_render.db"
    db_manager.init_database(db_file)

    tracker = BoxAgingTracker()
    tracker.db_path = db_file

    df = pd.DataFrame(
        [
            {
                "Barcode": "BC-101",
                "Mat.No": "PART-A101",
                "Mat.Desc": "BRACKET ASSY",
                "BIN": "SMT 9947",
                "Qty.": 100,
                "GR Date": "20261001",
                "GR_Date": pd.to_datetime("2026-10-01").date(),
                "Warehouse_Dwell_Days": 30,
                "Lot No": "20261001",
                "Lot_Date": pd.to_datetime("2026-10-01").date(),
                "Bin_Dwell_Days": 10,
                "GI Loc": "PRODUCTION",
            },
            {
                "Barcode": "BC-201",
                "Mat.No": "PART-B202",
                "Mat.Desc": "COVER PANEL",
                "BIN": "R52Z 4",
                "Qty.": 50,
                "GR Date": "20260901",
                "GR_Date": pd.to_datetime("2026-09-01").date(),
                "Warehouse_Dwell_Days": 120,
                "Lot No": "20260701",
                "Lot_Date": pd.to_datetime("2026-07-01").date(),
                "Bin_Dwell_Days": 120,
                "GI Loc": "CKD",
            },
        ]
    )

    # 1. Empty DB -> Field Instructions omitted in Daily HTML & Excel (Row 1 is Rank)
    res_empty = tracker.detect_mis_scans(df, mode="Daily", random_slots=0, run_date="2026-11-02")
    assert res_empty["field_instructions"] == []
    xlsx_empty = tracker.export_audit_excel(res_empty, output_path=tmp_path / "empty_daily.xlsx", mode="Daily", dry_run=True)
    wb_empty = openpyxl.load_workbook(xlsx_empty)
    assert wb_empty["Cycle_Count_Target_Bins"].cell(1, 1).value == "Rank"

    verifier = StockVerifier()
    html_empty = verifier.generate_html_report({**res_empty, "plant": "PLANT_MAIN", "audit_excel_path": xlsx_empty}, mode="Daily")
    assert "Field Instructions" not in html_empty
    assert_no_hangul({"daily_empty_html": html_empty}, attachments=[xlsx_empty])

    # 2. Seed 2 shortages for SMT 9947 -> Daily includes Field Instructions at top of HTML & Excel; Weekly still omits
    conn = db_manager.get_connection(db_file)
    try:
        _seed_run_and_results(
            conn,
            "20261029_DAILY",
            "2026-10-29",
            "Daily",
            [{"bin": "SMT 9947", "label_qty": 10, "counted_qty": 8.0, "diff_qty": -2.0, "is_discrepancy": 1}],
        )
        _seed_run_and_results(
            conn,
            "20261030_DAILY",
            "2026-10-30",
            "Daily",
            [{"bin": "SMT 9947", "label_qty": 15, "counted_qty": 7.0, "diff_qty": -8.0, "is_discrepancy": 1}],
        )
    finally:
        conn.close()

    res_daily = tracker.detect_mis_scans(df, mode="Daily", random_slots=0, run_date="2026-11-02")
    assert len(res_daily["field_instructions"]) == 1
    xlsx_daily = tracker.export_audit_excel(res_daily, output_path=tmp_path / "with_fi_daily.xlsx", mode="Daily", dry_run=True)
    wb_daily = openpyxl.load_workbook(xlsx_daily)
    ws_daily = wb_daily["Cycle_Count_Target_Bins"]
    assert ws_daily.cell(1, 1).value == "Field Instructions"
    assert "SMT 9947: short 2 of the last 3 counts (10→8, 15→7)." in str(ws_daily.cell(2, 1).value)
    assert ws_daily.cell(1, 1).font.size == 14 and ws_daily.cell(1, 1).font.bold is True
    assert ws_daily.cell(2, 1).font.size == 14 and ws_daily.cell(2, 1).font.bold is True
    assert ws_daily.page_setup.fitToWidth == 1
    assert ws_daily.page_setup.fitToHeight == 1
    assert ws_daily.page_setup.orientation == "portrait"
    assert ws_daily.cell(3, 1).value == "Rank"
    assert ws_daily.cell(4, 2).value == "SMT 9947"

    html_daily = verifier.generate_html_report({**res_daily, "plant": "PLANT_MAIN", "audit_excel_path": xlsx_daily}, mode="Daily")
    assert "Field Instructions" in html_daily
    assert "font-size: 20px; font-weight: 800;" in html_daily
    assert "font-size: 16px; font-weight: 700;" in html_daily
    assert html_daily.index("Field Instructions") < html_daily.index("Schedule Notice")
    assert html_daily.index("Field Instructions") < html_daily.index("<table")
    assert_no_hangul({"daily_fi_html": html_daily}, attachments=[xlsx_daily])

    # Weekly must NEVER include Field Instructions
    res_weekly = tracker.detect_mis_scans(df, mode="Weekly", random_slots=1, run_date="2026-11-02")
    assert res_weekly["field_instructions"] == []
    xlsx_weekly = tracker.export_audit_excel(res_weekly, output_path=tmp_path / "weekly.xlsx", mode="Weekly", dry_run=True)
    wb_weekly = openpyxl.load_workbook(xlsx_weekly)
    assert wb_weekly["Cycle_Count_Target_Bins"].cell(1, 1).value == "Rank"
    html_weekly = verifier.generate_html_report({**res_weekly, "plant": "PLANT_MAIN", "audit_excel_path": xlsx_weekly}, mode="Weekly")
    assert "Field Instructions" not in html_weekly
    assert_no_hangul({"weekly_html": html_weekly}, attachments=[xlsx_weekly])

    # 3. Verify Excel feedback sheet ingestion auto-detects header row below Field Instructions
    db_manager.save_audit_ledger_and_features(
        run_info={"run_id": "20261102_DAILY", "run_date": "2026-11-02", "mode": "Daily"},
        top_bins=res_daily["top_bins"],
        extended_bins=res_daily["extended_bins"],
        random_bins=[],
        db_path=db_file,
    )
    ws_daily.cell(4, 6, value=1)  # Counted QTY = 1 in row 4 (below Row 1 title, Row 2 bullet, Row 3 header)
    wb_daily.save(xlsx_daily)

    ingest_res = ingest_audit_results_from_excel(xlsx_daily, db_path=db_file, run_id="20261102_DAILY")
    assert ingest_res["ingested_count"] == 1
    assert ingest_res["errors"] == []



def test_b2_latest_match_immediately_excludes_resolved_bin(tmp_path):
    """#216 B-2: A bin that was short 2x earlier in the window but matched on its latest count (e.g. 70Z 2) is immediately excluded."""
    db_file = tmp_path / "d112_b2.db"
    db_manager.init_database(db_file)
    conn = db_manager.get_connection(db_file)
    try:
        _seed_run_and_results(
            conn,
            "20261027_DAILY",
            "2026-10-27",
            "Daily",
            [
                {"bin": "70Z 2", "label_qty": 8, "counted_qty": 6.0, "diff_qty": -2.0, "is_discrepancy": 1},
                {"bin": "SMT 9947", "label_qty": 19, "counted_qty": 18.0, "diff_qty": -1.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261028_DAILY",
            "2026-10-28",
            "Daily",
            [
                {"bin": "70Z 2", "label_qty": 6, "counted_qty": 5.0, "diff_qty": -1.0, "is_discrepancy": 1},
                {"bin": "SMT 9947", "label_qty": 15, "counted_qty": 14.0, "diff_qty": -1.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261029_DAILY",
            "2026-10-29",
            "Daily",
            [
                # 70Z 2 matched on 10/29 -> must be immediately excluded despite 2 shorts in last 3 runs!
                {"bin": "70Z 2", "label_qty": 3, "counted_qty": 3.0, "diff_qty": 0.0, "is_discrepancy": 0},
                {"bin": "SMT 9947", "label_qty": 10, "counted_qty": 8.0, "diff_qty": -2.0, "is_discrepancy": 1},
            ],
        )
    finally:
        conn.close()

    # After 10/28 (before 10/29 match), 70Z 2 WAS unresolved and showed up
    lines_1028 = db_manager.get_daily_field_instructions(
        target_date_str="2026-10-28", include_target_date=True, db_path=db_file
    )
    assert any(ln.startswith("70Z 2:") for ln in lines_1028)

    # After 10/29 (3->3 match), 70Z 2 is immediately excluded!
    lines_1029 = db_manager.get_daily_field_instructions(
        target_date_str="2026-10-29", include_target_date=True, db_path=db_file
    )
    assert not any(ln.startswith("70Z 2:") for ln in lines_1029)
    assert len(lines_1029) == 1
    assert lines_1029[0].startswith("SMT 9947: short 3 of the last 3 counts (19→18, 15→14, 10→8).")


def test_217_219_s5_crt_pro_only_excluded_and_s3_s4_fixed_last3_pre_sop(tmp_path):
    r"""#217 & #219: R1 auto-exclusion applies ONLY to S5 (CRT/PRO) & EXCLUDED (^S\d+); S3 & S4 (SMT/FLR pre-SOP) keep fixed last-3 window even if median_dwell_days < 1.0."""
    db_file = tmp_path / "d112_217_219.db"
    db_manager.init_database(db_file)
    conn = db_manager.get_connection(db_file)
    try:
        _seed_run_and_results(
            conn,
            "20261028_DAILY",
            "2026-10-28",
            "Daily",
            [
                {"bin": "CRT 9929", "label_qty": 12, "counted_qty": 0.0, "diff_qty": -12.0, "is_discrepancy": 1},
                {"bin": "PRO 12", "label_qty": 8, "counted_qty": 0.0, "diff_qty": -8.0, "is_discrepancy": 1},
                {"bin": "S51Z 5", "label_qty": 8, "counted_qty": 2.0, "diff_qty": -6.0, "is_discrepancy": 1},
                {"bin": "SMT 9949", "label_qty": 52, "counted_qty": 31.0, "diff_qty": -21.0, "is_discrepancy": 1},
                {"bin": "FLR 129", "label_qty": 21, "counted_qty": 12.0, "diff_qty": -9.0, "is_discrepancy": 1},
                {"bin": "60Z 1", "label_qty": 5, "counted_qty": 3.0, "diff_qty": -2.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261029_DAILY",
            "2026-10-29",
            "Daily",
            [
                {"bin": "CRT 9929", "label_qty": 10, "counted_qty": 0.0, "diff_qty": -10.0, "is_discrepancy": 1},
                {"bin": "PRO 12", "label_qty": 6, "counted_qty": 0.0, "diff_qty": -6.0, "is_discrepancy": 1},
                {"bin": "S51Z 5", "label_qty": 8, "counted_qty": 2.0, "diff_qty": -6.0, "is_discrepancy": 1},
                {"bin": "FLR 129", "label_qty": 16, "counted_qty": 11.0, "diff_qty": -5.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261030_DAILY",
            "2026-10-30",
            "Daily",
            [
                {"bin": "CRT 9929", "label_qty": 9, "counted_qty": 0.0, "diff_qty": -9.0, "is_discrepancy": 1},
                {"bin": "SMT 9949", "label_qty": 13, "counted_qty": 0.0, "diff_qty": -13.0, "is_discrepancy": 1},
                {"bin": "FLR 129", "label_qty": 12, "counted_qty": 3.0, "diff_qty": -9.0, "is_discrepancy": 1},
                {"bin": "60Z 1", "label_qty": 3, "counted_qty": 1.0, "diff_qty": -2.0, "is_discrepancy": 1},
            ],
        )
    finally:
        conn.close()

    # Pre-D114 fallback: CRT 9929, PRO 12 (S5) and S51Z 5 (EXCLUDED) are excluded; FLR 129, SMT 9949 (S4) & 60Z 1 (S3) qualify
    pre_lines = db_manager.get_daily_field_instructions(
        target_date_str="2026-10-30", include_target_date=True, db_path=db_file
    )
    assert len(pre_lines) == 3
    assert pre_lines[0].startswith("FLR 129:")
    assert pre_lines[1].startswith("SMT 9949:")
    assert pre_lines[2].startswith("60Z 1:")

    # Populate D114 ZONE_TURNOVER_STATS with noisy pre-SOP median < 1.0d on S4 (0.67d) and S3 (0.85d), and S5 = 0.22d:
    # Per #219, S3 & S4 MUST NOT be auto-excluded or window-shrunk; ONLY S5 uses ZONE_TURNOVER_STATS exclusion!
    conn = db_manager.get_connection(db_file)
    try:
        conn.executemany(
            """
            UPDATE ZONE_TURNOVER_STATS
            SET median_dwell_days = ?, window_days = ?, sample_n = ?, source = 'D114_SHADOW'
            WHERE zone_code = ?
            """,
            [
                (0.67, 0, 120, "S4"),
                (0.85, 0, 250, "S3"),
                (0.22, 0, 85, "S5"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    post_lines = db_manager.get_daily_field_instructions(
        target_date_str="2026-10-30", include_target_date=True, db_path=db_file
    )
    assert len(post_lines) == 3
    assert post_lines[0].startswith("FLR 129: short 3 of the last 3 counts")
    assert post_lines[1].startswith("SMT 9949: short 2 of the last 3 counts (52→31, 13→0).")
    assert post_lines[2].startswith("60Z 1: short 2 of the last 3 counts (5→3, 3→1).")


def test_219_s_rack_permanently_excluded_from_daily_weekly_and_random(tmp_path):
    r"""#219: ^S\d+ racks (e.g. S51Z 5, S1A 1) are permanently excluded from Daily, Weekly, Forced Weekly, Random, and Shadow EV pools."""
    db_file = tmp_path / "d112_srack.db"
    db_manager.init_database(db_file)
    tracker = BoxAgingTracker()
    tracker.db_path = db_file

    df = pd.DataFrame(
        [
            {
                "Barcode": "BC-S15",
                "Mat.No": "PART-S15",
                "Mat.Desc": "S RACK PART",
                "BIN": "S51Z 5",
                "Qty.": 200,
                "GR Date": "20260601",
                "GR_Date": pd.to_datetime("2026-06-01").date(),
                "Warehouse_Dwell_Days": 150,
                "Lot No": "20260601",
                "Lot_Date": pd.to_datetime("2026-06-01").date(),
                "Bin_Dwell_Days": 150,
                "GI Loc": "SL01",
            },
            {
                "Barcode": "BC-S1",
                "Mat.No": "PART-S15",
                "Mat.Desc": "S RACK LOW TIER",
                "BIN": "S1A 1",
                "Qty.": 500,
                "GR Date": "20260601",
                "GR_Date": pd.to_datetime("2026-06-01").date(),
                "Warehouse_Dwell_Days": 150,
                "Lot No": "20260601",
                "Lot_Date": pd.to_datetime("2026-06-01").date(),
                "Bin_Dwell_Days": 150,
                "GI Loc": "SL01",
            },
            {
                "Barcode": "BC-SMT",
                "Mat.No": "PART-SMT",
                "Mat.Desc": "NORMAL SMT PART",
                "BIN": "SMT 9947",
                "Qty.": 100,
                "GR Date": "20261001",
                "GR_Date": pd.to_datetime("2026-10-01").date(),
                "Warehouse_Dwell_Days": 30,
                "Lot No": "20261001",
                "Lot_Date": pd.to_datetime("2026-10-01").date(),
                "Bin_Dwell_Days": 10,
                "GI Loc": "SL01",
            },
            {
                "Barcode": "BC-R10",
                "Mat.No": "PART-R10",
                "Mat.Desc": "PLANT 3 HIGH RACK",
                "BIN": "R52Z 4",
                "Qty.": 80,
                "GR Date": "20260801",
                "GR_Date": pd.to_datetime("2026-08-01").date(),
                "Warehouse_Dwell_Days": 90,
                "Lot No": "20260801",
                "Lot_Date": pd.to_datetime("2026-08-01").date(),
                "Bin_Dwell_Days": 90,
                "GI Loc": "SL01",
            },
        ]
    )

    for mode in ("Daily", "Weekly"):
        res = tracker.detect_mis_scans(
            df,
            mode=mode,
            random_slots=5,
            run_date="2026-11-02",
            forced_weekly_spec={"enabled": True, "materials": ["PART-S15"], "slocs": ["SL01"], "action_required": "F-Check", "reason": "Test"},
        )
        for pool_key in ("top_bins", "extended_bins", "random_bins", "forced_weekly_bins", "shadow_ev_ranking"):
            bins_in_pool = {b["BIN"] for b in res.get(pool_key, [])}
            assert "S51Z 5" not in bins_in_pool, f"S51Z 5 leaked into {mode} {pool_key}"
            assert "S1A 1" not in bins_in_pool, f"S1A 1 leaked into {mode} {pool_key}"


def test_221_f1_pcs_short_explicit_known_and_unknown_qty_wording(tmp_path):
    """#221 F-1: Explicit box/pcs wording when box count matches and pcs is short (known qty vs unknown qty)."""
    db_file = tmp_path / "d112_221_f1.db"
    db_manager.init_database(db_file)
    conn = db_manager.get_connection(db_file)
    try:
        _seed_run_and_results(
            conn,
            "20261029_DAILY",
            "2026-10-29",
            "Daily",
            [
                {"bin": "60Z 1", "label_qty": 5, "counted_qty": 3.0, "diff_qty": -2.0, "is_discrepancy": 1},
                {"bin": "75Z 1", "label_qty": 5, "counted_qty": 3.0, "diff_qty": -2.0, "is_discrepancy": 1},
            ],
        )
        _seed_run_and_results(
            conn,
            "20261030_DAILY",
            "2026-10-30",
            "Daily",
            [
                {
                    "bin": "60Z 1",
                    "label_qty": 3,
                    "counted_qty": 3.0,
                    "diff_qty": None,
                    "is_discrepancy": 1,
                    "field_notes": "written 2@300 + 1@200; box count OK, 120 pcs short",
                },
                {
                    "bin": "75Z 1",
                    "label_qty": 3,
                    "counted_qty": 3.0,
                    "diff_qty": None,
                    "is_discrepancy": 1,
                    "field_notes": "box count OK, qty short",
                },
            ],
        )
    finally:
        conn.close()

    lines = db_manager.get_daily_field_instructions(target_date_str="2026-11-02", db_path=db_file)
    assert lines == [
        "60Z 1: short 2 of the last 3 counts (5→3 boxes; 3 boxes, 120 pcs short). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time.",
        "75Z 1: short 2 of the last 3 counts (5→3 boxes; 3 boxes but short in pcs). "
        "Please check boxes leaving this bin are scanned out (GI) at pick time.",
    ]
    assert_no_hangul({f"line_{i}": ln for i, ln in enumerate(lines)})

