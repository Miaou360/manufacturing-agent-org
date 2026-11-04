"""
Daily Briefing Generator (00_TaskManager)
아침에 Outlook 수신함 및 일정을 스캔하여 오늘 처리할 업무를 도메인별로 분류하고
마크다운 브리핑 리포트를 생성합니다.
"""

import os
import sys
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
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
import pythoncom
import win32com.client

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Base paths
CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
REPORTS_DIR = CURRENT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Categories and their detection keywords
CATEGORIES = {
    "📦 재고 및 창고 관리 (Inventory & Warehouse)": {
        "keywords": ["stock", "verification", "sp11", "sp31", "wms", "sl00", "sl01", "sl02", "sl06", "sl11", "mcm", "rack", "inventory", "재고", "창고", "창고팀", "실사", "cogi", "backlog", "ztcode_gi_err", "ztcode_stock_rec", "gi error", "gr error", "mb1b"],
        "senders": [s.strip() for s in os.getenv("BRIEFING_SENDERS_INV", "warehouse.lead,prod.coord,inv.analyst.a").split(",") if s.strip()]
    },
    "🧾 송장 및 매입 정산 (Invoices & Accounting)": {
        "keywords": ["invoice", "invoices", "open invoice", "remittance", "payment", "송장", "계산서", "정산", "전기", "posting"] + [k.strip().lower() for k in os.getenv("BRIEFING_KEYWORDS_ACCT", "vendor_a,vendor_b").split(",") if k.strip()],
        "senders": [s.strip() for s in os.getenv("BRIEFING_SENDERS_ACCT", "supv_a,eng.specialist,vendor_a,vendor_b,vendor_c").split(",") if s.strip()]
    },
    "🔄 EDI 및 발주 동기화 (EDI & Order Sync)": {
        "keywords": ["edi", "EDI_Gateway_A", "EDI_Gateway_B", "830", "862", "856", "delfor", "BINSYNTH9931", "ztcode_order_sync", "srm", "발주", "납품일정", "납기", "EDI_Portal"],
        "senders": ["EDI_Gateway_A", "EDI_Gateway_B", "edi_support", "edi_support"]
    },
    "🚚 물류 및 입고 오퍼레이션 (Logistics & Receiving)": {
        "keywords": ["eta", "carrier", "freight", "receiving", "unloading", "bol", "bill of lading", "gaylord", "gaylords", "dock", "duty", "customs", "관세", "하차", "배차", "물류", "입고"],
        "senders": [s.strip() for s in os.getenv("BRIEFING_SENDERS_LOG", "carrier_a,carrier_b,carrier_c,vendor_d,vendor_e,coordinator c").split(",") if s.strip()]
    },
    "⚙️ 개발/원소재 및 마스터데이터 (Engineering & MasterData)": {
        "keywords": ["bom", "silo", "사일로", "qms", "재생재", "신재", "rev", "master data", "tool transfer", "차종"],
        "senders": [s.strip() for s in os.getenv("BRIEFING_SENDERS_ENG", "analyst c,specialist b,specialist c,coordinator b").split(",") if s.strip()]
    }
}

URGENCY_KEYWORDS = ["urgent", "asap", "긴급", "누락", "missing", "error", "failed", "차이", "discrepancy", "confirm", "action point", "action needed", "disregard", "over shipped", "damaged"]


def categorize_mail(subject: str, sender: str, body: str) -> str:
    combined = f"{subject} {sender} {body}".lower()
    for cat_name, criteria in CATEGORIES.items():
        for kw in criteria["keywords"]:
            if kw in combined:
                return cat_name
        for s in criteria["senders"]:
            if s in sender.lower():
                return cat_name
    return "📌 일반 및 사내 공지 (General & Internal)"


def is_urgent(subject: str, body: str) -> bool:
    combined = f"{subject} {body}".lower()
    return any(uk in combined for uk in URGENCY_KEYWORDS)


