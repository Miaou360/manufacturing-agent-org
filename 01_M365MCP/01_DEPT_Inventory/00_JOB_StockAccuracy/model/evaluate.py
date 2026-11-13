"""
Offline Evaluation Engine for Stock Accuracy Models (Walk-Forward Split)
========================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / model / evaluate.py

Evaluates and compares:
1. Baseline 1: Domain Rule Score (bin_risk_score)
2. Baseline 2: Shadow Expected Value Score (priority_ev)
3. Candidate Model: LightGBM ML Classifier (if positive sample guard >= 200 is met)

Core Evaluation Metrics:
- precision@15: Top 15 rapid floor count accuracy
- precision@40: Top 40 daily total count accuracy
- recall@15, recall@40: Discrepancy capture rate
- Discovered Loss per Minute (작업분당 기대 발견 금액)
- Random Validation Error Rate (무작위 표본군 오차율 - Inverse Probability Weighted)
- Low-Risk Unselected Floor Error Rate (저위험·미선택군 오차율)

Outputs:
- model/reports/evaluation_summary_%Y%m%d_%H%M%S.md
- model/reports/evaluation_metrics_%Y%m%d_%H%M%S.csv
"""

import sys
import os

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union
import pandas as pd
import numpy as np

# Internal Imports
CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(CURRENT_DIR))

from audit_constants import LIST_TYPE_RANDOM
from build_dataset import (
    build_dataset_for_run,
    BaseLabelPlugin,
    Zm212ProxyLabel,
    AuditResultLabel,
    DEFAULT_STOCK_DB,
    DEFAULT_ZTCODE_STOCK_REC_DB
)
from train_lgbm import train_lgbm_model, predict_lgbm, HAS_ML_DEPS

logger = logging.getLogger("ModelEvaluate")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [ModelEvaluate] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

REPORTS_DIR = CURRENT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 1. Metric Calculation Routines
# ==============================================================================

def compute_ranking_metrics_at_k(
    df: pd.DataFrame,
    score_col: str,
    k: int,
    label_col: str = "label",
    value_col: str = "sum_expected_loss",
    exposure_col: str = "financial_exposure",
    time_col: str = "est_minutes"
) -> Dict[str, float]:
    """
    Computes precision@K, recall@K, and discovered loss per work minute at cutoff K.
    """
    if df.empty or k <= 0:
        return {
            f"precision@{k}": 0.0,
            f"recall@{k}": 0.0,
            f"hits@{k}": 0,
            f"discovered_loss@{k}": 0.0,
            f"audit_minutes@{k}": 0.0,
            f"loss_per_min@{k}": 0.0
        }

    # Sort descending by score_col (break ties by sum_expected_loss)
    sort_cols = [score_col]
    if value_col in df.columns and value_col != score_col:
        sort_cols.append(value_col)
    df_sorted = df.sort_values(by=sort_cols, ascending=False).reset_index(drop=True)

    top_k = df_sorted.head(k)
    total_positives = (df[label_col] == 1).sum()
    hits_at_k = int((top_k[label_col] == 1).sum())

    precision = hits_at_k / k
    recall = hits_at_k / max(1, total_positives)

    # Monetary discovered loss: actual positive bins' exposure or expected loss
    val_series = top_k[exposure_col] if exposure_col in top_k.columns and top_k[exposure_col].sum() > 0 else top_k[value_col]
    discovered_loss = float(val_series[top_k[label_col] == 1].sum())
    audit_minutes = float(top_k[time_col].sum())
    loss_per_min = discovered_loss / max(0.1, audit_minutes)

    return {
        f"precision@{k}": precision,
        f"recall@{k}": recall,
        f"hits@{k}": hits_at_k,
        f"discovered_loss@{k}": discovered_loss,
        f"audit_minutes@{k}": audit_minutes,
        f"loss_per_min@{k}": loss_per_min
    }


