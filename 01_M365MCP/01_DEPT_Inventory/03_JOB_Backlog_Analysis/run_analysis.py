"""
Main CLI Runner for 03_Backlog_Analysis
=======================================
Executes multi-dimensional backlog time-series analytics, generates Excel/MD reports,
and dispatches Outlook notification.

Usage:
  python run_analysis.py                  # Analyzes and generates Excel/Markdown reports
  python run_analysis.py --preview        # Also prints terminal dashboard
  python run_analysis.py --email          # Dispatches email with Excel attachment to System Architect
  python run_analysis.py --start-date 2026-10-23 --end-date 2026-10-31
"""

import sys
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

from config import TARGET_START_DATE, MILESTONE_ANALYSIS_DATE, REPORTS_DIR
from analytics_engine import BacklogAnalyticsEngine
from report_generator import BacklogReportGenerator
from notifier import send_analysis_report

logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")
logger = logging.getLogger("BacklogAnalysisRunner")


def ensure_today_backlog_accumulated(target_date: str = None) -> bool:
    """
    Checks if today's backlog data is accumulated in backlog_history.db.
    Per D66: 03_JOB NEVER triggers 02_JOB as a subprocess (no SAP posting or COGI manipulation).
    If today's data is missing, returns False so caller can proceed with available data
    and flag a missing date notice at the top of the report.
    """
    today_str = datetime.now().strftime("%Y-%m-%d")
    check_date = target_date or today_str

    # Past dates do not require real-time execution today
    if check_date < today_str:
        return True

    from config import DB_BACKLOG_PATH
    import sqlite3

    raw_count = 0
    metric_count = 0
    if DB_BACKLOG_PATH.exists():
        try:
            conn = sqlite3.connect(f"file:{DB_BACKLOG_PATH}?mode=ro", uri=True)
            try:
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM raw_backlog_history WHERE run_date = ?;", (check_date,))
                raw_count = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM daily_backlog_metrics WHERE run_date = ?;", (check_date,))
                metric_count = c.fetchone()[0]
            finally:
                conn.close()
        except Exception as e:
            logger.warning(f"⚠️ Could not check backlog DB: {e}")

    # If today's data is already recorded
    if raw_count > 0 and metric_count > 0:
        logger.info(
            f"✅ [PRE-ANALYSIS CHECK] Verified today's ({check_date}) Backlog data is accumulated "
            f"({raw_count} raw backlog items archived, metric recorded)."
        )
        return True

    # If today's data is missing, log warning only (D66: NO SUBPROCESS TO 02)
    logger.warning(
        f"⚠️ [PRE-ANALYSIS CHECK] Today's ({check_date}) Backlog data has not been accumulated yet. "
        f"(D66: 03_JOB will NOT trigger 02_JOB. Analysis proceeds with available data, missing notice will be flagged.)"
    )
    return False


def main():
    _verify_ops_env_log("03_JOB_ANALYSIS")
    today_str = datetime.now().strftime("%Y-%m-%d")
    parser = argparse.ArgumentParser(description="03_Backlog_Analysis Master Analytics Engine")
    parser.add_argument("--start-date", type=str, default=TARGET_START_DATE, help=f"Start date (YYYY-MM-DD, default: {TARGET_START_DATE})")
    parser.add_argument("--end-date", type=str, default=today_str, help=f"End date (YYYY-MM-DD, default: today {today_str})")
    parser.add_argument("--no-sync", action="store_true", help="Skip pre-analysis check/execution of today's backlog pipeline")
    parser.add_argument("--email", action="store_true", help="Send email report via Outlook")
    parser.add_argument("--to", type=str, nargs="+", default=None, help="Custom recipient email(s)")
    parser.add_argument("--preview", action="store_true", help="Print summary report to terminal")
    parser.add_argument("--dry-run", action="store_true", help="Read-only dry run: write outputs to scratch/DRYRUN_* and skip email")
    args = parser.parse_args()

    # Pre-analysis sync: Ensure today's GI and Backlog are cleared and data accumulated
    if not args.no_sync and args.end_date >= today_str:
        ensure_today_backlog_accumulated(target_date=today_str)

    logger.info("==========================================================================")
    logger.info(f" 🚀 [03_Backlog_Analysis] Starting Deep Analytics ({args.start_date} ~ {args.end_date})...")
    logger.info("==========================================================================")

    engine = BacklogAnalyticsEngine()
    result = engine.analyze(start_date=args.start_date, end_date=args.end_date)

    if not result["materials"]:
        logger.warning("⚠️ No raw backlog history records found for the specified period.")
        logger.info(f"   Target Period: {args.start_date} ~ {args.end_date}")
        logger.info("   Data accumulates daily from 02_Backlog_Clear (06:00 AM EST).")
        return

    # Generate Reports
    if args.dry_run:
        scratch_dir = Path(__file__).resolve().parent / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        generator = BacklogReportGenerator(output_dir=scratch_dir)
        excel_path = generator.generate_excel_report(result, dry_run=True)
        logger.info(f"   📊 [DRY-RUN] Multi-Tab Excel Report Generated: {excel_path}")

        md_content = generator.generate_markdown_summary(result)
        md_path = scratch_dir / f"DRYRUN_Backlog_Deep_Analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logger.info(f"   📝 [DRY-RUN] Markdown Summary Report Generated: {md_path}")
    else:
        generator = BacklogReportGenerator(output_dir=REPORTS_DIR)
        excel_path = generator.generate_excel_report(result)
        logger.info(f"   📊 Multi-Tab Excel Report Generated: {excel_path.name}")

        md_content = generator.generate_markdown_summary(result)
        md_path = REPORTS_DIR / f"Backlog_Deep_Analysis_{datetime.now().strftime('%Y%m%d')}.md"
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        logger.info(f"   📝 Markdown Summary Report Generated: {md_path.name}")

    # Terminal preview
    if args.preview or (not args.email and not args.dry_run):
        print("\n" + md_content + "\n")

    # Email dispatch
    if args.email and not args.dry_run:
        logger.info("📧 Dispatching Analysis Report Email via Outlook...")
        custom_recipients = {"to": args.to, "cc": []} if args.to else None
        success = send_analysis_report(result, excel_attachment_path=excel_path, recipients=custom_recipients)
        if success:
            logger.info("🎉 Email dispatched successfully!")
        else:
            logger.error("❌ Failed to send email.")

    logger.info("==========================================================================")
    logger.info(" ✅ [03_Backlog_Analysis] Analysis completed successfully!")
    logger.info("==========================================================================")


if __name__ == "__main__":
    main()
