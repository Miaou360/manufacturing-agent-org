"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - Manual PM Refresh Helpers (#330 / #392 / #394)
==========================================================================================
Supports `run_srm_pipeline.py --slot PM --refresh` (`run_srm_pm_refresh.bat`):
  1. Operational guards:
     - Only `--slot PM` allowed.
     - Refused during `06:20`..`07:35` morning freeze window.
     - Refused before today's scheduled `13:40` PM run has finished (`status == 'OK'`).
  2. Resolves the last PM supplier workbook actually sent today for each supplier
     (from `logs/srm_pm_last_sent_<YYYYMMDD>.json`, falling back to `13:40` sent files).
  3. Parses & compares `Truck plan` sheets semantically by `(part_no, day_header, slot_header)`
     so column shifts (e.g. `Pallet Truck` / `Other times` appearing or disappearing) never cause false diffs.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import load_workbook

from srm_parsers import SLOT_LABELS
from srm_supplier_files import supplier_file_prefix

STOP_PART_LABELS = {"Total EA", "Total boxes", "Total carts (boxes)"}


def _norm_slot_header(raw: Any) -> str:
    txt = str(raw or "").strip()
    if not txt:
        return ""
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return ""
    first = lines[0]
    if first in SLOT_LABELS:
        return first
    joined = " ".join(lines)
    if "pallet" in joined.lower():
        return "Pallet Truck"
    if "other" in joined.lower():
        return "Other times"
    if "day" in joined.lower() and "total" in joined.lower():
        return "Day total"
    return joined


def parse_truck_plan_workbook(xlsx_path: Path) -> Dict[str, Any]:
    """Extracts `{(part_no, day_header, slot_header): qty}` and `{part_no: part_name}` from a supplier workbook."""
    if not xlsx_path or not Path(xlsx_path).exists():
        return {"has_truck_plan": False, "days": [], "slots_by_day": {}, "parts": {}, "part_order": [], "cells": {}}
    wb = load_workbook(xlsx_path, data_only=True)
    if "Truck plan" not in wb.sheetnames:
        return {"has_truck_plan": False, "days": [], "slots_by_day": {}, "parts": {}, "part_order": [], "cells": {}}
    ws = wb["Truck plan"]
    h1, h2 = 5, 6
    max_col = ws.max_column

    day_by_col: Dict[int, str] = {}
    days_order: List[str] = []
    cur_day = ""
    for c in range(5, max_col + 1):
        v1 = ws.cell(row=h1, column=c).value
        if v1 is not None and str(v1).strip():
            s1 = str(v1).strip()
            if s1.lower() == "stock check":
                cur_day = ""
                continue
            cur_day = s1
            if cur_day not in days_order:
                days_order.append(cur_day)
        if cur_day:
            day_by_col[c] = cur_day

    load_cols: List[Tuple[int, str, str]] = []
    slots_by_day: Dict[str, List[str]] = {d: [] for d in days_order}
    for c, d_hdr in day_by_col.items():
        s_hdr = _norm_slot_header(ws.cell(row=h2, column=c).value)
        if not s_hdr or s_hdr == "Day total":
            continue
        load_cols.append((c, d_hdr, s_hdr))
        if s_hdr not in slots_by_day.setdefault(d_hdr, []):
            slots_by_day[d_hdr].append(s_hdr)

    parts: Dict[str, str] = {}
    part_order: List[str] = []
    cells: Dict[Tuple[str, str, str], float] = {}
    for r in range(h2 + 1, ws.max_row + 1):
        p_raw = ws.cell(row=r, column=1).value
        if p_raw is None:
            continue
        p_no = str(p_raw).strip()
        if not p_no or p_no in STOP_PART_LABELS:
            if p_no in STOP_PART_LABELS:
                break
            continue
        p_name = str(ws.cell(row=r, column=2).value or "").strip() or "(no description)"
        parts[p_no] = p_name
        if p_no not in part_order:
            part_order.append(p_no)
        for c, d_hdr, s_hdr in load_cols:
            raw_v = ws.cell(row=r, column=c).value
            try:
                q_val = float(raw_v) if raw_v is not None and str(raw_v).strip() != "" else 0.0
            except (TypeError, ValueError):
                q_val = 0.0
            cells[(p_no, d_hdr, s_hdr)] = q_val

    return {
        "has_truck_plan": True,
        "days": days_order,
        "slots_by_day": slots_by_day,
        "parts": parts,
        "part_order": part_order,
        "cells": cells,
    }


def _slot_sort_key(slot_hdr: str) -> Tuple[int, int, str]:
    if slot_hdr in SLOT_LABELS:
        return (0, SLOT_LABELS.index(slot_hdr), slot_hdr)
    if slot_hdr == "Pallet Truck":
        return (1, 0, slot_hdr)
    if slot_hdr == "Other times":
        return (2, 0, slot_hdr)
    return (3, 0, slot_hdr)


