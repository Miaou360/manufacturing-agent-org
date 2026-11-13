import sqlite3
import sys
from pathlib import Path
import pandas as pd
import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = BASE_DIR.parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
for _p in (str(BASE_DIR), str(DEPT_DIR), str(HQ_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gr_gi_resolver import (
    D109_SL00_GUARD_RELEASE_RUNS,
    load_d109_prior_completed_runs,
    resolve_material_errors_unified,
)
from ztcode_stock_guard import extract_active_gr_error_map
from gi_notifier import build_daily_cumulative_email_html
from gi_verdict_shared import evaluate_gi_errors_as_of
from db_manager import init_db


def _build_base_fixture(mat: str = "99000185", sl00_wms: float = 1400.0, sl02_sap: float = 1997.0):
    gi_rows = pd.DataFrame(
        [
            {
                "Material Number": mat,
                "Mvt": "311",
                "FromSt": "SL00",
                "TOSt": "SP31",
                "Qty": "600.0",
                "Date": "2026.10.28",
                "Message": "Insufficient stock",
            }
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": sl00_wms, "Surplus": -sl00_wms},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": sl02_sap, "WMS_Stock": 0.0, "Surplus": sl02_sap},
        ]
    )
    prev_mb52 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": sl02_sap},
        ]
    )
    prev_wms = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "WMS_Stock": sl00_wms},
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0},
        ]
    )
    return gi_rows, stock_comp, prev_mb52, prev_wms


def _seed_prior_runs_db_and_plans(tmp_path: Path, mat: str, completed_gaps: list, include_failed_run: bool = False):
    """
    Seeds a temporary SQLite DB + matching `GI_Resolution_Plan_*.xlsx` files in chronological order
    (oldest -> newest). Also optionally inserts a FAILED run in between to prove failed/skipped runs
    are ignored by D109 streak counting.
    """
    db_path = tmp_path / "gr_gi_history.db"
    plan_dir = tmp_path / "resolution_logs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    base_times = [
        ("20261029_080000", "2026-10-29 08:02:10", "20261029_080205"),
        ("20261029_083000", "2026-10-29 08:32:10", "20261029_083205"),
        ("20261029_090000", "2026-10-29 09:02:10", "20261029_090205"),
        ("20261029_093000", "2026-10-29 09:32:10", "20261029_093205"),
    ]
    for idx, gap_val in enumerate(completed_gaps):
        bid_ts, r_ts, p_ts = base_times[idx]
        batch_id = f"RUN_{bid_ts}"
        conn.execute(
            """
            INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status)
            VALUES (?, ?, 'P001', 1, 'COMPLETED')
            """,
            (batch_id, r_ts),
        )
        conn.execute(
            """
            INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message)
            VALUES (?, '2026.10.28', 'V100', ?, 'TEST PART', '311', 'SL00', 'SP31', 600.0, 'Insufficient stock')
            """,
            (batch_id, mat),
        )
        pdf = pd.DataFrame(
            [
                {
                    "Material": mat,
                    "Mvt_Original": "311",
                    "Deficit_Loc": "SL00",
                    "From_SLoc": "SL02",
                    "To_SLoc": "SL00",
                    "Transfer_Qty": 600.0,
                    "Action": "SKIP",
                    "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                    "SAP_By_SLoc": str({"SL02": 1997.0}),
                    "WMS_By_SLoc": str({"SL00": float(gap_val)}),
                    "Reason": "D37 hold",
                }
            ]
        )
        pdf.to_excel(plan_dir / f"GI_Resolution_Plan_{p_ts}.xlsx", index=False)

        if include_failed_run and idx == 1:
            conn.execute(
                """
                INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status)
                VALUES ('RUN_20261029_084500', '2026-10-29 08:46:00', 'P001', 0, 'FAILED')
                """
            )
    conn.commit()
    conn.close()
    return db_path, plan_dir


