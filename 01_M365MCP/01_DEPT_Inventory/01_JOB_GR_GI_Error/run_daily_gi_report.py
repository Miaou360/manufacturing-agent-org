"""
D106 Standalone Daily GI Error Report Dispatcher (07:30 AM Daily + 08:00 AM 1-Time Retry)
=========================================================================================
01_DEPT_Inventory / 01_JOB_GR_GI_Error
- Completely decoupled from SAP GUI (reads SQLite DB & latest resolution plan log only).
- Reporting window: Previous calendar day 00:00:00 ~ 23:59:59 (local time) + latest active errors.
- Primary schedule: 07:30 AM daily.
- Failure fallback: 1 retry at 08:00 AM; if 08:00 retry also fails, writes handoff alert markdown.
"""

import argparse
import json
import logging
import os
import re as _re_out_guard
import subprocess
import sys as _sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd
from dotenv import load_dotenv as _load_dotenv

_ENV_LOADED_PATH = None
for _idx_arg, _a in enumerate(_sys.argv):
    if _a == "--ops-root" and _idx_arg + 1 < len(_sys.argv):
        _cand_env = Path(_sys.argv[_idx_arg + 1]).resolve()
        for _ep in [_cand_env, *_cand_env.parents]:
            if (_ep / ".env").exists():
                _load_dotenv(_ep / ".env")
                _ENV_LOADED_PATH = _ep / ".env"
                break
        break
if _ENV_LOADED_PATH is None:
    for _ep in Path(__file__).resolve().parents:
        if (_ep / ".env").exists():
            _load_dotenv(_ep / ".env")
            _ENV_LOADED_PATH = _ep / ".env"
            break

_DUMMY_ENV_VALS = {
    "P001", "P002", "0000000", "9900000", "Enterprise SSO",
    "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT",
    "ACME", "ACME Manufacturing", "ACME Corp", "ACME America", "MAT-0000", "TEST-000",
}


def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val


_OUT_DUMMY_RE = _re_out_guard.compile(
    r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)",
    _re_out_guard.IGNORECASE,
)


def _assert_clean_ops_output(*texts: str, context: str = "email_output") -> None:
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return
    for _t in texts:
        if _t:
            _m = _OUT_DUMMY_RE.search(str(_t))
            if _m:
                raise RuntimeError(f"[OUTPUT GUARD CRITICAL] Dummy token '{_m.group(0)}' detected in {context} ({__file__})!")


def _verify_ops_env_log(job_name: str = "01_DAILY_GI_REPORT_0730") -> str:
    _req = (
        "SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME", "SAP_SSO_WINDOW_TITLE",
        "WMS_BASE_URL", "COMPANY_NAME", "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT",
    )
    _st = []
    for _k in _req:
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg


BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in _sys.path:
    _sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in _sys.path:
    _sys.path.insert(0, str(BASE_DIR.parent))
from report_labels import check_report_language

