"""#330 pipeline / mail / download-helper tests on synthetic data (dummy parts 9900xxxx, example.com addresses).
The browser steps of srm_collector.py were exercised against a local mock portal + SRM site (popup, iframe, date
input, search, tabs, downloads, login-form stop, wrong-date rejection); the real screens are checked with --probe."""
import csv
import datetime as dt
import shutil
import sys
from pathlib import Path

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_srm_monitoring as runner  # noqa: E402
import run_srm_pipeline as pipeline  # noqa: E402
import srm_collector as collector  # noqa: E402
import srm_mailer as mailer  # noqa: E402
from srm_parsers import vendor_name  # noqa: E402
from test_srm_monitoring import E, _hourly_rows, _zm_rows  # noqa: E402

D = dt.date(2026, 11, 12)
CFG = {"mode": "test", "user_env": "SRM_T_USER", "send_when_no_change": True,
       "suppliers": {"Vendor A": {"to_env": "SRM_T_TO_A", "cc_env": "SRM_T_CC_A"}}}


def _write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def _shift(rows, days):
    """Move the date labels of an hourly export (delivery date tomorrow = 13:40 file)."""
    rows[0] = [f"11/{int(c[3:]) + days}" if isinstance(c, str) and c.startswith("11/") else c for c in rows[0]]
    return rows


def _planreq(first_day=12):
    return [["plant code", "plant", "Partner code.", "Name of partner company.", "part number", "part name",
             "plan date", "classification", "standard", "unit", "require", E, E, "PLAN", E, E, E, "BIN"],
            [E] * 10 + [f"11/{first_day}", f"11/{first_day} SUM", f"11/{first_day + 1}", f"11/{first_day}",
                         f"11/{first_day} urgent", f"11/{first_day} SUM", f"11/{first_day + 1}", E],
            ["P999", "X", "V1", "Vendor A X", "099001234", "NAME", 1, "C", 10, "EA", 90, 90, 45, 0, E, 0, 45, E]]


def _inbox(root, d1=False, day=12):
    """Morning files (truck supplier part, class C, SRM day quantity too small on the 10-12 truck) [+ 13:40 files]."""
    o, r = [0.0] * 36, [0.0] * 36
    o[1], r[1], r[13], r[25] = 10.0, 95.0, 45.0, 45.0     # 55 in stock + 10 ordered < 95 needed
    folder = root / f"{D:%Y%m%d}"
    _write(folder / "Order Detail 20261112.csv",
           _shift(_hourly_rows(True, [("099001234", "Vendor A X", "C", 0.0, o)]), day - 12))
    _write(folder / "Require Detail 20261112.csv",
           _shift(_hourly_rows(False, [("099001234", "Vendor A X", "6D", 0.0, r)]), day - 12))
    _write(folder / "Plan & Require 20261112.csv", _planreq(day))
    if d1:
        _write(folder / "D+1" / "Order Detail 20261113.csv",       # a real export always has some quantity
               _shift(_hourly_rows(True, [("099001234", "Vendor A X", "C", 0.0, [10.0] + [0.0] * 35)]), 1))
        _write(folder / "D+1" / "Require Detail 20261113.csv",
               _shift(_hourly_rows(False, [("099001234", "Vendor A X", "6D", 0.0, r[12:] + [45.0] + [0.0] * 11)]), 1))
        _write(folder / "D+1" / "Plan & Require 20261113.csv", _planreq(13))
    return folder


class FakeSender:
    def __init__(self):
        self.sent = []

    def __call__(self, to, cc, subject, body, attachments):
        self.sent.append({"to": list(to), "cc": list(cc), "subject": subject, "body": body,
                          "att": [Path(a).name for a in attachments]})
        return True


