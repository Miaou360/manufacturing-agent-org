"""Action: record_count_result (data-derived facts_only guard)."""

from __future__ import annotations

from typing import Any

from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import OntologyRegistry

ACTION_NAME = "record_count_result"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    store: SnapshotStore,
    task: dict[str, Any],
    result_row: dict[str, Any],
    started_at: str,
    raw_sheet_value: Any = ...,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Validate count result facts from the task and result record, then append to ledger."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    counted = result_row.get("counted_boxes")
    diff = result_row.get("diff_boxes")
    cause = result_row.get("cause_class")
    book_boxes = int(task["book_boxes"])

    # Detect blank sheet input coerced to 0
    blank_coerced = False
    if raw_sheet_value is not ...:
        is_blank_raw = raw_sheet_value is None or (
            isinstance(raw_sheet_value, str) and raw_sheet_value.strip() == ""
        )
        if is_blank_raw and counted == 0:
            blank_coerced = True

    inferred_from_notes = str(result_row.get("quantity_source", "")).strip().lower() == "field_notes"

    computed = {
        "counted_boxes": counted,
        "diff_boxes": diff,
        "cause_class": cause,
        "book_boxes": book_boxes,
        "inferred_from_notes": inferred_from_notes,
        "blank_coerced_to_zero": blank_coerced,
    }
    results = run_guards([g.id for g in spec.guards], computed)

    run_id = str(result_row.get("run_id", task.get("run_id", "")))
    bin_code = str(result_row.get("bin_code", task.get("bin_code", "")))
    if all(r.passed for r in results):
        receipt = store.append_snapshot(
            "count_result",
            [result_row],
            source_name="warehouse_team_sheet",
            captured_at=started_at,
        )
        out_ref = receipt.snapshot_id
        outcome = "DONE"
    else:
        out_ref = f"handed_to_person:{run_id}:{bin_code}"
        outcome = "HANDED_TO_PERSON"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"run_id={run_id};bin={bin_code}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )
    return entry, results
