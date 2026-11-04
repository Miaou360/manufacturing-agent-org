import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = BASE_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(DEPT_DIR))
sys.path.insert(0, str(DEPT_DIR.parent / "00_HQ_TaskManager"))

from db_manager import init_db, log_transfer_posting
from gi_notifier import build_daily_cumulative_email_html
from gr_gi_resolver import (
    load_d109_prior_completed_runs,
    resolve_material_errors_unified,
    run_cascade,
)
from mb1b_executor import execute_d115_dest_pump_posting

HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


def test_d115_case1_reproduce_sp31_to_sl02_deficit_pump_and_311_equivalence(tmp_path):
    """
    #220 BINSYNTH9952:
    - 312 SP31 -> SL02 144 EA x 2 = 288 EA; SAP: SP31=141, SL02=8064, SL01=1440, SL49=1;
      WMS: SL02=8352 (+288 >= 288*0.9=259.2), SL01=1440, SL49=1 (2x verified, GR=0).
    - Expects deficit-only pump SL02 -> SP31 147 EA (`DEST_PUMP_CLEAR_D115`) + 1 `CHAIN_CASCADE_RESOLVE`,
      settled SL02 SAP = 8205 (|SAP-WMS| 288 -> 147), SP31 = 0, Step 1 + Step 2 POSTED,
      and English-only 07:30 1-B (D115) report section.
    - Also verifies mvt-agnostic `311` (`SL03 -> SL02` or `SP31 -> SL02`) produces identical behavior.
    """
    mat = "99290069-V1"
    gi_db = tmp_path / "gr_gi_history.db"
    sig_db = tmp_path / "signals.db"
    init_db(db_path=gi_db)

    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 1440.0, "WMS_Stock": 1440.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SL49", "SAP_Stock": 1.0, "WMS_Stock": 1.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    prev_mb52 = stock_comp[["Material", "SLoc", "SAP_Stock"]].copy()
    prev_wms = stock_comp[stock_comp["SLoc"].isin(["SL01", "SL02", "SL49"])][["Material", "SLoc", "WMS_Stock"]].copy()

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp = [r for r in plan if r.get("Action") == "TRANSFER_POSTING"]
    casc = [r for r in plan if r.get("Status") == "CHAIN_CASCADE_RESOLVE"]
    assert len(tp) == 1
    assert len(casc) == 1
    assert tp[0]["From_SLoc"] == "SL02"
    assert tp[0]["To_SLoc"] == "SP31"
    assert tp[0]["Transfer_Qty"] == 147.0
    assert tp[0]["Error_Qty"] == 288.0
    assert tp[0]["Status"] == "DEST_PUMP_CLEAR_D115"
    assert tp[0]["Delta_Accuracy"] > 0

    inter = {"SL01": 1440.0, "SL02": 8064.0 - 147.0, "SL49": 1.0, "SP31": 141.0 + 147.0}
    settled, cleared = run_cascade(
        inter,
        [
            {"src": "SP31", "dst": "SL02", "qty": 144.0, "mvt": "312"},
            {"src": "SP31", "dst": "SL02", "qty": 144.0, "mvt": "312"},
        ],
    )
    assert len(cleared) == 2
    assert settled["SP31"] == 0.0
    assert settled["SL02"] == 8205.0
    assert abs(settled["SL02"] - 8352.0) == 147.0

    # mvt 311 A -> B (SL03 -> SL02) equivalence check
    gi_311 = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL03", "TOSt": "SL02", "Qty": "288.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )
    stock_311 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SL03", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    prev_m_311 = stock_311[["Material", "SLoc", "SAP_Stock"]].copy()
    prev_w_311 = pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 8352.0}])
    plan_311 = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_311,
        stock_comparison=stock_311,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=prev_w_311,
        prev_mb52_df=prev_m_311,
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_311 = [r for r in plan_311 if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_311) == 1 and tp_311[0]["Status"] == "DEST_PUMP_CLEAR_D115" and tp_311[0]["Transfer_Qty"] == 147.0

    # Same-cycle Step 1 + Step 2 execution & 07:30 English report check
    calls = []
    res_list = execute_d115_dest_pump_posting(
        tp[0],
        batch_id="RUN_20261102_120000",
        post_fn=lambda it: (calls.append((it["From_SLoc"], it["To_SLoc"], float(it["Transfer_Qty"]))) or {
            "Material": it["Material"], "From": it["From_SLoc"], "To": it["To_SLoc"], "Qty": float(it["Transfer_Qty"]), "Result": "Document 4900991150 posted"
        }),
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: (True, "ZTCODE_STOCK_REC verified SP31->SL02 cleared (remaining=0.0 EA)"),
    )
    assert len(res_list) == 1
    assert calls == [("SL02", "SP31", 147.0)]
    assert res_list[0]["Posting_Type"] == "DEST_PUMP_CLEAR_D115"
    assert res_list[0]["Post_Status"] == "POSTED"
    log_transfer_posting("RUN_20261102_120000", res_list[0], db_path=gi_db, signal_db_path=sig_db)

    postings_24h_df = pd.DataFrame(
        [{
            "exec_time": "2026-11-02 12:03",
            "material": mat,
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "transfer_qty": 147.0,
            "doc_number": "4900991150",
            "post_status": "POSTED",
            "posting_type": "DEST_PUMP_CLEAR_D115",
            "reason": tp[0]["Reason"],
        }]
    )
    html = build_daily_cumulative_email_html(
        plan_df=pd.DataFrame(plan),
        posting_results=[],
        report_date="2026-11-03",
        report_time="07:30:00",
        postings_24h_df=postings_24h_df,
        metrics_24h={"total_runs": 24, "successful_postings": 1, "total_posted_qty": 147.0},
        runs_24h_df=pd.DataFrame(),
        target_prev_date="2026-11-02",
    )
    assert "1-B (D115). Destination Warehouse Deficit-Pump Clear" in html
    assert "DEST_PUMP_CLEAR_D115" in html
    assert "Dest Pump (D115)" in html
    assert not HANGUL_RE.search(html)


