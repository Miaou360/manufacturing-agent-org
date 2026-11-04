from datetime import datetime
from pathlib import Path
import sys
import pytest

JOB_DIR = Path(__file__).resolve().parents[1]
DEPT_DIR = JOB_DIR.parent
for _p in (JOB_DIR, DEPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import d107_portal_drafter as drafter
from report_labels import assert_no_hangul


SAMPLE_HEADERS = [
    "",
    "PLANT",
    "POST DATE",
    "GUBUN",
    "SUMNO",
    "BARCODE",
    "INFORECORD",
    "PART NUMBER",
    "PART DESC",
    "VENDOR",
    "MVT",
    "FROM LOC",
    "TO LOC",
    "QTY",
    "UNIT",
    "NOTE",
    "ORDER NOTE",
    "User Name",
]


def test_step_2_hard_blocks_submit_modes_and_01_window():
    for forbidden_mode in ("submit_2c", "draft", "submit", "\uAE30\uC548", "auto_submit"):
        with pytest.raises(PermissionError):
            drafter.run_d107_phase2a_dry_run(mode=forbidden_mode)

    assert drafter.ALLOWED_MODES == frozenset({"dry_run_2a", "save_temp_2b"})

    with pytest.raises(RuntimeError, match="01_JOB active window"):
        drafter.check_safe_portal_window(now=datetime(2026, 11, 2, 15, 2, 0))
    with pytest.raises(RuntimeError, match="01_JOB active window"):
        drafter.check_safe_portal_window(now=datetime(2026, 11, 2, 15, 33, 0))
    # Safe minute (including 07:23!) passes cleanly
    drafter.check_safe_portal_window(now=datetime(2026, 11, 3, 7, 23, 0))


def test_filter_and_bundle_prev_day_latest_01_recheck_and_20_row_chunking():
    # 25 prev-day candidates + 1 resolved in latest 01 + 1 today row (`20261102`) + 1 non-candidate row
    d107_cands = []
    portal_rows = []
    latest_01 = []

    for i in range(1, 26):
        part = f"MAT_CAND_{i:03d}"
        d107_cands.append(
            {
                "material": part,
                "mvt": "311",
                "from_sloc": "SL02",
                "to_sloc": "SP31",
                "deficit_loc": "SL02",
                "from_sap": 0.0,
                "from_wms": 0.0,
                "qty": float(10 + i),
                "first_seen_edt": "2026-10-31 12:00:00",
                "d107_status": "CANDIDATE",
            }
        )
        latest_01.append(
            {"material": part, "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": float(10 + i)}
        )
        portal_rows.append(
            [
                "", "P001", "20261101", "C", f"2026110100{i:04d}", f"BC{i:06d}", "530000",
                part, f"DESC {i} 외장", "V100", "311", "SL02", "SP31", str(10 + i), "EA", "Insufficient stock", "", "U1",
            ]
        )

    # Candidate resolved in latest 01_JOB snapshot -> MUST be excluded!
    d107_cands.append(
        {"material": "MAT_RESOLVED_01", "mvt": "311", "from_sloc": "SL01", "to_sloc": "SP31", "qty": 50.0, "d107_status": "CANDIDATE"}
    )
    portal_rows.append(
        ["", "P001", "20261101", "C", "20261101999999", "BC999999", "530000", "MAT_RESOLVED_01", "DESC", "V100", "311", "SL01", "SP31", "50", "EA", "Insufficient stock", "", "U1"]
    )

    # Today row (`20261102`) -> MUST NOT be selected!
    portal_rows.append(
        ["", "P001", "20261102", "C", "20261102000001", "BC_TODAY_01", "530013", "MAT_TODAY_772", "PIVOT-MTG", "V100", "311", "SL02", "SP31", "772", "EA", "Insufficient stock", "", "U1"]
    )

    # Prev-day row NOT in D107 candidates -> MUST NOT be selected!
    portal_rows.append(
        ["", "P001", "20261101", "C", "20261101888888", "BC888888", "530000", "MAT_UNLISTED", "UNLISTED", "V100", "311", "SL01", "SP31", "30", "EA", "Insufficient stock", "", "U1"]
    )

    bundle = drafter.filter_and_bundle_portal_rows(
        portal_headers=SAMPLE_HEADERS,
        portal_rows=portal_rows,
        d107_candidates=d107_cands,
        latest_01_gi_records=latest_01,
        report_date_str="2026-11-02",
        max_rows_per_draft=20,
    )

    assert len(bundle["excluded_resolved_in_latest_01"]) == 1
    assert bundle["excluded_resolved_in_latest_01"][0]["material"] == "MAT_RESOLVED_01"
    assert len(bundle["matched_rows"]) == 25
    assert bundle["draft_count"] == 2
    assert len(bundle["draft_batches"][0]) == 20
    assert len(bundle["draft_batches"][1]) == 5
    matched_mats = {r["material"] for r in bundle["matched_rows"]}
    assert "MAT_TODAY_772" not in matched_mats
    assert "MAT_UNLISTED" not in matched_mats
    assert "MAT_RESOLVED_01" not in matched_mats


def test_schema_and_form_mismatch_guards():
    with pytest.raises(drafter.PortalSchemaMismatchError):
        drafter.validate_portal_headers(["PLANT", "POST DATE", "PART NUMBER"])  # missing SUMNO/BARCODE/etc.

    sel = [{"sumno": "S1", "barcode": "B1", "material": "M1", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 100.0}]
    assert drafter.verify_form_rows_match(sel, sel) is True

    bad_form = [{"sumno": "S1", "barcode": "B1", "material": "M1", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 99.0}]
    with pytest.raises(drafter.PortalFormRowMismatchError):
        drafter.verify_form_rows_match(sel, bad_form)


def test_english_reason_overflow_excel_and_b3_hangul_exemption(tmp_path):
    rows = [
        {
            "plant": "P001",
            "post_date": "20261031",
            "sumno": f"20261031000{i}",
            "barcode": f"BC00{i}",
            "material": f"MAT_SHORT_{i}",
            "material_desc": f"BRACKET 외장 #{i}",  # raw SAP MAKTX Hangul token exempted by B-3!
            "mvt": "311",
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "qty": 772.0,
            "d107_candidate": {
                "deficit_loc": "SL02",
                "from_sap": 0.0,
                "from_wms": 0.0,
                "first_seen_edt": "2026-10-30 12:00:54",
            },
        }
        for i in range(1, 4)
    ]

    # Normal inline English reason
    pack_inline = drafter.build_deletion_reason_and_attachment(
        matched_rows=rows[:1],
        report_date_str="2026-11-02",
        max_reason_chars=900,
        out_dir=tmp_path,
    )
    assert pack_inline["overflow_used"] is False
    assert "MAT_SHORT_1 SL02->SP31 311 772 EA" in pack_inline["reason_text"]
    assert "unresolvable" in pack_inline["reason_text"]
    assert_no_hangul({"reason": pack_inline["reason_text"]})

    # Overflow -> 1-line summary + English Excel attachment (with raw SAP `Description` Hangul exempted by B-3!)
    pack_ovf = drafter.build_deletion_reason_and_attachment(
        matched_rows=rows,
        report_date_str="2026-11-02",
        max_reason_chars=120,
        out_dir=tmp_path,
    )
    assert pack_ovf["overflow_used"] is True
    att_p = Path(pack_ovf["attachment_path"])
    assert att_p.exists()
    assert_no_hangul({"reason": pack_ovf["reason_text"]}, attachments=[att_p])

    # Render [TEST] email HTML with raw SAP Description containing Hangul -> passes assert_no_hangul!
    subj, html = drafter.build_phase2a_test_email_html(
        {
            "report_date": "2026-11-02",
            "yesterday_ymd": "20261101",
            "portal_total_rows": 3,
            "d107_input_candidate_count": 3,
            "d107_source_file": "d107_phase1_20261102.json",
            "latest_01_file": "GI_Error_20261102_150000.XLS",
            "excluded_resolved_in_latest_01": [],
            "matched_rows": rows,
            "skipped_portal_rows": [],
            "max_rows_per_draft": 20,
            "draft_count": 1,
            "reason_preview": pack_ovf["reason_text"],
            "status": "DRY_RUN_2A_OK",
        }
    )
    assert subj.startswith("[TEST]")
    assert_no_hangul({"subject": subj, "html_body": html}, attachments=[att_p])


def test_0723_skips_when_today_phase1_json_missing(tmp_path):
    # Yesterday's JSON exists, but today's 07:18 JSON (`d107_phase1_20261103.json`) does NOT exist yet!
    (tmp_path / "d107_phase1_20261102.json").write_text(
        '{"report_date": "2026-11-02", "candidates": [{"material": "MAT_1", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 10}]}',
        encoding="utf-8",
    )
    res = drafter.run_d107_phase2a_dry_run(
        report_date="2026-11-03",
        mode="dry_run_2a",
        send_test_email=False,
        require_today_phase1=True,
        resolution_logs_dir=tmp_path,
        now_fn=lambda: datetime(2026, 11, 3, 7, 23, 5),
    )
    assert res["status"] == "SKIPPED_TODAY_PHASE1_MISSING"
    assert res["draft_count"] == 0
    assert res["d107_source_file"] == "MISSING_TODAY_PHASE1"
    subj, html = drafter.build_phase2a_test_email_html(res)
    assert "SKIPPED_TODAY_PHASE1_MISSING" in subj
    assert_no_hangul({"subject": subj, "html_body": html})


def test_0729_morning_cutoff_resolution():
    t_0723 = datetime(2026, 11, 3, 7, 23, 10)
    cutoff = drafter.resolve_batch_cutoff_dt(now=t_0723)
    assert cutoff == datetime(2026, 11, 3, 7, 29, 0)
    assert drafter.is_past_batch_cutoff(datetime(2026, 11, 3, 7, 28, 59), cutoff) is False
    assert drafter.is_past_batch_cutoff(datetime(2026, 11, 3, 7, 29, 0), cutoff) is True

    # Afternoon manual dry-run has no automatic 07:29 morning cutoff unless explicit cutoff_dt passed
    t_1520 = datetime(2026, 11, 3, 15, 20, 0)
    assert drafter.resolve_batch_cutoff_dt(now=t_1520) is None


def test_groupware_one_shot_login_circuit_breaker_and_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUPWARE_USERNAME", "gw_secret_id_token")
    monkeypatch.setenv("CORP_PORTAL_URL", "https://portal.internal.example/portal/openMain.do")
    scrubbed = drafter.redact_portal_sensitive(
        "Failed login for gw_secret_id_token at https://portal.internal.example/portal/openMain.do"
    )
    assert "gw_secret_id_token" not in scrubbed
    assert "portal.internal.example" not in scrubbed
    assert "<REDACTED:GROUPWARE_USERNAME>" in scrubbed

    class _FakeLoc:
        def __init__(self, counter_dict, name):
            self._c = counter_dict
            self._name = name

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def fill(self, val):
            self._c.setdefault("fills", []).append((self._name, val))

        def focus(self):
            self._c["focused"] = self._c.get("focused", 0) + 1

        def is_checked(self):
            return True

        def check(self):
            pass

        def click(self, **kwargs):
            self._c["clicks"] = self._c.get("clicks", 0) + 1

    class _FakePage:
        def __init__(self, after_url):
            self.url = after_url
            self.stats = {}

        def locator(self, sel):
            return _FakeLoc(self.stats, sel)

        def wait_for_load_state(self, *args, **kwargs):
            pass

    halt_flag = tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME
    bad_page = _FakePage("https://gw.internal/WebSite/Login.aspx")

    # 1st failed attempt trips the circuit breaker and writes halt_flag
    with pytest.raises(RuntimeError, match="circuit breaker tripped"):
        drafter.attempt_groupware_login_once_or_halt(bad_page, "dummy_gw_id", "dummy_pw", halt_flag)
    assert bad_page.stats.get("clicks") == 1
    assert halt_flag.exists()

    # 2nd attempt is IMMEDIATELY blocked before touching DOM or clicking `#btnPWLogin`!
    bad_page_2 = _FakePage("https://gw.internal/WebSite/Login.aspx")
    with pytest.raises(RuntimeError, match="circuit breaker is OPEN"):
        drafter.attempt_groupware_login_once_or_halt(bad_page_2, "dummy_gw_id", "dummy_pw", halt_flag)
    assert bad_page_2.stats.get("clicks", 0) == 0


def test_sso_unavailable_skips_without_credential_input(tmp_path):
    class _ZeroLoc:
        def __init__(self, count_val=0):
            self._cnt = count_val

        def count(self):
            return self._cnt

    class _FakeTab:
        def __init__(self):
            self.goto_calls = 0

        def on(self, *args, **kwargs):
            pass

        def goto(self, *args, **kwargs):
            self.goto_calls += 1

        def get_by_text(self, txt, exact=True):
            return _ZeroLoc(0)  # `전자결재` not visible

        def locator(self, sel):
            return _ZeroLoc(1 if "password" in sel else 0)  # password prompt visible!

    class _FakeCtx:
        def __init__(self):
            self.pages = [_FakeTab()]

    sso_res = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=_FakeCtx(),
        BINSYNTH9941_url="https://portal.internal.example/portal/openMain.do",
        wms_user="dummy_u",
        wms_pw="dummy_p",
        allow_login_once=False,
        halt_flag_path=tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME,
    )
    assert sso_res["portal_ready"] is False
    assert "SKIPPED_SSO_UNAVAILABLE" in sso_res["note"]

