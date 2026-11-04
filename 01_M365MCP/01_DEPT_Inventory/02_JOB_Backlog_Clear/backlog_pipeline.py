"""
Master Pipeline Orchestrator for 02_Backlog
===========================================
01_Inventory_Team / 02_Backlog (Plant P001 Headlamp)

Execution Flow & Domain Rules:
1. 선행조건 보장:
   - 01_GR_GI_Error의 GI 에러 자동 해소 파이프라인이 선행 완료된 후 실행.
2. 동시 시점 재고 스냅샷 수집:
   - WMS 실재고 & SAP MB52 동시 스냅샷 수집 및 품번별/창고별 정확도 분석.
3. SAP COGI 백로그 추출:
   - Plant P001, MRP Controller i01~i03, 당월 1일 ~ 현재일.
   - Excel 추출 후 COGI 창 즉시 탈출 (/n).
4. 재고정확도 보존 가드레일 시뮬레이션:
   - ΔV = V_after - V_before <= 0 조건을 만족하는 이동만 승인 (TRANSFER_POSTING).
   - 오차 증가 위험 건은 안전하게 차단 (SKIP).
5. SAP MB1B 311 이동전기:
   - Header Text: "Backlog_Clear", Recipient: "AUTO_AGENT"
   - 수하창고: SP31, SP11, SL49
6. SAP COGI 재처리 확정 (Save):
   - 이동전기 완료 후 다시 COGI 진입 -> 전체선택(btn[9]) -> 저장(btn[11]) -> /n 탈출.
7. 아침 일일 미처리 보고서 발송:
   - 매일 아침 08:00 AM 2차 실행 완료 시점에만 Outlook 이메일 + 엑셀 발송.
8. 정정/검증 재생성 전용 경로 (--report-only --as-of <run_id>):
   - SAP 접속·COGI 저장·이동전기·DB 쓰기 없이 저장된 플랜·COGI·재고 파일 및 읽기 전용 DB(?mode=ro)만 읽어 보고서 재생성/발송 (D108).
"""

import os
import re
import sys
import time
import sqlite3
import argparse
import logging
from pathlib import Path
import sys as _sys
from dotenv import load_dotenv as _load_dotenv
_ENV_LOADED_PATH = None
for _ep in Path(__file__).resolve().parents:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        _ENV_LOADED_PATH = _ep / ".env"
        break
if not _ENV_LOADED_PATH and "--ops-root" in _sys.argv:
    _or_idx = _sys.argv.index("--ops-root")
    if _or_idx + 1 < len(_sys.argv):
        for _ep in Path(_sys.argv[_or_idx + 1]).resolve().parents:
            if (_ep / ".env").exists():
                _load_dotenv(_ep / ".env")
                _ENV_LOADED_PATH = _ep / ".env"
                break
_DUMMY_ENV_VALS = {"P001", "P002", "0000000", "9900000", "Enterprise SSO", "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT", "ACME", "ACME Manufacturing", "ACME Corp", "ACME America", "MAT-0000", "TEST-000"}
def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val
import re as _re_out_guard
_OUT_DUMMY_RE = _re_out_guard.compile(r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)", _re_out_guard.IGNORECASE)
def _assert_clean_ops_output(*texts: str, context: str = "email_output") -> None:
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return
    for _t in texts:
        if _t:
            _m = _OUT_DUMMY_RE.search(str(_t))
            if _m:
                raise RuntimeError(f"[OUTPUT GUARD CRITICAL] Dummy token '{_m.group(0)}' detected in {context} ({__file__})!")
def _verify_ops_env_log(job_name: str = "PIPELINE") -> str:
    _req = ("SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME", "SAP_SSO_WINDOW_TITLE", "WMS_BASE_URL", "COMPANY_NAME", "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT")
    _st = []
    for _k in _req:
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
GR_GI_DIR = BASE_DIR.parent / "01_JOB_GR_GI_Error"

from sap_launcher import ensure_sap_open
from db_manager import (
    init_db, log_cogi_snapshot, log_daily_metric, log_raw_backlog_history,
    get_daily_transfers, get_daily_initial_backlog
)
from cogi_collector import export_cogi_errors, post_and_save_cogi, refresh_and_extract_initial_backlog
from backlog_resolver import build_backlog_resolution_plan
from mb1b_backlog_executor import execute_backlog_batch
from notifier import send_morning_backlog_report

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("BacklogPipeline")

PRECURSOR_MAX_AGE_MINUTES = 25.0
PRECURSOR_MIN_ELAPSED_MINUTES = 10.0
PRECURSOR_SETTLE_MINUTES = 10.0


