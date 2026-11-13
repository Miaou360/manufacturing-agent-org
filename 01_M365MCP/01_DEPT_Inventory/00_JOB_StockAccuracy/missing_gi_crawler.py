"""
WMS Missing GI Crawler (01_Inventory_Team / 00_StockAccuracy)
ACME Manufacturing WMS 웹사이트의 MissingGI 페이지(https://wms.example.com/WMS_WEB/MissingGI)에
접속하여 지정된 기간(기본값: 최근 7일 롤링 윈도우)의 PROD.QTY(BOM 소모) vs GI QTY(라인 출고)
차이 및 DIFF(BOX) 데이터를 조회하고 엑셀 파일로 자동 다운로드합니다.
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
from typing import Optional

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from dotenv import load_dotenv

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

BASE_DIR = Path(__file__).resolve().parent
MISSING_GI_STACK_DIR = BASE_DIR / "missing_gi_daily_stack"
MISSING_GI_STACK_DIR.mkdir(parents=True, exist_ok=True)

CHROMEDRIVER_PATH = Path.home() / ".cache" / "selenium" / "chromedriver" / "win64" / "155.0.8059.84" / "chromedriver.exe"
WMS_BASE_URL = _get_ops_env("WMS_BASE_URL", "https://wms.example.com/WMS_WEB").rstrip("/")
LOGIN_URL = f"{WMS_BASE_URL}/Login"
MISSING_GI_URL = f"{WMS_BASE_URL}/MissingGI"


def download_wms_missing_gi(
    start_date: Optional[datetime.date] = None,
    end_date: Optional[datetime.date] = None,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    driver: Optional[webdriver.Chrome] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    download_dir: Optional[Path] = None,
    headless: bool = True,
    timeout: int = 45
) -> Optional[Path]:
    """
    WMS MissingGI 페이지에서 최근 7일간(또는 지정 기간)의 소모 대비 출고 차이 엑셀을 자동 수집합니다.

    Args:
        start_date: 조회 시작일 (기본: 오늘 - 7일)
        end_date: 조회 종료일 (기본: 오늘)
        plant: 대상 플랜트 (기본: P001)
        driver: 기존 활성화된 Selenium WebDriver 객체 (재사용 시 로그인 생략)
        username: WMS 계정 ID
        password: WMS 계정 비밀번호
        download_dir: 파일 저장 폴더 (기본: missing_gi_daily_stack)
        headless: 브라우저 백그라운드 구동 여부 (기본: True)
        timeout: 다운로드 대기 타임아웃 (초)

    Returns:
        저장된 엑셀 파일 Path 객체, 실패 시 None
    """
    today = datetime.date.today()
    start_dt = start_date or (today - datetime.timedelta(days=7))
    end_dt = end_date or today

    start_str_wms = start_dt.strftime("%m/%d/%Y")
    end_str_wms = end_dt.strftime("%m/%d/%Y")
    file_tag = f"MissingGI_{plant}_{start_dt.strftime('%Y%m%d')}_{end_dt.strftime('%Y%m%d')}"

    save_path = download_dir or MISSING_GI_STACK_DIR
    save_path.mkdir(parents=True, exist_ok=True)
    target_file = save_path / f"{file_tag}.xlsx"

    # 이미 당일자 동일 기간 파일이 유효하게 존재하는 경우 재사용
    if target_file.exists() and target_file.stat().st_size > 5000:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⏩ [Cache] 이미 최신 MissingGI 파일이 존재합니다: {target_file.name}")
        return target_file

    user = username or _get_ops_env("WMS_USERNAME", "wms_user")
    pw = password or os.getenv("WMS_PASSWORD", "")

    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] WMS Missing GI 다운로드 시작...")
    print(f"   - 대상 플랜트: {plant}")
    print(f"   - 조회 기간: {start_dt} ~ {end_dt} ({start_str_wms} ~ {end_str_wms}, 롤링 7일)")
    print(f"   - 저장 위치: {target_file.resolve()}")

    own_driver = False
    if driver is None:
        own_driver = True
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
        try:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(save_path.resolve())
            })
        except Exception:
            pass

        # 1. 로그인 (기존 드라이버가 로그인 안 되어 있거나 자체 생성된 경우)
        if own_driver or "Login" in driver.current_url or driver.find_elements(By.ID, "potal_id"):
            driver.get(LOGIN_URL)
            time.sleep(4)
            login_inputs = driver.find_elements(By.ID, "potal_id")
            if login_inputs and login_inputs[0].is_displayed():
                login_inputs[0].clear()
                login_inputs[0].send_keys(user)
                pwd_el = driver.find_element(By.ID, "pwd")
                pwd_el.clear()
                pwd_el.send_keys(pw)
                driver.find_element(By.ID, "btnLogin").click()
                time.sleep(5)

        # 2. MissingGI 페이지 이동 (Blazor SPA 사이드바 링크 또는 URL)
        missing_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'MissingGI')]")
        if missing_links:
            driver.execute_script("arguments[0].click();", missing_links[0])
        else:
            driver.get(MISSING_GI_URL)
        time.sleep(5)

        if "MissingGI" not in driver.current_url:
            missing_links = driver.find_elements(By.XPATH, "//a[contains(@href, 'MissingGI')]")
            if missing_links:
                driver.execute_script("arguments[0].click();", missing_links[0])
                time.sleep(4)

        # 3. 조회 일자 입력 (JavaScript DOM 이벤트 트리거)
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

        # 4. Search 버튼 클릭
        search_btns = driver.find_elements(By.XPATH, "//button[contains(., 'Search')]")
        if not search_btns:
            raise RuntimeError("MissingGI Search 버튼을 찾을 수 없습니다.")
        driver.execute_script("arguments[0].click();", search_btns[0])

        # 5. 그리드 로딩 대기
        for w in range(20):
            time.sleep(1)
            loading = driver.find_elements(By.XPATH, "//*[contains(@class, 'loading') or @id='overlay' or contains(@class, 'rz-dialog')]")
            visible_loading = [l for l in loading if l.is_displayed()]
            if not visible_loading and w >= 2:
                break
        time.sleep(2)

        # 6. 다운로드 버튼 클릭
        candidate_dirs = [save_path, BASE_DIR / ".temp_downloads", Path.home() / "Downloads"]
        before_files = set()
        for cd in candidate_dirs:
            if cd.exists():
                before_files.update(glob.glob(str(cd / "*")))

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

        # 7. 다운로드 파일 감지
        downloaded = None
        for sec in range(timeout):
            time.sleep(1)
            current_files = set()
            for cd in candidate_dirs:
                if cd.exists():
                    current_files.update(glob.glob(str(cd / "*")))

            diff = current_files - before_files
            finished = [
                f for f in diff 
                if not f.endswith(".crdownload") and (f.endswith(".xlsx") or f.endswith(".xls"))
            ]
            if finished:
                downloaded = Path(max(finished, key=os.path.getmtime))
                break

        if downloaded and downloaded.exists():
            time.sleep(1)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            ts_file = save_path / f"{file_tag}_{ts}.xlsx"
            import shutil
            # If downloaded into another directory, copy to ts_file and remove original
            shutil.copyfile(str(downloaded), str(ts_file))
            shutil.copyfile(str(ts_file), str(target_file))
            if str(downloaded) != str(ts_file) and str(downloaded) != str(target_file):
                try:
                    downloaded.unlink()
                except Exception:
                    pass
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 🎉 [성공] MissingGI 파일 다운로드 완료: {target_file.name} (TS: {ts_file.name} | {target_file.stat().st_size:,} bytes)")
            return target_file
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⚠️ MissingGI 파일 다운로드 타임아웃.")
            return None

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ❌ MissingGI 수집 오류: {e}")
        import traceback
        traceback.print_exc()
        return None
    finally:
        if own_driver and driver:
            driver.quit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WMS Missing GI Crawler")
    parser.add_argument("--days", type=int, default=7, help="조회 롤링 일수 (기본: 7일)")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="대상 플랜트 (기본: P001)")
    parser.add_argument("--no-headless", action="store_true", help="브라우저 화면 표시")
    args = parser.parse_args()

    s_dt = datetime.date.today() - datetime.timedelta(days=args.days)
    e_dt = datetime.date.today()
    res = download_wms_missing_gi(start_date=s_dt, end_date=e_dt, plant=args.plant, headless=not args.no_headless)
    if res:
        print(f"Saved: {res}")
    else:
        print("Failed.")
