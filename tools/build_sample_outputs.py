#!/usr/bin/env python3
"""Build the sample outputs shown on the live page (demo/outputs.js).

The organization's real outputs are e-mails and workbooks about a real plant, so none of them is in this repository.
This script produces the same kinds of output for the synthetic plant of the 3D Warehouse Visualizer
(https://github.com/Miaou360/3d-warehouse-visualizer): the same parts, suppliers and rack locations that the
3D view and the pulling list use. Fixed seed: the output is identical on every run.

    python tools/build_sample_outputs.py --source ../3d-warehouse-visualizer/demo/data
    python tools/build_sample_outputs.py            # reads the published demo data
"""
from __future__ import annotations

import argparse
import json
import random
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PAGES_SOURCE = "https://miaou360.github.io/3d-warehouse-visualizer/demo/data"
SEED = 20261116
FAC = {"north": ("Facility North", "WH-NORTH"), "south": ("Facility South", "WH-SOUTH")}


def read_bundle(source: str, name: str) -> dict[str, Any]:
    if source.startswith("http"):
        with urllib.request.urlopen(f"{source.rstrip('/')}/{name}", timeout=30) as resp:  # noqa: S310
            text = resp.read().decode("utf-8")
    else:
        text = (Path(source) / name).read_text(encoding="utf-8")
    start = text.rindex("] = {") + 4 if '"] = {' in text else text.index("{")
    return json.loads(text[start:text.rindex("}") + 1])


def pct(a: float, b: float) -> float:
    return round(100.0 * a / b, 1) if b else 0.0


