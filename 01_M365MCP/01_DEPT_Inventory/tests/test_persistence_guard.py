"""
Unit Tests for Persistence Guard (00_HQ_TaskManager / persistence_guard.py)
===========================================================================
Tests User Decisions D37 / 028-2:
(a) 99000021-V1 reproduction: source warehouse surplus was not sustained -> REJECT
(b) SL00 inbound pending GR reproduction: destination deficit was not sustained -> REJECT
(c) Sustained difference across 2 consecutive snapshots -> ALLOW min(Δ_now, Δ_prev)
(d) External lines (SP31) bypass warehouse surplus rule
(e) Missing / stale previous snapshot blocks warehouse surplus
"""

import sys
import pytest
from pathlib import Path
from datetime import datetime

HQ_DIR = Path(__file__).resolve().parent.parent.parent / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from persistence_guard import (
    check_transfer_persistence,
    PersistenceResult,
    WAREHOUSE_SLOCS,
    EXTERNAL_SLOCS
)


def test_99000021_v1_reproduction_rejected():
    """
    (a) 99000021-V1 reproduction:
    In current snapshot, SL01 has SAP=144, WMS=0 (appears to have surplus +144 due to WMS scan out).
    In previous snapshot (15~30m ago), SL01 had SAP=144, WMS=144 (difference was 0, NO surplus).
    Persistence guard MUST reject moving 144 EA out of SL01.
    """
    current_sap = {"SL01": 144.0, "SL02": 0.0, "SP31": 0.0}
    current_wms = {"SL01": 0.0, "SL02": 144.0, "SP31": 0.0}

    prev_sap = {"SL01": 144.0, "SL02": 0.0, "SP31": 0.0}
    prev_wms = {"SL01": 144.0, "SL02": 144.0, "SP31": 0.0}

    res = check_transfer_persistence(
        material="99000021-V1",
        source_sloc="SL01",
        dest_sloc="SL02",
        candidate_qty=144.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=prev_wms,
        prev_sap_by_sloc=prev_sap,
        prev_snapshot_time="2026-10-26 15:42:52",
        is_prev_snapshot_valid=True
    )

    assert res.is_persistent is False
    assert res.persistent_qty == 0.0
    assert "99000021-V1 guardrail" in res.reason or "was NOT present in previous snapshot" in res.reason


def test_sl00_gr_pending_reproduction_rejected():
    """
    (b) SL00 inbound pending GR reproduction (D37: WMS scanned first, SAP GR pending):
    In current snapshot, SL00 has SAP=0, WMS=169 (goods newly physically arrived & scanned in WMS first,
    producing transient deficit = +169 in SAP before 101/GR interface document arrives).
    In previous snapshot, SL00 had SAP=0, WMS=0 (no deficit, goods had not arrived yet).
    Because the deficit is transient and pending SAP 101/GR, persistence guard MUST reject transfer into SL00.
    """
    current_sap = {"SP31": 500.0, "SL00": 0.0}
    current_wms = {"SP31": 0.0, "SL00": 169.0}

    prev_sap = {"SP31": 500.0, "SL00": 0.0}
    prev_wms = {"SP31": 0.0, "SL00": 0.0}

    res = check_transfer_persistence(
        material="99000034",
        source_sloc="SP31",
        dest_sloc="SL00",
        candidate_qty=169.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=prev_wms,
        prev_sap_by_sloc=prev_sap,
        prev_snapshot_time="2026-10-26 15:42:52",
        is_prev_snapshot_valid=True
    )

    assert res.is_persistent is False
    assert res.persistent_qty == 0.0
    assert "pending GR/interface guardrail" in res.reason or "was NOT present in previous snapshot" in res.reason


