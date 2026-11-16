"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - SQLite Ledger Manager (#351)
========================================================================
Owns `02_JOB_Delivery_Check/db/delivery_check.db` (WAL mode, single writer).
Named `delivery_db_manager` so `sys.modules["db_manager"]` never collides with
`00_JOB_StockAccuracy/db_manager.py` when `historical_backfill_crawler` is imported
in the same Python process (`db_manager.py` re-exports all symbols for convenience).
All ledgers outside `02_JOB_Delivery_Check` are strictly read-only (`?mode=ro`).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DEFAULT_DB_PATH = DB_DIR / "delivery_check.db"


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = Path(db_path) if db_path else DEFAULT_DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
    except Exception:
        pass
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Strictly read-only URI connection (`?mode=ro`) for cross-department ledgers (AGENTS.md Rule 4)."""
    uri = f"file:{Path(db_path).resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_database(db_path: Optional[Path] = None) -> Path:
    target = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = get_connection(target)
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS plan_horizon_parts (
                work_date TEXT NOT NULL,
                part_key TEXT NOT NULL,
                part_no TEXT NOT NULL,
                part_name TEXT NOT NULL,
                vendor_display TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                cls TEXT NOT NULL,
                box_qty REAL NOT NULL DEFAULT 0.0,
                start_sap REAL NOT NULL DEFAULT 0.0,
                start_wms REAL NOT NULL DEFAULT 0.0,
                start_in_zm212 INTEGER NOT NULL DEFAULT 1,
                load_json TEXT NOT NULL,
                req_json TEXT NOT NULL,
                srm_json TEXT NOT NULL,
                extra_json TEXT NOT NULL,
                add_json TEXT NOT NULL,
                plan_source TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (work_date, part_key)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS truck_runs (
                truck_id TEXT PRIMARY KEY,
                work_date TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                vendor_display TEXT NOT NULL,
                truck_label TEXT NOT NULL,
                slot_idx INTEGER NOT NULL,
                is_pallet_truck INTEGER NOT NULL DEFAULT 0,
                departure_ts TEXT NOT NULL,
                first_poll_due_ts TEXT NOT NULL,
                early_warn_due_ts TEXT NOT NULL,
                no_arrival_deadline_ts TEXT NOT NULL,
                status TEXT NOT NULL,
                planned_parts_count INTEGER NOT NULL DEFAULT 0,
                planned_boxes_total REAL NOT NULL DEFAULT 0.0,
                planned_pcs_total REAL NOT NULL DEFAULT 0.0,
                received_boxes_total INTEGER NOT NULL DEFAULT 0,
                received_pcs_total REAL NOT NULL DEFAULT 0.0,
                matched_barcodes_count INTEGER NOT NULL DEFAULT 0,
                quiet_polls INTEGER NOT NULL DEFAULT 0,
                first_receipt_ts TEXT,
                last_receipt_ts TEXT,
                last_quiet_check_ts TEXT,
                last_polled_ts TEXT,
                closed_ts TEXT,
                next_poll_due_ts TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tr_date_sup ON truck_runs(work_date, supplier_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tr_status ON truck_runs(work_date, status)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS truck_plan_lines (
                line_id TEXT PRIMARY KEY,
                truck_id TEXT NOT NULL,
                work_date TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                vendor_display TEXT NOT NULL,
                truck_label TEXT NOT NULL,
                slot_idx INTEGER NOT NULL,
                part_key TEXT NOT NULL,
                part_no TEXT NOT NULL,
                part_name TEXT NOT NULL,
                cls TEXT NOT NULL,
                box_qty REAL NOT NULL DEFAULT 0.0,
                srm_pcs REAL NOT NULL DEFAULT 0.0,
                extra_pcs REAL NOT NULL DEFAULT 0.0,
                planned_pcs REAL NOT NULL DEFAULT 0.0,
                planned_boxes REAL NOT NULL DEFAULT 0.0,
                frozen_at TEXT NOT NULL,
                UNIQUE (truck_id, part_key)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tpl_date_part ON truck_plan_lines(work_date, part_key)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS wms_gr_barcodes (
                barcode TEXT PRIMARY KEY,
                work_date TEXT NOT NULL,
                mat_no TEXT NOT NULL,
                part_key TEXT NOT NULL,
                mat_desc TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                gr_loc TEXT NOT NULL,
                gr_time TEXT NOT NULL,
                slot_idx INTEGER NOT NULL,
                gr_qty REAL NOT NULL,
                unit TEXT NOT NULL,
                move_type TEXT NOT NULL,
                trans_no TEXT NOT NULL,
                invoice_no TEXT NOT NULL,
                assigned_truck_id TEXT,
                is_unplanned INTEGER NOT NULL DEFAULT 0,
                source_file TEXT NOT NULL,
                first_seen_poll_ts TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wgb_date_part ON wms_gr_barcodes(work_date, part_key)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wgb_truck ON wms_gr_barcodes(assigned_truck_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS delivery_verdicts (
                verdict_id TEXT PRIMARY KEY,
                truck_id TEXT NOT NULL,
                work_date TEXT NOT NULL,
                supplier_key TEXT NOT NULL,
                truck_label TEXT NOT NULL,
                part_key TEXT NOT NULL,
                part_no TEXT NOT NULL,
                part_name TEXT NOT NULL,
                box_qty REAL NOT NULL DEFAULT 0.0,
                planned_boxes REAL NOT NULL DEFAULT 0.0,
                planned_pcs REAL NOT NULL DEFAULT 0.0,
                received_boxes INTEGER NOT NULL DEFAULT 0,
                received_pcs REAL NOT NULL DEFAULT 0.0,
                diff_pcs REAL NOT NULL DEFAULT 0.0,
                diff_boxes REAL NOT NULL DEFAULT 0.0,
                matched_barcodes_count INTEGER NOT NULL DEFAULT 0,
                first_gr_time TEXT,
                last_gr_time TEXT,
                verdict TEXT NOT NULL,
                shortage_receipt_basis TEXT NOT NULL,
                runout_received_only TEXT NOT NULL,
                line_shortage_alarm INTEGER NOT NULL DEFAULT 0,
                initial_alarm_at_close INTEGER NOT NULL DEFAULT 0,
                first_short_slot_label TEXT,
                deficit_before_next_truck_pcs REAL NOT NULL DEFAULT 0.0,
                buffer_low_15pct INTEGER NOT NULL DEFAULT 0,
                recovered_later_pcs REAL NOT NULL DEFAULT 0.0,
                open_shortfall_pcs REAL NOT NULL DEFAULT 0.0,
                recovery_note TEXT NOT NULL DEFAULT '',
                start_in_zm212 INTEGER NOT NULL DEFAULT 1,
                evaluated_at TEXT NOT NULL,
                UNIQUE (truck_id, part_key)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dv_date_truck ON delivery_verdicts(work_date, truck_id)")

        existing_php_cols = {
            r["name"] for r in cur.execute("PRAGMA table_info(plan_horizon_parts)").fetchall()
        }
        if "start_in_zm212" not in existing_php_cols:
            cur.execute("ALTER TABLE plan_horizon_parts ADD COLUMN start_in_zm212 INTEGER NOT NULL DEFAULT 1")

        existing_dv_cols = {
            r["name"] for r in cur.execute("PRAGMA table_info(delivery_verdicts)").fetchall()
        }
        for col_name, col_ddl in (
            ("initial_alarm_at_close", "INTEGER NOT NULL DEFAULT 0"),
            ("recovered_later_pcs", "REAL NOT NULL DEFAULT 0.0"),
            ("open_shortfall_pcs", "REAL NOT NULL DEFAULT 0.0"),
            ("recovery_note", "TEXT NOT NULL DEFAULT ''"),
            ("start_in_zm212", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if col_name not in existing_dv_cols:
                cur.execute(f"ALTER TABLE delivery_verdicts ADD COLUMN {col_name} {col_ddl}")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatch_notices (
                notice_key TEXT PRIMARY KEY,
                work_date TEXT NOT NULL,
                notice_type TEXT NOT NULL,
                truck_id TEXT,
                recipient_mode TEXT NOT NULL,
                subject TEXT NOT NULL,
                sent_ok INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_dn_date_type ON dispatch_notices(work_date, notice_type)")

        conn.commit()
    finally:
        conn.close()
    return target


def has_notice(conn: sqlite3.Connection, notice_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM dispatch_notices WHERE notice_key = ? AND sent_ok = 1",
        (notice_key,),
    ).fetchone()
    return row is not None


def record_notice(
    conn: sqlite3.Connection,
    *,
    notice_key: str,
    work_date: str,
    notice_type: str,
    truck_id: Optional[str],
    recipient_mode: str,
    subject: str,
    sent_ok: bool,
    detail: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO dispatch_notices (
            notice_key, work_date, notice_type, truck_id,
            recipient_mode, subject, sent_ok, detail, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(notice_key) DO UPDATE SET
            sent_ok = excluded.sent_ok,
            detail = excluded.detail,
            created_at = excluded.created_at
        """,
        (
            notice_key,
            work_date,
            notice_type,
            truck_id,
            recipient_mode,
            subject,
            1 if sent_ok else 0,
            detail,
            created_at,
        ),
    )