def compute_ipw_random_sample_error_rate(
    df: pd.DataFrame,
    label_col: str = "label",
    prob_col: str = "inclusion_probability",
    list_type_col: str = "list_type"
) -> Tuple[float, int]:
    """
    Computes Horvitz-Thompson / Inverse Probability Weighted (IPW) error rate
    from the random validation sample.
    """
    if df.empty:
        return 0.0, 0

    random_bins = df[df[list_type_col].isin(["RANDOM", "RANDOM_VALIDATION", LIST_TYPE_RANDOM])].copy()
    valid_random = random_bins.dropna(subset=[label_col])
    if valid_random.empty:
        # Fallback: estimate from unselected floor with equal probability
        unselected = df[df["is_selected"] == 0]
        valid_unselected = unselected.dropna(subset=[label_col])
        if valid_unselected.empty:
            valid_df = df.dropna(subset=[label_col])
            return float(valid_df[label_col].mean()) if not valid_df.empty else 0.0, len(df)
        return float(valid_unselected[label_col].mean()), len(valid_unselected)

    y = valid_random[label_col].to_numpy()
    pi = valid_random[prob_col].fillna(0.001).to_numpy()
    pi = np.clip(pi, 1e-6, 1.0)

    weights = 1.0 / pi
    ipw_rate = float(np.sum(weights * y) / np.sum(weights))
    return ipw_rate, len(valid_random)


def compute_unselected_error_rate(
    df: pd.DataFrame,
    label_col: str = "label",
    selected_col: str = "is_selected"
) -> Tuple[float, int, int]:
    """
    Computes error rate in the unselected floor population (shadow leakage).
    Ignores un-audited bins (where label is NaN) to avoid false zeros.
    """
    if df.empty:
        return 0.0, 0, 0

    unselected = df[(df[selected_col] == 0) & (df[label_col].notna())]
    total_unselected = len(unselected)
    if total_unselected == 0:
        return 0.0, 0, 0

    err_count = int((unselected[label_col] == 1).sum())
    err_rate = float(err_count / total_unselected)
    return err_rate, err_count, total_unselected


# ==============================================================================
# 2. Walk-Forward Evaluation Pipeline
# ==============================================================================

def evaluate_models_on_dataset(
    df_eval: pd.DataFrame,
    model_preds: Optional[Dict[str, np.ndarray]] = None
) -> pd.DataFrame:
    """
    Evaluates baseline rule score, shadow EV, and any candidate model predictions
    on a point-in-time dataset.
    """
    # 1. Models to evaluate
    eval_candidates = {
        "Baseline_Rule_Score": "bin_risk_score",
        "Baseline_Shadow_EV": "priority_ev"
    }

    # Add candidate ML predictions if provided
    if model_preds:
        for model_name, preds in model_preds.items():
            pred_col = f"pred_{model_name}"
            df_eval[pred_col] = preds
            eval_candidates[f"Candidate_{model_name}"] = pred_col

    # Unselected floor stats (constant for this snapshot)
    unselected_rate, unsel_err_cnt, total_unsel = compute_unselected_error_rate(df_eval)
    random_ipw_rate, random_sample_cnt = compute_ipw_random_sample_error_rate(df_eval)
    total_positives = int(df_eval["label"].sum())
    total_bins = len(df_eval)

    results = []
    for model_display, col_name in eval_candidates.items():
        if col_name not in df_eval.columns:
            logger.warning(f"Score column '{col_name}' missing from eval dataset. Skipping {model_display}.")
            continue

        m15 = compute_ranking_metrics_at_k(df_eval, score_col=col_name, k=15)
        m40 = compute_ranking_metrics_at_k(df_eval, score_col=col_name, k=40)

        row = {
            "run_date": df_eval["run_date"].iloc[0] if "run_date" in df_eval.columns else "unknown",
            "model_name": model_display,
            "score_column": col_name,
            "total_bins": total_bins,
            "total_positives": total_positives,
            "overall_base_rate": total_positives / max(1, total_bins),
            
            # K=15 (Rapid Floor)
            "precision@15": m15["precision@15"],
            "recall@15": m15["recall@15"],
            "hits@15": m15["hits@15"],
            "discovered_loss@15": m15["discovered_loss@15"],
            "audit_minutes@15": m15["audit_minutes@15"],
            "loss_per_min@15": m15["loss_per_min@15"],
            
            # K=40 (Daily Total)
            "precision@40": m40["precision@40"],
            "recall@40": m40["recall@40"],
            "hits@40": m40["hits@40"],
            "discovered_loss@40": m40["discovered_loss@40"],
            "audit_minutes@40": m40["audit_minutes@40"],
            "loss_per_min@40": m40["loss_per_min@40"],
            
            # Floor Representation
            "random_sample_ipw_rate": random_ipw_rate,
            "random_sample_count": random_sample_cnt,
            "unselected_floor_error_rate": unselected_rate,
            "unselected_error_count": unsel_err_cnt,
            "total_unselected_bins": total_unsel
        }
        results.append(row)

    return pd.DataFrame(results)


