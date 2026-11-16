"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - Core Projection & Truck State Engine (#351)
=======================================================================================
Implements all approved rules from handoff #351, #368, #370 §2, and #371 §3:
  1. WMS GR barcode reader (`Move.Type == '101'`, plant-local `[D 06:00, D+1 06:00)` window).
  2. Idempotent barcode-to-truck attribution (`barcode PRIMARY KEY`, FIFO across open planned
     trucks, unplanned receipts isolated so they never prematurely close an expected truck).
  3. Per-truck polling state machine:
       - `SCHEDULED` -> `POLLING_30M` at `departure + 30 min`
       - First matched planned barcode -> `RECEIVING_10M` (`10 min` cadence)
       - `2` consecutive quiet `10 min` polls (`20` quiet minutes) -> `CLOSED_RECEIVED`
         (any planned part on that truck with `received == 0` -> `NOT_DELIVERED`)
       - `0` barcodes by `min(next_same_supplier_truck_departure + 30m, departure + 180m)`
         -> `TRUCK_NOT_RECEIVED`
       - `0` barcodes at `departure + 90m` + counterfactual shortage before next truck
         -> `EARLY_WARNING_90M` (user only, once per truck)
  4. Two strictly separated stock metrics (`371 §3.4`):
       - **Hybrid (`Shortage (Receipt Basis)` Column A + `LINE_SHORTAGE_ALARM` + `BUFFER_LOW_15PCT`)**:
         closed trucks use actual `GR` at actual receipt slot; departed-but-open and future trucks
         use planned `Truck load`.
       - **`Runout (received only)`**: `.xlsb` row-10 formula (`start_sap + cum(actual_GR - req)`,
         future orders = 0), reference column only, NEVER triggers any alarm or flag.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DEPT_DIR = BASE_DIR.parent
MCP_ROOT = DEPT_DIR.parent
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"
INV_DEPT_DIR = MCP_ROOT / "01_DEPT_Inventory"

