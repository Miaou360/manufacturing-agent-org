"""
Model Training & Evaluation Pipeline Regression Test Suite
==========================================================
01_DEPT_Inventory / 00_JOB_StockAccuracy / tests / test_model_pipeline.py

Tests:
1. test_data_leakage_detection: Verifies strict detection and blocking of future timestamps.
2. test_label_plugins: Tests Zm212ProxyLabel join and AuditResultLabel NotImplementedError interface.
3. test_build_dataset_point_in_time: Validates dataset generation, aggregation to bin level, and multi-hot categories.
4. test_lgbm_training_guard: Verifies guardrail triggers when positive count < 200, and succeeds when >= 200.
5. test_evaluate_walk_forward_metrics: Validates computation of precision@15, precision@40, recall, IPW, and unselected floor error.
6. test_report_generation_and_warning: Verifies Markdown and CSV reports include the mandatory weak proxy warning header.
"""

import sys
import os
import sqlite3
import datetime
from pathlib import Path
import pytest
import pandas as pd
import numpy as np

CURRENT_DIR = Path(__file__).resolve().parent
JOB_DIR = CURRENT_DIR.parent
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(JOB_DIR / "model"))

from build_dataset import (
    verify_no_data_leakage,
    DataLeakageError,
    BaseLabelPlugin,
    Zm212ProxyLabel,
    AuditResultLabel,
    build_dataset_for_run
)
from train_lgbm import train_lgbm_model, predict_lgbm
from evaluate import (
    compute_ranking_metrics_at_k,
    compute_ipw_random_sample_error_rate,
    compute_unselected_error_rate,
    evaluate_models_on_dataset,
    run_walk_forward_evaluation,
    export_evaluation_report
)
from cause_codes import CAUSE_CODES, get_cause_code_info, list_valid_cause_codes
from db_manager import reconcile_in_flight_movement, save_audit_result, init_database
from audit_result_collector import record_single_audit_result, ingest_audit_results_from_excel

DEV_DB = JOB_DIR / "db" / "dev_stock_accuracy.db"
ZTCODE_STOCK_REC_DB = JOB_DIR.parent / "04_JOB_SystemReconciliation" / "db" / "system_reconciliation.db"

if DEV_DB.exists():
    init_database(DEV_DB)


def test_data_leakage_detection():
    """Verifies that any feature timestamp or column from the future raises DataLeakageError."""
    # 1. Clean dataset on or before run_date -> Must pass
    df_clean = pd.DataFrame({
        "bin": ["BIN-01", "BIN-02"],
        "created_at": ["2026-10-26 04:00:00", "2026-10-26 08:00:00"],
        "gr_date": ["2026-10-20", "2026-10-25"]
    })
    assert verify_no_data_leakage(df_clean, "2026-10-26") is True

    # 2. Leaked created_at after run_date -> Must raise DataLeakageError
    df_leaked_created = pd.DataFrame({
        "bin": ["BIN-01", "BIN-02"],
        "created_at": ["2026-10-26 04:00:00", "2026-10-27 00:00:01"],
        "gr_date": ["2026-10-20", "2026-10-25"]
    })
    with pytest.raises(DataLeakageError) as exc_info:
        verify_no_data_leakage(df_leaked_created, "2026-10-26")
    assert "Data Leakage Violation" in str(exc_info.value)

    # 3. Leaked gr_date after run_date -> Must raise DataLeakageError
    df_leaked_gr = pd.DataFrame({
        "bin": ["BIN-01"],
        "created_at": ["2026-10-26 04:00:00"],
        "gr_date": ["2026-10-28"]
    })
    with pytest.raises(DataLeakageError):
        verify_no_data_leakage(df_leaked_gr, "2026-10-26")

    # 4. Pre-existing target label in features -> Must raise DataLeakageError
    df_leaked_label = pd.DataFrame({
        "bin": ["BIN-01"],
        "created_at": ["2026-10-26 04:00:00"],
        "label": [1]
    })
    with pytest.raises(DataLeakageError):
        verify_no_data_leakage(df_leaked_label, "2026-10-26")


