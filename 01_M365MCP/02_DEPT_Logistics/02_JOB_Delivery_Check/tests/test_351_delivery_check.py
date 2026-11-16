"""
Comprehensive Unit & Regression Tests for #351 (`02_JOB_Delivery_Check`)
========================================================================
Covers all requirements from handoff #351, #368, #370 §2, and #371 §3:
  1. 00_JOB `06:50` daily crawler skip & ingest regression (`371 §3.1a`).
  2. Browser mutex busy -> immediate `POLL_SKIPPED_LOCK_BUSY` (`371 §3.1b`).
  3. Login form -> `WMS_SSO_EXPIRED` with zero credentials entered + 1/day user notice (`371 §3.1c`).
  4. Per-truck `30m -> 10m x 2` quiet close & per-truck `NOT_DELIVERED` rule (`371 §3.2`).
  5. `T+90m` user-only `EARLY_WARNING_90M` & `min(next_dep+30m, dep+180m)` `TRUCK_NOT_RECEIVED` cap (`371 §3.3`).
  6. Strict separation of Hybrid `Shortage (Receipt Basis)` (`LINE_SHORTAGE_ALARM` + `BUFFER_LOW_15PCT`
     including `Prod.Plan(S)==0` lookahead from `370 §2`) vs `Runout (received only)` (`371 §3.4`).
  7. Overlapping trucks single WMS read, barcode PK dedup, `OVER`, `UNPLANNED_RECEIPT`, `CANNOT_VERIFY`,
     `00_JOB` zero-write isolation, and `0` Hangul (`D111`).
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock

import pandas as pd
import pytest
from openpyxl import load_workbook

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

from delivery_db_manager import (  # noqa: E402
    fetch_truck_runs,
    fetch_verdicts,
    get_connection,
    init_database,
)
from delivery_notifier import dispatch_poll_notifications, load_delivery_config  # noqa: E402
from delivery_projection_engine import (  # noqa: E402
    evaluate_poll_cycle,
    ingest_and_attribute_barcodes,
    parse_gr_excel_barcodes,
)
from delivery_report_generator import (  # noqa: E402
    COL_A_HEADER,
    COL_B_HEADER,
    assert_workbook_no_hangul,
    write_delivery_check_workbook,
)
from plan_loader import sync_plan_to_ledger  # noqa: E402
from selenium_lock import SeleniumSessionLock  # noqa: E402
from srm_parsers import N_SLOTS  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers for synthetic plan bundles & WMS GR Excel files
# ---------------------------------------------------------------------------
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
            "trucks": trucks if cls == "C" else None,
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
    df = pd.DataFrame(rows, columns=cols)
    df.to_excel(path, index=False)
    return path


# ---------------------------------------------------------------------------
# 1. Collector Reuse & Safety Guard Tests (371 §3.1a, b, c)
# ---------------------------------------------------------------------------
def test_0650_daily_crawler_skip_and_ingest_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """371 §3.1(a): Existing 06:50 daily path (`_set_date_and_download`) behaves identically."""
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, 'download_gr_snapshot'):
        pytest.skip('Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot')

    fake_gr_dir = tmp_path / "gr_daily_stack"
    fake_gr_dir.mkdir(parents=True)
    d = dt.date(2026, 11, 15)
    existing_file = fake_gr_dir / "GR_List_20261115_20261115.xlsx"
    existing_file.write_bytes(b"X" * 6000)

    ingest_calls: List[Path] = []
    monkeypatch.setattr(hbc, "GR_STACK_DIR", fake_gr_dir)
    monkeypatch.setattr(hbc, "ingest_gr_excel", lambda p: ingest_calls.append(p) or 42)

    crawler = hbc.WMSChunkCrawler(headless=True)
    crawler.driver = MagicMock()

    # Case 1: Already ingested -> Skip completely without touching driver or ingest_gr_excel
    monkeypatch.setattr(hbc, "is_file_ingested", lambda page_type, fname: True)
    res1 = crawler._set_date_and_download("GR", d, d)
    assert res1 == existing_file
    assert ingest_calls == []
    crawler.driver.get.assert_not_called()

    # Case 2: File on disk >5KB but not yet ingested -> Resumable DB sync called once, zero browser nav
    monkeypatch.setattr(hbc, "is_file_ingested", lambda page_type, fname: False)
    res2 = crawler._set_date_and_download("GR", d, d)
    assert res2 == existing_file
    assert ingest_calls == [existing_file]
    crawler.driver.get.assert_not_called()


def test_download_gr_snapshot_lock_busy_skips_immediately(tmp_path: Path) -> None:
    """371 §3.1(b): Busy SeleniumSessionLock skips the poll immediately without delaying production jobs."""
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, 'download_gr_snapshot'):
        pytest.skip('Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot')

    lock_path = tmp_path / "test_selenium.lock"
    other_lock = SeleniumSessionLock(lock_file=lock_path)
    assert other_lock.acquire(owner="00_JOB_StockAccuracy", timeout=1.0) is True
    try:
        crawler = hbc.WMSChunkCrawler(headless=True)
        res = crawler.download_gr_snapshot(
            dt.date(2026, 11, 16),
            dt.date(2026, 11, 16),
            tmp_path / "snaps",
            sso_only=True,
            lock_timeout_s=0.15,
            lock_file=lock_path,
        )
        assert res["status"] == "POLL_SKIPPED_LOCK_BUSY"
        assert res["file"] is None
    finally:
        other_lock.release(owner="00_JOB_StockAccuracy")


def test_download_gr_snapshot_sso_expired_zero_credentials_and_once_per_day_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """371 §3.1(c): Login form detected -> zero credentials entered, returns WMS_SSO_EXPIRED, notifies 1x/day."""
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, 'download_gr_snapshot'):
        pytest.skip('Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot')

    monkeypatch.setattr(hbc.time, "sleep", lambda *_: None)
    crawler = hbc.WMSChunkCrawler(headless=True)
    fake_driver = MagicMock()
    fake_driver.current_url = "https://portal.internal/WMS_WEB/Login"
    login_called: List[bool] = []

    def _fake_setup(_dir: Path) -> None:
        crawler.driver = fake_driver

    monkeypatch.setattr(crawler, "_setup_browser_for_dir", _fake_setup)
    monkeypatch.setattr(crawler, "_login", lambda: login_called.append(True))

    lock_path = tmp_path / "sso_selenium.lock"
    res = crawler.download_gr_snapshot(
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        tmp_path / "snaps",
        sso_only=True,
        lock_timeout_s=1.0,
        lock_file=lock_path,
    )
    assert res["status"] == "WMS_SSO_EXPIRED"
    assert res["file"] is None
    assert login_called == []
    fake_driver.find_element.assert_not_called()
    fake_driver.quit.assert_called_once()

    # Verify once-per-day user-only notice idempotency
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "tester@internal-ops.local")
    db_path = init_database(tmp_path / "dc.db")
    conn = get_connection(db_path)
    sent_calls: List[Tuple[List[str], List[str], str]] = []

    def _sender(to: List[str], cc: List[str], subj: str, body: str, att: List[Path]) -> bool:
        sent_calls.append((to, cc, subj))
        return True

    cfg = load_delivery_config()
    d1 = dispatch_poll_notifications(
        conn,
        work_date="2026-11-16",
        as_of_ts=dt.datetime(2026, 11, 16, 8, 35),
        cfg=cfg,
        trucks=[],
        verdicts=[],
        newly_closed_truck_ids=[],
        early_warnings=[],
        sso_expired_detail=res["detail"],
        send_enabled=True,
        sender=_sender,
    )
    d2 = dispatch_poll_notifications(
        conn,
        work_date="2026-11-16",
        as_of_ts=dt.datetime(2026, 11, 16, 9, 5),
        cfg=cfg,
        trucks=[],
        verdicts=[],
        newly_closed_truck_ids=[],
        early_warnings=[],
        sso_expired_detail=res["detail"],
        send_enabled=True,
        sender=_sender,
    )
    conn.close()

    assert len(d1) == 1 and d1[0]["sent"] is True
    assert d2 == []
    assert len(sent_calls) == 1
    assert sent_calls[0][0] == ["tester@internal-ops.local"]
    assert sent_calls[0][1] == []
    assert sent_calls[0][2].startswith("[TEST] ")


# ---------------------------------------------------------------------------
# 2. Truck Polling Cadence (`30m -> 10m x 2`) & Per-Truck `NOT_DELIVERED` (371 §3.2)
# ---------------------------------------------------------------------------
def test_truck_polling_30m_to_10m_two_quiet_polls_and_per_truck_not_delivered(tmp_path: Path) -> None:
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # VendorAlpha truck `10-12` (slot 1 -> departs 08:00) carries PT-1001 (200 EA) and PT-1002 (100 EA)
    rows = [
        _make_part_row(
            key="PT-1001",
            part_no="PT-1001",
            name="BEZEL SUB ASSY LH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=300.0,
            load_slots={1: 200.0, 4: 200.0},
            req_slots={1: 50.0, 2: 50.0, 3: 50.0, 4: 50.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-1002",
            part_no="PT-1002",
            name="HOUSING MOLDING RH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=400.0,
            load_slots={1: 100.0, 4: 100.0},
            req_slots={1: 40.0, 2: 40.0, 3: 40.0, 4: 40.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-1001": 100.0, "PT-1002": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr.xlsx",
        [
            {
                "Barcode": "BC-1001-A",
                "Mat.No": "PT-1001",
                "Mat.Desc": "BEZEL SUB ASSY LH",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 08:28:00",
                "GR Qty": 100,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorAlpha",
                "Trans.No": "T1",
                "INVOICE_NO": "INV1",
            },
            {
                "Barcode": "BC-1001-B",
                "Mat.No": "PT-1001",
                "Mat.Desc": "BEZEL SUB ASSY LH",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 08:36:00",
                "GR Qty": 100,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorAlpha",
                "Trans.No": "T2",
                "INVOICE_NO": "INV1",
            },
        ],
    )

    def _poll_at(ts: dt.datetime) -> Dict[str, Any]:
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        return evaluate_poll_cycle(
            conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"]
        )

    # Poll 1 at 08:30: 1st box of PT-1001 arrives -> RECEIVING_10M, quiet_polls = 0
    _poll_at(dt.datetime(2026, 11, 16, 8, 30))
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "RECEIVING_10M"
    assert t08["quiet_polls"] == 0

    # Poll 2 at 08:40: 2nd box of PT-1001 arrives -> still RECEIVING_10M, quiet_polls = 0
    _poll_at(dt.datetime(2026, 11, 16, 8, 40))
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "RECEIVING_10M"
    assert t08["quiet_polls"] == 0
    assert t08["received_pcs_total"] == 200.0

    # Extra manual poll 2 minutes later (08:42): <10m elapsed -> MUST NOT advance quiet_polls!
    _poll_at(dt.datetime(2026, 11, 16, 8, 42))
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["quiet_polls"] == 0

    # Poll 3 at 08:50 (+10m quiet #1): quiet_polls == 1, still RECEIVING_10M
    _poll_at(dt.datetime(2026, 11, 16, 8, 50))
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "RECEIVING_10M"
    assert t08["quiet_polls"] == 1

    # Poll 4 at 09:00 (+20m quiet #2): truck closes as CLOSED_RECEIVED!
    res_close = _poll_at(dt.datetime(2026, 11, 16, 9, 0))
    assert res_close["newly_closed_trucks"] == ["20261116_VendorAlpha_10-12"]
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "CLOSED_RECEIVED"

    verdicts = {
        (v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")
    }
    assert verdicts[("20261116_VendorAlpha_10-12", "PT-1001")]["verdict"] == "DELIVERED"
    # Per-truck closing rule: PT-1002 had 0 receipt when truck closed -> immediately NOT_DELIVERED!
    assert verdicts[("20261116_VendorAlpha_10-12", "PT-1002")]["verdict"] == "NOT_DELIVERED"
    conn.close()


# ---------------------------------------------------------------------------
# 3. Hybrid Shortage Alarm vs Buffer <15% (370 §2) vs Runout (received only) (371 §3.4)
# ---------------------------------------------------------------------------
def test_hybrid_shortage_vs_buffer_15pct_vs_runout_received_only_never_mixed(tmp_path: Path) -> None:
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # Three parts on VendorAlpha truck `10-12` (slot 1 = 08:00), next truck at `16-18` (slot 4 = 14:00):
    # 1) PT-ALARM: start=100, load[1]=200, load[4]=200, req[1..4]=90 each.
    #    Receives only 50 EA -> hybrid_stock: s1=60, s2=-30 (<0 at 10am before s4!) -> LINE_SHORTAGE_ALARM!
    # 2) PT-BUF0: tests 370 §2 `Prod.Plan(S) == 0` lookahead!
    #    start=100, load[1]=200, load[4]=200, req[1]=60, req[2]=60, req[3]=60, req[4]=0, req[5]=100!
    #    Receives 190 EA (short by 10) -> hybrid_stock: s1=230, s2=170, s3=110 (`>=0` so NO alarm,
    #    but Req_ref looks ahead past req[4]=0 to req[5]=100 -> 1.15 * 100 = 115 > 110 -> BUFFER_LOW_15PCT=1!).
    # 3) PT-RO: delivered 100% on s1 (200/200), future truck s4=300 covers afternoon req[4..6]=120 each.
    #    Hybrid stock stays >= 0 everywhere (`Enough`), while `Runout (received only)` (future orders = 0)
    #    drops < 0 at slot 5 (`11/16 4pm`). Must trigger ZERO alarms and ZERO buffer flags!
    rows = [
        _make_part_row(
            key="PT-ALARM",
            part_no="PT-ALARM",
            name="LENS INNER LH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=100.0,
            load_slots={1: 200.0, 4: 300.0},
            req_slots={1: 90.0, 2: 90.0, 3: 90.0, 4: 90.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-BUF0",
            part_no="PT-BUF0",
            name="REFLECTOR MAIN RH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=100.0,
            load_slots={1: 200.0, 4: 200.0},
            req_slots={1: 60.0, 2: 60.0, 3: 60.0, 4: 0.0, 5: 100.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-RO",
            part_no="PT-RO",
            name="SHIELD BRACKET",
            vendor="VendorAlpha",
            cls="C",
            start_sap=100.0,
            load_slots={1: 200.0, 4: 300.0},
            req_slots={1: 50.0, 2: 50.0, 3: 50.0, 4: 100.0, 5: 100.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-ALARM": 50.0, "PT-BUF0": 10.0, "PT-RO": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_mix.xlsx",
        [
            {"Barcode": "B1", "Mat.No": "PT-ALARM", "Mat.Desc": "LENS INNER LH", "GR Loc": "SL00", "GR Time": "2026-11-16 08:25:00", "GR Qty": 50, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "1", "INVOICE_NO": "I1"},
            {"Barcode": "B2", "Mat.No": "PT-BUF0", "Mat.Desc": "REFLECTOR MAIN RH", "GR Loc": "SL00", "GR Time": "2026-11-16 08:26:00", "GR Qty": 190, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "2", "INVOICE_NO": "I1"},
            {"Barcode": "B3", "Mat.No": "PT-RO", "Mat.Desc": "SHIELD BRACKET", "GR Loc": "SL00", "GR Time": "2026-11-16 08:27:00", "GR Qty": 200, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "3", "INVOICE_NO": "I1"},
        ],
    )

    for ts in (
        dt.datetime(2026, 11, 16, 8, 30),
        dt.datetime(2026, 11, 16, 8, 40),
        dt.datetime(2026, 11, 16, 8, 50),
    ):
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        eval_res = evaluate_poll_cycle(
            conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"]
        )

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    tid = "20261116_VendorAlpha_10-12"

    # PT-ALARM: hybrid goes negative at slot 2 (`11/16 10am`) -> LINE_SHORTAGE_ALARM = 1
    v_alarm = vmap[(tid, "PT-ALARM")]
    assert v_alarm["verdict"] == "SHORT"
    assert v_alarm["line_shortage_alarm"] == 1
    assert v_alarm["first_short_slot_label"] == "11/16 10am"
    assert v_alarm["shortage_receipt_basis"] == "11/16 10am"

    # PT-BUF0: hybrid stays >= 0 (`Enough`), Prod.Plan(S=4)==0 looks ahead to S=5 (100) -> 110 < 115 -> buffer_low_15pct = 1, NO alarm!
    v_buf = vmap[(tid, "PT-BUF0")]
    assert v_buf["verdict"] == "SHORT"
    assert v_buf["line_shortage_alarm"] == 0
    assert v_buf["buffer_low_15pct"] == 1
    assert v_buf["shortage_receipt_basis"] == "Enough"

    # PT-RO: delivered 100%, future truck covers afternoon -> Column A = Enough, Runout (received only) = 11/16 4pm, ZERO alarms!
    v_ro = vmap[(tid, "PT-RO")]
    assert v_ro["verdict"] == "DELIVERED"
    assert v_ro["shortage_receipt_basis"] == "Enough"
    assert v_ro["runout_received_only"] == "11/16 4pm"
    assert v_ro["line_shortage_alarm"] == 0
    assert v_ro["buffer_low_15pct"] == 0

    # Workbook check: exact headers & 0 Hangul
    wb_stamped, wb_latest = write_delivery_check_workbook(
        tmp_path / "out",
        run_date,
        dt.datetime(2026, 11, 16, 8, 50),
        eval_res["projections"],
        fetch_truck_runs(conn, "2026-11-16"),
        fetch_verdicts(conn, "2026-11-16"),
        gr_available=True,
    )
    assert wb_stamped.exists() and wb_latest.exists()
    wb = load_workbook(wb_stamped)
    assert wb["Delivery_TOP_6D"]["A5"].value == COL_A_HEADER
    assert wb["Delivery_TOP_6D"]["B5"].value == COL_B_HEADER
    assert_workbook_no_hangul(wb)
    conn.close()


# ---------------------------------------------------------------------------
# 4. Early Warning at T+90m & No-Arrival Upper Limit (`TRUCK_NOT_RECEIVED`) (371 §3.3)
# ---------------------------------------------------------------------------
def test_early_warning_90m_user_only_and_truck_not_received_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "owner@internal-ops.local")
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # Truck 1 at 08:00 (`10-12`), next truck at 14:00 (`16-18`) -> deadline = min(14:30, 11:00) = 11:00
    rows = [
        _make_part_row(
            key="PT-MISS",
            part_no="PT-MISS",
            name="GUIDE LIGHTBAR",
            vendor="VendorBeta",
            cls="C",
            start_sap=50.0,
            load_slots={1: 150.0, 4: 200.0},
            req_slots={1: 60.0, 2: 60.0, 3: 60.0, 4: 60.0},
            trucks=[1, 4],
            run_date=run_date,
        )
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-MISS": 50.0},
        "suppliers": [{"match": "VendorBeta", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorBeta": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))
    empty_gr = _write_gr_xlsx(tmp_path / "empty_gr.xlsx", [])

    sent_subjects: List[str] = []

    def _sender(to: List[str], cc: List[str], subj: str, body: str, att: List[Path]) -> bool:
        assert to == ["owner@internal-ops.local"]
        assert cc == []
        assert subj.startswith("[TEST] ")
        sent_subjects.append(subj)
        return True

    def _step(ts: dt.datetime) -> None:
        bcs = parse_gr_excel_barcodes(empty_gr, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        ev = evaluate_poll_cycle(conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
        dispatch_poll_notifications(
            conn,
            work_date="2026-11-16",
            as_of_ts=ts,
            cfg=cfg,
            trucks=fetch_truck_runs(conn, "2026-11-16"),
            verdicts=fetch_verdicts(conn, "2026-11-16"),
            newly_closed_truck_ids=ev["newly_closed_trucks"],
            early_warnings=ev["early_warnings"],
            send_enabled=True,
            sender=_sender,
        )

    # 08:30 & 09:00: <90m -> zero emails
    _step(dt.datetime(2026, 11, 16, 8, 30))
    _step(dt.datetime(2026, 11, 16, 9, 0))
    assert sent_subjects == []

    # 09:30 (T+90m): 1 early warning mail
    _step(dt.datetime(2026, 11, 16, 9, 30))
    assert len(sent_subjects) == 1
    assert "[Early Warning]" in sent_subjects[0]

    # 10:00: idempotent -> still 1 email
    _step(dt.datetime(2026, 11, 16, 10, 0))
    assert len(sent_subjects) == 1

    # 11:00 (T+180m cap): closes as TRUCK_NOT_RECEIVED -> sends summary + urgent shortage alarm
    _step(dt.datetime(2026, 11, 16, 11, 0))
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "TRUCK_NOT_RECEIVED"
    assert len(sent_subjects) == 3
    assert any("[Delivery Check]" in s for s in sent_subjects)
    assert any("[URGENT][Delivery Shortage]" in s for s in sent_subjects)
    conn.close()


# ---------------------------------------------------------------------------
# 5. Overlapping Trucks Barcode Dedup, Unplanned Receipt Isolation & Missing GR Safety
# ---------------------------------------------------------------------------
def test_unplanned_receipt_does_not_close_unarrived_truck_and_missing_gr_cannot_verify(tmp_path: Path) -> None:
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    rows = [
        _make_part_row(
            key="PT-PLAN",
            part_no="PT-PLAN",
            name="COVER CAP",
            vendor="VendorAlpha",
            cls="C",
            start_sap=200.0,
            load_slots={1: 100.0, 2: 100.0},
            req_slots={1: 50.0, 2: 50.0},
            trucks=[1, 2],
            run_date=run_date,
        )
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-PLAN": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 2], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 2]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    # Missing GR (`gr_available=False`) -> CANNOT_VERIFY, zero alarms!
    ev_missing = evaluate_poll_cycle(
        conn, run_date, cfg, dt.datetime(2026, 11, 16, 8, 30), gr_available=False
    )
    assert ev_missing["newly_closed_trucks"] == []
    v_missing = fetch_verdicts(conn, "2026-11-16")
    assert all(v["verdict"] == "CANNOT_VERIFY" and v["line_shortage_alarm"] == 0 for v in v_missing)

    # Unplanned part arrives at 08:30 while PT-PLAN has 0 barcodes -> truck MUST stay POLLING_30M (not RECEIVING_10M!)
    gr_unplanned = _write_gr_xlsx(
        tmp_path / "unplanned.xlsx",
        [
            {
                "Barcode": "BC-UNP-1",
                "Mat.No": "PT-UNPLANNED",
                "Mat.Desc": "SPARE GASKET",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 08:25:00",
                "GR Qty": 40,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorAlpha",
                "Trans.No": "U1",
                "INVOICE_NO": "IU1",
            }
        ],
    )
    bcs = parse_gr_excel_barcodes(gr_unplanned, run_date, as_of_ts=dt.datetime(2026, 11, 16, 8, 30))
    attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, dt.datetime(2026, 11, 16, 8, 30))
    evaluate_poll_cycle(
        conn,
        run_date,
        cfg,
        dt.datetime(2026, 11, 16, 8, 30),
        gr_available=True,
        new_planned_by_truck=attr["new_planned_by_truck"],
    )
    t08 = fetch_truck_runs(conn, "2026-11-16")[0]
    assert t08["status"] == "POLLING_30M"
    verdicts_by_part = {v["part_key"]: v for v in fetch_verdicts(conn, "2026-11-16") if v["truck_id"] == t08["truck_id"]}
    assert verdicts_by_part["PT-UNPLANNED"]["verdict"] == "UNPLANNED_RECEIPT"
    conn.close()


def test_overlapping_trucks_barcode_dedup_and_over_delivery(tmp_path: Path) -> None:
    """Overlapping 08:00 (`10-12`) and 10:00 (`12-14`) trucks share one GR read without double-counting barcodes."""
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_ov.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    rows = [
        _make_part_row(
            key="PT-OV",
            part_no="PT-OV",
            name="CLIP RETAINER",
            vendor="VendorAlpha",
            cls="C",
            start_sap=100.0,
            load_slots={1: 100.0, 2: 100.0},
            req_slots={1: 40.0, 2: 40.0, 3: 40.0},
            trucks=[1, 2],
            run_date=run_date,
        )
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-OV": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 2], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 2]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    # 08:00 truck delayed until 10:25 (1 box = 100 EA), 10:00 truck delivers 2 boxes (200 EA = OVER!) at 10:28
    gr_ov = _write_gr_xlsx(
        tmp_path / "ov.xlsx",
        [
            {"Barcode": "BC-OV-1", "Mat.No": "PT-OV", "Mat.Desc": "CLIP RETAINER", "GR Loc": "SL00", "GR Time": "2026-11-16 10:25:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "O1", "INVOICE_NO": "IO1"},
            {"Barcode": "BC-OV-2", "Mat.No": "PT-OV", "Mat.Desc": "CLIP RETAINER", "GR Loc": "SL00", "GR Time": "2026-11-16 10:28:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "O2", "INVOICE_NO": "IO2"},
            {"Barcode": "BC-OV-3", "Mat.No": "PT-OV", "Mat.Desc": "CLIP RETAINER", "GR Loc": "SL00", "GR Time": "2026-11-16 10:29:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "O3", "INVOICE_NO": "IO2"},
        ],
    )

    for ts in (
        dt.datetime(2026, 11, 16, 10, 30),
        dt.datetime(2026, 11, 16, 10, 40),
        dt.datetime(2026, 11, 16, 10, 50),
    ):
        bcs = parse_gr_excel_barcodes(gr_ov, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        evaluate_poll_cycle(
            conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"]
        )

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    assert vmap[("20261116_VendorAlpha_10-12", "PT-OV")]["verdict"] == "DELIVERED"
    assert vmap[("20261116_VendorAlpha_10-12", "PT-OV")]["received_pcs"] == 100.0
    assert vmap[("20261116_VendorAlpha_12-14", "PT-OV")]["verdict"] == "OVER"
    assert vmap[("20261116_VendorAlpha_12-14", "PT-OV")]["received_pcs"] == 200.0
    conn.close()


# ---------------------------------------------------------------------------
# 6. Review #373 & #374 §0 Tests: Off-Schedule Recovery, Day_Part_Summary,
#    Pallet Day-Level Rule, and Consolidated 1-Mail-Per-Truck Alarm Table
# ---------------------------------------------------------------------------
def test_part_arrives_one_truck_late_off_schedule_recovers_and_clears_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """373 §Finding 1 (1, 2, 4a): Part planned on Truck 1 (`10-12`, 08:00), arrives on Truck 2 (`16-18`, 14:00).
    Truck 1 closes NOT_DELIVERED with LINE_SHORTAGE_ALARM=1; when Truck 2 arrives with PT-LATE (+200 at 14:25),
    Truck 2 row is OFF_SCHEDULE (NOT UNPLANNED_RECEIPT!), Truck 1 row updates to `recovered later: +200 at 14:25`
    with open_shortfall_pcs == 0, hybrid stock recovers >= 0 so LINE_SHORTAGE_ALARM clears to 0, and Truck 2's
    closing mail reports the cleared alarm!
    """
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "owner@internal-ops.local")
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_late.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # PT-LATE: planned 200 on slot 1 (`10-12`, 08:00), 0 on slot 4 (`16-18`, 14:00), next planned Day 1 slot 13
    # start_sap=150, req[1]=60, req[2]=60, req[3]=60 (total 180 -> dips to -30 at slot 3 = 12pm without receipt!)
    # PT-T2-ANCHOR: planned 100 on slot 4 (`16-18`, 14:00) so Truck 2 closes normally via 30m -> 10m x 2
    rows = [
        _make_part_row(
            key="PT-LATE",
            part_no="PT-LATE",
            name="HOUSING SUB ASSY RH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=150.0,
            load_slots={1: 200.0, 13: 200.0},
            req_slots={1: 60.0, 2: 60.0, 3: 60.0, 4: 40.0, 5: 40.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-T2-ANCHOR",
            part_no="PT-T2-ANCHOR",
            name="BRACKET MOUNTING LH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=300.0,
            load_slots={4: 100.0},
            req_slots={4: 30.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-LATE": 100.0, "PT-T2-ANCHOR": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_late.xlsx",
        [
            {"Barcode": "BL-1", "Mat.No": "PT-T2-ANCHOR", "Mat.Desc": "BRACKET MOUNTING LH", "GR Loc": "SL00", "GR Time": "2026-11-16 14:24:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "L1", "INVOICE_NO": "IL1"},
            {"Barcode": "BL-2", "Mat.No": "PT-LATE", "Mat.Desc": "HOUSING SUB ASSY RH", "GR Loc": "SL00", "GR Time": "2026-11-16 14:25:00", "GR Qty": 200, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "L2", "INVOICE_NO": "IL1"},
        ],
    )

    sent_mails: List[Tuple[str, str]] = []

    def _sender(to: List[str], cc: List[str], subj: str, body: str, att: List[Path]) -> bool:
        sent_mails.append((subj, body))
        return True

    def _run_at(ts: dt.datetime) -> Dict[str, Any]:
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        ev = evaluate_poll_cycle(conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
        dispatch_poll_notifications(
            conn,
            work_date="2026-11-16",
            as_of_ts=ts,
            cfg=cfg,
            trucks=fetch_truck_runs(conn, "2026-11-16"),
            verdicts=fetch_verdicts(conn, "2026-11-16"),
            newly_closed_truck_ids=ev["newly_closed_trucks"],
            early_warnings=ev["early_warnings"],
            send_enabled=True,
            sender=_sender,
        )
        return ev

    # At 11:00 (T+180m cap), Truck 1 (`10-12`) closes TRUCK_NOT_RECEIVED with active LINE_SHORTAGE_ALARM on PT-LATE!
    _run_at(dt.datetime(2026, 11, 16, 11, 0))
    vmap_11 = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    v1_before = vmap_11[("20261116_VendorAlpha_10-12", "PT-LATE")]
    assert v1_before["verdict"] == "TRUCK_NOT_RECEIVED"
    assert v1_before["line_shortage_alarm"] == 1
    assert v1_before["initial_alarm_at_close"] == 1
    assert v1_before["open_shortfall_pcs"] == 200.0

    # Truck 2 (`16-18`) receives PT-T2-ANCHOR + PT-LATE at 14:24-14:25, closes after 2 quiet polls at 14:50
    _run_at(dt.datetime(2026, 11, 16, 14, 30))
    _run_at(dt.datetime(2026, 11, 16, 14, 40))
    ev_close2 = _run_at(dt.datetime(2026, 11, 16, 14, 50))

    vmap_15 = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    v1_after = vmap_15[("20261116_VendorAlpha_10-12", "PT-LATE")]
    v2_off = vmap_15[("20261116_VendorAlpha_16-18", "PT-LATE")]

    assert v2_off["verdict"] == "OFF_SCHEDULE"
    assert v1_after["recovered_later_pcs"] == 200.0
    assert v1_after["open_shortfall_pcs"] == 0.0
    assert v1_after["recovery_note"] == "recovered later: +200 at 14:25"
    assert v1_after["line_shortage_alarm"] == 0
    assert v1_after["initial_alarm_at_close"] == 1
    assert v1_after["shortage_receipt_basis"] == "Enough"

    # Closing mail of Truck 2 includes "Earlier Shortage Alarms Cleared by Subsequent Receipts"
    t2_summary_bodies = [b for s, b in sent_mails if "VendorAlpha truck 16-18" in s and "[Delivery Check]" in s]
    assert len(t2_summary_bodies) == 1
    assert "Earlier Shortage Alarms Cleared by Subsequent Receipts" in t2_summary_bodies[0]
    assert "recovered later: +200 at 14:25" in t2_summary_bodies[0]

    # Day_Part_Summary shows PT-LATE Planned=200, Received=200, Day GR-PO=0, Verdict=DELIVERED
    dps = {r["part_key"]: r for r in ev_close2["day_part_summary"]}
    assert dps["PT-LATE"]["planned_day_pcs"] == 200.0
    assert dps["PT-LATE"]["received_day_pcs"] == 200.0
    assert dps["PT-LATE"]["day_gr_po_pcs"] == 0.0
    assert dps["PT-LATE"]["day_verdict"] == "DELIVERED"
    conn.close()


def test_part_arrives_one_truck_early_and_split_across_two_trucks_and_truly_unplanned(tmp_path: Path) -> None:
    """373 §Finding 1 (4b, 4c, 4d):
      - PT-EARLY: planned 0 on Truck 1 (`10-12`), 150 on Truck 2 (`16-18`), arrives during Truck 1 at 08:26 ->
        Truck 1 row is OFF_SCHEDULE; when Truck 2 closes with 0, Truck 2 row has `received earlier: +150 at 08:26`
        and open_shortfall_pcs == 0; Day_Part_Summary is DELIVERED.
      - PT-SPLIT: planned 200 on Truck 1 (`10-12`), 0 on Truck 2 (`16-18`), arrives 100 at 08:25 + 100 at 14:25 ->
        Truck 1 is SHORT with `recovered later: +100 at 14:25`, open_shortfall_pcs == 0; Truck 2 is OFF_SCHEDULE;
        Day_Part_Summary has Planned=200, Received=200, Trucks Planned=1, Trucks w/ Receipts=2, DELIVERED.
      - PT-TRULY-UNP: 0 planned all day -> UNPLANNED_RECEIPT on both Truck_Scorecard and Day_Part_Summary.
    """
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_early_split.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    rows = [
        _make_part_row(
            key="PT-EARLY",
            part_no="PT-EARLY",
            name="LENS OUTER LH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=300.0,
            load_slots={4: 150.0},
            req_slots={1: 30.0, 4: 40.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-SPLIT",
            part_no="PT-SPLIT",
            name="REFLECTOR SUB RH",
            vendor="VendorAlpha",
            cls="C",
            start_sap=400.0,
            load_slots={1: 200.0},
            req_slots={1: 40.0, 2: 40.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-T2-KEEP",
            part_no="PT-T2-KEEP",
            name="SCREW GROMMET",
            vendor="VendorAlpha",
            cls="C",
            start_sap=200.0,
            load_slots={4: 50.0},
            req_slots={4: 20.0},
            trucks=[1, 4],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-EARLY": 50.0, "PT-SPLIT": 100.0, "PT-T2-KEEP": 50.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_es.xlsx",
        [
            {"Barcode": "BE-1", "Mat.No": "PT-SPLIT", "Mat.Desc": "REFLECTOR SUB RH", "GR Loc": "SL00", "GR Time": "2026-11-16 08:25:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "E1", "INVOICE_NO": "IE1"},
            {"Barcode": "BE-2", "Mat.No": "PT-EARLY", "Mat.Desc": "LENS OUTER LH", "GR Loc": "SL00", "GR Time": "2026-11-16 08:26:00", "GR Qty": 150, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "E2", "INVOICE_NO": "IE1"},
            {"Barcode": "BE-3", "Mat.No": "PT-TRULY-UNP", "Mat.Desc": "GASKET SEALER", "GR Loc": "SL00", "GR Time": "2026-11-16 08:27:00", "GR Qty": 60, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "E3", "INVOICE_NO": "IE1"},
            {"Barcode": "BE-4", "Mat.No": "PT-T2-KEEP", "Mat.Desc": "SCREW GROMMET", "GR Loc": "SL00", "GR Time": "2026-11-16 14:24:00", "GR Qty": 50, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "E4", "INVOICE_NO": "IE2"},
            {"Barcode": "BE-5", "Mat.No": "PT-SPLIT", "Mat.Desc": "REFLECTOR SUB RH", "GR Loc": "SL00", "GR Time": "2026-11-16 14:25:00", "GR Qty": 100, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorAlpha", "Trans.No": "E5", "INVOICE_NO": "IE2"},
        ],
    )

    for ts in (
        dt.datetime(2026, 11, 16, 8, 30),
        dt.datetime(2026, 11, 16, 8, 40),
        dt.datetime(2026, 11, 16, 8, 50),
        dt.datetime(2026, 11, 16, 14, 30),
        dt.datetime(2026, 11, 16, 14, 40),
        dt.datetime(2026, 11, 16, 14, 50),
    ):
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts)
        ev = evaluate_poll_cycle(conn, run_date, cfg, ts, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    t1 = "20261116_VendorAlpha_10-12"
    t2 = "20261116_VendorAlpha_16-18"

    # (b) One truck early
    assert vmap[(t1, "PT-EARLY")]["verdict"] == "OFF_SCHEDULE"
    assert vmap[(t2, "PT-EARLY")]["verdict"] == "NOT_DELIVERED"
    assert vmap[(t2, "PT-EARLY")]["open_shortfall_pcs"] == 0.0
    assert vmap[(t2, "PT-EARLY")]["recovery_note"] == "received earlier: +150 at 08:26"

    # (c) Split across two trucks
    assert vmap[(t1, "PT-SPLIT")]["verdict"] == "SHORT"
    assert vmap[(t1, "PT-SPLIT")]["open_shortfall_pcs"] == 0.0
    assert vmap[(t1, "PT-SPLIT")]["recovery_note"] == "recovered later: +100 at 14:25"
    assert vmap[(t2, "PT-SPLIT")]["verdict"] == "OFF_SCHEDULE"

    # (d) Truly unplanned part
    assert vmap[(t1, "PT-TRULY-UNP")]["verdict"] == "UNPLANNED_RECEIPT"

    dps = {r["part_key"]: r for r in ev["day_part_summary"]}
    assert dps["PT-EARLY"]["day_verdict"] == "DELIVERED"
    assert dps["PT-EARLY"]["planned_day_pcs"] == 150.0 and dps["PT-EARLY"]["received_day_pcs"] == 150.0
    assert dps["PT-SPLIT"]["day_verdict"] == "DELIVERED"
    assert dps["PT-SPLIT"]["trucks_planned"] == 1 and dps["PT-SPLIT"]["trucks_with_receipts"] == 2
    assert dps["PT-TRULY-UNP"]["day_verdict"] == "UNPLANNED_RECEIPT"

    # Verify Sheet 1 is Day_Part_Summary and workbook has 0 Hangul
    wb_stamped, _ = write_delivery_check_workbook(
        tmp_path / "out_es",
        run_date,
        dt.datetime(2026, 11, 16, 14, 50),
        ev["projections"],
        fetch_truck_runs(conn, "2026-11-16"),
        fetch_verdicts(conn, "2026-11-16"),
        day_part_summary=ev["day_part_summary"],
        gr_available=True,
    )
    wb = load_workbook(wb_stamped)
    assert wb.sheetnames == ["Day_Part_Summary", "Delivery_TOP_6D", "Truck_Scorecard", "Truck_Runs"]
    assert_workbook_no_hangul(wb)
    conn.close()


def test_pallet_truck_day_level_check_and_consolidated_multi_alarm_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """373 §Finding 3, §Finding 4 & 374 §0:
      - Pallet Truck stores `pallet_departure: "12:00"` for display (`2026-11-16 12:00:00`), stays `DAY_LEVEL_OPEN`
        all afternoon/evening with ZERO early warnings and ZERO `TRUCK_NOT_RECEIVED`, accepts late `18:15`
        booking, and closes at `2026-11-17 05:59:00` as `CLOSED_DAY_LEVEL`.
      - When a C-truck closes with 3 simultaneous `line_shortage_alarm == 1` parts, `dispatch_poll_notifications`
        sends 1 summary mail + 1 consolidated urgent alarm table mail (2 mails total for that truck, NEVER 1 per part).
    """
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "owner@internal-ops.local")
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_pallet_mail.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()
    assert cfg["pallet_departure"] == "12:00"

    rows = [
        _make_part_row(
            key="PT-PAL-1",
            part_no="PT-PAL-1",
            name="PALLET RESIN CAP",
            vendor="VendorGamma",
            cls="L",
            start_sap=500.0,
            load_slots={0: 1000.0},
            req_slots={0: 200.0, 2: 200.0, 4: 200.0},
            trucks=[1],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-ALM-1",
            part_no="PT-ALM-1",
            name="ALARM PART ONE",
            vendor="VendorGamma",
            cls="C",
            start_sap=40.0,
            load_slots={1: 200.0, 13: 200.0},
            req_slots={1: 80.0, 2: 80.0},
            trucks=[1],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-ALM-2",
            part_no="PT-ALM-2",
            name="ALARM PART TWO",
            vendor="VendorGamma",
            cls="C",
            start_sap=30.0,
            load_slots={1: 200.0, 13: 200.0},
            req_slots={1: 70.0, 2: 70.0},
            trucks=[1],
            run_date=run_date,
        ),
        _make_part_row(
            key="PT-ALM-3",
            part_no="PT-ALM-3",
            name="ALARM PART THREE",
            vendor="VendorGamma",
            cls="C",
            start_sap=20.0,
            load_slots={1: 200.0, 13: 200.0},
            req_slots={1: 60.0, 2: 60.0},
            trucks=[1],
            run_date=run_date,
        ),
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-PAL-1": 250.0, "PT-ALM-1": 100.0, "PT-ALM-2": 100.0, "PT-ALM-3": 100.0},
        "suppliers": [{"match": "VendorGamma", "trucks": [1], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorGamma": [1]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    # Pallet barcode arrives late at 18:15; C-truck `10-12` receives 0 barcodes
    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_pal.xlsx",
        [
            {"Barcode": "BP-1", "Mat.No": "PT-PAL-1", "Mat.Desc": "PALLET RESIN CAP", "GR Loc": "SL00", "GR Time": "2026-11-16 18:15:00", "GR Qty": 1000, "Unit": "EA", "Move.Type": "101", "Vendor Name": "VendorGamma", "Trans.No": "P1", "INVOICE_NO": "IP1"},
        ],
    )

    sent_mails: List[Tuple[str, str]] = []

    def _sender(to: List[str], cc: List[str], subj: str, body: str, att: List[Path]) -> bool:
        sent_mails.append((subj, body))
        return True

    # At 11:00: C-truck `10-12` hits 180m cap with 3 simultaneous alarm parts -> MUST send 1 summary + 1 alarm table!
    ts_11 = dt.datetime(2026, 11, 16, 11, 0)
    bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_11)
    attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts_11)
    ev_11 = evaluate_poll_cycle(conn, run_date, cfg, ts_11, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
    dispatch_poll_notifications(
        conn,
        work_date="2026-11-16",
        as_of_ts=ts_11,
        cfg=cfg,
        trucks=fetch_truck_runs(conn, "2026-11-16"),
        verdicts=fetch_verdicts(conn, "2026-11-16"),
        newly_closed_truck_ids=ev_11["newly_closed_trucks"],
        early_warnings=ev_11["early_warnings"],
        send_enabled=True,
        sender=_sender,
    )
    urgent_11 = [(s, b) for s, b in sent_mails if "[URGENT][Delivery Shortage]" in s]
    summary_11 = [(s, b) for s, b in sent_mails if "[Delivery Check]" in s]
    assert len(summary_11) == 1
    assert len(urgent_11) == 1
    assert all(p in urgent_11[0][1] for p in ("PT-ALM-1", "PT-ALM-2", "PT-ALM-3"))

    # At 17:30 (>5h after 12:00 pallet display departure): Pallet Truck MUST still be DAY_LEVEL_OPEN, 0 early warning!
    ts_1730 = dt.datetime(2026, 11, 16, 17, 30)
    bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_1730)
    attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts_1730)
    ev_1730 = evaluate_poll_cycle(conn, run_date, cfg, ts_1730, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
    t_pal = [t for t in fetch_truck_runs(conn, "2026-11-16") if t["is_pallet_truck"]][0]
    assert t_pal["departure_ts"] == "2026-11-16 12:00:00"
    assert t_pal["status"] == "DAY_LEVEL_OPEN"
    assert ev_1730["early_warnings"] == []

    # At 18:30 (after 18:15 pallet receipt) and 19:00: Pallet Truck STILL stays DAY_LEVEL_OPEN until 05:59 next day
    for ts_ev in (dt.datetime(2026, 11, 16, 18, 30), dt.datetime(2026, 11, 16, 19, 0)):
        bcs = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_ev)
        attr = ingest_and_attribute_barcodes(conn, run_date, bcs, cfg, ts_ev)
        evaluate_poll_cycle(conn, run_date, cfg, ts_ev, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
    t_pal_ev = [t for t in fetch_truck_runs(conn, "2026-11-16") if t["is_pallet_truck"]][0]
    assert t_pal_ev["status"] == "DAY_LEVEL_OPEN"
    assert t_pal_ev["received_pcs_total"] == 1000.0

    # At 2026-11-17 05:59:00: Pallet Truck closes cleanly as CLOSED_DAY_LEVEL with DELIVERED verdict!
    ts_eod = dt.datetime(2026, 11, 17, 5, 59)
    ev_eod = evaluate_poll_cycle(conn, run_date, cfg, ts_eod, gr_available=True)
    assert "20261116_VendorGamma_PALLET" in ev_eod["newly_closed_trucks"]
    t_pal_closed = [t for t in fetch_truck_runs(conn, "2026-11-16") if t["is_pallet_truck"]][0]
    assert t_pal_closed["status"] == "CLOSED_DAY_LEVEL"
    v_pal = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}[("20261116_VendorGamma_PALLET", "PT-PAL-1")]
    assert v_pal["verdict"] == "DELIVERED"
    conn.close()
# ---------------------------------------------------------------------------
# 9. Handoff #377 & #378 Activation Guardrails (Kill Switch, WMS 1-Try Login + Block File,
#    Secret Redaction, Departed-Truck Freeze, 07:35 Summary Non-Failure Statuses)
# ---------------------------------------------------------------------------
def test_377_kill_switch_disabled_exits_immediately_code_zero(tmp_path: Path) -> None:
    import run_delivery_check as rdc

    cfg_file = tmp_path / "dc_disabled.json"
    cfg_file.write_text(json.dumps({"enabled": False, "mode": "test"}), encoding="utf-8")
    res = rdc.run_delivery_check(
        dt.date(2026, 11, 16),
        as_of_ts=dt.datetime(2026, 11, 16, 8, 40),
        config_path=cfg_file,
        dry_run=True,
    )
    assert res["status"] == "DISABLED"


def test_378_wms_login_allowed_one_attempt_and_zero_secrets_in_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, 'download_gr_snapshot'):
        pytest.skip('Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot')

    secret_id = "WMS_SECRET_ID_771"
    secret_pw = "WMS_SECRET_PW_882"
    monkeypatch.setenv("WMS_USERNAME", secret_id)
    monkeypatch.setenv("WMS_PASSWORD", secret_pw)
    monkeypatch.setattr(hbc.time, "sleep", lambda *_: None)

    crawler = hbc.WMSChunkCrawler(headless=True)
    crawler.username = secret_id
    crawler.password = secret_pw

    state = {"logged_in": False, "login_calls": 0}
    fake_driver = MagicMock()

    def _get_url(url: str) -> None:
        if "Login" in url:
            fake_driver.current_url = url
        elif not state["logged_in"]:
            fake_driver.current_url = "https://portal.internal/WMS_WEB/Login"
        else:
            fake_driver.current_url = url

    fake_driver.get.side_effect = _get_url
    id_el = MagicMock()
    id_el.is_displayed.side_effect = lambda: not state["logged_in"]
    pw_el = MagicMock()
    btn_el = MagicMock()

    def _click_login() -> None:
        state["logged_in"] = True

    btn_el.click.side_effect = _click_login

    def _find_elements(by: Any, val: str) -> list:
        if val in ("potal_id", "btnLogin"):
            return [id_el] if not state["logged_in"] else []
        return []

    def _find_element(by: Any, val: str) -> Any:
        if val == "pwd":
            return pw_el
        if val == "btnLogin":
            return btn_el
        # Search or Excel button -> write fake downloaded xlsx on Excel click
        if "Excel" in str(val):
            snap_tmp = next((tmp_path / "snaps").glob(".temp_gr_*"))
            _write_gr_xlsx(snap_tmp / "gr_export.xlsx", [])
        return MagicMock()

    fake_driver.find_elements.side_effect = _find_elements
    fake_driver.find_element.side_effect = _find_element

    orig_login = crawler._login

    def _counted_login() -> None:
        state["login_calls"] += 1
        orig_login()

    monkeypatch.setattr(crawler, "_setup_browser_for_dir", lambda _d: setattr(crawler, "driver", fake_driver))
    monkeypatch.setattr(crawler, "_login", _counted_login)

    block_file = tmp_path / "logs" / "wms_login_blocked.json"
    res = crawler.download_gr_snapshot(
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        tmp_path / "snaps",
        sso_only=False,
        login_block_file=block_file,
        lock_timeout_s=1.0,
        lock_file=tmp_path / "wms.lock",
    )
    captured = capsys.readouterr()
    combined_out = " ".join([captured.out, captured.err, json.dumps(res, default=str)])

    assert res["status"] == "OK"
    assert res["auth_path"] == "LOGIN_PERFORMED"
    assert state["login_calls"] == 1
    assert not block_file.exists()
    assert secret_id not in combined_out
    assert secret_pw not in combined_out


def test_378_wms_login_failure_writes_block_file_stops_second_run_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import historical_backfill_crawler as hbc
    if not hasattr(hbc.WMSChunkCrawler, 'download_gr_snapshot'):
        pytest.skip('Public repo Inventory snapshot predates WMSChunkCrawler.download_gr_snapshot')

    secret_id = "WMS_FAIL_ID_551"
    secret_pw = "WMS_FAIL_PW_662"
    monkeypatch.setenv("WMS_USERNAME", secret_id)
    monkeypatch.setenv("WMS_PASSWORD", secret_pw)
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "tester@internal-ops.local")
    monkeypatch.setattr(hbc.time, "sleep", lambda *_: None)

    crawler = hbc.WMSChunkCrawler(headless=True)
    crawler.username = secret_id
    crawler.password = secret_pw

    login_calls: List[int] = []
    fake_driver = MagicMock()
    fake_driver.current_url = "https://portal.internal/WMS_WEB/Login"
    monkeypatch.setattr(crawler, "_setup_browser_for_dir", lambda _d: setattr(crawler, "driver", fake_driver))
    monkeypatch.setattr(crawler, "_login", lambda: login_calls.append(1))

    block_file = tmp_path / "logs" / "wms_login_blocked.json"
    lock_file = tmp_path / "wms_fail.lock"

    # Run 1: login form stays visible after single _login() attempt -> writes block file
    res1 = crawler.download_gr_snapshot(
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        tmp_path / "snaps",
        sso_only=False,
        login_block_file=block_file,
        lock_timeout_s=1.0,
        lock_file=lock_file,
    )
    assert res1["status"] == "WMS_LOGIN_FAILED"
    assert len(login_calls) == 1
    assert block_file.exists()
    block_text = block_file.read_text(encoding="utf-8")
    assert secret_id not in block_text
    assert secret_pw not in block_text

    # Run 2: block file exists -> MUST NOT attempt login a second time!
    res2 = crawler.download_gr_snapshot(
        dt.date(2026, 11, 16),
        dt.date(2026, 11, 16),
        tmp_path / "snaps",
        sso_only=False,
        login_block_file=block_file,
        lock_timeout_s=1.0,
        lock_file=lock_file,
    )
    assert res2["status"] == "WMS_LOGIN_BLOCKED"
    assert len(login_calls) == 1  # Zero second attempt!

    # Verify once-per-day user-only notice and zero secret leak in email or stdout
    db_path = init_database(tmp_path / "dc_block.db")
    conn = get_connection(db_path)
    sent_bodies: List[str] = []

    def _sender(to: List[str], cc: List[str], subj: str, body: str, att: List[Path]) -> bool:
        assert to == ["tester@internal-ops.local"]
        assert cc == []
        sent_bodies.append(subj + " " + body)
        return True

    cfg = load_delivery_config()
    d1 = dispatch_poll_notifications(
        conn,
        work_date="2026-11-16",
        as_of_ts=dt.datetime(2026, 11, 16, 8, 40),
        cfg=cfg,
        trucks=[],
        verdicts=[],
        newly_closed_truck_ids=[],
        early_warnings=[],
        sso_expired_detail=res1["detail"],
        send_enabled=True,
        sender=_sender,
    )
    d2 = dispatch_poll_notifications(
        conn,
        work_date="2026-11-16",
        as_of_ts=dt.datetime(2026, 11, 16, 8, 50),
        cfg=cfg,
        trucks=[],
        verdicts=[],
        newly_closed_truck_ids=[],
        early_warnings=[],
        sso_expired_detail=res2["detail"],
        send_enabled=True,
        sender=_sender,
    )
    conn.close()
    assert len(d1) == 1 and d2 == []
    assert len(sent_bodies) == 1
    captured = capsys.readouterr()
    everything = " ".join([captured.out, captured.err, sent_bodies[0], json.dumps(res1, default=str), json.dumps(res2, default=str)])
    assert secret_id not in everything
    assert secret_pw not in everything


def test_377_departed_truck_plan_frozen_at_departure_even_while_scheduled(tmp_path: Path) -> None:
    from delivery_db_manager import fetch_horizon_parts, fetch_truck_plan_lines

    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_freeze.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # Initial sync at 06:45: slot 1 (`10-12`, departs 08:00) = 200 EA, slot 4 (`16-18`, departs 14:00) = 300 EA
    bundle_am = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": [
            _make_part_row(
                key="PT-FRZ",
                part_no="PT-FRZ",
                name="FROZEN TEST BEZEL",
                vendor="VendorAlpha",
                cls="C",
                start_sap=100.0,
                load_slots={1: 200.0, 4: 300.0},
                req_slots={1: 50.0, 4: 50.0},
                trucks=[1, 4],
                run_date=run_date,
            )
        ],
        "box_qty": {"PT-FRZ": 100.0},
        "suppliers": [{"match": "VendorAlpha", "trucks": [1, 4], "mail": False, "to_env": "", "cc_env": ""}],
        "trucks_cfg": {"VendorAlpha": [1, 4]},
    }
    sync_plan_to_ledger(conn, run_date, bundle_am, cfg, dt.datetime(2026, 11, 16, 6, 45))

    # At 08:15 (> 08:00 departure of slot 1, BEFORE 08:30 first poll so status is still SCHEDULED!),
    # inbox changes slot 1 -> 500 EA and future slot 4 -> 450 EA.
    bundle_rewritten = {
        **bundle_am,
        "rows": [
            _make_part_row(
                key="PT-FRZ",
                part_no="PT-FRZ",
                name="FROZEN TEST BEZEL",
                vendor="VendorAlpha",
                cls="C",
                start_sap=100.0,
                load_slots={1: 500.0, 4: 450.0},
                req_slots={1: 50.0, 4: 50.0},
                trucks=[1, 4],
                run_date=run_date,
            )
        ],
    }
    info = sync_plan_to_ledger(conn, run_date, bundle_rewritten, cfg, dt.datetime(2026, 11, 16, 8, 15))
    assert info["frozen_trucks_preserved"] >= 1

    lines_by_tid = {ln["truck_id"]: ln for ln in fetch_truck_plan_lines(conn, "2026-11-16")}
    assert lines_by_tid["20261116_VendorAlpha_10-12"]["planned_pcs"] == 200.0  # Frozen at departure!
    assert lines_by_tid["20261116_VendorAlpha_16-18"]["planned_pcs"] == 450.0  # Future truck updated!

    hp = fetch_horizon_parts(conn, "2026-11-16")["PT-FRZ"]
    assert hp["load_36"][1] == 200.0
    assert hp["load_36"][4] == 450.0
    conn.close()


def test_377_0735_summary_non_failure_statuses_and_first_monday_banner(tmp_path: Path) -> None:
    mon = pytest.importorskip('morning_ops_notifier')
    from delivery_notifier import build_truck_close_email

    # 1. Verify Monday 2026-11-16 first-day banner in close mail
    mail = build_truck_close_email(
        {
            "truck_id": "20261116_VendorAlpha_08-10",
            "work_date": "2026-11-16",
            "vendor_display": "VendorAlpha",
            "truck_label": "8-10",
            "departure_ts": "2026-11-16 06:00:00",
            "closed_ts": "2026-11-16 07:50:00",
            "status": "CLOSED_RECEIVED",
            "matched_barcodes_count": 1,
            "received_pcs_total": 100.0,
        },
        [],
        load_delivery_config(),
    )
    assert "after the 06:00 truck departed" in mail["body"]

    # 2. Verify 07:35 summary treats non-failure statuses as OK (alert=False)
    job_def = {
        "id": "delivery_check_10m",
        "name": "WMS GR Intraday Truck Delivery Check",
        "task_name": "TASK_Delivery_Check_10m",
        "schedule": "Mon-Sat every 10m (07:40 -> 05:59 next day, --send-test-mail)",
        "cadence": "intraday_10m",
        "slot_spec": {
            "kind": "intraday_10m",
            "start_hhmm": "07:40",
            "end_next_day_hhmm": "05:59",
            "window_early_min": 2,
            "window_grace_min": 25,
            "first_scheduled_slot": "2026-11-16 07:40",
        },
        "artifacts": {"json": "01_M365MCP/02_DEPT_Logistics/02_JOB_Delivery_Check/logs/delivery_check_latest.json"},
        "probe": {
            "type": "delivery_check_json",
            "json": "01_M365MCP/02_DEPT_Logistics/02_JOB_Delivery_Check/logs/delivery_check_latest.json",
        },
        "registration_state": "active",
    }
    jpath = tmp_path / "01_M365MCP/02_DEPT_Logistics/02_JOB_Delivery_Check/logs/delivery_check_latest.json"
    jpath.parent.mkdir(parents=True, exist_ok=True)
    t_map = {
        "TASK_Delivery_Check_10m": {
            "TaskName": "TASK_Delivery_Check_10m",
            "State": "Ready",
            "LastRunTime": "2026-11-17 05:59:05",
            "LastTaskResult": 0,
            "Triggers": "MSFT_TaskWeeklyTrigger: Start=2026-11-16T07:40:00 Rep=PT10M/PT22H15M Days=126",
        }
    }
    for ok_st in (
        "OK",
        "IDLE_NO_DUE_TRUCKS",
        "POLL_SKIPPED_LOCK_BUSY",
        "DISABLED",
        "PLAN_NOT_AVAILABLE_YET",
        "MISSING_SRM_INPUT",
        "MISSING_ZM212",
    ):
        jpath.write_text(
            json.dumps({"executed_at": "2026-11-17 05:59:05", "status": ok_st}),
            encoding="utf-8",
        )
        row = mon._evaluate_single_job(
            job_def,
            t_map,
            tmp_path,
            dt.datetime(2026, 11, 17, 7, 35, 0),
        )
        assert row["status_key"] == "ok", f"Expected ok for {ok_st}, got {row}"
        assert row["alert"] is False