def test_d109_case1_third_run_holds_and_d108_anomaly(tmp_path):
    """#189 BINSYNTH9952: 3rd consecutive run (2 prior COMPLETED + current) + GR=0 -> HOLD + D108 ANOMALY_GUARD_UNPOSTED."""
    assert D109_SL00_GUARD_RELEASE_RUNS == 4
    mat = "99000185"
    db_path, plan_dir = _seed_prior_runs_db_and_plans(tmp_path, mat, completed_gaps=[1400.0, 1400.0])
    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)
    assert len(prior_runs) == 2

    gi_rows, stock_comp, prev_mb52, prev_wms = _build_base_fixture(mat=mat, sl00_wms=1400.0)
    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 08:32:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["Action"] == "SKIP"
    assert r0["Status"] == "SKIPPED_PENDING_INTERFACE_SL00"
    assert r0["D109_Consec_Runs"] == 3

    verdict_res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 09:05:00",
        gi_db_path=db_path,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {
                "material": mat,
                "mvt": "311",
                "from_sloc": "SL00",
                "to_sloc": "SP31",
                "qty": 600.0,
                "error_date": "2026.10.28",
                "message": "Insufficient stock",
            }
        ],
        fresh_gr_records=[],
        override_plan_df=pd.DataFrame(plan_rows),
    )
    v0 = verdict_res["verdicts"][0]
    assert v0["d108_code"] == "ANOMALY_GUARD_UNPOSTED"
    assert v0["is_bug_candidate"] is True


def test_d109_case2_fourth_run_gr_zero_releases_with_cap_and_0730_section(tmp_path):
    """
    #189 Test 2: 4th consecutive COMPLETED run (with an intervening FAILED run ignored) + 3-file GR=0
    + non-decreasing SL00 gap -> GUARD_RELEASED_D109, capped at min(gi_qty, sl00_gap, from_surplus),
    and rendered in a dedicated 07:30 GI report section.
    """
    mat = "99000185"
    # Test cap: GI=600, SL00 gap grows/flat 450 -> 450 -> 500 -> 500, SL02 surplus=1997 => capped_qty = min(600, 500, 1997) = 500?
    # Note: 500 < 600 is a partial transfer; let's verify full release (SL00 gap = 1400 -> min(600, 1400, 1997) = 600 EA)
    db_path, plan_dir = _seed_prior_runs_db_and_plans(
        tmp_path, mat, completed_gaps=[1400.0, 1400.0, 1400.0], include_failed_run=True
    )
    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)
    assert len(prior_runs) == 3  # FAILED run strictly excluded

    gi_rows, stock_comp, prev_mb52, prev_wms = _build_base_fixture(mat=mat, sl00_wms=1400.0, sl02_sap=1997.0)
    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["Action"] == "TRANSFER_POSTING"
    assert r0["Status"] == "GUARD_RELEASED_D109"
    assert float(r0["Transfer_Qty"]) == 600.0
    assert r0["D109_Consec_Runs"] == 4
    assert r0["D109_Active_GR_Count"] == 0
    assert "GUARD_RELEASED_D109" in r0["Reason"]
    assert "4 consecutive runs" in r0["Reason"]
    assert "3-file GR=0" in r0["Reason"]

    # Verify 07:30 daily GI report renders dedicated GUARD_RELEASED_D109 section
    postings_24h = pd.DataFrame(
        [
            {
                "exec_time": "2026-10-29 09:32:20",
                "material": mat,
                "from_sloc": "SL02",
                "to_sloc": "SL00",
                "transfer_qty": 600.0,
                "doc_number": "4909999999",
                "post_status": "POSTED",
                "posting_type": "GUARD_RELEASED_D109",
                "reason": r0["Reason"],
            }
        ]
    )
    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(),
        posting_results=[],
        report_date="2026-10-30",
        report_time="07:30:00",
        postings_24h_df=postings_24h,
        metrics_24h={"total_runs": 4, "successful_postings": 1, "total_posted_qty": 600.0},
        runs_24h_df=pd.DataFrame(),
        target_prev_date="2026-10-29",
    )
    assert "1-B. SL00 Arrival-Hold Guard Release Postings (GUARD_RELEASED_D109: 1)" in html
    assert "SL00 Guard Release (D109)" in html
    assert mat in html


