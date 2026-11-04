"""
Hourly GI Interface Error Notification Module
=============================================
01_Inventory_Team / 01_GR_GI_Error (Plant P001 Headlamp)
Sends concise, executive HTML email reports to designated recipient (GI_ERROR_REPORT_TO) after every hourly cycle:
1. What was processed (MB1B 311 Transfer Posted)
2. What is blocked by GR errors (Protected GR-Dependent)
3. What cannot be processed (Insufficient stock / Excluded 901/902)
"""

import os
import sys
import time
import logging
import subprocess
from pathlib import Path
import os
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

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

logger = logging.getLogger("GINotifier")
_DEPT_DIR = Path(__file__).resolve().parents[1]
if str(_DEPT_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPT_DIR))
from report_labels import EN as _EN, check_report_language

def is_valid_recipient(addr: str) -> bool:
    """Returns True if addr is a non-empty, non-placeholder email address."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip().lower()
    return bool(clean) and "@" in clean and "example.com" not in clean

_env_gi_to = os.getenv("GI_ERROR_REPORT_TO")
DEFAULT_TO = [x.strip() for x in _env_gi_to.split(",") if is_valid_recipient(x)] if _env_gi_to else []



def _build_d110_cooldown_html(plan_df: Optional[pd.DataFrame]) -> str:
    """D110 (#197): Renders 24-hour consecutive failure cooldown callout for both 30-min and 07:30 GI reports."""
    if plan_df is None or plan_df.empty or "Status" not in plan_df.columns:
        return ""
    cd_df = plan_df[plan_df["Status"].astype(str).str.strip() == "SKIPPED_COOLDOWN_GUARD_D110"]
    if cd_df.empty:
        return ""
    items_html = ""
    seen_keys = set()
    for _, row in cd_df.iterrows():
        mat = str(row.get("Material") or "").strip()
        route = str(row.get("D110_Cooldown_Route") or row.get("Deficit_Loc") or "").strip()
        if (mat, route) in seen_keys:
            continue
        seen_keys.add((mat, route))
        cnt = int(float(row.get("D110_Cooldown_Consec_Fails") or 3))
        until_s = str(row.get("D110_Cooldown_Until") or "-").strip()
        err_s = str(row.get("D110_Cooldown_Last_Error") or row.get("Reason") or "").strip()
        items_html += (
            f"<li style='margin: 3px 0;'><strong>{mat}</strong> (<code>{route}</code>) &mdash; "
            f"<strong>{cnt} consecutive</strong> failure(s) &rarr; auto-retry paused until <code>{until_s}</code> "
            f"<span style='color:#7f1d1d;'>({err_s[:110]})</span></li>"
        )
    if not items_html:
        return ""
    return f"""
                <div style="background-color: #fef2f2; border: 1px solid #fca5a5; border-radius: 6px; padding: 9px 13px; font-size: 11px; color: #991b1b; margin-bottom: 14px;">
                    <strong>&#9208; D110 Consecutive-Failure 24h Cooldown ({len(seen_keys)} item(s) &bull; auto-retry paused):</strong>
                    <ul style="margin: 5px 0 0 18px; padding: 0;">{items_html}</ul>
                </div>
    """


def _build_d109_section_html(postings_24h_df: Optional[pd.DataFrame], plan_df: Optional[pd.DataFrame] = None) -> str:
    """D109: Renders 1-B callout table + B-4/B-5 human-check banners in the 07:30 daily GI report."""
    rows_html = ""
    count = 0
    if postings_24h_df is not None and not postings_24h_df.empty and "posting_type" in postings_24h_df.columns:
        d109_df = postings_24h_df[postings_24h_df["posting_type"].astype(str).str.contains("GUARD_RELEASED_D109", na=False)]
        for _, r in d109_df.iterrows():
            count += 1
            rsn = str(r.get("reason") or "GUARD_RELEASED_D109")
            rows_html += f"""
            <tr style="border-bottom: 1px solid #fde68a; background-color: #fffbeb;">
                <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{r.get('exec_time', '')}</td>
                <td style="padding: 7px 10px; font-weight: bold; color: #92400e;">{r.get('material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{r.get('from_sloc', '')} &rarr; {r.get('to_sloc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #b45309;">{float(r.get('transfer_qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; font-family: monospace; font-weight: bold; color: #1e40af;">{r.get('doc_number') or '-'}</td>
                <td style="padding: 7px 10px; color: #78350f; font-size: 11px;">{rsn}</td>
            </tr>
            """

    notices_html = ""
    if plan_df is not None and not plan_df.empty and "Material" in plan_df.columns:
        suspected_f_mats = []
        already_rel_mats = []
        for _, pr in plan_df.iterrows():
            pmat = str(pr.get("Material") or "").strip()
            pst = str(pr.get("Status") or "").strip()
            prsn = str(pr.get("Reason") or "")
            is_f = (
                bool(pr.get("D109_Suspected_Unposted_GR"))
                if "D109_Suspected_Unposted_GR" in pr and pd.notna(pr.get("D109_Suspected_Unposted_GR"))
                else ("verify sap gr" in prsn.lower() or "SAP 입고 전기 확인 필요" in prsn)
            )
            if is_f and pmat and pmat not in suspected_f_mats:
                suspected_f_mats.append(pmat)
            if pst == "SKIPPED_D109_ALREADY_RELEASED" and pmat and pmat not in already_rel_mats:
                already_rel_mats.append(pmat)

        if suspected_f_mats:
            mats_s = ", ".join(suspected_f_mats)
            notices_html += f"""
                <div style="background-color: #fff1f2; border: 1px solid #fda4af; border-radius: 6px; padding: 9px 13px; font-size: 11px; color: #9f1239; margin-bottom: 14px;">
                    <strong>&#9888; Suspected Missing GR (F &bull; Human Review):</strong> Material(s) <strong>{mats_s}</strong> &mdash; SL00 WMS=SAP=0 while physical surplus exists at destination SLoc (0 active GR errors). <strong>Please verify SAP GR posting</strong> (automated transfer skipped).
                </div>
            """
        if already_rel_mats:
            rmats_s = ", ".join(already_rel_mats)
            notices_html += f"""
                <div style="background-color: #fff7ed; border: 1px solid #fdba74; border-radius: 6px; padding: 9px 13px; font-size: 11px; color: #9a3412; margin-bottom: 14px;">
                    <strong>&#9888; Residual GI Error After D109 Release (Retry Blocked):</strong> Material(s) <strong>{rmats_s}</strong> &mdash; GI error remained after prior <code>GUARD_RELEASED_D109</code> posting (duplicate-posting safeguard active &bull; human review required).
                </div>
            """

    table_html = ""
    if count > 0:
        table_html = f"""
                <!-- SECTION 1B: D109 SL00 GUARD RELEASE POSTINGS -->
                <div class="section-header" style="color: #b45309; border-bottom: 2px solid #fcd34d;">&#9889; 1-B. SL00 Arrival-Hold Guard Release Postings (GUARD_RELEASED_D109: {count})</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 14%;">Posted At</th>
                            <th style="width: 13%;">Material</th>
                            <th style="width: 13%; text-align: center;">Route</th>
                            <th style="width: 10%; text-align: right;">Posted Qty</th>
                            <th style="width: 14%;">Mat. Doc</th>
                            <th style="width: 36%;">D109 Release Basis (Cycles &bull; 3-File GR Check &bull; Gap Trend)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {rows_html}
                    </tbody>
                </table>
        """
    d113_html = ""
    if postings_24h_df is not None and not postings_24h_df.empty and "posting_type" in postings_24h_df.columns:
        d113_df = postings_24h_df[
            postings_24h_df["posting_type"].astype(str).str.contains("ROUNDTRIP_CLEAR_VIA_DEST_D113|ROLLBACK_D113", na=False)
        ]
        if not d113_df.empty:
            d113_rows_html = ""
            for _, r in d113_df.iterrows():
                p_st = str(r.get("post_status") or "POSTED")
                rsn_113 = str(r.get("reason") or "ROUNDTRIP_CLEAR_VIA_DEST_D113")
                st_col = "#15803d" if p_st == "POSTED" else "#b91c1c"
                d113_rows_html += f"""
                <tr style="border-bottom: 1px solid #bbf7d0; background-color: #f0fdf4;">
                    <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{r.get('exec_time', '')}</td>
                    <td style="padding: 7px 10px; font-weight: bold; color: #14532d;">{r.get('material', '')}</td>
                    <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{r.get('from_sloc', '')} &rarr; {r.get('to_sloc', '')}</td>
                    <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #15803d;">{float(r.get('transfer_qty', 0)):,.1f}</td>
                    <td style="padding: 7px 10px; font-family: monospace; font-weight: bold; color: #1e40af;">{r.get('doc_number') or '-'}</td>
                    <td style="padding: 7px 10px; font-size: 11px; color: {st_col}; font-weight: bold;">{p_st} &mdash; {rsn_113}</td>
                </tr>
                """
            d113_html = f"""
                <!-- SECTION 1B-D113: DESTINATION WAREHOUSE ROUNDTRIP CLEAR (ENGLISH PER D111/D113) -->
                <div class="section-header" style="color: #15803d; border-bottom: 2px solid #86efac;">&#128260; 1-B (D113). SL00&rarr;SL0x Same-Cycle Destination Roundtrip Clear (ROUNDTRIP_CLEAR_VIA_DEST_D113: {len(d113_df)} item(s))</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 14%;">Exec Time</th>
                            <th style="width: 13%;">Material</th>
                            <th style="width: 13%; text-align: center;">Route</th>
                            <th style="width: 10%; text-align: right;">Qty (EA)</th>
                            <th style="width: 14%;">SAP Doc No.</th>
                            <th style="width: 36%;">Status &amp; D113 Verification Summary (SL00 WMS=SAP, GR=0, Net Change 0)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d113_rows_html}
                    </tbody>
                </table>
            """

    d115_html = ""
    if postings_24h_df is not None and not postings_24h_df.empty and "posting_type" in postings_24h_df.columns:
        d115_df = postings_24h_df[
            postings_24h_df["posting_type"].astype(str).str.contains("DEST_PUMP_CLEAR_D115|ROLLBACK_D115", na=False)
        ]
        if not d115_df.empty:
            d115_rows_html = ""
            for _, r in d115_df.iterrows():
                p_st115 = str(r.get("post_status") or "POSTED")
                rsn_115 = str(r.get("reason") or "DEST_PUMP_CLEAR_D115")
                st_col115 = "#0369a1" if p_st115 == "POSTED" else "#b91c1c"
                d115_rows_html += f"""
                <tr style="border-bottom: 1px solid #bae6fd; background-color: #f0f9ff;">
                    <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{r.get('exec_time', '')}</td>
                    <td style="padding: 7px 10px; font-weight: bold; color: #0c4a6e;">{r.get('material', '')}</td>
                    <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{r.get('from_sloc', '')} &rarr; {r.get('to_sloc', '')}</td>
                    <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #0369a1;">{float(r.get('transfer_qty', 0)):,.1f}</td>
                    <td style="padding: 7px 10px; font-family: monospace; font-weight: bold; color: #1e40af;">{r.get('doc_number') or '-'}</td>
                    <td style="padding: 7px 10px; font-size: 11px; color: {st_col115}; font-weight: bold;">{p_st115} &mdash; {rsn_115}</td>
                </tr>
                """
            d115_html = f"""
                <!-- SECTION 1B-D115: DESTINATION DEFICIT-PUMP CLEAR (ENGLISH PER D111/D115) -->
                <div class="section-header" style="color: #0369a1; border-bottom: 2px solid #7dd3fc;">&#128260; 1-B (D115). Destination Warehouse Deficit-Pump Clear (DEST_PUMP_CLEAR_D115: {len(d115_df)} item(s))</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 14%;">Exec Time</th>
                            <th style="width: 13%;">Material</th>
                            <th style="width: 13%; text-align: center;">Pump Route</th>
                            <th style="width: 10%; text-align: right;">Pump Qty (EA)</th>
                            <th style="width: 14%;">SAP Doc No.</th>
                            <th style="width: 36%;">Status &amp; D115 Verification Summary (Dest WMS-SAP &ge; 0.9x Err, GR=0, |SAP-WMS| Reduced)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {d115_rows_html}
                    </tbody>
                </table>
            """

    return table_html + d113_html + d115_html + notices_html


def build_gi_email_html(
    plan_df: pd.DataFrame,
    posting_results: List[Dict[str, Any]],
    run_date: str,
    run_time: str,
    cycle_label: str = "30-min cycle (Daytime)"
) -> str:
    # Filter active target errors (excluding 901, 902)
    active_df = plan_df[~plan_df['Mvt_Original'].isin(['901', '902'])] if plan_df is not None and not plan_df.empty else pd.DataFrame()
    excluded_90x_df = plan_df[plan_df['Mvt_Original'].isin(['901', '902'])] if plan_df is not None and not plan_df.empty else pd.DataFrame()

    total_count = len(active_df)
    total_qty = active_df['Transfer_Qty'].sum() if not active_df.empty and 'Transfer_Qty' in active_df.columns else 0.0

    # 1. Processed Items (Transfer Posted)
    resolved_count = len(posting_results) if posting_results else 0
    resolved_qty = sum(float(r.get("Qty", 0.0)) for r in posting_results) if posting_results else 0.0
    roundtrip_posted_count = sum(1 for r in (posting_results or []) if any(k in str(r.get("Posting_Type", "")) for k in ("ROUNDTRIP", "DEST_PUMP_CLEAR_D115")))

    # 2. Blocked by Active GR Errors
    gr_blocked_df = active_df[active_df['Status'] == 'PROTECTED_GR_DEPENDENT'] if not active_df.empty else pd.DataFrame()
    gr_blocked_count = len(gr_blocked_df)
    gr_blocked_qty = gr_blocked_df['Transfer_Qty'].sum() if not gr_blocked_df.empty else 0.0

    # 3. Chained Cascade Auto-Resolve (Downstream unblocked by root transfer)
    chain_df = active_df[active_df['Status'] == 'CHAIN_CASCADE_RESOLVE'] if not active_df.empty else pd.DataFrame()
    chain_count = len(chain_df)
    chain_qty = chain_df['Transfer_Qty'].sum() if not chain_df.empty else 0.0

    # 4. Cannot Process (Insufficient stock)
    cannot_process_df = active_df[
        (active_df['Action'] == 'SKIP') & (~active_df['Status'].isin(['PROTECTED_GR_DEPENDENT', 'CHAIN_CASCADE_RESOLVE', 'AUTO_CLEARED_BY_CURRENT_STOCK']))
    ] if not active_df.empty else pd.DataFrame()
    cannot_process_count = len(cannot_process_df)
    cannot_process_qty = cannot_process_df['Transfer_Qty'].sum() if not cannot_process_df.empty else 0.0

    # Table 1: Processed (Transfer Posted)
    resolved_rows_html = ""
    if posting_results:
        for r in posting_results:
            p_type = r.get("Posting_Type", "FULL_RESOLUTION")
            if "GUARD_RELEASED_D109" in p_type:
                type_badge = "SL00 Guard Release (D109)"
            elif "ROUNDTRIP_CLEAR_VIA_DEST_D113" in p_type:
                type_badge = "Dest Roundtrip (D113)"
            elif "DEST_PUMP_CLEAR_D115" in p_type:
                type_badge = "Dest Pump (D115)"
            elif "ROUNDTRIP" in p_type:
                type_badge = "Roundtrip"
            elif "FULL" in p_type or "READY" in p_type:
                type_badge = "Full Resolution"
            else:
                type_badge = "Partial Rebalance"
            badge_color = "#b45309" if "D109" in type_badge else ("#15803d" if ("Full" in type_badge or "Roundtrip" in type_badge or "D113" in type_badge) else "#0369a1")
            badge_bg = "#fef3c7" if "D109" in type_badge else ("#dcfce7" if ("Full" in type_badge or "Roundtrip" in type_badge or "D113" in type_badge) else "#e0f2fe")

            resolved_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #1a202c;">{r.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{r.get('From', '')} &rarr; {r.get('To', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #15803d;">{float(r.get('Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; text-align: center;"><span style="background-color: {badge_bg}; color: {badge_color}; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: bold;">{type_badge}</span></td>
                <td style="padding: 7px 10px; color: #047857; font-size: 11px;">{r.get('Result', '')}</td>
            </tr>
            """
    else:
        resolved_rows_html = '<tr><td colspan="5" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No transfer postings executed in this cycle.</td></tr>'

    # Table 2: Blocked by Active GR Errors
    gr_rows_html = ""
    if not gr_blocked_df.empty:
        for _, row in gr_blocked_df.iterrows():
            gr_rows_html += f"""
            <tr style="border-bottom: 1px solid #fed7aa; background-color: #fffaf0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #9a3412;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '311')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #c2410c;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #7c2d12; font-size: 11px;">Active ZTCODE_GI_ERR GR error pending &bull; Held to prevent duplicate deduction; auto-resolves once GR is posted.</td>
            </tr>
            """
    else:
        gr_rows_html = '<tr><td colspan="5" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No items currently held for active GR errors.</td></tr>'

    # Table 3: Chained Cascade Auto-Resolve (Downstream unblocked by root transfer)
    chain_rows_html = ""
    if not chain_df.empty:
        for _, row in chain_df.iterrows():
            chain_rows_html += f"""
            <tr style="border-bottom: 1px solid #e9d5ff; background-color: #faf5ff;">
                <td style="padding: 7px 10px; font-weight: bold; color: #6b21a8;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #7e22ce;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #581c87; font-size: 11px;">{row.get('Reason', '')}</td>
            </tr>
            """
    else:
        chain_rows_html = '<tr><td colspan="5" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No downstream chain-cascade items pending.</td></tr>'

    # Table 4: Cannot Process (Insufficient stock / Excluded)
    cannot_rows_html = ""
    if not cannot_process_df.empty:
        for _, row in cannot_process_df.iterrows():
            cannot_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #1a202c;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #e53e3e;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{row.get('Reason', row.get('Status', ''))}</td>
            </tr>
            """
    else:
        cannot_rows_html = '<tr><td colspan="5" style="padding: 10px; text-align: center; color: #38a169; font-weight: bold; font-size: 12px;">No unprocessable GI errors remaining (all clear).</td></tr>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7fafc; margin: 0; padding: 20px; }}
            .container {{ max-width: 820px; margin: 0 auto; background-color: #ffffff; border-radius: 6px; border: 1px solid #e2e8f0; overflow: hidden; }}
            .header {{ background: #1e293b; color: #ffffff; padding: 18px 24px; }}
            .header h1 {{ margin: 0 0 4px 0; font-size: 18px; font-weight: bold; }}
            .header p {{ margin: 0; opacity: 0.85; font-size: 12px; }}
            .content {{ padding: 20px 24px; }}
            .section-header {{ font-size: 12px; font-weight: bold; text-transform: uppercase; letter-spacing: 0.5px; color: #334155; margin: 20px 0 8px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 4px; }}
            .gr-header {{ color: #c2410c; border-bottom: 2px solid #fed7aa; }}
            .chain-header {{ color: #7e22ce; border-bottom: 2px solid #e9d5ff; }}
            .cannot-header {{ color: #b91c1c; border-bottom: 2px solid #fecaca; }}
            .res-header {{ color: #15803d; border-bottom: 2px solid #bbf7d0; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 16px; }}
            th {{ background-color: #f8fafc; color: #475569; padding: 8px 10px; text-align: left; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #cbd5e1; }}
            .gr-th {{ background-color: #ffedd5; color: #9a3412; border-bottom: 1px solid #fdba74; }}
            .chain-th {{ background-color: #faf5ff; color: #6b21a8; border-bottom: 1px solid #d8b4fe; }}
            .res-th {{ background-color: #f0fdf4; color: #166534; border-bottom: 1px solid #bbf7d0; }}
            .footer {{ background-color: #f8fafc; padding: 12px 24px; font-size: 11px; color: #94a3b8; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>[GI Error] {cycle_label} Auto-Resolution &amp; Status Report ({run_date} {run_time})</h1>
                <p>Plant: {_get_ops_env('SAP_PLANT', 'P001')} (Headlamp) | 24H Continuous Pipeline ({cycle_label}) | Stock Accuracy Optimization (&Delta;V &le; 0)</p>
            </div>
            <div class="content">
                <!-- KPI SUMMARY TABLE (5-METRIC GRID) -->
                <table style="width: 100%; border: none; margin-bottom: 16px;">
                    <tr>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #f1f5f9; border: 1px solid #cbd5e1; padding: 8px; border-radius: 4px;">
                                <div style="color: #475569; font-size: 10px; font-weight: bold; text-transform: uppercase;">Total GI Errors</div>
                                <div style="color: #0f172a; font-size: 18px; font-weight: bold;">{total_count}</div>
                                <div style="color: #64748b; font-size: 11px;">Short Qty: {total_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; padding: 8px; border-radius: 4px;">
                                <div style="color: #15803d; font-size: 10px; font-weight: bold; text-transform: uppercase;">Transfer Posted</div>
                                <div style="color: #14532d; font-size: 18px; font-weight: bold;">{resolved_count}</div>
                                <div style="color: #475569; font-size: 11px;">Posted Qty: {resolved_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #faf5ff; border: 1px solid #e9d5ff; padding: 8px; border-radius: 4px;">
                                <div style="color: #7e22ce; font-size: 10px; font-weight: bold; text-transform: uppercase;">Chain Auto-Resolve</div>
                                <div style="color: #581c87; font-size: 18px; font-weight: bold;">{chain_count}</div>
                                <div style="color: #7e22ce; font-size: 11px;">Qty: {chain_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #fff7ed; border: 1px solid #fed7aa; padding: 8px; border-radius: 4px;">
                                <div style="color: #c2410c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Pending GR</div>
                                <div style="color: #9a3412; font-size: 18px; font-weight: bold;">{gr_blocked_count}</div>
                                <div style="color: #475569; font-size: 11px;">Held Qty: {gr_blocked_qty:,.1f}</div>
                            </div>
                        </td>
                        <td style="width: 20%; padding: 3px;">
                            <div style="background-color: #fef2f2; border: 1px solid #fecaca; padding: 8px; border-radius: 4px;">
                                <div style="color: #b91c1c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Cannot Process</div>
                                <div style="color: #7f1d1d; font-size: 18px; font-weight: bold;">{cannot_process_count}</div>
                                <div style="color: #475569; font-size: 11px;">Short Qty: {cannot_process_qty:,.1f}</div>
                            </div>
                        </td>
                    </tr>
                </table>

                <!-- SECTION 1: TRANSFER POSTED (RESOLVED) -->
                <div class="section-header res-header">&check; 1. Transfer Postings Completed &bull; Real-Time MB1B 311 Resolution ({resolved_count} item(s), incl. {roundtrip_posted_count} roundtrip)</div>
                <table>
                    <thead>
                        <tr>
                            <th class="res-th">Material</th>
                            <th class="res-th" style="text-align: center;">Route (From &rarr; To)</th>
                            <th class="res-th" style="text-align: right;">Posted Qty</th>
                            <th class="res-th" style="text-align: center;">Type</th>
                            <th class="res-th">SAP Posting Result</th>
                        </tr>
                    </thead>
                    <tbody>
                        {resolved_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 2: BLOCKED BY ACTIVE GR ERRORS -->
                <div class="section-header gr-header">&bull; 2. Held for Active GR Errors &bull; Duplicate-Deduction Safeguard ({gr_blocked_count} item(s))</div>
                <table>
                    <thead>
                        <tr>
                            <th class="gr-th">Material</th>
                            <th class="gr-th" style="text-align: center;">Mvt</th>
                            <th class="gr-th" style="text-align: center;">Deficit SLoc</th>
                            <th class="gr-th" style="text-align: right;">Short Qty</th>
                            <th class="gr-th">Status &amp; Next Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {gr_rows_html}
                    </tbody>
                </table>

                <!-- SECTION 3: CHAINED CASCADE AUTO-RESOLVE -->
                <div class="section-header chain-header">&#128279; 3. Downstream Chain Auto-Resolve &bull; Clears Once Upstream Transfer Posts ({chain_count} item(s))</div>
                <table>
                    <thead>
                        <tr>
                            <th class="chain-th">Material</th>
                            <th class="chain-th" style="text-align: center;">Mvt</th>
                            <th class="chain-th" style="text-align: center;">Deficit SLoc</th>
                            <th class="chain-th" style="text-align: right;">Qty</th>
                            <th class="chain-th">Chain Resolution Basis</th>
                        </tr>
                    </thead>
                    <tbody>
                        {chain_rows_html}
                    </tbody>
                </table>

                {_build_d110_cooldown_html(plan_df)}

                <!-- SECTION 4: CANNOT PROCESS -->
                <div class="section-header cannot-header">&times; 4. Cannot Process &bull; Insufficient Stock / Guard Hold ({cannot_process_count} item(s))</div>
                <table>
                    <thead>
                        <tr>
                            <th>Material</th>
                            <th style="text-align: center;">Mvt</th>
                            <th style="text-align: center;">Deficit SLoc</th>
                            <th style="text-align: right;">Short Qty</th>
                            <th>Reason &amp; Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {cannot_rows_html}
                    </tbody>
                </table>

                <!-- 901/902 EXCLUDED NOTICE -->
                <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 4px; padding: 10px 14px; font-size: 11px; color: #64748b; margin-top: 12px; margin-bottom: 8px;">
                    <strong>&bull; Exclusion Policy:</strong> Movement types 901 (scrap/sale) and 902 ({len(excluded_90x_df)} item(s)) are excluded from automated transfer posting by operational safety policy.
                </div>
            </div>
            <div class="footer">
                Inventory Team Automation &bull; {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} {_get_ops_env('SAP_PLANT', 'P001')} &bull; Routine GI Error Resolution Report
            </div>
        </div>
    </body>
    </html>
    """
    return html



def send_hourly_gi_report(
    plan_df: pd.DataFrame,
    posting_results: List[Dict[str, Any]],
    run_date: str = None,
    run_time: str = None,
    to_recipients: List[str] = None,
    cycle_type: str = None
) -> bool:
    """Dispatches the routine GI resolution report (30-min daytime / 1-hour nighttime) via Outlook COM."""
    now = datetime.now()
    if not run_date:
        run_date = now.strftime("%Y-%m-%d")
    if not run_time:
        run_time = now.strftime("%I:%M %p EST")

    if not cycle_type:
        # Daytime 08:00 ~ 17:00 -> 30-min cycle, Otherwise -> 1-hour cycle
        if 8 <= now.hour < 17 or (now.hour == 17 and now.minute <= 5):
            cycle_type = _EN["CYCLE_30M_DAY"]
        else:
            cycle_type = _EN["CYCLE_1H_NIGHT"]

    to_list = to_recipients if to_recipients is not None else DEFAULT_TO
    to_list = [r for r in to_list if is_valid_recipient(r)]
    if not to_list:
        logger.error("[GI NOTIFIER] ❌ No valid recipients found (fail closed). Skipping routine email dispatch.")
        return False

    subject = f"[GI Error] {cycle_type} Auto-Resolution & Status Report ({run_date} {run_time})"

    # Slot Deduplication Guardrail (Prevents duplicate sends for same 30m/1h slot)
    try:
        from db_manager import is_email_slot_already_sent, record_email_slot_sent
        slot_minute = 30 if 15 <= now.minute < 45 else 0
        slot_hour = now.hour if now.minute < 45 else (now.hour + 1) % 24
        slot_date = run_date.replace("-", "")
        slot_key = f"ROUTINE_{slot_date}_{slot_hour:02d}{slot_minute:02d}"

        if is_email_slot_already_sent(slot_key):
            logger.warning(f"🛡️ [EMAIL DEDUP] Routine report for slot '{slot_key}' was ALREADY dispatched. Suppressing duplicate send!")
            return True
    except Exception as e_dedup:
        logger.warning(f"    ⚠️ Deduplication check warning: {e_dedup}")
        slot_key = None

    html_body = build_gi_email_html(plan_df, posting_results, run_date, run_time, cycle_label=cycle_type)

    logger.info(f"[GI NOTIFIER] Dispatching {cycle_type} GI report to: {to_list} (Slot: {slot_key})...")

    try:
        # Check / Start Outlook if needed
        res = subprocess.run('tasklist /fi "imagename eq OUTLOOK.EXE"', shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if "OUTLOOK.EXE" not in res.stdout:
            logger.info("[GI NOTIFIER] Launching Outlook.Application background process...")
            subprocess.Popen("start outlook", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(4)

        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = outlook.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass

        _test_rec = os.getenv("TEST_EMAIL_RECIPIENT", "").strip().lower()
        _is_user_only = bool(_test_rec and len(to_list) == 1 and to_list[0].strip().lower() == _test_rec) or subject.startswith("[TEST]")
        check_report_language("01_JOB_hourly_gi_report", subject=subject, html_body=html_body, is_user_only=_is_user_only)

        mail = outlook.CreateItem(0)  # 0: olMailItem
        _assert_clean_ops_output(subject, html_body, context="01_JOB gi_notifier")
        mail.Subject = subject
        mail.To = "; ".join(to_list)
        mail.HTMLBody = html_body

        mail.Send()
        logger.info(f"[GI NOTIFIER] ✅ Successfully dispatched hourly report to {to_list}!")

        if slot_key:
            try:
                record_email_slot_sent(slot_key, cycle_type, subject, to_list)
            except Exception as e_rec:
                logger.warning(f"    ⚠️ Could not record slot key to DB: {e_rec}")

        return True
    except Exception as e:
        logger.error(f"[GI NOTIFIER] ❌ Failed to dispatch email via Outlook: {e}")
        return False


def _build_d107_section_html(d107_payload: Optional[Dict[str, Any]]) -> str:
    if not d107_payload:
        return ""

    coll_ts = d107_payload.get("collection_time_edt") or "-"
    coll_mode = d107_payload.get("collection_mode") or "-"
    cleared_0700 = int(d107_payload.get("cleared_by_0700_count", 0))
    prev_date = d107_payload.get("prev_date") or "-"
    cand_list = d107_payload.get("candidates") or []
    hold_list = d107_payload.get("holds") or []
    prev_31x_cnt = int(d107_payload.get("prev_day_31x_remaining_count", 0))
    p2b = d107_payload.get("phase2b_summary") or {}
    if p2b:
        _p2_bundles = int(p2b.get("saved_draft_count", 0) or 0)
        _p2_rows = int(p2b.get("saved_row_count", 0) or 0)
        _p2_subj = str(p2b.get("draft_subject") or "Request to delete IWMS_GI of YYYY/MM/DD")
        _p2_pdate = str(p2b.get("prior_cohort_date") or prev_date)
        _p2_clr = int(p2b.get("prior_cleared_count", 0) or 0)
        _p2_pnd = int(p2b.get("prior_pending_count", 0) or 0)
        _p2_stl = int(p2b.get("prior_stale_count", 0) or 0)
        _p2_stl_lbls = ", ".join(p2b.get("prior_stale_labels") or [])
        _p2_stl_note = f' (incl. <strong>{_p2_stl}</strong> <code>{_p2_stl_lbls or "stale"}</code> &mdash; no auto re-save)' if _p2_stl > 0 else ""
        _p2_st = str(p2b.get("status") or "-")
        phase2b_line_html = (
            f'<br><strong style="color:#7f1d1d;">&bull; 07:23 Portal Temp-Save (D107 2b &bull; {_p2_st}):</strong> '
            f'Temp-saved today: <strong>{_p2_bundles}</strong> draft(s) (<strong>{_p2_rows}</strong> row(s)) &bull; '
            f'Temp Box title: <code>{_p2_subj}</code> &bull; '
            f'Prior temp-saved ({_p2_pdate}): <strong>{_p2_clr}</strong> cleared after user submit / '
            f'<strong>{_p2_pnd}</strong> still pending user submit{_p2_stl_note}'
        )
    else:
        phase2b_line_html = ""

    cand_rows_html = ""
    if cand_list:
        for c in cand_list:
            ev = c.get("d107_evidence") or {}
            first_seen = c.get("first_seen_edt") or c.get("error_date") or "-"
            ev_text = (
                f"① Age: ErrDate {c.get('error_date')} (first seen {first_seen}) | "
                f"② 01 Verdict: {ev.get('cond_2_01_status', c.get('plan_01_status', ''))} | "
                f"③ 7d GR/GAP: {ev.get('cond_3_gr_7d', '0')} | "
                f"④ Source Stock: {ev.get('cond_4_source_stock', '')} | "
                f"⑤ Pump/Roundtrip: {ev.get('cond_5_no_pump', 'No surplus available')}"
            )
            diff_note = c.get("d107_remaining_diff_note") or c.get("stock_summary") or "-"
            p0800 = c.get("post_0800_status") or "⏳ Pre-08:00 cycle (post-check pending)"
            p0800_color = "#b91c1c" if "⚠️" in str(p0800) else ("#15803d" if "✅" in str(p0800) else "#64748b")
            cand_rows_html += f"""
            <tr style="border-bottom: 1px solid #fecaca; background-color: #fff5f5;">
                <td style="padding: 7px 9px; font-weight: bold; color: #7f1d1d;">{c.get('material', '')}</td>
                <td style="padding: 7px 9px; text-align: center; color: #334155;">{c.get('mvt', '')}<br><span style="font-size:10px;color:#475569;">{c.get('from_sloc', '')}&rarr;{c.get('to_sloc', '')}</span></td>
                <td style="padding: 7px 9px; text-align: right; font-weight: bold; color: #b91c1c;">{float(c.get('qty') or 0):,.1f}</td>
                <td style="padding: 7px 9px; text-align: center; font-size: 11px; color: #334155;">{c.get('error_date', '')}<br><span style="font-size:10px;color:#64748b;">{first_seen}</span></td>
                <td style="padding: 7px 9px; font-size: 11px; color: #1e293b;">{ev_text}</td>
                <td style="padding: 7px 9px; font-size: 11px; color: #475569;">{diff_note}</td>
                <td style="padding: 7px 9px; font-size: 11px; font-weight: bold; color: {p0800_color};">{p0800}</td>
            </tr>
            """
    else:
        cand_rows_html = (
            f'<tr><td colspan="7" style="padding: 12px; text-align: center; color: #166534; font-weight: bold; font-size: 12px;">'
            f'No unresolvable 311/312 GI errors from {prev_date} or earlier met all strict D107 conditions 1–6 (0 candidates).'
            f'</td></tr>'
        )

    hold_html = ""
    if hold_list:
        h_rows = ""
        for h in hold_list:
            h_rs = "; ".join(h.get("d107_exclusion_reasons") or [])
            h_rows += f"""
            <tr style="border-bottom: 1px solid #fde68a; background-color: #fffbeb;">
                <td style="padding: 6px 9px; font-weight: bold; color: #92400e;">{h.get('material', '')}</td>
                <td style="padding: 6px 9px; text-align: center;">{h.get('mvt', '')} ({h.get('from_sloc', '')}&rarr;{h.get('to_sloc', '')})</td>
                <td style="padding: 6px 9px; text-align: right; font-weight: bold; color: #b45309;">{float(h.get('qty') or 0):,.1f}</td>
                <td style="padding: 6px 9px; text-align: center;">{h.get('error_date', '')}</td>
                <td style="padding: 6px 9px; font-size: 11px; color: #78350f;">{h_rs} ({h.get('stock_summary', '')})</td>
            </tr>
            """
        hold_html = f"""
        <div style="margin-top: -6px; margin-bottom: 18px; border: 1px solid #fcd34d; border-radius: 6px; overflow: hidden;">
            <div style="background-color: #fef3c7; padding: 7px 12px; font-size: 11px; font-weight: bold; color: #92400e;">
                &#9888; Held for Human Review ({len(hold_list)} item(s)) — Excluded from Delete Candidates Due to Uncertainty
            </div>
            <table style="margin-bottom: 0;">
                <thead>
                    <tr>
                        <th style="width: 15%; background-color:#fffbeb;">Material</th>
                        <th style="width: 15%; text-align:center; background-color:#fffbeb;">Mvt &bull; Route</th>
                        <th style="width: 12%; text-align:right; background-color:#fffbeb;">Qty</th>
                        <th style="width: 12%; text-align:center; background-color:#fffbeb;">Error Date</th>
                        <th style="width: 46%; background-color:#fffbeb;">Hold Reason &amp; Warehouse Stock</th>
                    </tr>
                </thead>
                <tbody>{h_rows}</tbody>
            </table>
        </div>
        """

    return f"""
                <!-- SECTION D107: UNRESOLVABLE GI ERROR DELETE CANDIDATES (PHASE 1 READ-ONLY) -->
                <div class="section-header" style="color: #7f1d1d; border-bottom: 2px solid #fca5a5;">
                    &#128465; Delete-Draft Candidates (D107 Phase 1 Validation — Manual Reference | Candidates: {len(cand_list)} / Holds: {len(hold_list)})
                </div>
                <div style="background-color: #fff1f2; border: 1px solid #fecdd3; border-radius: 6px; padding: 10px 14px; font-size: 11px; color: #881337; margin-bottom: 10px;">
                    <strong>&bull; Snapshot Collected:</strong> <code>{coll_ts}</code> ({coll_mode}) &bull;
                    <strong>Cleared/Excluded by 07:00 Cycle Postings:</strong> <strong>{cleared_0700}</strong> &bull;
                    <strong>Remaining Prev-Day ({prev_date} or earlier) 311/312:</strong> {prev_31x_cnt}<br>
                    <span style="color:#4c0519;">* Deleting a GI error removes only the interface error record without changing SAP/WMS stock quantities (Submit remains locked to user review).</span>
                    {phase2b_line_html}
                </div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 11%;">Material</th>
                            <th style="width: 10%; text-align: center;">Mvt &bull; Route</th>
                            <th style="width: 9%; text-align: right;">Qty</th>
                            <th style="width: 11%; text-align: center;">Err Date / First Seen</th>
                            <th style="width: 28%;">Conditions 1–5 Evidence</th>
                            <th style="width: 17%;">Remaining SAP–WMS Diff</th>
                            <th style="width: 14%;">Post-08:00 Check</th>
                        </tr>
                    </thead>
                    <tbody>
                        {cand_rows_html}
                    </tbody>
                </table>
                {hold_html}
    """


def build_daily_cumulative_email_html(
    plan_df: pd.DataFrame,
    posting_results: List[Dict[str, Any]],
    report_date: str,
    report_time: str,
    postings_24h_df: Optional[pd.DataFrame] = None,
    metrics_24h: Optional[Dict[str, Any]] = None,
    runs_24h_df: Optional[pd.DataFrame] = None,
    target_prev_date: Optional[str] = None,
    schedule_time_label: str = "08:00",
    d107_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Builds the comprehensive daily GI report HTML (supports D106 prev-day 00:00~23:59 @ 07:30 and legacy 24H)."""
    # Current active errors breakdown (excluding 901, 902; cast Mvt_Original to str for Excel-loaded DataFrames)
    if plan_df is not None and not plan_df.empty and 'Mvt_Original' in plan_df.columns:
        mvt_str = plan_df['Mvt_Original'].astype(str).str.strip().str.replace(r'\.0$', '', regex=True)
        active_df = plan_df[~mvt_str.isin(['901', '902'])].copy()
        active_df['Mvt_Original'] = mvt_str[~mvt_str.isin(['901', '902'])]
        excluded_90x_df = plan_df[mvt_str.isin(['901', '902'])].copy()
    else:
        active_df = pd.DataFrame()
        excluded_90x_df = pd.DataFrame()

    current_error_count = len(active_df)
    current_error_qty = active_df['Transfer_Qty'].sum() if not active_df.empty and 'Transfer_Qty' in active_df.columns else 0.0

    gr_blocked_df = active_df[active_df['Status'] == 'PROTECTED_GR_DEPENDENT'] if not active_df.empty else pd.DataFrame()
    gr_blocked_count = len(gr_blocked_df)
    gr_blocked_qty = gr_blocked_df['Transfer_Qty'].sum() if not gr_blocked_df.empty else 0.0

    chain_df = active_df[active_df['Status'] == 'CHAIN_CASCADE_RESOLVE'] if not active_df.empty else pd.DataFrame()
    chain_count = len(chain_df)
    chain_qty = chain_df['Transfer_Qty'].sum() if not chain_df.empty else 0.0

    cannot_process_df = active_df[
        (active_df['Action'] == 'SKIP') & (~active_df['Status'].isin(['PROTECTED_GR_DEPENDENT', 'CHAIN_CASCADE_RESOLVE', 'AUTO_CLEARED_BY_CURRENT_STOCK']))
    ] if not active_df.empty else pd.DataFrame()
    cannot_process_count = len(cannot_process_df)
    cannot_process_qty = cannot_process_df['Transfer_Qty'].sum() if not cannot_process_df.empty else 0.0

    # 24-hour aggregated metrics
    m24 = metrics_24h or {}
    total_runs_24h = m24.get('total_runs', 0)
    successful_postings_24h = m24.get('successful_postings', len(postings_24h_df) if postings_24h_df is not None else 0)
    total_posted_qty_24h = m24.get('total_posted_qty', 0.0)

    # Table 1: 24-hour Transfer Postings Table
    postings_rows_html = ""
    if postings_24h_df is not None and not postings_24h_df.empty:
        for _, r in postings_24h_df.iterrows():
            p_type = str(r.get("posting_type", "FULL_RESOLUTION"))
            if "GUARD_RELEASED_D109" in p_type:
                type_badge = "SL00 Guard Release (D109)"
            elif "ROUNDTRIP_CLEAR_VIA_DEST_D113" in p_type:
                type_badge = "Dest Roundtrip (D113)"
            elif "DEST_PUMP_CLEAR_D115" in p_type:
                type_badge = "Dest Pump (D115)"
            elif "ROUNDTRIP" in p_type:
                type_badge = "Roundtrip"
            elif "FULL" in p_type or "READY" in p_type:
                type_badge = "Full Resolution"
            else:
                type_badge = "Partial Rebalance"
            badge_color = "#b45309" if "D109" in type_badge else ("#15803d" if ("Full" in type_badge or "Roundtrip" in type_badge or "D113" in type_badge) else "#0369a1")
            badge_bg = "#fef3c7" if "D109" in type_badge else ("#dcfce7" if ("Full" in type_badge or "Roundtrip" in type_badge or "D113" in type_badge) else "#e0f2fe")

            status = str(r.get("post_status", "POSTED"))
            status_color = "#15803d" if status == "POSTED" else "#dc2626"

            postings_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{r.get('exec_time', '')}</td>
                <td style="padding: 7px 10px; font-weight: bold; color: #1a202c;">{r.get('material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{r.get('from_sloc', '')} &rarr; {r.get('to_sloc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #15803d;">{float(r.get('transfer_qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; font-family: monospace; font-weight: bold; color: #1e40af;">{r.get('doc_number') or '-'}</td>
                <td style="padding: 7px 10px; text-align: center;"><span style="background-color: {badge_bg}; color: {badge_color}; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: bold;">{type_badge}</span></td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: {status_color}; font-size: 11px;">{status}</td>
            </tr>
            """
    else:
        empty_post_msg = (
            f"No transfer postings executed on {target_prev_date} (00:00~23:59)."
            if target_prev_date
            else "No transfer postings executed in the past 24 hours."
        )
        postings_rows_html = f'<tr><td colspan="7" style="padding: 12px; text-align: center; color: #718096; font-size: 12px;">{empty_post_msg}</td></tr>'

    # Table 2: Current Active Remaining Errors
    remaining_rows_html = ""
    if not gr_blocked_df.empty:
        for _, row in gr_blocked_df.iterrows():
            remaining_rows_html += f"""
            <tr style="border-bottom: 1px solid #fed7aa; background-color: #fffaf0;">
                <td style="padding: 7px 10px; font-weight: bold; color: #9a3412;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '311')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #c2410c;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #9a3412; font-weight: bold;">Pending GR (B)</td>
                <td style="padding: 7px 10px; color: #7c2d12; font-size: 11px;">Active ZTCODE_GI_ERR GR error pending &bull; Held to prevent duplicate deduction (auto-resolves once GR posts)</td>
            </tr>
            """
    if not chain_df.empty:
        for _, row in chain_df.iterrows():
            remaining_rows_html += f"""
            <tr style="border-bottom: 1px solid #e9d5ff; background-color: #faf5ff;">
                <td style="padding: 7px 10px; font-weight: bold; color: #6b21a8;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #7e22ce;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #7e22ce; font-weight: bold;">Chain Auto-Resolve</td>
                <td style="padding: 7px 10px; color: #581c87; font-size: 11px;">{row.get('Reason', '')}</td>
            </tr>
            """
    if not cannot_process_df.empty:
        for _, row in cannot_process_df.iterrows():
            _rsn_str = str(row.get("Reason", ""))
            _is_f_row = (
                bool(row.get("D109_Suspected_Unposted_GR"))
                if ("D109_Suspected_Unposted_GR" in row and pd.notna(row.get("D109_Suspected_Unposted_GR")))
                else ("verify sap gr" in _rsn_str.lower() or "SAP 입고 전기 확인 필요" in _rsn_str)
            )
            _st_str = str(row.get("Status", ""))
            _st_label = (
                "Cooldown Hold (D110 · 24h)"
                if _st_str == "SKIPPED_COOLDOWN_GUARD_D110"
                else (
                    "Suspected Missing GR (F · Human Review)"
                    if _is_f_row
                    else (
                        "Residual After D109 (No Retry)"
                        if _st_str == "SKIPPED_D109_ALREADY_RELEASED"
                        else (
                            "SL00 Arrival Hold (D37/D109)"
                            if _st_str == "SKIPPED_PENDING_INTERFACE_SL00"
                            else "Insufficient Stock (SKIP)"
                        )
                    )
                )
            )
            remaining_rows_html += f"""
            <tr style="border-bottom: 1px solid #fecaca; background-color: #fef2f2;">
                <td style="padding: 7px 10px; font-weight: bold; color: #991b1b;">{row.get('Material', '')}</td>
                <td style="padding: 7px 10px; text-align: center; color: #4a5568;">{row.get('Mvt_Original', '')}</td>
                <td style="padding: 7px 10px; text-align: center; font-weight: bold; color: #2b6cb0;">{row.get('Deficit_Loc', '')}</td>
                <td style="padding: 7px 10px; text-align: right; font-weight: bold; color: #b91c1c;">{float(row.get('Transfer_Qty', 0)):,.1f}</td>
                <td style="padding: 7px 10px; color: #b91c1c; font-weight: bold;">{_st_label}</td>
                <td style="padding: 7px 10px; color: #4a5568; font-size: 11px;">{row.get('Reason', row.get('Status', 'No stock available in alternate SLocs'))}</td>
            </tr>
            """
    if not remaining_rows_html:
        remaining_rows_html = '<tr><td colspan="6" style="padding: 12px; text-align: center; color: #166534; font-weight: bold; font-size: 12px;">No remaining active GI errors (all clear).</td></tr>'

    # Table 3: 24-hour Run History Table (recent 15 runs)
    runs_rows_html = ""
    if runs_24h_df is not None and not runs_24h_df.empty:
        for _, r in runs_24h_df.head(15).iterrows():
            runs_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 6px 10px; color: #334155; font-size: 11px;">{r.get('run_time', '')}</td>
                <td style="padding: 6px 10px; font-family: monospace; color: #64748b; font-size: 10px;">{r.get('batch_id', '')}</td>
                <td style="padding: 6px 10px; text-align: center; font-weight: bold; color: #0f172a;">{r.get('total_gi_errors', 0)}</td>
                <td style="padding: 6px 10px; text-align: center; font-weight: bold; color: #15803d;">{r.get('postings_executed', 0)}</td>
                <td style="padding: 6px 10px; text-align: right; color: #64748b; font-size: 11px;">{float(r.get('elapsed_seconds', 0)):.1f}s</td>
                <td style="padding: 6px 10px; text-align: center; color: #15803d; font-size: 11px; font-weight: bold;">{r.get('status', 'COMPLETED')}</td>
            </tr>
            """
    else:
        runs_rows_html = '<tr><td colspan="6" style="padding: 10px; text-align: center; color: #718096; font-size: 12px;">No pipeline run history recorded.</td></tr>'

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; margin: 0; padding: 24px; }}
            .container {{ max-width: 900px; margin: 0 auto; background-color: #ffffff; border-radius: 8px; border: 1px solid #cbd5e1; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #ffffff; padding: 22px 28px; }}
            .header h1 {{ margin: 0 0 6px 0; font-size: 20px; font-weight: 700; letter-spacing: -0.3px; }}
            .header p {{ margin: 0; opacity: 0.9; font-size: 13px; color: #cbd5e1; }}
            .content {{ padding: 24px 28px; }}
            .section-header {{ font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: #1e293b; margin: 24px 0 10px 0; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; }}
            .res-header {{ color: #15803d; border-bottom: 2px solid #86efac; }}
            .rem-header {{ color: #b91c1c; border-bottom: 2px solid #fca5a5; }}
            .run-header {{ color: #0369a1; border-bottom: 2px solid #93c5fd; }}
            table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 18px; }}
            th {{ background-color: #f1f5f9; color: #334155; padding: 9px 10px; text-align: left; font-size: 11px; text-transform: uppercase; border-bottom: 2px solid #cbd5e1; font-weight: 700; }}
            .footer {{ background-color: #f8fafc; padding: 14px 28px; font-size: 11px; color: #64748b; border-top: 1px solid #e2e8f0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>{f"[GI Error] Daily Comprehensive Report (Prev-Day {target_prev_date} Resolution & Status)" if target_prev_date else "[GI Error] Daily Comprehensive Report (24H Cumulative Resolution & Status)"}</h1>
                <p>Plant: {_get_ops_env('SAP_PLANT', 'P001')} (Headlamp) &bull; Reported At: {report_date} {report_time} &bull; Scope: {f"Prev-Day {target_prev_date} 00:00 ~ 23:59 (Local Time)" if target_prev_date else "Past 24H Cumulative Log (Prev 08:00 ~ Today 08:00)"}</p>
            </div>
            <div class="content">
                <!-- 4 KPI SUMMARY CARDS -->
                <table style="width: 100%; border: none; margin-bottom: 20px;">
                    <tr>
                        <td style="width: 25%; padding: 4px;">
                            <div style="background-color: #f0f9ff; border: 1px solid #bae6fd; padding: 12px; border-radius: 6px;">
                                <div style="color: #0369a1; font-size: 10px; font-weight: bold; text-transform: uppercase;">{f"Prev-Day ({target_prev_date}) Runs" if target_prev_date else "24H Pipeline Runs"}</div>
                                <div style="color: #0c4a6e; font-size: 22px; font-weight: bold; margin-top: 2px;">{total_runs_24h} runs</div>
                                <div style="color: #0369a1; font-size: 11px; margin-top: 2px;">30m Day / 1h Night</div>
                            </div>
                        </td>
                        <td style="width: 25%; padding: 4px;">
                            <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; padding: 12px; border-radius: 6px;">
                                <div style="color: #15803d; font-size: 10px; font-weight: bold; text-transform: uppercase;">{f"Prev-Day ({target_prev_date}) Postings" if target_prev_date else "24H Transfer Postings"}</div>
                                <div style="color: #14532d; font-size: 22px; font-weight: bold; margin-top: 2px;">{successful_postings_24h} posted</div>
                                <div style="color: #166534; font-size: 11px; margin-top: 2px;">Posted Qty: {total_posted_qty_24h:,.1f} EA</div>
                            </div>
                        </td>
                        <td style="width: 25%; padding: 4px;">
                            <div style="background-color: #fef2f2; border: 1px solid #fecaca; padding: 12px; border-radius: 6px;">
                                <div style="color: #b91c1c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Remaining ({schedule_time_label})</div>
                                <div style="color: #7f1d1d; font-size: 22px; font-weight: bold; margin-top: 2px;">{current_error_count}</div>
                                <div style="color: #991b1b; font-size: 11px; margin-top: 2px;">Short Qty: {current_error_qty:,.1f} EA</div>
                            </div>
                        </td>
                        <td style="width: 25%; padding: 4px;">
                            <div style="background-color: #fff7ed; border: 1px solid #fed7aa; padding: 12px; border-radius: 6px;">
                                <div style="color: #c2410c; font-size: 10px; font-weight: bold; text-transform: uppercase;">Pending GR / Short</div>
                                <div style="color: #9a3412; font-size: 22px; font-weight: bold; margin-top: 2px;">{gr_blocked_count} / {cannot_process_count}</div>
                                <div style="color: #c2410c; font-size: 11px; margin-top: 2px;">Held: {gr_blocked_qty:,.1f} | Short: {cannot_process_qty:,.1f}</div>
                            </div>
                        </td>
                    </tr>
                </table>

                <!-- SECTION 1: RESOLUTION POSTINGS -->
                <div class="section-header res-header">&check; 1. {f"Prev-Day ({target_prev_date} 00:00~23:59)" if target_prev_date else "Past 24H"} Automated Transfer Postings (MB1B 311) ({successful_postings_24h})</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 14%;">Posted At</th>
                            <th style="width: 14%;">Material</th>
                            <th style="width: 16%; text-align: center;">Route (From&rarr;To)</th>
                            <th style="width: 12%; text-align: right;">Posted Qty</th>
                            <th style="width: 16%;">Mat. Doc</th>
                            <th style="width: 16%; text-align: center;">Type</th>
                            <th style="width: 12%; text-align: center;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {postings_rows_html}
                    </tbody>
                </table>

                {_build_d109_section_html(postings_24h_df, plan_df)}
                {_build_d110_cooldown_html(plan_df)}

                <!-- SECTION 2: CURRENT REMAINING ACTIVE ERRORS -->
                <div class="section-header rem-header">&bull; 2. Current ({schedule_time_label}) Remaining GI Errors &amp; Breakdown ({current_error_count})</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 14%;">Material</th>
                            <th style="width: 10%; text-align: center;">Mvt</th>
                            <th style="width: 12%; text-align: center;">Deficit SLoc</th>
                            <th style="width: 12%; text-align: right;">Short Qty</th>
                            <th style="width: 20%;">Category</th>
                            <th style="width: 32%;">Analysis &amp; Recommended Action</th>
                        </tr>
                    </thead>
                    <tbody>
                        {remaining_rows_html}
                    </tbody>
                </table>

                <!-- 901/902 EXCLUDED NOTICE -->
                <div style="background-color: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 14px; font-size: 11px; color: #475569; margin-top: -6px; margin-bottom: 20px;">
                    <strong>&bull; Exclusion Policy:</strong> Movement types 901 (scrap/sale) and 902 ({len(excluded_90x_df)} item(s)) are permanently excluded from automated transfer posting by operational safety policy.
                </div>

                {_build_d107_section_html(d107_payload)}

                <!-- SECTION 3: PIPELINE RUN LOG HISTORY -->
                <div class="section-header run-header">&#9200; 3. {f"Prev-Day ({target_prev_date})" if target_prev_date else "24H"} Pipeline Run Log Summary (Latest 15 Cycles)</div>
                <table>
                    <thead>
                        <tr>
                            <th style="width: 22%;">Run Time</th>
                            <th style="width: 28%;">Batch ID</th>
                            <th style="width: 14%; text-align: center;">GI Errors</th>
                            <th style="width: 14%; text-align: center;">Postings</th>
                            <th style="width: 11%; text-align: right;">Elapsed</th>
                            <th style="width: 11%; text-align: center;">Status</th>
                        </tr>
                    </thead>
                    <tbody>
                        {runs_rows_html}
                    </tbody>
                </table>
            </div>
            <div class="footer">
                Inventory Team Automation &bull; {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} {_get_ops_env('SAP_PLANT', 'P001')} &bull; Daily Comprehensive Report ({schedule_time_label} AM)
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_daily_cumulative_gi_report(
    plan_df: pd.DataFrame,
    posting_results: List[Dict[str, Any]],
    report_date: str = None,
    report_time: str = None,
    to_recipients: List[str] = None
) -> bool:
    """Dispatches the comprehensive 24-hour daily cumulative report at 08:00 AM via Outlook COM."""
    now = datetime.now()
    if not report_date:
        report_date = now.strftime("%Y-%m-%d")
    if not report_time:
        report_time = now.strftime("%I:%M %p EST")

    # Fetch 24-hour data from SQLite DB
    from db_manager import get_24h_postings, get_24h_metrics_summary, get_24h_run_history
    try:
        postings_24h_df = get_24h_postings()
        metrics_24h = get_24h_metrics_summary()
        runs_24h_df = get_24h_run_history(limit=48)
    except Exception as e_db:
        logger.warning(f"[GI NOTIFIER] Could not fetch 24h DB history: {e_db}")
        postings_24h_df = pd.DataFrame()
        metrics_24h = {}
        runs_24h_df = pd.DataFrame()

    to_list = to_recipients if to_recipients is not None else DEFAULT_TO
    to_list = [r for r in to_list if is_valid_recipient(r)]
    if not to_list:
        logger.error("[GI NOTIFIER] ❌ No valid recipients found (fail closed). Skipping Daily 24H email dispatch.")
        return False

    subject = f"[GI Error] Daily Comprehensive Report (24H Cumulative Resolution & Status) - {report_date} 08:00 AM"

    # Daily Deduplication Guardrail (Guarantees exactly ONE 08:00 AM report per calendar day)
    slot_key = f"DAILY_{report_date.replace('-', '')}_0800"
    try:
        from db_manager import is_email_slot_already_sent, record_email_slot_sent
        if is_email_slot_already_sent(slot_key):
            logger.warning(f"🛡️ [EMAIL DEDUP] Daily 24H report for '{slot_key}' was ALREADY dispatched today. Suppressing duplicate send!")
            return True
    except Exception as e_dedup:
        logger.warning(f"    ⚠️ Deduplication check warning: {e_dedup}")

    html_body = build_daily_cumulative_email_html(
        plan_df=plan_df,
        posting_results=posting_results,
        report_date=report_date,
        report_time=report_time,
        postings_24h_df=postings_24h_df,
        metrics_24h=metrics_24h,
        runs_24h_df=runs_24h_df
    )

    logger.info(f"[GI NOTIFIER] Dispatching Daily 24H Cumulative GI Report to: {to_list} (Slot: {slot_key})...")

    try:
        res = subprocess.run('tasklist /fi "imagename eq OUTLOOK.EXE"', shell=True, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if "OUTLOOK.EXE" not in res.stdout:
            logger.info("[GI NOTIFIER] Launching Outlook.Application background process...")
            subprocess.Popen("start outlook", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(4)

        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = outlook.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass

        _test_rec = os.getenv("TEST_EMAIL_RECIPIENT", "").strip().lower()
        _is_user_only = bool(_test_rec and len(to_list) == 1 and to_list[0].strip().lower() == _test_rec) or subject.startswith("[TEST]")
        check_report_language("01_JOB_hourly_gi_report", subject=subject, html_body=html_body, is_user_only=_is_user_only)

        _test_rec = os.getenv("TEST_EMAIL_RECIPIENT", "").strip().lower()
        _is_user_only = bool(_test_rec and len(to_list) == 1 and to_list[0].strip().lower() == _test_rec) or subject.startswith("[TEST]")
        check_report_language("01_JOB_daily_24h_gi_report", subject=subject, html_body=html_body, is_user_only=_is_user_only)

        mail = outlook.CreateItem(0)  # 0: olMailItem
        _assert_clean_ops_output(subject, html_body, context="01_JOB gi_notifier")
        mail.Subject = subject
        mail.To = "; ".join(to_list)
        mail.HTMLBody = html_body

        mail.Send()
        logger.info(f"[GI NOTIFIER] ✅ Successfully dispatched Daily 24H Cumulative Report to {to_list}!")

        try:
            record_email_slot_sent(slot_key, "DAILY_CUMULATIVE", subject, to_list)
        except Exception as e_rec:
            logger.warning(f"    ⚠️ Could not record daily slot key to DB: {e_rec}")

        return True
    except Exception as e:
        logger.error(f"[GI NOTIFIER] ❌ Failed to dispatch Daily 24H Cumulative Report via Outlook: {e}")
        return False
