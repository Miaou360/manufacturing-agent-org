"""
Daily Permanent Snapshot Archive — WMS Barcode Daily & SAP MB52 Daily (#236-B / #237-3)
=======================================================================================
Implements the operational retention principle:
  "Raw Excel exports are pruned after 7 days (after ledger verification);
   compressed daily SQLite snapshots are preserved permanently."

Ledgers:
  1. `00_JOB_StockAccuracy/db/wms_barcode_daily.db` -> `WMS_BARCODE_DAILY` + `WMS_BARCODE_DAILY_RUNS`
     - PK: `(snapshot_date, barcode)` (`WITHOUT ROWID`)
     - Daily 1-file selection priority:
         Tier 1 (`06:51_DAILY`): `wms_daily_stack` snapshot in `[06:40:00, 07:20:00]` closest to `06:51:00`
         Tier 2 (`04:16_EARLY`): `wms_daily_stack` snapshot in `[04:00:00, 04:45:00]` closest to `04:16:00`
         Tier 3 (`EARLIEST_FALLBACK`): earliest timestamped `WMS_StockList_*` across `wms_daily_stack` & `wms_parts_stack`
  2. `00_JOB_StockAccuracy/db/sap_stock_daily.db` -> `SAP_STOCK_DAILY` + `SAP_STOCK_DAILY_RUNS`
     - PK: `(snapshot_date, plant, mat_no, sloc)` (`WITHOUT ROWID`)
     - Selects `MB52_*` snapshot on `snapshot_date` whose timestamp is closest to `WMS_BARCODE_DAILY`'s
       `snapshot_time` (default target `06:51:00`), ensuring tight SAP/WMS timestamp pairing.
"""

from __future__ import annotations

import datetime
import gc
import logging
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd

DEPT_DIR = Path(__file__).resolve().parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from wms_mapping import map_wms_gi_loc_to_sap

logger = logging.getLogger("DailySnapshotArchive")

DEFAULT_WMS_DAILY_DB = DEPT_DIR / "00_JOB_StockAccuracy" / "db" / "wms_barcode_daily.db"
DEFAULT_SAP_DAILY_DB = DEPT_DIR / "00_JOB_StockAccuracy" / "db" / "sap_stock_daily.db"

DEFAULT_WMS_DAILY_STACK = DEPT_DIR / "00_JOB_StockAccuracy" / "wms_daily_stack"
DEFAULT_WMS_PARTS_STACK = DEPT_DIR / "01_JOB_GR_GI_Error" / "wms_parts_stack"
DEFAULT_MB52_DIRS: Tuple[Path, ...] = (
    DEPT_DIR / "01_JOB_GR_GI_Error" / "mb52",
    DEPT_DIR / "02_JOB_Backlog_Clear" / "mb52",
)

_TS_RE = re.compile(r"(202\d{5})_(\d{6})")
_DATE_ONLY_RE = re.compile(r"(202\d{5})")


def _lower_priority() -> None:
    try:
        import psutil
        psutil.Process(os.getpid()).nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def _seconds_of_day(hms: str) -> int:
    parts = [int(p) for p in hms.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) > 2 else 0)


def parse_file_date_and_time(fp: Path) -> Optional[Tuple[str, str]]:
    """
    Returns `('YYYY-MM-DD', 'HH:MM:SS')` for timestamped files (excludes canonical `latest` copies).
    """
    name_low = fp.name.lower()
    if fp.name.startswith("~$") or "latest" in name_low:
        return None
    m = _TS_RE.search(fp.stem)
    if m:
        try:
            dt = datetime.datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
            return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M:%S")
        except ValueError:
            return None
    m_d = _DATE_ONLY_RE.search(fp.stem)
    if m_d:
        try:
            d = datetime.datetime.strptime(m_d.group(1), "%Y%m%d").date()
            mtime = datetime.datetime.fromtimestamp(fp.stat().st_mtime)
            t_str = mtime.strftime("%H:%M:%S") if mtime.date() == d else "00:00:00"
            return d.strftime("%Y-%m-%d"), t_str
        except ValueError:
            return None
    return None