def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "WATCHLIST", tmp_path / "none.csv")
    monkeypatch.setattr(runner, "CLASS_FILE", tmp_path / "part_class.csv")
    monkeypatch.setattr(pipeline, "LOGS", tmp_path / "logs")
    monkeypatch.setenv("SRM_T_USER", "planner@example.com")
    monkeypatch.setenv("SRM_T_TO_A", "a.to@example.com")
    monkeypatch.setenv("SRM_T_CC_A", "a.cc@example.com")
    import srm_trucks
    sup = tmp_path / "suppliers.csv"
    sup.write_text("match,trucks,mail,to_env,cc_env\nVendor A,10-12 12-14 16-18 22-24,yes,SRM_T_TO_A,SRM_T_CC_A\n",
                   encoding="utf-8")
    monkeypatch.setattr(srm_trucks, "SUPPLIERS", sup)
    zm = tmp_path / "zm212.csv"
    _write(zm, _zm_rows())
    return zm


def test_vendor_name_english_part_only():
    assert vendor_name("Vendor A Plastics, Inc.") == "Vendor A Plastics, Inc."
    assert vendor_name("\uc608\uc2dc \uacf5\uc7a5 (Example Plant B") == "Example Plant B"


def test_infer_classes_from_slots():
    two = [0.0] * 36
    two[1] = two[4] = 10.0
    one = [0.0] * 36
    one[3] = 10.0
    eff, inferred = runner.infer_classes({"A": "L"}, {"A": {"slots": two}, "B": {"slots": two}, "C": {"slots": one}})
    assert eff == {"A": "L", "B": "C", "C": "L"} and sorted(inferred) == ["B", "C"]


