"""
Hourly GI Interface Error Resolution Pipeline
==============================================
01_Inventory_Team / 01_GR_GI_Error (Plant P001 Headlamp)
- 1시간마다 (또는 즉시 실행) 다음 4단계를 자동 실행합니다:
  STEP 1: SAP ZTCODE_STOCK_REC (ZTCODE_GI_ERR) 최신 에러 데이터 수집 (Goods Issue & GR Domestics)
  STEP 2: WMS & SAP MB52 재고 스냅샷 동기화
  STEP 3: 4대 안전 규칙 기반 GI 해결 플랜 생성 (gr_gi_resolver.py)
  STEP 4: MB1B 311 자동 이동전기 전표 발행 (mb1b_executor.py)
"""

import os
import sys
import time
import argparse
import logging
from pathlib import Path
import os
import sys as _sys
from dotenv import load_dotenv as _load_dotenv
_ENV_LOADED_PATH = None
for _ep in Path(__file__).resolve().parents:
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
from datetime import datetime
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "resolution_logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "continuous_daemon.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("HourlyGIPipeline")

import importlib.util
_db_spec = importlib.util.spec_from_file_location("job01_db_mgr", BASE_DIR / "db_manager.py")
_db_mgr = importlib.util.module_from_spec(_db_spec)
try:
    _db_spec.loader.exec_module(_db_mgr)
except Exception as _e_init:
    _db_mgr = None

from sap_launcher import ensure_sap_open
from ztcode_gi_collector import collect_ztcode_gi_err_errors
from sync_snapshot_collector import collect_simultaneous_snapshots
from gr_gi_resolver import resolve_gi_errors
from mb1b_executor import execute_batch_postings, execute_d113_roundtrip_posting
from gi_notifier import send_hourly_gi_report, send_daily_cumulative_gi_report


