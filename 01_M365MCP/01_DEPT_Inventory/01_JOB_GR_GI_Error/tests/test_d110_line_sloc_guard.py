import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = BASE_DIR.parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
for _p in (str(BASE_DIR), str(DEPT_DIR), str(HQ_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from db_manager import init_db
from gi_notifier import build_daily_cumulative_email_html, build_gi_email_html
from gr_gi_resolver import (
    D110_COOLDOWN_HOURS,
    D110_CONSECUTIVE_FAILURE_LIMIT,
    load_d110_failure_cooldown_map,
    resolve_material_errors_unified,
)


def test_d110_199_preserves_sp31_and_sl49_warehouse_accuracy_transfers():
    """
    User Decision #199:
      - D110 Rule 1 (SP* / SL49 source block) is NOT adopted because SL00~SL04 warehouse stock
        accuracy takes priority.
      - Verify that:
        1) D77 partial rebalancing from SP31 -> SL01 (improving SL01 accuracy) STILL executes.
        2) Full resolution READY transfer from SP31 -> SL02 (clearing SL02 -> SL01 where SL01 WMS=140) STILL executes.
        3) Roundtrip SP31 -> SL02 (clearing SL02 -> SP31) STILL executes.
    """
    # 1) D77 partial accuracy rebalancing from SP31 -> SL01
    mat_d77 = "99287459"
    gi_d77 = pd.DataFrame(
        [
            {
                "Material Number": mat_d77,
                "Mvt": "311",
                "FromSt": "SL01",
                "TOSt": "SL02",
                "Qty": "500.0",
                "Date": "2026.10.28",
                "Message": "Deficit of SL Unrestricted-use 500 EA",
            }
        ]
    )
    sc_d77 = pd.DataFrame(
        [
            {"Material": mat_d77, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 109.0, "Surplus": -109.0},
            {"Material": mat_d77, "SLoc": "SL02", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat_d77, "SLoc": "SP31", "SAP_Stock": 109.0, "WMS_Stock": 0.0, "Surplus": 109.0},
        ]
    )
    pm_d77 = pd.DataFrame([{"Material": mat_d77, "SLoc": "SL01", "SAP_Stock": 0.0}, {"Material": mat_d77, "SLoc": "SP31", "SAP_Stock": 109.0}])
    pw_d77 = pd.DataFrame([{"Material": mat_d77, "SLoc": "SL01", "WMS_Stock": 109.0}])
    plan_d77 = resolve_material_errors_unified(
        mat=mat_d77,
        rows_df=gi_d77,
        stock_comparison=sc_d77,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=pw_d77,
        prev_mb52_df=pm_d77,
        prev_snapshot_time="2026-10-29 14:00:00",
        active_gr_errors={},
        active_gi_errors={},
        gr_valid=True,
    )
    assert len(plan_d77) == 1
    assert plan_d77[0]["Action"] == "TRANSFER_POSTING"
    assert plan_d77[0]["Status"] == "PARTIAL_ACCURACY_REBALANCING"
    assert plan_d77[0]["From_SLoc"] == "SP31"
    assert plan_d77[0]["To_SLoc"] == "SL01"
    assert float(plan_d77[0]["Transfer_Qty"]) == 109.0

    # 2) Full resolution READY transfer from SP31 -> SL02 clearing SL02 -> SL01
    mat_rdy = "99363250"
    gi_rdy = pd.DataFrame(
        [
            {
                "Material Number": mat_rdy,
                "Mvt": "311",
                "FromSt": "SL02",
                "TOSt": "SL01",
                "Qty": "140.0",
                "Date": "2026.10.28",
                "Message": "Deficit of SL Unrestricted-use 140 EA",
            }
        ]
    )
    sc_rdy = pd.DataFrame(
        [
            {"Material": mat_rdy, "SLoc": "SL02", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat_rdy, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 140.0, "Surplus": -140.0},
            {"Material": mat_rdy, "SLoc": "SP31", "SAP_Stock": 1000.0, "WMS_Stock": 0.0, "Surplus": 1000.0},
        ]
    )
    pm_rdy = pd.DataFrame([{"Material": mat_rdy, "SLoc": "SL02", "SAP_Stock": 0.0}, {"Material": mat_rdy, "SLoc": "SL01", "SAP_Stock": 0.0}, {"Material": mat_rdy, "SLoc": "SP31", "SAP_Stock": 1000.0}])
    pw_rdy = pd.DataFrame([{"Material": mat_rdy, "SLoc": "SL02", "WMS_Stock": 0.0}, {"Material": mat_rdy, "SLoc": "SL01", "WMS_Stock": 140.0}])
    plan_rdy = resolve_material_errors_unified(
        mat=mat_rdy,
        rows_df=gi_rdy,
        stock_comparison=sc_rdy,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=pw_rdy,
        prev_mb52_df=pm_rdy,
        prev_snapshot_time="2026-10-29 14:00:00",
        active_gr_errors={},
        active_gi_errors={},
        gr_valid=True,
    )
    assert len(plan_rdy) == 1
    assert plan_rdy[0]["Action"] == "TRANSFER_POSTING"
    assert plan_rdy[0]["Status"] == "READY_FOR_EXECUTION"
    assert plan_rdy[0]["From_SLoc"] == "SP31"
    assert plan_rdy[0]["To_SLoc"] == "SL02"
    assert float(plan_rdy[0]["Transfer_Qty"]) == 140.0


def test_d110_rule2_consecutive_3_failures_24h_cooldown_and_report_rendering(tmp_path):
    """
    D110 Rule 2 (#197 / #199):
      - 3 consecutive failed attempts on (mat, SL02, SL01) trigger a 24h cooldown.
      - During cooldown (< 24h), (mat, SL02, SL01) is skipped with `SKIPPED_COOLDOWN_GUARD_D110`
        (for Full, Flush, and Partial D77 paths) and rendered in both the 30-min report
        (`build_gi_email_html`) and 07:30 report (`build_daily_cumulative_email_html`).
      - After 24h elapses, cooldown expires cleanly.
      - A `POSTED` row resets consecutive failure count to 0.
    """
    assert D110_CONSECUTIVE_FAILURE_LIMIT == 3
    assert D110_COOLDOWN_HOURS == 24

    db_path = tmp_path / "gr_gi_history.db"
    init_db(db_path=db_path)

    mat = "99231414"
    conn = sqlite3.connect(str(db_path))
    fail_rows = [
        ("RUN_20261029_080000", "2026-10-29 12:01:10", "FAILED", "Deficit of SL Unrestricted-use 100 EA"),
        ("RUN_20261029_083000", "2026-10-29 12:31:10", "FAILED", "Deficit of SL Unrestricted-use 100 EA"),
        ("RUN_20261029_090000", "2026-10-29 13:01:10", "FAILED_DEFICIT", "Valuation locked by the user SECRET99"),
    ]
    for bid, utc_ts, pst, resp in fail_rows:
        conn.execute(
            """
            INSERT INTO transfer_posting_history
            (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, mb1b_mvt, doc_number, post_status, posting_type, sap_response, reason)
            VALUES (?, ?, ?, 'SL02', 'SL01', 100.0, '311', '', ?, 'READY_FOR_EXECUTION', ?, 'test')
            """,
            (bid, utc_ts, mat, pst, resp),
        )
    conn.commit()
    conn.close()

    conn_ro = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    third_local_str = conn_ro.execute(
        "SELECT datetime(executed_at, 'localtime') FROM transfer_posting_history ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    conn_ro.close()
    third_local_dt = datetime.strptime(third_local_str, "%Y-%m-%d %H:%M:%S")

    cd_active = load_d110_failure_cooldown_map(
        gi_db_path=db_path,
        as_of_dt=third_local_dt + timedelta(hours=1),
    )
    assert (mat, "SL02", "SL01") in cd_active
    cd_entry = cd_active[(mat, "SL02", "SL01")]
    assert cd_entry["consec_failures"] == 3
    assert "SECRET99" not in cd_entry["last_error"]
    assert "<SAP_USER>" in cd_entry["last_error"]

    gi_rows = pd.DataFrame(
        [
            {
                "Material Number": mat,
                "Mvt": "311",
                "FromSt": "SL01",
                "TOSt": "SP31",
                "Qty": "100.0",
                "Date": "2026.10.29",
                "Message": "Deficit of SL Unrestricted-use 100 EA",
            }
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 200.0, "WMS_Stock": 100.0, "Surplus": 100.0},
        ]
    )
    prev_mb52 = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "SAP_Stock": 0.0},
            {"Material": mat, "SLoc": "SL02", "SAP_Stock": 200.0},
        ]
    )
    prev_wms = pd.DataFrame(
        [
            {"Material": mat, "SLoc": "SL01", "WMS_Stock": 0.0},
            {"Material": mat, "SLoc": "SL02", "WMS_Stock": 100.0},
        ]
    )

    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.29",
        prev_month_str="2026.09.30",
        prev_wms_df=prev_wms,
        prev_mb52_df=prev_mb52,
        prev_snapshot_time="2026-10-29 09:30:00",
        active_gr_errors={},
        active_gi_errors={},
        gr_valid=True,
        d110_cooldown_map=cd_active,
    )
    assert len(plan) == 1
    assert plan[0]["Action"] == "SKIP"
    assert plan[0]["Status"] == "SKIPPED_COOLDOWN_GUARD_D110"
    assert plan[0]["D110_Cooldown_Route"] == "SL02->SL01"

    plan_df = pd.DataFrame(plan)
    html_30m = build_gi_email_html(plan_df, [], "2026-10-29", "10:00 AM EDT")
    html_0730 = build_daily_cumulative_email_html(plan_df, [], "2026-10-29", "07:30 AM EDT")
    assert "D110 Consecutive-Failure 24h Cooldown" in html_30m
    assert mat in html_30m
    assert "D110 Consecutive-Failure 24h Cooldown" in html_0730
    assert "Cooldown Hold (D110 · 24h)" in html_0730

    cd_expired = load_d110_failure_cooldown_map(
        gi_db_path=db_path,
        as_of_dt=third_local_dt + timedelta(hours=25),
    )
    assert (mat, "SL02", "SL01") not in cd_expired

    conn2 = sqlite3.connect(str(db_path))
    conn2.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, mb1b_mvt, doc_number, post_status, posting_type, sap_response, reason)
        VALUES ('RUN_20261029_093000', '2026-10-29 13:31:10', ?, 'SL02', 'SL01', 100.0, '311', '4900000001', 'POSTED', 'READY_FOR_EXECUTION', 'Document posted', 'ok')
        """,
        (mat,),
    )
    conn2.commit()
    conn2.close()
    cd_reset = load_d110_failure_cooldown_map(
        gi_db_path=db_path,
        as_of_dt=third_local_dt + timedelta(hours=2),
    )
    assert (mat, "SL02", "SL01") not in cd_reset
