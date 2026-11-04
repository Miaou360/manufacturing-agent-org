"""
Unit Tests for D34: Physical Audit Hit Rate Tracking Engine
===========================================================
01_M365MCP/01_DEPT_Inventory/tests/test_d34_audit_hit_rate.py

Tests:
1. Pure factual integrity (Rule 9): Uncounted bins (NULL result) excluded from denominator.
2. Monotonic rank precision: Correct bucketing into Rank 1~5, 6~15, 16~40.
3. Synthetic data exclusion: data_origin='SYNTHETIC' strictly ignored.
4. Missing RANDOM baseline: Returns None for baseline & Lift gracefully without ZeroDivisionError.
5. Rolling cumulative calculation: Proper sum(discrepancies)/sum(samples) across 7d/28d windows.
6. DB persistence: Written and retrieved from AUDIT_HIT_RATE_DAILY without data loss.
7. Backfill against production SQLite DB (read-only validation for 20261026_DAILY).
"""

import sys
import os
import sqlite3
import datetime
import pytest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
JOB_00_DIR = BASE_DIR / "00_JOB_StockAccuracy"
sys.path.insert(0, str(JOB_00_DIR))

from hit_rate_calculator import (
    init_database,
    save_hit_rate_record,
    get_hit_rate_records,
    compute_hit_rate_for_run,
    backfill_all_hit_rates,
    get_hit_rate_history,
    generate_hit_rate_summary_text
)


@pytest.fixture
def test_db(tmp_path):
    """Creates a fresh test SQLite database with full schema."""
    db_file = tmp_path / "test_hit_rate.db"
    init_database(db_file)
    return db_file


