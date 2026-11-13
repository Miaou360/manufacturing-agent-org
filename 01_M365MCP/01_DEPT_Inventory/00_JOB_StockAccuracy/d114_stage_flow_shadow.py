"""
D114 Stage-Flow Dwell Profile & Overtake Shadow Engine (#212 / #216 B-4 / #217 / #237)
======================================================================================
Tracks barcode-level movement across the 5 warehouse flow stages (`S1` ~ `S5`, plus
`S1_TO_P1_TRANSIT` and `PRE_PUTAWAY`) from 01_JOB's 30m/1h `wms_parts_stack` snapshots
into a 7-day-retention-proof incremental WAL SQLite ledger (`db/d114_stage_shadow.db`).

STRICT SHADOW GUARANTEE:
  - Weight on 00_JOB `bin_risk_score` and target bin selection = `0.0` (zero score/list change).
  - Uses `00_HQ_TaskManager/wms_mapping.py` as single SLoc mapping source of truth.
"""

from __future__ import annotations

import datetime
import gc
import logging
import math
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from dotenv import load_dotenv as _load_dotenv

for _ep in list(Path(__file__).resolve().parents) + [
    Path.home() / "Desktop" / "CODES" / "01_M365MCP",
    Path.home() / "Desktop" / "CODES",
]:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env", override=False)
        break

