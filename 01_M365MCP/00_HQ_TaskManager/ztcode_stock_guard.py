"""
ZTCODE_GI_ERR Interface Guardrail Common Module (00_HQ_TaskManager / ztcode_stock_guard.py)
==============================================================================
Plant: P001 Headlamp
Standards:
- User Decision D44 (02_JOB GR/GI Interface Collision Prevention & Cross-Job Protection)
- User Decision D90-1 (Full GR->GI->GI Chain Protection across 01_JOB & 02_JOB)
- Universal Freshness Standard: Strictly requires explicit file paths from the current batch.
  Stale disk globbing and mtime fallbacks are prohibited.

Features:
1. parse_sap_utf16_list: Robust parser for SAP tab-delimited UTF-16 GUI exports.
2. extract_active_gr_error_map: Extracts material-keyed summary of active GR errors.
3. extract_active_gi_error_map: Extracts material-keyed summary of active GI errors (with directed Edges).
4. get_gr_chain_protected_slocs / has_gr_chain_gap: Full GR-dependent chain expansion & broken-bridge detection (D90-1 / D90-2).
5. check_material_ztcode_stock_rec_block / check_movement_ztcode_stock_rec_block: Determines whether a material or movement is blocked by active GR or GI error.
"""

import os
import re
import logging
from collections import Counter
from pathlib import Path
from typing import Dict, Any, List, Optional, Sequence, Tuple, Set, Union
import pandas as pd

GRPathInput = Optional[Union[Path, str, Sequence[Union[Path, str]]]]

logger = logging.getLogger("ZTCODE_STOCK_RECGuard")


def parse_sap_utf16_list(filepath: Path) -> pd.DataFrame:
    """Parses SAP UTF-16 tab-delimited exported list files."""
    if not filepath or not filepath.exists():
        return pd.DataFrame()

    try:
        with open(filepath, 'r', encoding='utf-16', errors='replace') as f:
            lines = f.readlines()
    except Exception as e_utf16:
        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            logger.error(f"Failed to read SAP export file {filepath}: {e_utf16}")
            return pd.DataFrame()

    header = None
    data_rows = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith('---') or 'Dynamic List Display' in stripped:
            continue
        if 'Interface Log' in stripped:
            continue

        parts = [p.strip() for p in line.split('\t') if p.strip()]
        if not parts:
            continue

        if 'Material Number' in parts or 'Material' in parts:
            header = parts
            continue

        if header and len(parts) >= 5:
            # Row typically begins with Date YYYY.MM.DD
            if re.match(r'^\d{4}\.\d{2}\.\d{2}$', parts[0]):
                row_dict = {}
                for i, col in enumerate(header):
                    if i < len(parts):
                        row_dict[col] = parts[i]
                    else:
                        row_dict[col] = ""
                data_rows.append(row_dict)

    return pd.DataFrame(data_rows)


def expand_gr_batch_paths(gr_path: GRPathInput) -> List[Path]:
    """
    Expands explicit GR file path(s) to include same-batch companion domain files
    (`GR_Error_<ts>.XLS`, `GR_Domestics_<ts>.XLS`, `GR_Import_<ts>.XLS`) from the SAME extraction
    cycle (#187 B-1). Never performs stale globbing across older timestamps:
      - If a filename carries an explicit `_YYYYMMDD_HHMMSS` timestamp, only sibling files sharing
        that exact `YYYYMMDD_HHMMSS` are attached.
      - If a canonical `GR_Error_YYYYMMDD.XLS` path is passed, only same-day `_YYYYMMDD_HHMMSS`
        companions whose mtime is within +-15s of the canonical file are attached.
    """
    if not gr_path:
        return []
    raw_list: List[Path] = []
    if isinstance(gr_path, (str, Path)):
        raw_list = [Path(gr_path)]
    else:
        raw_list = [Path(x) for x in gr_path if x]

    expanded: List[Path] = []
    seen: Set[str] = set()

    def _add(cand: Path) -> None:
        if cand and cand.exists():
            key = str(cand.resolve()).lower()
            if key not in seen:
                seen.add(key)
                expanded.append(cand)

    for p in raw_list:
        if not p or not p.exists():
            continue
        _add(p)
        parent = p.parent
        m_ts = re.search(r"(\d{8}_\d{6})", p.name)
        if m_ts:
            ts = m_ts.group(1)
            for prefix in ("GR_Error", "GR_Domestics", "GR_Import"):
                _add(parent / f"{prefix}_{ts}.XLS")
        else:
            m_day = re.match(r"^GR_(?:Error|Domestics|Import)_(\d{8})\.XLS$", p.name, re.IGNORECASE)
            if m_day:
                ymd = m_day.group(1)
                base_mtime = p.stat().st_mtime
                for prefix in ("GR_Error", "GR_Domestics", "GR_Import"):
                    for sib in parent.glob(f"{prefix}_{ymd}_*.XLS"):
                        try:
                            if abs(sib.stat().st_mtime - base_mtime) <= 15.0:
                                _add(sib)
                        except OSError:
                            pass
    return expanded


