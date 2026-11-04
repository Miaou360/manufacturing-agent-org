"""
Regression Tests for #236-B & #237:
  1. WMS_BARCODE_DAILY idempotency & 3-tier snapshot selection (06:51 -> 04:16 -> earliest)
  2. SAP_STOCK_DAILY UTF-16 TSV parsing & timestamp pairing with WMS_BARCODE_DAILY
  3. storage_retention.py FOLDER_LEDGER_MAP verification (never deletes uningested files) + B-0 pause guard
  4. 05_JOB_SL01_R_Bins SL01_R_BIN_OBSERVATION_HISTORY preserves prior episode's own bin/qty/mat_no
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

DEPT_DIR = Path(__file__).resolve().parent.parent
JOB00_DIR = DEPT_DIR / "00_JOB_StockAccuracy"
JOB05_DIR = DEPT_DIR / "05_JOB_SL01_R_Bins"
for _p in (str(DEPT_DIR), str(JOB00_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from daily_snapshot_archive import (
    init_wms_daily_db,
    init_sap_daily_db,
    select_wms_daily_candidate,
    sync_all_wms_barcode_daily,
    sync_all_sap_stock_daily,
    sync_daily_snapshots,
)
from storage_retention import FOLDER_LEDGER_MAP, MANAGED_FOLDERS, prune_folder


def _write_wms_xlsx(fp: Path, rows: list[dict]) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_excel(fp, index=False)


def _write_mb52_utf16_tsv(fp: Path, body_lines: list[str]) -> None:
    fp.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "\t\tPlnt\tMTyp\tSLoc\tMaterial Number\tMaterial Description\t\t\t"
        "BUn\tUnrestricted\tTotal Value\tQual.Insp.\tTotal Value\tBlocked\tValue\tTotal Value \tVal. GR BS"
    )
    txt = "SAP MB52 Export Title\n" + header + "\n" + "\n".join(body_lines) + "\n"
    fp.write_bytes(txt.encode("utf-16"))


def test_every_managed_folder_is_mapped_in_folder_ledger_map():
    """Ensures 100% of MANAGED_FOLDERS have an explicit ledger rule in FOLDER_LEDGER_MAP."""
    base = DEPT_DIR.resolve()
    for folder in MANAGED_FOLDERS:
        rel = folder.resolve().relative_to(base).as_posix()
        assert rel in FOLDER_LEDGER_MAP, f"Missing ledger mapping for {rel}"
        assert len(FOLDER_LEDGER_MAP[rel]) >= 1


def test_wms_barcode_daily_tier_selection_upgrade_and_idempotency(tmp_path):
    daily_dir = tmp_path / "wms_daily_stack"
    parts_dir = tmp_path / "wms_parts_stack"
    wms_db = tmp_path / "db" / "wms_barcode_daily.db"

    sample_rows = [
        {"Barcode": "BC-001", "Mat.No": "MAT-100", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 25, "GR Date": "20261020", "Lot No": "L1"},
        {"Barcode": "BC-002", "Mat.No": "MAT-200", "GI Loc": "CKD", "BIN": "R51Z 3", "Qty.": 50, "GR Date": "20261021", "Lot No": "L2"},
    ]

    # Day 1 (2026-10-28): Only parts_stack 01:02 exists -> Tier 3 EARLIEST_FALLBACK
    _write_wms_xlsx(parts_dir / "WMS_StockList_TEST_20261028_010200.xlsx", sample_rows)
    _write_wms_xlsx(parts_dir / "WMS_StockList_TEST_20261028_080200.xlsx", sample_rows[:1])

    # Day 2 (2026-10-31): 04:16 in daily_stack + 00:02 in parts_stack -> Tier 2 04:16_EARLY wins over 00:02!
    _write_wms_xlsx(parts_dir / "WMS_StockList_TEST_20261031_000200.xlsx", sample_rows[:1])
    _write_wms_xlsx(daily_dir / "WMS_StockList_TEST_20261031_041612.xlsx", sample_rows)

    c1 = select_wms_daily_candidate("2026-10-28", daily_dir, parts_dir)
    assert c1 is not None and c1[2] == "EARLIEST_FALLBACK" and c1[1] == "01:02:00"

    c2 = select_wms_daily_candidate("2026-10-31", daily_dir, parts_dir)
    assert c2 is not None and c2[2] == "04:16_EARLY" and c2[1] == "04:16:12"

    res1 = sync_all_wms_barcode_daily(wms_daily_dir=daily_dir, wms_parts_dir=parts_dir, db_path=wms_db)
    assert len(res1["ingested"]) == 2

    # Re-run without changes -> skipped_existing, idempotent row count
    res2 = sync_all_wms_barcode_daily(wms_daily_dir=daily_dir, wms_parts_dir=parts_dir, db_path=wms_db)
    assert len(res2["ingested"]) == 0
    assert res2["skipped_existing"] == ["2026-10-28", "2026-10-31"]

    # Now 06:51_DAILY arrives on 2026-10-31 with 3 boxes -> automatically upgrades Tier 2 -> Tier 1 idempotently!
    upgraded_rows = sample_rows + [
        {"Barcode": "BC-003", "Mat.No": "MAT-300", "GI Loc": "DMA", "BIN": "68Z 5", "Qty.": 10, "GR Date": "20261031", "Lot No": "L3"},
    ]
    _write_wms_xlsx(daily_dir / "WMS_StockList_TEST_20261031_065105.xlsx", upgraded_rows)

    res3 = sync_all_wms_barcode_daily(wms_daily_dir=daily_dir, wms_parts_dir=parts_dir, db_path=wms_db)
    assert len(res3["ingested"]) == 1
    assert res3["ingested"][0]["selection_tier"] == "06:51_DAILY"
    assert res3["ingested"][0]["row_count"] == 3

    # Force re-ingest on 2026-10-31 -> still exact 3 rows (idempotent!)
    res4 = sync_all_wms_barcode_daily(
        wms_daily_dir=daily_dir, wms_parts_dir=parts_dir, db_path=wms_db, force_dates=["2026-10-31"]
    )
    assert res4["ingested"][0]["row_count"] == 3

    conn = init_wms_daily_db(wms_db)
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM WMS_BARCODE_DAILY WHERE snapshot_date='2026-10-31'").fetchone()[0]
        slocs = dict(conn.execute("SELECT barcode, sloc FROM WMS_BARCODE_DAILY WHERE snapshot_date='2026-10-31'").fetchall())
        assert cnt == 3
        assert slocs == {"BC-001": "SL01", "BC-002": "SL02", "BC-003": "SL49"}
    finally:
        conn.close()


def test_sap_mb52_parsing_and_wms_timestamp_pairing(tmp_path):
    daily_dir = tmp_path / "wms_daily_stack"
    parts_dir = tmp_path / "wms_parts_stack"
    mb52_dir = tmp_path / "mb52"
    wms_db = tmp_path / "db" / "wms_barcode_daily.db"
    sap_db = tmp_path / "db" / "sap_stock_daily.db"

    _write_wms_xlsx(
        daily_dir / "WMS_StockList_TEST_20261031_041610.xlsx",
        [{"Barcode": "BC-1", "Mat.No": "MAT-100", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 10, "GR Date": "20261020", "Lot No": "L1"}],
    )

    # Create two MB52 files on 2026-10-31: 04:31 (closest to 04:16 WMS!) and 07:01
    mb_lines = [
        "\t\tP001\tROH1\tSL01\tMAT-100\tDESC A\t\t\tEA\t  1,250\t100.50\t0\t0.00\t5\t0.00\t100.50\t0",
        "\t\tP001\tROH2\t\tMAT-BLANK-SLOC\tDESC B\t\t\tEA\t10-\t0.00\t0\t0.00\t0\t0.00\t0.00\t0",
        # UoM footer row (blank Plnt & Material Number) -> MUST be excluded!
        "\t\t\t\t\t\t\t\t\tEA\t1,240\t\t0\t\t\t\t\t0",
    ]
    _write_mb52_utf16_tsv(mb52_dir / "MB52_P001_20261031_043100.XLS", mb_lines)
    _write_mb52_utf16_tsv(mb52_dir / "MB52_P001_20261031_070100.XLS", mb_lines[:1])

    out = sync_daily_snapshots(
        wms_daily_dir=daily_dir,
        wms_parts_dir=parts_dir,
        mb52_dirs=(mb52_dir,),
        wms_db_path=wms_db,
        sap_db_path=sap_db,
    )
    assert out["sap"]["ingested"][0]["snapshot_time"] == "04:31:00"
    assert out["sap"]["ingested"][0]["paired_wms_time"] == "04:16:10"
    assert out["sap"]["ingested"][0]["row_count"] == 2

    conn = init_sap_daily_db(sap_db)
    try:
        rows = conn.execute(
            "SELECT mat_no, sloc, unrestricted_qty, blocked_qty FROM SAP_STOCK_DAILY ORDER BY mat_no"
        ).fetchall()
        assert rows == [
            ("MAT-100", "SL01", 1250.0, 5.0),
            ("MAT-BLANK-SLOC", "", -10.0, 0.0),
        ]
    finally:
        conn.close()


def test_storage_retention_blocks_uningested_and_deletes_verified_files(tmp_path):
    base = tmp_path / "01_DEPT_Inventory"
    wms_daily = base / "00_JOB_StockAccuracy" / "wms_daily_stack"
    wms_parts = base / "01_JOB_GR_GI_Error" / "wms_parts_stack"
    mb52_dir = base / "01_JOB_GR_GI_Error" / "mb52"
    wms_db = base / "00_JOB_StockAccuracy" / "db" / "wms_barcode_daily.db"
    sap_db = base / "00_JOB_StockAccuracy" / "db" / "sap_stock_daily.db"
    shadow_db = base / "00_JOB_StockAccuracy" / "db" / "d114_stage_shadow.db"

    # Expired (>7d) files dated 2026-10-15
    f_daily = wms_daily / "WMS_StockList_TEST_20261015_065100.xlsx"
    f_parts = wms_parts / "WMS_StockList_TEST_20261015_100200.xlsx"
    f_mb52 = mb52_dir / "MB52_P001_20261015_070100.XLS"

    _write_wms_xlsx(f_daily, [{"Barcode": "B1", "Mat.No": "M1", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 1, "GR Date": "20261010", "Lot No": "L1"}])
    _write_wms_xlsx(f_parts, [{"Barcode": "B1", "Mat.No": "M1", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 1, "GR Date": "20261010", "Lot No": "L1"}])
    _write_mb52_utf16_tsv(f_mb52, ["\t\tP001\tROH1\tSL01\tM1\tDESC\t\t\tEA\t10\t0\t0\t0\t0\t0\t0\t0"])

    # 1. Before DB ingestion -> ALL 3 MUST BE KEPT!
    r_d1 = prune_folder(wms_daily, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    r_p1 = prune_folder(wms_parts, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    r_m1 = prune_folder(mb52_dir, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    assert r_d1["deleted"] == 0 and r_d1["skipped_uningested"] == 1 and f_daily.exists()
    assert r_p1["deleted"] == 0 and r_p1["skipped_uningested"] == 1 and f_parts.exists()
    assert r_m1["deleted"] == 0 and r_m1["skipped_uningested"] == 1 and f_mb52.exists()

    # 2. Ingest daily WMS + SAP ledgers, but d114_stage_shadow watermark is still BEFORE 10:02:00
    sync_daily_snapshots(
        wms_daily_dir=wms_daily,
        wms_parts_dir=wms_parts,
        mb52_dirs=(mb52_dir,),
        wms_db_path=wms_db,
        sap_db_path=sap_db,
    )
    from d114_stage_flow_shadow import init_shadow_db
    c_sh = init_shadow_db(shadow_db)
    c_sh.execute(
        "INSERT INTO STAGE_WATERMARK (source_name, first_snapshot_ts, last_snapshot_ts, last_filename, snapshots_processed, updated_at) "
        "VALUES ('wms_parts_stack', '2026-10-15 00:00:00', '2026-10-15 09:30:00', 'earlier.xlsx', 1, '2026-10-15 09:35:00')"
    )
    c_sh.commit()
    c_sh.close()

    # B-0 pause guard keeps f_daily even though it IS in WMS_BARCODE_DAILY!
    r_pause = prune_folder(wms_daily, retention_days=7, base_dir=base, pause_wms_stocklist=True)
    assert r_pause["deleted"] == 0 and r_pause["skipped_uningested"] == 1 and f_daily.exists()

    # Unpaused: f_daily & f_mb52 are verified and deleted; f_parts is STILL kept because watermark 09:30 < 10:02!
    r_d2 = prune_folder(wms_daily, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    r_m2 = prune_folder(mb52_dir, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    r_p2 = prune_folder(wms_parts, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    assert r_d2["deleted"] == 1 and not f_daily.exists()
    assert r_m2["deleted"] == 1 and not f_mb52.exists()
    assert r_p2["deleted"] == 0 and r_p2["skipped_uningested"] == 1 and f_parts.exists()

    # 3. Advance d114 watermark to 10:02:00 -> now f_parts is also verified and cleanly deleted!
    c_sh = init_shadow_db(shadow_db)
    c_sh.execute("UPDATE STAGE_WATERMARK SET last_snapshot_ts='2026-10-15 10:02:00' WHERE source_name='wms_parts_stack'")
    c_sh.commit()
    c_sh.close()

    r_p3 = prune_folder(wms_parts, retention_days=7, base_dir=base, pause_wms_stocklist=False)
    assert r_p3["deleted"] == 1 and not f_parts.exists()


def test_05_sl01_r_bin_history_preserves_episode1_bin_and_qty(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("job05_db_manager", JOB05_DIR / "db_manager.py")
    job05_db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(job05_db)

    test_db = tmp_path / "sl01_r_test.db"
    with patch.object(job05_db, "DB_PATH", test_db):
        job05_db.init_db()
        # Episode 1: BC-REENTER seen in R51Z 3 with qty 120 on 2026-10-27
        job05_db.upsert_observations(
            "2026-10-27",
            [{"barcode": "BC-REENTER", "mat_no": "MAT-OLD", "bin": "R51Z 3", "gi_loc": "SL01", "qty": 120.0}],
        )
        # Cleared on 2026-10-28
        job05_db.upsert_observations("2026-10-28", [])
        # Episode 2: Same barcode re-enters R99Z 5 with qty 40 on 2026-10-30
        job05_db.upsert_observations(
            "2026-10-30",
            [{"barcode": "BC-REENTER", "mat_no": "MAT-OLD", "bin": "R99Z 5", "gi_loc": "SL01", "qty": 40.0}],
        )

        conn = sqlite3.connect(str(test_db))
        try:
            hist = conn.execute(
                "SELECT episode, mat_no, bin, qty, first_seen_date, cleared_date FROM SL01_R_BIN_OBSERVATION_HISTORY"
            ).fetchall()
            cur_obs = conn.execute(
                "SELECT episode, status, bin, qty FROM SL01_R_BIN_OBSERVATION WHERE barcode='BC-REENTER'"
            ).fetchone()
            assert hist == [(1, "MAT-OLD", "R51Z 3", 120.0, "2026-10-27", "2026-10-28")]
            assert cur_obs == (2, "ACTIVE", "R99Z 5", 40.0)
        finally:
            conn.close()



def test_daily_pipeline_step55_failure_isolation_and_step6_ledger_guard(tmp_path, monkeypatch):
    """
    Merge Condition #2 (#238):
    Even if STEP 5.5A (`sync_daily_snapshots`) and STEP 5.5B (`run_d114_shadow_update`) raise
    exceptions after STEP 5 email dispatch:
      1. `run_pipeline()` MUST still return `True` (pipeline does NOT fail after email dispatch),
      2. An alert file `ALERT_DAILY_ARCHIVE_*.md` is written,
      3. Execution proceeds to STEP 6 (`run_retention_pruning`), AND
      4. STEP 6's `FOLDER_LEDGER_MAP` guard protects expired (>7d) uningested `WMS_StockList_*` files from deletion!
    """
    import importlib.util
    import storage_retention as sr
    import daily_snapshot_archive as dsa

    job00_dir = DEPT_DIR / "00_JOB_StockAccuracy"
    if str(job00_dir) not in sys.path:
        sys.path.insert(0, str(job00_dir))

    spec = importlib.util.spec_from_file_location("dp_under_test", job00_dir / "daily_pipeline.py")
    dp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dp)

    # Set up sandboxed base_dir with an expired (>7d) WMS_StockList file NOT in wms_barcode_daily.db
    fake_base = tmp_path / "01_DEPT_Inventory"
    fake_wms_daily = fake_base / "00_JOB_StockAccuracy" / "wms_daily_stack"
    f_expired = fake_wms_daily / "WMS_StockList_TEST_20261015_065100.xlsx"
    _write_wms_xlsx(
        f_expired,
        [{"Barcode": "B1", "Mat.No": "M1", "GI Loc": "Main parts", "BIN": "51Z 1", "Qty.": 5, "GR Date": "20261010", "Lot No": "L1"}],
    )

    dummy_audit_xlsx = tmp_path / "Audit_Dummy.xlsx"
    dummy_audit_xlsx.write_bytes(b"dummy")
    alert_dir = tmp_path / "handoff"
    alert_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(dp, "HANDOFF_ALERT_DIR", alert_dir)
    monkeypatch.setattr(dp, "run_full_ontology_update", lambda: {"total_materials_in_ontology": 10})

    class _FakeVerifier:
        def run_full_stock_analysis(self, plant, mode, dry_run=False):
            return {
                "audit_excel_path": str(dummy_audit_xlsx),
                "source_file": f_expired.name,
                "top_bins": [{"bin": "51Z 1"}],
                "extended_bins": [],
                "random_bins": [],
                "suspects_df": pd.DataFrame(),
            }

    monkeypatch.setattr(dp, "StockVerifier", _FakeVerifier)
    monkeypatch.setattr(
        dp,
        "send_warehouse_notification",
        lambda **kwargs: {"subject": "[Daily] Cycle Count List", "to": "user_placeholder"},
    )

    import db_manager as job00_dbm
    monkeypatch.setattr(
        job00_dbm,
        "save_audit_ledger_and_features",
        lambda **kwargs: {"run_id": "20261103_DAILY", "assignments_count": 1, "features_count": 0},
    )

    # Inject crashes into BOTH STEP 5.5A and STEP 5.5B!
    def _boom_archive(*args, **kwargs):
        raise RuntimeError("Simulated STEP 5.5A archive crash")

    import d114_stage_flow_shadow as d114_mod
    def _boom_d114(*args, **kwargs):
        raise RuntimeError("Simulated STEP 5.5B shadow crash")

    monkeypatch.setattr(dsa, "sync_daily_snapshots", _boom_archive)
    monkeypatch.setattr(d114_mod, "run_d114_shadow_update", _boom_d114)

    # Route STEP 6 retention pruning to our sandboxed folder & track that STEP 6 actually ran!
    step6_calls = []
    orig_prune_folder = sr.prune_folder

    def _sandboxed_run_retention(retention_days: int = 7, dry_run: bool = False):
        res = orig_prune_folder(
            fake_wms_daily,
            retention_days=retention_days,
            dry_run=dry_run,
            base_dir=fake_base,
            pause_wms_stocklist=False,
        )
        step6_calls.append(res)
        return res

    monkeypatch.setattr(sr, "run_retention_pruning", _sandboxed_run_retention)

    ok = dp.run_pipeline(
        plant="P001",
        mode="Daily",
        skip_crawl=True,
        skip_email=False,
        is_test=True,
        dry_run=False,
    )

    assert ok is True, "Pipeline MUST succeed even if post-email STEP 5.5A/5.5B fail"
    alerts = list(alert_dir.glob("ALERT_DAILY_ARCHIVE_*.md"))
    assert len(alerts) == 1 and "Simulated STEP 5.5A archive crash" in alerts[0].read_text(encoding="utf-8")
    assert len(step6_calls) == 1, "STEP 6 retention MUST still execute after STEP 5.5 exceptions"
    assert step6_calls[0]["deleted"] == 0
    assert step6_calls[0]["skipped_uningested"] == 1
    assert f_expired.exists(), "STEP 6 ledger guard MUST NOT delete uningested expired file!"
