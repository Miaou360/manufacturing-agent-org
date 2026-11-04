"""
Enterprise SAP Universal Self-Healing Engine & Guard
====================================================
00_HQ_TaskManager / sap_healer.py
Author: ACME Manufacturing HQ TaskManager Standard

전사(01_M365MCP) 모든 부서 및 JOB에서 SAP GUI 자동화 작업을 수행할 때
예상치 못한 모달 팝업, 보안 권한 경고, 세션 단절, 파일 중복 충돌을
원천 예방하고 자가치유(Auto-Healing)하는 전사 공통 가드레일 모듈입니다.

주요 핵심 기능:
1. Zero-Collision File Naming: 고유 타임스탬프(_YYYYMMDD_HHMMSS) 기반 파일명 생성
2. Proactive Security Whitelisting: saprules.xml 자동 화이트리스트 주입 및 Deny 규칙 자동 소거
3. Active Watchdog & Self-Healing:
   - SAP GUI Security 'Remember Decision + Allow' 자동 클릭
   - SAP GUI Security 'Rule Denied' 알림 자동 해소 및 saprules 패치
   - SAP 내부 안내/확인(Information/Warning) 모달 자동 돌파
   - 세션 끊김/킥아웃(#32770) 감지 및 복구
4. Blackbox Diagnostic & Learning:
   - 미지의 신규 팝업 발생 시 화면 스크린샷 캡처 및 컨트롤 텍스트 JSON 저장
"""

import os
import sys
import time
import json
import shutil
import ctypes
from ctypes import wintypes
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32gui
import win32con
import win32process
import psutil
from PIL import ImageGrab

# Initialize WinSta0 and Desktop access before GUI calls
user32 = ctypes.windll.user32
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
WNDENUMCHILDPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

def _attach_desktop():
    """Attaches background Python process to interactive WinSta0/Default desktop."""
    try:
        hwinsta = user32.OpenWindowStationW('WinSta0', False, 0x00020000 | 0x037F)
        user32.SetProcessWindowStation(hwinsta)
        hdesk = user32.OpenDesktopW('Default', 0, False, 0x00020000 | 0x01FF)
        user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

_attach_desktop()

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports" / "popups"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_JSON = REPORTS_DIR / "popup_history.json"

TASK_NAME = "SAP_Script_Runner"
WSCRIPT_PATH = r"C:\Windows\SysWOW64\wscript.exe //b //nologo" if Path(r"C:\Windows\SysWOW64\wscript.exe").exists() else r"C:\Windows\System32\wscript.exe //b //nologo"


# ==============================================================================
# 1. Zero-Collision File Naming (_YYYYMMDD_HHMMSS)
# ==============================================================================

