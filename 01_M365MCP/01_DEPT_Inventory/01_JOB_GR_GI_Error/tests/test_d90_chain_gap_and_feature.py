import sys
import sqlite3
from pathlib import Path
import pandas as pd
import pytest

BASE_01 = Path(__file__).resolve().parent.parent
BASE_00 = BASE_01.parent / "00_JOB_StockAccuracy"
HQ_DIR = BASE_01.parent.parent / "00_HQ_TaskManager"

for p in [str(HQ_DIR), str(BASE_01), str(BASE_00)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from ztcode_stock_guard import (
    get_gr_chain_protected_slocs,
    has_gr_chain_gap,
    check_movement_ztcode_stock_rec_block,
)
from gr_gi_resolver import resolve_material_errors_unified
import importlib.util

_spec01 = importlib.util.spec_from_file_location("job01_db", BASE_01 / "db_manager.py")
db01 = importlib.util.module_from_spec(_spec01)
_spec01.loader.exec_module(db01)

_spec00 = importlib.util.spec_from_file_location("job00_db", BASE_00 / "db_manager.py")
db00 = importlib.util.module_from_spec(_spec00)
_spec00.loader.exec_module(db00)

_spec_bat = importlib.util.spec_from_file_location("job00_bat", BASE_00 / "box_aging_tracker.py")
bat_mod = importlib.util.module_from_spec(_spec_bat)
_spec_bat.loader.exec_module(bat_mod)

_spec_hrc = importlib.util.spec_from_file_location("job00_hrc", BASE_00 / "hit_rate_calculator.py")
hrc_mod = importlib.util.module_from_spec(_spec_hrc)
_spec_hrc.loader.exec_module(hrc_mod)


def test_d90_1_full_chain_protection_in_01_and_02():
    """
    D90-1: When a material has GR(SL00) + GI(SL00->SL01) + GI(SL01->SP31),
    both SL00->SL01 and SL01->SP31 are protected as GR-dependent (PROTECTED_GR_DEPENDENT in 01_JOB,
    BLOCKED_BY_GR_ERROR in 02_JOB).
    """
    mat = "99000032-V"
    active_gr = {
        mat: {
            "Material": mat,
            "Total_GR_Qty": 120.0,
            "Count": 1,
            "Vendors": {"V100"},
            "SLocs": {"SL00"},
        }
    }
    active_gi = {
        mat: {
            "Material": mat,
            "Total_GI_Qty": 240.0,
            "Count": 2,
            "FromSLocs": {"SL00", "SL01"},
            "ToSLocs": {"SL01", "SP31"},
            "SrcSLocs": {"SL00", "SL01"},
            "DstSLocs": {"SL01", "SP31"},
            "Edges": {("SL00", "SL01"), ("SL01", "SP31")},
            "Mvts": {"311"},
        }
    }

    protected = get_gr_chain_protected_slocs(mat, active_gr, active_gi)
    assert protected == {"SL00", "SL01", "SP31"}
    assert has_gr_chain_gap(mat, active_gr, active_gi) is False

    # 02_JOB movement check on SL01 -> SP31 is BLOCKED_BY_GR_ERROR
    is_blk, status, _, _, _ = check_movement_ztcode_stock_rec_block(mat, "SL01", "SP31", active_gr, active_gi)
    assert is_blk is True
    assert status == "BLOCKED_BY_GR_ERROR"

    # 01_JOB resolver marks BOTH rows as PROTECTED_GR_DEPENDENT
    gi_rows = pd.DataFrame([
        {"Material Number": mat, "Mvt": "311", "FromSt": "SL00", "TOSt": "SL01", "Qty": "120", "Date": "2026.10.28", "Message": "Insufficient stock"},
        {"Material Number": mat, "Mvt": "311", "FromSt": "SL01", "TOSt": "SP31", "Qty": "120", "Date": "2026.10.28", "Message": "Insufficient stock"},
    ])
    stock_comp = pd.DataFrame([
        {"Material": mat, "SLoc": "SL02", "SAP_Stock": 500.0, "WMS_Stock": 0.0, "Surplus": 500.0},
    ])
    plan = resolve_material_errors_unified(
        mat=mat,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials={mat},
        today_str="2026.10.28",
        prev_month_str="2026.09.30",
        active_gr_errors=active_gr,
        active_gi_errors=active_gi,
    )
    assert len(plan) == 2
    assert all(r["Status"] == "PROTECTED_GR_DEPENDENT" for r in plan)


def test_d90_2_chain_gap_and_total_mismatch_dedup(tmp_path):
    """
    D90-2:
    - GR(SL00) + GI(SL01->SP31) WITHOUT GI(SL00->SL01) is NOT protected as PROTECTED_GR_DEPENDENT,
      and is recorded as signal_type='CHAIN_GAP'.
    - Multiple runs on the same day deduplicate to 1 row per (signal_date, material, signal_type),
      preserving first_observed_at and updating last_observed_at / run_id.
    """
    sig_db = tmp_path / "gr_gi_test.db"
    mat_cg = "99000023"
    active_gr = {
        mat_cg: {
            "Material": mat_cg,
            "Total_GR_Qty": 400.0,
            "Count": 1,
            "Vendors": {"V040"},
            "SLocs": {"SL00"},
        }
    }
    active_gi = {
        mat_cg: {
            "Material": mat_cg,
            "Total_GI_Qty": 100.0,
            "Count": 1,
            "FromSLocs": {"SL01"},
            "ToSLocs": {"SP31"},
            "SrcSLocs": {"SL01"},
            "DstSLocs": {"SP31"},
            "Edges": {("SL01", "SP31")},
            "Mvts": {"311"},
        }
    }
    assert has_gr_chain_gap(mat_cg, active_gr, active_gi) is True

    gi_rows = pd.DataFrame([
        {"Material Number": mat_cg, "Mvt": "311", "FromSt": "SL01", "TOSt": "SP31", "Qty": "100", "Date": "2026.10.28", "Message": "Insufficient stock"},
    ])
    stock_comp = pd.DataFrame([
        {"Material": mat_cg, "SLoc": "SL01", "SAP_Stock": 0.0, "WMS_Stock": 200.0, "Surplus": -200.0},
    ])

    plan1 = resolve_material_errors_unified(
        mat=mat_cg,
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials={mat_cg},
        today_str="2026.10.28",
        prev_month_str="2026.09.30",
        active_gr_errors=active_gr,
        active_gi_errors=active_gi,
        run_id="RUN_20261028_090000",
        signal_db_path=sig_db,
    )
    assert len(plan1) == 1
    assert plan1[0]["Status"] != "PROTECTED_GR_DEPENDENT"
    assert plan1[0].get("Is_Chain_Gap") is True

    # Record plan signals (includes CHAIN_GAP and TOTAL_MISMATCH)
    db01.record_plan_stock_gap_signals("RUN_20261028_090000", plan1, db_path=sig_db)
    # Second run on same date should update in-place, not duplicate
    db01.record_plan_stock_gap_signals("RUN_20261028_093000", plan1, db_path=sig_db)

    with sqlite3.connect(str(sig_db)) as conn:
        rows = conn.execute(
            "SELECT signal_date, material, signal_type, run_id, first_observed_at, last_observed_at FROM GI_STOCK_GAP_SIGNALS ORDER BY signal_type"
        ).fetchall()

    # Exactly 1 row for CHAIN_GAP and 1 row for TOTAL_MISMATCH on 2026-10-28
    by_type = {r[2]: r for r in rows}
    assert "CHAIN_GAP" in by_type
    assert "TOTAL_MISMATCH" in by_type
    assert len(rows) == 2
    assert by_type["CHAIN_GAP"][0] == "2026-10-28"
    assert by_type["CHAIN_GAP"][3] == "RUN_20261028_093000"


def test_d90_3_feature_shadow_weight_zero_and_stage1_rule(tmp_path):
    """
    D90-3 (Amended by Thread 146):
    - CHAIN_GAP is fed into 00_JOB as a feature (Chain_Gap_Flag, Chain_Gap_Days) with initial weight 0.
    - Ranking and Risk_Score do not change (0 points added).
    - Selected bin with Chain_Gap_Flag displays 'GR Chain Gap' in Reason / Risk_Categories.
    - AUDIT_FEATURE_SNAPSHOT persists chain_gap_flag and chain_gap_days, and links audit_run_id to GI_STOCK_GAP_SIGNALS.
    - Stage 1 hit_rate_calculator tracks GR_CHAIN_GAP with Beta(1, 3).
    """
    sig_db = tmp_path / "gr_gi_signals.db"
    acc_db = tmp_path / "inventory_history.db"

    # Seed 2 consecutive days of CHAIN_GAP for 99000023
    db01.save_gi_stock_gap_signal({
        "run_id": "RUN_20261027_150000",
        "signal_date": "2026-10-27",
        "material": "99000023",
        "mvt": "311",
        "error_from": "SL01",
        "error_to": "SP31",
        "error_qty": 100.0,
        "sap_total": 0.0,
        "wms_total": 200.0,
        "total_gap": 200.0,
        "signal_type": "CHAIN_GAP",
    }, db_path=sig_db)
    db01.save_gi_stock_gap_signal({
        "run_id": "RUN_20261028_063000",
        "signal_date": "2026-10-28",
        "material": "99000023",
        "mvt": "311",
        "error_from": "SL01",
        "error_to": "SP31",
        "error_qty": 100.0,
        "sap_total": 0.0,
        "wms_total": 200.0,
        "total_gap": 200.0,
        "signal_type": "CHAIN_GAP",
    }, db_path=sig_db)

    cg_feed = db00.get_active_chain_gap_feed(gi_db_path=sig_db, run_date="2026-10-28")
    assert "99000023" in cg_feed
    assert cg_feed["99000023"]["chain_gap_flag"] == 1
    assert cg_feed["99000023"]["consecutive_days"] == 2

    # Build synthetic WMS stock list with 2 bins:
    # A01-01-1 has 99000023 (SMT stagnant -> existing rule triggers)
    # B01-01-1 has 99000043 (only normal fresh stock -> does NOT trigger any rule even if in chain_gap_feed)
    cg_feed["99000043"] = {"material": "99000043", "chain_gap_flag": 1, "consecutive_days": 1}

    df_wms = pd.DataFrame([
        {
            "Barcode": "BC_SMT_1",
            "Mat.No": "99000023",
            "Mat.Desc": "TEST LENS",
            "BIN": "SMT-01",
            "Qty.": 100.0,
            "GR Date": "2026-09-01",
            "GR_Date": pd.Timestamp("2026-09-01").date(),
            "Lot.No": "260901",
            "Lot_Date": pd.Timestamp("2026-09-01").date(),
            "Lot_Age_Days": 57,
            "Warehouse_Dwell_Days": 30,
            "Bin_Dwell_Days": 15,
            "Ontology_Turnover_Days": 5.0,
            "Mover_Class": "FAST",
        },
        {
            "Barcode": "BC_NORM_2",
            "Mat.No": "99000043",
            "Mat.Desc": "FRESH PART",
            "BIN": "R01A 1",
            "Qty.": 50.0,
            "GR Date": "2026-10-28",
            "GR_Date": pd.Timestamp("2026-10-28").date(),
            "Lot.No": "261028",
            "Lot_Date": pd.Timestamp("2026-10-28").date(),
            "Lot_Age_Days": 0,
            "Warehouse_Dwell_Days": 0,
            "Bin_Dwell_Days": 1,
            "Ontology_Turnover_Days": 10.0,
            "Mover_Class": "NORMAL",
        },
    ])

    tracker_base = bat_mod.BoxAgingTracker()
    tracker_base.chain_gap_feed = {}
    res_base = tracker_base.detect_mis_scans(df_wms, mode="Daily", run_date="2026-10-28")

    tracker_cg = bat_mod.BoxAgingTracker()
    tracker_cg.chain_gap_feed = cg_feed
    res_cg = tracker_cg.detect_mis_scans(df_wms, mode="Daily", run_date="2026-10-28")

    # Ranking and Risk_Score are 100% identical (weight = 0)
    assert [b["BIN"] for b in res_base["top_bins"]] == [b["BIN"] for b in res_cg["top_bins"]]
    assert [b["Risk_Score"] for b in res_base["top_bins"]] == [b["Risk_Score"] for b in res_cg["top_bins"]]

    # SMT-01 has GR Chain Gap (2d) in Reason and Chain_Gap_Flag=1, Chain_Gap_Days=2 in suspects
    top_bin = res_cg["top_bins"][0]
    assert "GR Chain Gap (2d)" in top_bin["Reason"]
    s_row = res_cg["suspects"].iloc[0]
    assert s_row["Chain_Gap_Flag"] == 1
    assert s_row["Chain_Gap_Days"] == 2
    assert "GR_CHAIN_GAP" in s_row["Risk_Categories"]

    # Save ledger & features and verify DB columns + audit_run_id link
    db00.save_audit_ledger_and_features(
        run_info={"run_id": "20261028_DAILY", "run_date": "2026-10-28", "mode": "Daily"},
        top_bins=res_cg["top_bins"],
        extended_bins=res_cg["extended_bins"],
        suspects_df=res_cg["suspects"],
        random_bins=res_cg["random_bins"],
        db_path=acc_db,
        gi_signal_db_path=sig_db,
    )

    with sqlite3.connect(str(acc_db)) as conn:
        fs_row = conn.execute(
            "SELECT chain_gap_flag, chain_gap_days FROM AUDIT_FEATURE_SNAPSHOT WHERE run_id='20261028_DAILY' AND mat_no='99000023'"
        ).fetchone()
    assert fs_row == (1, 2)

    with sqlite3.connect(str(sig_db)) as conn:
        linked = conn.execute(
            "SELECT audit_run_id FROM GI_STOCK_GAP_SIGNALS WHERE material='99000023' AND signal_type='CHAIN_GAP' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert linked[0] == "20261028_DAILY"

    # Verify Stage 1 hit_rate_calculator includes GR_CHAIN_GAP
    assert "GR_CHAIN_GAP" in hrc_mod.CANONICAL_RULES
