"""
Missing GI Historical Weekly Backfill & Packaging Unit Learning Crawler
(01_Inventory_Team / 00_StockAccuracy)

WMS Missing GI 메뉴에서 과거 N주간(기본 8주)의 1주일 단위 스냅샷을 순차 수집·적재하고,
자재별 상시 출고/포장 단위 편차(Baseline Offset)를 통계적으로 학습합니다.

Guardrails 준수:
- 로컬 단일 브라우저 세션(Chrome 1개) 유지
- 요청 간 1~2초 Pacing Delay 준수
"""

import os
import sys
import time
import glob
import datetime
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
from typing import List, Dict, Any, Optional

import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import MONITORED_PLANTS
from db_manager import ingest_missing_gi_excel, update_missing_gi_baseline, get_connection

BASE_DIR = Path(__file__).resolve().parent
MISSING_GI_STACK_DIR = BASE_DIR / "missing_gi_daily_stack"
MISSING_GI_STACK_DIR.mkdir(parents=True, exist_ok=True)
CHROMEDRIVER_PATH = Path.home() / ".cache" / "selenium" / "chromedriver" / "win64" / "155.0.8059.84" / "chromedriver.exe"

from config import WMS_URL as LOGIN_URL, MISSING_GI_URL


def get_driver(save_path: Path, headless: bool = True) -> webdriver.Chrome:
    """단일 Chrome 브라우저 인스턴스 초기화"""
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument("--ignore-certificate-errors")
    options.add_argument("--start-maximized")
    options.add_argument("--window-size=1920,1080")
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")

    prefs = {
        "download.default_directory": str(save_path.resolve()),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "safebrowsing.enabled": True,
        "safebrowsing.disable_download_protection": True
    }
    options.add_experimental_option("prefs", prefs)

    service = Service(str(CHROMEDRIVER_PATH)) if CHROMEDRIVER_PATH.exists() else None
    driver = webdriver.Chrome(service=service, options=options)

    try:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(save_path.resolve())
        })
    except Exception:
        pass

    return driver


from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")


def perform_login(driver: webdriver.Chrome, username: Optional[str] = None, password: Optional[str] = None):
    """WMS SSO 자동 인증 또는 폼 로그인"""
    user = username or _get_ops_env("WMS_USERNAME", "wms_user")
    pw = password or os.getenv("WMS_PASSWORD", "")

    driver.get(LOGIN_URL)
    time.sleep(4)
    login_inputs = driver.find_elements(By.ID, "potal_id")
    if login_inputs and login_inputs[0].is_displayed():
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] WMS 로그인 시도: {user}")
        login_inputs[0].clear()
        login_inputs[0].send_keys(user)
        pwd_el = driver.find_element(By.ID, "pwd")
        pwd_el.clear()
        pwd_el.send_keys(pw)
        driver.find_element(By.ID, "btnLogin").click()
        time.sleep(5)
    else:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] SSO 자동 인증 완료 (URL: {driver.current_url})")