def get_safe_timestamped_path(
    target_dir: Path,
    base_prefix: str,
    ext: str = ".XLS"
) -> Tuple[Path, Path]:
    """
    Generates a unique timestamped filename (_YYYYMMDD_HHMMSS) to eliminate SAP 
    'File already exists / Replace' popups completely.
    
    Returns:
        (timestamped_path, latest_canonical_path)
    Example:
        timestamped_path: .../GI_Error_20261023_173000.XLS
        latest_canonical_path: .../GI_Error_20261023.XLS
    """
    target_dir = Path(target_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    ts_str = now.strftime("%Y%m%d_%H%M%S")
    date_str = now.strftime("%Y%m%d")

    clean_prefix = base_prefix.rstrip("_")
    clean_ext = ext if ext.startswith(".") else f".{ext}"

    timestamped_file = target_dir / f"{clean_prefix}_{ts_str}{clean_ext}"
    latest_file = target_dir / f"{clean_prefix}_{date_str}{clean_ext}"

    return timestamped_file, latest_file


# ==============================================================================
# 2. Proactive SAP Rules Whitelisting (saprules.xml)
# ==============================================================================

def ensure_sap_rules_whitelist(extra_paths: Optional[List[str]] = None) -> bool:
    """
    Proactively checks and patches %APPDATA%/SAP/Common/saprules.xml.
    - Eradicates any Action=2 (Deny) rules targeting workspace files.
    - Registers all project directories under Action=0 (Allow).
    """
    try:
        import xml.etree.ElementTree as ET
        xml_path = os.path.expandvars(r"%APPDATA%\SAP\Common\saprules.xml")
        if not os.path.exists(xml_path):
            return False

        # Backup once if not backed up
        bak_path = xml_path + ".bak"
        if not os.path.exists(bak_path):
            shutil.copyfile(xml_path, bak_path)

        tree = ET.parse(xml_path)
        root = tree.getroot()
        rules_elem = root.find("rules")
        if rules_elem is None:
            return False

        modified = False

        # 1. Remove any rule with Deny action (<action>2</action>) on user directories
        for r in list(rules_elem.findall("rule")):
            ctx_actions = [c.find("action").text for c in r.findall(".//contexts/context") if c.find("action") is not None]
            if "2" in ctx_actions:
                # Check if it targets Desktop / CODES
                names = [e.text for e in r.findall(".//files/name") + r.findall(".//directories/name")]
                if any("CODES" in str(n) or "Desktop" in str(n) for n in names):
                    rules_elem.remove(r)
                    modified = True

        # 2. Ensure rule 36 or allow rule has all workspace paths
        _codes_root = Path(__file__).resolve().parents[2].as_posix()
        _job01_root = f"{_codes_root}/01_M365MCP/01_DEPT_Inventory/01_JOB_GR_GI_Error"
        standard_paths = [
            f"{_job01_root}/gi_errors/",
            f"{_job01_root}/gr_errors/",
            f"{_job01_root}/mb52/",
            f"{_job01_root}/scratch/",
            f"{_codes_root}/"
        ]
        if extra_paths:
            for p in extra_paths:
                p_norm = str(Path(p).resolve()).replace("\\", "/")
                if not p_norm.endswith("/"): p_norm += "/"
                if p_norm not in standard_paths:
                    standard_paths.append(p_norm)

        # Look for existing rule covering CODES
        target_rule = None
        for r in rules_elem.findall("rule"):
            dirs = [d.text for d in r.findall(".//directories/name")]
            if any("CODES" in str(d) for d in dirs):
                target_rule = r
                break

        if target_rule is not None:
            dirs_elem = target_rule.find("directories")
            if dirs_elem is not None:
                existing = {d.text for d in dirs_elem.findall("name")}
                for sp in standard_paths:
                    if sp not in existing:
                        d_elem = ET.SubElement(dirs_elem, "name")
                        d_elem.text = sp
                        modified = True
        else:
            # Create a new whitelist rule
            new_id = str(max([int(r.attrib.get("id", 0)) for r in rules_elem.findall("rule")] + [50]) + 1)
            new_r = ET.SubElement(rules_elem, "rule", attrib={"id": new_id})
            dirs_elem = ET.SubElement(new_r, "directories")
            for sp in standard_paths:
                d_elem = ET.SubElement(dirs_elem, "name")
                d_elem.text = sp
            perm_elem = ET.SubElement(new_r, "permissions")
            perm_elem.text = "w"
            act_elem = ET.SubElement(new_r, "action")
            act_elem.text = "3"
            ctxs = ET.SubElement(new_r, "contexts")
            ctx = ET.SubElement(ctxs, "context")
            ET.SubElement(ctx, "system").text = "PRD"
            ET.SubElement(ctx, "network")
            ET.SubElement(ctx, "client").text = "100"
            ET.SubElement(ctx, "transaction").text = "*"
            ET.SubElement(ctx, "dynpro_name").text = "*"
            ET.SubElement(ctx, "dynpro_num").text = "*"
            ET.SubElement(ctx, "permissions").text = "w"
            ET.SubElement(ctx, "action").text = "0"
            modified = True

        if modified:
            tree.write(xml_path, encoding="utf-8", xml_declaration=True)
            return True
        return False
    except Exception as e:
        print(f"⚠️ [SAP HEALER] Whitelist check warning: {e}")
        return False


# ==============================================================================
# 3. Active Watchdog & Self-Healing Core
# ==============================================================================

def heal_sap_popups(capture_unknown: bool = True) -> List[Dict[str, Any]]:
    """
    Scans the interactive desktop for blocking SAP dialogs/popups.
    Automatically classifies, dismisses, and logs them.
    Returns: List of healing action records.
    """
    _attach_desktop()
    healed_records = []

    candidates = []

    def enum_cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            txt = win32gui.GetWindowText(hwnd).strip()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                proc = psutil.Process(pid.value)
                pname = proc.name().lower()
            except Exception:
                pname = ""

            # Check if popup belongs to SAP, SSO, WScript or is a modal dialog
            if cls in ["#32770", "Internet Explorer_TridentDlgFrame"] or any(k in pname for k in ["sap", "nets", "nsso", "wscript"]) or any(k in txt.lower() for k in ["script error", "windows script host"]):
                if txt != "SAP Logon 750" and cls != "SAP_FRONTEND_SESSION":
                    candidates.append((hwnd, pid.value, pname, cls, txt))
        return True

    user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

    for hwnd, pid, pname, cls, title in candidates:
        children = []

        def child_cb(ch, _):
            ch_cls = win32gui.GetClassName(ch)
            ch_txt = win32gui.GetWindowText(ch).strip()
            cid = user32.GetDlgCtrlID(ch)
            children.append((ch, ch_cls, ch_txt, cid))
            return True

        user32.EnumChildWindows(hwnd, WNDENUMCHILDPROC(child_cb), 0)
        all_text = " ".join([c[2] for c in children] + [title]).lower()

        # ----------------------------------------------------------------------
        # Case A: SAP GUI Security - Path Permission Request (Allow / Deny)
        # ----------------------------------------------------------------------
        if "security" in title.lower() and ("trying to create" in all_text or "grant the permission" in all_text):
            print(f"⚡ [SELF-HEALING] Detected SAP GUI Security Permission Dialog (HWND {hwnd}). Auto-authorizing...")
            # 1. Check 'Remember My Decision' checkbox
            for ch, ch_cls, ch_txt, cid in children:
                if "remember" in ch_txt.lower():
                    win32gui.SendMessage(ch, win32con.BM_SETCHECK, win32con.BST_CHECKED, 0)
            # 2. Click 'Allow'
            for ch, ch_cls, ch_txt, cid in children:
                if ch_txt.lower() in ["allow", "&allow"]:
                    win32gui.SendMessage(ch, win32con.BM_CLICK, 0, 0)
                    rec = {"action": "HEALED_SECURITY_ALLOW", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
                    healed_records.append(rec)
                    _log_popup_history(rec)
                    break
            continue

        # ----------------------------------------------------------------------
        # Case B: SAP GUI Security - Denied Rule Notification
        # ----------------------------------------------------------------------
        if "security" in title.lower() and "denied based on a rule" in all_text:
            print(f"⚡ [SELF-HEALING] Detected SAP Security Denied Notification (HWND {hwnd}). Auto-dismissing & patching rules...")
            # Click 'No' to dismiss
            for ch, ch_cls, ch_txt, cid in children:
                if ch_txt.lower() in ["no", "&no"]:
                    win32gui.SendMessage(ch, win32con.BM_CLICK, 0, 0)
                    break
            # Automatically patch saprules.xml
            ensure_sap_rules_whitelist()
            rec = {"action": "HEALED_SECURITY_DENIED_NOTIFICATION", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
            healed_records.append(rec)
            _log_popup_history(rec)
            continue

        # ----------------------------------------------------------------------
        # Case C: SAP GUI Security - View Requests / Details
        # ----------------------------------------------------------------------
        if "view requests" in title.lower() or "request details" in title.lower():
            print(f"⚡ [SELF-HEALING] Closing auxiliary security dialog (HWND {hwnd})...")
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            rec = {"action": "HEALED_SECURITY_VIEW_REQUESTS", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
            healed_records.append(rec)
            _log_popup_history(rec)
            continue

        # ----------------------------------------------------------------------
        # Case D: Kickout / Session Broken / Connection Terminated
        # ----------------------------------------------------------------------
        if any(k in all_text for k in ["connection broken", "session terminated", "wsaeconnreset", "session cancelled"]):
            print(f"⚡ [SELF-HEALING] Detected Session Kickout Dialog (HWND {hwnd}, Title: '{title}'). Auto-dismissing...")
            for ch, ch_cls, ch_txt, cid in children:
                if ch_txt.upper() in ["OK", "확인", "CLOSE", "닫기"] or cid in [1, 2]:
                    win32gui.SendMessage(ch, win32con.BM_CLICK, 0, 0)
                    rec = {"action": "HEALED_KICKOUT_POPUP", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
                    healed_records.append(rec)
                    _log_popup_history(rec)
                    break
            continue

        # ----------------------------------------------------------------------
        # Case E: Enterprise SSO / Internet Explorer - Script Error Dialog
        # ----------------------------------------------------------------------
        if cls == "Internet Explorer_TridentDlgFrame" or "script error" in title.lower():
            print(f"⚡ [SELF-HEALING] Detected Script Error Dialog (HWND {hwnd}). Auto-approving (Yes) to continue...")
            clicked_yes = False
            for ch, ch_cls, ch_txt, cid in children:
                if ch_txt.lower() in ["yes", "&yes", "예"] or cid in [6, win32con.IDYES]:
                    win32gui.SendMessage(ch, win32con.BM_CLICK, 0, 0)
                    clicked_yes = True
                    break
            if not clicked_yes:
                win32gui.SendMessage(hwnd, win32con.WM_COMMAND, 6, 0)  # IDYES
                user32.PostMessageW(hwnd, win32con.WM_CLOSE, 0, 0)
            rec = {"action": "HEALED_SCRIPT_ERROR_APPROVED_YES", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
            healed_records.append(rec)
            _log_popup_history(rec)
            continue

        # ----------------------------------------------------------------------
        # Case F: Standard Information / Warning Modal Dialogs
        # ----------------------------------------------------------------------
        if title.lower() in ["information", "warning", "hinweis", "sap"]:
            has_ok = False
            for ch, ch_cls, ch_txt, cid in children:
                if ch_txt.upper() in ["OK", "확인", "YES", "예"] or cid == 1:
                    print(f"⚡ [SELF-HEALING] Passing through Information/Warning dialog (HWND {hwnd}, Title: '{title}')...")
                    win32gui.SendMessage(ch, win32con.BM_CLICK, 0, 0)
                    has_ok = True
                    rec = {"action": "HEALED_INFO_MODAL", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
                    healed_records.append(rec)
                    _log_popup_history(rec)
                    break
            if has_ok:
                continue

        # ----------------------------------------------------------------------
        # Case G: Windows Script Host Error Popups
        # ----------------------------------------------------------------------
        if "windows script host" in title.lower() or pname in ["wscript.exe", "cscript.exe"]:
            print(f"⚡ [SELF-HEALING] Detected Windows Script Host popup (HWND {hwnd}). Auto-dismissing via WM_CLOSE...")
            user32.PostMessageW(hwnd, win32con.WM_CLOSE, 0, 0)
            subprocess.run("taskkill /F /IM wscript.exe", shell=True, capture_output=True)
            rec = {"action": "HEALED_WSCRIPT_POPUP", "hwnd": hwnd, "title": title, "time": datetime.now().isoformat()}
            healed_records.append(rec)
            _log_popup_history(rec)
            continue

        # ----------------------------------------------------------------------
        # Case H: Unknown Blocking Popup -> Blackbox Diagnostic Capture
        # ----------------------------------------------------------------------
        if capture_unknown:
            print(f"📸 [BLACKBOX CAPTURE] Unhandled popup detected (HWND {hwnd}, Title: '{title}'). Recording diagnostic...")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shot_path = REPORTS_DIR / f"unknown_popup_{ts}_{hwnd}.png"
            try:
                rect = win32gui.GetWindowRect(hwnd)
                # Expand slightly
                bbox = (max(0, rect[0] - 10), max(0, rect[1] - 10), rect[2] + 10, rect[3] + 10)
                img = ImageGrab.grab(bbox=bbox)
                img.save(str(shot_path))
            except Exception:
                shot_path = None

            child_meta = [{"class": c[1], "text": c[2], "id": c[3]} for c in children]
            rec = {
                "action": "UNKNOWN_POPUP_CAPTURED",
                "hwnd": hwnd,
                "title": title,
                "pname": pname,
                "screenshot": str(shot_path) if shot_path else None,
                "controls": child_meta,
                "time": datetime.now().isoformat()
            }
            healed_records.append(rec)
            _log_popup_history(rec)

    return healed_records


def _log_popup_history(record: Dict[str, Any]):
    """Appends diagnostic record to popup_history.json."""
    try:
        history = []
        if HISTORY_JSON.exists():
            try:
                with open(HISTORY_JSON, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(record)
        # Keep latest 200 records
        if len(history) > 200:
            history = history[-200:]
        with open(HISTORY_JSON, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ==============================================================================
# 4. Self-Healing VBS Script Executor with Watchdog
# ==============================================================================

def run_healed_sap_vbs(
    vbs_path: Path,
    result_file: Optional[Path] = None,
    timeout: int = 30,
    max_retries: int = 2
) -> str:
    """
    Executes a VBScript inside the interactive user console session using schtasks /it.
    Integrates active watchdog polling: if the script is blocked by a popup,
    automatically detects, dismisses, and retries.
    """
    vbs_path = Path(vbs_path).resolve()
    ensure_sap_rules_whitelist()

    for attempt in range(1, max_retries + 1):
        # 1. Clean previous state
        if result_file:
            res_p = Path(result_file)
            if res_p.exists():
                try: res_p.unlink()
                except Exception: pass

        # 2. Terminate any previous stuck runner task
        import subprocess
        subprocess.run(f'schtasks /end /tn "{TASK_NAME}"', shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        # 3. Create & Run task
        cmd_create = f'schtasks /create /tn "{TASK_NAME}" /tr "{WSCRIPT_PATH} \\"{vbs_path}\\"" /sc once /st 00:00 /it /f'
        subprocess.run(cmd_create, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        cmd_run = f'schtasks /run /tn "{TASK_NAME}"'
        subprocess.run(cmd_run, shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)

        # 4. Watchdog Loop
        start_time = time.time()
        healed_during_run = False

        while time.time() - start_time < timeout:
            # Check result file
            if result_file and Path(result_file).exists() and Path(result_file).stat().st_size > 0:
                time.sleep(0.3)
                try:
                    with open(result_file, "r", encoding="utf-8", errors="replace") as f:
                        return f.read().strip()
                except Exception:
                    pass

            # Watchdog: If half timeout passed or elapsed 8s without result, scan & heal popups
            elapsed = time.time() - start_time
            if elapsed > 6.0 and int(elapsed * 2) % 4 == 0:
                healing = heal_sap_popups(capture_unknown=True)
                if healing:
                    print(f"⚡ [WATCHDOG] Healed {len(healing)} popup(s) during execution!")
                    healed_during_run = True

            time.sleep(0.5)

        # Timeout reached
        print(f"⚠️ [WATCHDOG] Execution timeout ({timeout}s) on attempt {attempt}/{max_retries}.")
        healing = heal_sap_popups(capture_unknown=True)
        if healing:
            print(f"⚡ [WATCHDOG] Dismissed {len(healing)} blocking popup(s). Retrying script...")
            time.sleep(1.0)
            continue
        
        # If no result file is required, return triggered
        if not result_file:
            return "TASK_TRIGGERED"

    return ""


if __name__ == "__main__":
    print("=== Testing Enterprise SAP Healer ===")
    print("1. Testing Whitelist...")
    w_res = ensure_sap_rules_whitelist()
    print(f"   -> Whitelist check: {w_res}")

    print("2. Scanning Popups...")
    heals = heal_sap_popups(capture_unknown=False)
    print(f"   -> Healed popups count: {len(heals)}")

    print("3. Testing Timestamped Path Generator...")
    ts_f, l_f = get_safe_timestamped_path(BASE_DIR / "reports" / "popups", "Test_Export", ".XLS")
    print(f"   -> Timestamped: {ts_f.name}")
    print(f"   -> Canonical:   {l_f.name}")
    print("=== Test Complete ===")
