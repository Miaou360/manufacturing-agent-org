#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
05_JOB_SL01_R_Bins Master Pipeline Launcher
===========================================
Department: 01_DEPT_Inventory
Plant: P001 Headlamp
Schedule: Daily 04:15 AM (Mon-Sat, Sunday excluded)

Autonomous pipeline to detect and alert on stalled SL01 in-transit inventory
lingering in Rack (R) storage bins for more than 1 day.
"""

import os
import sys
import glob
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

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SL01R_Launcher")

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

STOCK_ACC_DIR = BASE_DIR.parent / "00_JOB_StockAccuracy"
if str(STOCK_ACC_DIR) not in sys.path:
    sys.path.append(str(STOCK_ACC_DIR))

try:
    from wms_crawler import download_wms_stock_list
except ImportError:
    download_wms_stock_list = None

from sl01_r_bins_operator import (
    run_daily_scan, generate_portrait_excel_report,
    send_outlook_notification, log_scan_run
)


def run_sl01_r_bins_pipeline(
    test_mode: bool = False,
    override_to: str = None,
    force: bool = False,
    skip_crawl: bool = False,
    dry_run: bool = False
) -> dict:
    """Executes the complete SL01 R-bin lag detection and reporting pipeline."""
    if dry_run:
        skip_crawl = True
    now = datetime.now()
    batch_id = now.strftime("RUN_%Y%m%d_%H%M%S")

    logger.info("==========================================================================")
    logger.info(f" 🚀 [05_JOB_SL01_R_Bins] Starting Daily Lag Monitoring Run (Batch: {batch_id})")
    logger.info("==========================================================================")

    # Schedule Check: Monday through Saturday only (Sunday = 6)
    if now.weekday() == 6 and not force:
        logger.info("ℹ️ Today is Sunday. SL01 R-Bin lag monitoring is scheduled Mon-Sat only. Skipping.")
        return {"status": "SKIPPED_SUNDAY", "processed": 0}

    # Step 0: Download Fresh WMS Stock Snapshot (D21/D39 Decoupled Execution)
    fresh_wms = None
    snapshot_path = None
    is_fallback = False
    fallback_file_time = None
    fallback_expired = False

    if skip_crawl:
        logger.info("\n>>> [STEP 0/3 SKIPPED] --skip-crawl flag set: Using latest existing WMS snapshot.")
    else:
        logger.info("\n>>> [STEP 0/3] Downloading Fresh WMS Stock Snapshot at 04:15 AM (D21/D39/D41)...")
        if download_wms_stock_list:
            try:
                HQ_DIR = BASE_DIR.parent.parent / "00_HQ_TaskManager"
                if str(HQ_DIR) not in sys.path:
                    sys.path.insert(0, str(HQ_DIR))
                from collect_retry import execute_with_retry, FatalCollectionError

                def _do_wms_download():
                    res = download_wms_stock_list(headless=True)
                    if not res or not Path(res).exists():
                        raise RuntimeError("WMS crawl returned None or file missing")
                    return res

                fresh_wms, metrics = execute_with_retry(
                    collector_func=_do_wms_download,
                    job_name="05_JOB_SL01_R_Bins_WMS",
                    time_budget_seconds=900.0,
                    max_retries=3,
                    backoff_delays=[30.0, 60.0, 120.0]
                )
                if fresh_wms and Path(fresh_wms).exists():
                    logger.info(f"✅ Fresh WMS Snapshot obtained: {Path(fresh_wms).name} (Attempts: {metrics['attempts']})")
                    snapshot_path = Path(fresh_wms)
            except FatalCollectionError as e_fatal:
                logger.error(f"🛑 Fatal credential error during WMS download (0 retries): {e_fatal}")
            except Exception as e_crawl:
                logger.error(f"❌ Error during WMS download after retries: {e_crawl}. Falling back to latest available snapshot...")
        else:
            logger.warning("⚠️ wms_crawler module not available. Falling back to latest available snapshot...")

    # Fallback Resolution if fresh download did not yield file
    if not snapshot_path:
        is_fallback = True
        daily_stack = STOCK_ACC_DIR / "wms_daily_stack"
        files = sorted(glob.glob(str(daily_stack / "*.xlsx")), key=os.path.getmtime, reverse=True)
        if not files:
            logger.error("❌ No fallback WMS snapshot found in stack. Pipeline cannot proceed.")
            return {"status": "FAILED_NO_SNAPSHOT"}
        snapshot_path = Path(files[0])
        file_mtime = datetime.fromtimestamp(os.path.getmtime(snapshot_path))
        fallback_file_time = file_mtime.strftime("%Y-%m-%d %H:%M:%S")
        age_hours = (now - file_mtime).total_seconds() / 3600.0
        logger.warning(f"⚠️ Using Fallback WMS Snapshot: {snapshot_path.name} (Age: {age_hours:.1f}h, Mtime: {fallback_file_time})")

        if age_hours > 24.0:
            fallback_expired = True
            logger.error(f"🚨 Fallback snapshot is older than 24 hours ({age_hours:.1f}h). Suppressing stale list.")

    # Expiration branch: Send failure alert only without stale list (never send email in dry_run)
    if fallback_expired and not dry_run:
        logger.warning(">>> Fallback snapshot > 24h: Suppressing list generation, sending failure alert only.")
        email_ok = send_outlook_notification(
            report_file=None,
            scan_date_str=now.strftime("%Y-%m-%d"),
            test_mode=test_mode,
            override_to=override_to,
            is_fallback=True,
            fallback_file_time=fallback_file_time,
            fallback_expired=True
        )
        return {
            "status": "FALLBACK_EXPIRED_ALERT_SENT",
            "fallback_file": str(snapshot_path),
            "fallback_file_time": fallback_file_time,
            "email_sent": email_ok
        }

    # Step 1: Scan WMS snapshot (Freeze observations if fallback)
    logger.info(f"\n>>> [STEP 1/3] Scanning WMS Stock Snapshot (Fallback: {is_fallback}, DryRun: {dry_run})...")
    if dry_run:
        import shutil
        import tempfile
        import db_manager as _sl01_db
        tmp_db = Path(tempfile.gettempdir()) / f"sl01_dryrun_{os.getpid()}.db"
        if Path(_sl01_db.DB_PATH).exists():
            shutil.copy2(_sl01_db.DB_PATH, tmp_db)
        dry_report_path = None
        try:
            total_active, stale_count, stale_qty, scan_date = run_daily_scan(
                snapshot_file=snapshot_path,
                is_fallback=False,
                db_path=tmp_db
            )
            dry_report_path = generate_portrait_excel_report(
                scan_date_str=scan_date,
                is_fallback=False,
                db_path=tmp_db,
                dry_run=True,
            )
        finally:
            for _ext in ("", "-wal", "-shm"):
                _p = Path(str(tmp_db) + _ext)
                if _p.exists():
                    try:
                        _p.unlink()
                    except Exception:
                        pass
        logger.info(
            f"✅ [DRY-RUN COMPLETED] Input: {snapshot_path} | "
            f"Active SL01 R-Bins: {total_active}, Stale (>=2d): {stale_count}, Stale Qty: {stale_qty:,.1f} EA | "
            f"Scratch Report: {dry_report_path} (No DB write, No Email)"
        )
        return {
            "status": "DRY_RUN_COMPLETED",
            "scan_date": scan_date,
            "input_file": str(snapshot_path),
            "total_active": total_active,
            "stale_count": stale_count,
            "stale_qty": stale_qty,
            "report_file": str(dry_report_path) if dry_report_path else "",
            "email_sent": False
        }

    total_active, stale_count, stale_qty, scan_date = run_daily_scan(
        snapshot_file=snapshot_path,
        is_fallback=is_fallback
    )

    # Step 2: Generate Portrait Excel Report
    logger.info("\n>>> [STEP 2/3] Generating Portrait Excel Inspection Report (Fallback: {is_fallback})...")
    report_file = generate_portrait_excel_report(
        scan_date_str=scan_date,
        is_fallback=is_fallback,
        fallback_file_time=fallback_file_time
    )

    # Step 3: Dispatch Outlook Notification
    logger.info(f"\n>>> [STEP 3/3] Dispatching Outlook Email Notification (Test: {test_mode}, Fallback: {is_fallback})...")
    email_ok = send_outlook_notification(
        report_file=report_file,
        scan_date_str=scan_date,
        test_mode=test_mode,
        override_to=override_to,
        is_fallback=is_fallback,
        fallback_file_time=fallback_file_time,
        fallback_expired=False
    )

    # Log run metrics to DB
    source_filename = snapshot_path.name
    log_scan_run(
        run_id=batch_id,
        scan_date=scan_date,
        source_file=source_filename,
        total_active=total_active,
        stale_count=stale_count,
        stale_qty=stale_qty,
        report_file=str(report_file),
        email_status="SENT" if email_ok else "FAILED_OR_SKIPPED"
    )

    logger.info(f"🎉 Pipeline Run Completed: {stale_count} Stale Items ({stale_qty:,.1f} EA) | Report: {report_file.name}")
    return {
        "status": "COMPLETED",
        "scan_date": scan_date,
        "is_fallback": is_fallback,
        "total_active": total_active,
        "stale_count": stale_count,
        "stale_qty": stale_qty,
        "report_file": str(report_file),
        "email_sent": email_ok
    }


def main():
    _verify_ops_env_log("05_JOB_SL01_R_BINS")
    parser = argparse.ArgumentParser(description="05_JOB_SL01_R_Bins Master Launcher")
    parser.add_argument("--test", action="store_true", help="Run in test mode (send email to user only)")
    parser.add_argument("--to", type=str, default=None, help="Override recipient email for test")
    parser.add_argument("--force", action="store_true", help="Force execution even on Sunday")
    parser.add_argument("--skip-crawl", action="store_true", help="Skip WMS download and use existing latest snapshot")
    parser.add_argument("--dry-run", action="store_true", help="Read-only dry run (no crawl, no DB write, no email)")
    args = parser.parse_args()

    res = run_sl01_r_bins_pipeline(
        test_mode=args.test,
        override_to=args.to,
        force=args.force,
        skip_crawl=args.skip_crawl,
        dry_run=args.dry_run
    )
    print(res)


if __name__ == "__main__":
    main()
