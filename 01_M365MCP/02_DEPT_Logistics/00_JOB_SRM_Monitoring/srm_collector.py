"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - SRM export download (Playwright; portal PC SSO, SRM .env account)
=============================================================================================================
Same clicks as the user's recording (11/12), for one delivery date per output folder:
  Sequence Order Plan    (TSTA005): Delivery Date -> search -> tab 'Plan & Require' -> Export to Excel file,
                                    tab 'Order Detail' -> Export to Excel file
  Sequence Order Require (TSTA006): Delivery Date -> search -> tab 'Require Detail' -> Export to Excel file
Saved as '<Order Detail|Require Detail|Plan & Require> <YYYYMMDD>.xlsx'; every file is parsed right away and its first
date must equal the delivery date (otherwise it is moved to 'rejected/' and the run fails).

Safety
  * own Edge profile (.pw_srm_profile) and lock (.pw_srm_browser.lock) - separate from D107 (.pw_d107_*)
  * portal opened with PC SSO only - zero credential input: a visible portal login form stops the run (SSO_UNAVAILABLE)
  * SRM's own login form (user 11/12 20:27: SRM uses the WMS account): ONE try per run with the .env account
    (SRM_USERNAME / SRM_PASSWORD, else WMS_USERNAME / WMS_PASSWORD; values never logged, screenshotted or dumped).
    A failed try writes logs/srm_login_blocked.json and every later run stops before typing (LOGIN_BLOCKED), so the
    5-minute retries can never lock the shared WMS account - check .env, then delete that file to resume.
  * read-only screens: no save / submit button is ever clicked; JS dialogs are accepted (export confirmations)
  * addresses only from .env (SRM_PORTAL_URL, optional SRM_BASE_URL); logs / probe files show URL paths only
Screen texts / selectors: config/srm_collector.json.

  python srm_collector.py --probe [--headed]               # walk the screens for today, screenshots + element list
  python srm_collector.py --date 2026-11-13 --out "inbox/20261112/D+1"
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))
CONFIG = BASE_DIR / "config" / "srm_collector.json"
PROFILE_DIR = BASE_DIR / ".pw_srm_profile"
LOCK_FILE = BASE_DIR / ".pw_srm_browser.lock"
LOCK_STALE_S = 20 * 60

from srm_parsers import parse_hourly, parse_plan_require, read_rows  # noqa: E402
from srm_projection import md  # noqa: E402

KIND_FILE = {"order": "Order Detail", "require": "Require Detail", "planreq": "Plan & Require"}
LOGIN_FORM = '#txtPC_LoginPW:visible, input[type="password"]:visible'
LOGIN_BLOCK = BASE_DIR / "logs" / "srm_login_blocked.json"     # written after a failed SRM login; blocks later tries


def srm_credentials() -> Tuple[str, str, str]:
    """(user, password, key prefix) for SRM's own login form: SRM_USERNAME / SRM_PASSWORD when both are set, else
    the WMS account (user 11/12 20:27: same ID / PW as WMS). Empty strings when neither pair is complete."""
    for prefix in ("SRM", "WMS"):
        user, pw = os.getenv(f"{prefix}_USERNAME", "").strip(), os.getenv(f"{prefix}_PASSWORD", "")
        if user and pw:
            return user, pw, prefix
    return "", "", ""


def login_blocked() -> Optional[Dict[str, Any]]:
    """Content of logs/srm_login_blocked.json (no secrets in it) or None."""
    if not LOGIN_BLOCK.exists():
        return None
    try:
        return json.loads(LOGIN_BLOCK.read_text(encoding="utf-8"))
    except Exception:
        return {"at": "?", "reason": "unreadable block file"}


def block_login(prefix: str, reason: str) -> None:
    LOGIN_BLOCK.parent.mkdir(parents=True, exist_ok=True)
    LOGIN_BLOCK.write_text(json.dumps({"at": f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}", "account": f"{prefix}_USERNAME",
                                       "reason": reason, "resume": "check the .env account, then delete this file"},
                                      ensure_ascii=False, indent=1), encoding="utf-8")


class CollectError(RuntimeError):
    def __init__(self, status: str, detail: str):
        super().__init__(f"{status}: {detail}")
        self.status, self.detail = status, detail


# ---------------------------------------------------------------- pure helpers (unit-tested)
def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    return json.loads((path or CONFIG).read_text(encoding="utf-8-sig"))


def redact(url: str) -> str:
    """URL path only (no scheme / host / query) for logs and probe files."""
    return urlsplit(url or "").path or "/"


def target_name(kind: str, delivery: dt.date) -> str:
    return f"{KIND_FILE[kind]} {delivery:%Y%m%d}.xlsx"


