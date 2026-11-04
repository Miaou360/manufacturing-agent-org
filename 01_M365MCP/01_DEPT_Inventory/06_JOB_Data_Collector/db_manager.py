"""
06_JOB_Data_Collector — ops_raw.db Database & Watermark / Movement Dictionary Manager
=====================================================================================
D104-1 ~ D104-5 / Thread 166, 167, 168:
- Single-writer WAL SQLite database (`06_JOB_Data_Collector/db/ops_raw.db`).
- All timestamps standardized to America/New_York (`EDT/EST`) local time strings (`YYYY-MM-DD HH:MM:SS`).
- Seeds `mvt_type_dict` with the 35 user-defined movement types (31 from Thread 167 + 261/262 + 901/902 from Thread 168).
- `qty_direction` (`+` / `-` / `BOTH`) is intentionally left NULL at seed time and populated from real MB51 sign/SLoc behavior.
- Unseen movement types encountered in MB51 are automatically registered with `definition_status='UNDEFINED'`.
- Includes blackout window detection (`is_in_blackout_window`) to avoid 01/00/02/04/05 SAP/WMS windows.
"""

import os
import sqlite3
import sys as _sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo
from dotenv import load_dotenv as _load_dotenv

_ENV_LOADED_PATH = None
for _ep in list(Path(__file__).resolve().parents) + [
    Path.home() / "Desktop" / "CODES" / "01_M365MCP",
    Path.home() / "Desktop" / "CODES",
]:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        _ENV_LOADED_PATH = _ep / ".env"
        break

_DUMMY_ENV_VALS = {
    "P001", "P002", "0000000", "Enterprise SSO",
    "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT",
}


def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val


def _verify_ops_env_log(job_name: str = "06_DATA_COLLECTOR") -> str:
    _req = (
        "SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME",
        "SAP_SSO_WINDOW_TITLE", "WMS_BASE_URL",
        "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT",
    )
    _st = []
    for _k in _req:
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg


BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
RAW_EXPORTS_DIR = BASE_DIR / "raw_exports"
SAMPLES_DIR = BASE_DIR / "_samples"
LOGS_DIR = BASE_DIR / "logs"

for _d in (DB_DIR, RAW_EXPORTS_DIR, SAMPLES_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_PATH = DB_DIR / "ops_raw.db"
EDT_TZ = ZoneInfo("America/New_York")


def now_edt_dt() -> datetime:
    return datetime.now(EDT_TZ)


def now_edt_str() -> str:
    return now_edt_dt().strftime("%Y-%m-%d %H:%M:%S")


def is_in_blackout_window(dt: Optional[datetime] = None) -> Tuple[bool, str]:
    """
    Checks whether `dt` (America/New_York) falls inside any operational SAP/WMS freeze window:
      - 01_JOB hourly/half-hourly runs: minute 58..59 or 00..06 every hour; minute 28..36 during 07:00..17:59
      - 05_JOB SL01 R-Bin scan: 03:45 .. 04:30
      - 02 / 04 / 00 morning peak: 05:55 .. 07:10 and 08:10 .. 08:25
    Returns (is_blackout, reason).
    """
    if dt is None:
        dt = now_edt_dt()
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=EDT_TZ)
    else:
        dt = dt.astimezone(EDT_TZ)

    h, m = dt.hour, dt.minute
    hm = h * 60 + m

    # Morning peak + 02/04 morning pass: 05:55 (355) ~ 08:25 (505)
    if 355 <= hm <= 505:
        return True, "MORNING_PEAK_00_01_02_04 (05:55~08:25 EDT)"
    # 05 dawn scan: 03:45 (225) ~ 04:30 (270)
    if 225 <= hm <= 270:
        return True, "DAWN_05_SCAN (03:45~04:30 EDT)"
    # Daytime 30-min 01_JOB cycles (08:00 ~ 17:05): allow launch ONLY in :05~:22 and :35~:52
    if 8 <= h <= 16 or (h == 17 and m <= 5):
        if m < 5 or m > 52:
            return True, "DAYTIME_HOURLY_01_CYCLE (:53~:04)"
        if 23 <= m <= 34:
            return True, "DAYTIME_HALF_HOURLY_01_CYCLE (:23~:34)"
        return False, "CLEAR"
    # Nighttime hourly 01_JOB cycles: block :53..:59 and :00..:06
    if m >= 53 or m <= 6:
        return True, "HOURLY_01_CYCLE (:53~:06)"

    return False, "CLEAR"


