"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - runner
==================================================
Today's stock = morning ZM212 (04_JOB raw export, read-only). SRM exports (until the download is automated):
  inbox/<YYYYMMDD>/       morning run (06:40-07:00), delivery date = today (final for today incl. urgent orders)
  inbox/<YYYYMMDD>/D+1/   afternoon run (13:40-14:00), delivery date = tomorrow -> replaces tomorrow onward (2-hour
                          and daily views)
Output: Excel report + one Excel file per supplier. No SAP, no SRM session, no email here
(download / mail: run_srm_pipeline.py).

  python run_srm_monitoring.py --dry-run                 # today, writes to scratch/
  python run_srm_monitoring.py --date 2026-11-13         # writes to reports/
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
MCP_ROOT = BASE_DIR.parents[1]
ZM212_DIR = MCP_ROOT / "01_DEPT_Inventory" / "04_JOB_SystemReconciliation" / "raw_exports"
INBOX_DIR = BASE_DIR / "inbox"
WATCHLIST = BASE_DIR / "data" / "watchlist.csv"
CLASS_FILE = BASE_DIR / "data" / "part_class.csv"

from srm_parsers import (N_SLOTS, parse_hourly, parse_plan_require, part_key, read_rows, read_wms_gr,  # noqa: E402
                         read_zm212_stock)
from srm_projection import (gr_by_slot, gr_by_wms_day, local_label, md, merge_daily, merge_hourly,  # noqa: E402
                            project_2w, project_6d, slot_start)
from srm_report import write_report  # noqa: E402
from srm_trucks import PENDING_REASON, leveling_config, load_trucks, pm_additional_days, run_days  # noqa: E402
from srm_plan import plan_parts, pm_extra_days  # noqa: E402
from srm_supplier_files import write_supplier_files  # noqa: E402
from srm_env import company_name, load_env  # noqa: E402

_EXT = (".xlsx", ".xls", ".xlsb", ".csv", ".txt")


def find_inputs(folder: Path) -> Dict[str, Optional[Path]]:
    """Newest file per kind by name: Order Detail / Require Detail / Plan & Require / WMS GR List."""
    kinds: Dict[str, List[Path]] = {"order": [], "require": [], "planreq": [], "wms_gr": []}
    for p in folder.glob("*") if folder.exists() else []:
        if p.suffix.lower() not in _EXT or p.name.startswith("~$"):
            continue
        n = re.sub(r"[^a-z0-9]+", " ", p.stem.lower())
        if "order" in n and "detail" in n:
            kinds["order"].append(p)
        elif "require" in n and "detail" in n:
            kinds["require"].append(p)
        elif "plan" in n and "require" in n:
            kinds["planreq"].append(p)
        elif re.search(r"(^| )(wms )?gr( |$)", n):
            kinds["wms_gr"].append(p)
    return {k: (max(v, key=lambda x: x.stat().st_mtime) if v else None) for k, v in kinds.items()}


def zm212_file(run_date: dt.date) -> Path:
    return ZM212_DIR / f"ZM212_StockAccuracy_IWMS_{run_date:%Y%m%d}.txt"


def load_watchlist(path: Optional[Path] = None) -> Optional[Dict[str, List[str]]]:
    """data/watchlist.csv (view,part): the user's monitored parts (seeded from the workbook's TOP_6D / TOP_2W).
    Kept out of git (real part numbers). Missing file -> None (all parts)."""
    path = path or WATCHLIST
    if not path.exists():
        return None
    out: Dict[str, List[str]] = {"6D": [], "2W": []}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            v = (r.get("view") or "").strip().upper()
            if v in out and (r.get("part") or "").strip():
                out[v].append(part_key(r["part"]))
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def learn_classes(seen: Dict[str, str], path: Optional[Path] = None, write: bool = True) -> Dict[str, str]:
    """Plan & Require classification (C = several deliveries a day, L = once a day) is blank on days without a plan,
    so the last known value per part is kept in data/part_class.csv (not in git)."""
    path = path or CLASS_FILE
    known: Dict[str, str] = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            known = {r["key"]: r["cls"] for r in csv.DictReader(f) if r.get("key")}
    known.update({k: v for k, v in seen.items() if v in ("C", "L")})
    if not write:          # dry runs (old test files) never change the learned classes
        return known
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "cls"])
            w.writerows(sorted(known.items()))
    except OSError:
        pass
    return known


