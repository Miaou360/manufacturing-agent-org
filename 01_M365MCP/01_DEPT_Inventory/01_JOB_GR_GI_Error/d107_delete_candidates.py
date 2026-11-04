"""
D107 Phase 1 — Unresolvable GI Error Delete Candidate Evaluator & Post-07:00 Collector
=======================================================================================
01_DEPT_Inventory / 01_JOB_GR_GI_Error (#180 · #181 · #182 · #184)

Phase 1 Contract (Read-Only Candidate Verification):
  - Target movements: 311 / 312 ONLY (901 / 902 and others excluded).
  - Target date scope: error_date <= previous calendar day (today's errors excluded).
  - Timing (#182):
      1. After 07:00 cycle completes (~07:03~07:05), collect FRESH ZTCODE_STOCK_REC GI/GR + MB52 + WMS
         around 07:18~07:20 using `sap_session_lock` (NEVER reuse 07:00 pre-posting snapshot!).
      2. Compute `cleared_by_0700_count` (items present in 07:00 pre-posting snapshot that
         disappeared after 07:00 cycle postings).
      3. Include "삭제 기안 후보 (1단계 검증용)" section in 07:30 D106 daily GI report showing
         collection timestamp, 07:00-cleared count, candidate table (with 1~5 evidence &
         remaining SAP-WMS diff note), "보류(사람 확인)" rows, and 08:00 post-check column.
      4. After 08:00 cycle completes (~08:08), compare candidates against post-08:00 state and
         emit handoff alert if any candidate was resolved by 01, gained GR linkage, or became
         resolvable via new stock inflow.
  - Strictly NO portal access in Phase 1 (2단계 전까지 웹 포털 접속 절대 금지).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

JOB_DIR = Path(__file__).resolve().parent
DEPT_DIR = JOB_DIR.parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
for _p in (JOB_DIR, DEPT_DIR, HQ_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from gi_verdict_shared import (  # noqa: E402
    DEFAULT_GI_DB,
    DEFAULT_GI_PLAN_DIR,
    DEFAULT_OPS_RAW_DB,
    D107_TARGET_MVTS,
    evaluate_gi_errors_as_of,
    parse_ztcode_stock_rec_gi_records_detailed,
    parse_ztcode_stock_rec_gr_records_detailed,
)

logger = logging.getLogger("D107DeleteCandidates")


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=5.0)


def _sig5(rec: Dict[str, Any]) -> Tuple[str, str, str, str, float]:
    return (
        str(rec.get("material") or rec.get("Material") or "").strip(),
        str(rec.get("mvt") or rec.get("Mvt_Original") or "").strip(),
        str(rec.get("from_sloc") or rec.get("From_SLoc") or "").strip().upper(),
        str(rec.get("to_sloc") or rec.get("To_SLoc") or "").strip().upper(),
        round(float(rec.get("qty") or rec.get("Transfer_Qty") or 0.0), 3),
    )


def _sig_occ_list(records: List[Dict[str, Any]]) -> List[Tuple[Tuple[str, str, str, str, float, str, int], Dict[str, Any]]]:
    """Assigns 1-based occurrence index per identical 6-tuple so multi-box lines (e.g. MAT_DUP480 x2) are tracked individually (#187 B-3)."""
    seen: Dict[Tuple[str, str, str, str, float, str], int] = {}
    out: List[Tuple[Tuple[str, str, str, str, float, str, int], Dict[str, Any]]] = []
    for r in records:
        k6 = (
            *_sig5(r),
            str(r.get("error_date") or r.get("Error_Date") or "").strip(),
        )
        occ = int(r.get("line_occ") or 0)
        if occ <= 0:
            seen[k6] = seen.get(k6, 0) + 1
            occ = seen[k6]
        else:
            seen[k6] = max(seen.get(k6, 0), occ)
        out.append(((*k6, occ), r))
    return out


def _load_batch_pre_posting_31x_prev_day(
    batch_id: str,
    report_date_str: str,
    gi_db_path: Path,
) -> List[Dict[str, Any]]:
    """Loads 311/312 GI errors with error_date < report_date from a given batch's pre-posting snapshot."""
    if not gi_db_path or not Path(gi_db_path).exists() or not batch_id:
        return []
    rep_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()
    out: List[Dict[str, Any]] = []
    with _connect_ro(Path(gi_db_path)) as conn:
        rows = conn.execute(
            """
            SELECT material, material_desc, mvt, from_sloc, to_sloc, qty, error_date, message
            FROM gi_error_snapshots
            WHERE batch_id = ?
            ORDER BY id ASC
            """,
            (batch_id,),
        ).fetchall()
    for r in rows:
        mvt = str(r[2] or "").strip().replace(".0", "")
        if mvt not in D107_TARGET_MVTS:
            continue
        ed_str = str(r[6] or "").strip()
        try:
            ed = datetime.strptime(ed_str.replace(".", "-").replace("/", "-"), "%Y-%m-%d").date()
        except ValueError:
            ed = None
        if ed is not None and ed < rep_date:
            out.append(
                {
                    "material": str(r[0] or "").strip(),
                    "material_desc": str(r[1] or "").strip(),
                    "mvt": mvt,
                    "from_sloc": str(r[3] or "").strip().upper(),
                    "to_sloc": str(r[4] or "").strip().upper(),
                    "qty": float(r[5] or 0.0),
                    "error_date": ed_str,
                    "message": str(r[7] or "").strip(),
                }
            )
    return out


def _find_morning_batches(
    report_date_str: str,
    gi_db_path: Path,
    gi_plan_dir: Optional[Path] = None,
) -> Dict[str, Optional[Tuple[str, str, int]]]:
    """
    Finds key morning batches on `report_date_str`:
      - batch_0700: 07:00 cycle (`07:00`~`07:19`)
      - batch_post_0700: earliest completed cycle strictly after 07:00 (`07:20`~`08:29`, e.g. `08:00`)
      - batch_post_0800: earliest completed cycle after 08:00 (`>= 08:05`, e.g. `08:30` or `09:00`)
    """
    res: Dict[str, Optional[Tuple[str, str, int]]] = {
        "batch_0700": None,
        "batch_post_0700": None,
        "batch_post_0800": None,
    }
    if not gi_db_path or not Path(gi_db_path).exists():
        return res

    ymd = report_date_str.replace("-", "")
    with _connect_ro(Path(gi_db_path)) as conn:
        rows = conn.execute(
            """
            SELECT batch_id, datetime(run_timestamp, 'localtime') AS local_ts, postings_executed
            FROM pipeline_run_metrics
            WHERE batch_id LIKE ? AND status = 'COMPLETED'
            ORDER BY id ASC
            """,
            (f"RUN_{ymd}_%",),
        ).fetchall()

    for bid, lts, pexec in rows:
        hm = str(bid).split("_")[-1][:4]
        tup = (str(bid), str(lts or ""), int(pexec or 0))
        if "0700" <= hm <= "0719" and res["batch_0700"] is None:
            res["batch_0700"] = tup
        elif "0720" <= hm <= "0759" and res["batch_post_0700"] is None:
            res["batch_post_0700"] = tup
        elif "0800" <= hm <= "0825" and res["batch_post_0700"] is None:
            res["batch_post_0700"] = tup
        if hm >= "0800" and res["batch_post_0800"] is None:
            res["batch_post_0800"] = tup

    # #215 / #222 fallback: when 07:00 or 08:00+ cycle had 0 active GI errors, hourly_gi_pipeline writes
    # GI_Resolution_Plan_YYYYMMDD_*.xlsx (or GI_Error_YYYYMMDD_*.XLS) and skips pipeline_run_metrics.
    if res["batch_0700"] is None and gi_plan_dir and Path(gi_plan_dir).exists():
        ymd_f07 = report_date_str.replace("-", "")
        for pf07 in sorted(Path(gi_plan_dir).glob(f"GI_Resolution_Plan_{ymd_f07}_07*.xlsx")):
            hhmmss07 = pf07.stem.split("_")[-1]
            if len(hhmmss07) >= 6 and "0700" <= hhmmss07[:4] <= "0719":
                ts07 = f"{report_date_str} {hhmmss07[:2]}:{hhmmss07[2:4]}:{hhmmss07[4:6]}"
                res["batch_0700"] = (f"PLAN_{ymd_f07}_{hhmmss07}", ts07, 0)
                break
    if res["batch_post_0800"] is None and gi_plan_dir and Path(gi_plan_dir).exists():
        ymd_f = report_date_str.replace("-", "")
        plan_files = sorted(Path(gi_plan_dir).glob(f"GI_Resolution_Plan_{ymd_f}_*.xlsx"))
        for pf in plan_files:
            parts = pf.stem.split("_")
            hhmmss = parts[-1] if parts else ""
            if len(hhmmss) >= 6 and hhmmss[:4] >= "0800":
                ts_str = f"{report_date_str} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
                res["batch_post_0800"] = (f"PLAN_{ymd_f}_{hhmmss}", ts_str, 0)
                break
        if res["batch_post_0800"] is None:
            gi_err_dir = Path(gi_plan_dir).parent / "gi_errors"
            if gi_err_dir.exists():
                for ef in sorted(gi_err_dir.glob(f"GI_Error_{ymd_f}_*.XLS")):
                    hhmmss = ef.stem.split("_")[-1]
                    if len(hhmmss) >= 6 and hhmmss[:4] >= "0800":
                        ts_str = f"{report_date_str} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
                        res["batch_post_0800"] = (f"FILE_{ymd_f}_{hhmmss}", ts_str, 0)
                        break
    return res


def _compute_0700_cleared_summary(
    report_date_str: str,
    current_verdicts: List[Dict[str, Any]],
    gi_db_path: Path,
) -> Tuple[int, List[Dict[str, Any]], Optional[str]]:
    """
    Computes how many prev-day 311/312 GI errors present in `RUN_YYYYMMDD_070000` pre-posting snapshot
    were cleared by the 07:00 cycle (absent from the fresh post-07:00 list).
    """
    batches = _find_morning_batches(report_date_str, gi_db_path)
    b0700 = batches.get("batch_0700")
    if not b0700:
        return 0, [], None

    b0700_id = b0700[0]
    pre_0700_items = _load_batch_pre_posting_31x_prev_day(b0700_id, report_date_str, gi_db_path)
    cur_occ_sigs = {k for k, _ in _sig_occ_list(current_verdicts)}
    cleared_items = [it for k, it in _sig_occ_list(pre_0700_items) if k not in cur_occ_sigs]
    return len(cleared_items), cleared_items, b0700_id


def attach_post_0800_comparison(
    payload: Dict[str, Any],
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
    post_0800_as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compares each candidate in `payload` against the post-08:00 cycle (`>= 08:25` or explicit `post_0800_as_of`)
    and populates `post_0800_status`, `post_0800_changed`, and `post_0800_batch_id`.
    """
    rep_date = str(payload.get("report_date") or "")
    if not rep_date:
        return payload

    batches = _find_morning_batches(rep_date, gi_db_path, gi_plan_dir=gi_plan_dir)
    b0830 = batches.get("batch_post_0800")

    target_batch_id: Optional[str] = None
    target_as_of: Optional[str] = post_0800_as_of
    is_file_only_fallback = False
    fallback_plan_df: Optional[pd.DataFrame] = None
    fallback_gi_recs: Optional[List[Dict[str, Any]]] = None

    if not target_as_of and b0830:
        b_id = b0830[0]
        target_as_of = b0830[1] or f"{rep_date} 08:35:00"
        if b_id.startswith(("PLAN_", "FILE_")):
            is_file_only_fallback = True
            hhmmss = b_id.split("_")[-1]
            ymd_f = rep_date.replace("-", "")
            pf = Path(gi_plan_dir) / f"GI_Resolution_Plan_{ymd_f}_{hhmmss}.xlsx" if gi_plan_dir else None
            if pf and pf.exists():
                try:
                    fallback_plan_df = pd.read_excel(pf)
                except Exception:
                    fallback_plan_df = pd.DataFrame()
            else:
                fallback_plan_df = pd.DataFrame()
            gi_err_dir = Path(gi_plan_dir).parent / "gi_errors" if gi_plan_dir else None
            ef_cands = sorted(gi_err_dir.glob(f"GI_Error_{ymd_f}_{hhmmss[:4]}*.XLS")) if (gi_err_dir and gi_err_dir.exists()) else []
            if ef_cands:
                fallback_gi_recs = parse_ztcode_stock_rec_gi_records_detailed(ef_cands[-1])
            elif fallback_plan_df is not None and fallback_plan_df.empty:
                fallback_gi_recs = []
        else:
            target_batch_id = b_id

    if not target_as_of and not target_batch_id and not is_file_only_fallback:
        for v in payload.get("candidates", []):
            v["post_0800_status"] = "⏳ Before 08:00 cycle (post-08:00 check pending)"
            v["post_0800_changed"] = False
        payload["post_0800_checked"] = False
        payload["post_0800_changed_count"] = 0
        return payload

    post_eval = evaluate_gi_errors_as_of(
        as_of=target_as_of,
        gi_db_path=gi_db_path,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=ops_raw_db_path,
        exact_batch_id=target_batch_id,
        active_gi_records=fallback_gi_recs,
        override_plan_df=fallback_plan_df,
    )
    if is_file_only_fallback and b0830:
        post_eval["gi_01_batch_id"] = b0830[0]
        post_eval["gi_01_plan_time_edt"] = target_as_of
    post_by_occ_sig = {k: v for k, v in _sig_occ_list(post_eval.get("verdicts", []))}
    post_by_sig5: Dict[Tuple[str, str, str, str, float], Dict[str, Any]] = {
        _sig5(v): v for v in post_eval.get("verdicts", [])
    }

    # Also check 08:00 cycle postings in transfer_posting_history
    ymd = rep_date.replace("-", "")
    posted_0800_mats: Dict[str, str] = {}
    if gi_db_path and Path(gi_db_path).exists():
        with _connect_ro(Path(gi_db_path)) as conn:
            prows = conn.execute(
                """
                SELECT material, from_sloc, to_sloc, transfer_qty, doc_number, batch_id
                FROM transfer_posting_history
                WHERE batch_id >= ? AND batch_id <= ? AND post_status = 'POSTED'
                """,
                (f"RUN_{ymd}_080000", f"RUN_{ymd}_090000"),
            ).fetchall()
        for pm, pf, pt, pq, pdoc, pbid in prows:
            posted_0800_mats[str(pm).strip()] = f"{pbid} {pf}→{pt} {float(pq or 0):,.0f} EA (Doc {pdoc})"

    changed_cnt = 0
    for cand_occ_key, cand in _sig_occ_list(payload.get("candidates", [])):
        sig = _sig5(cand)
        mat = str(cand.get("material") or "").strip()
        pv = post_by_occ_sig.get(cand_occ_key) or post_by_sig5.get(sig)
        if pv is None:
            if mat in posted_0800_mats:
                cand["post_0800_status"] = f"⚠️ Cleared by 08:00 posting ({posted_0800_mats[mat]}) — withdraw/exclude draft"
            else:
                cand["post_0800_status"] = "⚠️ Cleared after 08:00 (field/other job) — review draft withdrawal"
            cand["post_0800_changed"] = True
            changed_cnt += 1
        elif pv.get("d107_status") == "CANDIDATE":
            cand["post_0800_status"] = f"✅ Unchanged ({post_eval.get('gi_01_batch_id')})"
            cand["post_0800_changed"] = False
        else:
            reasons_txt = "; ".join(pv.get("d107_exclusion_reasons", []))
            cand["post_0800_status"] = f"⚠️ Status changed ({pv.get('d107_status')}): {reasons_txt} — review draft amendment/withdrawal"
            cand["post_0800_changed"] = True
            changed_cnt += 1

    payload["post_0800_checked"] = True
    payload["post_0800_batch_id"] = post_eval.get("gi_01_batch_id")
    payload["post_0800_time_edt"] = post_eval.get("gi_01_plan_time_edt")
    payload["post_0800_changed_count"] = changed_cnt
    return payload


def _parse_gr_file_records(gr_path: Any) -> List[Dict[str, Any]]:
    """Merges `GR_Error` + `GR_Domestics` + `GR_Import` with multiset deduplication (#187 B-1)."""
    return parse_ztcode_stock_rec_gr_records_detailed(gr_path)


def collect_fresh_post_0700_d107(
    report_date: Optional[str] = None,
    dry_run: bool = False,
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
    wait_for_0700_timeout_sec: float = 600.0,
) -> Dict[str, Any]:
    """
    #182: Executed after 07:00 cycle completes (target ~07:18~07:20).
    NEVER reuses 07:00 pre-posting snapshot; extracts fresh ZTCODE_STOCK_REC GI/GR + MB52 + WMS
    under `sap_session_lock`, runs D107 candidate evaluation (zero DB writes, zero portal access),
    and saves `d107_phase1_YYYYMMDD.json` (or `scratch/DRYRUN_d107_phase1_YYYYMMDD.json` on dry_run).
    """
    now = datetime.now()
    rep_date_str = report_date or now.strftime("%Y-%m-%d")
    prev_date_str = (datetime.strptime(rep_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    ymd = rep_date_str.replace("-", "")

    # Wait if 07:00 cycle is still running (or sap_session.lock exists)
    from sap_lock import sap_session_lock

    deadline = time.time() + max(5.0, float(wait_for_0700_timeout_sec))
    while time.time() < deadline:
        batches = _find_morning_batches(rep_date_str, gi_db_path, gi_plan_dir=gi_plan_dir)
        lock_file = HQ_DIR / "sap_session.lock"
        if batches.get("batch_0700") is not None and not lock_file.exists():
            break
        if now.hour >= 8 and not lock_file.exists():
            break
        time.sleep(5.0)

    run_tag = datetime.now().strftime("D107_%Y%m%d_%H%M%S")
    plant = os.getenv("SAP_PLANT", "").strip()
    if not plant:
        raise RuntimeError("[D107 ENV GUARD] SAP_PLANT is not configured.")

    from sap_launcher import ensure_sap_open
    from ztcode_gi_collector import collect_ztcode_gi_err_errors
    from sync_snapshot_collector import collect_simultaneous_snapshots
    from gr_gi_resolver import resolve_gi_errors

    with sap_session_lock(owner=f"01_JOB_{run_tag}", timeout=180.0) as acquired:
        if not acquired:
            raise RuntimeError(f"[D107 SAP LOCK] Could not acquire SAP lock for {run_tag} within 180s.")

        ensure_sap_open()
        errors = collect_ztcode_gi_err_errors(plant=plant)
        gi_file = errors.get("GI_Error")
        gr_file = errors.get("GR_Error")
        gr_files_all = errors.get("GR_Files") or ([Path(gr_file)] if (gr_file and Path(gr_file).exists()) else [])
        if not gi_file or not Path(gi_file).exists():
            raise RuntimeError("[D107 FRESH GUARD] Fresh ZTCODE_STOCK_REC GI_Error extraction failed.")

        snapshots = collect_simultaneous_snapshots(
            max_freshness_seconds=300.0,
            sap_lock_owner=f"01_JOB_{run_tag}",
        )
        collection_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build fresh resolution plan in memory / scratch without writing to DB signals
        fresh_plan_df = resolve_gi_errors(
            gi_path=Path(gi_file),
            gr_path=gr_files_all if gr_files_all else None,
            run_id=run_tag,
            record_signals=False,
        )

    fresh_gi_recs = parse_ztcode_stock_rec_gi_records_detailed(Path(gi_file))
    fresh_gr_recs = _parse_gr_file_records(gr_files_all if gr_files_all else gr_file)

    eval_res = evaluate_gi_errors_as_of(
        as_of=collection_ts,
        gi_db_path=gi_db_path,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=ops_raw_db_path,
        active_gi_records=fresh_gi_recs,
        override_plan_df=fresh_plan_df if fresh_plan_df is not None else pd.DataFrame(),
        fresh_gr_records=fresh_gr_recs,
    )

    payload = _format_d107_payload(
        report_date_str=rep_date_str,
        prev_date_str=prev_date_str,
        collection_time_edt=collection_ts,
        collection_mode="FRESH_POST_0700_SAP_WMS",
        eval_res=eval_res,
        gi_db_path=gi_db_path,
        source_files={
            "gi_file": Path(gi_file).name if gi_file else "",
            "gr_file": Path(gr_file).name if gr_file else "",
            "mb52_file": Path(snapshots["SAP_MB52"]).name if snapshots.get("SAP_MB52") else "",
            "wms_file": Path(snapshots["WMS_STOCK"]).name if snapshots.get("WMS_STOCK") else "",
        },
    )
    payload = attach_post_0800_comparison(
        payload,
        gi_db_path=gi_db_path,
        gi_plan_dir=gi_plan_dir,
        ops_raw_db_path=ops_raw_db_path,
    )
    out_path = _save_d107_json(payload, rep_date_str, gi_plan_dir=gi_plan_dir, dry_run=dry_run)
    payload["artifact_json"] = str(out_path)
    return payload


def _format_d107_payload(
    report_date_str: str,
    prev_date_str: str,
    collection_time_edt: str,
    collection_mode: str,
    eval_res: Dict[str, Any],
    gi_db_path: Path,
    source_files: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    verdicts = eval_res.get("verdicts", [])
    cleared_cnt, cleared_items, b0700_id = _compute_0700_cleared_summary(
        report_date_str=report_date_str,
        current_verdicts=verdicts,
        gi_db_path=gi_db_path,
    )

    candidates = [dict(v) for v in verdicts if v.get("d107_status") == "CANDIDATE"]
    holds = [dict(v) for v in verdicts if v.get("d107_status") == "HOLD_HUMAN_REVIEW"]
    excluded = [dict(v) for v in verdicts if v.get("d107_status") == "EXCLUDED"]

    # Prev-day 311/312 subset count for executive summary
    rep_d = datetime.strptime(report_date_str, "%Y-%m-%d").date()
    prev_31x_total = 0
    for v in verdicts:
        if str(v.get("mvt") or "") in D107_TARGET_MVTS:
            ed_s = str(v.get("error_date") or "").replace(".", "-").replace("/", "-")
            try:
                if datetime.strptime(ed_s, "%Y-%m-%d").date() < rep_d:
                    prev_31x_total += 1
            except ValueError:
                pass

    return {
        "report_date": report_date_str,
        "prev_date": prev_date_str,
        "collection_time_edt": collection_time_edt,
        "collection_mode": collection_mode,
        "reused_0700_pre_snapshot": False,
        "batch_0700_id": b0700_id,
        "cleared_by_0700_count": cleared_cnt,
        "cleared_by_0700_items": cleared_items,
        "preceding_batch_id": eval_res.get("gi_01_batch_id"),
        "total_active_gi_rows": len(verdicts),
        "prev_day_31x_remaining_count": prev_31x_total,
        "candidate_count": len(candidates),
        "hold_count": len(holds),
        "excluded_count": len(excluded),
        "candidates": candidates,
        "holds": holds,
        "excluded": excluded,
        "source_files": source_files or {},
    }


def _save_d107_json(
    payload: Dict[str, Any],
    report_date_str: str,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    dry_run: bool = False,
) -> Path:
    ymd = report_date_str.replace("-", "")
    if dry_run:
        scratch_dir = JOB_DIR / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        out_path = scratch_dir / f"DRYRUN_d107_phase1_{ymd}.json"
    else:
        target_dir = Path(gi_plan_dir) if gi_plan_dir else DEFAULT_GI_PLAN_DIR
        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"d107_phase1_{ymd}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_or_evaluate_d107_readonly(
    report_date_str: str,
    gi_db_path: Path = DEFAULT_GI_DB,
    gi_plan_dir: Path = DEFAULT_GI_PLAN_DIR,
    ops_raw_db_path: Optional[Path] = DEFAULT_OPS_RAW_DB,
    post_0800_as_of: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Loads today's `d107_phase1_YYYYMMDD.json` (collected fresh at ~07:20) if present;
    otherwise evaluates read-only from the post-07:00 snapshot in `gr_gi_history.db` + `resolution_logs`
    (never writing to DB or touching SAP/WMS/Portal).
    """
    ymd = report_date_str.replace("-", "")
    prev_date_str = (datetime.strptime(report_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    plan_dir = Path(gi_plan_dir) if gi_plan_dir else DEFAULT_GI_PLAN_DIR
    cached_json = plan_dir / f"d107_phase1_{ymd}.json"
    if cached_json.exists():
        try:
            payload = json.loads(cached_json.read_text(encoding="utf-8"))
            return attach_post_0800_comparison(
                payload,
                gi_db_path=gi_db_path,
                gi_plan_dir=plan_dir,
                ops_raw_db_path=ops_raw_db_path,
                post_0800_as_of=post_0800_as_of,
            )
        except Exception as exc:
            logger.warning(f"[D107] Could not parse cached {cached_json.name}, falling back to DB read-only eval: {exc}")

    batches = _find_morning_batches(report_date_str, gi_db_path)
    b_post0700 = batches.get("batch_post_0700")
    b_0700 = batches.get("batch_0700")

    if b_post0700:
        exact_bid = b_post0700[0]
        as_of_str = b_post0700[1] or f"{report_date_str} 08:05:00"
        mode_label = f"POST_0700_DB_SNAPSHOT ({exact_bid})"
    elif b_0700 and int(b_0700[2]) == 0:
        # 07:00 executed 0 postings -> post-07:00 state is identical to 07:00 snapshot (until 07:20 fresh run)
        exact_bid = b_0700[0]
        as_of_str = b_0700[1] or f"{report_date_str} 07:05:00"
        mode_label = f"POST_0700_ZERO_POSTING_SNAPSHOT ({exact_bid} — pre-07:20 preliminary)"
    else:
        exact_bid = None
        as_of_str = f"{report_date_str} 07:25:00"
        mode_label = "FALLBACK_LATEST_PRECEDING_SNAPSHOT"

    eval_res = evaluate_gi_errors_as_of(
        as_of=as_of_str,
        gi_db_path=gi_db_path,
        gi_plan_dir=plan_dir,
        ops_raw_db_path=ops_raw_db_path,
        exact_batch_id=exact_bid,
    )
    coll_ts = eval_res.get("gi_01_plan_time_edt") or eval_res.get("gi_01_run_time_edt") or as_of_str
    payload = _format_d107_payload(
        report_date_str=report_date_str,
        prev_date_str=prev_date_str,
        collection_time_edt=coll_ts,
        collection_mode=mode_label,
        eval_res=eval_res,
        gi_db_path=gi_db_path,
    )
    return attach_post_0800_comparison(
        payload,
        gi_db_path=gi_db_path,
        gi_plan_dir=plan_dir,
        ops_raw_db_path=ops_raw_db_path,
        post_0800_as_of=post_0800_as_of,
    )


def write_d107_post_0800_change_alert(
    payload: Dict[str, Any],
    handoff_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Optional[Path]:
    """
    #182: If any 07:20 candidate changed status after the 08:00 cycle (e.g. cleared by 01,
    gained GR error, or stock arrived), writes a handoff notice for withdrawal/amendment review.
    """
    changed = [c for c in payload.get("candidates", []) if c.get("post_0800_changed")]
    if not changed:
        return None

    rep_date = str(payload.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
    ymd = rep_date.replace("-", "")
    hm = datetime.now().strftime("%H%M")
    repo_root = JOB_DIR.parents[2]
    if dry_run:
        out_dir = JOB_DIR / "scratch"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"DRYRUN_ALERT_D107_{ymd}_{hm}_post0800_change.md"
    else:
        target_h = Path(handoff_dir) if handoff_dir else (repo_root / ".agents" / "handoff")
        out_dir = target_h if target_h.exists() else (JOB_DIR / "resolution_logs")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"ALERT_D107_{ymd}_{hm}_antigravity_to_claude_post0800-candidate-changed.md"

    lines = [
        "---",
        'thread: "D107-POST0800"',
        "from: antigravity (01_JOB D107 Post-08:00 Check)",
        "to: claude, user",
        f"created: {datetime.now().strftime('%Y-%m-%d %H:%M')} local",
        "type: info",
        "needs_user_decision: true",
        "---",
        "",
        f"# ⚠️ [D107 사후 점검] {rep_date} 08:00 회차 이후 삭제 기안 후보 상태 변동 ({len(changed)}건) — 기안 수정/철회 검토",
        "",
        f"- **07:20 수집 시각**: `{payload.get('collection_time_edt')}`",
        f"- **08:00 이후 확인 배치**: `{payload.get('post_0800_batch_id')}` (`{payload.get('post_0800_time_edt')}`)",
        "",
        "| 품번 | mvt | 경로 | 수량 | 발생일 | 08:00 이후 변동 내역 |",
        "| :--- | :---: | :---: | ---: | :---: | :--- |",
    ]
    for c in changed:
        lines.append(
            f"| `{c.get('material')}` | `{c.get('mvt')}` | `{c.get('from_sloc')}→{c.get('to_sloc')}` | "
            f"{float(c.get('qty') or 0):,.0f} | `{c.get('error_date')}` | {c.get('post_0800_status')} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path
