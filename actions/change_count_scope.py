"""Action: change_count_scope (data-derived reviewed_change guard)."""

from __future__ import annotations

from typing import Any

from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import OntologyRegistry

ACTION_NAME = "change_count_scope"


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    store: SnapshotStore,
    change_record: dict[str, Any],
    sloc_row: dict[str, Any],
    started_at: str,
) -> tuple[ActionLogEntry, list[GuardResult]]:
    """Verify independent reviewer and owner approval before updating StorageLocation scope."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    computed = {
        "author": str(change_record.get("author", "")),
        "reviewer": str(change_record.get("reviewer", "")),
        "approver": str(change_record.get("approver", "")),
        "owner": str(change_record.get("owner", "person.owner")),
    }
    results = run_guards([g.id for g in spec.guards], computed)

    sloc = str(sloc_row["sloc"])
    eligible = bool(sloc_row["count_eligible"])
    if all(r.passed for r in results):
        receipt = store.append_snapshot(
            "sloc_master",
            [sloc_row],
            source_name="actions.change_count_scope",
            captured_at=started_at,
        )
        out_ref = f"{receipt.snapshot_id}:{sloc}:count_eligible={str(eligible).lower()}"
        outcome = "DONE"
    else:
        out_ref = f"handed_to_person:{sloc}"
        outcome = "HANDED_TO_PERSON"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"sloc={sloc};count_eligible={str(eligible).lower()};author={computed['author']};reviewer={computed['reviewer']}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )
    return entry, results
