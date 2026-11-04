import importlib.util
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
import pandas as pd
import pytest

DEPT_DIR = Path(__file__).resolve().parents[1]


def _load_module(mod_name: str, file_path: Path, monkeypatch=None, job_dir: Path = None):
    if monkeypatch is not None and job_dir is not None:
        monkeypatch.syspath_prepend(str(job_dir))
        for shared_mod in ("config", "db_manager"):
            monkeypatch.delitem(sys.modules, shared_mod, raising=False)
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, mod_name, mod)
    else:
        sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_job00_export_audit_excel_dryrun_uses_scratch_and_preserves_canonical(tmp_path, monkeypatch):
    job00 = DEPT_DIR / "00_JOB_StockAccuracy"
    if str(job00) not in sys.path:
        sys.path.insert(0, str(job00))
    bat_mod = _load_module("job00_bat_dryrun_test", job00 / "box_aging_tracker.py")

    fake_audit_dir = tmp_path / "audit_reports"
    fake_scratch_dir = tmp_path / "job00_dir" / "scratch"
    fake_audit_dir.mkdir(parents=True)
    monkeypatch.setattr(bat_mod, "AUDIT_REPORTS_DIR", fake_audit_dir)
    monkeypatch.setattr(bat_mod, "CURRENT_DIR", tmp_path / "job00_dir")

    today_str = datetime.now().strftime("%Y%m%d")
    canonical_file = fake_audit_dir / f"WMS_Cycle_Count_Audit_List_{today_str}.xlsx"
    canonical_file.write_bytes(b"ORIGINAL_FLOOR_LIST_BYTES")
    mtime_before = canonical_file.stat().st_mtime

    tracker = bat_mod.BoxAgingTracker.__new__(bat_mod.BoxAgingTracker)
    tracker.scoring_config = {"shadow_ev": {"include_in_admin_excel": False}}
    analysis = {
        "suspects": pd.DataFrame(),
        "top_bins": [{"BIN": "A1-1-1", "Parts_List": "88100011", "Label_QTY": 2}],
        "extended_bins": [],
        "random_bins": [],
        "open_mismatches": [],
        "shadow_ev_ranking": [],
    }

    out = tracker.export_audit_excel(analysis, mode="Daily", dry_run=True)
    assert out.exists()
    assert out.parent.resolve() == fake_scratch_dir.resolve()
    assert out.name.startswith("DRYRUN_WMS_Cycle_Count_Audit_List_")
    assert canonical_file.read_bytes() == b"ORIGINAL_FLOOR_LIST_BYTES"
    assert canonical_file.stat().st_mtime == mtime_before


def test_job01_resolve_gi_and_daily_report_dryrun_write_only_to_scratch(tmp_path, monkeypatch):
    job01 = DEPT_DIR / "01_JOB_GR_GI_Error"
    if str(job01) not in sys.path:
        sys.path.insert(0, str(job01))

    res_mod = _load_module("job01_res_dryrun_test", job01 / "gr_gi_resolver.py")
    fake_base = tmp_path / "job01"
    fake_ops_logs = fake_base / "resolution_logs"
    fake_ops_logs.mkdir(parents=True)
    monkeypatch.setattr(res_mod, "BASE_DIR", fake_base)
    monkeypatch.setattr(res_mod, "OUTPUT_DIR", fake_ops_logs)
    monkeypatch.setattr(res_mod, "MB52_DIR", fake_base / "mb52")
    monkeypatch.setattr(res_mod, "WMS_DIR", fake_base / "wms")
    monkeypatch.setattr(res_mod, "load_mb52_stock", lambda *a, **kw: pd.DataFrame())
    monkeypatch.setattr(res_mod, "load_wms_stock", lambda *a, **kw: pd.DataFrame())

    gi_tsv = tmp_path / "gi_sample.xls"
    gi_tsv.write_text(
        "Plant\tMaterial Number\tMovement Type\tStorage Location\tReceiving Stor. Loc.\tQuantity\tMessage\tDocument Date\tPosting Date\n"
        "P999\t88100012\t311\tSL01\tSP31\t10\tDeficit of SL Unrestricted-use 10 EA\t2026.10.28\t2026.10.28\n",
        encoding="utf-16",
    )

    sig_db = tmp_path / "sig.db"
    plan_df = res_mod.resolve_gi_errors(
        gi_path=gi_tsv,
        gr_path=None,
        save_excel=True,
        record_signals=True,
        signal_db_path=sig_db,
        dry_run=True,
    )
    out_file = Path(plan_df.attrs["output_file"])
    assert out_file.exists()
    assert out_file.parent.resolve() == (fake_base / "scratch").resolve()
    assert out_file.name.startswith("DRYRUN_GI_Resolution_Plan_")
    assert list(fake_ops_logs.glob("*")) == []
    assert not sig_db.exists()

    rep_mod = _load_module("job01_d106_dryrun_test", job01 / "run_daily_gi_report.py")
    monkeypatch.setattr(rep_mod, "BASE_DIR", fake_base)
    db_file = tmp_path / "gi_hist.db"
    db_mgr = _load_module("job01_db_dryrun_test", job01 / "db_manager.py")
    db_mgr.init_db(db_file)
    rep_res = rep_mod.dispatch_daily_gi_report(
        slot="0730",
        report_date="2026-10-29",
        dry_run=True,
        db_path=db_file,
        resolution_logs_dir=fake_ops_logs,
    )
    assert rep_res["status"] == "DRY_RUN_OK"
    scratch_html = Path(rep_res["scratch_html_path"])
    assert scratch_html.exists()
    assert scratch_html.parent.resolve() == (fake_base / "scratch").resolve()
    assert scratch_html.name.startswith("DRYRUN_Daily_GI_Report_")


