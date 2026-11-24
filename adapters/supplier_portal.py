"""Supplier portal adapter interface and deterministic fake for simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class SupplierPortalAdapter(Protocol):
    """Boundary for reading supplier delivery orders and sending leveled truck plans."""

    def fetch_delivery_orders(self) -> list[dict[str, Any]]:
        ...

    def publish_truck_plan(self, supplier_id: str, run_date: str, plan_rows: list[dict[str, Any]]) -> str:
        ...


@dataclass
class FakeSupplierPortalAdapter:
    """In-memory supplier portal fake for synthetic runs."""

    orders: list[dict[str, Any]] = field(default_factory=list)
    published_plans: list[dict[str, Any]] = field(default_factory=list)

    def fetch_delivery_orders(self) -> list[dict[str, Any]]:
        return list(self.orders)

    def publish_truck_plan(
        self,
        supplier_id: str,
        run_date: str,
        plan_rows: list[dict[str, Any]],
    ) -> str:
        ref = f"truck_plan:{supplier_id}:{run_date}:{len(plan_rows)}"
        self.published_plans.append(
            {
                "ref": ref,
                "supplier_id": supplier_id,
                "run_date": run_date,
                "rows": list(plan_rows),
            }
        )
        return ref
