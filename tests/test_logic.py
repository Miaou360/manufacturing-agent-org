"""Unit tests for turnover profile, count selection, truck leveling, mismatch cause, and receipt matching."""

from __future__ import annotations

from logic.count_selection import select_count_tasks
from logic.mismatch_cause import classify_mismatch_cause
from logic.receipt_matching import match_receipts_to_trucks, summarize_delivery_status
from logic.truck_leveling import level_truck_orders
from logic.turnover_profile import compute_trimmed_mean, compute_turnover_profiles, is_small_part


def test_turnover_profile_on_toy_data() -> None:
    # 10% trim on 10 values drops min (1.0) and max (1000.0)
    vals = [1.0, 4.0, 5.0, 5.0, 6.0, 6.0, 7.0, 7.0, 8.0, 1000.0]
    assert round(compute_trimmed_mean(vals, trim_percent=0.10), 2) == 6.0
    assert is_small_part("Hex Flange Bolt M6")
    assert not is_small_part("Front Bumper Bracket")

    materials = [
        {"material_id": "PART-FAST", "description": "M6 Hex Bolt", "unit_pack_qty": 500},
        {"material_id": "PART-NONE", "description": "Rear Lamp Housing", "unit_pack_qty": 10},
    ]
    bins = [{"bin_code": "N01-01", "zone_code": "Zone A"}]
    zones = [{"zone_code": "Zone A"}]
    boxes = [
        {
            "barcode": f"B-{i}",
            "material_id": "PART-FAST",
            "qty": 500,
            "received_at": "2026-11-18T08:00:00",
            "bin_code": "N01-01",
            "sloc": "SL10",
            "status": "ISSUED_TO_PRODUCTION",
            "dwell_days": d,
        }
        for i, d in enumerate(vals)
    ] + [
        {
            "barcode": "B-ORPHAN",
            "material_id": "PART-NONE",
            "qty": 10,
            "received_at": "2026-11-10T08:00:00",
            "bin_code": "N01-01",
            "sloc": "SL10",
            "status": "ISSUED_WITHOUT_RECEIPT",
            "dwell_days": 99.0,
        }
    ]

    out = compute_turnover_profiles(
        materials=materials,
        boxes=boxes,
        bins=bins,
        zones=zones,
        profile_as_of="2026-11-23T04:30:00",
    )
    by_mat = {m["material_id"]: m for m in out["materials"]}
    assert by_mat["PART-FAST"]["small_part"] is True
    assert by_mat["PART-FAST"]["turnover_days_trimmed_mean"] == 6.0
    assert by_mat["PART-FAST"]["mover_class"] == "FAST"
    assert by_mat["PART-FAST"]["profile_as_of"] == "2026-11-23T04:30:00"
    assert by_mat["PART-NONE"]["mover_class"] == "NO_ISSUE_YET"
    assert out["zones"][0]["lookback_days"] >= 7


def test_count_selection_sl90_skip_and_random_lane() -> None:
    slocs = [
        {"sloc": "SL10", "facility": "Facility North", "count_eligible": True},
        {"sloc": "SL90", "facility": "Facility North", "count_eligible": False},
    ]
    bins = [
        {"bin_code": f"N01-{i:02d}", "facility": "Facility North", "zone_code": "Zone A", "rack_level": 1, "count_scope": "DAILY"}
        for i in range(1, 10)
    ] + [
        {"bin_code": "N09-01", "facility": "Facility North", "zone_code": "Zone B", "rack_level": 4, "count_scope": "WEEKLY"}
    ]
    # N01-01 and N01-02 hold only SL90 quarantine boxes -> must be excluded
    # N01-03 holds mixed SL10 (1 box) + SL90 (2 boxes) -> must count only 1 eligible box
    # N01-04 matched 3 days ago with identical barcodes -> must be skipped (7d daily cooldown)
    boxes = [
        {"barcode": "Q-1", "material_id": "PART-0001", "qty": 10, "bin_code": "N01-01", "sloc": "SL90", "status": "IN_STORAGE", "dwell_days": 50},
        {"barcode": "Q-2", "material_id": "PART-0001", "qty": 10, "bin_code": "N01-02", "sloc": "SL90", "status": "IN_STORAGE", "dwell_days": 50},
        {"barcode": "M-1", "material_id": "PART-0001", "qty": 10, "bin_code": "N01-03", "sloc": "SL10", "status": "IN_STORAGE", "dwell_days": 40},
        {"barcode": "M-Q1", "material_id": "PART-0001", "qty": 10, "bin_code": "N01-03", "sloc": "SL90", "status": "IN_STORAGE", "dwell_days": 40},
        {"barcode": "M-Q2", "material_id": "PART-0001", "qty": 10, "bin_code": "N01-03", "sloc": "SL90", "status": "IN_STORAGE", "dwell_days": 40},
        {"barcode": "S-1", "material_id": "PART-0002", "qty": 10, "bin_code": "N01-04", "sloc": "SL10", "status": "IN_STORAGE", "dwell_days": 30},
        {"barcode": "W-1", "material_id": "PART-0003", "qty": 10, "bin_code": "N09-01", "sloc": "SL10", "status": "IN_STORAGE", "dwell_days": 30},
    ] + [
        {"barcode": f"D-{i}", "material_id": "PART-0004", "qty": 10, "bin_code": f"N01-{i:02d}", "sloc": "SL10", "status": "IN_STORAGE", "dwell_days": 15}
        for i in range(5, 10)
    ]

    res = select_count_tasks(
        run_id="20261123_DAILY",
        list_date="2026-11-23",
        bins=bins,
        boxes=boxes,
        storage_locations=slocs,
        recent_matches={"N01-04": {"matched_at": "2026-11-20", "barcodes": ["S-1"]}},
        list_scope="DAILY",
        risk_limit=3,
        random_limit=2,
        seed=7,
    )
    selected_bins = {t["bin_code"] for t in res.tasks}
    assert "N01-01" not in selected_bins
    assert "N01-02" not in selected_bins
    assert set(res.excluded_by_sloc) == {"N01-01", "N01-02"}
    assert "N01-04" not in selected_bins
    assert res.skipped_bins["N01-04"] == "2026-11-27"
    assert "N09-01" not in selected_bins

    mixed_task = next(t for t in res.tasks if t["bin_code"] == "N01-03")
    assert mixed_task["book_boxes"] == 1

    lanes = {t["lane"] for t in res.tasks}
    assert "RISK" in lanes
    assert "RANDOM_BASELINE" in lanes

    # Weekly 14-day cooldown skips N09-01 if matched 10 days ago with unchanged barcode
    weekly_skip = select_count_tasks(
        run_id="20261123_WEEKLY",
        list_date="2026-11-23",
        bins=bins,
        boxes=boxes,
        storage_locations=slocs,
        recent_matches={"N09-01": {"matched_at": "2026-11-13", "barcodes": ["W-1"]}},
        list_scope="WEEKLY",
        high_rack_limit=5,
        seed=7,
    )
    assert weekly_skip.tasks == []
    assert weekly_skip.skipped_bins["N09-01"] == "2026-11-27"


