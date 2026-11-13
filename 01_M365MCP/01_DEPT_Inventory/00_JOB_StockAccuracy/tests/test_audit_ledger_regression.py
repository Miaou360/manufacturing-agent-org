"""
Audit Ledger & Point-in-Time Feature Snapshot Regression Tests
=============================================================
Department: 01_DEPT_Inventory / 00_JOB_StockAccuracy

Tests:
1. test_score_breakdown_identity: Verifies that for 100% of suspects in both Daily and Weekly
   modes, sum(Score_Breakdown.values()) == Risk_Score exactly, preserving ranking identity.
2. test_dev_db_reconstitution: Verifies that in an isolated SQLite DB, saving 2 runs
   persists AUDIT_RUN, AUDIT_ASSIGNMENT, AUDIT_FEATURE_SNAPSHOT, and allows 100% SQL reconstitution
   of the Sheet 1 Cycle_Count_Target_Bins list without needing the Excel file.
3. test_wms_stock_daily_agg: Verifies that WMS_STOCK_DAILY_AGG compresses raw barcode data
   into Bin x Mat.No aggregates with exact conservation of box counts and quantities.
"""

import sys
import json
import sqlite3
import datetime
from pathlib import Path
import os
try:
    import pytest
    pytestmark = pytest.mark.skipif(
        os.getenv("RUN_LIVE_DB_TESTS") != "1",
        reason="Live WMS snapshot & reconciliation DB regression test; set RUN_LIVE_DB_TESTS=1 to run",
    )
except ImportError:
    class _DummyPytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(func):
                return func
            return decorator
    pytest = _DummyPytest()

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CURRENT_DIR))

from box_aging_tracker import BoxAgingTracker
from db_manager import (
    init_database,
    save_audit_ledger_and_features,
    save_wms_stock_daily_agg,
    get_audit_assignments_by_run,
    get_connection
)


@pytest.fixture(scope="module")
def tracker_and_df():
    tracker = BoxAgingTracker()
    latest_file = tracker.find_latest_wms_file()
    assert latest_file is not None and latest_file.exists(), "Latest WMS file must exist for testing"
    df = pd.read_excel(latest_file)
    
    # Pre-parse like load_stock_list without writing to prod DB
    barcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), "Barcode")
    df = df.dropna(subset=[barcode_col]).copy()
    df["Barcode"] = df[barcode_col].astype(str).str.strip()
    df["Mat.No"] = df["Mat.No"].astype(str).str.strip()
    df["BIN"] = df["BIN"].astype(str).str.strip()
    df["Mat.Desc"] = df["Mat.Desc"].astype(str).str.strip()

    today_dt = pd.to_datetime(datetime.date.today())
    if "GR Date" in df.columns:
        gr_dt = pd.to_datetime(df["GR Date"].astype(str), format="%Y%m%d", errors="coerce")
        df["GR_Date"] = gr_dt.dt.date
        df["Warehouse_Dwell_Days"] = (today_dt - gr_dt).dt.days.fillna(0).astype(int)
    else:
        df["GR_Date"] = None
        df["Warehouse_Dwell_Days"] = 0

    from box_aging_tracker import parse_lot_no_date
    if "Lot No" in df.columns:
        df["Lot_Date"] = df["Lot No"].apply(parse_lot_no_date)
    else:
        df["Lot_Date"] = None
    df["Bin_Dwell_Days"] = 1

    return tracker, df, latest_file