def test_logged_in_signal_with_delayed_app_load_waits_reloads_and_passes(tmp_path):
    """#229 BINSYNTH9952: Logged-in signal (`SL AI` / `#nav-ai-btn`) present + delayed app load -> waits/reloads and passes."""

    class _Loc:
        def __init__(self, count_val=0, on_click=None, fills=None):
            self._cnt = count_val
            self._on_click = on_click
            self._fills = fills

        @property
        def first(self):
            return self

        def count(self):
            return self._cnt

        def fill(self, val):
            if self._fills is not None:
                self._fills.append(val)

        def click(self, **kwargs):
            if self._on_click:
                self._on_click()

    class _PortalTab:
        def __init__(self, ctx_ref):
            self.ctx = ctx_ref
            self.url = "https://portal.internal.example/portal/openMain.do"
            self.goto_calls = 0
            self.reload_calls = 0
            self.app_polls = 0
            self.fills = []

        def on(self, *args, **kwargs):
            pass

        def goto(self, url, **kwargs):
            self.url = url
            self.goto_calls += 1

        def reload(self, **kwargs):
            self.reload_calls += 1

        def wait_for_load_state(self, *args, **kwargs):
            pass

        def _open_gw_bridge(self):
            gw_tab = type(
                "_GwTab",
                (),
                {
                    "url": "https://gw.internal.example/WebSite/Approval/Main.aspx",
                    "locator": lambda _s, _sel: _Loc(0),
                },
            )()
            self.ctx.pages.append(gw_tab)

        def get_by_text(self, txt, exact=True):
            if txt == "SL AI":
                return _Loc(1)
            if txt == "전자결재":
                self.app_polls += 1
                # Becomes visible after reload (attempt 2, poll >= 2)
                if self.reload_calls >= 1 and self.app_polls >= 4:
                    return _Loc(1, on_click=self._open_gw_bridge, fills=self.fills)
                return _Loc(0, fills=self.fills)
            return _Loc(0, fills=self.fills)

        def locator(self, sel):
            if "password" in sel or "LoginID" in sel or "loginid" in sel:
                return _Loc(0, fills=self.fills)
            if "#nav-ai-btn" in sel:
                return _Loc(1, fills=self.fills)
            return _Loc(0, fills=self.fills)

    class _Ctx:
        def __init__(self):
            self.pages = []
            self.tab = _PortalTab(self)
            self.pages.append(self.tab)

    ctx = _Ctx()
    res = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=ctx,
        BINSYNTH9941_url="https://portal.internal.example/portal/openMain.do",
        wms_user="dummy_u",
        wms_pw="dummy_p",
        allow_login_once=False,
        halt_flag_path=tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME,
        cutoff_dt=datetime(2026, 11, 3, 7, 29, 0),
        now_fn=lambda: datetime(2026, 11, 3, 7, 23, 30),
        max_wait_per_attempt_s=0.15,
        max_reloads=2,
        poll_interval_s=0.05,
    )
    assert res["portal_ready"] is True
    assert res["status_code"] == "READY"
    assert res["sso_zero_input"] is True
    assert res["bridge_clicked"] is True
    assert res["attempts"] == 2
    assert ctx.tab.reload_calls == 1
    assert ctx.tab.fills == []


