"""
Report Generator for 03_Backlog_Analysis
========================================
Generates multi-tab Excel deep analysis reports, markdown summaries,
and HTML email templates.
"""

import os
import sys
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
from typing import Dict, Any, List
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from config import REPORTS_DIR


class BacklogReportGenerator:
    def __init__(self, output_dir: Path = REPORTS_DIR):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_excel_report(
        self,
        analysis_result: Dict[str, Any],
        filename_prefix: str = "Backlog_Deep_Analysis",
        dry_run: bool = False,
    ) -> Path:
        """Creates a professional multi-tab Excel workbook."""
        wb = openpyxl.Workbook()
        
        # Styles
        navy_header_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
        teal_header_fill = PatternFill(start_color="0D9488", end_color="0D9488", fill_type="solid")
        amber_header_fill = PatternFill(start_color="D97706", end_color="D97706", fill_type="solid")
        card_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
        alert_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
        
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        title_font = Font(name="Calibri", size=15, bold=True, color="1E3A8A")
        sub_font = Font(name="Calibri", size=10, italic=True, color="64748B")
        bold_font = Font(name="Calibri", size=10, bold=True)
        regular_font = Font(name="Calibri", size=10)
        alert_font = Font(name="Calibri", size=10, bold=True, color="B91C1C")
        
        thin_border = Border(
            left=Side(style='thin', color='CBD5E1'),
            right=Side(style='thin', color='CBD5E1'),
            top=Side(style='thin', color='CBD5E1'),
            bottom=Side(style='thin', color='CBD5E1')
        )
        
        align_center = Alignment(horizontal="center", vertical="center")
        align_left = Alignment(horizontal="left", vertical="center")
        align_right = Alignment(horizontal="right", vertical="center")

        # ----------------------------------------------------------------------
        # Tab 1: Executive Summary
        # ----------------------------------------------------------------------
        ws_sum = wb.active
        ws_sum.title = "Executive_Summary"
        ws_sum.views.sheetView[0].showGridLines = True

        p = analysis_result["period"]
        s = analysis_result["summary"]
        t = analysis_result["thresholds"]

        ws_sum.merge_cells("A1:G1")
        ws_sum["A1"] = f"🚀 {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Headlamp ({_get_ops_env('SAP_PLANT', 'P001')}) 생산 백로그(COGI) 시계열 종합 분석 보고서"
        ws_sum["A1"].font = title_font

        ws_sum.merge_cells("A2:G2")
        ws_sum["A2"] = f"분석 대상 기간: {p['start_date']} ~ {p['end_date']} (누적 {p['total_audit_days']}일간 적재 데이터) | 생성일시: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        ws_sum["A2"].font = sub_font

        # KPI Cards (Row 4-6)
        kpis = [
            ("총 분석 대상 품목수", f"{s['total_materials']:,} 품번", "A", "B"),
            ("누적 백로그 결손 수량", f"{s['total_cumulative_qty']:,.0f} EA", "C", "D"),
            ("MB1B 311 자동 해소율", f"{s['overall_resolvability_pct']:.1f}%", "E", "E"),
            ("라인스탑 고위험 품목", f"{s['line_stop_hazard_count']} 건", "F", "G"),
        ]
        for title, val, start_col, end_col in kpis:
            if start_col != end_col:
                ws_sum.merge_cells(f"{start_col}4:{end_col}4")
                ws_sum.merge_cells(f"{start_col}5:{end_col}5")
            ws_sum[f"{start_col}4"] = title
            ws_sum[f"{start_col}4"].font = bold_font
            ws_sum[f"{start_col}4"].alignment = align_center
            ws_sum[f"{start_col}4"].fill = card_fill
            ws_sum[f"{start_col}5"] = val
            ws_sum[f"{start_col}5"].font = Font(name="Calibri", size=14, bold=True, color="1E3A8A")
            ws_sum[f"{start_col}5"].alignment = align_center
            ws_sum[f"{start_col}5"].fill = card_fill

        # 4-Quadrant Distribution Table (Row 8)
        ws_sum.cell(row=8, column=1, value="📊 4분면(Quadrant) 매트릭스 분류 현황").font = Font(size=12, bold=True, color="1E3A8A")
        
        headers_q = ["사분면 구분", "정의 및 특징", "품목수", "누적 결손량(EA)", "해소율(%)", "주요 조치 권고사항"]
        for c_idx, h in enumerate(headers_q, 1):
            cell = ws_sum.cell(row=9, column=c_idx, value=h)
            cell.fill = navy_header_fill
            cell.font = header_font
            cell.alignment = align_center
            cell.border = thin_border

        q_rows = [
            ("🚨 Q1. 만성 대량 결손", "고빈도 + 대량 결손 (최우선 병목)", s["q1_count"], 
             sum(m["total_deficit_qty"] for m in analysis_result["quadrant_summary"]["Q1_CHRONIC_MASSIVE"]),
             np_mean_safe([m["resolvability_rate_pct"] for m in analysis_result["quadrant_summary"]["Q1_CHRONIC_MASSIVE"]]),
             "안전재고 상향 + 입고/보충 주기 전면 재검토 (구매/플래너)"),
            ("⚠️ Q2. 만성 운영 지체", "고빈도 + 소량 결손 (창고 피킹/불출 지연)", s["q2_count"],
             sum(m["total_deficit_qty"] for m in analysis_result["quadrant_summary"]["Q2_CHRONIC_OPERATIONAL"]),
             np_mean_safe([m["resolvability_rate_pct"] for m in analysis_result["quadrant_summary"]["Q2_CHRONIC_OPERATIONAL"]]),
             "창고-현장 자동보충(Min-Max) 및 피킹 동선 개선 (창고팀)"),
            ("⚡ Q3. 돌발 대형 결손", "저빈도 + 대량 결손 (공급사 결품/선적 지연)", s["q3_count"],
             sum(m["total_deficit_qty"] for m in analysis_result["quadrant_summary"]["Q3_CRITICAL_SURGE"]),
             np_mean_safe([m["resolvability_rate_pct"] for m in analysis_result["quadrant_summary"]["Q3_CRITICAL_SURGE"]]),
             "공급사 납기(ETA) 및 선적 품질 집중 추적 (구매팀)"),
            ("ℹ️ Q4. 경미 일시 결손", "저빈도 + 소량 결손 (일반 루틴)", s["q4_count"],
             sum(m["total_deficit_qty"] for m in analysis_result["quadrant_summary"]["Q4_MINOR_INFREQUENT"]),
             np_mean_safe([m["resolvability_rate_pct"] for m in analysis_result["quadrant_summary"]["Q4_MINOR_INFREQUENT"]]),
             "기본 일일 06/08시 자동 이동전기 루틴 유지 (시스템)")
        ]

        curr_r = 10
        for q_name, desc, cnt, vol, res_rate, act in q_rows:
            ws_sum.cell(row=curr_r, column=1, value=q_name).font = bold_font
            ws_sum.cell(row=curr_r, column=2, value=desc).font = regular_font
            ws_sum.cell(row=curr_r, column=3, value=cnt).font = bold_font
            ws_sum.cell(row=curr_r, column=3).alignment = align_center
            ws_sum.cell(row=curr_r, column=4, value=vol).number_format = "#,##0"
            ws_sum.cell(row=curr_r, column=4).font = bold_font
            ws_sum.cell(row=curr_r, column=5, value=f"{res_rate:.1f}%").alignment = align_center
            ws_sum.cell(row=curr_r, column=6, value=act).font = regular_font

            for c in range(1, 7):
                ws_sum.cell(row=curr_r, column=c).border = thin_border
            curr_r += 1

        # Line-Stop Hazard Warnings (Row curr_r + 2)
        curr_r += 2
        ws_sum.cell(row=curr_r, column=1, value="🛑 라인스탑 고위험(Line-Stop Critical) 품목 경보").font = Font(size=12, bold=True, color="B91C1C")
        curr_r += 1
        hazards = analysis_result["top_bottlenecks"]["line_stop_hazards"]
        if hazards:
            h_headers = ["품번", "품명", "저장위치", "발생일수", "누적결손(EA)", "온톨로지 회전구분", "회전일수(일)", "긴급 조치 권고"]
            for c_idx, h in enumerate(h_headers, 1):
                cell = ws_sum.cell(row=curr_r, column=c_idx, value=h)
                cell.fill = PatternFill(start_color="991B1B", end_color="991B1B", fill_type="solid")
                cell.font = header_font
                cell.alignment = align_center
                cell.border = thin_border
            curr_r += 1
            for hz in hazards:
                ws_sum.cell(row=curr_r, column=1, value=hz["material"]).font = alert_font
                ws_sum.cell(row=curr_r, column=2, value=hz["material_desc"]).font = regular_font
                ws_sum.cell(row=curr_r, column=3, value="/".join(hz["slocs"])).alignment = align_center
                ws_sum.cell(row=curr_r, column=4, value=f"{hz['occurrence_days']}일 ({hz['frequency_pct']}%)").alignment = align_center
                ws_sum.cell(row=curr_r, column=5, value=hz["total_deficit_qty"]).number_format = "#,##0"
                ws_sum.cell(row=curr_r, column=6, value=hz["mover_class"]).alignment = align_center
                ws_sum.cell(row=curr_r, column=7, value=hz["median_turnover_days"]).alignment = align_center
                ws_sum.cell(row=curr_r, column=8, value=hz["action_item"]).font = alert_font
                for c in range(1, 9):
                    ws_sum.cell(row=curr_r, column=c).fill = alert_fill
                    ws_sum.cell(row=curr_r, column=c).border = thin_border
                curr_r += 1
        else:
            ws_sum.cell(row=curr_r, column=1, value="현재 Fast Mover에 속하면서 만성 결손을 보이는 라인스탑 고위험 품목이 없습니다.").font = regular_font

        # ----------------------------------------------------------------------
        # Tab 2: 4-Quadrant Matrix (Detailed Material Master)
        # ----------------------------------------------------------------------
        ws_mat = wb.create_sheet(title="4_Quadrant_Matrix")
        ws_mat.views.sheetView[0].showGridLines = True

        mat_headers = [
            "우선순위", "사분면 구분", "품번", "품명", "저장위치", "발생일수", "발생빈도(%)", 
            "누적결손(EA)", "회당평균(EA)", "피크결손(EA)", "311해소수량(EA)", "해소율(%)", 
            "평균체류(일)", "최장체류(일)", "온톨로지 구분", "회전일수(일)", "라인스탑위험", "현장 조치 권고사항"
        ]
        for c_idx, h in enumerate(mat_headers, 1):
            cell = ws_mat.cell(row=1, column=c_idx, value=h)
            cell.fill = navy_header_fill
            cell.font = header_font
            cell.alignment = align_center
            cell.border = thin_border

        for r_idx, m in enumerate(analysis_result["materials"], 2):
            ws_mat.cell(row=r_idx, column=1, value=m["priority"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=2, value=m["quadrant_name"]).font = bold_font
            ws_mat.cell(row=r_idx, column=3, value=m["material"]).font = bold_font
            ws_mat.cell(row=r_idx, column=4, value=m["material_desc"]).font = regular_font
            ws_mat.cell(row=r_idx, column=5, value="/".join(m["slocs"])).alignment = align_center
            ws_mat.cell(row=r_idx, column=6, value=m["occurrence_days"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=7, value=f"{m['frequency_pct']:.1f}%").alignment = align_center
            ws_mat.cell(row=r_idx, column=8, value=m["total_deficit_qty"]).number_format = "#,##0"
            ws_mat.cell(row=r_idx, column=9, value=m["avg_deficit_qty"]).number_format = "#,##0"
            ws_mat.cell(row=r_idx, column=10, value=m["max_deficit_qty"]).number_format = "#,##0"
            ws_mat.cell(row=r_idx, column=11, value=m["transferred_qty"]).number_format = "#,##0"
            ws_mat.cell(row=r_idx, column=12, value=f"{m['resolvability_rate_pct']:.1f}%").alignment = align_center
            ws_mat.cell(row=r_idx, column=13, value=m["avg_aging_days"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=14, value=m["max_aging_days"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=15, value=m["mover_class"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=16, value=m["median_turnover_days"]).alignment = align_center
            ws_mat.cell(row=r_idx, column=17, value="🚨 위험" if m["is_line_stop_risk"] else "정상").alignment = align_center
            ws_mat.cell(row=r_idx, column=18, value=m["action_item"]).font = regular_font

            for c in range(1, len(mat_headers) + 1):
                ws_mat.cell(row=r_idx, column=c).border = thin_border
                if m["is_line_stop_risk"]:
                    ws_mat.cell(row=r_idx, column=c).fill = alert_fill

        # ----------------------------------------------------------------------
        # Tab 3: Top10 Bottlenecks
        # ----------------------------------------------------------------------
        ws_top = wb.create_sheet(title="Top10_Bottlenecks")
        ws_top.views.sheetView[0].showGridLines = True

        tb = analysis_result["top_bottlenecks"]
        
        # Section A: Top by Frequency
        ws_top.cell(row=1, column=1, value="📌 [Top 10] 최다 발생 빈도 품목 (만성 단골 백로그)").font = Font(size=12, bold=True, color="1E3A8A")
        top_h = ["순위", "품번", "품명", "발생일수", "발생빈도(%)", "누적결손(EA)", "온톨로지", "사분면"]
        for c_idx, h in enumerate(top_h, 1):
            c = ws_top.cell(row=2, column=c_idx, value=h)
            c.fill = teal_header_fill
            c.font = header_font
            c.alignment = align_center
            c.border = thin_border
        for rank, m in enumerate(tb["by_frequency"], 1):
            r = rank + 2
            ws_top.cell(row=r, column=1, value=rank).alignment = align_center
            ws_top.cell(row=r, column=2, value=m["material"]).font = bold_font
            ws_top.cell(row=r, column=3, value=m["material_desc"])
            ws_top.cell(row=r, column=4, value=m["occurrence_days"]).alignment = align_center
            ws_top.cell(row=r, column=5, value=f"{m['frequency_pct']:.1f}%").alignment = align_center
            ws_top.cell(row=r, column=6, value=m["total_deficit_qty"]).number_format = "#,##0"
            ws_top.cell(row=r, column=7, value=m["mover_class"]).alignment = align_center
            ws_top.cell(row=r, column=8, value=m["quadrant_name"])
            for c in range(1, 9):
                ws_top.cell(row=r, column=c).border = thin_border

        # Section B: Top by Volume
        start_row_b = len(tb["by_frequency"]) + 5
        ws_top.cell(row=start_row_b, column=1, value="📦 [Top 10] 최대 결손 수량 품목 (대량 결손)").font = Font(size=12, bold=True, color="1E3A8A")
        for c_idx, h in enumerate(["순위", "품번", "품명", "누적결손(EA)", "회당평균(EA)", "해소율(%)", "온톨로지", "사분면"], 1):
            c = ws_top.cell(row=start_row_b + 1, column=c_idx, value=h)
            c.fill = amber_header_fill
            c.font = header_font
            c.alignment = align_center
            c.border = thin_border
        for rank, m in enumerate(tb["by_volume"], 1):
            r = start_row_b + 1 + rank
            ws_top.cell(row=r, column=1, value=rank).alignment = align_center
            ws_top.cell(row=r, column=2, value=m["material"]).font = bold_font
            ws_top.cell(row=r, column=3, value=m["material_desc"])
            ws_top.cell(row=r, column=4, value=m["total_deficit_qty"]).number_format = "#,##0"
            ws_top.cell(row=r, column=5, value=m["avg_deficit_qty"]).number_format = "#,##0"
            ws_top.cell(row=r, column=6, value=f"{m['resolvability_rate_pct']:.1f}%").alignment = align_center
            ws_top.cell(row=r, column=7, value=m["mover_class"]).alignment = align_center
            ws_top.cell(row=r, column=8, value=m["quadrant_name"])
            for c in range(1, 9):
                ws_top.cell(row=r, column=c).border = thin_border

        # Auto-adjust column widths for all sheets
        for sheet in wb.worksheets:
            for col in sheet.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                for cell in col:
                    if cell.value:
                        val_str = str(cell.value)
                        # Han character length calculation
                        char_len = sum(2 if ord(char) > 127 else 1 for char in val_str)
                        if char_len > max_len:
                            max_len = char_len
                sheet.column_dimensions[col_letter].width = max(max_len + 3, 11)

        # Save Workbook
        today_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        if dry_run:
            if self.output_dir.resolve() != REPORTS_DIR.resolve():
                target_dir = self.output_dir
            else:
                target_dir = Path(__file__).resolve().parent / "scratch"
            target_dir.mkdir(parents=True, exist_ok=True)
            eff_prefix = filename_prefix if filename_prefix.startswith("DRYRUN_") else f"DRYRUN_{filename_prefix}"
            excel_path = target_dir / f"{eff_prefix}_{today_str}.xlsx"
        else:
            excel_path = self.output_dir / f"{filename_prefix}_{today_str}.xlsx"
        wb.save(excel_path)
        return excel_path

    def generate_markdown_summary(self, analysis_result: Dict[str, Any]) -> str:
        """Generates formatted markdown summary string."""
        p = analysis_result["period"]
        s = analysis_result["summary"]
        t = analysis_result["thresholds"]
        tb = analysis_result["top_bottlenecks"]

        lines = []
        lines.append(f"# 📊 {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} {_get_ops_env('SAP_PLANT', 'P001')} 백로그(COGI) 시계열 종합 분석 보고서")
        lines.append(f"> 분석 기간: `{p['start_date']}` ~ `{p['end_date']}` ({p['total_audit_days']}일간 적재) | 생성 시각: `{datetime.now().strftime('%Y-%m-%d %H:%M')}`\n")

        # KPI Highlights
        lines.append("## 🎯 핵심 요약 지표 (Executive KPIs)")
        lines.append(f"- **총 분석 품목수**: `{s['total_materials']:,}개 품번`")
        lines.append(f"- **누적 백로그 결손 수량**: `{s['total_cumulative_qty']:,.0f} EA`")
        lines.append(f"- **MB1B 311 창고잉여 자동 해소율**: `{s['overall_resolvability_pct']:.1f}%` (가짜 백로그 즉시 해소)")
        lines.append(f"- **🛑 라인스탑 고위험 품목 (Fast Mover 만성 결손)**: **{s['line_stop_hazard_count']}건**\n")

        # 4-Quadrant Matrix Table
        lines.append("## 🧭 4분면(Quadrant) 매트릭스 분류 현황")
        lines.append("| 사분면 구분 | 정의 및 특징 | 품목수 | 누적 결손(EA) | 주요 권고사항 |")
        lines.append("| :--- | :--- | :---: | :---: | :--- |")
        lines.append(f"| **🚨 Q1. 만성 대량 결손** | 고빈도 + 대량 결손 (최우선 병목) | **{s['q1_count']}** | {sum(m['total_deficit_qty'] for m in analysis_result['quadrant_summary']['Q1_CHRONIC_MASSIVE']):,.0f} | 안전재고 상향 + 입고/보충 주기 전면 재검토 |")
        lines.append(f"| **⚠️ Q2. 만성 운영 지체** | 고빈도 + 소량 결손 (창고 불출 지연) | **{s['q2_count']}** | {sum(m['total_deficit_qty'] for m in analysis_result['quadrant_summary']['Q2_CHRONIC_OPERATIONAL']):,.0f} | 창고-현장 자동보충(Min-Max) 및 피킹 개선 |")
        lines.append(f"| **⚡ Q3. 돌발 대형 결손** | 저빈도 + 대량 결손 (공급사 결품) | **{s['q3_count']}** | {sum(m['total_deficit_qty'] for m in analysis_result['quadrant_summary']['Q3_CRITICAL_SURGE']):,.0f} | 공급사 납기(ETA) 및 선적 품질 집중 추적 |")
        lines.append(f"| **ℹ️ Q4. 경미 일시 결손** | 저빈도 + 소량 결손 (일반 루틴) | **{s['q4_count']}** | {sum(m['total_deficit_qty'] for m in analysis_result['quadrant_summary']['Q4_MINOR_INFREQUENT']):,.0f} | 일일 06/08시 자동 이동전기 루틴 유지 |")
        lines.append("")

        # Top Bottlenecks
        lines.append("## 🏆 주요 병목 품목 Top 5 (Top Bottlenecks)")
        lines.append("### 1. 최다 발생 빈도 (만성 반복 품목)")
        for idx, m in enumerate(tb["by_frequency"][:5], 1):
            lines.append(f"{idx}. **{m['material']}** ({m['material_desc'][:30]}...)")
            lines.append(f"   - 발생: **{m['occurrence_days']}일** ({m['frequency_pct']}%) | 누적결손: `{m['total_deficit_qty']:,.0f} EA` | 온톨로지: `{m['mover_class']}`")
        lines.append("")

        lines.append("### 2. 최대 결손 수량 (대량 결손 품목)")
        for idx, m in enumerate(tb["by_volume"][:5], 1):
            lines.append(f"{idx}. **{m['material']}** ({m['material_desc'][:30]}...)")
            lines.append(f"   - 누적결손: **{m['total_deficit_qty']:,.0f} EA** | 311해소율: `{m['resolvability_rate_pct']:.1f}%` | 온톨로지: `{m['mover_class']}`")
        lines.append("")

        return "\n".join(lines)


def np_mean_safe(arr: List[float]) -> float:
    return float(sum(arr) / len(arr)) if arr else 0.0
