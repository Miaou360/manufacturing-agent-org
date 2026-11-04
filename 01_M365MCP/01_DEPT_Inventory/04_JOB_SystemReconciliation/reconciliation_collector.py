"""
ZTCODE_STOCK_REC WMS vs SAP Stock Accuracy Collector (SAP GUI Automated Extractor)
======================================================================
01_Inventory_Team / 04_JOB_SystemReconciliation (Plant P001 Headlamp)
- SAP GUI ZTCODE_STOCK_REC 트랜잭션을 실행하여 WMS vs SAP 전일 마감 재고정확도 리스트(radRA5 -> radRA1)를
  매일 06:35 AM EST에 자동으로 추출하여 raw_exports/ 에 안전하게 누적 저장합니다.
- Universal Zero-Collision Timestamp 표준 준수.
- SAP 세션 락(sap_session_lock) 적용으로 타 배치 프로세스와의 GUI 충돌 원천 차단.
"""

import os
import sys
import time
import shutil
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
from typing import Optional, Dict, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
RAW_DIR = BASE_DIR / "raw_exports"
SCRATCH_DIR = BASE_DIR / "scratch"
RAW_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

# Add shared HQ & sibling utilities
HQ_DIR = BASE_DIR.parent.parent / "00_HQ_TaskManager"
GR_GI_DIR = BASE_DIR.parent / "01_JOB_GR_GI_Error"
sys.path.insert(0, str(HQ_DIR))
sys.path.insert(0, str(GR_GI_DIR))

from sap_launcher import ensure_sap_open, run_vbs_in_sap
from sap_lock import sap_session_lock