def test_login_form_visible_returns_sso_unavailable_zero_input(tmp_path):
    """#229 Test 2: Visible login form (ID/PW input) -> immediate SSO_UNAVAILABLE with 0 credential input & 0 reloads."""

    fills = []

    class _Loc:
        def __init__(self, cnt):
            self._cnt = cnt

        @property
        def first(self):
            return self

        def count(self):
            return self._cnt

        def fill(self, val):
            fills.append(val)

    class _LoginTab:
        def __init__(self):
            self.goto_calls = 0
            self.reload_calls = 0

        def on(self, *args, **kwargs):
            pass

        def goto(self, *args, **kwargs):
            self.goto_calls += 1

        def reload(self, *args, **kwargs):
            self.reload_calls += 1

        def get_by_text(self, txt, exact=True):
            return _Loc(0)

        def locator(self, sel):
            if "password" in sel or "txtPC_LoginID" in sel:
                return _Loc(1)
            return _Loc(0)

    tab = _LoginTab()
    ctx = type("_Ctx", (), {"pages": [tab]})()

    auth = drafter.detect_portal_auth_state(tab)
    assert auth["auth_state"] == "SSO_UNAVAILABLE"
    assert auth["login_form_visible"] is True

    res = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=ctx,
        BINSYNTH9941_url="https://portal.internal.example/portal/openMain.do",
        wms_user="dummy_u",
        wms_pw="dummy_p",
        allow_login_once=False,
        halt_flag_path=tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME,
        max_wait_per_attempt_s=0.2,
        max_reloads=2,
        poll_interval_s=0.05,
    )
    assert res["portal_ready"] is False
    assert res["failure_reason"] == "SSO_UNAVAILABLE"
    assert res["status_code"] == "SKIPPED_SSO_UNAVAILABLE"
    assert res["attempts"] == 1
    assert tab.reload_calls == 0
    assert fills == []


