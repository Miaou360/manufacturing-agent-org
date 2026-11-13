#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
04_JOB_SystemReconciliation Execution Entry Point
==================================================
Department: 01_DEPT_Inventory
- 매일 06:35 AM EST에 스케줄러에 의해 실행되어 ZTCODE_STOCK_REC 전일 마감 재고정확도를 자동 수집,
  DB 적재, AI 기여도 분석 및 일일 Markdown/Excel 리포트를 생성합니다.
- 대용량 원본 파일 7일 보존 및 자동 정리(storage_retention) 연동.
"""

import os
import sys
import argparse
import time
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
# Load environment variables from 01_M365MCP/.env
from dotenv import load_dotenv
ENV_PATH = BASE_DIR.parents[1] / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in sys.path:
    sys.path.append(str(BASE_DIR.parent))  # for storage_retention

from reconciliation_collector import collect_ztcode_stock_rec_accuracy
from reconciliation_engine import process_reconciliation_file
from reconciliation_notifier import (
    generate_reconciliation_report,
    generate_reconciliation_excel,
    send_reconciliation_email,
    print_console_summary
)
from db_manager import get_latest_summary, get_top_financial_risks, get_baseline_summary


def main():
    _verify_ops_env_log("04_JOB_RECONCILIATION")
    parser = argparse.ArgumentParser(description="04_JOB_SystemReconciliation Pipeline Launcher")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="Target plant (default: P001)")
    parser.add_argument("--system", default="IWMS", help="System group: IWMS, OWMS, MES, ALL (default: IWMS)")
    parser.add_argument("--skip-extract", action="store_true", help="Skip SAP GUI extraction and parse latest raw export")
    parser.add_argument("--report-only", action="store_true", help="Generate report from existing DB data")
    parser.add_argument("--email", action="store_true", help="Send report email via Outlook")
    parser.add_argument("--test", action="store_true", help="Test mode (email sent only to TEST_EMAIL_RECIPIENT)")
    parser.add_argument("--dry-run", action="store_true", help="Read-only dry run: write outputs to scratch/DRYRUN_* without SAP extract, DB write, or email")
    args = parser.parse_args()

    start_time = time.time()
    print(f"\n[04_JOB_SystemReconciliation] ===================================================")
    print(f"  Starting Pipeline at {datetime.now().strftime('%Y-%m-%d %H:%M:%S EST')}")
    print(f"  Plant: {args.plant} | System Group: {args.system} | Test Mode: {args.test} | Dry Run: {args.dry_run}")
    print(f"==================================================================================")

    if args.dry_run:
        import glob as _glob
        latest_candidate = BASE_DIR / "raw_exports" / f"ZTCODE_STOCK_REC_StockAccuracy_{args.system}_latest.txt"
        raw_candidates = sorted(
            _glob.glob(str(BASE_DIR / "raw_exports" / f"ZTCODE_STOCK_REC_StockAccuracy_{args.system}_*.txt")),
            key=os.path.getmtime,
            reverse=True,
        )
        raw_file = latest_candidate if latest_candidate.exists() else (Path(raw_candidates[0]) if raw_candidates else None)
        attribution = None
        if raw_file and raw_file.exists():
            res = process_reconciliation_file(raw_file, dry_run=True)
            summary = res["summary"]
            attribution = res["attribution"]
        else:
            summary = get_latest_summary(plant=args.plant, system_group=args.system)
        if not summary:
            print("[ERROR] No raw export or DB summary available for dry-run.")
            sys.exit(1)
        report_path = generate_reconciliation_report(summary=summary, attribution=attribution, dry_run=True)
        excel_path = generate_reconciliation_excel(summary=summary, dry_run=True)
        print(f"[DRY-RUN OK] Markdown Report: {report_path}")
        print(f"[DRY-RUN OK] Excel Report:    {excel_path}")
        return

    if args.report_only:
        summary = get_latest_summary(plant=args.plant, system_group=args.system)
        if not summary:
            print("[ERROR] No data in DB to generate report.")
            sys.exit(1)
        report_path = generate_reconciliation_report(summary=summary)
        excel_path = generate_reconciliation_excel(summary=summary)
        if args.email:
            send_reconciliation_email(report_path, summary, excel_path=excel_path, is_test=args.test)
        print(f"[OK] Report generated: {report_path}")
        print(f"[OK] Excel report generated: {excel_path}")
        return

    raw_file = None
    if args.skip_extract:
        latest_candidate = BASE_DIR / "raw_exports" / f"ZTCODE_STOCK_REC_StockAccuracy_{args.system}_latest.txt"
        if latest_candidate.exists():
            raw_file = latest_candidate
            print(f"[INFO] Skipping SAP extraction. Using: {raw_file.name}")
        else:
            print(f"[WARN] Latest raw export not found. Falling back to SAP extraction.")

    if raw_file is None:
        raw_file = collect_ztcode_stock_rec_accuracy(plant=args.plant, system_group=args.system)
        if raw_file is None:
            latest_candidate = BASE_DIR / "raw_exports" / f"ZTCODE_STOCK_REC_StockAccuracy_{args.system}_latest.txt"
            if latest_candidate.exists():
                print(f"[WARN] Live SAP GUI extraction failed. Using latest available export: {latest_candidate.name}")
                raw_file = latest_candidate
            else:
                print("[ERROR] Failed to extract data from SAP GUI ZTCODE_STOCK_REC and no fallback available.")
                sys.exit(1)

    # Process extracted file
    print(f"\n[INFO] Processing file: {raw_file.name}...")
    res = process_reconciliation_file(raw_file)
    summary = res["summary"]
    attribution = res["attribution"]

    # Generate Markdown and Multi-Tab Excel Report
    report_path = generate_reconciliation_report(summary=summary, attribution=attribution)
    excel_path = generate_reconciliation_excel(summary=summary)
    print(f"  [OK] Excel Report Generated: {excel_path.name}")

    if args.email:
        send_reconciliation_email(report_path, summary, excel_path=excel_path, is_test=args.test)

    top_risks = get_top_financial_risks(summary["snapshot_date"], limit=5)
    print_console_summary(summary, top_risks)

    # Continuous Learning Baseline Reinforcement Log
    baseline = get_baseline_summary(plant=summary["plant"], system_group=summary["system_group"])
    if baseline and baseline.get("total_materials", 0) > 0:
        print(f"\n[Continuous Learning] ZTCODE_STOCK_REC Baseline Model Reinforcement Status:")
        print(f"  • Total Materials Profiled: {baseline['total_materials']:,} (Max Analyzed: {baseline['max_snapshots_analyzed']} snapshots)")
        print(f"  • Perfect Match: {baseline['perfect_count']:,} ({baseline.get('overall_match_rate', 0):.1f}%) | Ghost Deficit: {baseline['ghost_deficit_count']:,} | Over Stock: {baseline['over_stock_count']:,} | Volatile: {baseline['volatile_count']:,}")

    # Automated Storage Retention (Prune exports older than 7 days)
    try:
        from storage_retention import run_retention_pruning
        run_retention_pruning(retention_days=7)
    except Exception as e_prune:
        print(f"  [WARN] Storage retention pruning: {e_prune}")

    elapsed = time.time() - start_time
    print(f"[04_JOB_SystemReconciliation] Pipeline completed successfully in {elapsed:.1f}s.")
    print(f"  Markdown Report: {report_path.resolve()}")
    print(f"  Excel Report:    {excel_path.resolve()}\n")


if __name__ == "__main__":
    main()