def parse_merged_gr_dataframe(gr_path: GRPathInput) -> pd.DataFrame:
    """
    Parses and merges all fresh GR files (`GR_Error` + `GR_Domestics` + `GR_Import`) for a cycle (#187 B-1).
    Uses multiset reconciliation `max(C_combined[key], C_dom[key] + C_imp[key], C_other[key])` so:
      1. Genuine multi-box rows with identical `(date, vendor, mat, mvt, stor, qty)` inside `GR_Import`
         (e.g. `MAT_GR300` `60 + 60 + 180 = 300 EA`) are NEVER collapsed.
      2. Rows present in BOTH `GR_Error` (combined) and `GR_Import`/`GR_Domestics` are NEVER double-counted.
      3. Any row present ONLY in `GR_Import` or `GR_Domestics` (when `GR_Error` is partial/missing) is preserved.
    """
    paths = expand_gr_batch_paths(gr_path)
    if not paths:
        return pd.DataFrame()

    def _norm_records(df: pd.DataFrame, default_type: str) -> List[Dict[str, Any]]:
        if df is None or df.empty:
            return []
        cols = [str(c).strip() for c in df.columns]
        df = df.copy()
        df.columns = cols
        mat_c = next((c for c in cols if "Material Number" in c or c == "Material"), None)
        if not mat_c:
            return []
        desc_c = next((c for c in cols if "Material Desc" in c or "Description" in c), None)
        vnd_c = next((c for c in cols if "Vendor" in c or "Supplier" in c), None)
        dt_c = next((c for c in cols if c in ("GR Date", "Date", "Created On", "Posting Date", "Doc. Date") or "Date" in c), None)
        mvt_c = next((c for c in cols if c == "Mvt" or "Mvt" in c), None)
        stor_c = next((c for c in cols if c in ("Stor", "Stor.", "Storage Location", "SLoc") or "Stor" in c), None)
        qty_c = next((c for c in cols if c == "Qty" or "Qty" in c or "Quantity" in c), None)
        type_c = next((c for c in cols if c == "GR_Type"), None)

        recs: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            mat = str(r.get(mat_c, "")).strip()
            if not mat or mat.lower() in ("nan", "material", "*"):
                continue
            q_str = str(r.get(qty_c, "0")).replace(",", "").strip() if qty_c else "0"
            try:
                qty = float(q_str)
            except ValueError:
                qty = 0.0
            recs.append({
                "GR Date": str(r.get(dt_c, "")).strip() if dt_c else "",
                "Vendor": str(r.get(vnd_c, "")).strip() if vnd_c else "",
                "Material Number": mat,
                "Material Desc": str(r.get(desc_c, "")).strip() if desc_c else "",
                "Mvt": str(r.get(mvt_c, "")).replace(".0", "").strip() if mvt_c else "",
                "Stor": str(r.get(stor_c, "")).strip().upper() if stor_c else "",
                "Qty": qty,
                "GR_Type": (str(r.get(type_c, "")).strip() or default_type) if type_c else default_type,
            })
        return recs

    combined_buckets: List[Counter] = []
    dom_buckets: List[Counter] = []
    imp_buckets: List[Counter] = []
    other_buckets: List[Counter] = []
    exemplar_by_key: Dict[Tuple[str, str, str, str, str, float], Dict[str, Any]] = {}

    for p in paths:
        lname = p.name.lower()
        dtype = "DOM" if "domestic" in lname else ("IMP" if "import" in lname else "COMB")
        recs = _norm_records(parse_sap_utf16_list(p), dtype)
        c: Counter = Counter()
        for rec in recs:
            key = (
                rec["GR Date"],
                rec["Vendor"],
                rec["Material Number"],
                rec["Mvt"],
                rec["Stor"],
                round(float(rec["Qty"]), 3),
            )
            c[key] += 1
            if key not in exemplar_by_key or (exemplar_by_key[key].get("GR_Type") == "COMB" and rec["GR_Type"] in ("DOM", "IMP")):
                exemplar_by_key[key] = rec
        if "gr_domestic" in lname:
            dom_buckets.append(c)
        elif "gr_import" in lname:
            imp_buckets.append(c)
        elif "gr_error" in lname:
            combined_buckets.append(c)
        else:
            other_buckets.append(c)

    all_keys: Set[Tuple[str, str, str, str, str, float]] = set(exemplar_by_key.keys())
    merged_rows: List[Dict[str, Any]] = []
    for key in sorted(all_keys, key=lambda k: (k[2], k[0], k[1], -k[5])):
        c_comb = max((b[key] for b in combined_buckets), default=0)
        c_dom = max((b[key] for b in dom_buckets), default=0)
        c_imp = max((b[key] for b in imp_buckets), default=0)
        c_oth = sum(b[key] for b in other_buckets)
        final_cnt = max(c_comb, c_dom + c_imp, c_oth)
        base_rec = exemplar_by_key[key]
        for _ in range(final_cnt):
            merged_rows.append(dict(base_rec))

    return pd.DataFrame(merged_rows)


