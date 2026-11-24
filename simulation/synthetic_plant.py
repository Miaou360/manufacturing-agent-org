"""Synthetic plant generator feeding the append-only snapshot ledgers.

Reads the vendored synthetic plant snapshot derived from the 3D warehouse
visualizer demo data and appends deterministic operational snapshots for D-1
and D, including all six built-in guard scenarios.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from ledgers.snapshot_store import LoadReceipt, SnapshotStore, sha256_file

DEFAULT_DATA_PATH = Path(__file__).resolve().parent / "data" / "synthetic_plant_snapshot.json"
TRUCK_SLOTS = ("08:00", "10:00", "12:00", "14:00", "16:00")
SIM_SUPPLIERS = ("SUP-004", "SUP-005", "SUP-006")


@dataclass(frozen=True)
class SyntheticPlantContext:
    """Metadata and synthetic pulling plan returned after seeding the ledgers."""

    run_date: str
    prev_date: str
    receipts: dict[str, LoadReceipt]
    fresh_box_snapshot_id: str
    stale_box_snapshot_id: str
    material_suppliers: dict[str, str]
    starting_stock_boxes: dict[str, float]
    slot_demand_boxes: dict[tuple[str, str], float]


def load_vendored_snapshot(data_path: Path | str | None = None) -> tuple[dict[str, Any], str]:
    """Load the vendored synthetic plant JSON and return (payload, file_sha256)."""
    path = Path(data_path) if data_path is not None else DEFAULT_DATA_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload, sha256_file(path)


def _shift_date(iso_date: str, delta_days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=delta_days)).isoformat()


def feed_synthetic_plant(
    store: SnapshotStore,
    *,
    run_date: str = "2026-11-24",
    seed: int = 7,
    data_path: Path | str | None = None,
) -> SyntheticPlantContext:
    """Populate all simulated ledger tables with deterministic synthetic plant data.

    Built-in scenario fixtures included in the fed snapshots:
      1. StorageLocation SL90 (count_eligible=False) holds boxes in two occupied daily bins.
      2. An older wms_box_snapshot at D-1 05:30 enables the ~25.3h stale replay scenario.
      3. Paired surplus (SL20) and deficit (SL10) stock positions across two snapshots (25m apart),
         with one material blocked by an unposted in-flight goods receipt.
      4. Three suppliers with 5 slots each (08:00-16:00): SUP-001 overloaded (leveled),
         SUP-002 balanced (unchanged), SUP-003 repeated portal line (SENT_WITH_FLAGS).
      5. One receipt-linked interface error (101) handed to a person, one clearable (261).
      6. D-1 truck runs, order lines, and receipt scans producing a realistic mix of
         DELIVERED, SHORT, OVER, NOT_DELIVERED, and UNPLANNED_RECEIPT at 05:00.
    """
    rng = random.Random(seed)
    base, file_digest = load_vendored_snapshot(data_path)
    prev_day = _shift_date(run_date, -1)
    captured_d1_0530 = f"{prev_day}T05:30:00"
    captured_d_0630 = f"{run_date}T06:30:00"
    captured_d_0710 = f"{run_date}T07:10:00"
    captured_d_0735 = f"{run_date}T07:35:00"

    receipts: dict[str, LoadReceipt] = {}
    material_suppliers = {
        str(m["material_id"]): str(m["supplier_id"]) for m in base["materials"]
    }
    materials_by_supplier: dict[str, list[dict[str, Any]]] = {}
    for m in base["materials"]:
        materials_by_supplier.setdefault(str(m["supplier_id"]), []).append(m)

    # 1. Storage locations (SL10, SL20, SL30 eligible; SL90 starts eligible until D-1 16:00 reviewed change)
    initial_slocs = [
        {"sloc": "SL10", "facility": "Facility North", "count_eligible": True},
        {"sloc": "SL20", "facility": "Facility South", "count_eligible": True},
        {"sloc": "SL30", "facility": "Facility North", "count_eligible": True},
        {"sloc": "SL90", "facility": "Facility North", "count_eligible": False},
    ]
    receipts["sloc_master"] = store.append_snapshot(
        "sloc_master",
        initial_slocs,
        source_name="synthetic_plant_snapshot.json",
        captured_at=captured_d1_0530,
        file_sha256=file_digest,
    )

    # 2. Zones
    zone_rows = [
        {"zone_code": z, "median_dwell_days": None, "lookback_days": None}
        for z in base["zones"]
    ]
    receipts["zone_master"] = store.append_snapshot(
        "zone_master",
        zone_rows,
        source_name="synthetic_plant_snapshot.json",
        captured_at=captured_d_0630,
        file_sha256=file_digest,
    )

    # 3. Materials
    material_rows = [
        {
            "material_id": m["material_id"],
            "description": m["description"],
            "unit_pack_qty": m["unit_pack_qty"],
            "small_part": None,
            "turnover_days_trimmed_mean": None,
            "turnover_days_median": None,
            "mover_class": None,
            "profile_as_of": None,
        }
        for m in base["materials"]
    ]
    receipts["material_master"] = store.append_snapshot(
        "material_master",
        material_rows,
        source_name="synthetic_plant_snapshot.json",
        captured_at=captured_d_0630,
        file_sha256=file_digest,
    )

    # 4. Bins (two occupied daily bins hold SL90 quarantine stock; one had matching count 2d ago)
    occupied_bin_codes = {str(bx["bin_code"]) for bx in base["boxes"]}
    bin_rows = []
    daily_bins: list[str] = []
    occupied_daily_bins: list[str] = []
    for b in base["bins"]:
        row = {
            "bin_code": b["bin_code"],
            "facility": b["facility"],
            "zone_code": b["zone_code"],
            "rack_level": b["rack_level"],
            "count_scope": b["count_scope"],
            "last_counted_at": None,
            "skip_until": None,
        }
        if b["count_scope"] == "DAILY":
            daily_bins.append(b["bin_code"])
            if b["bin_code"] in occupied_bin_codes:
                occupied_daily_bins.append(b["bin_code"])
        bin_rows.append(row)

    pool = occupied_daily_bins if len(occupied_daily_bins) >= 3 else daily_bins
    sl90_bins = set(pool[:2])
    recent_matched_bin = pool[2] if len(pool) > 2 else pool[0]
    for row in bin_rows:
        if row["bin_code"] == recent_matched_bin:
            row["last_counted_at"] = f"{_shift_date(run_date, -2)}T09:30:00"

    receipts["bin_master"] = store.append_snapshot(
        "bin_master",
        bin_rows,
        source_name="synthetic_plant_snapshot.json",
        captured_at=captured_d_0630,
        file_sha256=file_digest,
    )

    # 5. Boxes: append D-1 05:30 snapshot first (for stale replay), then fresh D 06:30 snapshot
    base_date = date.fromisoformat(base["snapshot_date"])
    target_date = date.fromisoformat(run_date)
    date_offset = (target_date - base_date).days

    box_rows = []
    for bx in base["boxes"]:
        orig_recv = datetime.fromisoformat(bx["received_at"])
        shifted_recv = (orig_recv + timedelta(days=date_offset)).isoformat()
        sloc = "SL90" if bx["bin_code"] in sl90_bins else bx["sloc"]
        box_rows.append(
            {
                "barcode": bx["barcode"],
                "material_id": bx["material_id"],
                "qty": bx["qty"],
                "received_at": shifted_recv,
                "bin_code": bx["bin_code"],
                "sloc": sloc,
                "status": "IN_STORAGE",
                "dwell_days": None,
            }
        )

    for idx in range(25):
        mat = base["materials"][idx]
        dwell = rng.randint(2, 45)
        box_rows.append(
            {
                "barcode": f"PLT-ISS-{idx + 1:04d}",
                "material_id": mat["material_id"],
                "qty": mat["unit_pack_qty"],
                "received_at": f"{_shift_date(run_date, -dwell)}T07:30:00",
                "bin_code": daily_bins[idx % len(daily_bins)],
                "sloc": "SL10",
                "status": "ISSUED_TO_PRODUCTION",
                "dwell_days": float(dwell),
            }
        )

    stale_box_receipt = store.append_snapshot(
        "wms_box_snapshot",
        box_rows,
        source_name="wms_daily_export_d1",
        captured_at=captured_d1_0530,
        file_sha256=file_digest,
    )
    fresh_box_receipt = store.append_snapshot(
        "wms_box_snapshot",
        box_rows,
        source_name="wms_daily_export",
        captured_at=captured_d_0630,
        file_sha256=file_digest,
    )
    receipts["wms_box_snapshot"] = fresh_box_receipt

    # 6. Historical count task + count result for recent_matched_bin (matched 2 days ago)
    prev_run_id = f"{_shift_date(run_date, -2).replace('-', '')}_DAILY"
    matched_box_count = sum(
        1 for b in box_rows if b["bin_code"] == recent_matched_bin and b["status"] == "IN_STORAGE"
    )
    receipts["count_task"] = store.append_snapshot(
        "count_task",
        [
            {
                "run_id": prev_run_id,
                "bin_code": recent_matched_bin,
                "list_date": _shift_date(run_date, -2),
                "lane": "RISK",
                "rank": 1,
                "risk_score": 78.5,
                "reason": "prior_mismatch_watch",
                "book_boxes": matched_box_count,
            }
        ],
        source_name="prior_count_run",
        captured_at=f"{_shift_date(run_date, -2)}T06:50:00",
    )
    receipts["count_result"] = store.append_snapshot(
        "count_result",
        [
            {
                "run_id": prev_run_id,
                "bin_code": recent_matched_bin,
                "counted_boxes": matched_box_count,
                "diff_boxes": 0,
                "cause_class": "UNCLASSIFIED",
                "field_notes": "matched",
            }
        ],
        source_name="warehouse_team_sheet",
        captured_at=f"{_shift_date(run_date, -2)}T09:30:00",
    )

    # 7. Stock positions: two snapshots 25 min apart (07:10 and 07:35) with paired surplus (SL20) & deficit (SL10)
    pos_materials = [m["material_id"] for m in base["materials"][:10]]
    snap1_rows = []
    snap2_rows = []
    for idx, mat_id in enumerate(pos_materials):
        base_qty = float((idx + 3) * 100)
        if idx == 0:
            # Persistent paired gap: SL20 surplus +200, SL10 deficit -200 -> transfer improves accuracy
            sl10_row = {
                "material_id": mat_id,
                "sloc": "SL10",
                "snapshot_date": run_date,
                "erp_qty": base_qty - 200.0,
                "wms_qty": base_qty,
                "diff_qty": -200.0,
                "consecutive_days_different": 3,
            }
            sl20_row = {
                "material_id": mat_id,
                "sloc": "SL20",
                "snapshot_date": run_date,
                "erp_qty": base_qty + 200.0,
                "wms_qty": base_qty,
                "diff_qty": 200.0,
                "consecutive_days_different": 3,
            }
        elif idx == 1:
            # Persistent paired gap blocked by unposted in-flight receipt
            sl10_row = {
                "material_id": mat_id,
                "sloc": "SL10",
                "snapshot_date": run_date,
                "erp_qty": base_qty - 150.0,
                "wms_qty": base_qty,
                "diff_qty": -150.0,
                "consecutive_days_different": 2,
            }
            sl20_row = {
                "material_id": mat_id,
                "sloc": "SL20",
                "snapshot_date": run_date,
                "erp_qty": base_qty + 150.0,
                "wms_qty": base_qty,
                "diff_qty": 150.0,
                "consecutive_days_different": 2,
            }
        else:
            sl10_row = {
                "material_id": mat_id,
                "sloc": "SL10",
                "snapshot_date": run_date,
                "erp_qty": base_qty,
                "wms_qty": base_qty,
                "diff_qty": 0.0,
                "consecutive_days_different": 0,
            }
            sl20_row = {
                "material_id": mat_id,
                "sloc": "SL20",
                "snapshot_date": run_date,
                "erp_qty": base_qty,
                "wms_qty": base_qty,
                "diff_qty": 0.0,
                "consecutive_days_different": 0,
            }
        snap1_rows.extend([dict(sl10_row), dict(sl20_row)])
        snap2_rows.extend([dict(sl10_row), dict(sl20_row)])

    store.append_snapshot(
        "stock_position_snapshot",
        snap1_rows,
        source_name="erp_wms_reconcile_t1",
        captured_at=captured_d_0710,
    )
    receipts["stock_position_snapshot"] = store.append_snapshot(
        "stock_position_snapshot",
        snap2_rows,
        source_name="erp_wms_reconcile_t2",
        captured_at=captured_d_0735,
    )

    # 8. Interface errors: one clearable (261), one receipt-linked (101) handed to person
    error_rows = [
        {
            "error_id": f"{run_date}-ERR-001",
            "material_id": pos_materials[0],
            "movement_type": "261",
            "sloc": "SL10",
            "qty": 200.0,
            "receipt_linked": False,
            "status": "OPEN",
        },
        {
            "error_id": f"{run_date}-ERR-002",
            "material_id": pos_materials[2],
            "movement_type": "101",
            "sloc": "SL10",
            "qty": 120.0,
            "receipt_linked": True,
            "status": "OPEN",
        },
    ]
    receipts["interface_error_snapshot"] = store.append_snapshot(
        "interface_error_snapshot",
        error_rows,
        source_name="erp_interface_monitor",
        captured_at=captured_d_0735,
    )

    # 9. Baseline prior-day stock transfer
    receipts["stock_transfer"] = store.append_snapshot(
        "stock_transfer",
        [
            {
                "document_no": "4900000001",
                "material_id": pos_materials[3],
                "from_sloc": "SL20",
                "to_sloc": "SL10",
                "qty": 100.0,
                "status": "POSTED",
                "reason": "baseline_prior_day",
                "resolves_error_id": None,
                "resolves_backlog_line_id": None,
            }
        ],
        source_name="erp_transfer_history",
        captured_at=f"{prev_day}T16:00:00",
    )

    # 10. Suppliers (5 slots per supplier: 08:00, 10:00, 12:00, 14:00, 16:00)
    supplier_rows = [
        {
            "supplier_id": s["supplier_id"],
            "trailer_cap_carts": 24,
            "truck_slots": ",".join(TRUCK_SLOTS),
        }
        for s in base["suppliers"]
    ]
    receipts["supplier_master"] = store.append_snapshot(
        "supplier_master",
        supplier_rows,
        source_name="supplier_master_config",
        captured_at=captured_d_0630,
    )

    # Helper to pick genuine materials owned by each supplier
    def sup_mats(sup_id: str, count: int) -> list[str]:
        rows = sorted(materials_by_supplier.get(sup_id, ()), key=lambda m: str(m["material_id"]))
        return [str(m["material_id"]) for m in rows[:count]]

    sup1_id, sup2_id, sup3_id = SIM_SUPPLIERS
    sup1_m = sup_mats(sup1_id, 6)
    sup2_m = sup_mats(sup2_id, 6)
    sup3_m = sup_mats(sup3_id, 6)

    # 11. Truck runs for D-1 (yesterday, CLOSED) and D (today, PLANNED) across 3 suppliers × 5 slots
    truck_run_rows: list[dict[str, Any]] = []
    for d_str, status in ((prev_day, "CLOSED"), (run_date, "PLANNED")):
        for sup_id in SIM_SUPPLIERS:
            for slot in TRUCK_SLOTS:
                truck_run_rows.append(
                    {
                        "truck_run_id": f"{sup_id}-{d_str}-{slot.replace(':', '')}",
                        "supplier_id": sup_id,
                        "run_date": d_str,
                        "slot": slot,
                        "planned_carts": None,
                        "status": status,
                    }
                )
    receipts["truck_run"] = store.append_snapshot(
        "truck_run",
        truck_run_rows,
        source_name="truck_schedule",
        captured_at=captured_d_0630,
    )

    # 12. D-1 order lines (sent yesterday) + D-1 goods receipts for realistic 05:00 delivery check
    d1_orders = [
        # sup1_id D-1 (mostly DELIVERED, 1 SHORT, 1 OVER)
        {"order_no": f"DO-{prev_day}-101", "material_id": sup1_m[0], "supplier_id": sup1_id, "due_window": f"{prev_day} 08:00", "ordered_boxes": 4, "truck_run_id": f"{sup1_id}-{prev_day}-0800"},
        {"order_no": f"DO-{prev_day}-102", "material_id": sup1_m[1], "supplier_id": sup1_id, "due_window": f"{prev_day} 10:00", "ordered_boxes": 4, "truck_run_id": f"{sup1_id}-{prev_day}-1000"},
        {"order_no": f"DO-{prev_day}-103", "material_id": sup1_m[2], "supplier_id": sup1_id, "due_window": f"{prev_day} 12:00", "ordered_boxes": 5, "truck_run_id": f"{sup1_id}-{prev_day}-1200"},
        {"order_no": f"DO-{prev_day}-104", "material_id": sup1_m[3], "supplier_id": sup1_id, "due_window": f"{prev_day} 14:00", "ordered_boxes": 3, "truck_run_id": f"{sup1_id}-{prev_day}-1400"},
        {"order_no": f"DO-{prev_day}-105", "material_id": sup1_m[4], "supplier_id": sup1_id, "due_window": f"{prev_day} 16:00", "ordered_boxes": 4, "truck_run_id": f"{sup1_id}-{prev_day}-1600"},
        # sup2_id D-1 (DELIVERED + 1 NOT_DELIVERED)
        {"order_no": f"DO-{prev_day}-201", "material_id": sup2_m[0], "supplier_id": sup2_id, "due_window": f"{prev_day} 08:00", "ordered_boxes": 3, "truck_run_id": f"{sup2_id}-{prev_day}-0800"},
        {"order_no": f"DO-{prev_day}-202", "material_id": sup2_m[1], "supplier_id": sup2_id, "due_window": f"{prev_day} 10:00", "ordered_boxes": 3, "truck_run_id": f"{sup2_id}-{prev_day}-1000"},
        {"order_no": f"DO-{prev_day}-203", "material_id": sup2_m[2], "supplier_id": sup2_id, "due_window": f"{prev_day} 12:00", "ordered_boxes": 3, "truck_run_id": f"{sup2_id}-{prev_day}-1200"},
        {"order_no": f"DO-{prev_day}-204", "material_id": sup2_m[3], "supplier_id": sup2_id, "due_window": f"{prev_day} 14:00", "ordered_boxes": 3, "truck_run_id": f"{sup2_id}-{prev_day}-1400"},
        # sup3_id D-1 (all DELIVERED)
        {"order_no": f"DO-{prev_day}-301", "material_id": sup3_m[0], "supplier_id": sup3_id, "due_window": f"{prev_day} 08:00", "ordered_boxes": 3, "truck_run_id": f"{sup3_id}-{prev_day}-0800"},
        {"order_no": f"DO-{prev_day}-302", "material_id": sup3_m[1], "supplier_id": sup3_id, "due_window": f"{prev_day} 12:00", "ordered_boxes": 3, "truck_run_id": f"{sup3_id}-{prev_day}-1200"},
    ]

    # Today D order lines (3 suppliers × 5 slots; sup1 overloaded, sup2 balanced, sup3 duplicate line)
    d_orders = [
        # sup1_id: total 68 carts, avg=13.6, limit=min(24, 1.7*13.6)=23 -> 12:00 has 30 carts (>23) -> leveled!
        {"order_no": f"DO-{run_date}-101", "material_id": sup1_m[0], "supplier_id": sup1_id, "due_window": f"{run_date} 08:00", "ordered_boxes": 8, "truck_run_id": f"{sup1_id}-{run_date}-0800"},
        {"order_no": f"DO-{run_date}-102", "material_id": sup1_m[1], "supplier_id": sup1_id, "due_window": f"{run_date} 10:00", "ordered_boxes": 10, "truck_run_id": f"{sup1_id}-{run_date}-1000"},
        {"order_no": f"DO-{run_date}-103", "material_id": sup1_m[2], "supplier_id": sup1_id, "due_window": f"{run_date} 12:00", "ordered_boxes": 16, "truck_run_id": f"{sup1_id}-{run_date}-1200"},
        {"order_no": f"DO-{run_date}-104", "material_id": sup1_m[3], "supplier_id": sup1_id, "due_window": f"{run_date} 12:00", "ordered_boxes": 14, "truck_run_id": f"{sup1_id}-{run_date}-1200"},
        {"order_no": f"DO-{run_date}-105", "material_id": sup1_m[4], "supplier_id": sup1_id, "due_window": f"{run_date} 14:00", "ordered_boxes": 10, "truck_run_id": f"{sup1_id}-{run_date}-1400"},
        {"order_no": f"DO-{run_date}-106", "material_id": sup1_m[5], "supplier_id": sup1_id, "due_window": f"{run_date} 16:00", "ordered_boxes": 10, "truck_run_id": f"{sup1_id}-{run_date}-1600"},
        # sup2_id: balanced 10 carts per slot (avg=10, limit=17) -> untouched!
        {"order_no": f"DO-{run_date}-201", "material_id": sup2_m[0], "supplier_id": sup2_id, "due_window": f"{run_date} 08:00", "ordered_boxes": 10, "truck_run_id": f"{sup2_id}-{run_date}-0800"},
        {"order_no": f"DO-{run_date}-202", "material_id": sup2_m[1], "supplier_id": sup2_id, "due_window": f"{run_date} 10:00", "ordered_boxes": 10, "truck_run_id": f"{sup2_id}-{run_date}-1000"},
        {"order_no": f"DO-{run_date}-203", "material_id": sup2_m[2], "supplier_id": sup2_id, "due_window": f"{run_date} 12:00", "ordered_boxes": 10, "truck_run_id": f"{sup2_id}-{run_date}-1200"},
        {"order_no": f"DO-{run_date}-204", "material_id": sup2_m[3], "supplier_id": sup2_id, "due_window": f"{run_date} 14:00", "ordered_boxes": 10, "truck_run_id": f"{sup2_id}-{run_date}-1400"},
        {"order_no": f"DO-{run_date}-205", "material_id": sup2_m[4], "supplier_id": sup2_id, "due_window": f"{run_date} 16:00", "ordered_boxes": 10, "truck_run_id": f"{sup2_id}-{run_date}-1600"},
        # sup3_id: Scenario 6 — repeated portal order line (DO-date-302, sup3_m[1]) -> SENT_WITH_FLAGS + alert!
        {"order_no": f"DO-{run_date}-301", "material_id": sup3_m[0], "supplier_id": sup3_id, "due_window": f"{run_date} 08:00", "ordered_boxes": 8, "truck_run_id": f"{sup3_id}-{run_date}-0800"},
        {"order_no": f"DO-{run_date}-302", "material_id": sup3_m[1], "supplier_id": sup3_id, "due_window": f"{run_date} 10:00", "ordered_boxes": 6, "truck_run_id": f"{sup3_id}-{run_date}-1000"},
        {"order_no": f"DO-{run_date}-302", "material_id": sup3_m[1], "supplier_id": sup3_id, "due_window": f"{run_date} 12:00", "ordered_boxes": 6, "truck_run_id": f"{sup3_id}-{run_date}-1200"},
        {"order_no": f"DO-{run_date}-303", "material_id": sup3_m[2], "supplier_id": sup3_id, "due_window": f"{run_date} 14:00", "ordered_boxes": 8, "truck_run_id": f"{sup3_id}-{run_date}-1400"},
        {"order_no": f"DO-{run_date}-304", "material_id": sup3_m[3], "supplier_id": sup3_id, "due_window": f"{run_date} 16:00", "ordered_boxes": 8, "truck_run_id": f"{sup3_id}-{run_date}-1600"},
    ]

    store.append_snapshot(
        "delivery_order_snapshot",
        d1_orders,
        source_name="supplier_portal_orders_d1",
        captured_at=f"{prev_day}T06:40:00",
    )
    receipts["delivery_order_snapshot"] = store.append_snapshot(
        "delivery_order_snapshot",
        d_orders,
        source_name="supplier_portal_orders",
        captured_at=captured_d_0630,
    )

    # 13. Goods receipts: D-1 scans (mostly DELIVERED + 1 SHORT + 1 OVER + 1 NOT_DELIVERED + 1 UNPLANNED_RECEIPT)
    # plus 1 unposted D receipt for pos_materials[1] (triggers nothing_in_flight on post_stock_transfer)
    gr_rows: list[dict[str, Any]] = []
    gr_seq = 1

    def add_gr(mat_id: str, ts: str, boxes_n: int, erp_posted: bool = True) -> None:
        nonlocal gr_seq
        for _ in range(boxes_n):
            gr_rows.append(
                {
                    "barcode": f"PLT-GR-{gr_seq:04d}",
                    "material_id": mat_id,
                    "qty": 100,
                    "received_at": ts,
                    "erp_posted": erp_posted,
                    "truck_run_id": None,
                }
            )
            gr_seq += 1

    # SUP-001 D-1: 101 DELIVERED(4/4), 102 DELIVERED(4/4), 103 SHORT(3/5), 104 OVER(5/3), 105 DELIVERED(4/4)
    add_gr(sup1_m[0], f"{prev_day}T08:15:00", 4)
    add_gr(sup1_m[1], f"{prev_day}T10:20:00", 4)
    add_gr(sup1_m[2], f"{prev_day}T12:18:00", 3)
    add_gr(sup1_m[3], f"{prev_day}T14:12:00", 5)
    add_gr(sup1_m[4], f"{prev_day}T16:10:00", 4)
    # SUP-002 D-1: 201 DELIVERED(3/3), 202 DELIVERED(3/3), 203 NOT_DELIVERED(0/3), 204 DELIVERED(3/3)
    add_gr(sup2_m[0], f"{prev_day}T08:10:00", 3)
    add_gr(sup2_m[1], f"{prev_day}T10:15:00", 3)
    add_gr(sup2_m[3], f"{prev_day}T14:25:00", 3)
    # SUP-003 D-1: 301 DELIVERED(3/3), 302 DELIVERED(3/3) + UNPLANNED_RECEIPT for sup3_m[4] on 16:00 truck
    add_gr(sup3_m[0], f"{prev_day}T08:22:00", 3)
    add_gr(sup3_m[1], f"{prev_day}T12:14:00", 3)
    add_gr(sup3_m[4], f"{prev_day}T16:15:00", 2)

    # Today D: unposted in-flight receipt for pos_materials[1]
    add_gr(pos_materials[1], f"{run_date}T07:50:00", 1, erp_posted=False)

    receipts["goods_receipt"] = store.append_snapshot(
        "goods_receipt",
        gr_rows,
        source_name="dock_wms_receipts",
        captured_at=captured_d_0735,
    )

    # 14. Synthetic pulling plan (starting stock + per-slot demand) for today's truck runs
    starting_stock_boxes: dict[str, float] = {}
    slot_demand_boxes: dict[tuple[str, str], float] = {}
    for ln in d_orders:
        mat = str(ln["material_id"])
        rid = str(ln["truck_run_id"])
        starting_stock_boxes[mat] = 6.0
        # Demand <= starting stock + arrival so baseline & pull-forward stay >= 0
        slot_demand_boxes[(mat, rid)] = float(max(2, int(ln["ordered_boxes"]) - 2))

    return SyntheticPlantContext(
        run_date=run_date,
        prev_date=prev_day,
        receipts=receipts,
        fresh_box_snapshot_id=fresh_box_receipt.snapshot_id,
        stale_box_snapshot_id=stale_box_receipt.snapshot_id,
        material_suppliers=material_suppliers,
        starting_stock_boxes=starting_stock_boxes,
        slot_demand_boxes=slot_demand_boxes,
    )