def test_label_plugins():
    """Validates Zm212ProxyLabel and AuditResultLabel interfaces and ground truth loading."""
    # 1. AuditResultLabel properties & live extraction
    audit_plugin = AuditResultLabel(db_path=DEV_DB)
    assert audit_plugin.is_weak_proxy is False
    assert audit_plugin.name == "audit_result_ground_truth"
    if DEV_DB.exists():
        labels = audit_plugin.get_labels(pd.DataFrame({"bin": ["SMT 9947", "CRT 03"]}), "2026-10-22")
        assert not labels.empty
        assert "label" in labels.columns
        assert "is_audited" in labels.columns
        assert bool(labels["is_weak_proxy"].iloc[0]) is False

    # 2. Zm212ProxyLabel properties
    zm_plugin = Zm212ProxyLabel(ztcode_stock_rec_db_path=ZTCODE_STOCK_REC_DB, min_days_forward=1, max_days_forward=7)
    assert zm_plugin.is_weak_proxy is True
    assert "ztcode_stock_rec_weak_proxy" in zm_plugin.name
    assert "Weak proxy" in zm_plugin.description or "weak proxy" in zm_plugin.description.lower()


def test_lgbm_training_guard_and_fit():
    """Verifies that LightGBM guard aborts when positives < 200 and trains when >= 200."""
    # Synthetic dataset
    np.random.seed(42)
    n_samples = 1000
    
    # Case A: Low positives (50 < 200) -> Guard triggers
    y_low = np.zeros(n_samples)
    y_low[:50] = 1
    df_low = pd.DataFrame({
        "bin_risk_score": np.random.uniform(0, 100, n_samples),
        "priority_ev": np.random.uniform(0, 50, n_samples),
        "primary_mover_class": np.random.choice(["Fast", "Normal", "Slow"], n_samples),
        "plant": "P001",
        "tier": np.random.choice([1, 2, 3], n_samples),
        "cat_CRITICAL_GHOST": np.random.choice([0, 1], n_samples),
        "label": y_low
    })

    res_skipped = train_lgbm_model(df_low, min_positives=200)
    assert res_skipped["status"] == "skipped"
    assert res_skipped["model"] is None
    assert res_skipped["pos_count"] == 50

    # Case B: Sufficient positives (250 >= 200) -> Model trains
    y_high = np.zeros(n_samples)
    y_high[:250] = 1
    df_high = df_low.copy()
    df_high["label"] = y_high
    
    res_trained = train_lgbm_model(df_high, min_positives=200)
    assert res_trained["status"] == "trained"
    assert res_trained["model"] is not None
    assert res_trained["train_auc"] > 0.5
    assert not res_trained["feature_importances"].empty

    # Verify predictions
    preds = predict_lgbm(res_trained, df_high)
    assert len(preds) == n_samples
    assert np.all(preds >= 0.0) and np.all(preds <= 1.0)


def test_metric_calculation_routines():
    """Validates Precision@K, Recall@K, Discovered Loss per Minute, IPW, and unselected error rate."""
    df_test = pd.DataFrame({
        "bin": [f"B-{i:03d}" for i in range(100)],
        "bin_risk_score": list(range(100, 0, -1)),  # B-000 has 100, B-099 has 1
        "sum_expected_loss": [100.0] * 100,
        "financial_exposure": [200.0] * 100,
        "est_minutes": [2.0] * 100,
        "label": [1] * 10 + [0] * 90,  # Top 10 are positives, remaining 90 are 0
        "is_selected": [1] * 40 + [0] * 60,
        "list_type": ["RAPID_FLOOR"] * 15 + ["EXTENDED_SECONDARY"] * 22 + ["RANDOM_VALIDATION"] * 3 + ["UNSELECTED"] * 60,
        "inclusion_probability": [1.0] * 37 + [0.05] * 3 + [0.05] * 60
    })

    # 1. Metrics at K=15
    m15 = compute_ranking_metrics_at_k(df_test, score_col="bin_risk_score", k=15)
    # Top 15 contains all 10 positives and 5 negatives
    assert m15["hits@15"] == 10
    assert m15["precision@15"] == pytest.approx(10 / 15, abs=1e-4)
    assert m15["recall@15"] == pytest.approx(1.0, abs=1e-4)
    assert m15["audit_minutes@15"] == pytest.approx(30.0, abs=1e-4)  # 15 bins * 2.0m
    assert m15["discovered_loss@15"] == pytest.approx(2000.0, abs=1e-4)  # 10 pos * $200
    assert m15["loss_per_min@15"] == pytest.approx(2000.0 / 30.0, abs=1e-4)

    # 2. IPW Random Sample Error Rate
    # Set 1 of the 3 random validation bins to 1
    df_test.loc[df_test["list_type"] == "RANDOM_VALIDATION", "label"] = [1, 0, 0]
    ipw_rate, cnt = compute_ipw_random_sample_error_rate(df_test)
    assert cnt == 3
    assert ipw_rate == pytest.approx(1 / 3, abs=1e-4)

    # 3. Unselected Floor Error Rate
    df_test.loc[df_test["is_selected"] == 0, "label"] = [1] * 6 + [0] * 54  # 6 / 60 = 10%
    unsel_rate, err_cnt, tot_unsel = compute_unselected_error_rate(df_test)
    assert tot_unsel == 60
    assert err_cnt == 6
    assert unsel_rate == pytest.approx(0.10, abs=1e-4)


