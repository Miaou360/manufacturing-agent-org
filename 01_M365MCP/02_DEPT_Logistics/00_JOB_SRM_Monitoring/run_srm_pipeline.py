"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - scheduled pipeline (AM 06:40, PM 13:40) & manual PM refresh (#330/#392/#394)
========================================================================================================================
AM (06:40): SRM download with delivery date today -> inbox/<date>/. Retries every 5 min until 07:00; waits for ZM212.
PM (13:40): SRM download with delivery date tomorrow -> inbox/<date>/D+1/. Retries every 5 min until 14:00; mails.
PM Refresh (`--slot PM --refresh` / `run_srm_pm_refresh.bat`):
  Re-downloads fresh D+1 SRM exports after a late manual SRM order change, recomputes the PM truck plans, compares each
  supplier's Truck plan against the last PM file actually sent to that supplier today, highlights changed cells in yellow,
  and sends `UPDATED` mails ONLY to suppliers whose truck plan changed (unchanged suppliers get nothing).

  python run_srm_pipeline.py --slot AM
  python run_srm_pipeline.py --slot PM --refresh                           # manual PM refresh (live changed-only)
  python run_srm_pipeline.py --slot PM --refresh --dry-run                 # no mail, scratch output + diff
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
LOGS = BASE_DIR / "logs"
LOCK = LOGS / "srm_pipeline.lock"
LOCK_STALE_S = 90 * 60

from run_srm_monitoring import INBOX_DIR, find_inputs, run_engine, zm212_file  # noqa: E402
import srm_mailer  # noqa: E402
from srm_collector import check_export  # noqa: E402
from srm_env import load_env  # noqa: E402
from srm_pm_refresh import (  # noqa: E402
    check_pm_refresh_guards,
    diff_truck_plan_parsed,
    find_last_sent_pm_plan,
    format_pm_refresh_console_summary,
    parse_truck_plan_workbook,
    record_last_sent_pm_plan,
)
from srm_supplier_files import supplier_file_prefix  # noqa: E402

KINDS = ("order", "require", "planreq")
SLOTS = {"AM": {"retry_until": "07:00", "zm212_until": "07:20"},
         "PM": {"retry_until": "14:00", "zm212_until": None}}
RETRY_EVERY_MIN = 5.0
NO_RETRY = {"CONFIG", "LOGIN_FAILED", "LOGIN_BLOCKED"}


def default_slot(now: dt.datetime) -> str:
    return "AM" if now.hour < 12 else "PM"


def at(day: dt.date, hhmm: str) -> dt.datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return dt.datetime.combine(day, dt.time(h, m))


def targets(slot: str, run_date: dt.date, inbox_root: Path) -> List[Tuple[dt.date, Path]]:
    today = inbox_root / f"{run_date:%Y%m%d}"
    return [(run_date, today)] + ([(run_date + dt.timedelta(days=1), today / "D+1")] if slot == "PM" else [])


def inbox_problems(folder: Path, delivery: dt.date) -> Dict[str, str]:
    found = find_inputs(folder)
    out: Dict[str, str] = {}
    for k in KINDS:
        if not found[k]:
            out[k] = "missing"
            continue
        c = check_export(k, found[k], delivery)
        if not c["ok"]:
            out[k] = f"{c['status']}: {c['detail']}"
    return out


