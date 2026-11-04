"""
Unit Tests for D108 Shared GI Error Verdict & 02 Report-Only Correction Path (#183 / #184)
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

os.environ["M365_TEST_MODE"] = "1"

JOB02_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = JOB02_DIR.parent
for _p in (str(JOB02_DIR), str(DEPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gi_verdict_shared import evaluate_gi_errors_as_of, enrich_backlog_plan_with_d108
from notifier import _match_plan_row_for_cogi_item, build_email_html
from backlog_pipeline import run_report_only_as_of


def _seed_test_gi_db_and_plan(tmp_path: Path):
    gi_db = tmp_path / "gr_gi_history.db"
    gi_plan_dir = tmp_path / "gi_plans"
    gi_plan_dir.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(gi_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE pipeline_run_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                run_timestamp TEXT,
                plant TEXT,
                postings_executed INTEGER,
                elapsed_seconds REAL,
                status TEXT
            );
            CREATE TABLE gi_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                extracted_at TEXT,
                error_date TEXT,
                vendor TEXT,
                material TEXT,
                material_desc TEXT,
                mvt TEXT,
                from_sloc TEXT,
                to_sloc TEXT,
                qty REAL,
                message TEXT
            );
            CREATE TABLE gr_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                extracted_at TEXT,
                error_date TEXT,
                vendor TEXT,
                material TEXT,
                material_desc TEXT,
                mvt TEXT,
                qty REAL,
                message TEXT
            );
            CREATE TABLE transfer_posting_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                executed_at TEXT,
                material TEXT,
                from_sloc TEXT,
                to_sloc TEXT,
                transfer_qty REAL,
                mb1b_mvt TEXT,
                doc_number TEXT,
                post_status TEXT,
                posting_type TEXT,
                sap_response TEXT,
                reason TEXT
            );
            CREATE TABLE GI_STOCK_GAP_SIGNALS (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                observed_at TEXT,
                material TEXT,
                mvt TEXT,
                error_from TEXT,
                error_to TEXT,
                error_qty REAL,
                roundtrip_sloc TEXT,
                roundtrip_qty REAL,
                doc_no TEXT,
                sap_by_sloc TEXT,
                wms_by_sloc TEXT,
                sap_total REAL,
                wms_total REAL,
                total_gap REAL,
                signal_type TEXT,
                signal_date TEXT
            );
            """
        )
        # Note: run_timestamp stored in UTC so 'localtime' is deterministic via datetime('now') offset;
        # easiest deterministic UTC timestamp: compute from local target 2026-10-29 08:02:17
        utc_ts = datetime.fromtimestamp(datetime(2026, 10, 29, 8, 2, 17).timestamp()).astimezone(
            datetime.now().astimezone().tzinfo
        )
        # Convert local 2026-10-29 08:02:17 to UTC string for SQLite datetime(..., 'localtime')
        from datetime import timezone
        utc_0802 = datetime(2026, 10, 29, 8, 2, 17).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        utc_0800 = datetime(2026, 10, 29, 8, 0, 50).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, postings_executed, elapsed_seconds, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_080000", utc_0802, "TEST_PLT", 0, 90.0, "COMPLETED"),
        )
        gi_rows = [
            # 1. Pure shortage (D + D107 candidate)
            ("RUN_20261029_080000", utc_0800, "2026.10.27", "V1", "MAT_D", "Part D", "311", "SL02", "SP31", 115.0, "Insufficient stock"),
            # 2. Book discrepancy (A: WMS >= qty)
            ("RUN_20261029_080000", utc_0800, "2026.10.28", "V1", "MAT_A", "Part A", "311", "SL01", "SP31", 128.0, "Insufficient stock"),
            # 3. Missing GR suspect (F: SAP=0, WMS>0)
            ("RUN_20261029_080000", utc_0800, "2026.10.28", "V1", "MAT_F", "Part F", "311", "SL00", "SP31", 100.0, "Insufficient stock"),
            # 4. Active GR linked (B)
            ("RUN_20261029_080000", utc_0800, "2026.10.29", "V2", "MAT_B", "Part B", "311", "SL01", "SP31", 207.0, "Insufficient stock"),
            # 5. Anomaly ②: Guard unposted (D37 SL00)
            ("RUN_20261029_080000", utc_0800, "2026.10.28", "V1", "MAT_G", "Part G", "311", "SL00", "SP31", 600.0, "Insufficient stock"),
            # 6. Anomaly ③: Posting failed
            ("RUN_20261029_080000", utc_0800, "2026.10.28", "V1", "MAT_FAIL", "Part Fail", "311", "SL01", "SP31", 50.0, "Insufficient stock"),
            # 7. Excluded mvt 901
            ("RUN_20261029_080000", utc_0800, "2026.10.28", "V1", "MAT_901", "Part 901", "901", "SL03", "SL03", 500.0, "Order error"),
        ]
        conn.executemany(
            """
            INSERT INTO gi_error_snapshots
            (batch_id, extracted_at, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            gi_rows,
        )
        conn.execute(
            """
            INSERT INTO gr_error_snapshots
            (batch_id, extracted_at, error_date, vendor, material, material_desc, mvt, qty, message)
            VALUES (?, ?, '2026.10.28', '20405', 'MAT_B', 'Part B', '101', 300.0, '')
            """,
            ("RUN_20261029_080000", utc_0800),
        )
        conn.execute(
            """
            INSERT INTO transfer_posting_history
            (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, mb1b_mvt, doc_number, post_status, posting_type, sap_response, reason)
            VALUES ('RUN_20261029_080000', ?, 'MAT_FAIL', 'SL02', 'SL01', 50.0, '311', '', 'FAILED', 'DIRECT', 'M7 021 Deficit', 'Approved')
            """,
            (utc_0802,),
        )
        conn.commit()

    plan_df = pd.DataFrame(
        [
            {
                "Material": "MAT_D",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL02",
                "From_SLoc": "",
                "To_SLoc": "SL02",
                "Transfer_Qty": 115.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "전사 가용 잉여재고 부재",
                "SAP_By_SLoc": "{'SL01': 200.0, 'SP31': 100.0}",
                "WMS_By_SLoc": "{'SL01': 200.0}",
            },
            {
                "Material": "MAT_A",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SL01",
                "Transfer_Qty": 128.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "전사 가용 잉여재고 부재",
                "SAP_By_SLoc": "{'SL01': 125.0, 'SL02': 1408.0}",
                "WMS_By_SLoc": "{'SL01': 176.0, 'SL02': 1408.0}",
            },
            {
                "Material": "MAT_F",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL00",
                "From_SLoc": "",
                "To_SLoc": "SL00",
                "Transfer_Qty": 100.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "전사 가용 잉여재고 부재",
                "SAP_By_SLoc": "{}",
                "WMS_By_SLoc": "{'SL01': 100.0}",
            },
            {
                "Material": "MAT_B",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SL01",
                "Transfer_Qty": 207.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "전사 가용 잉여재고 부재",
                "SAP_By_SLoc": "{'SL00': 120.0, 'SL01': 120.0}",
                "WMS_By_SLoc": "{'SL00': 420.0, 'SL01': 120.0}",
            },
            {
                "Material": "MAT_G",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL00",
                "From_SLoc": "SL02",
                "To_SLoc": "SL00",
                "Transfer_Qty": 600.0,
                "Action": "SKIP",
                "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                "Reason": "SL00 입고 창고 도착 이동(SL02->SL00) 보류 (D37)",
                "SAP_By_SLoc": "{'SL02': 2000.0}",
                "WMS_By_SLoc": "{'SL00': 1400.0}",
            },
            {
                "Material": "MAT_FAIL",
                "Mvt_Original": 311,
                "Deficit_Loc": "SL01",
                "From_SLoc": "SL02",
                "To_SLoc": "SL01",
                "Transfer_Qty": 50.0,
                "Action": "TRANSFER_POSTING",
                "Status": "APPROVED_FULL_RESOLUTION",
                "Reason": "Approved",
                "SAP_By_SLoc": "{'SL02': 100.0}",
                "WMS_By_SLoc": "{'SL02': 0.0}",
            },
            {
                "Material": "MAT_901",
                "Mvt_Original": 901,
                "Deficit_Loc": "SL03",
                "From_SLoc": "",
                "To_SLoc": "SL03",
                "Transfer_Qty": 500.0,
                "Action": "SKIP",
                "Status": "EXCLUDED_MVT_901",
                "Reason": "이동유형 901 제외",
                "SAP_By_SLoc": "{}",
                "WMS_By_SLoc": "{}",
            },
        ]
    )
    gi_plan_file = gi_plan_dir / "GI_Resolution_Plan_20261029_080216.xlsx"
    plan_df.to_excel(gi_plan_file, index=False)
    return gi_db, gi_plan_dir


def test_d108_and_d107_all_verdict_branches(tmp_path: Path):
    gi_db, gi_plan_dir = _seed_test_gi_db_and_plan(tmp_path)

    # Include all 7 errors from 08:00 PLUS 1 brand-new error created at 08:12 (① NEW_AFTER_01)
    active_recs = [
        {"material": "MAT_D", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 115.0, "error_date": "2026.10.27"},
        {"material": "MAT_A", "mvt": "311", "from_sloc": "SL01", "to_sloc": "SP31", "qty": 128.0, "error_date": "2026.10.28"},
        {"material": "MAT_F", "mvt": "311", "from_sloc": "SL00", "to_sloc": "SP31", "qty": 100.0, "error_date": "2026.10.28"},
        {"material": "MAT_B", "mvt": "311", "from_sloc": "SL01", "to_sloc": "SP31", "qty": 207.0, "error_date": "2026.10.29"},
        {"material": "MAT_G", "mvt": "311", "from_sloc": "SL00", "to_sloc": "SP31", "qty": 600.0, "error_date": "2026.10.28"},
        {"material": "MAT_FAIL", "mvt": "311", "from_sloc": "SL01", "to_sloc": "SP31", "qty": 50.0, "error_date": "2026.10.28"},
        {"material": "MAT_901", "mvt": "901", "from_sloc": "SL03", "to_sloc": "SL03", "qty": 500.0, "error_date": "2026.10.28"},
        {"material": "MAT_NEW", "mvt": "311", "from_sloc": "SL01", "to_sloc": "SP31", "qty": 80.0, "error_date": "2026.10.29"},
    ]

    res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 08:17:00",
        gi_db_path=gi_db,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=tmp_path / "nonexistent.db",
        active_gi_records=active_recs,
    )
    by_mat = {v["material"]: v for v in res["verdicts"]}

    assert by_mat["MAT_D"]["d108_code"] == "D"
    assert by_mat["MAT_D"]["d107_status"] == "CANDIDATE"
    assert "D107 delete candidate" in by_mat["MAT_D"]["d108_label"]

    assert by_mat["MAT_A"]["d108_code"] == "A"
    assert by_mat["MAT_A"]["d107_status"] == "EXCLUDED"

    assert by_mat["MAT_F"]["d108_code"] == "F"
    assert by_mat["MAT_F"]["d107_status"] == "HOLD_HUMAN_REVIEW" or by_mat["MAT_F"]["d107_status"] == "EXCLUDED"

    assert by_mat["MAT_B"]["d108_code"] == "B"
    assert by_mat["MAT_B"]["d107_status"] == "EXCLUDED"

    assert by_mat["MAT_G"]["d108_code"] == "ANOMALY_GUARD_UNPOSTED"
    assert by_mat["MAT_G"]["d108_anomaly_type"] == "GUARD_UNPOSTED"
    assert by_mat["MAT_G"]["is_bug_candidate"] is True

    assert by_mat["MAT_FAIL"]["d108_code"] == "ANOMALY_POSTING_FAILED"
    assert by_mat["MAT_FAIL"]["is_bug_candidate"] is True

    assert by_mat["MAT_901"]["d108_code"] == "EXCLUDED_MVT"
    assert by_mat["MAT_901"]["d107_status"] == "EXCLUDED"

    assert by_mat["MAT_NEW"]["d108_code"] == "ANOMALY_NEW_AFTER_01"
    assert by_mat["MAT_NEW"]["is_bug_candidate"] is False


def test_d71_movement_block_preserved_and_multi_sloc_matching(tmp_path: Path):
    gi_db, gi_plan_dir = _seed_test_gi_db_and_plan(tmp_path)

    bl_df = pd.DataFrame(
        [
            {
                "Material": "MAT_A",
                "Material_Desc": "Part A",
                "Target_SLoc": "SP31",
                "Deficit_Qty": 222.0,
                "Error_Code": "M7 021",
                "Action": "SKIP",
                "Status": "BLOCKED_BY_GI_ERROR",
                "Pending_GI_Qty": 128.0,
                "Pending_GI_SLocs": "SP31",
                "Reason": "Blocked by D71",
            },
            {
                "Material": "MAT_A",
                "Material_Desc": "Part A",
                "Target_SLoc": "SL49",
                "Deficit_Qty": 17.0,
                "Error_Code": "M7 021",
                "Action": "SKIP",
                "Status": "WAREHOUSE_SAP_SHORT_PERSISTENT",
                "Pending_GI_Qty": 0.0,
                "Pending_GI_SLocs": "",
                "Reason": "SL49 shortage",
            },
        ]
    )

    enriched = enrich_backlog_plan_with_d108(
        bl_df,
        as_of="2026-10-29 08:17:00",
        gi_db_path=gi_db,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=tmp_path / "none.db",
    )
    # D71 block MUST stay intact
    assert enriched.loc[0, "Action"] == "SKIP"
    assert enriched.loc[0, "Status"] == "BLOCKED_BY_GI_ERROR"
    assert enriched.loc[0, "D108_Cause_Code"] == "A"

    # Multi-SLoc COGI matching: SL49 item must NOT inherit SP31's BLOCKED_BY_GI_ERROR
    m_sp31 = _match_plan_row_for_cogi_item(enriched, {"Material": "MAT_A", "SLoc": "SP31", "Qty": 222.0, "Error_Code": "M7 021"})
    m_sl49 = _match_plan_row_for_cogi_item(enriched, {"Material": "MAT_A", "SLoc": "SL49", "Qty": 11.0, "Error_Code": "M7 021"})
    assert m_sp31["Status"] == "BLOCKED_BY_GI_ERROR"
    assert m_sl49["Status"] == "WAREHOUSE_SAP_SHORT_PERSISTENT"
    assert m_sl49["Target_SLoc"] == "SL49"

    html = build_email_html(
        enriched,
        execution_results=[],
        run_date="2026-10-29",
        run_time="08:15 AM EST",
        final_cogi_items=[
            {"Material": "MAT_A", "SLoc": "SP31", "Qty": 222.0, "Error_Code": "M7 021"},
            {"Material": "MAT_A", "SLoc": "SL49", "Qty": 11.0, "Error_Code": "M7 021"},
        ],
        is_correction=True,
    )
    assert "[Correction] Updated root-cause verdicts for GI error-linked backlogs" in html
    assert "Book mismatch (A)" in html


def test_report_only_as_of_zero_db_write(tmp_path: Path):
    gi_db, gi_plan_dir = _seed_test_gi_db_and_plan(tmp_path)
    logs_dir = tmp_path / "logs"
    cogi_dir = tmp_path / "cogi"
    gi_err_dir = tmp_path / "gi_err"
    logs_dir.mkdir()
    cogi_dir.mkdir()
    gi_err_dir.mkdir()

    bl_db = tmp_path / "backlog_history.db"
    with sqlite3.connect(str(bl_db)) as conn:
        conn.executescript(
            """
            CREATE TABLE backlog_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT, run_date TEXT, run_time TEXT, executed_at TEXT, created_at TEXT,
                material TEXT, material_desc TEXT, from_sloc TEXT, to_sloc TEXT, transfer_qty REAL,
                header_text TEXT, sap_doc_no TEXT, sap_raw_msg TEXT, status TEXT
            );
            CREATE TABLE raw_backlog_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT, run_time TEXT, qty REAL
            );
            CREATE TABLE SAP_DEFICIT_UNRESOLVED_TRACKER (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                material TEXT, material_desc TEXT, storage_loc TEXT, run_date TEXT,
                sap_stock REAL, wms_stock REAL, deficit_qty REAL,
                accuracy_before REAL, accuracy_after REAL, recent_doc TEXT,
                first_detected_date TEXT, consecutive_days INTEGER, is_sl49 INTEGER, is_sustained_deficit INTEGER
            );
            CREATE TABLE daily_backlog_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT);
            """
        )
        conn.execute("INSERT INTO raw_backlog_history (run_date, run_time, qty) VALUES ('2026-10-29', '06:20', 500.0)")
        conn.commit()

    bl_plan_path = logs_dir / "Backlog_Resolution_Plan_20261029_081732.xlsx"
    pd.DataFrame(
        [
            {
                "Material": "MAT_G",
                "Material_Desc": "Part G",
                "Target_SLoc": "SP31",
                "Deficit_Qty": 641.0,
                "Error_Code": "M7 021",
                "Action": "SKIP",
                "Status": "BLOCKED_BY_GI_ERROR",
                "Pending_GI_Qty": 600.0,
                "Pending_GI_SLocs": "SP31",
                "Reason": "Blocked",
            }
        ]
    ).to_excel(bl_plan_path, index=False)

    res = run_report_only_as_of(
        as_of="20261029_0815",
        send_email=False,
        logs_dir=logs_dir,
        cogi_dir=cogi_dir,
        gi_dir=gi_err_dir,
        backlog_db_path=bl_db,
        gi_db_path=gi_db,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=tmp_path / "none.db",
    )
    assert res["mode"] == "REPORT_ONLY"
    assert res["gi_blocked_count"] == 1
    assert res["plan_df"].loc[0, "D108_Cause_Code"] == "ANOMALY_GUARD_UNPOSTED"

    # Verify daily_backlog_metrics remained untouched (0 rows)
    with sqlite3.connect(str(bl_db)) as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM daily_backlog_metrics").fetchone()[0]
    assert cnt == 0


def test_b1_three_file_gr_import_domestics_protection_and_multiset_dedup(tmp_path: Path):
    """
    #187 B-1: Reproduces SL00 WMS +300 = GR_Import (180 + 60 + 60 = 300 EA, 3 rows) + GR_Domestics + GR_Error.
    Verifies:
      1. Multiset reconciliation preserves 60 + 60 + 180 = 300 EA (3 rows) without collapsing identical
         60 EA boxes to 240 EA and without double-counting to 600 EA when GR_Error also contains them.
      2. Even when GR_Error only has DOM rows (partial), GR_Import companion file rescues the 300 EA protection!
      3. Both 01 (check_movement_ztcode_stock_rec_block / resolve_gi_errors) and 02 (build_backlog_resolution_plan + D108)
         protect SL00 (BLOCKED_BY_GR_ERROR / PROTECTED_GR_DEPENDENT) and classify D108 as B (GR 미처리 대기).
    """
    from ztcode_stock_guard import extract_active_gr_error_map, check_movement_ztcode_stock_rec_block
    from backlog_resolver import build_backlog_resolution_plan

    gr_dir = tmp_path / "gr_errors"
    gi_dir = tmp_path / "gi_errors"
    gr_dir.mkdir()
    gi_dir.mkdir()

    from datetime import datetime as _dt_now
    ts = _dt_now.now().strftime("%Y%m%d") + "_081630"
    dom_file = gr_dir / f"GR_Domestics_{ts}.XLS"
    imp_file = gr_dir / f"GR_Import_{ts}.XLS"
    comb_partial = gr_dir / f"GR_Error_{ts}.XLS"

    dom_txt = (
        "2026.10.29                     Dynamic List Display                             1\n"
        "GR Interface Log (Domestics)\n"
        "\tGR Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tStor\tQty\n"
        "\t2026.10.28\tV_DOM\tMAT_DOM_ONLY\tRESIN PELLET\t101\tSL00\t500.000\n"
    )
    imp_txt = (
        "2026.10.29                     Dynamic List Display                             1\n"
        "GR Interface Log (Import)\n"
        "\tGR Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tStor\tQty\n"
        "\t2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t180.000\n"
        "\t2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t60.000\n"
        "\t2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t60.000\n"
    )
    # Case A: GR_Error has ONLY DOM row -> passing comb_partial auto-merges GR_Import_<ts>.XLS!
    comb_partial_txt = (
        "GR Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tStor\tQty\tGR_Type\n"
        "2026.10.28\tV_DOM\tMAT_DOM_ONLY\tRESIN PELLET\t101\tSL00\t500.000\tDOM\n"
    )
    dom_file.write_text(dom_txt, encoding="utf-16")
    imp_file.write_text(imp_txt, encoding="utf-16")
    comb_partial.write_text(comb_partial_txt, encoding="utf-16")

    gr_map_rescued = extract_active_gr_error_map(comb_partial)
    assert "MAT_GR300" in gr_map_rescued
    assert gr_map_rescued["MAT_GR300"]["Count"] == 3
    assert abs(gr_map_rescued["MAT_GR300"]["Total_GR_Qty"] - 300.0) < 1e-6
    assert gr_map_rescued["MAT_GR300"]["SLocs"] == {"SL00"}

    # Case B: GR_Error ALSO contains all 3 IMP rows -> multiset dedup keeps 300 EA (not 600 EA, not 240 EA)!
    comb_full_txt = (
        comb_partial_txt
        + "2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t180.000\tIMP\n"
        + "2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t60.000\tIMP\n"
        + "2026.10.28\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t101\tSL00\t60.000\tIMP\n"
    )
    comb_partial.write_text(comb_full_txt, encoding="utf-16")
    gr_map_dedup = extract_active_gr_error_map([comb_partial, dom_file, imp_file])
    assert gr_map_dedup["MAT_GR300"]["Count"] == 3
    assert abs(gr_map_dedup["MAT_GR300"]["Total_GR_Qty"] - 300.0) < 1e-6

    # Movement out of SL00 is strictly blocked by BLOCKED_BY_GR_ERROR
    blocked, status, _, q_pend, _ = check_movement_ztcode_stock_rec_block("MAT_GR300", "SL00", "SP31", gr_map_dedup, {})
    assert blocked is True
    assert status == "BLOCKED_BY_GR_ERROR"
    assert abs(q_pend - 300.0) < 1e-6

    # 02_JOB end-to-end: MAT_GR300 has COGI @ SP31 (125 EA) + GI SL01->SP31 (60 EA) + GR_Import @ SL00 (300 EA)
    gi_file = gi_dir / f"GI_Error_{ts}.XLS"
    gi_file.write_text(
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n"
        "2026.10.29\tV_IMP\tMAT_GR300\tHEADLAMP SUB ASSY\t311\tSL01\tSP31\t60.000\tInsufficient stock\n",
        encoding="utf-16",
    )
    stock_matrix = pd.DataFrame(
        [
            {"Material": "MAT_GR300", "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 300.0, "Surplus": -300.0},
            {"Material": "MAT_GR300", "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": "MAT_GR300", "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
        ]
    )
    cogi_items = [
        {
            "Material": "MAT_GR300",
            "Material_Desc": "HEADLAMP SUB ASSY",
            "SLoc": "SP31",
            "Qty": 125.0,
            "Error_Code": "M7 021",
            "Can_Resolve_By_Transfer": True,
            "Error_Date": "2026.10.29",
        },
        {
            "Material": "MAT_GR300",
            "Material_Desc": "HEADLAMP SUB ASSY",
            "SLoc": "SL00",
            "Qty": 50.0,
            "Error_Code": "M7 021",
            "Can_Resolve_By_Transfer": True,
            "Error_Date": "2026.10.29",
        },
    ]
    plan_02 = build_backlog_resolution_plan(
        cogi_items,
        stock_matrix=stock_matrix,
        gi_path=gi_file,
        gr_path=[comb_partial, dom_file, imp_file],
        require_fresh_ztcode_stock_rec=True,
        save_excel=False,
    )
    by_tgt = {r["Target_SLoc"]: r for _, r in plan_02.iterrows()}
    assert by_tgt["SL00"]["Status"] == "BLOCKED_BY_GR_ERROR"
    assert abs(float(by_tgt["SL00"]["Pending_GR_Qty"]) - 300.0) < 1e-6
    assert by_tgt["SP31"]["Status"] == "BLOCKED_BY_GI_ERROR"
    assert by_tgt["SP31"]["D108_Cause_Code"] == "B"


def test_b2_gi_link_gone_when_no_active_gi_error_exists(tmp_path: Path):
    """
    #187 B-2: When GI error was already cleared earlier (e.g. 09:30 ROUNDTRIP_CLEAR) so the
    evening GI_Error file is empty (or has no overlapping GI error for that material/SLoc),
    enrich_backlog_plan_with_d108 MUST assign `GI_LINK_GONE` and NEVER `ANOMALY_NEW_AFTER_01`.
    """
    gi_db, gi_plan_dir = _seed_test_gi_db_and_plan(tmp_path)
    empty_gi = tmp_path / "GI_Error_20261029_180500.XLS"
    empty_gi.write_text(
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n",
        encoding="utf-16",
    )

    bl_df = pd.DataFrame(
        [
            {
                "Material": "MAT_CLEARED_0930",
                "Material_Desc": "Cleared Part",
                "Target_SLoc": "SP31",
                "Deficit_Qty": 100.0,
                "Error_Code": "M7 021",
                "Action": "SKIP",
                "Status": "BLOCKED_BY_GI_ERROR",
                "Pending_GI_Qty": 100.0,
                "Pending_GI_SLocs": "SP31",
                "Reason": "Historical GI check",
            }
        ]
    )
    enriched = enrich_backlog_plan_with_d108(
        bl_df,
        as_of="2026-10-29 18:05:00",
        gi_path=empty_gi,
        gi_db_path=gi_db,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=tmp_path / "none.db",
    )
    assert enriched.loc[0, "D108_Cause_Code"] == "GI_LINK_GONE"
    assert enriched.loc[0, "D108_Cause_Code"] != "ANOMALY_NEW_AFTER_01"
    assert enriched.loc[0, "D108_Is_Bug_Candidate"] == False
