"""
Unit tests for D114 Stage-Flow Shadow Engine & B-4 / #217 / #237 fixes.
"""

from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pytest

JOB_DIR = Path(__file__).resolve().parent.parent
DEPT_DIR = JOB_DIR.parent
for p in (str(JOB_DIR), str(DEPT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from box_aging_tracker import get_bin_characteristics, normalize_wms_sloc
from d114_stage_flow_shadow import (
    classify_d114_stage,
    init_shadow_db,
    ingest_snapshot_dataframe,
    ingest_wms_parts_stack_incremental,
    recompute_profiles_and_transitions,
    compute_and_save_shadow_signals,
    kaplan_meier_quantiles,
)


def test_b4_wms_mapping_unification_and_resin_regex_fix():
    assert normalize_wms_sloc("CKD") == "SL02"
    assert normalize_wms_sloc("Raw material") == "SL03"
    assert normalize_wms_sloc("Main parts") == "SL01"
    assert normalize_wms_sloc("Receipt Goods SL") == "SL00"
    assert normalize_wms_sloc("DMA") == "SL49"

    resin_info = get_bin_characteristics("RESIN")
    assert resin_info["is_plant3"] is False
    assert resin_info["is_plant1"] is False
    assert resin_info["is_non_physical_or_excluded"] is True

    r_info = get_bin_characteristics("R51Z 3")
    assert r_info["is_plant3"] is True
    assert r_info["is_plant1"] is False


def test_d114_stage_mapping_a1_through_a6_and_exclusions():
    pfxs = ("SMT", "SMD", "FLR", "OEM")

    assert classify_d114_stage("CKD", "R52Z 4", pfxs) == ("S1", "SL02")
    assert classify_d114_stage("Main parts", "54Z 4", pfxs) == ("S2", "SL01")
    assert classify_d114_stage("Main parts", "69Z 2", pfxs) == ("S3", "SL01")
    assert classify_d114_stage("Main parts", "SMT 9947", pfxs) == ("S4", "SL01")
    assert classify_d114_stage("Main parts", "FLR 129", pfxs) == ("S4", "SL01")
    assert classify_d114_stage("Main parts", "CRT 9926", pfxs) == ("S5", "SL01")
    assert classify_d114_stage("Main parts", "PRO 12", pfxs) == ("S5", "SL01")

    assert classify_d114_stage("Main parts", "R55Z 3", pfxs) == ("S1_TO_P1_TRANSIT", "SL01")
    assert classify_d114_stage("CKD", "59Z 3", pfxs) == ("S2", "SL02")
    assert classify_d114_stage("CKD", "58Z 1", pfxs) == ("S3", "SL02")
    assert classify_d114_stage("CKD", "SMT 9948", pfxs) == ("S4", "SL02")
    assert classify_d114_stage("CKD", "CRT 9927", pfxs) == ("S5", "SL02")

    assert classify_d114_stage("Main parts", "S51Z 5", pfxs) == ("EXCLUDED", "SL01")
    assert classify_d114_stage("Main parts", "S1A 1", pfxs) == ("EXCLUDED", "SL01")
    assert classify_d114_stage("Raw material", "RESIN", pfxs) == ("EXCLUDED", "SL03")
    assert classify_d114_stage("DMA", "68Z 5", pfxs) == ("EXCLUDED", "SL49")
    assert classify_d114_stage("Receipt Goods SL", "68Z 5", pfxs) == ("S2", "SL00")
    assert classify_d114_stage("Receipt Goods SL", "FLR 132", pfxs) == ("S4", "SL00")
    assert classify_d114_stage("Receipt Goods SL", "", pfxs) == ("PRE_PUTAWAY", "SL00")
    assert classify_d114_stage("CKD", "SCR 1", pfxs) == ("EXCLUDED", "SL02")
    assert classify_d114_stage("Main parts", "NG", pfxs) == ("EXCLUDED", "SL01")


def test_d114_incremental_ledger_km_shrinkage_overtake_and_zero_score_impact(tmp_path):
    shadow_db = tmp_path / "d114_test.db"
    conn = init_shadow_db(shadow_db)
    try:
        df0 = pd.DataFrame([
            {"Barcode": "BC-INIT", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261025", "Lot No": "261025"},
        ])
        ingest_snapshot_dataframe(conn, df0, "2026-10-27 10:00:00", "snap0.xlsx", floor_prefixes=("SMT", "FLR"))

        rows_t1 = [
            {"Barcode": "BC-INIT", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261025", "Lot No": "261025"},
            {"Barcode": "BC-STUCK", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
        ] + [
            {"Barcode": f"BC-NORM-{i}", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "55Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"}
            for i in range(6)
        ]
        ingest_snapshot_dataframe(conn, pd.DataFrame(rows_t1), "2026-10-27 11:00:00", "snap1.xlsx", floor_prefixes=("SMT", "FLR"))

        rows_t2 = [
            {"Barcode": "BC-INIT", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261025", "Lot No": "261025"},
            {"Barcode": "BC-STUCK", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
            {"Barcode": "BC-NEWER-1", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "55Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
            {"Barcode": "BC-NEWER-2", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "55Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
        ] + [
            {"Barcode": f"BC-NORM-{i}", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "SMT 9947", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"}
            for i in range(6)
        ]
        ingest_snapshot_dataframe(conn, pd.DataFrame(rows_t2), "2026-10-27 12:00:00", "snap2.xlsx", floor_prefixes=("SMT", "FLR"))

        rows_t3 = [
            {"Barcode": "BC-INIT", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261025", "Lot No": "261025"},
            {"Barcode": "BC-STUCK", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
            {"Barcode": "BC-NEWER-1", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "CRT 10", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
        ]
        ingest_snapshot_dataframe(conn, pd.DataFrame(rows_t3), "2026-10-27 13:30:00", "snap3.xlsx", floor_prefixes=("SMT", "FLR"))

        rows_t4 = [
            {"Barcode": "BC-INIT", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261025", "Lot No": "261025"},
            {"Barcode": "BC-STUCK", "Mat.No": "MAT-A", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 50, "GR Date": "20261027", "Lot No": "261027"},
        ]
        ingest_snapshot_dataframe(conn, pd.DataFrame(rows_t4), "2026-10-27 19:30:00", "snap4.xlsx", floor_prefixes=("SMT", "FLR"))
    finally:
        conn.close()

    prof = recompute_profiles_and_transitions(db_path=shadow_db)
    assert prof["profiles"] > 0
    assert prof["transitions"] > 0

    sig = compute_and_save_shadow_signals(
        run_date="2026-10-28",
        selected_bins_by_mode={"Daily": ["51Z 1"]},
        db_path=shadow_db,
    )
    assert sig["daily_suspect_bins"] == 1
    top_bin = sig["top_daily"][0]
    assert top_bin[0] == "51Z 1"
    assert top_bin[1] == "S3"
    assert top_bin[3] > 1.0
    assert top_bin[4] >= 2

    conn = init_shadow_db(shadow_db)
    try:
        w_vals = [r[0] for r in conn.execute("SELECT shadow_weight FROM D114_SHADOW_BIN_DAILY").fetchall()]
        assert all(w == 0.0 for w in w_vals)
    finally:
        conn.close()


def test_d114_max_snapshot_ts_stepwise_ingest(tmp_path):
    stack = tmp_path / "wms_parts_stack"
    stack.mkdir()
    shadow_db = tmp_path / "d114_step.db"

    for ts in ("20261103_063000", "20261103_070200", "20261103_080000"):
        df = pd.DataFrame([
            {"Barcode": "BC-1", "Mat.No": "MAT-1", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 10, "GR Date": "20261101", "Lot No": "L1"},
        ])
        fp = stack / f"WMS_StockList_TEST_{ts}.xlsx"
        df.to_excel(fp, index=False)

    r1 = ingest_wms_parts_stack_incremental(stack_dir=stack, db_path=shadow_db, max_snapshot_ts="2026-11-03 07:05:00")
    assert r1["newly_processed_files"] == 2
    assert r1["last_snapshot_ts"] == "2026-11-03 07:02:00"

    r2 = ingest_wms_parts_stack_incremental(stack_dir=stack, db_path=shadow_db)
    assert r2["newly_processed_files"] == 1
    assert r2["last_snapshot_ts"] == "2026-11-03 08:00:00"