def test_truck_leveling_invariants() -> None:
    runs = [
        {"truck_run_id": "SUP-001-0800", "supplier_id": "SUP-001", "run_date": "2026-11-23", "slot": "08:00", "status": "PLANNED"},
        {"truck_run_id": "SUP-001-1200", "supplier_id": "SUP-001", "run_date": "2026-11-23", "slot": "12:00", "status": "PLANNED"},
        {"truck_run_id": "SUP-001-1600", "supplier_id": "SUP-001", "run_date": "2026-11-23", "slot": "16:00", "status": "PLANNED"},
    ]
    # Overloaded 12:00 truck (18 + 14 = 32 > cap 24)
    overloaded_lines = [
        {"order_no": "DO-1", "material_id": "PART-0001", "supplier_id": "SUP-001", "due_window": "2026-11-23 08:00", "ordered_boxes": 10, "truck_run_id": "SUP-001-0800"},
        {"order_no": "DO-2", "material_id": "PART-0002", "supplier_id": "SUP-001", "due_window": "2026-11-23 12:00", "ordered_boxes": 18, "truck_run_id": "SUP-001-1200"},
        {"order_no": "DO-3", "material_id": "PART-0003", "supplier_id": "SUP-001", "due_window": "2026-11-23 12:00", "ordered_boxes": 14, "truck_run_id": "SUP-001-1200"},
    ]
    leveled = level_truck_orders(
        order_lines=overloaded_lines,
        truck_runs=runs,
        trailer_cap_carts=24,
        starting_stock_boxes={"PART-0001": 5, "PART-0002": 5, "PART-0003": 5},
        slot_demand_boxes={
            ("PART-0001", "SUP-001-0800"): 8,
            ("PART-0002", "SUP-001-1200"): 10,
            ("PART-0003", "SUP-001-1200"): 10,
        },
    )
    assert leveled.overload_triggered is True
    assert leveled.changed_lines == 1
    assert leveled.pushed_later_count == 0
    assert leveled.min_projected_stock >= 0
    assert max(leveled.planned_carts_by_truck.values()) <= 24
    assert leveled.portal_totals == leveled.plan_totals

    # Balanced orders must remain completely untouched
    balanced_lines = [
        {"order_no": "DO-10", "material_id": "PART-0001", "supplier_id": "SUP-001", "due_window": "2026-11-23 08:00", "ordered_boxes": 12, "truck_run_id": "SUP-001-0800"},
        {"order_no": "DO-11", "material_id": "PART-0002", "supplier_id": "SUP-001", "due_window": "2026-11-23 12:00", "ordered_boxes": 12, "truck_run_id": "SUP-001-1200"},
        {"order_no": "DO-12", "material_id": "PART-0003", "supplier_id": "SUP-001", "due_window": "2026-11-23 16:00", "ordered_boxes": 12, "truck_run_id": "SUP-001-1600"},
    ]
    unchanged = level_truck_orders(order_lines=balanced_lines, truck_runs=runs, trailer_cap_carts=24)
    assert unchanged.overload_triggered is False
    assert unchanged.changed_lines == 0
    assert unchanged.moves == []

    # 1.7x average overload threshold (< cap 24) still triggers leveling
    five_runs = [
        {"truck_run_id": f"SUP-001-{s.replace(':', '')}", "supplier_id": "SUP-001", "run_date": "2026-11-24", "slot": s, "status": "PLANNED"}
        for s in ("08:00", "10:00", "12:00", "14:00", "16:00")
    ]
    spiky_lines = [
        {"order_no": "SP-1", "material_id": "PART-0001", "supplier_id": "SUP-001", "due_window": "2026-11-24 08:00", "ordered_boxes": 4, "truck_run_id": "SUP-001-0800"},
        {"order_no": "SP-2", "material_id": "PART-0002", "supplier_id": "SUP-001", "due_window": "2026-11-24 10:00", "ordered_boxes": 4, "truck_run_id": "SUP-001-1000"},
        {"order_no": "SP-3", "material_id": "PART-0003", "supplier_id": "SUP-001", "due_window": "2026-11-24 12:00", "ordered_boxes": 12, "truck_run_id": "SUP-001-1200"},
        {"order_no": "SP-4", "material_id": "PART-0004", "supplier_id": "SUP-001", "due_window": "2026-11-24 12:00", "ordered_boxes": 8, "truck_run_id": "SUP-001-1200"},
        {"order_no": "SP-5", "material_id": "PART-0005", "supplier_id": "SUP-001", "due_window": "2026-11-24 14:00", "ordered_boxes": 4, "truck_run_id": "SUP-001-1400"},
        {"order_no": "SP-6", "material_id": "PART-0006", "supplier_id": "SUP-001", "due_window": "2026-11-24 16:00", "ordered_boxes": 4, "truck_run_id": "SUP-001-1600"},
    ]
    # Total 36 across 5 trucks -> avg=7.2, 1.7*avg=12.24 -> limit=12 (< cap 24); 12:00 has 20 > 12 -> pulls 8 forward!
    avg_leveled = level_truck_orders(order_lines=spiky_lines, truck_runs=five_runs, trailer_cap_carts=24)
    assert avg_leveled.overload_triggered is True
    assert avg_leveled.effective_limit_carts == 12
    assert max(avg_leveled.planned_carts_by_truck.values()) <= 12
    assert avg_leveled.pushed_later_count == 0
    assert avg_leveled.portal_totals == avg_leveled.plan_totals


