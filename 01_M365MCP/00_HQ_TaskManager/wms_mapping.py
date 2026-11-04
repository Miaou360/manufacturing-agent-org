"""
Universal WMS-to-SAP Storage Location Mapper & Stock Accuracy Metric Calculator
=============================================================================
00_HQ_TaskManager / wms_mapping.py

Purpose:
- Enterprise standard for converting WMS textual GI Locations to SAP 4-character SLoc codes.
- Whitelist enforcement for allowed plant storage locations and external transfer buffers.
- Official SAP ZTCODE_STOCK_REC Material-Level MM Stock Accuracy calculation engine.
Shared across 00_JOB_StockAccuracy, 01_JOB_GR_GI_Error, 02_JOB_Backlog_Clear,
04_JOB_SystemReconciliation, and 05_JOB_SL01_R_Bins.
"""

import logging
from datetime import datetime
from typing import Optional, Set, Dict, Any, Tuple

logger = logging.getLogger("WMSMapping")

# Standard Mapping Table from WMS 'GI Loc' descriptive string to SAP SLoc code
WMS_TO_SAP_SLOC_MAP = {
    "RECEIPT GOODS SL": "SL00",
    "MAIN PARTS": "SL01",
    "CKD": "SL02",
    "RAW MATERIAL": "SL03",
    "PASS THROUGH": "SL04",
    "DMA": "SL49",
}

# Reverse Mapping (SAP SLoc code to primary WMS GI Loc name)
SAP_SLOC_TO_WMS_MAP = {v: k for k, v in WMS_TO_SAP_SLOC_MAP.items()}

# Whitelisted Plant Storage Locations (Subject to MM Stock Accuracy Scoring)
ALLOWED_BALANCE_SLOCS: Set[str] = {
    "SL00", "SL01", "SL02", "SL03", "SL04", "SL49"
}

# Whitelisted External Transfer Buffers / WIP Areas (Excluded from Accuracy Score)
ALLOWED_EXTERNAL_SLOCS: Set[str] = {
    "SP31", "SP32", "SP11"
}

# Complete allowed storage location universe for inventory operations
VALID_OPERATION_SLOCS: Set[str] = ALLOWED_BALANCE_SLOCS | ALLOWED_EXTERNAL_SLOCS


def map_wms_gi_loc_to_sap(gi_loc_str: Optional[str]) -> Optional[str]:
    """
    Converts a WMS 'GI Loc' descriptive string to SAP 4-character SLoc code.
    Example: 'Main parts' -> 'SL01', 'CKD' -> 'SL02', 'Receipt Goods SL' -> 'SL00'
    Returns normalized SLoc string or None if unmapped.
    """
    if not gi_loc_str:
        return None
    cleaned = str(gi_loc_str).strip().upper()
    
    # 1. Direct dictionary match
    if cleaned in WMS_TO_SAP_SLOC_MAP:
        return WMS_TO_SAP_SLOC_MAP[cleaned]
    
    # 2. Check if already an SLxx code
    if cleaned.startswith("SL") and len(cleaned) == 4 and cleaned in VALID_OPERATION_SLOCS:
        return cleaned
    if cleaned.startswith("SP") and len(cleaned) == 4 and cleaned in VALID_OPERATION_SLOCS:
        return cleaned
        
    logger.debug(f"[WMS-MAP] Unmapped or out-of-scope GI Loc: '{gi_loc_str}'")
    return None


def is_d115_sp_sloc(sloc: Optional[str]) -> bool:
    """Checks whether a storage location is a production/line SP* buffer (e.g. SP31, SP32, SP11)."""
    if not sloc:
        return False
    s = str(sloc).strip().upper()
    return len(s) == 4 and s.startswith("SP") and s[2:].isdigit()


def is_valid_target_sloc(sloc: Optional[str]) -> bool:
    """Checks whether a storage location code is whitelisted for posting operations."""
    if not sloc:
        return False
    s = str(sloc).strip().upper()
    return s in VALID_OPERATION_SLOCS or is_d115_sp_sloc(s)


def is_accuracy_balance_sloc(sloc: Optional[str]) -> bool:
    """Checks whether a storage location is in scope for official MM Stock Accuracy scoring."""
    if not sloc:
        return False
    return str(sloc).strip().upper() in ALLOWED_BALANCE_SLOCS


def calc_material_sloc_accuracy(wms_qty: Optional[float], sap_qty: Optional[float], exclude_zero_pair: bool = True) -> Optional[float]:
    """
    Calculates official SAP ZTCODE_STOCK_REC MM Stock Accuracy score (%) per (Material, SLoc).
    Formula verified against production ZTCODE_STOCK_REC snapshot data:
      Score = max(0.0, (1.0 - abs(WMS - SAP) / SAP) * 100.0)
    Notes:
    - If WMS == SAP == 0 and exclude_zero_pair is True: returns None (excluded from ZTCODE_STOCK_REC report denominator).
    - If WMS == SAP: returns 100.0%
    - If SAP <= 0 and WMS > 0: score is 0.0% (unbalanced surplus in WMS, zero denominator)
    """
    w = float(wms_qty if wms_qty is not None else 0.0)
    s = float(sap_qty if sap_qty is not None else 0.0)

    # Both zero: excluded from ZTCODE_STOCK_REC denominator
    if abs(w) < 1e-6 and abs(s) < 1e-6:
        return None if exclude_zero_pair else 100.0

    # Exact match
    if abs(w - s) < 1e-6:
        return 100.0

    # Denominator zero or negative with discrepancy
    if s <= 0.0:
        return 0.0

    ratio = 1.0 - (abs(w - s) / s)
    return max(0.0, round(ratio * 100.0, 3))


