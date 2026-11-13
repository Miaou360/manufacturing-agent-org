"""
Automated Data Retention & Disk Space Management Module (#236-B / #237-2)
=========================================================================
01_DEPT_Inventory (Manufacturing Site Plant P001)

Purpose:
- 대용량 WMS 및 SAP 엑셀 원본 파일(WMS_StockList, GR_List, GI_List, COGI, MB52 등)은
  대응하는 SQLite DB 원장에 **적재가 확인된 경우에만** 일정 기간(기본 7일) 경과 후 정리(Prune)합니다.
- 데이터 무결성 보장:
  1. SQLite DB 파일(*.db) 및 소스 코드, 설정 파일은 절대 삭제 대상에서 제외.
  2. 원본 파일의 날짜(파일명의 YYYYMMDD 또는 파일 최종 수정일)가 보존 기한을 초과했더라도,
     `FOLDER_LEDGER_MAP`의 대응 DB 원장에 해당 날짜/시각이 적재되지 않았으면 **절대 삭제하지 않고 보존**.
  3. 삭제된 파일 수, 미적재 보호(스킵) 파일 수 및 확보 용량(MB)을 집계하여 로깅.
"""

import os
import sys
import re
import sqlite3
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("StorageRetention")

BASE_DIR = Path(__file__).resolve().parent

# B-0 / B-4 emergency pause switch: set PAUSE_WMS_STOCKLIST_PRUNE=1 to freeze WMS_StockList_* deletion completely
PAUSE_WMS_STOCKLIST_PRUNE: bool = os.getenv("PAUSE_WMS_STOCKLIST_PRUNE", "0").strip().lower() in ("1", "true", "yes")

# Target directories to monitor and clean up (Raw Excel - 7 days retention, gated by FOLDER_LEDGER_MAP)
MANAGED_FOLDERS = [
    # 00_JOB_StockAccuracy stacks
    BASE_DIR / "00_JOB_StockAccuracy" / "gr_daily_stack",
    BASE_DIR / "00_JOB_StockAccuracy" / "gi_daily_stack",
    BASE_DIR / "00_JOB_StockAccuracy" / "missing_gi_daily_stack",
    BASE_DIR / "00_JOB_StockAccuracy" / "wms_daily_stack",
    # 01_JOB_GR_GI_Error dumps
    BASE_DIR / "01_JOB_GR_GI_Error" / "gi_errors",
    BASE_DIR / "01_JOB_GR_GI_Error" / "gr_errors",
    BASE_DIR / "01_JOB_GR_GI_Error" / "mb52",
    BASE_DIR / "01_JOB_GR_GI_Error" / "wms_parts_stack",
    # 02_JOB_Backlog_Clear dumps (raw_history is exempted for permanent audit retention)
    BASE_DIR / "02_JOB_Backlog_Clear" / "cogi_errors",
    BASE_DIR / "02_JOB_Backlog_Clear" / "mb52",
    # 04_JOB_SystemReconciliation dumps
    BASE_DIR / "04_JOB_SystemReconciliation" / "raw_exports",
    # 05_JOB_SL01_R_Bins reports
    BASE_DIR / "05_JOB_SL01_R_Bins" / "reports",
]