def test_build_dataset_and_evaluate_end_to_end(tmp_path):
    """
    End-to-End Test: Verifies build_dataset and evaluate run cleanly on dev DB
    and generate Markdown and CSV reports with the mandatory warning banner.
    """
    if not DEV_DB.exists():
        pytest.skip(f"Dev DB not found at {DEV_DB}. Skipping live DB integration test.")

    # Check available runs in dev DB
    conn = sqlite3.connect(f"file:{DEV_DB.resolve().as_posix()}?mode=ro", uri=True)
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT run_date FROM AUDIT_RUN ORDER BY run_date;")
    runs = [r[0] for r in cur.fetchall()]
    conn.close()

    if not runs:
        pytest.skip("No AUDIT_RUN found in dev DB.")

    test_date = runs[-1]
    
    # 1. Build Dataset
    ds = build_dataset_for_run(
        run_date=test_date,
        db_path=DEV_DB,
        ztcode_stock_rec_db_path=ZTCODE_STOCK_REC_DB
    )
    assert not ds.empty
    assert "bin" in ds.columns
    assert "bin_risk_score" in ds.columns
    assert "sum_expected_loss" in ds.columns
    assert "priority_ev" in ds.columns
    assert "label" in ds.columns
    assert "cat_CRITICAL_GHOST" in ds.columns

    # 2. Run Evaluation with weak proxy
    report_dir = tmp_path / "test_reports"
    metrics_df, meta_info = run_walk_forward_evaluation(
        run_dates=[test_date],
        db_path=DEV_DB,
        ztcode_stock_rec_db_path=ZTCODE_STOCK_REC_DB,
        label_source="proxy",
        min_positives_for_lgbm=200
    )
    assert not metrics_df.empty
    assert "precision@15" in metrics_df.columns
    assert "precision@40" in metrics_df.columns
    assert "loss_per_min@15" in metrics_df.columns
    assert "random_sample_ipw_rate" in metrics_df.columns
    assert "unselected_floor_error_rate" in metrics_df.columns

    # 3. Export Reports and Verify Warning Block
    md_file, csv_file = export_evaluation_report(metrics_df, meta_info, reports_dir=report_dir)
    assert md_file.exists()
    assert csv_file.exists()

    md_content = md_file.read_text(encoding="utf-8")
    assert "대리 라벨(Weak Proxy Label) 해석상 한계 및 주의사항" in md_content
    assert "[!WARNING]" in md_content
    assert "AUDIT_RESULT Ground Truth" in md_content


