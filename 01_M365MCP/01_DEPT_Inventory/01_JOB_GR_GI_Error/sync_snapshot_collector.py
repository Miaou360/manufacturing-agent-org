"""
Simultaneous Snapshot Collector (WMS & SAP MB52)
================================================
01_Inventory_Team / 01_GR_GI_Error
- 실시간 물류 변동에 따른 오차를 최소화하기 위해 WMS 실재고와 SAP MB52 재고를 동일 시점에 동시 수집합니다.
- WMS: Selenium headless/eager crawler -> wms_parts_stack/
- SAP: MB52 P001 Automated Export (01. MB52_P001.ipynb 기반) -> mb52/
"""

import os
import sys
import time
import glob
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
from typing import Optional, Dict, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

WMS_SAVE_DIR = BASE_DIR / "wms_parts_stack"
MB52_SAVE_DIR = BASE_DIR / "mb52"

WMS_SAVE_DIR.mkdir(parents=True, exist_ok=True)
MB52_SAVE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SnapshotCollector")

def export_sap_mb52(plant: str = _get_ops_env("SAP_PLANT", "P001"), timeout: int = 60) -> Optional[Path]:
    """
    Downloads MB52 inventory from SAP GUI for the specified plant.
    Uses native VBScript runner for 100% reliable COM automation.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_filename = f"MB52_{plant}_{timestamp}.XLS"
    target_path = MB52_SAVE_DIR / target_filename
    res_path = BASE_DIR / "scratch" / f"mb52_done_{timestamp}.txt"
    vbs_path = BASE_DIR / "scratch" / f"export_mb52_{timestamp}.vbs"

    logger.info(f"[SAP-MB52] Starting SAP MB52 export for Plant {plant}...")
    try:
        from sap_launcher import probe_sap_health, ensure_sap_open, run_vbs_in_sap
        probe = probe_sap_health(timeout=10)
        if not probe.get("alive"):
            ensure_sap_open()

        vbs_code = f'''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If

session.findById("wnd[0]/tbar[0]/okcd").text = "/nmb52"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1500

session.findById("wnd[0]/usr/ctxtMATNR-LOW").text = ""
session.findById("wnd[0]/usr/ctxtMATNR-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtWERKS-LOW").text = "{plant}"
session.findById("wnd[0]/usr/ctxtWERKS-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtLGORT-LOW").text = ""
session.findById("wnd[0]/usr/ctxtLGORT-HIGH").text = ""
session.findById("wnd[0]").sendVKey 8
WScript.Sleep 2500

session.findById("wnd[0]/mbar/menu[0]/menu[1]/menu[2]").select()
WScript.Sleep 1500

session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").select()
session.findById("wnd[1]/tbar[0]/btn[0]").press()
WScript.Sleep 1500

session.findById("wnd[1]/usr/ctxtDY_PATH").text = "{str(MB52_SAVE_DIR.resolve())}"
session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "{target_filename}"
session.findById("wnd[1]/tbar[0]/btn[11]").press()
WScript.Sleep 2000

On Error Resume Next
session.findById("wnd[2]/tbar[0]/btn[11]").press()
session.findById("wnd[1]/tbar[0]/btn[11]").press()

Dim fso, f
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.CreateTextFile("{str(res_path.resolve())}", True)
f.WriteLine "OK"
f.Close
'''
        with open(vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_code)

        run_vbs_in_sap(vbs_path, res_path, timeout=60)
        if target_path.exists() and target_path.stat().st_size > 10000:
            logger.info(f"[SAP-MB52] Successfully exported: {target_path} ({target_path.stat().st_size:,} bytes)")
            return target_path, True
    except Exception as e:
        if target_path.exists() and target_path.stat().st_size > 10000:
            logger.info(f"[SAP-MB52] Exported file verified after runner notice: {target_path} ({target_path.stat().st_size:,} bytes)")
            return target_path, True
        logger.error(f"[SAP-MB52] Export failed: {e}")

    # Fallback to most recent MB52 (STALE, REPORT ONLY)
    existing = sorted(glob.glob(str(MB52_SAVE_DIR / "*.xls*")), key=os.path.getmtime, reverse=True)
    if existing:
        logger.warning(f"[SAP-MB52] FRESH EXPORT FAILED! Using stale fallback: {existing[0]} (TRANSFERS FORBIDDEN)")
        return Path(existing[0]), False
    return None, False

def _ensure_canonical_wms_filename(target_file: Path) -> Path:
    """Ensures a freshly crawled WMS file has the canonical WMS_StockList_HL_%Y%m%d_%H%M%S.xlsx name."""
    if not target_file or not target_file.exists():
        return target_file
    if target_file.name.startswith("WMS_StockList_HL_"):
        return target_file
    st = target_file.stat()
    ts_str = datetime.fromtimestamp(st.st_mtime).strftime("%Y%m%d_%H%M%S")
    renamed = target_file.parent / f"WMS_StockList_HL_{ts_str}{target_file.suffix}"
    if renamed.exists() and renamed.resolve() != target_file.resolve():
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        renamed = target_file.parent / f"WMS_StockList_HL_{ts_str}{target_file.suffix}"
    target_file.rename(renamed)
    os.utime(renamed, (st.st_atime, st.st_mtime))
    logger.info(f"[WMS] Renamed raw WMS file '{target_file.name}' -> '{renamed.name}'")
    return renamed


def export_wms_stock(max_retries: int = 3, run_start_time: float = None) -> Tuple[Optional[Path], bool]:
    """
    Crawls WMS Stock List using Selenium and saves to wms_parts_stack.
    Returns (Path, is_fresh_boolean).
    """
    logger.info("[WMS] Starting WMS Stock List crawler (resilient mode)...")
    stock_acc_dir = BASE_DIR.parent / "00_JOB_StockAccuracy"
    crawler_script = stock_acc_dir / "wms_crawler.py"
    start_ts = run_start_time or time.time()
    
    if crawler_script.exists():
        import subprocess
        for attempt in range(1, max_retries + 1):
            logger.info(f"[WMS] Crawler attempt {attempt}/{max_retries}...")
            cmd = [sys.executable, str(crawler_script), "--dest", str(WMS_SAVE_DIR.resolve()), "--headless"]
            res = subprocess.run(cmd, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            
            if res.returncode == 0:
                latest_files = sorted(glob.glob(str(WMS_SAVE_DIR / "*.xlsx")), key=os.path.getmtime, reverse=True)
                if latest_files:
                    target_file = Path(latest_files[0])
                    # Integrity check: file must be non-empty (>50KB) and modified after run started
                    mtime = target_file.stat().st_mtime
                    if target_file.stat().st_size > 50000 and (mtime >= start_ts - 5.0):
                        target_file = _ensure_canonical_wms_filename(target_file)
                        logger.info(f"[WMS] Successfully crawled fresh WMS stock: {target_file} ({target_file.stat().st_size:,} bytes)")
                        return target_file, True
                    elif target_file.stat().st_size <= 50000:
                        logger.warning(f"[WMS] File size suspiciously small ({target_file.stat().st_size} bytes). Retrying...")
                    else:
                        logger.warning(f"[WMS] File modified before current run (mtime={mtime}, start={start_ts}). Retrying...")
            else:
                logger.warning(f"[WMS] Attempt {attempt} failed (returncode {res.returncode}): {res.stderr.strip()[:200]}")
            
            if attempt < max_retries:
                backoff = attempt * 3
                logger.info(f"[WMS] Waiting {backoff}s before retry...")
                time.sleep(backoff)
                
    # Fallback to latest in 00_JOB_StockAccuracy/wms_daily_stack (Read-only report mode)
    daily_stack = stock_acc_dir / "wms_daily_stack"
    existing = sorted(glob.glob(str(daily_stack / "*.xlsx")), key=os.path.getmtime, reverse=True)
    if existing:
        logger.warning(f"[WMS] FRESH CRAWL FAILED! Falling back to 04:00 daily stack (REPORT ONLY, NO TRANSFERS): {existing[0]}")
        return Path(existing[0]), False
        
    return None, False

def collect_simultaneous_snapshots(
    max_freshness_seconds: float = 300.0,
    enable_retry: bool = True,
    time_budget_seconds: float = 600.0,
    sap_lock_owner: Optional[str] = None
) -> Dict:
    """
    Executes both SAP MB52 and WMS Stock extraction simultaneously.
    Guarantees strict fresh snapshot verification before authorizing transfers.
    Both files MUST be freshly collected in this cycle AND within max_freshness_seconds.
    Under D41, if extraction fails or pair desynchronizes (>300s), autonomous retries
    and resynchronization are attempted within the allocated time budget.
    """
    logger.info("=== Starting Simultaneous Snapshot Collection (WMS & SAP MB52) ===")
    run_start = time.time()
    results = {}
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_sap = executor.submit(export_sap_mb52, _get_ops_env("SAP_PLANT", "P001"))
        future_wms = executor.submit(export_wms_stock, 3, run_start)
        
        sap_path, sap_fresh = future_sap.result()
        wms_path, wms_fresh = future_wms.result()
        
    time_diff = None
    if sap_path and wms_path and sap_path.exists() and wms_path.exists():
        time_diff = abs(sap_path.stat().st_mtime - wms_path.stat().st_mtime)

    # If first attempt is already synchronized & fresh, return immediately (zero latency)
    if wms_fresh and sap_fresh and time_diff is not None and time_diff <= max_freshness_seconds:
        results["SAP_MB52"] = sap_path
        results["WMS_STOCK"] = wms_path
        results["is_wms_fresh"] = True
        results["is_sap_fresh"] = True
        results["time_diff_sec"] = round(time_diff, 1)
        results["can_execute_transfers"] = True
        logger.info(f"Collection Complete: SAP={sap_path} (fresh=True) | WMS={wms_path} (fresh=True) | TimeDiff={time_diff:.1f}s | CanTransfer=True")
        return results

    # D41 Autonomous Retry & Resynchronization if initial attempt was not fully fresh or desynchronized
    if enable_retry:
        try:
            HQ_DIR = BASE_DIR.parent.parent / "00_HQ_TaskManager"
            if str(HQ_DIR) not in sys.path:
                sys.path.insert(0, str(HQ_DIR))
            from collect_retry import collect_paired_snapshots_resilient

            # Auto-detect SAP lock owner from current process if not explicitly passed
            if sap_lock_owner is None:
                try:
                    from sap_lock import _global_lock
                    l_info = _global_lock._read_lock_info()
                    if l_info.get("pid") == os.getpid():
                        sap_lock_owner = l_info.get("owner")
                except Exception:
                    pass

            remaining_budget = max(30.0, time_budget_seconds - (time.time() - run_start))
            logger.warning(f"⚠️ [D41 RE-SYNC] Initial simultaneous collection imperfect (wms_fresh={wms_fresh}, sap_fresh={sap_fresh}, diff={time_diff}s). Engaging resilient recovery engine...")

            def _get_wms():
                return export_wms_stock(max_retries=1, run_start_time=time.time())

            def _get_sap():
                return export_sap_mb52(plant=_get_ops_env("SAP_PLANT", "P001"))

            retry_results = collect_paired_snapshots_resilient(
                collect_wms_func=_get_wms,
                collect_sap_func=_get_sap,
                max_pair_diff_seconds=max_freshness_seconds,
                time_budget_seconds=remaining_budget,
                max_retries=2,
                sap_lock_owner=sap_lock_owner
            )
            if retry_results.get("can_execute_transfers"):
                return retry_results
            elif retry_results.get("WMS_STOCK") and retry_results.get("SAP_MB52"):
                results.update(retry_results)
                return results
        except Exception as e_retry:
            logger.error(f"❌ D41 Resilient recovery engine error: {e_retry}")

    results["SAP_MB52"] = sap_path
    results["WMS_STOCK"] = wms_path
    results["is_wms_fresh"] = wms_fresh
    results["is_sap_fresh"] = sap_fresh
    results["time_diff_sec"] = round(time_diff, 1) if time_diff is not None else None
    results["can_execute_transfers"] = False
    logger.warning(f"[SNAPSHOT] Freshness check failed: wms_fresh={wms_fresh}, sap_fresh={sap_fresh}, time_diff={time_diff}s (threshold: {max_freshness_seconds}s)")
    return results

if __name__ == "__main__":
    snapshots = collect_simultaneous_snapshots()
    print("Collected Snapshots:", snapshots)
