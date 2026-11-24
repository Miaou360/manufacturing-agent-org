"""Pure action guard functions and registry enforcing ontology/actions.yaml rules."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Callable, Mapping

from logic.count_selection import check_recent_match_skip

DEFAULT_PROTECTED_MOVEMENTS = frozenset({"101", "102", "122", "561"})


@dataclass(frozen=True)
class GuardResult:
    """Outcome of one pure guard evaluation."""

    id: str
    passed: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


GuardFn = Callable[[Mapping[str, Any]], GuardResult]


def _parse_iso(value: str | datetime | date) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def guard_count_scope(ctx: Mapping[str, Any]) -> GuardResult:
    """Check bin count scope matches list scope and box counts include only eligible SLocs."""
    scope_mismatches = list(ctx.get("scope_mismatches") or ())
    box_count_mismatches = list(ctx.get("box_count_mismatches") or ())
    if scope_mismatches or box_count_mismatches:
        parts: list[str] = []
        if scope_mismatches:
            parts.append(f"scope mismatch on {scope_mismatches}")
        if box_count_mismatches:
            parts.append(f"eligible box count mismatch on {box_count_mismatches}")
        return GuardResult("count_scope", False, "; ".join(parts))

    expected_scope = str(ctx.get("list_scope", "DAILY")).upper()
    eligible_boxes = int(ctx.get("eligible_boxes", 0))
    if eligible_boxes <= 0:
        return GuardResult(
            "count_scope",
            False,
            "count list has zero boxes in count_eligible=true storage locations",
        )
    return GuardResult(
        "count_scope",
        True,
        f"scope matches {expected_scope} with {eligible_boxes} eligible box(es)",
    )


def guard_recent_match_skip(ctx: Mapping[str, Any]) -> GuardResult:
    """Verify no task bin is inside an active skip_until cooldown, or evaluate one bin."""
    if "active_skip_violations" in ctx:
        violations = list(ctx.get("active_skip_violations") or ())
        checked = int(ctx.get("checked_tasks", 0))
        if violations:
            return GuardResult(
                "recent_match_skip",
                False,
                f"task(s) include bin(s) with skip_until after list_date: {violations}",
            )
        return GuardResult(
            "recent_match_skip",
            True,
            f"0 of {checked} task bin(s) have active skip_until after list_date",
        )

    eligible, reason, _ = check_recent_match_skip(
        list_scope=str(ctx.get("list_scope", "DAILY")),
        cooldown_days=ctx.get("cooldown_days"),
        last_matched_at=ctx.get("last_matched_at"),
        as_of_date=ctx.get("as_of_date"),
        previous_barcodes=ctx.get("previous_barcodes"),
        current_barcodes=ctx.get("current_barcodes"),
    )
    return GuardResult("recent_match_skip", eligible, reason)


def guard_fresh_snapshot(ctx: Mapping[str, Any]) -> GuardResult:
    """Require warehouse snapshot age to be at most 24 hours."""
    max_age_hours = float(ctx.get("max_age_hours", 24.0))
    snapshot_at = ctx.get("snapshot_captured_at")
    now_at = ctx.get("now_at")
    if not snapshot_at or not now_at:
        return GuardResult("fresh_snapshot", False, "missing snapshot_captured_at or now_at")

    age_hours = (_parse_iso(now_at) - _parse_iso(snapshot_at)).total_seconds() / 3600.0
    if age_hours < 0:
        return GuardResult(
            "fresh_snapshot",
            False,
            f"snapshot timestamp is {abs(age_hours):.2f}h in the future",
        )
    if age_hours > max_age_hours:
        return GuardResult(
            "fresh_snapshot",
            False,
            f"snapshot is {age_hours:.2f}h old (limit {max_age_hours:.1f}h)",
        )
    return GuardResult(
        "fresh_snapshot",
        True,
        f"snapshot age {age_hours:.2f}h <= {max_age_hours:.1f}h",
    )


def guard_facts_only(ctx: Mapping[str, Any]) -> GuardResult:
    """Require verified physical counts; blank means not counted, never zero or inferred."""
    counted_boxes = ctx.get("counted_boxes")
    inferred_from_notes = bool(ctx.get("inferred_from_notes", False))
    blank_coerced_to_zero = bool(ctx.get("blank_coerced_to_zero", False))

    if inferred_from_notes:
        return GuardResult("facts_only", False, "quantity must not be inferred from field notes")
    if blank_coerced_to_zero:
        return GuardResult("facts_only", False, "blank count must stay null, never zero")

    has_diff = "diff_boxes" in ctx
    diff_boxes = ctx.get("diff_boxes")
    has_cause = "cause_class" in ctx
    cause_class = ctx.get("cause_class")
    has_book = "book_boxes" in ctx and ctx.get("book_boxes") is not None

    if counted_boxes is None:
        if has_diff and diff_boxes is not None:
            return GuardResult(
                "facts_only",
                False,
                f"counted_boxes is null so diff_boxes must be null (got {diff_boxes})",
            )
        if has_cause and str(cause_class or "") != "UNCLASSIFIED":
            return GuardResult(
                "facts_only",
                False,
                f"null counted_boxes must have cause_class=UNCLASSIFIED (got {cause_class})",
            )
        return GuardResult("facts_only", True, "uncounted row preserved as null / UNCLASSIFIED")

    if isinstance(counted_boxes, bool) or not isinstance(counted_boxes, int) or counted_boxes < 0:
        return GuardResult(
            "facts_only",
            False,
            f"invalid counted_boxes={counted_boxes!r}; expected non-negative int or null",
        )
    if has_diff and diff_boxes is None:
        return GuardResult(
            "facts_only",
            False,
            "counted_boxes is set so diff_boxes cannot be null",
        )
    if has_diff and has_book:
        expected_diff = counted_boxes - int(ctx["book_boxes"])
        if int(diff_boxes) != expected_diff:
            return GuardResult(
                "facts_only",
                False,
                f"diff_boxes={diff_boxes} does not equal counted-book ({expected_diff})",
            )
    return GuardResult("facts_only", True, "count record contains verified fact")


def guard_not_receipt_linked(ctx: Mapping[str, Any]) -> GuardResult:
    """Refuse clearing an interface error tied to a goods receipt."""
    if bool(ctx.get("receipt_linked", False)):
        return GuardResult(
            "not_receipt_linked",
            False,
            f"error {ctx.get('error_id', '')} is linked to a goods receipt",
        )
    return GuardResult("not_receipt_linked", True, "error is not linked to a goods receipt")


def guard_protected_movement(ctx: Mapping[str, Any]) -> GuardResult:
    """Refuse clearing protected ERP movement types."""
    movement_type = str(ctx.get("movement_type", "")).strip()
    protected = frozenset(
        str(m) for m in ctx.get("protected_movements", DEFAULT_PROTECTED_MOVEMENTS)
    )
    if not movement_type:
        return GuardResult("protected_movement", False, "missing movement_type")
    if movement_type in protected:
        return GuardResult(
            "protected_movement",
            False,
            f"movement type {movement_type} is protected and cannot be auto-cleared",
        )
    return GuardResult("protected_movement", True, f"movement type {movement_type} is clearable")


def guard_persistent_difference(ctx: Mapping[str, Any]) -> GuardResult:
    """Require a stock difference to persist across two snapshots 15-60 minutes apart."""
    older_at = ctx.get("older_snapshot_at")
    newer_at = ctx.get("newer_snapshot_at")
    now_at = ctx.get("now_at", newer_at)
    older_diff = float(ctx.get("older_diff_qty", 0.0))
    newer_diff = float(ctx.get("newer_diff_qty", 0.0))
    min_gap_min = float(ctx.get("min_gap_minutes", 15.0))
    max_older_age_min = float(ctx.get("max_older_age_minutes", 60.0))

    if not older_at or not newer_at or not now_at:
        return GuardResult(
            "persistent_difference",
            False,
            "two snapshots with timestamps are required",
        )
    t_old = _parse_iso(older_at)
    t_new = _parse_iso(newer_at)
    t_now = _parse_iso(now_at)
    gap_min = (t_new - t_old).total_seconds() / 60.0
    older_age_min = (t_now - t_old).total_seconds() / 60.0

    if older_diff == 0 or newer_diff == 0 or (older_diff > 0) != (newer_diff > 0):
        return GuardResult(
            "persistent_difference",
            False,
            f"difference did not persist in same direction (older={older_diff}, newer={newer_diff})",
        )
    if gap_min < min_gap_min:
        return GuardResult(
            "persistent_difference",
            False,
            f"snapshot gap {gap_min:.1f}m < {min_gap_min:.1f}m minimum",
        )
    if older_age_min > max_older_age_min:
        return GuardResult(
            "persistent_difference",
            False,
            f"older snapshot age {older_age_min:.1f}m > {max_older_age_min:.1f}m limit",
        )
    return GuardResult(
        "persistent_difference",
        True,
        f"difference persisted across {gap_min:.1f}m gap (older age {older_age_min:.1f}m)",
    )


def guard_nothing_in_flight(ctx: Mapping[str, Any]) -> GuardResult:
    """Block transfer when WMS-to-ERP receipts or issues for the material are in flight."""
    in_flight_receipts = int(ctx.get("in_flight_receipts", 0))
    in_flight_issues = int(ctx.get("in_flight_issues", 0))
    if in_flight_receipts > 0 or in_flight_issues > 0:
        return GuardResult(
            "nothing_in_flight",
            False,
            f"in-flight postings detected (receipts={in_flight_receipts}, issues={in_flight_issues})",
        )
    return GuardResult("nothing_in_flight", True, "no in-flight WMS-to-ERP postings")


def guard_accuracy_not_down(ctx: Mapping[str, Any]) -> GuardResult:
    """Require plant stock accuracy after the transfer to be at least as high as before."""
    before = float(ctx.get("accuracy_before", 0.0))
    after = float(ctx.get("accuracy_after", 0.0))
    if after + 1e-9 < before:
        return GuardResult(
            "accuracy_not_down",
            False,
            f"stock accuracy would drop from {before:.4f} to {after:.4f}",
        )
    return GuardResult(
        "accuracy_not_down",
        True,
        f"stock accuracy maintained/improved ({before:.4f} -> {after:.4f})",
    )


def guard_no_shortage(ctx: Mapping[str, Any]) -> GuardResult:
    """Ensure projected stock stays non-negative and orders only pull earlier, never later."""
    min_projected = float(ctx.get("min_projected_stock", 0.0))
    pushed_later_count = int(ctx.get("pushed_later_count", 0))
    if min_projected < 0:
        return GuardResult(
            "no_shortage",
            False,
            f"projected stock drops below zero ({min_projected:.1f})",
        )
    if pushed_later_count > 0:
        return GuardResult(
            "no_shortage",
            False,
            f"{pushed_later_count} order line(s) pushed to a later truck",
        )
    return GuardResult(
        "no_shortage",
        True,
        f"min_projected_stock={min_projected:.1f} >= 0 and pull-forward only",
    )


def guard_minimal_change(ctx: Mapping[str, Any]) -> GuardResult:
    """Allow changes to a supplier's orders only when triggered by cap or overload threshold."""
    changed_lines = int(ctx.get("changed_lines", 0))
    overload_triggered = bool(ctx.get("overload_triggered", False))
    if changed_lines > 0 and not overload_triggered:
        return GuardResult(
            "minimal_change",
            False,
            f"{changed_lines} line(s) changed without overload trigger",
        )
    return GuardResult(
        "minimal_change",
        True,
        f"changed_lines={changed_lines}, overload_triggered={overload_triggered}",
    )


