"""#330 02_DEPT_Logistics 00_JOB_SRM_Monitoring - engine / parser tests on synthetic data (dummy parts 9900xxxx).
The engine was also checked against the user's 11/11 workbook (TOP_6D 26/26, TOP_2W 111/111 identical) with
.agents/handoff/attachments/330_oracle_check.py (real data stays outside git)."""
import csv
import datetime as dt
import sys
from pathlib import Path

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))

import run_srm_monitoring as runner  # noqa: E402
from srm_parsers import SLOT_LABELS, parse_hourly, parse_plan_require, part_key, read_wms_gr, read_zm212_stock  # noqa: E402
from srm_projection import ENOUGH, gr_by_slot, gr_by_wms_day, merge_daily, merge_hourly, project_2w, project_6d  # noqa: E402

D = dt.date(2026, 11, 12)
E = ""


def _hourly_rows(with_add, recs):
    """SRM Order/Require Detail layout: DAY(+ADD) / DAY1 / DAY2 blocks of 12 slots + day total."""
    lead = ["plant code", "Partner code.", "Name of partner company.", "part number", "part name", "unit", "classification"]
    h0, h1 = list(lead), [E] * len(lead)
    for d, lab in enumerate(("DAY", "DAY1", "DAY2")):
        if d == 0 and with_add:
            h0 += [lab] + [E] * 12 + [f"11/{12 + d}"]
            h1 += ["ADD", *SLOT_LABELS, E]
        else:
            h0 += [lab] + [E] * 11 + [f"11/{12 + d}"]
            h1 += [*SLOT_LABELS, E]
    rows = [h0, h1]
    for part, vendor, cls, add, slots in recs:
        r = ["P999", "V1", vendor, part, f"NAME {part}", "EA", cls]
        for d in range(3):
            day = slots[d * 12:(d + 1) * 12]
            if d == 0 and with_add:
                r.append(add)
            r += day + [sum(day)]
        rows.append(r)
    return rows


def _zm_rows():
    return [["2026.11.12  Dynamic List Display"], [], [],
            [E, E, "Posting date", "Material number", "Material type", "Material description", E, E,
             "SWS storage location", "SAP storage location", "   SWS Quantity", "   SAP Quantity"],
            [],
            [E, E, "2026.11.11", "099001234", "ROH1", "A", E, E, "SL00", "SL00", "40", "50"],
            [E, E, "2026.11.11", "99001234", "ROH1", "A", E, E, "SL01", "SL01", "5", "5"],
            [E, E, "2026.11.11", "099005678-01", "ROH1", "B", E, E, "SL01", "SL01", "10", "0"]]


def test_part_key_and_zm212_sum():
    assert part_key("099001234") == part_key(99001234.0) == "99001234"
    assert part_key("099005678-01") == "99005678-01"
    st = read_zm212_stock(_zm_rows())
    assert st["99001234"] == {"sap": 55.0, "wms": 45.0} and st["99005678-01"] == {"sap": 0.0, "wms": 10.0}


def test_hourly_parse_add_and_totals():
    slots = [0.0] * 36
    slots[2] = 100.0
    labels, o = parse_hourly(_hourly_rows(True, [("099001234", "Vendor A", "C", 7.0, slots)]))
    rec = o["99001234"]
    assert labels == ["11/12", "11/13", "11/14"]
    assert rec["add"] == 7.0 and rec["slots"][2] == 100.0 and rec["totals"] == [100.0, 0.0, 0.0]
    assert rec["vendor"] == "Vendor A" and rec["part"] == "099001234"