def check_recent_gi_run(max_age_minutes: float = PRECURSOR_MAX_AGE_MINUTES) -> Optional[dict]:
    """Checks if a successful GI error pipeline completed within max_age_minutes."""
    gi_db = GR_GI_DIR / "db" / "gr_gi_history.db"
    if not gi_db.exists():
        return None
    try:
        with sqlite3.connect(str(gi_db), timeout=5.0) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT batch_id, datetime(run_timestamp, 'localtime') as run_time,
                       postings_executed, elapsed_seconds, status
                FROM pipeline_run_metrics
                WHERE plant = ? AND status = 'COMPLETED'
                ORDER BY id DESC LIMIT 1
            """, (_get_ops_env("SAP_PLANT", "P001"),))
            row = cursor.fetchone()
            if not row:
                return None

            run_dt = datetime.strptime(row[1], "%Y-%m-%d %H:%M:%S")
            age_seconds = (datetime.now() - run_dt).total_seconds()
            if 0 <= age_seconds <= (max_age_minutes * 60):
                return {
                    "batch_id": row[0],
                    "run_time": row[1],
                    "postings_executed": row[2] or 0,
                    "elapsed_seconds": row[3],
                    "status": row[4],
                    "age_minutes": round(age_seconds / 60.0, 1),
                    "age_seconds": age_seconds
                }
    except Exception as e:
        logger.warning(f"⚠️ Could not check recent GI run: {e}")
    return None


def run_precursor_gi_pipeline(execute_transfers: bool = True):
    """
    Guarantees GI errors are cleared before backlog resolution by checking recent runs or executing GI pipeline.
    Enforces D51 (Thread 058) 10-Minute Interface Settling Guardrails:
    1. If a recent 01 run completed within 25m and posted >= 1 transfer, guarantees at least 10 minutes elapsed before proceeding.
    2. If no recent run exists and 02 triggers 01 directly, and that 01 posts >= 1 transfer, waits PRECURSOR_SETTLE_MINUTES (10m).
    """
    logger.info("========================================================")
    logger.info(" [PRECURSOR] Checking 01_GR_GI_Error Precursor Status...")
    logger.info("========================================================")

    recent_gi = check_recent_gi_run(max_age_minutes=PRECURSOR_MAX_AGE_MINUTES)
    if recent_gi:
        postings = recent_gi.get("postings_executed", 0)
        age_sec = recent_gi.get("age_seconds", 0.0)
        logger.info(
            f"✅ [PRECURSOR HANDSHAKE] Verified recent GI Error run completed: "
            f"Batch {recent_gi['batch_id']} at {recent_gi['run_time']} "
            f"({recent_gi['age_minutes']}m ago, Postings: {postings}). "
            f"Skipping duplicate GI execution."
        )
        # D51-3: Ensure at least 10 minutes elapsed if transfers were posted
        min_sec = PRECURSOR_MIN_ELAPSED_MINUTES * 60.0
        if postings > 0 and age_sec < min_sec:
            wait_sec = min_sec - age_sec
            logger.info(
                f"⏳ [PRECURSOR SETTLE] 01 GI run posted {postings} transfers {recent_gi['age_minutes']}m ago (< {PRECURSOR_MIN_ELAPSED_MINUTES}m). "
                f"Waiting {wait_sec:.0f}s for SAP interface settlement before Backlog processing..."
            )
            time.sleep(wait_sec)
        return

    logger.info(f"ℹ️ [PRECURSOR] No recent GI run found within {PRECURSOR_MAX_AGE_MINUTES}m. Triggering 01_GR_GI_Error Pipeline First...")
    gi_pipeline_script = GR_GI_DIR / "hourly_gi_pipeline.py"
    if gi_pipeline_script.exists():
        import subprocess
        cmd = [sys.executable, str(gi_pipeline_script), "--no-email"]
        if not execute_transfers:
            cmd.append("--no-post")
        res = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if res.returncode == 0:
            logger.info("✅ [PRECURSOR] GI Error Pipeline completed successfully.")
            # D51-2: If 01 was triggered directly and posted >= 1 transfer, wait 10 minutes for interface settlement
            new_gi = check_recent_gi_run(max_age_minutes=5.0)
            if new_gi and new_gi.get("postings_executed", 0) > 0:
                settle_sec = PRECURSOR_SETTLE_MINUTES * 60.0
                logger.info(
                    f"⏳ [PRECURSOR SETTLE] Direct 01 run posted {new_gi['postings_executed']} transfers. "
                    f"Waiting {PRECURSOR_SETTLE_MINUTES}m ({int(settle_sec)}s) for SAP interface settlement..."
                )
                time.sleep(settle_sec)
        else:
            logger.warning(f"⚠️ [PRECURSOR] GI Error Pipeline returned non-zero code ({res.returncode}): {res.stderr[:200]}")
    else:
        logger.warning(f"[PRECURSOR] GI pipeline script not found at {gi_pipeline_script}")

    time.sleep(3)


def _execute_backlog_cycle_core(
    run_time_label: str = "MANUAL",
    send_email: bool = False,
    to_recipients: List[str] = None,
    cc_recipients: List[str] = None,
    is_test: bool = False,
    pre_refresh: Optional[bool] = None,
    execute_transfers: bool = True,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    save_plan_excel: bool = True
) -> Dict[str, Any]:
    """
    Executes a complete single Backlog resolution cycle:
    - If pre_refresh is True (default for 06:20 AM / 06:10 AM / 06:00 AM): Runs COGI Select All -> Save first to refresh state,
      archives initial backlog file to cogi_errors/raw_history/ and inserts into raw_backlog_history DB table.
    """
    init_db(db_path=db_path)
    if pre_refresh is None:
        pre_refresh = (run_time_label in ["06:00", "06:10", "06:20"])

    batch_id = datetime.now().strftime(f"BACKLOG_{run_time_label}_%Y%m%d_%H%M%S")
    run_date = datetime.now().strftime("%Y-%m-%d")
    cycle_start_time = time.time()
    
    logger.info(f"\n========================================================")
    logger.info(f" 🚀 Starting Backlog Pipeline Cycle [{run_time_label}] (Batch: {batch_id})")
    logger.info(f"    Pre-Refresh Save: {'ENABLED (06:20 AM Raw History Archive)' if pre_refresh else 'DISABLED'}")
    logger.info(f"========================================================")

    # 1. Precursor: Clear GI Errors first (with smart handshake)
    run_precursor_gi_pipeline(execute_transfers=execute_transfers)

    # Cross-process SAP GUI Mutex Lock
    hq_dir = BASE_DIR.parent.parent / "00_HQ_TaskManager"
    if str(hq_dir) not in sys.path:
        sys.path.insert(0, str(hq_dir))
    from sap_lock import sap_session_lock

    with sap_session_lock(owner=f"02_JOB_Backlog_Clear_{run_time_label}", timeout=180.0) as acquired:
        if not acquired:
            logger.error(f"❌ [SAP LOCK] Could not acquire SAP lock for {run_time_label} within 180s. Another SAP job is running.")
            return {"status": "LOCK_TIMEOUT", "error": "SAP session locked"}

        # 2. Extract COGI errors (Pre-refresh save if enabled AND execute_transfers is True)
        if pre_refresh and execute_transfers:
            logger.info("\n[STEP 1/5] Pre-refreshing COGI (Select All -> Save) and extracting Pre-Resolution Raw Backlog...")
            cogi_file, cogi_items, save_msg = refresh_and_extract_initial_backlog(plant=_get_ops_env("SAP_PLANT", "P001"), dispo_low="i01", dispo_high="i03")
            logger.info(f"           Pre-refresh Save Result: {save_msg}")

            # Archive pre-resolution raw backlog history to SQLite DB table
            if cogi_items:
                raw_count = log_raw_backlog_history(
                    history_id=batch_id,
                    run_date=run_date,
                    run_time=run_time_label,
                    source_file=cogi_file.name if cogi_file else "",
                    items=cogi_items
                )
                logger.info(f"           💾 Archived {raw_count} pre-resolution backlog items to 'raw_backlog_history' DB table.")
        else:
            if not execute_transfers and pre_refresh:
                logger.info(f"\n[STEP 1/5] Read-only mode: Pre-refresh save bypassed. Extracting SAP COGI Backlogs (Plant {_get_ops_env('SAP_PLANT', 'P001')}, i01~i03)...")
            else:
                logger.info(f"\n[STEP 1/5] Extracting SAP COGI Backlogs (Plant {_get_ops_env('SAP_PLANT', 'P001')}, i01~i03)...")
            cogi_file, cogi_items = export_cogi_errors(plant=_get_ops_env("SAP_PLANT", "P001"), dispo_low="i01", dispo_high="i03")
        
        if not cogi_items:
            logger.info("🎉 [STEP 1/5] No COGI backlog items found! All clear.")
            metric = {
                "run_date": run_date,
                "run_time": run_time_label,
                "total_cogi_items": 0,
                "total_cogi_qty": 0.0,
                "resolved_items": 0,
                "resolved_qty": 0.0,
                "unresolved_items": 0,
                "unresolved_qty": 0.0,
                "email_sent": False,
                "notes": "No COGI errors found"
            }
            log_daily_metric(metric)
            return metric

        # Log operational snapshot to DB
        log_cogi_snapshot(batch_id, cogi_items)
        logger.info(f"           Logged {len(cogi_items)} operational COGI snapshot records to database.")

        # 2.5 Simultaneous Stock Snapshots (WMS & MB52) per D21/D30
        logger.info("\n[STEP 2/5] Updating Stock Snapshots (WMS & SAP MB52)...")
        try:
            if str(GR_GI_DIR) not in sys.path:
                sys.path.insert(0, str(GR_GI_DIR))
            from sync_snapshot_collector import collect_simultaneous_snapshots
            snapshots = collect_simultaneous_snapshots(
                max_freshness_seconds=300.0,
                sap_lock_owner=f"02_JOB_Backlog_Clear_{run_time_label}"
            )
            logger.info(f"           - MB52 File: {snapshots.get('SAP_MB52')}")
            logger.info(f"           - WMS File:  {snapshots.get('WMS_STOCK')}")
            if not snapshots.get('can_execute_transfers', False):
                logger.warning("           ⚠️ [FRESHNESS GUARD] Fresh simultaneous snapshot verification failed! Disabling transfers (REPORT ONLY).")
                execute_transfers = False
        except Exception as e_snap:
            logger.warning(f"           ⚠️ Snapshot collection warning: {e_snap}")
            execute_transfers = False

        # 2.2 Fresh ZTCODE_GI_ERR GR & GI Error Collection (D44-1 / D44-3)
        logger.info("\n[STEP 1.5/5] Extracting Fresh ZTCODE_GI_ERR GR & GI Errors (D44 Guard)...")
        fresh_gi_path = None
        fresh_gr_path = None
        fresh_gi_ok = False
        fresh_gr_ok = False
        try:
            if str(GR_GI_DIR) not in sys.path:
                sys.path.insert(0, str(GR_GI_DIR))
            from ztcode_gi_collector import collect_ztcode_gi_err_errors
            ztcode_gi_err_res = collect_ztcode_gi_err_errors(plant=_get_ops_env("SAP_PLANT", "P001"))
            fresh_gi = ztcode_gi_err_res.get("GI_Error")
            fresh_gr = ztcode_gi_err_res.get("GR_Error")
            fresh_gr_bundle = [
                Path(x) for x in (ztcode_gi_err_res.get("GR_Files") or [])
                if x and Path(x).exists() and Path(x).stat().st_mtime >= (cycle_start_time - 5.0)
            ]
            # B-5: Verify file mtime >= cycle_start_time (freshness cannot be satisfied by stale files)
            fresh_gi_ok = bool(fresh_gi and Path(fresh_gi).exists() and Path(fresh_gi).stat().st_mtime >= (cycle_start_time - 5.0))
            fresh_gr_ok = bool(fresh_gr and Path(fresh_gr).exists() and Path(fresh_gr).stat().st_mtime >= (cycle_start_time - 5.0))
            if fresh_gi_ok:
                fresh_gi_path = Path(fresh_gi)
                logger.info(f"           - Fresh GI Error (mtime verified): {fresh_gi_path.name}")
            else:
                logger.warning(f"           ⚠️ Fresh GI Error collection failed, missing or stale: {fresh_gi}")
            if fresh_gr_ok:
                fresh_gr_path = fresh_gr_bundle if fresh_gr_bundle else Path(fresh_gr)
                logger.info(f"           - Fresh GR Error (mtime verified): {[p.name for p in fresh_gr_bundle] if fresh_gr_bundle else Path(fresh_gr).name}")
            else:
                logger.warning(f"           ⚠️ Fresh GR Error collection failed, missing or stale: {fresh_gr}")

            if not fresh_gi_ok or not fresh_gr_ok:
                logger.warning("           ⚠️ [D44 GUARD] Fresh ZTCODE_GI_ERR collection failed! Disabling transfers (REPORT ONLY).")
                execute_transfers = False
        except Exception as e_zm:
            logger.warning(f"           ⚠️ [D44 GUARD] ZTCODE_GI_ERR collection exception: {e_zm}. Disabling transfers (REPORT ONLY).")
            execute_transfers = False

        # 3. Build resolution plan with stock accuracy guardrails
        logger.info("\n[STEP 2.5/5] Analyzing WMS-SAP Stock Accuracy & Building Safe Plan...")
        plan_df = build_backlog_resolution_plan(
            cogi_items,
            gi_path=fresh_gi_path if fresh_gi_ok else None,
            gr_path=fresh_gr_path if fresh_gr_ok else None,
            require_fresh_ztcode_stock_rec=True,
            output_dir=output_dir,
            save_excel=save_plan_excel
        )

        # D32: Extract and record remaining SAP deficits
        from backlog_resolver import extract_remaining_sap_deficits
        from db_manager import record_unresolved_sap_deficits, get_unresolved_sap_deficits
        remaining_deficits = extract_remaining_sap_deficits(plan_df, run_date=run_date)
        try:
            rec_cnt = record_unresolved_sap_deficits(run_date, remaining_deficits, db_path=db_path)
            logger.info(f"           [D32 DEFICIT] Tracked {rec_cnt} remaining SAP deficits in DB.")
        except Exception as e_def:
            logger.warning(f"           ⚠️ [D32 DEFICIT] Recording warning: {e_def}")

        approved_postings = plan_df[plan_df['Action'] == 'TRANSFER_POSTING']
        skipped_items = plan_df[plan_df['Action'] == 'SKIP']
        logger.info(f"           Plan Summary: {len(approved_postings)} Approved for Transfer | {len(skipped_items)} Guardrail Preserved (SKIP)")

        # 4. Execute MB1B 311 Transfers (Header: Backlog_Clear)
        execution_results = []
        if execute_transfers and not approved_postings.empty:
            logger.info("\n[STEP 3/5] Executing MB1B 311 Material Transfers (Header: Backlog_Clear)...")
            execution_results = execute_backlog_batch(
                plan_df,
                batch_id=batch_id,
                run_date=run_date,
                run_time=run_time_label,
                delay_between=2.0
            )
        else:
            if not execute_transfers:
                logger.info("\n[STEP 3/5] MB1B Execution skipped (--no-post / --dry-run specified or freshness failed).")
            else:
                logger.info("\n[STEP 3/5] No MB1B transfers to execute in this cycle.")

        # 5. Re-open COGI and Save (재처리)
        successful_transfers = [r for r in execution_results if "499" in str(r.get("Result", "")) or r.get("Status") == "SUCCESS"]
        final_cogi_items = []
        has_direct_cogi_items = (
            plan_df is not None and not plan_df.empty and
            (plan_df['Status'].isin(['RESOLVABLE_BY_COGI_SAVE', 'LOCKED_BY_CONCURRENT_USER'])).any()
        )
        if execute_transfers and (successful_transfers or has_direct_cogi_items):
            direct_cnt = len(plan_df[plan_df['Status'].isin(['RESOLVABLE_BY_COGI_SAVE', 'LOCKED_BY_CONCURRENT_USER'])]) if has_direct_cogi_items else 0
            logger.info(f"\n[STEP 4/5] Re-opening COGI to Select All & Save ({len(successful_transfers)} items replenished, {direct_cnt} direct save items)...")
            save_msg = post_and_save_cogi(plant=_get_ops_env("SAP_PLANT", "P001"), dispo_low="i01", dispo_high="i03")
            logger.info(f"           COGI Save Result: {save_msg}")

            # Ensure SAP asynchronous database commit (V1 update task) is fully finished
            logger.info("           Waiting 5s for SAP background database commit...")
            time.sleep(5)

            # Post-resolution verification: Re-query COGI to get exact confirmed remaining backlog
            logger.info("           Verifying post-resolution remaining COGI backlog from SAP...")
            final_cogi_file, final_cogi_items = export_cogi_errors(plant=_get_ops_env("SAP_PLANT", "P001"), dispo_low="i01", dispo_high="i03")
            logger.info(f"           ✅ Confirmed actual remaining backlog in SAP: {len(final_cogi_items)} items.")
        else:
            if not execute_transfers:
                logger.info("\n[STEP 4/5] Skipped COGI Save (--no-post / --dry-run specified).")
            else:
                logger.info("\n[STEP 4/5] Skipped COGI Save (no transfers executed and no direct COGI save candidates).")
            final_cogi_items = [it for it in cogi_items if it.get("Material")]

    # 6. Notification (Sent ONLY after COGI save & post-verification are 100% complete)
    email_sent = False
    cur_time_str = "08:15 AM EST" if "08" in run_time_label else ("06:20 AM EST" if "06" in run_time_label else datetime.now().strftime("%I:%M %p EST"))
    if send_email:
        # Retrieve cumulative daily transfers from SQLite DB (aggregating 06:00 AM and 08:00 AM runs)
        cumulative_transfers = get_daily_transfers(run_date)
        if not cumulative_transfers and execution_results:
            cumulative_transfers = execution_results

        # Retrieve initial baseline from 06:00 AM raw backlog history
        initial_raw = get_daily_initial_backlog(run_date)
        initial_count = initial_raw.get("count", 0)
        initial_qty = initial_raw.get("total_qty", 0.0)

        # Retrieve D32 remaining deficits for report
        deficits_for_report = get_unresolved_sap_deficits(run_date)
        if not deficits_for_report and remaining_deficits:
            deficits_for_report = remaining_deficits

        logger.info(f"\n[STEP 5/5] Sending Verified Cumulative Backlog Report via Outlook ({cur_time_str})...")
        logger.info(f"           Cumulative Daily Transfers Today: {len(cumulative_transfers)} items ({sum(t['Qty'] for t in cumulative_transfers):,.1f} EA)")
        email_sent = send_morning_backlog_report(
            plan_df=plan_df,
            execution_results=cumulative_transfers,
            final_cogi_items=final_cogi_items,
            run_date=run_date,
            run_time=cur_time_str,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            is_test=is_test,
            initial_count=initial_count,
            initial_qty=initial_qty,
            remaining_deficits=deficits_for_report
        )
    else:
        logger.info(f"\n[STEP 5/5] Email notification skipped for this run (scheduled for 08:00 AM only).")

    # 7. Log summary metrics to DB
    total_qty = plan_df['Deficit_Qty'].sum() if not plan_df.empty else 0.0
    resolved_qty = sum([float(r.get("Qty", 0.0)) for r in successful_transfers])
    partial_items = plan_df[plan_df['Status'] == 'PARTIAL_ACCURACY_REBALANCING'] if not plan_df.empty else pd.DataFrame()
    partial_rem_qty = (partial_items['Deficit_Qty'] - partial_items['Transfer_Qty']).sum() if not partial_items.empty else 0.0

    unresolved_count = len(skipped_items) + len(partial_items)
    unresolved_qty = (skipped_items['Deficit_Qty'].sum() if not skipped_items.empty else 0.0) + partial_rem_qty

    fully_resolved_count = len([r for r in execution_results if "499" in str(r.get("Result", ""))]) - len(partial_items)

    metric = {
        "run_date": run_date,
        "run_time": run_time_label,
        "total_cogi_items": len(plan_df),
        "total_cogi_qty": total_qty,
        "resolved_items": len(successful_transfers),
        "resolved_qty": resolved_qty,
        "unresolved_items": unresolved_count,
        "unresolved_qty": unresolved_qty,
        "email_sent": email_sent,
        "email_sent_at": datetime.now().isoformat() if email_sent else None,
        "notes": f"Batch: {batch_id} ({fully_resolved_count} Full + {len(partial_items)} Partial, {unresolved_count} Remaining in COGI)"
    }
    log_daily_metric(metric, db_path=db_path)
    logger.info(f"\n[PIPELINE COMPLETE] {len(successful_transfers)} transfers posted ({resolved_qty:,.1f} EA), {unresolved_count} items remaining in COGI ({unresolved_qty:,.1f} EA). Logged to DB.")

    # 8. Storage Retention & Disk Pruning
    try:
        sys.path.insert(0, str(BASE_DIR.parent))
        from storage_retention import run_retention_pruning
        run_retention_pruning(retention_days=7)
    except Exception as e_ret:
        logger.warning(f"Storage retention cleanup warning: {e_ret}")

    return metric


def _extract_ts_from_filename(path: Path) -> Optional[datetime]:
    m = re.search(r"(\d{8})_(\d{6})", path.name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _parse_cycle_anchor(as_of: Optional[str]) -> datetime:
    if not as_of:
        return datetime.now()
    s = str(as_of).strip()
    m6 = re.search(r"(\d{8})_(\d{6})", s)
    if m6:
        return datetime.strptime(f"{m6.group(1)}_{m6.group(2)}", "%Y%m%d_%H%M%S")
    m4 = re.search(r"(\d{8})_(\d{4})", s)
    if m4:
        return datetime.strptime(f"{m4.group(1)}_{m4.group(2)}00", "%Y%m%d_%H%M%S")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt in ("%Y-%m-%d", "%Y%m%d"):
                return dt.replace(hour=8, minute=15, second=0)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Unsupported --as-of format: {as_of!r}. Use e.g. 20261029_0815 or BACKLOG_MANUAL_NOW_20261029_081502.")


def _load_readonly_report_context(run_date: str, cutoff_dt: datetime, db_path: Path) -> Tuple[List[Dict[str, Any]], int, float, List[Dict[str, Any]]]:
    """Reads cumulative transfers, initial raw baseline, and D32 remaining SAP deficits strictly via ?mode=ro."""
    transfers: List[Dict[str, Any]] = []
    initial_count = 0
    initial_qty = 0.0
    deficits: List[Dict[str, Any]] = []
    if not db_path.exists():
        return transfers, initial_count, initial_qty, deficits

    compact_date = run_date.replace("-", "")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5.0) as conn:
        conn.row_factory = sqlite3.Row
        # 1. Cumulative transfers on run_date up to cutoff_dt + 15m
        t_rows = conn.execute(
            """
            SELECT * FROM backlog_transfers
            WHERE (run_date = ? OR batch_id LIKE ? OR date(executed_at) = ? OR date(created_at) = ?)
              AND (status = 'SUCCESS' OR sap_doc_no IS NOT NULL)
            ORDER BY id ASC
            """,
            (run_date, f"%{compact_date}%", run_date, run_date),
        ).fetchall()
        for r in t_rows:
            bid = str(r["batch_id"] or "")
            bts = _extract_ts_from_filename(Path(bid)) if bid else None
            if bts is not None and bts > (cutoff_dt + timedelta(minutes=15)):
                continue
            rt = str(r["run_time"] or "").strip()
            if "06" in rt or "06" in bid:
                cycle_label = "06:20 AM" if ("20" in rt or "20" in bid) else ("06:10 AM" if ("10" in rt or "10" in bid) else "06:00 AM")
            elif "08" in rt or "08" in bid:
                cycle_label = "08:15 AM" if ("15" in rt or "15" in bid) else "08:00 AM"
            elif rt:
                cycle_label = f"{rt} AM" if "AM" not in rt and "PM" not in rt else rt
            else:
                cycle_label = "Earlier Run"
            transfers.append(
                {
                    "Material": r["material"] or "",
                    "Material_Desc": r["material_desc"] or "",
                    "From": r["from_sloc"] or "",
                    "To": r["to_sloc"] or "",
                    "Qty": float(r["transfer_qty"] or 0.0),
                    "Header_Text": r["header_text"] or "Backlog_Clear",
                    "Result": r["sap_raw_msg"] or f"Document {r['sap_doc_no']} posted",
                    "Doc_No": r["sap_doc_no"] or "",
                    "Status": r["status"] or "SUCCESS",
                    "Cycle": cycle_label,
                    "Batch_Id": bid,
                }
            )

        # 2. Initial raw backlog baseline
        raw_rows = conn.execute(
            "SELECT qty, run_time FROM raw_backlog_history WHERE run_date = ? ORDER BY id ASC",
            (run_date,),
        ).fetchall()
        if raw_rows:
            r06 = [r for r in raw_rows if str(r["run_time"] or "").startswith("06")]
            chosen_raw = r06 if r06 else raw_rows
            initial_count = len(chosen_raw)
            initial_qty = sum(float(r["qty"] or 0.0) for r in chosen_raw)

        # 3. Remaining SAP deficits (D32)
        def_rows = conn.execute(
            """
            SELECT
                material AS Material,
                material_desc AS Material_Desc,
                storage_loc AS Storage_Loc,
                run_date,
                sap_stock AS SAP_Stock,
                wms_stock AS WMS_Stock,
                deficit_qty AS Deficit_Qty,
                accuracy_before AS Accuracy_Before,
                accuracy_after AS Accuracy_After,
                recent_doc AS Recent_Doc,
                first_detected_date,
                consecutive_days,
                is_sl49,
                is_sustained_deficit
            FROM SAP_DEFICIT_UNRESOLVED_TRACKER
            WHERE run_date = ?
            ORDER BY is_sl49 ASC, deficit_qty DESC
            """,
            (run_date,),
        ).fetchall()
        deficits = [dict(r) for r in def_rows]

    return transfers, initial_count, initial_qty, deficits


def run_report_only_as_of(
    as_of: Optional[str] = None,
    send_email: bool = False,
    is_test: bool = False,
    dry_run: bool = False,
    is_correction: bool = False,
    correction_note: Optional[str] = None,
    to_recipients: Optional[List[str]] = None,
    cc_recipients: Optional[List[str]] = None,
    logs_dir: Optional[Path] = None,
    cogi_dir: Optional[Path] = None,
    gi_dir: Optional[Path] = None,
    backlog_db_path: Optional[Path] = None,
    gi_db_path: Optional[Path] = None,
    gi_plan_dir: Optional[Path] = None,
    ops_raw_db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Rebuilds and optionally sends the 02 Backlog report strictly from saved artifacts as of `as_of` (#183).
    GUARANTEES: Zero SAP access, zero COGI save, zero MB1B posting, zero operational DB writes (?mode=ro only).
    """
    logger.info(_verify_ops_env_log("02_JOB_REPORT_ONLY"))
    anchor_dt = _parse_cycle_anchor(as_of)

    eff_logs_dir = Path(logs_dir or (BASE_DIR / "resolution_logs"))
    eff_cogi_dir = Path(cogi_dir or (BASE_DIR / "cogi_errors"))
    eff_gi_dir = Path(gi_dir or (GR_GI_DIR / "gi_errors"))
    eff_bl_db = Path(backlog_db_path or (BASE_DIR / "db" / "backlog_history.db"))

    # 1. Locate saved Backlog_Resolution_Plan_*.xlsx for this cycle
    plan_candidates: List[Tuple[datetime, Path]] = []
    for p in eff_logs_dir.glob("Backlog_Resolution_Plan_*.xlsx"):
        pts = _extract_ts_from_filename(p)
        if pts is not None:
            plan_candidates.append((pts, p))
    if not plan_candidates:
        raise FileNotFoundError(f"No saved Backlog_Resolution_Plan_*.xlsx found in {eff_logs_dir}")

    in_window = [
        (pts, p)
        for pts, p in plan_candidates
        if (anchor_dt - timedelta(minutes=2)) <= pts <= (anchor_dt + timedelta(minutes=15))
    ]
    if in_window:
        in_window.sort(key=lambda x: abs((x[0] - anchor_dt).total_seconds()))
        plan_ts, plan_path = in_window[0]
    else:
        le_cands = [(pts, p) for pts, p in plan_candidates if pts <= (anchor_dt + timedelta(minutes=1))]
        if not le_cands:
            raise FileNotFoundError(f"No Backlog_Resolution_Plan_*.xlsx found on or before {anchor_dt} in {eff_logs_dir}")
        le_cands.sort(key=lambda x: x[0], reverse=True)
        plan_ts, plan_path = le_cands[0]

    run_date = plan_ts.strftime("%Y-%m-%d")

    # 2. Locate post-cycle COGI_<PLANT>_*.XLS in [anchor_dt - 2m, plan_ts + 10m]
    from cogi_collector import parse_cogi_file
    cogi_candidates: List[Tuple[datetime, Path]] = []
    for search_d in (eff_cogi_dir, eff_cogi_dir / "raw_history"):
        if search_d.exists():
            for cp in search_d.glob("COGI_*.XLS"):
                cts = _extract_ts_from_filename(cp)
                if cts is not None and (anchor_dt - timedelta(minutes=2)) <= cts <= (plan_ts + timedelta(minutes=10)):
                    cogi_candidates.append((cts, cp))
    cogi_candidates.sort(key=lambda x: x[0], reverse=True)
    cogi_path = cogi_candidates[0][1] if cogi_candidates else None
    final_cogi_items = parse_cogi_file(cogi_path) if cogi_path else None

    # 3. Locate cycle GI_Error_*.XLS <= plan_ts + 30s
    gi_candidates: List[Tuple[datetime, Path]] = []
    if eff_gi_dir.exists():
        for gp in eff_gi_dir.glob("GI_Error_*.XLS"):
            gts = _extract_ts_from_filename(gp)
            if gts is not None and gts <= (plan_ts + timedelta(seconds=30)) and gts.date() == plan_ts.date():
                gi_candidates.append((gts, gp))
    gi_candidates.sort(key=lambda x: x[0], reverse=True)
    gi_path = gi_candidates[0][1] if gi_candidates else None

    eff_gr_dir = eff_gi_dir.parent / "gr_errors"
    gr_candidates: List[Tuple[datetime, Path]] = []
    if eff_gr_dir.exists():
        for grp in eff_gr_dir.glob("GR_Error_*.XLS"):
            grts = _extract_ts_from_filename(grp)
            if grts is not None and grts <= (plan_ts + timedelta(seconds=30)) and grts.date() == plan_ts.date():
                gr_candidates.append((grts, grp))
    gr_candidates.sort(key=lambda x: x[0], reverse=True)
    gr_path_ro = gr_candidates[0][1] if gr_candidates else None

    # 4. Read saved plan & enrich with D108 using preceding 01 cycle as of plan_ts
    plan_df = pd.read_excel(plan_path)
    dept_dir = BASE_DIR.parent
    if str(dept_dir) not in sys.path:
        sys.path.insert(0, str(dept_dir))
    from gi_verdict_shared import enrich_backlog_plan_with_d108, DEFAULT_GI_DB, DEFAULT_GI_PLAN_DIR, DEFAULT_OPS_RAW_DB

    plan_df = enrich_backlog_plan_with_d108(
        plan_df,
        as_of=plan_ts,
        gi_path=gi_path,
        gr_path=gr_path_ro,
        gi_db_path=gi_db_path or DEFAULT_GI_DB,
        gi_plan_dir=gi_plan_dir or DEFAULT_GI_PLAN_DIR,
        ops_raw_db_path=ops_raw_db_path or DEFAULT_OPS_RAW_DB,
    )

    # 5. Load read-only DB context
    cumulative_transfers, initial_count, initial_qty, deficits_for_report = _load_readonly_report_context(
        run_date=run_date,
        cutoff_dt=plan_ts,
        db_path=eff_bl_db,
    )

    run_time_str = "08:15 AM EST" if plan_ts.hour == 8 else (
        "06:20 AM EST" if plan_ts.hour == 6 else plan_ts.strftime("%I:%M %p EST")
    )

    logger.info("========================================================")
    logger.info(f" 📄 [REPORT-ONLY MODE] Reconstructed 02 Cycle as of {plan_ts.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"    - Saved 02 Plan : {plan_path.name}")
    logger.info(f"    - Post COGI File: {cogi_path.name if cogi_path else 'NONE'} ({len(final_cogi_items) if final_cogi_items is not None else 0} items)")
    logger.info(f"    - GI Error File : {gi_path.name if gi_path else 'NONE'}")
    logger.info(f"    - Preceding 01  : {plan_df.attrs.get('d108_eval_meta', {})}")
    logger.info("    - SAP / DB Write: ZERO (100% Read-Only)")
    logger.info("========================================================")

    gi_blocked = plan_df[plan_df["Status"] == "BLOCKED_BY_GI_ERROR"] if not plan_df.empty else pd.DataFrame()
    if not gi_blocked.empty:
        disp_cols = [
            c
            for c in (
                "Material",
                "Target_SLoc",
                "Deficit_Qty",
                "D108_Cause_Code",
                "D108_Cause_Label",
                "D108_Severity",
                "D108_Linked_GI",
                "D108_01_Verdict",
                "D108_01_Plan_Time",
                "D108_Is_Bug_Candidate",
                "D108_Detail",
            )
            if c in gi_blocked.columns
        ]
        print("\n[D108 GI-BLOCKED RECLASSIFICATION TABLE]")
        print(gi_blocked[disp_cols].to_string(index=False))

    dry_run_files: Dict[str, str] = {}
    if dry_run:
        from notifier import build_email_html, generate_unresolved_excel
        eff_note = correction_note or ("Updated root-cause verdicts for GI error-linked backlogs" if is_correction else None)
        html_preview = build_email_html(
            plan_df=plan_df,
            execution_results=cumulative_transfers,
            final_cogi_items=final_cogi_items,
            run_date=run_date,
            run_time=run_time_str,
            initial_count=initial_count,
            initial_qty=initial_qty,
            remaining_deficits=deficits_for_report,
            is_correction=is_correction,
            correction_note=eff_note,
        )
        scratch_dir = BASE_DIR / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        html_path = scratch_dir / f"DRYRUN_Backlog_Report_{plan_ts.strftime('%Y%m%d_%H%M%S')}.html"
        html_path.write_text(html_preview, encoding="utf-8")
        xlsx_path = generate_unresolved_excel(
            plan_df=plan_df,
            run_date=run_date,
            final_cogi_items=final_cogi_items,
            remaining_deficits=deficits_for_report,
            output_dir=scratch_dir,
            dry_run=True,
        )
        dry_run_files = {
            "html": str(html_path),
            "excel": str(xlsx_path) if xlsx_path else "",
        }
        logger.info(f" 🧪 [DRY-RUN PREVIEW SAVED] HTML={html_path.name} | Excel={Path(xlsx_path).name if xlsx_path else 'NONE'}")

    email_sent = False
    if send_email and not dry_run:
        eff_note = correction_note or ("Updated root-cause verdicts for GI error-linked backlogs" if is_correction else None)
        email_sent = send_morning_backlog_report(
            plan_df=plan_df,
            execution_results=cumulative_transfers,
            final_cogi_items=final_cogi_items,
            run_date=run_date,
            run_time=run_time_str,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            is_test=is_test,
            initial_count=initial_count,
            initial_qty=initial_qty,
            remaining_deficits=deficits_for_report,
            is_correction=is_correction,
            correction_note=eff_note,
        )

    return {
        "mode": "REPORT_ONLY",
        "as_of": plan_ts.strftime("%Y-%m-%d %H:%M:%S"),
        "plan_file": str(plan_path),
        "cogi_file": str(cogi_path) if cogi_path else None,
        "gi_file": str(gi_path) if gi_path else None,
        "d108_meta": plan_df.attrs.get("d108_eval_meta", {}),
        "gi_blocked_count": int(len(gi_blocked)),
        "email_sent": email_sent,
        "dry_run_files": dry_run_files,
        "plan_df": plan_df,
    }


def execute_backlog_cycle(
    run_time_label: str = "MANUAL",
    send_email: bool = False,
    to_recipients: List[str] = None,
    cc_recipients: List[str] = None,
    is_test: bool = False,
    pre_refresh: Optional[bool] = None,
    max_retries: int = 1,
    execute_transfers: bool = True
) -> Dict[str, Any]:
    """
    Self-healing Backlog cycle runner with automatic recovery from mid-cycle SAP kickouts.
    If connection is dropped or error popup appears in the middle of execution:
    1. Dismisses error popup and cleans up dead session.
    2. Cold-starts fresh SAP session via Enterprise SSO.
    3. Safely re-executes cycle from the beginning with fresh COGI extraction.
    """
    try:
        return _execute_backlog_cycle_core(
            run_time_label=run_time_label,
            send_email=send_email,
            to_recipients=to_recipients,
            cc_recipients=cc_recipients,
            is_test=is_test,
            pre_refresh=pre_refresh,
            execute_transfers=execute_transfers
        )
    except Exception as e:
        if max_retries > 0:
            logger.warning("========================================================")
            logger.warning(f" ⚠️ [BACKLOG INTERRUPTION] Cycle failed due to error: {e}")
            logger.warning("    -> Initiating Self-Healing: Dismissing kick-out dialogs & restarting SAP...")
            logger.warning("========================================================")
            time.sleep(3)
            try:
                ensure_sap_open()
            except Exception as e_launch:
                logger.error(f"    ❌ SAP recovery launch failed: {e_launch}")

            logger.info("🔄 [RETRYING] Re-executing Backlog cycle from beginning with fresh SAP session...")
            return execute_backlog_cycle(
                run_time_label=run_time_label,
                send_email=send_email,
                to_recipients=to_recipients,
                cc_recipients=cc_recipients,
                is_test=is_test,
                pre_refresh=pre_refresh,
                max_retries=max_retries - 1,
                execute_transfers=execute_transfers
            )
        else:
            logger.error(f"❌ [BACKLOG TERMINATED] Cycle failed after retry attempt: {e}")
            raise


def start_scheduler():
    """
    Runs the automated schedule:
    - 06:20 AM EST: Run 1 (06:00 01 GI 처리 후 반영 대기 및 Pre-Refresh & Raw History Archive, No email)
    - 08:15 AM EST: Run 2 (Final Morning Resolution & Morning Cumulative Report)
    """
    logger.info(_verify_ops_env_log("02_JOB_BACKLOG"))
    logger.info("=== Starting Backlog Scheduled Service (06:20 AM & 08:15 AM EST) ===")
    
    last_run_06 = None
    last_run_08 = None

    while True:
        now = datetime.now()
        cur_date_str = now.strftime("%Y-%m-%d")
        cur_hour = now.hour
        cur_minute = now.minute

        # 06:20 AM Run (Pre-Refresh Save & Raw History Archive after 06:00 GI settling)
        if cur_hour == 6 and cur_minute == 20 and last_run_06 != cur_date_str:
            logger.info("⏰ [SCHEDULE] Triggering 06:20 AM EST Backlog Cycle (Run 1 with Pre-Refresh Save)...")
            execute_backlog_cycle(run_time_label="06:20", send_email=False, pre_refresh=True)
            last_run_06 = cur_date_str

        # 08:15 AM Run (Final Morning Report)
        if cur_hour == 8 and cur_minute == 15 and last_run_08 != cur_date_str:
            logger.info("⏰ [SCHEDULE] Triggering 08:15 AM EST Backlog Cycle (Run 2 with Morning Report)...")
            execute_backlog_cycle(run_time_label="08:15", send_email=True, pre_refresh=False)
            last_run_08 = cur_date_str

        time.sleep(20)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="02_Backlog Master Pipeline Orchestrator")
    parser.add_argument("--run-now", action="store_true", help="Execute single cycle immediately")
    parser.add_argument("--report-only", action="store_true", help="Regenerate report from saved files & read-only DB without SAP/COGI/MB1B/DB writes")
    parser.add_argument("--as-of", type=str, default=None, help="Target cycle run_id or timestamp for --report-only (e.g. 20261029_0815)")
    parser.add_argument("--correction", action="store_true", help="Mark email subject with [Correction] and top banner ('Updated root-cause verdicts for GI error-linked backlogs')")
    parser.add_argument("--correction-note", type=str, default=None, help="Optional custom top banner text for --correction")
    parser.add_argument("--ops-root", type=str, default=None, help="Optional path to operational 01_DEPT_Inventory when running --report-only from a worktree")
    parser.add_argument("--dry-run", action="store_true", help="Generate resolution plan only, do not post to SAP")
    parser.add_argument("--no-post", action="store_true", help="Strict read-only test mode (No MB1B postings, no COGI save, SAP queries only)")
    parser.add_argument("--pre-refresh", action="store_true", help="Execute COGI Select All -> Save first to refresh backlog before extracting")
    parser.add_argument("--email", action="store_true", help="Send email report in this run (Production list)")
    parser.add_argument("--test-email", action="store_true", help="Send email report in test mode (Only to designated test recipient)")
    parser.add_argument("--schedule", action="store_true", help="Run background scheduler for 06:20 and 08:15 AM")
    parser.add_argument("--to", type=str, nargs="+", default=None, help="Recipient email addresses")
    parser.add_argument("--cc", type=str, nargs="+", default=None, help="CC email addresses")
    args = parser.parse_args()

    execute_transfers = not (args.dry_run or args.no_post)

    if args.report_only:
        send_mail = bool(args.email or args.test_email)
        ro_kwargs: Dict[str, Any] = {}
        if args.ops_root:
            ops_dept = Path(args.ops_root).resolve()
            for _cand_env in (ops_dept.parents[1] / ".env", ops_dept.parent / ".env"):
                if _cand_env.exists():
                    _load_dotenv(_cand_env, override=False)
                    _ENV_LOADED_PATH = _ENV_LOADED_PATH or _cand_env
                    break
            ro_kwargs = {
                "logs_dir": ops_dept / "02_JOB_Backlog_Clear" / "resolution_logs",
                "cogi_dir": ops_dept / "02_JOB_Backlog_Clear" / "cogi_errors",
                "gi_dir": ops_dept / "01_JOB_GR_GI_Error" / "gi_errors",
                "backlog_db_path": ops_dept / "02_JOB_Backlog_Clear" / "db" / "backlog_history.db",
                "gi_db_path": ops_dept / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db",
                "gi_plan_dir": ops_dept / "01_JOB_GR_GI_Error" / "resolution_logs",
                "ops_raw_db_path": ops_dept.parent / "00_HQ_TaskManager" / "data_collector" / "db" / "ops_raw.db",
            }
        run_report_only_as_of(
            as_of=args.as_of,
            send_email=send_mail,
            is_test=bool(args.test_email),
            dry_run=bool(args.dry_run),
            is_correction=bool(args.correction),
            correction_note=args.correction_note,
            to_recipients=args.to,
            cc_recipients=args.cc,
            **ro_kwargs,
        )
        sys.exit(0)
    elif args.dry_run:
        logger.info(_verify_ops_env_log("02_JOB_BACKLOG"))
        from cogi_collector import parse_cogi_file
        from backlog_resolver import extract_remaining_sap_deficits
        import glob as _glob
        cogi_files = sorted(
            _glob.glob(str(BASE_DIR / "cogi_errors" / "COGI_*.XLS")) +
            _glob.glob(str(BASE_DIR / "cogi_errors" / "raw_history" / "COGI_*.XLS")),
            key=os.path.getmtime, reverse=True
        )
        cogi_path = Path(cogi_files[0]) if cogi_files else None
        cogi_items = parse_cogi_file(cogi_path) if cogi_path else []
        gi_files = sorted(_glob.glob(str(GR_GI_DIR / "gi_errors" / "GI_Error_*.XLS")), key=os.path.getmtime, reverse=True)
        gr_files = sorted(_glob.glob(str(GR_GI_DIR / "gr_errors" / "GR_Error_*.XLS")), key=os.path.getmtime, reverse=True)
        gi_path = Path(gi_files[0]) if gi_files else None
        gr_path = Path(gr_files[0]) if gr_files else None
        plan_df = build_backlog_resolution_plan(
            cogi_items,
            gi_path=gi_path,
            gr_path=gr_path,
            require_fresh_ztcode_stock_rec=True,
            save_excel=True,
            dry_run=True,
        )
        out_file = plan_df.attrs.get("output_file", "")
        if not out_file:
            scratch_dir = BASE_DIR / "scratch"
            scratch_dir.mkdir(parents=True, exist_ok=True)
            out_p = scratch_dir / f"DRYRUN_Backlog_Resolution_Plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            plan_df.to_excel(out_p, index=False)
            out_file = str(out_p)
        deficits = extract_remaining_sap_deficits(plan_df, run_date=datetime.now().strftime("%Y-%m-%d"))
        approved = len(plan_df[plan_df["Action"] == "TRANSFER_POSTING"]) if not plan_df.empty else 0
        skipped = len(plan_df[plan_df["Action"] == "SKIP"]) if not plan_df.empty else 0
        logger.info(
            f"✅ [02_JOB DRY-RUN COMPLETED] COGI Input: {cogi_path} ({len(cogi_items)} items) | "
            f"GI Input: {gi_path} | GR Input: {gr_path} | "
            f"Plan Rows: {len(plan_df)} (Approved={approved}, Skipped={skipped}, Remaining Deficits={len(deficits)}) | "
            f"Output: {out_file} (No SAP Posting, No DB Write, No Email)"
        )
        sys.exit(0)
    elif args.run_now:
        send_mail = (args.email or args.test_email) and execute_transfers
        execute_backlog_cycle(
            run_time_label="MANUAL_NOW",
            send_email=send_mail,
            to_recipients=args.to,
            cc_recipients=args.cc,
            is_test=args.test_email,
            pre_refresh=args.pre_refresh,
            execute_transfers=execute_transfers
        )
    elif args.schedule:
        start_scheduler()
    else:
        parser.print_help()
