"""Action: post_stock_transfer (data-derived persistence, in-flight, and accuracy guards)."""

from __future__ import annotations

from typing import Any

from actions.action_log import ActionLog, ActionLogEntry
from actions.guards import GuardResult, run_guards
from adapters.erp import ERPAdapter
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import OntologyRegistry

ACTION_NAME = "post_stock_transfer"


def _position_accuracy(positions: list[dict[str, Any]]) -> float:
    """Share of (material_id, sloc) positions with zero ERP-WMS difference."""
    if not positions:
        return 1.0
    matched = sum(1 for p in positions if abs(float(p["diff_qty"])) < 1e-9)
    return matched / len(positions)


def execute(
    *,
    registry: OntologyRegistry,
    action_log: ActionLog,
    store: SnapshotStore,
    erp: ERPAdapter,
    material_id: str,
    reason: str,
    started_at: str,
    from_sloc: str | None = None,
    to_sloc: str | None = None,
    qty: float | None = None,
    resolves_error_id: str | None = None,
) -> tuple[ActionLogEntry, list[GuardResult], dict[str, Any] | None]:
    """Derive surplus/deficit SLocs and evaluate all transfer guards directly from ledger data."""
    spec = registry.get_action(ACTION_NAME)
    registry.require_guards_implemented(ACTION_NAME)

    # Latest stock position snapshot across all materials and storage locations
    latest_pos = store.query_ro(
        """
        SELECT material_id, sloc, snapshot_date, erp_qty, wms_qty, diff_qty, captured_at
        FROM (
            SELECT material_id, sloc, snapshot_date, erp_qty, wms_qty, diff_qty, captured_at,
                   ROW_NUMBER() OVER (PARTITION BY material_id, sloc ORDER BY load_seq DESC) AS rn
            FROM stock_position_snapshot
        )
        WHERE rn = 1
        """
    )

    mat_positions = [p for p in latest_pos if str(p["material_id"]) == material_id]
    if from_sloc is None:
        surplus = [p for p in mat_positions if float(p["diff_qty"]) > 1e-9]
        surplus.sort(key=lambda p: (-float(p["diff_qty"]), str(p["sloc"])))
        from_sloc = str(surplus[0]["sloc"]) if surplus else ""
    if to_sloc is None:
        deficit = [p for p in mat_positions if float(p["diff_qty"]) < -1e-9]
        deficit.sort(key=lambda p: (float(p["diff_qty"]), str(p["sloc"])))
        to_sloc = str(deficit[0]["sloc"]) if deficit else ""

    if qty is None:
        src_pos = next((p for p in mat_positions if str(p["sloc"]) == from_sloc), None)
        dst_pos = next((p for p in mat_positions if str(p["sloc"]) == to_sloc), None)
        if src_pos and dst_pos:
            qty = min(abs(float(src_pos["diff_qty"])), abs(float(dst_pos["diff_qty"])))
        elif dst_pos:
            qty = abs(float(dst_pos["diff_qty"]))
        else:
            qty = 0.0
    qty = float(qty)

    # Two latest snapshots for (material_id, to_sloc) for persistent_difference
    hist = store.query_ro(
        """
        SELECT captured_at, diff_qty
        FROM stock_position_snapshot
        WHERE material_id = ? AND sloc = ?
        ORDER BY load_seq DESC
        LIMIT 2
        """,
        (material_id, to_sloc),
    )
    older_at = hist[1]["captured_at"] if len(hist) >= 2 else None
    newer_at = hist[0]["captured_at"] if len(hist) >= 1 else None
    older_diff = float(hist[1]["diff_qty"]) if len(hist) >= 2 else 0.0
    newer_diff = float(hist[0]["diff_qty"]) if len(hist) >= 1 else 0.0

    # In-flight unposted goods receipts and unposted/open receipt-linked issues for this material
    unposted_gr = store.query_ro(
        """
        SELECT COUNT(*) AS cnt
        FROM (
            SELECT barcode, material_id, erp_posted,
                   ROW_NUMBER() OVER (PARTITION BY barcode ORDER BY load_seq DESC) AS rn
            FROM goods_receipt
        )
        WHERE rn = 1 AND material_id = ? AND erp_posted = 0
        """,
        (material_id,),
    )
    in_flight_receipts = int(unposted_gr[0]["cnt"]) if unposted_gr else 0

    unposted_issues = store.query_ro(
        """
        SELECT COUNT(*) AS cnt
        FROM (
            SELECT error_id, material_id, receipt_linked, status,
                   ROW_NUMBER() OVER (PARTITION BY error_id ORDER BY load_seq DESC) AS rn
            FROM interface_error_snapshot
        )
        WHERE rn = 1 AND material_id = ? AND status = 'OPEN' AND receipt_linked = 1
        """,
        (material_id,),
    )
    in_flight_issues = int(unposted_issues[0]["cnt"]) if unposted_issues else 0

    # Compute plant stock accuracy BEFORE and AFTER applying this transfer
    accuracy_before = _position_accuracy(latest_pos)
    simulated_after = []
    for p in latest_pos:
        row = dict(p)
        if str(row["material_id"]) == material_id and str(row["sloc"]) == from_sloc:
            row["erp_qty"] = float(row["erp_qty"]) - qty
            row["diff_qty"] = row["erp_qty"] - float(row["wms_qty"])
        elif str(row["material_id"]) == material_id and str(row["sloc"]) == to_sloc:
            row["erp_qty"] = float(row["erp_qty"]) + qty
            row["diff_qty"] = row["erp_qty"] - float(row["wms_qty"])
        simulated_after.append(row)
    accuracy_after = _position_accuracy(simulated_after)

    computed = {
        "older_snapshot_at": older_at,
        "newer_snapshot_at": newer_at,
        "now_at": started_at,
        "older_diff_qty": older_diff,
        "newer_diff_qty": newer_diff,
        "in_flight_receipts": in_flight_receipts,
        "in_flight_issues": in_flight_issues,
        "accuracy_before": accuracy_before,
        "accuracy_after": accuracy_after,
    }
    results = run_guards([g.id for g in spec.guards], computed)

    posted: dict[str, Any] | None = None
    if all(r.passed for r in results) and from_sloc and to_sloc and qty > 0:
        posted = erp.post_stock_transfer(
            material_id=material_id,
            from_sloc=from_sloc,
            to_sloc=to_sloc,
            qty=qty,
            reason=reason,
        )
        store.append_snapshot(
            "stock_transfer",
            [
                {
                    "document_no": posted["document_no"],
                    "material_id": posted["material_id"],
                    "from_sloc": posted["from_sloc"],
                    "to_sloc": posted["to_sloc"],
                    "qty": posted["qty"],
                    "status": posted["status"],
                    "reason": posted["reason"],
                    "resolves_error_id": resolves_error_id,
                    "resolves_backlog_line_id": None,
                }
            ],
            source_name="actions.post_stock_transfer",
            captured_at=started_at,
        )
        out_ref = f"document:{posted['document_no']}"
        outcome = "DONE"
    else:
        out_ref = f"handed_to_person:{material_id}:{from_sloc}->{to_sloc}"
        outcome = "HANDED_TO_PERSON"

    entry = action_log.record(
        action_type=ACTION_NAME,
        performed_by=spec.performed_by,
        started_at=started_at,
        inputs_ref=f"material={material_id};from={from_sloc};to={to_sloc};qty={qty}",
        guard_results=results,
        outcome=outcome,
        approval=spec.approval,
        outputs_ref=out_ref,
    )
    return entry, results, posted
