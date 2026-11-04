"""
Shared GI Error Verdict & Root-Cause Engine (D108 & D107)
=========================================================
01_DEPT_Inventory / Shared across:
  - 02_JOB_Backlog_Clear (D108: reclassify BLOCKED_BY_GI_ERROR by 01 verdict)
  - 03_JOB_Backlog_Analysis (D108: reclassify OTHER_GI_HOLD in D103 mart)
  - 01_JOB_GR_GI_Error (D107: Phase 1 unresolvable GI error delete candidates)

Core Contract (#183 & #184 & #180~#182):
  Given 1 GI error record (or all active GI errors as of `as_of_dt`):
    1. Preceding 01 cycle verdict (`GI_Resolution_Plan` + `pipeline_run_metrics` + `transfer_posting_history`)
    2. 7-day GR linkage (`gr_error_snapshots` active & 7d + `GI_STOCK_GAP_SIGNALS` WMS>SAP + `ops_raw.db` MB51 101/131)
    3. Source & plant SAP vs WMS physical stock + pump/roundtrip availability
    4. Anomaly detection when 01 judged resolvable yet GI error remains:
       - ① NEW_AFTER_01: created after preceding 01 collection timestamp
       - ② GUARD_UNPOSTED: 01 found transfer path but held posting via guard (e.g. D37 SL00, D77)
       - ③ POSTING_FAILED / POSTED_BUT_REMAINED: 01 attempted posting and failed or remained

All SQLite connections strictly use read-only URI (`?mode=ro`) with zero writes.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import pandas as pd

logger = logging.getLogger("GIVerdictShared")
from report_labels import EN as _EN

INVENTORY_DEPT_DIR = Path(__file__).resolve().parent
DEFAULT_GI_DIR = INVENTORY_DEPT_DIR / "01_JOB_GR_GI_Error"
DEFAULT_GI_DB = DEFAULT_GI_DIR / "db" / "gr_gi_history.db"
DEFAULT_GI_PLAN_DIR = DEFAULT_GI_DIR / "resolution_logs"
DEFAULT_OPS_RAW_DB = INVENTORY_DEPT_DIR.parent / "00_HQ_TaskManager" / "data_collector" / "db" / "ops_raw.db"

BALANCE_WAREHOUSE_SLOCS: Tuple[str, ...] = ("SL00", "SL01", "SL02", "SL03", "SL04")
D107_TARGET_MVTS: Set[str] = {"311", "312"}


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _safe_parse_dict(val: Any) -> Dict[str, float]:
    if isinstance(val, dict):
        return {str(k).strip().upper(): float(v or 0.0) for k, v in val.items()}
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return {}
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return {}
    try:
        parsed = ast.literal_eval(s)
        if isinstance(parsed, dict):
            return {str(k).strip().upper(): float(v or 0.0) for k, v in parsed.items()}
    except Exception:
        pass
    return {}


D109_SL00_GUARD_RELEASE_RUNS = 4


def _parse_as_of_dt(as_of: Optional[Any]) -> datetime:
    if isinstance(as_of, datetime):
        return as_of
    if isinstance(as_of, date):
        return datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59)
    if not as_of:
        return datetime.now()
    s = str(as_of).strip()
    # Extract YYYYMMDD_HHMMSS or YYYYMMDD_HHMM inside batch IDs like BACKLOG_MANUAL_NOW_20261029_081502 or RUN_20261029_080000
    m6 = re.search(r"(\d{8})_(\d{6})", s)
    if m6:
        return datetime.strptime(f"{m6.group(1)}_{m6.group(2)}", "%Y%m%d_%H%M%S")
    m4 = re.search(r"(\d{8})_(\d{4})", s)
    if m4:
        return datetime.strptime(f"{m4.group(1)}_{m4.group(2)}59", "%Y%m%d_%H%M%S")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        try:
            dt = datetime.strptime(s, fmt)
            if fmt in ("%Y-%m-%d", "%Y%m%d"):
                return dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    raise ValueError(f"Unrecognized as_of timestamp format: {as_of!r}")


def _parse_sap_date(val: Any) -> Optional[date]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    if not s or s.lower() == "nan":
        return None
    s_clean = s[:10].replace(".", "-").replace("/", "-")
    try:
        return datetime.strptime(s_clean, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_plan_ts_from_name(path: Path) -> Optional[datetime]:
    m = re.search(r"GI_Resolution_Plan_(\d{8})_(\d{6})\.xlsx$", path.name)
    if not m:
        return None
    try:
        return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _batch_nominal_dt(batch_id: str, fallback_dt: Optional[datetime] = None) -> Optional[datetime]:
    m = re.search(r"RUN_(\d{8})_(\d{6})", str(batch_id or ""))
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        except ValueError:
            pass
    return fallback_dt


def find_preceding_gi_cycle(
    as_of_dt: datetime,
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    exact_batch_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Finds the latest completed 01 GI cycle <= as_of_dt (or exact_batch_id) and its matching GI_Resolution_Plan Excel.
    """
    result: Dict[str, Any] = {
        "batch_id": None,
        "run_time_edt": None,
        "extracted_at_edt": None,
        "plan_path": None,
        "plan_time_edt": None,
        "plan_df": pd.DataFrame(),
        "gi_snapshots": [],
        "postings": [],
    }
    as_of_str = as_of_dt.strftime("%Y-%m-%d %H:%M:%S")

    if gi_db_path and Path(gi_db_path).exists():
        try:
            with _connect_ro(Path(gi_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                if exact_batch_id:
                    row = conn.execute(
                        """
                        SELECT batch_id, datetime(run_timestamp, 'localtime') AS run_local, status, postings_executed
                        FROM pipeline_run_metrics
                        WHERE batch_id = ?
                        ORDER BY id DESC LIMIT 1
                        """,
                        (exact_batch_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        """
                        SELECT batch_id, datetime(run_timestamp, 'localtime') AS run_local, status, postings_executed
                        FROM pipeline_run_metrics
                        WHERE status = 'COMPLETED'
                          AND datetime(run_timestamp, 'localtime') <= ?
                        ORDER BY run_timestamp DESC, id DESC LIMIT 1
                        """,
                        (as_of_str,),
                    ).fetchone()

                if row:
                    b_id = row["batch_id"]
                    result["batch_id"] = b_id
                    result["run_time_edt"] = row["run_local"]
                else:
                    # Fallback to latest gi_error_snapshots batch <= as_of_dt
                    snap_row = conn.execute(
                        """
                        SELECT batch_id, MAX(datetime(extracted_at, 'localtime')) AS ext_local
                        FROM gi_error_snapshots
                        WHERE datetime(extracted_at, 'localtime') <= ?
                        GROUP BY batch_id
                        ORDER BY ext_local DESC LIMIT 1
                        """,
                        (as_of_str,),
                    ).fetchone()
                    if snap_row:
                        result["batch_id"] = snap_row["batch_id"]
                        result["run_time_edt"] = snap_row["ext_local"]

                if result["batch_id"]:
                    b_id = result["batch_id"]
                    snaps = conn.execute(
                        """
                        SELECT id, batch_id, datetime(extracted_at, 'localtime') AS extracted_local,
                               error_date, vendor, material, material_desc, mvt, from_sloc, to_sloc, qty, message
                        FROM gi_error_snapshots
                        WHERE batch_id = ?
                        ORDER BY id ASC
                        """,
                        (b_id,),
                    ).fetchall()
                    result["gi_snapshots"] = [dict(r) for r in snaps]
                    if result["gi_snapshots"]:
                        result["extracted_at_edt"] = result["gi_snapshots"][0]["extracted_local"]

                    posts = conn.execute(
                        """
                        SELECT id, batch_id, datetime(executed_at, 'localtime') AS executed_local,
                               material, from_sloc, to_sloc, transfer_qty, mb1b_mvt,
                               doc_number, post_status, posting_type, sap_response, reason
                        FROM transfer_posting_history
                        WHERE batch_id = ?
                        ORDER BY id ASC
                        """,
                        (b_id,),
                    ).fetchall()
                    result["postings"] = [dict(r) for r in posts]
        except Exception as exc:
            logger.warning(f"[GI_VERDICT] Read-only GI DB lookup warning: {exc}")

    # Locate matching GI_Resolution_Plan_*.xlsx
    if gi_plan_dir and Path(gi_plan_dir).exists():
        plan_candidates: List[Tuple[datetime, Path]] = []
        for p in Path(gi_plan_dir).glob("GI_Resolution_Plan_*.xlsx"):
            pts = _parse_plan_ts_from_name(p)
            if pts is not None and pts <= (as_of_dt + timedelta(seconds=30)):
                plan_candidates.append((pts, p))
        plan_candidates.sort(key=lambda x: x[0], reverse=True)

        chosen_plan: Optional[Tuple[datetime, Path]] = None
        if result["batch_id"] and plan_candidates:
            run_dt = (
                datetime.strptime(result["run_time_edt"], "%Y-%m-%d %H:%M:%S")
                if result["run_time_edt"]
                else as_of_dt
            )
            nom_dt = _batch_nominal_dt(result["batch_id"], run_dt)
            for pts, p in plan_candidates:
                if nom_dt and (nom_dt - timedelta(minutes=2)) <= pts <= (run_dt + timedelta(minutes=5)):
                    chosen_plan = (pts, p)
                    break
        if chosen_plan is None and plan_candidates:
            chosen_plan = plan_candidates[0]

        if chosen_plan is not None:
            pts, p_path = chosen_plan
            result["plan_path"] = p_path
            result["plan_time_edt"] = pts.strftime("%Y-%m-%d %H:%M:%S")
            try:
                df = pd.read_excel(p_path)
                if "Material" in df.columns:
                    df["Material"] = df["Material"].astype(str).str.strip()
                result["plan_df"] = df
            except Exception as exc:
                logger.warning(f"[GI_VERDICT] Could not read GI plan {p_path.name}: {exc}")

    return result


def _load_gr_and_gap_evidence(
    materials: Set[str],
    as_of_dt: datetime,
    current_batch_id: Optional[str],
    gi_db_path: Path = DEFAULT_GI_DB,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
) -> Dict[str, Dict[str, Any]]:
    """
    Loads 7-day & current-batch GR error snapshots, 7-day GI_STOCK_GAP_SIGNALS (WMS > SAP),
    first-observed GI timestamps, and 7-day MB51 101/131 receipt evidence per material.
    """
    evidence: Dict[str, Dict[str, Any]] = {
        m: {
            "gr_active_count": 0,
            "gr_active_qty": 0.0,
            "gr_active_vendors": [],
            "gr_active_dates": [],
            "gr_7d_count": 0,
            "gr_7d_qty": 0.0,
            "gr_7d_batches": 0,
            "gr_7d_last_date": "",
            "gr_7d_last_ext_edt": "",
            "gr_7d_vendors": [],
            "gap_7d_wms_gt_sap_count": 0,
            "gap_7d_types": [],
            "gap_7d_max_gap": 0.0,
            "mb51_available": False,
            "mb51_max_cpudt": "",
            "mb51_7d_101_131_rows": [],
            "first_seen_by_sig": {},
        }
        for m in materials
    }
    if not materials:
        return evidence

    start_7d_str = (as_of_dt - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    as_of_str = as_of_dt.strftime("%Y-%m-%d %H:%M:%S")
    mat_list = sorted(materials)
    placeholders = ",".join("?" for _ in mat_list)

    if gi_db_path and Path(gi_db_path).exists():
        try:
            with _connect_ro(Path(gi_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # 1. Current batch active GR errors (catches GR_Error + GR_Import + GR_Domestics)
                if current_batch_id:
                    rows = conn.execute(
                        f"""
                        SELECT material, vendor, error_date, qty, mvt, message
                        FROM gr_error_snapshots
                        WHERE batch_id = ? AND material IN ({placeholders})
                        """,
                        [current_batch_id, *mat_list],
                    ).fetchall()
                    for r in rows:
                        m = str(r["material"]).strip()
                        ev = evidence[m]
                        ev["gr_active_count"] += 1
                        ev["gr_active_qty"] += float(r["qty"] or 0.0)
                        v = str(r["vendor"] or "").strip()
                        if v and v not in ev["gr_active_vendors"]:
                            ev["gr_active_vendors"].append(v)
                        ed = str(r["error_date"] or "").strip()
                        if ed and ed not in ev["gr_active_dates"]:
                            ev["gr_active_dates"].append(ed)

                # 2. 7-day GR error snapshots
                rows_7d = conn.execute(
                    f"""
                    SELECT material, batch_id, vendor, error_date, qty, datetime(extracted_at, 'localtime') AS ext_local
                    FROM gr_error_snapshots
                    WHERE datetime(extracted_at, 'localtime') BETWEEN ? AND ?
                      AND material IN ({placeholders})
                    ORDER BY extracted_at DESC
                    """,
                    [start_7d_str, as_of_str, *mat_list],
                ).fetchall()
                batches_by_mat: Dict[str, Set[str]] = {m: set() for m in materials}
                for r in rows_7d:
                    m = str(r["material"]).strip()
                    ev = evidence[m]
                    ev["gr_7d_count"] += 1
                    ev["gr_7d_qty"] += float(r["qty"] or 0.0)
                    batches_by_mat[m].add(str(r["batch_id"]))
                    if not ev["gr_7d_last_date"]:
                        ev["gr_7d_last_date"] = str(r["error_date"] or r["ext_local"] or "")
                    if not ev["gr_7d_last_ext_edt"]:
                        ev["gr_7d_last_ext_edt"] = str(r["ext_local"] or "")
                    v = str(r["vendor"] or "").strip()
                    if v and v not in ev["gr_7d_vendors"]:
                        ev["gr_7d_vendors"].append(v)
                for m, bset in batches_by_mat.items():
                    evidence[m]["gr_7d_batches"] = len(bset)

                # 3. 7-day GI_STOCK_GAP_SIGNALS where WMS > SAP
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "GI_STOCK_GAP_SIGNALS" in tables:
                    sig_date_start = (as_of_dt.date() - timedelta(days=7)).strftime("%Y-%m-%d")
                    sig_date_end = as_of_dt.date().strftime("%Y-%m-%d")
                    sig_rows = conn.execute(
                        f"""
                        SELECT material, signal_type, sap_total, wms_total, total_gap, signal_date
                        FROM GI_STOCK_GAP_SIGNALS
                        WHERE signal_date BETWEEN ? AND ?
                          AND material IN ({placeholders})
                        """,
                        [sig_date_start, sig_date_end, *mat_list],
                    ).fetchall()
                    for r in sig_rows:
                        m = str(r["material"]).strip()
                        stype = str(r["signal_type"] or "").strip().upper()
                        sap_t = float(r["sap_total"] or 0.0)
                        wms_t = float(r["wms_total"] or 0.0)
                        t_gap = float(r["total_gap"] or (wms_t - sap_t))
                        if stype in ("CHAIN_GAP", "MISSING_GR_SUSPECT", "TOTAL_MISMATCH") and (
                            wms_t > sap_t + 1e-6 or t_gap > 1e-6
                        ):
                            ev = evidence[m]
                            ev["gap_7d_wms_gt_sap_count"] += 1
                            if stype not in ev["gap_7d_types"]:
                                ev["gap_7d_types"].append(stype)
                            ev["gap_7d_max_gap"] = max(ev["gap_7d_max_gap"], max(t_gap, wms_t - sap_t))

                # 4. Earliest seen timestamp in gi_error_snapshots per (material, mvt, from_sloc, to_sloc, qty)
                first_rows = conn.execute(
                    f"""
                    SELECT material, mvt, from_sloc, to_sloc, ROUND(qty, 3) AS rqty,
                           MIN(datetime(extracted_at, 'localtime')) AS first_seen_edt,
                           MIN(error_date) AS min_error_date
                    FROM gi_error_snapshots
                    WHERE datetime(extracted_at, 'localtime') <= ?
                      AND material IN ({placeholders})
                    GROUP BY material, mvt, from_sloc, to_sloc, ROUND(qty, 3)
                    """,
                    [as_of_str, *mat_list],
                ).fetchall()
                for r in first_rows:
                    m = str(r["material"]).strip()
                    sig = (
                        str(r["mvt"] or "").strip(),
                        str(r["from_sloc"] or "").strip().upper(),
                        str(r["to_sloc"] or "").strip().upper(),
                        round(float(r["rqty"] or 0.0), 3),
                    )
                    evidence[m]["first_seen_by_sig"][sig] = {
                        "first_seen_edt": str(r["first_seen_edt"] or ""),
                        "min_error_date": str(r["min_error_date"] or ""),
                    }
        except Exception as exc:
            logger.warning(f"[GI_VERDICT] Error loading GR/gap evidence from GI DB: {exc}")

    # 5. Optional 7-day MB51 101/131 pattern check from ops_raw.db (read-only)
    if ops_raw_db_path and Path(ops_raw_db_path).exists():
        try:
            with _connect_ro(Path(ops_raw_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                tables = {
                    r[0]
                    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                if "mb51_raw" in tables:
                    max_row = conn.execute("SELECT MAX(posting_date), MAX(entry_date) FROM mb51_raw").fetchone()
                    max_cpudt = str((max_row[1] or max_row[0] or "") if max_row else "")
                    d_start = (as_of_dt.date() - timedelta(days=7)).strftime("%Y-%m-%d")
                    d_end = as_of_dt.date().strftime("%Y-%m-%d")
                    mb_rows = conn.execute(
                        f"""
                        SELECT material, mvt_type, sloc, ABS(qty) AS abs_qty, posting_date, entry_date
                        FROM mb51_raw
                        WHERE material IN ({placeholders})
                          AND mvt_type IN ('101', '131', '311')
                          AND COALESCE(entry_date, posting_date) BETWEEN ? AND ?
                        """,
                        [*mat_list, d_start, d_end],
                    ).fetchall()
                    for m in materials:
                        evidence[m]["mb51_available"] = True
                        evidence[m]["mb51_max_cpudt"] = max_cpudt
                    for r in mb_rows:
                        m = str(r["material"]).strip()
                        if m in evidence:
                            evidence[m]["mb51_7d_101_131_rows"].append(dict(r))
        except Exception as exc:
            logger.debug(f"[GI_VERDICT] Optional MB51 lookup skipped: {exc}")

    return evidence


def _format_stock_summary(sap_by_sloc: Dict[str, float], wms_by_sloc: Dict[str, float]) -> str:
    all_slocs = sorted(
        set(sap_by_sloc.keys()) | set(wms_by_sloc.keys()),
        key=lambda s: (0 if s in BALANCE_WAREHOUSE_SLOCS else 1, s),
    )
    parts = []
    for s in all_slocs:
        sv = sap_by_sloc.get(s, 0.0)
        wv = wms_by_sloc.get(s, 0.0)
        if abs(sv) > 1e-6 or abs(wv) > 1e-6:
            parts.append(f"{s} SAP {sv:,.0f}/WMS {wv:,.0f}")
    return ", ".join(parts) if parts else "All SLocs SAP 0 / WMS 0"


def _load_historical_stock_by_mat_as_of(
    as_of_dt: datetime,
    materials: Set[str],
    gi_dir: Path = DEFAULT_GI_DIR,
) -> Dict[str, Tuple[Dict[str, float], Dict[str, float]]]:
    """
    Loads historical MB52_*.XLS and WMS_StockList_HL_*.xlsx closest to `as_of_dt`
    when `SAP_By_SLoc` / `WMS_By_SLoc` is absent from pre-D100 GI_Resolution_Plan files.
    """
    out: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}
    if not materials or not gi_dir or not Path(gi_dir).exists():
        return out

    def _find_closest_le(folder: Path, pattern: str) -> Optional[Path]:
        if not folder.exists():
            return None
        cands: List[Tuple[datetime, Path]] = []
        for f in folder.glob(pattern):
            m = re.search(r"(\d{8})_(\d{6})", f.name)
            if m:
                try:
                    fts = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
                    if fts <= (as_of_dt + timedelta(minutes=3)) and fts.date() == as_of_dt.date():
                        cands.append((fts, f))
                except ValueError:
                    pass
        cands.sort(key=lambda x: x[0], reverse=True)
        return cands[0][1] if cands else None

    mb52_path = _find_closest_le(Path(gi_dir) / "mb52", "MB52_*.XLS*")
    wms_path = _find_closest_le(Path(gi_dir) / "wms_parts_stack", "WMS_StockList_*.xlsx")
    if not mb52_path and not wms_path:
        return out

    sap_maps: Dict[str, Dict[str, float]] = {m: {} for m in materials}
    wms_maps: Dict[str, Dict[str, float]] = {m: {} for m in materials}

    if mb52_path:
        try:
            try:
                df_m = pd.read_csv(mb52_path, sep="\t", encoding="utf-16")
                if len(df_m.columns) <= 2:
                    df_m = pd.read_html(str(mb52_path))[0]
            except Exception:
                df_m = pd.read_html(str(mb52_path))[0]
            cols = [str(c).strip() for c in df_m.columns]
            df_m.columns = cols
            mc = next((c for c in cols if "Material" in c and "Desc" not in c), None)
            sc = next((c for c in cols if "SLoc" in c or "Storage" in c), None)
            uc = next((c for c in cols if "Unrestricted" in c), None)
            if mc and sc and uc:
                sub = df_m[[mc, sc, uc]].dropna()
                sub.columns = ["Material", "SLoc", "SAP_Stock"]
                sub["Material"] = sub["Material"].astype(str).str.strip()
                sub = sub[sub["Material"].isin(materials)]
                for _, r in sub.iterrows():
                    m_key = str(r["Material"]).strip()
                    s_key = str(r["SLoc"]).strip().upper()
                    v = pd.to_numeric(str(r["SAP_Stock"]).replace(",", "").strip(), errors="coerce")
                    sap_maps.setdefault(m_key, {})[s_key] = sap_maps.get(m_key, {}).get(s_key, 0.0) + float(
                        0.0 if pd.isna(v) else v
                    )
        except Exception as exc:
            logger.debug(f"[GI_VERDICT] Historical MB52 parse skipped ({mb52_path.name}): {exc}")

    if wms_path:
        try:
            import sys as _sys
            hq_dir = INVENTORY_DEPT_DIR.parent / "00_HQ_TaskManager"
            if str(hq_dir) not in _sys.path:
                _sys.path.insert(0, str(hq_dir))
            from wms_mapping import map_wms_gi_loc_to_sap

            df_w = pd.read_excel(wms_path)
            mc = next((c for c in df_w.columns if "Mat.No" in c or "Material" in c), None)
            lc = next((c for c in df_w.columns if "GI Loc" in c or "SLoc" in c), None)
            qc = next((c for c in df_w.columns if "Qty" in c), None)
            if mc and lc and qc:
                sub_w = df_w[[mc, lc, qc]].dropna()
                sub_w.columns = ["Material", "Raw_Loc", "WMS_Stock"]
                sub_w["Material"] = sub_w["Material"].astype(str).str.strip()
                sub_w = sub_w[sub_w["Material"].isin(materials)]
                if not sub_w.empty:
                    sub_w = sub_w.copy()
                    sub_w["SLoc"] = sub_w["Raw_Loc"].apply(map_wms_gi_loc_to_sap)
                    sub_w = sub_w.dropna(subset=["SLoc"])
                    for _, r in sub_w.iterrows():
                        m_key = str(r["Material"]).strip()
                        s_key = str(r["SLoc"]).strip().upper()
                        v = pd.to_numeric(r["WMS_Stock"], errors="coerce")
                        wms_maps.setdefault(m_key, {})[s_key] = wms_maps.get(m_key, {}).get(s_key, 0.0) + float(
                            0.0 if pd.isna(v) else v
                        )
        except Exception as exc:
            logger.debug(f"[GI_VERDICT] Historical WMS parse skipped ({wms_path.name}): {exc}")

    for m in materials:
        out[m] = (sap_maps.get(m, {}), wms_maps.get(m, {}))
    return out


def evaluate_gi_errors_as_of(
    as_of: Optional[Any] = None,
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
    exact_batch_id: Optional[str] = None,
    active_gi_records: Optional[List[Dict[str, Any]]] = None,
    fallback_stock_by_mat: Optional[Dict[str, Tuple[Dict[str, float], Dict[str, float]]]] = None,
    drafted_keys: Optional[Set[Tuple[str, str, str, str, float]]] = None,
    override_plan_df: Optional[pd.DataFrame] = None,
    fresh_gr_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Evaluates all active GI errors as of `as_of` and returns structured verdicts for both D108 and D107.

    If `active_gi_records` is supplied (e.g., fresh ZTCODE_STOCK_REC GI errors extracted by 02_JOB at 08:16 or D107 at 07:20),
    each record in `active_gi_records` is evaluated against the preceding 01 cycle (`<= as_of`), enabling
    detection of `① 신규 에러` (created after 01's collection timestamp) and excluding errors already cleared by 01!
    Otherwise, evaluates the GI errors from the preceding 01 cycle's `gi_error_snapshots`.
    """
    as_of_dt = _parse_as_of_dt(as_of)
    ref_date = as_of_dt.date()
    drafted_set = drafted_keys or set()

    cycle = find_preceding_gi_cycle(
        as_of_dt=as_of_dt,
        gi_db_path=gi_db_path,
        gi_plan_dir=gi_plan_dir,
        exact_batch_id=exact_batch_id,
    )
    batch_id = cycle["batch_id"]
    plan_df: pd.DataFrame = override_plan_df if override_plan_df is not None else cycle["plan_df"]
    plan_time_edt = cycle["plan_time_edt"] or cycle["run_time_edt"] or ""
    gi_snaps_01 = cycle["gi_snapshots"]
    postings_01 = cycle["postings"]

    # Build signature set from 01's snapshot (or fresh plan when override_plan_df is supplied)
    snap_01_sigs: Set[Tuple[str, str, str, str, float]] = set()
    snap_01_mats: Set[str] = set()
    sig_source = (active_gi_records if (override_plan_df is not None and active_gi_records is not None) else gi_snaps_01)
    for s in sig_source:
        mat_s = str(s.get("material") or s.get("Material") or "").strip()
        sig = (
            mat_s,
            str(s.get("mvt") or s.get("Mvt_Original") or "").strip(),
            str(s.get("from_sloc") or s.get("From_SLoc") or "").strip().upper(),
            str(s.get("to_sloc") or s.get("To_SLoc") or "").strip().upper(),
            round(float(s.get("qty") or s.get("Transfer_Qty") or 0.0), 3),
        )
        snap_01_sigs.add(sig)
        snap_01_mats.add(mat_s)

    target_records = active_gi_records if active_gi_records is not None else gi_snaps_01
    materials = {str(r.get("material") or r.get("Material") or "").strip() for r in target_records}
    materials.discard("")

    eff_fallback_stock: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = dict(fallback_stock_by_mat or {})
    has_plan_stock_cols = bool(
        plan_df is not None and not plan_df.empty and "SAP_By_SLoc" in plan_df.columns and "WMS_By_SLoc" in plan_df.columns
    )
    if not has_plan_stock_cols and not eff_fallback_stock and gi_plan_dir:
        hist_dt = (
            datetime.strptime(plan_time_edt, "%Y-%m-%d %H:%M:%S")
            if plan_time_edt
            else as_of_dt
        )
        eff_fallback_stock = _load_historical_stock_by_mat_as_of(
            as_of_dt=hist_dt,
            materials=materials,
            gi_dir=Path(gi_plan_dir).parent,
        )

    ev_by_mat = _load_gr_and_gap_evidence(
        materials=materials,
        as_of_dt=as_of_dt,
        current_batch_id=batch_id,
        gi_db_path=gi_db_path,
        ops_raw_db_path=ops_raw_db_path,
    )
    if fresh_gr_records is not None:
        for gm_reset in ev_by_mat:
            ev_by_mat[gm_reset]["gr_active_count"] = 0
            ev_by_mat[gm_reset]["gr_active_qty"] = 0.0
            ev_by_mat[gm_reset]["gr_active_vendors"] = []
            ev_by_mat[gm_reset]["gr_active_dates"] = []
        for gr in fresh_gr_records:
            gm = str(gr.get("material") or gr.get("Material") or gr.get("Material Number") or "").strip()
            if gm in ev_by_mat:
                gq = float(gr.get("qty") or gr.get("Qty") or 0.0)
                gv = str(gr.get("vendor") or gr.get("Vendor") or "").strip()
                gd = str(gr.get("error_date") or gr.get("Error_Date") or gr.get("GR Date") or "").strip()
                ev_by_mat[gm]["gr_active_count"] = int(ev_by_mat[gm].get("gr_active_count", 0)) + 1
                ev_by_mat[gm]["gr_active_qty"] = float(ev_by_mat[gm].get("gr_active_qty", 0.0)) + gq
                ev_by_mat[gm]["gr_7d_count"] = max(1, int(ev_by_mat[gm].get("gr_7d_count", 0)))
                if gd and not ev_by_mat[gm].get("gr_7d_last_date"):
                    ev_by_mat[gm]["gr_7d_last_date"] = gd
                if gv and gv not in ev_by_mat[gm]["gr_active_vendors"]:
                    ev_by_mat[gm]["gr_active_vendors"].append(gv)
                if gd and gd not in ev_by_mat[gm]["gr_active_dates"]:
                    ev_by_mat[gm]["gr_active_dates"].append(gd)

    # Index 01 postings by material
    posts_by_mat: Dict[str, List[Dict[str, Any]]] = {}
    for p in postings_01:
        pm = str(p.get("material") or "").strip()
        posts_by_mat.setdefault(pm, []).append(p)

    # #187 B-3: Count occurrences per identical ZTCODE_GI_ERR signature (mat, mvt, from, to, qty, error_date)
    sig6_totals: Dict[Tuple[str, str, str, str, float, str], int] = {}
    for _r in target_records:
        _k6 = (
            str(_r.get("material") or _r.get("Material") or "").strip(),
            str(_r.get("mvt") or _r.get("Mvt") or "").strip(),
            str(_r.get("from_sloc") or _r.get("From_SLoc") or "").strip().upper(),
            str(_r.get("to_sloc") or _r.get("To_SLoc") or "").strip().upper(),
            round(float(_r.get("qty") or _r.get("Qty") or 0.0), 3),
            str(_r.get("error_date") or _r.get("Error_Date") or "").strip(),
        )
        sig6_totals[_k6] = sig6_totals.get(_k6, 0) + 1
    sig6_seen: Dict[Tuple[str, str, str, str, float, str], int] = {}
    mat_route_gi_qty: Dict[Tuple[str, str, str], float] = {}
    for _r in target_records:
        _rmat = str(_r.get("material") or _r.get("Material") or "").strip()
        _rmvt = str(_r.get("mvt") or _r.get("Mvt") or "").replace(".0", "").strip()
        _rfrom = str(_r.get("from_sloc") or _r.get("From_SLoc") or "").strip().upper()
        _rto = str(_r.get("to_sloc") or _r.get("To_SLoc") or "").strip().upper()
        _rqty = float(_r.get("qty") or _r.get("Qty") or 0.0)
        _eff_s = _rto if _rmvt == "312" else _rfrom
        _eff_d = _rfrom if _rmvt == "312" else _rto
        mat_route_gi_qty[(_rmat, _eff_s, _eff_d)] = mat_route_gi_qty.get((_rmat, _eff_s, _eff_d), 0.0) + _rqty
    _d109_prior_cache: Optional[List[Dict[str, Any]]] = None

    verdicts: List[Dict[str, Any]] = []
    for idx, rec in enumerate(target_records):
        mat = str(rec.get("material") or rec.get("Material") or "").strip()
        mat_desc = str(rec.get("material_desc") or rec.get("Material_Desc") or rec.get("Desc") or "").strip()
        mvt = str(rec.get("mvt") or rec.get("Mvt") or "").strip()
        from_sloc = str(rec.get("from_sloc") or rec.get("From_SLoc") or "").strip().upper()
        to_sloc = str(rec.get("to_sloc") or rec.get("To_SLoc") or "").strip().upper()
        qty = float(rec.get("qty") or rec.get("Qty") or 0.0)
        error_date = str(rec.get("error_date") or rec.get("Error_Date") or "").strip()
        vendor = str(rec.get("vendor") or rec.get("Vendor") or "").strip()
        msg = str(rec.get("message") or rec.get("Message") or "").strip()

        sig_5 = (mat, mvt, from_sloc, to_sloc, round(qty, 3))
        sig_6 = (mat, mvt, from_sloc, to_sloc, round(qty, 3), error_date)
        sig6_seen[sig_6] = sig6_seen.get(sig_6, 0) + 1
        line_occ = sig6_seen[sig_6]
        line_occ_total = sig6_totals.get(sig_6, 1)
        error_key = f"{mat}|{mvt}|{from_sloc}|{to_sloc}|{qty:.3f}|{error_date or '-'}|#{line_occ}" 
        sig_4 = (mvt, from_sloc, to_sloc, round(qty, 3))
        ev = ev_by_mat.get(mat, {})
        first_info = ev.get("first_seen_by_sig", {}).get(sig_4, {})
        first_seen_edt = first_info.get("first_seen_edt") or cycle.get("extracted_at_edt") or ""

        # Check if newly appeared after 01 cycle collection
        is_new_after_01 = bool(
            active_gi_records is not None
            and gi_snaps_01
            and sig_5 not in snap_01_sigs
            and mat not in snap_01_mats
        )

        # Match rows in 01 GI_Resolution_Plan
        mat_plan = (
            plan_df[plan_df["Material"] == mat]
            if (plan_df is not None and not plan_df.empty and "Material" in plan_df.columns)
            else pd.DataFrame()
        )
        matched_plan_rows: List[Dict[str, Any]] = []
        if not mat_plan.empty:
            for _, pr in mat_plan.iterrows():
                pr_mvt = str(pr.get("Mvt_Original") or "").replace(".0", "").strip()
                if not mvt or not pr_mvt or pr_mvt == mvt:
                    matched_plan_rows.append(pr.to_dict())
            if not matched_plan_rows:
                matched_plan_rows = [pr.to_dict() for _, pr in mat_plan.iterrows()]

        # Choose primary 01 status row & companion notes (e.g. WMS_ZERO_NOT_PERSISTENT alongside INSUFFICIENT_SURPLUS)
        primary_plan: Optional[Dict[str, Any]] = None
        companion_statuses: List[str] = []
        if matched_plan_rows:
            non_flush = [
                r for r in matched_plan_rows if str(r.get("Status") or "") != "WMS_ZERO_NOT_PERSISTENT"
            ]
            if non_flush:
                # Prefer exact Transfer_Qty == qty or Error_Qty == qty if multiple
                exact_q = [
                    r
                    for r in non_flush
                    if abs(float(r.get("Error_Qty") or r.get("Transfer_Qty") or 0.0) - qty) < 1e-3
                ]
                primary_plan = exact_q[0] if exact_q else non_flush[0]
            else:
                primary_plan = matched_plan_rows[0]
            for r in matched_plan_rows:
                st = str(r.get("Status") or "").strip()
                if primary_plan is not None and r is not primary_plan and st and st not in companion_statuses:
                    companion_statuses.append(st)

        plan_status = str(primary_plan.get("Status") or "NOT_IN_01_PLAN").strip() if primary_plan else "NOT_IN_01_PLAN"
        plan_action = str(primary_plan.get("Action") or "").strip() if primary_plan else ""
        plan_reason = str(primary_plan.get("Reason") or "").strip() if primary_plan else ""
        plan_from = (
            ""
            if (primary_plan is None or pd.isna(primary_plan.get("From_SLoc")))
            else str(primary_plan.get("From_SLoc") or "").strip().upper()
        )
        plan_to = (
            ""
            if (primary_plan is None or pd.isna(primary_plan.get("To_SLoc")))
            else str(primary_plan.get("To_SLoc") or "").strip().upper()
        )
        plan_tqty = float(primary_plan.get("Transfer_Qty") or 0.0) if primary_plan else 0.0
        deficit_loc = (
            str(primary_plan.get("Deficit_Loc") or "").strip().upper()
            if (primary_plan and not pd.isna(primary_plan.get("Deficit_Loc")))
            else (to_sloc if mvt == "312" else from_sloc)
        )

        # Parse SAP / WMS stock dicts
        sap_by_sloc = _safe_parse_dict(primary_plan.get("SAP_By_SLoc")) if primary_plan else {}
        wms_by_sloc = _safe_parse_dict(primary_plan.get("WMS_By_SLoc")) if primary_plan else {}
        if not sap_by_sloc and not wms_by_sloc and eff_fallback_stock and mat in eff_fallback_stock:
            sap_by_sloc, wms_by_sloc = eff_fallback_stock[mat]

        from_sap = sap_by_sloc.get(from_sloc, 0.0)
        from_wms = wms_by_sloc.get(from_sloc, 0.0)
        def_sap = sap_by_sloc.get(deficit_loc, 0.0)
        def_wms = wms_by_sloc.get(deficit_loc, 0.0)
        to_sap = sap_by_sloc.get(to_sloc, 0.0)
        to_wms = wms_by_sloc.get(to_sloc, 0.0)
        plant_sap_total = sum(sap_by_sloc.values())
        plant_wms_total = sum(wms_by_sloc.values())

        wh_wms_excess_by_sloc = {
            s: wms_by_sloc.get(s, 0.0) - sap_by_sloc.get(s, 0.0)
            for s in BALANCE_WAREHOUSE_SLOCS
            if (wms_by_sloc.get(s, 0.0) - sap_by_sloc.get(s, 0.0)) > 1e-6
        }
        wh_sap_surplus_by_sloc = {
            s: sap_by_sloc.get(s, 0.0) - wms_by_sloc.get(s, 0.0)
            for s in BALANCE_WAREHOUSE_SLOCS
            if (sap_by_sloc.get(s, 0.0) - wms_by_sloc.get(s, 0.0)) > 1e-6
        }
        stock_summary = _format_stock_summary(sap_by_sloc, wms_by_sloc)

        # Posting history check for this material in preceding 01 cycle
        mat_posts = [
            p for p in posts_by_mat.get(mat, [])
            if str(p.get("posting_type") or "").upper() not in ("ROLLBACK_D113", "ROLLBACK_D115")
        ]
        failed_posts = [
            p
            for p in mat_posts
            if str(p.get("post_status") or "").upper() in ("ROLLED_BACK_D113", "ROLLBACK_FAILED_D113", "ROLLED_BACK_D115", "ROLLBACK_FAILED_D115", "FAILED_STEP1")
            or (
                str(p.get("post_status") or "").upper() not in ("SUCCESS", "POSTED")
                and not str(p.get("doc_number") or "").strip().startswith("49")
            )
        ]
        success_posts = [
            p
            for p in mat_posts
            if str(p.get("post_status") or "").upper() not in ("ROLLED_BACK_D113", "ROLLBACK_FAILED_D113", "ROLLED_BACK_D115", "ROLLBACK_FAILED_D115", "FAILED_STEP1")
            and (
                str(p.get("post_status") or "").upper() in ("SUCCESS", "POSTED")
                or str(p.get("doc_number") or "").strip().startswith("49")
            )
        ]

        # Check MB51 7-day 101/131 -> same-qty 311 unposted pattern
        mb51_rows = ev.get("mb51_7d_101_131_rows", [])
        gr_101_131_qtys = [float(r["abs_qty"]) for r in mb51_rows if str(r["mvt_type"]) in ("101", "131")]
        mvt_311_qtys = [float(r["abs_qty"]) for r in mb51_rows if str(r["mvt_type"]) == "311"]
        has_mb51_same_qty_gr_untransferred = any(abs(q - qty) < 1e-3 for q in gr_101_131_qtys) and not any(
            abs(q - qty) < 1e-3 for q in mvt_311_qtys
        )

        # =====================================================================
        # PART A: D108 Classification (#183)
        # =====================================================================
        d108_code = "D"
        d108_label = _EN["D108_D"]
        d108_severity = _EN["SEV_CRITICAL"]
        d108_anomaly_type = ""
        is_bug_candidate = False
        d108_detail = ""

        is_excluded_mvt = (
            mvt not in D107_TARGET_MVTS
            or plan_status.startswith("EXCLUDED_MVT")
            or plan_status.startswith("EXCLUDED_")
        )

        # Upstream root check for CHAIN_CASCADE_RESOLVE
        upstream_guard_row = None
        if plan_status == "CHAIN_CASCADE_RESOLVE" and matched_plan_rows:
            for r in matched_plan_rows:
                if str(r.get("Status") or "") != "CHAIN_CASCADE_RESOLVE":
                    upstream_guard_row = r
                    break

        if is_excluded_mvt:
            d108_code = "EXCLUDED_MVT"
            d108_label = f"GI error (excluded mvt {mvt or '?'}) — human review"
            d108_severity = _EN["SEV_EXCLUDED"]
            d108_detail = plan_reason or f"Movement type {mvt} excluded from automated transfer posting"

        elif (is_new_after_01 or plan_status == "NOT_IN_01_PLAN") and int(ev.get("gr_active_count", 0)) == 0:
            d108_code = "ANOMALY_NEW_AFTER_01"
            d108_label = _EN["D108_ANOMALY_NEW_AFTER_01"]
            d108_severity = _EN["SEV_TEMPORARY"]
            d108_anomaly_type = "NEW_AFTER_01"
            is_bug_candidate = False
            d108_detail = (
                f"New GI error created after prior 01 cycle ({batch_id or 'N/A'}, {plan_time_edt or 'N/A'}) — "
                f"awaiting next 01 cycle verdict ({stock_summary})"
            )

        elif plan_action == "TRANSFER_POSTING":
            if failed_posts:
                fail_msg = str(failed_posts[0].get("sap_response") or failed_posts[0].get("post_status") or "SAP posting error")
                d108_code = "ANOMALY_POSTING_FAILED"
                d108_label = _EN["D108_ANOMALY_POSTING_FAILED"]
                d108_severity = _EN["SEV_CHECK_BUG"]
                d108_anomaly_type = "POSTING_FAILED"
                is_bug_candidate = True
                d108_detail = f"SAP posting failed after 01 approval ({plan_from}→{plan_to} {plan_tqty:,.0f} EA): {fail_msg}"
            elif success_posts:
                docs = ", ".join(str(p.get("doc_number") or "") for p in success_posts if p.get("doc_number"))
                d108_code = "ANOMALY_POSTED_BUT_REMAINED"
                d108_label = _EN["D108_ANOMALY_POSTED_BUT_REMAINED"]
                d108_severity = _EN["SEV_CHECK_BUG"]
                d108_anomaly_type = "POSTED_BUT_REMAINED"
                is_bug_candidate = True
                d108_detail = f"01 transfer posted (Doc: {docs}) yet error remained in ZTCODE_STOCK_REC after reprocessing ({stock_summary})"
            elif plan_status in ("ROUNDTRIP_CLEAR_VIA_DEST_D113", "DEST_PUMP_CLEAR_D115"):
                _tag_rt = "D115" if plan_status == "DEST_PUMP_CLEAR_D115" else "D113"
                d108_code = f"RESOLVABLE_{_tag_rt}_NEXT_RUN"
                d108_label = f"resolvable — {_tag_rt} next run"
                d108_severity = _EN["SEV_TEMPORARY"]
                d108_anomaly_type = ""
                is_bug_candidate = False
                d108_detail = (
                    f"resolvable — {_tag_rt} next run: {plan_from}->{plan_to} {plan_tqty:,.0f} EA "
                    f"(3-file GR=0, same-cycle clearance | {stock_summary})"
                )
            else:
                d108_code = "ANOMALY_GUARD_UNPOSTED"
                d108_label = _EN["D108_ANOMALY_APPROVED_UNPOSTED"]
                d108_severity = _EN["SEV_CHECK_BUG"]
                d108_anomaly_type = "APPROVED_NOT_EXECUTED"
                is_bug_candidate = True
                d108_detail = f"01 planned TRANSFER_POSTING ({plan_from}→{plan_to} {plan_tqty:,.0f} EA) with no execution record: {plan_reason}"

        elif (
            plan_status == "CHAIN_CASCADE_RESOLVE"
            and upstream_guard_row is not None
            and str(upstream_guard_row.get("Status") or "").strip() in ("ROUNDTRIP_CLEAR_VIA_DEST_D113", "DEST_PUMP_CLEAR_D115")
            and not failed_posts
            and not success_posts
        ):
            up_fr_d113 = str(upstream_guard_row.get("From_SLoc") or "")
            up_to_d113 = str(upstream_guard_row.get("To_SLoc") or "")
            up_qt_d113 = float(upstream_guard_row.get("Transfer_Qty") or 0.0)
            d108_code = "RESOLVABLE_D113_NEXT_RUN"
            d108_label = "resolvable — D113 next run"
            d108_severity = _EN["SEV_TEMPORARY"]
            d108_anomaly_type = ""
            is_bug_candidate = False
            d108_detail = (
                f"resolvable — D113 next run: upstream {up_fr_d113}->{up_to_d113} {up_qt_d113:,.0f} EA "
                f"cascades {from_sloc}->{to_sloc} {qty:,.0f} EA ({stock_summary})"
            )
        elif plan_status == "SKIPPED_D109_ALREADY_RELEASED" or (
            plan_status == "CHAIN_CASCADE_RESOLVE"
            and upstream_guard_row is not None
            and str(upstream_guard_row.get("Status") or "").strip() == "SKIPPED_D109_ALREADY_RELEASED"
        ):
            up_rs_rel = (
                str(upstream_guard_row.get("Reason") or "")
                if (plan_status == "CHAIN_CASCADE_RESOLVE" and upstream_guard_row is not None)
                else plan_reason
            )
            d108_code = "ANOMALY_POSTED_BUT_REMAINED"
            d108_label = _EN["D108_ANOMALY_D109_RESIDUAL"]
            d108_severity = _EN["SEV_CHECK_BUG_REVIEW"]
            d108_anomaly_type = "POSTED_BUT_REMAINED"
            is_bug_candidate = True
            d108_detail = f"D109 duplicate-posting safeguard blocked retry: {up_rs_rel} ({stock_summary})"

        elif plan_status in (
            "SKIPPED_PENDING_INTERFACE_SL00",
            "AUTO_CLEARED_BY_CURRENT_STOCK",
            "CHAIN_CASCADE_RESOLVE",
        ) or (
            plan_action == "SKIP"
            and plan_from
            and plan_tqty > 0
            and "GUARD" not in plan_status
            and not plan_status.startswith("PROTECTED_")
        ):
            # Case ②: 01 found a resolution path (or stock already existed) yet skipped posting via guard!
            eff_reason = plan_reason
            eff_route = f"{plan_from}→{plan_to} {plan_tqty:,.0f} EA" if plan_from else plan_status
            if plan_status == "CHAIN_CASCADE_RESOLVE" and upstream_guard_row is not None:
                up_st = str(upstream_guard_row.get("Status") or "")
                up_fr = str(upstream_guard_row.get("From_SLoc") or "")
                up_to = str(upstream_guard_row.get("To_SLoc") or "")
                up_qt = float(upstream_guard_row.get("Transfer_Qty") or 0.0)
                up_rs = str(upstream_guard_row.get("Reason") or "")
                eff_route = f"Upstream {up_fr}→{up_to} {up_qt:,.0f} EA ({up_st})"
                eff_reason = f"{plan_reason} | Upstream hold reason: {up_rs}"

            underlying_note = ""
            if wh_wms_excess_by_sloc or wh_sap_surplus_by_sloc:
                underlying_note = f" [Underlying stock: {stock_summary} — accompanied by Book mismatch (A)]"

            d108_code = "ANOMALY_GUARD_UNPOSTED"
            d108_label = _EN["D108_ANOMALY_GUARD_UNPOSTED"]
            d108_severity = _EN["SEV_CHECK_BUG_POLICY"]
            d108_anomaly_type = "GUARD_UNPOSTED"
            is_bug_candidate = True
            d108_detail = f"01 detected resolution path ({eff_route}) but held by guard: {eff_reason}{underlying_note}"

            # D109 (#189): 1~3회차 대기 건은 ANOMALY_GUARD_UNPOSTED 유지, >=4회차이면서 활성 GR 에러 때문에 보류된 건은 B로 분류
            d109_src_row = (
                upstream_guard_row
                if (plan_status == "CHAIN_CASCADE_RESOLVE" and upstream_guard_row is not None)
                else primary_plan
            )
            is_sl00_guard_hold = (
                plan_status == "SKIPPED_PENDING_INTERFACE_SL00"
                or (
                    plan_status == "CHAIN_CASCADE_RESOLVE"
                    and upstream_guard_row is not None
                    and str(upstream_guard_row.get("Status") or "").strip() == "SKIPPED_PENDING_INTERFACE_SL00"
                )
            )
            if is_sl00_guard_hold:
                sl00_consec = 0
                raw_consec = d109_src_row.get("D109_Consec_Runs") if d109_src_row else None
                if raw_consec is not None and not pd.isna(raw_consec):
                    try:
                        sl00_consec = int(float(raw_consec))
                    except (TypeError, ValueError):
                        sl00_consec = 0
                if sl00_consec <= 0 and gi_db_path and Path(gi_db_path).exists():
                    try:
                        if _d109_prior_cache is None:
                            import importlib.util as _ilu_d109
                            _res_p = Path(__file__).resolve().parent / "01_JOB_GR_GI_Error" / "gr_gi_resolver.py"
                            _sp_d109 = _ilu_d109.spec_from_file_location("_d109_res", _res_p)
                            _mod_d109 = _ilu_d109.module_from_spec(_sp_d109)
                            _sp_d109.loader.exec_module(_mod_d109)
                            _d109_prior_cache = _mod_d109.load_d109_prior_completed_runs(
                                gi_db_path=Path(gi_db_path),
                                plan_dir=Path(gi_plan_dir) if gi_plan_dir else None,
                                before_ts=as_of_dt + timedelta(seconds=1),
                            )
                        _cnt = 0
                        for _pr in (_d109_prior_cache or []):
                            if mat not in (_pr.get("sl00_held_mats") or {}):
                                break
                            _cnt += 1
                        if override_plan_df is not None and (
                            not batch_id or not any(_pr.get("batch_id") == batch_id for _pr in (_d109_prior_cache or []))
                        ):
                            _cnt += 1
                        sl00_consec = max(1, _cnt)
                    except Exception:
                        sl00_consec = 1
                raw_gr_cnt = d109_src_row.get("D109_Active_GR_Count") if d109_src_row else None
                plan_gr_cnt = 0
                if raw_gr_cnt is not None and not pd.isna(raw_gr_cnt):
                    try:
                        plan_gr_cnt = int(float(raw_gr_cnt))
                    except (TypeError, ValueError):
                        plan_gr_cnt = 0
                eff_gr_cnt = max(int(ev.get("gr_active_count", 0) or 0), plan_gr_cnt)
                if sl00_consec >= D109_SL00_GUARD_RELEASE_RUNS and eff_gr_cnt > 0:
                    gr_qty_b = float(ev.get("gr_active_qty", 0.0) or 0.0)
                    gr_vnd_b = ",".join(ev.get("gr_active_vendors", [])) or vendor or "-"
                    gr_dts_b = ",".join(ev.get("gr_active_dates", [])) or "-"
                    d108_code = "B"
                    d108_label = _EN["D108_B"]
                    d108_severity = _EN["SEV_TEMPORARY"]
                    d108_anomaly_type = ""
                    is_bug_candidate = False
                    d108_detail = (
                        f"SL00 arrival hold persisted for {sl00_consec} consecutive cycles, but {eff_gr_cnt} active GR error(s) "
                        f"({gr_qty_b:,.0f} EA, Vendor {gr_vnd_b}, GR Date {gr_dts_b}) block D109 guard release — "
                        f"will auto-resolve once GR clears ({stock_summary})"
                    )
                elif eff_gr_cnt == 0:
                    # #211 D113 & #190 B-4 check for SL00 -> SL0x when GR == 0 and SL00 WMS == SAP
                    eff_src_sloc = to_sloc if mvt == "312" else from_sloc
                    eff_dst_sloc = from_sloc if mvt == "312" else to_sloc
                    sl00_sap_v = float(sap_by_sloc.get("SL00", 0.0))
                    sl00_wms_v = float(wms_by_sloc.get("SL00", 0.0))
                    dest_sap_v = float(sap_by_sloc.get(eff_dst_sloc, 0.0)) if eff_dst_sloc else 0.0
                    dest_gap_v = (
                        float(wms_by_sloc.get(eff_dst_sloc, 0.0)) - dest_sap_v
                        if eff_dst_sloc
                        else 0.0
                    )
                    req_gi_for_dest = max(qty, float(mat_route_gi_qty.get((mat, eff_src_sloc, eff_dst_sloc), qty)))
                    d113_route_ok = (
                        mvt in ("311", "312")
                        and eff_src_sloc == "SL00"
                        and eff_dst_sloc in ("SL01", "SL02", "SL03", "SL04")
                        and abs(sl00_wms_v - sl00_sap_v) <= 1e-6
                        and dest_sap_v >= qty - 1e-6
                    )
                    if d113_route_ok:
                        d108_code = "RESOLVABLE_D113_NEXT_RUN"
                        d108_label = "resolvable — D113 next run"
                        d108_severity = _EN["SEV_TEMPORARY"]
                        d108_anomaly_type = ""
                        is_bug_candidate = False
                        d108_detail = (
                            f"resolvable — D113 next run: SL00->{eff_dst_sloc} ({qty:,.0f} EA), "
                            f"SL00 WMS=SAP={sl00_sap_v:,.0f}, 3-file GR=0, {eff_dst_sloc} SAP={dest_sap_v:,.0f} EA ({stock_summary})"
                        )
                    elif (
                        sl00_consec >= D109_SL00_GUARD_RELEASE_RUNS
                        and eff_src_sloc == "SL00"
                        and eff_dst_sloc
                        and eff_dst_sloc != "SL00"
                        and abs(sl00_sap_v) <= 1e-6
                        and abs(sl00_wms_v) <= 1e-6
                        and dest_gap_v >= req_gi_for_dest - 1e-6
                    ):
                        d108_code = "F"
                        d108_label = _EN["D108_F_REVIEW"]
                        d108_severity = _EN["SEV_CRITICAL_REVIEW"]
                        d108_anomaly_type = ""
                        is_bug_candidate = False
                        d108_detail = (
                            f"[Human review · Verify SAP GR posting] SL00 arrival hold persisted {sl00_consec} cycles · "
                            f"3-file GR=0 · SL00 WMS=SAP=0 · Dest SLoc ({eff_dst_sloc}) WMS−SAP=+{dest_gap_v:,.0f} EA "
                            f"(>= GI {req_gi_for_dest:,.0f} EA) — physical stock arrived without SAP GR record ({stock_summary})"
                        )

        elif plan_status.startswith("PROTECTED_GR") or ev.get("gr_active_count", 0) > 0:
            gr_cnt = ev.get("gr_active_count", 0)
            gr_qty = ev.get("gr_active_qty", 0.0)
            gr_vnd = ",".join(ev.get("gr_active_vendors", [])) or vendor or "-"
            gr_dts = ",".join(ev.get("gr_active_dates", [])) or "-"
            d108_code = "B"
            d108_label = _EN["D108_B"]
            d108_severity = _EN["SEV_TEMPORARY"]
            if gr_cnt > 0:
                d108_detail = (
                    f"Linked to {gr_cnt} active GR error(s) ({gr_qty:,.0f} EA, Vendor {gr_vnd}, GR Date {gr_dts}) — "
                    f"01 will auto-process once GR clears ({stock_summary})"
                )
            else:
                d108_detail = f"01 GR-dependency hold ({plan_status}): {plan_reason} ({stock_summary})"

        else:
            # Insufficient stock / accuracy guard (INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD, WMS_ZERO_NOT_PERSISTENT, etc.)
            # Distinguish D (true physical shortage) vs F (suspected unposted GR) vs A (book discrepancy)
            has_physical_in_def_loc = def_wms >= qty - 1e-6
            has_any_wh_wms_excess = bool(wh_wms_excess_by_sloc)
            sl00_wms_excess = wh_wms_excess_by_sloc.get("SL00", 0.0)

            companion_note = ""
            if "WMS_ZERO_NOT_PERSISTENT" in companion_statuses:
                companion_note = (
                    f" [Note: {from_sloc} SAP residual {from_sap:,.0f} EA is eligible for partial D91 flush once persistence is confirmed, "
                    f"but insufficient to clear full GI error {qty:,.0f} EA]"
                )

            eff_src_else = to_sloc if mvt == "312" else from_sloc
            eff_dst_else = from_sloc if mvt == "312" else to_sloc
            sl00_sap_else = float(sap_by_sloc.get("SL00", 0.0))
            sl00_wms_else = float(wms_by_sloc.get("SL00", 0.0))
            dest_sap_else = float(sap_by_sloc.get(eff_dst_else, 0.0)) if eff_dst_else else 0.0
            is_d113_else = (
                mvt in ("311", "312")
                and eff_src_else == "SL00"
                and eff_dst_else in ("SL01", "SL02", "SL03", "SL04")
                and int(ev.get("gr_active_count", 0) or 0) == 0
                and abs(sl00_wms_else - sl00_sap_else) <= 1e-6
                and dest_sap_else >= qty - 1e-6
            )
            if is_d113_else:
                d108_code = "RESOLVABLE_D113_NEXT_RUN"
                d108_label = "resolvable — D113 next run"
                d108_severity = _EN["SEV_TEMPORARY"]
                d108_detail = (
                    f"resolvable — D113 next run: SL00->{eff_dst_else} ({qty:,.0f} EA), "
                    f"SL00 WMS=SAP={sl00_sap_else:,.0f}, 3-file GR=0, {eff_dst_else} SAP={dest_sap_else:,.0f} EA ({stock_summary}){companion_note}"
                )
            elif plant_sap_total <= 1e-6 and plant_wms_total > 1e-6:
                d108_code = "F"
                d108_label = _EN["D108_F"]
                d108_severity = _EN["SEV_CRITICAL"]
                d108_detail = (
                    f"Plant-wide SAP stock is 0 while physical WMS stock is {plant_wms_total:,.0f} EA ({stock_summary}) — "
                    f"suspected unposted SAP GR{companion_note}"
                )
            elif sl00_wms_excess > 1e-6 and def_sap < qty:
                d108_code = "F"
                d108_label = _EN["D108_F"]
                d108_severity = _EN["SEV_CRITICAL"]
                d108_detail = (
                    f"Receiving SLoc SL00 has physical WMS excess (+{sl00_wms_excess:,.0f} EA) with 0 active GR errors ({stock_summary}) — "
                    f"verify unposted GR or missing interface{companion_note}"
                )
            elif has_physical_in_def_loc or has_any_wh_wms_excess or def_wms > def_sap + 1e-6:
                d108_code = "A"
                d108_label = _EN["D108_A"]
                d108_severity = _EN["SEV_CRITICAL"]
                if has_physical_in_def_loc:
                    d108_detail = (
                        f"Deficit SLoc ({deficit_loc}) has physical WMS stock ({def_wms:,.0f} EA >= error {qty:,.0f} EA) "
                        f"but insufficient SAP book stock ({def_sap:,.0f} EA) and no eligible surplus elsewhere ({stock_summary}){companion_note}"
                    )
                else:
                    excess_str = ", ".join(f"{k} +{v:,.0f}" for k, v in sorted(wh_wms_excess_by_sloc.items()))
                    d108_detail = (
                        f"{deficit_loc} physical shortage (SAP {def_sap:,.0f}/WMS {def_wms:,.0f}, error {qty:,.0f} EA) and "
                        f"warehouse book mismatch ({excess_str}; {stock_summary}) blocked by accuracy guard{companion_note}"
                    )
            else:
                d108_code = "D"
                d108_label = _EN["D108_D"]
                d108_severity = _EN["SEV_CRITICAL"]
                d108_detail = (
                    f"Both SAP ({def_sap:,.0f} EA) and WMS ({def_wms:,.0f} EA) in {deficit_loc} are below error qty ({qty:,.0f} EA), "
                    f"with no eligible pump/roundtrip surplus plant-wide ({stock_summary}){companion_note}"
                )

        # =====================================================================
        # PART B: D107 Phase 1 Delete Candidate Evaluation (#180 · #181 · #182)
        # =====================================================================
        err_dt = _parse_sap_date(error_date)
        cond_0_mvt_ok = mvt in D107_TARGET_MVTS
        cond_1_prev_day_ok = bool(err_dt is not None and err_dt < ref_date)
        cond_2_01_insuff_ok = (
            plan_status in ("INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD", "SHORTAGE_DEFICIT_PRESERVED")
            and "WMS_ZERO_NOT_PERSISTENT" not in companion_statuses
            and d108_code != "RESOLVABLE_D113_NEXT_RUN"
            and not is_new_after_01
        )
        cond_3_no_gr_7d_ok = (
            ev.get("gr_active_count", 0) == 0
            and ev.get("gr_7d_count", 0) == 0
            and ev.get("gap_7d_wms_gt_sap_count", 0) == 0
            and not has_mb51_same_qty_gr_untransferred
        )
        max_wh_wms_excess = max(wh_wms_excess_by_sloc.values(), default=0.0)
        total_wh_wms_excess = sum(wh_wms_excess_by_sloc.values())
        cond_4_def_short_ok = def_sap < qty - 1e-6 and def_wms < qty - 1e-6
        cond_4_no_wms_excess_cover_ok = total_wh_wms_excess < qty - 1e-6 and max_wh_wms_excess < qty - 1e-6
        cond_4_no_physical_ok = cond_4_def_short_ok and cond_4_no_wms_excess_cover_ok

        # Ambiguity guard (#181 #6): if ANY warehouse has WMS > SAP (even < error_qty), hold for human check!
        has_ambiguous_wms_excess = bool(wh_wms_excess_by_sloc) or (plant_wms_total > plant_sap_total + 1e-6)

        # Condition 5: neither pumpable SAP>WMS surplus nor D97 roundtrip priming available
        rt_partner_sloc = from_sloc if mvt == "312" else to_sloc
        has_d97_roundtrip_sap = (
            rt_partner_sloc in BALANCE_WAREHOUSE_SLOCS
            and float(sap_by_sloc.get(rt_partner_sloc, 0.0)) >= qty - 1e-6
        )
        has_pump_sap_surplus = any(
            s != deficit_loc and v >= qty - 1e-6 for s, v in wh_sap_surplus_by_sloc.items()
        )
        cond_5_no_pump_ok = cond_2_01_insuff_ok and not has_pump_sap_surplus and not has_d97_roundtrip_sap
        cond_6_not_drafted_ok = (
            sig_5 not in drafted_set
            and error_key not in drafted_set
            and (mat, mvt, from_sloc, to_sloc, round(qty, 3), error_date, line_occ) not in drafted_set
        )

        excl_reasons: List[str] = []
        if not cond_0_mvt_ok:
            excl_reasons.append(f"[Cond 0 Excl] Non-target movement type (requires 311/312): mvt={mvt}")
        if not cond_1_prev_day_ok:
            excl_reasons.append(f"[Cond 1 Excl] Error date ({error_date or 'unknown'}) is not <= {(ref_date - timedelta(days=1)).isoformat()} (same-day error excluded)")
        if not cond_2_01_insuff_ok:
            if "WMS_ZERO_NOT_PERSISTENT" in companion_statuses:
                excl_reasons.append("[Cond 2 Hold] Accompanied by D91 WMS=0 1st-cycle flush wait (partial flush pending next cycle)")
            else:
                excl_reasons.append(f"[Cond 2 Excl] 01 verdict is not pure stock shortage: {plan_status}")
        if not cond_3_no_gr_7d_ok:
            if ev.get("gr_active_count", 0) > 0 or ev.get("gr_7d_count", 0) > 0:
                excl_reasons.append(
                    f"[Cond 3 Excl] 7-day GR error history exists (active {ev.get('gr_active_count', 0)} / "
                    f"7d cumulative {ev.get('gr_7d_count', 0)} rows · {ev.get('gr_7d_batches', 0)} runs, latest {ev.get('gr_7d_last_date', '-')})"
                )
            if ev.get("gap_7d_wms_gt_sap_count", 0) > 0:
                excl_reasons.append(
                    f"[Cond 3 Excl] 7-day GI_STOCK_GAP_SIGNALS (WMS>SAP) {ev.get('gap_7d_wms_gt_sap_count', 0)} hit(s) "
                    f"({','.join(ev.get('gap_7d_types', []))}, max +{ev.get('gap_7d_max_gap', 0.0):,.0f} EA)"
                )
            if has_mb51_same_qty_gr_untransferred:
                excl_reasons.append(f"[Cond 3 Excl] 7-day MB51 detected same-qty ({qty:,.0f} EA) 101/131 GR without subsequent 311 transfer")
        if not cond_4_no_physical_ok:
            if not cond_4_def_short_ok:
                excl_reasons.append(
                    f"[Cond 4 Excl] Deficit SLoc ({deficit_loc}) SAP ({def_sap:,.0f}) or WMS ({def_wms:,.0f}) >= error qty ({qty:,.0f})"
                )
            if not cond_4_no_wms_excess_cover_ok:
                ex_str = ", ".join(f"{k} +{v:,.0f}" for k, v in sorted(wh_wms_excess_by_sloc.items()))
                excl_reasons.append(
                    f"[Cond 4 Excl] Warehouse WMS>SAP physical excess ({ex_str}) >= error qty ({qty:,.0f}) — book mismatch / unposted GR stock exists"
                )
        if not cond_5_no_pump_ok and cond_2_01_insuff_ok:
            if has_d97_roundtrip_sap:
                excl_reasons.append(
                    f"[Cond 5 Excl] D97 roundtrip priming available ({rt_partner_sloc} SAP {float(sap_by_sloc.get(rt_partner_sloc, 0.0)):,.0f} >= error {qty:,.0f} EA)"
                )
            if has_pump_sap_surplus:
                excl_reasons.append(f"[Cond 5 Hold] Other SLoc SAP>WMS surplus exists ({wh_sap_surplus_by_sloc}) — verify pump/roundtrip eligibility")
        if not cond_6_not_drafted_ok:
            excl_reasons.append("[Cond 6 Excl] Already drafted for deletion")

        if (
            cond_0_mvt_ok
            and cond_1_prev_day_ok
            and cond_2_01_insuff_ok
            and cond_3_no_gr_7d_ok
            and cond_4_no_physical_ok
            and cond_5_no_pump_ok
            and cond_6_not_drafted_ok
        ):
            if has_ambiguous_wms_excess:
                d107_status = "HOLD_HUMAN_REVIEW"
                excess_desc = ", ".join(f"{k}: WMS>SAP +{v:,.0f} EA" for k, v in sorted(wh_wms_excess_by_sloc.items()))
                excl_reasons.append(
                    f"[Hold (Human review)] Warehouse WMS>SAP excess exists below error qty ({qty:,.0f} EA) ({excess_desc or stock_summary}) — check missing GR / book mismatch first"
                )
            else:
                d107_status = "CANDIDATE"
        elif (
            cond_0_mvt_ok
            and cond_1_prev_day_ok
            and cond_3_no_gr_7d_ok
            and cond_4_no_physical_ok
            and ("WMS_ZERO_NOT_PERSISTENT" in companion_statuses or (cond_2_01_insuff_ok and has_pump_sap_surplus and not has_d97_roundtrip_sap))
        ):
            d107_status = "HOLD_HUMAN_REVIEW"
        else:
            # #187 B-4: If ONLY Condition 3 failed, active GR == 0, no WMS>SAP gap / MB51 untransferred,
            # and most recent GR history date is >= 3 days prior to ref_date (e.g. MAT_STALE_GR: D, 3,000 EA,
            # active GR 0, last GR 10/23 = 6 days ago), surface in HOLD_HUMAN_REVIEW ("보류(사람 확인)")
            # without relaxing CANDIDATE criteria.
            last_gr_dt = _parse_sap_date(ev.get("gr_7d_last_date", ""))
            is_cond3_stale_gr_only = (
                cond_0_mvt_ok
                and cond_1_prev_day_ok
                and cond_2_01_insuff_ok
                and not cond_3_no_gr_7d_ok
                and cond_4_no_physical_ok
                and cond_5_no_pump_ok
                and cond_6_not_drafted_ok
                and not has_ambiguous_wms_excess
                and int(ev.get("gr_active_count", 0)) == 0
                and int(ev.get("gap_7d_wms_gt_sap_count", 0)) == 0
                and not has_mb51_same_qty_gr_untransferred
                and last_gr_dt is not None
                and (ref_date - last_gr_dt).days >= 3
            )
            if is_cond3_stale_gr_only:
                d107_status = "HOLD_HUMAN_REVIEW"
                days_ago = (ref_date - last_gr_dt).days
                ext_note = f", last snapshot {ev.get('gr_7d_last_ext_edt', '-')[:16]}" if ev.get("gr_7d_last_ext_edt") else ""
                excl_reasons.append(
                    f"[Hold (Human review)] Active GR errors = 0 · Latest GR date {ev.get('gr_7d_last_date', '-')} "
                    f"({days_ago}d ago >= 3d, 7d snapshots {ev.get('gr_7d_batches', 0)} runs{ext_note}) — "
                    f"all conditions except Cond 3 met (D: both physical & book short); human review for deletion"
                )
            else:
                d107_status = "EXCLUDED"

        if d108_code == "D" and d107_status == "CANDIDATE":
            d108_label = _EN["D108_D_D107"]

        remaining_diff_note = (
            f"Deletion removes GI error record only (0 stock change) · Residual stock unchanged: "
            f"From({from_sloc}) SAP {from_sap:,.0f}/WMS {from_wms:,.0f} (diff {from_sap - from_wms:+,.0f}), "
            f"To({to_sloc}) SAP {to_sap:,.0f}/WMS {to_wms:,.0f} | All [{stock_summary}]"
        )

        verdicts.append(
            {
                "seq": idx + 1,
                "material": mat,
                "material_desc": mat_desc,
                "mvt": mvt,
                "from_sloc": from_sloc,
                "to_sloc": to_sloc,
                "deficit_loc": deficit_loc,
                "qty": qty,
                "error_date": error_date,
                "vendor": vendor,
                "message": msg,
                "first_seen_edt": first_seen_edt,
                "error_key": error_key,
                "line_occ": line_occ,
                "line_occ_total": line_occ_total,
                "gi_link_summary": (
                    f"mvt {mvt} {from_sloc}→{to_sloc} {qty:,.0f} EA (Date {error_date or '-'})"
                    + (f" [#{line_occ}/{line_occ_total}]" if line_occ_total > 1 else "")
                ),
                "gi_01_batch_id": batch_id or "NONE",
                "gi_01_plan_time": plan_time_edt or "NONE",
                "gi_01_status": plan_status,
                "gi_01_companion_statuses": companion_statuses,
                "gi_01_action": plan_action,
                "gi_01_reason": plan_reason,
                "from_sap": from_sap,
                "from_wms": from_wms,
                "to_sap": to_sap,
                "to_wms": to_wms,
                "plant_sap_total": plant_sap_total,
                "plant_wms_total": plant_wms_total,
                "stock_summary": stock_summary,
                "gr_active_count": ev.get("gr_active_count", 0),
                "gr_active_qty": ev.get("gr_active_qty", 0.0),
                "gr_7d_count": ev.get("gr_7d_count", 0),
                "gap_7d_wms_gt_sap_count": ev.get("gap_7d_wms_gt_sap_count", 0),
                "mb51_max_cpudt": ev.get("mb51_max_cpudt", ""),
                "has_mb51_same_qty_gr_untransferred": has_mb51_same_qty_gr_untransferred,
                # D108 fields (#183)
                "d108_code": d108_code,
                "d108_label": d108_label,
                "d108_severity": d108_severity,
                "d108_anomaly_type": d108_anomaly_type,
                "is_bug_candidate": is_bug_candidate,
                "d108_detail": d108_detail,
                # D107 fields (#180 ~ #182)
                "d107_status": d107_status,
                "d107_cond_1_prev_day": cond_1_prev_day_ok,
                "d107_cond_2_01_insuff": cond_2_01_insuff_ok,
                "d107_cond_3_no_gr_7d": cond_3_no_gr_7d_ok,
                "d107_cond_4_no_physical": cond_4_no_physical_ok,
                "d107_cond_5_no_pump": cond_5_no_pump_ok,
                "d107_exclusion_reasons": excl_reasons,
                "d107_remaining_diff_note": remaining_diff_note,
                "d107_evidence": {
                    "cond_2_01_status": plan_status,
                    "cond_3_gr_7d": (
                        f"GR snapshots {ev.get('gr_7d_count', 0)} / GAP(WMS>SAP) {ev.get('gap_7d_wms_gt_sap_count', 0)} / "
                        f"MB51 same-qty untransferred {'YES' if has_mb51_same_qty_gr_untransferred else '0'}"
                    ),
                    "cond_4_source_stock": f"{deficit_loc} SAP {def_sap:,.0f} / WMS {def_wms:,.0f} (< {qty:,.0f} EA)",
                    "cond_5_no_pump": "Other SLoc SAP>WMS surplus 0 · D97 roundtrip unavailable" if cond_5_no_pump_ok else "Pump/roundtrip available",
                },
            }
        )

    return {
        "as_of_edt": as_of_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "ref_date": ref_date.isoformat(),
        "gi_01_batch_id": batch_id,
        "gi_01_run_time_edt": cycle["run_time_edt"],
        "gi_01_extracted_at_edt": cycle["extracted_at_edt"],
        "gi_01_plan_path": str(cycle["plan_path"]) if cycle["plan_path"] else None,
        "gi_01_plan_time_edt": plan_time_edt,
        "verdicts": verdicts,
    }


_D108_PRIORITY_RANK: Dict[str, int] = {
    "ANOMALY_POSTING_FAILED": 0,
    "ANOMALY_POSTED_BUT_REMAINED": 1,
    "ANOMALY_GUARD_UNPOSTED": 2,
    "ANOMALY_NEW_AFTER_01": 3,
    "B": 4,
    "F": 5,
    "A": 6,
    "D": 7,
    "EXCLUDED_MVT": 8,
}


def resolve_d108_for_backlog_item(
    material: str,
    target_sloc: str,
    verdicts_by_mat: Dict[str, List[Dict[str, Any]]],
    cand_sloc: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Matches a COGI backlog item `(material, target_sloc, cand_sloc)` to its overlapping GI error verdict(s)
    and returns the highest-priority D108 root-cause verdict with aggregated GI link summary.
    """
    mat = str(material or "").strip()
    tgt = str(target_sloc or "").strip().upper()
    cnd = str(cand_sloc or "").strip().upper()
    involved = {s for s in (tgt, cnd) if s and s != "NAN"}

    mat_verdicts = verdicts_by_mat.get(mat, [])
    if not mat_verdicts:
        return None

    overlapping = [
        v
        for v in mat_verdicts
        if not involved or ({v["from_sloc"], v["to_sloc"]} & involved)
    ]
    if involved and not overlapping:
        return None
    candidates = overlapping if overlapping else mat_verdicts
    candidates_sorted = sorted(
        candidates,
        key=lambda v: (_D108_PRIORITY_RANK.get(v["d108_code"], 99), -float(v.get("qty") or 0.0)),
    )
    chosen = dict(candidates_sorted[0])

    # Deduplicate GI link summaries across multiple identical or chained lines
    seen_links: List[str] = []
    for v in candidates:
        lnk = v["gi_link_summary"]
        if lnk not in seen_links:
            seen_links.append(lnk)
    if len(candidates) > 1 and len(seen_links) == 1:
        chosen["gi_link_summary"] = f"{seen_links[0]} ×{len(candidates)}"
    else:
        chosen["gi_link_summary"] = " ; ".join(seen_links)

    return chosen


def parse_ztcode_stock_rec_gi_records_detailed(gi_path: Optional[Path]) -> List[Dict[str, Any]]:
    """
    Parses a ZTCODE_GI_ERR GI_Error_*.XLS file into row-level GI error dicts via ztcode_stock_guard.parse_sap_utf16_list
    so newly created errors between 01's cycle and 02's cycle are detected as ① NEW_AFTER_01.
    """
    if not gi_path or not Path(gi_path).exists():
        return []
    p = Path(gi_path)
    try:
        hq_dir = INVENTORY_DEPT_DIR.parent / "00_HQ_TaskManager"
        import sys as _sys
        if str(hq_dir) not in _sys.path:
            _sys.path.insert(0, str(hq_dir))
        from ztcode_stock_guard import parse_sap_utf16_list

        df = parse_sap_utf16_list(p)
        if df.empty:
            return []

        df.columns = [str(c).strip() for c in df.columns]
        mat_col = next((c for c in df.columns if "Material Number" in c or c == "Material"), None)
        desc_col = next((c for c in df.columns if "Material Desc" in c or "Description" in c), None)
        mvt_col = next((c for c in df.columns if c == "Mvt" or "Mvt" in c or "Movement" in c), None)
        from_col = next((c for c in df.columns if c == "FromSt" or "From" in c), None)
        to_col = next((c for c in df.columns if c == "TOSt" or c.upper() == "TOST" or "To" in c), None)
        qty_col = next((c for c in df.columns if c == "Qty" or "Qty" in c or "Quantity" in c), None)
        date_col = next((c for c in df.columns if c == "Date" or "Date" in c), None)
        vnd_col = next((c for c in df.columns if "Vendor" in c or "Customer" in c), None)
        msg_col = next((c for c in df.columns if "Message" in c or "Error" in c or "Text" in c), None)

        if not mat_col or not qty_col:
            return []

        records: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            mat = str(r.get(mat_col) or "").strip()
            if not mat or mat.lower() in ("nan", "material", "*"):
                continue
            q_raw = str(r.get(qty_col) or "0").replace(",", "").strip()
            try:
                qty = abs(float(q_raw))
            except ValueError:
                continue
            if qty <= 0:
                continue
            mvt = str(r.get(mvt_col) or "").replace(".0", "").strip() if mvt_col else ""
            f_sloc = str(r.get(from_col) or "").strip().upper() if from_col else ""
            t_sloc = str(r.get(to_col) or "").strip().upper() if to_col else ""
            records.append(
                {
                    "material": mat,
                    "material_desc": "" if not desc_col or str(r.get(desc_col)) == "nan" else str(r.get(desc_col)).strip(),
                    "mvt": mvt,
                    "from_sloc": "" if f_sloc == "NAN" else f_sloc,
                    "to_sloc": "" if t_sloc == "NAN" else t_sloc,
                    "qty": qty,
                    "error_date": "" if not date_col or str(r.get(date_col)) == "nan" else str(r.get(date_col)).strip(),
                    "vendor": "" if not vnd_col or str(r.get(vnd_col)) == "nan" else str(r.get(vnd_col)).strip(),
                    "message": "" if not msg_col or str(r.get(msg_col)) == "nan" else str(r.get(msg_col)).strip(),
                }
            )
        return records
    except Exception as exc:
        logger.warning(f"[GI_VERDICT] Could not parse detailed GI file {p.name}: {exc}")
        return []


def parse_ztcode_stock_rec_gr_records_detailed(gr_path: Any) -> List[Dict[str, Any]]:
    """
    Parses and merges all fresh GR files (`GR_Error` + `GR_Domestics` + `GR_Import`) via
    `ztcode_stock_guard.parse_merged_gr_dataframe` (#187 B-1) into row-level dicts.
    """
    if not gr_path:
        return []
    try:
        hq_dir = INVENTORY_DEPT_DIR.parent / "00_HQ_TaskManager"
        import sys as _sys
        if str(hq_dir) not in _sys.path:
            _sys.path.insert(0, str(hq_dir))
        from ztcode_stock_guard import parse_merged_gr_dataframe

        df = parse_merged_gr_dataframe(gr_path)
        if df is None or df.empty:
            return []
        out: List[Dict[str, Any]] = []
        for _, r in df.iterrows():
            m = str(r.get("Material Number") or "").strip()
            if not m:
                continue
            out.append(
                {
                    "material": m,
                    "qty": float(r.get("Qty") or 0.0),
                    "vendor": str(r.get("Vendor") or "").strip(),
                    "error_date": str(r.get("GR Date") or "").strip(),
                    "stor": str(r.get("Stor") or "").strip().upper(),
                    "gr_type": str(r.get("GR_Type") or "").strip(),
                }
            )
        return out
    except Exception as exc:
        logger.warning(f"[GI_VERDICT] Could not parse detailed GR file(s) {gr_path}: {exc}")
        return []


def enrich_backlog_plan_with_d108(
    plan_df: pd.DataFrame,
    as_of: Optional[Any] = None,
    gi_path: Optional[Path] = None,
    gr_path: Optional[Any] = None,
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
    stock_matrix: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Enriches `BLOCKED_BY_GI_ERROR` rows in `02_JOB` `plan_df` with D108 root-cause columns
    WHILE KEEPING `Action == 'SKIP'` and `Status == 'BLOCKED_BY_GI_ERROR'` intact (D71 movement block preserved).
    """
    if plan_df is None or plan_df.empty:
        return plan_df

    df = plan_df.copy()
    df.attrs = dict(getattr(plan_df, "attrs", {}))

    d108_cols = (
        "D108_Cause_Code",
        "D108_Cause_Label",
        "D108_Severity",
        "D108_Linked_GI",
        "D108_01_Verdict",
        "D108_01_Plan_Time",
        "D108_Detail",
    )
    for col in d108_cols:
        if col not in df.columns:
            df[col] = ""
    if "D108_Is_Bug_Candidate" not in df.columns:
        df["D108_Is_Bug_Candidate"] = False

    mask_gi = df["Status"] == "BLOCKED_BY_GI_ERROR"
    if not mask_gi.any():
        return df

    fallback_stock: Dict[str, Tuple[Dict[str, float], Dict[str, float]]] = {}
    eff_matrix = stock_matrix if stock_matrix is not None else df.attrs.get("stock_matrix")
    if eff_matrix is not None and not eff_matrix.empty:
        for mat, grp in eff_matrix.groupby("Material"):
            s_map = {str(r["SLoc"]).upper(): float(r.get("SAP_Stock", 0.0)) for _, r in grp.iterrows()}
            w_map = {str(r["SLoc"]).upper(): float(r.get("WMS_Stock", 0.0)) for _, r in grp.iterrows()}
            fallback_stock[str(mat).strip()] = (s_map, w_map)

    has_explicit_gi_file = bool(gi_path and Path(gi_path).exists())
    active_gi_recs = parse_ztcode_stock_rec_gi_records_detailed(gi_path) if has_explicit_gi_file else None
    from ztcode_stock_guard import expand_gr_batch_paths
    gr_expanded = expand_gr_batch_paths(gr_path) if gr_path else []
    fresh_gr_recs = parse_ztcode_stock_rec_gr_records_detailed(gr_expanded) if gr_expanded else None

    eval_res = evaluate_gi_errors_as_of(
        as_of=as_of,
        gi_db_path=gi_db_path,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=ops_raw_db_path,
        active_gi_records=active_gi_recs,
        fallback_stock_by_mat=fallback_stock,
        fresh_gr_records=fresh_gr_recs,
    )

    verdicts_by_mat: Dict[str, List[Dict[str, Any]]] = {}
    for v in eval_res["verdicts"]:
        verdicts_by_mat.setdefault(v["material"], []).append(v)

    for idx in df[mask_gi].index:
        mat = str(df.at[idx, "Material"]).strip()
        tgt = str(df.at[idx, "Target_SLoc"]).strip().upper()
        cnd_raw = df.at[idx, "Best_Cand_SLoc"] if "Best_Cand_SLoc" in df.columns else ""
        cnd = "" if pd.isna(cnd_raw) else str(cnd_raw).strip().upper()

        v_match = resolve_d108_for_backlog_item(mat, tgt, verdicts_by_mat, cand_sloc=cnd)
        if v_match is None:
            # #187 B-2: No active linked GI error exists at `as_of` -> GI_LINK_GONE (NOT ① ANOMALY_NEW_AFTER_01!)
            df.at[idx, "D108_Cause_Code"] = "GI_LINK_GONE"
            df.at[idx, "D108_Cause_Label"] = _EN["D108_CLEARED"]
            df.at[idx, "D108_Severity"] = _EN["SEV_CLEARED"]
            df.at[idx, "D108_Linked_GI"] = "NONE (no active linked GI error)"
            df.at[idx, "D108_01_Verdict"] = "GI_LINK_GONE"
            df.at[idx, "D108_01_Plan_Time"] = eval_res.get("gi_01_plan_time_edt") or "NONE"
            df.at[idx, "D108_Detail"] = (
                "Linked GI error already cleared or has no SLoc overlap with current active GI errors — "
                "re-evaluate under standard backlog causes (B/R/C/A/D)"
            )
            df.at[idx, "D108_Is_Bug_Candidate"] = False
        else:
            df.at[idx, "D108_Cause_Code"] = v_match["d108_code"]
            df.at[idx, "D108_Cause_Label"] = v_match["d108_label"]
            df.at[idx, "D108_Severity"] = v_match["d108_severity"]
            df.at[idx, "D108_Linked_GI"] = v_match["gi_link_summary"]
            comp = (
                f" (+{','.join(v_match['gi_01_companion_statuses'])})"
                if v_match.get("gi_01_companion_statuses")
                else ""
            )
            df.at[idx, "D108_01_Verdict"] = f"{v_match['gi_01_status']}{comp}"
            df.at[idx, "D108_01_Plan_Time"] = f"{v_match['gi_01_plan_time']} ({v_match['gi_01_batch_id']})"
            df.at[idx, "D108_Detail"] = v_match["d108_detail"]
            df.at[idx, "D108_Is_Bug_Candidate"] = bool(v_match["is_bug_candidate"])
            df.at[idx, "Reason"] = (
                f"[{v_match['d108_severity']} | {v_match['d108_label']}] "
                f"Linked GI: {v_match['gi_link_summary']} | "
                f"01 Verdict ({v_match['gi_01_plan_time']}): {v_match['gi_01_status']}{comp} — "
                f"{v_match['d108_detail']} (D71 transfer block maintained)"
            )

    df.attrs["d108_eval_meta"] = {
        "as_of_edt": eval_res["as_of_edt"],
        "gi_01_batch_id": eval_res["gi_01_batch_id"],
        "gi_01_plan_time_edt": eval_res["gi_01_plan_time_edt"],
    }
    return df
