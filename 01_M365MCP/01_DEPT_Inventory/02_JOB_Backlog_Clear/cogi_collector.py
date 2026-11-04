"""
SAP COGI Backlog Collector & Reprocessor
========================================
01_Inventory_Team / 02_Backlog
- SAP COGI 트랜잭션을 실행하여 생산 확정 후 출고 지연/결품 에러(Backlog) 목록을 자동 추출합니다.
- Plant: P001 (Headlamp)
- MRP Controller: i01 ~ i03
- Date Range: 당월 1일 ~ 현재일 (YYYY.MM.01 ~ YYYY.MM.DD)
- MB1B 311 재고 이동 후 다시 COGI에 진입하여 '전체 선택(btn[9]) -> 저장(btn[11])'으로 백로그 자동 확정 처리.
"""

import os
import sys
import time
import re
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
from typing import List, Dict, Tuple, Optional, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
COGI_DIR = BASE_DIR / "cogi_errors"
RAW_HISTORY_DIR = COGI_DIR / "raw_history"
SCRATCH_DIR = BASE_DIR / "scratch"

COGI_DIR.mkdir(parents=True, exist_ok=True)
RAW_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

from sap_launcher import ensure_sap_open, run_vbs_in_sap

def get_cogi_date_range(ref_date: datetime = None) -> Tuple[str, str, bool]:
    """
    Calculates COGI selection date range:
    1. Standard (Day 2~31): 1st of current month to YESTERDAY (e.g. Oct 1 ~ Oct 22 if today is Oct 23).
    2. Month-Beginning (Day 1): 1st of previous month to LAST DAY of previous month
       (e.g. Sep 1 ~ Sep 30 if today is Oct 1).
    Returns (from_date_str, to_date_str, is_prev_month).
    """
    if ref_date is None:
        ref_date = datetime.now()

    yesterday = ref_date - timedelta(days=1)

    if ref_date.day == 1:
        # Today is 1st: yesterday was last day of previous month
        from_date = yesterday.replace(day=1).strftime("%Y.%m.%d")
        to_date = yesterday.strftime("%Y.%m.%d")
        is_prev_month = True
    else:
        from_date = ref_date.strftime("%Y.%m.01")
        to_date = yesterday.strftime("%Y.%m.%d")
        is_prev_month = False

    return from_date, to_date, is_prev_month