def test_d115_case2_not_triggered_when_dest_wms_minus_sap_below_90pct_or_non_sl0x():
    """
    #220 Test 2:
    - SL02 WMS - SAP = +186 (< 288 * 0.9 = 259.2, even though > 141) -> MUST NOT FIRE (0 transfers).
    - Also verifies B = SL49 (non-SL01~SL04) MUST NOT FIRE.
    """
    mat = "99290069-LOWGAP"
    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
        ]
    )
    stock_low_gap = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8250.0, "Surplus": -186.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    prev_m = stock_low_gap[["Material", "SLoc", "SAP_Stock"]].copy()
    prev_w = pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 8250.0}])

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_low_gap,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=prev_w,
        prev_mb52_df=prev_m,
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    assert [r for r in plan if r.get("Action") == "TRANSFER_POSTING"] == []
    assert all(r.get("Status") == "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD" for r in plan)

    # #222 Part A: SL49 ('DMA') IS WMS-managed -> B=SL49 uses evidence=B_WMS (WMS-SAP >= 0.9*err),
    # while B=SL00 / SL30 remain strictly excluded!
    gi_sl49 = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "312", "FromSt": "SL49", "TOSt": "SP31", "Qty": "288.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"}]
    )
    stock_sl49 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL49", "SAP_Stock": 500.0, "WMS_Stock": 788.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    plan_sl49 = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_sl49,
        stock_comparison=stock_sl49,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL49", "WMS_Stock": 788.0}]),
        prev_mb52_df=stock_sl49[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_sl49 = [r for r in plan_sl49 if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_sl49) == 1
    assert tp_sl49[0]["Status"] == "DEST_PUMP_CLEAR_D115"
    assert "evidence=B_WMS" in tp_sl49[0]["Reason"]


def test_d115_case3_held_when_any_active_3file_gr_exists():
    """
    #220 Test 3:
    - Even when active GR error is on non-overlapping SLoc (`SL00`) while GI error is `SP31 -> SL02`,
      D115 Rule 4 (`3-file GR == 0`) strictly holds posting (`Action == 'SKIP'`, `PROTECTED_GR_DEPENDENT`).
    """
    mat = "99290069-GRHOLD"
    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use 3 EA"},
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    prev_m = stock_comp[["Material", "SLoc", "SAP_Stock"]].copy()
    prev_w = pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 8352.0}])
    active_gr = {
        mat: {
            "Count": 1,
            "Total_GR_Qty": 144.0,
            "SLocs": {"SL00"},
            "Vendors": {"VEND01"},
            "Dates": {"2026.11.02"},
        }
    }

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=prev_w,
        prev_mb52_df=prev_m,
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors=active_gr,
        active_gi_errors={},
        gr_valid=True,
    )
    assert [r for r in plan if r.get("Action") == "TRANSFER_POSTING"] == []
    assert all(r.get("Status") == "PROTECTED_GR_DEPENDENT" for r in plan)


