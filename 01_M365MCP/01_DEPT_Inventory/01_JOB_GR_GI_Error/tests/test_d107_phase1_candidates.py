import sqlite3
import sys
from pathlib import Path

import pandas as pd

JOB_DIR = Path(__file__).resolve().parents[1]
DEPT_DIR = JOB_DIR.parent
for _p in (JOB_DIR, DEPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import d107_delete_candidates as d107
import run_daily_gi_report as d106_runner
from gi_notifier import build_daily_cumulative_email_html


def _seed_test_gi_db(db_path: Path, logs_dir: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE pipeline_run_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                run_timestamp TEXT,
                total_gi_errors INTEGER,
                total_gr_errors INTEGER DEFAULT 0,
                postings_executed INTEGER,
                full_resolution_count INTEGER DEFAULT 0,
                partial_rebalance_count INTEGER DEFAULT 0,
                total_posted_qty REAL,
                elapsed_seconds REAL,
                status TEXT
            );
            CREATE TABLE gi_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                extracted_at TEXT,
                vendor TEXT DEFAULT '',
                material TEXT,
                material_desc TEXT,
                mvt TEXT,
                from_sloc TEXT,
                to_sloc TEXT,
                qty REAL,
                error_date TEXT,
                message TEXT
            );
            CREATE TABLE gr_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                extracted_at TEXT,
                material TEXT,
                qty REAL,
                vendor TEXT,
                error_date TEXT,
                mvt TEXT DEFAULT '101',
                message TEXT DEFAULT ''
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
                detected_at TEXT,
                part_no TEXT,
                signal_type TEXT,
                sap_total_qty REAL,
                wms_total_qty REAL,
                gap_qty REAL
            );
            CREATE TABLE gi_daily_report_dispatch (
                report_date TEXT PRIMARY KEY,
                prev_date TEXT,
                status TEXT,
                attempts INTEGER DEFAULT 0,
                sent_at TEXT,
                subject TEXT,
                recipients TEXT,
                last_error TEXT
            );
            """
        )
        # 07:00 cycle had 3 prev-day 311 items; 1 of them (MAT_CLR07) gets cleared by 07:00 posting!
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, total_gi_errors, postings_executed, total_posted_qty, elapsed_seconds, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_070000", "2026-10-29 11:02:00", 3, 1, 50.0, 120.0, "COMPLETED"),
        )
        conn.executemany(
            "INSERT INTO gi_error_snapshots (batch_id, extracted_at, material, material_desc, mvt, from_sloc, to_sloc, qty, error_date, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("RUN_20261029_070000", "2026-10-29 11:01:00", "MAT_CLR07", "Cleared", "311", "SL01", "SP31", 50.0, "2026.10.28", "Deficit"),
                ("RUN_20261029_070000", "2026-10-29 11:01:00", "MAT_CAND", "PureShort", "311", "SL02", "SP31", 100.0, "2026.10.27", "Deficit"),
                ("RUN_20261029_070000", "2026-10-29 11:01:00", "MAT_HOLD", "SmallGap", "311", "SL01", "SP31", 80.0, "2026.10.28", "Deficit"),
            ],
        )

        # Post-07:00 snapshot (07:20 / 08:00): MAT_CLR07 is gone; MAT_CAND, MAT_HOLD, MAT_GR7D, MAT_901, MAT_TODAY remain
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, total_gi_errors, postings_executed, total_posted_qty, elapsed_seconds, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_072500", "2026-10-29 11:25:00", 5, 0, 0.0, 90.0, "COMPLETED"),
        )
        conn.executemany(
            "INSERT INTO gi_error_snapshots (batch_id, extracted_at, material, material_desc, mvt, from_sloc, to_sloc, qty, error_date, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_CAND", "PureShort", "311", "SL02", "SP31", 100.0, "2026.10.27", "Deficit"),
                ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_HOLD", "SmallGap", "311", "SL01", "SP31", 80.0, "2026.10.28", "Deficit"),
                ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_GR7D", "HistGR", "311", "SL01", "SP31", 200.0, "2026.10.27", "Deficit"),
                ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_901", "Scrap", "901", "SL01", "SL01", 300.0, "2026.10.25", "Deficit"),
                ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_TODAY", "TodayErr", "311", "SL01", "SP31", 90.0, "2026.10.29", "Deficit"),
            ],
        )
        # 4-day-old GR error on MAT_GR7D (active=0, >=3d ago) -> promoted to HOLD_HUMAN_REVIEW (#187 B-4)!
        conn.execute(
            "INSERT INTO gr_error_snapshots (batch_id, extracted_at, material, qty, vendor, error_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("RUN_20261025_100000", "2026-10-25 14:00:00", "MAT_GR7D", 200.0, "V100", "2026.10.25"),
        )
        # 1-day-old GR error on MAT_GR_RECENT (<3d ago) -> remains strictly EXCLUDED!
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, extracted_at, material, material_desc, mvt, from_sloc, to_sloc, qty, error_date, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_072500", "2026-10-29 11:24:00", "MAT_GR_RECENT", "RecentGR", "311", "SL01", "SP31", 150.0, "2026.10.28", "Deficit"),
        )
        conn.execute(
            "INSERT INTO gr_error_snapshots (batch_id, extracted_at, material, qty, vendor, error_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("RUN_20261028_150000", "2026-10-28 19:00:00", "MAT_GR_RECENT", 150.0, "V101", "2026.10.28"),
        )

    plan_0725 = pd.DataFrame(
        [
            {
                "Material": "MAT_CAND",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL02",
                "From_SLoc": "",
                "To_SLoc": "SP31",
                "Transfer_Qty": 100.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "타 창고 잉여 없음",
                "SAP_By_SLoc": "{'SL01': 200.0, 'SL02': 0.0, 'SP31': 50.0}",
                "WMS_By_SLoc": "{'SL01': 200.0, 'SL02': 0.0, 'SP31': 0.0}",
            },
            {
                "Material": "MAT_HOLD",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SP31",
                "Transfer_Qty": 80.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "타 창고 잉여 없음",
                # Ambiguous: SL01 WMS (20) > SAP (10), excess +10 < 80 -> HOLD_HUMAN_REVIEW!
                "SAP_By_SLoc": "{'SL01': 10.0}",
                "WMS_By_SLoc": "{'SL01': 20.0}",
            },
            {
                "Material": "MAT_GR7D",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SP31",
                "Transfer_Qty": 200.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "타 창고 잉여 없음",
                "SAP_By_SLoc": "{'SL01': 0.0}",
                "WMS_By_SLoc": "{'SL01': 0.0}",
            },
            {
                "Material": "MAT_901",
                "Mvt_Original": "901",
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SL01",
                "Transfer_Qty": 300.0,
                "Action": "SKIP",
                "Status": "EXCLUDED_MVT_901",
                "Reason": "901 제외",
                "SAP_By_SLoc": "{}",
                "WMS_By_SLoc": "{}",
            },
            {
                "Material": "MAT_TODAY",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SP31",
                "Transfer_Qty": 90.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "당일 에러",
                "SAP_By_SLoc": "{'SL01': 0.0}",
                "WMS_By_SLoc": "{'SL01': 0.0}",
            },
            {
                "Material": "MAT_GR_RECENT",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL01",
                "From_SLoc": "",
                "To_SLoc": "SP31",
                "Transfer_Qty": 150.0,
                "Action": "SKIP",
                "Status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
                "Reason": "타 창고 잉여 없음",
                "SAP_By_SLoc": "{'SL01': 0.0}",
                "WMS_By_SLoc": "{'SL01': 0.0}",
            },
        ]
    )
    plan_0725.to_excel(logs_dir / "GI_Resolution_Plan_20261029_072500.xlsx", index=False)


def test_d107_portal_zero_access_guard():
    """Phase 1 (#180): Strictly zero reference to SAPLOG_PORTAL_URL or portal HTTP/Selenium calls."""
    for p in (
        JOB_DIR / "d107_delete_candidates.py",
        JOB_DIR / "run_daily_gi_report.py",
        JOB_DIR / "gi_notifier.py",
        DEPT_DIR / "gi_verdict_shared.py",
    ):
        src = p.read_text(encoding="utf-8")
        assert "SAPLOG_PORTAL_URL" not in src, f"Portal env var referenced prematurely in {p.name}!"


def test_d107_candidate_hold_excluded_and_0700_cleared(tmp_path):
    db_p = tmp_path / "gr_gi_history.db"
    logs_d = tmp_path / "resolution_logs"
    _seed_test_gi_db(db_p, logs_d)

    payload = d107.load_or_evaluate_d107_readonly(
        report_date_str="2026-10-29",
        gi_db_path=db_p,
        gi_plan_dir=logs_d,
        ops_raw_db_path=None,
    )
    assert payload["cleared_by_0700_count"] == 1
    assert payload["cleared_by_0700_items"][0]["material"] == "MAT_CLR07"
    assert payload["candidate_count"] == 1
    assert payload["candidates"][0]["material"] == "MAT_CAND"
    assert payload["hold_count"] == 2
    hold_mats = {h["material"] for h in payload["holds"]}
    assert hold_mats == {"MAT_HOLD", "MAT_GR7D"}
    excl_mats = {e["material"] for e in payload["excluded"]}
    assert excl_mats == {"MAT_GR_RECENT", "MAT_901", "MAT_TODAY"}


def test_d107_post_0800_change_detection_and_html_section(tmp_path):
    db_p = tmp_path / "gr_gi_history.db"
    logs_d = tmp_path / "resolution_logs"
    _seed_test_gi_db(db_p, logs_d)

    # Simulate 08:00 cycle clearing MAT_CAND and 08:30 cycle completing with 0 errors
    with sqlite3.connect(db_p) as conn:
        conn.execute(
            "INSERT INTO transfer_posting_history (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, mb1b_mvt, doc_number, post_status, posting_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_080000", "2026-10-29 12:02:00", "MAT_CAND", "SL01", "SP31", 100.0, "311", "DOC_TEST_001", "POSTED", "FULL_RESOLUTION"),
        )
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, total_gi_errors, postings_executed, total_posted_qty, elapsed_seconds, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("RUN_20261029_083000", "2026-10-29 12:32:00", 0, 0, 0.0, 60.0, "COMPLETED"),
        )
    pd.DataFrame().to_excel(logs_d / "GI_Resolution_Plan_20261029_083200.xlsx", index=False)

    payload = d107.load_or_evaluate_d107_readonly(
        report_date_str="2026-10-29",
        gi_db_path=db_p,
        gi_plan_dir=logs_d,
        ops_raw_db_path=None,
    )
    assert payload["post_0800_checked"] is True
    assert payload["post_0800_changed_count"] == 1
    assert "DOC_TEST_001" in payload["candidates"][0]["post_0800_status"]

    alert_file = d107.write_d107_post_0800_change_alert(payload, handoff_dir=tmp_path, dry_run=True)
    assert alert_file is not None and alert_file.exists()
    assert "DRYRUN_ALERT_D107_" in alert_file.name

    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(),
        posting_results=[],
        report_date="2026-10-29",
        report_time="07:30 AM",
        target_prev_date="2026-10-28",
        schedule_time_label="07:30",
        d107_payload=payload,
    )
    assert "Delete-Draft Candidates" in html
    assert "MAT_CAND" in html
    assert "MAT_HOLD" in html
    assert "Cleared/Excluded by 07:00 Cycle Postings" in html

    res = d106_runner.dispatch_daily_gi_report(
        slot="0730",
        report_date="2026-10-29",
        dry_run=True,
        db_path=db_p,
        resolution_logs_dir=logs_d,
        ops_raw_db_path=None,
    )
    assert res["status"] == "DRY_RUN_OK"
    assert res["d107_candidate_count"] == 1
    assert "DRYRUN_Daily_GI_Report_" in res["dry_run_preview_html"]


