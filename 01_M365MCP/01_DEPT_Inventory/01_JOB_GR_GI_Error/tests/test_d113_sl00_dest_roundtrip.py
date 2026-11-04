import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(DEPT_DIR))
sys.path.insert(0, str(DEPT_DIR.parent / "00_HQ_TaskManager"))

from db_manager import init_db, log_transfer_posting
from gi_notifier import build_daily_cumulative_email_html
from gi_verdict_shared import evaluate_gi_errors_as_of
from gr_gi_resolver import (
    load_d109_prior_completed_runs,
    resolve_material_errors_unified,
)
from mb1b_executor import execute_d113_roundtrip_posting


def test_d113_step1_and_step2_success_and_anti_shadowing(tmp_path):
    """
    #211 BINSYNTH9952:
    - 311 SL00 -> SL01 480 EA x 2 = 960 EA (modeled on 99375648 & 99375649).
    - Case A (09:00 pattern): SL01 WMS=3360 > SAP=1017, SP31=2700, SP11=2923 (anti-shadowing check!).
    - Case B (10:00 pattern): SL01 WMS=2760 == SAP=2760 (0 gap in SL01, SP31=2210).
    - Both produce Action='TRANSFER_POSTING', Status='ROUNDTRIP_CLEAR_VIA_DEST_D113', SL01->SL00 960 EA,
      net variance delta 0, Step 1 + Step 2 verified POSTED, GI_STOCK_GAP_SIGNALS recorded when WMS!=SAP,
      and 07:30 D106 report renders dedicated English 1-B (D113) row.
    """
    mat_a = "99375648"
    gi_db = tmp_path / "gr_gi_history.db"
    sig_db = tmp_path / "signals.db"
    init_db(db_path=gi_db)

    gi_rows_a = pd.DataFrame(
        [
            {"Material Number": mat_a, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
            {"Material Number": mat_a, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
        ]
    )
    stock_a = pd.DataFrame(
        [
            {"Material": mat_a, "SLoc": "SL01", "SAP_Stock": 1017.0, "WMS_Stock": 3360.0, "Surplus": -2343.0},
            {"Material": mat_a, "SLoc": "SP31", "SAP_Stock": 2700.0, "WMS_Stock": 0.0, "Surplus": 2700.0},
            {"Material": mat_a, "SLoc": "SP11", "SAP_Stock": 2923.0, "WMS_Stock": 0.0, "Surplus": 2923.0},
        ]
    )
    prev_mb52_a = pd.DataFrame(
        [
            {"Material": mat_a, "SLoc": "SL01", "SAP_Stock": 1017.0},
            {"Material": mat_a, "SLoc": "SP31", "SAP_Stock": 2700.0},
            {"Material": mat_a, "SLoc": "SP11", "SAP_Stock": 2923.0},
        ]
    )
    prev_wms_a = pd.DataFrame([{"Material": mat_a, "SLoc": "SL01", "WMS_Stock": 3360.0}])

    plan_a = resolve_material_errors_unified(
        mat=mat_a,
        rows_df=gi_rows_a,
        stock_comparison=stock_a,
        protected_gr_materials=set(),
        today_str="2026.10.30",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms_a,
        prev_mb52_df=prev_mb52_a,
        prev_snapshot_time="2026-10-30 08:32:00",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_a = [r for r in plan_a if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_a) == 1
    assert tp_a[0]["From_SLoc"] == "SL01"
    assert tp_a[0]["To_SLoc"] == "SL00"
    assert tp_a[0]["Transfer_Qty"] == 960.0
    assert tp_a[0]["Status"] == "ROUNDTRIP_CLEAR_VIA_DEST_D113"
    assert abs(tp_a[0]["Delta_Accuracy"]) <= 1e-6

    # Execute Step 1 + Step 2 in same cycle
    posted_calls = []

    def fake_post_ok(it):
        posted_calls.append((it["From_SLoc"], it["To_SLoc"], float(it["Transfer_Qty"])))
        return {
            "Material": it["Material"],
            "From": it["From_SLoc"],
            "To": it["To_SLoc"],
            "Qty": float(it["Transfer_Qty"]),
            "Result": "Document 4900990000 posted",
        }

    res_list = execute_d113_roundtrip_posting(
        tp_a[0],
        batch_id="RUN_20261030_090000",
        post_fn=fake_post_ok,
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: (True, "ZTCODE_STOCK_REC verified SL00->SL01 cleared (remaining=0.0 EA)"),
    )
    assert len(res_list) == 1
    assert posted_calls == [("SL01", "SL00", 960.0)]
    assert res_list[0]["Post_Status"] == "POSTED"
    assert res_list[0]["Step2_Verified"] is True
    assert res_list[0]["Rolled_Back"] is False

    log_transfer_posting("RUN_20261030_090000", res_list[0], db_path=gi_db, signal_db_path=sig_db)
    with sqlite3.connect(str(sig_db)) as sconn:
        sig_rows = sconn.execute(
            "SELECT material, error_from, error_to, roundtrip_sloc, roundtrip_qty, signal_type FROM GI_STOCK_GAP_SIGNALS"
        ).fetchall()
    assert len(sig_rows) == 1
    assert sig_rows[0] == (mat_a, "SL00", "SL01", "SL01", 960.0, "ROUNDTRIP_CLEAR")

    # Case B (10:00 pattern: SL01 WMS == SAP == 5280 >= 960)
    mat_b = "99375649"
    gi_rows_b = pd.DataFrame(
        [
            {"Material Number": mat_b, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
            {"Material Number": mat_b, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"},
        ]
    )
    stock_b = pd.DataFrame(
        [
            {"Material": mat_b, "SLoc": "SL01", "SAP_Stock": 5280.0, "WMS_Stock": 5280.0, "Surplus": 0.0},
            {"Material": mat_b, "SLoc": "SP31", "SAP_Stock": 2210.0, "WMS_Stock": 0.0, "Surplus": 2210.0},
        ]
    )
    prev_mb52_b = pd.DataFrame([{"Material": mat_b, "SLoc": "SL01", "SAP_Stock": 5280.0}, {"Material": mat_b, "SLoc": "SP31", "SAP_Stock": 2210.0}])
    prev_wms_b = pd.DataFrame([{"Material": mat_b, "SLoc": "SL01", "WMS_Stock": 5280.0}])

    plan_b = resolve_material_errors_unified(
        mat=mat_b,
        rows_df=gi_rows_b,
        stock_comparison=stock_b,
        protected_gr_materials=set(),
        today_str="2026.10.30",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms_b,
        prev_mb52_df=prev_mb52_b,
        prev_snapshot_time="2026-10-30 09:32:00",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_b = [r for r in plan_b if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_b) == 1
    assert tp_b[0]["From_SLoc"] == "SL01"
    assert tp_b[0]["To_SLoc"] == "SL00"
    assert tp_b[0]["Transfer_Qty"] == 960.0
    assert tp_b[0]["Status"] == "ROUNDTRIP_CLEAR_VIA_DEST_D113"

    # Verify 07:30 D106 report renders dedicated English 1-B (D113) section
    postings_24h_df = pd.DataFrame(
        [
            {
                "material": mat_a,
                "from_sloc": "SL01",
                "to_sloc": "SL00",
                "transfer_qty": 960.0,
                "doc_number": "4900990000",
                "post_status": "POSTED",
                "posting_type": "ROUNDTRIP_CLEAR_VIA_DEST_D113",
                "sap_response": "Document 4900990000 posted | Step 2 OK",
                "posted_at": "2026-10-30 09:18:00",
            }
        ]
    )
    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(),
        posting_results=[],
        report_date="2026-10-31",
        report_time="07:30:00",
        postings_24h_df=postings_24h_df,
        metrics_24h={"total_runs": 24, "successful_postings": 1, "total_posted_qty": 960.0},
        runs_24h_df=pd.DataFrame(),
        target_prev_date="2026-10-30",
    )
    assert "1-B (D113)" in html
    assert "Same-Cycle Destination Roundtrip Clear" in html
    assert "Dest Roundtrip (D113)" in html


def test_d113_step2_failure_immediately_reverses_step1_and_blocks_repeat_in_streak(tmp_path):
    """
    #211 Test 2 (Rule 5):
    - Step 1 (`SL01 -> SL00 960 EA`) posts Doc 4900990001.
    - Step 2 fails (`verify_gi_cleared_fn` returns False or raises an exception).
    - Immediately executes reverse MB1B 311 (`SL00 -> SL01 960 EA`, Header_Text='D113_Rollback') in the SAME cycle!
    - Logs `ROLLED_BACK_D113` + `ROLLBACK_D113` to DB; next cycle in the same error streak does NOT auto-post again.
    """
    mat = "99375650"
    gi_db = tmp_path / "gr_gi_history.db"
    sig_db = tmp_path / "signals.db"
    plan_dir = tmp_path / "resolution_logs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=gi_db)

    item = {
        "Material": mat,
        "Mvt_Original": "311",
        "From_SLoc": "SL01",
        "To_SLoc": "SL00",
        "Transfer_Qty": 960.0,
        "MB1B_Mvt": "311",
        "Header_Text": "GI_Error_Clear",
        "Action": "TRANSFER_POSTING",
        "Status": "ROUNDTRIP_CLEAR_VIA_DEST_D113",
        "Error_From": "SL00",
        "Error_To": "SL01",
        "Error_Qty": 960.0,
        "SAP_Total": 1017.0,
        "WMS_Total": 3360.0,
        "Total_Gap": 2343.0,
        "Reason": "D113 test",
    }

    calls = []

    def fake_post_seq(it):
        calls.append((it["From_SLoc"], it["To_SLoc"], float(it["Transfer_Qty"]), it.get("Header_Text")))
        doc_no = "4900990001" if len(calls) == 1 else "4900990002"
        return {
            "Material": it["Material"],
            "From": it["From_SLoc"],
            "To": it["To_SLoc"],
            "Qty": float(it["Transfer_Qty"]),
            "Result": f"Document {doc_no} posted",
        }

    res_list = execute_d113_roundtrip_posting(
        item,
        batch_id="RUN_20261030_090000",
        post_fn=fake_post_seq,
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: (False, "ZTCODE_STOCK_REC SL00->SL01 still remaining=960.0 EA"),
    )
    assert len(calls) == 2
    assert calls[0] == ("SL01", "SL00", 960.0, "GI_Error_Clear")
    assert calls[1] == ("SL00", "SL01", 960.0, "D113_Rollback")
    assert len(res_list) == 2
    assert res_list[0]["Post_Status"] == "ROLLED_BACK_D113"
    assert res_list[0]["Rolled_Back"] is True
    assert res_list[0]["Rollback_Doc"] == "4900990002"
    assert res_list[1]["Posting_Type"] == "ROLLBACK_D113"
    assert res_list[1]["Post_Status"] == "POSTED"

    # Persist run & postings -> verify no GI_STOCK_GAP_SIGNALS written on rollback & next run blocks repeat D113
    with sqlite3.connect(str(gi_db)) as conn:
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES ('RUN_20261030_090000', '2026-10-30 09:05:00', 'P999', 2, 'COMPLETED')"
        )
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES ('RUN_20261030_090000', '2026.10.28', 'V100', ?, 'PART', '311', 'SL00', 'SL01', 480.0, 'Insufficient stock')",
            (mat,),
        )
        conn.commit()
    for sr in res_list:
        log_transfer_posting("RUN_20261030_090000", sr, db_path=gi_db, signal_db_path=sig_db)

    assert not sig_db.exists()

    prior_runs = load_d109_prior_completed_runs(gi_db_path=gi_db, plan_dir=plan_dir)
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.28", "Message": "Insufficient stock"}]
    )
    stock = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "SAP_Stock": 1017.0, "WMS_Stock": 3360.0, "Surplus": -2343.0}])
    prev_mb52 = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "SAP_Stock": 1017.0}])
    prev_wms = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "WMS_Stock": 3360.0}])

    plan_next = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock,
        protected_gr_materials=set(),
        today_str="2026.10.30",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-30 09:05:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert sum(1 for r in plan_next if r.get("Action") == "TRANSFER_POSTING") == 0


