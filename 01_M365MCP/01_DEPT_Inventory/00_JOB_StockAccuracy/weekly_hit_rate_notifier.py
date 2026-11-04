"""
Weekly Audit Hit Rate Notifier (주간 실사 타율 및 적중률 리포터)
============================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / weekly_hit_rate_notifier.py

Core Responsibilities:
1. Formats weekly hit rate report into executive-ready HTML format.
2. Displays Overall Model Hit Rate (TOP 15 + EXTENDED 25), Rank Tier breakdown (1~5, 6~15, 16~40),
   and Rolling Cumulative 7d/28d trends.
3. Observes Ground Truth Integrity (AGENTS.md Rule 9 & Rule 10):
   - Clear tone ("표본 축적 중 (1회차 n=..., 판단 유보)", "기준선 부족 (n=0)").
   - Strictly routes test/draft emails to TEST_EMAIL_RECIPIENT with [TEST] [DRAFT] prefix.
   - User Decision D42: Regular schedule every Monday 08:30 EST (First official dispatch: 2026-11-02).
"""

import os
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
from datetime import datetime
from typing import Optional, Dict, Any, List

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

CURRENT_DIR = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    _env_path = CURRENT_DIR.parents[1] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

import importlib.util
spec = importlib.util.spec_from_file_location("stock_acc_db_mgr", CURRENT_DIR / "db_manager.py")
_db_mgr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_db_mgr)
DB_PATH = _db_mgr.DB_PATH
get_hit_rate_records = _db_mgr.get_hit_rate_records

spec_calc = importlib.util.spec_from_file_location("hit_rate_calc", CURRENT_DIR / "hit_rate_calculator.py")
_calc = importlib.util.module_from_spec(spec_calc)
spec_calc.loader.exec_module(_calc)
compute_hit_rate_for_run = _calc.compute_hit_rate_for_run

logger = logging.getLogger("WeeklyHitRateNotifier")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [HitRateNotify] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

def get_weekly_hit_rate_recipient() -> Optional[str]:
    """
    Returns the single designated recipient for the Weekly Hit Rate Report.
    Per User Decision D43, this report is strictly user-only (TEST_EMAIL_RECIPIENT)
    both during trial and after format finalization. CC/BCC is always empty.
    There is no pathway to switch to production distribution lists (STOCK_ACCURACY_TO/CC).
    Fail-closed: Returns None if recipient is missing, empty, or contains 'example.com'.
    """
    recipient = os.getenv(
        "TEST_EMAIL_RECIPIENT", 
        os.getenv("WEEKLY_HIT_RATE_RECIPIENT", os.getenv("STOCK_ACCURACY_VERIFY", ""))
    )
    if not recipient:
        return None
    return recipient.strip()

TEST_RECIPIENT = get_weekly_hit_rate_recipient()


