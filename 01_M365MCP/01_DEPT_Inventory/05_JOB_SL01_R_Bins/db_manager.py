"""
SL01 R-Bin Movement Lag Monitor - Database Manager
==================================================
01_Inventory_Team / 05_JOB_SL01_R_Bins
Database: db/sl01_r_bins.db

Tracks pallets/boxes in SL01 (Main parts) stored in Rack (R) bins.
Maintains true observation history across daily snapshots to detect
inventory lingering >= 2 business days in transfer state (D98).
"""

import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

logger = logging.getLogger("SL01R_DB")

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DB_PATH = DB_DIR / "sl01_r_bins.db"

DB_DIR.mkdir(parents=True, exist_ok=True)


def calculate_business_days(start_date_str: str, end_date_str: str) -> int:
    """
    Calculates elapsed business days between start_date and end_date (D87-2).
    Counts all days from (start_date + 1 day) up to end_date EXCLUDING Sundays (weekday == 6).
    Saturday is counted (weekday == 5) as physical warehouse restocking occurs on Saturdays.

    Examples:
      Sat -> Mon = 1 day (Sun excluded)
      Fri -> Sat = 1 day
      Mon -> Tue = 1 day
      Sat -> Tue = 2 days (Sun excluded, Mon + Tue counted)
    """
    try:
        start_dt = datetime.strptime(str(start_date_str)[:10], "%Y-%m-%d").date()
        end_dt = datetime.strptime(str(end_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return 0

    if end_dt <= start_dt:
        return 0

    business_days = 0
    cur_date = start_dt + timedelta(days=1)
    while cur_date <= end_dt:
        if cur_date.weekday() != 6:  # 6 is Sunday
            business_days += 1
        cur_date += timedelta(days=1)

    return business_days


def get_db_connection(db_path: Optional[Path] = None, timeout: float = 15.0) -> sqlite3.Connection:
    target = Path(db_path or DB_PATH)
    conn = sqlite3.connect(str(target), timeout=timeout)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[Path] = None):
    """Initializes tables for SL01 R-bin observation tracking and scan runs with D87/D88/D98 columns."""
    target = Path(db_path or DB_PATH)
    with get_db_connection(target) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS SL01_R_BIN_OBSERVATION (
                barcode TEXT PRIMARY KEY,
                mat_no TEXT NOT NULL,
                mat_desc TEXT,
                bin TEXT NOT NULL,
                gi_loc TEXT NOT NULL,
                qty REAL NOT NULL,
                lot_no TEXT,
                vendor_code TEXT,
                vendor_name TEXT,
                first_seen_date TEXT NOT NULL,
                last_seen_date TEXT NOT NULL,
                cleared_date TEXT,
                days_in_sl01_r INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE or CLEARED
                episode INTEGER NOT NULL DEFAULT 1,
                prev_first_seen_date TEXT,
                prev_cleared_date TEXT,
                sl02_signal TEXT DEFAULT 'EMPTY_OR_NO_SL02',
                sl02_details TEXT DEFAULT '',
                sl02_other_bin TEXT DEFAULT '',
                cross_bin_persisted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS SL01_R_BIN_OBSERVATION_HISTORY (
                history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                barcode TEXT NOT NULL,
                episode INTEGER NOT NULL,
                mat_no TEXT NOT NULL,
                bin TEXT NOT NULL,
                first_seen_date TEXT NOT NULL,
                last_seen_date TEXT NOT NULL,
                cleared_date TEXT,
                days_in_sl01_r INTEGER NOT NULL,
                qty REAL NOT NULL,
                cleared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS SL01_R_BIN_CROSS_PAIR (
                pair_id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_date TEXT NOT NULL,
                barcode TEXT NOT NULL,
                mat_no TEXT NOT NULL,
                qty REAL NOT NULL,
                lot_no TEXT DEFAULT '',
                sl01_bin TEXT NOT NULL,
                sl02_bin TEXT NOT NULL,
                is_strict_lot_single INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS SL01_R_BIN_SCAN_RUN (
                run_id TEXT PRIMARY KEY,
                scan_date TEXT NOT NULL,
                source_file TEXT NOT NULL,
                total_active_r_bins INTEGER NOT NULL,
                stale_r_bins_count INTEGER NOT NULL,
                stale_total_qty REAL NOT NULL,
                report_file TEXT,
                email_status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # Migration guard: Add columns if table existed without D87/D88/D98 schema
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(SL01_R_BIN_OBSERVATION);")
        existing_cols = {row["name"] for row in cursor.fetchall()}

        migration_cols = [
            ("episode", "INTEGER NOT NULL DEFAULT 1"),
            ("prev_first_seen_date", "TEXT"),
            ("prev_cleared_date", "TEXT"),
            ("cleared_date", "TEXT"),
            ("sl02_signal", "TEXT DEFAULT 'EMPTY_OR_NO_SL02'"),
            ("sl02_details", "TEXT DEFAULT ''"),
            ("sl02_other_bin", "TEXT DEFAULT ''"),
            ("cross_bin_persisted", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in migration_cols:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE SL01_R_BIN_OBSERVATION ADD COLUMN {col_name} {col_def};")

        cursor.execute("PRAGMA table_info(SL01_R_BIN_CROSS_PAIR);")
        existing_pair_cols = {row["name"] for row in cursor.fetchall()}
        pair_migration_cols = [
            ("lot_no", "TEXT DEFAULT ''"),
            ("is_strict_lot_single", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_def in pair_migration_cols:
            if col_name not in existing_pair_cols:
                cursor.execute(f"ALTER TABLE SL01_R_BIN_CROSS_PAIR ADD COLUMN {col_name} {col_def};")

        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_sl01_r_status_days 
                ON SL01_R_BIN_OBSERVATION(status, days_in_sl01_r);
            CREATE INDEX IF NOT EXISTS idx_sl01_r_mat 
                ON SL01_R_BIN_OBSERVATION(mat_no);
            CREATE INDEX IF NOT EXISTS idx_sl01_r_bin 
                ON SL01_R_BIN_OBSERVATION(bin);
            CREATE INDEX IF NOT EXISTS idx_sl01_r_signal 
                ON SL01_R_BIN_OBSERVATION(sl02_signal);
            CREATE INDEX IF NOT EXISTS idx_cross_pair_date_bar 
                ON SL01_R_BIN_CROSS_PAIR(scan_date, barcode);
            CREATE INDEX IF NOT EXISTS idx_cross_pair_match 
                ON SL01_R_BIN_CROSS_PAIR(mat_no, qty, sl01_bin, sl02_bin);
            CREATE INDEX IF NOT EXISTS idx_cross_pair_strict_match 
                ON SL01_R_BIN_CROSS_PAIR(mat_no, qty, lot_no, sl01_bin, sl02_bin, is_strict_lot_single);
        """)

        conn.commit()
    logger.info(f"Database initialized: {target}")


def upsert_observations(
    scan_date_str: str,
    records: List[Dict],
    is_backfill: bool = False,
    db_path: Optional[Path] = None
) -> Tuple[int, int, int]:
    """
    Upserts observations from a snapshot for scan_date_str (YYYY-MM-DD) adhering to D87 & D98.
    - If barcode is new: first_seen_date = scan_date_str, days = 0, episode = 1.
    - If barcode was CLEARED (D87-3): archives old episode to HISTORY, resets first_seen_date = scan_date_str, days = 0, episode = old + 1.
    - If barcode is ACTIVE: recalculates days_in_sl01_r using business days excluding Sunday (D87-2).
    - D98-3: Only records & flags 🟣 cross-bin pairs when same mat_no, same qty, AND same lot_no
      appear in strictly 1 other SL02 R-bin, without deleting any existing SL01_R_BIN_CROSS_PAIR rows.
    - If not is_backfill: barcodes currently ACTIVE but missing from records are marked CLEARED.
    Returns: (new_count, updated_count, cleared_count)
    """
    if not records and is_backfill:
        return (0, 0, 0)

    target_db = Path(db_path or DB_PATH)
    new_count = 0
    updated_count = 0
    cleared_count = 0
    current_barcodes = set()

    with get_db_connection(target_db) as conn:
        cursor = conn.cursor()
        now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for r in records:
            barcode = str(r["barcode"]).strip()
            current_barcodes.add(barcode)
            lot_val = str(r.get("lot_no", "") or "").strip()
            if lot_val.lower() == "nan":
                lot_val = ""

            cursor.execute(
                "SELECT first_seen_date, last_seen_date, cleared_date, status, episode, mat_no, bin, qty FROM SL01_R_BIN_OBSERVATION WHERE barcode = ?",
                (barcode,)
            )
            row = cursor.fetchone()

            # D98-3: Strict cross-bin candidate check (same mat_no, same qty, same lot_no, and strictly 1 other R-bin in SL02)
            cand_bins = r.get("candidate_cross_bins", [])
            if not cand_bins and r.get("candidate_cross_bin"):
                cand_bins = [r["candidate_cross_bin"]]
            cand_bins = sorted(list(set(str(b).strip() for b in cand_bins if str(b).strip() and str(b).strip() != r["bin"])))

            has_persisted_cross_bin = False
            persisted_other_bin = ""

            # Preserve existing SL01_R_BIN_CROSS_PAIR rows (no DELETE); reset strict flag for today's barcode before setting if strict
            cursor.execute("""
                UPDATE SL01_R_BIN_CROSS_PAIR
                SET is_strict_lot_single = 0
                WHERE scan_date = ? AND barcode = ?
            """, (scan_date_str, barcode))

            if len(cand_bins) == 1:
                strict_bin = cand_bins[0]
                cursor.execute("""
                    SELECT sl02_bin FROM SL01_R_BIN_CROSS_PAIR
                    WHERE mat_no = ? AND qty = ? AND COALESCE(lot_no, '') = ?
                      AND sl01_bin = ? AND sl02_bin = ?
                      AND is_strict_lot_single = 1
                      AND scan_date < ?
                    LIMIT 1
                """, (r["mat_no"], float(r["qty"]), lot_val, r["bin"], strict_bin, scan_date_str))
                if cursor.fetchone():
                    has_persisted_cross_bin = True
                    persisted_other_bin = strict_bin

                # Upsert strict pair for today without deleting existing rows
                cursor.execute("""
                    SELECT pair_id FROM SL01_R_BIN_CROSS_PAIR
                    WHERE scan_date = ? AND barcode = ? AND sl01_bin = ? AND sl02_bin = ?
                    LIMIT 1
                """, (scan_date_str, barcode, r["bin"], strict_bin))
                existing_pair = cursor.fetchone()
                if existing_pair:
                    cursor.execute("""
                        UPDATE SL01_R_BIN_CROSS_PAIR
                        SET mat_no = ?, qty = ?, lot_no = ?, is_strict_lot_single = 1
                        WHERE pair_id = ?
                    """, (r["mat_no"], float(r["qty"]), lot_val, existing_pair["pair_id"]))
                else:
                    cursor.execute("""
                        INSERT INTO SL01_R_BIN_CROSS_PAIR (
                            scan_date, barcode, mat_no, qty, lot_no, sl01_bin, sl02_bin, is_strict_lot_single
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                    """, (scan_date_str, barcode, r["mat_no"], float(r["qty"]), lot_val, r["bin"], strict_bin))

            # D88-2 / D98-2 Signal evaluation (reference icons)
            is_refilled = r.get("is_refilled", False) or ("REFILLED_OTHER_MATERIAL" in r.get("sl02_signal", ""))
            is_dup = has_persisted_cross_bin or ("DUP_ELSEWHERE" in r.get("sl02_signal", "")) or ("REFILLED_AND_DUP" in r.get("sl02_signal", ""))
            is_same = r.get("is_same_remains", False) or ("SAME_MATERIAL_REMAINS" in r.get("sl02_signal", ""))

            if is_refilled and is_dup:
                sig = "REFILLED_AND_DUP"
            elif is_refilled:
                sig = "REFILLED_OTHER_MATERIAL"
            elif is_dup:
                sig = "DUP_ELSEWHERE"
            elif is_same:
                sig = "SAME_MATERIAL_REMAINS"
            else:
                sig = r.get("sl02_signal", "EMPTY_OR_NO_SL02")

            other_bin_val = persisted_other_bin if is_dup else r.get("sl02_other_bin", "")
            cross_persisted_val = 1 if is_dup else 0
            det = r.get("sl02_details", "")

            if row:
                old_status = row["status"]
                old_first_seen = row["first_seen_date"]
                old_last_seen = row["last_seen_date"]
                old_cleared = row["cleared_date"] or old_last_seen
                curr_episode = row["episode"] or 1

                if old_status == "CLEARED":
                    # D87-3 / #237: Archive prior CLEARED episode's own mat_no, bin, and qty before resetting
                    old_mat = row["mat_no"] or r["mat_no"]
                    old_bin = row["bin"] or r["bin"]
                    old_qty = float(row["qty"]) if row["qty"] is not None else float(r["qty"])
                    cursor.execute("""
                        INSERT INTO SL01_R_BIN_OBSERVATION_HISTORY (
                            barcode, episode, mat_no, bin, first_seen_date, last_seen_date, cleared_date, days_in_sl01_r, qty, cleared_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        barcode, curr_episode, old_mat, old_bin, old_first_seen, old_last_seen, old_cleared,
                        calculate_business_days(old_first_seen, old_cleared), old_qty, now_ts
                    ))

                    new_episode = curr_episode + 1
                    cursor.execute("""
                        UPDATE SL01_R_BIN_OBSERVATION
                        SET mat_no = ?, mat_desc = ?, bin = ?, gi_loc = ?, qty = ?,
                            lot_no = ?, vendor_code = ?, vendor_name = ?,
                            first_seen_date = ?, last_seen_date = ?, cleared_date = NULL, days_in_sl01_r = 0,
                            status = 'ACTIVE', episode = ?, prev_first_seen_date = ?,
                            prev_cleared_date = ?, sl02_signal = ?, sl02_details = ?,
                            sl02_other_bin = ?, cross_bin_persisted = ?, updated_at = ?
                        WHERE barcode = ?
                    """, (
                        r["mat_no"], r.get("mat_desc", ""), r["bin"], r["gi_loc"], float(r["qty"]),
                        lot_val, r.get("vendor_code", ""), r.get("vendor_name", ""),
                        scan_date_str, scan_date_str, new_episode, old_first_seen,
                        old_cleared, sig, det, other_bin_val, cross_persisted_val, now_ts, barcode
                    ))
                    updated_count += 1
                else:
                    # Ongoing active observation: recalculate lag via business days (D87-2)
                    days_lag = calculate_business_days(old_first_seen, scan_date_str)
                    cursor.execute("""
                        UPDATE SL01_R_BIN_OBSERVATION
                        SET mat_no = ?, mat_desc = ?, bin = ?, gi_loc = ?, qty = ?,
                            lot_no = ?, vendor_code = ?, vendor_name = ?,
                            last_seen_date = ?, days_in_sl01_r = ?, status = 'ACTIVE',
                            sl02_signal = ?, sl02_details = ?, sl02_other_bin = ?,
                            cross_bin_persisted = ?, updated_at = ?
                        WHERE barcode = ?
                    """, (
                        r["mat_no"], r.get("mat_desc", ""), r["bin"], r["gi_loc"], float(r["qty"]),
                        lot_val, r.get("vendor_code", ""), r.get("vendor_name", ""),
                        scan_date_str, days_lag, sig, det, other_bin_val, cross_persisted_val, now_ts, barcode
                    ))
                    updated_count += 1
            else:
                cursor.execute("""
                    INSERT INTO SL01_R_BIN_OBSERVATION (
                        barcode, mat_no, mat_desc, bin, gi_loc, qty,
                        lot_no, vendor_code, vendor_name,
                        first_seen_date, last_seen_date, days_in_sl01_r,
                        status, episode, sl02_signal, sl02_details,
                        sl02_other_bin, cross_bin_persisted,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'ACTIVE', 1, ?, ?, ?, ?, ?, ?)
                """, (
                    barcode, r["mat_no"], r.get("mat_desc", ""), r["bin"], r["gi_loc"], float(r["qty"]),
                    lot_val, r.get("vendor_code", ""), r.get("vendor_name", ""),
                    scan_date_str, scan_date_str, sig, det, other_bin_val, cross_persisted_val, now_ts, now_ts
                ))
                new_count += 1

        # Mark missing active items as CLEARED only on forward daily scans (not during multi-snapshot backfill)
        if not is_backfill:
            cursor.execute(
                "SELECT barcode FROM SL01_R_BIN_OBSERVATION WHERE status = 'ACTIVE'"
            )
            all_active = [row[0] for row in cursor.fetchall()]
            missing = set(all_active) - current_barcodes
            for b in missing:
                cursor.execute("""
                    UPDATE SL01_R_BIN_OBSERVATION
                    SET status = 'CLEARED', cleared_date = ?, updated_at = ?
                    WHERE barcode = ?
                """, (scan_date_str, now_ts, b))
                cleared_count += 1

        conn.commit()

    logger.info(
        f"Upsert for {scan_date_str}: {new_count} new, {updated_count} updated, {cleared_count} cleared."
    )
    return (new_count, updated_count, cleared_count)


def get_active_lagging_observations(
    min_days: Optional[int] = None,
    db_path: Optional[Path] = None,
    apply_tiered_threshold: bool = False
) -> List[Dict]:
    """
    Returns active SL01 R-bin records adhering strictly to D98-1 and D98-2:
    - D98-1 Single dwell-day criterion: Regardless of signal, all active SL01 R-bin items
      with business days (excluding Sunday) >= 2 days (or custom min_days if explicitly provided).
      The D88-1 exception for 🔴/🟣 >= 1 day is abolished.
    - D98-2 Sorting: Dwell days DESC (days_in_sl01_r DESC) -> Qty DESC (qty DESC) -> Part ASC (mat_no ASC).
      Icons (🔴🟣🟠) are kept as reference indicators.
    """
    target_db = Path(db_path or DB_PATH)
    threshold = min_days if min_days is not None else 2
    with get_db_connection(target_db) as conn:
        cursor = conn.cursor()
        query = """
            SELECT barcode, mat_no, mat_desc, bin, gi_loc, qty,
                   lot_no, vendor_code, vendor_name,
                   first_seen_date, last_seen_date, days_in_sl01_r, status,
                   episode, prev_first_seen_date, prev_cleared_date, cleared_date,
                   sl02_signal, sl02_details, sl02_other_bin, cross_bin_persisted
            FROM SL01_R_BIN_OBSERVATION
            WHERE status = 'ACTIVE' AND days_in_sl01_r >= ?
            ORDER BY 
                days_in_sl01_r DESC, 
                qty DESC, 
                mat_no ASC,
                bin ASC
        """
        cursor.execute(query, (threshold,))
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def get_all_active_observations(db_path: Optional[Path] = None) -> List[Dict]:
    """Returns all currently ACTIVE SL01 R-bin records with D98-2 sorting (days_in_sl01_r DESC, qty DESC, mat_no ASC)."""
    target_db = Path(db_path or DB_PATH)
    with get_db_connection(target_db) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT barcode, mat_no, mat_desc, bin, gi_loc, qty,
                   lot_no, vendor_code, vendor_name,
                   first_seen_date, last_seen_date, days_in_sl01_r, status,
                   episode, prev_first_seen_date, prev_cleared_date, cleared_date,
                   sl02_signal, sl02_details, sl02_other_bin, cross_bin_persisted
            FROM SL01_R_BIN_OBSERVATION
            WHERE status = 'ACTIVE'
            ORDER BY 
                days_in_sl01_r DESC, 
                qty DESC, 
                mat_no ASC,
                bin ASC
        """)
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def get_active_count_delta(
    scan_date_str: str,
    drop_pct_threshold: float = 50.0,
    min_prev_active: int = 5,
    db_path: Optional[Path] = None,
) -> Dict:
    """
    Computes day-over-day active SL01 R-bin pallet count change for non-blocking sharp-drop banner (#175).
    Uses latest prior SL01_R_BIN_SCAN_RUN when available, otherwise reconstructs prior active count
    from SL01_R_BIN_OBSERVATION ((ACTIVE with first_seen_date < scan_date) + (CLEARED on scan_date)).
    """
    target_db = Path(db_path or DB_PATH)
    with get_db_connection(target_db) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM SL01_R_BIN_OBSERVATION WHERE status = 'ACTIVE'")
        curr_active = int(cur.fetchone()[0] or 0)

        cur.execute(
            "SELECT COUNT(*) FROM SL01_R_BIN_OBSERVATION WHERE status = 'CLEARED' AND cleared_date = ?",
            (scan_date_str,),
        )
        cleared_today = int(cur.fetchone()[0] or 0)

        cur.execute(
            """
            SELECT scan_date, total_active_r_bins
            FROM SL01_R_BIN_SCAN_RUN
            WHERE scan_date < ?
            ORDER BY scan_date DESC, created_at DESC
            LIMIT 1
            """,
            (scan_date_str,),
        )
        prev_run = cur.fetchone()
        if prev_run is not None:
            prev_active = int(prev_run["total_active_r_bins"] or 0)
            prev_scan_date = str(prev_run["scan_date"] or "")
        else:
            cur.execute(
                """
                SELECT COUNT(*) FROM SL01_R_BIN_OBSERVATION
                WHERE (status = 'ACTIVE' AND first_seen_date < ?)
                   OR (status = 'CLEARED' AND cleared_date = ?)
                """,
                (scan_date_str, scan_date_str),
            )
            prev_active = int(cur.fetchone()[0] or 0)
            prev_scan_date = ""

    if prev_active > 0 and curr_active < prev_active:
        drop_pct = round(((prev_active - curr_active) / prev_active) * 100.0, 1)
    else:
        drop_pct = 0.0

    is_sharp_drop = bool(prev_active >= min_prev_active and drop_pct >= drop_pct_threshold)
    return {
        "scan_date": scan_date_str,
        "prev_scan_date": prev_scan_date,
        "prev_active_count": prev_active,
        "curr_active_count": curr_active,
        "cleared_today_count": cleared_today,
        "drop_pct": drop_pct,
        "is_sharp_drop": is_sharp_drop,
    }


def log_scan_run(
    run_id: str,
    scan_date: str,
    source_file: str,
    total_active: int,
    stale_count: int,
    stale_qty: float,
    report_file: Optional[str] = None,
    email_status: str = "PENDING",
    db_path: Optional[Path] = None
):
    """Logs a scan run record."""
    target_db = Path(db_path or DB_PATH)
    with get_db_connection(target_db) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO SL01_R_BIN_SCAN_RUN (
                run_id, scan_date, source_file, total_active_r_bins,
                stale_r_bins_count, stale_total_qty, report_file, email_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, scan_date, source_file, total_active,
            stale_count, stale_qty, report_file, email_status
        ))
        conn.commit()
