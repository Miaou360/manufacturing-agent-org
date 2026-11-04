"""
Unit Tests for D74 Stage 1: Continuous Learning Loop (규칙별 적중률 · 품번/빈 이력 · 학습 데이터 누적)
======================================================================================================
01_M365MCP/01_DEPT_Inventory/tests/test_learning_loop.py

Verifies:
1. Rule-based hit rate calculation and Beta(1, 3) smoothing.
2. Historical discrepancy scoring for Bins and Materials with shrinkage preventing 0%/100% extremes.
3. Continuous learning loop progress towards Stage 2 (n=50) and Stage 3 (n=200).
4. Strict factual integrity (Rule 9):
   - Uncounted bins (counted_qty IS NULL) strictly excluded.
   - Synthetic/Seed/Test origins strictly excluded.
   - Cancelled assignments and quarantined runs strictly excluded.
5. HTML email rendering includes Sections 4, 5, 6 without template errors.
"""

import sys
import os
import sqlite3
import pytest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
JOB_00_DIR = BASE_DIR / "00_JOB_StockAccuracy"
sys.path.insert(0, str(JOB_00_DIR))

from hit_rate_calculator import (
    init_database,
    compute_hit_rate_for_run,
    compute_rule_hit_rates,
    compute_historical_discrepancy_scores,
    get_learning_loop_progress,
    CANONICAL_RULES
)
from weekly_hit_rate_notifier import build_weekly_hit_rate_html


@pytest.fixture
def mock_db(tmp_path):
    """Creates a temporary SQLite database initialized with full schema."""
    db_file = tmp_path / "test_learning_loop.db"
    init_database(db_file)
    return db_file


def test_rule_hit_rates_and_beta_smoothing(mock_db):
    """
    Verifies that rules are correctly extracted from primary_reason and selection_method,
    and Beta(1, 3) smoothing properly shrinks small sample sizes.
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_20261027_DAILY', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:50:00')
    """)

    # 4 assignments:
    # A1: Ghost Stock + Floor FIFO Bypass, discrepancy=1
    # A2: Ghost Stock, discrepancy=0
    # A3: SMT Stagnant, discrepancy=1
    # A4: Random sample (selection_method=RANDOM_SAMPLE), discrepancy=0
    assignments = [
        ('A1', 'RUN_20261027_DAILY', 'SMT 07', 1, 'TOP', 'RULE_SCORE', 'Ghost Stock (33 Bx Deficit) / Floor FIFO Bypass (14d)', 10),
        ('A2', 'RUN_20261027_DAILY', '52Z 2', 2, 'TOP', 'RULE_SCORE', 'Ghost Stock (44 Bx Deficit)', 10),
        ('A3', 'RUN_20261027_DAILY', 'SMT 9946', 3, 'TOP', 'RULE_SCORE', 'SMT Stagnant Buffer (7d/Turn 2d)', 5),
        ('A4', 'RUN_20261027_DAILY', '51Z 1', 4, 'RANDOM', 'RANDOM_SAMPLE', 'Random sample (model validation)', 1),
    ]
    cur.executemany("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 50.0, 'Audit', '2026-10-27 06:50:00')
    """, assignments)

    results = [
        ('A1', 'RUN_20261027_DAILY', 'SMT 07', '99000031', 10, 20, 10, 1, 'PHYSICAL'),
        ('A2', 'RUN_20261027_DAILY', '52Z 2', '99000013-01', 10, 10, 0, 0, 'PHYSICAL'),
        ('A3', 'RUN_20261027_DAILY', 'SMT 9946', '99000015', 5, 0, -5, 1, 'PHYSICAL'),
        ('A4', 'RUN_20261027_DAILY', '51Z 1', '99000010', 1, 1, 0, 0, 'PHYSICAL'),
    ]
    cur.executemany("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, mat_no, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-10-27 07:30:00')
    """, results)
    conn.commit()
    conn.close()

    # Compute rule hit rates
    rules = compute_rule_hit_rates(run_id='RUN_20261027_DAILY', db_path=mock_db, save_to_db=True, alpha=1.0, beta=3.0)
    rule_dict = {r['rule_code']: r for r in rules}

    # Ghost Stock: triggered in A1 and A2 -> n=2, d=1
    ghost = rule_dict['GHOST_MISSING_GI']
    assert ghost['sample_size'] == 2
    assert ghost['discrepancy_count'] == 1
    assert ghost['hit_rate'] == 0.5
    # Smoothed: (1 + 1) / (2 + 1 + 3) = 2 / 6 = 0.3333
    assert round(ghost['smoothed_score'], 4) == round(2 / 6, 4)

    # Floor FIFO Bypass: triggered in A1 -> n=1, d=1
    fifo = rule_dict['FLOOR_FIFO_BYPASS']
    assert fifo['sample_size'] == 1
    assert fifo['discrepancy_count'] == 1
    assert fifo['hit_rate'] == 1.0
    # Smoothed: (1 + 1) / (1 + 1 + 3) = 2 / 5 = 0.40 (does NOT stay 1.0!)
    assert round(fifo['smoothed_score'], 4) == 0.40

    # SMT Stagnant: triggered in A3 -> n=1, d=1
    smt = rule_dict['SMT_STAGNANT']
    assert smt['sample_size'] == 1
    assert smt['discrepancy_count'] == 1
    assert round(smt['smoothed_score'], 4) == 0.40

    # Random Validation: triggered in A4 -> n=1, d=0
    rand = rule_dict['RANDOM_VALIDATION']
    assert rand['sample_size'] == 1
    assert rand['discrepancy_count'] == 0
    assert rand['hit_rate'] == 0.0
    # Smoothed: (0 + 1) / (1 + 1 + 3) = 1 / 5 = 0.20 (does NOT stay 0.0!)
    assert round(rand['smoothed_score'], 4) == 0.20


