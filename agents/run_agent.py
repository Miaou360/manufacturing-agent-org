"""Scheduled agent configuration loader and simulation order helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

SCHEDULE_PATH = Path(__file__).resolve().parent / "schedule.yaml"


def load_schedule(path: Path | str | None = None) -> dict[str, Any]:
    """Load the agent schedule, people entries, and HQ coordination rules."""
    target = Path(path) if path is not None else SCHEDULE_PATH
    with target.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict) or "agents" not in data:
        raise ValueError(f"Invalid schedule YAML: {target}")
    return data


def all_agents(schedule: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return all declared agents in schedule.yaml order."""
    cfg = schedule if schedule is not None else load_schedule()
    return list(cfg.get("agents", []))


def simulated_agents(schedule: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return agents enabled for the daily synthetic simulation ordered by sim_at."""
    cfg = schedule if schedule is not None else load_schedule()
    active = [agent for agent in cfg.get("agents", []) if agent.get("simulated")]
    return sorted(active, key=lambda item: (str(item.get("sim_at", "")), str(item["id"])))


def simulation_steps(schedule: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return all simulated agents and people steps sorted by sim_at."""
    cfg = schedule if schedule is not None else load_schedule()
    steps: list[dict[str, Any]] = []
    for agent in cfg.get("agents", []):
        if agent.get("simulated") and agent.get("sim_at"):
            steps.append({**agent, "actor_kind": "agent"})
    for person in cfg.get("people", []):
        if person.get("sim_at"):
            steps.append({**person, "actor_kind": "person"})
    return sorted(steps, key=lambda item: (str(item["sim_at"]), str(item["id"])))
