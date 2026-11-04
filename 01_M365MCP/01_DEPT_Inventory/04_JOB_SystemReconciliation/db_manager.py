"""
Database Manager for System Reconciliation (WMS vs SAP)
=======================================================
01_Inventory_Team / 04_JOB_SystemReconciliation
- SQLite 기반 로컬 DB (db/system_reconciliation.db)
- WAL 모드 적용 (고속 비차단 동시성 보장)
- 전일 마감 일일 재고정확도 집계, 품목별 상세 스냅샷, 만성 불일치(Ghost Inventory) 레지스트리 및
  AI 자동화 조치(MB1B 311)의 인과적 기여도(Attribution) 저장.
"""

import os
import sys
import sqlite3
import json
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
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "system_reconciliation.db"

logger = logging.getLogger("SystemReconciliation_DB")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Returns a SQLite connection with WAL mode enabled."""
    target_path = str(db_path or DB_PATH)
    conn = sqlite3.connect(target_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    return conn


def init_db(db_path: Optional[Path] = None):
    """Initializes tables and indexes for System Reconciliation."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Daily Accuracy Summary Table
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS accuracy_daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            posting_date TEXT NOT NULL,
            plant TEXT DEFAULT 'P001',
            system_group TEXT DEFAULT 'IWMS',
            total_items INTEGER NOT NULL,
            matched_items INTEGER NOT NULL,
            mismatched_items INTEGER NOT NULL,
            accuracy_rate REAL NOT NULL,
            avg_score REAL NOT NULL,
            total_diff_qty REAL NOT NULL,
            total_financial_exposure REAL NOT NULL,
            dod_accuracy_delta REAL DEFAULT 0.0,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(snapshot_date, plant, system_group)
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sum_snap ON accuracy_daily_summary(snapshot_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sum_post ON accuracy_daily_summary(posting_date);")

        # 2. Material Accuracy Snapshot (Daily line items)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS accuracy_material_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_date TEXT NOT NULL,
            posting_date TEXT NOT NULL,
            plant TEXT DEFAULT 'P001',
            system_group TEXT DEFAULT 'IWMS',
            material TEXT NOT NULL,
            material_type TEXT,
            description TEXT,
            wms_lgort TEXT,
            sap_lgort TEXT,
            wms_qty REAL NOT NULL,
            sap_qty REAL NOT NULL,
            diff_qty REAL NOT NULL,
            score REAL NOT NULL,
            net_price REAL DEFAULT 0.0,
            currency TEXT DEFAULT 'USD',
            price_unit REAL DEFAULT 1.0,
            financial_exposure REAL DEFAULT 0.0,
            update_date TEXT,
            update_time TEXT,
            captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(snapshot_date, plant, system_group, material, wms_lgort, sap_lgort)
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mat_snap ON accuracy_material_snapshot(snapshot_date);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mat_code ON accuracy_material_snapshot(material);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mat_diff ON accuracy_material_snapshot(diff_qty);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_mat_expo ON accuracy_material_snapshot(financial_exposure);")

        # 3. Chronic Mismatch Registry (Ghost Inventory Lifecycle)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS chronic_mismatch_registry (
            material TEXT PRIMARY KEY,
            material_type TEXT,
            description TEXT,
            plant TEXT DEFAULT 'P001',
            system_group TEXT DEFAULT 'IWMS',
            first_detected_date TEXT NOT NULL,
            last_detected_date TEXT NOT NULL,
            consecutive_days INTEGER DEFAULT 1,
            current_wms_qty REAL DEFAULT 0.0,
            current_sap_qty REAL DEFAULT 0.0,
            current_diff_qty REAL DEFAULT 0.0,
            current_financial_exposure REAL DEFAULT 0.0,
            status TEXT DEFAULT 'ACTIVE',
            recommended_for_audit BOOLEAN DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chr_status ON chronic_mismatch_registry(status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chr_days ON chronic_mismatch_registry(consecutive_days);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chr_audit ON chronic_mismatch_registry(recommended_for_audit);")

        # 4. AI Resolution Attribution Log
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_attribution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reconciliation_date TEXT NOT NULL,
            evaluated_items_count INTEGER DEFAULT 0,
            resolved_items_count INTEGER DEFAULT 0,
            unresolved_items_count INTEGER DEFAULT 0,
            resolution_success_rate REAL DEFAULT 0.0,
            accuracy_lift_pct REAL DEFAULT 0.0,
            details_json TEXT,
            evaluated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(reconciliation_date)
        );
        """)

        # 5. Continuous Learning Material Baseline Model (Cross-Time Statistical Profile)
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_material_baseline (
            material TEXT PRIMARY KEY,
            description TEXT,
            material_type TEXT,
            plant TEXT DEFAULT 'P001',
            system_group TEXT DEFAULT 'IWMS',
            sample_snapshots INTEGER NOT NULL,
            perfect_match_count INTEGER NOT NULL,
            mismatch_count INTEGER NOT NULL,
            match_rate REAL NOT NULL,
            avg_diff_qty REAL NOT NULL,
            median_diff_qty REAL NOT NULL,
            std_diff_qty REAL NOT NULL,
            avg_financial_exposure REAL NOT NULL,
            chronic_status TEXT NOT NULL,
            last_wms_qty REAL NOT NULL,
            last_sap_qty REAL NOT NULL,
            last_diff_qty REAL NOT NULL,
            last_posting_date TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_base_status ON reconciliation_material_baseline(chronic_status);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_base_match ON reconciliation_material_baseline(match_rate);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_base_expo ON reconciliation_material_baseline(avg_financial_exposure);")

        conn.commit()


def save_daily_snapshot(
    summary: Dict[str, Any],
    materials: List[Dict[str, Any]],
    db_path: Optional[Path] = None
) -> bool:
    """
    Saves daily summary, detailed items, and updates chronic mismatch registry in a single atomic transaction.
    """
    init_db(db_path)
    snapshot_date = summary["snapshot_date"]
    plant = summary.get("plant", _get_ops_env("SAP_PLANT", "P001"))
    system_group = summary.get("system_group", "IWMS")

    with get_connection(db_path) as conn:
        cursor = conn.cursor()

        # 1. Calculate Day-over-Day delta if prior summary exists
        cursor.execute("""
            SELECT accuracy_rate FROM accuracy_daily_summary
            WHERE snapshot_date < ? AND plant = ? AND system_group = ?
            ORDER BY snapshot_date DESC LIMIT 1
        """, (snapshot_date, plant, system_group))
        prev_row = cursor.fetchone()
        dod_delta = 0.0
        if prev_row:
            dod_delta = round(summary["accuracy_rate"] - prev_row["accuracy_rate"], 2)
        summary["dod_accuracy_delta"] = dod_delta

        # 2. Insert or Replace Summary
        cursor.execute("""
            INSERT OR REPLACE INTO accuracy_daily_summary (
                snapshot_date, posting_date, plant, system_group,
                total_items, matched_items, mismatched_items,
                accuracy_rate, avg_score, total_diff_qty,
                total_financial_exposure, dod_accuracy_delta, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            summary["snapshot_date"], summary["posting_date"], plant, system_group,
            summary["total_items"], summary["matched_items"], summary["mismatched_items"],
            summary["accuracy_rate"], summary["avg_score"], summary["total_diff_qty"],
            summary["total_financial_exposure"], dod_delta
        ))

        # 3. Bulk Insert Material Snapshots
        mat_tuples = []
        mismatched_materials = {}
        for m in materials:
            mat_tuples.append((
                snapshot_date, summary["posting_date"], plant, system_group,
                m["material"], m.get("material_type", ""), m.get("description", ""),
                m.get("wms_lgort", ""), m.get("sap_lgort", ""),
                m["wms_qty"], m["sap_qty"], m["diff_qty"], m["score"],
                m.get("net_price", 0.0), m.get("currency", "USD"), m.get("price_unit", 1.0),
                m.get("financial_exposure", 0.0),
                m.get("update_date", ""), m.get("update_time", "")
            ))
            if m["diff_qty"] != 0:
                mismatched_materials[m["material"]] = m

        cursor.executemany("""
            INSERT OR REPLACE INTO accuracy_material_snapshot (
                snapshot_date, posting_date, plant, system_group,
                material, material_type, description,
                wms_lgort, sap_lgort, wms_qty, sap_qty, diff_qty, score,
                net_price, currency, price_unit, financial_exposure,
                update_date, update_time, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, mat_tuples)

        # 4. Update Chronic Mismatch Registry
        # Fetch current active records
        cursor.execute("SELECT material, first_detected_date, consecutive_days FROM chronic_mismatch_registry WHERE status = 'ACTIVE'")
        active_registry = {r["material"]: (r["first_detected_date"], r["consecutive_days"]) for r in cursor.fetchall()}

        for mat, item in mismatched_materials.items():
            # Exclude consignment raw materials (ROH2/ROH3, 74-*, 89-*) from chronic registry
            m_type = item.get("material_type", "")
            if m_type in ("ROH2", "ROH3") or mat.startswith("74-") or mat.startswith("89-"):
                continue

            if mat in active_registry:
                first_date, prev_days = active_registry[mat]
                new_days = prev_days + 1
                rec_audit = 1 if new_days >= 3 else 0
                cursor.execute("""
                    UPDATE chronic_mismatch_registry SET
                        last_detected_date = ?,
                        consecutive_days = ?,
                        current_wms_qty = ?,
                        current_sap_qty = ?,
                        current_diff_qty = ?,
                        current_financial_exposure = ?,
                        recommended_for_audit = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE material = ?
                """, (
                    snapshot_date, new_days, item["wms_qty"], item["sap_qty"],
                    item["diff_qty"], item.get("financial_exposure", 0.0), rec_audit, mat
                ))
            else:
                cursor.execute("""
                    INSERT OR REPLACE INTO chronic_mismatch_registry (
                        material, material_type, description, plant, system_group,
                        first_detected_date, last_detected_date, consecutive_days,
                        current_wms_qty, current_sap_qty, current_diff_qty,
                        current_financial_exposure, status, recommended_for_audit, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, 'ACTIVE', 0, CURRENT_TIMESTAMP)
                """, (
                    mat, item.get("material_type", ""), item.get("description", ""),
                    plant, system_group, snapshot_date, snapshot_date,
                    item["wms_qty"], item["sap_qty"], item["diff_qty"],
                    item.get("financial_exposure", 0.0)
                ))

        # Check for resolved items (previously active, now diff == 0)
        for mat in active_registry:
            if mat not in mismatched_materials:
                cursor.execute("""
                    UPDATE chronic_mismatch_registry SET
                        status = 'RESOLVED',
                        recommended_for_audit = 0,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE material = ?
                """, (mat,))

        conn.commit()

        # 5. Continuous Learning: Automatically update baseline statistical model
        try:
            update_reconciliation_baseline(plant=plant, system_group=system_group, db_path=db_path)
        except Exception as e_base:
            logger.warning(f"Continuous learning baseline update failed: {e_base}")

        return True


# ==========================================
# Query APIs for Inter-Job Synergies
# ==========================================

def get_latest_summary(plant: str = _get_ops_env("SAP_PLANT", "P001"), system_group: str = "IWMS") -> Optional[Dict[str, Any]]:
    """Returns the latest daily summary metrics."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM accuracy_daily_summary
            WHERE plant = ? AND system_group = ?
            ORDER BY snapshot_date DESC LIMIT 1
        """, (plant, system_group))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_top_financial_risks(snapshot_date: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Returns TOP N materials with highest financial discrepancy (diff_qty * net_price).
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        if snapshot_date is None:
            cursor.execute("SELECT MAX(snapshot_date) as max_date FROM accuracy_daily_summary")
            r = cursor.fetchone()
            snapshot_date = r["max_date"] if r else None

        if not snapshot_date:
            return []

        cursor.execute("""
            SELECT material, description, material_type, wms_lgort, sap_lgort,
                   wms_qty, sap_qty, diff_qty, score, net_price, financial_exposure
            FROM accuracy_material_snapshot
            WHERE snapshot_date = ? AND diff_qty != 0
              AND material_type NOT IN ('ROH2', 'ROH3')
              AND material NOT LIKE '74-%'
              AND material NOT LIKE '89-%'
            ORDER BY financial_exposure DESC LIMIT ?
        """, (snapshot_date, limit))
        return [dict(r) for r in cursor.fetchall()]


def get_all_discrepancies(snapshot_date: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns all mismatched items (diff_qty != 0) sorted by financial exposure descending."""
    with get_connection() as conn:
        cursor = conn.cursor()
        if snapshot_date is None:
            cursor.execute("SELECT MAX(snapshot_date) as max_date FROM accuracy_daily_summary")
            r = cursor.fetchone()
            snapshot_date = r["max_date"] if r else None

        if not snapshot_date:
            return []

        cursor.execute("""
            SELECT material, description, material_type, wms_lgort, sap_lgort,
                   wms_qty, sap_qty, diff_qty, score, net_price, financial_exposure
            FROM accuracy_material_snapshot
            WHERE snapshot_date = ? AND diff_qty != 0
              AND material_type NOT IN ('ROH2', 'ROH3')
              AND material NOT LIKE '74-%'
              AND material NOT LIKE '89-%'
            ORDER BY financial_exposure DESC
        """, (snapshot_date,))
        return [dict(r) for r in cursor.fetchall()]


def get_chronic_ghost_items(min_days: int = 3) -> List[Dict[str, Any]]:
    """
    SYNERGY WITH 00_JOB_StockAccuracy:
    Returns materials persistently mismatched for min_days or more.
    These are the highest priority candidates for physical floor cycle counting.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT material, material_type, description, first_detected_date,
                   last_detected_date, consecutive_days, current_wms_qty,
                   current_sap_qty, current_diff_qty, current_financial_exposure
            FROM chronic_mismatch_registry
            WHERE status = 'ACTIVE' AND consecutive_days >= ?
              AND material_type NOT IN ('ROH2', 'ROH3')
              AND material NOT LIKE '74-%'
              AND material NOT LIKE '89-%'
            ORDER BY consecutive_days DESC, current_financial_exposure DESC
        """, (min_days,))
        return [dict(r) for r in cursor.fetchall()]