def _parse_sap_float(val: Any) -> float:
    if val is None:
        return 0.0
    s = str(val).strip().replace(",", "")
    if not s or s.lower() == "nan":
        return 0.0
    if s.endswith("-"):
        s = "-" + s[:-1].strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


# ==============================================================================
# 1. WMS_BARCODE_DAILY
# ==============================================================================

def init_wms_daily_db(db_path: Path = DEFAULT_WMS_DAILY_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS WMS_BARCODE_DAILY (
            snapshot_date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            source_file   TEXT NOT NULL,
            barcode       TEXT NOT NULL,
            mat_no        TEXT NOT NULL,
            bin           TEXT,
            sloc          TEXT,
            qty           REAL NOT NULL DEFAULT 0.0,
            gr_date       TEXT,
            lot           TEXT,
            PRIMARY KEY (snapshot_date, barcode)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS WMS_BARCODE_DAILY_RUNS (
            snapshot_date   TEXT PRIMARY KEY,
            snapshot_time   TEXT NOT NULL,
            selection_tier  TEXT NOT NULL,
            source_file     TEXT NOT NULL,
            row_count       INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            ingested_at     TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def select_wms_daily_candidate(
    target_date: str,
    wms_daily_dir: Path = DEFAULT_WMS_DAILY_STACK,
    wms_parts_dir: Path = DEFAULT_WMS_PARTS_STACK,
) -> Optional[Tuple[Path, str, str]]:
    """
    Selects `(file_path, snapshot_time, selection_tier)` for `target_date` (`YYYY-MM-DD`):
      1. `06:51_DAILY` in `wms_daily_dir` (`06:40:00` ~ `07:20:00`, closest to `06:51:00`)
      2. `04:16_EARLY` in `wms_daily_dir` (`04:00:00` ~ `04:45:00`, closest to `04:16:00`)
      3. `EARLIEST_FALLBACK` across `wms_daily_dir` + `wms_parts_dir`
    """
    daily_cands: List[Tuple[str, Path]] = []
    all_cands: List[Tuple[str, Path]] = []

    for folder, is_daily in ((Path(wms_daily_dir), True), (Path(wms_parts_dir), False)):
        if not folder.exists():
            continue
        for fp in folder.glob("WMS_StockList_*.xlsx"):
            if not fp.is_file() or fp.stat().st_size < 1024:
                continue
            parsed = parse_file_date_and_time(fp)
            if not parsed or parsed[0] != target_date:
                continue
            t_str = parsed[1]
            all_cands.append((t_str, fp))
            if is_daily:
                daily_cands.append((t_str, fp))

    if not all_cands:
        return None

    target_0651 = _seconds_of_day("06:51:00")
    t1 = [
        (abs(_seconds_of_day(t) - target_0651), t, fp)
        for t, fp in daily_cands
        if _seconds_of_day("06:40:00") <= _seconds_of_day(t) <= _seconds_of_day("07:20:00")
    ]
    if t1:
        t1.sort(key=lambda x: (x[0], x[1], x[2].name))
        return t1[0][2], t1[0][1], "06:51_DAILY"

    target_0416 = _seconds_of_day("04:16:00")
    t2 = [
        (abs(_seconds_of_day(t) - target_0416), t, fp)
        for t, fp in daily_cands
        if _seconds_of_day("04:00:00") <= _seconds_of_day(t) <= _seconds_of_day("04:45:00")
    ]
    if t2:
        t2.sort(key=lambda x: (x[0], x[1], x[2].name))
        return t2[0][2], t2[0][1], "04:16_EARLY"

    all_cands.sort(key=lambda x: (x[0], 0 if x[1].parent == Path(wms_daily_dir) else 1, x[1].name))
    return all_cands[0][1], all_cands[0][0], "EARLIEST_FALLBACK"


def discover_available_wms_dates(
    wms_daily_dir: Path = DEFAULT_WMS_DAILY_STACK,
    wms_parts_dir: Path = DEFAULT_WMS_PARTS_STACK,
) -> List[str]:
    dates = set()
    for folder in (Path(wms_daily_dir), Path(wms_parts_dir)):
        if not folder.exists():
            continue
        for fp in folder.glob("WMS_StockList_*.xlsx"):
            if not fp.is_file() or fp.stat().st_size < 1024:
                continue
            parsed = parse_file_date_and_time(fp)
            if parsed:
                dates.add(parsed[0])
    return sorted(dates)


def ingest_wms_daily_file(
    file_path: Path,
    snapshot_date: str,
    snapshot_time: str,
    selection_tier: str = "MANUAL",
    db_path: Path = DEFAULT_WMS_DAILY_DB,
) -> Dict[str, Any]:
    """
    Idempotently replaces `snapshot_date` in `WMS_BARCODE_DAILY` with rows from `file_path`.
    """
    _lower_priority()
    wanted = {"Barcode", "Mat.No", "GI Loc", "BIN", "Qty.", "GR Date", "Lot No"}
    df = pd.read_excel(file_path, usecols=lambda c: c in wanted)

    cols = df.columns
    n_len = len(df)
    bc_vals = df["Barcode"].tolist() if "Barcode" in cols else []
    mat_vals = df["Mat.No"].tolist() if "Mat.No" in cols else [""] * n_len
    loc_vals = df["GI Loc"].tolist() if "GI Loc" in cols else [""] * n_len
    bin_vals = df["BIN"].tolist() if "BIN" in cols else [""] * n_len
    qty_vals = df["Qty."].tolist() if "Qty." in cols else [0.0] * n_len
    gr_vals = df["GR Date"].tolist() if "GR Date" in cols else [""] * n_len
    lot_vals = df["Lot No"].tolist() if "Lot No" in cols else [""] * n_len

    dedup: Dict[str, Tuple[Any, ...]] = {}
    dup_count = 0
    src_name = file_path.name

    for bc_raw, mat_raw, gloc_raw, bin_raw, qty_raw, gr_raw, lot_raw in zip(
        bc_vals, mat_vals, loc_vals, bin_vals, qty_vals, gr_vals, lot_vals
    ):
        bc = "" if bc_raw is None else str(bc_raw).strip()
        if not bc or bc.lower() == "nan":
            continue
        mat = "" if mat_raw is None else str(mat_raw).strip()
        if mat.lower() == "nan":
            mat = ""
        b_clean = "" if bin_raw is None else str(bin_raw).strip().upper()
        if b_clean == "NAN":
            b_clean = ""
        gloc_str = "" if gloc_raw is None else str(gloc_raw).strip()
        if gloc_str.lower() == "nan":
            gloc_str = ""
        sloc = map_wms_gi_loc_to_sap(gloc_str) or gloc_str.upper()
        q_f = _parse_sap_float(qty_raw)
        gr_s = "" if gr_raw is None else str(gr_raw).strip()
        if gr_s.lower() == "nan":
            gr_s = ""
        lot_s = "" if lot_raw is None else str(lot_raw).strip()
        if lot_s.lower() == "nan":
            lot_s = ""

        if bc in dedup:
            dup_count += 1
            prev = dedup[bc]
            dedup[bc] = (
                snapshot_date,
                snapshot_time,
                src_name,
                bc,
                mat or prev[4],
                b_clean or prev[5],
                sloc or prev[6],
                prev[7] + q_f,
                gr_s or prev[8],
                lot_s or prev[9],
            )
        else:
            dedup[bc] = (
                snapshot_date,
                snapshot_time,
                src_name,
                bc,
                mat,
                b_clean,
                sloc,
                q_f,
                gr_s,
                lot_s,
            )

    del df
    gc.collect()

    rows_to_insert = list(dedup.values())
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = init_wms_daily_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM WMS_BARCODE_DAILY WHERE snapshot_date = ?", (snapshot_date,))
        if rows_to_insert:
            cur.executemany(
                """
                INSERT INTO WMS_BARCODE_DAILY (
                    snapshot_date, snapshot_time, source_file, barcode, mat_no, bin, sloc, qty, gr_date, lot
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
        cur.execute(
            """
            INSERT INTO WMS_BARCODE_DAILY_RUNS (
                snapshot_date, snapshot_time, selection_tier, source_file, row_count, duplicate_count, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                snapshot_time = excluded.snapshot_time,
                selection_tier = excluded.selection_tier,
                source_file = excluded.source_file,
                row_count = excluded.row_count,
                duplicate_count = excluded.duplicate_count,
                ingested_at = excluded.ingested_at
            """,
            (snapshot_date, snapshot_time, selection_tier, src_name, len(rows_to_insert), dup_count, now_s),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "snapshot_date": snapshot_date,
        "snapshot_time": snapshot_time,
        "selection_tier": selection_tier,
        "source_file": src_name,
        "row_count": len(rows_to_insert),
        "duplicate_count": dup_count,
    }


def sync_all_wms_barcode_daily(
    wms_daily_dir: Path = DEFAULT_WMS_DAILY_STACK,
    wms_parts_dir: Path = DEFAULT_WMS_PARTS_STACK,
    db_path: Path = DEFAULT_WMS_DAILY_DB,
    force_dates: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Ensures every date available on disk is ingested into `WMS_BARCODE_DAILY`.
    Automatically upgrades a date if a higher-priority tier (e.g. `06:51_DAILY` replacing `04:16_EARLY`)
    became available, or if `snapshot_date` is in `force_dates`.
    """
    tier_rank = {"06:51_DAILY": 3, "04:16_EARLY": 2, "EARLIEST_FALLBACK": 1, "MANUAL_DOWNLOAD": 0, "MANUAL": 0}
    forced_set = set(force_dates or [])

    conn = init_wms_daily_db(db_path)
    try:
        existing = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT snapshot_date, snapshot_time, selection_tier, source_file, row_count FROM WMS_BARCODE_DAILY_RUNS"
            ).fetchall()
        }
    finally:
        conn.close()

    avail_dates = discover_available_wms_dates(wms_daily_dir=wms_daily_dir, wms_parts_dir=wms_parts_dir)
    ingested_results = []
    skipped_dates = []
    errors = []

    for d_str in avail_dates:
        cand = select_wms_daily_candidate(d_str, wms_daily_dir=wms_daily_dir, wms_parts_dir=wms_parts_dir)
        if not cand:
            continue
        fp, snap_time, tier = cand
        prev = existing.get(d_str)
        need_ingest = (
            d_str in forced_set
            or prev is None
            or int(prev[3] or 0) == 0
            or tier_rank.get(tier, 0) > tier_rank.get(prev[1], 0)
        )
        if not need_ingest:
            skipped_dates.append(d_str)
            continue
        try:
            res = ingest_wms_daily_file(
                file_path=fp,
                snapshot_date=d_str,
                snapshot_time=snap_time,
                selection_tier=tier,
                db_path=db_path,
            )
            ingested_results.append(res)
        except Exception as exc:
            logger.error(f"[WMS_BARCODE_DAILY] Failed on {d_str} ({fp.name}): {exc}")
            errors.append({"snapshot_date": d_str, "file": fp.name, "error": str(exc)})

    return {
        "available_dates": avail_dates,
        "ingested": ingested_results,
        "skipped_existing": skipped_dates,
        "errors": errors,
    }


# ==============================================================================
# 2. SAP_STOCK_DAILY (MB52)
# ==============================================================================

def init_sap_daily_db(db_path: Path = DEFAULT_SAP_DAILY_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS SAP_STOCK_DAILY (
            snapshot_date    TEXT NOT NULL,
            snapshot_time    TEXT NOT NULL,
            source_file      TEXT NOT NULL,
            plant            TEXT NOT NULL,
            mat_no           TEXT NOT NULL,
            sloc             TEXT NOT NULL,
            mat_type         TEXT,
            uom              TEXT,
            unrestricted_qty REAL NOT NULL DEFAULT 0.0,
            qual_insp_qty    REAL NOT NULL DEFAULT 0.0,
            blocked_qty      REAL NOT NULL DEFAULT 0.0,
            total_value      REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (snapshot_date, plant, mat_no, sloc)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS SAP_STOCK_DAILY_RUNS (
            snapshot_date   TEXT PRIMARY KEY,
            snapshot_time   TEXT NOT NULL,
            paired_wms_time TEXT NOT NULL,
            source_file     TEXT NOT NULL,
            row_count       INTEGER NOT NULL,
            ingested_at     TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def _read_mb52_dataframe(fp: Path) -> pd.DataFrame:
    """Reads SAP MB52 export (UTF-16 TSV `.XLS`, UTF-8 TSV/CSV, or `.xlsx`)."""
    ext = fp.suffix.lower()
    if ext == ".xlsx":
        df = pd.read_excel(fp, dtype=str)
    else:
        last_err: Optional[Exception] = None
        df = None
        for enc in ("utf-16", "utf-8-sig", "cp1252"):
            try:
                cand = pd.read_csv(fp, sep="\t", encoding=enc, skiprows=1, dtype=str)
                if "Material Number" in [str(c).strip() for c in cand.columns]:
                    df = cand
                    break
                cand0 = pd.read_csv(fp, sep="\t", encoding=enc, dtype=str)
                if "Material Number" in [str(c).strip() for c in cand0.columns]:
                    df = cand0
                    break
            except Exception as e:
                last_err = e
        if df is None:
            raise RuntimeError(f"Unable to parse MB52 file {fp.name}: {last_err}")

    # Deduplicate stripped column names (e.g. 'Total Value' vs 'Total Value ')
    clean_cols: List[str] = []
    seen_cnt: Dict[str, int] = {}
    for raw_c in df.columns:
        c = str(raw_c).strip()
        idx = seen_cnt.get(c, 0)
        seen_cnt[c] = idx + 1
        clean_cols.append(c if idx == 0 else f"{c}__{idx}")
    df.columns = clean_cols
    return df


def discover_available_mb52_dates(mb52_dirs: Iterable[Path] = DEFAULT_MB52_DIRS) -> List[str]:
    dates = set()
    for d in mb52_dirs:
        p = Path(d)
        if not p.exists():
            continue
        for fp in p.iterdir():
            if not fp.is_file() or fp.stat().st_size < 256:
                continue
            if fp.suffix.lower() not in (".xls", ".xlsx", ".csv", ".txt"):
                continue
            if not fp.name.upper().startswith("MB52"):
                continue
            parsed = parse_file_date_and_time(fp)
            if parsed:
                dates.add(parsed[0])
    return sorted(dates)


def select_mb52_daily_candidate(
    target_date: str,
    target_hms: str = "06:51:00",
    mb52_dirs: Iterable[Path] = DEFAULT_MB52_DIRS,
) -> Optional[Tuple[Path, str]]:
    """
    Selects `(file_path, snapshot_time)` for `target_date` whose timestamp is closest to `target_hms`
    (typically `WMS_BARCODE_DAILY.snapshot_time`, default `06:51:00`).
    """
    target_sec = _seconds_of_day(target_hms)
    cands: List[Tuple[int, str, Path]] = []
    for d in mb52_dirs:
        p = Path(d)
        if not p.exists():
            continue
        for fp in p.iterdir():
            if not fp.is_file() or fp.stat().st_size < 256:
                continue
            if fp.suffix.lower() not in (".xls", ".xlsx", ".csv", ".txt"):
                continue
            if not fp.name.upper().startswith("MB52"):
                continue
            parsed = parse_file_date_and_time(fp)
            if not parsed or parsed[0] != target_date:
                continue
            t_str = parsed[1]
            cands.append((abs(_seconds_of_day(t_str) - target_sec), t_str, fp))

    if not cands:
        return None
    cands.sort(key=lambda x: (x[0], x[1], x[2].name))
    return cands[0][2], cands[0][1]


def ingest_sap_mb52_daily_file(
    file_path: Path,
    snapshot_date: str,
    snapshot_time: str,
    paired_wms_time: str = "06:51:00",
    db_path: Path = DEFAULT_SAP_DAILY_DB,
) -> Dict[str, Any]:
    """
    Idempotently replaces `snapshot_date` in `SAP_STOCK_DAILY` with `Material x SLoc` rows from `file_path`.
    Filters out UoM subtotal/footer rows where `Material Number` or `Plnt` is blank.
    """
    _lower_priority()
    df = _read_mb52_dataframe(file_path)
    cols = set(df.columns)

    n_len = len(df)
    plnt_vals = df["Plnt"].tolist() if "Plnt" in cols else [""] * n_len
    mat_vals = df["Material Number"].tolist() if "Material Number" in cols else [""] * n_len
    sloc_vals = df["SLoc"].tolist() if "SLoc" in cols else [""] * n_len
    mtyp_vals = df["MTyp"].tolist() if "MTyp" in cols else [""] * n_len
    bun_vals = df["BUn"].tolist() if "BUn" in cols else [""] * n_len
    unr_vals = df["Unrestricted"].tolist() if "Unrestricted" in cols else [0.0] * n_len
    qi_vals = df["Qual.Insp."].tolist() if "Qual.Insp." in cols else [0.0] * n_len
    blk_vals = df["Blocked"].tolist() if "Blocked" in cols else [0.0] * n_len
    val_vals = df["Total Value"].tolist() if "Total Value" in cols else [0.0] * n_len

    src_name = file_path.name
    agg: Dict[Tuple[str, str, str], Tuple[Any, ...]] = {}

    for pl_r, mat_r, sl_r, mt_r, uom_r, unr_r, qi_r, blk_r, val_r in zip(
        plnt_vals, mat_vals, sloc_vals, mtyp_vals, bun_vals, unr_vals, qi_vals, blk_vals, val_vals
    ):
        pl = "" if pl_r is None else str(pl_r).strip().upper()
        mat = "" if mat_r is None else str(mat_r).strip()
        if not pl or pl == "NAN" or not mat or mat.lower() == "nan":
            continue
        sl = "" if sl_r is None else str(sl_r).strip().upper()
        if sl == "NAN":
            sl = ""
        mt = "" if mt_r is None or str(mt_r).strip().lower() == "nan" else str(mt_r).strip().upper()
        uom = "" if uom_r is None or str(uom_r).strip().lower() == "nan" else str(uom_r).strip().upper()
        unr = _parse_sap_float(unr_r)
        qi = _parse_sap_float(qi_r)
        blk = _parse_sap_float(blk_r)
        tot_v = _parse_sap_float(val_r)

        pk = (pl, mat, sl)
        if pk in agg:
            prev = agg[pk]
            agg[pk] = (
                snapshot_date,
                snapshot_time,
                src_name,
                pl,
                mat,
                sl,
                mt or prev[6],
                uom or prev[7],
                round(prev[8] + unr, 4),
                round(prev[9] + qi, 4),
                round(prev[10] + blk, 4),
                round(prev[11] + tot_v, 2),
            )
        else:
            agg[pk] = (
                snapshot_date,
                snapshot_time,
                src_name,
                pl,
                mat,
                sl,
                mt,
                uom,
                round(unr, 4),
                round(qi, 4),
                round(blk, 4),
                round(tot_v, 2),
            )

    del df
    gc.collect()

    rows_to_insert = list(agg.values())
    now_s = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = init_sap_daily_db(db_path)
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM SAP_STOCK_DAILY WHERE snapshot_date = ?", (snapshot_date,))
        if rows_to_insert:
            cur.executemany(
                """
                INSERT INTO SAP_STOCK_DAILY (
                    snapshot_date, snapshot_time, source_file, plant, mat_no, sloc,
                    mat_type, uom, unrestricted_qty, qual_insp_qty, blocked_qty, total_value
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
        cur.execute(
            """
            INSERT INTO SAP_STOCK_DAILY_RUNS (
                snapshot_date, snapshot_time, paired_wms_time, source_file, row_count, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                snapshot_time = excluded.snapshot_time,
                paired_wms_time = excluded.paired_wms_time,
                source_file = excluded.source_file,
                row_count = excluded.row_count,
                ingested_at = excluded.ingested_at
            """,
            (snapshot_date, snapshot_time, paired_wms_time, src_name, len(rows_to_insert), now_s),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "snapshot_date": snapshot_date,
        "snapshot_time": snapshot_time,
        "paired_wms_time": paired_wms_time,
        "source_file": src_name,
        "row_count": len(rows_to_insert),
    }


def sync_all_sap_stock_daily(
    mb52_dirs: Iterable[Path] = DEFAULT_MB52_DIRS,
    sap_db_path: Path = DEFAULT_SAP_DAILY_DB,
    wms_db_path: Path = DEFAULT_WMS_DAILY_DB,
    force_dates: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """
    Ensures every MB52 date available on disk is ingested into `SAP_STOCK_DAILY`,
    paired to the same day's `WMS_BARCODE_DAILY` timestamp (or `06:51:00` default).
    """
    forced_set = set(force_dates or [])
    wms_times: Dict[str, str] = {}
    if Path(wms_db_path).exists():
        try:
            c_w = sqlite3.connect(f"file:{Path(wms_db_path).resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                wms_times = {
                    r[0]: r[1]
                    for r in c_w.execute("SELECT snapshot_date, snapshot_time FROM WMS_BARCODE_DAILY_RUNS").fetchall()
                }
            finally:
                c_w.close()
        except Exception:
            pass

    conn = init_sap_daily_db(sap_db_path)
    try:
        existing = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                "SELECT snapshot_date, snapshot_time, paired_wms_time, source_file, row_count FROM SAP_STOCK_DAILY_RUNS"
            ).fetchall()
        }
    finally:
        conn.close()

    avail_dates = discover_available_mb52_dates(mb52_dirs=mb52_dirs)
    ingested_results = []
    skipped_dates = []
    errors = []

    for d_str in avail_dates:
        target_wms_t = wms_times.get(d_str, "06:51:00")
        cand = select_mb52_daily_candidate(d_str, target_hms=target_wms_t, mb52_dirs=mb52_dirs)
        if not cand:
            continue
        fp, mb_time = cand
        prev = existing.get(d_str)
        need_ingest = (
            d_str in forced_set
            or prev is None
            or int(prev[3] or 0) == 0
            or prev[0] != mb_time
        )
        if not need_ingest:
            skipped_dates.append(d_str)
            continue
        try:
            res = ingest_sap_mb52_daily_file(
                file_path=fp,
                snapshot_date=d_str,
                snapshot_time=mb_time,
                paired_wms_time=target_wms_t,
                db_path=sap_db_path,
            )
            ingested_results.append(res)
        except Exception as exc:
            logger.error(f"[SAP_STOCK_DAILY] Failed on {d_str} ({fp.name}): {exc}")
            errors.append({"snapshot_date": d_str, "file": fp.name, "error": str(exc)})

    return {
        "available_dates": avail_dates,
        "ingested": ingested_results,
        "skipped_existing": skipped_dates,
        "errors": errors,
    }


def sync_daily_snapshots(
    force_dates: Optional[Iterable[str]] = None,
    wms_daily_dir: Path = DEFAULT_WMS_DAILY_STACK,
    wms_parts_dir: Path = DEFAULT_WMS_PARTS_STACK,
    mb52_dirs: Iterable[Path] = DEFAULT_MB52_DIRS,
    wms_db_path: Path = DEFAULT_WMS_DAILY_DB,
    sap_db_path: Path = DEFAULT_SAP_DAILY_DB,
) -> Dict[str, Any]:
    """
    Runs WMS daily barcode sync first, then pairs SAP MB52 daily sync to WMS timestamps.
    """
    wms_res = sync_all_wms_barcode_daily(
        wms_daily_dir=wms_daily_dir,
        wms_parts_dir=wms_parts_dir,
        db_path=wms_db_path,
        force_dates=force_dates,
    )
    sap_res = sync_all_sap_stock_daily(
        mb52_dirs=mb52_dirs,
        sap_db_path=sap_db_path,
        wms_db_path=wms_db_path,
        force_dates=force_dates,
    )
    return {"wms": wms_res, "sap": sap_res}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Sync permanent daily WMS Barcode & SAP MB52 SQLite ledgers.")
    parser.add_argument("--force-date", action="append", default=None, help="Force re-ingest YYYY-MM-DD")
    args = parser.parse_args()

    out = sync_daily_snapshots(force_dates=args.force_date)
    print(json.dumps(out, indent=2, ensure_ascii=False))

