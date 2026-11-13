"""
D103 Step 1 (164b Verified Rebuild): Backlog Root-Cause Classification & Resolution Attribution Mart Builder
============================================================================================================
Strictly Read-Only across 00, 01, 02, 04, 05 SQLite DBs and raw MB52/WMS/Plan files.
Implements all 9 rules from Thread 169 (164b):
  - Rule 1 & 9: Primary stock & bin location source = 01_JOB exact-time MB52_*.XLS + WMS_StockList_HL_*.xlsx
                (06:00/06:02 for 06:20 runs; 18:00/18:02 for 18:05 run).
                04_JOB used strictly for E (chronic_mismatch_registry) and unit price fallback.
  - Rule 2: 02_JOB per-line Backlog_Resolution_Plan_*.xlsx Status & Reason linked directly as primary anchor.
  - Rule 3: Partial shortage split — when deficit_qty > avail_plant_stock (max(plant_sap, plant_wms)),
            split into covered row (avail_plant_stock) and uncovered excess row (D — 진성 결품 초과 부족분).
  - Rule 4: B+E severity — Primary='B' (and C1, C2, GI hold, COGI save wait) stays TEMPORARY (🟡) even when flag_E=True.
  - Rule 5 & 6: A (Book Discrepancy) assigned ONLY when candidate warehouse has SAP < WMS blocking transfer
                (WAREHOUSE_SAP_SHORT_PERSISTENT). SP31 SAP >= deficit -> OTHER (COGI 저장 대기, 자재).
                Warehouse min(SAP, WMS) >= deficit -> C (C1/C2/C3).
  - Rule 7: C3 3-day rule — applied after Rule 3 excess D split; consec_days >= 3 alone adds badge only;
            C3 confirmed only when >= 1 barcode anomaly signal (실사부족, 랙무변동, FIFO역전, 00_MissingGI) is present.
  - Rule 8: F (Missing GR) — GI_STOCK_GAP_SIGNALS filtered strictly by observed_at <= snapshot_time_edt and
            wms_total > sap_total (excluding 312 ROUNDTRIP_CLEAR).
  - D103-6 Attribution: Exact EDT window filtering on backlog_transfers / transfer_posting_history with
            per-material cap attr_02 = min(reduced_qty, q_02_in_window).
"""

import os
import re
import shutil
import sys
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict, Counter
from dotenv import load_dotenv as _load_dotenv
import openpyxl
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CODES_ROOT = Path.home() / "Desktop" / "CODES"
for _ep in [CODES_ROOT / "01_M365MCP", CODES_ROOT, *Path(__file__).resolve().parents]:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        break

