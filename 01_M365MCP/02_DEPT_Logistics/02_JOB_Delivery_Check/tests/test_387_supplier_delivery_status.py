"""#387 comprehensive unit tests for supplier-facing SRM_DeliveryStatus workbook, warnings, and safety guards."""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

JOB_DIR = Path(__file__).resolve().parents[1]
DEPT_DIR = JOB_DIR.parent
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"
INV_DIR = DEPT_DIR.parent / "01_DEPT_Inventory"

for _p in (str(JOB_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delivery_db_manager import (  # noqa: E402
    fetch_barcodes,
    fetch_horizon_parts,
    fetch_truck_plan_lines,
    fetch_truck_runs,
    fetch_verdicts,
    get_connection,
    init_database,
)
from delivery_notifier import (  # noqa: E402
    dispatch_poll_notifications,
    load_delivery_config,
    validate_supplier_mail_config,
)
from delivery_projection_engine import (  # noqa: E402
    evaluate_poll_cycle,
    ingest_and_attribute_barcodes,
)
from plan_loader import compute_srm_plan_readonly, sync_plan_to_ledger  # noqa: E402
from srm_parsers import SLOT_LABELS  # noqa: E402
from supplier_delivery_status import (  # noqa: E402
    MISSING_ZM212_INTERNAL_LABEL,
    OPEN_DASH,
    build_supplier_delivery_status,
    write_supplier_delivery_status_file,
)

D = dt.date(2026, 11, 16)
E = ""


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def _hourly_rows(with_add: bool, recs: list) -> list:
    lead = ["plant code", "Partner code.", "Name of partner company.", "part number", "part name", "unit", "classification"]
    h0, h1 = list(lead), [E] * len(lead)
    for d, lab in enumerate(("DAY", "DAY1", "DAY2")):
        if d == 0 and with_add:
            h0 += [lab] + [E] * 12 + [f"11/{16 + d}"]
            h1 += ["ADD", *SLOT_LABELS, E]
        else:
            h0 += [lab] + [E] * 11 + [f"11/{16 + d}"]
            h1 += [*SLOT_LABELS, E]
    rows = [h0, h1]
    for part, vendor, cls, add, slots in recs:
        r = ["PLT1", "V1", vendor, part, f"DESC {part}", "EA", cls]
        for d in range(3):
            day = slots[d * 12 : (d + 1) * 12]
            if d == 0 and with_add:
                r.append(add)
            r += day + [sum(day)]
        rows.append(r)
    return rows


def _setup_fixture(
    tmp_path: Path,
    *,
    order_recs: list,
    req_recs: list,
    planreq_recs: list,
    zm_items: list,
):
    inbox = tmp_path / "inbox" / "20261116"
    _write_csv(inbox / "Order Detail.csv", _hourly_rows(True, order_recs))
    _write_csv(inbox / "Require Detail.csv", _hourly_rows(False, req_recs))

    pr_h0 = ["plant code", "plant", "Partner code.", "Name of partner company.", "part number", "part name", "plan date",
             "classification", "standard", "unit", "require", E, E, "PLAN", E, E, E, "BIN"]
    pr_h1 = [E] * 10 + ["11/16", "11/16 SUM", "11/17", "11/16", "11/16 urgent", "11/16 SUM", "11/17", E]
    pr_rows = [pr_h0, pr_h1]
    for part, vendor, cls, box_qty, req_d0, plan_d0 in planreq_recs:
        pr_rows.append(["PLT1", "X", "V1", vendor, part, f"DESC {part}", 1, cls, box_qty, "EA",
                        req_d0, req_d0, 0, plan_d0, E, plan_d0, 0, E])
    _write_csv(inbox / "Plan & Require.csv", pr_rows)

    zm_rows = [
        ["2026.11.16 Dynamic List Display"],
        [],
        [],
        [E, E, "Posting date", "Material number", "Material type", "Material description", E, E,
         "SWS storage location", "SAP storage location", "SWS Quantity", "SAP Quantity"],
        [],
    ]
    for mat_no, sap_q, wms_q in zm_items:
        zm_rows.append([E, E, "2026.11.16", mat_no, "ROH1", f"DESC {mat_no}", E, E, "SL00", "SL00", str(wms_q), str(sap_q)])
    zm_path = tmp_path / "zm212.csv"
    _write_csv(zm_path, zm_rows)

    sup_csv = tmp_path / "suppliers.csv"
    sup_csv.write_text(
        "match,trucks,mail,to_env,cc_env\nVendor A,10-12 12-14 16-18 22-24,yes,TEST_SUP_TO_A,TEST_SUP_CC_A\n",
        encoding="utf-8",
    )
    trucks_json = tmp_path / "trucks.json"
    trucks_json.write_text(
        json.dumps({
            "pm_additional_days": "next_run_day",
            "run_days": "MonSat",
            "leveling": {"enabled": True, "safety_margin": 0.15, "freeze_lead_min": 30, "overload_factor": 1.7},
        }),
        encoding="utf-8",
    )
    wl_csv = tmp_path / "no_watchlist.csv"
    cls_csv = tmp_path / "part_class.csv"

    bundle = compute_srm_plan_readonly(
        D,
        inbox_dir=inbox,
        zm212_path=zm_path,
        suppliers_csv=sup_csv,
        trucks_json=trucks_json,
        watchlist_csv=wl_csv,
        class_csv=cls_csv,
        include_d1=False,
    )
    assert bundle["status"] == "OK", bundle
    db_file = tmp_path / "delivery_check.db"
    init_database(db_file)
    conn = get_connection(db_file)
    cfg = load_delivery_config()
    sync_plan_to_ledger(conn, D, bundle, cfg, dt.datetime(2026, 11, 16, 7, 40))
    return conn, bundle, cfg


def _bc(barcode: str, mat_no: str, vendor: str, ts_str: str, qty: float) -> dict:
    gr_dt = dt.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    hrs = (gr_dt - dt.datetime(2026, 11, 16, 6, 0)).total_seconds() / 3600.0
    slot_idx = int(hrs // 24) * 12 + int((hrs % 24) // 2)
    pk = mat_no.lstrip("0") or "0"
    return {
        "barcode": barcode,
        "work_date": "2026-11-16",
        "mat_no": mat_no,
        "part_key": pk,
        "mat_desc": f"DESC {mat_no}",
        "vendor_name": vendor,
        "gr_loc": "SL00",
        "gr_time": ts_str,
        "gr_dt": gr_dt,
        "slot_idx": slot_idx,
        "gr_qty": float(qty),
        "unit": "EA",
        "move_type": "101",
        "trans_no": "T1",
        "invoice_no": "INV1",
        "source_file": "gr.xlsx",
    }


def test_387_live_mode_refused_unless_explicitly_approved_by_user(tmp_path, monkeypatch):
    """#387: `supplier_mail_mode: 'live'` is refused unless `supplier_mail_live_approved_by_user: true`."""
    bad_cfg_file = tmp_path / "bad_delivery_check.json"
    bad_cfg_file.write_text(json.dumps({"supplier_mail_mode": "live"}), encoding="utf-8")
    with pytest.raises(PermissionError, match="supplier_mail_live_approved_by_user"):
        load_delivery_config(bad_cfg_file)

    with pytest.raises(PermissionError, match="supplier_mail_live_approved_by_user"):
        validate_supplier_mail_config({"supplier_mail_mode": "live", "supplier_mail_live_approved_by_user": False})

    # Approved live config passes validation
    assert validate_supplier_mail_config({"supplier_mail_mode": "live", "supplier_mail_live_approved_by_user": True}) == "live"


def test_387_warning_boundary_box_rounding_open_dash_colors_and_single_sheet(tmp_path, monkeypatch):
    """#387:
    - Partial delivery still covered by next truck (start=60, delivered=40, req=100 before 16-18) -> red fill, NO warning!
    - Shortfall causing stock < 0 before next truck (start=35, delivered=40, req=100 -> -25 before 16-18, box=20)
      -> rounds up -25 to 40 pcs (2 boxes): 'Need 40 pcs (2 boxes) more before the 11/16 2pm truck (short from 11/16 12pm)'.
    - Over-delivery -> blue fill (`00E0F2FE`); equal -> no fill; open trucks -> `–` (`\\u2013`); Pallet -> day received so far.
    - Workbook has ONLY sheet `Truck plan` (no `2-hour detail`)."""
    monkeypatch.setenv("COMPANY_NAME", "TestPlant")
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "tester@example.com")

    # Part 1 (99001001): covered partial delivery -> start=60, srm[1]=100, srm[4]=100, req[1..3]=[40,30,30] -> end stock before slot 4 = 0 >= 0 (NO warning!)
    s1 = [0.0] * 36; s1[1] = 100.0; s1[4] = 100.0
    q1 = [0.0] * 36; q1[1], q1[2], q1[3] = 40.0, 30.0, 30.0

    # Part 2 (99001002): shortfall warning -> start=75 (no shortage on plan: 75+100-100=75), srm[1]=100, srm[4]=100, req[1..3]=[40,30,30]
    # Deliver only 0 on 10-12 ( wait: let Part 1 deliver 40, Part 2 deliver 0 on 10-12 -> 75 + 0 - 100 = -25 at slot 3 = 12pm! )
    # With box_qty = 20, raw_need = 25 -> ceil(25/20) = 2 boxes = 40 pcs!
    s2 = [0.0] * 36; s2[1] = 100.0; s2[4] = 100.0
    q2 = [0.0] * 36; q2[1], q2[2], q2[3] = 40.0, 30.0, 30.0

    # Part 3 (99001003): over-delivery on 10-12 (40->60) + balanced loads on 12-14 & 22-24 so max truck <= 1.7x avg!
    s3 = [0.0] * 36; s3[1] = 40.0; s3[2] = 200.0; s3[7] = 200.0
    q3 = [0.0] * 36

    # Part 4 (99001004): Class L Pallet Truck -> srm[0]=120, delivers 60 so far -> Pallet column shows 60
    s4 = [0.0] * 36; s4[0] = 120.0
    q4 = [0.0] * 36

    conn, bundle, cfg = _setup_fixture(
        tmp_path,
        order_recs=[
            ("099001001", "Vendor A Inc", "C", 0.0, s1),
            ("099001002", "Vendor A Inc", "C", 0.0, s2),
            ("099001003", "Vendor A Inc", "C", 0.0, s3),
            ("099001004", "Vendor A Inc", "L", 0.0, s4),
        ],
        req_recs=[
            ("099001001", "Vendor A Inc", "6D", 0.0, q1),
            ("099001002", "Vendor A Inc", "6D", 0.0, q2),
            ("099001003", "Vendor A Inc", "6D", 0.0, q3),
            ("099001004", "Vendor A Inc", "6D", 0.0, q4),
        ],
        planreq_recs=[
            ("099001001", "Vendor A Inc", "C", 20, 100, 200),
            ("099001002", "Vendor A Inc", "C", 20, 100, 200),
            ("099001003", "Vendor A Inc", "C", 20, 0, 440),
            ("099001004", "Vendor A Inc", "L", 60, 0, 120),
        ],
        zm_items=[
            ("099001001", 60, 60),
            ("099001002", 75, 75),
            ("099001003", 100, 100),
            ("099001004", 200, 200),
        ],
    )
    try:
        # Poll 1 at 08:30: barcodes arrive for 10-12 (Part 1 = 40, Part 2 = 0, Part 3 = 60, Pallet Part 4 = 60)
        bcs = [
            _bc("BC1", "099001001", "Vendor A Inc", "2026-11-16 08:20:00", 40.0),
            _bc("BC3A", "099001003", "Vendor A Inc", "2026-11-16 08:21:00", 40.0),
            _bc("BC3B", "099001003", "Vendor A Inc", "2026-11-16 08:25:00", 20.0),
            _bc("BC4", "099001004", "Vendor A Inc", "2026-11-16 08:26:00", 60.0),
        ]
        t1 = dt.datetime(2026, 11, 16, 8, 30)
        attr1 = ingest_and_attribute_barcodes(conn, D, bcs, cfg, t1)
        evaluate_poll_cycle(conn, D, cfg, t1, gr_available=True, new_planned_by_truck=attr1["new_planned_by_truck"])

        # Two quiet 10-min polls at 08:40 and 08:50 -> closes 10-12!
        evaluate_poll_cycle(conn, D, cfg, dt.datetime(2026, 11, 16, 8, 40), gr_available=True, new_planned_by_truck={})
        ev_close = evaluate_poll_cycle(conn, D, cfg, dt.datetime(2026, 11, 16, 8, 50), gr_available=True, new_planned_by_truck={})
        assert "20261116_Vendor A_10-12" in ev_close["newly_closed_trucks"]

        horizon = fetch_horizon_parts(conn, "2026-11-16")
        trucks = fetch_truck_runs(conn, "2026-11-16")
        lines = fetch_truck_plan_lines(conn, "2026-11-16")
        all_bcs = fetch_barcodes(conn, "2026-11-16")
        verdicts = fetch_verdicts(conn, "2026-11-16")

        model = build_supplier_delivery_status(
            D, dt.datetime(2026, 11, 16, 8, 50), "Vendor A",
            horizon, trucks, lines, all_bcs, ev_close["projections"], bundle["trucks_cfg"],
        )
        assert model is not None
        by_no = {p["part_no"]: p for p in model["parts"]}
        assert by_no["099001001"]["warning"] == ""    # Covered before 16-18 (stock hits exact 0, not < 0)!
        assert by_no["099001002"]["warning"] == "Need 40 pcs (2 boxes) more before the 11/16 2pm truck (short from 11/16 12pm)"
        assert by_no["099001003"]["warning"] == ""

        xlsx_path = write_supplier_delivery_status_file(tmp_path / "sup_out", model)
        wb = load_workbook(xlsx_path)
        assert wb.sheetnames == ["Truck plan"]   # Single sheet, NO '2-hour detail'!
        ws = wb["Truck plan"]

        # Verify header line contains run time, closed truck, and two-row explanation
        sub = str(ws["A2"].value)
        assert "08:50 EST" in sub and "10-12" in sub and "Plan =" in sub and "Delivered =" in sub

        # Rows: Part 1 = rows 7(Plan), 8(Delivered); Part 2 = rows 9, 10; Part 3 = rows 11, 12; Part 4 (Class L) = rows 13, 14
        # Col 6 = 10-12 (closed), Col 7 = 12-14 (open), Col 8 = 16-18 (open), Col 9 = 22-24 (open), Col 10 = Pallet, Col 11 = Day total
        assert ws.cell(row=7, column=5).value == "Plan"
        assert ws.cell(row=8, column=5).value == "Delivered"
        # Part 1: 40 < 100 on closed 10-12 -> PINK fill, open 16-18 -> '–', Warning empty!
        assert ws.cell(row=7, column=6).value == 100
        assert ws.cell(row=8, column=6).value == 40
        assert ws.cell(row=8, column=6).fill.fgColor.rgb == "00F4B6C2"
        assert ws.cell(row=8, column=8).value == OPEN_DASH
        assert ws.cell(row=8, column=ws.max_column).value is None

        # Part 2: 0 < 100 on closed 10-12 -> value 0, PINK fill, Warning on Delivered row ONLY!
        assert ws.cell(row=10, column=6).value == 0
        assert ws.cell(row=10, column=6).fill.fgColor.rgb == "00F4B6C2"
        assert ws.cell(row=9, column=ws.max_column).value is None
        assert ws.cell(row=10, column=ws.max_column).value == "Need 40 pcs (2 boxes) more before the 11/16 2pm truck (short from 11/16 12pm)"

        # Part 3: 60 > 40 on closed 10-12 -> LIGHT_BLUE fill!
        assert ws.cell(row=12, column=6).value == 60
        assert ws.cell(row=12, column=6).fill.fgColor.rgb == "00E0F2FE"

        # Part 4 (Pallet): day-level received so far = 60
        assert ws.cell(row=13, column=10).value == 120
        assert ws.cell(row=14, column=10).value == 60

        # Dispatch check: sends user-only [TEST] SUPPLIER_DELIVERY_STATUS mail when warning exists
        sent_list = []
        notices = dispatch_poll_notifications(
            conn,
            work_date="2026-11-16",
            as_of_ts=dt.datetime(2026, 11, 16, 8, 50),
            cfg=cfg,
            trucks=trucks,
            verdicts=verdicts,
            newly_closed_truck_ids=ev_close["newly_closed_trucks"],
            early_warnings=[],
            send_enabled=True,
            sender=lambda to, cc, subj, body, att: (sent_list.append((to, cc, subj, body, att)) or True),
            horizon_parts=horizon,
            plan_lines=lines,
            barcodes=all_bcs,
            projections=ev_close["projections"],
            trucks_cfg=bundle["trucks_cfg"],
            suppliers_rows=bundle["suppliers"],
            supplier_status_out_dir=tmp_path / "sup_mail_out",
        )
        sup_notices = [n for n in notices if n["notice_type"] == "SUPPLIER_DELIVERY_STATUS"]
        assert len(sup_notices) == 1 and sup_notices[0]["sent"] is True
        sup_mail = [m for m in sent_list if "truck delivery status" in m[2]][0]
        assert sup_mail[0] == ["tester@example.com"] and sup_mail[1] == []
        assert sup_mail[2].startswith("[TEST] ")
        assert "TEST_SUP_TO_A" in sup_mail[3]
        assert Path(sup_mail[4][0]).name.startswith("SRM_DeliveryStatus_Vendor_A_Inc_20261116_0850")
    finally:
        conn.close()


def test_387_off_schedule_receipt_counted_on_closed_truck_and_start_stock_guard(tmp_path, monkeypatch):
    """#387:
    1) OFF_SCHEDULE receipt arriving during an open C-truck window (`12-14`) for a part planned only on `22-24`
       is counted in `12-14`'s `Delivered` cell (blue fill since 40 > 0).
    2) Start-stock guard: part NOT in morning ZM212 (`start_in_zm212 == False`) with shortfall before next truck
       produces NO supplier warning (so 0 warnings -> NO supplier status mail sent!), while internal user mail
       shows `start stock not in ZM212 – check`."""
    monkeypatch.setenv("COMPANY_NAME", "TestPlant")
    monkeypatch.setenv("TEST_EMAIL_RECIPIENT", "tester@example.com")

    # Part A (99002001): NOT in ZM212! Planned 100 on 12-14, 100 on 22-24; req[2]=60, req[3]=60 -> if 12-14 delivers 20, stock goes < 0,
    # BUT since 99002001 is absent from ZM212, Start-stock guard MUST suppress supplier warning!
    sa = [0.0] * 36; sa[2] = 100.0; sa[7] = 100.0
    qa = [0.0] * 36; qa[2] = 20.0; qa[3] = 80.0

    # Part B (99002002): IN ZM212 (start=200), planned on 16-18 (80 EA) & 22-24 (80 EA) [carts 0,5,4,9 -> wait: 0,5,4,4+5=9, avg=4.5, 1.7x=7.65, make 16-18=120 so 0,5,6,9 avg=5.0, 1.7x=8.5 -> make 16-18=160 so 0,5,8,9 avg=5.5, 1.7x=9.35!]
    sb = [0.0] * 36; sb[4] = 160.0; sb[7] = 80.0
    qb = [0.0] * 36

    conn, bundle, cfg = _setup_fixture(
        tmp_path,
        order_recs=[
            ("099002001", "Vendor A Inc", "C", 0.0, sa),
            ("099002002", "Vendor A Inc", "C", 0.0, sb),
        ],
        req_recs=[
            ("099002001", "Vendor A Inc", "6D", 0.0, qa),
            ("099002002", "Vendor A Inc", "6D", 0.0, qb),
        ],
        planreq_recs=[
            ("099002001", "Vendor A Inc", "C", 20, 100, 200),
            ("099002002", "Vendor A Inc", "C", 20, 0, 240),
        ],
        zm_items=[
            # ONLY 099002002 is in ZM212! 099002001 is missing from ZM212!
            ("099002002", 200, 200),
        ],
    )
    try:
        # Close 10-12 (NO_PLANNED_LOAD) and receive on 12-14 (departure 10:00):
        # 20 EA planned Part A + 40 EA OFF_SCHEDULE Part B!
        bcs = [
            _bc("BC_A1", "099002001", "Vendor A Inc", "2026-11-16 10:15:00", 20.0),
            _bc("BC_B_OFF", "099002002", "Vendor A Inc", "2026-11-16 10:18:00", 40.0),
        ]
        t_p1 = dt.datetime(2026, 11, 16, 10, 30)
        attr = ingest_and_attribute_barcodes(conn, D, bcs, cfg, t_p1)
        evaluate_poll_cycle(conn, D, cfg, t_p1, gr_available=True, new_planned_by_truck=attr["new_planned_by_truck"])
        evaluate_poll_cycle(conn, D, cfg, dt.datetime(2026, 11, 16, 10, 40), gr_available=True, new_planned_by_truck={})
        ev_close = evaluate_poll_cycle(conn, D, cfg, dt.datetime(2026, 11, 16, 10, 50), gr_available=True, new_planned_by_truck={})
        assert "20261116_Vendor A_12-14" in ev_close["newly_closed_trucks"]

        horizon = fetch_horizon_parts(conn, "2026-11-16")
        trucks = fetch_truck_runs(conn, "2026-11-16")
        lines = fetch_truck_plan_lines(conn, "2026-11-16")
        all_bcs = fetch_barcodes(conn, "2026-11-16")
        verdicts = fetch_verdicts(conn, "2026-11-16")

        model = build_supplier_delivery_status(
            D, dt.datetime(2026, 11, 16, 10, 50), "Vendor A",
            horizon, trucks, lines, all_bcs, ev_close["projections"], bundle["trucks_cfg"],
        )
        by_no = {p["part_no"]: p for p in model["parts"]}

        # 1) OFF_SCHEDULE receipt on Part B counted on closed truck 12-14 (slot 2)!
        assert by_no["099002002"]["days"][0]["truck_plan"][2] == 0.0
        assert by_no["099002002"]["days"][0]["truck_deliv"][2] == 40.0

        # 2) Start-stock guard on Part A: warning suppressed -> model["warnings"] is EMPTY!
        assert by_no["099002001"]["start_in_zm212"] is False
        assert by_no["099002001"]["warning"] == ""
        assert by_no["099002001"]["warn_info"]["suppressed_missing_zm212"] is True
        assert model["warnings"] == []

        # 3) Dispatch: NO supplier mail sent (0 valid supplier warnings), while internal user close mail shows
        # 'start stock not in ZM212 – check'!
        sent_list = []
        notices = dispatch_poll_notifications(
            conn,
            work_date="2026-11-16",
            as_of_ts=dt.datetime(2026, 11, 16, 10, 50),
            cfg=cfg,
            trucks=trucks,
            verdicts=verdicts,
            newly_closed_truck_ids=ev_close["newly_closed_trucks"],
            early_warnings=[],
            send_enabled=True,
            sender=lambda to, cc, subj, body, att: (sent_list.append((to, cc, subj, body, att)) or True),
            horizon_parts=horizon,
            plan_lines=lines,
            barcodes=all_bcs,
            projections=ev_close["projections"],
            trucks_cfg=bundle["trucks_cfg"],
            suppliers_rows=bundle["suppliers"],
            supplier_status_out_dir=tmp_path / "sup_mail_out2",
        )
        assert [n for n in notices if n["notice_type"] == "SUPPLIER_DELIVERY_STATUS"] == []
        assert len(sent_list) == 1   # Only internal TRUCK_CLOSE_SUMMARY!
        assert MISSING_ZM212_INTERNAL_LABEL in sent_list[0][3]
    finally:
        conn.close()