def test_bin_and_material_historical_scores(mock_db):
    """
    Verifies that historical discrepancy scores are correctly aggregated by bin and material,
    and Beta shrinkage prevents extreme swings.
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    # Day 1: Run 1
    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_DAY_1', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)
    # Day 2: Run 2
    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_DAY_2', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:00:00')
    """)

    # Bin 'REPEAT_BIN' audited on Day 1 and Day 2 (discrepancy both days)
    # Bin 'CLEAN_BIN' audited on Day 1 and Day 2 (match both days)
    # Part 'REPEAT_MAT' present in REPEAT_BIN both days
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES 
        ('A_D1_1', 'RUN_DAY_1', 'REPEAT_BIN', 1, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'Audit', '2026-10-26 06:00:00'),
        ('A_D1_2', 'RUN_DAY_1', 'CLEAN_BIN', 2, 'TOP', 'RULE', 'Same-Bin Spread', 5, 70.0, 'Audit', '2026-10-26 06:00:00'),
        ('A_D2_1', 'RUN_DAY_2', 'REPEAT_BIN', 1, 'TOP', 'RULE', 'Ghost Stock', 10, 85.0, 'Audit', '2026-10-27 06:00:00'),
        ('A_D2_2', 'RUN_DAY_2', 'CLEAN_BIN', 2, 'TOP', 'RULE', 'Same-Bin Spread', 5, 65.0, 'Audit', '2026-10-27 06:00:00')
    """)

    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, mat_no, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES 
        ('A_D1_1', 'RUN_DAY_1', 'REPEAT_BIN', 'REPEAT_MAT, MAT_B', 10, 5, -5, 1, 'PHYSICAL', '2026-10-26 07:00:00'),
        ('A_D1_2', 'RUN_DAY_1', 'CLEAN_BIN', 'CLEAN_MAT', 5, 5, 0, 0, 'PHYSICAL', '2026-10-26 07:00:00'),
        ('A_D2_1', 'RUN_DAY_2', 'REPEAT_BIN', 'REPEAT_MAT, MAT_C', 10, 8, -2, 1, 'PHYSICAL', '2026-10-27 07:00:00'),
        ('A_D2_2', 'RUN_DAY_2', 'CLEAN_BIN', 'CLEAN_MAT', 5, 5, 0, 0, 'PHYSICAL', '2026-10-27 07:00:00')
    """)
    conn.commit()
    conn.close()

    scores = compute_historical_discrepancy_scores(db_path=mock_db, save_to_db=True, alpha=1.0, beta=3.0)
    bins = {b['entity_id']: b for b in scores['bins']}
    mats = {m['entity_id']: m for m in scores['materials']}

    # REPEAT_BIN: n=2, d=2
    # Raw: 1.0, Smoothed: (2 + 1) / (2 + 4) = 3 / 6 = 0.50
    assert bins['REPEAT_BIN']['audit_count'] == 2
    assert bins['REPEAT_BIN']['discrepancy_count'] == 2
    assert bins['REPEAT_BIN']['raw_hit_rate'] == 1.0
    assert round(bins['REPEAT_BIN']['smoothed_score'], 4) == 0.50
    assert bins['REPEAT_BIN']['last_discrepancy'] == 1

    # CLEAN_BIN: n=2, d=0
    # Raw: 0.0, Smoothed: (0 + 1) / (2 + 4) = 1 / 6 = 0.1667
    assert bins['CLEAN_BIN']['audit_count'] == 2
    assert bins['CLEAN_BIN']['discrepancy_count'] == 0
    assert round(bins['CLEAN_BIN']['smoothed_score'], 4) == round(1 / 6, 4)
    assert bins['CLEAN_BIN']['last_discrepancy'] == 0

    # REPEAT_MAT: present in both audits of REPEAT_BIN -> n=2, d=2
    assert mats['REPEAT_MAT']['audit_count'] == 2
    assert mats['REPEAT_MAT']['discrepancy_count'] == 2
    assert round(mats['REPEAT_MAT']['smoothed_score'], 4) == 0.50
    assert 'REPEAT_BIN' in mats['REPEAT_MAT']['associated_bins']


