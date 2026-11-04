"""
MMSC Central Storage Location Automated Maintenance Operator
============================================================
01_DEPT_Inventory / 01_JOB_GR_GI_Error (Plant P001 Headlamp & P002)

Purpose:
- Handles automatic creation and extension of standard storage locations
  (SL00, SL01, SL02, SL49, SP31) in transaction MMSC for materials that
  trigger "Material <mat> P001 <sloc> does not exist" errors during GI / MB1B transfers.
- Recorded GUI Script baseline: %USERPROFILE%\\AppData\\Roaming\\SAP\\SAP GUI\\Scripts\\Script3.vbs
- Zero-Collision: Unique timestamped scripts, dynamic row discovery, and automatic DB audit logging.
"""

import os
import sys
import glob
import time
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
from typing import List, Dict, Tuple, Optional, Any
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = BASE_DIR / "scratch"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
GI_ERRORS_DIR = BASE_DIR / "gi_errors"

logger = logging.getLogger("MMSCOperator")

# Standard storage locations required for materials in Plant P001
STANDARD_REQUIRED_SLOCS = ["SL00", "SL01", "SL02", "SL49", "SP31"]


def generate_mmsc_vbs(material: str, plant: str = _get_ops_env("SAP_PLANT", "P001"), required_slocs: List[str] = None) -> Tuple[Path, Path]:
    """
    Generates a standalone VBScript to dynamically inspect and extend missing storage locations in MMSC.
    """
    if required_slocs is None:
        required_slocs = STANDARD_REQUIRED_SLOCS

    req_vbs_array = 'Array("' + '", "'.join(required_slocs) + '")'
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    script_path = SCRATCH_DIR / f"mmsc_{material}_{timestamp}.vbs"
    result_path = SCRATCH_DIR / f"mmsc_res_{material}_{timestamp}.txt"
    res_str = str(result_path.resolve()).replace("\\", "\\\\")

    vbs_code = f'''If Not IsObject(application) Then
   Set SapGuiAuto = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session = connection.Children(0)
End If

' 1. Enter MMSC Transaction
session.findById("wnd[0]/tbar[0]/okcd").text = "/nmmsc"
session.findById("wnd[0]").sendVKey 0

session.findById("wnd[0]/usr/ctxtRM03M-MATNR").text = "{material}"
session.findById("wnd[0]/usr/ctxtRM03M-WERKS").text = "{plant}"
session.findById("wnd[0]/usr/radRM03M-LFLAG").setFocus
session.findById("wnd[0]").sendVKey 0

' 2. Inspect existing storage locations in table control
Dim r, val, emptyRow, existingList
existingList = " "
emptyRow = -1

For r = 0 To 15
    On Error Resume Next
    val = session.findById("wnd[0]/usr/sub:SAPMM03M:0195/ctxtRM03M-LGORT[" & r & ",0]").text
    If Err.Number <> 0 Then
        Exit For
    End If
    val = Trim(val)
    If val <> "" Then
        existingList = existingList & val & " "
    ElseIf emptyRow = -1 Then
        emptyRow = r
    End If
Next

' 3. Identify and populate missing standard storage locations
Dim reqs, req, addedList, lastField
reqs = {req_vbs_array}
addedList = ""
lastField = ""

For Each req In reqs
    If InStr(existingList, " " & req & " ") = 0 Then
        If emptyRow >= 0 And emptyRow <= 15 Then
            session.findById("wnd[0]/usr/sub:SAPMM03M:0195/ctxtRM03M-LGORT[" & emptyRow & ",0]").text = req
            addedList = addedList & req & " "
            lastField = "wnd[0]/usr/sub:SAPMM03M:0195/ctxtRM03M-LGORT[" & emptyRow & ",0]"
            emptyRow = emptyRow + 1
        End If
    End If
Next

' 4. Save if new storage locations were added
Dim statusMsg, popupMsg
statusMsg = ""
popupMsg = ""

If Trim(addedList) <> "" Then
    If lastField <> "" Then
        session.findById(lastField).setFocus
        session.findById(lastField).caretPosition = 4
    End If
    session.findById("wnd[0]").sendVKey 11
    WScript.Sleep 1000

    ' Handle informational modal popups if present
    If session.Children.Count > 1 Then
        popupMsg = session.findById("wnd[1]").text
        session.findById("wnd[1]").sendVKey 0
        WScript.Sleep 500
    End If

    ' Handle yellow warning confirmation if sbar requires Enter
    If session.findById("wnd[0]/sbar").messageType = "W" Or session.findById("wnd[0]/sbar").messageType = "I" Then
        session.findById("wnd[0]").sendVKey 0
        WScript.Sleep 500
    End If

    statusMsg = session.findById("wnd[0]/sbar").text
    If Trim(statusMsg) = "" Then
        statusMsg = "SUCCESS_SAVED"
    End If
Else
    statusMsg = "ALL_SLOCS_ALREADY_EXIST"
End If

' 5. Write structured result to file
Dim fso, resFile
Set fso = CreateObject("Scripting.FileSystemObject")
Set resFile = fso.CreateTextFile("{res_str}", True)
resFile.WriteLine "STATUS:" & statusMsg
resFile.WriteLine "EXISTING:" & Trim(existingList)
resFile.WriteLine "ADDED:" & Trim(addedList)
resFile.WriteLine "POPUP:" & popupMsg
resFile.Close

' 6. Clean exit to Easy Access
session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0
'''

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(vbs_code)

    return script_path, result_path