def _run_pipeline_core(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    execute_transfers: bool = True,
    max_transfers: int = None,
    send_email: bool = True,
    to_recipients: list = None,
    cycle_type: str = None,
    force_daily_report: bool = False,
    force_routine_email: bool = False
) -> dict:
    """Runs a single pass of the 24H continuous GI error resolution pipeline."""
    batch_id = datetime.now().strftime("RUN_%Y%m%d_%H%M%S")
    start_time = time.time()
    
    if not cycle_type:
        now_dt = datetime.now()
        cycle_type = "30-min cycle (Daytime)" if (8 <= now_dt.hour < 17 or (now_dt.hour == 17 and now_dt.minute <= 5)) else "1-hour cycle (Night)"

    logger.info("==========================================================================")
    logger.info(f" 🚀 [24H GI PIPELINE - {cycle_type}] Starting Error Resolution Run (Batch: {batch_id} | Plant: {plant})...")
    logger.info("==========================================================================")

    now_dt = datetime.now()
    is_8am_slot = (now_dt.hour == 8 and now_dt.minute <= 30)

    if not send_email:
        should_send_email = False
        report_mode = "NONE"
    elif force_daily_report:
        should_send_email = True
        report_mode = "DAILY_CUMULATIVE"
    elif force_routine_email:
        should_send_email = True
        report_mode = "ROUTINE"
    else:
        # Policy: 08:00 AM daily cumulative report, or routine if 8am slot
        if is_8am_slot:
            should_send_email = True
            report_mode = "DAILY_CUMULATIVE"
        else:
            should_send_email = False
            logger.info(f"    ℹ️ [REPORTING POLICY] Current run ({now_dt.strftime('%H:%M')}) is not 08:00 AM. Email suppressed (DB logging only).")


    # Cross-process SAP GUI Mutex Lock
    hq_dir = BASE_DIR.parent.parent / "00_HQ_TaskManager"
    if str(hq_dir) not in sys.path:
        sys.path.insert(0, str(hq_dir))
    from sap_lock import sap_session_lock

    with sap_session_lock(owner=f"01_JOB_GR_GI_Error_{batch_id}", timeout=180.0) as acquired:
        if not acquired:
            logger.error(f"❌ [SAP LOCK] Could not acquire SAP lock for batch {batch_id} within 180s. Aborting this run.")
            return {"status": "LOCK_TIMEOUT", "processed": 0}

        # STEP 0: Check / Launch SAP GUI
        logger.info("\n>>> [STEP 0/5] Checking SAP GUI Session...")
        ensure_sap_open()

        # STEP 1: Collect latest ZTCODE_GI_ERR errors
        logger.info("\n>>> [STEP 1/5] Extracting Fresh ZTCODE_GI_ERR Interface Errors (ZTCODE_STOCK_REC)...")
        errors = collect_ztcode_gi_err_errors(plant=plant)
        gi_file = errors.get('GI_Error')
        gr_file = errors.get('GR_Error')
        gr_files_all = errors.get('GR_Files') or ([Path(gr_file)] if gr_file else [])
        logger.info(f"    - GI Error File: {gi_file}")
        logger.info(f"    - GR Error File: {gr_file} (3-file bundle: {[Path(x).name for x in gr_files_all if x]})")

        fresh_gi_ok = bool(gi_file and Path(gi_file).exists())
        fresh_gr_ok = bool(gr_file and Path(gr_file).exists())
        if not fresh_gi_ok or not fresh_gr_ok:
            logger.warning(
                f"    ⚠️ [ZTCODE_GI_ERR GUARD] Fresh extraction check failed (GI_OK={fresh_gi_ok}, GR_OK={fresh_gr_ok}). "
                "Stale file fallback is strictly forbidden. Disabling MB1B postings for this run (REPORT ONLY)."
            )
            execute_transfers = False

        # Auto-save snapshots to SQLite DB
        df_gr = None
        try:
            from gr_gi_resolver import parse_sap_utf16_list
            if _db_mgr and "pytest" not in sys.modules:
                if fresh_gi_ok:
                    df_gi = parse_sap_utf16_list(Path(gi_file))
                    _db_mgr.save_gi_error_snapshots(batch_id, df_gi)
                if fresh_gr_ok:
                    from ztcode_stock_guard import parse_merged_gr_dataframe
                    df_gr = parse_merged_gr_dataframe(gr_files_all if gr_files_all else Path(gr_file))
                    _db_mgr.save_gr_error_snapshots(batch_id, df_gr)
        except Exception as e_dbsave:
            logger.warning(f"    ⚠️ Could not save snapshots to DB: {e_dbsave}")

        # STEP 2: Collect Stock Snapshots (WMS & MB52)
        logger.info("\n>>> [STEP 2/5] Updating Stock Snapshots (WMS & SAP MB52)...")
        try:
            snapshots = collect_simultaneous_snapshots(
                max_freshness_seconds=300.0,
                sap_lock_owner=f"01_JOB_GR_GI_Error_{batch_id}"
            )
            logger.info(f"    - MB52 File: {snapshots.get('SAP_MB52')}")
            logger.info(f"    - WMS File:  {snapshots.get('WMS_STOCK')}")
            if not snapshots.get('can_execute_transfers', False):
                logger.warning("    ⚠️ [FRESHNESS GUARD] Fresh simultaneous snapshot verification failed! Disabling MB1B postings for this run (REPORT ONLY).")
                execute_transfers = False
        except Exception as e_snap:
            logger.warning(f"    ⚠️ Snapshot collection warning (using available stock, TRANSFERS DISABLED): {e_snap}")
            execute_transfers = False

        # STEP 3: Generate Resolution Plan
        logger.info("\n>>> [STEP 3/5] Analyzing Errors & Generating Optimal Resolution Plan...")
        if not fresh_gi_ok:
            logger.warning("    ⚠️ [ZTCODE_GI_ERR GUARD] Fresh GI error file missing. Resolution plan cannot be generated.")
            plan_df = pd.DataFrame()
        else:
            plan_df = resolve_gi_errors(
                gi_path=Path(gi_file),
                gr_path=(gr_files_all if gr_files_all else Path(gr_file)) if fresh_gr_ok else None,
                run_id=batch_id,
                record_signals=True
            )
        if plan_df is None or plan_df.empty:
            logger.info("    ℹ️ No active GI errors found to resolve. Pipeline finished.")
            if should_send_email:
                try:
                    if report_mode == "DAILY_CUMULATIVE":
                        send_daily_cumulative_gi_report(plan_df=pd.DataFrame(), posting_results=[], to_recipients=to_recipients)
                    else:
                        send_hourly_gi_report(plan_df=pd.DataFrame(), posting_results=[], to_recipients=to_recipients, cycle_type=cycle_type)
                except Exception as e_mail:
                    logger.warning(f"    ⚠️ Could not dispatch email: {e_mail}")
            return {"status": "NO_ERRORS", "processed": 0}

        ready_items = plan_df[plan_df['Action'] == 'TRANSFER_POSTING']
        full_items = plan_df[plan_df['Status'] == 'READY_FOR_EXECUTION']
        roundtrip_items = plan_df[plan_df['Status'].isin(['ROUNDTRIP_CLEAR', 'ROUNDTRIP_CLEAR_VIA_DEST_D113', 'DEST_PUMP_CLEAR_D115'])]
        d109_items = plan_df[plan_df['Status'] == 'GUARD_RELEASED_D109']
        partial_items = plan_df[plan_df['Status'] == 'PARTIAL_ACCURACY_REBALANCING']
        chain_items = plan_df[plan_df['Status'] == 'CHAIN_CASCADE_RESOLVE']
        insuff_items = plan_df[plan_df['Status'].isin(['INSUFFICIENT_SAP_STOCK', 'INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD', 'SKIPPED_COOLDOWN_GUARD_D110'])]
        cooldown_items = plan_df[plan_df['Status'] == 'SKIPPED_COOLDOWN_GUARD_D110']
        logger.info(f"    - Total Errors Analyzed:   {len(plan_df)}")
        logger.info(
            f"    - Ready for Execution:     {len(ready_items)} "
            f"(Full: {len(full_items)}, Roundtrip Clear: {len(roundtrip_items)}, Partial Rebalance: {len(partial_items)})"
        )
        logger.info(f"    - Chained Cascade Resolve: {len(chain_items)} (Downstream unblocked)")
        logger.info(f"    - Insufficient SAP Stock:  {len(insuff_items)} (D110 24h Cooldown: {len(cooldown_items)})")
        logger.info(f"    - Excluded (901/902):      {len(plan_df[plan_df['Status'].str.contains('EXCLUDED', na=False)])}")
        logger.info(f"    - Protected (GR Link):     {len(plan_df[plan_df['Status'] == 'PROTECTED_GR_DEPENDENT'])}")

        # STEP 4: Execute MB1B Transfer Postings (non-D113 inside lock; D113 releases lock during Step 2 wait)
        posting_results = []
        d113_ready_df = pd.DataFrame()
        if execute_transfers and not ready_items.empty:
            _same_cycle_statuses = ('ROUNDTRIP_CLEAR_VIA_DEST_D113', 'DEST_PUMP_CLEAR_D115')
            non_d113_plan = plan_df[~plan_df['Status'].isin(_same_cycle_statuses)]
            d113_ready_df = ready_items[ready_items['Status'].isin(_same_cycle_statuses)]
            if max_transfers is not None:
                rem_cap = max(0, max_transfers)
                non_d113_ready_cnt = int((non_d113_plan['Action'] == 'TRANSFER_POSTING').sum())
                if non_d113_ready_cnt > 0:
                    logger.info(f"\n>>> [STEP 4/5] Executing Standard MB1B 311 Transfer Postings (Count: {min(rem_cap, non_d113_ready_cnt)})...")
                    posting_results.extend(execute_batch_postings(non_d113_plan, max_postings=rem_cap, delay_between=3.0, batch_id=batch_id))
                    rem_cap = max(0, rem_cap - len(posting_results))
                d113_ready_df = d113_ready_df.head(rem_cap)
            else:
                if (non_d113_plan['Action'] == 'TRANSFER_POSTING').any():
                    logger.info(f"\n>>> [STEP 4/5] Executing Standard MB1B 311 Transfer Postings...")
                    posting_results.extend(execute_batch_postings(non_d113_plan, max_postings=None, delay_between=3.0, batch_id=batch_id))
        else:
            if not execute_transfers:
                logger.info("\n>>> [STEP 4/5] Execution skipped (--dry-run specified).")
            else:
                logger.info("\n>>> [STEP 4/5] No ready transfer postings to execute.")

    # STEP 4-B (D113 #211): Same-cycle Step 1 + Step 2 GI verification + automatic rollback (releases SAP lock during wait)
    if execute_transfers and not d113_ready_df.empty:
        logger.info(f"\n>>> [STEP 4-B/5] Executing D113 Same-Cycle Roundtrip Postings (Count: {len(d113_ready_df)})...")
        for _, d113_row in d113_ready_df.iterrows():
            d113_res_list = execute_d113_roundtrip_posting(
                d113_row.to_dict(),
                batch_id=batch_id,
                sap_lock_factory=lambda: sap_session_lock(owner=f"01_JOB_D113_{batch_id}", timeout=180.0),
            )
            for s_res in d113_res_list:
                try:
                    if _db_mgr:
                        _db_mgr.log_transfer_posting(batch_id, s_res)
                except Exception as e_db113:
                    logger.warning(f"    ⚠️ Could not log D113 posting to DB: {e_db113}")
                posting_results.append(s_res)

    elapsed = time.time() - start_time
    logger.info("==========================================================================")
    logger.info(f" 🎉 [PIPELINE COMPLETED] Run finished in {elapsed:.1f}s. Postings: {len(posting_results)}")
    logger.info("==========================================================================\n")

    # Record run metrics to SQLite DB
    try:
        if _db_mgr and "pytest" not in sys.modules:
            _db_mgr.save_pipeline_metrics({
                'batch_id': batch_id,
                'plant': plant,
                'total_gi_errors': len(plan_df) if plan_df is not None else 0,
                'total_gr_errors': len(df_gr) if df_gr is not None else 0,
                'full_resolution_count': len(full_items) + len(roundtrip_items) + len(d109_items),
                'partial_rebalance_count': len(partial_items),
                'insufficient_stock_count': len(insuff_items),
                'excluded_count': len(plan_df[plan_df['Status'].str.contains('EXCLUDED', na=False)]),
                'postings_executed': len(posting_results),
                'elapsed_seconds': round(elapsed, 1),
                'status': 'COMPLETED'
            })
    except Exception as e_met:
        logger.warning(f"    ⚠️ Could not save pipeline metrics: {e_met}")

    # STEP 5: Dispatch Email Report (Policy-driven)
    if should_send_email:
        try:
            if report_mode == "DAILY_CUMULATIVE":
                logger.info(">>> [STEP 5/5] Sending Daily 24H Cumulative GI Report via Outlook (08:00 AM Briefing)...")
                send_daily_cumulative_gi_report(
                    plan_df=plan_df,
                    posting_results=posting_results,
                    to_recipients=to_recipients
                )
            else:
                logger.info(f">>> [STEP 5/5] Sending Routine ({cycle_type}) GI Resolution Report via Outlook...")
                send_hourly_gi_report(
                    plan_df=plan_df,
                    posting_results=posting_results,
                    to_recipients=to_recipients,
                    cycle_type=cycle_type
                )
        except Exception as e_mail:
            logger.warning(f"    ⚠️ Could not dispatch email: {e_mail}")
    else:
        logger.info(">>> [STEP 5/5] Email report skipped (Policy: logs accumulated for 08:00 AM daily briefing).")

    # STEP 6: Automated Storage Retention & Disk Pruning
    try:
        sys.path.insert(0, str(BASE_DIR.parent))
        from storage_retention import run_retention_pruning
        run_retention_pruning(retention_days=7)
    except Exception as e_ret:
        logger.warning(f"    ⚠️ Storage retention cleanup warning: {e_ret}")

    return {
        "batch_id": batch_id,
        "status": "COMPLETED",
        "total_errors": len(plan_df),
        "ready_items": len(ready_items),
        "roundtrip_items": len(roundtrip_items),
        "postings_executed": len(posting_results),
        "elapsed_sec": round(elapsed, 1)
    }