def test_score_breakdown_identity(tracker_and_df):
    """
    Asserts that sum(Score_Breakdown.values()) == Risk_Score for all calculated suspects,
    guaranteeing that rule-level breakdown matches total score with zero deviation.
    """
    tracker, df, _ = tracker_and_df

    # 1. Daily Mode
    res_daily = tracker.detect_mis_scans(df, mode="Daily")
    suspects_daily = res_daily["suspects"]
    assert not suspects_daily.empty, "Daily suspects must not be empty"

    for idx, row in suspects_daily.iterrows():
        sb = row["Score_Breakdown"]
        score = row["Risk_Score"]
        assert isinstance(sb, dict), f"Row {idx}: Score_Breakdown must be a dict"
        sb_sum = sum(sb.values())
        assert sb_sum == score, f"Daily Row {idx} ({row['Barcode']}): sum(breakdown)={sb_sum} != Risk_Score={score}"

    assert len(res_daily["top_bins"]) == 15, "Daily top bins must be 15"
    assert len(res_daily["extended_bins"]) == 25, "Daily extended bins must be 25"

    # 2. Weekly Mode
    res_weekly = tracker.detect_mis_scans(df, mode="Weekly")
    suspects_weekly = res_weekly["suspects"]
    assert not suspects_weekly.empty, "Weekly suspects must not be empty"

    for idx, row in suspects_weekly.iterrows():
        sb = row["Score_Breakdown"]
        score = row["Risk_Score"]
        assert isinstance(sb, dict), f"Row {idx}: Score_Breakdown must be a dict"
        sb_sum = sum(sb.values())
        assert sb_sum == score, f"Weekly Row {idx} ({row['Barcode']}): sum(breakdown)={sb_sum} != Risk_Score={score}"

    assert len(res_weekly["top_bins"]) == 50, "Weekly top bins must be 50"


def test_dev_db_reconstitution(tmp_path, tracker_and_df):
    """
    Verifies that an isolated test database stores 2 runs and can reconstruct
    the exact Sheet 1 target bin list via SQL query.
    """
    tracker, df, latest_file = tracker_and_df
    dev_db = tmp_path / "inventory_dev_test.db"
    init_database(dev_db)

    res = tracker.detect_mis_scans(df, mode="Daily")
    top_bins = res["top_bins"]
    extended_bins = res["extended_bins"]
    suspects_sample = res["suspects"].head(500)  # Use top 500 for fast test

    # Run 1: 2026-10-25
    run1 = {
        "run_id": "20261025_DAILY",
        "run_date": "2026-10-25",
        "mode": "Daily",
        "scoring_version": "v1.0_rules",
        "wms_snapshot_file": latest_file.name,
        "config_hash": "v1.0_default"
    }
    save_audit_ledger_and_features(run1, top_bins, extended_bins, suspects_sample, db_path=dev_db)

    # Run 2: 2026-10-26
    run2 = {
        "run_id": "20261026_DAILY",
        "run_date": "2026-10-26",
        "mode": "Daily",
        "scoring_version": "v1.0_rules",
        "wms_snapshot_file": latest_file.name,
        "config_hash": "v1.0_default"
    }
    save_res2 = save_audit_ledger_and_features(run2, top_bins, extended_bins, suspects_sample, db_path=dev_db)
    assert save_res2["assignments_count"] == 40
    assert save_res2["features_count"] == 500

    # SQL Reconstitution Test
    assignments = get_audit_assignments_by_run("20261026_DAILY", db_path=dev_db)
    assert len(assignments) == 40, "Must recover all 40 assignments"

    all_expected = top_bins + extended_bins
    for rank_idx, (sql_row, exp_bin) in enumerate(zip(assignments, all_expected), 1):
        assert sql_row["rank"] == rank_idx
        assert sql_row["bin"] == exp_bin["BIN"]
        assert sql_row["bin_risk_score"] == exp_bin["Risk_Score"]
        assert sql_row["action_required"] == exp_bin["Action_Required"]
        assert sql_row["primary_reason"] == exp_bin["Reason"]
        assert sql_row["label_qty"] == exp_bin["Label_QTY"]

        # Parse materials JSON
        mats = json.loads(sql_row["materials_json"])
        assert isinstance(mats, list)

        # Check list type partitioning
        if rank_idx <= 15:
            assert sql_row["list_type"] == "TOP"
        else:
            assert sql_row["list_type"] == "EXTENDED"


