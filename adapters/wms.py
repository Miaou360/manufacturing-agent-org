"""WMS adapter interface and deterministic fake for simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class WMSAdapter(Protocol):
    """Boundary for reading warehouse snapshots and publishing count lists."""

    def fetch_box_snapshot(self) -> tuple[str, list[dict[str, Any]]]:
        ...

    def publish_count_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> str:
        ...


@dataclass
class FakeWMSAdapter:
    """In-memory WMS fake serving synthetic snapshots and recording published count lists."""

    captured_at: str = ""
    boxes: list[dict[str, Any]] = field(default_factory=list)
    published_runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    def fetch_box_snapshot(self) -> tuple[str, list[dict[str, Any]]]:
        return self.captured_at, list(self.boxes)

    def publish_count_tasks(self, run_id: str, tasks: list[dict[str, Any]]) -> str:
        self.published_runs[run_id] = list(tasks)
        return f"count_list:{run_id}:{len(tasks)}"
