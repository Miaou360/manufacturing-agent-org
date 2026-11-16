"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - input readers (read-only, no SAP/WMS/SRM session here)
==================================================================================================
Layouts follow the files the user pastes into the monitoring workbook (02._SRM_Short_Term_Monitoring):
  * SRM "Order Detail"   : 2 header rows, blocks DAY / DAY1 / DAY2 = (ADD on DAY) + 12 two-hour slots + day total
  * SRM "Require Detail" : same blocks without ADD (production requirement per slot)
  * SRM "Plan & Require" : daily, section "require" (production requirement) and section "PLAN" (delivery plan)
  * ZM212 (04_JOB raw export, morning)  : SAP / WMS(SWS) quantity per material and storage location
  * WMS GR List           : barcode receipts with GR Time (local) and Move.Type
Everything is header-driven (no fixed column numbers) because Excel exports move columns around.
"""
from __future__ import annotations

import csv
import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

SLOT_LABELS = ("8-10", "10-12", "12-14", "14-16", "16-18", "18-20", "20-22", "22-24", "24-02", "02-04", "04-06", "06-08")
N_SLOTS = len(SLOT_LABELS)
N_HOURLY_DAYS = 3  # DAY, DAY1, DAY2


# ---------------------------------------------------------------------------- cells
def text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:
            return ""
        return str(int(v)) if v.is_integer() else str(v)
    if isinstance(v, (dt.datetime, dt.date)):
        return f"{v.month}/{v.day}"
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"-?\d+\.0", s) else s


def num(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return 0.0 if v != v else float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def part_key(v: Any) -> str:
    """Join key across SRM / ZM212 / WMS: upper case, leading zeros of the number dropped.
    '099001234-01' -> '99001234-01', 99001234.0 -> '99001234'. Display always keeps the SRM spelling."""
    return re.sub(r"^0+(?=\d)", "", text(v).upper())


def vendor_name(v: Any) -> str:
    """Supplier name for the English reports: a name with non-English characters keeps its English part,
    e.g. '<Korean name> (Example Plant B' -> 'Example Plant B'."""
    s = text(v)
    if not re.search(r"[^\x00-\x7F]", s):
        return s
    m = re.search(r"\(([^()]*[A-Za-z][^()]*)\)?\s*$", s)
    eng = m.group(1) if m else re.sub(r"[^\x00-\x7F]+", " ", s)
    return re.sub(r"\s+", " ", eng).strip(" ()") or s


def _cell(row: List[Any], i: int) -> Any:
    return row[i] if 0 <= i < len(row) else None


# ---------------------------------------------------------------------------- files
def read_rows(path: Any) -> List[List[Any]]:
    """First sheet of .xlsx/.xls/.xlsb or a .csv/.txt (tab or comma) as a list of rows."""
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".csv", ".txt"):
        raw = p.read_bytes()
        text_ = None
        for enc in ("utf-8-sig", "utf-16", "cp949", "latin1"):
            try:
                text_ = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        lines = (text_ or "").splitlines()
        delim = "\t" if sum(l.count("\t") for l in lines[:20]) > sum(l.count(",") for l in lines[:20]) else ","
        return [list(r) for r in csv.reader(lines, delimiter=delim)]
    import pandas as pd

    df = pd.read_excel(p, header=None, dtype=object, sheet_name=0)
    return [[None if (isinstance(v, float) and v != v) else v for v in row] for row in df.values.tolist()]


def _find_header(rows: List[List[Any]], needle: str, limit: int = 15) -> int:
    for i, r in enumerate(rows[:limit]):
        if any(text(c).strip().lower() == needle for c in r):
            return i
    raise ValueError(f"header '{needle}' not found")


def _col(header: List[Any], *names: str) -> int:
    low = [re.sub(r"\s+", " ", text(c)).strip().lower() for c in header]
    for n in names:
        if n.lower() in low:
            return low.index(n.lower())
    raise ValueError(f"column {names} not found")


# ---------------------------------------------------------------------------- SRM hourly (Order / Require Detail)
def parse_hourly(rows: List[List[Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Returns (day_labels[3], {part_key: {part, vendor, name, unit, cls, add, slots[36], totals[3]}})."""
    h = _find_header(rows, "part number")
    h0, h1 = rows[h], rows[h + 1]
    c_part = _col(h0, "part number")
    c_vendor = _col(h0, "Name of partner company.", "name of partner company")
    c_name = _col(h0, "part name", "material name")
    c_unit = _col(h0, "unit")
    c_cls = _col(h0, "classification")
    blocks = []
    for j, c in enumerate(h0):
        if text(c).upper() in ("DAY", "DAY1", "DAY2"):
            has_add = text(_cell(h1, j)).upper() == "ADD"
            start = j + 1 if has_add else j
            cols = list(range(start, start + N_SLOTS))
            labels = [text(_cell(h1, k)) for k in cols]
            if labels != list(SLOT_LABELS):
                raise ValueError(f"unexpected slot labels after {text(c)}: {labels}")
            blocks.append({"add": j if has_add else None, "cols": cols, "total": start + N_SLOTS,
                           "label": text(_cell(h0, start + N_SLOTS))})
    if len(blocks) != N_HOURLY_DAYS:
        raise ValueError(f"expected 3 day blocks (DAY/DAY1/DAY2), found {len(blocks)}")
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows[h + 2:]:
        part = text(_cell(r, c_part))
        if not part:
            continue
        rec = out.setdefault(part_key(part), {
            "part": part, "vendor": vendor_name(_cell(r, c_vendor)), "name": text(_cell(r, c_name)),
            "unit": text(_cell(r, c_unit)), "cls": text(_cell(r, c_cls)),
            "add": 0.0, "slots": [0.0] * (N_SLOTS * N_HOURLY_DAYS), "totals": [0.0] * N_HOURLY_DAYS})
        for d, b in enumerate(blocks):
            if b["add"] is not None:
                rec["add"] += num(_cell(r, b["add"]))
            for s, k in enumerate(b["cols"]):
                rec["slots"][d * N_SLOTS + s] += num(_cell(r, k))
            rec["totals"][d] += num(_cell(r, b["total"]))
    return [b["label"] for b in blocks], out


