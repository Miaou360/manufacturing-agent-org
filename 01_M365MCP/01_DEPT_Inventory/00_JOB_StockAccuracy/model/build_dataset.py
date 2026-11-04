"""
Offline Dataset Builder for Stock Accuracy Model Training & Evaluation
======================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / model / build_dataset.py

Constructs point-in-time training and evaluation datasets by joining
AUDIT_FEATURE_SNAPSHOT (features at run_date) with historical label sources
via an extensible Plugin Architecture.

Label Plugins:
1. Zm212ProxyLabel:
   - Queries SAP vs WMS reconciliation mismatches within 1~7 days after run_date.
   - Material-level discrepancy is propagated to physical bins containing that material.
   - Marked explicitly as a Weak Proxy Label (대리 라벨).
2. AuditResultLabel:
   - Future ground-truth interface for AUDIT_RESULT physical counting table.
   - Raises NotImplementedError pending field result ingestion.

Data Leakage Prevention:
- Includes verify_no_data_leakage() ensuring all feature timestamps <= run_date.
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
from abc import ABC, abstractmethod
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
from typing import Optional, List, Dict, Any, Tuple, Union
import pandas as pd
import numpy as np

# Configure Logging
logger = logging.getLogger("BuildDataset")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [BuildDataset] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Default Database Paths
CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
DEFAULT_STOCK_DB = JOB_DIR / "db" / "inventory_history.db"
DEFAULT_ZTCODE_STOCK_REC_DB = JOB_DIR.parent / "04_JOB_SystemReconciliation" / "db" / "system_reconciliation.db"


class DataLeakageError(Exception):
    """Raised when future information beyond run_date is detected in features."""
    pass


def verify_no_data_leakage(df_features: pd.DataFrame, run_date: str) -> bool:
    """
    Validates that zero future information exists in the feature snapshot.
    
    Checks:
    1. created_at timestamps are on or prior to run_date (23:59:59).
    2. gr_date (Goods Receipt) does not exceed run_date.
    3. No future label columns exist prior to plugin joining.
    
    Raises:
        DataLeakageError: If any feature row contains post-run_date data.
    """
    if df_features.empty:
        logger.warning(f"Feature dataset is empty for run_date {run_date}.")
        return True

    target_date = datetime.date.fromisoformat(run_date)
    end_of_run_day = f"{run_date} 23:59:59"

    # 1. Check created_at
    if "created_at" in df_features.columns:
        invalid_ts = df_features[df_features["created_at"].astype(str) > end_of_run_day]
        if not invalid_ts.empty:
            sample_ts = invalid_ts["created_at"].iloc[0]
            raise DataLeakageError(
                f"Data Leakage Violation: {len(invalid_ts)} rows in AUDIT_FEATURE_SNAPSHOT "
                f"have created_at '{sample_ts}' which is strictly after run_date '{run_date}'."
            )

    # 2. Check gr_date (if present and parseable)
    if "gr_date" in df_features.columns:
        valid_gr = df_features["gr_date"].dropna().astype(str).str.strip()
        future_gr = valid_gr[valid_gr > run_date]
        if not future_gr.empty:
            raise DataLeakageError(
                f"Data Leakage Violation: {len(future_gr)} rows have Goods Receipt date (gr_date) "
                f"'{future_gr.iloc[0]}' after run_date '{run_date}'."
            )

    # 3. Check for pre-existing label columns
    prohibited_cols = {"label", "ground_truth", "actual_diff_qty", "audit_error"}
    leaked_cols = prohibited_cols.intersection(set(df_features.columns))
    if leaked_cols:
        raise DataLeakageError(
            f"Data Leakage Violation: Feature dataset already contains target label columns: {leaked_cols}"
        )

    logger.info(f"✅ [LEAKAGE CHECK PASSED] 0 records leak future data beyond run_date '{run_date}'.")
    return True


# ==============================================================================
# 1. Label Source Plugin Architecture
# ==============================================================================

class BaseLabelPlugin(ABC):
    """Abstract base class for cycle count target label sources."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the label source plugin."""
        pass

    @property
    @abstractmethod
    def is_weak_proxy(self) -> bool:
        """True if this label is an indirect proxy rather than physical ground truth."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Detailed technical description and caveats."""
        pass

    @abstractmethod
    def get_labels(
        self,
        df_features: pd.DataFrame,
        run_date: str,
        conn_stock: Optional[sqlite3.Connection] = None
    ) -> pd.DataFrame:
        """
        Generates or joins target labels for the provided features.
        
        Args:
            df_features: Feature snapshot dataframe (at item or bin level).
            run_date: 'YYYY-MM-DD' representing the snapshot date.
            conn_stock: SQLite connection to stock_accuracy DB.
            
        Returns:
            DataFrame containing at least:
            - 'bin': Physical bin identifier
            - 'label': Binary target (1 = discrepancy, 0 = match)
            - 'discrepancy_qty': Quantity discrepancy (if known)
            - 'financial_exposure': Dollar discrepancy (if known)
            - 'label_source': Name of this plugin
            - 'is_weak_proxy': Boolean flag
        """
        pass


