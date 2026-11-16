"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - Read-Only Plan-of-Record Loader (#351)
==================================================================================
Reads the morning (`06:40` AM) and optional afternoon (`13:40` PM `D+1`) SRM exports
plus morning `ZM212` stock using `00_JOB_SRM_Monitoring`'s pure functions with
`write=False` so ZERO files in `00_JOB_SRM_Monitoring` or `01_DEPT_Inventory` are
ever opened for writing.

Plan-of-record rules (`351_1255` + `371 §3`):
  - Day 0 (`work_date`) truck loads come from the sent `Truck load` plan (`r["plan"]["load"]`).
  - Once a truck has departed (`status != 'SCHEDULED'`), its `truck_plan_lines` are frozen
    and never overwritten by later runs.
  - When `D+1` (`13:40` PM) files become available, Days 1..2 (`slots[12:36]`) of
    `plan_horizon_parts` are updated so 3-day shortage projections reflect tomorrow's
    confirmed SRM schedule, and any still-`SCHEDULED` future trucks (`departure_ts > as_of_ts`)
    refresh cleanly.
  - Every `part_no` carries a non-blank `part_name` via `material_desc_helper.get_material_desc` (#279).
"""
from __future__ import annotations

import datetime as dt
import json
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

from material_desc_helper import get_material_desc  # noqa: E402
from run_srm_monitoring import (  # noqa: E402
    CLASS_FILE,
    INBOX_DIR,
    WATCHLIST,
    find_inputs,
    infer_classes,
    learn_classes,
    load_watchlist,
    zm212_file,
)
from srm_parsers import N_SLOTS, SLOT_LABELS, parse_hourly, parse_plan_require, read_rows, read_zm212_stock  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_projection import md, merge_daily, merge_hourly, project_6d, slot_start  # noqa: E402
from srm_trucks import leveling_config, load_suppliers, pm_additional_days, run_days  # noqa: E402


def _resolve_Operational_srm_dir() -> Path:
    """When running inside a git worktree without gitignored data/inbox, fall back to main checkout read-only."""
    if (SRM_DIR / "inbox").exists() or (SRM_DIR / "data" / "suppliers.csv").exists():
        return SRM_DIR
    for cand_parent in BASE_DIR.parents:
        main_srm = cand_parent.parent / "CODES" / "01_M365MCP" / "02_DEPT_Logistics" / "00_JOB_SRM_Monitoring"
        if main_srm.exists():
            return main_srm
    return SRM_DIR


def _resolve_operational_zm212(run_date: dt.date, explicit_zm212: Optional[Path] = None) -> Path:
    if explicit_zm212 is not None:
        return Path(explicit_zm212)
    cand = zm212_file(run_date)
    if cand.exists():
        return cand
    for cand_parent in BASE_DIR.parents:
        main_zm = (
            cand_parent.parent
            / "CODES"
            / "01_M365MCP"
            / "01_DEPT_Inventory"
            / "04_JOB_SystemReconciliation"
            / "raw_exports"
            / f"ZM212_StockAccuracy_IWMS_{run_date:%Y%m%d}.txt"
        )
        if main_zm.exists():
            return main_zm
    return cand


def match_supplier_key(vendor: str, suppliers_rows: List[Dict[str, Any]]) -> Optional[str]:
    v = (vendor or "").lower()
    for row in suppliers_rows:
        m = (row.get("match") or "").strip()
        if m and m.lower() in v:
            return m
    return None


def compute_srm_plan_readonly(
    run_date: dt.date,
    *,
    inbox_dir: Optional[Path] = None,
    zm212_path: Optional[Path] = None,
    suppliers_csv: Optional[Path] = None,
    trucks_json: Optional[Path] = None,
    watchlist_csv: Optional[Path] = None,
    class_csv: Optional[Path] = None,
    include_d1: bool = True,
) -> Dict[str, Any]:
    """
    Pure read-only computation of `res6` with `r['plan']` populated.
    Writes ZERO files anywhere on disk (`learn_classes(..., write=False)`).
    """
    op_srm = _resolve_Operational_srm_dir()
    inbox = Path(inbox_dir) if inbox_dir else (op_srm / "inbox" / f"{run_date:%Y%m%d}")
    zm212 = _resolve_operational_zm212(run_date, zm212_path)
    sup_path = Path(suppliers_csv) if suppliers_csv else (op_srm / "data" / "suppliers.csv")
    trk_path = Path(trucks_json) if trucks_json else (op_srm / "config" / "trucks.json")
    wl_path = Path(watchlist_csv) if watchlist_csv else (op_srm / "data" / "watchlist.csv")
    cls_path = Path(class_csv) if class_csv else (op_srm / "data" / "part_class.csv")

    files = find_inputs(inbox)
    missing = [k for k in ("order", "require", "planreq") if not files[k]]
    if missing:
        return {"status": "MISSING_SRM_INPUT", "missing": missing, "inbox": str(inbox), "rows": [], "suppliers": []}
    if not zm212.exists():
        return {"status": "MISSING_ZM212", "zm212": str(zm212), "rows": [], "suppliers": []}

    o_lab_m, orders_m = parse_hourly(read_rows(files["order"]))
    r_lab_m, requires_m = parse_hourly(read_rows(files["require"]))
    labels2w, planreq = parse_plan_require(read_rows(files["planreq"]))

    input_dates = {
        "order": o_lab_m[0],
        "require": r_lab_m[0],
        "plan_require": (labels2w[0] if labels2w else "").replace(" SUM", "").strip(),
    }
    wrong = [k for k, v in input_dates.items() if v != md(run_date)]
    if wrong:
        return {
            "status": "WRONG_INPUT_DATE",
            "expected": md(run_date),
            "wrong": wrong,
            "input_dates": input_dates,
            "rows": [],
            "suppliers": [],
        }

    stock = read_zm212_stock(read_rows(zm212))
    orders, requires = orders_m, requires_m
    orders_d1 = requires_d1 = None
    use_d1 = False

    if include_d1:
        d1 = find_inputs(inbox / "D+1")
        if d1["order"] and d1["require"]:
            o_lab, orders_d1 = parse_hourly(read_rows(d1["order"]))
            r_lab, requires_d1 = parse_hourly(read_rows(d1["require"]))
            orders, used_o = merge_hourly(run_date, orders_m, orders_d1, o_lab)
            requires, used_r = merge_hourly(run_date, requires_m, requires_d1, r_lab)
            use_d1 = bool(used_o and used_r)
            if not use_d1:
                orders, requires = orders_m, requires_m
        if d1["planreq"]:
            p_lab, p1 = parse_plan_require(read_rows(d1["planreq"]))
            labels2w, planreq_m, used_p = merge_daily(run_date, labels2w, planreq, p_lab, p1)
            if used_p:
                for k, r in p1.items():
                    planreq_m.setdefault(k, r)
                planreq = planreq_m

    wl = load_watchlist(wl_path)
    seen = {k: r.get("cls", "") for k, r in planreq.items()}
    for src in (orders_m, orders_d1 or {}):
        seen.update({k: r["cls"].upper() for k, r in src.items() if r["cls"].upper() in ("C", "L")})
    cls = learn_classes(seen, path=cls_path, write=False)
    multi = [k for k, c in cls.items() if c == "C"]
    cls_run, _ = infer_classes(cls, orders)

    all6 = list(
        dict.fromkeys(
            list(orders)
            + list(orders_d1 or {})
            + [k for k, r in requires.items() if r["cls"].upper() == "6D"]
            + multi
        )
    )
    p6 = list(dict.fromkeys(wl["6D"] + all6)) if wl else all6

    res6 = project_6d(run_date, p6, orders, requires, stock, {})
    for r in res6:
        src = (orders_d1 or {}).get(r["key"]) or (requires_d1 or {}).get(r["key"]) or planreq.get(r["key"])
        if src and not r["vendor"]:
            r.update(vendor=src["vendor"], name=src["name"], part=src["part"])
        r["start_in_zm212"] = bool(r["key"] in stock)
        r["start_wms"] = float(stock[r["key"]]["wms"]) if r["key"] in stock else 0.0

    # Guarantee non-blank Part Name everywhere (#279)
    missing_desc_parts = [r["part"] for r in res6 if not (r.get("name") or "").strip()]
    fallback_descs = get_material_desc(missing_desc_parts) if missing_desc_parts else {}
    for r in res6:
        if not (r.get("name") or "").strip():
            r["name"] = fallback_descs.get(r["part"], "(no description)")

    box_map = {k: float(r.get("box_qty") or 0.0) for k, r in planreq.items()}
    suppliers_rows = load_suppliers(sup_path)
    trucks_cfg = {r["match"]: r["trucks"] for r in suppliers_rows if r.get("trucks")}
    pm_extra = pm_additional_days(trk_path)
    days_cfg = run_days(trk_path)

    plan_parts(
        run_date,
        res6,
        orders_m,
        requires_m,
        orders_d1,
        requires_d1,
        use_d1,
        trucks_cfg,
        box_map,
        cls_run,
        pm_extra,
        days_cfg,
        level_cfg=leveling_config(trk_path, suppliers_path=sup_path if sup_path.exists() else None),
    )

    return {
        "status": "OK",
        "run_date": run_date.isoformat(),
        "plan_source": "PM_MERGED" if use_d1 else "AM",
        "inbox": str(inbox),
        "zm212": str(zm212),
        "rows": res6,
        "box_qty": box_map,
        "suppliers": suppliers_rows,
        "trucks_cfg": trucks_cfg,
    }


def _parse_pallet_departure(run_date: dt.date, hhmm: str) -> dt.datetime:
    """
    Parses `pallet_departure` (`"12:00"` from `374 §0`) into `departure_dt` for display and later use.
    Per `374 §0`, `pallet_departure` is NOT used for slot shifting or verdicts yet; Class L remains
    a pure day-level check (`slot_idx = 0` day bucket matching `Delivery_TOP_6D` `8-10` / `Total`).
    """
    raw = (hhmm or "12:00").strip()
    parts = raw.split(":")
    hh = int(parts[0])
    mm = int(parts[1]) if len(parts) > 1 else 0
    base_day = run_date + dt.timedelta(days=1 if hh < 6 else 0)
    return dt.datetime.combine(base_day, dt.time(hh, mm, 0))


def sync_plan_to_ledger(
    conn,
    run_date: dt.date,
    plan_bundle: Dict[str, Any],
    cfg: Dict[str, Any],
    as_of_ts: dt.datetime,
) -> Dict[str, Any]:
    """
    Persists `plan_horizon_parts`, `truck_runs`, and `truck_plan_lines` into `delivery_check.db`.
    Immutable rule: any `truck_run` already past `SCHEDULED` (`POLLING_30M`, `RECEIVING_10M`,
    `CLOSED_RECEIVED`, `TRUCK_NOT_RECEIVED`) keeps its frozen `truck_plan_lines`.
    """
    work_date_str = run_date.isoformat()
    now_str = as_of_ts.strftime("%Y-%m-%d %H:%M:%S")
    rows: List[Dict[str, Any]] = plan_bundle["rows"]
    box_map: Dict[str, float] = plan_bundle["box_qty"]
    suppliers_rows: List[Dict[str, Any]] = plan_bundle["suppliers"]
    trucks_cfg: Dict[str, List[int]] = plan_bundle["trucks_cfg"]
    plan_source: str = plan_bundle["plan_source"]

    first_poll_min = int(cfg.get("first_poll_after_departure_min", 30))
    early_warn_min = int(cfg.get("early_warning_after_departure_min", 90))
    no_arrival_cap_min = int(cfg.get("no_arrival_cap_min", 180))
    next_truck_grace_min = int(cfg.get("next_truck_grace_min", 30))
    pallet_dep_hhmm = str(cfg.get("pallet_departure", "12:00"))
    pallet_dep_dt = _parse_pallet_departure(run_date, pallet_dep_hhmm)
    pallet_slot_idx = 0  # Day-level bucket (`8-10` / Day Total, matching `Delivery_TOP_6D`; 12:00 is display-only per 374 §0)
    pallet_end_of_day_dt = slot_start(run_date, 1, 0) - dt.timedelta(minutes=1)

    # If no suppliers.csv exists (e.g. synthetic test without suppliers.csv), derive supplier keys from rows
    if not suppliers_rows:
        seen_vendors = sorted({r["vendor"] for r in rows if r.get("vendor")})
        suppliers_rows = [{"match": v, "trucks": [], "mail": False, "to_env": "", "cc_env": ""} for v in seen_vendors]

    existing_trucks = {
        r["truck_id"]: dict(r)
        for r in conn.execute("SELECT * FROM truck_runs WHERE work_date = ?", (work_date_str,)).fetchall()
    }

    # 1. Upsert 36-slot horizon per part (Days 0..2 = 36 slots)
    by_supplier: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        p = r.get("plan")
        if not p or not r.get("vendor"):
            continue
        sup_key = match_supplier_key(r["vendor"], suppliers_rows) or r["vendor"]
        box = float(box_map.get(r["key"], 0.0) or 0.0)
        cls_code = p.get("cls") or "C"
        load_36 = [float(x) for x in p["load"][: 3 * N_SLOTS]]
        req_36 = [float(x) for x in p["req"][: 3 * N_SLOTS]]
        srm_36 = [float(x) for x in p["srm"][: 3 * N_SLOTS]]
        extra_36 = [float(x) for x in p["extra"][: 3 * N_SLOTS]]
        add_3 = [float(x) for x in p["add"][:3]]

        # 377 §1.5a: Freeze Day-0 slot loads for any slot whose departure <= as_of_ts or whose truck left SCHEDULED
        prev_part = conn.execute(
            "SELECT load_json FROM plan_horizon_parts WHERE work_date = ? AND part_key = ?",
            (work_date_str, r["key"]),
        ).fetchone()
        if prev_part:
            prev_load = [float(x) for x in json.loads(prev_part["load_json"])]
            if cls_code == "L":
                pal_tid = f"{run_date:%Y%m%d}_{sup_key}_PALLET"
                ex_pal = existing_trucks.get(pal_tid)
                if ex_pal and (ex_pal["departure_ts"] <= now_str or ex_pal["status"] != "DAY_LEVEL_OPEN"):
                    load_36 = prev_load[:N_SLOTS] + load_36[N_SLOTS:]
            else:
                for s_i in range(N_SLOTS):
                    s_dep = slot_start(run_date, 0, s_i).strftime("%Y-%m-%d %H:%M:%S")
                    s_tid = f"{run_date:%Y%m%d}_{sup_key}_{SLOT_LABELS[s_i]}"
                    ex_trk = existing_trucks.get(s_tid)
                    if s_dep <= now_str or (ex_trk and ex_trk["status"] not in ("SCHEDULED", "NO_PLANNED_LOAD")):
                        load_36[s_i] = prev_load[s_i]

        conn.execute(
            """
            INSERT INTO plan_horizon_parts (
                work_date, part_key, part_no, part_name, vendor_display, supplier_key,
                cls, box_qty, start_sap, start_wms, start_in_zm212,
                load_json, req_json, srm_json, extra_json, add_json, plan_source, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(work_date, part_key) DO UPDATE SET
                part_name = excluded.part_name,
                box_qty = excluded.box_qty,
                start_in_zm212 = excluded.start_in_zm212,
                load_json = excluded.load_json,
                req_json = excluded.req_json,
                srm_json = excluded.srm_json,
                extra_json = excluded.extra_json,
                add_json = excluded.add_json,
                plan_source = excluded.plan_source,
                updated_at = excluded.updated_at
            """,
            (
                work_date_str,
                r["key"],
                r["part"],
                r["name"],
                r["vendor"],
                sup_key,
                cls_code,
                box,
                float(r.get("start_sap") or 0.0),
                float(r.get("start_wms") or 0.0),
                1 if r.get("start_in_zm212", True) else 0,
                json.dumps(load_36),
                json.dumps(req_36),
                json.dumps(srm_36),
                json.dumps(extra_36),
                json.dumps(add_3),
                plan_source,
                now_str,
            ),
        )
        by_supplier.setdefault(sup_key, []).append(r)

    # 2. Build Day-0 truck specifications per configured supplier
    created_trucks = 0
    frozen_kept = 0
    for sup_row in suppliers_rows:
        sup_key = sup_row["match"]
        sup_parts = by_supplier.get(sup_key, [])
        if not sup_parts:
            continue
        vendor_display = sup_parts[0]["vendor"]
        fixed_slots: List[int] = list(trucks_cfg.get(sup_key, []))

        c_parts = [r for r in sup_parts if (r["plan"].get("cls") or "C") != "L"]
        l_parts = [r for r in sup_parts if (r["plan"].get("cls") or "") == "L"]

        # Candidate Day-0 slot trucks for Class C (fixed slots + any non-zero 'Other' slot)
        slot_specs: List[Tuple[str, int, int, List[Tuple[Dict[str, Any], float, float, float]]]] = []
        candidate_slots = sorted(
            set(fixed_slots)
            | {
                s
                for r in c_parts
                for s in range(N_SLOTS)
                if float(r["plan"]["load"][s]) > 1e-9
            }
        )
        for s_idx in candidate_slots:
            t_label = SLOT_LABELS[s_idx]
            lines = []
            for r in c_parts:
                p = r["plan"]
                q_load = float(p["load"][s_idx])
                if q_load > 1e-9:
                    q_srm = float(p["srm"][s_idx]) + (float(p["add"][0]) if (s_idx == 0 and not fixed_slots) else 0.0)
                    q_ext = float(p["extra"][s_idx])
                    lines.append((r, q_srm, q_ext, q_load))
            slot_specs.append((t_label, s_idx, 0, lines))

        # Candidate Pallet Truck for once-a-day Class L parts (day-level check, 12:00 display per 373 Finding 3 & 374 §0)
        pallet_lines = []
        for r in l_parts:
            p = r["plan"]
            q_load = float(sum(p["load"][:N_SLOTS]))
            if q_load > 1e-9:
                q_srm = float(sum(p["srm"][:N_SLOTS])) + float(p["add"][0])
                q_ext = float(sum(p["extra"][:N_SLOTS]))
                pallet_lines.append((r, q_srm, q_ext, q_load))
        if pallet_lines:
            slot_specs.append(("PALLET", pallet_slot_idx, 1, pallet_lines))

        # Sort supplier's Day-0 trucks by departure slot so we can compute next-same-supplier-truck deadline
        slot_specs.sort(key=lambda x: (x[1], x[2]))
        active_departures: List[Tuple[str, int, dt.datetime]] = [
            (lbl, is_p, pallet_dep_dt if is_p else slot_start(run_date, 0, s_idx))
            for lbl, s_idx, is_p, lns in slot_specs
            if lns
        ]
        # Next-day first truck departure fallback (capped at +180 min anyway)
        next_day_first_dep = (
            slot_start(run_date, 1, fixed_slots[0])
            if fixed_slots
            else slot_start(run_date, 1, 0)
        )

        for t_label, s_idx, is_pallet, lines in slot_specs:
            truck_id = f"{run_date:%Y%m%d}_{sup_key}_{t_label}"
            if is_pallet:
                dep_dt = pallet_dep_dt
                first_poll_dt = dep_dt + dt.timedelta(minutes=first_poll_min)
                deadline_dt = pallet_end_of_day_dt
                early_warn_dt = pallet_end_of_day_dt
            else:
                dep_dt = slot_start(run_date, 0, s_idx)
                first_poll_dt = dep_dt + dt.timedelta(minutes=first_poll_min)
                early_warn_dt = dep_dt + dt.timedelta(minutes=early_warn_min)
                later_deps = [
                    d_ts
                    for _, is_p, d_ts in active_departures
                    if is_p == 0 and d_ts > dep_dt
                ]
                next_same_sup_dep = later_deps[0] if later_deps else next_day_first_dep
                deadline_dt = min(
                    next_same_sup_dep + dt.timedelta(minutes=next_truck_grace_min),
                    dep_dt + dt.timedelta(minutes=no_arrival_cap_min),
                )

            existing = existing_trucks.get(truck_id)
            if existing and (
                existing["departure_ts"] <= now_str
                or existing["status"] not in ("SCHEDULED", "NO_PLANNED_LOAD", "DAY_LEVEL_OPEN")
            ):
                frozen_kept += 1
                continue

            planned_pcs_total = sum(x[3] for x in lines)
            planned_boxes_total = 0.0
            for r, _, _, q_load in lines:
                bx = float(box_map.get(r["key"], 0.0) or 0.0)
                planned_boxes_total += (q_load / bx) if bx > 0 else 0.0

            if not lines:
                init_status = "NO_PLANNED_LOAD"
            elif is_pallet:
                init_status = "DAY_LEVEL_OPEN"
            else:
                init_status = "SCHEDULED"
            next_due = first_poll_dt.strftime("%Y-%m-%d %H:%M:%S") if lines else None

            conn.execute(
                """
                INSERT INTO truck_runs (
                    truck_id, work_date, supplier_key, vendor_display, truck_label,
                    slot_idx, is_pallet_truck, departure_ts, first_poll_due_ts,
                    early_warn_due_ts, no_arrival_deadline_ts, status,
                    planned_parts_count, planned_boxes_total, planned_pcs_total,
                    next_poll_due_ts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(truck_id) DO UPDATE SET
                    departure_ts = excluded.departure_ts,
                    first_poll_due_ts = excluded.first_poll_due_ts,
                    early_warn_due_ts = excluded.early_warn_due_ts,
                    no_arrival_deadline_ts = excluded.no_arrival_deadline_ts,
                    status = excluded.status,
                    planned_parts_count = excluded.planned_parts_count,
                    planned_boxes_total = excluded.planned_boxes_total,
                    planned_pcs_total = excluded.planned_pcs_total,
                    next_poll_due_ts = excluded.next_poll_due_ts,
                    updated_at = excluded.updated_at
                """,
                (
                    truck_id,
                    work_date_str,
                    sup_key,
                    vendor_display,
                    t_label,
                    s_idx,
                    is_pallet,
                    dep_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    first_poll_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    early_warn_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    deadline_dt.strftime("%Y-%m-%d %H:%M:%S"),
                    init_status,
                    len(lines),
                    round(planned_boxes_total, 2),
                    planned_pcs_total,
                    next_due,
                    now_str,
                    now_str,
                ),
            )

            conn.execute("DELETE FROM truck_plan_lines WHERE truck_id = ?", (truck_id,))
            for r, q_srm, q_ext, q_load in lines:
                bx = float(box_map.get(r["key"], 0.0) or 0.0)
                p_boxes = round(q_load / bx, 2) if bx > 0 else 0.0
                line_id = f"{truck_id}__{r['key']}"
                conn.execute(
                    """
                    INSERT INTO truck_plan_lines (
                        line_id, truck_id, work_date, supplier_key, vendor_display,
                        truck_label, slot_idx, part_key, part_no, part_name, cls,
                        box_qty, srm_pcs, extra_pcs, planned_pcs, planned_boxes, frozen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        line_id,
                        truck_id,
                        work_date_str,
                        sup_key,
                        vendor_display,
                        t_label,
                        s_idx,
                        r["key"],
                        r["part"],
                        r["name"],
                        r["plan"].get("cls") or "C",
                        bx,
                        q_srm,
                        q_ext,
                        q_load,
                        p_boxes,
                        now_str,
                    ),
                )
            created_trucks += 1

    conn.commit()
    return {
        "work_date": work_date_str,
        "plan_source": plan_source,
        "parts_registered": len(rows),
        "trucks_synced": created_trucks,
        "frozen_trucks_preserved": frozen_kept,
    }
