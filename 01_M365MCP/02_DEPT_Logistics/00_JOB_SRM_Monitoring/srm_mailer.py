"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - mails (Outlook COM; user decisions 11/12)
====================================================================================
config/srm_mail.json
  mode 'test' (default): every mail goes only to the user (TEST_EMAIL_RECIPIENT), subject starts with [TEST],
                         supplier previews name the .env keys a live mail would use (key names only, never values).
  mode 'live'          : each supplier mail goes to SRM_SUPPLIER_TO_<NAME> (CC SRM_SUPPLIER_CC_<NAME> + the user).
  suppliers            : data/suppliers.csv rows with mail = yes (+ their .env key names; not in git); every other
                         supplier file is only attached to the internal mail.
Internal mail (always user only): run summary + still-short parts + the monitoring report and supplier files.
Supplier mail: short English note (loading changes / additional quantities) + that supplier's own file only.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from srm_parsers import N_SLOTS, SLOT_LABELS
from srm_plan import window
from srm_env import assert_clean, company_name, load_env  # noqa: F401 (load_env re-exported)
from srm_supplier_files import supplier_file_prefix
from srm_trucks import load_suppliers

BASE_DIR = Path(__file__).resolve().parent
CONFIG = BASE_DIR / "config" / "srm_mail.json"
Sender = Callable[[List[str], List[str], str, str, List[Path]], bool]


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or CONFIG
    cfg = json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}
    cfg.setdefault("mode", "test")
    cfg.setdefault("user_env", "TEST_EMAIL_RECIPIENT")
    cfg.setdefault("send_when_no_change", True)
    if "suppliers" not in cfg:          # names and key names live in data/suppliers.csv (not in git)
        cfg["suppliers"] = {r["match"]: {"to_env": r["to_env"], "cc_env": r["cc_env"]}
                            for r in load_suppliers() if r["mail"]}
    if cfg["mode"] not in ("test", "live"):
        cfg["mode"] = "test"   # anything unexpected -> the safe mode
    return cfg


def addresses(env_key: str) -> List[str]:
    return [x.strip() for x in os.getenv(env_key, "").replace(";", ",").split(",") if "@" in x]


def supplier_key(vendor: str, cfg: Dict[str, Any]) -> Optional[str]:
    v = (vendor or "").lower()
    return next((k for k in cfg.get("suppliers", {}) if k.lower() in v), None)


def recipients(vendor: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str], str]:
    """(to, cc, banner). test: the user only + a banner naming the live .env keys. live: supplier, user in CC."""
    user = addresses(cfg["user_env"])
    keys = cfg["suppliers"].get(supplier_key(vendor, cfg) or "", {})
    if cfg["mode"] != "live":
        return user, [], (f"TEST MODE - in live mode this mail goes to .env {keys.get('to_env', '?')} "
                          f"(CC {keys.get('cc_env', '?')} and you). Nothing was sent to the supplier.")
    to = addresses(keys["to_env"]) if keys.get("to_env") else []
    cc = (addresses(keys["cc_env"]) if keys.get("cc_env") else []) + user
    return to, [c for c in dict.fromkeys(cc) if c not in to], ""


# ---------------------------------------------------------------- bodies (English only)
_CSS = "font-family:Calibri,Arial,sans-serif;font-size:13px;color:#222"
_TH = "background:#1F3864;color:#fff;padding:4px 8px;font-size:12px;text-align:left"
_TD = "border-bottom:1px solid #ddd;padding:3px 8px;font-size:12px"