def test_cause_codes_registry():
    """Validates root cause registry structure and validation helpers."""
    codes = list_valid_cause_codes()
    assert "CC01_GHOST_MISSING_GI" in codes
    assert "CC07_IN_FLIGHT_MOVEMENT" in codes
    assert "CC08_MATCH_VERIFIED" in codes

    # Valid info lookup
    info_match = get_cause_code_info("CC08_MATCH_VERIFIED")
    assert info_match["is_discrepancy"] is False
    assert info_match["code"] == "CC08_MATCH_VERIFIED"

    # Invalid code raises ValueError when allow_fallback=False
    with pytest.raises(ValueError):
        get_cause_code_info("INVALID_CODE_999", allow_fallback=False)

    # Graceful fallback returns CC99
    info_fb = get_cause_code_info("INVALID_CODE_999", allow_fallback=True)
    assert info_fb["code"] == "CC99_OTHER_UNIDENTIFIED"


def test_in_flight_movement_reconciliation(tmp_path):
    """
    Validates in-flight movement reconciliation:
    Case A: Offset matched (effective_book_qty == counted_qty) -> true_diff_qty = 0, in_flight_offset_matched = True
    Case B: Genuine discrepancy -> true_diff_qty accurately reflects true delta after ΔGR - ΔGI.
    """
    db_file = tmp_path / "test_inflight.db"
    init_database(db_file)
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    # Insert mock in-flight GR (100 units into BIN-A between 04:00 and 05:00)
    cur.execute("""
        INSERT INTO GR_TRANSACTIONS (barcode, mat_no, gr_loc, gr_time, gr_qty, created_at)
        VALUES ('BAR-01', 'MAT-100', 'BIN-A', '2026-10-26 04:30:00', 100.0, '2026-10-26 04:30:00')
    """)
    # Insert mock in-flight GI (30 units out of BIN-A between 04:00 and 05:00)
    cur.execute("""
        INSERT INTO GI_TRANSACTIONS (barcode, mat_no, gi_loc, gi_time, qty, created_at)
        VALUES ('BAR-02', 'MAT-100', 'BIN-A', '2026-10-26 04:45:00', 30.0, '2026-10-26 04:45:00')
    """)
    conn.commit()

    # Case A: Snapshot book qty = 200. Counted qty = 270.
    # In-flight Δ = +100 - 30 = +70. Effective book = 270.
    # Apparent raw diff = +70, but true diff = 0!
    res_a = reconcile_in_flight_movement(
        conn=conn,
        bin_val="BIN-A",
        mat_no="MAT-100",
        snapshot_time="2026-10-26 04:00:00",
        counted_at="2026-10-26 05:00:00",
        book_qty_at_count=200.0,
        counted_qty=270.0
    )
    assert res_a["in_flight_gr_qty"] == 100.0
    assert res_a["in_flight_gi_qty"] == 30.0
    assert res_a["effective_book_qty"] == 270.0
    assert res_a["raw_diff_qty"] == 70.0
    assert res_a["true_diff_qty"] == 0.0
    assert res_a["in_flight_offset_matched"] is True

    # Case B: Counted qty = 250 (Shortage of 20 despite in-flight movements)
    res_b = reconcile_in_flight_movement(
        conn=conn,
        bin_val="BIN-A",
        mat_no="MAT-100",
        snapshot_time="2026-10-26 04:00:00",
        counted_at="2026-10-26 05:00:00",
        book_qty_at_count=200.0,
        counted_qty=250.0
    )
    assert res_b["effective_book_qty"] == 270.0
    assert res_b["true_diff_qty"] == -20.0
    assert res_b["in_flight_offset_matched"] is False

    conn.close()