def test_uncounted_bins_excluded_from_denominator(test_db):
    """Verifies that bins with no count result are excluded from sample size (Rule 9)."""
    conn = sqlite3.connect(str(test_db))
    cur = conn.cursor()

    # Create run
    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_TEST_01', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)

    # Create 5 assignments: 2 TOP (rank 1, 2), 3 EXTENDED (rank 16, 17, 18)
    for rank, (aid, b, lt) in enumerate([
        ('A1', 'BIN_01', 'TOP'),
        ('A2', 'BIN_02', 'TOP'),
        ('A3', 'BIN_16', 'EXTENDED'),
        ('A4', 'BIN_17', 'EXTENDED'),
        ('A5', 'BIN_18', 'EXTENDED'),
    ], 1):
        cur.execute("""
            INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at)
            VALUES (?, 'RUN_TEST_01', ?, ?, ?, 'RULE', 80.0, 'Audit', 10, '2026-10-26 06:00:00')
        """, (aid, b, rank, lt))

    # Record results for only 3 assignments (A1: match, A2: uncounted/NULL, A3: discrepancy, A4: match, A5: uncounted/NULL)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES ('A1', 'RUN_TEST_01', 'BIN_01', 10, 10, 0, 0, 'PHYSICAL', '2026-10-26 07:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES ('A3', 'RUN_TEST_01', 'BIN_16', 10, 5, -5, 1, 'PHYSICAL', '2026-10-26 07:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES ('A4', 'RUN_TEST_01', 'BIN_17', 10, 10, 0, 0, 'PHYSICAL', '2026-10-26 07:00:00')
    """)
    conn.commit()
    conn.close()

    metrics = compute_hit_rate_for_run('RUN_TEST_01', db_path=test_db, save_to_db=True)

    # 5 assigned, but only 3 counted!
    assert metrics["sample_size_top"] == 1  # A1 only (A2 uncounted)
    assert metrics["discrepancy_count_top"] == 0
    assert metrics["hit_rate_top"] == 0.0

    assert metrics["sample_size_extended"] == 2  # A3, A4 (A5 uncounted)
    assert metrics["discrepancy_count_extended"] == 1  # A3
    assert metrics["hit_rate_extended"] == 0.5  # 1 / 2

    assert metrics["sample_size_model"] == 3
    assert metrics["discrepancy_count_model"] == 1
    assert round(metrics["hit_rate_model"], 4) == round(1 / 3, 4)

    assert "2 uncounted bins" in metrics["notes"]


def test_monotonic_rank_precision_bucketing(test_db):
    """Verifies precision across rank buckets (1~5, 6~15, 16~40)."""
    conn = sqlite3.connect(str(test_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_RANK_TEST', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)

    # Rank 1~5: 5 bins, 4 discrepancies (80%)
    for r in range(1, 6):
        aid = f"R_{r:02d}"
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_RANK_TEST', ?, ?, 'TOP', 'RULE', 90, 'Audit', 10, '2026-10-26')", (aid, aid, r))
        is_disc = 1 if r <= 4 else 0
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_RANK_TEST', ?, 10, ?, ?, 'PHYSICAL', '2026-10-26')", (aid, aid, 5 if is_disc else 10, is_disc))

    # Rank 6~15: 10 bins, 5 discrepancies (50%)
    for r in range(6, 16):
        aid = f"R_{r:02d}"
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_RANK_TEST', ?, ?, 'TOP', 'RULE', 70, 'Audit', 10, '2026-10-26')", (aid, aid, r))
        is_disc = 1 if r <= 10 else 0
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_RANK_TEST', ?, 10, ?, ?, 'PHYSICAL', '2026-10-26')", (aid, aid, 5 if is_disc else 10, is_disc))

    # Rank 16~40: 25 bins, 5 discrepancies (20%)
    for r in range(16, 41):
        aid = f"R_{r:02d}"
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_RANK_TEST', ?, ?, 'EXTENDED', 'RULE', 40, 'Audit', 10, '2026-10-26')", (aid, aid, r))
        is_disc = 1 if r <= 20 else 0
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_RANK_TEST', ?, 10, ?, ?, 'PHYSICAL', '2026-10-26')", (aid, aid, 5 if is_disc else 10, is_disc))

    conn.commit()
    conn.close()

    metrics = compute_hit_rate_for_run('RUN_RANK_TEST', db_path=test_db, save_to_db=True)

    assert metrics["rank_1_5_sample"] == 5
    assert metrics["rank_1_5_discrepancies"] == 4
    assert metrics["rank_1_5_hit_rate"] == 0.8  # 80%

    assert metrics["rank_6_15_sample"] == 10
    assert metrics["rank_6_15_discrepancies"] == 5
    assert metrics["rank_6_15_hit_rate"] == 0.5  # 50%

    assert metrics["rank_16_40_sample"] == 25
    assert metrics["rank_16_40_discrepancies"] == 5
    assert metrics["rank_16_40_hit_rate"] == 0.2  # 20%

    # Monotonic precision confirmed: 80% > 50% > 20%
    assert metrics["rank_1_5_hit_rate"] > metrics["rank_6_15_hit_rate"] > metrics["rank_16_40_hit_rate"]


def test_synthetic_data_exclusion(test_db):
    """Verifies that synthetic/seed data is strictly excluded from hit rates."""
    conn = sqlite3.connect(str(test_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_SYNTH_TEST', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)

    # 1 real physical count (discrepancy=1)
    cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES ('R1', 'RUN_SYNTH_TEST', 'B1', 1, 'TOP', 'RULE', 90, 'Audit', 10, '2026-10-26')")
    cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES ('R1', 'RUN_SYNTH_TEST', 'B1', 10, 0, 1, 'PHYSICAL', '2026-10-26')")

    # 10 synthetic seeded counts (all match, discrepancy=0)
    for i in range(2, 12):
        aid = f"S{i}"
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_SYNTH_TEST', ?, ?, 'TOP', 'RULE', 90, 'Audit', 10, '2026-10-26')", (aid, aid, i))
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_SYNTH_TEST', ?, 10, 10, 0, 'SYNTHETIC', '2026-10-26')", (aid, aid))

    conn.commit()
    conn.close()

    metrics = compute_hit_rate_for_run('RUN_SYNTH_TEST', db_path=test_db, save_to_db=False)

    # Only R1 should be counted! (sample = 1, discrepancy = 1, rate = 100%)
    assert metrics["sample_size_top"] == 1
    assert metrics["discrepancy_count_top"] == 1
    assert metrics["hit_rate_top"] == 1.0


def test_missing_random_baseline_handled_cleanly(test_db):
    """Verifies that missing random validation samples return None for baseline and Lift without error."""
    conn = sqlite3.connect(str(test_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_NO_RAND', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)
    cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES ('T1', 'RUN_NO_RAND', 'B1', 1, 'TOP', 'RULE', 90, 'Audit', 10, '2026-10-26')")
    cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES ('T1', 'RUN_NO_RAND', 'B1', 10, 5, 1, 'PHYSICAL', '2026-10-26')")
    conn.commit()
    conn.close()

    metrics = compute_hit_rate_for_run('RUN_NO_RAND', db_path=test_db, save_to_db=True)

    assert metrics["sample_size_random_28d"] == 0
    assert metrics["discrepancy_count_random_28d"] == 0
    assert metrics["hit_rate_random_28d"] is None
    assert metrics["lift"] is None
    assert "Random baseline sample n=0" in metrics["notes"]

    report_text = generate_hit_rate_summary_text('RUN_NO_RAND', db_path=test_db)
    assert "기준선 부족" in report_text


def test_rolling_cumulative_with_random_lift(test_db):
    """Verifies rolling cumulative calculations and Lift when random samples are present."""
    conn = sqlite3.connect(str(test_db))
    cur = conn.cursor()

    # Day 1: 2026-10-25
    cur.execute("INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at) VALUES ('RUN_D1', '2026-10-25', 'Daily', 'v1', '2026-10-25')")
    # 10 model bins: 4 discrepancies (40%)
    for i in range(1, 11):
        aid = f"D1_M_{i}"
        is_disc = 1 if i <= 4 else 0
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_D1', ?, ?, 'TOP', 'RULE', 80, 'Audit', 10, '2026-10-25')", (aid, aid, i))
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_D1', ?, 10, ?, ?, 'PHYSICAL', '2026-10-25')", (aid, aid, 5 if is_disc else 10, is_disc))

    # 3 random validation bins: 1 discrepancy (33.3%)
    for i in range(1, 4):
        aid = f"D1_R_{i}"
        is_disc = 1 if i == 1 else 0
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_D1', ?, ?, 'RANDOM_VALIDATION', 'RAND', 10, 'Audit', 10, '2026-10-25')", (aid, aid, 40 + i))
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_D1', ?, 10, ?, ?, 'PHYSICAL', '2026-10-25')", (aid, aid, 5 if is_disc else 10, is_disc))

    # Day 2: 2026-10-26
    cur.execute("INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at) VALUES ('RUN_D2', '2026-10-26', 'Daily', 'v1', '2026-10-26')")
    # 10 model bins: 6 discrepancies (60%)
    for i in range(1, 11):
        aid = f"D2_M_{i}"
        is_disc = 1 if i <= 6 else 0
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_D2', ?, ?, 'TOP', 'RULE', 80, 'Audit', 10, '2026-10-26')", (aid, aid, i))
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_D2', ?, 10, ?, ?, 'PHYSICAL', '2026-10-26')", (aid, aid, 5 if is_disc else 10, is_disc))

    # 3 random validation bins: 0 discrepancies (0%)
    for i in range(1, 4):
        aid = f"D2_R_{i}"
        cur.execute("INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at) VALUES (?, 'RUN_D2', ?, ?, 'RANDOM_VALIDATION', 'RAND', 10, 'Audit', 10, '2026-10-26')", (aid, aid, 40 + i))
        cur.execute("INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, is_discrepancy, data_origin, created_at) VALUES (?, 'RUN_D2', ?, 10, 10, 0, 'PHYSICAL', '2026-10-26')", (aid, aid))

    conn.commit()
    conn.close()

    metrics_d2 = compute_hit_rate_for_run('RUN_D2', db_path=test_db, save_to_db=True)

    # Day 2 model hit rate: 6 / 10 = 60%
    assert metrics_d2["hit_rate_model"] == 0.60

    # 7-day cumulative model: (4 + 6) / (10 + 10) = 10 / 20 = 50%
    assert metrics_d2["cumulative_7d_hit_rate"] == 0.50

    # 28-day random baseline: (1 + 0) / (3 + 3) = 1 / 6 = 16.67%
    assert metrics_d2["sample_size_random_28d"] == 6
    assert metrics_d2["discrepancy_count_random_28d"] == 1
    assert round(metrics_d2["hit_rate_random_28d"], 4) == round(1 / 6, 4)

    # Lift: 0.60 / (1/6) = 3.60x
    assert metrics_d2["lift"] == pytest.approx(3.60, 0.05)


@pytest.mark.skipif(
    __import__("os").getenv("RUN_LIVE_DB_TESTS") != "1",
    reason="Live production DB integration test; set RUN_LIVE_DB_TESTS=1 to run",
)
def test_production_db_backfill_and_report():
    """Validates real production data in inventory_history.db for 20261026_DAILY."""
    prod_db = JOB_00_DIR / "db" / "inventory_history.db"
    if not prod_db.exists():
        pytest.skip("Production DB not present on local machine")

    results = backfill_all_hit_rates(db_path=prod_db)
    assert len(results) >= 1

    r_1026 = next((r for r in results if r["run_id"] == "20261026_DAILY"), None)
    assert r_1026 is not None

    # Expected exact metrics verified after SCR 1 (counted_qty IS NULL) exclusion:
    # 40 assignments, 3 uncounted bins excluded (EXT_TRL TRL 1, EXT_TRL TRL 2, SCR 1) -> 37 counted model bins
    assert r_1026["sample_size_model"] == 37
    assert r_1026["discrepancy_count_model"] == 13
    assert round(r_1026["hit_rate_model"], 3) == 0.351

    assert r_1026["sample_size_top"] == 13
    assert r_1026["discrepancy_count_top"] == 6
    assert round(r_1026["hit_rate_top"], 3) == 0.462

    assert r_1026["sample_size_extended"] == 24
    assert r_1026["discrepancy_count_extended"] == 7
    assert round(r_1026["hit_rate_extended"], 3) == 0.292

    # Monotonic precision: 66.7% > 40.0% > 29.2%
    assert round(r_1026["rank_1_5_hit_rate"], 3) == 0.667
    assert round(r_1026["rank_6_15_hit_rate"], 3) == 0.400
    assert round(r_1026["rank_16_40_hit_rate"], 3) == 0.292

    # Random baseline is missing on 10/26 -> None
    assert r_1026["sample_size_random_28d"] == 0
    assert r_1026["hit_rate_random_28d"] is None
    assert r_1026["lift"] is None

    # Summary text generation
    report = generate_hit_rate_summary_text("20261026_DAILY", db_path=prod_db)
    assert "실사 리스트 타율 분석 리포트" in report
    assert "35.1%" in report
    assert "46.2%" in report