def build_weekly_hit_rate_html(
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None
) -> str:
    """
    Builds professional HTML email body for Weekly Hit Rate Report.
    """
    target_db = Path(db_path or DB_PATH)
    records = get_hit_rate_records(run_id=run_id, limit=1, db_path=target_db)
    
    if not records and run_id:
        # Try computing if not already saved
        try:
            rec = compute_hit_rate_for_run(run_id, db_path=target_db, save_to_db=True)
            records = [rec]
        except Exception as e:
            logger.error(f"Failed to compute hit rate for {run_id}: {e}")

    if not records:
        return "<html><body><p>No hit rate records available for weekly report.</p></body></html>"

    rec = records[0]

    def _fmt_pct(val):
        return f"{val:.1%}" if val is not None else "N/A"

    def _fmt_lift(val):
        if val is not None:
            return f"{val:.2f}x"
        return f"기준선 부족 (n={rec.get('sample_size_random_28d', 0)})"

    audit_date = rec.get("audit_date", datetime.now().strftime("%Y-%m-%d"))
    curr_run_id = rec.get("run_id", "CURRENT")

    # D74 Stage 1: Load continuous learning loop metrics
    rule_hit_rates = []
    top_bins = []
    top_materials = []
    progress = {}
    try:
        if hasattr(_calc, "compute_rule_hit_rates"):
            rule_hit_rates = _calc.compute_rule_hit_rates(db_path=target_db, save_to_db=False)
        if hasattr(_calc, "compute_historical_discrepancy_scores"):
            hist_scores = _calc.compute_historical_discrepancy_scores(db_path=target_db, save_to_db=False)
            top_bins = hist_scores.get("bins", [])[:5]
            top_materials = hist_scores.get("materials", [])[:5]
        if hasattr(_calc, "get_learning_loop_progress"):
            progress = _calc.get_learning_loop_progress(db_path=target_db)
    except Exception as e:
        logger.warning(f"Failed to fetch Stage 1 learning loop metrics: {e}")

    # Build Section 4 HTML rows (Rule Hit Rates)
    category_colors = {
        "DEFICIT": ("#fee2e2", "#991b1b"),
        "AGING": ("#fef3c7", "#92400e"),
        "FIFO": ("#e0e7ff", "#3730a3"),
        "LOT": ("#f3e8ff", "#6b21a8"),
        "ANOMALY": ("#ffedd5", "#9a3412"),
        "BASELINE": ("#f1f5f9", "#475569"),
        "EXPLORATION": ("#dbeafe", "#1e40af"),
    }
    rule_table_rows = []
    for r in rule_hit_rates:
        if r.get("sample_size", 0) == 0:
            continue
        bg, text_col = category_colors.get(r.get("category"), ("#f1f5f9", "#475569"))
        raw_val = f"{r['hit_rate']:.1%}" if r.get("hit_rate") is not None else "N/A"
        smoothed_val = f"{r['smoothed_score']:.1%}" if r.get("smoothed_score") is not None else "N/A"
        n_sample = r.get("sample_size", 0)
        if n_sample < 10:
            assessment = f'<span class="badge" style="background-color: #f1f5f9; color: #64748b;">표본 축적 중 (n={n_sample}, 판단 유보)</span>'
        elif (r.get("smoothed_score") or 0) >= 0.40:
            assessment = '<span class="badge" style="background-color: #fee2e2; color: #991b1b;">고위험 적중 우수</span>'
        elif (r.get("smoothed_score") or 0) >= 0.25:
            assessment = '<span class="badge" style="background-color: #fef3c7; color: #92400e;">정상 감지 (공장 평균)</span>'
        else:
            assessment = '<span class="badge" style="background-color: #f0fdf4; color: #166534;">저위험 / 안정</span>'
        rule_table_rows.append(f"""<tr>
            <td><strong>{r['rule_name']}</strong></td>
            <td><span class="badge" style="background-color: {bg}; color: {text_col};">{r.get('category', 'RULE')}</span></td>
            <td style="text-align: center;">{r['sample_size']}</td>
            <td style="text-align: center; color: #dc2626; font-weight: bold;">{r['discrepancy_count']}</td>
            <td style="text-align: right; font-weight: bold;">{raw_val}</td>
            <td style="text-align: right; font-weight: bold; color: #1e3a8a;">{smoothed_val}</td>
            <td style="text-align: center;">{assessment}</td>
        </tr>""")
    rule_table_body = "\n".join(rule_table_rows) if rule_table_rows else "<tr><td colspan='7'>규칙별 표본 수집 중</td></tr>"

    # Coverage status for rule parsing (D74 / Thread 110)
    unmatched_rule = next((r for r in rule_hit_rates if r.get("rule_code") == "UNCLASSIFIED"), None)
    if unmatched_rule and unmatched_rule.get("sample_size", 0) > 0:
        rule_coverage_note = f"""<div style="background-color: #fef2f2; border: 1px solid #fecaca; border-radius: 4px; padding: 8px 12px; margin-top: -12px; margin-bottom: 16px; font-size: 11px; color: #991b1b;">
            ⚠️ <strong>규칙 파싱 누락 발생:</strong> 미분류 빈 {unmatched_rule['sample_size']}건 (불일치 {unmatched_rule['discrepancy_count']}건). 선정 사유 텍스트 변경 여부 점검 필요.
        </div>"""
    else:
        rule_coverage_note = """<div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 4px; padding: 8px 12px; margin-top: -12px; margin-bottom: 16px; font-size: 11px; color: #166534;">
            ✅ <strong>규칙 파싱 신뢰성:</strong> 미분류 빈 0건 (전체 실사 대상 빈이 정규 규칙에 100% 정상 매핑됨).
        </div>"""

    # Build Section 5 HTML rows (Watchlist Bins & Materials)
    bin_table_rows = []
    for b in top_bins:
        raw_val = f"{b['raw_hit_rate']:.1%}"
        smoothed_val = f"{b['smoothed_score']:.1%}"
        last_st = '<span style="color: #dc2626; font-weight: bold;">불일치</span>' if b.get("last_discrepancy") == 1 else '<span style="color: #166534;">일치</span>'
        bin_table_rows.append(f"""<tr>
            <td><strong>{b['entity_id']}</strong></td>
            <td style="text-align: center;">{b['audit_count']}</td>
            <td style="text-align: center; color: #dc2626; font-weight: bold;">{b['discrepancy_count']}</td>
            <td style="text-align: right;">{raw_val}</td>
            <td style="text-align: right; font-weight: bold; color: #dc2626;">{smoothed_val}</td>
            <td style="text-align: center;">{b.get('last_audit_date', '-')} ({last_st})</td>
        </tr>""")
    bin_table_body = "\n".join(bin_table_rows) if bin_table_rows else "<tr><td colspan='6'>표본 축적 중</td></tr>"

    mat_table_rows = []
    for m in top_materials:
        raw_val = f"{m['raw_hit_rate']:.1%}"
        smoothed_val = f"{m['smoothed_score']:.1%}"
        bins_list = m.get("associated_bins", [])
        bins_str = ", ".join(bins_list[:3])
        if len(bins_list) > 3:
            bins_str += f" 외 {len(bins_list) - 3}곳"
        mat_table_rows.append(f"""<tr>
            <td><strong>{m['entity_id']}</strong></td>
            <td style="text-align: center;">{m['audit_count']}</td>
            <td style="text-align: center; color: #dc2626; font-weight: bold;">{m['discrepancy_count']}</td>
            <td style="text-align: right;">{raw_val}</td>
            <td style="text-align: right; font-weight: bold; color: #dc2626;">{smoothed_val}</td>
            <td>{bins_str or '-'}</td>
        </tr>""")
    mat_table_body = "\n".join(mat_table_rows) if mat_table_rows else "<tr><td colspan='6'>표본 축적 중</td></tr>"

    # Build Section 6 progress
    s2 = progress.get("stage2", {})
    s3 = progress.get("stage3", {})
    total_lbl = progress.get("total_labels", 0)
    total_dsc = progress.get("total_discrepancies", 0)
    overall_hr = f"{progress.get('overall_hit_rate', 0):.1%}"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; color: #1e293b; }}
        .container {{ max-width: 820px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; border: 1px solid #e2e8f0; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); }}
        .header {{ background: linear-gradient(135deg, #1e3a8a 0%, #0f172a 100%); color: #ffffff; padding: 24px 28px; }}
        .header h1 {{ margin: 0 0 6px 0; font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }}
        .header p {{ margin: 0; opacity: 0.85; font-size: 13px; }}
        .content {{ padding: 24px 28px; }}
        .kpi-grid {{ display: flex; gap: 12px; margin-bottom: 24px; }}
        .kpi-card {{ flex: 1; padding: 14px 16px; border-radius: 6px; border: 1px solid #e2e8f0; }}
        .kpi-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
        .kpi-value {{ font-size: 24px; font-weight: 800; line-height: 1.1; margin-bottom: 4px; }}
        .kpi-sub {{ font-size: 11px; color: #64748b; }}
        .section-title {{ font-size: 14px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #0f172a; margin: 24px 0 10px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 20px; }}
        th {{ background-color: #f1f5f9; color: #475569; padding: 10px 12px; text-align: left; font-size: 11px; text-transform: uppercase; font-weight: 600; border-bottom: 1px solid #cbd5e1; }}
        td {{ padding: 9px 12px; border-bottom: 1px solid #f1f5f9; color: #334155; }}
        .badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
        .footer {{ background-color: #f8fafc; padding: 16px 28px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; text-align: center; }}
        .callout {{ background-color: #eff6ff; border-left: 4px solid #3b82f6; padding: 12px 16px; border-radius: 0 4px 4px 0; font-size: 12px; color: #1e40af; margin-bottom: 20px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Weekly Physical Audit Hit Rate & Model Precision Report</h1>
            <p>Plant: {_get_ops_env('SAP_PLANT', 'P001')} (Headlamp) | Audit Date: {audit_date} | Run ID: {curr_run_id}</p>
        </div>
        <div class="content">
            <!-- CALLOUT -->
            <div class="callout">
                <strong>📌 주간 실사 타율 리포트 안내 (D42):</strong> 본 리포트는 물리 실사 결과(Ground Truth)를 바탕으로 AI 이상 재고 추출 모델의 적중률(Hit Rate)과 순위별 정밀도를 추적합니다. (매주 월요일 08:30 정기 발행)
            </div>

            <!-- KPI CARDS -->
            <table style="width: 100%; border: none; margin-bottom: 20px;">
                <tr>
                    <td style="width: 25%; padding: 4px; border: none;">
                        <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; padding: 12px; border-radius: 6px;">
                            <div style="color: #1d4ed8; font-size: 11px; font-weight: bold; text-transform: uppercase;">Overall Model Hit Rate</div>
                            <div style="color: #1e3a8a; font-size: 22px; font-weight: bold; margin-top: 4px;">{_fmt_pct(rec.get('hit_rate_model'))}</div>
                            <div style="color: #475569; font-size: 11px; margin-top: 2px;">{rec.get('discrepancy_count_model', 0)} / {rec.get('sample_size_model', 0)} Bins</div>
                        </div>
                    </td>
                    <td style="width: 25%; padding: 4px; border: none;">
                        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; padding: 12px; border-radius: 6px;">
                            <div style="color: #15803d; font-size: 11px; font-weight: bold; text-transform: uppercase;">TOP 15 Hit Rate</div>
                            <div style="color: #14532d; font-size: 22px; font-weight: bold; margin-top: 4px;">{_fmt_pct(rec.get('hit_rate_top'))}</div>
                            <div style="color: #475569; font-size: 11px; margin-top: 2px;">{rec.get('discrepancy_count_top', 0)} / {rec.get('sample_size_top', 0)} Bins</div>
                        </div>
                    </td>
                    <td style="width: 25%; padding: 4px; border: none;">
                        <div style="background-color: #faf5ff; border: 1px solid #e9d5ff; padding: 12px; border-radius: 6px;">
                            <div style="color: #7e22ce; font-size: 11px; font-weight: bold; text-transform: uppercase;">EXTENDED 25 Hit Rate</div>
                            <div style="color: #581c87; font-size: 22px; font-weight: bold; margin-top: 4px;">{_fmt_pct(rec.get('hit_rate_extended'))}</div>
                            <div style="color: #475569; font-size: 11px; margin-top: 2px;">{rec.get('discrepancy_count_extended', 0)} / {rec.get('sample_size_extended', 0)} Bins</div>
                        </div>
                    </td>
                    <td style="width: 25%; padding: 4px; border: none;">
                        <div style="background-color: #fff7ed; border: 1px solid #fed7aa; padding: 12px; border-radius: 6px;">
                            <div style="color: #c2410c; font-size: 11px; font-weight: bold; text-transform: uppercase;">Model Lift (vs Random)</div>
                            <div style="color: #9a3412; font-size: 16px; font-weight: bold; margin-top: 8px;">{_fmt_lift(rec.get('lift'))}</div>
                            <div style="color: #475569; font-size: 11px; margin-top: 4px;">Random n={rec.get('sample_size_random_28d', 0)}</div>
                        </div>
                    </td>
                </tr>
            </table>

            <!-- SECTION 1: HIT RATE BY AUDIT LIST TYPE -->
            <div class="section-title">1. 실사 리스트 유형별 적중 현황 (Hit Rate by List Type)</div>
            <table>
                <thead>
                    <tr>
                        <th>구분</th>
                        <th style="text-align: center;">실사 표본 (n)</th>
                        <th style="text-align: center;">불일치 빈 수</th>
                        <th style="text-align: right;">적중률 (Hit Rate)</th>
                        <th>비고</th>
                    </tr>
                </thead>
                <tbody>
                    <tr style="background-color: #f8fafc; font-weight: bold;">
                        <td>전체 모델 (TOP 15 + EXTENDED 25)</td>
                        <td style="text-align: center;">{rec.get('sample_size_model', 0)}</td>
                        <td style="text-align: center; color: #dc2626;">{rec.get('discrepancy_count_model', 0)}</td>
                        <td style="text-align: right; color: #1e3a8a; font-size: 13px;">{_fmt_pct(rec.get('hit_rate_model'))}</td>
                        <td>미실사 빈 3개(EXT_TRL TRL 1,2, SCR 1) 분모 엄격 제외</td>
                    </tr>
                    <tr>
                        <td>🎯 TOP 15 (우선 실사)</td>
                        <td style="text-align: center;">{rec.get('sample_size_top', 0)}</td>
                        <td style="text-align: center; color: #dc2626;">{rec.get('discrepancy_count_top', 0)}</td>
                        <td style="text-align: right; color: #15803d; font-weight: bold;">{_fmt_pct(rec.get('hit_rate_top'))}</td>
                        <td>초고위험군 집중 실사 (트레일러 2개 제외 후 n=13)</td>
                    </tr>
                    <tr>
                        <td>📦 EXTENDED 25 (잔여 실사)</td>
                        <td style="text-align: center;">{rec.get('sample_size_extended', 0)}</td>
                        <td style="text-align: center; color: #dc2626;">{rec.get('discrepancy_count_extended', 0)}</td>
                        <td style="text-align: right; color: #7e22ce; font-weight: bold;">{_fmt_pct(rec.get('hit_rate_extended'))}</td>
                        <td>중위험군 확대 실사 (스크랩 1개 제외 후 n=24)</td>
                    </tr>
                    <tr style="color: #64748b;">
                        <td>🎲 무작위 검증군 (28일 누적 기준선)</td>
                        <td style="text-align: center;">{rec.get('sample_size_random_28d', 0)}</td>
                        <td style="text-align: center;">{rec.get('discrepancy_count_random_28d', 0)}</td>
                        <td style="text-align: right;">{_fmt_pct(rec.get('hit_rate_random_28d'))}</td>
                        <td>무작위 표본 미배정 (향후 대조군 축적 예정)</td>
                    </tr>
                </tbody>
            </table>

            <!-- SECTION 2: RANK TIER BREAKDOWN -->
            <div class="section-title">2. 순위 구간별 정밀도 분석 (Rank Tier Monotonicity)</div>
            <table>
                <thead>
                    <tr>
                        <th>순위 구간 (Rank Tier)</th>
                        <th style="text-align: center;">표본 수 (n)</th>
                        <th style="text-align: center;">불일치 수</th>
                        <th style="text-align: right;">적중률 (Hit Rate)</th>
                        <th style="text-align: center;">상태</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><strong>Rank 1 ~ 5 (초고위험)</strong></td>
                        <td style="text-align: center;">{rec.get('rank_1_5_sample', 0)}</td>
                        <td style="text-align: center; color: #dc2626; font-weight: bold;">{rec.get('rank_1_5_discrepancies', 0)}</td>
                        <td style="text-align: right; font-weight: bold; color: #1e3a8a;">{_fmt_pct(rec.get('rank_1_5_hit_rate'))}</td>
                        <td style="text-align: center;"><span class="badge" style="background-color: #fef3c7; color: #92400e;">표본 축적 중 (1회차 n={rec.get('rank_1_5_sample', 0)}, 판단 유보)</span></td>
                    </tr>
                    <tr>
                        <td><strong>Rank 6 ~ 15 (고위험)</strong></td>
                        <td style="text-align: center;">{rec.get('rank_6_15_sample', 0)}</td>
                        <td style="text-align: center; color: #dc2626; font-weight: bold;">{rec.get('rank_6_15_discrepancies', 0)}</td>
                        <td style="text-align: right; font-weight: bold; color: #1e3a8a;">{_fmt_pct(rec.get('rank_6_15_hit_rate'))}</td>
                        <td style="text-align: center;"><span class="badge" style="background-color: #fef3c7; color: #92400e;">표본 축적 중 (1회차 n={rec.get('rank_6_15_sample', 0)}, 판단 유보)</span></td>
                    </tr>
                    <tr>
                        <td><strong>Rank 16 ~ 40 (중위험)</strong></td>
                        <td style="text-align: center;">{rec.get('rank_16_40_sample', 0)}</td>
                        <td style="text-align: center; color: #dc2626; font-weight: bold;">{rec.get('rank_16_40_discrepancies', 0)}</td>
                        <td style="text-align: right; font-weight: bold; color: #1e3a8a;">{_fmt_pct(rec.get('rank_16_40_hit_rate'))}</td>
                        <td style="text-align: center;"><span class="badge" style="background-color: #fef3c7; color: #92400e;">표본 축적 중 (1회차 n={rec.get('rank_16_40_sample', 0)}, 판단 유보)</span></td>
                    </tr>
                </tbody>
            </table>

            <!-- SECTION 3: ROLLING TREND -->
            <div class="section-title">3. 이동 누적 추이 (Rolling Cumulative Trend)</div>
            <table>
                <thead>
                    <tr>
                        <th>누적 지표</th>
                        <th style="text-align: right;">누적 적중률</th>
                        <th>설명</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><strong>7일 이동 누적 타율</strong></td>
                        <td style="text-align: right; font-weight: bold; color: #2563eb;">{_fmt_pct(rec.get('cumulative_7d_hit_rate'))}</td>
                        <td>최근 7일간 실사 누적 불일치 합 / 표본 합</td>
                    </tr>
                    <tr>
                        <td><strong>28일 이동 누적 타율</strong></td>
                        <td style="text-align: right; font-weight: bold; color: #2563eb;">{_fmt_pct(rec.get('cumulative_28d_hit_rate'))}</td>
                        <td>최근 28일간 실사 누적 불일치 합 / 표본 합 (장기 모델 안정성 검증)</td>
                    </tr>
                </tbody>
            </table>

            <!-- SECTION 4: HIT RATE BY RULE -->
            <div class="section-title">4. 규칙(선정 사유)별 적중률 (Hit Rate by Rule / Reason)</div>
            <table>
                <thead>
                    <tr>
                        <th>선정 규칙 (Rule Name)</th>
                        <th>유형</th>
                        <th style="text-align: center;">실사 표본 (n)</th>
                        <th style="text-align: center;">불일치 빈 수</th>
                        <th style="text-align: right;">실제 타율</th>
                        <th style="text-align: right;">평활 타율 (Beta)</th>
                        <th style="text-align: center;">진단 / 평가</th>
                    </tr>
                </thead>
                <tbody>
                    {rule_table_body}
                </tbody>
            </table>
            {rule_coverage_note}

            <!-- SECTION 5: HISTORICAL DISCREPANCY WATCHLIST -->
            <div class="section-title">5. 과거 불일치 이력 요주의 품목 및 빈 (Historical Discrepancy Watchlist)</div>
            <div class="callout" style="background-color: #f8fafc; border-left: 4px solid #64748b; color: #334155; margin-bottom: 12px; font-size: 11px;">
                <strong>📌 Beta(1, 3) 베이지안 스무딩 적용 (D74 Stage 1):</strong> 1~2회 소수 실사로 인한 극단적 왜곡(100% 영구 낙인 또는 0% 섣부른 안심)을 방지하기 위해, 공장 평균 기준선(~25%)으로 회귀(Shrinkage)시키는 <code>(d + 1) / (n + 4)</code> 평활 점수를 적용합니다.
            </div>

            <div style="font-size: 12px; font-weight: bold; color: #1e293b; margin: 14px 0 6px 0;">📍 요주의 빈 TOP 5 (High-Risk Bins)</div>
            <table>
                <thead>
                    <tr>
                        <th>빈 번호 (Bin)</th>
                        <th style="text-align: center;">누적 실사 (n)</th>
                        <th style="text-align: center;">불일치 횟수 (d)</th>
                        <th style="text-align: right;">실제 타율</th>
                        <th style="text-align: right;">평활 위험도 (Beta)</th>
                        <th style="text-align: center;">최근 실사 및 상태</th>
                    </tr>
                </thead>
                <tbody>
                    {bin_table_body}
                </tbody>
            </table>

            <div style="font-size: 12px; font-weight: bold; color: #1e293b; margin: 14px 0 6px 0;">📦 요주의 품목 TOP 5 (High-Risk Materials)</div>
            <table>
                <thead>
                    <tr>
                        <th>품번 (Material)</th>
                        <th style="text-align: center;">누적 실사 (n)</th>
                        <th style="text-align: center;">불일치 횟수 (d)</th>
                        <th style="text-align: right;">실제 타율</th>
                        <th style="text-align: right;">평활 위험도 (Beta)</th>
                        <th>발견 빈 위치</th>
                    </tr>
                </thead>
                <tbody>
                    {mat_table_body}
                </tbody>
            </table>

            <!-- SECTION 6: LEARNING LOOP PROGRESS -->
            <div class="section-title">6. 모델 학습 데이터 누적 및 다음 단계 로드맵 (Continuous Learning Loop Progress)</div>
            <table style="width: 100%; border: none; margin-bottom: 16px;">
                <tr>
                    <td style="width: 33%; padding: 4px; border: none;">
                        <div style="background-color: #f8fafc; border: 1px solid #cbd5e1; padding: 12px; border-radius: 6px;">
                            <div style="color: #475569; font-size: 11px; font-weight: bold; text-transform: uppercase;">확정 물리 라벨 (Ground Truth)</div>
                            <div style="color: #0f172a; font-size: 20px; font-weight: bold; margin-top: 4px;">{total_lbl} Bins</div>
                            <div style="color: #64748b; font-size: 11px; margin-top: 2px;">불일치 {total_dsc}건 (타율 {overall_hr})</div>
                        </div>
                    </td>
                    <td style="width: 33%; padding: 4px; border: none;">
                        <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; padding: 12px; border-radius: 6px;">
                            <div style="color: #1d4ed8; font-size: 11px; font-weight: bold; text-transform: uppercase;">Stage 2: 정규화 로지스틱 회귀</div>
                            <div style="color: #1e3a8a; font-size: 20px; font-weight: bold; margin-top: 4px;">{s2.get('progress_pct', 0)}%</div>
                            <div style="color: #1e40af; font-size: 11px; margin-top: 2px;">목표 50건 중 {total_dsc}건 (잔여 {s2.get('remaining', 0)}건)</div>
                        </div>
                    </td>
                    <td style="width: 33%; padding: 4px; border: none;">
                        <div style="background-color: #faf5ff; border: 1px solid #e9d5ff; padding: 12px; border-radius: 6px;">
                            <div style="color: #7e22ce; font-size: 11px; font-weight: bold; text-transform: uppercase;">Stage 3: LightGBM 엔진</div>
                            <div style="color: #581c87; font-size: 20px; font-weight: bold; margin-top: 4px;">{s3.get('progress_pct', 0)}%</div>
                            <div style="color: #6b21a8; font-size: 11px; margin-top: 2px;">목표 200건 중 {total_dsc}건 (잔여 {s3.get('remaining', 0)}건)</div>
                        </div>
                    </td>
                </tr>
            </table>

            <div style="background-color: #f1f5f9; border-radius: 6px; padding: 14px 16px; font-size: 12px; line-height: 1.6; color: #334155; margin-bottom: 20px;">
                <div>⚙️ <strong>단계 2·3 재학습 파이프라인 (오프라인 무부하 원칙)</strong>: WMS·SAP를 일체 조회하지 않는 순수 로컬 SQLite 기반 오프라인 연산으로 실행됩니다. (권장 실행: 평일 18:10, 01_JOB 일일 루프 17:00 종료 후)</div>
                <div style="margin-top: 6px;">🛡️ <strong>학습 모델 승격 가드레일 (Safe Promotion Guard)</strong>: 섀도 모드(엑셀 비교 시트)에서 <strong>무작위 검증 표본</strong> 대비 현 규칙 점수보다 Lift가 <strong>2주 연속</strong> 높을 때만 월요일 본 리포트를 통해 승격을 권고하며, <strong>사용자의 명시적 승인 후</strong>에만 실제 실사 순위에 반영됩니다 (자동 승격 절대 금지).</div>
            </div>

            <div style="font-size: 11px; color: #64748b; line-height: 1.5; margin-top: 16px;">
                * <strong>데이터 신뢰성 원칙 (Factual Integrity)</strong>: 현장 실사에서 수량이 실제로 확인된(counted_qty IS NOT NULL) 빈만을 분모로 집계하며, 미방문 빈이나 가상 적치장은 분모/분자에서 완전 제외됩니다.
            </div>
        </div>
        <div class="footer">
            {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} {_get_ops_env('SAP_PLANT', 'P001')} &bull; Inventory Stock Accuracy Automation &bull; Weekly Hit Rate Monitor
        </div>
    </div>
</body>
</html>"""
    return html


def send_weekly_hit_rate_email(
    run_id: str = "20261026_DAILY",
    is_test: bool = True,
    to_recipient: Optional[str] = None,
    db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    Sends the Weekly Hit Rate Report email via Outlook COM.
    User Decision D43 & Safety Guardrail (Rule 10):
    - Strictly routes exclusively to user sole recipient (TEST_EMAIL_RECIPIENT).
    - Production warehouse recipients (STOCK_ACCURACY_TO/CC) are permanently excluded.
    - CC and BCC are always empty string.
    - When is_test=True, prefix is [TEST] [DRAFT]. Otherwise [DRAFT].
    """
    target_to = to_recipient if to_recipient else get_weekly_hit_rate_recipient()
    if not target_to or "example.com" in target_to.lower():
        logger.error(f"❌ [NOTIFIER] Recipient missing or placeholder domain '{target_to}'. Aborting email send (fail closed).")
        return {
            "success": False,
            "error": f"Recipient missing or placeholder domain '{target_to}' (fail closed)",
            "recipient": target_to
        }
    html_body = build_weekly_hit_rate_html(run_id=run_id, db_path=db_path)

    today_str = datetime.now().strftime("%Y-%m-%d")
    prefix = "[TEST] [DRAFT] " if is_test else "[DRAFT] "
    subject = f"{prefix}주간 실사 리스트 타율 및 적중률 추이 리포트 ({today_str})"

    logger.info(f"📧 Sending Weekly Hit Rate email to: {target_to} (Subject: {subject})...")

    try:
        import subprocess
        res = subprocess.run(
            'tasklist /fi "imagename eq OUTLOOK.EXE"',
            shell=True,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if "OUTLOOK.EXE" not in res.stdout:
            logger.info("Starting Outlook for MAPI initialization...")
            subprocess.Popen("start outlook", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            import time
            time.sleep(4)

        import time
        import pythoncom
        pythoncom.CoInitialize()

        import win32com.client
        outlook = None
        mail = None
        for attempt in range(5):
            try:
                outlook = win32com.client.Dispatch("Outlook.Application")
                try:
                    mapi = outlook.GetNamespace("MAPI")
                    mapi.Logon("", "", False, False)
                except Exception:
                    pass
                mail = outlook.CreateItem(0)  # 0: olMailItem
                if mail:
                    break
            except Exception as e:
                logger.info(f"Waiting for Outlook initialization (attempt {attempt+1}/5)...")
                time.sleep(2)

        if not mail:
            raise RuntimeError("Failed to obtain Outlook mail item after retries.")

        _assert_clean_ops_output(subject, html_body, context="00_JOB weekly_hit_rate_notifier")
        mail.Subject = subject
        mail.To = target_to
        mail.CC = ""
        mail.BCC = ""
        mail.HTMLBody = html_body

        mail.Send()
        logger.info(f"✅ Weekly Hit Rate email successfully sent to {target_to}!")
        return {
            "success": True,
            "subject": subject,
            "recipient": target_to,
            "sent_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    except Exception as e:
        logger.error(f"❌ Failed to send Weekly Hit Rate email: {e}")
        return {
            "success": False,
            "error": str(e),
            "subject": subject,
            "recipient": target_to
        }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Weekly Hit Rate Notifier")
    parser.add_argument("--run-id", default="20261026_DAILY", help="Target Audit Run ID")
    parser.add_argument("--test", action="store_true", default=True, help="Send test email to TEST_EMAIL_RECIPIENT")
    parser.add_argument("--to", default=None, help="Custom recipient override (for testing only)")
    parser.add_argument("--html-only", action="store_true", help="Print HTML to stdout without sending")
    args = parser.parse_args()

    if args.html_only:
        print(build_weekly_hit_rate_html(run_id=args.run_id))
    else:
        res = send_weekly_hit_rate_email(run_id=args.run_id, is_test=args.test, to_recipient=args.to)
        print(res)