def test_d109_case3_gr_import_one_row_holds_and_d108_classifies_b(tmp_path):
    """
    #189 Test 3 & D108 rule: 4th consecutive run, GR_Error=0 & GR_Domestics=0, but GR_Import has 1 row
    (on non-overlapping SLoc SL02 so D73 SLoc-overlap does not pre-filter it) -> D109 holds as
    SKIPPED_PENDING_INTERFACE_SL00 AND D108 classifies it as 'B' (GR 미처리 대기) instead of ANOMALY_GUARD_UNPOSTED.
    """
    mat = "99000186"
    db_path, plan_dir = _seed_prior_runs_db_and_plans(tmp_path, mat, completed_gaps=[1400.0, 1400.0, 1400.0])
    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)

    # Create 3-file GR bundle in tmp_path where GR_Error & GR_Domestics are empty, GR_Import has 1 row on SL01
    # (non-overlapping with both GI SL00->SP31 and transfer SL02->SL00, testing D109 material-level 3-file GR guard!)
    ts = "20261029_093010"
    header = "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tStor\tQty\tMessage\n"
    (tmp_path / f"GR_Error_{ts}.XLS").write_text(header, encoding="utf-16")
    (tmp_path / f"GR_Domestics_{ts}.XLS").write_text(header, encoding="utf-16")
    (tmp_path / f"GR_Import_{ts}.XLS").write_text(
        header + f"2026.10.29\tV_IMP\t{mat}\tIMPORT PART\t101\tSL01\t200\tPending GR\n",
        encoding="utf-16",
    )
    active_gr_map = extract_active_gr_error_map(tmp_path / f"GR_Error_{ts}.XLS")
    assert mat in active_gr_map
    assert active_gr_map[mat]["Count"] == 1

    gi_rows, stock_comp, prev_mb52, prev_wms = _build_base_fixture(mat=mat, sl00_wms=1400.0)
    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(active_gr_map.keys()),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:02:00",
        active_gr_errors=active_gr_map,
        active_gi_errors={mat: {"Edges": {("SL00", "SP31")}}},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["Action"] == "SKIP"
    assert r0["Status"] == "SKIPPED_PENDING_INTERFACE_SL00"
    assert r0["D109_Consec_Runs"] == 4
    assert r0["D109_Active_GR_Count"] == 1

    verdict_res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 09:35:00",
        gi_db_path=db_path,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {
                "material": mat,
                "mvt": "311",
                "from_sloc": "SL00",
                "to_sloc": "SP31",
                "qty": 600.0,
                "error_date": "2026.10.28",
                "message": "Insufficient stock",
            }
        ],
        fresh_gr_records=[
            {
                "material": mat,
                "qty": 200.0,
                "vendor": "V_IMP",
                "error_date": "2026.10.29",
            }
        ],
        override_plan_df=pd.DataFrame(plan_rows),
    )
    v0 = verdict_res["verdicts"][0]
    assert v0["d108_code"] == "B"
    assert v0["is_bug_candidate"] is False


def test_d109_case4_decreasing_sl00_gap_holds(tmp_path):
    """#189 Test 4: 4th consecutive run + GR=0, but SL00 WMS-SAP gap is decreasing (1400->1400->1100->800) -> HOLD."""
    mat = "99000187"
    db_path, plan_dir = _seed_prior_runs_db_and_plans(tmp_path, mat, completed_gaps=[1400.0, 1400.0, 1100.0])
    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)

    gi_rows, stock_comp, prev_mb52, prev_wms = _build_base_fixture(mat=mat, sl00_wms=800.0)
    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["Action"] == "SKIP"
    assert r0["Status"] == "SKIPPED_PENDING_INTERFACE_SL00"
    assert r0["D109_Consec_Runs"] == 4
    assert "decreasing" in r0["Reason"]


