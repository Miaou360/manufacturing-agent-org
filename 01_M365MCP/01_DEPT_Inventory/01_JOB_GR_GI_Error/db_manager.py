"""
Database Manager for GR/GI Interface Error Management
=====================================================
01_Inventory_Team / 01_GR_GI_Error (Plant P001 Headlamp)
- SQLite 기반 로컬 DB (db/gr_gi_history.db)
- WAL 모드 적용 (고속 비차단 동시성 보장)
- 수집된 GI/GR 에러 스냅샷, MB1B 실제 이동전기 전표 발행 이력, 시간별 배치 실행 메트릭 영구 저장
- D97 & D90-2: GI_STOCK_GAP_SIGNALS 단일 테이블에 ROUNDTRIP_CLEAR / CHAIN_GAP / TOTAL_MISMATCH 통합 기록
  ((signal_date, material, signal_type) 기준 일 1행 중복 제거 및 first_observed_at / last_observed_at 갱신)
"""

import os
import re
import sys
import json
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
from typing import Optional, List, Dict, Any
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "gr_gi_history.db"

logger = logging.getLogger("GRGI_DBManager")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with WAL mode enabled."""
    target_path = Path(db_path) if db_path is not None else DB_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target_path), timeout=30.0)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Optional[Path] = None):
    """Initializes the database schema if tables do not exist and migrates D90-2 columns."""
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        
        # 1. GI Error Snapshots
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gi_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_date TEXT,
                vendor TEXT,
                material TEXT NOT NULL,
                material_desc TEXT,
                mvt TEXT,
                from_sloc TEXT,
                to_sloc TEXT,
                qty REAL,
                message TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gi_batch ON gi_error_snapshots(batch_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gi_mat ON gi_error_snapshots(material);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gi_date ON gi_error_snapshots(error_date);")

        # 2. GR Error Snapshots
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gr_error_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                error_date TEXT,
                vendor TEXT,
                material TEXT NOT NULL,
                material_desc TEXT,
                mvt TEXT,
                qty REAL,
                message TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gr_batch ON gr_error_snapshots(batch_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gr_mat ON gr_error_snapshots(material);")

        # 3. MB1B Transfer Posting History
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS transfer_posting_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                material TEXT NOT NULL,
                from_sloc TEXT NOT NULL,
                to_sloc TEXT NOT NULL,
                transfer_qty REAL NOT NULL,
                mb1b_mvt TEXT DEFAULT '311',
                doc_number TEXT,
                post_status TEXT NOT NULL,
                posting_type TEXT NOT NULL,
                sap_response TEXT,
                reason TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_posting_batch ON transfer_posting_history(batch_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_posting_mat ON transfer_posting_history(material);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_posting_doc ON transfer_posting_history(doc_number);")

        # 4. Pipeline Run Metrics
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pipeline_run_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT UNIQUE NOT NULL,
                run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                plant TEXT NOT NULL,
                total_gi_errors INTEGER DEFAULT 0,
                total_gr_errors INTEGER DEFAULT 0,
                full_resolution_count INTEGER DEFAULT 0,
                partial_rebalance_count INTEGER DEFAULT 0,
                insufficient_stock_count INTEGER DEFAULT 0,
                excluded_count INTEGER DEFAULT 0,
                postings_executed INTEGER DEFAULT 0,
                elapsed_seconds REAL DEFAULT 0.0,
                status TEXT NOT NULL
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_metrics_batch ON pipeline_run_metrics(batch_id);")

        # 5. Email Dispatch Log (Deduplication Guardrail)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gi_email_sent_log (
                slot_key TEXT PRIMARY KEY,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                email_type TEXT,
                subject TEXT,
                recipients TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_email_slot ON gi_email_sent_log(slot_key);")

        # 5b. D106 Daily 07:30 Report Dispatch & 08:00 Retry State
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gi_daily_report_dispatch (
                report_date TEXT PRIMARY KEY,
                target_prev_date TEXT NOT NULL,
                status TEXT NOT NULL,
                attempt_count INTEGER DEFAULT 0,
                first_attempt_at TIMESTAMP,
                last_attempt_at TIMESTAMP,
                sent_at TIMESTAMP,
                subject TEXT,
                recipients TEXT,
                last_error TEXT
            );
        """)

        # 6. MMSC Storage Location Extension History
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS mmsc_extension_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                material TEXT NOT NULL,
                plant TEXT NOT NULL,
                existing_slocs TEXT,
                added_slocs TEXT,
                status TEXT NOT NULL,
                sap_response TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mmsc_mat ON mmsc_extension_history(material);")

        # 7. GI Stock Gap Signals (D97 & D90-2: ROUNDTRIP_CLEAR / CHAIN_GAP / TOTAL_MISMATCH)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS GI_STOCK_GAP_SIGNALS (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                signal_date TEXT,
                first_observed_at TIMESTAMP,
                last_observed_at TIMESTAMP,
                material TEXT NOT NULL,
                mvt TEXT,
                error_from TEXT,
                error_to TEXT,
                error_qty REAL,
                roundtrip_sloc TEXT,
                roundtrip_qty REAL,
                doc_no TEXT,
                sap_by_sloc TEXT,
                wms_by_sloc TEXT,
                sap_total REAL,
                wms_total REAL,
                total_gap REAL,
                signal_type TEXT NOT NULL,
                audit_run_id TEXT
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gap_sig_run ON GI_STOCK_GAP_SIGNALS(run_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gap_sig_mat ON GI_STOCK_GAP_SIGNALS(material);")

        # Schema migration for existing GI_STOCK_GAP_SIGNALS table
        cursor.execute("PRAGMA table_info(GI_STOCK_GAP_SIGNALS);")
        existing_cols = {r[1] for r in cursor.fetchall()}
        migrations = [
            ("signal_date", "TEXT"),
            ("first_observed_at", "TIMESTAMP"),
            ("last_observed_at", "TIMESTAMP"),
            ("audit_run_id", "TEXT"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_cols:
                cursor.execute(f"ALTER TABLE GI_STOCK_GAP_SIGNALS ADD COLUMN {col_name} {col_type};")

        cursor.execute("""
            UPDATE GI_STOCK_GAP_SIGNALS
            SET signal_date = COALESCE(signal_date, substr(observed_at, 1, 10)),
                first_observed_at = COALESCE(first_observed_at, observed_at),
                last_observed_at = COALESCE(last_observed_at, observed_at)
            WHERE signal_date IS NULL OR first_observed_at IS NULL OR last_observed_at IS NULL;
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_gap_sig_date_mat_type ON GI_STOCK_GAP_SIGNALS(signal_date, material, signal_type);")

        # 8. D107 Phase 2b Portal Temp-Saved Draft Tracking (Duplicate Save Prevention & User Submit Reconciliation)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS d107_drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                row_key TEXT UNIQUE NOT NULL,
                sumno TEXT NOT NULL,
                barcode TEXT NOT NULL,
                material TEXT NOT NULL,
                material_desc TEXT,
                mvt TEXT NOT NULL,
                from_sloc TEXT,
                to_sloc TEXT,
                qty REAL NOT NULL,
                post_date TEXT,
                draft_mode TEXT NOT NULL DEFAULT 'save_temp_2b',
                draft_subject TEXT,
                reason_line TEXT,
                saved_date TEXT NOT NULL,
                saved_at TIMESTAMP NOT NULL,
                status TEXT NOT NULL DEFAULT 'SAVED_TEMP',
                cleared_detected_at TIMESTAMP,
                last_checked_at TIMESTAMP
            );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_d107_drafts_status ON d107_drafts(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_d107_drafts_saved_date ON d107_drafts(saved_date);")

        conn.commit()
    logger.info(f"Database initialized at: {db_path or DB_PATH}")


def is_email_slot_already_sent(slot_key: str) -> bool:
    """Checks if an email was already dispatched for the given schedule slot."""
    init_db()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sent_at FROM gi_email_sent_log WHERE slot_key = ?", (slot_key,))
        row = cursor.fetchone()
        return bool(row)


def record_email_slot_sent(slot_key: str, email_type: str, subject: str, recipients: list):
    """Records that an email was successfully dispatched for this slot."""
    init_db()
    with get_connection() as conn:
        cursor = conn.cursor()
        recip_str = "; ".join(recipients) if isinstance(recipients, list) else str(recipients)
        cursor.execute("""
            INSERT OR REPLACE INTO gi_email_sent_log (slot_key, email_type, subject, recipients)
            VALUES (?, ?, ?, ?)
        """, (slot_key, email_type, subject, recip_str))
        conn.commit()


def save_gi_error_snapshots(batch_id: str, df: pd.DataFrame) -> int:
    """Inserts a batch of extracted GI errors into gi_error_snapshots."""
    if df is None or df.empty:
        return 0
        
    records = []
    for _, row in df.iterrows():
        records.append((
            batch_id,
            str(row.get('Date', '')).strip(),
            str(row.get('Vendor', '')).strip(),
            str(row.get('Material Number', '')).strip(),
            str(row.get('Material Desc', '')).strip(),
            str(row.get('Mvt', '')).strip(),
            str(row.get('FromSt', '')).strip().upper(),
            str(row.get('TOSt', '')).strip().upper(),
            float(str(row.get('Qty', 0)).replace(',', '').strip() or 0.0),
            str(row.get('Message', '')).strip()
        ))
        
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO gi_error_snapshots (
                batch_id, error_date, vendor, material, material_desc,
                mvt, from_sloc, to_sloc, qty, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        conn.commit()
        
    logger.info(f"Saved {len(records)} GI error records to DB (batch_id: {batch_id})")
    return len(records)


def save_gr_error_snapshots(batch_id: str, df: pd.DataFrame) -> int:
    """Inserts a batch of extracted GR errors into gr_error_snapshots."""
    if df is None or df.empty:
        return 0
        
    records = []
    for _, row in df.iterrows():
        records.append((
            batch_id,
            str(row.get('GR Date', row.get('Date', ''))).strip(),
            str(row.get('Vendor', '')).strip(),
            str(row.get('Material Number', row.get('Material', ''))).strip(),
            str(row.get('Material Desc', '')).strip(),
            str(row.get('Mvt', '')).strip(),
            float(str(row.get('Qty', 0)).replace(',', '').strip() or 0.0),
            str(row.get('Message', '')).strip()
        ))
        
    with get_connection() as conn:
        conn.executemany("""
            INSERT INTO gr_error_snapshots (
                batch_id, error_date, vendor, material, material_desc,
                mvt, qty, message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, records)
        conn.commit()
        
    logger.info(f"Saved {len(records)} GR error records to DB (batch_id: {batch_id})")
    return len(records)


def log_transfer_posting(batch_id: str, item_dict: dict, db_path: Optional[Path] = None, signal_db_path: Optional[Path] = None) -> int:
    """
    Logs an individual MB1B transfer posting result into transfer_posting_history.
    Extracts SAP document number (e.g. 4990000010) from result string.
    """
    mat = item_dict.get('Material', '')
    from_sloc = item_dict.get('From', item_dict.get('From_SLoc', ''))
    to_sloc = item_dict.get('To', item_dict.get('To_SLoc', ''))
    qty = float(item_dict.get('Qty', item_dict.get('Transfer_Qty', 0.0)))
    res_str = str(item_dict.get('Result', ''))
    posting_type = str(item_dict.get('Posting_Type', 'FULL_RESOLUTION'))
    reason = str(item_dict.get('Reason', ''))
    
    # Extract SAP document number (10 digits starting with 49...)
    doc_match = re.search(r'\b(49\d{8})\b', res_str)
    doc_number = doc_match.group(1) if doc_match else ""
    explicit_status = str(item_dict.get("Post_Status") or "").strip()
    if explicit_status:
        post_status = explicit_status
    elif doc_number:
        post_status = "POSTED"
    elif re.search(r'deficit', res_str, re.IGNORECASE):
        post_status = "FAILED_DEFICIT"
    elif re.search(r'error|fail', res_str, re.IGNORECASE):
        post_status = "FAILED_ERROR"
    elif not res_str.strip():
        post_status = "FAILED_NO_RESPONSE"
    else:
        post_status = "FAILED"
    
    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO transfer_posting_history (
                batch_id, material, from_sloc, to_sloc, transfer_qty,
                mb1b_mvt, doc_number, post_status, posting_type,
                sap_response, reason
            ) VALUES (?, ?, ?, ?, ?, '311', ?, ?, ?, ?, ?)
        """, (
            batch_id, mat, from_sloc, to_sloc, qty,
            doc_number, post_status, posting_type, res_str, reason
        ))
        conn.commit()
        last_id = cursor.lastrowid
        
    logger.info(f"Logged Transfer Posting in DB: ID={last_id} | Mat={mat} | Doc={doc_number or 'NONE'} | Status={post_status}")

    if posting_type in ("ROUNDTRIP_CLEAR", "ROUNDTRIP_CLEAR_VIA_DEST_D113", "DEST_PUMP_CLEAR_D115") and post_status == "POSTED":
        try:
            sap_tot_v = float(item_dict.get("SAP_Total", 0.0) or 0.0)
            wms_tot_v = float(item_dict.get("WMS_Total", 0.0) or 0.0)
            if abs(sap_tot_v - wms_tot_v) > 1e-6:
                save_gi_stock_gap_signal(
                    {
                        "run_id": batch_id,
                        "material": mat,
                        "mvt": item_dict.get("Mvt_Original", ""),
                        "error_from": item_dict.get("Error_From", to_sloc),
                        "error_to": item_dict.get("Error_To", from_sloc),
                        "error_qty": item_dict.get("Error_Qty", qty),
                        "roundtrip_sloc": from_sloc,
                        "roundtrip_qty": qty,
                        "doc_no": doc_number,
                        "sap_by_sloc": item_dict.get("SAP_By_SLoc", {}),
                        "wms_by_sloc": item_dict.get("WMS_By_SLoc", {}),
                        "sap_total": sap_tot_v,
                        "wms_total": wms_tot_v,
                        "total_gap": item_dict.get("Total_Gap", wms_tot_v - sap_tot_v),
                        "signal_type": "ROUNDTRIP_CLEAR",
                    },
                    db_path=signal_db_path if signal_db_path is not None else db_path,
                )
        except Exception as e_sig:
            logger.warning(f"Could not save GI_STOCK_GAP_SIGNALS on ROUNDTRIP_CLEAR posting: {e_sig}")

    return last_id


def save_pipeline_metrics(metrics: dict):
    """Saves high-level run metrics for each hourly execution."""
    batch_id = metrics.get('batch_id', datetime.now().strftime("%Y%m%d_%H%M%S"))
    with get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pipeline_run_metrics (
                batch_id, plant, total_gi_errors, total_gr_errors,
                full_resolution_count, partial_rebalance_count,
                insufficient_stock_count, excluded_count,
                postings_executed, elapsed_seconds, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id,
            metrics.get('plant', _get_ops_env('SAP_PLANT', 'P001')),
            metrics.get('total_gi_errors', 0),
            metrics.get('total_gr_errors', 0),
            metrics.get('full_resolution_count', 0),
            metrics.get('partial_rebalance_count', 0),
            metrics.get('insufficient_stock_count', 0),
            metrics.get('excluded_count', 0),
            metrics.get('postings_executed', 0),
            metrics.get('elapsed_seconds', 0.0),
            metrics.get('status', 'COMPLETED')
        ))
        conn.commit()
    logger.info(f"Saved Pipeline Run Metrics for batch {batch_id}")


def get_latest_run_metric(plant: str = _get_ops_env("SAP_PLANT", "P001")) -> Optional[dict]:
    """Returns the most recent completed pipeline run metric."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT batch_id, datetime(run_timestamp, 'localtime') as run_time,
                   postings_executed, elapsed_seconds, status
            FROM pipeline_run_metrics
            WHERE plant = ? AND status = 'COMPLETED'
            ORDER BY id DESC LIMIT 1
        """, (plant,))
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "batch_id": row[0],
            "run_timestamp": row[1],
            "postings_executed": row[2],
            "elapsed_seconds": row[3],
            "status": row[4]
        }


def get_recent_postings(limit: int = 20) -> pd.DataFrame:
    """Returns the most recent transfer postings as a DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(f"""
            SELECT id, batch_id, executed_at, material, from_sloc, to_sloc,
                   transfer_qty, doc_number, post_status, posting_type, reason
            FROM transfer_posting_history
            ORDER BY id DESC LIMIT {limit}
        """, conn)


def get_24h_postings() -> pd.DataFrame:
    """Returns all transfer postings executed in the past 24 hours."""
    with get_connection() as conn:
        return pd.read_sql("""
            SELECT id, batch_id, datetime(executed_at, 'localtime') as exec_time,
                   material, from_sloc, to_sloc, transfer_qty, doc_number,
                   post_status, posting_type, reason
            FROM transfer_posting_history
            WHERE executed_at >= datetime('now', '-24 hours')
            ORDER BY id DESC
        """, conn)


def get_24h_metrics_summary() -> dict:
    """Returns high-level KPI metrics for the past 24 hours."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total_runs,
                COALESCE(SUM(postings_executed), 0) as total_postings,
                COALESCE(AVG(elapsed_seconds), 0.0) as avg_elapsed
            FROM pipeline_run_metrics
            WHERE run_timestamp >= datetime('now', '-24 hours')
        """)
        row = cursor.fetchone()
        
        cursor.execute("""
            SELECT 
                COUNT(*) as total_post_records,
                COALESCE(SUM(CASE WHEN post_status = 'POSTED' THEN transfer_qty ELSE 0 END), 0.0) as total_posted_qty,
                COALESCE(SUM(CASE WHEN post_status = 'POSTED' THEN 1 ELSE 0 END), 0) as successful_postings,
                COALESCE(SUM(CASE WHEN post_status = 'FAILED' THEN 1 ELSE 0 END), 0) as failed_postings
            FROM transfer_posting_history
            WHERE executed_at >= datetime('now', '-24 hours')
        """)
        post_row = cursor.fetchone()
        
        return {
            'total_runs': row[0] if row else 0,
            'total_postings_metric': row[1] if row else 0,
            'avg_elapsed_sec': round(row[2], 1) if row else 0.0,
            'total_post_records': post_row[0] if post_row else 0,
            'total_posted_qty': round(post_row[1], 1) if post_row else 0.0,
            'successful_postings': post_row[2] if post_row else 0,
            'failed_postings': post_row[3] if post_row else 0
        }


def get_24h_run_history(limit: int = 48) -> pd.DataFrame:
    """Returns the history of pipeline runs over the past 24 hours."""
    with get_connection() as conn:
        return pd.read_sql(f"""
            SELECT batch_id, datetime(run_timestamp, 'localtime') as run_time,
                   total_gi_errors, total_gr_errors, postings_executed,
                   full_resolution_count, partial_rebalance_count,
                   elapsed_seconds, status
            FROM pipeline_run_metrics
            WHERE run_timestamp >= datetime('now', '-24 hours')
            ORDER BY id DESC LIMIT {limit}
        """, conn)


def _open_readonly_conn(db_path: Optional[Path] = None) -> sqlite3.Connection:
    target = Path(db_path) if db_path is not None else DB_PATH
    if target.exists():
        uri = f"file:{target.as_posix()}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=30.0)
    return get_connection(db_path=target)


def get_prev_day_postings(prev_date: str, db_path: Optional[Path] = None) -> pd.DataFrame:
    """D106: Returns all transfer postings executed during prev_date 00:00:00 ~ 23:59:59 local time."""
    start_ts = f"{prev_date} 00:00:00"
    end_ts = f"{prev_date} 23:59:59"
    with _open_readonly_conn(db_path=db_path) as conn:
        return pd.read_sql(
            """
            SELECT id, batch_id, datetime(executed_at, 'localtime') as exec_time,
                   material, from_sloc, to_sloc, transfer_qty, doc_number,
                   post_status, posting_type, reason
            FROM transfer_posting_history
            WHERE datetime(executed_at, 'localtime') BETWEEN ? AND ?
            ORDER BY id DESC
            """,
            conn,
            params=(start_ts, end_ts),
        )


def get_prev_day_metrics_summary(prev_date: str, db_path: Optional[Path] = None) -> dict:
    """D106: Returns KPI summary for prev_date 00:00:00 ~ 23:59:59 local time."""
    start_ts = f"{prev_date} 00:00:00"
    end_ts = f"{prev_date} 23:59:59"
    with _open_readonly_conn(db_path=db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                COUNT(*) as total_runs,
                COALESCE(SUM(postings_executed), 0) as total_postings,
                COALESCE(AVG(elapsed_seconds), 0.0) as avg_elapsed
            FROM pipeline_run_metrics
            WHERE datetime(run_timestamp, 'localtime') BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            SELECT
                COUNT(*) as total_post_records,
                COALESCE(SUM(CASE WHEN post_status = 'POSTED' THEN transfer_qty ELSE 0 END), 0.0) as total_posted_qty,
                COALESCE(SUM(CASE WHEN post_status = 'POSTED' THEN 1 ELSE 0 END), 0) as successful_postings,
                COALESCE(SUM(CASE WHEN post_status LIKE 'FAILED%' THEN 1 ELSE 0 END), 0) as failed_postings
            FROM transfer_posting_history
            WHERE datetime(executed_at, 'localtime') BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        post_row = cursor.fetchone()
        return {
            "total_runs": row[0] if row else 0,
            "total_postings_metric": row[1] if row else 0,
            "avg_elapsed_sec": round(row[2], 1) if row else 0.0,
            "total_post_records": post_row[0] if post_row else 0,
            "total_posted_qty": round(post_row[1], 1) if post_row else 0.0,
            "successful_postings": post_row[2] if post_row else 0,
            "failed_postings": post_row[3] if post_row else 0,
        }


def get_prev_day_run_history(prev_date: str, limit: int = 48, db_path: Optional[Path] = None) -> pd.DataFrame:
    """D106: Returns pipeline run history during prev_date 00:00:00 ~ 23:59:59 local time."""
    start_ts = f"{prev_date} 00:00:00"
    end_ts = f"{prev_date} 23:59:59"
    with _open_readonly_conn(db_path=db_path) as conn:
        return pd.read_sql(
            f"""
            SELECT batch_id, datetime(run_timestamp, 'localtime') as run_time,
                   total_gi_errors, total_gr_errors, postings_executed,
                   full_resolution_count, partial_rebalance_count,
                   elapsed_seconds, status
            FROM pipeline_run_metrics
            WHERE datetime(run_timestamp, 'localtime') BETWEEN ? AND ?
            ORDER BY id DESC LIMIT {int(limit)}
            """,
            conn,
            params=(start_ts, end_ts),
        )


def get_daily_report_dispatch_state(report_date: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """D106: Fetches dispatch state for report_date ('SENT', 'FAILED_0730', 'RETRY_SENT', 'RETRY_FAILED')."""
    init_db(db_path=db_path)
    with get_connection(db_path=db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM gi_daily_report_dispatch WHERE report_date = ?", (report_date,))
        r = cur.fetchone()
        return dict(r) if r else None


def record_daily_report_dispatch_attempt(
    report_date: str,
    target_prev_date: str,
    status: str,
    subject: str = "",
    recipients: Optional[list] = None,
    error_msg: str = "",
    db_path: Optional[Path] = None,
) -> dict:
    """D106: Records 07:30 primary or 08:00 retry dispatch state."""
    init_db(db_path=db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    recip_str = "; ".join(recipients) if isinstance(recipients, list) else str(recipients or "")
    existing = get_daily_report_dispatch_state(report_date, db_path=db_path)
    attempts = (int(existing["attempt_count"]) if existing else 0) + 1
    first_ts = existing["first_attempt_at"] if existing and existing.get("first_attempt_at") else now_str
    sent_ts = now_str if status in ("SENT", "RETRY_SENT") else (existing.get("sent_at") if existing else None)
    with get_connection(db_path=db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO gi_daily_report_dispatch (
                report_date, target_prev_date, status, attempt_count,
                first_attempt_at, last_attempt_at, sent_at, subject, recipients, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                report_date,
                target_prev_date,
                status,
                attempts,
                first_ts,
                now_str,
                sent_ts,
                subject,
                recip_str,
                error_msg,
            ),
        )
        conn.commit()
    return get_daily_report_dispatch_state(report_date, db_path=db_path)


def get_active_error_summary(db_path: Optional[Path] = None) -> pd.DataFrame:
    """Returns summary of latest batch GI errors."""
    with _open_readonly_conn(db_path=db_path) as conn:
        return pd.read_sql("""
            SELECT material, mvt, from_sloc, to_sloc, qty, message, error_date
            FROM gi_error_snapshots
            WHERE batch_id = (SELECT batch_id FROM gi_error_snapshots ORDER BY id DESC LIMIT 1)
        """, conn)


def log_mmsc_extension(batch_id: str, record: dict):
    """Logs an MMSC storage location extension execution."""
    init_db()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO mmsc_extension_history (
                batch_id, material, plant, existing_slocs, added_slocs, status, sap_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            batch_id,
            record.get("material", ""),
            record.get("plant", _get_ops_env("SAP_PLANT", "P001")),
            record.get("existing_slocs", ""),
            record.get("added_slocs", ""),
            record.get("status", "UNKNOWN"),
            record.get("sap_response", "")
        ))
        conn.commit()


def get_recent_mmsc_extensions(limit: int = 10) -> pd.DataFrame:
    """Returns recent MMSC extension records."""
    init_db()
    with get_connection() as conn:
        return pd.read_sql(f"""
            SELECT id, batch_id, datetime(executed_at, 'localtime') as executed_at,
                   material, plant, existing_slocs, added_slocs, status, sap_response
            FROM mmsc_extension_history
            ORDER BY id DESC LIMIT {limit}
        """, conn)


def _derive_signal_date(run_id: str, explicit_date: Optional[str] = None) -> str:
    """Derives YYYY-MM-DD signal_date from explicit_date, run_id, or current date."""
    if explicit_date:
        clean = str(explicit_date).strip().replace(".", "-")
        if re.match(r"^\d{4}-\d{2}-\d{2}$", clean):
            return clean
        if re.match(r"^\d{8}$", clean):
            return f"{clean[:4]}-{clean[4:6]}-{clean[6:8]}"
    m = re.search(r"(20\d{2})(\d{2})(\d{2})", str(run_id or ""))
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.now().strftime("%Y-%m-%d")


def save_gi_stock_gap_signal(signal: Dict[str, Any], db_path: Optional[Path] = None) -> int:
    """
    Inserts or updates a D97 / D90-2 stock gap signal row in GI_STOCK_GAP_SIGNALS.
    Deduplicated to 1 row per (signal_date, material, signal_type) per D90-2:
    - Preserves first_observed_at from the first observation of the day
    - Updates last_observed_at, run_id, doc_no, and latest stock quantities on subsequent cycles
    """
    init_db(db_path=db_path)

    now_ts = str(signal.get("observed_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    run_id = str(signal.get("run_id") or datetime.now().strftime("RUN_%Y%m%d_%H%M%S"))
    signal_date = _derive_signal_date(run_id, signal.get("signal_date"))
    material = str(signal.get("material", "")).strip()
    mvt = str(signal.get("mvt", "")).strip()
    error_from = str(signal.get("error_from", "")).strip()
    error_to = str(signal.get("error_to", "")).strip()
    error_qty = float(signal.get("error_qty", 0.0) or 0.0)
    roundtrip_sloc = str(signal.get("roundtrip_sloc", "")).strip()
    roundtrip_qty = float(signal.get("roundtrip_qty", 0.0) or 0.0)
    doc_no = str(signal.get("doc_no", "") or "").strip()
    audit_run_id = signal.get("audit_run_id")

    sap_by_sloc_raw = signal.get("sap_by_sloc", {})
    wms_by_sloc_raw = signal.get("wms_by_sloc", {})
    if isinstance(sap_by_sloc_raw, str):
        sap_by_sloc_json = sap_by_sloc_raw
        try:
            sap_dict = json.loads(sap_by_sloc_raw)
        except Exception:
            sap_dict = {}
    else:
        sap_dict = {str(k): float(v) for k, v in (sap_by_sloc_raw or {}).items()}
        sap_by_sloc_json = json.dumps(sap_dict, ensure_ascii=False, sort_keys=True)

    if isinstance(wms_by_sloc_raw, str):
        wms_by_sloc_json = wms_by_sloc_raw
        try:
            wms_dict = json.loads(wms_by_sloc_raw)
        except Exception:
            wms_dict = {}
    else:
        wms_dict = {str(k): float(v) for k, v in (wms_by_sloc_raw or {}).items()}
        wms_by_sloc_json = json.dumps(wms_dict, ensure_ascii=False, sort_keys=True)

    sap_total = float(signal.get("sap_total") if signal.get("sap_total") is not None else sum(sap_dict.values()))
    wms_total = float(signal.get("wms_total") if signal.get("wms_total") is not None else sum(wms_dict.values()))
    total_gap = float(signal.get("total_gap") if signal.get("total_gap") is not None else (wms_total - sap_total))
    signal_type = str(signal.get("signal_type") or "ROUNDTRIP_CLEAR").strip()

    with get_connection(db_path=db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, doc_no, audit_run_id FROM GI_STOCK_GAP_SIGNALS
            WHERE signal_date = ? AND material = ? AND signal_type = ?
            ORDER BY id DESC LIMIT 1
            """,
            (signal_date, material, signal_type)
        )
        existing = cursor.fetchone()
        if existing:
            row_id = existing[0]
            new_doc_no = doc_no if doc_no else (existing[1] or "")
            new_audit_run = audit_run_id if audit_run_id else existing[2]
            cursor.execute(
                """
                UPDATE GI_STOCK_GAP_SIGNALS
                SET run_id = ?,
                    last_observed_at = ?,
                    mvt = CASE WHEN ? != '' THEN ? ELSE mvt END,
                    error_from = CASE WHEN ? != '' THEN ? ELSE error_from END,
                    error_to = CASE WHEN ? != '' THEN ? ELSE error_to END,
                    error_qty = CASE WHEN ? > 0 THEN ? ELSE error_qty END,
                    roundtrip_sloc = CASE WHEN ? != '' THEN ? ELSE roundtrip_sloc END,
                    roundtrip_qty = CASE WHEN ? > 0 THEN ? ELSE roundtrip_qty END,
                    doc_no = ?,
                    sap_by_sloc = CASE WHEN ? != '{}' THEN ? ELSE sap_by_sloc END,
                    wms_by_sloc = CASE WHEN ? != '{}' THEN ? ELSE wms_by_sloc END,
                    sap_total = ?,
                    wms_total = ?,
                    total_gap = ?,
                    audit_run_id = ?
                WHERE id = ?
                """,
                (
                    run_id, now_ts,
                    mvt, mvt,
                    error_from, error_from,
                    error_to, error_to,
                    error_qty, error_qty,
                    roundtrip_sloc, roundtrip_sloc,
                    roundtrip_qty, roundtrip_qty,
                    new_doc_no,
                    sap_by_sloc_json, sap_by_sloc_json,
                    wms_by_sloc_json, wms_by_sloc_json,
                    sap_total, wms_total, total_gap,
                    new_audit_run,
                    row_id
                )
            )
            conn.commit()
            return row_id

        cursor.execute(
            """
            INSERT INTO GI_STOCK_GAP_SIGNALS (
                run_id, observed_at, signal_date, first_observed_at, last_observed_at,
                material, mvt, error_from, error_to, error_qty,
                roundtrip_sloc, roundtrip_qty, doc_no,
                sap_by_sloc, wms_by_sloc, sap_total, wms_total, total_gap,
                signal_type, audit_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, now_ts, signal_date, now_ts, now_ts,
                material, mvt, error_from, error_to, error_qty,
                roundtrip_sloc, roundtrip_qty, doc_no,
                sap_by_sloc_json, wms_by_sloc_json, sap_total, wms_total, total_gap,
                signal_type, audit_run_id
            )
        )
        conn.commit()
        return cursor.lastrowid


def record_plan_stock_gap_signals(
    run_id: str,
    plan_rows: List[Dict[str, Any]],
    db_path: Optional[Path] = None
) -> int:
    """
    D97 & D90-2 Unified Signal Recording at plan stage:
    1. ROUNDTRIP_CLEAR: when Status == 'ROUNDTRIP_CLEAR' and SAP_Total != WMS_Total
    2. CHAIN_GAP: when Is_Chain_Gap is True (GR SL00 + GI SL01->SP31 without SL00->SL01 bridge)
    3. TOTAL_MISMATCH: when a GI error material has SAP_Total != WMS_Total and is not ROUNDTRIP_CLEAR
    Deduplicated to 1 row per (signal_date, material, signal_type).
    """
    count = 0
    roundtrip_mats = set()

    # Pass 1: ROUNDTRIP_CLEAR and CHAIN_GAP
    for r in plan_rows:
        mat = str(r.get("Material", "")).strip()
        if not mat:
            continue
        sap_tot = float(r.get("SAP_Total", 0.0) or 0.0)
        wms_tot = float(r.get("WMS_Total", 0.0) or 0.0)

        if r.get("Status") in ("ROUNDTRIP_CLEAR", "ROUNDTRIP_CLEAR_VIA_DEST_D113", "DEST_PUMP_CLEAR_D115"):
            roundtrip_mats.add(mat)
            if abs(sap_tot - wms_tot) > 1e-6:
                save_gi_stock_gap_signal(
                    {
                        "run_id": run_id,
                        "material": mat,
                        "mvt": r.get("Mvt_Original", ""),
                        "error_from": r.get("Error_From", r.get("To_SLoc", "")),
                        "error_to": r.get("Error_To", r.get("From_SLoc", "")),
                        "error_qty": r.get("Error_Qty", r.get("Transfer_Qty", 0.0)),
                        "roundtrip_sloc": r.get("From_SLoc", ""),
                        "roundtrip_qty": r.get("Transfer_Qty", 0.0),
                        "doc_no": r.get("Doc_No", ""),
                        "sap_by_sloc": r.get("SAP_By_SLoc", {}),
                        "wms_by_sloc": r.get("WMS_By_SLoc", {}),
                        "sap_total": sap_tot,
                        "wms_total": wms_tot,
                        "total_gap": wms_tot - sap_tot,
                        "signal_type": "ROUNDTRIP_CLEAR",
                    },
                    db_path=db_path,
                )
                count += 1

        if r.get("Is_Chain_Gap"):
            save_gi_stock_gap_signal(
                {
                    "run_id": run_id,
                    "material": mat,
                    "mvt": r.get("Mvt_Original", ""),
                    "error_from": r.get("Error_From", r.get("Deficit_Loc", "")),
                    "error_to": r.get("Error_To", r.get("To_SLoc", "")),
                    "error_qty": r.get("Error_Qty", r.get("Transfer_Qty", 0.0)),
                    "roundtrip_sloc": "",
                    "roundtrip_qty": 0.0,
                    "doc_no": "",
                    "sap_by_sloc": r.get("SAP_By_SLoc", {}),
                    "wms_by_sloc": r.get("WMS_By_SLoc", {}),
                    "sap_total": sap_tot,
                    "wms_total": wms_tot,
                    "total_gap": wms_tot - sap_tot,
                    "signal_type": "CHAIN_GAP",
                },
                db_path=db_path,
            )
            count += 1

    # Pass 2: TOTAL_MISMATCH (GI error materials with SAP_Total != WMS_Total not in ROUNDTRIP_CLEAR)
    seen_total_mismatch = set()
    for r in plan_rows:
        mat = str(r.get("Material", "")).strip()
        if not mat or mat in roundtrip_mats or mat in seen_total_mismatch:
            continue
        if "SAP_Total" not in r or "WMS_Total" not in r:
            continue
        sap_tot = float(r.get("SAP_Total", 0.0) or 0.0)
        wms_tot = float(r.get("WMS_Total", 0.0) or 0.0)
        if abs(sap_tot - wms_tot) > 1e-6:
            seen_total_mismatch.add(mat)
            save_gi_stock_gap_signal(
                {
                    "run_id": run_id,
                    "material": mat,
                    "mvt": r.get("Mvt_Original", ""),
                    "error_from": r.get("Error_From", r.get("Deficit_Loc", "")),
                    "error_to": r.get("Error_To", r.get("To_SLoc", "")),
                    "error_qty": r.get("Error_Qty", r.get("Transfer_Qty", 0.0)),
                    "roundtrip_sloc": "",
                    "roundtrip_qty": 0.0,
                    "doc_no": "",
                    "sap_by_sloc": r.get("SAP_By_SLoc", {}),
                    "wms_by_sloc": r.get("WMS_By_SLoc", {}),
                    "sap_total": sap_tot,
                    "wms_total": wms_tot,
                    "total_gap": wms_tot - sap_tot,
                    "signal_type": "TOTAL_MISMATCH",
                },
                db_path=db_path,
            )
            count += 1

    return count


def build_d107_draft_row_key(sumno: Any, barcode: Any, material: Any, mvt: Any, qty: Any) -> str:
    """Builds the canonical D107 Phase 2b composite key: SUMNO|BARCODE|PART|MVT|QTY."""
    s_clean = str(sumno or "").strip()
    b_clean = str(barcode or "").strip()
    m_clean = str(material or "").strip()
    v_clean = str(mvt or "").replace(".0", "").strip()
    try:
        q_val = round(float(qty or 0.0), 3)
    except (TypeError, ValueError):
        q_val = 0.0
    return f"{s_clean}|{b_clean}|{m_clean}|{v_clean}|{q_val:.3f}"


def get_pending_d107_temp_drafts(db_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Returns all active `SAVED_TEMP` rows in `d107_drafts` keyed by `row_key`."""
    init_db(db_path=db_path)
    with get_connection(db_path=db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM d107_drafts
            WHERE status = 'SAVED_TEMP'
            ORDER BY saved_at ASC, id ASC
            """
        )
        return {str(r["row_key"]): dict(r) for r in cur.fetchall()}


def count_d107_business_days(saved_date_str: str, report_date_str: str) -> int:
    from datetime import timedelta
    try:
        d0 = datetime.strptime(str(saved_date_str)[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(str(report_date_str)[:10], "%Y-%m-%d").date()
    except Exception:
        return 0
    if d1 <= d0:
        return 0
    return sum(1 for k in range(1, (d1 - d0).days + 1) if (d0 + timedelta(days=k)).weekday() < 5)


def format_d107_pending_or_stale_label(saved_date_or_at: str, report_date_str: str) -> tuple:
    s = str(saved_date_or_at or "").strip()
    mmdd = f"{s[5:7]}/{s[8:10]}" if len(s) >= 10 and s[4] == "-" else "prev"
    bdays = count_d107_business_days(s[:10], report_date_str)
    if bdays > 2:
        return f"stale (saved {mmdd}, {bdays} days)", True, bdays
    return f"pending user submit (saved {mmdd})", False, bdays


def record_d107_temp_draft_rows(
    rows: List[Dict[str, Any]],
    draft_subject: str,
    saved_at: Optional[str] = None,
    draft_mode: str = "save_temp_2b",
    db_path: Optional[Path] = None,
    overwrite_stale: bool = False,
) -> int:
    """
    Records portal rows temp-saved by `save_temp_2b` into `d107_drafts` (`INSERT OR IGNORE` on `row_key`
    so original `saved_at` / `saved_date` is never overwritten on accidental replay).
    """
    if not rows:
        return 0
    init_db(db_path=db_path)
    now_str = saved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    saved_date = now_str[:10]
    inserted = 0
    with get_connection(db_path=db_path) as conn:
        cur = conn.cursor()
        for r in rows:
            rk = build_d107_draft_row_key(
                r.get("sumno"),
                r.get("barcode"),
                r.get("material"),
                r.get("mvt"),
                r.get("qty"),
            )
            sql_stmt = (
                """
                INSERT INTO d107_drafts (
                    row_key, sumno, barcode, material, material_desc,
                    mvt, from_sloc, to_sloc, qty, post_date,
                    draft_mode, draft_subject, reason_line,
                    saved_date, saved_at, status, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SAVED_TEMP', ?)
                ON CONFLICT(row_key) DO UPDATE SET
                    draft_subject = excluded.draft_subject,
                    reason_line = excluded.reason_line,
                    saved_date = excluded.saved_date,
                    saved_at = excluded.saved_at,
                    status = 'SAVED_TEMP',
                    last_checked_at = excluded.last_checked_at
                """
                if overwrite_stale
                else """
                INSERT OR IGNORE INTO d107_drafts (
                    row_key, sumno, barcode, material, material_desc,
                    mvt, from_sloc, to_sloc, qty, post_date,
                    draft_mode, draft_subject, reason_line,
                    saved_date, saved_at, status, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SAVED_TEMP', ?)
                """
            )
            cur.execute(
                sql_stmt,
                (
                    rk,
                    str(r.get("sumno") or "").strip(),
                    str(r.get("barcode") or "").strip(),
                    str(r.get("material") or "").strip(),
                    str(r.get("material_desc") or "").strip(),
                    str(r.get("mvt") or "").replace(".0", "").strip(),
                    str(r.get("from_sloc") or "").strip().upper(),
                    str(r.get("to_sloc") or "").strip().upper(),
                    round(float(r.get("qty") or 0.0), 3),
                    str(r.get("post_date") or "").strip(),
                    draft_mode,
                    str(draft_subject or "").strip(),
                    str(r.get("reason_line") or "").strip(),
                    saved_date,
                    now_str,
                    now_str,
                ),
            )
            if cur.rowcount > 0:
                inserted += 1
        conn.commit()
    return inserted


def reconcile_d107_temp_drafts(
    portal_row_keys: Optional[set],
    latest_01_gi_records: Optional[List[Dict[str, Any]]],
    report_date_str: str,
    checked_at: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Reconciles prior `SAVED_TEMP` rows against the live portal GI list + latest `01_JOB` ZTCODE_STOCK_REC snapshot:
      - If BOTH `portal_row_keys` AND `latest_01_gi_records` are supplied and a `SAVED_TEMP` row has
        disappeared from BOTH, marks it `USER_SUBMITTED_CLEARED` (`cleared_detected_at = checked_at`).
      - Computes prior-saved summary (`saved_date < report_date_str`) for `[TEST]` + `07:30` D106 reports.
    """
    init_db(db_path=db_path)
    now_str = checked_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    ztcode_stock_rec_sigs: Optional[Dict[tuple, int]] = None
    if latest_01_gi_records is not None:
        ztcode_stock_rec_sigs = {}
        for rec in latest_01_gi_records:
            sig = (
                str(rec.get("material") or "").strip(),
                str(rec.get("mvt") or "").replace(".0", "").strip(),
                str(rec.get("from_sloc") or "").strip().upper(),
                str(rec.get("to_sloc") or "").strip().upper(),
                round(float(rec.get("qty") or 0.0), 3),
            )
            ztcode_stock_rec_sigs[sig] = ztcode_stock_rec_sigs.get(sig, 0) + 1

    newly_cleared_keys: List[str] = []
    with get_connection(db_path=db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if portal_row_keys is not None and ztcode_stock_rec_sigs is not None:
            cur.execute("SELECT * FROM d107_drafts WHERE status = 'SAVED_TEMP' ORDER BY id ASC")
            open_rows = [dict(r) for r in cur.fetchall()]
            for row in open_rows:
                rk = str(row["row_key"])
                sig = (
                    str(row["material"] or "").strip(),
                    str(row["mvt"] or "").replace(".0", "").strip(),
                    str(row["from_sloc"] or "").strip().upper(),
                    str(row["to_sloc"] or "").strip().upper(),
                    round(float(row["qty"] or 0.0), 3),
                )
                in_portal = rk in portal_row_keys
                in_ztcode_stock_rec = ztcode_stock_rec_sigs.get(sig, 0) > 0
                if not in_portal and not in_ztcode_stock_rec:
                    cur.execute(
                        """
                        UPDATE d107_drafts
                        SET status = 'USER_SUBMITTED_CLEARED',
                            cleared_detected_at = ?,
                            last_checked_at = ?
                        WHERE row_key = ?
                        """,
                        (now_str, now_str, rk),
                    )
                    newly_cleared_keys.append(rk)
                else:
                    cur.execute(
                        "UPDATE d107_drafts SET last_checked_at = ? WHERE row_key = ?",
                        (now_str, rk),
                    )
            conn.commit()

    return get_d107_draft_status_summary(
        report_date_str=report_date_str,
        newly_cleared_count=len(newly_cleared_keys),
        db_path=db_path,
    )


def get_d107_draft_status_summary(
    report_date_str: str,
    newly_cleared_count: int = 0,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Read-only summary of `d107_drafts` for both `07:23` (`[TEST]` email) and `07:30` (`D106` 1-line summary).
    """
    init_db(db_path=db_path)
    with get_connection(db_path=db_path) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM d107_drafts ORDER BY saved_at ASC, id ASC")
        all_rows = [dict(r) for r in cur.fetchall()]

    today_saved = [r for r in all_rows if str(r.get("saved_date") or "") == report_date_str]
    prior_rows = [r for r in all_rows if str(r.get("saved_date") or "") < report_date_str]
    prior_dates = sorted({str(r.get("saved_date") or "") for r in prior_rows if r.get("saved_date")})
    prior_cohort_date = prior_dates[-1] if prior_dates else None

    if prior_cohort_date:
        prior_cleared = [
            r for r in prior_rows
            if str(r.get("status") or "") == "USER_SUBMITTED_CLEARED"
            and (
                str(r.get("saved_date") or "") == prior_cohort_date
                or str(r.get("cleared_detected_at") or "")[:10] == report_date_str
            )
        ]
    else:
        prior_cleared = []

    prior_pending = [r for r in prior_rows if str(r.get("status") or "") == "SAVED_TEMP"]
    stale_labels: List[str] = []
    stale_rows: List[Dict[str, Any]] = []
    for pr in prior_pending:
        lbl, is_stale, bdays = format_d107_pending_or_stale_label(
            str(pr.get("saved_date") or pr.get("saved_at") or ""),
            report_date_str,
        )
        pr["pending_label"] = lbl
        pr["business_days_elapsed"] = bdays
        if is_stale:
            stale_rows.append(pr)
            if lbl not in stale_labels:
                stale_labels.append(lbl)

    return {
        "report_date": report_date_str,
        "today_saved_count": len(today_saved),
        "today_saved_rows": today_saved,
        "prior_cohort_date": prior_cohort_date,
        "prior_cleared_count": len(prior_cleared),
        "prior_pending_count": len(prior_pending),
        "prior_stale_count": len(stale_rows),
        "prior_stale_labels": stale_labels,
        "newly_cleared_count": int(newly_cleared_count),
        "prior_pending_rows": prior_pending,
    }


if __name__ == "__main__":
    init_db()
    print("DB initialized successfully at:", DB_PATH)
    postings = get_recent_postings(5)
    print("\nRecent postings in DB:")
    print(postings)