def run_pipeline(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    execute_transfers: bool = True,
    max_transfers: int = None,
    send_email: bool = True,
    to_recipients: list = None,
    cycle_type: str = None,
    force_daily_report: bool = False,
    force_routine_email: bool = False,
    max_retries: int = 1
) -> dict:
    """
    Self-healing pipeline runner with automatic recovery from mid-cycle SAP kickouts.
    If connection is dropped or error popup appears in the middle of execution:
    1. Dismisses error popup and cleans up dead session.
    2. Cold-starts fresh SAP session via Enterprise SSO.
    3. Safely re-executes pipeline from the beginning with fresh SAP extraction.
    """
    try:
        return _run_pipeline_core(
            plant=plant,
            execute_transfers=execute_transfers,
            max_transfers=max_transfers,
            send_email=send_email,
            to_recipients=to_recipients,
            cycle_type=cycle_type,
            force_daily_report=force_daily_report,
            force_routine_email=force_routine_email
        )
    except Exception as e:
        if max_retries > 0:
            logger.warning("==========================================================================")
            logger.warning(f" ⚠️ [PIPELINE INTERRUPTION] Cycle failed due to error: {e}")
            logger.warning("    -> Initiating Self-Healing: Dismissing kick-out dialogs & restarting SAP...")
            logger.warning("==========================================================================")
            time.sleep(3)
            try:
                ensure_sap_open()
            except Exception as e_launch:
                logger.error(f"    ❌ SAP recovery launch failed: {e_launch}")

            logger.info("🔄 [RETRYING] Re-executing pipeline from beginning with fresh SAP session...")
            return run_pipeline(
                plant=plant,
                execute_transfers=execute_transfers,
                max_transfers=max_transfers,
                send_email=send_email,
                to_recipients=to_recipients,
                cycle_type=cycle_type,
                force_daily_report=force_daily_report,
                force_routine_email=force_routine_email,
                max_retries=max_retries - 1
            )
        else:
            logger.error(f"❌ [PIPELINE TERMINATED] Run failed after retry attempt: {e}")
            raise