def test_recount_priority_and_wms_admin_exclusion(tmp_path):
    """
    Verifies that:
    1. Recounts (1:N) take precedence over initial counts for the same assignment.
    2. Administrative WMS adjustments (audit_origin='WMS_ADMIN_ADJUSTMENT') are excluded.
    """
    db_file = tmp_path / "test_recount.db"
    init_database(db_file)
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('TEST_RUN_01', '2026-10-26', 'Daily', 'v1.0', '2026-10-26 04:00:00');
    """)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at)
        VALUES ('ASSIGN-01', 'TEST_RUN_01', 'BIN-101', 1, 'RAPID_FLOOR', 'TOP_RISK', 95.0, 'COUNT', 100, '2026-10-26 04:00:00');
    """)

    # 1. Initial count: counted 80 (diff -20), is_recount=0, is_confirmed=0
    cur.execute("""
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at, book_qty_at_count, counted_qty, diff_qty, diff_amount,
            cause_code, minutes_spent, counter_id, is_recount, is_confirmed, audit_origin, created_at
        ) VALUES (
            'ASSIGN-01', 'TEST_RUN_01', 'BIN-101', 'MAT-1', '2026-10-26 05:00:00', 100.0, 80.0, -20.0, -200.0,
            'CC01_GHOST_MISSING_GI', 2.0, 'WORKER_01', 0, 0, 'PHYSICAL_CYCLE_COUNT', '2026-10-26 05:00:00'
        );
    """)

    # 2. Confirmed recount: supervisor recount found 75 (diff -25), is_recount=1, is_confirmed=1
    cur.execute("""
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at, book_qty_at_count, counted_qty, diff_qty, diff_amount,
            cause_code, minutes_spent, counter_id, is_recount, is_confirmed, audit_origin, created_at
        ) VALUES (
            'ASSIGN-01', 'TEST_RUN_01', 'BIN-101', 'MAT-1', '2026-10-26 06:00:00', 100.0, 75.0, -25.0, -250.0,
            'CC01_GHOST_MISSING_GI', 3.0, 'SUPERVISOR_01', 1, 1, 'PHYSICAL_CYCLE_COUNT', '2026-10-26 06:00:00'
        );
    """)

    # 3. Administrative WMS edit: is_confirmed=1, but audit_origin='WMS_ADMIN_ADJUSTMENT' (MUST BE EXCLUDED)
    cur.execute("""
        INSERT INTO AUDIT_RESULT (
            assignment_id, run_id, bin, mat_no, counted_at, book_qty_at_count, counted_qty, diff_qty, diff_amount,
            cause_code, minutes_spent, counter_id, is_recount, is_confirmed, audit_origin, created_at
        ) VALUES (
            'ASSIGN-01', 'TEST_RUN_01', 'BIN-101', 'MAT-1', '2026-10-26 07:00:00', 100.0, 100.0, 0.0, 0.0,
            'CC08_MATCH_VERIFIED', 0.5, 'SAP_BATCH_JOB', 0, 1, 'WMS_ADMIN_ADJUSTMENT', '2026-10-26 07:00:00'
        );
    """)
    conn.commit()
    conn.close()

    plugin = AuditResultLabel(db_path=db_file)
    df_features = pd.DataFrame({"bin": ["BIN-101"]})
    df_labels = plugin.get_labels(df_features, "2026-10-26")

    assert len(df_labels) == 1
    # Must have picked the confirmed recount (-25.0), NOT the unconfirmed initial (-20.0), NOT the admin edit (0.0)
    assert df_labels["label"].iloc[0] == 1
    assert df_labels["discrepancy_qty"].iloc[0] == 25.0
    assert df_labels["financial_exposure"].iloc[0] == 250.0
    assert df_labels["is_audited"].iloc[0] == 1


def test_excel_feedback_sheet_ingestion(tmp_path):
    """Verifies parsing of returned Excel sheets and ingestion into AUDIT_RESULT."""
    db_file = tmp_path / "test_excel_ingest.db"
    init_database(db_file)
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_EXCEL_01', '2026-10-26', 'Daily', 'v1.0', '2026-10-26 04:00:00');
    """)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at)
        VALUES ('ASSIGN-EX-01', 'RUN_EXCEL_01', 'BIN-EX-01', 1, 'RAPID_FLOOR', 'TOP_RISK', 90.0, 'COUNT', 50, '2026-10-26 04:00:00');
    """)
    conn.commit()
    conn.close()

    # Create dummy Excel file
    excel_file = tmp_path / "Returned_Count_Sheet.xlsx"
    df_excel = pd.DataFrame({
        "Bin #": ["BIN-EX-01"],
        "Count QTY": [45],
        "Cause Code": ["CC04_PARTIAL_PICK_UNSCANNED"],
        "Minutes Spent": [2.5],
        "Counter ID": ["FIELD_OP_99"],
        "Is Recount": ["N"],
        "Remark": ["Box open on rack"]
    })
    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        df_excel.to_excel(writer, sheet_name="Cycle_Count_Target_Bins", index=False)

    res = ingest_audit_results_from_excel(excel_file, db_path=db_file, run_id="RUN_EXCEL_01")
    assert res["status"] == "success"
    assert res["ingested_count"] == 1

    # Verify persisted record
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("SELECT counted_qty, diff_qty, cause_code, counter_id FROM AUDIT_RESULT WHERE assignment_id='ASSIGN-EX-01'")
    row = cur.fetchone()
    conn.close()

    assert row is not None
    assert row[0] == 45.0
    assert row[1] == -5.0
    assert row[2] == "CC04_PARTIAL_PICK_UNSCANNED"
    assert row[3] == "FIELD_OP_99"


