"""
Cycle Count Audit Result Collector (현장 실사 실적 수집 및 원장 적재기)
====================================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / audit_result_collector.py

Channels:
1. Excel Sheet Ingestion (종이 실사 후 엑셀 입력 회신 시트 자동 인제스트)
2. Tablet / CLI Direct Record (태블릿 또는 콘솔 현장 즉시 입력)
3. In-flight Movement Reconciliation (실사 도중 발생한 입출고 실시간 보정)
4. Distinguishes Physical Audit Corrections from Administrative WMS Edits.
"""

import sys
import os

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import json
import sqlite3
import datetime
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Union
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CURRENT_DIR))

from cause_codes import CAUSE_CODES, get_cause_code_info, list_valid_cause_codes
from audit_constants import DATA_ORIGIN_PHYSICAL, DATA_ORIGIN_SYNTHETIC, DATA_ORIGIN_TEST
from db_manager import (
    get_connection,
    save_audit_result,
    reconcile_in_flight_movement,
    get_audit_results_by_run,
    get_latest_material_net_prices,
    DB_PATH
)

DEV_DB_PATH = CURRENT_DIR / "db" / "dev_stock_accuracy.db"

logger = logging.getLogger("AuditResultCollector")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [AuditResultCollector] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

FEEDBACK_DIR = CURRENT_DIR / "audit_feedback"
FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# 1. Single Result Validation & Ingestion
# ==============================================================================

def record_single_audit_result(
    assignment_id: str,
    counted_qty: float,
    cause_code: str,
    minutes_spent: float,
    counter_id: str,
    counted_at: Optional[str] = None,
    is_recount: bool = False,
    is_confirmed: bool = True,
    audit_origin: str = "PHYSICAL_CYCLE_COUNT",
    data_origin: str = DATA_ORIGIN_PHYSICAL,
    notes: str = "",
    db_path: Optional[Path] = None,
    net_price_override: Optional[float] = None
) -> Dict[str, Any]:
    """
    Validates, reconciles in-flight movement, and persists a single physical count result.
    
    Guarantees:
    - assignment_id exists in AUDIT_ASSIGNMENT.
    - cause_code is validated against standard cause code registry.
    - in-flight movements between assignment dispatch and counted_at are auto-reconciled.
    - WMS admin adjustments are cleanly distinguished from physical counts.
    """
    target_db = Path(db_path or DB_PATH)
    conn = get_connection(target_db)
    cur = conn.cursor()

    # 1. Fetch Assignment Master Info
    cur.execute("""
        SELECT a.assignment_id, a.run_id, a.bin, a.label_qty, a.materials_json,
               a.created_at AS dispatch_time, r.run_date
        FROM AUDIT_ASSIGNMENT a
        JOIN AUDIT_RUN r ON a.run_id = r.run_id
        WHERE a.assignment_id = ?;
    """, [assignment_id])
    assign_row = cur.fetchone()

    if not assign_row:
        conn.close()
        raise ValueError(f"Assignment ID '{assignment_id}' does not exist in AUDIT_ASSIGNMENT.")

    run_id = assign_row["run_id"]
    bin_val = assign_row["bin"]
    book_qty = float(assign_row["label_qty"])
    dispatch_time = assign_row["dispatch_time"]
    mat_list = []
    if assign_row["materials_json"]:
        try:
            mat_list = json.loads(assign_row["materials_json"])
        except Exception:
            pass
    primary_mat = mat_list[0] if mat_list else None

    # Counted timestamp defaults to now
    count_time = counted_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 2. Validate Cause Code
    resolved_cause = None
    if cause_code:
        code_info = get_cause_code_info(cause_code)
        resolved_cause = code_info.get("code")

    # 3. In-Flight Movement Reconciliation
    reconciled = reconcile_in_flight_movement(
        conn=conn,
        bin_val=bin_val,
        mat_no=primary_mat,
        snapshot_time=dispatch_time,
        counted_at=count_time,
        book_qty_at_count=book_qty,
        counted_qty=counted_qty
    )

    true_diff_qty = reconciled["true_diff_qty"]
    effective_book_qty = reconciled["effective_book_qty"]

    # If in-flight movement completely accounted for the difference, override cause to CC07
    if reconciled["in_flight_offset_matched"]:
        resolved_cause = "CC07_IN_FLIGHT_MOVEMENT"
        true_diff_qty = 0.0

    # 4. Resolve Unit Price for Financial Difference
    unit_price = net_price_override
    if unit_price is None:
        price_map = get_latest_material_net_prices() or {}
        unit_price = price_map.get(primary_mat, 10.0) if primary_mat else 10.0  # safe default

    diff_amount = round(true_diff_qty * unit_price, 2)
    if abs(true_diff_qty) <= 0.001:
        diff_amount = 0.0

    is_discrepancy = 1 if abs(true_diff_qty) > 0.001 else 0

    # 5. Persist into AUDIT_RESULT
    result_payload = {
        "assignment_id": assignment_id,
        "run_id": run_id,
        "bin": bin_val,
        "mat_no": primary_mat,
        "counted_at": count_time,
        "book_qty_at_count": book_qty,
        "counted_qty": counted_qty,
        "diff_qty": true_diff_qty,
        "diff_amount": diff_amount,
        "cause_code": resolved_cause,
        "minutes_spent": minutes_spent,
        "counter_id": counter_id,
        "is_recount": 1 if is_recount else 0,
        "is_confirmed": 1 if is_confirmed else 0,
        "audit_origin": audit_origin,
        "data_origin": data_origin,
        "wms_adjustment_status": "NOT_ADJUSTED",
        "in_flight_gr_qty": reconciled["in_flight_gr_qty"],
        "in_flight_gi_qty": reconciled["in_flight_gi_qty"],
        "in_flight_adjusted": 1 if reconciled["in_flight_adjusted"] else 0,
        "effective_book_qty": effective_book_qty,
        "is_discrepancy": is_discrepancy,
        "notes": notes or (reconciled.get("notes") or "")
    }

    result_id = save_audit_result(result_payload, db_path=target_db)
    conn.close()

    result_payload["result_id"] = result_id
    logger.info(
        f"✅ [AUDIT RESULT SAVED] Result ID: {result_id}, Assignment: {assignment_id}, "
        f"Bin: {bin_val}, Counted: {counted_qty} (Book: {book_qty}, Eff: {effective_book_qty}), "
        f"Diff: {true_diff_qty} (${diff_amount:,.2f}), Cause: {resolved_cause}, "
        f"Recount: {is_recount}, Counter: {counter_id}"
    )
    return result_payload


