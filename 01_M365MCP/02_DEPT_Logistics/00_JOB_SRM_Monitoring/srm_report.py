"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - Excel report (English; part name next to part number)
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import datetime as _dt

from srm_parsers import SLOT_LABELS
from srm_projection import ENOUGH, ROWS, N_HOURLY_DAYS, N_SLOTS, local_label, slot_start, timeline_6d

_RED = "C00000"
_SHORT_FILL = "FDE2E2"
_HEAD_FILL = "DDEBF7"


def _sheet_rows(ws, header: List[str], data: List[List[Any]], short_cols: List[int], first_value_col: int) -> None:
    from openpyxl.styles import Font, PatternFill, Alignment

    ws.append(header)
    for c in ws[ws.max_row]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor=_HEAD_FILL)
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for row in data:
        ws.append(row)
        r = ws.max_row
        for ci in short_cols:
            cell = ws.cell(row=r, column=ci)
            if cell.value not in (None, "", ENOUGH):
                cell.fill = PatternFill("solid", fgColor=_SHORT_FILL)
                cell.font = Font(bold=True, color=_RED)
        for ci in range(first_value_col, len(row) + 1):
            cell = ws.cell(row=r, column=ci)
            if isinstance(cell.value, (int, float)):
                cell.number_format = "#,##0;[Red]-#,##0"
    ws.freeze_panes = ws.cell(row=2, column=first_value_col)


def write_report(path: Path, run_date, res6: List[Dict[str, Any]], res2: List[Dict[str, Any]], day_labels_2w: List[str],
                 box_qty: Dict[str, float], notes: List[str]) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    by2 = {r["key"]: r for r in res2}
    by6 = {r["key"]: r for r in res6}
    keys = list(dict.fromkeys([r["key"] for r in res6] + [r["key"] for r in res2]))

    def _rank(k: str):
        r6, r2 = by6.get(k), by2.get(k)
        short = any(x not in (None, ENOUGH) for x in ((r6 or {}).get("shortage_inv"), (r6 or {}).get("shortage_gr"), (r2 or {}).get("shortage")))
        meta = r6 or r2
        return (0 if short else 1, meta["vendor"], meta["part"])

    summary = []
    for k in sorted(keys, key=_rank):
        r6, r2 = by6.get(k), by2.get(k)
        meta = r6 or r2
        summary.append([meta["vendor"], meta["part"], meta["name"],
                        r6["shortage_inv"] if r6 else "", r6["shortage_gr"] if r6 else "", r2["shortage"] if r2 else "",
                        meta["start_sap"], meta["start_wms"]])
    _sheet_rows(ws, ["Vendor", "Part #", "Part Name", "2-hour view: first shortage (with planned deliveries)",
                     "2-hour view: first shortage (received stock only)", "14-day view: first shortage",
                     "Start stock SAP", "Start stock WMS"], summary, [4, 5, 6], 7)
    ws.append([])
    for n in notes:
        ws.append([n])
    for col, w in zip("ABCDEFGH", (34, 16, 42, 22, 22, 18, 14, 14)):
        ws.column_dimensions[col].width = w

    # 2-hour view
    ws6 = wb.create_sheet("2-hour view (3 days)")
    head = ["Shortage", "Vendor", "Part #", "Part Name", "Box Qty", "Start (SAP)", "Row"]
    for kind, d, s in timeline_6d():
        head.append("ADD" if kind == "add" else
                    local_label(slot_start(run_date, d, s)) if kind == "slot" else f"Day {d + 1} total")
    data = []
    for r in res6:
        for row in ROWS:
            lab = r["shortage_gr"] if row in ("GR-PO", "GR Short") else r["shortage_inv"]
            data.append([lab, r["vendor"], r["part"], r["name"], box_qty.get(r["key"]), r["start_sap"], row] + r["rows"][row])
    _sheet_rows(ws6, head, data, [1], 8)
    for col, w in zip("ABCDEFG", (14, 30, 16, 40, 8, 10, 10)):
        ws6.column_dimensions[col].width = w

    # 14-day view
    ws2 = wb.create_sheet("Daily view (14 days)")
    head = ["Shortage", "Vendor", "Part #", "Part Name", "Box Qty", "Start (SAP)", "Start (WMS)", "Row"] + list(day_labels_2w)
    data = []
    for r in res2:
        for row in ROWS:
            data.append([r["shortage"], r["vendor"], r["part"], r["name"], r["box_qty"], r["start_sap"], r["start_wms"], row] + r["rows"][row])
    _sheet_rows(ws2, head, data, [1], 9)
    for col, w in zip("ABCDEFGH", (14, 30, 16, 40, 8, 10, 10, 10)):
        ws2.column_dimensions[col].width = w

    # loading changes / additional quantities across suppliers (days 0-2 of the plan)
    rows = []
    for r in sorted(res6, key=lambda x: (x["vendor"], x["part"])):
        p = r.get("plan")
        if not p:
            continue
        for d in range(3):
            day = p["dates"][d]
            for s_ in range(N_SLOTS):
                i = d * N_SLOTS + s_
                srm = p["srm"][i] + (p["add"][d] if (s_ == 0 and not p["trucks"]) else 0.0)
                changed = bool(p["trucks"]) and abs(p["load"][i] - p["srm"][i]) > 1e-9
                if not changed and not p["extra"][i]:
                    continue
                note = "urgent ADD spread over the trucks" if (p["trucks"] and p["add"][d]) else ""
                rows.append([r["vendor"], r["part"], r["name"], p["cls"], f"{day.month}/{day.day}", SLOT_LABELS[s_],
                             local_label(slot_start(run_date, d, s_)), srm, p["load"][i], p["extra"][i] or None, note])
    wst = wb.create_sheet("Loading changes")
    _sheet_rows(wst, ["Vendor", "Part #", "Part Name", "Class", "Day", "SRM slot", "Local time", "SRM qty",
                      "Load / delivery qty", "Additional (not in SRM)", "Note"], rows, [], 8)
    for col, w in zip("ABCDEFGHIJK", (30, 16, 40, 6, 7, 9, 12, 9, 12, 12, 30)):
        wst.column_dimensions[col].width = w
    if not rows:
        wst.append(["No loading changes and no additional quantities."])

    write_overview(wb, run_date, res6, box_qty)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return path


