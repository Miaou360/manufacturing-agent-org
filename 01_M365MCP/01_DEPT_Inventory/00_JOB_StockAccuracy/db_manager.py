"""
Database Manager (01_Inventory_Team / 00_StockAccuracy)
SQLite 기반 WMS 이력(Stock List, GR, GI) 적재 및 바코드 수명주기, 온톨로지 관리 모듈
"""

import sqlite3
import datetime
import logging
import json
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
        if _k == "OEM_FLOOR_BIN_PREFIX":
            _fb = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "").strip()
            if _fb and _fb not in _DUMMY_ENV_VALS:
                _st.append("FLOOR_BUFFER_BIN_PREFIXES=configured")
                continue
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from typing import Optional, List, Dict, Any, Tuple
import pandas as pd
import numpy as np

logger = logging.getLogger("StockAccuracyDB")

BASE_DIR = Path(__file__).resolve().parent
DB_DIR = BASE_DIR / "db"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "inventory_history.db"

ONTOLOGY_DIR = BASE_DIR / "ontology"
ONTOLOGY_DIR.mkdir(parents=True, exist_ok=True)
ONTOLOGY_JSON_PATH = ONTOLOGY_DIR / "material_turnover_ontology.json"

GR_STACK_DIR = BASE_DIR / "gr_daily_stack"
GR_STACK_DIR.mkdir(parents=True, exist_ok=True)

GI_STACK_DIR = BASE_DIR / "gi_daily_stack"
GI_STACK_DIR.mkdir(parents=True, exist_ok=True)


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
    except Exception:
        pass
    return conn