def test_sustained_surplus_and_deficit_allows_min_delta():
    """
    (c) Sustained difference across both snapshots:
    Current:
      SL03 SAP=100, WMS=50 (surplus = +50)
      SL01 SAP=0, WMS=35 (deficit = +35)
    Previous:
      SL03 SAP=100, WMS=60 (surplus = +40)
      SL01 SAP=0, WMS=40 (deficit = +40)
    Result:
      persistent_source_surplus = min(50, 40) = 40.0
      persistent_dest_deficit = min(35, 40) = 35.0
      allowable persistent_qty = min(50_req, 40, 35) = 35.0 EA!
    """
    current_sap = {"SL03": 100.0, "SL01": 0.0}
    current_wms = {"SL03": 50.0, "SL01": 35.0}

    prev_sap = {"SL03": 100.0, "SL01": 0.0}
    prev_wms = {"SL03": 60.0, "SL01": 40.0}

    res = check_transfer_persistence(
        material="99000026",
        source_sloc="SL03",
        dest_sloc="SL01",
        candidate_qty=50.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=prev_wms,
        prev_sap_by_sloc=prev_sap,
        prev_snapshot_time="2026-10-26 15:42:52",
        is_prev_snapshot_valid=True
    )

    assert res.is_persistent is True
    assert res.persistent_qty == 35.0
    assert res.source_persistent_surplus == 40.0
    assert res.dest_persistent_deficit == 35.0


def test_sp31_external_source_bypasses_warehouse_surplus_rule():
    """
    External line locations (SP31, SP11) are not warehouse inventory balance locations.
    Available SAP stock is dispatched without warehouse surplus persistence requirement.
    """
    current_sap = {"SP31": 200.0, "SL01": 0.0}
    current_wms = {"SP31": 0.0, "SL01": 50.0}

    prev_sap = {"SP31": 200.0, "SL01": 0.0}
    prev_wms = {"SP31": 0.0, "SL01": 50.0}

    res = check_transfer_persistence(
        material="99000026",
        source_sloc="SP31",
        dest_sloc="SL01",
        candidate_qty=50.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=prev_wms,
        prev_sap_by_sloc=prev_sap,
        prev_snapshot_time="2026-10-26 15:42:52",
        is_prev_snapshot_valid=True
    )

    assert res.is_persistent is True
    assert res.persistent_qty == 50.0
    assert res.source_persistent_surplus == 200.0


def test_missing_or_stale_previous_snapshot_blocks_warehouse_surplus():
    """
    If no valid previous snapshot is available (first run or stale > 60m),
    warehouse surplus transfers are blocked to prevent moving against unverified transient stock.
    """
    current_sap = {"SL03": 100.0, "SL01": 0.0}
    current_wms = {"SL03": 50.0, "SL01": 35.0}

    res = check_transfer_persistence(
        material="99000026",
        source_sloc="SL03",
        dest_sloc="SL01",
        candidate_qty=50.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=None,
        prev_sap_by_sloc=None,
        prev_snapshot_time=None,
        is_prev_snapshot_valid=False
    )

    assert res.is_persistent is False
    assert res.persistent_qty == 0.0
    assert "No valid previous snapshot available" in res.reason


def test_recent_interval_less_than_15m_excluded_as_previous_snapshot(tmp_path):
    """
    (f) If candidate snapshot is within 2~5 minutes (e.g. 120s ago, < 15m),
    load_previous_snapshot_pair MUST exclude it to prevent comparing two snapshots that both
    suffer from the same un-interfaced backlog (e.g. both showing false surplus 144 on 99000021-V1).
    """
    import os
    import time
    from persistence_guard import load_previous_snapshot_pair
    wms_dir = tmp_path / "wms"
    mb52_dir = tmp_path / "mb52"
    wms_dir.mkdir()
    mb52_dir.mkdir()

    now = time.time()

    # Current files
    cur_wms = wms_dir / "WMS_StockList_HL_now.xlsx"
    cur_mb52 = mb52_dir / "MB52_P001_now.XLS"
    cur_wms.write_text("dummy")
    cur_mb52.write_text("dummy")
    os.utime(cur_wms, (now, now))
    os.utime(cur_mb52, (now, now))

    # Candidate files from 2 minutes (120s) ago
    t_2m = now - 120.0
    cand_wms = wms_dir / "WMS_StockList_HL_2m.xlsx"
    cand_mb52 = mb52_dir / "MB52_P001_2m.XLS"
    cand_wms.write_text("dummy")
    cand_mb52.write_text("dummy")
    os.utime(cand_wms, (t_2m, t_2m))
    os.utime(cand_mb52, (t_2m, t_2m))

    # Default min_interval is 15 minutes (900s)
    w_df, m_df, t_str = load_previous_snapshot_pair(
        cur_wms, cur_mb52, wms_dir=wms_dir, mb52_dir=mb52_dir
    )
    # Must be None because 2m < 15m
    assert w_df is None
    assert m_df is None
    assert t_str is None


