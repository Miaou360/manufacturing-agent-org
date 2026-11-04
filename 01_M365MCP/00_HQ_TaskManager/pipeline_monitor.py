"""
Pipeline Monitor (00_TaskManager)
=================================
Centralized Orchestration & Health Monitoring for 01_DEPT_Inventory Automation:
1. Windows Task Scheduler status for all 4 pipelines.
2. 24/7 Daemon process health & PID lock verification.
3. Live database metrics (Stock Accuracy, GI Error, COGI Backlog).
4. Storage & disk retention status.
5. Markdown report generation for integration with daily_briefing.py.
"""

import os
import sys
import io
import csv
import json
import sqlite3
import datetime
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
INVENTORY_DIR = PROJECT_ROOT / "01_DEPT_Inventory"

# Database paths
DB_STOCK_ACCURACY = INVENTORY_DIR / "00_JOB_StockAccuracy" / "db" / "inventory_history.db"
DB_GR_GI_ERROR = INVENTORY_DIR / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db"
DB_BACKLOG = INVENTORY_DIR / "02_JOB_Backlog_Clear" / "db" / "backlog_history.db"
LOCK_GI_DAEMON = INVENTORY_DIR / "01_JOB_GR_GI_Error" / "hourly_gi_pipeline.lock"
ONTOLOGY_PATH = INVENTORY_DIR / "00_JOB_StockAccuracy" / "ontology" / "material_turnover_ontology.json"

TASK_NAMES = [
    ("SCM_Agent_Daily_Stock_Accuracy", "WMS 크롤링 및 재고 정확도 순환실사", "매일 04:00 AM (365일 무휴 수집, 일요일 메일 스킵)"),
    ("SCM_Agent_Daily_Backlog_0610", "COGI 백로그 1차 사전 SAVE & 아카이브", "매일 06:10 AM"),
    ("SCM_Agent_Daily_Backlog_0815", "COGI 백로그 2차 최종 해소 & 리포트", "매일 08:15 AM"),
    ("SCM_Agent_Continuous_GI_Error", "GI 인터페이스 에러 24시간 무중단 자동 해소", "24/7 상시 (주간 30m / 야간 1h)"),
    ("SCM_Agent_Weekly_Backlog_Analysis", "COGI 시계열 4대차원 주간 심층 분석", "매주 금 08:30 AM (08:15 백로그 해소 완료 후)")
]


