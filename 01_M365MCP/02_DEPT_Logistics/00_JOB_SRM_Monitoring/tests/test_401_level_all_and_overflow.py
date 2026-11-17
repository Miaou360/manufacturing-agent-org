"""#401 / #401a / #401b / #401c unit tests:
- freeze_departed=false levels past trucks (and removes frozen info note); freeze_departed=true preserves freeze.
- Uncapped 3rd supplier with fixed trucks & mail=no levels with 1.7x rule and sends 0 supplier mails.
- Overflow > cap goes to the LAST truck while strictly preserving >= 100% coverage (115% Pass 1, 100% Pass 2, + relay).
- Day-total trigger (`day_total > cap * n_trucks`) emits exact English warning in Excel & mail + adds SRM_EXTRA_TRUCK_TO
  (or logs when missing/empty); rare `day_total <= cap * n_trucks` forced > 33 for coverage emits info note only."""
import datetime as dt
from pathlib import Path
import sys

from openpyxl import load_workbook

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import srm_mailer as mailer  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_pm_refresh import parse_truck_plan_workbook  # noqa: E402
from srm_supplier_files import write_supplier_files  # noqa: E402
from srm_trucks import CONFIG, leveling_config  # noqa: E402
from test_367_truck_leveling import TR_ALPHA, TR_BETA, _make_part  # noqa: E402

D = dt.date(2026, 11, 17)