def test_d115_case4_step2_failure_rollback_and_b1_race_guard(tmp_path):
    """
    #220 Test 4:
    - Step 1 posts `SL02 -> SP31 147 EA`, Step 2 fails -> immediately posts reverse `SP31 -> SL02 147 EA`
      (`ROLLED_BACK_D115` + `ROLLBACK_D115`) and blocks retry in the same unbroken GI streak.
    - Also verifies B-1 rollback race: if `SP31 -> SL02` rollback fails due to `SP31` deficit (SAP GI batch
      just consumed `SP31`), classifies as `POSTED` (`Resolved_Via_Rollback_Race=True`).
    """
    mat = "99290069-RB"
    gi_db = tmp_path / "gr_gi_history.db"
    init_db(db_path=gi_db)

    item = {
        "Material": mat,
        "Mvt_Original": "312",
        "From_SLoc": "SL02",
        "To_SLoc": "SP31",
        "Error_From": "SP31",
        "Error_To": "SL02",
        "Error_Qty": 288.0,
        "Transfer_Qty": 147.0,
        "Status": "DEST_PUMP_CLEAR_D115",
        "Reason": "DEST_PUMP_CLEAR_D115 test",
    }
    calls = []

    def fake_post_rb(it):
        calls.append((it["From_SLoc"], it["To_SLoc"], float(it["Transfer_Qty"]), it.get("Header_Text")))
        doc = "4900991151" if len(calls) == 1 else "4900991152"
        return {"Material": it["Material"], "From": it["From_SLoc"], "To": it["To_SLoc"], "Qty": float(it["Transfer_Qty"]), "Result": f"Document {doc} posted"}

    res_rb = execute_d115_dest_pump_posting(
        item,
        batch_id="RUN_20261102_120000",
        post_fn=fake_post_rb,
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: (False, "ZTCODE_STOCK_REC SP31->SL02 still remaining=288.0 EA"),
        now_fn=lambda: datetime(2026, 11, 2, 12, 3, 0),
        sleep_fn=lambda _s: None,
    )
    assert len(res_rb) == 2
    assert calls == [
        ("SL02", "SP31", 147.0, None),
        ("SP31", "SL02", 147.0, "D115_Rollback"),
    ]
    assert res_rb[0]["Posting_Type"] == "DEST_PUMP_CLEAR_D115"
    assert res_rb[0]["Post_Status"] == "ROLLED_BACK_D115"
    assert res_rb[0]["Rolled_Back"] is True
    assert res_rb[1]["Posting_Type"] == "ROLLBACK_D115"
    assert res_rb[1]["Post_Status"] == "POSTED"

    # Log rollback to temp DB and verify unbroken streak blocks endless retry
    with sqlite3.connect(str(gi_db)) as conn:
        conn.execute(
            "INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, status) VALUES (?, ?, 'P001', 2, 'COMPLETED')",
            ("RUN_20261102_120000", "2026-11-02 12:15:00"),
        )
        conn.execute(
            "INSERT INTO gi_error_snapshots (batch_id, extracted_at, error_date, material, mvt, from_sloc, to_sloc, qty, message) VALUES (?, ?, '2026.11.02', ?, '312', 'SL02', 'SP31', 144.0, 'Deficit')",
            ("RUN_20261102_120000", "2026-11-02 12:01:00", mat),
        )
        conn.commit()
    for r in res_rb:
        log_transfer_posting("RUN_20261102_120000", r, db_path=gi_db)

    prior_runs = load_d109_prior_completed_runs(gi_db_path=gi_db, plan_dir=tmp_path)
    gi_rows = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "144.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    plan_next = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 8352.0}]),
        prev_mb52_df=stock_comp[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 12:02:00",
        active_gr_errors={},
        d109_prior_runs=prior_runs,
        gr_valid=True,
    )
    assert [r for r in plan_next if r.get("Action") == "TRANSFER_POSTING"] == []

    # B-1 Rollback Race: rollback SP31->SL02 fails due to SP31 deficit -> classified POSTED
    race_calls = []

    def fake_post_race(it):
        race_calls.append((it["From_SLoc"], it["To_SLoc"]))
        if len(race_calls) == 1:
            return {"Material": it["Material"], "From": "SL02", "To": "SP31", "Qty": 147.0, "Result": "Document 4900991160 posted"}
        return {"Material": it["Material"], "From": "SP31", "To": "SL02", "Qty": 147.0, "Result": "Deficit of SL Unrestricted-use 147 EA : SP31"}

    verify_seq = iter([(False, "still 288"), (False, "still 288"), (False, "still 288"), (True, "ZTCODE_STOCK_REC SP31->SL02 cleared=0.0")])
    res_race = execute_d115_dest_pump_posting(
        item,
        batch_id="RUN_20261102_123000",
        post_fn=fake_post_race,
        wait_for_gi_interface_fn=lambda: None,
        verify_gi_cleared_fn=lambda _it: next(verify_seq),
        now_fn=lambda: datetime(2026, 11, 2, 12, 33, 0),
        sleep_fn=lambda _s: None,
    )
    assert len(res_race) == 1
    assert res_race[0]["Post_Status"] == "POSTED"
    assert res_race[0]["Resolved_Via_Rollback_Race"] is True


