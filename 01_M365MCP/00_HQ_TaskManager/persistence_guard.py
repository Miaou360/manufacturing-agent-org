"""
Persistence Guard Engine (00_HQ_TaskManager / persistence_guard.py)
===================================================================
Plant: P001 Headlamp
Standard: 2-Snapshot Sustained Variance Validation (User Decisions D37 / 028-2)

Operational Rationale:
- In ACME Manufacturing operations, all physical barcoded movements are scanned in WMS first,
  after which WMS asynchronously sends interface documents to SAP (D37).
- Interface latency (empirically measured at 5m 28s ~ 10m) causes transient variances
  where SAP temporarily appears to have surplus (before a 311 deduction arrives) or
  deficit (before a 101/311 inbound arrives).
- If an automated bot transfers stock against a transient discrepancy, the subsequent
  arrival of the SAP interface doubles the movement, severely degrading MM Stock Accuracy
  (as observed in 99000021-V1 and SL00 inbound receipts).

Core Rules (028-2):
1. Warehouse Surplus Transfers (Source SLoc in SL00~SL04, SL49):
   - Available surplus is bounded by: min(Δ_now, Δ_prev)
   - If Δ_prev <= 0 or previous snapshot is unavailable/stale (> max_age_minutes),
     persistent surplus is 0.0 and transfer out of warehouse is strictly rejected.
2. Warehouse Deficit Inbounds (Dest SLoc in SL00~SL04, SL49):
   - Valid deficit is bounded by: min(deficit_now, deficit_prev)
   - If deficit_prev <= 0 (e.g. fresh WMS scan with pending SAP GR), transfer is rejected.
3. External Production Lines (SP31, SP11):
   - SP31 and SP11 are not in-scope warehouse balance locations. Available SAP stock
     may be dispatched directly without persistence requirement.
"""

import os
import glob
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple, Any, NamedTuple
import pandas as pd

logger = logging.getLogger("PersistenceGuard")

# Warehouse storage locations subject to MM stock accuracy scoring & persistence rules
WAREHOUSE_SLOCS = {"SL00", "SL01", "SL02", "SL03", "SL04", "SL49"}
EXTERNAL_SLOCS = {"SP31", "SP11"}

DEFAULT_MAX_AGE_MINUTES = 60.0
# Minimum interval between consecutive snapshots: 15 minutes (900s).
# Operational Rationale (Thread 037 / User Decision D37):
# Empirically measured SAP interface latency is >= 5m 28s. If snapshots are taken within
# 2~5 minutes of each other (e.g. 16:00 and 16:02), both snapshots observe the exact same
# transient un-interfaced backlog (e.g. false surplus 144 EA on 99000021-V1). Setting the
# minimum interval to 15 minutes (with 60m upper bound) ensures that transient interface
# delays have cleared, verifying genuine physical inventory persistence.
DEFAULT_MIN_INTERVAL_SECONDS = 900.0  # 15 minutes


class PersistenceResult(NamedTuple):
    is_persistent: bool
    persistent_qty: float
    source_persistent_surplus: float
    dest_persistent_deficit: float
    reason: str
    prev_snapshot_time: Optional[str]