def download_with_retry(jobs: List[Tuple[dt.date, Path]], collector: Callable[..., Dict[str, Any]],
                        retry_until: dt.datetime, every_min: float, now_fn: Callable[[], dt.datetime],
                        sleep: Callable[[float], None]) -> Dict[str, Any]:
    start, attempts = now_fn(), []

    def problems() -> Dict[str, Dict[str, str]]:
        return {f"{d:%Y-%m-%d}": p for d, f in jobs if (p := inbox_problems(f, d))}

    while True:
        need = [(d, f, sorted(inbox_problems(f, d))) for d, f in jobs]
        need = [x for x in need if x[2]]
        if not need:
            return {"status": "OK", "attempts": attempts, "problems": {}}
        res = collector(need)
        attempts.append({"at": f"{now_fn():%H:%M:%S}", "status": res.get("status"),
                         "detail": str(res.get("detail", ""))[:400],
                         "files": [Path(x).name for x in res.get("files", [])]})
        left = problems()
        if not left:
            return {"status": "OK", "attempts": attempts, "problems": {}}
        next_try = start + dt.timedelta(minutes=every_min * len(attempts))
        if res.get("status") in NO_RETRY or next_try > retry_until + dt.timedelta(seconds=59):
            return {"status": "FAILED", "attempts": attempts, "problems": left}
        wait = (next_try - now_fn()).total_seconds()
        if wait > 0:
            sleep(wait)


