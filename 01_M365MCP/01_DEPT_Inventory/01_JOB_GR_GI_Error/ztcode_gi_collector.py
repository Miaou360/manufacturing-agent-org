"""
ZTCODE_GI_ERR Interface Error Collector (SAP GUI Automated Extractor)
===============================================================
01_Inventory_Team / 01_GR_GI_Error
- SAP GUI ZTCODE_STOCK_REC 트랜잭션을 실행하여 GR Domestics 및 Goods Issue 에러(Flag N)를
  실시간으로 자동 추출하여 gr_errors/ 및 gi_errors/ 에 누적 저장합니다.
"""

import os
import sys
import time
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
from typing import Optional, Dict

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
GR_DIR = BASE_DIR / "gr_errors"
GI_DIR = BASE_DIR / "gi_errors"
SCRATCH_DIR = BASE_DIR / "scratch"

GR_DIR.mkdir(parents=True, exist_ok=True)
GI_DIR.mkdir(parents=True, exist_ok=True)
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

from sap_launcher import ensure_sap_open, run_vbs_in_sap


def collect_ztcode_gi_err_errors(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    from_date: Optional[str] = None,
    to_date: Optional[str] = None
) -> Dict[str, Optional[Path]]:
    """
    Extracts both Goods Issue (Flag N) and GR Domestics (Flag N) from ZTCODE_STOCK_REC.
    Default date range: First day of current month (YYYY.MM.01) to Today (YYYY.MM.DD).
    """
    # 0. Ensure SAP GUI is open and logged in
    ensure_sap_open()

    today = datetime.now()
    today_str = today.strftime("%Y.%m.%d") if to_date is None else to_date
    from_date_str = today.replace(day=1).strftime("%Y.%m.%d") if from_date is None else from_date
    date_file_str = today.strftime("%Y%m%d")
    ts_file_str = today.strftime("%Y%m%d_%H%M%S")

    gi_target = GI_DIR / f"GI_Error_{date_file_str}.XLS"
    gr_target = GR_DIR / f"GR_Error_{date_file_str}.XLS"
    gi_ts_target = GI_DIR / f"GI_Error_{ts_file_str}.XLS"
    gr_ts_target = GR_DIR / f"GR_Error_{ts_file_str}.XLS"
    results = {"GI_Error": None, "GR_Error": None}

    # 1. Goods Issue Extraction (radRA3)
    def _do_extract_gi():
        extract_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        gi_temp = GI_DIR / f"GI_Error_temp_{extract_ts}.XLS"
        gi_vbs_path = SCRATCH_DIR / f"extract_gi_{extract_ts}.vbs"
        gi_res_path = SCRATCH_DIR / f"gi_done_{extract_ts}.txt"
        print(f"\n[ZTCODE_GI_ERR] 1/2 Extracting Goods Issue errors (Flag N) -> {gi_target.name}...")
        if gi_temp.exists():
            try:
                gi_temp.unlink()
            except Exception:
                pass

        vbs_gi = f'''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If

session.findById("wnd[0]/tbar[0]/okcd").text = "/nztcode_stock_rec"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 1500

session.findById("wnd[0]/usr/ctxtP_WERKS").text = "{plant}"
session.findById("wnd[0]/usr/txtP_FLAG").text = "N"
session.findById("wnd[0]/usr/ctxtS_CPUDT-LOW").text = "{from_date_str}"
session.findById("wnd[0]/usr/ctxtS_CPUDT-HIGH").text = "{today_str}"
session.findById("wnd[0]/usr/radRA3").select()

session.findById("wnd[0]").sendVKey 8
WScript.Sleep 3000

session.findById("wnd[0]/mbar/menu[0]/menu[3]/menu[2]").select()
WScript.Sleep 1500

session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").select()
session.findById("wnd[1]/tbar[0]/btn[0]").press()
WScript.Sleep 1500

session.findById("wnd[1]/usr/ctxtDY_PATH").text = "{str(GI_DIR.resolve())}"
session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "{gi_temp.name}"
session.findById("wnd[1]/tbar[0]/btn[11]").press()
WScript.Sleep 2500

On Error Resume Next
session.findById("wnd[2]/tbar[0]/btn[11]").press()

Dim fso, f
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.CreateTextFile("{str(gi_res_path.resolve())}", True)
f.WriteLine "OK"
f.Close
'''
        if gi_res_path.exists(): gi_res_path.unlink()
        with open(gi_vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_gi)

        run_vbs_in_sap(gi_vbs_path, gi_res_path, timeout=25)
        if gi_temp.exists() and gi_temp.stat().st_size > 0:
            import shutil
            shutil.copyfile(gi_temp, gi_target)
            shutil.copyfile(gi_temp, gi_ts_target)
            try:
                gi_temp.unlink()
            except Exception:
                pass
            print(f"         ✅ Goods Issue extracted successfully ({gi_target.stat().st_size:,} bytes | TS: {gi_ts_target.name})")
            return gi_target
        else:
            from collect_retry import RetryableCollectionError
            raise RetryableCollectionError("Goods Issue export file missing or empty")


    HQ_DIR = BASE_DIR.parent.parent / "00_HQ_TaskManager"
    if str(HQ_DIR) not in sys.path:
        sys.path.insert(0, str(HQ_DIR))

    try:
        from collect_retry import execute_with_retry
        gi_res, _ = execute_with_retry(
            _do_extract_gi,
            job_name="ZTCODE_GI_ERR_GI_Extract",
            time_budget_seconds=120.0,
            max_retries=2,
            backoff_delays=[5.0, 10.0]
        )
        results["GI_Error"] = gi_res
    except Exception as e_gi:
        print(f"         ⚠️ Goods Issue extraction failed or no data: {e_gi}")
        results["GI_Error"] = None

    # 2. GR Domestics & Import Extraction (radRA1 & radRA2)
    def _do_extract_gr():
        import pandas as pd
        import shutil
        from ztcode_stock_guard import parse_sap_utf16_list

        extract_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        gr_dom_temp = GR_DIR / f"GR_Dom_temp_{extract_ts}.XLS"
        gr_imp_temp = GR_DIR / f"GR_Imp_temp_{extract_ts}.XLS"
        gr_vbs_path = SCRATCH_DIR / f"extract_gr_{extract_ts}.vbs"
        gr_res_path = SCRATCH_DIR / f"gr_done_{extract_ts}.txt"
        print(f"\n[ZTCODE_GI_ERR] 2/2 Extracting GR errors (Domestics radRA1 & Import radRA2) -> {gr_target.name}...")
        for p in (gr_dom_temp, gr_imp_temp):
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

        vbs_gr = f'''If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If

Sub ExtractRadio(radId, outPath, outFileName)
    session.findById("wnd[0]/tbar[0]/okcd").text = "/nztcode_stock_rec"
    session.findById("wnd[0]").sendVKey 0
    WScript.Sleep 1500

    session.findById("wnd[0]/usr/ctxtP_WERKS").text = "{plant}"
    session.findById("wnd[0]/usr/txtP_FLAG").text = "N"
    session.findById("wnd[0]/usr/ctxtS_CPUDT-LOW").text = "{from_date_str}"
    session.findById("wnd[0]/usr/ctxtS_CPUDT-HIGH").text = "{today_str}"
    session.findById("wnd[0]/usr/" & radId).select()

    session.findById("wnd[0]").sendVKey 8
    WScript.Sleep 2500

    session.findById("wnd[0]/mbar/menu[0]/menu[3]/menu[2]").select()
    WScript.Sleep 1500

    session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]").select()
    session.findById("wnd[1]/tbar[0]/btn[0]").press()
    WScript.Sleep 1500

    session.findById("wnd[1]/usr/ctxtDY_PATH").text = outPath
    session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = outFileName
    session.findById("wnd[1]/tbar[0]/btn[11]").press()
    WScript.Sleep 2500

    On Error Resume Next
    session.findById("wnd[2]/tbar[0]/btn[11]").press()
    On Error GoTo 0
End Sub

ExtractRadio "radRA1", "{str(GR_DIR.resolve())}", "{gr_dom_temp.name}"
ExtractRadio "radRA2", "{str(GR_DIR.resolve())}", "{gr_imp_temp.name}"

Dim fso, f
Set fso = CreateObject("Scripting.FileSystemObject")
Set f = fso.CreateTextFile("{str(gr_res_path.resolve())}", True)
f.WriteLine "OK"
f.Close
'''
        if gr_res_path.exists(): gr_res_path.unlink()
        with open(gr_vbs_path, "w", encoding="utf-8") as f:
            f.write(vbs_gr)

        run_vbs_in_sap(gr_vbs_path, gr_res_path, timeout=40)

        dom_ok = gr_dom_temp.exists() and gr_dom_temp.stat().st_size > 0
        imp_ok = gr_imp_temp.exists() and gr_imp_temp.stat().st_size > 0

        # Strict Fail-Closed Guard (Claude review 072-1):
        # Both Domestic and Import extractions must succeed. If either fails, reject whole GR.
        if not (dom_ok and imp_ok):
            from collect_retry import RetryableCollectionError
            raise RetryableCollectionError(f"GR collection incomplete: Dom_OK={dom_ok}, Imp_OK={imp_ok} - Fail Closed")

        df_dom = parse_sap_utf16_list(gr_dom_temp)
        if not df_dom.empty:
            df_dom['GR_Type'] = 'DOM'

        df_imp = parse_sap_utf16_list(gr_imp_temp)
        if not df_imp.empty:
            df_imp['GR_Type'] = 'IMP'

        df_comb = pd.concat([df_dom, df_imp], ignore_index=True)

        cols = ['GR Date', 'Vendor', 'Material Number', 'Material Desc', 'Mvt', 'Stor', 'Qty', 'GR_Type']
        for target_file in (gr_target, gr_ts_target):
            with open(target_file, 'w', encoding='utf-16') as f:
                f.write('\t'.join(cols) + '\n')
                for _, r in df_comb.iterrows():
                    f.write('\t'.join(str(r.get(c, '')).strip() for c in cols) + '\n')

        # Archive individual domain files for traceability
        dom_archive = GR_DIR / f"GR_Domestics_{ts_file_str}.XLS"
        imp_archive = GR_DIR / f"GR_Import_{ts_file_str}.XLS"
        shutil.copyfile(gr_dom_temp, dom_archive)
        shutil.copyfile(gr_imp_temp, imp_archive)

        for p in (gr_dom_temp, gr_imp_temp):
            try:
                p.unlink()
            except Exception:
                pass

        print(f"         ✅ Unified GR extracted successfully ({len(df_comb)} rows: {len(df_dom)} DOM, {len(df_imp)} IMP | TS: {gr_ts_target.name})")
        return {
            "GR_Error": gr_target,
            "GR_Error_TS": gr_ts_target,
            "GR_Domestics": dom_archive,
            "GR_Import": imp_archive,
            "GR_Files": [gr_ts_target, dom_archive, imp_archive],
        }


    try:
        from collect_retry import execute_with_retry
        gr_res, _ = execute_with_retry(
            _do_extract_gr,
            job_name="ZTCODE_GI_ERR_GR_Extract",
            time_budget_seconds=120.0,
            max_retries=2,
            backoff_delays=[5.0, 10.0]
        )
        if isinstance(gr_res, dict):
            results["GR_Error"] = gr_res.get("GR_Error")
            results["GR_Error_TS"] = gr_res.get("GR_Error_TS")
            results["GR_Domestics"] = gr_res.get("GR_Domestics")
            results["GR_Import"] = gr_res.get("GR_Import")
            results["GR_Files"] = gr_res.get("GR_Files")
        else:
            results["GR_Error"] = gr_res
    except Exception as e_gr:
        print(f"         ⚠️ GR extraction failed or incomplete: {e_gr}")
        results["GR_Error"] = None
        results["GR_Domestics"] = None
        results["GR_Import"] = None
        results["GR_Files"] = None

    return results


if __name__ == "__main__":
    res = collect_ztcode_gi_err_errors(plant=_get_ops_env("SAP_PLANT", "P001"))
    print("\n[ZTCODE_GI_ERR Extraction Finished]:", res)
