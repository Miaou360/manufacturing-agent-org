"""ERP adapter interface and deterministic in-memory fake for simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ERPAdapter(Protocol):
    """Boundary for ERP reads and postings."""

    def clear_interface_error(self, error_id: str, reason: str) -> dict[str, Any]:
        ...

    def post_stock_transfer(
        self,
        *,
        material_id: str,
        from_sloc: str,
        to_sloc: str,
        qty: float,
        reason: str,
    ) -> dict[str, Any]:
        ...


@dataclass
class FakeERPAdapter:
    """Deterministic fake ERP adapter that records postings without calling an external ERP."""

    cleared_errors: list[dict[str, Any]] = field(default_factory=list)
    posted_transfers: list[dict[str, Any]] = field(default_factory=list)
    _doc_seq: int = 100000

    def clear_interface_error(self, error_id: str, reason: str) -> dict[str, Any]:
        record = {"error_id": error_id, "status": "CLEARED", "reason": reason}
        self.cleared_errors.append(record)
        return record

    def post_stock_transfer(
        self,
        *,
        material_id: str,
        from_sloc: str,
        to_sloc: str,
        qty: float,
        reason: str,
    ) -> dict[str, Any]:
        self._doc_seq += 1
        doc_no = f"4900{self._doc_seq}"
        record = {
            "document_no": doc_no,
            "material_id": material_id,
            "from_sloc": from_sloc,
            "to_sloc": to_sloc,
            "qty": float(qty),
            "status": "POSTED",
            "reason": reason,
        }
        self.posted_transfers.append(record)
        return record
