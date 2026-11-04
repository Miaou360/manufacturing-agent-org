"""
Warehouse Notifier (01_Inventory_Team / 00_StockAccuracy)
재고 정확도 분석 결과를 바탕으로 창고 담당자들에게
정기 점검 메일을 발송하거나 검토용 창(Display)을 엽니다.
"""

import os
import sys
import time
import argparse
import datetime
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
        if _k == "OEM_FLOOR_BIN_PREFIX":
            _fb = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "").strip()
            if _fb and _fb not in _DUMMY_ENV_VALS:
                _st.append("FLOOR_BUFFER_BIN_PREFIXES=configured")
                continue
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from typing import Optional, Dict, Any
import pythoncom
import win32com.client

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import get_recipients, VERIFICATION_MODE, MONITORED_PLANTS, DEFAULT_INTERVAL_HOURS
from stock_verifier import StockVerifier
_DEPT_DIR = Path(__file__).resolve().parent.parent
if str(_DEPT_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPT_DIR))
from report_labels import check_report_language


def send_warehouse_notification(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    mode: str = "Daily",
    display_first: bool = False,
    to_recipients: list = None,
    cc_recipients: list = None,
    is_test: bool = False,
    analysis: Optional[Dict[str, Any]] = None
) -> dict:
    """Analyze stock and send or display notification email via Outlook COM."""
    pythoncom.CoInitialize()
    try:
        # 1. Run Full Stock Analysis & Generate Audit Excel (or use provided analysis dict)
        verifier = StockVerifier()
        if analysis is None:
            analysis = verifier.run_full_stock_analysis(plant=plant, mode=mode)
        html_body = verifier.generate_html_report(analysis, mode=mode)
        audit_excel_path = analysis.get("audit_excel_path")
        
        # 2. Outlook COM Dispatch
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = getattr(ol, "Session", None)
        if ns is None:
            try:
                ns = ol.GetNameSpace("MAPI")
            except Exception:
                try:
                    ns = ol.GetNamespace("MAPI")
                except Exception:
                    ns = None
        if ns is not None:
            try:
                ns.Logon("", "", False, False)
            except Exception:
                pass

        mail = ol.CreateItem(0)  # 0 = olMailItem
        
        # 3. Recipients (Safety: If is_test or VERIFICATION_MODE is True, only sends to user)
        if is_test:
            raw_test = (
                os.getenv("TEST_EMAIL_RECIPIENT", "")
                or os.getenv("STOCK_ACCURACY_VERIFY", "")
                or os.getenv("GI_ERROR_REPORT_TO", "")
                or os.getenv("SMTP_USER", "")
            )
            first_addr = raw_test.replace(";", ",").split(",")[0].strip() if raw_test else ""
            to_list = [first_addr] if first_addr else []
            cc_list = []
        else:
            recipients = get_recipients(mode=mode)
            to_list = to_recipients if to_recipients is not None else recipients.get("to", [])
            cc_list = cc_recipients if cc_recipients is not None else (recipients.get("cc", []) if to_recipients is None else [])
        
        from config import is_valid_recipient
        to_list = [r for r in to_list if is_valid_recipient(r)]
        cc_list = [r for r in cc_list if is_valid_recipient(r)]

        if not to_list:
            print("[00_StockAccuracy] ❌ No valid recipients found (fail closed). Skipping email dispatch.")
            return {"success": False, "action": "skipped_fail_closed", "to": "", "cc": ""}

        to_str = "; ".join(to_list)
        cc_str = "; ".join(cc_list) if cc_list else ""
        mail.To = to_str
        if cc_list:
            mail.CC = cc_str
            
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        prefix = "[TEST] " if is_test else ""
        mail_subject = f"{prefix}[{mode}] Cycle Count List ({today_str})"
            
        _assert_clean_ops_output(mail_subject, html_body, context="00_JOB warehouse_notifier")
        check_report_language(
            f"00_JOB_warehouse_notifier_{mode}",
            mail_subject,
            html_body,
            attachments=[audit_excel_path] if audit_excel_path else [],
            is_user_only=bool(is_test),
        )
        mail.Subject = mail_subject
        mail.HTMLBody = html_body

        # 4. Attach Multi-Sheet Audit Excel
        if audit_excel_path and Path(audit_excel_path).exists():
            mail.Attachments.Add(str(Path(audit_excel_path).resolve()))
            print(f"[00_StockAccuracy] Attached file: {Path(audit_excel_path).name}")
        
        masked_to = f"<redacted:{len(to_list)}_recipient(s)>"
        if display_first:
            mail.Display()
            msg = f"Opened Outlook draft window (To: {masked_to}) for review before sending."
            print(f"[00_StockAccuracy] {msg}")
            return {"success": True, "action": "displayed", "subject": mail_subject, "to": masked_to, "cc": ""}
        else:
            mail.Send()
            msg = f"Email dispatched successfully (To: {masked_to})."
            print(f"[00_StockAccuracy] {msg}")
            return {"success": True, "action": "sent", "subject": mail_subject, "to": masked_to, "cc": ""}
            
    finally:
        pythoncom.CoUninitialize()


def main():
    parser = argparse.ArgumentParser(description="Warehouse Stock Notification Mailer")
    parser.add_argument("--plant", default=_get_ops_env("SAP_PLANT", "P001"), help="Target Plant (default: P001)")
    parser.add_argument("--mode", default="Daily", choices=["Daily", "Weekly"], help="Report frequency mode (Daily or Weekly)")
    parser.add_argument("--send", action="store_true", default=True, help="Send directly via Outlook COM (default: True)")
    parser.add_argument("--display", action="store_true", help="Display email in Outlook window instead of sending directly")
    parser.add_argument("--dry-run", action="store_true", help="Read-only dry run: write outputs to scratch/DRYRUN_* without sending email")
    parser.add_argument("--loop", type=float, default=0, help="Run repeatedly every N hours (e.g. 4.0)")
    
    args = parser.parse_args()
    display_mode = args.display

    if args.dry_run:
        verifier = StockVerifier()
        analysis = verifier.run_full_stock_analysis(plant=args.plant, mode=args.mode, dry_run=True)
        html_body = verifier.generate_html_report(analysis, mode=args.mode)
        _assert_clean_ops_output(html_body, context="00_JOB warehouse_notifier dry_run")
        scratch_dir = Path(__file__).resolve().parent / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = scratch_dir / f"DRYRUN_Warehouse_Report_{args.mode}_{ts_str}.html"
        html_path.write_text(html_body, encoding="utf-8")
        print(f"[00_StockAccuracy] [DRY-RUN] Excel: {analysis.get('audit_excel_path')} | HTML: {html_path}")
        return
    
    if args.loop > 0:
        print(f"[00_StockAccuracy] 주기적 알림 모드 시작: 매 {args.loop}시간마다 실행됩니다.")
        while True:
            print(f"\n[00_StockAccuracy] {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 재고 점검 및 알림 실행...")
            try:
                send_warehouse_notification(plant=args.plant, mode=args.mode, display_first=display_mode)
            except Exception as e:
                print(f"[00_StockAccuracy] 에러 발생: {e}")
            
            sleep_secs = int(args.loop * 3600)
            print(f"[00_StockAccuracy] 다음 실행까지 대기 중 ({args.loop}시간 후)...")
            time.sleep(sleep_secs)
    else:
        send_warehouse_notification(plant=args.plant, mode=args.mode, display_first=display_mode)


if __name__ == "__main__":
    main()