def guard_trailer_cap(ctx: Mapping[str, Any]) -> GuardResult:
    """Verify planned carts per truck stay within the supplier's trailer cap."""
    trailer_cap = int(ctx.get("trailer_cap_carts", 0))
    planned_by_truck = ctx.get("planned_carts_by_truck") or {}
    if trailer_cap <= 0:
        return GuardResult("trailer_cap", False, f"invalid trailer_cap_carts={trailer_cap}")
    over = {k: v for k, v in planned_by_truck.items() if int(v) > trailer_cap}
    if over:
        return GuardResult(
            "trailer_cap",
            False,
            f"truck(s) exceed trailer cap {trailer_cap}: {over}",
        )
    return GuardResult("trailer_cap", True, f"all trucks within cap {trailer_cap}")


def guard_output_verified(ctx: Mapping[str, Any]) -> GuardResult:
    """Verify plan totals match portal orders per material and day (V1-V6)."""
    portal_totals: Mapping[tuple[str, str], int] = ctx.get("portal_totals") or {}
    plan_totals: Mapping[tuple[str, str], int] = ctx.get("plan_totals") or {}
    negative_lines = int(ctx.get("negative_lines", 0))
    invalid_slots = int(ctx.get("invalid_slots", 0))
    duplicate_lines = int(ctx.get("duplicate_lines", 0))

    failures: list[str] = []
    if negative_lines > 0:
        failures.append(f"V1 negative_lines={negative_lines}")
    if invalid_slots > 0:
        failures.append(f"V2 invalid_slots={invalid_slots}")
    if duplicate_lines > 0:
        failures.append(f"V3 duplicate_lines={duplicate_lines}")

    portal_sum = sum(portal_totals.values())
    plan_sum = sum(plan_totals.values())
    if portal_sum != plan_sum:
        failures.append(f"V4 grand_total portal={portal_sum} vs plan={plan_sum}")

    missing_keys = set(portal_totals) - set(plan_totals)
    extra_keys = set(plan_totals) - set(portal_totals)
    if missing_keys or extra_keys:
        failures.append(f"V5 key_mismatch missing={len(missing_keys)} extra={len(extra_keys)}")

    mismatched = [
        k for k in set(portal_totals) & set(plan_totals) if portal_totals[k] != plan_totals[k]
    ]
    if mismatched:
        failures.append(f"V6 material_day_mismatch count={len(mismatched)}")

    if failures:
        return GuardResult("output_verified", False, "; ".join(failures))
    return GuardResult(
        "output_verified",
        True,
        f"V1-V6 passed ({len(plan_totals)} material-day keys)",
    )