def check_export(kind: str, path: Path, delivery: dt.date) -> Dict[str, Any]:
    """{'ok', 'status', 'first_date', 'parts', 'total'}: the export must parse, start on the delivery date and hold
    part rows with quantities (user 11/12: not retrievable / empty file / empty data -> retry)."""
    try:
        rows = read_rows(path)
        if kind == "planreq":
            labels, recs = parse_plan_require(rows)
            total = sum(sum(r["require"]) + sum(r["plan"]) for r in recs.values())
        else:
            labels, recs = parse_hourly(rows)
            total = sum(sum(r["slots"]) + r["add"] for r in recs.values())
    except Exception as exc:  # not an SRM export of this kind (no grid, error page, other tab)
        return {"ok": False, "status": "WRONG_FILE", "first_date": "",
                "detail": f"unreadable ({type(exc).__name__}: {str(exc)[:80]})"}
    first = (labels[0] if labels else "").replace(" SUM", "").strip()
    out = {"first_date": first, "parts": len(recs), "total": total}
    if first != md(delivery):
        return {**out, "ok": False, "status": "WRONG_FILE", "detail": f"first date {first}, wanted {md(delivery)}"}
    if not recs:
        return {**out, "ok": False, "status": "EMPTY_FILE", "detail": "no part rows"}
    if total <= 0:
        return {**out, "ok": False, "status": "EMPTY_FILE", "detail": f"{len(recs)} parts but no quantities"}
    return {**out, "ok": True, "status": "OK", "detail": ""}


def validate_download(kind: str, path: Path, delivery: dt.date) -> Tuple[bool, str]:
    """(ok, first date) when the date check decides, otherwise (False, reason)."""
    c = check_export(kind, path, delivery)
    return c["ok"], (c["first_date"] if c["ok"] or c["detail"].startswith("first date") else c["detail"])


def acquire_lock() -> None:
    if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < LOCK_STALE_S:
        raise CollectError("LOCKED", "another SRM browser run is active (.pw_srm_browser.lock)")
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")