def test_d109_case5_b4_sl00_zero_dest_wms_excess_classifies_f_and_posts_zero(tmp_path):
    """
    #190 B-4: 311 SL00->SL01 (480x2=960 EA), >=4 runs + 3-file GR=0
    + SL00 WMS=SAP=0 + dest SL01 WMS-SAP (+2343 >= 960) + SP11 SAP surplus ->
    posting = 0 (SKIP), D108 = 'F' (입고 누락 의심, 사람 확인), D107 excluded, 07:30 report shows 'SAP 입고 전기 확인 필요'.
    """
    mat = "99375648"
    db_path = tmp_path / "gr_gi_history.db"
    plan_dir = tmp_path / "resolution_logs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    for bid_ts, r_ts, p_ts in [
        ("20261029_080000", "2026-10-29 08:02:10", "20261029_080205"),
        ("20261029_083000", "2026-10-29 08:32:10", "20261029_083205"),
        ("20261029_090000", "2026-10-29 09:02:10", "20261029_090205"),
    ]:
        batch_id = f"RUN_{bid_ts}"
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES (?, ?, 'P001', 2, 'COMPLETED')",
            (batch_id, r_ts),
        )
        for _ in range(2):
            conn.execute(
                "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES (?, '2026.10.28', 'V100', ?, 'TEST PART', '311', 'SL00', 'SL01', 480.0, 'Insufficient stock')",
                (batch_id, mat),
            )
        pd.DataFrame(
            [
                {
                    "Material": mat,
                    "Mvt_Original": "311",
                    "Deficit_Loc": "SL00",
                    "From_SLoc": "SP11",
                    "To_SLoc": "SL00",
                    "Transfer_Qty": 960.0,
                    "Action": "SKIP",
                    "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                    "SAP_By_SLoc": str({"SL01": 200.0, "SP11": 2923.0}),
                    "WMS_By_SLoc": str({"SL01": 2543.0}),
                    "Reason": "D37 hold",
                }
            ]
        ).to_excel(plan_dir / f"GI_Resolution_Plan_{p_ts}.xlsx", index=False)
    conn.commit()
    conn.close()

    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)
    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
            {"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 2543.0, "Surplus": -2343.0},
            {"Material": mat, "SLoc": "SP11", "SAP_Stock": 2923.0, "WMS_Stock": 0.0, "Surplus": 2923.0},
        ]
    )
    prev_mb52 = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "SAP_Stock": 200.0}, {"Material": mat, "SLoc": "SP11", "SAP_Stock": 2923.0}])
    prev_wms = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "WMS_Stock": 2543.0}])

    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    # 0 postings executed!
    assert sum(1 for r in plan_rows if r.get("Action") == "TRANSFER_POSTING") == 0
    root_r = [r for r in plan_rows if r.get("Status") == "SKIPPED_PENDING_INTERFACE_SL00"][0]
    assert root_r["D109_Consec_Runs"] == 4
    assert root_r["D109_Suspected_Unposted_GR"] is True
    assert "verify SAP GR posting" in root_r["Reason"]

    verdict_res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 09:35:00",
        gi_db_path=db_path,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28", "message": "Insufficient stock"},
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28", "message": "Insufficient stock"},
        ],
        fresh_gr_records=[],
        override_plan_df=pd.DataFrame(plan_rows),
    )
    assert len(verdict_res["verdicts"]) == 2
    for v in verdict_res["verdicts"]:
        assert v["d108_code"] == "F"
        assert "Suspected missing GR (F)" in v["d108_label"]
        assert "human review" in v["d108_label"]
        assert v["is_bug_candidate"] is False
        assert v["d107_status"] != "CANDIDATE"

    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(plan_rows),
        posting_results=[],
        report_date="2026-10-30",
        report_time="07:30:00",
        postings_24h_df=pd.DataFrame(),
        metrics_24h={"total_runs": 4, "successful_postings": 0, "total_posted_qty": 0.0},
        runs_24h_df=pd.DataFrame(),
        target_prev_date="2026-10-29",
    )
    assert "verify SAP GR posting" in html


