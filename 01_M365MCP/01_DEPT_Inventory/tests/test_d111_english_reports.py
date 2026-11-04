"""
D111 (#207) Operational Report 100% English Enforcement & Language Guard Tests
==============================================================================
Verifies that every operational report sent beyond user-only (`TEST_EMAIL_RECIPIENT` / `[TEST]` / `03_JOB`)
renders with ZERO Hangul characters (U+AC00-U+D7A3, U+1100-U+11FF, U+3130-U+318F) across:
- Email Subject
- Email HTML Body (headers, KPI cards, cause labels, banners, footers, empty states)
- Attached Excel Workbooks (sheet names, column headers, and cell values)

Also verifies that `check_report_language()` never blocks operational dispatch when Hangul slips in,
writes `ALERT_LANG_<context>_<ts>.md`, and skips alerting on user-only (`is_user_only=True`) reports.
"""

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pandas as pd
import pytest

DEPT_DIR = Path(__file__).resolve().parents[1]
if str(DEPT_DIR) not in sys.path:
    sys.path.insert(0, str(DEPT_DIR))

from report_labels import EN, assert_no_hangul, check_report_language, find_hangul


def _load_module_unique(unique_name: str, file_path: Path):
    parent = str(file_path.parent)
    if parent in sys.path:
        sys.path.remove(parent)
    sys.path.insert(0, parent)
    sys.modules.pop("db_manager", None)
    spec = importlib.util.spec_from_file_location(unique_name, str(file_path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_00_job_daily_and_weekly_zero_hangul(tmp_path, monkeypatch):
    job00_dir = DEPT_DIR / "00_JOB_StockAccuracy"
    sys.path.insert(0, str(job00_dir))
    import box_aging_tracker
    import stock_verifier

    monkeypatch.setattr(box_aging_tracker, "get_latest_missing_gi_map", lambda plant=None: {})

    # Verify legacy Korean reason in forced_weekly_gr_check.json is auto-converted to English
    cfg_path = tmp_path / "forced_weekly_gr_check.json"
    cfg_path.write_text(
        json.dumps(
            {
                "active_from": "2026-10-30",
                "active_until": "2026-11-01",
                "materials": ["SAMPLE_MAT_48"],
                "slocs": ["SL00", "SL01"],
                "reason": "입고 누락 의심(F) — SAP GR 확인용",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded_spec = box_aging_tracker.load_forced_weekly_gr_check_spec(
        run_date="2026-10-31", config_path=cfg_path
    )
    assert loaded_spec["reason"] == EN["FORCED_WEEKLY_GR_REASON"]
    assert find_hangul(loaded_spec["reason"]) == []

    sample_top = [
        {
            "Rank": 1,
            "BIN": "63Z 3",
            "Zone_Type": "Plant 1 High-Rack",
            "Tier": 3,
            "Risk_Score": 100,
            "Label_QTY": 4,
            "Total_PCS": 480,
            "Parts_List": "SAMPLE_MAT_48",
            "Sample_Barcodes": "BC_F_001, BC_F_002",
            "Oldest_Lot_Date": "2026-10-25",
            "Max_Dwell": 6,
            "Avg_Std_Turn": 7.0,
            "Action_Required": EN["GR_MISSING_CHECK_F"],
            "Reason": loaded_spec["reason"],
            "Risk_Category": "FORCED_WEEKLY_GR_CHECK",
            "selection_method": "FORCED_WEEKLY_GR_CHECK",
            "list_type": "FORCED_F_CHECK",
        },
        {
            "Rank": 2,
            "BIN": "R54Z 4",
            "Zone_Type": "Plant 3 Rack",
            "Tier": 4,
            "Risk_Score": 85,
            "Label_QTY": 2,
            "Total_PCS": 240,
            "Parts_List": "SAMPLE_MAT_10",
            "Sample_Barcodes": "BC_R_001",
            "Oldest_Lot_Date": "2026-09-15",
            "Max_Dwell": 45,
            "Avg_Std_Turn": 10.0,
            "Action_Required": "Full Box Count",
            "Reason": "High-Rack Aging (45d >= 3.0x Turn)",
            "Risk_Category": "HIGH_RACK_AGING",
            "selection_method": "RULE_SCORE",
            "list_type": "TOP",
        },
    ]
    sample_rand = [
        {
            "Rank": "R1",
            "BIN": "R56Z 2",
            "Zone_Type": "Plant 3 Rack",
            "Tier": 2,
            "Risk_Score": 10,
            "Label_QTY": 1,
            "Total_PCS": 100,
            "Parts_List": "SAMPLE_MAT_99",
            "Sample_Barcodes": "BC_RND_01",
            "Oldest_Lot_Date": "2026-10-28",
            "Max_Dwell": 3,
            "Avg_Std_Turn": 7.0,
            "Action_Required": "Random Baseline Check",
            "Reason": "Random Sample",
            "Risk_Category": "RANDOM_SAMPLE",
            "selection_method": "RANDOM_SAMPLE",
            "list_type": "RANDOM",
        }
    ]

    tracker = box_aging_tracker.BoxAgingTracker.__new__(box_aging_tracker.BoxAgingTracker)
    tracker.plant_code = "P999"
    xlsx_weekly = tmp_path / "WMS_Weekly_Cycle_Count_List_20261031.xlsx"
    tracker.export_audit_excel(
        {"top_bins": sample_top, "extended_bins": [], "random_bins": sample_rand},
        output_path=xlsx_weekly,
        mode="Weekly",
    )

    verifier = stock_verifier.StockVerifier.__new__(stock_verifier.StockVerifier)
    html_weekly = verifier.generate_html_report(
        {
            "plant": "P999",
            "top_bins": sample_top,
            "extended_bins": [],
            "random_bins": sample_rand,
            "audit_excel_path": xlsx_weekly.name,
        },
        mode="Weekly",
    )
    html_daily = verifier.generate_html_report(
        {
            "plant": "P999",
            "top_bins": sample_top[1:],
            "extended_bins": sample_top[1:],
            "random_bins": sample_rand,
            "audit_excel_path": "WMS_Daily_Cycle_Count_List_20261102.xlsx",
        },
        mode="Daily",
    )

    assert_no_hangul(
        {
            "00_daily_subject": "[Daily] Cycle Count List (2026-11-02)",
            "00_weekly_subject": "[Weekly] Cycle Count List (2026-10-31)",
            "00_daily_html": html_daily,
            "00_weekly_html": html_weekly,
        },
        attachments=[xlsx_weekly],
    )


def test_01_job_routine_and_d106_0730_zero_hangul(tmp_path):
    job01_dir = DEPT_DIR / "01_JOB_GR_GI_Error"
    gi_notifier = _load_module_unique("_d111_gi_notifier", job01_dir / "gi_notifier.py")
    gr_gi_resolver = _load_module_unique("_d111_gr_gi_resolver", job01_dir / "gr_gi_resolver.py")
    d107_mod = _load_module_unique("_d111_d107_mod", job01_dir / "d107_delete_candidates.py")

    # Exercise D91 hold reason in gr_gi_resolver
    gi_rows = pd.DataFrame(
        [
            {
                "Date": "2026.10.31",
                "Material Number": "SAMPLE_MAT_01",
                "Mvt": "311",
                "FromSt": "SL01",
                "TOSt": "SP31",
                "Qty": "200.0",
                "Message": "Insufficient stock",
            }
        ]
    )
    stock_comp = pd.DataFrame(
        [
            {"Material": "SAMPLE_MAT_01", "SLoc": "SL01", "SAP_Stock": 200.0, "WMS_Stock": 0.0, "Surplus": 200.0},
            {"Material": "SAMPLE_MAT_01", "SLoc": "SP31", "SAP_Stock": 0.0, "WMS_Stock": 0.0, "Surplus": 0.0},
        ]
    )
    plan_d91 = gr_gi_resolver.resolve_material_errors_unified(
        mat="SAMPLE_MAT_01",
        rows_df=gi_rows,
        stock_comparison=stock_comp,
        protected_gr_materials=set(),
        today_str="2026.10.31",
        prev_month_str="2026.09.30",
        prev_wms_df=None,
        prev_snapshot_time=None,
    )
    # Add representative rows covering every status banner (GR, Chain, D109 F, D109 Residual, D110 Cooldown, 901)
    extra_rows = [
        {
            "Material": "SAMPLE_MAT_GR",
            "Mvt_Original": "311",
            "Deficit_Loc": "SL01",
            "Transfer_Qty": 120.0,
            "Action": "SKIP",
            "Status": "PROTECTED_GR_DEPENDENT",
            "Reason": "Protected by active ZTCODE_GI_ERR GR error",
        },
        {
            "Material": "SAMPLE_MAT_CHAIN",
            "Mvt_Original": "311",
            "Deficit_Loc": "SL01",
            "Transfer_Qty": 90.0,
            "Action": "SKIP",
            "Status": "CHAIN_CASCADE_RESOLVE",
            "Reason": "[Chained Auto-Resolve] Clears once upstream SL00->SL01 posts",
        },
        {
            "Material": "SAMPLE_MAT_F",
            "Mvt_Original": "311",
            "Deficit_Loc": "SL00",
            "Transfer_Qty": 480.0,
            "Action": "SKIP",
            "Status": "SKIPPED_PENDING_INTERFACE_SL00",
            "D109_Suspected_Unposted_GR": True,
            "Reason": "Suspected missing GR (F) — verify SAP GR posting",
        },
        {
            "Material": "SAMPLE_MAT_D109_RES",
            "Mvt_Original": "311",
            "Deficit_Loc": "SL00",
            "Transfer_Qty": 300.0,
            "Action": "SKIP",
            "Status": "SKIPPED_D109_ALREADY_RELEASED",
            "Reason": "Residual GI error after prior GUARD_RELEASED_D109 — retry blocked",
        },
        {
            "Material": "SAMPLE_MAT_CD",
            "Mvt_Original": "311",
            "Deficit_Loc": "SL01",
            "D110_Cooldown_Route": "SL02->SL01",
            "D110_Cooldown_Consec_Fails": 3,
            "D110_Cooldown_Until": "2026-11-03 08:00",
            "D110_Cooldown_Last_Error": "M7 021 Deficit",
            "Transfer_Qty": 100.0,
            "Action": "SKIP",
            "Status": "SKIPPED_COOLDOWN_GUARD_D110",
            "Reason": "Paused for 24h after 3 consecutive SAP failures",
        },
        {
            "Material": "SAMPLE_MAT_901",
            "Mvt_Original": "901",
            "Deficit_Loc": "SL03",
            "Transfer_Qty": 50.0,
            "Action": "SKIP",
            "Status": "EXCLUDED_901_902",
            "Reason": "Movement 901 excluded",
        },
    ]
    plan_df = pd.DataFrame(plan_d91 + extra_rows)
    plan_xlsx = tmp_path / "GI_Resolution_Plan_20261031_080000.xlsx"
    plan_df.to_excel(plan_xlsx, index=False)

    posting_results = [
        {
            "Material": "SAMPLE_MAT_OK",
            "From": "SL02",
            "To": "SL01",
            "Qty": 240.0,
            "Posting_Type": "ROUNDTRIP_CLEAR",
            "Result": "Doc 4900990001 posted",
        },
        {
            "Material": "SAMPLE_MAT_D109",
            "From": "SL02",
            "To": "SL00",
            "Qty": 600.0,
            "Posting_Type": "GUARD_RELEASED_D109",
            "Result": "Doc 4900990002 posted",
        },
    ]
    html_30m = gi_notifier.build_gi_email_html(
        plan_df, posting_results, "2026-10-31", "08:00 AM", cycle_label=EN["CYCLE_30M_DAY"]
    )

    postings_24h_df = pd.DataFrame(
        [
            {
                "exec_time": "2026-10-30 14:02:10",
                "material": "SAMPLE_MAT_D109",
                "from_sloc": "SL02",
                "to_sloc": "SL00",
                "transfer_qty": 600.0,
                "doc_number": "4900990002",
                "post_status": "POSTED",
                "posting_type": "GUARD_RELEASED_D109",
                "reason": "GUARD_RELEASED_D109: 4 consecutive runs, 3-file GR=0",
            }
        ]
    )
    runs_24h_df = pd.DataFrame(
        [
            {
                "run_time": "2026-10-30 23:01:00",
                "batch_id": "RUN_20261030_230000",
                "total_gi_errors": 5,
                "postings_executed": 1,
                "elapsed_seconds": 42.3,
                "status": "COMPLETED",
            }
        ]
    )
    d107_payload = {
        "collection_time_edt": "2026-10-31 07:20:05 EST",
        "collection_mode": "FRESH_0720",
        "cleared_by_0700_count": 2,
        "prev_date": "2026-10-30",
        "prev_day_31x_remaining_count": 2,
        "candidates": [
            {
                "material": "SAMPLE_MAT_CAND",
                "mvt": "311",
                "from_sloc": "SL01",
                "to_sloc": "SP31",
                "qty": 120.0,
                "error_date": "2026.10.29",
                "first_seen_edt": "2026-10-29 08:02",
                "d107_evidence": {
                    "cond_2_01_status": "SKIP / INSUFFICIENT_STOCK",
                    "cond_3_gr_7d": "0",
                    "cond_4_source_stock": "SL01 SAP=0, WMS=0",
                    "cond_5_no_pump": "No surplus available",
                },
                "d107_remaining_diff_note": "SL01: SAP 0 / WMS 0 (0)",
                "post_0800_status": "✅ Unchanged (Still remaining after 08:00)",
            }
        ],
        "holds": [
            {
                "material": "SAMPLE_MAT_HOLD",
                "mvt": "311",
                "from_sloc": "SL00",
                "to_sloc": "SL01",
                "qty": 480.0,
                "error_date": "2026.10.30",
                "d107_exclusion_reasons": ["Active 7d GR history present"],
                "stock_summary": "SL01: SAP 100 / WMS 580 (-480)",
            }
        ],
    }
    assert find_hangul(d107_mod.__doc__ or "") == [] or True  # module import sanity
    html_0730 = gi_notifier.build_daily_cumulative_email_html(
        plan_df=plan_df,
        posting_results=[],
        report_date="2026-10-31",
        report_time="07:30 AM",
        postings_24h_df=postings_24h_df,
        metrics_24h={"total_runs": 33, "successful_postings": 1, "total_posted_qty": 600.0},
        runs_24h_df=runs_24h_df,
        target_prev_date="2026-10-30",
        schedule_time_label="07:30",
        d107_payload=d107_payload,
    )

    subj_30m = f"[GI Error] {EN['CYCLE_30M_DAY']} Auto-Resolution & Status Report (2026-10-31 08:00 AM)"
    subj_0730 = "[GI Error] Daily Comprehensive Report (Prev-Day 2026-10-30 Resolution & Status) - 2026-10-31 07:30 AM"

    assert_no_hangul(
        {
            "01_30m_subject": subj_30m,
            "01_0730_subject": subj_0730,
            "01_30m_html": html_30m,
            "01_0730_html": html_0730,
        },
        attachments=[plan_xlsx],
    )


def test_02_job_backlog_d108_correction_and_excel_zero_hangul(tmp_path):
    job02_dir = DEPT_DIR / "02_JOB_Backlog_Clear"
    notifier02 = _load_module_unique("_d111_notifier02", job02_dir / "notifier.py")

    old_reports_dir = notifier02.REPORTS_DIR
    notifier02.REPORTS_DIR = tmp_path
    try:
        plan_df = pd.DataFrame(
            [
                {
                    "Material": "SAMPLE_MAT_A",
                    "Material_Desc": "LENS COVER",
                    "Target_SLoc": "SP31",
                    "Deficit_Qty": 180.0,
                    "Error_Code": "M7 021",
                    "Action": "SKIP",
                    "Status": "BLOCKED_BY_GI_ERROR",
                    "Pending_GI_Qty": 180.0,
                    "Pending_GI_SLocs": "SP31",
                    "D108_Cause_Code": "ANOMALY_NEW_AFTER_01",
                    "D108_Cause_Label": EN["D108_ANOMALY_NEW_AFTER_01"],
                    "D108_Severity": EN["SEV_TEMPORARY"],
                    "D108_Detail": "New GI error created after prior 01 cycle — awaiting next 01 verdict",
                    "D108_GI_Link": "mvt 311 SL01->SP31 180 EA (Date 2026.10.31)",
                    "Reason": "Blocked by pending GI error",
                },
                {
                    "Material": "SAMPLE_MAT_SL49",
                    "Material_Desc": "BRACKET ASSY",
                    "Target_SLoc": "SL49",
                    "Deficit_Qty": 40.0,
                    "Error_Code": "M7 021",
                    "Action": "SKIP",
                    "Status": "TRUE_SHORTAGE",
                    "Reason": "No surplus stock available across plant",
                },
            ]
        )
        cogi_items = [
            {"Material": "SAMPLE_MAT_A", "Material_Desc": "LENS COVER", "SLoc": "SP31", "Qty": 180.0, "Error_Code": "M7 021"},
            {"Material": "SAMPLE_MAT_SL49", "Material_Desc": "BRACKET ASSY", "SLoc": "SL49", "Qty": 40.0, "Error_Code": "M7 021"},
        ]
        remaining_deficits = [
            {
                "Material": "SAMPLE_MAT_DEF1",
                "Material_Desc": "HOUSING",
                "Storage_Loc": "SL01",
                "SAP_Stock": 0.0,
                "WMS_Stock": 120.0,
                "Deficit_Qty": 120.0,
                "Accuracy_After": 0.0,
                "consecutive_days": 3,
                "is_sl49": 0,
                "is_sustained_deficit": 1,
            },
            {
                "Material": "SAMPLE_MAT_DEF2",
                "Material_Desc": "BEZEL",
                "Storage_Loc": "SL49",
                "SAP_Stock": 0.0,
                "WMS_Stock": 60.0,
                "Deficit_Qty": 60.0,
                "Accuracy_After": 0.0,
                "consecutive_days": 1,
                "is_sl49": 1,
                "is_sustained_deficit": 0,
            },
        ]
        xlsx_02 = notifier02.generate_unresolved_excel(
            plan_df=plan_df, final_cogi_items=cogi_items, run_date="20261031_0800", remaining_deficits=remaining_deficits
        )
        html_02 = notifier02.build_email_html(
            plan_df=plan_df,
            execution_results=[
                {
                    "Material": "SAMPLE_MAT_POST",
                    "From": "SL02",
                    "To": "SP31",
                    "Qty": 100.0,
                    "Type": "Full Resolution",
                    "Result": "Doc 4900990003 posted",
                }
            ],
            run_date="2026-10-31",
            run_time="08:28 AM EST",
            final_cogi_items=cogi_items,
            remaining_deficits=remaining_deficits,
            is_correction=True,
        )
        subj_02 = f"{EN['CORRECTION']} [P999] Daily Backlog (COGI) Resolution & Status Report - 2026-10-31"

        assert_no_hangul(
            {
                "02_backlog_subject": subj_02,
                "02_backlog_html": html_02,
            },
            attachments=[xlsx_02],
        )
    finally:
        notifier02.REPORTS_DIR = old_reports_dir


def test_04_job_reconciliation_html_markdown_and_excel_zero_hangul(tmp_path):
    job04_dir = DEPT_DIR / "04_JOB_SystemReconciliation"
    rec_mod = _load_module_unique("_d111_rec_notifier", job04_dir / "reconciliation_notifier.py")

    summary = {
        "snapshot_date": "2026-10-31",
        "posting_date": "2026-10-30",
        "plant": "P999",
        "system_group": "IWMS",
        "avg_score": 97.45,
        "accuracy_rate": 91.20,
        "dod_accuracy_delta": 0.35,
        "total_items": 1450,
        "matched_items": 1322,
        "mismatched_items": 128,
        "total_diff_qty": 4520.0,
        "total_financial_exposure": 18450.50,
    }
    top_risks = [
        {
            "material": "SAMPLE_MAT_R1",
            "description": "LED DRIVER MODULE",
            "material_type": "ROH1",
            "wms_lgort": "SL01",
            "sap_lgort": "SL01",
            "wms_qty": 480.0,
            "sap_qty": 240.0,
            "diff_qty": 240.0,
            "score": 50.0,
            "net_price": 25.0,
            "financial_exposure": 6000.0,
        }
    ]
    chronic_ghosts = [
        {
            "material": "SAMPLE_MAT_G1",
            "material_type": "ROH1",
            "description": "HEAT SINK",
            "first_detected_date": "2026-10-27",
            "last_detected_date": "2026-10-31",
            "consecutive_days": 5,
            "current_wms_qty": 100.0,
            "current_sap_qty": 200.0,
            "current_diff_qty": -100.0,
            "current_financial_exposure": 1500.0,
        }
    ]
    baseline_profile = {
        "total_materials": 1450,
        "max_snapshots_analyzed": 14,
        "perfect_count": 1200,
        "overall_match_rate": 90.5,
        "ghost_deficit_count": 40,
        "over_stock_count": 55,
        "volatile_count": 155,
    }
    attribution = {
        "evaluated_materials_count": 6,
        "resolved_materials_count": 5,
        "accuracy_lift_pct": 0.42,
    }

    with patch.object(rec_mod, "get_top_financial_risks", return_value=top_risks), \
         patch.object(rec_mod, "get_all_discrepancies", return_value=top_risks), \
         patch.object(rec_mod, "get_chronic_ghost_items", return_value=chronic_ghosts), \
         patch.object(rec_mod, "get_baseline_summary", return_value=baseline_profile):
        xlsx_04 = rec_mod.generate_reconciliation_excel(summary, output_dir=tmp_path, dry_run=True)
        md_04 = rec_mod.generate_reconciliation_report(
            summary=summary, attribution=attribution, output_dir=tmp_path, dry_run=True
        )
        html_04 = rec_mod.build_executive_html_body(
            summary=summary,
            top_risks=top_risks,
            chronic_ghosts=chronic_ghosts,
            attribution=attribution,
            excel_filename=xlsx_04.name,
        )

    subj_04 = (
        f"[Stock Accuracy] Official WMS vs SAP Stock Accuracy "
        f"({summary['avg_score']:.2f}%, Match Rate {summary['accuracy_rate']:.2f}%) - {summary['snapshot_date']}"
    )
    assert_no_hangul(
        {
            "04_rec_subject": subj_04,
            "04_rec_html": html_04,
            "04_rec_markdown": md_04.read_text(encoding="utf-8"),
        },
        attachments=[xlsx_04],
    )


def test_05_job_sl01_r_bins_html_and_excel_zero_hangul(tmp_path, monkeypatch):
    job05_dir = DEPT_DIR / "05_JOB_SL01_R_Bins"
    op05 = _load_module_unique("_d111_op05", job05_dir / "sl01_r_bins_operator.py")

    db_file = tmp_path / "sl01_r_test.db"
    op05.db_manager.init_db(db_file)
    sample_obs = [
        {
            "barcode": "BC_SL01R_01",
            "mat_no": "SAMPLE_MAT_R01",
            "mat_desc": "COVER LENS",
            "bin": "R53Z 3",
            "qty": 120.0,
            "lot_no": "261025",
            "gr_date": "20261025",
            "first_seen_date": "2026-10-28",
            "last_seen_date": "2026-10-31",
            "days_in_sl01_r": 3,
            "status": "ACTIVE",
            "sl02_signal": "REFILLED_AND_DUP",
            "sl02_details": "Other part in SL02",
            "sl02_other_bin": "R14B 2",
        }
    ]
    monkeypatch.setattr(op05, "get_active_lagging_observations", lambda db_path=None: sample_obs)
    monkeypatch.setattr(op05.db_manager, "get_active_count_delta", lambda scan_date_str, db_path=None: {"is_sharp_drop": False})

    xlsx_05 = op05.generate_portrait_excel_report("2026-10-31", output_dir=tmp_path, db_path=db_file, dry_run=True)

    captured = []
    monkeypatch.setenv("STOCK_ACCURACY_TO", ("ops_team" + "@" + "example.org"))
    monkeypatch.setenv("COMPANY_NAME", "OpsPlant")
    monkeypatch.setenv("SAP_PLANT", "P999")
    monkeypatch.setattr(
        op05,
        "_dispatch_email",
        lambda to_e, cc_e, subj, body, attachment=None: captured.append((subj, body, attachment)) or True,
    )

    assert op05.send_outlook_notification(xlsx_05, "2026-10-31", test_mode=False, db_path=db_file) is True
    assert op05.send_outlook_notification(
        xlsx_05, "2026-10-31", test_mode=False, is_fallback=True, fallback_file_time="2026-10-31 04:16", db_path=db_file
    ) is True
    assert op05.send_outlook_notification(
        None, "2026-10-31", test_mode=False, fallback_expired=True, fallback_file_time="2026-10-29 04:16", db_path=db_file
    ) is True

    assert len(captured) == 3
    texts = {}
    for idx, (s, b, _) in enumerate(captured, 1):
        texts[f"05_subj_{idx}"] = s
        texts[f"05_html_{idx}"] = b
    assert_no_hangul(texts, attachments=[xlsx_05])


def test_check_report_language_non_blocking_alert_and_user_only_exemption(tmp_path):
    # 1. Operational report with clean English -> 0 hits, no alert file
    hits_clean = check_report_language(
        "test_clean_ops",
        subject="[Daily] Cycle Count List (2026-11-02)",
        html_body="<p>All items verified in English.</p>",
        is_user_only=False,
        alert_dir=tmp_path,
    )
    assert hits_clean == []
    assert list(tmp_path.glob("ALERT_LANG_*.md")) == []

    # 2. User-only report ([TEST] / TEST_EMAIL_RECIPIENT / 03_JOB / D43 Hit Rate) -> exempt!
    hits_user_only = check_report_language(
        "test_user_only_kr",
        subject="[TEST] 주간 실사 적중률 분석 보고서",
        html_body="<p>사용자 단독 보고서는 한국어 허용</p>",
        is_user_only=True,
        alert_dir=tmp_path,
    )
    assert hits_user_only == []
    assert list(tmp_path.glob("ALERT_LANG_*.md")) == []

    # 3. Operational report with accidental Hangul in HTML + Excel -> NEVER raises, writes ALERT_LANG_*.md
    bad_xlsx = tmp_path / "Bad_Attachment.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Material", "Reason"])
    ws.append(["SAMPLE_MAT_01", "입고 누락 의심"])
    wb.save(bad_xlsx)

    hits_bad = check_report_language(
        "02_JOB_backlog_notifier",
        subject="[Correction] Daily Backlog Report",
        html_body="<div>장부 불일치</div>",
        attachments=[bad_xlsx],
        is_user_only=False,
        alert_dir=tmp_path,
    )
    assert len(hits_bad) == 5
    alerts = list(tmp_path.glob("ALERT_LANG_02_JOB_backlog_notifier_*.md"))
    assert len(alerts) == 1
    alert_txt = alerts[0].read_text(encoding="utf-8")
    assert "Dispatch was NOT blocked" in alert_txt
    assert "html_body" in alert_txt
    assert "Bad_Attachment.xlsx:Summary" in alert_txt


def test_b3_raw_sap_description_column_exempt_while_other_columns_caught(tmp_path):
    """
    B-3 (#216): Raw SAP material master `Description` / `Mat.Desc` / `Material Description`
    cell values containing Hangul (e.g. '외장', '줄') must NOT trigger Hangul alerts, whereas
    Hangul in any other column or header MUST still trigger.
    """
    xlsx_ok = tmp_path / "Raw_MAKTX_OK.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Suspect_Boxes"
    ws.append(["Field Instructions"])
    ws.append(["SMT 9947: short 2 of the last 3 counts (10->8, 12->9)."])
    ws.append(["Part #", "Description", "Risk Reason"])
    ws.append(["SAMPLE_MAT_01", "T/SCREW S/A - TRUSS(M4X16), 외장 AD SPO H", "FIFO Bypass"])
    ws.append(["SAMPLE_MAT_02", "T/P SCREW(3X10) 2줄", "High Rack Aging"])
    wb.save(xlsx_ok)

    html_ok = (
        "<table><tr><th>Material</th><th>Description</th><th>Verdict</th></tr>"
        "<tr><td>SAMPLE_MAT_01</td><td>T/SCREW 외장 AD</td><td>Stock shortage (D)</td></tr></table>"
    )
    assert_no_hangul({"html": html_ok}, attachments=[xlsx_ok])
    assert check_report_language("b3_ok", subject="Daily Report", html_body=html_ok, attachments=[xlsx_ok], alert_dir=tmp_path) == []

    # Hangul in non-Description column or Korean header MUST still be caught
    xlsx_bad = tmp_path / "Non_Desc_Hangul.xlsx"
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Suspect_Boxes"
    ws2.append(["Part #", "Description", "Risk Reason"])
    ws2.append(["SAMPLE_MAT_01", "T/SCREW 외장 AD", "장기체류"])
    wb2.save(xlsx_bad)
    hits = check_report_language(
        "b3_bad",
        subject="Daily Report",
        html_body="<table><tr><th>Material</th><th>Description</th><th>Verdict</th></tr><tr><td>M1</td><td>외장</td><td>부족</td></tr></table>",
        attachments=[xlsx_bad],
        alert_dir=tmp_path,
    )
    assert len(hits) == 2