CURRENT_DIR = Path(__file__).resolve().parent
DEPT_DIR = CURRENT_DIR.parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
for _p in (str(HQ_DIR), str(DEPT_DIR), str(CURRENT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from wms_mapping import map_wms_gi_loc_to_sap

logger = logging.getLogger("D114StageFlowShadow")

DEFAULT_SHADOW_DB_PATH = CURRENT_DIR / "db" / "d114_stage_shadow.db"
DEFAULT_INVENTORY_HISTORY_DB_PATH = CURRENT_DIR / "db" / "inventory_history.db"
DEFAULT_WMS_PARTS_STACK_DIR = DEPT_DIR / "01_JOB_GR_GI_Error" / "wms_parts_stack"

FLOW_STAGES: Tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5")
TRACKED_STATES: Tuple[str, ...] = ("PRE_PUTAWAY", "S1", "S1_TO_P1_TRANSIT", "S2", "S3", "S4", "S5")

STAGE_ORDER: Dict[str, float] = {
    "PRE_PUTAWAY": 0.0,
    "S1": 1.0,
    "S1_TO_P1_TRANSIT": 1.5,
    "S2": 2.0,
    "S3": 3.0,
    "S4": 4.0,
    "S5": 5.0,
    "CONSUMED_OR_GI": 6.0,
}

STAGE_TARGET_LIST: Dict[str, str] = {
    "S1": "Weekly",
    "S2": "Weekly",
    "S3": "Daily",
    "S4": "Daily",
    "S5": "Daily",
}

STAGE_LABELS: Dict[str, str] = {
    "S1": "Plant 3 Storage (R*)",
    "S1_TO_P1_TRANSIT": "Plant 3->1 Transit (SL01 + R*)",
    "S2": "Plant 1 Tier 3-5 Storage",
    "S3": "Plant 1 Tier 1-2 Picking",
    "S4": "Line Buffer (SMT / Floor)",
    "S5": "Repack Supply Carts (CRT / PRO)",
}

_R_RACK_RE = re.compile(r"^R\d+", re.IGNORECASE)
_S_RACK_RE = re.compile(r"^S\d+", re.IGNORECASE)
_P1_HIGH_RACK_RE = re.compile(r"^\d+[A-Z]+\s+[3-9]\d*$", re.IGNORECASE)
_P1_PICK_RACK_RE = re.compile(r"^\d+[A-Z]+\s+[1-2]$", re.IGNORECASE)
_FNAME_TS_RE = re.compile(r"(\d{8})_(\d{6})")


def _get_floor_buffer_prefixes() -> Tuple[str, ...]:
    base = ["SMT", "SMD", "FLR", "OEM"]
    for env_key in ("FLOOR_BUFFER_BIN_PREFIXES", "OEM_FLOOR_BIN_PREFIX"):
        raw = os.getenv(env_key, "").strip()
        if raw:
            for tok in re.split(r"[,;]", raw):
                t_up = tok.strip().upper()
                if t_up and t_up not in base:
                    base.append(t_up)
    return tuple(base)


def classify_d114_stage(
    gi_loc: Any,
    bin_val: Any,
    floor_prefixes: Optional[Tuple[str, ...]] = None,
) -> Tuple[str, str]:
    """
    Returns `(stage_code, sap_sloc)` per #212 / #216 B-4 / #217:
      - A1 Option A: `(SL01, ^R\\d+)` -> `S1_TO_P1_TRANSIT`
      - A2 Option A: physical Plant 1 rack in `SL02` -> `S2` (Tier 3-5) / `S3` (Tier 1-2)
      - A3 Option A: physical `SMT/Floor` or `CRT/PRO` in `SL02` -> `S4` / `S5`
      - A4 (#217): `^S\\d+` (`S51Z 5`, etc.) -> `EXCLUDED`
      - A5 Option A: `SL03` / `RESIN` -> `EXCLUDED`
      - A6 Option A: `SL49` -> `EXCLUDED`; `SL00` + physical bin -> `S1..S5` by physical BIN;
        `SL00` + blank bin -> `PRE_PUTAWAY`
      - `SCR*`, `NG`, `EXT_TRL*`, `CKD*`, `D*`, `TEST*` -> `EXCLUDED`
    """
    sloc = map_wms_gi_loc_to_sap(str(gi_loc or "").strip()) or ""
    b = str(bin_val or "").strip().upper()
    if b == "NAN":
        b = ""

    # SLoc-level exclusions (A5: SL03 Raw Material; A6: SL49 Quality Hold; SL04 Pass-Through)
    if sloc in ("SL03", "SL04", "SL49"):
        return "EXCLUDED", sloc

    # Empty bin handling
    if not b or b == "BINSYNTH9953":
        if sloc == "SL00":
            return "PRE_PUTAWAY", sloc
        return "EXCLUDED", sloc

    # Physical bin exclusions (A4 ^S\d+, A5 RESIN, SCR*, NG, virtual/external)
    if (
        b in ("NG", "RESIN")
        or b.startswith(("EXT_TRL", "CKD", "SCR", "D", "TEST"))
        or _S_RACK_RE.match(b)
    ):
        return "EXCLUDED", sloc

    pfxs = floor_prefixes if floor_prefixes is not None else _get_floor_buffer_prefixes()

    # A1 Option A: (SL01, R*) -> S1_TO_P1_TRANSIT
    if _R_RACK_RE.match(b):
        if sloc == "SL01":
            return "S1_TO_P1_TRANSIT", sloc
        if sloc in ("SL02", "SL00", ""):
            return "S1", sloc
        return "EXCLUDED", sloc

    # Only SL00 / SL01 / SL02 (or blank in synthetic unit tests) enter S2..S5
    if sloc not in ("SL00", "SL01", "SL02", ""):
        return "EXCLUDED", sloc

    if b.startswith(("CRT", "PRO")):
        return "S5", sloc
    if b.startswith(pfxs):
        return "S4", sloc
    if _P1_HIGH_RACK_RE.match(b):
        return "S2", sloc
    if _P1_PICK_RACK_RE.match(b):
        return "S3", sloc

    return "EXCLUDED", sloc


def parse_snapshot_timestamp(file_path: Path) -> datetime.datetime:
    """Extract `YYYY-MM-DD HH:MM:SS` from `WMS_StockList_*_YYYYMMDD_HHMMSS.xlsx` or file mtime."""
    m = _FNAME_TS_RE.search(file_path.stem)
    if m:
        return datetime.datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
    return datetime.datetime.fromtimestamp(file_path.stat().st_mtime).replace(microsecond=0)


def init_shadow_db(db_path: Path = DEFAULT_SHADOW_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS STAGE_WATERMARK (
            source_name TEXT PRIMARY KEY,
            first_snapshot_ts TEXT,
            last_snapshot_ts TEXT,
            last_filename TEXT,
            snapshots_processed INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS BARCODE_STAGE_STATE (
            barcode TEXT PRIMARY KEY,
            mat_no TEXT NOT NULL,
            stage TEXT NOT NULL,
            entry_bin TEXT,
            last_bin TEXT,
            sloc TEXT,
            qty REAL,
            gr_date TEXT,
            lot_no TEXT,
            entered_ts TEXT NOT NULL,
            last_seen_ts TEXT NOT NULL,
            snapshot_ticks INTEGER NOT NULL DEFAULT 1,
            censored_left INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bss_mat_stage ON BARCODE_STAGE_STATE(mat_no, stage)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bss_stage ON BARCODE_STAGE_STATE(stage)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS BARCODE_STAGE_EPISODES (
            episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL,
            mat_no TEXT NOT NULL,
            stage TEXT NOT NULL,
            entry_bin TEXT,
            last_bin TEXT,
            entered_ts TEXT NOT NULL,
            exited_ts TEXT NOT NULL,
            dwell_hours REAL NOT NULL,
            dwell_days REAL NOT NULL,
            next_stage TEXT NOT NULL,
            snapshot_ticks INTEGER NOT NULL DEFAULT 1,
            censored_left INTEGER NOT NULL DEFAULT 0,
            gr_date TEXT,
            lot_no TEXT
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bse_mat_stage ON BARCODE_STAGE_EPISODES(mat_no, stage)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bse_stage ON BARCODE_STAGE_EPISODES(stage)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bse_entered ON BARCODE_STAGE_EPISODES(stage, mat_no, entered_ts)")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS STAGE_DWELL_PROFILE (
            mat_no TEXT NOT NULL,
            stage TEXT NOT NULL,
            n_completed INTEGER NOT NULL,
            n_active INTEGER NOT NULL,
            n_left_censored INTEGER NOT NULL,
            raw_median_hours REAL,
            raw_p90_hours REAL,
            shrunk_median_hours REAL NOT NULL,
            shrunk_p90_hours REAL NOT NULL,
            shrunk_median_days REAL NOT NULL,
            shrunk_p90_days REAL NOT NULL,
            shrink_weight REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mat_no, stage)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS STAGE_TRANSITION_STATS (
            mat_no TEXT NOT NULL,
            from_stage TEXT NOT NULL,
            to_stage TEXT NOT NULL,
            transition_count INTEGER NOT NULL,
            from_total INTEGER NOT NULL,
            transition_share REAL NOT NULL,
            is_off_path INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (mat_no, from_stage, to_stage)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS D114_SHADOW_BIN_DAILY (
            run_date TEXT NOT NULL,
            target_list TEXT NOT NULL,
            bin TEXT NOT NULL,
            stage TEXT NOT NULL,
            active_box_count INTEGER NOT NULL,
            overstay_box_count INTEGER NOT NULL,
            overtaken_suspect_box_count INTEGER NOT NULL,
            max_overstay_ratio REAL NOT NULL,
            max_overtaken_by_count INTEGER NOT NULL,
            top_mat_no TEXT,
            top_barcode TEXT,
            selected_in_prod_list INTEGER NOT NULL DEFAULT 0,
            shadow_weight REAL NOT NULL DEFAULT 0.0,
            snapshot_ts TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (run_date, target_list, bin)
        )
        """
    )
    conn.commit()
    return conn


def kaplan_meier_quantiles(
    durations: np.ndarray,
    events: np.ndarray,
    quantiles: Tuple[float, ...] = (0.50, 0.90),
) -> Tuple[float, ...]:
    """
    Compute right-censored survival quantiles (`q=0.50` -> median, `q=0.90` -> p90 where `S(t) <= 1 - q`).
    `events`: 1 = completed exit (`BARCODE_STAGE_EPISODES`), 0 = right-censored active stay (`BARCODE_STAGE_STATE`).
    If survival curve never drops below `1 - q` (heavy censoring in slow stages like S1/S2 during week 1),
    returns the empirical `q`-quantile across all observed durations as a conservative non-collapsing lower bound.
    """
    if len(durations) == 0:
        return tuple(0.0 for _ in quantiles)

    d = np.asarray(durations, dtype=np.float64)
    e = np.asarray(events, dtype=np.int8)
    valid = np.isfinite(d) & (d >= 0.0)
    d = d[valid]
    e = e[valid]
    if len(d) == 0:
        return tuple(0.0 for _ in quantiles)

    ev_times = np.unique(d[e == 1])
    if len(ev_times) == 0:
        return tuple(float(np.quantile(d, q)) for q in quantiles)

    order = np.argsort(d, kind="mergesort")
    d_sorted = d[order]
    n_total = len(d_sorted)

    at_risk = n_total - np.searchsorted(d_sorted, ev_times, side="left")
    ev_only_sorted = np.sort(d[e == 1])
    left_idx = np.searchsorted(ev_only_sorted, ev_times, side="left")
    right_idx = np.searchsorted(ev_only_sorted, ev_times, side="right")
    d_counts = right_idx - left_idx

    hazards = np.clip(d_counts / np.maximum(at_risk, 1), 0.0, 1.0)
    surv = np.cumprod(1.0 - hazards)

    out: List[float] = []
    emp_fallbacks = np.quantile(d, quantiles)
    for q, emp_q in zip(quantiles, emp_fallbacks):
        target_s = 1.0 - q
        idx_hit = np.where(surv <= target_s + 1e-12)[0]
        if len(idx_hit) > 0:
            km_val = float(ev_times[idx_hit[0]])
            out.append(max(km_val, 0.0))
        else:
            out.append(float(max(ev_times[-1], emp_q)))
    return tuple(out)


def _check_memory_guard(max_ram_pct: float = 85.0) -> None:
    try:
        import psutil
        if psutil.virtual_memory().percent >= max_ram_pct:
            gc.collect()
    except Exception:
        pass


def ingest_snapshot_dataframe(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    snapshot_ts_str: str,
    filename: str,
    floor_prefixes: Optional[Tuple[str, ...]] = None,
) -> Dict[str, int]:
    """
    Process a single chronological WMS Stock List snapshot into `BARCODE_STAGE_STATE` & `BARCODE_STAGE_EPISODES`.
    """
    cur = conn.cursor()
    wm = cur.execute(
        "SELECT first_snapshot_ts, last_snapshot_ts, snapshots_processed FROM STAGE_WATERMARK WHERE source_name='wms_parts_stack'"
    ).fetchone()
    if wm and wm[1] and snapshot_ts_str <= str(wm[1]):
        return {"skipped": 1, "active": 0, "episodes_closed": 0}

    is_very_first_snapshot = wm is None or int(wm[2] or 0) == 0
    first_ts_str = snapshot_ts_str if is_very_first_snapshot else str(wm[0])
    snap_dt = datetime.datetime.strptime(snapshot_ts_str, "%Y-%m-%d %H:%M:%S")

    pfxs = floor_prefixes if floor_prefixes is not None else _get_floor_buffer_prefixes()

    prev_state: Dict[str, Tuple[Any, ...]] = {}
    for row in cur.execute(
        "SELECT barcode, mat_no, stage, entry_bin, last_bin, sloc, qty, gr_date, lot_no, entered_ts, last_seen_ts, snapshot_ticks, censored_left FROM BARCODE_STAGE_STATE"
    ).fetchall():
        prev_state[row[0]] = row

    seen_barcodes = set()
    to_upsert_state: List[Tuple[Any, ...]] = []
    to_insert_episodes: List[Tuple[Any, ...]] = []

    cols = df.columns
    bc_vals = df["Barcode"].astype(str).str.strip().tolist() if "Barcode" in cols else []
    mat_vals = df["Mat.No"].astype(str).str.strip().tolist() if "Mat.No" in cols else [""] * len(bc_vals)
    loc_vals = df["GI Loc"].tolist() if "GI Loc" in cols else [""] * len(bc_vals)
    bin_vals = df["BIN"].tolist() if "BIN" in cols else [""] * len(bc_vals)
    qty_vals = df["Qty."].tolist() if "Qty." in cols else [0.0] * len(bc_vals)
    gr_vals = df["GR Date"].astype(str).str.strip().tolist() if "GR Date" in cols else [""] * len(bc_vals)
    lot_vals = df["Lot No"].astype(str).str.strip().tolist() if "Lot No" in cols else [""] * len(bc_vals)

    for bc, mat, gloc, bval, qty_raw, gr_d, lot_n in zip(
        bc_vals, mat_vals, loc_vals, bin_vals, qty_vals, gr_vals, lot_vals
    ):
        if not bc or bc.lower() == "nan":
            continue
        seen_barcodes.add(bc)
        stage, sloc = classify_d114_stage(gloc, bval, floor_prefixes=pfxs)
        b_clean = "" if bval is None or str(bval).strip().lower() == "nan" else str(bval).strip().upper()
        try:
            q_f = float(qty_raw or 0.0)
        except Exception:
            q_f = 0.0

        prev = prev_state.get(bc)
        if stage == "EXCLUDED":
            if prev is not None:
                p_mat, p_stage, p_ebin, p_lbin, _, _, p_gr, p_lot, p_ent, _, p_ticks, p_cens = prev[1:]
                ent_dt = datetime.datetime.strptime(p_ent, "%Y-%m-%d %H:%M:%S")
                dwell_h = max(0.0, (snap_dt - ent_dt).total_seconds() / 3600.0)
                to_insert_episodes.append(
                    (
                        bc, p_mat, p_stage, p_ebin, p_lbin, p_ent, snapshot_ts_str,
                        round(dwell_h, 4), round(dwell_h / 24.0, 6), "EXCLUDED", int(p_ticks), int(p_cens), p_gr, p_lot
                    )
                )
            continue

        if prev is None:
            cens = 1 if is_very_first_snapshot else 0
            to_upsert_state.append(
                (bc, mat, stage, b_clean, b_clean, sloc, q_f, gr_d, lot_n, snapshot_ts_str, snapshot_ts_str, 1, cens)
            )
        else:
            p_mat, p_stage, p_ebin, p_lbin, _, _, p_gr, p_lot, p_ent, _, p_ticks, p_cens = prev[1:]
            if p_stage == stage:
                to_upsert_state.append(
                    (
                        bc, mat or p_mat, stage, p_ebin, b_clean or p_lbin, sloc, q_f,
                        gr_d or p_gr, lot_n or p_lot, p_ent, snapshot_ts_str, int(p_ticks) + 1, int(p_cens)
                    )
                )
            else:
                ent_dt = datetime.datetime.strptime(p_ent, "%Y-%m-%d %H:%M:%S")
                dwell_h = max(0.0, (snap_dt - ent_dt).total_seconds() / 3600.0)
                to_insert_episodes.append(
                    (
                        bc, p_mat, p_stage, p_ebin, p_lbin, p_ent, snapshot_ts_str,
                        round(dwell_h, 4), round(dwell_h / 24.0, 6), stage, int(p_ticks), int(p_cens), p_gr, p_lot
                    )
                )
                to_upsert_state.append(
                    (bc, mat or p_mat, stage, b_clean, b_clean, sloc, q_f, gr_d or p_gr, lot_n or p_lot, snapshot_ts_str, snapshot_ts_str, 1, 0)
                )

    disappeared_bcs: List[Tuple[str]] = []
    for bc, prev in prev_state.items():
        if bc not in seen_barcodes:
            p_mat, p_stage, p_ebin, p_lbin, _, _, p_gr, p_lot, p_ent, _, p_ticks, p_cens = prev[1:]
            ent_dt = datetime.datetime.strptime(p_ent, "%Y-%m-%d %H:%M:%S")
            dwell_h = max(0.0, (snap_dt - ent_dt).total_seconds() / 3600.0)
            to_insert_episodes.append(
                (
                    bc, p_mat, p_stage, p_ebin, p_lbin, p_ent, snapshot_ts_str,
                    round(dwell_h, 4), round(dwell_h / 24.0, 6), "CONSUMED_OR_GI", int(p_ticks), int(p_cens), p_gr, p_lot
                )
            )
            disappeared_bcs.append((bc,))

    upserted_bcs = {r[0] for r in to_upsert_state}
    excluded_now_bcs = [(bc,) for bc in (seen_barcodes - upserted_bcs) if bc in prev_state]

    if disappeared_bcs or excluded_now_bcs:
        cur.executemany("DELETE FROM BARCODE_STAGE_STATE WHERE barcode = ?", disappeared_bcs + excluded_now_bcs)

    if to_upsert_state:
        cur.executemany(
            """
            INSERT INTO BARCODE_STAGE_STATE (
                barcode, mat_no, stage, entry_bin, last_bin, sloc, qty, gr_date, lot_no,
                entered_ts, last_seen_ts, snapshot_ticks, censored_left
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                mat_no = excluded.mat_no,
                stage = excluded.stage,
                entry_bin = excluded.entry_bin,
                last_bin = excluded.last_bin,
                sloc = excluded.sloc,
                qty = excluded.qty,
                gr_date = excluded.gr_date,
                lot_no = excluded.lot_no,
                entered_ts = excluded.entered_ts,
                last_seen_ts = excluded.last_seen_ts,
                snapshot_ticks = excluded.snapshot_ticks,
                censored_left = excluded.censored_left
            """,
            to_upsert_state,
        )

    if to_insert_episodes:
        cur.executemany(
            """
            INSERT INTO BARCODE_STAGE_EPISODES (
                barcode, mat_no, stage, entry_bin, last_bin, entered_ts, exited_ts,
                dwell_hours, dwell_days, next_stage, snapshot_ticks, censored_left, gr_date, lot_no
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_insert_episodes,
        )

    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_cnt = 1 if is_very_first_snapshot else int(wm[2]) + 1
    cur.execute(
        """
        INSERT INTO STAGE_WATERMARK (source_name, first_snapshot_ts, last_snapshot_ts, last_filename, snapshots_processed, updated_at)
        VALUES ('wms_parts_stack', ?, ?, ?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            last_snapshot_ts = excluded.last_snapshot_ts,
            last_filename = excluded.last_filename,
            snapshots_processed = excluded.snapshots_processed,
            updated_at = excluded.updated_at
        """,
        (first_ts_str, snapshot_ts_str, filename, new_cnt, now_s),
    )
    conn.commit()
    return {
        "skipped": 0,
        "active": len(to_upsert_state),
        "episodes_closed": len(to_insert_episodes),
    }


def ingest_wms_parts_stack_incremental(
    stack_dir: Path = DEFAULT_WMS_PARTS_STACK_DIR,
    db_path: Path = DEFAULT_SHADOW_DB_PATH,
    max_files: Optional[int] = None,
    max_snapshot_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Incrementally ingest all unprocessed `WMS_StockList_*.xlsx` snapshots in chronological order.
    Lowers process priority (`BELOW_NORMAL_PRIORITY_CLASS`) and streams 1 file at a time with `gc.collect()`.
    """
    try:
        import psutil
        psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass

    conn = init_shadow_db(db_path)
    try:
        wm = conn.execute(
            "SELECT last_snapshot_ts, snapshots_processed FROM STAGE_WATERMARK WHERE source_name='wms_parts_stack'"
        ).fetchone()
        last_ts = str(wm[0]) if wm and wm[0] else ""

        stack_dir = Path(stack_dir)
        candidates: List[Tuple[str, Path]] = []
        if stack_dir.exists():
            for fp in stack_dir.glob("WMS_StockList_*.xlsx"):
                if fp.name.startswith("~$") or fp.stat().st_size < 1024:
                    continue
                if "latest" in fp.name.lower():
                    continue
                ts_s = parse_snapshot_timestamp(fp).strftime("%Y-%m-%d %H:%M:%S")
                if ts_s > last_ts and (max_snapshot_ts is None or ts_s <= str(max_snapshot_ts)):
                    candidates.append((ts_s, fp))

        candidates.sort(key=lambda item: (item[0], item[1].name))
        if max_files is not None:
            candidates = candidates[: int(max_files)]

        wanted_cols = {"Barcode", "Mat.No", "GI Loc", "BIN", "Qty.", "GR Date", "Lot No"}
        processed = 0
        total_closed = 0
        pfxs = _get_floor_buffer_prefixes()

        for ts_s, fp in candidates:
            _check_memory_guard()
            df = pd.read_excel(fp, usecols=lambda c: c in wanted_cols)
            res = ingest_snapshot_dataframe(conn, df, ts_s, fp.name, floor_prefixes=pfxs)
            del df
            gc.collect()
            if not res.get("skipped"):
                processed += 1
                total_closed += int(res.get("episodes_closed", 0))

        wm_after = conn.execute(
            "SELECT first_snapshot_ts, last_snapshot_ts, snapshots_processed FROM STAGE_WATERMARK WHERE source_name='wms_parts_stack'"
        ).fetchone()
        return {
            "newly_processed_files": processed,
            "episodes_closed_in_run": total_closed,
            "first_snapshot_ts": wm_after[0] if wm_after else None,
            "last_snapshot_ts": wm_after[1] if wm_after else None,
            "total_snapshots_processed": int(wm_after[2]) if wm_after else 0,
        }
    finally:
        conn.close()


def recompute_profiles_and_transitions(
    db_path: Path = DEFAULT_SHADOW_DB_PATH,
    prior_strength_k: float = 10.0,
) -> Dict[str, Any]:
    """
    Recomputes:
      1. `STAGE_DWELL_PROFILE` (`ALL` stage priors + Bayesian-shrunk `mat_no x stage` median & p90)
         accounting for right-censored active stays via Kaplan-Meier (`censored_left == 0` preferred;
         includes `censored_left == 1` lower bounds when exact-entry observations < 10).
      2. `STAGE_TRANSITION_STATS` (`(ALL, from, to)` and `(mat_no, from, to)` path shares).
    """
    conn = init_shadow_db(db_path)
    try:
        cur = conn.cursor()
        wm = cur.execute(
            "SELECT last_snapshot_ts FROM STAGE_WATERMARK WHERE source_name='wms_parts_stack'"
        ).fetchone()
        if not wm or not wm[0]:
            return {"profiles": 0, "transitions": 0, "stage_priors": {}}
        latest_dt = datetime.datetime.strptime(str(wm[0]), "%Y-%m-%d %H:%M:%S")
        now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        ep_rows = cur.execute(
            """
            SELECT mat_no, stage, dwell_hours, censored_left, next_stage
            FROM BARCODE_STAGE_EPISODES
            WHERE stage IN ('S1', 'S1_TO_P1_TRANSIT', 'S2', 'S3', 'S4', 'S5')
            """
        ).fetchall()

        act_rows = cur.execute(
            """
            SELECT mat_no, stage, entered_ts, censored_left
            FROM BARCODE_STAGE_STATE
            WHERE stage IN ('S1', 'S1_TO_P1_TRANSIT', 'S2', 'S3', 'S4', 'S5')
            """
        ).fetchall()

        stage_exact_obs: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        stage_all_obs: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
        mat_exact_obs: Dict[Tuple[str, str], List[Tuple[float, int]]] = defaultdict(list)
        mat_all_obs: Dict[Tuple[str, str], List[Tuple[float, int]]] = defaultdict(list)
        counts_meta: Dict[Tuple[str, str], List[int]] = defaultdict(lambda: [0, 0, 0])
        stage_meta: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])

        trans_mat: Dict[Tuple[str, str, str], int] = defaultdict(int)
        trans_all: Dict[Tuple[str, str], int] = defaultdict(int)

        for mat, stg, dw_h, cens_l, nxt_stg in ep_rows:
            dh = max(0.0, float(dw_h or 0.0))
            cl = int(cens_l or 0)
            stage_all_obs[stg].append((dh, 1))
            mat_all_obs[(mat, stg)].append((dh, 1))
            counts_meta[(mat, stg)][0] += 1
            stage_meta[stg][0] += 1
            if cl == 0:
                stage_exact_obs[stg].append((dh, 1))
                mat_exact_obs[(mat, stg)].append((dh, 1))
            else:
                counts_meta[(mat, stg)][2] += 1
                stage_meta[stg][2] += 1

            trans_mat[(mat, stg, nxt_stg)] += 1
            trans_all[(stg, nxt_stg)] += 1

        for mat, stg, ent_ts, cens_l in act_rows:
            ent_dt = datetime.datetime.strptime(str(ent_ts), "%Y-%m-%d %H:%M:%S")
            dh = max(0.0, (latest_dt - ent_dt).total_seconds() / 3600.0)
            cl = int(cens_l or 0)
            stage_all_obs[stg].append((dh, 0))
            mat_all_obs[(mat, stg)].append((dh, 0))
            counts_meta[(mat, stg)][1] += 1
            stage_meta[stg][1] += 1
            if cl == 0:
                stage_exact_obs[stg].append((dh, 0))
                mat_exact_obs[(mat, stg)].append((dh, 0))
            else:
                counts_meta[(mat, stg)][2] += 1
                stage_meta[stg][2] += 1

        stage_priors: Dict[str, Tuple[float, float]] = {}
        profile_rows: List[Tuple[Any, ...]] = []

        for stg in ("S1", "S1_TO_P1_TRANSIT", "S2", "S3", "S4", "S5"):
            obs = stage_exact_obs[stg] if len(stage_exact_obs[stg]) >= 10 else stage_all_obs[stg]
            if obs:
                d_arr = np.array([x[0] for x in obs], dtype=np.float64)
                e_arr = np.array([x[1] for x in obs], dtype=np.int8)
                med_h, p90_h = kaplan_meier_quantiles(d_arr, e_arr, (0.50, 0.90))
            else:
                med_h, p90_h = 24.0, 72.0
            p90_h = max(p90_h, med_h, 0.5)
            stage_priors[stg] = (med_h, p90_h)
            n_c, n_a, n_lc = stage_meta[stg]
            profile_rows.append(
                (
                    "__ALL__", stg, n_c, n_a, n_lc,
                    round(med_h, 4), round(p90_h, 4),
                    round(med_h, 4), round(p90_h, 4),
                    round(med_h / 24.0, 4), round(p90_h / 24.0, 4),
                    1.0, now_s,
                )
            )

        for (mat, stg), meta in counts_meta.items():
            obs = mat_exact_obs[(mat, stg)] if len(mat_exact_obs[(mat, stg)]) >= 5 else mat_all_obs[(mat, stg)]
            prior_med, prior_p90 = stage_priors[stg]
            n_c, n_a, n_lc = meta
            if obs:
                d_arr = np.array([x[0] for x in obs], dtype=np.float64)
                e_arr = np.array([x[1] for x in obs], dtype=np.int8)
                raw_med, raw_p90 = kaplan_meier_quantiles(d_arr, e_arr, (0.50, 0.90))
                raw_p90 = max(raw_p90, raw_med)
            else:
                raw_med, raw_p90 = prior_med, prior_p90

            n_exact_c = sum(1 for _, ev in mat_exact_obs[(mat, stg)] if ev == 1)
            n_exact_a = sum(1 for _, ev in mat_exact_obs[(mat, stg)] if ev == 0)
            n_eff = float(n_exact_c) + 0.5 * float(n_exact_a)
            if n_eff <= 0.0:
                n_eff = 0.25 * float(n_c + n_a)
            w = n_eff / (n_eff + float(prior_strength_k))

            shr_med = w * raw_med + (1.0 - w) * prior_med
            shr_p90 = max(shr_med, w * raw_p90 + (1.0 - w) * prior_p90, 0.5)

            profile_rows.append(
                (
                    mat, stg, n_c, n_a, n_lc,
                    round(raw_med, 4), round(raw_p90, 4),
                    round(shr_med, 4), round(shr_p90, 4),
                    round(shr_med / 24.0, 4), round(shr_p90 / 24.0, 4),
                    round(w, 4), now_s,
                )
            )

        cur.execute("DELETE FROM STAGE_DWELL_PROFILE")
        cur.executemany(
            """
            INSERT INTO STAGE_DWELL_PROFILE (
                mat_no, stage, n_completed, n_active, n_left_censored,
                raw_median_hours, raw_p90_hours, shrunk_median_hours, shrunk_p90_hours,
                shrunk_median_days, shrunk_p90_days, shrink_weight, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            profile_rows,
        )

        trans_rows: List[Tuple[Any, ...]] = []
        from_totals_all: Dict[str, int] = defaultdict(int)
        for (stg, _), c in trans_all.items():
            from_totals_all[stg] += c
        for (stg, nxt), c in trans_all.items():
            tot = max(1, from_totals_all[stg])
            sh = c / tot
            off = 1 if (tot >= 10 and sh < 0.05) else 0
            trans_rows.append(("__ALL__", stg, nxt, c, tot, round(sh, 4), off, now_s))

        from_totals_mat: Dict[Tuple[str, str], int] = defaultdict(int)
        for (mat, stg, _), c in trans_mat.items():
            from_totals_mat[(mat, stg)] += c
        for (mat, stg, nxt), c in trans_mat.items():
            tot = max(1, from_totals_mat[(mat, stg)])
            sh = c / tot
            off = 1 if (tot >= 10 and sh < 0.05) else 0
            trans_rows.append((mat, stg, nxt, c, tot, round(sh, 4), off, now_s))

        cur.execute("DELETE FROM STAGE_TRANSITION_STATS")
        if trans_rows:
            cur.executemany(
                """
                INSERT INTO STAGE_TRANSITION_STATS (
                    mat_no, from_stage, to_stage, transition_count, from_total,
                    transition_share, is_off_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                trans_rows,
            )

        conn.commit()
        return {
            "profiles": len(profile_rows),
            "transitions": len(trans_rows),
            "stage_priors": {
                k: {"median_hours": round(v[0], 2), "p90_hours": round(v[1], 2), "median_days": round(v[0] / 24.0, 3)}
                for k, v in stage_priors.items()
            },
        }
    finally:
        conn.close()


def _load_selected_bins_from_audit_ledger(
    run_date: str,
    inv_db_path: Path = DEFAULT_INVENTORY_HISTORY_DB_PATH,
) -> Dict[str, List[str]]:
    """Read-only lookup of production selected bins from `AUDIT_ASSIGNMENT` for standalone CLI runs."""
    out: Dict[str, List[str]] = {"Daily": [], "Weekly": []}
    if not inv_db_path.exists():
        return out
    try:
        conn = sqlite3.connect(f"file:{inv_db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = conn.execute(
                """
                SELECT r.mode, a.bin
                FROM AUDIT_ASSIGNMENT a
                JOIN AUDIT_RUN r ON a.run_id = r.run_id
                WHERE r.run_date = ?
                  AND r.run_id NOT LIKE '%_TEST_%'
                """,
                (run_date,),
            ).fetchall()
            for mode_val, bin_val in rows:
                m = "Weekly" if str(mode_val or "").strip().lower() == "weekly" else "Daily"
                if bin_val:
                    out[m].append(str(bin_val).strip().upper())
        finally:
            conn.close()
    except Exception:
        pass
    return out


def compute_and_save_shadow_signals(
    run_date: Optional[str] = None,
    selected_bins_by_mode: Optional[Dict[str, Iterable[str]]] = None,
    db_path: Path = DEFAULT_SHADOW_DB_PATH,
    inv_db_path: Path = DEFAULT_INVENTORY_HISTORY_DB_PATH,
) -> Dict[str, Any]:
    """
    Computes `zone_overstay_ratio = current_dwell / shrunk_p90` and `overtaken_by_count`
    (number of newer-entry boxes of the same `mat_no` in the same `stage` that ALREADY exited
    to a strictly downstream stage `STAGE_ORDER[next_stage] > STAGE_ORDER[stage]`) for every active
    box in `S1..S5`, aggregates to bin level, and stores in `D114_SHADOW_BIN_DAILY` with `shadow_weight = 0.0`.
    """
    conn = init_shadow_db(db_path)
    try:
        cur = conn.cursor()
        wm = cur.execute(
            "SELECT last_snapshot_ts FROM STAGE_WATERMARK WHERE source_name='wms_parts_stack'"
        ).fetchone()
        if not wm or not wm[0]:
            return {"daily_suspect_bins": 0, "weekly_suspect_bins": 0, "top_daily": [], "top_weekly": []}

        snap_ts = str(wm[0])
        snap_dt = datetime.datetime.strptime(snap_ts, "%Y-%m-%d %H:%M:%S")
        r_date = str(run_date or snap_ts[:10])[:10]
        now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if selected_bins_by_mode is None:
            selected_bins_by_mode = _load_selected_bins_from_audit_ledger(r_date, inv_db_path=inv_db_path)

        p90_map: Dict[Tuple[str, str], float] = {}
        for mat, stg, p90_h in cur.execute(
            "SELECT mat_no, stage, shrunk_p90_hours FROM STAGE_DWELL_PROFILE"
        ).fetchall():
            p90_map[(mat, stg)] = float(p90_h)

        downstream_entries: Dict[Tuple[str, str], List[Tuple[str, str, str]]] = defaultdict(list)
        for mat, stg, ent_ts, nxt_stg, gr_d, bc in cur.execute(
            """
            SELECT mat_no, stage, entered_ts, next_stage, COALESCE(gr_date, ''), barcode
            FROM BARCODE_STAGE_EPISODES
            WHERE stage IN ('S1', 'S2', 'S3', 'S4', 'S5')
            """
        ).fetchall():
            if STAGE_ORDER.get(nxt_stg, -1.0) > STAGE_ORDER.get(stg, 99.0):
                downstream_entries[(mat, stg)].append((str(ent_ts), str(gr_d), str(bc)))

        for k in downstream_entries:
            downstream_entries[k].sort(key=lambda x: x[0])

        import bisect

        selected_lookup: Dict[str, set] = {
            "Daily": {str(b).strip().upper() for b in (selected_bins_by_mode or {}).get("Daily", [])},
            "Weekly": {str(b).strip().upper() for b in (selected_bins_by_mode or {}).get("Weekly", [])},
        }

        bin_agg: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        active_rows = cur.execute(
            """
            SELECT barcode, mat_no, stage, last_bin, entered_ts, COALESCE(gr_date, ''), censored_left
            FROM BARCODE_STAGE_STATE
            WHERE stage IN ('S1', 'S2', 'S3', 'S4', 'S5')
            """
        ).fetchall()

        for bc, mat, stg, lbin, ent_ts, gr_d, cens_l in active_rows:
            b_clean = str(lbin or "").strip().upper()
            if not b_clean:
                continue
            ent_dt = datetime.datetime.strptime(str(ent_ts), "%Y-%m-%d %H:%M:%S")
            cur_h = max(0.0, (snap_dt - ent_dt).total_seconds() / 3600.0)
            p90_h = p90_map.get((mat, stg)) or p90_map.get(("__ALL__", stg)) or 24.0
            overstay_ratio = cur_h / max(p90_h, 0.5)

            ds_list = downstream_entries.get((mat, stg))
            overtaken_cnt = 0
            if ds_list:
                ts_keys = [x[0] for x in ds_list]
                idx = bisect.bisect_right(ts_keys, str(ent_ts))
                if int(cens_l or 0) == 0:
                    overtaken_cnt = sum(1 for x in ds_list[idx:] if x[2] != bc)
                else:
                    overtaken_cnt = sum(
                        1 for x in ds_list[idx:]
                        if x[2] != bc and (not gr_d or not x[1] or x[1] >= gr_d)
                    )

            is_overstay = overstay_ratio > 1.0
            is_suspect = is_overstay and overtaken_cnt >= 1
            t_list = STAGE_TARGET_LIST[stg]
            key = (t_list, b_clean, stg)
            agg = bin_agg.get(key)
            if agg is None:
                agg = {
                    "active_box_count": 0,
                    "overstay_box_count": 0,
                    "overtaken_suspect_box_count": 0,
                    "max_overstay_ratio": 0.0,
                    "max_overtaken_by_count": 0,
                    "best_rank_tuple": (-1, -1.0, -1),
                    "top_mat_no": mat,
                    "top_barcode": bc,
                }
                bin_agg[key] = agg

            agg["active_box_count"] += 1
            if is_overstay:
                agg["overstay_box_count"] += 1
            if is_suspect:
                agg["overtaken_suspect_box_count"] += 1
            if overstay_ratio > agg["max_overstay_ratio"]:
                agg["max_overstay_ratio"] = overstay_ratio
            if overtaken_cnt > agg["max_overtaken_by_count"]:
                agg["max_overtaken_by_count"] = overtaken_cnt

            cand_rank = (1 if is_suspect else 0, overstay_ratio, overtaken_cnt)
            if cand_rank > agg["best_rank_tuple"]:
                agg["best_rank_tuple"] = cand_rank
                agg["top_mat_no"] = mat
                agg["top_barcode"] = bc

        out_rows: List[Tuple[Any, ...]] = []
        for (t_list, b_clean, stg), agg in bin_agg.items():
            in_prod = 1 if b_clean in selected_lookup.get(t_list, set()) else 0
            out_rows.append(
                (
                    r_date,
                    t_list,
                    b_clean,
                    stg,
                    agg["active_box_count"],
                    agg["overstay_box_count"],
                    agg["overtaken_suspect_box_count"],
                    round(agg["max_overstay_ratio"], 4),
                    agg["max_overtaken_by_count"],
                    agg["top_mat_no"],
                    agg["top_barcode"],
                    in_prod,
                    0.0,
                    snap_ts,
                    now_s,
                )
            )

        cur.execute("DELETE FROM D114_SHADOW_BIN_DAILY WHERE run_date = ?", (r_date,))
        if out_rows:
            cur.executemany(
                """
                INSERT INTO D114_SHADOW_BIN_DAILY (
                    run_date, target_list, bin, stage, active_box_count,
                    overstay_box_count, overtaken_suspect_box_count, max_overstay_ratio,
                    max_overtaken_by_count, top_mat_no, top_barcode, selected_in_prod_list,
                    shadow_weight, snapshot_ts, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                out_rows,
            )
        conn.commit()

        top_daily = cur.execute(
            """
            SELECT bin, stage, overtaken_suspect_box_count, max_overstay_ratio, max_overtaken_by_count, top_mat_no
            FROM D114_SHADOW_BIN_DAILY
            WHERE run_date = ? AND target_list = 'Daily' AND overtaken_suspect_box_count > 0
            ORDER BY max_overstay_ratio DESC, max_overtaken_by_count DESC, bin ASC
            LIMIT 10
            """,
            (r_date,),
        ).fetchall()
        top_weekly = cur.execute(
            """
            SELECT bin, stage, overtaken_suspect_box_count, max_overstay_ratio, max_overtaken_by_count, top_mat_no
            FROM D114_SHADOW_BIN_DAILY
            WHERE run_date = ? AND target_list = 'Weekly' AND overtaken_suspect_box_count > 0
            ORDER BY max_overstay_ratio DESC, max_overtaken_by_count DESC, bin ASC
            LIMIT 10
            """,
            (r_date,),
        ).fetchall()
        d_cnt = cur.execute(
            "SELECT COUNT(*) FROM D114_SHADOW_BIN_DAILY WHERE run_date = ? AND target_list = 'Daily' AND overtaken_suspect_box_count > 0",
            (r_date,),
        ).fetchone()[0]
        w_cnt = cur.execute(
            "SELECT COUNT(*) FROM D114_SHADOW_BIN_DAILY WHERE run_date = ? AND target_list = 'Weekly' AND overtaken_suspect_box_count > 0",
            (r_date,),
        ).fetchone()[0]

        return {
            "run_date": r_date,
            "snapshot_ts": snap_ts,
            "daily_suspect_bins": int(d_cnt),
            "weekly_suspect_bins": int(w_cnt),
            "top_daily": [tuple(r) for r in top_daily],
            "top_weekly": [tuple(r) for r in top_weekly],
        }
    finally:
        conn.close()


def run_d114_shadow_update(
    stack_dir: Path = DEFAULT_WMS_PARTS_STACK_DIR,
    db_path: Path = DEFAULT_SHADOW_DB_PATH,
    run_date: Optional[str] = None,
    selected_bins_by_mode: Optional[Dict[str, Iterable[str]]] = None,
    max_snapshot_ts: Optional[str] = None,
) -> Dict[str, Any]:
    """End-to-end incremental ingest -> KM/Bayesian profile recompute -> shadow bin evaluation (`weight = 0.0`)."""
    ing = ingest_wms_parts_stack_incremental(
        stack_dir=stack_dir,
        db_path=db_path,
        max_snapshot_ts=max_snapshot_ts,
    )
    prof = recompute_profiles_and_transitions(db_path=db_path)
    sig = compute_and_save_shadow_signals(
        run_date=run_date,
        selected_bins_by_mode=selected_bins_by_mode,
        db_path=db_path,
    )
    return {"ingest": ing, "profiles": prof, "signals": sig}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="D114 Stage-Flow Shadow Engine")
    parser.add_argument("--stack-dir", type=Path, default=DEFAULT_WMS_PARTS_STACK_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_SHADOW_DB_PATH)
    parser.add_argument("--run-date", type=str, default=None)
    parser.add_argument("--max-snapshot-ts", type=str, default=None)
    parser.add_argument(
        "--mode",
        type=str,
        default="full",
        choices=["full", "ingest_only"],
        help="ingest_only: incremental wms_parts_stack ingestion only (every 2h); full: ingest + profile + D114_SHADOW_BIN_DAILY",
    )
    args = parser.parse_args()

    if args.mode == "ingest_only":
        summary = {
            "mode": "ingest_only",
            "ingest": ingest_wms_parts_stack_incremental(
                stack_dir=args.stack_dir,
                db_path=args.db_path,
                max_snapshot_ts=args.max_snapshot_ts,
            ),
        }
    else:
        summary = run_d114_shadow_update(
            stack_dir=args.stack_dir,
            db_path=args.db_path,
            run_date=args.run_date,
            max_snapshot_ts=args.max_snapshot_ts,
        )
    print(json.dumps(summary, indent=2, ensure_ascii=False))
