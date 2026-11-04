import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

JOB01_DIR = Path(__file__).resolve().parent.parent
if str(JOB01_DIR) not in sys.path:
    sys.path.insert(0, str(JOB01_DIR))

import db_manager
import run_daily_gi_report as d106


def _seed_prev_day_db(db_file: Path):
    db_manager.init_db(db_path=db_file)
    conn = sqlite3.connect(str(db_file))
    # Insert 2 runs inside 2026-10-28 local time, and 2 runs outside (2026-10-27 23:00 local, 2026-10-29 01:00 local)
    # Using 'utc' modifier in SQLite so datetime(ts, 'localtime') maps back to the exact local timestamp!
    conn.executemany(
        """
        INSERT INTO pipeline_run_metrics (batch_id, run_timestamp, plant, total_gi_errors, postings_executed, elapsed_seconds, status)
        VALUES (?, datetime(?, 'utc'), 'P001', ?, ?, ?, 'COMPLETED')
        """,
        [
            ("RUN_20261027_230000", "2026-10-27 23:02:00", 5, 1, 100.0),
            ("RUN_20261028_000000", "2026-10-28 00:02:00", 10, 1, 120.0),
            ("RUN_20261028_230000", "2026-10-28 23:58:00", 11, 1, 130.0),
            ("RUN_20261029_010000", "2026-10-29 01:02:00", 13, 1, 110.0),
        ],
    )
    conn.executemany(
        """
        INSERT INTO transfer_posting_history (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, sap_response, reason)
        VALUES (?, datetime(?, 'utc'), ?, 'SL01', 'SP31', ?, ?, 'POSTED', 'READY_FOR_EXECUTION', 'Posted', 'Test')
        """,
        [
            ("RUN_20261027_230000", "2026-10-27 23:59:59", "99000011", 50.0, "4990000001"),
            ("RUN_20261028_000000", "2026-10-28 00:00:05", "99000022", 30.0, "4990000002"),
            ("RUN_20261028_230000", "2026-10-28 23:59:50", "99000033", 70.0, "4990000003"),
            ("RUN_20261029_010000", "2026-10-29 00:00:05", "99000044", 90.0, "4990000004"),
        ],
    )
    conn.commit()
    conn.close()


def test_d106_zero_sap_imports_and_exact_prev_day_window(tmp_path):
    """D106: Standalone dispatcher never imports SAP modules and strictly filters prev_date 00:00~23:59 local."""
    chk = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, r'{JOB01_DIR}'); import run_daily_gi_report; assert not (set(['sap_launcher', 'ztcode_gi_collector', 'mb1b_executor']) & set(sys.modules))"], capture_output=True, text=True)
    assert chk.returncode == 0, chk.stderr

    db_file = tmp_path / "test_gi.db"
    _seed_prev_day_db(db_file)
    logs_dir = tmp_path / "res_logs"
    logs_dir.mkdir()
    pd.DataFrame([
        {"Material": "99000055", "Mvt_Original": 311, "Deficit_Loc": "SP31", "Transfer_Qty": 25.0, "Action": "SKIP", "Status": "INSUFFICIENT_SAP_STOCK", "Reason": "Short"},
        {"Material": "99000066", "Mvt_Original": 901, "Deficit_Loc": "SL01", "Transfer_Qty": 10.0, "Action": "SKIP", "Status": "EXCLUDED_901", "Reason": "Scrap"},
    ]).to_excel(logs_dir / "GI_Resolution_Plan_20261029_070200.xlsx", index=False)

    res = d106.dispatch_daily_gi_report(
        slot="0730",
        report_date="2026-10-29",
        dry_run=True,
        db_path=db_file,
        resolution_logs_dir=logs_dir,
    )
    assert res["status"] == "DRY_RUN_OK"
    assert res["prev_date"] == "2026-10-28"
    assert res["total_runs_prev_day"] == 2
    assert res["successful_postings_prev_day"] == 2
    assert res["total_posted_qty_prev_day"] == 100.0
    assert res["current_remaining_errors"] == 1  # 901 excluded


def test_d106_0730_success_skips_0800_retry(tmp_path):
    """D106: When 07:30 succeeds, 08:00 retry exits immediately without sending duplicate email."""
    db_file = tmp_path / "test_gi2.db"
    _seed_prev_day_db(db_file)
    logs_dir = tmp_path / "res_logs2"
    logs_dir.mkdir()

    sent_calls = []
    with patch.object(d106, "_send_via_outlook_com", side_effect=lambda **kw: sent_calls.append(kw)),          patch.object(d106, "DEFAULT_TO", [("ops.user" + "@" + "example.org")]):
        r1 = d106.dispatch_daily_gi_report(slot="0730", report_date="2026-10-29", db_path=db_file, resolution_logs_dir=logs_dir)
        assert r1["status"] == "SENT"
        assert len(sent_calls) == 1

        r2 = d106.dispatch_daily_gi_report(slot="0800_retry", report_date="2026-10-29", db_path=db_file, resolution_logs_dir=logs_dir)
        assert r2["status"] == "ALREADY_SENT"
        assert len(sent_calls) == 1