def guard_owner_approved_recipients(ctx: Mapping[str, Any]) -> GuardResult:
    """Keep delivery status owner-only until supplier delivery is explicitly approved."""
    owner = str(ctx.get("owner", "owner@example.invalid"))
    recipients = [str(r) for r in (ctx.get("recipients") or ())]
    owner_approved = bool(ctx.get("owner_approved", False))
    if not recipients:
        return GuardResult("owner_approved_recipients", False, "no recipients specified")
    if not owner_approved and any(r != owner for r in recipients):
        return GuardResult(
            "owner_approved_recipients",
            False,
            "external recipients blocked until owner approves supplier delivery",
        )
    return GuardResult(
        "owner_approved_recipients",
        True,
        "recipients approved" if owner_approved else "owner-only delivery enforced",
    )


def guard_reviewed_change(ctx: Mapping[str, Any]) -> GuardResult:
    """Require an independent reviewer (reviewer != author) and owner approval."""
    if "author" in ctx or "reviewer" in ctx or "approver" in ctx:
        author = str(ctx.get("author", "")).strip()
        reviewer = str(ctx.get("reviewer", "")).strip()
        approver = str(ctx.get("approver", "")).strip()
        owner = str(ctx.get("owner", "person.owner")).strip()
        if not author or not reviewer:
            return GuardResult(
                "reviewed_change",
                False,
                "change record must name both author and reviewer",
            )
        if reviewer == author:
            return GuardResult(
                "reviewed_change",
                False,
                f"reviewer ({reviewer}) must differ from author ({author})",
            )
        if approver != owner:
            return GuardResult(
                "reviewed_change",
                False,
                f"approver ({approver or 'MISSING'}) must be the owner ({owner})",
            )
        return GuardResult(
            "reviewed_change",
            True,
            f"authored by {author}, reviewed by {reviewer}, approved by {approver}",
        )

    reviewed = bool(ctx.get("second_agent_reviewed", False))
    approved = bool(ctx.get("owner_approved", False))
    if not reviewed or not approved:
        return GuardResult(
            "reviewed_change",
            False,
            f"requires second_agent_reviewed=True and owner_approved=True (got {reviewed}, {approved})",
        )
    return GuardResult("reviewed_change", True, "second-agent review and owner approval recorded")