def test_wms_stock_daily_agg(tmp_path, tracker_and_df):
    """
    Verifies WMS_STOCK_DAILY_AGG aggregation accuracy and upsert idempotency.
    """
    _, df, _ = tracker_and_df
    dev_db = tmp_path / "inventory_dev_test.db"
    init_database(dev_db)

    today_str = "2026-10-26"
    inserted = save_wms_stock_daily_agg(df, snapshot_date=today_str, db_path=dev_db)
    assert inserted > 0, "Aggregated records must be inserted"

    conn = get_connection(dev_db)
    cur = conn.cursor()
    row = cur.execute("SELECT COUNT(*), SUM(box_count), SUM(total_qty) FROM WMS_STOCK_DAILY_AGG WHERE snapshot_date = ?", (today_str,)).fetchone()
    
    db_groups = row[0]
    db_boxes = row[1]
    db_qty = row[2]

    # Compare with direct pandas calculation with BINSYNTH9953 handling
    bin_clean = df["BIN"].fillna("BINSYNTH9953").astype(str).str.strip().str.upper()
    bin_clean = bin_clean.replace({"": "BINSYNTH9953", "NAN": "BINSYNTH9953", "NONE": "BINSYNTH9953", "NULL": "BINSYNTH9953"})
    mat_clean = df["Mat.No"].fillna("UNKNOWN").astype(str).str.strip()
    mat_clean = mat_clean.replace({"": "UNKNOWN", "NAN": "UNKNOWN", "NONE": "UNKNOWN"})
    exp_groups = pd.DataFrame({"bin": bin_clean, "mat": mat_clean}).groupby(["bin", "mat"], dropna=False).ngroups
    exp_boxes = len(df)
    exp_qty = float(pd.to_numeric(df["Qty."], errors="coerce").fillna(0.0).sum())

    assert db_groups == exp_groups
    assert db_boxes == exp_boxes
    assert abs(db_qty - exp_qty) < 1.0  # Floating point tolerance

    # Re-run same day upsert (idempotency check)
    re_inserted = save_wms_stock_daily_agg(df, snapshot_date=today_str, db_path=dev_db)
    assert re_inserted == inserted
    count_after = cur.execute("SELECT COUNT(*) FROM WMS_STOCK_DAILY_AGG WHERE snapshot_date = ?", (today_str,)).fetchone()[0]
    assert count_after == db_groups, "UPSERT must not duplicate rows on same snapshot date"
    conn.close()


def test_chronic_mismatch_feed_and_features(tmp_path, tracker_and_df):
    """Verifies that 04_JOB chronic mismatch feed is read cleanly, and feature snapshot records ztcode_stock_rec metrics."""
    tracker, df, latest_file = tracker_and_df
    from db_manager import get_active_chronic_mismatch_feed
    feed = get_active_chronic_mismatch_feed()
    assert isinstance(feed, dict)
    assert len(feed) > 0, "Feed should contain active audit-recommended items from 04_JOB"

    # Verify consignment parts are excluded
    for k in feed:
        assert not k.startswith("74-") and not k.startswith("89-")

    dev_db = tmp_path / "dev_chronic.db"
    init_database(dev_db)
    
    # Run Mode A and save features
    res = tracker.detect_mis_scans(df, mode="Daily", chronic_feed_mode="feature_only")
    run_info = {
        "run_id": "20261026_TEST_CHRONIC",
        "run_date": "2026-10-26",
        "mode": "Daily",
        "scoring_version": "v1.0_rules_chronic",
        "wms_snapshot_file": latest_file.name
    }
    save_audit_ledger_and_features(
        run_info=run_info,
        top_bins=res["top_bins"],
        extended_bins=res["extended_bins"],
        suspects_df=res["suspects"],
        db_path=dev_db
    )

    conn = sqlite3.connect(dev_db)
    cur = conn.cursor()
    
    # Check that columns exist and have non-zero entries for chronic suspects
    row = cur.execute("""
        SELECT COUNT(*), MAX(ztcode_stock_rec_exposure), MAX(ztcode_stock_rec_consecutive_days)
        FROM AUDIT_FEATURE_SNAPSHOT
        WHERE run_id = '20261026_TEST_CHRONIC' AND ztcode_stock_rec_exposure > 0
    """).fetchone()
    count_chronic_boxes, max_expo, max_days = row
    assert count_chronic_boxes > 0, "Should have stored feature snapshots for chronic items"
    assert max_expo > 10000.0, "Max exposure should be > $10,000"
    assert max_days >= 3, "Max consecutive days should be >= 3"
    conn.close()


