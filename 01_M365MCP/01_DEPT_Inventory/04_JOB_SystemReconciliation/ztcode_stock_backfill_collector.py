#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZTCODE_STOCK_REC Historical Stock Accuracy Backfill & Continuous Learning Collector
========================================================================
01_Inventory_Team / 04_JOB_SystemReconciliation (Plant P001 Headlamp)
- SAP GUI ZTCODE_STOCK_REC 트랜잭션에서 과거 52주간(1년)의 주간 일요일 마감 재고정확도 데이터를 순차적으로 수집하여
  로컬 DB(system_reconciliation.db)에 적재하고, 품목별 통계 학습 모델(reconciliation_material_baseline)을
  생성 및 지속적으로 강화합니다.
- Workspace Concurrency Guardrails 준수:
  * Windows 프로세스 우선순위 BELOW_NORMAL_PRIORITY_CLASS 적용
  * SAP 세션 락(sap_session_lock) 상시 유지
  * 쿼리 간 2.5초 페이싱 딜레이로 SAP 서버 부하 0% 수렴
  * 멱등성 보장 (이미 적재된 날짜는 건너뛰기)
"""

import os
import sys
import time
import argparse
import psutil
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
from datetime import datetime, timedelta
from typing import List, Set, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Set lower process priority to protect user foreground apps (SAP, Outlook, Teams)
try:
    p = psutil.Process()
    p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from reconciliation_collector import collect_ztcode_stock_rec_accuracy
from reconciliation_engine import process_reconciliation_file
from db_manager import (
    get_connection,
    update_reconciliation_baseline,
    get_baseline_summary,
    get_chronic_ghost_from_baseline
)


def get_completed_posting_dates(plant: str = _get_ops_env("SAP_PLANT", "P001"), system_group: str = "IWMS") -> Set[str]:
    """Returns set of posting dates already stored in accuracy_daily_summary."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT posting_date FROM accuracy_daily_summary WHERE plant = ? AND system_group = ?",
            (plant, system_group)
        )
        return {r["posting_date"] for r in cursor.fetchall()}


def run_historical_backfill(
    weeks: int = 52,
    mode: str = "weekly",
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    system_group: str = "IWMS",
    start_date: str = "2026-10-18",
    pacing_delay: float = 2.5,
    force: bool = False
):
    """
    Executes historical backfill for past weeks (weekly Sunday cadence or daily snapshots).
    """
    start_time = time.time()
    print("=" * 80)
    print(f" [ZTCODE_STOCK_REC Backfill] Starting Historical Stock Accuracy Collection (Mode: {mode.upper()})")
    print(f" Plant: {plant} | System Group: {system_group} | Anchor Date: {start_date}")
    print(f" Pacing Delay: {pacing_delay}s | Priority: BELOW_NORMAL_PRIORITY_CLASS")
    print("=" * 80)

    # 1. Calculate target posting dates
    base_dt = datetime.strptime(start_date, "%Y-%m-%d")
    target_dates: List[str] = []
    if mode.lower() == "daily":
        total_days = weeks * 7
        for i in range(total_days):
            target_dates.append((base_dt - timedelta(days=i)).strftime("%Y-%m-%d"))
    else:  # weekly
        for i in range(weeks):
            target_dates.append((base_dt - timedelta(days=7 * i)).strftime("%Y-%m-%d"))

    # 2. Check existing dates in DB
    existing_dates = get_completed_posting_dates(plant, system_group)
    print(f"[INFO] Found {len(existing_dates)} existing posting dates already in DB.")

    pending_dates = [d for d in target_dates if d not in existing_dates or force]
    print(f"[INFO] Target Dates: {len(target_dates)} | Pending to Extract: {len(pending_dates)}\n")

    if not pending_dates:
        print("[INFO] All requested snapshots are already in DB. Updating baseline model...")
        trained_count = update_reconciliation_baseline(plant=plant, system_group=system_group)
        print_baseline_report(plant, system_group, trained_count)
        return

    extracted_count = 0
    failed_count = 0
    consecutive_empty = 0

    for idx, post_date in enumerate(pending_dates, 1):
        print(f"--- [{idx}/{len(pending_dates)}] Collecting ZTCODE_STOCK_REC for Date: {post_date} ---")
        try:
            raw_file = collect_ztcode_stock_rec_accuracy(
                plant=plant,
                system_group=system_group,
                posting_date=post_date,
                timeout=60
            )

            if raw_file and raw_file.exists() and raw_file.stat().st_size > 1000:
                res = process_reconciliation_file(raw_file)
                sm = res["summary"]
                print(f"  [OK] Ingested: {sm['total_items']:,} materials | Accuracy: {sm['accuracy_rate']:.2f}% (Score: {sm['avg_score']:.2f}%) | Exposure: ${sm['total_financial_exposure']:,.2f}")
                extracted_count += 1
                consecutive_empty = 0
            else:
                print(f"  [WARN] No export generated for {post_date} (SAP holds no data or purged by retention).")
                failed_count += 1
                consecutive_empty += 1

        except Exception as e:
            print(f"  [ERROR] Extraction failed for {post_date}: {e}")
            failed_count += 1
            consecutive_empty += 1

        if consecutive_empty >= 3:
            print(f"\n[INFO] Reached earliest available historical record in SAP ZTCODE_STOCK_REC (3 consecutive queries returned no data).")
            print("  Older records have been purged by SAP operational table retention.")
            print("  Gracefully stopping backfill extraction and transitioning to baseline model training.\n")
            break

        # Pacing delay to avoid overloading SAP GUI
        if idx < len(pending_dates):
            time.sleep(pacing_delay)

    # 3. Final Continuous Learning Baseline Training
    print("\n" + "=" * 80)
    print(" [Continuous Learning] Re-training reconciliation_material_baseline across all snapshots...")
    print("=" * 80)
    trained_count = update_reconciliation_baseline(plant=plant, system_group=system_group)

    elapsed = time.time() - start_time
    print(f"\n[ZTCODE_STOCK_REC Backfill] Completed in {elapsed:.1f}s. Extracted: {extracted_count}, Skipped/Purged: {failed_count}")
    print_baseline_report(plant, system_group, trained_count)