class Zm212ProxyLabel(BaseLabelPlugin):
    """
    Weak Proxy Label Plugin based on SAP vs WMS Reconciliation (ZTCODE_STOCK_REC).
    
    Logic:
    - Queries accuracy_material_snapshot in system_reconciliation.db.
    - Evaluates whether material had an official system discrepancy (diff_qty != 0 or score < 100)
      within 1 to 7 days after run_date.
    - Material-level discrepancy is propagated to any physical bin storing that material.
    
    CRITICAL LIMITATION:
    - This is a WEAK PROXY. A material mismatch at the plant level may stem from administrative
      goods movement delays, consignment misalignments, or line-side staging rather than
      physical misplacement in this particular bin.
    """

    def __init__(
        self,
        ztcode_stock_rec_db_path: Optional[Union[str, Path]] = None,
        min_days_forward: int = 1,
        max_days_forward: int = 7
    ):
        self.ztcode_stock_rec_db_path = Path(ztcode_stock_rec_db_path or DEFAULT_ZTCODE_STOCK_REC_DB)
        self.min_days_forward = min_days_forward
        self.max_days_forward = max_days_forward

    @property
    def name(self) -> str:
        return f"ztcode_stock_rec_weak_proxy_{self.min_days_forward}to{self.max_days_forward}d"

    @property
    def is_weak_proxy(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            f"Material-level ZTCODE_STOCK_REC system discrepancy within +{self.min_days_forward} to "
            f"+{self.max_days_forward} days after run_date propagated to containing bins. "
            "CAUTION: Weak proxy label; subject to administrative timing noise."
        )

    def get_labels(
        self,
        df_features: pd.DataFrame,
        run_date: str,
        conn_stock: Optional[sqlite3.Connection] = None
    ) -> pd.DataFrame:
        """Point-in-time join of forward ZTCODE_STOCK_REC mismatches."""
        if not self.ztcode_stock_rec_db_path.exists():
            logger.warning(f"ZTCODE_STOCK_REC DB not found at {self.ztcode_stock_rec_db_path}. All labels will default to 0.")
            unique_bins = df_features["bin"].unique() if "bin" in df_features.columns else []
            return pd.DataFrame({
                "bin": unique_bins,
                "label": 0,
                "discrepancy_qty": 0.0,
                "financial_exposure": 0.0,
                "label_source": self.name,
                "is_weak_proxy": True,
                "notes": "ZTCODE_STOCK_REC DB missing; fallback to zero"
            })

        dt_run = datetime.date.fromisoformat(run_date)
        start_date = (dt_run + datetime.timedelta(days=self.min_days_forward)).strftime("%Y-%m-%d")
        end_date = (dt_run + datetime.timedelta(days=self.max_days_forward)).strftime("%Y-%m-%d")

        logger.info(
            f"🔍 [ZTCODE_STOCK_REC PROXY LABEL] Querying forward window [{start_date} ~ {end_date}] "
            f"(+{self.min_days_forward}d to +{self.max_days_forward}d post run_date {run_date})..."
        )

        query = """
        SELECT 
            TRIM(material) AS mat_no,
            MAX(CASE WHEN diff_qty != 0 OR score < 100 OR ABS(financial_exposure) > 0.01 THEN 1 ELSE 0 END) AS mat_has_mismatch,
            MAX(ABS(diff_qty)) AS mat_max_diff_qty,
            MAX(ABS(financial_exposure)) AS mat_max_exposure
        FROM accuracy_material_snapshot
        WHERE (posting_date >= ? AND posting_date <= ?)
           OR (snapshot_date >= ? AND snapshot_date <= ?)
        GROUP BY TRIM(material);
        """

        uri_path = f"file:{self.ztcode_stock_rec_db_path.resolve().as_posix()}?mode=ro"
        conn_zm = sqlite3.connect(uri_path, uri=True)
        try:
            df_zm = pd.read_sql_query(query, conn_zm, params=[start_date, end_date, start_date, end_date])
        finally:
            conn_zm.close()

        logger.info(f"   ZTCODE_STOCK_REC forward records found: {len(df_zm)} materials evaluated.")
        if not df_zm.empty:
            mismatched_cnt = (df_zm["mat_has_mismatch"] == 1).sum()
            logger.info(f"   ZTCODE_STOCK_REC forward mismatched materials: {mismatched_cnt} / {len(df_zm)}.")

        # Map to bin level via material association
        if "mat_no" in df_features.columns and "bin" in df_features.columns:
            df_merged = df_features[["bin", "mat_no"]].drop_duplicates().copy()
            df_merged["mat_no"] = df_merged["mat_no"].astype(str).str.strip()
            df_merged = df_merged.merge(df_zm, on="mat_no", how="left")
            df_merged["mat_has_mismatch"] = df_merged["mat_has_mismatch"].fillna(0).astype(int)
            df_merged["mat_max_diff_qty"] = df_merged["mat_max_diff_qty"].fillna(0.0)
            df_merged["mat_max_exposure"] = df_merged["mat_max_exposure"].fillna(0.0)

            # Aggregate to bin
            bin_labels = df_merged.groupby("bin").agg(
                label=("mat_has_mismatch", "max"),
                discrepancy_qty=("mat_max_diff_qty", "max"),
                financial_exposure=("mat_max_exposure", "max")
            ).reset_index()
        else:
            # Features already at bin level
            unique_bins = df_features["bin"].unique()
            bin_labels = pd.DataFrame({"bin": unique_bins, "label": 0, "discrepancy_qty": 0.0, "financial_exposure": 0.0})

        bin_labels["label_source"] = self.name
        bin_labels["is_weak_proxy"] = True
        bin_labels["notes"] = f"ZTCODE_STOCK_REC mismatch in [{start_date} ~ {end_date}]"
        
        pos_bins = (bin_labels["label"] == 1).sum()
        logger.info(f"   Bin-level proxy label generated: {pos_bins} / {len(bin_labels)} positive bins ({pos_bins / max(1, len(bin_labels)):.2%}).")
        return bin_labels


