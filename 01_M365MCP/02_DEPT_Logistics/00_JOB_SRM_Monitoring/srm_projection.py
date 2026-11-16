"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - stock projection (pure functions, no I/O)
=====================================================================================
Runs 06:50 (delivery date today) and 13:40 (delivery date tomorrow -> merge_hourly / merge_daily replace
tomorrow onward). Reproduces the user's workbook (02._SRM_Short_Term_Monitoring, sheets TOP_6D / TOP_2W) exactly;
checked against the 11/11 workbook: 26/26 parts (TOP_6D) and 111/111 parts (TOP_2W) identical.

TOP_6D (2-hour slots, today 06:00 -> D+3 06:00; SRM slot '8-10' = 06:00 local):
  Order     = SRM Order Detail (ADD + 12 slots + day total per day)
  Prod.Plan = SRM Require Detail
  Inv       = SAP start + cumulative(Order - Prod.Plan)                    -> "Shortage" for Order..GR rows
  GR        = WMS receipts (101) per slot; receipts before 06:00 today are already in the morning stock
  GR-PO     = GR - Order
  GR Short  = SAP start + cumulative(GR - Prod.Plan)  (only what has really arrived) -> "Shortage" for GR-PO / GR Short
  Shortage  = local time label of the first slot below zero, else "Enough"
TOP_2W (daily, today + 14 days; day 0 = '<date> SUM'):
  Order     = SRM Plan&Require 'PLAN' (delivery plan), Prod.Plan = 'require' (production requirement)
  Inv       = SAP start + cumulative(Order - Prod.Plan)
  GR        = WMS receipts today and tomorrow (WMS day 06:00-06:00), GR-PO = GR - Order on those days
  GR Short  = WMS start + cumulative(today: GR, from tomorrow: Order) - cumulative Prod.Plan
  Shortage  = day label of the first GR Short below zero, else "Enough" (same label on all six rows)
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Iterable, List, Optional, Tuple

from srm_parsers import N_HOURLY_DAYS, N_SLOTS

ROWS = ("Order", "Prod.Plan", "Inv", "GR", "GR-PO", "GR Short")
ENOUGH = "Enough"
SLOT_TO_LOCAL_OFFSET_H = -2   # SRM slot '8-10' starts at 06:00 local (workbook 'TN Time' row)
DAY_START_H = 6               # morning ZM212 / WMS day boundary


# ---------------------------------------------------------------------------- 6-day hourly view
def timeline_6d() -> List[Tuple[str, int, int]]:
    """40 positions: ('add',0,0), then per day d: 12 x ('slot',d,s) and ('total',d,0)."""
    out: List[Tuple[str, int, int]] = [("add", 0, 0)]
    for d in range(N_HOURLY_DAYS):
        out += [("slot", d, s) for s in range(N_SLOTS)] + [("total", d, 0)]
    return out


def slot_start(run_date: dt.date, day: int, slot: int, offset_h: int = SLOT_TO_LOCAL_OFFSET_H) -> dt.datetime:
    return dt.datetime.combine(run_date, dt.time(0)) + dt.timedelta(days=day, hours=8 + 2 * slot + offset_h)


def local_label(ts: dt.datetime) -> str:
    h = ts.hour % 12 or 12
    return f"{ts.month}/{ts.day} {h}{'am' if ts.hour < 12 else 'pm'}"