def download_single_missing_gi_week(
    driver: webdriver.Chrome,
    start_dt: datetime.date,
    end_dt: datetime.date,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    save_path: Optional[Path] = None,
    timeout: int = 40
) -> Optional[Path]:
    """단일 1주일 기간의 Missing GI 리포트 다운로드"""
    save_dir = save_path or MISSING_GI_STACK_DIR
    save_dir.mkdir(parents=True, exist_ok=True)
    file_tag = f"MissingGI_{plant}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
    target_file = save_dir / f"{file_tag}.xlsx"

    # 캐시 확인 (기존 파일이 있으면 스킵)
    if target_file.exists() and target_file.stat().st_size > 5000:
        print(f"   ⏩ [Cache Hit] 이미 파일이 존재합니다: {target_file.name} ({target_file.stat().st_size:,} bytes)")
        return target_file

    start_str_wms = start_dt.strftime("%m/%d/%Y")
    end_str_wms = end_dt.strftime("%m/%d/%Y")
    print(f"   📥 [수집 시작] {plant} 기간: {start_dt} ~ {end_dt} ({start_str_wms} ~ {end_str_wms})...")

    # 1. MissingGI 페이지 이동
    missing_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'MissingGI')]")
    if missing_links:
        driver.execute_script("arguments[0].click();", missing_links[0])
    else:
        driver.get(MISSING_GI_URL)
    time.sleep(3)

    if "MissingGI" not in driver.current_url:
        missing_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'MissingGI')]")
        if missing_links:
            driver.execute_script("arguments[0].click();", missing_links[0])
            time.sleep(3)

    # 2. 날짜 설정 (JavaScript 이벤트 디스패치)
    js_code = f"""
        let inputs = document.querySelectorAll("input.rz-inputtext");
        if (inputs.length >= 2) {{
            inputs[0].value = '{start_str_wms}';
            inputs[0].dispatchEvent(new Event('input', {{ bubbles: true }}));
            inputs[0].dispatchEvent(new Event('change', {{ bubbles: true }}));
            
            inputs[1].value = '{end_str_wms}';
            inputs[1].dispatchEvent(new Event('input', {{ bubbles: true }}));
            inputs[1].dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
    """
    driver.execute_script(js_code)
    time.sleep(1.5)

    # 3. Search 버튼 클릭
    search_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Search')]")
    if not search_btns:
        print("   ❌ Search 버튼을 찾을 수 없습니다.")
        return None
    driver.execute_script("arguments[0].click();", search_btns[0])

    # 4. 그리드 로딩 대기
    for w in range(25):
        time.sleep(1)
        loading = driver.find_elements(By.XPATH, "//*[contains(@class, 'loading') or @id='overlay' or contains(@class, 'rz-dialog')]")
        visible_loading = [l for l in loading if l.is_displayed()]
        if not visible_loading and w >= 2:
            break
    time.sleep(2)

    # 5. 다운로드 버튼 클릭
    downloads_dir = Path.home() / "Downloads"
    for old_f in list(save_dir.glob("MissingGI_20*.xlsx")) + (list(downloads_dir.glob("MissingGI_20*.xlsx")) if downloads_dir.exists() else []):
        if not old_f.name.startswith(f"MissingGI_{plant}_"):
            try:
                old_f.unlink()
            except Exception:
                pass

    click_time = time.time()
    download_btn = driver.find_element(By.XPATH, "//button[contains(@class, 'rz-success') and (contains(., 'Download') or contains(., 'Excel'))]")
    driver.execute_script("arguments[0].click();", download_btn)
    time.sleep(2)

    # 브라우저 알럿 수락
    try:
        alert = driver.switch_to.alert
        alert.accept()
    except Exception:
        pass

    # 6. 파일 다운로드 감지 (클릭 시각 이후 생성된 파일 탐지)
    downloaded = None
    for _ in range(timeout):
        time.sleep(1)
        all_candidates = []
        for p in [save_dir, downloads_dir, BASE_DIR / ".temp_downloads"]:
            if p and p.exists():
                for f in p.glob("*.xlsx"):
                    if not f.name.endswith(".crdownload") and f.stat().st_mtime >= click_time - 2:
                        all_candidates.append(f)
        if all_candidates:
            downloaded = max(all_candidates, key=lambda x: x.stat().st_mtime)
            break

    if downloaded and downloaded.exists():
        time.sleep(1)
        import shutil
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        ts_file = save_dir / f"{file_tag}_{ts}.xlsx"
        shutil.copyfile(str(downloaded), str(ts_file))
        shutil.move(str(downloaded), str(target_file))
        print(f"   🎉 [저장 완료] {target_file.name} (TS: {ts_file.name} | {target_file.stat().st_size:,} bytes)")
        time.sleep(1.5)  # Pacing delay
        return target_file
    else:
        print(f"   ⚠️ 다운로드 타임아웃 ({start_dt} ~ {end_dt})")
        return None