def test_d106_0730_fail_triggers_single_0800_retry_and_handoff_alert_on_second_fail(tmp_path):
    """D106: If 07:30 fails, 08:00 retries once; if 08:00 also fails, writes handoff alert and blocks 3rd retry."""
    db_file = tmp_path / "test_gi3.db"
    _seed_prev_day_db(db_file)
    logs_dir = tmp_path / "res_logs3"
    logs_dir.mkdir()
    handoff_dir = tmp_path / "handoff"
    handoff_dir.mkdir()

    with patch.object(d106, "_send_via_outlook_com", side_effect=RuntimeError("Outlook RPC unavailable")),          patch.object(d106, "DEFAULT_TO", [("ops.user" + "@" + "example.org")]):
        r1 = d106.dispatch_daily_gi_report(
            slot="0730", report_date="2026-10-29", db_path=db_file, resolution_logs_dir=logs_dir, handoff_dir=handoff_dir
        )
        assert r1["status"] == "FAILED_0730"
        assert list(handoff_dir.glob("ALERT_D106_*.md")) == []

        r2 = d106.dispatch_daily_gi_report(
            slot="0800_retry", report_date="2026-10-29", db_path=db_file, resolution_logs_dir=logs_dir, handoff_dir=handoff_dir
        )
        assert r2["status"] == "RETRY_FAILED"
        alerts = list(handoff_dir.glob("ALERT_D106_*.md"))
        assert len(alerts) == 1
        assert "2026-10-28" in alerts[0].read_text(encoding="utf-8")

        r3 = d106.dispatch_daily_gi_report(
            slot="0800_retry", report_date="2026-10-29", db_path=db_file, resolution_logs_dir=logs_dir, handoff_dir=handoff_dir
        )
        assert r3["status"] == "ALREADY_RETRY_FAILED"


def test_d106_scheduler_xml_has_battery_settings_and_0730_0800_triggers():
    """D106 + 154-A: Verify Task Scheduler XML has battery flags false, InteractiveToken, and 07:30 + 08:00 triggers."""
    ps1 = JOB01_DIR / "register_gi_report_0730_task.ps1"
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), "-PrintXmlOnly"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    xml = proc.stdout
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<StartBoundary>2026-10-30T07:30:00</StartBoundary>" in xml
    assert "<StartBoundary>2026-10-30T08:00:00</StartBoundary>" in xml

def test_d106_renders_d107_2b_oneline_temp_save_and_user_submit_status_english(tmp_path):
    """#233: 07:30 D106 report renders 1-line D107 2b temp-save & user-submit reconciliation status in 100% English."""
    import json
    from report_labels import assert_no_hangul

    db_file = tmp_path / "test_gi_2b.db"
    _seed_prev_day_db(db_file)

    # Seed 2 rows saved yesterday (1 cleared after user submit, 1 still pending) + 1 row saved today
    db_manager.record_d107_temp_draft_rows(
        rows=[
            {"sumno": "S_Y1", "barcode": "B_Y1", "material": "MAT_Y1", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 100.0, "post_date": "20261027"},
            {"sumno": "S_Y2", "barcode": "B_Y2", "material": "MAT_Y2", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 200.0, "post_date": "20261027"},
        ],
        draft_subject="Request to delete IWMS_GI of 2026/10/28",
        saved_at="2026-10-28 07:24:00",
        db_path=db_file,
    )
    # Reconcile on 2026-10-29: MAT_Y1 disappeared from portal & ZTCODE_STOCK_REC -> cleared; MAT_Y2 still on portal -> pending
    pending_key_y2 = db_manager.build_d107_draft_row_key("S_Y2", "B_Y2", "MAT_Y2", "311", 200.0)
    db_manager.reconcile_d107_temp_drafts(
        portal_row_keys={pending_key_y2},
        latest_01_gi_records=[{"material": "MAT_Y2", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 200.0}],
        report_date_str="2026-10-29",
        checked_at="2026-10-29 07:23:30",
        db_path=db_file,
    )
    db_manager.record_d107_temp_draft_rows(
        rows=[
            {"sumno": "S_T1", "barcode": "B_T1", "material": "MAT_T1", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 772.0, "post_date": "20261028"},
        ],
        draft_subject="Request to delete IWMS_GI of 2026/10/29",
        saved_at="2026-10-29 07:24:10",
        db_path=db_file,
    )

    logs_dir = tmp_path / "res_logs_2b"
    logs_dir.mkdir()
    (logs_dir / "d107_phase1_20261029.json").write_text(
        json.dumps({"report_date": "2026-10-29", "prev_date": "2026-10-28", "candidates": [], "holds": []}),
        encoding="utf-8",
    )
    (logs_dir / "d107_phase2a_20261029.json").write_text(
        json.dumps({
            "status": "SAVE_TEMP_2B_OK",
            "mode": "save_temp_2b",
            "saved_temp_draft_count": 1,
            "saved_temp_row_count": 1,
            "draft_subject": "Request to delete IWMS_GI of 2026/10/29",
        }),
        encoding="utf-8",
    )

    res = d106.dispatch_daily_gi_report(
        slot="0730",
        report_date="2026-10-29",
        dry_run=True,
        db_path=db_file,
        resolution_logs_dir=logs_dir,
    )
    html_out = Path(res["scratch_html_path"]).read_text(encoding="utf-8")
    assert "07:23 Portal Temp-Save (D107 2b" in html_out
    assert "Request to delete IWMS_GI of 2026/10/29" in html_out
    assert "<strong>1</strong> cleared after user submit" in html_out
    assert "<strong>1</strong> still pending user submit" in html_out
    assert_no_hangul({"subject": res["subject"], "html_body": html_out})