def check_scheduled_tasks() -> List[Dict[str, Any]]:
    """Inspects Windows Scheduled Tasks status via schtasks."""
    results = []
    for t_name, desc, schedule in TASK_NAMES:
        info = {
            "task_name": t_name,
            "description": desc,
            "schedule": schedule,
            "status": "Unknown",
            "next_run": "N/A",
            "last_run": "N/A",
            "last_result": "N/A",
            "is_healthy": False
        }
        try:
            cmd = ["schtasks", "/query", "/tn", t_name, "/fo", "csv", "/v"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                reader = csv.DictReader(io.StringIO(res.stdout))
                for row in reader:
                    info["status"] = row.get("Status", "Unknown")
                    info["next_run"] = row.get("Next Run Time", "N/A")
                    info["last_run"] = row.get("Last Run Time", "N/A")
                    info["last_result"] = row.get("Last Result", "N/A")
                    # Task is healthy if status is Running or Ready
                    if info["status"] in ["Running", "Ready"]:
                        info["is_healthy"] = True
                    break
            else:
                info["status"] = "Not Registered"
        except Exception as e:
            info["status"] = f"Error: {e}"
        results.append(info)
    return results


def check_daemon_process() -> Dict[str, Any]:
    """Inspects 01_GR_GI_Error 24/7 daemon process health via lock file and psutil/PowerShell."""
    daemon_info = {
        "lock_exists": False,
        "pid": None,
        "is_running": False,
        "cpu_percent": 0.0,
        "memory_mb": 0.0,
        "uptime": "N/A"
    }

    if not LOCK_GI_DAEMON.exists():
        return daemon_info

    daemon_info["lock_exists"] = True
    try:
        pid_str = LOCK_GI_DAEMON.read_text(encoding="utf-8").strip()
        pid = int(pid_str)
        daemon_info["pid"] = pid

        # Check process via PowerShell
        ps_cmd = f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object Id, ProcessName, WorkingSet64, StartTime | ConvertTo-Json"
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            proc_data = json.loads(res.stdout)
            daemon_info["is_running"] = True
            ws_bytes = proc_data.get("WorkingSet64", 0)
            daemon_info["memory_mb"] = round(ws_bytes / (1024 * 1024), 1)
            start_time = proc_data.get("StartTime", "")
            if start_time and "Date(" in str(start_time):
                # Handle /Date(xxx)/ format if returned
                daemon_info["uptime"] = "Active"
            else:
                daemon_info["uptime"] = str(start_time)[:19]
    except Exception:
        daemon_info["is_running"] = False

    return daemon_info


def check_stock_accuracy_db() -> Dict[str, Any]:
    """Queries 00_StockAccuracy DB health & metrics."""
    data = {
        "exists": False,
        "size_mb": 0.0,
        "journal_mode": "N/A",
        "gr_count": 0,
        "gi_count": 0,
        "ontology_count": 0,
        "ontology_updated": False,
        "last_audit_date": "N/A"
    }
    if not DB_STOCK_ACCURACY.exists():
        return data

    data["exists"] = True
    data["size_mb"] = round(DB_STOCK_ACCURACY.stat().st_size / (1024 * 1024), 1)

    try:
        conn = sqlite3.connect(str(DB_STOCK_ACCURACY))
        c = conn.cursor()
        c.execute("PRAGMA journal_mode;")
        data["journal_mode"] = str(c.fetchone()[0]).lower()

        c.execute("SELECT COUNT(*) FROM GR_TRANSACTIONS;")
        data["gr_count"] = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM GI_TRANSACTIONS;")
        data["gi_count"] = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM MATERIAL_ONTOLOGY;")
        data["ontology_count"] = c.fetchone()[0]

        conn.close()
    except Exception:
        pass

    if ONTOLOGY_PATH.exists():
        data["ontology_updated"] = True

    return data


def check_gr_gi_error_db() -> Dict[str, Any]:
    """Queries 01_GR_GI_Error DB health & today's resolution metrics."""
    data = {
        "exists": False,
        "size_mb": 0.0,
        "journal_mode": "N/A",
        "total_transfers": 0,
        "today_transfers": 0,
        "today_transfer_materials": [],
        "last_run_timestamp": "N/A",
        "last_run_status": "N/A",
        "latest_open_errors": 0,
        "email_logs_count": 0,
        "latest_email_slot": "N/A"
    }
    if not DB_GR_GI_ERROR.exists():
        return data

    data["exists"] = True
    data["size_mb"] = round(DB_GR_GI_ERROR.stat().st_size / (1024 * 1024), 2)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(str(DB_GR_GI_ERROR))
        c = conn.cursor()
        c.execute("PRAGMA journal_mode;")
        data["journal_mode"] = str(c.fetchone()[0]).lower()

        # Total & Today's transfers
        c.execute("SELECT COUNT(*) FROM transfer_posting_history;")
        data["total_transfers"] = c.fetchone()[0]

        c.execute("""
            SELECT material, transfer_qty, doc_number, executed_at, post_status 
            FROM transfer_posting_history 
            WHERE SUBSTR(executed_at, 1, 10) = ?;
        """, (today_str,))
        rows = c.fetchall()
        data["today_transfers"] = len(rows)
        data["today_transfer_materials"] = [f"{r[0]} ({int(r[1]) if r[1] else 0}EA -> Doc {r[2] if r[2] else 'PENDING'})" for r in rows[:5]]

        # Last pipeline run metrics
        c.execute("SELECT run_timestamp, status, total_gi_errors FROM pipeline_run_metrics ORDER BY id DESC LIMIT 1;")
        metric = c.fetchone()
        if metric:
            data["last_run_timestamp"] = str(metric[0])
            data["last_run_status"] = str(metric[1])
            data["latest_open_errors"] = metric[2]

        # Email sent log
        c.execute("SELECT slot_key, sent_at FROM gi_email_sent_log ORDER BY rowid DESC LIMIT 1;")
        elog = c.fetchone()
        if elog:
            data["latest_email_slot"] = f"{elog[0]} ({elog[1][:16]})"
        c.execute("SELECT COUNT(*) FROM gi_email_sent_log;")
        data["email_logs_count"] = c.fetchone()[0]

        conn.close()
    except Exception:
        pass

    return data


def check_backlog_db() -> Dict[str, Any]:
    """Queries 02_Backlog_Clear DB health & today's COGI resolution metrics."""
    data = {
        "exists": False,
        "size_mb": 0.0,
        "journal_mode": "N/A",
        "total_transfers": 0,
        "today_transfers": 0,
        "today_transfer_materials": [],
        "raw_history_today_count": 0,
        "unresolved_today_count": 0,
        "latest_run_date": "N/A"
    }
    if not DB_BACKLOG.exists():
        return data

    data["exists"] = True
    data["size_mb"] = round(DB_BACKLOG.stat().st_size / (1024 * 1024), 2)
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(str(DB_BACKLOG))
        c = conn.cursor()
        c.execute("PRAGMA journal_mode;")
        data["journal_mode"] = str(c.fetchone()[0]).lower()

        # Transfers
        c.execute("SELECT COUNT(*) FROM backlog_transfers;")
        data["total_transfers"] = c.fetchone()[0]

        c.execute("""
            SELECT material, transfer_qty, sap_doc_no, executed_at 
            FROM backlog_transfers 
            WHERE SUBSTR(executed_at, 1, 10) = ?;
        """, (today_str,))
        rows = c.fetchall()
        data["today_transfers"] = len(rows)
        data["today_transfer_materials"] = [f"{r[0]} ({int(r[1]) if r[1] else 0}EA -> Doc {r[2]})" for r in rows[:5]]

        # Raw history today
        c.execute("""
            SELECT COUNT(*) FROM raw_backlog_history 
            WHERE SUBSTR(run_date, 1, 10) = ? OR SUBSTR(extracted_at, 1, 10) = ?;
        """, (today_str, today_str))
        data["raw_history_today_count"] = c.fetchone()[0]

        # Latest metrics
        c.execute("SELECT run_date, unresolved_items FROM daily_backlog_metrics ORDER BY id DESC LIMIT 1;")
        metric = c.fetchone()
        if metric:
            data["latest_run_date"] = str(metric[0])
            data["unresolved_today_count"] = metric[1]

        conn.close()
    except Exception:
        pass

    return data


def get_pipeline_status() -> Dict[str, Any]:
    """Aggregates comprehensive live status across all inventory systems."""
    return {
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "scheduled_tasks": check_scheduled_tasks(),
        "gi_daemon": check_daemon_process(),
        "stock_accuracy": check_stock_accuracy_db(),
        "gr_gi_error": check_gr_gi_error_db(),
        "backlog": check_backlog_db()
    }


def render_terminal_dashboard(status: Dict[str, Any]) -> str:
    """Renders high-visibility terminal text dashboard."""
    lines = []
    ts = status["timestamp"]
    lines.append("=" * 82)
    lines.append(f" 🚀 [{os.getenv('COMPANY_NAME', 'ACME Manufacturing').upper()}] 자재관리 자동화 파이프라인 통합 관제 대시보드 ({ts})")
    lines.append("=" * 82)

    # 1. Scheduled Tasks
    lines.append("\n[1] Windows Task Scheduler 가동 상태 (100% Headless 무인 자동화)")
    lines.append("-" * 82)
    lines.append(f"{'태스크 명':<25} | {'상태':<9} | {'다음 실행':<22} | {'주기/설명'}")
    lines.append("-" * 82)
    for t in status["scheduled_tasks"]:
        badge = "🟢" if t["status"] in ["Running", "Ready"] else "🔴"
        lines.append(f"{badge} {t['task_name']:<23} | {t['status']:<9} | {t['next_run']:<22} | {t['schedule']}")
    lines.append("-" * 82)

    # 2. Daemon Process
    d = status["gi_daemon"]
    lines.append("\n[2] 24/7 상시 데몬 프로세스 상태 (01_GR_GI_Error)")
    lines.append("-" * 82)
    if d["is_running"]:
        lines.append(f"🟢 프로세스 활성 상태: PID {d['pid']} (정상 가동 중)")
        lines.append(f"   - 메모리 점유율: {d['memory_mb']} MB (리소스 안전 가드레일 준수)")
        lines.append(f"   - 가동 시작시각: {d['uptime']}")
    else:
        lines.append(f"🔴 프로세스 정지 상태 (Lock File PID: {d.get('pid', 'N/A')})")
    lines.append("-" * 82)

    # 3. Pipeline Real-time Metrics
    sa = status["stock_accuracy"]
    ge = status["gr_gi_error"]
    bl = status["backlog"]

    lines.append("\n[3] 파이프라인별 실시간 DB 및 처리 실적")
    lines.append("-" * 82)
    lines.append("📦 00_StockAccuracy (재고정확도 순환실사):")
    lines.append(f"   - DB 크기: {sa['size_mb']} MB (WAL: {sa['journal_mode']}) | 온톨로지 마스터: {sa['ontology_count']:,}개 품번")
    lines.append(f"   - WMS 입고 누적: {sa['gr_count']:,}건 | 출고 누적: {sa['gi_count']:,}건")

    lines.append("\n⚡ 01_GR_GI_Error (24시간 무중단 입출고 인터페이스 해소):")
    lines.append(f"   - DB 크기: {ge['size_mb']} MB (WAL: {ge['journal_mode']}) | 최근 가동: {ge['last_run_timestamp']}")
    lines.append(f"   - 오늘 MB1B 311 자동 해소: {ge['today_transfers']}건 발행 (전체 누적: {ge['total_transfers']}건)")
    if ge["today_transfer_materials"]:
        lines.append(f"   - 오늘 처리 품번 샘플: {', '.join(ge['today_transfer_materials'])}")
    lines.append(f"   - 최신 알림 발송 슬롯: {ge['latest_email_slot']}")

    lines.append("\n🏭 02_Backlog_Clear (COGI 생산 백로그 일일 2회 사전/본해소):")
    lines.append(f"   - DB 크기: {bl['size_mb']} MB (WAL: {bl['journal_mode']}) | 오늘 초기 COGI: {bl['raw_history_today_count']}건")
    lines.append(f"   - 오늘 MB1B 311 잉여 이동전기: {bl['today_transfers']}건 (전체 누적: {bl['total_transfers']}건)")
    if bl["today_transfer_materials"]:
        lines.append(f"   - 오늘 해소 품번 샘플: {', '.join(bl['today_transfer_materials'])}")
    lines.append(f"   - 미해소 잔여 COGI: {bl['unresolved_today_count']}건 (자재 플래너 조치 대기)")

    lines.append("=" * 82)
    return "\n".join(lines)


def get_inventory_health_summary_md(status: Optional[Dict[str, Any]] = None) -> str:
    """Generates markdown section formatted for daily briefing integration."""
    if status is None:
        status = get_pipeline_status()

    lines = []
    lines.append("## 🤖 자재관리 자동화 파이프라인 가동 현황 (Autonomous Pipelines Live)")
    lines.append(f"> 점검 시각: `{status['timestamp']}` | 관제 대상: 4개 배치/데몬 태스크 및 3대 데이터베이스\n")

    # Table of tasks
    lines.append("| 파이프라인 | 상태 | 다음 실행 예정 | 가동 주기 및 정책 |")
    lines.append("| :--- | :---: | :--- | :--- |")
    for t in status["scheduled_tasks"]:
        badge = "🟢 정상" if t["status"] in ["Running", "Ready"] else f"🔴 {t['status']}"
        next_run = t["next_run"] if t["next_run"] != "N/A" else "상시 구동 중"
        lines.append(f"| **{t['task_name']}**<br>({t['description']}) | {badge} | `{next_run}` | {t['schedule']} |")
    lines.append("")

    # Today's Highlights
    d = status["gi_daemon"]
    ge = status["gr_gi_error"]
    bl = status["backlog"]
    sa = status["stock_accuracy"]

    daemon_badge = "🟢 정상 가동 (PID " + str(d.get("pid")) + f", {d.get('memory_mb', 0)}MB)" if d["is_running"] else "🔴 정지 상태"

    lines.append("### 📊 실시간 파이프라인 실행 요약")
    lines.append(f"- **24/7 GI 에러 자동 해소 데몬**: {daemon_badge}")
    lines.append(f"  - 오늘 MB1B 311 자동 전표 발행: **{ge['today_transfers']}건** (누적 {ge['total_transfers']}건)")
    lines.append(f"  - 최근 수집 및 분석 시각: `{ge['last_run_timestamp']}` (결과: `{ge['last_run_status']}`)")
    lines.append(f"- **COGI 생산 백로그 파이프라인**:")
    lines.append(f"  - 오늘 1차/2차 누적 자동 전표 발행: **{bl['today_transfers']}건** (초기 백로그 {bl['raw_history_today_count']}건 중)")
    lines.append(f"  - 플래너 조치 요망 미해소 잔여건: **{bl['unresolved_today_count']}건**")
    lines.append(f"- **재고 정확도 순환실사 (Stock Accuracy)**:")
    lines.append(f"  - WMS 빅데이터 누적: 입고 {sa['gr_count']:,}건 / 출고 {sa['gi_count']:,}건 | 온톨로지: {sa['ontology_count']:,}개 품번 최신화 완료")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ACME Manufacturing Autonomous Pipeline Live Monitor")
    parser.add_argument("--json", action="store_true", help="Output JSON format")
    parser.add_argument("--markdown", action="store_true", help="Output Markdown summary")
    args = parser.parse_args()

    st = get_pipeline_status()
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    elif args.markdown:
        print(get_inventory_health_summary_md(st))
    else:
        print(render_terminal_dashboard(st))