def is_in_guard_window(dt: Optional[datetime] = None) -> bool:
    """
    Checks if given datetime (or now) falls in the daily ZTCODE_STOCK_REC aggregation guard window:
    05:30:00 ~ 06:15:00 (EST).
    """
    from datetime import time as dt_time
    check_time = dt if dt is not None else datetime.now()
    t = check_time.time()
    return dt_time(5, 30, 0) <= t <= dt_time(6, 15, 0)


def evaluate_settled_state_accuracy(
    wms_stock_by_sloc: Dict[str, float],
    sap_initial_by_sloc: Dict[str, float],
    sap_final_by_sloc: Dict[str, float],
    sap_intermediate_by_sloc: Optional[Dict[str, float]] = None,
    target_slocs: Optional[Set[str]] = None,
    allow_temporary_mismatch: bool = True,
    current_time: Optional[datetime] = None
) -> Tuple[bool, Dict[str, Any]]:
    """
    Evaluates whether a planned inventory transfer satisfies the ZTCODE_STOCK_REC accuracy objective function.
    (User Decisions D18 & D19)
    
    Conditions:
    1. FINAL SETTLED STATE (ALWAYS REQUIRED AT ALL TIMES):
       - No individual (Material, SLoc) accuracy score may decrease:
         Score_final(L) >= Score_initial(L) for ALL L in target_slocs.
       - The overall mean ZTCODE_STOCK_REC score across active storage locations (excluding 0/0) must increase or hold:
         Mean(Score_final) >= Mean(Score_initial).
    2. INTERMEDIATE STATE (Immediately after MB1B transfer, before GI/Backlog consumption):
       - During the guard window (05:30~06:15) or if allow_temporary_mismatch is False:
         Score_intermediate(L) >= Score_initial(L) for ALL L in target_slocs (temporary dip strictly forbidden).
       - Outside the guard window:
         Temporary dip in intermediate state is permitted IF condition 1 (Final Settled State) is satisfied.
    """
    slocs = target_slocs or ALLOWED_BALANCE_SLOCS
    guard_active = is_in_guard_window(current_time) or (not allow_temporary_mismatch)

    init_scores = {}
    final_scores = {}
    final_sloc_drops = []
    inter_sloc_drops = []

    for loc in slocs:
        w = float(wms_stock_by_sloc.get(loc, 0.0))
        s_init = float(sap_initial_by_sloc.get(loc, 0.0))
        s_final = float(sap_final_by_sloc.get(loc, 0.0))

        sc_init = calc_material_sloc_accuracy(w, s_init, exclude_zero_pair=True)
        sc_final = calc_material_sloc_accuracy(w, s_final, exclude_zero_pair=True)

        if sc_init is not None:
            init_scores[loc] = sc_init
        if sc_final is not None:
            final_scores[loc] = sc_final

        # Check Final State Degradation (Tolerance 1e-6 for floating point precision)
        val_init = sc_init if sc_init is not None else 100.0
        val_final = sc_final if sc_final is not None else 100.0
        if val_final < (val_init - 1e-6):
            final_sloc_drops.append((loc, val_init, val_final))

        # Check Intermediate State Degradation
        if sap_intermediate_by_sloc is not None:
            s_inter = float(sap_intermediate_by_sloc.get(loc, s_init))
            sc_inter = calc_material_sloc_accuracy(w, s_inter, exclude_zero_pair=True)
            val_inter = sc_inter if sc_inter is not None else 100.0
            if val_inter < (val_init - 1e-6):
                inter_sloc_drops.append((loc, val_init, val_inter))

    mean_init = (sum(init_scores.values()) / len(init_scores)) if init_scores else 100.0
    mean_final = (sum(final_scores.values()) / len(final_scores)) if final_scores else 100.0

    # Decision logic
    if final_sloc_drops:
        is_approved = False
        reason = f"FINAL_STATE_SLOC_DROP: SLoc score degraded in final state: {final_sloc_drops}"
    elif mean_final < (mean_init - 1e-6):
        is_approved = False
        reason = f"FINAL_STATE_MEAN_DROP: Mean accuracy decreased from {mean_init:.1f}% to {mean_final:.1f}%"
    elif guard_active and inter_sloc_drops:
        is_approved = False
        reason = f"GUARD_WINDOW_ACTIVE (05:30-06:15): Temporary intermediate SLoc score drop forbidden: {inter_sloc_drops}"
    else:
        is_approved = True
        reason = f"ACCURACY_IMPROVED_OR_PRESERVED: Mean {mean_init:.1f}% -> {mean_final:.1f}% (Inter drops: {len(inter_sloc_drops)})"

    details = {
        "is_approved": is_approved,
        "guard_active": guard_active,
        "mean_init": round(mean_init, 2),
        "mean_final": round(mean_final, 2),
        "score_delta": round(mean_final - mean_init, 2),
        "final_sloc_drops": final_sloc_drops,
        "inter_sloc_drops": inter_sloc_drops,
        "init_scores": init_scores,
        "final_scores": final_scores,
        "reason": reason
    }
    return is_approved, details
