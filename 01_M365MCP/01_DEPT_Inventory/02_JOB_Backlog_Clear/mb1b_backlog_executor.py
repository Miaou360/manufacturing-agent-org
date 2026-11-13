"""
MB1B Backlog Transfer Posting Automated Executor
=================================================
01_Inventory_Team / 02_Backlog
- SAP COGI 생산 백로그를 해소하기 위해 MB1B 311 플랜트 내 저장위치 간 이동전기를 자동 실행합니다.
- 선행 가드레일: WMS-SAP 재고정확도 분석을 통과한 건만 실행.
- 필수 명시 헤더: Header Text = "Backlog_Clear"
- 수하 저장위치: 생산창고(SP31, SP11) 혹은 품질창고(SL49)
- 출하 저장위치: 가용 잉여재고가 있는 보관창고(SL02, SL00 등)
- 모든 실행 결과는 db/backlog_history.db 에 실시간 감사 기록됩니다.
"""

import os
import sys
import time
import subprocess
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
import pandas as pd
from typing import Dict, List, Optional, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = BASE_DIR / "scratch"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

from sap_launcher import ensure_sap_open, run_vbs_in_sap
from db_manager import log_transfer_posting

def generate_mb1b_backlog_vbs(item: dict) -> tuple[Path, Path]:
    """Generates a standalone VBScript to execute one MB1B transfer posting with Backlog_Clear."""
    mat = item['Material']
    qty = str(int(item['Transfer_Qty']) if item['Transfer_Qty'] == int(item['Transfer_Qty']) else item['Transfer_Qty'])
    from_sloc = item['From_SLoc'].lower()
    to_sloc = item['To_SLoc'].lower()
    
    today_str = datetime.now().strftime("%Y.%m.%d")
    doc_date = item.get('Doc_Date', today_str)
    post_date = item.get('Post_Date', today_str)
    header_text = item.get('Header_Text', 'Backlog_Clear')
    recipient = item.get('Recipient') or _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT')

    vbs_code = f'''On Error Resume Next
If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If

' 1. Enter MB1B
session.findById("wnd[0]/tbar[0]/okcd").text = "/nmb1b"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1500

' 2. Clear & Fill Header
session.findById("wnd[0]/usr/ctxtMKPF-BLDAT").text = ""
session.findById("wnd[0]/usr/ctxtMKPF-BLDAT").text = "{doc_date}"
session.findById("wnd[0]/usr/ctxtMKPF-BUDAT").text = ""
session.findById("wnd[0]/usr/ctxtMKPF-BUDAT").text = "{post_date}"
session.findById("wnd[0]/usr/txtMKPF-BKTXT").text = ""
session.findById("wnd[0]/usr/txtMKPF-BKTXT").text = "{header_text}"
session.findById("wnd[0]/usr/ctxtRM07M-BWARTWA").text = ""
session.findById("wnd[0]/usr/ctxtRM07M-BWARTWA").text = "311"
session.findById("wnd[0]/usr/ctxtRM07M-WERKS").text = ""
session.findById("wnd[0]/usr/ctxtRM07M-WERKS").text = "{_get_ops_env('SAP_PLANT', 'P001')}"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1500

' 3. Clear & Fill Detail Header
session.findById("wnd[0]/usr/txtMSEGK-WEMPF").text = ""
session.findById("wnd[0]/usr/txtMSEGK-WEMPF").text = "{recipient}"
session.findById("wnd[0]/usr/ctxtMSEGK-UMLGO").text = ""
session.findById("wnd[0]/usr/ctxtMSEGK-UMLGO").text = "{to_sloc}"

' 4. Clear & Fill Item Row 0
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/ctxtMSEG-MATNR[0,7]").text = ""
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/ctxtMSEG-MATNR[0,7]").text = "{mat}"
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/txtMSEG-ERFMG[0,26]").text = ""
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/txtMSEG-ERFMG[0,26]").text = "{qty}"
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/ctxtMSEG-LGORT[0,48]").text = ""
session.findById("wnd[0]/usr/sub:SAPMM07M:0421/ctxtMSEG-LGORT[0,48]").text = "{from_sloc}"

' 5. Save Document
session.findById("wnd[0]/tbar[0]/btn[11]").press
WScript.Sleep 1500

' 6. Read Status Message
Dim msg
msg = session.findById("wnd[0]/sbar").text
If Err.Number <> 0 Then
   msg = "ERROR: " & Err.Description
End If

' 7. Write status message to result file
Dim fso, resFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set resFile = fso.CreateTextFile("__RESULT_PATH__", True)
resFile.WriteLine msg
resFile.Close

' 8. Safely close any popup dialog and return to Easy Access (/n)
If session.Children.Count > 1 Then
   session.findById("wnd[1]").close
End If
session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0
'''
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_path = SCRATCH_DIR / f"mb1b_backlog_{mat}_{timestamp}.vbs"
    result_path = SCRATCH_DIR / f"mb1b_backlog_res_{mat}_{timestamp}.txt"

    final_vbs = vbs_code.replace("__RESULT_PATH__", str(result_path.resolve()))
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(final_vbs)
        
    return script_path, result_path


