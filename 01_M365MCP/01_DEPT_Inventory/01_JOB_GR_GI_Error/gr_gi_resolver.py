"""
GR/GI Interface Error Resolver & Optimization Engine
====================================================
01_Inventory_Team / 01_GR_GI_Error (Plant P001 Headlamp)

Core Logic & Business Rules:
1. GR-GI Dependency Safeguard:
   - GI errors that share materials with active GR errors (e.g. 99000032-V) MUST BE PRESERVED.
   - Resolving GR errors will automatically resolve the downstream GI errors.
2. Movement Type Rules:
   - Only 311 and 312 errors are processed.
   - 901 (Scrap/Disposal) and 902 errors are strictly EXCLUDED and preserved.
3. 312 Reversal Handling (Method A):
   - 312 deficit is at TOSt (e.g. SP31).
   - Replenish TOSt via 311 transfer from surplus SAP location so 312 interface auto-clears in 10 mins.
4. Stock Accuracy Best Optimization:
   - Always transfer stock from storage locations where SAP_QTY > WMS_QTY (Surplus).
   - Minimizes variances across both origin and destination warehouses.
5. Date Sensitivity:
   - Previous month errors -> Document Date & Posting Date set to previous month.
   - Current month errors -> Today's date.
6. Clean Input Safeguard:
   - Every SAP input field is cleared with "" before input.
"""

import os
import sys
import glob
import re
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
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from datetime import datetime
from typing import Optional
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
GR_DIR = BASE_DIR / "gr_errors"
GI_DIR = BASE_DIR / "gi_errors"
MB52_DIR = BASE_DIR / "mb52"
WMS_DIR = BASE_DIR / "wms_parts_stack"
OUTPUT_DIR = BASE_DIR / "resolution_logs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_ROOT = BASE_DIR.parent.parent
HQ_DIR = PROJECT_ROOT / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from wms_mapping import (
    map_wms_gi_loc_to_sap,
    is_valid_target_sloc,
    is_accuracy_balance_sloc,
    ALLOWED_BALANCE_SLOCS,
    ALLOWED_EXTERNAL_SLOCS,
    VALID_OPERATION_SLOCS,
    calc_material_sloc_accuracy,
    evaluate_settled_state_accuracy
)
from persistence_guard import (
    check_transfer_persistence,
    load_previous_snapshot_pair
)
from ztcode_stock_guard import (
    gi_flow,
    extract_active_gr_error_map,
    extract_active_gi_error_map,
    get_gr_chain_protected_slocs,
    has_gr_chain_gap,
    check_movement_ztcode_stock_rec_block
)

# Standard warehouse rooms for Plant P001 Headlamp (Subject to Stock Accuracy Scoring)
WAREHOUSE_SLOCS = ALLOWED_BALANCE_SLOCS
DEFAULT_LINE_SLOC = 'SP31'
D109_SL00_GUARD_RELEASE_RUNS = 4
# #195: D109 guard release posting is strictly restricted to WMS-managed SL0x source warehouses (excludes SP* and SL49)
D109_ALLOWED_SOURCE_SLOCS = {"SL00", "SL01", "SL02", "SL03", "SL04"}
# #211 (D113): SL00 -> SL0x GI error same-cycle roundtrip clear via destination warehouse SAP stock
D113_ALLOWED_DEST_SLOCS = {"SL01", "SL02", "SL03", "SL04"}
# #220 / #222 (D115): A -> B shortage GI error deficit-only pump clear via destination B stock:
#   - B in SL01~SL04, SL49 (WMS-managed, SL49='DMA'): evidence=B_WMS (WMS(B) - SAP(B) >= 0.9 * error_sum, 2x)
#   - B in SP* (no WMS at B, A in SL00~SL04):         evidence=A_WMS (WMS(A) <= SAP(A), 2x, |0-WMS(A)| <= |SAP(A)-WMS(A)|)
D115_WMS_DEST_SLOCS = {"SL01", "SL02", "SL03", "SL04", "SL49"}
D115_ALLOWED_DEST_SLOCS = D115_WMS_DEST_SLOCS
D115_SP_ALLOWED_SOURCE_SLOCS = {"SL00", "SL01", "SL02", "SL03", "SL04"}
D115_WMS_GAP_MIN_RATIO = 0.9


def _is_d115_sp_dest(sloc: Optional[str]) -> bool:
    s = str(sloc or "").strip().upper()
    return len(s) == 4 and s.startswith("SP") and s[2:].isdigit()
# #221 M-2: Explicit single-rule-per-material precedence: D97 -> D113 (A=SL00) -> D115 -> D70
RULE_PRECEDENCE_ORDER = ("D97", "D113", "D115", "D70")
RULE_PRECEDENCE_RANK = {"D97": 4, "D113": 3, "D115": 2, "D70": 1}
# #197 / #199 (D110 Rule 2 only): Consecutive failure 24h cooldown (Rule 1 SP*/SL49 source block not adopted per #199)
D110_CONSECUTIVE_FAILURE_LIMIT = 3
D110_COOLDOWN_HOURS = 24


def _mask_sap_user_d110(msg: str) -> str:
    if not msg:
        return ""
    return re.sub(r"(by\s+(?:the\s+)?user\s+)\S+", r"\1<SAP_USER>", str(msg), flags=re.IGNORECASE)


def load_d110_failure_cooldown_map(
    gi_db_path: Optional[Path] = None,
    as_of_dt: Optional[datetime] = None,
    failure_limit: int = D110_CONSECUTIVE_FAILURE_LIMIT,
    cooldown_hours: int = D110_COOLDOWN_HOURS,
    exclude_batch_id: Optional[str] = None,
) -> dict:
    """
    D110 (#197) Rule 2:
    Scans `transfer_posting_history` (strictly read-only `?mode=ro`) to identify `(material, from_sloc, to_sloc)`
    routes that failed `>= failure_limit` (default 3) consecutive times and are still within their
    `cooldown_hours` (default 24h) cooldown window as of `as_of_dt` (defaults to local `datetime.now()`).
    """
    import sqlite3
    from datetime import timedelta

    target_db = Path(gi_db_path) if gi_db_path is not None else (BASE_DIR / "db" / "gr_gi_history.db")
    if not target_db.exists():
        return {}

    ref_dt = as_of_dt if as_of_dt is not None else datetime.now()
    uri = f"file:{target_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    try:
        rows = conn.execute(
            """
            SELECT id, batch_id, datetime(executed_at, 'localtime') AS local_ts,
                   material, from_sloc, to_sloc, transfer_qty, post_status, doc_number, sap_response
            FROM transfer_posting_history
            WHERE batch_id NOT LIKE 'DRYRUN%'
            ORDER BY id ASC
            """
        ).fetchall()
    except Exception:
        conn.close()
        return {}
    conn.close()

    track: dict = {}
    excl = str(exclude_batch_id or "").strip()
    for _rid, batch_id, local_ts, mat, f_sloc, t_sloc, t_qty, p_status, doc_no, sap_resp in rows:
        bid = str(batch_id or "").strip()
        if excl and bid == excl:
            continue

        row_dt = None
        if local_ts:
            try:
                row_dt = datetime.strptime(str(local_ts)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                row_dt = None
        if row_dt is None:
            m_b = re.search(r"(\d{8}_\d{6})", bid)
            if m_b:
                try:
                    row_dt = datetime.strptime(m_b.group(1), "%Y%m%d_%H%M%S")
                except ValueError:
                    row_dt = None
        if row_dt is None:
            continue
        if as_of_dt is not None and row_dt > ref_dt:
            continue

        k = (
            str(mat or "").strip(),
            str(f_sloc or "").strip().upper(),
            str(t_sloc or "").strip().upper(),
        )
        if not k[0] or not k[1] or not k[2]:
            continue

        st = track.setdefault(
            k,
            {
                "consec": 0,
                "cooldown_until": None,
                "triggered_at": None,
                "last_fail_dt": None,
                "last_error": "",
                "last_qty": 0.0,
                "last_batch_id": "",
            },
        )
        if st["cooldown_until"] is not None and row_dt >= st["cooldown_until"]:
            st["consec"] = 0
            st["cooldown_until"] = None
            st["triggered_at"] = None

        ps_u = str(p_status or "").strip().upper()
        doc_s = str(doc_no or "").strip()
        is_posted = ps_u not in ("ROLLED_BACK_D113", "ROLLBACK_FAILED_D113", "ROLLED_BACK_D115", "ROLLBACK_FAILED_D115", "FAILED_STEP1") and (ps_u in ("POSTED", "SUCCESS") or doc_s.startswith("49"))
        if is_posted:
            st["consec"] = 0
            st["cooldown_until"] = None
            st["triggered_at"] = None
        else:
            st["consec"] += 1
            st["last_fail_dt"] = row_dt
            st["last_error"] = _mask_sap_user_d110(str(sap_resp or ps_u or "").strip())
            st["last_qty"] = float(t_qty or 0.0)
            st["last_batch_id"] = bid
            if st["consec"] >= int(failure_limit) and st["cooldown_until"] is None:
                st["triggered_at"] = row_dt
                st["cooldown_until"] = row_dt + timedelta(hours=float(cooldown_hours))

    active_map = {}
    for k, st in track.items():
        cd_until = st.get("cooldown_until")
        if cd_until is not None and ref_dt < cd_until:
            active_map[k] = {
                "material": k[0],
                "from_sloc": k[1],
                "to_sloc": k[2],
                "consec_failures": int(st["consec"]),
                "triggered_at": st["triggered_at"].strftime("%Y-%m-%d %H:%M:%S") if st["triggered_at"] else "",
                "last_failed_at": st["last_fail_dt"].strftime("%Y-%m-%d %H:%M:%S") if st["last_fail_dt"] else "",
                "cooldown_until": cd_until.strftime("%Y-%m-%d %H:%M:%S"),
                "last_error": st["last_error"],
                "last_qty": float(st["last_qty"]),
                "last_batch_id": st["last_batch_id"],
            }
    return active_map

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("GRGIResolver")

def parse_sap_utf16_list(filepath: Path) -> pd.DataFrame:
    """Parses SAP UTF-16 tab-delimited exported list files."""
    if not filepath.exists():
        return pd.DataFrame()
        
    with open(filepath, 'r', encoding='utf-16', errors='replace') as f:
        lines = f.readlines()
        
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
                # Normalize length to match header
                row_dict = {}
                for i, col in enumerate(header):
                    if i < len(parts):
                        row_dict[col] = parts[i]
                    else:
                        row_dict[col] = ""
                data_rows.append(row_dict)
                
    df = pd.DataFrame(data_rows)
    return df

def load_mb52_stock() -> pd.DataFrame:
    """Loads current MB52 stock by (Material, SLoc)."""
    files = sorted(glob.glob(str(MB52_DIR / "*.XLS*")), key=os.path.getmtime, reverse=True)
    if not files:
        logger.warning("[D44 GUARD] No MB52 file found in MB52_DIR. Stale desktop fallback forbidden.")
        return pd.DataFrame()

        
    mb52_file = files[0]
    logger.info(f"Loading MB52 stock from: {mb52_file}")
    
    try:
        try:
            df = pd.read_html(mb52_file)[0]
        except Exception:
            df = pd.read_csv(mb52_file, sep='\t', encoding='utf-16')
            
        # Clean columns
        cols = [str(c).strip() for c in df.columns]
        df.columns = cols
        
        # Standardize: Material Number, SLoc, Unrestricted
        mat_col = next((c for c in cols if 'Material' in c and 'Desc' not in c), None)
        sloc_col = next((c for c in cols if 'SLoc' in c or 'Storage' in c), None)
        unres_col = next((c for c in cols if 'Unrestricted' in c), None)
        
        if mat_col and sloc_col and unres_col:
            df = df[[mat_col, sloc_col, unres_col]].dropna()
            df.columns = ['Material', 'SLoc', 'SAP_Stock']
            # Clean numeric
            df['SAP_Stock'] = df['SAP_Stock'].astype(str).str.replace(',', '').str.strip()
            df['SAP_Stock'] = pd.to_numeric(df['SAP_Stock'], errors='coerce').fillna(0)
            df['Material'] = df['Material'].astype(str).str.strip()
            df['SLoc'] = df['SLoc'].astype(str).str.strip().str.upper()
            return df
    except Exception as e:
        logger.error(f"Error loading MB52: {e}")
        
    return pd.DataFrame()

def load_wms_stock() -> pd.DataFrame:
    """Loads latest WMS stock by (Material, GI Loc)."""
    # Check 00_JOB_StockAccuracy daily stack or wms_parts_stack
    files = sorted(glob.glob(str(WMS_DIR / "*.xlsx")), key=os.path.getmtime, reverse=True)
    if not files:
        daily_stack = BASE_DIR.parent / "00_JOB_StockAccuracy" / "wms_daily_stack"
        files = sorted(glob.glob(str(daily_stack / "*.xlsx")), key=os.path.getmtime, reverse=True)
    if not files:
        legacy_stack = BASE_DIR.parent / "00_StockAccuracy" / "wms_daily_stack"
        files = sorted(glob.glob(str(legacy_stack / "*.xlsx")), key=os.path.getmtime, reverse=True)
        
    if not files:
        logger.warning("No WMS stock file found.")
        return pd.DataFrame()
        
    wms_file = files[0]
    logger.info(f"Loading WMS stock from: {wms_file}")
    
    try:
        df = pd.read_excel(wms_file)
        # Columns: Mat.No, GI Loc, Qty.
        mat_col = next((c for c in df.columns if 'Mat.No' in c or 'Material' in c), None)
        loc_col = next((c for c in df.columns if 'GI Loc' in c or 'SLoc' in c), None)
        qty_col = next((c for c in df.columns if 'Qty' in c), None)
        
        if mat_col and loc_col and qty_col:
            df = df[[mat_col, loc_col, qty_col]].dropna()
            df.columns = ['Material', 'Raw_Loc', 'WMS_Stock']
            df['WMS_Stock'] = pd.to_numeric(df['WMS_Stock'], errors='coerce').fillna(0)
            df['Material'] = df['Material'].astype(str).str.strip()
            # Standardize WMS textual GI Loc to official SAP SLoc
            df['SLoc'] = df['Raw_Loc'].apply(map_wms_gi_loc_to_sap)
            # Filter out unmapped or out-of-scope locations
            df = df.dropna(subset=['SLoc'])
            df['SLoc'] = df['SLoc'].astype(str).str.strip().str.upper()
            # Aggregate by Material and SLoc
            df = df.groupby(['Material', 'SLoc'], as_index=False)['WMS_Stock'].sum()
            logger.info(f"Loaded and mapped WMS stock: {len(df)} (Material, SLoc) rows.")
            return df
    except Exception as e:
        logger.error(f"Error loading WMS: {e}")
        
    return pd.DataFrame()

def _parse_sloc_dict_d109(val) -> dict:
    if isinstance(val, dict):
        return {str(k).strip().upper(): float(v or 0.0) for k, v in val.items()}
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return {}
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "{}"):
        return {}
    import ast
    import json
    try:
        parsed = json.loads(s)
    except Exception:
        try:
            parsed = ast.literal_eval(s)
        except Exception:
            return {}
    if isinstance(parsed, dict):
        out = {}
        for k, v in parsed.items():
            try:
                out[str(k).strip().upper()] = float(v or 0.0)
            except (TypeError, ValueError):
                pass
        return out
    return {}


