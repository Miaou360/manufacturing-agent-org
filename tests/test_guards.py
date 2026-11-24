"""Unit tests for all 16 action guards (pass and fail cases for every guard)."""

from __future__ import annotations

import pytest

from actions.guards import (
    GUARD_REGISTRY,
    guard_accuracy_not_down,
    guard_count_scope,
    guard_facts_only,
    guard_fresh_snapshot,
    guard_minimal_change,
    guard_no_shortage,
    guard_not_receipt_linked,
    guard_nothing_in_flight,
    guard_output_verified,
    guard_owner_approved_recipients,
    guard_owner_only,
    guard_persistent_difference,
    guard_protected_movement,
    guard_recent_match_skip,
    guard_reviewed_change,
    guard_trailer_cap,
    run_guards,
)
from ontology.registry import load_registry


def test_every_ontology_guard_is_registered() -> None:
    registry = load_registry(validate=True)
    declared = {g.id for act in registry.action_types.values() for g in act.guards}
    assert declared == set(GUARD_REGISTRY)


def test_guard_count_scope_pass_and_fail() -> None:
    ok = guard_count_scope(
        {
            "list_scope": "DAILY",
            "eligible_boxes": 4,
            "scope_mismatches": [],
            "box_count_mismatches": [],
        }
    )
    assert ok.passed

    bad_scope = guard_count_scope(
        {
            "list_scope": "DAILY",
            "eligible_boxes": 4,
            "scope_mismatches": ["N01-01"],
            "box_count_mismatches": [],
        }
    )
    assert not bad_scope.passed

    bad_sloc = guard_count_scope(
        {
            "list_scope": "DAILY",
            "eligible_boxes": 0,
            "scope_mismatches": [],
            "box_count_mismatches": ["N01-02(task=2,eligible=0)"],
        }
    )
    assert not bad_sloc.passed


def test_guard_recent_match_skip_pass_and_fail() -> None:
    skip = guard_recent_match_skip(
        {
            "list_scope": "DAILY",
            "last_matched_at": "2026-11-20",
            "as_of_date": "2026-11-23",
            "previous_barcodes": ["PLT-1", "PLT-2"],
            "current_barcodes": ["PLT-2", "PLT-1"],
        }
    )
    assert not skip.passed

    changed = guard_recent_match_skip(
        {
            "list_scope": "DAILY",
            "last_matched_at": "2026-11-20",
            "as_of_date": "2026-11-23",
            "previous_barcodes": ["PLT-1", "PLT-2"],
            "current_barcodes": ["PLT-1", "PLT-3"],
        }
    )
    assert changed.passed


def test_guard_fresh_snapshot_pass_and_fail() -> None:
    ok = guard_fresh_snapshot(
        {
            "snapshot_captured_at": "2026-11-23T06:30:00",
            "now_at": "2026-11-23T06:50:00",
        }
    )
    assert ok.passed

    stale = guard_fresh_snapshot(
        {
            "snapshot_captured_at": "2026-11-21T06:30:00",
            "now_at": "2026-11-23T06:50:00",
        }
    )
    assert not stale.passed


def test_guard_facts_only_pass_and_fail() -> None:
    assert guard_facts_only({"counted_boxes": 5}).passed
    assert guard_facts_only({"counted_boxes": None}).passed
    assert not guard_facts_only({"counted_boxes": 5, "inferred_from_notes": True}).passed
    assert not guard_facts_only({"counted_boxes": 0, "blank_coerced_to_zero": True}).passed
    assert not guard_facts_only({"counted_boxes": -1}).passed


def test_guard_not_receipt_linked_pass_and_fail() -> None:
    assert guard_not_receipt_linked({"error_id": "E1", "receipt_linked": False}).passed
    assert not guard_not_receipt_linked({"error_id": "E2", "receipt_linked": True}).passed


def test_guard_protected_movement_pass_and_fail() -> None:
    assert guard_protected_movement({"movement_type": "261"}).passed
    assert not guard_protected_movement({"movement_type": "101"}).passed


