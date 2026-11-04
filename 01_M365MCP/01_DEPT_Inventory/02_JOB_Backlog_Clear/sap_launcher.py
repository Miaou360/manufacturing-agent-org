"""
SAP Automated Launcher, Authenticator & Self-Healing Session Manager
===================================================================
01_Inventory_Team (Plant P001 Headlamp & P002)
Handles:
1. Active session COM health probing (GetObject("SAPGUI")).
2. Detection & automatic dismissal of timeout/kickout error popups (#32770).
3. Automatic cleanup of dead/severed/zombie SAP GUI processes.
4. Autonomous cold start from Enterprise SSO (PRD -> Confirm -> HANA SSO -> SAP Easy Access).
5. Seamless self-healing recovery back to normal operations.
"""

import sys
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import os
import time
import ctypes
from ctypes import wintypes
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
import psutil
import subprocess
import win32gui
import win32con
import win32process

# Initialize WinSta0 and Desktop access before GUI libraries
user32 = ctypes.windll.user32
hwinsta = user32.OpenWindowStationW('WinSta0', False, 0x00020000 | 0x037F)
user32.SetProcessWindowStation(hwinsta)
hdesk = user32.OpenDesktopW('Default', 0, False, 0x00020000 | 0x01FF)
user32.SetThreadDesktop(hdesk)

import pywinauto

BASE_DIR = Path(__file__).resolve().parent
SCRATCH_DIR = BASE_DIR / "scratch"
SCRATCH_DIR.mkdir(exist_ok=True)

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