def check_transfer_persistence(
    material: str,
    source_sloc: str,
    dest_sloc: str,
    candidate_qty: float,
    current_wms_by_sloc: Dict[str, float],
    current_sap_by_sloc: Dict[str, float],
    prev_wms_by_sloc: Optional[Dict[str, float]] = None,
    prev_sap_by_sloc: Optional[Dict[str, float]] = None,
    prev_snapshot_time: Optional[str] = None,
    current_snapshot_time: Optional[str] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
    is_prev_snapshot_valid: bool = True
) -> PersistenceResult:
    """
    Evaluates whether a candidate transfer is backed by persistent, sustained inventory
    differences across two consecutive snapshots (min(Δ_now, Δ_prev) rule).
    """
    source_sloc = str(source_sloc).strip().upper()
    dest_sloc = str(dest_sloc).strip().upper()
    cand_q = float(candidate_qty)

    if cand_q <= 0:
        return PersistenceResult(
            is_persistent=False,
            persistent_qty=0.0,
            source_persistent_surplus=0.0,
            dest_persistent_deficit=0.0,
            reason="Candidate quantity is zero or negative",
            prev_snapshot_time=prev_snapshot_time
        )

    # 1. Evaluate Source Available Stock
    if source_sloc in WAREHOUSE_SLOCS:
        cur_sap_s = current_sap_by_sloc.get(source_sloc, 0.0)
        cur_wms_s = current_wms_by_sloc.get(source_sloc, 0.0)
        cur_surplus_s = cur_sap_s - cur_wms_s

        if cur_surplus_s <= 0:
            return PersistenceResult(
                is_persistent=False,
                persistent_qty=0.0,
                source_persistent_surplus=0.0,
                dest_persistent_deficit=0.0,
                reason=f"Source {source_sloc} is not in surplus in current snapshot (SAP={cur_sap_s:.1f}, WMS={cur_wms_s:.1f})",
                prev_snapshot_time=prev_snapshot_time
            )

        # Previous snapshot verification for warehouse surplus
        if not is_prev_snapshot_valid or prev_wms_by_sloc is None or prev_sap_by_sloc is None:
            return PersistenceResult(
                is_persistent=False,
                persistent_qty=0.0,
                source_persistent_surplus=0.0,
                dest_persistent_deficit=0.0,
                reason=f"Source {source_sloc} warehouse surplus rejected: No valid previous snapshot available within {max_age_minutes:.0f}m",
                prev_snapshot_time=prev_snapshot_time
            )

        prev_sap_s = prev_sap_by_sloc.get(source_sloc, 0.0)
        prev_wms_s = prev_wms_by_sloc.get(source_sloc, 0.0)
        prev_surplus_s = prev_sap_s - prev_wms_s

        if prev_surplus_s <= 0:
            return PersistenceResult(
                is_persistent=False,
                persistent_qty=0.0,
                source_persistent_surplus=0.0,
                dest_persistent_deficit=0.0,
                reason=f"Source {source_sloc} warehouse surplus was NOT present in previous snapshot ({prev_snapshot_time or 'prior'}): cur_surplus={cur_surplus_s:.1f}, prev_surplus={prev_surplus_s:.1f} (99000021-V1 guardrail)",
                prev_snapshot_time=prev_snapshot_time
            )

        source_persistent_surplus = min(cur_surplus_s, prev_surplus_s, cur_sap_s)
    else:
        # SP31, SP11, etc: external buffer lines, not subject to warehouse accuracy scoring
        source_persistent_surplus = current_sap_by_sloc.get(source_sloc, 0.0)

    # 2. Evaluate Destination Deficit (if destination is an audited warehouse)
    if dest_sloc in WAREHOUSE_SLOCS:
        cur_sap_d = current_sap_by_sloc.get(dest_sloc, 0.0)
        cur_wms_d = current_wms_by_sloc.get(dest_sloc, 0.0)
        cur_deficit_d = cur_wms_d - cur_sap_d

        if cur_deficit_d > 0:
            if not is_prev_snapshot_valid or prev_wms_by_sloc is None or prev_sap_by_sloc is None:
                return PersistenceResult(
                    is_persistent=False,
                    persistent_qty=0.0,
                    source_persistent_surplus=source_persistent_surplus,
                    dest_persistent_deficit=0.0,
                    reason=f"Destination {dest_sloc} warehouse deficit rejected: No valid previous snapshot available within {max_age_minutes:.0f}m",
                    prev_snapshot_time=prev_snapshot_time
                )

            prev_sap_d = prev_sap_by_sloc.get(dest_sloc, 0.0)
            prev_wms_d = prev_wms_by_sloc.get(dest_sloc, 0.0)
            prev_deficit_d = prev_wms_d - prev_sap_d

            if prev_deficit_d <= 0:
                return PersistenceResult(
                    is_persistent=False,
                    persistent_qty=0.0,
                    source_persistent_surplus=source_persistent_surplus,
                    dest_persistent_deficit=0.0,
                    reason=f"Destination {dest_sloc} deficit was NOT present in previous snapshot ({prev_snapshot_time or 'prior'}): cur_deficit={cur_deficit_d:.1f}, prev_deficit={prev_deficit_d:.1f} (pending GR/interface guardrail)",
                    prev_snapshot_time=prev_snapshot_time
                )

            dest_persistent_deficit = min(cur_deficit_d, prev_deficit_d)
        else:
            dest_persistent_deficit = cand_q
    else:
        dest_persistent_deficit = cand_q

    # 3. Compute allowable persistent transfer quantity
    allowed_q = min(cand_q, source_persistent_surplus, dest_persistent_deficit)

    if allowed_q <= 0:
        return PersistenceResult(
            is_persistent=False,
            persistent_qty=0.0,
            source_persistent_surplus=source_persistent_surplus,
            dest_persistent_deficit=dest_persistent_deficit,
            reason=f"Allowable persistent quantity is 0 (source_surplus={source_persistent_surplus:.1f}, dest_deficit={dest_persistent_deficit:.1f})",
            prev_snapshot_time=prev_snapshot_time
        )

    return PersistenceResult(
        is_persistent=True,
        persistent_qty=allowed_q,
        source_persistent_surplus=source_persistent_surplus,
        dest_persistent_deficit=dest_persistent_deficit,
        reason="Verified sustained variance across 2 consecutive snapshots",
        prev_snapshot_time=prev_snapshot_time
    )


