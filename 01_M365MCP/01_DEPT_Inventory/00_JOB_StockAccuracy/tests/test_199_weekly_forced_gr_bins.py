"""
Unit tests for #199 Item 3:
Forced top-pinned Weekly cycle count inclusion of SL00/SL01 bins for SAP GR missing check (Cause F),
separate from the 50-bin Weekly cap, with Plant 1 floor exemption scoped strictly to FORCED_WEEKLY_GR_CHECK.
"""

import datetime
import sys
from pathlib import Path
import pandas as pd
import pytest

JOB_DIR = Path(__file__).resolve().parent.parent
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from box_aging_tracker import BoxAgingTracker, load_forced_weekly_gr_check_spec
from stock_verifier import StockVerifier


def _make_Tracker_clean() -> BoxAgingTracker:
    tracker = BoxAgingTracker.__new__(BoxAgingTracker)
    tracker.ontology = {
        **{
            f"NORM_PART_{i:03d}": {
                "turnover_profile": {"trimmed_mean_days": 10.0, "mover_class": "MEDIUM_MOVER"}
            }
            for i in range(1, 61)
        },
        **{
            f"FLOOR_PART_{j:02d}": {
                "turnover_profile": {"trimmed_mean_days": 5.0, "mover_class": "FAST_MOVER"}
            }
            for j in range(1, 21)
        },
    }
    tracker.missing_gi_map = {}
    tracker.issued_gr_map = {}
    tracker.material_activity_map = {}
    tracker.chronic_feed = {}
    tracker.chain_gap_feed = {}
    tracker.net_prices = {}
    tracker.degraded_inputs = {}
    tracker.scoring_config = {
        "scoring_version": "test_199",
        "target_counts": {
            "daily": {"top_slots": 15, "extended_slots": 25},
            "weekly": {"top_slots": 50},
        },
        "random_sampling": {"daily_slots": 3, "weekly_slots": 5},
    }
    return tracker