def test_logged_in_portal_not_ready_and_dom_sso_url_fallback(tmp_path):
    """#229 §1-§3: Logged-in without app -> SKIPPED_PORTAL_NOT_READY (never SSO_UNAVAILABLE); DOM URL fallback works."""

    class _Loc:
        def __init__(self, cnt=0):
            self._cnt = cnt

        @property
        def first(self):
            return self

        def count(self):
            return self._cnt

    class _StuckLoggedInTab:
        def __init__(self, dom_url=None):
            self.dom_url = dom_url
            self.reloads = 0

        def on(self, *args, **kwargs):
            pass

        def goto(self, *args, **kwargs):
            pass

        def reload(self, *args, **kwargs):
            self.reloads += 1

        def evaluate(self, script, *args):
            if "data-sso-url" in script:
                return self.dom_url
            return False

        def get_by_text(self, txt, exact=True):
            return _Loc(1 if txt in ("SL AI", "APP STORE") else 0)

        def locator(self, sel):
            if "#nav-ai-btn" in sel:
                return _Loc(1)
            return _Loc(0)

    # Case A: Logged in (`SL AI` visible), no login form, app never appears -> SKIPPED_PORTAL_NOT_READY
    stuck_tab = _StuckLoggedInTab(dom_url=None)
    ctx_a = type("_CtxA", (), {"pages": [stuck_tab]})()
    res_a = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=ctx_a,
        BINSYNTH9941_url="https://portal.internal.example/portal/openMain.do",
        wms_user="dummy_u",
        wms_pw="dummy_p",
        allow_login_once=False,
        halt_flag_path=tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME,
        cutoff_dt=datetime(2026, 11, 3, 7, 29, 0),
        now_fn=lambda: datetime(2026, 11, 3, 7, 24, 0),
        max_wait_per_attempt_s=0.1,
        max_reloads=2,
        poll_interval_s=0.05,
    )
    assert res_a["portal_ready"] is False
    assert res_a["failure_reason"] == "PORTAL_NOT_READY"
    assert res_a["status_code"] == "SKIPPED_PORTAL_NOT_READY"
    assert res_a["sso_zero_input"] is True
    assert stuck_tab.reloads == 2

    subj, html = drafter.build_phase2a_test_email_html(
        {
            "report_date": "2026-11-03",
            "yesterday_ymd": "20261102",
            "portal_total_rows": 0,
            "d107_input_candidate_count": 1,
            "d107_source_file": "d107_phase1_20261103.json",
            "latest_01_file": "GI_Error_20261103_071806.XLS",
            "excluded_resolved_in_latest_01": [],
            "matched_rows": [],
            "skipped_portal_rows": [],
            "max_rows_per_draft": 20,
            "draft_count": 0,
            "gw_session_note": res_a["note"],
            "status": res_a["status_code"],
        }
    )
    assert "SKIPPED_PORTAL_NOT_READY" in subj
    assert_no_hangul({"subject": subj, "html_body": html})

    # Case B: Direct SSO bridge URL readable in DOM -> opens in same context with zero credential input
    class _BridgePage:
        def __init__(self):
            self.url = "about:blank"

        def on(self, *args, **kwargs):
            pass

        def goto(self, target_url, **kwargs):
            self.url = target_url

        def locator(self, sel):
            return _Loc(0)

    class _CtxB:
        def __init__(self):
            self.pages = [_StuckLoggedInTab(dom_url="/sso/bridgeApproval.do")]

        def new_page(self):
            bp = _BridgePage()
            self.pages.append(bp)
            return bp

    ctx_b = _CtxB()
    res_b = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=ctx_b,
        BINSYNTH9941_url="https://portal.internal.example/portal/openMain.do",
        wms_user="dummy_u",
        wms_pw="dummy_p",
        allow_login_once=False,
        halt_flag_path=tmp_path / drafter.GW_LOGIN_HALT_FLAG_NAME,
        max_wait_per_attempt_s=0.1,
        max_reloads=1,
        poll_interval_s=0.05,
    )
    assert res_b["portal_ready"] is True
    assert res_b["bridge_mode"] == "dom_url"
    assert res_b["sso_zero_input"] is True
    assert ctx_b.pages[-1].url == "https://portal.internal.example/sso/bridgeApproval.do"