# ==============================================================================
# 2. Excel Return Sheet Batch Ingestion
# ==============================================================================

def ingest_audit_results_from_excel(
    excel_path: Union[str, Path],
    counter_id_fallback: str = "FIELD_OPERATOR",
    db_path: Optional[Path] = None,
    run_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Ingests worker counting results from an Excel feedback sheet.
    
    Expected Columns (case-insensitive, flexible):
    - 'BIN' or 'Bin #'
    - 'Count QTY' or 'Counted Qty'
    - 'Cause Code' or 'Cause' (Optional, defaults to CC08 if diff=0, CC01 if diff!=0)
    - 'Minutes' or 'Minutes Spent' (Optional, defaults to 2.0)
    - 'Counter ID' or 'Counter' (Optional, defaults to counter_id_fallback)
    - 'Recount' or 'Is Recount' (Optional, Y/N or 1/0)
    """
    target_path = Path(excel_path)
    if not target_path.exists():
        raise FileNotFoundError(f"Excel file not found at {target_path}")

    target_db = Path(db_path or DB_PATH)
    logger.info(f"📂 [EXCEL INGESTION] Processing return sheet: {target_path.name}...")

    # Load Excel sheet (prefer Cycle_Count_Target_Bins if multi-sheet; auto-detect header row if D112 Field Instructions precede table)
    xl = pd.ExcelFile(target_path)
    sheet_name = "Cycle_Count_Target_Bins" if "Cycle_Count_Target_Bins" in xl.sheet_names else xl.sheet_names[0]
    df = pd.read_excel(target_path, sheet_name=sheet_name)
    if not any(str(c).strip().lower() in ("bin", "bin #", "location") for c in df.columns):
        preview_df = pd.read_excel(target_path, sheet_name=sheet_name, header=None, nrows=12)
        for hdr_idx in range(len(preview_df)):
            row_vals = {str(v).strip().lower() for v in preview_df.iloc[hdr_idx].tolist() if pd.notna(v)}
            if row_vals & {"bin", "bin #", "location"}:
                df = pd.read_excel(target_path, sheet_name=sheet_name, header=hdr_idx)
                break

    # Column normalization mapping
    col_map = {c.strip().lower(): c for c in df.columns}
    bin_col = next((col_map[c] for c in ["bin", "bin #", "location"] if c in col_map), None)
    match_col = next((col_map[c] for c in [
        "physical match? (ok/ng)", "physical match", "match? (ok/ng)", "match?",
        "match", "ok/ng", "status", "result"
    ] if c in col_map), None)
    count_col = next((col_map[c] for c in ["count qty", "counted qty", "physical count"] if c in col_map), None)
    diff_col = next((col_map[c] for c in ["diff qty", "diff", "difference"] if c in col_map), None)
    cause_col = next((col_map[c] for c in ["discrepancy cause", "cause code", "cause", "root cause", "field notes / reason"] if c in col_map), None)
    min_col = next((col_map[c] for c in ["minutes spent", "minutes", "duration"] if c in col_map), None)
    counter_col = next((col_map[c] for c in ["auditor", "counter id", "counter", "operator", "initials"] if c in col_map), None)
    time_col = next((col_map[c] for c in ["count time", "audit time", "counted at", "time"] if c in col_map), None)
    recount_col = next((col_map[c] for c in ["is recount", "recount"] if c in col_map), None)
    notes_col = next((col_map[c] for c in ["field notes / reason", "field notes", "notes", "remark", "audit check"] if c in col_map), None)

    if not bin_col or (not count_col and not match_col):
        raise ValueError(
            f"Required columns (Bin and Count QTY or Physical Match? (OK/NG)) not found in sheet '{sheet_name}'. "
            f"Available columns: {df.columns.tolist()}"
        )

    # Find assignments in target_db to map bin -> assignment_id
    conn = get_connection(target_db)
    cur = conn.cursor()
    if run_id:
        cur.execute("""
            SELECT a.assignment_id, a.run_id, a.bin, a.label_qty, a.materials_json, a.created_at
            FROM AUDIT_ASSIGNMENT a
            WHERE a.run_id = ?;
        """, [run_id])
    else:
        cur.execute("""
            SELECT a.assignment_id, a.run_id, a.bin, a.label_qty, a.materials_json, a.created_at
            FROM AUDIT_ASSIGNMENT a
            WHERE a.run_id = (SELECT MAX(run_id) FROM AUDIT_ASSIGNMENT);
        """)
    assign_map = {r["bin"]: dict(r) for r in cur.fetchall()}
    conn.close()

    ingested_count = 0
    errors = []

    for idx, row in df.iterrows():
        b_val = str(row[bin_col]).strip()
        count_val = row[count_col] if count_col else None
        match_val = str(row[match_col]).strip().upper() if match_col and pd.notna(row[match_col]) else ""

        # Skip rows where both Count QTY and Match status are blank
        has_count = pd.notna(count_val) and str(count_val).strip() != ""
        has_match = bool(match_val)
        if not has_count and not has_match:
            continue

        assign_info = assign_map.get(b_val)
        if not assign_info:
            errors.append(f"Row {idx}: Bin '{b_val}' has no matching AUDIT_ASSIGNMENT in active run.")
            continue

        assignment_id = assign_info["assignment_id"]
        book_qty = float(assign_info["label_qty"])

        # Determine counted_qty and match state
        is_ok = match_val in ["OK", "Y", "YES", "TRUE", "1", "O", "MATCH", "V", "CHECK"]
        is_ng = match_val in ["NG", "N", "NO", "FALSE", "0", "DIFF", "MISMATCH"]

        if is_ok and not has_count:
            # Operator confirmed match without re-entering quantity
            counted_qty = book_qty
        elif has_count:
            try:
                counted_qty = float(count_val)
            except ValueError:
                c_str = str(count_val).strip().upper()
                if c_str in ["OK", "Y", "YES", "TRUE", "MATCH", "V", "CHECK", "O"]:
                    counted_qty = book_qty
                    is_ok = True
                else:
                    errors.append(f"Row {idx}: Invalid non-numeric Count QTY '{count_val}' for Bin {b_val}")
                    continue
        elif is_ng and not has_count:
            # Marked NG without count: check if diff is provided, else default to 0.0
            if diff_col and pd.notna(row[diff_col]):
                try:
                    diff_val = float(row[diff_col])
                    counted_qty = book_qty + diff_val
                except ValueError:
                    counted_qty = 0.0
            else:
                counted_qty = 0.0
        else:
            continue

        # Determine Cause Code (D74 / AGENTS.md Rule 9: Never fabricate cause code. If blank, remain None/NULL)
        raw_cause = str(row[cause_col]).strip() if cause_col and pd.notna(row[cause_col]) and str(row[cause_col]).strip() else None

        # Minutes spent (D74 / AGENTS.md Rule 9: Never fabricate minutes. If blank, remain None/NULL)
        try:
            minutes = float(row[min_col]) if min_col and pd.notna(row[min_col]) and str(row[min_col]).strip() else None
        except ValueError:
            minutes = None

        # Counter ID
        counter_id = str(row[counter_col]).strip() if counter_col and pd.notna(row[counter_col]) else counter_id_fallback

        # Count Time
        counted_at = str(row[time_col]).strip() if time_col and pd.notna(row[time_col]) else None

        # Recount flag
        is_recount = False
        if recount_col and pd.notna(row[recount_col]):
            rc_str = str(row[recount_col]).strip().upper()
            is_recount = rc_str in ["1", "Y", "YES", "TRUE", "RECOUNT"]

        notes = str(row[notes_col]).strip() if notes_col and pd.notna(row[notes_col]) else ""
        if match_val and not notes:
            notes = f"Field Match: {match_val}"

        try:
            record_single_audit_result(
                assignment_id=assignment_id,
                counted_qty=counted_qty,
                cause_code=raw_cause,
                minutes_spent=minutes,
                counter_id=counter_id,
                counted_at=counted_at,
                is_recount=is_recount,
                is_confirmed=True,
                notes=notes,
                db_path=target_db
            )
            ingested_count += 1
        except Exception as e:
            errors.append(f"Row {idx} (Bin {b_val}): {e}")

    logger.info(f"✅ Excel Ingestion Complete: {ingested_count} results recorded. Errors: {len(errors)}.")

    # D34: Automatically compute and update hit rate metrics for this run
    hit_rate_metrics = None
    target_run_id = run_id
    if not target_run_id and assign_map:
        first_item = next(iter(assign_map.values()), None)
        if first_item:
            target_run_id = first_item.get("run_id")

    if target_run_id and ingested_count > 0:
        try:
            from hit_rate_calculator import compute_hit_rate_for_run
            hit_rate_metrics = compute_hit_rate_for_run(target_run_id, db_path=target_db, save_to_db=True)
            hr_m = hit_rate_metrics.get("hit_rate_model")
            hr_str = f"{hr_m:.1%}" if hr_m is not None else "N/A"
            logger.info(f"📊 [D34] Computed audit hit rate for Run {target_run_id}: MODEL={hr_str}")
            from hit_rate_calculator import compute_daily_slot_reduction_tracker, format_slot_reduction_tracker_report
            slot_tracker = compute_daily_slot_reduction_tracker(run_id=target_run_id, db_path=target_db)
            hit_rate_metrics["slot_reduction_tracker"] = slot_tracker
            logger.info("\n" + format_slot_reduction_tracker_report(slot_tracker))
        except Exception as ex:
            logger.warning(f"[D34 Warning] Failed to compute audit hit rate for {target_run_id}: {ex}")

    return {
        "status": "success" if not errors else "partial",
        "ingested_count": ingested_count,
        "hit_rate_metrics": hit_rate_metrics,
        "errors": errors
    }


# ==============================================================================
# 3. Seed Realistic Ground Truth Data for Run (Dev & Testing)
# ==============================================================================

def seed_realistic_audit_results_for_run(
    run_id: str,
    db_path: Optional[Path] = None
) -> int:
    """
    Seeds realistic ground truth cycle count results for all assignments in a run.
    
    Generates:
    - True Ghost Stock discrepancies (CC01) for high-scoring critical ghost bins.
    - True Wrong Bin misplacements (CC02) for high-risk bins.
    - In-flight movement offsets (CC07) for moving parts (reconciled to zero).
    - Perfect matches (CC08) for clean/normal bins.
    - Includes recounts (is_recount=1) for 10% of discrepancy bins to BINSYNTH9952:N relations.
    """
    target_db = Path(db_path or DEV_DB_PATH)
    if target_db.resolve() == DB_PATH.resolve():
        raise ValueError(
            "CRITICAL SECURITY GUARD: Seeding synthetic audit results into production DB "
            f"({DB_PATH.name}) is strictly prohibited! Use dev_stock_accuracy.db instead."
        )
    conn = get_connection(target_db)
    cur = conn.cursor()

    cur.execute("""
        SELECT a.assignment_id, a.run_id, a.bin, a.rank, a.list_type,
               a.bin_risk_score, a.label_qty, a.primary_reason, a.created_at,
               r.run_date
        FROM AUDIT_ASSIGNMENT a
        JOIN AUDIT_RUN r ON a.run_id = r.run_id
        WHERE a.run_id = ?
        ORDER BY a.rank ASC;
    """, [run_id])
    assignments = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not assignments:
        logger.warning(f"No assignments found for run_id '{run_id}'.")
        return 0

    price_map = get_latest_material_net_prices()
    seeded_count = 0

    for idx, a in enumerate(assignments, 1):
        assign_id = a["assignment_id"]
        bin_val = a["bin"]
        book_qty = float(a["label_qty"])
        score = float(a["bin_risk_score"])
        run_date = a["run_date"]
        count_time = f"{run_date} 05:{15 + idx % 40:02d}:00"

        # Discrepancy Generation Model based on domain risk score:
        # High score bins (score >= 70) have 75% discrepancy probability
        # Medium score bins (40 <= score < 70) have 40% discrepancy probability
        # Low score / Random validation bins have 10% discrepancy probability
        if score >= 70:
            if idx % 4 != 0:  # 75% discrepancy
                counted_qty = max(0.0, book_qty - 100.0 if book_qty >= 100 else 0.0)
                cause = "CC01_GHOST_MISSING_GI" if "Ghost" in str(a["primary_reason"]) else "CC02_WRONG_BIN_PUTAWAY"
                minutes = 3.5
            else:
                counted_qty = book_qty
                cause = "CC08_MATCH_VERIFIED"
                minutes = 1.5
        elif score >= 40:
            if idx % 3 == 0:  # 33% discrepancy
                counted_qty = max(0.0, book_qty - 50.0)
                cause = "CC04_PARTIAL_PICK_UNSCANNED" if "Floor" in str(a["primary_reason"]) else "CC03_FIFO_BURIAL"
                minutes = 2.5
            else:
                counted_qty = book_qty
                cause = "CC08_MATCH_VERIFIED"
                minutes = 1.5
        else:
            # Low risk / Random validation
            if idx == 42:  # 1 in random sample has discrepancy
                counted_qty = max(0.0, book_qty - 20.0)
                cause = "CC06_UNIT_PACK_OFFSET"
                minutes = 2.0
            else:
                counted_qty = book_qty
                cause = "CC08_MATCH_VERIFIED"
                minutes = 1.0

        # Initial Count (is_recount=0)
        # Bins matching book qty or low/medium-risk findings are confirmed directly.
        # Severe high-risk discrepancies (score >= 80) remain tentative until supervisor recount confirms them.
        initial_confirmed = True if score < 80 else (counted_qty == book_qty)
        res1 = record_single_audit_result(
            assignment_id=assign_id,
            counted_qty=counted_qty,
            cause_code=cause,
            minutes_spent=minutes,
            counter_id="USER_SYNTH_9930",
            counted_at=count_time,
            is_recount=False,
            is_confirmed=initial_confirmed,
            data_origin=DATA_ORIGIN_SYNTHETIC,
            notes="Initial cycle count (SYNTHETIC_SEED)",
            db_path=target_db
        )
        seeded_count += 1

        # For high discrepancy bins, add a Recount (is_recount=1, 1:N)
        if counted_qty != book_qty and score >= 80:
            recount_time = f"{run_date} 06:10:00"
            record_single_audit_result(
                assignment_id=assign_id,
                counted_qty=counted_qty,  # Recount confirmed the shortage
                cause_code=cause,
                minutes_spent=minutes + 1.0,
                counter_id="SUPERVISOR_01",
                counted_at=recount_time,
                is_recount=True,
                is_confirmed=True,
                data_origin=DATA_ORIGIN_SYNTHETIC,
                notes="Supervisor recount confirmed shortage (SYNTHETIC_SEED). Forwarding for MB1B adjustment.",
                db_path=target_db
            )
            seeded_count += 1

    logger.info(f"✨ [GROUND TRUTH SEEDED] Seeded {seeded_count} audit results for Run {run_id}.")
    return seeded_count


# ==============================================================================
# 4. CLI Entrypoint
# ==============================================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Audit Result Collector (Physical Count Ingestion)")
    parser.add_argument("--ingest-excel", type=str, default=None, help="Path to returned count sheet Excel")
    parser.add_argument("--run-id", type=str, default=None, help="Optional run_id to map Excel counts to")
    parser.add_argument("--seed-run-id", type=str, default=None, help="Seed realistic ground truth for run_id")
    parser.add_argument("--db-path", type=str, default=str(DB_PATH), help="Target SQLite DB path")
    args = parser.parse_args()

    if args.seed_run_id:
        seed_realistic_audit_results_for_run(args.seed_run_id, db_path=Path(args.db_path))
    elif args.ingest_excel:
        ingest_audit_results_from_excel(args.ingest_excel, db_path=Path(args.db_path), run_id=args.run_id)
    else:
        print("Audit Result Collector CLI. Use --ingest-excel or --seed-run-id.")
