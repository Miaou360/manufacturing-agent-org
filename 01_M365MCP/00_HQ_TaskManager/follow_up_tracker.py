"""
Follow-up Tracker (00_TaskManager)
내가 보낸 메일(Sent Items) 중 상대방의 확인/회신이 필요한 안건을 선별하고,
받은 편지함(Inbox)에 회신이 도착했는지 대조하여 미결 상태를 추적합니다.
"""

import os
import sys
import datetime
from pathlib import Path
import pythoncom
import win32com.client

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CURRENT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = CURRENT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

REQUEST_INDICATORS = [
    "please", "could you", "would you", "confirm", "check", "verify",
    "부탁드립니다", "요청", "확인", "의견", "언제", "회신", "진행상황",
    "누락", "missing", "difference", "update", "send me"
]


def clean_subject(subj: str) -> str:
    """Strip RE:, FW:, etc. to match conversation threads."""
    s = subj.strip()
    while True:
        lower_s = s.lower()
        if lower_s.startswith("re:") or lower_s.startswith("fw:") or lower_s.startswith("wg:"):
            s = s[3:].strip()
        elif lower_s.startswith("re :") or lower_s.startswith("fw :"):
            s = s[4:].strip()
        else:
            break
    return s.strip()


def run_follow_up_tracker(max_sent: int = 35) -> Path:
    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = getattr(ol, "Session", None)
        if ns is None:
            try:
                ns = ol.GetNameSpace("MAPI")
            except Exception:
                ns = ol.GetNamespace("MAPI")
        
        sent_folder = ns.GetDefaultFolder(5)   # olFolderSentMail
        inbox_folder = ns.GetDefaultFolder(6)  # olFolderInbox
        
        # 1. Fetch recent inbox subjects & dates for matching
        inbox_items = inbox_folder.Items
        try:
            inbox_items.Sort("[ReceivedTime]", True)
        except Exception:
            pass
            
        inbox_subjects = {}
        item = inbox_items.GetFirst()
        count = 0
        while item and count < 100:
            try:
                subj = getattr(item, "Subject", "")
                r_time = getattr(item, "ReceivedTime", None)
                cleaned = clean_subject(subj).lower()
                if cleaned not in inbox_subjects:
                    inbox_subjects[cleaned] = []
                inbox_subjects[cleaned].append(r_time)
                count += 1
            except Exception:
                pass
            try:
                item = inbox_items.GetNext()
            except Exception:
                break

        # 2. Fetch sent items
        sent_items = sent_folder.Items
        try:
            sent_items.Sort("[SentOn]", True)
        except Exception:
            pass
            
        pending_list = []
        resolved_list = []
        
        item = sent_items.GetFirst()
        sent_count = 0
        while item and sent_count < max_sent:
            try:
                subj = getattr(item, "Subject", "")
                to = getattr(item, "To", "")
                sent_on = getattr(item, "SentOn", None)
                body = getattr(item, "Body", "")
                
                # Check if it looks like a request or inquiry
                combined = f"{subj} {body}".lower()
                is_request = any(ind in combined for ind in REQUEST_INDICATORS)
                
                if is_request and sent_on:
                    cleaned_subj = clean_subject(subj).lower()
                    replies = inbox_subjects.get(cleaned_subj, [])
                    
                    # Check if there is a reply received AFTER the sent time
                    has_newer_reply = False
                    for r_time in replies:
                        if r_time and r_time > sent_on:
                            has_newer_reply = True
                            break
                    
                    entry = {
                        "subject": subj,
                        "to": to,
                        "sent_on": str(sent_on)[:16],
                        "snippet": " ".join(body.split())[:150]
                    }
                    
                    if has_newer_reply:
                        resolved_list.append(entry)
                    else:
                        pending_list.append(entry)
                        
                sent_count += 1
            except Exception:
                pass
                
            try:
                item = sent_items.GetNext()
            except Exception:
                break
                
        # 3. Write Report
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")
        report_path = REPORTS_DIR / f"follow_up_status_{today_str}.md"
        
        lines = []
        lines.append(f"# ⏳ 보낸 편지 회신 대기(Follow-up) 상태 보고서 ({today_str})")
        lines.append(f"> 총 점검 발송 건수: {sent_count}건 | 회신 대기 중: {len(pending_list)}건 | 회신 완료/해결: {len(resolved_list)}건\n")
        
        lines.append("## 🔴 회신 대기 중인 안건 (Pending Follow-ups)")
        if pending_list:
            for i, p in enumerate(pending_list, 1):
                lines.append(f"### {i}. {p['subject']}")
                lines.append(f"- **수신인**: `{p['to']}`")
                lines.append(f"- **발송일시**: {p['sent_on']}")
                lines.append(f"- **요청 내용 발췌**: *\"{p['snippet']}\"*")
                lines.append("")
        else:
            lines.append("- 현재 회신 대기 중인 안건이 없습니다.\n")
            
        lines.append("## 🟢 회신 완료 또는 해결된 안건 (Resolved / Replied)")
        for r in resolved_list[:5]:
            lines.append(f"- **{r['subject']}** (수신: `{r['to'][:30]}...`, 발송: {r['sent_on']})")
        lines.append("")
        
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            
        return report_path
        
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    print("[00_TaskManager] Follow-up Tracker 실행...")
    p = run_follow_up_tracker()
    print(f"[00_TaskManager] Follow-up 보고서 생성 완료: {p}")
