"""
Reconciliation Report & Alert Generator
=======================================
01_Inventory_Team / 04_JOB_SystemReconciliation
- 일일 WMS vs SAP 재고정확도 분석 리포트 생성 및 저장 (reports/Reconciliation_Report_%Y%m%d.md / .xlsx).
- 공식 MM 재고정확도 수식 표준 적용:
  G: [MM] Stock Accuracy = AVG { 1 - ( |I-WMS stock - SAP stock| / SAP stock ) } * 100
- 주요 지표 요약:
  1. 공식 전사 재고정확도 (avg_score, %) 및 전일 대비 변동폭 (DoD Delta)
  2. 완전 일치율 (accuracy_rate, %, Diff=0 기준)
  3. 재무 리스크 TOP 5 부품 (불일치 금액 기준)
  4. AI 파이프라인(MB1B 311) 인과 기여도 (Attribution)
  5. 만성 유령 재고(Ghost Inventory, 3일 이상 지속) 현장 실사 권고 목록
"""

import os
import sys
import pandas as pd
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
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Load environment variables from 01_M365MCP/.env
ENV_PATH = BASE_DIR.parents[1] / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    load_dotenv()

sys.path.insert(0, str(BASE_DIR))
if str(BASE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(BASE_DIR.parent))
from report_labels import check_report_language
from db_manager import (
    get_latest_summary,
    get_top_financial_risks,
    get_all_discrepancies,
    get_chronic_ghost_items,
    get_baseline_summary
)


