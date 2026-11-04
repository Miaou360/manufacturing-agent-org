"""
Database Manager for 02_Backlog
===============================
SQLite database schema and operations for tracking SAP COGI errors,
MB1B transfer postings (Header: Backlog_Clear), and daily resolution metrics.
Follows WAL mode, single-writer pattern, and resource guardrails.
"""

import os
import sys
import sqlite3
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
from typing import List, Dict, Optional, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "backlog_history.db"

logger = logging.getLogger("BacklogDB")

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection configured with WAL mode and row factory."""
    target_db = db_path or DB_PATH
    conn = sqlite3.connect(str(target_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn

def init_db(db_path: Optional[Path] = None):
    """Initializes tables and indexes for Backlog management."""
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        
        # 1. Raw Pre-Resolution Backlog History (매일 6시 Refresh 후 처리 전 원본 이력)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS raw_backlog_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            history_id TEXT NOT NULL,
            run_date TEXT NOT NULL,
            run_time TEXT NOT NULL,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            material TEXT NOT NULL,
            material_desc TEXT,
            plant TEXT DEFAULT 'P001',
            sloc TEXT NOT NULL,
            mty TEXT DEFAULT '261',
            qty REAL NOT NULL,
            uoe TEXT DEFAULT 'EA',
            error_code TEXT,
            entry_date TEXT,
            error_date TEXT,
            source_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_raw_hist_date ON raw_backlog_history(run_date, run_time);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_raw_hist_mat ON raw_backlog_history(material);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_raw_hist_id ON raw_backlog_history(history_id);")

        # 2. COGI Operational Snapshots
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS cogi_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            material TEXT NOT NULL,
            material_desc TEXT,
            plant TEXT DEFAULT 'P001',
            sloc TEXT NOT NULL,
            mty TEXT DEFAULT '261',
            qty REAL NOT NULL,
            uoe TEXT DEFAULT 'EA',
            error_code TEXT,
            entry_date TEXT,
            error_date TEXT,
            resolution_status TEXT DEFAULT 'PENDING',
            resolved_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cogi_mat_sloc ON cogi_snapshots(material, sloc);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cogi_snap_status ON cogi_snapshots(snapshot_id, resolution_status);")

        # 2. MB1B Transfer Postings
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS backlog_transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL,
            run_date TEXT,
            run_time TEXT,
            executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            material TEXT NOT NULL,
            material_desc TEXT,
            from_sloc TEXT NOT NULL,
            to_sloc TEXT NOT NULL,
            transfer_qty REAL NOT NULL,
            header_text TEXT DEFAULT 'Backlog_Clear',
            recipient TEXT DEFAULT 'AUTO_AGENT',
            sap_doc_no TEXT,
            sap_raw_msg TEXT,
            status TEXT NOT NULL,
            variance_before REAL,
            variance_after REAL,
            delta_variance REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # Migration: Add run_date and run_time if table was created previously without them
        cursor.execute("PRAGMA table_info(backlog_transfers);")
        existing_cols = [row["name"] for row in cursor.fetchall()]
        if "run_date" not in existing_cols:
            cursor.execute("ALTER TABLE backlog_transfers ADD COLUMN run_date TEXT;")
        if "run_time" not in existing_cols:
            cursor.execute("ALTER TABLE backlog_transfers ADD COLUMN run_time TEXT;")

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_mat ON backlog_transfers(material);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_batch ON backlog_transfers(batch_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_date ON backlog_transfers(run_date, run_time);")

        # 3. Daily Execution Summary & Notification Metrics
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily_backlog_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            run_time TEXT NOT NULL,
            total_cogi_items INTEGER DEFAULT 0,
            total_cogi_qty REAL DEFAULT 0.0,
            resolved_items INTEGER DEFAULT 0,
            resolved_qty REAL DEFAULT 0.0,
            unresolved_items INTEGER DEFAULT 0,
            unresolved_qty REAL DEFAULT 0.0,
            email_sent INTEGER DEFAULT 0,
            email_sent_at TIMESTAMP,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)

        # 4. SAP Deficit Unresolved Tracker (D32)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS SAP_DEFICIT_UNRESOLVED_TRACKER (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            material TEXT NOT NULL,
            material_desc TEXT,
            storage_loc TEXT NOT NULL,
            run_date TEXT NOT NULL,
            sap_stock REAL NOT NULL,
            wms_stock REAL NOT NULL,
            deficit_qty REAL NOT NULL,
            accuracy_before REAL,
            accuracy_after REAL,
            recent_doc TEXT,
            first_detected_date TEXT NOT NULL,
            consecutive_days INTEGER DEFAULT 1,
            is_sl49 INTEGER DEFAULT 0,
            is_sustained_deficit INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(material, storage_loc, run_date)
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sdut_date ON SAP_DEFICIT_UNRESOLVED_TRACKER(run_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sdut_mat ON SAP_DEFICIT_UNRESOLVED_TRACKER(material);")

        cursor.execute("PRAGMA table_info(SAP_DEFICIT_UNRESOLVED_TRACKER);")
        sdut_cols = [r[1] for r in cursor.fetchall()]
        if "is_sustained_deficit" not in sdut_cols:
            try:
                cursor.execute("ALTER TABLE SAP_DEFICIT_UNRESOLVED_TRACKER ADD COLUMN is_sustained_deficit INTEGER DEFAULT 0;")
            except Exception:
                pass

        conn.commit()
        logger.info(f"Database initialized at {DB_PATH} (WAL enabled).")

def log_raw_backlog_history(history_id: str, run_date: str, run_time: str, source_file: str, items: List[Dict[str, Any]], db_path: Optional[Path] = None) -> int:
    """
    Inserts refreshed pre-resolution raw backlog items into raw_backlog_history table.
    Tracks every initial backlog state before MB1B stock transfer execution.
    """
    if not items:
        return 0
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        inserted = 0
        for it in items:
            cursor.execute("""
            INSERT INTO raw_backlog_history (
                history_id, run_date, run_time, material, material_desc,
                plant, sloc, mty, qty, uoe, error_code, entry_date, error_date,
                source_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                history_id,
                run_date,
                run_time,
                it.get("Material", "").strip(),
                it.get("Material_Desc", "").strip(),
                it.get("Plant", _get_ops_env("SAP_PLANT", "P001")).strip(),
                it.get("SLoc", "").strip(),
                it.get("MTy", "261").strip(),
                float(it.get("Qty", 0.0)),
                it.get("UoE", "EA").strip(),
                it.get("Error_Code", "").strip(),
                it.get("Entry_Date", "").strip(),
                it.get("Error_Date", "").strip(),
                source_file
            ))
            inserted += 1
        conn.commit()
        return inserted