SAP_PLANT = os.getenv("SAP_PLANT", "P001").strip() or "P001"
INV_DIR = CODES_ROOT / "01_M365MCP/01_DEPT_Inventory"
WT_JOB03_DIR = Path(__file__).resolve().parent
WT_INV_DIR = WT_JOB03_DIR.parent
WT_HQ_DIR = WT_INV_DIR.parent / "00_HQ_TaskManager"
for _p in (WT_INV_DIR, WT_HQ_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ztcode_stock_guard import expand_gr_batch_paths, extract_active_gr_error_map, gi_flow
from gi_verdict_shared import (
    evaluate_gi_errors_as_of,
    parse_ztcode_stock_rec_gi_records_detailed, parse_ztcode_stock_rec_gr_records_detailed,
    resolve_d108_for_backlog_item,
)

OUT_DIR = WT_JOB03_DIR / "scratch"
OUT_DIR.mkdir(parents=True, exist_ok=True)
MAIN_OUT_DIR = INV_DIR / "03_JOB_Backlog_Analysis/scratch"
MAIN_OUT_DIR.mkdir(parents=True, exist_ok=True)

EDT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

WMS_TO_SAP_SLOC_MAP = {
    "RECEIPT GOODS SL": "SL00",
    "MAIN PARTS": "SL01",
    "CKD": "SL02",
    "RAW MATERIAL": "SL03",
    "PASS THROUGH": "SL04",
    "DMA": "SL49",
}

JOB00_WMS = INV_DIR / "00_JOB_StockAccuracy/wms_daily_stack"
JOB01_MB52 = INV_DIR / "01_JOB_GR_GI_Error/mb52"
JOB01_WMS = INV_DIR / "01_JOB_GR_GI_Error/wms_parts_stack"
JOB01_GI_ERR = INV_DIR / "01_JOB_GR_GI_Error/gi_errors"
JOB01_GR_ERR = INV_DIR / "01_JOB_GR_GI_Error/gr_errors"
JOB01_PLANS = INV_DIR / "01_JOB_GR_GI_Error/resolution_logs"
JOB02_DIR = INV_DIR / "02_JOB_Backlog_Clear"
OPS_RAW_DB_CANDIDATES = [
    INV_DIR / "06_JOB_Data_Collector/db/ops_raw.db",
    # Canonical operational ops_raw.db only (#194: never use worktree copy)
]


def resolve_ops_raw_db_path(explicit_path: Path | None = None) -> Path | None:
    if explicit_path and explicit_path.exists():
        return explicit_path
    for cand in OPS_RAW_DB_CANDIDATES:
        if cand.exists():
            return cand
    return None


def aggregate_mb51_window_for_material(
    mb51_rows: list[dict],
    target_slocs: set[str],
    auto_doc_nos: set[str],
    per_sloc_cap: dict[str, float] | None = None,
    t0_edt: str | None = None,
    t1_edt: str | None = None,
) -> dict:
    """
    Aggregates MB51 rows inside [t0_edt, t1_edt] for a single material into
    mutually exclusive movement buckets (#187 B-5 tightened):
      - mb51_311_manual: STRICTLY mvt 311 (net of mvt 312 reversals) where:
          1) arrival SLoc (`sloc` on `+` leg) ∈ item's backlog COGI SLocs in THAT transition
          2) `counter_sloc != sloc` (excludes intra-SLoc postings; mvt 411 consignment excluded!)
          3) `entry_datetime_edt <= t1_edt` (and `>= t0_edt` when timestamps provided)
          4) excludes `01_AUTO` / `02_AUTO` SAP documents and headers.
          When `per_sloc_cap` (`{sloc: reduced_rem_qty}`) is provided, manual 311 net inflow is
          also capped per individual reduced COGI SLoc so a `->SP31` transfer never absorbs an `SL49` reduction.
      - mb51_411_consign: informational mvt 411/412 consignment-to-own conversion in target SLoc.
      - mb51_101_131_gr: positive GR inflow on 101/131/953 (net of 102/132/122/954 reversals in window).
      - mb51_scrap: net scrap/disposal outflow on 551/552/553/554/593/594/907/908.
      - mb51_261_gi: order GI outflow (-qty_entry > 0) on 261/929.
      - mb51_262_re: order GI reversal (+qty_entry > 0) on 262/930.
    """
    net_311_by_sloc: dict[str, float] = defaultdict(float)
    q_411_net = 0.0
    q_gr_net = 0.0
    q_scrap_net = 0.0
    q_261_gi = 0.0
    q_262_re = 0.0
    manual_311_docs = set()
    manual_311_users = set()
    manual_311_raw_docs = []

    eff_slocs = {str(s).strip().upper() for s in target_slocs if str(s).strip()} if target_slocs else {"SP31", "SP11", "SL49"}

    seen_doc_item_keys = set()
    for r in mb51_rows:
        doc_no_dedup = str(r.get("mat_doc_no") or "").strip()
        doc_item_dedup = str(r.get("mat_doc_item") or "").strip()
        doc_yr_dedup = str(r.get("mat_doc_year") or "").strip()
        if doc_no_dedup and doc_item_dedup:
            dedup_k = (doc_no_dedup, doc_item_dedup, doc_yr_dedup)
            if dedup_k in seen_doc_item_keys:
                continue
            seen_doc_item_keys.add(dedup_k)
        r_edt = str(r.get("entry_datetime_edt") or "").strip()
        if t0_edt and r_edt and r_edt < t0_edt:
            continue
        if t1_edt and r_edt and r_edt > t1_edt:
            continue

        mvt = str(r.get("mvt_type") or "").strip()
        sloc = str(r.get("sloc") or "").strip().upper()
        c_sloc = str(r.get("counter_sloc") or "").strip().upper()
        doc_no = str(r.get("mat_doc_no") or "").strip()
        hdr_txt = str(r.get("doc_header_text") or "").strip()
        qty = float(r.get("qty_entry") or 0.0)

        if mvt in ("311", "312"):
            if (
                sloc in eff_slocs
                and (not c_sloc or c_sloc != sloc)
                and doc_no not in auto_doc_nos
                and hdr_txt not in ("Backlog_Clear", "GI_Error_Clear")
            ):
                # Count arrival leg (+qty on 311) into backlog SLoc, and deduct reversal/outflow (-qty on 312 or intra-backlog outflow)
                if mvt == "311" and qty > 0:
                    if per_sloc_cap is None and c_sloc and c_sloc in eff_slocs:
                        continue
                    net_311_by_sloc[sloc] += qty
                    if doc_no:
                        manual_311_docs.add(doc_no)
                    u_s = str(r.get("username") or "").strip()
                    if u_s:
                        manual_311_users.add(u_s)
                    manual_311_raw_docs.append({
                        "mat_doc_no": doc_no,
                        "mat_doc_item": str(r.get("mat_doc_item") or "-").strip() or "-",
                        "mvt_type": mvt,
                        "from_sloc": c_sloc or "?",
                        "to_sloc": sloc,
                        "qty": round(qty, 2),
                        "cpudt_kst": f"{r.get('entry_date') or ''} {r.get('entry_time') or ''}".strip(),
                        "entry_datetime_edt": r_edt,
                        "vs_obs_end": "BEFORE_OR_AT_OBS (<=t1)" if (not t1_edt or not r_edt or r_edt <= t1_edt) else "AFTER_OBS (>t1)",
                    })
                elif mvt == "311" and qty < 0 and per_sloc_cap is not None and c_sloc in eff_slocs:
                    # Outflow from one backlog SLoc to another backlog SLoc reduces source SLoc's net manual 311
                    net_311_by_sloc[sloc] += qty
                elif mvt == "312" and qty < 0:
                    net_311_by_sloc[sloc] += qty
                    manual_311_raw_docs.append({
                        "mat_doc_no": doc_no,
                        "mat_doc_item": str(r.get("mat_doc_item") or "-").strip() or "-",
                        "mvt_type": mvt,
                        "from_sloc": c_sloc or "?",
                        "to_sloc": sloc,
                        "qty": round(qty, 2),
                        "cpudt_kst": f"{r.get('entry_date') or ''} {r.get('entry_time') or ''}".strip(),
                        "entry_datetime_edt": r_edt,
                        "vs_obs_end": "BEFORE_OR_AT_OBS (<=t1)" if (not t1_edt or not r_edt or r_edt <= t1_edt) else "AFTER_OBS (>t1)",
                    })
        elif mvt in ("411", "412"):
            if sloc in eff_slocs:
                if mvt == "411" and qty > 0:
                    q_411_net += qty
                elif mvt == "412" and qty < 0:
                    q_411_net += qty
        elif mvt in ("101", "131", "953"):
            if qty > 0:
                q_gr_net += qty
        elif mvt in ("102", "122", "132", "954"):
            # Reversal reduces net GR in the same window
            q_gr_net += qty  # qty is negative
        elif mvt in ("551", "553", "593", "907"):
            # Scrap outflow has qty_entry < 0
            q_scrap_net += (-qty if qty < 0 else qty)
        elif mvt in ("552", "554", "594", "908"):
            # Scrap reversal cancels scrap outflow
            q_scrap_net -= abs(qty)
        elif mvt in ("261", "929"):
            if qty < 0:
                q_261_gi += (-qty)
        elif mvt in ("262", "930"):
            if qty > 0:
                q_262_re += qty

    raw_311_unclipped = round(sum(max(0.0, v) for v in net_311_by_sloc.values()), 2)
    if per_sloc_cap is not None:
        q_311_eff = round(
            sum(min(max(0.0, float(cap_q)), max(0.0, net_311_by_sloc.get(s_k, 0.0))) for s_k, cap_q in per_sloc_cap.items()),
            2,
        )
    else:
        q_311_eff = raw_311_unclipped

    q_gr_clean = round(max(0.0, q_gr_net), 2)
    q_scrap_clean = round(max(0.0, q_scrap_net), 2)
    q_261_total = round(q_261_gi + q_262_re, 2)

    return {
        "raw_mb51_311_manual_qty": q_311_eff,
        "raw_mb51_311_unclipped_qty": raw_311_unclipped,
        "raw_mb51_411_consign_qty": round(max(0.0, q_411_net), 2),
        "raw_mb51_101_131_gr_qty": q_gr_clean,
        "raw_mb51_scrap_qty": q_scrap_clean,
        "raw_mb51_261_gi_qty": round(q_261_gi, 2),
        "raw_mb51_262_re_qty": round(q_262_re, 2),
        "raw_mb51_261_total_qty": q_261_total,
        "manual_311_doc_cnt": len(manual_311_docs),
        "manual_311_users": ",".join(sorted(manual_311_users)),
        "manual_311_raw_docs": manual_311_raw_docs,
    }


def decompose_unknown_with_mb51(rem_after_gr: float, attr_gr: float, mb51_stats: dict) -> dict:
    """
    Decomposes `rem_after_gr` (formerly lumped as `UNKNOWN`) into mutually exclusive
    capped buckets so sum of all attributed categories == reduced_qty (100% conservation):
      1. MB51_311_MANUAL: manual 311/312/etc. positive inflow into target COGI SLoc
      2. MB51_101_131_GR: 101/131/953 GR in window (net of attr_gr already attributed via GR_POSTED)
      3. MB51_551_553_593_SCRAP: 551/553/593/907 scrap/disposal posting
      4. MB51_261_CONSUMED: 261 GI / 262 RE posting (COGI flush of pre-existing SLoc stock)
      5. UNKNOWN_RESIDUAL: no matching MB51 movement in window (COGI deletion or uncovered window)
    """
    rem = round(max(0.0, float(rem_after_gr)), 2)
    if rem <= 0:
        return {
            "attr_mb51_311_manual": 0.0,
            "attr_mb51_101_131_gr": 0.0,
            "attr_mb51_scrap": 0.0,
            "attr_mb51_261_consumed": 0.0,
            "attr_unknown_residual": 0.0,
        }

    c_311 = round(min(rem, float(mb51_stats.get("raw_mb51_311_manual_qty", 0.0))), 2)
    rem = round(rem - c_311, 2)

    avail_gr = round(max(0.0, float(mb51_stats.get("raw_mb51_101_131_gr_qty", 0.0)) - float(attr_gr)), 2)
    c_gr = round(min(rem, avail_gr), 2)
    rem = round(rem - c_gr, 2)

    c_scrap = round(min(rem, float(mb51_stats.get("raw_mb51_scrap_qty", 0.0))), 2)
    rem = round(rem - c_scrap, 2)

    c_261 = round(min(rem, float(mb51_stats.get("raw_mb51_261_total_qty", 0.0))), 2)
    rem = round(rem - c_261, 2)

    return {
        "attr_mb51_311_manual": c_311,
        "attr_mb51_101_131_gr": c_gr,
        "attr_mb51_scrap": c_scrap,
        "attr_mb51_261_consumed": c_261,
        "attr_unknown_residual": rem,
    }

def map_d108_verdict_to_03_cause(v_match: dict | None) -> dict | None:
    """
    Maps a shared `gi_verdict_shared.py` verdict dict into `03` D103 mart root-cause fields (#183, #184, #187 B-2).
    When `v_match is None` or `d108_code == 'GI_LINK_GONE'` (no active linked GI error exists at snapshot time),
    returns `None` so `03` falls through to normal non-GI root-cause classification (`B/R/F/A/C/D`).
    """
    if v_match is None:
        return None

    code = str(v_match.get("d108_code") or "D").strip()
    if code == "GI_LINK_GONE":
        return None
    d107_st = str(v_match.get("d107_status") or "EXCLUDED").strip()
    gi_lnk = str(v_match.get("gi_link_summary") or "-").strip()
    st_01 = str(v_match.get("gi_01_status") or "NONE").strip()
    comp = v_match.get("gi_01_companion_statuses") or []
    st_01_full = f"{st_01}(+{','.join(comp)})" if comp else st_01
    is_bug = bool(v_match.get("is_bug_candidate"))

    if code == "D":
        primary = "D"
        detailed_code = "D"
        other_sub = None
        d_sub = (
            "D. 진성 결품 (D108 GI연계 — D107 삭제 후보)"
            if d107_st == "CANDIDATE"
            else "D. 진성 결품 (D108 GI연계 재고 부족)"
        )
        sev = "CRITICAL (🔴)"
    elif code == "A":
        primary = "A"
        detailed_code = "A"
        other_sub = None
        d_sub = "A. 장부 불일치 (보충 창고 SAP < WMS)"
        sev = "CRITICAL (🔴)"
    elif code == "F":
        primary = "F"
        detailed_code = "F"
        other_sub = None
        d_sub = "F. 입고 누락 (Missing GR)"
        sev = "CRITICAL (🔴)"
    elif code == "B":
        primary = "B"
        detailed_code = "B"
        other_sub = None
        d_sub = "B. GR 미처리 대기"
        sev = "TEMPORARY (🟡)"
    elif code == "ANOMALY_NEW_AFTER_01":
        primary = "OTHER"
        detailed_code = "OTHER_GI_NEW_1"
        other_sub = "OTHER — GI 에러 (① 01 수집 후 신규·다음 회차 대기)"
        d_sub = other_sub
        sev = "TEMPORARY (🟡)"
    elif code == "ANOMALY_GUARD_UNPOSTED":
        primary = "OTHER"
        detailed_code = "OTHER_GI_GUARD_2"
        other_sub = "OTHER — GI 이상 점검 (② 01 처리가능 판정·가드 미전기)"
        d_sub = other_sub
        sev = "CRITICAL (🔴)"
    elif code in ("ANOMALY_POSTING_FAILED", "ANOMALY_POSTED_BUT_REMAINED"):
        primary = "OTHER"
        detailed_code = "OTHER_GI_FAIL_3"
        other_sub = "OTHER — GI 이상 점검 (③ 01 전기 실패·잔류 버그 후보)"
        d_sub = other_sub
        sev = "CRITICAL (🔴)"
    else:
        primary = "OTHER"
        detailed_code = "OTHER_GI_EXCL"
        other_sub = f"OTHER — GI 제외 유형 (mvt {v_match.get('mvt') or '?'})"
        d_sub = other_sub
        sev = "OTHER (⚪)"

    d107_tag = "|D107삭제후보" if d107_st == "CANDIDATE" else ""
    bug_tag = "|버그점검후보" if is_bug else ""
    badge = f"D108_GI연계[{code}:{gi_lnk}|01:{st_01_full}{d107_tag}{bug_tag}]"
    ev_str = f"D108[{code} | {gi_lnk} | 01({v_match.get('gi_01_plan_time') or '-'}):{st_01_full} | {v_match.get('d108_detail') or ''}]"

    return {
        "d108_code": code,
        "primary": primary,
        "detailed_code": detailed_code,
        "other_sub_name": other_sub,
        "d_sub_note": d_sub,
        "severity": sev,
        "is_bug_candidate": is_bug,
        "d107_status": d107_st,
        "d108_badge": badge,
        "d108_evidence": ev_str,
        "gi_link_summary": gi_lnk,
        "gi_01_verdict": st_01_full,
    }


def _find_gi_error_file_le(gi_dir: Path, snap_edt_str: str) -> Path | None:
    if not gi_dir.exists():
        return None
    try:
        anchor_dt = datetime.strptime(snap_edt_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    cands = []
    for p in gi_dir.glob("GI_Error_*.XLS*"):
        m = re.search(r"(\d{8})_(\d{6})", p.name)
        if m:
            try:
                fts = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
                if fts <= (anchor_dt + timedelta(seconds=30)) and fts.date() == anchor_dt.date():
                    cands.append((fts, p))
            except ValueError:
                pass
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1] if cands else None


def _find_gr_error_file_le(gr_dir: Path, snap_edt_str: str) -> Path | None:
    if not gr_dir.exists():
        return None
    try:
        anchor_dt = datetime.strptime(snap_edt_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    cands = []
    for p in gr_dir.glob("GR_*.XLS*"):
        m = re.search(r"(\d{8})_(\d{6})", p.name)
        if m:
            try:
                fts = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
                if fts <= (anchor_dt + timedelta(seconds=60)) and fts.date() == anchor_dt.date():
                    cands.append((fts, p))
            except ValueError:
                pass
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1] if cands else None


SNAPSHOT_FILE_MAP = {
    "2026-10-23": {
        "snap_edt": "2026-10-23 08:41:28",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261023_084136.XLS",
        "wms": JOB00_WMS / "WMS_StockList_HL_20261023_060500.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261023_095458.xlsx",
    },
    "2026-10-25": {
        "snap_edt": "2026-10-25 11:14:21",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261025_103041.XLS",
        "wms": JOB00_WMS / "WMS_StockList_HL_20261025_104231.xlsx",
        "plan": JOB02_DIR / "scratch/archive_test_plans/Backlog_Resolution_Plan_20261025_111504.xlsx",
    },
    "2026-10-26": {
        "snap_edt": "2026-10-26 06:10:17",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261026_060040.XLS",
        "wms": JOB00_WMS / "WMS_StockList_HL_20261026_040130.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261026_061057.xlsx",
    },
    "2026-10-27": {
        "snap_edt": "2026-10-27 06:20:32",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261027_060041.XLS",
        "wms": JOB01_WMS / "WMS_StockList_HL_20261027_060142.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261027_062357.xlsx",
    },
    "2026-10-28": {
        "snap_edt": "2026-10-28 06:20:30",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261028_060059.XLS",
        "wms": JOB01_WMS / "WMS_StockList_HL_20261028_060210.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261028_062401.xlsx",
    },
    "2026-10-28_1805": {
        "snap_edt": "2026-10-28 18:05:50",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261028_180054.XLS",
        "wms": JOB01_WMS / "WMS_StockList_HL_20261028_180208.xlsx",
        "plan": None,
    },
    "2026-10-29": {
        "snap_edt": "2026-10-29 06:23:28",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261029_062114.XLS",
        "wms": JOB01_WMS / "WMS_StockList_HL_20261029_062213.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261029_062328.xlsx",
    },
    "2026-10-29_0815": {
        "snap_edt": "2026-10-29 08:17:32",
        "mb52": JOB01_MB52 / f"MB52_{SAP_PLANT}_20261029_081525.XLS",
        "wms": JOB01_WMS / "WMS_StockList_HL_20261029_081624.xlsx",
        "plan": JOB02_DIR / "resolution_logs/Backlog_Resolution_Plan_20261029_081732.xlsx",
    },
}


def utc_to_edt_str(ts_val) -> str:
    if not ts_val:
        return ""
    s = str(ts_val).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(EDT).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return s


def utc_to_edt_date(ts_val) -> str:
    edt_s = utc_to_edt_str(ts_val)
    return edt_s[:10] if len(edt_s) >= 10 else ""




SP31_01_ACCURACY_LINK_TAG = "01 정확도 이동 연계"
SP31_01_ACCURACY_WINDOW_HOURS = 72
WAREHOUSE_BALANCE_SLOCS_199 = {"SL00", "SL01", "SL02", "SL03", "SL04"}


def _verify_doc_in_mb51(
    c06: sqlite3.Connection | None,
    doc_no: str,
    mat: str,
    from_sloc: str,
    to_sloc: str,
    qty: float,
    edt_ts: str,
    max_mb51_edt: str,
) -> str:
    """
    #200: Cross-checks `transfer_posting_history.doc_number` against `ops_raw.db.mb51_raw`
    strictly for verification/warning. Never overwrites `edt_ts`, `from_sloc`, `to_sloc`, or `qty`.
    """
    if c06 is None:
        return "N/A"
    if not doc_no:
        return "WARN: 전표번호 누락"
    try:
        m_rows = c06.execute(
            """
            SELECT material, sloc, counter_sloc, mvt_type, qty_entry
            FROM mb51_raw
            WHERE mat_doc_no = ?
            """,
            (doc_no,),
        ).fetchall()
    except Exception:
        return "N/A"

    if not m_rows:
        if max_mb51_edt and edt_ts > max_mb51_edt:
            return f"UNCOLLECTED({doc_no}, >{max_mb51_edt[:16]})"
        return f"WARN: MB51 전표 미발견({doc_no})"

    has_src_leg = False
    has_dst_leg = False
    observed_slocs = []
    for mr in m_rows:
        m_mat = str(mr[0] or "").strip()
        m_sloc = str(mr[1] or "").strip().upper()
        m_q = float(mr[4] or 0.0)
        observed_slocs.append(f"{m_sloc}:{m_q:g}")
        if m_mat == mat and m_sloc == from_sloc and abs(m_q + qty) <= 0.01:
            has_src_leg = True
        if m_mat == mat and m_sloc == to_sloc and abs(m_q - qty) <= 0.01:
            has_dst_leg = True

    if has_src_leg and has_dst_leg:
        return f"OK({doc_no})"
    return f"WARN: MB51 불일치({doc_no}: {', '.join(observed_slocs)})"


def load_01_sp31_accuracy_transfers(
    c01: sqlite3.Connection,
    c06: sqlite3.Connection | None = None,
) -> list[dict]:
    """
    #199 (1-B) & #200: Loads `01_JOB` `POSTED` `SP31 -> SL00..SL04` warehouse-accuracy transfers
    directly from `transfer_posting_history` (strictly read-only):
      - `edt_ts` (`executed_at` UTC -> EDT), `from_sloc`, `to_sloc`, `transfer_qty`, `doc_number`
        are preserved verbatim from `transfer_posting_history`.
      - `mb51_raw` (`c06`) is used ONLY to verify `doc_number` consistency (`mb51_doc_check`),
        never for timestamps or SLoc routing.
      - Filters `batch_id LIKE 'RUN_%'` (excludes DRYRUN/TEST and one-off CORR_/COMP_ batches).
      - Excludes `ROUNDTRIP_CLEAR` and `READY_FOR_EXECUTION` zero-drain roundtrips where an active
        same-batch `gi_error_snapshots` row returned `>= transfer_qty` from `to_sloc -> SP31`
        in the same cycle (net SP31 book decrease == 0).
    """
    max_mb51_edt = ""
    if c06 is not None:
        try:
            mx_row = c06.execute("SELECT MAX(entry_datetime_edt) FROM mb51_raw").fetchone()
            max_mb51_edt = str(mx_row[0] or "").strip() if mx_row else ""
        except Exception:
            max_mb51_edt = ""

    rows = c01.execute(
        """
        SELECT id, batch_id, executed_at, material, from_sloc, to_sloc,
               transfer_qty, doc_number, post_status, posting_type, reason
        FROM transfer_posting_history
        WHERE post_status = 'POSTED'
          AND batch_id LIKE 'RUN_%'
          AND from_sloc = 'SP31'
          AND to_sloc IN ('SL00', 'SL01', 'SL02', 'SL03', 'SL04')
          AND COALESCE(posting_type, '') != 'ROUNDTRIP_CLEAR'
        ORDER BY id ASC
        """
    ).fetchall()

    out = []
    for r in rows:
        d = dict(r)
        bid = str(d.get("batch_id") or "").strip()
        mat = str(d.get("material") or "").strip()
        f_sloc = str(d.get("from_sloc") or "").strip().upper()
        t_sloc = str(d.get("to_sloc") or "").strip().upper()
        q = float(d.get("transfer_qty") or 0.0)
        p_type = str(d.get("posting_type") or "").strip().upper()
        if q <= 0 or not mat or f_sloc != "SP31" or t_sloc not in WAREHOUSE_BALANCE_SLOCS_199:
            continue

        if p_type == "READY_FOR_EXECUTION" and bid:
            try:
                gi_snaps = c01.execute(
                    "SELECT mvt, from_sloc, to_sloc, qty FROM gi_error_snapshots WHERE batch_id = ? AND material = ?",
                    (bid, mat),
                ).fetchall()
            except Exception:
                gi_snaps = []
            ret_qty = 0.0
            for gs in gi_snaps:
                mv = str(gs[0] or "").replace(".0", "").strip()
                fs = str(gs[1] or "").strip().upper()
                ts = str(gs[2] or "").strip().upper()
                sf, df_loc = gi_flow(mv, fs, ts)
                src_eff = sf or fs
                dst_eff = df_loc or ts
                if src_eff == t_sloc and dst_eff == "SP31":
                    ret_qty += float(gs[3] or 0.0)
            if ret_qty + 1e-6 >= q:
                # Same-cycle roundtrip returned >= q back to SP31 (zero SP31 book drain)
                continue

        edt_s = utc_to_edt_str(d.get("executed_at"))
        if not edt_s:
            continue
        doc_no = str(d.get("doc_number") or "").strip()
        q_rnd = round(q, 2)
        mb51_chk = _verify_doc_in_mb51(
            c06=c06,
            doc_no=doc_no,
            mat=mat,
            from_sloc=f_sloc,
            to_sloc=t_sloc,
            qty=q_rnd,
            edt_ts=edt_s,
            max_mb51_edt=max_mb51_edt,
        )
        out.append(
            {
                "id": d.get("id"),
                "batch_id": bid,
                "edt_ts": edt_s,
                "material": mat,
                "from_sloc": f_sloc,
                "to_sloc": t_sloc,
                "transfer_qty": q_rnd,
                "doc_number": doc_no,
                "posting_type": p_type or "READY_FOR_EXECUTION",
                "mb51_doc_check": mb51_chk,
            }
        )
    return out


def match_sp31_01_accuracy_link_72h(
    material: str,
    slocs_str: str,
    snap_edt_str: str,
    sp31_acc_transfers: list[dict],
    window_hours: int = SP31_01_ACCURACY_WINDOW_HOURS,
) -> dict:
    """
    #199 (1-B) & #200: For an `SP31` backlog row as of `snap_edt_str`, checks whether `01_JOB` posted
    a net `SP31 -> SL00..SL04` warehouse-accuracy transfer for the same material within the
    preceding `window_hours` (default 72h: `[snap_dt - 72h, snap_dt]`), evaluated strictly on
    `transfer_posting_history.executed_at` (EDT).
    Returns a reference-only dict (`tag`, `qty_72h`, `doc_count`, `detail`, `mb51_doc_check`, `hits`)
    without altering any root-cause code or severity group.
    """
    sloc_tokens = {s.strip().upper() for s in str(slocs_str or "").split(",") if s.strip()}
    if "SP31" not in sloc_tokens or not snap_edt_str or not sp31_acc_transfers:
        return {"tag": "-", "qty_72h": 0.0, "doc_count": 0, "detail": "-", "mb51_doc_check": "-", "hits": []}

    try:
        snap_dt = datetime.strptime(str(snap_edt_str).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return {"tag": "-", "qty_72h": 0.0, "doc_count": 0, "detail": "-", "mb51_doc_check": "-", "hits": []}

    win_start = snap_dt - timedelta(hours=float(window_hours))
    mat_s = str(material or "").strip()
    hits = []
    for tr in sp31_acc_transfers:
        if str(tr.get("material") or "").strip() != mat_s:
            continue
        try:
            tr_dt = datetime.strptime(str(tr.get("edt_ts") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if win_start <= tr_dt <= snap_dt:
            hits.append(tr)

    if not hits:
        return {"tag": "-", "qty_72h": 0.0, "doc_count": 0, "detail": "-", "mb51_doc_check": "-", "hits": []}

    tot_q = round(sum(float(h.get("transfer_qty") or 0.0) for h in hits), 2)
    parts = [
        f"{h['edt_ts']} {h['from_sloc']}->{h['to_sloc']} {float(h['transfer_qty']):g}EA(전표 {h['doc_number']}, {h['posting_type']})"
        for h in hits
    ]
    chk_parts = [str(h.get("mb51_doc_check") or "N/A") for h in hits]
    return {
        "tag": SP31_01_ACCURACY_LINK_TAG,
        "qty_72h": tot_q,
        "doc_count": len(hits),
        "detail": "; ".join(parts),
        "mb51_doc_check": "; ".join(chk_parts),
        "hits": hits,
    }


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def parse_mb52_file(path: Path):
    """Parses SAP UTF-16 TSV MB52 file. Returns (by_mat_sloc, price_by_mat, desc_by_mat)."""
    by_mat_sloc = defaultdict(float)
    price_by_mat = {}
    desc_by_mat = {}
    with open(path, "r", encoding="utf-16", errors="replace") as f:
        for line in f:
            cols = [c.strip() for c in line.rstrip("\r\n").split("\t")]
            if len(cols) >= 15 and cols[2] == SAP_PLANT:
                sloc = cols[4].strip().upper()
                mat = cols[5].strip()
                desc = cols[6].strip()
                if desc and mat not in desc_by_mat:
                    desc_by_mat[mat] = desc
                unres_s = cols[-8].replace(",", "").strip()
                val_s = cols[-7].replace(",", "").strip()
                try:
                    unres = -float(unres_s[:-1]) if unres_s.endswith("-") else float(unres_s)
                except Exception:
                    unres = 0.0
                try:
                    val = -float(val_s[:-1]) if val_s.endswith("-") else float(val_s)
                except Exception:
                    val = 0.0
                by_mat_sloc[(mat, sloc)] += unres
                if unres > 0 and val > 0:
                    price_by_mat[mat] = val / unres
    return by_mat_sloc, price_by_mat, desc_by_mat


def parse_wms_file(path: Path, target_mats: set):
    """Parses WMS_StockList_HL_*.xlsx for target materials. Returns (by_mat_sloc, bins_by_mat)."""
    by_mat_sloc = defaultdict(float)
    bins_by_mat = defaultdict(list)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not r or len(r) < 9:
            continue
        mat = str(r[1] or "").strip()
        if mat not in target_mats:
            continue
        gi_loc = str(r[3] or "").strip().upper()
        sloc = WMS_TO_SAP_SLOC_MAP.get(gi_loc)
        if not sloc:
            continue
        try:
            q = float(r[5] or 0.0)
        except Exception:
            q = 0.0
        bin_code = str(r[8] or "").strip().upper()
        gr_date = str(r[7] or "").strip()
        by_mat_sloc[(mat, sloc)] += q
        bins_by_mat[mat].append({"sloc": sloc, "bin": bin_code, "qty": q, "gr_date": gr_date})
    wb.close()
    return by_mat_sloc, bins_by_mat


def parse_plan_file(path: Path):
    """Parses 02_JOB Backlog_Resolution_Plan_*.xlsx. Returns list of row dicts."""
    if not path or not path.exists():
        return []
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    wb.close()
    if not rows:
        return []
    hdr = [str(c or "").strip() for c in rows[0]]
    out = []
    for r in rows[1:]:
        d = dict(zip(hdr, r))
        if d.get("Material"):
            out.append(d)
    return out


def _load_line_floor_prefixes() -> tuple[str, ...]:
    raw = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "") or os.getenv("OEM_FLOOR_BIN_PREFIX", "OEM")
    extra = tuple(t.strip().upper() for t in re.split(r"[,;]", raw) if t.strip())
    base = ["CRT", "PRO", "SMT", "OEM"]
    for tok in extra:
        if tok not in base:
            base.append(tok)
    return tuple(base)

_LINE_FLOOR_PREFIXES = _load_line_floor_prefixes()


def is_line_supply_bin(sloc: str, bin_code: str) -> bool:
    """True if bin is in SL01 R-bin or line supply zone (SL01 R*, CRT*, PRO*, SMT*, OEM*)."""
    b = str(bin_code or "").strip().upper()
    s = str(sloc or "").strip().upper()
    if b.startswith(_LINE_FLOOR_PREFIXES):
        return True
    if s == "SL01" and b.startswith("R"):
        return True
    return False


def build_d103_step1(ops_raw_db_path: Path | None = None):
    c00 = connect_ro(INV_DIR / "00_JOB_StockAccuracy/db/inventory_history.db")
    c01 = connect_ro(INV_DIR / "01_JOB_GR_GI_Error/db/gr_gi_history.db")
    c02 = connect_ro(INV_DIR / "02_JOB_Backlog_Clear/db/backlog_history.db")
    c04 = connect_ro(INV_DIR / "04_JOB_SystemReconciliation/db/system_reconciliation.db")
    c05 = connect_ro(INV_DIR / "05_JOB_SL01_R_Bins/db/sl01_r_bins.db")
    resolved_ops_db = resolve_ops_raw_db_path(ops_raw_db_path)
    c06 = connect_ro(resolved_ops_db) if resolved_ops_db else None

    # =========================================================================
    # DELIVERABLE 1: DB Timestamp Column Basis Table (D103-1)
    # =========================================================================
    ts_audit_rows = [
        {"job": "02_Backlog_Clear", "db": "backlog_history.db", "table": "raw_backlog_history", "column": "run_date", "stored_tz": "EDT (Local Date)", "sample_raw": "2026-10-28", "sample_edt": "2026-10-28", "conversion_rule": "Already America/New_York business date"},
        {"job": "02_Backlog_Clear", "db": "backlog_history.db", "table": "raw_backlog_history", "column": "extracted_at / created_at", "stored_tz": "UTC (SQLite CURRENT_TIMESTAMP)", "sample_raw": "2026-10-28 10:21:28", "sample_edt": "2026-10-28 06:21:28 EDT", "conversion_rule": "UTC -> America/New_York (-04:00 EDT / -05:00 EST)"},
        {"job": "02_Backlog_Clear", "db": "backlog_history.db", "table": "cogi_snapshots", "column": "extracted_at / created_at", "stored_tz": "UTC (SQLite CURRENT_TIMESTAMP)", "sample_raw": "2026-10-28 12:15:27", "sample_edt": "2026-10-28 08:15:27 EDT", "conversion_rule": "UTC -> America/New_York; snapshot_id suffix has local HHMMSS"},
        {"job": "02_Backlog_Clear", "db": "backlog_history.db", "table": "backlog_transfers", "column": "executed_at / created_at", "stored_tz": "UTC (SQLite CURRENT_TIMESTAMP)", "sample_raw": "2026-10-28 10:28:42", "sample_edt": "2026-10-28 06:28:42 EDT", "conversion_rule": "UTC -> America/New_York; filter transitions by exact EDT window"},
        {"job": "02_Backlog_Clear", "db": "backlog_history.db", "table": "daily_backlog_metrics", "column": "email_sent_at vs created_at", "stored_tz": "email_sent_at=EDT (ISO) / created_at=UTC", "sample_raw": "2026-10-28T08:19:46 / 2026-10-28 12:19:46", "sample_edt": "2026-10-28 08:19:46 EDT", "conversion_rule": "email_sent_at is Python datetime.now().isoformat() (EDT); created_at is UTC"},
        {"job": "01_GR_GI_Error", "db": "gr_gi_history.db", "table": "gi_error_snapshots / gr_error_snapshots", "column": "extracted_at / batch_id", "stored_tz": "extracted_at=UTC / batch_id=EDT", "sample_raw": "2026-10-28 23:00:52 / RUN_20261028_190000", "sample_edt": "2026-10-28 19:00:52 EDT", "conversion_rule": "UTC -> America/New_York; batch_id has local EDT timestamp"},
        {"job": "01_GR_GI_Error", "db": "gr_gi_history.db", "table": "transfer_posting_history", "column": "executed_at", "stored_tz": "UTC (SQLite CURRENT_TIMESTAMP)", "sample_raw": "2026-10-28 22:04:39", "sample_edt": "2026-10-28 18:04:39 EDT", "conversion_rule": "UTC -> America/New_York; batch_id has local EDT timestamp"},
        {"job": "01_GR_GI_Error", "db": "gr_gi_history.db", "table": "GI_STOCK_GAP_SIGNALS", "column": "observed_at / signal_date", "stored_tz": "EDT (Python datetime.now())", "sample_raw": "2026-10-28 13:04:22 / 2026-10-28", "sample_edt": "2026-10-28 13:04:22 EDT", "conversion_rule": "Already America/New_York; filter observed_at <= snapshot_time_edt"},
        {"job": "04_SystemReconciliation", "db": "system_reconciliation.db", "table": "chronic_mismatch_registry", "column": "first/last_detected_date vs updated_at", "stored_tz": "detected_date=EDT / updated_at=UTC", "sample_raw": "2026-10-28 / 2026-10-28 10:35:45", "sample_edt": "2026-10-28 06:35:45 EDT", "conversion_rule": "Used strictly for E (chronic mismatch badge) & price fallback"},
        {"job": "00_StockAccuracy", "db": "inventory_history.db", "table": "BARCODE_BIN_STAY / AUDIT_RUN", "column": "last_seen_date / updated_at / created_at", "stored_tz": "EDT (Python datetime.now())", "sample_raw": "2026-10-28 / 2026-10-28 06:59:25", "sample_edt": "2026-10-28 06:59:25 EDT", "conversion_rule": "All 00_JOB timestamps are generated in Python local time (EDT)"},
        {"job": "06_Data_Collector", "db": "ops_raw.db", "table": "mb51_raw", "column": "entry_date+entry_time vs entry_datetime_edt", "stored_tz": "entry_date/time=KST (SAP CPUDT+CPUTM) / entry_datetime_edt=EDT", "sample_raw": "2026-10-28 21:15:00 / 2026-10-28 08:15:00", "sample_edt": "2026-10-28 08:15:00 EDT", "conversion_rule": "SAP KST (-13h EDT) pre-converted at ingest into entry_datetime_edt; filter exact EDT window"},
    ]
    df_ts_audit = pd.DataFrame(ts_audit_rows)

    # =========================================================================
    # Preload 04 Auxiliary Maps (Strictly for Flag E & Unit Price Fallback — Rule 1)
    # =========================================================================
    price_map_04 = {}
    for r in c04.execute("""
        SELECT material, net_price, price_unit, currency
        FROM accuracy_material_snapshot
        WHERE net_price IS NOT NULL AND net_price > 0
        ORDER BY snapshot_date ASC
    """):
        mat = str(r["material"]).strip()
        pu = float(r["price_unit"] or 1.0)
        u_p = float(r["net_price"]) / (pu if pu > 0 else 1.0)
        price_map_04[mat] = (u_p, str(r["currency"] or "USD"), "04_NET_PRICE")

    chronic_map = {}
    for r in c04.execute("""
        SELECT * FROM chronic_mismatch_registry
        WHERE status = 'ACTIVE' AND consecutive_days >= 3
    """):
        chronic_map[str(r["material"]).strip()] = dict(r)

    # =========================================================================
    # Preload 00 Anomaly Features for C3 (Rule 7)
    # =========================================================================
    bin_stay_by_mat = defaultdict(list)
    for r in c00.execute("""
        SELECT mat_no, current_bin, COUNT(*) as box_cnt,
               MAX(consecutive_days_in_bin) as max_bin_days,
               MAX(warehouse_dwell_days) as max_wh_days
        FROM BARCODE_BIN_STAY
        WHERE is_active = 1
        GROUP BY mat_no, current_bin
    """):
        bin_stay_by_mat[str(r["mat_no"]).strip()].append(dict(r))

    feat_00_by_date_mat = {}
    feat_00_latest_by_mat = {}
    for r in c00.execute("""
        SELECT ar.run_date, f.mat_no,
               COUNT(f.id) as box_cnt,
               MAX(f.fifo_bypass_gap_days) as max_fifo_gap_days,
               MAX(f.missing_gi_flag) as max_missing_gi_flag,
               MAX(f.missing_gi_diff_box) as max_missing_gi_diff_box
        FROM AUDIT_FEATURE_SNAPSHOT f
        JOIN AUDIT_RUN ar ON f.run_id = ar.run_id
        WHERE ar.mode != 'QUARANTINED' AND ar.run_id NOT LIKE '%TEST%'
        GROUP BY ar.run_date, f.mat_no
        ORDER BY ar.run_date ASC
    """):
        d_s = str(r["run_date"]).strip()
        m_s = str(r["mat_no"]).strip()
        feat_00_by_date_mat[(d_s, m_s)] = dict(r)
        feat_00_latest_by_mat[m_s] = dict(r)

    audit_shortage_by_mat = {}
    for r in c00.execute("""
        SELECT mat_no, bin, book_qty_at_count, counted_qty, diff_qty, run_id
        FROM AUDIT_RESULT
        WHERE is_discrepancy = 1 AND counted_qty < book_qty_at_count
    """):
        audit_shortage_by_mat[str(r["mat_no"]).strip()] = dict(r)

    # =========================================================================
    # Preload 01 Signals & Snapshots with Exact EDT Timestamps (Rule 8)
    # =========================================================================
    gap_signals_all = []
    for r in c01.execute("SELECT * FROM GI_STOCK_GAP_SIGNALS ORDER BY observed_at ASC"):
        gap_signals_all.append(dict(r))

    gr_err_all = []
    for r in c01.execute("SELECT * FROM gr_error_snapshots"):
        d = dict(r)
        d["edt_ts"] = utc_to_edt_str(r["extracted_at"])
        d["edt_date"] = d["edt_ts"][:10]
        gr_err_all.append(d)

    gi_err_all = []
    for r in c01.execute("SELECT * FROM gi_error_snapshots"):
        d = dict(r)
        d["edt_ts"] = utc_to_edt_str(r["extracted_at"])
        d["edt_date"] = d["edt_ts"][:10]
        gi_err_all.append(d)

    sp31_01_acc_transfers = load_01_sp31_accuracy_transfers(c01, c06=c06)

    # =========================================================================
    # Preload Exact-Time 01 MB52 + WMS + 02 Plan Files for All 6 Snapshots (Rule 1, 2, 9)
    # =========================================================================
    all_backlog_mats = {
        str(r[0]).strip()
        for r in c02.execute("SELECT DISTINCT material FROM raw_backlog_history").fetchall()
    }
    for r in c02.execute("SELECT DISTINCT material FROM cogi_snapshots WHERE snapshot_id LIKE 'BACKLOG_MANUAL_NOW_20261029%'").fetchall():
        all_backlog_mats.add(str(r[0]).strip())

    cogi_1805_path = JOB02_DIR / f"cogi_errors/COGI_{SAP_PLANT}_20261028_180550.XLS"
    df_1805_raw = pd.DataFrame()
    if cogi_1805_path.exists():
        sys.path.insert(0, str(JOB02_DIR))
        from cogi_collector import parse_cogi_file
        df_1805_raw = pd.DataFrame(parse_cogi_file(cogi_1805_path))
        for m in df_1805_raw["Material"].unique():
            all_backlog_mats.add(str(m).strip())

    gi_db_ro_path = INV_DIR / "01_JOB_GR_GI_Error/db/gr_gi_history.db"
    snap_data_cache = {}
    mb52_price_fallback = {}
    for key, cfg in SNAPSHOT_FILE_MAP.items():
        mb_map, pr_map, desc_map = parse_mb52_file(cfg["mb52"])
        mb52_price_fallback.update(pr_map)
        wms_map, bin_map = parse_wms_file(cfg["wms"], all_backlog_mats)
        plan_rows = parse_plan_file(cfg["plan"]) if cfg["plan"] else []

        # Build fallback stock map & evaluate D108 shared GI verdict as of snap_edt (#183, #184)
        fb_stock = {}
        for m_k in all_backlog_mats:
            s_d = {s: mb_map.get((m_k, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04", "SL49", "SP31", "SP11") if mb_map.get((m_k, s), 0.0) != 0}
            w_d = {s: wms_map.get((m_k, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04", "SL49") if wms_map.get((m_k, s), 0.0) != 0}
            fb_stock[m_k] = (s_d, w_d)

        gi_xls = _find_gi_error_file_le(JOB01_GI_ERR, cfg["snap_edt"])
        gr_xls = _find_gr_error_file_le(JOB01_GR_ERR, cfg["snap_edt"])
        active_gi_recs = parse_ztcode_stock_rec_gi_records_detailed(gi_xls) if gi_xls else None
        fresh_gr_recs = parse_ztcode_stock_rec_gr_records_detailed(gr_xls) if gr_xls else None
        active_gr_map = extract_active_gr_error_map([gr_xls]) if gr_xls else {}
        d108_eval = evaluate_gi_errors_as_of(
            as_of=cfg["snap_edt"],
            gi_db_path=gi_db_ro_path,
            gi_plan_dir=JOB01_PLANS,
            ops_raw_db_path=resolved_ops_db,
            fresh_gr_records=fresh_gr_recs,

            active_gi_records=active_gi_recs,
            fallback_stock_by_mat=fb_stock,
        )
        d108_by_mat = defaultdict(list)
        for v in d108_eval.get("verdicts", []):
            d108_by_mat[str(v["material"]).strip()].append(v)

        snap_data_cache[key] = {
            "snap_edt": cfg["snap_edt"],
            "mb52_file": cfg["mb52"].name,
            "wms_file": cfg["wms"].name,
            "plan_file": cfg["plan"].name if cfg["plan"] else "NONE (Dry-Run)",
            "gi_file": gi_xls.name if gi_xls else "01_SNAPSHOT_DB",
            "gr_files": [p.name for p in expand_gr_batch_paths([gr_xls])] if gr_xls else [],
            "active_gr_map": active_gr_map,
            "mb_map": mb_map,
            "pr_map": pr_map,
            "desc_map": desc_map,
            "wms_map": wms_map,
            "bin_map": bin_map,
            "plan_rows": plan_rows,
            "d108_verdicts_by_mat": d108_by_mat,
            "d108_eval_meta": {
                "gi_01_batch_id": d108_eval.get("gi_01_batch_id"),
                "gi_01_plan_time_edt": d108_eval.get("gi_01_plan_time_edt"),
            },
        }

    def get_unit_price(mat: str, snap_key: str):
        # Prefer exact-time MB52 value/qty unit price or 04 net_price
        pr_mb = snap_data_cache[snap_key]["pr_map"].get(mat) or mb52_price_fallback.get(mat)
        if mat in price_map_04:
            u_p, curr, src = price_map_04[mat]
            return u_p, curr, "PRICED"
        if pr_mb and pr_mb > 0:
            return pr_mb, "USD", "PRICED"
        return None, "USD", "NO_PRICE (단가 없음)"

    # =========================================================================
    # Helper: Classify a Single Row (after Rule 2 Plan Linking & Rule 3 Split)
    # =========================================================================
    def classify_row(
        snap_key: str,
        b_date: str,
        mat: str,
        mat_desc: str,
        row_qty: float,
        orig_mat_deficit: float,
        cogi_lines: int,
        slocs_str: str,
        err_codes_str: str,
        consec_days: int,
        plan_status: str,
        plan_xfer_qty: float,
        plan_cand_sloc: str,
        plan_reason: str,
        is_excess_d_split: bool = False,
        is_partial_shortage_covered: bool = False,
    ):
        sc = snap_data_cache[snap_key]
        snap_edt = sc["snap_edt"]
        mb_map = sc["mb_map"]
        wms_map = sc["wms_map"]
        bins = sc["bin_map"].get(mat, [])

        # Exact-time SAP and WMS totals (Rule 1)
        sap_by_sloc = {s: mb_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04", "SL49", "SP31", "SP11") if mb_map.get((mat, s), 0.0) != 0}
        wms_by_sloc = {s: wms_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04", "SL49") if wms_map.get((mat, s), 0.0) != 0}

        plant_sap = round(sum(v for v in sap_by_sloc.values() if v > 0), 2)
        plant_wms = round(sum(v for v in wms_by_sloc.values() if v > 0), 2)
        wh_sap = round(sum(mb_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04") if mb_map.get((mat, s), 0.0) > 0), 2)
        wh_wms = round(sum(wms_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04") if wms_map.get((mat, s), 0.0) > 0), 2)
        sp31_sap = round(mb_map.get((mat, "SP31"), 0.0), 2)

        # Bin breakdown from exact-time WMS file (Rule 9)
        line_bins = [b for b in bins if is_line_supply_bin(b["sloc"], b["bin"])]
        rack_bins = [b for b in bins if not is_line_supply_bin(b["sloc"], b["bin"])]
        line_zone_qty = round(sum(b["qty"] for b in line_bins), 2)
        rack_zone_qty = round(sum(b["qty"] for b in rack_bins), 2)

        # Exact-time 01 GI_STOCK_GAP_SIGNALS and ZTCODE_STOCK_REC errors (Rule 8: strictly observed_at <= snap_edt)
        valid_gap_sigs = [
            g for g in gap_signals_all
            if g["material"] == mat
            and str(g["observed_at"]) <= snap_edt
            and str(g["signal_type"]) in ("CHAIN_GAP", "TOTAL_MISMATCH")
            and float(g["wms_total"] or 0.0) > float(g["sap_total"] or 0.0)
        ]
        snap_date_10 = snap_edt[:10]
        gr_day_cands = [g for g in gr_err_all if g["edt_date"] == snap_date_10 and g["edt_ts"] <= snap_edt]
        latest_gr_bid = sorted({g["batch_id"] for g in gr_day_cands})[-1] if gr_day_cands else None
        gr_errs_at_snap = [g for g in gr_day_cands if g["material"] == mat and g["batch_id"] == latest_gr_bid] if latest_gr_bid else []

        gi_day_cands = [g for g in gi_err_all if g["edt_date"] == snap_date_10 and g["edt_ts"] <= snap_edt]
        latest_gi_bid = sorted({g["batch_id"] for g in gi_day_cands})[-1] if gi_day_cands else None
        gi_errs_at_snap = [g for g in gi_day_cands if g["material"] == mat and g["batch_id"] == latest_gi_bid] if latest_gi_bid else []

        file_gr_info = sc.get("active_gr_map", {}).get(mat)
        active_gr_qty = max(
            round(sum(abs(float(g.get("error_qty") or g.get("qty") or 0.0)) for g in gr_errs_at_snap), 2),
            round(float(file_gr_info.get("Total_GR_Qty", 0.0)) if file_gr_info else 0.0, 2),
        )
        has_active_gr = (len(gr_errs_at_snap) > 0) or (file_gr_info is not None and active_gr_qty > 0)

        # Check D90 unposted GR into SL00: active 311 SL00->SL01 GI error at snap_edt + SL00 WMS > SL00 SAP + plant_wms > plant_sap + no GR error
        sl00_311_gi = [g for g in gi_errs_at_snap if str(g.get("mvt")) == "311" and str(g.get("from_sloc")) == "SL00"]
        sl00_sap = mb_map.get((mat, "SL00"), 0.0)
        sl00_wms = wms_map.get((mat, "SL00"), 0.0)
        is_strict_F = (
            not has_active_gr
            and (
                len(valid_gap_sigs) > 0
                or (len(sl00_311_gi) > 0 and sl00_wms > sl00_sap and plant_wms > plant_sap)
            )
        )

        # Non-GI B condition (#187 B-6): item has active GR error AND warehouse SAP outside pending GR cannot cover deficit
        wh_sap_non_sl00 = round(sum(mb_map.get((mat, s), 0.0) for s in ("SL01", "SL02", "SL03", "SL04") if mb_map.get((mat, s), 0.0) > 0), 2)
        is_non_gi_gr_blocked = (
            has_active_gr
            and sp31_sap < row_qty
            and wh_sap < row_qty
            and (wh_sap + active_gr_qty) >= row_qty
        )

        err_list = [e.strip() for e in str(err_codes_str or "").split(",") if e.strip()]
        non_m7021 = [e for e in err_list if e != "M7 021"]

        # Determine Primary Cause using Rule 2 + Rule 3 + Rule 5 + Rule 6 + Rule 8 + D108 Shared GI Verdict (#183, #184, #187 B-2 & B-6)
        other_sub_name = None
        d108_mapped = None
        gi_link_gone_note = False
        first_cogi_sloc = next((s.strip().upper() for s in str(slocs_str or "").split(",") if s.strip()), "")

        if plan_status == "BLOCKED_BY_GI_ERROR" or (
            plan_status == "NO_PLAN_FILE"
            and len(gi_errs_at_snap) > 0
            and any(str(g.get("mvt")) in ("311", "312") for g in gi_errs_at_snap)
        ):
            v_match = resolve_d108_for_backlog_item(
                material=mat,
                target_sloc=first_cogi_sloc,
                verdicts_by_mat=sc["d108_verdicts_by_mat"],
                cand_sloc=plan_cand_sloc,
            )
            d108_mapped = map_d108_verdict_to_03_cause(v_match)
            if d108_mapped is None and plan_status == "BLOCKED_BY_GI_ERROR":
                gi_link_gone_note = True

        if d108_mapped is not None and not is_excess_d_split:
            primary = d108_mapped["primary"]
            other_sub_name = d108_mapped["other_sub_name"]
            d_sub_note = d108_mapped["d_sub_note"]
        elif is_excess_d_split:
            if has_active_gr:
                primary = "B"
                d_sub_note = "B. GR 미처리 대기"
            else:
                primary = "D"
                d_sub_note = "D. 진성 결품 (부분 결품 초과 부족분)"
        elif plan_status == "MASTER_DATA_ERROR" or (non_m7021 and plan_status not in ("APPROVED_FULL_RESOLUTION", "PARTIAL_ACCURACY_REBALANCING")):
            primary = "OTHER"
            other_sub_name = f"OTHER — 마스터 에러 ({'/'.join(non_m7021) if non_m7021 else 'M7 096'})"
            d_sub_note = other_sub_name
        elif plan_status in ("TRUE_SHORTAGE", "MATERIAL_NOT_FOUND_IN_STOCK", "NO_SOURCE_CANDIDATE") or (plant_sap <= 0 and plant_wms <= 0):
            if has_active_gr:
                primary = "B"
                d_sub_note = "B. GR 미처리 대기"
            else:
                primary = "D"
                d_sub_note = "D. 진성 결품 (전사 재고 0)"
        elif plan_status == "RESOLVABLE_BY_COGI_SAVE" or ((plan_status == "NO_PLAN_FILE" or gi_link_gone_note) and sp31_sap >= row_qty):
            primary = "OTHER"
            other_sub_name = "OTHER — COGI 저장 대기 (SP31 장부재고 보유)"
            d_sub_note = other_sub_name
        elif plan_status in ("APPROVED_FULL_RESOLUTION", "PARTIAL_ACCURACY_REBALANCING"):
            primary = "R"
            d_sub_note = "R. 02 자동 해소 대상"
        elif is_non_gi_gr_blocked:
            primary = "B"
            d_sub_note = "B. GR 미처리 대기"
        elif is_strict_F:
            primary = "F"
            d_sub_note = "F. 입고 누락 (실물 WMS 입고·SAP GR 미반영)"
        elif plan_status == "WAREHOUSE_SAP_SHORT_PERSISTENT":
            primary = "A"
            d_sub_note = "A. 장부 불일치 (보충 창고 SAP < WMS 부족)"
        elif plan_status == "WAREHOUSE_BALANCED_WOULD_DEGRADE":
            primary = "C"
            d_sub_note = "C. 현장 체류·미불출"
        elif plan_status == "ACCURACY_GUARDRAIL_BLOCKED":
            # 2026-10-27 legacy combined guardrail status: split into C vs A using exact 06:00 MB52 + 06:01 WMS (Rule 5 & 6)
            has_balanced_wh = any(
                min(mb_map.get((mat, s), 0.0), wms_map.get((mat, s), 0.0)) >= row_qty
                and mb_map.get((mat, s), 0.0) >= wms_map.get((mat, s), 0.0)
                for s in ("SL00", "SL01", "SL02", "SL03", "SL04")
            )
            has_sap_short_wh = any(
                wms_map.get((mat, s), 0.0) > mb_map.get((mat, s), 0.0)
                and wms_map.get((mat, s), 0.0) > 0
                for s in ("SL00", "SL01", "SL02", "SL03", "SL04")
            )
            if has_sap_short_wh and not has_balanced_wh:
                primary = "A"
                d_sub_note = "A. 장부 불일치 (보충 창고 SAP < WMS 부족)"
            else:
                primary = "C"
                d_sub_note = "C. 현장 체류·미불출"
        else:
            # 18:05 dry-run (NO_PLAN_FILE) or GI_LINK_GONE fallthrough classification from exact MB52 + WMS
            if plant_sap <= 0 and plant_wms <= 0:
                primary = "B" if has_active_gr else "D"
                d_sub_note = "B. GR 미처리 대기" if has_active_gr else "D. 진성 결품 (전사 재고 0)"
            elif any(mb_map.get((mat, s), 0.0) > wms_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04")):
                primary = "R"
                d_sub_note = "R. 02 자동 해소 대상"
            elif any(
                min(mb_map.get((mat, s), 0.0), wms_map.get((mat, s), 0.0)) >= row_qty
                and mb_map.get((mat, s), 0.0) == wms_map.get((mat, s), 0.0)
                for s in ("SL00", "SL01", "SL02", "SL03", "SL04")
            ):
                primary = "C"
                d_sub_note = "C. 현장 체류·미불출"
            elif any(wms_map.get((mat, s), 0.0) > mb_map.get((mat, s), 0.0) for s in ("SL00", "SL01", "SL02", "SL03", "SL04")):
                primary = "A"
                d_sub_note = "A. 장부 불일치 (보충 창고 SAP < WMS 부족)"
            else:
                primary = "C"
                d_sub_note = "C. 현장 체류·미불출"

        # Rule 7 & Rule 9: C1 / C2 / C3 sub-classification
        feat_00 = feat_00_by_date_mat.get((b_date, mat)) or feat_00_latest_by_mat.get(mat) or {}
        aud_short = audit_shortage_by_mat.get(mat)
        max_fifo_gap = float(feat_00.get("max_fifo_gap_days") or 0.0)
        max_mgi_flag = str(feat_00.get("max_missing_gi_flag") or "")
        max_mgi_box = float(feat_00.get("max_missing_gi_diff_box") or 0.0)

        bins_00 = bin_stay_by_mat.get(mat, [])
        rack_stay_bins = [
            b for b in bins_00
            if str(b.get("current_bin") or "").strip() != ""
            and not is_line_supply_bin("", str(b.get("current_bin") or ""))
            and int(b.get("max_wh_days") or 0) >= 3
        ]
        min_rack_bin_stay_days = min([int(b["max_bin_days"] or 0) for b in rack_stay_bins], default=0)

        c_sub_code = "-"
        c_sub_name = "-"
        c3_anomaly_signals = []
        if primary == "C":
            if aud_short is not None:
                c3_anomaly_signals.append(f"실사부족({aud_short['bin']}:{aud_short['diff_qty']:g}EA)")
            if min_rack_bin_stay_days >= 3 and consec_days >= 3:
                c3_anomaly_signals.append(f"랙무변동({min_rack_bin_stay_days:g}d+)")
            if max_fifo_gap >= 7.0 and consec_days >= 3:
                c3_anomaly_signals.append(f"FIFO역전({max_fifo_gap:g}d)")
            if max_mgi_flag == "UNDER" and max_mgi_box >= 3.0 and consec_days >= 3:
                c3_anomaly_signals.append(f"00_MissingGI({max_mgi_box:g}box)")

            # Rule 7: C3 confirmed ONLY when >= 1 barcode anomaly signal is present!
            if len(c3_anomaly_signals) >= 1:
                c_sub_code = "C3"
                c_sub_name = "C3. 미스캔 소진 의심 (= A 승격 후보)"
            elif line_zone_qty > 0:
                c_sub_code = "C2"
                c_sub_name = "C2. 라인 공급 구역 체류 (SL01 R·CRT·PRO·SMT·OEM)"
            else:
                c_sub_code = "C1"
                c_sub_name = "C1. 창고 일반 랙 재고 미불출 (창고 랙 대기)"

        # Auxiliary badges (Rule 4 & Rule 7 & D108)
        chron = chronic_map.get(mat)
        flag_E = chron is not None
        flag_3d_badge = (consec_days >= 3)
        flag_C3_promote = (c_sub_code == "C3")

        aux_list = []
        if d108_mapped is not None:
            aux_list.append(d108_mapped["d108_badge"])
        elif gi_link_gone_note:
            aux_list.append("GI연결없음(GI_LINK_GONE->일반원인전환)")
        if is_excess_d_split:
            aux_list.append(f"부분결품분할(총부족{orig_mat_deficit:g}EA중초과분)")
        elif is_partial_shortage_covered:
            aux_list.append(f"부분결품분할(총부족{orig_mat_deficit:g}EA중재고보유분)")
        if plan_status == "PARTIAL_ACCURACY_REBALANCING" and plan_xfer_qty > 0:
            aux_list.append(f"02부분이동({plan_xfer_qty:g}EA이동/{row_qty - plan_xfer_qty:g}EA잔여)")
        if flag_E:
            aux_list.append("E(만성불일치)")
        if flag_3d_badge:
            aux_list.append(f"[3일+ 백로그 지속({consec_days}d)]")
        if flag_C3_promote:
            aux_list.append(f"C3이상신호({'/'.join(c3_anomaly_signals)})")

        sp31_acc_link = match_sp31_01_accuracy_link_72h(
            material=mat,
            slocs_str=slocs_str,
            snap_edt_str=snap_edt,
            sp31_acc_transfers=sp31_01_acc_transfers,
        )
        if sp31_acc_link["tag"] != "-":
            aux_list.append(f"{sp31_acc_link['tag']}({sp31_acc_link['qty_72h']:g}EA)")

        # Rule 4: Severity Group — ONLY A, D, F, C3, and D108 bug/guard inspection (②/③) are CRITICAL (🔴)!
        if d108_mapped is not None:
            severity = d108_mapped["severity"]
        elif primary in ("A", "D", "F") or c_sub_code == "C3":
            severity = "CRITICAL (🔴)"
        elif primary in ("B", "R", "C") or other_sub_name in (
            "OTHER — COGI 저장 대기 (SP31 장부재고 보유)",
        ):
            severity = "TEMPORARY (🟡)"
        else:
            severity = "OTHER (⚪)"

        # Owner Team (Thread 168 & Thread 169 Rule 5)
        gi_mvts = {str(x.get("mvt") or "") for x in gi_errs_at_snap}
        if gi_mvts & {"593", "594", "343", "553", "554", "907", "908"}:
            owner_team = "품질"
        elif primary in ("D", "F", "B", "R", "A", "OTHER"):
            owner_team = "자재"
        elif primary == "C":
            owner_team = "생산 (자재 참조)" if c_sub_code == "C3" else "생산"
        else:
            owner_team = "자재"

        # Price calculation (D103-5)
        u_price, curr, price_status = get_unit_price(mat, snap_key)
        amount_usd = round(row_qty * u_price, 2) if u_price is not None else None

        cause_label_map = {
            "D": d_sub_note,
            "F": "F. 입고 누락 (Missing GR)",
            "B": "B. GR 미처리 대기",
            "R": "R. 02 자동 해소 대상",
            "A": "A. 장부 불일치 (보충 창고 SAP < WMS)",
            "C": c_sub_name,
            "OTHER": other_sub_name or "OTHER — 기타",
        }
        if d108_mapped is not None:
            detailed_code = d108_mapped["detailed_code"]
        elif primary == "C":
            detailed_code = c_sub_code
        elif primary == "OTHER":
            if "COGI 저장" in (other_sub_name or ""):
                detailed_code = "OTHER_COGI_SAVE"
            else:
                detailed_code = "OTHER_MASTER"
        else:
            detailed_code = primary

        # Evidence string with exact 01 MB52/WMS file names & per-SLoc numbers
        sap_s_str = ", ".join(f"{k}:{v:g}" for k, v in sorted(sap_by_sloc.items())) or "0"
        wms_s_str = ", ".join(f"{k}:{v:g}" for k, v in sorted(wms_by_sloc.items())) or "0"
        ev_parts = [
            f"01_MB52({sc['mb52_file']}): Plant={plant_sap:g}[{sap_s_str}]",
            f"01_WMS({sc['wms_file']}): Plant={plant_wms:g}[{wms_s_str}]",
            f"02_Plan[{plan_status}: xfer={plan_xfer_qty:g}, cand={plan_cand_sloc or '-'}]",
        ]
        if d108_mapped is not None:
            ev_parts.append(d108_mapped["d108_evidence"])
        if line_zone_qty > 0 or rack_zone_qty > 0:
            top_bins = ", ".join(f"{b['sloc']}:{b['bin']}({b['qty']:g})" for b in bins[:3])
            ev_parts.append(f"WMS_Bins[LineZone={line_zone_qty:g}EA, Rack={rack_zone_qty:g}EA | {top_bins}]")
        if c3_anomaly_signals:
            ev_parts.append(f"00_C3_Anomaly[{', '.join(c3_anomaly_signals)}]")
        if chron:
            ev_parts.append(f"04_Chronic[days={chron['consecutive_days']}, diff={chron['current_diff_qty']:g}]")
        if sp31_acc_link["tag"] != "-":
            ev_parts.append(f"01_SP31_AccLink72h[{sp31_acc_link['detail']}]")

        return {
            "business_date_edt": b_date,
            "snapshot_time_edt": snap_edt,
            "material": mat,
            "material_desc": mat_desc or sc["desc_map"].get(mat, ""),
            "cogi_lines": cogi_lines,
            "slocs": slocs_str,
            "error_codes": err_codes_str,
            "consecutive_backlog_days": consec_days,
            "orig_material_deficit_qty": round(orig_mat_deficit, 2),
            "deficit_qty": round(row_qty, 2),
            "plan_status": plan_status,
            "plan_transfer_qty": round(plan_xfer_qty, 2),
            "primary_cause": primary,
            "c_sub_code": c_sub_code,
            "detailed_cause_code": detailed_code,
            "detailed_cause_name": cause_label_map[primary],
            "owner_team": owner_team,
            "severity_group": severity,
            "ref_01_accuracy_link": sp31_acc_link["tag"],
            "ref_01_accuracy_qty_72h": sp31_acc_link["qty_72h"],
            "ref_01_accuracy_detail": sp31_acc_link["detail"],
            "ref_01_mb51_doc_check": sp31_acc_link["mb51_doc_check"],
            "aux_flags": ", ".join(aux_list) if aux_list else "-",
            "d108_cause_code": d108_mapped["d108_code"] if d108_mapped else "-",
            "d108_linked_gi": d108_mapped["gi_link_summary"] if d108_mapped else "-",
            "d108_01_verdict": d108_mapped["gi_01_verdict"] if d108_mapped else "-",
            "d108_d107_status": d108_mapped["d107_status"] if d108_mapped else "-",
            "d108_is_bug_candidate": bool(d108_mapped["is_bug_candidate"]) if d108_mapped else False,
            "flag_E_chronic": bool(flag_E),
            "flag_3d_badge": bool(flag_3d_badge),
            "flag_C3_promote": bool(flag_C3_promote),
            "unit_price_usd": round(u_price, 4) if u_price is not None else None,
            "currency": curr,
            "deficit_amount_usd": amount_usd,
            "price_status": price_status,
            "plant_sap_qty": plant_sap,
            "plant_wms_qty": plant_wms,
            "wh_sap_qty": wh_sap,
            "wh_wms_qty": wh_wms,
            "sp31_sap_qty": sp31_sap,
            "wms_line_zone_qty": line_zone_qty,
            "wms_rack_zone_qty": rack_zone_qty,
            "evidence_detail": " | ".join(ev_parts),
        }

    # =========================================================================
    # DELIVERABLE 2: Build Material x Date Mart (2026-10-23 ~ 2026-10-28, First Batch/Day)
    # Applying Rule 2 (02 Plan Link) and Rule 3 (Partial Shortage Split)
    # =========================================================================
    first_batches = {}
    batch_min_edt = {}

    for r in c02.execute("""
        SELECT run_date, history_id, MIN(extracted_at) as min_ext
        FROM raw_backlog_history
        WHERE history_id NOT LIKE '%TEST%' AND source_file NOT LIKE '%TEST%'
        GROUP BY run_date, history_id
        ORDER BY run_date, min_ext
    """):
        if r["run_date"] not in first_batches:
            first_batches[r["run_date"]] = r["history_id"]
            batch_min_edt[r["run_date"]] = utc_to_edt_str(r["min_ext"])

    ordered_dates = sorted(first_batches.keys())
    consec_streak = defaultdict(int)
    prev_date_mats = set()

    mart_rows = []
    daily_mat_qty = defaultdict(dict)  # date -> {mat: total_deficit_qty}
    snap_mat_sloc_qty = defaultdict(lambda: defaultdict(float))  # snap_key -> {(mat, sloc): qty}


    for b_date in ordered_dates:
        hid = first_batches[b_date]
        for rs in c02.execute(
            "SELECT material, sloc, ROUND(SUM(qty), 2) as q FROM raw_backlog_history WHERE run_date = ? AND history_id = ? GROUP BY material, sloc",
            (b_date, hid),
        ):
            snap_mat_sloc_qty[b_date][(str(rs["material"]).strip(), str(rs["sloc"] or "").strip().upper())] = float(rs["q"] or 0.0)
        sc = snap_data_cache[b_date]
        mb_map = sc["mb_map"]
        wms_map = sc["wms_map"]

        # Index 02 plan rows by Material
        plan_by_mat = defaultdict(list)
        for pr in sc["plan_rows"]:
            plan_by_mat[str(pr["Material"]).strip()].append(pr)

        raw_rows = c02.execute("""
            SELECT material, MAX(material_desc) as material_desc,
                   COUNT(*) as line_cnt, GROUP_CONCAT(DISTINCT sloc) as slocs,
                   GROUP_CONCAT(DISTINCT error_code) as err_codes,
                   ROUND(SUM(qty), 2) as deficit_qty
            FROM raw_backlog_history
            WHERE run_date = ? AND history_id = ?
            GROUP BY material
            ORDER BY deficit_qty DESC
        """, (b_date, hid)).fetchall()

        current_mats = set()
        for r in raw_rows:
            mat = str(r["material"]).strip()
            current_mats.add(mat)
            consec_streak[mat] = (consec_streak[mat] + 1) if (mat in prev_date_mats) else 1
            tot_def = round(float(r["deficit_qty"] or 0.0), 2)
            daily_mat_qty[b_date][mat] = tot_def

            # Total available plant stock at exact 01 snapshot time (Rule 1 & Rule 3)
            sap_tot = round(sum(v for (k_m, k_s), v in mb_map.items() if k_m == mat and v > 0), 2)
            wms_tot = round(sum(v for (k_m, k_s), v in wms_map.items() if k_m == mat and v > 0), 2)
            avail_stock = round(max(sap_tot, wms_tot), 2)

            p_list = plan_by_mat.get(mat, [])
            # Group p_list by Status
            by_st = defaultdict(lambda: {"def": 0.0, "xfer": 0.0, "cnt": 0, "cand": None, "reason": ""})
            for pr in p_list:
                st = str(pr.get("Status") or "NO_PLAN_FILE").strip()
                by_st[st]["def"] = round(by_st[st]["def"] + float(pr.get("Deficit_Qty") or 0.0), 2)
                by_st[st]["xfer"] = round(by_st[st]["xfer"] + float(pr.get("Transfer_Qty") or 0.0), 2)
                by_st[st]["cnt"] += 1
                if pr.get("Best_Cand_SLoc"):
                    by_st[st]["cand"] = str(pr.get("Best_Cand_SLoc"))
                if pr.get("Reason"):
                    by_st[st]["reason"] = str(pr.get("Reason"))

            if not by_st:
                by_st["NO_PLAN_FILE"] = {"def": tot_def, "xfer": 0.0, "cnt": int(r["line_cnt"]), "cand": None, "reason": ""}

            # Rule 3: Check if total material deficit exceeds available plant stock (and avail_stock > 0)
            if tot_def > avail_stock and avail_stock > 0:
                covered_qty = avail_stock
                excess_d_qty = round(tot_def - avail_stock, 2)
                # Use the primary non-shortage status from 02 plan for the covered portion
                main_st = next(iter(by_st.keys()))
                st_info = by_st[main_st]
                cov_xfer = min(covered_qty, st_info["xfer"])
                mart_rows.append(classify_row(
                    snap_key=b_date,
                    b_date=b_date,
                    mat=mat,
                    mat_desc=r["material_desc"],
                    row_qty=covered_qty,
                    orig_mat_deficit=tot_def,
                    cogi_lines=int(r["line_cnt"]),
                    slocs_str=r["slocs"],
                    err_codes_str="M7 021",
                    consec_days=consec_streak[mat],
                    plan_status=main_st,
                    plan_xfer_qty=cov_xfer,
                    plan_cand_sloc=st_info["cand"],
                    plan_reason=st_info["reason"],
                    is_excess_d_split=False,
                    is_partial_shortage_covered=True,
                ))
                mart_rows.append(classify_row(
                    snap_key=b_date,
                    b_date=b_date,
                    mat=mat,
                    mat_desc=r["material_desc"],
                    row_qty=excess_d_qty,
                    orig_mat_deficit=tot_def,
                    cogi_lines=0,
                    slocs_str=r["slocs"],
                    err_codes_str="M7 021",
                    consec_days=consec_streak[mat],
                    plan_status="TRUE_SHORTAGE_PARTIAL_EXCESS",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason=f"Partial shortage: deficit {tot_def:g} EA > plant stock {avail_stock:g} EA",
                    is_excess_d_split=True,
                ))
            else:
                # Emit one row per distinct 02 status group:
                # if a material has multiple lines with different statuses that map to different causes
                # (e.g., TRUE_SHORTAGE + MASTER_DATA_ERROR, or PARTIAL_ACCURACY_REBALANCING + WAREHOUSE_BALANCED_WOULD_DEGRADE),
                # emitting per-status rows preserves 02's exact status totals!
                for st, st_info in by_st.items():
                    err_for_st = "M7 096" if st == "MASTER_DATA_ERROR" else "M7 021"
                    mart_rows.append(classify_row(
                        snap_key=b_date,
                        b_date=b_date,
                        mat=mat,
                        mat_desc=r["material_desc"],
                        row_qty=st_info["def"],
                        orig_mat_deficit=tot_def,
                        cogi_lines=st_info["cnt"],
                        slocs_str=r["slocs"],
                        err_codes_str=err_for_st,
                        consec_days=consec_streak[mat],
                        plan_status=st,
                        plan_xfer_qty=st_info["xfer"],
                        plan_cand_sloc=st_info["cand"],
                        plan_reason=st_info["reason"],
                        is_excess_d_split=False,
                    ))

        for m in list(consec_streak.keys()):
            if m not in current_mats:
                consec_streak[m] = 0
        prev_date_mats = current_mats

    df_mart = pd.DataFrame(mart_rows)

    # =========================================================================
    # Also Classify 2026-10-28 18:05 Live Snapshot (with Rule 1, 3, 4, 5, 6, 7, 8, 9)
    # =========================================================================
    live_1805_rows = []
    qty_1805_raw_map = {}
    qty_1805_net_map = {}
    sc_1805 = snap_data_cache["2026-10-28_1805"]
    mb_1805 = sc_1805["mb_map"]
    wms_1805 = sc_1805["wms_map"]

    if not df_1805_raw.empty:
        for (m_18, s_18), grp_ms in df_1805_raw.groupby(["Material", "SLoc"]):
            snap_mat_sloc_qty["2026-10-28_1805"][(str(m_18).strip(), str(s_18 or "").strip().upper())] = round(float(grp_ms["Qty"].sum()), 2)
        for mat, grp in df_1805_raw.groupby("Material"):
            mat_s = str(mat).strip()
            raw_q = round(float(grp["Qty"].sum()), 2)
            mvt312_max = max(
                (
                    float(g.get("err_qty") or 0.0)
                    for g in gi_err_all
                    if g["material"] == mat_s and g["edt_date"] == "2026-10-28" and str(g.get("mvt")) == "312"
                ),
                default=0.0,
            )
            net_q = max(0.0, round(raw_q - mvt312_max, 2))
            qty_1805_raw_map[mat_s] = raw_q
            qty_1805_net_map[mat_s] = net_q
            slocs_l = sorted(grp["SLoc"].astype(str).unique().tolist())
            errs_l = sorted(grp["Error_Code"].astype(str).unique().tolist())
            c_days = consec_streak.get(mat_s, 1)

            sap_tot = round(sum(v for (k_m, k_s), v in mb_1805.items() if k_m == mat_s and v > 0), 2)
            wms_tot = round(sum(v for (k_m, k_s), v in wms_1805.items() if k_m == mat_s and v > 0), 2)
            avail_stock = round(max(sap_tot, wms_tot), 2)

            # Split mixed M7 096 (Master Data Error) + M7 021 lines into separate mart rows
            if "M7 096" in errs_l and "M7 021" in errs_l:
                q_096 = round(float(grp[grp["Error_Code"] == "M7 096"]["Qty"].sum()), 2)
                q_021 = round(net_q - q_096, 2)
                live_1805_rows.append(classify_row(
                    snap_key="2026-10-28_1805",
                    b_date="2026-10-28",
                    mat=mat_s,
                    mat_desc=grp["Material_Desc"].iloc[0],
                    row_qty=q_021,
                    orig_mat_deficit=net_q,
                    cogi_lines=len(grp[grp["Error_Code"] == "M7 021"]),
                    slocs_str=",".join(slocs_l),
                    err_codes_str="M7 021",
                    consec_days=c_days,
                    plan_status="TRUE_SHORTAGE",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason="Zero stock at 18:00",
                    is_excess_d_split=False,
                ))
                live_1805_rows.append(classify_row(
                    snap_key="2026-10-28_1805",
                    b_date="2026-10-28",
                    mat=mat_s,
                    mat_desc=grp["Material_Desc"].iloc[0],
                    row_qty=q_096,
                    orig_mat_deficit=net_q,
                    cogi_lines=len(grp[grp["Error_Code"] == "M7 096"]),
                    slocs_str=",".join(slocs_l),
                    err_codes_str="M7 096",
                    consec_days=c_days,
                    plan_status="MASTER_DATA_ERROR",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason="M7 096 Master Data Error",
                    is_excess_d_split=False,
                ))
            elif net_q > avail_stock and avail_stock > 0:
                cov_q = avail_stock
                exc_q = round(net_q - avail_stock, 2)
                live_1805_rows.append(classify_row(
                    snap_key="2026-10-28_1805",
                    b_date="2026-10-28",
                    mat=mat_s,
                    mat_desc=grp["Material_Desc"].iloc[0],
                    row_qty=cov_q,
                    orig_mat_deficit=net_q,
                    cogi_lines=len(grp),
                    slocs_str=",".join(slocs_l),
                    err_codes_str=",".join(errs_l),
                    consec_days=c_days,
                    plan_status="NO_PLAN_FILE",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason="",
                    is_excess_d_split=False,
                    is_partial_shortage_covered=True,
                ))
                live_1805_rows.append(classify_row(
                    snap_key="2026-10-28_1805",
                    b_date="2026-10-28",
                    mat=mat_s,
                    mat_desc=grp["Material_Desc"].iloc[0],
                    row_qty=exc_q,
                    orig_mat_deficit=net_q,
                    cogi_lines=0,
                    slocs_str=",".join(slocs_l),
                    err_codes_str=",".join(errs_l),
                    consec_days=c_days,
                    plan_status="TRUE_SHORTAGE_PARTIAL_EXCESS",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason=f"Partial shortage: deficit {net_q:g} EA > plant stock {avail_stock:g} EA",
                    is_excess_d_split=True,
                ))
            else:
                live_1805_rows.append(classify_row(
                    snap_key="2026-10-28_1805",
                    b_date="2026-10-28",
                    mat=mat_s,
                    mat_desc=grp["Material_Desc"].iloc[0],
                    row_qty=net_q,
                    orig_mat_deficit=net_q,
                    cogi_lines=len(grp),
                    slocs_str=",".join(slocs_l),
                    err_codes_str=",".join(errs_l),
                    consec_days=c_days,
                    plan_status="NO_PLAN_FILE",
                    plan_xfer_qty=0.0,
                    plan_cand_sloc=None,
                    plan_reason="",
                    is_excess_d_split=False,
                    is_partial_shortage_covered=False,
                ))

    df_1805 = pd.DataFrame(live_1805_rows)

    # =========================================================================
    # DELIVERABLE 3: Daily Cause Summary & Critical vs. Temporary Breakdown
    # =========================================================================
    def summarize_cause_df(df_in: pd.DataFrame, date_col: str = "business_date_edt"):
        s_rows = []
        seen_b_for_snap = set()
        for (b_date, sev, p_cause, d_code, d_name, o_team), grp in df_in.groupby(
            [date_col, "severity_group", "primary_cause", "detailed_cause_code", "detailed_cause_name", "owner_team"]
        ):
            if p_cause == "B":
                seen_b_for_snap.add(b_date)
            priced_grp = grp[grp["price_status"] == "PRICED"]
            unpriced_grp = grp[grp["price_status"] != "PRICED"]
            s_rows.append({
                "snapshot_key": b_date,
                "severity_group": sev,
                "primary_cause": p_cause,
                "detailed_cause_code": d_code,
                "detailed_cause_name": d_name,
                "owner_team": o_team,
                "row_count": len(grp),
                "distinct_materials": grp["material"].nunique(),
                "total_deficit_qty": round(float(grp["deficit_qty"].sum()), 1),
                "plan_transfer_qty": round(float(grp["plan_transfer_qty"].sum()), 1),
                "priced_row_count": len(priced_grp),
                "priced_amount_usd": round(float(priced_grp["deficit_amount_usd"].sum()), 2) if not priced_grp.empty else 0.0,
                "no_price_row_count": len(unpriced_grp),
                "no_price_deficit_qty": round(float(unpriced_grp["deficit_qty"].sum()), 1) if not unpriced_grp.empty else 0.0,
            })
        for snap_val in sorted(df_in[date_col].unique()):
            if snap_val not in seen_b_for_snap:
                s_rows.append({
                    "snapshot_key": snap_val,
                    "severity_group": "TEMPORARY (🟡)",
                    "primary_cause": "B",
                    "detailed_cause_code": "B",
                    "detailed_cause_name": "B. GR 미처리 대기",
                    "owner_team": "자재",
                    "row_count": 0,
                    "distinct_materials": 0,
                    "total_deficit_qty": 0.0,
                    "plan_transfer_qty": 0.0,
                    "priced_row_count": 0,
                    "priced_amount_usd": 0.0,
                    "no_price_row_count": 0,
                    "no_price_deficit_qty": 0.0,
                })
        return pd.DataFrame(s_rows).sort_values(["snapshot_key", "severity_group", "detailed_cause_code"])

    df_cause_summary = summarize_cause_df(df_mart, "business_date_edt")
    df_1805_summary = summarize_cause_df(df_1805, "snapshot_time_edt")

    # =========================================================================
    # Classify 2026-10-29 08:15 Snapshot (`BACKLOG_MANUAL_NOW_20261029_081502`) for D108 Cross-Verification (#183, #184)
    # =========================================================================
    qty_1029_0815 = {}
    rows_1029_0815 = []
    if "2026-10-29_0815" in snap_data_cache:
        sc_0815 = snap_data_cache["2026-10-29_0815"]
        plan_0815_by_mat = defaultdict(list)
        for pr in sc_0815["plan_rows"]:
            plan_0815_by_mat[str(pr["Material"]).strip()].append(pr)
        raw_0815 = c02.execute("""
            SELECT material, MAX(material_desc) as material_desc,
                   COUNT(*) as line_cnt, GROUP_CONCAT(DISTINCT sloc) as slocs,
                   GROUP_CONCAT(DISTINCT error_code) as err_codes,
                   ROUND(SUM(qty), 2) as deficit_qty
            FROM cogi_snapshots
            WHERE snapshot_id = 'BACKLOG_MANUAL_NOW_20261029_081502'
            GROUP BY material
            ORDER BY deficit_qty DESC
        """).fetchall()
        for r in raw_0815:
            mat_s = str(r["material"]).strip()
            tot_def = round(float(r["deficit_qty"] or 0.0), 2)
            qty_1029_0815[mat_s] = tot_def
            p_list = plan_0815_by_mat.get(mat_s, [])
            by_st = defaultdict(lambda: {"def": 0.0, "xfer": 0.0, "cnt": 0, "cand": None, "reason": ""})
            for pr in p_list:
                st = str(pr.get("Status") or "NO_PLAN_FILE").strip()
                by_st[st]["def"] = round(by_st[st]["def"] + float(pr.get("Deficit_Qty") or 0.0), 2)
                by_st[st]["xfer"] = round(by_st[st]["xfer"] + float(pr.get("Transfer_Qty") or 0.0), 2)
                by_st[st]["cnt"] += 1
                if pr.get("Best_Cand_SLoc"):
                    by_st[st]["cand"] = str(pr.get("Best_Cand_SLoc"))
                if pr.get("Reason"):
                    by_st[st]["reason"] = str(pr.get("Reason"))
            if not by_st:
                by_st["NO_PLAN_FILE"] = {"def": tot_def, "xfer": 0.0, "cnt": int(r["line_cnt"]), "cand": None, "reason": ""}
            for st, st_info in by_st.items():
                err_for_st = "M7 096" if st == "MASTER_DATA_ERROR" else "M7 021"
                rows_1029_0815.append(classify_row(
                    snap_key="2026-10-29_0815",
                    b_date="2026-10-29_0815",
                    mat=mat_s,
                    mat_desc=r["material_desc"],
                    row_qty=st_info["def"] if st_info["def"] > 0 else tot_def,
                    orig_mat_deficit=tot_def,
                    cogi_lines=st_info["cnt"],
                    slocs_str=r["slocs"],
                    err_codes_str=err_for_st,
                    consec_days=consec_streak.get(mat_s, 1),
                    plan_status=st,
                    plan_xfer_qty=st_info["xfer"],
                    plan_cand_sloc=st_info["cand"],
                    plan_reason=st_info["reason"],
                    is_excess_d_split=False,
                ))
    df_1029_0815 = pd.DataFrame(rows_1029_0815)

    # =========================================================================
    # DELIVERABLE 4: Resolution Attribution Table (D103-6 — Window-Exact + Cap)
    # =========================================================================
    qty_1028_0815 = {}
    for r in c02.execute("""
        SELECT material, sloc, ROUND(SUM(qty), 2) as q
        FROM cogi_snapshots
        WHERE snapshot_id = 'BACKLOG_MANUAL_NOW_20261028_081503'
        GROUP BY material, sloc
    """):
        m_k = str(r["material"]).strip()
        s_k = str(r["sloc"] or "").strip().upper()
        q_v = float(r["q"] or 0.0)
        snap_mat_sloc_qty["2026-10-28_0815"][(m_k, s_k)] = q_v
        qty_1028_0815[m_k] = round(qty_1028_0815.get(m_k, 0.0) + q_v, 2)
    for r in c02.execute("""
        SELECT material, sloc, ROUND(SUM(qty), 2) as q
        FROM cogi_snapshots
        WHERE snapshot_id = 'BACKLOG_MANUAL_NOW_20261029_081502'
        GROUP BY material, sloc
    """):
        snap_mat_sloc_qty["2026-10-29_0815"][(str(r["material"]).strip(), str(r["sloc"] or "").strip().upper())] = float(r["q"] or 0.0)

    transfers_02_all = []
    auto_doc_nos = set()
    for r in c02.execute("SELECT * FROM backlog_transfers WHERE status IN ('SUCCESS', 'POSTED')"):
        d = dict(r)
        d["edt_ts"] = utc_to_edt_str(r["executed_at"])
        transfers_02_all.append(d)
        doc_s = str(d.get("sap_doc_no") or "").strip()
        if doc_s:
            auto_doc_nos.add(doc_s)

    transfers_01_all = []
    for r in c01.execute("SELECT * FROM transfer_posting_history WHERE post_status = 'POSTED'"):
        d = dict(r)
        d["edt_ts"] = utc_to_edt_str(r["executed_at"])
        transfers_01_all.append(d)
        doc_s = str(d.get("doc_number") or "").strip()
        if doc_s:
            auto_doc_nos.add(doc_s)

    # Build per-material COGI target SLoc map across all snapshots for manual 311 inflow filtering
    mat_cogi_slocs = defaultdict(set)
    for r in c02.execute("SELECT DISTINCT material, sloc FROM raw_backlog_history WHERE sloc IS NOT NULL"):
        m_s = str(r["material"]).strip()
        s_s = str(r["sloc"]).strip().upper()
        if m_s and s_s:
            mat_cogi_slocs[m_s].add(s_s)
    for r in c02.execute("SELECT DISTINCT material, sloc FROM cogi_snapshots WHERE sloc IS NOT NULL"):
        m_s = str(r["material"]).strip()
        s_s = str(r["sloc"]).strip().upper()
        if m_s and s_s:
            mat_cogi_slocs[m_s].add(s_s)
    if not df_1805_raw.empty and "SLoc" in df_1805_raw.columns:
        for _, r in df_1805_raw[["Material", "SLoc"]].drop_duplicates().iterrows():
            m_s = str(r["Material"]).strip()
            s_s = str(r["SLoc"]).strip().upper()
            if m_s and s_s:
                mat_cogi_slocs[m_s].add(s_s)

    min_mb51_edt = ""
    max_mb51_edt = ""
    if c06 is not None:
        cov_row = c06.execute(
            "SELECT MIN(entry_datetime_edt) AS min_ts, MAX(entry_datetime_edt) AS max_ts FROM mb51_raw WHERE entry_datetime_edt IS NOT NULL AND entry_datetime_edt != ''"
        ).fetchone()
        if cov_row:
            min_mb51_edt = str(cov_row["min_ts"] or "")
            max_mb51_edt = str(cov_row["max_ts"] or "")

    def get_window_coverage_label(start_edt: str, end_edt: str) -> str:
        if not max_mb51_edt:
            return "NO_OPS_RAW_DB"
        if min_mb51_edt <= start_edt and end_edt <= max_mb51_edt:
            return "FULL (100%)"
        if start_edt <= max_mb51_edt < end_edt:
            return f"PARTIAL (~{max_mb51_edt} EDT)"
        return f"UNCOVERED (max={max_mb51_edt})"

    b5_raw_311_doc_rows = []
    b5_neg_ctrl_rows = []

    def attribute_transition_exact(
        label: str,
        start_map: dict,
        end_map: dict,
        start_edt: str,
        end_edt: str,
        start_sloc_key: str,
        end_sloc_key: str,
    ):
        s0_sloc_map = snap_mat_sloc_qty.get(start_sloc_key, {})
        s1_sloc_map = snap_mat_sloc_qty.get(end_sloc_key, {})
        start_slocs_by_m = defaultdict(set)
        for (m_k, s_k), q_v in s0_sloc_map.items():
            if q_v > 0 and s_k:
                start_slocs_by_m[m_k].add(s_k)

        # Sum 02 and 01 transfers strictly inside [start_edt, end_edt] (both total and by target SLoc)
        q02_win = defaultdict(float)
        q02_win_by_sloc = defaultdict(float)
        for t in transfers_02_all:
            if start_edt <= t["edt_ts"] <= end_edt:
                m_t = str(t["material"]).strip()
                to_s = str(t.get("to_sloc") or "").strip().upper()
                tq = float(t["transfer_qty"] or 0.0)
                q02_win[m_t] = round(q02_win[m_t] + tq, 2)
                if to_s:
                    q02_win_by_sloc[(m_t, to_s)] = round(q02_win_by_sloc[(m_t, to_s)] + tq, 2)

        q01_win = defaultdict(float)
        q01_win_by_sloc = defaultdict(float)
        for t in transfers_01_all:
            if start_edt <= t["edt_ts"] <= end_edt:
                m_t = str(t["material"]).strip()
                to_s = str(t.get("to_sloc") or "").strip().upper()
                tq = float(t["transfer_qty"] or 0.0)
                q01_win[m_t] = round(q01_win[m_t] + tq, 2)
                if to_s:
                    q01_win_by_sloc[(m_t, to_s)] = round(q01_win_by_sloc[(m_t, to_s)] + tq, 2)

        start_date_str = start_edt[:10]
        start_shortage_mats = set(
            df_mart[
                (df_mart["business_date_edt"] == start_date_str)
                & (df_mart["primary_cause"].isin(["B", "D", "F"]))
            ]["material"].unique()
        )
        gr_batches_before_start = sorted({g["batch_id"] for g in gr_err_all if g["edt_ts"] <= start_edt})
        gr_batches_before_end = sorted({g["batch_id"] for g in gr_err_all if g["edt_ts"] <= end_edt})
        b_start = gr_batches_before_start[-1] if gr_batches_before_start else None
        b_end = gr_batches_before_end[-1] if gr_batches_before_end else None

        gr_qty_at_start = defaultdict(float)
        gr_qty_at_end = defaultdict(float)
        if b_start:
            for g in gr_err_all:
                if g["batch_id"] == b_start:
                    gr_qty_at_start[str(g["material"]).strip()] += abs(float(g.get("error_qty") or g.get("qty") or 0.0))
        if b_end:
            for g in gr_err_all:
                if g["batch_id"] == b_end:
                    gr_qty_at_end[str(g["material"]).strip()] += abs(float(g.get("error_qty") or g.get("qty") or 0.0))

        all_mats = sorted(set(start_map.keys()) | set(end_map.keys()) | set(q02_win.keys()))
        win_cov = get_window_coverage_label(start_edt, end_edt)

        # Query MB51 rows inside [start_edt, end_edt] for all materials in transition + negative control across plant
        mb51_by_mat = defaultdict(list)
        plant_manual_311_to_line_mats = set()
        if c06 is not None:
            if all_mats:
                placeholders = ",".join("?" for _ in all_mats)
                q_sql = f"""
                    SELECT material, mat_doc_no, mat_doc_item, mat_doc_year, sloc, counter_sloc, mvt_type, qty_entry,
                           entry_date, entry_time, doc_header_text, username, entry_datetime_edt
                    FROM mb51_raw
                    WHERE entry_datetime_edt >= ? AND entry_datetime_edt <= ?
                      AND material IN ({placeholders})
                    ORDER BY entry_datetime_edt, mat_doc_no
                """
                for r in c06.execute(q_sql, [start_edt, end_edt, *all_mats]):
                    mb51_by_mat[str(r["material"]).strip()].append(dict(r))

            for r_nc in c06.execute(
                """
                SELECT DISTINCT material, mat_doc_no, sloc, counter_sloc, doc_header_text
                FROM mb51_raw
                WHERE entry_datetime_edt >= ? AND entry_datetime_edt <= ?
                  AND mvt_type = '311' AND qty_entry > 0 AND sloc IN ('SP31', 'SP11', 'SL49')
                """,
                (start_edt, end_edt),
            ):
                d_no = str(r_nc["mat_doc_no"] or "").strip()
                hdr = str(r_nc["doc_header_text"] or "").strip()
                sl = str(r_nc["sloc"] or "").strip().upper()
                csl = str(r_nc["counter_sloc"] or "").strip().upper()
                if d_no not in auto_doc_nos and hdr not in ("Backlog_Clear", "GI_Error_Clear") and csl != sl:
                    plant_manual_311_to_line_mats.add(str(r_nc["material"]).strip())

        rows_detail = []
        unk_decomp_rows = []
        neg_ctrl_backlog_mats = []
        reduced_with_311_mats = set()
        agg = defaultdict(lambda: {"mat_cnt": 0, "reduced_qty": 0.0})
        total_start = round(sum(start_map.values()), 2)
        total_end = round(sum(end_map.values()), 2)
        total_reduced = 0.0
        total_new_added = 0.0
        total_02_raw_in_win = round(sum(q02_win.values()), 2)
        total_02_attributed = 0.0

        for m in all_mats:
            q0 = round(start_map.get(m, 0.0), 2)
            q1 = round(end_map.get(m, 0.0), 2)
            delta = round(q1 - q0, 2)
            q_02 = round(q02_win.get(m, 0.0), 2)
            q_01 = round(q01_win.get(m, 0.0), 2)

            item_start_slocs = start_slocs_by_m.get(m) or mat_cogi_slocs.get(m, {"SP31"})
            per_sloc_rem_cap = {}
            if delta < 0:
                for s_c in item_start_slocs:
                    s_q0 = s0_sloc_map.get((m, s_c), 0.0)
                    s_q1 = s1_sloc_map.get((m, s_c), 0.0)
                    s_red = round(max(0.0, s_q0 - s_q1), 2)
                    if s_red > 0:
                        s_auto = q02_win_by_sloc.get((m, s_c), 0.0) + q01_win_by_sloc.get((m, s_c), 0.0)
                        per_sloc_rem_cap[s_c] = round(max(0.0, s_red - s_auto), 2)

            mb51_st = aggregate_mb51_window_for_material(
                mb51_by_mat.get(m, []),
                item_start_slocs,
                auto_doc_nos,
                per_sloc_cap=per_sloc_rem_cap if delta < 0 else None,
                t0_edt=start_edt,
                t1_edt=end_edt,
            )

            if delta < 0:
                red_q = round(-delta, 2)
                total_reduced = round(total_reduced + red_q, 2)

                # Cap 02 attribution at min(red_q, q_02)
                attr_02 = round(min(red_q, q_02), 2)
                rem_after_02 = round(red_q - attr_02, 2)
                attr_01 = round(min(rem_after_02, q_01), 2)
                rem_after_01 = round(rem_after_02 - attr_01, 2)

                attr_gr = 0.0
                rem_after_gr = 0.0
                if rem_after_01 > 0:
                    gr_cleared_qty = max(0.0, gr_qty_at_start.get(m, 0.0) - gr_qty_at_end.get(m, 0.0))
                    if m in start_shortage_mats and gr_cleared_qty >= rem_after_01:
                        attr_gr = rem_after_01
                    else:
                        rem_after_gr = rem_after_01

                dec = decompose_unknown_with_mb51(rem_after_gr, attr_gr, mb51_st)
                attr_mb51_311 = dec["attr_mb51_311_manual"]
                attr_mb51_gr = dec["attr_mb51_101_131_gr"]
                attr_mb51_scrap = dec["attr_mb51_scrap"]
                attr_mb51_261 = dec["attr_mb51_261_consumed"]
                attr_unk_res = dec["attr_unknown_residual"]

                if mb51_st["raw_mb51_311_unclipped_qty"] > 0:
                    reduced_with_311_mats.add(m)

                if attr_mb51_311 > 0:
                    for rd in mb51_st["manual_311_raw_docs"]:
                        b5_raw_311_doc_rows.append({
                            "transition": label,
                            "obs_end_edt": end_edt,
                            "material": m,
                            "backlog_slocs_at_t0": ",".join(sorted(item_start_slocs)),
                            "reduced_qty": red_q,
                            "pre_mb51_unknown_qty": rem_after_gr,
                            "attributed_311_qty": attr_mb51_311,
                            "mat_doc_no": rd["mat_doc_no"],
                            "mat_doc_item": rd.get("mat_doc_item", "-"),
                            "mvt": rd["mvt_type"],
                            "from_to_sloc": f"{rd['from_sloc']}->{rd['to_sloc']}",
                            "doc_qty": rd["qty"],
                            "cpudt_kst": rd["cpudt_kst"],
                            "entry_datetime_edt": rd["entry_datetime_edt"],
                            "vs_obs_end": rd["vs_obs_end"],
                        })

                total_02_attributed = round(total_02_attributed + attr_02, 2)

                if attr_02 > 0:
                    agg["02_AUTO (백로그 자동 이동전기)"]["mat_cnt"] += 1
                    agg["02_AUTO (백로그 자동 이동전기)"]["reduced_qty"] = round(agg["02_AUTO (백로그 자동 이동전기)"]["reduced_qty"] + attr_02, 2)
                if attr_01 > 0:
                    agg["01_AUTO (GI 에러 자동 이동전기)"]["mat_cnt"] += 1
                    agg["01_AUTO (GI 에러 자동 이동전기)"]["reduced_qty"] = round(agg["01_AUTO (GI 에러 자동 이동전기)"]["reduced_qty"] + attr_01, 2)
                if attr_gr > 0:
                    agg["GR_POSTED (GR 에러 해소/입고 반영)"]["mat_cnt"] += 1
                    agg["GR_POSTED (GR 에러 해소/입고 반영)"]["reduced_qty"] = round(agg["GR_POSTED (GR 에러 해소/입고 반영)"]["reduced_qty"] + attr_gr, 2)
                if attr_mb51_311 > 0:
                    agg["MB51_311_MANUAL (311 수동 이동 보충)"]["mat_cnt"] += 1
                    agg["MB51_311_MANUAL (311 수동 이동 보충)"]["reduced_qty"] = round(agg["MB51_311_MANUAL (311 수동 이동 보충)"]["reduced_qty"] + attr_mb51_311, 2)
                if attr_mb51_gr > 0:
                    agg["MB51_101_131_GR (101/131 입고 반영)"]["mat_cnt"] += 1
                    agg["MB51_101_131_GR (101/131 입고 반영)"]["reduced_qty"] = round(agg["MB51_101_131_GR (101/131 입고 반영)"]["reduced_qty"] + attr_mb51_gr, 2)
                if attr_mb51_scrap > 0:
                    agg["MB51_551_553_593_SCRAP (551/553/593 폐기 처리)"]["mat_cnt"] += 1
                    agg["MB51_551_553_593_SCRAP (551/553/593 폐기 처리)"]["reduced_qty"] = round(agg["MB51_551_553_593_SCRAP (551/553/593 폐기 처리)"]["reduced_qty"] + attr_mb51_scrap, 2)
                if attr_mb51_261 > 0:
                    agg["MB51_261_CONSUMED (261 생산 소비 / 기보유재고 COGI 확정)"]["mat_cnt"] += 1
                    agg["MB51_261_CONSUMED (261 생산 소비 / 기보유재고 COGI 확정)"]["reduced_qty"] = round(agg["MB51_261_CONSUMED (261 생산 소비 / 기보유재고 COGI 확정)"]["reduced_qty"] + attr_mb51_261, 2)
                if attr_unk_res > 0:
                    agg["UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)"]["mat_cnt"] += 1
                    agg["UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)"]["reduced_qty"] = round(agg["UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)"]["reduced_qty"] + attr_unk_res, 2)

                detail_rec = {
                    "transition": label,
                    "mb51_window_coverage": win_cov,
                    "material": m,
                    "cogi_slocs": ",".join(sorted(item_start_slocs)),
                    "start_qty": q0,
                    "end_qty": q1,
                    "reduced_qty": red_q,
                    "raw_02_transfer_qty_in_window": q_02,
                    "attributed_02_qty": attr_02,
                    "raw_01_transfer_qty_in_window": q_01,
                    "attributed_01_qty": attr_01,
                    "attributed_gr_qty": attr_gr,
                    "pre_mb51_unknown_qty": rem_after_gr,
                    "raw_mb51_311_manual_qty": mb51_st["raw_mb51_311_manual_qty"],
                    "raw_mb51_411_consign_qty": mb51_st["raw_mb51_411_consign_qty"],
                    "raw_mb51_101_131_gr_qty": mb51_st["raw_mb51_101_131_gr_qty"],
                    "raw_mb51_scrap_qty": mb51_st["raw_mb51_scrap_qty"],
                    "raw_mb51_261_gi_qty": mb51_st["raw_mb51_261_gi_qty"],
                    "raw_mb51_262_re_qty": mb51_st["raw_mb51_262_re_qty"],
                    "attributed_mb51_311_manual_qty": attr_mb51_311,
                    "attributed_mb51_101_131_gr_qty": attr_mb51_gr,
                    "attributed_mb51_scrap_qty": attr_mb51_scrap,
                    "attributed_mb51_261_consumed_qty": attr_mb51_261,
                    "attributed_unknown_residual_qty": attr_unk_res,
                    "manual_311_users": mb51_st["manual_311_users"],
                    "unreduced_02_transfer_qty": round(max(0.0, q_02 - attr_02), 2),
                }
                rows_detail.append(detail_rec)
                if rem_after_gr > 0:
                    unk_decomp_rows.append(detail_rec)
            else:
                if q0 > 0 and mb51_st["raw_mb51_311_unclipped_qty"] > 0:
                    neg_ctrl_backlog_mats.append(f"{m}({q0:g}->{q1:g}, 311={mb51_st['raw_mb51_311_unclipped_qty']:g})")
                if delta > 0:
                    total_new_added = round(total_new_added + delta, 2)
                if q_02 > 0:
                    rows_detail.append({
                        "transition": label,
                        "mb51_window_coverage": win_cov,
                        "material": m,
                        "cogi_slocs": ",".join(sorted(item_start_slocs)),
                        "start_qty": q0,
                        "end_qty": q1,
                        "reduced_qty": 0.0,
                        "raw_02_transfer_qty_in_window": q_02,
                        "attributed_02_qty": 0.0,
                        "raw_01_transfer_qty_in_window": q_01,
                        "attributed_01_qty": 0.0,
                        "attributed_gr_qty": 0.0,
                        "pre_mb51_unknown_qty": 0.0,
                        "raw_mb51_311_manual_qty": mb51_st["raw_mb51_311_manual_qty"],
                        "raw_mb51_411_consign_qty": mb51_st["raw_mb51_411_consign_qty"],
                        "raw_mb51_101_131_gr_qty": mb51_st["raw_mb51_101_131_gr_qty"],
                        "raw_mb51_scrap_qty": mb51_st["raw_mb51_scrap_qty"],
                        "raw_mb51_261_gi_qty": mb51_st["raw_mb51_261_gi_qty"],
                        "raw_mb51_262_re_qty": mb51_st["raw_mb51_262_re_qty"],
                        "attributed_mb51_311_manual_qty": 0.0,
                        "attributed_mb51_101_131_gr_qty": 0.0,
                        "attributed_mb51_scrap_qty": 0.0,
                        "attributed_mb51_261_consumed_qty": 0.0,
                        "attributed_unknown_residual_qty": 0.0,
                        "manual_311_users": mb51_st["manual_311_users"],
                        "unreduced_02_transfer_qty": q_02,
                    })

        b5_neg_ctrl_rows.append({
            "transition": label,
            "window_edt": f"{start_edt} ~ {end_edt}",
            "mb51_coverage": win_cov,
            "start_backlog_mats": sum(1 for v in start_map.values() if v > 0),
            "reduced_mats_with_manual_311": len(reduced_with_311_mats),
            "attributed_311_mats": agg["MB51_311_MANUAL (311 수동 이동 보충)"]["mat_cnt"],
            "attributed_311_qty": round(agg["MB51_311_MANUAL (311 수동 이동 보충)"]["reduced_qty"], 2),
            "neg_ctrl_backlog_no_reduction_cnt": len(neg_ctrl_backlog_mats),
            "neg_ctrl_backlog_materials": ", ".join(neg_ctrl_backlog_mats) if neg_ctrl_backlog_mats else "-",
            "neg_ctrl_plant_311_to_line_total_mats": len(plant_manual_311_to_line_mats),
            "neg_ctrl_plant_no_backlog_reduction_cnt": len(plant_manual_311_to_line_mats - reduced_with_311_mats),
            "unknown_residual_mats": agg["UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)"]["mat_cnt"],
            "unknown_residual_qty": round(agg["UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)"]["reduced_qty"], 2),
        })

        summary_list = []
        for cat in [
            "02_AUTO (백로그 자동 이동전기)",
            "01_AUTO (GI 에러 자동 이동전기)",
            "GR_POSTED (GR 에러 해소/입고 반영)",
            "MB51_311_MANUAL (311 수동 이동 보충)",
            "MB51_101_131_GR (101/131 입고 반영)",
            "MB51_551_553_593_SCRAP (551/553/593 폐기 처리)",
            "MB51_261_CONSUMED (261 생산 소비 / 기보유재고 COGI 확정)",
            "UNKNOWN_RESIDUAL (MB51 무전표 — COGI 삭제·미수집 구간)",
        ]:
            v = agg[cat]
            pct = round((v["reduced_qty"] / total_reduced) * 100.0, 1) if total_reduced > 0 else 0.0
            summary_list.append({
                "transition": label,
                "window_edt": f"{start_edt} ~ {end_edt}",
                "mb51_window_coverage": win_cov,
                "start_total_qty": round(total_start, 2),
                "end_total_qty": round(total_end, 2),
                "gross_reduced_qty": round(total_reduced, 2),
                "gross_new_added_qty": round(total_new_added, 2),
                "net_change_qty": round(total_end - total_start, 2),
                "raw_02_transfer_total_in_window": round(total_02_raw_in_win, 2),
                "resolution_category": cat,
                "resolved_material_count": v["mat_cnt"],
                "attributed_reduced_qty": round(v["reduced_qty"], 2),
                "share_of_reduction_pct": pct,
            })
        return summary_list, rows_detail, unk_decomp_rows

    t_25 = batch_min_edt.get("2026-10-25", "2026-10-25 11:14:56")
    t_26 = batch_min_edt.get("2026-10-26", "2026-10-26 06:10:47")
    t_27 = batch_min_edt.get("2026-10-27", "2026-10-27 06:21:47")
    t_28 = batch_min_edt.get("2026-10-28", "2026-10-28 06:21:28")
    t_29 = batch_min_edt.get("2026-10-29", "2026-10-29 06:21:14")

    s0, d0, u0 = attribute_transition_exact(
        "0) 10/25 11:14 -> 10/26 06:10 (일일)",
        daily_mat_qty["2026-10-25"],
        daily_mat_qty["2026-10-26"],
        t_25,
        t_26,
        "2026-10-25",
        "2026-10-26",
    )
    s1, d1, u1 = attribute_transition_exact(
        "1) 10/26 06:10 -> 10/27 06:21 (일일)",
        daily_mat_qty["2026-10-26"],
        daily_mat_qty["2026-10-27"],
        t_26,
        t_27,
        "2026-10-26",
        "2026-10-27",
    )
    s2, d2, u2 = attribute_transition_exact(
        "2) 10/27 06:21 -> 10/28 06:21 (일일)",
        daily_mat_qty["2026-10-27"],
        daily_mat_qty["2026-10-28"],
        t_27,
        t_28,
        "2026-10-27",
        "2026-10-28",
    )
    s3, d3, u3 = attribute_transition_exact(
        "3) 10/28 06:21 -> 10/28 08:15 (10/28 오전)",
        daily_mat_qty["2026-10-28"],
        qty_1028_0815,
        t_28,
        "2026-10-28 08:15:27",
        "2026-10-28",
        "2026-10-28_0815",
    )
    s4, d4, u4 = attribute_transition_exact(
        "4) 10/28 06:21 -> 10/28 18:05 (10/28 주간 누적 Raw)",
        daily_mat_qty["2026-10-28"],
        qty_1805_raw_map,
        t_28,
        "2026-10-28 18:05:50",
        "2026-10-28",
        "2026-10-28_1805",
    )
    s5, d5, u5 = attribute_transition_exact(
        "5) 10/28 06:21 -> 10/29 06:21 (일일)",
        daily_mat_qty["2026-10-28"],
        daily_mat_qty.get("2026-10-29", {}),
        t_28,
        t_29,
        "2026-10-28",
        "2026-10-29",
    )
    s6, d6, u6 = attribute_transition_exact(
        "6) 10/29 06:21 -> 10/29 08:15 (10/29 오전)",
        daily_mat_qty.get("2026-10-29", {}),
        qty_1029_0815,
        t_29,
        "2026-10-29 08:15:25",
        "2026-10-29",
        "2026-10-29_0815",
    )

    df_res_summary = pd.DataFrame(s0 + s1 + s2 + s3 + s4 + s5 + s6)
    df_res_detail = pd.DataFrame(d0 + d1 + d2 + d3 + d4 + d5 + d6)
    df_unk_decomp = pd.DataFrame(u0 + u1 + u2 + u3 + u4 + u5 + u6)
    df_b5_raw_docs = pd.DataFrame(b5_raw_311_doc_rows)
    df_b5_neg_ctrl = pd.DataFrame(b5_neg_ctrl_rows)

    # =========================================================================
    # DELIVERABLE 5: Multi-Signal / Split Cases & D108 GI-Blocked Reclassified Table
    # =========================================================================
    df_1028 = df_mart[df_mart["business_date_edt"] == "2026-10-28"].copy()
    edge_candidates = df_1028[df_1028["aux_flags"] != "-"].sort_values("deficit_qty", ascending=False).head(10)
    df_edge_cases = edge_candidates[[
        "business_date_edt", "material", "material_desc", "orig_material_deficit_qty", "deficit_qty",
        "deficit_amount_usd", "plan_status", "plan_transfer_qty",
        "primary_cause", "detailed_cause_code", "detailed_cause_name", "owner_team",
        "aux_flags", "severity_group",
        "plant_sap_qty", "plant_wms_qty", "wh_sap_qty", "wh_wms_qty", "sp31_sap_qty",
        "wms_line_zone_qty", "wms_rack_zone_qty", "evidence_detail"
    ]].copy()

    df_c_split = df_mart[df_mart["primary_cause"] == "C"][[
        "business_date_edt", "material", "material_desc", "deficit_qty", "deficit_amount_usd",
        "consecutive_backlog_days", "c_sub_code", "detailed_cause_name", "owner_team",
        "severity_group", "aux_flags", "wms_line_zone_qty", "wms_rack_zone_qty", "evidence_detail"
    ]].copy()

    combined_for_d108 = [df_mart]
    if not df_1805.empty:
        combined_for_d108.append(df_1805)
    if not df_1029_0815.empty:
        combined_for_d108.append(df_1029_0815)
    df_all_snaps = pd.concat(combined_for_d108, ignore_index=True)
    df_d108_reclassified = df_all_snaps[df_all_snaps["d108_cause_code"] != "-"][[
        "business_date_edt", "snapshot_time_edt", "material", "material_desc", "slocs",
        "orig_material_deficit_qty", "deficit_qty", "plan_status",
        "d108_cause_code", "primary_cause", "detailed_cause_code", "detailed_cause_name",
        "severity_group", "d108_linked_gi", "d108_01_verdict", "d108_d107_status",
        "d108_is_bug_candidate", "plant_sap_qty", "plant_wms_qty", "evidence_detail"
    ]].copy()

    df_sp31_acc_linked = df_all_snaps[df_all_snaps["ref_01_accuracy_link"] != "-"][[
        "business_date_edt", "snapshot_time_edt", "material", "material_desc", "slocs",
        "orig_material_deficit_qty", "deficit_qty", "plan_status",
        "primary_cause", "detailed_cause_code", "detailed_cause_name", "severity_group",
        "ref_01_accuracy_link", "ref_01_accuracy_qty_72h", "ref_01_accuracy_detail",
        "ref_01_mb51_doc_check", "sp31_sap_qty", "wh_sap_qty", "wh_wms_qty"
    ]].copy()

    # =========================================================================
    # Save Excel Deliverable in Scratch (Timestamped + Canonical Copy per Rule #7 & #179)
    # =========================================================================
    ts_now = datetime.now(EDT).strftime("%Y%m%d_%H%M%S")
    save_dirs = (OUT_DIR,) if OUT_DIR.resolve() != MAIN_OUT_DIR.resolve() else (OUT_DIR,)
    for target_dir in save_dirs:
        ts_excel = target_dir / f"DRYRUN_D103_Backlog_RootCause_Mart_{ts_now}.xlsx"
        latest_excel = target_dir / "D103_Backlog_RootCause_Mart_latest.xlsx"
        with pd.ExcelWriter(ts_excel, engine="openpyxl") as writer:
            df_ts_audit.to_excel(writer, sheet_name="1_Timestamp_Basis_D103_1", index=False)
            df_mart.to_excel(writer, sheet_name="2_Material_Date_Mart", index=False)
            df_cause_summary.to_excel(writer, sheet_name="3_Daily_Cause_Summary", index=False)
            df_res_summary.to_excel(writer, sheet_name="4_Resolution_Summary", index=False)
            df_res_detail.to_excel(writer, sheet_name="4B_Resolution_Item_Detail", index=False)
            df_unk_decomp.to_excel(writer, sheet_name="4C_UNKNOWN_MB51_Decomp", index=False)
            df_b5_raw_docs.to_excel(writer, sheet_name="4D_B5_Manual311_RawDocs", index=False)
            df_b5_neg_ctrl.to_excel(writer, sheet_name="4E_B5_Negative_Control", index=False)
            df_1805.to_excel(writer, sheet_name="5_Live_1028_1805_Mart", index=False)
            df_1805_summary.to_excel(writer, sheet_name="5B_Live_1028_1805_Summary", index=False)
            df_edge_cases.to_excel(writer, sheet_name="6_MultiSignal_Split_Cases", index=False)
            df_c_split.to_excel(writer, sheet_name="7_00_Barcode_C_Split_165", index=False)
            df_d108_reclassified.to_excel(writer, sheet_name="8_D108_GI_Reclassified", index=False)
            if not df_1029_0815.empty:
                df_1029_0815.to_excel(writer, sheet_name="9_1029_0815_Mart", index=False)
            df_sp31_acc_linked.to_excel(writer, sheet_name="10_SP31_01_Accuracy_Link", index=False)
        shutil.copyfile(ts_excel, latest_excel)
        print(f"Saved Excel deliverable: {ts_excel} -> {latest_excel.name} ({ts_excel.stat().st_size:,} bytes)")

    def _mask_user_list(u_csv: str) -> str:
        if not u_csv:
            return "-"
        out = []
        for u in str(u_csv).split(","):
            u = u.strip()
            if not u:
                continue
            out.append(u if u in ("SLBC", "SAMIT") else "<SAP_USER>")
        return ",".join(dict.fromkeys(out)) if out else "-"

    # =========================================================================
    # Auto-Generate Verbatim Markdown Tables for Handoff 164b / 184 (Zero Hand-Typing!)
    # =========================================================================
    max_ord_date = ordered_dates[-1] if ordered_dates else "2026-10-29"
    md_lines = []
    md_lines.append(f"### [Auto-Generated by `d103_mart_builder.py`] 1. 일자별 백로그 원인 분류 요약 (`2026-10-23 ~ {max_ord_date} 06:20`)")
    md_lines.append("")
    md_lines.append("| 일자 (`EDT`) | 심각도 그룹 | 대분류 | 세부코드 | 세부 원인명 | 담당팀 | 행수 | 품목수 | 부족수량 (`EA`) | `02` 계획이동 (`EA`) | 단가보유 금액 (`USD`) | 단가미보유 (`행 / EA`) |")
    md_lines.append("| :--- | :--- | :---: | :---: | :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, r in df_cause_summary.iterrows():
        md_lines.append(
            f"| `{r['snapshot_key']}` | {r['severity_group']} | **{r['primary_cause']}** | **{r['detailed_cause_code']}** | {r['detailed_cause_name']} | **{r['owner_team']}** | {r['row_count']} | {r['distinct_materials']} | {r['total_deficit_qty']:,.1f} | {r['plan_transfer_qty']:,.1f} | \\${r['priced_amount_usd']:,.2f} ({r['priced_row_count']}행) | {r['no_price_row_count']}행 / {r['no_price_deficit_qty']:,.1f} |"
        )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 2. 일자별 총합 검증 (`raw_backlog_history` 및 `02` Plan 대비 100% 일치)")
    md_lines.append("")
    md_lines.append("| 일자 (`EDT`) | 원천 `raw_backlog_history` 총 수량 (`EA`) | 마트 합산 총 수량 (`EA`) | 🔴 심각한 문제 (`A·D·F·C3·D108②③`) | 🟡 일시적 체류 (`B·R·C1·C2·D108①·COGI대기`) | ⚪ 기타 (`마스터·제외mvt`) | `02` 계획 이동량 (`EA`) |")
    md_lines.append("| :--- | ---: | ---: | ---: | ---: | ---: | ---: |")
    for b_d in ordered_dates:
        sub = df_mart[df_mart["business_date_edt"] == b_d]
        raw_sum = round(sum(daily_mat_qty[b_d].values()), 1)
        mart_sum = round(float(sub["deficit_qty"].sum()), 1)
        crit_sum = round(float(sub[sub["severity_group"].str.startswith("CRITICAL")]["deficit_qty"].sum()), 1)
        temp_sum = round(float(sub[sub["severity_group"].str.startswith("TEMPORARY")]["deficit_qty"].sum()), 1)
        oth_sum = round(float(sub[sub["severity_group"].str.startswith("OTHER")]["deficit_qty"].sum()), 1)
        xf_sum = round(float(sub["plan_transfer_qty"].sum()), 1)
        md_lines.append(f"| `{b_d}` | **{raw_sum:,.1f}** | **{mart_sum:,.1f}** | {crit_sum:,.1f} | {temp_sum:,.1f} | {oth_sum:,.1f} | {xf_sum:,.1f} |")

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 3. `D108` 공통 판정(`gi_verdict_shared.py`) 기반 `GI 에러 연계 건` 재분류 전수 내역 (`02`·`03`·`D107` 100% 단일 함수 공유)")
    md_lines.append("")
    md_lines.append("| 기준일/회차 (`EDT`) | 품목코드 (`material`) | COGI SLoc | 행수량 (`EA`) | 원 `02` 상태 | `D108` 판정 코드 | `03` 재분류 세부코드 | 심각도 | 연결 `GI` 에러 요약 | 직전 `01` 판정 | `D107` 삭제후보 | 버그점검 |")
    md_lines.append("| :--- | :--- | :---: | ---: | :--- | :---: | :---: | :--- | :--- | :--- | :---: | :---: |")
    for _, r in df_d108_reclassified.iterrows():
        md_lines.append(
            f"| `{r['business_date_edt']}` (`{r['snapshot_time_edt'][11:]}`) | `{r['material']}` | `{r['slocs']}` | **{r['deficit_qty']:,.1f}** | `{r['plan_status']}` | **`{r['d108_cause_code']}`** | **`{r['detailed_cause_code']}`** ({r['detailed_cause_name']}) | {r['severity_group']} | {r['d108_linked_gi']} | `{r['d108_01_verdict']}` | `{r['d108_d107_status']}` | `{'YES' if r['d108_is_bug_candidate'] else 'NO'}` |"
        )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 4. 해소 경로 귀속 검증 테이블 (`D103-6` + `MB51` `2026-10-20~2026-10-28` `CPUDT` 연결 — 시간 윈도우 일치 및 품목별 캡 적용)")
    md_lines.append("")
    md_lines.append("| 전환 구간 (`Transition`) | 정확한 `EDT` 윈도우 | `MB51` 커버리지 | 총 감소량 (`EA`) | 총 신규증가 (`EA`) | 윈도우 내 `02` 원 이동량 (`EA`) | 해소 경로 (`resolution_category`) | 귀속 품목수 | 귀속 감소량 (`EA`) | 감소 비중 (`%`) |")
    md_lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | :--- | ---: | ---: | ---: |")
    for _, r in df_res_summary.iterrows():
        md_lines.append(
            f"| {r['transition']} | `{r['window_edt']}` | `{r['mb51_window_coverage']}` | {r['gross_reduced_qty']:,.2f} | {r['gross_new_added_qty']:,.2f} | **{r['raw_02_transfer_total_in_window']:,.2f}** | {r['resolution_category']} | {r['resolved_material_count']} | **{r['attributed_reduced_qty']:,.2f}** | {r['share_of_reduction_pct']:.1f}% |"
        )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 5. 구 `UNKNOWN` 해소분 품목별 `MB51` 이동유형 분해 상세 (`pre_mb51_unknown_qty > 0`)")
    md_lines.append("")
    md_lines.append("| 전환 구간 | `MB51` 커버리지 | 품목코드 (`material`) | COGI SLoc | 구 `UNKNOWN` (`EA`) | 원 `311` 수동 (`EA`) | 원 `101/131` 입고 (`EA`) | 원 `551/553/593` 폐기 (`EA`) | 원 `261/262` 소비 (`EA`) | 귀속 `311` (`EA`) | 귀속 `101/131` (`EA`) | 귀속 폐기 (`EA`) | 귀속 `261` (`EA`) | 잔여 무전표 (`EA`) | `311` 작업자 |")
    md_lines.append("| :--- | :--- | :--- | :---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :---: |")
    if not df_unk_decomp.empty:
        for _, r in df_unk_decomp.iterrows():
            raw_261_tot = round(float(r["raw_mb51_261_gi_qty"]) + float(r["raw_mb51_262_re_qty"]), 2)
            md_lines.append(
                f"| {r['transition']} | `{r['mb51_window_coverage']}` | `{r['material']}` | `{r['cogi_slocs']}` | **{r['pre_mb51_unknown_qty']:,.2f}** | {r['raw_mb51_311_manual_qty']:,.2f} | {r['raw_mb51_101_131_gr_qty']:,.2f} | {r['raw_mb51_scrap_qty']:,.2f} | {raw_261_tot:,.2f} | **{r['attributed_mb51_311_manual_qty']:,.2f}** | **{r['attributed_mb51_101_131_gr_qty']:,.2f}** | **{r['attributed_mb51_scrap_qty']:,.2f}** | **{r['attributed_mb51_261_consumed_qty']:,.2f}** | **{r['attributed_unknown_residual_qty']:,.2f}** | `{_mask_user_list(r['manual_311_users'])}` |"
            )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 6. `[187 B-5]` 음성 대조 (`Negative Control`) 및 강화 후 `UNKNOWN_RESIDUAL` 요약")
    md_lines.append("")
    md_lines.append("| 전환 구간 | 정확한 `EDT` 윈도우 | `MB51` 커버리지 | 기초 백로그 품번수 | 수동 `311` 발생+감소 품번수 | `MB51_311_MANUAL` 귀속 (`품번 / EA`) | 음성대조 A: 기초 백로그 중 `311` 발생했으나 미감소(`Δ≥0`) 품번수 (상세) | 음성대조 B: 전사 라인향(`SP31/SP11/SL49`) 수동 `311` 중 백로그 미감소 품번수 | 강화 후 `UNKNOWN_RESIDUAL` (`품번 / EA`) |")
    md_lines.append("| :--- | :--- | :--- | ---: | ---: | ---: | :--- | ---: | ---: |")
    for _, r in df_b5_neg_ctrl.iterrows():
        md_lines.append(
            f"| {r['transition']} | `{r['window_edt']}` | `{r['mb51_coverage']}` | {r['start_backlog_mats']} | {r['reduced_mats_with_manual_311']} | **{r['attributed_311_mats']}품번 / {r['attributed_311_qty']:,.2f} EA** | **{r['neg_ctrl_backlog_no_reduction_cnt']}품번** (`{r['neg_ctrl_backlog_materials']}`) | **{r['neg_ctrl_plant_no_backlog_reduction_cnt']}** / {r['neg_ctrl_plant_311_to_line_total_mats']} | **{r['unknown_residual_mats']}품번 / {r['unknown_residual_qty']:,.2f} EA** |"
        )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 7. `[187 B-5]` `MB51_311_MANUAL` 귀속 품목별 `MB51` 원문 전표 내역")
    md_lines.append("")
    md_lines.append("| 전환 구간 | 감소 관측 시각 (`EDT`) | 품번 (`material`) | 기초 백로그 SLoc | 총 감소량 (`EA`) | 구 `UNKNOWN` (`EA`) | 귀속 `311` (`EA`) | `MB51` 문서번호 | 품목번호 (`ZEILE`) | `mvt` | `from->to SLoc` | 전표 수량 (`EA`) | `CPUDT+CPUTM` (`KST`) | `EDT` 환산 시각 | 감소 시점 대비 |")
    md_lines.append("| :--- | :--- | :--- | :---: | ---: | ---: | ---: | :--- | :---: | :---: | :---: | ---: | :--- | :--- | :---: |")
    if not df_b5_raw_docs.empty:
        for _, r in df_b5_raw_docs.iterrows():
            md_lines.append(
                f"| {r['transition']} | `{r['obs_end_edt']}` | `{r['material']}` | `{r['backlog_slocs_at_t0']}` | {r['reduced_qty']:,.2f} | {r['pre_mb51_unknown_qty']:,.2f} | **{r['attributed_311_qty']:,.2f}** | `{r['mat_doc_no']}` | `{r.get('mat_doc_item', '-')}` | `{r['mvt']}` | `{r['from_to_sloc']}` | {r['doc_qty']:,.2f} | `{r['cpudt_kst']}` | `{r['entry_datetime_edt']}` | `{r['vs_obs_end']}` |"
            )

    md_lines.append("")
    md_lines.append("### [Auto-Generated by `d103_mart_builder.py`] 8. `[199]` `SP31` 백로그 중 직전 72h 내 `01` 창고 정확도 이동 연계 (`ref_01_accuracy_link`) 참고 표시 내역 (원인 코드 현행 유지)")
    md_lines.append("")
    md_lines.append("| 기준일/회차 (`EDT`) | 품목코드 (`material`) | COGI SLoc | 부족수량 (`EA`) | 원 `02` 상태 | 원인 코드 (`유지`) | 심각도 (`유지`) | 참고 열 (`ref_01_accuracy_link`) | 직전 72h `01` `SP31->SL0x` 이동량 (`EA`) | `01` 정확도 이동 전표 상세 (`transfer_posting_history` `executed_at` EDT 원문) | `MB51` `doc_number` 대조 (불일치 경고 열) |")
    md_lines.append("| :--- | :--- | :---: | ---: | :--- | :---: | :---: | :---: | ---: | :--- | :--- |")
    if not df_sp31_acc_linked.empty:
        for _, r in df_sp31_acc_linked.iterrows():
            md_lines.append(
                f"| `{r['business_date_edt']}` (`{r['snapshot_time_edt'][11:]}`) | `{r['material']}` | `{r['slocs']}` | **{r['deficit_qty']:,.1f}** | `{r['plan_status']}` | **`{r['detailed_cause_code']}`** ({r['detailed_cause_name']}) | {r['severity_group']} | **`{r['ref_01_accuracy_link']}`** | **{r['ref_01_accuracy_qty_72h']:,.1f}** | {r['ref_01_accuracy_detail']} | `{r['ref_01_mb51_doc_check']}` |"
            )

    md_out_path = OUT_DIR / "d103_164b_tables.md"
    md_out_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"Saved verbatim markdown tables to: {md_out_path}")

    for c in (c00, c01, c02, c04, c05):
        c.close()
    if c06 is not None:
        c06.close()


if __name__ == "__main__":
    build_d103_step1()
