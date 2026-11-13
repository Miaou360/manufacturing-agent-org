"""
D107 Phase 2 (Step 2a Dry-Run & Step 2b Save-Temp) — Portal GI Error Deletion Draft Automation
==============================================================================================
01_DEPT_Inventory / 01_JOB_GR_GI_Error (#222 · #224 · #229 · #230 · #233 · D96 · D107 · D111)

Strict Guardrails:
  1. Mode Gate (`ALLOWED_MODES == {"dry_run_2a", "save_temp_2b"}`):
     - `dry_run_2a`: Opens & verifies `WF_FORM_ST_78` 7-field match, clicks neither `Save temp.` nor `Submit`.
     - `save_temp_2b` (#233): Fills English Subject (`Request to delete IWMS_GI of YYYY/MM/DD`) & English
       `Reason for deletion`, clicks `Save temp.` (`#btSave`) ONLY, records `(SUMNO, BARCODE, PART, MVT, QTY)`
       in SQLite `d107_drafts` to prevent duplicate temp-saves across days, and reconciles user-submitted rows.
     - `Submit` (`#btDraft` / `기안`) is HARD-LOCKED (`PermissionError` + browser JS override) in ALL modes!
  2. Schedule & Cutoff (`#224` 15:55 User Decision — `07:23` Confirmed):
     - Verifies today's `d107_phase1_YYYYMMDD.json` (from `07:18` collect) exists before
       opening the portal (`require_today_phase1=True`). If missing, skips & notifies.
     - Enforces a hard `07:29` batch cutoff before `07:30` `01_JOB`: any remaining draft
       bundles are halted and reported (naturally re-evaluated in the next day's pool).
  3. SSO Session & Account Lockout Protection (D96 & `#224` 15:44 / 16:15):
     - Reads `CORP_PORTAL_URL`, `WMS_PORTAL_GI_LIST_URL`, `WMS_USERNAME`, `WMS_PASSWORD` from `.env`.
     - Zero hardcoded URLs, hostnames, IDs, employee names, or passwords anywhere in code or logs.
     - Authenticates via `CORP_PORTAL_URL` (`openMain.do` SSO) -> clicks `전자결재` app to bridge
       SSO cookies onto the Groupware host (zero password input under active PC SSO), then opens
       `WMS_PORTAL_GI_LIST_URL` in a second tab of `.pw_d107_profile` -> opens `WF_FORM_ST_78`
       (`Form.aspx` — Subject `#SUBJECT`, Reason `#JFORM_BUSINESS_CONTENTS_ETC`).
     - Never logs into `/WebSite/Login.aspx` directly; if `CORP_PORTAL_URL` ever prompts for login,
       halts unless `allow_gw_login_once=True`, and trips `.pw_d107_gw_login_halted.flag` on 1st failure.
  4. Row Scope (`#222` 14:24 Override):
     - Bundles ALL portal rows where `POST DATE <= yesterday` AND verified as `CANDIDATE`
       in D107 Phase 1 AND still unresolved in latest `01_JOB` ZTCODE_STOCK_REC GI snapshot.
     - Splits into chunks of <= 20 rows (portal native JS limit) and logs draft count.
  5. Language & Test Mail (Rule 10 & D111):
     - Builds 1-line-per-row English `Reason for deletion` (overflow -> 1-line summary + Excel).
     - Enforces `assert_no_hangul` (with D111 B-3 raw SAP `Description` exemption).
     - Sends `[TEST]` report strictly to `TEST_EMAIL_RECIPIENT` (`CC=[]`, `[TEST]` prefix).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

import pandas as pd
from dotenv import load_dotenv

JOB_DIR = Path(__file__).resolve().parent
DEPT_DIR = JOB_DIR.parent
HQ_DIR = DEPT_DIR.parent / "00_HQ_TaskManager"
for _p in (JOB_DIR, DEPT_DIR, HQ_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

_ENV_LOADED = False
for _ep in (*Path(__file__).resolve().parents, Path.cwd(), Path.cwd() / "01_M365MCP"):
    if (_ep / ".env").exists():
        load_dotenv(_ep / ".env")
        _ENV_LOADED = True
        break
if not _ENV_LOADED:
    try:
        _gdir = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"], cwd=str(JOB_DIR), text=True, stderr=subprocess.DEVNULL
        ).strip()
        _main_root = Path(_gdir).resolve().parent
        if (_main_root / "01_M365MCP" / ".env").exists():
            load_dotenv(_main_root / "01_M365MCP" / ".env")
    except Exception:
        pass

from gi_verdict_shared import (  # noqa: E402
    D107_TARGET_MVTS,
    parse_ztcode_stock_rec_gi_records_detailed,
)
from report_labels import assert_no_hangul, check_report_language  # noqa: E402

logger = logging.getLogger("D107PortalDrafter")

REQUIRED_PORTAL_HEADERS: Tuple[str, ...] = (
    "PLANT",
    "POST DATE",
    "GUBUN",
    "SUMNO",
    "BARCODE",
    "PART NUMBER",
    "PART DESC",
    "MVT",
    "FROM LOC",
    "TO LOC",
    "QTY",
)

PORTAL_MAX_ROWS_PER_DRAFT: int = 20
DEFAULT_REASON_MAX_CHARS: int = 900
DEFAULT_MORNING_CUTOFF_HHMM: str = "07:29"
GW_LOGIN_HALT_FLAG_NAME: str = ".pw_d107_gw_login_halted.flag"
import importlib.util as _ilu_d107_db
_spec_d107_db = _ilu_d107_db.spec_from_file_location("_job01_d107_db_manager", JOB_DIR / "db_manager.py")
db_manager = _ilu_d107_db.module_from_spec(_spec_d107_db)
_spec_d107_db.loader.exec_module(db_manager)

WF_FORM_SUBJECT_SELECTOR: str = "#SUBJECT"
WF_FORM_REASON_SELECTOR: str = "#JFORM_BUSINESS_CONTENTS_ETC"
WF_FORM_SAVE_TEMP_SELECTOR: str = "#btSave"
FORBIDDEN_SUBMIT_SELECTORS: Tuple[str, ...] = (
    "#btDraft",
    "#btApproved",
    "#btRec",
    "#btDeptDraft",
    "#btAction",
)
FORBIDDEN_SUBMIT_TOKENS: Tuple[str, ...] = (
    "btdraft",
    "btapproved",
    "btrec",
    "btdeptdraft",
    "btaction",
    "submit",
    "submit_2c",
    "\uAE30\uC548",  # 기안
    "\uC0C1\uC2E0",  # 상신
    'requestprocess("draft")',
    "requestprocess('draft')",
)
ALLOWED_MODES = frozenset({"dry_run_2a", "save_temp_2b"})


def assert_not_submit_action(action_or_selector: Any) -> None:
    """
    #233 Hard Lock: Unconditionally blocks ANY Submit/Draft/Approve selector or action in ALL modes
    (including `save_temp_2b`) until 2-3 business days of 2b verification + Claude review + user final OK.
    """
    norm = re.sub(r"\s+", "", str(action_or_selector or "")).lower()
    if norm in ("draft", "#draft"):
        raise PermissionError(
            f"[D107-2b] Hard lock violation: Submit/Draft action '{action_or_selector}' is strictly forbidden."
        )
    for tok in FORBIDDEN_SUBMIT_TOKENS:
        if tok in norm:
            raise PermissionError(
                f"[D107-2b] Hard lock violation: Submit/Draft selector '{action_or_selector}' is strictly forbidden."
            )


def click_portal_form_action(form_frame: Any, selector: str, mode: str) -> None:
    """
    Single gated entry point for clicking any toolbar button on `WF_FORM_ST_78` (`Form.aspx`).
    - Raises `PermissionError` on ANY Submit/Draft selector even when `mode == 'save_temp_2b'`.
    - Raises `PermissionError` on ANY toolbar button when `mode == 'dry_run_2a'`.
    - Permits ONLY `#btSave` (`Save temp.`) when `mode == 'save_temp_2b'`.
    """
    assert_not_submit_action(selector)
    if mode not in ALLOWED_MODES:
        raise PermissionError(f"[D107] Unauthorized mode '{mode}'.")
    if mode != "save_temp_2b":
        raise PermissionError(f"[D107] Toolbar action '{selector}' is hard-disabled in mode='{mode}'.")
    if str(selector or "").strip() != WF_FORM_SAVE_TEMP_SELECTOR:
        raise PermissionError(
            f"[D107-2b] Only '{WF_FORM_SAVE_TEMP_SELECTOR}' (Save temp.) is permitted in save_temp_2b; got '{selector}'."
        )
    btn_loc = form_frame.locator(WF_FORM_SAVE_TEMP_SELECTOR).first
    if btn_loc.count() == 0:
        raise PortalSchemaMismatchError(
            f"[D107-2b] Save temp. button ({WF_FORM_SAVE_TEMP_SELECTOR}) not found on Form.aspx."
        )
    btn_loc.click(timeout=10000, no_wait_after=True)


def format_draft_subject_en(report_date_str: str, batch_idx: int = 1, total_batches: int = 1) -> str:
    """Formats standardized English Groupware Temp Box draft title (`Request to delete IWMS_GI of YYYY/MM/DD`)."""
    ymd_slash = str(report_date_str).replace("-", "/")
    base = f"Request to delete IWMS_GI of {ymd_slash}"
    if int(total_batches or 1) > 1:
        return f"{base} ({batch_idx}/{total_batches})"
    return base


class PortalSchemaMismatchError(RuntimeError):
    """Raised when portal table headers or approval form columns deviate from expected contract."""


class PortalFormRowMismatchError(RuntimeError):
    """Raised when WF_FORM_ST_78 form rows do not 100% match selected portal rows."""


def redact_portal_sensitive(text: Any) -> str:
    """Scrubs URLs, hostnames, credentials, and email addresses from any string (D96)."""
    if text is None:
        return ""
    out = str(text)
    for env_key in (
        "CORP_PORTAL_URL",
        "WMS_PORTAL_GI_LIST_URL",
        "WMS_BASE_URL",
        "WMS_PASSWORD",
        "WMS_USERNAME",
        "GROUPWARE_USERNAME",
        "SAP_USERNAME",
    ):
        secret = os.getenv(env_key, "").strip()
        if secret:
            out = out.replace(secret, f"<REDACTED:{env_key}>")
            if env_key.endswith("_URL"):
                sp = urlsplit(secret)
                if sp.netloc:
                    out = out.replace(sp.netloc, f"<REDACTED_HOST:{env_key}>")
    out = re.sub(r"https?://[^\s\"'<>]+", "<REDACTED_URL>", out)
    out = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<REDACTED_EMAIL>", out)
    return out


def check_safe_portal_window(now: Optional[datetime] = None, allow_any_minute: bool = False) -> None:
    """Refuses execution during 01_JOB active windows (:00~:05 and :30~:35)."""
    if allow_any_minute:
        return
    dt = now or datetime.now()
    if dt.minute in range(0, 6) or dt.minute in range(30, 36):
        raise RuntimeError(
            f"[D107-2a] Execution blocked during 01_JOB active window (current time {dt.strftime('%H:%M')}; "
            "avoid :00~:05 and :30~:35)."
        )


def resolve_batch_cutoff_dt(
    now: Optional[datetime] = None,
    cutoff_dt: Optional[datetime] = None,
    morning_cutoff_hhmm: str = DEFAULT_MORNING_CUTOFF_HHMM,
) -> Optional[datetime]:
    """
    Resolves the hard stop deadline before the `07:30` `01_JOB` cycle (`#224` §B: `07:23` -> `07:29` cutoff).
    Applies automatically during any `07:xx` morning run, or when an explicit `cutoff_dt` is supplied.
    """
    if cutoff_dt is not None:
        return cutoff_dt
    dt = now or datetime.now()
    if dt.hour == 7 and morning_cutoff_hhmm:
        hh, mm = (int(x) for x in morning_cutoff_hhmm.split(":", 1))
        return dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return None


def is_past_batch_cutoff(now: datetime, cutoff_dt: Optional[datetime]) -> bool:
    """Returns True when `now` has reached or passed `cutoff_dt`."""
    return cutoff_dt is not None and now >= cutoff_dt


def attempt_groupware_login_once_or_halt(
    page: Any,
    gw_user: str,
    gw_pw: str,
    halt_flag_path: Path,
) -> bool:
    """
    1-shot portal login fallback with persistent circuit breaker (`#224` §B):
      - Refuses immediately if `halt_flag_path` exists.
      - Clears ID input before filling so residual values are never appended.
      - On any failure (login screen remains or error page), writes `halt_flag_path` and raises
        immediately with ZERO retries.
    """
    if halt_flag_path.exists():
        raise RuntimeError(
            f"[D107-2a] Portal login circuit breaker is OPEN ({halt_flag_path.name} present) — "
            "refusing login attempt to protect 10-strike lockout."
        )
    if not gw_user or not gw_pw:
        raise RuntimeError("[D107-2a] Missing WMS_USERNAME or WMS_PASSWORD for 1-shot portal login.")

    id_loc = page.locator('#txtPC_LoginID, input[type="text"], input[name*="id" i], input[id*="id" i]').first
    pw_temp_loc = page.locator("#txtPC_LoginPWTemp")
    pw_loc = page.locator('#txtPC_LoginPW, input[type="password"]').first
    btn_loc = page.locator('#btnPWLogin, button[type="submit"], input[type="submit"], #loginBtn, .btn-login').first

    if id_loc.count() == 0 or pw_loc.count() == 0 or btn_loc.count() == 0:
        halt_flag_path.write_text(f"DOM_MISMATCH {datetime.now().isoformat()}\n", encoding="utf-8")
        raise RuntimeError("[D107-2a] Login DOM selectors missing — circuit breaker tripped.")

    id_loc.fill("")
    id_loc.fill(gw_user)
    if pw_temp_loc.count() > 0:
        pw_temp_loc.focus()
        time.sleep(0.2)
    pw_loc.fill("")
    pw_loc.fill(gw_pw)

    btn_loc.click(no_wait_after=True)
    time.sleep(5.0)

    after_path = urlsplit(page.url).path.lower()
    pw_still_visible = False
    try:
        pw_still_visible = page.locator('input[type="password"]:visible').count() > 0
    except Exception:
        pass
    if "login" in after_path or "errorpage" in after_path or pw_still_visible:
        halt_flag_path.write_text(f"LOGIN_FAILED {datetime.now().isoformat()}\n", encoding="utf-8")
        raise RuntimeError(
            "[D107-2a] Single portal login attempt did not succeed — circuit breaker tripped "
            f"({halt_flag_path.name} written); zero retries performed."
        )
    return True


def _safe_locator_count(page: Any, selector: str) -> int:
    try:
        return int(page.locator(selector).count())
    except Exception:
        return 0


def _safe_text_count(page: Any, text_val: str) -> int:
    try:
        return int(page.get_by_text(text_val, exact=True).count())
    except Exception:
        return 0


def detect_portal_auth_state(page: Any) -> Dict[str, Any]:
    """
    Separates portal login-form detection from delayed app-menu loading (`#229` §1):
      - `SSO_UNAVAILABLE`: ONLY when a login input form (ID/password fields) is visibly rendered.
      - `READY`: Logged in (no login form) and `전자결재` (`e-Approval`) app element is present.
      - `PORTAL_NOT_READY`: Logged-in signals (`#nav-ai-btn` / `SL AI` / `APP STORE` / avatar / pinned menu)
        are present without a login form, while the `APP STORE` app icon list has not finished rendering.
      - `LOADING`: Initial transition with neither login form nor app element yet.
    """
    pw_visible = _safe_locator_count(page, '#txtPC_LoginPW:visible, input[type="password"]:visible') > 0
    id_visible = (
        _safe_locator_count(
            page,
            '#txtPC_LoginID:visible, input[name*="loginid" i]:visible, input[id*="loginid" i]:visible',
        )
        > 0
        and _safe_locator_count(page, '#txtPC_LoginPW, input[type="password"]') > 0
    )
    login_form_visible = bool(pw_visible or id_visible)

    logged_in_signal = bool(
        not login_form_visible
        and (
            _safe_locator_count(
                page,
                '#nav-ai-btn:visible, button.aiBtn:visible, a.my-apps-title-link:visible, '
                '#my-apps-slide-pinned, #user-settings-mail-filter, [data-dept-nm]',
            )
            > 0
            or _safe_text_count(page, "SL AI") > 0
            or _safe_text_count(page, "APP STORE") > 0
        )
    )

    app_visible = bool(
        not login_form_visible
        and (
            _safe_text_count(page, "전자결재") > 0
            or _safe_text_count(page, "e-Approval") > 0
            or _safe_locator_count(
                page,
                'a[title="전자결재"]:visible, a[title*="전자결재"]:visible, a[title="e-Approval"]:visible',
            )
            > 0
        )
    )

    if login_form_visible:
        auth_state = "SSO_UNAVAILABLE"
    elif app_visible:
        auth_state = "READY"
    elif logged_in_signal:
        auth_state = "PORTAL_NOT_READY"
    else:
        auth_state = "LOADING"

    return {
        "auth_state": auth_state,
        "login_form_visible": login_form_visible,
        "logged_in_signal": logged_in_signal,
        "app_visible": app_visible,
    }


def extract_dom_sso_bridge_url(page: Any, base_url: str) -> Optional[str]:
    """
    Reads a direct Groupware SSO bridge URL from the portal DOM if present (`#229` §3 fallback).
    Returns an absolute http(s) URL or None when only `#` / JS-bound handlers exist.
    """
    if not hasattr(page, "evaluate"):
        return None
    try:
        raw_url = page.evaluate(
            """() => {
                const isTarget = (el) => {
                    const label = [
                        el.getAttribute && el.getAttribute('title'),
                        el.getAttribute && el.getAttribute('aria-label'),
                        el.getAttribute && el.getAttribute('data-app-name'),
                        el.textContent,
                    ].filter(Boolean).join(' ');
                    return label.includes('전자결재') || label.includes('e-Approval');
                };
                const attrNames = ['data-sso-url', 'data-bridge-url', 'data-menu-url', 'data-url', 'href'];
                const candidates = Array.from(document.querySelectorAll('a, button, li, [data-sso-url], [data-menu-url]'));
                for (const el of candidates) {
                    if (!isTarget(el)) continue;
                    for (const attr of attrNames) {
                        const val = (el.getAttribute(attr) || '').trim();
                        if (val && val !== '#' && !val.toLowerCase().startsWith('javascript:')) {
                            return val;
                        }
                    }
                }
                return null;
            }"""
        )
    except Exception:
        return None
    if not raw_url or not isinstance(raw_url, str):
        return None
    resolved = urljoin(base_url, raw_url.strip())
    sp = urlsplit(resolved)
    if sp.scheme.lower() in ("http", "https") and sp.netloc:
        return resolved
    return None


def _dismiss_portal_notice_modals(page: Any) -> None:
    for btn_txt in ("전체 닫기", "닫기"):
        if _safe_text_count(page, btn_txt) > 0:
            try:
                page.get_by_text(btn_txt, exact=True).first.click(timeout=1500, no_wait_after=True)
                time.sleep(0.4)
            except Exception:
                pass


def _wait_for_groupware_bridge_tab(
    ctx: Any,
    pages_before: int,
    poll_interval_s: float = 1.0,
    max_polls: int = 20,
    expected_gw_host: str = "",
) -> Tuple[bool, bool, bool]:
    """
    Waits for the bridged Groupware tab/page to settle in `ctx`.
    Returns `(bridged_ok, login_form_seen_on_bridge, host_mismatch)`.
    """
    exp_host = (expected_gw_host or "").strip().lower()
    saw_host_mismatch = False
    for _ in range(max(1, max_polls)):
        if len(ctx.pages) > pages_before:
            gw_tab = ctx.pages[-1]
            sp = urlsplit(getattr(gw_tab, "url", "") or "")
            gp = sp.path.lower()
            gh = sp.netloc.lower()
            if "login.aspx" in gp or _safe_locator_count(gw_tab, 'input[type="password"]:visible') > 0:
                return False, True, False
            if exp_host and gh and gh != exp_host:
                saw_host_mismatch = True
            elif gp and gp != "blank" and ("approval" in gp or "website" in gp or "portal" in gp or "sso" in gp):
                if not exp_host or gh == exp_host:
                    if poll_interval_s >= 0.5:
                        time.sleep(min(2.0, poll_interval_s * 2))
                    return True, False, False
        if poll_interval_s > 0:
            time.sleep(poll_interval_s)
    if saw_host_mismatch:
        return False, False, True
    if exp_host and len(ctx.pages) > pages_before:
        gh = urlsplit(getattr(ctx.pages[-1], "url", "") or "").netloc.lower()
        if gh != exp_host:
            return False, False, True
    return len(ctx.pages) > pages_before and not exp_host, False, False


def _try_portal_js_menu_bridge(tab1: Any, gw_menu_row: Optional[Dict[str, Any]]) -> bool:
    """
    Invokes the portal's native `openMenuPageOrNoop(...)` SSO bridge in `tab1` using ONLY
    the exact `전자결재` (`e-Approval`) row object from `/portal/top/selectDrctMenu.json` (`#230`).
    Zero credential input — uses existing authenticated portal session only.
    """
    if not gw_menu_row or not hasattr(tab1, "evaluate"):
        return False
    names = {
        str(gw_menu_row.get("menu_nm") or "").strip(),
        str(gw_menu_row.get("menu_nm_kr") or "").strip(),
        str(gw_menu_row.get("menu_nm_en") or "").strip(),
    }
    if "전자결재" not in names and "e-Approval" not in names:
        return False
    try:
        return bool(
            tab1.evaluate(
                """(row) => {
                    if (typeof openMenuPageOrNoop !== 'function') return false;
                    openMenuPageOrNoop(
                        row.menu_id ?? '',
                        row.uppr_menu_id ?? '',
                        row.menu_url_get ?? '',
                        row.popup_yn ?? '',
                        row.popup_opt ?? '',
                        row.system_type ?? '',
                        row.menu_url ?? '',
                        row.menu_param ?? '',
                        row.def_param_yn ?? '',
                        row.left_yn ?? '',
                        'Y'
                    );
                    return true;
                }""",
                gw_menu_row,
            )
        )
    except Exception:
        return False


def ensure_BINSYNTH9941_and_groupware_sso(
    ctx: Any,
    BINSYNTH9941_url: str,
    wms_user: str,
    wms_pw: str,
    allow_login_once: bool,
    halt_flag_path: Path,
    cutoff_dt: Optional[datetime] = None,
    now_fn: Optional[Callable[[], datetime]] = None,
    max_wait_per_attempt_s: float = 60.0,
    max_reloads: int = 2,
    poll_interval_s: float = 1.0,
) -> Dict[str, Any]:
    """
    Establishes Groupware SSO session via `CORP_PORTAL_URL` (`#224` · `#229` hardened):
      1. Classification split (`#229` §1):
         - `SKIPPED_SSO_UNAVAILABLE`: ONLY when a login input form (ID/password) is visibly rendered.
         - `SKIPPED_PORTAL_NOT_READY`: Logged-in signal (`#nav-ai-btn` / `SL AI` / `APP STORE`) or slow
           portal render without any login form after bounded wait + reload retries.
      2. Bounded wait & reload retry (`#229` §2):
         - Waits (`networkidle` + selector polling up to `max_wait_per_attempt_s`) and reloads up to
           `max_reloads` times, strictly inside `cutoff_dt` (`07:29`).
      3. Same-context SSO bridge fallback (`#229` §3):
         - Primary: clicks `전자결재` / `e-Approval` app element in `APP STORE`.
         - Fallback A: opens a direct SSO bridge URL read from DOM (`extract_dom_sso_bridge_url`) in the
           same authenticated browser context (`ctx.new_page().goto(...)`).
         - Fallback B: invokes the portal's own `openMenuPageOrNoop(...)` with the `selectDrctMenu.json`
           `전자결재` row when `slTop.js`'s `commCondition` AJAX race leaves the carousel empty.
         - Zero credential entry when `allow_login_once=False`.
    """
    clock = now_fn or datetime.now
    tab1 = ctx.pages[0] if ctx.pages else ctx.new_page()
    if hasattr(tab1, "on"):
        tab1.on("dialog", lambda d: d.accept())

    captured_gw_menu: Dict[str, Any] = {}

    def _capture_drct_menu(resp: Any) -> None:
        try:
            if "selectDrctMenu.json" not in str(getattr(resp, "url", "")):
                return
            payload = resp.json()
            items = payload.get("input_data") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                names = {
                    str(item.get("menu_nm") or "").strip(),
                    str(item.get("menu_nm_kr") or "").strip(),
                    str(item.get("menu_nm_en") or "").strip(),
                }
                if "전자결재" in names or "e-Approval" in names:
                    captured_gw_menu.update(item)
                    break
        except Exception:
            pass

    if hasattr(tab1, "on"):
        try:
            tab1.on("response", _capture_drct_menu)
        except Exception:
            pass

    saw_logged_in = False
    attempts_done = 0
    total_attempts = 1 + max(0, int(max_reloads))

    for attempt_idx in range(total_attempts):
        if is_past_batch_cutoff(clock(), cutoff_dt):
            break

        attempts_done = attempt_idx + 1
        if attempt_idx == 0:
            tab1.goto(BINSYNTH9941_url, wait_until="domcontentloaded", timeout=30000)
        else:
            logger.info(
                "[D107-2a] Portal app menu not ready; reloading CORP_PORTAL_URL (retry %d/%d)...",
                attempt_idx,
                max_reloads,
            )
            if hasattr(tab1, "reload"):
                tab1.reload(wait_until="domcontentloaded", timeout=30000)
            else:
                tab1.goto(BINSYNTH9941_url, wait_until="domcontentloaded", timeout=30000)

        if hasattr(tab1, "wait_for_load_state") and not is_past_batch_cutoff(clock(), cutoff_dt):
            try:
                tab1.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass

        max_polls = max(1, int(round(max_wait_per_attempt_s / max(poll_interval_s, 0.05))))
        js_bridge_tried_this_attempt = False

        for poll_i in range(max_polls):
            if is_past_batch_cutoff(clock(), cutoff_dt):
                break

            auth = detect_portal_auth_state(tab1)
            if auth["logged_in_signal"]:
                saw_logged_in = True

            if auth["login_form_visible"]:
                if not allow_login_once:
                    return {
                        "portal_ready": False,
                        "failure_reason": "SSO_UNAVAILABLE",
                        "status_code": "SKIPPED_SSO_UNAVAILABLE",
                        "sso_zero_input": False,
                        "bridge_clicked": False,
                        "bridge_mode": "none",
                        "attempts": attempts_done,
                        "note": (
                            "Login input form is visible on CORP_PORTAL_URL (PC SSO did not authenticate automatically); "
                            "stopped immediately with zero credential input (SKIPPED_SSO_UNAVAILABLE)."
                        ),
                    }
                attempt_groupware_login_once_or_halt(tab1, wms_user, wms_pw, halt_flag_path)
                continue

            _dismiss_portal_notice_modals(tab1)
            auth = detect_portal_auth_state(tab1)
            if auth["logged_in_signal"]:
                saw_logged_in = True

            if auth["app_visible"]:
                pages_before = len(ctx.pages)
                clicked = False
                for txt_label in ("전자결재", "e-Approval"):
                    if _safe_text_count(tab1, txt_label) > 0:
                        tab1.get_by_text(txt_label, exact=True).first.click(timeout=5000, no_wait_after=True)
                        clicked = True
                        break
                if not clicked and _safe_locator_count(tab1, 'a[title*="전자결재"], a[title="e-Approval"]') > 0:
                    tab1.locator('a[title*="전자결재"], a[title="e-Approval"]').first.click(
                        timeout=5000, no_wait_after=True
                    )
                    clicked = True

                if clicked:
                    bridged_ok, bridge_saw_login, _ = _wait_for_groupware_bridge_tab(
                        ctx, pages_before=pages_before, poll_interval_s=min(poll_interval_s, 1.0), max_polls=20
                    )
                    if bridge_saw_login:
                        return {
                            "portal_ready": False,
                            "failure_reason": "SSO_UNAVAILABLE",
                            "status_code": "SKIPPED_SSO_UNAVAILABLE",
                            "sso_zero_input": False,
                            "bridge_clicked": True,
                            "bridge_mode": "app_click",
                            "attempts": attempts_done,
                            "note": (
                                "Groupware SSO bridge redirected to a visible login form; "
                                "stopped immediately with zero credential input (SKIPPED_SSO_UNAVAILABLE)."
                            ),
                        }
                    return {
                        "portal_ready": True,
                        "failure_reason": None,
                        "status_code": "READY",
                        "sso_zero_input": True,
                        "bridge_clicked": True,
                        "bridge_mode": "app_click",
                        "attempts": attempts_done,
                        "note": (
                            f"CORP_PORTAL_URL session active (SSO zero-input=True, "
                            f"Groupware SSO app bridge clicked=True, attempts={attempts_done})."
                        ),
                    }

            if auth["logged_in_signal"]:
                dom_bridge_url = extract_dom_sso_bridge_url(tab1, BINSYNTH9941_url)
                if dom_bridge_url:
                    bridge_page = ctx.new_page()
                    if hasattr(bridge_page, "on"):
                        bridge_page.on("dialog", lambda d: d.accept())
                    bridge_page.goto(dom_bridge_url, wait_until="domcontentloaded", timeout=30000)
                    if poll_interval_s >= 0.5:
                        time.sleep(min(2.0, poll_interval_s * 2))
                    bp_path = urlsplit(getattr(bridge_page, "url", "") or "").path.lower()
                    if "login.aspx" in bp_path or _safe_locator_count(bridge_page, 'input[type="password"]:visible') > 0:
                        return {
                            "portal_ready": False,
                            "failure_reason": "SSO_UNAVAILABLE",
                            "status_code": "SKIPPED_SSO_UNAVAILABLE",
                            "sso_zero_input": False,
                            "bridge_clicked": True,
                            "bridge_mode": "dom_url",
                            "attempts": attempts_done,
                            "note": (
                                "DOM SSO bridge URL redirected to a visible login form; "
                                "stopped immediately with zero credential input (SKIPPED_SSO_UNAVAILABLE)."
                            ),
                        }
                    return {
                        "portal_ready": True,
                        "failure_reason": None,
                        "status_code": "READY",
                        "sso_zero_input": True,
                        "bridge_clicked": True,
                        "bridge_mode": "dom_url",
                        "attempts": attempts_done,
                        "note": (
                            f"CORP_PORTAL_URL session active (SSO zero-input=True, "
                            f"Groupware SSO bridged via same-context DOM URL, attempts={attempts_done})."
                        ),
                    }

                halfway_poll = max(1, min(8, max_polls // 2))
                if captured_gw_menu and not js_bridge_tried_this_attempt and poll_i >= halfway_poll:
                    js_bridge_tried_this_attempt = True
                    pages_before = len(ctx.pages)
                    exp_gw_host = (
                        urlsplit(os.getenv("CORP_GROUPWARE_PORTAL_URL", "").strip()).netloc.lower()
                        or urlsplit(str(captured_gw_menu.get("menu_url") or "")).netloc.lower()
                    )
                    if _try_portal_js_menu_bridge(tab1, captured_gw_menu):
                        bridged_ok, bridge_saw_login, host_mismatch = _wait_for_groupware_bridge_tab(
                            ctx,
                            pages_before=pages_before,
                            poll_interval_s=min(poll_interval_s, 1.0),
                            max_polls=20,
                            expected_gw_host=exp_gw_host,
                        )
                        if bridge_saw_login:
                            return {
                                "portal_ready": False,
                                "failure_reason": "SSO_UNAVAILABLE",
                                "status_code": "SKIPPED_SSO_UNAVAILABLE",
                                "sso_zero_input": False,
                                "bridge_clicked": True,
                                "bridge_mode": "js_menu",
                                "attempts": attempts_done,
                                "note": (
                                    "Portal menu SSO bridge redirected to a visible login form; "
                                    "stopped immediately with zero credential input (SKIPPED_SSO_UNAVAILABLE)."
                                ),
                            }
                        if host_mismatch or not bridged_ok:
                            return {
                                "portal_ready": False,
                                "failure_reason": "PORTAL_NOT_READY",
                                "status_code": "SKIPPED_PORTAL_NOT_READY",
                                "sso_zero_input": True,
                                "bridge_clicked": True,
                                "bridge_mode": "js_menu",
                                "attempts": attempts_done,
                                "note": (
                                    "Portal js_menu bridge tab did not navigate to the expected approval host; "
                                    "aborted immediately with zero credential input (SKIPPED_PORTAL_NOT_READY)."
                                ),
                            }
                        return {
                            "portal_ready": True,
                            "failure_reason": None,
                            "status_code": "READY",
                            "sso_zero_input": True,
                            "bridge_clicked": True,
                            "bridge_mode": "js_menu",
                            "attempts": attempts_done,
                            "note": (
                                f"CORP_PORTAL_URL session active (SSO zero-input=True, "
                                f"Groupware SSO bridged via same-context js_menu, attempts={attempts_done})."
                            ),
                        }

            if poll_i + 1 < max_polls and poll_interval_s > 0:
                time.sleep(poll_interval_s)

    return {
        "portal_ready": False,
        "failure_reason": "PORTAL_NOT_READY",
        "status_code": "SKIPPED_PORTAL_NOT_READY",
        "sso_zero_input": True,
        "bridge_clicked": False,
        "bridge_mode": "none",
        "attempts": attempts_done,
        "note": (
            f"Portal main page did not finish rendering e-Approval app bridge (logged_in_signal={saw_logged_in}, "
            f"attempts={attempts_done}) within cutoff; stopped with zero credential input (SKIPPED_PORTAL_NOT_READY)."
        ),
    }


def extract_wf_form_st_78_rows(page: Any) -> List[Dict[str, Any]]:
    """
    Read-only extractor for `WF_FORM_ST_78` item rows across all frames on `Form.aspx`.
    Matches any table containing `SUMNO`, `BARCODE`, and `PART`/`MATERIAL` headers.
    """
    extracted: List[Dict[str, Any]] = []
    for frame in page.frames:
        try:
            tables = frame.evaluate(
                """() => Array.from(document.querySelectorAll('table')).map(tbl => ({
                    rows: Array.from(tbl.rows).map(tr =>
                        Array.from(tr.cells).map(td => (td.innerText || td.textContent || '').trim())
                    )
                }))"""
            )
        except Exception:
            continue
        for tbl in tables or []:
            rows = tbl.get("rows") or []
            if len(rows) < 2:
                continue
            hdr_idx = None
            col_idx: Dict[str, int] = {}
            for r_i, cells in enumerate(rows[:5]):
                upper_cells = [re.sub(r"\s+", " ", c.upper()).strip() for c in cells]
                if any("SUMNO" in c for c in upper_cells) and any("BARCODE" in c for c in upper_cells):
                    hdr_idx = r_i
                    for c_i, uc in enumerate(upper_cells):
                        if "SUMNO" in uc and "sumno" not in col_idx:
                            col_idx["sumno"] = c_i
                        elif "BARCODE" in uc and "barcode" not in col_idx:
                            col_idx["barcode"] = c_i
                        elif ("PART NUMBER" in uc or uc in ("PART", "PART NO", "MATERIAL")) and "material" not in col_idx:
                            col_idx["material"] = c_i
                        elif "MVT" in uc and "mvt" not in col_idx:
                            col_idx["mvt"] = c_i
                        elif "FROM" in uc and "from_sloc" not in col_idx:
                            col_idx["from_sloc"] = c_i
                        elif "TO" in uc and "to_sloc" not in col_idx:
                            col_idx["to_sloc"] = c_i
                        elif "QTY" in uc and "qty" not in col_idx:
                            col_idx["qty"] = c_i
                    break
            if hdr_idx is None or len(col_idx) < 5:
                continue
            for data_cells in rows[hdr_idx + 1:]:
                if len(data_cells) <= max(col_idx.values()):
                    continue
                sumno_v = data_cells[col_idx["sumno"]].strip()
                barcode_v = data_cells[col_idx["barcode"]].strip()
                if not sumno_v and not barcode_v:
                    continue
                qty_str = data_cells[col_idx.get("qty", -1)].replace(",", "").strip() if "qty" in col_idx else "0"
                try:
                    qty_f = float(qty_str or 0.0)
                except ValueError:
                    qty_f = 0.0
                extracted.append(
                    {
                        "sumno": sumno_v,
                        "barcode": barcode_v,
                        "material": data_cells[col_idx["material"]].strip() if "material" in col_idx else "",
                        "mvt": data_cells[col_idx["mvt"]].replace(".0", "").strip() if "mvt" in col_idx else "",
                        "from_sloc": data_cells[col_idx["from_sloc"]].strip().upper() if "from_sloc" in col_idx else "",
                        "to_sloc": data_cells[col_idx["to_sloc"]].strip().upper() if "to_sloc" in col_idx else "",
                        "qty": qty_f,
                    }
                )
            if extracted:
                return extracted
    return extracted


def validate_portal_headers(headers: Sequence[str]) -> Dict[str, int]:
    """Validates `#excelTable thead th` headers and returns uppercase header -> column index map."""
    norm_map: Dict[str, int] = {}
    for idx, h in enumerate(headers):
        clean = str(h or "").strip().upper()
        if clean and clean not in norm_map:
            norm_map[clean] = idx
    missing = [req for req in REQUIRED_PORTAL_HEADERS if req not in norm_map]
    if missing:
        raise PortalSchemaMismatchError(
            f"[D107-2a] Portal table schema changed — missing required column(s): {missing} (found: {list(norm_map.keys())})"
        )
    return norm_map


def _norm_ymd(date_str: Any) -> str:
    digits = re.sub(r"\D", "", str(date_str or ""))
    return digits[:8] if len(digits) >= 8 else ""


def _sig5(part: str, mvt: str, from_loc: str, to_loc: str, qty: float) -> Tuple[str, str, str, str, float]:
    return (
        str(part or "").strip(),
        str(mvt or "").replace(".0", "").strip(),
        str(from_loc or "").strip().upper(),
        str(to_loc or "").strip().upper(),
        round(float(qty or 0.0), 3),
    )


def get_today_d107_phase1_path(
    report_date_str: str,
    resolution_logs_dir: Optional[Path] = None,
) -> Path:
    """Returns expected path of today's `d107_phase1_YYYYMMDD.json` generated by `07:18` collect."""
    logs_dir = Path(resolution_logs_dir) if resolution_logs_dir else (JOB_DIR / "resolution_logs")
    ymd = report_date_str.replace("-", "")
    return logs_dir / f"d107_phase1_{ymd}.json"


