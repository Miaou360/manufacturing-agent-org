"""Daily plant simulation runner driven strictly by agents/schedule.yaml sim_at order.

Usage:
    python -m simulation.run_day --seed 7 --date 2026-11-24
"""

from __future__ import annotations

import argparse
import csv
import time
from collections import Counter
from pathlib import Path
from typing import Any

from actions import (
    change_count_scope,
    clear_interface_error,
    post_stock_transfer,
    publish_count_list,
    record_count_result,
    send_delivery_status,
    send_truck_plan,
)
from actions.action_log import ActionLog
from adapters.erp import FakeERPAdapter
from adapters.mail import FakeMailAdapter
from adapters.supplier_portal import FakeSupplierPortalAdapter
from adapters.wms import FakeWMSAdapter
from agents.run_agent import load_schedule, simulation_steps
from ledgers.snapshot_store import SnapshotStore
from logic.count_selection import select_count_tasks
from logic.mismatch_cause import classify_mismatch_cause
from logic.receipt_matching import match_receipts_to_trucks, summarize_delivery_status
from logic.truck_leveling import level_truck_orders
from logic.turnover_profile import compute_turnover_profiles
from ontology.registry import OntologyRegistry, load_registry
from simulation.synthetic_plant import feed_synthetic_plant

OWNER_EMAIL = "owner@example.invalid"


def _fetch_view(store: SnapshotStore, registry: OntologyRegistry, view_name: str) -> list[dict[str, Any]]:
    conn = store.open_ro()
    try:
        registry.materialize(conn)
        rows = conn.execute(f'SELECT * FROM temp."{view_name}"').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    return path