def generate_daily_briefing(max_emails: int = 50, days_ahead_calendar: int = 2) -> Path:
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = getattr(ol, "Session", None)
        if ns is None:
            try:
                ns = ol.GetNameSpace("MAPI")
            except Exception:
                ns = ol.GetNamespace("MAPI")
        
        # 1. Scan Inbox
        inbox = ns.GetDefaultFolder(6)  # olFolderInbox
        items = inbox.Items
        try:
            items.Sort("[ReceivedTime]", True)
        except Exception:
            pass

        email_items = []
        item = items.GetFirst()
        count = 0
        while item and count < max_emails:
            try:
                subject = getattr(item, "Subject", "(No Subject)")
                sender = getattr(item, "SenderName", "")
                sender_email = getattr(item, "SenderEmailAddress", "")
                received_time = getattr(item, "ReceivedTime", None)
                body = getattr(item, "Body", "")
                
                # Check recent items
                email_items.append({
                    "subject": subject,
                    "sender": sender,
                    "sender_email": sender_email,
                    "received_time": str(received_time) if received_time else "",
                    "body_snippet": " ".join(body.split())[:200] if body else "",
                    "category": categorize_mail(subject, f"{sender} {sender_email}", body),
                    "urgent": is_urgent(subject, body)
                })
                count += 1
            except Exception:
                pass
            
            try:
                item = items.GetNext()
            except Exception:
                break

        # 2. Scan Calendar
        calendar = ns.GetDefaultFolder(9)  # olFolderCalendar
        cal_items = calendar.Items
        cal_items.IncludeRecurrences = True
        cal_items.Sort("[Start]")
        
        now = datetime.datetime.now()
        end_date = now + datetime.timedelta(days=days_ahead_calendar)
        start_str = now.strftime("%m/%d/%Y 00:00 AM")
        end_str = end_date.strftime("%m/%d/%Y 11:59 PM")
        filter_query = f"[Start] >= '{start_str}' AND [Start] <= '{end_str}'"
        
        meetings = []
        try:
            filtered_meetings = cal_items.Restrict(filter_query)
            for m in filtered_meetings:
                try:
                    meetings.append({
                        "subject": getattr(m, "Subject", "(No Title)"),
                        "start": str(getattr(m, "Start", "")),
                        "end": str(getattr(m, "End", "")),
                        "location": getattr(m, "Location", "")
                    })
                except Exception:
                    pass
        except Exception:
            pass

        # 3. Build Markdown Report
        today_str = now.strftime("%Y-%m-%d")
        report_path = REPORTS_DIR / f"daily_briefing_{today_str}.md"
        
        urgent_items = [e for e in email_items if e["urgent"]]
        
        md_lines = []
        md_lines.append(f"# 🌅 일일 업무 종합 브리핑 ({today_str})")
        md_lines.append(f"> 생성 시각: {now.strftime('%Y-%m-%d %H:%M')} | 분석 메일: 최근 {len(email_items)}건 | 예정 미팅: {len(meetings)}건\n")
        
        # Urgent Alerts
        md_lines.append("## 🚨 긴급 및 주의 요망 안건 (Action Required)")
        if urgent_items:
            for u in urgent_items[:8]:
                md_lines.append(f"- **[{u['category'].split()[1]}] {u['subject']}**")
                md_lines.append(f"  - *발신*: {u['sender']} ({u['received_time'][:16]})")
                md_lines.append(f"  - *내용 요약*: `{u['body_snippet'][:130]}...`\n")
        else:
            md_lines.append("- 현재 감지된 긴급 주의 안건이 없습니다.\n")

        # Today's Meetings
        md_lines.append("## 📅 오늘 및 주요 미팅 일정")
        if meetings:
            for m in meetings:
                loc_txt = f" (장소: {m['location']})" if m['location'] else ""
                md_lines.append(f"- **{m['start']} ~ {m['end']}**: {m['subject']}{loc_txt}")
        else:
            md_lines.append("- 향후 2일간 예정된 주요 미팅이 없습니다.")
        md_lines.append("")

        # 4. Autonomous Pipeline Health & Metrics
        try:
            from pipeline_monitor import get_inventory_health_summary_md
            pipeline_md = get_inventory_health_summary_md()
            md_lines.append(pipeline_md)
        except Exception as e:
            md_lines.append(f"<!-- Pipeline status note: {e} -->\n")

        # Category Breakdown
        md_lines.append("## 📂 도메인 팀별 상세 업무 현황")
        
        grouped = {}
        for item_data in email_items:
            cat = item_data["category"]
            grouped.setdefault(cat, []).append(item_data)

        for cat_name in CATEGORIES.keys():
            items_in_cat = grouped.get(cat_name, [])
            md_lines.append(f"### {cat_name} ({len(items_in_cat)}건)")
            if items_in_cat:
                for item_data in items_in_cat[:6]:
                    urg_tag = "🔴 [긴급] " if item_data["urgent"] else ""
                    md_lines.append(f"- {urg_tag}**{item_data['subject']}** | *{item_data['sender']}* ({item_data['received_time'][:16]})")
                    md_lines.append(f"  - {item_data['body_snippet'][:120]}...")
            else:
                md_lines.append("- 최근 관련 메일이 없습니다.")
            md_lines.append("")

        # General/Other
        gen_items = grouped.get("📌 일반 및 사내 공지 (General & Internal)", [])
        if gen_items:
            md_lines.append(f"### 📌 일반 및 사내 공지 ({len(gen_items)}건)")
            for g in gen_items[:4]:
                md_lines.append(f"- **{g['subject']}** | *{g['sender']}* ({g['received_time'][:16]})")
            md_lines.append("")

        report_content = "\n".join(md_lines)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_content)

        return report_path

    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    print("[00_TaskManager] 일일 업무 브리핑 생성 시작...")
    path = generate_daily_briefing()
    print(f"[00_TaskManager] 브리핑 보고서 생성 완료: {path}")
