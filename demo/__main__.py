"""End-to-end offline demo runner for one complete manufacturing agent job.

Runs the Interface-Error & Stock-Transfer Guarded Pipeline against the tiny
anonymized dataset in `demo/sample_dataset.json`:
  - Validates the YAML ontology and all declared guards.
  - Loads two append-only snapshots (T-30m and T-now) into a local SQLite ledger.
  - Executes `clear_interface_error` and `post_stock_transfer` through every guard:
      * MAT-DEMO-001 -> safe persistent gap, accuracy improves -> DONE
      * MAT-DEMO-002 -> receipt-linked & in-flight receipt     -> HANDED_TO_PERSON
      * MAT-DEMO-003 -> balanced source would degrade accuracy -> HANDED_TO_PERSON
      * MAT-DEMO-004 -> protected movement type (561)          -> HANDED_TO_PERSON
  - Exports `demo_action_log.csv` and `demo_summary.md` into `--out-dir`.

Usage:
    python -m demo
    python -m demo --out-dir out/demo
    python -m demo --full-day --seed 7 --date 2026-11-24
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from actions import clear_interface_error, post_stock_transfer
from actions.action_log import ActionLog
from adapters.erp import FakeERPAdapter
from ledgers.snapshot_store import SnapshotStore
from ontology.registry import load_registry
from simulation.run_day import main as run_full_day_main

DEMO_DATASET_PATH = Path(__file__).resolve().parent / "sample_dataset.json"


def run_demo(
    *,
    dataset_path: Path = DEMO_DATASET_PATH,
    out_dir: Path | str = "out/demo",
) -> dict[str, Any]:
    """Run the offline single-job guarded interface-error & transfer demo."""
    data = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    run_date = str(data["run_date"])
    captured_now = str(data["captured_at"])
    captured_prev = captured_now.replace("08:00:00", "07:30:00")

    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)
    db_path = out_path / "demo_ledger.db"
    if db_path.exists():
        db_path.unlink()

    registry = load_registry(validate=True, check_modules=True, check_guards=True)
    store = SnapshotStore(db_path)
    store.init_db()

    store.append_snapshot("sloc_master", data["storage_locations"], source_name="demo.sloc_master", captured_at=captured_prev)
    store.append_snapshot("material_master", data["materials"], source_name="demo.material_master", captured_at=captured_prev)
    store.append_snapshot(
        "stock_position_snapshot",
        data["stock_positions_t_minus_30m"],
        source_name="demo.erp_wms_position_prev",
        captured_at=captured_prev,
    )
    store.append_snapshot(
        "stock_position_snapshot",
        data["stock_positions_now"],
        source_name="demo.erp_wms_position_now",
        captured_at=captured_now,
    )
    store.append_snapshot("goods_receipt", data["goods_receipts"], source_name="demo.wms_gr", captured_at=captured_now)
    store.append_snapshot("interface_error_snapshot", data["interface_errors"], source_name="demo.erp_errors", captured_at=captured_now)

    action_log = ActionLog(db_path, run_date=run_date)
    erp = FakeERPAdapter()

    clear_results: list[dict[str, Any]] = []
    for err in data["interface_errors"]:
        entry, guards = clear_interface_error.execute(
            registry=registry,
            action_log=action_log,
            erp=erp,
            error=err,
            reason="demo_guarded_interface_error_check",
            started_at=captured_now,
        )
        clear_results.append(
            {
                "error_id": err["error_id"],
                "material_id": err["material_id"],
                "movement_type": err["movement_type"],
                "outcome": entry.outcome,
                "failed_guards": [g.id for g in guards if not g.passed],
            }
        )

    transfer_results: list[dict[str, Any]] = []
    deficit_mats = sorted({p["material_id"] for p in data["stock_positions_now"] if float(p["diff_qty"]) < 0})
    for mat_id in deficit_mats:
        extra_kwargs: dict[str, Any] = {}
        if mat_id == "MAT-DEMO-003":
            # Propose a partial pull from a balanced warehouse SLoc to demonstrate accuracy_not_down blocking
            extra_kwargs = {"from_sloc": "SL02", "to_sloc": "SP31", "qty": 20.0}
        entry, guards, _ = post_stock_transfer.execute(
            registry=registry,
            action_log=action_log,
            store=store,
            erp=erp,
            material_id=mat_id,
            reason="demo_persistent_gap_rebalance",
            started_at=captured_now,
            **extra_kwargs,
        )
        transfer_results.append(
            {
                "material_id": mat_id,
                "outcome": entry.outcome,
                "failed_guards": [g.id for g in guards if not g.passed],
            }
        )

    log_csv = action_log.export_csv(out_path / "demo_action_log.csv")
    summary_md = out_path / "demo_summary.md"
    lines = [
        f"# Offline Agent Demo Summary ({data['plant']} · {run_date})",
        "",
        "Ran **Inventory 01 · Interface-Error & Stock-Transfer Agent** against `demo/sample_dataset.json` (100% offline, fake ERP adapter).",
        "",
        "## 1. Interface Error Clearing (`clear_interface_error`)",
        "",
        "| Error ID | Material | Movement | Outcome | Failed Guards |",
        "| :--- | :--- | :---: | :--- | :--- |",
    ]
    for r in clear_results:
        fg = ", ".join(r["failed_guards"]) if r["failed_guards"] else "—"
        lines.append(f"| `{r['error_id']}` | `{r['material_id']}` | `{r['movement_type']}` | **{r['outcome']}** | {fg} |")

    lines.extend(
        [
            "",
            "## 2. Guarded Stock Transfers (`post_stock_transfer`)",
            "",
            "| Material | Outcome | Failed Guards |",
            "| :--- | :--- | :--- |",
        ]
    )
    for r in transfer_results:
        fg = ", ".join(r["failed_guards"]) if r["failed_guards"] else "—"
        lines.append(f"| `{r['material_id']}` | **{r['outcome']}** | {fg} |")

    lines.extend(
        [
            "",
            f"- ERP errors cleared: **{len(erp.cleared_errors)}**",
            f"- ERP transfers posted: **{len(erp.posted_transfers)}**",
            f"- Full audit trail: `{log_csv.name}`",
        ]
    )
    summary_md.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    return {
        "plant": data["plant"],
        "run_date": run_date,
        "cleared_errors": len(erp.cleared_errors),
        "posted_transfers": len(erp.posted_transfers),
        "clear_results": clear_results,
        "transfer_results": transfer_results,
        "action_log_csv": str(log_csv),
        "summary_md": str(summary_md),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline manufacturing agent demo.")
    parser.add_argument("--out-dir", default="out/demo", help="Output directory for demo artifacts.")
    parser.add_argument("--full-day", action="store_true", help="Run the full multi-agent synthetic day simulation.")
    parser.add_argument("--seed", type=int, default=7, help="Seed for --full-day mode.")
    parser.add_argument("--date", default="2026-11-24", help="Date for --full-day mode.")
    args = parser.parse_args(argv)

    if args.full_day:
        return run_full_day_main(["--seed", str(args.seed), "--date", args.date, "--out-dir", args.out_dir])

    res = run_demo(out_dir=args.out_dir)
    print(f"Offline Demo Complete ({res['plant']} | {res['run_date']})")
    print(f"  clear_interface_error : cleared={res['cleared_errors']}/{len(res['clear_results'])}")
    for r in res["clear_results"]:
        fg = f" (blocked by: {', '.join(r['failed_guards'])})" if r["failed_guards"] else ""
        print(f"    - {r['error_id']} {r['material_id']} mvt={r['movement_type']} -> {r['outcome']}{fg}")
    print(f"  post_stock_transfer   : posted={res['posted_transfers']}/{len(res['transfer_results'])}")
    for r in res["transfer_results"]:
        fg = f" (blocked by: {', '.join(r['failed_guards'])})" if r["failed_guards"] else ""
        print(f"    - {r['material_id']} -> {r['outcome']}{fg}")
    print(f"  Artifacts: {res['summary_md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