def test_d113_sl00_wms_gt_sap_keeps_d37_hold():
    """
    #211 Test 3:
    When SL00 WMS > SAP (e.g. SL00 WMS=480, SAP=0 -> genuine pending GR interface!),
    D113 MUST NOT fire; D37 hold (`SKIPPED_PENDING_INTERFACE_SL00` / 0 transfers) strictly remains in force.
    """
    mat = "99375651"
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.30", "Message": "Insufficient stock"}]
    )
    stock = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 480.0, "Surplus": -480.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1500.0, "WMS_Stock": 1500.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 1000.0, "WMS_Stock": 0.0, "Surplus": 1000.0},
        ]
    )
    prev_mb52 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1500.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 1000.0},
        ]
    )
    prev_wms = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "WMS_Stock": 480.0},
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 1500.0},
        ]
    )

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock,
        protected_gr_materials=set(),
        today_str="2026.10.30",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-30 08:32:00",
        active_gr_errors={},
        gr_valid=True,
    )
    assert sum(1 for r in plan if r.get("Action") == "TRANSFER_POSTING") == 0
    assert any(r.get("Status") == "SKIPPED_PENDING_INTERFACE_SL00" for r in plan)


def test_d113_active_gr_error_holds():
    """
    #211 Test 4 (Rule 3):
    Even if 1 active GR row exists for the material across 3 GR files (even on a non-overlapping SLoc like SL02),
    D113 holds (0 transfers).
    """
    mat = "99375652"
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "480.0", "Date": "2026.10.30", "Message": "Insufficient stock"}]
    )
    stock = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "SAP_Stock": 1500.0, "WMS_Stock": 1500.0, "Surplus": 0.0}])
    prev_mb52 = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "SAP_Stock": 1500.0}])
    prev_wms = pd.DataFrame([{"Material": mat, "SLoc": "SL01", "WMS_Stock": 1500.0}])

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock,
        protected_gr_materials={mat},
        today_str="2026.10.30",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-30 08:32:00",
        active_gr_errors={mat: {"SLocs": {"SL02"}, "Count": 1, "Total_GR_Qty": 480.0}},
        gr_valid=True,
    )
    assert sum(1 for r in plan if r.get("Action") == "TRANSFER_POSTING") == 0