def test_project_6d_inv_gr_short_and_labels():
    o_slots = [0.0] * 36
    o_slots[2] = 100.0                      # delivery 10-12 local
    r_slots = [0.0] * 36
    for s in range(6):
        r_slots[s] = 30.0                   # 30 per slot 06:00-18:00
    _, orders = parse_hourly(_hourly_rows(True, [("099001234", "Vendor A", "C", 0.0, o_slots)]))
    _, reqs = parse_hourly(_hourly_rows(False, [("099001234", "Vendor A", "6D", 0.0, r_slots)]))
    stock = {"99001234": {"sap": 50.0, "wms": 40.0}}
    gr = [("99001234", dt.datetime(2026, 11, 12, 5, 0), 999.0),      # before 06:00 -> already in morning stock
          ("99001234", dt.datetime(2026, 11, 12, 10, 30), 60.0)]     # 10-12 slot (index 2)
    res = project_6d(D, ["99001234"], orders, reqs, stock, gr_by_slot(D, gr))[0]
    inv, gs = res["rows"]["Inv"], res["rows"]["GR Short"]
    assert inv[:8] == [50.0, 20.0, -10.0, 60.0, 30.0, 0.0, -30.0, -30.0]
    assert gs[:8] == [50.0, 20.0, -10.0, 20.0, -10.0, -40.0, -70.0, -70.0]
    assert res["rows"]["GR"][3] == 60.0 and res["rows"]["GR-PO"][3] == -40.0
    assert res["rows"]["GR"][13] == 60.0 and res["rows"]["Order"][13] == 100.0   # day totals
    assert res["shortage_inv"] == "11/12 8am" and res["shortage_gr"] == "11/12 8am"
    ok = project_6d(D, ["99001234"], orders, {}, stock, {})[0]
    assert ok["shortage_inv"] == ENOUGH and ok["shortage_gr"] == ENOUGH


def test_project_2w_rules():
    h0 = ["plant code", "plant", "Partner code.", "Name of partner company.", "part number", "part name", "plan date",
          "classification", "standard", "unit", "require", E, E, E, "PLAN", E, E, E, E, "BIN"]
    h1 = [E] * 10 + ["11/12", "11/12 Add QTY", "11/12 SUM", "11/13", "11/12", "11/12 Add QTY", "11/12 urgent",
                     "11/12 SUM", "11/13", E]
    row = ["P999", "X", "V1", "Vendor A", "099001234", "NAME", 1, "L", 24, "EA",
           E, E, 80, 50, E, E, E, 20, 40, E]
    labels, pr = parse_plan_require([h0, h1, row])
    assert labels == ["11/12 SUM", "11/13"] and pr["99001234"]["require"] == [80.0, 50.0]
    assert pr["99001234"]["plan"] == [20.0, 40.0] and pr["99001234"]["box_qty"] == 24.0
    stock = {"99001234": {"sap": 70.0, "wms": 60.0}}
    gr = gr_by_wms_day(D, [("99001234", dt.datetime(2026, 11, 12, 9), 15.0),
                           ("99001234", dt.datetime(2026, 11, 13, 5, 59), 5.0),     # still WMS day 0
                           ("99001234", dt.datetime(2026, 11, 13, 7), 30.0)])       # WMS day 1
    res = project_2w(labels, ["99001234"], pr, stock, gr)[0]
    assert res["rows"]["Inv"] == [10.0, 0.0]                        # SAP 70 + (20-80) + (40-50)
    assert res["rows"]["GR"] == [20.0, 30.0] and res["rows"]["GR-PO"] == [0.0, -10.0]
    assert res["rows"]["GR Short"] == [0.0, -10.0]                  # WMS 60 + GR 20 - 80, then + order 40 - 50
    assert res["shortage"] == "11/13"


def test_wms_gr_reader_keeps_101_only():
    rows = [["Vendor Name", "Barcode", "Mat.No", "Mat.Desc", "GR Loc", "GR Time", "GR Qty", "Unit", "FIFO Viol", "Move.Type"],
            ["V", "B1", "099001234", "A", "SL00", "2026-11-12 07:00:00", "10", "EA", "", "101"],
            ["V", "B2", "099001234", "A", "SL00", "2026-11-12 07:10:00", "4", "EA", "", "122"]]
    assert read_wms_gr(rows) == [("99001234", dt.datetime(2026, 11, 12, 7), 10.0)]