def get_next_schedule_target(now: datetime, grace_seconds: float = 0.0) -> tuple:
    """
    Computes the exact next schedule timestamp and interval type based on:
    - 08:00 ~ 17:00: Every 30 minutes (:00, :30) -> 30분 주기 (주간)
    - 17:01 ~ 07:59: Every 1 hour (:00) -> 1시간 주기 (야간)
    If grace_seconds > 0 (e.g. 60s on daemon startup) and now is within <= grace_seconds
    after a scheduled slot, returns that slot immediately so late daemon startup does not skip it.
    """
    from datetime import timedelta
    slots = []
    # 00:00 to 07:00 (hourly)
    for h in range(0, 8):
        slots.append((h, 0, "1-hour cycle (Night)"))
    # 08:00 to 17:00 (30-min)
    for h in range(8, 17):
        slots.append((h, 0, "30-min cycle (Daytime)"))
        slots.append((h, 30, "30-min cycle (Daytime)"))
    slots.append((17, 0, "30-min cycle (Daytime)"))
    # 18:00 to 23:00 (hourly)
    for h in range(18, 24):
        slots.append((h, 0, "1-hour cycle (Night)"))

    # Check slots today (including startup grace window if enabled)
    for h, m, label in slots:
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if grace_seconds > 0.0 and 0.0 <= (now - target).total_seconds() <= grace_seconds:
            return target, label
        if target > now:
            return target, label

    # Tomorrow's first slot (00:00)
    tomorrow = now + timedelta(days=1)
    target = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
    return target, "1-hour cycle (Night)"