def test_js_menu_bridge_aborts_immediately_on_unexpected_host():
    """#230 guard: if `openMenuPageOrNoop` opens a tab not on the approval host, abort immediately."""

    class _FakeResp:
        url = "https://portal.example.invalid/portal/top/selectDrctMenu.json"

        def json(self):
            return {
                "input_data": [
                    {
                        "menu_nm": "전자결재",
                        "menu_nm_en": "e-Approval",
                        "menu_id": "M01",
                        "uppr_menu_id": "TOP",
                        "menu_url": "https://approval.example.invalid/sso/bridge",
                        "menu_url_get": "https://approval.example.invalid/sso/bridge",
                        "popup_yn": "Y",
                        "popup_opt": "N",
                        "system_type": "E",
                        "menu_param": "",
                        "def_param_yn": "Y",
                        "left_yn": "N",
                    }
                ]
            }

    class _WrongTab:
        url = "https://unexpected.example.invalid/other"

        def locator(self, _sel):
            class _L:
                def count(self):
                    return 0
            return _L()

    class _FakePortalTab:
        url = "https://portal.example.invalid/portal/openMain.do"

        def __init__(self, ctx):
            self._ctx = ctx
            self._resp_cb = None

        def on(self, evt, cb):
            if evt == "response":
                self._resp_cb = cb

        def goto(self, _u, **_kw):
            if self._resp_cb:
                self._resp_cb(_FakeResp())

        def wait_for_load_state(self, *_a, **_kw):
            return None

        def locator(self, sel):
            class _L:
                def count(self):
                    return 1 if "#nav-ai-btn" in sel else 0
            return _L()

        def get_by_text(self, txt, exact=True):
            class _T:
                def count(self):
                    return 1 if txt in ("SL AI", "APP STORE") else 0
            return _T()

        def evaluate(self, script, arg=None):
            if arg is not None and "openMenuPageOrNoop" in script:
                self._ctx.pages.append(_WrongTab())
                return True
            return None

    class _FakeCtx:
        def __init__(self):
            self.pages = []
            self.pages.append(_FakePortalTab(self))

    ctx = _FakeCtx()
    res = drafter.ensure_BINSYNTH9941_and_groupware_sso(
        ctx=ctx,
        BINSYNTH9941_url="https://portal.example.invalid/portal/openMain.do",
        wms_user="unused",
        wms_pw="unused",
        allow_login_once=False,
        halt_flag_path=Path("nonexistent_halt.flag"),
        max_wait_per_attempt_s=0.3,
        max_reloads=2,
        poll_interval_s=0.05,
    )
    assert res["portal_ready"] is False
    assert res["status_code"] == "SKIPPED_PORTAL_NOT_READY"
    assert res["bridge_mode"] == "js_menu"
    assert res["attempts"] == 1