def test_mismatch_cause_and_receipt_matching() -> None:
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=None) == "UNCLASSIFIED"
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=0) == "EMPTY"
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=3) == "LINE_SCAN_MISS"
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=7) == "BIN_TO_BIN_MISS"
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=4, field_notes="wrong part mixed in bin") == "WRONG_PART"
    assert classify_mismatch_cause(book_boxes=5, counted_boxes=3, field_notes="listed part not found", multi_part_bin=True) == "PART_MISSING"

    runs = [
        {"truck_run_id": "SUP-001-0800", "supplier_id": "SUP-001", "run_date": "2026-11-23", "slot": "08:00"},
        {"truck_run_id": "SUP-001-1200", "supplier_id": "SUP-001", "run_date": "2026-11-23", "slot": "12:00"},
    ]
    orders = [
        {"supplier_id": "SUP-001", "truck_run_id": "SUP-001-0800", "material_id": "PART-0001", "ordered_boxes": 2},
        {"supplier_id": "SUP-001", "truck_run_id": "SUP-001-0800", "material_id": "PART-0002", "ordered_boxes": 1},
        {"supplier_id": "SUP-001", "truck_run_id": "SUP-001-1200", "material_id": "PART-0003", "ordered_boxes": 1},
        {"supplier_id": "SUP-001", "truck_run_id": "SUP-001-1200", "material_id": "PART-0004", "ordered_boxes": 2},
    ]
    receipts = [
        {"barcode": "G-1", "material_id": "PART-0001", "qty": 100, "received_at": "2026-11-23T08:20:00", "erp_posted": True},
        {"barcode": "G-2", "material_id": "PART-0002", "qty": 100, "received_at": "2026-11-23T08:25:00", "erp_posted": True},
        {"barcode": "G-3", "material_id": "PART-0003", "qty": 100, "received_at": "2026-11-23T12:15:00", "erp_posted": True},
        {"barcode": "G-4", "material_id": "PART-0003", "qty": 100, "received_at": "2026-11-23T12:16:00", "erp_posted": True},
        {"barcode": "G-5", "material_id": "PART-0005", "qty": 100, "received_at": "2026-11-23T12:20:00", "erp_posted": True},
    ]
    matched = match_receipts_to_trucks(
        receipts=receipts,
        truck_runs=runs,
        order_lines=orders,
        material_suppliers={"PART-0005": "SUP-001"},
    )
    status = {r["material_id"]: r["status"] for r in summarize_delivery_status(order_lines=orders, matched_receipts=matched, truck_runs=runs)}
    assert status == {
        "PART-0001": "SHORT",
        "PART-0002": "DELIVERED",
        "PART-0003": "OVER",
        "PART-0004": "NOT_DELIVERED",
        "PART-0005": "UNPLANNED_RECEIPT",
    }