def test_d109_case6_b5_one_time_release_safety_blocks_repeat_posting(tmp_path):
    """
    #190 B-5: After GUARD_RELEASED_D109 is posted in Run 4, if the same GI error remains in Run 5
    (and even after 3+ subsequent SKIP runs in Run 8), it is NEVER release-posted again (alert only).
    """
    mat = "99000189"
    db_path, plan_dir = _seed_prior_runs_db_and_plans(tmp_path, mat, completed_gaps=[1400.0, 1400.0, 1400.0])

    # Record Run 4 as GUARD_RELEASED_D109 posted in DB + Plan, followed by 3 more runs where GI error still remained
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, postings_executed, status) VALUES ('RUN_20261029_093000', '2026-10-29 09:32:10', 'P001', 1, 1, 'COMPLETED')"
    )
    conn.execute(
        "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES ('RUN_20261029_093000', '2026.10.28', 'V100', ?, 'TEST PART', '311', 'SL00', 'SP31', 600.0, 'Insufficient stock')",
        (mat,),
    )
    conn.execute(
        """
        INSERT INTO transfer_posting_history (batch_id, material, from_sloc, to_sloc, transfer_qty, mb1b_mvt, doc_number, post_status, posting_type, sap_response, reason)
        VALUES ('RUN_20261029_093000', ?, 'SL02', 'SL00', 600.0, '311', '4909999998', 'POSTED', 'GUARD_RELEASED_D109', 'Document 4909999998 posted', 'GUARD_RELEASED_D109')
        """,
        (mat,),
    )
    pd.DataFrame(
        [
            {
                "Material": mat,
                "Mvt_Original": "311",
                "Deficit_Loc": "SL00",
                "From_SLoc": "SL02",
                "To_SLoc": "SL00",
                "Transfer_Qty": 600.0,
                "Action": "TRANSFER_POSTING",
                "Status": "GUARD_RELEASED_D109",
                "SAP_By_SLoc": str({"SL02": 1997.0}),
                "WMS_By_SLoc": str({"SL00": 1400.0}),
                "Reason": "GUARD_RELEASED_D109",
            }
        ]
    ).to_excel(plan_dir / "GI_Resolution_Plan_20261029_093205.xlsx", index=False)

    # Also add 3 subsequent runs (10:00, 10:30, 11:00) where the same GI error persisted!
    for bid_ts, r_ts, p_ts in [
        ("20261029_100000", "2026-10-29 10:02:10", "20261029_100205"),
        ("20261029_103000", "2026-10-29 10:32:10", "20261029_103205"),
        ("20261029_110000", "2026-10-29 11:02:10", "20261029_110205"),
    ]:
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES (?, ?, 'P001', 1, 'COMPLETED')",
            (f"RUN_{bid_ts}", r_ts),
        )
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES (?, '2026.10.28', 'V100', ?, 'TEST PART', '311', 'SL00', 'SP31', 600.0, 'Insufficient stock')",
            (f"RUN_{bid_ts}", mat),
        )
        pd.DataFrame(
            [
                {
                    "Material": mat,
                    "Mvt_Original": "311",
                    "Deficit_Loc": "SL00",
                    "From_SLoc": "SL02",
                    "To_SLoc": "SL00",
                    "Transfer_Qty": 600.0,
                    "Action": "SKIP",
                    "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                    "SAP_By_SLoc": str({"SL02": 1397.0}),
                    "WMS_By_SLoc": str({"SL00": 1400.0}),
                    "Reason": "hold",
                }
            ]
        ).to_excel(plan_dir / f"GI_Resolution_Plan_{p_ts}.xlsx", index=False)
    conn.commit()
    conn.close()

    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)
    gi_rows, stock_comp, prev_mb52, prev_wms = _build_base_fixture(mat=mat, sl00_wms=1400.0, sl02_sap=1397.0)
    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 11:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["Action"] == "SKIP"
    assert r0["Status"] == "SKIPPED_D109_ALREADY_RELEASED"
    assert r0["D109_Already_Released"] is True
    assert "retry blocked" in r0["Reason"]

    verdict_res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 11:35:00",
        gi_db_path=db_path,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SP31", "qty": 600.0, "error_date": "2026.10.28", "message": "Insufficient stock"}
        ],
        fresh_gr_records=[],
        override_plan_df=pd.DataFrame(plan_rows),
    )
    assert verdict_res["verdicts"][0]["d108_code"] == "ANOMALY_POSTED_BUT_REMAINED"

    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(plan_rows),
        posting_results=[],
        report_date="2026-10-30",
        report_time="07:30:00",
        postings_24h_df=pd.DataFrame(),
        metrics_24h={"total_runs": 8, "successful_postings": 1, "total_posted_qty": 600.0},
        runs_24h_df=pd.DataFrame(),
        target_prev_date="2026-10-29",
    )
    assert "Retry Blocked" in html


