"""
Unit & Regression Tests for Handoff #403 (3-Truck 60-Cart Schedule & All-Supplier Class L Pallet Tracking)
=================================================================
Covers:
  1. B1 (`00_JOB_SRM_Monitoring`):
     - Unique `"8-10 10-12 22-24": 60` cap resolution via `data/suppliers.csv`.
     - Absorption of SRM quantities in non-fixed ('Other') slots (`14-16`, `16-18`) onto the 3 fixed
       trucks (`8-10`, `10-12`, `22-24`) under `115% -> 100%` coverage, keeping day totals unchanged,
       overflowing `> 60` to the last truck, firing `Extra truck needed` when `day_total > 180`, and
       listing any unplaceable non-truck quantity if moving it would cause a shortage.
     - `mail: no` builds the supplier workbook for internal review while sending ZERO supplier mails.
  2. B2 (`02_JOB_Delivery_Check`):
     - Automatic inclusion of Class L (Pallet) items across EVERY SRM supplier (even unlisted once-a-day
       suppliers not in `suppliers.csv`) at day level (`12:00` display, `DAY_LEVEL_OPEN -> CLOSED_DAY_LEVEL`).
     - Non-SRM sea-freight container barcodes (`pk not in horizon`) from Pallet-only suppliers are ignored.
     - Pallet shortage alarm fires when end-of-day stock does not cover the next requirement.
     - Zero supplier-status files and zero supplier mails for `mail: no` and auto-derived Pallet suppliers.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

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
from delivery_notifier import dispatch_poll_notifications, load_delivery_config  # noqa: E402
from delivery_projection_engine import (  # noqa: E402
    evaluate_poll_cycle,
    ingest_and_attribute_barcodes,
    parse_gr_excel_barcodes,
)
from plan_loader import sync_plan_to_ledger  # noqa: E402
from srm_mailer import load_config as load_srm_mail_cfg, send_run_mails  # noqa: E402
from srm_parsers import N_SLOTS  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_supplier_files import write_supplier_files  # noqa: E402
from srm_trucks import leveling_config, load_trucks  # noqa: E402


def _make_srm_part(
    key: str,
    vendor: str,
    start_sap: float,
    srm_slots: List[float],
    req_slots: List[float],
    add_d0: float = 0.0,
) -> tuple:
    row = {
        "key": key,
        "part": key,
        "name": f"PART {key}",
        "vendor": vendor,
        "start_sap": start_sap,
        "start_wms": start_sap,
    }
    o = {"slots": list(srm_slots), "add": add_d0}
    q = {"slots": list(req_slots), "add": 0.0}
    return row, o, q


def _make_bundle_row(
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
            "trucks": trucks if cls == "C" and trucks else None,
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


def test_403_b1_three_truck_60_cap_other_slot_absorption_overflow_and_no_mail(
    tmp_path: Path, monkeypatch
) -> None:
    sup_csv = tmp_path / "suppliers.csv"
    monkeypatch.setenv("COMPANY_NAME", "Two-Facility Plant")
    sup_csv.write_text(
        "\n".join([
            "match,trucks,mail,to_env,cc_env",
            "VendorThree,8-10 10-12 22-24,no,,",
            "",
        ]),
        encoding="utf-8",
    )
    lvl = leveling_config(suppliers_path=sup_csv)
    assert lvl["max_carts_per_truck"]["VendorThree"] == 60
    tr_cfg = load_trucks(sup_csv)
    assert tr_cfg == {"VendorThree": [0, 1, 7]}

    run_date = dt.date(2026, 11, 17)
    # Day 0: non-fixed 16-18 (slot 4) has 20 carts (200 EA) + fixed [30, 20, 20] = 90 carts total (<= 180)
    # Day 1: heavy day [75, 70, 55] = 200 carts (> 3 * 60 = 180) -> [60, 60, 80] + Extra truck needed for 20 carts!
    # Day 2: unplaceable non-truck slot (slot 3 has 100 EA, fixed trucks [1, 7] wait: here trucks=[0,1,7], put slot 0?
    #        Suppose slot 2 (`12-14`) has 100 EA, wait: pulling slot 2 -> slot 1 is earlier so always valid!
    #        What if trucks were [2, 4, 7] and slot 0 had immediate req? Test unplaceable separately below!)
    srm = [0.0] * 36
    req = [0.0] * 36
    srm[0], srm[1], srm[4], srm[7] = 300.0, 200.0, 200.0, 200.0
    srm[12 + 0], srm[12 + 1], srm[12 + 7] = 750.0, 700.0, 550.0
    r1, o1, q1 = _make_srm_part("PT-3T-1", "VendorThree Inc", 1000.0, srm, req)

    diag = plan_parts(
        run_date,
        [r1],
        {"PT-3T-1": o1},
        {"PT-3T-1": q1},
        None,
        None,
        False,
        tr_cfg,
        {"PT-3T-1": 10.0},
        {"PT-3T-1": "C"},
        level_cfg=lvl,
    )
    d0 = diag["suppliers"]["VendorThree"]["days"]["2026-11-17"]
    assert d0["other_carts_before"] == 20.0
    assert d0["other_carts_after"] == 0.0
    assert r1["plan"]["load"][4] == 0.0
    assert sum(r1["plan"]["load"][:12]) == 900.0
    assert all(x <= 60.0 for x in d0["trucks_after"])
    assert d0["extra_truck_warnings"] == []

    d1 = diag["suppliers"]["VendorThree"]["days"]["2026-11-18"]
    assert d1["trucks_after"] == [60.0, 60.0, 80.0]
    assert d1["extra_truck_warnings"] == [
        "Extra truck needed: Wed 11/18 total 200 carts > 3 trucks × 60 = 180. "
        "Please arrange an additional truck for 20 carts."
    ]

    # Supplier file is generated for internal review, AND mail=no sends ZERO supplier mail even in live mode!
    sfiles = write_supplier_files(tmp_path / "sup_out", "AM", [r1], {"PT-3T-1": 10.0}, dt.datetime(2026, 11, 17, 6, 45))
    assert len(sfiles) == 1
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "planner@internal-ops.local")
    mail_cfg = {
        "mode": "live",
        "user_env": "TEST_EMAIL_RECIPIENT",
        "send_when_no_change": True,
        "suppliers": {},  # mail=no in suppliers.csv -> excluded from cfg["suppliers"]
    }
    sent_subjects: List[str] = []
    send_run_mails(
        {
            "run_date": "2026-11-17",
            "slot": "AM",
            "run_label": "AM",
            "parts_2h": 1,
            "parts_daily": 1,
            "short_2h_planned": 0,
            "short_daily": 0,
            "loading_changed_parts": 1,
            "additional_qty_parts": 0,
            "still_short_parts": 0,
            "recheck_parts": 0,
            "supplier_files": [str(sfiles[0])],
        },
        [r1],
        dt.datetime(2026, 11, 17, 6, 45),
        cfg=mail_cfg,
        sender=lambda to, cc, subj, body, att: sent_subjects.append(subj) or True,
    )
    # Only the 1 internal summary mail is sent — zero supplier mails!
    assert len(sent_subjects) == 1
    assert "[SRM Monitoring]" in sent_subjects[0]


def test_403_b1_unplaceable_other_slot_listed_without_creating_shortage() -> None:
    run_date = dt.date(2026, 11, 17)
    # Fixed trucks start at slot 2 (`12-14`), while SRM has 50 EA at slot 0 (`8-10`) and immediate req[0] = 50 EA
    # with start_sap = 0 -> moving slot 0 to slot 2 would create Inv[0] = -50 < 0! Must keep at slot 0 & list!
    srm = [0.0] * 36
    req = [0.0] * 36
    srm[0], srm[2] = 50.0, 100.0
    req[0] = 50.0
    r_u, o_u, q_u = _make_srm_part("PT-UNP-OTH", "VendorThree", 0.0, srm, req)
    lvl = {
        "enabled": True,
        "freeze_departed": False,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"VendorThree": 60},
    }
    diag = plan_parts(
        run_date,
        [r_u],
        {"PT-UNP-OTH": o_u},
        {"PT-UNP-OTH": q_u},
        None,
        None,
        False,
        {"VendorThree": [2, 4, 7]},
        {"PT-UNP-OTH": 10.0},
        {"PT-UNP-OTH": "C"},
        level_cfg=lvl,
    )
    d0 = diag["suppliers"]["VendorThree"]["days"]["2026-11-17"]
    assert len(d0["unplaced_other_slots"]) == 1
    assert d0["unplaced_other_slots"][0]["slot"] == "8-10"
    assert r_u["plan"]["load"][0] == 50.0
    assert all(v >= -1e-9 for v in r_u["plan"]["stock"])


def test_403_b2_all_srm_pallet_suppliers_tracked_endstock_shortage_and_no_new_supplier_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "planner@internal-ops.local")
    run_date = dt.date(2026, 11, 16)
    db_path = init_database(tmp_path / "dc_403.db")
    conn = get_connection(db_path)
    cfg = load_delivery_config()

    # Configured supplier in suppliers.csv WITH mail=False (`VendorThree`, Class C, short before next truck)
    # + TWO unlisted once-a-day Class L suppliers (`VendorPalletA` delivered OK, `VendorPalletB` NOT_DELIVERED
    #   where end stock = 100 < tomorrow's next requirement = 250 -> triggers pallet shortage alarm!)
    rows = [
        _make_bundle_row(
            key="PT-C-NOMAIL",
            part_no="PT-C-NOMAIL",
            name="BRACKET THREE",
            vendor="VendorThree Inc",
            cls="C",
            start_sap=40.0,
            load_slots={1: 200.0, 7: 200.0},
            req_slots={2: 100.0},
            trucks=[0, 1, 7],
            run_date=run_date,
        ),
        _make_bundle_row(
            key="PT-PAL-OK",
            part_no="PT-PAL-OK",
            name="ONCE A DAY LENS",
            vendor="VendorPalletA",
            cls="L",
            start_sap=500.0,
            load_slots={0: 1000.0, 12: 1000.0},
            req_slots={0: 200.0, 12: 200.0},
            trucks=[],
            run_date=run_date,
        ),
        _make_bundle_row(
            key="PT-PAL-SHORT",
            part_no="PT-PAL-SHORT",
            name="ONCE A DAY HOUSING",
            vendor="VendorPalletB",
            cls="L",
            start_sap=300.0,
            load_slots={0: 1000.0, 12: 1000.0},
            req_slots={2: 200.0, 12: 250.0},  # End stock without today's 1000 = 100 < next req 250!
            trucks=[],
            run_date=run_date,
        ),
    ]
    suppliers_rows = [
        {"match": "VendorThree", "trucks": [0, 1, 7], "mail": False, "to_env": "", "cc_env": ""},
    ]
    bundle = {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "AM",
        "rows": rows,
        "box_qty": {"PT-C-NOMAIL": 20.0, "PT-PAL-OK": 250.0, "PT-PAL-SHORT": 250.0},
        "suppliers": suppliers_rows,
        "trucks_cfg": {"VendorThree": [0, 1, 7]},
    }
    sync_plan_to_ledger(conn, run_date, bundle, cfg, dt.datetime(2026, 11, 16, 7, 0))

    tmap = {t["truck_id"]: t for t in fetch_truck_runs(conn, "2026-11-16")}
    assert "20261116_VendorPalletA_PALLET" in tmap
    assert "20261116_VendorPalletB_PALLET" in tmap
    assert tmap["20261116_VendorPalletA_PALLET"]["status"] == "DAY_LEVEL_OPEN"

    # WMS GR has:
    # - PT-PAL-OK (1000 EA -> DELIVERED)
    # - Non-SRM sea-freight container item from VendorPalletB (`SEA-999` not in horizon -> MUST BE IGNORED!)
    gr_xlsx = _write_gr_xlsx(
        tmp_path / "gr_403.xlsx",
        [
            {
                "Barcode": "BPOK-1",
                "Mat.No": "PT-PAL-OK",
                "Mat.Desc": "ONCE A DAY LENS",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 15:20:00",
                "GR Qty": 1000,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorPalletA",
                "Trans.No": "TP1",
                "INVOICE_NO": "IP1",
            },
            {
                "Barcode": "BSEA-IGNORE",
                "Mat.No": "SEA-999",
                "Mat.Desc": "NON SRM SEA CONTAINER PART",
                "GR Loc": "SL00",
                "GR Time": "2026-11-16 16:00:00",
                "GR Qty": 5000,
                "Unit": "EA",
                "Move.Type": "101",
                "Vendor Name": "VendorPalletB",
                "Trans.No": "TSEA",
                "INVOICE_NO": "ISEA",
            },
        ],
    )

    sup_out_dir = tmp_path / "sup_status_out"
    sent_notices: List[str] = []

    # 1) Close VendorThree 10-12 C-truck at 11:00 -> mail=False MUST produce ZERO supplier-status files!
    ts_11 = dt.datetime(2026, 11, 16, 11, 0)
    bcs_11 = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_11)
    attr_11 = ingest_and_attribute_barcodes(conn, run_date, bcs_11, cfg, ts_11)
    ev_11 = evaluate_poll_cycle(conn, run_date, cfg, ts_11, gr_available=True, new_planned_by_truck=attr_11["new_planned_by_truck"])
    from delivery_db_manager import fetch_barcodes, fetch_horizon_parts, fetch_truck_plan_lines

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
        sender=lambda to, cc, subj, body, att: sent_notices.append(subj) or True,
        horizon_parts=fetch_horizon_parts(conn, "2026-11-16"),
        plan_lines=fetch_truck_plan_lines(conn, "2026-11-16"),
        barcodes=fetch_barcodes(conn, "2026-11-16"),
        projections=ev_11["projections"],
        trucks_cfg={"VendorThree": [0, 1, 7]},
        suppliers_rows=suppliers_rows,
        supplier_status_out_dir=sup_out_dir,
    )
    assert not sup_out_dir.exists() or list(sup_out_dir.glob("*.xlsx")) == []
    assert not any("truck delivery status" in s.lower() for s in sent_notices)

    # 2) Close workday at 05:55 next morning -> Pallet trucks close CLOSED_DAY_LEVEL!
    ts_eod = dt.datetime(2026, 11, 17, 5, 55)
    bcs_eod = parse_gr_excel_barcodes(gr_xlsx, run_date, as_of_ts=ts_eod)
    attr_eod = ingest_and_attribute_barcodes(conn, run_date, bcs_eod, cfg, ts_eod)
    ev_eod = evaluate_poll_cycle(conn, run_date, cfg, ts_eod, gr_available=True, new_planned_by_truck=attr_eod["new_planned_by_truck"])

    vmap = {(v["truck_id"], v["part_key"]): v for v in fetch_verdicts(conn, "2026-11-16")}
    assert vmap[("20261116_VendorPalletA_PALLET", "PT-PAL-OK")]["verdict"] == "DELIVERED"
    assert vmap[("20261116_VendorPalletA_PALLET", "PT-PAL-OK")]["line_shortage_alarm"] == 0

    v_short = vmap[("20261116_VendorPalletB_PALLET", "PT-PAL-SHORT")]
    assert v_short["verdict"] == "NOT_DELIVERED"
    assert v_short["line_shortage_alarm"] == 1
    assert v_short["deficit_before_next_truck_pcs"] == 150.0  # next req 250 - end stock 100 = 150!

    # SEA-999 non-SRM container part was completely ignored from verdicts and Day_Part_Summary!
    assert all(v["part_key"] != "SEA-999" for v in fetch_verdicts(conn, "2026-11-16"))
    assert all(r["part_key"] != "SEA-999" for r in ev_eod["day_part_summary"])
    conn.close()