def execute_single_backlog_transfer(item: dict) -> dict:
    """Executes a single MB1B transfer posting using the interactive SAP runner."""
    ensure_sap_open()

    vbs_path, result_path = generate_mb1b_backlog_vbs(item)
    print(f"\n[MB1B BACKLOG] Posting: Material={item['Material']} | Qty={item['Transfer_Qty']} | {item['From_SLoc']} -> {item['To_SLoc']} (Header: {item.get('Header_Text', 'Backlog_Clear')})")
    print(f"               Script: {vbs_path.name}")
    
    output = run_vbs_in_sap(vbs_path, result_path, timeout=30)
    print(f"               SAP Result: {output}")
        
    return {
        'Material': item['Material'],
        'Material_Desc': item.get('Material_Desc', ''),
        'From': item['From_SLoc'],
        'To': item['To_SLoc'],
        'Qty': item['Transfer_Qty'],
        'Header_Text': item.get('Header_Text', 'Backlog_Clear'),
        'Recipient': item.get('Recipient') or _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'),
        'Variance_Before': item.get('Variance_Before'),
        'Variance_After': item.get('Variance_After'),
        'Delta_Variance': item.get('Delta_Variance'),
        'Result': output
    }


def execute_backlog_batch(
    plan_df: pd.DataFrame,
    batch_id: str = None,
    run_date: str = None,
    run_time: str = None,
    delay_between: float = 2.0
) -> List[Dict[str, Any]]:
    """Executes all approved transfer postings in the backlog resolution plan and logs to DB."""
    ensure_sap_open()

    if not batch_id:
        batch_id = datetime.now().strftime("BATCH_BACKLOG_%Y%m%d_%H%M%S")

    ready = plan_df[plan_df['Action'] == 'TRANSFER_POSTING']
    if ready.empty:
        print("[MB1B BACKLOG BATCH] No approved transfer postings in plan.")
        return []

    results = []
    print(f"\n========================================================")
    print(f" [MB1B BACKLOG BATCH] Executing {len(ready)} items (Batch: {batch_id})...")
    print(f"========================================================")

    for idx, (_, row) in enumerate(ready.iterrows(), 1):
        item = row.to_dict()
        print(f"\n[{idx}/{len(ready)}] Material: {item['Material']} | {item['From_SLoc']} -> {item['To_SLoc']} ({item['Transfer_Qty']} EA)")
        res = execute_single_backlog_transfer(item)

        # Log to SQLite database
        try:
            log_transfer_posting(batch_id, res, run_date=run_date, run_time=run_time)
        except Exception as e_db:
            print(f"               [DB LOG WARNING] Could not log to DB: {e_db}")

        results.append(res)
        time.sleep(delay_between)

    print(f"\n[MB1B BACKLOG BATCH COMPLETE] Successfully executed {len(results)} postings.")
    return results


if __name__ == "__main__":
    print("[MB1B BACKLOG EXECUTOR READY]")