def run_missing_gi_weekly_backfill(
    weeks: int = 52,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    headless: bool = True,
    clean_old: bool = True
) -> Dict[str, Any]:
    """
    과거 N주간의 Missing GI 주간 단위 데이터를 백필 수집하고
    단위 편차(Packaging Offset) 베이스라인 모델을 학습합니다.
    """
    today = datetime.date.today()
    print("=" * 70)
    print(f" 📥 WMS Missing GI 과거 {weeks}주간 분할 백필 & 단위 학습 시작 (플랜트: {plant})")
    print(f"    - 실행 일시: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"    - 저장 폴더: {MISSING_GI_STACK_DIR.resolve()}")
    print("=" * 70)

    # 이전 비표준/중복 스냅샷 데이터 정리 (52주 정규 일~토 주간 데이터 전면 재적재)
    if clean_old:
        print("🧹 [정리] 이전 비표준/중복 과거 스냅샷 데이터 정리 중...")
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("DELETE FROM MISSING_GI_SNAPSHOT WHERE plant = ?", (plant,))
        cur.execute("DELETE FROM MISSING_GI_BASELINE")
        conn.commit()
        conn.close()
        print("   ✅ 이전 중복 스냅샷 및 베이스라인 정리 완료 (신규 52주 비중첩 데이터로 전면 재적재)")

    # 1주일 단위 구간 생성: 가장 최근 완결된 주간(일요일~토요일) 기준 (예: 2026-10-18 ~ 2026-10-24)
    days_since_saturday = (today.weekday() + 2) % 7
    latest_saturday = today if days_since_saturday == 0 else (today - datetime.timedelta(days=days_since_saturday))
    latest_sunday = latest_saturday - datetime.timedelta(days=6)

    intervals = []
    for i in range(weeks):
        s_d = latest_sunday - datetime.timedelta(days=i * 7)
        e_d = latest_saturday - datetime.timedelta(days=i * 7)
        intervals.append((s_d, e_d))

    driver = None
    collected_files: List[Path] = []
    ingested_counts: List[int] = []

    try:
        driver = get_driver(MISSING_GI_STACK_DIR, headless=headless)
        perform_login(driver)

        for idx, (s_dt, e_dt) in enumerate(intervals, 1):
            print(f"\n[{idx}/{weeks} 주차] {s_dt} ~ {e_dt} (7일간)")
            try:
                f_path = download_single_missing_gi_week(
                    driver=driver,
                    start_dt=s_dt,
                    end_dt=e_dt,
                    plant=plant,
                    save_path=MISSING_GI_STACK_DIR
                )
                if f_path and f_path.exists():
                    collected_files.append(f_path)
                    n = ingest_missing_gi_excel(f_path, plant=plant)
                    ingested_counts.append(n)
                    print(f"   ➔ DB 적재 완료: {n:,}개 품목 레코드")
                else:
                    print(f"   ❌ 해당 주차 수집 실패")
            except Exception as week_err:
                print(f"   ⚠️ [{idx}주차 처리 중 오류]: {week_err}")

            time.sleep(2)  # Guardrail Pacing Delay

    except Exception as e:
        print(f"❌ 백필 도중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            driver.quit()
            print("\n[Browser] 브라우저 세션 정상 종료.")

    # -------------------------------------------------------------------------
    # Baseline Model Training & Summary
    # -------------------------------------------------------------------------
    print("\n" + "=" * 70)
    print(" 🧠 품목별 상시 포장/출고 단위 편차(Packaging Offset Baseline) 통계 학습 시작...")
    print("=" * 70)
    n_trained = update_missing_gi_baseline()
    print(f"✅ 총 {n_trained:,}개 품목의 과거 주간 편차 베이스라인 학습 완료!")

    # 도출된 단위 편차 품목 검증
    conn = get_connection()
    offset_items = pd.read_sql_query("""
        SELECT mat_clean, mat_no, mat_desc, sample_weeks, avg_diff_box, median_diff_box, std_diff_box, is_unit_offset_item
        FROM MISSING_GI_BASELINE
        WHERE is_unit_offset_item = 1
        ORDER BY sample_weeks DESC, ABS(median_diff_box) DESC
    """, conn)
    conn.close()

    print(f"\n🔍 [학습 결과] 상시 포장/출고 단위 편차 품목(Unit Offset Items): 총 {len(offset_items):,}개 발견")
    if not offset_items.empty:
        print("-" * 70)
        print(offset_items.head(10)[["mat_clean", "sample_weeks", "median_diff_box", "std_diff_box", "mat_desc"]].to_string(index=False))
        print("-" * 70)
        print("💡 위 품목들은 매주 고정된 박스 편차가 발생하므로, 단순 차이가 아닌 '기준선 초과 이상치'만 Ghost Stock으로 엄격 관리됩니다.")

    return {
        "weeks_target": weeks,
        "files_collected": len(collected_files),
        "total_records_ingested": sum(ingested_counts),
        "materials_profiled": n_trained,
        "unit_offset_items_count": len(offset_items)
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Missing GI Weekly Backfill & Unit Discrepancy Profiler")
    parser.add_argument("--weeks", type=int, default=52, help="수집할 과거 주간 수 (기본: 52주, 1년치)")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="대상 플랜트 (기본: P001)")
    parser.add_argument("--visible", action="store_true", help="브라우저 화면 표시 모드")
    parser.add_argument("--keep-old", action="store_true", help="기존 스냅샷 데이터 유지 (기본: 중복 방지 위해 정리 후 적재)")
    args = parser.parse_args()

    run_missing_gi_weekly_backfill(
        weeks=args.weeks,
        plant=args.plant,
        headless=not args.visible,
        clean_old=not args.keep_old
    )
