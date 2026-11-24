"""Action: publish_count_list (data-derived guards + hold_and_alert_owner)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from actions import raise_system_alert
from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.mail import MailAdapter
from adapters.wms import WMSAdapter
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import OntologyRegistry

ACTION_NAME = "publish_count_list"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    store: SnapshotStore,
    wms: WMSAdapter,
    mail: MailAdapter,
    owner: str,
    run_id: str,
    list_date: str,
    list_scope: str,
    tasks: list[dict[str, Any]],
    bins: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    storage_locations: list[dict[str, Any]],
    snapshot_id: str,
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Compute count_scope, recent_match_skip, and fresh_snapshot from data, then publish or hold."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    scope = list_scope.upper()
    bin_map = {str(b["bin_code"]): b for b in bins}
    eligible_slocs = {str(s["sloc"]) for s in storage_locations if bool(s.get("count_eligible"))}

    eligible_boxes_by_bin: dict[str, int] = defaultdict(int)
    ineligible_boxes_by_bin: dict[str, int] = defaultdict(int)
    for box in boxes:
        if str(box.get("status", "IN_STORAGE")) != "IN_STORAGE":
            continue
        bcode = str(box["bin_code"])
        if str(box.get("sloc")) in eligible_slocs:
            eligible_boxes_by_bin[bcode] += 1
        else:
            ineligible_boxes_by_bin[bcode] += 1

    scope_mismatches: list[str] = []
    box_count_mismatches: list[str] = []
    active_skip_violations: list[str] = []
    total_eligible_boxes = 0

    for task in tasks:
        bcode = str(task["bin_code"])
        b_obj = bin_map.get(bcode)
        if b_obj is None or str(b_obj.get("count_scope", "")).upper() != scope:
            scope_mismatches.append(bcode)
        actual_elig = eligible_boxes_by_bin.get(bcode, 0)
        task_boxes = int(task.get("book_boxes", 0))
        if actual_elig <= 0 or task_boxes != actual_elig:
            box_count_mismatches.append(f"{bcode}(task={task_boxes},eligible={actual_elig})")
        total_eligible_boxes += actual_elig

        if b_obj is not None and b_obj.get("skip_until") and str(b_obj["skip_until"]) > list_date:
            active_skip_violations.append(f"{bcode}(skip_until={b_obj['skip_until']})")

    prov_rows = store.query_ro(
        "SELECT captured_at FROM ledger_provenance WHERE snapshot_id = ? ORDER BY load_id DESC LIMIT 1",
        (snapshot_id,),
    )
    snapshot_captured_at = prov_rows[0]["captured_at"] if prov_rows else None

    computed = {
        "list_scope": scope,
        "eligible_boxes": total_eligible_boxes,
        "scope_mismatches": scope_mismatches,
        "box_count_mismatches": box_count_mismatches,
        "active_skip_violations": active_skip_violations,
        "checked_tasks": len(tasks),
        "snapshot_captured_at": snapshot_captured_at,
        "now_at": started_at,
        "max_age_hours": 24.0,
    }
    results = run_guards([g.id for g in spec.guards], computed)

    if all(r.passed for r in results):
        out_ref = wms.publish_count_tasks(run_id, tasks)
        outcome = "DONE"
    else:
        out_ref = f"held:{run_id}"
        outcome = "HELD"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"run_id={run_id};snapshot={snapshot_id};tasks={len(tasks)}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )

    if outcome == "HELD" and spec.on_guard_fail == "hold_and_alert_owner":
        reasons = "; ".join(g.reason for g in results if not g.passed)
        raise_system_alert.execute(
            registry=registry,
            action_log=action_log,
            mail=mail,
            performed_by=spec.performed_by,
            owner=owner,
            recipients=[owner],
            subject=f"[System alert] Count list held ({run_id})",
            body=f"publish_count_list held by guard(s): {reasons}",
            started_at=started_at,
        )

    return entry, results