def load_d109_prior_completed_runs(
    gi_db_path: Optional[Path] = None,
    plan_dir: Optional[Path] = None,
    exclude_run_id: Optional[str] = None,
    before_ts: Optional[datetime] = None,
    max_runs: int = 48,
) -> list:
    """
    D109: Loads prior COMPLETED 01 pipeline runs (most recent first) by pairing
    `pipeline_run_metrics` (status='COMPLETED') + `gi_error_snapshots` with each run's
    `GI_Resolution_Plan_YYYYMMDD_HHMMSS.xlsx` in `plan_dir`. Failed/skipped runs are excluded.
    Opens SQLite strictly read-only (`?mode=ro`).
    """
    import sqlite3
    target_db = Path(gi_db_path) if gi_db_path is not None else (BASE_DIR / "db" / "gr_gi_history.db")
    target_plan_dir = Path(plan_dir) if plan_dir is not None else OUTPUT_DIR
    if not target_db.exists():
        return []

    plan_entries = []
    if target_plan_dir.exists():
        for p in target_plan_dir.glob("GI_Resolution_Plan_*.xlsx"):
            m = re.search(r"(\d{8}_\d{6})", p.name)
            if m:
                try:
                    pts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
                    plan_entries.append((pts, p))
                except ValueError:
                    pass
    plan_entries.sort(key=lambda x: x[0])

    uri = f"file:{target_db.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    try:
        rows = conn.execute(
            """
            SELECT id, batch_id, datetime(run_timestamp, 'localtime') as run_time, total_gi_errors
            FROM pipeline_run_metrics
            WHERE status = 'COMPLETED'
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(max_runs),),
        ).fetchall()

        prior_runs = []
        used_plans = set()
        for _rid, batch_id, run_time, total_gi in rows:
            bid = str(batch_id or "").strip()
            if not bid or (exclude_run_id and bid == str(exclude_run_id).strip()):
                continue
            m_b = re.search(r"(\d{8}_\d{6})", bid)
            bts = None
            if m_b:
                try:
                    bts = datetime.strptime(m_b.group(1), "%Y%m%d_%H%M%S")
                except ValueError:
                    bts = None
            if bts is None and run_time:
                try:
                    bts = datetime.strptime(str(run_time)[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    bts = None
            if before_ts is not None and bts is not None and bts >= before_ts:
                continue

            # Load GI error keys from gi_error_snapshots for this batch_id
            snap_rows = conn.execute(
                """
                SELECT material, mvt, from_sloc, to_sloc, qty, error_date
                FROM gi_error_snapshots
                WHERE batch_id = ?
                """,
                (bid,),
            ).fetchall()
            gi_keys_by_mat = {}
            gi_keys_nodate_by_mat = {}
            for smat, smvt, sfrom, sto, sqty, sdate in snap_rows:
                ms = str(smat or "").strip()
                mv = str(smvt or "").replace(".0", "").strip()
                fs = str(sfrom or "").strip().upper()
                ts_loc = str(sto or "").strip().upper()
                src_f, dst_f = gi_flow(mv, fs, ts_loc)
                src_eff = src_f if src_f else fs
                dst_eff = dst_f if dst_f else ts_loc
                qv = round(float(sqty or 0.0), 3)
                dt_s = str(sdate or "").strip()
                gi_keys_by_mat.setdefault(ms, set()).add((mv, src_eff, dst_eff, qv, dt_s))
                gi_keys_nodate_by_mat.setdefault(ms, set()).add((mv, src_eff, dst_eff, qv))

            # Match GI_Resolution_Plan_<ts>.xlsx within [-15s, +600s] of batch start
            matched_plan_path = None
            if bts is not None and plan_entries:
                cands = [
                    (abs((pts - bts).total_seconds()), pts, pp)
                    for pts, pp in plan_entries
                    if pp not in used_plans and -15.0 <= (pts - bts).total_seconds() <= 600.0
                ]
                if cands:
                    cands.sort(key=lambda x: x[0])
                    matched_plan_path = cands[0][2]
                    used_plans.add(matched_plan_path)

            sl00_held_mats = {}
            d109_released_mats = {}
            d113_attempted_mats = {}
            d115_attempted_mats = {}
            try:
                d113_rows = conn.execute(
                    """
                    SELECT material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type
                    FROM transfer_posting_history
                    WHERE batch_id = ? AND (
                        (posting_type = 'ROUNDTRIP_CLEAR_VIA_DEST_D113' AND post_status IN ('ROLLED_BACK_D113', 'ROLLBACK_FAILED_D113'))
                        OR posting_type = 'ROLLBACK_D113'
                        OR (posting_type = 'DEST_PUMP_CLEAR_D115' AND post_status IN ('ROLLED_BACK_D115', 'ROLLBACK_FAILED_D115'))
                        OR posting_type = 'ROLLBACK_D115'
                    )
                    """,
                    (bid,),
                ).fetchall()
                for rmat, rfrom, rto, rqty, rdoc, rst, rptype in d113_rows:
                    rmat_s = str(rmat or "").strip()
                    if rmat_s:
                        entry_att = {
                            "batch_id": bid,
                            "from_sloc": str(rfrom or "").strip().upper(),
                            "to_sloc": str(rto or "").strip().upper(),
                            "transfer_qty": float(rqty or 0.0),
                            "doc_number": str(rdoc or "").strip(),
                            "post_status": str(rst or "").strip().upper(),
                        }
                        if "D115" in str(rptype or "").upper() or "D115" in str(rst or "").upper():
                            d115_attempted_mats[rmat_s] = entry_att
                        else:
                            d113_attempted_mats[rmat_s] = entry_att
            except Exception:
                pass
            try:
                rel_post_rows = conn.execute(
                    """
                    SELECT material, from_sloc, to_sloc, transfer_qty, doc_number, post_status
                    FROM transfer_posting_history
                    WHERE batch_id = ? AND posting_type = 'GUARD_RELEASED_D109'
                    """,
                    (bid,),
                ).fetchall()
                for rmat, rfrom, rto, rqty, rdoc, rst in rel_post_rows:
                    rmat_s = str(rmat or "").strip()
                    if rmat_s:
                        d109_released_mats[rmat_s] = {
                            "batch_id": bid,
                            "from_sloc": str(rfrom or "").strip().upper(),
                            "to_sloc": str(rto or "").strip().upper(),
                            "transfer_qty": float(rqty or 0.0),
                            "doc_number": str(rdoc or "").strip(),
                            "post_status": str(rst or "").strip().upper(),
                            "source": "DB",
                        }
            except Exception:
                pass

            if matched_plan_path is not None and matched_plan_path.exists():
                try:
                    pdf = pd.read_excel(matched_plan_path)
                    if not pdf.empty and "Status" in pdf.columns and "Material" in pdf.columns:
                        st_col = pdf["Status"].astype(str).str.strip()
                        sub = pdf[st_col == "SKIPPED_PENDING_INTERFACE_SL00"]
                        for _, pr in sub.iterrows():
                            pmat = str(pr.get("Material") or "").strip()
                            if not pmat:
                                continue
                            sap_d = _parse_sloc_dict_d109(pr.get("SAP_By_SLoc"))
                            wms_d = _parse_sloc_dict_d109(pr.get("WMS_By_SLoc"))
                            if "SL00_WMS_SAP_Gap" in pr and pd.notna(pr.get("SL00_WMS_SAP_Gap")):
                                gap_v = float(pr.get("SL00_WMS_SAP_Gap") or 0.0)
                            else:
                                gap_v = float(wms_d.get("SL00", 0.0)) - float(sap_d.get("SL00", 0.0))
                            sl00_held_mats[pmat] = {
                                "status": "SKIPPED_PENDING_INTERFACE_SL00",
                                "from_sloc": str(pr.get("From_SLoc") or "").strip().upper(),
                                "to_sloc": str(pr.get("To_SLoc") or "").strip().upper(),
                                "transfer_qty": float(pr.get("Transfer_Qty") or 0.0),
                                "sap_by_sloc": sap_d,
                                "wms_by_sloc": wms_d,
                                "sl00_gap": gap_v,
                            }
                        rel_sub = pdf[st_col == "GUARD_RELEASED_D109"]
                        for _, pr in rel_sub.iterrows():
                            pmat = str(pr.get("Material") or "").strip()
                            if pmat and pmat not in d109_released_mats:
                                d109_released_mats[pmat] = {
                                    "batch_id": bid,
                                    "from_sloc": str(pr.get("From_SLoc") or "").strip().upper(),
                                    "to_sloc": str(pr.get("To_SLoc") or "").strip().upper(),
                                    "transfer_qty": float(pr.get("Transfer_Qty") or 0.0),
                                    "doc_number": "",
                                    "post_status": "PLANNED",
                                    "source": "PLAN",
                                }
                except Exception as e_plan:
                    logger.warning(f"[D109] Failed reading historical plan {matched_plan_path.name}: {e_plan}")

            prior_runs.append(
                {
                    "batch_id": bid,
                    "run_time": str(run_time or ""),
                    "total_gi_errors": int(total_gi or 0),
                    "plan_file": matched_plan_path.name if matched_plan_path else None,
                    "gi_keys_by_mat": gi_keys_by_mat,
                    "gi_keys_nodate_by_mat": gi_keys_nodate_by_mat,
                    "sl00_held_mats": sl00_held_mats,
                    "d109_released_mats": d109_released_mats,
                    "d113_attempted_mats": d113_attempted_mats,
                    "d115_attempted_mats": d115_attempted_mats,
                }
            )
        return prior_runs
    finally:
        conn.close()


def evaluate_d109_sl00_guard_release(
    mat: str,
    s_loc: str,
    planned_q: float,
    root_e: dict,
    cleared_sl00_errors: list,
    current_sap: dict,
    current_wms: dict,
    source_persistent_surplus: float,
    prior_completed_runs: Optional[list],
    active_gr_errors: Optional[dict],
    protected_gr_materials: Optional[set],
    gr_valid: bool = True,
) -> dict:
    """
    D109: Evaluates whether a non-roundtrip transfer to `SL00` (`SKIPPED_PENDING_INTERFACE_SL00`)
    should be released (`GUARD_RELEASED_D109`) under all 4 mandatory conditions + 1-time release safety guard:
      0. Safety Guard (#190 B-5): If `GUARD_RELEASED_D109` was already executed in a prior run during the
         same unbroken GI error streak, NEVER release again (alert only, zero retry/double posting).
      1. Same GI error key held as `SKIPPED_PENDING_INTERFACE_SL00` for >= `D109_SL00_GUARD_RELEASE_RUNS` (4)
         consecutive COMPLETED `01` runs (including current run).
      2. Active GR error count for `mat` == 0 across `GR_Error` + `GR_Import` + `GR_Domestics` (`gr_valid=True`).
      3. `SL00` `WMS - SAP` gap has NOT decreased across the 4-run window `[g_{-3}, g_{-2}, g_{-1}, g_0]`.
      4. Capped transfer qty = `min(gi_error_qty, sl00_wms - sl00_sap, from_sloc_sap_surplus) > 0`
         (plus caller re-verifies D77 / settled-state accuracy).
    """
    mvt = str(root_e.get("mvt") or "").replace(".0", "").strip()
    src = str(root_e.get("src") or "SL00").strip().upper()
    dst = str(root_e.get("dst") or "").strip().upper()
    qty_r = round(float(root_e.get("qty") or 0.0), 3)
    err_date = str(root_e.get("err_date") or "").strip()
    key_5 = (mvt, src, dst, qty_r, err_date)
    key_4 = (mvt, src, dst, qty_r)

    def _run_has_same_gi_key(prun: dict) -> bool:
        keys5 = (prun.get("gi_keys_by_mat") or {}).get(mat, set())
        keys4 = (prun.get("gi_keys_nodate_by_mat") or {}).get(mat, set())
        if err_date and any(k[4] for k in keys5):
            return key_5 in keys5
        if keys4:
            return key_4 in keys4
        if prun.get("gi_keys_by_mat") is None and prun.get("gi_keys_nodate_by_mat") is None:
            return mat in (prun.get("sl00_held_mats") or {}) or mat in (prun.get("d109_released_mats") or {})
        return False

    # #190 B-5: Check unbroken GI error streak for any prior GUARD_RELEASED_D109 execution
    already_released = False
    already_released_info = None
    for prun in (prior_completed_runs or []):
        if not _run_has_same_gi_key(prun):
            break
        rel_map = prun.get("d109_released_mats") or {}
        if mat in rel_map:
            already_released = True
            already_released_info = rel_map[mat]
            break

    prior_consec = 0
    prior_gaps_rev = []
    for prun in (prior_completed_runs or []):
        held_map = prun.get("sl00_held_mats") or {}
        if mat not in held_map:
            break
        if not _run_has_same_gi_key(prun):
            break
        prior_consec += 1
        prior_gaps_rev.append(float(held_map[mat].get("sl00_gap", 0.0)))

    consec_runs = prior_consec + 1
    sl00_wms_cur = float(current_wms.get("SL00", 0.0))
    sl00_sap_cur = float(current_sap.get("SL00", 0.0))
    current_sl00_gap = sl00_wms_cur - sl00_sap_cur

    # Active GR check across 3-file bundle (GR_Error + GR_Import + GR_Domestics)
    gr_map = active_gr_errors or {}
    prot_set = protected_gr_materials or set()
    if not gr_valid:
        gr_count = -1
        gr_zero_ok = False
        gr_check_label = "GR_UNVERIFIED"
    else:
        gr_info = gr_map.get(mat)
        if isinstance(gr_info, dict):
            gr_count = max(1, int(gr_info.get("Count", 1) or 1))
        elif gr_info:
            gr_count = 1
        elif mat in prot_set:
            gr_count = 1
        else:
            gr_count = 0
        gr_zero_ok = (gr_count == 0) and (mat not in prot_set)
        gr_check_label = "3-file GR=0 (GR_Error/Import/Domestics)" if gr_zero_ok else f"Active GR={gr_count}"

    # 4-run SL00 WMS-SAP gap sequence (oldest -> newest across last D109_SL00_GUARD_RELEASE_RUNS runs)
    needed_prior = D109_SL00_GUARD_RELEASE_RUNS - 1
    recent_prior_chron = list(reversed(prior_gaps_rev[:needed_prior]))
    gap_window = recent_prior_chron + [current_sl00_gap]
    gap_non_decreasing = (
        len(gap_window) >= D109_SL00_GUARD_RELEASE_RUNS
        and all(gap_window[i] >= gap_window[i - 1] - 1e-6 for i in range(1, len(gap_window)))
    )

    gi_err_qty = (
        sum(float(e.get("qty") or 0.0) for e in cleared_sl00_errors)
        if cleared_sl00_errors
        else float(root_e.get("qty") or planned_q)
    )
    s_loc_norm = str(s_loc or "").strip().upper()
    is_wms_sl0x_source = s_loc_norm in D109_ALLOWED_SOURCE_SLOCS
    raw_from_surplus = float(current_sap.get(s_loc, 0.0)) - float(current_wms.get(s_loc, 0.0))
    if s_loc in WAREHOUSE_SLOCS:
        eff_from_surplus = min(raw_from_surplus, float(source_persistent_surplus))
    else:
        eff_from_surplus = raw_from_surplus

    capped_qty = max(
        0.0,
        min(
            float(planned_q),
            float(gi_err_qty),
            float(current_sl00_gap),
            float(eff_from_surplus),
        ),
    )

    # #190 B-4 & #211 D113: Only classify as F (suspected unposted GR) when destination X lacks SAP stock for D113 roundtrip
    dest_wms_minus_sap = (
        float(current_wms.get(dst, 0.0)) - float(current_sap.get(dst, 0.0))
        if (dst and dst != "SL00")
        else 0.0
    )
    dest_sap_avail = float(current_sap.get(dst, 0.0)) if (dst and dst in D113_ALLOWED_DEST_SLOCS) else 0.0
    suspected_unposted_gr_f = (
        not already_released
        and consec_runs >= D109_SL00_GUARD_RELEASE_RUNS
        and gr_zero_ok
        and abs(sl00_wms_cur) <= 1e-6
        and abs(sl00_sap_cur) <= 1e-6
        and bool(dst)
        and dst != "SL00"
        and dest_wms_minus_sap >= gi_err_qty - 1e-6
        and dest_sap_avail < gi_err_qty - 1e-6
    )

    hold_reasons = []
    if already_released:
        rel_bid = (already_released_info or {}).get("batch_id") or "-"
        rel_doc = (already_released_info or {}).get("doc_number") or "N/A"
        hold_reasons.append(f"Residual GI error after prior GUARD_RELEASED_D109 posting ({rel_bid}, Doc:{rel_doc}) — retry blocked (duplicate-posting safeguard)")
    else:
        if consec_runs < D109_SL00_GUARD_RELEASE_RUNS:
            hold_reasons.append(f"Consecutive runs {consec_runs}/{D109_SL00_GUARD_RELEASE_RUNS} below threshold")
        if not gr_zero_ok:
            hold_reasons.append(gr_check_label)
        if consec_runs >= D109_SL00_GUARD_RELEASE_RUNS and not gap_non_decreasing:
            hold_reasons.append("SL00 WMS-SAP gap decreasing (interface inflow in progress)")
        if not is_wms_sl0x_source:
            hold_reasons.append(f"Source SLoc ({s_loc_norm or '-'}) is non-WMS (only SL0x allowed for D109 release; SP*·SL49 excluded)")
        if suspected_unposted_gr_f:
            hold_reasons.append(
                f"Suspected missing GR (F): SL00 WMS=SAP=0, dest SLoc ({dst}) WMS-SAP=+{dest_wms_minus_sap:,.1f} EA (>= GI {gi_err_qty:,.1f} EA) "
                f"— physical stock arrived without SAP GR record; verify SAP GR posting"
            )
        elif is_wms_sl0x_source and capped_qty <= 1e-6:
            hold_reasons.append(
                f"Capped qty 0 EA (GI={gi_err_qty:,.1f}, SL00 WMS-SAP={current_sl00_gap:,.1f}, {s_loc} surplus={eff_from_surplus:,.1f})"
            )

    released = (
        not already_released
        and is_wms_sl0x_source
        and consec_runs >= D109_SL00_GUARD_RELEASE_RUNS
        and gr_zero_ok
        and gap_non_decreasing
        and capped_qty > 1e-6
    )
    gap_trace_str = " -> ".join(f"{g:,.1f}" for g in gap_window)
    return {
        "released": released,
        "is_wms_sl0x_source": is_wms_sl0x_source,
        "already_released": already_released,
        "already_released_info": already_released_info,
        "suspected_unposted_gr_f": suspected_unposted_gr_f,
        "dest_sloc": dst,
        "dest_wms_minus_sap": dest_wms_minus_sap,
        "consec_runs": consec_runs,
        "gr_count": max(0, gr_count),
        "gr_zero_ok": gr_zero_ok,
        "gr_check_label": gr_check_label,
        "current_sl00_gap": current_sl00_gap,
        "gap_window": gap_window,
        "gap_trace_str": gap_trace_str,
        "gap_non_decreasing": gap_non_decreasing,
        "gi_err_qty": gi_err_qty,
        "from_sap_surplus": eff_from_surplus,
        "capped_qty": capped_qty,
        "hold_reason_summary": ", ".join(hold_reasons) if hold_reasons else "",
    }


def evaluate_d113_sl00_dest_roundtrip(
    mat: str,
    s_loc: str,
    d_loc: str,
    remaining_errors: list,
    current_sap: dict,
    current_wms: dict,
    prev_sap_by_sloc: Optional[dict],
    prev_wms_by_sloc: Optional[dict],
    prev_snapshot_time: Optional[str],
    active_gr_errors: Optional[dict],
    protected_gr_materials: Optional[set],
    gr_valid: bool = True,
    prior_completed_runs: Optional[list] = None,
) -> dict:
    """
    D113 (#211): Evaluates whether a `SL00 -> X` (`X in SL01~SL04`, mvt `311/312`) GI error
    can be resolved via same-cycle roundtrip using destination warehouse `X`'s SAP stock (`X -> SL00` + GI clear):
      1. GI error flow is `SL00 -> X` (`X in SL01~SL04`), mvt `311` or `312`, pure shortage message.
      2. `SL00 WMS == SAP` (diff == 0, including 0==0) in BOTH current snapshot and previous snapshot (2 consecutive runs).
      3. Active 3-file GR (`Error` / `Import` / `Domestics`) count for `mat` == 0 (`gr_valid=True`).
      4. Destination `X` (`s_loc`) has `SAP_Stock >= error_qty`.
    """
    s_norm = str(s_loc or "").strip().upper()
    d_norm = str(d_loc or "").strip().upper()
    if d_norm != "SL00" or s_norm not in D113_ALLOWED_DEST_SLOCS:
        return {"eligible": False, "reason": "not_sl00_to_sl0x"}

    gr_map = active_gr_errors or {}
    prot_set = protected_gr_materials or set()
    if not gr_valid or mat in gr_map or mat in prot_set:
        return {"eligible": False, "reason": "active_or_unverified_gr"}

    cur_sl00_wms = float(current_wms.get("SL00", 0.0))
    cur_sl00_sap = float(current_sap.get("SL00", 0.0))
    if abs(cur_sl00_wms - cur_sl00_sap) > 1e-6:
        return {"eligible": False, "reason": "current_sl00_wms_ne_sap", "cur_sl00_gap": cur_sl00_wms - cur_sl00_sap}
    if not prev_snapshot_time or prev_wms_by_sloc is None or prev_sap_by_sloc is None:
        return {"eligible": False, "reason": "prev_snapshot_missing"}
    prev_sl00_wms = float(prev_wms_by_sloc.get("SL00", 0.0))
    prev_sl00_sap = float(prev_sap_by_sloc.get("SL00", 0.0))
    if abs(prev_sl00_wms - prev_sl00_sap) > 1e-6:
        return {"eligible": False, "reason": "prev_sl00_wms_ne_sap", "prev_sl00_gap": prev_sl00_wms - prev_sl00_sap}

    matched_errs = []
    for err in remaining_errors:
        mvt_e = str(err.get("mvt") or "").replace(".0", "").strip()
        if err.get("src") == "SL00" and err.get("dst") == s_norm and mvt_e in ("311", "312"):
            msg_lower = str(err.get("msg", "")).lower()
            is_pure_deficit = any(k in msg_lower for k in ("deficit", "shortage", "insufficient", "부족"))
            has_blocking_err = any(
                kw in msg_lower
                for kw in ("lock", "blocked", "closed", "period", "customiz", "missing", "account", "valuation")
            )
            if is_pure_deficit and not has_blocking_err:
                matched_errs.append(err)
    if not matched_errs:
        return {"eligible": False, "reason": "no_matching_sl00_to_x_shortage_error"}

    for prun in (prior_completed_runs or []):
        keys4 = (prun.get("gi_keys_nodate_by_mat") or {}).get(mat, set())
        e0 = matched_errs[0]
        k4 = (
            str(e0.get("mvt") or "").replace(".0", "").strip(),
            "SL00",
            s_norm,
            round(float(e0.get("qty") or 0.0), 3),
        )
        if keys4 and k4 not in keys4:
            break
        att_map = prun.get("d113_attempted_mats") or {}
        if mat in att_map:
            return {"eligible": False, "reason": "already_attempted_in_streak", "attempt_info": att_map[mat]}

    avail_x = float(current_sap.get(s_norm, 0.0))
    curr_sl00 = float(current_sap.get("SL00", 0.0))
    needed_list = []
    cum_needed = 0.0
    for err in matched_errs:
        eq = float(err.get("qty") or 0.0)
        nd = max(0.0, eq - curr_sl00)
        if nd > 0:
            needed_list.append(nd)
        cum_needed += eq
    total_needed = max(0.0, cum_needed - curr_sl00)
    if total_needed > 0 and total_needed not in needed_list:
        needed_list.append(total_needed)

    feasible_qtys = sorted({round(q, 4) for q in needed_list if 0 < q <= avail_x + 1e-6})
    if not feasible_qtys:
        return {"eligible": False, "reason": "insufficient_dest_sap_stock", "avail_x": avail_x}

    return {
        "eligible": True,
        "dest_sloc": s_norm,
        "avail_x": avail_x,
        "feasible_qtys": feasible_qtys,
        "matched_errors": matched_errs,
    }


def evaluate_d115_dest_pump_clear(
    mat: str,
    s_loc: str,
    d_loc: str,
    curr_avail_d: float,
    remaining_errors: list,
    current_sap: dict,
    current_wms: dict,
    prev_sap_by_sloc: Optional[dict],
    prev_wms_by_sloc: Optional[dict],
    prev_snapshot_time: Optional[str],
    active_gr_errors: Optional[dict],
    protected_gr_materials: Optional[set],
    gr_valid: bool = True,
    prior_completed_runs: Optional[list] = None,
    require_prev_snapshot: bool = False,
) -> dict:
    """
    D115 (#220): Evaluates whether an `A -> B` (`d_loc -> s_loc`) shortage GI error
    (same material & same route summed across multiple items, mvt-agnostic over transfer flows)
    can be resolved in the same cycle by pumping ONLY the deficit `error_sum - SAP(A)` from `B`:
      1. `B` (`s_loc`) in `SL01~SL04` (WMS-managed warehouse); `A` (`d_loc`) != `B` (`SP*` or `SL0x`).
      2. `WMS(B) - SAP(B) >= error_sum * 0.9` in current snapshot (physical goods already returned to `B`).
      3. `pump_qty = error_sum - SAP(A)` (`0 < pump_qty < error_sum`) and `SAP(B) >= pump_qty`.
      4. Active 3-file GR == 0 (`gr_valid=True`) AND 2 consecutive snapshots satisfy `WMS(B) - SAP(B) >= error_sum * 0.9`.
    """
    s_norm = str(s_loc or "").strip().upper()
    d_norm = str(d_loc or "").strip().upper()
    is_b_wms = s_norm in D115_WMS_DEST_SLOCS
    is_b_sp = _is_d115_sp_dest(s_norm)
    if not d_norm or d_norm == s_norm or not (is_b_wms or is_b_sp):
        return {"eligible": False, "reason": "dest_not_d115_target"}

    if is_b_wms:
        # A=SL00 -> B=SL01~SL04 is explicitly governed by higher-precedence D113 & D37/D109;
        # if B=SL49 and A=SL00, require zero SL00 interface gap.
        if d_norm == "SL00" and s_norm in D113_ALLOWED_DEST_SLOCS:
            return {"eligible": False, "reason": "source_a_is_sl00_governed_by_d113_d109"}
        if d_norm == "SL00" and abs(float(current_wms.get("SL00", 0.0)) - float(current_sap.get("SL00", 0.0))) > 1e-6:
            return {"eligible": False, "reason": "sl00_pending_interface_gap"}
    else:
        # #222 Part A: For B in SP* (no WMS at B), source A must be in SL00~SL04
        if d_norm not in D115_SP_ALLOWED_SOURCE_SLOCS:
            return {"eligible": False, "reason": "sp_dest_requires_source_a_in_sl00_sl04"}

    matched_errs = []
    for err in remaining_errors:
        if err.get("src") == d_norm and err.get("dst") == s_norm:
            msg_lower = str(err.get("msg", "")).lower()
            is_pure_deficit = any(k in msg_lower for k in ("deficit", "shortage", "insufficient", "부족"))
            has_blocking_err = any(
                kw in msg_lower
                for kw in ("lock", "blocked", "closed", "period", "customiz", "missing", "account", "valuation")
            )
            if is_pure_deficit and not has_blocking_err:
                matched_errs.append(err)
    if not matched_errs:
        return {"eligible": False, "reason": "no_matching_a_to_b_shortage_error"}

    error_sum = round(sum(float(e.get("qty") or 0.0) for e in matched_errs), 4)
    avail_a = float(curr_avail_d or 0.0)
    pump_qty = round(error_sum - avail_a, 4)
    if pump_qty <= 1e-6:
        return {"eligible": False, "reason": "no_deficit_at_source_a", "pump_qty": pump_qty}
    if is_b_wms and s_norm in D113_ALLOWED_DEST_SLOCS and pump_qty >= error_sum - 1e-6:
        return {"eligible": False, "reason": "zero_source_a_stock_use_d97_or_d113", "pump_qty": pump_qty}

    avail_b = float(current_sap.get(s_norm, 0.0))
    if avail_b + 1e-6 < pump_qty:
        return {"eligible": False, "reason": "insufficient_dest_b_sap_stock", "avail_b": avail_b, "pump_qty": pump_qty}

    cur_wms_a = float(current_wms.get(d_norm, 0.0))
    cur_gap_b = float(current_wms.get(s_norm, 0.0)) - avail_b
    prev_gap_b = cur_gap_b

    if is_b_wms:
        min_gap_req = error_sum * D115_WMS_GAP_MIN_RATIO
        if cur_gap_b + 1e-6 < min_gap_req:
            return {
                "eligible": False,
                "reason": "insufficient_dest_b_wms_minus_sap",
                "cur_gap_b": cur_gap_b,
                "min_gap_req": min_gap_req,
                "error_sum": error_sum,
            }
    else:
        # #222 Part A (B in SP*, evidence=A_WMS):
        # Condition 2: WMS(A) <= SAP(A) (A's WMS already deducted physical outbound)
        if cur_wms_a > avail_a + 1e-6:
            return {
                "eligible": False,
                "reason": "source_a_wms_exceeds_sap",
                "cur_wms_a": cur_wms_a,
                "avail_a": avail_a,
            }
        # Condition 6: Settled SAP(A) = 0 -> |0 - WMS(A)| <= |SAP(A) - WMS(A)| (A accuracy must not worsen)
        if abs(0.0 - cur_wms_a) > abs(avail_a - cur_wms_a) + 1e-6:
            return {
                "eligible": False,
                "reason": "source_a_accuracy_worsens",
                "cur_wms_a": cur_wms_a,
                "avail_a": avail_a,
            }

    gr_map = active_gr_errors or {}
    prot_set = protected_gr_materials or set()
    if not gr_valid or mat in gr_map or mat in prot_set:
        return {
            "eligible": False,
            "reason": "active_or_unverified_gr",
            "gr_blocked": True,
            "error_sum": error_sum,
            "pump_qty": pump_qty,
        }

    if prev_snapshot_time:
        if prev_wms_by_sloc is None or prev_sap_by_sloc is None:
            return {"eligible": False, "reason": "prev_snapshot_missing"}
        if is_b_wms:
            min_gap_req = error_sum * D115_WMS_GAP_MIN_RATIO
            prev_gap_b = float(prev_wms_by_sloc.get(s_norm, 0.0)) - float(prev_sap_by_sloc.get(s_norm, 0.0))
            if prev_gap_b + 1e-6 < min_gap_req:
                return {
                    "eligible": False,
                    "reason": "prev_dest_b_wms_minus_sap_below_threshold",
                    "prev_gap_b": prev_gap_b,
                    "min_gap_req": min_gap_req,
                }
        else:
            prev_wms_a = float(prev_wms_by_sloc.get(d_norm, 0.0))
            prev_sap_a = float(prev_sap_by_sloc.get(d_norm, 0.0))
            if prev_wms_a > prev_sap_a + 1e-6:
                return {
                    "eligible": False,
                    "reason": "prev_source_a_wms_exceeds_sap",
                    "prev_wms_a": prev_wms_a,
                    "prev_sap_a": prev_sap_a,
                }
    elif require_prev_snapshot:
        return {"eligible": False, "reason": "prev_snapshot_missing"}

    for prun in (prior_completed_runs or []):
        keys4 = (prun.get("gi_keys_nodate_by_mat") or {}).get(mat, set())
        e0 = matched_errs[0]
        k4 = (
            str(e0.get("mvt") or "").replace(".0", "").strip(),
            d_norm,
            s_norm,
            round(float(e0.get("qty") or 0.0), 3),
        )
        if keys4 and k4 not in keys4:
            break
        att115_map = prun.get("d115_attempted_mats") or {}
        if mat in att115_map:
            return {"eligible": False, "reason": "already_attempted_in_streak", "attempt_info": att115_map[mat]}

    return {
        "eligible": True,
        "evidence": "B_WMS" if is_b_wms else "A_WMS",
        "source_a": d_norm,
        "dest_b": s_norm,
        "avail_a": avail_a,
        "avail_b": avail_b,
        "cur_wms_a": cur_wms_a,
        "cur_gap_b": cur_gap_b,
        "prev_gap_b": prev_gap_b,
        "error_sum": error_sum,
        "pump_qty": pump_qty,
        "matched_errors": matched_errs,
    }


def run_cascade(sap_state: dict, pending_errors: list):
    """
    Simulates domino-style cascade clearing on pending errors given a SAP stock state.
    Returns: (final_state, list_of_cleared_indices)
    """
    state = dict(sap_state)
    cleared_indices = []
    changed = True
    while changed:
        changed = False
        for idx, e in enumerate(pending_errors):
            if idx not in cleared_indices:
                src = e['src']
                dst = e['dst']
                qty = e['qty']
                if state.get(src, 0.0) >= qty:
                    state[src] -= qty
                    state[dst] = state.get(dst, 0.0) + qty
                    cleared_indices.append(idx)
                    changed = True
    return state, cleared_indices

def resolve_material_errors_unified(
    mat: str,
    rows_df: pd.DataFrame,
    stock_comparison: pd.DataFrame,
    protected_gr_materials: set,
    today_str: str,
    prev_month_str: str,
    prev_wms_df: Optional[pd.DataFrame] = None,
    prev_mb52_df: Optional[pd.DataFrame] = None,
    prev_snapshot_time: Optional[str] = None,
    active_gr_errors: Optional[dict] = None,
    active_gi_errors: Optional[dict] = None,
    run_id: Optional[str] = None,
    signal_db_path: Optional[Path] = None,
    d109_prior_runs: Optional[list] = None,
    gr_valid: bool = True,
    d110_cooldown_map: Optional[dict] = None,
    require_prev_snapshot: bool = False,
) -> list:
    """
    Unified resolution engine for a single material's GI errors (handles N=1 and chained N>=2).
    1. GR Dependency Guard (D73): If material has active GR error, preserve rows where SLoc intersects as PROTECTED_GR_DEPENDENT.
    2. Exclude 901/902 and non-311/312 movements.
    3. Directed Flow Graph & Cascade Simulation:
       - Checks initial auto-clearing by current SAP stock.
       - Evaluates candidate source warehouses (S) outside deficit locations.
       - Selects optimal (S -> D, q) transfer that clears max errors, minimizes plant variance (Delta V <= 0),
         and minimizes necessary transfer quantity.
       - Marks root unblocking transfer as READY_FOR_EXECUTION.
       - Marks subsequent unblocked errors as CHAIN_CASCADE_RESOLVE (Action: SKIP, prevents duplicate transfers).
       - Evaluates partial rebalancing if full resolution is unavailable.
       - Marks remaining unresolvable errors as INSUFFICIENT_SAP_STOCK.
    """
    plan_rows = []

    # 1. Filter out protected GR errors (D73: Narrowed to SLoc intersection; D90-1: Full Chain Protection)
    actionable_candidate_rows = []
    is_chain_gap = False
    row_edges = set()
    for _, r_edge in rows_df.iterrows():
        _m = str(r_edge.get('Mvt', '')).strip()
        _f = str(r_edge.get('FromSt', '')).strip().upper()
        _t = str(r_edge.get('TOSt', '')).strip().upper()
        _s, _d = gi_flow(_m, _f, _t)
        if _s and _d:
            row_edges.add((_s, _d))

    if active_gr_errors and mat in active_gr_errors:
        gr_info = active_gr_errors[mat]
        base_gr_slocs = gr_info.get('SLocs', set())
        gr_slocs = (
            get_gr_chain_protected_slocs(mat, active_gr_errors, active_gi_errors, extra_edges=row_edges)
            if base_gr_slocs else set()
        )
        is_chain_gap = has_gr_chain_gap(mat, active_gr_errors, active_gi_errors, extra_edges=row_edges)

        for _, r in rows_df.iterrows():
            mvt = str(r.get('Mvt', '')).strip()
            from_st = str(r.get('FromSt', '')).strip().upper()
            to_st = str(r.get('TOSt', '')).strip().upper()
            raw_qty = str(r.get('Qty', '0')).replace(',', '').strip()
            qty = float(raw_qty) if raw_qty else 0.0
            src_flow, dst_flow = gi_flow(mvt, from_st, to_st)
            deficit_loc = src_flow if src_flow else (from_st if mvt == '311' else to_st)

            # Check SLoc intersection (including D90-1 full chain expansion)
            overlapping = set()
            if deficit_loc and deficit_loc in gr_slocs:
                overlapping.add(deficit_loc)
            if dst_flow and dst_flow in gr_slocs:
                overlapping.add(dst_flow)

            # Block if overlapping SLoc exists or base_gr_slocs is empty (fail-closed)
            if overlapping or (not base_gr_slocs):
                overlap_str = ", ".join(sorted(list(overlapping))) if overlapping else (", ".join(sorted(list(gr_slocs))) or "N/A")
                v_str = ", ".join(sorted(list(gr_info.get('Vendors', [])))) or "N/A"
                gr_qty = gr_info.get('Total_GR_Qty', 0.0)
                plan_rows.append({
                    'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                    'From_SLoc': '', 'To_SLoc': deficit_loc, 'Transfer_Qty': qty,
                    'MB1B_Mvt': '311', 'Doc_Date': today_str, 'Post_Date': today_str,
                    'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                    'Action': 'SKIP', 'Status': 'PROTECTED_GR_DEPENDENT',
                    'Reason': f"GR {overlap_str} {gr_qty:,.1f} EA ↔ GI {deficit_loc}->{dst_flow or to_st} GR-linked hold (Vendor: {v_str}, D73/D90-1)"
                })
            else:
                actionable_candidate_rows.append(r)
    elif mat in protected_gr_materials and not active_gr_errors:
        for _, r in rows_df.iterrows():
            mvt = str(r.get('Mvt', '')).strip()
            from_st = str(r.get('FromSt', '')).strip().upper()
            to_st = str(r.get('TOSt', '')).strip().upper()
            raw_qty = str(r.get('Qty', '0')).replace(',', '').strip()
            qty = float(raw_qty) if raw_qty else 0.0
            deficit_loc = from_st if mvt == '311' else to_st
            plan_rows.append({
                'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                'From_SLoc': '', 'To_SLoc': deficit_loc, 'Transfer_Qty': qty,
                'MB1B_Mvt': '311', 'Doc_Date': today_str, 'Post_Date': today_str,
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': 'PROTECTED_GR_DEPENDENT',
                'Reason': 'Pending active GR error on material - held to prevent duplicate posting distortion'
            })
        return plan_rows
    else:
        actionable_candidate_rows = [r for _, r in rows_df.iterrows()]

    if not actionable_candidate_rows:
        return plan_rows

    eval_rows_df = pd.DataFrame(actionable_candidate_rows)

    # 2. Separate excluded (901/902/unsupported) vs actionable (311/312)
    actionable = []
    for idx, r in eval_rows_df.iterrows():
        mvt = str(r.get('Mvt', '')).strip()
        from_st = str(r.get('FromSt', '')).strip().upper()
        to_st = str(r.get('TOSt', '')).strip().upper()
        raw_qty = str(r.get('Qty', '0')).replace(',', '').strip()
        qty = float(raw_qty) if raw_qty else 0.0
        msg_val = r.get('Message', '')
        msg = "" if pd.isna(msg_val) else str(msg_val).strip()
        if msg.lower() in ('nan', 'none'):
            msg = ""
        err_date = str(r.get('Date', today_str)).strip()

        src_flow, dst_flow = gi_flow(mvt, from_st, to_st)
        deficit_loc = src_flow if src_flow else from_st

        # Posting Date logic
        is_prev_month = False
        try:
            err_dt = datetime.strptime(err_date, "%Y.%m.%d")
            if err_dt.month != datetime.now().month:
                is_prev_month = True
        except Exception:
            pass
        doc_date = prev_month_str if is_prev_month else today_str
        post_date = prev_month_str if is_prev_month else today_str

        # Skip rows without error messages unless 901/902, preserving them as SKIPPED_NO_MESSAGE
        if not msg and mvt not in ['901', '902']:
            plan_rows.append({
                'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                'From_SLoc': from_st, 'To_SLoc': to_st, 'Transfer_Qty': qty,
                'MB1B_Mvt': '311', 'Doc_Date': doc_date, 'Post_Date': post_date,
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': 'SKIPPED_NO_MESSAGE',
                'Persistence_Checked': False,
                'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                'Persistent_Qty': 0.0,
                'Reason': 'Blank ZTCODE_GI_ERR error message - excluded and preserved pending verification (SKIPPED_NO_MESSAGE)'
            })
            continue

        if mvt in ['901', '902']:
            plan_rows.append({
                'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                'From_SLoc': '', 'To_SLoc': deficit_loc, 'Transfer_Qty': qty,
                'MB1B_Mvt': '311', 'Doc_Date': doc_date, 'Post_Date': post_date,
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': f'EXCLUDED_MVT_{mvt}',
                'Reason': f'Movement type {mvt} (scrap/disposal) - excluded from automated processing'
            })
            continue

        if mvt not in ['311', '312']:
            plan_rows.append({
                'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                'From_SLoc': '', 'To_SLoc': deficit_loc, 'Transfer_Qty': qty,
                'MB1B_Mvt': '311', 'Doc_Date': doc_date, 'Post_Date': post_date,
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': 'UNKNOWN_MVT',
                'Reason': f'Unsupported movement type: {mvt}'
            })
            continue

        if not is_valid_target_sloc(from_st) or not is_valid_target_sloc(to_st):
            plan_rows.append({
                'Material': mat, 'Mvt_Original': mvt, 'Deficit_Loc': deficit_loc,
                'From_SLoc': '', 'To_SLoc': deficit_loc, 'Transfer_Qty': qty,
                'MB1B_Mvt': '311', 'Doc_Date': doc_date, 'Post_Date': post_date,
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': 'EXCLUDED_INVALID_SLOC',
                'Reason': f'Unauthorized storage location ({from_st} -> {to_st}) - excluded'
            })
            continue

        src, dst = gi_flow(mvt, from_st, to_st)
        if not src or not dst:
            continue

        actionable.append({
            'idx': idx,
            'mvt': mvt,
            'from_st': from_st,
            'to_st': to_st,
            'src': src,
            'dst': dst,
            'qty': qty,
            'deficit_loc': deficit_loc,
            'doc_date': doc_date,
            'post_date': post_date,
            'msg': msg
        })

    if not actionable:
        return plan_rows

    # 3. Setup Stock Matrix across all involved storage locations
    mat_stock = stock_comparison[stock_comparison['Material'] == mat] if stock_comparison is not None and not stock_comparison.empty else pd.DataFrame()
    all_slocs = set()
    current_sap = {}
    current_wms = {}
    current_surplus = {}

    if not mat_stock.empty:
        for _, s_row in mat_stock.iterrows():
            sloc = str(s_row['SLoc']).upper()
            if is_valid_target_sloc(sloc):
                all_slocs.add(sloc)
                current_sap[sloc] = float(s_row.get('SAP_Stock', 0.0))
                current_wms[sloc] = float(s_row.get('WMS_Stock', 0.0))
            current_surplus[sloc] = float(s_row.get('Surplus', 0.0))

    for e in actionable:
        all_slocs.add(e['src'])
        all_slocs.add(e['dst'])

    for s in all_slocs:
        if s not in current_sap: current_sap[s] = 0.0
        if s not in current_wms: current_wms[s] = 0.0
        if s not in current_surplus: current_surplus[s] = current_sap[s] - current_wms[s]

    # Ensure all audited warehouse slocs are tracked in stock dicts
    for s in WAREHOUSE_SLOCS:
        if s not in current_sap: current_sap[s] = 0.0
        if s not in current_wms: current_wms[s] = 0.0
        if s not in current_surplus: current_surplus[s] = 0.0

    initial_sap_snapshot = {k: float(v) for k, v in current_sap.items() if abs(float(v)) > 1e-6}
    initial_wms_snapshot = {k: float(v) for k, v in current_wms.items() if abs(float(v)) > 1e-6}
    initial_sap_total = sum(initial_sap_snapshot.values())
    initial_wms_total = sum(initial_wms_snapshot.values())

    # Extract previous snapshot stock by storage location for persistence guard (028-2)
    prev_sap_by_sloc = {}
    prev_wms_by_sloc = {}
    if prev_mb52_df is not None and not prev_mb52_df.empty:
        p_m_df = prev_mb52_df[prev_mb52_df['Material'] == mat]
        for _, pr in p_m_df.iterrows():
            prev_sap_by_sloc[str(pr['SLoc']).upper()] = float(pr['SAP_Stock'])
    if prev_wms_df is not None and not prev_wms_df.empty:
        p_w_df = prev_wms_df[prev_wms_df['Material'] == mat]
        for _, pr in p_w_df.iterrows():
            prev_wms_by_sloc[str(pr['SLoc']).upper()] = float(pr['WMS_Stock'])

    # 4. Check initial auto-clearing by current SAP stock
    base_state, init_cleared_local = run_cascade(current_sap, actionable)
    unresolved_indices = [i for i in range(len(actionable)) if i not in init_cleared_local]

    for i in init_cleared_local:
        e = actionable[i]
        plan_rows.append({
            'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': e['deficit_loc'],
            'From_SLoc': '', 'To_SLoc': e['deficit_loc'], 'Transfer_Qty': e['qty'],
            'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
            'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
            'Action': 'SKIP', 'Status': 'AUTO_CLEARED_BY_CURRENT_STOCK',
            'Persistence_Checked': True,
            'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
            'Persistent_Qty': 0.0,
            'Reason': 'Auto-resolvable by current SAP stock (no additional transfer posting required)'
        })

    eff_cooldown_map = d110_cooldown_map or {}
    d110_cooldown_blocked_by_dloc = {}
    d115_gr_blocked_by_dloc = {}
    matched_rule_for_material: Optional[str] = None

    # 5. Iterative Cascade Simulation & Transfer Selection
    while unresolved_indices:
        if matched_rule_for_material in ("D97", "D113", "D115"):
            # #221 M-2: Same-cycle roundtrip/pump rules execute at most once per material per run;
            # never match two rules on the same material in one run.
            break
        remaining_errors = [actionable[i] for i in unresolved_indices]
        deficit_locations = sorted(list(set(e['deficit_loc'] for e in remaining_errors)))

        # Candidate source locations: S not in deficit_locations, SAP_Stock > 0
        # Replaces legacy "Matched Warehouse Protection" rule with D18/D19 evaluate_settled_state_accuracy
        candidate_sources = [
            s for s in all_slocs
            if (
                s not in deficit_locations
                or (
                    s in D113_ALLOWED_DEST_SLOCS
                    and any(e.get('src') == 'SL00' and e.get('dst') == s for e in remaining_errors)
                )
                or (
                    (s in D115_WMS_DEST_SLOCS or _is_d115_sp_dest(s))
                    and any(e.get('dst') == s and e.get('src') != s for e in remaining_errors)
                )
            )
            and current_sap.get(s, 0.0) > 0
        ]
        if not candidate_sources:
            # Fallback: check any location with net excess stock above its own pending outbound demand
            candidate_sources = [
                s for s in all_slocs
                if (current_sap.get(s, 0.0) - sum(e['qty'] for e in remaining_errors if e['src'] == s) > 0)
            ]

        if not candidate_sources:
            break

        best_option = None
        best_d113_option = None

        for d_loc in deficit_locations:
            d_errors = [e for e in remaining_errors if e['src'] == d_loc]
            curr_avail_d = base_state.get(d_loc, 0.0)
            total_req_d = sum(e['qty'] for e in d_errors)
            net_needed_d = max(0.0, total_req_d - curr_avail_d)

            # Test candidate transfer quantities: individual error quantities and cumulative requirements
            candidate_quantities = set()
            for e in d_errors:
                candidate_quantities.add(e['qty'])
            if net_needed_d > 0:
                candidate_quantities.add(net_needed_d)

            for s_loc in candidate_sources:
                # D110 (#197) Rule 2: 24-hour cooldown after >=3 consecutive failures on (material, from_sloc, to_sloc)
                if (mat, s_loc, d_loc) in eff_cooldown_map:
                    d110_cooldown_blocked_by_dloc.setdefault(d_loc, eff_cooldown_map[(mat, s_loc, d_loc)])
                    continue

                # D71/D73: Check if movement from s_loc to d_loc is blocked by intersecting GR/GI errors
                # D92: 01_JOB candidate movement is by definition heading to the deficit location of its own GI error,
                # so it must NOT be self-blocked by active_gi_errors. Only active_gr_errors (D71/D73) are checked.
                if active_gr_errors:
                    is_ztcode_stock_rec_blocked, _, _, _, _ = check_movement_ztcode_stock_rec_block(
                        mat=mat,
                        src_sloc=s_loc,
                        dst_sloc=d_loc,
                        active_gr_errors=active_gr_errors or {},
                        active_gi_errors={}
                    )
                    if is_ztcode_stock_rec_blocked:
                        continue

                surplus_s = current_surplus.get(s_loc, 0.0)
                avail_s = current_sap.get(s_loc, 0.0)

                # Persistence Guard Check (028-2 / D37)
                persist_res = check_transfer_persistence(
                    material=mat,
                    source_sloc=s_loc,
                    dest_sloc=d_loc,
                    candidate_qty=avail_s,
                    current_wms_by_sloc=current_wms,
                    current_sap_by_sloc=current_sap,
                    prev_wms_by_sloc=prev_wms_by_sloc if prev_snapshot_time else None,
                    prev_sap_by_sloc=prev_sap_by_sloc if prev_snapshot_time else None,
                    prev_snapshot_time=prev_snapshot_time,
                    is_prev_snapshot_valid=(prev_snapshot_time is not None)
                )

                is_312_prime_candidate = False
                is_d113_candidate = False
                is_d115_candidate = False
                matched_312_err = None
                wh_gap_s = 0.0
                d113_elig = (
                    evaluate_d113_sl00_dest_roundtrip(
                        mat=mat,
                        s_loc=s_loc,
                        d_loc=d_loc,
                        remaining_errors=remaining_errors,
                        current_sap=current_sap,
                        current_wms=current_wms,
                        prev_sap_by_sloc=prev_sap_by_sloc if prev_snapshot_time else None,
                        prev_wms_by_sloc=prev_wms_by_sloc if prev_snapshot_time else None,
                        prev_snapshot_time=prev_snapshot_time,
                        active_gr_errors=active_gr_errors,
                        protected_gr_materials=protected_gr_materials,
                        gr_valid=gr_valid,
                        prior_completed_runs=d109_prior_runs,
                    )
                    if (d_loc == 'SL00' and s_loc in D113_ALLOWED_DEST_SLOCS)
                    else {"eligible": False}
                )
                if d113_elig.get("eligible"):
                    is_d113_candidate = True

                d115_elig = (
                    evaluate_d115_dest_pump_clear(
                        mat=mat,
                        s_loc=s_loc,
                        d_loc=d_loc,
                        curr_avail_d=curr_avail_d,
                        remaining_errors=remaining_errors,
                        current_sap=current_sap,
                        current_wms=current_wms,
                        prev_sap_by_sloc=prev_sap_by_sloc if prev_snapshot_time else None,
                        prev_wms_by_sloc=prev_wms_by_sloc if prev_snapshot_time else None,
                        prev_snapshot_time=prev_snapshot_time,
                        active_gr_errors=active_gr_errors,
                        protected_gr_materials=protected_gr_materials,
                        gr_valid=gr_valid,
                        prior_completed_runs=d109_prior_runs,
                        require_prev_snapshot=require_prev_snapshot,
                    )
                    if ((s_loc in D115_WMS_DEST_SLOCS or _is_d115_sp_dest(s_loc)) and d_loc != s_loc)
                    else {"eligible": False}
                )
                if d115_elig.get("eligible"):
                    is_d115_candidate = True
                elif d115_elig.get("gr_blocked"):
                    d115_gr_blocked_by_dloc.setdefault(d_loc, {"dest_b": s_loc, **d115_elig})

                if s_loc in WAREHOUSE_SLOCS:
                    effective_surplus_s = persist_res.source_persistent_surplus
                    if effective_surplus_s <= 0:
                        wh_gap_s = current_wms.get(s_loc, 0.0) - current_sap.get(s_loc, 0.0)
                        if is_d113_candidate:
                            is_312_prime_candidate = True
                            matched_312_err = d113_elig["matched_errors"][0]
                        elif is_d115_candidate:
                            is_312_prime_candidate = True
                            matched_312_err = d115_elig["matched_errors"][0]
                        elif wh_gap_s > 0:
                            prev_gap_ok = True
                            if prev_snapshot_time:
                                prev_gap_s = prev_wms_by_sloc.get(s_loc, 0.0) - prev_sap_by_sloc.get(s_loc, 0.0)
                                if prev_gap_s <= 0:
                                    prev_gap_ok = False
                            
                            if prev_gap_ok:
                                for err in remaining_errors:
                                    err_src = err.get('src')
                                    err_dst = err.get('dst')
                                    if err_dst == s_loc and err_src == d_loc:
                                        msg_lower = str(err.get('msg', '')).lower()
                                        is_pure_deficit = (
                                            'deficit' in msg_lower
                                            or 'shortage' in msg_lower
                                            or 'insufficient' in msg_lower
                                            or '부족' in msg_lower
                                        )
                                        has_blocking_err = any(kw in msg_lower for kw in [
                                            'lock', 'blocked', 'closed', 'period', 'customiz', 'missing', 'account', 'valuation'
                                        ])
                                        if is_pure_deficit and not has_blocking_err:
                                            is_312_prime_candidate = True
                                            matched_312_err = err
                                            break
                        if not is_312_prime_candidate:
                            continue
                        max_usable_s = avail_s
                    else:
                        max_usable_s = avail_s if (is_d113_candidate or is_d115_candidate) else min(avail_s, persist_res.persistent_qty)
                else:
                    # #222 Part A: When s_loc is SP* and d_loc -> s_loc is an outbound shortage error from SL00~SL04,
                    # pumping SP* -> A to return to SP* is strictly governed by D115 (must not bypass WMS(A)<=SAP(A) via D70!)
                    if (
                        _is_d115_sp_dest(s_loc)
                        and d_loc in D115_SP_ALLOWED_SOURCE_SLOCS
                        and any(e.get('src') == d_loc and e.get('dst') == s_loc for e in d_errors)
                        and not is_d115_candidate
                    ):
                        continue
                    effective_surplus_s = surplus_s
                    max_usable_s = avail_s

                if is_d115_candidate:
                    candidate_quantities_s = {float(d115_elig["pump_qty"])}
                elif is_312_prime_candidate:
                    candidate_quantities_s = set()
                    for e in d_errors:
                        if e.get('dst') == s_loc:
                            needed_e = max(0.0, e['qty'] - curr_avail_d)
                            if needed_e > 0:
                                candidate_quantities_s.add(needed_e)
                    s_return_total = sum(e['qty'] for e in d_errors if e.get('dst') == s_loc)
                    if s_return_total > curr_avail_d + 1e-6:
                        candidate_quantities_s.add(s_return_total - curr_avail_d)
                    if net_needed_d > 0 and all(e.get('dst') == s_loc for e in d_errors):
                        candidate_quantities_s.add(net_needed_d)
                else:
                    candidate_quantities_s = set(candidate_quantities)
                    if is_d113_candidate:
                        candidate_quantities_s.update(d113_elig.get("feasible_qtys") or [])
                    for e in d_errors:
                        if e.get('dst') == s_loc and curr_avail_d > 0 and e['qty'] > curr_avail_d:
                            candidate_quantities_s.add(e['qty'] - curr_avail_d)

                # D94: Perfect accuracy flush & return candidate quantity (Thread 124, 125, 126)
                # If candidate source S is an audited warehouse in SAP surplus (SAP > WMS),
                # and S is the destination (dst) of error(s) at D:
                inflow_s = sum(e['qty'] for e in d_errors if e.get('dst') == s_loc)
                q_star = None
                sap_s = current_sap.get(s_loc, 0.0)
                wms_s = current_wms.get(s_loc, 0.0)
                if s_loc in WAREHOUSE_SLOCS and inflow_s > 0 and (sap_s > wms_s):
                    calc_q = sap_s + inflow_s - wms_s
                    # Conditions: net_needed_d <= q* <= avail_s, q* > 0
                    if net_needed_d <= calc_q <= avail_s and calc_q > 0:
                        net_drain = calc_q - inflow_s
                        # Net drain from S must not exceed persistent surplus (if positive drain)
                        if net_drain <= 0 or net_drain <= persist_res.persistent_qty + 1e-6:
                            q_star = calc_q
                            candidate_quantities_s.add(q_star)

                for raw_q in candidate_quantities_s:
                    if q_star is not None and abs(raw_q - q_star) < 1e-6:
                        test_q = q_star
                    else:
                        test_q = min(raw_q, max_usable_s)
                    if test_q <= 0:
                        continue

                    # Simulate transfer S -> D
                    test_state = dict(base_state)
                    test_state[s_loc] -= test_q
                    test_state[d_loc] = test_state.get(d_loc, 0.0) + test_q

                    final_state, cascade_cleared_local = run_cascade(test_state, remaining_errors)
                    cleared_count = len(cascade_cleared_local)
                    if cleared_count == 0:
                        continue

                    # D70 & D97 Pump-Priming / Roundtrip Guard (Condition d & c-3):
                    # If source S was granted exception (started with SAP < WMS deficit),
                    # the settled state MUST leave S with >= its initial stock (net gain >= 0),
                    # at least 1 GI error returning to S must be cleared, and
                    # warehouse gap must be >= (error_qty - test_q).
                    if is_312_prime_candidate:
                        if final_state.get(s_loc, 0.0) < base_state.get(s_loc, 0.0) - 1e-6:
                            continue
                        cleared_return_errs = [
                            remaining_errors[i] for i in cascade_cleared_local
                            if remaining_errors[i].get('src') == d_loc and remaining_errors[i].get('dst') == s_loc
                        ]
                        if not cleared_return_errs:
                            continue
                        req_err_qty = sum(e.get('qty', 0.0) for e in cleared_return_errs)
                        if is_d115_candidate:
                            # D115 Rule 6 (B_WMS): B's |SAP - WMS| must strictly decrease & no negative book stock at A or B
                            abs_b_before = abs(current_sap.get(s_loc, 0.0) - current_wms.get(s_loc, 0.0))
                            abs_b_after = abs(final_state.get(s_loc, 0.0) - current_wms.get(s_loc, 0.0))
                            if abs_b_after >= abs_b_before - 1e-6:
                                continue
                            if test_state.get(s_loc, 0.0) < -1e-6 or final_state.get(d_loc, 0.0) < -1e-6:
                                continue
                        elif not is_d113_candidate:
                            # Non-D115 / non-D113 deficit-warehouse priming on non-SL00 destinations is restricted to zero-gain roundtrips (D97).
                            # d_loc == 'SL00' preserves main's gate so D37/D109 holds pending SL00 interface rows cleanly.
                            if d_loc != 'SL00' and abs(final_state.get(s_loc, 0.0) - base_state.get(s_loc, 0.0)) > 1e-6:
                                continue
                            if wh_gap_s < (req_err_qty - test_q) - 1e-6:
                                continue

                    # #222 Part A Rule 6 (A_WMS): When B is SP*, A's |SAP - WMS| must not worsen & no negative stock at A or B
                    if is_d115_candidate and _is_d115_sp_dest(s_loc):
                        abs_a_before = abs(current_sap.get(d_loc, 0.0) - current_wms.get(d_loc, 0.0))
                        abs_a_after = abs(final_state.get(d_loc, 0.0) - current_wms.get(d_loc, 0.0))
                        if abs_a_after > abs_a_before + 1e-6:
                            continue
                        if test_state.get(s_loc, 0.0) < -1e-6 or final_state.get(d_loc, 0.0) < -1e-6:
                            continue

                    # Evaluate using D18 & D19 Objective Function (settled state ZTCODE_STOCK_REC accuracy)
                    is_approved, eval_details = evaluate_settled_state_accuracy(
                        wms_stock_by_sloc=current_wms,
                        sap_initial_by_sloc=current_sap,
                        sap_final_by_sloc=final_state,
                        sap_intermediate_by_sloc=test_state,
                        target_slocs=WAREHOUSE_SLOCS
                    )

                    # Strict Guardrail: Unapproved candidates are excluded from competition (prevents shadowing approved alternatives)
                    if not is_approved:
                        continue

                    score_delta = eval_details['score_delta']
                    mean_final = eval_details['mean_final']
                    is_wh_surplus = 1 if (s_loc in WAREHOUSE_SLOCS and effective_surplus_s > 0) else 0

                    # Multi-objective score tuple among APPROVED candidates:
                    # 1. Number of cleared errors (maximize)
                    # 2. Prefer warehouse surplus over external SP31/SP11: is_wh_surplus
                    # 3. Accuracy score delta (maximize)
                    # 4. Minimal transfer quantity: -test_q (avoid over-moving)
                    # 5. Source surplus: effective_surplus_s
                    score = (
                        cleared_count,
                        is_wh_surplus,
                        score_delta,
                        -test_q,
                        effective_surplus_s
                    )

                    # D113 Rule 6 (#211): Check exact settled-state variance delta == 0 on X and SL00
                    is_d113_roundtrip_cand = False
                    if is_d113_candidate and d_loc == 'SL00' and s_loc in D113_ALLOWED_DEST_SLOCS:
                        var_x_before = current_sap.get(s_loc, 0.0) - current_wms.get(s_loc, 0.0)
                        var_x_after = final_state.get(s_loc, 0.0) - current_wms.get(s_loc, 0.0)
                        var_00_before = current_sap.get('SL00', 0.0) - current_wms.get('SL00', 0.0)
                        var_00_after = final_state.get('SL00', 0.0) - current_wms.get('SL00', 0.0)
                        has_sl00_return = any(
                            remaining_errors[i].get('src') == 'SL00' and remaining_errors[i].get('dst') == s_loc
                            for i in cascade_cleared_local
                        )
                        if (
                            has_sl00_return
                            and abs(var_x_after - var_x_before) <= 1e-6
                            and abs(var_00_after - var_00_before) <= 1e-6
                            and abs(score_delta) <= 1e-6
                        ):
                            is_d113_roundtrip_cand = True

                    has_return_to_s_cand = any(
                        remaining_errors[i].get('src') == d_loc and remaining_errors[i].get('dst') == s_loc
                        for i in cascade_cleared_local
                    )
                    net_gain_s_cand = final_state.get(s_loc, 0.0) - base_state.get(s_loc, 0.0)
                    is_d97_roundtrip_cand = (
                        d_loc != 'SL00'
                        and not is_d113_roundtrip_cand
                        and not is_d115_candidate
                        and has_return_to_s_cand
                        and abs(net_gain_s_cand) <= 1e-6
                        and abs(score_delta) <= 1e-6
                    )
                    if is_d97_roundtrip_cand:
                        cand_rule_id = 'D97'
                    elif is_d113_roundtrip_cand:
                        cand_rule_id = 'D113'
                    elif is_d115_candidate:
                        cand_rule_id = 'D115'
                    else:
                        cand_rule_id = 'D70'

                    # #221 M-2: The same material must not be matched by two rules in one run
                    if matched_rule_for_material is not None and cand_rule_id != matched_rule_for_material:
                        continue

                    rank_score = (RULE_PRECEDENCE_RANK[cand_rule_id],) + score

                    cand_dict = {
                        'rule_id': cand_rule_id,
                        'rank_score': rank_score,
                        'score': score,
                        'source': s_loc,
                        'dest': d_loc,
                        'qty': test_q,
                        'cleared_local': cascade_cleared_local,
                        'is_approved': is_approved,
                        'eval_details': eval_details,
                        'surplus_s': effective_surplus_s,
                        'final_state': final_state,
                        'persistent_qty': test_q,
                        'is_d113_roundtrip': is_d113_roundtrip_cand,
                        'is_d115_dest_pump': bool(is_d115_candidate),
                        'd115_meta': d115_elig if is_d115_candidate else None,
                    }
                    if is_d113_roundtrip_cand and (best_d113_option is None or rank_score > best_d113_option['rank_score']):
                        best_d113_option = cand_dict
                    if best_option is None or rank_score > best_option['rank_score']:
                        best_option = cand_dict

        # Check if best_option is valid (clears >= 1 error and strictly approved by objective function)
        # #211 D113 Anti-Shadowing: If best_option targets SL00 as a non-D113 candidate that will be blocked by D37/D109,
        # promote best_d113_option so an unapproved SL00 transfer (e.g. SP31->SL00) never shadows a valid D113 roundtrip!
        if (
            best_option
            and best_option['is_approved']
            and best_option['dest'] == 'SL00'
            and not best_option.get('is_d113_roundtrip')
            and best_d113_option is not None
        ):
            _cand_cl = [unresolved_indices[idx] for idx in best_option['cleared_local']]
            _cand_root = next((actionable[idx] for idx in _cand_cl if actionable[idx]['src'] == 'SL00'), actionable[_cand_cl[0]])
            _cand_sl00_errs = [actionable[idx] for idx in _cand_cl if actionable[idx].get('deficit_loc') == 'SL00']
            _pre_d109 = evaluate_d109_sl00_guard_release(
                mat=mat,
                s_loc=best_option['source'],
                planned_q=best_option['qty'],
                root_e=_cand_root,
                cleared_sl00_errors=_cand_sl00_errs,
                current_sap=current_sap,
                current_wms=current_wms,
                source_persistent_surplus=float(best_option.get('surplus_s', 0.0)),
                prior_completed_runs=d109_prior_runs,
                active_gr_errors=active_gr_errors,
                protected_gr_materials=protected_gr_materials,
                gr_valid=gr_valid,
            )
            if not _pre_d109.get('released'):
                best_option = best_d113_option

        if best_option and best_option['is_approved']:
            s_loc = best_option['source']
            d_loc = best_option['dest']
            q = best_option['qty']
            cleared_actionable_indices = [unresolved_indices[idx] for idx in best_option['cleared_local']]
            eval_det = best_option['eval_details']
            score_delta = eval_det['score_delta']
            mean_init = eval_det['mean_init']
            mean_final = eval_det['mean_final']

            # Find root error (first error originating at d_loc)
            root_idx = None
            for idx in cleared_actionable_indices:
                if actionable[idx]['src'] == d_loc:
                    root_idx = idx
                    break
            if root_idx is None:
                root_idx = cleared_actionable_indices[0]

            root_e = actionable[root_idx]
            is_chain = len(cleared_actionable_indices) > 1

            is_return_to_s = any(
                actionable[idx].get('src') == d_loc and actionable[idx].get('dst') == s_loc
                for idx in cleared_actionable_indices
            )
            net_gain_s = best_option['final_state'].get(s_loc, 0.0) - base_state.get(s_loc, 0.0)
            is_zero_gain_roundtrip = is_return_to_s and abs(net_gain_s) < 1e-6 and abs(score_delta) < 1e-6
            is_d113_roundtrip = bool(best_option.get('is_d113_roundtrip'))
            is_d115_dest_pump = bool(best_option.get('is_d115_dest_pump'))

            if is_d113_roundtrip:
                reason_root = (
                    f"ROUNDTRIP_CLEAR_VIA_DEST_D113: Step 1 {s_loc}->SL00 {q:,.1f} EA + Step 2 same-cycle "
                    f"{root_e['mvt']} GI clear (SL00->{s_loc}, {len(cleared_actionable_indices)} item(s)) | "
                    f"SL00 WMS=SAP={current_sap.get('SL00', 0.0):,.0f} (2x verified), 3-file GR=0, "
                    f"{s_loc} & SL00 net variance delta 0 (Accuracy {mean_init:.1f}% -> {mean_final:.1f}%)"
                )
            elif is_d115_dest_pump:
                _b_sap_i = current_sap.get(s_loc, 0.0)
                _b_wms_i = current_wms.get(s_loc, 0.0)
                _b_sap_f = best_option['final_state'].get(s_loc, 0.0)
                _a_sap_i = current_sap.get(d_loc, 0.0)
                _a_wms_i = current_wms.get(d_loc, 0.0)
                _a_sap_f = best_option['final_state'].get(d_loc, 0.0)
                _err_sum_115 = float((best_option.get('d115_meta') or {}).get('error_sum', q))
                _mvt312_note = " (312 reverse pump-priming transfer)" if root_e['mvt'] == '312' else ""
                _ev_tag_115 = str((best_option.get('d115_meta') or {}).get('evidence', 'B_WMS'))
                if _ev_tag_115 == 'A_WMS':
                    reason_root = (
                        f"DEST_PUMP_CLEAR_D115{_mvt312_note}: Step 1 deficit-only pump {s_loc}->{d_loc} {q:,.1f} EA + Step 2 same-cycle "
                        f"{root_e['mvt']} GI clear ({d_loc}->{s_loc} {_err_sum_115:,.1f} EA, {len(cleared_actionable_indices)} item(s)) | "
                        f"evidence=A_WMS, {d_loc} WMS<=SAP ({_a_wms_i:,.0f}<={_a_sap_i:,.0f}, 2x verified), 3-file GR=0, "
                        f"{d_loc} SAP {_a_sap_i:,.0f}->{_a_sap_f:,.0f} (|SAP-WMS| {abs(_a_sap_i - _a_wms_i):,.0f}->{abs(_a_sap_f - _a_wms_i):,.0f}), "
                        f"{s_loc} SAP {_b_sap_i:,.0f}->{_b_sap_f:,.0f} (Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
                    )
                else:
                    reason_root = (
                        f"DEST_PUMP_CLEAR_D115{_mvt312_note}: Step 1 deficit-only pump {s_loc}->{d_loc} {q:,.1f} EA + Step 2 same-cycle "
                        f"{root_e['mvt']} GI clear ({d_loc}->{s_loc} {_err_sum_115:,.1f} EA, {len(cleared_actionable_indices)} item(s)) | "
                        f"evidence={_ev_tag_115}, {s_loc} WMS-SAP=+{_b_wms_i - _b_sap_i:,.0f} (>=0.9x {_err_sum_115:,.0f}, 2x verified), 3-file GR=0, "
                        f"{s_loc} SAP {_b_sap_i:,.0f}->{_b_sap_f:,.0f} (|SAP-WMS| {abs(_b_sap_i - _b_wms_i):,.0f}->{abs(_b_sap_f - _b_wms_i):,.0f}, "
                        f"Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
                    )
            elif is_zero_gain_roundtrip:
                reason_root = (
                    f"Roundtrip priming clear (D97 ROUNDTRIP_CLEAR): Transfer {q:,.1f} EA from {s_loc} to {d_loc} "
                    f"➔ clears {root_e['mvt']} GI error ({root_e['qty']:,.1f} EA, {d_loc}->{s_loc}) and restores {s_loc} "
                    f"(Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
                )
            elif is_chain:
                reason_root = f"Chained cascade optimization: Transfer {q:,.1f} EA from surplus SLoc ({s_loc}) to deficit SLoc ({d_loc}) ➔ unblocks {len(cleared_actionable_indices)-1} downstream item(s) (Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
            elif root_e['mvt'] == '312' and root_e.get('dst') == s_loc:
                reason_root = f"312 reverse pump-priming transfer (D70): Transfer {q:,.1f} EA from {s_loc} to {d_loc} ➔ clears 312 GI error ({root_e['qty']:,.1f} EA) and returns stock to {s_loc} (Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
            else:
                reason_root = f"{root_e['mvt']} error resolution: Transfer stock from {s_loc} (avail {best_option['surplus_s']:,.1f} EA) to {d_loc} (Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"

            # Interim Guardrail 1 (028-1 / D37): Skip transfers to SL00 unless D113 verified (SL00 WMS==SAP x2 + 3-file GR==0)
            # or D109 4-run guard release applies. When SL00 WMS > SAP or GR >= 1, D37 strictly holds even for roundtrips!
            action_root = 'TRANSFER_POSTING'
            if is_d113_roundtrip:
                status_root = 'ROUNDTRIP_CLEAR_VIA_DEST_D113'
            elif is_d115_dest_pump:
                status_root = 'DEST_PUMP_CLEAR_D115'
            elif is_zero_gain_roundtrip:
                status_root = 'ROUNDTRIP_CLEAR'
            else:
                status_root = 'READY_FOR_EXECUTION'
            d109_meta = None
            sim_final_state = dict(best_option['final_state'])
            if d_loc == 'SL00' and not is_d113_roundtrip and not is_d115_dest_pump:
                cleared_sl00_errs = [
                    actionable[idx] for idx in cleared_actionable_indices if actionable[idx].get('deficit_loc') == 'SL00'
                ]
                d109_eval = evaluate_d109_sl00_guard_release(
                    mat=mat,
                    s_loc=s_loc,
                    planned_q=q,
                    root_e=root_e,
                    cleared_sl00_errors=cleared_sl00_errs,
                    current_sap=current_sap,
                    current_wms=current_wms,
                    source_persistent_surplus=float(best_option.get('surplus_s', 0.0)),
                    prior_completed_runs=d109_prior_runs,
                    active_gr_errors=active_gr_errors,
                    protected_gr_materials=protected_gr_materials,
                    gr_valid=gr_valid,
                )
                d109_meta = d109_eval
                can_release_d109 = bool(d109_eval['released'])
                capped_q = float(d109_eval['capped_qty'])

                if can_release_d109 and abs(capped_q - q) > 1e-6:
                    test_state_c = dict(base_state)
                    test_state_c[s_loc] -= capped_q
                    test_state_c[d_loc] = test_state_c.get(d_loc, 0.0) + capped_q
                    final_state_c, cascade_cleared_c = run_cascade(test_state_c, remaining_errors)
                    is_app_c, eval_det_c = evaluate_settled_state_accuracy(
                        wms_stock_by_sloc=current_wms,
                        sap_initial_by_sloc=current_sap,
                        sap_final_by_sloc=final_state_c,
                        sap_intermediate_by_sloc=test_state_c,
                        target_slocs=WAREHOUSE_SLOCS,
                    )
                    if not is_app_c:
                        can_release_d109 = False
                        d109_eval['hold_reason_summary'] = "Capped qty failed settled-state accuracy objective"
                    elif len(cascade_cleared_c) == 0 and eval_det_c['score_delta'] < 0.001:
                        can_release_d109 = False
                        d109_eval['hold_reason_summary'] = "Partial capped qty did not meet D77 +0.1%p accuracy gain"
                    else:
                        q = capped_q
                        sim_final_state = dict(final_state_c)
                        mean_init = eval_det_c['mean_init']
                        mean_final = eval_det_c['mean_final']
                        score_delta = eval_det_c['score_delta']
                        if cascade_cleared_c:
                            cleared_actionable_indices = [unresolved_indices[i] for i in cascade_cleared_c]
                        else:
                            cleared_actionable_indices = [root_idx]

                if d109_eval.get('already_released'):
                    action_root = 'SKIP'
                    status_root = 'SKIPPED_D109_ALREADY_RELEASED'
                    hold_note = d109_eval.get('hold_reason_summary') or 'Prior guard-release posting already executed'
                    reason_root = (
                        f"D109 safeguard (duplicate-posting block): {hold_note} — retry blocked (alert only, human review required)"
                    )
                elif can_release_d109:
                    action_root = 'TRANSFER_POSTING'
                    status_root = 'GUARD_RELEASED_D109'
                    q = capped_q if abs(capped_q - q) <= 1e-6 else q
                    reason_root = (
                        f"GUARD_RELEASED_D109: SL00 hold persisted {d109_eval['consec_runs']} consecutive runs, "
                        f"{d109_eval['gr_check_label']} verified, "
                        f"SL00 WMS-SAP gap non-decreasing ({d109_eval['gap_trace_str']} EA) "
                        f"➔ {s_loc}->SL00 {q:,.1f} EA guard-release transfer "
                        f"(Accuracy {mean_init:.1f}% -> {mean_final:.1f}%, &Delta;Score={score_delta:+.1f}%p)"
                    )
                else:
                    action_root = 'SKIP'
                    status_root = 'SKIPPED_PENDING_INTERFACE_SL00'
                    hold_note = d109_eval.get('hold_reason_summary') or 'Criteria not met'
                    reason_root = (
                        f"Arrival transfer to receiving SLoc SL00 ({s_loc}->SL00) - held to prevent duplicate reflection of pending interface "
                        f"(D37/028 guard | D109 check: consecutive {d109_eval['consec_runs']}/{D109_SL00_GUARD_RELEASE_RUNS} runs, "
                        f"{d109_eval['gr_check_label']}, reason: {hold_note})"
                    )





            row_entry = {
                'Material': mat, 'Mvt_Original': root_e['mvt'], 'Deficit_Loc': d_loc,
                'From_SLoc': s_loc, 'To_SLoc': d_loc, 'Transfer_Qty': q,
                'MB1B_Mvt': '311', 'Doc_Date': root_e['doc_date'], 'Post_Date': root_e['post_date'],
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': action_root, 'Status': status_root,
                'Accuracy_Before': mean_init, 'Accuracy_After': mean_final, 'Delta_Accuracy': score_delta,
                'Persistence_Checked': True,
                'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                'Persistent_Qty': q,
                'Reason': reason_root
            }
            if d109_meta is not None:
                row_entry.update({
                    'Error_From': root_e.get('src', d_loc),
                    'Error_To': root_e.get('dst', ''),
                    'Error_Qty': float(d109_meta['gi_err_qty']),
                    'D109_Consec_Runs': int(d109_meta['consec_runs']),
                    'D109_Active_GR_Count': int(d109_meta['gr_count']),
                    'SL00_WMS_SAP_Gap': float(d109_meta['current_sl00_gap']),
                    'D109_Already_Released': bool(d109_meta.get('already_released', False)),
                    'D109_Suspected_Unposted_GR': bool(d109_meta.get('suspected_unposted_gr_f', False)),
                })
            if status_root in ('ROUNDTRIP_CLEAR', 'ROUNDTRIP_CLEAR_VIA_DEST_D113', 'DEST_PUMP_CLEAR_D115'):
                cleared_return_qty = sum(
                    float(actionable[idx].get('qty', 0.0))
                    for idx in cleared_actionable_indices
                    if actionable[idx].get('src') == d_loc and actionable[idx].get('dst') == s_loc
                )
                row_entry.update({
                    'Error_From': root_e.get('src', d_loc),
                    'Error_To': root_e.get('dst', s_loc),
                    'Error_Qty': float(cleared_return_qty if cleared_return_qty > 0 else root_e.get('qty', q)),
                    'SAP_By_SLoc': initial_sap_snapshot,
                    'WMS_By_SLoc': initial_wms_snapshot,
                    'SAP_Total': initial_sap_total,
                    'WMS_Total': initial_wms_total,
                    'Total_Gap': initial_wms_total - initial_sap_total,
                })
                if signal_db_path is not None and abs(initial_sap_total - initial_wms_total) > 1e-6:
                    try:
                        import importlib.util as _ilu
                        _spec = _ilu.spec_from_file_location("_job01_db_mgr", BASE_DIR / "db_manager.py")
                        _dbm = _ilu.module_from_spec(_spec)
                        _spec.loader.exec_module(_dbm)
                        _dbm.record_plan_stock_gap_signals(
                            run_id=run_id or datetime.now().strftime("PLAN_%Y%m%d_%H%M%S"),
                            plan_rows=[row_entry],
                            db_path=Path(signal_db_path)
                        )
                    except Exception as e_sig:
                        logger.warning(f"Could not record GI_STOCK_GAP_SIGNALS in resolve_material_errors_unified: {e_sig}")

            if action_root == 'TRANSFER_POSTING':
                matched_rule_for_material = best_option.get('rule_id', 'D70')
            plan_rows.append(row_entry)

            # Downstream unblocked errors (Action: SKIP to prevent duplicate transfers)
            for idx in cleared_actionable_indices:
                if idx == root_idx:
                    continue
                down_e = actionable[idx]
                plan_rows.append({
                    'Material': mat, 'Mvt_Original': down_e['mvt'], 'Deficit_Loc': down_e['deficit_loc'],
                    'From_SLoc': '', 'To_SLoc': down_e['deficit_loc'], 'Transfer_Qty': down_e['qty'],
                    'MB1B_Mvt': '311', 'Doc_Date': down_e['doc_date'], 'Post_Date': down_e['post_date'],
                    'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                    'Action': 'SKIP', 'Status': 'CHAIN_CASCADE_RESOLVE',
                    'Persistence_Checked': True,
                    'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                    'Persistent_Qty': 0.0,
                    'Reason': f"Upstream transfer ({s_loc} ➔ {d_loc}) automatically feeds {down_e['src']} for chained cascade resolution (duplicate transfer prevented)"
                })

            if d109_meta is not None:
                for _pr_c in plan_rows:
                    if _pr_c.get('Status') == 'CHAIN_CASCADE_RESOLVE' and 'D109_Consec_Runs' not in _pr_c:
                        _pr_c['D109_Consec_Runs'] = int(d109_meta['consec_runs'])
                        _pr_c['D109_Active_GR_Count'] = int(d109_meta['gr_count'])
                        _pr_c['SL00_WMS_SAP_Gap'] = float(d109_meta['current_sl00_gap'])
                        _pr_c['D109_Already_Released'] = bool(d109_meta.get('already_released', False))
                        _pr_c['D109_Suspected_Unposted_GR'] = bool(d109_meta.get('suspected_unposted_gr_f', False))

            # Update simulation state
            base_state = dict(sim_final_state)
            current_sap = dict(sim_final_state)
            for s in all_slocs:
                current_surplus[s] = current_sap.get(s, 0.0) - current_wms.get(s, 0.0)
            unresolved_indices = [i for i in unresolved_indices if i not in cleared_actionable_indices]
        else:
            break

    # 5.2 Warehouse Stock Accuracy Maximization Flush (자재/불량창고 정합도 100% 달성 규칙)
    flush_processed_indices = []
    for idx in unresolved_indices:
        e = actionable[idx]
        if e['mvt'] != '311':
            continue

        from_st = e['from_st']
        to_st = e['to_st']
        req_qty = e['qty']
        avail_sap = current_sap.get(from_st, 0.0)
        wms_stock = current_wms.get(from_st, 0.0)
        surplus = avail_sap - wms_stock

        is_wh_from = from_st in WAREHOUSE_SLOCS

        if is_wh_from and avail_sap > 0.0:
            if wms_stock == 0.0:
                # D91 Persistence Guard: WMS=0 must be confirmed in previous snapshot (15~60 min ago)
                has_prev_snapshot = bool(prev_snapshot_time and prev_wms_df is not None and not prev_wms_df.empty)
                prev_wms_stock = prev_wms_by_sloc.get(from_st, 0.0)

                if not has_prev_snapshot:
                    plan_rows.append({
                        'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': e['deficit_loc'],
                        'From_SLoc': '', 'To_SLoc': e['deficit_loc'], 'Transfer_Qty': req_qty,
                        'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
                        'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                        'Action': 'SKIP', 'Status': 'WMS_ZERO_NOT_PERSISTENT',
                        'Posting_Type': 'WMS_ZERO_FLUSH_BLOCKED',
                        'Persistence_Checked': False,
                        'Prev_Snapshot_Time': 'NONE',
                        'Persistent_Qty': 0.0,
                        'Reason': f"SLoc ({from_st}) current WMS stock is 0, but prior snapshot is unavailable to verify persistence - 1st-cycle flush held (D91)"
                    })
                    continue
                elif prev_wms_stock > 0.0:
                    plan_rows.append({
                        'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': e['deficit_loc'],
                        'From_SLoc': '', 'To_SLoc': e['deficit_loc'], 'Transfer_Qty': req_qty,
                        'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
                        'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                        'Action': 'SKIP', 'Status': 'WMS_ZERO_NOT_PERSISTENT',
                        'Posting_Type': 'WMS_ZERO_FLUSH_BLOCKED',
                        'Persistence_Checked': True,
                        'Prev_Snapshot_Time': prev_snapshot_time,
                        'Persistent_Qty': prev_wms_stock,
                        'Reason': f"SLoc ({from_st}) current WMS stock is 0, but prior ({prev_snapshot_time}) WMS stock ({prev_wms_stock:,.1f} EA > 0) indicates potential interface lag - flush excluded (D91)"
                    })
                    continue

                flush_qty = min(avail_sap, req_qty)
                target_dest = to_st
                if to_st in WAREHOUSE_SLOCS and current_wms.get(to_st, 0.0) == 0.0:
                    target_dest = DEFAULT_LINE_SLOC  # 'SP31'
                    reason_transfer = (
                        f"Persistent WMS=0 verified ({from_st}=0, {to_st}=0, prior {prev_snapshot_time}): flushed SAP book residual "
                        f"({flush_qty:,.1f} EA) to production SLoc ({target_dest}) to achieve 100% warehouse stock accuracy "
                        f"(production SLocs excluded from warehouse accuracy denominator)"
                    )
                    reason_skip = (
                        f"Remaining shortage ({req_qty - flush_qty:,.1f} EA) prevents full GI error clear (SKIP), "
                        f"but flushed {from_st} residual to production SLoc ({target_dest}) for 100% warehouse accuracy"
                    )
                else:
                    reason_transfer = (
                        f"Persistent WMS=0 in {from_st} verified (prior {prev_snapshot_time}): transferred SAP book residual "
                        f"({flush_qty:,.1f} EA) to {target_dest} to achieve 100% stock accuracy in {from_st}"
                    )
                    reason_skip = (
                        f"Remaining shortage ({req_qty - flush_qty:,.1f} EA) prevents full GI error clear (SKIP), "
                        f"but flushed {flush_qty:,.1f} EA from {from_st} to {target_dest} for 100% warehouse accuracy"
                    )
            elif surplus > 0.0:
                flush_qty = min(surplus, req_qty)
                target_dest = to_st
                reason_transfer = (
                    f"Transferred {flush_qty:,.1f} EA of {from_st} SAP surplus ({surplus:,.1f} EA) "
                    f"to {target_dest} to achieve 100% stock accuracy in {from_st}"
                )
                shortage = req_qty - flush_qty
                reason_skip = (
                    f"Remaining shortage ({shortage:,.1f} EA) prevents full GI error clear (SKIP), "
                    f"but flushed {flush_qty:,.1f} EA surplus from {from_st} to {target_dest} for 100% warehouse accuracy"
                )
            else:
                continue

            # D110 (#197) Rule 2: Never execute flush on a cooled-down (material, from_sloc, target_dest) route
            if (mat, from_st, target_dest) in eff_cooldown_map:
                d110_cooldown_blocked_by_dloc.setdefault(e['deficit_loc'], eff_cooldown_map[(mat, from_st, target_dest)])
                continue

            # D71/D73/B-1: Never flush out of or into a GR-error protected storage location
            if active_gr_errors:
                is_ztcode_stock_rec_fl_blocked, _, _, _, _ = check_movement_ztcode_stock_rec_block(
                    mat=mat,
                    src_sloc=from_st,
                    dst_sloc=target_dest,
                    active_gr_errors=active_gr_errors or {},
                    active_gi_errors={},
                )
                if is_ztcode_stock_rec_fl_blocked:
                    continue

            # Simulate settled state accuracy to ensure objective function approval
            test_flush_state = dict(current_sap)
            test_flush_state[from_st] -= flush_qty
            test_flush_state[target_dest] = test_flush_state.get(target_dest, 0.0) + flush_qty

            is_app_flush, eval_flush = evaluate_settled_state_accuracy(
                wms_stock_by_sloc=current_wms,
                sap_initial_by_sloc=current_sap,
                sap_final_by_sloc=test_flush_state,
                sap_intermediate_by_sloc=test_flush_state,
                target_slocs=WAREHOUSE_SLOCS
            )

            if not is_app_flush:
                continue

            # Interim Guardrail 1 (028-1 / D37): Skip transfers to SL00 pending interface
            action_flush = 'TRANSFER_POSTING'
            status_flush = 'READY_FOR_EXECUTION'
            if target_dest == 'SL00':
                action_flush = 'SKIP'
                status_flush = 'SKIPPED_PENDING_INTERFACE_SL00'
                reason_transfer = f"Arrival transfer to receiving SLoc SL00 ({from_st}->SL00) - held to prevent duplicate reflection of pending interface (D37/028 guard)"

            # 1. MB1B Transfer Posting to clear warehouse phantom stock
            plan_rows.append({
                'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': e['deficit_loc'],
                'From_SLoc': from_st, 'To_SLoc': target_dest, 'Transfer_Qty': flush_qty,
                'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': action_flush, 'Status': status_flush,
                'Posting_Type': 'WMS_ZERO_FLUSH',
                'Accuracy_Before': eval_flush['mean_init'], 'Accuracy_After': eval_flush['mean_final'],
                'Delta_Accuracy': eval_flush['score_delta'],
                'Persistence_Checked': True,
                'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                'Persistent_Qty': 0.0,
                'Reason': reason_transfer
            })

            # 2. GI Error row marked as SKIP with shortage deficit recorded
            plan_rows.append({
                'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': e['deficit_loc'],
                'From_SLoc': '', 'To_SLoc': e['deficit_loc'], 'Transfer_Qty': req_qty,
                'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
                'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                'Action': 'SKIP', 'Status': 'SHORTAGE_DEFICIT_PRESERVED',
                'Posting_Type': 'WMS_ZERO_FLUSH_REMAINDER',
                'Persistence_Checked': True,
                'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                'Persistent_Qty': 0.0,
                'Reason': reason_skip
            })

            # Update simulated SAP state
            current_sap = dict(test_flush_state)
            base_state = dict(test_flush_state)
            for s in all_slocs:
                current_surplus[s] = current_sap.get(s, 0.0) - current_wms.get(s, 0.0)
            flush_processed_indices.append(idx)

    unresolved_indices = [i for i in unresolved_indices if i not in flush_processed_indices]

    # 5.3 Check for partial rebalancing from other surplus locations for remaining unresolved errors
    if unresolved_indices:
        first_unres = actionable[unresolved_indices[0]]
        d_loc = first_unres['deficit_loc']
        req_qty = first_unres['qty']

        partial_cands = [s for s in all_slocs if s != d_loc and current_sap.get(s, 0.0) > 0]
        if partial_cands:
            best_p_sloc = None
            best_p_qty = 0.0
            best_p_score_delta = 0.001  # D77: Requires strictly positive accuracy improvement (>= +0.1%p)
            best_p_eval = None

            for p_s in partial_cands:
                # D115 (#222): Never let Section 5.3 partial rebalancing pump from SP* into d_loc
                # when an unresolved d_loc -> SP* GI error exists (governed strictly by D115 A_WMS rules).
                if _is_d115_sp_dest(p_s) and any(
                    actionable[u_idx]['deficit_loc'] == d_loc and _is_d115_sp_dest(actionable[u_idx]['to_st'])
                    for u_idx in unresolved_indices
                ):
                    continue

                # D110 (#197) Rule 2: 24-hour cooldown after >=3 consecutive failures
                if (mat, p_s, d_loc) in eff_cooldown_map:
                    d110_cooldown_blocked_by_dloc.setdefault(d_loc, eff_cooldown_map[(mat, p_s, d_loc)])
                    continue

                # D71/D73: Check if partial transfer p_s -> d_loc is blocked by intersecting GR/GI errors
                # D92: 01_JOB candidate movement is by definition heading to the deficit location of its own GI error,
                # so it must NOT be self-blocked by active_gi_errors. Only active_gr_errors (D71/D73) are checked.
                if active_gr_errors:
                    is_ztcode_stock_rec_blocked, _, _, _, _ = check_movement_ztcode_stock_rec_block(
                        mat=mat,
                        src_sloc=p_s,
                        dst_sloc=d_loc,
                        active_gr_errors=active_gr_errors or {},
                        active_gi_errors={}
                    )
                    if is_ztcode_stock_rec_blocked:
                        continue

                p_avail = current_sap.get(p_s, 0.0)

                # Persistence Guard Check on partial rebalancing candidate
                p_persist = check_transfer_persistence(
                    material=mat,
                    source_sloc=p_s,
                    dest_sloc=d_loc,
                    candidate_qty=p_avail,
                    current_wms_by_sloc=current_wms,
                    current_sap_by_sloc=current_sap,
                    prev_wms_by_sloc=prev_wms_by_sloc if prev_snapshot_time else None,
                    prev_sap_by_sloc=prev_sap_by_sloc if prev_snapshot_time else None,
                    prev_snapshot_time=prev_snapshot_time,
                    is_prev_snapshot_valid=(prev_snapshot_time is not None)
                )

                if p_s in WAREHOUSE_SLOCS:
                    if p_persist.source_persistent_surplus <= 0:
                        continue
                    max_usable_p = min(p_avail, p_persist.persistent_qty)
                else:
                    max_usable_p = p_avail

                if d_loc in WAREHOUSE_SLOCS and p_persist.dest_persistent_deficit <= 0:
                    continue

                p_qty = min(max_usable_p, req_qty)
                if p_qty <= 0: continue

                test_p_state = dict(base_state)
                test_p_state[p_s] -= p_qty
                test_p_state[d_loc] = test_p_state.get(d_loc, 0.0) + p_qty

                is_app_p, eval_p = evaluate_settled_state_accuracy(
                    wms_stock_by_sloc=current_wms,
                    sap_initial_by_sloc=current_sap,
                    sap_final_by_sloc=test_p_state,
                    sap_intermediate_by_sloc=test_p_state,
                    target_slocs=WAREHOUSE_SLOCS
                )

                if is_app_p:
                    p_delta = eval_p['score_delta']
                    # D77: Partial rebalancing strictly requires actual positive accuracy improvement (p_delta >= +0.1%p)
                    if p_delta >= 0.001 and (p_delta > best_p_score_delta or (p_delta == best_p_score_delta and p_qty > best_p_qty)):
                        best_p_score_delta = p_delta
                        best_p_sloc = p_s
                        best_p_qty = p_qty
                        best_p_eval = eval_p

            if best_p_sloc and best_p_eval and best_p_eval['is_approved'] and best_p_qty > 0 and best_p_score_delta >= 0.001:
                rem_qty = req_qty - best_p_qty
                if d_loc == 'SL00':
                    # D37 / 028-1: Never allow partial rebalancing transfers into SL00 while pending GR interface (SL00 WMS > SAP)!
                    plan_rows.append({
                        'Material': mat, 'Mvt_Original': first_unres['mvt'], 'Deficit_Loc': d_loc,
                        'From_SLoc': best_p_sloc, 'To_SLoc': d_loc, 'Transfer_Qty': best_p_qty,
                        'MB1B_Mvt': '311', 'Doc_Date': first_unres['doc_date'], 'Post_Date': first_unres['post_date'],
                        'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                        'Action': 'SKIP', 'Status': 'SKIPPED_PENDING_INTERFACE_SL00',
                        'Accuracy_Before': best_p_eval['mean_init'], 'Accuracy_After': best_p_eval['mean_final'],
                        'Delta_Accuracy': best_p_eval['score_delta'],
                        'Persistence_Checked': True,
                        'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                        'Persistent_Qty': best_p_qty,
                        'Reason': f"SL00 receiving partial transfer ({best_p_sloc}->SL00) held to prevent duplicate posting while pending GR interface (D37/028 guard)"
                    })
                else:
                    plan_rows.append({
                        'Material': mat, 'Mvt_Original': first_unres['mvt'], 'Deficit_Loc': d_loc,
                        'From_SLoc': best_p_sloc, 'To_SLoc': d_loc, 'Transfer_Qty': best_p_qty,
                        'MB1B_Mvt': '311', 'Doc_Date': first_unres['doc_date'], 'Post_Date': first_unres['post_date'],
                        'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
                        'Action': 'TRANSFER_POSTING', 'Status': 'PARTIAL_ACCURACY_REBALANCING',
                        'Accuracy_Before': best_p_eval['mean_init'], 'Accuracy_After': best_p_eval['mean_final'],
                        'Delta_Accuracy': best_p_eval['score_delta'],
                        'Persistence_Checked': True,
                        'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
                        'Persistent_Qty': best_p_qty,
                        'Reason': f"Full GI clear unavailable (needed {req_qty} EA, residual {rem_qty:,.1f} EA requires manual cleanup) - partial transfer of {best_p_qty} EA from {best_p_sloc} for +{best_p_eval['score_delta']:.2f}%p accuracy gain (D77)"
                    })
                unresolved_indices.pop(0)

    # 6. Mark any remaining unresolved errors
    for idx in unresolved_indices:
        e = actionable[idx]
        from_st = e['from_st']
        def_loc_e = e['deficit_loc']
        avail_s = current_sap.get(from_st, 0.0)
        wms_s = current_wms.get(from_st, 0.0)
        max_cand_stock = max([current_sap.get(s, 0.0) for s in all_slocs if s not in deficit_locations], default=0.0)

        cd_info = d110_cooldown_blocked_by_dloc.get(def_loc_e)
        d115_gr_info = d115_gr_blocked_by_dloc.get(def_loc_e)
        if cd_info is not None:
            skip_status = 'SKIPPED_COOLDOWN_GUARD_D110'
            skip_reason = (
                f"D110 consecutive failure cooldown (24h hold): {cd_info['from_sloc']}->{cd_info['to_sloc']} "
                f"failed {cd_info['consec_failures']} consecutive times (latest {cd_info['last_failed_at']}, "
                f"reason: {cd_info['last_error'] or 'SAP error'}) ➔ auto-retry blocked until {cd_info['cooldown_until']} (SKIP)"
            )
            extra_d110 = {
                'D110_Cooldown_Active': True,
                'D110_Cooldown_Route': f"{cd_info['from_sloc']}->{cd_info['to_sloc']}",
                'D110_Cooldown_Consec_Fails': int(cd_info['consec_failures']),
                'D110_Cooldown_Until': str(cd_info['cooldown_until']),
                'D110_Cooldown_Last_Error': str(cd_info['last_error'] or ''),
            }
        elif d115_gr_info is not None:
            skip_status = 'PROTECTED_GR_DEPENDENT'
            skip_reason = (
                f"D115 Rule 4 hold (active/unverified 3-file GR): {d115_gr_info['dest_b']}->{def_loc_e} "
                f"deficit-pump ({d115_gr_info['pump_qty']:,.1f} EA) held until active GR clears (SKIP)"
            )
            extra_d110 = {}
        else:
            skip_status = 'INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD'
            skip_reason = (
                f"No eligible plant-wide surplus stock or settled-state accuracy guard not satisfied "
                f"(max candidate SLoc stock: {max_cand_stock:,.1f} EA) - transfer excluded & error preserved (SKIP)"
            )
            extra_d110 = {}

        skip_row = {
            'Material': mat, 'Mvt_Original': e['mvt'], 'Deficit_Loc': def_loc_e,
            'From_SLoc': '', 'To_SLoc': def_loc_e, 'Transfer_Qty': e['qty'],
            'MB1B_Mvt': '311', 'Doc_Date': e['doc_date'], 'Post_Date': e['post_date'],
            'Recipient': _get_ops_env('SAP_MB1B_RECIPIENT', 'AUTO_AGENT'), 'Header_Text': 'GI_Error_Clear',
            'Action': 'SKIP', 'Status': skip_status,
            'Persistence_Checked': True,
            'Prev_Snapshot_Time': prev_snapshot_time or 'NONE',
            'Persistent_Qty': 0.0,
            'Reason': skip_reason,
        }
        skip_row.update(extra_d110)
        plan_rows.append(skip_row)

    # D90-2: Attach SAP/WMS total snapshots to plan rows and tag CHAIN_GAP when detected
    chain_gap_tagged = False
    for pr in plan_rows:
        if 'SAP_Total' not in pr:
            pr['SAP_By_SLoc'] = initial_sap_snapshot
            pr['WMS_By_SLoc'] = initial_wms_snapshot
            pr['SAP_Total'] = initial_sap_total
            pr['WMS_Total'] = initial_wms_total
            pr['Total_Gap'] = initial_wms_total - initial_sap_total
        if is_chain_gap and not chain_gap_tagged and pr.get('Status') != 'PROTECTED_GR_DEPENDENT':
            first_act = actionable[0]
            pr['Is_Chain_Gap'] = True
            pr.setdefault('Error_From', first_act.get('src', ''))
            pr.setdefault('Error_To', first_act.get('dst', ''))
            pr.setdefault('Error_Qty', float(first_act.get('qty', 0.0)))
            chain_gap_tagged = True

    if signal_db_path is not None and is_chain_gap:
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("_job01_db_mgr", BASE_DIR / "db_manager.py")
            _dbm = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_dbm)
            cg_rows = [r for r in plan_rows if r.get('Is_Chain_Gap') and r.get('Status') not in ('ROUNDTRIP_CLEAR', 'ROUNDTRIP_CLEAR_VIA_DEST_D113', 'DEST_PUMP_CLEAR_D115')]
            if cg_rows:
                _dbm.record_plan_stock_gap_signals(
                    run_id=run_id or datetime.now().strftime("PLAN_%Y%m%d_%H%M%S"),
                    plan_rows=cg_rows,
                    db_path=Path(signal_db_path)
                )
        except Exception as e_sig_cg:
            logger.warning(f"Could not record CHAIN_GAP signal in resolve_material_errors_unified: {e_sig_cg}")

    return plan_rows

def resolve_gi_errors(
    gi_path: Optional[Path] = None,
    gr_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    save_excel: bool = True,
    run_id: Optional[str] = None,
    record_signals: bool = False,
    signal_db_path: Optional[Path] = None,
    dry_run: bool = False,
    gi_db_path: Optional[Path] = None,
    plan_history_dir: Optional[Path] = None,
    d109_prior_runs: Optional[list] = None,
    d110_cooldown_map: Optional[dict] = None,
) -> pd.DataFrame:
    """
    Main resolution engine:
    1. Reads fresh GR & GI errors directly from passed paths (strictly no stale glob/mtime fallback)
    2. Applies GR dependency filter
    3. Excludes 901/902 and marks empty message rows as SKIPPED_NO_MESSAGE
    4. Computes surplus SAP locations (SAP_Stock - WMS_Stock > 0)
    5. Groups by Material Number and applies Directed Flow Graph & Cascade Simulation
    6. Builds optimized MB1B transfer proposals and marks chained downstream errors
    """
    logger.info("=== Starting GR/GI Interface Error Resolution Engine ===")
    
    # 1. Load GI Errors (Freshness & Stale-Globbing Guard)
    if not gi_path or not Path(gi_path).exists():
        logger.error(f"❌ [GI GUARD] Fresh GI error file missing or invalid: {gi_path}. Stale file globbing is strictly prohibited.")
        return pd.DataFrame()
    gi_df = parse_sap_utf16_list(Path(gi_path))
    logger.info(f"Loaded GI Errors: {len(gi_df)} records from {Path(gi_path).name}")

    # 2. Load GR & GI Errors (Freshness & Stale-Globbing Guard)
    active_gr_errors = {}
    active_gi_errors = {}
    protected_gr_materials = set()
    from ztcode_stock_guard import expand_gr_batch_paths
    gr_expanded_paths = expand_gr_batch_paths(gr_path)
    gr_valid = False
    if gr_expanded_paths:
        active_gr_errors = extract_active_gr_error_map(gr_expanded_paths)
        protected_gr_materials = set(active_gr_errors.keys())
        logger.info(f"Loaded GR Errors: {len(active_gr_errors)} materials from {[p.name for p in gr_expanded_paths]}")
        gr_valid = True
    else:
        logger.warning(f"⚠️ [GR GUARD] Fresh GR error file missing or invalid: {gr_path}. Stale file globbing is strictly prohibited. Transfers will be blocked (SKIP) to prevent GR interface collision.")

    if gi_path and Path(gi_path).exists():
        active_gi_errors = extract_active_gi_error_map(Path(gi_path))

    # 2.1 Auto-heal missing storage locations via MMSC (SL00, SL01, SL02, SL49, SP31)
    try:
        from mmsc_operator import auto_heal_gi_error_slocs
        healed_records = auto_heal_gi_error_slocs(gi_df, plant=_get_ops_env("SAP_PLANT", "P001"))
        if healed_records:
            logger.info(f"✅ [MMSC AUTO-HEAL] Evaluated/Healed {len(healed_records)} materials with missing storage locations.")
    except Exception as e_mmsc:
        logger.warning(f"⚠️ [MMSC AUTO-HEAL WARNING] Could not run MMSC healing: {e_mmsc}")
    
    # 3. Load MB52 & WMS for Stock Accuracy Optimization
    mb52_stock = load_mb52_stock()
    wms_stock = load_wms_stock()

    # Load Previous Snapshot Pair for Persistence Guard (028-2 / D37)
    mb52_files = sorted(glob.glob(str(MB52_DIR / "*.XLS*")), key=os.path.getmtime, reverse=True)
    wms_files = sorted(glob.glob(str(WMS_DIR / "*.xlsx")), key=os.path.getmtime, reverse=True)
    prev_wms_df = None
    prev_mb52_df = None
    prev_snapshot_time = None
    if mb52_files and wms_files:
        prev_wms_df, prev_mb52_df, prev_snapshot_time = load_previous_snapshot_pair(
            current_wms_file=Path(wms_files[0]),
            current_mb52_file=Path(mb52_files[0]),
            wms_dir=WMS_DIR,
            mb52_dir=MB52_DIR
        )
    
    # Merge stock comparison
    stock_comparison = None
    if not mb52_stock.empty and not wms_stock.empty:
        stock_comparison = pd.merge(mb52_stock, wms_stock, on=['Material', 'SLoc'], how='outer').fillna(0)
        stock_comparison['Surplus'] = stock_comparison['SAP_Stock'] - stock_comparison['WMS_Stock']
        logger.info(f"Stock comparison matrix ready ({len(stock_comparison)} rows).")

    # 4. Group by Material Number to analyze linked / chained GI errors (N >= 1)
    eff_d109_prior_runs = d109_prior_runs
    if eff_d109_prior_runs is None:
        try:
            eff_d109_prior_runs = load_d109_prior_completed_runs(
                gi_db_path=gi_db_path,
                plan_dir=plan_history_dir or OUTPUT_DIR,
                exclude_run_id=run_id,
            )
        except Exception as e_d109_hist:
            logger.warning(f"⚠️ [D109] Could not load prior completed runs for SL00 guard check: {e_d109_hist}")
            eff_d109_prior_runs = []

    eff_d110_cooldown_map = d110_cooldown_map
    if eff_d110_cooldown_map is None:
        try:
            eff_d110_cooldown_map = load_d110_failure_cooldown_map(
                gi_db_path=gi_db_path,
                exclude_batch_id=run_id,
            )
            if eff_d110_cooldown_map:
                logger.info(f"🛡️ [D110 COOLDOWN] Active 24h failure cooldown routes: {len(eff_d110_cooldown_map)}")
        except Exception as e_d110_cd:
            logger.warning(f"⚠️ [D110] Could not load failure cooldown map: {e_d110_cd}")
            eff_d110_cooldown_map = {}
    resolution_plan = []
    today = datetime.now()
    today_str = today.strftime("%Y.%m.%d")
    prev_month_str = (today.replace(day=1) - pd.Timedelta(days=1)).strftime("%Y.%m.%d")
    
    if 'Material Number' in gi_df.columns:
        for mat, group_df in gi_df.groupby('Material Number', sort=False):
            mat_str = str(mat).strip()
            mat_plan = resolve_material_errors_unified(
                mat=mat_str,
                rows_df=group_df,
                stock_comparison=stock_comparison,
                protected_gr_materials=protected_gr_materials,
                today_str=today_str,
                prev_month_str=prev_month_str,
                prev_wms_df=prev_wms_df,
                prev_mb52_df=prev_mb52_df,
                prev_snapshot_time=prev_snapshot_time,
                active_gr_errors=active_gr_errors,
                active_gi_errors=active_gi_errors,
                d109_prior_runs=eff_d109_prior_runs,
                gr_valid=gr_valid,
                d110_cooldown_map=eff_d110_cooldown_map,
                require_prev_snapshot=True,
            )
            resolution_plan.extend(mat_plan)
    else:
        logger.warning("No 'Material Number' column in GI error data.")

    # 5. GR-Dependency Fail-Safe Guard: If fresh GR error verification failed, block all transfers
    if not gr_valid:
        for r in resolution_plan:
            if r.get('Action') == 'TRANSFER_POSTING':
                r['Action'] = 'SKIP'
                r['Status'] = 'UNVERIFIED_GR_EXTRACTION'
                r['Reason'] = 'Fresh GR error extraction failed (GR dependency unverified) - transfer locked (SKIP)'

    if dry_run:
        record_signals = False
        signal_db_path = None

    if record_signals or signal_db_path is not None:
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location("_job01_db_mgr", BASE_DIR / "db_manager.py")
            _dbm = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_dbm)
            eff_run_id = run_id or today.strftime("BATCH_%Y%m%d_%H%M%S")
            sig_cnt = _dbm.record_plan_stock_gap_signals(
                run_id=eff_run_id,
                plan_rows=resolution_plan,
                db_path=Path(signal_db_path) if signal_db_path is not None else None
            )
            if sig_cnt > 0:
                logger.info(f"Recorded {sig_cnt} GI_STOCK_GAP_SIGNALS row(s) for run_id={eff_run_id}")
        except Exception as e_sig:
            logger.warning(f"Could not record GI_STOCK_GAP_SIGNALS in resolve_gi_errors: {e_sig}")

    plan_df = pd.DataFrame(resolution_plan)
    
    # Save plan to Excel & CSV if requested
    if save_excel:
        if dry_run:
            if output_dir is not None and Path(output_dir).resolve() != OUTPUT_DIR.resolve():
                target_dir = Path(output_dir)
            else:
                target_dir = BASE_DIR / "scratch"
            target_dir.mkdir(parents=True, exist_ok=True)
            out_file = target_dir / f"DRYRUN_GI_Resolution_Plan_{today.strftime('%Y%m%d_%H%M%S')}.xlsx"
        else:
            target_dir = Path(output_dir or OUTPUT_DIR)
            target_dir.mkdir(parents=True, exist_ok=True)
            out_file = target_dir / f"GI_Resolution_Plan_{today.strftime('%Y%m%d_%H%M%S')}.xlsx"
        plan_df.to_excel(out_file, index=False)
        plan_df.attrs["output_file"] = str(out_file)
        logger.info(f"Resolution Plan generated: {out_file}")
    
    return plan_df

if __name__ == "__main__":
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    latest_gi = sorted(glob.glob(str(GI_DIR / "*.XLS*")), key=os.path.getmtime, reverse=True)
    latest_gr = sorted(glob.glob(str(GR_DIR / "*.XLS*")), key=os.path.getmtime, reverse=True)
    gi_p = Path(latest_gi[0]) if latest_gi else None
    gr_p = Path(latest_gr[0]) if latest_gr else None
    plan = resolve_gi_errors(gi_path=gi_p, gr_path=gr_p)
    if plan is not None and not plan.empty:
        print("\n=== RESOLUTION SUMMARY ===")
        print(plan['Status'].value_counts())
        print("\n=== READY TRANSFERS (SAMPLE) ===")
        transfers = plan[plan['Action'] == 'TRANSFER_POSTING']
        if not transfers.empty:
            print(transfers[['Material', 'Mvt_Original', 'From_SLoc', 'To_SLoc', 'Transfer_Qty', 'Doc_Date']].head(10))
        else:
            print("No transfers ready for execution.")