def test_d113_d108_verdict_resolvable_next_run_not_f(tmp_path):
    """
    #211 Test 5:
    D108 classifies D113-eligible `SL00 -> SL01` items as `"resolvable — D113 next run"` instead of `F`,
    and never marks them as D107 delete candidates.
    """
    mat = "99375648"
    gi_db = tmp_path / "gr_gi_history.db"
    plan_dir = tmp_path / "resolution_logs"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=gi_db)

    # Even if prior plan had SKIPPED_PENDING_INTERFACE_SL00 (or unposted ROUNDTRIP_CLEAR_VIA_DEST_D113),
    # D108 classifies both 480 EA lines as "resolvable — D113 next run"!
    override_df = pd.DataFrame(
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
                "D109_Consec_Runs": 5,
                "D109_Active_GR_Count": 0,
                "SAP_By_SLoc": str({"SL01": 1017.0, "SP11": 2923.0}),
                "WMS_By_SLoc": str({"SL01": 3360.0}),
                "Reason": "Pre-D113 hold",
            }
        ]
    )
    res = evaluate_gi_errors_as_of(
        as_of="2026-10-30 09:35:00",
        gi_db_path=gi_db,
        gi_plan_dir=plan_dir,
        active_gi_records=[
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28", "message": "Insufficient stock"},
            {"material": mat, "mvt": "311", "from_sloc": "SL00", "to_sloc": "SL01", "qty": 480.0, "error_date": "2026.10.28", "message": "Insufficient stock"},
        ],
        fresh_gr_records=[],
        override_plan_df=override_df,
    )
    assert len(res["verdicts"]) == 2
    for v in res["verdicts"]:
        assert v["d108_code"] == "RESOLVABLE_D113_NEXT_RUN"
        assert v["d108_label"] == "resolvable — D113 next run"
        assert v["is_bug_candidate"] is False
        assert v["d107_status"] != "CANDIDATE"