LOG_DIR = BASE_DIR / "resolution_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(_sys.stdout),
        logging.FileHandler(LOG_DIR / "daily_gi_report_0730.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("DailyGIReport0730")

import importlib.util as _ilu_dbm
_spec_dbm = _ilu_dbm.spec_from_file_location("_job01_db_manager", Path(__file__).resolve().parent / "db_manager.py")
db_manager = _ilu_dbm.module_from_spec(_spec_dbm)
_spec_dbm.loader.exec_module(db_manager)
from gi_notifier import DEFAULT_TO, build_daily_cumulative_email_html, is_valid_recipient
from d107_delete_candidates import (
    collect_fresh_post_0700_d107,
    load_or_evaluate_d107_readonly,
    write_d107_post_0800_change_alert,
)


def load_latest_plan_df_readonly(
    resolution_logs_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> pd.DataFrame:
    """
    Loads the most recent GI_Resolution_Plan_*.xlsx written by the hourly daemon (e.g. 07:00 cycle).
    Falls back to gi_error_snapshots in gr_gi_history.db if no Excel plan file is present.
    Never touches SAP GUI.
    """
    target_dir = Path(resolution_logs_dir) if resolution_logs_dir is not None else LOG_DIR
    plan_files = sorted(target_dir.glob("GI_Resolution_Plan_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    if plan_files:
        try:
            df = pd.read_excel(plan_files[0])
            logger.info(f"[D106] Loaded latest active resolution plan: {plan_files[0].name} ({len(df)} rows)")
            # Filter out rows that were already executed in that latest batch (Action == TRANSFER_POSTING)
            if not df.empty and "Action" in df.columns:
                df = df[df["Action"] != "TRANSFER_POSTING"].copy()
            return df
        except Exception as e_xl:
            logger.warning(f"[D106] Could not read {plan_files[0].name}, falling back to DB snapshot: {e_xl}")

    # Fallback: reconstruct minimal plan_df from latest gi_error_snapshots in DB
    snap_df = db_manager.get_active_error_summary(db_path=db_path)
    if snap_df is None or snap_df.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "Material": snap_df["material"].astype(str),
        "Mvt_Original": snap_df["mvt"].astype(str),
        "Deficit_Loc": snap_df["from_sloc"].astype(str),
        "Transfer_Qty": pd.to_numeric(snap_df["qty"], errors="coerce").fillna(0.0),
        "Action": "SKIP",
        "Status": "DB_SNAPSHOT_FALLBACK",
        "Reason": snap_df["message"].fillna("Remaining GI error from latest DB snapshot").astype(str),
    })


def write_handoff_failure_alert(
    report_date: str,
    prev_date: str,
    error_msg: str,
    handoff_dir: Optional[Path] = None,
) -> Path:
    """
    D106: Called ONLY when both 07:30 primary send and 08:00 1-time retry fail.
    Writes a sanitized failure notice to .agents/handoff/ (or resolution_logs/ fallback).
    """
    repo_root = BASE_DIR.parents[2]
    target_handoff = Path(handoff_dir) if handoff_dir is not None else (repo_root / ".agents" / "handoff")
    ymd = report_date.replace("-", "")
    hm = datetime.now().strftime("%H%M")
    if target_handoff.exists():
        out_path = target_handoff / f"ALERT_D106_{ymd}_{hm}_antigravity_to_claude_gi-daily-report-retry-failed.md"
    else:
        out_path = LOG_DIR / f"ALERT_D106_{ymd}_{hm}_gi-daily-report-retry-failed.md"

    safe_err = _re_out_guard.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<redacted_email>", str(error_msg))
    content = (
        f"---\n"
        f'thread: "D106-ALERT"\n'
        f"from: antigravity (01_JOB D106 Watchdog)\n"
        f"to: claude, user\n"
        f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')} local\n"
        f"type: bug\n"
        f"needs_user_decision: false\n"
        f"---\n\n"
        f"# 🚨 [D106 발송 실패 알림] 01 전날({prev_date}) GI 일일 보고 07:30 본발송 및 08:00 재시도 모두 실패\n\n"
        f"- **보고 기준일**: `{report_date}` (집계 대상 전일: `{prev_date} 00:00:00 ~ 23:59:59`)\n"
        f"- **상태**: `RETRY_FAILED` (07:30 1차 발송 실패 후 08:00 1회 재시도까지 실패)\n"
        f"- **오류 원문**: `{safe_err}`\n"
    )
    out_path.write_text(content, encoding="utf-8")
    logger.error(f"[D106 ALERT] Wrote retry failure record to: {out_path}")
    return out_path


def _send_via_outlook_com(to_list: List[str], subject: str, html_body: str) -> None:
    res = subprocess.run(
        'tasklist /fi "imagename eq OUTLOOK.EXE"',
        shell=True,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if "OUTLOOK.EXE" not in (res.stdout or ""):
        logger.info("[D106] Launching Outlook.Application background process...")
        subprocess.Popen("start outlook", shell=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(4)

    try:
        import pythoncom
        pythoncom.CoInitialize()
        has_com = True
    except Exception:
        has_com = False

    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = outlook.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass

        mail = outlook.CreateItem(0)
        _assert_clean_ops_output(subject, html_body, context="01_JOB D106 run_daily_gi_report")
        mail.Subject = subject
        mail.To = "; ".join(to_list)
        mail.CC = ""
        mail.BCC = ""
        mail.HTMLBody = html_body
        mail.Send()
    finally:
        if has_com:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def dispatch_daily_gi_report(
    slot: str = "auto",
    report_date: Optional[str] = None,
    dry_run: bool = False,
    test_mode: bool = False,
    db_path: Optional[Path] = None,
    resolution_logs_dir: Optional[Path] = None,
    handoff_dir: Optional[Path] = None,
    ops_raw_db_path: Optional[Path] = None,
) -> dict:
    """
    Executes D106 standalone daily GI report dispatch.
    - slot='0730': Primary 07:30 AM send. On failure records FAILED_0730 (08:00 trigger will retry).
    - slot='0800_retry': 08:00 AM 1-time retry ONLY if today's report is not yet SENT/RETRY_SENT.
    """
    _verify_ops_env_log("01_DAILY_GI_REPORT_0730")
    now = datetime.now()
    rep_dt = datetime.strptime(report_date, "%Y-%m-%d") if report_date else now
    rep_date_str = rep_dt.strftime("%Y-%m-%d")
    prev_date_str = (rep_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    eff_slot = slot
    if eff_slot == "auto":
        eff_slot = "0800_retry" if now.hour == 8 else "0730"

    # Check existing dispatch state in production mode (skip state gate in dry_run / test_mode)
    if not dry_run and not test_mode:
        state = db_manager.get_daily_report_dispatch_state(rep_date_str, db_path=db_path)
        if state and state.get("status") in ("SENT", "RETRY_SENT"):
            logger.info(
                f"[D106] Report for {rep_date_str} already dispatched ({state.get('status')} at {state.get('sent_at')}). Exiting cleanly."
            )
            return {"status": "ALREADY_SENT", "report_date": rep_date_str, "prev_date": prev_date_str}
        if eff_slot == "0800_retry" and state and state.get("status") == "RETRY_FAILED":
            logger.warning(f"[D106] 08:00 retry already failed once for {rep_date_str}. Suppressing further retries.")
            return {"status": "ALREADY_RETRY_FAILED", "report_date": rep_date_str, "prev_date": prev_date_str}

    # Read-only DB queries for previous calendar day 00:00:00 ~ 23:59:59 local time
    postings_df = db_manager.get_prev_day_postings(prev_date_str, db_path=db_path)
    metrics_prev = db_manager.get_prev_day_metrics_summary(prev_date_str, db_path=db_path)
    runs_df = db_manager.get_prev_day_run_history(prev_date_str, limit=48, db_path=db_path)
    plan_df = load_latest_plan_df_readonly(resolution_logs_dir=resolution_logs_dir, db_path=db_path)

    sched_label = "07:30"
    rep_time_str = now.strftime("%I:%M %p")
    base_subject = f"[GI Error] Daily Comprehensive Report (Prev-Day {prev_date_str} Resolution & Status) - {rep_date_str} 07:30 AM"

    # Recipient resolution (Guardrail #10 enforced for test_mode)
    if test_mode:
        raw_test = (
            os.getenv("TEST_EMAIL_RECIPIENT")
            or os.getenv("GI_ERROR_REPORT_TO")
            or os.getenv("STOCK_ACCURACY_VERIFY", "")
        )
        single_user = raw_test.replace(";", ",").split(",")[0].strip() if raw_test else ""
        to_list = [single_user] if is_valid_recipient(single_user) else []
        subject = f"[TEST] {base_subject}"
    else:
        to_list = [r for r in DEFAULT_TO if is_valid_recipient(r)]
        subject = base_subject

    eff_gi_db = Path(db_path) if db_path is not None else (BASE_DIR / "db" / "gr_gi_history.db")
    eff_logs_dir = Path(resolution_logs_dir) if resolution_logs_dir is not None else LOG_DIR
    d107_payload = load_or_evaluate_d107_readonly(
        report_date_str=rep_date_str,
        gi_db_path=eff_gi_db,
        gi_plan_dir=eff_logs_dir,
        ops_raw_db_path=ops_raw_db_path,
    )
    try:
        db_draft_summary = db_manager.get_d107_draft_status_summary(
            report_date_str=rep_date_str,
            db_path=eff_gi_db,
        )
        p2_json = eff_logs_dir / f"d107_phase2a_{rep_date_str.replace('-', '')}.json"
        p2_data = json.loads(p2_json.read_text(encoding="utf-8")) if p2_json.exists() else {}
        d107_payload["phase2b_summary"] = {
            "mode": p2_data.get("mode") or "save_temp_2b",
            "status": p2_data.get("status") or "NOT_RUN_YET",
            "saved_draft_count": int(p2_data.get("saved_temp_draft_count", p2_data.get("completed_draft_count", 0)) or 0),
            "saved_row_count": int(p2_data.get("saved_temp_row_count", db_draft_summary.get("today_saved_count", 0)) or 0),
            "draft_subject": p2_data.get("draft_subject") or f"Request to delete IWMS_GI of {rep_date_str.replace('-', '/')}",
            "prior_cohort_date": db_draft_summary.get("prior_cohort_date") or prev_date_str,
            "prior_cleared_count": int(db_draft_summary.get("prior_cleared_count", 0) or 0),
            "prior_pending_count": int(db_draft_summary.get("prior_pending_count", 0) or 0),
            "prior_stale_count": int(db_draft_summary.get("prior_stale_count", 0) or 0),
            "prior_stale_labels": list(db_draft_summary.get("prior_stale_labels") or []),
        }
    except Exception as e_p2:
        logger.warning(f"[D106] Could not load D107 2b draft status summary: {e_p2}")

    html_body = build_daily_cumulative_email_html(
        plan_df=plan_df,
        posting_results=[],
        report_date=rep_date_str,
        report_time=rep_time_str,
        postings_24h_df=postings_df,
        metrics_24h=metrics_prev,
        runs_24h_df=runs_df,
        target_prev_date=prev_date_str,
        schedule_time_label=sched_label,
        d107_payload=d107_payload,
    )
    _assert_clean_ops_output(subject, html_body, context="01_JOB D106 dry/live output")

    active_rem_cnt = 0
    if plan_df is not None and not plan_df.empty and "Mvt_Original" in plan_df.columns:
        mvt_s = plan_df["Mvt_Original"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        active_rem_cnt = int((~mvt_s.isin(["901", "902"])).sum())

    summary = {
        "report_date": rep_date_str,
        "prev_date": prev_date_str,
        "period": f"{prev_date_str} 00:00:00 ~ {prev_date_str} 23:59:59 (local)",
        "slot": eff_slot,
        "subject": subject,
        "total_runs_prev_day": int(metrics_prev.get("total_runs", 0)),
        "successful_postings_prev_day": int(metrics_prev.get("successful_postings", 0)),
        "total_posted_qty_prev_day": float(metrics_prev.get("total_posted_qty", 0.0)),
        "current_remaining_errors": active_rem_cnt,
        "recipient_count": len(to_list),
    }
    logger.info(
        f"[D106 SUMMARY] period='{summary['period']}' | runs={summary['total_runs_prev_day']} | "
        f"postings={summary['successful_postings_prev_day']} ({summary['total_posted_qty_prev_day']} EA) | "
        f"remaining_errors={summary['current_remaining_errors']} | subject='{subject}'"
    )

    summary["d107_candidate_count"] = int(d107_payload.get("candidate_count", 0))
    summary["d107_hold_count"] = int(d107_payload.get("hold_count", 0))
    summary["d107_cleared_by_0700_count"] = int(d107_payload.get("cleared_by_0700_count", 0))
    summary["d107_collection_time_edt"] = d107_payload.get("collection_time_edt")

    if dry_run:
        scratch_dir = BASE_DIR / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        preview_file = scratch_dir / f"DRYRUN_Daily_GI_Report_{rep_date_str.replace('-', '')}_{now.strftime('%H%M%S')}.html"
        preview_file.write_text(html_body, encoding="utf-8")
        summary["scratch_html_path"] = str(preview_file)
        summary["dry_run_preview_html"] = str(preview_file)
        summary["status"] = "DRY_RUN_OK"
        return summary

    if not to_list:
        err_msg = "No valid recipient email configured (fail closed)."
        logger.error(f"[D106] ❌ {err_msg}")
        if not test_mode:
            fail_st = "RETRY_FAILED" if eff_slot == "0800_retry" else "FAILED_0730"
            db_manager.record_daily_report_dispatch_attempt(
                rep_date_str, prev_date_str, fail_st, subject=subject, recipients=[], error_msg=err_msg, db_path=db_path
            )
            if fail_st == "RETRY_FAILED":
                write_handoff_failure_alert(rep_date_str, prev_date_str, err_msg, handoff_dir=handoff_dir)
        summary["status"] = "FAILED_NO_RECIPIENT"
        return summary

    try:
        check_report_language("01_JOB_D106_daily_gi_report", subject=subject, html_body=html_body, is_user_only=test_mode)
        _send_via_outlook_com(to_list=to_list, subject=subject, html_body=html_body)
        logger.info(f"[D106] ✅ Email dispatched ({eff_slot}, test_mode={test_mode}) to <redacted:{len(to_list)}_recipient(s)>.")
        if not test_mode:
            ok_st = "RETRY_SENT" if eff_slot == "0800_retry" else "SENT"
            db_manager.record_daily_report_dispatch_attempt(
                rep_date_str, prev_date_str, ok_st, subject=subject, recipients=to_list, error_msg="", db_path=db_path
            )
            slot_key = f"DAILY_{rep_date_str.replace('-', '')}_0730"
            db_manager.record_email_slot_sent(slot_key, "DAILY_CUMULATIVE_D106", subject, to_list)
            summary["status"] = ok_st
        else:
            summary["status"] = "TEST_SENT"
        return summary
    except Exception as exc:
        err_str = str(exc)
        logger.error(f"[D106] ❌ Outlook dispatch failed ({eff_slot}): {err_str}")
        if not test_mode:
            fail_st = "RETRY_FAILED" if eff_slot == "0800_retry" else "FAILED_0730"
            db_manager.record_daily_report_dispatch_attempt(
                rep_date_str, prev_date_str, fail_st, subject=subject, recipients=to_list, error_msg=err_str, db_path=db_path
            )
            if fail_st == "RETRY_FAILED":
                alert_file = write_handoff_failure_alert(rep_date_str, prev_date_str, err_str, handoff_dir=handoff_dir)
                summary["alert_file"] = str(alert_file)
            summary["status"] = fail_st
        else:
            summary["status"] = "TEST_FAILED"
        summary["error"] = err_str
        return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="D106 Standalone Daily GI Error Report (07:30 AM + 08:00 Retry)")
    parser.add_argument("--slot", choices=["auto", "0730", "0800_retry"], default="auto")
    parser.add_argument("--report-date", default=None, help="Override report date YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Render & validate without sending email or writing DB")
    parser.add_argument("--test", action="store_true", help="Send [TEST] email to user only (Guardrail #10)")
    parser.add_argument("--db-path", default=None, help="Optional override path to gr_gi_history.db")
    parser.add_argument("--resolution-logs-dir", default=None, help="Optional override path to resolution_logs dir")
    parser.add_argument("--ops-root", default=None, help="Optional operational 01_DEPT_Inventory root for worktree dry-run")
    parser.add_argument("--collect-d107", action="store_true", help="D107 (#182): Collect fresh post-07:00 ZTCODE_STOCK_REC+MB52+WMS at ~07:20")
    parser.add_argument("--check-post-0800", action="store_true", help="D107 (#182): Run post-08:00 candidate status comparison & alert")
    args = parser.parse_args()

    ops_dept = Path(args.ops_root).resolve() if args.ops_root else None
    eff_db = Path(args.db_path) if args.db_path else (ops_dept / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db" if ops_dept else None)
    eff_logs = Path(args.resolution_logs_dir) if args.resolution_logs_dir else (ops_dept / "01_JOB_GR_GI_Error" / "resolution_logs" if ops_dept else None)
    eff_ops_raw = (ops_dept.parent / "00_HQ_TaskManager" / "data_collector" / "db" / "ops_raw.db") if ops_dept else None

    if args.collect_d107:
        _verify_ops_env_log("01_D107_FRESH_0720")
        d107_res = collect_fresh_post_0700_d107(
            report_date=args.report_date,
            dry_run=args.dry_run,
            gi_db_path=eff_db or (BASE_DIR / "db" / "gr_gi_history.db"),
            gi_plan_dir=eff_logs or LOG_DIR,
            ops_raw_db_path=eff_ops_raw,
        )
        logger.info(
            f"[D107 COLLECT DONE] time={d107_res.get('collection_time_edt')} | "
            f"cleared_0700={d107_res.get('cleared_by_0700_count')} | "
            f"candidates={d107_res.get('candidate_count')} | holds={d107_res.get('hold_count')}"
        )
        _sys.exit(0)

    if args.check_post_0800:
        _verify_ops_env_log("01_D107_POST0800_CHECK")
        rep_d = args.report_date or datetime.now().strftime("%Y-%m-%d")
        d107_res = load_or_evaluate_d107_readonly(
            report_date_str=rep_d,
            gi_db_path=eff_db or (BASE_DIR / "db" / "gr_gi_history.db"),
            gi_plan_dir=eff_logs or LOG_DIR,
            ops_raw_db_path=eff_ops_raw,
        )
        alert_p = write_d107_post_0800_change_alert(d107_res, dry_run=args.dry_run)
        logger.info(
            f"[D107 POST-08:00 CHECK] changed={d107_res.get('post_0800_changed_count', 0)} | alert={alert_p}"
        )
        _sys.exit(0)

    res = dispatch_daily_gi_report(
        slot=args.slot,
        report_date=args.report_date,
        dry_run=args.dry_run,
        test_mode=args.test,
        db_path=eff_db,
        resolution_logs_dir=eff_logs,
        ops_raw_db_path=eff_ops_raw,
    )
    if str(res.get("status", "")).startswith("FAILED") or res.get("status") in ("RETRY_FAILED", "TEST_FAILED"):
        _sys.exit(1)
