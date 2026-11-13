"""
Historical Backfill Crawler (01_Inventory_Team / 00_StockAccuracy)
WMS GR List 및 GI List의 과거 데이터를 1주일 단위 분할 청크로 자동 다운로드하고,
SQLite DB 적재 및 온톨로지 마스터를 갱신합니다.
- 중복 방지 (기존 다운로드 파일 자동 감지 및 Skip)
- 작업 중단 시 재개(Resumable) 지원
"""

import os
import sys
import time
import glob
import datetime
import argparse
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
from typing import List, Tuple, Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from dotenv import load_dotenv

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

from db_manager import (
    init_database,
    ingest_gr_excel,
    ingest_gi_excel,
    is_file_ingested,
    GR_STACK_DIR,
    GI_STACK_DIR
)
from turnover_ontology_engine import run_full_ontology_update

CHROMEDRIVER_PATH = Path.home() / ".cache" / "selenium" / "chromedriver" / "win64" / "155.0.8059.84" / "chromedriver.exe"
WMS_BASE_URL = _get_ops_env("WMS_BASE_URL", "https://wms.example.com/WMS_WEB").rstrip("/")
LOGIN_URL = f"{WMS_BASE_URL}/Login"
GR_URL = f"{WMS_BASE_URL}/GRList"
GI_URL = f"{WMS_BASE_URL}/GiList"


def generate_weekly_intervals(weeks: int = 52, end_date: Optional[datetime.date] = None) -> List[Tuple[datetime.date, datetime.date]]:
    """과거 N주간의 (시작일, 종료일) 1주일 구간 리스트 생성 (과거부터 최신순 정렬)"""
    end = end_date or datetime.date.today()
    intervals = []
    
    current_end = end
    for _ in range(weeks):
        current_start = current_end - datetime.timedelta(days=6)
        intervals.append((current_start, current_end))
        current_end = current_start - datetime.timedelta(days=1)
        
    intervals.reverse()
    return intervals