def gr_by_slot(run_date: dt.date, events: Iterable[Tuple[str, dt.datetime, float]],
               offset_h: int = SLOT_TO_LOCAL_OFFSET_H) -> Dict[Tuple[str, int], float]:
    """{(part_key, day*12+slot): qty}; receipts before the first slot (already in the morning stock) and
    after the 3-day window are ignored."""
    t0 = slot_start(run_date, 0, 0, offset_h)
    out: Dict[Tuple[str, int], float] = {}
    for key, ts, qty in events:
        hrs = (ts - t0).total_seconds() / 3600.0
        if hrs < 0 or hrs >= 24 * N_HOURLY_DAYS:
            continue
        idx = int(hrs // 24) * N_SLOTS + int((hrs % 24) // 2)
        out[(key, idx)] = out.get((key, idx), 0.0) + qty
    return out


def _first_negative(values: List[float], labels: List[Optional[str]]) -> str:
    for v, lab in zip(values, labels):
        if lab is not None and v < -1e-9:
            return lab
    return ENOUGH


def project_6d(run_date: dt.date, part_keys: Iterable[str], orders: Dict[str, Dict[str, Any]],
               requires: Dict[str, Dict[str, Any]], stock: Dict[str, Dict[str, float]],
               gr_slots: Dict[Tuple[str, int], float], offset_h: int = SLOT_TO_LOCAL_OFFSET_H) -> List[Dict[str, Any]]:
    tl = timeline_6d()
    labels = [local_label(slot_start(run_date, d, s, offset_h)) if kind == "slot" else None for kind, d, s in tl]
    out = []
    for key in part_keys:
        o, q = orders.get(key), requires.get(key)
        meta = o or q or {}
        start = stock.get(key, {}).get("sap", 0.0)
        rows = {r: [] for r in ROWS}
        inv = gs = start
        for kind, d, s in tl:
            if kind == "add":
                order, req, gr = (o or {}).get("add", 0.0), 0.0, 0.0
            elif kind == "slot":
                i = d * N_SLOTS + s
                order = o["slots"][i] if o else 0.0
                req = q["slots"][i] if q else 0.0
                gr = gr_slots.get((key, i), 0.0)
            else:
                order = o["totals"][d] if o else 0.0
                req = q["totals"][d] if q else 0.0
                gr = sum(gr_slots.get((key, d * N_SLOTS + x), 0.0) for x in range(N_SLOTS))
            if kind != "total":
                inv += order - req
                gs += gr - req
            rows["Order"].append(order)
            rows["Prod.Plan"].append(req)
            rows["Inv"].append(inv)
            rows["GR"].append(gr)
            rows["GR-PO"].append(gr - order)
            rows["GR Short"].append(gs)
        out.append({
            "key": key, "part": meta.get("part", key), "vendor": meta.get("vendor", ""), "name": meta.get("name", ""),
            "start_sap": start, "start_wms": stock.get(key, {}).get("wms", 0.0), "rows": rows,
            "shortage_inv": _first_negative(rows["Inv"], labels),
            "shortage_gr": _first_negative(rows["GR Short"], labels),
        })
    return out


# ---------------------------------------------------------------------------- 2-week daily view
def gr_by_wms_day(run_date: dt.date, events: Iterable[Tuple[str, dt.datetime, float]], n_days: int = 2) -> Dict[Tuple[str, int], float]:
    t0 = dt.datetime.combine(run_date, dt.time(DAY_START_H))
    out: Dict[Tuple[str, int], float] = {}
    for key, ts, qty in events:
        d = int((ts - t0).total_seconds() // 86400) if ts >= t0 else -1
        if 0 <= d < n_days:
            out[(key, d)] = out.get((key, d), 0.0) + qty
    return out


def project_2w(day_labels: List[str], part_keys: Iterable[str], plan_req: Dict[str, Dict[str, Any]],
               stock: Dict[str, Dict[str, float]], gr_days: Dict[Tuple[str, int], float]) -> List[Dict[str, Any]]:
    n = len(day_labels)
    out = []
    for key in part_keys:
        rec = plan_req.get(key) or {}
        order = list(rec.get("plan", [0.0] * n))[:n]
        req = list(rec.get("require", [0.0] * n))[:n]
        sap = stock.get(key, {}).get("sap", 0.0)
        wms = stock.get(key, {}).get("wms", 0.0)
        rows = {r: [] for r in ROWS}
        inv, gs = sap, wms
        for d in range(n):
            gr = gr_days.get((key, d), 0.0) if d < 2 else None
            inv += order[d] - req[d]
            gs += (gr if d == 0 else order[d]) - req[d]
            rows["Order"].append(order[d])
            rows["Prod.Plan"].append(req[d])
            rows["Inv"].append(inv)
            rows["GR"].append(gr)
            rows["GR-PO"].append(None if gr is None else gr - order[d])
            rows["GR Short"].append(gs)
        out.append({
            "key": key, "part": rec.get("part", key), "vendor": rec.get("vendor", ""), "name": rec.get("name", ""),
            "box_qty": rec.get("box_qty", 0.0), "start_sap": sap, "start_wms": wms, "rows": rows,
            "shortage": _first_negative(rows["GR Short"], list(day_labels)),
        })
    return out


# ---------------------------------------------------------------------------- 06:50 + 13:40 (D+1) merge
def md(d: dt.date) -> str:
    return f"{d.month}/{d.day}"


def merge_hourly(run_date: dt.date, morning: Dict[str, Dict[str, Any]], d1: Optional[Dict[str, Dict[str, Any]]],
                 d1_labels: Optional[List[str]]) -> Tuple[Dict[str, Dict[str, Any]], bool]:
    """Workbook rule (TOP_6D): today's block always from the 06:50 file (delivery date = today, final incl. urgent
    orders); tomorrow / day after from the 13:40 file (delivery date = tomorrow: its DAY -> DAY1, its DAY1 -> DAY2)
    when that file's first date is today+1, otherwise from the 06:50 file. Returns (merged, d1_used)."""
    if not d1 or not d1_labels or d1_labels[0] != md(run_date + dt.timedelta(days=1)):
        return morning, False
    out: Dict[str, Dict[str, Any]] = {}
    for key in list(dict.fromkeys(list(morning) + list(d1))):
        m, n = morning.get(key), d1.get(key)
        base = dict(m or n)
        slots = list(m["slots"][:N_SLOTS]) if m else [0.0] * N_SLOTS
        slots += list(n["slots"][:2 * N_SLOTS]) if n else [0.0] * (2 * N_SLOTS)
        totals = [m["totals"][0] if m else 0.0] + (list(n["totals"][:2]) if n else [0.0, 0.0])
        base.update(slots=slots, totals=totals, add=(m or {}).get("add", 0.0))
        out[key] = base
    return out, True


def merge_daily(run_date: dt.date, labels: List[str], morning: Dict[str, Dict[str, Any]],
                d1_labels: Optional[List[str]], d1: Optional[Dict[str, Dict[str, Any]]],
                n_days: int = 15) -> Tuple[List[str], Dict[str, Dict[str, Any]], bool]:
    """Daily view (user decision 11/12, option A): today from the 06:50 Plan & Require, tomorrow onward
    (delivery plan AND requirement) from the 13:40 Plan & Require when its first date is today+1."""
    def _dates(lbls: List[str]) -> List[str]:
        return [l.replace(" SUM", "").strip() for l in lbls]

    use_d1 = bool(d1 and d1_labels and _dates(d1_labels)[0] == md(run_date + dt.timedelta(days=1)))
    want = [md(run_date + dt.timedelta(days=i)) for i in range(n_days)]
    out_labels = [labels[0] if labels else f"{want[0]} SUM"] + want[1:]
    m_idx = {d: i for i, d in enumerate(_dates(labels))}
    n_idx = {d: i for i, d in enumerate(_dates(d1_labels or []))}
    out: Dict[str, Dict[str, Any]] = {}
    for key in list(dict.fromkeys(list(morning) + (list(d1) if use_d1 else []))):
        m, n = morning.get(key), (d1 or {}).get(key) if use_d1 else None
        rec = dict(m or n)
        req, plan = [], []
        for i, d in enumerate(want):
            src, idx = (n, n_idx.get(d)) if (use_d1 and i >= 1) else (m, m_idx.get(d))
            req.append(src["require"][idx] if src and idx is not None else 0.0)
            plan.append(src["plan"][idx] if src and idx is not None else 0.0)
        rec.update(require=req, plan=plan)
        out[key] = rec
    return out_labels, out, use_d1