def test_b3_duplicate_ztcode_stock_rec_lines_have_distinct_occ_keys_and_0700_diff(tmp_path):
    """
    #187 B-3: Two separate ZTCODE_GI_ERR lines with identical (material, mvt, from, to, qty, error_date)
    (e.g. two 480 EA boxes) get distinct `error_key` (#1, #2) and `[#1/2행]`, `[#2/2행]` labels,
    and if 1 of the 2 lines is cleared by 07:00, `_compute_0700_cleared_summary` counts 1 cleared and 1 remaining!
    """
    db_p = tmp_path / "gr_gi_dup.db"
    logs_d = tmp_path / "logs_dup"
    _seed_test_gi_db(db_p, logs_d)

    with sqlite3.connect(db_p) as conn:
        # Add 2 identical 480 EA lines at 07:00, and keep 1 of them at 07:25 (1 cleared by 07:00!)
        conn.executemany(
            "INSERT INTO gi_error_snapshots (batch_id, extracted_at, material, material_desc, mvt, from_sloc, to_sloc, qty, error_date, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("RUN_20261029_070000", "2026-10-29 11:01:00", "MAT_DUP480", "Film LH", "311", "SL00", "SL01", 480.0, "2026.10.28", "Insufficient stock"),
                ("RUN_20261029_070000", "2026-10-29 11:01:00", "MAT_DUP480", "Film LH", "311", "SL00", "SL01", 480.0, "2026.10.28", "Insufficient stock"),
            ],
        )

    from gi_verdict_shared import evaluate_gi_errors_as_of
    two_lines = [
        {"material": "MAT_DUP480", "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28"},
        {"material": "MAT_DUP480", "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28"},
    ]
    plan_dup = pd.DataFrame(
        [
            {
                "Material": "MAT_DUP480",
                "Mvt_Original": "311",
                "Deficit_Loc": "SL00",
                "From_SLoc": "SL01",
                "To_SLoc": "SL00",
                "Transfer_Qty": 480.0,
                "Action": "SKIP",
                "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                "Reason": "SL00 guard",
                "SAP_By_SLoc": "{'SL00': 0.0, 'SL01': 960.0}",
                "WMS_By_SLoc": "{'SL00': 960.0, 'SL01': 960.0}",
            }
        ]
    )
    ev2 = evaluate_gi_errors_as_of(
        as_of="2026-10-29 07:25:00",
        gi_db_path=db_p,
        gi_plan_dir=logs_d,
        ops_raw_db_path=None,
        active_gi_records=two_lines,
        override_plan_df=plan_dup,
    )
    v_dups = [v for v in ev2["verdicts"] if v["material"] == "MAT_DUP480"]
    assert len(v_dups) == 2
    assert v_dups[0]["error_key"].endswith("|#1")
    assert v_dups[1]["error_key"].endswith("|#2")
    assert "[#1/2]" in v_dups[0]["gi_link_summary"]
    assert "[#2/2]" in v_dups[1]["gi_link_summary"]

    # Now test when only 1 of the 2 480 EA lines remains after 07:00
    ev1 = evaluate_gi_errors_as_of(
        as_of="2026-10-29 07:25:00",
        gi_db_path=db_p,
        gi_plan_dir=logs_d,
        ops_raw_db_path=None,
        active_gi_records=two_lines[:1],
        override_plan_df=plan_dup,
    )
    # MAT_CLR07 + MAT_CAND + MAT_HOLD + 1 of MAT_DUP480 cleared: let's check MAT_DUP480 specifically
    clr_cnt, clr_items, _ = d107._compute_0700_cleared_summary("2026-10-29", ev1["verdicts"], db_p)
    dup_cleared = [x for x in clr_items if x["material"] == "MAT_DUP480"]
    assert len(dup_cleared) == 1


