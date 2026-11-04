"""
Daily Stock Accuracy Automation Pipeline (01_Inventory_Team / 00_StockAccuracy)
매일 아침 06:50 EDT에 정기 실행되는 전체 자동화 파이프라인 (07:20 전 발송):
1. WMS 당일 재고 스냅샷(Stock List) 자동 다운로드 (wms_daily_stack)
2. WMS 어제 하루치 확정 입출고(GR/GI) 다운로드 및 SQLite DB 적재 (gr_daily_stack, gi_daily_stack)
3. 1년치 트랜잭션 매칭 및 온톨로지 회전일수(Turnover Days) 마스터 전면 갱신
4. 신규 온톨로지 & 당일 재고 기반 다차원 오스캔/체류 진단 및 실사 엑셀(40개 빈) 생성
5. Outlook을 통한 [Daily] Cycle Count List 정기 알림 메일 자동 발송
6. 전 과정 상세 파일/콘솔 로깅 및 에러 추적
"""

import os
import sys
import time
import logging
import datetime
import traceback
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
        if _k == "OEM_FLOOR_BIN_PREFIX":
            _fb = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "").strip()
            if _fb and _fb not in _DUMMY_ENV_VALS:
                _st.append("FLOOR_BUFFER_BIN_PREFIXES=configured")
                continue
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Base directory & logging setup
CURRENT_DIR = Path(__file__).resolve().parent
LOGS_DIR = CURRENT_DIR / "logs"
HANDOFF_ALERT_DIR = Path.home() / "Desktop" / "CODES" / ".agents" / "handoff"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

today_str = datetime.date.today().strftime("%Y%m%d")
log_file_path = LOGS_DIR / f"daily_pipeline_{today_str}.log"

# Configure root logger
logger = logging.getLogger("DailyPipeline")
logger.setLevel(logging.INFO)