def wait_for_file(path: Path, minutes: float, poll_s: float = 30.0, settle_s: float = 5.0,
                  sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> bool:
    end = clock() + minutes * 60
    while True:
        if path.exists():
            size = path.stat().st_size
            sleep(settle_s)
            if size > 0 and path.exists() and path.stat().st_size == size:
                return True
        if clock() >= end:
            return False
        sleep(poll_s)


def _lock(lock_path: Path = LOCK) -> bool:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists() and time.time() - lock_path.stat().st_mtime < LOCK_STALE_S:
        return False
    lock_path.write_text(f"{os.getpid()} {dt.datetime.now():%Y-%m-%d %H:%M:%S}", encoding="utf-8")
    return True


def _unlock(lock_path: Path = LOCK) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


def run_pipeline(
    slot: str,
    run_date: dt.date,
    skip_download: bool = False,
    mail: bool = True,
    dry_run: bool = False,
    resend: bool = False,
    zm212_wait_min: Optional[float] = None,
    retry_until: Optional[str] = None,
    retry_every_min: float = RETRY_EVERY_MIN,
    collector: Optional[Callable[..., Dict[str, Any]]] = None,
    sender: Optional[Callable[..., bool]] = None,
    zm212: Optional[Path] = None,
    inbox_root: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    mail_cfg: Optional[Dict[str, Any]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], dt.datetime] = dt.datetime.now,
    only_supplier: Optional[str] = None,
    updated: bool = False,
    refresh: bool = False,
    logs_dir: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
    suppliers_csv: Optional[Path] = None,
    watchlist_csv: Optional[Path] = None,
    class_csv: Optional[Path] = None,
) -> Dict[str, Any]:
    started = now_fn()
    eff_logs = logs_dir or LOGS
    eff_reports = reports_dir or (BASE_DIR / "reports")
    st: Dict[str, Any] = {
        "slot": slot,
        "run_date": run_date.isoformat(),
        "started": f"{started:%Y-%m-%d %H:%M:%S}",
        "status": "STARTED",
        "notes": [],
        "dry_run": dry_run,
        "refresh_mode": refresh,
    }
    send = mail and not dry_run
    sender = sender or srm_mailer.outlook_send
    cfg = mail_cfg or srm_mailer.load_config()
    eff_inbox_root = inbox_root or INBOX_DIR
    jobs = targets(slot, run_date, eff_inbox_root)
    today_dir = jobs[0][1]
    engine_today_dir = today_dir

    def fail(stage: str, detail: str) -> Dict[str, Any]:
        st.update(status="FAILED", stage=stage, detail=detail)
        if send:
            st["alert_sent"] = srm_mailer.send_alert(stage, detail, st, cfg=cfg, sender=sender)
        return st

    try:
        if refresh:
            guard_err = check_pm_refresh_guards(slot, run_date, started, eff_logs)
            if guard_err:
                return fail(guard_err[0], guard_err[1])

        # 1. SRM files
        if refresh:
            p_today = inbox_problems(today_dir, run_date)
            if p_today:
                return fail("INPUT_FILES", json.dumps({f"{run_date:%Y-%m-%d}": p_today}))
            if skip_download:
                p_d1 = inbox_problems(today_dir / "D+1", run_date + dt.timedelta(days=1))
                if p_d1:
                    return fail("INPUT_FILES", json.dumps({f"{run_date + dt.timedelta(days=1):%Y-%m-%d}": p_d1}))
            else:
                if collector is None:
                    from srm_collector import collect as collector  # noqa: N813
                tomorrow = run_date + dt.timedelta(days=1)
                if dry_run:
                    scratch_base = (out_dir or (BASE_DIR / "scratch")) / f"pm_refresh_dryrun_{started:%Y%m%d_%H%M%S}"
                    engine_today_dir = scratch_base / "inbox" / f"{run_date:%Y%m%d}"
                    engine_today_dir.mkdir(parents=True, exist_ok=True)
                    for mf in today_dir.glob("*"):
                        if mf.is_file():
                            shutil.copy2(mf, engine_today_dir / mf.name)
                    staging_d1 = engine_today_dir / "D+1"
                else:
                    staging_d1 = today_dir / f"D+1_refresh_{started:%H%M%S}"
                staging_d1.mkdir(parents=True, exist_ok=True)
                until = at(run_date, retry_until) if retry_until else (started - dt.timedelta(seconds=1))
                dl = download_with_retry([(tomorrow, staging_d1)], collector, until, retry_every_min, now_fn, sleep)
                st["download"] = dl
                if dl["status"] != "OK":
                    last = dl["attempts"][-1] if dl["attempts"] else {}
                    return fail(
                        "PM_REFRESH_DOWNLOAD",
                        f"{len(dl['attempts'])} try(s); last: {last.get('status')} {last.get('detail', '')}; "
                        f"problems: {json.dumps(dl['problems'])}"[:900],
                    )
                if not dry_run:
                    prod_d1 = today_dir / "D+1"
                    prod_d1.mkdir(parents=True, exist_ok=True)
                    snap_1340 = eff_reports / "sent_snapshots" / f"{run_date:%Y%m%d}_PM_1340" / "D+1_1340"
                    if not snap_1340.exists():
                        snap_1340.mkdir(parents=True, exist_ok=True)
                        for old_f in prod_d1.glob("*"):
                            if old_f.is_file():
                                shutil.copy2(old_f, snap_1340 / old_f.name)
                    prev_arch = today_dir / f"D+1_prev_{started:%H%M%S}"
                    prev_arch.mkdir(parents=True, exist_ok=True)
                    for old_f in prod_d1.glob("*"):
                        if old_f.is_file():
                            shutil.copy2(old_f, prev_arch / old_f.name)
                    for fresh_f in staging_d1.glob("*"):
                        if fresh_f.is_file():
                            dst_f = prod_d1 / fresh_f.name
                            tmp_f = prod_d1 / (fresh_f.name + ".tmp")
                            shutil.copy2(fresh_f, tmp_f)
                            os.replace(tmp_f, dst_f)
                            os.utime(dst_f, None)
        elif skip_download:
            left = {f"{d:%Y-%m-%d}": p for d, f in jobs if (p := inbox_problems(f, d))}
            if left:
                return fail("INPUT_FILES", json.dumps(left))
        else:
            if collector is None:
                from srm_collector import collect as collector  # noqa: N813
            until = at(run_date, retry_until or SLOTS[slot]["retry_until"])
            dl = download_with_retry(jobs, collector, until, retry_every_min, now_fn, sleep)
            st["download"] = dl
            if dl["status"] != "OK":
                last = dl["attempts"][-1] if dl["attempts"] else {}
                return fail("DOWNLOAD", f"{len(dl['attempts'])} tries until {until:%H:%M}; last: "
                                        f"{last.get('status')} {last.get('detail', '')}; still bad: "
                                        f"{json.dumps(dl['problems'])}"[:900])
            if len(dl["attempts"]) > 1:
                st["notes"].append(f"SRM files complete on try {len(dl['attempts'])} "
                                   f"({dl['attempts'][-1]['at'][:5]}).")

        # 2. morning stock
        zm = zm212 or zm212_file(run_date)
        if zm212_wait_min is None:
            until_zm = SLOTS[slot]["zm212_until"]
            zm212_wait_min = max(0.0, (at(run_date, until_zm) - now_fn()).total_seconds() / 60) if until_zm else 0.0
        if not wait_for_file(zm, zm212_wait_min, sleep=sleep):
            return fail("ZM212", f"{zm.name} not ready (waited {zm212_wait_min:.0f} min for the 04_JOB 06:45 export)")

        # 3. engine (+ refresh baseline lookup if refresh=True)
        out = out_dir or (BASE_DIR / ("scratch" if dry_run else "reports"))
        prev_plans: Optional[Dict[str, Dict[str, Any]]] = None
        sup_out_dir: Optional[Path] = None
        f_tag = ""
        if refresh:
            prev_plans = {}
            for sup_match in (cfg.get("suppliers") or {}):
                found_prev = find_last_sent_pm_plan(sup_match, run_date, eff_logs, eff_reports, match_key=sup_match)
                if found_prev is None:
                    return fail(
                        "REFRESH_NO_PRIOR_SENT_PLAN",
                        f"Cannot compare refresh for '{sup_match}': no prior sent PM workbook found for {run_date.isoformat()}",
                    )
                prev_plans[sup_match] = found_prev
            sup_out_dir = out / f"suppliers_{run_date:%Y%m%d}_PM_REFRESH_{started:%H%M%S}"
            f_tag = f"_UPDATED_{started:%H%M}"

        status, res6 = run_engine(
            run_date,
            engine_today_dir,
            zm,
            out,
            learn=not dry_run,
            run_ts=started,
            prev_plans=prev_plans,
            supplier_out_dir=sup_out_dir,
            file_tag=f_tag,
            suppliers_csv=suppliers_csv,
            watchlist_csv=watchlist_csv,
            class_csv=class_csv,
        )
        st["engine"] = status
        if status.get("status") != "OK":
            return fail("ENGINE", f"{status.get('status')}: " + json.dumps(
                {k: status.get(k) for k in ("missing", "expected", "wrong", "zm212", "detail") if status.get(k)}))
        if slot == "PM" and not status.get("d1_hourly_used"):
            return fail("PM_WITHOUT_TOMORROW_FILES", "SRM files for tomorrow missing or not for tomorrow "
                                                     f"(dates {status.get('input_dates')}) - supplier mails not sent")
        status["notes"] = st["notes"] + status.get("notes", [])

        refresh_info: Optional[Dict[str, Any]] = None
        sup_file_by_vendor: Dict[str, Path] = {}
        if refresh:
            gen_files = [Path(x) for x in status.get("supplier_files", [])]
            vendors_in_run = sorted({
                r["vendor"] for r in res6
                if r.get("plan") and r.get("vendor") and srm_mailer.supplier_key(r["vendor"], cfg)
            })
            diffs_map: Dict[str, List[Dict[str, Any]]] = {}
            prev_sent_map: Dict[str, str] = {}
            sup_summary_list: List[Dict[str, Any]] = []
            only_low = (only_supplier or "").strip().lower()
            for vendor in vendors_in_run:
                m_key = srm_mailer.supplier_key(vendor, cfg) or vendor
                if only_low and only_low not in vendor.lower() and only_low not in m_key.lower():
                    continue
                att_list = [f for f in gen_files if f.name.startswith(supplier_file_prefix(vendor))]
                if not att_list:
                    continue
                att_f = att_list[0]
                sup_file_by_vendor[vendor] = att_f
                prev_entry = find_last_sent_pm_plan(vendor, run_date, eff_logs, eff_reports, match_key=m_key)
                if prev_entry is None:
                    return fail("REFRESH_NO_PRIOR_SENT_PLAN", f"No prior sent PM file for '{vendor}'")
                v_diffs = diff_truck_plan_parsed(prev_entry["parsed"], parse_truck_plan_workbook(att_f))
                diffs_map[vendor] = v_diffs
                prev_sent_map[vendor] = prev_entry["sent_at"]
                sup_summary_list.append({
                    "vendor": vendor,
                    "supplier_key": m_key,
                    "prev_sent": prev_entry["sent_at"],
                    "prev_file": str(prev_entry["file"]),
                    "new_file": str(att_f),
                    "new_sha256": hashlib.sha256(att_f.read_bytes()).hexdigest()[:12],
                    "changed_cells": len(v_diffs),
                    "would_send": len(v_diffs) > 0,
                    "sent": False,
                    "diffs": v_diffs,
                })
            refresh_info = {
                "refresh_at": f"{started:%H:%M}",
                "prev_sent": prev_sent_map,
                "diffs": diffs_map,
            }
            st["refresh"] = {
                "refresh_at": f"{started:%H:%M}",
                "suppliers": sup_summary_list,
            }
            status["refresh"] = st["refresh"]

        # 4. mails
        if send:
            marker = eff_logs / f"srm_mail_sent_{run_date:%Y%m%d}_{slot}.json"
            suppliers = True
            if not refresh and cfg["mode"] == "live" and marker.exists() and not resend:
                suppliers = False
                st["notes"].append("Live supplier mails were already sent for this date and slot (--resend repeats).")
            results = srm_mailer.send_run_mails(
                status, res6, started, cfg=cfg, sender=sender, suppliers=suppliers,
                only_supplier=only_supplier, updated=(updated or refresh), refresh_info=refresh_info,
            )
            st["mails"] = results
            if refresh and cfg["mode"] == "live":
                sent_updates = {
                    r["mail"]: {
                        "sent_at": f"{now_fn():%H:%M}",
                        "file": str(sup_file_by_vendor[r["mail"]]),
                    }
                    for r in results
                    if r["mail"] != "internal" and r.get("sent") and r["mail"] in sup_file_by_vendor
                }
                if sent_updates:
                    record_last_sent_pm_plan(run_date, eff_logs, sent_updates)
            elif cfg["mode"] == "live" and any(r["mail"] != "internal" and r.get("sent") for r in results):
                eff_logs.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
            failed = [r["mail"] for r in results if not r.get("sent") and not r.get("reason")]
            if failed:
                st.update(status="MAIL_FAILED", detail=f"mail not sent: {', '.join(failed)}")
                return st
        st["status"] = "OK"
        return st
    except Exception as exc:
        st["traceback"] = traceback.format_exc()[-2000:]
        return fail("ERROR", f"{type(exc).__name__}: {str(exc)[:300]}")
    finally:
        st["finished"] = f"{now_fn():%Y-%m-%d %H:%M:%S}"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="#330 SRM Monitoring pipeline (download -> ZM212 -> engine -> mails)")
    ap.add_argument("--slot", choices=("AM", "PM"), help="AM = 06:40 (delivery date today), PM = 13:40 (tomorrow)")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default today)")
    ap.add_argument("--skip-download", action="store_true", help="use the SRM files already in the inbox")
    ap.add_argument("--no-mail", action="store_true", help="no mail at all (also no failure alert)")
    ap.add_argument("--dry-run", action="store_true", help="write to scratch/, no mail")
    ap.add_argument("--resend", action="store_true", help="live mode: send supplier mails again for this date/slot")
    ap.add_argument("--refresh", action="store_true", help="manual PM refresh: re-download D+1 & send UPDATED only to changed suppliers")
    ap.add_argument("--only-supplier", help="filter supplier mail to matching supplier only (e.g. for targeted re-send)")
    ap.add_argument("--updated", action="store_true", help="mark supplier mail subject and body as UPDATED")
    ap.add_argument("--retry-until", help="HH:MM last download try (default AM 07:00, PM 14:00)")
    ap.add_argument("--retry-every-min", type=float, default=RETRY_EVERY_MIN, help="minutes between tries (5)")
    ap.add_argument("--zm212-wait-min", type=float, help="minutes to wait for ZM212 (default AM until 07:20, PM 0)")
    ap.add_argument("--console", action="store_true", help="print human-readable summary & append JSON to logs/srm_pipeline.log")
    a = ap.parse_args(argv)
    now = dt.datetime.now()
    slot = a.slot or ("PM" if a.refresh else default_slot(now))
    run_date = dt.date.fromisoformat(a.date) if a.date else now.date()
    load_env()
    if a.console:
        mode_tag = "PREVIEW (--dry-run, no mail)" if a.dry_run else "LIVE"
        print(f"[SRM PM Refresh] Starting ({mode_tag}) for {run_date.isoformat()}... Downloading fresh SRM D+1 files, please wait...", flush=True)
    if not _lock():
        st = {"slot": slot, "run_date": run_date.isoformat(), "status": "LOCKED", "stage": "LOCKED", "dry_run": a.dry_run,
              "started": f"{now:%Y-%m-%d %H:%M:%S}", "finished": f"{now:%Y-%m-%d %H:%M:%S}",
              "detail": "another SRM pipeline run is active (logs/srm_pipeline.lock)"}
        line = json.dumps(st, ensure_ascii=False)
        if a.console:
            LOGS.mkdir(parents=True, exist_ok=True)
            with open(LOGS / "srm_pipeline.log", "a", encoding="utf-8") as lf:
                lf.write(line + "\n")
            print("\n" + format_pm_refresh_console_summary(st), flush=True)
        else:
            print(line)
        return 3
    try:
        st = run_pipeline(
            slot, run_date, skip_download=a.skip_download, mail=not a.no_mail, dry_run=a.dry_run,
            resend=a.resend, zm212_wait_min=a.zm212_wait_min, retry_until=a.retry_until,
            retry_every_min=a.retry_every_min, only_supplier=a.only_supplier, updated=a.updated,
            refresh=a.refresh,
        )
    finally:
        _unlock()
    LOGS.mkdir(parents=True, exist_ok=True)
    suffix = ("_REFRESH" if a.refresh else "") + ("_DRYRUN" if a.dry_run else "")
    name = f"srm_pipeline_{run_date:%Y%m%d}_{slot}_{now:%H%M%S}{suffix}.json"
    (LOGS / name).write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    brief = {k: st.get(k) for k in ("slot", "run_date", "status", "stage", "detail")}
    brief["download_tries"] = len((st.get("download") or {}).get("attempts", []))
    brief["engine"] = {k: (st.get("engine") or {}).get(k) for k in (
        "status", "run_label", "parts_2h", "loading_changed_parts", "additional_qty_parts", "still_short_parts",
        "recheck_parts", "report")}
    if st.get("refresh"):
        brief["refresh"] = [
            {k: s.get(k) for k in ("vendor", "prev_sent", "changed_cells", "would_send", "sent", "new_sha256")}
            for s in st["refresh"].get("suppliers", [])
        ]
    brief["mails"] = [{k: r.get(k) for k in ("mail", "sent", "reason", "error")} for r in st.get("mails", [])]
    brief["log"] = name
    brief_line = json.dumps(brief, ensure_ascii=False, default=str)
    if a.console:
        with open(LOGS / "srm_pipeline.log", "a", encoding="utf-8") as lf:
            lf.write(brief_line + "\n")
        print("\n" + format_pm_refresh_console_summary(st, log_name=name), flush=True)
    else:
        print(brief_line)
    return 0 if st["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
