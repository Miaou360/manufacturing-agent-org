"""Minimal-change, pull-forward truck load leveling logic.

Rules:
  - Per-truck limit = min(trailer_cap_carts, overload_factor * day_average_carts).
  - Only change a supplier's orders when at least one truck exceeds that limit.
  - Pull orders to earlier truck slots only, never push to later slots.
  - Move the largest box-count lines first to minimize the number of changed lines.
  - Destinations stay within the same per-truck limit.
  - Preserve per-material-per-day totals and never allow projected stock to drop below zero.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LevelingResult:
    """Output of truck load leveling plus verification metrics."""

    order_lines: list[dict[str, Any]]
    truck_runs: list[dict[str, Any]]
    planned_carts_by_truck: dict[str, int]
    effective_limit_carts: int
    changed_lines: int
    overload_triggered: bool
    pushed_later_count: int
    min_projected_stock: float
    portal_totals: dict[tuple[str, str], int]
    plan_totals: dict[tuple[str, str], int]
    moves: list[dict[str, Any]]


def _slot_key(truck_run: dict[str, Any]) -> tuple[str, str]:
    return (str(truck_run["run_date"]), str(truck_run["slot"]))


def totals_by_material_day(
    lines: list[dict[str, Any]],
    run_date_map: dict[str, str],
) -> dict[tuple[str, str], int]:
    """Aggregate ordered boxes by (material_id, run_date)."""
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for line in lines:
        r_id = str(line["truck_run_id"])
        r_date = run_date_map.get(r_id, str(line.get("due_window", ""))[:10])
        totals[(str(line["material_id"]), r_date)] += int(line["ordered_boxes"])
    return dict(totals)


def compute_min_projected_stock(
    lines: list[dict[str, Any]],
    ordered_runs: list[dict[str, Any]],
    starting_stock_boxes: dict[str, float] | None,
    slot_demand_boxes: dict[tuple[str, str], float] | None,
) -> float:
    """Simulate end-of-slot stock across ordered truck runs for every material."""
    if not ordered_runs:
        return 0.0
    materials = sorted({str(line["material_id"]) for line in lines})
    arrivals: dict[tuple[str, str], float] = defaultdict(float)
    for line in lines:
        arrivals[(str(line["material_id"]), str(line["truck_run_id"]))] += float(line["ordered_boxes"])

    min_inv = float("inf")
    start_map = starting_stock_boxes or {}
    demand_map = slot_demand_boxes or {}
    for mat in materials:
        inv = float(start_map.get(mat, 0.0))
        for run in ordered_runs:
            rid = str(run["truck_run_id"])
            inv += arrivals.get((mat, rid), 0.0) - float(demand_map.get((mat, rid), 0.0))
            min_inv = min(min_inv, inv)
    return 0.0 if min_inv == float("inf") else round(min_inv, 4)


def level_truck_orders(
    *,
    order_lines: list[dict[str, Any]],
    truck_runs: list[dict[str, Any]],
    trailer_cap_carts: int,
    overload_factor: float = 1.7,
    starting_stock_boxes: dict[str, float] | None = None,
    slot_demand_boxes: dict[tuple[str, str], float] | None = None,
) -> LevelingResult:
    """Level one supplier's delivery order lines across fixed truck runs."""
    ordered_runs = sorted([dict(r) for r in truck_runs], key=_slot_key)
    run_index = {str(r["truck_run_id"]): idx for idx, r in enumerate(ordered_runs)}
    run_date_map = {str(r["truck_run_id"]): str(r["run_date"]) for r in ordered_runs}
    run_slot_map = {str(r["truck_run_id"]): str(r["slot"]) for r in ordered_runs}

    baseline_lines = [dict(line) for line in order_lines]
    portal_totals = totals_by_material_day(baseline_lines, run_date_map)

    lines: list[dict[str, Any]] = []
    for line in order_lines:
        copy_ln = dict(line)
        orig_rid = str(copy_ln["truck_run_id"])
        copy_ln["portal_slot"] = run_slot_map.get(orig_rid, str(copy_ln.get("due_window", ""))[-5:])
        copy_ln["slot"] = copy_ln["portal_slot"]
        copy_ln["changed"] = False
        copy_ln["flag"] = ""
        lines.append(copy_ln)

    carts_by_run: dict[str, int] = {str(r["truck_run_id"]): 0 for r in ordered_runs}
    for line in lines:
        rid = str(line["truck_run_id"])
        carts_by_run[rid] = carts_by_run.get(rid, 0) + int(line["ordered_boxes"])

    # Compute per-day effective limit: min(cap, 1.7 * day's average truck load)
    runs_by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in ordered_runs:
        runs_by_day[str(run["run_date"])].append(run)

    day_limit: dict[str, int] = {}
    overload_triggered = False
    for r_date, d_runs in runs_by_day.items():
        total_day = sum(carts_by_run[str(r["truck_run_id"])] for r in d_runs)
        avg_day = total_day / max(1, len(d_runs))
        if total_day > 0:
            raw_limit = min(float(trailer_cap_carts), overload_factor * avg_day)
            eff = max(1, int(math.floor(raw_limit + 1e-9)))
        else:
            eff = trailer_cap_carts
        day_limit[r_date] = eff
        if any(carts_by_run[str(r["truck_run_id"])] > eff for r in d_runs):
            overload_triggered = True

    moves: list[dict[str, Any]] = []
    changed_indices: set[int] = set()

    if overload_triggered:
        for src_run in ordered_runs:
            src_id = str(src_run["truck_run_id"])
            src_date = str(src_run["run_date"])
            src_idx = run_index[src_id]
            limit = day_limit[src_date]

            while carts_by_run[src_id] > limit:
                earlier_runs = [
                    str(r["truck_run_id"])
                    for r in ordered_runs[:src_idx]
                    if str(r["run_date"]) == src_date
                    and carts_by_run[str(r["truck_run_id"])] < limit
                ]
                if not earlier_runs:
                    break

                src_line_indices = sorted(
                    [i for i, ln in enumerate(lines) if str(ln["truck_run_id"]) == src_id],
                    key=lambda i: (-int(lines[i]["ordered_boxes"]), str(lines[i]["order_no"])),
                )

                moved_one = False
                for line_idx in src_line_indices:
                    boxes = int(lines[line_idx]["ordered_boxes"])
                    fitting = sorted(
                        [dst for dst in earlier_runs if carts_by_run[dst] + boxes <= limit],
                        key=lambda dst: (carts_by_run[dst], run_index[dst]),
                    )
                    if not fitting:
                        continue
                    dst_id = fitting[0]
                    lines[line_idx]["truck_run_id"] = dst_id
                    lines[line_idx]["slot"] = run_slot_map.get(dst_id, "")
                    lines[line_idx]["changed"] = True
                    carts_by_run[src_id] -= boxes
                    carts_by_run[dst_id] += boxes
                    changed_indices.add(line_idx)
                    moves.append(
                        {
                            "order_no": lines[line_idx]["order_no"],
                            "material_id": lines[line_idx]["material_id"],
                            "boxes": boxes,
                            "from_truck_run_id": src_id,
                            "to_truck_run_id": dst_id,
                        }
                    )
                    moved_one = True
                    break

                if not moved_one:
                    if not src_line_indices:
                        break
                    dst_id = min(earlier_runs, key=lambda dst: (carts_by_run[dst], run_index[dst]))
                    room = limit - carts_by_run[dst_id]
                    if room <= 0:
                        break
                    line_idx = src_line_indices[0]
                    excess = carts_by_run[src_id] - limit
                    move_boxes = min(room, excess, int(lines[line_idx]["ordered_boxes"]) - 1)
                    if move_boxes <= 0:
                        break
                    lines[line_idx]["ordered_boxes"] = int(lines[line_idx]["ordered_boxes"]) - move_boxes
                    lines[line_idx]["changed"] = True
                    split_line = dict(lines[line_idx])
                    split_line["order_no"] = f"{lines[line_idx]['order_no']}-PF"
                    split_line["ordered_boxes"] = move_boxes
                    split_line["truck_run_id"] = dst_id
                    split_line["slot"] = run_slot_map.get(dst_id, "")
                    split_line["changed"] = True
                    lines.append(split_line)
                    carts_by_run[src_id] -= move_boxes
                    carts_by_run[dst_id] += move_boxes
                    changed_indices.add(line_idx)
                    moves.append(
                        {
                            "order_no": split_line["order_no"],
                            "material_id": split_line["material_id"],
                            "boxes": move_boxes,
                            "from_truck_run_id": src_id,
                            "to_truck_run_id": dst_id,
                        }
                    )

    pushed_later = 0
    baseline_idx_by_order = {
        (str(ln["order_no"]), str(ln["material_id"])): run_index[str(ln["truck_run_id"])]
        for ln in baseline_lines
        if str(ln["truck_run_id"]) in run_index
    }
    for ln in lines:
        base_order = str(ln["order_no"]).removesuffix("-PF")
        key = (base_order, str(ln["material_id"]))
        if key in baseline_idx_by_order and str(ln["truck_run_id"]) in run_index:
            if run_index[str(ln["truck_run_id"])] > baseline_idx_by_order[key]:
                pushed_later += 1

    updated_runs = []
    for run in ordered_runs:
        r_copy = dict(run)
        r_copy["planned_carts"] = carts_by_run[str(r_copy["truck_run_id"])]
        updated_runs.append(r_copy)

    plan_totals = totals_by_material_day(lines, run_date_map)
    min_proj = compute_min_projected_stock(lines, updated_runs, starting_stock_boxes, slot_demand_boxes)
    eff_limit = min(day_limit.values()) if day_limit else trailer_cap_carts

    return LevelingResult(
        order_lines=lines,
        truck_runs=updated_runs,
        planned_carts_by_truck=carts_by_run,
        effective_limit_carts=eff_limit,
        changed_lines=len(changed_indices),
        overload_triggered=overload_triggered,
        pushed_later_count=pushed_later,
        min_projected_stock=min_proj,
        portal_totals=portal_totals,
        plan_totals=plan_totals,
        moves=moves,
    )