def diff_truck_plan_parsed(old_parsed: Dict[str, Any], new_parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Returns ordered list of cell changes `part`, `name`, `day`, `truck`, `old_qty`, `new_qty`."""
    days = list(dict.fromkeys(list(old_parsed.get("days") or []) + list(new_parsed.get("days") or [])))
    all_parts = list(dict.fromkeys(list(new_parsed.get("part_order") or []) + list(old_parsed.get("part_order") or [])))
    old_cells = old_parsed.get("cells") or {}
    new_cells = new_parsed.get("cells") or {}
    old_parts = old_parsed.get("parts") or {}
    new_parts = new_parsed.get("parts") or {}

    diffs: List[Dict[str, Any]] = []
    for d_hdr in days:
        slots = sorted(
            set((old_parsed.get("slots_by_day") or {}).get(d_hdr, []))
            | set((new_parsed.get("slots_by_day") or {}).get(d_hdr, [])),
            key=_slot_sort_key,
        )
        for s_hdr in slots:
            for p_no in all_parts:
                oq = float(old_cells.get((p_no, d_hdr, s_hdr), 0.0))
                nq = float(new_cells.get((p_no, d_hdr, s_hdr), 0.0))
                if abs(nq - oq) > 1e-6:
                    diffs.append({
                        "part": p_no,
                        "name": new_parts.get(p_no) or old_parts.get(p_no) or "(no description)",
                        "day": d_hdr,
                        "truck": s_hdr,
                        "old_qty": oq,
                        "new_qty": nq,
                    })
    return diffs


def diff_truck_plan_workbooks(old_xlsx: Path, new_xlsx: Path) -> List[Dict[str, Any]]:
    return diff_truck_plan_parsed(parse_truck_plan_workbook(old_xlsx), parse_truck_plan_workbook(new_xlsx))


def check_pm_refresh_guards(
    slot: str,
    run_date: dt.date,
    now_ts: dt.datetime,
    logs_dir: Path,
) -> Optional[Tuple[str, str]]:
    """Checks operational safety guards for `--refresh` (#392 §2)."""
    if slot != "PM":
        return "REFRESH_GUARD", "--refresh is only supported with --slot PM"
    t_now = now_ts.time()
    if dt.time(6, 20) <= t_now <= dt.time(7, 35):
        return "REFRESH_FREEZE_WINDOW", "PM refresh refused during 06:20-07:35 morning freeze window"

    marker = logs_dir / f"srm_mail_sent_{run_date:%Y%m%d}_PM.json"
    if marker.exists():
        return None
    for p in sorted(logs_dir.glob(f"srm_pipeline_{run_date:%Y%m%d}_PM_*.json")):
        if "_DRYRUN" in p.name or "_REFRESH" in p.name:
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if obj.get("status") == "OK":
                return None
        except Exception:
            continue
    return "REFRESH_BEFORE_1340", f"Today's scheduled 13:40 PM run for {run_date.isoformat()} has not finished yet"


def _base_1340_sent_time(run_date: dt.date, logs_dir: Path) -> str:
    for p in sorted(logs_dir.glob(f"srm_pipeline_{run_date:%Y%m%d}_PM_*.json")):
        if "_DRYRUN" in p.name or "_REFRESH" in p.name:
            continue
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if obj.get("status") == "OK":
                st = str(obj.get("started") or "")
                if len(st) >= 16:
                    hhmm = st[11:16]
                    return "13:40" if hhmm.startswith("13:4") else hhmm
        except Exception:
            continue
    return "13:40"


def find_last_sent_pm_plan(
    vendor: str,
    run_date: dt.date,
    logs_dir: Path,
    reports_dir: Path,
    match_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Finds `{sent_at, file, parsed}` for the last PM plan actually sent to `vendor` on `run_date`."""
    ledger_path = logs_dir / f"srm_pm_last_sent_{run_date:%Y%m%d}.json"
    prefix = supplier_file_prefix(vendor)
    mk_low = (match_key or "").strip().lower()
    v_low = (vendor or "").strip().lower()
    if ledger_path.exists():
        try:
            led = json.loads(ledger_path.read_text(encoding="utf-8"))
            for v_key, entry in led.items():
                vk_low = v_key.lower()
                if (
                    v_key == vendor
                    or supplier_file_prefix(v_key) == prefix
                    or (mk_low and mk_low in vk_low)
                    or (vk_low and vk_low in v_low)
                ):
                    fp = Path(entry["file"])
                    if fp.exists():
                        return {
                            "sent_at": str(entry.get("sent_at") or "13:40"),
                            "file": fp,
                            "parsed": parse_truck_plan_workbook(fp),
                        }
        except Exception:
            pass

    sent_hhmm = _base_1340_sent_time(run_date, logs_dir)
    for folder in (
        reports_dir / "sent_snapshots" / f"{run_date:%Y%m%d}_PM_1340",
        reports_dir / f"suppliers_{run_date:%Y%m%d}_PM",
    ):
        if not folder.exists():
            continue
        matches = sorted(folder.glob(f"{prefix}*.xlsx"))
        if not matches and mk_low:
            matches = [p for p in sorted(folder.glob("SRM_TruckPlan_*.xlsx")) if mk_low in p.name.lower()]
        if matches:
            fp = matches[-1]
            return {
                "sent_at": sent_hhmm,
                "file": fp,
                "parsed": parse_truck_plan_workbook(fp),
            }
    return None


def record_last_sent_pm_plan(
    run_date: dt.date,
    logs_dir: Path,
    updates: Dict[str, Dict[str, str]],
) -> Path:
    """Atomically updates `logs/srm_pm_last_sent_<YYYYMMDD>.json` after live refresh mails are sent."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = logs_dir / f"srm_pm_last_sent_{run_date:%Y%m%d}.json"
    cur: Dict[str, Any] = {}
    if ledger_path.exists():
        try:
            cur = json.loads(ledger_path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
    cur.update(updates)
    tmp = ledger_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, ledger_path)
    return ledger_path


STAGE_REASONS: Dict[str, str] = {
    "LOCKED": "Another SRM pipeline run is currently active (lock busy: logs/srm_pipeline.lock).",
    "REFRESH_BEFORE_1340": "Today's scheduled 13:40 PM run has not finished yet.",
    "REFRESH_FREEZE_WINDOW": "Refused during the 06:20-07:35 morning freeze window.",
    "REFRESH_GUARD": "Manual refresh is only allowed for the PM slot (--slot PM).",
    "PM_REFRESH_DOWNLOAD": "Fresh SRM D+1 download failed (existing D+1 files were left untouched).",
    "REFRESH_NO_PRIOR_SENT_PLAN": "No prior sent PM truck plan workbook was found for today.",
    "MAIL_FAILED": "One or more Outlook mails failed to send.",
}


def format_pm_refresh_console_summary(st: Dict[str, Any], log_name: str = "") -> str:
    """Human-readable console summary for interactive `run_srm_pm_refresh.bat` window (#396 §1)."""
    dry = bool(st.get("dry_run"))
    mode_lbl = "PREVIEW / DRY-RUN (NO MAIL SENT)" if dry else "LIVE PM REFRESH"
    status = str(st.get("status") or "UNKNOWN")
    run_date = str(st.get("run_date") or "")
    started = str(st.get("started") or "")
    finished = str(st.get("finished") or "")

    lines = [
        "=" * 72,
        f"  SRM PM REFRESH  |  {mode_lbl}",
        f"  Run date: {run_date}  |  Started: {started or '-'}  |  Finished: {finished or '-'}",
        "=" * 72,
    ]
    if status != "OK":
        stage = str(st.get("stage") or status)
        why = STAGE_REASONS.get(stage) or STAGE_REASONS.get(status) or "Pipeline stopped before completion."
        detail = str(st.get("detail") or "").strip()
        lines.append(f"  [REFUSED / FAILED]  Status: {status}  (Stage: {stage})")
        lines.append(f"  Why: {why}")
        if detail:
            lines.append(f"  Detail: {detail}")
    else:
        sups = ((st.get("refresh") or {}).get("suppliers")) or []
        sent_cnt = 0
        would_cnt = 0
        lines.append("  Supplier comparison against last sent PM plan:")
        for s in sups:
            vendor = str(s.get("vendor") or s.get("supplier_key") or "Supplier")
            prev_sent = str(s.get("prev_sent") or "13:40")
            n_chg = int(s.get("changed_cells") or 0)
            would = bool(s.get("would_send"))
            sent = bool(s.get("sent"))
            if dry:
                if would:
                    would_cnt += 1
                    act = "WOULD SEND UPDATED mail (preview only - no mail sent)"
                else:
                    act = "No change - no mail needed"
            else:
                if sent:
                    sent_cnt += 1
                    act = "SENT UPDATED mail"
                elif n_chg == 0:
                    act = "No change - mail skipped"
                else:
                    act = "Changed, not sent (check mail mode/log)"
            lines.append(f"    - {vendor}: {n_chg} changed cell(s) vs {prev_sent} plan  ->  {act}")
        lines.append("-" * 72)
        if dry:
            lines.append(f"  RESULT (PREVIEW): {would_cnt} supplier(s) would receive an UPDATED mail (0 mails sent).")
        else:
            lines.append(f"  RESULT: {sent_cnt} supplier(s) received an UPDATED mail.")
    if log_name:
        lines.append(f"  Log file: logs/{log_name}")
    lines.append("=" * 72)
    return "\n".join(lines)