def load_previous_snapshot_pair(
    current_wms_file: Path,
    current_mb52_file: Path,
    wms_dir: Optional[Path] = None,
    mb52_dir: Optional[Path] = None,
    max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES,
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[str]]:
    """
    Identifies and loads the previous simultaneous WMS & MB52 snapshot files collected
    prior to the current run.
    Returns: (prev_wms_df, prev_mb52_df, prev_snapshot_time_str)
    """
    wms_folder = wms_dir or current_wms_file.parent
    mb52_folder = mb52_dir or current_mb52_file.parent

    cur_wms_mtime = os.path.getmtime(current_wms_file)
    cur_mb52_mtime = os.path.getmtime(current_mb52_file)
    cur_time = min(cur_wms_mtime, cur_mb52_mtime)

    # 1. Find previous WMS file
    wms_candidates = []
    for f in glob.glob(str(wms_folder / "*.xlsx")):
        p = Path(f)
        if p.resolve() == current_wms_file.resolve():
            continue
        mtime = os.path.getmtime(p)
        delta_sec = cur_time - mtime
        if min_interval_seconds <= delta_sec <= (max_age_minutes * 60.0):
            wms_candidates.append((mtime, p))

    if not wms_candidates:
        logger.info(f"No suitable previous WMS snapshot found within {max_age_minutes:.0f}m.")
        return None, None, None

    wms_candidates.sort(key=lambda x: x[0], reverse=True)

    selected_wms_path = None
    selected_wms_time = None
    selected_mb52_path = None
    selected_mb52_time = None

    # Search in reverse chronological order for the newest WMS candidate that has a simultaneous MB52 counterpart
    for prev_wms_time, prev_wms_path in wms_candidates:
        mb52_candidates = []
        for f in glob.glob(str(mb52_folder / "*.XLS*")):
            p = Path(f)
            if p.resolve() == current_mb52_file.resolve():
                continue
            mtime = os.path.getmtime(p)
            delta_with_prev_wms = abs(mtime - prev_wms_time)
            if delta_with_prev_wms <= 300.0:  # Within 5 mins of previous WMS
                mb52_candidates.append((delta_with_prev_wms, p, mtime))

        if mb52_candidates:
            mb52_candidates.sort(key=lambda x: x[0])
            _, prev_mb52_path, prev_mb52_time = mb52_candidates[0]
            selected_wms_path = prev_wms_path
            selected_wms_time = prev_wms_time
            selected_mb52_path = prev_mb52_path
            selected_mb52_time = prev_mb52_time
            break

    if not selected_wms_path or not selected_mb52_path:
        logger.info(f"No simultaneous WMS & MB52 snapshot pair found within {max_age_minutes:.0f}m.")
        return None, None, None

    time_str = datetime.fromtimestamp(selected_wms_time).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Found valid previous snapshot pair: WMS={selected_wms_path.name}, MB52={selected_mb52_path.name} (Time: {time_str})")

    # 3. Load DataFrames
    try:
        from wms_mapping import map_wms_gi_loc_to_sap
        # Load WMS
        df_wms = pd.read_excel(selected_wms_path)
        mat_col = next((c for c in df_wms.columns if 'Mat.No' in c or 'Material' in c), None)
        loc_col = next((c for c in df_wms.columns if 'GI Loc' in c or 'SLoc' in c), None)
        qty_col = next((c for c in df_wms.columns if 'Qty' in c), None)
        if mat_col and loc_col and qty_col:
            df_wms['Material'] = df_wms[mat_col].astype(str).str.strip()
            df_wms['SLoc'] = df_wms[loc_col].apply(map_wms_gi_loc_to_sap)
            df_wms['WMS_Stock'] = pd.to_numeric(df_wms[qty_col].astype(str).str.replace(',', '').str.strip(), errors='coerce').fillna(0)
            df_wms = df_wms.dropna(subset=['SLoc'])
            wms_clean = df_wms.groupby(['Material', 'SLoc'])['WMS_Stock'].sum().reset_index()
        else:
            wms_clean = None

        # Load MB52
        try:
            df_m = pd.read_html(selected_mb52_path)[0]
        except Exception:
            df_m = pd.read_csv(selected_mb52_path, sep='\t', encoding='utf-16')
        cols = [str(c).strip() for c in df_m.columns]
        df_m.columns = cols
        m_mat = next((c for c in cols if 'Material' in c and 'Desc' not in c), None)
        m_loc = next((c for c in cols if 'SLoc' in c or 'Storage' in c), None)
        m_unres = next((c for c in cols if 'Unrestricted' in c), None)
        if m_mat and m_loc and m_unres:
            df_m['Material'] = df_m[m_mat].astype(str).str.strip()
            df_m['SLoc'] = df_m[m_loc].astype(str).str.strip().str.upper()
            df_m['SAP_Stock'] = pd.to_numeric(df_m[m_unres].astype(str).str.replace(',', '').str.strip(), errors='coerce').fillna(0)
            mb52_clean = df_m.groupby(['Material', 'SLoc'])['SAP_Stock'].sum().reset_index()
        else:
            mb52_clean = None

        return wms_clean, mb52_clean, time_str
    except Exception as e:
        logger.error(f"Error loading previous snapshot pair: {e}")
        return None, None, None