def extract_active_gr_error_map(gr_path: GRPathInput) -> Dict[str, Dict[str, Any]]:
    """
    Extracts active GR (Goods Receipt) error materials from fresh GR export(s)
    (`GR_Error` + `GR_Domestics` + `GR_Import`, #187 B-1).
    Returns:
        Dict[material -> { 'Material': mat, 'Desc': desc, 'Total_GR_Qty': float,
                            'Count': int, 'Vendors': set, 'Dates': set, 'SLocs': set }]
    """
    gr_df = parse_merged_gr_dataframe(gr_path)
    if gr_df.empty:
        return {}

    gr_items: Dict[str, Dict[str, Any]] = {}

    mat_col = next((c for c in gr_df.columns if 'Material Number' in c or c == 'Material'), None)
    if not mat_col:
        return {}

    for _, row in gr_df.iterrows():
        mat = str(row.get(mat_col, '')).strip()
        if not mat:
            continue

        qty_str = str(row.get('Qty', '0')).replace(',', '').strip()
        try:
            qty = float(qty_str)
        except ValueError:
            qty = 0.0

        desc = str(row.get('Material Desc', '')).strip()
        vendor = str(row.get('Vendor', '')).strip()
        gr_date = str(row.get('Date', row.get('GR Date', ''))).strip()
        stor = str(row.get('Stor', row.get('Storage Location', row.get('SLoc', '')))).strip().upper()

        if mat not in gr_items:
            gr_items[mat] = {
                'Material': mat,
                'Desc': desc,
                'Total_GR_Qty': 0.0,
                'Count': 0,
                'Vendors': set(),
                'Dates': set(),
                'SLocs': set()
            }
        gr_items[mat]['Total_GR_Qty'] += qty
        gr_items[mat]['Count'] += 1
        if vendor:
            gr_items[mat]['Vendors'].add(vendor)
        if gr_date:
            gr_items[mat]['Dates'].add(gr_date)
        if stor:
            gr_items[mat]['SLocs'].add(stor)

    return gr_items


def gi_flow(mvt: Any, from_st: Any, to_st: Any) -> Tuple[Optional[str], Optional[str]]:
    """
    Standardizes ZTCODE_GI_ERR GI movement direction under User Decision D47:
    - Mvt '311': normal direction, source (deficit/departure) = from_st, destination (arrival) = to_st
    - Mvt '312': reversal direction, source (deficit/departure) = to_st, destination (arrival) = from_st
    - Other movements (901, 902, etc.): not transfer flow, returns (None, None)

    Returns:
        (src, dst) tuple where src is deficit/departure SLoc, dst is destination SLoc.
    """
    m = str(mvt).strip() if mvt is not None else ""
    f = str(from_st).strip().upper() if from_st else ""
    t = str(to_st).strip().upper() if to_st else ""

    if m == '311':
        return (f, t)
    elif m == '312':
        return (t, f)
    else:
        return (None, None)