def get_raw_backlog_history(run_date: Optional[str] = None, run_time: Optional[str] = None, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Retrieves raw backlog history records filtered by run_date or run_time."""
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM raw_backlog_history WHERE 1=1"
        params = []
        if run_date:
            query += " AND run_date = ?"
            params.append(run_date)
        if run_time:
            query += " AND run_time = ?"
            params.append(run_time)
        query += " ORDER BY id ASC"
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def log_cogi_snapshot(snapshot_id: str, items: List[Dict[str, Any]], db_path: Optional[Path] = None) -> int:
    """Inserts extracted COGI error items into the snapshot table."""
    if not items:
        return 0
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        inserted = 0
        for it in items:
            cursor.execute("""
            INSERT INTO cogi_snapshots (
                snapshot_id, material, material_desc, plant, sloc, mty, qty, uoe,
                error_code, entry_date, error_date, resolution_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snapshot_id,
                it.get("Material", "").strip(),
                it.get("Material_Desc", "").strip(),
                it.get("Plant", _get_ops_env("SAP_PLANT", "P001")).strip(),
                it.get("SLoc", "").strip(),
                it.get("MTy", "261").strip(),
                float(it.get("Qty", 0.0)),
                it.get("UoE", "EA").strip(),
                it.get("Error_Code", "").strip(),
                it.get("Entry_Date", "").strip(),
                it.get("Error_Date", "").strip(),
                it.get("Resolution_Status", "PENDING")
            ))
            inserted += 1
        conn.commit()
        return inserted