# Explicit folder -> authoritative SQLite ledger mapping (#237 Item 2 & #236 B-4).
# Every managed folder's expired file MUST satisfy ALL listed ledger checks before unlink().
# Supported check_type values:
#   - "date_equals":    SELECT 1 FROM <table> WHERE <column> = :yyyy_mm_dd [AND <extra_where>] LIMIT 1
#   - "ts_date_prefix": SELECT 1 FROM <table> WHERE <column> >= :day_start AND <column> <= :day_end LIMIT 1
#   - "batch_yyyymmdd": SELECT 1 FROM <table> WHERE <column> LIKE 'RUN_' || :yyyymmdd || '_%' [AND <extra_where>] LIMIT 1
#   - "watermark_ge":   SELECT 1 FROM <table> WHERE source_name = :source_name AND <column> >= :file_ts LIMIT 1
FOLDER_LEDGER_MAP: Dict[str, List[Dict[str, str]]] = {
    "00_JOB_StockAccuracy/wms_daily_stack": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/wms_barcode_daily.db",
            "table": "WMS_BARCODE_DAILY_RUNS",
            "column": "snapshot_date",
            "check_type": "date_equals",
            "extra_where": "row_count > 0",
        },
    ],
    "01_JOB_GR_GI_Error/wms_parts_stack": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/d114_stage_shadow.db",
            "table": "STAGE_WATERMARK",
            "column": "last_snapshot_ts",
            "check_type": "watermark_ge",
            "source_name": "wms_parts_stack",
        },
        {
            "db_rel": "00_JOB_StockAccuracy/db/wms_barcode_daily.db",
            "table": "WMS_BARCODE_DAILY_RUNS",
            "column": "snapshot_date",
            "check_type": "date_equals",
            "extra_where": "row_count > 0",
        },
    ],
    "01_JOB_GR_GI_Error/mb52": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/sap_stock_daily.db",
            "table": "SAP_STOCK_DAILY_RUNS",
            "column": "snapshot_date",
            "check_type": "date_equals",
            "extra_where": "row_count > 0",
        },
    ],
    "02_JOB_Backlog_Clear/mb52": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/sap_stock_daily.db",
            "table": "SAP_STOCK_DAILY_RUNS",
            "column": "snapshot_date",
            "check_type": "date_equals",
            "extra_where": "row_count > 0",
        },
    ],
    "00_JOB_StockAccuracy/gr_daily_stack": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/inventory_history.db",
            "table": "GR_TRANSACTIONS",
            "column": "gr_time",
            "check_type": "ts_date_prefix",
        },
    ],
    "00_JOB_StockAccuracy/gi_daily_stack": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/inventory_history.db",
            "table": "GI_TRANSACTIONS",
            "column": "gi_time",
            "check_type": "ts_date_prefix",
        },
    ],
    "00_JOB_StockAccuracy/missing_gi_daily_stack": [
        {
            "db_rel": "00_JOB_StockAccuracy/db/inventory_history.db",
            "table": "MISSING_GI_SNAPSHOT",
            "column": "end_date",
            "check_type": "date_equals",
        },
    ],
    "01_JOB_GR_GI_Error/gi_errors": [
        {
            "db_rel": "01_JOB_GR_GI_Error/db/gr_gi_history.db",
            "table": "pipeline_run_metrics",
            "column": "batch_id",
            "check_type": "batch_yyyymmdd",
            "extra_where": "status = 'COMPLETED'",
        },
    ],
    "01_JOB_GR_GI_Error/gr_errors": [
        {
            "db_rel": "01_JOB_GR_GI_Error/db/gr_gi_history.db",
            "table": "pipeline_run_metrics",
            "column": "batch_id",
            "check_type": "batch_yyyymmdd",
            "extra_where": "status = 'COMPLETED'",
        },
    ],
    "02_JOB_Backlog_Clear/cogi_errors": [
        {
            "db_rel": "02_JOB_Backlog_Clear/db/backlog_history.db",
            "table": "daily_backlog_metrics",
            "column": "run_date",
            "check_type": "date_equals",
        },
    ],
    "04_JOB_SystemReconciliation/raw_exports": [
        {
            "db_rel": "04_JOB_SystemReconciliation/db/system_reconciliation.db",
            "table": "accuracy_daily_summary",
            "column": "snapshot_date",
            "check_type": "date_equals",
        },
    ],
    "05_JOB_SL01_R_Bins/reports": [
        {
            "db_rel": "05_JOB_SL01_R_Bins/db/sl01_r_bins.db",
            "table": "SL01_R_BIN_SCAN_RUN",
            "column": "scan_date",
            "check_type": "date_equals",
        },
    ],
}

# Resolution plan logs for transfer audit trail (30 days retention per Claude Thread 056)
AUDIT_LOG_FOLDERS = [
    BASE_DIR / "01_JOB_GR_GI_Error" / "resolution_logs",
    BASE_DIR / "02_JOB_Backlog_Clear" / "resolution_logs",
]

