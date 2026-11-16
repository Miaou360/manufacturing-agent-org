"""
02_DEPT_Logistics / 02_JOB_Delivery_Check - Email Notifier (#351 / #387)
========================================================================
Hard rules (`371 §3.6`, `#387`, `AGENTS.md §10` & `§11` D111):
  - Internal mails (`mode: "test"`): always exclusively to `TEST_EMAIL_RECIPIENT` (`CC = []`), `[TEST] ` prefix.
  - Supplier-facing `SRM_DeliveryStatus_*.xlsx` mail (`#387`):
      * Triggered ONLY when a C-truck closes (`is_pallet_truck == 0`) AND at least one valid `Warning` exists
        for that supplier (parts missing from morning ZM212 are excluded via Start-stock guard and shown in
        the user's internal mail as `start stock not in ZM212 – check`).
      * Controlled by `supplier_mail_mode` (default `"test"` -> user only `[TEST]`).
      * Setting `supplier_mail_mode: "live"` is strictly REFUSED (`PermissionError`) unless
        `supplier_mail_live_approved_by_user: true` is also explicitly set.
  - Every rendered subject & HTML body passes `assert_no_hangul()` (0 Hangul characters)
    and carries `Part #` + `Part Name` side by side (#279).
"""
from __future__ import annotations

import datetime as dt
import html
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DEPT_DIR = BASE_DIR.parent
MCP_ROOT = DEPT_DIR.parent
SRM_DIR = DEPT_DIR / "00_JOB_SRM_Monitoring"
INV_DEPT_DIR = MCP_ROOT / "01_DEPT_Inventory"