def test_mode_a_and_mode_b_selection(tracker_and_df):
    """Verifies that Mode A uses natural ranking and Mode B reserves K=3 slots with CHRONIC_MISMATCH_FEED."""
    tracker, df, latest_file = tracker_and_df
    
    # Mode A: feature_only
    res_a = tracker.detect_mis_scans(df, mode="Daily", chronic_feed_mode="feature_only")
    for b in res_a["top_bins"]:
        assert b["selection_method"] == "RULE_SCORE"
        assert b["list_type"] == "TOP"
    for b in res_a["extended_bins"]:
        assert b["selection_method"] == "RULE_SCORE"
        assert b["list_type"] == "EXTENDED"

    # Mode B: reserved_slots
    res_b = tracker.detect_mis_scans(df, mode="Daily", chronic_feed_mode="reserved_slots")
    top_b = res_b["top_bins"]
    assert len(top_b) == 15
    
    # Check reserved slots
    reserved = [b for b in top_b if b["selection_method"] == "CHRONIC_MISMATCH_FEED"]
    assert len(reserved) == 3, f"Expected 3 reserved slots, got {len(reserved)}"
    for b in reserved:
        assert b["list_type"] == "CHRONIC_FEED"
        assert b["has_chronic"] is True
        assert b.get("Tier") in [1, 2] or b["BIN"].startswith((("SMT", "CRT", "DOLLY") + box_aging_tracker.OEM_FLOOR_PREFIXES)) or b.get("Tier") == 0


def test_stratified_random_sampling(tmp_path, tracker_and_df):
    """
    Verifies Task 3:
    1. Seed reproducibility: Running twice with the same run_date produces identical samples.
    2. Seed variance: Running with a different run_date produces a different sample.
    3. Stratification & constraints: Daily random bins respect floor-walkable constraints (Tier <= 2 or floor).
    4. Inclusion probabilities: Computed and bounded (0 < pi <= 1) based on stratum population.
    5. DB persistence & reconstitution: AUDIT_ASSIGNMENT stores list_type='RANDOM', selection_method='RANDOM_SAMPLE',
       and exact inclusion_probability.
    6. Excel Sheet 1 export: Verifies that random bins are appended with Action Required="Random Verification"
       and Reason="Random sample (model validation)".
    """
    tracker, df, latest_file = tracker_and_df
    
    # 1. Reproducibility test
    res_date1_a = tracker.detect_mis_scans(df, mode="Daily", run_date="2026-10-26")
    res_date1_b = tracker.detect_mis_scans(df, mode="Daily", run_date="2026-10-26")
    res_date2   = tracker.detect_mis_scans(df, mode="Daily", run_date="2026-10-27")

    bins1_a = [b["BIN"] for b in res_date1_a["random_bins"]]
    bins1_b = [b["BIN"] for b in res_date1_b["random_bins"]]
    bins2   = [b["BIN"] for b in res_date2["random_bins"]]

    assert len(bins1_a) == 3, f"Expected 3 daily random bins, got {len(bins1_a)}"
    assert bins1_a == bins1_b, f"Reproducibility failed: {bins1_a} != {bins1_b}"
    assert bins1_a != bins2, f"Date variance failed: {bins1_a} should differ from {bins2}"

    # 2. Daily physical constraints check (floor-walkable only)
    for b in res_date1_a["random_bins"]:
        tier = b.get("Tier")
        b_name = b["BIN"]
        assert (tier in [0, 1, 2] or b_name.startswith((("SMT", "CRT", "DOLLY") + box_aging_tracker.OEM_FLOOR_PREFIXES))), (
            f"Daily random bin {b_name} violated floor-walkable constraint (Tier={tier})"
        )
        assert b["Action_Required"] == "Random Verification"
        assert b["Reason"] == "Random sample (model validation)"
        assert 0 < b["inclusion_probability"] <= 1.0

    # 3. Weekly mode test (5 slots, includes high racks)
    res_weekly = tracker.detect_mis_scans(df, mode="Weekly", run_date="2026-10-26")
    assert len(res_weekly["random_bins"]) == 5
    has_tier3_plus = any(b.get("Tier", 0) >= 3 for b in res_weekly["random_bins"])
    assert has_tier3_plus, "Weekly random sampling should include high rack (Tier 3+) strata"

    # 4. DB Persistence in isolated dev_db
    dev_db = tmp_path / "dev_random_test.db"
    init_database(dev_db)
    
    run_info = {
        "run_id": "20261026_RANDOM_DAILY",
        "run_date": "2026-10-26",
        "mode": "Daily",
        "scoring_version": "v1.0_rules",
        "wms_snapshot_file": latest_file.name
    }
    save_audit_ledger_and_features(
        run_info=run_info,
        top_bins=res_date1_a["top_bins"],
        extended_bins=res_date1_a["extended_bins"],
        random_bins=res_date1_a["random_bins"],
        suspects_df=res_date1_a["suspects"].head(100),
        db_path=dev_db
    )

    assignments = get_audit_assignments_by_run("20261026_RANDOM_DAILY", db_path=dev_db)
    assert len(assignments) == 43, f"Expected 40 prioritized + 3 random = 43, got {len(assignments)}"
    
    random_assignments = [a for a in assignments if a["list_type"] == "RANDOM"]
    assert len(random_assignments) == 3
    for a in random_assignments:
        assert a["selection_method"] == "RANDOM_SAMPLE"
        assert a["action_required"] == "Random Verification"
        assert a["primary_reason"] == "Random sample (model validation)"
        assert a["inclusion_probability"] is not None and 0 < a["inclusion_probability"] <= 1.0

    # 5. Excel Sheet 1 verification
    test_excel_path = tmp_path / "audit_random_test.xlsx"
    tracker.export_audit_excel(res_date1_a, output_path=test_excel_path, mode="Daily")
    assert test_excel_path.exists()
    
    sheet1_df = pd.read_excel(test_excel_path, sheet_name="Cycle_Count_Target_Bins")
    assert len(sheet1_df) == 43, f"Sheet 1 should contain 43 rows (40 target + 3 random), got {len(sheet1_df)}"
    # Check last 3 rows correspond to the 3 random validation bins in 5-column format
    tail3_bins = sheet1_df.tail(3)["Bin #"].tolist()
    expected_random_bins = [b["BIN"] for b in res_date1_a["random_bins"]]
    assert tail3_bins == expected_random_bins, f"Expected {expected_random_bins}, got {tail3_bins}"


