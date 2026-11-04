"""
LightGBM Classification Model Skeleton for Cycle Count Discrepancy Prediction
=============================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / model / train_lgbm.py

Features:
- Handles extreme class imbalance via scale_pos_weight.
- Categorical features: primary_mover_class, plant, tier.
- Multi-hot encoded risk categories: CRITICAL_GHOST, FIFO_VIOLATION, etc.
- Guardrail: Aborts training if positive samples < 200 (configurable threshold).
- Modular export of feature importance and calibrated probabilities.
"""

import sys
import os

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
from typing import Optional, Dict, Any, List, Tuple
import pandas as pd
import numpy as np

try:
    import lightgbm as lgb
    from sklearn.preprocessing import OrdinalEncoder
    from sklearn.metrics import roc_auc_score, average_precision_score
    HAS_ML_DEPS = True
except ImportError:
    HAS_ML_DEPS = False

logger = logging.getLogger("TrainLGBM")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [TrainLGBM] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Feature Configuration
CATEGORICAL_FEATURES = ["primary_mover_class", "plant", "tier"]

NUMERIC_FEATURES = [
    "box_count",
    "total_qty",
    "unique_materials_count",
    "bin_risk_score",
    "avg_risk_score",
    "max_warehouse_dwell_days",
    "max_bin_dwell_days",
    "max_lot_age_days",
    "min_ontology_turnover_days",
    "max_fifo_gap_days",
    "max_lot_spread_days",
    "max_missing_gi_diff_box",
    "ztcode_stock_rec_diff_qty",
    "ztcode_stock_rec_consecutive_days",
    "ztcode_stock_rec_exposure",
    "max_p_error_proxy",
    "sum_expected_loss",
    "est_minutes",
    "priority_ev"
]

MULTI_HOT_PREFIX = "cat_"


def get_feature_columns(df: pd.DataFrame) -> Tuple[List[str], List[str]]:
    """Identifies numeric, categorical, and multi-hot features present in the dataset."""
    avail_cat = [c for c in CATEGORICAL_FEATURES if c in df.columns]
    avail_num = [c for c in NUMERIC_FEATURES if c in df.columns]
    avail_multi = [c for c in df.columns if c.startswith(MULTI_HOT_PREFIX)]
    all_num = avail_num + avail_multi
    return all_num, avail_cat


def prepare_lgbm_data(
    df: pd.DataFrame,
    cat_encoders: Optional[Dict[str, OrdinalEncoder]] = None,
    is_training: bool = True
) -> Tuple[pd.DataFrame, Dict[str, OrdinalEncoder], List[str]]:
    """
    Cleans, imputes, and encodes features for LightGBM.
    """
    num_cols, cat_cols = get_feature_columns(df)
    feature_cols = num_cols + cat_cols

    df_out = df[feature_cols].copy()

    # Fill numeric NaNs with safe defaults
    for col in num_cols:
        df_out[col] = pd.to_numeric(df_out[col], errors="coerce").fillna(0.0)

    # Encode categorical columns
    encoders = cat_encoders or {}
    for col in cat_cols:
        df_out[col] = df_out[col].astype(str).fillna("Unknown")
        if is_training:
            enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
            df_out[col] = enc.fit_transform(df_out[[col]])
            encoders[col] = enc
        else:
            enc = encoders.get(col)
            if enc:
                df_out[col] = enc.transform(df_out[[col]])
            else:
                df_out[col] = 0.0

    return df_out, encoders, feature_cols