def test_guard_persistent_difference_pass_and_fail() -> None:
    ok = guard_persistent_difference(
        {
            "older_snapshot_at": "2026-11-23T07:10:00",
            "newer_snapshot_at": "2026-11-23T07:30:00",
            "now_at": "2026-11-23T07:45:00",
            "older_diff_qty": -200,
            "newer_diff_qty": -200,
        }
    )
    assert ok.passed

    too_close = guard_persistent_difference(
        {
            "older_snapshot_at": "2026-11-23T07:20:00",
            "newer_snapshot_at": "2026-11-23T07:30:00",
            "now_at": "2026-11-23T07:35:00",
            "older_diff_qty": -200,
            "newer_diff_qty": -200,
        }
    )
    assert not too_close.passed

    too_old = guard_persistent_difference(
        {
            "older_snapshot_at": "2026-11-23T06:00:00",
            "newer_snapshot_at": "2026-11-23T07:30:00",
            "now_at": "2026-11-23T07:35:00",
            "older_diff_qty": -200,
            "newer_diff_qty": -200,
        }
    )
    assert not too_old.passed


def test_guard_nothing_in_flight_pass_and_fail() -> None:
    assert guard_nothing_in_flight({"in_flight_receipts": 0, "in_flight_issues": 0}).passed
    assert not guard_nothing_in_flight({"in_flight_receipts": 1, "in_flight_issues": 0}).passed


def test_guard_accuracy_not_down_pass_and_fail() -> None:
    assert guard_accuracy_not_down({"accuracy_before": 0.94, "accuracy_after": 0.95}).passed
    assert not guard_accuracy_not_down({"accuracy_before": 0.94, "accuracy_after": 0.93}).passed


def test_guard_no_shortage_pass_and_fail() -> None:
    assert guard_no_shortage({"min_projected_stock": 0.0, "pushed_later_count": 0}).passed
    assert not guard_no_shortage({"min_projected_stock": -5.0, "pushed_later_count": 0}).passed
    assert not guard_no_shortage({"min_projected_stock": 10.0, "pushed_later_count": 1}).passed


def test_guard_minimal_change_pass_and_fail() -> None:
    assert guard_minimal_change({"changed_lines": 0, "overload_triggered": False}).passed
    assert guard_minimal_change({"changed_lines": 2, "overload_triggered": True}).passed
    assert not guard_minimal_change({"changed_lines": 1, "overload_triggered": False}).passed


def test_guard_trailer_cap_pass_and_fail() -> None:
    assert guard_trailer_cap(
        {"trailer_cap_carts": 24, "planned_carts_by_truck": {"T1": 24, "T2": 18}}
    ).passed
    assert not guard_trailer_cap(
        {"trailer_cap_carts": 24, "planned_carts_by_truck": {"T1": 25, "T2": 18}}
    ).passed


def test_guard_output_verified_pass_and_fail() -> None:
    totals = {("PART-0001", "2026-11-23"): 10, ("PART-0002", "2026-11-23"): 20}
    assert guard_output_verified({"portal_totals": totals, "plan_totals": dict(totals)}).passed

    mismatched = {("PART-0001", "2026-11-23"): 9, ("PART-0002", "2026-11-23"): 20}
    assert not guard_output_verified({"portal_totals": totals, "plan_totals": mismatched}).passed


def test_guard_owner_approved_recipients_pass_and_fail() -> None:
    owner = "owner@example.invalid"
    assert guard_owner_approved_recipients(
        {"owner": owner, "recipients": [owner], "owner_approved": False}
    ).passed
    assert not guard_owner_approved_recipients(
        {"owner": owner, "recipients": ["supplier@example.invalid"], "owner_approved": False}
    ).passed
    assert guard_owner_approved_recipients(
        {"owner": owner, "recipients": ["supplier@example.invalid"], "owner_approved": True}
    ).passed


def test_guard_reviewed_change_pass_and_fail() -> None:
    assert guard_reviewed_change({"second_agent_reviewed": True, "owner_approved": True}).passed
    assert not guard_reviewed_change({"second_agent_reviewed": True, "owner_approved": False}).passed


def test_guard_owner_only_pass_and_fail() -> None:
    owner = "owner@example.invalid"
    assert guard_owner_only({"owner": owner, "recipients": [owner]}).passed
    assert not guard_owner_only({"owner": owner, "recipients": [owner, "ops@example.invalid"]}).passed


def test_run_guards_raises_on_missing_guard() -> None:
    with pytest.raises(KeyError, match="missing_guard"):
        run_guards(["missing_guard"], {})
