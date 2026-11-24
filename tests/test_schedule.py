"""Tests verifying agents/schedule.yaml consistency and simulation execution order."""

from __future__ import annotations

from pathlib import Path

from agents.run_agent import all_agents, load_schedule, simulation_steps
from ontology.registry import load_registry
from simulation.run_day import run_day


def test_schedule_has_ten_agents_and_valid_actions() -> None:
    schedule = load_schedule()
    agents = all_agents(schedule)
    assert len(agents) == 10

    registry = load_registry(validate=True)
    valid_actions = set(registry.action_types)
    for agent in agents:
        for act in agent.get("actions", ()):
            assert act in valid_actions, f"Agent {agent['id']} references unknown action {act}"
    for person in schedule.get("people", ()):
        for act in person.get("actions", ()):
            assert act in valid_actions, f"Person {person['id']} references unknown action {act}"


def test_simulation_runs_in_exact_sim_at_order(tmp_path: Path) -> None:
    schedule = load_schedule()
    expected = [(str(step["sim_at"]), str(step["id"])) for step in simulation_steps(schedule)]
    assert expected == [
        ("05:00", "agent.receipt_tracking"),
        ("06:40", "agent.order_dispatch"),
        ("06:45", "agent.reconciliation"),
        ("06:50", "agent.stock_accuracy"),
        ("08:00", "agent.interface_errors"),
        ("09:30", "person.warehouse_team"),
    ]

    summary = run_day(seed=7, run_date="2026-11-24", out_dir=tmp_path)
    assert summary["executed_order"] == expected