def guard_owner_only(ctx: Mapping[str, Any]) -> GuardResult:
    """Ensure system alerts go strictly to the owner and never to operational recipients."""
    owner = str(ctx.get("owner", "owner@example.invalid"))
    recipients = [str(r) for r in (ctx.get("recipients") or ())]
    if not recipients or any(r != owner for r in recipients):
        return GuardResult(
            "owner_only",
            False,
            f"system alert recipients must be owner-only ({owner}), got {recipients}",
        )
    return GuardResult("owner_only", True, f"owner-only recipient verified ({owner})")


GUARD_REGISTRY: dict[str, GuardFn] = {
    "count_scope": guard_count_scope,
    "recent_match_skip": guard_recent_match_skip,
    "fresh_snapshot": guard_fresh_snapshot,
    "facts_only": guard_facts_only,
    "not_receipt_linked": guard_not_receipt_linked,
    "protected_movement": guard_protected_movement,
    "persistent_difference": guard_persistent_difference,
    "nothing_in_flight": guard_nothing_in_flight,
    "accuracy_not_down": guard_accuracy_not_down,
    "no_shortage": guard_no_shortage,
    "minimal_change": guard_minimal_change,
    "trailer_cap": guard_trailer_cap,
    "output_verified": guard_output_verified,
    "owner_approved_recipients": guard_owner_approved_recipients,
    "reviewed_change": guard_reviewed_change,
    "owner_only": guard_owner_only,
}


def run_guards(
    guard_ids: tuple[str, ...] | list[str],
    context: Mapping[str, Any],
    *,
    registry: Mapping[str, GuardFn] | None = None,
) -> list[GuardResult]:
    """Run all guard ids declared for an action; raise KeyError if any guard is missing."""
    table = registry if registry is not None else GUARD_REGISTRY
    missing = [gid for gid in guard_ids if gid not in table]
    if missing:
        raise KeyError(f"Missing guard implementation(s): {missing}")
    return [table[gid](context) for gid in guard_ids]
