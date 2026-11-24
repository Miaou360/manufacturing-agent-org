"""Action: send_truck_plan (data-derived leveling/verification guards + send_with_flags)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from actions import raise_system_alert
from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.mail import MailAdapter
from adapters.supplier_portal import SupplierPortalAdapter
from logic.truck_leveling import compute_min_projected_stock, totals_by_material_day
from ontology.registry import OntologyRegistry

ACTION_NAME = "send_truck_plan"
FLAG_TEXT = "check this line on the supplier portal"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    portal: SupplierPortalAdapter,
    mail: MailAdapter,
    owner: str,
    supplier: dict[str, Any],
    run_date: str,
    portal_lines: list[dict[str, Any]],
    plan_lines: list[dict[str, Any]],
    truck_runs: list[dict[str, Any]],
    overload_triggered: bool,
    starting_stock_boxes: dict[str, float],
    slot_demand_boxes: dict[tuple[str, str], float],
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult], list[dict[str, Any]]]:
    """Compute all 4 truck-plan guards from portal/plan/stock objects, send, flag & alert on fail."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    supplier_id = str(supplier["supplier_id"])
    trailer_cap = int(supplier["trailer_cap_carts"])
    ordered_runs = sorted(truck_runs, key=lambda r: (str(r["run_date"]), str(r["slot"])))
    valid_run_ids = {str(r["truck_run_id"]) for r in ordered_runs}
    run_index = {str(r["truck_run_id"]): i for i, r in enumerate(ordered_runs)}
    run_date_map = {str(r["truck_run_id"]): str(r["run_date"]) for r in ordered_runs}

    # Check pushed-later moves and changed lines relative to portal baseline
    portal_slots_by_key: dict[tuple[str, str], list[int]] = {}
    for ln in portal_lines:
        rid = str(ln["truck_run_id"])
        if rid in run_index:
            portal_slots_by_key.setdefault((str(ln["order_no"]), str(ln["material_id"])), []).append(run_index[rid])

    pushed_later = 0
    changed_lines = 0
    planned_carts: dict[str, int] = {str(r["truck_run_id"]): 0 for r in ordered_runs}
    negative_lines = 0
    invalid_slots = 0

    for ln in plan_lines:
        rid = str(ln["truck_run_id"])
        boxes = int(ln["ordered_boxes"])
        if boxes < 0:
            negative_lines += 1
        if rid not in valid_run_ids:
            invalid_slots += 1
        else:
            planned_carts[rid] = planned_carts.get(rid, 0) + boxes

        base_order = str(ln["order_no"]).removesuffix("-PF")
        key = (base_order, str(ln["material_id"]))
        orig_indices = portal_slots_by_key.get(key)
        if orig_indices and rid in run_index:
            cur_idx = run_index[rid]
            if cur_idx > max(orig_indices):
                pushed_later += 1
            if bool(ln.get("changed")) or (cur_idx not in orig_indices):
                changed_lines += 1

    # Detect repeated (order_no, material_id) lines in portal / plan rows
    key_counts = Counter((str(ln["order_no"]), str(ln["material_id"])) for ln in plan_lines)
    duplicate_keys = {k for k, cnt in key_counts.items() if cnt > 1}
    duplicate_lines = sum(cnt - 1 for cnt in key_counts.values() if cnt > 1)

    portal_totals = totals_by_material_day(portal_lines, run_date_map)
    plan_totals = totals_by_material_day(plan_lines, run_date_map)
    min_proj = compute_min_projected_stock(
        plan_lines, ordered_runs, starting_stock_boxes, slot_demand_boxes
    )

    computed = {
        "min_projected_stock": min_proj,
        "pushed_later_count": pushed_later,
        "changed_lines": changed_lines,
        "overload_triggered": bool(overload_triggered),
        "trailer_cap_carts": trailer_cap,
        "planned_carts_by_truck": planned_carts,
        "portal_totals": portal_totals,
        "plan_totals": plan_totals,
        "negative_lines": negative_lines,
        "invalid_slots": invalid_slots,
        "duplicate_lines": duplicate_lines,
    }
    results = run_guards([g.id for g in spec.guards], computed)

    flagged_lines = [dict(ln) for ln in plan_lines]
    all_passed = all(r.passed for r in results)
    if not all_passed:
        for ln in flagged_lines:
            key = (str(ln["order_no"]), str(ln["material_id"]))
            if key in duplicate_keys or int(ln["ordered_boxes"]) < 0 or str(ln["truck_run_id"]) not in valid_run_ids:
                ln["flag"] = FLAG_TEXT

    ref = portal.publish_truck_plan(supplier_id, run_date, flagged_lines)
    outcome = "DONE" if all_passed else "SENT_WITH_FLAGS"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"supplier={supplier_id};date={run_date};rows={len(flagged_lines)}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=ref,
    )

    if outcome == "SENT_WITH_FLAGS" and spec.on_guard_fail == "send_with_flags":
        reasons = "; ".join(g.reason for g in results if not g.passed)
        raise_system_alert.execute(
            registry=registry,
            action_log=action_log,
            mail=mail,
            performed_by=spec.performed_by,
            owner=owner,
            recipients=[owner],
            subject=f"[System alert] Truck plan sent with flags ({supplier_id}, {run_date})",
            body=f"Plan sent to {supplier_id} with flagged line(s): {reasons}",
            started_at=started_at,
        )

    return entry, results, flagged_lines