def test_shadow_ev_calculation_and_excel(tmp_path, tracker_and_df):
    """
    Verifies Task 4:
    1. scoring_version and config_hash are present in analysis result.
    2. p_error_proxy is monotonically bounded in (0, 1).
    3. net_price is correctly looked up from 04_JOB accuracy_material_snapshot.
    4. expected_loss = p_error_proxy * (Qty * net_price).
    5. priority_ev = expected_loss / est_minutes.
    6. AUDIT_FEATURE_SNAPSHOT stores all 5 shadow EV columns.
    7. Shadow_EV_Ranking sheet is generated with Top 15 bins, Rule Rank, and Rank Shift.
    """
    tracker, df, latest_file = tracker_and_df
    res = tracker.detect_mis_scans(df, mode="Daily", run_date="2026-10-26")

    # 1. Scoring version & hash
    assert res.get("scoring_version") == "v1.0_rules"
    assert res.get("config_hash") is not None and len(res["config_hash"]) > 0

    # 2. Suspect-level Shadow EV checks
    suspects = res["suspects"]
    assert not suspects.empty
    has_any_price = False

    for _, row in suspects.head(200).iterrows():
        p = row.get("P_Error_Proxy")
        assert p is not None and 0.0 < p < 1.0, f"p_error_proxy must be in (0, 1), got {p}"
        
        np = row.get("Net_Price")
        el = row.get("Expected_Loss")
        em = row.get("Est_Minutes")
        ev = row.get("Priority_EV")
        qty = float(row.get("Qty", 0.0) or 0.0)

        assert em in [1.0, 1.5, 2.0, 5.0], f"Invalid est_minutes: {em}"

        if np is not None:
            has_any_price = True
            expected_el = round(p * qty * np, 2)
            assert abs(el - expected_el) <= 0.02, f"Expected loss mismatch: {el} != {expected_el}"
            expected_ev = round(el / em, 2)
            assert abs(ev - expected_ev) <= 0.02, f"Priority EV mismatch: {ev} != {expected_ev}"
        else:
            assert el is None
            assert ev is None

    assert has_any_price, "At least some suspects must have net_price populated from 04_JOB snapshot"

    # 3. Bin-level Shadow EV Top 15 Ranking
    shadow_ranking = res.get("shadow_ev_ranking", [])
    assert len(shadow_ranking) == 15, f"Expected 15 shadow EV bins, got {len(shadow_ranking)}"
    
    for i, item in enumerate(shadow_ranking, 1):
        assert item["Shadow_Rank"] == i
        assert item["Rule_Rank"] >= 1
        assert "Rank_Shift" in item
        assert item["Priority_EV"] is not None and item["Priority_EV"] >= 0.0
        assert item["BIN"] is not None

    # Check sorting: Priority EV must be descending
    ev_values = [item["Priority_EV"] for item in shadow_ranking]
    assert ev_values == sorted(ev_values, reverse=True), f"Shadow EV ranking must be descending: {ev_values}"

    # 4. DB Persistence of Shadow EV columns in AUDIT_FEATURE_SNAPSHOT
    dev_db = tmp_path / "dev_shadow_ev_test.db"
    init_database(dev_db)
    run_info = {
        "run_id": "20261026_SHADOW_TEST",
        "run_date": "2026-10-26",
        "mode": "Daily",
        "scoring_version": res["scoring_version"],
        "wms_snapshot_file": latest_file.name,
        "config_hash": res["config_hash"]
    }
    save_audit_ledger_and_features(
        run_info=run_info,
        top_bins=res["top_bins"],
        extended_bins=res["extended_bins"],
        random_bins=res["random_bins"],
        suspects_df=res["suspects"].head(200),
        db_path=dev_db
    )

    conn = sqlite3.connect(dev_db)
    cur = conn.cursor()
    row = cur.execute("""
        SELECT COUNT(*), AVG(p_error_proxy), MAX(expected_loss), MAX(priority_ev)
        FROM AUDIT_FEATURE_SNAPSHOT
        WHERE run_id = '20261026_SHADOW_TEST' AND net_price IS NOT NULL
    """).fetchone()
    count_priced, avg_p, max_loss, max_ev = row
    assert count_priced > 0, "DB should contain feature records with non-null net_price"
    assert 0.0 < avg_p < 1.0, f"Average p_error_proxy must be in (0, 1), got {avg_p}"
    assert max_loss > 0.0, f"Max expected loss must be > 0, got {max_loss}"
    assert max_ev > 0.0, f"Max priority ev must be > 0, got {max_ev}"
    conn.close()

    # 5. Excel Sheet Generation verification (Shadow_EV_Ranking sheet)
    excel_path = tmp_path / "test_shadow_excel.xlsx"
    tracker.export_audit_excel(res, output_path=excel_path, mode="Daily")
    assert excel_path.exists()

    xl = pd.ExcelFile(excel_path)
    assert "Shadow_EV_Ranking" in xl.sheet_names, f"Shadow_EV_Ranking sheet missing: {xl.sheet_names}"
    df_shadow = pd.read_excel(excel_path, sheet_name="Shadow_EV_Ranking")
    assert len(df_shadow) == 15
    for expected_col in ["Shadow Rank", "Rule Rank", "Rank Shift", "Bin #", "P(Error) Proxy", "Priority EV ($/min)"]:
        assert expected_col in df_shadow.columns, f"Missing column {expected_col} in Shadow_EV_Ranking"


