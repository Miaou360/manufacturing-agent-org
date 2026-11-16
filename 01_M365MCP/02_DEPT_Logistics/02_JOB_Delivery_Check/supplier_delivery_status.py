"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - Supplier-Facing Delivery Status Workbook (#387)
===========================================================================================
Generates `SRM_DeliveryStatus_<Supplier>_<YYYYMMDD>_<HHMM>.xlsx` (single sheet `Truck plan`,
reusing `00_JOB_SRM_Monitoring/srm_supplier_files.py` & `report_excel_style.py` building blocks):
  - Two rows per part:
      * `Plan`      = truck load from the 00_JOB plan sent to the supplier.
      * `Delivered` = actual WMS receipts attributed to each closed truck (`0` if nothing;
        incl. `OFF_SCHEDULE`), `–` (`\u2013`) on trucks not closed yet, and day-level received
        so far on `Pallet Truck`.
  - Colour on `Delivered`: below `Plan` = red fill (`PINK`), above `Plan` = blue fill (`LIGHT_BLUE`),
    equal = no semantic fill. Day total on both rows.
  - Last column `Warning` (on `Delivered` row only): when hybrid stock goes `< 0` before the next
    truck carrying the part arrives (and a delivery shortfall contributed), writes:
      `Need N pcs (B boxes) more before the <time> truck (short from <time>)`
    rounded up to full boxes (`Box_QTY`). Empty when covered by next truck or no shortfall.
  - Start-stock guard: parts missing from morning `ZM212` (`start_in_zm212 == False`) NEVER
    produce a supplier warning (surfaced only in the user's internal mail as
    `start stock not in ZM212 – check`).
