"""#330 / #392 / #394 unit tests for manual PM refresh (`run_srm_pm_refresh.bat` / `--slot PM --refresh`)."""
import csv
import datetime as dt
import json
import sys
from pathlib import Path

from openpyxl import load_workbook

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_srm_pipeline as pipeline  # noqa: E402
from report_excel_style import YELLOW  # noqa: E402
from test_srm_monitoring import E, _hourly_rows  # noqa: E402
from test_srm_pipeline import D, FakeSender, _env, _shift, _write  # noqa: E402


def _setup_two_supplier_env(tmp_path: Path, monkeypatch):
    zm = _env(monkeypatch, tmp_path)
    monkeypatch.setenv("COMPANY_NAME", "RefreshPlant Corp")
    monkeypatch.setenv("SRM_T_USER", "me@test.local")
    monkeypatch.setenv("SRM_T_TO_A", "sup_a@vendor-a.test")
    monkeypatch.setenv("SRM_T_CC_A", "cc_a@vendor-a.test")
    monkeypatch.setenv("SRM_T_TO_B", "sup_b@vendor-b.test")
    sup_csv = tmp_path / "suppliers.csv"
    sup_csv.write_text(
        "match,trucks,mail,to_env,cc_env\n"
        "Vendor A,10-12 16-18,yes,SRM_T_TO_A,SRM_T_CC_A\n"
        "Vendor B,10-12 16-18,yes,SRM_T_TO_B,\n",
        encoding="utf-8",
    )
    cfg = {
        "mode": "live",
        "user_env": "SRM_T_USER",
        "send_when_no_change": True,
        "signature": "Logistics Team",
        "suppliers": {
            "Vendor A": {"to_env": "SRM_T_TO_A", "cc_env": "SRM_T_CC_A"},
            "Vendor B": {"to_env": "SRM_T_TO_B", "cc_env": ""},
        },
    }
    return zm, sup_csv, cfg


def _write_srm_bundle(folder: Path, first_day: int, qty_a_slot1: float = 40.0, qty_b_slot1: float = 60.0):
    sa, sb = [0.0] * 36, [0.0] * 36
    sa[1], sb[1] = qty_a_slot1, qty_b_slot1
    ra, rb = [0.0] * 36, [0.0] * 36
    ra[1], rb[1] = 20.0, 20.0
    shift_d = first_day - 12
    _write(
        folder / f"Order Detail 202611{first_day:02d}.csv",
        _shift(_hourly_rows(True, [
            ("099001234", "Vendor A Inc", "C", 0.0, sa),
            ("099005678-01", "Vendor B LLC", "C", 0.0, sb),
        ]), shift_d),
    )
    _write(
        folder / f"Require Detail 202611{first_day:02d}.csv",
        _shift(_hourly_rows(False, [
            ("099001234", "Vendor A Inc", "6D", 0.0, ra),
            ("099005678-01", "Vendor B LLC", "6D", 0.0, rb),
        ]), shift_d),
    )
    _write(
        folder / f"Plan & Require 202611{first_day:02d}.csv",
        [
            ["plant code", "plant", "Partner code.", "Name of partner company.", "part number", "part name",
             "plan date", "classification", "standard", "unit", "require", E, E, "PLAN", E, E, E, "BIN"],
            [E] * 10 + [f"11/{first_day}", f"11/{first_day} SUM", f"11/{first_day + 1}", f"11/{first_day}",
                        f"11/{first_day} urgent", f"11/{first_day} SUM", f"11/{first_day + 1}", E],
            ["P999", "X", "V1", "Vendor A Inc", "099001234", "PART A DESC", 1, "C", 20, "EA", 20, 20, 20, 40, E, 40, 20, E],
            ["P999", "X", "V2", "Vendor B LLC", "099005678-01", "PART B DESC", 1, "C", 20, "EA", 20, 20, 20, 60, E, 60, 20, E],
        ],
    )