def _build_weekly_df() -> pd.DataFrame:
    today = datetime.date.today()
    rows = []

    # 60 normal Weekly-eligible high-rack / Plant 3 bins (aging 60 days so they score > 0)
    for i in range(1, 61):
        bin_code = f"R{i:02d}A3" if i <= 30 else f"12A {3 + (i % 3)}"
        # Ensure unique bin codes for 60 bins
        if i > 30:
            bin_code = f"{10 + (i - 30):02d}H 4"
        gr_dt = today - datetime.timedelta(days=60 + (i % 10))
        rows.append({
            "Barcode": f"BC_NORM_{i:03d}",
            "Mat.No": f"NORM_PART_{i:03d}",
            "Mat.Desc": "NORMAL HIGH RACK MODULE",
            "GI Loc": "Main parts",
            "BIN": bin_code,
            "Qty.": 100.0,
            "GR_Date": gr_dt,
            "Warehouse_Dwell_Days": (today - gr_dt).days,
            "Bin_Dwell_Days": 30,
            "Lot No": gr_dt.strftime("%Y%m%d"),
            "Lot_Date": gr_dt,
        })

    # 20 normal Plant 1 floor bins (Daily-only; must NOT enter normal Weekly Top, only Daily)
    for j in range(1, 21):
        fl_dt = today - datetime.timedelta(days=25)
        rows.append({
            "Barcode": f"BC_FLOOR_NORM_{j:02d}",
            "Mat.No": f"FLOOR_PART_{j:02d}",
            "Mat.Desc": "SCREW M4 HEX",
            "GI Loc": "Main parts",
            "BIN": f"{j:02d}B 1",
            "Qty.": 500.0,
            "GR_Date": fl_dt,
            "Warehouse_Dwell_Days": 25,
            "Bin_Dwell_Days": 10,
            "Lot No": fl_dt.strftime("%Y%m%d"),
            "Lot_Date": fl_dt,
        })

    # Forced Material A ("SAMPLE_PART_48"):
    # 1) SL00 bin ("Receipt Goods SL"), 0-dwell fresh box (would have 0 risk score normally!)
    rows.append({
        "Barcode": "BC_F48_SL00_1",
        "Mat.No": "SAMPLE_PART_48",
        "Mat.Desc": "FILM S/A LH",
        "GI Loc": "Receipt Goods SL",
        "BIN": "61Z 4",
        "Qty.": 240.0,
        "GR_Date": today,
        "Warehouse_Dwell_Days": 0,
        "Bin_Dwell_Days": 1,
        "Lot No": today.strftime("%Y%m%d"),
        "Lot_Date": today,
    })
    # 2) SL01 mixed-material bin ("63Z 3": 2 boxes of SAMPLE_PART_48 + 3 boxes of OTHER_MIX_PART)
    for k in range(2):
        rows.append({
            "Barcode": f"BC_F48_MIX_{k}",
            "Mat.No": "SAMPLE_PART_48",
            "Mat.Desc": "FILM S/A LH",
            "GI Loc": "Main parts",
            "BIN": "63Z 3",
            "Qty.": 60.0,
            "GR_Date": today - datetime.timedelta(days=3),
            "Warehouse_Dwell_Days": 3,
            "Bin_Dwell_Days": 3,
            "Lot No": today.strftime("%Y%m%d"),
            "Lot_Date": today,
        })
    for k in range(3):
        rows.append({
            "Barcode": f"BC_OTHER_MIX_{k}",
            "Mat.No": "OTHER_MIX_PART",
            "Mat.Desc": "BRACKET SUB",
            "GI Loc": "Main parts",
            "BIN": "63Z 3",
            "Qty.": 90.0,
            "GR_Date": today - datetime.timedelta(days=35),
            "Warehouse_Dwell_Days": 35,
            "Bin_Dwell_Days": 35,
            "Lot No": (today - datetime.timedelta(days=35)).strftime("%Y%m%d"),
            "Lot_Date": today - datetime.timedelta(days=35),
        })
    # 3) SL01 Plant 1 Tier-2 floor bin ("64Z 2") — normally excluded from Weekly, must be allowed ONLY for forced check
    rows.append({
        "Barcode": "BC_F48_FLOOR_1",
        "Mat.No": "SAMPLE_PART_48",
        "Mat.Desc": "FILM S/A LH",
        "GI Loc": "Main parts",
        "BIN": "64Z 2",
        "Qty.": 480.0,
        "GR_Date": today - datetime.timedelta(days=3),
        "Warehouse_Dwell_Days": 3,
        "Bin_Dwell_Days": 3,
        "Lot No": today.strftime("%Y%m%d"),
        "Lot_Date": today,
    })

    # Forced Material B ("SAMPLE_PART_49"):
    # 4) SL01 Plant 1 Tier-1 floor bin ("62Z 1")
    rows.append({
        "Barcode": "BC_F49_FLOOR_1",
        "Mat.No": "SAMPLE_PART_49",
        "Mat.Desc": "FILM S/A RH",
        "GI Loc": "SL01",
        "BIN": "62Z 1",
        "Qty.": 240.0,
        "GR_Date": today - datetime.timedelta(days=30),
        "Warehouse_Dwell_Days": 30,
        "Bin_Dwell_Days": 15,
        "Lot No": (today - datetime.timedelta(days=30)).strftime("%Y%m%d"),
        "Lot_Date": today - datetime.timedelta(days=30),
    })

    # Excluded SLocs / Excluded non-physical bins for forced materials (MUST NOT be included!)
    rows.append({
        "Barcode": "BC_F49_SL02_EXCL",
        "Mat.No": "SAMPLE_PART_49",
        "Mat.Desc": "FILM S/A RH",
        "GI Loc": "Raw material",  # SL02 -> excluded
        "BIN": "18A 4",
        "Qty.": 240.0,
        "GR_Date": today,
        "Warehouse_Dwell_Days": 0,
        "Bin_Dwell_Days": 1,
        "Lot No": today.strftime("%Y%m%d"),
        "Lot_Date": today,
    })
    rows.append({
        "Barcode": "BC_F48_EXT_TRL_EXCL",
        "Mat.No": "SAMPLE_PART_48",
        "Mat.Desc": "FILM S/A LH",
        "GI Loc": "Main parts",
        "BIN": "EXT_TRL01",  # Non-physical staging -> excluded
        "Qty.": 240.0,
        "GR_Date": today,
        "Warehouse_Dwell_Days": 0,
        "Bin_Dwell_Days": 1,
        "Lot No": today.strftime("%Y%m%d"),
        "Lot_Date": today,
    })

    return pd.DataFrame(rows)