def test_config_mutation_rank_shift(tracker_and_df):
    """
    Verifies that changing YAML scoring parameters shifts the resulting bin ranking:
    1. Base run with default config.
    2. Mutated run with high_rack_aging or fifo_bypass given an overwhelming weight.
    3. Confirms that mutated config alters the top_bins order, demonstrating dynamic config coupling.
    """
    import copy
    tracker, df, _ = tracker_and_df
    base_res = tracker.detect_mis_scans(df, mode="Daily")
    base_top_bins = [b["BIN"] for b in base_res["top_bins"]]

    # Mutate 1: Heavily boost small part dwell stagnant
    mutated_cfg = copy.deepcopy(tracker.scoring_config)
    mutated_cfg["weights"]["small_part_dwell_stagnant"] = 500
    res_mutated1 = tracker.detect_mis_scans(df, mode="Daily", scoring_config=mutated_cfg)
    mutated1_top_bins = [b["BIN"] for b in res_mutated1["top_bins"]]

    assert base_top_bins != mutated1_top_bins, (
        f"Mutating small_part_dwell_stagnant must alter ranking: {base_top_bins[:5]} == {mutated1_top_bins[:5]}"
    )

    # Mutate 2: Heavily boost SMT stagnant
    mutated_cfg2 = copy.deepcopy(tracker.scoring_config)
    mutated_cfg2["weights"]["smt_stagnant_daily"] = 500
    res_mutated2 = tracker.detect_mis_scans(df, mode="Daily", scoring_config=mutated_cfg2)
    mutated2_top_bins = [b["BIN"] for b in res_mutated2["top_bins"]]

    assert base_top_bins != mutated2_top_bins, (
        f"Mutating smt_stagnant_daily must alter ranking: {base_top_bins[:5]} == {mutated2_top_bins[:5]}"
    )
    assert mutated1_top_bins != mutated2_top_bins, (
        f"Different weight mutations should produce different rankings"
    )