class AuditResultLabel(BaseLabelPlugin):
    """
    Ground-Truth Label Plugin based on AUDIT_RESULT physical counting table.
    
    Extracts genuine ground truth labels from verified warehouse floor counts.
    - Strictly filters audit_origin == 'PHYSICAL_CYCLE_COUNT' to prevent replacing
      physical results with routine WMS administrative adjustments.
    - Resolves 1:N recounts by prioritizing confirmed recounts (is_recount=1, is_confirmed=1).
    - Sets is_weak_proxy = False.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None, allow_synthetic: bool = True):
        self.db_path = Path(db_path or DEFAULT_STOCK_DB)
        self.allow_synthetic = allow_synthetic
        self.has_synthetic = False
        self.data_origin = "PHYSICAL"

    @property
    def name(self) -> str:
        return "audit_result_ground_truth"

    @property
    def is_weak_proxy(self) -> bool:
        return False

    @property
    def description(self) -> str:
        return "Ground truth physical cycle count discrepancy from AUDIT_RESULT table (Verified Physical Audit)."

    def get_labels(
        self,
        df_features: pd.DataFrame,
        run_date: str,
        conn_stock: Optional[sqlite3.Connection] = None
    ) -> pd.DataFrame:
        target_db = Path(self.db_path)
        if not target_db.exists():
            raise FileNotFoundError(f"Database not found at {target_db}")

        uri_path = f"file:{target_db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri_path, uri=True)
        try:
            # Query confirmed physical audit results for the run_date
            query = """
            SELECT r.result_id, r.assignment_id, r.run_id, r.bin, r.mat_no,
                   r.counted_at, r.book_qty_at_count, r.counted_qty,
                   r.diff_qty, r.diff_amount, r.cause_code,
                   r.minutes_spent, r.counter_id, r.is_recount,
                   r.is_confirmed, r.audit_origin, r.data_origin,
                   r.is_discrepancy, r.label_source, r.field_notes
            FROM AUDIT_RESULT r
            JOIN AUDIT_RUN m ON r.run_id = m.run_id
            WHERE m.run_date = ?
              AND r.audit_origin = 'PHYSICAL_CYCLE_COUNT'
              AND r.is_confirmed = 1
            ORDER BY r.assignment_id, r.is_recount DESC, r.counted_at DESC;
            """
            df_res = pd.read_sql_query(query, conn, params=[run_date])
        finally:
            conn.close()

        if df_res.empty:
            logger.warning(f"No confirmed AUDIT_RESULT records found for run_date '{run_date}'.")
            unique_bins = df_features["bin"].unique() if "bin" in df_features.columns else []
            return pd.DataFrame({
                "bin": unique_bins,
                "label": np.nan,
                "discrepancy_qty": 0.0,
                "financial_exposure": 0.0,
                "cause_code": "UNAUDITED",
                "label_source": self.name,
                "is_weak_proxy": False,
                "is_audited": 0,
                "notes": "No audit results recorded"
            })

        if not self.allow_synthetic:
            df_res = df_res[df_res["data_origin"] == "PHYSICAL"].copy()

        self.has_synthetic = bool((df_res["data_origin"] == "SYNTHETIC_SEED").any()) if not df_res.empty else False
        self.data_origin = "SYNTHETIC_SEED" if self.has_synthetic else "PHYSICAL"

        # Deduplicate to keep the latest confirmed recount per physical bin
        df_res_dedup = df_res.drop_duplicates(subset=["bin"]).copy()

        # Compute Ground Truth binary label:
        # Prioritize explicit is_discrepancy flag if present; fallback to diff_qty and cause_code
        def _is_true_error(row):
            if pd.notna(row.get("is_discrepancy")):
                return int(row["is_discrepancy"])
            cause = str(row["cause_code"]).strip()
            diff_val = row.get("diff_qty")
            diff = abs(float(diff_val)) if diff_val is not None and not pd.isna(diff_val) else 0.0
            if cause in ["CC08_MATCH_VERIFIED", "CC07_IN_FLIGHT_MOVEMENT"]:
                return 0
            return 1 if diff > 0.001 or cause.startswith("CC") else 0

        df_res_dedup["label"] = df_res_dedup.apply(_is_true_error, axis=1)
        df_res_dedup["discrepancy_qty"] = df_res_dedup["diff_qty"].abs().fillna(0.0)
        df_res_dedup["financial_exposure"] = df_res_dedup["diff_amount"].abs().fillna(0.0)
        df_res_dedup["label_source"] = df_res_dedup["label_source"].fillna(self.name)
        df_res_dedup["is_weak_proxy"] = False
        df_res_dedup["is_audited"] = 1
        df_res_dedup["notes"] = df_res_dedup["cause_code"]

        # Merge with all bins in the feature snapshot to preserve the full floor population
        if "bin" in df_features.columns:
            all_bins = pd.DataFrame({"bin": df_features["bin"].unique()})
            merged = all_bins.merge(
                df_res_dedup[["bin", "label", "discrepancy_qty", "financial_exposure", "cause_code", "label_source", "is_weak_proxy", "is_audited", "notes"]],
                on="bin",
                how="left"
            )
            # DO NOT fillna(0) for label - preserve NaN for un-audited bins!
            merged["discrepancy_qty"] = merged["discrepancy_qty"].fillna(0.0)
            merged["financial_exposure"] = merged["financial_exposure"].fillna(0.0)
            merged["cause_code"] = merged["cause_code"].fillna("UNAUDITED")
            merged["label_source"] = self.name
            merged["is_weak_proxy"] = False
            merged["is_audited"] = merged["is_audited"].fillna(0).astype(int)
            merged["notes"] = merged["notes"].fillna("Unselected Floor")

            audited_cnt = int(merged["is_audited"].sum())
            pos_cnt = int(merged[merged["is_audited"] == 1]["label"].sum())
            logger.info(
                f"   [GROUND TRUTH AUDIT LABEL] Audited bins: {audited_cnt}, Discrepancies confirmed: {pos_cnt} "
                f"({pos_cnt / max(1, audited_cnt):.2%}). Origin: {self.data_origin}"
            )
            return merged
        else:
            return df_res_dedup


# ==============================================================================
# 2. Point-in-Time Dataset Builder
# ==============================================================================

def load_raw_features_and_assignments(
    run_date: str,
    db_path: Optional[Union[str, Path]] = None
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    """
    Reads AUDIT_FEATURE_SNAPSHOT, AUDIT_RUN, and AUDIT_ASSIGNMENT for a specific run_date.
    """
    target_db = Path(db_path or DEFAULT_STOCK_DB)
    if not target_db.exists():
        raise FileNotFoundError(f"Stock accuracy database not found at {target_db}")

    uri = f"file:{target_db.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        # 1. Fetch Run Master
        cur = conn.cursor()
        cur.execute(
            "SELECT run_id, run_date, mode, scoring_version, config_hash, created_at "
            "FROM AUDIT_RUN WHERE run_date = ? ORDER BY created_at DESC LIMIT 1;",
            [run_date]
        )
        run_row = cur.fetchone()
        if not run_row:
            # Fallback: check if run_date matches run_id prefix
            cur.execute(
                "SELECT run_id, run_date, mode, scoring_version, config_hash, created_at "
                "FROM AUDIT_RUN WHERE run_id LIKE ? ORDER BY created_at DESC LIMIT 1;",
                [f"{run_date.replace('-', '')}%"]
            )
            run_row = cur.fetchone()

        if not run_row:
            raise ValueError(f"No AUDIT_RUN entry found for date '{run_date}' in {target_db.name}")

        run_info = {
            "run_id": run_row[0],
            "run_date": run_row[1],
            "mode": run_row[2],
            "scoring_version": run_row[3],
            "config_hash": run_row[4],
            "run_created_at": run_row[5]
        }
        run_id = run_info["run_id"]

        # 2. Fetch Feature Snapshot
        query_features = """
        SELECT 
            id, run_id, barcode, mat_no, bin, plant, tier, qty, gr_date,
            warehouse_dwell_days, bin_dwell_days, lot_age_days, ontology_turnover_days,
            mover_class, missing_gi_flag, missing_gi_diff_box, fifo_bypass_gap_days,
            same_bin_lot_spread_days, risk_score, risk_categories, score_breakdown_json,
            ztcode_stock_rec_diff_qty, ztcode_stock_rec_consecutive_days, ztcode_stock_rec_exposure,
            net_price, p_error_proxy, expected_loss, est_minutes, priority_ev, created_at
        FROM AUDIT_FEATURE_SNAPSHOT
        WHERE run_id = ?;
        """
        df_features = pd.read_sql_query(query_features, conn, params=[run_id])

        # 3. Fetch Audit Assignments
        query_assignments = """
        SELECT 
            assignment_id, run_id, bin, rank AS selection_rank, list_type,
            selection_method, bin_risk_score, action_required, primary_reason,
            materials_json, label_qty, plant, tier, inclusion_probability, created_at
        FROM AUDIT_ASSIGNMENT
        WHERE run_id = ?;
        """
        df_assignments = pd.read_sql_query(query_assignments, conn, params=[run_id])

    finally:
        conn.close()

    logger.info(f"Loaded {len(df_features)} snapshot items and {len(df_assignments)} assignments for Run {run_id} ({run_date}).")
    return df_features, df_assignments, run_info


def aggregate_features_to_bin_level(
    df_features: pd.DataFrame,
    df_assignments: pd.DataFrame,
    run_info: Dict[str, Any]
) -> pd.DataFrame:
    """
    Aggregates barcode/item-level feature snapshots into physical bin-level records.
    
    Preserves:
    - max risk score (the domain rule score driving selection)
    - sum of expected loss and net monetary exposure
    - max priority EV
    - est_minutes (duration)
    - box count and total qty
    - mover class of highest risk item
    - multi-hot risk categories
    - assignment rank, list_type, inclusion_probability
    """
    if df_features.empty:
        return pd.DataFrame()

    # Pre-parse risk categories multi-hot
    known_categories = [
        "CRITICAL_GHOST", "FIFO_VIOLATION", "SAME_BIN_DIFF_LOT",
        "AGING_SLOW_STOCK", "HIGH_RACK_DWELL", "SMT_STAGNANT",
        "TURNOVER_ANOMALY", "UNIT_OFFSET_RECOVERY"
    ]

    records = []
    # Group by physical bin
    for bin_code, group in df_features.groupby("bin"):
        # Highest risk row
        top_row = group.sort_values(by="risk_score", ascending=False).iloc[0]
        
        # All categories in this bin
        all_cats_str = " | ".join(group["risk_categories"].dropna().astype(str).tolist())
        
        # Multi-hot dict
        multi_hot = {
            f"cat_{cat}": 1 if cat in all_cats_str else 0
            for cat in known_categories
        }

        # Pricing & EV
        sum_exp_loss = group["expected_loss"].fillna(0.0).sum()
        est_min = top_row["est_minutes"]
        if pd.isna(est_min) or est_min is None or est_min <= 0:
            est_min = 2.0  # standard default

        max_p_ev = group["priority_ev"].fillna(0.0).max()
        if max_p_ev == 0.0 and sum_exp_loss > 0:
            max_p_ev = sum_exp_loss / est_min

        rec = {
            "bin": bin_code,
            "run_id": run_info["run_id"],
            "run_date": run_info["run_date"],
            "scoring_version": run_info.get("scoring_version", "unknown"),
            "config_hash": run_info.get("config_hash", "unknown"),
            "plant": top_row.get("plant", _get_ops_env("SAP_PLANT", "P001")),
            "tier": top_row.get("tier", 1),
            "box_count": len(group),
            "total_qty": group["qty"].fillna(0.0).sum(),
            "unique_materials_count": group["mat_no"].nunique(),
            "primary_mat_no": top_row["mat_no"],
            "primary_mover_class": top_row["mover_class"] if pd.notna(top_row["mover_class"]) else "Normal",
            
            # Risk & Aging Metrics
            "bin_risk_score": group["risk_score"].max(),
            "avg_risk_score": group["risk_score"].mean(),
            "max_warehouse_dwell_days": group["warehouse_dwell_days"].max(),
            "max_bin_dwell_days": group["bin_dwell_days"].max(),
            "max_lot_age_days": group["lot_age_days"].fillna(0).max(),
            "min_ontology_turnover_days": group["ontology_turnover_days"].fillna(30.0).min(),
            "max_fifo_gap_days": group["fifo_bypass_gap_days"].fillna(0).max(),
            "max_lot_spread_days": group["same_bin_lot_spread_days"].fillna(0).max(),
            "max_missing_gi_diff_box": group["missing_gi_diff_box"].fillna(0.0).max(),
            
            # ZTCODE_STOCK_REC Features (Point-in-time)
            "ztcode_stock_rec_diff_qty": group["ztcode_stock_rec_diff_qty"].fillna(0.0).max(),
            "ztcode_stock_rec_consecutive_days": group["ztcode_stock_rec_consecutive_days"].fillna(0).max(),
            "ztcode_stock_rec_exposure": group["ztcode_stock_rec_exposure"].fillna(0.0).max(),

            # Shadow EV Metrics
            "max_p_error_proxy": group["p_error_proxy"].fillna(0.0).max(),
            "sum_expected_loss": sum_exp_loss,
            "est_minutes": est_min,
            "priority_ev": max_p_ev,
            
            # Audit assignment metadata placeholder
            "is_selected": 0,
            "selection_rank": 9999,
            "list_type": "UNSELECTED",
            "selection_method": "NONE",
            "inclusion_probability": 0.001,
            "created_at": top_row["created_at"]
        }
        rec.update(multi_hot)
        records.append(rec)

    df_bin = pd.DataFrame(records)

    # Merge assignment metadata
    if not df_assignments.empty:
        assign_map = df_assignments.set_index("bin").to_dict(orient="index")
        for idx, row in df_bin.iterrows():
            b = row["bin"]
            if b in assign_map:
                a_info = assign_map[b]
                df_bin.at[idx, "is_selected"] = 1
                df_bin.at[idx, "selection_rank"] = a_info["selection_rank"]
                df_bin.at[idx, "list_type"] = a_info["list_type"]
                df_bin.at[idx, "selection_method"] = a_info["selection_method"]
                df_bin.at[idx, "inclusion_probability"] = a_info.get("inclusion_probability", 1.0)

    # Impute unselected inclusion probability for Horvitz-Thompson weighting
    unselected_count = (df_bin["is_selected"] == 0).sum()
    random_sample_target = 3  # From README Section 6 (3 random validation bins)
    default_unselected_prob = random_sample_target / max(1, unselected_count)
    df_bin.loc[df_bin["is_selected"] == 0, "inclusion_probability"] = default_unselected_prob

    return df_bin


def build_dataset_for_run(
    run_date: str,
    label_plugin: Optional[BaseLabelPlugin] = None,
    db_path: Optional[Union[str, Path]] = None,
    ztcode_stock_rec_db_path: Optional[Union[str, Path]] = None,
    enforce_leakage_check: bool = True
) -> pd.DataFrame:
    """
    High-level entrypoint: Loads point-in-time features, verifies zero data leakage,
    aggregates to physical bin level, and joins labels from the plugin.
    
    Args:
        run_date: 'YYYY-MM-DD'
        label_plugin: Plugin instance (defaults to Zm212ProxyLabel)
        db_path: Path to stock_accuracy DB (or dev DB)
        ztcode_stock_rec_db_path: Path to reconciliation DB
        enforce_leakage_check: Whether to raise on leakage
        
    Returns:
        pd.DataFrame ready for training or evaluation.
    """
    logger.info(f"================================================================")
    logger.info(f"🏗️  [BUILD DATASET] Constructing dataset for Run Date: {run_date}")
    logger.info(f"================================================================")

    # 1. Load Raw Snapshots
    df_raw_features, df_assignments, run_info = load_raw_features_and_assignments(run_date, db_path)

    # 2. Strict Data Leakage Check
    if enforce_leakage_check:
        verify_no_data_leakage(df_raw_features, run_date)

    # 3. Label Plugin Resolution
    plugin = label_plugin or Zm212ProxyLabel(ztcode_stock_rec_db_path=ztcode_stock_rec_db_path)
    logger.info(f"Label Plugin: [{plugin.name}] (Weak Proxy: {plugin.is_weak_proxy})")

    # 4. Generate Labels
    df_labels = plugin.get_labels(df_raw_features, run_date)

    # 5. Aggregate Features to Physical Bin Level
    df_bin = aggregate_features_to_bin_level(df_raw_features, df_assignments, run_info)

    # 6. Point-in-Time Join
    df_dataset = df_bin.merge(df_labels, on="bin", how="left")
    if plugin.is_weak_proxy:
        df_dataset["label"] = df_dataset["label"].fillna(0).astype(int)
    # Note: for ground truth (not is_weak_proxy), un-audited bins retain NaN to prevent selection bias
    df_dataset["label_source"] = plugin.name
    df_dataset["is_weak_proxy"] = plugin.is_weak_proxy

    pos_cnt = int((df_dataset["label"] == 1).sum())
    valid_cnt = int(df_dataset["label"].notna().sum())
    rate_str = f"{pos_cnt / max(1, valid_cnt):.2%}" if valid_cnt > 0 else "N/A"
    logger.info(
        f"✅ Dataset successfully created: {len(df_dataset)} bins ({valid_cnt} labeled). "
        f"Positive labels: {pos_cnt} ({rate_str})."
    )
    return df_dataset


def build_training_dataset(
    run_dates: List[str],
    label_plugin: Optional[BaseLabelPlugin] = None,
    db_path: Optional[Union[str, Path]] = None,
    filter_audited_only: bool = True
) -> pd.DataFrame:
    """
    Builds a concatenated training dataset across multiple run dates.
    If filter_audited_only is True, filters to rows where label is not NaN (is_audited == 1).
    """
    frames = []
    for d in run_dates:
        df_d = build_dataset_for_run(d, label_plugin=label_plugin, db_path=db_path)
        frames.append(df_d)
    if not frames:
        return pd.DataFrame()
    full_df = pd.concat(frames, ignore_index=True)
    if filter_audited_only:
        full_df = full_df[full_df["label"].notna()].copy()
        full_df["label"] = full_df["label"].astype(int)
    return full_df


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Build offline training/eval dataset")
    parser.add_argument("--run-date", type=str, default="2026-10-26", help="Run date (YYYY-MM-DD)")
    parser.add_argument("--db-path", type=str, default=str(DEFAULT_STOCK_DB), help="Stock accuracy DB path")
    parser.add_argument("--ztcode_stock_rec-db-path", type=str, default=str(DEFAULT_ZTCODE_STOCK_REC_DB), help="ZTCODE_STOCK_REC DB path")
    parser.add_argument("--label-source", type=str, choices=["audit_result", "proxy"], default="audit_result",
                        help="Label source plugin ('audit_result' or 'proxy')")
    parser.add_argument("--output", type=str, default=None, help="Optional CSV output path")
    args = parser.parse_args()

    if args.label_source == "audit_result":
        plugin = AuditResultLabel(db_path=args.db_path)
    else:
        plugin = Zm212ProxyLabel(ztcode_stock_rec_db_path=args.ztcode_stock_rec_db_path)

    ds = build_dataset_for_run(
        run_date=args.run_date,
        label_plugin=plugin,
        db_path=args.db_path,
        ztcode_stock_rec_db_path=args.ztcode_stock_rec_db_path
    )
    if args.output:
        out_p = Path(args.output)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        ds.to_csv(out_p, index=False)
        print(f"Saved dataset to {out_p}")
    else:
        print(ds[["bin", "bin_risk_score", "sum_expected_loss", "priority_ev", "label"]].head(10))
