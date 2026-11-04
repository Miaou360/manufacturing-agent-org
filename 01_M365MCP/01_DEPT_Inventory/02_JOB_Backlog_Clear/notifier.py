"""
Backlog Daily Morning Notifier (Outlook Automated Sender)
=========================================================
01_Inventory_Team / 02_Backlog
- Automatically sends daily morning backlog status at 08:00 AM EST via Outlook.
- Fully English, concise executive layout with structured headers and data tables (with D108 GI verdict enrichment).
- Attachment: Unresolved_Backlog_Report_YYYYMMDD_0800.xlsx
- Default recipient: configured via .env (BACKLOG_PROD_TO)
"""

import os
import sys
import html as _html
import logging
from pathlib import Path
import sys as _sys
from dotenv import load_dotenv as _load_dotenv
_ENV_LOADED_PATH = None
for _ep in Path(__file__).resolve().parents:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        _ENV_LOADED_PATH = _ep / ".env"
        break
_DUMMY_ENV_VALS = {"P001", "P002", "0000000", "9900000", "Enterprise SSO", "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT", "ACME", "ACME Manufacturing", "ACME Corp", "ACME America", "MAT-0000", "TEST-000"}
def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val
import re as _re_out_guard
_OUT_DUMMY_RE = _re_out_guard.compile(r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)", _re_out_guard.IGNORECASE)
def _assert_clean_ops_output(*texts: str, context: str = "email_output") -> None:
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return
    for _t in texts:
        if _t:
            _m = _OUT_DUMMY_RE.search(str(_t))
            if _m:
                raise RuntimeError(f"[OUTPUT GUARD CRITICAL] Dummy token '{_m.group(0)}' detected in {context} ({__file__})!")
def _verify_ops_env_log(job_name: str = "PIPELINE") -> str:
    _req = ("SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME", "SAP_SSO_WINDOW_TITLE", "WMS_BASE_URL", "COMPANY_NAME", "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT")
    _st = []
    for _k in _req:
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from datetime import datetime
from typing import List, Dict, Optional, Any
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
REPORTS_DIR = BASE_DIR / "resolution_logs"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

try:
    from dotenv import load_dotenv
    _env_path = BASE_DIR.parents[1] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

logger = logging.getLogger("BacklogNotifier")

# D111 / #207: shared English display labels + pre-dispatch Hangul guard
_DEPT_DIR = str(BASE_DIR.parent)
if _DEPT_DIR not in sys.path:
    sys.path.insert(0, _DEPT_DIR)
from report_labels import EN as _EN, check_report_language  # noqa: E402

def is_valid_recipient(addr: str) -> bool:
    """Returns True if addr is a non-empty, non-placeholder email address."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip().lower()
    return bool(clean) and "@" in clean and "example.com" not in clean

# Production Distribution List (Configured via .env)
_env_to = os.getenv("BACKLOG_PROD_TO")
PROD_TO = [x.strip() for x in _env_to.split(",") if is_valid_recipient(x)] if _env_to else []

_env_cc = os.getenv("BACKLOG_PROD_CC")
PROD_CC = [x.strip() for x in _env_cc.split(",") if is_valid_recipient(x)] if _env_cc else []

# Test Mode Distribution List (D33 & Rule 10: strictly user-only)
_env_test_to = os.getenv("TEST_EMAIL_RECIPIENT", os.getenv("BACKLOG_TEST_TO", ""))
TEST_TO = [x.strip() for x in _env_test_to.split(",") if is_valid_recipient(x)] if _env_test_to else []
TEST_CC = []

DEFAULT_TO = PROD_TO
DEFAULT_CC = PROD_CC


def _match_plan_row_for_cogi_item(plan_df: Optional[pd.DataFrame], it: Dict[str, Any]) -> Dict[str, Any]:
    """
    Matches a post-save COGI item to its exact `(Material, Target_SLoc)` row in `plan_df`
    so multi-SLoc materials (e.g. SP31 + SL49) never inherit the wrong SLoc's status.
    """
    mat = str(it.get("Material", "")).strip()
    it_sloc = str(it.get("SLoc", "SP31")).strip().upper()
    it_qty = float(it.get("Qty", 0.0))
    it_err = str(it.get("Error_Code", "")).strip()

    if plan_df is not None and not plan_df.empty and "Material" in plan_df.columns:
        mat_rows = plan_df[plan_df["Material"].astype(str).str.strip() == mat]
        if not mat_rows.empty:
            if "Target_SLoc" in mat_rows.columns:
                sloc_rows = mat_rows[mat_rows["Target_SLoc"].astype(str).str.strip().str.upper() == it_sloc]
            else:
                sloc_rows = pd.DataFrame()
            chosen = sloc_rows.iloc[0] if not sloc_rows.empty else mat_rows.iloc[0]
            r_dict = chosen.to_dict()
            r_dict["Target_SLoc"] = it_sloc
            r_dict["Deficit_Qty"] = it_qty
            if it_err:
                r_dict["Error_Code"] = it_err
            if r_dict.get("Status") == "PARTIAL_ACCURACY_REBALANCING":
                r_dict["Reason"] = (
                    f"Partially replenished ({r_dict.get('Transfer_Qty', 0):,.1f} EA transferred) - "
                    f"Remaining deficit {r_dict['Deficit_Qty']:,.1f} EA in COGI"
                )
            return r_dict

    return {
        "Material": mat,
        "Material_Desc": it.get("Material_Desc", ""),
        "Target_SLoc": it_sloc,
        "Deficit_Qty": it_qty,
        "Error_Code": it_err,
        "Status": "ACTION_REQUIRED",
        "Pending_GR_Qty": 0,
        "Pending_GR_Vendors": "-",
        "Reason": "Unresolved backlog item remaining in SAP COGI",
    }


def _normalize_deficit_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Normalizes deficit dictionary to handle both DB column names (lowercase) and memory keys (TitleCase)."""
    sloc = str(d.get("Storage_Loc") or d.get("storage_loc") or d.get("SLoc") or d.get("sloc") or "").strip().upper()
    return {
        "Material": str(d.get("Material") or d.get("material") or "").strip(),
        "Material_Desc": str(d.get("Material_Desc") or d.get("material_desc") or "").strip(),
        "Storage_Loc": sloc,
        "SAP_Stock": float(d.get("SAP_Stock") if d.get("SAP_Stock") is not None else d.get("sap_stock", 0.0)),
        "WMS_Stock": float(d.get("WMS_Stock") if d.get("WMS_Stock") is not None else d.get("wms_stock", 0.0)),
        "Deficit_Qty": float(d.get("Deficit_Qty") if d.get("Deficit_Qty") is not None else d.get("deficit_qty", 0.0)),
        "Accuracy_Before": d.get("Accuracy_Before") if d.get("Accuracy_Before") is not None else d.get("accuracy_before"),
        "Accuracy_After": d.get("Accuracy_After") if d.get("Accuracy_After") is not None else d.get("accuracy_after"),
        "Recent_Doc": str(d.get("Recent_Doc") or d.get("recent_doc") or ""),
        "first_detected_date": str(d.get("first_detected_date") or d.get("run_date") or ""),
        "consecutive_days": int(d.get("consecutive_days", 1)),
        "is_sl49": int(d.get("is_sl49") or (1 if sloc == "SL49" else 0)),
        "is_sustained_deficit": d.get("is_sustained_deficit")
    }