# Formatter
log_formatter = logging.Formatter(
    fmt="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# File handler
file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

# Import sub-modules
try:
    HQ_DIR = CURRENT_DIR.parent.parent / "00_HQ_TaskManager"
    if str(HQ_DIR) not in sys.path:
        sys.path.insert(0, str(HQ_DIR))
    from collect_retry import execute_with_retry, FatalCollectionError

    from wms_crawler import download_wms_stock_list
    from historical_backfill_crawler import WMSChunkCrawler
    from missing_gi_crawler import download_wms_missing_gi
    from turnover_ontology_engine import run_full_ontology_update
    from stock_verifier import StockVerifier
    from warehouse_notifier import send_warehouse_notification
    from db_manager import init_database, ingest_missing_gi_excel
except Exception as imp_err:
    logger.critical(f"모듈 임포트 실패: {imp_err}\n{traceback.format_exc()}")
    sys.exit(1)


def run_pipeline(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    headless_browser: bool = True,
    mode: str = "auto",
    skip_crawl: bool = False,
    skip_email: bool = False,
    display_first: bool = False,
    to_recipients: list = None,
    cc_recipients: list = None,
    is_test: bool = False,
    dry_run: bool = False
) -> bool:
    """
    Execute complete end-to-end stock accuracy pipeline:
    - Monday - Friday: Daily Rapid Audit (40 Bins) & Daily Email Dispatch
    - Saturday: Weekly Deep Audit (50 Bins) & Weekly Email Dispatch
    - Sunday: Complete Data Collection, DB Load & Ontology Update (Email skipped per warehouse policy)
    """
    if dry_run:
        skip_crawl = True
        skip_email = True
    _verify_ops_env_log("00_JOB_DAILY_PIPELINE")
    start_total_time = time.time()
    today = datetime.date.today()
    weekday = today.weekday()  # 0: Mon, 1: Tue, ..., 5: Sat, 6: Sun
    weekday_name = today.strftime("%A")
    now_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Execution Mode: Saturday is Weekly Deep Audit, other days (including Sunday) are Daily.
    # Note: On Sunday, data collection, DB stacking and analysis run fully, but email dispatch is skipped per warehouse policy.
    if mode.lower() == "auto":
        resolved_mode = "Weekly" if weekday == 5 else "Daily"
    else:
        resolved_mode = "Weekly" if mode.lower() == "weekly" else "Daily"
    
    logger.info("==========================================================================")
    logger.info(f" 🚀 {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Stock Accuracy Automation Pipeline ({now_ts})")
    logger.info(f"    - Schedule Day: {weekday_name} (Weekday Index: {weekday})")
    logger.info(f"    - Resolved Mode: [{resolved_mode}] Cycle Count (06:50 AM EDT)")
    logger.info(f"    - Target Facility: Plant {plant}")
    logger.info(f"    - Headless Browser: {headless_browser}")
    logger.info(f"    - Skip Crawl: {skip_crawl}")
    logger.info(f"    - Log File: {log_file_path.name}")
    logger.info("==========================================================================")

    pipeline_success = True

    # -------------------------------------------------------------------------
    # STEP 1: WMS Stock List 다운로드 (당일 06:00 스냅샷)
    # -------------------------------------------------------------------------
    if skip_crawl:
        logger.info("\n>>> [STEP 1/5 스킵] --skip-crawl 설정: 기존 WMS 스냅샷 파일을 사용합니다.")
    else:
        logger.info("\n>>> [STEP 1/5] WMS 실시간 Stock List 엑셀 다운로드 시작...")
        s1_time = time.time()
        stock_file = None
        try:
            def _do_crawl():
                res = download_wms_stock_list(headless=headless_browser)
                if not res or not Path(res).exists():
                    raise RuntimeError("WMS stock list download failed")
                return res

            stock_file, metrics = execute_with_retry(
                collector_func=_do_crawl,
                job_name="00_JOB_Daily_WMS",
                time_budget_seconds=900.0,
                max_retries=3,
                backoff_delays=[30.0, 60.0, 120.0]
            )
            if stock_file and Path(stock_file).exists():
                elapsed = time.time() - s1_time
                logger.info(f"    ✅ [STEP 1 성공] 최신 WMS 파일 다운로드 완료: {stock_file.name} ({elapsed:.1f}초, 시도: {metrics['attempts']}회)")
            else:
                logger.error("    ❌ [STEP 1 경고] WMS 파일 다운로드 실패 (신규 파일 감지 불가)")
                pipeline_success = False
        except FatalCollectionError as ef:
            logger.error(f"    🛑 [STEP 1 치명적 인증오류 (재시도 중단)]: {ef}")
            pipeline_success = False
        except Exception as e1:
            logger.error(f"    ❌ [STEP 1 예외 발생]: {e1}\n{traceback.format_exc()}")
            pipeline_success = False

        time.sleep(2)

    # -------------------------------------------------------------------------
    # STEP 2: 어제 하루치 확정 GR/GI 수집 & SQLite DB 적재
    # -------------------------------------------------------------------------
    if skip_crawl:
        logger.info("\n>>> [STEP 2/5 스킵] --skip-crawl 설정: WMS 입출고 수집을 건너뜁니다.")
    else:
        logger.info("\n>>> [STEP 2/5] WMS 어제자 입출고(GR/GI) 마감 데이터 수집 및 DB 적재 시작...")
        s2_time = time.time()
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        crawler = None
        try:
            init_database()
            logger.info(f"    - 대상 마감일: {yesterday} (00:00 ~ 23:59)")
            crawler = WMSChunkCrawler(headless=headless_browser)
            crawler._setup_browser()
            crawler._login()

            # 1. GR 수집 & 적재
            logger.info("    - GR(입고) 이력 수집 중...")
            crawler._set_date_and_download("GR", yesterday, yesterday)
            time.sleep(2)

            # 2. GI 수집 & 적재
            logger.info("    - GI(출고/이동) 이력 수집 중...")
            crawler._set_date_and_download("GI", yesterday, yesterday)
            time.sleep(2)

            # 3. Missing GI (완결 주간 일요일~토요일 소모 vs 라인 출고 차이) 수집 & DB 적재
            #    - 정기 수집일: 매주 일요일 아침 06:50 (어제 마감된 직전 일~토 완결 주간 수집)
            #    - 예외 보충일: 매주 월요일 아침 06:50 (일요일 미수집 시 자동 보충 수집)
            #    - 평일(화~토): 진행 중인 불완전 주간의 중복/왜곡 적재를 방지하고, 일요일에 학습된 마스터 베이스라인 사용
            today_dt = datetime.date.today()
            is_sunday = (today_dt.weekday() == 6)
            is_monday = (today_dt.weekday() == 0)

            if is_sunday or is_monday:
                logger.info("    - Missing GI (완결 주간 일요일~토요일 생산 소모 vs 라인 출고 차이) 수집 중...")
                try:
                    if is_sunday:
                        mgi_end = today_dt - datetime.timedelta(days=1)   # 어제 토요일
                        mgi_start = mgi_end - datetime.timedelta(days=6)  # 직전 일요일
                    else:  # is_monday
                        mgi_end = today_dt - datetime.timedelta(days=2)   # 그저께 토요일
                        mgi_start = mgi_end - datetime.timedelta(days=6)  # 직전 일요일

                    mgi_file = download_wms_missing_gi(
                        start_date=mgi_start,
                        end_date=mgi_end,
                        plant=plant,
                        driver=crawler.driver,
                        headless=headless_browser
                    )
                    if mgi_file and mgi_file.exists():
                        n_mgi = ingest_missing_gi_excel(mgi_file, plant=plant)
                        logger.info(f"    ✅ [Missing GI] DB 적재 및 베이스라인 갱신 완료: {n_mgi:,}개 품목 ({mgi_file.name})")
                    else:
                        logger.warning("    ⚠️ [Missing GI] 파일 수집 실패 또는 신규 파일 없음")
                except Exception as mgi_err:
                    logger.warning(f"    ⚠️ [Missing GI 경고] 수집 중 오류 (파이프라인 계속 진행): {mgi_err}")
            else:
                logger.info("    ℹ️ [Missing GI] 주간 정기 갱신일(일요일) 외 평일: 기존 학습된 베이스라인 마스터를 활용합니다.")

            elapsed = time.time() - s2_time
            logger.info(f"    ✅ [STEP 2 성공] 어제자 GR/GI 및 MissingGI 수집/적재 완료 ({elapsed:.1f}초)")
        except Exception as e2:
            logger.error(f"    ❌ [STEP 2 예외 발생]: {e2}\n{traceback.format_exc()}")
            pipeline_success = False
        finally:
            if crawler and crawler.driver:
                try:
                    crawler.driver.quit()
                except Exception:
                    pass

    time.sleep(2)

    # -------------------------------------------------------------------------
    # STEP 3: 온톨로지 회전일수(Turnover Days) 마스터 전면 재계산
    # -------------------------------------------------------------------------
    if dry_run:
        logger.info("\n>>> [STEP 3/5 스킵] --dry-run 설정: 온톨로지 마스터 재계산/DB 쓰기를 건너뜁니다.")
    else:
        logger.info("\n>>> [STEP 3/5] 누적 트랜잭션 매칭 및 온톨로지 회전일수 마스터 재계산 시작...")
        s3_time = time.time()
        try:
            onto_summary = run_full_ontology_update()
            elapsed = time.time() - s3_time
            total_parts = onto_summary.get("total_materials_in_ontology", 0) if isinstance(onto_summary, dict) else 0
            logger.info(f"    ✅ [STEP 3 성공] 온톨로지 갱신 완료: 총 {total_parts:,}개 품목 프로파일 최신화 ({elapsed:.1f}초)")
        except Exception as e3:
            logger.error(f"    ❌ [STEP 3 예외 발생]: {e3}\n{traceback.format_exc()}")
            pipeline_success = False

    time.sleep(1)

    # -------------------------------------------------------------------------
    # STEP 4: 재고 분석 및 실사 타깃 엑셀 생성 (Daily 40개 빈 / Weekly 50개 빈)
    # -------------------------------------------------------------------------
    logger.info(f"\n>>> [STEP 4/5] 최신 재고 vs 온톨로지 대조 [{resolved_mode}] 분석 및 실사 엑셀 생성 시작...")
    s4_time = time.time()
    verifier = None
    analysis = None
    try:
        verifier = StockVerifier()
        analysis = verifier.run_full_stock_analysis(plant=plant, mode=resolved_mode, dry_run=dry_run)
        audit_excel = analysis.get("audit_excel_path")
        target_count = (
            len(analysis.get("top_bins", [])) + 
            len(analysis.get("extended_bins", [])) + 
            len(analysis.get("random_bins", []))
        )
        elapsed = time.time() - s4_time
        logger.info(f"    ✅ [STEP 4 성공] [{resolved_mode}] 실사 분석 완료: 총 {target_count}개 타깃 빈 도출 ({elapsed:.1f}초)")
        logger.info(f"    - 생성된 실사 엑셀: {Path(audit_excel).name} ({Path(audit_excel).stat().st_size:,} bytes)")

        if dry_run:
            logger.info(
                f"    ✅ [DRY-RUN SUMMARY] Mode={resolved_mode} | Input WMS={analysis.get('source_file')} | "
                f"Suspect Rows={len(analysis.get('suspects_df', []))} | Target Bins={target_count} "
                f"(Top={len(analysis.get('top_bins', []))}, Ext={len(analysis.get('extended_bins', []))}, Rand={len(analysis.get('random_bins', []))}) "
                f"(DB write skipped)"
            )
        # STEP 4-B: 실사 지시 원장(Audit Assignment Ledger) 및 시점 피처 스냅샷 DB 적재
        if not dry_run:
          try:
            import pandas as pd
            from config import SCORING_VERSION, get_config_hash
            from db_manager import save_audit_ledger_and_features
            run_id = f"{today_str}_TEST_{resolved_mode.upper()}" if is_test else f"{today_str}_{resolved_mode.upper()}"
            run_info = {
                "run_id": run_id,
                "run_date": today.strftime("%Y-%m-%d"),
                "mode": resolved_mode,
                "scoring_version": analysis.get("scoring_version") or SCORING_VERSION,
                "wms_snapshot_file": analysis.get("source_file", ""),
                "config_hash": analysis.get("config_hash") or get_config_hash()
            }
            save_res = save_audit_ledger_and_features(
                run_info=run_info,
                top_bins=analysis.get("top_bins", []),
                extended_bins=analysis.get("extended_bins", []),
                random_bins=analysis.get("random_bins", []),
                suspects_df=analysis.get("suspects_df", pd.DataFrame())
            )
            logger.info(f"    ✅ [실사 지시 원장] DB 적재 완료: Run ID '{save_res['run_id']}', 지시 빈 {save_res['assignments_count']}개, 피처 스냅샷 {save_res['features_count']:,}행")
          except Exception as e_audit_save:
            logger.warning(f"    ⚠️ [실사 원장 저장 경고] DB 적재 중 오류 (메일 발송 파이프라인 계속 진행): {e_audit_save}")
    except Exception as e4:
        logger.error(f"    ❌ [STEP 4 예외 발생]: {e4}\n{traceback.format_exc()}")
        pipeline_success = False

    time.sleep(1)

    # -------------------------------------------------------------------------
    # STEP 5: Outlook 정기 알림 이메일 발송 (일요일 발송 제외 정책 적용)
    # -------------------------------------------------------------------------
    logger.info("\n>>> [STEP 5/5] Outlook 정기 실사 알림 메일 발송 단계...")
    s5_time = time.time()
    
    if skip_email:
        logger.info("    ⏸️ [STEP 5 생략] --skip-email 플래그가 지정되어 이메일 발송을 건너뜁니다.")
    elif weekday == 6 and mode.lower() == "auto" and os.getenv("STOCK_ACCURACY_FORCE_EMAIL", "false").lower() != "true":
        # Sunday Policy: Daily data analysis & DB stacking are fully active, but emails are excluded on Sundays.
        logger.info("    ⏸️ [STEP 5 생략] 일요일 이메일 발송 제외 정책(Sunday Policy) 적용:")
        logger.info("       - 데이터 수집, DB 누적, 온톨로지 재계산 및 실사 엑셀 생성은 정상 완료되었습니다.")
        logger.info("       - 현장 작업자 휴무 및 주말 메일 피로도 방지를 위해 일요일 정기 메일은 발송하지 않습니다.")

    else:
        try:
            mail_res = send_warehouse_notification(
                plant=plant,
                mode=resolved_mode,
                display_first=display_first,
                to_recipients=to_recipients,
                cc_recipients=cc_recipients,
                is_test=is_test
            )
            elapsed = time.time() - s5_time
            logger.info(f"    ✅ [STEP 5 성공] 이메일 발송 완료: {mail_res.get('subject')} ➔ {mail_res.get('to')} ({elapsed:.1f}초)")
        except Exception as e5:
            logger.error(f"    ❌ [STEP 5 예외 발생]: {e5}\n{traceback.format_exc()}")
            pipeline_success = False

    # -------------------------------------------------------------------------
    # STEP 5.5: Post-Dispatch Permanent Daily Snapshot Archive & D114 Shadow (#236-B / #237)
    # -------------------------------------------------------------------------
    if not dry_run:
        try:
            if str(CURRENT_DIR.parent) not in sys.path:
                sys.path.insert(0, str(CURRENT_DIR.parent))
            from daily_snapshot_archive import sync_daily_snapshots
            arch_res = sync_daily_snapshots(force_dates=[today.strftime("%Y-%m-%d")])
            wms_err = arch_res.get("wms", {}).get("errors", [])
            sap_err = arch_res.get("sap", {}).get("errors", [])
            logger.info(
                f"    ✅ [STEP 5.5A Daily Ledgers] WMS ingested={len(arch_res.get('wms', {}).get('ingested', []))} | "
                f"SAP MB52 ingested={len(arch_res.get('sap', {}).get('ingested', []))}"
            )
            if wms_err or sap_err:
                raise RuntimeError(f"Daily ledger errors: wms={wms_err}, sap={sap_err}")
        except Exception as e_arch:
            logger.error(f"    ⚠️ [STEP 5.5A 경고] 일별 영구 원장 적재 오류 (다음 실행 시 자동 재시도, 원본 보존): {e_arch}")
            try:
                handoff_dir = HANDOFF_ALERT_DIR
                if handoff_dir.exists():
                    alert_fp = handoff_dir / f"ALERT_DAILY_ARCHIVE_{today_str}.md"
                    alert_fp.write_text(f"# Daily Snapshot Archive Alert ({now_ts})\n\nError: `{e_arch}`\n", encoding="utf-8")
            except Exception:
                pass

        try:
            from d114_stage_flow_shadow import run_d114_shadow_update
            sel_bins = []
            if isinstance(analysis, dict):
                for grp in ("forced_weekly_bins", "top_bins", "extended_bins", "random_bins"):
                    for b_item in analysis.get(grp, []):
                        if isinstance(b_item, dict) and b_item.get("bin"):
                            sel_bins.append(b_item["bin"])
            d114_res = run_d114_shadow_update(
                run_date=today.strftime("%Y-%m-%d"),
                selected_bins_by_mode={resolved_mode: sel_bins} if sel_bins else None,
            )
            logger.info(
                f"    ✅ [STEP 5.5B D114 Shadow] files={d114_res['ingest']['newly_processed_files']} | "
                f"daily_suspects={d114_res['signals']['daily_suspect_bins']} | "
                f"weekly_suspects={d114_res['signals']['weekly_suspect_bins']}"
            )
        except Exception as e_d114:
            logger.warning(f"    ⚠️ [STEP 5.5B D114 Shadow 경고] 비차단 오류: {e_d114}")

    # -------------------------------------------------------------------------
    # STEP 6: Automated Storage Retention & Disk Pruning (7-day policy + DB Ledger Guard)
    # -------------------------------------------------------------------------
    if not dry_run:
      try:
        sys.path.insert(0, str(CURRENT_DIR.parent))
        from storage_retention import run_retention_pruning
        run_retention_pruning(retention_days=7)
      except Exception as e_ret:
        logger.warning(f"    ⚠️ Storage retention cleanup warning: {e_ret}")

    total_elapsed = time.time() - start_total_time
    logger.info("\n==========================================================================")
    if pipeline_success:
        logger.info(f" 🎉 [ALL COMPLETED] 모든 파이프라인 단계가 성공적으로 완료되었습니다! (총 소요시간: {total_elapsed:.1f}초)")
    else:
        logger.warning(f" ⚠️ [COMPLETED WITH WARNINGS] 일부 단계에서 오류가 발생했습니다. 로그 파일을 확인하세요. (총 소요시간: {total_elapsed:.1f}초)")
    logger.info("==========================================================================\n")

    return pipeline_success


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ACME Manufacturing Stock Accuracy Pipeline (Daily & Weekly)")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="Target Plant (default: P001)")
    parser.add_argument("--mode", default="auto", choices=["auto", "Daily", "Weekly"], help="Cycle count mode (default: auto -> Saturday=Weekly, Others=Daily)")
    parser.add_argument("--visible", action="store_true", help="Run browser in visible mode (default: headless background)")
    parser.add_argument("--headless", action="store_true", default=True, help="Run browser in headless mode (default: True)")
    parser.add_argument("--skip-crawl", action="store_true", help="Skip WMS scraping and reuse existing WMS stock list file")
    parser.add_argument("--skip-email", action="store_true", help="Skip email sending step")
    parser.add_argument("--display", action="store_true", help="Display Outlook draft window instead of sending directly")
    parser.add_argument("--to", type=str, nargs="+", default=None, help="Custom recipient email(s)")
    parser.add_argument("--cc", type=str, nargs="+", default=None, help="Custom CC recipient email(s)")
    parser.add_argument("--test", action="store_true", help="Test mode (marks DB run as TEST and routes email strictly to TEST_EMAIL_RECIPIENT)")
    parser.add_argument("--dry-run", action="store_true", help="Read-only dry run (no crawl, no DB write, no email)")
    args = parser.parse_args()

    is_headless = not args.visible

    success = run_pipeline(
        plant=args.plant,
        headless_browser=is_headless,
        mode=args.mode,
        skip_crawl=args.skip_crawl,
        skip_email=args.skip_email,
        display_first=args.display,
        to_recipients=args.to,
        cc_recipients=args.cc,
        is_test=args.test,
        dry_run=args.dry_run
    )
    sys.exit(0 if success else 1)