def test_d109_case7_non_sl0x_source_sp11_never_releases_even_when_sl00_wms_gt_sap(tmp_path):
    """#195 Test 7: SL00 WMS-SAP > 0, 4 consecutive runs, GR=0, but candidate source is SP11 (or SL49) -> posted 0 (hold maintained, D108 = ②)."""
    from gr_gi_resolver import D109_ALLOWED_SOURCE_SLOCS

    assert "SL49" not in D109_ALLOWED_SOURCE_SLOCS
    assert "SP11" not in D109_ALLOWED_SOURCE_SLOCS
    assert D109_ALLOWED_SOURCE_SLOCS == {"SL00", "SL01", "SL02", "SL03", "SL04"}

    mat = "99000191"
    db_path = tmp_path / "gr_gi_history_sp11.db"
    plan_dir = tmp_path / "resolution_logs_sp11"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=db_path)

    conn = sqlite3.connect(str(db_path))
    for bid_ts, r_ts, p_ts in [
        ("20261029_080000", "2026-10-29 08:02:10", "20261029_080205"),
        ("20261029_083000", "2026-10-29 08:32:10", "20261029_083205"),
        ("20261029_090000", "2026-10-29 09:02:10", "20261029_090205"),
    ]:
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES (?, ?, 'P001', 1, 'COMPLETED')",
            (f"RUN_{bid_ts}", r_ts),
        )
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES (?, '2026.10.28', 'V100', ?, 'TEST PART', '311', 'SL00', 'SL01', 600.0, 'Insufficient stock')",
            (f"RUN_{bid_ts}", mat),
        )
        pd.DataFrame(
            [
                {
                    "Material": mat,
                    "Mvt_Original": "311",
                    "Deficit_Loc": "SL00",
                    "From_SLoc": "SP11",
                    "To_SLoc": "SL00",
                    "Transfer_Qty": 600.0,
                    "Action": "SKIP",
                    "Status": "SKIPPED_PENDING_INTERFACE_SL00",
                    "SAP_By_SLoc": str({"SP11": 2000.0}),
                    "WMS_By_SLoc": str({"SL00": 1400.0}),
                    "Reason": "D37 hold",
                }
            ]
        ).to_excel(plan_dir / f"GI_Resolution_Plan_{p_ts}.xlsx", index=False)
    conn.commit()
    conn.close()

    prior_runs = load_d109_prior_completed_runs(gi_db_path=db_path, plan_dir=plan_dir)
    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "600.0", "Date": "2026.10.28", "Message": "Insufficient stock"}
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 1400.0, "Surplus": -1400.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 600.0, "Surplus": -600.0},
            {"Material": mat, "SLoc": "SP11", "SAP_Stock": 2000.0, "WMS_Stock": 0.0, "Surplus": 2000.0},
        ]
    )
    prev_mb52 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SP11", "SAP_Stock": 2000.0},
        ]
    )
    prev_wms = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "WMS_Stock": 1400.0},
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 600.0},
        ]
    )

    plan_rows = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert len(plan_rows) == 1
    r0 = plan_rows[0]
    assert r0["From_SLoc"] == "SP11"
    assert r0["Action"] == "SKIP"
    assert r0["Status"] == "SKIPPED_PENDING_INTERFACE_SL00"
    assert r0["D109_Consec_Runs"] == 4
    assert r0["D109_Active_GR_Count"] == 0
    assert r0["SL00_WMS_SAP_Gap"] == pytest.approx(1400.0)
    assert r0["D109_Suspected_Unposted_GR"] is False
    assert "only SL0x allowed for D109 release" in r0["Reason"]

    # Executable postings count must be strictly 0
    executable_rows = [r for r in plan_rows if r.get("Action") == "TRANSFER_POSTING"]
    assert len(executable_rows) == 0

    # D108 follows current rules: since B-4 (SL00 WMS=SAP=0) is false, classifies as ② ANOMALY_GUARD_UNPOSTED
    verdict_res = evaluate_gi_errors_as_of(
        as_of="2026-10-29 09:35:00",
        gi_db_path=db_path,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 600.0, "error_date": "2026.10.28", "message": "Insufficient stock"}
        ],
        fresh_gr_records=[],
        override_plan_df=pd.DataFrame(plan_rows),
    )
    assert verdict_res["verdicts"][0]["d108_code"] == "ANOMALY_GUARD_UNPOSTED"

