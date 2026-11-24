"""End-to-end smoke, six-scenario, and determinism tests for simulation.run_day."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from ontology.registry import load_registry
from simulation.run_day import main, run_day


def test_run_day_all_six_scenarios_and_all_guards_logged(tmp_path: Path) -> None:
    summary = run_day(seed=7, run_date="2026-11-24", out_dir=tmp_path)
    assert summary["run_date"] == "2026-11-24"
    assert summary["ontology_objects"] == 15
    assert summary["simulated_objects"] == 14
    assert summary["ontology_links"] == 22
    assert summary["ontology_actions"] == 8
    assert summary["elapsed_sec"] < 60.0

    for fname in ("count_list.csv", "truck_plans.csv", "delivery_status.csv", "action_log.csv", "summary.md"):
        assert (tmp_path / fname).exists(), f"Missing output {fname}"

    # Scenario 1: SL90 quarantine bins excluded + cooldown skipped + RANDOM_BASELINE + new columns
    assert len(summary["excluded_by_sloc"]) == 2
    assert len(summary["skipped_bins"]) >= 1
    with (tmp_path / "count_list.csv").open("r", encoding="utf-8") as handle:
        count_rows = list(csv.DictReader(handle))
    listed_bins = {r["bin_code"] for r in count_rows}
    assert listed_bins.isdisjoint(set(summary["excluded_by_sloc"]))
    assert listed_bins.isdisjoint(set(summary["skipped_bins"]))
    assert any(r["lane"] == "RANDOM_BASELINE" for r in count_rows)
    assert all(r["materials"] for r in count_rows)
    assert all(r["counted_boxes"] == "" for r in count_rows)

    # Delivery check (05:00 on D-1) has realistic mix of all 5 verdicts
    with (tmp_path / "delivery_status.csv").open("r", encoding="utf-8") as handle:
        dlv_rows = list(csv.DictReader(handle))
    verdicts = {r["status"] for r in dlv_rows}
    assert verdicts == {"DELIVERED", "SHORT", "OVER", "NOT_DELIVERED", "UNPLANNED_RECEIPT"}

    # Truck plans: SUP-004 leveled, SUP-005 untouched, SUP-006 flagged duplicate
    with (tmp_path / "truck_plans.csv").open("r", encoding="utf-8") as handle:
        plan_rows = list(csv.DictReader(handle))
    assert any(r["supplier_id"] == "SUP-004" and r["changed"] == "True" for r in plan_rows)
    assert all(r["changed"] == "False" for r in plan_rows if r["supplier_id"] == "SUP-005")
    assert any(
        r["supplier_id"] == "SUP-006" and r["flag"] == "check this line on the supplier portal"
        for r in plan_rows
    )

    # Action log checks: all 16 guards present, deterministic IDs, no agent.any
    with (tmp_path / "action_log.csv").open("r", encoding="utf-8") as handle:
        log_rows = list(csv.DictReader(handle))
    assert len(log_rows) == summary["action_log_rows"]
    assert all(r["action_id"].startswith("ACT-20261124-") for r in log_rows)
    assert all(r["performed_by"] != "agent.any" for r in log_rows)

    seen_guards: set[str] = set()
    failed_guards_by_action: dict[str, set[str]] = {}
    outcomes_by_action: dict[str, set[str]] = {}
    for row in log_rows:
        act = row["action_type"]
        outcomes_by_action.setdefault(act, set()).add(row["outcome"])
        for g in json.loads(row["guard_results"]):
            seen_guards.add(g["id"])
            if not g["passed"]:
                failed_guards_by_action.setdefault(act, set()).add(g["id"])

    registry = load_registry(validate=True)
    declared_guards = {g.id for act in registry.action_types.values() for g in act.guards}
    assert declared_guards <= seen_guards

    # Scenario 2: stale replay -> HELD + raise_system_alert
    assert "HELD" in outcomes_by_action["publish_count_list"]
    assert "fresh_snapshot" in failed_guards_by_action["publish_count_list"]
    assert any("20261124_DAILY_REPLAY_STALE" in r["inputs_ref"] for r in log_rows)

    # Scenario 3: in-flight receipt blocks transfer
    assert "HANDED_TO_PERSON" in outcomes_by_action["post_stock_transfer"]
    assert "nothing_in_flight" in failed_guards_by_action["post_stock_transfer"]

    # Scenario 4 & 6: truck leveling + duplicate line SENT_WITH_FLAGS
    assert "DONE" in outcomes_by_action["send_truck_plan"]
    assert "SENT_WITH_FLAGS" in outcomes_by_action["send_truck_plan"]
    assert "output_verified" in failed_guards_by_action["send_truck_plan"]

    # Scenario 5: receipt-linked interface error handed to person
    assert "HANDED_TO_PERSON" in outcomes_by_action["clear_interface_error"]
    assert "not_receipt_linked" in failed_guards_by_action["clear_interface_error"]


def test_run_day_is_byte_deterministic(tmp_path: Path) -> None:
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    run_day(seed=7, run_date="2026-11-24", out_dir=out1)
    run_day(seed=7, run_date="2026-11-24", out_dir=out2)

    for fname in ("count_list.csv", "truck_plans.csv", "delivery_status.csv", "action_log.csv", "summary.md"):
        assert (out1 / fname).read_bytes() == (out2 / fname).read_bytes(), f"{fname} differed between runs"


def test_run_day_cli_exits_zero(tmp_path: Path) -> None:
    rc = main(["--seed", "7", "--date", "2026-11-24", "--out-dir", str(tmp_path)])
    assert rc == 0