def fetch_horizon_parts(conn: sqlite3.Connection, work_date: str) -> Dict[str, Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM plan_horizon_parts WHERE work_date = ? ORDER BY vendor_display, part_no",
        (work_date,),
    ).fetchall()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        d["load_36"] = [float(x) for x in json.loads(d["load_json"])]
        d["req_36"] = [float(x) for x in json.loads(d["req_json"])]
        d["srm_36"] = [float(x) for x in json.loads(d["srm_json"])]
        d["extra_36"] = [float(x) for x in json.loads(d["extra_json"])]
        d["add_3"] = [float(x) for x in json.loads(d["add_json"])]
        out[d["part_key"]] = d
    return out


def fetch_truck_runs(conn: sqlite3.Connection, work_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM truck_runs WHERE work_date = ? ORDER BY departure_ts, supplier_key, slot_idx, truck_id",
        (work_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_truck_plan_lines(conn: sqlite3.Connection, work_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM truck_plan_lines WHERE work_date = ? ORDER BY truck_id, part_no",
        (work_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_barcodes(conn: sqlite3.Connection, work_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM wms_gr_barcodes WHERE work_date = ? ORDER BY gr_time, barcode",
        (work_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_verdicts(conn: sqlite3.Connection, work_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM delivery_verdicts WHERE work_date = ? ORDER BY truck_id, part_no",
        (work_date,),
    ).fetchall()
    return [dict(r) for r in rows]