for _p in (str(BASE_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DEPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delivery_db_manager import (  # noqa: E402
    fetch_barcodes,
    fetch_horizon_parts,
    fetch_truck_plan_lines,
    fetch_truck_runs,
)
try:
    from material_desc_helper import get_material_desc  # noqa: E402
except ImportError:
    def get_material_desc(materials, **_kw):  # type: ignore[misc]
        if isinstance(materials, str):
            return "(no description)"
        return {str(m): "(no description)" for m in (materials or [])}
from srm_parsers import N_HOURLY_DAYS, N_SLOTS, num, part_key, read_rows, text, vendor_name  # noqa: E402
from srm_projection import ENOUGH, local_label, slot_start  # noqa: E402

CLOSED_TRUCK_STATES = frozenset({"CLOSED_RECEIVED", "TRUCK_NOT_RECEIVED", "CLOSED_DAY_LEVEL"})
OPEN_TRUCK_STATES = frozenset({"SCHEDULED", "POLLING_30M", "RECEIVING_10M", "DAY_LEVEL_OPEN"})


def _parse_ts(val: Any) -> dt.datetime:
    if isinstance(val, dt.datetime):
        return val
    s = str(val).strip()[:19]
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _fmt_ts(ts: dt.datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _col_opt(header: List[Any], *names: str) -> Optional[int]:
    low = [re.sub(r"\s+", " ", text(c)).strip().lower() for c in header]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    return None


def slot_index_for_ts(run_date: dt.date, ts: dt.datetime) -> int:
    """Maps a plant-local timestamp to 2-hour slot index `0..35` (`0 = run_date 06:00`)."""
    t0 = slot_start(run_date, 0, 0)
    hrs = (ts - t0).total_seconds() / 3600.0
    if hrs < 0:
        return -1
    return int(hrs // 24) * N_SLOTS + int((hrs % 24) // 2)


def parse_gr_excel_barcodes(
    gr_path: Path,
    work_date: dt.date,
    *,
    as_of_ts: Optional[dt.datetime] = None,
    move_types: Tuple[str, ...] = ("101",),
) -> List[Dict[str, Any]]:
    """
    Reads WMS `GrList` Excel/CSV rows (`Move.Type == '101'`) within the plant workday
    window `[work_date 06:00:00, work_date+1 06:00:00)` and `<= as_of_ts` (when given).
    """
    rows = read_rows(gr_path)
    if not rows:
        return []

    hdr_idx: Optional[int] = None
    for i, r in enumerate(rows[:20]):
        low_cells = {re.sub(r"\s+", " ", text(c)).strip().lower() for c in r}
        if "mat.no" in low_cells and "gr time" in low_cells:
            hdr_idx = i
            break
    if hdr_idx is None:
        raise ValueError(f"WMS GR header ('Mat.No', 'GR Time') not found in {gr_path.name}")

    hdr = rows[hdr_idx]
    c_bc = _col_opt(hdr, "Barcode")
    c_mat = _col_opt(hdr, "Mat.No")
    c_desc = _col_opt(hdr, "Mat.Desc", "Material Description")
    c_loc = _col_opt(hdr, "GR Loc")
    c_time = _col_opt(hdr, "GR Time")
    c_qty = _col_opt(hdr, "GR Qty", "Qty")
    c_unit = _col_opt(hdr, "Unit")
    c_mvt = _col_opt(hdr, "Move.Type")
    c_ven = _col_opt(hdr, "Vendor Name", "Vendor")
    c_trn = _col_opt(hdr, "Trans.No")
    c_inv = _col_opt(hdr, "INVOICE_NO", "Invoice No")

    if c_mat is None or c_time is None or c_qty is None or c_mvt is None:
        raise ValueError(f"Required WMS GR columns missing in {gr_path.name}")

    t_start = slot_start(work_date, 0, 0)
    t_end = slot_start(work_date, 1, 0)
    parsed: List[Dict[str, Any]] = []
    seen_bc: set[str] = set()

    for idx, r in enumerate(rows[hdr_idx + 1 :], start=1):
        mat_raw = text(r[c_mat] if c_mat < len(r) else None)
        mvt_raw = text(r[c_mvt] if c_mvt < len(r) else None)
        if not mat_raw or mvt_raw not in move_types:
            continue

        raw_t = r[c_time] if c_time < len(r) else None
        if isinstance(raw_t, dt.datetime):
            gr_dt = raw_t
        else:
            t_str = text(raw_t)[:19]
            try:
                gr_dt = dt.datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue

        if gr_dt < t_start or gr_dt >= t_end:
            continue
        if as_of_ts is not None and gr_dt > as_of_ts:
            continue

        bc = text(r[c_bc] if (c_bc is not None and c_bc < len(r)) else None)
        if not bc or bc.lower() == "nan":
            bc = f"SYNTH_{work_date:%Y%m%d}_{part_key(mat_raw)}_{gr_dt:%H%M%S}_{idx}"
        if bc in seen_bc:
            continue
        seen_bc.add(bc)

        s_idx = slot_index_for_ts(work_date, gr_dt)
        parsed.append(
            {
                "barcode": bc,
                "work_date": work_date.isoformat(),
                "mat_no": mat_raw,
                "part_key": part_key(mat_raw),
                "mat_desc": text(r[c_desc] if (c_desc is not None and c_desc < len(r)) else None),
                "vendor_name": vendor_name(r[c_ven] if (c_ven is not None and c_ven < len(r)) else None),
                "gr_loc": text(r[c_loc] if (c_loc is not None and c_loc < len(r)) else None) or "SL00",
                "gr_time": _fmt_ts(gr_dt),
                "gr_dt": gr_dt,
                "slot_idx": max(0, min(35, s_idx)),
                "gr_qty": float(num(r[c_qty] if c_qty < len(r) else 0.0)),
                "unit": text(r[c_unit] if (c_unit is not None and c_unit < len(r)) else None) or "EA",
                "move_type": mvt_raw,
                "trans_no": text(r[c_trn] if (c_trn is not None and c_trn < len(r)) else None),
                "invoice_no": text(r[c_inv] if (c_inv is not None and c_inv < len(r)) else None),
                "source_file": gr_path.name,
            }
        )

    missing_mats = [x["mat_no"] for x in parsed if not x["mat_desc"]]
    if missing_mats:
        desc_map = get_material_desc(missing_mats)
        for x in parsed:
            if not x["mat_desc"]:
                x["mat_desc"] = desc_map.get(x["mat_no"], "(no description)")
    else:
        for x in parsed:
            if not x["mat_desc"]:
                x["mat_desc"] = "(no description)"

    parsed.sort(key=lambda x: (x["gr_time"], x["barcode"]))
    return parsed


def _resolve_barcode_supplier(
    bc_row: Dict[str, Any],
    supplier_keys: List[str],
    part_to_suppliers: Dict[str, List[str]],
) -> str:
    v_low = (bc_row.get("vendor_name") or "").lower()
    for sk in supplier_keys:
        if sk.lower() in v_low:
            return sk
    cands = part_to_suppliers.get(bc_row["part_key"], [])
    if len(cands) == 1:
        return cands[0]
    return bc_row.get("vendor_name") or "UNKNOWN"


def ingest_and_attribute_barcodes(
    conn,
    work_date: dt.date,
    parsed_barcodes: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    as_of_ts: dt.datetime,
) -> Dict[str, Any]:
    """
    Idempotently inserts new WMS GR barcodes (`barcode PRIMARY KEY`) and attributes unassigned
    barcodes to eligible open trucks of the matching supplier.
    Returns per-truck counts of newly matched planned barcodes on this poll!
    """
    work_date_str = work_date.isoformat()
    poll_ts_str = _fmt_ts(as_of_ts)
    early_grace_min = int(cfg.get("early_arrival_grace_min", 20))

    trucks = fetch_truck_runs(conn, work_date_str)
    plan_lines = fetch_truck_plan_lines(conn, work_date_str)
    horizon = fetch_horizon_parts(conn, work_date_str)

    supplier_keys = sorted({t["supplier_key"] for t in trucks} | {h["supplier_key"] for h in horizon.values()})
    part_to_suppliers: Dict[str, List[str]] = {}
    for h in horizon.values():
        part_to_suppliers.setdefault(h["part_key"], [])
        if h["supplier_key"] not in part_to_suppliers[h["part_key"]]:
            part_to_suppliers[h["part_key"]].append(h["supplier_key"])

    existing_bcs = {
        r["barcode"]: dict(r)
        for r in conn.execute("SELECT * FROM wms_gr_barcodes WHERE work_date = ?", (work_date_str,)).fetchall()
    }

    # Track running received PCS per (truck_id, part_key) from already-assigned barcodes
    received_by_tp: Dict[Tuple[str, str], float] = {}
    for b in existing_bcs.values():
        if b.get("assigned_truck_id") and not b.get("is_unplanned"):
            k = (b["assigned_truck_id"], b["part_key"])
            received_by_tp[k] = received_by_tp.get(k, 0.0) + float(b["gr_qty"])

    planned_by_tp: Dict[Tuple[str, str], float] = {
        (ln["truck_id"], ln["part_key"]): float(ln["planned_pcs"])
        for ln in plan_lines
    }

    open_trucks_by_sup: Dict[str, List[Dict[str, Any]]] = {}
    earliest_c_truck_by_sup: Dict[str, dt.datetime] = {}
    has_c_trucks_by_sup: Dict[str, bool] = {}
    tracked_truck_suppliers = {t["supplier_key"] for t in trucks}
    for t in trucks:
        sup = t["supplier_key"]
        dep_dt = _parse_ts(t["departure_ts"])
        if not t["is_pallet_truck"]:
            has_c_trucks_by_sup[sup] = True
            if sup not in earliest_c_truck_by_sup or dep_dt < earliest_c_truck_by_sup[sup]:
                earliest_c_truck_by_sup[sup] = dep_dt
        if t["status"] in OPEN_TRUCK_STATES:
            open_trucks_by_sup.setdefault(sup, []).append(t)

    for sup in open_trucks_by_sup:
        open_trucks_by_sup[sup].sort(key=lambda x: (_parse_ts(x["departure_ts"]), x["is_pallet_truck"], x["truck_id"]))

    new_barcodes_inserted = 0
    new_planned_by_truck: Dict[str, int] = {}

    for b in parsed_barcodes:
        bc_id = b["barcode"]
        if bc_id in existing_bcs:
            continue

        sup_key = _resolve_barcode_supplier(b, supplier_keys, part_to_suppliers)
        gr_dt: dt.datetime = b["gr_dt"]
        pk = b["part_key"]

        cand_open = open_trucks_by_sup.get(sup_key, [])
        eligible: List[Dict[str, Any]] = []
        first_c_dep = earliest_c_truck_by_sup.get(sup_key)
        for t in cand_open:
            dep_dt = _parse_ts(t["departure_ts"])
            is_first_of_day = (
                (not t["is_pallet_truck"] and first_c_dep is not None and dep_dt == first_c_dep)
                or bool(t["is_pallet_truck"])
            )
            min_ts = slot_start(work_date, 0, 0) if is_first_of_day else (dep_dt - dt.timedelta(minutes=early_grace_min))
            if gr_dt >= min_ts:
                eligible.append(t)

        assigned_truck_id: Optional[str] = None
        is_unplanned = 0

        planned_eligible = [t for t in eligible if (t["truck_id"], pk) in planned_by_tp]
        eligible_c = [t for t in eligible if not t["is_pallet_truck"]]
        if planned_eligible:
            # 1) Earliest open eligible truck still needing quantity for this part
            unfilled = [
                t
                for t in planned_eligible
                if received_by_tp.get((t["truck_id"], pk), 0.0) < planned_by_tp[(t["truck_id"], pk)] - 1e-9
            ]
            if unfilled:
                chosen = unfilled[0]
            else:
                # Over-delivery: assign to latest eligible planned truck whose transit window started (or last eligible)
                transit_ready = [
                    t
                    for t in planned_eligible
                    if gr_dt >= _parse_ts(t["departure_ts"]) + dt.timedelta(minutes=20)
                ]
                chosen = transit_ready[-1] if transit_ready else planned_eligible[0]
            assigned_truck_id = chosen["truck_id"]
            received_by_tp[(assigned_truck_id, pk)] = received_by_tp.get((assigned_truck_id, pk), 0.0) + b["gr_qty"]
            new_planned_by_truck[assigned_truck_id] = new_planned_by_truck.get(assigned_truck_id, 0) + 1
        elif eligible_c:
            # Off-schedule or unplanned receipt arriving during an open C-truck window:
            # attach with is_unplanned=1 (NEVER increments new_planned_by_truck or triggers RECEIVING_10M / closing!)
            is_unplanned = 1
            receiving_now = [t for t in eligible_c if t["status"] == "RECEIVING_10M"]
            chosen = receiving_now[-1] if receiving_now else eligible_c[-1]
            assigned_truck_id = chosen["truck_id"]
        elif sup_key in tracked_truck_suppliers:
            # Arrived outside open C-truck windows (or supplier only has a Pallet Truck):
            # isolate into supplier's OFF_WINDOW bucket (or PALLET if supplier has zero C-trucks)
            # so neither PALLET nor already-closed C-trucks are polluted and zero day receipts vanish.
            is_unplanned = 1
            if not has_c_trucks_by_sup.get(sup_key) and eligible:
                assigned_truck_id = eligible[-1]["truck_id"]
            else:
                assigned_truck_id = f"{work_date:%Y%m%d}_{sup_key}_OFF_WINDOW"
        else:
            is_unplanned = 1

        conn.execute(
            """
            INSERT INTO wms_gr_barcodes (
                barcode, work_date, mat_no, part_key, mat_desc, vendor_name, supplier_key,
                gr_loc, gr_time, slot_idx, gr_qty, unit, move_type, trans_no, invoice_no,
                assigned_truck_id, is_unplanned, source_file, first_seen_poll_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bc_id,
                work_date_str,
                b["mat_no"],
                pk,
                b["mat_desc"],
                b["vendor_name"],
                sup_key,
                b["gr_loc"],
                b["gr_time"],
                b["slot_idx"],
                b["gr_qty"],
                b["unit"],
                b["move_type"],
                b["trans_no"],
                b["invoice_no"],
                assigned_truck_id,
                is_unplanned,
                b["source_file"],
                poll_ts_str,
            ),
        )
        new_barcodes_inserted += 1

    conn.commit()
    return {
        "new_barcodes_inserted": new_barcodes_inserted,
        "new_planned_by_truck": new_planned_by_truck,
    }


def compute_part_projections(
    run_date: dt.date,
    horizon_parts: Dict[str, Dict[str, Any]],
    trucks: List[Dict[str, Any]],
    plan_lines: List[Dict[str, Any]],
    barcodes: List[Dict[str, Any]],
    *,
    buffer_margin: float = 0.15,
    force_closed_zero_truck_ids: Optional[set[str]] = None,
    as_of_ts: Optional[dt.datetime] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Computes both stock series per part over the 36 two-hour slots (`Days 0..2`):
      1. `hybrid_stock_36` (`Shortage (Receipt Basis)` Column A + `LINE_SHORTAGE_ALARM` + `BUFFER_LOW_15PCT`):
         - Closed trucks (`CLOSED_RECEIVED` / `TRUCK_NOT_RECEIVED` / `CLOSED_DAY_LEVEL`, or
           `force_closed_zero_truck_ids` for the `T+90m` early-warning counterfactual) use actual `GR`
           at actual receipt slot.
         - Open (`SCHEDULED` / `POLLING_30M` / `RECEIVING_10M` / `DAY_LEVEL_OPEN`) and future Day 1..2
           trucks use `Truck load`.
      2. `received_only_stock_36` (`Runout (received only)` reference column):
         - `start_sap + cumulative(all_actual_GR - req_36)` (future orders = 0). Never triggers alarms.
    """
    forced_zero = force_closed_zero_truck_ids or set()
    truck_by_id = {t["truck_id"]: t for t in trucks}
    clamped_as_of_slot = (
        max(0, min(3 * N_SLOTS - 1, slot_index_for_ts(run_date, as_of_ts)))
        if as_of_ts is not None
        else 0
    )

    # Map (part_key, day0_slot) -> list of (truck_id, planned_pcs) so we know which Day-0 planned loads stay open
    open_day0_plan_by_slot: Dict[Tuple[str, int], float] = {}
    for ln in plan_lines:
        tid = ln["truck_id"]
        t = truck_by_id.get(tid)
        if not t:
            continue
        is_closed = (t["status"] in CLOSED_TRUCK_STATES) or (tid in forced_zero)
        if not is_closed:
            pk = ln["part_key"]
            if t["is_pallet_truck"]:
                # Keep Day-0 slot distribution from horizon load_36 for an open Pallet Truck
                hp = horizon_parts.get(pk)
                if hp:
                    for s_i in range(N_SLOTS):
                        if hp["load_36"][s_i] > 1e-9:
                            open_day0_plan_by_slot[(pk, s_i)] = (
                                open_day0_plan_by_slot.get((pk, s_i), 0.0) + hp["load_36"][s_i]
                            )
            else:
                s_i = int(ln["slot_idx"])
                open_day0_plan_by_slot[(pk, s_i)] = open_day0_plan_by_slot.get((pk, s_i), 0.0) + float(ln["planned_pcs"])

    all_gr_by_slot: Dict[Tuple[str, int], float] = {}
    hybrid_gr_by_slot: Dict[Tuple[str, int], float] = {}

    for b in barcodes:
        pk = b["part_key"]
        s_i = int(b["slot_idx"])
        if not (0 <= s_i < 3 * N_SLOTS):
            continue
        q = float(b["gr_qty"])
        all_gr_by_slot[(pk, s_i)] = all_gr_by_slot.get((pk, s_i), 0.0) + q

        tid = b.get("assigned_truck_id")
        if tid in forced_zero:
            continue
        if not tid or b.get("is_unplanned"):
            hybrid_gr_by_slot[(pk, s_i)] = hybrid_gr_by_slot.get((pk, s_i), 0.0) + q
        else:
            t = truck_by_id.get(tid)
            if t and t["status"] in CLOSED_TRUCK_STATES:
                hybrid_gr_by_slot[(pk, s_i)] = hybrid_gr_by_slot.get((pk, s_i), 0.0) + q

    slot_labels_36 = [
        local_label(slot_start(run_date, i // N_SLOTS, i % N_SLOTS))
        for i in range(3 * N_SLOTS)
    ]

    results: Dict[str, Dict[str, Any]] = {}
    for pk, hp in horizon_parts.items():
        start_sap = float(hp["start_sap"])
        load_36 = list(hp["load_36"][: 3 * N_SLOTS])
        req_36 = list(hp["req_36"][: 3 * N_SLOTS])

        has_day0_truck_runs = any(ln["part_key"] == pk for ln in plan_lines)

        plan_stock_36: List[float] = []
        hybrid_supply_36: List[float] = []
        hybrid_stock_36: List[float] = []
        actual_gr_36: List[float] = []
        gr_po_36: List[float] = []
        received_only_stock_36: List[float] = []

        inv_p = start_sap
        inv_h = start_sap
        inv_ro = start_sap

        for i in range(3 * N_SLOTS):
            p_load = load_36[i]
            req = req_36[i]
            act_gr = all_gr_by_slot.get((pk, i), 0.0)

            if i < N_SLOTS:
                if has_day0_truck_runs:
                    h_sup = open_day0_plan_by_slot.get((pk, i), 0.0) + hybrid_gr_by_slot.get((pk, i), 0.0)
                else:
                    h_sup = p_load + act_gr
            else:
                h_sup = p_load

            inv_p += p_load - req
            inv_h += h_sup - req
            inv_ro += act_gr - req

            plan_stock_36.append(inv_p)
            hybrid_supply_36.append(h_sup)
            hybrid_stock_36.append(inv_h)
            actual_gr_36.append(act_gr)
            gr_po_36.append(act_gr - p_load)
            received_only_stock_36.append(inv_ro)

        # Hybrid shortage label: if a past dip before `clamped_as_of_slot` was already recovered
        # by a later receipt so all current/future slots >= 0, clear to ENOUGH (`373 §Finding 1.2`).
        future_neg = next(
            (i for i in range(clamped_as_of_slot, 3 * N_SLOTS) if hybrid_stock_36[i] < -1e-9),
            None,
        )
        if future_neg is None:
            short_hybrid = ENOUGH
        else:
            start_neg = future_neg
            while start_neg > 0 and hybrid_stock_36[start_neg - 1] < -1e-9:
                start_neg -= 1
            short_hybrid = slot_labels_36[start_neg]

        short_plan = next(
            (slot_labels_36[i] for i, v in enumerate(plan_stock_36) if v < -1e-9),
            ENOUGH,
        )
        runout_ro = next(
            (slot_labels_36[i] for i, v in enumerate(received_only_stock_36) if v < -1e-9),
            ENOUGH,
        )

        results[pk] = {
            "part_key": pk,
            "part_no": hp["part_no"],
            "part_name": hp["part_name"],
            "vendor_display": hp["vendor_display"],
            "supplier_key": hp["supplier_key"],
            "cls": hp["cls"],
            "box_qty": float(hp["box_qty"]),
            "start_sap": start_sap,
            "start_in_zm212": bool(int(hp.get("start_in_zm212", 1))),
            "add_0": float(hp["add_3"][0]) if hp.get("add_3") else 0.0,
            "load_36": load_36,
            "req_36": req_36,
            "plan_stock_36": plan_stock_36,
            "hybrid_supply_36": hybrid_supply_36,
            "hybrid_stock_36": hybrid_stock_36,
            "actual_gr_36": actual_gr_36,
            "gr_po_36": gr_po_36,
            "received_only_stock_36": received_only_stock_36,
            "shortage_plan_basis": short_plan,
            "shortage_receipt_basis": short_hybrid,
            "runout_received_only": runout_ro,
            "slot_labels_36": slot_labels_36,
            "buffer_margin": buffer_margin,
            "as_of_slot": clamped_as_of_slot,
        }
    return results


def evaluate_truck_part_gap(
    proj: Dict[str, Any],
    *,
    t_slot: int,
    is_pallet_truck: bool,
    planned_pcs: float,
    received_pcs: float,
    truck_is_closed: bool,
) -> Dict[str, Any]:
    """
    Evaluates the gap `[t_slot, S)` between this truck (`t_slot`) and the next truck slot `S`
    carrying this part (`371 §3.4` + `370 §2` + `373 §Finding 1.2`):
      - `line_shortage_alarm`: active `hybrid_stock < 0` in `[max(t_slot, min(as_of_slot, S-1)), S)`
        caused by under-delivery or late receipt. If a later off-schedule receipt restores
        `hybrid_stock >= 0` for the remainder of the gap, the alarm clears automatically.
      - `buffer_low_15pct` (no alarm): when `S < 36` and not `line_shortage_alarm`,
        `hybrid_stock[S - 1] < (1 + margin) * Req_ref`, where `Req_ref = req[S]` if `> 0`,
        else first `req[idx] > 0` for `idx >= S` (`370 §2`).
    """
    load_36 = proj["load_36"]
    req_36 = proj["req_36"]
    hybrid_36 = proj["hybrid_stock_36"]
    plan_36 = proj["plan_stock_36"]
    labels_36 = proj["slot_labels_36"]
    margin = float(proj.get("buffer_margin", 0.15))
    as_of_slot = int(proj.get("as_of_slot", 0))

    search_from = max(t_slot + 1, N_SLOTS) if is_pallet_truck else (t_slot + 1)
    next_slot_s = next((idx for idx in range(search_from, 3 * N_SLOTS) if load_36[idx] > 1e-9), 3 * N_SLOTS)

    active_start = max(0, t_slot, min(as_of_slot, max(0, next_slot_s - 1)))
    gap_indices = list(range(active_start, max(active_start + 1, next_slot_s)))
    neg_slots = [idx for idx in gap_indices if hybrid_36[idx] < -1e-9]

    full_gap_indices = list(range(max(0, t_slot), max(t_slot + 1, next_slot_s)))
    min_hybrid_active = min((hybrid_36[idx] for idx in gap_indices), default=0.0)
    min_plan_active = min((plan_36[idx] for idx in gap_indices), default=0.0)
    delivery_degraded = (received_pcs < planned_pcs - 1e-9) or (min_hybrid_active < min_plan_active - 1e-9)

    line_shortage = bool(truck_is_closed and neg_slots and delivery_degraded)
    if line_shortage:
        first_neg = neg_slots[0]
        while first_neg > max(0, t_slot) and hybrid_36[first_neg - 1] < -1e-9:
            first_neg -= 1
        first_short_lbl = labels_36[first_neg]
        streak_indices = list(range(first_neg, max(active_start + 1, next_slot_s)))
        min_streak = min((hybrid_36[idx] for idx in streak_indices), default=min_hybrid_active)
        deficit_pcs = round(max(0.0, -min_streak), 2)
    else:
        first_short_lbl = None
        deficit_pcs = 0.0

    buffer_low = False
    min_hybrid_full = min((hybrid_36[idx] for idx in full_gap_indices), default=0.0)
    min_plan_full = min((plan_36[idx] for idx in full_gap_indices), default=0.0)
    degraded_at_s = (received_pcs < planned_pcs - 1e-9) or (min_hybrid_full < min_plan_full - 1e-9)
    if truck_is_closed and (not line_shortage) and next_slot_s < 3 * N_SLOTS and degraded_at_s:
        req_ref = req_36[next_slot_s]
        if req_ref <= 1e-9:
            req_ref = next((req_36[idx] for idx in range(next_slot_s, 3 * N_SLOTS) if req_36[idx] > 1e-9), 0.0)
        if req_ref > 1e-9 and hybrid_36[next_slot_s - 1] < (1.0 + margin) * req_ref - 1e-9:
            buffer_low = True

    return {
        "next_truck_slot": next_slot_s,
        "line_shortage_alarm": line_shortage,
        "first_short_slot_label": first_short_lbl,
        "deficit_before_next_truck_pcs": deficit_pcs,
        "buffer_low_15pct": buffer_low,
    }


def _merge_minute_chunks(events: List[Tuple[dt.datetime, float]]) -> List[Tuple[dt.datetime, str, float]]:
    """Groups barcode quantities sharing the same `YYYY-MM-DD HH:MM` minute for clean recovery notes."""
    buckets: Dict[str, Tuple[dt.datetime, float]] = {}
    for dt_val, qty in sorted(events, key=lambda x: x[0]):
        key = dt_val.strftime("%Y-%m-%d %H:%M")
        if key in buckets:
            buckets[key] = (buckets[key][0], buckets[key][1] + qty)
        else:
            buckets[key] = (dt_val, qty)
    return [(dt_val, key[11:16], round(tot, 2)) for key, (dt_val, tot) in sorted(buckets.items())]


def _format_event_pairs(pairs: List[Tuple[str, float]]) -> str:
    merged: Dict[str, float] = {}
    order: List[str] = []
    for hm, q in pairs:
        if hm not in merged:
            order.append(hm)
            merged[hm] = 0.0
        merged[hm] += q
    return ", ".join(f"+{merged[hm]:,.0f} at {hm}" for hm in order if merged[hm] > 1e-9)


def build_day_part_summary(
    run_date: dt.date,
    horizon_parts: Dict[str, Dict[str, Any]],
    trucks: List[Dict[str, Any]],
    plan_lines: List[Dict[str, Any]],
    barcodes: List[Dict[str, Any]],
    projections: Dict[str, Dict[str, Any]],
    *,
    gr_available: bool = True,
) -> List[Dict[str, Any]]:
    """
    Builds the day-level reconciliation table (`Day_Part_Summary` — `373 §Finding 1.3` & `Finding 3`):
    per supplier and part — planned day total, received day total, day GR-PO, day verdict,
    number of trucks planned / with receipts, first and last receipt time, and hybrid shortage.
    """
    tracked_suppliers: Dict[str, str] = {}
    truck_by_id = {t["truck_id"]: t for t in trucks}
    for t in trucks:
        tracked_suppliers.setdefault(t["supplier_key"], t["vendor_display"])

    plan_by_sp: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for ln in plan_lines:
        if float(ln["planned_pcs"]) > 1e-9:
            plan_by_sp.setdefault((ln["supplier_key"], ln["part_key"]), []).append(ln)

    bcs_by_sp: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for b in barcodes:
        sk = b.get("supplier_key") or ""
        if sk in tracked_suppliers:
            bcs_by_sp.setdefault((sk, b["part_key"]), []).append(b)

    all_sp = sorted(set(plan_by_sp.keys()) | set(bcs_by_sp.keys()))
    summary_rows: List[Dict[str, Any]] = []

    for sup_key, pk in all_sp:
        sp_lines = plan_by_sp.get((sup_key, pk), [])
        sp_bcs = bcs_by_sp.get((sup_key, pk), [])
        planned_day = round(sum(float(ln["planned_pcs"]) for ln in sp_lines), 2)
        received_day = round(sum(float(b["gr_qty"]) for b in sp_bcs), 2)
        if planned_day <= 1e-9 and received_day <= 1e-9:
            continue

        hp = horizon_parts.get(pk)
        if sp_lines:
            part_no = sp_lines[0]["part_no"]
            part_name = sp_lines[0]["part_name"]
            cls_code = sp_lines[0]["cls"]
            box_qty = float(sp_lines[0]["box_qty"])
            vendor_disp = sp_lines[0]["vendor_display"]
        elif hp:
            part_no = hp["part_no"]
            part_name = hp["part_name"]
            cls_code = hp["cls"]
            box_qty = float(hp["box_qty"])
            vendor_disp = hp["vendor_display"]
        else:
            part_no = sp_bcs[0]["mat_no"]
            part_name = sp_bcs[0]["mat_desc"]
            cls_code = "-"
            box_qty = 0.0
            vendor_disp = tracked_suppliers.get(sup_key, sup_key)

        day_gr_po = round(received_day - planned_day, 2)
        trucks_planned = len(sp_lines)
        trucks_with_rcpt = len({b["assigned_truck_id"] for b in sp_bcs if b.get("assigned_truck_id")})
        first_rcpt = min((b["gr_time"] for b in sp_bcs), default=None)
        last_rcpt = max((b["gr_time"] for b in sp_bcs), default=None)

        any_open_planned = any(
            truck_by_id.get(ln["truck_id"], {}).get("status") in OPEN_TRUCK_STATES
            for ln in sp_lines
        )
        if not gr_available and any_open_planned:
            day_verdict = "CANNOT_VERIFY"
        elif planned_day <= 1e-9:
            day_verdict = "UNPLANNED_RECEIPT"
        elif abs(received_day - planned_day) <= 1e-9:
            day_verdict = "DELIVERED"
        elif received_day > planned_day + 1e-9:
            day_verdict = "OVER"
        elif any_open_planned:
            day_verdict = "IN_PROGRESS" if received_day > 1e-9 else "OPEN"
        else:
            day_verdict = "SHORT" if received_day > 1e-9 else "NOT_DELIVERED"

        proj = projections.get(pk)
        short_rcpt = (
            proj["shortage_receipt_basis"]
            if (proj and (gr_available or not any_open_planned))
            else ("CANNOT_VERIFY" if not gr_available else ENOUGH)
        )
        runout_ro = (
            proj["runout_received_only"]
            if (proj and (gr_available or not any_open_planned))
            else ("CANNOT_VERIFY" if not gr_available else ENOUGH)
        )

        summary_rows.append(
            {
                "work_date": run_date.isoformat(),
                "supplier_key": sup_key,
                "vendor_display": vendor_disp,
                "part_key": pk,
                "part_no": part_no,
                "part_name": part_name,
                "cls": cls_code,
                "box_qty": box_qty,
                "planned_day_pcs": planned_day,
                "received_day_pcs": received_day,
                "day_gr_po_pcs": day_gr_po,
                "day_verdict": day_verdict,
                "trucks_planned": trucks_planned,
                "trucks_with_receipts": trucks_with_rcpt,
                "first_receipt_ts": first_rcpt,
                "last_receipt_ts": last_rcpt,
                "shortage_receipt_basis": short_rcpt,
                "runout_received_only": runout_ro,
            }
        )

    summary_rows.sort(
        key=lambda r: (
            0 if r["shortage_receipt_basis"] not in (ENOUGH, "CANNOT_VERIFY") else 1,
            r["vendor_display"],
            0 if r["cls"] == "C" else 1,
            r["part_no"],
        )
    )
    return summary_rows


def evaluate_poll_cycle(
    conn,
    run_date: dt.date,
    cfg: Dict[str, Any],
    as_of_ts: dt.datetime,
    *,
    gr_available: bool,
    new_planned_by_truck: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """
    Advances the per-truck state machine at `as_of_ts` and refreshes `delivery_verdicts`.
    When `gr_available=False` (e.g. `WMS_SSO_EXPIRED` or `POLL_SKIPPED_LOCK_BUSY` or missing GR),
    NO truck is closed and NO false alarm is raised (`verdict = 'CANNOT_VERIFY'`).
    """
    work_date_str = run_date.isoformat()
    now_str = _fmt_ts(as_of_ts)
    new_by_truck = new_planned_by_truck or {}

    poll_30m = int(cfg.get("poll_before_first_receipt_min", 30))
    poll_10m = int(cfg.get("poll_after_first_receipt_min", 10))
    quiet_needed = int(cfg.get("quiet_polls_to_close", 2))
    buffer_margin = float(cfg.get("buffer_margin", 0.15))

    trucks = fetch_truck_runs(conn, work_date_str)
    plan_lines = fetch_truck_plan_lines(conn, work_date_str)
    barcodes = fetch_barcodes(conn, work_date_str)
    horizon = fetch_horizon_parts(conn, work_date_str)

    bcs_by_truck: Dict[str, List[Dict[str, Any]]] = {}
    for b in barcodes:
        if b.get("assigned_truck_id"):
            bcs_by_truck.setdefault(b["assigned_truck_id"], []).append(b)

    newly_closed_trucks: List[str] = []
    early_warning_candidates: List[Dict[str, Any]] = []

    if gr_available:
        for t in trucks:
            tid = t["truck_id"]
            st = t["status"]
            if st in CLOSED_TRUCK_STATES or st == "NO_PLANNED_LOAD":
                continue

            t_bcs = [b for b in bcs_by_truck.get(tid, []) if not b.get("is_unplanned")]
            matched_cnt = len(t_bcs)
            rec_pcs_tot = sum(float(b["gr_qty"]) for b in t_bcs)
            first_gr = min((b["gr_time"] for b in t_bcs), default=None)
            last_gr = max((b["gr_time"] for b in t_bcs), default=None)
            new_cnt = int(new_by_truck.get(tid, 0))

            first_due_dt = _parse_ts(t["first_poll_due_ts"])
            early_warn_dt = _parse_ts(t["early_warn_due_ts"])
            deadline_dt = _parse_ts(t["no_arrival_deadline_ts"])
            quiet_polls = int(t["quiet_polls"] or 0)
            last_quiet_str = t.get("last_quiet_check_ts")
            closed_ts_str: Optional[str] = t.get("closed_ts")
            next_due_str: Optional[str] = t.get("next_poll_due_ts")

            if bool(t["is_pallet_truck"]):
                # 373 Finding 3 + 374 §0: Pallet Truck is a day-level check (no truck-level
                # TRUCK_NOT_RECEIVED, no early warning, closes only at end of workday 05:59 next day).
                if as_of_ts >= deadline_dt:
                    st = "CLOSED_DAY_LEVEL"
                    closed_ts_str = now_str
                    next_due_str = None
                    newly_closed_trucks.append(tid)
                else:
                    st = "DAY_LEVEL_OPEN"
                    if as_of_ts >= first_due_dt:
                        next_due_str = _fmt_ts(min(as_of_ts + dt.timedelta(minutes=poll_30m), deadline_dt))
                    else:
                        next_due_str = _fmt_ts(first_due_dt)
            elif matched_cnt > 0:
                if st in ("SCHEDULED", "POLLING_30M"):
                    st = "RECEIVING_10M"
                    quiet_polls = 0
                    last_quiet_str = now_str
                    next_due_str = _fmt_ts(as_of_ts + dt.timedelta(minutes=poll_10m))
                elif st == "RECEIVING_10M":
                    if new_cnt > 0:
                        quiet_polls = 0
                        last_quiet_str = now_str
                        next_due_str = _fmt_ts(as_of_ts + dt.timedelta(minutes=poll_10m))
                    else:
                        ref_dt = _parse_ts(last_quiet_str) if last_quiet_str else as_of_ts
                        if (as_of_ts - ref_dt).total_seconds() >= (poll_10m * 60 - 5):
                            quiet_polls += 1
                            last_quiet_str = now_str
                            if quiet_polls >= quiet_needed:
                                st = "CLOSED_RECEIVED"
                                closed_ts_str = now_str
                                next_due_str = None
                                newly_closed_trucks.append(tid)
                            else:
                                next_due_str = _fmt_ts(as_of_ts + dt.timedelta(minutes=poll_10m))
            else:
                if as_of_ts >= deadline_dt:
                    st = "TRUCK_NOT_RECEIVED"
                    closed_ts_str = now_str
                    next_due_str = None
                    newly_closed_trucks.append(tid)
                elif as_of_ts >= first_due_dt:
                    st = "POLLING_30M"
                    next_cand = as_of_ts + dt.timedelta(minutes=poll_30m)
                    next_due_str = _fmt_ts(min(next_cand, deadline_dt))
                    if as_of_ts >= early_warn_dt:
                        early_warning_candidates.append(t)

            conn.execute(
                """
                UPDATE truck_runs
                   SET status = ?,
                       received_boxes_total = ?,
                       received_pcs_total = ?,
                       matched_barcodes_count = ?,
                       quiet_polls = ?,
                       first_receipt_ts = ?,
                       last_receipt_ts = ?,
                       last_quiet_check_ts = ?,
                       last_polled_ts = ?,
                       closed_ts = ?,
                       next_poll_due_ts = ?,
                       updated_at = ?
                 WHERE truck_id = ?
                """,
                (
                    st,
                    matched_cnt,
                    rec_pcs_tot,
                    matched_cnt,
                    quiet_polls,
                    first_gr,
                    last_gr,
                    last_quiet_str,
                    now_str,
                    closed_ts_str,
                    next_due_str,
                    now_str,
                    tid,
                ),
            )
        conn.commit()

    trucks = fetch_truck_runs(conn, work_date_str)
    projections = compute_part_projections(
        run_date,
        horizon,
        trucks,
        plan_lines,
        barcodes,
        buffer_margin=buffer_margin,
        as_of_ts=as_of_ts,
    )

    early_warnings: List[Dict[str, Any]] = []
    for cand_t in early_warning_candidates:
        tid = cand_t["truck_id"]
        cf_proj = compute_part_projections(
            run_date,
            horizon,
            trucks,
            plan_lines,
            barcodes,
            buffer_margin=buffer_margin,
            force_closed_zero_truck_ids={tid},
            as_of_ts=as_of_ts,
        )
        t_lines = [ln for ln in plan_lines if ln["truck_id"] == tid]
        short_parts = []
        for ln in t_lines:
            pk = ln["part_key"]
            if pk not in cf_proj:
                continue
            gap = evaluate_truck_part_gap(
                cf_proj[pk],
                t_slot=int(cand_t["slot_idx"]),
                is_pallet_truck=bool(cand_t["is_pallet_truck"]),
                planned_pcs=float(ln["planned_pcs"]),
                received_pcs=0.0,
                truck_is_closed=True,
            )
            if gap["line_shortage_alarm"]:
                short_parts.append(
                    {
                        "part_no": ln["part_no"],
                        "part_name": ln["part_name"],
                        "planned_pcs": float(ln["planned_pcs"]),
                        "first_short_slot_label": gap["first_short_slot_label"],
                        "deficit_pcs": gap["deficit_before_next_truck_pcs"],
                    }
                )
        if short_parts:
            early_warnings.append({"truck": cand_t, "short_parts": short_parts})

    # Build truck lookup (including synthetic OFF_WINDOW bucket per tracked supplier)
    truck_map: Dict[str, Dict[str, Any]] = {t["truck_id"]: t for t in trucks}
    vendor_by_sup = {t["supplier_key"]: t["vendor_display"] for t in trucks}
    for sup_key, ven_disp in vendor_by_sup.items():
        off_tid = f"{run_date:%Y%m%d}_{sup_key}_OFF_WINDOW"
        if off_tid not in truck_map:
            truck_map[off_tid] = {
                "truck_id": off_tid,
                "work_date": work_date_str,
                "supplier_key": sup_key,
                "vendor_display": ven_disp,
                "truck_label": "OFF_WINDOW",
                "slot_idx": 0,
                "is_pallet_truck": 0,
                "departure_ts": f"{work_date_str} 06:00:00",
                "status": "OFF_WINDOW",
                "closed_ts": None,
            }

    lines_by_tp = {(ln["truck_id"], ln["part_key"]): ln for ln in plan_lines}
    planned_day_by_sup_part: Dict[Tuple[str, str], float] = {}
    for ln in plan_lines:
        k_sp = (ln["supplier_key"], ln["part_key"])
        planned_day_by_sup_part[k_sp] = planned_day_by_sup_part.get(k_sp, 0.0) + float(ln["planned_pcs"])

    prev_initial_alarms = {
        r["verdict_id"]: int(r["initial_alarm_at_close"] or 0)
        for r in conn.execute(
            "SELECT verdict_id, initial_alarm_at_close FROM delivery_verdicts WHERE work_date = ?",
            (work_date_str,),
        ).fetchall()
    }

    bcs_by_tp: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for b in barcodes:
        if b.get("assigned_truck_id"):
            bcs_by_tp.setdefault((b["assigned_truck_id"], b["part_key"]), []).append(b)

    all_keys = sorted(set(lines_by_tp.keys()) | set(bcs_by_tp.keys()))
    staged_verdicts: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for tid, pk in all_keys:
        t = truck_map.get(tid)
        if not t:
            continue
        sup_key = t["supplier_key"]
        ln = lines_by_tp.get((tid, pk))
        tp_bcs = bcs_by_tp.get((tid, pk), [])
        rec_boxes = len(tp_bcs)
        rec_pcs = sum(float(b["gr_qty"]) for b in tp_bcs)
        first_gr = min((b["gr_time"] for b in tp_bcs), default=None)
        last_gr = max((b["gr_time"] for b in tp_bcs), default=None)

        if ln is not None:
            part_no = ln["part_no"]
            part_name = ln["part_name"]
            box_qty = float(ln["box_qty"])
            plan_boxes = float(ln["planned_boxes"])
            plan_pcs = float(ln["planned_pcs"])
        else:
            sample_b = tp_bcs[0]
            hp = horizon.get(pk)
            part_no = hp["part_no"] if hp else sample_b["mat_no"]
            part_name = hp["part_name"] if hp else sample_b["mat_desc"]
            box_qty = float(hp["box_qty"]) if hp else 0.0
            plan_boxes = 0.0
            plan_pcs = 0.0

        diff_pcs = round(rec_pcs - plan_pcs, 2)
        diff_boxes = round((rec_pcs / box_qty) - plan_boxes, 2) if box_qty > 0 else float(rec_boxes - plan_boxes)

        t_closed = t["status"] in CLOSED_TRUCK_STATES
        planned_day_pcs = planned_day_by_sup_part.get((sup_key, pk), 0.0)

        if not gr_available and not t_closed and t["status"] != "OFF_WINDOW":
            verdict = "CANNOT_VERIFY"
        elif ln is None:
            # 373 Finding 1.1: OFF_SCHEDULE if planned anywhere today for this supplier; else UNPLANNED_RECEIPT
            verdict = "OFF_SCHEDULE" if planned_day_pcs > 1e-9 else "UNPLANNED_RECEIPT"
        elif t["status"] == "SCHEDULED":
            verdict = "SCHEDULED"
        elif t["status"] == "DAY_LEVEL_OPEN":
            verdict = "IN_PROGRESS" if rec_pcs > 1e-9 else "DAY_LEVEL_OPEN"
        elif t["status"] in ("POLLING_30M", "RECEIVING_10M"):
            verdict = "IN_PROGRESS" if rec_pcs > 1e-9 else "WAITING_ARRIVAL"
        elif t["status"] == "TRUCK_NOT_RECEIVED":
            verdict = "TRUCK_NOT_RECEIVED"
        else:
            # CLOSED_RECEIVED or CLOSED_DAY_LEVEL: judge per line
            if rec_pcs <= 1e-9:
                verdict = "NOT_DELIVERED"
            elif abs(rec_pcs - plan_pcs) <= 1e-9:
                verdict = "DELIVERED"
            elif rec_pcs < plan_pcs - 1e-9:
                verdict = "SHORT"
            else:
                verdict = "OVER"

        proj = projections.get(pk)
        if proj and ln is not None:
            gap = evaluate_truck_part_gap(
                proj,
                t_slot=int(t["slot_idx"]),
                is_pallet_truck=bool(t["is_pallet_truck"]),
                planned_pcs=plan_pcs,
                received_pcs=rec_pcs,
                truck_is_closed=t_closed,
            )
            short_rcpt = proj["shortage_receipt_basis"] if (gr_available or t_closed) else "CANNOT_VERIFY"
            runout_ro = proj["runout_received_only"] if (gr_available or t_closed) else "CANNOT_VERIFY"
            alarm_flag = 1 if gap["line_shortage_alarm"] else 0
            first_short_lbl = gap["first_short_slot_label"]
            deficit_pcs = gap["deficit_before_next_truck_pcs"]
            buf_flag = 1 if gap["buffer_low_15pct"] else 0
        else:
            short_rcpt = proj["shortage_receipt_basis"] if (proj and gr_available) else (ENOUGH if gr_available else "CANNOT_VERIFY")
            runout_ro = proj["runout_received_only"] if (proj and gr_available) else (ENOUGH if gr_available else "CANNOT_VERIFY")
            alarm_flag = 0
            first_short_lbl = None
            deficit_pcs = 0.0
            buf_flag = 0

        verdict_id = f"{tid}__{pk}"
        init_alarm = max(prev_initial_alarms.get(verdict_id, 0), alarm_flag)
        init_shortfall = round(max(0.0, plan_pcs - rec_pcs), 2) if (ln is not None and t_closed) else 0.0
        hp_for_zm = horizon.get(pk)
        in_zm_flag = int(hp_for_zm.get("start_in_zm212", 1)) if hp_for_zm is not None else 1

        staged_verdicts[(tid, pk)] = {
            "verdict_id": verdict_id,
            "truck_id": tid,
            "work_date": work_date_str,
            "supplier_key": sup_key,
            "truck_label": t["truck_label"],
            "departure_dt": _parse_ts(t["departure_ts"]),
            "part_key": pk,
            "part_no": part_no,
            "part_name": part_name,
            "box_qty": box_qty,
            "planned_boxes": plan_boxes,
            "planned_pcs": plan_pcs,
            "received_boxes": rec_boxes,
            "received_pcs": rec_pcs,
            "diff_pcs": diff_pcs,
            "diff_boxes": diff_boxes,
            "matched_barcodes_count": rec_boxes,
            "first_gr_time": first_gr,
            "last_gr_time": last_gr,
            "verdict": verdict,
            "shortage_receipt_basis": short_rcpt,
            "runout_received_only": runout_ro,
            "line_shortage_alarm": alarm_flag,
            "initial_alarm_at_close": init_alarm,
            "first_short_slot_label": first_short_lbl,
            "deficit_before_next_truck_pcs": deficit_pcs,
            "buffer_low_15pct": buf_flag,
            "recovered_later_pcs": 0.0,
            "open_shortfall_pcs": init_shortfall,
            "recovery_note": "planned today on another truck" if verdict == "OFF_SCHEDULE" else "",
            "start_in_zm212": in_zm_flag,
            "later_events": [],
            "earlier_events": [],
            "has_plan_line": ln is not None,
        }

    # 373 Finding 1.2: Match off-schedule & excess receipts against closed shortfall lines per (supplier, part)
    keys_by_sp: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for (tid, pk), sv in staged_verdicts.items():
        keys_by_sp.setdefault((sv["supplier_key"], pk), []).append((tid, pk))

    for (sup_key, pk), tp_list in keys_by_sp.items():
        shortfall_items = sorted(
            [staged_verdicts[k] for k in tp_list if staged_verdicts[k]["open_shortfall_pcs"] > 1e-9],
            key=lambda x: (x["departure_dt"], x["truck_id"]),
        )
        if not shortfall_items:
            continue

        raw_chunks: List[Tuple[dt.datetime, float]] = []
        for tid, _ in tp_list:
            sv = staged_verdicts[(tid, pk)]
            tp_bcs = sorted(bcs_by_tp.get((tid, pk), []), key=lambda b: (b["gr_time"], b["barcode"]))
            if not sv["has_plan_line"] and sv["verdict"] == "OFF_SCHEDULE":
                for b in tp_bcs:
                    raw_chunks.append((_parse_ts(b["gr_time"]), float(b["gr_qty"])))
            elif sv["has_plan_line"] and sv["received_pcs"] > sv["planned_pcs"] + 1e-9:
                # Excess portion on an OVER planned truck
                cum = 0.0
                plan_q = sv["planned_pcs"]
                for b in tp_bcs:
                    bq = float(b["gr_qty"])
                    prev_cum = cum
                    cum += bq
                    if cum > plan_q + 1e-9:
                        excess = cum - max(plan_q, prev_cum)
                        if excess > 1e-9:
                            raw_chunks.append((_parse_ts(b["gr_time"]), excess))

        chunks = _merge_minute_chunks(raw_chunks)
        for ch_dt, ch_hm, ch_qty in chunks:
            rem = ch_qty
            # Pass 1: Later receipt recovering an earlier closed shortfall (`recovered later: +N at HH:MM`)
            for sf in shortfall_items:
                if rem <= 1e-9:
                    break
                if sf["open_shortfall_pcs"] > 1e-9 and ch_dt > sf["departure_dt"]:
                    alloc = min(rem, sf["open_shortfall_pcs"])
                    sf["recovered_later_pcs"] = round(sf["recovered_later_pcs"] + alloc, 2)
                    sf["open_shortfall_pcs"] = round(max(0.0, sf["open_shortfall_pcs"] - alloc), 2)
                    sf["later_events"].append((ch_hm, alloc))
                    rem = round(rem - alloc, 2)
            # Pass 2: Earlier off-schedule receipt covering a later closed shortfall (`received earlier: +N at HH:MM`)
            for sf in shortfall_items:
                if rem <= 1e-9:
                    break
                if sf["open_shortfall_pcs"] > 1e-9 and ch_dt <= sf["departure_dt"]:
                    alloc = min(rem, sf["open_shortfall_pcs"])
                    sf["recovered_later_pcs"] = round(sf["recovered_later_pcs"] + alloc, 2)
                    sf["open_shortfall_pcs"] = round(max(0.0, sf["open_shortfall_pcs"] - alloc), 2)
                    sf["earlier_events"].append((ch_hm, alloc))
                    rem = round(rem - alloc, 2)

        for sf in shortfall_items:
            note_parts: List[str] = []
            if sf["later_events"]:
                note_parts.append("recovered later: " + _format_event_pairs(sf["later_events"]))
            if sf["earlier_events"]:
                note_parts.append("received earlier: " + _format_event_pairs(sf["earlier_events"]))
            sf["recovery_note"] = "; ".join(note_parts)

    # Persist refreshed `delivery_verdicts` (remove stale rows if any)
    conn.execute("DELETE FROM delivery_verdicts WHERE work_date = ?", (work_date_str,))
    for (tid, pk), sv in staged_verdicts.items():
        conn.execute(
            """
            INSERT INTO delivery_verdicts (
                verdict_id, truck_id, work_date, supplier_key, truck_label,
                part_key, part_no, part_name, box_qty,
                planned_boxes, planned_pcs, received_boxes, received_pcs,
                diff_pcs, diff_boxes, matched_barcodes_count, first_gr_time, last_gr_time,
                verdict, shortage_receipt_basis, runout_received_only,
                line_shortage_alarm, initial_alarm_at_close, first_short_slot_label,
                deficit_before_next_truck_pcs, buffer_low_15pct,
                recovered_later_pcs, open_shortfall_pcs, recovery_note, start_in_zm212, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sv["verdict_id"],
                sv["truck_id"],
                sv["work_date"],
                sv["supplier_key"],
                sv["truck_label"],
                sv["part_key"],
                sv["part_no"],
                sv["part_name"],
                sv["box_qty"],
                sv["planned_boxes"],
                sv["planned_pcs"],
                sv["received_boxes"],
                sv["received_pcs"],
                sv["diff_pcs"],
                sv["diff_boxes"],
                sv["matched_barcodes_count"],
                sv["first_gr_time"],
                sv["last_gr_time"],
                sv["verdict"],
                sv["shortage_receipt_basis"],
                sv["runout_received_only"],
                sv["line_shortage_alarm"],
                sv["initial_alarm_at_close"],
                sv["first_short_slot_label"],
                sv["deficit_before_next_truck_pcs"],
                sv["buffer_low_15pct"],
                sv["recovered_later_pcs"],
                sv["open_shortfall_pcs"],
                sv["recovery_note"],
                sv["start_in_zm212"],
                now_str,
            ),
        )

    conn.commit()
    day_summary = build_day_part_summary(
        run_date,
        horizon,
        trucks,
        plan_lines,
        barcodes,
        projections,
        gr_available=gr_available,
    )
    return {
        "as_of_ts": now_str,
        "gr_available": gr_available,
        "newly_closed_trucks": newly_closed_trucks,
        "early_warnings": early_warnings,
        "projections": projections,
        "day_part_summary": day_summary,
    }