def run_day(
    *,
    seed: int = 7,
    run_date: str = "2026-11-24",
    out_dir: Path | str = "out",
) -> dict[str, Any]:
    """Run one complete simulated plant day in schedule.yaml sim_at order."""
    t0 = time.perf_counter()
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    db_path = out_path / f"plant_ledger_{run_date.replace('-', '')}.db"
    if db_path.exists():
        db_path.unlink()

    registry = load_registry(validate=True, check_modules=True, check_guards=True)
    schedule = load_schedule()
    steps = simulation_steps(schedule)

    store = SnapshotStore(db_path)
    store.init_db()
    plant = feed_synthetic_plant(store, run_date=run_date, seed=seed)

    action_log = ActionLog(db_path, run_date=run_date)
    erp = FakeERPAdapter()
    wms = FakeWMSAdapter(captured_at=f"{run_date}T06:30:00")
    portal = FakeSupplierPortalAdapter()
    mail = FakeMailAdapter()

    # Governance baseline on D-1 at 16:00: SL90 quarantine marked count_eligible=false via reviewed change
    change_count_scope.execute(
        registry=registry,
        action_log=action_log,
        store=store,
        change_record={
            "author": "agent.builder",
            "reviewer": "agent.architect",
            "approver": "person.owner",
            "owner": "person.owner",
        },
        sloc_row={"sloc": "SL90", "facility": "Facility North", "count_eligible": False},
        started_at=f"{plant.prev_date}T16:00:00",
    )

    ro_conn = store.open_ro()
    try:
        materialized = registry.materialize(ro_conn)
    finally:
        ro_conn.close()

    # Shared state produced as each scheduled actor runs in sim_at order
    executed_order: list[tuple[str, str]] = []
    delivery_status_rows: list[dict[str, Any]] = []
    truck_plan_rows: list[dict[str, Any]] = []
    count_tasks: list[dict[str, Any]] = []
    excluded_by_sloc: list[str] = []
    skipped_bins: dict[str, str] = {}
    leveled_moves_total = 0
    flagged_plans = 0
    stale_outcome = ""
    reconciliation_accuracy = 0.0

    for step in steps:
        actor_id = str(step["id"])
        sim_at = str(step["sim_at"])
        step_ts = f"{run_date}T{sim_at}:00"
        executed_order.append((sim_at, actor_id))

        if actor_id == "agent.receipt_tracking":
            # 05:00: Compare D-1 truck plans against D-1 warehouse receipt scans
            d1_runs = [
                r for r in _fetch_view(store, registry, "TruckRun") if str(r["run_date"]) == plant.prev_date
            ]
            d1_orders = store.query_ro(
                """
                SELECT order_no, material_id, supplier_id, due_window, ordered_boxes, truck_run_id
                FROM delivery_order_snapshot
                WHERE source_name IS NULL OR snapshot_id IN (
                    SELECT snapshot_id FROM ledger_provenance WHERE source_name = 'supplier_portal_orders_d1'
                )
                """
                if False
                else """
                SELECT d.order_no, d.material_id, d.supplier_id, d.due_window, d.ordered_boxes, d.truck_run_id
                FROM delivery_order_snapshot d
                JOIN ledger_provenance p ON p.snapshot_id = d.snapshot_id
                WHERE p.source_name = 'supplier_portal_orders_d1'
                """
            )
            d1_receipts = [
                gr
                for gr in _fetch_view(store, registry, "GoodsReceipt")
                if str(gr["received_at"]).startswith(plant.prev_date)
            ]
            matched_d1 = match_receipts_to_trucks(
                receipts=d1_receipts,
                truck_runs=d1_runs,
                order_lines=d1_orders,
                material_suppliers=plant.material_suppliers,
            )
            store.append_snapshot(
                "goods_receipt",
                matched_d1,
                source_name="logic.receipt_matching",
                captured_at=step_ts,
            )
            delivery_status_rows = summarize_delivery_status(
                order_lines=d1_orders,
                matched_receipts=matched_d1,
                truck_runs=d1_runs,
            )
            verdict_counts = Counter(r["status"] for r in delivery_status_rows)
            send_delivery_status.execute(
                registry=registry,
                action_log=action_log,
                mail=mail,
                owner=OWNER_EMAIL,
                recipients=[OWNER_EMAIL],
                owner_approved=False,
                subject=f"Daily delivery check ({plant.prev_date} trucks, run {run_date} {sim_at})",
                body=f"Checked {len(delivery_status_rows)} D-1 lines: {dict(sorted(verdict_counts.items()))}",
                started_at=step_ts,
            )

        elif actor_id == "agent.order_dispatch":
            # 06:40: Level today's supplier truck orders and send plans
            suppliers = {s["supplier_id"]: s for s in _fetch_view(store, registry, "Supplier")}
            d_runs = [
                r for r in _fetch_view(store, registry, "TruckRun") if str(r["run_date"]) == run_date
            ]
            d_portal_orders = store.query_ro(
                """
                SELECT d.order_no, d.material_id, d.supplier_id, d.due_window, d.ordered_boxes, d.truck_run_id
                FROM delivery_order_snapshot d
                JOIN ledger_provenance p ON p.snapshot_id = d.snapshot_id
                WHERE p.source_name = 'supplier_portal_orders'
                ORDER BY d.load_seq ASC
                """
            )
            all_plan_lines: list[dict[str, Any]] = []
            all_updated_runs: list[dict[str, Any]] = []

            for sup_id in sorted({str(ln["supplier_id"]) for ln in d_portal_orders}):
                sup_portal = [ln for ln in d_portal_orders if str(ln["supplier_id"]) == sup_id]
                sup_runs = [r for r in d_runs if str(r["supplier_id"]) == sup_id]
                sup_spec = suppliers[sup_id]
                cap = int(sup_spec["trailer_cap_carts"])

                leveled = level_truck_orders(
                    order_lines=sup_portal,
                    truck_runs=sup_runs,
                    trailer_cap_carts=cap,
                    starting_stock_boxes=plant.starting_stock_boxes,
                    slot_demand_boxes=plant.slot_demand_boxes,
                )
                leveled_moves_total += len(leveled.moves)

                entry, _, sent_lines = send_truck_plan.execute(
                    registry=registry,
                    action_log=action_log,
                    portal=portal,
                    mail=mail,
                    owner=OWNER_EMAIL,
                    supplier=sup_spec,
                    run_date=run_date,
                    portal_lines=sup_portal,
                    plan_lines=leveled.order_lines,
                    truck_runs=leveled.truck_runs,
                    overload_triggered=leveled.overload_triggered,
                    starting_stock_boxes=plant.starting_stock_boxes,
                    slot_demand_boxes=plant.slot_demand_boxes,
                    started_at=step_ts,
                )
                if entry.outcome == "SENT_WITH_FLAGS":
                    flagged_plans += 1

                all_plan_lines.extend(sent_lines)
                all_updated_runs.extend(leveled.truck_runs)
                for ln in sent_lines:
                    truck_plan_rows.append(
                        {
                            "supplier_id": sup_id,
                            "run_date": run_date,
                            "truck_run_id": ln["truck_run_id"],
                            "portal_slot": ln.get("portal_slot", ""),
                            "slot": ln.get("slot", ""),
                            "order_no": ln["order_no"],
                            "material_id": ln["material_id"],
                            "ordered_boxes": ln["ordered_boxes"],
                            "planned_truck_carts": leveled.planned_carts_by_truck.get(ln["truck_run_id"], 0),
                            "trailer_cap_carts": cap,
                            "changed": bool(ln.get("changed", False)),
                            "flag": ln.get("flag", ""),
                        }
                    )

            store.append_snapshot(
                "delivery_order_snapshot",
                all_plan_lines,
                source_name="logic.truck_leveling",
                captured_at=step_ts,
            )
            store.append_snapshot(
                "truck_run",
                all_updated_runs,
                source_name="logic.truck_leveling",
                captured_at=step_ts,
            )

        elif actor_id == "agent.reconciliation":
            # 06:45: Read materialized StockPosition snapshot and compute baseline accuracy
            positions = _fetch_view(store, registry, "StockPosition")
            if positions:
                matched_pos = sum(1 for p in positions if abs(float(p["diff_qty"])) < 1e-9)
                reconciliation_accuracy = round(matched_pos / len(positions), 4)

        elif actor_id == "agent.stock_accuracy":
            # 06:50: Compute turnover profile, then select and publish daily count list
            materials = _fetch_view(store, registry, "Material")
            boxes = _fetch_view(store, registry, "Box")
            bins = _fetch_view(store, registry, "Bin")
            zones = _fetch_view(store, registry, "Zone")
            slocs = _fetch_view(store, registry, "StorageLocation")

            turnover = compute_turnover_profiles(
                materials=materials,
                boxes=boxes,
                bins=bins,
                zones=zones,
                profile_as_of=step_ts,
            )
            store.append_snapshot(
                "material_master",
                turnover["materials"],
                source_name="logic.turnover_profile",
                captured_at=step_ts,
            )
            store.append_snapshot(
                "wms_box_snapshot",
                turnover["boxes"],
                source_name="logic.turnover_profile",
                captured_at=f"{run_date}T06:30:00",
            )
            store.append_snapshot(
                "zone_master",
                turnover["zones"],
                source_name="logic.turnover_profile",
                captured_at=step_ts,
            )

            materials_fresh = _fetch_view(store, registry, "Material")
            boxes_fresh = _fetch_view(store, registry, "Box")
            prior_results = _fetch_view(store, registry, "CountResult")
            prior_tasks = _fetch_view(store, registry, "CountTask")

            prior_task_dates = {(t["run_id"], t["bin_code"]): t["list_date"] for t in prior_tasks}
            elig_slocs = {s["sloc"] for s in slocs if s["count_eligible"]}
            recent_matches: dict[str, dict[str, Any]] = {}
            for res_row in prior_results:
                if res_row.get("diff_boxes") == 0:
                    bcode = str(res_row["bin_code"])
                    m_date = prior_task_dates.get((res_row["run_id"], bcode), run_date)
                    barcodes = sorted(
                        b["barcode"]
                        for b in boxes_fresh
                        if b["bin_code"] == bcode and b["status"] == "IN_STORAGE" and b["sloc"] in elig_slocs
                    )
                    recent_matches[bcode] = {"matched_at": m_date, "barcodes": barcodes}

            daily_run_id = f"{run_date.replace('-', '')}_DAILY"
            selection = select_count_tasks(
                run_id=daily_run_id,
                list_date=run_date,
                bins=bins,
                boxes=boxes_fresh,
                storage_locations=slocs,
                materials=materials_fresh,
                recent_matches=recent_matches,
                list_scope="DAILY",
                risk_limit=6,
                random_limit=3,
                seed=seed,
            )
            count_tasks = selection.tasks
            excluded_by_sloc = selection.excluded_by_sloc
            skipped_bins = selection.skipped_bins

            updated_bins = []
            for b in bins:
                b_copy = dict(b)
                if b_copy["bin_code"] in skipped_bins:
                    b_copy["skip_until"] = skipped_bins[b_copy["bin_code"]]
                updated_bins.append(b_copy)
            if skipped_bins:
                store.append_snapshot(
                    "bin_master",
                    updated_bins,
                    source_name="logic.count_selection",
                    captured_at=step_ts,
                )

            # Publish daily count list against the fresh D 06:30 snapshot (PASS -> DONE)
            publish_count_list.execute(
                registry=registry,
                action_log=action_log,
                store=store,
                wms=wms,
                mail=mail,
                owner=OWNER_EMAIL,
                run_id=daily_run_id,
                list_date=run_date,
                list_scope="DAILY",
                tasks=count_tasks,
                bins=updated_bins,
                boxes=boxes_fresh,
                storage_locations=slocs,
                snapshot_id=plant.fresh_box_snapshot_id,
                started_at=step_ts,
            )
            store.append_snapshot(
                "count_task",
                count_tasks,
                source_name="actions.publish_count_list",
                captured_at=step_ts,
            )

            # Built-in stale replay scenario: replay against D-1 05:30 snapshot (~25.3h old -> HELD + alert)
            stale_run_id = f"{run_date.replace('-', '')}_DAILY_REPLAY_STALE"
            stale_entry, _ = publish_count_list.execute(
                registry=registry,
                action_log=action_log,
                store=store,
                wms=wms,
                mail=mail,
                owner=OWNER_EMAIL,
                run_id=stale_run_id,
                list_date=run_date,
                list_scope="DAILY",
                tasks=count_tasks,
                bins=updated_bins,
                boxes=boxes_fresh,
                storage_locations=slocs,
                snapshot_id=plant.stale_box_snapshot_id,
                started_at=step_ts,
            )
            stale_outcome = stale_entry.outcome

        elif actor_id == "agent.interface_errors":
            # 08:00: Clear safe interface errors and post guarded stock transfers
            errors = _fetch_view(store, registry, "InterfaceError")
            for err in sorted(errors, key=lambda e: str(e["error_id"])):
                clear_interface_error.execute(
                    registry=registry,
                    action_log=action_log,
                    erp=erp,
                    error=err,
                    reason="safe_auto_clear_non_receipt",
                    started_at=step_ts,
                )

            deficit_materials = sorted(
                {
                    str(p["material_id"])
                    for p in _fetch_view(store, registry, "StockPosition")
                    if float(p["diff_qty"]) < -1e-9
                }
            )
            for mat_id in deficit_materials:
                post_stock_transfer.execute(
                    registry=registry,
                    action_log=action_log,
                    store=store,
                    erp=erp,
                    material_id=mat_id,
                    reason="persistent_erp_wms_gap",
                    started_at=step_ts,
                )

        elif actor_id == "person.warehouse_team":
            # 09:30: Record physical count results returned by the warehouse team
            if len(count_tasks) >= 2:
                t0_task = count_tasks[0]
                counted_0 = max(0, int(t0_task["book_boxes"]) - 1)
                cause_0 = classify_mismatch_cause(
                    book_boxes=int(t0_task["book_boxes"]),
                    counted_boxes=counted_0,
                    field_notes="pulled to line without scan",
                )
                record_count_result.execute(
                    registry=registry,
                    action_log=action_log,
                    store=store,
                    task=t0_task,
                    result_row={
                        "run_id": t0_task["run_id"],
                        "bin_code": t0_task["bin_code"],
                        "counted_boxes": counted_0,
                        "diff_boxes": counted_0 - int(t0_task["book_boxes"]),
                        "cause_class": cause_0,
                        "field_notes": "pulled to line without scan",
                    },
                    started_at=step_ts,
                )
                t1_task = count_tasks[1]
                record_count_result.execute(
                    registry=registry,
                    action_log=action_log,
                    store=store,
                    task=t1_task,
                    result_row={
                        "run_id": t1_task["run_id"],
                        "bin_code": t1_task["bin_code"],
                        "counted_boxes": None,
                        "diff_boxes": None,
                        "cause_class": "UNCLASSIFIED",
                        "field_notes": "bin blocked by forklift, not counted",
                    },
                    started_at=step_ts,
                    raw_sheet_value="",
                )

    # Write deterministic out/ artifacts
    count_csv_rows = [{**t, "counted_boxes": ""} for t in count_tasks]
    count_list_csv = _write_csv(
        out_path / "count_list.csv",
        count_csv_rows,
        [
            "run_id",
            "list_date",
            "rank",
            "lane",
            "bin_code",
            "materials",
            "book_boxes",
            "counted_boxes",
            "risk_score",
            "reason",
        ],
    )
    truck_plans_csv = _write_csv(
        out_path / "truck_plans.csv",
        truck_plan_rows,
        [
            "supplier_id",
            "run_date",
            "truck_run_id",
            "portal_slot",
            "slot",
            "order_no",
            "material_id",
            "ordered_boxes",
            "planned_truck_carts",
            "trailer_cap_carts",
            "changed",
            "flag",
        ],
    )
    delivery_status_csv = _write_csv(
        out_path / "delivery_status.csv",
        delivery_status_rows,
        [
            "supplier_id",
            "truck_run_id",
            "material_id",
            "planned_boxes",
            "received_boxes",
            "variance_boxes",
            "status",
        ],
    )
    action_log_csv = action_log.export_csv(out_path / "action_log.csv")

    all_actions = action_log.fetch_all()
    outcome_counts = Counter(row["outcome"] for row in all_actions)
    verdict_counts = Counter(row["status"] for row in delivery_status_rows)
    sim_obj_count = len(registry.simulated_objects())
    elapsed = round(time.perf_counter() - t0, 3)

    summary_md_path = out_path / "summary.md"
    summary_lines = [
        f"# Daily Plant Simulation Summary ({run_date}, seed={seed})",
        "",
        f"- Ontology: {len(registry.object_types)} object types ({sim_obj_count} simulated), {len(registry.link_types)} links, {len(registry.action_types)} actions",
        f"- Schedule order: {', '.join(f'{t} {a}' for t, a in executed_order)}",
        f"- Delivery check (D-1 {plant.prev_date}, run 05:00): {dict(sorted(verdict_counts.items()))}",
        f"- Truck dispatch (06:40): {leveled_moves_total} pull-forward move(s); {flagged_plans} plan(s) SENT_WITH_FLAGS (duplicate portal line)",
        f"- Reconciliation (06:45): baseline position accuracy {reconciliation_accuracy:.1%}",
        f"- Count list (06:50): {len(count_tasks)} bins published (SL90 excluded: {excluded_by_sloc}; cooldown skipped: {list(skipped_bins)})",
        f"- Stale replay scenario (~25.3h old snapshot): {stale_outcome} (owner alert raised)",
        f"- Interface errors (08:00, fake ERP): cleared={len(erp.cleared_errors)}, transfers={len(erp.posted_transfers)} (receipt-linked & in-flight handed to person)",
        f"- Action log rows: {len(all_actions)} ({dict(sorted(outcome_counts.items()))})",
        "",
        "## Generated Artifacts",
        f"- `{count_list_csv.name}`",
        f"- `{truck_plans_csv.name}`",
        f"- `{delivery_status_csv.name}`",
        f"- `{action_log_csv.name}`",
    ]
    with summary_md_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(summary_lines) + "\n")

    return {
        "run_date": run_date,
        "prev_date": plant.prev_date,
        "seed": seed,
        "db_path": str(db_path),
        "out_dir": str(out_path),
        "ontology_objects": len(registry.object_types),
        "simulated_objects": sim_obj_count,
        "ontology_links": len(registry.link_types),
        "ontology_actions": len(registry.action_types),
        "executed_order": executed_order,
        "ledger_loads": len(plant.receipts),
        "materialized_counts": materialized,
        "delivery_verdicts": dict(sorted(verdict_counts.items())),
        "count_tasks": len(count_tasks),
        "excluded_by_sloc": excluded_by_sloc,
        "skipped_bins": skipped_bins,
        "leveled_moves": leveled_moves_total,
        "flagged_plans": flagged_plans,
        "cleared_errors": len(erp.cleared_errors),
        "posted_transfers": len(erp.posted_transfers),
        "action_log_rows": len(all_actions),
        "outcome_counts": dict(sorted(outcome_counts.items())),
        "elapsed_sec": elapsed,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one simulated plant day.")
    parser.add_argument("--seed", type=int, default=7, help="Deterministic RNG seed.")
    parser.add_argument("--date", dest="run_date", default="2026-11-24", help="Simulation date (YYYY-MM-DD).")
    parser.add_argument("--out-dir", default="out", help="Directory for simulation outputs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    summary = run_day(seed=args.seed, run_date=args.run_date, out_dir=args.out_dir)
    order_str = ", ".join(f"{t} {a}" for t, a in summary["executed_order"])
    lines = [
        f"Plant simulation complete: date={summary['run_date']} seed={summary['seed']} elapsed={summary['elapsed_sec']}s",
        f"Ontology: {summary['ontology_objects']} objects ({summary['simulated_objects']} simulated), {summary['ontology_links']} links, {summary['ontology_actions']} actions",
        f"Schedule order: {order_str}",
        f"Delivery check (05:00, {summary['prev_date']}): {summary['delivery_verdicts']}",
        f"Truck dispatch (06:40): {summary['leveled_moves']} pull-forward move(s), {summary['flagged_plans']} plan(s) SENT_WITH_FLAGS",
        f"Count list (06:50): {summary['count_tasks']} bins published | SL90 excluded={summary['excluded_by_sloc']} | skipped={list(summary['skipped_bins'])} | stale replay=HELD",
        f"Interface errors (08:00): cleared={summary['cleared_errors']}, transfers={summary['posted_transfers']} (receipt-linked & in-flight handed to person)",
        f"Action log: {summary['action_log_rows']} rows -> {summary['outcome_counts']}",
        f"Outputs written to: {summary['out_dir']}",
    ]
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