LOCK_FILE = BASE_DIR / "hourly_gi_pipeline.lock"

def acquire_singleton_lock() -> bool:
    """
    Guarantees that only ONE instance of the continuous daemon runs at any time.
    If an existing instance is active, the newcomer yields and exits cleanly (sys.exit(0)).
    If the lock file is stale (process dead or non-existent), the newcomer takes over the lock.
    """
    import psutil
    current_pid = os.getpid()
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text().strip())
            if old_pid != current_pid and psutil.pid_exists(old_pid):
                proc = psutil.Process(old_pid)
                cmd_line = " ".join(proc.cmdline())
                if "hourly_gi" in cmd_line:
                    logger.warning(f"⚠️ [SINGLETON LOCK] Active daemon already running (PID {old_pid}). Newcomer (PID {current_pid}) yielding and exiting cleanly.")
                    sys.exit(0)
        except Exception as e:
            logger.warning(f"⚠️ [SINGLETON LOCK] Error inspecting existing PID: {e}")

    try:
        LOCK_FILE.write_text(str(current_pid))
    except Exception as e_write:
        logger.warning(f"⚠️ [SINGLETON LOCK] Could not write lock file: {e_write}")
    return True


def start_24h_daemon(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    execute_transfers: bool = True,
    max_transfers: int = None,
    send_email: bool = True,
    to_recipients: list = None
):
    """
    Runs the 24/7 continuous autonomous resolution pipeline:
    - 08:00 ~ 17:00: Every 30 minutes (:00, :30)
    - 17:01 ~ 07:59: Every 1 hour (:00)
    Aligned strictly to schedule clock slots (no premature startup runs).
    """
    acquire_singleton_lock()
    logger.info(_verify_ops_env_log("01_JOB_GI_DAEMON"))

    logger.info("==========================================================================")
    logger.info(" ⏰ [24/7 CONTINUOUS DAEMON] Starting 24H Autonomous GI Error Pipeline...")
    logger.info("    • 08:00 ~ 17:00 EST: 30분 주기 주간 정밀 해소 & 보고 (:00, :30 정시)")
    logger.info("    • 17:01 ~ 07:59 EST: 1시간 주기 야간 무인 해소 & 보고 (:00 정시)")
    logger.info("    • Singleton Protection: 1대 인스턴스 독점 보장 (중복 프로세스 자동 차단)")
    logger.info("    • Slot Deduplication: 동일 시간대 리포트 이메일 중복 발송 원천 차단")
    logger.info("==========================================================================")

    # Infinite 24/7 clock-aligned loop (with 60s startup grace window on first cycle)
    is_first_cycle = True
    while True:
        now = datetime.now()
        grace_sec = 60.0 if is_first_cycle else 0.0
        is_first_cycle = False
        next_target, next_label = get_next_schedule_target(now, grace_seconds=grace_sec)
        wait_seconds = (next_target - now).total_seconds()
        if wait_seconds <= 0:
            logger.info(
                f"⏰ [24/7 SCHEDULER] Startup within {grace_sec:.0f}s grace window of slot "
                f"{next_target.strftime('%Y-%m-%d %H:%M:%S')} ({next_label}) — executing immediately!"
            )
        else:
            logger.info(
                f"⏰ [24/7 SCHEDULER] Next cycle aligned to: {next_target.strftime('%Y-%m-%d %H:%M:%S')} "
                f"({next_label} | Wait: {wait_seconds/60:.1f}m)..."
            )
            # Sleep in intervals of up to 15 seconds to ensure clean response and 0% CPU
            while datetime.now() < next_target:
                remaining = (next_target - datetime.now()).total_seconds()
                time.sleep(min(15.0, max(1.0, remaining)))

        try:
            run_pipeline(
                plant=plant,
                execute_transfers=execute_transfers,
                max_transfers=max_transfers,
                send_email=send_email,
                to_recipients=to_recipients,
                cycle_type=next_label
            )
        except Exception as e:
            logger.error(f"[DAEMON ERROR] Pipeline cycle exception: {e}")