def train_lgbm_model(
    df_train: pd.DataFrame,
    target_col: str = "label",
    min_positives: int = 200,
    df_val: Optional[pd.DataFrame] = None,
    params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Trains a LightGBM binary classifier with class imbalance handling and guardrails.
    
    Args:
        df_train: Training DataFrame containing features and target_col.
        target_col: Binary label column (1 = discrepancy, 0 = match).
        min_positives: Minimum required positive count to proceed with training.
        df_val: Optional validation DataFrame.
        params: LightGBM hyperparameters override.
        
    Returns:
        Dict containing model bundle or skip reason.
    """
    if not HAS_ML_DEPS:
        raise ImportError("lightgbm and scikit-learn must be installed to run train_lgbm_model.")

    if target_col not in df_train.columns:
        raise ValueError(f"Target column '{target_col}' not found in training dataset.")

    # Filter to labeled samples only (ground truth physical audits only label audited bins)
    df_train_labeled = df_train[df_train[target_col].notna()].copy()
    if df_train_labeled.empty:
        logger.warning("⚠️ No labeled samples found in training dataset.")
        return {
            "status": "skipped",
            "reason": "NO_LABELED_SAMPLES",
            "pos_count": 0,
            "min_positives": min_positives,
            "model": None,
            "encoders": None,
            "feature_names": []
        }

    y_train = df_train_labeled[target_col].astype(int)
    pos_count = int(y_train.sum())
    total_count = len(y_train)
    neg_count = total_count - pos_count

    logger.info(
        f"📊 [TRAIN LGBM] Dataset size: {total_count} labeled bins. "
        f"Positives: {pos_count} ({pos_count / max(1, total_count):.2%}), Negatives: {neg_count}."
    )

    # =========================================================================
    # GUARD: Minimum positive sample count check
    # =========================================================================
    if pos_count < min_positives:
        warn_msg = (
            f"⚠️ [GUARD TRIGGERED] Insufficient positive samples for LightGBM training: "
            f"{pos_count} < {min_positives}. Training aborted to prevent severe overfitting."
        )
        logger.warning(warn_msg)
        return {
            "status": "skipped",
            "reason": f"Positive count ({pos_count}) below threshold ({min_positives})",
            "pos_count": pos_count,
            "min_positives": min_positives,
            "model": None,
            "encoders": None,
            "feature_names": []
        }

    # Prepare features
    X_train, encoders, feature_names = prepare_lgbm_data(df_train_labeled, is_training=True)

    # Class imbalance weight
    scale_pos_weight = neg_count / max(1, pos_count)

    # Default parameters
    lgb_params = {
        "objective": "binary",
        "boosting_type": "gbdt",
        "learning_rate": 0.05,
        "num_leaves": 31,
        "max_depth": 5,
        "min_child_samples": 20,
        "scale_pos_weight": scale_pos_weight,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "random_state": 42,
        "verbose": -1,
        "n_estimators": 100
    }
    if params:
        lgb_params.update(params)

    logger.info(f"   Training LightGBM with scale_pos_weight={scale_pos_weight:.2f}...")
    model = lgb.LGBMClassifier(**lgb_params)

    eval_set = None
    if df_val is not None and not df_val.empty and target_col in df_val.columns:
        df_val_labeled = df_val[df_val[target_col].notna()].copy()
        if not df_val_labeled.empty:
            X_val, _, _ = prepare_lgbm_data(df_val_labeled, cat_encoders=encoders, is_training=False)
            y_val = df_val_labeled[target_col].astype(int)
            eval_set = [(X_val, y_val)]

    model.fit(
        X_train,
        y_train,
        eval_set=eval_set,
        callbacks=[lgb.early_stopping(stopping_rounds=15, verbose=False)] if eval_set else None
    )

    # Train evaluation
    train_preds = model.predict_proba(X_train)[:, 1]
    train_auc = roc_auc_score(y_train, train_preds) if len(np.unique(y_train)) > 1 else 0.5
    train_pr_auc = average_precision_score(y_train, train_preds) if len(np.unique(y_train)) > 1 else 0.0

    # Feature Importance
    importances = pd.DataFrame({
        "feature": feature_names,
        "importance": model.feature_importances_
    }).sort_values(by="importance", ascending=False).reset_index(drop=True)

    logger.info(f"✅ [TRAINING COMPLETE] Train ROC-AUC: {train_auc:.4f}, PR-AUC: {train_pr_auc:.4f}")
    logger.info(f"   Top 5 Features:\n" + "\n".join([f"     - {r.feature}: {r.importance}" for _, r in importances.head(5).iterrows()]))

    return {
        "status": "trained",
        "model": model,
        "encoders": encoders,
        "feature_names": feature_names,
        "pos_count": pos_count,
        "min_positives": min_positives,
        "train_auc": train_auc,
        "train_pr_auc": train_pr_auc,
        "feature_importances": importances
    }


def predict_lgbm(model_bundle: Dict[str, Any], df_test: pd.DataFrame) -> np.ndarray:
    """
    Computes predicted discrepancy probabilities using a trained model bundle.
    If model was skipped, returns zeros or None.
    """
    if model_bundle.get("status") != "trained" or model_bundle.get("model") is None:
        logger.warning("Model bundle has not been trained. Returning zero probability array.")
        return np.zeros(len(df_test))

    model = model_bundle["model"]
    encoders = model_bundle["encoders"]
    X_test, _, _ = prepare_lgbm_data(df_test, cat_encoders=encoders, is_training=False)
    return model.predict_proba(X_test)[:, 1]
