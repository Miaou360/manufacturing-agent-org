"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - per-part loading plan over the SRM days of a run (#330 / #367 / #384)
=================================================================================================================
06:40 run (AM): days = today, +1, +2 from the morning files (delivery date today).
13:40 run (PM): days = today (morning file) + the 3 days of the 13:40 file (delivery date tomorrow: +1, +2, +3).
Sequence (#384 R1-R5 minimal change from SRM order):
  1. Read SRM order + urgent ADD (day totals fixed; no memory of earlier mails).
  2. Compute baseline (`srm_trucks.plan_trucks`, freezing departed trucks `R2`; untouched parts with no shortage and
     no ADD stay strictly as SRM ordered even if below 115% `R5`).
  3. Distribute urgent ADD onto lightest editable trucks (`R3a`) and unload editable trucks > `1.7 x day_average`
     with minimal parts touched (`R3b` & `R4`, `srm_leveling.level_all_suppliers`).
  4. Independent post-check & fallback to baseline on any violation (`R5`).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Optional

from srm_leveling import compute_frozen_slots, level_all_suppliers
from srm_parsers import N_SLOTS
from srm_trucks import (
    FIRST_TRUCK_REASON,
    PENDING_REASON,
    daily_extras,
    leveling_config,
    plan_trucks,
    trucks_for,
)


def day_sources(morning: Dict[str, Dict[str, Any]], d1: Optional[Dict[str, Dict[str, Any]]], key: str,
                use_d1: bool) -> Dict[str, List[float]]:
    """{'slots': n*12, 'add': n} for one part (orders or requirements file pair)."""
    m = morning.get(key)
    if not use_d1:
        slots = list(m["slots"]) if m else [0.0] * (3 * N_SLOTS)
        return {"slots": slots, "add": [m["add"] if m else 0.0, 0.0, 0.0]}
    n = (d1 or {}).get(key)
    slots = (list(m["slots"][:N_SLOTS]) if m else [0.0] * N_SLOTS) + (list(n["slots"]) if n else [0.0] * (3 * N_SLOTS))
    return {"slots": slots, "add": [m["add"] if m else 0.0, n["add"] if n else 0.0, 0.0, 0.0]}


def is_run_day(day: dt.date, run_days: str = "MonSat") -> bool:
    """Scheduled run days: 'MonSat' (user 11/12, no Sunday run) or 'Daily'."""
    return run_days == "Daily" or day.weekday() != 6


def pm_extra_days(run_date: dt.date, n_days: int, pm_extra: str, run_days: str = "MonSat") -> set:
    """Day indexes (0 = run date) of a 13:40 run that may get an additional quantity."""
    if pm_extra == "tomorrow":
        return {0, 1}
    if pm_extra == "next_run_day":
        d = 1
        while d < n_days - 1 and not is_run_day(run_date + dt.timedelta(days=d), run_days):
            d += 1
        return set(range(d + 1))
    return set(range(n_days))


def plan_parts(
    run_date: dt.date,
    res6: List[Dict[str, Any]],
    orders_m: Dict[str, Dict[str, Any]],
    requires_m: Dict[str, Dict[str, Any]],
    orders_d1: Optional[Dict[str, Dict[str, Any]]],
    requires_d1: Optional[Dict[str, Dict[str, Any]]],
    use_d1: bool,
    trucks_cfg: Dict[str, List[int]],
    box_qty: Dict[str, float],
    cls: Dict[str, str],
    pm_extra: str = "all",
    run_days: str = "MonSat",
    *,
    level_cfg: Optional[Dict[str, Any]] = None,
    run_ts: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Sets `r['plan']` on every projection row (`res6`) and returns the `#367 / #384` leveling summary."""
    n_days = 4 if use_d1 else 3
    extra_days = pm_extra_days(run_date, n_days, pm_extra, run_days) if use_d1 else {0}
    dates = [run_date + dt.timedelta(days=i) for i in range(n_days)]

    lvl_cfg = level_cfg if level_cfg is not None else leveling_config()
    delay_buf = 1.0 + float(lvl_cfg.get("safety_margin", 0.15))
    freeze_lead = float(lvl_cfg.get("freeze_lead_min", 30.0))
    frozen_slots = compute_frozen_slots(run_date, n_days, run_ts, freeze_lead)

    for r in res6:
        k = r["key"]
        o = day_sources(orders_m, orders_d1, k, use_d1)
        q = day_sources(requires_m, requires_d1, k, use_d1)
        req = q["slots"]
        box = box_qty.get(k, 0.0)
        tr = trucks_for(r["vendor"], trucks_cfg)
        multi = cls.get(k, "C" if not cls else "") != "L"
        if tr and cls.get(k) == "C":
            p = plan_trucks(
                r["start_sap"],
                o["add"],
                o["slots"],
                req,
                tr,
                box,
                extra_days,
                delay_buffer=delay_buf,
                frozen_slots=frozen_slots,
            )
            load, extra, add_by_slot, unfixed, reason = (
                p["slots"], p["extra"], p["add_by_slot"], p["unfixed"], p["reason"]
            )
        else:
            supply = list(o["slots"])
            add_by_slot = [0.0] * len(supply)
            for d in range(n_days):
                supply[d * N_SLOTS] += o["add"][d]
                add_by_slot[d * N_SLOTS] = float(o["add"][d])
            extra = daily_extras(r["start_sap"], supply, req, box, extra_days)
            load = [a + b for a, b in zip(supply, extra)]
            unfixed = reason = None
        stock, inv = [], r["start_sap"]
        for a, b in zip(load, req):
            inv += a - b
            stock.append(inv)
            if inv < -1e-9 and unfixed is None:
                unfixed = len(stock) - 1
                reason = FIRST_TRUCK_REASON if (unfixed // N_SLOTS) in extra_days else PENDING_REASON
        r["plan"] = {
            "dates": dates,
            "trucks": tr if (tr and cls.get(k) == "C") else None,
            "multi": multi,
            "cls": cls.get(k, ""),
            "srm": o["slots"],
            "add": o["add"],
            "add_by_slot": add_by_slot,
            "req": req,
            "load": load,
            "extra": extra,
            "stock": stock,
            "unfixed": unfixed,
            "reason": reason,
            "extra_days": extra_days,
        }

    return level_all_suppliers(
        run_date,
        res6,
        trucks_cfg,
        box_qty,
        lvl_cfg,
        run_ts=run_ts,
    )


def window(r: Dict[str, Any], first_day: int, n: int = 3) -> Dict[str, Any]:
    """Slice `r['plan']` to days `first_day .. first_day+n-1`; 'start' = stock before the window."""
    p = r["plan"]
    a, b = first_day * N_SLOTS, (first_day + n) * N_SLOTS
    start = r["start_sap"] if first_day == 0 else p["stock"][a - 1]
    unfixed = p["unfixed"]
    return {
        "dates": p["dates"][first_day:first_day + n],
        "trucks": p["trucks"],
        "multi": p["multi"],
        "cls": p["cls"],
        "srm": p["srm"][a:b],
        "add": p["add"][first_day:first_day + n],
        "req": p["req"][a:b],
        "load": p["load"][a:b],
        "extra": p["extra"][a:b],
        "stock": p["stock"][a:b],
        "start": start,
        "unfixed": (unfixed - a) if unfixed is not None and a <= unfixed < b else None,
        "short_before": unfixed is not None and unfixed < a,
        "reason": p["reason"],
    }