def test_wrong_input_date_stops_engine(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    folder = _inbox(tmp_path / "inbox", day=13)            # files for 11/13 in the 11/12 run
    st = runner.run(D, folder, zm, tmp_path / "out")
    assert st["status"] == "WRONG_INPUT_DATE" and st["expected"] == "11/12"


def test_pipeline_am_test_mode_mails_user_only(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    _inbox(tmp_path / "inbox")
    snd = FakeSender()
    st = pipeline.run_pipeline("AM", D, skip_download=True, sender=snd, zm212=zm, inbox_root=tmp_path / "inbox",
                               out_dir=tmp_path / "out", mail_cfg=CFG, sleep=lambda s: None)
    assert st["status"] == "OK" and st["engine"]["additional_qty_parts"] == 1
    assert [m["mail"] for m in st["mails"]] == ["Vendor A X", "internal"] and all(m["sent"] for m in st["mails"])
    sup, internal = snd.sent
    assert sup["to"] == ["planner@example.com"] and sup["cc"] == []                 # test mode: user only
    assert sup["subject"].startswith("[TEST] ") and "truck loading plan - Vendor A X" in sup["subject"]
    assert "SRM_T_TO_A" in sup["body"] and "a.to@example.com" not in sup["body"]   # key names only
    assert sup["att"] == ["SRM_TruckPlan_Vendor_A_X_20261112_AM.xlsx"] and "not yet in SRM" in sup["body"] and "+30" in sup["body"]
    assert internal["to"] == ["planner@example.com"] and internal["att"][0].startswith("SRM_Monitoring_20261112_AM")


def test_pipeline_live_mode_supplier_once(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    _inbox(tmp_path / "inbox")
    live = dict(CFG, mode="live")
    kw = dict(skip_download=True, zm212=zm, inbox_root=tmp_path / "inbox", out_dir=tmp_path / "out",
              mail_cfg=live, sleep=lambda s: None)
    snd = FakeSender()
    assert pipeline.run_pipeline("AM", D, sender=snd, **kw)["status"] == "OK"
    sup = snd.sent[0]
    assert sup["to"] == ["a.to@example.com"] and sup["cc"] == ["a.cc@example.com", "planner@example.com"]
    assert not sup["subject"].startswith("[TEST]")
    snd2 = FakeSender()
    st = pipeline.run_pipeline("AM", D, sender=snd2, **kw)                 # same date / slot again
    assert [m["mail"] for m in st["mails"]] == ["internal"] and len(snd2.sent) == 1
    snd3 = FakeSender()
    assert len(pipeline.run_pipeline("AM", D, sender=snd3, resend=True, **kw)["mails"]) == 2
    monkeypatch.delenv("SRM_T_TO_A")
    res = mailer.send_run_mails({"run_label": "AM", "run_date": "2026-11-12", "supplier_files": [], "report": ""},
                                [], dt.datetime(2026, 11, 12, 7), cfg=live, sender=FakeSender())
    assert res == [{"mail": "internal", "sent": True}]


class Clock:
    """Fake wall clock for the retry window: sleep() moves time forward."""
    def __init__(self, start):
        self.t = start

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += dt.timedelta(seconds=s)


def _copier(src, plan):
    """Fake collector: on call n copies the kinds in plan[n] (missing call -> nothing) and records the request."""
    calls = []
    names = {"order": "Order Detail", "require": "Require Detail", "planreq": "Plan & Require"}

    def collect(jobs):
        n = len(calls)
        calls.append([(d, Path(o).name, list(k)) for d, o, k in jobs])
        for d, out, kinds in jobs:
            folder = src if d == D else src / "D+1"
            for kind in kinds:
                if kind in plan.get(n, ()):
                    f = next(folder.glob(f"{names[kind]}*.csv"))
                    Path(out).mkdir(parents=True, exist_ok=True)
                    shutil.copy(f, Path(out) / f.name)
        return {"status": "OK" if plan.get(n) else "EMPTY_FILE", "detail": "", "files": []}
    return collect, calls


def test_pipeline_pm_needs_tomorrow_files(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    _inbox(tmp_path / "inbox")                              # no D+1 folder
    snd = FakeSender()
    st = pipeline.run_pipeline("PM", D, skip_download=True, sender=snd, zm212=zm, inbox_root=tmp_path / "inbox",
                               out_dir=tmp_path / "out", mail_cfg=CFG, sleep=lambda s: None)
    assert st["status"] == "FAILED" and st["stage"] == "INPUT_FILES" and "2026-11-13" in st["detail"]
    assert st["alert_sent"] and len(snd.sent) == 1 and "FAILED" in snd.sent[0]["subject"]
    assert snd.sent[0]["to"] == ["planner@example.com"]


def test_pipeline_pm_downloads_and_mails_tomorrow_window(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    src = _inbox(tmp_path / "src", d1=True)
    collect, calls = _copier(src, {0: ("order", "require", "planreq")})
    clock = Clock(dt.datetime(2026, 11, 12, 13, 40))
    snd = FakeSender()
    st = pipeline.run_pipeline("PM", D, collector=collect, sender=snd, zm212=zm, inbox_root=tmp_path / "inbox",
                               out_dir=tmp_path / "out", mail_cfg=CFG, sleep=clock.sleep, now_fn=clock.now)
    all3 = ["order", "planreq", "require"]
    assert calls == [[(D, "20261112", all3), (D + dt.timedelta(days=1), "D+1", all3)]]   # today's files missing too
    assert st["status"] == "OK" and st["engine"]["run_label"] == "PM" and st["engine"]["d1_hourly_used"]
    assert snd.sent[0]["att"] == ["SRM_TruckPlan_Vendor_A_X_20261113_PM.xlsx"] and "Fri 11/13" in snd.sent[0]["subject"]


def test_pipeline_retries_every_5_min_only_missing_files(tmp_path, monkeypatch):
    """User 11/12: start 06:40; file not retrievable / empty -> try again every 5 minutes until 07:00."""
    zm = _env(monkeypatch, tmp_path)
    src = _inbox(tmp_path / "src")
    collect, calls = _copier(src, {1: ("planreq", "order"), 2: ("require",)})     # try 1 nothing, 2 two, 3 last
    clock = Clock(dt.datetime(2026, 11, 12, 6, 40))
    st = pipeline.run_pipeline("AM", D, collector=collect, sender=FakeSender(), zm212=zm,
                               inbox_root=tmp_path / "inbox", out_dir=tmp_path / "out", mail_cfg=CFG,
                               sleep=clock.sleep, now_fn=clock.now)
    assert st["status"] == "OK" and [a["at"] for a in st["download"]["attempts"]] == ["06:40:00", "06:45:00", "06:50:00"]
    assert calls[2] == [(D, "20261112", ["require"])]          # only the file still missing is downloaded again
    assert any("complete on try 3" in n for n in st["engine"]["notes"])
    # never complete -> tries at 06:40, 06:45, 06:50, 06:55, 07:00, then one alert to the user
    collect2, calls2 = _copier(src, {})
    clock2, snd = Clock(dt.datetime(2026, 11, 12, 6, 40)), FakeSender()
    st = pipeline.run_pipeline("AM", D, collector=collect2, sender=snd, zm212=zm, inbox_root=tmp_path / "inbox2",
                               out_dir=tmp_path / "out", mail_cfg=CFG, sleep=clock2.sleep, now_fn=clock2.now)
    assert st["status"] == "FAILED" and st["stage"] == "DOWNLOAD" and len(calls2) == 5
    assert st["download"]["attempts"][-1]["at"] == "07:00:00" and len(snd.sent) == 1
    # PM: 13:40 .. 14:00
    collect3, calls3 = _copier(src, {})
    clock3 = Clock(dt.datetime(2026, 11, 12, 13, 40))
    pipeline.run_pipeline("PM", D, collector=collect3, mail=False, zm212=zm, inbox_root=tmp_path / "inbox3",
                          out_dir=tmp_path / "out", mail_cfg=CFG, sleep=clock3.sleep, now_fn=clock3.now)
    assert len(calls3) == 5 and clock3.t <= dt.datetime(2026, 11, 12, 14, 0, 59)


def test_pipeline_download_failure_and_zm212_wait(tmp_path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    snd = FakeSender()
    clock = Clock(dt.datetime(2026, 11, 12, 7, 5))          # after 07:00: a single try
    kw = dict(sender=snd, inbox_root=tmp_path / "inbox", out_dir=tmp_path / "out", mail_cfg=CFG,
              sleep=clock.sleep, now_fn=clock.now)
    st = pipeline.run_pipeline("AM", D, collector=lambda jobs: {"status": "SSO_UNAVAILABLE", "detail": "login form"},
                               zm212=zm, **kw)
    assert st["status"] == "FAILED" and st["stage"] == "DOWNLOAD" and "SSO_UNAVAILABLE" in st["detail"]
    assert len(st["download"]["attempts"]) == 1
    _inbox(tmp_path / "inbox")                              # files saved by hand -> nothing to download
    st = pipeline.run_pipeline("AM", D, collector=lambda jobs: {"status": "SSO_UNAVAILABLE"}, zm212=zm, **kw)
    assert st["status"] == "OK" and st["download"]["attempts"] == []
    st = pipeline.run_pipeline("AM", D, skip_download=True, zm212=tmp_path / "missing.txt", zm212_wait_min=0, **kw)
    assert st["status"] == "FAILED" and st["stage"] == "ZM212"


def test_failed_srm_login_is_not_retried(tmp_path, monkeypatch):
    """user 11/12 20:27: SRM logs in with the WMS account - a failed login is never retried every 5 minutes
    (the shared WMS account could lock); one alert to the user instead."""
    zm = _env(monkeypatch, tmp_path)
    clock, snd, calls = Clock(dt.datetime(2026, 11, 12, 6, 40)), FakeSender(), []

    def collect(jobs):
        calls.append(len(jobs))
        return {"status": "LOGIN_FAILED", "detail": "SRM login failed once"}
    st = pipeline.run_pipeline("AM", D, collector=collect, sender=snd, zm212=zm, inbox_root=tmp_path / "inbox",
                               out_dir=tmp_path / "out", mail_cfg=CFG, sleep=clock.sleep, now_fn=clock.now)
    assert st["status"] == "FAILED" and st["stage"] == "DOWNLOAD" and "LOGIN_FAILED" in st["detail"]
    assert len(calls) == 1 and clock.t == dt.datetime(2026, 11, 12, 6, 40) and len(snd.sent) == 1


def test_srm_account_keys_and_login_block(tmp_path, monkeypatch):
    for k in ("SRM_USERNAME", "SRM_PASSWORD", "WMS_USERNAME", "WMS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert collector.srm_credentials() == ("", "", "")
    monkeypatch.setenv("WMS_USERNAME", "dummy_user_w")
    monkeypatch.setenv("WMS_PASSWORD", "dummy_pw_w")
    assert collector.srm_credentials() == ("dummy_user_w", "dummy_pw_w", "WMS")
    monkeypatch.setenv("SRM_USERNAME", "dummy_user_s")             # half an SRM pair is ignored
    assert collector.srm_credentials()[2] == "WMS"
    monkeypatch.setenv("SRM_PASSWORD", "dummy_pw_s")
    assert collector.srm_credentials() == ("dummy_user_s", "dummy_pw_s", "SRM")
    monkeypatch.setattr(collector, "LOGIN_BLOCK", tmp_path / "logs" / "srm_login_blocked.json")
    assert collector.login_blocked() is None
    collector.block_login("SRM", "login form still visible after one try")
    text = collector.LOGIN_BLOCK.read_text(encoding="utf-8")
    assert collector.login_blocked()["account"] == "SRM_USERNAME" and "dummy" not in text   # key name only


def test_wait_for_file_until_size_settles(tmp_path):
    f = tmp_path / "zm.txt"
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s
        if t["now"] >= 60 and not f.exists():
            f.write_text("x" * 10)
    assert pipeline.wait_for_file(f, 5, sleep=sleep, clock=lambda: t["now"])
    assert not pipeline.wait_for_file(tmp_path / "never.txt", 1, sleep=sleep, clock=lambda: t["now"])


def test_collector_helpers(tmp_path):
    assert collector.redact("https://host.example.com/srm/SCREEN_PLAN?x=1") == "/srm/SCREEN_PLAN"
    assert collector.target_name("planreq", dt.date(2026, 11, 13)) == "Plan & Require 20261113.xlsx"
    p = tmp_path / "Order Detail.csv"
    _write(p, _hourly_rows(True, [("099001234", "Vendor A X", "C", 0.0, [5.0] + [0.0] * 35)]))
    assert collector.validate_download("order", p, D) == (True, "11/12")
    assert collector.validate_download("order", p, D + dt.timedelta(days=1)) == (False, "11/12")
    empty = tmp_path / "Order Detail empty.csv"
    _write(empty, _hourly_rows(True, []))                          # grid with headers only
    assert collector.check_export("order", empty, D)["status"] == "EMPTY_FILE"
    zero = tmp_path / "Order Detail zero.csv"
    _write(zero, _hourly_rows(True, [("099001234", "Vendor A X", "C", 0.0, [0.0] * 36)]))
    c = collector.check_export("order", zero, D)
    assert c["status"] == "EMPTY_FILE" and "no quantities" in c["detail"]
    junk = tmp_path / "junk.csv"
    _write(junk, [["no", "srm", "header"]])
    ok, why = collector.validate_download("order", junk, D)
    assert not ok and why.startswith("unreadable")
    cfg = collector.load_config()
    assert [e["kind"] for s in cfg["screens"] for e in s["exports"]] == ["planreq", "order", "require"]


def test_outlook_hang_is_a_timeout_not_a_hang(monkeypatch):
    import time as _t
    monkeypatch.setattr(mailer, "MAIL_TIMEOUT_S", 0.3)
    ok, err = mailer._safe_send(lambda *a: _t.sleep(5), [], [], "s", "b", [])
    assert not ok and err.startswith("TIMEOUT")
    ok, err = mailer._safe_send(lambda *a: (_ for _ in ()).throw(RuntimeError("offline")), [], [], "s", "b", [])
    assert not ok and "offline" in err
