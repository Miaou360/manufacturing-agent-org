"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - CLI Entrypoint & Replay Runner (#351)
=================================================================================
Modes:
  1. Intraday single poll (reuses `WMSChunkCrawler.download_gr_snapshot(..., sso_only=True)`):
       python run_delivery_check.py --send-test-mail
  2. Offline single-timestamp check against an existing WMS GR Excel export (zero browser, zero mail):
       python run_delivery_check.py --date 2026-11-13 --gr-file <path.xlsx> --as-of "2026-11-13 15:00:00" --dry-run
  3. Full-day chronological 10-minute state-machine replay + worked examples per supplier (`371 §3.8`):
       python run_delivery_check.py --date 2026-11-13 --gr-file <path.xlsx> --replay-day --worked-examples --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parent
DEPT_DIR = BASE_DIR.parent
MCP_ROOT = DEPT_DIR.parent
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"
INV_DEPT_DIR = MCP_ROOT / "01_DEPT_Inventory"
STOCK_ACC_DIR = INV_DEPT_DIR / "00_JOB_StockAccuracy"

for _p in (str(BASE_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DEPT_DIR), str(STOCK_ACC_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delivery_db_manager import (  # noqa: E402
    DEFAULT_DB_PATH,
    fetch_barcodes,
    fetch_horizon_parts,
    fetch_truck_plan_lines,
    fetch_truck_runs,
    fetch_verdicts,
    get_connection,
    init_database,
)
from delivery_notifier import dispatch_poll_notifications, load_delivery_config  # noqa: E402
from delivery_projection_engine import (  # noqa: E402
    CLOSED_TRUCK_STATES,
    OPEN_TRUCK_STATES,
    evaluate_poll_cycle,
    ingest_and_attribute_barcodes,
    parse_gr_excel_barcodes,
)
from delivery_report_generator import write_delivery_check_workbook  # noqa: E402
from plan_loader import compute_srm_plan_readonly, sync_plan_to_ledger  # noqa: E402
from srm_env import load_env  # noqa: E402
from srm_parsers import N_SLOTS  # noqa: E402
from srm_projection import slot_start  # noqa: E402


def _is_git_worktree(path: Path) -> bool:
    return "codes_worktrees" in str(path.resolve()).lower()


def has_due_open_trucks(trucks: List[Dict[str, Any]], as_of_ts: dt.datetime) -> bool:
    as_of_str = as_of_ts.strftime("%Y-%m-%d %H:%M:%S")
    for t in trucks:
        if t["status"] not in OPEN_TRUCK_STATES:
            continue
        due = t.get("next_poll_due_ts") or t.get("first_poll_due_ts")
        if due and as_of_str >= due:
            return True
    return False


def build_worked_examples(
    run_date: dt.date,
    trucks: List[Dict[str, Any]],
    plan_lines: List[Dict[str, Any]],
    barcodes: List[Dict[str, Any]],
    verdicts: List[Dict[str, Any]],
    projections: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Produces one complete worked example per supplier (`371 §3.8`):
      plan line -> barcodes matched -> truck closed at -> GR-PO -> hybrid stock per slot -> column A
      (+ `Runout (received only)` side by side).
    """
    suppliers = sorted({t["supplier_key"] for t in trucks if t["status"] != "NO_PLANNED_LOAD"})
    bcs_by_tp: Dict[tuple, List[Dict[str, Any]]] = {}
    for b in barcodes:
        if b.get("assigned_truck_id") and not b.get("is_unplanned"):
            bcs_by_tp.setdefault((b["assigned_truck_id"], b["part_key"]), []).append(b)

    verdict_by_tp = {(v["truck_id"], v["part_key"]): v for v in verdicts}
    examples: List[Dict[str, Any]] = []

    for sup in suppliers:
        sup_lines = [ln for ln in plan_lines if ln["supplier_key"] == sup]
        if not sup_lines:
            continue
        # Prefer a line with matched barcodes or interesting variance so the trace is maximally informative
        def _score_line(ln: Dict[str, Any]) -> tuple:
            v = verdict_by_tp.get((ln["truck_id"], ln["part_key"]), {})
            has_bc = 1 if bcs_by_tp.get((ln["truck_id"], ln["part_key"])) else 0
            has_var = 1 if abs(float(v.get("diff_pcs", 0.0))) > 1e-9 else 0
            return (-has_bc, -has_var, ln["slot_idx"], ln["part_no"])

        chosen_ln = sorted(sup_lines, key=_score_line)[0]
        tid = chosen_ln["truck_id"]
        pk = chosen_ln["part_key"]
        truck = next(t for t in trucks if t["truck_id"] == tid)
        v = verdict_by_tp.get((tid, pk), {})
        matched_bcs = bcs_by_tp.get((tid, pk), [])
        proj = projections.get(pk, {})

        day0_slots = []
        for s_i in range(N_SLOTS):
            day0_slots.append(
                {
                    "slot_idx": s_i,
                    "local_label": proj["slot_labels_36"][s_i] if proj else "",
                    "plan_load": proj["load_36"][s_i] if proj else 0.0,
                    "actual_gr": proj["actual_gr_36"][s_i] if proj else 0.0,
                    "hybrid_supply": proj["hybrid_supply_36"][s_i] if proj else 0.0,
                    "req": proj["req_36"][s_i] if proj else 0.0,
                    "hybrid_end_stock": proj["hybrid_stock_36"][s_i] if proj else 0.0,
                    "received_only_end_stock": proj["received_only_stock_36"][s_i] if proj else 0.0,
                }
            )

        examples.append(
            {
                "supplier_key": sup,
                "vendor_display": truck["vendor_display"],
                "truck_id": tid,
                "truck_label": truck["truck_label"],
                "departure_ts": truck["departure_ts"],
                "truck_status": truck["status"],
                "first_receipt_ts": truck["first_receipt_ts"],
                "last_receipt_ts": truck["last_receipt_ts"],
                "truck_closed_ts": truck["closed_ts"],
                "part_no": chosen_ln["part_no"],
                "part_name": chosen_ln["part_name"],
                "box_qty": chosen_ln["box_qty"],
                "start_sap": proj.get("start_sap", 0.0),
                "planned_boxes": chosen_ln["planned_boxes"],
                "planned_pcs": chosen_ln["planned_pcs"],
                "received_boxes": v.get("received_boxes", 0),
                "received_pcs": v.get("received_pcs", 0.0),
                "gr_minus_po_pcs": v.get("diff_pcs", 0.0),
                "verdict": v.get("verdict", ""),
                "matched_barcodes": [
                    {"barcode": b["barcode"], "gr_time": b["gr_time"], "gr_qty": b["gr_qty"]}
                    for b in matched_bcs[:8]
                ],
                "matched_barcodes_total": len(matched_bcs),
                "column_a_shortage_receipt_basis": v.get("shortage_receipt_basis", ""),
                "line_shortage_alarm": bool(v.get("line_shortage_alarm")),
                "buffer_low_15pct": bool(v.get("buffer_low_15pct")),
                "runout_received_only": v.get("runout_received_only", ""),
                "day0_slot_trace": day0_slots,
            }
        )
    return examples


def run_delivery_check(
    run_date: dt.date,
    *,
    as_of_ts: Optional[dt.datetime] = None,
    gr_file: Optional[Path] = None,
    replay_day: bool = False,
    dry_run: bool = False,
    send_test_mail: bool = False,
    db_path: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    inbox_dir: Optional[Path] = None,
    zm212_path: Optional[Path] = None,
    config_path: Optional[Path] = None,
    allow_worktree_live: bool = False,
    headless_browser: bool = True,
) -> Dict[str, Any]:
    cfg = load_delivery_config(config_path)
    now_ts = as_of_ts or dt.datetime.now().replace(microsecond=0)
    if not bool(cfg.get("enabled", True)):
        return {
            "status": "DISABLED",
            "work_date": run_date.isoformat(),
            "as_of_ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "detail": "Disabled via config/delivery_check.json (enabled=false).",
        }

    if dry_run:
        target_root = BASE_DIR / "scratch" / f"dryrun_{run_date:%Y%m%d}"
        db_file = Path(db_path) if db_path else (target_root / "delivery_check_dryrun.db")
        report_dir = Path(out_dir) if out_dir else (target_root / "out")
        snap_dir = target_root / "snapshots"
        if replay_day and db_path is None:
            for sfx in ("", "-wal", "-shm"):
                p_rm = Path(str(db_file) + sfx)
                if p_rm.exists():
                    p_rm.unlink()
    else:
        db_file = Path(db_path) if db_path else DEFAULT_DB_PATH
        report_dir = Path(out_dir) if out_dir else (BASE_DIR / "out")
        snap_dir = BASE_DIR / "snapshots"

    init_database(db_file)
    conn = get_connection(db_file)

    try:
        plan_bundle = compute_srm_plan_readonly(
            run_date,
            inbox_dir=inbox_dir,
            zm212_path=zm212_path,
            include_d1=(not replay_day),
        )
        if plan_bundle["status"] != "OK":
            return {"status": plan_bundle["status"], "detail": plan_bundle}

        sync_info = sync_plan_to_ledger(conn, run_date, plan_bundle, cfg, now_ts)
        trucks = fetch_truck_runs(conn, run_date.isoformat())

        # Full-day chronological 10-minute replay mode (offline file required)
        if replay_day:
            if not gr_file or not Path(gr_file).exists():
                return {"status": "MISSING_GR_FILE_FOR_REPLAY", "gr_file": str(gr_file)}
            t_cursor = slot_start(run_date, 0, 0) + dt.timedelta(minutes=30)
            t_end = slot_start(run_date, 1, 0) - dt.timedelta(minutes=1)  # 05:59 next day (end of workday)
            closed_events: List[Dict[str, Any]] = []
            early_events: List[Dict[str, Any]] = []
            eval_res: Dict[str, Any] = {}

            # Parse file once up front and slice by gr_dt <= t_cursor at each 10m tick (+ final 05:59 tick)
            all_day_barcodes = parse_gr_excel_barcodes(Path(gr_file), run_date)
            replay_ticks: List[dt.datetime] = []
            while t_cursor < t_end:
                replay_ticks.append(t_cursor)
                t_cursor += dt.timedelta(minutes=10)
            replay_ticks.append(t_end)

            for tick_ts in replay_ticks:
                cur_trucks = fetch_truck_runs(conn, run_date.isoformat())
                if has_due_open_trucks(cur_trucks, tick_ts):
                    visible_bcs = [b for b in all_day_barcodes if b["gr_dt"] <= tick_ts]
                    attr = ingest_and_attribute_barcodes(conn, run_date, visible_bcs, cfg, tick_ts)
                    eval_res = evaluate_poll_cycle(
                        conn,
                        run_date,
                        cfg,
                        tick_ts,
                        gr_available=True,
                        new_planned_by_truck=attr["new_planned_by_truck"],
                    )
                    for tid in eval_res["newly_closed_trucks"]:
                        closed_events.append({"truck_id": tid, "closed_at": eval_res["as_of_ts"]})
                    for ew in eval_res["early_warnings"]:
                        tid = ew["truck"]["truck_id"]
                        if not any(x["truck_id"] == tid for x in early_events):
                            early_events.append(
                                {
                                    "truck_id": tid,
                                    "warned_at": eval_res["as_of_ts"],
                                    "short_parts": ew["short_parts"],
                                }
                            )

            # Finalize PM D+1 merge if present so 3-day horizon reflects afternoon update
            pm_bundle = compute_srm_plan_readonly(
                run_date, inbox_dir=inbox_dir, zm212_path=zm212_path, include_d1=True
            )
            if pm_bundle["status"] == "OK":
                sync_plan_to_ledger(conn, run_date, pm_bundle, cfg, t_end)
                eval_res = evaluate_poll_cycle(conn, run_date, cfg, t_end, gr_available=True)

            final_trucks = fetch_truck_runs(conn, run_date.isoformat())
            final_verdicts = fetch_verdicts(conn, run_date.isoformat())
            final_lines = fetch_truck_plan_lines(conn, run_date.isoformat())
            final_barcodes = fetch_barcodes(conn, run_date.isoformat())
            day_summary = eval_res.get("day_part_summary", [])
            wb_stamped, wb_latest = write_delivery_check_workbook(
                report_dir,
                run_date,
                t_end,
                eval_res["projections"],
                final_trucks,
                final_verdicts,
                gr_available=True,
                day_part_summary=day_summary,
            )
            examples = build_worked_examples(
                run_date,
                final_trucks,
                final_lines,
                final_barcodes,
                final_verdicts,
                eval_res["projections"],
            )
            return {
                "status": "OK",
                "mode": "REPLAY_DAY",
                "work_date": run_date.isoformat(),
                "plan_sync": sync_info,
                "barcodes_ingested": len(final_barcodes),
                "closed_events": closed_events,
                "early_warnings": early_events,
                "workbook": str(wb_stamped),
                "workbook_latest": str(wb_latest),
                "worked_examples": examples,
                "verdict_counts": _count_by_key(final_verdicts, "verdict"),
                "day_verdict_counts": _count_by_key(day_summary, "day_verdict"),
                "day_part_summary_count": len(day_summary),
                "alarm_count": sum(1 for v in final_verdicts if v.get("line_shortage_alarm")),
                "cleared_alarm_count": sum(
                    1
                    for v in final_verdicts
                    if v.get("initial_alarm_at_close") and not v.get("line_shortage_alarm")
                ),
                "buffer_low_count": sum(1 for v in final_verdicts if v.get("buffer_low_15pct")),
            }

        # Single-poll mode (offline `--gr-file` OR live intraday browser snapshot)
        gr_available = False
        sso_expired_detail: Optional[str] = None
        crawl_status = "OFFLINE_FILE"

        if gr_file is not None:
            gr_path = Path(gr_file)
            if gr_path.exists():
                parsed_bcs = parse_gr_excel_barcodes(gr_path, run_date, as_of_ts=now_ts)
                attr = ingest_and_attribute_barcodes(conn, run_date, parsed_bcs, cfg, now_ts)
                gr_available = True
            else:
                crawl_status = "MISSING_GR_FILE"
                attr = {"new_barcodes_inserted": 0, "new_planned_by_truck": {}}
        else:
            if not has_due_open_trucks(trucks, now_ts):
                return {
                    "status": "IDLE_NO_DUE_TRUCKS",
                    "work_date": run_date.isoformat(),
                    "as_of_ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "trucks_total": len(trucks),
                }
            if _is_git_worktree(BASE_DIR) and not allow_worktree_live:
                return {
                    "status": "BLOCKED_WORKTREE_LIVE_BROWSER",
                    "detail": "Live Selenium WMS crawl blocked inside git worktree; pass --gr-file for offline test.",
                }
            if str(STOCK_ACC_DIR) in sys.path:
                sys.path.remove(str(STOCK_ACC_DIR))
            sys.path.insert(0, str(STOCK_ACC_DIR))
            sys.modules.pop("db_manager", None)
            from historical_backfill_crawler import WMSChunkCrawler

            block_dir = (BASE_DIR / "scratch" / f"dryrun_{run_date:%Y%m%d}") if dry_run else (BASE_DIR / "logs")
            block_file = block_dir / "wms_login_blocked.json"
            allow_env_login = bool(cfg.get("allow_wms_env_login", True))
            crawler = WMSChunkCrawler(headless=headless_browser)
            dl = crawler.download_gr_snapshot(
                run_date,
                run_date,
                snap_dir,
                sso_only=(not allow_env_login),
                login_block_file=block_file,
                lock_timeout_s=float(cfg.get("browser_lock_timeout_s", 15.0)),
                owner="02_JOB_Delivery_Check",
            )
            crawl_status = dl["status"]
            if crawl_status == "OK" and dl.get("file"):
                parsed_bcs = parse_gr_excel_barcodes(Path(dl["file"]), run_date, as_of_ts=now_ts)
                attr = ingest_and_attribute_barcodes(conn, run_date, parsed_bcs, cfg, now_ts)
                gr_available = True
            elif crawl_status in ("WMS_SSO_EXPIRED", "WMS_LOGIN_FAILED", "WMS_LOGIN_BLOCKED"):
                sso_expired_detail = dl.get("detail") or f"WMS authentication paused ({crawl_status})"
                attr = {"new_barcodes_inserted": 0, "new_planned_by_truck": {}}
            elif crawl_status == "POLL_SKIPPED_LOCK_BUSY":
                return {
                    "status": "POLL_SKIPPED_LOCK_BUSY",
                    "work_date": run_date.isoformat(),
                    "as_of_ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    "detail": dl.get("detail"),
                }
            else:
                attr = {"new_barcodes_inserted": 0, "new_planned_by_truck": {}}

        eval_res = evaluate_poll_cycle(
            conn,
            run_date,
            cfg,
            now_ts,
            gr_available=gr_available,
            new_planned_by_truck=attr["new_planned_by_truck"],
        )
        final_trucks = fetch_truck_runs(conn, run_date.isoformat())
        final_verdicts = fetch_verdicts(conn, run_date.isoformat())
        final_lines = fetch_truck_plan_lines(conn, run_date.isoformat())
        final_barcodes = fetch_barcodes(conn, run_date.isoformat())

        day_summary = eval_res.get("day_part_summary", [])
        wb_stamped, wb_latest = write_delivery_check_workbook(
            report_dir,
            run_date,
            now_ts,
            eval_res["projections"],
            final_trucks,
            final_verdicts,
            gr_available=gr_available,
            day_part_summary=day_summary,
        )

        final_horizon = fetch_horizon_parts(conn, run_date.isoformat())
        notices = dispatch_poll_notifications(
            conn,
            work_date=run_date.isoformat(),
            as_of_ts=now_ts,
            cfg=cfg,
            trucks=final_trucks,
            verdicts=final_verdicts,
            newly_closed_truck_ids=eval_res["newly_closed_trucks"],
            early_warnings=eval_res["early_warnings"],
            workbook_path=wb_stamped,
            sso_expired_detail=sso_expired_detail,
            send_enabled=(bool(send_test_mail) and not dry_run),
            horizon_parts=final_horizon,
            plan_lines=final_lines,
            barcodes=final_barcodes,
            projections=eval_res["projections"],
            trucks_cfg=plan_bundle["trucks_cfg"],
            suppliers_rows=plan_bundle["suppliers"],
            supplier_status_out_dir=report_dir / f"suppliers_{run_date:%Y%m%d}",
        )

        return {
            "status": "OK" if gr_available else crawl_status,
            "work_date": run_date.isoformat(),
            "as_of_ts": now_ts.strftime("%Y-%m-%d %H:%M:%S"),
            "crawl_status": crawl_status,
            "gr_available": gr_available,
            "new_barcodes_inserted": attr["new_barcodes_inserted"],
            "newly_closed_trucks": eval_res["newly_closed_trucks"],
            "early_warnings_count": len(eval_res["early_warnings"]),
            "workbook": str(wb_stamped),
            "workbook_latest": str(wb_latest),
            "notices": notices,
            "verdict_counts": _count_by_key(final_verdicts, "verdict"),
            "day_verdict_counts": _count_by_key(day_summary, "day_verdict"),
            "day_part_summary_count": len(day_summary),
            "alarm_count": sum(1 for v in final_verdicts if v.get("line_shortage_alarm")),
            "cleared_alarm_count": sum(
                1
                for v in final_verdicts
                if v.get("initial_alarm_at_close") and not v.get("line_shortage_alarm")
            ),
            "buffer_low_count": sum(1 for v in final_verdicts if v.get("buffer_low_15pct")),
            "worked_examples": build_worked_examples(
                run_date, final_trucks, final_lines, final_barcodes, final_verdicts, eval_res["projections"]
            ),
        }
    finally:
        conn.close()


def _count_by_key(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        k = str(r.get(key, ""))
        out[k] = out.get(k, 0) + 1
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="02_DEPT_Logistics / 02_JOB_Delivery_Check (#351)")
    ap.add_argument("--date", help="Work date YYYY-MM-DD (default: current 06:00-06:00 plant workday)")
    ap.add_argument("--as-of", help="Evaluation timestamp 'YYYY-MM-DD HH:MM:SS' (default: now)")
    ap.add_argument("--gr-file", help="Offline WMS GR List .xlsx/.csv path (skips live browser crawl)")
    ap.add_argument("--inbox", help="Optional override for 00_JOB inbox/<YYYYMMDD>")
    ap.add_argument("--zm212", help="Optional override for morning ZM212 export")
    ap.add_argument("--replay-day", action="store_true", help="Chronologically replay 10-min ticks across the workday")
    ap.add_argument("--worked-examples", action="store_true", help="Include full worked examples per supplier in JSON output")
    ap.add_argument("--dry-run", action="store_true", help="Write DB and Excel outputs to scratch/ and send zero emails")
    ap.add_argument("--send-test-mail", action="store_true", help="Send due [TEST] notices to TEST_EMAIL_RECIPIENT only")
    ap.add_argument("--visible-browser", action="store_true", help="Run Chrome non-headless during live WMS snapshot")
    a = ap.parse_args(argv)

    load_env()
    now_dt = dt.datetime.strptime(a.as_of, "%Y-%m-%d %H:%M:%S") if a.as_of else dt.datetime.now().replace(microsecond=0)
    if a.date:
        work_dt = dt.date.fromisoformat(a.date)
    else:
        # Plant workday boundary 06:00 -> 06:00 (T3)
        work_dt = (now_dt - dt.timedelta(hours=6)).date()

    res = run_delivery_check(
        work_dt,
        as_of_ts=now_dt,
        gr_file=Path(a.gr_file) if a.gr_file else None,
        replay_day=a.replay_day,
        dry_run=a.dry_run,
        send_test_mail=a.send_test_mail,
        inbox_dir=Path(a.inbox) if a.inbox else None,
        zm212_path=Path(a.zm212) if a.zm212 else None,
        headless_browser=not a.visible_browser,
    )
    if not a.worked_examples and "worked_examples" in res and not a.replay_day:
        res = {k: v for k, v in res.items() if k != "worked_examples"}

    logs_dir = BASE_DIR / ("scratch" if a.dry_run else "logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    res_with_ts = {"executed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **res}
    payload_txt = json.dumps(res_with_ts, ensure_ascii=False, indent=2)
    log_path = logs_dir / f"delivery_check_{dt.datetime.now():%Y%m%d_%H%M%S}{'_DRYRUN' if a.dry_run else ''}.json"
    log_path.write_text(payload_txt, encoding="utf-8")
    if not a.dry_run:
        latest_path = logs_dir / "delivery_check_latest.json"
        tmp_latest = logs_dir / "delivery_check_latest.json.tmp"
        tmp_latest.write_text(payload_txt, encoding="utf-8")
        os.replace(tmp_latest, latest_path)
    print(payload_txt)
    non_fail_statuses = {
        "OK",
        "IDLE_NO_DUE_TRUCKS",
        "POLL_SKIPPED_LOCK_BUSY",
        "DISABLED",
        "PLAN_NOT_AVAILABLE_YET",
        "MISSING_SRM_INPUT",
        "MISSING_ZM212",
    }
    return 0 if res.get("status") in non_fail_statuses else 1


if __name__ == "__main__":
    sys.exit(main())