def test_save_temp_2b_hard_locks_all_submit_selectors_and_actions():
    """#233 Mandate: Submit (#btDraft / 기안 / etc.) MUST raise PermissionError even in save_temp_2b."""
    clicks = []

    class _Loc:
        def __init__(self, sel):
            self._sel = sel

        @property
        def first(self):
            return self

        def count(self):
            return 1

        def click(self, **kwargs):
            clicks.append(self._sel)

    class _Frame:
        def locator(self, sel):
            return _Loc(sel)

    frame = _Frame()

    for forbidden_sel in (
        "#btDraft",
        "#btApproved",
        "#btRec",
        "#btDeptDraft",
        "#btAction",
        "Submit",
        "submit_2c",
        "draft",
        "\uAE30\uC548",  # 기안
        "\uC0C1\uC2E0",  # 상신
        "requestProcess('DRAFT')",
    ):
        with pytest.raises(PermissionError):
            drafter.click_portal_form_action(frame, forbidden_sel, mode="save_temp_2b")

    # Save temp. is ALSO blocked when mode == dry_run_2a
    with pytest.raises(PermissionError):
        drafter.click_portal_form_action(frame, "#btSave", mode="dry_run_2a")

    assert clicks == []

    # ONLY #btSave in mode == save_temp_2b is allowed
    drafter.click_portal_form_action(frame, "#btSave", mode="save_temp_2b")
    assert clicks == ["#btSave"]


class _FakeFormPage2b:
    def __init__(self, shots_log=None):
        self.url = "https://gw.example.invalid/WebSite/Approval/Forms/Form.aspx"
        self.frames = [self]
        self.fills = []
        self.clicks = []
        self.gotos = []
        self.js_evals = []
        self._resp_cbs = []
        self._closed = False
        self._shots_log = shots_log if shots_log is not None else []

    def is_closed(self):
        return self._closed

    def on(self, evt, cb):
        if evt == "response":
            self._resp_cbs.append(cb)

    def evaluate(self, script, *args):
        self.js_evals.append(script)
        return None

    def locator(self, sel):
        page_ref = self

        class _L:
            @property
            def first(self):
                return self

            def count(self):
                if 'input[type="file"]' in sel:
                    return 0
                return 1

            def fill(self, val):
                page_ref.fills.append((sel, val))

            def click(self, **kwargs):
                page_ref.clicks.append(sel)
                if sel == "#btSave":
                    resp = type(
                        "_Resp",
                        (),
                        {"url": "https://gw.example.invalid/WebSite/Approval/Forms/submitForm.aspx", "status": 200},
                    )()
                    for cb in page_ref._resp_cbs:
                        cb(resp)

        return _L()

    def screenshot(self, path, full_page=False):
        Path(path).write_bytes(b"fake_png")
        self._shots_log.append(str(path))

    def goto(self, target_url, **kwargs):
        self.gotos.append(target_url)


def test_save_temp_2b_aborts_without_saving_on_single_field_form_mismatch(tmp_path):
    """#233 Mandate: If even 1 of the 7 form fields mismatches, abort with 0 clicks & 0 DB records."""
    db_file = tmp_path / "mismatch_guard.db"
    drafter.db_manager.init_db(db_path=db_file)

    selected_rows = [
        {
            "plant": "P001",
            "post_date": "20261103",
            "sumno": "202611030001",
            "barcode": "BC_OK_001",
            "material": "MAT_772",
            "material_desc": "BRACKET",
            "mvt": "311",
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "qty": 772.0,
            "d107_candidate": {"deficit_loc": "SL02", "from_sap": 0.0, "from_wms": 0.0, "first_seen_edt": "2026-11-03 10:00:00"},
        }
    ]
    # 6 fields identical, 1 field (`qty`: 771 vs 772) mismatched!
    mismatched_form_rows = [
        {
            "sumno": "202611030001",
            "barcode": "BC_OK_001",
            "material": "MAT_772",
            "mvt": "311",
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "qty": 771.0,
        }
    ]

    fake_form = _FakeFormPage2b()
    with pytest.raises(drafter.PortalFormRowMismatchError):
        drafter.verify_and_process_form_batch(
            form_page=fake_form,
            batch_rows=selected_rows,
            report_date_str="2026-11-04",
            mode="save_temp_2b",
            reason_text="MAT_772 SL02->SP31 311 772 EA (SUMNO 202611030001): SAP 0/WMS 0 in SL02 since 11/03, no GR pending, no D97/D113/D115 path -- unresolvable",
            save_shot_path=tmp_path / "shot.png",
            saved_at="2026-11-04 07:24:00",
            db_path=db_file,
            extracted_form_rows=mismatched_form_rows,
            poll_interval_s=0.01,
            max_wait_s=0.05,
        )

    assert fake_form.clicks == []
    assert fake_form.fills == []
    summary = drafter.db_manager.get_d107_draft_status_summary("2026-11-04", db_path=db_file)
    assert summary["today_saved_count"] == 0
    assert drafter.db_manager.get_pending_d107_temp_drafts(db_path=db_file) == {}