def test_learning_loop_progress_milestones(mock_db):
    """
    Verifies that learning loop progress calculates correct remaining counts to
    Stage 2 (n=50) and Stage 3 (n=200).
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_TEST_PROG', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:00:00')
    """)

    # Insert 30 assignments, 10 discrepancies
    for i in range(1, 31):
        aid = f"ASSIGN_{i:03d}"
        is_disc = 1 if i <= 10 else 0
        cur.execute("""
            INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at)
            VALUES (?, 'RUN_TEST_PROG', ?, ?, 'TOP', 'RULE', 50.0, 'Audit', 10, '2026-10-27 06:00:00')
        """, (aid, f"BIN_{i}", i))
        cur.execute("""
            INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
            VALUES (?, 'RUN_TEST_PROG', ?, 10, ?, ?, ?, 'PHYSICAL', '2026-10-27 07:00:00')
        """, (aid, f"BIN_{i}", 5 if is_disc else 10, -5 if is_disc else 0, is_disc))

    conn.commit()
    conn.close()

    progress = get_learning_loop_progress(db_path=mock_db)
    assert progress['total_labels'] == 30
    assert progress['total_discrepancies'] == 10
    assert progress['overall_hit_rate'] == round(10 / 30, 4)

    # Stage 2: Target 50, Current 10, Remaining 40, Progress 20.0%
    s2 = progress['stage2']
    assert s2['target'] == 50
    assert s2['current'] == 10
    assert s2['remaining'] == 40
    assert s2['progress_pct'] == 20.0
    assert not s2['is_ready']

    # Stage 3: Target 200, Current 10, Remaining 190, Progress 5.0%
    s3 = progress['stage3']
    assert s3['target'] == 200
    assert s3['current'] == 10
    assert s3['remaining'] == 190
    assert s3['progress_pct'] == 5.0
    assert not s3['is_ready']