def init_database(db_path: Optional[Path] = None):
    """데이터베이스 테이블 및 인덱스 초기화"""
    conn = get_connection(db_path)
    cur = conn.cursor()

    # 1. GR Transactions (입고 이력)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS GR_TRANSACTIONS (
        barcode TEXT PRIMARY KEY,
        mat_no TEXT,
        mat_desc TEXT,
        gr_loc TEXT,
        gr_time TEXT,
        gr_qty REAL,
        unit TEXT,
        move_type TEXT,
        vendor_name TEXT,
        bin TEXT,
        trans_no TEXT,
        invoice_no TEXT,
        skid TEXT,
        source_file TEXT,
        created_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gr_mat ON GR_TRANSACTIONS(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gr_time ON GR_TRANSACTIONS(gr_time)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gr_move ON GR_TRANSACTIONS(move_type)")

    # 2. GI Transactions (출고 및 이동 이력)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS GI_TRANSACTIONS (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT,
        mat_no TEXT,
        mat_desc TEXT,
        gi_loc TEXT,
        gr_loc TEXT,
        gi_time TEXT,
        qty REAL,
        unit TEXT,
        move_type TEXT,
        lot_no TEXT,
        vendor_code TEXT,
        trans_no TEXT,
        source_file TEXT,
        created_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gi_barcode ON GI_TRANSACTIONS(barcode)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gi_mat ON GI_TRANSACTIONS(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gi_time ON GI_TRANSACTIONS(gi_time)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_gi_dest ON GI_TRANSACTIONS(gr_loc)")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_gi_trans ON GI_TRANSACTIONS(barcode, trans_no)")

    # 3. Barcode Lifecycle (바코드별 입출고 종합 수명주기)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS BARCODE_LIFECYCLE (
        barcode TEXT PRIMARY KEY,
        mat_no TEXT,
        mat_desc TEXT,
        gr_time TEXT,
        gr_loc TEXT,
        production_gi_time TEXT,
        production_gi_loc TEXT,
        dwell_hours REAL,
        dwell_days REAL,
        status TEXT,
        lot_no TEXT,
        is_orphan INTEGER DEFAULT 0,
        updated_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bl_mat ON BARCODE_LIFECYCLE(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bl_status ON BARCODE_LIFECYCLE(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bl_orphan ON BARCODE_LIFECYCLE(is_orphan)")

    # 4. Material Ontology (품번별 온톨로지 지식 마스터)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS MATERIAL_ONTOLOGY (
        mat_no TEXT PRIMARY KEY,
        mat_desc TEXT,
        total_boxes INTEGER,
        produced_boxes INTEGER,
        orphan_boxes INTEGER,
        orphan_rate REAL,
        mean_turnover_days REAL,
        trimmed_mean_days REAL,
        median_turnover_days REAL,
        min_turnover_days REAL,
        max_turnover_days REAL,
        std_turnover_days REAL,
        mover_class TEXT,
        is_small_part INTEGER DEFAULT 0,
        hand_pick_risk INTEGER DEFAULT 0,
        updated_at TEXT
    )
    """)

    # 5. Missing GI Snapshots (생산 소모 vs 라인 출고 차이 및 유령재고 스냅샷)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS MISSING_GI_SNAPSHOT (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT,
        start_date TEXT,
        end_date TEXT,
        plant TEXT DEFAULT 'P001',
        mat_no TEXT,
        mat_clean TEXT,
        mat_desc TEXT,
        vendor_name TEXT,
        project TEXT,
        prod_bom REAL,
        gi_qty REAL,
        diff_qty REAL,
        diff_box REAL,
        diff_flag TEXT,
        target_bins TEXT,
        source_file TEXT,
        created_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgi_mat ON MISSING_GI_SNAPSHOT(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgi_clean ON MISSING_GI_SNAPSHOT(mat_clean)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgi_snap ON MISSING_GI_SNAPSHOT(snapshot_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgi_flag ON MISSING_GI_SNAPSHOT(diff_flag)")

    # 6. Missing GI Baseline (출고/포장 단위 상시 편차 학습 및 이상치 탐지 기준선 마스터)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS MISSING_GI_BASELINE (
        mat_clean TEXT PRIMARY KEY,
        mat_no TEXT,
        mat_desc TEXT,
        sample_weeks INTEGER,
        avg_diff_box REAL,
        median_diff_box REAL,
        std_diff_box REAL,
        is_unit_offset_item INTEGER DEFAULT 0,
        chronic_under_rate REAL DEFAULT 0.0,
        chronic_over_rate REAL DEFAULT 0.0,
        classification TEXT DEFAULT 'NORMAL_SYNCHRONIZED',
        updated_at TEXT
    )
    """)
    try:
        cur.execute("ALTER TABLE MISSING_GI_BASELINE ADD COLUMN chronic_under_rate REAL DEFAULT 0.0")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE MISSING_GI_BASELINE ADD COLUMN chronic_over_rate REAL DEFAULT 0.0")
    except Exception:
        pass
    try:
        cur.execute("ALTER TABLE MISSING_GI_BASELINE ADD COLUMN classification TEXT DEFAULT 'NORMAL_SYNCHRONIZED'")
    except Exception:
        pass

    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgib_clean ON MISSING_GI_BASELINE(mat_clean)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgib_offset ON MISSING_GI_BASELINE(is_unit_offset_item)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mgib_class ON MISSING_GI_BASELINE(classification)")

    # 7. Barcode Bin Stay (WMS 일일 재고 스냅샷 기반 빈 체류일수 역추적 마스터)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS BARCODE_BIN_STAY (
        barcode TEXT PRIMARY KEY,
        mat_no TEXT,
        current_bin TEXT,
        first_seen_in_bin TEXT,
        last_seen_date TEXT,
        consecutive_days_in_bin INTEGER DEFAULT 1,
        gr_date TEXT,
        warehouse_dwell_days INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        updated_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bbs_bin ON BARCODE_BIN_STAY(current_bin)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bbs_mat ON BARCODE_BIN_STAY(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bbs_active ON BARCODE_BIN_STAY(is_active)")

    # 8. Audit Run Master (실사 회차 마스터)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_RUN (
        run_id TEXT PRIMARY KEY,
        run_date TEXT NOT NULL,
        mode TEXT NOT NULL,
        scoring_version TEXT NOT NULL,
        wms_snapshot_file TEXT,
        config_hash TEXT,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_date ON AUDIT_RUN(run_date)")

    # 9. Audit Assignment Ledger (실사 지시 원장)
    # [NOTE: Future Audit Results Mapping]
    # 향후 현장 작업자의 실물 카운팅 실적, 오차 수량, 조치 결과 및 소요시간을 기록하는
    # AUDIT_RESULT 테이블은 본 테이블의 assignment_id를 외래키(FK)로 1:N 또는 1:1 조인 연계됩니다.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_ASSIGNMENT (
        assignment_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        bin TEXT NOT NULL,
        rank INTEGER NOT NULL,
        list_type TEXT NOT NULL,
        selection_method TEXT NOT NULL,
        bin_risk_score REAL NOT NULL,
        action_required TEXT NOT NULL,
        primary_reason TEXT,
        materials_json TEXT,
        label_qty INTEGER NOT NULL,
        plant TEXT DEFAULT 'P001',
        tier INTEGER,
        inclusion_probability REAL DEFAULT 1.0,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_aa_run ON AUDIT_ASSIGNMENT(run_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_aa_bin ON AUDIT_ASSIGNMENT(bin)")

    # Migration: Add issued and feature_snapshot_quality to AUDIT_RUN if table already exists
    cur.execute("PRAGMA table_info(AUDIT_RUN)")
    ar_run_cols = [r[1] for r in cur.fetchall()]
    if "issued" not in ar_run_cols:
        cur.execute("ALTER TABLE AUDIT_RUN ADD COLUMN issued INTEGER DEFAULT 0")
    if "feature_snapshot_quality" not in ar_run_cols:
        cur.execute("ALTER TABLE AUDIT_RUN ADD COLUMN feature_snapshot_quality TEXT DEFAULT 'REALTIME'")
    if "audit_started_at" not in ar_run_cols:
        cur.execute("ALTER TABLE AUDIT_RUN ADD COLUMN audit_started_at TEXT")

    # Migration: Add inclusion_probability and label_qty_basis to AUDIT_ASSIGNMENT if table already exists
    cur.execute("PRAGMA table_info(AUDIT_ASSIGNMENT)")
    aa_cols = [r[1] for r in cur.fetchall()]
    if "inclusion_probability" not in aa_cols:
        cur.execute("ALTER TABLE AUDIT_ASSIGNMENT ADD COLUMN inclusion_probability REAL DEFAULT 1.0")
    if "label_qty_basis" not in aa_cols:
        cur.execute("ALTER TABLE AUDIT_ASSIGNMENT ADD COLUMN label_qty_basis TEXT DEFAULT 'SUSPECT_BARCODES'")
    if "wms_barcodes_json" not in aa_cols:
        cur.execute("ALTER TABLE AUDIT_ASSIGNMENT ADD COLUMN wms_barcodes_json TEXT")

    # 10. Audit Feature Snapshot (시점 피처 스냅샷 - 미선정 후보 포함 전수 보존)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_FEATURE_SNAPSHOT (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        barcode TEXT NOT NULL,
        mat_no TEXT NOT NULL,
        bin TEXT NOT NULL,
        plant TEXT DEFAULT 'P001',
        tier INTEGER,
        qty REAL,
        gr_date TEXT,
        warehouse_dwell_days INTEGER DEFAULT 0,
        bin_dwell_days INTEGER DEFAULT 1,
        lot_age_days INTEGER,
        ontology_turnover_days REAL,
        mover_class TEXT,
        missing_gi_flag TEXT,
        missing_gi_diff_box REAL,
        fifo_bypass_gap_days INTEGER,
        same_bin_lot_spread_days INTEGER,
        risk_score INTEGER,
        risk_categories TEXT,
        score_breakdown_json TEXT,
        ztcode_stock_rec_diff_qty REAL DEFAULT 0.0,
        ztcode_stock_rec_consecutive_days INTEGER DEFAULT 0,
        ztcode_stock_rec_exposure REAL DEFAULT 0.0,
        net_price REAL,
        p_error_proxy REAL,
        expected_loss REAL,
        est_minutes REAL,
        priority_ev REAL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_afs_run ON AUDIT_FEATURE_SNAPSHOT(run_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_afs_mat ON AUDIT_FEATURE_SNAPSHOT(mat_no)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_afs_bin ON AUDIT_FEATURE_SNAPSHOT(bin)")

    # Migration: Add ztcode_stock_rec and shadow EV columns if table already exists
    cur.execute("PRAGMA table_info(AUDIT_FEATURE_SNAPSHOT)")
    afs_cols = [r[1] for r in cur.fetchall()]
    if "ztcode_stock_rec_diff_qty" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN ztcode_stock_rec_diff_qty REAL DEFAULT 0.0")
    if "ztcode_stock_rec_consecutive_days" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN ztcode_stock_rec_consecutive_days INTEGER DEFAULT 0")
    if "ztcode_stock_rec_exposure" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN ztcode_stock_rec_exposure REAL DEFAULT 0.0")
    for s_col in ["net_price", "p_error_proxy", "expected_loss", "est_minutes", "priority_ev"]:
        if s_col not in afs_cols:
            cur.execute(f"ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN {s_col} REAL")
    if "chain_gap_flag" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN chain_gap_flag INTEGER DEFAULT 0")
    if "chain_gap_days" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN chain_gap_days INTEGER DEFAULT 0")
    if "cooldown_excluded" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN cooldown_excluded INTEGER DEFAULT 0")
    if "cooldown_reason" not in afs_cols:
        cur.execute("ALTER TABLE AUDIT_FEATURE_SNAPSHOT ADD COLUMN cooldown_reason TEXT")

    # 11. WMS Stock Daily Aggregation (Bin x Material 압축 일일 재고 스냅샷)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS WMS_STOCK_DAILY_AGG (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_date TEXT NOT NULL,
        bin TEXT NOT NULL,
        mat_no TEXT NOT NULL,
        box_count INTEGER NOT NULL,
        total_qty REAL NOT NULL,
        oldest_gr_date TEXT,
        newest_gr_date TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(snapshot_date, bin, mat_no)
    )
    """)
    # 12. Audit Result Master (실사 결과 실적 원장 - 1:N Recount 지원)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_RESULT (
        result_id INTEGER PRIMARY KEY AUTOINCREMENT,
        assignment_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        bin TEXT NOT NULL,
        mat_no TEXT,
        counted_at TEXT,
        book_qty_at_count REAL NOT NULL,
        counted_qty REAL,
        diff_qty REAL,
        diff_amount REAL,
        cause_code TEXT,
        minutes_spent REAL,
        counter_id TEXT,
        is_recount INTEGER DEFAULT 0,
        is_confirmed INTEGER DEFAULT 1,
        audit_origin TEXT DEFAULT 'PHYSICAL_CYCLE_COUNT',
        data_origin TEXT DEFAULT 'PHYSICAL',
        wms_adjustment_status TEXT DEFAULT 'NOT_ADJUSTED',
        in_flight_gr_qty REAL,
        in_flight_gi_qty REAL,
        in_flight_adjusted INTEGER DEFAULT 0,
        effective_book_qty REAL,
        notes TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(assignment_id) REFERENCES AUDIT_ASSIGNMENT(assignment_id)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_assign ON AUDIT_RESULT(assignment_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_bin ON AUDIT_RESULT(bin)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_run ON AUDIT_RESULT(run_id)")
    # Migration: Add columns to AUDIT_RESULT if table already exists
    cur.execute("PRAGMA table_info(AUDIT_RESULT)")
    ar_cols = [r[1] for r in cur.fetchall()]
    for col_name, col_type in [
        ("audit_origin", "TEXT DEFAULT 'PHYSICAL_CYCLE_COUNT'"),
        ("data_origin", "TEXT DEFAULT 'PHYSICAL'"),
        ("wms_adjustment_status", "TEXT DEFAULT 'NOT_ADJUSTED'"),
        ("in_flight_gr_qty", "REAL DEFAULT 0.0"),
        ("in_flight_gi_qty", "REAL DEFAULT 0.0"),
        ("in_flight_adjusted", "INTEGER DEFAULT 0"),
        ("effective_book_qty", "REAL"),
        ("is_discrepancy", "INTEGER NULL"),
        ("label_source", "TEXT NULL"),
        ("field_notes", "TEXT NULL"),
        ("is_discrepancy_vs_label", "INTEGER NULL"),
        ("wms_boxes_at_count", "REAL NULL"),
        ("is_discrepancy_at_count", "INTEGER NULL"),
        ("timing_diff_excluded", "INTEGER DEFAULT 0")
    ]:
        if col_name not in ar_cols:
            try:
                cur.execute(f"ALTER TABLE AUDIT_RESULT ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass

    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_cause ON AUDIT_RESULT(cause_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_time ON AUDIT_RESULT(counted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_origin ON AUDIT_RESULT(audit_origin)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_data_origin ON AUDIT_RESULT(data_origin)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ar_discrepancy ON AUDIT_RESULT(is_discrepancy)")

    # 13. Audit Hit Rate Daily (실사 리스트 타율 및 적중률 추이 원장)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_HIT_RATE_DAILY (
        run_id TEXT NOT NULL,
        audit_date TEXT NOT NULL,
        calculated_at TEXT NOT NULL,
        scoring_version TEXT,
        sample_size_top INTEGER DEFAULT 0,
        discrepancy_count_top INTEGER DEFAULT 0,
        hit_rate_top REAL,
        sample_size_extended INTEGER DEFAULT 0,
        discrepancy_count_extended INTEGER DEFAULT 0,
        hit_rate_extended REAL,
        sample_size_model INTEGER DEFAULT 0,
        discrepancy_count_model INTEGER DEFAULT 0,
        hit_rate_model REAL,
        sample_size_random_28d INTEGER DEFAULT 0,
        discrepancy_count_random_28d INTEGER DEFAULT 0,
        hit_rate_random_28d REAL,
        lift REAL,
        rank_1_5_sample INTEGER DEFAULT 0,
        rank_1_5_discrepancies INTEGER DEFAULT 0,
        rank_1_5_hit_rate REAL,
        rank_6_15_sample INTEGER DEFAULT 0,
        rank_6_15_discrepancies INTEGER DEFAULT 0,
        rank_6_15_hit_rate REAL,
        rank_16_40_sample INTEGER DEFAULT 0,
        rank_16_40_discrepancies INTEGER DEFAULT 0,
        rank_16_40_hit_rate REAL,
        cumulative_7d_hit_rate REAL,
        cumulative_28d_hit_rate REAL,
        notes TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (run_id, calculated_at)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ahr_run ON AUDIT_HIT_RATE_DAILY(run_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ahr_date ON AUDIT_HIT_RATE_DAILY(audit_date)")

    # 14. Audit Rule Hit Rate (D74 Stage 1: 규칙 및 선정 사유별 적중률 원장)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_RULE_HIT_RATE (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        rule_code TEXT NOT NULL,
        rule_name TEXT NOT NULL,
        category TEXT NOT NULL,
        sample_size INTEGER NOT NULL,
        discrepancy_count INTEGER NOT NULL,
        hit_rate REAL,
        smoothed_score REAL,
        calculated_at TEXT NOT NULL,
        UNIQUE(run_id, rule_code)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_arhr_run ON AUDIT_RULE_HIT_RATE(run_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_arhr_rule ON AUDIT_RULE_HIT_RATE(rule_code)")

    # 15. Audit Historical Discrepancy Score (D74 Stage 1: 품번 및 빈별 과거 불일치 이력 및 베타 스무딩 점수)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS AUDIT_HISTORICAL_DISCREPANCY_SCORE (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        audit_count INTEGER NOT NULL,
        discrepancy_count INTEGER NOT NULL,
        raw_hit_rate REAL NOT NULL,
        smoothed_score REAL NOT NULL,
        last_audit_date TEXT,
        last_run_id TEXT,
        last_discrepancy INTEGER,
        associated_bins_json TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(entity_type, entity_id)
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ahds_type_id ON AUDIT_HISTORICAL_DISCREPANCY_SCORE(entity_type, entity_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ahds_score ON AUDIT_HISTORICAL_DISCREPANCY_SCORE(smoothed_score DESC)")

    # 16. Zone Turnover Stats (D112 #217 / D114 #212: zone -> window_days & sample_n lookup)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS ZONE_TURNOVER_STATS (
        zone_code TEXT PRIMARY KEY,
        stage_label TEXT,
        median_dwell_days REAL,
        window_days INTEGER,
        sample_n INTEGER NOT NULL DEFAULT 0,
        min_sample_n INTEGER NOT NULL DEFAULT 30,
        source TEXT NOT NULL DEFAULT 'FALLBACK_PRE_D114',
        updated_at TEXT
    )
    """)
    _now_seed = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.executemany(
        """
        INSERT OR IGNORE INTO ZONE_TURNOVER_STATS (
            zone_code, stage_label, median_dwell_days, window_days, sample_n, min_sample_n, source, updated_at
        ) VALUES (?, ?, ?, ?, 0, 30, 'FALLBACK_PRE_D114', ?)
        """,
        [
            ("S1", "Plant 3 Storage (R*)", None, None, _now_seed),
            ("S2", "Plant 1 Tier 3-5 Storage", None, None, _now_seed),
            ("S3", "Plant 1 Tier 1-2 Picking", None, None, _now_seed),
            ("S4", "Line Buffer (SMT / Floor)", None, None, _now_seed),
            ("S5", "Repack Supply Carts (CRT / PRO)", None, 0, _now_seed),
            ("EXCLUDED", "Excluded Racks (S* / SCR / RESIN / Virtual)", None, 0, _now_seed),
        ],
    )

    conn.commit()
    conn.close()


def ingest_gr_excel(file_path: Path) -> int:
    """GR List 엑셀 파일을 읽어 SQLite DB에 UPSERT 적재"""
    df = pd.read_excel(file_path)
    if "Barcode" not in df.columns:
        return 0

    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    inserted = 0
    for _, row in df.iterrows():
        barcode = str(row.get("Barcode", "")).strip()
        if not barcode or barcode == "nan":
            continue

        mat_no = str(row.get("Mat.No", "")).strip()
        mat_desc = str(row.get("Mat.Desc", "")).strip()
        gr_loc = str(row.get("GR Loc", "")).strip()
        gr_time = str(row.get("GR Time", "")).strip()
        gr_qty = float(row.get("GR Qty", 0.0) or 0.0)
        unit = str(row.get("Unit", "EA")).strip()
        move_type = str(row.get("Move.Type", "101")).strip()
        vendor_name = str(row.get("Vendor Name", "")).strip()
        bin_loc = str(row.get("BIN", "")).strip()
        trans_no = str(row.get("Trans.No", "")).strip()
        invoice_no = str(row.get("INVOICE_NO", "")).strip()
        skid = str(row.get("SKID", "")).strip()

        cur.execute("""
        INSERT INTO GR_TRANSACTIONS (
            barcode, mat_no, mat_desc, gr_loc, gr_time, gr_qty, unit,
            move_type, vendor_name, bin, trans_no, invoice_no, skid,
            source_file, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            gr_time = excluded.gr_time,
            move_type = excluded.move_type,
            gr_loc = excluded.gr_loc,
            source_file = excluded.source_file
        """, (
            barcode, mat_no, mat_desc, gr_loc, gr_time, gr_qty, unit,
            move_type, vendor_name, bin_loc, trans_no, invoice_no, skid,
            file_path.name, now_str
        ))
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def ingest_gi_excel(file_path: Path) -> int:
    """GI List 엑셀 파일을 읽어 SQLite DB에 적재"""
    df = pd.read_excel(file_path)
    if "Barcode" not in df.columns:
        return 0

    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    inserted = 0
    records = []
    for _, row in df.iterrows():
        barcode = str(row.get("Barcode", "")).strip()
        if not barcode or barcode == "nan":
            continue

        records.append((
            barcode,
            str(row.get("Mat.No", "")).strip(),
            str(row.get("Mat.Desc", "")).strip(),
            str(row.get("GI Loc", "")).strip(),
            str(row.get("GR Loc", "")).strip(),
            str(row.get("GI Time", "")).strip(),
            float(row.get("Qty", 0.0) or 0.0),
            str(row.get("Unit", "EA")).strip(),
            str(row.get("Move.Type", "311")).strip(),
            str(row.get("Lot No", "")).strip(),
            str(row.get("Vendor Code", "")).strip(),
            str(row.get("Trans.No", "")).strip(),
            file_path.name,
            now_str
        ))
        inserted += 1

    cur.executemany("""
    INSERT OR IGNORE INTO GI_TRANSACTIONS (
        barcode, mat_no, mat_desc, gi_loc, gr_loc, gi_time, qty, unit,
        move_type, lot_no, vendor_code, trans_no, source_file, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)

    conn.commit()
    conn.close()
    return inserted


def is_file_ingested(page_type: str, filename: str) -> bool:
    """해당 엑셀 파일이 DB에 이미 적재되었는지 확인"""
    conn = get_connection()
    cur = conn.cursor()
    table = "GR_TRANSACTIONS" if page_type == "GR" else "GI_TRANSACTIONS"
    row = cur.execute(f"SELECT 1 FROM {table} WHERE source_file = ? LIMIT 1", (filename,)).fetchone()
    conn.close()
    return row is not None


def ingest_missing_gi_excel(file_path: Path, plant: str = _get_ops_env("SAP_PLANT", "P001")) -> int:
    """Missing GI 엑셀 파일을 읽어 SQLite DB의 MISSING_GI_SNAPSHOT 테이블에 적재"""
    df = pd.read_excel(file_path)
    if "Mat.No" not in df.columns or "DIFF_FLAG" not in df.columns:
        return 0

    conn = get_connection()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 파일명에서 날짜 범위 추출 시도 (예: MissingGI_P001_20261016_20261023.xlsx)
    import re
    m = _re_out_guard.search(r"(\d{8})_(\d{8})", file_path.name)
    if m:
        s_raw, e_raw = m.group(1), m.group(2)
        start_date = f"{s_raw[:4]}-{s_raw[4:6]}-{s_raw[6:]}"
        end_date = f"{e_raw[:4]}-{e_raw[4:6]}-{e_raw[6:]}"
        snapshot_date = end_date
    else:
        start_date = datetime.date.today().strftime("%Y-%m-%d")
        end_date = datetime.date.today().strftime("%Y-%m-%d")
        snapshot_date = end_date

    # 기존 동일 파일 또는 동일 기간(start_date, end_date) 기록 삭제 (Idempotent 갱신)
    cur.execute("""
        DELETE FROM MISSING_GI_SNAPSHOT 
        WHERE source_file = ? OR (plant = ? AND start_date = ? AND end_date = ?)
    """, (file_path.name, plant, start_date, end_date))

    # #1 ~ #9 열들 추출
    bin_cols = [c for c in df.columns if str(c).startswith("#") and str(c)[1:].isdigit()]

    records = []
    for _, row in df.iterrows():
        mat_no = str(row.get("Mat.No", "")).strip()
        if not mat_no or mat_no == "nan":
            continue
        mat_clean = mat_no.lstrip("0")
        mat_desc = str(row.get("Mat.Desc", "")).strip()
        vendor_name = str(row.get("Vendor Name", "")).strip()
        project = str(row.get("Project", "") or row.get("ProjectCd", "")).strip()

        try:
            prod_bom = float(row.get("PROD_BOM", 0.0) or 0.0)
        except Exception:
            prod_bom = 0.0
        try:
            gi_qty = float(row.get("Qty.", 0.0) or 0.0)
        except Exception:
            gi_qty = 0.0
        try:
            diff_qty = float(row.get("Diff", 0.0) or 0.0)
        except Exception:
            diff_qty = 0.0
        try:
            diff_box = float(row.get("DIFF_BOX", 0.0) or 0.0)
        except Exception:
            diff_box = 0.0

        diff_flag = str(row.get("DIFF_FLAG", "")).strip().upper()

        # #1 ~ #9 빈들 모으기
        target_bins_list = []
        for bc in bin_cols:
            val = str(row.get(bc, "")).strip()
            if val and val != "nan":
                target_bins_list.append(val)
        target_bins = ", ".join(target_bins_list)

        records.append((
            snapshot_date, start_date, end_date, plant,
            mat_no, mat_clean, mat_desc, vendor_name, project,
            prod_bom, gi_qty, diff_qty, diff_box, diff_flag,
            target_bins, file_path.name, now_str
        ))

    cur.executemany("""
    INSERT INTO MISSING_GI_SNAPSHOT (
        snapshot_date, start_date, end_date, plant,
        mat_no, mat_clean, mat_desc, vendor_name, project,
        prod_bom, gi_qty, diff_qty, diff_box, diff_flag,
        target_bins, source_file, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, records)

    conn.commit()
    conn.close()

    # 스냅샷 적재 후 베이스라인 자동 갱신
    try:
        update_missing_gi_baseline()
    except Exception as e:
        print(f"[DB Warning] 베이스라인 갱신 경고: {e}")

    return len(records)


def update_missing_gi_baseline(conn: Optional[sqlite3.Connection] = None) -> int:
    """
    MISSING_GI_SNAPSHOT에 적재된 다수 주간(Weekly) 데이터를 기반으로
    각 품목의 상시 출고/포장 단위 편차(Baseline Offset)를 통계적으로 학습·갱신합니다.
    - sample_weeks >= 2 주간 이상 관측된 품목 분석
    - std_diff_box <= 2.0 (변동폭이 안정적임)
    - abs(median_diff_box) >= 1.0 (상시 일정 박스 편차가 지속됨)
      ➔ is_unit_offset_item = 1 로 분류하여 오탐(False Positive) 방지
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        df = pd.read_sql_query("""
            SELECT mat_clean, mat_no, mat_desc, prod_bom, gi_qty, diff_box, diff_flag, snapshot_date
            FROM MISSING_GI_SNAPSHOT
            WHERE mat_clean IS NOT NULL AND mat_clean != ''
        """, conn)

        if df.empty:
            return 0

        grouped = df.groupby("mat_clean")
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        baseline_records = []

        for mat_clean, group in grouped:
            mat_no = group["mat_no"].iloc[0]
            mat_desc = group["mat_desc"].iloc[0]
            sample_weeks = int(group["snapshot_date"].nunique())
            diff_boxes = group["diff_box"].dropna().values

            if len(diff_boxes) == 0:
                continue

            avg_diff = round(float(np.mean(diff_boxes)), 2)
            med_diff = round(float(np.median(diff_boxes)), 2)
            std_diff = round(float(np.std(diff_boxes)), 2) if len(diff_boxes) > 1 else 0.0

            # 비율 계산
            flags = group["diff_flag"].fillna("").str.upper()
            under_count = int((flags == "UNDER").sum())
            over_count = int((flags == "OVER").sum())
            chronic_under_rate = round(float(under_count / sample_weeks), 2)
            chronic_over_rate = round(float(over_count / sample_weeks), 2)

            # 도메인 다차원 분류 규칙:
            # 1. 만성 미출고 품목 (Chronic Unscanned Line Issue):
            #    주간의 80% 이상이 UNDER 결손이며, 중앙값이 1.0박스 이상 -> 라인 직투입 무스캔 프로세스 누수
            if sample_weeks >= 3 and chronic_under_rate >= 0.8 and med_diff >= 1.0:
                classification = "CHRONIC_UNSCANNED_LINE_ISSUE"
                is_unit_offset = 0
            # 2. 상시 과출고 품목 (Chronic Over-Issued):
            #    주간의 80% 이상이 OVER 출고 -> 후열 적치/방치 의심
            elif sample_weeks >= 3 and chronic_over_rate >= 0.8 and med_diff <= -1.0:
                classification = "CHRONIC_OVER_ISSUED"
                is_unit_offset = 0
            # 3. 상시 포장단위 편차 (Packaging Unit Offset):
            #    주간별 편차 표준편차가 1.0박스 이하로 매우 안정적이며 중앙값이 1박스 이상 차이남 -> SNP 단위 차이
            elif sample_weeks >= 3 and std_diff <= 1.0 and abs(med_diff) >= 1.0:
                classification = "UNIT_PACK_OFFSET_NORMAL"
                is_unit_offset = 1
            # 4. 급성 변동 결손 고위험 품목 (Sporadic Volatile Deficit):
            #    만성적이지 않으나 특정 주간에 급격한 결손 편차가 발생 -> 유령재고/오스캔 진성 의심군 (실사 1순위)
            elif std_diff > 1.5 and sample_weeks >= 2 and chronic_under_rate < 0.8 and chronic_over_rate < 0.8:
                classification = "SPORADIC_VOLATILE_DEFICIT"
                is_unit_offset = 0
            else:
                classification = "NORMAL_SYNCHRONIZED"
                is_unit_offset = 0

            baseline_records.append((
                mat_clean, mat_no, mat_desc, sample_weeks,
                avg_diff, med_diff, std_diff, is_unit_offset,
                chronic_under_rate, chronic_over_rate, classification, now_str
            ))

        cur = conn.cursor()
        cur.executemany("""
        INSERT INTO MISSING_GI_BASELINE (
            mat_clean, mat_no, mat_desc, sample_weeks,
            avg_diff_box, median_diff_box, std_diff_box,
            is_unit_offset_item, chronic_under_rate, chronic_over_rate,
            classification, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(mat_clean) DO UPDATE SET
            mat_no = excluded.mat_no,
            mat_desc = excluded.mat_desc,
            sample_weeks = excluded.sample_weeks,
            avg_diff_box = excluded.avg_diff_box,
            median_diff_box = excluded.median_diff_box,
            std_diff_box = excluded.std_diff_box,
            is_unit_offset_item = excluded.is_unit_offset_item,
            chronic_under_rate = excluded.chronic_under_rate,
            chronic_over_rate = excluded.chronic_over_rate,
            classification = excluded.classification,
            updated_at = excluded.updated_at
        """, baseline_records)

        conn.commit()
        return len(baseline_records)
    finally:
        if close_conn:
            conn.close()


def get_latest_missing_gi_map(plant: str = _get_ops_env("SAP_PLANT", "P001")) -> Dict[str, Dict[str, Any]]:
    """가장 최근 적재된 Missing GI 스냅샷을 품번 기준 dict로 반환하며 학습된 Baseline Offset 결합"""
    conn = get_connection()
    cur = conn.cursor()
    # 최신 snapshot_date 조회
    row = cur.execute("SELECT MAX(snapshot_date) FROM MISSING_GI_SNAPSHOT WHERE plant = ?", (plant,)).fetchone()
    if not row or not row[0]:
        conn.close()
        return {}

    latest_date = row[0]
    rows = cur.execute("""
        SELECT s.mat_no, s.mat_clean, s.mat_desc, s.vendor_name, s.project,
               s.prod_bom, s.gi_qty, s.diff_qty, s.diff_box, s.diff_flag, s.target_bins,
               COALESCE(b.median_diff_box, 0.0) AS baseline_diff_box,
               COALESCE(b.is_unit_offset_item, 0) AS is_unit_offset_item,
               COALESCE(b.std_diff_box, 0.0) AS baseline_std_box,
               COALESCE(b.sample_weeks, 0) AS baseline_sample_weeks
        FROM MISSING_GI_SNAPSHOT s
        LEFT JOIN MISSING_GI_BASELINE b ON s.mat_clean = b.mat_clean
        WHERE s.plant = ? AND s.snapshot_date = ?
    """, (plant, latest_date)).fetchall()
    conn.close()

    result = {}
    for r in rows:
        diff_box = r["diff_box"] or 0.0
        baseline_diff = r["baseline_diff_box"] or 0.0
        anomaly_diff_box = round(diff_box - baseline_diff, 2)

        item = {
            "mat_no": r["mat_no"],
            "mat_clean": r["mat_clean"],
            "mat_desc": r["mat_desc"],
            "vendor_name": r["vendor_name"],
            "project": r["project"],
            "prod_bom": r["prod_bom"],
            "gi_qty": r["gi_qty"],
            "diff_qty": r["diff_qty"],
            "diff_box": diff_box,
            "diff_flag": r["diff_flag"],
            "target_bins": r["target_bins"],
            "baseline_diff_box": baseline_diff,
            "is_unit_offset_item": bool(r["is_unit_offset_item"]),
            "baseline_std_box": r["baseline_std_box"],
            "baseline_sample_weeks": r["baseline_sample_weeks"],
            "anomaly_diff_box": anomaly_diff_box
        }
        result[r["mat_no"]] = item
        result[r["mat_clean"]] = item

    return result


def get_wtd_date_range(ref_date: Optional[datetime.date] = None) -> Tuple[datetime.date, datetime.date]:
    """
    일요일을 한 주의 시작으로 하는 주간 누적(WTD: Week-to-Date) 시작일과 종료일 계산.
    예: 2026-10-23(금) -> start: 2026-10-18(일), end: 2026-10-23(금)
    """
    target = ref_date or datetime.date.today()
    # Python weekday(): 월=0, 화=1, ..., 일=6
    # 일요일 기준 경과 일수: (target.weekday() + 1) % 7
    days_since_sunday = (target.weekday() + 1) % 7
    start_sunday = target - datetime.timedelta(days=days_since_sunday)
    return start_sunday, target


def update_barcode_bin_stay(df: pd.DataFrame, snapshot_date: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> int:
    """
    WMS Stock List 스냅샷을 기반으로 각 바코드의 동일 Bin 연속 체류일수(consecutive_days_in_bin) 및
    창고 총 체류일수(warehouse_dwell_days = Today - GR Date)를 업데이트합니다.
    """
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        snap_date_str = snapshot_date or datetime.date.today().strftime("%Y-%m-%d")
        snap_dt = pd.to_datetime(snap_date_str)
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur = conn.cursor()

        # Identify required columns
        barcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), "Barcode")
        bin_col = next((c for c in df.columns if c.strip().lower() == "bin"), "BIN")
        mat_col = next((c for c in df.columns if "mat" in c.lower() and "no" in c.lower()), "Mat.No")
        gr_col = next((c for c in df.columns if "gr" in c.lower() and "date" in c.lower()), "GR Date")

        clean_df = df.dropna(subset=[barcode_col]).copy()
        clean_df["b_code"] = clean_df[barcode_col].astype(str).str.strip()
        clean_df["m_no"] = clean_df[mat_col].astype(str).str.strip()
        clean_df["b_bin"] = clean_df[bin_col].astype(str).str.strip().str.upper()

        if gr_col in clean_df.columns:
            gr_dt = pd.to_datetime(clean_df[gr_col].astype(str), format="%Y%m%d", errors="coerce")
            clean_df["gr_d_str"] = gr_dt.dt.strftime("%Y-%m-%d").fillna("")
            clean_df["wh_dwell"] = (snap_dt - gr_dt).dt.days.fillna(0).astype(int)
        else:
            clean_df["gr_d_str"] = ""
            clean_df["wh_dwell"] = 0

        # Load existing active state
        existing = pd.read_sql_query("SELECT barcode, current_bin, first_seen_in_bin, consecutive_days_in_bin FROM BARCODE_BIN_STAY", conn)
        existing_map = existing.set_index("barcode").to_dict(orient="index") if not existing.empty else {}

        records_to_upsert = []
        for _, row in clean_df.iterrows():
            b = row["b_code"]
            mat = row["m_no"]
            b_bin = row["b_bin"]
            gr_d = row["gr_d_str"]
            wh_dwell = row["wh_dwell"]

            if b in existing_map:
                prev = existing_map[b]
                if prev["current_bin"] == b_bin:
                    first_seen = prev["first_seen_in_bin"]
                    consec = (snap_dt - pd.to_datetime(first_seen)).days + 1
                else:
                    first_seen = snap_date_str
                    consec = 1
            else:
                first_seen = snap_date_str
                consec = 1

            records_to_upsert.append((
                b, mat, b_bin, first_seen, snap_date_str, consec, gr_d, wh_dwell, 1, now_str
            ))

        cur.execute("CREATE TEMP TABLE IF NOT EXISTS temp_bbs AS SELECT * FROM BARCODE_BIN_STAY WHERE 0")
        cur.execute("DELETE FROM temp_bbs")
        cur.executemany("INSERT INTO temp_bbs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", records_to_upsert)

        cur.execute("""
        INSERT OR REPLACE INTO BARCODE_BIN_STAY (
            barcode, mat_no, current_bin, first_seen_in_bin, last_seen_date,
            consecutive_days_in_bin, gr_date, warehouse_dwell_days, is_active, updated_at
        ) SELECT * FROM temp_bbs
        """)
        cur.execute("DROP TABLE temp_bbs")

        # Deactivate vanished barcodes
        cur.execute("UPDATE BARCODE_BIN_STAY SET is_active = 0 WHERE last_seen_date != ?", (snap_date_str,))
        conn.commit()
        return len(records_to_upsert)
    finally:
        if close_conn:
            conn.close()


def get_bin_dwell_map(active_only: bool = True) -> Dict[str, Dict[str, Any]]:
    """바코드별 빈 체류일수 및 창고 체류일수 빠른 조회를 위한 dict 반환"""
    conn = get_connection()
    cur = conn.cursor()
    query = """
        SELECT barcode, mat_no, current_bin, first_seen_in_bin, last_seen_date,
               consecutive_days_in_bin, gr_date, warehouse_dwell_days
        FROM BARCODE_BIN_STAY
    """
    if active_only:
        query += " WHERE is_active = 1"

    rows = cur.execute(query).fetchall()
    conn.close()

    result = {}
    for r in rows:
        result[r["barcode"]] = {
            "current_bin": r["current_bin"],
            "first_seen_in_bin": r["first_seen_in_bin"],
            "consecutive_days_in_bin": r["consecutive_days_in_bin"],
            "gr_date": r["gr_date"],
            "warehouse_dwell_days": r["warehouse_dwell_days"]
        }
    return result


def get_latest_issued_gr_map() -> Dict[str, Dict[str, Any]]:
    """
    Query BARCODE_LIFECYCLE for materials with production GI history:
    Returns dict: mat_no -> {
        'latest_issued_gr_date': datetime.date,
        'latest_gi_time': str
    }
    Maps both raw mat_no and stripped (without leading zeros).
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT mat_no, MAX(gr_time) as max_gr_time, MAX(production_gi_time) as max_gi_time
        FROM BARCODE_LIFECYCLE
        WHERE status = 'PRODUCED' AND gr_time IS NOT NULL AND gr_time != ''
        GROUP BY mat_no
    """)
    rows = cur.fetchall()
    conn.close()

    result = {}
    for r in rows:
        mat_no = str(r["mat_no"]).strip()
        gr_t = r["max_gr_time"]
        gi_t = r["max_gi_time"]
        gr_d = None
        if gr_t:
            try:
                gr_d = datetime.date.fromisoformat(str(gr_t)[:10])
            except Exception:
                pass
        item = {
            "latest_issued_gr_date": gr_d,
            "latest_gi_time": gi_t
        }
        result[mat_no] = item
        clean_mat = mat_no.lstrip("0")
        if clean_mat and clean_mat != mat_no:
            result[clean_mat] = item
    return result


def get_material_activity_map(threshold_days: int = 45) -> Dict[str, Dict[str, Any]]:
    """
    Query GR and GI transactions to determine recent activity per material:
    Returns dict: mat_no -> {
        'is_active': bool,
        'latest_activity_date': datetime.date,
        'days_since_activity': int
    }
    """
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT mat_no, MAX(last_time) as latest_act
        FROM (
            SELECT mat_no, MAX(gi_time) as last_time FROM GI_TRANSACTIONS GROUP BY mat_no
            UNION ALL
            SELECT mat_no, MAX(gr_time) as last_time FROM GR_TRANSACTIONS GROUP BY mat_no
        )
        GROUP BY mat_no
    """)
    rows = cur.fetchall()
    conn.close()

    today = datetime.date.today()
    result = {}
    for r in rows:
        mat_no = str(r["mat_no"]).strip()
        act_str = r["latest_act"]
        act_d = None
        days_since = 999
        is_active = False
        if act_str:
            try:
                act_d = datetime.date.fromisoformat(str(act_str)[:10])
                days_since = (today - act_d).days
                is_active = (days_since <= threshold_days)
            except Exception:
                pass
        item = {
            "is_active": is_active,
            "latest_activity_date": act_d,
            "days_since_activity": days_since
        }
        result[mat_no] = item
        clean_mat = mat_no.lstrip("0")
        if clean_mat and clean_mat != mat_no:
            result[clean_mat] = item
    return result


def save_wms_stock_daily_agg(
    df: pd.DataFrame,
    snapshot_date: Optional[str] = None,
    db_path: Optional[Path] = None
) -> int:
    """
    WMS Stock List 원본 DataFrame을 Bin x Mat.No 단위로 그룹화하여
    WMS_STOCK_DAILY_AGG 테이블에 압축 적재합니다 (5.4만 행 -> 약 4.9천 행).
    """
    if df.empty:
        return 0

    snap_date = snapshot_date or datetime.date.today().strftime("%Y-%m-%d")
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Column identification
    bin_col = next((c for c in df.columns if c.strip().lower() == "bin"), "BIN")
    mat_col = next((c for c in df.columns if "mat" in c.lower() and "no" in c.lower()), "Mat.No")
    bcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), "Barcode")
    qty_col = next((c for c in df.columns if "qty" in c.lower()), "Qty.")
    gr_col = next((c for c in df.columns if "gr" in c.lower() and "date" in c.lower()), "GR Date")

    sub = df[[bin_col, mat_col, bcode_col, qty_col]].copy()
    sub.columns = ["bin", "mat_no", "barcode", "qty"]
    sub["bin"] = sub["bin"].fillna("BINSYNTH9953").astype(str).str.strip().str.upper()
    sub["bin"] = sub["bin"].replace({"": "BINSYNTH9953", "NAN": "BINSYNTH9953", "NONE": "BINSYNTH9953", "NULL": "BINSYNTH9953"})
    sub["mat_no"] = sub["mat_no"].fillna("UNKNOWN").astype(str).str.strip()
    sub["mat_no"] = sub["mat_no"].replace({"": "UNKNOWN", "NAN": "UNKNOWN", "NONE": "UNKNOWN"})
    sub["qty"] = pd.to_numeric(sub["qty"], errors="coerce").fillna(0.0)

    if gr_col in df.columns:
        sub["gr_date"] = df[gr_col].astype(str).str.strip()
    elif "GR_Date" in df.columns:
        sub["gr_date"] = df["GR_Date"].astype(str).str.strip()
    else:
        sub["gr_date"] = ""

    # Group by (bin, mat_no) preserving all BINSYNTH9953 rows
    grouped = sub.groupby(["bin", "mat_no"], as_index=False, dropna=False).agg(
        box_count=("barcode", "count"),
        total_qty=("qty", "sum"),
        oldest_gr_date=("gr_date", lambda s: str(s.replace("", "99999999").min()) if not s.empty else ""),
        newest_gr_date=("gr_date", lambda s: str(s.max()) if not s.empty else "")
    )
    # Clean up dummy min
    grouped["oldest_gr_date"] = grouped["oldest_gr_date"].replace("99999999", "")

    records = [
        (snap_date, r["bin"], r["mat_no"], int(r["box_count"]), float(r["total_qty"]),
         str(r["oldest_gr_date"]), str(r["newest_gr_date"]), now_str)
        for _, r in grouped.iterrows()
    ]

    conn = get_connection(db_path)
    cur = conn.cursor()
    chunk_size = 5000
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        cur.executemany("""
        INSERT OR REPLACE INTO WMS_STOCK_DAILY_AGG (
            snapshot_date, bin, mat_no, box_count, total_qty, oldest_gr_date, newest_gr_date, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, chunk)

    conn.commit()
    conn.close()
    return len(records)


def save_audit_ledger_and_features(
    run_info: Dict[str, Any],
    top_bins: List[Dict[str, Any]],
    extended_bins: Optional[List[Dict[str, Any]]] = None,
    suspects_df: Optional[pd.DataFrame] = None,
    random_bins: Optional[List[Dict[str, Any]]] = None,
    db_path: Optional[Path] = None,
    gi_signal_db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """
    1. AUDIT_RUN (실사 회차 정보)
    2. AUDIT_ASSIGNMENT (실사 지시 목록 40~50개 빈 + 무작위 검증 표본)
    3. AUDIT_FEATURE_SNAPSHOT (당일 계산된 suspects 전체 피처 및 score_breakdown)
    위 3개 테이블을 단일 트랜잭션으로 원자적(Atomic) 적재합니다.
    """
    import json
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    run_id = run_info.get("run_id")
    if not run_id:
        r_date = run_info.get("run_date", datetime.date.today().strftime("%Y-%m-%d")).replace("-", "")
        r_mode = run_info.get("mode", "Daily").upper()
        run_id = f"{r_date}_{r_mode}"

    run_date = run_info.get("run_date", datetime.date.today().strftime("%Y-%m-%d"))
    mode = run_info.get("mode", "Daily")
    scoring_version = run_info.get("scoring_version", "v1.0_rules")
    wms_snapshot_file = run_info.get("wms_snapshot_file", "")
    config_hash = run_info.get("config_hash", "default")

    # 1. Insert or Replace AUDIT_RUN
    cur.execute("""
    INSERT OR REPLACE INTO AUDIT_RUN (
        run_id, run_date, mode, scoring_version, wms_snapshot_file, config_hash, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (run_id, run_date, mode, scoring_version, wms_snapshot_file, config_hash, now_str))

    # 2. Build and Insert AUDIT_ASSIGNMENT records
    assignments = []
    # TOP bins
    rank = 1
    for b in top_bins:
        bin_code = b.get("BIN", "")
        parts = b.get("Parts_List", "")
        mat_json = json.dumps(parts.split(", ") if isinstance(parts, str) and parts else (list(parts) if isinstance(parts, (list, set)) else []))
        wms_bcs = b.get("WMS_Barcodes") or b.get("Barcodes") or []
        wms_bcs_json = json.dumps(sorted(list(set(str(x).strip() for x in wms_bcs)))) if wms_bcs else None
        assignment_id = f"{run_id}_{rank:03d}_{bin_code}"
        l_type = b.get("list_type", "TOP")
        s_method = b.get("selection_method", "RULE_SCORE")
        inc_prob = float(b.get("inclusion_probability", 1.0))
        lbl_basis = str(b.get("Label_Qty_Basis", "ALL_BIN_BARCODES"))
        assignments.append((
            assignment_id, run_id, bin_code, rank, l_type, s_method,
            float(b.get("Risk_Score", 0.0)), str(b.get("Action_Required", "Physical Audit")),
            str(b.get("Reason", "")), mat_json, int(b.get("Label_QTY", 0)),
            str(b.get("Plant", "Plant 1")), int(b.get("Tier") or 0), inc_prob, lbl_basis, wms_bcs_json, now_str
        ))
        rank += 1

    # EXTENDED bins (if Daily mode)
    if extended_bins:
        for b in extended_bins:
            bin_code = b.get("BIN", "")
            parts = b.get("Parts_List", "")
            mat_json = json.dumps(parts.split(", ") if isinstance(parts, str) and parts else (list(parts) if isinstance(parts, (list, set)) else []))
            wms_bcs = b.get("WMS_Barcodes") or b.get("Barcodes") or []
            wms_bcs_json = json.dumps(sorted(list(set(str(x).strip() for x in wms_bcs)))) if wms_bcs else None
            assignment_id = f"{run_id}_{rank:03d}_{bin_code}"
            l_type = b.get("list_type", "EXTENDED")
            s_method = b.get("selection_method", "RULE_SCORE")
            inc_prob = float(b.get("inclusion_probability", 1.0))
            lbl_basis = str(b.get("Label_Qty_Basis", "ALL_BIN_BARCODES"))
            assignments.append((
                assignment_id, run_id, bin_code, rank, l_type, s_method,
                float(b.get("Risk_Score", 0.0)), str(b.get("Action_Required", "Physical Audit")),
                str(b.get("Reason", "")), mat_json, int(b.get("Label_QTY", 0)),
                str(b.get("Plant", "Plant 1")), int(b.get("Tier") or 0), inc_prob, lbl_basis, wms_bcs_json, now_str
            ))
            rank += 1

    # RANDOM bins (Validation Sample for selection bias correction & exploration)
    if random_bins:
        for b in random_bins:
            bin_code = b.get("BIN", "")
            parts = b.get("Parts_List", "")
            mat_json = json.dumps(parts.split(", ") if isinstance(parts, str) and parts else (list(parts) if isinstance(parts, (list, set)) else []))
            wms_bcs = b.get("WMS_Barcodes") or b.get("Barcodes") or []
            wms_bcs_json = json.dumps(sorted(list(set(str(x).strip() for x in wms_bcs)))) if wms_bcs else None
            assignment_id = f"{run_id}_{rank:03d}_{bin_code}"
            l_type = b.get("list_type", "RANDOM")
            s_method = b.get("selection_method", "RANDOM_SAMPLE")
            inc_prob = float(b.get("inclusion_probability", 1.0))
            lbl_basis = str(b.get("Label_Qty_Basis", "ALL_BIN_BARCODES"))
            assignments.append((
                assignment_id, run_id, bin_code, rank, l_type, s_method,
                float(b.get("Risk_Score", 0.0)), str(b.get("Action_Required", "Random Verification")),
                str(b.get("Reason", "Random sample (model validation)")), mat_json, int(b.get("Label_QTY", 0)),
                str(b.get("Plant", "Plant 1")), int(b.get("Tier") or 0), inc_prob, lbl_basis, wms_bcs_json, now_str
            ))
            rank += 1

    if assignments:
        cur.executemany("""
        INSERT OR REPLACE INTO AUDIT_ASSIGNMENT (
            assignment_id, run_id, bin, rank, list_type, selection_method,
            bin_risk_score, action_required, primary_reason, materials_json,
            label_qty, plant, tier, inclusion_probability, label_qty_basis, wms_barcodes_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, assignments)

    # 3. Insert AUDIT_FEATURE_SNAPSHOT (전체 suspects 피처 전수 저장)
    features_count = 0
    if suspects_df is not None and not suspects_df.empty:
        # Clear existing features for this run_id to avoid duplicate on re-run
        cur.execute("DELETE FROM AUDIT_FEATURE_SNAPSHOT WHERE run_id = ?", (run_id,))

        feature_records = []
        for _, row in suspects_df.iterrows():
            sb = row.get("Score_Breakdown", {})
            sb_json = json.dumps(sb) if isinstance(sb, dict) else (str(sb) if sb else "{}")
            feature_records.append((
                run_id,
                str(row.get("Barcode", "")),
                str(row.get("Mat_No", "")),
                str(row.get("BIN", "")),
                str(row.get("Plant", "Plant 1")),
                int(row.get("Tier") or 0),
                float(row.get("Qty", 0.0) or 0.0),
                str(row.get("GR_Date", "") or ""),
                int(row.get("Warehouse_Dwell_Days", 0) or 0),
                int(row.get("Bin_Dwell_Days", 1) or 1),
                int(row.get("Lot_Age_Days", 0) or 0),
                float(row.get("Ontology_Turnover_Days", 0.0) or 0.0),
                str(row.get("Mover_Class", "UNKNOWN") or "UNKNOWN"),
                str(row.get("Missing_GI_Flag", "BALANCED") or "BALANCED"),
                float(row.get("Missing_GI_Diff_Box", 0.0) or 0.0),
                int(row.get("FIFO_Bypass_Gap_Days", 0) or 0),
                int(row.get("Same_Bin_Lot_Spread_Days", 0) or 0),
                int(row.get("Risk_Score", 0) or 0),
                str(row.get("Risk_Categories", "") or ""),
                sb_json,
                float(row.get("ZTCODE_STOCK_REC_Diff_Qty", 0.0) or 0.0),
                int(row.get("ZTCODE_STOCK_REC_Consecutive_Days", 0) or 0),
                float(row.get("ZTCODE_STOCK_REC_Exposure", 0.0) or 0.0),
                float(row["Net_Price"]) if row.get("Net_Price") is not None and pd.notna(row.get("Net_Price")) else None,
                float(row["P_Error_Proxy"]) if row.get("P_Error_Proxy") is not None and pd.notna(row.get("P_Error_Proxy")) else None,
                float(row["Expected_Loss"]) if row.get("Expected_Loss") is not None and pd.notna(row.get("Expected_Loss")) else None,
                float(row["Est_Minutes"]) if row.get("Est_Minutes") is not None and pd.notna(row.get("Est_Minutes")) else None,
                float(row["Priority_EV"]) if row.get("Priority_EV") is not None and pd.notna(row.get("Priority_EV")) else None,
                int(row.get("Chain_Gap_Flag", 0) or 0),
                int(row.get("Chain_Gap_Days", 0) or 0),
                int(row.get("Cooldown_Excluded", 0) or 0),
                str(row.get("Cooldown_Reason", "") or "") or None,
                now_str
            ))

        chunk_size = 5000
        for i in range(0, len(feature_records), chunk_size):
            chunk = feature_records[i:i + chunk_size]
            cur.executemany("""
            INSERT INTO AUDIT_FEATURE_SNAPSHOT (
                run_id, barcode, mat_no, bin, plant, tier, qty, gr_date,
                warehouse_dwell_days, bin_dwell_days, lot_age_days,
                ontology_turnover_days, mover_class, missing_gi_flag,
                missing_gi_diff_box, fifo_bypass_gap_days,
                same_bin_lot_spread_days, risk_score, risk_categories,
                score_breakdown_json, ztcode_stock_rec_diff_qty, ztcode_stock_rec_consecutive_days,
                ztcode_stock_rec_exposure, net_price, p_error_proxy, expected_loss,
                est_minutes, priority_ev, chain_gap_flag, chain_gap_days,
                cooldown_excluded, cooldown_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, chunk)

        features_count = len(feature_records)

    conn.commit()
    conn.close()

    # D90-3: Link audit_run_id to GI_STOCK_GAP_SIGNALS for CHAIN_GAP materials in this run
    # Only touch operational 01_JOB DB when saving to operational DB_PATH (or explicit gi_signal_db_path)
    try:
        is_op_db = (db_path is None) or (Path(db_path).resolve() == DB_PATH.resolve())
        target_gi_db = Path(gi_signal_db_path) if gi_signal_db_path is not None else (
            (Path(__file__).resolve().parent.parent / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db")
            if is_op_db else None
        )
        if target_gi_db and target_gi_db.exists():
            cg_mats = set()
            if suspects_df is not None and not suspects_df.empty and "Chain_Gap_Flag" in suspects_df.columns:
                cg_sub = suspects_df[suspects_df["Chain_Gap_Flag"] == 1]
                for m_val in cg_sub["Mat_No"].dropna().unique():
                    cg_mats.add(str(m_val).strip())
            for b_list in [top_bins or [], extended_bins or [], random_bins or []]:
                for b_item in b_list:
                    for p_str in str(b_item.get("Parts_List", "")).split(","):
                        if p_str.strip():
                            cg_mats.add(p_str.strip())
            if cg_mats:
                with sqlite3.connect(str(target_gi_db), timeout=10.0) as g_conn:
                    g_cur = g_conn.cursor()
                    g_cur.execute("PRAGMA table_info(GI_STOCK_GAP_SIGNALS);")
                    g_cols = {r[1] for r in g_cur.fetchall()}
                    if "audit_run_id" in g_cols:
                        placeholders = ",".join(["?"] * len(cg_mats))
                        g_cur.execute(
                            f"""
                            UPDATE GI_STOCK_GAP_SIGNALS
                            SET audit_run_id = ?
                            WHERE signal_type = 'CHAIN_GAP'
                              AND material IN ({placeholders})
                              AND (audit_run_id IS NULL OR audit_run_id = '')
                            """,
                            [run_id] + sorted(list(cg_mats))
                        )
                        g_conn.commit()
    except Exception as e_cg_link:
        logger.warning(f"[ChainGapFeed] Could not link audit_run_id to GI_STOCK_GAP_SIGNALS: {e_cg_link}")

    return {
        "run_id": run_id,
        "assignments_count": len(assignments),
        "features_count": features_count
    }


def get_audit_assignments_by_run(run_id: str, db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """특정 run_id의 실사 지시 목록을 rank 순으로 복원 조회합니다."""
    conn = get_connection(db_path)
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT assignment_id, run_id, bin, rank, list_type, selection_method,
               bin_risk_score, action_required, primary_reason, materials_json,
               label_qty, plant, tier, inclusion_probability, created_at
        FROM AUDIT_ASSIGNMENT
        WHERE run_id = ?
        ORDER BY rank ASC
    """, (run_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]



def get_active_chain_gap_feed(
    gi_db_path: Optional[Path] = None,
    run_date: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    D90-2 / D90-3 (Amended by Thread 146):
    Reads 01_JOB_GR_GI_Error's GI_STOCK_GAP_SIGNALS table for signal_type='CHAIN_GAP'
    and computes active chain_gap_flag (1) and consecutive_days per material.
    """
    target_gi_db = Path(gi_db_path) if gi_db_path else (
        Path(__file__).resolve().parent.parent / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db"
    )
    if not target_gi_db.exists():
        return {}

    ref_date_str = str(run_date or datetime.date.today().strftime("%Y-%m-%d"))[:10]
    try:
        ref_date = datetime.date.fromisoformat(ref_date_str)
    except Exception:
        ref_date = datetime.date.today()

    chain_gap_items: Dict[str, Dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{target_gi_db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='GI_STOCK_GAP_SIGNALS'")
            if not cur.fetchone():
                return {}
            cur.execute("PRAGMA table_info(GI_STOCK_GAP_SIGNALS)")
            cols = {r[1] for r in cur.fetchall()}
            date_expr = "COALESCE(signal_date, substr(observed_at, 1, 10))" if "signal_date" in cols else "substr(observed_at, 1, 10)"
            cur.execute(f"""
                SELECT material, {date_expr} as sig_date, error_from, error_to, error_qty, total_gap
                FROM GI_STOCK_GAP_SIGNALS
                WHERE signal_type = 'CHAIN_GAP'
                ORDER BY sig_date DESC, id DESC
            """)
            by_mat: Dict[str, List[Dict[str, Any]]] = {}
            for r in cur.fetchall():
                m = str(r["material"] or "").strip()
                if m:
                    by_mat.setdefault(m, []).append(dict(r))

            for m, rows in by_mat.items():
                dates_sorted = sorted(
                    {str(x["sig_date"])[:10] for x in rows if x.get("sig_date")},
                    reverse=True
                )
                if not dates_sorted:
                    continue
                try:
                    latest_dt = datetime.date.fromisoformat(dates_sorted[0])
                except Exception:
                    continue
                # Active if observed on ref_date or within the last 1 day (e.g. previous day's 01_JOB cycles)
                if (ref_date - latest_dt).days > 1:
                    continue

                cons_days = 1
                prev_dt = latest_dt
                for d_str in dates_sorted[1:]:
                    try:
                        cur_dt = datetime.date.fromisoformat(d_str)
                    except Exception:
                        break
                    gap = (prev_dt - cur_dt).days
                    # Allow Sunday skip (gap == 2 if prev_dt is Monday)
                    if gap == 1 or (gap == 2 and prev_dt.weekday() == 0):
                        cons_days += 1
                        prev_dt = cur_dt
                    else:
                        break

                latest_row = rows[0]
                info = {
                    "material": m,
                    "chain_gap_flag": 1,
                    "consecutive_days": cons_days,
                    "latest_signal_date": dates_sorted[0],
                    "error_from": str(latest_row.get("error_from") or ""),
                    "error_to": str(latest_row.get("error_to") or ""),
                    "error_qty": float(latest_row.get("error_qty") or 0.0),
                    "total_gap": float(latest_row.get("total_gap") or 0.0),
                }
                chain_gap_items[m] = info
                chain_gap_items[m.lstrip("0")] = info
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[ChainGapFeed] Could not read GI_STOCK_GAP_SIGNALS ({e}), continuing with standard scoring.")
        return {}

    return chain_gap_items


def get_active_chronic_mismatch_feed(
    rec_db_path: Optional[Path] = None,
    main_conn: Optional[sqlite3.Connection] = None
) -> Dict[str, Dict[str, Any]]:
    """
    04_JOB_SystemReconciliation의 chronic_mismatch_registry에서
    status='ACTIVE' AND recommended_for_audit=1인 품목 및 시계열 오차 지표를 조회합니다.
    - main_conn이 전달되면 ATTACH DATABASE를 우선 시도합니다.
    - 파일 부재, 락(Lock) 또는 예외 발생 시 경고 로그만 남기고 {}를 반환하여
      00_JOB 실사 파이프라인의 무중단 동작을 보장합니다.
    - 위탁/레진 착시 품목(ROH2, ROH3, 74-*, 89-*)은 원천 배제됩니다.
    Returns:
        {material_code: {
            "material": str,
            "description": str,
            "consecutive_days": int,
            "current_diff_qty": float,
            "current_financial_exposure": float,
            "recommended_for_audit": int
        }}
    """
    target_rec_db = Path(rec_db_path) if rec_db_path else (
        Path(__file__).resolve().parent.parent / "04_JOB_SystemReconciliation" / "db" / "system_reconciliation.db"
    )
    if not target_rec_db.exists():
        logger.warning(f"[ChronicFeed] System reconciliation DB not found: {target_rec_db}")
        return {}

    chronic_items = {}
    try:
        # Strategy A: Use ATTACH if main connection provided
        if main_conn is not None:
            cur = main_conn.cursor()
            attached = False
            try:
                cur.execute(f"ATTACH DATABASE '{target_rec_db.as_posix()}' AS rec_db")
                attached = True
                cur.execute("""
                    SELECT material, description, consecutive_days, current_diff_qty, current_financial_exposure
                    FROM rec_db.chronic_mismatch_registry
                    WHERE status = 'ACTIVE' AND recommended_for_audit = 1
                      AND material_type NOT IN ('ROH2', 'ROH3')
                      AND material NOT LIKE '74-%'
                      AND material NOT LIKE '89-%'
                    ORDER BY current_financial_exposure DESC
                """)
                for row in cur.fetchall():
                    mat = str(row[0]).strip()
                    item_dict = {
                        "material": mat,
                        "description": str(row[1] or ""),
                        "consecutive_days": int(row[2] or 0),
                        "current_diff_qty": float(row[3] or 0.0),
                        "current_financial_exposure": float(row[4] or 0.0),
                        "recommended_for_audit": 1
                    }
                    chronic_items[mat] = item_dict
                    chronic_items[mat.lstrip("0")] = item_dict
            finally:
                if attached:
                    try:
                        cur.execute("DETACH DATABASE rec_db")
                    except Exception:
                        pass
        else:
            # Strategy B: Read-only URI direct connection with WAL & timeout
            conn = sqlite3.connect(f"file:{target_rec_db.as_posix()}?mode=ro", uri=True, timeout=3.0)
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT material, description, consecutive_days, current_diff_qty, current_financial_exposure
                    FROM chronic_mismatch_registry
                    WHERE status = 'ACTIVE' AND recommended_for_audit = 1
                      AND material_type NOT IN ('ROH2', 'ROH3')
                      AND material NOT LIKE '74-%'
                      AND material NOT LIKE '89-%'
                    ORDER BY current_financial_exposure DESC
                """)
                for row in cur.fetchall():
                    mat = str(row["material"]).strip()
                    item_dict = {
                        "material": mat,
                        "description": str(row["description"] or ""),
                        "consecutive_days": int(row["consecutive_days"] or 0),
                        "current_diff_qty": float(row["current_diff_qty"] or 0.0),
                        "current_financial_exposure": float(row["current_financial_exposure"] or 0.0),
                        "recommended_for_audit": 1
                    }
                    chronic_items[mat] = item_dict
                    chronic_items[mat.lstrip("0")] = item_dict
            finally:
                conn.close()
    except Exception as e:
        logger.warning(f"[ChronicFeed] Could not read chronic mismatch feed ({e}), continuing with standard scoring.")
        return {}

    return chronic_items


def get_latest_material_net_prices(rec_db_path: Optional[Path] = None) -> Dict[str, float]:
    """
    04_JOB_SystemReconciliation의 accuracy_material_snapshot 테이블에서
    가장 최근 snapshot_date 기준 품목별 유효 단가(net_price / price_unit)를 읽기 전용으로 조회합니다.
    - 파일 부재, 락(Lock) 또는 예외 발생 시 경고 로그만 남기고 {}를 반환합니다.
    - 품목 코드는 원본 문자열 및 앞자리 0 제거 버전(lstrip("0")) 둘 다 매핑합니다.
    Returns:
        {material_code: unit_price_float}
    """
    target_rec_db = Path(rec_db_path) if rec_db_path else (
        Path(__file__).resolve().parent.parent / "04_JOB_SystemReconciliation" / "db" / "system_reconciliation.db"
    )
    if not target_rec_db.exists():
        logger.warning(f"[NetPrice] System reconciliation DB not found: {target_rec_db}")
        return {}

    price_map = {}
    try:
        # Open in read-only URI mode to prevent any locking on production reconciliation DB
        uri_path = f"file:{target_rec_db.resolve().as_posix()}?mode=ro"
        conn = sqlite3.connect(uri_path, uri=True, timeout=5.0)
        cur = conn.cursor()
        
        # Check if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='accuracy_material_snapshot'")
        if not cur.fetchone():
            conn.close()
            return {}

        cur.execute("""
            SELECT material, net_price, price_unit
            FROM accuracy_material_snapshot
            WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM accuracy_material_snapshot)
              AND net_price > 0
        """)
        for row in cur.fetchall():
            mat = str(row[0]).strip()
            net_price = float(row[1] or 0.0)
            price_unit = float(row[2] or 1.0)
            unit_price = round(net_price / price_unit if price_unit > 0 else net_price, 4)
            price_map[mat] = unit_price
            price_map[mat.lstrip("0")] = unit_price
        conn.close()
        return price_map
    except Exception as e:
        logger.warning(f"[NetPrice] Failed to query latest material net prices (continuing without prices): {e}")
        return {}

def reconcile_in_flight_movement(
    conn: sqlite3.Connection,
    bin_val: str,
    mat_no: Optional[str],
    snapshot_time: str,
    counted_at: str,
    book_qty_at_count: float,
    counted_qty: float
) -> Dict[str, Any]:
    """
    Reconciles in-flight Goods Receipt (GR) and Goods Issue (GI) that occurred
    between cycle count dispatch (snapshot_time) and actual physical counting (counted_at).

    Formula:
        effective_book_qty = book_qty_at_count + in_flight_gr_qty - in_flight_gi_qty
        diff_qty = counted_qty - effective_book_qty

    If counted_qty matches effective_book_qty, the apparent discrepancy was 100% caused by in-flight movement!
    """
    cur = conn.cursor()
    b_clean = str(bin_val).strip().upper()
    m_clean = str(mat_no).strip() if mat_no else None

    # 1. Query in-flight GR (destination bin = b_clean)
    if m_clean:
        cur.execute("""
            SELECT COALESCE(SUM(gr_qty), 0.0) FROM GR_TRANSACTIONS
            WHERE (TRIM(UPPER(gr_loc)) = ? OR TRIM(UPPER(skid)) = ? OR TRIM(UPPER(bin)) = ?)
              AND TRIM(mat_no) = ?
              AND gr_time >= ? AND gr_time <= ?;
        """, [b_clean, b_clean, b_clean, m_clean, snapshot_time, counted_at])
    else:
        cur.execute("""
            SELECT COALESCE(SUM(gr_qty), 0.0) FROM GR_TRANSACTIONS
            WHERE (TRIM(UPPER(gr_loc)) = ? OR TRIM(UPPER(skid)) = ? OR TRIM(UPPER(bin)) = ?)
              AND gr_time >= ? AND gr_time <= ?;
        """, [b_clean, b_clean, b_clean, snapshot_time, counted_at])
    in_flight_gr = float(cur.fetchone()[0] or 0.0)

    # 2. Query in-flight GI (origin bin = b_clean)
    if m_clean:
        cur.execute("""
            SELECT COALESCE(SUM(qty), 0.0) FROM GI_TRANSACTIONS
            WHERE TRIM(UPPER(gi_loc)) = ?
              AND TRIM(mat_no) = ?
              AND gi_time >= ? AND gi_time <= ?;
        """, [b_clean, m_clean, snapshot_time, counted_at])
    else:
        cur.execute("""
            SELECT COALESCE(SUM(qty), 0.0) FROM GI_TRANSACTIONS
            WHERE TRIM(UPPER(gi_loc)) = ?
              AND gi_time >= ? AND gi_time <= ?;
        """, [b_clean, snapshot_time, counted_at])
    in_flight_gi = float(cur.fetchone()[0] or 0.0)

    # Calculate effective book quantity
    effective_book_qty = round(book_qty_at_count + in_flight_gr - in_flight_gi, 2)
    raw_diff = round(counted_qty - book_qty_at_count, 2)
    true_diff = round(counted_qty - effective_book_qty, 2)

    in_flight_offset_matched = (abs(true_diff) <= 0.001 and abs(raw_diff) > 0.001)

    return {
        "in_flight_gr_qty": in_flight_gr,
        "in_flight_gi_qty": in_flight_gi,
        "effective_book_qty": effective_book_qty,
        "raw_diff_qty": raw_diff,
        "true_diff_qty": true_diff,
        "in_flight_adjusted": (in_flight_gr > 0 or in_flight_gi > 0),
        "in_flight_offset_matched": in_flight_offset_matched
    }


def save_audit_result(
    result_data: Dict[str, Any],
    db_path: Optional[Path] = None
) -> int:
    """
    Saves or appends a physical cycle count result to AUDIT_RESULT.
    Supports recounts (is_recount=1) for 1:N relationship with AUDIT_ASSIGNMENT.
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
    INSERT INTO AUDIT_RESULT (
        assignment_id, run_id, bin, mat_no, counted_at,
        book_qty_at_count, counted_qty, diff_qty, diff_amount,
        cause_code, minutes_spent, counter_id, is_recount,
        is_confirmed, audit_origin, data_origin, wms_adjustment_status,
        in_flight_gr_qty, in_flight_gi_qty, in_flight_adjusted,
        effective_book_qty, notes, is_discrepancy, label_source, field_notes, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, [
        result_data["assignment_id"],
        result_data["run_id"],
        str(result_data["bin"]).strip(),
        result_data.get("mat_no"),
        result_data["counted_at"],
        float(result_data["book_qty_at_count"]),
        float(result_data["counted_qty"]) if result_data.get("counted_qty") is not None else None,
        float(result_data["diff_qty"]) if result_data.get("diff_qty") is not None else None,
        float(result_data["diff_amount"]) if result_data.get("diff_amount") is not None else 0.0,
        str(result_data["cause_code"]).strip() if result_data.get("cause_code") else None,
        float(result_data["minutes_spent"]) if result_data.get("minutes_spent") is not None else None,
        str(result_data["counter_id"]).strip() if result_data.get("counter_id") else None,
        int(result_data.get("is_recount", 0)),
        int(result_data.get("is_confirmed", 1)),
        result_data.get("audit_origin", "PHYSICAL_CYCLE_COUNT"),
        result_data.get("data_origin", "PHYSICAL"),
        result_data.get("wms_adjustment_status", "NOT_ADJUSTED"),
        float(result_data.get("in_flight_gr_qty", 0.0)),
        float(result_data.get("in_flight_gi_qty", 0.0)),
        int(result_data.get("in_flight_adjusted", 0)),
        float(result_data.get("effective_book_qty", result_data["book_qty_at_count"])),
        result_data.get("notes", ""),
        result_data.get("is_discrepancy"),
        result_data.get("label_source"),
        result_data.get("field_notes"),
        now_str
    ])
    result_id = cur.lastrowid
    conn.commit()
    conn.close()
    return result_id


def get_audit_results_by_run(
    run_id: str,
    db_path: Optional[Path] = None,
    confirmed_only: bool = True
) -> List[Dict[str, Any]]:
    """
    Retrieves all audit counting results for a run_id, optionally filtering by confirmed.
    """
    conn = get_connection(db_path)
    cur = conn.cursor()
    query = """
        SELECT result_id, assignment_id, run_id, bin, mat_no, counted_at,
               book_qty_at_count, counted_qty, diff_qty, diff_amount,
               cause_code, minutes_spent, counter_id, is_recount,
               is_confirmed, audit_origin, data_origin, wms_adjustment_status,
               in_flight_gr_qty, in_flight_gi_qty, in_flight_adjusted,
               effective_book_qty, notes, created_at
        FROM AUDIT_RESULT
        WHERE run_id = ?
    """
    params = [run_id]
    if confirmed_only:
        query += " AND is_confirmed = 1"
    query += " ORDER BY assignment_id, is_recount ASC, counted_at ASC;"

    cur.execute(query, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_hit_rate_record(
    record: Dict[str, Any],
    db_path: Optional[Path] = None
) -> None:
    """
    Saves a calculated audit hit rate record to AUDIT_HIT_RATE_DAILY.
    Uses INSERT OR REPLACE on (run_id, calculated_at) to preserve version history
    while preventing exact collision.
    """
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
    INSERT OR REPLACE INTO AUDIT_HIT_RATE_DAILY (
        run_id, audit_date, calculated_at, scoring_version,
        sample_size_top, discrepancy_count_top, hit_rate_top,
        sample_size_extended, discrepancy_count_extended, hit_rate_extended,
        sample_size_model, discrepancy_count_model, hit_rate_model,
        sample_size_random_28d, discrepancy_count_random_28d, hit_rate_random_28d, lift,
        rank_1_5_sample, rank_1_5_discrepancies, rank_1_5_hit_rate,
        rank_6_15_sample, rank_6_15_discrepancies, rank_6_15_hit_rate,
        rank_16_40_sample, rank_16_40_discrepancies, rank_16_40_hit_rate,
        cumulative_7d_hit_rate, cumulative_28d_hit_rate, notes, created_at
    ) VALUES (
        ?, ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?
    )
    """, [
        record["run_id"],
        record["audit_date"],
        record["calculated_at"],
        record.get("scoring_version"),
        int(record.get("sample_size_top", 0)),
        int(record.get("discrepancy_count_top", 0)),
        record.get("hit_rate_top"),
        int(record.get("sample_size_extended", 0)),
        int(record.get("discrepancy_count_extended", 0)),
        record.get("hit_rate_extended"),
        int(record.get("sample_size_model", 0)),
        int(record.get("discrepancy_count_model", 0)),
        record.get("hit_rate_model"),
        int(record.get("sample_size_random_28d", 0)),
        int(record.get("discrepancy_count_random_28d", 0)),
        record.get("hit_rate_random_28d"),
        record.get("lift"),
        int(record.get("rank_1_5_sample", 0)),
        int(record.get("rank_1_5_discrepancies", 0)),
        record.get("rank_1_5_hit_rate"),
        int(record.get("rank_6_15_sample", 0)),
        int(record.get("rank_6_15_discrepancies", 0)),
        record.get("rank_6_15_hit_rate"),
        int(record.get("rank_16_40_sample", 0)),
        int(record.get("rank_16_40_discrepancies", 0)),
        record.get("rank_16_40_hit_rate"),
        record.get("cumulative_7d_hit_rate"),
        record.get("cumulative_28d_hit_rate"),
        record.get("notes", ""),
        now_str
    ])
    conn.commit()
    conn.close()


def get_hit_rate_records(
    run_id: Optional[str] = None,
    limit: int = 30,
    db_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves audit hit rate records from AUDIT_HIT_RATE_DAILY.
    Returns latest calculation per run_id by default.
    """
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    if run_id:
        cur.execute("""
            SELECT * FROM AUDIT_HIT_RATE_DAILY
            WHERE run_id = ?
            ORDER BY calculated_at DESC
            LIMIT ?;
        """, (run_id, limit))
    else:
        # Get latest per run_id
        cur.execute("""
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY calculated_at DESC) as rn
                FROM AUDIT_HIT_RATE_DAILY
            )
            SELECT * FROM ranked
            WHERE rn = 1
            ORDER BY audit_date DESC, run_id DESC
            LIMIT ?;
        """, (limit,))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_rule_hit_rates(
    records: List[Dict[str, Any]],
    db_path: Optional[Path] = None
) -> None:
    """
    Saves rule hit rate records to AUDIT_RULE_HIT_RATE.
    Uses INSERT OR REPLACE on (run_id, rule_code).
    """
    if not records:
        return
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.executemany("""
    INSERT OR REPLACE INTO AUDIT_RULE_HIT_RATE (
        run_id, rule_code, rule_name, category,
        sample_size, discrepancy_count, hit_rate, smoothed_score, calculated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            r["run_id"],
            r["rule_code"],
            r["rule_name"],
            r.get("category", "DOMAIN_RULE"),
            int(r["sample_size"]),
            int(r["discrepancy_count"]),
            r.get("hit_rate"),
            r.get("smoothed_score"),
            r.get("calculated_at", now_str)
        )
        for r in records
    ])
    conn.commit()
    conn.close()


def get_rule_hit_rates(
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves rule hit rate records from AUDIT_RULE_HIT_RATE.
    If run_id is None, returns the latest run's records.
    """
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    if run_id:
        cur.execute("""
            SELECT * FROM AUDIT_RULE_HIT_RATE
            WHERE run_id = ?
            ORDER BY sample_size DESC, hit_rate DESC;
        """, (run_id,))
    else:
        cur.execute("""
            SELECT * FROM AUDIT_RULE_HIT_RATE
            WHERE run_id = (SELECT MAX(run_id) FROM AUDIT_RULE_HIT_RATE)
            ORDER BY sample_size DESC, hit_rate DESC;
        """)

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def save_historical_discrepancy_scores(
    records: List[Dict[str, Any]],
    db_path: Optional[Path] = None
) -> None:
    """
    Saves historical discrepancy scores to AUDIT_HISTORICAL_DISCREPANCY_SCORE.
    Uses INSERT OR REPLACE on (entity_type, entity_id).
    """
    if not records:
        return
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.executemany("""
    INSERT OR REPLACE INTO AUDIT_HISTORICAL_DISCREPANCY_SCORE (
        entity_type, entity_id, audit_count, discrepancy_count,
        raw_hit_rate, smoothed_score, last_audit_date, last_run_id,
        last_discrepancy, associated_bins_json, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            r["entity_type"],
            r["entity_id"],
            int(r["audit_count"]),
            int(r["discrepancy_count"]),
            float(r["raw_hit_rate"]),
            float(r["smoothed_score"]),
            r.get("last_audit_date"),
            r.get("last_run_id"),
            int(r.get("last_discrepancy", 0)),
            json.dumps(r.get("associated_bins", [])) if isinstance(r.get("associated_bins"), list) else str(r.get("associated_bins_json") or "[]"),
            r.get("updated_at", now_str)
        )
        for r in records
    ])
    conn.commit()
    conn.close()


def get_top_discrepancy_history(
    entity_type: str = "MATERIAL",
    limit: int = 10,
    db_path: Optional[Path] = None
) -> List[Dict[str, Any]]:
    """
    Retrieves top historical discrepancy watchlist items for entity_type ('MATERIAL' or 'BIN').
    Ordered by smoothed_score DESC, discrepancy_count DESC.
    """
    init_database(db_path)
    conn = get_connection(db_path)
    cur = conn.cursor()

    cur.execute("""
        SELECT * FROM AUDIT_HISTORICAL_DISCREPANCY_SCORE
        WHERE entity_type = ?
        ORDER BY smoothed_score DESC, discrepancy_count DESC
        LIMIT ?;
    """, (entity_type.upper(), limit))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def calculate_business_days_excl_sunday(start_date_str: str, end_date_str: str) -> int:
    """
    Calculates elapsed business days between start_date_str and end_date_str
    excluding Sundays (weekday == 6). Saturday is counted as a warehouse operating day.
    """
    try:
        s_dt = datetime.date.fromisoformat(str(start_date_str)[:10])
        e_dt = datetime.date.fromisoformat(str(end_date_str)[:10])
    except Exception:
        return 0
    if e_dt <= s_dt:
        return 0
    b_days = 0
    cur_d = s_dt + datetime.timedelta(days=1)
    while cur_d <= e_dt:
        if cur_d.weekday() != 6:
            b_days += 1
        cur_d += datetime.timedelta(days=1)
    return b_days


def get_audit_cooldown_history(
    target_date_str: str,
    max_business_days: int = 3,
    db_path: Optional[Path] = None
) -> Dict[str, Dict[str, Any]]:
    """
    D99 Audit Cooldown History Lookup:
    Finds the most recent confirmed physical audit result per bin prior to target_date_str
    within `max_business_days` business days (excluding Sunday).

    Returns dict keyed by bin name:
      {
        "bin": str,
        "run_id": str,
        "audit_date": str,
        "elapsed_business_days": int,
        "is_discrepancy": int (0 for match or timing_diff_excluded=1, 1 for mismatch),
        "timing_diff_excluded": int,
        "label_qty": float,
        "counted_qty": float,
        "part_numbers": str,
        "audited_barcodes": set[str],
        "audited_total_pcs": Optional[float],
        "wms_adjustment_status": str,
      }
    """
    resolved_db = Path(db_path or DB_PATH)
    if not resolved_db.exists():
        return {}

    conn = sqlite3.connect(f"file:{resolved_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('AUDIT_RUN', 'AUDIT_RESULT', 'AUDIT_ASSIGNMENT')"
            ).fetchall()
        }
        if len(tables) < 3:
            return {}

        cur.execute("""
            WITH ranked_results AS (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.assignment_id
                        ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                    ) as rn
                FROM AUDIT_RESULT r
                WHERE COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
                  AND (r.counted_qty IS NOT NULL OR r.is_discrepancy IS NOT NULL)
            )
            SELECT
                ar.run_id,
                ar.run_date,
                ar.wms_snapshot_file,
                a.assignment_id,
                a.bin,
                a.label_qty,
                a.materials_json,
                a.wms_barcodes_json,
                rr.book_qty_at_count,
                rr.effective_book_qty,
                rr.wms_boxes_at_count,
                rr.counted_qty,
                rr.is_discrepancy,
                COALESCE(rr.timing_diff_excluded, 0) as timing_diff_excluded,
                COALESCE(rr.wms_adjustment_status, 'NOT_ADJUSTED') as wms_adjustment_status,
                rr.mat_no
            FROM AUDIT_RUN ar
            JOIN AUDIT_ASSIGNMENT a ON ar.run_id = a.run_id
            JOIN ranked_results rr ON a.assignment_id = rr.assignment_id AND rr.rn = 1
            WHERE ar.run_date < ?
              AND ar.run_id NOT LIKE '%QUARANTINED%'
              AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
            ORDER BY ar.run_date DESC, rr.result_id DESC
        """, (str(target_date_str)[:10],))

        rows = [dict(r) for r in cur.fetchall()]
        if not rows:
            return {}

        # Cache suspect barcodes & PCS per (run_id, bin) from AUDIT_FEATURE_SNAPSHOT
        run_ids = sorted(list({r["run_id"] for r in rows}))
        placeholders = ",".join(["?"] * len(run_ids))
        afs_barcodes: Dict[Tuple[str, str], set] = {}
        afs_pcs_map: Dict[Tuple[str, str], float] = {}
        afs_has_null_qty: set = set()
        has_afs = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='AUDIT_FEATURE_SNAPSHOT'"
        ).fetchone()
        if has_afs:
            cur.execute(
                f"SELECT run_id, bin, barcode, qty FROM AUDIT_FEATURE_SNAPSHOT WHERE run_id IN ({placeholders})",
                run_ids,
            )
            for afs_r in cur.fetchall():
                key = (str(afs_r["run_id"]), str(afs_r["bin"]).strip())
                bc_val = str(afs_r["barcode"]).strip()
                if bc_val not in afs_barcodes.get(key, set()):
                    afs_barcodes.setdefault(key, set()).add(bc_val)
                    if afs_r["qty"] is not None:
                        afs_pcs_map[key] = afs_pcs_map.get(key, 0.0) + float(afs_r["qty"])
                    else:
                        afs_has_null_qty.add(key)

        # Cache bin-level total PCS per (snapshot_date, bin) from WMS_STOCK_DAILY_AGG (#203)
        agg_pcs_map: Dict[Tuple[str, str], float] = {}
        has_agg = cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='WMS_STOCK_DAILY_AGG'"
        ).fetchone()
        audit_dates = sorted(list({str(r["run_date"])[:10] for r in rows if r.get("run_date")}))
        if has_agg and audit_dates:
            d_ph = ",".join(["?"] * len(audit_dates))
            cur.execute(
                f"""
                SELECT snapshot_date, bin, SUM(total_qty) AS total_pcs
                FROM WMS_STOCK_DAILY_AGG
                WHERE snapshot_date IN ({d_ph})
                GROUP BY snapshot_date, bin
                """,
                audit_dates,
            )
            for agg_r in cur.fetchall():
                if agg_r["total_pcs"] is not None:
                    agg_pcs_map[(str(agg_r["snapshot_date"])[:10], str(agg_r["bin"]).strip())] = float(agg_r["total_pcs"])
    finally:
        conn.close()

    # Optional snapshot file cache for legacy runs where wms_barcodes_json / WMS_STOCK_DAILY_AGG was not populated
    snap_bin_cache: Dict[str, Dict[str, set]] = {}
    snap_pcs_cache: Dict[str, Dict[str, float]] = {}
    wms_stack_dir = BASE_DIR / "wms_daily_stack"

    def _ensure_snap_cached(fname: str) -> None:
        if not fname or fname in snap_bin_cache:
            return
        snap_path = wms_stack_dir / fname
        bin_map: Dict[str, set] = {}
        pcs_map: Dict[str, float] = {}
        if snap_path.exists():
            try:
                sdf = pd.read_excel(snap_path).dropna(subset=["Barcode"])
                for _, srow in sdf.iterrows():
                    sb = str(srow.get("BIN", "")).strip()
                    sbc = str(srow.get("Barcode", "")).strip()
                    if sb and sbc:
                        bin_map.setdefault(sb, set()).add(sbc)
                        sqty = float(srow.get("Qty.", 0) or 0) if "Qty." in sdf.columns else 0.0
                        pcs_map[sb] = pcs_map.get(sb, 0.0) + sqty
            except Exception:
                pass
        snap_bin_cache[fname] = bin_map
        snap_pcs_cache[fname] = pcs_map

    cooldown_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        b_name = str(r["bin"]).strip()
        if b_name in cooldown_map:
            # Already recorded the most recent audit for this bin
            continue

        a_date = str(r["run_date"])[:10]
        elapsed = calculate_business_days_excl_sunday(a_date, str(target_date_str)[:10])
        if elapsed < 1 or elapsed > max_business_days:
            continue

        snap_fname = str(r.get("wms_snapshot_file") or "").strip()

        # Determine audited barcode composition
        audited_bcs = set()
        if r.get("wms_barcodes_json"):
            try:
                parsed = json.loads(r["wms_barcodes_json"])
                if isinstance(parsed, list):
                    audited_bcs = {str(x).strip() for x in parsed if str(x).strip()}
            except Exception:
                pass

        if not audited_bcs and snap_fname:
            _ensure_snap_cached(snap_fname)
            audited_bcs = set(snap_bin_cache.get(snap_fname, {}).get(b_name, set()))

        if not audited_bcs:
            audited_bcs = set(afs_barcodes.get((r["run_id"], b_name), set()))

        # Determine audited total PCS (#203: detect in-place PCS adjustments on identical barcodes)
        audited_total_pcs: Optional[float] = agg_pcs_map.get((a_date, b_name))
        if audited_total_pcs is None and snap_fname:
            _ensure_snap_cached(snap_fname)
            if b_name in snap_pcs_cache.get(snap_fname, {}):
                audited_total_pcs = float(snap_pcs_cache[snap_fname][b_name])
        if audited_total_pcs is None:
            afs_key = (r["run_id"], b_name)
            afs_bcs_for_bin = afs_barcodes.get(afs_key, set())
            if (
                audited_bcs
                and afs_bcs_for_bin == audited_bcs
                and afs_key not in afs_has_null_qty
                and afs_key in afs_pcs_map
            ):
                audited_total_pcs = float(afs_pcs_map[afs_key])

        # Parse part numbers
        part_str = str(r.get("mat_no") or "").strip()
        if not part_str and r.get("materials_json"):
            try:
                m_list = json.loads(r["materials_json"])
                if isinstance(m_list, list):
                    part_str = ", ".join(str(x).strip() for x in m_list if str(x).strip())
            except Exception:
                part_str = str(r["materials_json"])

        timing_excl = int(r.get("timing_diff_excluded") or 0)
        is_disc = 0 if timing_excl == 1 else (1 if int(r.get("is_discrepancy") or 0) == 1 else 0)
        adj_status = str(r.get("wms_adjustment_status") or "NOT_ADJUSTED").strip().upper()

        eff_label = (
            r["effective_book_qty"]
            if r.get("effective_book_qty") is not None
            else (
                r["wms_boxes_at_count"]
                if r.get("wms_boxes_at_count") is not None
                else (r["book_qty_at_count"] if r.get("book_qty_at_count") is not None else (r.get("label_qty") or 0))
            )
        )
        cooldown_map[b_name] = {
            "bin": b_name,
            "run_id": r["run_id"],
            "audit_date": a_date,
            "elapsed_business_days": elapsed,
            "is_discrepancy": is_disc,
            "timing_diff_excluded": timing_excl,
            "wms_adjustment_status": adj_status,
            "label_qty": float(eff_label),
            "counted_qty": float(r["counted_qty"]) if r.get("counted_qty") is not None else None,
            "part_numbers": part_str,
            "audited_barcodes": audited_bcs,
            "audited_total_pcs": audited_total_pcs,
        }

    return cooldown_map


_PCS_SHORT_NOTE_RE = _re_out_guard.compile(
    r"\b(short|missing|loose\s+(?:packs?\s+)?taken|partial\s+pick)\b|빼간|부족",
    _re_out_guard.IGNORECASE,
)
_PCS_SURPLUS_NOTE_RE = _re_out_guard.compile(
    r"(\+\s*\d+\s*pcs\b|\bextra\s+loose\b|\bextra\s+pcs\b|\bsurplus\b)",
    _re_out_guard.IGNORECASE,
)
_S_RACK_BIN_RE = _re_out_guard.compile(r"^S\d+", _re_out_guard.IGNORECASE)
_R_RACK_BIN_RE = _re_out_guard.compile(r"^R\d+", _re_out_guard.IGNORECASE)
_P1_HIGH_RACK_RE = _re_out_guard.compile(r"^\d+[A-Z]+\s+[3-9]\d*$", _re_out_guard.IGNORECASE)
_P1_PICK_RACK_RE = _re_out_guard.compile(r"^\d+[A-Z]+\s+[1-2]$", _re_out_guard.IGNORECASE)


def _get_d112_floor_buffer_prefixes() -> Tuple[str, ...]:
    base = ["SMT", "SMD", "FLR", "OEM"]
    for env_key in ("FLOOR_BUFFER_BIN_PREFIXES", "OEM_FLOOR_BIN_PREFIX"):
        raw = os.getenv(env_key, "").strip()
        if raw:
            for tok in _re_out_guard.split(r"[,;]", raw):
                t_up = tok.strip().upper()
                if t_up and t_up not in base:
                    base.append(t_up)
    return tuple(base)


def classify_d112_zone(bin_val: Any) -> str:
    """
    D112 (#217) / D114 (#212): Map physical BIN to canonical stage/zone (`S1`..`S5` or `EXCLUDED`).
      - `EXCLUDED`: `S0..S9` (`S51Z 5`), `RESIN`, `SCR*`, `NG`, `EXT_TRL*`, `CKD*`, `D*`, `TEST*`, empty
      - `S5`: `CRT*`, `PRO*` (2h repack supply carts)
      - `S4`: `SMT*`, `SMD*`, `<OEM_FLOOR_BIN_PREFIX>*` / `FLR*`
      - `S1`: `R0..R9` (Plant 3 storage)
      - `S2`: Plant 1 Tier 3-5 high racks
      - `S3`: Plant 1 Tier 1-2 picking racks (and generic Daily test rack bins)
    """
    b = str(bin_val or "").strip().upper()
    if (
        not b
        or b in ("NAN", "BINSYNTH9953", "NG", "RESIN")
        or b.startswith(("EXT_TRL", "CKD", "SCR", "D", "TEST"))
        or _S_RACK_BIN_RE.match(b)
    ):
        return "EXCLUDED"
    if b.startswith(("CRT", "PRO")):
        return "S5"
    if b.startswith(_get_d112_floor_buffer_prefixes()):
        return "S4"
    if _R_RACK_BIN_RE.match(b):
        return "S1"
    if _P1_HIGH_RACK_RE.match(b):
        return "S2"
    if _P1_PICK_RACK_RE.match(b):
        return "S3"
    return "S3"


DEFAULT_D112_ZONE_TURNOVER_CONFIG: Dict[str, Dict[str, Any]] = {
    "S1": {"zone_code": "S1", "stage_label": "Plant 3 Storage (R*)", "median_dwell_days": None, "window_days": None, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
    "S2": {"zone_code": "S2", "stage_label": "Plant 1 Tier 3-5 Storage", "median_dwell_days": None, "window_days": None, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
    "S3": {"zone_code": "S3", "stage_label": "Plant 1 Tier 1-2 Picking", "median_dwell_days": None, "window_days": None, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
    "S4": {"zone_code": "S4", "stage_label": "Line Buffer (SMT / Floor)", "median_dwell_days": None, "window_days": None, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
    "S5": {"zone_code": "S5", "stage_label": "Repack Supply Carts (CRT / PRO)", "median_dwell_days": None, "window_days": 0, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
    "EXCLUDED": {"zone_code": "EXCLUDED", "stage_label": "Excluded Racks (S* / SCR / RESIN / Virtual)", "median_dwell_days": None, "window_days": 0, "sample_n": 0, "min_sample_n": 30, "source": "FALLBACK_PRE_D114"},
}


def get_d112_zone_turnover_config(db_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """
    D112 (#217): Read `ZONE_TURNOVER_STATS` (`zone -> window_days, sample_n`) from DB in read-only mode,
    falling back cleanly to `DEFAULT_D112_ZONE_TURNOVER_CONFIG` before D114 data accumulates.
    """
    cfg = {k: dict(v) for k, v in DEFAULT_D112_ZONE_TURNOVER_CONFIG.items()}
    resolved_db = Path(db_path or DB_PATH)
    if not resolved_db.exists():
        return cfg
    try:
        conn = sqlite3.connect(f"file:{resolved_db.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            has_tbl = cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ZONE_TURNOVER_STATS'"
            ).fetchone()
            if has_tbl:
                for r in cur.execute("SELECT * FROM ZONE_TURNOVER_STATS").fetchall():
                    d = dict(r)
                    zc = str(d.get("zone_code") or "").strip().upper()
                    if zc:
                        cfg[zc] = {**cfg.get(zc, {}), **d}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[D112] Failed to read ZONE_TURNOVER_STATS (using fallback): {e}")
    return cfg


def resolve_d112_zone_window(
    zone_code: str,
    zone_cfg_map: Dict[str, Dict[str, Any]],
) -> Tuple[bool, Optional[int], int, str]:
    r"""
    Resolve `(is_excluded, window_days_cap, sample_n, source)` for a zone under #217 & #219:
      - `EXCLUDED` (`^S\d+`, `RESIN`, `SCR*`, `EXT_TRL*`, etc.) -> `(True, 0, 0, 'EXCLUDED_ZONE')`
      - `S5` (`CRT*`, `PRO*`): `ZONE_TURNOVER_STATS` automatic switching applies ONLY to `S5`:
          * If `sample_n >= min_sample_n` and `median_dwell_days is not None`:
              - `median_dwell_days < 1.0` -> `(True, 0, sample_n, 'D114_TURNOVER_S5_LT_1D')`
              - `median_dwell_days >= 1.0` -> `(False, math.ceil(med_f), sample_n, 'D114_TURNOVER_S5')`
          * Pre-D114 fallback -> `(True, 0, sample_n, 'FALLBACK_PRE_D114_S5_EXCLUDED')`
      - `S1`..`S4` (including `S3` picking racks and `S4` `SMT*` / `<OEM_FLOOR_BIN_PREFIX>*` pre-SOP):
          * Fixed `"last 3 counts, >= 2 shorts"` (`window_days_cap = None`, never auto-excluded or shrunk
            even if `median_dwell_days < 1.0`, until post-SOP review per #219).
    """
    import math as _math

    zc = str(zone_code or "EXCLUDED").strip().upper()
    if zc == "EXCLUDED":
        return True, 0, 0, "EXCLUDED_ZONE"

    entry = zone_cfg_map.get(zc) or DEFAULT_D112_ZONE_TURNOVER_CONFIG.get(zc, {})
    sample_n = int(entry.get("sample_n") or 0)
    min_n = int(entry.get("min_sample_n") or 30)
    med = entry.get("median_dwell_days")

    if zc == "S5":
        if sample_n >= min_n and med is not None:
            med_f = float(med)
            if med_f < 1.0:
                return True, 0, sample_n, "D114_TURNOVER_S5_LT_1D"
            return False, int(_math.ceil(med_f)), sample_n, "D114_TURNOVER_S5"
        return True, 0, sample_n, "FALLBACK_PRE_D114_S5_EXCLUDED"

    # #219: S1..S4 (especially S3 & S4 including SMT / OEM floor pre-SOP) stay on fixed last-3 window
    return False, None, sample_n, "FIXED_LAST3_PRE_SOP"


def get_daily_field_instructions(
    target_date_str: Optional[str] = None,
    max_lines: int = 5,
    recent_runs_window: int = 3,
    min_short_count: int = 2,
    include_target_date: bool = False,
    db_path: Optional[Path] = None,
) -> List[str]:
    """
    D112 (#209 / #210 / #216 B-2 / #217): Build R1-only English "Field Instructions" lines (max 5)
    for 00_JOB Daily Cycle Count emails and Sheet 1.
    """
    if max_lines <= 0 or recent_runs_window <= 0:
        return []

    resolved_db = Path(db_path or DB_PATH)
    if not resolved_db.exists():
        return []

    cutoff_date = str(target_date_str or datetime.date.today().strftime("%Y-%m-%d"))[:10]
    cutoff_dt = datetime.date.fromisoformat(cutoff_date)
    anchor_dt = cutoff_dt + datetime.timedelta(days=1) if include_target_date else cutoff_dt
    date_cmp = "<=" if include_target_date else "<"

    zone_cfg_map = get_d112_zone_turnover_config(resolved_db)

    conn = sqlite3.connect(f"file:{resolved_db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        tables = {
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('AUDIT_RUN', 'AUDIT_RESULT', 'AUDIT_ASSIGNMENT')"
            ).fetchall()
        }
        if len(tables) < 3:
            return []

        # 1. Identify the last N completed Daily runs (must have >=1 counted AUDIT_RESULT row)
        run_rows = cur.execute(
            f"""
            SELECT ar.run_id, ar.run_date
            FROM AUDIT_RUN ar
            WHERE UPPER(COALESCE(ar.mode, '')) = 'DAILY'
              AND ar.run_date {date_cmp} ?
              AND ar.run_id NOT LIKE '%QUARANTINED%'
              AND ar.run_id NOT LIKE '%TEST%'
              AND EXISTS (
                  SELECT 1
                  FROM AUDIT_RESULT r2
                  WHERE r2.run_id = ar.run_id
                    AND COALESCE(r2.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
                    AND r2.is_discrepancy IS NOT NULL
              )
            ORDER BY ar.run_date DESC, ar.created_at DESC, ar.run_id DESC
            LIMIT ?
            """,
            (cutoff_date, int(recent_runs_window)),
        ).fetchall()

        if len(run_rows) < min_short_count:
            return []

        ordered_runs = list(reversed([dict(r) for r in run_rows]))
        run_ids = [r["run_id"] for r in ordered_runs]
        run_order_idx = {rid: idx for idx, rid in enumerate(run_ids)}
        run_date_map = {r["run_id"]: str(r["run_date"])[:10] for r in ordered_runs}
        placeholders = ",".join(["?"] * len(run_ids))

        # 2. Fetch deduplicated counted results across those Daily runs
        res_rows = cur.execute(
            f"""
            WITH ranked_results AS (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.run_id, COALESCE(r.assignment_id, r.bin)
                        ORDER BY COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                    ) AS rn
                FROM AUDIT_RESULT r
                WHERE r.run_id IN ({placeholders})
                  AND COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
                  AND r.is_discrepancy IS NOT NULL
            )
            SELECT
                rr.run_id,
                COALESCE(a.bin, rr.bin) AS bin,
                a.label_qty,
                rr.book_qty_at_count,
                rr.effective_book_qty,
                rr.counted_qty,
                rr.diff_qty,
                rr.is_discrepancy,
                COALESCE(rr.timing_diff_excluded, 0) AS timing_diff_excluded,
                rr.cause_code,
                rr.notes,
                rr.field_notes
            FROM ranked_results rr
            LEFT JOIN AUDIT_ASSIGNMENT a ON rr.assignment_id = a.assignment_id
            WHERE rr.rn = 1
              AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
            """,
            run_ids,
        ).fetchall()

        # 3. #216 B-2: Fetch each bin's most recent completed count up to cutoff (across any valid run)
        #    so if the latest count verified a MATCH (is_discrepancy == 0), the bin is immediately excluded.
        latest_status_rows = cur.execute(
            f"""
            WITH all_valid AS (
                SELECT
                    COALESCE(a.bin, r.bin) AS bin,
                    r.is_discrepancy,
                    ROW_NUMBER() OVER (
                        PARTITION BY TRIM(COALESCE(a.bin, r.bin))
                        ORDER BY ar.run_date DESC, ar.created_at DESC,
                                 COALESCE(r.is_confirmed, 1) DESC, COALESCE(r.is_recount, 0) DESC, r.result_id DESC
                    ) AS rn
                FROM AUDIT_RESULT r
                JOIN AUDIT_RUN ar ON r.run_id = ar.run_id
                LEFT JOIN AUDIT_ASSIGNMENT a ON r.assignment_id = a.assignment_id
                WHERE ar.run_date {date_cmp} ?
                  AND ar.run_id NOT LIKE '%QUARANTINED%'
                  AND ar.run_id NOT LIKE '%TEST%'
                  AND COALESCE(r.data_origin, '') NOT IN ('SYNTHETIC', 'SEED', 'TEST')
                  AND r.is_discrepancy IS NOT NULL
                  AND COALESCE(a.action_required, '') NOT LIKE 'CANCELLED%'
            )
            SELECT TRIM(bin) AS bin, is_discrepancy
            FROM all_valid
            WHERE rn = 1
            """,
            (cutoff_date,),
        ).fetchall()
        latest_is_match_bins = {
            str(r["bin"]).strip()
            for r in latest_status_rows
            if r["bin"] and int(r["is_discrepancy"] or 0) == 0
        }
    finally:
        conn.close()

    short_by_bin: Dict[str, List[Dict[str, Any]]] = {}
    for row in res_rows:
        b_name = str(row["bin"] or "").strip()
        if not b_name:
            continue
        # #216 B-2: Immediately exclude if the bin's most recent completed count was a MATCH
        if b_name in latest_is_match_bins:
            continue

        # #217: Zone turnover window & exclusion check
        z_code = classify_d112_zone(b_name)
        is_excl, w_days_cap, _, _ = resolve_d112_zone_window(z_code, zone_cfg_map)
        if is_excl:
            continue
        if w_days_cap is not None:
            r_date_s = run_date_map.get(row["run_id"])
            if r_date_s:
                age_days = (anchor_dt - datetime.date.fromisoformat(r_date_s)).days
                if age_days > w_days_cap:
                    continue

        if int(row["timing_diff_excluded"] or 0) == 1 or int(row["is_discrepancy"] or 0) != 1:
            continue
        if row["counted_qty"] is None:
            continue

        raw_lbl = row["label_qty"] if row["label_qty"] is not None else row["book_qty_at_count"]
        if raw_lbl is None:
            continue
        lbl_int = int(round(float(raw_lbl)))
        cnt_int = int(round(float(row["counted_qty"])))

        diff_val = row["diff_qty"]
        eff_book = (
            float(row["effective_book_qty"])
            if row["effective_book_qty"] is not None
            else float(row["book_qty_at_count"] if row["book_qty_at_count"] is not None else raw_lbl)
        )

        is_box_short = diff_val is not None and float(diff_val) < -1e-6
        is_pcs_short = False
        if not is_box_short and diff_val is None:
            cause_up = str(row["cause_code"] or "").strip().upper()
            combined_notes = f"{row['field_notes'] or ''} {row['notes'] or ''}".strip()
            if cause_up == "CC04_PARTIAL_PICK_UNSCANNED":
                is_pcs_short = True
            elif combined_notes and _PCS_SHORT_NOTE_RE.search(combined_notes) and not _PCS_SURPLUS_NOTE_RE.search(combined_notes):
                is_pcs_short = True

        if not (is_box_short or is_pcs_short):
            continue

        pcs_short_qty_str = None
        if is_box_short:
            short_mag = max(0.0, eff_book - float(row["counted_qty"]))
            if short_mag <= 0.0:
                short_mag = abs(float(diff_val))
            pair_str = f"{lbl_int}→{cnt_int}"
        else:
            # PCS loose-pick shortage when box count matches (0.5 ranks below >=1 whole-box shortage on tie-break)
            short_mag = 0.5
            notes_for_qty = f"{row['field_notes'] or ''} {row['notes'] or ''}".strip()
            m_qty = _re_out_guard.search(
                r"(?<!\+)\b(\d[\d,]*)\s*(?:pcs?|ea)\s*(?:short|shortage|missing|under)\b",
                notes_for_qty,
                flags=_re_out_guard.IGNORECASE,
            )
            if not m_qty:
                m_qty = _re_out_guard.search(
                    r"\b(?:short|shortage|missing|under)\s*(?:by\s*)?(\d[\d,]*)\s*(?:pcs?|ea)\b",
                    notes_for_qty,
                    flags=_re_out_guard.IGNORECASE,
                )
            if m_qty:
                pcs_short_qty_str = m_qty.group(1)
            if lbl_int == cnt_int:
                box_noun = "box" if cnt_int == 1 else "boxes"
                if pcs_short_qty_str:
                    pair_str = f"{cnt_int} {box_noun}, {pcs_short_qty_str} pcs short"
                else:
                    pair_str = f"{cnt_int} {box_noun} but short in pcs"
            else:
                pair_str = f"{lbl_int}→{cnt_int}"

        short_by_bin.setdefault(b_name, []).append(
            {
                "run_idx": run_order_idx.get(row["run_id"], 0),
                "lbl_int": lbl_int,
                "cnt_int": cnt_int,
                "is_pcs_same_box": bool(is_pcs_short and lbl_int == cnt_int),
                "pair_str": pair_str,
                "short_mag": short_mag,
                "zone_code": z_code,
                "w_days_cap": w_days_cap,
            }
        )

    qualified: List[Tuple[int, float, float, str, str]] = []
    for b_name, events in short_by_bin.items():
        events.sort(key=lambda e: e["run_idx"])
        k = len(events)
        if k < min_short_count:
            continue
        w_days_cap = events[-1]["w_days_cap"]
        if w_days_cap is not None:
            denom = sum(
                1
                for r in ordered_runs
                if (anchor_dt - datetime.date.fromisoformat(str(r["run_date"])[:10])).days <= w_days_cap
            )
            denom = max(k, min(int(recent_runs_window), denom))
        else:
            denom = int(recent_runs_window)
        latest_short = float(events[-1]["short_mag"])
        total_short = float(sum(e["short_mag"] for e in events))
        # #221 F-1: When any event has matching box count + pcs shortage, spell out "boxes" explicitly and join with '; '
        if any(e.get("is_pcs_same_box") for e in events):
            rendered_events = []
            for e in events:
                if e.get("is_pcs_same_box"):
                    rendered_events.append(e["pair_str"])
                else:
                    b_noun = "box" if int(e["cnt_int"]) == 1 else "boxes"
                    rendered_events.append(f"{e['lbl_int']}→{e['cnt_int']} {b_noun}")
            pairs_joined = "; ".join(rendered_events)
        else:
            pairs_joined = ", ".join(e["pair_str"] for e in events)
        line = (
            f"{b_name}: short {k} of the last {denom} counts ({pairs_joined}). "
            f"Please check boxes leaving this bin are scanned out (GI) at pick time."
        )
        qualified.append((k, latest_short, total_short, b_name, line))

    qualified.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [item[4] for item in qualified[:max_lines]]


if __name__ == "__main__":
    init_database()
    print("Database initialized successfully at:", DB_PATH)