def test_20m_interval_sustained_surplus_allowed():
    """
    (g) Sustained surplus over >= 15m interval (e.g. 20m ago):
    If previous snapshot from 20 minutes ago ALSO had surplus 144,
    and current snapshot has surplus 144, persistence is verified and allowed up to 144.
    """
    current_sap = {"SL01": 144.0, "SL02": 0.0}
    current_wms = {"SL01": 0.0, "SL02": 144.0}

    # 20 minutes ago, SL01 also had surplus 144 (sustained genuine stock imbalance)
    prev_sap = {"SL01": 144.0, "SL02": 0.0}
    prev_wms = {"SL01": 0.0, "SL02": 144.0}

    res = check_transfer_persistence(
        material="99000021-V1",
        source_sloc="SL01",
        dest_sloc="SL02",
        candidate_qty=144.0,
        current_wms_by_sloc=current_wms,
        current_sap_by_sloc=current_sap,
        prev_wms_by_sloc=prev_wms,
        prev_sap_by_sloc=prev_sap,
        prev_snapshot_time="2026-10-26 15:40:00",
        is_prev_snapshot_valid=True
    )

    assert res.is_persistent is True
    assert res.persistent_qty == 144.0
    assert res.source_persistent_surplus == 144.0


def test_load_previous_snapshot_pair_skips_unpaired_wms_to_find_valid_pair(tmp_path):
    """
    (h) Improved pair detection:
    If newest WMS candidate has no matching MB52 (e.g. manual WMS run or crashed MB52),
    load_previous_snapshot_pair iterates down to find the older WMS candidate that DOES have a valid MB52 pair.
    """
    import os
    import time
    import pandas as pd
    from persistence_guard import load_previous_snapshot_pair
    wms_dir = tmp_path / "wms"
    mb52_dir = tmp_path / "mb52"
    wms_dir.mkdir()
    mb52_dir.mkdir()

    now = time.time()
    cur_wms = wms_dir / "WMS_StockList_HL_now.xlsx"
    cur_mb52 = mb52_dir / "MB52_P001_now.XLS"
    cur_wms.write_text("dummy")
    cur_mb52.write_text("dummy")
    os.utime(cur_wms, (now, now))
    os.utime(cur_mb52, (now, now))

    # Candidate 1: 18m ago (WMS only, NO matching MB52)
    t_18m = now - 18 * 60
    orphan_wms = wms_dir / "WMS_StockList_HL_18m.xlsx"
    orphan_wms.write_text("dummy")
    os.utime(orphan_wms, (t_18m, t_18m))

    # Candidate 2: 25m ago (Both WMS and MB52 exist!)
    t_25m = now - 25 * 60
    pair_wms = wms_dir / "WMS_StockList_HL_25m.xlsx"
    pair_mb52 = mb52_dir / "MB52_P001_25m.XLS"

    df_w = pd.DataFrame([{"Mat.No": "99000021-V1", "GI Loc": "SL01", "Qty": 100}])
    df_w.to_excel(pair_wms, index=False)

    df_m = pd.DataFrame([{"Material": "99000021-V1", "SLoc": "SL01", "Unrestricted": 100}])
    df_m.to_html(pair_mb52, index=False)

    os.utime(pair_wms, (t_25m, t_25m))
    os.utime(pair_mb52, (t_25m, t_25m))

    w_df, m_df, t_str = load_previous_snapshot_pair(
        cur_wms, cur_mb52, wms_dir=wms_dir, mb52_dir=mb52_dir
    )
    assert w_df is not None
    assert m_df is not None
    assert t_str is not None
    assert len(w_df) == 1
    assert len(m_df) == 1

