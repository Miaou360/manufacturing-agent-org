"""
Audit Hit Rate Calculator (D34 - 실사 리스트 타율 및 적중률 추이 연산기)
========================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / hit_rate_calculator.py

Core Responsibilities:
1. Computes physical audit hit rate (discrepancy detection rate) per audit run.
2. Segregates TOP (1~15) vs EXTENDED (16~40) vs MODEL Overall (TOP+EXTENDED).
3. Segregates rank tiers (1~5, 6~15, 16~40) to monitor monotonic precision.
4. Computes rolling 7-day and 28-day cumulative hit rates (sum of discrepancies / sum of samples).
5. Computes 28-day rolling random validation baseline and model Lift.
6. Enforces Ground Truth Guardrails (AGENTS.md Rule 9):
   - Excludes uncounted bins from the denominator (pure factual integrity).
   - Strictly excludes synthetic/seed/test data origins.
   - Handles missing random baseline gracefully (Lift=None, sample=0).
7. Persists daily metrics to AUDIT_HIT_RATE_DAILY.
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
from typing import Optional, List, Dict, Any, Tuple
import pandas as pd

import importlib.util

CURRENT_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("stock_acc_db_mgr", CURRENT_DIR / "db_manager.py")
_db_mgr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_db_mgr)

get_connection = _db_mgr.get_connection
init_database = _db_mgr.init_database
save_hit_rate_record = _db_mgr.save_hit_rate_record
get_hit_rate_records = _db_mgr.get_hit_rate_records
save_rule_hit_rates = getattr(_db_mgr, "save_rule_hit_rates", None)
get_rule_hit_rates = getattr(_db_mgr, "get_rule_hit_rates", None)
save_historical_discrepancy_scores = getattr(_db_mgr, "save_historical_discrepancy_scores", None)
get_top_discrepancy_history = getattr(_db_mgr, "get_top_discrepancy_history", None)
DB_PATH = _db_mgr.DB_PATH

# D74 Stage 1: Canonical Rules and Category Mappings
CANONICAL_RULES = {
    "GHOST_MISSING_GI": ("Ghost Stock (Missing GI)", "DEFICIT", lambda r: "Ghost" in r or "Missing GI" in r),
    "SMT_STAGNANT": ("SMT Stagnant Buffer", "AGING", lambda r: "SMT Stagnant" in r or "Floor Buffer Stagnant" in r),
    "FLOOR_FIFO_BYPASS": ("Floor FIFO Bypass", "FIFO", lambda r: "Floor FIFO Bypass" in r or "FIFO Bypass" in r),
    "SAME_BIN_SPREAD": ("Same-Bin Lot Spread", "LOT", lambda r: "Same-Bin Spread" in r),
    "SMALL_PART_STAGNANT": ("Small Part Stagnant/Aging", "AGING", lambda r: "Small Part" in r),
    "FIFO_INVERSION": ("FIFO Inversion (Stock)", "FIFO", lambda r: "FIFO Inversion" in r),
    "REPACK_STAGNANT": ("Repack Cart Stagnant", "AGING", lambda r: "Repack" in r),
    "MISSING_GI_SPIKE": ("Missing GI Spike Anomaly", "ANOMALY", lambda r: "Missing GI Spike" in r),
    "HIGH_RACK_AGING": ("High-Rack Aging", "AGING", lambda r: "High-Rack Aging" in r),
    "TURNOVER_ANOMALY": ("Turnover Anomaly", "ANOMALY", lambda r: "Turnover Anomaly" in r),
    "UNIT_OFFSET_BASELINE": ("Unit Offset Baseline", "BASELINE", lambda r: "Unit Offset" in r),
    "GR_CHAIN_GAP": ("GR Chain Gap", "INTERFACE", lambda r: "GR Chain Gap" in r or "Chain Gap" in r),
    "RANDOM_VALIDATION": ("Random Validation Sample", "EXPLORATION", lambda r: "Random sample" in r),
}

logger = logging.getLogger("AuditHitRateCalculator")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [HitRateCalc] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def compute_hit_rate_for_run(
    run_id: str,
    db_path: Optional[Path] = None,
    save_to_db: bool = True
) -> Dict[str, Any]:
    """
    Computes comprehensive hit rate metrics for a given audit run.
    
    Metrics:
    - TOP (1~15): sample, discrepancies, hit rate
    - EXTENDED (16~40): sample, discrepancies, hit rate
    - MODEL Overall: sample, discrepancies, hit rate
    - Rank 1~5, Rank 6~15, Rank 16~40 breakdowns
    - Cumulative 7-day and 28-day model hit rates (rolling numerator/denominator sum)
    - 28-day rolling RANDOM validation baseline and Lift
    
    Pure Factual Integrity:
    - Excludes uncounted bins (NULL result) from denominator.
    - Excludes synthetic/seed data.
    - If random baseline sample is 0, baseline hit rate and lift are None.
    """
    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    # 1. Fetch Audit Run Info
    cur.execute("""
        SELECT run_id, run_date, scoring_version
        FROM AUDIT_RUN
        WHERE run_id = ?;
    """, (run_id,))
    run_row = cur.fetchone()
    if not run_row:
        conn.close()
        raise ValueError(f"Run ID '{run_id}' not found in AUDIT_RUN.")

    audit_date = run_row["run_date"]
    scoring_version = run_row["scoring_version"] or "unknown"

    # 2. Fetch Assignments and Confirmed Results for Current Run
    # Using CTE to select the latest confirmed result per assignment and exclude synthetic data
    cur.execute("""
        WITH ranked_results AS (
            SELECT 
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id 
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT 
            a.assignment_id,
            a.bin,
            a.rank,
            a.list_type,
            rr.is_discrepancy,
            rr.counted_qty
        FROM AUDIT_ASSIGNMENT a
        LEFT JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE a.run_id = ?
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
        ORDER BY a.rank ASC;
    """, (run_id,))
    rows = [dict(r) for r in cur.fetchall()]

    # 3. Categorize and Calculate Current Run Metrics
    top_sample = 0
    top_disc = 0
    ext_sample = 0
    ext_disc = 0
    rand_sample_day = 0
    rand_disc_day = 0

    r1_5_sample = 0
    r1_5_disc = 0
    r6_15_sample = 0
    r6_15_disc = 0
    r16_40_sample = 0
    r16_40_disc = 0

    for r in rows:
        is_counted = (r["counted_qty"] is not None or r["is_discrepancy"] is not None)
        if not is_counted:
            continue  # Pure factual integrity (Rule 9 / 044): strictly exclude uncounted bins (counted_qty IS NULL)

        is_disc = (r["is_discrepancy"] == 1)
        l_type = (r["list_type"] or "").upper()
        rank = int(r["rank"] or 0)

        # List Type classification
        if l_type == "TOP" or (not l_type and 1 <= rank <= 15):
            top_sample += 1
            if is_disc:
                top_disc += 1
        elif l_type == "EXTENDED" or (not l_type and 16 <= rank <= 40):
            ext_sample += 1
            if is_disc:
                ext_disc += 1
        elif "RANDOM" in l_type:
            rand_sample_day += 1
            if is_disc:
                rand_disc_day += 1

        # Rank Bucket classification
        if 1 <= rank <= 5:
            r1_5_sample += 1
            if is_disc:
                r1_5_disc += 1
        elif 6 <= rank <= 15:
            r6_15_sample += 1
            if is_disc:
                r6_15_disc += 1
        elif 16 <= rank <= 40:
            r16_40_sample += 1
            if is_disc:
                r16_40_disc += 1

    model_sample = top_sample + ext_sample
    model_disc = top_disc + ext_disc

    hit_rate_top = round(top_disc / top_sample, 4) if top_sample > 0 else None
    hit_rate_extended = round(ext_disc / ext_sample, 4) if ext_sample > 0 else None
    hit_rate_model = round(model_disc / model_sample, 4) if model_sample > 0 else None

    rank_1_5_hit_rate = round(r1_5_disc / r1_5_sample, 4) if r1_5_sample > 0 else None
    rank_6_15_hit_rate = round(r6_15_disc / r6_15_sample, 4) if r6_15_sample > 0 else None
    rank_16_40_hit_rate = round(r16_40_disc / r16_40_sample, 4) if r16_40_sample > 0 else None

    # 4. Rolling Window Calculations (7-day and 28-day)
    try:
        current_dt = datetime.date.fromisoformat(str(audit_date)[:10])
    except Exception:
        current_dt = datetime.date.today()

    d7_start = (current_dt - datetime.timedelta(days=6)).isoformat()
    d28_start = (current_dt - datetime.timedelta(days=27)).isoformat()
    audit_date_str = current_dt.isoformat()

    cur.execute("""
        WITH ranked_results AS (
            SELECT 
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id 
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT 
            ar.run_date,
            a.list_type,
            rr.is_discrepancy
        FROM AUDIT_RUN ar
        JOIN AUDIT_ASSIGNMENT a ON ar.run_id = a.run_id
        LEFT JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE ar.run_id NOT LIKE '%QUARANTINED%'
          AND ar.run_date BETWEEN ? AND ?
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
          AND (rr.counted_qty IS NOT NULL OR rr.is_discrepancy IS NOT NULL);
    """, (d28_start, audit_date_str))
    hist_rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # 7-day model cumulative
    d7_model_sample = 0
    d7_model_disc = 0
    # 28-day model cumulative
    d28_model_sample = 0
    d28_model_disc = 0
    # 28-day random baseline
    d28_rand_sample = 0
    d28_rand_disc = 0

    for hr in hist_rows:
        h_date = str(hr["run_date"])[:10]
        h_type = (hr["list_type"] or "").upper()
        h_disc = (hr["is_discrepancy"] == 1)

        # Random baseline (28d window)
        if "RANDOM" in h_type:
            d28_rand_sample += 1
            if h_disc:
                d28_rand_disc += 1
        elif h_type in ("TOP", "EXTENDED") or not h_type:
            # 28-day model
            d28_model_sample += 1
            if h_disc:
                d28_model_disc += 1

            # 7-day model
            if h_date >= d7_start:
                d7_model_sample += 1
                if h_disc:
                    d7_model_disc += 1

    cumulative_7d_hit_rate = round(d7_model_disc / d7_model_sample, 4) if d7_model_sample > 0 else hit_rate_model
    cumulative_28d_hit_rate = round(d28_model_disc / d28_model_sample, 4) if d28_model_sample > 0 else hit_rate_model

    hit_rate_random_28d = round(d28_rand_disc / d28_rand_sample, 4) if d28_rand_sample > 0 else None
    lift = None
    if hit_rate_random_28d is not None and hit_rate_random_28d > 0 and hit_rate_model is not None:
        lift = round(hit_rate_model / hit_rate_random_28d, 2)

    calculated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build Notes
    notes_parts = []
    if d28_rand_sample == 0:
        notes_parts.append("Random baseline sample n=0 (Lift unavailable)")
    else:
        notes_parts.append(f"Random baseline 28d n={d28_rand_sample}")

    uncounted_count = len(rows) - model_sample - rand_sample_day
    if uncounted_count > 0:
        notes_parts.append(f"{uncounted_count} uncounted bins (counted_qty IS NULL AND is_discrepancy IS NULL, Rule 9) excluded")

    notes = "; ".join(notes_parts)

    record = {
        "run_id": run_id,
        "audit_date": audit_date_str,
        "calculated_at": calculated_at,
        "scoring_version": scoring_version,
        "sample_size_top": top_sample,
        "discrepancy_count_top": top_disc,
        "hit_rate_top": hit_rate_top,
        "sample_size_extended": ext_sample,
        "discrepancy_count_extended": ext_disc,
        "hit_rate_extended": hit_rate_extended,
        "sample_size_model": model_sample,
        "discrepancy_count_model": model_disc,
        "hit_rate_model": hit_rate_model,
        "sample_size_random_28d": d28_rand_sample,
        "discrepancy_count_random_28d": d28_rand_disc,
        "hit_rate_random_28d": hit_rate_random_28d,
        "lift": lift,
        "rank_1_5_sample": r1_5_sample,
        "rank_1_5_discrepancies": r1_5_disc,
        "rank_1_5_hit_rate": rank_1_5_hit_rate,
        "rank_6_15_sample": r6_15_sample,
        "rank_6_15_discrepancies": r6_15_disc,
        "rank_6_15_hit_rate": rank_6_15_hit_rate,
        "rank_16_40_sample": r16_40_sample,
        "rank_16_40_discrepancies": r16_40_disc,
        "rank_16_40_hit_rate": rank_16_40_hit_rate,
        "cumulative_7d_hit_rate": cumulative_7d_hit_rate,
        "cumulative_28d_hit_rate": cumulative_28d_hit_rate,
        "notes": notes
    }

    if save_to_db:
        save_hit_rate_record(record, db_path=target_db)
        try:
            if save_rule_hit_rates:
                compute_rule_hit_rates(run_id=run_id, db_path=target_db, save_to_db=True)
            if save_historical_discrepancy_scores:
                compute_historical_discrepancy_scores(db_path=target_db, save_to_db=True)
        except Exception as e:
            logger.warning(f"Failed to auto-update rule hit rates / historical scores for {run_id}: {e}")

        def _f(v):
            return f"{v:.1%}" if v is not None else "N/A"
        logger.info(
            f"✅ [D34] Hit rate computed for {run_id} ({audit_date_str}): "
            f"MODEL={_f(hit_rate_model)} (TOP={_f(hit_rate_top)}, EXT={_f(hit_rate_extended)}), "
            f"Rank 1~5={_f(rank_1_5_hit_rate)}, 6~15={_f(rank_6_15_hit_rate)}, 16~40={_f(rank_16_40_hit_rate)}"
        )

    return record


def compute_rule_hit_rates(
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    save_to_db: bool = True,
    alpha: float = 1.0,
    beta: float = 3.0
) -> List[Dict[str, Any]]:
    """
    Computes hit rate and Beta-smoothed score per canonical rule and selection method (D74 Stage 1).
    
    If run_id is provided, calculates for that specific run.
    If run_id is None, calculates cumulative across all active confirmed runs.
    
    Beta-smoothing:
      Smoothed Score = (d + alpha) / (n + alpha + beta)
      Default prior Beta(1, 3) shrinks small sample sizes towards the ~25% warehouse baseline,
      preventing extreme 0% or 100% volatility on 1-2 counts.
    """
    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    query = """
        WITH ranked_results AS (
            SELECT 
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id 
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT 
            a.assignment_id,
            a.run_id,
            a.bin,
            a.rank,
            a.list_type,
            a.selection_method,
            a.primary_reason,
            rr.is_discrepancy,
            EXISTS (
                SELECT 1 FROM AUDIT_FEATURE_SNAPSHOT fs
                WHERE fs.run_id = a.run_id AND fs.bin = a.bin AND COALESCE(fs.chain_gap_flag, 0) = 1
            ) as has_chain_gap_feature
        FROM AUDIT_ASSIGNMENT a
        JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE a.run_id NOT LIKE '%QUARANTINED%'
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
          AND (rr.counted_qty IS NOT NULL OR rr.is_discrepancy IS NOT NULL)
    """
    params = []
    if run_id:
        query += " AND a.run_id = ?"
        params.append(run_id)

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    effective_run_id = run_id or "CUMULATIVE"
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    rule_stats = {}
    for code, (name, category, _) in CANONICAL_RULES.items():
        rule_stats[code] = {
            "run_id": effective_run_id,
            "rule_code": code,
            "rule_name": name,
            "category": category,
            "sample_size": 0,
            "discrepancy_count": 0,
            "hit_rate": None,
            "smoothed_score": round((0.0 + alpha) / (0.0 + alpha + beta), 4),
            "calculated_at": now_str
        }

    unmatched_count = 0
    unmatched_disc = 0
    unmatched_bins = []

    for row in rows:
        reason = str(row["primary_reason"] or "")
        is_disc = (row["is_discrepancy"] == 1)
        s_method = str(row["selection_method"] or "")

        matched_any = False
        for code, (name, category, match_fn) in CANONICAL_RULES.items():
            if match_fn(reason) or (code == "RANDOM_VALIDATION" and s_method == "RANDOM_SAMPLE") or (code == "GR_CHAIN_GAP" and bool(row.get("has_chain_gap_feature"))):
                rule_stats[code]["sample_size"] += 1
                if is_disc:
                    rule_stats[code]["discrepancy_count"] += 1
                matched_any = True

        if not matched_any:
            unmatched_count += 1
            if is_disc:
                unmatched_disc += 1
            unmatched_bins.append(f"{row['bin']}({reason[:20]})")

    if unmatched_count > 0:
        logger.warning(
            f"⚠️ [D74 Rule Hit Rate] {unmatched_count}/{len(rows)} counted bins unmatched by canonical rules: "
            f"{', '.join(unmatched_bins[:5])}"
        )
    else:
        logger.info(f"✅ [D74 Rule Hit Rate] All {len(rows)} counted bins successfully mapped to canonical rules (unmatched=0).")

    results = []
    for code, stat in rule_stats.items():
        n = stat["sample_size"]
        d = stat["discrepancy_count"]
        stat["hit_rate"] = round(d / n, 4) if n > 0 else None
        stat["smoothed_score"] = round((d + alpha) / (n + alpha + beta), 4)
        results.append(stat)

    if unmatched_count > 0:
        results.append({
            "run_id": effective_run_id,
            "rule_code": "UNCLASSIFIED",
            "rule_name": "⚠️ 미분류 빈 (Unclassified / Parsing Fallback)",
            "category": "ANOMALY",
            "sample_size": unmatched_count,
            "discrepancy_count": unmatched_disc,
            "hit_rate": round(unmatched_disc / unmatched_count, 4) if unmatched_count > 0 else None,
            "smoothed_score": round((unmatched_disc + alpha) / (unmatched_count + alpha + beta), 4),
            "calculated_at": now_str
        })

    # Sort: rules with samples first by hit_rate DESC, then sample_size DESC
    results.sort(key=lambda x: (x["sample_size"] > 0, x["hit_rate"] or 0.0, x["sample_size"]), reverse=True)

    if save_to_db and save_rule_hit_rates:
        save_rule_hit_rates(results, db_path=target_db)

    return results


def compute_historical_discrepancy_scores(
    db_path: Optional[Path] = None,
    save_to_db: bool = True,
    alpha: float = 1.0,
    beta: float = 3.0
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Computes historical discrepancy scores and Beta-smoothed rates for bins and materials (D74 Stage 1).
    
    Beta-smoothing:
      Smoothed Score = (d + alpha) / (n + alpha + beta)
      Prevents 1-2 count volatility (e.g. 1/1 does not become 100% permanent mark,
      0/1 does not become 0% clean clearance).
    """
    import json
    from collections import defaultdict

    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    query = """
        WITH ranked_results AS (
            SELECT 
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id 
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT 
            a.assignment_id,
            a.run_id,
            ar.run_date,
            a.bin,
            a.materials_json,
            rr.mat_no,
            rr.counted_qty,
            rr.is_discrepancy
        FROM AUDIT_ASSIGNMENT a
        JOIN AUDIT_RUN ar ON a.run_id = ar.run_id
        JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE a.run_id NOT LIKE '%QUARANTINED%'
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
          AND (rr.counted_qty IS NOT NULL OR rr.is_discrepancy IS NOT NULL)
        ORDER BY ar.run_date ASC;
    """
    cur.execute(query)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Bin Historical Aggregation
    bin_map = defaultdict(lambda: {
        "n": 0, "d": 0, "last_date": "", "last_run_id": "", "last_disc": 0
    })
    for r in rows:
        b = str(r["bin"] or "").strip()
        if not b:
            continue
        is_disc = (r["is_discrepancy"] == 1)
        bin_map[b]["n"] += 1
        if is_disc:
            bin_map[b]["d"] += 1
        bin_map[b]["last_date"] = str(r["run_date"] or "")[:10]
        bin_map[b]["last_run_id"] = str(r["run_id"] or "")
        bin_map[b]["last_disc"] = 1 if is_disc else 0

    bin_records = []
    for b, stat in bin_map.items():
        n = stat["n"]
        d = stat["d"]
        raw_hr = round(d / n, 4) if n > 0 else 0.0
        smoothed = round((d + alpha) / (n + alpha + beta), 4)
        bin_records.append({
            "entity_type": "BIN",
            "entity_id": b,
            "audit_count": n,
            "discrepancy_count": d,
            "raw_hit_rate": raw_hr,
            "smoothed_score": smoothed,
            "last_audit_date": stat["last_date"],
            "last_run_id": stat["last_run_id"],
            "last_discrepancy": stat["last_disc"],
            "associated_bins": [],
            "updated_at": now_str
        })
    bin_records.sort(key=lambda x: (x["smoothed_score"], x["discrepancy_count"], x["audit_count"]), reverse=True)

    # 2. Material Historical Aggregation
    mat_map = defaultdict(lambda: {
        "n": 0, "d": 0, "last_date": "", "last_run_id": "", "last_disc": 0, "bins": set()
    })
    for r in rows:
        m_str = str(r["mat_no"] or "")
        parts = [p.strip() for p in m_str.split(",") if p.strip()]
        if not parts and r["materials_json"]:
            try:
                parts = json.loads(r["materials_json"])
            except Exception:
                pass
        b = str(r["bin"] or "").strip()
        is_disc = (r["is_discrepancy"] == 1)
        for p in set(parts):
            mat_map[p]["n"] += 1
            if is_disc:
                mat_map[p]["d"] += 1
            if b:
                mat_map[p]["bins"].add(b)
            mat_map[p]["last_date"] = str(r["run_date"] or "")[:10]
            mat_map[p]["last_run_id"] = str(r["run_id"] or "")
            mat_map[p]["last_disc"] = 1 if is_disc else 0

    mat_records = []
    for m, stat in mat_map.items():
        n = stat["n"]
        d = stat["d"]
        raw_hr = round(d / n, 4) if n > 0 else 0.0
        smoothed = round((d + alpha) / (n + alpha + beta), 4)
        mat_records.append({
            "entity_type": "MATERIAL",
            "entity_id": m,
            "audit_count": n,
            "discrepancy_count": d,
            "raw_hit_rate": raw_hr,
            "smoothed_score": smoothed,
            "last_audit_date": stat["last_date"],
            "last_run_id": stat["last_run_id"],
            "last_discrepancy": stat["last_disc"],
            "associated_bins": sorted(list(stat["bins"])),
            "updated_at": now_str
        })
    mat_records.sort(key=lambda x: (x["smoothed_score"], x["discrepancy_count"], x["audit_count"]), reverse=True)

    if save_to_db and save_historical_discrepancy_scores:
        save_historical_discrepancy_scores(bin_records + mat_records, db_path=target_db)

    return {"bins": bin_records, "materials": mat_records}


def get_learning_loop_progress(db_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Returns data accumulation progress towards Stage 2 (Logistic Regression, n=50 discrepancies)
    and Stage 3 (LightGBM, n=200 discrepancies) per D74.
    """
    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    cur.execute("""
        WITH ranked_results AS (
            SELECT 
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id 
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT 
            COUNT(*) as total_labels,
            SUM(CASE WHEN rr.is_discrepancy = 1 THEN 1 ELSE 0 END) as total_discrepancies,
            COUNT(DISTINCT a.run_id) as total_runs
        FROM AUDIT_ASSIGNMENT a
        JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE a.run_id NOT LIKE '%QUARANTINED%'
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
          AND (rr.counted_qty IS NOT NULL OR rr.is_discrepancy IS NOT NULL);
    """)
    row = cur.fetchone()
    conn.close()

    total_labels = int(row["total_labels"] or 0)
    total_disc = int(row["total_discrepancies"] or 0)
    total_runs = int(row["total_runs"] or 0)

    overall_hit_rate = round(total_disc / total_labels, 4) if total_labels > 0 else 0.0

    stage2_target = 50
    stage2_remaining = max(0, stage2_target - total_disc)
    stage2_pct = min(100.0, round(total_disc / stage2_target * 100, 1))
    stage2_ready = total_disc >= stage2_target

    stage3_target = 200
    stage3_remaining = max(0, stage3_target - total_disc)
    stage3_pct = min(100.0, round(total_disc / stage3_target * 100, 1))
    stage3_ready = total_disc >= stage3_target

    return {
        "total_labels": total_labels,
        "total_discrepancies": total_disc,
        "total_runs": total_runs,
        "overall_hit_rate": overall_hit_rate,
        "stage2": {
            "name": "Stage 2: 정규화 로지스틱 회귀 (L2 Regularized Logistic Regression)",
            "target": stage2_target,
            "current": total_disc,
            "remaining": stage2_remaining,
            "progress_pct": stage2_pct,
            "is_ready": stage2_ready,
            "mode": "섀도 모드 (Shadow EV 엑셀 비교 시트)",
            "cadence": "주 1회 오프라인 재학습 (권장: 평일 18:10)",
        },
        "stage3": {
            "name": "Stage 3: LightGBM 비선형 모델 (LightGBM Gradient Boosting)",
            "target": stage3_target,
            "current": total_disc,
            "remaining": stage3_remaining,
            "progress_pct": stage3_pct,
            "is_ready": stage3_ready,
            "mode": "섀도 모드 (Shadow Model Comparison)",
            "cadence": "불일치 200건 도달 시 주 1회",
        },
        "promotion_rules": {
            "baseline": "무작위 검증 표본(Random Validation Sample)",
            "lift_condition": "규칙 점수 대비 Lift가 2주 연속 높을 때",
            "approval_required": True,
            "note": "월요일 주간 타율 메일에 승격 권고 후 사용자 명시적 승인 하에만 실사 순위 반영 (자동 승격 절대 금지)"
        }
    }


def backfill_all_hit_rates(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Backfills hit rate calculations for all historical runs with confirmed results.
    Excludes quarantined runs.
    """
    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    cur.execute("""
        SELECT DISTINCT ar.run_id, ar.run_date, ar.created_at
        FROM AUDIT_RUN ar
        JOIN AUDIT_RESULT r ON ar.run_id = r.run_id
        WHERE ar.run_id NOT LIKE '%QUARANTINED%'
          AND COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        ORDER BY ar.run_date ASC, ar.created_at ASC;
    """)
    runs = [dict(r) for r in cur.fetchall()]
    conn.close()

    results = []
    for r in runs:
        r_id = r["run_id"]
        try:
            res = compute_hit_rate_for_run(r_id, db_path=target_db, save_to_db=True)
            results.append(res)
        except Exception as e:
            logger.error(f"Failed to backfill run {r_id}: {e}")

    logger.info(f"📊 [D34 Backfill Complete] Computed hit rates for {len(results)} historical runs.")
    return results


def get_hit_rate_history(
    limit: int = 30,
    db_path: Optional[Path] = None
) -> pd.DataFrame:
    """
    Returns latest hit rate history as a pandas DataFrame.
    """
    records = get_hit_rate_records(limit=limit, db_path=db_path)
    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def generate_hit_rate_summary_text(
    run_id: str,
    db_path: Optional[Path] = None
) -> str:
    """
    Generates a formatted markdown summary of hit rate metrics for a run.
    """
    records = get_hit_rate_records(run_id=run_id, limit=1, db_path=db_path)
    if not records:
        return f"No hit rate records found for Run ID: {run_id}"

    rec = records[0]

    def fmt_pct(val):
        return f"{val:.1%}" if val is not None else "N/A"

    def fmt_lift(val):
        return f"{val:.2f}x" if val is not None else f"기준선 부족 (n={rec['sample_size_random_28d']})"

    lines = [
        f"### 📊 실사 리스트 타율 분석 리포트 (Run: {rec['run_id']} / {rec['audit_date']})",
        "",
        "| 구분 | 실사 표본 (n) | 불일치 빈 수 | 타율 (Hit Rate) |",
        "| :--- | :---: | :---: | :---: |",
        f"| **전체 모델 (TOP+EXT)** | **{rec['sample_size_model']}** | **{rec['discrepancy_count_model']}** | **{fmt_pct(rec['hit_rate_model'])}** |",
        f"| 🎯 TOP 15 우선 실사 | {rec['sample_size_top']} | {rec['discrepancy_count_top']} | {fmt_pct(rec['hit_rate_top'])} |",
        f"| 📦 EXTENDED 25 잔여 | {rec['sample_size_extended']} | {rec['discrepancy_count_extended']} | {fmt_pct(rec['hit_rate_extended'])} |",
        f"| 🎲 무작위 기준선 (28일 누적) | {rec['sample_size_random_28d']} | {rec['discrepancy_count_random_28d']} | {fmt_pct(rec['hit_rate_random_28d'])} |",
        "",
        f"- **모델 향상도 (Lift)**: **{fmt_lift(rec['lift'])}**",
        f"- **이동 누적 타율**: 7일 누적 **{fmt_pct(rec['cumulative_7d_hit_rate'])}** | 28일 누적 **{fmt_pct(rec['cumulative_28d_hit_rate'])}**",
        "",
        "#### 🎯 순위 구간별 적중률 (Rank Tier Breakdown)",
        "| 순위 구간 | 표본 수 (n) | 불일치 수 | 적중률 | 상태 |",
        "| :--- | :---: | :---: | :---: | :---: |",
        f"| **Rank 1 ~ 5 (초고위험)** | {rec['rank_1_5_sample']} | {rec['rank_1_5_discrepancies']} | {fmt_pct(rec['rank_1_5_hit_rate'])} | 표본 축적 중 (1회차 n={rec['rank_1_5_sample']}, 판단 유보) |",
        f"| **Rank 6 ~ 15 (고위험)** | {rec['rank_6_15_sample']} | {rec['rank_6_15_discrepancies']} | {fmt_pct(rec['rank_6_15_hit_rate'])} | 표본 축적 중 (1회차 n={rec['rank_6_15_sample']}, 판단 유보) |",
        f"| **Rank 16 ~ 40 (중위험)** | {rec['rank_16_40_sample']} | {rec['rank_16_40_discrepancies']} | {fmt_pct(rec['rank_16_40_hit_rate'])} | 표본 축적 중 (1회차 n={rec['rank_16_40_sample']}, 판단 유보) |",
        "",
        f"> 📌 **비고**: {rec['notes'] or '정상 기록'}"
    ]
    try:
        tracker = compute_daily_slot_reduction_tracker(run_id=run_id, db_path=db_path)
        lines.append("")
        lines.append(format_slot_reduction_tracker_report(tracker))
    except Exception as exc:
        logger.warning(f"[SlotReductionTracker] Skipped summary section for {run_id}: {exc}")
    return "\n".join(lines)


def compute_daily_slot_reduction_tracker(
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    #188 ⑥: Tracks rank-band hit rates (1-5 / 6-15 / 16-25 / 26-40 / RANDOM),
    cumulative daily trend, Claude's slot-reduction readiness criteria (>=10 daily runs &
    >=5 consecutive days with 26-40 <= RANDOM while upper tiers stay monotonic),
    estimated missed discrepancies if reduced (e.g. 43->35), and Weekly cross-check.
    """
    from collections import defaultdict

    target_db = Path(db_path or DB_PATH)
    init_database(target_db)
    conn = get_connection(target_db)
    cur = conn.cursor()

    cur.execute(
        """
        WITH ranked_results AS (
            SELECT
                r.*,
                ROW_NUMBER() OVER (
                    PARTITION BY r.assignment_id
                    ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                ) as rn
            FROM AUDIT_RESULT r
            WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
        )
        SELECT
            ar.run_id,
            ar.run_date,
            COALESCE(ar.mode, '') AS mode,
            a.bin,
            a.rank,
            COALESCE(a.list_type, '') AS list_type,
            rr.counted_qty,
            rr.is_discrepancy,
            COALESCE(rr.label_source, '') AS label_source
        FROM AUDIT_RUN ar
        JOIN AUDIT_ASSIGNMENT a ON ar.run_id = a.run_id
        JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
        WHERE ar.run_id NOT LIKE '%QUARANTINED%'
          AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
          AND (rr.counted_qty IS NOT NULL OR rr.is_discrepancy IS NOT NULL)
        ORDER BY ar.run_date ASC, ar.run_id ASC, a.rank ASC;
        """
    )
    all_rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    def _empty_band() -> Dict[str, Any]:
        return {"sample": 0, "hits": 0, "box_hits": 0, "pcs_hits": 0, "hit_rate": None}

    def _classify_band(rank: int, list_type: str) -> str:
        lt = (list_type or "").upper()
        if "RANDOM" in lt:
            return "RANDOM"
        if 1 <= rank <= 5:
            return "1-5"
        if 6 <= rank <= 15:
            return "6-15"
        if 16 <= rank <= 25:
            return "16-25"
        if 26 <= rank <= 40:
            return "26-40"
        return "41+"

    def _is_pcs_row(row: Dict[str, Any]) -> bool:
        return row.get("counted_qty") is None or "PCS" in str(row.get("label_source") or "").upper()

    rows_by_run: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    run_meta: Dict[str, Dict[str, str]] = {}
    for r in all_rows:
        rid = str(r["run_id"])
        rows_by_run[rid].append(r)
        if rid not in run_meta:
            m_str = str(r["mode"] or "").strip()
            if not m_str:
                m_str = "Weekly" if "WEEKLY" in rid.upper() else "Daily"
            run_meta[rid] = {"run_id": rid, "run_date": str(r["run_date"])[:10], "mode": m_str}

    daily_runs: List[Dict[str, Any]] = []
    weekly_runs: List[Dict[str, Any]] = []
    cum_bands = {b: _empty_band() for b in ("1-5", "6-15", "16-25", "26-40", "RANDOM")}
    missed_if_35_total = 0
    missed_if_38_total = 0
    daily_audited_bins_by_date: Dict[str, set] = defaultdict(set)

    for rid, meta in sorted(run_meta.items(), key=lambda kv: (kv[1]["run_date"], kv[0])):
        rlist = rows_by_run[rid]
        if meta["mode"].lower() == "weekly" or "WEEKLY" in rid.upper():
            weekly_runs.append({"meta": meta, "rows": rlist})
            continue

        bands = {b: _empty_band() for b in ("1-5", "6-15", "16-25", "26-40", "RANDOM")}
        missed_35_run = 0
        missed_38_run = 0
        for row in rlist:
            daily_audited_bins_by_date[meta["run_date"]].add(str(row["bin"]).strip())
            rk = int(row["rank"] or 0)
            lt = str(row["list_type"] or "")
            bkey = _classify_band(rk, lt)
            if bkey not in bands:
                continue
            is_disc = int(row["is_discrepancy"] or 0) == 1
            is_pcs = _is_pcs_row(row)
            for tgt in (bands[bkey], cum_bands[bkey]):
                tgt["sample"] += 1
                if is_disc:
                    tgt["hits"] += 1
                    if is_pcs:
                        tgt["pcs_hits"] += 1
                    else:
                        tgt["box_hits"] += 1
            if "RANDOM" not in lt.upper() and is_disc:
                if 33 <= rk <= 40:
                    missed_35_run += 1
                if 36 <= rk <= 40:
                    missed_38_run += 1

        missed_if_35_total += missed_35_run
        missed_if_38_total += missed_38_run
        for bk, bv in bands.items():
            bv["hit_rate"] = round(bv["hits"] / bv["sample"], 4) if bv["sample"] > 0 else None

        cum_rand_hr = (
            round(cum_bands["RANDOM"]["hits"] / cum_bands["RANDOM"]["sample"], 4)
            if cum_bands["RANDOM"]["sample"] > 0
            else bands["RANDOM"]["hit_rate"]
        )
        hr_26_40 = bands["26-40"]["hit_rate"]
        le_random = (
            hr_26_40 is not None
            and cum_rand_hr is not None
            and hr_26_40 <= cum_rand_hr + 1e-9
        )
        daily_runs.append(
            {
                "run_id": rid,
                "run_date": meta["run_date"],
                "bands": bands,
                "cum_random_hr_to_date": cum_rand_hr,
                "is_26_40_le_random": le_random,
                "missed_if_35": missed_35_run,
                "missed_if_38": missed_38_run,
                "total_sample": sum(v["sample"] for v in bands.values()),
                "total_hits": sum(v["hits"] for v in bands.values()),
            }
        )

    for bk, bv in cum_bands.items():
        bv["hit_rate"] = round(bv["hits"] / bv["sample"], 4) if bv["sample"] > 0 else None

    consec_le_rand = 0
    for dr in reversed(daily_runs):
        if dr["is_26_40_le_random"]:
            consec_le_rand += 1
        else:
            break

    hr15 = cum_bands["1-5"]["hit_rate"]
    hr615 = cum_bands["6-15"]["hit_rate"]
    hr1625 = cum_bands["16-25"]["hit_rate"]
    upper_monotonic = (
        hr15 is not None
        and hr615 is not None
        and hr1625 is not None
        and (hr15 >= hr615 >= hr1625)
    )
    n_days = len(daily_runs)
    proposal_ready = (n_days >= 10) and (consec_le_rand >= 5) and upper_monotonic

    weekly_crosschecks: List[Dict[str, Any]] = []
    for wr in weekly_runs:
        w_meta = wr["meta"]
        w_rows = wr["rows"]
        try:
            w_dt = datetime.date.fromisoformat(w_meta["run_date"])
        except Exception:
            w_dt = None
        prior_daily_bins: set = set()
        if w_dt is not None:
            for d_str, bset in daily_audited_bins_by_date.items():
                try:
                    dd = datetime.date.fromisoformat(d_str)
                    if 0 <= (w_dt - dd).days <= 6:
                        prior_daily_bins |= bset
                except Exception:
                    pass
        w_sample = len(w_rows)
        w_hits = sum(1 for r in w_rows if int(r["is_discrepancy"] or 0) == 1)
        omitted_rows = [r for r in w_rows if str(r["bin"]).strip() not in prior_daily_bins]
        omitted_sample = len(omitted_rows)
        omitted_hits = sum(1 for r in omitted_rows if int(r["is_discrepancy"] or 0) == 1)
        weekly_crosschecks.append(
            {
                "run_id": w_meta["run_id"],
                "run_date": w_meta["run_date"],
                "weekly_sample": w_sample,
                "weekly_hits": w_hits,
                "weekly_hit_rate": round(w_hits / w_sample, 4) if w_sample > 0 else None,
                "non_daily_sample": omitted_sample,
                "non_daily_hits": omitted_hits,
                "non_daily_hit_rate": round(omitted_hits / omitted_sample, 4) if omitted_sample > 0 else None,
            }
        )

    target_daily = None
    if run_id:
        target_daily = next((d for d in daily_runs if d["run_id"] == run_id), None)
    if target_daily is None and daily_runs:
        target_daily = daily_runs[-1]

    return {
        "target_run": target_daily,
        "daily_runs": daily_runs,
        "cumulative_bands": cum_bands,
        "n_daily_runs": n_days,
        "consecutive_26_40_le_random": consec_le_rand,
        "upper_monotonic": upper_monotonic,
        "proposal_ready": proposal_ready,
        "missed_if_35_total": missed_if_35_total,
        "missed_if_35_per_day": round(missed_if_35_total / n_days, 2) if n_days > 0 else 0.0,
        "missed_if_38_total": missed_if_38_total,
        "missed_if_38_per_day": round(missed_if_38_total / n_days, 2) if n_days > 0 else 0.0,
        "weekly_crosschecks": weekly_crosschecks,
    }


def format_slot_reduction_tracker_report(tracker: Dict[str, Any]) -> str:
    """Formats #188 ⑥ rank-band breakdown, cumulative trend, and Weekly cross-check in #185 format."""
    def _fp(val: Optional[float]) -> str:
        return f"{val * 100:.1f}%" if val is not None else "N/A"

    lines: List[str] = ["#### 📉 [188 ⑥] Daily 빈 수 축소 검토용 구간별 적중률 · 누적 추이 모니터 (참고 지표 / 현행 43빈 유지)"]
    tgt = tracker.get("target_run")
    if tgt:
        b = tgt["bands"]
        lines.append("```text")
        lines.append(f"=== [RANK BUCKET BREAKDOWN — {tgt['run_id']} ({tgt['run_date']})] ===")
        labels = [
            ("1-5", "Rank 1~5 (TOP 상위)     "),
            ("6-15", "Rank 6~15 (TOP 하위)    "),
            ("16-25", "Rank 16~25 (EXT 상위)   "),
            ("26-40", "Rank 26~40 (EXT 하위)   "),
            ("RANDOM", "RANDOM 검증             "),
        ]
        for k, title in labels:
            st = b[k]
            lines.append(
                f"  {title} | sample={st['sample']:2d} | hits={st['hits']:2d} "
                f"(BOX={st['box_hits']:2d}, PCS={st['pcs_hits']:1d}) | hit_rate={_fp(st['hit_rate'])}"
            )
        lines.append("```")

    lines.append("")
    lines.append("| 일자 (`Run`) | `1-5` | `6-15` | `16-25` | `26-40` | `RANDOM` | `26-40 ≤ 누적RND` | `43→35` 축소 시 미포착 불일치(`#33~40`) |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
    for dr in tracker.get("daily_runs", []):
        db = dr["bands"]
        c_str = (
            f"{db['1-5']['hits']}/{db['1-5']['sample']} ({_fp(db['1-5']['hit_rate'])})",
            f"{db['6-15']['hits']}/{db['6-15']['sample']} ({_fp(db['6-15']['hit_rate'])})",
            f"{db['16-25']['hits']}/{db['16-25']['sample']} ({_fp(db['16-25']['hit_rate'])})",
            f"{db['26-40']['hits']}/{db['26-40']['sample']} ({_fp(db['26-40']['hit_rate'])})",
            f"{db['RANDOM']['hits']}/{db['RANDOM']['sample']} ({_fp(db['RANDOM']['hit_rate'])})",
        )
        le_lbl = "YES" if dr["is_26_40_le_random"] else "NO"
        lines.append(
            f"| `{dr['run_date']}` (`{dr['run_id']}`) | {c_str[0]} | {c_str[1]} | {c_str[2]} | {c_str[3]} | {c_str[4]} | {le_lbl} (기준 {_fp(dr['cum_random_hr_to_date'])}) | {dr['missed_if_35']}건 |"
        )

    cb = tracker["cumulative_bands"]
    n_days = tracker["n_daily_runs"]
    lines.append(
        f"| **누적 합계 ({n_days}일)** | **{cb['1-5']['hits']}/{cb['1-5']['sample']} ({_fp(cb['1-5']['hit_rate'])})** | "
        f"**{cb['6-15']['hits']}/{cb['6-15']['sample']} ({_fp(cb['6-15']['hit_rate'])})** | "
        f"**{cb['16-25']['hits']}/{cb['16-25']['sample']} ({_fp(cb['16-25']['hit_rate'])})** | "
        f"**{cb['26-40']['hits']}/{cb['26-40']['sample']} ({_fp(cb['26-40']['hit_rate'])})** | "
        f"**{cb['RANDOM']['hits']}/{cb['RANDOM']['sample']} ({_fp(cb['RANDOM']['hit_rate'])})** | "
        f"연속 {tracker['consecutive_26_40_le_random']}일 | 누적 {tracker['missed_if_35_total']}건 (일평균 {tracker['missed_if_35_per_day']:.2f}건) |"
    )
    lines.append("")
    lines.append(
        f"- **참고 축소 제안 게이트 (Claude 제안안 · 사용자 미확정)**: "
        f"누적 일수 `{n_days}/10일`, `26-40 ≤ RANDOM` 연속 `{tracker['consecutive_26_40_le_random']}/5일`, "
        f"상위 단조(`1-5 ≥ 6-15 ≥ 16-25`) `{'충족' if tracker['upper_monotonic'] else '미충족'}` "
        f"➔ **`{'제안 검토 가능 (handoff 상정 대상)' if tracker['proposal_ready'] else '데이터 축적 중 (현행 43빈 유지)'}`**"
    )

    wcs = tracker.get("weekly_crosschecks", [])
    if not wcs:
        lines.append("- **Weekly 대조 (`Daily 미포함 빈 불일치 여부`)**: 적재된 Weekly 실사 이력 `0건` (토요일 Weekly 실사 적재 시 자동 대조)")
    else:
        for wc in wcs:
            lines.append(
                f"- **Weekly 대조 (`{wc['run_id']}` / `{wc['run_date']}`)**: "
                f"전체 `{wc['weekly_hits']}/{wc['weekly_sample']} ({_fp(wc['weekly_hit_rate'])})` | "
                f"직전 6일 Daily 미포함 빈 `{wc['non_daily_hits']}/{wc['non_daily_sample']} ({_fp(wc['non_daily_hit_rate'])})`"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit Hit Rate Calculator")
    parser.add_argument("--run-id", type=str, default=None, help="Run ID to compute hit rate for")
    parser.add_argument("--backfill", action="store_true", help="Backfill hit rates for all historical runs")
    parser.add_argument("--report", type=str, default=None, help="Generate summary report for run_id")
    args = parser.parse_args()

    if args.backfill:
        res = backfill_all_hit_rates()
        for r in res:
            print(f"Run {r['run_id']}: MODEL={r['hit_rate_model']:.1%}, TOP={r['hit_rate_top']:.1%}, EXT={r['hit_rate_extended']:.1%}")
    elif args.report:
        print(generate_hit_rate_summary_text(args.report))
    elif args.run_id:
        r = compute_hit_rate_for_run(args.run_id)
        print(generate_hit_rate_summary_text(args.run_id))
    else:
        print("Usage: python hit_rate_calculator.py --run-id <run_id> OR --backfill OR --report <run_id>")
