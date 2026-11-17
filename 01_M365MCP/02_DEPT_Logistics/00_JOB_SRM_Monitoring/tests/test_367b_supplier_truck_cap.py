"""#367b (notes 388 & 389) unit tests: per-supplier hard cart cap (33), 100% fallback, shortage-only warning,
frozen truck info-only, uncapped supplier 1.7x unchanged, and targeted UPDATED re-send."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import srm_mailer as mailer  # noqa: E402
from srm_parsers import SLOT_LABELS  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_trucks import CONFIG, leveling_config  # noqa: E402
from test_367_truck_leveling import TR_ALPHA, TR_BETA, _make_part  # noqa: E402

D = dt.date(2026, 11, 16)


def test_367b_config_schedule_key_resolves_via_suppliers_csv_without_vendor_name_in_git(tmp_path):
    raw_txt = CONFIG.read_text(encoding="utf-8")
    # Git-tracked config/trucks.json carries only slot schedule keys, zero supplier names (#367b & #399)
    assert "8-10 12-14 16-18 20-22 02-04" in raw_txt
    assert "10-12 12-14 16-18 22-24" in raw_txt
    raw_obj = json.loads(raw_txt)
    assert raw_obj["leveling"]["max_carts_per_truck"] == {
        "8-10 12-14 16-18 20-22 02-04": 33,
        "10-12 12-14 16-18 22-24": 33,
        "8-10 10-12 22-24": 60,
    }

    sup_csv = tmp_path / "suppliers.csv"
    sup_csv.write_text(
        "match,trucks,mail,to_env,cc_env\n"
        "SupplierFour,10-12 12-14 16-18 22-24,yes,TO_A,CC_A\n"
        "SupplierFive,8-10 12-14 16-18 20-22 02-04,yes,TO_B,CC_B\n"
        "SupplierThree,8-10 14-16 20-22,no,TO_C,CC_C\n",
        encoding="utf-8",
    )
    cfg = leveling_config(CONFIG, suppliers_path=sup_csv)
    assert cfg["max_carts_per_truck"] == {"SupplierFour": 33, "SupplierFive": 33}

    bad_cap = tmp_path / "bad_cap.json"
    bad_cap.write_text(json.dumps({"leveling": {"max_carts_per_truck": {"Vendor C": 0}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="integer >= 1"):
        leveling_config(bad_cap)


def test_367b_cap_respected_stops_at_33_and_uncapped_supplier_stays_1_7x():
    """Supplier with cap=33 unloads [40, 34, 20, 5, 35] -> all <= 33 (whereas 1.7x avg=26.8->45.56 would touch 0);
    uncapped Vendor H in the same run still uses 1.7x day_average."""
    # Vendor C (capped at 33): [40, 34, 20, 5, 35] carts (box=20)
    srm_c = [0.0] * 36
    srm_c[0], srm_c[2], srm_c[4], srm_c[6], srm_c[9] = 800.0, 680.0, 400.0, 100.0, 700.0
    rc, oc, qc = _make_part("99001001", "Vendor C", 2000.0, srm_c, [10.0] * 36)

    # Vendor H (uncapped): [35, 30, 25, 30] -> sum=120, avg=30, 1.7*avg=51 -> even though 35 > 33, NO cap on Vendor H!
    srm_h = [0.0] * 36
    srm_h[1], srm_h[2], srm_h[4], srm_h[7] = 700.0, 600.0, 500.0, 600.0
    rh, oh, qh = _make_part("99001002", "Vendor H", 2000.0, srm_h, [10.0] * 36)

    tr_both = {**TR_ALPHA, **TR_BETA}
    lvl_cfg = {
        "enabled": True,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor C": 33},
    }
    summary = plan_parts(
        D, [rc, rh],
        {"99001001": oc, "99001002": oh},
        {"99001001": qc, "99001002": qh},
        None, None, False, tr_both,
        {"99001001": 20.0, "99001002": 20.0},
        {"99001001": "C", "99001002": "C"},
        level_cfg=lvl_cfg,
    )
    dc = summary["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert dc["trucks_before"] == [40.0, 34.0, 20.0, 5.0, 35.0]
    assert all(x <= 33.0 for x in dc["trucks_after"])
    assert dc["trucks_after"] == [33.0, 33.0, 20.0, 15.0, 33.0]
    assert summary["warnings"] == []

    dh = summary["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert dh["trucks_after"] == [35.0, 30.0, 25.0, 30.0]
    assert dh["parts_touched"] == 0


def test_367b_pass2_100pct_fallback_used_only_when_115pct_blocks_cap_and_never_by_1_7x():
    """#389:
    - Pass 1 tries 115% first. If truck is still > 33, Pass 2 retries at 100% (`Inv(S-1) >= Req(S)`, never `< 0`),
      recording `margin_used = 1.00` and listing it in internal_body.
    - Uncapped 1.7x supplier NEVER uses 100% fallback."""
    # Setup for capped Vendor C: [35, 10, 10, 10, 10] carts (box=20).
    # Slot 0 has 35 carts (700 EA) of part 99002001; slots 2,4,6,9 have 10 carts (200 EA) of background part 99002002.
    # For 99002001: start_sap = 0, req[0] = 560, req[2] = 100.
    # At baseline (700 on slot 0): Inv(0) = 140, Inv(1) = 140 (>= 115 = 115% of req[2]=100!).
    # Moving 2 carts (40 EA) from slot 0 -> slot 2 leaves 660 on slot 0 -> Inv(1) = 660 - 560 = 100:
    #   * Fails 115% (100 < 115), so Pass 1 leaves 1 cart (120 >= 115 -> 34 carts) on slot 0!
    #   * Succeeds at 100% (100 >= 100 and Inv >= 0 everywhere!), so Pass 2 moves the 2nd cart -> 33 carts!
    srm_p = [0.0] * 36
    srm_p[0], srm_p[2] = 700.0, 20.0   # 35 carts on 8-10, 1 cart on 12-14
    req_p = [0.0] * 36
    req_p[0], req_p[2] = 560.0, 100.0
    srm_bg = [0.0] * 36
    srm_bg[2], srm_bg[4], srm_bg[6], srm_bg[9] = 180.0, 200.0, 200.0, 200.0   # [0, 9, 10, 10, 10] -> total [35, 10, 10, 10, 10]

    rp, op, qp = _make_part("99002001", "Vendor C", 0.0, srm_p, req_p)
    rb, ob, qb = _make_part("99002002", "Vendor C", 1000.0, srm_bg, [0.0] * 36)

    lvl_cap = {
        "enabled": True,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor C": 33},
    }
    sum_cap = plan_parts(
        D, [rp, rb],
        {"99002001": op, "99002002": ob},
        {"99002001": qp, "99002002": qb},
        None, None, False, TR_BETA,
        {"99002001": 20.0, "99002002": 20.0},
        {"99002001": "C", "99002002": "C"},
        level_cfg=lvl_cap,
    )
    d0 = sum_cap["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d0["trucks_before"] == [35.0, 10.0, 10.0, 10.0, 10.0]
    assert d0["trucks_after"][0] == 33.0
    assert all(x <= 33.0 for x in d0["trucks_after"])
    assert all(v >= -1e-9 for v in rp["plan"]["stock"])
    assert sum_cap["warnings"] == []
    assert len(sum_cap["moves_100pct"]) == 1
    assert sum_cap["moves_100pct"][0]["part"] == "099002001"
    assert sum_cap["moves_100pct"][0]["margin_used"] == 1.00

    body = mailer.internal_body(
        {"run_date": "2026-11-16", "run_label": "PM", "leveling": sum_cap, "notes": sum_cap["notes"]},
        ["Vendor C"],
    )
    assert "Cap fallback moves at 100% margin" in body
    assert "099002001" in body
    assert rp["name"] in body

    # Same geometry on UNCAPPED Vendor C (1.7 * 15 = 25.5): Pass 1 moves 1 cart to 34 (Inv=120 >= 115),
    # and does NOT use 100% fallback for the remaining carts!
    rp2, op2, qp2 = _make_part("99002001", "Vendor C", 0.0, srm_p, req_p)
    rb2, ob2, qb2 = _make_part("99002002", "Vendor C", 1000.0, srm_bg, [0.0] * 36)
    sum_uncap = plan_parts(
        D, [rp2, rb2],
        {"99002001": op2, "99002002": ob2},
        {"99002001": qp2, "99002002": qb2},
        None, None, False, TR_BETA,
        {"99002001": 20.0, "99002002": 20.0},
        {"99002001": "C", "99002002": "C"},
        level_cfg={**lvl_cap, "max_carts_per_truck": {}},
    )
    assert sum_uncap["moves_100pct"] == []
    assert sum_uncap["suppliers"]["Vendor C"]["days"][D.isoformat()]["trucks_after"][0] == 34.0


def test_367b_frozen_over_cap_is_info_only_while_true_shortage_or_excess_total_warns():
    """#388 & #389:
    1) Frozen (departed) truck over 33 is untouched and NOT a warning (info_notes only).
    2) Editable truck forced > 33 to prevent shortage OR because day total > 33 * editable_trucks emits warning,
       and destination trucks stay strictly <= 33."""
    lvl_cap = {
        "enabled": True,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor C": 33},
    }
    # Case 1: Today at 14:35 -> 8-10 (34 carts, departed), 12-14 (18), 16-18 (1), 20-22 (0), 02-04 (41)
    srm1 = [0.0] * 36
    srm1[0], srm1[2], srm1[4], srm1[6], srm1[9] = 680.0, 360.0, 20.0, 0.0, 820.0
    r1, o1, q1 = _make_part("99003001", "Vendor C", 2000.0, srm1, [0.0] * 36)
    s1 = plan_parts(
        D, [r1], {"99003001": o1}, {"99003001": q1}, None, None, False, TR_BETA,
        {"99003001": 20.0}, {"99003001": "C"}, level_cfg=lvl_cap,
        run_ts=dt.datetime(2026, 11, 16, 14, 35),
    )
    d0 = s1["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d0["trucks_after"] == [34.0, 18.0, 1.0, 8.0, 33.0]
    assert s1["warnings"] == []   # Departed 34-cart truck is NOT a warning!
    assert any("Frozen (departed) truck over 33 carts (info only)" in x for x in s1["info_notes"])

    # Case 2: Total carts = 166 across 5 editable trucks ([58, 35, 14, 21, 38]) -> 4 trucks at 33, 1 truck at 34 + warning!
    srm2 = [0.0] * 36
    srm2[0], srm2[2], srm2[4], srm2[6], srm2[9] = 1160.0, 700.0, 280.0, 420.0, 760.0
    r2, o2, q2 = _make_part("99003002", "Vendor C", 2000.0, srm2, [0.0] * 36)
    s2 = plan_parts(
        D, [r2], {"99003002": o2}, {"99003002": q2}, None, None, False, TR_BETA,
        {"99003002": 20.0}, {"99003002": "C"}, level_cfg=lvl_cap,
    )
    d0_2 = s2["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d0_2["trucks_after"] == [33.0, 33.0, 33.0, 33.0, 34.0]
    assert len(s2["warnings"]) == 1
    assert "Extra truck needed: Mon 11/16 total 166 carts > 5 trucks × 33 = 165. Please arrange an additional truck for 1 carts." in s2["warnings"][0]

    # Case 3 (#401c): Genuine slot-0 consumption forces > 33 while day total (36) <= 165 -> info line only, no extra-truck warning
    srm3 = [0.0] * 36
    srm3[0] = 720.0   # 36 carts
    req3 = [0.0] * 36
    req3[0] = 720.0   # All 720 EA consumed in slot 0 -> moving even 1 cart causes Inv(0) = -20 < 0!
    r3, o3, q3 = _make_part("99003003", "Vendor C", 0.0, srm3, req3)
    s3 = plan_parts(
        D, [r3], {"99003003": o3}, {"99003003": q3}, None, None, False, TR_BETA,
        {"99003003": 20.0}, {"99003003": "C"}, level_cfg=lvl_cap,
    )
    assert s3["suppliers"]["Vendor C"]["days"][D.isoformat()]["trucks_after"] == [36.0, 0.0, 0.0, 0.0, 0.0]
    assert s3["warnings"] == []
    assert any("Truck 8-10 36 carts (> 33) kept for coverage" in x for x in s3["info_notes"])


def test_367b_targeted_updated_resend_filters_supplier_and_marks_subject(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_NAME", "TestPlant Corp")
    monkeypatch.setenv("SRM_T_USER", "me@test.local")
    monkeypatch.setenv("SRM_T_TO_A", "sup_a@vendor-a.test")
    monkeypatch.setenv("SRM_T_CC_A", "cc_a@vendor-a.test")
    monkeypatch.setenv("SRM_T_TO_B", "sup_b@vendor-b.test")

    ra, oa, qa = _make_part("99004001", "Vendor A", 500.0, [20.0] + [0.0] * 35, [0.0] * 36)
    rb, ob, qb = _make_part("99004002", "Vendor B", 500.0, [40.0] + [0.0] * 35, [0.0] * 36)
    tr = {"Vendor A": [0, 2], "Vendor B": [0, 2]}
    plan_parts(D, [ra, rb], {"99004001": oa, "99004002": ob}, {"99004001": qa, "99004002": qb},
               None, None, False, tr, {"99004001": 20.0, "99004002": 20.0}, {"99004001": "C", "99004002": "C"})
    from srm_supplier_files import write_supplier_files
    sup_files = write_supplier_files(tmp_path, "PM", [ra, rb], {"99004001": 20.0, "99004002": 20.0}, dt.datetime(2026, 11, 16, 14, 45))

    sent = []
    def fake_send(to, cc, subj, body, atts):
        sent.append({"to": to, "cc": cc, "subj": subj, "body": body, "atts": [Path(x).name for x in atts]})
        return True

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
    status = {"run_date": "2026-11-16", "run_label": "PM", "supplier_files": [str(x) for x in sup_files], "notes": []}
    res = mailer.send_run_mails(
        status, [ra, rb], dt.datetime(2026, 11, 16, 14, 45),
        cfg=cfg, sender=fake_send, suppliers=True, only_supplier="Vendor B", updated=True,
    )
    sup_sent = [x for x in sent if "SRM Monitoring" not in x["subj"]]
    assert len(sup_sent) == 1
    assert sup_sent[0]["to"] == ["sup_b@vendor-b.test"]
    assert sup_sent[0]["cc"] == ["me@test.local"]
    assert sup_sent[0]["subj"].startswith("UPDATED - TestPlant Corp truck loading plan - Vendor B - ")
    assert "UPDATED" in sup_sent[0]["body"]
    assert res[0]["mail"] == "Vendor B" and res[0]["sent"] is True


def test_399_four_truck_supplier_with_33_cap_moves_over_33_and_warns_above_33_at_100pct():
    """#399: 4-truck supplier (TR_ALPHA = [1, 2, 4, 7] -> 10-12, 12-14, 16-18, 22-24) with cap=33:
    1) Trucks over 33 unload to <= 33 (Pass 1 at 115%, Pass 2 fallback at 100% when needed) where 1.7x would not touch.
    2) When a truck is still > 33 even at 100% (either immediate consumption shortage barrier or day total > 4*33),
       emits the 'Truck over 33 carts' warning while keeping 0 shortages and day totals unchanged."""
    lvl_cap4 = {
        "enabled": True,
        "safety_margin": 0.15,
        "freeze_lead_min": 30.0,
        "overload_factor": 1.7,
        "max_carts_per_truck": {"Vendor H": 33},
    }

    # (1a) [39, 35, 18, 20] carts (sum=112, avg=28 -> 1.7x=47.6 would move 0): cap=33 unloads both >33 trucks to <=33
    srm_a = [0.0] * 36
    srm_a[1], srm_a[2], srm_a[4], srm_a[7] = 780.0, 700.0, 360.0, 400.0
    ra, oa, qa = _make_part("99005001", "Vendor H", 2000.0, srm_a, [10.0] * 36)
    sa = plan_parts(
        D, [ra], {"99005001": oa}, {"99005001": qa}, None, None, False, TR_ALPHA,
        {"99005001": 20.0}, {"99005001": "C"}, level_cfg=lvl_cap4,
    )
    da = sa["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert da["trucks_before"] == [39.0, 35.0, 18.0, 20.0]
    assert all(x <= 33.0 for x in da["trucks_after"])
    assert sum(da["trucks_after"]) == 112.0
    assert sum(ra["plan"]["load"][:12]) == sum(srm_a[:12])
    assert all(v >= -1e-9 for v in ra["plan"]["stock"])
    assert sa["warnings"] == []

    # (1b) Pass 2 100% fallback on 4-truck supplier: [35, 10, 10, 10] where 115% stops at 34, 100% reaches 33
    srm_p = [0.0] * 36
    srm_p[1], srm_p[2] = 700.0, 20.0
    req_p = [0.0] * 36
    req_p[1], req_p[2] = 560.0, 100.0
    srm_bg = [0.0] * 36
    srm_bg[2], srm_bg[4], srm_bg[7] = 180.0, 200.0, 200.0
    rp, op, qp = _make_part("99005002", "Vendor H", 0.0, srm_p, req_p)
    rb, ob, qb = _make_part("99005003", "Vendor H", 1000.0, srm_bg, [0.0] * 36)
    sp = plan_parts(
        D, [rp, rb], {"99005002": op, "99005003": ob}, {"99005002": qp, "99005003": qb},
        None, None, False, TR_ALPHA, {"99005002": 20.0, "99005003": 20.0}, {"99005002": "C", "99005003": "C"},
        level_cfg=lvl_cap4,
    )
    dp = sp["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert dp["trucks_before"] == [35.0, 10.0, 10.0, 10.0]
    assert all(x <= 33.0 for x in dp["trucks_after"])
    assert sp["warnings"] == []
    assert len(sp["moves_100pct"]) == 1 and sp["moves_100pct"][0]["margin_used"] == 1.00

    # (2a) Still above 33 at 100% due to immediate slot-1 consumption (36 carts needed before 12-14 truck arrives)
    srm_w1 = [0.0] * 36
    srm_w1[1], srm_w1[2], srm_w1[4], srm_w1[7] = 720.0, 200.0, 200.0, 200.0
    req_w1 = [0.0] * 36
    req_w1[1] = 720.0
    rw1, ow1, qw1 = _make_part("99005004", "Vendor H", 0.0, srm_w1, req_w1)
    sw1 = plan_parts(
        D, [rw1], {"99005004": ow1}, {"99005004": qw1}, None, None, False, TR_ALPHA,
        {"99005004": 20.0}, {"99005004": "C"}, level_cfg=lvl_cap4,
    )
    dw1 = sw1["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert dw1["trucks_after"] == [36.0, 10.0, 10.0, 10.0]
    assert all(v >= -1e-9 for v in rw1["plan"]["stock"])
    assert sw1["warnings"] == []
    assert any("Truck 10-12 36 carts (> 33) kept for coverage" in x for x in sw1["info_notes"])

    # (2b) Day total = 135 carts across 4 editable trucks (> 4 * 33 = 132) -> [33, 33, 33, 36] on LAST truck + warning
    srm_w2 = [0.0] * 36
    srm_w2[1], srm_w2[2], srm_w2[4], srm_w2[7] = 900.0, 720.0, 540.0, 540.0
    rw2, ow2, qw2 = _make_part("99005005", "Vendor H", 2000.0, srm_w2, [0.0] * 36)
    sw2 = plan_parts(
        D, [rw2], {"99005005": ow2}, {"99005005": qw2}, None, None, False, TR_ALPHA,
        {"99005005": 20.0}, {"99005005": "C"}, level_cfg=lvl_cap4,
    )
    dw2 = sw2["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert dw2["trucks_after"] == [33.0, 33.0, 33.0, 36.0]
    assert len(sw2["warnings"]) == 1
    assert "Extra truck needed: Mon 11/16 total 135 carts > 4 trucks × 33 = 132. Please arrange an additional truck for 3 carts." in sw2["warnings"][0]