for _p in (str(BASE_DIR), str(DEPT_DIR), str(SRM_DIR), str(INV_DEPT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from delivery_db_manager import has_notice, record_notice  # noqa: E402
from report_labels import assert_no_hangul  # noqa: E402
from srm_env import assert_clean, company_name, load_env  # noqa: E402
from srm_mailer import Sender, _safe_send, addresses, outlook_send  # noqa: E402
from supplier_delivery_status import (  # noqa: E402
    MISSING_ZM212_INTERNAL_LABEL,
    build_supplier_delivery_status,
    write_supplier_delivery_status_file,
)

CONFIG_PATH = BASE_DIR / "config" / "delivery_check.json"

_CSS = "font-family:'Segoe UI',Calibri,Arial,sans-serif;font-size:13px;color:#0F172A"
_TH = "background:#1E293B;color:#FFFFFF;padding:5px 9px;font-size:12px;text-align:left;border:1px solid #CBD5E1"
_TD = "border:1px solid #CBD5E1;padding:4px 9px;font-size:12px"


def validate_supplier_mail_config(cfg: Dict[str, Any]) -> str:
    """Enforces #387 safety guard: `supplier_mail_mode='live'` is refused unless `supplier_mail_live_approved_by_user` is True."""
    sm_mode = str(cfg.get("supplier_mail_mode", "test")).strip().lower()
    if sm_mode not in ("test", "live"):
        raise ValueError(f"config/delivery_check.json supplier_mail_mode={sm_mode!r}; expected 'test' or 'live'")
    if sm_mode == "live" and cfg.get("supplier_mail_live_approved_by_user") is not True:
        raise PermissionError(
            "supplier_mail_mode='live' is refused: supplier_mail_live_approved_by_user must be explicitly true "
            "after user approval in chat (#387)."
        )
    return sm_mode


def load_delivery_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path else CONFIG_PATH
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    cfg["mode"] = "test"
    cfg.setdefault("supplier_mail_mode", "test")
    cfg.setdefault("supplier_mail_live_approved_by_user", False)
    validate_supplier_mail_config(cfg)
    cfg.setdefault("enabled", True)
    cfg.setdefault("allow_wms_env_login", True)
    cfg.setdefault("user_env", "TEST_EMAIL_RECIPIENT")
    cfg.setdefault("signature", "Logistics Team")
    return cfg


def _html_table(headers: List[str], rows: List[List[Any]]) -> str:
    th_html = "".join(f"<th style='{_TH}'>{html.escape(str(h))}</th>" for h in headers)
    tr_list: List[str] = []
    for r in rows:
        tds = "".join(f"<td style='{_TD}'>{html.escape(str(c))}</td>" for c in r)
        tr_list.append(f"<tr>{tds}</tr>")
    return f"<table style='border-collapse:collapse;margin:8px 0 14px'><tr>{th_html}</tr>{''.join(tr_list)}</table>"


def _test_banner() -> str:
    return (
        "<p style='background:#FFF2CC;padding:6px 10px;border:1px solid #BF8F00;font-size:12px'>"
        "<b>TEST MODE (User Only)</b> - Sent exclusively to TEST_EMAIL_RECIPIENT. "
        "No supplier or team recipients are configured.</p>"
    )


def _company_label() -> str:
    try:
        return company_name()
    except RuntimeError:
        return "Plant Logistics"


def resolve_supplier_status_recipients(
    vendor: str,
    cfg: Dict[str, Any],
    suppliers_rows: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[List[str], List[str], str, str]:
    """Returns `(to, cc, subject_prefix, banner)` for the supplier delivery status email (#387)."""
    sm_mode = validate_supplier_mail_config(cfg)
    user_addrs = addresses(cfg.get("user_env", "TEST_EMAIL_RECIPIENT"))
    v_low = (vendor or "").lower()
    matched_row: Dict[str, Any] = {}
    for r in (suppliers_rows or []):
        m = (r.get("match") or "").strip()
        if m and m.lower() in v_low:
            matched_row = r
            break

    to_env = matched_row.get("to_env") or "UNCONFIGURED_TO_ENV"
    cc_env = matched_row.get("cc_env") or "UNCONFIGURED_CC_ENV"
    if sm_mode != "live":
        banner = (
            f"TEST MODE - in live mode this supplier delivery status mail goes to .env {to_env} "
            f"(CC {cc_env} and you). Nothing was sent to the supplier."
        )
        return user_addrs, [], "[TEST] ", banner

    to_list = addresses(to_env) if matched_row.get("to_env") else []
    cc_raw = (addresses(cc_env) if matched_row.get("cc_env") else []) + user_addrs
    cc_list = [c for c in dict.fromkeys(cc_raw) if c not in to_list]
    return to_list, cc_list, "", ""


def build_supplier_delivery_status_email(
    model: Dict[str, Any],
    closed_truck_labels: List[str],
    cfg: Dict[str, Any],
    *,
    subject_prefix: str = "[TEST] ",
    banner: str = "",
) -> Dict[str, str]:
    vendor = model["vendor_display"]
    run_date: dt.date = model["run_date"]
    as_of_ts: dt.datetime = model["as_of_ts"]
    warnings: List[Dict[str, Any]] = model["warnings"]
    company = _company_label()
    sig = (cfg.get("signature") or "").strip() or f"{company} Logistics"
    closed_str = ", ".join(closed_truck_labels)

    subject = (
        f"{subject_prefix}{company} truck delivery status - {vendor} - "
        f"{run_date:%a} {run_date.month}/{run_date.day} ({closed_str} closed)"
    )

    warn_rows = []
    for w in warnings:
        d0 = w["days"][0]
        warn_rows.append([
            w["part_no"],
            w["part_name"],
            f"{d0['total_plan']:,.0f}",
            f"{float(d0['total_deliv']):,.0f}",
            w["warning"],
        ])

    parts_html = [f"<div style='{_CSS}'>"]
    if banner:
        parts_html.append(
            f"<p style='background:#FFF2CC;padding:6px 10px;border:1px solid #BF8F00;font-size:12px'>"
            f"<b>{html.escape(banner)}</b></p>"
        )
    parts_html += [
        f"<p>Hello {html.escape(vendor)} team,</p>",
        f"<p>Attached is the updated <b>{html.escape(company)} truck delivery status</b> as of "
        f"<b>{as_of_ts:%a %m/%d %H:%M} EST</b> (closed truck(s): <b>{html.escape(', '.join( model['closed_truck_labels'] ))}</b>). "
        f"Based on WMS receipts recorded so far, the following item(s) require additional delivery before the next scheduled truck to avoid a line shortage:</p>",
        _html_table(["Part #", "Part name", "Today Plan", "Delivered so far", "Warning"], warn_rows),
        "<p>Please review the <b>Truck plan</b> sheet in the attached file (<i>Plan</i> vs <i>Delivered</i> per truck) "
        "and let us know the recovery truck time.</p>",
        f"<p>Thank you,<br>{html.escape(sig)}</p></div>",
    ]
    body = "".join(parts_html)
    assert_no_hangul({"supplier_status_subject": subject, "supplier_status_body": body})
    return {"subject": subject, "body": body}


def build_truck_close_email(
    truck: Dict[str, Any],
    truck_verdicts: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    *,
    cleared_earlier_alarms: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    vendor = truck["vendor_display"]
    t_lbl = truck["truck_label"]
    dep_short = truck["departure_ts"][:16]
    if truck.get("is_pallet_truck"):
        dep_short = f"{dep_short} (Day-level)"
    status = truck["status"]
    sig = (cfg.get("signature") or "").strip() or f"{_company_label()} Logistics"

    counts: Dict[str, int] = {}
    for v in truck_verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    summary_bits = ", ".join(f"{k}: {n}" for k, n in sorted(counts.items())) or "0 lines"

    subject = f"[TEST] [Delivery Check] {vendor} truck {t_lbl} ({dep_short}) closed - {summary_bits}"

    rows = []
    for v in truck_verdicts:
        in_zm = bool(int(v.get("start_in_zm212", 1)))
        if v.get("line_shortage_alarm") and not in_zm:
            alarm_cell = MISSING_ZM212_INTERNAL_LABEL
        elif v.get("line_shortage_alarm"):
            alarm_cell = f"SHORTAGE ({v['first_short_slot_label']}, -{v['deficit_before_next_truck_pcs']:,.0f} EA)"
        elif v.get("buffer_low_15pct"):
            alarm_cell = "Buffer <15%"
        else:
            alarm_cell = "OK"

        rows.append(
            [
                v["part_no"],
                v["part_name"],
                f"{v['planned_boxes']:,.1f} ({v['planned_pcs']:,.0f} EA)",
                f"{v['received_boxes']:,d} ({v['received_pcs']:,.0f} EA)",
                f"{v['diff_pcs']:+,.0f} EA",
                v["verdict"],
                f"{float(v.get('open_shortfall_pcs') or 0.0):,.0f} EA",
                v.get("recovery_note") or "-",
                v["shortage_receipt_basis"],
                alarm_cell,
                v["runout_received_only"],
            ]
        )

    cleared_html = ""
    if cleared_earlier_alarms:
        c_rows = [
            [
                cv["truck_label"],
                cv["part_no"],
                cv["part_name"],
                cv.get("recovery_note") or "Recovered by subsequent receipt",
                cv["shortage_receipt_basis"],
            ]
            for cv in cleared_earlier_alarms
        ]
        cleared_html = (
            "<p style='color:#15803D;font-size:13px'><b>Earlier Shortage Alarms Cleared by Subsequent Receipts</b></p>"
            + _html_table(
                ["Earlier Truck", "Part #", "Part Name", "Recovery Detail", "Shortage (Receipt Basis)"],
                c_rows,
            )
        )

    wdate_str = str(truck.get("work_date") or "")
    if wdate_str < "2026-11-16":
        pre_live_banner = (
            "<p style='color:#64748B;font-size:12px'><i>Pre-live dry-run note: Data prior to 2026-11-16 is not a supplier "
            "baseline (suppliers had not received the dispatched truck plan yet).</i></p>"
        )
    elif wdate_str == "2026-11-16":
        pre_live_banner = (
            "<p style='color:#64748B;font-size:12px'><i>First-day note (2026-11-16): Suppliers received Monday's truck plan "
            "at 06:40 EST, after the 06:00 truck departed, so Monday's earliest truck(s) could not follow the 06:40 plan.</i></p>"
        )
    else:
        pre_live_banner = ""

    body = (
        f"<div style='{_CSS}'>"
        + _test_banner()
        + pre_live_banner
        + f"<p><b>{html.escape(_company_label())} Delivery Check - Truck Closed ({html.escape(status)})</b></p>"
        + _html_table(
            ["Item", "Value"],
            [
                ["Supplier", vendor],
                ["Truck Slot / Departure", f"{t_lbl} (departed {dep_short} EST)"],
                ["Truck Status / Closed At", f"{status} at {(truck.get('closed_ts') or '-')[:16]} EST"],
                ["Matched Planned Barcodes", f"{truck['matched_barcodes_count']} box(es), {truck['received_pcs_total']:,.0f} EA"],
            ],
        )
        + _html_table(
            [
                "Part #",
                "Part Name",
                "Planned",
                "Received",
                "GR-PO",
                "Verdict",
                "Open Shortfall",
                "Recovery / Note",
                "Shortage (Receipt Basis)",
                "Next-Truck Check",
                "Runout (received only)",
            ],
            rows,
        )
        + cleared_html
        + "<p style='color:#475569;font-size:12px'>Note: <b>Shortage (Receipt Basis)</b> uses actual GR for closed trucks "
        "and planned Truck load for open/future trucks. <b>Runout (received only)</b> assumes zero future deliveries and "
        "is shown for reference only.</p>"
        + f"<p>Thank you,<br>{html.escape(sig)}</p></div>"
    )
    assert_no_hangul({"delivery_close_subject": subject, "delivery_close_body": body})
    return {"subject": subject, "body": body}


def build_shortage_alarm_email(
    truck: Dict[str, Any],
    alarm_verdicts: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    *,
    missing_zm212_verdicts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, str]:
    vendor = truck["vendor_display"]
    t_lbl = truck["truck_label"]
    earliest = alarm_verdicts[0]["first_short_slot_label"] or "Before next truck"
    sig = (cfg.get("signature") or "").strip() or f"{_company_label()} Logistics"

    subject = (
        f"[TEST] [URGENT][Delivery Shortage] {vendor} truck {t_lbl} - "
        f"{len(alarm_verdicts)} part(s) run short before next truck ({earliest})"
    )
    rows = [
        [
            v["part_no"],
            v["part_name"],
            f"{v['planned_pcs']:,.0f} EA",
            f"{v['received_pcs']:,.0f} EA",
            f"{v['diff_pcs']:+,.0f} EA",
            v["verdict"],
            v["first_short_slot_label"] or v["shortage_receipt_basis"],
            f"-{v['deficit_before_next_truck_pcs']:,.0f} EA",
        ]
        for v in alarm_verdicts
    ]
    zm_note = ""
    if missing_zm212_verdicts:
        zm_rows = [
            [mv["part_no"], mv["part_name"], f"{mv['planned_pcs']:,.0f} EA", f"{mv['received_pcs']:,.0f} EA", MISSING_ZM212_INTERNAL_LABEL]
            for mv in missing_zm212_verdicts
        ]
        zm_note = (
            "<p style='color:#B45309;font-size:13px'><b>Excluded from supplier warning (missing morning ZM212 stock)</b></p>"
            + _html_table(["Part #", "Part Name", "Planned", "Received", "Guard Note"], zm_rows)
        )

    body = (
        f"<div style='{_CSS}'>"
        + _test_banner()
        + "<p style='color:#C00000;font-size:14px'><b>[URGENT] Line Shortage Projected Before Next Truck</b></p>"
        + f"<p>Truck <b>{html.escape(t_lbl)}</b> from <b>{html.escape(vendor)}</b> "
        + f"(departure {html.escape(truck['departure_ts'][:16])} EST) closed as "
        + f"<b>{html.escape(truck['status'])}</b> with an under-delivery that drives hybrid projected stock below zero "
        + "before the next scheduled truck carrying the affected part(s):</p>"
        + _html_table(
            [
                "Part #",
                "Part Name",
                "Planned",
                "Received",
                "GR-PO",
                "Verdict",
                "First Short Slot",
                "Max Deficit Before Next Truck",
            ],
            rows,
        )
        + zm_note
        + f"<p>Thank you,<br>{html.escape(sig)}</p></div>"
    )
    assert_no_hangul({"delivery_alarm_subject": subject, "delivery_alarm_body": body})
    return {"subject": subject, "body": body}


def build_early_warning_email(
    truck: Dict[str, Any],
    short_parts: List[Dict[str, Any]],
) -> Dict[str, str]:
    vendor = truck["vendor_display"]
    t_lbl = truck["truck_label"]
    earliest = short_parts[0]["first_short_slot_label"] or "Before next truck"
    subject = (
        f"[TEST] [Early Warning] {vendor} truck {t_lbl} - 0 WMS GR at T+90m "
        f"(line short at {earliest} if missed)"
    )
    rows = [
        [
            sp["part_no"],
            sp["part_name"],
            f"{sp['planned_pcs']:,.0f} EA",
            sp["first_short_slot_label"] or "-",
            f"-{sp['deficit_pcs']:,.0f} EA",
        ]
        for sp in short_parts
    ]
    body = (
        f"<div style='{_CSS}'>"
        + "<p style='background:#FCE4D6;padding:6px 10px;border:1px solid #C55A11;font-size:12px'>"
        + "<b>INTERNAL USER-ONLY EARLY WARNING (T + 90 min)</b> - Never sent to suppliers or planners.</p>"
        + f"<p>No WMS GR (Move.Type 101) has been posted yet 90+ minutes after scheduled departure for "
        + f"<b>{html.escape(vendor)}</b> truck <b>{html.escape(t_lbl)}</b> "
        + f"(departed {html.escape(truck['departure_ts'][:16])} EST). If this truck does not arrive, "
        + "hybrid stock runs below zero before the next truck on:</p>"
        + _html_table(
            ["Part #", "Part Name", "Planned on Truck", "Projected Short Slot", "Max Deficit"],
            rows,
        )
        + "</div>"
    )
    assert_no_hangul({"early_warning_subject": subject, "early_warning_body": body})
    return {"subject": subject, "body": body}


def build_sso_expired_email(work_date: str, detail: str) -> Dict[str, str]:
    is_login_block = "wms_login_blocked" in (detail or "").lower() or "login" in (detail or "").lower() and "sso_only=true" not in (detail or "").lower()
    if is_login_block:
        subject = f"[TEST] [Delivery Check] {work_date} WMS login paused - check .env and wms_login_blocked.json"
        lead = (
            "<p><b>WMS Login Paused After Single Attempt (Account Lockout Guard)</b></p>"
            "<p>The intraday WMS GR snapshot collector could not sign in on its single allowed attempt "
            "(or found an existing <code>logs/wms_login_blocked.json</code> guard file). No further login "
            "attempts will be made, zero credential values were logged, no trucks were closed, and open lines "
            "remain in <code>CANNOT_VERIFY</code> until you verify <code>.env</code> and delete "
            "<code>logs/wms_login_blocked.json</code>.</p>"
        )
    else:
        subject = f"[TEST] [Delivery Check] {work_date} WMS SSO expired - intraday GR poll paused"
        lead = (
            "<p><b>WMS Portal Login Form Detected (Zero Credentials Entered)</b></p>"
            "<p>The intraday WMS GR snapshot collector encountered a portal login screen while "
            "<code>sso_only=True</code>. Per security guardrails, zero credentials were entered, "
            "no trucks were closed, and all open lines remain in <code>CANNOT_VERIFY</code> until "
            "the PC SSO session is refreshed.</p>"
        )
    body = (
        f"<div style='{_CSS}'>"
        + _test_banner()
        + lead
        + _html_table(["Work Date", "Detail"], [[work_date, detail]])
        + "</div>"
    )
    assert_no_hangul({"sso_expired_subject": subject, "sso_expired_body": body})
    return {"subject": subject, "body": body}


def dispatch_poll_notifications(
    conn,
    *,
    work_date: str,
    as_of_ts: dt.datetime,
    cfg: Dict[str, Any],
    trucks: List[Dict[str, Any]],
    verdicts: List[Dict[str, Any]],
    newly_closed_truck_ids: List[str],
    early_warnings: List[Dict[str, Any]],
    workbook_path: Optional[Path] = None,
    sso_expired_detail: Optional[str] = None,
    send_enabled: bool = False,
    sender: Sender = outlook_send,
    horizon_parts: Optional[Dict[str, Dict[str, Any]]] = None,
    plan_lines: Optional[List[Dict[str, Any]]] = None,
    barcodes: Optional[List[Dict[str, Any]]] = None,
    projections: Optional[Dict[str, Dict[str, Any]]] = None,
    trucks_cfg: Optional[Dict[str, List[int]]] = None,
    suppliers_rows: Optional[List[Dict[str, Any]]] = None,
    supplier_status_out_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """
    Renders and dispatches due internal and supplier-status notices idempotently.
    Enforces `validate_supplier_mail_config(cfg)` up front (refuses unapproved `live` mode).
    """
    sm_mode = validate_supplier_mail_config(cfg)
    load_env()
    now_str = as_of_ts.strftime("%Y-%m-%d %H:%M:%S")
    to_addrs = addresses(cfg.get("user_env", "TEST_EMAIL_RECIPIENT"))
    attachments = [Path(workbook_path)] if (workbook_path and Path(workbook_path).exists()) else []
    dispatched: List[Dict[str, Any]] = []

    def _emit(
        notice_key: str,
        notice_type: str,
        truck_id: Optional[str],
        msg: Dict[str, str],
        attach: List[Path],
        *,
        to_override: Optional[List[str]] = None,
        cc_override: Optional[List[str]] = None,
        rec_mode: str = "test_user_only",
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        if has_notice(conn, notice_key):
            return
        eff_to = to_addrs if to_override is None else to_override
        eff_cc = [] if cc_override is None else cc_override
        meta = extra_meta or {}
        if not send_enabled:
            dispatched.append(
                {
                    "notice_key": notice_key,
                    "notice_type": notice_type,
                    "truck_id": truck_id,
                    "sent": False,
                    "dry_run": True,
                    "subject": msg["subject"],
                    "attachments": [Path(a).name for a in attach],
                    **meta,
                }
            )
            return
        if not eff_to:
            dispatched.append(
                {
                    "notice_key": notice_key,
                    "notice_type": notice_type,
                    "truck_id": truck_id,
                    "sent": False,
                    "reason": "no recipient address in .env",
                    "subject": msg["subject"],
                    **meta,
                }
            )
            return
        if rec_mode == "live_supplier":
            assert_clean(msg["subject"], msg["body"])
        ok, err = _safe_send(sender, eff_to, eff_cc, msg["subject"], msg["body"], attach)
        record_notice(
            conn,
            notice_key=notice_key,
            work_date=work_date,
            notice_type=notice_type,
            truck_id=truck_id,
            recipient_mode=rec_mode,
            subject=msg["subject"],
            sent_ok=ok,
            detail=err or "sent",
            created_at=now_str,
        )
        conn.commit()
        dispatched.append(
            {
                "notice_key": notice_key,
                "notice_type": notice_type,
                "truck_id": truck_id,
                "sent": ok,
                "subject": msg["subject"],
                "attachments": [Path(a).name for a in attach],
                **meta,
                **({"error": err} if err else {}),
            }
        )

    if sso_expired_detail:
        nk = f"{work_date}__WMS_SSO_EXPIRED"
        _emit(nk, "WMS_SSO_EXPIRED", None, build_sso_expired_email(work_date, sso_expired_detail), [])

    for ew in early_warnings:
        t = ew["truck"]
        nk = f"{t['truck_id']}__EARLY_WARNING_90M"
        _emit(nk, "EARLY_WARNING_90M", t["truck_id"], build_early_warning_email(t, ew["short_parts"]), [])

    truck_map = {t["truck_id"]: t for t in trucks}
    verdicts_by_truck: Dict[str, List[Dict[str, Any]]] = {}
    for v in verdicts:
        verdicts_by_truck.setdefault(v["truck_id"], []).append(v)

    closed_c_by_supplier: Dict[str, List[Dict[str, Any]]] = {}
    for tid in newly_closed_truck_ids:
        t = truck_map.get(tid)
        if not t:
            continue
        if not t.get("is_pallet_truck"):
            closed_c_by_supplier.setdefault(t["supplier_key"], []).append(t)

        t_verdicts = verdicts_by_truck.get(tid, [])
        sup_cleared = [
            v
            for v in verdicts
            if v["supplier_key"] == t["supplier_key"]
            and v["truck_id"] != tid
            and v.get("initial_alarm_at_close")
            and not v.get("line_shortage_alarm")
        ]
        nk_sum = f"{tid}__TRUCK_CLOSE_SUMMARY"
        _emit(
            nk_sum,
            "TRUCK_CLOSE_SUMMARY",
            tid,
            build_truck_close_email(t, t_verdicts, cfg, cleared_earlier_alarms=sup_cleared),
            attachments,
        )

        real_alarms = [v for v in t_verdicts if v.get("line_shortage_alarm") and int(v.get("start_in_zm212", 1))]
        zm_missing_alarms = [v for v in t_verdicts if v.get("line_shortage_alarm") and not int(v.get("start_in_zm212", 1))]
        if real_alarms:
            nk_alm = f"{tid}__LINE_SHORTAGE_ALARM"
            _emit(
                nk_alm,
                "LINE_SHORTAGE_ALARM",
                tid,
                build_shortage_alarm_email(t, real_alarms, cfg, missing_zm212_verdicts=zm_missing_alarms),
                attachments,
            )

    # #387: Supplier-facing SRM_DeliveryStatus file & mail (C-truck close only, only when >=1 valid Warning exists)
    if (
        closed_c_by_supplier
        and horizon_parts is not None
        and plan_lines is not None
        and barcodes is not None
        and projections is not None
        and trucks_cfg is not None
    ):
        run_dt = dt.date.fromisoformat(work_date)
        sup_dir = Path(supplier_status_out_dir) if supplier_status_out_dir else (BASE_DIR / "out" / f"suppliers_{run_dt:%Y%m%d}")
        for sup_key, closed_t_list in sorted(closed_c_by_supplier.items()):
            model = build_supplier_delivery_status(
                run_dt,
                as_of_ts,
                sup_key,
                horizon_parts,
                trucks,
                plan_lines,
                barcodes,
                projections,
                trucks_cfg,
            )
            if not model or not model["warnings"]:
                continue
            status_xlsx = write_supplier_delivery_status_file(sup_dir, model)
            s_to, s_cc, s_tag, s_banner = resolve_supplier_status_recipients(
                model["vendor_display"], cfg, suppliers_rows
            )
            trigger_tid = closed_t_list[-1]["truck_id"]
            closed_lbls = [ct["truck_label"] for ct in closed_t_list]
            msg_sup = build_supplier_delivery_status_email(
                model,
                closed_lbls,
                cfg,
                subject_prefix=s_tag,
                banner=s_banner,
            )
            nk_sup = f"{trigger_tid}__SUPPLIER_DELIVERY_STATUS"
            _emit(
                nk_sup,
                "SUPPLIER_DELIVERY_STATUS",
                trigger_tid,
                msg_sup,
                [status_xlsx],
                to_override=s_to,
                cc_override=s_cc,
                rec_mode="live_supplier" if sm_mode == "live" else "test_user_only",
                extra_meta={
                    "supplier_key": sup_key,
                    "supplier_file": str(status_xlsx),
                    "warnings": [
                        {"part_no": w["part_no"], "part_name": w["part_name"], "warning": w["warning"]}
                        for w in model["warnings"]
                    ],
                },
            )

    return dispatched