def test_d115_m2_explicit_rule_precedence_d97_d113_d115_d70_single_rule_per_run():
    """
    #221 M-2:
    Verifies explicit rule precedence `D97 -> D113 (A=SL00) -> D115 -> D70`
    and ensures the same material is NEVER matched by two rules in one run.
    """
    from gr_gi_resolver import RULE_PRECEDENCE_ORDER

    assert RULE_PRECEDENCE_ORDER == ("D97", "D113", "D115", "D70")

    mat = "99290069-PREC"

    # Subcase A: Material has BOTH a D97 route (312 SP11->SL01 100 EA, SAP(SP11)=0, SL01 WMS-SAP=+100)
    # AND a D115 route (312 SP31->SL02 288 EA, SAP(SP31)=141, SL02 WMS-SAP=+288) -> D97 wins ONLY (1 rule in run)!
    gi_d97_vs_d115 = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL01", "TOSt": "SP11", "Qty": "100.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "288.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
        ]
    )
    stock_a = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 500.0, "WMS_Stock": 600.0, "Surplus": -100.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SP11", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    plan_a = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_d97_vs_d115,
        stock_comparison=stock_a,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=stock_a[stock_a["SLoc"].isin(["SL01", "SL02"])][["Material", "SLoc", "WMS_Stock"]].copy(),
        prev_mb52_df=stock_a[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_a = [r for r in plan_a if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_a) == 1
    assert tp_a[0]["Status"] == "ROUNDTRIP_CLEAR"  # D97 beats D115; D115 does NOT also fire in same run!

    # Subcase B: Material has BOTH a D113 route (311 SL00->SL01 100 EA, SL00 WMS=SAP=0, SL01 SAP=500)
    # AND a D115 route (312 SP31->SL02 288 EA, SAP(SP31)=141, SL02 WMS-SAP=+288) -> D113 wins ONLY!
    gi_d113_vs_d115 = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "100.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "288.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
        ]
    )
    stock_b = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL00", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 500.0, "WMS_Stock": 600.0, "Surplus": -100.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    plan_b = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_d113_vs_d115,
        stock_comparison=stock_b,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=stock_b[stock_b["SLoc"].isin(["SL00", "SL01", "SL02"])][["Material", "SLoc", "WMS_Stock"]].copy(),
        prev_mb52_df=stock_b[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_b = [r for r in plan_b if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_b) == 1
    assert tp_b[0]["Status"] == "ROUNDTRIP_CLEAR_VIA_DEST_D113"  # D113 beats D115!

    # Subcase C: Material has BOTH a D115 route (312 SP31->SL02 288 EA) AND a D70 surplus route (311 SP11->SL01 50 EA with SL03 surplus) -> D115 wins ONLY!
    gi_d115_vs_d70 = pd.DataFrame(
        [
            {"Material Number": mat, "Mvt": "312", "FromSt": "SL02", "TOSt": "SP31", "Qty": "288.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
            {"Material Number": mat, "Mvt": "311", "FromSt": "SL01", "TOSt": "SP11", "Qty": "50.0", "Date": "2026.11.02", "Message": "Deficit of SL Unrestricted-use"},
        ]
    )
    stock_c = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 8064.0, "WMS_Stock": 8352.0, "Surplus": -288.0},
            {"Material": mat, "SLoc": "SL03", "SAP_Stock": 100.0, "WMS_Stock": 0.0, "Surplus": 100.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 141.0, "WMS_Stock": 0.0, "Surplus": 141.0},
        ]
    )
    plan_c = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_d115_vs_d70,
        stock_comparison=stock_c,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=stock_c[stock_c["SLoc"].isin(["SL01", "SL02", "SL03"])][["Material", "SLoc", "WMS_Stock"]].copy(),
        prev_mb52_df=stock_c[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_c = [r for r in plan_c if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_c) == 1
    assert tp_c[0]["Status"] == "DEST_PUMP_CLEAR_D115"  # D115 beats D70; D70 does NOT also fire in same run!


def test_d115_222_case1_311_sl02_to_sp31_wms_le_sap_triggers_and_wms_gt_sap_blocks():
    """
    #222 Part A BINSYNTH9952:
    - 311 SL02 -> SP31 (772 EA), SAP(SL02)=500, SAP(SP31)=300 (pump_qty=272):
      * When WMS(SL02)=0 <= SAP(SL02)=500 on 2 consecutive snapshots -> FIRES `DEST_PUMP_CLEAR_D115` with `evidence=A_WMS`.
      * When WMS(SL02)=1200 > SAP(SL02)=500 -> MUST NOT FIRE (0 transfers, even though ZTCODE_STOCK_REC 0%->0% would otherwise tie).
      * Also verifies `B = SP32` (`311 SL01 -> SP32`) works identically.
    """
    mat = "99043849-SP"
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL02", "TOSt": "SP31", "Qty": "772.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )

    # Subcase A: WMS(SL02)=0 <= SAP(SL02)=500 (2x) -> FIRES SP31 -> SL02 272 EA (`evidence=A_WMS`)
    stock_ok = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 500.0, "WMS_Stock": 0.0, "Surplus": 500.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 300.0, "WMS_Stock": 0.0, "Surplus": 300.0},
        ]
    )
    plan_ok = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_ok,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0}]),
        prev_mb52_df=stock_ok[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_ok = [r for r in plan_ok if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_ok) == 1
    assert tp_ok[0]["From_SLoc"] == "SP31"
    assert tp_ok[0]["To_SLoc"] == "SL02"
    assert tp_ok[0]["Transfer_Qty"] == 272.0
    assert tp_ok[0]["Status"] == "DEST_PUMP_CLEAR_D115"
    assert "evidence=A_WMS" in tp_ok[0]["Reason"]
    assert not HANGUL_RE.search(tp_ok[0]["Reason"])

    # Subcase B: WMS(SL02)=1200 > SAP(SL02)=500 -> MUST NOT FIRE (0 transfers)
    stock_gt = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 500.0, "WMS_Stock": 1200.0, "Surplus": -700.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 300.0, "WMS_Stock": 0.0, "Surplus": 300.0},
        ]
    )
    plan_gt = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_gt,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 1200.0}]),
        prev_mb52_df=stock_gt[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    assert [r for r in plan_gt if r.get("Action") == "TRANSFER_POSTING"] == []

    # Subcase C: SP32 whitelisted & supported (`311 SL01 -> SP32`)
    gi_sp32 = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL01", "TOSt": "SP32", "Qty": "200.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )
    stock_sp32 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 50.0, "WMS_Stock": 0.0, "Surplus": 50.0},
            {"Material": mat, "SLoc": "SP32", "SAP_Stock": 160.0, "WMS_Stock": 0.0, "Surplus": 160.0},
        ]
    )
    plan_sp32 = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_sp32,
        stock_comparison=stock_sp32,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL01", "WMS_Stock": 0.0}]),
        prev_mb52_df=stock_sp32[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_sp32 = [r for r in plan_sp32 if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_sp32) == 1 and tp_sp32[0]["From_SLoc"] == "SP32" and tp_sp32[0]["Transfer_Qty"] == 150.0


def test_d115_222_case2_sp31_sap_less_than_pump_qty_does_not_trigger():
    """
    #222 Part A Test 2:
    - 311 SL02 -> SP31 (772 EA), SAP(SL02)=500, WMS(SL02)=0 (pump_qty=272),
      but SAP(SP31)=200 < 272 -> MUST NOT FIRE.
    """
    mat = "99043849-LOWSP"
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL02", "TOSt": "SP31", "Qty": "772.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 500.0, "WMS_Stock": 0.0, "Surplus": 500.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 200.0, "WMS_Stock": 0.0, "Surplus": 200.0},
        ]
    )
    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 0.0}]),
        prev_mb52_df=stock_comp[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    assert [r for r in plan if r.get("Status") == "DEST_PUMP_CLEAR_D115"] == []
    assert [r for r in plan if r.get("From_SLoc") == "SP31"] == []


def test_d115_222_case3_source_a_accuracy_worsening_does_not_trigger():
    """
    #222 Part A Test 3:
    - 311 SL02 -> SP31 (772 EA), SAP(SL02)=500, WMS(SL02)=400 (`WMS <= SAP` holds!), SAP(SP31)=300 >= pump_qty(272):
      Draining SL02 to 0 worsens SL02 |SAP - WMS| from |500 - 400|=100 to |0 - 400|=400 (and ZTCODE_STOCK_REC 80% -> 0%)
      -> MUST NOT FIRE!
    """
    mat = "99043849-ACCWORSEN"
    gi_rows = pd.DataFrame(
        [{"Material Number": mat, "Mvt": "311", "FromSt": "SL02", "TOSt": "SP31", "Qty": "772.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 500.0, "WMS_Stock": 400.0, "Surplus": 100.0},
            {"Material": mat, "SLoc": "SP31", "SAP_Stock": 300.0, "WMS_Stock": 0.0, "Surplus": 300.0},
        ]
    )
    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat, "SLoc": "SL02", "WMS_Stock": 400.0}]),
        prev_mb52_df=stock_comp[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    assert [r for r in plan if r.get("Status") == "DEST_PUMP_CLEAR_D115"] == []
    assert [r for r in plan if r.get("From_SLoc") == "SP31"] == []

    # Verify SL49 (DMA WMS-managed) uses evidence=B_WMS
    mat_sl49 = "99043849-SL49BWMS"
    gi_sl49 = pd.DataFrame(
        [{"Material Number": mat_sl49, "Mvt": "311", "FromSt": "SL01", "TOSt": "SL49", "Qty": "100.0", "Date": "2026.11.02", "Message": "Insufficient stock"}]
    )
    stock_sl49 = pd.DataFrame(
        [
            {"Material": mat_sl49, "SLoc": "SL01", "SAP_Stock": 40.0, "WMS_Stock": 0.0, "Surplus": 40.0},
            {"Material": mat_sl49, "SLoc": "SL49", "SAP_Stock": 80.0, "WMS_Stock": 180.0, "Surplus": -100.0},
        ]
    )
    plan_sl49 = resolve_material_errors_unified(
        mat=mat_sl49,
        rows_df=gi_sl49,
        stock_comparison=stock_sl49,
        protected_gr_materials=set(),
        today_str="2026.11.02",
        prev_month_str="2026.10.31",
        prev_wms_df=pd.DataFrame([{"Material": mat_sl49, "SLoc": "SL01", "WMS_Stock": 0.0}, {"Material": mat_sl49, "SLoc": "SL49", "WMS_Stock": 180.0}]),
        prev_mb52_df=stock_sl49[["Material", "SLoc", "SAP_Stock"]].copy(),
        prev_snapshot_time="2026-11-02 11:02:26",
        active_gr_errors={},
        gr_valid=True,
    )
    tp_sl49 = [r for r in plan_sl49 if r.get("Action") == "TRANSFER_POSTING"]
    assert len(tp_sl49) == 1
    assert tp_sl49[0]["Status"] == "DEST_PUMP_CLEAR_D115"
    assert tp_sl49[0]["From_SLoc"] == "SL49"
    assert tp_sl49[0]["Transfer_Qty"] == 60.0
    assert "evidence=B_WMS" in tp_sl49[0]["Reason"]