def generate_unresolved_excel(
    plan_df: pd.DataFrame,
    run_date: str,
    final_cogi_items: Optional[List[Dict[str, Any]]] = None,
    remaining_deficits: Optional[List[Dict[str, Any]]] = None,
    output_dir: Optional[Path] = None,
    is_test: bool = False,
    dry_run: bool = False,
) -> Optional[Path]:
    """Generates a multi-tab Excel report for unresolved backlog items and remaining SAP deficits (D32 & D108)."""
    rows = []
    if final_cogi_items is not None:
        for it in final_cogi_items:
            rows.append(_match_plan_row_for_cogi_item(plan_df, it))
    elif plan_df is not None and not plan_df.empty:
        for _, row in plan_df.iterrows():
            if row['Action'] == 'SKIP':
                rows.append(row.to_dict())
            elif row.get('Status') == 'PARTIAL_ACCURACY_REBALANCING':
                r = row.to_dict()
                rem = float(row.get('Deficit_Qty', 0)) - float(row.get('Transfer_Qty', 0))
                r['Deficit_Qty'] = rem
                r['Reason'] = f"Partially replenished ({row.get('Transfer_Qty', 0):,.1f} EA transferred) - Remaining deficit {rem:,.1f} EA in COGI"
                rows.append(r)

    has_cogi = bool(rows)
    has_deficits = bool(remaining_deficits)

    if not has_cogi and not has_deficits:
        logger.info("[NOTIFIER] All backlog items were fully resolved and no remaining SAP deficits.")
        return None

    # Guard against polluting production resolution_logs during test/dry-run (#179: job-local scratch/)
    if dry_run:
        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"DRYRUN_Unresolved_Backlog_Report_{ts_str}.xlsx"
        if output_dir is None or Path(output_dir).resolve() == REPORTS_DIR.resolve():
            target_dir = BASE_DIR / "scratch"
        else:
            target_dir = Path(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
    elif is_test:
        filename = f"[TEST]_Unresolved_Backlog_Report_{run_date}_0800.xlsx"
        if output_dir is None or Path(output_dir).resolve() == REPORTS_DIR.resolve():
            target_dir = BASE_DIR / "scratch"
            target_dir.mkdir(parents=True, exist_ok=True)
        else:
            target_dir = Path(output_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
    else:
        filename = f"Unresolved_Backlog_Report_{run_date}_0800.xlsx"
        target_dir = Path(output_dir or REPORTS_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)

    filepath = target_dir / filename

    with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
        if has_cogi:
            unresolved = pd.DataFrame(rows)
            export_cols = [
                'Material', 'Material_Desc', 'Target_SLoc', 'Deficit_Qty',
                'Error_Code', 'Status',
                'D108_Cause_Code', 'D108_Cause_Label', 'D108_Severity',
                'D108_Linked_GI', 'D108_01_Verdict', 'D108_01_Plan_Time',
                'Best_Cand_SLoc', 'Best_Cand_SAP', 'Best_Cand_WMS',
                'Pending_GR_Qty', 'Pending_GR_Vendors',
                'Pending_GI_Qty', 'Pending_GI_SLocs', 'Reason'
            ]
            avail_cols = [c for c in export_cols if c in unresolved.columns]
            report_df = unresolved[avail_cols].copy()
            report_df.rename(columns={
                'Material': 'Material',
                'Material_Desc': 'Description',
                'Target_SLoc': 'Target SLoc',
                'Deficit_Qty': 'Remaining Deficit Qty',
                'Error_Code': 'SAP Error Code',
                'Status': 'Status',
                'D108_Cause_Code': 'D108 Root Cause Code',
                'D108_Cause_Label': 'D108 Root Cause Verdict',
                'D108_Severity': 'D108 Severity',
                'D108_Linked_GI': 'Linked GI Error (mvt/from->to/qty/date)',
                'D108_01_Verdict': '01 Latest Verdict',
                'D108_01_Plan_Time': '01 Verdict Cycle Time',
                'Best_Cand_SLoc': 'Best Candidate SLoc',
                'Best_Cand_SAP': 'Candidate SAP Stock',
                'Best_Cand_WMS': 'Candidate WMS Stock',
                'Pending_GR_Qty': 'Pending GR Qty',
                'Pending_GR_Vendors': 'Vendor',
                'Pending_GI_Qty': 'Pending GI Qty',
                'Pending_GI_SLocs': 'GI Storage Locs',
                'Reason': 'Reason / Guidance'
            }, inplace=True)
            report_df.to_excel(writer, sheet_name="Unresolved_COGI", index=False)
        else:
            pd.DataFrame([{"Notice": "All COGI backlogs fully resolved."}]).to_excel(writer, sheet_name="Unresolved_COGI", index=False)

        # Tab 2: Remaining SAP Deficits (D32)
        if has_deficits:
            d_rows = []
            for raw_d in remaining_deficits:
                d = _normalize_deficit_dict(raw_d)
                d_rows.append({
                    "Material": d["Material"],
                    "Description": d["Material_Desc"],
                    "Storage Location": d["Storage_Loc"],
                    "SAP Stock": d["SAP_Stock"],
                    "WMS Stock": d["WMS_Stock"],
                    "Remaining Deficit Qty": d["Deficit_Qty"],
                    "Accuracy (Current)": f"{float(d['Accuracy_Before'])*100:.1f}%" if d['Accuracy_Before'] is not None else "-",
                    "Accuracy (Post-Transfer)": f"{float(d['Accuracy_After'])*100:.1f}%" if d['Accuracy_After'] is not None else "-",
                    "Recent Document / Error": d["Recent_Doc"],
                    "First Detected Date": d["first_detected_date"],
                    "Consecutive Days Detected": d["consecutive_days"],
                    "Location Category": _EN["SL49_NOT_COUNTED"] if d["is_sl49"] else "Standard Warehouse",
                    "Sustained Deficit Status": (
                        "SUSTAINED (also present in previous snapshot)" if d["is_sustained_deficit"] == 1
                        else ("TRANSIENT_OR_NEW (new or suspected pending interface)" if d["is_sustained_deficit"] == 0 else "NO_PREV_DATA (no previous snapshot)")
                    )
                })
            def_df = pd.DataFrame(d_rows)
            def_df.to_excel(writer, sheet_name="Remaining_SAP_Deficits", index=False)
        else:
            pd.DataFrame([{"Notice": "No warehouse materials with unresolved SAP inventory deficit."}]).to_excel(writer, sheet_name="Remaining_SAP_Deficits", index=False)

    logger.info(f"[NOTIFIER] Multi-tab backlog & deficit Excel generated: {filepath}")
    return filepath


def _format_target_sloc(sloc_val: Any) -> str:
    """Formats target storage location with styling and special badge for SL49."""
    s = str(sloc_val).strip().upper()
    if s == 'SL49':
        return f'<span style="background-color: #fef3c7; color: #92400e; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px;">{_EN["SL49_QUALITY_DMA"]}</span>'
    return f'<span style="font-weight: bold; color: #2b6cb0;">{s}</span>'


def _format_d108_badge(code_val: Any, label_val: Any, sev_val: Any) -> str:
    """Renders D108 root-cause badge for GI-linked COGI rows (#183)."""
    code = "" if pd.isna(code_val) else str(code_val).strip()
    label = "" if pd.isna(label_val) else str(label_val).strip()
    sev = "" if pd.isna(sev_val) else str(sev_val).strip()
    if not label:
        label = _EN["D108_UNCLASSIFIED"]
    disp = _html.escape(f"{sev} {label}".strip())
    if code.startswith("ANOMALY_") and code != "ANOMALY_NEW_AFTER_01":
        style = "background-color: #fee2e2; color: #991b1b; border: 1px solid #fca5a5;"
    elif code in ("D", "A", "F"):
        style = "background-color: #fee2e2; color: #991b1b; border: 1px solid #fecaca;"
    elif code in ("B", "ANOMALY_NEW_AFTER_01"):
        style = "background-color: #fef3c7; color: #92400e; border: 1px solid #fde68a;"
    else:
        style = "background-color: #f1f5f9; color: #475569; border: 1px solid #cbd5e1;"
    return f'<span style="{style} display: inline-block; padding: 2px 7px; border-radius: 4px; font-weight: bold; font-size: 11px;">{disp}</span>'


def build_email_html(
    plan_df: pd.DataFrame,
    execution_results: List[Dict],
    run_date: str,
    run_time: str,
    final_cogi_items: Optional[List[Dict[str, Any]]] = None,
    initial_count: Optional[int] = None,
    initial_qty: Optional[float] = None,
    remaining_deficits: Optional[List[Dict[str, Any]]] = None,
    is_correction: bool = False,
    correction_note: Optional[str] = None
) -> str:
    """Builds a clean, executive HTML email body with cumulative daily resolution tracking and D108 GI verdicts."""
    # Cumulative resolved items and quantities
    resolved_count = len([r for r in execution_results if "499" in str(r.get("Result", "")) or r.get("Status") == "SUCCESS"]) if execution_results else 0
    resolved_qty = sum([float(r.get("Qty", 0.0)) for r in execution_results if "499" in str(r.get("Result", "")) or r.get("Status") == "SUCCESS"]) if execution_results else 0.0

    # Build list of items still pending in COGI (Verified directly from live SAP post-resolution check)
    unresolved_rows = []
    if final_cogi_items is not None:
        for it in final_cogi_items:
            r_dict = _match_plan_row_for_cogi_item(plan_df, it)
            if r_dict.get('Status') == 'PARTIAL_ACCURACY_REBALANCING':
                r_dict['Reason'] = f"Partially replenished ({r_dict.get('Transfer_Qty', 0):,.1f} EA transferred) &bull; Remaining {r_dict['Deficit_Qty']:,.1f} EA deficit in COGI"
            unresolved_rows.append(r_dict)
    elif plan_df is not None:
        for _, row in plan_df.iterrows():
            if row['Action'] == 'SKIP':
                unresolved_rows.append(row.to_dict())
            elif row.get('Status') == 'PARTIAL_ACCURACY_REBALANCING':
                r_dict = row.to_dict()
                remaining_def = float(row.get('Deficit_Qty', 0)) - float(row.get('Transfer_Qty', 0))
                r_dict['Deficit_Qty'] = remaining_def
                r_dict['Reason'] = f"Partially replenished ({row.get('Transfer_Qty', 0):,.1f} EA transferred) &bull; Remaining {remaining_def:,.1f} EA deficit in COGI"
                unresolved_rows.append(r_dict)

    unresolved_df = pd.DataFrame(unresolved_rows) if unresolved_rows else pd.DataFrame()

    gr_blocked_df = unresolved_df[unresolved_df['Status'] == 'BLOCKED_BY_GR_ERROR'] if not unresolved_df.empty else pd.DataFrame()
    gi_blocked_df = unresolved_df[unresolved_df['Status'] == 'BLOCKED_BY_GI_ERROR'] if not unresolved_df.empty else pd.DataFrame()
    other_unres_df = unresolved_df[~unresolved_df['Status'].isin(['BLOCKED_BY_GR_ERROR', 'BLOCKED_BY_GI_ERROR'])] if not unresolved_df.empty else pd.DataFrame()

    gr_blocked_count = len(gr_blocked_df)
    gr_blocked_qty = gr_blocked_df['Deficit_Qty'].sum() if not gr_blocked_df.empty else 0.0

    gi_blocked_count = len(gi_blocked_df)
    gi_blocked_qty = gi_blocked_df['Deficit_Qty'].sum() if not gi_blocked_df.empty else 0.0

    # D108 breakdown for KPI card subtitle
    gi_kpi_sub = f"Deficit: {gi_blocked_qty:,.1f}"
    if not gi_blocked_df.empty and 'D108_Cause_Code' in gi_blocked_df.columns:
        codes = [str(x or '').strip() for x in gi_blocked_df['D108_Cause_Code'].tolist()]
        crit_cnt = sum(1 for c in codes if c in ('D', 'A', 'F'))
        gr_cnt = sum(1 for c in codes if c == 'B')
        anom_cnt = sum(1 for c in codes if c.startswith('ANOMALY_'))
        parts_kpi = []
        if crit_cnt:
            parts_kpi.append(f"Critical (D/A/F) {crit_cnt}")
        if gr_cnt:
            parts_kpi.append(f"Pending GR (B) {gr_cnt}")
        if anom_cnt:
            parts_kpi.append(f"⚠️ Anomaly {anom_cnt}")
        if parts_kpi:
            gi_kpi_sub = " · ".join(parts_kpi)

    other_unres_count = len(other_unres_df)
    other_unres_qty = other_unres_df['Deficit_Qty'].sum() if not other_unres_df.empty else 0.0

    # Total daily scope: initial morning baseline (from 06:00 raw history) or cumulative sum
    total_count = initial_count if initial_count and initial_count > 0 else (len(plan_df) if plan_df is not None else 0)
    total_qty = initial_qty if initial_qty and initial_qty > 0 else (plan_df['Deficit_Qty'].sum() if plan_df is not None and 'Deficit_Qty' in plan_df.columns else 0.0)

    # Ensure total_count and total_qty are at least resolved + unresolved
    total_count = max(total_count, resolved_count + len(unresolved_df))
    total_qty = max(total_qty, resolved_qty + (unresolved_df['Deficit_Qty'].sum() if not unresolved_df.empty else 0.0))

    # Optional Top Correction Banner (#183 line 38)
    correction_banner_html = ""
    if is_correction:
        note_text = _html.escape(correction_note or _EN["CORRECTION_DEFAULT_NOTE"])
        correction_banner_html = f"""
        <div style="background-color: #eff6ff; border-left: 4px solid #1d4ed8; border-top: 1px solid #bfdbfe; border-right: 1px solid #bfdbfe; border-bottom: 1px solid #bfdbfe; padding: 10px 14px; border-radius: 4px; margin-bottom: 16px; color: #1e3a8a; font-size: 13px; font-weight: bold;">
            {_EN["CORRECTION"]} {note_text}
        </div>
        """

    # 1. GR-Blocked Table Rows
    gr_rows_html = ""
    if not gr_blocked_df.empty:
        for _, row in gr_blocked_df.iterrows():
            gr_rows_html += f"""
            <tr style="border-bottom: 1px solid #fed7aa; background-color: #fffaf0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #9a3412;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; color: #4a5568;">{str(row.get('Material_Desc', ''))[:28]}</td>
                <td style="padding: 7px 10px; text-align: center;">{_format_target_sloc(row.get('Target_SLoc', ''))}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #c2410c;">{row.get('Deficit_Qty', 0):,.1f}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #047857;">{row.get('Pending_GR_Qty', 0):,.1f}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4b5563;">{row.get('Pending_GR_Vendors', '-')}</td>
                <td style="padding: 7px 10px; color: #7c2d12; font-size: 11px;">{row.get('Reason', '')}</td>
            </tr>
            """
    else:
        gr_rows_html = '<tr><td colspan="7" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No backlogs blocked by active GR errors.</td></tr>'

    # 1.5. GI-Blocked Table Rows (D44 + D108 Root-Cause Reclassification)
    gi_rows_html = ""
    if not gi_blocked_df.empty:
        for _, row in gi_blocked_df.iterrows():
            d108_code = row.get('D108_Cause_Code', '')
            d108_badge = _format_d108_badge(d108_code, row.get('D108_Cause_Label', ''), row.get('D108_Severity', ''))
            linked_gi = row.get('D108_Linked_GI', '')
            if not linked_gi or pd.isna(linked_gi):
                linked_gi = f"{row.get('Pending_GI_SLocs', '-')} ({float(row.get('Pending_GI_Qty', 0)):,.1f} EA)"
            v01 = "" if pd.isna(row.get('D108_01_Verdict', '')) else str(row.get('D108_01_Verdict', '')).strip()
            t01 = "" if pd.isna(row.get('D108_01_Plan_Time', '')) else str(row.get('D108_01_Plan_Time', '')).strip()
            det = "" if pd.isna(row.get('D108_Detail', '')) else str(row.get('D108_Detail', '')).strip()
            if v01 or det:
                evid_html = (
                    f"<div style='font-weight: 600; color: #1e293b;'>01 Verdict: {_html.escape(v01 or '-')} "
                    f"<span style='color: #64748b; font-weight: normal;'>({_html.escape(t01 or '-')})</span></div>"
                    f"<div style='color: #475569; margin-top: 2px;'>{_html.escape(det)}</div>"
                )
            else:
                evid_html = _html.escape(str(row.get('Reason', '')))

            row_bg = "#fff1f2" if str(d108_code).startswith("ANOMALY_") and d108_code != "ANOMALY_NEW_AFTER_01" else "#fefce8"
            gi_rows_html += f"""
            <tr style="border-bottom: 1px solid #fde047; background-color: {row_bg};">
                <td style="padding: 7px 10px; font-weight: bold; color: #854d0e;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; color: #4a5568;">{str(row.get('Material_Desc', ''))[:24]}</td>
                <td style="padding: 7px 10px; text-align: center;">{_format_target_sloc(row.get('Target_SLoc', ''))}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #a16207;">{row.get('Deficit_Qty', 0):,.1f}</td>
                <td style="padding: 7px 10px;">{d108_badge}</td>
                <td style="padding: 7px 10px; color: #334155; font-size: 11px;">{_html.escape(str(linked_gi))}</td>
                <td style="padding: 7px 10px; font-size: 11px;">{evid_html}</td>
            </tr>
            """
    else:
        gi_rows_html = '<tr><td colspan="7" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No backlogs blocked by active GI interface errors.</td></tr>'

    # 2. Other Unresolved Table Rows
    other_rows_html = ""
    if not other_unres_df.empty:
        for _, row in other_unres_df.iterrows():
            other_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #1a202c;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; color: #4a5568;">{str(row.get('Material_Desc', ''))[:28]}</td>
                <td style="padding: 7px 10px; text-align: center;">{_format_target_sloc(row.get('Target_SLoc', ''))}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #e53e3e;">{row.get('Deficit_Qty', 0):,.1f}</td>
                <td style="padding: 7px 10px; text-align: center; color: #718096; font-weight: bold;">{row.get('Error_Code', '')}</td>
                <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">
                    {row.get('Reason', '')}
                    {f"<br><span style='color: #2b6cb0; font-size: 10px;'>[Best Candidate: {row.get('Best_Cand_SLoc')} | SAP: {float(row.get('Best_Cand_SAP', 0)):,.1f} | WMS: {float(row.get('Best_Cand_WMS', 0)):,.1f}]</span>" if row.get('Best_Cand_SLoc') and not pd.isna(row.get('Best_Cand_SLoc')) else ""}
                </td>
            </tr>
            """
    else:
        other_rows_html = '<tr><td colspan="6" style="padding: 10px; text-align: center; color: #38a169; font-weight: bold;">No pending items requiring planner action.</td></tr>'

    # 2.5. Remaining SAP Deficits Table Rows (D32)
    deficit_rows_html = ""
    if remaining_deficits:
        for raw_d in remaining_deficits:
            d = _normalize_deficit_dict(raw_d)
            mat = d['Material']
            desc = d['Material_Desc'][:28]
            sloc = d['Storage_Loc']
            sap_stk = d['SAP_Stock']
            wms_stk = d['WMS_Stock']
            def_qty = d['Deficit_Qty']
            acc_post = d['Accuracy_After']
            acc_str = f"{acc_post * 100:.1f}%" if acc_post is not None else "-"
            days = d['consecutive_days']
            days_badge = f"{days}d" if days > 1 else "1d"
            days_style = "background-color: #fee2e2; color: #991b1b;" if days >= 3 else "background-color: #f1f5f9; color: #475569;"

            is_sl49 = d['is_sl49']
            if is_sl49:
                loc_badge = f'<span style="background-color: #fef3c7; color: #92400e; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px;">{sloc} [SL49 Quality DMA &bull; Not a cycle-count target]</span>'
            else:
                loc_badge = f'<span style="background-color: #e0f2fe; color: #0369a1; padding: 2px 6px; border-radius: 4px; font-weight: bold; font-size: 11px;">{sloc}</span>'

            sust_val = d.get('is_sustained_deficit')
            if sust_val == 1:
                sust_badge = f'<span style="background-color: #fee2e2; color: #991b1b; padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold;">{_EN["SUSTAINED_DEFICIT"]}</span>'
            elif sust_val == 0:
                sust_badge = f'<span style="background-color: #f1f5f9; color: #475569; padding: 1px 6px; border-radius: 3px; font-size: 10px;">{_EN["NEW_OR_PENDING"]}</span>'
            else:
                sust_badge = '<span style="background-color: #f8fafc; color: #94a3b8; padding: 1px 6px; border-radius: 3px; font-size: 10px;">-</span>'

            deficit_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 6px 10px; font-weight: bold; color: #1a202c;">{mat}</td>
                <td style="padding: 6px 10px; color: #4a5568;">{desc}</td>
                <td style="padding: 6px 10px; text-align: center;">{loc_badge}</td>
                <td style="padding: 6px 10px; text-align: center;">{sust_badge}</td>
                <td style="padding: 6px 10px; text-align: right; color: #4b5563;">{sap_stk:,.1f}</td>
                <td style="padding: 6px 10px; text-align: right; color: #4b5563;">{wms_stk:,.1f}</td>
                <td style="padding: 6px 10px; text-align: right; font-weight: bold; color: #dc2626;">{def_qty:,.1f}</td>
                <td style="padding: 6px 10px; text-align: center; color: #2563eb;">{acc_str}</td>
                <td style="padding: 6px 10px; text-align: center;"><span style="{days_style} padding: 1px 6px; border-radius: 3px; font-size: 10px; font-weight: bold;">{days_badge}</span></td>
            </tr>
            """
    else:
        deficit_rows_html = '<tr><td colspan="9" style="padding: 10px; text-align: center; color: #38a169; font-size: 12px;">No warehouse materials with unresolved SAP inventory deficit (WMS &le; SAP across all locations).</td></tr>'

    # 3. Auto-Resolved Table Rows (Cumulative Daily Transfers: 06:00 & 08:00)
    resolved_rows_html = ""
    if execution_results:
        for r in execution_results:
            cycle_badge = r.get("Cycle", "08:00 AM")
            if "06" in cycle_badge:
                badge_style = "background-color: #dbeafe; color: #1e40af; border: 1px solid #bfdbfe;"
            elif "08" in cycle_badge:
                badge_style = "background-color: #dcfce7; color: #166534; border: 1px solid #bbf7d0;"
            else:
                badge_style = "background-color: #f1f5f9; color: #475569; border: 1px solid #cbd5e1;"

            resolved_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 6px 10px; text-align: center;"><span style="{badge_style} padding: 2px 7px; border-radius: 4px; font-weight: bold; font-size: 11px;">{cycle_badge}</span></td>
                <td style="padding: 6px 10px; font-weight: bold; color: #1a202c;">{r.get('Material', '')}</td>
                <td style="padding: 6px 10px; text-align: center; color: #4a5568;">{r.get('From', '')} &rarr; {r.get('To', '')}</td>
                <td style="padding: 6px 10px; text-align: right; font-weight: bold; color: #22543d;">{float(r.get('Qty', 0)):,.1f}</td>
                <td style="padding: 6px 10px; text-align: center; color: #2b6cb0; font-family: monospace;">{r.get('Header_Text', 'Backlog_Clear')}</td>
                <td style="padding: 6px 10px; color: #047857; font-size: 11px;">{r.get('Result', '')}</td>
            </tr>
            """
    else:
        resolved_rows_html = '<tr><td colspan="6" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No transfers executed today.</td></tr>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 920px; margin: 0 auto; background-color: #ffffff; border-radius: 6px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #1a365d; color: #ffffff; padding: 18px 24px; }}
            .header h1 {{ margin: 0 0 4px 0; font-size: 18px; font-weight: bold; }}
            .header p {{ margin: 0; opacity: 0.85; font-size: 12px; }}
            .content {{ padding: 20px 24px; }}
            .section-header {{ font-size: 13px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; color: #2d3748; margin: 20px 0 8px 0; border-bottom: 2px solid #edf2f7; padding-bottom: 4px; }}
            .gr-header {{ color: #c2410c; border-bottom: 2px solid #fed7aa; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 16px; }}
            th {{ background-color: #f1f5f9; color: #475569; padding: 8px 10px; text-align: left; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #cbd5e1; }}
            .gr-th {{ background-color: #ffedd5; color: #9a3412; border-bottom: 1px solid #fdba74; }}
            .footer {{ background-color: #f8fafc; padding: 14px 24px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>Daily Backlog (COGI) Resolution & Status Report ({run_date}, {run_time})</h1>
                <p>Plant: {_get_ops_env('SAP_PLANT', 'P001')} (Headlamp) | Cumulative Resolution Report (06:20 AM & 08:15 AM Cycles) | Variance Minimization (&Delta;V &le; 0)</p>
            </div>
            <div class="content">
                {correction_banner_html}
                <!-- KPI SUMMARY TABLE -->
                <table style="width: 100%; border: none; margin-bottom: 16px;">
                    <tr>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #eff6ff; border: 1px solid #bfdbfe; padding: 10px; border-radius: 4px;">
                                <div style="color: #1d4ed8; font-size: 10px; font-weight: bold; text-transform: uppercase;">Total Daily Backlog</div>
                                <div style="color: #1e3a8a; font-size: 18px; font-weight: bold;">{total_count}</div>
                                <div style="color: #475569; font-size: 10px;">Deficit: {total_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; padding: 10px; border-radius: 4px;">
                                <div style="color: #15803d; font-size: 10px; font-weight: bold; text-transform: uppercase;">Transfer Posted</div>
                                <div style="color: #14532d; font-size: 18px; font-weight: bold;">{resolved_count}</div>
                                <div style="color: #475569; font-size: 10px;">Transferred: {resolved_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #fff7ed; border: 1px solid #fed7aa; padding: 10px; border-radius: 4px;">
                                <div style="color: #c2410c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Pending GR Errors</div>
                                <div style="color: #9a3412; font-size: 18px; font-weight: bold;">{gr_blocked_count}</div>
                                <div style="color: #475569; font-size: 10px;">Planner ({gr_blocked_qty:,.1f})</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #fefce8; border: 1px solid #fef08a; padding: 10px; border-radius: 4px;">
                                <div style="color: #a16207; font-size: 10px; font-weight: bold; text-transform: uppercase;">GI-Linked Backlogs</div>
                                <div style="color: #854d0e; font-size: 18px; font-weight: bold;">{gi_blocked_count}</div>
                                <div style="color: #475569; font-size: 10px;">{_html.escape(gi_kpi_sub)}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #fef2f2; border: 1px solid #fecaca; padding: 10px; border-radius: 4px;">
                                <div style="color: #b91c1c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Other Backlog Items</div>
                                <div style="color: #7f1d1d; font-size: 18px; font-weight: bold;">{other_unres_count}</div>
                                <div style="color: #475569; font-size: 10px;">Deficit: {other_unres_qty:,.1f}</div>
                            </div>
                        </td>
                    </tr>
                </table>

                <!-- SECTION 1: PENDING GR ERRORS -->
                <div class="section-header gr-header">Pending Goods Receipt (GR) Errors &bull; Action Required by Each Planner</div>
                <table>
                    <thead>
                        <tr>
                            <th class="gr-th">Material</th>
                            <th class="gr-th">Description</th>
                            <th class="gr-th" style="text-align: center;">Target SLoc</th>
                            <th class="gr-th" style="text-align: right;">COGI Qty</th>
                            <th class="gr-th" style="text-align: right;">Pending GR</th>
                            <th class="gr-th" style="text-align: center;">Vendor</th>
                            <th class="gr-th">Reason / Action Guide</th>
                        </tr>
                    </thead>
                    <tbody>
                        {gr_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 1.5: PENDING GI ERRORS (D44 Movement Block + D108 Root-Cause Verdict) -->
                <div class="section-header" style="color: #a16207; border-bottom: 2px solid #fef08a;">GI Error-Linked Backlogs &bull; Root Cause by Preceding 01 Verdict (D71 Movement Block Active)</div>
                <div style="font-size: 11px; color: #64748b; margin-top: 2px; margin-bottom: 8px;">
                    02 runs immediately after 01 (06:00 &amp; 08:00). Remaining GI-linked backlogs are classified by why 01 could not clear the linked GI error (Stock shortage D / Book mismatch A / Suspected missing GR F / Pending GR B / ⚠️ Anomaly).
                </div>
                <table>
                    <thead>
                        <tr>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047;">Material</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047;">Description</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047; text-align: center;">Target SLoc</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047; text-align: right;">COGI Qty</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047;">Backlog Root Cause (D108)</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047;">Linked GI Error</th>
                            <th style="background-color: #fef9c3; color: #854d0e; border-bottom: 1px solid #fde047;">01 Verdict &bull; Cycle Time &amp; Evidence</th>
                        </tr>
                    </thead>
                    <tbody>
                        {gi_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 2: PENDING BACKLOG ITEMS -->
                <div class="section-header" style="color: #b91c1c; border-bottom: 2px solid #fecaca;">Pending Backlog Items</div>
                <table>
                    <thead>
                        <tr>
                            <th>Material</th>
                            <th>Description</th>
                            <th style="text-align: center;">Target SLoc</th>
                            <th style="text-align: right;">Deficit Qty</th>
                            <th style="text-align: center;">SAP Error</th>
                            <th>Reason / Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {other_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 2.5: REMAINING SAP DEFICITS (D32) -->
                <div class="section-header" style="color: #2563eb; border-bottom: 2px solid #bfdbfe;">Remaining SAP Deficits (Plant-wide SAP Shortage &bull; WMS &gt; SAP)</div>
                <div style="font-size: 11px; color: #64748b; margin-top: 4px; margin-bottom: 8px;">
                    Materials where WMS physical inventory exceeds SAP system stock after all available warehouse transfers. Requires manual review or PO delivery check.
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>Material</th>
                            <th>Description</th>
                            <th style="text-align: center;">SLoc</th>
                            <th style="text-align: center;">Persistence</th>
                            <th style="text-align: right;">SAP Stock</th>
                            <th style="text-align: right;">WMS Stock</th>
                            <th style="text-align: right; color: #dc2626;">Deficit Qty</th>
                            <th style="text-align: center;">Accuracy</th>
                            <th style="text-align: center;">Streak</th>
                        </tr>
                    </thead>
                    <tbody>
                        {deficit_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 3: CUMULATIVE TRANSFERS POSTED -->
                <div class="section-header">Completed Transfer Postings (Today's Cumulative: 06:20 & 08:15 Runs)</div>
                <table>
                    <thead>
                        <tr>
                            <th style="text-align: center; width: 90px;">Run Cycle</th>
                            <th>Material</th>
                            <th style="text-align: center;">Route</th>
                            <th style="text-align: right;">Transfer Qty</th>
                            <th style="text-align: center;">Header Text</th>
                            <th>SAP Posting Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {resolved_rows_html}
                    </tbody>
                </table>

                <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; padding: 10px 14px; border-radius: 4px; font-size: 11px; color: #64748b;">
                    Attached: <strong>Unresolved_Backlog_Report_{run_date.replace('-', '')}_0800.xlsx</strong> containing complete itemized details.
                </div>
            </div>
            <div class="footer">
                Automated Inventory Team Pipeline &bull; {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} {_get_ops_env('SAP_PLANT', 'P001')} &bull; Cumulative Daily Resolution Report
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_morning_backlog_report(
    plan_df: pd.DataFrame,
    execution_results: List[Dict],
    final_cogi_items: Optional[List[Dict[str, Any]]] = None,
    run_date: str = None,
    run_time: str = "08:00 AM EST",
    to_recipients: List[str] = None,
    cc_recipients: List[str] = None,
    is_test: bool = False,
    initial_count: Optional[int] = None,
    initial_qty: Optional[float] = None,
    remaining_deficits: Optional[List[Dict[str, Any]]] = None,
    is_correction: bool = False,
    correction_note: Optional[str] = None
) -> bool:
    """Sends the morning backlog report via Outlook COM."""
    if not run_date:
        run_date = datetime.now().strftime("%Y-%m-%d")

    if is_test:
        _dyn_test_to = os.getenv("TEST_EMAIL_RECIPIENT", os.getenv("BACKLOG_TEST_TO", ""))
        to_list = [x.strip() for x in _dyn_test_to.split(",") if is_valid_recipient(x)] if _dyn_test_to else TEST_TO
        cc_list = []
    else:
        _dyn_prod_to = [x.strip() for x in os.getenv("BACKLOG_PROD_TO", "").split(",") if is_valid_recipient(x)]
        _dyn_prod_cc = [x.strip() for x in os.getenv("BACKLOG_PROD_CC", "").split(",") if is_valid_recipient(x)]
        eff_prod_to = PROD_TO or _dyn_prod_to
        eff_prod_cc = PROD_CC or _dyn_prod_cc
        to_list = to_recipients if to_recipients is not None else eff_prod_to
        cc_list = cc_recipients if cc_recipients is not None else (eff_prod_cc if to_recipients is None else [])

    # Fail closed validation: ensure valid recipients exist
    to_list = [r for r in to_list if is_valid_recipient(r)]
    cc_list = [r for r in cc_list if is_valid_recipient(r)]

    if not to_list:
        logger.error("❌ [NOTIFIER] Recipient list is empty or contains only invalid/placeholder addresses. Aborting email send (fail closed).")
        return False

    excel_path = generate_unresolved_excel(
        plan_df, run_date.replace("-", ""),
        final_cogi_items=final_cogi_items,
        remaining_deficits=remaining_deficits,
        is_test=is_test
    )
    html_body = build_email_html(
        plan_df,
        execution_results,
        run_date,
        run_time,
        final_cogi_items=final_cogi_items,
        initial_count=initial_count,
        initial_qty=initial_qty,
        remaining_deficits=remaining_deficits,
        is_correction=is_correction,
        correction_note=correction_note
    )

    prefixes = []
    if is_test:
        prefixes.append("[TEST]")
    if is_correction:
        prefixes.append(_EN["CORRECTION"])
    prefix_str = (" ".join(prefixes) + " ") if prefixes else ""
    subject = f"{prefix_str}Daily Backlog (COGI) Resolution & Status Report ({run_date}, {run_time})"

    logger.info(f"[NOTIFIER] Sending email ('{subject}') to: {to_list} (CC: {cc_list})...")

    try:
        import subprocess
        res = subprocess.run('tasklist /fi "imagename eq OUTLOOK.EXE"', shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if "OUTLOOK.EXE" not in res.stdout:
            logger.info("[NOTIFIER] Starting Outlook for MAPI initialization...")
            subprocess.Popen("start outlook", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            import time
            time.sleep(4)

        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = outlook.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass
        mail = outlook.CreateItem(0)  # 0: olMailItem

        _assert_clean_ops_output(subject, html_body, context="02_JOB backlog_notifier")
        # D111: non-blocking Hangul check before operational dispatch (user-only test routing skipped)
        check_report_language(
            "02_JOB_backlog_notifier", subject, html_body,
            attachments=[excel_path] if excel_path else [], is_user_only=is_test,
        )
        mail.Subject = subject
        mail.To = "; ".join(to_list)
        if cc_list:
            mail.CC = "; ".join(cc_list)

        mail.HTMLBody = html_body

        if excel_path and excel_path.exists():
            mail.Attachments.Add(str(excel_path.resolve()))
            logger.info(f"[NOTIFIER] Attached Excel: {excel_path.name}")

        mail.Send()
        logger.info(f"✅ [NOTIFIER] Morning backlog report email successfully sent to {to_list}!")
        return True

    except Exception as e:
        logger.error(f"❌ [NOTIFIER] Failed to send email via Outlook: {e}")
        return False


if __name__ == "__main__":
    print("[NOTIFIER READY]")
