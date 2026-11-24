"""Action: clear_interface_error (data-derived guards from InterfaceError object)."""

from __future__ import annotations

from typing import Any

from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.erp import ERPAdapter
from ontology.registry import OntologyRegistry

ACTION_NAME = "clear_interface_error"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    erp: ERPAdapter,
    error: dict[str, Any],
    reason: str,
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Evaluate not_receipt_linked and protected_movement from the error record."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    error_id = str(error["error_id"])
    computed = {
        "error_id": error_id,
        "receipt_linked": bool(error.get("receipt_linked", False)),
        "movement_type": str(error.get("movement_type", "")),
    }
    results = run_guards([g.id for g in spec.guards], computed)

    if all(r.passed for r in results):
        res = erp.clear_interface_error(error_id, reason)
        out_ref = f"cleared:{res['error_id']}"
        outcome = "DONE"
    else:
        out_ref = f"handed_to_person:{error_id}"
        outcome = "HANDED_TO_PERSON"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"error_id={error_id};mvt={computed['movement_type']}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )
    return entry, results