def test_weekly_forced_gr_bins_top_pinned_and_separate_from_cap(monkeypatch):
    import box_aging_tracker
    monkeypatch.setattr(box_aging_tracker, "get_latest_missing_gi_map", lambda plant=None: {})

    tracker = _make_Tracker_clean()
    df = _build_weekly_df()
    spec = {
        "active_from": "2026-10-30",
        "active_until": "2026-11-01",
        "materials": ["SAMPLE_PART_48", "SAMPLE_PART_49"],
        "slocs": ["SL00", "SL01"],
        "action_required": "GR Missing Check (F)",
        "reason": "Suspected missing GR (F) — verify SAP GR",
    }

    res = tracker.detect_mis_scans(
        df,
        mode="Weekly",
        run_date="2026-10-31",
        forced_weekly_spec=spec,
    )

    forced = res["forced_weekly_bins"]
    top_bins = res["top_bins"]
    random_bins = res["random_bins"]

    # 4 forced physical SL00/SL01 bins: 61Z 4, 63Z 3, 64Z 2, 62Z 1 (18A 4 in SL02 & EXT_TRL01 excluded)
    assert [b["BIN"] for b in forced] == ["61Z 4", "63Z 3", "64Z 2", "62Z 1"]

    # Separate from 50-bin Weekly cap: 4 forced + 50 normal Top = 54 top_bins, plus 5 random_bins = 59 total
    assert len(top_bins) == 54
    assert len(random_bins) == 5
    all_bins = [b["BIN"] for b in top_bins] + [b["BIN"] for b in random_bins]
    assert len(all_bins) == len(set(all_bins)) == 59  # Zero duplicates!
    assert "18A 4" not in [b["BIN"] for b in forced]
    assert "18A 4" not in [b["BIN"] for b in top_bins]
    assert "EXT_TRL01" not in all_bins

    # Pinned at Rank #1..#4 with exact reason and action
    for idx in range(4):
        fb = top_bins[idx]
        assert fb["selection_method"] == "FORCED_WEEKLY_GR_CHECK"
        assert fb["list_type"] == "FORCED_F_CHECK"
        assert fb["Action_Required"] == "GR Missing Check (F)"
        assert fb["Reason"] == "Suspected missing GR (F) — verify SAP GR"

    # Mixed bin 63Z 3 must report ALL_BIN_BARCODES (2 + 3 = 5 labels, 390 pcs) per D101
    b_16s3 = next(b for b in forced if b["BIN"] == "63Z 3")
    assert b_16s3["Label_QTY"] == 5
    assert b_16s3["Total_PCS"] == 390
    assert b_16s3["Parts_List"] == "OTHER_MIX_PART, SAMPLE_PART_48"

    # Remaining 50 normal Weekly Top bins have zero Plant 1 floor bins
    for b in top_bins[4:]:
        assert b["selection_method"] == "RULE_SCORE"
        assert b["list_type"] == "TOP"

    # HTML rendering verification
    verifier = StockVerifier.__new__(StockVerifier)
    html = verifier.generate_html_report(
        {
            "plant": "TEST_PLANT",
            "top_bins": top_bins,
            "extended_bins": [],
            "random_bins": random_bins,
            "audit_excel_path": "WMS_Weekly_Cycle_Count_List_20261031.xlsx",
        },
        mode="Weekly",
    )
    assert "Forced F-Check 4 + Top 50 + Random 5" in html
    assert "GR Missing Check (F)" in html
    assert "Suspected missing GR (F) — verify SAP GR" in html


def test_weekly_forced_gr_bins_inactive_outside_window_and_daily_mode(monkeypatch):
    import box_aging_tracker
    monkeypatch.setattr(box_aging_tracker, "get_latest_missing_gi_map", lambda plant=None: {})

    tracker = _make_Tracker_clean()
    df = _build_weekly_df()
    spec = {
        "active_from": "2026-10-30",
        "active_until": "2026-11-01",
        "materials": ["SAMPLE_PART_48", "SAMPLE_PART_49"],
        "slocs": ["SL00", "SL01"],
    }

    # Next week Saturday 2026-11-07 -> expired, normal 50 + 5 = 55 bins
    res_expired = tracker.detect_mis_scans(
        df,
        mode="Weekly",
        run_date="2026-11-07",
        forced_weekly_spec=spec,
    )
    assert res_expired["forced_weekly_bins"] == []
    assert len(res_expired["top_bins"]) == 50
    assert len(res_expired["random_bins"]) == 5

    # Daily mode on 2026-10-30 -> unaffected by Weekly forced spec
    res_daily = tracker.detect_mis_scans(
        df,
        mode="Daily",
        run_date="2026-10-30",
        forced_weekly_spec=spec,
    )
    assert res_daily["forced_weekly_bins"] == []


def test_auto_discovery_from_gi_resolution_plan_d109(tmp_path):
    plan_file = tmp_path / "GI_Resolution_Plan_20261029_170214.xlsx"
    pd.DataFrame([
        {"Material": "SAMPLE_PART_48", "D109_Suspected_Unposted_GR": 1},
        {"Material": "SAMPLE_PART_49", "D109_Suspected_Unposted_GR": True},
        {"Material": "SAMPLE_PART_OTHER", "D109_Suspected_Unposted_GR": 0},
    ]).to_excel(plan_file, index=False)

    nonexistent_cfg = tmp_path / "no_such_forced.json"
    loaded = load_forced_weekly_gr_check_spec(
        run_date="2026-10-31",
        config_path=nonexistent_cfg,
        gi_resolution_logs_dir=tmp_path,
    )
    assert loaded["enabled"] is True
    assert loaded["materials"] == {"SAMPLE_PART_48", "SAMPLE_PART_49"}
    assert loaded["slocs"] == {"SL00", "SL01"}
    assert loaded["reason"] == "Suspected missing GR (F) — verify SAP GR"

    # Also verify loading directly from JSON config file
    cfg_json = tmp_path / "forced_weekly_gr_check.json"
    cfg_json.write_text(
        '{"active_from":"2026-10-30","active_until":"2026-11-01","materials":["SAMPLE_PART_001"],"slocs":["SL00","SL01"]}',
        encoding="utf-8",
    )
    loaded_json = load_forced_weekly_gr_check_spec(run_date="2026-10-30", config_path=cfg_json)
    assert loaded_json["enabled"] is True
    assert loaded_json["materials"] == {"SAMPLE_PART_001"}