def run_walk_forward_evaluation(
    run_dates: List[str],
    db_path: Optional[Union[str, Path]] = None,
    ztcode_stock_rec_db_path: Optional[Union[str, Path]] = None,
    label_plugin: Optional[BaseLabelPlugin] = None,
    label_source: str = "audit_result",
    min_positives_for_lgbm: int = 200
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Executes walk-forward temporal split evaluation:
    - Sorts run dates chronologically.
    - If multiple dates exist:
      - Train LightGBM on dates < t (if positives >= min_positives_for_lgbm).
      - Evaluate on date t.
    - If single date exists:
      - Evaluates baselines (Rule Score, Shadow EV) on the date.
      - Checks LightGBM guard on that date.
    """
    sorted_dates = sorted(list(set(run_dates)))
    logger.info(f"⏳ [WALK-FORWARD EVAL] Evaluating across {len(sorted_dates)} date(s): {sorted_dates}")

    # Resolve label plugin
    if label_plugin is not None:
        resolved_plugin = label_plugin
    elif label_source == "audit_result":
        resolved_plugin = AuditResultLabel(db_path=db_path)
    else:
        resolved_plugin = Zm212ProxyLabel(ztcode_stock_rec_db_path=ztcode_stock_rec_db_path)

    logger.info(f"Using Label Plugin: [{resolved_plugin.name}] (Weak Proxy: {resolved_plugin.is_weak_proxy})")

    all_eval_rows = []
    lgbm_guard_reports = []

    # Cache built datasets
    datasets_by_date = {}
    for r_date in sorted_dates:
        ds = build_dataset_for_run(
            run_date=r_date,
            db_path=db_path,
            ztcode_stock_rec_db_path=ztcode_stock_rec_db_path,
            label_plugin=resolved_plugin
        )
        datasets_by_date[r_date] = ds

    for idx, eval_date in enumerate(sorted_dates):
        df_eval = datasets_by_date[eval_date]
        model_preds = {}

        # If previous dates exist, train LightGBM candidate on historical data
        if idx > 0 and HAS_ML_DEPS:
            train_dates = sorted_dates[:idx]
            df_train = pd.concat([datasets_by_date[d] for d in train_dates], ignore_index=True)
            logger.info(f"Training LightGBM candidate on {train_dates} ({len(df_train)} bins) for eval on {eval_date}...")
            
            lgbm_res = train_lgbm_model(
                df_train=df_train,
                target_col="label",
                min_positives=min_positives_for_lgbm
            )
            lgbm_guard_reports.append({
                "eval_date": eval_date,
                "train_dates": train_dates,
                "result": lgbm_res
            })

            if lgbm_res.get("status") == "trained":
                preds = predict_lgbm(lgbm_res, df_eval)
                model_preds["LightGBM"] = preds
        elif idx == 0 and HAS_ML_DEPS:
            # Check training guard on the single/first date for diagnostic reporting
            lgbm_res = train_lgbm_model(
                df_train=df_eval,
                target_col="label",
                min_positives=min_positives_for_lgbm
            )
            lgbm_guard_reports.append({
                "eval_date": eval_date,
                "train_dates": [eval_date],
                "result": lgbm_res
            })
            if lgbm_res.get("status") == "trained":
                # In-sample evaluation for single-date diagnostics
                preds = predict_lgbm(lgbm_res, df_eval)
                model_preds["LightGBM_InSample"] = preds

        eval_df = evaluate_models_on_dataset(df_eval, model_preds=model_preds)
        all_eval_rows.append(eval_df)

    final_metrics_df = pd.concat(all_eval_rows, ignore_index=True) if all_eval_rows else pd.DataFrame()
    data_origin = getattr(resolved_plugin, "data_origin", "PHYSICAL")
    has_synthetic = getattr(resolved_plugin, "has_synthetic", False)
    meta = {
        "lgbm_guard_reports": lgbm_guard_reports,
        "dates": sorted_dates,
        "label_source": resolved_plugin.name,
        "is_weak_proxy": resolved_plugin.is_weak_proxy,
        "label_description": resolved_plugin.description,
        "data_origin": data_origin,
        "has_synthetic": has_synthetic
    }
    return final_metrics_df, meta


# ==============================================================================
# 3. Report Generation Routines
# ==============================================================================

def export_evaluation_report(
    metrics_df: pd.DataFrame,
    meta_info: Dict[str, Any],
    reports_dir: Optional[Path] = None
) -> Tuple[Path, Path]:
    """
    Exports Markdown and CSV evaluation reports to model/reports/.
    Includes ground truth audit banner or weak proxy warning banner based on label source.
    """
    target_dir = Path(reports_dir or REPORTS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    md_path = target_dir / f"evaluation_summary_{ts_str}.md"
    csv_path = target_dir / f"evaluation_metrics_{ts_str}.csv"

    # Save CSV
    metrics_df.to_csv(csv_path, index=False)

    is_weak_proxy = meta_info.get("is_weak_proxy", True)
    has_synthetic = meta_info.get("has_synthetic", False) or meta_info.get("data_origin") == "SYNTHETIC_SEED"

    # Build Markdown Report
    md = []
    md.append(f"# 📊 {os.getenv('COMPANY_NAME', 'ACME Manufacturing')} 재고정확도 모델 오프라인 평가 리포트 (Offline Evaluation Report)")
    md.append("")
    if is_weak_proxy:
        md.append("> [!WARNING]")
        md.append("> ### ⚠️ 대리 라벨(Weak Proxy Label) 해석상 한계 및 주의사항 (Critical Limitation Notice)")
        md.append("> 본 오프라인 평가 및 학습 파이프라인에서 사용된 타겟 라벨은 **ZTCODE_STOCK_REC SAP vs WMS 시스템 불일치 데이터 기반의 약한 대리 라벨(Weak Proxy Label)**입니다.")
        md.append("> - **1. 공간적 해상도 불일치 (Spatial Resolution Disconnect)**: ZTCODE_STOCK_REC는 공장 전체 품목(Material) 단위의 합산 차이이므로, 특정 물리적 로케이션(Bin)의 실제 수량 오차를 직접 확정하지 않습니다.")
        md.append("> - **2. 행정/시차 노이즈 (Administrative & Timing Noise)**: 전표 처리 시차(MB1B 311 등), 외주/사급 출고 지연, 가상 유령 재고 등으로 인한 위양성(False Positive)과 상계 오류로 인한 위음성(False Negative)이 내포되어 있습니다.")
        md.append("> - **3. 운영 모델 적용 제한**: 본 리포트의 지표는 ML 학습 및 평가 파이프라인의 **엔드투엔드(End-to-End) 아키텍처 무결성 검증용 골격**이며, **실제 현장 실사 실적(AUDIT_RESULT Ground Truth)이 수집되기 전까지는 현행 도메인 규칙 점수를 대체할 수 없습니다.**")
    elif has_synthetic:
        md.append("> [!WARNING]")
        md.append("> ### ⚠️ 합성 시드 데이터 기반 파이프라인 검증 알림 (Synthetic Seed Verification - DO NOT INTERPRET AS REAL ACCURACY)")
        md.append("> 본 리포트의 지표는 **기술적 검증용 가상 시드 데이터(SYNTHETIC_SEED)**를 타겟 라벨로 사용하여 산출되었습니다.")
        md.append("> - **1. 순환 평가 주의 (Circular Logic Warning)**: 합성 시드 데이터는 도메인 규칙 위험도 점수(Bin Risk Score)를 기반으로 가상 생성된 실적이므로, 본 리포트의 정밀도(Precision) 및 재현율(Recall) 수치는 **파이프라인의 종단 간 연산 무결성을 검증하기 위한 기술적 지표**일 뿐 실제 모델의 성능을 대변하지 않습니다.")
        md.append("> - **2. 실제 현장 실사 데이터 수집 필요**: 실제 현장 실사자가 바닥 재고를 직접 카운팅한 진성 실적(PHYSICAL)이 누적되기 전까지는 본 지표를 모델 성능으로 인용하거나 운영 배포 근거로 사용할 수 없습니다.")
    else:
        md.append("> [!NOTE]")
        md.append("> ### 🎯 현장 실사 확정 실적 기반 평가 (Ground Truth Audit Result Evaluation)")
        md.append("> 본 평가는 현장 실사자가 바닥 재고를 직접 검증한 **AUDIT_RESULT 확정 실사 실적(Verified Physical Cycle Count Ground Truth)**을 타겟 라벨로 사용하였습니다.")
        md.append("> - **1. 물리적 실사 검증 (Physical Verification)**: WMS 상의 단순 행정 조정이나 가상 이력(`audit_origin == 'WMS_ADMIN_ADJUSTMENT'`)은 원천 배제하고, 실사자 및 관리자 재실사로 확정된 실물 오차(`audit_origin == 'PHYSICAL_CYCLE_COUNT'`, `is_confirmed = 1`)만을 정답 라벨로 인정합니다.")
        md.append("> - **2. 입출고 시차 자동 보정 (In-flight Movement Reconciliation)**: 지시서 발행 시점과 실사 시점 사이에 발생한 실시간 입고(ΔGR) 및 출고(ΔGI)를 역추적 보정하여 가짜 불일치(False Discrepancy)를 원천 제거하였습니다.")
        md.append("> - **3. 1:N 재실사(Recount) 최신 확정값 반영**: 복수 재실사 발생 시 관리자 최종 확인(is_recount=1) 결과를 최우선 반영하여 오차 판정 무결성을 확보하였습니다.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 📌 1. 평가 개요 (Evaluation Overview)")
    md.append(f"- **생성 일시**: `{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`")
    md.append(f"- **평가 대상 일자**: `{', '.join(meta_info.get('dates', []))}`")
    md.append(f"- **타겟 라벨 소스**: `{meta_info.get('label_source', 'unknown')}` ({'약한 대리 라벨' if is_weak_proxy else '현장 실사 확정 정답 라벨'})")
    md.append(f"- **평가 기준**: Walk-Forward 시간순 분할 및 도메인 규칙 vs 섀도 기대가치(EV) vs 후보 ML 비교")
    md.append(f"- **CSV 원본 파일**: `{csv_path.name}`")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## 🎯 2. 모델별 주요 성과 지표 (Comparative Performance Metrics)")
    md.append("")

    # Markdown Table of Metrics
    if not metrics_df.empty:
        table_cols = [
            "run_date", "model_name", "precision@15", "recall@15", "loss_per_min@15",
            "precision@40", "recall@40", "loss_per_min@40",
            "random_sample_ipw_rate", "unselected_floor_error_rate"
        ]
        display_df = metrics_df[[c for c in table_cols if c in metrics_df.columns]].copy()
        
        # Format columns for display
        for p_col in ["precision@15", "recall@15", "precision@40", "recall@40", "random_sample_ipw_rate", "unselected_floor_error_rate"]:
            if p_col in display_df.columns:
                display_df[p_col] = display_df[p_col].apply(lambda x: f"{x:.2%}")
        for l_col in ["loss_per_min@15", "loss_per_min@40"]:
            if l_col in display_df.columns:
                display_df[l_col] = display_df[l_col].apply(lambda x: f"${x:,.2f}/m")

        def df_to_markdown_table(d_df: pd.DataFrame) -> str:
            try:
                return d_df.to_markdown(index=False)
            except Exception:
                headers = list(d_df.columns)
                h_line = "| " + " | ".join(str(h) for h in headers) + " |"
                s_line = "| " + " | ".join("---" for _ in headers) + " |"
                r_lines = [
                    "| " + " | ".join(str(v) for v in row) + " |"
                    for row in d_df.itertuples(index=False)
                ]
                return "\n".join([h_line, s_line] + r_lines)

        md.append(df_to_markdown_table(display_df))
    else:
        md.append("_평가 데이터셋이 비어 있습니다._")

    md.append("")
    md.append("---")
    md.append("")
    md.append("## 🤖 3. LightGBM 학습 가드레일 상태 (Training Guardrail Audit)")
    md.append("")
    guard_reports = meta_info.get("lgbm_guard_reports", [])
    if guard_reports:
        for gr in guard_reports:
            res = gr.get("result", {})
            status = res.get("status", "unknown")
            eval_d = gr.get("eval_date")
            pos_c = res.get("pos_count", 0)
            min_p = res.get("min_positives", 200)

            if status == "skipped":
                md.append(f"- **평가일 `{eval_d}` (학습 대상: `{gr.get('train_dates')}`)**:")
                md.append(f"  - ⚠️ **학습 중단 (Guard Triggered)**: 양성 샘플 수(`{pos_c}건`)가 최소 기준치(`{min_p}건`) 미만입니다.")
                md.append(f"  - **사유**: 과적합(Overfitting) 방지 원칙에 따라 모델 학습을 자동 건너뛰었습니다.")
            elif status == "trained":
                md.append(f"- **평가일 `{eval_d}` (학습 대상: `{gr.get('train_dates')}`)**:")
                md.append(f"  - ✅ **학습 완료 (Training Successful)**: 양성 샘플 `{pos_c}건` (기준 `{min_p}건` 충족)")
                md.append(f"  - Train ROC-AUC: `{res.get('train_auc', 0.0):.4f}`, PR-AUC: `{res.get('train_pr_auc', 0.0):.4f}`")
                fi = res.get("feature_importances")
                if fi is not None and not fi.empty:
                    md.append("  - **상위 주요 피처 (Top 5 Features)**:")
                    for _, r in fi.head(5).iterrows():
                        md.append(f"    - `{r['feature']}`: {r['importance']}")
    else:
        md.append("- LightGBM 평가 기록 없음.")

    md.append("")
    md.append("---")
    md.append("")
    md.append("## 💡 4. 지표 해석 가이드 (Metric Interpretation Guide)")
    md.append("1. **Precision@15 (Daily Rapid Floor 적중률)**: 상위 15개 빈 중 대리 라벨 불일치가 발생한 빈의 비율입니다.")
    md.append("2. **Precision@40 (Daily Total 적중률)**: 일일 전체 40개 빈 중 대리 라벨 불일치가 발생한 빈의 비율입니다.")
    md.append("3. **Discovered Loss / Minute (작업분당 기대 발견 금액)**: 현장 실사자가 1분 작업할 때 발견할 수 있는 금액 가치입니다. 소요시간 대비 경제성을 측정합니다.")
    md.append("4. **Random Sample IPW Error Rate (무작위 표본군 오차율)**: 역확률 가중(IPW)을 통해 공장 전체의 편향 없는 기저 오차율(Baseline Discrepancy Rate)을 추정합니다.")
    md.append("5. **Unselected Floor Error Rate (미선택군 오차율)**: 실사 대상에서 제외된 나머지 바닥 재고의 오차율로서, 모델의 그림자 누락(False Negative Leakage) 위험도를 감시합니다.")
    md.append("")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    logger.info(f"✅ [REPORTS EXPORTED]")
    logger.info(f"   Markdown: {md_path}")
    logger.info(f"   CSV:      {csv_path}")

    return md_path, csv_path


# ==============================================================================
# 4. CLI Entrypoint
# ==============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Evaluate cycle count ranking models offline")
    parser.add_argument("--db-path", type=str, default=str(DEFAULT_STOCK_DB), help="Stock accuracy DB path")
    parser.add_argument("--ztcode_stock_rec-db-path", type=str, default=str(DEFAULT_ZTCODE_STOCK_REC_DB), help="ZTCODE_STOCK_REC DB path")
    parser.add_argument("--label-source", type=str, choices=["audit_result", "proxy"], default="audit_result",
                        help="Label source plugin ('audit_result' for Ground Truth AUDIT_RESULT table, 'proxy' for ZTCODE_STOCK_REC proxy)")
    parser.add_argument("--run-dates", nargs="+", default=None, help="One or more run dates (YYYY-MM-DD)")
    parser.add_argument("--min-positives", type=int, default=200, help="Minimum positive samples for LightGBM")
    args = parser.parse_args()

    # Discover dates from DB if not provided
    target_dates = args.run_dates
    if not target_dates:
        t_db = Path(args.db_path)
        if t_db.exists():
            c = sqlite3.connect(f"file:{t_db.resolve().as_posix()}?mode=ro", uri=True)
            try:
                cur = c.cursor()
                cur.execute("SELECT DISTINCT run_date FROM AUDIT_RUN ORDER BY run_date;")
                target_dates = [r[0] for r in cur.fetchall()]
            finally:
                c.close()
        if not target_dates:
            target_dates = ["2026-10-26"]

    metrics, meta = run_walk_forward_evaluation(
        run_dates=target_dates,
        db_path=args.db_path,
        ztcode_stock_rec_db_path=args.ztcode_stock_rec_db_path,
        label_source=args.label_source,
        min_positives_for_lgbm=args.min_positives
    )

    md_file, csv_file = export_evaluation_report(metrics, meta)
    print("\n" + "=" * 80)
    print(f"Evaluation completed successfully!")
    print(f"Summary Report: {md_file}")
    print(f"Metrics CSV:    {csv_file}")
    print("=" * 80)
    if not metrics.empty:
        print(metrics[["run_date", "model_name", "precision@15", "precision@40", "loss_per_min@15"]].to_string())