def test_runner_dry_run_end_to_end(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def _w(name, rows):
        with (inbox / name).open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)

    o_slots = [0.0] * 36
    o_slots[2] = 100.0
    r_slots = [30.0] * 6 + [0.0] * 30
    _w("SRM_Order_Detail.csv", _hourly_rows(True, [("099001234", "Vendor A", "C", 0.0, o_slots)]))
    _w("SRM_Require_Detail.csv", _hourly_rows(False, [("099001234", "Vendor A", "6D", 0.0, r_slots)]))
    _w("SRM_Plan&Require.csv", [
        ["plant code", "plant", "Partner code.", "Name of partner company.", "part number", "part name", "plan date",
         "classification", "standard", "unit", "require", E, E, "PLAN", E, E, E, "BIN"],
        [E] * 10 + ["11/12", "11/12 SUM", "11/13", "11/12", "11/12 urgent", "11/12 SUM", "11/13", E],
        ["P999", "X", "V1", "Vendor A", "099001234", "NAME", 1, "L", 24, "EA", 80, 80, 50, 20, E, 20, 40, E]])
    zm = tmp_path / "zm212.csv"
    with zm.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(_zm_rows())
    monkeypatch.setattr(runner, "WATCHLIST", tmp_path / "none.csv")
    monkeypatch.setattr(runner, "CLASS_FILE", tmp_path / "part_class.csv")
    import srm_trucks
    monkeypatch.setattr(srm_trucks, "SUPPLIERS", tmp_path / "no_suppliers.csv")
    st = runner.run(D, inbox, zm, tmp_path / "out")
    assert st["status"] == "OK" and Path(st["report"]).exists()
    assert st["parts_2h"] == 1 and st["short_2h_planned"] == 1 and st["parts_daily"] == 1
    assert any("WMS GR List not provided" in n for n in st["notes"])
    assert runner.run(D, tmp_path / "empty", zm, tmp_path / "out")["status"] == "MISSING_INPUT"
    assert runner.run(D, inbox, tmp_path / "nope.txt", tmp_path / "out")["status"] == "MISSING_ZM212"


def test_merge_1340_d1_files_replace_tomorrow_onward():
    """06:50 file (delivery date = today) keeps today; 13:40 file (delivery date = tomorrow) replaces days 2-3."""
    m_slots = [1.0] * 12 + [2.0] * 12 + [3.0] * 12
    n_slots = [7.0] * 12 + [8.0] * 12 + [9.0] * 12
    _, m = parse_hourly(_hourly_rows(True, [("099001234", "Vendor A", "C", 5.0, m_slots)]))
    rows_n = _hourly_rows(True, [("099001234", "Vendor A", "C", 6.0, n_slots)])
    rows_n[0] = [("11/" + str(int(c[3:]) + 1)) if isinstance(c, str) and c.startswith("11/") else c for c in rows_n[0]]
    lab_n, n = parse_hourly(rows_n)
    assert lab_n == ["11/13", "11/14", "11/15"]
    x, used = merge_hourly(D, m, n, lab_n)
    assert used and x["99001234"]["slots"] == [1.0] * 12 + [7.0] * 12 + [8.0] * 12
    assert x["99001234"]["add"] == 5.0 and x["99001234"]["totals"] == [12.0, 84.0, 96.0]
    same, used2 = merge_hourly(D, m, n, ["11/12", "11/13", "11/14"])      # not for tomorrow -> 06:50 only
    assert not used2 and same is m

    m_daily = {"99001234": {"part": "099001234", "vendor": "V", "name": "N", "box_qty": 1.0,
                            "require": [10.0, 11.0, 12.0], "plan": [20.0, 21.0, 22.0]}}
    n_daily = {"99001234": {"part": "099001234", "vendor": "V", "name": "N", "box_qty": 1.0,
                            "require": [31.0, 32.0, 33.0], "plan": [41.0, 42.0, 43.0]}}
    lab, y, used3 = merge_daily(D, ["11/12 SUM", "11/13", "11/14"], m_daily, ["11/13 SUM", "11/14", "11/15"], n_daily, n_days=4)
    assert used3 and lab == ["11/12 SUM", "11/13", "11/14", "11/15"]
    assert y["99001234"]["require"] == [10.0, 31.0, 32.0, 33.0] and y["99001234"]["plan"] == [20.0, 41.0, 42.0, 43.0]
    lab2, y2, used4 = merge_daily(D, ["11/12 SUM", "11/13", "11/14"], m_daily, None, None, n_days=3)
    assert not used4 and y2["99001234"]["require"] == [10.0, 11.0, 12.0]


