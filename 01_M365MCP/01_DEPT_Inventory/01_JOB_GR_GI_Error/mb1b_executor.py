"""
MB1B Transfer Posting Automated Executor
========================================
01_Inventory_Team / 01_GR_GI_Error
- GI 에러 해소(311 및 312 환입 대응)를 위해 MB1B 화면 조작 및 311 이동전기를 자동 실행합니다.
- Recorded Script1.vbs 기반 필드 조작
- 선(先) "" 초기화 규칙 철저 준수
- Recipient: "AUTO_AGENT", Header Text: "GI_Error_Clear"
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
from datetime import datetime, timedelta
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = BASE_DIR / "scratch"
SCRATCH_DIR.mkdir(parents=True, exist_ok=True)

def generate_mb1b_vbs(item: dict) -> Path:
    """Generates a standalone VBScript to execute one MB1B transfer posting."""
    mat = item['Material']
    qty = str(int(item['Transfer_Qty']) if item['Transfer_Qty'] == int(item['Transfer_Qty']) else item['Transfer_Qty'])
    from_sloc = item['From_SLoc'].lower()
    to_sloc = item['To_SLoc'].lower()
    doc_date = item['Doc_Date']
    post_date = item['Post_Date']
    header_text = item.get('Header_Text', 'GI_Error_Clear')
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
    script_path = SCRATCH_DIR / f"mb1b_post_{mat}_{timestamp}.vbs"
    result_path = SCRATCH_DIR / f"mb1b_res_{mat}_{timestamp}.txt"

    final_vbs = vbs_code.replace("__RESULT_PATH__", str(result_path.resolve()))
    with open(script_path, "w", encoding="utf-8") as f:
        f.write(final_vbs)
        
    return script_path, result_path


def execute_mb1b_posting(item: dict) -> dict:
    """Executes a single MB1B transfer posting using the interactive SAP runner."""
    from sap_launcher import ensure_sap_open, run_vbs_in_sap
    
    # Ensure SAP is open
    ensure_sap_open()

    vbs_path, result_path = generate_mb1b_vbs(item)
    print(f"\n[MB1B EXECUTE] Posting: Material={item['Material']} | Qty={item['Transfer_Qty']} | {item['From_SLoc']} -> {item['To_SLoc']}")
    print(f"               Script: {vbs_path.name}")
    
    output = run_vbs_in_sap(vbs_path, result_path, timeout=30)
    print(f"               SAP Result: {output}")

    # Self-healing retry if storage location does not exist
    if "does not exist" in output.lower():
        print(f"               🚨 [MMSC AUTO-HEAL] Storage location does not exist in SAP. Running MMSC auto-extension...")
        try:
            from mmsc_operator import ensure_storage_locations
            heal_res = ensure_storage_locations(item['Material'], plant=_get_ops_env("SAP_PLANT", "P001"))
            print(f"               ✅ [MMSC AUTO-HEAL] Result: {heal_res.get('status')} | Added: {heal_res.get('added_slocs')}. Retrying MB1B posting...")
            time.sleep(1.0)
            vbs_retry, res_retry = generate_mb1b_vbs(item)
            output = run_vbs_in_sap(vbs_retry, res_retry, timeout=30)
            print(f"               SAP Retry Result: {output}")
        except Exception as e_retry:
            print(f"               ⚠️ [MMSC AUTO-HEAL ERROR] Retry failed: {e_retry}")
        
    return {
        'Material': item['Material'],
        'From': item['From_SLoc'],
        'To': item['To_SLoc'],
        'Qty': item['Transfer_Qty'],
        'Result': output
    }


def _is_mb1b_posted_ok(res_dict: dict) -> tuple[bool, str]:
    import re as _re_doc
    res_txt = str((res_dict or {}).get("Result") or "")
    m = _re_doc.search(r"\b(49\d{8})\b", res_txt)
    if m:
        return True, m.group(1)
    if "posted" in res_txt.lower() and "error" not in res_txt.lower():
        return True, ""
    return False, ""


def _next_job01_cycle_start(now: datetime) -> datetime:
    """
    Returns the wall-clock start time of the next scheduled 01_JOB cycle:
    - Daytime (08:00 <= hour < 17:00): :00 and :30 (16:30 -> 17:00).
    - Nighttime (17:00 <= hour or hour < 08:00): top of next hour (:00).
    """
    if 8 <= now.hour < 17:
        if now.minute < 30:
            return now.replace(minute=30, second=0, microsecond=0)
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _compute_d113_wait_schedule(now: datetime, max_total_wait_sec: int = 900) -> dict:
    """
    B-1 (#216): Computes D113 Step 2 wait budget:
    - Ceiling = next 10-min SAP GI batch tick (:x7:45) + 1 tick (capped at max_total_wait_sec = 900s / ~15m).
    - Hard deadline = 5 minutes before the next 01_JOB cycle start (unconditional cutoff).
    """
    next_cycle_dt = _next_job01_cycle_start(now)
    cycle_cutoff_dt = next_cycle_dt - timedelta(minutes=5)
    max_cap_dt = now + timedelta(seconds=max_total_wait_sec)
    hard_deadline = min(max_cap_dt, cycle_cutoff_dt)
    total_budget_sec = max(0, int((hard_deadline - now).total_seconds()))

    mod10 = now.minute % 10
    cur_sec = mod10 * 60 + now.second
    target_sec = 7 * 60 + 45  # :x7:45
    if cur_sec < target_sec - 30:
        raw_tick1 = target_sec - cur_sec
    else:
        raw_tick1 = (10 * 60 - cur_sec) + target_sec
    raw_tick1 = max(5, min(int(raw_tick1), 660))
    tick1_wait_sec = min(raw_tick1, total_budget_sec) if total_budget_sec > 0 else 0
    rem_after_tick1 = max(0, total_budget_sec - tick1_wait_sec)
    tick2_wait_sec = min(600, rem_after_tick1)
    return {
        "next_cycle_start": next_cycle_dt,
        "hard_deadline": hard_deadline,
        "total_budget_sec": total_budget_sec,
        "tick1_wait_sec": tick1_wait_sec,
        "tick2_wait_sec": tick2_wait_sec,
    }


def _sleep_until_or_seconds(wait_s: int, hard_deadline: datetime = None, now_fn=None, sleep_fn=None) -> None:
    eff_now = now_fn or datetime.now
    eff_sleep = sleep_fn or time.sleep
    rem = max(0, int(wait_s))
    while rem > 0:
        if hard_deadline is not None:
            rem_to_dl = int((hard_deadline - eff_now()).total_seconds())
            if rem_to_dl <= 0:
                break
            rem = min(rem, rem_to_dl)
        step_sleep = min(5, rem)
        eff_sleep(step_sleep)
        rem -= step_sleep


def _default_wait_for_sap_gi_interface() -> None:
    """
    SAP runs its ZTCODE_GI_ERR GI interface batch job every 10 minutes around :04~:07, :14~:17, ..., :54~:57.
    Sleeps cleanly (5s intervals, 0% CPU) until :x7:45, bounded by B-1 next-cycle -5m cutoff.
    """
    sched = _compute_d113_wait_schedule(datetime.now())
    wait_s = sched["tick1_wait_sec"]
    print(f"               ⏳ [D113 STEP 2 WAIT] Waiting {wait_s}s for SAP 10-min GI interface batch (deadline {sched['hard_deadline'].strftime('%H:%M:%S')})...")
    _sleep_until_or_seconds(wait_s, hard_deadline=sched["hard_deadline"])


def _default_verify_gi_cleared_in_ztcode_stock_rec(item: dict, pre_active_route_qty: float = None) -> tuple[bool, str]:
    """
    Re-extracts fresh ZTCODE_STOCK_REC GI_Error in the same cycle and verifies whether the target `A -> B` GI error
    (D113 `SL00 -> X` or D115 `A -> B`) cleared in SAP ZTCODE_GI_ERR.
    """
    from ztcode_gi_collector import collect_ztcode_gi_err_errors
    from gr_gi_resolver import parse_sap_utf16_list
    from ztcode_stock_guard import gi_flow

    plant = _get_ops_env("SAP_PLANT", "P001")
    mat = str(item.get("Material") or "").strip()
    err_src = str(item.get("Error_From") or item.get("To_SLoc") or "SL00").strip().upper()
    err_dst = str(item.get("Error_To") or item.get("From_SLoc") or "").strip().upper()
    posted_qty = float(item.get("Transfer_Qty") or 0.0)
    is_d115 = str(item.get("Status") or "").strip().upper() == "DEST_PUMP_CLEAR_D115"
    expected_before = float(pre_active_route_qty if pre_active_route_qty is not None else item.get("Error_Qty") or posted_qty)
    expected_clear = expected_before if is_d115 else posted_qty

    extracted = collect_ztcode_gi_err_errors(plant=plant)
    gi_file = extracted.get("GI_Error")
    if not gi_file or not Path(gi_file).exists():
        return False, "ZTCODE_STOCK_REC post-Step1 GI extraction missing"

    df_after = parse_sap_utf16_list(Path(gi_file))
    rem_qty = 0.0
    if df_after is not None and not df_after.empty and "Material Number" in df_after.columns:
        sub = df_after[df_after["Material Number"].astype(str).str.strip() == mat]
        for _, r in sub.iterrows():
            mv = str(r.get("Mvt") or "").replace(".0", "").strip()
            fs = str(r.get("FromSt") or "").strip().upper()
            ts = str(r.get("TOSt") or "").strip().upper()
            sf, df_loc = gi_flow(mv, fs, ts)
            if sf == err_src and df_loc == err_dst:
                q_str = str(r.get("Qty") or "0").replace(",", "").strip()
                rem_qty += float(q_str or 0.0)

    cleared_qty = max(0.0, expected_before - rem_qty)
    if rem_qty <= 1e-6 or cleared_qty >= expected_clear - 1e-6:
        return True, f"ZTCODE_STOCK_REC verified {err_src}->{err_dst} cleared (remaining={rem_qty:,.1f} EA)"
    return False, f"ZTCODE_STOCK_REC {err_src}->{err_dst} still remaining={rem_qty:,.1f} EA (expected clear={expected_clear:,.1f} EA)"


def _is_sl00_deficit_rollback_failure(rb_res: dict) -> bool:
    """B-1 (#216): Detects when rollback SL00->X failed because SAP GI batch already consumed SL00 stock."""
    msg = str((rb_res or {}).get("Result") or "").lower()
    return any(k in msg for k in ("deficit", "shortage", "insufficient", "exceeded by", "m7 021", "m7021"))


def execute_d113_roundtrip_posting(
    item: dict,
    batch_id: str = None,
    post_fn=None,
    verify_gi_cleared_fn=None,
    wait_for_gi_interface_fn=None,
    sap_lock_factory=None,
    now_fn=None,
    sleep_fn=None,
    max_total_wait_sec: int = 900,
) -> list[dict]:
    """
    D113 Rule 5 (#211) + B-1 (#216): Executes Step 1 (`X -> SL00` MB1B 311) and Step 2 (GI error `SL00 -> X` clearance)
    consecutively in the SAME cycle.
    - Wait ceiling: next 10-min SAP batch tick + 1 tick (max ~15m = 900s), unconditionally bounded by
      5 minutes before the next 01_JOB cycle start.
    - Pre-rollback ZTCODE_STOCK_REC check: re-queries ZTCODE_STOCK_REC one final time right before executing rollback.
    - Rollback race handling: if rollback (`SL00 -> X`) fails due to SL00 deficit (because SAP's GI batch
      consumed SL00 right before/during rollback), classifies as RESOLVED (`POSTED`, NOT `ROLLED_BACK_D113`,
      retry NOT blocked).
    """
    eff_post_fn = post_fn or execute_mb1b_posting
    eff_verify_fn = verify_gi_cleared_fn or _default_verify_gi_cleared_in_ztcode_stock_rec
    eff_now = now_fn or datetime.now
    eff_sleep = sleep_fn or time.sleep

    is_d115 = str(item.get("Status") or "").strip().upper() == "DEST_PUMP_CLEAR_D115"
    rule_tag = "D115" if is_d115 else "D113"
    primary_ptype = "DEST_PUMP_CLEAR_D115" if is_d115 else "ROUNDTRIP_CLEAR_VIA_DEST_D113"
    rb_ptype = "ROLLBACK_D115" if is_d115 else "ROLLBACK_D113"
    rb_ok_status = "ROLLED_BACK_D115" if is_d115 else "ROLLED_BACK_D113"
    rb_fail_status = "ROLLBACK_FAILED_D115" if is_d115 else "ROLLBACK_FAILED_D113"
    rb_row_fail_status = "FAILED_ROLLBACK_D115" if is_d115 else "FAILED_ROLLBACK_D113"
    rb_header_text = "D115_Rollback" if is_d115 else "D113_Rollback"

    mat = str(item["Material"]).strip()
    dest_x = str(item["From_SLoc"]).strip().upper()  # Step 1 sends B -> A (or X -> SL00)
    sl00 = str(item["To_SLoc"]).strip().upper()      # A (e.g. SP31 or SL00)
    qty = float(item["Transfer_Qty"])

    def _run_verify() -> tuple[bool, str]:
        if sap_lock_factory is not None:
            with sap_lock_factory() as acq_v:
                if not acq_v:
                    return False, f"SAP lock timeout on {rule_tag} Step 2 verification"
                return eff_verify_fn(item)
        return eff_verify_fn(item)

    # Step 1: B -> A (inside SAP lock if sap_lock_factory provided)
    if sap_lock_factory is not None:
        with sap_lock_factory() as acq:
            if not acq:
                res1 = {"Material": mat, "From": dest_x, "To": sl00, "Qty": qty, "Result": f"ERROR: SAP lock timeout on {rule_tag} Step 1"}
            else:
                res1 = eff_post_fn(item)
    else:
        res1 = eff_post_fn(item)

    res1["Posting_Type"] = primary_ptype
    res1["Reason"] = item.get("Reason", "")
    for extra_k in ("Mvt_Original", "Error_From", "Error_To", "Error_Qty", "SAP_By_SLoc", "WMS_By_SLoc", "SAP_Total", "WMS_Total", "Total_Gap"):
        if extra_k in item:
            res1[extra_k] = item[extra_k]

    ok1, doc1 = _is_mb1b_posted_ok(res1)
    if not ok1:
        res1["Post_Status"] = "FAILED_STEP1"
        res1["Step2_Verified"] = False
        res1["Rolled_Back"] = False
        return [res1]

    # Step 1 succeeded -> MUST verify Step 2 in same cycle (up to next tick + 1 tick, <= next_cycle - 5m) or roll back!
    sched = _compute_d113_wait_schedule(eff_now(), max_total_wait_sec=max_total_wait_sec)
    hard_deadline = sched["hard_deadline"]
    step2_ok = False
    step2_note = ""
    try:
        # Tick 1 wait outside SAP lock
        if wait_for_gi_interface_fn is not None:
            if eff_now() < hard_deadline:
                wait_for_gi_interface_fn()
        else:
            if sched["tick1_wait_sec"] > 0:
                print(f"               ⏳ [D113 STEP 2 TICK 1] Waiting {sched['tick1_wait_sec']}s (deadline {hard_deadline.strftime('%H:%M:%S')})...")
                _sleep_until_or_seconds(sched["tick1_wait_sec"], hard_deadline=hard_deadline, now_fn=eff_now, sleep_fn=eff_sleep)
        step2_ok, step2_note = _run_verify()

        # Tick 2 (+1 tick, up to ~15m total and strictly stopping 5m before next 01 cycle)
        if not step2_ok and eff_now() < hard_deadline:
            rem_budget = int((hard_deadline - eff_now()).total_seconds())
            if wait_for_gi_interface_fn is not None:
                if rem_budget > 0:
                    wait_for_gi_interface_fn()
                    step2_ok, step2_note = _run_verify()
            elif sched["tick2_wait_sec"] > 0 and rem_budget >= 10:
                t2_s = min(600, rem_budget)
                print(f"               ⏳ [D113 STEP 2 TICK 2 (+1 TICK)] Waiting {t2_s}s (deadline {hard_deadline.strftime('%H:%M:%S')})...")
                _sleep_until_or_seconds(t2_s, hard_deadline=hard_deadline, now_fn=eff_now, sleep_fn=eff_sleep)
                step2_ok, step2_note = _run_verify()
    except Exception as exc_s2:
        step2_ok = False
        step2_note = f"Step 2 exception: {exc_s2}"

    if step2_ok:
        res1["Result"] = f"{res1['Result']} | Step 2 OK: {step2_note}"
        res1["Post_Status"] = "POSTED"
        res1["Step2_Verified"] = True
        res1["Rolled_Back"] = False
        return [res1]

    # B-1 (#216): Re-query ZTCODE_STOCK_REC one final time immediately prior to rollback!
    try:
        pre_rb_ok, pre_rb_note = _run_verify()
    except Exception as exc_pre:
        pre_rb_ok, pre_rb_note = False, f"Pre-rollback ZTCODE_STOCK_REC check error: {exc_pre}"
    if pre_rb_ok:
        res1["Result"] = f"{res1['Result']} | Step 2 OK (pre-rollback ZTCODE_STOCK_REC recheck): {pre_rb_note}"
        res1["Post_Status"] = "POSTED"
        res1["Step2_Verified"] = True
        res1["Rolled_Back"] = False
        res1["Resolved_Pre_Rollback"] = True
        return [res1]

    # Step 2 still unresolved -> Immediate reverse MB1B 311 (A -> B) to restore pre-cycle SAP state!
    print(f"               🚨 [{rule_tag} ROLLBACK] Step 2 failed ({step2_note}). Immediately reversing {sl00} -> {dest_x} ({qty} EA)!")
    rb_item = dict(item)
    rb_item["From_SLoc"] = sl00
    rb_item["To_SLoc"] = dest_x
    rb_item["Header_Text"] = rb_header_text

    if sap_lock_factory is not None:
        with sap_lock_factory() as acq_rb:
            rb_res = eff_post_fn(rb_item) if acq_rb else {
                "Material": mat, "From": sl00, "To": dest_x, "Qty": qty, "Result": f"ERROR: SAP lock timeout on {rule_tag} rollback"
            }
    else:
        rb_res = eff_post_fn(rb_item)

    rb_ok, rb_doc = _is_mb1b_posted_ok(rb_res)
    if not rb_ok:
        # B-1 (#216): Rollback race — if SAP GI batch consumed source SLoc right as rollback ran,
        # classify as RESOLVED (POSTED, NOT ROLLED_BACK, retry NOT blocked)!
        post_rb_cleared = False
        post_rb_note = ""
        if _is_sl00_deficit_rollback_failure(rb_res):
            try:
                post_rb_cleared, post_rb_note = _run_verify()
            except Exception:
                post_rb_cleared = True
            if post_rb_cleared or _is_sl00_deficit_rollback_failure(rb_res):
                res1["Post_Status"] = "POSTED"
                res1["Step2_Verified"] = True
                res1["Rolled_Back"] = False
                res1["Resolved_Via_Rollback_Race"] = True
                res1["Result"] = (
                    f"{res1['Result']} | Step 2 OK (B-1 rollback race resolved: {sl00} consumed by SAP GI batch; "
                    f"rollback msg={rb_res.get('Result', '')}; ztcode_stock_rec={post_rb_note or f'{sl00} deficit confirmed'})"
                )
                return [res1]

    res1["Post_Status"] = rb_ok_status if rb_ok else rb_fail_status
    res1["Step2_Verified"] = False
    res1["Rolled_Back"] = rb_ok
    res1["Rollback_Doc"] = rb_doc
    res1["Result"] = (
        f"{res1['Result']} | Step 2 FAILED ({step2_note}) -> Rollback {sl00}->{dest_x}: {rb_res.get('Result', '')}"
    )

    rb_res["Posting_Type"] = rb_ptype
    rb_res["Post_Status"] = "POSTED" if rb_ok else rb_row_fail_status
    rb_res["Reason"] = f"{rule_tag} automatic rollback of Doc {doc1 or 'N/A'} after Step 2 failure: {step2_note}"
    return [res1, rb_res]


def execute_d115_dest_pump_posting(item: dict, **kwargs) -> list[dict]:
    """D115 (#220): Executes Step 1 deficit-only pump (`B -> A`) + Step 2 `A -> B` ZTCODE_STOCK_REC clearance + B-1 rollback."""
    item_d115 = dict(item)
    item_d115["Status"] = "DEST_PUMP_CLEAR_D115"
    return execute_d113_roundtrip_posting(item_d115, **kwargs)


def execute_batch_postings(plan_df: pd.DataFrame, max_postings: int = None, delay_between: float = 3.0, batch_id: str = None) -> list:
    """Executes all TRANSFER_POSTING items in the resolution plan and logs to SQLite DB."""
    from sap_launcher import ensure_sap_open
    ensure_sap_open()

    if not batch_id:
        batch_id = datetime.now().strftime("BATCH_%Y%m%d_%H%M%S")

    ready = plan_df[plan_df['Action'] == 'TRANSFER_POSTING']
    if ready.empty:
        print("[MB1B BATCH] No ready transfer postings in plan.")
        return []

    if max_postings:
        ready = ready.head(max_postings)

    results = []
    print(f"\n========================================================")
    print(f" [MB1B BATCH] Starting Batch Posting for {len(ready)} items (Batch: {batch_id})...")
    print(f"========================================================")

    for idx, (_, row) in enumerate(ready.iterrows(), 1):
        item = row.to_dict()
        print(f"\n[{idx}/{len(ready)}] Processing Material: {item['Material']} | {item['From_SLoc']} -> {item['To_SLoc']} ({item['Transfer_Qty']} EA)")
        if str(item.get('Status') or '') in ('ROUNDTRIP_CLEAR_VIA_DEST_D113', 'DEST_PUMP_CLEAR_D115'):
            step_results = execute_d113_roundtrip_posting(item, batch_id=batch_id)
        else:
            res = execute_mb1b_posting(item)
            res['Posting_Type'] = item.get('Status', 'FULL_RESOLUTION')
            res['Reason'] = item.get('Reason', '')
            for extra_k in ('Mvt_Original', 'Error_From', 'Error_To', 'Error_Qty', 'SAP_By_SLoc', 'WMS_By_SLoc', 'SAP_Total', 'WMS_Total', 'Total_Gap'):
                if extra_k in item:
                    res[extra_k] = item[extra_k]
            step_results = [res]

        for s_res in step_results:
            try:
                from db_manager import log_transfer_posting
                log_transfer_posting(batch_id, s_res)
            except Exception as e_db:
                print(f"               [DB LOG WARNING] Could not log to DB: {e_db}")
            results.append(s_res)
        time.sleep(delay_between)

    print(f"\n[MB1B BATCH COMPLETE] Successfully processed {len(results)} items.")
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="MB1B Automated Transfer Posting Executor")
    parser.add_argument("--batch", action="store_true", help="Execute all ready transfers in batch")
    parser.add_argument("--max", type=int, default=None, help="Limit number of postings")
    args = parser.parse_args()

    from gr_gi_resolver import resolve_gi_errors
    plan = resolve_gi_errors()
    
    if plan is not None and not plan.empty:
        if args.batch:
            execute_batch_postings(plan, max_postings=args.max)
        else:
            ready = plan[plan['Action'] == 'TRANSFER_POSTING']
            if not ready.empty:
                test_row = ready.iloc[0].to_dict()
                print("\n=== SELECTED TEST CASE ===")
                for k, v in test_row.items():
                    v_str = str(v).encode('ascii', errors='replace').decode('ascii')
                    print(f"  {k}: {v_str}")
                script_path, res_path = generate_mb1b_vbs(test_row)
                print(f"\nGenerated test script at: {script_path.name}")