def test_d113_b1_rollback_race_and_wait_ceiling(tmp_path):
    """
    #216 B-1 regression test:
    1) Wait ceiling = next batch tick + 1 tick (max ~15m = 900s), unconditionally stopping
       5 minutes before the next 01_JOB cycle start (e.g. 09:25 before 09:30 daytime cycle).
    2) Pre-rollback ZTCODE_STOCK_REC re-query catches clearance right before rollback without calling rollback MB1B.
    3) Rollback race: if SAP GI batch consumes SL00 stock milliseconds before rollback MB1B (SL00->X)
       so rollback fails with SL00 deficit, D113 classifies as RESOLVED (`Post_Status='POSTED'`,
       NOT `ROLLED_BACK_D113`) and does NOT block retries in subsequent runs.
    """
    from datetime import datetime
    from mb1b_executor import _compute_d113_wait_schedule, execute_d113_roundtrip_posting

    # (a) Wait ceiling & next-01-cycle -5m hard cutoff
    sched_early = _compute_d113_wait_schedule(datetime(2026, 11, 2, 9, 3, 0))
    assert sched_early["next_cycle_start"] == datetime(2026, 11, 2, 9, 30, 0)
    assert sched_early["hard_deadline"] == datetime(2026, 11, 2, 9, 18, 0)  # 900s (15m) cap < 09:25
    assert sched_early["tick1_wait_sec"] == 285  # 09:03:00 -> 09:07:45
    assert sched_early["tick2_wait_sec"] == 600  # +1 tick within 900s cap

    sched_late = _compute_d113_wait_schedule(datetime(2026, 11, 2, 9, 18, 0))
    assert sched_late["next_cycle_start"] == datetime(2026, 11, 2, 9, 30, 0)
    assert sched_late["hard_deadline"] == datetime(2026, 11, 2, 9, 25, 0)  # Hard stop 5m before 09:30!
    assert sched_late["total_budget_sec"] == 420

    # (b) Rollback race (SL00 deficit on rollback SL00->SL01) -> RESOLVED, NOT ROLLED_BACK, NO retry block!
    mat = "99375659"
    gi_db = tmp_path / "gr_gi_race.db"
    sig_db = tmp_path / "signals_race.db"
    plan_dir = tmp_path / "resolution_logs_race"
    plan_dir.mkdir(parents=True, exist_ok=True)
    init_db(db_path=gi_db)

    item = {
        "Material": mat,
        "Mvt_Original": "311",
        "From_SLoc": "SL01",
        "To_SLoc": "SL00",
        "Transfer_Qty": 480.0,
        "MB1B_Mvt": "311",
        "Header_Text": "GI_Error_Clear",
        "Action": "TRANSFER_POSTING",
        "Status": "ROUNDTRIP_CLEAR_VIA_DEST_D113",
        "Error_From": "SL00",
        "Error_To": "SL01",
        "Error_Qty": 480.0,
        "SAP_Total": 1500.0,
        "WMS_Total": 1500.0,
        "Total_Gap": 0.0,
        "Reason": "D113 B-1 race test",
    }

    mb1b_calls = []

    def fake_post_race(it):
        mb1b_calls.append((it["From_SLoc"], it["To_SLoc"], it.get("Header_Text")))
        if len(mb1b_calls) == 1:
            return {"Material": mat, "From": "SL01", "To": "SL00", "Qty": 480.0, "Result": "Document 4900990011 posted"}
        # Rollback SL00 -> SL01 fails because SAP GI batch just consumed the 480 EA in SL00!
        return {"Material": mat, "From": "SL00", "To": "SL01", "Qty": 480.0, "Result": "ERROR: Deficit of SL Unrestricted-use 480 EA : SL00"}

    ztcode_stock_rec_checks = [
        (False, "Tick 1 still pending"),
        (False, "Tick 2 still pending"),
        (False, "Pre-rollback ZTCODE_STOCK_REC milliseconds before SAP batch commit"),
        (True, "ZTCODE_STOCK_REC verified SL00->SL01 cleared (remaining=0.0 EA)"),
    ]

    res_race = execute_d113_roundtrip_posting(
        item,
        batch_id="RUN_20261102_090000",
        post_fn=fake_post_race,
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: ztcode_stock_rec_checks.pop(0),
        now_fn=lambda: datetime(2026, 11, 2, 9, 3, 0),
    )
    assert len(res_race) == 1
    assert res_race[0]["Post_Status"] == "POSTED"
    assert res_race[0]["Step2_Verified"] is True
    assert res_race[0]["Rolled_Back"] is False
    assert res_race[0]["Resolved_Via_Rollback_Race"] is True

    # Log to DB and verify retry is NOT blocked if another error occurs
    with sqlite3.connect(str(gi_db)) as conn:
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES ('RUN_20261102_090000', '2026-11-02 09:05:00', 'P999', 1, 'COMPLETED')"
        )
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message) VALUES ('RUN_20261102_090000', '2026.11.02', 'V100', ?, 'PART', '311', 'SL00', 'SL01', 480.0, 'Insufficient stock')",
            (mat,),
        )
        conn.commit()
    for sr in res_race:
        log_transfer_posting("RUN_20261102_090000", sr, db_path=gi_db, signal_db_path=sig_db)

    prior_runs = load_d109_prior_completed_runs(gi_db_path=gi_db, plan_dir=plan_dir)
    assert mat not in (prior_runs[0].get("d113_attempted_mats") or {})