def parse_cogi_file(file_path: Path) -> List[Dict[str, Any]]:
    """
    Parses an exported COGI UTF-16 spreadsheet file into structured records.
    COGI exports pair rows:
      Row A: ['X', Material, Plant, SLoc, MTy, Qty, UoE, ErrorGroup, ErrorNo, Count]
      Row B: [Material_Desc, EntryDate, ErrorDate]
    """
    if not file_path.exists():
        return []

    try:
        with open(file_path, "r", encoding="utf-16", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    items = []
    i = 0
    total_lines = len(lines)

    while i < total_lines:
        line_a = lines[i].strip()
        parts_a = [p.strip() for p in line_a.split("\t") if p.strip()]

        # Check if line_a is an item header line starting with 'X'
        if parts_a and parts_a[0] == 'X' and len(parts_a) >= 6:
            mat = parts_a[1]
            plant = parts_a[2] if len(parts_a) > 2 else _get_ops_env("SAP_PLANT", "P001")
            sloc = parts_a[3] if len(parts_a) > 3 else ""
            mty = parts_a[4] if len(parts_a) > 4 else "261"
            
            # Clean quantity string (remove commas)
            raw_qty = parts_a[5].replace(",", "")
            try:
                qty = float(raw_qty)
            except ValueError:
                qty = 0.0

            uoe = parts_a[6] if len(parts_a) > 6 else "EA"
            
            # Error codes (e.g. M7 021)
            err_grp = parts_a[7] if len(parts_a) > 7 else ""
            err_no = parts_a[8] if len(parts_a) > 8 else ""
            err_code = f"{err_grp} {err_no}".strip()

            # Next line usually contains description and dates
            desc = ""
            entry_date = ""
            error_date = ""
            if i + 1 < total_lines:
                parts_b = [p.strip() for p in lines[i+1].strip().split("\t") if p.strip()]
                if parts_b and parts_b[0] != 'X':
                    desc = parts_b[0]
                    if len(parts_b) > 1:
                        entry_date = parts_b[1]
                    if len(parts_b) > 2:
                        error_date = parts_b[2]
                    i += 1  # Skip consumed detail line

            can_resolve = (err_grp == "M7" and err_no == "021") or ("deficit" in desc.lower())

            items.append({
                "Material": mat,
                "Material_Desc": desc,
                "Plant": plant,
                "SLoc": sloc,
                "MTy": mty,
                "Qty": qty,
                "UoE": uoe,
                "Error_Code": err_code,
                "Entry_Date": entry_date,
                "Error_Date": error_date,
                "Can_Resolve_By_Transfer": can_resolve
            })

        i += 1

    return items


def export_cogi_errors(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    dispo_low: str = "i01",
    dispo_high: str = "i03",
    output_dir: Optional[Path] = None,
    filename_prefix: str = "COGI_"
) -> Tuple[Optional[Path], List[Dict[str, Any]]]:
    """
    Automates SAP COGI export to an Excel file and returns parsed items.
    Ensures COGI window is closed (/n) afterwards.
    """
    ensure_sap_open()

    if output_dir is None:
        output_dir = COGI_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    from_date, to_date, is_prev = get_cogi_date_range()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_filename = f"{filename_prefix}{plant}_{timestamp}.XLS"
    target_path = output_dir / target_filename

    res_path = SCRATCH_DIR / f"cogi_export_{timestamp}.txt"
    if res_path.exists():
        res_path.unlink()

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

On Error Resume Next

' 1. Enter COGI
session.findById("wnd[0]/tbar[0]/okcd").text = "/ncogi"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1000

' 2. Fill Selection criteria
session.findById("wnd[0]/usr/ctxtS_WERKS-LOW").text = "{plant}"
session.findById("wnd[0]/usr/ctxtS_DISPO-LOW").text = "{dispo_low}"
session.findById("wnd[0]/usr/ctxtS_DISPO-HIGH").text = "{dispo_high}"
session.findById("wnd[0]/usr/ctxtP_DATPV").text = "{from_date}"
session.findById("wnd[0]/usr/ctxtP_DATPB").text = "{to_date}"

' 3. Execute F8
session.findById("wnd[0]/tbar[1]/btn[8]").press
WScript.Sleep 1500
Dim f8Wait
f8Wait = 0
Do While session.Busy And f8Wait < 30
   WScript.Sleep 500
   f8Wait = f8Wait + 1
Loop

' 4. Check if error table loaded or empty status
Dim sbar_text
sbar_text = session.findById("wnd[0]/sbar").text

' 5. Select All & Export
session.findById("wnd[0]/tbar[1]/btn[9]").press
WScript.Sleep 500
session.findById("wnd[0]/mbar/menu[0]/menu[4]").select
WScript.Sleep 1500

' 6. Confirm Spreadsheet radio (SPO5)
session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").select
session.findById("wnd[1]/tbar[0]/btn[0]").press
WScript.Sleep 1500

' 7. Specify Path & Filename
session.findById("wnd[1]/usr/ctxtDY_PATH").text = "{str(output_dir.resolve())}"
session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "{target_filename}"
session.findById("wnd[1]/tbar[0]/btn[11]").press
WScript.Sleep 1500
session.findById("wnd[1]/tbar[0]/btn[0]").press
WScript.Sleep 1500

' 8. Handle Overwrite dialog if any
session.findById("wnd[2]/tbar[0]/btn[11]").press
session.findById("wnd[1]/tbar[0]/btn[11]").press

' 9. IMPORTANT: Exit COGI back to SAP Easy Access (/n)
session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0

' 10. Write Result
Dim fso, resFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set resFile = fso.CreateTextFile("{str(res_path.resolve())}", True)
resFile.WriteLine "DONE: " & sbar_text
resFile.Close
'''
    vbs_path = SCRATCH_DIR / f"cogi_export_{timestamp}.vbs"
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_code)

    print(f"[COGI COLLECTOR] Exporting COGI for {plant} ({dispo_low}~{dispo_high}) -> {target_filename}...")
    run_vbs_in_sap(vbs_path, res_path, timeout=35)

    if target_path.exists() and target_path.stat().st_size > 0:
        items = parse_cogi_file(target_path)
        print(f"                 ✅ Successfully exported: {len(items)} backlog items parsed ({target_path.stat().st_size:,} bytes).")
        return target_path, items
    else:
        print(f"                 ⚠️ COGI export failed or no data returned.")
        return None, []


def post_and_save_cogi(plant: str = _get_ops_env("SAP_PLANT", "P001"), dispo_low: str = "i01", dispo_high: str = "i03") -> str:
    """
    Enters COGI, executes search with date range, selects all items,
    and presses Save (btn[11]) to clear resolved backlogs in SAP.
    Exits back to /n when complete.
    """
    ensure_sap_open()

    from_date, to_date, is_prev = get_cogi_date_range()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    res_path = SCRATCH_DIR / f"cogi_save_{timestamp}.txt"
    if res_path.exists():
        res_path.unlink()

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

On Error Resume Next

' 1. Enter COGI
session.findById("wnd[0]/tbar[0]/okcd").text = "/ncogi"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1000

' 2. Fill Selection criteria
session.findById("wnd[0]/usr/ctxtS_WERKS-LOW").text = "{plant}"
session.findById("wnd[0]/usr/ctxtS_DISPO-LOW").text = "{dispo_low}"
session.findById("wnd[0]/usr/ctxtS_DISPO-HIGH").text = "{dispo_high}"
session.findById("wnd[0]/usr/ctxtP_DATPV").text = "{from_date}"
session.findById("wnd[0]/usr/ctxtP_DATPB").text = "{to_date}"

' 3. Execute F8
session.findById("wnd[0]/tbar[1]/btn[8]").press
WScript.Sleep 2500

' 4. Select All (btn[9]) and Save (btn[11])
session.findById("wnd[0]/tbar[1]/btn[9]").press
WScript.Sleep 500
session.findById("wnd[0]/tbar[0]/btn[11]").press

' 5. Dynamic Detection Loop: Monitor session.Busy, modal popups, and status bar text
Dim maxWaitSec, elapsedSec, isFinished, sbar_text, popup_text
maxWaitSec = 120
elapsedSec = 0
isFinished = False
sbar_text = ""
popup_text = ""

Do While elapsedSec < maxWaitSec
   WScript.Sleep 1000
   elapsedSec = elapsedSec + 1
   
   ' Check A: Modal Dialog (Information / Warning / Confirmation) appeared
   If session.Children.Count > 1 Then
      isFinished = True
      Exit Do
   End If
   
   ' Check B: Session is idle and status bar has completion message
   If Not session.Busy Then
      Dim curStatus
      curStatus = Trim(session.findById("wnd[0]/sbar").text)
      If Len(curStatus) > 0 Then
         sbar_text = curStatus
         isFinished = True
         Exit Do
      End If
   End If
Loop

' 6. Dismiss any confirmation popup if appeared and capture message
If session.Children.Count > 1 Then
   On Error Resume Next
   popup_text = session.findById("wnd[1]/usr/txtMESSTXT1").text
   If Len(popup_text) = 0 Then
      popup_text = session.findById("wnd[1]").text
   End If
   session.findById("wnd[1]/tbar[0]/btn[0]").press
   session.findById("wnd[1]").close
   WScript.Sleep 1000
   On Error Resume Next
End If

' Capture final status bar text if empty
If Len(sbar_text) = 0 Then
   sbar_text = Trim(session.findById("wnd[0]/sbar").text)
End If

If Len(popup_text) > 0 Then
   sbar_text = popup_text & " | " & sbar_text
End If

' 7. Exit COGI back to /n
session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1000

' 8. Write Result
Dim fso, resFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set resFile = fso.CreateTextFile("{str(res_path.resolve())}", True)
resFile.WriteLine sbar_text & " (Processing Elapsed: " & elapsedSec & "s)"
resFile.Close
'''
    vbs_path = SCRATCH_DIR / f"cogi_save_{timestamp}.vbs"
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_code)

    print(f"[COGI REPROCESS] Executing Select All & Save in COGI for {plant} (Dynamic completion monitoring up to 120s)...")
    res = run_vbs_in_sap(vbs_path, res_path, timeout=150)
    print(f"                 SAP Response: '{res}'")
    return res