"""
from __future__ import annotations

import datetime as dt
import math
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

from delivery_projection_engine import CLOSED_TRUCK_STATES, OPEN_TRUCK_STATES  # noqa: E402
from report_excel_style import (  # noqa: E402
    BODY_TEXT,
    BOX_TOTAL_FMT,
    HDR_PRIMARY,
    HDR_SECONDARY,
    HDR_SUBTLE,
    HDR_TOTAL,
    LIGHT_BLUE,
    META_BAND,
    MUTED_TEXT,
    NUM_FMT,
    ORANGE,
    PINK,
    RED,
    STOCK_FMT,
    SUBTLE_TEXT,
    WHITE,
    ZEBRA_FILL,
    apply_title_and_legend_band,
    make_styles,
)
from report_labels import assert_no_hangul  # noqa: E402
from srm_env import company_name  # noqa: E402
from srm_parsers import N_SLOTS, SLOT_LABELS  # noqa: E402
from srm_supplier_files import _safe, _slot_time  # noqa: E402

OPEN_DASH = "\u2013"
MISSING_ZM212_INTERNAL_LABEL = "start stock not in ZM212 \u2013 check"


def _company_str() -> str:
    try:
        return company_name()
    except RuntimeError:
        return "Plant Logistics"


def delivery_status_filename(vendor: str, run_date: dt.date, as_of_ts: dt.datetime) -> str:
    return f"SRM_DeliveryStatus_{_safe(vendor)}_{run_date:%Y%m%d}_{as_of_ts:%H%M}.xlsx"


def compute_part_supplier_warning(
    proj: Dict[str, Any],
    *,
    sup_trucks: List[Dict[str, Any]],
    planned_day0_by_slot: Dict[int, float],
    closed_received_day0: float,
    closed_planned_day0: float,
) -> Dict[str, Any]:
    """
    Evaluates whether a delivery shortfall on closed C-truck(s) drives `hybrid_stock_36 < 0`
    before the next unclosed/future truck carrying this part arrives (#387).
    Choice for Class L (`Pallet Truck`): Pallet has no intraday close event (`DAY_LEVEL_OPEN`
    until 05:59 next morning), so intraday C-truck closes only warn on Class C items.
    """
    cls_code = proj.get("cls") or "C"
    if cls_code == "L":
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    closed_c = [t for t in sup_trucks if not t.get("is_pallet_truck") and t["status"] in CLOSED_TRUCK_STATES]
    if not closed_c:
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    first_closed_slot = min(int(t["slot_idx"]) for t in closed_c)
    load_36: List[float] = proj["load_36"]
    hybrid_36: List[float] = proj["hybrid_stock_36"]
    plan_36: List[float] = proj["plan_stock_36"]
    labels_36: List[str] = proj["slot_labels_36"]
    as_of_slot = int(proj.get("as_of_slot", 0))
    box_qty = float(proj.get("box_qty") or 0.0)
    start_in_zm212 = bool(proj.get("start_in_zm212", True))

    future_carrying_slots: List[int] = []
    for t in sup_trucks:
        if not t.get("is_pallet_truck") and t["status"] in OPEN_TRUCK_STATES:
            s_i = int(t["slot_idx"])
            if planned_day0_by_slot.get(s_i, 0.0) > 1e-9:
                future_carrying_slots.append(s_i)
    for s_i in range(N_SLOTS, 3 * N_SLOTS):
        if load_36[s_i] > 1e-9:
            future_carrying_slots.append(s_i)

    s_next = min(future_carrying_slots) if future_carrying_slots else (3 * N_SLOTS)
    active_start = max(0, first_closed_slot, min(as_of_slot, max(0, s_next - 1)))
    if s_next <= active_start:
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    gap_slots = list(range(active_start, s_next))
    neg_slots = [u for u in gap_slots if hybrid_36[u] < -1e-9]
    if not neg_slots:
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    min_hybrid = min(hybrid_36[u] for u in gap_slots)
    min_plan = min(plan_36[u] for u in gap_slots)
    shortfall_degraded = (closed_received_day0 < closed_planned_day0 - 1e-9) or (min_hybrid < min_plan - 1e-9)
    if not shortfall_degraded:
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    first_neg = neg_slots[0]
    while first_neg > first_closed_slot and hybrid_36[first_neg - 1] < -1e-9:
        first_neg -= 1

    raw_need = -min(hybrid_36[u] for u in range(first_neg, s_next))
    if raw_need <= 1e-9:
        return {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

    if box_qty > 1e-9:
        boxes_need = int(math.ceil(raw_need / box_qty - 1e-9))
        pcs_need = float(boxes_need * box_qty)
        box_word = "box" if boxes_need == 1 else "boxes"
        qty_phrase = f"{pcs_need:,.0f} pcs ({boxes_need:,d} {box_word})"
    else:
        boxes_need = 0
        pcs_need = float(math.ceil(raw_need - 1e-9))
        qty_phrase = f"{pcs_need:,.0f} pcs"

    short_from_lbl = labels_36[first_neg]
    if s_next < 3 * N_SLOTS:
        next_truck_lbl = labels_36[s_next]
        raw_msg = f"Need {qty_phrase} more before the {next_truck_lbl} truck (short from {short_from_lbl})"
    else:
        next_truck_lbl = "end of 3-day plan"
        raw_msg = f"Need {qty_phrase} more before end of 3-day plan (short from {short_from_lbl})"

    if not start_in_zm212:
        return {
            "warning_text": "",
            "suppressed_missing_zm212": True,
            "internal_note": MISSING_ZM212_INTERNAL_LABEL,
            "raw_warning_if_unfiltered": raw_msg,
            "pcs_needed": pcs_need,
            "boxes_needed": boxes_need,
            "next_truck_label": next_truck_lbl,
            "short_from_label": short_from_lbl,
        }

    return {
        "warning_text": raw_msg,
        "suppressed_missing_zm212": False,
        "pcs_needed": pcs_need,
        "boxes_needed": boxes_need,
        "next_truck_label": next_truck_lbl,
        "short_from_label": short_from_lbl,
    }


def build_supplier_delivery_status(
    run_date: dt.date,
    as_of_ts: dt.datetime,
    supplier_key: str,
    horizon_parts: Dict[str, Dict[str, Any]],
    trucks: List[Dict[str, Any]],
    plan_lines: List[Dict[str, Any]],
    barcodes: List[Dict[str, Any]],
    projections: Dict[str, Dict[str, Any]],
    trucks_cfg: Dict[str, List[int]],
) -> Optional[Dict[str, Any]]:
    """Builds the structured 3-day `Plan` / `Delivered` + `Warning` model for one configured truck supplier."""
    fixed_slots = list(trucks_cfg.get(supplier_key) or [])
    if not fixed_slots:
        return None

    sup_trucks = [t for t in trucks if t["supplier_key"] == supplier_key]
    sup_parts_raw = [hp for hp in horizon_parts.values() if hp["supplier_key"] == supplier_key]
    if not sup_parts_raw:
        return None

    vendor_display = sup_trucks[0]["vendor_display"] if sup_trucks else sup_parts_raw[0]["vendor_display"]
    dates = [run_date + dt.timedelta(days=d) for d in range(3)]
    as_of_str = as_of_ts.strftime("%Y-%m-%d %H:%M:%S")

    truck_by_slot: Dict[int, Dict[str, Any]] = {
        int(t["slot_idx"]): t for t in sup_trucks if not t.get("is_pallet_truck")
    }
    pallet_truck = next((t for t in sup_trucks if t.get("is_pallet_truck")), None)
    off_window_tid = f"{run_date:%Y%m%d}_{supplier_key}_OFF_WINDOW"

    slot_is_closed: Dict[int, bool] = {}
    closed_labels: List[str] = []
    for s_i in fixed_slots:
        t_row = truck_by_slot.get(s_i)
        if t_row is not None:
            is_c = (t_row["status"] in CLOSED_TRUCK_STATES) or (
                t_row["status"] == "NO_PLANNED_LOAD" and as_of_str >= str(t_row.get("no_arrival_deadline_ts") or "9999")
            )
            slot_is_closed[s_i] = is_c
            if t_row["status"] in CLOSED_TRUCK_STATES:
                closed_labels.append(f"{SLOT_LABELS[s_i]} ({_slot_time(run_date, s_i)})")
        else:
            slot_is_closed[s_i] = False

    plan_line_map: Dict[Tuple[str, int, int], float] = {}
    for ln in plan_lines:
        if ln["supplier_key"] == supplier_key:
            plan_line_map[(ln["part_key"], int(ln["slot_idx"]), int(bool(ln["cls"] == "L")))] = float(ln["planned_pcs"])

    bcs_by_tp: Dict[Tuple[str, str], float] = {}
    bcs_by_part: Dict[str, List[Dict[str, Any]]] = {}
    for b in barcodes:
        if b.get("supplier_key") != supplier_key:
            continue
        pk = b["part_key"]
        q = float(b["gr_qty"])
        bcs_by_part.setdefault(pk, []).append(b)
        tid = b.get("assigned_truck_id")
        if tid:
            bcs_by_tp[(tid, pk)] = bcs_by_tp.get((tid, pk), 0.0) + q

    others_slots = [s for s in range(N_SLOTS) if s not in fixed_slots]

    part_entries: List[Dict[str, Any]] = []
    warnings_out: List[Dict[str, Any]] = []
    suppressed_zm212_out: List[Dict[str, Any]] = []

    sorted_hp = sorted(sup_parts_raw, key=lambda h: (h["cls"] == "L", h["part_no"]))
    for hp in sorted_hp:
        pk = hp["part_key"]
        cls_code = hp["cls"] or "C"
        load_36 = list(hp["load_36"][: 3 * N_SLOTS])
        req_36 = list(hp["req_36"][: 3 * N_SLOTS])
        srm_36 = list(hp["srm_36"][: 3 * N_SLOTS])
        extra_36 = list(hp["extra_36"][: 3 * N_SLOTS])
        add_3 = list(hp["add_3"][:3])
        part_bcs = bcs_by_part.get(pk, [])

        active = (
            any(load_36)
            or any(req_36)
            or any(srm_36)
            or any(add_3)
            or any(extra_36)
            or bool(part_bcs)
            or cls_code == "C"
        )
        if not active:
            continue

        days_data: List[Dict[str, Any]] = []
        planned_day0_slots: Dict[int, float] = {}
        closed_rec_d0 = 0.0
        closed_plan_d0 = 0.0

        for d in range(3):
            base = d * N_SLOTS
            if cls_code == "L":
                if d == 0:
                    p_pal = plan_line_map.get((pk, 0, 1), sum(load_36[base : base + N_SLOTS]))
                    d_pal: Any = sum(float(b["gr_qty"]) for b in part_bcs)
                    pal_closed = bool(pallet_truck and pallet_truck["status"] in CLOSED_TRUCK_STATES)
                else:
                    p_pal = sum(load_36[base : base + N_SLOTS])
                    d_pal = OPEN_DASH
                    pal_closed = False
                days_data.append({
                    "truck_plan": {},
                    "truck_deliv": {},
                    "pallet_plan": p_pal,
                    "pallet_deliv": d_pal,
                    "pallet_closed": pal_closed,
                    "other_plan": 0.0,
                    "other_deliv": OPEN_DASH if d > 0 else 0.0,
                    "total_plan": p_pal,
                    "total_deliv": d_pal if d == 0 else OPEN_DASH,
                    "all_planned_closed": pal_closed if p_pal > 1e-9 else (d == 0),
                })
            else:
                t_plan: Dict[int, float] = {}
                t_deliv: Dict[int, Any] = {}
                for t_s in fixed_slots:
                    if d == 0:
                        q_p = plan_line_map.get((pk, t_s, 0), float(load_36[base + t_s]))
                        planned_day0_slots[t_s] = q_p
                        t_row = truck_by_slot.get(t_s)
                        tid = t_row["truck_id"] if t_row else f"{run_date:%Y%m%d}_{supplier_key}_{SLOT_LABELS[t_s]}"
                        if slot_is_closed.get(t_s, False):
                            q_d = float(bcs_by_tp.get((tid, pk), 0.0))
                            t_deliv[t_s] = q_d
                            closed_rec_d0 += q_d
                            closed_plan_d0 += q_p
                        else:
                            t_deliv[t_s] = OPEN_DASH
                    else:
                        q_p = float(load_36[base + t_s])
                        t_deliv[t_s] = OPEN_DASH
                    t_plan[t_s] = q_p

                oth_plan = float(sum(load_36[base + s] for s in others_slots))
                if d == 0:
                    oth_deliv_num = float(bcs_by_tp.get((off_window_tid, pk), 0.0))
                    for s in others_slots:
                        oth_tid = f"{run_date:%Y%m%d}_{supplier_key}_{SLOT_LABELS[s]}"
                        oth_deliv_num += float(bcs_by_tp.get((oth_tid, pk), 0.0))
                    closed_rec_d0 += oth_deliv_num
                    closed_plan_d0 += oth_plan
                    oth_deliv: Any = oth_deliv_num
                    tot_deliv: Any = float(sum(float(b["gr_qty"]) for b in part_bcs))
                    all_p_closed = all(slot_is_closed.get(t_s, False) for t_s, q_p in t_plan.items() if q_p > 1e-9)
                else:
                    oth_deliv = OPEN_DASH
                    tot_deliv = OPEN_DASH
                    all_p_closed = False

                tot_plan = sum(t_plan.values()) + oth_plan
                days_data.append({
                    "truck_plan": t_plan,
                    "truck_deliv": t_deliv,
                    "pallet_plan": 0.0,
                    "pallet_deliv": OPEN_DASH,
                    "pallet_closed": False,
                    "other_plan": oth_plan,
                    "other_deliv": oth_deliv,
                    "total_plan": tot_plan,
                    "total_deliv": tot_deliv,
                    "all_planned_closed": all_p_closed,
                })

        proj = projections.get(pk)
        if proj is not None:
            warn_info = compute_part_supplier_warning(
                proj,
                sup_trucks=sup_trucks,
                planned_day0_by_slot=planned_day0_slots,
                closed_received_day0=closed_rec_d0,
                closed_planned_day0=closed_plan_d0,
            )
        else:
            warn_info = {"warning_text": "", "suppressed_missing_zm212": False, "pcs_needed": 0.0, "boxes_needed": 0}

        entry = {
            "part_key": pk,
            "part_no": hp["part_no"],
            "part_name": hp["part_name"],
            "cls": cls_code,
            "box_qty": float(hp["box_qty"] or 0.0),
            "start_sap": float(hp["start_sap"] or 0.0),
            "start_in_zm212": bool(int(hp.get("start_in_zm212", 1))),
            "days": days_data,
            "warning": warn_info["warning_text"],
            "warn_info": warn_info,
            "closed_planned_day0": closed_plan_d0,
            "closed_received_day0": closed_rec_d0,
        }
        part_entries.append(entry)
        if warn_info["warning_text"]:
            warnings_out.append(entry)
        elif warn_info.get("suppressed_missing_zm212"):
            suppressed_zm212_out.append(entry)

    return {
        "supplier_key": supplier_key,
        "vendor_display": vendor_display,
        "run_date": run_date,
        "as_of_ts": as_of_ts,
        "dates": dates,
        "trucks": fixed_slots,
        "slot_is_closed": slot_is_closed,
        "closed_truck_labels": closed_labels,
        "parts": part_entries,
        "warnings": warnings_out,
        "suppressed_missing_zm212": suppressed_zm212_out,
    }


def write_supplier_delivery_status_file(out_dir: Path, model: Dict[str, Any]) -> Path:
    """Writes `SRM_DeliveryStatus_<Supplier>_<YYYYMMDD>_<HHMM>.xlsx` with one sheet `Truck plan` (#387)."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border
    from openpyxl.utils import get_column_letter as L

    vendor = model["vendor_display"]
    run_date: dt.date = model["run_date"]
    as_of_ts: dt.datetime = model["as_of_ts"]
    dates: List[dt.date] = model["dates"]
    trucks: List[int] = model["trucks"]
    parts: List[Dict[str, Any]] = model["parts"]
    closed_txt = ", ".join(model["closed_truck_labels"]) if model["closed_truck_labels"] else "none yet"
    company = _company_str()

    subtitle = (
        f"{vendor}   |   {company}   |   run {as_of_ts:%a %m/%d %H:%M} EST   |   "
        f"Closed trucks: {closed_txt}   |   "
        f"Two rows per part: Plan = dispatched truck load; Delivered = actual WMS receipts ({OPEN_DASH} if truck not closed yet)."
    )

    st = make_styles()
    fill, font, thin, mid, grid_border = st["fill"], st["font"], st["thin"], st["mid"], st["grid_border"]
    center = st["align_center"]
    right = st["align_right"]

    wb = Workbook()
    ws = wb.active
    ws.title = "Truck plan"

    lead = ["Part #", "Part name", "Box qty", "Stock 06:00 today", "Row"]
    h1, h2 = 5, 6
    cols: List[Dict[str, Any]] = []
    col = len(lead) + 1

    for d, day in enumerate(dates):
        has_pallet = any(
            p["cls"] == "L" and (p["days"][d]["pallet_plan"] > 1e-9 or (d == 0 and isinstance(p["days"][d]["pallet_deliv"], (int, float)) and p["days"][d]["pallet_deliv"] > 1e-9))
            for p in parts
        )
        has_other = any(
            p["cls"] != "L" and (
                p["days"][d]["other_plan"] > 1e-9
                or (isinstance(p["days"][d]["other_deliv"], (int, float)) and p["days"][d]["other_deliv"] > 1e-9)
            )
            for p in parts
        )
        spec: Dict[str, Any] = {"truck": {}, "start": col}
        for t_s in trucks:
            ws.cell(row=h2, column=col, value=f"{SLOT_LABELS[t_s]}\n{_slot_time(day, t_s)}")
            spec["truck"][t_s] = col
            col += 1
        if has_pallet:
            ws.cell(row=h2, column=col, value="Pallet\nTruck")
            spec["pallet"] = col
            col += 1
        if has_other:
            ws.cell(row=h2, column=col, value="Other\ntimes")
            spec["other"] = col
            col += 1
        ws.cell(row=h2, column=col, value="Day\ntotal")
        spec["total"] = col
        ws.merge_cells(start_row=h1, start_column=spec["start"], end_row=h1, end_column=col)
        ws.cell(row=h1, column=spec["start"], value=f"{day:%a} {day.month}/{day.day}")
        cols.append(spec)
        col += 1

    warn_col = col
    apply_title_and_legend_band(
        ws,
        "Truck delivery status",
        subtitle,
        [
            (PINK, "Delivered below Plan / Shortage warning"),
            (LIGHT_BLUE, "Delivered above Plan"),
            (ORANGE, "Pallet Truck - once-a-day items"),
            (META_BAND, "Day total"),
        ],
        [1, 5, 9, 14],
        warn_col,
    )

    for c, h in enumerate(lead + [None] * (warn_col - len(lead) - 1) + ["Warning"], 1):
        if h:
            ws.merge_cells(start_row=h1, start_column=c, end_row=h2, end_column=c)
            ws.cell(row=h1, column=c, value=h)

    for rr in (h1, h2):
        for c in range(1, warn_col + 1):
            cell = ws.cell(row=rr, column=c)
            is_lead_or_warn = c <= len(lead) or c == warn_col
            cell.fill = fill(HDR_PRIMARY if (rr == h1 or is_lead_or_warn) else HDR_SECONDARY)
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

    plan_totals: Dict[int, float] = {}
    deliv_totals: Dict[int, float] = {}
    plan_boxes: Dict[int, float] = {}
    deliv_boxes: Dict[int, float] = {}

    row = h2 + 1
    for n, p in enumerate(parts):
        r_plan = row
        r_deliv = row + 1
        ws.row_dimensions[r_plan].height = 19
        ws.row_dimensions[r_deliv].height = 20

        is_sep = n > 0 and parts[n - 1]["cls"] != "L" and p["cls"] == "L"
        top_plan = mid if is_sep else thin
        bx = p["box_qty"]

        c_pl_lbl = ws.cell(row=r_plan, column=5, value="Plan")
        c_pl_lbl.font = font(size=9.0, bold=False, color=SUBTLE_TEXT)
        c_pl_lbl.alignment = st["align_left"]

        c_dl_lbl = ws.cell(row=r_deliv, column=5, value="Delivered")
        c_dl_lbl.font = font(size=9.0, bold=True, color=HDR_PRIMARY)
        c_dl_lbl.alignment = st["align_left"]

        for d, spec in enumerate(cols):
            dd = p["days"][d]
            if p["cls"] == "L":
                col_items: List[Tuple[Any, Optional[int], float, Any, bool]] = [
                    ("pallet", spec.get("pallet"), dd["pallet_plan"], dd["pallet_deliv"], True),
                ]
            else:
                col_items = [
                    (t_s, spec["truck"].get(t_s), dd["truck_plan"][t_s], dd["truck_deliv"][t_s], True)
                    for t_s in trucks
                ]
                if "other" in spec:
                    col_items.append(("other", spec.get("other"), dd["other_plan"], dd["other_deliv"], True))

            for key, c_idx, q_plan, q_deliv, _ in col_items:
                if c_idx is None:
                    continue
                if q_plan > 1e-9:
                    cp = ws.cell(row=r_plan, column=c_idx, value=q_plan)
                    cp.number_format = NUM_FMT
                    cp.font = font(size=9.5, bold=False, color=BODY_TEXT)
                    cp.alignment = right
                    if key == "pallet":
                        cp.fill = fill(ORANGE)
                    plan_totals[c_idx] = plan_totals.get(c_idx, 0.0) + q_plan
                    if bx > 1e-9:
                        plan_boxes[c_idx] = plan_boxes.get(c_idx, 0.0) + (q_plan / bx)

                cd = ws.cell(row=r_deliv, column=c_idx)
                if q_deliv == OPEN_DASH:
                    cd.value = OPEN_DASH
                    cd.number_format = "@"
                    cd.font = font(size=9.5, bold=False, color=MUTED_TEXT)
                    cd.alignment = center
                else:
                    dq = float(q_deliv)
                    cd.value = dq
                    cd.number_format = STOCK_FMT
                    cd.alignment = right
                    deliv_totals[c_idx] = deliv_totals.get(c_idx, 0.0) + dq
                    if bx > 1e-9:
                        deliv_boxes[c_idx] = deliv_boxes.get(c_idx, 0.0) + (dq / bx)

                    if dq < q_plan - 1e-9:
                        cd.fill = fill(PINK)
                        cd.font = font(size=9.5, bold=True, color=RED)
                    elif dq > q_plan + 1e-9:
                        cd.fill = fill(LIGHT_BLUE)
                        cd.font = font(size=9.5, bold=True, color=HDR_SECONDARY)
                    else:
                        cd.font = font(size=9.5, bold=False, color=BODY_TEXT)

            tot_c = spec["total"]
            tp_val = dd["total_plan"]
            td_val = dd["total_deliv"]

            ctp = ws.cell(row=r_plan, column=tot_c, value=tp_val if tp_val > 1e-9 else None)
            ctp.number_format = NUM_FMT
            ctp.font = font(size=9.5, bold=True, color=BODY_TEXT)
            ctp.alignment = right
            ctp.fill = fill(META_BAND)
            if tp_val > 1e-9:
                plan_totals[tot_c] = plan_totals.get(tot_c, 0.0) + tp_val

            ctd = ws.cell(row=r_deliv, column=tot_c)
            if td_val == OPEN_DASH:
                ctd.value = OPEN_DASH
                ctd.number_format = "@"
                ctd.font = font(size=9.5, bold=False, color=MUTED_TEXT)
                ctd.alignment = center
                ctd.fill = fill(META_BAND)
            else:
                tdq = float(td_val)
                ctd.value = tdq
                ctd.number_format = STOCK_FMT
                ctd.alignment = right
                deliv_totals[tot_c] = deliv_totals.get(tot_c, 0.0) + tdq
                if dd["all_planned_closed"] and tdq < tp_val - 1e-9:
                    ctd.fill = fill(PINK)
                    ctd.font = font(size=9.5, bold=True, color=RED)
                elif tdq > tp_val + 1e-9:
                    ctd.fill = fill(LIGHT_BLUE)
                    ctd.font = font(size=9.5, bold=True, color=HDR_SECONDARY)
                else:
                    ctd.fill = fill(META_BAND)
                    ctd.font = font(size=9.5, bold=True, color=BODY_TEXT)

        if p["warning"]:
            cw = ws.cell(row=r_deliv, column=warn_col, value=p["warning"])
            cw.fill = fill(PINK)
            cw.font = font(size=9.0, bold=True, color=RED)
            cw.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        start_disp = p["start_sap"] if p["start_in_zm212"] else None
        for c, v in enumerate([p["part_no"], p["part_name"], bx if bx > 1e-9 else None, start_disp], 1):
            ws.merge_cells(start_row=r_plan, start_column=c, end_row=r_deliv, end_column=c)
            cell = ws.cell(row=r_plan, column=c, value=v)
            cell.font = font(size=9.5, bold=(c == 1), color=BODY_TEXT)
            cell.number_format = STOCK_FMT if c == 4 else (NUM_FMT if c == 3 else "@")
            cell.alignment = st["align_left_wrap"] if c == 2 else (st["align_left"] if c == 1 else right)

        for rr, top_b, bot_b in ((r_plan, top_plan, thin), (r_deliv, thin, thin)):
            for c in range(1, warn_col + 1):
                cell = ws.cell(row=rr, column=c)
                if cell.fill.fgColor.rgb in (None, "00000000") and n % 2 == 1:
                    cell.fill = fill(ZEBRA_FILL)
                cell.border = Border(left=thin, right=thin, top=top_b, bottom=bot_b)

        row += 2

    last_data_row = row - 1

    footer_specs = [
        ("Total EA (Plan)", plan_totals, NUM_FMT, mid),
        ("Total EA (Delivered)", deliv_totals, STOCK_FMT, thin),
        ("Total carts (boxes) - Plan", plan_boxes, BOX_TOTAL_FMT, thin),
        ("Total carts (boxes) - Delivered", deliv_boxes, BOX_TOTAL_FMT, thin),
    ]
    for label, data_map, fmt, top_b in footer_specs:
        ws.row_dimensions[row].height = 20
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
        lbl_c = ws.cell(row=row, column=1, value=label)
        lbl_c.font = font(size=9.5, bold=True, color=HDR_PRIMARY)
        lbl_c.alignment = st["align_left"]
        for c_idx, val in data_map.items():
            c_cell = ws.cell(row=row, column=c_idx, value=round(val, 1) if fmt == BOX_TOTAL_FMT else round(val, 2))
            c_cell.number_format = fmt
            c_cell.font = font(size=9.5, bold=True, color=HDR_PRIMARY)
            c_cell.alignment = right
        for c in range(1, warn_col + 1):
            c_cell = ws.cell(row=row, column=c)
            c_cell.fill = fill(HDR_SUBTLE)
            c_cell.border = Border(left=thin, right=thin, top=top_b, bottom=thin)
        row += 1

    for rr in range(h2 + 1, row):
        for spec in cols:
            sc_first = ws.cell(row=rr, column=spec["start"])
            sc_first.border = Border(
                left=mid, right=sc_first.border.right, top=sc_first.border.top, bottom=sc_first.border.bottom
            )
            tc = ws.cell(row=rr, column=spec["total"])
            tc.border = Border(left=mid, right=mid, top=tc.border.top, bottom=tc.border.bottom)
        if rr <= last_data_row:
            wc = ws.cell(row=rr, column=warn_col)
            wc.border = Border(left=mid, right=thin, top=wc.border.top, bottom=wc.border.bottom)

    for c, wdt in {1: 15, 2: 38, 3: 8.5, 4: 13.5, 5: 10.5}.items():
        ws.column_dimensions[L(c)].width = wdt
    for c in range(len(lead) + 1, warn_col):
        ws.column_dimensions[L(c)].width = 10.5
    ws.column_dimensions[L(warn_col)].width = 46

    ws.freeze_panes = ws.cell(row=h2 + 1, column=len(lead) + 1)
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.print_title_rows = f"{h1}:{h2}"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / delivery_status_filename(vendor, run_date, as_of_ts)
    wb.save(out_path)

    all_texts = {
        f"r{r_i}c{c_i}": str(ws.cell(row=r_i, column=c_i).value)
        for r_i in range(1, ws.max_row + 1)
        for c_i in range(1, ws.max_column + 1)
        if ws.cell(row=r_i, column=c_i).value is not None
    }
    assert_no_hangul(all_texts)
    return out_path