def generate_reconciliation_excel(
    summary: Dict[str, Any],
    snapshot_date: Optional[str] = None,
    output_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Path:
    """
    Generates a multi-tab professional Excel report:
    - Tab 1: Executive_Summary
    - Tab 2: Top_Financial_Risks
    - Tab 3: All_Discrepancies
    - Tab 4: Chronic_Ghost_Inventory
    """
    date_str = summary["snapshot_date"].replace("-", "")
    if dry_run:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if output_dir is not None and Path(output_dir).resolve() != REPORTS_DIR.resolve():
            target_dir = Path(output_dir)
        else:
            target_dir = BASE_DIR / "scratch"
        target_dir.mkdir(parents=True, exist_ok=True)
        excel_path = target_dir / f"DRYRUN_Reconciliation_Report_{ts_str}.xlsx"
    else:
        target_dir = Path(output_dir or REPORTS_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        excel_path = target_dir / f"Reconciliation_Report_{date_str}.xlsx"

    s_date = summary["snapshot_date"]
    top_risks = get_top_financial_risks(s_date, limit=20)
    all_discrepancies = get_all_discrepancies(s_date)
    chronic_ghosts = get_chronic_ghost_items(min_days=3)

    rename_cols = {
        "material": "Material",
        "description": "Description",
        "material_type": "Material Type",
        "wms_lgort": "WMS SLoc",
        "sap_lgort": "SAP SLoc",
        "wms_qty": "WMS Qty",
        "sap_qty": "SAP Qty",
        "diff_qty": "Diff Qty",
        "score": "Accuracy Score (%)",
        "net_price": "Unit Price ($)",
        "financial_exposure": "Financial Exposure ($)",
    }

    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        # 1. Summary Sheet
        summary_rows = [
            {"Metric": "Snapshot Date", "Value": summary["snapshot_date"], "Note": "Next-day close aggregation"},
            {"Metric": "SAP Posting Date", "Value": summary["posting_date"], "Note": "ZTCODE_STOCK_REC posting date basis"},
            {"Metric": "Plant / System Group", "Value": f"{summary['plant']} / {summary['system_group']}", "Note": "Headlamp manufacturing plant"},
            {"Metric": "Official MM Stock Accuracy", "Value": f"{summary.get('avg_score', 0.0):.2f}%", "Note": "Formula: AVG { 1 - (|WMS-SAP|/SAP) } * 100"},
            {"Metric": "Perfect Match Rate", "Value": f"{summary.get('accuracy_rate', 0.0):.2f}%", "Note": f"{summary.get('matched_items', 0):,} / {summary.get('total_items', 0):,} items"},
            {"Metric": "Day-over-Day Delta (DoD)", "Value": f"{summary.get('dod_accuracy_delta', 0.0):+.2f}%p", "Note": "Change vs previous snapshot"},
            {"Metric": "Total Evaluated Materials", "Value": f"{summary.get('total_items', 0):,}", "Note": "ZTCODE_STOCK_REC ALV Grid total items"},
            {"Metric": "Exact Match Materials (Diff = 0)", "Value": f"{summary.get('matched_items', 0):,}", "Note": "100% matched items"},
            {"Metric": "Mismatched Materials (Diff != 0)", "Value": f"{summary.get('mismatched_items', 0):,}", "Note": "Items requiring review"},
            {"Metric": "Total Absolute Diff Qty (|Diff|)", "Value": f"{summary.get('total_diff_qty', 0.0):,.1f} EA", "Note": "Sum of absolute quantity differences"},
            {"Metric": "Total Financial Exposure", "Value": f"${summary.get('total_financial_exposure', 0.0):,.2f} USD", "Note": "Unit-price weighted discrepancy value"},
            {"Metric": "Consignment Stock Filter", "Value": "Applied (ROH2/ROH3 excluded)", "Note": "74-* (resin) and 89-* (chemical/paint) excluded"},
        ]
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Executive_Summary", index=False)

        # 2. Top Financial Risks
        if top_risks:
            df_top = pd.DataFrame(top_risks)
            df_top.rename(columns=rename_cols).to_excel(writer, sheet_name="Top_Financial_Risks", index=False)

        # 3. All Discrepancies
        if all_discrepancies:
            df_all = pd.DataFrame(all_discrepancies)
            df_all.rename(columns=rename_cols).to_excel(writer, sheet_name="All_Discrepancies", index=False)

        # 4. Chronic Ghost Inventory
        if chronic_ghosts:
            df_ghost = pd.DataFrame(chronic_ghosts)
            ghost_rename = {
                "material": "Material",
                "material_type": "Material Type",
                "description": "Description",
                "first_detected_date": "First Detected",
                "last_detected_date": "Last Detected",
                "consecutive_days": "Consecutive Days",
                "current_wms_qty": "WMS Qty",
                "current_sap_qty": "SAP Qty",
                "current_diff_qty": "Diff Qty",
                "current_financial_exposure": "Financial Exposure ($)",
            }
            df_ghost.rename(columns=ghost_rename).to_excel(writer, sheet_name="Chronic_Ghost_Inventory", index=False)

    return excel_path


def generate_reconciliation_report(
    summary: Optional[Dict[str, Any]] = None,
    attribution: Optional[Dict[str, Any]] = None,
    output_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Path:
    """
    Generates and saves the daily reconciliation report (Markdown).
    Returns path to the saved markdown report.
    """
    if summary is None:
        summary = get_latest_summary()

    if not summary:
        raise ValueError("No summary data available to generate report.")

    top_risks = get_top_financial_risks(summary.get("snapshot_date"), limit=5)
    chronic_ghosts = get_chronic_ghost_items(min_days=3)

    today_str = summary["snapshot_date"].replace("-", "")
    if dry_run:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if output_dir is not None and Path(output_dir).resolve() != REPORTS_DIR.resolve():
            target_dir = Path(output_dir)
        else:
            target_dir = BASE_DIR / "scratch"
        target_dir.mkdir(parents=True, exist_ok=True)
        report_file = target_dir / f"DRYRUN_Reconciliation_Report_{ts_str}.md"
        latest_file = None
    else:
        target_dir = Path(output_dir or REPORTS_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        report_file = target_dir / f"Reconciliation_Report_{today_str}.md"
        latest_file = target_dir / "Reconciliation_Report_latest.md"

    # Delta badge formatting
    delta = summary.get("dod_accuracy_delta", 0.0)
    delta_str = f"+{delta:.2f}%p 📈" if delta > 0 else (f"{delta:.2f}%p 📉" if delta < 0 else "0.00%p ➖")

    avg_score = summary.get("avg_score", summary.get("accuracy_rate", 0.0))
    match_rate = summary.get("accuracy_rate", 0.0)

    md = []
    md.append(f"# 🛡️ {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} [01_Inventory] Official WMS vs SAP Stock Accuracy Report")
    md.append(f"**Snapshot Date**: `{summary['snapshot_date']}` (Posting Date: `{summary['posting_date']}`) | **Plant**: `{summary['plant']}` (`{summary['system_group']}`)")
    md.append(f"**Generated At**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z').strip()}\n")
    md.append("---\n")

    # 1. Executive Summary Cards
    md.append("## 📊 1. Executive Stock Accuracy KPIs")
    md.append("> **Standard Formula**:  ")
    md.append(r"> $$G:\ [MM]\ \text{Stock Accuracy} = AVG \left\{ 1 - \frac{|I\text{-}WMS\text{ stock} - SAP\text{ stock}|}{SAP\text{ stock}} \right\} \times 100$$")
    md.append("> 🏷️ **Consignment Stock Filter**: Consignment raw materials (ROH2: 74-* resin, ROH3: 89-* chemical/paint) with structural book-to-physical offsets are automatically excluded.\n")
    md.append("| Official MM Accuracy | Perfect Match Rate | DoD Delta | Total Items | Matched | Mismatched | Financial Exposure |")
    md.append("| :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    md.append(f"| **{avg_score:.2f}%** 🎯 | **{match_rate:.2f}%** | **{delta_str}** | **{summary['total_items']:,}** | **{summary['matched_items']:,}** | **{summary['mismatched_items']:,}** | **${summary['total_financial_exposure']:,.2f}** |\n")

    # 2. Top 5 Financial Risks
    md.append("## 💰 2. Top 5 Discrepancies by Financial Exposure")
    md.append("> *Highest dollar-weighted inventory discrepancies ranked by unit price impact.*")
    if top_risks:
        md.append("| Rank | Material | Description | WMS Qty | SAP Qty | Diff Qty | Unit Price | **Financial Exposure ($)** | Accuracy Score |")
        md.append("| :---: | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
        for idx, r in enumerate(top_risks, 1):
            md.append(f"| {idx} | `{r['material']}` | {r.get('description', '')[:30]} | {r['wms_qty']:,.0f} | {r['sap_qty']:,.0f} | **{r['diff_qty']:+,.0f}** | ${r.get('net_price', 0):,.2f} | **${r.get('financial_exposure', 0):,.2f}** | {r.get('score', 0):.1f}% |")
    else:
        md.append("✅ 100% Matched Across All Materials (No Financial Exposure)")
    md.append("")

    # 3. AI Pipeline Attribution & Synergies
    md.append("## ⚡ 3. Automated Pipeline Attribution (MB1B 311)")
    if attribution and attribution.get("evaluated_materials_count", 0) > 0:
        ev_count = attribution["evaluated_materials_count"]
        res_count = attribution["resolved_materials_count"]
        lift = attribution.get("accuracy_lift_pct", 0.0)
        success_rate = (res_count / ev_count * 100) if ev_count > 0 else 0.0

        md.append(f"- **Prev-Day Automated Transfer (01/02 JOB MB1B 311) Materials**: `{ev_count}`")
        md.append(f"- **Confirmed 100% Resolved in Today's ZTCODE_STOCK_REC**: `{res_count}` (Resolution Rate: **{success_rate:.1f}%**)")
        md.append(f"- **Direct Official Accuracy Lift**: **+{lift:.2f}%p** 🚀\n")
    else:
        md.append("ℹ️ No prior-day automated transfer items to evaluate for this snapshot.\n")

    # 4. Chronic Ghost Inventory Alert (Cycle Count Priority)
    md.append("## 👻 4. Chronic Discrepancy Alert (Persisting >= 3 Days)")
    md.append("> *Materials with $WMS \\neq SAP$ for 3+ consecutive days; recommended as priority physical cycle-count targets (`00_JOB_StockAccuracy`).*")
    if chronic_ghosts:
        md.append("| Material | Description | Consecutive Days | WMS Qty | SAP Qty | Diff Qty | Exposure ($) | Recommended Action |")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |")
        for g in chronic_ghosts[:10]:
            md.append(f"| `{g['material']}` | {g.get('description', '')[:25]} | **{g['consecutive_days']}d** | {g['current_wms_qty']:,.0f} | {g['current_sap_qty']:,.0f} | {g['current_diff_qty']:+,.0f} | ${g['current_financial_exposure']:,.2f} | 🔍 Priority Cycle Count (`00_JOB`) |")
    else:
        md.append("✅ No chronic discrepancies persisting >= 3 days.")
    md.append("")

    # 5. Continuous Learning Baseline Model (Long-term Profile)
    baseline = get_baseline_summary(plant=summary.get("plant", _get_ops_env("SAP_PLANT", "P001")), system_group=summary.get("system_group", "IWMS"))
    if baseline and baseline.get("total_materials", 0) > 0:
        md.append("## 🤖 5. ZTCODE_STOCK_REC Longitudinal Baseline Profile")
        md.append(f"- **Evaluated Materials**: `{baseline['total_materials']:,}` (up to `{baseline['max_snapshots_analyzed']}` historical snapshots)")
        md.append(f"- **Consistently Matched**: `{baseline['perfect_count']:,}` (Mean Match Rate: **{baseline.get('overall_match_rate', 0.0):.2f}%**)")
        md.append(f"- **Chronic SAP Surplus**: `{baseline['ghost_deficit_count']:,}` | **Chronic WMS Surplus**: `{baseline['over_stock_count']:,}` | **Volatile**: `{baseline['volatile_count']:,}`\n")

    report_content = "\n".join(md)
    report_file.write_text(report_content, encoding="utf-8")
    if latest_file is not None:
        latest_file.write_text(report_content, encoding="utf-8")

    return report_file


def get_reconciliation_recipients(to_recipients: Optional[List[str]] = None, is_test: bool = False) -> str:
    """Returns recipient email address, strictly resolving to test user or production configured recipients."""
    if is_test:
        test_to = os.getenv("TEST_EMAIL_RECIPIENT")
        if test_to and "@" in test_to and "example.com" not in test_to:
            return test_to.strip()
        raise ValueError("[FAIL CLOSED] Test email recipient (TEST_EMAIL_RECIPIENT) not configured or invalid.")

    if to_recipients:
        valid = [r.strip() for r in to_recipients if r.strip() and "example.com" not in r]
        if valid:
            return "; ".join(valid)

    # 1. Environment variable from .env
    env_to = os.getenv("RECONCILIATION_REPORT_TO") or os.getenv("GI_ERROR_REPORT_TO")
    if env_to:
        valid = [r.strip() for r in env_to.split(",") if r.strip() and "example.com" not in r]
        if valid:
            return "; ".join(valid)

    raise ValueError("[FAIL CLOSED] Operational reconciliation recipient not configured or invalid.")


def build_executive_html_body(
    summary: Dict[str, Any],
    top_risks: List[Dict[str, Any]],
    chronic_ghosts: List[Dict[str, Any]],
    attribution: Optional[Dict[str, Any]] = None,
    excel_filename: Optional[str] = None
) -> str:
    """Builds a clean, executive, Outlook-compatible HTML report with modern aesthetics."""
    avg_score = summary.get("avg_score", summary.get("accuracy_rate", 0.0))
    match_rate = summary.get("accuracy_rate", 0.0)
    delta = summary.get("dod_accuracy_delta", 0.0)
    delta_str = f"+{delta:.2f}%p" if delta > 0 else (f"{delta:.2f}%p" if delta < 0 else "0.00%p")
    delta_color = "#16a34a" if delta > 0 else ("#dc2626" if delta < 0 else "#64748b")
    delta_icon = "▲" if delta > 0 else ("▼" if delta < 0 else "—")

    # 1. Top Financial Risks Table Rows (Max 5)
    risk_rows_html = []
    for idx, r in enumerate(top_risks[:5], 1):
        diff = r.get("diff_qty", 0.0)
        diff_str = f"{diff:+,.0f}"
        diff_badge = f'<span style="display:inline-block; padding:2px 8px; border-radius:4px; font-weight:700; font-size:11px; background:#eff6ff; color:#1d4ed8;">{diff_str}</span>' if diff > 0 else f'<span style="display:inline-block; padding:2px 8px; border-radius:4px; font-weight:700; font-size:11px; background:#fef2f2; color:#b91c1c;">{diff_str}</span>'
        
        wms_loc = r.get("wms_lgort", "")
        sap_loc = r.get("sap_lgort", "")
        loc_str = f"{wms_loc} : {sap_loc}" if wms_loc != sap_loc else wms_loc

        risk_rows_html.append(f"""
        <tr style="border-bottom: 1px solid #f1f5f9; font-size: 12px;">
            <td style="padding: 10px 8px; text-align: center; color: #64748b; font-weight: 600;">{idx}</td>
            <td style="padding: 10px 8px; font-family: Consolas, monospace; font-weight: 700; color: #0f172a;">{r.get('material', '')}</td>
            <td style="padding: 10px 8px; color: #334155; max-width: 220px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{r.get('description', '')[:28]}</td>
            <td style="padding: 10px 8px; text-align: center; color: #475569; font-size: 11px;">{loc_str}</td>
            <td style="padding: 10px 8px; text-align: right; color: #0f172a; font-weight: 600;">{r.get('wms_qty', 0):,.0f}</td>
            <td style="padding: 10px 8px; text-align: right; color: #0f172a; font-weight: 600;">{r.get('sap_qty', 0):,.0f}</td>
            <td style="padding: 10px 8px; text-align: right;">{diff_badge}</td>
            <td style="padding: 10px 8px; text-align: right; color: #b91c1c; font-weight: 800;">${r.get('financial_exposure', 0):,.2f}</td>
        </tr>
        """)

    top_risks_table = "".join(risk_rows_html) if risk_rows_html else '<tr><td colspan="8" style="padding: 16px; text-align: center; color: #16a34a; font-weight: 600;">✅ 100% Matched Across All Materials (No Financial Exposure)</td></tr>'

    # 2. Chronic Ghost Items Table (3+ days, Top 5 by Financial Exposure)
    ghost_section_html = ""
    if chronic_ghosts:
        sorted_ghosts = sorted(chronic_ghosts, key=lambda x: x.get("current_financial_exposure", 0.0), reverse=True)[:5]
        ghost_rows = []
        for g in sorted_ghosts:
            days = g.get("consecutive_days", 0)
            diff = g.get("current_diff_qty", 0.0)
            diff_badge = f'<span style="color:#b91c1c; font-weight:700;">{diff:+,.0f} EA</span>'
            ghost_rows.append(f"""
            <tr style="border-bottom: 1px solid #f1f5f9; font-size: 12px;">
                <td style="padding: 8px; font-family: Consolas, monospace; font-weight: 700; color: #0f172a;">{g.get('material', '')}</td>
                <td style="padding: 8px; color: #334155; max-width: 220px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">{g.get('description', '')[:26]}</td>
                <td style="padding: 8px; text-align: center;"><span style="background:#fee2e2; color:#991b1b; padding:2px 8px; border-radius:4px; font-weight:700; font-size:11px;">{days}d streak</span></td>
                <td style="padding: 8px; text-align: right;">{diff_badge}</td>
                <td style="padding: 8px; text-align: right; color: #991b1b; font-weight: 700;">${g.get('current_financial_exposure', 0):,.2f}</td>
                <td style="padding: 8px; text-align: center; font-size: 11px; color: #475569;"><span style="background:#f1f5f9; padding:2px 6px; border-radius:4px; font-weight:600;">Priority Cycle Count (00_JOB)</span></td>
            </tr>
            """)
        ghost_section_html = f"""
        <div style="margin-top: 24px;">
            <div style="font-size: 14px; font-weight: 700; color: #991b1b; margin-bottom: 6px;">
                👻 Chronic Discrepancies (Top 5 Persisting &ge; 3 Days)
            </div>
            <div style="font-size: 11px; color: #64748b; margin-bottom: 10px;">
                Materials with persistent book-to-physical offsets for 3+ consecutive days &mdash; recommended as priority physical cycle-count targets (00_JOB_StockAccuracy).
            </div>
            <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; border: 1px solid #fed7aa; border-radius: 8px; overflow: hidden; background: #fff;">
                <thead>
                    <tr style="background: #fff7ed; border-bottom: 2px solid #fdba74; font-size: 11px; color: #9a3412; font-weight: 700;">
                        <th style="padding: 8px; text-align: left;">Material</th>
                        <th style="padding: 8px; text-align: left;">Description</th>
                        <th style="padding: 8px; text-align: center;">Streak</th>
                        <th style="padding: 8px; text-align: right;">Diff Qty</th>
                        <th style="padding: 8px; text-align: right;">Exposure ($)</th>
                        <th style="padding: 8px; text-align: center;">Action</th>
                    </tr>
                </thead>
                <tbody>
                    {"".join(ghost_rows)}
                </tbody>
            </table>
        </div>
        """

    # 3. AI Attribution Banner (if any)
    ai_banner_html = ""
    if attribution and attribution.get("evaluated_materials_count", 0) > 0:
        ev = attribution.get("evaluated_materials_count", 0)
        res = attribution.get("resolved_materials_count", 0)
        lift = attribution.get("accuracy_lift_pct", 0.0)
        ai_banner_html = f"""
        <div style="margin-top: 20px; padding: 12px 16px; background: #faf5ff; border: 1px solid #e9d5ff; border-radius: 8px;">
            <div style="font-size: 12px; font-weight: 700; color: #6b21a8; margin-bottom: 2px;">
                ⚡ Automated Transfer Posting (01_JOB MB1B 311) Attribution
            </div>
            <div style="font-size: 12px; color: #7e22ce;">
                <b>{res}</b> of <b>{ev}</b> prior-day auto-posted materials confirmed 100% matched in ZTCODE_STOCK_REC &rarr; Direct accuracy lift: <b>+{lift:.2f}%p</b>
            </div>
        </div>
        """

    # 4. Continuous Learning Baseline Card
    baseline_card_html = ""
    baseline = get_baseline_summary(plant=summary.get("plant", _get_ops_env("SAP_PLANT", "P001")), system_group=summary.get("system_group", "IWMS"))
    if baseline and baseline.get("total_materials", 0) > 0:
        baseline_card_html = f"""
        <div style="margin-top: 24px; padding: 14px 18px; background: #faf5ff; border: 1px solid #e9d5ff; border-radius: 8px;">
            <div style="font-size: 13px; font-weight: 700; color: #6b21a8; margin-bottom: 6px;">
                🤖 ZTCODE_STOCK_REC Longitudinal Baseline Profile
            </div>
            <div style="font-size: 11px; color: #581c87; line-height: 1.6;">
                &bull; <b>Evaluated Scope</b>: <b>{baseline['total_materials']:,}</b> materials profiled across up to <b>{baseline['max_snapshots_analyzed']}</b> historical ZTCODE_STOCK_REC snapshots.<br/>
                &bull; <b>Profile Breakdown</b>: Consistently Matched <b>{baseline['perfect_count']:,}</b> ({baseline.get('overall_match_rate', 0):.1f}%) &bull; Chronic SAP Surplus <b>{baseline['ghost_deficit_count']:,}</b> &bull; Chronic WMS Surplus <b>{baseline['over_stock_count']:,}</b> &bull; Volatile <b>{baseline['volatile_count']:,}</b><br/>
                &bull; <b>Continuous Tracking</b>: Daily ZTCODE_STOCK_REC snapshots refine transient-lag vs. chronic-discrepancy classification over time.
            </div>
        </div>
        """

    excel_info = f"attached workbook <b>{excel_filename}</b>" if excel_filename else "the attached multi-sheet Excel workbook"

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
    </head>
    <body style="margin: 0; padding: 20px; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif; color: #1e293b;">
        <div style="max-width: 820px; margin: 0 auto; background: #ffffff; border-radius: 12px; border: 1px solid #e2e8f0; padding: 28px; box-shadow: 0 4px 12px rgba(0,0,0,0.04);">
            
            <!-- Header -->
            <div style="border-bottom: 2px solid #1e3a8a; padding-bottom: 14px; margin-bottom: 20px;">
                <div style="display: inline-block; padding: 3px 8px; background: #eff6ff; color: #1e40af; border-radius: 4px; font-size: 11px; font-weight: 700; margin-bottom: 6px;">
                    {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing').upper()} INVENTORY TEAM &bull; 04_JOB_SystemReconciliation
                </div>
                <h2 style="margin: 0; color: #0f172a; font-size: 20px; font-weight: 800; letter-spacing: -0.3px;">
                    🛡️ Official WMS vs SAP Stock Accuracy Daily Report
                </h2>
                <div style="font-size: 12px; color: #64748b; margin-top: 6px;">
                    Snapshot Date: <b>{summary['snapshot_date']}</b> (SAP Posting Date: {summary['posting_date']}) &bull; Plant: <b>{summary['plant']}</b> ({summary['system_group']})
                </div>
                <div style="display: inline-block; padding: 3px 8px; background: #fef3c7; color: #92400e; border: 1px solid #fde68a; border-radius: 4px; font-size: 11px; font-weight: 700; margin-top: 8px;">
                    🏷️ Consignment Filter Applied: ROH2/ROH3 (74-* resin, 89-* chemical/paint) excluded
                </div>
            </div>

            <!-- 4-Grid KPI Cards -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom: 24px; border-collapse: separate; border-spacing: 10px 0;">
                <tr>
                    <td width="25%" style="background: #f0f7ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 14px 12px; text-align: center;">
                        <div style="font-size: 11px; font-weight: 700; color: #1e40af; text-transform: uppercase;">Official Accuracy</div>
                        <div style="font-size: 24px; font-weight: 800; color: #1e3a8a; margin: 4px 0;">{avg_score:.2f}%</div>
                        <div style="font-size: 11px; font-weight: 700; color: {delta_color};">{delta_icon} DoD {delta_str}</div>
                    </td>
                    <td width="25%" style="background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 14px 12px; text-align: center;">
                        <div style="font-size: 11px; font-weight: 700; color: #166534; text-transform: uppercase;">Perfect Match Rate</div>
                        <div style="font-size: 24px; font-weight: 800; color: #14532d; margin: 4px 0;">{match_rate:.2f}%</div>
                        <div style="font-size: 11px; color: #15803d; font-weight: 600;">{summary['matched_items']:,} / {summary['total_items']:,} items</div>
                    </td>
                    <td width="25%" style="background: #fffbeb; border: 1px solid #fde68a; border-radius: 10px; padding: 14px 12px; text-align: center;">
                        <div style="font-size: 11px; font-weight: 700; color: #92400e; text-transform: uppercase;">Mismatched Items</div>
                        <div style="font-size: 24px; font-weight: 800; color: #78350f; margin: 4px 0;">{summary['mismatched_items']:,}</div>
                        <div style="font-size: 11px; color: #b45309; font-weight: 600;">Total |Diff| {summary['total_diff_qty']:,.0f} EA</div>
                    </td>
                    <td width="25%" style="background: #fff1f2; border: 1px solid #fecdd3; border-radius: 10px; padding: 14px 12px; text-align: center;">
                        <div style="font-size: 11px; font-weight: 700; color: #9f1239; text-transform: uppercase;">Financial Exposure</div>
                        <div style="font-size: 21px; font-weight: 800; color: #881337; margin: 4px 0;">${summary['total_financial_exposure']:,.0f}</div>
                        <div style="font-size: 11px; color: #be123c; font-weight: 600;">Price-weighted sum</div>
                    </td>
                </tr>
            </table>

            <!-- Section 1: Top 5 Financial Risks -->
            <div style="margin-top: 16px;">
                <div style="font-size: 14px; font-weight: 700; color: #0f172a; margin-bottom: 6px;">
                    💰 Top 5 Discrepancies by Financial Exposure
                </div>
                <div style="font-size: 11px; color: #64748b; margin-bottom: 10px;">
                    Highest dollar-impact inventory discrepancies combining quantity variance and unit price.
                </div>
                <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse: collapse; border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden;">
                    <thead>
                        <tr style="background: #f8fafc; border-bottom: 2px solid #cbd5e1; font-size: 11px; color: #334155; font-weight: 700;">
                            <th style="padding: 10px 8px; text-align: center; width: 30px;">#</th>
                            <th style="padding: 10px 8px; text-align: left;">Material</th>
                            <th style="padding: 10px 8px; text-align: left;">Description</th>
                            <th style="padding: 10px 8px; text-align: center;">SLoc (WMS : SAP)</th>
                            <th style="padding: 10px 8px; text-align: right;">WMS Qty</th>
                            <th style="padding: 10px 8px; text-align: right;">SAP Qty</th>
                            <th style="padding: 10px 8px; text-align: right;">Diff Qty</th>
                            <th style="padding: 10px 8px; text-align: right;">Exposure ($)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {top_risks_table}
                    </tbody>
                </table>
            </div>

            <!-- Section 2: Chronic Ghosts -->
            {ghost_section_html}

            <!-- AI Attribution -->
            {ai_banner_html}

            <!-- Section 3: Continuous Learning Baseline -->
            {baseline_card_html}

            <!-- Notice Box -->
            <div style="margin-top: 24px; padding: 14px 18px; background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; font-size: 12px; color: #475569; line-height: 1.6;">
                💡 <b>Full Detail</b>: Complete SLoc-level reconciliation records across all <b>{summary['total_items']:,}</b> materials and 4 analytical tabs are included in {excel_info}.
            </div>

            <!-- Footer -->
            <div style="margin-top: 20px; padding-top: 14px; border-top: 1px solid #e2e8f0; font-size: 11px; color: #94a3b8; text-align: center;">
                {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Plant {_get_ops_env('SAP_PLANT', 'P001')} Headlamp &bull; 01_DEPT_Inventory / 04_JOB_SystemReconciliation &bull; Automated Daily Audit Pipeline
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_reconciliation_email(
    report_path: Path,
    summary: Dict[str, Any],
    excel_path: Optional[Path] = None,
    to_recipients: Optional[List[str]] = None,
    attribution: Optional[Dict[str, Any]] = None,
    is_test: bool = False
) -> bool:
    """Dispatches the executive reconciliation report via Outlook COM with Excel attachment."""
    import pythoncom
    import win32com.client

    to_str = get_reconciliation_recipients(to_recipients, is_test=is_test)

    delta = summary.get("dod_accuracy_delta", 0.0)
    delta_str = f"+{delta:.2f}%p" if delta >= 0 else f"{delta:.2f}%p"

    avg_score = summary.get("avg_score", summary.get("accuracy_rate", 0.0))
    match_rate = summary.get("accuracy_rate", 0.0)

    prefix = "[TEST] " if is_test else ""
    subject = f"{prefix}[Stock Accuracy] Official WMS vs SAP Stock Accuracy ({avg_score:.2f}%, Match Rate {match_rate:.2f}%) - {summary['snapshot_date']}"

    # Fetch top risks and chronic ghosts for clean HTML rendering
    s_date = summary.get("snapshot_date")
    top_risks = get_top_financial_risks(s_date, limit=5)
    chronic_ghosts = get_chronic_ghost_items(min_days=3)
    excel_name = excel_path.name if excel_path and excel_path.exists() else None

    html_body = build_executive_html_body(
        summary=summary,
        top_risks=top_risks,
        chronic_ghosts=chronic_ghosts,
        attribution=attribution,
        excel_filename=excel_name
    )

    try:
        pythoncom.CoInitialize()
        ol = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = ol.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass
        check_report_language(
            "04_JOB_reconciliation_notifier",
            subject=subject,
            html_body=html_body,
            attachments=[excel_path] if excel_path else [],
            is_user_only=is_test,
        )
        mail = ol.CreateItem(0)
        _assert_clean_ops_output(subject, html_body, context="04_JOB reconciliation_notifier")
        mail.To = to_str
        mail.Subject = subject
        mail.HTMLBody = html_body

        if excel_path and excel_path.exists():
            mail.Attachments.Add(str(excel_path))
            print(f"  [OK] Attached Reconciliation Excel Report: {excel_path.name}")

        mail.Send()
        print(f"  [OK] Executive reconciliation report email sent to: {to_str}")
        return True
    except Exception as e:
        print(f"  [WARN] Could not dispatch reconciliation email: {e}")
        return False


def print_console_summary(summary: Dict[str, Any], top_risks: List[Dict[str, Any]]):
    """Prints clean terminal summary for console observation."""
    delta = summary.get("dod_accuracy_delta", 0.0)
    delta_str = f"+{delta:.2f}%p" if delta >= 0 else f"{delta:.2f}%p"
    avg_score = summary.get("avg_score", summary.get("accuracy_rate", 0.0))
    match_rate = summary.get("accuracy_rate", 0.0)

    print("\n" + "=" * 76)
    print(f"  🛡️ {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} [01_Inventory] WMS vs SAP 시스템 공식 재고정확도")
    print(f"  - 집계 일자: {summary.get('snapshot_date')} (마감기준: {summary.get('posting_date')})")
    print(f"  - 🎯 공식 MM 재고정확도: {avg_score:.2f}% (수식: AVG {{ 1 - (|WMS-SAP|/SAP) }} * 100)")
    print(f"  - 📊 전 품목 완전 일치율: {match_rate:.2f}% (DoD 변동: {delta_str})")
    print(f"  - 전체 모수 품목: {summary.get('total_items', 0):,}개 (일치: {summary.get('matched_items', 0):,}개 / 불일치: {summary.get('mismatched_items', 0):,}개)")
    print(f"  - 총 불일치 차이수량: {summary.get('total_diff_qty', 0.0):,.1f} EA")
    print(f"  - 총 재무 위험금액: ${summary.get('total_financial_exposure', 0.0):,.2f} USD")
    print("=" * 76)

    if top_risks:
        print("\n  [Top Financial Risk Items]")
        for i, item in enumerate(top_risks, 1):
            mat = item.get('material', item.get('mat_no', ''))
            desc = item.get('description', item.get('mat_desc', ''))[:20]
            wms = item.get('wms_qty', item.get('wms_qty', 0.0))
            sap = item.get('sap_qty', 0.0)
            diff = item.get('diff_qty', 0.0)
            exp = item.get('financial_exposure', 0.0)
            score = item.get('score', 0.0)
            print(f"  {i}. {mat} ({desc}) | WMS={wms:,.0f} vs SAP={sap:,.0f} | Diff={diff:+,.0f} | Exp=${exp:,.2f} | Score={score:.1f}%")
    print()