def test_truck_plan_pull_forward_and_add_spread():
    from srm_trucks import plan_trucks
    tr = [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]
    # day total covers, 2-hour view short at 12-14: pull boxes forward; each truck also keeps 110% of the
    # requirement of the slot in which the next truck arrives (delay buffer)
    p10 = plan_trucks(260.0, 0.0, [0, 0, 0, 0, 63, 0, 0, 315, 0, 0, 0, 0] + [0] * 24,
                      [100, 91, 99, 34, 0, 0, 0, 9, 75, 0, 0, 0] + [0] * 24, tr, 21.0, delay_buffer=1.10)
    assert [p10["slots"][i] for i in tr] == [42, 42, 0, 294] and p10["unfixed"] is None
    p = plan_trucks(260.0, 0.0, [0, 0, 0, 0, 63, 0, 0, 315, 0, 0, 0, 0] + [0] * 24,
                    [100, 91, 99, 34, 0, 0, 0, 9, 75, 0, 0, 0] + [0] * 24, tr, 21.0)
    assert [p["slots"][i] for i in tr] == [63, 21, 0, 294] and p["unfixed"] is None
    assert sum(p["slots"]) == 378
    # urgent ADD spread over the trucks by need; leftover on the last truck; total unchanged
    p = plan_trucks(50.0, 300.0, [0.0] * 36, [40] * 7 + [0] * 29, tr, 30.0)
    assert [p["slots"][i] for i in tr] == [90, 90, 60, 60] and sum(p["slots"]) == 300 and p["unfixed"] is None
    # shortage before the first truck cannot be fixed by reloading
    p = plan_trucks(0.0, 100.0, [0.0] * 36, [10] + [0] * 35, tr, 10.0)
    assert p["unfixed"] == 0 and p["reason"].startswith("Before the first truck")
    # SRM day quantity too small: minimum extra (box multiples) on the truck that needs it, flagged separately
    p = plan_trucks(0.0, 0.0, [0, 10] + [0] * 34, [0, 45] + [0] * 34, tr, 10.0)
    assert p["unfixed"] is None and p["extra"][1] == 40 and p["slots"][1] == 50


def test_daily_extras_minimum_boxes():
    from srm_trucks import daily_extras
    sup = [0.0] * 36
    req = [0.0] * 36
    req[3], req[15] = 35.0, 20.0
    ex = daily_extras(10.0, sup, req, 12.0)
    assert ex[0] == 36.0 and ex[12] == 12.0 and sum(ex) == 48.0   # 10-35 -> -25 -> 3 boxes; then 11-20 -> 1 box


def test_extra_only_on_allowed_days():
    from srm_trucks import daily_extras, plan_trucks
    tr = [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]
    req = [0.0] * 36
    req[1], req[13] = 45.0, 45.0
    am = plan_trucks(0.0, 0.0, [0.0] * 36, req, tr, 10.0, extra_days={0})
    assert am["extra"][1] == 50 and am["extra"][13] == 0 and am["reason"].startswith("Orders for this day come with the 13:40")
    pm = plan_trucks(0.0, 0.0, [0.0] * 36, req, tr, 10.0, extra_days={1, 2})
    assert pm["extra"][1] == 0 and pm["extra"][13] == 90   # also covers the open shortage carried from today
    assert daily_extras(0.0, [0.0] * 36, req, 10.0, {0})[12] == 0


def _res_row(key, vendor, start):
    return {"key": key, "part": "0" + key, "vendor": vendor, "name": "NAME " + key, "start_sap": start}


def test_plan_parts_am_and_pm_windows():
    from srm_plan import plan_parts, window
    tr = {"Vendor A": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}
    req = [0.0] * 36
    req[1], req[13], req[25] = 45.0, 45.0, 45.0
    om = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": [0.0] * 36}}
    rm = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req}}
    rows = [_res_row("99001234", "Vendor A X", 0.0)]
    plan_parts(D, rows, om, rm, None, None, False, tr, {"99001234": 10.0}, {"99001234": "C"})
    p = rows[0]["plan"]
    assert len(p["load"]) == 36 and p["extra"][1] == 50 and sum(p["extra"]) == 50   # AM: today only
    w = window(rows[0], 0)
    assert w["start"] == 0.0 and w["dates"][0] == D
    # PM: 13:40 file (delivery date tomorrow) has 3 days -> 4-day plan, supplier window = tomorrow + 2
    r1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req[12:] + [45.0] + [0.0] * 11}}
    o1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": [0.0] * 36}}
    rows = [_res_row("99001234", "Vendor A X", 0.0)]
    plan_parts(D, rows, om, rm, o1, r1, True, tr, {"99001234": 10.0}, {"99001234": "C"})
    p = rows[0]["plan"]
    assert len(p["load"]) == 48 and p["extra"][1] == 50 and p["extra"][13] == 40   # tomorrow uses the 5 left today
    assert all(v >= 0 for v in p["stock"]) and p["unfixed"] is None
    w = window(rows[0], 1)
    assert w["dates"][0] == D + dt.timedelta(days=1) and w["start"] == 5.0     # 50 - 45 carried over from today


