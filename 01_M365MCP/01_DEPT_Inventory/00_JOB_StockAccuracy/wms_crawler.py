"""
WMS Stock List Crawler (01_Inventory_Team / 00_StockAccuracy)
ACME Manufacturing WMS 웹사이트에 접속하여 최신 Stock List 엑셀 파일을 자동 다운로드하여 wms_daily_stack에 저장합니다.
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
from typing import Optional, Tuple

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import (
    StaleElementReferenceException,
    ElementClickInterceptedException,
    TimeoutException
)
from dotenv import load_dotenv

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Load .env from project root if available
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    load_dotenv(env_file)

from config import WMS_URL, WMS_DOWNLOAD_DIR

CHROMEDRIVER_PATH = Path.home() / ".cache" / "selenium" / "chromedriver" / "win64" / "155.0.8059.84" / "chromedriver.exe"


def _retry_click(driver: webdriver.Chrome, by: By, value: str, timeout: int = 30) -> None:
    """StaleElement 및 렌더링 지연에 안전한 재시도 클릭 함수"""
    end_time = time.time() + timeout
    last_err = None
    while time.time() < end_time:
        try:
            el = WebDriverWait(driver, 4).until(EC.presence_of_element_located((by, value)))
            # Scroll into view
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            time.sleep(0.3)
            el.click()
            return
        except (StaleElementReferenceException, ElementClickInterceptedException) as e:
            last_err = e
            time.sleep(1)
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise TimeoutException(f"요소 클릭 타임아웃 ({by}={value}): {last_err}")



def _quarantine_stale_crdownloads(
    save_path: Path,
    max_age_seconds: float = 3600.0,
    now_ts: Optional[float] = None
) -> list:
    """Moves stale .crdownload files older than max_age_seconds (default 1 hour) into scratch/ instead of deleting."""
    now = now_ts if now_ts is not None else time.time()
    moved = []
    if not save_path or not save_path.exists():
        return moved
    scratch_dir = save_path.parent / "scratch"
    for f_str in glob.glob(str(save_path / "*.crdownload")):
        try:
            f_path = Path(f_str)
            mtime = os.path.getmtime(f_str)
            if (now - mtime) > max_age_seconds:
                scratch_dir.mkdir(parents=True, exist_ok=True)
                ts_suffix = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                dest = scratch_dir / f"{f_path.stem}_{ts_suffix}{f_path.suffix}"
                import shutil
                shutil.move(str(f_path), str(dest))
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 🧹 고아 .crdownload 격리 이동 (>1h): {f_path.name} -> {dest}")
                moved.append(dest)
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⚠️ .crdownload 격리 중 경고 ({f_str}): {e}")
    return moved


def _get_active_crdownloads(
    save_path: Path,
    click_start_time: float,
    downloads_dir: Optional[Path] = None
) -> list:
    """Returns only .crdownload files created/modified at or after click_start_time (with 2s clock tolerance)."""
    candidates = glob.glob(str(save_path / "*.crdownload"))
    if downloads_dir and downloads_dir.exists():
        candidates.extend(glob.glob(str(downloads_dir / "*.crdownload")))
    active = []
    for f_str in candidates:
        try:
            if os.path.getmtime(f_str) >= (click_start_time - 2.0):
                active.append(f_str)
        except OSError:
            pass
    return active


def _detect_completed_download(
    save_path: Path,
    before_mtimes: dict,
    downloads_dir: Path = None,
    before_dl_mtimes: dict = None
):
    """Detects newly created OR in-place overwritten Excel files in save_path or downloads_dir."""
    after_files = glob.glob(str(save_path / "*.xlsx")) + glob.glob(str(save_path / "*.xls"))
    finished = [
        f for f in after_files
        if not f.endswith(".crdownload")
        and (f not in before_mtimes or os.path.getmtime(f) > before_mtimes[f] + 0.5)
    ]
    if finished:
        return Path(max(finished, key=os.path.getmtime)), False

    if downloads_dir and downloads_dir.exists() and before_dl_mtimes is not None:
        after_dl = glob.glob(str(downloads_dir / "*.xlsx")) + glob.glob(str(downloads_dir / "*.xls"))
        finished_dl = [
            f for f in after_dl
            if not f.endswith(".crdownload")
            and "Stock" in Path(f).name
            and (f not in before_dl_mtimes or os.path.getmtime(f) > before_dl_mtimes[f] + 0.5)
        ]
        if finished_dl:
            return Path(max(finished_dl, key=os.path.getmtime)), True

    return None, False


def download_wms_stock_list(
    username: Optional[str] = None,
    password: Optional[str] = None,
    download_dir: Optional[Path] = None,
    headless: bool = True,
    timeout: int = 60
) -> Optional[Path]:
    """
    ACME Manufacturing WMS에서 최신 Stock List 엑셀 파일을 자동 다운로드합니다.
    
    Args:
        username: WMS 계정 ID (미지정 시 .env WMS_USERNAME 사용)
        password: WMS 계정 PW (미지정 시 .env WMS_PASSWORD 사용)
        download_dir: 엑셀 파일 저장 폴더 (기본: wms_daily_stack)
        headless: 브라우저 백그라운드 구동 여부 (기본: True)
        timeout: 최대 대기 시간 (초)
        
    Returns:
        다운로드 완료된 엑셀 파일 Path 객체, 실패 시 None
    """
    user = username or _get_ops_env("WMS_USERNAME", "wms_user")
    pw = password or os.getenv("WMS_PASSWORD", "")
    save_path = download_dir or WMS_DOWNLOAD_DIR
    save_path.mkdir(parents=True, exist_ok=True)

    try:
        from selenium_lock import selenium_session_lock
    except ImportError:
        HQ_DIR = Path(__file__).resolve().parent.parent.parent / "00_HQ_TaskManager"
        if str(HQ_DIR) not in sys.path:
            sys.path.insert(0, str(HQ_DIR))
        from selenium_lock import selenium_session_lock

    with selenium_session_lock(owner="WMS_StockList_Crawler", timeout=120) as acquired:
        if not acquired:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ❌ Selenium 브라우저 잠금 획득 실패 (동시 브라우저 1개 제한 준수)")
            return None

        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] WMS Stock List 다운로드 작업 시작...")
        print(f"   - 대상 URL: {WMS_URL}")
        print(f"   - 저장 위치: {save_path.resolve()}")
        print(f"   - 구동 모드: {'Headless' if headless else 'Visible'}")

        _quarantine_stale_crdownloads(save_path, max_age_seconds=3600.0)
        before_files = set(glob.glob(str(save_path / "*.xlsx")) + glob.glob(str(save_path / "*.xls")))
        before_mtimes = {f: os.path.getmtime(f) for f in before_files if os.path.exists(f)}

        options = Options()
        options.page_load_strategy = "eager"
        options.add_argument("--ignore-certificate-errors")
        if headless:
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-position=-3000,-3000")
            options.add_argument("--window-size=1920,1080")
        else:
            options.add_argument("--start-maximized")

        prefs = {
            "download.default_directory": str(save_path.resolve()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "safebrowsing.disable_download_protection": True
        }
        options.add_experimental_option("prefs", prefs)

        service = Service(str(CHROMEDRIVER_PATH)) if CHROMEDRIVER_PATH.exists() else None
        if service:
            import subprocess
            service.creation_flags = subprocess.CREATE_NO_WINDOW
        driver = webdriver.Chrome(service=service, options=options)
        driver_pid = driver.service.process.pid if driver.service and driver.service.process else None

    try:
        # Chrome DevTools Protocol을 통해 다운로드 폴더 강제 지정
        try:
            driver.execute_cdp_cmd("Page.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(save_path.resolve())
            })
        except Exception as e:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] CDP 경고: {e}")

        # 1. WMS 접속
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 1. WMS 접속 중...")
        driver.get(WMS_URL)
        time.sleep(5)

        # 2. 로그인 처리 (SSO 자동인증 or 폼 로그인)
        login_inputs = driver.find_elements(By.ID, "potal_id")
        if login_inputs and login_inputs[0].is_displayed():
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 2. 로그인 입력 (User: {user})...")
            login_inputs[0].clear()
            login_inputs[0].send_keys(user)
            pwd_el = driver.find_element(By.ID, "pwd")
            pwd_el.clear()
            pwd_el.send_keys(pw)
            driver.find_element(By.ID, "btnLogin").click()
            time.sleep(5)
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 2. SSO 자동 인증 완료 (URL: {driver.current_url})")

        # 3. Stock List 메뉴 클릭
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 3. 'Stock List' 메뉴 클릭...")
        time.sleep(2)
        _retry_click(driver, By.XPATH, "//li[@title='Stock List']", timeout=20)
        time.sleep(3)

        # 4. Search 버튼 클릭
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 4. 'Search' 버튼 클릭 (전체 재고 조회)...")
        _retry_click(driver, By.XPATH, "//button[contains(@class, 'rz-primary') and contains(., 'Search')]", timeout=20)
        time.sleep(3)

        # 5. 데이터 조회 로딩 대기 (최소 15초 이상, 그리드 데이터 행 렌더링 확인)
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 5. 재고 데이터 로딩 대기 (그리드 행 렌더링 감지)...")
        downloads_dir = Path.home() / "Downloads"
        before_downloads = set(glob.glob(str(downloads_dir / "*.xlsx")) + glob.glob(str(downloads_dir / "*.xls"))) if downloads_dir.exists() else set()
        before_dl_mtimes = {f: os.path.getmtime(f) for f in before_downloads if os.path.exists(f)}

        grid_ready = False
        for w in range(90):
            time.sleep(1)
            # 로딩 인디케이터 확인
            loading_indicators = driver.find_elements(
                By.XPATH,
                "//*[contains(@class, 'rz-dialog') or contains(@class, 'loading') or @id='overlay' or contains(@class, 'busy')]"
            )
            visible_ind = [el for el in loading_indicators if el.is_displayed()]

            # 실제 그리드 데이터 행 확인
            data_rows = driver.find_elements(By.XPATH, "//table[contains(@class, 'rz-grid-table')]//tbody/tr")
            has_data = len(data_rows) >= 5

            # 최소 15초 경과 후 오버레이가 없고 실제 데이터 행이 렌더링되었을 때 완료
            if not visible_ind and has_data and w >= 15:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 데이터 로딩 완료: 행 수 {len(data_rows)}개 감지 ({w}초 소요)")
                grid_ready = True
                break
            elif not visible_ind and w >= 30 and has_data:
                print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 데이터 로딩 완료 ({w}초 소요)")
                grid_ready = True
                break

        if not grid_ready:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⚠️ 그리드 행 감지 지연, 추가 5초 안정화 대기...")
            time.sleep(5)
        else:
            time.sleep(3)  # SignalR 이벤트 바인딩 안정화

        # 6. Excel 다운로드 버튼 클릭
        click_start_time = time.time()
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 6. 'Excel' 다운로드 버튼 클릭...")
        _retry_click(driver, By.XPATH, "//button[contains(@class, 'rz-success') and contains(., 'Excel')]", timeout=20)

        # 브라우저 알럿 팝업 처리
        time.sleep(3)
        try:
            alert = driver.switch_to.alert
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 브라우저 알림 수락: '{alert.text}'")
            alert.accept()
        except Exception:
            pass

        # 7. 파일 다운로드 감지 및 저장 (save_path 및 Downloads 폴더 양방향 감시)
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 7. 엑셀 파일 다운로드 완료 대기 중...")
        downloaded_file = None
        for sec in range(timeout * 2):
            time.sleep(1)
            crdownloads = _get_active_crdownloads(save_path, click_start_time=click_start_time, downloads_dir=downloads_dir)
            if crdownloads:
                if sec % 10 == 0:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 파일 전송 진행 중... ({sec}초 경과)")
                continue

            # 1차 & 2차: save_path 및 Downloads 폴더 감시 (신규 파일 및 동일 파일명 덮어쓰기 mtime 감지)
            detected_path, from_dl = _detect_completed_download(
                save_path=save_path,
                before_mtimes=before_mtimes,
                downloads_dir=downloads_dir,
                before_dl_mtimes=before_dl_mtimes
            )
            if detected_path is not None:
                downloaded_file = detected_path
                if from_dl:
                    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 💡 Downloads 폴더에서 파일 감지됨: {downloaded_file.name}")
                break

        if downloaded_file and downloaded_file.exists():
            timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            target_name = save_path / f"WMS_StockList_HL_{timestamp_str}.xlsx"
            time.sleep(1)
            import shutil
            shutil.move(str(downloaded_file), str(target_name))
            print(f"\n[{datetime.datetime.now().strftime('%H:%M:%S')}] 🎉 [성공] WMS 최신 재고 파일 다운로드 및 적재 완료!")
            print(f"   - 파일명: {target_name.name}")
            print(f"   - 전체 경로: {target_name.resolve()}")
            print(f"   - 파일 크기: {target_name.stat().st_size:,} bytes\n")
            return target_name
        else:
            print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ⚠️ 신규 다운로드 파일을 감지하지 못했습니다.")
            return None

    except Exception as e:
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] ❌ 크롤러 실행 오류: {e}")
        import traceback
        traceback.print_exc()
        return None

    finally:
        time.sleep(1)
        try:
            driver.quit()
        except Exception:
            pass
        if 'driver_pid' in locals() and driver_pid:
            try:
                from collect_retry import cleanup_driver_process
                cleanup_driver_process(driver_pid)
            except Exception:
                pass
        print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] 브라우저 세션 정상 종료.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WMS Stock List 자동 크롤러")
    parser.add_argument("--visible", action="store_true", help="화면 표시 모드로 실행 (기본값: 헤드리스 백그라운드)")
    parser.add_argument("--headless", action="store_true", default=True, help="헤드리스 백그라운드 모드 (기본값)")
    parser.add_argument("--dest", type=str, default=None, help="다운로드 저장 대상 디렉토리 경로 (기본값: wms_daily_stack)")
    args = parser.parse_args()

    is_headless = not args.visible
    dest_path = Path(args.dest) if args.dest else None
    download_wms_stock_list(download_dir=dest_path, headless=is_headless)