# =============================================================================
# Seed Movement Type Dictionary (D104-5 / Thread 167 + Thread 168)
# (mvt_code, mvt_name_raw, category_draft, reversal_pair)
# Note: qty_direction is intentionally None until verified against real MB51 data.
# =============================================================================
MVT_TYPE_SEED_ROWS: List[Tuple[str, str, str, Optional[str]]] = [
    # 입고 (Goods Receipt)
    ("101", "GR", "입고", "101↔102"),
    ("102", "GR Reversal", "입고", "101↔102"),
    ("122", "GR Cancelation", "입고 취소(반품 성격)", None),
    ("953", "Additional GR", "입고(추가)", "953↔954"),
    ("954", "953 Cancelation", "입고(추가)", "953↔954"),
    # 생산 소요 및 벌크 소요 (Production / Bulk Consumption)
    ("261", "생산 소요 (GI for Order)", "생산 소요", "261↔262"),
    ("262", "생산 소요 취소 (RE for Order)", "생산 소요", "261↔262"),
    ("929", "Bulk Consumption", "소요(벌크)", "929↔930"),
    ("930", "929 Cancelation", "소요(벌크)", "929↔930"),
    # 이동 및 대체 (Transfers & Substitutions)
    ("311", "Storage to Storage", "창고 간 이동", "311↔312"),
    ("312", "311 Reversal", "창고 간 이동", "311↔312"),
    ("301", "Plant to Plant", "플랜트 간 이동", "301↔302"),
    ("302", "301 Reverse", "플랜트 간 이동", "301↔302"),
    ("309", "Substitution", "품번 대체(품번 간 이동)", "309↔310"),
    ("310", "Substitution Reversal", "품번 대체(품번 간 이동)", "309↔310"),
    ("411", "Consignment > Standard", "소유 전환(위탁→자가)", "411↔412"),
    ("412", "411 Reversal", "소유 전환(위탁→자가)", "411↔412"),
    # 재고 상태 변경 / 품질 (Quality / Stock Status)
    ("343", "보류재고 → 가용재고", "재고 상태 변경(품질)", None),
    # 폐기 (Scrap / Disposal)
    ("553", "SL49 Scrap", "폐기(SL49)", "553↔554"),
    ("554", "553 Cancelation", "폐기(SL49)", "553↔554"),
    ("593", "Scrap - quality", "폐기(품질)", "593↔594"),
    ("594", "593 Calcelation", "폐기(품질)", "593↔594"),
    ("907", "Goods Disposal", "폐기(처분)", "907↔908"),
    ("908", "Reversal of 907", "폐기(처분)", "907↔908"),
    # 출하 및 매각 (Goods Issue / Delivery / Resell / Sale)
    ("601", "Goods Issue", "출하", "601↔602"),
    ("602", "Reversal of 601", "출하", "601↔602"),
    ("905", "Good Delivery - resell", "출하(재판매)", None),
    ("901", "매각 (Sale)", "출하(매각)", "901↔902"),
    ("902", "매각 취소 (Sale Reversal)", "출하(매각)", "901↔902"),
    # 샘플 및 개발 출고 (Sample / Development GI)
    ("921", "Sample Substitution", "샘플 출고", "921↔922"),
    ("922", "Reversal of 922", "샘플 출고", "921↔922"),
    ("923", "Sample Cost Substitution", "샘플 출고(비용)", "923↔924"),
    ("924", "Reversal of 923", "샘플 출고(비용)", "923↔924"),
    ("925", "Development I/O GI", "개발 출고", "925↔926"),
    ("926", "925 Cancelation", "개발 출고", "925↔926"),
]