def test_save_temp_2b_end_to_end_temp_save_duplicate_skip_and_reconciliation(tmp_path):
    """
    #233 End-to-End Lifecycle:
      Day 1 (11/04): 2 rows matched -> Save temp. (#btSave) -> recorded in d107_drafts -> [TEST] English email.
      Day 2 (11/05): User submitted MAT_A (gone from portal & ZTCODE_STOCK_REC), MAT_B still unsubmitted on portal ->
                     MAT_A reconciled as cleared (1), MAT_B NOT re-saved & reported as 'pending user submit (saved 11/04)'.
    """
    db_file = tmp_path / "lifecycle_2b.db"
    drafter.db_manager.init_db(db_path=db_file)

    d107_cands = [
        {
            "material": "MAT_A",
            "mvt": "311",
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "deficit_loc": "SL02",
            "from_sap": 0.0,
            "from_wms": 0.0,
            "qty": 100.0,
            "first_seen_edt": "2026-11-03 06:00:00",
            "d107_status": "CANDIDATE",
        },
        {
            "material": "MAT_B",
            "mvt": "311",
            "from_sloc": "SL02",
            "to_sloc": "SP31",
            "deficit_loc": "SL02",
            "from_sap": 0.0,
            "from_wms": 0.0,
            "qty": 250.0,
            "first_seen_edt": "2026-11-03 06:30:00",
            "d107_status": "CANDIDATE",
        },
    ]
    portal_rows_day1 = [
        ["", "P001", "20261103", "C", "202611030001", "BC_A1", "530000", "MAT_A", "COVER 외장 A", "V1", "311", "SL02", "SP31", "100", "EA", "Insufficient stock", "", "U1"],
        ["", "P001", "20261103", "C", "202611030002", "BC_B2", "530000", "MAT_B", "COVER 외장 B", "V1", "311", "SL02", "SP31", "250", "EA", "Insufficient stock", "", "U1"],
    ]
    latest_01_day1 = [
        {"material": "MAT_A", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 100.0},
        {"material": "MAT_B", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 250.0},
    ]

    bundle_d1 = drafter.filter_and_bundle_portal_rows(
        portal_headers=SAMPLE_HEADERS,
        portal_rows=portal_rows_day1,
        d107_candidates=d107_cands,
        latest_01_gi_records=latest_01_day1,
        report_date_str="2026-11-04",
        pending_temp_drafts=drafter.db_manager.get_pending_d107_temp_drafts(db_path=db_file),
    )
    assert len(bundle_d1["matched_rows"]) == 2
    assert bundle_d1["draft_count"] == 1

    reason_d1 = drafter.build_deletion_reason_and_attachment(
        matched_rows=bundle_d1["matched_rows"],
        report_date_str="2026-11-04",
        out_dir=tmp_path,
    )
    fake_form_d1 = _FakeFormPage2b()
    shot_d1 = tmp_path / "D107_2b_FormBatch1.png"
    b_res = drafter.verify_and_process_form_batch(
        form_page=fake_form_d1,
        batch_rows=bundle_d1["draft_batches"][0],
        report_date_str="2026-11-04",
        mode="save_temp_2b",
        reason_text=reason_d1["reason_text"],
        batch_idx=1,
        total_batches=1,
        save_shot_path=shot_d1,
        saved_at="2026-11-04 07:24:15",
        db_path=db_file,
        extracted_form_rows=bundle_d1["draft_batches"][0],
        poll_interval_s=0.01,
        max_wait_s=0.05,
    )
    assert b_res["temp_saved"] is True
    assert b_res["draft_subject"] == "Request to delete IWMS_GI of 2026/11/04"
    assert b_res["confirm_state"] == "SUBMIT_FORM_HTTP_200"
    assert b_res["db_inserted_count"] == 2
    assert fake_form_d1.clicks == ["#btSave"]
    assert ("#SUBJECT", "Request to delete IWMS_GI of 2026/11/04") in fake_form_d1.fills
    assert ("#JFORM_BUSINESS_CONTENTS_ETC", reason_d1["reason_text"]) in fake_form_d1.fills
    assert shot_d1.exists()
    assert fake_form_d1.gotos == ["about:blank"]

    subj_d1, html_d1 = drafter.build_phase2a_test_email_html(
        {
            **bundle_d1,
            "mode": "save_temp_2b",
            "status": "SAVE_TEMP_2B_OK",
            "completed_draft_count": 1,
            "saved_temp_draft_count": 1,
            "saved_temp_row_count": 2,
            "draft_subject": "Request to delete IWMS_GI of 2026/11/04",
            "prior_cohort_date": "2026-11-03",
            "prior_cleared_count": 0,
            "prior_pending_count": 0,
            "d107_source_file": "d107_phase1_20261104.json",
            "latest_01_file": "GI_Error_20261104_071800.XLS",
            "reason_preview": reason_d1["reason_text"],
        }
    )
    assert "Phase 2b Save-Temp" in subj_d1
    assert "1 bundle(s) / 2 row(s)" in subj_d1
    assert "Request to delete IWMS_GI of 2026/11/04" in html_d1
    assert "Temp Box" in html_d1
    assert_no_hangul({"subject": subj_d1, "html_body": html_d1})

    # Day 2 (2026-11-05): User submitted MAT_A (gone from portal & ZTCODE_STOCK_REC), MAT_B still unsubmitted!
    portal_rows_day2 = [
        ["", "P001", "20261103", "C", "202611030002", "BC_B2", "530000", "MAT_B", "COVER 외장 B", "V1", "311", "SL02", "SP31", "250", "EA", "Insufficient stock", "", "U1"],
    ]
    latest_01_day2 = [
        {"material": "MAT_B", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 250.0},
    ]
    live_keys_d2 = drafter.extract_all_portal_row_keys(SAMPLE_HEADERS, portal_rows_day2)
    rec_d2 = drafter.db_manager.reconcile_d107_temp_drafts(
        portal_row_keys=live_keys_d2,
        latest_01_gi_records=latest_01_day2,
        report_date_str="2026-11-05",
        checked_at="2026-11-05 07:23:20",
        db_path=db_file,
    )
    assert rec_d2["newly_cleared_count"] == 1
    assert rec_d2["prior_cleared_count"] == 1
    assert rec_d2["prior_pending_count"] == 1

    pending_map_d2 = drafter.db_manager.get_pending_d107_temp_drafts(db_path=db_file)
    bundle_d2 = drafter.filter_and_bundle_portal_rows(
        portal_headers=SAMPLE_HEADERS,
        portal_rows=portal_rows_day2,
        d107_candidates=[d107_cands[1]],
        latest_01_gi_records=latest_01_day2,
        report_date_str="2026-11-05",
        pending_temp_drafts=pending_map_d2,
    )
    assert bundle_d2["matched_rows"] == []
    assert bundle_d2["draft_count"] == 0
    assert len(bundle_d2["pending_user_submit_rows"]) == 1
    assert bundle_d2["skipped_portal_rows"][0]["skip_reason"] == "pending user submit (saved 11/04)"

    subj_d2, html_d2 = drafter.build_phase2a_test_email_html(
        {
            **bundle_d2,
            "mode": "save_temp_2b",
            "status": "SAVE_TEMP_2B_OK",
            "completed_draft_count": 0,
            "saved_temp_draft_count": 0,
            "saved_temp_row_count": 0,
            "draft_subject": "Request to delete IWMS_GI of 2026/11/05",
            "prior_cohort_date": rec_d2["prior_cohort_date"],
            "prior_cleared_count": rec_d2["prior_cleared_count"],
            "prior_pending_count": rec_d2["prior_pending_count"],
            "d107_source_file": "d107_phase1_20261105.json",
            "latest_01_file": "GI_Error_20261105_071800.XLS",
            "reason_preview": "",
        }
    )
    assert "pending user submit (saved 11/04)" in html_d2
    assert "<strong>1</strong> row(s) cleared from Portal &amp; ZTCODE_STOCK_REC after user submit" in html_d2
    assert "<strong>1</strong> row(s) still pending user submit" in html_d2
    assert_no_hangul({"subject": subj_d2, "html_body": html_d2})

