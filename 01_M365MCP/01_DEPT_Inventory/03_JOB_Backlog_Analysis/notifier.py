"""
Notification Module for 03_Backlog_Analysis
===========================================
Dispatches comprehensive executive analysis reports & multi-tab Excel workbooks
to System Architect via Outlook COM.
"""

import sys
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
from typing import Dict, Any, Optional
import pythoncom
import win32com.client

from config import RECIPIENTS

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(level=logging.INFO, format="[%(asctime)s][%(levelname)s] %(message)s")
logger = logging.getLogger("BacklogAnalyticsNotifier")


def build_email_html(analysis_result: Dict[str, Any]) -> str:
    """Builds clean, executive-level HTML email body."""
    p = analysis_result["period"]
    s = analysis_result["summary"]
    tb = analysis_result["top_bottlenecks"]
    q_sum = analysis_result["quadrant_summary"]

    html = f"""
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Malgun Gothic", sans-serif; color: #1e293b; background-color: #f8fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); border: 1px solid #e2e8f0; }}
            .header {{ background: linear-gradient(135deg, #1e3a8a 0%, #0f172a 100%); color: #ffffff; padding: 26px 30px; }}
            .header h1 {{ margin: 0; font-size: 21px; font-weight: 700; letter-spacing: -0.5px; }}
            .header p {{ margin: 6px 0 0 0; font-size: 13px; color: #94a3b8; }}
            .content {{ padding: 26px 30px; }}
            .kpi-grid {{ display: table; width: 100%; table-layout: fixed; margin-bottom: 24px; }}
            .kpi-cell {{ display: table-cell; background: #f1f5f9; padding: 14px 12px; border-radius: 8px; text-align: center; border: 1px solid #e2e8f0; }}
            .kpi-cell + .kpi-cell {{ padding-left: 12px; }}
            .kpi-title {{ font-size: 11px; font-weight: 600; color: #64748b; text-transform: uppercase; margin-bottom: 4px; }}
            .kpi-value {{ font-size: 20px; font-weight: 800; color: #1e3a8a; }}
            .section-title {{ font-size: 15px; font-weight: 700; color: #0f172a; margin: 24px 0 12px 0; border-left: 4px solid #1e3a8a; padding-left: 10px; }}
            .table-custom {{ width: 100%; border-collapse: collapse; margin-bottom: 20px; font-size: 12px; }}
            .table-custom th {{ background-color: #f8fafc; color: #475569; font-weight: 700; text-align: left; padding: 9px 12px; border-bottom: 2px solid #e2e8f0; }}
            .table-custom td {{ padding: 9px 12px; border-bottom: 1px solid #f1f5f9; color: #334155; }}
            .badge-q1 {{ background-color: #fee2e2; color: #991b1b; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
            .badge-q2 {{ background-color: #fef3c7; color: #92400e; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
            .badge-q3 {{ background-color: #e0e7ff; color: #3730a3; padding: 3px 8px; border-radius: 4px; font-weight: 700; font-size: 11px; }}
            .badge-q4 {{ background-color: #f1f5f9; color: #475569; padding: 3px 8px; border-radius: 4px; font-weight: 600; font-size: 11px; }}
            .alert-box {{ background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 8px; padding: 14px 18px; margin-bottom: 22px; }}
            .alert-box h4 {{ margin: 0 0 6px 0; color: #991b1b; font-size: 13px; font-weight: 700; }}
            .alert-box p {{ margin: 0; color: #7f1d1d; font-size: 12px; line-height: 1.5; }}
            .footer {{ background: #f8fafc; padding: 16px 30px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📊 {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Plant {_get_ops_env('SAP_PLANT', 'P001')} 백로그(COGI) 시계열 종합 분석 보고서</h1>
                <p>분석 대상 기간: {p['start_date']} ~ {p['end_date']} ({p['total_audit_days']}일간 원본 적재 데이터) &bull; 03_Backlog_Analysis</p>
            </div>
            <div class="content">
                <!-- 4 KPI Cards -->
                <div class="kpi-grid">
                    <div class="kpi-cell">
                        <div class="kpi-title">분석 대상 품목수</div>
                        <div class="kpi-value">{s['total_materials']:,} 품번</div>
                    </div>
                    <div class="kpi-cell">
                        <div class="kpi-title">누적 백로그 수량</div>
                        <div class="kpi-value">{s['total_cumulative_qty']:,.0f} EA</div>
                    </div>
                    <div class="kpi-cell">
                        <div class="kpi-title">311 창고잉여 해소율</div>
                        <div class="kpi-value">{s['overall_resolvability_pct']:.1f}%</div>
                    </div>
                    <div class="kpi-cell">
                        <div class="kpi-title">라인스탑 고위험 품목</div>
                        <div class="kpi-value" style="color: {'#b91c1c' if s['line_stop_hazard_count'] > 0 else '#15803d'};">{s['line_stop_hazard_count']} 건</div>
                    </div>
                </div>
    """

    # Line-stop Hazards if any
    hazards = tb.get("line_stop_hazards", [])
    if hazards:
        hz_items = "".join([f"<li><b>{h['material']}</b> ({h['material_desc'][:30]}...) &bull; 누적결손: {h['total_deficit_qty']:,.0f} EA &bull; 회전일수: {h['median_turnover_days']}일 ({h['mover_class']})</li>" for h in hazards[:4]])
        html += f"""
                <div class="alert-box">
                    <h4>🛑 긴급 조치 요망: 라인스탑 고위험(Line-Stop Critical) 품목 {len(hazards)}건 감지</h4>
                    <p>회전율이 극도로 높은 Fast Mover 품목 중 만성적으로 COGI 결손이 발생하는 품목입니다:<br>
                    <ul style="margin: 6px 0 0 0; padding-left: 20px;">{hz_items}</ul></p>
                </div>
        """

    # Quadrant Matrix Table
    html += f"""
                <div class="section-title">🧭 4분면(Quadrant) 매트릭스 분류 현황</div>
                <table class="table-custom">
                    <thead>
                        <tr>
                            <th>사분면 구분</th>
                            <th>정의 및 성격</th>
                            <th style="text-align:center;">품목수</th>
                            <th style="text-align:right;">누적 결손량</th>
                            <th>주요 권고 조치</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td><span class="badge-q1">🚨 Q1. 만성 대량</span></td>
                            <td>고빈도 + 대량 결손 (최우선 병목)</td>
                            <td style="text-align:center; font-weight:700;">{s['q1_count']}</td>
                            <td style="text-align:right; font-weight:700;">{sum(m['total_deficit_qty'] for m in q_sum['Q1_CHRONIC_MASSIVE']):,.0f} EA</td>
                            <td>안전재고 상향 + 입고/보충 주기 전면 재검토</td>
                        </tr>
                        <tr>
                            <td><span class="badge-q2">⚠️ Q2. 만성 운영지체</span></td>
                            <td>고빈도 + 소량 결손 (창고 불출 지연)</td>
                            <td style="text-align:center; font-weight:700;">{s['q2_count']}</td>
                            <td style="text-align:right; font-weight:700;">{sum(m['total_deficit_qty'] for m in q_sum['Q2_CHRONIC_OPERATIONAL']):,.0f} EA</td>
                            <td>창고-현장 자동보충(Min-Max) 및 피킹 동선 개선</td>
                        </tr>
                        <tr>
                            <td><span class="badge-q3">⚡ Q3. 돌발 대형</span></td>
                            <td>저빈도 + 대량 결손 (공급사 결품 의심)</td>
                            <td style="text-align:center; font-weight:700;">{s['q3_count']}</td>
                            <td style="text-align:right; font-weight:700;">{sum(m['total_deficit_qty'] for m in q_sum['Q3_CRITICAL_SURGE']):,.0f} EA</td>
                            <td>공급사 납기(ETA) 및 선적 품질 집중 추적</td>
                        </tr>
                        <tr>
                            <td><span class="badge-q4">ℹ️ Q4. 경미 일시</span></td>
                            <td>저빈도 + 소량 결손 (일반 루틴)</td>
                            <td style="text-align:center; font-weight:700;">{s['q4_count']}</td>
                            <td style="text-align:right; font-weight:700;">{sum(m['total_deficit_qty'] for m in q_sum['Q4_MINOR_INFREQUENT']):,.0f} EA</td>
                            <td>기본 일일 06/08시 자동 이동전기 루틴 유지</td>
                        </tr>
                    </tbody>
                </table>

                <div class="section-title">🏆 Top 5 최다 발생 만성 결손 품목 (High Frequency)</div>
                <table class="table-custom">
                    <thead>
                        <tr>
                            <th style="width:30px;">#</th>
                            <th>품번</th>
                            <th>품명</th>
                            <th style="text-align:center;">발생일수</th>
                            <th style="text-align:right;">누적 결손량</th>
                            <th style="text-align:center;">온톨로지</th>
                            <th>사분면 분류</th>
                        </tr>
                    </thead>
                    <tbody>
    """

    for idx, m in enumerate(tb["by_frequency"][:5], 1):
        html += f"""
                        <tr>
                            <td>{idx}</td>
                            <td><b>{m['material']}</b></td>
                            <td>{m['material_desc'][:32]}</td>
                            <td style="text-align:center; font-weight:700;">{m['occurrence_days']}일 ({m['frequency_pct']}%)</td>
                            <td style="text-align:right; font-weight:700;">{m['total_deficit_qty']:,.0f} EA</td>
                            <td style="text-align:center;">{m['mover_class']}</td>
                            <td>{m['quadrant_name']}</td>
                        </tr>
        """

    html += f"""
                    </tbody>
                </table>
                <p style="font-size: 12px; color: #64748b; margin-top: 15px;">
                    📎 첨부파일: <b>다분할 상세 엑셀 시트</b> (Executive Summary, 4분면 매트릭스 전체, Top 10 병목, 온톨로지 결합 분석 포함)를 참조해 주시기 바랍니다.
                </p>
            </div>
            <div class="footer">
                {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Plant {_get_ops_env('SAP_PLANT', 'P001')} Headlamp &bull; 01_DEPT_Inventory / 03_JOB_Backlog_Analysis &bull; Generated autonomously
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_analysis_report(
    analysis_result: Dict[str, Any],
    excel_attachment_path: Optional[Path] = None,
    recipients: Optional[Dict[str, Any]] = None
) -> bool:
    if recipients is None:
        recipients = RECIPIENTS

    valid_to = [r for r in recipients.get("to", []) if r and "example.com" not in r.lower()]
    if not valid_to:
        logger.error("   ❌ [NOTIFIER] No valid recipients configured (fail closed). Skipping email dispatch.")
        return False

    p = analysis_result["period"]
    subject = f"[{_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')}] Backlog (COGI) 시계열 종합 분석 보고서 ({p['start_date']} ~ {p['end_date']})"
    html_body = build_email_html(analysis_result)
    _assert_clean_ops_output(subject, html_body, context="03_JOB backlog_analysis_notifier")

    pythoncom.CoInitialize()
    try:
        ol = win32com.client.Dispatch("Outlook.Application")
        ns = getattr(ol, "Session", None)
        if ns is None:
            try:
                ns = ol.GetNameSpace("MAPI")
            except Exception:
                ns = ol.GetNamespace("MAPI")
        try:
            ns.Logon("", "", False, False)
        except Exception:
            pass
        mail = ol.CreateItem(0)  # olMailItem
        mail.Subject = subject
        to_str = "; ".join(recipients["to"])
        mail.To = to_str
        if recipients.get("cc"):
            mail.CC = "; ".join(recipients["cc"])
        mail.HTMLBody = html_body

        if excel_attachment_path and excel_attachment_path.exists():
            mail.Attachments.Add(str(excel_attachment_path))
            logger.info(f"   📎 Attached Excel Report: {excel_attachment_path.name}")

        mail.Send()
        logger.info(f"   ✅ Analysis Report Email dispatched successfully to: {to_str}")
        return True
    except Exception as e:
        logger.error(f"   ❌ Failed to send analysis report email: {e}")
        return False
    finally:
        pythoncom.CoUninitialize()