def main():
    parser = argparse.ArgumentParser(description="24/7 Continuous SAP GI Error Resolution Pipeline")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="Plant (default: P001)")
    parser.add_argument("--daemon", action="store_true", help="Run 24/7 continuous autonomous daemon (08-17h: 30m, night: 1h)")
    parser.add_argument("--loop", type=float, default=0, help="Run repeatedly every N hours (legacy override)")
    parser.add_argument("--dry-run", action="store_true", help="Generate resolution plan only, do not post to SAP")
    parser.add_argument("--no-post", action="store_true", help="Strict read-only test mode (No MB1B postings, SAP queries only)")
    parser.add_argument("--no-email", action="store_true", help="Disable sending email report")
    parser.add_argument("--daily-report", action="store_true", help="Force sending 24H daily cumulative report to user now")
    parser.add_argument("--force-email", action="store_true", help="Force sending routine cycle report email")
    parser.add_argument("--max", type=int, default=None, help="Limit number of transfers per run")
    parser.add_argument("--to", type=str, nargs="+", default=None, help="Custom email recipients")
    args = parser.parse_args()

    execute_postings = not (args.dry_run or args.no_post)
    send_email = not args.no_email

    if args.dry_run and not args.daemon and args.loop <= 0:
        import glob as _glob
        _verify_ops_env_log("01_JOB_GI_DRYRUN")
        gi_files = sorted(_glob.glob(str(BASE_DIR / "gi_errors" / "GI_Error_*.XLS*")), key=os.path.getmtime, reverse=True)
        gr_files = sorted(_glob.glob(str(BASE_DIR / "gr_errors" / "GR_Error_*.XLS*")), key=os.path.getmtime, reverse=True)
        gi_path = Path(gi_files[0]) if gi_files else None
        gr_path = Path(gr_files[0]) if gr_files else None
        if gi_path and gi_path.exists():
            plan_df = resolve_gi_errors(
                gi_path=gi_path,
                gr_path=gr_path if (gr_path and gr_path.exists()) else None,
                save_excel=True,
                record_signals=False,
                dry_run=True,
            )
            out_file = plan_df.attrs.get("output_file", "")
            ready_cnt = len(plan_df[plan_df["Action"] == "TRANSFER_POSTING"]) if not plan_df.empty and "Action" in plan_df.columns else 0
        else:
            scratch_dir = BASE_DIR / "scratch"
            scratch_dir.mkdir(parents=True, exist_ok=True)
            out_p = scratch_dir / f"DRYRUN_GI_Resolution_Plan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            pd.DataFrame().to_excel(out_p, index=False)
            out_file = str(out_p)
            plan_df = pd.DataFrame()
            ready_cnt = 0
        logger.info(
            f"✅ [01_JOB DRY-RUN COMPLETED] GI Input={gi_path} | GR Input={gr_path} | "
            f"Plan Rows={len(plan_df)} (Ready={ready_cnt}) | Output={out_file} "
            f"(No SAP Session, No DB Write, No Email)"
        )
        return

    if args.daemon:
        start_24h_daemon(
            plant=args.plant,
            execute_transfers=execute_postings,
            max_transfers=args.max,
            send_email=send_email,
            to_recipients=args.to
        )
    elif args.loop > 0:
        logger.info(f"[ROUTINE MODE] Pipeline scheduled to run every {args.loop} hour(s) with email reporting.")
        while True:
            try:
                run_pipeline(
                    plant=args.plant,
                    execute_transfers=execute_postings,
                    max_transfers=args.max,
                    send_email=send_email,
                    to_recipients=args.to,
                    force_daily_report=args.daily_report,
                    force_routine_email=args.force_email
                )
            except Exception as e:
                logger.error(f"[ROUTINE ERROR] Exception during pipeline run: {e}")
            
            sleep_secs = int(args.loop * 3600)
            logger.info(f"[ROUTINE SLEEP] Waiting for next run ({args.loop}h / {sleep_secs}s)...")
            time.sleep(sleep_secs)
    else:
        run_pipeline(
            plant=args.plant,
            execute_transfers=execute_postings,
            max_transfers=args.max,
            send_email=send_email,
            to_recipients=args.to,
            force_daily_report=args.daily_report,
            force_routine_email=args.force_email
        )


if __name__ == "__main__":
    main()