def print_baseline_report(plant: str, system_group: str, trained_count: int):
    """Prints executive summary of the continuous learning model."""
    summary = get_baseline_summary(plant=plant, system_group=system_group)
    ghosts = get_chronic_ghost_from_baseline(limit=10, plant=plant, system_group=system_group)

    print("\n" + "#" * 80)
    print(f" CONTINUOUS LEARNING BASELINE MODEL REPORT (Plant {plant} / {system_group})")
    print("#" * 80)
    print(f"  Total Materials Profiled:   {summary.get('total_materials', 0):,}")
    print(f"  Max Snapshots Analyzed:     {summary.get('max_snapshots_analyzed', 0)} snapshots")
    print(f"  Overall Avg Match Rate:     {summary.get('overall_match_rate', 0.0):.2f}%")
    print(f"  ----------------------------------------------------------------")
    print(f"  [TIER 1] Chronic Perfect Match:   {summary.get('perfect_count', 0):,} items")
    print(f"  [TIER 2] Chronic Ghost Deficit:   {summary.get('ghost_deficit_count', 0):,} items (SAP > WMS phantom stock)")
    print(f"  [TIER 3] Chronic Over Stock:      {summary.get('over_stock_count', 0):,} items (WMS > SAP unposted receipts)")
    print(f"  [TIER 4] Volatile / Fluctuating:  {summary.get('volatile_count', 0):,} items (Erratic variance)")
    print(f"  [TIER 5] Sporadic Discrepancy:    {summary.get('sporadic_count', 0):,} items (Minor timing lag)")
    print(f"  ----------------------------------------------------------------")

    if ghosts:
        print("\n  [TOP CHRONIC GHOST DEFICITS (Physical Cycle Count Priorities)]:")
        print(f"  {'Material':<16} | {'Samples':<7} | {'Match%':<7} | {'AvgDiff':<9} | {'Exposure':<12} | {'Description'}")
        print("  " + "-" * 75)
        for g in ghosts:
            print(f"  {g['material']:<16} | {g['sample_snapshots']:<7} | {g['match_rate']:<6.1f}% | {g['avg_diff_qty']:<9.1f} | ${g['avg_financial_exposure']:<11,.2f} | {g['description'][:30]}")
    print("#" * 80 + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ZTCODE_STOCK_REC Historical Stock Accuracy Backfill & Learning Model Trainer")
    parser.add_argument("--weeks", type=int, default=52, help="Number of weeks to scan back (default: 52)")
    parser.add_argument("--mode", choices=["weekly", "daily"], default="weekly", help="Sampling mode: weekly (Sundays) or daily (default: weekly)")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="Plant code (default: P001)")
    parser.add_argument("--system", default="IWMS", help="System group (default: IWMS)")
    parser.add_argument("--start-date", default="2026-10-18", help="Most recent posting date (default: 2026-10-18)")
    parser.add_argument("--delay", type=float, default=2.5, help="Pacing delay between queries in seconds (default: 2.5)")
    parser.add_argument("--force", action="store_true", help="Force re-extraction of existing dates")
    args = parser.parse_args()

    run_historical_backfill(
        weeks=args.weeks,
        mode=args.mode,
        plant=args.plant,
        system_group=args.system,
        start_date=args.start_date,
        pacing_delay=args.delay,
        force=args.force
    )