def get_material_accuracy_profile(material: str) -> Optional[Dict[str, Any]]:
    """
    SYNERGY WITH 03_JOB_Backlog_Analysis:
    Returns the latest WMS vs SAP accuracy status for a specific material code.
    Helps diagnose whether a backlog item is a true shortage, systemic transfer lag, or phantom stock.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT snapshot_date, posting_date, material, description, wms_lgort, sap_lgort,
                   wms_qty, sap_qty, diff_qty, score, net_price, financial_exposure
            FROM accuracy_material_snapshot
            WHERE material = ?
            ORDER BY snapshot_date DESC LIMIT 1
        """, (material.strip(),))
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        # Diagnosis tagging
        wms = d["wms_qty"]
        sap = d["sap_qty"]
        diff = d["diff_qty"]
        if wms > 0 and sap == 0:
            d["diagnosis"] = "SYSTEMIC_TRANSFER_LAG"  # Parts exist in WMS, need MB1B 311 to line
            d["action"] = "AI_AUTO_311_TARGET"
        elif wms == 0 and sap == 0:
            d["diagnosis"] = "TRUE_PHYSICAL_SHORTAGE"  # Zero in plant
            d["action"] = "ESCALATE_BUYER_VENDOR"
        elif wms == 0 and sap > 0:
            d["diagnosis"] = "PHANTOM_STOCK_RISK"  # SAP believes stock exists, physical is 0
            d["action"] = "URGENT_LINE_STOP_ALERT"
        else:
            d["diagnosis"] = "NORMAL_OR_BALANCED"
            d["action"] = "MONITOR"
        return d