def test_pm_refresh_guards_before_1340_freeze_window_am_slot_and_lock(tmp_path, monkeypatch):
    zm, sup_csv, cfg = _setup_two_supplier_env(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    _write_srm_bundle(inbox / f"{D:%Y%m%d}", 12)
    _write_srm_bundle(inbox / f"{D:%Y%m%d}" / "D+1", 13)
    logs = tmp_path / "logs"
    reports = tmp_path / "reports"
    snd = FakeSender()

    # 1) Refused on AM slot
    st_am = pipeline.run_pipeline(
        "AM", D, skip_download=True, refresh=True, sender=snd, zm212=zm, inbox_root=inbox,
        out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 15, 40),
    )
    assert st_am["status"] == "FAILED" and st_am["stage"] == "REFRESH_GUARD"

    # 2) Refused during 06:20-07:35 freeze window
    st_frz = pipeline.run_pipeline(
        "PM", D, skip_download=True, refresh=True, sender=snd, zm212=zm, inbox_root=inbox,
        out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 6, 45),
    )
    assert st_frz["status"] == "FAILED" and st_frz["stage"] == "REFRESH_FREEZE_WINDOW"

    # 3) Refused before today's 13:40 PM run has finished
    st_pre = pipeline.run_pipeline(
        "PM", D, skip_download=True, refresh=True, sender=snd, zm212=zm, inbox_root=inbox,
        out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 15, 40),
    )
    assert st_pre["status"] == "FAILED" and st_pre["stage"] == "REFRESH_BEFORE_1340"

    # 4) Lock guard
    lock_p = tmp_path / "test.lock"
    assert pipeline._lock(lock_p) is True
    assert pipeline._lock(lock_p) is False
    pipeline._unlock(lock_p)
    assert pipeline._lock(lock_p) is True
    pipeline._unlock(lock_p)


