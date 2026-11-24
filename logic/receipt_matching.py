"""Goods receipt matching to fixed supplier truck runs and delivery status verdicts.

Verdict vocabulary:
  - DELIVERED         : received boxes match planned boxes
  - SHORT             : received > 0 but fewer than planned
  - OVER              : received more boxes than planned
  - NOT_DELIVERED     : planned > 0 and zero boxes received
  - UNPLANNED_RECEIPT : boxes received with no planned order line on that truck
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

DELIVERY_VERDICTS = frozenset(
    {"DELIVERED", "SHORT", "OVER", "NOT_DELIVERED", "UNPLANNED_RECEIPT"}
)


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))


def _slot_datetime(run_date: str, slot: str) -> datetime:
    return datetime.fromisoformat(f"{run_date}T{slot}:00")


def match_receipts_to_trucks(
    *,
    receipts: list[dict[str, Any]],
    truck_runs: list[dict[str, Any]],
    order_lines: list[dict[str, Any]] | None = None,
    material_suppliers: dict[str, str] | None = None,
    early_minutes: int = 30,
    late_minutes: int = 120,
) -> list[dict[str, Any]]:
    """Assign each goods receipt to the truck run whose arrival window contains it."""
    mat_to_suppliers: dict[str, set[str]] = defaultdict(set)
    for line in order_lines or ():
        mat_to_suppliers[str(line["material_id"])].add(str(line["supplier_id"]))
    for mat_id, sup_id in (material_suppliers or {}).items():
        mat_to_suppliers[str(mat_id)].add(str(sup_id))

    runs_by_supplier: dict[str, list[tuple[datetime, dict[str, Any]]]] = defaultdict(list)
    all_runs: list[tuple[datetime, dict[str, Any]]] = []
    for run in truck_runs:
        slot_dt = _slot_datetime(str(run["run_date"]), str(run["slot"]))
        entry = (slot_dt, run)
        runs_by_supplier[str(run["supplier_id"])].append(entry)
        all_runs.append(entry)

    early_delta = timedelta(minutes=early_minutes)
    late_delta = timedelta(minutes=late_minutes)

    matched: list[dict[str, Any]] = []
    for rec in receipts:
        row = dict(rec)
        recv_dt = _parse_dt(str(row["received_at"]))
        mat_id = str(row["material_id"])
        candidate_suppliers = mat_to_suppliers.get(mat_id)
        if candidate_suppliers:
            candidates = [
                item
                for sup in sorted(candidate_suppliers)
                for item in runs_by_supplier.get(sup, ())
            ]
        else:
            candidates = all_runs

        in_window = [
            (abs((recv_dt - slot_dt).total_seconds()), str(run["truck_run_id"]))
            for slot_dt, run in candidates
            if (slot_dt - early_delta) <= recv_dt <= (slot_dt + late_delta)
        ]
        if in_window:
            in_window.sort(key=lambda pair: (pair[0], pair[1]))
            row["truck_run_id"] = in_window[0][1]
        else:
            row["truck_run_id"] = None
        matched.append(row)
    return matched


def summarize_delivery_status(
    *,
    order_lines: list[dict[str, Any]],
    matched_receipts: list[dict[str, Any]],
    truck_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Compare planned boxes per (supplier, truck_run, material) against received boxes."""
    run_supplier = {str(r["truck_run_id"]): str(r["supplier_id"]) for r in (truck_runs or ())}

    planned: dict[tuple[str, str, str], int] = defaultdict(int)
    for line in order_lines:
        rid = str(line["truck_run_id"])
        sup_id = str(line["supplier_id"])
        run_supplier.setdefault(rid, sup_id)
        planned[(sup_id, rid, str(line["material_id"]))] += int(line["ordered_boxes"])

    received: dict[tuple[str, str, str], int] = defaultdict(int)
    for rec in matched_receipts:
        rid = str(rec.get("truck_run_id") or "")
        if not rid:
            continue
        sup_id = run_supplier.get(rid, "UNKNOWN")
        received[(sup_id, rid, str(rec["material_id"]))] += 1

    all_keys = sorted(set(planned) | set(received))
    rows: list[dict[str, Any]] = []
    for sup_id, run_id, mat_id in all_keys:
        exp_boxes = planned.get((sup_id, run_id, mat_id), 0)
        got_boxes = received.get((sup_id, run_id, mat_id), 0)
        diff = got_boxes - exp_boxes
        if exp_boxes == 0 and got_boxes > 0:
            verdict = "UNPLANNED_RECEIPT"
        elif got_boxes == 0 and exp_boxes > 0:
            verdict = "NOT_DELIVERED"
        elif diff == 0:
            verdict = "DELIVERED"
        elif diff < 0:
            verdict = "SHORT"
        else:
            verdict = "OVER"
        rows.append(
            {
                "supplier_id": sup_id,
                "truck_run_id": run_id,
                "material_id": mat_id,
                "planned_boxes": exp_boxes,
                "received_boxes": got_boxes,
                "variance_boxes": diff,
                "status": verdict,
            }
        )
    return rows