class WMSChunkCrawler:
    def __init__(self, headless: bool = False, timeout: int = 60):
        self.headless = headless
        self.timeout = timeout
        self.driver: Optional[webdriver.Chrome] = None
        self.username = _get_ops_env("WMS_USERNAME", "wms_user")
        self.password = os.getenv("WMS_PASSWORD", "")
        self.temp_dir = Path(__file__).resolve().parent / ".temp_downloads"
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Background process priority optimization
        try:
            import psutil
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        except Exception:
            pass

    def _setup_browser(self):
        options = Options()
        options.page_load_strategy = "eager"
        options.add_argument("--ignore-certificate-errors")
        options.add_argument("--start-maximized")
        if self.headless:
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

        prefs = {
            "download.default_directory": str(self.temp_dir.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "safebrowsing.disable_download_protection": True
        }
        options.add_experimental_option("prefs", prefs)

        service = Service(str(CHROMEDRIVER_PATH)) if CHROMEDRIVER_PATH.exists() else None
        self.driver = webdriver.Chrome(service=service, options=options)
        
        try:
            self.driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(self.temp_dir.resolve())
            })
        except Exception:
            pass

    def _login(self):
        print("[Crawler] WMS 로그인 접속 중...")
        self.driver.get(LOGIN_URL)
        time.sleep(5)
        
        login_inputs = self.driver.find_elements(By.ID, "potal_id")
        if login_inputs and login_inputs[0].is_displayed():
            print(f"[Crawler] 로그인 입력 (User: {self.username})...")
            login_inputs[0].clear()
            login_inputs[0].send_keys(self.username)
            pwd_el = self.driver.find_element(By.ID, "pwd")
            pwd_el.clear()
            pwd_el.send_keys(self.password)
            self.driver.find_element(By.ID, "btnLogin").click()
            time.sleep(5)
        else:
            print("[Crawler] SSO 자동 로그인 완료.")

    def _set_date_and_download(self, page_type: str, start_dt: datetime.date, end_dt: datetime.date) -> Optional[Path]:
        """지정된 기간으로 그리드 조회 후 엑셀 다운로드"""
        url = GR_URL if page_type == "GR" else GI_URL
        target_dir = GR_STACK_DIR if page_type == "GR" else GI_STACK_DIR
        
        start_str_wms = start_dt.strftime("%m/%d/%Y")
        end_str_wms = end_dt.strftime("%m/%d/%Y")
        file_prefix = f"{page_type}_List_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"
        target_file = target_dir / f"{file_prefix}.xlsx"

        # 이미 존재하는 경우 Skip & DB 적재 확인 (Resumable)
        if target_file.exists() and target_file.stat().st_size > 5000:
            if not is_file_ingested(page_type, target_file.name):
                print(f"  [DB 적재] 기존 다운로드 파일 DB 동기화: {target_file.name}")
                if page_type == "GR":
                    ingest_gr_excel(target_file)
                else:
                    ingest_gi_excel(target_file)
            else:
                print(f"  [Skip] 이미 다운로드 및 DB 적재 완료됨: {target_file.name}")
            return target_file

        print(f"  [{page_type}] {start_dt} ~ {end_dt} ({start_str_wms} ~ {end_str_wms}) 조회 시작...")
        self.driver.get(url)
        time.sleep(4)

        # 세션 만료 검사
        if "Login" in self.driver.current_url or self.driver.find_elements(By.ID, "btnLogin"):
            print("  [Auth] 세션 만료 감지, 재로그인 수행 중...")
            self._login()
            self.driver.get(url)
            time.sleep(4)

        # 1. 날짜 입력 (JavaScript Event dispatch)
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
        self.driver.execute_script(js_code)
        time.sleep(1)

        # 2. Search 버튼 클릭
        before_files = set(glob.glob(str(self.temp_dir / "*.xlsx")) + glob.glob(str(self.temp_dir / "*.xls")))
        search_btn = self.driver.find_element(By.XPATH, "//button[contains(@class, 'rz-primary') and contains(., 'Search')]")
        self.driver.execute_script("arguments[0].click();", search_btn)

        # 3. 로딩 대기
        for w in range(45):
            time.sleep(1)
            loading = self.driver.find_elements(By.XPATH, "//*[contains(@class, 'loading') or @id='overlay' or contains(@class, 'rz-dialog')]")
            if not [l for l in loading if l.is_displayed()] and w >= 2:
                break
        time.sleep(2)

        # 4. Excel 다운로드 버튼 클릭
        excel_btn = self.driver.find_element(By.XPATH, "//button[contains(@class, 'rz-success') and contains(., 'Excel')]")
        self.driver.execute_script("arguments[0].click();", excel_btn)

        # 알럿 확인
        time.sleep(2)
        try:
            alert = self.driver.switch_to.alert
            alert.accept()
        except Exception:
            pass

        # 5. 다운로드 완료 감지
        downloaded = None
        for _ in range(60):
            time.sleep(1)
            cr = glob.glob(str(self.temp_dir / "*.crdownload"))
            if cr:
                continue
            after_files = set(glob.glob(str(self.temp_dir / "*.xlsx")) + glob.glob(str(self.temp_dir / "*.xls")))
            diff = after_files - before_files
            if diff:
                downloaded = Path(list(diff)[0])
                break

        if downloaded and downloaded.exists():
            time.sleep(1)
            downloaded.rename(target_file)
            print(f"  🎉 [{page_type}] 저장 성공: {target_file.name} ({target_file.stat().st_size:,} bytes)")
            
            # DB 즉시 적재
            if page_type == "GR":
                n = ingest_gr_excel(target_file)
            else:
                n = ingest_gi_excel(target_file)
            print(f"     ➔ DB 적재 완료: {n:,}건")
            return target_file
        else:
            print(f"  ⚠️ [{page_type}] 다운로드 감지 타임아웃 ({start_dt} ~ {end_dt})")
            return None

    def run_backfill(self, weeks: int = 52):
        """과거 N주간의 GR과 GI 데이터를 순회하며 다운로드 및 DB 적재"""
        init_database()
        intervals = generate_weekly_intervals(weeks=weeks)
        print(f"\n=======================================================")
        print(f" 과거 {weeks}주 WMS 이력(GR/GI) 백필 크롤링 시작 (총 {len(intervals)}구간)")
        print(f" 기간: {intervals[0][0]} ~ {intervals[-1][1]}")
        print(f"=======================================================\n")

        self._setup_browser()
        try:
            self._login()

            for idx, (s_dt, e_dt) in enumerate(intervals, 1):
                print(f"\n[진행률: {idx}/{len(intervals)} ({(idx/len(intervals))*100:.1f}%)] 구간: {s_dt} ~ {e_dt}")
                try:
                    # 1. GR 수집
                    self._set_date_and_download("GR", s_dt, e_dt)
                    time.sleep(2)
                    
                    # 2. GI 수집
                    self._set_date_and_download("GI", s_dt, e_dt)
                    time.sleep(2)
                except Exception as step_err:
                    print(f"  ❌ [{s_dt} ~ {e_dt}] 수집 중 예외 발생: {step_err}")
                    time.sleep(3)

            print("\n=======================================================")
            print(" 🎉 모든 주간 데이터 수집 및 DB 적재 완료! 온톨로지 엔진을 구동합니다.")
            print("=======================================================\n")
            run_full_ontology_update()

        finally:
            # 임시 폴더 잔여 파일 청소
            if self.temp_dir.exists():
                for f in self.temp_dir.glob("*.*"):
                    try:
                        f.unlink()
                    except Exception:
                        pass
                try:
                    self.temp_dir.rmdir()
                except Exception:
                    pass

            if self.driver:
                self.driver.quit()
                print("[Crawler] 브라우저 정상 종료.")


def run_daily_incremental():
    """매일 정기 실행용: 어제 하루치의 GR/GI 데이터를 수집하여 적재"""
    init_database()
    yesterday = datetime.date.today() - datetime.timedelta(days=1)
    crawler = WMSChunkCrawler(headless=False)
    crawler._setup_browser()
    try:
        crawler._login()
        print(f"\n[일일 증분 수집] 대상일: {yesterday}")
        crawler._set_date_and_download("GR", yesterday, yesterday)
        time.sleep(2)
        crawler._set_date_and_download("GI", yesterday, yesterday)
        time.sleep(2)
        run_full_ontology_update()
    finally:
        if crawler.driver:
            crawler.driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WMS GR/GI 과거 데이터 주간 단위 백필 크롤러")
    parser.add_argument("--weeks", type=int, default=4, help="수집할 과거 주 수 (기본: 최근 4주, 전체: 52)")
    parser.add_argument("--daily", action="store_true", help="어제 하루치 증분 수집 모드")
    parser.add_argument("--headless", action="store_true", help="헤드리스 백그라운드 모드")
    args = parser.parse_args()

    if args.daily:
        run_daily_incremental()
    else:
        crawler = WMSChunkCrawler(headless=args.headless)
        crawler.run_backfill(weeks=args.weeks)