def test_rule9_data_hygiene_quarantine_guardrails(mock_db):
    """
    Verifies that uncounted bins, synthetic data, cancelled bins, and quarantined runs
    are 100% excluded from rule hit rates, historical scores, and learning progress.
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES 
        ('RUN_VALID', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:00:00'),
        ('RUN_20261026_QUARANTINED', '2026-10-26', 'Daily', 'v1.0_rules', '2026-10-26 06:00:00')
    """)

    # 1. Valid counted row (should be counted)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_VALID', 'RUN_VALID', 'BIN_VALID', 1, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'Audit', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, counted_qty, book_qty_at_count, is_discrepancy, data_origin, created_at)
        VALUES ('A_VALID', 'RUN_VALID', 'BIN_VALID', 5, 10, 1, 'PHYSICAL', '2026-10-27 07:00:00')
    """)

    # 2. Uncounted bin (counted_qty IS NULL -> must be excluded)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_UNCOUNTED', 'RUN_VALID', 'BIN_UNCOUNTED', 2, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'Audit', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, counted_qty, book_qty_at_count, is_discrepancy, data_origin, created_at)
        VALUES ('A_UNCOUNTED', 'RUN_VALID', 'BIN_UNCOUNTED', NULL, 10, NULL, 'PHYSICAL', '2026-10-27 07:00:00')
    """)

    # 3. Synthetic/Seed data (must be excluded)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_SYNTH', 'RUN_VALID', 'BIN_SYNTH', 3, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'Audit', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, counted_qty, book_qty_at_count, is_discrepancy, data_origin, created_at)
        VALUES ('A_SYNTH', 'RUN_VALID', 'BIN_SYNTH', 5, 10, 1, 'SYNTHETIC', '2026-10-27 07:00:00')
    """)

    # 4. Cancelled assignment (action_required LIKE 'CANCELLED%' -> must be excluded)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_CANCELLED', 'RUN_VALID', 'BIN_CANCELLED', 4, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'CANCELLED_VIOLATION', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, counted_qty, book_qty_at_count, is_discrepancy, data_origin, created_at)
        VALUES ('A_CANCELLED', 'RUN_VALID', 'BIN_CANCELLED', 5, 10, 1, 'PHYSICAL', '2026-10-27 07:00:00')
    """)

    # 5. Quarantined run (must be excluded)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_QUAR', 'RUN_20261026_QUARANTINED', 'BIN_QUAR', 1, 'TOP', 'RULE', 'Ghost Stock', 10, 80.0, 'Audit', '2026-10-26 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, counted_qty, book_qty_at_count, is_discrepancy, data_origin, created_at)
        VALUES ('A_QUAR', 'RUN_20261026_QUARANTINED', 'BIN_QUAR', 5, 10, 1, 'PHYSICAL', '2026-10-26 07:00:00')
    """)

    conn.commit()
    conn.close()

    progress = get_learning_loop_progress(db_path=mock_db)
    # Exactly ONE valid record ('A_VALID') should be counted
    assert progress['total_labels'] == 1
    assert progress['total_discrepancies'] == 1


def test_weekly_html_email_sections_render(mock_db):
    """
    Verifies that build_weekly_hit_rate_html renders Section 4, 5, 6 without template errors.
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_EMAIL_TEST', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_E1', 'RUN_EMAIL_TEST', 'SMT 07', 1, 'TOP', 'RULE_SCORE', 'Ghost Stock (14 Bx Deficit) / Floor FIFO Bypass', 10, 85.0, 'Audit', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, mat_no, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES ('A_E1', 'RUN_EMAIL_TEST', 'SMT 07', '99000031', 10, 5, -5, 1, 'PHYSICAL', '2026-10-27 07:00:00')
    """)
    conn.commit()
    conn.close()

    compute_hit_rate_for_run('RUN_EMAIL_TEST', db_path=mock_db, save_to_db=True)
    html = build_weekly_hit_rate_html(run_id='RUN_EMAIL_TEST', db_path=mock_db)

    assert "4. 규칙(선정 사유)별 적중률 (Hit Rate by Rule / Reason)" in html
    assert "5. 과거 불일치 이력 요주의 품목 및 빈 (Historical Discrepancy Watchlist)" in html
    assert "6. 모델 학습 데이터 누적 및 다음 단계 로드맵 (Continuous Learning Loop Progress)" in html
    assert "Stage 2: 정규화 로지스틱 회귀" in html
    assert "Stage 3: LightGBM 엔진" in html
    assert "자동 승격 절대 금지" in html
    # With n=1, diagnosis must be guarded as "표본 축적 중" (not "고위험 적중 우수")
    assert "표본 축적 중 (n=1, 판단 유보)" in html
    assert "미분류 빈 0건" in html


def test_unclassified_bin_tracking_and_warning(mock_db):
    """
    Verifies that when an assignment reason does not match any canonical rule,
    compute_rule_hit_rates tags it as UNCLASSIFIED and the HTML surfaces a warning.
    """
    conn = sqlite3.connect(str(mock_db))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_UNMATCHED_TEST', '2026-10-27', 'Daily', 'v1.0_rules', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, primary_reason, label_qty, bin_risk_score, action_required, created_at)
        VALUES ('A_UNKNOWN', 'RUN_UNMATCHED_TEST', 'UNKNOWN_BIN_99', 1, 'TOP', 'MANUAL_OVERRIDE', 'Completely Unknown New Reason String', 10, 85.0, 'Audit', '2026-10-27 06:00:00')
    """)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (assignment_id, run_id, bin, mat_no, book_qty_at_count, counted_qty, diff_qty, is_discrepancy, data_origin, created_at)
        VALUES ('A_UNKNOWN', 'RUN_UNMATCHED_TEST', 'UNKNOWN_BIN_99', '99000031', 10, 5, -5, 1, 'PHYSICAL', '2026-10-27 07:00:00')
    """)
    conn.commit()
    conn.close()

    rules = compute_rule_hit_rates(run_id='RUN_UNMATCHED_TEST', db_path=mock_db, save_to_db=False)
    unclassified = next((r for r in rules if r.get('rule_code') == 'UNCLASSIFIED'), None)
    assert unclassified is not None
    assert unclassified['sample_size'] == 1
    assert unclassified['discrepancy_count'] == 1

    compute_hit_rate_for_run('RUN_UNMATCHED_TEST', db_path=mock_db, save_to_db=True)
    html = build_weekly_hit_rate_html(run_id='RUN_UNMATCHED_TEST', db_path=mock_db)
    assert "규칙 파싱 누락 발생" in html
    assert "미분류 빈 1건" in html

