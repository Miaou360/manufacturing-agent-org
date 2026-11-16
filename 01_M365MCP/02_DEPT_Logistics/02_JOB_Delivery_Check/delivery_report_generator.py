"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - Excel Workbook Generator (#351)
===========================================================================
Generates `out/SRM_Delivery_Check_YYYYMMDD_HHMMSS.xlsx` + canonical copy
`out/SRM_Delivery_Check_latest.xlsx` (AGENTS.md §7), styled with
`02_DEPT_Logistics/report_excel_style.py` and strictly English-only (D111).

Sheets:
  1. `Delivery_TOP_6D`
     - Column A exact header : `Shortage (Receipt Basis)` (hybrid: closed trucks = actual GR,
       departed-but-open + future trucks = `Truck load`; 371 §3.4 & §3.5)
     - Column B exact header : `Runout (received only)` (.xlsb row-10 formula, future orders = 0;
       reference only, NEVER highlighted as an alarm; 371 §3.4)
     - Columns C..H          : `Vendor`, `Part #`, `Part Name`, `Box Qty`, `Start (SAP)`, `Row`
     - 40 timeline columns   : `ADD` + 3 days x (12 two-hour slots + `Day total`)
     - 6 rows per part       : `Order (Truck Plan)`, `Prod.Plan`, `Inv (Plan)`, `GR`, `GR-PO`, `GR Short (Hybrid)`
  2. `Truck_Scorecard`       : Per `(truck, part)` delivery verdict, `GR-PO`, `Shortage (Receipt Basis)`,
                               `Line Shortage Alarm`, `Buffer <15%`, and `Runout (received only)`.
  3. `Truck_Runs`            : High-level truck polling state machine overview.