# ---------------------------------------------------------------------------- supplier overview (TOP_6D style)
def write_overview(wb, run_date, res6: List[Dict[str, Any]], box_qty: Dict[str, float], title: str = "Overview") -> None:
    """One block per part, like the user's TOP_6D sheet: Order (SRM) / Truck load (suppliers with truck times) / Prod.Plan / Inv.
    Columns: ADD + 12 two-hour slots + day total, for 3 days. Inv uses the truck load where there is one."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    ws = wb.create_sheet(title, 0)
    fill = lambda c: PatternFill("solid", fgColor=c)
    head_fill, order_fill, add_fill, chg_fill, short_fill = fill("1F3864"), fill("FCE4D6"), fill("FFE699"), fill("FFE699"), fill("FDE2E2")
    thin = Side(style="thin", color="BFBFBF")
    numfmt = '#,##0;[Red]-#,##0;"-"'
    cols: List[tuple] = [("add", 0, 0)] + [(k, d, s) for d in range(N_HOURLY_DAYS)
                                           for k, s in [("slot", s) for s in range(N_SLOTS)] + [("total", 0)]]
    lead = ["Shortage", "Vendor", "Part #", "Part Name", "Box Qty", "Start Inv", "Row"]
    # header rows: local time / SRM round / SRM slot
    r1 = [f"{run_date.month}/{run_date.day}", "", "", "SRM orders by 2-hour truck slot (3 days)", "", "", "Local time"]
    r2 = ["", "", "", "", "", "", "SRM round"]
    r3 = list(lead)
    for kind, d, s in cols:
        if kind == "add":
            r1.append(""); r2.append(""); r3.append("ADD")
        elif kind == "slot":
            r1.append(local_label(slot_start(run_date, d, s))); r2.append((s + 3) % 12 + 1); r3.append(SLOT_LABELS[s])
        else:
            dd = run_date + _dt.timedelta(days=d)
            r1.append(""); r2.append(""); r3.append(f"{dd.month}/{dd.day} total")
    for row in (r1, r2, r3):
        ws.append(row)
    for c in ws[3]:
        c.fill = head_fill
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center")
    for c in ws[1][7:] + ws[2][7:]:
        c.font = Font(bold=True, size=9)
        c.alignment = Alignment(horizontal="center")
    ws.cell(row=1, column=1).fill = fill("FFFF00")

    def _series(vals36: List[float], add: float, totals_from: Optional[List[float]] = None) -> List[float]:
        out = [add]
        for d in range(N_HOURLY_DAYS):
            day = vals36[d * N_SLOTS:(d + 1) * N_SLOTS]
            out += day + [sum(day) + (add if d == 0 else 0.0)]
        return out

    for r in sorted(res6, key=lambda x: (x["vendor"], x["part"])):
        p = r.get("plan")
        if not p:
            continue
        o36, q36, load36 = p["srm"][:3 * N_SLOTS], p["req"][:3 * N_SLOTS], p["load"][:3 * N_SLOTS]
        ext36, add = p["extra"][:3 * N_SLOTS], p["add"][0]
        has_trucks = bool(p["trucks"])
        stock = p["stock"][:3 * N_SLOTS]
        inv_rows = [r["start_sap"]]
        for d in range(3):
            inv_rows += stock[d * N_SLOTS:(d + 1) * N_SLOTS] + [stock[(d + 1) * N_SLOTS - 1]]
        short = next((local_label(slot_start(run_date, i // N_SLOTS, i % N_SLOTS)) for i, v in enumerate(stock)
                      if v < -1e-9), None)
        label = short or "Enough"
        base = [label, r["vendor"], r["part"], r["name"], box_qty.get(r["key"]) or None, r["start_sap"]]
        o_add = list(o36)
        blocks = [("Order (SRM)", _series(o_add, add))]
        if has_trucks or any(ext36):
            blocks.append(("Truck load" if has_trucks else "Delivery", _series(load36, 0.0)))
        blocks += [("Prod.Plan", _series(q36, 0.0)), ("Inv", inv_rows)]
        for name, vals in blocks:
            ws.append(base + [name] + vals)
            rr = ws.max_row
            for ci in range(1, len(base) + 2 + len(vals)):
                c = ws.cell(row=rr, column=ci)
                c.border = Border(bottom=thin) if name == "Inv" else Border()
                if ci >= 8:
                    c.number_format = numfmt
            if name == "Order (SRM)":
                for ci in range(1, 8):
                    ws.cell(row=rr, column=ci).fill = order_fill
                if add > 0:
                    ws.cell(row=rr, column=8).fill = add_fill
            if name in ("Truck load", "Delivery"):
                for j, (kind, d, s) in enumerate(cols):
                    if kind != "slot":
                        continue
                    i = d * N_SLOTS + s
                    if ext36[i] > 1e-9:
                        ws.cell(row=rr, column=8 + j).fill = fill("F4B6C2")
                        ws.cell(row=rr, column=8 + j).font = Font(bold=True, color="C00000")
                    elif has_trucks and abs(load36[i] - o36[i]) > 1e-9:
                        ws.cell(row=rr, column=8 + j).fill = chg_fill
                        ws.cell(row=rr, column=8 + j).font = Font(bold=True)
            if name == "Inv":
                for ci in range(8, 8 + len(vals)):
                    ws.cell(row=rr, column=ci).font = Font(color="7030A0")
            if label != "Enough":
                ws.cell(row=rr, column=1).fill = short_fill
                ws.cell(row=rr, column=1).font = Font(bold=True, color="C00000")
    for col, w in zip("ABCDEFG", (11, 22, 13, 36, 7, 8, 11)):
        ws.column_dimensions[col].width = w
    from openpyxl.utils import get_column_letter
    for j in range(len(cols)):
        ws.column_dimensions[get_column_letter(8 + j)].width = 7.5
    ws.freeze_panes = "H4"
    ws.auto_filter.ref = f"A3:G{ws.max_row}"