def release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- browser session
_INVENTORY_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const pick = (sel, n) => Array.from(document.querySelectorAll(sel)).filter(vis).slice(0, n);
  const txt = e => ((e.innerText || e.value || e.title || e.getAttribute('aria-label') || '') + '')
                   .trim().replace(/\s+/g, ' ').slice(0, 50);
  const dateLike = v => /^\d{4}[-./]\d{1,2}[-./]\d{1,2}$|^\d{1,2}[-./]\d{1,2}[-./]\d{4}$/.test(v || '');
  const secret = e => e.type === 'password' || /user|login|pass|pw/i.test((e.id || '') + ' ' + (e.name || ''));
  const dx = window.DevExpress && DevExpress.ui && DevExpress.ui.dxDateBox;
  const iso = v => { if (!v) return ''; const d = new Date(v); return isNaN(d) ? String(v) : d.toISOString().slice(0, 10); };
  const dateboxes = pick('.dx-datebox', 10).map(b => { const i = dx ? dx.getInstance(b) : null;
    return {widget: !!i, value: i ? iso(i.option('value')) : '', min: i ? iso(i.option('min')) : '',
            max: i ? iso(i.option('max')) : '', invalid: b.classList.contains('dx-invalid'),
            text: ((b.querySelector('input.dx-texteditor-input') || {}).value || '')}; });
  return {
    devextreme: !!dx, dateboxes,
    inputs: pick('input,select,textarea', 80).map(e => ({
      tag: e.tagName.toLowerCase(), type: e.type || '', id: e.id || '', name: e.name || '',
      placeholder: e.placeholder || '', aria: e.getAttribute('aria-label') || '',
      readonly: !!e.readOnly,
      value: secret(e) ? '' : (dateLike(e.value) ? e.value : (e.value ? '<' + e.value.length + ' chars>' : ''))})),
    clickables: pick('button,a,[role=button],[role=tab],[role=menuitem],li,span[onclick],div[onclick],'
                     + 'input[type=button],input[type=submit]', 400)
      .map(e => ({tag: e.tagName.toLowerCase(), id: e.id || '', role: e.getAttribute('role') || '',
                  cls: ((e.className && e.className.toString) ? e.className.toString() : '').split(' ')[0] || '',
                  text: txt(e)}))
      .filter(x => x.text && x.text.indexOf('@') < 0),
  };
}"""


class SrmSession:
    """One Edge window (own profile) for all downloads of a run."""

    def __init__(self, cfg: Dict[str, Any], headed: bool = False, channel: str = "msedge",
                 probe_dir: Optional[Path] = None, log: Callable[[str], None] = print,
                 profile_dir: Optional[Path] = None):
        self.cfg, self.headed, self.channel, self.log = cfg, headed, channel, log
        self.probe_dir = probe_dir
        self.profile_dir = profile_dir or PROFILE_DIR
        self.t = {"portal": 60, "popup": 30, "screen": 60, "grid": 90, "download": 120, **cfg.get("timeouts_s", {})}
        self.downloads: List[Any] = []
        self.steps: List[Dict[str, Any]] = []
        self._n = 0
        self.srm = None
        self.home_url = ""
        self.no_direct: set = set()          # screens whose <SRM_BASE_URL>/<id> did not open (menu path instead)
        self.login_tried = False             # SRM login: at most one try per run

    # -------- lifecycle
    def __enter__(self) -> "SrmSession":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        kw: Dict[str, Any] = dict(user_data_dir=str(self.profile_dir), headless=not self.headed,
                                  accept_downloads=True, viewport={"width": 1600, "height": 1000})
        if self.channel and self.channel != "chromium":
            kw["channel"] = self.channel
        self.ctx = self._pw.chromium.launch_persistent_context(**kw)
        self.ctx.on("page", self._watch)
        for p in self.ctx.pages:
            self._watch(p)
        return self

    def __exit__(self, *exc: Any) -> None:
        for fn in (lambda: self.ctx.close(), lambda: self._pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def _watch(self, page: Any) -> None:
        page.on("download", lambda d: self.downloads.append(d))
        page.on("dialog", lambda d: d.accept())

    # -------- probe / step log
    def step(self, name: str, page: Any = None, ok: bool = True, detail: str = "", inventory: bool = False) -> None:
        rec: Dict[str, Any] = {"step": name, "ok": ok, "detail": detail}
        if page is not None:
            rec["url_path"] = redact(getattr(page, "url", ""))
        if self.probe_dir is not None and page is not None:
            self._n += 1
            self.probe_dir.mkdir(parents=True, exist_ok=True)
            shot = self.probe_dir / f"{self._n:02d}_{name}.png"
            try:
                page.screenshot(path=str(shot), full_page=True)
                rec["screenshot"] = shot.name
            except Exception as exc:
                rec["screenshot_error"] = str(exc)[:120]
            if inventory or not ok:
                inv = self.inventory(page)
                (self.probe_dir / f"{self._n:02d}_{name}_elements.json").write_text(
                    json.dumps(inv, ensure_ascii=False, indent=1), encoding="utf-8")
                rec["elements"] = f"{self._n:02d}_{name}_elements.json"
        self.steps.append(rec)
        self.log(f"[SRM] {'ok ' if ok else 'FAIL'} {name} {detail}".rstrip())

    def inventory(self, page: Any) -> Dict[str, Any]:
        out: Dict[str, Any] = {"url_path": redact(page.url), "frames": []}
        for fr in page.frames:
            try:
                data = fr.evaluate(_INVENTORY_JS)
            except Exception as exc:
                data = {"error": str(exc)[:200]}
            out["frames"].append({"name": fr.name, "url_path": redact(fr.url), **data})
        return out

    # -------- element search across frames
    @staticmethod
    def _first_visible(loc: Any, limit: int = 20) -> Any:
        try:
            n = min(loc.count(), limit)
        except Exception:
            return None
        for i in range(n):
            try:
                if loc.nth(i).is_visible():
                    return loc.nth(i)
            except Exception:
                continue
        return None

    def find_text(self, page: Any, texts: List[str]) -> Any:
        for fr in page.frames:
            for t in texts:
                for loc in (lambda: fr.get_by_role("button", name=t, exact=True),
                            lambda: fr.get_by_role("tab", name=t, exact=True),
                            lambda: fr.get_by_text(t, exact=True),
                            lambda: fr.locator(f'[title="{t}"]'),
                            lambda: fr.locator(f'input[type=button][value="{t}"], input[type=submit][value="{t}"]')):
                    try:
                        hit = self._first_visible(loc())
                    except Exception:
                        hit = None
                    if hit is not None:
                        return hit
        return None

    def close_notices(self, page: Any, rounds: int = 5) -> int:
        """Portal notice pop-ups: tick 'close all' (notice_close_all_selector), then press the pop-up's own close button
        (notice_close_selectors, else a *button* named like notice_close_texts - never a checkbox label or a page
        widget). Repeats for stacked pop-ups. Returns the number of pop-ups closed."""
        closed = 0
        for _ in range(rounds):
            hit = None
            for fr in page.frames:
                for sel in self.cfg.get("notice_close_selectors", []):
                    try:
                        hit = self._first_visible(fr.locator(sel))
                    except Exception:
                        hit = None
                    if hit is not None:
                        break
                if hit is not None:
                    break
            if hit is None:
                for fr in page.frames:
                    for t in self.cfg.get("notice_close_texts", []):
                        try:
                            hit = self._first_visible(fr.get_by_role("button", name=t, exact=True))
                        except Exception:
                            hit = None
                        if hit is not None:
                            break
                    if hit is not None:
                        break
            if hit is None:
                return closed
            sel_all = self.cfg.get("notice_close_all_selector")
            if sel_all:
                for fr in page.frames:
                    try:
                        box = self._first_visible(fr.locator(sel_all))
                        if box is not None:
                            box.check(timeout=1500)
                            break
                    except Exception:
                        continue
            try:
                hit.click(timeout=3000)
                closed += 1
            except Exception:
                return closed
            page.wait_for_timeout(600)
        return closed

    def login_form_visible(self, page: Any) -> bool:
        for fr in page.frames:
            try:
                if fr.locator(LOGIN_FORM).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _login_fields(self, page: Any) -> Tuple[Any, Any]:
        spec = self.cfg.get("srm_login", {})
        for fr in page.frames:
            try:
                user = self._first_visible(fr.locator(spec.get("user_selector", '#Input_UserName')))
                pw = self._first_visible(fr.locator(spec.get("password_selector", 'input[type="password"]')))
            except Exception:
                continue
            if user is not None and pw is not None:
                return user, pw
        return None, None

    def _clear_login_fields(self, page: Any) -> None:
        user, pw = self._login_fields(page)
        for box in (user, pw):
            try:
                if box is not None:
                    box.fill("", timeout=2000)
            except Exception:
                pass

    def srm_login(self, page: Any) -> None:
        """SRM's own login form (see module doc): one try with the .env account. Nothing is typed when an earlier try
        failed (LOGIN_BLOCKED) or no account is set (SSO_UNAVAILABLE); a failed try blocks later runs (LOGIN_FAILED).
        No screenshot or element list is taken while a value is in a field."""
        if self.login_tried:
            self.step("srm_login_again", page, False, "SRM asked for a second login in one run - stopped, nothing typed")
            raise CollectError("LOGIN_FAILED", "SRM asked for a second login in the same run - stopped (nothing typed)")
        blocked = login_blocked()
        if blocked:
            self.step("srm_login_blocked", page, False, "an earlier SRM login failed - nothing typed")
            raise CollectError("LOGIN_BLOCKED", f"SRM login blocked since {blocked.get('at', '?')} after a failed try - "
                                                "check the .env account, then delete logs/srm_login_blocked.json")
        user, pw, prefix = srm_credentials()
        if not (user and pw):
            self.step("srm_login_form", page, False, "SRM asked for a login - no SRM_ / WMS_ USERNAME + PASSWORD in .env")
            raise CollectError("SSO_UNAVAILABLE", "login form visible on SRM and no SRM / WMS account in .env (nothing typed)")
        box_user, box_pw = self._login_fields(page)
        if box_user is None or box_pw is None:
            self.step("srm_login_form", page, False, "SRM login fields not found (config srm_login) - nothing typed")
            raise CollectError("SSO_UNAVAILABLE", "SRM login fields not found (config/srm_collector.json srm_login) - "
                                                  "nothing typed")
        self.log(f"[SRM] login with the {prefix} account from .env (one try)")
        self.login_tried = True
        box_user.fill(user)
        box_pw.fill(pw)
        submit = self.find_text(page, self.cfg.get("srm_login", {}).get("submit_texts", ["Login", "Log in"]))
        try:
            if submit is not None:
                submit.click(timeout=5000)
            else:
                box_pw.press("Enter")
        except Exception:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=self.t["popup"] * 1000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
        if self.login_form_visible(page):
            block_login(prefix, "SRM login form still visible after one try")
            self._clear_login_fields(page)
            self.step("srm_login_failed", page, False, f"SRM login with the {prefix} account failed - later runs blocked")
            raise CollectError("LOGIN_FAILED", f"SRM login with the {prefix} account failed once - no retry (shared WMS "
                                               "account); check .env, then delete logs/srm_login_blocked.json")
        self.step("srm_login", page, True, f"signed in with the {prefix} account from .env")

    DATE_VALUE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{1,2}-\d{1,2})\s*$")

    def find_date_input(self, page: Any) -> Any:
        """Delivery Date input: the configured selector, then its label in any letter case (the real SRM shows
        'delivery date'), then - a DevExtreme date box has no label link (probe 11/13 06:42) - the visible text input
        that already holds a date (MM/DD/YYYY or YYYY-MM-DD)."""
        spec = self.cfg.get("date_input", {})
        up, low = "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"
        for fr in page.frames:
            cands = []
            if spec.get("selector"):
                cands.append(lambda: fr.locator(spec["selector"]))
            for lt in spec.get("label_texts", []):
                lt_low = " ".join(lt.lower().split())
                cands += [lambda lt=lt: fr.get_by_label(lt, exact=True),
                          lambda lt_low=lt_low: fr.locator(
                              f"xpath=//*[translate(normalize-space(.), '{up}', '{low}')='{lt_low}']/following::input[1]")]
            for c in cands:
                try:
                    hit = self._first_visible(c())
                except Exception:
                    hit = None
                if hit is not None:
                    return hit
        if spec.get("value_fallback", True):
            for fr in page.frames:
                try:
                    loc = fr.locator("input[type=text], input:not([type])")
                    n = min(loc.count(), 60)
                except Exception:
                    continue
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if el.is_visible() and self.DATE_VALUE.match(el.input_value(timeout=500) or ""):
                            # pin this element: a position (nth) drifts when the screen adds inputs while loading
                            # (probe 06:48 - the plant box appeared above it and got the date instead)
                            el.evaluate("e => e.setAttribute('data-srm-date', '1')")
                            return fr.locator("input[data-srm-date='1']").first
                    except Exception:
                        continue
        return None

    def _wait(self, fn: Callable[[], Any], seconds: float, page: Any) -> Any:
        end = time.time() + seconds
        while True:
            got = fn()
            if got:
                return got
            if time.time() > end:
                return None
            page.wait_for_timeout(700)

    # -------- flow
    def open_srm(self) -> Any:
        base = os.getenv("SRM_BASE_URL", "").strip().rstrip("/")
        if base:            # user 11/12 20:38: SRM main URL known - straight to SRM (own login), no portal / pop-ups
            return self._open_srm_direct(base)
        portal = os.getenv("SRM_PORTAL_URL", "").strip()
        if not portal:
            raise CollectError("CONFIG", "SRM_PORTAL_URL (or SRM_BASE_URL) is not set in .env")
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        page.goto(portal, wait_until="domcontentloaded", timeout=self.t["portal"] * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)   # notice pop-ups render after the first load
        except Exception:
            pass
        app = None
        store_clicked = False
        start = time.time()
        end = start + self.t["portal"]
        while app is None:
            if self.login_form_visible(page):
                self.step("portal_login_form", page, False, "PC SSO did not sign in - stopped, no credentials entered")
                raise CollectError("SSO_UNAVAILABLE", "login form visible on the portal (no credentials entered)")
            self.close_notices(page)
            app = self.find_text(page, self.cfg["portal_app_texts"])
            if app is None and not store_clicked and time.time() > start + 5:   # app list may sit behind APP STORE
                store = self.find_text(page, self.cfg.get("portal_app_store_texts", []))
                if store is not None:
                    store.click(timeout=3000, no_wait_after=True)
                    store_clicked = True
            if app is None and time.time() > end:
                self.step("portal_no_srm_app", page, False, "SRM app not found", inventory=True)
                raise CollectError("NO_SRM_APP", "SRM app not found on the portal")
            if app is None:
                page.wait_for_timeout(1000)
        self.step("portal", page)
        before = len(self.ctx.pages)
        last: Optional[Exception] = None
        for _ in range(3):                    # a notice pop-up that appears late stays on top of the app icon
            try:
                app.click(timeout=5000, no_wait_after=True)
                break
            except Exception as exc:
                last = exc
                self.close_notices(page)
                page.wait_for_timeout(800)
                app = self.find_text(page, self.cfg["portal_app_texts"]) or app
        else:
            self.step("portal_app_click", page, False, "SRM app could not be clicked", inventory=True)
            raise CollectError("NO_SRM_APP", f"SRM app not clickable (pop-up on top?): {str(last)[:160]}")
        base = os.getenv("SRM_BASE_URL", "").strip().rstrip("/")
        srm = self._wait(lambda: self._srm_page(portal, base), self.t["popup"], page)
        if srm is None:     # no page on the SRM host yet: newest new window, else the portal window itself
            srm = self.ctx.pages[-1] if len(self.ctx.pages) > before else page
        try:
            srm.wait_for_load_state("domcontentloaded", timeout=self.t["popup"] * 1000)
        except Exception:
            pass
        srm.wait_for_timeout(2500)                     # SSO hand-over (menu param window) and notice pop-ups
        for p in list(self.ctx.pages):
            if self.login_form_visible(p):
                self.srm_login(p)
                if p is not srm and not p.is_closed():
                    srm = p
        self.step("srm_home", srm, True, f"{len(self.ctx.pages)} window(s)", inventory=True)
        self.srm, self.home_url = srm, srm.url
        return self.srm

    def _open_srm_direct(self, base: str) -> Any:
        """SRM_BASE_URL set: SRM home in the first tab; its own login form gets the one-try .env login."""
        page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        home = base + "/" + str(self.cfg.get("srm_home_path", "Home")).lstrip("/")
        page.goto(home, wait_until="domcontentloaded", timeout=self.t["portal"] * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        if self.login_form_visible(page):
            self.srm_login(page)
        self.step("srm_home", page, True, "direct (SRM_BASE_URL)", inventory=True)
        self.srm, self.home_url = page, page.url
        return page

    def _srm_page(self, portal: str, base: str) -> Any:
        """Newest open window on the SRM host (SRM_BASE_URL), or without it any window outside the portal host."""
        phost = urlsplit(portal).netloc.lower()
        for p in reversed(self.ctx.pages):
            try:
                if p.is_closed():
                    continue
                u = p.url or ""
            except Exception:
                continue
            if base:
                if u.lower().startswith(base.lower()):
                    return p
            elif u.startswith("http") and urlsplit(u).netloc.lower() not in ("", phost):
                return p
        return None

    def open_screen(self, screen: Dict[str, Any]) -> Any:
        srm = self.srm
        base = os.getenv("SRM_BASE_URL", "").strip().rstrip("/")
        if base and screen["id"] not in self.no_direct:
            srm.goto(base + "/" + screen["id"], wait_until="domcontentloaded", timeout=self.t["screen"] * 1000)
            date_in = self._screen_date_input(srm, min(self.t["screen"], 25))
            if date_in is None and screen.get("menu_texts"):    # <base>/<id> is not the screen: menu path instead
                self.no_direct.add(screen["id"])
                self.step(f"{screen['id']}_direct_url", srm, False, "no Delivery Date on <SRM_BASE_URL>/<id> - menu path")
                srm = self._menu(screen)
                date_in = self._screen_date_input(srm, self.t["screen"])
        else:
            srm = self._menu(screen)
            date_in = self._screen_date_input(srm, self.t["screen"])
        if date_in is None:
            self.step(f"{screen['id']}_no_date_input", srm, False, "Delivery Date input not found", inventory=True)
            raise CollectError("DATE_INPUT_NOT_FOUND", f"{screen['id']}: Delivery Date input not found")
        # the screen keeps initialising after the date box appears (plant, default date, first grid load - probe
        # 11/13 06:48: a date typed during 'Loading...' was wiped), so wait until it is idle and look again
        self.wait_ready(self.srm, self.t["grid"])
        date_in = self.find_date_input(self.srm) or date_in
        self.step(f"{screen['id']}_open", self.srm, inventory=True)
        return date_in

    def wait_ready(self, page: Any, seconds: float) -> bool:
        """Network idle and no visible busy indicator (config busy_selectors, e.g. the DevExtreme 'Loading...' panel)."""
        end = time.time() + seconds
        try:
            page.wait_for_load_state("networkidle", timeout=int(seconds * 1000))
        except Exception:
            pass
        sels = self.cfg.get("busy_selectors", [])
        while True:
            busy = False
            for fr in page.frames:
                for sel in sels:
                    try:
                        if self._first_visible(fr.locator(sel)) is not None:
                            busy = True
                            break
                    except Exception:
                        continue
                if busy:
                    break
            if not busy:
                page.wait_for_timeout(int(self.cfg.get("settle_ms", 800)))
                return True
            if time.time() > end:
                return False
            page.wait_for_timeout(500)

    def _screen_date_input(self, srm: Any, seconds: float) -> Any:
        for t in self.cfg.get("srm_notice_close_texts", []):
            hit = self.find_text(srm, [t])
            if hit is not None:
                try:
                    hit.click(timeout=1500, no_wait_after=True)
                except Exception:
                    pass
        return self._wait(lambda: self.find_date_input(srm), seconds, srm)

    def _menu(self, screen: Dict[str, Any]) -> Any:
        """Menu path, e.g. Material > Material Plan > Sequence Order Plan: the screen entry is clicked directly when
        visible (menu already open), otherwise the path is walked from the top - on the SRM home page if needed."""
        path = screen.get("menu_texts", [])
        srm = self.srm

        def walk() -> Any:
            hit = self.find_text(srm, [path[-1]])
            if hit is None:
                for t in path[:-1]:
                    step = self._wait(lambda: self.find_text(srm, [t]), 5, srm)
                    if step is not None:
                        step.click(timeout=5000)
                        srm.wait_for_timeout(800)
                hit = self._wait(lambda: self.find_text(srm, [path[-1]]), 8, srm)
            return hit

        hit = walk()
        if hit is None and self.home_url and srm.url != self.home_url:
            srm.goto(self.home_url, wait_until="domcontentloaded", timeout=self.t["screen"] * 1000)
            hit = walk()
        if hit is None:
            self.step(f"{screen['id']}_menu", srm, False, f"menu '{' > '.join(path)}' not found", inventory=True)
            raise CollectError("SCREEN_NOT_FOUND", f"menu '{' > '.join(path)}' not found (set SRM_BASE_URL in .env "
                                                   "or fix menu_texts in config/srm_collector.json)")
        before = len(self.ctx.pages)
        hit.click(timeout=5000)
        srm.wait_for_timeout(1200)
        if len(self.ctx.pages) > before:            # the menu opened the screen in a new window
            self.srm = srm = self.ctx.pages[-1]
            try:
                srm.wait_for_load_state("domcontentloaded", timeout=self.t["screen"] * 1000)
            except Exception:
                pass
        return srm

    @staticmethod
    def _digits(text: str) -> str:
        return "".join(ch for ch in (text or "") if ch.isdigit())

    _WIDGET_JS = r"""(e, a) => {
      const box = e.closest('.dx-datebox');
      const dx = window.DevExpress && DevExpress.ui && DevExpress.ui.dxDateBox;
      const inst = box && dx ? dx.getInstance(box) : null;
      if (!inst) return null;
      if (a.set) inst.option('value', new Date(a.y, a.m - 1, a.d));
      const v = inst.option('value');
      if (!v) return '';
      const d = new Date(v);
      return isNaN(d) ? String(v) : [d.getFullYear(), d.getMonth() + 1, d.getDate()].join('-');
    }"""

    def widget_date(self, date_in: Any, delivery: dt.date, set_it: bool = False) -> Optional[bool]:
        """None = no DevExtreme widget found (only the typed text can be checked); else whether the widget's own
        value - the one the screen queries with - is the delivery date (probe 11/13 07:10: TSTA006 showed the typed
        11/14 but exported 11/13)."""
        try:
            got = date_in.evaluate(self._WIDGET_JS, {"set": set_it, "y": delivery.year, "m": delivery.month,
                                                      "d": delivery.day})
        except Exception:
            return None
        if got is None:
            return None
        return got == f"{delivery.year}-{delivery.month}-{delivery.day}"

    def set_date_and_search(self, screen: Dict[str, Any], date_in: Any, delivery: dt.date) -> None:
        """Delivery Date = delivery: nothing to type when the box already shows it (AM: today is the default),
        otherwise type it (up to 3 tries, the box is looked up again each time) and compare the whole date."""
        srm = self.srm
        value = delivery.strftime(self.cfg.get("date_input", {}).get("format", "%Y-%m-%d"))
        want, shown = self._digits(value), ""
        for attempt in range(3):
            date_in = self.find_date_input(srm) or date_in
            try:
                shown = date_in.input_value(timeout=2000)
            except Exception:
                shown = ""
            if self._digits(shown) == want:
                break
            try:
                date_in.click(timeout=3000)
                date_in.fill(value, timeout=3000)
            except Exception:  # read-only calendar input: set the value the way a date picker would
                date_in.evaluate("(e, v) => { e.removeAttribute('readonly'); e.value = v; "
                                 "e.dispatchEvent(new Event('input', {bubbles: true})); "
                                 "e.dispatchEvent(new Event('change', {bubbles: true})); }", value)
            try:
                date_in.press("Tab", timeout=2000)
            except Exception:
                pass
            srm.wait_for_timeout(800 + 700 * attempt)
            try:
                shown = date_in.input_value(timeout=2000)
            except Exception:
                shown = ""
            if self._digits(shown) == want:
                break
            self.wait_ready(srm, 15)
        if self._digits(shown) != want:
            self.step(f"{screen['id']}_date", srm, False, f"date not taken (shows '{shown}')", inventory=True)
            raise CollectError("DATE_NOT_SET", f"{screen['id']}: Delivery Date shows '{shown}', wanted {value}")
        how = "text"
        ok = self.widget_date(date_in, delivery)
        if ok is False:                       # the box shows the date but the widget still holds another one
            ok = self.widget_date(date_in, delivery, set_it=True)
            how = "widget"
            srm.wait_for_timeout(600)
        if ok is False:
            self.step(f"{screen['id']}_date", srm, False, f"widget keeps another date (shows '{shown}')", inventory=True)
            raise CollectError("DATE_NOT_SET", f"{screen['id']}: the date box does not take {value}")
        self.search(screen)
        self.step(f"{screen['id']}_searched_{delivery:%Y%m%d}", srm,
                  detail=f"date set via {how}" + ("" if ok is None else " (widget checked)"), inventory=True)

    def search(self, screen: Dict[str, Any]) -> None:
        srm = self.srm
        btn = self.find_text(srm, self.cfg["search_texts"])
        if btn is None:
            self.step(f"{screen['id']}_no_search", srm, False, "search button not found", inventory=True)
            raise CollectError("SEARCH_NOT_FOUND", f"{screen['id']}: search button not found")
        btn.click(timeout=5000)
        srm.wait_for_timeout(600)
        self.wait_ready(srm, self.t["grid"])

    def export(self, screen: Dict[str, Any], exp: Dict[str, Any], delivery: dt.date, out_dir: Path) -> Path:
        srm = self.srm
        tab = self.find_text(srm, [exp["tab_text"]])
        if tab is None:
            self.step(f"{exp['kind']}_no_tab", srm, False, f"tab '{exp['tab_text']}' not found", inventory=True)
            raise CollectError("TAB_NOT_FOUND", f"tab '{exp['tab_text']}' not found")
        tab.click(timeout=5000)
        srm.wait_for_timeout(1000)
        self.wait_ready(srm, self.t["grid"])
        if self.cfg.get("search_after_tab"):
            self.search(screen)
        btn = self.find_text(srm, self.cfg["export_texts"])
        if btn is None:
            self.step(f"{exp['kind']}_no_export", srm, False, "export button not found", inventory=True)
            raise CollectError("EXPORT_NOT_FOUND", f"{exp['tab_text']}: export button not found")
        self.downloads.clear()
        btn.click(timeout=10000)
        end, confirmed = time.time() + self.t["download"], False
        while not self.downloads:
            if time.time() > end:
                self.step(f"{exp['kind']}_no_download", srm, False, "no file within the time limit", inventory=True)
                raise CollectError("DOWNLOAD_TIMEOUT", f"{exp['tab_text']}: no file downloaded")
            if not confirmed and time.time() > end - self.t["download"] + 8:
                ok_btn = self.find_text(srm, self.cfg.get("confirm_texts", []))
                if ok_btn is not None:
                    ok_btn.click(timeout=3000)
                confirmed = True
            srm.wait_for_timeout(500)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / target_name(exp["kind"], delivery)
        self.downloads.pop(0).save_as(str(path))
        chk = check_export(exp["kind"], path, delivery)
        if not chk["ok"]:
            rej = out_dir / "rejected"
            rej.mkdir(exist_ok=True)
            shutil.move(str(path), str(rej / f"{dt.datetime.now():%H%M%S}_{path.name}"))
            self.step(f"{exp['kind']}_{chk['status'].lower()}", srm, False, chk["detail"], inventory=True)
            raise CollectError(chk["status"], f"{exp['tab_text']}: {chk['detail']}")
        self.step(f"{exp['kind']}_{delivery:%Y%m%d}", srm, True, f"{path.name} ({chk['parts']} parts)")
        return path


SESSION_ERRORS = {"CONFIG", "SSO_UNAVAILABLE", "LOGIN_FAILED", "LOGIN_BLOCKED", "NO_SRM_APP", "LOCKED",
                  "BROWSER_ERROR"}


def collect(jobs: List[Tuple[Any, ...]], cfg: Optional[Dict[str, Any]] = None, headed: bool = False,
            channel: str = "msedge", probe_dir: Optional[Path] = None, log: Callable[[str], None] = print,
            profile_dir: Optional[Path] = None) -> Dict[str, Any]:
    """jobs = [(delivery date, output folder[, kinds])] - kinds limits the exports (retry of the missing ones only).
    One browser session; a screen or export that fails does not stop the others. Never raises.
    Returns {'status': 'OK' | first failure, 'errors': [...], 'files': [...], 'steps': [...], 'seconds'}."""
    cfg = cfg or load_config()
    res: Dict[str, Any] = {"status": "STARTED", "files": [], "errors": [], "steps": []}
    t0 = time.time()
    try:
        acquire_lock()
    except CollectError as exc:
        return {**res, "status": exc.status, "detail": exc.detail, "seconds": 0.0}
    try:
        with SrmSession(cfg, headed=headed, channel=channel, probe_dir=probe_dir, log=log,
                        profile_dir=profile_dir) as s:
            try:
                s.open_srm()
                for job in jobs:
                    delivery, out_dir = job[0], Path(job[1])
                    kinds = set(job[2]) if len(job) > 2 and job[2] else None
                    for screen in cfg["screens"]:
                        exports = [e for e in screen["exports"] if kinds is None or e["kind"] in kinds]
                        if not exports:
                            continue
                        try:
                            date_in = s.open_screen(screen)
                            s.set_date_and_search(screen, date_in, delivery)
                        except CollectError as exc:      # screen failed: its exports are retried next time
                            res["errors"].append({"status": exc.status, "detail": exc.detail, "screen": screen["id"]})
                            continue
                        for exp in exports:
                            try:
                                res["files"].append(str(s.export(screen, exp, delivery, out_dir)))
                            except CollectError as exc:
                                res["errors"].append({"status": exc.status, "detail": exc.detail, "kind": exp["kind"]})
            except CollectError as exc:                    # portal / SSO: nothing else can work
                res["errors"].append({"status": exc.status, "detail": exc.detail})
            except Exception as exc:                       # Playwright timeout etc.
                res["errors"].append({"status": "ERROR", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"})
                try:
                    s.step("error", s.srm or (s.ctx.pages[-1] if s.ctx.pages else None), False,
                           res["errors"][-1]["detail"], inventory=True)
                except Exception:
                    pass
            res["steps"] = s.steps
    except Exception as exc:  # browser did not start (Edge / Playwright missing)
        res["errors"].append({"status": "BROWSER_ERROR", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"})
    finally:
        release_lock()
    res["status"] = res["errors"][0]["status"] if res["errors"] else "OK"
    res["detail"] = "; ".join(f"{e['status']}: {e['detail']}" for e in res["errors"])[:600]
    res["seconds"] = round(time.time() - t0, 1)
    return res


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="#330 SRM export download (portal PC SSO; SRM login with the .env "
                                             "account, one try per run)")
    ap.add_argument("--date", help="delivery date YYYY-MM-DD (default today)")
    ap.add_argument("--out", help="output folder (default inbox/<today> or the probe folder)")
    ap.add_argument("--probe", action="store_true", help="screenshots + element list per step into scratch/")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--channel", default="msedge", help="browser channel (default msedge)")
    a = ap.parse_args(argv)
    from srm_env import load_env
    load_env()
    delivery = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    probe_dir = BASE_DIR / "scratch" / f"srm_probe_{dt.datetime.now():%Y%m%d_%H%M%S}" if a.probe else None
    out = Path(a.out) if a.out else (probe_dir / "downloads" if probe_dir else
                                      BASE_DIR / "inbox" / f"{dt.date.today():%Y%m%d}")
    res = collect([(delivery, out)], headed=a.headed, channel=a.channel, probe_dir=probe_dir)
    res["files"] = [Path(f).name for f in res["files"]]
    if probe_dir:
        probe_dir.mkdir(parents=True, exist_ok=True)
        (probe_dir / "probe_result.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        res["probe_dir"] = str(probe_dir.relative_to(BASE_DIR))
    print(json.dumps({k: v for k, v in res.items() if k != "steps"}, ensure_ascii=False))
    return 0 if res["status"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