def refresh_and_extract_initial_backlog(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    dispo_low: str = "i01",
    dispo_high: str = "i03"
) -> Tuple[Optional[Path], List[Dict[str, Any]], str]:
    """
    1. Pre-Refresh Save: Enters COGI and performs Select All -> Save first to refresh SAP state
       (clearing any released table locks or newly arrived inventory before daily processing).
    2. Exports the remaining COGI backlog to cogi_errors/raw_history/ as the official
       Pre-Resolution Raw Backlog History.
    3. Returns (target_path, parsed_items, save_status_msg).
    """
    print(f"\n========================================================")
    print(f" [COGI PRE-REFRESH & RAW HISTORY EXTRACTION]")
    print(f" Plant: {plant} | Dispo: {dispo_low}~{dispo_high}")
    print(f"========================================================")
    
    # Step 1: Pre-refresh Save
    print(f"[COGI PRE-REFRESH] 1. Executing COGI Select All -> Save to refresh backlog state...")
    save_msg = post_and_save_cogi(plant=plant, dispo_low=dispo_low, dispo_high=dispo_high)
    time.sleep(2.5)

    # Step 2: Export refreshed raw history
    print(f"[COGI PRE-REFRESH] 2. Exporting Pre-Resolution Raw Backlog to raw_history/...")
    target_path, items = export_cogi_errors(
        plant=plant,
        dispo_low=dispo_low,
        dispo_high=dispo_high,
        output_dir=RAW_HISTORY_DIR,
        filename_prefix="COGI_RAW_INITIAL_"
    )
    
    if target_path and target_path.exists():
        print(f"[COGI PRE-REFRESH] ✅ Raw backlog history archived: {target_path.name} ({len(items)} items)")
    else:
        print(f"[COGI PRE-REFRESH] ⚠️ No backlog items remaining or export empty after refresh.")

    return target_path, items, save_msg


if __name__ == "__main__":
    file_path, items = export_cogi_errors()
    print(f"\n[Test Export Result]: {len(items)} items found.")
    for it in items[:5]:
        print("  -", it)