def extract_active_gi_error_map(gi_path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """
    Extracts active GI (Goods Issue) error materials from the fresh GI export.
    Conservatively includes all movements (311, 312, 901, 902) and empty message rows.
    Uses gi_flow (D47) to properly identify true deficit/source and destination storage locations.
    Returns:
        Dict[material -> { 'Material': mat, 'Desc': desc, 'Total_GI_Qty': float,
                            'Count': int, 'FromSLocs': set, 'ToSLocs': set,
                            'SrcSLocs': set, 'DstSLocs': set, 'Edges': set, 'Mvts': set }]
    """
    if not gi_path or not Path(gi_path).exists():
        return {}

    gi_df = parse_sap_utf16_list(Path(gi_path))
    if gi_df.empty:
        return {}

    gi_items: Dict[str, Dict[str, Any]] = {}

    mat_col = next((c for c in gi_df.columns if 'Material Number' in c or c == 'Material'), None)
    if not mat_col:
        return {}

    for _, row in gi_df.iterrows():
        mat = str(row.get(mat_col, '')).strip()
        if not mat:
            continue

        qty_str = str(row.get('Qty', '0')).replace(',', '').strip()
        try:
            qty = float(qty_str)
        except ValueError:
            qty = 0.0

        desc = str(row.get('Material Desc', '')).strip()
        from_st = str(row.get('FromSt', '')).strip().upper()
        to_st = str(row.get('TOSt', '')).strip().upper()
        mvt = str(row.get('Mvt', '')).strip()

        src_loc, dst_loc = gi_flow(mvt, from_st, to_st)

        if mat not in gi_items:
            gi_items[mat] = {
                'Material': mat,
                'Desc': desc,
                'Total_GI_Qty': 0.0,
                'Count': 0,
                'FromSLocs': set(),
                'ToSLocs': set(),
                'SrcSLocs': set(),
                'DstSLocs': set(),
                'Edges': set(),
                'Mvts': set()
            }
        gi_items[mat]['Total_GI_Qty'] += qty
        gi_items[mat]['Count'] += 1
        if from_st:
            gi_items[mat]['FromSLocs'].add(from_st)
        if to_st:
            gi_items[mat]['ToSLocs'].add(to_st)
        if src_loc:
            gi_items[mat]['SrcSLocs'].add(src_loc)
        if dst_loc:
            gi_items[mat]['DstSLocs'].add(dst_loc)
        if src_loc and dst_loc:
            gi_items[mat]['Edges'].add((src_loc, dst_loc))
        if mvt:
            gi_items[mat]['Mvts'].add(mvt)

    return gi_items


def _extract_gi_edges_for_material(
    gi_entry: Optional[Dict[str, Any]],
    extra_edges: Optional[Set[Tuple[str, str]]] = None
) -> Set[Tuple[str, str]]:
    """Extracts directed (src, dst) GI error edges for a material."""
    edges: Set[Tuple[str, str]] = set()
    if extra_edges:
        for u, v in extra_edges:
            if u and v:
                edges.add((str(u).strip().upper(), str(v).strip().upper()))
    if not gi_entry:
        return edges

    raw_edges = gi_entry.get('Edges') or gi_entry.get('edges')
    if raw_edges:
        for u, v in raw_edges:
            if u and v:
                edges.add((str(u).strip().upper(), str(v).strip().upper()))
    else:
        src_slocs = gi_entry.get('SrcSLocs') or set()
        dst_slocs = gi_entry.get('DstSLocs') or set()
        if len(src_slocs) == 1 and len(dst_slocs) == 1:
            u = str(next(iter(src_slocs))).strip().upper()
            v = str(next(iter(dst_slocs))).strip().upper()
            if u and v:
                edges.add((u, v))
    return edges


def get_gr_chain_protected_slocs(
    mat: str,
    active_gr_errors: Optional[Dict[str, Dict[str, Any]]],
    active_gi_errors: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_edges: Optional[Set[Tuple[str, str]]] = None
) -> Set[str]:
    """
    D90-1 Full Chain Protection:
    Computes the set of storage locations protected by an active GR error and any
    unbroken directed chain of GI errors starting from the GR error SLoc(s).
    Example:
    - GR(SL00) + GI(SL00 -> SL01) + GI(SL01 -> SP31):
      Starts with {'SL00'} -> expands via (SL00, SL01) to {'SL00', 'SL01'} ->
      expands via (SL01, SP31) to {'SL00', 'SL01', 'SP31'}.
    - GR(SL00) + GI(SL01 -> SP31) [WITHOUT GI(SL00 -> SL01)]:
      Starts with {'SL00'} -> (SL01, SP31) is disconnected from SL00 ->
      remains {'SL00'} (SL01 -> SP31 is NOT GR-chain protected; flagged as CHAIN_GAP).
    """
    mat_clean = str(mat).strip()
    if not active_gr_errors or mat_clean not in active_gr_errors:
        return set()

    gr = active_gr_errors[mat_clean]
    base_slocs = {str(s).strip().upper() for s in (gr.get('SLocs') or gr.get('slocs') or set()) if str(s).strip()}
    if not base_slocs:
        return set()

    protected = set(base_slocs)
    gi_entry = (active_gi_errors or {}).get(mat_clean)
    edges = _extract_gi_edges_for_material(gi_entry, extra_edges=extra_edges)
    if not edges:
        return protected

    changed = True
    while changed:
        changed = False
        for u, v in edges:
            if u in protected and v and v not in protected:
                protected.add(v)
                changed = True

    return protected


def has_gr_chain_gap(
    mat: str,
    active_gr_errors: Optional[Dict[str, Dict[str, Any]]],
    active_gi_errors: Optional[Dict[str, Dict[str, Any]]] = None,
    extra_edges: Optional[Set[Tuple[str, str]]] = None
) -> bool:
    """
    D90-2 Chain Gap Detection:
    Returns True when a material has an active GR error (e.g. at SL00) AND at least
    one active GI error edge (u -> v, such as SL01 -> SP31) that is disconnected
    from the GR error SLoc chain (i.e. missing intermediate bridge SL00 -> SL01).
    """
    mat_clean = str(mat).strip()
    if not active_gr_errors or mat_clean not in active_gr_errors:
        return False

    gr = active_gr_errors[mat_clean]
    base_slocs = {str(s).strip().upper() for s in (gr.get('SLocs') or gr.get('slocs') or {'SL00'}) if str(s).strip()}
    if not base_slocs:
        base_slocs = {'SL00'}

    gi_entry = (active_gi_errors or {}).get(mat_clean)
    edges = _extract_gi_edges_for_material(gi_entry, extra_edges=extra_edges)
    if not edges:
        return False

    protected = get_gr_chain_protected_slocs(
        mat_clean,
        {mat_clean: {**gr, 'SLocs': base_slocs}},
        active_gi_errors=active_gi_errors,
        extra_edges=extra_edges
    )
    for u, v in edges:
        if u not in protected and v not in protected:
            return True
    return False


def check_material_ztcode_stock_rec_block(
    mat: str,
    active_gr_errors: Dict[str, Dict[str, Any]],
    active_gi_errors: Dict[str, Dict[str, Any]]
) -> Tuple[bool, Optional[str], Optional[str], float, str]:
    """
    Checks if a material is blocked by active GR or GI errors (D44-2).
    GR check takes precedence over GI check.

    Returns:
        (is_blocked, status, reason, pending_qty, info_str)
    """
    mat_clean = str(mat).strip()

    # 1. GR error check
    if mat_clean in active_gr_errors:
        gr = active_gr_errors[mat_clean]
        v_str = ", ".join(sorted(list(gr.get('Vendors', [])))) or "N/A"
        qty = gr.get('Total_GR_Qty', 0.0)
        cnt = gr.get('Count', 1)
        reason = f"Active inbound GR error exists ({qty:,.1f} EA, {cnt} records, Vendor: {v_str}) - Blocked by D44 Guard"
        return True, "BLOCKED_BY_GR_ERROR", reason, qty, v_str

    # 2. GI error check
    if mat_clean in active_gi_errors:
        gi = active_gi_errors[mat_clean]
        loc_str = ", ".join(sorted(list(gi.get('SrcSLocs') or gi.get('FromSLocs', [])))) or "N/A"
        qty = gi.get('Total_GI_Qty', 0.0)
        cnt = gi.get('Count', 1)
        reason = f"Active GI interface error exists ({qty:,.1f} EA, {cnt} records, SLoc: {loc_str}) - Blocked by D44 Guard"
        return True, "BLOCKED_BY_GI_ERROR", reason, qty, loc_str

    return False, None, None, 0.0, ""


def check_movement_ztcode_stock_rec_block(
    mat: str,
    src_sloc: Optional[str],
    dst_sloc: Optional[str],
    active_gr_errors: Dict[str, Dict[str, Any]],
    active_gi_errors: Dict[str, Dict[str, Any]]
) -> Tuple[bool, Optional[str], Optional[str], float, str]:
    """
    Checks if a specific material movement (from src_sloc to dst_sloc) is blocked
    by active GR or GI errors under User Decision D71 (076 D71 수정) & D90-1 (Full Chain Protection).
    
    Rules:
    1. GR Error (Inbound receipt delay + D90-1 Full Chain Protection):
       Narrowed to intersecting storage locations (076 D71 수정) expanded along unbroken
       directed GI error chains starting from the GR error SLoc(s) (D90-1).
       Movement is blocked ONLY IF src_sloc or dst_sloc is present in that involved SLoc set
       (or if no specific SLoc was recorded on the error row, as fail-closed).
       Otherwise, allowed to proceed to standard stock accuracy evaluation.
    2. GI Error (Interface transfer error):
       Narrowed to intersecting storage locations (D71).
       Extracts the union of all involved storage locations (SrcSLocs | DstSLocs | FromSLocs | ToSLocs)
       for the active GI errors of this material.
       Movement is blocked ONLY IF src_sloc or dst_sloc is present in that involved SLoc set.
       Otherwise, allowed to proceed to standard stock accuracy evaluation.
    
    Returns:
        (is_blocked, status, reason, pending_qty, info_str)
    """
    mat_clean = str(mat).strip()
    req_src = str(src_sloc).strip().upper() if src_sloc else ""
    req_dst = str(dst_sloc).strip().upper() if dst_sloc else ""

    # 1. GR error check: intersecting SLoc block (076 D71 수정 + D90-1 Full Chain Protection)
    if mat_clean in active_gr_errors:
        gr = active_gr_errors[mat_clean]
        base_gr_slocs = gr.get('SLocs', set())
        gr_slocs = get_gr_chain_protected_slocs(mat_clean, active_gr_errors, active_gi_errors) if base_gr_slocs else set()

        overlapping = set()
        if req_src and req_src in gr_slocs:
            overlapping.add(req_src)
        if req_dst and req_dst in gr_slocs:
            overlapping.add(req_dst)

        # Block if overlapping SLoc is found, or if no specific SLoc was recorded (fail-closed), or if both endpoints unspecified
        if overlapping or (not base_gr_slocs) or (not req_src and not req_dst):
            overlap_str = ", ".join(sorted(list(overlapping))) if overlapping else (", ".join(sorted(list(gr_slocs))) or "N/A")
            v_str = ", ".join(sorted(list(gr.get('Vendors', [])))) or "N/A"
            qty = gr.get('Total_GR_Qty', 0.0)
            cnt = gr.get('Count', 1)
            reason = f"Active inbound GR error overlaps involved SLoc [{overlap_str}] ({qty:,.1f} EA, {cnt} records, Vendor: {v_str}) - Blocked by D71/D90-1 Guard"
            return True, "BLOCKED_BY_GR_ERROR", reason, qty, overlap_str

    # 2. GI error check: intersecting SLoc block (D71)
    if mat_clean in active_gi_errors:
        gi = active_gi_errors[mat_clean]
        involved_slocs = set()
        involved_slocs.update(gi.get('SrcSLocs', set()))
        involved_slocs.update(gi.get('DstSLocs', set()))
        involved_slocs.update(gi.get('FromSLocs', set()))
        involved_slocs.update(gi.get('ToSLocs', set()))

        overlapping = set()
        if req_src and req_src in involved_slocs:
            overlapping.add(req_src)
        if req_dst and req_dst in involved_slocs:
            overlapping.add(req_dst)

        if overlapping or (not req_src and not req_dst):
            overlap_str = ", ".join(sorted(list(overlapping))) if overlapping else ", ".join(sorted(list(involved_slocs)))
            qty = gi.get('Total_GI_Qty', 0.0)
            cnt = gi.get('Count', 1)
            reason = f"Active GI error overlaps involved SLoc [{overlap_str}] ({qty:,.1f} EA, {cnt} records) - Blocked by D71 Guard"
            return True, "BLOCKED_BY_GI_ERROR", reason, qty, overlap_str

    return False, None, None, 0.0, ""