def test_supplier_file_marks_changes_and_additional(tmp_path):
    from openpyxl import load_workbook
    from srm_plan import plan_parts
    from srm_supplier_files import write_supplier_files
    tr = {"Vendor A": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}
    req = [0.0] * 36
    req[1:5] = [100.0, 91.0, 99.0, 34.0]
    slots = [0.0] * 36
    slots[4], slots[7] = 63.0, 315.0
    om = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": slots},
          "99005678": {"part": "099005678", "vendor": "Vendor A X", "name": "M", "add": 0.0, "slots": [0.0] * 36}}
    req2 = [0.0] * 36
    req2[3] = 30.0
    rm = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req},
          "99005678": {"part": "099005678", "vendor": "Vendor A X", "name": "M", "add": 0.0, "slots": req2}}
    rows = [_res_row("99001234", "Vendor A X", 260.0), _res_row("99005678", "Vendor A X", 0.0)]
    plan_parts(D, rows, om, rm, None, None, False, tr, {"99001234": 21.0, "99005678": 10.0},
               {"99001234": "C", "99005678": "C"})
    paths = write_supplier_files(tmp_path, "AM", rows, {"99001234": 21.0, "99005678": 10.0}, dt.datetime(2026, 11, 12, 6, 55))
    assert len(paths) == 1 and paths[0].name.startswith("SRM_TruckPlan_Vendor_A_X_20261112_AM")
    ws = load_workbook(paths[0])["Truck plan"]
    fills = {(c.row, c.column): c.fill.fgColor.rgb for row in ws.iter_rows(min_row=7, max_row=8) for c in row}
    assert "00FFE699" in fills.values()          # yellow: loading changed from SRM
    assert "00F4B6C2" in fills.values()          # pink: additional qty not in SRM (second part)
    texts = [ws.cell(row=r, column=ws.max_column).value for r in (7, 8)]
    assert texts[0] == "OK" and texts[1].startswith("+30 additional")


def test_learn_classes_keeps_last_known(tmp_path):
    f = tmp_path / "cls.csv"
    assert runner.learn_classes({"A": "C", "B": "L"}, f) == {"A": "C", "B": "L"}
    assert runner.learn_classes({"A": "", "C": "C"}, f) == {"A": "C", "B": "L", "C": "C"}   # blank keeps old value


def test_truck_plan_additional_shown_on_the_truck_that_got_more():
    """A truck that gave boxes to an earlier truck and then needs an additional quantity itself: the additional
    quantity is labelled on the earlier truck (loads unchanged), so no truck reads 'below SRM' and 'additional'."""
    from srm_trucks import plan_trucks
    tr = [0, 2, 4]                                     # 8-10 / 12-14 / 16-18
    req = [63.0] * 5 + [0.0] * 31
    p = plan_trucks(0.0, 0.0, [126.0, 0.0, 84.0] + [0.0] * 33, req, tr, 21.0)
    assert [p["slots"][i] for i in tr] == [210, 42, 63] and p["unfixed"] is None
    assert [p["extra"][i] for i in tr] == [42, 0, 63] and sum(p["extra"]) == 105


def test_pm_additional_days_tomorrow_only():
    from srm_plan import plan_parts
    from srm_trucks import PENDING_REASON
    tr = {"Vendor A": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}
    req = [0.0] * 36
    req[1], req[13], req[25] = 45.0, 45.0, 45.0
    om = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": [0.0] * 36}}
    rm = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req}}
    r1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0,
                       "slots": req[12:] + [45.0] + [0.0] * 11}}
    o1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": [0.0] * 36}}
    rows = [_res_row("99001234", "Vendor A X", 0.0)]
    plan_parts(D, rows, om, rm, o1, r1, True, tr, {"99001234": 10.0}, {"99001234": "C"}, "tomorrow")
    p = rows[0]["plan"]
    assert p["extra"][1] == 50 and p["extra"][13] == 40 and sum(p["extra"][24:]) == 0
    assert p["unfixed"] == 25 and p["reason"] == PENDING_REASON          # day after tomorrow: recheck 13:40


def test_pm_extra_days_next_run_day():
    """user 11/12 19:26: additional quantity up to the next run day (no Sunday run -> Saturday decides Monday)."""
    from srm_plan import pm_extra_days
    thu, fri, sat = dt.date(2026, 11, 12), dt.date(2026, 11, 13), dt.date(2026, 11, 14)
    assert pm_extra_days(thu, 4, "next_run_day") == {0, 1}            # Saturday waits for Friday 13:40
    assert pm_extra_days(fri, 4, "next_run_day") == {0, 1}
    assert pm_extra_days(sat, 4, "next_run_day") == {0, 1, 2}         # Sat, Sun, Mon
    assert pm_extra_days(sat, 4, "next_run_day", "Daily") == {0, 1}
    assert pm_extra_days(sat, 4, "tomorrow") == {0, 1} and pm_extra_days(thu, 4, "all") == {0, 1, 2, 3}