from audit_constants import (
    LIST_TYPE_RAPID,
    LIST_TYPE_EXTENDED,
    LIST_TYPE_RANDOM,
    DATA_ORIGIN_PHYSICAL,
    DATA_ORIGIN_SYNTHETIC,
    AUDIT_ORIGIN_PHYSICAL
)
from db_manager import DB_PATH
from audit_result_collector import seed_realistic_audit_results_for_run


def test_audit_constants_integrity():
    """Verifies that all standard audit constants are properly defined."""
    assert LIST_TYPE_RAPID == "RAPID_FLOOR"
    assert LIST_TYPE_EXTENDED == "EXTENDED_SECONDARY"
    assert LIST_TYPE_RANDOM == "RANDOM"
    assert DATA_ORIGIN_PHYSICAL == "PHYSICAL"
    assert DATA_ORIGIN_SYNTHETIC == "SYNTHETIC_SEED"
    assert AUDIT_ORIGIN_PHYSICAL == "PHYSICAL_CYCLE_COUNT"


def test_seed_safeguard_prevents_production_mutation():
    """Verifies that attempting to seed synthetic audit results into production DB is strictly blocked."""
    with pytest.raises(ValueError, match="CRITICAL SECURITY GUARD"):
        seed_realistic_audit_results_for_run("20261026_DAILY", db_path=DB_PATH)


def test_synthetic_seed_report_banner(tmp_path):
    """Verifies that evaluation report displays [!WARNING] Synthetic Seed banner when evaluating seeded dev data."""
    if not DEV_DB.exists():
        pytest.skip(f"Dev DB not found at {DEV_DB}.")

    report_dir = tmp_path / "synthetic_reports"
    metrics_df, meta_info = run_walk_forward_evaluation(
        run_dates=["2026-10-22"],
        db_path=DEV_DB,
        label_source="audit_result",
        min_positives_for_lgbm=200
    )
    md_file, csv_file = export_evaluation_report(metrics_df, meta_info, reports_dir=report_dir)
    assert md_file.exists()
    md_text = md_file.read_text(encoding="utf-8")
    assert "> [!WARNING]" in md_text
    assert "합성 시드 데이터 기반 파이프라인 검증 알림" in md_text
    assert "순환 평가 주의" in md_text
    assert "파이프라인의 종단 간 연산 무결성을 검증하기 위한 기술적 지표" in md_text


def test_physical_ground_truth_report_banner(tmp_path):
    """Verifies that evaluation report displays [!NOTE] Ground Truth banner when label data origin is PHYSICAL."""
    report_dir = tmp_path / "physical_reports"
    meta_info = {
        "is_weak_proxy": False,
        "has_synthetic": False,
        "data_origin": "PHYSICAL",
        "dates": ["2026-10-26"],
        "label_source": "audit_result_ground_truth",
        "label_description": "Verified Physical Cycle Count",
        "lgbm_guard_reports": []
    }
    dummy_metrics = pd.DataFrame([{
        "eval_date": "2026-10-26",
        "model_name": "Domain Rules",
        "precision@15": 0.8,
        "precision@40": 0.7,
        "recall": 0.6,
        "ipw_error_rate": 0.05,
        "unselected_floor_error_rate": 0.02
    }])
    md_file, csv_file = export_evaluation_report(dummy_metrics, meta_info, reports_dir=report_dir)
    assert md_file.exists()
    md_text = md_file.read_text(encoding="utf-8")
    assert "> [!NOTE]" in md_text
    assert "현장 실사 확정 실적 기반 평가" in md_text
    assert "입출고 시차 자동 보정" in md_text
    assert "1:N 재실사(Recount)" in md_text
    assert "> [!WARNING]" not in md_text