def test_saved_temp_older_than_2_business_days_marked_stale_no_auto_resave(tmp_path):
    """#235: SAVED_TEMP > 2 business days old is labeled 'stale (saved MM/DD, N days)' with 0 auto re-save."""
    db_file = tmp_path / "stale_2b.db"
    drafter.db_manager.init_db(db_path=db_file)
    drafter.db_manager.record_d107_temp_draft_rows(
        rows=[{"sumno": "202611030009", "barcode": "BC_STL", "material": "MAT_STALE", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 50.0, "post_date": "20261103"}],
        draft_subject="Request to delete IWMS_GI of 2026/11/04",
        saved_at="2026-11-04 07:24:00",  # Wednesday
        db_path=db_file,
    )
    # Monday 2026-11-09 is 3 business days after Wednesday 2026-11-04 (Thu, Fri, Mon) -> stale!
    summary = drafter.db_manager.get_d107_draft_status_summary("2026-11-09", db_path=db_file)
    assert summary["prior_stale_count"] == 1
    assert summary["prior_stale_labels"] == ["stale (saved 11/04, 3 days)"]

    cands = [{"material": "MAT_STALE", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 50.0, "d107_status": "CANDIDATE"}]
    p_rows = [["", "P001", "20261103", "C", "202611030009", "BC_STL", "530000", "MAT_STALE", "DESC", "V1", "311", "SL02", "SP31", "50", "EA", "Insufficient stock", "", "U1"]]
    l_01 = [{"material": "MAT_STALE", "mvt": "311", "from_sloc": "SL02", "to_sloc": "SP31", "qty": 50.0}]

    bundle_auto = drafter.filter_and_bundle_portal_rows(
        portal_headers=SAMPLE_HEADERS,
        portal_rows=p_rows,
        d107_candidates=cands,
        latest_01_gi_records=l_01,
        report_date_str="2026-11-09",
        pending_temp_drafts=drafter.db_manager.get_pending_d107_temp_drafts(db_path=db_file),
    )
    assert bundle_auto["matched_rows"] == []  # NO automatic re-save!
    assert bundle_auto["skipped_portal_rows"][0]["skip_reason"] == "stale (saved 11/04, 3 days)"

    subj, html = drafter.build_phase2a_test_email_html(
        {
            **bundle_auto,
            "mode": "save_temp_2b",
            "status": "SAVE_TEMP_2B_OK",
            "prior_cohort_date": "2026-11-04",
            "prior_cleared_count": 0,
            "prior_pending_count": 1,
            "prior_stale_count": 1,
            "prior_stale_labels": ["stale (saved 11/04, 3 days)"],
        }
    )
    assert "stale (saved 11/04, 3 days)" in html
    assert_no_hangul({"subject": subj, "html_body": html})

    bundle_manual = drafter.filter_and_bundle_portal_rows(
        portal_headers=SAMPLE_HEADERS,
        portal_rows=p_rows,
        d107_candidates=cands,
        latest_01_gi_records=l_01,
        report_date_str="2026-11-09",
        pending_temp_drafts=drafter.db_manager.get_pending_d107_temp_drafts(db_path=db_file),
        force_resave_stale=True,
    )
    assert len(bundle_manual["matched_rows"]) == 1