def test_pm_next_run_day_saturday_plans_monday():
    from srm_plan import plan_parts
    from srm_trucks import PENDING_REASON
    tr = {"Vendor A": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}
    req_m, req_1 = [0.0] * 36, [0.0] * 36
    req_m[1] = 45.0                                    # run day, first truck slot
    req_1[1], req_1[13], req_1[25] = 45.0, 45.0, 45.0  # +1, +2, +3 days (13:40 file)
    zero = [0.0] * 36

    def run(day, mode):
        om = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": zero}}
        rm = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req_m}}
        o1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": zero}}
        r1 = {"99001234": {"part": "099001234", "vendor": "Vendor A X", "name": "N", "add": 0.0, "slots": req_1}}
        rows = [_res_row("99001234", "Vendor A X", 0.0)]
        plan_parts(day, rows, om, rm, o1, r1, True, tr, {"99001234": 10.0}, {"99001234": "C"}, mode, "MonSat")
        return rows[0]["plan"]

    p = run(dt.date(2026, 11, 14), "next_run_day")                    # Saturday 13:40
    assert (p["extra"][1], p["extra"][13], p["extra"][25]) == (50, 40, 50)   # Sat, Sun, Mon covered now
    assert sum(p["extra"][36:]) == 0 and p["unfixed"] == 37 and p["reason"] == PENDING_REASON   # Tue: Mon 13:40
    p = run(dt.date(2026, 11, 12), "next_run_day")                    # Thursday 13:40 = today and tomorrow only
    assert (p["extra"][1], p["extra"][13], p["extra"][25]) == (50, 40, 0)
    assert p["unfixed"] == 25 and p["reason"] == PENDING_REASON       # Saturday: Friday 13:40 decides


def test_truck_config_values_are_checked(tmp_path):
    import pytest
    from srm_trucks import PM_EXTRA_MODES, RUN_DAYS, pm_additional_days, run_days
    f = tmp_path / "trucks.json"
    f.write_text('{"pm_additional_days": "next_run_day", "run_days": "MonSat"}', encoding="utf-8")
    assert pm_additional_days(f) == "next_run_day" and run_days(f) == "MonSat"
    assert pm_additional_days(tmp_path / "missing.json") == "all" and run_days(tmp_path / "missing.json") == "MonSat"
    f.write_text('{"pm_additional_days": "tomorow", "run_days": "Weekdays"}', encoding="utf-8")
    with pytest.raises(ValueError):
        pm_additional_days(f)                                         # a typo stops the run instead of 'all'
    with pytest.raises(ValueError):
        run_days(f)
    assert pm_additional_days() in PM_EXTRA_MODES and run_days() in RUN_DAYS   # the shipped config is valid


def test_learn_classes_dry_run_does_not_write(tmp_path):
    f = tmp_path / "cls.csv"
    runner.learn_classes({"A": "C"}, f)
    assert runner.learn_classes({"A": "L", "B": "C"}, f, write=False) == {"A": "L", "B": "C"}
    assert runner.learn_classes({}, f) == {"A": "C"}                    # file unchanged by the dry run


def test_suppliers_file_trucks_and_mail_keys(tmp_path):
    from srm_trucks import load_suppliers, load_trucks
    f = tmp_path / "suppliers.csv"
    f.write_text("match,trucks,mail,to_env,cc_env\nVendor A,12-14 10-12,yes,SRM_T_TO_A,SRM_T_CC_A\n"
                 "Vendor B,,no,,\n", encoding="utf-8")
    rows = load_suppliers(f)
    assert rows[0] == {"match": "Vendor A", "trucks": [1, 2], "mail": True, "to_env": "SRM_T_TO_A", "cc_env": "SRM_T_CC_A"}
    assert rows[1]["trucks"] == [] and rows[1]["mail"] is False
    assert load_trucks(f) == {"Vendor A": [1, 2]} and load_suppliers(tmp_path / "missing.csv") == []
    f.write_text("match,trucks,mail,to_env,cc_env\nVendor A,10-11,yes,,\n", encoding="utf-8")
    try:
        load_suppliers(f)
        assert False, "typo in a slot name must stop the run"
    except ValueError as exc:
        assert "10-11" in str(exc)