def ensure_storage_locations(
    material: str,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    required_slocs: List[str] = None,
    batch_id: str = None,
    timeout: int = 30
) -> Dict[str, Any]:
    """
    Ensures that standard storage locations are configured for a given material in Plant P001.
    If missing, automatically enters them via MMSC, saves, and logs to SQLite DB.
    """
    from sap_launcher import ensure_sap_open, run_vbs_in_sap

    if not batch_id:
        batch_id = datetime.now().strftime("MMSC_%Y%m%d_%H%M%S")

    material = str(material).strip()
    logger.info(f"🔍 [MMSC CHECK] Verifying storage locations for Material: {material} (Plant: {plant})...")

    ensure_sap_open()

    vbs_path, result_path = generate_mmsc_vbs(material, plant=plant, required_slocs=required_slocs)

    output = run_vbs_in_sap(vbs_path, result_path, timeout=timeout)

    parsed_result = {
        "material": material,
        "plant": plant,
        "status": "FAILED",
        "existing_slocs": "",
        "added_slocs": "",
        "sap_response": "",
        "success": False
    }

    if result_path.exists():
        try:
            content = result_path.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.startswith("STATUS:"):
                    parsed_result["status"] = line.split(":", 1)[1].strip()
                elif line.startswith("EXISTING:"):
                    parsed_result["existing_slocs"] = line.split(":", 1)[1].strip()
                elif line.startswith("ADDED:"):
                    parsed_result["added_slocs"] = line.split(":", 1)[1].strip()
                elif line.startswith("POPUP:"):
                    popup = line.split(":", 1)[1].strip()
                    if popup:
                        parsed_result["sap_response"] += f"[Popup: {popup}] "

            if parsed_result["status"] in ["ALL_SLOCS_ALREADY_EXIST", "SUCCESS_SAVED", "Function carried out"]:
                parsed_result["success"] = True
                parsed_result["sap_response"] += parsed_result["status"]
            elif "carried out" in parsed_result["status"].lower():
                parsed_result["success"] = True
                parsed_result["sap_response"] += parsed_result["status"]
            else:
                parsed_result["sap_response"] += parsed_result["status"]
        except Exception as e_parse:
            logger.error(f"Error parsing MMSC result: {e_parse}")

    logger.info(f"   [MMSC RESULT] Status={parsed_result['status']} | Existing={parsed_result['existing_slocs']} | Added={parsed_result['added_slocs']}")

    # Log to SQLite DB
    try:
        from db_manager import log_mmsc_extension
        log_mmsc_extension(batch_id, parsed_result)
    except Exception as e_db:
        logger.warning(f"Could not log MMSC extension to DB: {e_db}")

    return parsed_result


def auto_heal_gi_error_slocs(gi_df: Optional[pd.DataFrame] = None, plant: str = _get_ops_env("SAP_PLANT", "P001"), batch_id: str = None) -> List[Dict[str, Any]]:
    """
    Scans GI error entries and automatically heals materials with "does not exist" errors.
    Returns list of extension results.
    """
    from gr_gi_resolver import parse_sap_utf16_list

    if gi_df is None or gi_df.empty:
        # Find latest GI error file
        files = sorted(glob.glob(str(GI_ERRORS_DIR / "*.XLS*")), key=os.path.getmtime, reverse=True)
        if not files:
            logger.warning("[MMSC HEAL] No GI error export files found in gi_errors.")
            return []
        latest_file = Path(files[0])
        gi_df = parse_sap_utf16_list(latest_file)

    if gi_df.empty or "Message" not in gi_df.columns:
        return []

    # Find rows with "does not exist"
    missing_loc_mask = gi_df["Message"].str.contains("does not exist", case=False, na=False)
    missing_rows = gi_df[missing_loc_mask]

    if missing_rows.empty:
        logger.info("[MMSC HEAL] No 'does not exist' storage location errors detected.")
        return []

    unique_materials = missing_rows["Material Number"].dropna().unique().tolist()
    logger.info(f"🚨 [MMSC HEAL] Found {len(unique_materials)} materials with missing storage locations: {unique_materials}")

    results = []
    for mat in unique_materials:
        res = ensure_storage_locations(mat, plant=plant, batch_id=batch_id)
        results.append(res)
        time.sleep(1.0)

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MMSC Storage Location Automated Maintenance Operator")
    parser.add_argument("--mat", type=str, help="Material number to verify and extend")
    parser.add_argument("--plant", type=str, default=_get_ops_env("SAP_PLANT", "P001"), help="Plant (default: P001)")
    parser.add_argument("--scan", action="store_true", help="Scan latest GI errors and auto-heal missing SLocs")
    parser.add_argument("--list", action="store_true", help="List recent MMSC extension history from DB")
    args = parser.parse_args()

    if args.list:
        from db_manager import get_recent_mmsc_extensions
        df = get_recent_mmsc_extensions(20)
        print("\n=== RECENT MMSC EXTENSIONS ===")
        if df.empty:
            print("No extension records found.")
        else:
            print(df.to_string(index=False))
    elif args.scan:
        healed = auto_heal_gi_error_slocs(plant=args.plant)
        print(f"\nCompleted auto-healing for {len(healed)} materials.")
    elif args.mat:
        res = ensure_storage_locations(args.mat, plant=args.plant)
        print("\n=== MMSC RESULT ===")
        for k, v in res.items():
            print(f"  {k}: {v}")
    else:
        parser.print_help()
