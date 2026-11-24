"""Tests proving action guards fail from real data/objects rather than caller-supplied context."""

from __future__ import annotations

from pathlib import Path

from actions import (
    change_count_scope,
    post_stock_transfer,
    publish_count_list,
    record_count_result,
    send_truck_plan,
)
from actions.action_log import ActionLog
from adapters.erp import FakeERPAdapter
from adapters.mail import FakeMailAdapter
from adapters.supplier_portal import FakeSupplierPortalAdapter
from adapters.wms import FakeWMSAdapter
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import load_registry

OWNER = "owner@example.invalid"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _init_store(tmp_path: Path) -> tuple[SnapshotStore, ActionLog]:
    db = tmp_path / "test.db"
    store = SnapshotStore(db)
    store.init_db()
    return store, ActionLog(db, run_date="2026-11-24")


def test_run_day_contains_no_guard_context_and_logic_does_not_import_actions() -> None:
    run_day_src = (REPO_ROOT / "simulation" / "run_day.py").read_text(encoding="utf-8")
    assert "guard_context" not in run_day_src

    for py_file in (REPO_ROOT / "logic").glob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        assert "from actions" not in src
        assert "import actions" not in src


def test_ineligible_box_in_task_fails_count_scope_and_alerts_owner(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    snap = store.append_snapshot(
        "wms_box_snapshot",
        [{"barcode": "B-1", "material_id": "PART-0001", "qty": 10, "received_at": "2026-11-20T08:00:00", "bin_code": "N01-01", "sloc": "SL90", "status": "IN_STORAGE", "dwell_days": 4.0}],
        source_name="wms_export",
        captured_at="2026-11-24T06:30:00",
    )
    mail = FakeMailAdapter()
    entry, results = publish_count_list.execute(
        registry=registry,
        action_log=log,
        store=store,
        wms=FakeWMSAdapter(),
        mail=mail,
        owner=OWNER,
        run_id="20261124_DAILY",
        list_date="2026-11-24",
        list_scope="DAILY",
        tasks=[{"run_id": "20261124_DAILY", "bin_code": "N01-01", "book_boxes": 1}],
        bins=[{"bin_code": "N01-01", "count_scope": "DAILY", "skip_until": None}],
        boxes=[{"barcode": "B-1", "bin_code": "N01-01", "sloc": "SL90", "status": "IN_STORAGE"}],
        storage_locations=[{"sloc": "SL90", "count_eligible": False}],
        snapshot_id=snap.snapshot_id,
        started_at="2026-11-24T06:50:00",
    )
    assert entry.outcome == "HELD"
    assert not next(r for r in results if r.id == "count_scope").passed
    assert len(mail.sent_messages) == 1
    assert mail.sent_messages[0]["recipients"] == [OWNER]


def test_future_skip_until_fails_recent_match_skip(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    snap = store.append_snapshot(
        "wms_box_snapshot",
        [{"barcode": "B-1", "material_id": "PART-0001", "qty": 10, "received_at": "2026-11-20T08:00:00", "bin_code": "N01-01", "sloc": "SL10", "status": "IN_STORAGE", "dwell_days": 4.0}],
        source_name="wms_export",
        captured_at="2026-11-24T06:30:00",
    )
    mail = FakeMailAdapter()
    entry, results = publish_count_list.execute(
        registry=registry,
        action_log=log,
        store=store,
        wms=FakeWMSAdapter(),
        mail=mail,
        owner=OWNER,
        run_id="20261124_DAILY",
        list_date="2026-11-24",
        list_scope="DAILY",
        tasks=[{"run_id": "20261124_DAILY", "bin_code": "N01-01", "book_boxes": 1}],
        bins=[{"bin_code": "N01-01", "count_scope": "DAILY", "skip_until": "2026-11-29"}],
        boxes=[{"barcode": "B-1", "bin_code": "N01-01", "sloc": "SL10", "status": "IN_STORAGE"}],
        storage_locations=[{"sloc": "SL10", "count_eligible": True}],
        snapshot_id=snap.snapshot_id,
        started_at="2026-11-24T06:50:00",
    )
    assert entry.outcome == "HELD"
    assert not next(r for r in results if r.id == "recent_match_skip").passed
    assert len(mail.sent_messages) == 1


def test_blank_stored_as_zero_fails_facts_only(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    entry, results = record_count_result.execute(
        registry=registry,
        action_log=log,
        store=store,
        task={"run_id": "20261124_DAILY", "bin_code": "N01-01", "book_boxes": 4},
        result_row={
            "run_id": "20261124_DAILY",
            "bin_code": "N01-01",
            "counted_boxes": 0,
            "diff_boxes": -4,
            "cause_class": "EMPTY",
            "field_notes": "",
        },
        started_at="2026-11-24T09:30:00",
        raw_sheet_value="",
    )
    assert entry.outcome == "HANDED_TO_PERSON"
    assert not next(r for r in results if r.id == "facts_only").passed


def test_quantity_inferred_from_field_notes_fails_facts_only(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    entry, results = record_count_result.execute(
        registry=registry,
        action_log=log,
        store=store,
        task={"run_id": "20261124_DAILY", "bin_code": "N01-01", "book_boxes": 4},
        result_row={
            "run_id": "20261124_DAILY",
            "bin_code": "N01-01",
            "counted_boxes": 2,
            "diff_boxes": -2,
            "cause_class": "LINE_SCAN_MISS",
            "field_notes": "looks like 2 boxes",
            "quantity_source": "field_notes",
        },
        started_at="2026-11-24T09:30:00",
    )
    assert entry.outcome == "HANDED_TO_PERSON"
    assert not next(r for r in results if r.id == "facts_only").passed


def test_transfer_lowering_accuracy_fails_accuracy_not_down(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    # SL20 is ALREADY balanced (diff=0), SL10 is also balanced (diff=0) except we force a transfer out of SL20 -> breaks both!
    rows = [
        {"material_id": "PART-0001", "sloc": "SL10", "snapshot_date": "2026-11-24", "erp_qty": 100.0, "wms_qty": 150.0, "diff_qty": -50.0, "consecutive_days_different": 2},
        {"material_id": "PART-0001", "sloc": "SL20", "snapshot_date": "2026-11-24", "erp_qty": 200.0, "wms_qty": 200.0, "diff_qty": 0.0, "consecutive_days_different": 0},
        {"material_id": "PART-0002", "sloc": "SL10", "snapshot_date": "2026-11-24", "erp_qty": 100.0, "wms_qty": 100.0, "diff_qty": 0.0, "consecutive_days_different": 0},
    ]
    store.append_snapshot("stock_position_snapshot", rows, source_name="t1", captured_at="2026-11-24T07:10:00")
    store.append_snapshot("stock_position_snapshot", rows, source_name="t2", captured_at="2026-11-24T07:35:00")

    erp = FakeERPAdapter()
    # Moving 100 out of matched SL20 into SL10 leaves SL10 at +50 (unmatched) and SL20 at -100 (unmatched): accuracy drops 2/3 -> 1/3!
    entry, results, posted = post_stock_transfer.execute(
        registry=registry,
        action_log=log,
        store=store,
        erp=erp,
        material_id="PART-0001",
        from_sloc="SL20",
        to_sloc="SL10",
        qty=100.0,
        reason="bad_over_transfer",
        started_at="2026-11-24T08:00:00",
    )
    assert posted is None
    assert entry.outcome == "HANDED_TO_PERSON"
    assert not next(r for r in results if r.id == "accuracy_not_down").passed


def test_repeated_portal_line_fails_output_verified_and_sends_with_flags(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    _, log = _init_store(tmp_path)
    runs = [
        {"truck_run_id": "SUP-003-2026-11-24-0800", "supplier_id": "SUP-003", "run_date": "2026-11-24", "slot": "08:00"},
        {"truck_run_id": "SUP-003-2026-11-24-1000", "supplier_id": "SUP-003", "run_date": "2026-11-24", "slot": "10:00"},
    ]
    dup_lines = [
        {"order_no": "DO-DUP", "material_id": "PART-0005", "supplier_id": "SUP-003", "due_window": "2026-11-24 08:00", "ordered_boxes": 6, "truck_run_id": "SUP-003-2026-11-24-0800"},
        {"order_no": "DO-DUP", "material_id": "PART-0005", "supplier_id": "SUP-003", "due_window": "2026-11-24 10:00", "ordered_boxes": 6, "truck_run_id": "SUP-003-2026-11-24-1000"},
    ]
    mail = FakeMailAdapter()
    portal = FakeSupplierPortalAdapter()
    entry, results, flagged = send_truck_plan.execute(
        registry=registry,
        action_log=log,
        portal=portal,
        mail=mail,
        owner=OWNER,
        supplier={"supplier_id": "SUP-003", "trailer_cap_carts": 24},
        run_date="2026-11-24",
        portal_lines=dup_lines,
        plan_lines=dup_lines,
        truck_runs=runs,
        overload_triggered=False,
        starting_stock_boxes={"PART-0005": 4.0},
        slot_demand_boxes={("PART-0005", "SUP-003-2026-11-24-0800"): 4.0},
        started_at="2026-11-24T06:40:00",
    )
    assert entry.outcome == "SENT_WITH_FLAGS"
    assert not next(r for r in results if r.id == "output_verified").passed
    assert all(ln["flag"] == "check this line on the supplier portal" for ln in flagged)
    assert len(portal.published_plans) == 1
    assert len(mail.sent_messages) == 1


def test_stock_shortage_fails_no_shortage(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    _, log = _init_store(tmp_path)
    runs = [
        {"truck_run_id": "SUP-001-2026-11-24-0800", "supplier_id": "SUP-001", "run_date": "2026-11-24", "slot": "08:00"},
    ]
    lines = [
        {"order_no": "DO-1", "material_id": "PART-0001", "supplier_id": "SUP-001", "due_window": "2026-11-24 08:00", "ordered_boxes": 5, "truck_run_id": "SUP-001-2026-11-24-0800"},
    ]
    entry, results, _ = send_truck_plan.execute(
        registry=registry,
        action_log=log,
        portal=FakeSupplierPortalAdapter(),
        mail=FakeMailAdapter(),
        owner=OWNER,
        supplier={"supplier_id": "SUP-001", "trailer_cap_carts": 24},
        run_date="2026-11-24",
        portal_lines=lines,
        plan_lines=lines,
        truck_runs=runs,
        overload_triggered=False,
        starting_stock_boxes={"PART-0001": 2.0},
        slot_demand_boxes={("PART-0001", "SUP-001-2026-11-24-0800"): 10.0},
        started_at="2026-11-24T06:40:00",
    )
    assert entry.outcome == "SENT_WITH_FLAGS"
    assert not next(r for r in results if r.id == "no_shortage").passed


def test_self_reviewed_change_fails_reviewed_change(tmp_path: Path) -> None:
    registry = load_registry(validate=True)
    store, log = _init_store(tmp_path)
    entry, results = change_count_scope.execute(
        registry=registry,
        action_log=log,
        store=store,
        change_record={
            "author": "agent.builder",
            "reviewer": "agent.builder",
            "approver": "person.owner",
            "owner": "person.owner",
        },
        sloc_row={"sloc": "SL90", "facility": "Facility North", "count_eligible": False},
        started_at="2026-11-23T16:00:00",
    )
    assert entry.outcome == "HANDED_TO_PERSON"
    assert not next(r for r in results if r.id == "reviewed_change").passed
