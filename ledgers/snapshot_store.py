"""Append-only SQLite snapshot ledger with provenance and read-only cross-ledger queries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "material_master": (
        "material_id",
        "description",
        "unit_pack_qty",
        "small_part",
        "turnover_days_trimmed_mean",
        "turnover_days_median",
        "mover_class",
        "profile_as_of",
    ),
    "wms_box_snapshot": (
        "barcode",
        "material_id",
        "qty",
        "received_at",
        "bin_code",
        "sloc",
        "status",
        "dwell_days",
    ),
    "bin_master": (
        "bin_code",
        "facility",
        "zone_code",
        "rack_level",
        "count_scope",
        "last_counted_at",
        "skip_until",
    ),
    "zone_master": (
        "zone_code",
        "median_dwell_days",
        "lookback_days",
    ),
    "sloc_master": (
        "sloc",
        "facility",
        "count_eligible",
    ),
    "stock_position_snapshot": (
        "material_id",
        "sloc",
        "snapshot_date",
        "erp_qty",
        "wms_qty",
        "diff_qty",
        "consecutive_days_different",
    ),
    "count_task": (
        "run_id",
        "bin_code",
        "list_date",
        "lane",
        "rank",
        "risk_score",
        "reason",
        "book_boxes",
    ),
    "count_result": (
        "run_id",
        "bin_code",
        "counted_boxes",
        "diff_boxes",
        "cause_class",
        "field_notes",
    ),
    "interface_error_snapshot": (
        "error_id",
        "material_id",
        "movement_type",
        "sloc",
        "qty",
        "receipt_linked",
        "status",
    ),
    "stock_transfer": (
        "document_no",
        "material_id",
        "from_sloc",
        "to_sloc",
        "qty",
        "status",
        "reason",
        "resolves_error_id",
        "resolves_backlog_line_id",
    ),
    "backlog_snapshot": (
        "backlog_line_id",
        "material_id",
        "order_no",
        "sloc",
        "qty",
    ),
    "supplier_master": (
        "supplier_id",
        "trailer_cap_carts",
        "truck_slots",
    ),
    "delivery_order_snapshot": (
        "order_no",
        "material_id",
        "supplier_id",
        "due_window",
        "ordered_boxes",
        "truck_run_id",
    ),
    "truck_run": (
        "truck_run_id",
        "supplier_id",
        "run_date",
        "slot",
        "planned_carts",
        "status",
    ),
    "goods_receipt": (
        "barcode",
        "material_id",
        "qty",
        "received_at",
        "erp_posted",
        "truck_run_id",
    ),
}


@dataclass(frozen=True)
class LoadReceipt:
    """Provenance receipt returned after appending a snapshot batch."""

    snapshot_id: str
    table_name: str
    source_name: str
    file_sha256: str
    captured_at: str
    row_count: int


def sha256_file(path: Path | str) -> str:
    """Return the lowercase hex SHA-256 digest of a file."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_payload(rows: list[dict[str, Any]]) -> str:
    """Compute a deterministic SHA-256 digest for in-memory snapshot rows."""
    encoded = json.dumps(rows, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def open_readonly(db_path: Path | str) -> sqlite3.Connection:
    """Open a ledger database strictly in SQLite read-only URI mode."""
    resolved = Path(db_path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Ledger database does not exist: {resolved}")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _normalize_cell(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


class SnapshotStore:
    """Append-only SQLite store for source snapshots and derived object states."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path).resolve()

    def init_db(self) -> None:
        """Create ledger tables and enable WAL mode for concurrent reads."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript(schema_sql)

    def open_ro(self) -> sqlite3.Connection:
        """Open this store in strict read-only mode."""
        return open_readonly(self.db_path)

    def append_snapshot(
        self,
        table_name: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        source_name: str,
        captured_at: str,
        file_sha256: str | None = None,
        snapshot_id: str | None = None,
    ) -> LoadReceipt:
        """Append rows to a ledger table and record one provenance entry."""
        if table_name not in TABLE_COLUMNS:
            raise ValueError(f"Unknown ledger table: {table_name}")

        cols = TABLE_COLUMNS[table_name]
        row_list = [dict(r) for r in rows]
        digest = file_sha256 or sha256_payload(row_list)
        snap_id = snapshot_id or f"{table_name}:{captured_at}:{digest[:8]}"

        insert_cols = ("snapshot_id", "captured_at") + cols
        placeholders = ", ".join("?" for _ in insert_cols)
        sql = f"INSERT INTO {table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})"

        values = [
            tuple(
                [snap_id, captured_at]
                + [_normalize_cell(row.get(col)) for col in cols]
            )
            for row in row_list
        ]

        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            if values:
                conn.executemany(sql, values)
            conn.execute(
                """
                INSERT INTO ledger_provenance
                    (snapshot_id, table_name, source_name, file_sha256, captured_at, row_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (snap_id, table_name, source_name, digest, captured_at, len(values)),
            )
            conn.commit()

        return LoadReceipt(
            snapshot_id=snap_id,
            table_name=table_name,
            source_name=source_name,
            file_sha256=digest,
            captured_at=captured_at,
            row_count=len(values),
        )

    def list_provenance(self, table_name: str | None = None) -> list[dict[str, Any]]:
        """Read provenance records via a read-only connection."""
        conn = self.open_ro()
        try:
            if table_name:
                cur = conn.execute(
                    """
                    SELECT load_id, snapshot_id, table_name, source_name,
                           file_sha256, captured_at, row_count
                    FROM ledger_provenance
                    WHERE table_name = ?
                    ORDER BY load_id ASC
                    """,
                    (table_name,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT load_id, snapshot_id, table_name, source_name,
                           file_sha256, captured_at, row_count
                    FROM ledger_provenance
                    ORDER BY load_id ASC
                    """
                )
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()

    def latest_provenance(self, table_name: str) -> dict[str, Any] | None:
        """Return the most recent provenance entry for one ledger table."""
        rows = self.list_provenance(table_name=table_name)
        return rows[-1] if rows else None

    def query_ro(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Execute a read-only SQL query and return rows as dicts."""
        conn = self.open_ro()
        try:
            cur = conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