def load_latest_d107_phase1_payload(
    report_date_str: str,
    resolution_logs_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Optional[Path]]:
    """
    Loads today's `d107_phase1_YYYYMMDD.json` if present; otherwise falls back to the most recent
    confirmed `d107_phase1_*.json` on or before `report_date_str`.
    """
    logs_dir = Path(resolution_logs_dir) if resolution_logs_dir else (JOB_DIR / "resolution_logs")
    ymd = report_date_str.replace("-", "")
    exact = logs_dir / f"d107_phase1_{ymd}.json"
    if exact.exists():
        return json.loads(exact.read_text(encoding="utf-8")), exact
    cands = sorted(
        p for p in logs_dir.glob("d107_phase1_*.json")
        if p.stem.split("_")[-1].isdigit() and p.stem.split("_")[-1] <= ymd
    )
    if cands:
        latest = cands[-1]
        return json.loads(latest.read_text(encoding="utf-8")), latest
    return {"report_date": report_date_str, "candidates": []}, None


def load_latest_01_gi_records(gi_errors_dir: Optional[Path] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Loads the most recent `01_JOB` `GI_Error_*.XLS` snapshot for pre-selection re-verification."""
    err_dir = Path(gi_errors_dir) if gi_errors_dir else (JOB_DIR / "gi_errors")
    if not err_dir.exists():
        return [], None
    files = sorted(err_dir.glob("GI_Error_*.XLS"), key=lambda p: p.stat().st_mtime)
    if not files:
        return [], None
    latest = files[-1]
    return parse_ztcode_stock_rec_gi_records_detailed(latest), latest.name


def extract_all_portal_row_keys(
    portal_headers: Sequence[str],
    portal_rows: Sequence[Sequence[Any]],
) -> set:
    """
    Extracts `d107_drafts` composite keys `(SUMNO, BARCODE, PART, MVT, QTY)` across ALL live rows
    in `#excelTable` so prior `SAVED_TEMP` reconciliation checks the entire portal table.
    """
    col_map = validate_portal_headers(portal_headers)
    keys = set()
    for raw in portal_rows:
        if len(raw) <= 1:
            continue

        def _c(col_name: str) -> str:
            ci = col_map[col_name]
            return str(raw[ci] if ci < len(raw) else "").strip()

        sumno = _c("SUMNO")
        barcode = _c("BARCODE")
        if not sumno and not barcode:
            continue
        part_no = _c("PART NUMBER")
        mvt = _c("MVT").replace(".0", "").strip()
        qty_raw = _c("QTY").replace(",", "")
        try:
            qty_val = float(qty_raw or 0.0)
        except ValueError:
            qty_val = 0.0
        keys.add(db_manager.build_d107_draft_row_key(sumno, barcode, part_no, mvt, qty_val))
    return keys


def filter_and_bundle_portal_rows(
    portal_headers: Sequence[str],
    portal_rows: Sequence[Sequence[Any]],
    d107_candidates: Sequence[Dict[str, Any]],
    latest_01_gi_records: Optional[Sequence[Dict[str, Any]]],
    report_date_str: str,
    max_rows_per_draft: int = PORTAL_MAX_ROWS_PER_DRAFT,
    pending_temp_drafts: Optional[Dict[str, Dict[str, Any]]] = None,
    force_resave_stale: bool = False,
) -> Dict[str, Any]:
    """
    Applies strict D107 Phase 2 eligibility & bundling rules (#222 + 14:24 override):
      1. Header schema verified (`validate_portal_headers`).
      2. Each D107 `CANDIDATE` re-verified against `latest_01_gi_records` (when provided) so rows already
         cleared in SAP ZTCODE_STOCK_REC are excluded before portal selection.
      3. Only portal rows with `POST DATE <= yesterday`, `MVT in {'311', '312'}`, non-empty `SUMNO` + `BARCODE`,
         and 1-to-1 multiset match on `(PART NUMBER, MVT, FROM LOC, TO LOC, QTY)` against verified D107
         `CANDIDATE` rows are selected.
      4. Selected rows are chunked into drafts of at most `max_rows_per_draft` (`20`).
    """
    col_map = validate_portal_headers(portal_headers)
    yesterday_ymd = (datetime.strptime(report_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")

    latest_01_counts: Optional[Dict[Tuple[str, str, str, str, float], int]] = None
    if latest_01_gi_records is not None:
        latest_01_counts = {}
        for r in latest_01_gi_records:
            k = _sig5(
                r.get("material", ""),
                r.get("mvt", ""),
                r.get("from_sloc", ""),
                r.get("to_sloc", ""),
                float(r.get("qty") or 0.0),
            )
            latest_01_counts[k] = latest_01_counts.get(k, 0) + 1

    verified_pool: List[Dict[str, Any]] = []
    excluded_resolved_01: List[Dict[str, Any]] = []
    rem_01 = dict(latest_01_counts) if latest_01_counts is not None else None

    for c in d107_candidates:
        if str(c.get("d107_status") or "CANDIDATE").upper() != "CANDIDATE":
            continue
        k = _sig5(
            c.get("material", ""),
            c.get("mvt", ""),
            c.get("from_sloc", ""),
            c.get("to_sloc", ""),
            float(c.get("qty") or 0.0),
        )
        if rem_01 is not None:
            if rem_01.get(k, 0) <= 0:
                excluded_resolved_01.append(dict(c))
                continue
            rem_01[k] -= 1
        verified_pool.append(dict(c))

    available_cands: List[Optional[Dict[str, Any]]] = list(verified_pool)
    matched_rows: List[Dict[str, Any]] = []
    skipped_portal_rows: List[Dict[str, Any]] = []
    pending_user_submit_rows: List[Dict[str, Any]] = []
    portal_row_keys: List[str] = []
    pending_lookup = dict(pending_temp_drafts or {})

    for row_idx, raw in enumerate(portal_rows):
        if len(raw) < len(col_map):
            if len(raw) <= 1:
                continue

        def _cell(col_name: str) -> str:
            ci = col_map[col_name]
            return str(raw[ci] if ci < len(raw) else "").strip()

        plant = _cell("PLANT")
        post_date_ymd = _norm_ymd(_cell("POST DATE"))
        gubun = _cell("GUBUN")
        sumno = _cell("SUMNO")
        barcode = _cell("BARCODE")
        part_no = _cell("PART NUMBER")
        part_desc = _cell("PART DESC")
        mvt = _cell("MVT").replace(".0", "").strip()
        from_loc = _cell("FROM LOC").upper()
        to_loc = _cell("TO LOC").upper()
        qty_raw = _cell("QTY").replace(",", "")
        note_txt = _cell("NOTE") if "NOTE" in col_map else ""

        try:
            qty_val = float(qty_raw or 0.0)
        except ValueError:
            qty_val = 0.0

        row_info = {
            "portal_row_index": row_idx,
            "plant": plant,
            "post_date": post_date_ymd,
            "gubun": gubun,
            "sumno": sumno,
            "barcode": barcode,
            "material": part_no,
            "material_desc": part_desc,
            "mvt": mvt,
            "from_sloc": from_loc,
            "to_sloc": to_loc,
            "qty": qty_val,
            "note": note_txt,
        }

        if not post_date_ymd or post_date_ymd > yesterday_ymd:
            row_info["skip_reason"] = f"POST_DATE_AFTER_YESTERDAY ({post_date_ymd} > {yesterday_ymd})"
            skipped_portal_rows.append(row_info)
            continue
        if mvt not in D107_TARGET_MVTS:
            row_info["skip_reason"] = f"EXCLUDED_MVT ({mvt})"
            skipped_portal_rows.append(row_info)
            continue
        if not sumno or not barcode:
            row_info["skip_reason"] = "MISSING_SUMNO_OR_BARCODE"
            skipped_portal_rows.append(row_info)
            continue

        rk = db_manager.build_d107_draft_row_key(sumno, barcode, part_no, mvt, qty_val)
        row_info["row_key"] = rk
        portal_row_keys.append(rk)

        if rk in pending_lookup:
            saved_rec = pending_lookup[rk]
            saved_dt_str = str(saved_rec.get("saved_date") or saved_rec.get("saved_at") or "").strip()
            pend_label, is_stale, bdays = db_manager.format_d107_pending_or_stale_label(saved_dt_str, report_date_str)
            if not (is_stale and force_resave_stale):
                row_sig_p = _sig5(part_no, mvt, from_loc, to_loc, qty_val)
                for ci_idx, cand in enumerate(available_cands):
                    if cand is None:
                        continue
                    c_sig = _sig5(
                        cand.get("material", ""),
                        cand.get("mvt", ""),
                        cand.get("from_sloc", ""),
                        cand.get("to_sloc", ""),
                        float(cand.get("qty") or 0.0),
                    )
                    if c_sig == row_sig_p:
                        available_cands[ci_idx] = None
                        break
                row_info["skip_reason"] = pend_label
                row_info["is_stale"] = is_stale
                row_info["business_days_elapsed"] = bdays
                row_info["saved_at"] = saved_rec.get("saved_at")
                pending_user_submit_rows.append(row_info)
                skipped_portal_rows.append(row_info)
                continue

        row_sig = _sig5(part_no, mvt, from_loc, to_loc, qty_val)
        matched_cand: Optional[Dict[str, Any]] = None
        for ci_idx, cand in enumerate(available_cands):
            if cand is None:
                continue
            c_sig = _sig5(
                cand.get("material", ""),
                cand.get("mvt", ""),
                cand.get("from_sloc", ""),
                cand.get("to_sloc", ""),
                float(cand.get("qty") or 0.0),
            )
            if c_sig == row_sig:
                matched_cand = cand
                available_cands[ci_idx] = None
                break

        if matched_cand is None:
            row_info["skip_reason"] = "NOT_IN_VERIFIED_D107_CANDIDATES"
            skipped_portal_rows.append(row_info)
            continue

        row_info["d107_candidate"] = matched_cand
        matched_rows.append(row_info)

    eff_limit = max(1, int(max_rows_per_draft))
    draft_batches: List[List[Dict[str, Any]]] = [
        matched_rows[i: i + eff_limit] for i in range(0, len(matched_rows), eff_limit)
    ]

    return {
        "report_date": report_date_str,
        "yesterday_ymd": yesterday_ymd,
        "portal_total_rows": len([r for r in portal_rows if len(r) > 1]),
        "d107_input_candidate_count": len(d107_candidates),
        "d107_verified_still_open_count": len(verified_pool),
        "excluded_resolved_in_latest_01": excluded_resolved_01,
        "matched_rows": matched_rows,
        "skipped_portal_rows": skipped_portal_rows,
        "pending_user_submit_rows": pending_user_submit_rows,
        "portal_row_keys": portal_row_keys,
        "max_rows_per_draft": eff_limit,
        "draft_count": len(draft_batches),
        "draft_batches": draft_batches,
    }


def format_single_row_reason_en(matched_row: Dict[str, Any]) -> str:
    """Formats 1 concise English D107 basis line for `Reason for deletion` (#222 14:24 #3)."""
    cand = matched_row.get("d107_candidate") or {}
    part = matched_row["material"]
    f_loc = matched_row["from_sloc"]
    t_loc = matched_row["to_sloc"]
    mvt = matched_row["mvt"]
    qty = float(matched_row["qty"] or 0.0)
    def_loc = str(cand.get("deficit_loc") or (t_loc if mvt == "312" else f_loc)).upper()
    def_sap = float(cand.get("to_sap" if mvt == "312" else "from_sap") or 0.0)
    def_wms = float(cand.get("to_wms" if mvt == "312" else "from_wms") or 0.0)
    first_seen = str(cand.get("first_seen_edt") or matched_row.get("post_date") or "").strip()
    mmdd = first_seen[5:10].replace("-", "/") if len(first_seen) >= 10 and "-" in first_seen[:10] else (
        f"{matched_row['post_date'][4:6]}/{matched_row['post_date'][6:8]}"
        if len(str(matched_row.get("post_date") or "")) == 8
        else "prev-day"
    )
    return (
        f"{part} {f_loc}->{t_loc} {mvt} {qty:,.0f} EA (SUMNO {matched_row['sumno']}): "
        f"SAP {def_sap:,.0f}/WMS {def_wms:,.0f} in {def_loc} since {mmdd}, "
        f"no GR pending, no D97/D113/D115 path -- unresolvable"
    )


def build_deletion_reason_and_attachment(
    matched_rows: Sequence[Dict[str, Any]],
    report_date_str: str,
    max_reason_chars: int = DEFAULT_REASON_MAX_CHARS,
    out_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Builds English `Reason for deletion` text. If length exceeds `max_reason_chars`, switches to
    1-line English summary and writes per-row English evidence Excel attachment (D111 B-3 compliant).
    """
    if not matched_rows:
        return {
            "reason_text": "",
            "overflow_used": False,
            "attachment_path": None,
            "per_row_lines": [],
        }

    lines = [format_single_row_reason_en(r) for r in matched_rows]
    joined = "\n".join(lines)
    if len(joined) <= max_reason_chars:
        assert_no_hangul({"reason_for_deletion": joined})
        return {
            "reason_text": joined,
            "overflow_used": False,
            "attachment_path": None,
            "per_row_lines": lines,
        }

    target_dir = Path(out_dir) if out_dir else (JOB_DIR / "scratch")
    target_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    xlsx_path = target_dir / f"D107_Deletion_Basis_{ts}.xlsx"

    rows_export = []
    for r, ln in zip(matched_rows, lines):
        rows_export.append(
            {
                "Plant": r["plant"],
                "Post Date": r["post_date"],
                "SUMNO": r["sumno"],
                "Barcode": r["barcode"],
                "Material": r["material"],
                "Description": r.get("material_desc", ""),  # B-3 raw SAP MAKTX exempt column
                "Mvt": r["mvt"],
                "From SLoc": r["from_sloc"],
                "To SLoc": r["to_sloc"],
                "Qty": float(r["qty"] or 0.0),
                "D107 Basis": ln,
            }
        )
    pd.DataFrame(rows_export).to_excel(xlsx_path, index=False, sheet_name="D107_Deletion_Basis")
    summary_line = (
        f"{len(matched_rows)} unresolvable prev-day GI error row(s) verified by D107 Phase 1 "
        f"(0 GR pending, 0 physical/pumpable stock, no D97/D113/D115 path); "
        f"see attached {xlsx_path.name} for per-row basis."
    )
    assert_no_hangul({"reason_for_deletion": summary_line}, attachments=[xlsx_path])
    return {
        "reason_text": summary_line,
        "overflow_used": True,
        "attachment_path": str(xlsx_path),
        "per_row_lines": lines,
    }


def verify_form_rows_match(
    selected_rows: Sequence[Dict[str, Any]],
    form_rows: Sequence[Dict[str, Any]],
) -> bool:
    """
    Verifies 100% 1-to-1 match on (SUMNO, BARCODE, PART, MVT, FROM, TO, QTY) between selected
    portal rows and rendered `WF_FORM_ST_78` form table rows. Raises `PortalFormRowMismatchError` on mismatch.
    """
    def _key7(r: Dict[str, Any]) -> Tuple[str, str, str, str, str, str, float]:
        return (
            str(r.get("sumno") or "").strip(),
            str(r.get("barcode") or "").strip(),
            str(r.get("material") or r.get("part_number") or "").strip(),
            str(r.get("mvt") or "").replace(".0", "").strip(),
            str(r.get("from_sloc") or r.get("from_loc") or "").strip().upper(),
            str(r.get("to_sloc") or r.get("to_loc") or "").strip().upper(),
            round(float(r.get("qty") or 0.0), 3),
        )

    sel_keys = sorted(_key7(r) for r in selected_rows)
    frm_keys = sorted(_key7(r) for r in form_rows)
    if sel_keys != frm_keys:
        raise PortalFormRowMismatchError(
            f"[D107-2a] WF_FORM_ST_78 row mismatch! Selected={sel_keys} vs Form={frm_keys}"
        )
    return True


def _find_wf_form_frame(form_page: Any) -> Any:
    """Locates the `Form.aspx` frame containing `#SUBJECT` / `#btSave` on `WF_FORM_ST_78`."""
    for frame in getattr(form_page, "frames", []) or []:
        f_url = str(getattr(frame, "url", "") or "").lower()
        if "form.aspx" in f_url:
            return frame
    for frame in getattr(form_page, "frames", []) or []:
        try:
            if frame.locator(WF_FORM_SAVE_TEMP_SELECTOR).count() > 0 or frame.locator(WF_FORM_SUBJECT_SELECTOR).count() > 0:
                return frame
        except Exception:
            continue
    return getattr(form_page, "main_frame", form_page)


def execute_save_temp_on_form(
    form_page: Any,
    draft_subject: str,
    reason_text: str,
    attachment_path: Optional[str],
    mode: str,
    save_shot_path: Optional[Path] = None,
    poll_interval_s: float = 0.5,
    max_wait_s: float = 15.0,
) -> Dict[str, Any]:
    """
    Executes Step 2b `Save temp.` (`#btSave`) on `WF_FORM_ST_78` (`Form.aspx`) with triple `Submit` lock:
      1. Python action gate (`assert_not_submit_action` + `click_portal_form_action`).
      2. Browser JS hard-lock overriding `doButtonCase.btDraft/btApproved/btRec/btDeptDraft/btAction`
         and wrapping `window.requestProcess` to reject any mode != `'TEMPSAVE'`.
      3. Fills English `#SUBJECT` (`Request to delete IWMS_GI of YYYY/MM/DD`) and English
         `#JFORM_BUSINESS_CONTENTS_ETC`, clicks `#btSave` ONLY, verifies `submitForm.aspx` / Covision
         `#AlertLayer` / popup close, captures screenshot, and closes the form.
    """
    assert_not_submit_action(WF_FORM_SAVE_TEMP_SELECTOR)
    if mode != "save_temp_2b":
        raise PermissionError(f"[D107-2b] execute_save_temp_on_form is forbidden in mode='{mode}'.")
    assert_no_hangul({"draft_subject": draft_subject, "reason_text": reason_text})

    form_frame = _find_wf_form_frame(form_page)

    # Browser-side JS hard-lock against accidental Submit/Draft execution
    if hasattr(form_frame, "evaluate"):
        try:
            form_frame.evaluate(
                """() => {
                    const blockSubmit = () => {
                        throw new Error('D107_2B_HARD_LOCK: Submit/Draft action is strictly forbidden');
                    };
                    if (typeof window.doButtonCase === 'object' && window.doButtonCase) {
                        for (const k of ['btDraft', 'btApproved', 'btRec', 'btDeptDraft', 'btAction']) {
                            window.doButtonCase[k] = blockSubmit;
                        }
                    }
                    if (typeof window.requestProcess === 'function' && !window.__d107ReqProcWrapped) {
                        const origReq = window.requestProcess;
                        window.requestProcess = function(reqMode) {
                            if (String(reqMode || '').toUpperCase() !== 'TEMPSAVE') {
                                throw new Error('D107_2B_HARD_LOCK: requestProcess(' + reqMode + ') forbidden');
                            }
                            return origReq.apply(this, arguments);
                        };
                        window.__d107ReqProcWrapped = true;
                    }
                }"""
            )
        except Exception:
            pass

    subj_loc = form_frame.locator(WF_FORM_SUBJECT_SELECTOR).first
    if subj_loc.count() == 0:
        raise PortalSchemaMismatchError(f"[D107-2b] Subject input ({WF_FORM_SUBJECT_SELECTOR}) not found on Form.aspx.")
    subj_loc.fill(draft_subject)

    reason_loc = form_frame.locator(WF_FORM_REASON_SELECTOR).first
    if reason_loc.count() == 0:
        raise PortalSchemaMismatchError(f"[D107-2b] Reason textarea ({WF_FORM_REASON_SELECTOR}) not found on Form.aspx.")
    reason_loc.fill(reason_text)

    if attachment_path and Path(attachment_path).exists():
        try:
            file_loc = form_frame.locator('input[type="file"]').first
            if file_loc.count() > 0:
                file_loc.set_input_files(str(Path(attachment_path).resolve()))
        except Exception as exc_att:
            logger.warning("[D107-2b] Optional form file input upload skipped: %s", redact_portal_sensitive(exc_att))

    submit_form_http_status: List[int] = []

    def _on_resp(resp: Any) -> None:
        try:
            r_url = str(getattr(resp, "url", "") or "").lower()
            if "submitform.aspx" in r_url:
                submit_form_http_status.append(int(getattr(resp, "status", 0) or 0))
        except Exception:
            pass

    if hasattr(form_page, "on"):
        try:
            form_page.on("response", _on_resp)
        except Exception:
            pass

    # Pre-capture filled form so screenshot is preserved even if submitForm.aspx calls window.close()
    if save_shot_path is not None and hasattr(form_page, "screenshot"):
        try:
            form_page.screenshot(path=str(save_shot_path), full_page=True)
        except Exception:
            pass

    click_portal_form_action(form_frame, WF_FORM_SAVE_TEMP_SELECTOR, mode=mode)

    confirm_state = "PENDING"
    max_polls = max(1, int(round(max_wait_s / max(poll_interval_s, 0.05))))
    for poll_idx in range(max_polls):
        if hasattr(form_page, "is_closed") and form_page.is_closed():
            confirm_state = "WINDOW_CLOSED_AFTER_TEMPSAVE"
            break

        alert_state: Optional[Dict[str, Any]] = None
        if hasattr(form_frame, "evaluate"):
            try:
                alert_state = form_frame.evaluate(
                    """() => {
                        const errEl = document.querySelector('#AlertLayer_Error');
                        if (errEl && errEl.offsetParent !== null) {
                            const em = document.querySelector('#popup_messageError');
                            return { kind: 'error', msg: (em ? em.innerText : errEl.innerText || '').trim() };
                        }
                        const al = document.querySelector('#AlertLayer');
                        if (al && al.offsetParent !== null) {
                            const pm = document.querySelector('#popup_message');
                            const okBtn = document.querySelector('#popup_ok');
                            return {
                                kind: String(al.getAttribute('AlertType') || 'inform').toLowerCase(),
                                msg: (pm ? pm.innerText : al.innerText || '').trim(),
                                has_ok: Boolean(okBtn)
                            };
                        }
                        return null;
                    }"""
                )
            except Exception:
                # Frame may detach cleanly right as submitForm.aspx closes the window
                if hasattr(form_page, "is_closed") and form_page.is_closed():
                    confirm_state = "WINDOW_CLOSED_AFTER_TEMPSAVE"
                    break

        if isinstance(alert_state, dict):
            kind = str(alert_state.get("kind") or "")
            msg = redact_portal_sensitive(alert_state.get("msg") or "")
            if kind == "error":
                raise RuntimeError(f"[D107-2b] Save temp. returned error dialog on Form.aspx: {msg}")
            if kind == "confirm" and alert_state.get("has_ok"):
                try:
                    form_frame.locator("#popup_ok").first.click(timeout=5000, no_wait_after=True)
                except Exception:
                    pass
            elif kind in ("inform", "warning"):
                confirm_state = f"ALERT_{kind.upper()}: {msg}" if msg else f"ALERT_{kind.upper()}"
                if save_shot_path is not None and hasattr(form_page, "screenshot"):
                    try:
                        form_page.screenshot(path=str(save_shot_path), full_page=True)
                    except Exception:
                        pass
                if alert_state.get("has_ok"):
                    try:
                        form_frame.locator("#popup_ok").first.click(timeout=5000, no_wait_after=True)
                    except Exception:
                        pass
                break

        if submit_form_http_status:
            code = submit_form_http_status[-1]
            if code >= 400:
                raise RuntimeError(f"[D107-2b] submitForm.aspx returned HTTP {code} on TEMPSAVE.")
            if poll_interval_s > 0:
                time.sleep(min(0.5, poll_interval_s))
            if save_shot_path is not None and hasattr(form_page, "screenshot"):
                try:
                    if not (hasattr(form_page, "is_closed") and form_page.is_closed()):
                        form_page.screenshot(path=str(save_shot_path), full_page=True)
                except Exception:
                    pass
            confirm_state = f"SUBMIT_FORM_HTTP_{code}"
            break

        if poll_idx + 1 < max_polls and poll_interval_s > 0:
            time.sleep(poll_interval_s)

    if confirm_state == "PENDING":
        confirm_state = "BT_SAVE_CLICKED"
        if save_shot_path is not None and hasattr(form_page, "screenshot"):
            try:
                if not (hasattr(form_page, "is_closed") and form_page.is_closed()):
                    form_page.screenshot(path=str(save_shot_path), full_page=True)
            except Exception:
                pass

    if not (hasattr(form_page, "is_closed") and form_page.is_closed()):
        if hasattr(form_page, "goto"):
            try:
                form_page.goto("about:blank")
            except Exception:
                pass

    return {
        "temp_saved": True,
        "draft_subject": draft_subject,
        "confirm_state": confirm_state,
        "submit_form_http_status": submit_form_http_status[-1] if submit_form_http_status else None,
        "screenshot_path": str(save_shot_path) if save_shot_path else None,
    }


def verify_and_process_form_batch(
    form_page: Any,
    batch_rows: Sequence[Dict[str, Any]],
    report_date_str: str,
    mode: str,
    reason_text: str,
    attachment_path: Optional[str] = None,
    batch_idx: int = 1,
    total_batches: int = 1,
    save_shot_path: Optional[Path] = None,
    saved_at: Optional[str] = None,
    db_path: Optional[Path] = None,
    overwrite_stale: bool = False,
    extracted_form_rows: Optional[Sequence[Dict[str, Any]]] = None,
    poll_interval_s: float = 0.5,
    max_wait_s: float = 15.0,
) -> Dict[str, Any]:
    """
    Verifies 100% 7-field match on `WF_FORM_ST_78` BEFORE any form action:
      - If even 1 field mismatches, raises `PortalFormRowMismatchError` immediately (0 clicks, 0 DB rows).
      - In `dry_run_2a`: closes form (`about:blank`) with 0 clicks and 0 DB writes.
      - In `save_temp_2b`: clicks `#btSave` (`Save temp.`) ONLY and records rows in `d107_drafts`.
    """
    if mode not in ALLOWED_MODES:
        raise PermissionError(f"[D107] Unauthorized mode '{mode}'.")

    form_rows = (
        list(extracted_form_rows)
        if extracted_form_rows is not None
        else extract_wf_form_st_78_rows(form_page)
    )
    if not form_rows:
        raise PortalSchemaMismatchError(
            "[D107] Form.aspx opened but item table rows could not be extracted."
        )

    # Strict 7-field gate (#233): raises PortalFormRowMismatchError before any click or DB write!
    verify_form_rows_match(batch_rows, form_rows)

    if mode == "dry_run_2a":
        if save_shot_path is not None and hasattr(form_page, "screenshot"):
            try:
                form_page.screenshot(path=str(save_shot_path), full_page=True)
            except Exception:
                pass
        if hasattr(form_page, "goto") and not (hasattr(form_page, "is_closed") and form_page.is_closed()):
            form_page.goto("about:blank")
        return {
            "batch_index": batch_idx,
            "row_count": len(batch_rows),
            "form_rows_matched": True,
            "temp_saved": False,
            "draft_subject": None,
            "confirm_state": "DRY_RUN_NO_SAVE",
            "db_inserted_count": 0,
            "verified_form_rows": form_rows,
            "screenshot_path": str(save_shot_path) if save_shot_path else None,
        }

    draft_subject = format_draft_subject_en(
        report_date_str=report_date_str,
        batch_idx=batch_idx,
        total_batches=total_batches,
    )
    save_info = execute_save_temp_on_form(
        form_page=form_page,
        draft_subject=draft_subject,
        reason_text=reason_text,
        attachment_path=attachment_path,
        mode=mode,
        save_shot_path=save_shot_path,
        poll_interval_s=poll_interval_s,
        max_wait_s=max_wait_s,
    )
    rows_for_db = [{**r, "reason_line": format_single_row_reason_en(r)} for r in batch_rows]
    inserted = db_manager.record_d107_temp_draft_rows(
        rows=rows_for_db,
        draft_subject=draft_subject,
        draft_mode="save_temp_2b",
        saved_at=saved_at,
        db_path=db_path,
        overwrite_stale=overwrite_stale,
    )
    return {
        "batch_index": batch_idx,
        "row_count": len(batch_rows),
        "form_rows_matched": True,
        "temp_saved": True,
        "draft_subject": draft_subject,
        "confirm_state": save_info["confirm_state"],
        "db_inserted_count": inserted,
        "verified_form_rows": form_rows,
        "screenshot_path": save_info["screenshot_path"],
    }


def build_phase2a_test_email_html(run_result: Dict[str, Any]) -> Tuple[str, str]:
    """Renders 100% English `[TEST]` email summarizing Step 2a Dry-Run or Step 2b Save-Temp (D111 B-3 compliant)."""
    rep_date = str(run_result.get("report_date") or datetime.now().strftime("%Y-%m-%d"))
    mode = str(run_result.get("mode") or "dry_run_2a")
    is_2b = mode == "save_temp_2b"
    matched_cnt = len(run_result.get("matched_rows") or [])
    draft_cnt = int(run_result.get("draft_count") or 0)
    portal_tot = int(run_result.get("portal_total_rows") or 0)
    default_status = "SAVE_TEMP_2B_OK" if is_2b else "DRY_RUN_2A_OK"
    status_tag = str(run_result.get("status") or default_status)
    completed_drafts = int(run_result.get("completed_draft_count", run_result.get("form_verified_batches", 0)) or 0)
    saved_drafts = int(run_result.get("saved_temp_draft_count", completed_drafts if is_2b else 0) or 0)
    saved_rows = int(run_result.get("saved_temp_row_count", matched_cnt if (is_2b and saved_drafts > 0) else 0) or 0)
    deferred_drafts = int(run_result.get("deferred_draft_count", 0) or 0)
    deferred_rows = int(run_result.get("deferred_row_count", 0) or 0)

    draft_subj = str(run_result.get("draft_subject") or format_draft_subject_en(rep_date))
    prior_cohort = str(run_result.get("prior_cohort_date") or "prev-day")
    prior_cleared = int(run_result.get("prior_cleared_count", 0) or 0)
    prior_pending = int(run_result.get("prior_pending_count", 0) or 0)
    prior_stale = int(run_result.get("prior_stale_count", 0) or 0)
    prior_stale_labels = ", ".join(run_result.get("prior_stale_labels") or [])
    stale_suffix = (
        f" (incl. <strong>{prior_stale}</strong> <code>{prior_stale_labels or 'stale'}</code> &mdash; no auto re-save)"
        if prior_stale > 0
        else ""
    )

    if is_2b:
        subject = (
            f"[TEST] [D107 Phase 2b Save-Temp] GI Error Deletion Temp-Save — "
            f"{rep_date} ({saved_drafts} bundle(s) / {saved_rows} row(s) / {status_tag})"
        )
        banner_title = "[TEST] D107 Phase 2 (Step 2b Save-Temp) — Portal GI Deletion Temp-Save Report"
        mode_desc = "<code>save_temp_2b</code> (Save temp. ONLY; Submit strictly locked to user review)"
    else:
        subject = (
            f"[TEST] [D107 Phase 2a Dry-Run] GI Error Deletion Draft Verification — "
            f"{rep_date} ({matched_cnt} eligible / {draft_cnt} draft(s) / {status_tag})"
        )
        banner_title = "[TEST] D107 Phase 2 (Step 2a Dry-Run) — Portal GI Deletion Draft Verification"
        mode_desc = "<code>dry_run_2a</code> (Submit &amp; Save temp. strictly disabled)"

    matched_rows_html = ""
    for r in run_result.get("matched_rows") or []:
        matched_rows_html += (
            f"<tr>"
            f"<td>{r['post_date']}</td>"
            f"<td><code>{r['sumno']}</code></td>"
            f"<td><code>{r['barcode']}</code></td>"
            f"<td><strong>{r['material']}</strong></td>"
            f"<td>{r.get('material_desc', '')}</td>"
            f"<td style='text-align:center;'>{r['mvt']}</td>"
            f"<td style='text-align:center;'>{r['from_sloc']}&rarr;{r['to_sloc']}</td>"
            f"<td style='text-align:right;'>{float(r['qty'] or 0):,.0f}</td>"
            f"</tr>"
        )
    if not matched_rows_html:
        empty_msg = (
            "0 new prev-day D107 candidate rows temp-saved today (no new Temp Box draft created)."
            if is_2b
            else "0 eligible prev-day D107 candidate rows processed (no draft opened; zero portal rows touched)."
        )
        matched_rows_html = (
            f"<tr><td colspan='8' style='text-align:center; color:#64748b; padding:10px;'>{empty_msg}</td></tr>"
        )

    skipped_rows_html = ""
    for s in run_result.get("skipped_portal_rows") or []:
        skipped_rows_html += (
            f"<tr>"
            f"<td>{s['post_date']}</td>"
            f"<td><code>{s['sumno']}</code></td>"
            f"<td><strong>{s['material']}</strong></td>"
            f"<td>{s.get('material_desc', '')}</td>"
            f"<td style='text-align:center;'>{s['mvt']}</td>"
            f"<td style='text-align:center;'>{s['from_sloc']}&rarr;{s['to_sloc']}</td>"
            f"<td style='text-align:right;'>{float(s['qty'] or 0):,.0f}</td>"
            f"<td><code>{s.get('skip_reason', '')}</code></td>"
            f"</tr>"
        )
    if not skipped_rows_html:
        skipped_rows_html = (
            "<tr><td colspan='8' style='text-align:center; color:#64748b; padding:8px;'>None</td></tr>"
        )

    reason_preview = run_result.get("reason_preview") or "(No new rows temp-saved today)"
    gw_note = run_result.get("gw_session_note") or "Portal list verified via dedicated Playwright Edge profile."
    cutoff_line = (
        f"<li>Morning 07:29 Cutoff Status: Completed <strong>{completed_drafts}</strong> bundle(s); "
        f"Deferred by 07:29 cutoff: <strong>{deferred_drafts}</strong> bundle(s) (<strong>{deferred_rows}</strong> row(s))</li>"
    )
    temp_box_guide_html = (
        f"<div style='background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 6px; padding: 10px 14px; margin-bottom: 14px; color: #1e3a8a;'>"
        f"<strong>Temp Box Lookup (1-Line Guide):</strong> In Groupware e-Approval &rarr; <strong>Temp Box</strong>, "
        f"open title <code>{draft_subj}</code>, review the {saved_rows} row(s) &amp; English Reason, and click Submit manually.<br>"
        f"<strong>Prior Temp-Saved Reconciliation ({prior_cohort}):</strong> "
        f"<strong>{prior_cleared}</strong> row(s) cleared from Portal &amp; ZTCODE_STOCK_REC after user submit / "
        f"<strong>{prior_pending}</strong> row(s) still pending user submit{stale_suffix}."
        f"</div>"
    )

    sec1_label = (
        f"1. Temp-Saved D107 Deletion Draft Rows ({saved_rows} row(s) across {saved_drafts} bundle(s))"
        if is_2b
        else f"1. Eligible D107 Deletion Draft Rows ({matched_cnt})"
    )

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family: Segoe UI, Arial, sans-serif; font-size: 13px; color: #1e293b; line-height: 1.5;">
  <div style="max-width: 960px; margin: 0 auto; border: 1px solid #cbd5e1; border-radius: 8px; overflow: hidden;">
    <div style="background: #0f172a; color: #ffffff; padding: 14px 18px;">
      <div style="font-size: 16px; font-weight: 700;">{banner_title}</div>
      <div style="font-size: 12px; color: #cbd5e1; margin-top: 4px;">
        Report Date: {rep_date} &bull; Schedule: <code>07:23</code> (cutoff <code>07:29</code>) &bull; Mode: {mode_desc} &bull; Status: <strong>{status_tag}</strong>
      </div>
    </div>
    <div style="padding: 16px 18px;">
      {temp_box_guide_html}
      <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px 14px; margin-bottom: 14px;">
        <strong>Execution Summary:</strong>
        <ul style="margin: 6px 0 0 18px; padding: 0;">
          <li>Portal Live Rows Inspected: <strong>{portal_tot}</strong> (Cutoff: <code>POST DATE &le; {run_result.get('yesterday_ymd', '')}</code>)</li>
          <li>D107 Phase 1 Input Candidates: <strong>{run_result.get('d107_input_candidate_count', 0)}</strong> (Source: <code>{run_result.get('d107_source_file', 'N/A')}</code>)</li>
          <li>Latest 01_JOB Snapshot Re-Verification: <code>{run_result.get('latest_01_file', 'N/A')}</code> (Excluded resolved: <strong>{len(run_result.get('excluded_resolved_in_latest_01') or [])}</strong>)</li>
          <li>Eligible Rows Matched (SUMNO+BARCODE+PART+MVT+FROM/TO+QTY): <strong>{matched_cnt}</strong> &rarr; Bundle Count: <strong>{draft_cnt}</strong> (max {run_result.get('max_rows_per_draft', 20)} rows/draft)</li>
          <li>Temp-Saved Bundles Today: <strong>{saved_drafts}</strong> bundle(s) / <strong>{saved_rows}</strong> row(s) (Temp Box Title: <code>{draft_subj}</code>)</li>
          <li>Prior Temp-Saved Status ({prior_cohort}): <strong>{prior_cleared}</strong> cleared after user submit / <strong>{prior_pending}</strong> pending user submit</li>
          {cutoff_line}
          <li>Session &amp; Security Note (D96): {gw_note}</li>
        </ul>
      </div>

      <div style="font-weight: 700; margin: 12px 0 6px 0;">{sec1_label}</div>
      <table style="width: 100%; border-collapse: collapse; font-size: 12px;" border="1" cellpadding="6" cellspacing="0">
        <thead style="background: #f1f5f9;">
          <tr>
            <th>Post Date</th><th>SUMNO</th><th>Barcode</th><th>Material</th><th>Description</th><th>Mvt</th><th>Route</th><th>Qty</th>
          </tr>
        </thead>
        <tbody>{matched_rows_html}</tbody>
      </table>

      <div style="font-weight: 700; margin: 16px 0 6px 0;">2. Portal Rows Untouched / Excluded / Pending User Submit ({len(run_result.get('skipped_portal_rows') or [])})</div>
      <table style="width: 100%; border-collapse: collapse; font-size: 12px;" border="1" cellpadding="6" cellspacing="0">
        <thead style="background: #f1f5f9;">
          <tr>
            <th>Post Date</th><th>SUMNO</th><th>Material</th><th>Description</th><th>Mvt</th><th>Route</th><th>Qty</th><th>Guardrail / Pending Status</th>
          </tr>
        </thead>
        <tbody>{skipped_rows_html}</tbody>
      </table>

      <div style="font-weight: 700; margin: 16px 0 6px 0;">3. English Reason for Deletion</div>
      <pre style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px; padding: 10px; font-size: 11px; white-space: pre-wrap;">{reason_preview}</pre>
    </div>
  </div>
</body>
</html>"""

    assert_no_hangul({"subject": subject, "html_body": html})
    return subject, html


def send_d107_phase2a_test_email(run_result: Dict[str, Any]) -> bool:
    """
    AGENTS.md Rule 10 & D111: Sends Step 2a/2b report strictly to `TEST_EMAIL_RECIPIENT` ONLY
    (`CC = ''`, `BCC = ''`, `[TEST]` subject prefix, 0 Hangul verified).
    """
    test_to = os.getenv("TEST_EMAIL_RECIPIENT", "").strip()
    if not test_to or "@" not in test_to or "example.com" in test_to.lower():
        raise RuntimeError("[D107] Valid TEST_EMAIL_RECIPIENT is required in .env for [TEST] dispatch.")

    subject, html_body = build_phase2a_test_email_html(run_result)
    if not subject.startswith("[TEST]"):
        subject = f"[TEST] {subject}"

    att_list: List[Path] = []
    for extra_att in [run_result.get("attachment_path"), *(run_result.get("attachment_paths") or [])]:
        if extra_att and Path(extra_att).exists():
            p_obj = Path(extra_att).resolve()
            if p_obj not in att_list:
                att_list.append(p_obj)

    assert_no_hangul({"subject": subject, "html_body": html_body}, attachments=att_list)
    check_report_language(
        "01_JOB_d107_phase2_test",
        subject=subject,
        html_body=html_body,
        attachments=att_list,
        is_user_only=True,
    )

    res = subprocess.run(
        'tasklist /fi "imagename eq OUTLOOK.EXE"',
        shell=True,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if "OUTLOOK.EXE" not in res.stdout:
        subprocess.Popen("start outlook", shell=True, creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(4)

    import win32com.client

    outlook = win32com.client.Dispatch("Outlook.Application")
    mail = outlook.CreateItem(0)
    mail.Subject = subject
    mail.To = test_to
    mail.CC = ""
    mail.BCC = ""
    mail.HTMLBody = html_body
    for p_att in att_list:
        mail.Attachments.Add(str(p_att))

    attached_shots: List[Path] = []
    for sp in [*(run_result.get("all_screenshot_paths") or []), run_result.get("portal_screenshot_path")]:
        if sp and Path(sp).exists():
            sp_res = Path(sp).resolve()
            if sp_res not in attached_shots:
                attached_shots.append(sp_res)
                mail.Attachments.Add(str(sp_res))

    mail.Send()
    logger.info("[D107] Dispatched user-only [TEST] email via TEST_EMAIL_RECIPIENT.")
    return True


def _wait_for_wf_form_st_78(gi_page: Any, ctx: Any, pages_before: int, max_wait_s: int = 40) -> Any:
    """Polls until `Form.aspx` (`WF_FORM_ST_78`) renders its item table or auth/error page appears."""
    form_page = gi_page
    for _ in range(max_wait_s):
        time.sleep(1.0)
        if len(ctx.pages) > pages_before:
            form_page = ctx.pages[-1]
        frame_paths = [urlsplit(f.url).path.lower() for f in form_page.frames]
        if any("errorpage" in fp or "login.aspx" in fp for fp in frame_paths):
            return form_page
        if any("form.aspx" in fp for fp in frame_paths):
            rows = extract_wf_form_st_78_rows(form_page)
            if rows:
                time.sleep(1.5)
                return form_page
    return form_page


def run_d107_phase2a_dry_run(
    report_date: Optional[str] = None,
    mode: str = "dry_run_2a",
    send_test_email: bool = False,
    allow_any_minute: bool = False,
    require_today_phase1: bool = True,
    cutoff_dt: Optional[datetime] = None,
    allow_gw_login_once: bool = False,
    resolution_logs_dir: Optional[Path] = None,
    gi_errors_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
    force_resave_stale: bool = False,
    now_fn: Optional[Callable[[], datetime]] = None,
) -> Dict[str, Any]:
    """
    Executes D107 Phase 2 (`dry_run_2a` or `save_temp_2b`) via `CORP_PORTAL_URL` SSO bridge -> `WMS_PORTAL_GI_LIST_URL`.
    Hard-blocks `submit_2c` and any other unauthorized mode (`Submit` `#btDraft` is NEVER clicked).
    """
    assert_not_submit_action(mode)
    if mode not in ALLOWED_MODES:
        raise PermissionError(
            f"[D107] Mode '{mode}' is hard-disabled. Authorized modes: {sorted(ALLOWED_MODES)} (Submit remains locked)."
        )

    clock = now_fn or datetime.now
    start_now = clock()
    check_safe_portal_window(now=start_now, allow_any_minute=allow_any_minute)

    rep_date_str = report_date or start_now.strftime("%Y-%m-%d")
    yesterday_ymd = (datetime.strptime(rep_date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y%m%d")
    eff_db_path = Path(db_path) if db_path is not None else db_manager.DB_PATH
    base_draft_subject = format_draft_subject_en(rep_date_str)
    draft_summary = db_manager.get_d107_draft_status_summary(report_date_str=rep_date_str, db_path=eff_db_path)

    today_phase1_path = get_today_d107_phase1_path(rep_date_str, resolution_logs_dir=resolution_logs_dir)
    if require_today_phase1 and not today_phase1_path.exists():
        skip_note = (
            f"Skipped before opening portal: today's D107 Phase 1 verdict file ({today_phase1_path.name}) "
            "was not found (07:18 collect has not completed yet)."
        )
        logger.warning("[D107] %s", skip_note)
        skipped_res: Dict[str, Any] = {
            "status": "SKIPPED_TODAY_PHASE1_MISSING",
            "mode": mode,
            "report_date": rep_date_str,
            "yesterday_ymd": yesterday_ymd,
            "portal_total_rows": 0,
            "d107_input_candidate_count": 0,
            "d107_verified_still_open_count": 0,
            "excluded_resolved_in_latest_01": [],
            "matched_rows": [],
            "skipped_portal_rows": [],
            "pending_user_submit_rows": [],
            "max_rows_per_draft": PORTAL_MAX_ROWS_PER_DRAFT,
            "draft_count": 0,
            "draft_batches": [],
            "completed_draft_count": 0,
            "saved_temp_draft_count": 0,
            "saved_temp_row_count": int(draft_summary.get("today_saved_count", 0) or 0),
            "draft_subject": base_draft_subject,
            "prior_cohort_date": draft_summary.get("prior_cohort_date"),
            "prior_cleared_count": int(draft_summary.get("prior_cleared_count", 0) or 0),
            "prior_pending_count": int(draft_summary.get("prior_pending_count", 0) or 0),
            "deferred_draft_count": 0,
            "deferred_row_count": 0,
            "d107_source_file": "MISSING_TODAY_PHASE1",
            "latest_01_file": "SKIPPED",
            "reason_preview": "",
            "overflow_used": False,
            "attachment_path": None,
            "attachment_paths": [],
            "portal_screenshot_path": None,
            "all_screenshot_paths": [],
            "form_verified_batches": 0,
            "gw_session_note": skip_note,
            "test_email_sent": False,
        }
        if send_test_email:
            send_d107_phase2a_test_email(skipped_res)
            skipped_res["test_email_sent"] = True
        return skipped_res

    BINSYNTH9941_url = os.getenv("CORP_PORTAL_URL", "").strip()
    portal_url = os.getenv("WMS_PORTAL_GI_LIST_URL", "").strip()
    wms_user = os.getenv("WMS_USERNAME", "").strip()
    wms_pw = os.getenv("WMS_PASSWORD", "").strip()
    if not BINSYNTH9941_url or not portal_url or not wms_user or not wms_pw:
        raise RuntimeError(
            "[D107] Missing required .env keys among (CORP_PORTAL_URL, WMS_PORTAL_GI_LIST_URL, WMS_USERNAME, WMS_PASSWORD)."
        )

    effective_cutoff_dt = resolve_batch_cutoff_dt(now=start_now, cutoff_dt=cutoff_dt)
    phase1_payload, phase1_file = load_latest_d107_phase1_payload(rep_date_str, resolution_logs_dir=resolution_logs_dir)
    latest_01_recs, latest_01_name = load_latest_01_gi_records(gi_errors_dir=gi_errors_dir)

    profile_dir = JOB_DIR / ".pw_d107_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    halt_flag_path = JOB_DIR / GW_LOGIN_HALT_FLAG_NAME
    shots_dir = JOB_DIR / "scratch" / "d107_screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    lock_file = JOB_DIR / ".pw_d107_browser.lock"
    if lock_file.exists():
        age_s = time.time() - lock_file.stat().st_mtime
        if age_s < 600:
            raise RuntimeError("[D107] Another D107 browser instance lock is active — max 1 browser enforced.")
    lock_file.write_text(str(os.getpid()), encoding="utf-8")

    # #226: Direct credential entry is hard-disabled in operational schedule (`allow_gw_login_once = False`).
    allow_gw_login_once = False

    from playwright.sync_api import sync_playwright
    import shutil

    status_tag = "SAVE_TEMP_2B_OK" if mode == "save_temp_2b" else "DRY_RUN_2A_OK"
    completed_draft_count = 0
    saved_temp_draft_count = 0
    saved_temp_row_count = 0
    deferred_draft_count = 0
    deferred_row_count = 0
    form_verified_batches = 0
    newly_cleared_count = 0
    mismatch_abort_err: Optional[PortalFormRowMismatchError] = None
    batch_verifications: List[Dict[str, Any]] = []
    all_screenshot_paths: List[str] = []
    attachment_paths: List[str] = []
    bundle: Dict[str, Any] = {
        "report_date": rep_date_str,
        "yesterday_ymd": yesterday_ymd,
        "portal_total_rows": 0,
        "d107_input_candidate_count": len(phase1_payload.get("candidates", [])),
        "d107_verified_still_open_count": 0,
        "excluded_resolved_in_latest_01": [],
        "matched_rows": [],
        "skipped_portal_rows": [],
        "pending_user_submit_rows": [],
        "max_rows_per_draft": PORTAL_MAX_ROWS_PER_DRAFT,
        "draft_count": 0,
        "draft_batches": [],
    }
    reason_pack: Dict[str, Any] = {
        "reason_text": "",
        "overflow_used": False,
        "attachment_path": None,
        "per_row_lines": [],
    }
    list_shot: Optional[Path] = None
    gw_note = ""
    sso_bridge_mode = "none"

    try:
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="msedge",
                headless=True,
                viewport={"width": 1600, "height": 1100},
            )
            try:
                sso_info = ensure_BINSYNTH9941_and_groupware_sso(
                    ctx=ctx,
                    BINSYNTH9941_url=BINSYNTH9941_url,
                    wms_user=wms_user,
                    wms_pw=wms_pw,
                    allow_login_once=False,
                    halt_flag_path=halt_flag_path,
                    cutoff_dt=effective_cutoff_dt,
                    now_fn=clock,
                )
                gw_note = sso_info["note"]
                sso_bridge_mode = str(sso_info.get("bridge_mode") or "none")

                if not sso_info["portal_ready"]:
                    status_tag = str(
                        sso_info.get("status_code")
                        or (
                            "SKIPPED_PORTAL_NOT_READY"
                            if sso_info.get("failure_reason") == "PORTAL_NOT_READY"
                            else "SKIPPED_SSO_UNAVAILABLE"
                        )
                    )
                    ts_tag = clock().strftime("%Y%m%d_%H%M%S")
                    shot_prefix = (
                        "D107_PortalNotReady"
                        if status_tag == "SKIPPED_PORTAL_NOT_READY"
                        else "D107_SSOUnavailable"
                    )
                    sso_shot = shots_dir / f"{shot_prefix}_{ts_tag}.png"
                    if ctx.pages:
                        ctx.pages[0].screenshot(path=str(sso_shot), full_page=False)
                        list_shot = sso_shot
                        all_screenshot_paths.append(str(sso_shot))
                    logger.warning("[D107] %s", gw_note)
                else:
                    gi_page = ctx.new_page()
                    gi_page.on("dialog", lambda d: d.accept())
                    gi_page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
                    for _ in range(15):
                        if gi_page.locator("#excelTable").count() > 0:
                            break
                        time.sleep(1.0)
                    time.sleep(1.5)

                    tbl_data = gi_page.evaluate(
                        """() => ({
                            hdrs: Array.from(document.querySelectorAll('#excelTable thead th')).map(th => th.innerText.trim()),
                            rows: Array.from(document.querySelectorAll('#excelTable tbody tr')).map(tr =>
                                Array.from(tr.cells).map(td => td.innerText.trim())
                            )
                        })"""
                    )

                    # Reconcile prior SAVED_TEMP rows against BOTH live portal & latest 01 ZTCODE_STOCK_REC (#233)
                    live_portal_keys = extract_all_portal_row_keys(tbl_data["hdrs"], tbl_data["rows"])
                    reconciled = db_manager.reconcile_d107_temp_drafts(
                        portal_row_keys=live_portal_keys,
                        latest_01_gi_records=latest_01_recs if latest_01_name else None,
                        report_date_str=rep_date_str,
                        checked_at=clock().strftime("%Y-%m-%d %H:%M:%S"),
                        db_path=eff_db_path,
                    )
                    newly_cleared_count = int(reconciled.get("newly_cleared_count", 0) or 0)
                    pending_temp_map = db_manager.get_pending_d107_temp_drafts(db_path=eff_db_path)

                    bundle = filter_and_bundle_portal_rows(
                        portal_headers=tbl_data["hdrs"],
                        portal_rows=tbl_data["rows"],
                        d107_candidates=phase1_payload.get("candidates", []),
                        latest_01_gi_records=latest_01_recs if latest_01_name else None,
                        report_date_str=rep_date_str,
                        pending_temp_drafts=pending_temp_map,
                        force_resave_stale=force_resave_stale,
                    )

                    ts_tag = clock().strftime("%Y%m%d_%H%M%S")
                    mode_tag = "2b" if mode == "save_temp_2b" else "2a"
                    list_shot = shots_dir / f"D107_{mode_tag}_PortalList_{ts_tag}.png"
                    gi_page.screenshot(path=str(list_shot), full_page=False)
                    all_screenshot_paths.append(str(list_shot))

                    reason_pack = build_deletion_reason_and_attachment(
                        matched_rows=bundle["matched_rows"],
                        report_date_str=rep_date_str,
                        out_dir=JOB_DIR / "scratch",
                    )
                    if reason_pack.get("attachment_path"):
                        attachment_paths.append(str(reason_pack["attachment_path"]))

                    total_batches = len(bundle["draft_batches"])
                    for b_idx, batch_rows in enumerate(bundle["draft_batches"], start=1):
                        if is_past_batch_cutoff(clock(), effective_cutoff_dt):
                            deferred_batches = bundle["draft_batches"][b_idx - 1:]
                            deferred_draft_count = len(deferred_batches)
                            deferred_row_count = sum(len(b) for b in deferred_batches)
                            status_tag = "PARTIAL_CUTOFF_0729"
                            gw_note = (
                                f"Stopped at 07:29 cutoff before 07:30 01_JOB: completed {completed_draft_count}/{total_batches} "
                                f"draft(s); deferred {deferred_draft_count} draft(s) ({deferred_row_count} row(s)) to next day's pool."
                            )
                            logger.warning("[D107] %s", gw_note)
                            break

                        if b_idx > 1:
                            gi_page.goto(portal_url, wait_until="domcontentloaded", timeout=30000)
                            for _ in range(15):
                                if gi_page.locator("#excelTable").count() > 0:
                                    break
                                time.sleep(1.0)
                            time.sleep(1.5)

                        for r in batch_rows:
                            r_idx = int(r["portal_row_index"])
                            gi_page.locator("#excelTable tbody tr").nth(r_idx).locator("td.select-checkbox").click()
                            time.sleep(0.5)
                        time.sleep(1.0)
                        gi_page.locator("#approval").click()
                        time.sleep(1.0)
                        pages_before = len(ctx.pages)
                        gi_page.locator("#approvalConfirm").click(no_wait_after=True)

                        form_page = _wait_for_wf_form_st_78(gi_page, ctx, pages_before, max_wait_s=40)
                        frame_paths = [urlsplit(f.url).path.lower() for f in form_page.frames]

                        form_shot = shots_dir / f"D107_{mode_tag}_FormBatch{b_idx}_{ts_tag}.png"
                        form_page.screenshot(path=str(form_shot), full_page=True)
                        list_shot = form_shot
                        all_screenshot_paths.append(str(form_shot))

                        if any("errorpage" in fp or "login.aspx" in fp for fp in frame_paths):
                            status_tag = "SKIPPED_SSO_UNAVAILABLE"
                            gw_note = (
                                f"Selected {len(batch_rows)} eligible row(s), clicked Approval for Delete -> OK, "
                                "and approval frame returned Groupware auth/error page; stopped without input (SKIPPED_SSO_UNAVAILABLE)."
                            )
                            break

                        batch_reason_pack = (
                            reason_pack
                            if total_batches == 1
                            else build_deletion_reason_and_attachment(
                                matched_rows=batch_rows,
                                report_date_str=rep_date_str,
                                out_dir=JOB_DIR / "scratch",
                            )
                        )
                        if batch_reason_pack.get("attachment_path") and str(batch_reason_pack["attachment_path"]) not in attachment_paths:
                            attachment_paths.append(str(batch_reason_pack["attachment_path"]))

                        try:
                            b_res = verify_and_process_form_batch(
                                form_page=form_page,
                                batch_rows=batch_rows,
                                report_date_str=rep_date_str,
                                mode=mode,
                                reason_text=batch_reason_pack["reason_text"],
                                attachment_path=batch_reason_pack.get("attachment_path"),
                                batch_idx=b_idx,
                                total_batches=total_batches,
                                save_shot_path=form_shot,
                                saved_at=clock().strftime("%Y-%m-%d %H:%M:%S"),
                                db_path=eff_db_path,
                                overwrite_stale=force_resave_stale,
                            )
                        except PortalFormRowMismatchError as mm_exc:
                            status_tag = "ABORTED_FORM_MISMATCH"
                            clean_mm = redact_portal_sensitive(mm_exc)
                            gw_note = (
                                f"ABORTED ON 7-FIELD FORM MISMATCH in batch {b_idx}/{total_batches}: {clean_mm} "
                                "(0 rows saved to Temp Box; 0 rows recorded in d107_drafts)."
                            )
                            logger.error("[D107] %s", gw_note)
                            mismatch_abort_err = PortalFormRowMismatchError(clean_mm)
                            if not (hasattr(form_page, "is_closed") and form_page.is_closed()):
                                try:
                                    form_page.goto("about:blank")
                                except Exception:
                                    pass
                            break

                        form_verified_batches += 1
                        completed_draft_count += 1
                        if b_res.get("temp_saved"):
                            saved_temp_draft_count += 1
                            saved_temp_row_count += len(batch_rows)
                        batch_verifications.append(b_res)

                for pg in list(ctx.pages):
                    try:
                        if not (hasattr(pg, "is_closed") and pg.is_closed()):
                            pg.goto("about:blank")
                    except Exception:
                        pass
            finally:
                ctx.close()
    except Exception as exc:
        raise RuntimeError(redact_portal_sensitive(exc)) from None
    finally:
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass

    end_now = clock()
    finished_before_0729 = (not is_past_batch_cutoff(end_now, effective_cutoff_dt)) if effective_cutoff_dt else True
    final_draft_summary = db_manager.get_d107_draft_status_summary(
        report_date_str=rep_date_str,
        newly_cleared_count=newly_cleared_count,
        db_path=eff_db_path,
    )

    result: Dict[str, Any] = {
        **bundle,
        "status": status_tag,
        "mode": mode,
        "started_at": start_now.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_at": end_now.strftime("%Y-%m-%d %H:%M:%S"),
        "finished_before_0729_cutoff": finished_before_0729,
        "completed_draft_count": completed_draft_count,
        "saved_temp_draft_count": saved_temp_draft_count,
        "saved_temp_row_count": saved_temp_row_count,
        "draft_subject": base_draft_subject,
        "prior_cohort_date": final_draft_summary.get("prior_cohort_date"),
        "prior_cleared_count": int(final_draft_summary.get("prior_cleared_count", 0) or 0),
        "prior_pending_count": int(final_draft_summary.get("prior_pending_count", 0) or 0),
        "prior_stale_count": int(final_draft_summary.get("prior_stale_count", 0) or 0),
        "prior_stale_labels": list(final_draft_summary.get("prior_stale_labels") or []),
        "newly_cleared_count": newly_cleared_count,
        "deferred_draft_count": deferred_draft_count,
        "deferred_row_count": deferred_row_count,
        "batch_verifications": batch_verifications,
        "all_screenshot_paths": all_screenshot_paths,
        "d107_source_file": phase1_file.name if phase1_file else "NONE",
        "latest_01_file": latest_01_name or "NONE",
        "reason_preview": reason_pack["reason_text"],
        "overflow_used": reason_pack["overflow_used"],
        "attachment_path": reason_pack["attachment_path"],
        "attachment_paths": attachment_paths,
        "portal_screenshot_path": str(list_shot) if list_shot else None,
        "form_verified_batches": form_verified_batches,
        "bridge_mode": sso_bridge_mode,
        "gw_session_note": gw_note,
    }

    # Persist sanitized JSON run log (`AGENTS.md` Rule 7: timestamped + canonical daily copy)
    logs_out_dir = Path(resolution_logs_dir) if resolution_logs_dir else (JOB_DIR / "resolution_logs")
    logs_out_dir.mkdir(parents=True, exist_ok=True)
    ymd_tag = rep_date_str.replace("-", "")
    run_ts_tag = end_now.strftime("%Y%m%d_%H%M%S")
    ts_json_path = logs_out_dir / f"d107_phase2a_{run_ts_tag}.json"
    canon_json_path = logs_out_dir / f"d107_phase2a_{ymd_tag}.json"
    sanitized_json = redact_portal_sensitive(json.dumps(result, ensure_ascii=False, indent=2))
    ts_json_path.write_text(sanitized_json, encoding="utf-8")
    shutil.copyfile(ts_json_path, canon_json_path)

    if send_test_email:
        send_d107_phase2a_test_email(result)
        result["test_email_sent"] = True
    else:
        result["test_email_sent"] = False

    if mismatch_abort_err is not None:
        raise mismatch_abort_err

    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser(description="D107 Phase 2 (Step 2a Dry-Run & Step 2b Save-Temp)")
    ap.add_argument("--report-date", default=None, help="YYYY-MM-DD (defaults to today)")
    ap.add_argument(
        "--mode",
        choices=sorted(ALLOWED_MODES),
        default=None,
        help="Execution mode: dry_run_2a or save_temp_2b (Submit remains hard-locked)",
    )
    ap.add_argument("--dry-run-2a", action="store_true", help="Alias for --mode dry_run_2a")
    ap.add_argument("--save-temp-2b", action="store_true", help="Alias for --mode save_temp_2b")
    ap.add_argument("--send-test-email", action="store_true", help="Send user-only [TEST] English email")
    ap.add_argument(
        "--no-require-today-phase1",
        action="store_true",
        help="Allow fallback to earlier d107_phase1_*.json (default requires today's 07:18 file)",
    )
    ap.add_argument("--resolution-logs-dir", default=None, help="Optional path to resolution_logs dir")
    ap.add_argument("--gi-errors-dir", default=None, help="Optional path to gi_errors dir")
    ap.add_argument("--db-path", default=None, help="Optional path to gr_gi_history.db")
    ap.add_argument(
        "--force-resave-stale",
        action="store_true",
        help="Manual-only flag to re-save SAVED_TEMP rows older than 2 business days (default: never auto re-save)",
    )
    args = ap.parse_args()

    resolved_mode = args.mode
    if resolved_mode is None:
        if args.save_temp_2b:
            resolved_mode = "save_temp_2b"
        elif args.dry_run_2a:
            resolved_mode = "dry_run_2a"
        else:
            raise SystemExit("Pass --mode {dry_run_2a,save_temp_2b} (or --dry-run-2a / --save-temp-2b).")

    out = run_d107_phase2a_dry_run(
        report_date=args.report_date,
        mode=resolved_mode,
        send_test_email=args.send_test_email,
        require_today_phase1=not args.no_require_today_phase1,
        allow_gw_login_once=False,
        resolution_logs_dir=Path(args.resolution_logs_dir) if args.resolution_logs_dir else None,
        gi_errors_dir=Path(args.gi_errors_dir) if args.gi_errors_dir else None,
        db_path=Path(args.db_path) if args.db_path else None,
        force_resave_stale=args.force_resave_stale,
    )
    safe_summary = {
        "status": out["status"],
        "mode": out["mode"],
        "bridge_mode": out.get("bridge_mode", "none"),
        "report_date": out["report_date"],
        "yesterday_ymd": out["yesterday_ymd"],
        "portal_total_rows": out["portal_total_rows"],
        "matched_count": len(out["matched_rows"]),
        "draft_count": out["draft_count"],
        "completed_draft_count": out["completed_draft_count"],
        "saved_temp_draft_count": out.get("saved_temp_draft_count", 0),
        "saved_temp_row_count": out.get("saved_temp_row_count", 0),
        "prior_cleared_count": out.get("prior_cleared_count", 0),
        "prior_pending_count": out.get("prior_pending_count", 0),
        "deferred_draft_count": out["deferred_draft_count"],
        "finished_before_0729_cutoff": out.get("finished_before_0729_cutoff", True),
        "skipped_count": len(out["skipped_portal_rows"]),
        "test_email_sent": out["test_email_sent"],
    }
    print(json.dumps(safe_summary, indent=2))