def test_excel_feedback_sheet_with_ok_ng_and_print_setup(tmp_path):
    """
    Verifies:
    1. Parsing of user-friendly Excel feedback sheets with 'Physical Match? (OK/NG)' column.
    2. 'OK' with blank count correctly auto-derives counted_qty = book_qty, cause = CC08_MATCH_VERIFIED.
    3. 'NG' with count correctly persists discrepancy and cause.
    """
    db_file = tmp_path / "test_ok_ng.db"
    init_database(db_file)
    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO AUDIT_RUN (run_id, run_date, mode, scoring_version, created_at)
        VALUES ('RUN_OKNG_01', '2026-10-26', 'Daily', 'v1.0', '2026-10-26 04:00:00');
    """)
    cur.execute("""
        INSERT INTO AUDIT_ASSIGNMENT (assignment_id, run_id, bin, rank, list_type, selection_method, bin_risk_score, action_required, label_qty, created_at)
        VALUES 
        ('ASSIGN-OK-01', 'RUN_OKNG_01', 'BIN-OK-01', 1, 'RAPID_FLOOR', 'TOP_RISK', 80.0, 'COUNT', 10, '2026-10-26 04:00:00'),
        ('ASSIGN-NG-02', 'RUN_OKNG_01', 'BIN-NG-02', 2, 'RAPID_FLOOR', 'TOP_RISK', 95.0, 'COUNT', 20, '2026-10-26 04:00:00');
    """)
    conn.commit()
    conn.close()

    excel_file = tmp_path / "Returned_Feedback_OKNG.xlsx"
    df_excel = pd.DataFrame([
        {
            "Rank": 1,
            "Bin #": "BIN-OK-01",
            "Part #": "PART-01",
            "Label QTY": 10,
            "Physical Match? (OK/NG)": "OK",
            "Counted QTY": None,
            "Diff QTY": None,
            "Discrepancy Cause": "",
            "Auditor": "LEAD_A",
            "Count Time": "2026-10-26 10:15:00",
            "Field Notes": "Rack clean"
        },
        {
            "Rank": 2,
            "Bin #": "BIN-NG-02",
            "Part #": "PART-02",
            "Label QTY": 20,
            "Physical Match? (OK/NG)": "NG",
            "Counted QTY": 14,
            "Diff QTY": -6,
            "Discrepancy Cause": "CC01_GHOST_MISSING_GI",
            "Auditor": "LEAD_A",
            "Count Time": "2026-10-26 10:20:00",
            "Field Notes": "6 boxes consumed at line"
        }
    ])
    with pd.ExcelWriter(excel_file, engine="openpyxl") as writer:
        df_excel.to_excel(writer, sheet_name="Cycle_Count_Target_Bins", index=False)

    res = ingest_audit_results_from_excel(excel_file, db_path=db_file, run_id="RUN_OKNG_01")
    assert res["status"] == "success"
    assert res["ingested_count"] == 2

    conn = sqlite3.connect(str(db_file))
    cur = conn.cursor()
    cur.execute("SELECT bin, counted_qty, diff_qty, cause_code, counter_id, notes FROM AUDIT_RESULT ORDER BY bin")
    rows = cur.fetchall()
    conn.close()

    assert len(rows) == 2
    # Row 1: BIN-NG-02
    assert rows[0][0] == "BIN-NG-02"
    assert rows[0][1] == 14.0
    assert rows[0][2] == -6.0
    assert rows[0][3] == "CC01_GHOST_MISSING_GI"
    assert rows[0][4] == "LEAD_A"

    # Row 2: BIN-OK-01
    assert rows[1][0] == "BIN-OK-01"
    assert rows[1][1] == 10.0  # Auto-derived from book_qty
    assert rows[1][2] == 0.0
    assert rows[1][3] is None  # Ground Truth Rule 9: unprovided cause_code stays NULL
    assert rows[1][4] == "LEAD_A"


