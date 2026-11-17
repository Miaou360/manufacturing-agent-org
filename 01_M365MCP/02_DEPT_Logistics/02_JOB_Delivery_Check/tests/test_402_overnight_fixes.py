"""
Unit & Regression Tests for Handoff #402 (`02_JOB_Delivery_Check` Overnight Fixes)
==================================================================================
Covers:
  1. `night_no_arrival_cap_min: 240` (`20:00..05:59` departures -> 4h window capped at `05:59`),
     fixing false `TRUCK_NOT_RECEIVED` when night receiving scans a `00:00` (`02-04`) truck at
     `03:00:21` & `03:08:20`, while keeping daytime `180m` cap and genuine night `LINE_SHORTAGE_ALARM`.
  2. Physical truck arrival carrying today-planned off-schedule parts (`gr_time >= departure_ts`),
     closing `VendorDelta 20-22` as `CLOSED_RECEIVED` (`NOT_DELIVERED` + `OFF_SCHEDULE`) instead of
     false `TRUCK_NOT_RECEIVED`, while preserving `TRUCK_NOT_RECEIVED` for truly unplanned parts or
     pre-departure stragglers.
  3. `WMSChunkCrawler.download_gr_snapshot` `90s` Search/download wait + single `45s` Excel re-click
     across the `06:00` WMS rollover.
  4. `_catch_up_unclosed_prior_workday` silently healing an unclosed prior workday (`CANNOT_VERIFY -> 0`)
     from `00_JOB`'s `GR_List_YYYYMMDD_YYYYMMDD.xlsx` with zero emails sent.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pandas as pd
import pytest

JOB_DIR = Path(__file__).resolve().parents[1]
DEPT_DIR = JOB_DIR.parent
MCP_ROOT = DEPT_DIR.parent
HQ_DIR = MCP_ROOT / "00_HQ_TaskManager"
INV_DEPT_DIR = MCP_ROOT / "01_DEPT_Inventory"
STOCK_ACC_DIR = INV_DEPT_DIR / "00_JOB_StockAccuracy"
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"

for _p in (str(JOB_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DEPT_DIR), str(STOCK_ACC_DIR), str(HQ_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delivery_db_manager import fetch_truck_runs, fetch_verdicts, get_connection, init_database  # noqa: E402
from delivery_notifier import load_delivery_config  # noqa: E402
from delivery_projection_engine import (  # noqa: E402
    evaluate_poll_cycle,
    ingest_and_attribute_barcodes,
    parse_gr_excel_barcodes,
)
from plan_loader import sync_plan_to_ledger  # noqa: E402
from run_delivery_check import _catch_up_unclosed_prior_workday  # noqa: E402
from srm_parsers import N_SLOTS  # noqa: E402


def _make_part_row(
    *,
    key: str,
    part_no: str,
    name: str,
    vendor: str,
    cls: str,
    start_sap: float,
    load_slots: Dict[int, float],
    req_slots: Dict[int, float],
    trucks: List[int],
    run_date: dt.date = dt.date(2026, 11, 16),
) -> Dict[str, Any]:
    load_36 = [float(load_slots.get(i, 0.0)) for i in range(3 * N_SLOTS)]
    req_36 = [float(req_slots.get(i, 0.0)) for i in range(3 * N_SLOTS)]
    stock_36: List[float] = []
    inv = start_sap
    for ld, rq in zip(load_36, req_36):
        inv += ld - rq
        stock_36.append(inv)
    return {
        "key": key,
        "part": part_no,
        "name": name,
        "vendor": vendor,
        "start_sap": start_sap,
        "start_wms": start_sap,
        "plan": {
            "dates": [run_date + dt.timedelta(days=i) for i in range(3)],
            "trucks": trucks if cls == "L" else trucks,
            "multi": cls != "L",
            "cls": cls,
            "srm": list(load_36),
            "add": [0.0, 0.0, 0.0],
            "req": req_36,
            "load": load_36,
            "extra": [0.0] * (3 * N_SLOTS),
            "stock": stock_36,
            "unfixed": None,
            "reason": None,
            "extra_days": {0},
        },
    }


def _write_gr_xlsx(path: Path, rows: List[Dict[str, Any]]) -> Path:
    cols = [
        "Barcode",
        "Mat.No",
        "Mat.Desc",
        "GR Loc",
        "GR Time",
        "GR Qty",
        "Unit",
        "Move.Type",
        "Vendor Name",
        "Trans.No",
        "INVOICE_NO",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=cols).to_excel(path, index=False)
    return path


def test_402_night_truck_240m_cap_catches_0300_scan_while_day_stays_180m(tmp_path: Path) -> None:
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_night.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()
    assert cfg["no_arrival_cap_min"] == 180
    assert cfg["night_no_arrival_cap_min"] == 240

    # VendorNight has daytime slot 4 (`16-18`, dep 14:00) and night slot 9 (`02-04`, dep 00:00)
    # VendorOmega has night slot 7 (`22-24`, dep 20:00) with real shortage before slot 13 (`11/17 8am`)
    rows = [
        _make_part_row(
            key="PT-DAY",
            part_no="PT-DAY",
            name="DAYTIME HOUSING LH",
            vendor="VendorNight",
            cls="C",
            start_sap=500.0,
            load_slots={4: 100.0},
            req_slots={4: 20.0},
            trucks=[4, 9],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-NIGHT",
            part_no="PT-NIGHT",
            name="MIDNIGHT HOUSING RH",
            vendor="VendorNight",
            cls="C",
            start_sap=500.0,
            load_slots={9: 196.0},
            req_slots={9: 40.0},
            trucks=[4, 9],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-SHORT-2224",
            part_no="PT-SHORT-2224",
            name="NIGHT SHORTAGE HOUSING",
            vendor="VendorOmega",
            cls="C",
            start_sap=40.0,
            load_slots={7: 84.0, 13: 84.0},
            req_slots={8: 30.0, 9: 30.0},
            trucks=[1, 7],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-DAY": 50.0, "PT-NIGHT": 28.0, "PT-SHORT-2224": 21.0},
        "suppliers": [
            {"match": "VendorNight", "trucks": [4, 9], "mail": False, "to_env": "", "cc_env": ""},
            {"match": "VendorOmega", "trucks": [1, 7], "mail": False, "to_env": "", "cc_env": ""},
        ],
        "trucks_cfg": {"VendorNight": [4, 9], "VendorOmega": [1, 7]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    tmap = {t["truck_id"]: t for t in fetch_truck_runs(conn, "2026-11-16")}
    # Daytime 14:00 -> +180m = 17:00
    assert tmap["20261116_VendorNight_16-18"]["no_arrival_deadline_ts"] == "2026-11-16 17:00:00"
    # Night 20:00 -> +240m = 00:00; Night 00:00 -> +240m = 04:00
    assert tmap["20261116_VendorOmega_22-24"]["no_arrival_deadline_ts"] == "2026-11-17 00:00:00"
    assert tmap["20261116_VendorNight_02-04"]["no_arrival_deadline_ts"] == "2026-11-17 04:00:00"

    # Scan arrives at 03:00:21 (21s past the old 03:00 deadline!)
    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_night.xlsx",
        [
            {
                "Barcode": "BN-1",
                "Mat.No": "PT-NIGHT",
                "Mat.Desc": "MIDNIGHT HOUSING RH",
                "GR Loc": "SL00",
                "GR Time": "2026-11-17 03:00:21",
                "GR Qty": 196,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorNight",
                "Trans.No": "TN1",
                "INVOICE_NO": "IN1",
            }
        ],
    )

    for ts in (
        dt.datetime(2026, 11, 17, 0, 0, 1),
        dt.datetime(2026, 11, 17, 3, 0, 1),
        dt.datetime(2026, 11, 17, 3, 10, 0),
        dt.datetime(2026, 11, 17, 3, 20, 0),
        dt.datetime(2026, 11, 17, 3, 30, 0),
    ):
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        evaluate_poll_cycle(conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])

    tmap_after = {t["truck_id"]: t for t in fetch_truck_runs(conn, "2026-11-16")}
    assert tmap_after["20261116_VendorNight_02-04"]["status"] == "CLOSED_RECEIVED"
    assert tmap_after["20261116_VendorOmega_22-24"]["status"] == "TRUCK_NOT_RECEIVED"

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    assert vmap[("20261116_VendorNight_02-04", "PT-NIGHT")]["verdict"] == "DELIVERED"
    assert vmap[("20261116_VendorOmega_22-24", "PT-SHORT-2224")]["verdict"] == "TRUCK_NOT_RECEIVED"
    assert vmap[("20261116_VendorOmega_22-24", "PT-SHORT-2224")]["line_shortage_alarm"] == 1
    conn.close()


def test_402_truck_carrying_only_off_schedule_today_parts_closes_received_not_missing(tmp_path: Path) -> None:
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_offsched_trk.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # VendorDelta: `20-22` (slot 6, dep 18:00) plans PT-2022 (168 EA); `02-04` (slot 9, dep 00:00) plans PT-0204 (96 EA).
    # VendorUnp: `20-22` (slot 6, dep 18:00) plans PT-UNP-PLAN, receives ONLY truly unplanned PT-SPARE + pre-dep straggler.
    rows = [
        _make_part_row(
            key="PT-2022",
            part_no="PT-2022",
            name="HOUSING LH 2022",
            vendor="VendorDelta",
            cls="C",
            start_sap=400.0,
            load_slots={6: 168.0},
            req_slots={6: 30.0},
            trucks=[6, 9],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-0204",
            part_no="PT-0204",
            name="HOUSING LH 0204",
            vendor="VendorDelta",
            cls="C",
            start_sap=400.0,
            load_slots={9: 96.0},
            req_slots={9: 20.0},
            trucks=[6, 9],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-UNP-PLAN",
            part_no="PT-UNP-PLAN",
            name="BRACKET PLANNED",
            vendor="VendorUnp",
            cls="C",
            start_sap=400.0,
            load_slots={6: 100.0},
            req_slots={6: 20.0},
            trucks=[4, 6, 9],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-STRAGGLER",
            part_no="PT-STRAGGLER",
            name="EARLIER STRAGGLER",
            vendor="VendorUnp",
            cls="C",
            start_sap=400.0,
            load_slots={9: 50.0},
            req_slots={9: 10.0},
            trucks=[4, 6, 9],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-2022": 21.0, "PT-0204": 16.0, "PT-UNP-PLAN": 50.0, "PT-STRAGGLER": 50.0},
        "suppliers": [
            {"match": "VendorDelta", "trucks": [6, 9], "mail": False, "to_env": "", "cc_env": ""},
            {"match": "VendorUnp", "trucks": [4, 6, 9], "mail": False, "to_env": "", "cc_env": ""},
        ],
        "trucks_cfg": {"VendorDelta": [6, 9], "VendorUnp": [4, 6, 9]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_2022.xlsx",
        [
            # Arrives AFTER 18:00 departure on VendorDelta 20-22 (planned today on 02-04!)
            {
                "Barcode": "BDEL-1",
                "Mat.No": "PT-0204",
                "Mat.Desc": "HOUSING LH 0204",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 18:13:34",
                "GR Qty": 96,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorDelta",
                "Trans.No": "TH1",
                "INVOICE_NO": "IH1",
            },
            # Pre-departure straggler (17:45 < 18:00) + truly unplanned PT-SPARE (0 planned today) on VendorUnp
            {
                "Barcode": "BUNP-STRAG",
                "Mat.No": "PT-STRAGGLER",
                "Mat.Desc": "EARLIER STRAGGLER",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 17:45:00",
                "GR Qty": 50,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorUnp",
                "Trans.No": "TU0",
                "INVOICE_NO": "IU0",
            },
            {
                "Barcode": "BUNP-SPARE",
                "Mat.No": "PT-SPARE",
                "Mat.Desc": "TRULY UNPLANNED SPARE",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 18:15:00",
                "GR Qty": 25,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorUnp",
                "Trans.No": "TU1",
                "INVOICE_NO": "IU1",
            },
        ],
    )

    # At 18:30: VendorDelta 20-22 stays POLLING_30M (does NOT prematurely close!)
    ts_1830 = dt.datetime(2026, 11, 16, 18, 30)
    bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_1830)
    attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts_1830)
    evaluate_poll_cycle(conn, run_date, cfg, ts_1830, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
    tmap_1830 = {t["truck_id"]: t for t in fetch_truck_runs(conn, "2026-11-16")}
    assert tmap_1830["20261116_VendorDelta_20-22"]["status"] == "POLLING_30M"

    # At 21:00:01 (deadline): VendorDelta 20-22 closes CLOSED_RECEIVED; VendorUnp 20-22 closes TRUCK_NOT_RECEIVED!
    ts_2100 = dt.datetime(2026, 11, 16, 21, 0, 1)
    bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_2100)
    attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts_2100)
    evaluate_poll_cycle(conn, run_date, cfg, ts_2100, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])

    tmap_2100 = {t["truck_id"]: t for t in fetch_truck_runs(conn, "2026-11-16")}
    del_2022 = tmap_2100["20261116_VendorDelta_20-22"]
    assert del_2022["status"] == "CLOSED_RECEIVED"
    assert del_2022["first_receipt_ts"] == "2026-11-16 18:13:34"
    assert del_2022["matched_barcodes_count"] == 0
    assert tmap_2100["20261116_VendorUnp_20-22"]["status"] == "TRUCK_NOT_RECEIVED"

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    assert vmap[("20261116_VendorDelta_20-22", "PT-2022")]["verdict"] == "NOT_DELIVERED"
    assert vmap[("20261116_VendorDelta_20-22", "PT-0204")]["verdict"] == "OFF_SCHEDULE"
    assert vmap[("20261116_VendorUnp_20-22", "PT-UNP-PLAN")]["verdict"] == "TRUCK_NOT_RECEIVED"
    conn.close()


def test_402_download_gr_snapshot_90s_wait_and_45s_excel_retry_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, "download_gr_snapshot"):
        pytest.skip("Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot")

    monkeypatch.setattr(hbc.time, "sleep", lambda *_: None)
    crawler = hbc.WMSChunkCrawler(headless=True)
    fake_driver = MagicMock()
    fake_driver.current_url = "https://portal.internal/WMS_WEB/Stock/GrList"

    excel_clicks = {"count": 0}

    def _exec_script(script: str, el: Any = None) -> None:
        if getattr(el, "_tag_name", "") == "excel_btn":
            excel_clicks["count"] += 1
            # First click ignored (simulating 06:00 rollover render lag); 2nd click at dl_w==45 produces file!
            if excel_clicks["count"] == 2:
                snap_tmp = next((tmp_path / "snaps").glob(".temp_gr_*"))
                _write_gr_xlsx(snap_tmp / "gr_rollover.xlsx", [])

    def _find_element(by: Any, val: str) -> Any:
        m = MagicMock()
        if "Excel" in str(val):
            m._tag_name = "excel_btn"
        return m

    fake_driver.execute_script.side_effect = _exec_script
    fake_driver.find_element.side_effect = _find_element
    fake_driver.find_elements.return_value = []
    monkeypatch.setattr(crawler, "_setup_browser_for_dir", lambda _d: setattr(crawler, "driver", fake_driver))

    res = crawler.download_gr_snapshot(
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        tmp_path / "snaps",
        sso_only=True,
        lock_timeout_s=1.0,
        lock_file=tmp_path / "rollover.lock",
    )
    assert res["status"] == "OK"
    assert excel_clicks["count"] == 2
    assert Path(res["file"]).exists()


def test_402_silent_prior_workday_catchup_heals_cannot_verify_with_zero_mails(tmp_path: Path) -> None:
    prev_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_catchup.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    rows = [
        _make_part_row(
            key="PT-PAL-CLOSE",
            part_no="PT-PAL-CLOSE",
            name="PALLET BRACKET",
            vendor="VendorPal",
            cls="L",
            start_sap=200.0,
            load_slots={0: 240.0},
            req_slots={0: 40.0},
            trucks=[],
            run_date=prev_date,
        )
    ]
    bundle = {
        "status": "OK",
        "run_date": prev_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-PAL-CLOSE": 240.0},
        "suppliers": [{"match": "VendorPal", "trucks": [], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {},
    }
    sync_plan_to_ledger(conn, prev_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))
    # Simulate 05:59 WMS_DOWNLOAD_TIMEOUT leaving Pallet truck DAY_LEVEL_OPEN + CANNOT_VERIFY
    evaluate_poll_cycle(conn, prev_date, cfg, dt.datetime(2026, 11, 17, 5, 59), gr_available=False)
    assert fetch_verdicts(conn, "2026-11-16")[0]["verdict"] == "CANNOT_VERIFY"

    # 00_JOB downloads GR_List_20261116_20261116.xlsx (>5 KB)
    gr_stack = tmp_path / "gr_daily_stack"
    gr_file = _write_gr_xlsx(
        gr_stack / "GR_List_20261116_20261116.xlsx",
        [
            {
                "Barcode": "BPAL-LATE",
                "Mat.No": "PT-PAL-CLOSE",
                "Mat.Desc": "PALLET BRACKET",
                "GR Loc": "SL00",
                "GR Time": "2026-11-17 05:25:00",
                "GR Qty": 240,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorPal",
                "Trans.No": "TPAL",
                "INVOICE_NO": "IPAL",
            }
        ]
        + [
            {
                "Barcode": f"BPAD-{i}",
                "Mat.No": f"OTHER-{i}",
                "Mat.Desc": "PADDING ROW FOR >5KB FILE CHECK",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 12:00:00",
                "GR Qty": 10,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "OtherVendor",
                "Trans.No": f"T-{i}",
                "INVOICE_NO": f"I-{i}",
            }
            for i in range(60)
        ],
    )
    assert gr_file.stat().st_size > 5000

    res = _catch_up_unclosed_prior_workday(
        conn,
        prev_date,
        cfg,
        tmp_path / "out",
        gr_stack_dir=gr_stack,
    )
    assert res is not None
    assert res["status"] == "CAUGHT_UP_SILENTLY"
    assert res["mails_sent"] == 0
    assert res["newly_closed_trucks"] == ["20261116_VendorPal_PALLET"]
    assert res["verdict_counts"].get("CANNOT_VERIFY", 0) == 0
    assert res["verdict_counts"]["DELIVERED"] == 1
    conn.close()