def test_401_freeze_departed_false_levels_past_truck_while_true_preserves_freeze(tmp_path):
    # Production config sets freeze_departed=False; omitted in JSON defaults to True in code
    prod_cfg = leveling_config(CONFIG)
    assert prod_cfg["freeze_departed"] is False

    minimal_json = tmp_path / "min_trucks.json"
    minimal_json.write_text('{"leveling": {"enabled": true}}', encoding="utf-8")
    assert leveling_config(minimal_json)["freeze_departed"] is True

    # Past 8-10 (06:00) truck at 58 carts when run_ts = 06:40:
    # - freeze_departed=False -> [58, 32, 20, 20, 20] (sum=150 <= 165) levels 8-10 down to <= 33, 0 frozen info notes
    # - freeze_departed=True  -> 8-10 stays frozen at 58 + "Frozen (departed) truck" info note
    srm = [0.0] * 36
    srm[0], srm[2], srm[4], srm[6], srm[9] = 1160.0, 640.0, 400.0, 400.0, 400.0
    r_off, o_off, q_off = _make_part("99010001", "Vendor C", 2000.0, srm, [0.0] * 36)
    s_off = plan_parts(
        D, [r_off], {"99010001": o_off}, {"99010001": q_off}, None, None, False, TR_BETA,
        {"99010001": 20.0}, {"99010001": "C"},
        level_cfg={"enabled": True, "freeze_departed": False, "safety_margin": 0.15, "freeze_lead_min": 30.0,
                   "overload_factor": 1.7, "max_carts_per_truck": {"Vendor C": 33}},
        run_ts=dt.datetime(2026, 11, 17, 6, 40),
    )
    d_off = s_off["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d_off["frozen_trucks"] == []
    assert d_off["trucks_before"] == [58.0, 32.0, 20.0, 20.0, 20.0]
    assert all(x <= 33.0 for x in d_off["trucks_after"])
    assert sum(d_off["trucks_after"]) == 150.0
    assert s_off["info_notes"] == []
    assert s_off["warnings"] == []

    r_on, o_on, q_on = _make_part("99010001", "Vendor C", 2000.0, srm, [0.0] * 36)
    s_on = plan_parts(
        D, [r_on], {"99010001": o_on}, {"99010001": q_on}, None, None, False, TR_BETA,
        {"99010001": 20.0}, {"99010001": "C"},
        level_cfg={"enabled": True, "freeze_departed": True, "safety_margin": 0.15, "freeze_lead_min": 30.0,
                   "overload_factor": 1.7, "max_carts_per_truck": {"Vendor C": 33}},
        run_ts=dt.datetime(2026, 11, 17, 6, 40),
    )
    d_on = s_on["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d_on["frozen_trucks"] == ["8-10"]
    assert d_on["trucks_after"][0] == 58.0
    assert any("Frozen (departed) truck over 33 carts (info only)" in x for x in s_on["info_notes"])


def test_401_third_uncapped_supplier_uses_1_7x_and_mail_no_sends_zero_supplier_mail(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_NAME", "TestPlant Corp")
    monkeypatch.setenv("SRM_T_USER", "me@test.local")
    monkeypatch.setenv("SRM_T_TO_C", "sup_c@vendor-c.test")

    sup_csv = tmp_path / "suppliers.csv"
    sup_csv.write_text(
        "match,trucks,mail,to_env,cc_env\n"
        "Vendor C,8-10 12-14 16-18 20-22 02-04,yes,SRM_T_TO_C,\n"
        "Vendor J,8-10 10-12 16-18 22-24,no,,\n",
        encoding="utf-8",
    )
    from srm_trucks import load_suppliers, load_trucks
    rows_sup = load_suppliers(sup_csv)
    tr_map = load_trucks(sup_csv)
    assert tr_map["Vendor J"] == [0, 1, 4, 7]

    # Vendor J (uncapped, 4 trucks): [5, 5, 10, 40] -> sum=60, avg=15, 1.7*avg=25.5 -> 40 unloads to <= 25.5!
    srm_j = [0.0] * 36
    srm_j[0], srm_j[1], srm_j[4], srm_j[7] = 100.0, 100.0, 200.0, 800.0
    rj, oj, qj = _make_part("99010002", "Vendor J Corp", 2000.0, srm_j, [0.0] * 36)
    rc, oc, qc = _make_part("99010003", "Vendor C LLC", 2000.0, [200.0] + [0.0] * 35, [0.0] * 36)

    lvl_cfg = leveling_config(CONFIG, suppliers_path=sup_csv)
    assert lvl_cfg["max_carts_per_truck"] == {"Vendor C": 33}   # Vendor J has no cap -> 1.7x

    s = plan_parts(
        D, [rj, rc], {"99010002": oj, "99010003": oc}, {"99010002": qj, "99010003": qc},
        None, None, False, tr_map, {"99010002": 20.0, "99010003": 20.0}, {"99010002": "C", "99010003": "C"},
        level_cfg=lvl_cfg,
    )
    dj = s["suppliers"]["Vendor J"]["days"][D.isoformat()]
    assert dj["trucks_before"] == [5.0, 5.0, 10.0, 40.0]
    assert all(x <= 25.5 for x in dj["trucks_after"])
    assert sum(dj["trucks_after"]) == 60.0

    sup_files = write_supplier_files(tmp_path, "AM", [rj, rc], {"99010002": 20.0, "99010003": 20.0}, dt.datetime(2026, 11, 17, 6, 45))
    assert any("Vendor_J" in f.name for f in sup_files)

    mail_cfg = {
        "mode": "live",
        "user_env": "SRM_T_USER",
        "send_when_no_change": True,
        "suppliers": {r["match"]: {"to_env": r["to_env"], "cc_env": r["cc_env"]} for r in rows_sup if r["mail"]},
    }
    sent = []
    mailer.send_run_mails(
        {"run_date": "2026-11-17", "run_label": "AM", "supplier_files": [str(x) for x in sup_files], "notes": []},
        [rj, rc], dt.datetime(2026, 11, 17, 6, 45), cfg=mail_cfg,
        sender=lambda to, cc, subj, body, atts: sent.append(subj) or True,
    )
    assert any("Vendor C" in s_subj for s_subj in sent)
    assert not any("Vendor J" in s_subj for s_subj in sent)


def test_401abc_overflow_goes_to_last_truck_respects_100pct_coverage_and_relay():
    lvl_cap5 = {
        "enabled": True,
        "freeze_departed": False,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor C": 33},
    }

    # (A) 186 carts across 5 trucks ([58, 39, 25, 26, 38]) -> [33, 33, 33, 33, 54], warning for 21 carts
    srm1 = [0.0] * 36
    srm1[0], srm1[2], srm1[4], srm1[6], srm1[9] = 1160.0, 780.0, 500.0, 520.0, 760.0
    r1, o1, q1 = _make_part("99010010", "Vendor C", 2000.0, srm1, [0.0] * 36)
    s1 = plan_parts(
        D, [r1], {"99010010": o1}, {"99010010": q1}, None, None, False, TR_BETA,
        {"99010010": 20.0}, {"99010010": "C"}, level_cfg=lvl_cap5, run_ts=dt.datetime(2026, 11, 17, 6, 40),
    )
    d1 = s1["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d1["trucks_after"] == [33.0, 33.0, 33.0, 33.0, 54.0]
    assert len(s1["warnings"]) == 1
    expected_warn = "Extra truck needed: Tue 11/17 total 186 carts > 5 trucks × 33 = 165. Please arrange an additional truck for 21 carts."
    assert expected_warn in s1["warnings"][0]
    assert d1["extra_truck_warnings"] == [expected_warn]

    # (B) Coverage barrier + overflow: slot 0 needs 36 carts immediately (req[0]=720, start=0), remaining slots hold 150 carts
    # -> total=186 > 165; slot 0 stays at 36 for >=100% coverage, middle trucks <=33, overflow on last truck (51), 0 shortages!
    srm_cov = [0.0] * 36
    srm_cov[0] = 720.0   # 36 carts needed in slot 0
    req_cov = [0.0] * 36
    req_cov[0] = 720.0
    srm_rest = [0.0] * 36
    srm_rest[2], srm_rest[4], srm_rest[6], srm_rest[9] = 800.0, 700.0, 700.0, 800.0   # [40, 35, 35, 40] = 150 carts
    rc1, oc1, qc1 = _make_part("99010011", "Vendor C", 0.0, srm_cov, req_cov)
    rc2, oc2, qc2 = _make_part("99010012", "Vendor C", 3000.0, srm_rest, [0.0] * 36)
    s2 = plan_parts(
        D, [rc1, rc2], {"99010011": oc1, "99010012": oc2}, {"99010011": qc1, "99010012": qc2},
        None, None, False, TR_BETA, {"99010011": 20.0, "99010012": 20.0}, {"99010011": "C", "99010012": "C"},
        level_cfg=lvl_cap5,
    )
    d2 = s2["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d2["trucks_after"] == [36.0, 33.0, 33.0, 33.0, 51.0]
    assert all(v >= -1e-9 for v in rc1["plan"]["stock"])
    assert all(v >= -1e-9 for v in rc2["plan"]["stock"])
    assert any("Truck 8-10 36 carts (> 33) kept for coverage" in x for x in s2["info_notes"])
    assert len(s2["warnings"]) == 1 and expected_warn in s2["warnings"][0]

    # (C) 1-hop relay: 8-10 has 35 carts of Part X consumed at 16-18 (can move to 12-14, CANNOT jump to 02-04),
    # while 12-14/16-18/20-22/02-04 hold [33, 33, 33, 36] of well-stocked Part Y (total=170 > 165).
    # Relay moves 2 carts of Part Y (12-14 -> 02-04) + 2 carts of Part X (8-10 -> 12-14) -> [33, 33, 33, 33, 38]!
    srm_x = [0.0] * 36
    srm_x[0], srm_x[6] = 700.0, 20.0   # 35 carts on 8-10, 1 cart on 20-22
    req_x = [0.0] * 36
    req_x[2], req_x[6] = 560.0, 160.0  # Inv(1)=660>=1.15*560 allows 2 carts 8-10->12-14, while 8-10->02-04 would cause Inv(6)=-40<0!
    srm_y = [0.0] * 36
    srm_y[2], srm_y[4], srm_y[6], srm_y[9] = 660.0, 660.0, 640.0, 720.0
    rx, ox, qx = _make_part("99010013", "Vendor C", 0.0, srm_x, req_x)
    ry, oy, qy = _make_part("99010014", "Vendor C", 5000.0, srm_y, [0.0] * 36)
    s3 = plan_parts(
        D, [rx, ry], {"99010013": ox, "99010014": oy}, {"99010013": qx, "99010014": qy},
        None, None, False, TR_BETA, {"99010013": 20.0, "99010014": 20.0}, {"99010013": "C", "99010014": "C"},
        level_cfg=lvl_cap5,
    )
    d3 = s3["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d3["trucks_after"] == [33.0, 33.0, 33.0, 33.0, 38.0]
    assert all(v >= -1e-9 for v in rx["plan"]["stock"])


def test_401bc_excel_and_mail_warning_and_srm_extra_truck_to_recipient(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_NAME", "TestPlant Corp")
    monkeypatch.setenv("SRM_T_USER", "me@test.local")
    monkeypatch.setenv("SRM_T_TO_C", "sup_c@vendor-c.test")
    monkeypatch.setenv("SRM_T_CC_C", "cc_c@vendor-c.test")
    monkeypatch.setenv("SRM_T_TO_H", "sup_h@vendor-h.test")

    # Vendor C: 186 carts > 165 -> Extra truck needed warning ON
    srm_c = [0.0] * 36
    srm_c[0], srm_c[2], srm_c[4], srm_c[6], srm_c[9] = 1160.0, 780.0, 500.0, 520.0, 760.0
    rc, oc, qc = _make_part("99010021", "Vendor C", 2000.0, srm_c, [0.0] * 36)

    # Vendor H: 100 carts <= 132 -> Extra truck needed warning OFF
    srm_h = [0.0] * 36
    srm_h[1], srm_h[2], srm_h[4], srm_h[7] = 500.0, 500.0, 500.0, 500.0
    rh, oh, qh = _make_part("99010022", "Vendor H", 2000.0, srm_h, [0.0] * 36)

    lvl_cfg = {
        "enabled": True,
        "freeze_departed": False,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor C": 33, "Vendor H": 33},
    }
    tr_both = {**TR_BETA, **TR_ALPHA}
    plan_parts(
        D, [rc, rh], {"99010021": oc, "99010022": oh}, {"99010021": qc, "99010022": qh},
        None, None, False, tr_both, {"99010021": 20.0, "99010022": 20.0}, {"99010021": "C", "99010022": "C"},
        level_cfg=lvl_cfg,
    )

    sup_files = write_supplier_files(tmp_path, "AM", [rc, rh], {"99010021": 20.0, "99010022": 20.0}, dt.datetime(2026, 11, 17, 6, 45))
    fc = next(p for p in sup_files if "Vendor_C" in p.name)
    wb = load_workbook(fc, data_only=True)
    ws = wb["Truck plan"]
    expected_w = "Extra truck needed: Tue 11/17 total 186 carts > 5 trucks × 33 = 165. Please arrange an additional truck for 21 carts."
    all_vals = [str(ws.cell(r, 1).value or "") for r in range(1, ws.max_row + 1)]
    assert any(expected_w in v for v in all_vals)
    # PM refresh workbook parser remains 100% compatible
    parsed = parse_truck_plan_workbook(fc)
    assert parsed["has_truck_plan"] is True and "099010021" in parsed["parts"]

    mail_cfg = {
        "mode": "live",
        "user_env": "SRM_T_USER",
        "send_when_no_change": True,
        "suppliers": {
            "Vendor C": {"to_env": "SRM_T_TO_C", "cc_env": "SRM_T_CC_C"},
            "Vendor H": {"to_env": "SRM_T_TO_H", "cc_env": ""},
        },
    }

    # Case 1: SRM_EXTRA_TRUCK_TO set -> added ONLY to Vendor C (warning ON), NOT Vendor H (warning OFF)
    monkeypatch.setenv("SRM_EXTRA_TRUCK_TO", "internal_dispatch@plant.test")
    sent1 = []
    res1 = mailer.send_run_mails(
        {"run_date": "2026-11-17", "run_label": "AM", "supplier_files": [str(x) for x in sup_files], "notes": []},
        [rc, rh], dt.datetime(2026, 11, 17, 6, 45), cfg=mail_cfg,
        sender=lambda to, cc, subj, body, atts: sent1.append({"to": to, "cc": cc, "subj": subj, "body": body}) or True,
    )
    mc1 = next(x for x in sent1 if "Vendor C" in x["subj"])
    mh1 = next(x for x in sent1 if "Vendor H" in x["subj"])
    assert mc1["to"] == ["sup_c@vendor-c.test", "internal_dispatch@plant.test"]
    import html
    assert html.escape(expected_w) in mc1["body"]
    assert mh1["to"] == ["sup_h@vendor-h.test"]
    assert "Extra truck needed:" not in mh1["body"]
    assert next(r for r in res1 if r["mail"] == "Vendor C")["extra_truck_to"] == "added"
    assert "extra_truck_to" not in next(r for r in res1 if r["mail"] == "Vendor H")

    # Case 2: SRM_EXTRA_TRUCK_TO missing/empty -> mail still sends + log line recorded
    monkeypatch.delenv("SRM_EXTRA_TRUCK_TO", raising=False)
    sent2 = []
    st2 = {"run_date": "2026-11-17", "run_label": "AM", "supplier_files": [str(x) for x in sup_files], "notes": []}
    res2 = mailer.send_run_mails(
        st2, [rc, rh], dt.datetime(2026, 11, 17, 6, 45), cfg=mail_cfg,
        sender=lambda to, cc, subj, body, atts: sent2.append({"to": to, "subj": subj}) or True,
    )
    mc2 = next(x for x in sent2 if "Vendor C" in x["subj"])
    assert mc2["to"] == ["sup_c@vendor-c.test"]
    rc_res2 = next(r for r in res2 if r["mail"] == "Vendor C")
    assert rc_res2["sent"] is True
    assert rc_res2["extra_truck_to"] == "missing_or_empty"
    assert any("SRM_EXTRA_TRUCK_TO is missing or empty" in n for n in st2["notes"])