def build(source: str) -> dict[str, Any]:
    rng = random.Random(SEED)
    planning = read_bundle(source, "planning.js")
    today = date.fromisoformat(planning["snapshot_date"])
    parts = {p["part_no"]: p for p in planning["parts"]}
    pallets: list[dict[str, Any]] = []
    bays: dict[str, dict[str, Any]] = {}
    for key in ("north", "south"):
        for bay in read_bundle(source, f"facility_{key}.js")["bays"]:
            if bay["zone"] not in ("Zone A", "Zone B"):
                continue
            bays[bay["bay_id"]] = bay
            for it in bay["items"]:
                pallets.append({**it, "bin": f"{bay['bay_id']}-{it['level']}", "bay": bay["bay_id"], "fac": key})
    by_part: dict[str, list[dict[str, Any]]] = {}
    for p in pallets:
        by_part.setdefault(p["part_no"], []).append(p)
    part_ids = sorted(by_part)
    desc = lambda pn: parts[pn]["description"]                                  # noqa: E731

    # ---- signals that the real jobs derive from ERP / WMS history (seeded here) ----
    open_error_parts = set(rng.sample(part_ids, 14))
    repeat_parts = set(rng.sample(part_ids, 16))
    overtaken = {p["box_id"] for p in rng.sample(pallets, 60)}
    newest_gr: dict[str, str] = {pn: max(x["gr_date"] for x in rows) for pn, rows in by_part.items()}

    # ---- 1. daily cycle count list ----
    scored = []
    for p in pallets:
        reasons, score = [], 0
        if p["age_days"] > 365:
            reasons.append(f"Stock age {p['age_days']} days"); score += 35
        elif p["age_days"] > 180:
            reasons.append(f"Stock age {p['age_days']} days"); score += 20
        if p["box_id"] in overtaken or (p["age_days"] > 90 and newest_gr[p["part_no"]] > p["gr_date"] and parts[p["part_no"]].get("velocity") == "fast"):
            reasons.append("Newer lots issued first (FIFO overtaken)"); score += 30
        if p["part_no"] in open_error_parts:
            reasons.append("Open goods-issue error on this part"); score += 40
        if p["part_no"] in repeat_parts:
            reasons.append("Mismatch in 2 of the last 3 counts"); score += 25
        if p["qty"] < 0.25 * parts[p["part_no"]].get("pallet_qty", 48):
            reasons.append("Partial pallet"); score += 10
        scored.append((score, p, reasons))
    scored.sort(key=lambda t: (-t[0], t[1]["bin"]))
    quota = {"north": 16, "south": 6}
    model_rows, used_bins = [], set()
    for score, p, reasons in scored:
        if quota[p["fac"]] and p["bin"] not in used_bins and reasons:
            quota[p["fac"]] -= 1
            used_bins.add(p["bin"])
            model_rows.append(("Model", score, p, reasons))
    rest = [t for t in scored if t[1]["bin"] not in used_bins]
    explore = [("Explore", s, p, ["Random pick (baseline for the hit rate)"]) for s, p, _ in rng.sample(rest, 5)]
    follow = [("Follow-up", s, p, ["Recount: adjusted after yesterday's count"]) for s, p, _ in rng.sample(rest, 3)]
    count_rows = []
    res_rng = random.Random(SEED + 1)                 # count results: separate stream from the list selection
    for lane, score, p, reasons in model_rows + explore + follow:
        miss_p = {"Model": min(0.62, 0.12 + score / 260.0), "Explore": 0.14, "Follow-up": 0.2}[lane]
        counted, result = p["qty"], "Match"
        if res_rng.random() < miss_p:
            kind = res_rng.choice(["short", "short", "over", "empty", "wrong"])
            if kind == "short":
                counted, result = max(0, p["qty"] - res_rng.choice([1, 2, 5, 10, 24])), "Short"
            elif kind == "over":
                counted, result = p["qty"] + res_rng.choice([1, 2, 6, 12]), "Over"
            elif kind == "empty":
                counted, result = 0, "Not found"
            else:
                counted, result = p["qty"], "Wrong part in bin"
        count_rows.append({"lane": lane, "bin": p["bin"], "facility": FAC[p["fac"]][0], "part_no": p["part_no"],
                           "description": p["description"], "system_qty": p["qty"], "score": score, "reasons": reasons,
                           "counted_qty": counted, "result": result})
    lane_order = {"Model": 0, "Follow-up": 1, "Explore": 2}
    count_rows.sort(key=lambda r: (lane_order[r["lane"]], -r["score"], r["bin"]))
    for i, r in enumerate(count_rows, 1):
        r["no"] = i
    def hit(lane: str) -> dict[str, Any]:
        rows = [r for r in count_rows if r["lane"] == lane]
        miss = sum(r["result"] != "Match" for r in rows)
        return {"lane": lane, "bins": len(rows), "mismatches": miss, "hit_rate": pct(miss, len(rows))}
    trend = []
    for back in range(13, 0, -1):
        d = today - timedelta(days=back)
        if d.weekday() == 6:
            continue
        trend.append({"date": d.isoformat(), "model": round(31 + rng.uniform(-7, 9) + (13 - back) * 0.5, 1),
                      "explore": round(13 + rng.uniform(-5, 5), 1)})
    hits = [hit("Model"), hit("Follow-up"), hit("Explore")]
    trend.append({"date": today.isoformat(), "model": hits[0]["hit_rate"], "explore": hits[2]["hit_rate"]})
    count = {"rows": count_rows, "lanes": hits, "trend": trend, "bins_total": len({p["bin"] for p in pallets})}

    # ---- 2. interface (goods receipt / goods issue) errors ----
    causes = [
        ("GI 261", "Stock is in the other storage location", "Transfer posted, error cleared on re-run", "Auto-cleared"),
        ("GI 261", "Stock is in the other storage location", "Transfer posted, error cleared on re-run", "Auto-cleared"),
        ("GI 261", "Stock is in the other storage location", "Transfer posted, error cleared on re-run", "Auto-cleared"),
        ("GI 261", "Receipt not posted yet (truck unloaded, GR pending)", "Waits for the receipt; re-checked every 30 minutes", "Waiting"),
        ("GI 261", "No stock anywhere in the plant", "No transfer possible; sent to the inventory planner", "Needs a person"),
        ("GR 101", "Purchase order quantity exceeded", "Not touched; sent to purchasing", "Needs a person"),
        ("GI 261", "Material locked by another user", "Retried on the next run", "Auto-cleared"),
        ("GI 261", "Transfer would lower stock accuracy", "Blocked by the accuracy guard; listed for a count", "Held by guard"),
    ]
    interface_rows = []
    for i, pn in enumerate(sorted(open_error_parts)):
        mv, cause, action, status = causes[i % len(causes)]
        qty = rng.choice([4, 12, 24, 36, 60, 96, 120, 240])
        interface_rows.append({"part_no": pn, "description": desc(pn), "movement": mv, "qty": qty, "cause": cause,
                               "action": action, "status": status, "first_seen": f"{rng.choice([5, 5, 6, 6, 7]):02d}:{rng.choice([0, 30]):02d}",
                               "to_count_list": status == "Held by guard"})
    interface_rows.sort(key=lambda r: (["Needs a person", "Held by guard", "Waiting", "Auto-cleared"].index(r["status"]), r["part_no"]))
    interface = {"rows": interface_rows, "runs_today": 9,
                 "summary": {s: sum(r["status"] == s for r in interface_rows) for s in ["Auto-cleared", "Waiting", "Held by guard", "Needs a person"]}}

    # ---- 3. production backlog (failed backflush) ----
    bk_parts = rng.sample(part_ids, 46)
    cleared, remaining = [], []
    for i, pn in enumerate(bk_parts):
        need = rng.choice([6, 12, 18, 30, 48, 72, 150, 300])
        north = sum(x["qty"] for x in by_part[pn] if x["fac"] == "north")
        if i < 29:
            _ = rng.randint(1, 8)
            _ = rng.randrange(10**7)
            cleared.append({"part_no": pn, "description": desc(pn), "qty": need, "from": "WH-NORTH", "to": "LINE",
                            "run": "06:20" if i < 17 else ("08:15" if i < 23 else "08:30"),
                            "document": f"DOC-{i + 1:06d}",
                            "accuracy_before": round(rng.uniform(91, 97), 1), "delta": round(rng.uniform(0.05, 0.6), 2)})
        else:
            south = sum(x["qty"] for x in by_part[pn] if x["fac"] == "south")
            if north + south < need:
                why = ("Not enough stock in the plant", "Shortage: purchasing follows up")
            elif north < need:
                why = ("Stock only in Facility South", "Cross-facility transfer needs a person to release")
            elif pn in open_error_parts:
                why = ("Open goods-issue error on the same part", "Cleared after the interface error is resolved")
            else:
                why = rng.choice([
                    ("ERP and WMS disagree in two snapshots", "Held by the persistence guard; on tomorrow's count list"),
                    ("Transfer would lower stock accuracy", "Held by the accuracy guard"),
                    ("Receipt for this part is being posted", "Held by the chain guard; retried on the next run"),
                ])
            remaining.append({"part_no": pn, "description": desc(pn), "qty": need, "north_stock": north, "cause": why[0], "next_step": why[1]})
    backlog = {"start": len(bk_parts), "cleared": cleared, "remaining": remaining,
               "cleared_qty": sum(r["qty"] for r in cleared), "by_run": {r: sum(c["run"] == r for c in cleared) for r in ["06:20", "08:15", "08:30"]}}

    # ---- 4. ERP vs WMS reconciliation ----
    recon_rows = []
    diff_parts = set(rng.sample(part_ids, 23))
    for pn in part_ids:
        wms = sum(x["qty"] for x in by_part[pn])
        erp = wms
        if pn in diff_parts:
            erp = max(0, wms + rng.choice([-1, 1]) * rng.choice([12, 24, 48, 96, 120]))
        recon_rows.append({"part_no": pn, "description": desc(pn), "erp": erp, "wms": wms, "diff": erp - wms,
                           "snapshots": rng.choice([1, 1, 2, 3, 5]) if erp != wms else 0})
    acc_today = 96.42
    var = sorted([r for r in recon_rows if r["diff"]], key=lambda r: -abs(r["diff"]))
    for r in var:
        r["verdict"] = ("Persistent: goes to the count list" if r["snapshots"] >= 3 else
                        "Seen twice: watched" if r["snapshots"] == 2 else "Timing: posting in progress")
    acc_trend = [{"date": (today - timedelta(days=b)).isoformat(), "accuracy": round(acc_today - b * 0.11 + rng.uniform(-0.18, 0.18), 2)}
                 for b in range(13, 0, -1)]
    acc_trend.append({"date": today.isoformat(), "accuracy": acc_today})
    recon = {"parts": len(recon_rows), "matched": len(recon_rows) - len(var), "accuracy": acc_today, "variances": var[:14],
             "persistent": sum(r["snapshots"] >= 3 for r in var), "trend": acc_trend}

    # ---- 5. weekly backlog analysis ----
    weeks = []
    for w in range(8, 0, -1):
        start = today - timedelta(days=today.weekday() + 7 * w)
        opened = int(208 - (8 - w) * 9 + rng.uniform(-11, 11))
        auto = int(opened * (0.58 + (8 - w) * 0.025 + rng.uniform(-0.03, 0.03)))
        weeks.append({"week": start.isoformat(), "opened": opened, "auto_cleared": auto, "by_person": int((opened - auto) * 0.7),
                      "carried": opened - auto - int((opened - auto) * 0.7)})
    root = [("Unscanned picks & late transfers", 56), ("4-week supply vs usage gap (BOM / box check)", 33),
            ("Warehouse shortage & unposted GR", 11)]
    repeat = [{"part_no": pn, "description": desc(pn), "days": d, "cause": c}
              for pn, d, c in zip(sorted(rng.sample(part_ids, 6)), [5, 5, 4, 4, 3, 3],
                                  ["Unscanned picks & late transfers", "Unscanned picks & late transfers", "4-week supply vs usage gap",
                                   "Warehouse shortage & unposted GR", "Unscanned picks & late transfers", "4-week supply vs usage gap"])]
    weekly = {"weeks": weeks, "root_causes": [{"cause": c, "share": s} for c, s in root], "repeat_parts": repeat}

    # ---- 6. short-term supply monitoring ----
    sup_rows = []
    fast = [pn for pn in part_ids if parts[pn].get("velocity") == "fast"] or part_ids
    for pn in sorted(rng.sample(fast, min(12, len(fast)))):
        need = rng.choice([120, 240, 360, 480, 720])
        on_hand = rng.choice([0, 40, 90, 150, 260, 400])
        truck = rng.choice([0, 0, 120, 240, 480])
        short = max(0, need - on_hand - truck)
        sup_rows.append({"part_no": pn, "description": desc(pn), "supplier": parts[pn]["supplier_name"], "need_2h": need, "on_hand": on_hand,
                         "on_truck": truck, "short": short, "window": rng.choice(["10:00-12:00", "12:00-14:00", "16:00-18:00"]),
                         "action": ("Add %d to the next truck" % short) if short else "Covered"})
    sup_rows.sort(key=lambda r: (-r["short"], r["window"]))
    srm = {"rows": sup_rows, "monitored": 29, "daily_monitored": 94, "short": sum(r["short"] > 0 for r in sup_rows),
           "suppliers": ["Supplier A", "Supplier B", "Supplier C"]}

    # ---- 7. morning summary ----
    slots = [("02:15", "Movement ledger collection", "col", "OK", "Yesterday's material documents loaded"),
             ("03:10", "Ledger backup", "hq", "OK", "9 ledgers copied and verified"),
             ("05:45", "Stock snapshots (ERP and WMS)", "rec", "OK", "Both snapshots taken within 2 minutes"),
             ("06:00", "Interface errors, continuous daemon", "gi", "OK", "23 transfers posted across 29 runs"),
             ("06:20", "Backlog clearing, run 1", "bkl", "OK", f"{backlog['by_run']['06:20']} transfers posted"),
             ("06:40", "Supplier monitoring & truck plan", "srm", "OK", "29 2h / 94 daily parts · Supplier A & B leveled truck plans sent"),
             ("06:50", "Daily count list", "acc", "OK", f"{len(count_rows)} bins sent to the count team"),
             ("07:20", "Portal draft collection", "gi", "LATE", "Finished 6 minutes after the slot (portal slow)"),
             ("07:30", "Reconciliation report", "rec", "OK", f"Official accuracy {acc_today}%")]
    morning = {"slots": [{"time": t, "task": n, "unit": u, "status": s, "note": x} for t, n, u, s, x in sorted(slots)],
               "ok": 20, "late": 1, "failed": 0}

    # ---- 8. manifest of real-format rendered report artifacts under demo/reports/ ----
    reports = {
        "count": [
            {"id": "daily-mail", "label": "Daily Dispatch Mail (06:50)", "kind": "HTML mail", "src": "reports/count_daily_mail.html"},
            {"id": "daily-xlsx", "label": "Daily Count Sheet (.xlsx)", "kind": "2-tab workbook", "src": "reports/count_daily_sheet.html"},
            {"id": "weekly-mail", "label": "Saturday Weekly Mail (07:00)", "kind": "HTML mail", "src": "reports/count_weekly_mail.html"},
            {"id": "weekly-xlsx", "label": "Saturday Weekly Sheet (.xlsx)", "kind": "2-tab workbook", "src": "reports/count_weekly_sheet.html"},
        ],
        "hitrate": [
            {"id": "brief", "label": "Weekly Floor Brief & Hit-Rate Mail", "kind": "6-chart HTML mail", "src": "reports/weekly_hit_rate.html"},
            {"id": "xlsx", "label": "4-Sheet Evidence Workbook (.xlsx)", "kind": "4-tab workbook", "src": "reports/weekly_hit_rate_workbook.html"},
        ],
        "backlog": [
            {"id": "mail", "label": "Daily Resolution & Status Mail (08:42)", "kind": "HTML mail", "src": "reports/backlog_mail.html"},
            {"id": "xlsx", "label": "Unresolved Backlog Workbook (.xlsx)", "kind": "2-tab workbook", "src": "reports/backlog_workbook.html"},
        ],
        "interface": [
            {"id": "mail", "label": "Daily GI Cumulative Report (07:30)", "kind": "HTML mail + D107/D109/D110", "src": "reports/gi_daily_mail.html"},
        ],
        "weekly": [
            {"id": "slides", "label": "5-Page Visual Report (PDF Layout)", "kind": "5 landscape pages", "src": "reports/backlog_3q_slides.html"},
            {"id": "mail", "label": "Friday 08:30 Cover Mail", "kind": "HTML mail", "src": "reports/backlog_3q_mail.html"},
            {"id": "xlsx", "label": "3-Sheet Filterable Workbook (.xlsx)", "kind": "3-tab workbook", "src": "reports/backlog_3q_workbook.html"},
        ],
        "srm": [
            {"id": "truck-plan", "label": "Supplier A Truck Plan (.xlsx)", "kind": "Truck plan + 2-hour detail · 33-cart cap + Extra truck banner", "src": "reports/srm_truck_plan.html"},
            {"id": "mails", "label": "Supplier & Internal Dispatch Mails", "kind": "HTML mails + Leveling Summary", "src": "reports/srm_mail.html"},
            {"id": "mon-xlsx", "label": "Internal Monitoring Workbook (.xlsx)", "kind": "3-tab workbook", "src": "reports/srm_monitoring_workbook.html"},
        ],
        "srm-updated": [
            {"id": "updated-xlsx", "label": "UPDATED Supplier A Truck Plan (.xlsx)", "kind": "Yellow = changed since 13:40 plan", "src": "reports/srm_truck_plan_updated.html"},
            {"id": "updated-mail", "label": "UPDATED Mail & Diff Summary (15:40)", "kind": "Replacement top line + cell diff", "src": "reports/srm_updated_mail.html"},
        ],
        "delivery": [
            {"id": "status-xlsx", "label": "Supplier A Delivery Status (.xlsx)", "kind": "Plan / Delivered + red shortfall + Warning", "src": "reports/delivery_status_workbook.html"},
            {"id": "status-mail", "label": "Shortfall Warning & Truck-Close Mails", "kind": "HTML mails", "src": "reports/delivery_status_mail.html"},
            {"id": "dlv-int-xlsx", "label": "Internal Delivery Check Workbook (.xlsx)", "kind": "3-tab workbook", "src": "reports/delivery_internal_workbook.html"},
        ],
        "recon": [
            {"id": "mail", "label": "Executive Reconciliation Mail (07:30)", "kind": "HTML mail", "src": "reports/recon_mail.html"},
            {"id": "xlsx", "label": "4-Sheet Reconciliation Workbook (.xlsx)", "kind": "4-tab workbook", "src": "reports/recon_workbook.html"},
        ],
        "morning": [
            {"id": "mail", "label": "HQ Morning Operations Brief (07:35)", "kind": "HTML mail", "src": "reports/morning_ops.html"},
        ],
    }

    return {"meta": {"date": today.isoformat(), "seed": SEED, "source": "https://github.com/Miaou360/3d-warehouse-visualizer",
                     "notice": "Sample outputs for a synthetic plant. No real part, supplier, quantity or result."},
            "reports": reports,
            "count": count, "interface": interface, "backlog": backlog, "recon": recon, "weekly": weekly, "srm": srm, "morning": morning}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default=PAGES_SOURCE, help="folder or URL with the 3D Warehouse Visualizer data bundles")
    args = ap.parse_args()
    out = Path(__file__).resolve().parents[1] / "demo" / "outputs.js"
    payload = build(args.source)
    out.write_text("/* Generated by tools/build_sample_outputs.py from the 3D Warehouse Visualizer demo data. 100% synthetic. */\n"
                   "window.OUTPUTS = " + json.dumps(payload, separators=(",", ":")) + ";\n", encoding="utf-8", newline="\n")
    c = payload["count"]
    print(f"{out.name}: count {len(c['rows'])} bins {[(l['lane'], l['hit_rate']) for l in c['lanes']]}, "
          f"backlog {payload['backlog']['start']} -> {len(payload['backlog']['remaining'])}, accuracy {payload['recon']['accuracy']}%")


if __name__ == "__main__":
    main()