def log_transfer_posting(
    batch_id: str,
    result: Dict[str, Any],
    run_date: Optional[str] = None,
    run_time: Optional[str] = None
) -> int:
    """Logs an executed MB1B transfer posting."""
    import re
    raw_msg = result.get("Result", "")
    doc_match = re.search(r"(\d{10})", raw_msg)
    doc_no = doc_match.group(1) if doc_match else None
    status = "SUCCESS" if doc_no else "FAILED"

    if not run_date:
        run_date = datetime.now().strftime("%Y-%m-%d")
    if not run_time:
        if "06" in batch_id:
            run_time = "06:00"
        elif "08" in batch_id:
            run_time = "08:00"
        else:
            run_time = datetime.now().strftime("%H:%M")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO backlog_transfers (
            batch_id, run_date, run_time, material, material_desc, from_sloc, to_sloc, transfer_qty,
            header_text, recipient, sap_doc_no, sap_raw_msg, status,
            variance_before, variance_after, delta_variance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id,
            run_date,
            run_time,
            result.get("Material", ""),
            result.get("Material_Desc", ""),
            result.get("From", ""),
            result.get("To", ""),
            float(result.get("Qty", 0.0)),
            result.get("Header_Text", "Backlog_Clear"),
            result.get("Recipient") or _get_ops_env("SAP_MB1B_RECIPIENT", "AUTO_AGENT"),
            doc_no,
            raw_msg,
            status,
            result.get("Variance_Before"),
            result.get("Variance_After"),
            result.get("Delta_Variance")
        ))
        conn.commit()
        return cursor.lastrowid