def test_pm_refresh_no_change_sends_zero_supplier_mails_and_one_supplier_change_mails_only_that_one(tmp_path, monkeypatch):
    zm, sup_csv, cfg = _setup_two_supplier_env(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    _write_srm_bundle(inbox / f"{D:%Y%m%d}", 12, qty_a_slot1=40.0, qty_b_slot1=60.0)
    _write_srm_bundle(inbox / f"{D:%Y%m%d}" / "D+1", 13, qty_a_slot1=40.0, qty_b_slot1=60.0)
    logs = tmp_path / "logs"
    reports = tmp_path / "reports"

    # Run scheduled 13:40 PM first
    snd_1340 = FakeSender()
    st_1340 = pipeline.run_pipeline(
        "PM", D, skip_download=True, sender=snd_1340, zm212=zm, inbox_root=inbox,
        out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 13, 40),
    )
    assert st_1340["status"] == "OK"
    assert len([m for m in snd_1340.sent if "SRM Monitoring" not in m["subject"]]) == 2

    # Refresh 1: fresh collector returns IDENTICAL D+1 -> NO supplier mail sent!
    def fake_collector_same(need):
        for d_deliv, out_folder, _ in need:
            _write_srm_bundle(Path(out_folder), d_deliv.day, qty_a_slot1=40.0, qty_b_slot1=60.0)
        return {"status": "OK", "files": []}

    snd_ref1 = FakeSender()
    st_ref1 = pipeline.run_pipeline(
        "PM", D, refresh=True, collector=fake_collector_same, sender=snd_ref1, zm212=zm,
        inbox_root=inbox, out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 15, 35),
    )
    assert st_ref1["status"] == "OK"
    sup_mails_1 = [m for m in snd_ref1.sent if "SRM Monitoring" not in m["subject"]]
    assert sup_mails_1 == []   # 0 supplier mails when nothing changed!
    assert len(snd_ref1.sent) == 1 and "0 supplier(s) updated" in snd_ref1.sent[0]["subject"]

    # Refresh 2 (--dry-run): Vendor B changes 60 -> 100 EA, Vendor A stays 40 EA -> dry-run sends NOTHING and leaves prod D+1 untouched!
    def fake_collector_b_changed(need):
        for d_deliv, out_folder, _ in need:
            _write_srm_bundle(Path(out_folder), d_deliv.day, qty_a_slot1=40.0, qty_b_slot1=100.0)
        return {"status": "OK", "files": []}

    snd_dry = FakeSender()
    st_dry = pipeline.run_pipeline(
        "PM", D, refresh=True, dry_run=True, collector=fake_collector_b_changed, sender=snd_dry, zm212=zm,
        inbox_root=inbox, out_dir=tmp_path / "scratch", logs_dir=logs, reports_dir=reports, mail_cfg=cfg,
        suppliers_csv=sup_csv, now_fn=lambda: dt.datetime(2026, 11, 12, 15, 36),
    )
    assert st_dry["status"] == "OK"
    assert snd_dry.sent == []   # Dry-run sends ZERO mails!
    ref_map_dry = {s["vendor"]: s for s in st_dry["refresh"]["suppliers"]}
    assert ref_map_dry["Vendor A Inc"]["changed_cells"] == 0 and ref_map_dry["Vendor A Inc"]["would_send"] is False
    assert ref_map_dry["Vendor B LLC"]["changed_cells"] == 1 and ref_map_dry["Vendor B LLC"]["would_send"] is True

    # Refresh 3 (LIVE at 15:40): ONLY Vendor B gets UPDATED mail (user in CC, top line, yellow cell + legend); Vendor A gets nothing!
    snd_live = FakeSender()
    st_live = pipeline.run_pipeline(
        "PM", D, refresh=True, collector=fake_collector_b_changed, sender=snd_live, zm212=zm,
        inbox_root=inbox, out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 15, 40),
    )
    assert st_live["status"] == "OK"
    sup_mails_live = [m for m in snd_live.sent if "SRM Monitoring" not in m["subject"]]
    assert len(sup_mails_live) == 1
    m_b = sup_mails_live[0]
    assert m_b["to"] == ["sup_b@vendor-b.test"]
    assert m_b["cc"] == ["me@test.local"]
    assert m_b["subject"].startswith("UPDATED - RefreshPlant Corp truck loading plan - Vendor B LLC - ")
    assert "Updated after an SRM order change at 15:40. This plan replaces the one sent at 13:40." in m_b["body"]
    assert "Thank you,<br>Logistics Team" in m_b["body"]

    # Check yellow highlight + legend in attached Vendor B workbook
    att_b = Path(st_live["refresh"]["suppliers"][1]["new_file"])
    wb_b = load_workbook(att_b)
    ws_b = wb_b["Truck plan"]
    legend_texts = [str(ws_b.cell(row=rr, column=c).value or "") for rr in (2, 3, 4) for c in range(1, ws_b.max_column + 1)]
    assert any("Yellow = changed since the 13:40 plan" in t for t in legend_texts)
    assert ws_b.cell(row=7, column=5).value == 80
    assert ws_b.cell(row=7, column=5).fill.fgColor.rgb.endswith(YELLOW)
    assert "Changed since 13:40 plan: 40 -> 80" in ws_b.cell(row=7, column=5).comment.text

    # Internal mail lists Vendor B changed cells + Vendor A nothing sent
    int_mail = [m for m in snd_live.sent if "SRM Monitoring" in m["subject"]][0]
    assert "099005678-01 — NAME 099005678-01" in int_mail["body"]
    assert "Nothing sent (unchanged)" in int_mail["body"]

    # Refresh 4 (at 16:10 with same 100 EA): compares against 15:40 UPDATED file -> 0 changes -> sends nothing!
    snd_after = FakeSender()
    st_after = pipeline.run_pipeline(
        "PM", D, refresh=True, collector=fake_collector_b_changed, sender=snd_after, zm212=zm,
        inbox_root=inbox, out_dir=reports, logs_dir=logs, reports_dir=reports, mail_cfg=cfg, suppliers_csv=sup_csv,
        now_fn=lambda: dt.datetime(2026, 11, 12, 16, 10),
    )
    assert st_after["status"] == "OK"
    assert [m for m in snd_after.sent if "SRM Monitoring" not in m["subject"]] == []