def _table(head: List[str], rows: List[List[Any]]) -> str:
    h = "".join(f"<th style='{_TH}'>{html.escape(x)}</th>" for x in head)
    b = "".join("<tr>" + "".join(f"<td style='{_TD}'>{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table style='border-collapse:collapse;margin:6px 0 12px'><tr>{h}</tr>{b}</table>"


def _day(d: dt.date) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def supplier_summary(items: List[Tuple[Dict[str, Any], Dict[str, Any]]]) -> Dict[str, Any]:
    """One row per truck whose quantity differs from the SRM order (moved between trucks and / or additional
    quantity not in SRM), sorted by day / truck / part. Once-a-day items: one 'Pallet Truck' row per day."""
    rows, add = [], False
    for r, w in items:
        add = add or any(a > 1e-9 for a in w["add"])
        pallet = w["cls"] == "L" or not w["trucks"]
        for d, day in enumerate(w["dates"]):
            a, b = d * N_SLOTS, (d + 1) * N_SLOTS
            if pallet:
                extra = sum(w["extra"][a:b])
                if extra > 1e-9:
                    srm = sum(w["srm"][a:b]) + w["add"][d]
                    rows.append(((a, r["part"]), [r["part"], r["name"], _day(day), "Pallet Truck", f"{srm:,.0f}",
                                                  f"{srm + extra:,.0f}", f"+{extra:,.0f}"]))
                continue
            for i in range(a, b):
                load, srm, extra = w["load"][i], w["srm"][i], w["extra"][i]
                if abs(load - srm) > 1e-9 or extra > 1e-9:
                    rows.append(((i, r["part"]), [r["part"], r["name"], _day(day), SLOT_LABELS[i - a],
                                                  f"{srm:,.0f}", f"{load:,.0f}",
                                                  f"+{extra:,.0f}" if extra > 1e-9 else ""]))
    rows.sort()
    return {"rows": [x for _, x in rows], "extra": any(x[6] for _, x in rows), "add": add}


def supplier_body(vendor: str, run_label: str, items: List[Tuple[Dict[str, Any], Dict[str, Any]]],
                  issued: dt.datetime, banner: str, signature: Optional[str] = None,
                  updated: bool = False, refresh_meta: Optional[Dict[str, Any]] = None) -> str:
    what = "today's final orders including urgent orders" if run_label == "AM" else "orders from tomorrow"
    company = company_name()
    sig = (signature or "").strip() or f"{company} Logistics"
    s = supplier_summary(items)
    parts = [f"<div style='{_CSS}'>"]
    if banner:
        parts.append(f"<p style='background:#FFF2CC;padding:6px 10px;border:1px solid #BF8F00'><b>"
                     f"{html.escape(banner)}</b></p>")
    if refresh_meta:
        ref_at = str(refresh_meta.get("refresh_at") or f"{issued:%H:%M}")
        prev_at = str(refresh_meta.get("prev_sent") or "13:40")
        parts.append(
            f"<p style='background:#FFF2CC;padding:6px 10px;border-left:4px solid #D6B656'><b>"
            f"Updated after an SRM order change at {html.escape(ref_at)}. "
            f"This plan replaces the one sent at {html.escape(prev_at)}.</b></p>"
        )
    upd_note = (
        " <b>(UPDATED — replaces the earlier update for these dates; please use this updated plan.)</b>"
        if (updated and not refresh_meta) else ""
    )
    parts += [f"<p>Hello {html.escape(vendor)} team,</p>",
              f"<p>Attached is the {html.escape(company)} truck loading plan ({html.escape(what)}), issued "
              f"{issued:%a %m/%d %H:%M} EST.{upd_note} Please load each truck as shown on the <b>Truck plan</b> sheet.</p>"]
    if refresh_meta and refresh_meta.get("diffs"):
        prev_at = str(refresh_meta.get("prev_sent") or "13:40")
        parts.append(
            f"<p><b>Changes since the {html.escape(prev_at)} plan</b> "
            f"(highlighted in yellow on the <b>Truck plan</b> sheet):</p>"
        )
        parts.append(_table(
            ["Part #", "Part name", "Day", "Truck", f"{prev_at} qty", "Updated qty"],
            [[d["part"], d["name"], d["day"], d["truck"], f"{d['old_qty']:,.0f}", f"{d['new_qty']:,.0f}"]
             for d in refresh_meta["diffs"]],
        ))
    if s["rows"]:
        parts.append("<p><b>Trucks that differ from the SRM order</b>: quantity balanced across the day's trucks so "
                     "no truck is overloaded and the line does not run short between trucks (yellow cells)"
                     + ("; the urgent ADD quantity of the day is included in the trucks" if s["add"] else "")
                     + ". The day total changes only by the additional quantity.</p>")
        parts.append(_table(["Part #", "Part name", "Day", "Truck", "SRM qty", "Load qty", "Additional"], s["rows"]))
        if s["extra"]:
            parts.append("<p><b>Additional = needed to avoid a line shortage, not yet in SRM</b> (pink cells). "
                         f"{html.escape(company)} will confirm these quantities.</p>")
    else:
        parts.append("<p>No changes to the SRM orders in this update.</p>")
    parts.append(f"<p>Thank you,<br>{html.escape(sig)}</p></div>")
    return "".join(parts)


def internal_body(status: Dict[str, Any], mailed: List[str]) -> str:
    when = ("AM 06:40-07:00 (delivery date today)" if status.get("run_label") == "AM"
            else "PM 13:40-14:00 (delivery date tomorrow)")
    rows = [["Run", f"{status.get('run_date')} {when}"],
            ["Items on the 2-hour view / daily view", f"{status.get('parts_2h')} / {status.get('parts_daily')}"],
            ["2-hour shortages with SRM as is", status.get("short_2h_planned")],
            ["Daily view shortages", status.get("short_daily")],
            ["Truck loading changed (suppliers with truck times)", status.get("loading_changed_parts")],
            ["Truck leveling (ADD / >1.7x avg)", status.get("leveling_summary_text", "none")],
            ["Additional quantity not in SRM", status.get("additional_qty_parts")],
            ["Still short after loading (act now)", status.get("still_short_parts")],
            ["Short on later days - recheck after 13:40", status.get("recheck_parts")],
            ["Supplier mails", ", ".join(mailed) or "none"]]
    out = [f"<div style='{_CSS}'><p><b>SRM Monitoring (#330)</b></p>", _table(["Item", "Value"], rows)]
    short = status.get("still_short") or []
    if short:
        out.append("<p><b>Still short after loading</b> - call the supplier / extra truck:</p>")
        out.append(_table(["Part #", "Part name", "Supplier", "First short", "Reason"],
                          [[x["part"], x["name"], x["vendor"], x["when"], x["reason"]] for x in short]))
    lvl_obj = status.get("leveling") or {}
    mv100 = status.get("moves_100pct") or lvl_obj.get("moves_100pct") or []
    if mv100:
        out.append("<p><b>Cap fallback moves at 100% margin (Inv &ge; Req, no shortage)</b>:</p>")
        out.append(_table(
            ["Part #", "Part name", "Supplier", "Day", "Move", "Carts (EA)", "Margin"],
            [[
                m["part"], m["name"], m.get("supplier", ""), m.get("day_label") or m.get("date", ""),
                f"{m['from_truck']} -> {m['to_truck']}", f"{m['carts']} ({m['qty']:,.0f} EA)",
                f"{m.get('margin_used', 1.0):.0%}",
            ] for m in mv100],
        ))
    cap_warns = status.get("cap_warnings") or lvl_obj.get("warnings") or []
    if cap_warns:
        out.append("<p><b>Truck cap warnings (extra truck review)</b>:</p><ul>"
                   + "".join(f"<li><b>{html.escape(str(w))}</b></li>" for w in cap_warns) + "</ul>")
    info_n = lvl_obj.get("info_notes") or []
    if info_n:
        out.append("<p>Departed truck info (untouched):</p><ul>"
                   + "".join(f"<li>{html.escape(str(inf))}</li>" for inf in info_n) + "</ul>")
    ref_obj = status.get("refresh") or {}
    if ref_obj:
        out.append(f"<p><b>PM Refresh comparison (refresh at {html.escape(str(ref_obj.get('refresh_at', '')))})</b>:</p>")
        sup_rows_tbl = []
        for s_item in ref_obj.get("suppliers", []):
            sup_rows_tbl.append([
                s_item["vendor"],
                s_item["prev_sent"],
                str(s_item["changed_cells"]),
                "UPDATED sent" if s_item.get("sent") else (
                    "CHANGED (not sent - dry-run/filtered)" if s_item["changed_cells"] > 0
                    else "Nothing sent (unchanged)"
                ),
            ])
        if sup_rows_tbl:
            out.append(_table(["Supplier", "Compared with", "Changed cells", "Action"], sup_rows_tbl))
        for s_item in ref_obj.get("suppliers", []):
            if s_item.get("diffs"):
                out.append(f"<p><b>{html.escape(s_item['vendor'])} — changed cells ({len(s_item['diffs'])})</b>:</p>")
                out.append(_table(
                    ["Part # — Part name", "Day", "Truck", "Old -> New"],
                    [[f"{d['part']} — {d['name']}", d["day"], d["truck"],
                      f"{d['old_qty']:,.0f} -> {d['new_qty']:,.0f}"] for d in s_item["diffs"]],
                ))
    notes = status.get("notes") or []
    if notes:
        out.append("<p>Notes:</p><ul>" + "".join(f"<li>{html.escape(str(n))}</li>" for n in notes) + "</ul>")
    out.append("<p>Attached: monitoring report and supplier files.</p></div>")
    return "".join(out)


def alert_body(stage: str, detail: str, status: Dict[str, Any]) -> str:
    rows = [["Stage", stage], ["Detail", detail], ["Run", f"{status.get('run_date')} {status.get('slot', '')}"]]
    return (f"<div style='{_CSS}'><p><b>SRM Monitoring (#330) did not finish.</b> No supplier mail was sent.</p>"
            + _table(["Item", "Value"], rows)
            + "<p>Manual fallback: save the SRM exports into the job inbox folder and run "
              "run_srm_pipeline.py with --skip-download (see README).</p></div>")


# ---------------------------------------------------------------- Outlook
def outlook_send(to: List[str], cc: List[str], subject: str, html_body: str, attachments: List[Path]) -> bool:
    if not to:
        return False
    import win32com.client  # Windows / Outlook only

    try:
        mail = win32com.client.Dispatch("Outlook.Application").CreateItem(0)
    except AttributeError:
        mail = win32com.client.gencache.EnsureDispatch("Outlook.Application").CreateItem(0)
    mail.To = "; ".join(to)
    if cc:
        mail.CC = "; ".join(cc)
    mail.Subject = subject
    mail.HTMLBody = html_body
    for a in attachments:
        mail.Attachments.Add(str(Path(a).resolve()))
    mail.Send()
    return True


MAIL_TIMEOUT_S = 120     # Outlook that cannot reach Exchange (e.g. a Microsoft 365 outage) must not hang the run


def _safe_send(sender: Sender, *args: Any, timeout_s: Optional[float] = None) -> Tuple[bool, str]:
    """Runs the send in a worker thread with a time limit; errors and time-outs come back as text, never raised."""
    box: Dict[str, Any] = {}

    def work() -> None:
        try:
            if sender is outlook_send:
                try:
                    import pythoncom
                    pythoncom.CoInitialize()
                except Exception:
                    pass
            box["ok"] = bool(sender(*args))
        except Exception as exc:
            box["err"] = f"{type(exc).__name__}: {exc}"

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(MAIL_TIMEOUT_S if timeout_s is None else timeout_s)
    if t.is_alive():
        return False, f"TIMEOUT: Outlook did not answer within {MAIL_TIMEOUT_S if timeout_s is None else timeout_s:.0f}s"
    return bool(box.get("ok")), box.get("err", "")


def send_alert(stage: str, detail: str, status: Dict[str, Any], cfg: Optional[Dict[str, Any]] = None,
               sender: Sender = outlook_send) -> bool:
    """Failure notice to the user only."""
    load_env()
    cfg = cfg or load_config()
    subject = f"[SRM Monitoring] {status.get('run_date')} {status.get('slot', '')} FAILED - {stage}"
    ok, _ = _safe_send(sender, addresses(cfg["user_env"]), [], subject, alert_body(stage, detail, status), [])
    return ok


def send_run_mails(status: Dict[str, Any], res6: List[Dict[str, Any]], issued: dt.datetime,
                   cfg: Optional[Dict[str, Any]] = None, sender: Sender = outlook_send,
                   suppliers: bool = True, only_supplier: Optional[str] = None,
                   updated: bool = False, refresh_info: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Supplier mails (configured suppliers that have a file), then the internal mail. One result per mail, without
    addresses."""
    load_env()
    cfg = cfg or load_config()
    tag = "[TEST] " if cfg["mode"] != "live" else ""
    label = status.get("run_label", "AM")
    update = "morning update" if label == "AM" else "afternoon update"
    files = [Path(x) for x in status.get("supplier_files", [])]
    first_day = 1 if label == "PM" else 0
    results: List[Dict[str, Any]] = []
    by_vendor: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
    for r in res6:
        if r.get("plan") and r.get("vendor") and supplier_key(r["vendor"], cfg):
            by_vendor.setdefault(r["vendor"], []).append((r, window(r, first_day)))
    mailed: List[str] = []
    only_low = (only_supplier or "").strip().lower()
    for vendor, items in (sorted(by_vendor.items()) if suppliers else []):
        if only_low and only_low not in vendor.lower() and only_low not in (supplier_key(vendor, cfg) or "").lower():
            continue
        att = [f for f in files if f.name.startswith(supplier_file_prefix(vendor))]
        if not att:
            results.append({"mail": vendor, "sent": False, "reason": "no supplier file (no active parts)"})
            continue
        ref_meta = None
        is_upd = updated
        if refresh_info is not None:
            v_diffs = (refresh_info.get("diffs") or {}).get(vendor, [])
            prev_at = (refresh_info.get("prev_sent") or {}).get(vendor, "13:40")
            if not v_diffs:
                results.append({
                    "mail": vendor,
                    "sent": False,
                    "reason": f"unchanged vs {prev_at} plan (nothing sent)",
                    "changed_cells": 0,
                })
                continue
            is_upd = True
            ref_meta = {
                "refresh_at": refresh_info.get("refresh_at") or f"{issued:%H:%M}",
                "prev_sent": prev_at,
                "diffs": v_diffs,
            }
        elif not cfg["send_when_no_change"] and not supplier_summary(items)["rows"]:
            results.append({"mail": vendor, "sent": False, "reason": "no change (send_when_no_change=false)"})
            continue
        to, cc, banner = recipients(vendor, cfg)
        if not to:
            results.append({"mail": vendor, "sent": False, "reason": "no address in .env"})
            continue
        upd_prefix = "UPDATED - " if is_upd else ""
        subject = (f"{tag}{upd_prefix}{company_name()} truck loading plan - {vendor} - {_day(items[0][1]['dates'][0])} "
                   f"({update})")
        body = supplier_body(vendor, label, items, issued, banner, signature=cfg.get("signature"),
                             updated=is_upd, refresh_meta=ref_meta)
        if cfg["mode"] == "live":
            try:
                assert_clean(subject, body)
            except RuntimeError as exc:
                results.append({"mail": vendor, "sent": False, "error": str(exc)})
                continue
        ok, err = _safe_send(sender, to, cc, subject, body, att)
        results.append({"mail": vendor, "sent": ok, "mode": cfg["mode"], "attachments": [a.name for a in att],
                        **({"error": err} if err else {})})
        if ok:
            mailed.append(vendor)
        if err.startswith("TIMEOUT"):
            results.append({"mail": "internal", "sent": False, "error": "skipped: Outlook not answering"})
            return results
    if refresh_info is not None:
        subject = (f"{tag}[SRM Monitoring] {status.get('run_date')} PM refresh "
                   f"({refresh_info.get('refresh_at', f'{issued:%H:%M}')}) - {len(mailed)} supplier(s) updated")
    else:
        subject = f"{tag}[SRM Monitoring] {status.get('run_date')} {label} update"
    if status.get("still_short_parts"):
        subject += f" - {status['still_short_parts']} part(s) still short"
    report = [Path(status["report"])] if status.get("report") else []
    if status.get("refresh") and isinstance(status["refresh"].get("suppliers"), list):
        mailed_set = set(mailed)
        for s_rec in status["refresh"]["suppliers"]:
            s_rec["sent"] = s_rec.get("vendor") in mailed_set
    ok, err = _safe_send(sender, addresses(cfg["user_env"]), [], subject, internal_body(status, mailed),
                         report + files)
    results.append({"mail": "internal", "sent": ok, **({"error": err} if err else {})})
    return results