def infer_classes(cls: Dict[str, str], orders: Dict[str, Dict[str, Any]]) -> Tuple[Dict[str, str], List[str]]:
    """Parts never classified in Plan & Require: C (several deliveries a day) when the SRM orders use two or more
    time slots on one day, else L. Inferred values are used for this run only (not written to part_class.csv)."""
    eff = dict(cls)
    inferred: List[str] = []
    for k, r in orders.items():
        if eff.get(k) in ("C", "L"):
            continue
        s = r["slots"]
        per_day = [sum(1 for x in s[d * N_SLOTS:(d + 1) * N_SLOTS] if x > 0) for d in range(len(s) // N_SLOTS)]
        eff[k] = "C" if max(per_day or [0]) >= 2 else "L"
        inferred.append(k)
    return eff, inferred


def run(run_date: dt.date, inbox: Path, zm212: Path, out_dir: Path, learn: bool = True,
        run_ts: Optional[dt.datetime] = None) -> Dict[str, Any]:
    return run_engine(run_date, inbox, zm212, out_dir, learn, run_ts=run_ts)[0]


def run_engine(run_date: dt.date, inbox: Path, zm212: Path, out_dir: Path, learn: bool = True,
               run_ts: Optional[dt.datetime] = None, prev_plans: Optional[Dict[str, Dict[str, Any]]] = None,
               supplier_out_dir: Optional[Path] = None, file_tag: str = "",
               suppliers_csv: Optional[Path] = None, watchlist_csv: Optional[Path] = None,
               class_csv: Optional[Path] = None) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(status, 2-hour rows with r['plan']) - the rows feed the supplier mails (run_srm_pipeline.py)."""
    status: Dict[str, Any] = {"run_date": run_date.isoformat(), "status": "STARTED", "notes": []}
    try:
        company_name()          # supplier files carry .env COMPANY_NAME - check before any work
    except RuntimeError as exc:
        status.update(status="MISSING_ENV", detail=str(exc))
        return status, []
    files = find_inputs(inbox)
    d1 = find_inputs(inbox / "D+1")
    status["inputs"] = {k: (str(v) if v else None) for k, v in files.items()}
    missing = [k for k in ("order", "require", "planreq") if not files[k]]
    if missing:
        status.update(status="MISSING_INPUT", missing=missing)
        return status, []
    if not zm212.exists():
        status.update(status="MISSING_ZM212", zm212=str(zm212))
        return status, []
    o_lab_m, orders_m = parse_hourly(read_rows(files["order"]))
    r_lab_m, requires_m = parse_hourly(read_rows(files["require"]))
    labels2w, planreq = parse_plan_require(read_rows(files["planreq"]))
    status["input_dates"] = {"order": o_lab_m[0], "require": r_lab_m[0],
                             "plan_require": (labels2w[0] if labels2w else "").replace(" SUM", "").strip()}
    wrong = [k for k, v in status["input_dates"].items() if v != md(run_date)]
    if wrong:   # 06:50 files must be for delivery date = run date, otherwise every number is wrong
        status.update(status="WRONG_INPUT_DATE", expected=md(run_date), wrong=wrong)
        return status, []
    stock = read_zm212_stock(read_rows(zm212))
    orders, requires = orders_m, requires_m
    orders_d1 = requires_d1 = None
    use_d1 = False
    status.update(d1_hourly_used=False, d1_daily_used=False)
    status["inputs_d1"] = {k: (str(v) if v else None) for k, v in d1.items() if k != "wms_gr"}
    if d1["order"] and d1["require"]:
        o_lab, orders_d1 = parse_hourly(read_rows(d1["order"]))
        r_lab, requires_d1 = parse_hourly(read_rows(d1["require"]))
        status["input_dates"].update(d1_order=o_lab[0], d1_require=r_lab[0])
        orders, used_o = merge_hourly(run_date, orders_m, orders_d1, o_lab)
        requires, used_r = merge_hourly(run_date, requires_m, requires_d1, r_lab)
        use_d1 = used_o and used_r
        status["d1_hourly_used"] = use_d1
        if not use_d1:
            orders, requires = orders_m, requires_m
            status["notes"].append("13:40 Order/Require files are not for tomorrow: 06:50 files used for all days.")
    if d1["planreq"]:
        p_lab, p1 = parse_plan_require(read_rows(d1["planreq"]))
        status["input_dates"]["d1_plan_require"] = (p_lab[0] if p_lab else "").replace(" SUM", "").strip()
        labels2w, planreq_m, used_p = merge_daily(run_date, labels2w, planreq, p_lab, p1)
        status["d1_daily_used"] = used_p
        if used_p:
            for k, r in p1.items():          # box qty / classification of parts that only the 13:40 file has
                planreq_m.setdefault(k, r)
            planreq = planreq_m
        else:
            status["notes"].append("13:40 Plan & Require is not for tomorrow: 06:50 file used for all days.")
    gr_files = [f for f in (files["wms_gr"], d1["wms_gr"]) if f]
    gr_file = max(gr_files, key=lambda f: f.stat().st_mtime) if gr_files else None   # newest GR List of the day
    status["inputs"]["wms_gr"] = str(gr_file) if gr_file else None
    gr = read_wms_gr(read_rows(gr_file)) if gr_file else []
    if not gr_file:
        status["notes"].append("WMS GR List not provided: GR rows are 0 and 'received stock only' uses the morning stock only.")
    wl = load_watchlist(watchlist_csv)
    seen = {k: r.get("cls", "") for k, r in planreq.items()}
    for src in (orders_m, orders_d1 or {}):
        seen.update({k: r["cls"].upper() for k, r in src.items() if r["cls"].upper() in ("C", "L")})
    cls = learn_classes(seen, path=class_csv, write=learn)
    multi = [k for k, c in cls.items() if c == "C"]   # several deliveries a day: always listed (user 11/12)
    cls_run, inferred = infer_classes(cls, orders)
    if inferred:
        status["notes"].append(f"Parts without a Plan & Require classification yet: {len(inferred)} "
                               "(C/L taken from their SRM time slots for this run).")
    all6 = list(dict.fromkeys(list(orders) + list(orders_d1 or {}) +
                              [k for k, r in requires.items() if r["cls"].upper() == "6D"] + multi))
    if wl:
        p6 = list(dict.fromkeys(wl["6D"] + all6))   # overview covers every 2-hour item (user 11/12)
        p2 = wl["2W"]
    else:
        p6 = all6
        p2 = sorted(k for k, r in planreq.items() if any(r["plan"]) or any(r["require"]))
        status["notes"].append("No watchlist: all parts with SRM orders / 6D requirements are shown.")
    no_stock = [k for k in set(p6) | set(p2) if k not in stock]
    if no_stock:
        status["notes"].append(f"Monitored parts not in the morning ZM212 (start stock 0): {len(no_stock)}.")
    res6 = project_6d(run_date, p6, orders, requires, stock, gr_by_slot(run_date, gr))
    for r in res6:   # parts with no SRM 2-hour record: supplier / name from the 13:40 file or Plan & Require
        src = (orders_d1 or {}).get(r["key"]) or (requires_d1 or {}).get(r["key"]) or planreq.get(r["key"])
        if src and not r["vendor"]:
            r.update(vendor=src["vendor"], name=src["name"], part=src["part"])
    res2 = project_2w(labels2w, p2, planreq, stock, gr_by_wms_day(run_date, gr))
    box = {k: r["box_qty"] for k, r in planreq.items()}
    run_label = "PM" if use_d1 else "AM"
    pm_extra, days_cfg = pm_additional_days(), run_days()
    lvl_cfg = leveling_config(suppliers_path=suppliers_csv) if suppliers_csv else leveling_config()
    if run_ts is not None and run_ts.date() == run_date:
        stamp = run_ts
    elif run_ts is None and run_date == dt.date.today():
        stamp = dt.datetime.now()
    else:
        stamp = dt.datetime.combine(run_date, dt.time(13, 40) if use_d1 else dt.time(6, 40))
    lvl_summary = plan_parts(
        run_date, res6, orders_m, requires_m, orders_d1, requires_d1, use_d1, load_trucks(suppliers_csv), box, cls_run,
        pm_extra, days_cfg, level_cfg=lvl_cfg, run_ts=stamp,
    )
    status["notes"].extend(lvl_summary.get("notes") or [])
    extra_until = run_date + dt.timedelta(days=max(pm_extra_days(run_date, 4, pm_extra, days_cfg) if use_d1 else {0}))
    path = out_dir / f"SRM_Monitoring_{run_date:%Y%m%d}_{run_label}_{stamp:%H%M}{file_tag}.xlsx"
    write_report(path, run_date, res6, res2, labels2w, box,
                 [f"Morning stock: ZM212 {zm212.name}"] + [f"Note: {n}" for n in status["notes"]])
    sup_dir = supplier_out_dir or (out_dir / f"suppliers_{run_date:%Y%m%d}_{run_label}")
    sup = write_supplier_files(sup_dir, run_label, res6, box, stamp, prev_plans=prev_plans, file_tag=file_tag)
    plans = [(r, r["plan"]) for r in res6 if r.get("plan")]
    real = [(r, p) for r, p in plans if p["unfixed"] is not None and p["reason"] != PENDING_REASON]
    pending = [(r, p) for r, p in plans if p["unfixed"] is not None and p["reason"] == PENDING_REASON]

    def _when(p: Dict[str, Any]) -> str:
        i = p["unfixed"]
        return local_label(slot_start(run_date, i // N_SLOTS, i % N_SLOTS))

    status.update(
        status="OK", run_label=run_label, report=str(path), supplier_files=[str(x) for x in sup],
        parts_2h=len(res6), parts_daily=len(res2),
        short_2h_planned=sum(r["shortage_inv"] != "Enough" for r in res6),
        short_2h_received=sum(r["shortage_gr"] != "Enough" for r in res6),
        short_daily=sum(r["shortage"] != "Enough" for r in res2),
        loading_changed_parts=sum(1 for _, p in plans if p["trucks"] and any(
            abs(a - b) > 1e-9 for a, b in zip(p["load"], p["srm"]))),
        additional_qty_parts=sum(1 for _, p in plans if any(p["extra"])),
        still_short_parts=len(real),          # short even after loading + additional quantity (call / extra truck)
        recheck_parts=len(pending),           # short on a day whose orders come with the 13:40 update
        still_short=[{"part": r["part"], "name": r["name"], "vendor": r["vendor"], "when": _when(p),
                      "reason": p["reason"]} for r, p in real][:40],
        inferred_class_parts=len(inferred), pm_additional_days=pm_extra if use_d1 else None,
        additional_until=extra_until.isoformat(),   # last day that may get an additional quantity in this run
        leveling=lvl_summary,
        cap_warnings=list(lvl_summary.get("warnings") or []),
        moves_100pct=list(lvl_summary.get("moves_100pct") or []),
        leveling_summary_text=(
            f"{sum(d_rep['parts_touched'] for s_rep in (lvl_summary.get('suppliers') or {}).values() for d_rep in (s_rep.get('days') or {}).values())} part-day(s) touched, "
            f"{sum(d_rep['carts_moved'] for s_rep in (lvl_summary.get('suppliers') or {}).values() for d_rep in (s_rep.get('days') or {}).values()):.1f} cart(s) moved"
            if any(
                d_rep["parts_touched"] > 0
                for s_rep in (lvl_summary.get("suppliers") or {}).values()
                for d_rep in (s_rep.get("days") or {}).values()
            )
            else "none (SRM baseline kept)"
        ),
    )
    return status, res6


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="02_DEPT_Logistics 00 SRM Monitoring (v1: files in, Excel out)")
    ap.add_argument("--date", help="run date YYYY-MM-DD (default today)")
    ap.add_argument("--inbox", help="folder with SRM exports (default inbox/<YYYYMMDD>)")
    ap.add_argument("--zm212", help="morning ZM212 export (default 04 raw_exports, today)")
    ap.add_argument("--dry-run", action="store_true", help="write to scratch/ instead of reports/")
    a = ap.parse_args(argv)
    load_env()
    run_date = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    inbox = Path(a.inbox) if a.inbox else INBOX_DIR / f"{run_date:%Y%m%d}"
    zm = Path(a.zm212) if a.zm212 else zm212_file(run_date)
    out_dir = BASE_DIR / ("scratch" if a.dry_run else "reports")
    st = run(run_date, inbox, zm, out_dir, learn=not a.dry_run)
    logs = BASE_DIR / "logs"
    logs.mkdir(exist_ok=True)
    (logs / f"srm_run_{dt.datetime.now():%Y%m%d_%H%M%S}{'_DRYRUN' if a.dry_run else ''}.json").write_text(
        json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(st, ensure_ascii=False))
    return 0 if st["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