def record_ai_attribution(
    reconciliation_date: str,
    evaluated_count: int,
    resolved_count: int,
    accuracy_lift_pct: float,
    details: Dict[str, Any]
):
    """Saves AI Attribution evaluation to DB."""
    with get_connection() as conn:
        cursor = conn.cursor()
        unresolved = max(0, evaluated_count - resolved_count)
        rate = round((resolved_count / evaluated_count * 100), 2) if evaluated_count > 0 else 0.0
        cursor.execute("""
            INSERT OR REPLACE INTO ai_attribution_log (
                reconciliation_date, evaluated_items_count, resolved_items_count,
                unresolved_items_count, resolution_success_rate, accuracy_lift_pct,
                details_json, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            reconciliation_date, evaluated_count, resolved_count, unresolved,
            rate, accuracy_lift_pct, json.dumps(details, ensure_ascii=False)
        ))
        conn.commit()


# ==========================================
# Continuous Learning Baseline Model Engine
# ==========================================

def update_reconciliation_baseline(
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    system_group: str = "IWMS",
    db_path: Optional[Path] = None
) -> int:
    """
    Continuous Learning Model:
    Scans all historical snapshots in accuracy_material_snapshot for the given plant/system_group,
    computes multi-snapshot distribution metrics (mean, median, standard deviation, match rate,
    chronic risk tier), and updates reconciliation_material_baseline.
    Called automatically after every daily snapshot and backfill ingestion.
    """
    import math
    from collections import defaultdict

    init_db(db_path)
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT material, material_type, description, wms_qty, sap_qty, diff_qty,
                   financial_exposure, posting_date
            FROM accuracy_material_snapshot
            WHERE plant = ? AND system_group = ?
              AND material_type NOT IN ('ROH2', 'ROH3')
              AND material NOT LIKE '74-%'
              AND material NOT LIKE '89-%'
            ORDER BY material, posting_date ASC
        """, (plant, system_group))
        rows = cursor.fetchall()
        if not rows:
            return 0

        material_data = defaultdict(list)
        for r in rows:
            material_data[r["material"]].append(r)

        baseline_records = []
        for mat, items in material_data.items():
            sample_count = len(items)
            perfect_count = sum(1 for it in items if it["diff_qty"] == 0)
            mismatch_count = sample_count - perfect_count
            match_rate = round((perfect_count / sample_count) * 100.0, 2)

            diffs = [it["diff_qty"] for it in items]
            avg_diff = round(sum(diffs) / sample_count, 2)

            sorted_diffs = sorted(diffs)
            mid = sample_count // 2
            if sample_count % 2 == 1:
                median_diff = round(sorted_diffs[mid], 2)
            else:
                median_diff = round((sorted_diffs[mid - 1] + sorted_diffs[mid]) / 2.0, 2)

            variance = sum((d - avg_diff) ** 2 for d in diffs) / sample_count
            std_diff = round(math.sqrt(variance), 2)

            exposures = [it["financial_exposure"] for it in items]
            avg_exposure = round(sum(exposures) / sample_count, 2)

            last_item = items[-1]
            desc = last_item["description"] or ""
            m_type = last_item["material_type"] or ""

            # 5-Tier Chronic Classification
            if sample_count >= 2:
                if match_rate >= 95.0:
                    status = "CHRONIC_PERFECT"
                elif avg_diff <= -5.0 and (mismatch_count / sample_count) >= 0.6:
                    status = "CHRONIC_GHOST_DEFICIT"  # SAP > WMS consistently (phantom stock in SAP / unposted scrap)
                elif avg_diff >= 5.0 and (mismatch_count / sample_count) >= 0.6:
                    status = "CHRONIC_OVER_STOCK"     # WMS > SAP consistently (unposted GR or transfer lag)
                elif std_diff >= 15.0 or ((mismatch_count / sample_count) >= 0.4 and std_diff >= 8.0):
                    status = "VOLATILE_FLUCTUATING"   # Erratic variance across snapshots
                else:
                    status = "SPORADIC_DISCREPANCY"   # Minor or occasional timing differences
            else:
                status = "CHRONIC_PERFECT" if last_item["diff_qty"] == 0 else "SPORADIC_DISCREPANCY"

            baseline_records.append((
                mat, desc, m_type, plant, system_group,
                sample_count, perfect_count, mismatch_count, match_rate,
                avg_diff, median_diff, std_diff, avg_exposure,
                status, last_item["wms_qty"], last_item["sap_qty"],
                last_item["diff_qty"], last_item["posting_date"]
            ))

        cursor.executemany("""
            INSERT OR REPLACE INTO reconciliation_material_baseline (
                material, description, material_type, plant, system_group,
                sample_snapshots, perfect_match_count, mismatch_count, match_rate,
                avg_diff_qty, median_diff_qty, std_diff_qty, avg_financial_exposure,
                chronic_status, last_wms_qty, last_sap_qty, last_diff_qty,
                last_posting_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, baseline_records)
        conn.commit()

        logger.info(f"Updated baseline model for {len(baseline_records):,} materials ({plant}/{system_group}).")
        return len(baseline_records)


def get_baseline_summary(plant: str = _get_ops_env("SAP_PLANT", "P001"), system_group: str = "IWMS") -> Dict[str, Any]:
    """Returns high-level statistics of the continuous learning baseline model."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as total_materials,
                SUM(CASE WHEN chronic_status = 'CHRONIC_PERFECT' THEN 1 ELSE 0 END) as perfect_count,
                SUM(CASE WHEN chronic_status = 'CHRONIC_GHOST_DEFICIT' THEN 1 ELSE 0 END) as ghost_deficit_count,
                SUM(CASE WHEN chronic_status = 'CHRONIC_OVER_STOCK' THEN 1 ELSE 0 END) as over_stock_count,
                SUM(CASE WHEN chronic_status = 'VOLATILE_FLUCTUATING' THEN 1 ELSE 0 END) as volatile_count,
                SUM(CASE WHEN chronic_status = 'SPORADIC_DISCREPANCY' THEN 1 ELSE 0 END) as sporadic_count,
                ROUND(AVG(match_rate), 2) as overall_match_rate,
                MAX(sample_snapshots) as max_snapshots_analyzed
            FROM reconciliation_material_baseline
            WHERE plant = ? AND system_group = ?
        """, (plant, system_group))
        row = cursor.fetchone()
        return dict(row) if row else {}


def get_chronic_ghost_from_baseline(
    limit: int = 10,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    system_group: str = "IWMS"
) -> List[Dict[str, Any]]:
    """Returns top chronic ghost deficit materials from the trained baseline model."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT material, description, material_type, sample_snapshots,
                   match_rate, avg_diff_qty, std_diff_qty, avg_financial_exposure,
                   chronic_status, last_wms_qty, last_sap_qty, last_diff_qty, last_posting_date
            FROM reconciliation_material_baseline
            WHERE plant = ? AND system_group = ? AND chronic_status = 'CHRONIC_GHOST_DEFICIT'
              AND material_type NOT IN ('ROH2', 'ROH3')
              AND material NOT LIKE '74-%'
              AND material NOT LIKE '89-%'
            ORDER BY avg_financial_exposure DESC LIMIT ?
        """, (plant, system_group, limit))
        return [dict(r) for r in cursor.fetchall()]


def get_material_baseline(
    material: str,
    plant: str = _get_ops_env("SAP_PLANT", "P001"),
    system_group: str = "IWMS"
) -> Optional[Dict[str, Any]]:
    """Returns the baseline learning profile for a specific material."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM reconciliation_material_baseline
            WHERE material = ? AND plant = ? AND system_group = ?
        """, (material.strip(), plant, system_group))
        row = cursor.fetchone()
        return dict(row) if row else None