# Scratch folders for temporary VBS/TXT/DRYRUN_* cleanup (24 hours retention; RECONSTRUCTED_* preserved)
SCRATCH_FOLDERS = [
    BASE_DIR / "00_JOB_StockAccuracy" / "scratch",
    BASE_DIR / "01_JOB_GR_GI_Error" / "scratch",
    BASE_DIR / "02_JOB_Backlog_Clear" / "scratch",
    BASE_DIR / "03_JOB_Backlog_Analysis" / "scratch",
    BASE_DIR / "04_JOB_SystemReconciliation" / "scratch",
    BASE_DIR / "05_JOB_SL01_R_Bins" / "scratch",
]

DEFAULT_RETENTION_DAYS = 7
AUDIT_LOG_RETENTION_DAYS = 30

_TS_FNAME_RE = re.compile(r"(202[4-9][0-1][0-9][0-3][0-9])_(\d{6})")


def extract_date_from_filename(filename: str) -> Optional[datetime]:
    """
    Attempts to parse YYYYMMDD date from common filename conventions.
    For files with date ranges (e.g. MissingGI_P001_20261018_20261024),
    returns the latest (end) date to prevent premature pruning.
    """
    matches = re.findall(r"(202[4-9][0-1][0-9][0-3][0-9])", filename)
    if matches:
        valid_dates = []
        for m in matches:
            try:
                valid_dates.append(datetime.strptime(m, "%Y%m%d"))
            except ValueError:
                pass
        if valid_dates:
            return max(valid_dates)
    return None


def extract_timestamp_from_filename(file_path: Path) -> str:
    """Returns `'YYYY-MM-DD HH:MM:SS'` from `*_YYYYMMDD_HHMMSS.*` or file date / mtime."""
    m = _TS_FNAME_RE.search(file_path.stem)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    d = extract_date_from_filename(file_path.name)
    if d is not None:
        return d.strftime("%Y-%m-%d 23:59:59")
    return datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_folder_key(folder_path: Path, base_dir: Path = BASE_DIR) -> Optional[str]:
    try:
        rel = folder_path.resolve().relative_to(base_dir.resolve()).as_posix()
        if rel in FOLDER_LEDGER_MAP:
            return rel
    except Exception:
        pass
    norm = folder_path.as_posix()
    for key in FOLDER_LEDGER_MAP:
        if norm.endswith(key):
            return key
    return None


