"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - one Excel file per supplier (user 11/12; #369 A style)
==================================================================================================
Window: morning run (AM, delivery date today) = today + 2 days; afternoon run (PM, delivery date tomorrow) =
tomorrow + 2 days (stock carried over from today's plan).
Sheet "Truck plan" (suppliers with fixed trucks): rows = parts (several-a-day items first, then once-a-day items),
  columns per day = its trucks, Pallet Truck (once-a-day items), Other times (SRM time without a truck), Day total.
  Yellow = loading changed from the SRM order, pink = additional quantity not yet in SRM, slate-grey = day total.
Sheet "2-hour detail": per part Load / Requirement / Stock / SRM order on every 2-hour slot.
Suppliers without fixed trucks get the 2-hour detail only.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

DEPT_DIR = Path(__file__).resolve().parent.parent
if str(DEPT_DIR) not in sys.path:
    sys.path.insert(0, str(DEPT_DIR))

from report_excel_style import (  # noqa: E402
    AMBER,
    BODY_TEXT,
    BOX_TOTAL_FMT,
    GREEN,
    HDR_PRIMARY,
    HDR_SECONDARY,
    HDR_SUBTLE,
    HDR_TOTAL,
    LIGHT_BLUE,
    META_BAND,
    MUTED_TEXT,
    NUM_FMT as NUM,
    ORANGE,
    PINK,
    RED,
    STOCK_FMT as STOCK,
    STOCK_POS_TEXT,
    SUBTLE_TEXT,
    WHITE,
    YELLOW,
    ZEBRA_FILL as ZEBRA,
    apply_title_and_legend_band,
    make_styles,
)
from srm_env import company_name  # noqa: E402
from srm_parsers import N_SLOTS, SLOT_LABELS  # noqa: E402
from srm_plan import window  # noqa: E402
from srm_projection import local_label  # noqa: E402
from srm_trucks import PENDING_REASON  # noqa: E402


def _safe(name: str) -> str:
    """File-name part; a name without any English letters gets a short stable code (no two suppliers share a file)."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")[:40]
    return s or "Supplier_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]


def supplier_file_prefix(vendor: str) -> str:
    """File name start of a supplier's file (the mailer attaches only that supplier's own file)."""
    return f"SRM_TruckPlan_{_safe(vendor)}_"


def _slot_time(day: dt.date, s: int) -> str:
    return local_label(dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=6 + 2 * s)).split(" ")[1]


def status_text(w: Dict[str, Any]) -> Dict[str, str]:
    """Stock check cell for a part window."""
    short = next((i for i, v in enumerate(w["stock"]) if v < -1e-9), None)
    extra = sum(w["extra"])
    if w["start"] < -1e-9:
        return {"text": "Short at start", "color": RED, "fill": ""}
    if short is not None:
        day = w["dates"][short // N_SLOTS]
        when = f"{day.month}/{day.day} {_slot_time(day, short % N_SLOTS)}"
        if w["reason"] == PENDING_REASON and short // N_SLOTS > 0:
            return {"text": f"Recheck 13:40 ({when})", "color": AMBER, "fill": ""}
        return {"text": f"Short {when} - call us", "color": RED, "fill": ""}
    if extra > 1e-9:
        return {"text": f"+{extra:,.0f} additional", "color": RED, "fill": PINK}
    return {"text": "OK", "color": GREEN, "fill": ""}


def write_supplier_files(out_dir: Path, run_label: str, res6: List[Dict[str, Any]], box_qty: Dict[str, float],
                         issued: dt.datetime, prev_plans: Optional[Dict[str, Dict[str, Any]]] = None,
                         file_tag: str = "") -> List[Path]:
    first_day = 1 if run_label == "PM" else 0
    by_vendor: Dict[str, List[Dict[str, Any]]] = {}
    for r in res6:
        if not r.get("plan") or not r["vendor"]:
            continue
        w = window(r, first_day)
        active = any(w["srm"]) or any(w["req"]) or any(w["add"]) or any(w["extra"])
        if active or w["cls"] == "C":                       # several-a-day items always listed (user 11/12)
            by_vendor.setdefault(r["vendor"], []).append((r, w))
    paths = []
    for vendor, items in sorted(by_vendor.items()):
        items.sort(key=lambda x: (x[1]["cls"] == "L", x[0]["part"]))
        dates = items[0][1]["dates"]
        path = out_dir / f"{supplier_file_prefix(vendor)}{dates[0]:%Y%m%d}_{run_label}{file_tag}.xlsx"
        p_info = None
        if prev_plans:
            p_info = prev_plans.get(vendor)
            if p_info is None:
                v_low = vendor.lower()
                p_info = next((val for k, val in prev_plans.items() if k.lower() in v_low or v_low in k.lower()), None)
        _write_one(path, vendor, items, run_label, box_qty, issued, prev_info=p_info)
        paths.append(path)
    return paths


def _write_one(path: Path, vendor: str, items: List[tuple], run_label: str, box_qty: Dict[str, float],
               issued: dt.datetime, prev_info: Optional[Dict[str, Any]] = None) -> None:
    from openpyxl import Workbook
    from openpyxl.comments import Comment
    from openpyxl.styles import Alignment, Border
    from openpyxl.utils import get_column_letter as L

    company = company_name()
    AUTHOR = company   # cell-note author  # noqa: N806
    st = make_styles()
    fill, font, thin, mid, grid_border = st["fill"], st["font"], st["thin"], st["mid"], st["grid_border"]
    center = st["align_center"]
    right = st["align_right"]
    trucks = next((w["trucks"] for _, w in items if w["trucks"]), None)
    dates: List[dt.date] = items[0][1]["dates"]
    start_label = "Stock 06:00 today" if run_label == "AM" else "Stock 06:00 tomorrow (projected)"
    issue_txt = ("morning update - today's final orders incl. urgent orders" if run_label == "AM"
                 else "afternoon update - orders from tomorrow")
    subtitle = f"{vendor}   |   {company}   |   issued {issued:%a %m/%d %H:%M} EST   |   {issue_txt}"
    wb = Workbook()

    def part_cells(ws, row: int, r: Dict[str, Any], w: Dict[str, Any]) -> None:
        for c, v in enumerate([r["part"], r["name"], box_qty.get(r["key"]) or None, w["start"]], 1):
            cell = ws.cell(row=row, column=c, value=v)
            cell.font = font(size=9.5, bold=(c == 1), color=BODY_TEXT)
            cell.number_format = STOCK if c == 4 else (NUM if c == 3 else "@")
            cell.alignment = st["align_left"] if c <= 2 else right

    # ---------------------------------------------------------------- Truck plan (suppliers with trucks)
    if trucks:
        ws = wb.active
        ws.title = "Truck plan"
        lead = ["Part #", "Part name", "Box qty", start_label]
        h1, h2 = 5, 6
        others = [s for s in range(N_SLOTS) if s not in trucks]
        cols: List[Dict[str, Any]] = []   # per day: {'truck': {slot: col}, 'pallet': col, 'other': col, 'total': col}
        col = len(lead) + 1
        prev_cells_early = ((prev_info or {}).get("parsed") or {}).get("cells") or {}
        for d, day in enumerate(dates):
            d_hdr = f"{day:%a} {day.month}/{day.day}"
            pallet = (
                any(w["cls"] == "L" and any(w["load"][d * N_SLOTS:(d + 1) * N_SLOTS]) for _, w in items)
                or any(prev_cells_early.get((r["part"], d_hdr, "Pallet Truck"), 0.0) > 1e-6 for r, _ in items)
            )
            other = (
                any(w["cls"] != "L" and any(w["load"][d * N_SLOTS + s] for s in others) for _, w in items)
                or any(prev_cells_early.get((r["part"], d_hdr, "Other times"), 0.0) > 1e-6 for r, _ in items)
            )
            spec: Dict[str, Any] = {"truck": {}, "start": col}
            for t in trucks:
                ws.cell(row=h2, column=col, value=f"{SLOT_LABELS[t]}\n{_slot_time(day, t)}")
                spec["truck"][t] = col
                col += 1
            if pallet:
                ws.cell(row=h2, column=col, value="Pallet\nTruck")
                spec["pallet"] = col
                col += 1
            if other:
                ws.cell(row=h2, column=col, value="Other\ntimes")
                spec["other"] = col
                col += 1
            ws.cell(row=h2, column=col, value="Day\ntotal")
            spec["total"] = col
            ws.merge_cells(start_row=h1, start_column=spec["start"], end_row=h1, end_column=col)
            ws.cell(row=h1, column=spec["start"], value=f"{day:%a} {day.month}/{day.day}")
            cols.append(spec)
            col += 1
        status_col = col
        prev_sent_lbl = str((prev_info or {}).get("sent_at") or "13:40")
        prev_cells_map = ((prev_info or {}).get("parsed") or {}).get("cells") or {}
        is_refresh_mode = prev_info is not None
        yellow_legend = (
            f"Yellow = changed since the {prev_sent_lbl} plan"
            if is_refresh_mode
            else "Loading changed from the SRM order (SRM qty in the cell note)"
        )
        apply_title_and_legend_band(
            ws,
            "Truck loading plan",
            subtitle,
            [
                (YELLOW, yellow_legend),
                (PINK, f"Additional - not yet in SRM ({company} to confirm)"),
                (ORANGE, "Pallet Truck - once-a-day items"),
                (META_BAND, "Day total"),
            ],
            [1, 4, 9, 14],
            status_col,
        )
        for c, h in enumerate(lead + [None] * (status_col - len(lead) - 1) + ["Stock check"], 1):
            if h:
                ws.merge_cells(start_row=h1, start_column=c, end_row=h2, end_column=c)
                ws.cell(row=h1, column=c, value=h)
        for rr in (h1, h2):
            for c in range(1, status_col + 1):
                cell = ws.cell(row=rr, column=c)
                is_lead_or_status = c <= len(lead) or c == status_col
                cell.fill = fill(HDR_PRIMARY if (rr == h1 or is_lead_or_status) else HDR_SECONDARY)
                cell.font = font(size=10 if rr == h1 else 9.5, bold=True, color=WHITE)
                cell.alignment = center
                cell.border = grid_border
        for spec in cols:
            for key in ("pallet", "other"):
                if key in spec:
                    ws.cell(row=h2, column=spec[key]).fill = fill(HDR_SUBTLE)
                    ws.cell(row=h2, column=spec[key]).font = font(size=9.5, bold=True, color=HDR_PRIMARY)
            ws.cell(row=h2, column=spec["total"]).fill = fill(HDR_TOTAL)
            for rr in (h1, h2):
                tc = ws.cell(row=rr, column=spec["total"])
                tc.border = Border(left=thin if rr == h1 else mid, right=mid, top=thin, bottom=thin)
                sc_start = ws.cell(row=rr, column=spec["start"])
                sc_start.border = Border(left=mid, right=sc_start.border.right, top=thin, bottom=thin)
        ws.row_dimensions[h1].height = 22
        ws.row_dimensions[h2].height = 30
        totals: Dict[int, float] = {}
        boxes: Dict[int, float] = {}
        row = h2 + 1
        for n, (r, w) in enumerate(items):
            ws.row_dimensions[row].height = 20
            part_cells(ws, row, r, w)
            box = box_qty.get(r["key"]) or 0.0
            for d, spec in enumerate(cols):
                base = d * N_SLOTS
                day_sum = 0.0
                if w["cls"] == "L":
                    vals = {"pallet": sum(w["load"][base:base + N_SLOTS])}
                    pe = sum(w["extra"][base:base + N_SLOTS])
                else:
                    vals = {t: w["load"][base + t] for t in trucks}
                    vals["other"] = sum(w["load"][base + s] for s in others)
                    pe = 0.0
                day_hdr_key = f"{dates[d]:%a} {dates[d].month}/{dates[d].day}"
                for key, q in vals.items():
                    c = spec["truck"].get(key) if isinstance(key, int) else spec.get(key)
                    if c is None:
                        continue
                    slot_hdr_key = (
                        SLOT_LABELS[key] if isinstance(key, int)
                        else ("Pallet Truck" if key == "pallet" else "Other times")
                    )
                    old_q = float(prev_cells_map.get((r["part"], day_hdr_key, slot_hdr_key), 0.0)) if is_refresh_mode else 0.0
                    changed_vs_prev = is_refresh_mode and abs(q - old_q) > 1e-6
                    if not q and not changed_vs_prev:
                        continue
                    cell = ws.cell(row=row, column=c, value=q)
                    cell.font = font(size=9.5, bold=False, color=BODY_TEXT)
                    day_sum += q
                    totals[c] = totals.get(c, 0.0) + q
                    boxes[c] = boxes.get(c, 0.0) + (q / box if box else 0.0)
                    srm = (w["srm"][base + key] if isinstance(key, int) else
                           sum(w["srm"][base + s] for s in (range(N_SLOTS) if key == "pallet" else others)) + w["add"][d])
                    ex = w["extra"][base + key] if isinstance(key, int) else pe
                    moved = q - srm - ex
                    if isinstance(key, int) and abs(moved) > 1e-9:
                        cum_before = sum(
                            w["load"][base + s_i] - w["srm"][base + s_i] - w["extra"][base + s_i]
                            for s_i in range(key)
                        )
                        cum_after = cum_before + moved
                        if moved > 0:
                            dir_phrase = "from earlier trucks" if cum_before < -1e-9 else "from later trucks"
                            moved_txt = f" {moved:+,.0f} " + dir_phrase + (" / urgent ADD" if w["add"][d] else "")
                        else:
                            dir_phrase = "moved to later trucks" if cum_after < -1e-9 else "moved to earlier trucks"
                            moved_txt = f" {moved:+,.0f} " + dir_phrase
                    else:
                        moved_txt = ("" if abs(moved) <= 1e-9 else f" {moved:+,.0f} " + (
                            "from later trucks" + (" / urgent ADD" if w["add"][d] else "") if moved > 0
                            else "moved to earlier trucks"))
                    chg_prefix = f"Changed since {prev_sent_lbl} plan: {old_q:,.0f} -> {q:,.0f}. " if changed_vs_prev else ""
                    if ex > 1e-9:
                        cell.fill = fill(PINK)
                        cell.font = font(size=9.5, bold=True, color=RED)
                        cell.comment = Comment(f"{chg_prefix}Load {q:,.0f} = SRM order {srm:,.0f}{moved_txt} + additional "
                                               f"{ex:,.0f} (needed to avoid a line shortage, not yet in SRM; "
                                               f"{company} will confirm)", AUTHOR)
                    elif is_refresh_mode:
                        # In refresh mode, YELLOW strictly marks cells changed vs the previous sent plan (#394)
                        base_note = ""
                        if key == "pallet":
                            base_note = ("Once-a-day item (Pallet Truck). SRM time: " + ", ".join(
                                f"{SLOT_LABELS[s]} {w['srm'][base + s]:,.0f}" for s in range(N_SLOTS) if w["srm"][base + s])
                                + (f"; urgent ADD {w['add'][d]:,.0f}" if w["add"][d] else ""))
                        elif key == "other":
                            base_note = ("SRM time without a truck: " + ", ".join(
                                f"{SLOT_LABELS[s]} {w['load'][base + s]:,.0f}" for s in others if w["load"][base + s]))
                        elif abs(q - srm) > 1e-9:
                            base_note = f"Load {q:,.0f} = SRM order {srm:,.0f}{moved_txt}" + (
                                f" (urgent ADD {w['add'][d]:,.0f} of the day spread over the trucks)" if w["add"][d] else "")
                        if changed_vs_prev:
                            cell.fill = fill(YELLOW)
                            cell.font = font(size=9.5, bold=True, color=BODY_TEXT)
                        elif key in ("pallet", "other"):
                            cell.fill = fill(ORANGE)
                        full_note = (chg_prefix + base_note).strip()
                        if full_note:
                            cell.comment = Comment(full_note, AUTHOR)
                    elif key == "pallet":
                        cell.fill = fill(ORANGE)
                        cell.comment = Comment("Once-a-day item (Pallet Truck). SRM time: " + ", ".join(
                            f"{SLOT_LABELS[s]} {w['srm'][base + s]:,.0f}" for s in range(N_SLOTS) if w["srm"][base + s])
                            + (f"; urgent ADD {w['add'][d]:,.0f}" if w["add"][d] else ""), AUTHOR)
                    elif key == "other":
                        cell.fill = fill(ORANGE)
                        cell.comment = Comment("SRM time without a truck: " + ", ".join(
                            f"{SLOT_LABELS[s]} {w['load'][base + s]:,.0f}" for s in others if w["load"][base + s]), AUTHOR)
                    elif abs(q - srm) > 1e-9:
                        cell.fill = fill(YELLOW)
                        cell.font = font(size=9.5, bold=True, color=BODY_TEXT)
                        cell.comment = Comment(f"Load {q:,.0f} = SRM order {srm:,.0f}{moved_txt}" + (
                            f" (urgent ADD {w['add'][d]:,.0f} of the day spread over the trucks)" if w["add"][d] else ""),
                            AUTHOR)
                ws.cell(row=row, column=spec["total"], value=day_sum or None)
                totals[spec["total"]] = totals.get(spec["total"], 0.0) + day_sum
            st_info = status_text(w)
            sc = ws.cell(row=row, column=status_col, value=st_info["text"])
            sc.font = font(size=9.0, bold=True, color=st_info["color"])
            sc.alignment = Alignment(horizontal="center", vertical="center")
            if st_info["fill"]:
                sc.fill = fill(st_info["fill"])
            is_sep = n > 0 and items[n - 1][1]["cls"] != "L" and w["cls"] == "L"
            top_side = mid if is_sep else thin
            for c in range(1, status_col + 1):
                cell = ws.cell(row=row, column=c)
                if c > len(lead) and c != status_col:
                    cell.number_format = NUM
                    cell.alignment = right
                    if cell.value is None:
                        cell.font = font(size=9.5, color=BODY_TEXT)
                if cell.fill.fgColor.rgb in (None, "00000000") and n % 2 == 1:
                    cell.fill = fill(ZEBRA)
                cell.border = Border(left=thin, right=thin, top=top_side, bottom=thin)
            row += 1
        last = row - 1
        for label, data, fmt in (("Total EA", totals, NUM), ("Total carts (boxes)", boxes, BOX_TOTAL_FMT)):
            ws.row_dimensions[row].height = 21
            lbl_cell = ws.cell(row=row, column=1, value=label)
            lbl_cell.font = font(size=9.5, bold=True, color=HDR_PRIMARY)
            lbl_cell.alignment = st["align_left"]
            for c, v in data.items():
                cell = ws.cell(row=row, column=c, value=round(v, 1) if v else None)
                cell.number_format = fmt
                cell.font = font(size=9.5, bold=True, color=HDR_PRIMARY)
                cell.alignment = right
            top_b = mid if label == "Total EA" else thin
            for c in range(1, status_col + 1):
                cell = ws.cell(row=row, column=c)
                cell.fill = fill(HDR_SUBTLE)
                if c > 1 and cell.value is None:
                    cell.font = font(size=9.5, bold=True, color=HDR_PRIMARY)
                cell.border = Border(left=thin, right=thin, top=top_b, bottom=thin)
            row += 1
        for rr in range(h2 + 1, row):
            for spec in cols:
                sc_first = ws.cell(row=rr, column=spec["start"])
                sc_first.border = Border(left=mid, right=sc_first.border.right, top=sc_first.border.top, bottom=sc_first.border.bottom)
                tc = ws.cell(row=rr, column=spec["total"])
                if rr <= last:
                    tc.fill = fill(META_BAND)
                    tc.font = font(size=9.5, bold=True, color=BODY_TEXT)
                tc.border = Border(left=mid, right=mid, top=tc.border.top, bottom=tc.border.bottom)
        for c, wdt in {1: 15, 2: 42, 3: 9, 4: 14}.items():
            ws.column_dimensions[L(c)].width = wdt
        for c in range(len(lead) + 1, status_col):
            ws.column_dimensions[L(c)].width = 10.5
        ws.column_dimensions[L(status_col)].width = 26
        ws.freeze_panes = ws.cell(row=h2 + 1, column=len(lead) + 1)
        ws.page_setup.orientation = "landscape"
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.print_title_rows = f"{h1}:{h2}"
        ws2 = wb.create_sheet("2-hour detail")
    else:
        ws2 = wb.active
        ws2.title = "2-hour detail"

    # ---------------------------------------------------------------- 2-hour detail
    lead = ["Part #", "Part name", "Box qty", start_label, ""]
    h1, h2 = 5, 6
    col = len(lead) + 1
    slot_cols: List[tuple] = []
    for d, day in enumerate(dates):
        first = col
        for s in range(N_SLOTS):
            ws2.cell(row=h2, column=col, value=f"{SLOT_LABELS[s]}\n{_slot_time(day, s)}")
            slot_cols.append(("slot", d, s, col))
            col += 1
        ws2.cell(row=h2, column=col, value="Day\ntotal")
        slot_cols.append(("total", d, 0, col))
        ws2.merge_cells(start_row=h1, start_column=first, end_row=h1, end_column=col)
        ws2.cell(row=h1, column=first, value=f"{day:%a} {day.month}/{day.day}")
        col += 1
    last_col = col - 1
    apply_title_and_legend_band(
        ws2,
        "2-hour plan detail",
        subtitle,
        [
            (HDR_SECONDARY, "Truck time"),
            (YELLOW, "Changed from SRM"),
            (PINK, "Additional - not yet in SRM"),
            (META_BAND, "Day total (not a time slot)"),
        ],
        [1, 5, 11, 18],
        last_col,
    )
    for c, h in enumerate(lead, 1):
        ws2.merge_cells(start_row=h1, start_column=c, end_row=h2, end_column=c)
        ws2.cell(row=h1, column=c, value=h)
    for rr in (h1, h2):
        for c in range(1, last_col + 1):
            cell = ws2.cell(row=rr, column=c)
            cell.fill = fill(HDR_PRIMARY)
            cell.font = font(size=9.5 if rr == h1 else 9.0, bold=True, color=WHITE)
            cell.alignment = center
            cell.border = grid_border
    for kind, d, s, c in slot_cols:
        cell = ws2.cell(row=h2, column=c)
        if kind == "total":
            cell.fill = fill(HDR_TOTAL)
            cell.border = Border(left=mid, right=mid, top=thin, bottom=thin)
        elif trucks and s in trucks:
            cell.fill = fill(HDR_SECONDARY)
        else:
            cell.fill = fill(HDR_SUBTLE)
            cell.font = font(size=9.0, bold=True, color=HDR_PRIMARY)
    ws2.row_dimensions[h1].height = 22
    ws2.row_dimensions[h2].height = 30
    row = h2 + 1
    for r, w in items:
        is_truck = bool(w["trucks"]) and w["cls"] != "L"
        main = "Truck load" if is_truck else ("Pallet Truck" if trucks else "Delivery")
        srm_with_add = list(w["srm"])
        for d in range(len(dates)):
            srm_with_add[d * N_SLOTS] += w["add"][d]
        lines = [(main, w["load"]), ("Requirement", w["req"]), ("Stock", w["stock"]), ("SRM order", srm_with_add)]
        first = row
        for name, vals in lines:
            ws2.row_dimensions[row].height = 18
            lbl_c = ws2.cell(row=row, column=5, value=name)
            lbl_c.font = font(size=9.0, bold=(name == main), color=HDR_PRIMARY if name == main else SUBTLE_TEXT)
            lbl_c.alignment = st["align_left"]
            lbl_c.border = grid_border
            for kind, d, s, c in slot_cols:
                day_vals = vals[d * N_SLOTS:(d + 1) * N_SLOTS]
                v = (day_vals[s] if kind == "slot" else (day_vals[-1] if name == "Stock" else sum(day_vals)))
                cell = ws2.cell(row=row, column=c, value=v if (v or name == "Stock") else None)
                cell.number_format = STOCK if name == "Stock" else NUM
                cell.alignment = right
                cell.border = grid_border
                if kind == "total":
                    cell.fill = fill(META_BAND)
                    cell.font = font(size=9.0, bold=True, color=BODY_TEXT)
                    cell.border = Border(left=mid, right=mid, top=thin, bottom=thin)
                    continue
                i = d * N_SLOTS + s
                if name == main:
                    cell.font = font(size=9.0, bold=True, color=HDR_PRIMARY)
                    if is_truck and s in w["trucks"]:
                        cell.fill = fill(LIGHT_BLUE)
                    if w["extra"][i] > 1e-9:
                        cell.fill = fill(PINK)
                        cell.font = font(size=9.0, bold=True, color=RED)
                        cell.comment = Comment(f"Additional {w['extra'][i]:,.0f} - not yet in SRM; {company} will confirm",
                                               AUTHOR)
                    elif v and abs(v - srm_with_add[i]) > 1e-9:
                        cell.fill = fill(YELLOW)
                elif name == "Stock":
                    is_neg = (v or 0) < 0
                    cell.font = font(size=9.0, bold=is_neg, color=RED if is_neg else STOCK_POS_TEXT)
                elif name == "SRM order":
                    cell.font = font(size=8.5, italic=True, color=MUTED_TEXT)
                else:
                    cell.font = font(size=9.0, color=BODY_TEXT)
            row += 1
        for c, v in enumerate([r["part"], r["name"], box_qty.get(r["key"]) or None, w["start"]], 1):
            ws2.merge_cells(start_row=first, start_column=c, end_row=row - 1, end_column=c)
            cell = ws2.cell(row=first, column=c, value=v)
            cell.font = font(size=9.5, bold=(c == 1), color=BODY_TEXT)
            cell.alignment = st["align_left_wrap"] if c == 2 else (st["align_left"] if c == 1 else right)
            cell.number_format = STOCK if c == 4 else (NUM if c == 3 else "@")
            for mr in range(first, row):
                ws2.cell(row=mr, column=c).border = grid_border
        for c in range(1, last_col + 1):
            cell = ws2.cell(row=row - 1, column=c)
            cell.border = Border(left=cell.border.left, right=cell.border.right, top=cell.border.top, bottom=mid)
    for c, wdt in {1: 15, 2: 34, 3: 8.5, 4: 12, 5: 13}.items():
        ws2.column_dimensions[L(c)].width = wdt
    for kind, d, s, c in slot_cols:
        ws2.column_dimensions[L(c)].width = 9.5 if kind == "total" else 7.8
    ws2.freeze_panes = ws2.cell(row=h2 + 1, column=len(lead) + 1)
    ws2.page_setup.orientation = "landscape"
    ws2.sheet_properties.pageSetUpPr.fitToPage = True
    ws2.page_setup.fitToWidth, ws2.page_setup.fitToHeight = 1, 0
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