def test_d107_post_0800_file_only_zero_error_fallback(tmp_path):
    """
    #215 / #222: When 08:00 cycle has 0 active GI errors, hourly_gi_pipeline writes
    GI_Resolution_Plan_YYYYMMDD_080254.xlsx without inserting into pipeline_run_metrics.
    `attach_post_0800_comparison` must detect the 08:02 plan file, mark `post_0800_checked=True`,
    and flag any 07:25 candidate that disappeared at 08:02 as `post_0800_changed=True`.
    """
    db_p = tmp_path / "gr_gi_history.db"
    logs_d = tmp_path / "resolution_logs"
    _seed_test_gi_db(db_p, logs_d)

    # Write ONLY an empty 08:02 plan file (NO pipeline_run_metrics row for 08:00!)
    pd.DataFrame().to_excel(logs_d / "GI_Resolution_Plan_20261029_080254.xlsx", index=False)

    payload = d107.load_or_evaluate_d107_readonly(
        report_date_str="2026-10-29",
        gi_db_path=db_p,
        gi_plan_dir=logs_d,
        ops_raw_db_path=None,
    )
    assert payload["post_0800_checked"] is True
    assert payload["post_0800_batch_id"] == "PLAN_20261029_080254"
    assert payload["post_0800_changed_count"] == 1
    assert payload["candidates"][0]["post_0800_changed"] is True