def is_file_ingested_in_ledger(
    file_path: Path,
    file_date: datetime,
    ledger_specs: List[Dict[str, str]],
    base_dir: Path = BASE_DIR,
) -> Tuple[bool, str]:
    """
    Verifies via read-only SQLite connection (`?mode=ro`) that `file_path` / `file_date`
    has been persisted into EVERY configured target ledger in `ledger_specs`.
    Returns `(True, 'ok')` if verified, or `(False, reason)` to block deletion.
    """
    yyyy_mm_dd = file_date.strftime("%Y-%m-%d")
    yyyymmdd = file_date.strftime("%Y%m%d")
    file_ts = extract_timestamp_from_filename(file_path)

    for spec in ledger_specs:
        db_file = Path(base_dir) / spec["db_rel"]
        if not db_file.exists() or db_file.stat().st_size == 0:
            return False, f"ledger_db_missing:{spec['db_rel']}"

        table = spec["table"]
        col = spec["column"]
        ctype = spec["check_type"]
        extra = spec.get("extra_where", "").strip()
        extra_clause = f" AND ({extra})" if extra else ""

        try:
            conn = sqlite3.connect(f"file:{db_file.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
            try:
                if ctype == "date_equals":
                    sql = f"SELECT 1 FROM {table} WHERE {col} = ?{extra_clause} LIMIT 1"
                    row = conn.execute(sql, (yyyy_mm_dd,)).fetchone()
                elif ctype == "ts_date_prefix":
                    sql = f"SELECT 1 FROM {table} WHERE {col} >= ? AND {col} <= ?{extra_clause} LIMIT 1"
                    row = conn.execute(sql, (f"{yyyy_mm_dd} 00:00:00", f"{yyyy_mm_dd} 23:59:59")).fetchone()
                elif ctype == "batch_yyyymmdd":
                    sql = f"SELECT 1 FROM {table} WHERE {col} LIKE ?{extra_clause} LIMIT 1"
                    row = conn.execute(sql, (f"RUN_{yyyymmdd}_%",)).fetchone()
                elif ctype == "watermark_ge":
                    src = spec.get("source_name", "wms_parts_stack")
                    sql = f"SELECT 1 FROM {table} WHERE source_name = ? AND {col} >= ?{extra_clause} LIMIT 1"
                    row = conn.execute(sql, (src, file_ts)).fetchone()
                else:
                    return False, f"unknown_check_type:{ctype}"
            finally:
                conn.close()
        except Exception as exc:
            return False, f"ledger_query_error:{table}:{exc}"

        if row is None:
            return False, f"not_in_ledger:{table}({yyyy_mm_dd})"

    return True, "ok"


def prune_folder(
    folder_path: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    allowed_extensions: List[str] = None,
    dry_run: bool = False,
    base_dir: Path = BASE_DIR,
    ledger_specs_override: Optional[List[Dict[str, str]]] = None,
    pause_wms_stocklist: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Cleans up old files in a folder based on retention days AND SQLite ledger verification.
    Does not touch databases, markdown, python scripts, or any file not yet verified in its ledger DB.
    """
    if allowed_extensions is None:
        allowed_extensions = [".xlsx", ".xls", ".csv", ".tmp", ".txt"]

    allowed_ext_set = {ext.lower() for ext in allowed_extensions}

    if not folder_path.exists():
        return {
            "folder": str(folder_path),
            "scanned": 0,
            "deleted": 0,
            "skipped_uningested": 0,
            "freed_bytes": 0,
        }

    pause_wms = PAUSE_WMS_STOCKLIST_PRUNE if pause_wms_stocklist is None else bool(pause_wms_stocklist)
    folder_key = _resolve_folder_key(folder_path, base_dir=base_dir)
    ledger_specs = (
        ledger_specs_override
        if ledger_specs_override is not None
        else (FOLDER_LEDGER_MAP.get(folder_key) if folder_key else None)
    )

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    scanned = 0
    deleted = 0
    skipped_uningested = 0
    freed_bytes = 0

    for item in folder_path.iterdir():
        if not item.is_file():
            continue

        # Never prune canonical latest files, backfills, or historical archives
        item_lower = item.name.lower()
        if "latest" in item_lower or "backfill" in item_lower or "history" in item_lower or "archive" in item_lower:
            continue

        ext = item.suffix.lower()
        if ext not in allowed_ext_set:
            continue

        scanned += 1
        file_size = item.stat().st_size

        # Determine file date: preference given to date in filename, fallback to mtime
        file_date = extract_date_from_filename(item.name)
        if file_date is None:
            file_date = datetime.fromtimestamp(item.stat().st_mtime)

        if file_date < cutoff_date:
            # B-0 explicit pause guard for WMS_StockList_*
            if pause_wms and item_lower.startswith("wms_stocklist_"):
                skipped_uningested += 1
                logger.info(f"   ⏸️ [PAUSED] Skipped WMS_StockList file per pause guard: {item.name}")
                continue

            # #237-2 / #236-B-4: Verify file/date exists in authoritative DB ledger before deleting
            if ledger_specs:
                verified, reason = is_file_ingested_in_ledger(
                    file_path=item,
                    file_date=file_date,
                    ledger_specs=ledger_specs,
                    base_dir=base_dir,
                )
                if not verified:
                    skipped_uningested += 1
                    logger.warning(
                        f"   🛡️ [LEDGER GUARD] Kept expired file {item.name} ({file_date.strftime('%Y-%m-%d')}): {reason}"
                    )
                    continue

            if not dry_run:
                try:
                    item.unlink()
                    deleted += 1
                    freed_bytes += file_size
                    logger.info(
                        f"   🗑️ Deleted expired file ({file_date.strftime('%Y-%m-%d')}): {item.name} ({file_size / 1024 / 1024:.2f} MB)"
                    )
                except Exception as e:
                    logger.warning(f"   ⚠️ Could not delete {item.name}: {e}")
            else:
                deleted += 1
                freed_bytes += file_size
                logger.info(
                    f"   [DRY-RUN] Would delete ({file_date.strftime('%Y-%m-%d')}): {item.name} ({file_size / 1024 / 1024:.2f} MB)"
                )

    return {
        "folder": str(folder_path),
        "scanned": scanned,
        "deleted": deleted,
        "skipped_uningested": skipped_uningested,
        "freed_bytes": freed_bytes,
    }


def prune_scratch_artifacts(
    max_age_hours: int = 24,
    dry_run: bool = False
) -> Dict[str, Any]:
    """Cleans up temporary VBS scripts, txt markers, and test artifacts in scratch folders."""
    cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
    scratch_deleted = 0
    scratch_bytes = 0

    for s_folder in SCRATCH_FOLDERS:
        if not s_folder.exists():
            continue
        for item in s_folder.iterdir():
            if not item.is_file():
                continue
            # Never purge preserved RECONSTRUCTED_* audit artifacts
            if item.name.startswith("RECONSTRUCTED_"):
                continue
            # Clean VBS, generated TXT markers, temporary preview HTMLs, and DRYRUN_* files
            if item.suffix.lower() in [".vbs", ".txt", ".html"] or item.name.startswith("DRYRUN_"):
                mtime = datetime.fromtimestamp(item.stat().st_mtime)
                if mtime < cutoff_time:
                    size = item.stat().st_size
                    if not dry_run:
                        try:
                            item.unlink()
                            scratch_deleted += 1
                            scratch_bytes += size
                        except Exception:
                            pass
                    else:
                        scratch_deleted += 1
                        scratch_bytes += size

    return {"deleted": scratch_deleted, "freed_bytes": scratch_bytes}


def run_retention_pruning(
    retention_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Executes disk retention cleanup across all Inventory Team modules.
    Keeps files within retention_days, purges older files ONLY after verifying DB ledger ingestion.
    """
    logger.info("==========================================================================")
    logger.info(f" 🧹 [STORAGE RETENTION] Starting Cleanup (Retention: {retention_days} Days | Dry Run: {dry_run})...")
    logger.info("==========================================================================")

    total_scanned = 0
    total_deleted = 0
    total_skipped_uningested = 0
    total_freed_bytes = 0
    folder_results = []

    for folder in MANAGED_FOLDERS:
        if folder.exists():
            res = prune_folder(folder, retention_days=retention_days, dry_run=dry_run)
            total_scanned += res["scanned"]
            total_deleted += res["deleted"]
            total_skipped_uningested += res.get("skipped_uningested", 0)
            total_freed_bytes += res["freed_bytes"]
            folder_results.append(res)

    # Prune audit/resolution plan logs with 30 days retention (Audit Trail preservation; no DB ledger gate)
    for a_folder in AUDIT_LOG_FOLDERS:
        if a_folder.exists():
            res_a = prune_folder(
                a_folder,
                retention_days=AUDIT_LOG_RETENTION_DAYS,
                dry_run=dry_run,
                ledger_specs_override=[],
            )
            total_scanned += res_a["scanned"]
            total_deleted += res_a["deleted"]
            total_freed_bytes += res_a["freed_bytes"]
            folder_results.append(res_a)

    # Clean temporary scratch files older than 24 hours
    scratch_res = prune_scratch_artifacts(max_age_hours=24, dry_run=dry_run)
    total_deleted += scratch_res["deleted"]
    total_freed_bytes += scratch_res["freed_bytes"]

    freed_mb = total_freed_bytes / (1024 * 1024)
    logger.info("==========================================================================")
    logger.info(
        f" 🎉 [CLEANUP FINISHED] Scanned: {total_scanned} | Purged: {total_deleted} | "
        f"Protected(Uningested): {total_skipped_uningested} | Freed: {freed_mb:.2f} MB"
    )
    logger.info("==========================================================================")

    return {
        "retention_days": retention_days,
        "scanned_files": total_scanned,
        "purged_files": total_deleted,
        "skipped_uningested_files": total_skipped_uningested,
        "freed_mb": round(freed_mb, 2),
        "dry_run": dry_run,
        "folder_results": folder_results,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Clean up expired raw Excel files from Inventory Team directories.")
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS, help="Retention period in days (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate without deleting files")
    args = parser.parse_args()

    run_retention_pruning(retention_days=args.days, dry_run=args.dry_run)