# Thread 171: Claude draft classifications discovered from 10/27 MB51 sample (pending user confirmation)
MVT_TYPE_CLAUDE_DRAFT_ROWS: List[Tuple[str, str, str, Optional[str]]] = [
    ("131", "Goods receipt (REM/생산입고)", "입고(생산)", "131↔132"),
    ("132", "Goods receipt reversal (생산입고 취소)", "입고(생산)", "131↔132"),
    ("321", "TF quality to unrest (품질→가용 전환)", "재고 상태 변경(품질→가용)", "321↔322"),
]


def get_connection(db_path: Path = DEFAULT_DB_PATH, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    conn.row_factory = sqlite3.Row
    return conn


def init_ops_raw_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    """
    Initializes `ops_raw.db` schema (`mb51_raw`, `mvt_type_dict`, `collect_runs`, `collect_watermark`)
    and seeds the 35 user-listed movement types (`D104-5`).
    """
    ts_now = now_edt_str()
    conn = get_connection(db_path, read_only=False)
    try:
        with conn:
            # 1) mb51_raw: 23 standard MB51 layout columns + run metadata + dedup key (mat_doc_no, mat_doc_item, mat_doc_year, sloc, mvt_type, qty_in_unit_of_entry)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mb51_raw (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mat_doc_no TEXT NOT NULL,
                    mat_doc_item TEXT NOT NULL,
                    mat_doc_year TEXT NOT NULL,
                    posting_date TEXT NOT NULL,
                    entry_date TEXT,
                    entry_time TEXT,
                    entry_datetime_edt TEXT,
                    material TEXT NOT NULL,
                    material_desc TEXT,
                    plant TEXT,
                    sloc TEXT,
                    counter_sloc TEXT,
                    mvt_type TEXT NOT NULL,
                    mvt_type_text TEXT,
                    qty_entry REAL NOT NULL,
                    unit_entry TEXT,
                    amount_lc REAL,
                    currency TEXT,
                    doc_date TEXT,
                    username TEXT,
                    recipient TEXT,
                    reason_for_mvt TEXT,
                    po_number TEXT,
                    order_no TEXT,
                    cost_center TEXT,
                    vendor TEXT,
                    customer TEXT,
                    batch TEXT,
                    special_stock TEXT,
                    doc_header_text TEXT,
                    reference TEXT,
                    source_file TEXT,
                    collect_run_id TEXT NOT NULL,
                    collected_at_edt TEXT NOT NULL,
                    UNIQUE(mat_doc_no, mat_doc_item, mat_doc_year, sloc, mvt_type, qty_entry)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mb51_posting_date ON mb51_raw(posting_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mb51_entry_date ON mb51_raw(entry_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mb51_mat_date ON mb51_raw(material, posting_date);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_mb51_mvt_type ON mb51_raw(mvt_type);")

            # 2) mvt_type_dict: Movement Type Dictionary (D104-5 / Thread 167 + 168)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mvt_type_dict (
                    mvt_code TEXT PRIMARY KEY,
                    mvt_name_raw TEXT,
                    category_draft TEXT,
                    category_status TEXT NOT NULL DEFAULT 'DRAFT',
                    reversal_pair TEXT,
                    qty_direction TEXT,
                    definition_status TEXT NOT NULL DEFAULT 'USER_LISTED',
                    first_seen_date TEXT,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    created_at_edt TEXT NOT NULL,
                    updated_at_edt TEXT NOT NULL
                )
            """)

            # 3) collect_runs: Execution log per source & date window
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collect_runs (
                    run_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    target_date_from TEXT NOT NULL,
                    target_date_to TEXT NOT NULL,
                    date_basis TEXT NOT NULL DEFAULT 'ENTRY_DATE',
                    raw_file_path TEXT,
                    raw_row_count INTEGER NOT NULL DEFAULT 0,
                    inserted_row_count INTEGER NOT NULL DEFAULT 0,
                    dedup_skipped_count INTEGER NOT NULL DEFAULT 0,
                    new_undefined_mvt_count INTEGER NOT NULL DEFAULT 0,
                    elapsed_sec REAL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    started_at_edt TEXT NOT NULL,
                    finished_at_edt TEXT
                )
            """)

            # 4) collect_watermark: Per-source resume watermark (D104-3 / Thread 172 KST CPUDT & completed EDT date)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS collect_watermark (
                    source_name TEXT PRIMARY KEY,
                    backfill_start_date TEXT NOT NULL,
                    last_completed_date TEXT,
                    last_completed_cpudt_kst TEXT,
                    completed_edt_date TEXT,
                    last_run_id TEXT,
                    last_status TEXT,
                    updated_at_edt TEXT NOT NULL
                )
            """)
            for _col in ("last_completed_cpudt_kst", "completed_edt_date"):
                try:
                    conn.execute(f"ALTER TABLE collect_watermark ADD COLUMN {_col} TEXT;")
                except sqlite3.OperationalError:
                    pass

            # Seed mvt_type_dict (35 USER_LISTED rows, qty_direction=NULL)
            for m_code, m_name, cat_draft, rev_pair in MVT_TYPE_SEED_ROWS:
                conn.execute("""
                    INSERT INTO mvt_type_dict (
                        mvt_code, mvt_name_raw, category_draft, category_status,
                        reversal_pair, qty_direction, definition_status,
                        created_at_edt, updated_at_edt
                    ) VALUES (?, ?, ?, 'DRAFT', ?, NULL, 'USER_LISTED', ?, ?)
                    ON CONFLICT(mvt_code) DO UPDATE SET
                        mvt_name_raw = excluded.mvt_name_raw,
                        category_draft = excluded.category_draft,
                        reversal_pair = excluded.reversal_pair,
                        definition_status = 'USER_LISTED',
                        updated_at_edt = excluded.updated_at_edt
                """, (m_code, m_name, cat_draft, rev_pair, ts_now, ts_now))

            # Seed CLAUDE_DRAFT rows (131, 132, 321 from Thread 171)
            for m_code, m_name, cat_draft, rev_pair in MVT_TYPE_CLAUDE_DRAFT_ROWS:
                conn.execute("""
                    INSERT INTO mvt_type_dict (
                        mvt_code, mvt_name_raw, category_draft, category_status,
                        reversal_pair, qty_direction, definition_status,
                        created_at_edt, updated_at_edt
                    ) VALUES (?, ?, ?, 'CLAUDE_DRAFT', ?, NULL, 'CLAUDE_DRAFT', ?, ?)
                    ON CONFLICT(mvt_code) DO UPDATE SET
                        mvt_name_raw = excluded.mvt_name_raw,
                        category_draft = excluded.category_draft,
                        category_status = 'CLAUDE_DRAFT',
                        reversal_pair = excluded.reversal_pair,
                        definition_status = 'CLAUDE_DRAFT',
                        updated_at_edt = excluded.updated_at_edt
                """, (m_code, m_name, cat_draft, rev_pair, ts_now, ts_now))

            # Seed initial watermark for sap_mb51 starting at 2026-10-01 if not present
            conn.execute("""
                INSERT INTO collect_watermark (
                    source_name, backfill_start_date, last_completed_date,
                    last_run_id, last_status, updated_at_edt
                ) VALUES ('sap_mb51', '2026-10-01', NULL, NULL, 'INITIALIZED', ?)
                ON CONFLICT(source_name) DO NOTHING
            """, (ts_now,))
    finally:
        conn.close()


def ensure_mvt_type_registered(
    conn: sqlite3.Connection,
    mvt_code: str,
    seen_date: str,
    observed_sign: Optional[str] = None,
    sample_increment: int = 1,
    sap_mvt_text: Optional[str] = None,
) -> bool:
    """
    Checks if `mvt_code` exists in `mvt_type_dict`.
    If unseen, automatically inserts it with `definition_status='UNDEFINED'` so 03_JOB reports can surface it.
    Returns True if newly registered as UNDEFINED, False if already existed.
    """
    code_clean = str(mvt_code or "").strip()
    if not code_clean:
        return False
    ts_now = now_edt_str()
    row = conn.execute("SELECT mvt_code, qty_direction FROM mvt_type_dict WHERE mvt_code = ?", (code_clean,)).fetchone()
    if row is None:
        raw_lbl = f"미정의 이동유형 (자동감지: {sap_mvt_text})" if sap_mvt_text else "미정의 이동유형 (자동감지)"
        conn.execute("""
            INSERT INTO mvt_type_dict (
                mvt_code, mvt_name_raw, category_draft, category_status,
                reversal_pair, qty_direction, definition_status,
                first_seen_date, sample_count, created_at_edt, updated_at_edt
            ) VALUES (?, ?, 'UNDEFINED', 'UNDEFINED', NULL, ?, 'UNDEFINED', ?, ?, ?, ?)
        """, (code_clean, raw_lbl, observed_sign, seen_date, sample_increment, ts_now, ts_now))
        return True
    else:
        # Increment sample_count and optionally record qty_direction if verified from real data
        cur_dir = row["qty_direction"]
        new_dir = cur_dir
        if observed_sign in ("+", "-", "BOTH"):
            if cur_dir is None:
                new_dir = observed_sign
            elif cur_dir != observed_sign and cur_dir != "BOTH":
                new_dir = "BOTH"
        conn.execute("""
            UPDATE mvt_type_dict
            SET sample_count = sample_count + ?,
                first_seen_date = COALESCE(first_seen_date, ?),
                qty_direction = ?,
                updated_at_edt = ?
            WHERE mvt_code = ?
        """, (sample_increment, seen_date, new_dir, ts_now, code_clean))
        return False


def update_watermark(
    source_name: str,
    completed_date: str,
    run_id: str,
    status: str = "SUCCESS",
    db_path: Path = DEFAULT_DB_PATH,
) -> None:
    ts_now = now_edt_str()
    try:
        comp_edt = (datetime.strptime(completed_date, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        comp_edt = None
    conn = get_connection(db_path, read_only=False)
    try:
        with conn:
            conn.execute("""
                UPDATE collect_watermark
                SET last_completed_date = CASE
                        WHEN last_completed_date IS NULL OR ? > last_completed_date THEN ?
                        ELSE last_completed_date
                    END,
                    last_completed_cpudt_kst = CASE
                        WHEN last_completed_cpudt_kst IS NULL OR ? > last_completed_cpudt_kst THEN ?
                        ELSE last_completed_cpudt_kst
                    END,
                    completed_edt_date = CASE
                        WHEN last_completed_cpudt_kst IS NULL OR ? > last_completed_cpudt_kst THEN ?
                        ELSE completed_edt_date
                    END,
                    last_run_id = ?,
                    last_status = ?,
                    updated_at_edt = ?
                WHERE source_name = ?
            """, (
                completed_date, completed_date,
                completed_date, completed_date,
                completed_date, comp_edt,
                run_id, status, ts_now, source_name,
            ))
    finally:
        conn.close()


if __name__ == "__main__":
    init_ops_raw_db()
    c = get_connection(read_only=True)
    cnt = c.execute("SELECT COUNT(*) FROM mvt_type_dict").fetchone()[0]
    wm = dict(c.execute("SELECT * FROM collect_watermark WHERE source_name='sap_mb51'").fetchone())
    c.close()
    print(f"[06_JOB_Data_Collector] ops_raw.db initialized at {DEFAULT_DB_PATH} | mvt_type_dict={cnt} rows | watermark={wm}")