# ---------------------------------------------------------------------------- SRM daily (Plan & Require)
def _daily_section(h0: List[Any], h1: List[Any], name: str) -> List[int]:
    starts = [j for j, c in enumerate(h0) if text(c).strip()]
    s = _col(h0, name)
    e = min([j for j in starts if j > s] or [max(len(h0), len(h1))])
    sum_cols = [k for k in range(s, e) if text(_cell(h1, k)).upper().endswith("SUM")]
    if not sum_cols:
        raise ValueError(f"no '<date> SUM' column in section {name}")
    days = [sum_cols[0]] + [k for k in range(sum_cols[0] + 1, e)
                            if re.fullmatch(r"\d{1,2}/\d{1,2}", text(_cell(h1, k)))]
    return days


def parse_plan_require(rows: List[List[Any]]) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """Returns (day_labels, {part_key: {part, vendor, name, box_qty, require[n], plan[n]}}).
    'require' = production requirement per day, 'PLAN' = delivery (order) plan per day; day 0 = '<date> SUM'."""
    h = _find_header(rows, "part number")
    h0, h1 = rows[h], rows[h + 1]
    c_part = _col(h0, "part number")
    c_vendor = _col(h0, "Name of partner company.", "name of partner company")
    c_name = _col(h0, "part name")
    c_box = _col(h0, "standard")
    c_cls = _col(h0, "classification")
    req_cols = _daily_section(h0, h1, "require")
    plan_cols = _daily_section(h0, h1, "plan")
    n = min(len(req_cols), len(plan_cols))
    labels = [text(_cell(h1, k)) for k in req_cols[:n]]
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows[h + 2:]:
        part = text(_cell(r, c_part))
        if not part:
            continue
        rec = out.setdefault(part_key(part), {
            "part": part, "vendor": vendor_name(_cell(r, c_vendor)), "name": text(_cell(r, c_name)),
            "box_qty": num(_cell(r, c_box)), "cls": text(_cell(r, c_cls)).upper(),
            "require": [0.0] * n, "plan": [0.0] * n})
        for d in range(n):
            rec["require"][d] += num(_cell(r, req_cols[d]))
            rec["plan"][d] += num(_cell(r, plan_cols[d]))
    return labels, out


# ---------------------------------------------------------------------------- morning stock (ZM212)
def read_zm212_stock(rows: List[List[Any]]) -> Dict[str, Dict[str, float]]:
    """{part_key: {'sap': SAP qty, 'wms': SWS qty}} summed over every storage-location row of the report
    (the workbook's Start Inv = SAP quantity of the morning ZM212, all rows)."""
    h = None
    for i, r in enumerate(rows[:40]):
        if any(text(c).strip() == "Material number" for c in r):
            h = i
            break
    if h is None:
        raise ValueError("ZM212 header 'Material number' not found")
    hdr = [re.sub(r"\s+", " ", text(c)).strip() for c in rows[h]]
    c_mat, c_sws, c_sap = hdr.index("Material number"), hdr.index("SWS Quantity"), hdr.index("SAP Quantity")
    out: Dict[str, Dict[str, float]] = {}
    for r in rows[h + 1:]:
        m = text(_cell(r, c_mat))
        if not m or m == "Material number":
            continue
        rec = out.setdefault(part_key(m), {"sap": 0.0, "wms": 0.0})
        rec["sap"] += num(_cell(r, c_sap))
        rec["wms"] += num(_cell(r, c_sws))
    return out


# ---------------------------------------------------------------------------- WMS GR List
def read_wms_gr(rows: List[List[Any]], move_types: Tuple[str, ...] = ("101",)) -> List[Tuple[str, dt.datetime, float]]:
    """[(part_key, GR time (local), qty)] for receipts with the given movement types."""
    h = _find_header(rows, "mat.no")
    hdr = rows[h]
    c_mat, c_time, c_qty, c_mvt = _col(hdr, "Mat.No"), _col(hdr, "GR Time"), _col(hdr, "GR Qty"), _col(hdr, "Move.Type")
    out = []
    for r in rows[h + 1:]:
        m = text(_cell(r, c_mat))
        if not m or text(_cell(r, c_mvt)) not in move_types:
            continue
        t = _cell(r, c_time)
        if not isinstance(t, dt.datetime):
            try:
                t = dt.datetime.strptime(text(t)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
        out.append((part_key(m), t, num(_cell(r, c_qty))))
    return out