def test_job02_backlog_resolver_and_notifier_dryrun_write_only_to_scratch(tmp_path, monkeypatch):
    job02 = DEPT_DIR / "02_JOB_Backlog_Clear"
    if str(job02) not in sys.path:
        sys.path.insert(0, str(job02))

    bres_mod = _load_module("job02_res_dryrun_test", job02 / "backlog_resolver.py")
    fake_base = tmp_path / "job02"
    fake_logs = fake_base / "resolution_logs"
    fake_logs.mkdir(parents=True)
    monkeypatch.setattr(bres_mod, "BASE_DIR", fake_base)
    monkeypatch.setattr(bres_mod, "LOGS_DIR", fake_logs)

    stock_matrix = pd.DataFrame([
        {"Material": "88100013", "SLoc": "SL02", "SAP_Stock": 100.0, "WMS_Stock": 50.0, "Surplus": 50.0},
        {"Material": "88100013", "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
    ])
    cogi_items = [{
        "Material": "88100013",
        "Material_Desc": "LAMP ASSY TEST",
        "SLoc": "SP31",
        "Qty": 10.0,
        "Error_Code": "M7021",
        "Can_Resolve_By_Transfer": True,
        "Error_Date": "2026.10.28",
    }]
    plan_df = bres_mod.build_backlog_resolution_plan(
        cogi_items,
        save_excel=True,
        stock_matrix=stock_matrix,
        active_gr_errors=[],
        active_gi_errors=[],
        dry_run=True,
    )
    out_plan = Path(plan_df.attrs["output_file"])
    assert out_plan.exists()
    assert out_plan.parent.resolve() == (fake_base / "scratch").resolve()
    assert out_plan.name.startswith("DRYRUN_Backlog_Resolution_Plan_")
    assert list(fake_logs.glob("*")) == []

    not_mod = _load_module("job02_not_dryrun_test", job02 / "notifier.py")
    monkeypatch.setattr(not_mod, "BASE_DIR", fake_base)
    monkeypatch.setattr(not_mod, "REPORTS_DIR", fake_logs)
    unres_path = not_mod.generate_unresolved_excel(
        plan_df=plan_df,
        run_date="20261029",
        final_cogi_items=cogi_items,
        remaining_deficits=[],
        dry_run=True,
    )
    assert unres_path is not None and unres_path.exists()
    assert unres_path.parent.resolve() == (fake_base / "scratch").resolve()
    assert unres_path.name.startswith("DRYRUN_Unresolved_Backlog_Report_")
    assert list(fake_logs.glob("*")) == []


def test_job03_report_generator_dryrun_writes_to_scratch(tmp_path, monkeypatch):
    job03 = DEPT_DIR / "03_JOB_Backlog_Analysis"
    rg_mod = _load_module("job03_rg_dryrun_test", job03 / "report_generator.py", monkeypatch, job03)
    fake_reports = tmp_path / "reports"
    fake_scratch = tmp_path / "scratch"
    fake_reports.mkdir(parents=True)
    monkeypatch.setattr(rg_mod, "REPORTS_DIR", fake_reports)

    gen = rg_mod.BacklogReportGenerator(output_dir=fake_scratch)
    mat_entry = {
        "priority": 1,
        "material": "88100014",
        "material_desc": "BRACKET TEST",
        "slocs": ["SP31"],
        "occurrence_days": 1,
        "frequency_pct": 16.7,
        "total_deficit_qty": 10.0,
        "avg_deficit_qty": 10.0,
        "max_deficit_qty": 10.0,
        "transferred_qty": 10.0,
        "resolvable_qty": 10.0,
        "unresolvable_qty": 0.0,
        "resolvability_rate_pct": 100.0,
        "avg_aging_days": 0.0,
        "max_aging_days": 0,
        "primary_sloc": "SP31",
        "mover_class": "FAST",
        "median_turnover_days": 3.0,
        "std_turnover_days": 3.0,
        "quadrant_code": "Q4_MINOR_INFREQUENT",
        "quadrant_name": "Q4",
        "is_line_stop_risk": False,
        "is_line_stop_hazard": False,
        "action_item": "Monitor",
        "recommendation": "Monitor",
    }
    sample_result = {
        "period": {"start_date": "2026-10-23", "end_date": "2026-10-28", "total_audit_days": 6},
        "summary": {
            "total_materials": 1,
            "total_cumulative_qty": 10.0,
            "overall_resolvability_pct": 100.0,
            "line_stop_hazard_count": 0,
            "q1_count": 0,
            "q2_count": 0,
            "q3_count": 0,
            "q4_count": 1,
        },
        "thresholds": {"freq_median": 1.0, "qty_median": 10.0},
        "materials": [mat_entry],
        "quadrant_summary": {
            "Q1_CHRONIC_MASSIVE": [],
            "Q2_CHRONIC_OPERATIONAL": [],
            "Q3_CRITICAL_SURGE": [],
            "Q4_MINOR_INFREQUENT": [mat_entry],
        },
        "daily_trend": [{
            "run_date": "2026-10-28",
            "item_count": 1,
            "total_qty": 10.0,
            "resolved_items": 1,
            "resolved_qty": 10.0,
            "unresolved_items": 0,
            "unresolved_qty": 0.0,
        }],
        "sloc_breakdown": [{"sloc": "SP31", "item_count": 1, "total_qty": 10.0, "share_pct": 100.0}],
        "top_bottlenecks": {"by_frequency": [], "by_volume": [], "line_stop_hazards": []},
    }
    out_xlsx = gen.generate_excel_report(sample_result, dry_run=True)
    assert out_xlsx.exists()
    assert out_xlsx.parent.resolve() == fake_scratch.resolve()
    assert out_xlsx.name.startswith("DRYRUN_Backlog_Deep_Analysis_")
    assert list(fake_reports.glob("*")) == []


def test_job04_reconciliation_dryrun_preserves_latest_and_skips_db(tmp_path, monkeypatch):
    job04 = DEPT_DIR / "04_JOB_SystemReconciliation"
    rnot_mod = _load_module("job04_rnot_dryrun_test", job04 / "reconciliation_notifier.py", monkeypatch, job04)
    fake_base = tmp_path / "job04"
    fake_reports = fake_base / "reports"
    fake_reports.mkdir(parents=True)
    latest_md = fake_reports / "Reconciliation_Report_latest.md"
    latest_md.write_text("CANONICAL_LATEST_MD", encoding="utf-8")

    monkeypatch.setattr(rnot_mod, "BASE_DIR", fake_base)
    monkeypatch.setattr(rnot_mod, "REPORTS_DIR", fake_reports)
    monkeypatch.setattr(rnot_mod, "get_top_financial_risks", lambda *a, **kw: [])
    monkeypatch.setattr(rnot_mod, "get_all_discrepancies", lambda *a, **kw: [])
    monkeypatch.setattr(rnot_mod, "get_chronic_ghost_items", lambda *a, **kw: [])
    monkeypatch.setattr(rnot_mod, "get_baseline_summary", lambda *a, **kw: {})

    summary = {
        "snapshot_date": "2026-10-29",
        "posting_date": "2026-10-28",
        "plant": "P999",
        "system_group": "IWMS",
        "total_items": 100,
        "matched_items": 98,
        "mismatched_items": 2,
        "accuracy_rate": 98.0,
        "avg_score": 99.1,
        "dod_accuracy_delta": 0.2,
        "total_diff_qty": 5.0,
        "total_financial_exposure": 50.0,
    }
    md_out = rnot_mod.generate_reconciliation_report(summary=summary, dry_run=True)
    xlsx_out = rnot_mod.generate_reconciliation_excel(summary=summary, dry_run=True)
    assert md_out.exists() and md_out.parent.resolve() == (fake_base / "scratch").resolve()
    assert md_out.name.startswith("DRYRUN_Reconciliation_Report_")
    assert xlsx_out.exists() and xlsx_out.parent.resolve() == (fake_base / "scratch").resolve()
    assert xlsx_out.name.startswith("DRYRUN_Reconciliation_Report_")
    assert latest_md.read_text(encoding="utf-8") == "CANONICAL_LATEST_MD"
    assert list(fake_reports.glob("*.xlsx")) == []


def test_job05_portrait_report_dryrun_preserves_canonical_latest(tmp_path, monkeypatch):
    job05 = DEPT_DIR / "05_JOB_SL01_R_Bins"
    op_mod = _load_module("job05_op_dryrun_test", job05 / "sl01_r_bins_operator.py", monkeypatch, job05)
    db_mod = _load_module("job05_db_dryrun_test", job05 / "db_manager.py", monkeypatch, job05)
    fake_base = tmp_path / "job05"
    fake_reports = fake_base / "reports"
    fake_reports.mkdir(parents=True)
    canonical_latest = fake_reports / "SL01_R_Bins_Lag_Report_latest.xlsx"
    canonical_latest.write_bytes(b"CANONICAL_05_LATEST")

    tmp_db = tmp_path / "sl01.db"
    db_mod.init_db(tmp_db)
    monkeypatch.setattr(op_mod, "BASE_DIR", fake_base)
    monkeypatch.setattr(op_mod, "REPORTS_DIR", fake_reports)

    out_xlsx = op_mod.generate_portrait_excel_report(
        scan_date_str="2026-10-29",
        db_path=tmp_db,
        dry_run=True,
    )
    assert out_xlsx.exists()
    assert out_xlsx.parent.resolve() == (fake_base / "scratch").resolve()
    assert out_xlsx.name.startswith("DRYRUN_SL01_R_Bins_Lag_Report_")
    assert canonical_latest.read_bytes() == b"CANONICAL_05_LATEST"


def test_storage_retention_purges_old_dryrun_and_preserves_reconstructed(tmp_path, monkeypatch):
    ret_mod = _load_module("dept_ret_dryrun_test", DEPT_DIR / "storage_retention.py")
    s_dir = tmp_path / "scratch"
    s_dir.mkdir(parents=True)
    monkeypatch.setattr(ret_mod, "SCRATCH_FOLDERS", [s_dir])

    old_dryrun = s_dir / "DRYRUN_WMS_Cycle_Count_Audit_List_20261025_120000.xlsx"
    old_reconstructed = s_dir / "RECONSTRUCTED_WMS_Cycle_Count_Audit_List_20261028_0659.xlsx"
    old_dryrun.write_bytes(b"OLD_DRY")
    old_reconstructed.write_bytes(b"KEEP_RECON")

    old_ts = time.time() - (48 * 3600)
    os.utime(old_dryrun, (old_ts, old_ts))
    os.utime(old_reconstructed, (old_ts, old_ts))

    res = ret_mod.prune_scratch_artifacts(max_age_hours=24, dry_run=False)
    assert res["deleted"] == 1
    assert not old_dryrun.exists()
    assert old_reconstructed.exists()
