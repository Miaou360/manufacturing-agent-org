"""
Turnover Days & Material Ontology Engine (01_Inventory_Team / 00_StockAccuracy)
WMS GR(입고) 및 GI(출고/이동) 데이터를 기반으로 바코드별 체류일수 계산,
미입고 출고(Orphan GI) 예외 처리, Trimmed Mean 회전일수 산출 및
온톨로지 마스터(JSON/DB)를 생성·업데이트합니다.
"""

import os
import sys
import json
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
import pandas as pd
import numpy as np

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from db_manager import get_connection, ONTOLOGY_JSON_PATH, DB_PATH

SMALL_PART_KEYWORDS = ["SCREW", "NUT", "AIMING", "AMING", "CLIP", "WASHER", "PIN"]


def update_barcode_lifecycles(conn: sqlite3.Connection, chunk_size: int = 50000) -> Dict[str, int]:
    """
    GR과 GI 트랜잭션을 벡터화(Vectorized) 매칭하여 BARCODE_LIFECYCLE 테이블 고속 업데이트.
    - 입고 취소(102), 이동 취소(312) 반영
    - 생산 출고 위치(SP31, SP11) 필터링
    - 미입고 출고(Orphan GI) 감지 및 분류
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("[OntologyEngine] 1. 바코드 수명주기(Lifecycle) 벡터화 분석 시작...")

    # Load GR records (valid 101, excluding 102 cancelled)
    gr_df = pd.read_sql_query("""
        SELECT barcode, mat_no, mat_desc, gr_loc, gr_time, move_type
        FROM GR_TRANSACTIONS
    """, conn)
    cancelled_gr_barcodes = set(gr_df[gr_df["move_type"] == "102"]["barcode"])
    valid_gr_df = gr_df[(gr_df["move_type"] == "101") & (~gr_df["barcode"].isin(cancelled_gr_barcodes))].drop_duplicates("barcode", keep="last")

    # Load GI records (valid production issues)
    gi_df = pd.read_sql_query("""
        SELECT barcode, mat_no, mat_desc, gi_loc, gr_loc, gi_time, move_type, lot_no
        FROM GI_TRANSACTIONS
    """, conn)
    cancelled_gi_barcodes = set(gi_df[gi_df["move_type"] == "312"]["barcode"])
    valid_gi_df = gi_df[(gi_df["move_type"] != "312") & (~gi_df["barcode"].isin(cancelled_gi_barcodes))]
    prod_gi_df = valid_gi_df[valid_gi_df["gr_loc"].isin(["SP31", "SP11"])].sort_values("gi_time").drop_duplicates("barcode", keep="first")

    # Outer merge to reconcile all barcodes
    merged = pd.merge(
        valid_gr_df[["barcode", "mat_no", "mat_desc", "gr_time", "gr_loc"]],
        prod_gi_df[["barcode", "mat_no", "mat_desc", "gi_time", "gr_loc", "lot_no"]],
        on="barcode",
        how="outer",
        suffixes=("_gr", "_gi")
    )

    merged["mat_no"] = merged["mat_no_gr"].combine_first(merged["mat_no_gi"])
    merged["mat_desc"] = merged["mat_desc_gr"].combine_first(merged["mat_desc_gi"])
    merged["gr_loc"] = merged["gr_loc_gr"]

    has_gr = merged["gr_time"].notna()
    has_gi = merged["gi_time"].notna()

    gr_dt = pd.to_datetime(merged["gr_time"])
    gi_dt = pd.to_datetime(merged["gi_time"])
    diff_sec = (gi_dt - gr_dt).dt.total_seconds()

    merged["dwell_hours"] = np.where(has_gr & has_gi, (diff_sec / 3600.0).round(2), None)
    merged["dwell_days"] = np.where(has_gr & has_gi, (diff_sec / 86400.0).round(2), None)

    conditions = [has_gr & has_gi, has_gr & ~has_gi, ~has_gr & has_gi]
    choices = ["PRODUCED", "IN_STORAGE", "ORPHAN_GI"]
    merged["status"] = np.select(conditions, choices, default="UNKNOWN")
    merged["is_orphan"] = np.where(~has_gr & has_gi, 1, 0)
    merged["updated_at"] = now_str

    produced_count = int((merged["status"] == "PRODUCED").sum())
    in_storage_count = int((merged["status"] == "IN_STORAGE").sum())
    orphan_gi_count = int((merged["status"] == "ORPHAN_GI").sum())

    stats = {
        "total_barcodes": len(merged),
        "produced_count": produced_count,
        "in_storage_count": in_storage_count,
        "orphan_gi_count": orphan_gi_count
    }

    # Prepare rows for batch insert
    export_cols = [
        "barcode", "mat_no", "mat_desc", "gr_time", "gr_loc",
        "gi_time", "gr_loc_gi", "dwell_hours", "dwell_days",
        "status", "lot_no", "is_orphan", "updated_at"
    ]
    export_df = merged[export_cols].replace({np.nan: None})
    records_to_upsert = export_df.values.tolist()

    cur = conn.cursor()
    # Batch chunk upsert
    total_records = len(records_to_upsert)
    print(f"   - 총 {total_records:,}건 수명주기 데이터 DB 적재 시작 (청크 크기: {chunk_size:,})...")

    for i in range(0, total_records, chunk_size):
        chunk = records_to_upsert[i: i + chunk_size]
        cur.executemany("""
        INSERT INTO BARCODE_LIFECYCLE (
            barcode, mat_no, mat_desc, gr_time, gr_loc, production_gi_time,
            production_gi_loc, dwell_hours, dwell_days, status, lot_no,
            is_orphan, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            mat_no = excluded.mat_no,
            mat_desc = excluded.mat_desc,
            gr_time = excluded.gr_time,
            gr_loc = excluded.gr_loc,
            production_gi_time = excluded.production_gi_time,
            production_gi_loc = excluded.production_gi_loc,
            dwell_hours = excluded.dwell_hours,
            dwell_days = excluded.dwell_days,
            status = excluded.status,
            lot_no = excluded.lot_no,
            is_orphan = excluded.is_orphan,
            updated_at = excluded.updated_at
        """, chunk)
        conn.commit()

    print(f"   - 총 바코드: {stats['total_barcodes']:,}건")
    print(f"   - 생산 출고 완료(매칭): {stats['produced_count']:,}건")
    print(f"   - 현재 창고 보관 중: {stats['in_storage_count']:,}건")
    print(f"   - 미입고 출고(Orphan GI): {stats['orphan_gi_count']:,}건 (회전일수 왜곡 방지를 위해 통계에서 분리)")
    return stats


def compute_trimmed_mean(series: pd.Series, trim_percent: float = 0.10) -> float:
    """상·하위 지정 비율(기본 10%)을 절사한 Trimmed Mean 계산"""
    clean = series.dropna().sort_values().values
    n = len(clean)
    if n == 0:
        return 0.0
    if n < 5:
        return float(np.mean(clean))
    k = int(n * trim_percent)
    if 2 * k >= n:
        return float(np.median(clean))
    trimmed = clean[k: n - k]
    return float(np.mean(trimmed))


def build_material_ontology(conn: sqlite3.Connection) -> Dict[str, Any]:
    """
    BARCODE_LIFECYCLE 데이터를 바탕으로 품번(Mat.No)별
    Turnover Days 통계 및 도메인 지식(소물류, 오스캔 위험도)을 계산하여
    DB 및 JSON 온톨로지 마스터에 저장 (벡터화 초고속 연산)
    """
    print("\n[OntologyEngine] 2. 품번별 온톨로지(Material Ontology) 생성 중...")

    df = pd.read_sql_query("""
        SELECT mat_no, mat_desc, dwell_days, status, is_orphan
        FROM BARCODE_LIFECYCLE
        WHERE mat_no IS NOT NULL AND mat_no != ''
    """, conn)

    if df.empty:
        return {}

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ontology_dict: Dict[str, Any] = {}
    db_rows = []

    # 전체 수량 및 Orphan 집계
    total_counts = df.groupby("mat_no").size()
    orphan_counts = df.groupby("mat_no")["is_orphan"].sum()
    first_desc = df.groupby("mat_no")["mat_desc"].first()

    # PRODUCED 바코드 기준 dwell_days 통계 집계
    prod_df = df[df["status"] == "PRODUCED"].dropna(subset=["dwell_days"])
    agg_stats = prod_df.groupby("mat_no")["dwell_days"].agg(
        produced_boxes="count",
        mean_days="mean",
        median_days="median",
        min_days="min",
        max_days="max",
        std_days="std"
    )

    merged_ont = pd.DataFrame({
        "total_boxes": total_counts,
        "orphan_boxes": orphan_counts,
        "mat_desc": first_desc
    }).join(agg_stats, how="left")

    merged_ont["produced_boxes"] = merged_ont["produced_boxes"].fillna(0).astype(int)
    merged_ont["orphan_boxes"] = merged_ont["orphan_boxes"].fillna(0).astype(int)
    merged_ont["orphan_rate"] = (merged_ont["orphan_boxes"] / merged_ont["total_boxes"]).round(4)
    merged_ont["mean_days"] = merged_ont["mean_days"].round(2)
    merged_ont["median_days"] = merged_ont["median_days"].round(2)
    merged_ont["min_days"] = merged_ont["min_days"].round(2)
    merged_ont["max_days"] = merged_ont["max_days"].round(2)
    merged_ont["std_days"] = merged_ont["std_days"].fillna(0.0).round(2)

    # Trimmed mean 계산 및 온톨로지 딕셔너리 구축
    # prod_df groupby dictionary for fast trimmed mean
    prod_grouped = prod_df.groupby("mat_no")["dwell_days"]

    for mat_no, row in merged_ont.iterrows():
        desc = str(row["mat_desc"]) if pd.notnull(row["mat_desc"]) else ""
        total_boxes = int(row["total_boxes"])
        orphan_boxes = int(row["orphan_boxes"])
        orphan_rate = float(row["orphan_rate"])
        produced_boxes = int(row["produced_boxes"])

        desc_upper = desc.upper()
        is_small_part = 1 if any(kw in desc_upper for kw in SMALL_PART_KEYWORDS) else 0
        hand_pick_risk = 1 if is_small_part else 0

        if produced_boxes >= 1:
            mean_days = float(row["mean_days"])
            median_days = float(row["median_days"])
            min_days = float(row["min_days"])
            max_days = float(row["max_days"])
            std_days = float(row["std_days"])

            # Compute trimmed mean
            dwell_vals = prod_grouped.get_group(mat_no)
            trimmed_days = round(compute_trimmed_mean(dwell_vals, 0.10), 2)

            if trimmed_days <= 3.0:
                mover_class = "FAST_MOVER"
            elif trimmed_days <= 14.0:
                mover_class = "NORMAL"
            elif trimmed_days <= 45.0:
                mover_class = "SLOW_MOVER"
            else:
                mover_class = "STAGNANT"
        else:
            mean_days = None
            trimmed_days = None
            median_days = None
            min_days = None
            max_days = None
            std_days = None
            mover_class = "PENDING_FIRST_GI"

        item_ontology = {
            "mat_no": mat_no,
            "mat_desc": desc,
            "total_boxes_tracked": total_boxes,
            "produced_boxes": produced_boxes,
            "orphan_boxes": orphan_boxes,
            "orphan_rate": orphan_rate,
            "turnover_profile": {
                "mean_days": mean_days,
                "trimmed_mean_days": trimmed_days,
                "median_days": median_days,
                "min_days": min_days,
                "max_days": max_days,
                "std_days": std_days,
                "mover_class": mover_class
            },
            "shop_floor_rules": {
                "is_small_part": bool(is_small_part),
                "hand_pick_risk": bool(hand_pick_risk),
                "supermarket_rapid_turnover": bool(mover_class == "FAST_MOVER")
            },
            "last_updated": now_str
        }

        ontology_dict[mat_no] = item_ontology

        db_rows.append((
            mat_no, desc, total_boxes, produced_boxes, orphan_boxes, orphan_rate,
            mean_days, trimmed_days, median_days, min_days, max_days, std_days,
            mover_class, is_small_part, hand_pick_risk, now_str
        ))

    # Save to SQLite table
    cur = conn.cursor()
    cur.execute("DELETE FROM MATERIAL_ONTOLOGY")
    cur.executemany("""
    INSERT INTO MATERIAL_ONTOLOGY (
        mat_no, mat_desc, total_boxes, produced_boxes, orphan_boxes, orphan_rate,
        mean_turnover_days, trimmed_mean_days, median_turnover_days, min_turnover_days,
        max_turnover_days, std_turnover_days, mover_class, is_small_part,
        hand_pick_risk, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, db_rows)
    conn.commit()

    # Save to JSON
    with open(ONTOLOGY_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(ontology_dict, f, ensure_ascii=False, indent=2)

    print(f"🎉 온톨로지 구축 완료! 총 {len(ontology_dict):,}개 품번 등록")
    print(f"   - JSON 파일: {ONTOLOGY_JSON_PATH}")
    print(f"   - SQLite 테이블: MATERIAL_ONTOLOGY ({DB_PATH.name})")

    return ontology_dict


def run_full_ontology_update(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """DB 연결 및 전체 수명주기/온톨로지 업데이트 파이프라인 실행 (요약 통계 반환)"""
    close_conn = False
    if conn is None:
        conn = get_connection()
        close_conn = True

    try:
        lifecycle_stats = update_barcode_lifecycles(conn)
        ontology_dict = build_material_ontology(conn)
        return {
            "total_materials_in_ontology": len(ontology_dict),
            "lifecycle_stats": lifecycle_stats,
            "ontology": ontology_dict
        }
    finally:
        if close_conn:
            conn.close()


if __name__ == "__main__":
    run_full_ontology_update()