def collect_ztcode_stock_rec_accuracy(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    system_group: str = "IWMS",
    posting_date: Optional[str] = None,
    timeout: int = 45
) -> Optional[Path]:
    """
    Extracts WMS vs SAP Stock Accuracy list from SAP GUI ZTCODE_STOCK_REC.
    - radRA5: Stock Accuracy
    - radRA1: IWMS (default)
    - posting_date: Optional past posting date (e.g. '2026-10-18' or '2026.10.18')
    Saves to raw_exports/ with Zero-Collision Timestamp and creates canonical copies.
    """
    now = datetime.now()
    ts_str = now.strftime("%Y%m%d_%H%M%S")

    if posting_date:
        sap_date_str = posting_date.replace("-", ".")
        date_clean = sap_date_str.replace(".", "")
        ts_target = RAW_DIR / f"ZTCODE_STOCK_REC_StockAccuracy_{system_group}_{date_clean}_{ts_str}.txt"
        date_target = RAW_DIR / f"ZTCODE_STOCK_REC_StockAccuracy_{system_group}_{date_clean}.txt"
        date_vbs = f'session.findById("wnd[0]/usr/ctxtS_BUDAT-LOW").text = "{sap_date_str}"\nsession.findById("wnd[0]/usr/ctxtS_BUDAT-HIGH").text = "{sap_date_str}"\n'
        print(f"\n[ZTCODE_STOCK_REC Collector] Starting SAP GUI extraction for Plant {plant} ({system_group}) - Posting Date: {sap_date_str}...")
    else:
        date_str = now.strftime("%Y%m%d")
        ts_target = RAW_DIR / f"ZTCODE_STOCK_REC_StockAccuracy_{system_group}_{ts_str}.txt"
        date_target = RAW_DIR / f"ZTCODE_STOCK_REC_StockAccuracy_{system_group}_{date_str}.txt"
        date_vbs = ""
        print(f"\n[ZTCODE_STOCK_REC Collector] Starting SAP GUI extraction for Plant {plant} ({system_group}) - Daily Snapshot...")

    latest_target = RAW_DIR / f"ZTCODE_STOCK_REC_StockAccuracy_{system_group}_latest.txt"
    temp_target = RAW_DIR / f"temp_ztcode_stock_rec_{system_group}.txt"
    temp_target.write_text("INIT", encoding="utf-8")
    dir_str = str(temp_target.parent.resolve()).rstrip("\\") + "\\"

    # Map system group to radio button in Screen 2
    # radRA1: IWMS, radRA2: OWMS, radRA3: MES, radRA4: Seq.Prod., radRA5: All
    rad_map = {
        "IWMS": "radRA1",
        "OWMS": "radRA2",
        "MES": "radRA3",
        "SEQ": "radRA4",
        "ALL": "radRA5"
    }
    target_rad = rad_map.get(system_group.upper(), "radRA1")

    # Ensure SAP is running and logged in
    with sap_session_lock("04_SystemReconciliation_Collector", timeout=90):
        ensure_sap_open()

        if temp_target.exists():
            temp_target.unlink(missing_ok=True)

        vbs_script = f'''On Error Resume Next
If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
Set connection = application.Children(0)
Set session    = connection.Children(0)

session.findById("wnd[0]/tbar[0]/okcd").text = "/nztcode_stock_rec"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1500

session.findById("wnd[0]/usr/radRA5").select
session.findById("wnd[0]/tbar[1]/btn[8]").press
WScript.Sleep 2000

session.findById("wnd[0]/usr/{target_rad}").select
{date_vbs}session.findById("wnd[0]/tbar[1]/btn[8]").press

Dim waitCount
waitCount = 0
Do While waitCount < 30
   WScript.Sleep 1000
   waitCount = waitCount + 1
   If Not session.Busy Then Exit Do
Loop
WScript.Sleep 2500

session.findById("wnd[0]/usr/cntlMYCONTAINER/shellcont/shell").pressToolbarContextButton "&MB_EXPORT"
session.findById("wnd[0]/usr/cntlMYCONTAINER/shellcont/shell").selectContextMenuItem "&PC"
WScript.Sleep 2000

session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").select
session.findById("wnd[1]/tbar[0]/btn[0]").press
WScript.Sleep 2000

session.findById("wnd[1]/usr/ctxtDY_PATH").text = "{dir_str}"
session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "{temp_target.name}"
session.findById("wnd[1]/tbar[0]/btn[11]").press
WScript.Sleep 2500

session.findById("wnd[2]/tbar[0]/btn[11]").press
Err.Clear

session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0

Dim fso, f
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.CreateTextFile("{str((SCRATCH_DIR / 'collector_done.txt').resolve())}", True)
f.WriteLine "OK"
f.Close
'''
        vbs_path = SCRATCH_DIR / f"extract_ztcode_stock_rec_{os.getpid()}.vbs"
        done_path = SCRATCH_DIR / "collector_done.txt"
        done_path.unlink(missing_ok=True)
        vbs_path.write_text(vbs_script, encoding="utf-8")

        try:
            res_vbs = run_vbs_in_sap(vbs_path, result_file=done_path, timeout=90)
            print(f"  [ZTCODE_STOCK_REC Collector] VBS Result: {res_vbs}")
        finally:
            vbs_path.unlink(missing_ok=True)
            done_path.unlink(missing_ok=True)

        if temp_target.exists() and temp_target.stat().st_size > 1000:
            shutil.copyfile(temp_target, ts_target)
            shutil.copyfile(temp_target, date_target)
            if posting_date is None:
                shutil.copyfile(temp_target, latest_target)
            temp_target.unlink(missing_ok=True)
            print(f"  [OK] Extraction succeeded: {ts_target.name} ({ts_target.stat().st_size:,} bytes)")
            print(f"  [OK] Canonical copy updated: {date_target.name}")
            return ts_target
        else:
            print("  [ERROR] ZTCODE_STOCK_REC export file not generated or size too small.")
            return None
            return None


if __name__ == "__main__":
    res = collect_ztcode_stock_rec_accuracy(plant=_get_ops_env("SAP_PLANT", "P001"), system_group="IWMS")
    print(f"Result: {res}")