def get_daily_transfers(run_date: str) -> List[Dict[str, Any]]:
    """
    Retrieves all successful MB1B transfer postings executed on run_date (YYYY-MM-DD),
    covering both 06:00 AM and 08:00 AM cycles.
    """
    compact_date = run_date.replace("-", "")
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM backlog_transfers
            WHERE (run_date = ? OR batch_id LIKE ? OR date(executed_at) = ? OR date(created_at) = ?)
              AND (status = 'SUCCESS' OR sap_doc_no IS NOT NULL)
            ORDER BY id ASC
        """, (run_date, f"%{compact_date}%", run_date, run_date))
        rows = [dict(row) for row in cursor.fetchall()]
        
        formatted = []
        for r in rows:
            rt = str(r.get("run_time") or "").strip()
            bid = str(r.get("batch_id") or "")
            if "06" in rt or "06" in bid:
                cycle_label = "06:10 AM" if ("10" in rt or "10" in bid) else "06:00 AM"
            elif "08" in rt or "08" in bid:
                cycle_label = "08:15 AM" if ("15" in rt or "15" in bid) else "08:00 AM"
            elif rt:
                cycle_label = f"{rt} AM" if "AM" not in rt and "PM" not in rt else rt
            else:
                cycle_label = "Earlier Run"

            formatted.append({
                "Material": r.get("material", ""),
                "Material_Desc": r.get("material_desc", ""),
                "From": r.get("from_sloc", ""),
                "To": r.get("to_sloc", ""),
                "Qty": float(r.get("transfer_qty", 0.0)),
                "Header_Text": r.get("header_text", "Backlog_Clear"),
                "Result": r.get("sap_raw_msg") or f"Document {r.get('sap_doc_no')} posted",
                "Doc_No": r.get("sap_doc_no", ""),
                "Status": r.get("status", "SUCCESS"),
                "Cycle": cycle_label,
                "Batch_Id": bid
            })
        return formatted

def get_daily_initial_backlog(run_date: str) -> Dict[str, Any]:
    """
    Retrieves the initial raw backlog state captured at 06:10 AM (or 06:00 AM) for run_date.
    Returns count, total_qty, and items.
    """
    items = get_raw_backlog_history(run_date=run_date, run_time="06:10")
    if not items:
        items = get_raw_backlog_history(run_date=run_date, run_time="06:00")
    if not items:
        # Fallback to any raw history for that date
        items = get_raw_backlog_history(run_date=run_date)
    
    total_qty = sum(float(it.get("qty", 0.0)) for it in items)
    return {
        "count": len(items),
        "total_qty": total_qty,
        "items": items
    }

def log_daily_metric(metric: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """Logs daily run summary metrics."""
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
        INSERT INTO daily_backlog_metrics (
            run_date, run_time, total_cogi_items, total_cogi_qty,
            resolved_items, resolved_qty, unresolved_items, unresolved_qty,
            email_sent, email_sent_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            metric.get("run_date"),
            metric.get("run_time"),
            metric.get("total_cogi_items", 0),
            metric.get("total_cogi_qty", 0.0),
            metric.get("resolved_items", 0),
            metric.get("resolved_qty", 0.0),
            metric.get("unresolved_items", 0),
            metric.get("unresolved_qty", 0.0),
            1 if metric.get("email_sent") else 0,
            metric.get("email_sent_at"),
            metric.get("notes", "")
        ))
        conn.commit()
        return cursor.lastrowid


def record_unresolved_sap_deficits(run_date: str, deficit_rows: List[Dict[str, Any]], db_path: Optional[Path] = None) -> int:
    """
    Records remaining unresolved SAP deficits (where WMS > SAP across the plant or SLoc)
    into SAP_DEFICIT_UNRESOLVED_TRACKER (D32).
    Calculates consecutive days and preserves first_detected_date.
    Rule: If consecutive detection is broken by missing a working day, resets consecutive_days to 1.
    Sunday is excluded from breaking continuity.
    """
    if not deficit_rows:
        return 0

    target_db = db_path or DB_PATH
    conn = sqlite3.connect(str(target_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        # Find previous recorded run_date
        cursor.execute("SELECT DISTINCT run_date FROM SAP_DEFICIT_UNRESOLVED_TRACKER WHERE run_date < ? ORDER BY run_date DESC LIMIT 1;", (run_date,))
        prev_row = cursor.fetchone()
        prev_date = prev_row["run_date"] if prev_row else None

        # Build map of previous records
        prev_map = {}
        if prev_date:
            cursor.execute("SELECT material, storage_loc, first_detected_date, consecutive_days FROM SAP_DEFICIT_UNRESOLVED_TRACKER WHERE run_date = ?;", (prev_date,))
            for r in cursor.fetchall():
                key = (r["material"], r["storage_loc"])
                prev_map[key] = {
                    "first_detected_date": r["first_detected_date"],
                    "consecutive_days": r["consecutive_days"]
                }

        # Check if prev_date is contiguous to run_date (allowing for Sunday gap)
        is_contiguous = False
        if prev_date:
            try:
                d_curr = datetime.strptime(run_date, "%Y-%m-%d")
                d_prev = datetime.strptime(prev_date, "%Y-%m-%d")
                day_diff = (d_curr - d_prev).days
                if day_diff == 1:
                    is_contiguous = True
                elif day_diff == 2 and (d_prev.weekday() == 5):  # Prev was Saturday, intervening was Sunday
                    is_contiguous = True
            except Exception:
                is_contiguous = False

        inserted = 0
        for row in deficit_rows:
            mat = str(row.get("Material", "")).strip()
            sloc = str(row.get("Storage_Loc", row.get("SLoc", ""))).strip().upper()
            if not mat or not sloc:
                continue

            key = (mat, sloc)
            if key in prev_map and is_contiguous:
                consecutive = prev_map[key]["consecutive_days"] + 1
                first_detected = prev_map[key]["first_detected_date"]
            else:
                consecutive = 1
                first_detected = run_date

            is_sl49 = 1 if sloc == "SL49" else 0
            is_sustained = 1 if row.get("is_sustained_deficit") else 0

            cursor.execute("""
            INSERT OR REPLACE INTO SAP_DEFICIT_UNRESOLVED_TRACKER (
                material, material_desc, storage_loc, run_date,
                sap_stock, wms_stock, deficit_qty,
                accuracy_before, accuracy_after, recent_doc,
                first_detected_date, consecutive_days, is_sl49, is_sustained_deficit, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (
                mat,
                row.get("Material_Desc", ""),
                sloc,
                run_date,
                float(row.get("SAP_Stock", 0.0)),
                float(row.get("WMS_Stock", 0.0)),
                float(row.get("Deficit_Qty", 0.0)),
                row.get("Accuracy_Before"),
                row.get("Accuracy_After"),
                str(row.get("Recent_Doc", "")),
                first_detected,
                consecutive,
                is_sl49,
                is_sustained
            ))
            inserted += 1

        conn.commit()
        return inserted
    finally:
        conn.close()


def get_unresolved_sap_deficits(run_date: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Returns unresolved SAP deficit records for the specified run_date."""
    target_db = db_path or DB_PATH
    conn = sqlite3.connect(str(target_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.cursor()
        cursor.execute("""
        SELECT 
            material AS Material,
            material_desc AS Material_Desc,
            storage_loc AS Storage_Loc,
            run_date,
            sap_stock AS SAP_Stock,
            wms_stock AS WMS_Stock,
            deficit_qty AS Deficit_Qty,
            accuracy_before AS Accuracy_Before,
            accuracy_after AS Accuracy_After,
            recent_doc AS Recent_Doc,
            first_detected_date,
            consecutive_days,
            is_sl49,
            is_sustained_deficit
        FROM SAP_DEFICIT_UNRESOLVED_TRACKER
        WHERE run_date = ?
        ORDER BY is_sl49 ASC, deficit_qty DESC;
        """, (run_date,))
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("[DB INIT] Successfully initialized backlog_history.db")