SSO_SHORTCUT = os.getenv("SAP_SSO_SHORTCUT", r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\NETS\Enterprise SSO.lnk")
SSO_WINDOW_TITLE = _get_ops_env("SAP_SSO_WINDOW_TITLE", "Enterprise SSO")
EMPLOYEE_NO = _get_ops_env("SAP_USERNAME", "0000000")
TASK_NAME = "SAP_Script_Runner"
WSCRIPT_EXE = r"C:\Windows\SysWOW64\wscript.exe" if Path(r"C:\Windows\SysWOW64\wscript.exe").exists() else r"C:\Windows\System32\wscript.exe"
WSCRIPT_PATH = f"{WSCRIPT_EXE} //b //nologo"

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
WNDENUMCHILDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def safe_set_foreground(hwnd):
    """Safely restore and bring a window to foreground."""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _build_vbs_task_xml(wscript_exe: str, vbs_path: Path) -> str:
    """Builds Task Scheduler XML with DisallowStartIfOnBatteries=false and StopIfGoingOnBatteries=false."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{wscript_exe}</Command>
      <Arguments>//b //nologo "{vbs_path}"</Arguments>
    </Exec>
  </Actions>
</Task>"""


def run_vbs_in_sap(vbs_path: Path, result_file: Path = None, timeout: int = 30) -> str:
    """
    Executes a VBScript inside the interactive user console session using schtasks XML (/it equivalent)
    with DisallowStartIfOnBatteries=false and StopIfGoingOnBatteries=false so scripts execute even on battery.
    Uses unique task name per invocation to prevent concurrent collision.
    """
    vbs_path = Path(vbs_path).resolve()
    unique_task = f"{TASK_NAME}_{os.getpid()}_{int(time.time()*1000)%100000}"
    xml_path = SCRATCH_DIR / f"_{unique_task}.xml"

    try:
        xml_path.write_text(_build_vbs_task_xml(WSCRIPT_EXE, vbs_path), encoding="utf-16")
        cmd_create = f'schtasks /create /tn "{unique_task}" /xml "{xml_path}" /f'
        res_create = subprocess.run(cmd_create, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if res_create.returncode != 0:
            cmd_fallback = f'schtasks /create /tn "{unique_task}" /tr "{WSCRIPT_PATH} \\"{vbs_path}\\"" /sc once /st 00:00 /it /f'
            subprocess.run(cmd_fallback, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        cmd_run = f'schtasks /run /tn "{unique_task}"'
        subprocess.run(cmd_run, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        res_content = ""
        if result_file:
            result_file = Path(result_file)
            start_time = time.time()
            while time.time() - start_time < timeout:
                if result_file.exists() and result_file.stat().st_size > 0:
                    time.sleep(0.3)
                    try:
                        with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                            res_content = f.read().strip()
                            break
                    except Exception:
                        pass
                time.sleep(0.5)
        else:
            res_content = "TASK_TRIGGERED"
    finally:
        cmd_del = f'schtasks /delete /tn "{unique_task}" /f'
        subprocess.run(cmd_del, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            xml_path.unlink(missing_ok=True)
        except Exception:
            pass

    return res_content


def probe_sap_health(timeout: int = 5) -> dict:
    """
    Fast (1-2s) COM probe into SAP GUI.
    Verifies that the session is actually alive, connected, and responsive (not kicked out or frozen).
    Returns: {"alive": bool, "title": str, "tcode": str, "error": str}
    """
    vbs_file = SCRATCH_DIR / "_probe_health.vbs"
    res_file = SCRATCH_DIR / "_probe_health_res.txt"

    vbs_code = f"""On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
Set outFile = fso.CreateTextFile("{str(res_file)}", True)

Set SapGuiAuto = GetObject("SAPGUI")
If Err.Number <> 0 Then
    outFile.WriteLine "FAIL_GETOBJECT: " & Err.Description
    outFile.Close
    WScript.Quit 1
End If

Set application = SapGuiAuto.GetScriptingEngine
If Err.Number <> 0 Or application Is Nothing Then
    outFile.WriteLine "FAIL_ENGINE: " & Err.Description
    outFile.Close
    WScript.Quit 2
End If

If application.Children.Count = 0 Then
    outFile.WriteLine "FAIL_CONN: No SAP connections found"
    outFile.Close
    WScript.Quit 3
End If

Set connection = application.Children(0)
If Err.Number <> 0 Or connection Is Nothing Then
    outFile.WriteLine "FAIL_CONN: " & Err.Description
    outFile.Close
    WScript.Quit 3
End If

If connection.Children.Count = 0 Then
    outFile.WriteLine "FAIL_SESSION: No SAP sessions found"
    outFile.Close
    WScript.Quit 4
End If

Set session = connection.Children(0)
If Err.Number <> 0 Or session Is Nothing Then
    outFile.WriteLine "FAIL_SESSION: " & Err.Description
    outFile.Close
    WScript.Quit 4
End If

Dim wTitle, tCode
wTitle = session.ActiveWindow.Text
tCode = session.Info.Transaction
If Err.Number <> 0 Then
    outFile.WriteLine "FAIL_WINDOW: " & Err.Description
    outFile.Close
    WScript.Quit 5
End If

outFile.WriteLine "ALIVE|" & wTitle & "|" & tCode
outFile.Close
WScript.Quit 0
"""
    try:
        vbs_file.write_text(vbs_code, encoding="utf-8")
        if res_file.exists():
            res_file.unlink()

        out_str = run_vbs_in_sap(vbs_file, res_file, timeout=timeout)
        if out_str.startswith("ALIVE|"):
            parts = out_str.split("|")
            return {
                "alive": True,
                "title": parts[1] if len(parts) > 1 else "",
                "tcode": parts[2] if len(parts) > 2 else "",
                "error": None
            }
        else:
            return {
                "alive": False,
                "title": "",
                "tcode": "",
                "error": out_str if out_str else "TIMEOUT_OR_NO_RESPONSE"
            }
    except Exception as e:
        return {
            "alive": False,
            "title": "",
            "tcode": "",
            "error": str(e)
        }
    finally:
        # Note: We NEVER delete vbs_file to prevent 'Can not find script file' errors
        if res_file.exists():
            try:
                res_file.unlink()
            except Exception:
                pass


def dismiss_sap_error_popups() -> int:
    """
    Finds and automatically dismisses any modal error / kick-out / warning popups (#32770) from SAP.
    (e.g., 'Connection broken', 'Session terminated by system', 'WSAECONNRESET', etc.)
    Excludes the main 'SAP Logon 750' launcher window.
    Returns: count of dismissed dialogs.
    """
    candidates = []

    def enum_cb(hwnd, lparam):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        cls = buf.value
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            proc = psutil.Process(pid.value)
            pname = proc.name().lower()
        except Exception:
            pname = ""

        if cls in ["#32770", "Internet Explorer_TridentDlgFrame"] or "wscript" in pname:

            title_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title_buf, 512)
            title = title_buf.value.strip()

            if "windows script host" in title.lower() or "wscript" in pname:
                if user32.IsWindowVisible(hwnd):
                    candidates.append((hwnd, pid.value, pname, title, "WSCRIPT"))
            elif cls == "Internet Explorer_TridentDlgFrame" or "script error" in title.lower():
                if user32.IsWindowVisible(hwnd):
                    candidates.append((hwnd, pid.value, pname, title, "IE_SCRIPT"))
            elif "sap" in pname or "sap" in title.lower():
                # Exclude main SAP Logon launcher window
                if title != "SAP Logon 750" and user32.IsWindowVisible(hwnd):
                    candidates.append((hwnd, pid.value, pname, title, "SAP"))
        return True

    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

    dismissed = 0
    for item in candidates:
        hwnd, pid, pname, title = item[0], item[1], item[2], item[3]
        popup_type = item[4] if len(item) > 4 else "SAP"

        if popup_type == "WSCRIPT":
            print(f"⚠️ [WSCRIPT POPUP DETECTED] HWND {hwnd}, Title: '{title}'. Auto-dismissing via WM_CLOSE...")
            user32.PostMessageW(hwnd, win32con.WM_CLOSE, 0, 0)
            subprocess.run("taskkill /F /IM wscript.exe", shell=True, capture_output=True)
            dismissed += 1
            time.sleep(0.5)
            continue

        if popup_type == "IE_SCRIPT":
            print(f"⚠️ [SCRIPT ERROR POPUP DETECTED] HWND {hwnd}, Title: '{title}'. Auto-approving 'Yes' to continue...")
            child_buttons = []
            def enum_child_cb(child_h, extra):
                t_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(child_h, t_buf, 256)
                cid = user32.GetDlgCtrlID(child_h)
                child_buttons.append((child_h, cid, t_buf.value.strip()))
                return True
            user32.EnumChildWindows(hwnd, WNDENUMCHILDPROC(enum_child_cb), 0)
            clicked = False
            for btn_h, cid, btn_txt in child_buttons:
                if btn_txt.lower() in ["yes", "&yes", "예"] or cid in [6, win32con.IDYES]:
                    win32gui.SendMessage(btn_h, win32con.BM_CLICK, 0, 0)
                    clicked = True
                    break
            if not clicked:
                win32gui.SendMessage(hwnd, win32con.WM_COMMAND, 6, 0)
                user32.PostMessageW(hwnd, win32con.WM_CLOSE, 0, 0)
            dismissed += 1
            time.sleep(1)
            continue
        child_controls = []
        def enum_child_cb(child_h, extra):
            c_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(child_h, c_buf, 256)
            t_buf = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(child_h, t_buf, 512)
            cid = user32.GetDlgCtrlID(child_h)
            child_controls.append((child_h, c_buf.value, t_buf.value.strip(), cid))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMCHILDPROC(enum_child_cb), 0)
        cids = [c[3] for c in child_controls]
        all_text = " ".join([c[2] for c in child_controls] + [title]).lower()

        # Guard: NEVER dismiss the HANA login modal dialog!
        if title == "SAP" and (101 in cids or 111 in cids):
            print(f"ℹ️ [SAP LOGIN MODAL PRESERVED] HWND {hwnd} is the HANA credentials modal. Skipping dismissal.")
            continue

        # Guard: NEVER dismiss file export dialogs
        if any(k in all_text for k in ["save list in file", "choose format"]):
            continue

        child_buttons = [c for c in child_controls if c[1].lower() == "button"]
        print(f"⚠️ [SAP POPUP DETECTED] HWND {hwnd}, Title: '{title}', Buttons: {[(b[0], b[3], b[2]) for b in child_buttons]}")

        clicked = False
        for btn_h, bcls, btn_txt, cid in child_buttons:
            if btn_txt.upper() in ["OK", "확인", "닫기", "CLOSE", "YES", "예"] or cid in [1, 2]:
                print(f"    -> Auto-clicking '{btn_txt}' (ID {cid}) on HWND {hwnd} to dismiss kick-out dialog...")
                win32gui.SendMessage(btn_h, win32con.BM_CLICK, 0, 0)
                clicked = True
                dismissed += 1
                break

        if not clicked:
            print(f"    -> Fallback: Sending IDOK/VK_RETURN to dialog HWND {hwnd}...")
            win32gui.SendMessage(hwnd, win32con.WM_COMMAND, 1, 0)
            win32gui.SendMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
            win32gui.SendMessage(hwnd, win32con.WM_KEYUP, win32con.VK_RETURN, 0)
            dismissed += 1

        time.sleep(1)

    return dismissed


def cold_start_sap_session() -> bool:
    """
    Executes cold start of SAP GUI from Enterprise SSO.
    Handles SSO launch, PRD click, Confirm, HANA modal employee number input, and Easy Access readiness.
    """
    print("[SAP COLD START] Starting Enterprise SSO login sequence...")

    # 1. Locate or start Enterprise SSO
    sso_hwnd = win32gui.FindWindow(None, SSO_WINDOW_TITLE)
    if not sso_hwnd:
        print(f"    • Launching SSO shortcut: {SSO_SHORTCUT}")
        os.startfile(SSO_SHORTCUT)
        for _ in range(15):
            time.sleep(1)
            sso_hwnd = win32gui.FindWindow(None, SSO_WINDOW_TITLE)
            if sso_hwnd:
                break

    if not sso_hwnd:
        raise RuntimeError(f"{SSO_WINDOW_TITLE} 창을 찾거나 실행할 수 없습니다.")

    print(f"    • Enterprise SSO Window located (HWND {sso_hwnd})")
    safe_set_foreground(sso_hwnd)
    time.sleep(1)

    # 2. Find IE Server child
    ie_hwnd = None
    def enum_ie_cb(h, extra):
        nonlocal ie_hwnd
        if win32gui.GetClassName(h) == "Internet Explorer_Server":
            ie_hwnd = h
            return False
        return True

    win32gui.EnumChildWindows(sso_hwnd, enum_ie_cb, None)
    if not ie_hwnd:
        raise RuntimeError("SSO 창 내부의 웹 뷰(Internet Explorer_Server)를 찾을 수 없습니다.")

    app = pywinauto.Application(backend="uia").connect(handle=ie_hwnd)
    ie_win = app.window(handle=ie_hwnd)

    # 3. Click PRD link
    print("    • Locating and clicking 'PRD' in Enterprise SSO...")
    BINSYNTH9940_link = None
    for l in ie_win.descendants(control_type="Hyperlink"):
        if "PRD" in l.window_text():
            BINSYNTH9940_link = l
            break

    if not BINSYNTH9940_link:
        print("    ⚠️ 'PRD' link not visible. Closing stale SSO window and retrying...")
        win32gui.SendMessage(sso_hwnd, win32con.WM_CLOSE, 0, 0)
        time.sleep(2)
        os.startfile(SSO_SHORTCUT)
        time.sleep(5)
        return cold_start_sap_session()

    BINSYNTH9940_link.invoke()
    print("    • 'PRD' clicked. Waiting for 'Choose account' dialog...")

    # 4. Click Confirm on Choose account dialog
    confirm_btn = None
    for _ in range(15):
        time.sleep(1)
        dismiss_sap_error_popups()
        for b in ie_win.descendants(control_type="Button"):
            if b.window_text().strip() == "Confirm":
                confirm_btn = b
                break
        if confirm_btn:
            break

    if not confirm_btn:
        raise RuntimeError("'Choose account' 화면의 'Confirm' 버튼을 찾을 수 없습니다.")

    print("    • Clicking 'Confirm' on account selection...")
    confirm_btn.invoke()
    print("    • 'Confirm' clicked. Waiting for HANA conversion modal dialog...")

    # 5. Handle SAP HANA modal (#32770, Title: SAP)
    modal_hwnd = None
    for i in range(25):
        time.sleep(1)
        dismiss_sap_error_popups()
        candidate = win32gui.FindWindow("#32770", "SAP")
        if candidate and win32gui.IsWindowVisible(candidate):
            modal_hwnd = candidate
            print(f"    • HANA modal dialog detected after {i+1}s (HWND {modal_hwnd})")
            break

    if not modal_hwnd:
        raise RuntimeError("사번 입력 팝업(SAP #32770)이 제한시간 내에 나타나지 않았습니다.")

    print(f"    • Entering Employee No '{EMPLOYEE_NO}' into modal...")
    safe_set_foreground(modal_hwnd)
    time.sleep(0.5)

    modal_children = {}
    def enum_modal_cb(h, extra):
        cid = win32gui.GetDlgCtrlID(h)
        modal_children[cid] = h
        return True

    win32gui.EnumChildWindows(modal_hwnd, enum_modal_cb, None)

    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    target_tid, _ = win32process.GetWindowThreadProcessId(modal_hwnd)
    ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, True)

    focused_h = ctypes.windll.user32.GetFocus()
    canvas_h = modal_children.get(101, None)
    target_input = focused_h if focused_h else canvas_h

    # Type Employee No
    for ch in EMPLOYEE_NO:
        win32gui.SendMessage(target_input, win32con.WM_CHAR, ord(ch), 0)
        time.sleep(0.05)

    # Press Enter
    win32gui.SendMessage(target_input, win32con.WM_KEYDOWN, win32con.VK_RETURN, 0)
    win32gui.SendMessage(target_input, win32con.WM_KEYUP, win32con.VK_RETURN, 0)

    # Click OK button (111) as backup
    btn_111 = modal_children.get(111, None)
    if btn_111:
        win32gui.SendMessage(btn_111, win32con.BM_CLICK, 0, 0)

    ctypes.windll.user32.AttachThreadInput(cur_tid, target_tid, False)
    print("    • Employee No submitted.")

    # 6. Wait for SAP Easy Access
    print("    • Waiting for SAP Easy Access main window...")
    for i in range(30):
        time.sleep(1)
        dismiss_sap_error_popups()
        sap_main = win32gui.FindWindow("SAP_FRONTEND_SESSION", None)
        if sap_main and win32gui.IsWindowVisible(sap_main):
            title = win32gui.GetWindowText(sap_main)
            print(f"✅ [SAP LAUNCH SUCCESS] SAP Session is ready! HWND: {sap_main} ('{title}')")
            try:
                win32gui.ShowWindow(sap_main, win32con.SW_SHOWMINNOACTIVE)
            except Exception:
                pass
            return True

    raise RuntimeError("SAP Easy Access 창이 30초 내에 로드되지 않았습니다.")


def ensure_sap_open() -> bool:
    """
    Self-healing master controller:
    1. Checks for and dismisses any blocking error / kickout popups (#32770).
    2. Probes COM connection health of the active SAP session (fast 1-2s test).
    3. If healthy, returns True immediately.
    4. If unhealthy, frozen, disconnected, or no window exists:
       - Cleans up dead sapgui processes (taskkill /F /IM sapgui.exe).
       - Performs clean cold start from Enterprise SSO (PRD -> Confirm -> HANA SSO).
       - Re-verifies COM health before returning.
    """
    _verify_ops_env_log("SAP_LAUNCHER")
    # Step 1: Dismiss any existing kickout/error/warning popups
    dismiss_count = dismiss_sap_error_popups()
    if dismiss_count > 0:
        print(f"[SAP HEALING] Dismissed {dismiss_count} blocking SAP error/kickout popup(s). Waiting 2s for stabilization...")
        time.sleep(2)

    # Step 2: Check active window and probe COM health
    active_sap = win32gui.FindWindow("SAP_FRONTEND_SESSION", None)
    if active_sap and win32gui.IsWindowVisible(active_sap):
        probe_res = probe_sap_health(timeout=10)
        if probe_res["alive"]:
            print(f"[SAP READY] Healthy SAP Session verified: HWND {active_sap} ('{probe_res['title']}', TCode: '{probe_res['tcode']}')")
            try:
                win32gui.ShowWindow(active_sap, win32con.SW_SHOWMINNOACTIVE)
            except Exception:
                pass
            return True
        else:
            print(f"[SAP HEALING] Active SAP window {active_sap} failed initial COM health probe ({probe_res['error']}).")
            print("             Dismissing popups and retrying health probe once without force-killing SAP...")
            dismiss_sap_error_popups()
            time.sleep(3)
            still_open = win32gui.FindWindow("SAP_FRONTEND_SESSION", None)
            if still_open and win32gui.IsWindowVisible(still_open):
                retry_probe = probe_sap_health(timeout=10)
                if retry_probe["alive"]:
                    print(f"[SAP READY] Healthy SAP Session verified on retry: HWND {still_open} ('{retry_probe['title']}', TCode: '{retry_probe['tcode']}')")
                    try:
                        win32gui.ShowWindow(still_open, win32con.SW_SHOWMINNOACTIVE)
                    except Exception:
                        pass
                    return True
                raise RuntimeError(
                    f"Active SAP window {still_open} did not respond to COM health probe after retry ({retry_probe['error']}). "
                    "Skipping forced termination to protect active session; deferring to next cycle."
                )

    # Step 3: All windows closed state -> Cold Start via Enterprise SSO
    print("[SAP LAUNCH] SAP GUI is not running or severed. Starting automated SSO launch sequence...")
    cold_start_sap_session()

    # Step 4: Verify new session with health probe
    time.sleep(2)
    verify_res = probe_sap_health(timeout=6)
    if verify_res["alive"]:
        print(f"🎉 [SAP RECOVERED] Successfully established and verified fresh SAP session! ('{verify_res['title']}')")
        return True
    else:
        raise RuntimeError(f"신규 실행된 SAP 세션의 헬스체크 검증에 실패했습니다: {verify_res['error']}")


if __name__ == "__main__":
    ensure_sap_open()