"""
from __future__ import annotations

import datetime as dt
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DEPT_DIR = BASE_DIR.parent
MCP_ROOT = DEPT_DIR.parent
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"
INV_DEPT_DIR = MCP_ROOT / "01_DEPT_Inventory"

for _p in (str(BASE_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DEPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from report_excel_style import (  # noqa: E402
    AMBER,
    BODY_TEXT,
    BOX_TOTAL_FMT,
    GREEN,
    HDR_PRIMARY,
    HDR_SECONDARY,
    HDR_SUBTLE,
    HDR_TOTAL,
    META_BAND,
    MUTED_TEXT,
    NUM_FMT,
    PINK,
    RED,
    STOCK_FMT,
    STOCK_POS_TEXT,
    SUBTLE_TEXT,
    WHITE,
    YELLOW,
    ZEBRA_FILL,
    apply_title_and_legend_band,
    make_styles,
)
from report_labels import EXEMPT_RAW_DESC_HEADERS, find_hangul  # noqa: E402
from srm_env import company_name  # noqa: E402
from srm_parsers import N_HOURLY_DAYS, N_SLOTS, SLOT_LABELS  # noqa: E402
from srm_projection import ENOUGH, local_label, slot_start, timeline_6d  # noqa: E402

COL_A_HEADER = "Shortage (Receipt Basis)"
COL_B_HEADER = "Runout (received only)"
DETAIL_ROWS = (
    "Order (Truck Plan)",
    "Prod.Plan",
    "Inv (Plan)",
    "GR",
    "GR-PO",
    "GR Short (Hybrid)",
)


def _series_40(vals36: List[float], add_val: float = 0.0, *, is_stock: bool = False, start_val: float = 0.0) -> List[float]:
    if is_stock:
        out = [start_val + add_val]
        for d in range(N_HOURLY_DAYS):
            day_slice = vals36[d * N_SLOTS : (d + 1) * N_SLOTS]
            out += day_slice + [day_slice[-1]]
        return out
    out = [add_val]
    for d in range(N_HOURLY_DAYS):
        day_slice = vals36[d * N_SLOTS : (d + 1) * N_SLOTS]
        out += day_slice + [sum(day_slice) + (add_val if d == 0 else 0.0)]
    return out


def assert_workbook_no_hangul(wb) -> None:
    """Verifies zero Hangul across sheet names, headers, and non-exempt cells (D111)."""
    for ws in wb.worksheets:
        bad_title = find_hangul(ws.title)
        if bad_title:
            raise AssertionError(f"Hangul detected in sheet title '{ws.title}': {bad_title}")

        exempt_cols: set[int] = set()
        for r_idx in range(1, min(8, ws.max_row + 1)):
            for c_idx in range(1, ws.max_column + 1):
                val = ws.cell(row=r_idx, column=c_idx).value
                if isinstance(val, str) and val.strip().lower() in (
                    EXEMPT_RAW_DESC_HEADERS | {"part name"}
                ):
                    exempt_cols.add(c_idx)

        for r_idx in range(1, ws.max_row + 1):
            for c_idx in range(1, ws.max_column + 1):
                val = ws.cell(row=r_idx, column=c_idx).value
                if not isinstance(val, str):
                    continue
                if c_idx in exempt_cols and r_idx > 6:
                    continue
                bad = find_hangul(val)
                if bad:
                    raise AssertionError(
                        f"Hangul detected in {ws.title}!R{r_idx}C{c_idx} ('{val}'): {bad}"
                    )


def _company_label() -> str:
    try:
        return company_name()
    except RuntimeError:
        return "Plant Logistics"


def _fallback_day_summary_from_verdicts(
    run_date: dt.date,
    projections: Dict[str, Dict[str, Any]],
    trucks: List[Dict[str, Any]],
    verdicts: List[Dict[str, Any]],
    *,
    gr_available: bool = True,
) -> List[Dict[str, Any]]:
    truck_by_id = {t["truck_id"]: t for t in trucks}
    by_sp: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for v in verdicts:
        by_sp.setdefault((v["supplier_key"], v["part_key"]), []).append(v)

    rows: List[Dict[str, Any]] = []
    for (sup_key, pk), vlist in sorted(by_sp.items()):
        planned_day = round(sum(float(v["planned_pcs"]) for v in vlist), 2)
        received_day = round(sum(float(v["received_pcs"]) for v in vlist), 2)
        if planned_day <= 1e-9 and received_day <= 1e-9:
            continue
        first_v = vlist[0]
        t0 = truck_by_id.get(first_v["truck_id"], {})
        proj = projections.get(pk, {})
        trucks_planned = sum(1 for v in vlist if float(v["planned_pcs"]) > 1e-9)
        trucks_rcpt = sum(1 for v in vlist if float(v["received_pcs"]) > 1e-9)
        first_gr = min((v["first_gr_time"] for v in vlist if v.get("first_gr_time")), default=None)
        last_gr = max((v["last_gr_time"] for v in vlist if v.get("last_gr_time")), default=None)
        any_open = any(
            float(v["planned_pcs"]) > 1e-9
            and truck_by_id.get(v["truck_id"], {}).get("status")
            in ("SCHEDULED", "POLLING_30M", "RECEIVING_10M", "DAY_LEVEL_OPEN")
            for v in vlist
        )
        if not gr_available and any_open:
            day_v = "CANNOT_VERIFY"
        elif planned_day <= 1e-9:
            day_v = "UNPLANNED_RECEIPT"
        elif abs(received_day - planned_day) <= 1e-9:
            day_v = "DELIVERED"
        elif received_day > planned_day + 1e-9:
            day_v = "OVER"
        elif any_open:
            day_v = "IN_PROGRESS" if received_day > 1e-9 else "OPEN"
        else:
            day_v = "SHORT" if received_day > 1e-9 else "NOT_DELIVERED"
        rows.append(
            {
                "work_date": run_date.isoformat(),
                "supplier_key": sup_key,
                "vendor_display": t0.get("vendor_display") or proj.get("vendor_display") or sup_key,
                "part_key": pk,
                "part_no": first_v["part_no"],
                "part_name": first_v["part_name"],
                "cls": proj.get("cls") or "-",
                "box_qty": float(first_v.get("box_qty") or 0.0),
                "planned_day_pcs": planned_day,
                "received_day_pcs": received_day,
                "day_gr_po_pcs": round(received_day - planned_day, 2),
                "day_verdict": day_v,
                "trucks_planned": trucks_planned,
                "trucks_with_receipts": trucks_rcpt,
                "first_receipt_ts": first_gr,
                "last_receipt_ts": last_gr,
                "shortage_receipt_basis": first_v.get("shortage_receipt_basis", ENOUGH),
                "runout_received_only": first_v.get("runout_received_only", ENOUGH),
            }
        )
    return rows


def write_delivery_check_workbook(
    out_dir: Path,
    run_date: dt.date,
    as_of_ts: dt.datetime,
    projections: Dict[str, Dict[str, Any]],
    trucks: List[Dict[str, Any]],
    verdicts: List[Dict[str, Any]],
    *,
    gr_available: bool = True,
    day_part_summary: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Path, Path]:
    """
    Writes timestamped `SRM_Delivery_Check_YYYYMMDD_HHMMSS.xlsx` and updates
    `SRM_Delivery_Check_latest.xlsx` inside `out_dir` (never touches `00_JOB` files).
    """
    from openpyxl import Workbook
    from openpyxl.styles import Border
    from openpyxl.utils import get_column_letter as L

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    company = _company_label()
    st = make_styles()
    fill, font, thin, mid, grid_border = st["fill"], st["font"], st["thin"], st["mid"], st["grid_border"]
    center = st["align_center"]
    right = st["align_right"]
    left = st["align_left"]

    pre_live_note = (
        "   |   PRE-LIVE DRY RUN (suppliers did not receive truck plan before 2026-11-16; not a baseline)"
        if run_date < dt.date(2026, 11, 16)
        else ""
    )
    subtitle_1 = (
        f"{company}   |   Work Date: {run_date:%Y-%m-%d}   |   As of: {as_of_ts:%Y-%m-%d %H:%M} EST   |   "
        f"WMS GR Status: {'AVAILABLE' if gr_available else 'CANNOT_VERIFY (missing/locked)'}{pre_live_note}"
    )

    summary_rows = (
        day_part_summary
        if day_part_summary is not None
        else _fallback_day_summary_from_verdicts(
            run_date, projections, trucks, verdicts, gr_available=gr_available
        )
    )

    wb = Workbook()

    # =========================================================================
    # Sheet 1 (First Tab): Day_Part_Summary (373 Finding 1.3 & Finding 3)
    # =========================================================================
    ws0 = wb.active
    ws0.title = "Day_Part_Summary"
    dps_headers = [
        "Supplier",
        "Part #",
        "Part Name",
        "Class",
        "Box Qty",
        "Planned Day Total (PCS)",
        "Received Day Total (PCS)",
        "Day GR-PO (PCS)",
        "Day Verdict",
        "Trucks Planned",
        "Trucks w/ Receipts",
        "First Receipt Time",
        "Last Receipt Time",
        COL_A_HEADER,
        COL_B_HEADER,
    ]
    apply_title_and_legend_band(
        ws0,
        "Day Part Summary - Daily Planned vs Received Reconciliation (SRM Day Total)",
        subtitle_1,
        [
            (PINK, "NOT_DELIVERED / Active Hybrid Shortage"),
            (YELLOW, "SHORT / OVER / UNPLANNED_RECEIPT"),
            (META_BAND, "Pallet (Class L) is checked at day level (closes 05:59 next day)"),
        ],
        [1, 5, 10],
        len(dps_headers),
    )
    ws0.row_dimensions[5].height = 28
    for ci, h_txt in enumerate(dps_headers, start=1):
        c = ws0.cell(row=5, column=ci, value=h_txt)
        c.fill = fill(HDR_TOTAL if ci == len(dps_headers) else HDR_PRIMARY)
        c.font = font(size=9.5, bold=True, color=WHITE)
        c.alignment = center
        c.border = grid_border

    for n, sr in enumerate(summary_rows):
        rr = 6 + n
        ws0.row_dimensions[rr].height = 19
        vals0 = [
            sr["vendor_display"],
            sr["part_no"],
            sr["part_name"],
            "Pallet (L)" if sr["cls"] == "L" else sr["cls"],
            sr["box_qty"] or None,
            sr["planned_day_pcs"],
            sr["received_day_pcs"],
            sr["day_gr_po_pcs"],
            sr["day_verdict"],
            sr["trucks_planned"],
            sr["trucks_with_receipts"],
            (sr["first_receipt_ts"] or "-")[:16],
            (sr["last_receipt_ts"] or "-")[:16],
            sr["shortage_receipt_basis"],
            sr["runout_received_only"],
        ]
        for ci, val in enumerate(vals0, start=1):
            cell = ws0.cell(row=rr, column=ci, value=val)
            cell.border = grid_border
            if n % 2 == 1:
                cell.fill = fill(ZEBRA_FILL)
            if ci in (5, 6, 7, 8, 10, 11):
                cell.alignment = right
                cell.number_format = STOCK_FMT if ci == 8 else NUM_FMT
                cell.font = font(size=9.0, bold=(ci in (6, 7, 8)), color=BODY_TEXT)
            elif ci == 9:
                cell.alignment = center
                dv = str(val)
                if dv == "NOT_DELIVERED":
                    cell.fill = fill(PINK)
                    cell.font = font(size=9.0, bold=True, color=RED)
                elif dv in ("SHORT", "OVER", "UNPLANNED_RECEIPT"):
                    cell.fill = fill(YELLOW)
                    cell.font = font(size=9.0, bold=True, color=AMBER)
                elif dv == "DELIVERED":
                    cell.font = font(size=9.0, bold=True, color=GREEN)
                else:
                    cell.font = font(size=9.0, color=SUBTLE_TEXT)
            elif ci == 14:
                cell.alignment = center
                is_sh = val not in (ENOUGH, "CANNOT_VERIFY", "", None)
                if is_sh:
                    cell.fill = fill(PINK)
                cell.font = font(size=9.0, bold=is_sh, color=RED if is_sh else GREEN)
            elif ci == 15:
                cell.alignment = center
                cell.fill = fill(META_BAND)
                cell.font = font(size=8.5, italic=True, color=MUTED_TEXT)
            else:
                cell.alignment = left if ci in (1, 2, 3) else center
                cell.font = font(size=9.0, bold=(ci == 2), color=BODY_TEXT)

    dps_widths = (26, 15, 38, 11, 9, 16, 16, 14, 18, 12, 14, 17, 17, 22, 20)
    for ci, wdt in enumerate(dps_widths, start=1):
        ws0.column_dimensions[L(ci)].width = wdt
    ws0.freeze_panes = "D6"

    # =========================================================================
    # Sheet 2: Delivery_TOP_6D
    # =========================================================================
    ws1 = wb.create_sheet("Delivery_TOP_6D")

    tl = timeline_6d()
    lead_cols = [
        COL_A_HEADER,
        COL_B_HEADER,
        "Vendor",
        "Part #",
        "Part Name",
        "Box Qty",
        "Start (SAP)",
        "Row",
    ]
    total_cols = len(lead_cols) + len(tl)
    apply_title_and_legend_band(
        ws1,
        "SRM Delivery Check - 2-Hour Receipt & Hybrid Shortage Projection (3 Days)",
        subtitle_1,
        [
            (PINK, "Shortage (Receipt Basis): hybrid stock < 0 (closed trucks = actual GR; open/future = Truck load)"),
            (META_BAND, "Runout (received only): reference w/ future orders = 0 (never triggers alarms)"),
            (YELLOW, "GR-PO variance on closed/active slots"),
        ],
        [1, 8, 18],
        total_cols,
    )

    h1, h2 = 5, 6
    for idx, hdr_name in enumerate(lead_cols, start=1):
        ws1.merge_cells(start_row=h1, start_column=idx, end_row=h2, end_column=idx)
        ws1.cell(row=h1, column=idx, value=hdr_name)

    for j, (kind, d, s) in enumerate(tl):
        c = len(lead_cols) + 1 + j
        if kind == "add":
            ws1.cell(row=h1, column=c, value=f"{run_date.month}/{run_date.day}")
            ws1.cell(row=h2, column=c, value="ADD")
        elif kind == "slot":
            ws1.cell(row=h1, column=c, value=local_label(slot_start(run_date, d, s)))
            ws1.cell(row=h2, column=c, value=SLOT_LABELS[s])
        else:
            dd = run_date + dt.timedelta(days=d)
            ws1.cell(row=h1, column=c, value=f"Day {d + 1}")
            ws1.cell(row=h2, column=c, value=f"{dd.month}/{dd.day}\ntotal")

    for rr in (h1, h2):
        ws1.row_dimensions[rr].height = 24 if rr == h1 else 28
        for c in range(1, total_cols + 1):
            cell = ws1.cell(row=rr, column=c)
            is_ref_col = c == 2
            cell.fill = fill(HDR_TOTAL if is_ref_col else (HDR_PRIMARY if (rr == h1 or c <= len(lead_cols)) else HDR_SECONDARY))
            cell.font = font(size=9.5 if rr == h1 else 9.0, bold=True, color=WHITE)
            cell.alignment = center
            cell.border = grid_border

    sorted_parts = sorted(
        projections.values(),
        key=lambda p: (
            0 if p["shortage_receipt_basis"] != ENOUGH else 1,
            p["vendor_display"],
            p["part_no"],
        ),
    )

    row_cursor = h2 + 1
    for p in sorted_parts:
        short_rcpt = p["shortage_receipt_basis"] if gr_available else "CANNOT_VERIFY"
        runout_ro = p["runout_received_only"] if gr_available else "CANNOT_VERIFY"
        start_sap = p["start_sap"]

        row_payloads = [
            ("Order (Truck Plan)", _series_40(p["load_36"], 0.0)),
            ("Prod.Plan", _series_40(p["req_36"], 0.0)),
            ("Inv (Plan)", _series_40(p["plan_stock_36"], 0.0, is_stock=True, start_val=start_sap)),
            ("GR", _series_40(p["actual_gr_36"], 0.0)),
            ("GR-PO", _series_40(p["gr_po_36"], 0.0)),
            ("GR Short (Hybrid)", _series_40(p["hybrid_stock_36"], 0.0, is_stock=True, start_val=start_sap)),
        ]

        for r_label, vals40 in row_payloads:
            ws1.row_dimensions[row_cursor].height = 18
            lead_vals = [
                short_rcpt,
                runout_ro,
                p["vendor_display"],
                p["part_no"],
                p["part_name"],
                p["box_qty"] or None,
                start_sap,
                r_label,
            ]
            for ci, lv in enumerate(lead_vals, start=1):
                cell = ws1.cell(row=row_cursor, column=ci, value=lv)
                cell.border = grid_border
                if ci == 1:
                    is_short = lv not in (ENOUGH, "CANNOT_VERIFY", "", None)
                    cell.alignment = center
                    cell.font = font(size=9.0, bold=is_short, color=RED if is_short else GREEN)
                    if is_short:
                        cell.fill = fill(PINK)
                elif ci == 2:
                    # Reference-only column: muted italic text, NEVER red/pink alarm fill
                    cell.alignment = center
                    cell.fill = fill(META_BAND)
                    cell.font = font(size=8.5, italic=True, color=MUTED_TEXT)
                elif ci in (3, 4, 5, 8):
                    cell.alignment = left
                    cell.font = font(size=9.0, bold=(ci == 4 or r_label == "GR Short (Hybrid)"), color=BODY_TEXT)
                else:
                    cell.alignment = right
                    cell.number_format = STOCK_FMT if ci == 7 else NUM_FMT
                    cell.font = font(size=9.0, color=BODY_TEXT)

            is_stock_row = r_label in ("Inv (Plan)", "GR Short (Hybrid)")
            for j, v in enumerate(vals40):
                c = len(lead_cols) + 1 + j
                kind, _, _ = tl[j]
                cell = ws1.cell(row=row_cursor, column=c, value=v if (is_stock_row or abs(v) > 1e-9) else None)
                cell.number_format = STOCK_FMT if is_stock_row else NUM_FMT
                cell.alignment = right
                cell.border = grid_border
                if kind == "total":
                    cell.fill = fill(META_BAND)
                    cell.font = font(size=9.0, bold=True, color=BODY_TEXT)
                elif r_label == "GR Short (Hybrid)" and v < -1e-9:
                    cell.fill = fill(PINK)
                    cell.font = font(size=9.0, bold=True, color=RED)
                elif r_label == "GR-PO" and abs(v) > 1e-9 and j <= N_SLOTS:
                    cell.fill = fill(YELLOW)
                    cell.font = font(size=9.0, bold=True, color=RED if v < 0 else BODY_TEXT)
                else:
                    cell.font = font(size=9.0, color=STOCK_POS_TEXT if is_stock_row else BODY_TEXT)

            if r_label == "GR Short (Hybrid)":
                for c in range(1, total_cols + 1):
                    cur_b = ws1.cell(row=row_cursor, column=c).border
                    ws1.cell(row=row_cursor, column=c).border = Border(
                        left=cur_b.left, right=cur_b.right, top=cur_b.top, bottom=mid
                    )
            row_cursor += 1

    for ci, wdt in enumerate((22, 20, 26, 15, 38, 9, 12, 18), start=1):
        ws1.column_dimensions[L(ci)].width = wdt
    for j in range(len(tl)):
        ws1.column_dimensions[L(len(lead_cols) + 1 + j)].width = 8.5
    ws1.freeze_panes = ws1.cell(row=h2 + 1, column=len(lead_cols) + 1)

    # =========================================================================
    # Sheet 3: Truck_Scorecard
    # =========================================================================
    ws2 = wb.create_sheet("Truck_Scorecard")
    sc_headers = [
        "Truck ID",
        "Supplier",
        "Truck Slot",
        "Departure",
        "Truck Status",
        "Closed At",
        "Part #",
        "Part Name",
        "Box Qty",
        "Planned Boxes",
        "Planned PCS",
        "Received Boxes",
        "Received PCS",
        "GR-PO (PCS)",
        "Matched Barcodes",
        "Verdict",
        "Open Shortfall (PCS)",
        "Schedule / Recovery Note",
        COL_A_HEADER,
        "Line Shortage Alarm",
        "Buffer <15%",
        COL_B_HEADER,
    ]
    apply_title_and_legend_band(
        ws2,
        "Truck Delivery Scorecard - Per Truck & Planned Part",
        subtitle_1,
        [
            (PINK, "LINE_SHORTAGE_ALARM / NOT_DELIVERED / TRUCK_NOT_RECEIVED"),
            (YELLOW, "SHORT / OVER / OFF_SCHEDULE / UNPLANNED_RECEIPT"),
            (META_BAND, "Runout (received only) - reference only"),
        ],
        [1, 8, 16],
        len(sc_headers),
    )

    hr = 5
    ws2.row_dimensions[hr].height = 28
    for ci, h_txt in enumerate(sc_headers, start=1):
        c = ws2.cell(row=hr, column=ci, value=h_txt)
        c.fill = fill(HDR_TOTAL if ci == len(sc_headers) else HDR_PRIMARY)
        c.font = font(size=9.5, bold=True, color=WHITE)
        c.alignment = center
        c.border = grid_border

    truck_by_id = {t["truck_id"]: t for t in trucks}
    vendor_by_sup = {t["supplier_key"]: t["vendor_display"] for t in trucks}
    r_idx = hr + 1
    for n, v in enumerate(verdicts):
        ws2.row_dimensions[r_idx].height = 19
        t = truck_by_id.get(v["truck_id"], {})
        is_off_win = v["truck_label"] == "OFF_WINDOW"
        is_pallet = bool(t.get("is_pallet_truck"))
        slot_disp = (
            f"PALLET (Day / {SLOT_LABELS[int(t['slot_idx'])]})"
            if is_pallet
            else v["truck_label"]
        )
        dep_disp = (
            "-"
            if is_off_win
            else (
                f"{(t.get('departure_ts') or '')[:16]} (Day-level)"
                if is_pallet
                else (t.get("departure_ts") or "")[:16]
            )
        )
        alarm_txt = (
            f"YES ({v['first_short_slot_label']}, -{v['deficit_before_next_truck_pcs']:,.0f} EA)"
            if v.get("line_shortage_alarm")
            else ("CLEARED" if v.get("initial_alarm_at_close") else "NO")
        )
        buf_txt = "LOW (<15%)" if v.get("buffer_low_15pct") else "OK"
        row_vals = [
            v["truck_id"],
            t.get("vendor_display") or vendor_by_sup.get(v["supplier_key"], v["supplier_key"]),
            slot_disp,
            dep_disp,
            "OFF_WINDOW" if is_off_win else (t.get("status") or ""),
            (t.get("closed_ts") or "-")[:16],
            v["part_no"],
            v["part_name"],
            v["box_qty"] or None,
            v["planned_boxes"],
            v["planned_pcs"],
            v["received_boxes"],
            v["received_pcs"],
            v["diff_pcs"],
            v["matched_barcodes_count"],
            v["verdict"],
            float(v.get("open_shortfall_pcs") or 0.0),
            v.get("recovery_note") or "-",
            v["shortage_receipt_basis"],
            alarm_txt,
            buf_txt,
            v["runout_received_only"],
        ]
        for ci, val in enumerate(row_vals, start=1):
            cell = ws2.cell(row=r_idx, column=ci, value=val)
            cell.border = grid_border
            if n % 2 == 1:
                cell.fill = fill(ZEBRA_FILL)
            if ci in (9, 11, 13, 14, 15, 17):
                cell.alignment = right
                cell.number_format = STOCK_FMT if ci == 14 else NUM_FMT
                cell.font = font(size=9.0, color=BODY_TEXT)
            elif ci == 10 or ci == 12:
                cell.alignment = right
                cell.number_format = BOX_TOTAL_FMT if ci == 10 else NUM_FMT
                cell.font = font(size=9.0, color=BODY_TEXT)
            elif ci == 16:
                cell.alignment = center
                vrd = str(val)
                if vrd in ("NOT_DELIVERED", "TRUCK_NOT_RECEIVED"):
                    cell.fill = fill(PINK)
                    cell.font = font(size=9.0, bold=True, color=RED)
                elif vrd in ("SHORT", "OVER", "OFF_SCHEDULE", "UNPLANNED_RECEIPT"):
                    cell.fill = fill(YELLOW)
                    cell.font = font(size=9.0, bold=True, color=AMBER)
                elif vrd == "DELIVERED":
                    cell.font = font(size=9.0, bold=True, color=GREEN)
                else:
                    cell.font = font(size=9.0, color=SUBTLE_TEXT)
            elif ci == 18:
                cell.alignment = left
                cell.font = font(size=8.5, color=GREEN if str(val).startswith(("recovered", "received")) else SUBTLE_TEXT)
            elif ci == 19:
                cell.alignment = center
                is_sh = val not in (ENOUGH, "CANNOT_VERIFY", "", None)
                if is_sh:
                    cell.fill = fill(PINK)
                cell.font = font(size=9.0, bold=is_sh, color=RED if is_sh else GREEN)
            elif ci == 20:
                cell.alignment = center
                if v.get("line_shortage_alarm"):
                    cell.fill = fill(PINK)
                    cell.font = font(size=9.0, bold=True, color=RED)
                elif val == "CLEARED":
                    cell.font = font(size=9.0, bold=True, color=GREEN)
                else:
                    cell.font = font(size=9.0, color=SUBTLE_TEXT)
            elif ci == 21:
                cell.alignment = center
                if v.get("buffer_low_15pct"):
                    cell.fill = fill(YELLOW)
                    cell.font = font(size=9.0, bold=True, color=AMBER)
                else:
                    cell.font = font(size=9.0, color=SUBTLE_TEXT)
            elif ci == 22:
                cell.alignment = center
                cell.fill = fill(META_BAND)
                cell.font = font(size=8.5, italic=True, color=MUTED_TEXT)
            else:
                cell.alignment = left if ci in (1, 2, 7, 8) else center
                cell.font = font(size=9.0, bold=(ci == 7), color=BODY_TEXT)
        r_idx += 1

    sc_widths = (28, 24, 16, 24, 18, 16, 15, 36, 9, 12, 12, 12, 12, 12, 14, 18, 16, 30, 22, 24, 13, 20)
    for ci, wdt in enumerate(sc_widths, start=1):
        ws2.column_dimensions[L(ci)].width = wdt
    ws2.freeze_panes = "I6"

    # =========================================================================
    # Sheet 4: Truck_Runs
    # =========================================================================
    ws3 = wb.create_sheet("Truck_Runs")
    tr_headers = [
        "Truck ID",
        "Supplier",
        "Truck Slot",
        "Departure",
        "Status",
        "Planned Parts",
        "Planned Boxes",
        "Planned PCS",
        "Received Boxes",
        "Received PCS",
        "Quiet Polls",
        "First Receipt",
        "Last Receipt",
        "Closed At",
        "Next Poll Due",
        "Close / No-Arrival Cap",
    ]
    apply_title_and_legend_band(
        ws3,
        "Truck Polling State Machine Overview",
        subtitle_1,
        [
            (HDR_SECONDARY, "C-Trucks: POLLING_30M -> RECEIVING_10M (2 quiet polls) | Pallet: DAY_LEVEL_OPEN -> CLOSED_DAY_LEVEL (05:59)"),
        ],
        [1],
        len(tr_headers),
    )
    ws3.row_dimensions[5].height = 26
    for ci, h_txt in enumerate(tr_headers, start=1):
        c = ws3.cell(row=5, column=ci, value=h_txt)
        c.fill = fill(HDR_PRIMARY)
        c.font = font(size=9.5, bold=True, color=WHITE)
        c.alignment = center
        c.border = grid_border

    for n, t in enumerate(trucks):
        rr = 6 + n
        ws3.row_dimensions[rr].height = 19
        is_pallet = bool(t.get("is_pallet_truck"))
        slot_lbl = f"PALLET (Day / {SLOT_LABELS[int(t['slot_idx'])]})" if is_pallet else t["truck_label"]
        dep_lbl = f"{t['departure_ts'][:16]} (Day-level)" if is_pallet else t["departure_ts"][:16]
        vals = [
            t["truck_id"],
            t["vendor_display"],
            slot_lbl,
            dep_lbl,
            t["status"],
            t["planned_parts_count"],
            t["planned_boxes_total"],
            t["planned_pcs_total"],
            t["received_boxes_total"],
            t["received_pcs_total"],
            t["quiet_polls"],
            (t["first_receipt_ts"] or "-")[:16],
            (t["last_receipt_ts"] or "-")[:16],
            (t["closed_ts"] or "-")[:16],
            (t["next_poll_due_ts"] or "-")[:16],
            t["no_arrival_deadline_ts"][:16],
        ]
        for ci, val in enumerate(vals, start=1):
            cell = ws3.cell(row=rr, column=ci, value=val)
            cell.border = grid_border
            if n % 2 == 1:
                cell.fill = fill(ZEBRA_FILL)
            cell.alignment = right if 6 <= ci <= 11 else (left if ci <= 2 else center)
            if ci in (6, 8, 9, 10, 11):
                cell.number_format = NUM_FMT
            elif ci == 7:
                cell.number_format = BOX_TOTAL_FMT
            cell.font = font(size=9.0, bold=(ci == 5), color=RED if val == "TRUCK_NOT_RECEIVED" else BODY_TEXT)

    for ci, wdt in enumerate((28, 26, 17, 25, 18, 12, 12, 12, 12, 12, 11, 16, 16, 16, 16, 20), start=1):
        ws3.column_dimensions[L(ci)].width = wdt
    ws3.freeze_panes = "F6"

    assert_workbook_no_hangul(wb)

    stamped_path = out_dir / f"SRM_Delivery_Check_{as_of_ts:%Y%m%d_%H%M%S}.xlsx"
    latest_path = out_dir / "SRM_Delivery_Check_latest.xlsx"
    wb.save(stamped_path)
    shutil.copyfile(stamped_path, latest_path)
    return stamped_path, latest_path
