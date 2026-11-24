"""Deterministic action log persistence and CSV export."""

from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from actions.guards import GuardResult
from ledgers.snapshot_store import open_readonly

VALID_OUTCOMES = frozenset({"DONE", "HELD", "HANDED_TO_PERSON", "SENT_WITH_FLAGS"})


@dataclass(frozen=True)
class ActionLogEntry:
    action_id: str
    action_type: str
    performed_by: str
    started_at: str
    inputs_ref: str
    guard_results: list[dict[str, Any]]
    outcome: str
    approval: str
    outputs_ref: str


class ActionLog:
    """Append-only audit log for every action run with deterministic action IDs."""

    def __init__(self, db_path: Path | str, *, run_date: str | None = None) -> None:
        self.db_path = Path(db_path).resolve()
        self._run_date_tag = run_date.replace("-", "") if run_date else None

    def _next_action_id(self, conn: sqlite3.Connection, started_at: str) -> str:
        tag = self._run_date_tag or str(started_at)[:10].replace("-", "")
        if len(tag) != 8 or not tag.isdigit():
            tag = "00000000"
        count = int(conn.execute("SELECT COUNT(*) FROM action_log").fetchone()[0])
        return f"ACT-{tag}-{count + 1:04d}"

    def record(
        self,
        *,
        action_type: str,
        performed_by: str,
        started_at: str,
        inputs_ref: str,
        guard_results: Iterable[GuardResult],
        outcome: str,
        approval: str,
        outputs_ref: str,
        action_id: str | None = None,
    ) -> ActionLogEntry:
        """Write one action execution record to the ledger database."""
        if outcome not in VALID_OUTCOMES:
            raise ValueError(f"Invalid action outcome: {outcome}")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            resolved_id = action_id or self._next_action_id(conn, started_at)
            entry = ActionLogEntry(
                action_id=resolved_id,
                action_type=action_type,
                performed_by=performed_by,
                started_at=started_at,
                inputs_ref=inputs_ref,
                guard_results=[g.to_dict() for g in guard_results],
                outcome=outcome,
                approval=approval,
                outputs_ref=outputs_ref,
            )
            conn.execute(
                """
                INSERT INTO action_log (
                    action_id, action_type, performed_by, started_at,
                    inputs_ref, guard_results, outcome, approval, outputs_ref
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.action_id,
                    entry.action_type,
                    entry.performed_by,
                    entry.started_at,
                    entry.inputs_ref,
                    json.dumps(entry.guard_results, sort_keys=True),
                    entry.outcome,
                    entry.approval,
                    entry.outputs_ref,
                ),
            )
            conn.commit()
        return entry

    def fetch_all(self) -> list[dict[str, Any]]:
        """Read all logged action rows via a read-only connection."""
        conn = open_readonly(self.db_path)
        try:
            rows = conn.execute(
                """
                SELECT action_id, action_type, performed_by, started_at,
                       inputs_ref, guard_results, outcome, approval, outputs_ref
                FROM action_log
                ORDER BY started_at ASC, action_id ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_csv(self, output_path: Path | str) -> Path:
        """Write all action log entries to a CSV file."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = self.fetch_all()
        fieldnames = [
            "action_id",
            "action_type",
            "performed_by",
            "started_at",
            "inputs_ref",
            "guard_results",
            "outcome",
            "approval",
            "outputs_ref",
        ]
        with out.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return out