def test_pm_refresh_download_failure_leaves_existing_d1_untouched_and_sends_user_alert_only(tmp_path, monkeypatch):
    zm, sup_csv, cfg = _setup_two_supplier_env(tmp_path, monkeypatch)
    inbox = tmp_path / "inbox"
    _write_srm_bundle(inbox / f"{D:%Y%m%d}", 12)
    _write_srm_bundle(inbox / f"{D:%Y%m%d}" / "D+1", 13)
    logs = tmp_path / "logs"
    reports = tmp_path / "reports"
    (logs).mkdir(parents=True, exist_ok=True)
    (logs / f"srm_mail_sent_{D:%Y%m%d}_PM.json").write_text("[]", encoding="utf-8")

    snd = FakeSender()
    st_fail = pipeline.run_pipeline(
        "PM", D, refresh=True,
        collector=lambda need: {"status": "LOGIN_BLOCKED", "detail": "blocked", "files": []},
        sender=snd, zm212=zm, inbox_root=inbox, out_dir=reports, logs_dir=logs, reports_dir=reports,
        mail_cfg=cfg, suppliers_csv=sup_csv, now_fn=lambda: dt.datetime(2026, 11, 12, 15, 40),
    )
    assert st_fail["status"] == "FAILED" and st_fail["stage"] == "PM_REFRESH_DOWNLOAD"
    assert len(snd.sent) == 1
    assert snd.sent[0]["to"] == ["me@test.local"]
    assert "FAILED - PM_REFRESH_DOWNLOAD" in snd.sent[0]["subject"]


def test_console_summary_shows_updated_supplier_and_all_refusal_reasons():
    from srm_pm_refresh import format_pm_refresh_console_summary

    # Live run: Vendor B sent UPDATED, Vendor A unchanged
    s_live = format_pm_refresh_console_summary({
        "status": "OK",
        "dry_run": False,
        "run_date": "2026-11-16",
        "started": "2026-11-16 15:43:03",
        "finished": "2026-11-16 15:44:08",
        "refresh": {
            "suppliers": [
                {"vendor": "Vendor A Inc", "prev_sent": "13:40", "changed_cells": 0, "would_send": False, "sent": False},
                {"vendor": "Vendor B LLC", "prev_sent": "13:40", "changed_cells": 6, "would_send": True, "sent": True},
            ]
        },
    }, log_name="srm_pipeline_20261116_PM_154303_REFRESH.json")
    assert "LIVE PM REFRESH" in s_live
    assert "Vendor A Inc: 0 changed cell(s) vs 13:40 plan  ->  No change - mail skipped" in s_live
    assert "Vendor B LLC: 6 changed cell(s) vs 13:40 plan  ->  SENT UPDATED mail" in s_live
    assert "RESULT: 1 supplier(s) received an UPDATED mail." in s_live

    # Preview (--dry-run)
    s_dry = format_pm_refresh_console_summary({
        "status": "OK",
        "dry_run": True,
        "run_date": "2026-11-16",
        "refresh": {
            "suppliers": [
                {"vendor": "Vendor B LLC", "prev_sent": "15:44", "changed_cells": 3, "would_send": True, "sent": False},
            ]
        },
    })
    assert "PREVIEW / DRY-RUN (NO MAIL SENT)" in s_dry
    assert "WOULD SEND UPDATED mail (preview only - no mail sent)" in s_dry
    assert "RESULT (PREVIEW): 1 supplier(s) would receive an UPDATED mail (0 mails sent)." in s_dry

    # Refusal reasons: before 13:40, 06:20-07:35, lock busy, download failed
    for stage, expected_phrase in [
        ("REFRESH_BEFORE_1340", "13:40 PM run has not finished yet"),
        ("REFRESH_FREEZE_WINDOW", "06:20-07:35 morning freeze window"),
        ("LOCKED", "lock busy"),
        ("PM_REFRESH_DOWNLOAD", "download failed"),
    ]:
        out = format_pm_refresh_console_summary({
            "status": "LOCKED" if stage == "LOCKED" else "FAILED",
            "stage": stage,
            "run_date": "2026-11-16",
            "detail": f"detail-{stage}",
        })
        assert "[REFUSED / FAILED]" in out
        assert expected_phrase in out