if __name__ == "__main__":
    import tempfile
    import shutil
    print("\n[Audit Ledger & Feature Snapshot Regression Test Suite]")
    print("=" * 65)

    print("\n1. Initializing test fixtures (loading latest WMS file)...")
    tracker = BoxAgingTracker()
    latest_file = tracker.find_latest_wms_file()
    df = pd.read_excel(latest_file)
    barcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), "Barcode")
    df = df.dropna(subset=[barcode_col]).copy()
    df["Barcode"] = df[barcode_col].astype(str).str.strip()
    df["Mat.No"] = df["Mat.No"].astype(str).str.strip()
    df["BIN"] = df["BIN"].astype(str).str.strip()
    df["Mat.Desc"] = df["Mat.Desc"].astype(str).str.strip()
    today_dt = pd.to_datetime(datetime.date.today())
    if "GR Date" in df.columns:
        gr_dt = pd.to_datetime(df["GR Date"].astype(str), format="%Y%m%d", errors="coerce")
        df["GR_Date"] = gr_dt.dt.date
        df["Warehouse_Dwell_Days"] = (today_dt - gr_dt).dt.days.fillna(0).astype(int)
    else:
        df["GR_Date"] = None
        df["Warehouse_Dwell_Days"] = 0
    from box_aging_tracker import parse_lot_no_date
    if "Lot No" in df.columns:
        df["Lot_Date"] = df["Lot No"].apply(parse_lot_no_date)
    else:
        df["Lot_Date"] = None
    df["Bin_Dwell_Days"] = 1
    fixtures = (tracker, df, latest_file)
    print(f"   [OK] Fixtures loaded: {len(df):,} rows from {latest_file.name}")

    print("\n2. Running BINSYNTH9952: test_score_breakdown_identity...")
    test_score_breakdown_identity(fixtures)
    print("   [PASS] 100% of suspects in Daily & Weekly have sum(score_breakdown) == Risk_Score!")

    temp_dir = Path(tempfile.mkdtemp())
    try:
        print("\n3. Running Test 2: test_dev_db_reconstitution...")
        test_dev_db_reconstitution(temp_dir, fixtures)
        print("   [PASS] SQL query of AUDIT_ASSIGNMENT perfectly reconstitutes Sheet 1 target bins!")

        print("\n4. Running Test 3: test_wms_stock_daily_agg...")
        test_wms_stock_daily_agg(temp_dir, fixtures)
        print("   [PASS] WMS_STOCK_DAILY_AGG compression and box/qty conservation verified!")

        print("\n5. Running Test 4: test_chronic_mismatch_feed_and_features...")
        test_chronic_mismatch_feed_and_features(temp_dir, fixtures)
        print("   [PASS] Chronic feed retrieval and ZTCODE_STOCK_REC feature snapshot persistence verified!")

        print("\n6. Running Test 5: test_mode_a_and_mode_b_selection...")
        test_mode_a_and_mode_b_selection(fixtures)
        print("   [PASS] Mode A (feature_only) & Mode B (reserved_slots) selection verified!")

        print("\n7. Running Test 6: test_stratified_random_sampling...")
        test_stratified_random_sampling(temp_dir, fixtures)
        print("   [PASS] Stratified random sampling reproducibility, variance, constraints, DB, and Excel verified!")

        print("\n8. Running Test 7: test_shadow_ev_calculation_and_excel...")
        test_shadow_ev_calculation_and_excel(temp_dir, fixtures)
        print("   [PASS] Shadow EV (p_error, net_price, expected_loss, priority_ev, Excel sheet) verified!")

        print("\n9. Running Test 8: test_config_mutation_rank_shift...")
        test_config_mutation_rank_shift(fixtures)
        print("   [PASS] Config mutation dynamically alters ranking (proven YAML decoupling)!")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n" + "=" * 65)
    print("🎉 ALL 8 REGRESSION TESTS PASSED SUCCESSFULLY! (Zero Deviations)")


