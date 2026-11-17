"""#367 / #384 comprehensive unit tests for minimal-change SRM truck leveling (R1-R5)."""
import datetime as dt
import json
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_srm_monitoring as runner  # noqa: E402
import srm_leveling as lvl  # noqa: E402
import srm_mailer as mailer  # noqa: E402
from srm_parsers import N_SLOTS, SLOT_LABELS  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_supplier_files import write_supplier_files  # noqa: E402
from srm_trucks import leveling_config  # noqa: E402
from test_srm_monitoring import _res_row  # noqa: E402

D = dt.date(2026, 11, 16)
TR_ALPHA = {"Vendor H": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}     # [1, 2, 4, 7]
TR_BETA = {"Vendor C": [SLOT_LABELS.index(x) for x in ("8-10", "12-14", "16-18", "20-22", "02-04")]}  # [0, 2, 4, 6, 9]


def _make_part(key: str, vendor: str, start_sap: float, srm_36, req_36, add: float = 0.0):
    row = _res_row(key, vendor, start_sap)
    o = {"part": "0" + key, "vendor": vendor, "name": "DESC " + key, "add": add, "slots": list(srm_36)}
    q = {"part": "0" + key, "vendor": vendor, "name": "DESC " + key, "add": 0.0, "slots": list(req_36)}
    return row, o, q


def test_384_r1_no_mail_memory_and_no_change_when_below_1_7x_avg_and_no_add():
    """#384 R1 & R3:
    1) Churn lock / last_mailed_plan helpers are completely removed.
    2) When no truck exceeds 1.7 x day_average and ADD == 0, 0 parts are touched and 0 carts move."""
    assert not hasattr(lvl, "load_prior_mailed_plan")
    assert not hasattr(lvl, "save_mailed_plan_snapshot")
    assert not hasattr(lvl, "compute_mailed_cell_diffs")
    assert not hasattr(runner, "MAILED_PLAN_FILE")

    # Trucks [15, 20, 25, 30] carts -> sum = 90, avg = 22.5, 1.7 * avg = 38.25 -> max 30 <= 38.25 -> 0 changes!
    srm1 = [0.0] * 36
    srm2 = [0.0] * 36
    srm1[1], srm1[2], srm1[4], srm1[7] = 300.0, 200.0, 300.0, 200.0   # box=20 -> [15, 10, 15, 10]
    srm2[1], srm2[2], srm2[4], srm2[7] = 0.0, 300.0, 300.0, 600.0     # box=30 -> [0, 10, 10, 20] -> total [15, 20, 25, 30]
    r1, o1, q1 = _make_part("99000011", "Vendor H", 500.0, srm1, [10.0] * 36)
    r2, o2, q2 = _make_part("99000012", "Vendor H", 500.0, srm2, [10.0] * 36)

    summary = plan_parts(
        D, [r1, r2],
        {"99000011": o1, "99000012": o2},
        {"99000011": q1, "99000012": q2},
        None, None, False, TR_ALPHA,
        {"99000011": 20.0, "99000012": 30.0},
        {"99000011": "C", "99000012": "C"},
    )
    assert r1["plan"]["load"] == srm1
    assert r2["plan"]["load"] == srm2
    d0 = summary["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert d0["day_avg"] == 22.5
    assert d0["threshold"] == 38.25
    assert d0["trucks_before"] == [15.0, 20.0, 25.0, 30.0]
    assert d0["trucks_after"] == [15.0, 20.0, 25.0, 30.0]
    assert d0["parts_touched"] == 0
    assert d0["carts_moved"] == 0.0


def test_384_r5_untouched_part_below_115_pct_stays_strictly_as_srm_ordered():
    """#384 R5: Parts we do NOT touch are left as SRM ordered, even if below 115% (no shortage -> leave it).
    Do NOT pull them forward just for the 115% margin!"""
    # Part A: start_sap = 105, srm = [0, 0, 100, 0] (5 carts of 20 at 16-18, slot 4), req[4] = 100.
    # Inv before slot 4 is 105 (< 115 = 115% of 100!), and Inv at slot 4 is 105 + 100 - 100 = 105 >= 0 (NO shortage!).
    srm_a = [0.0] * 36
    srm_a[4] = 100.0   # 5 carts at 16-18
    req_a = [0.0] * 36
    req_a[4] = 100.0

    # Subcase 1: Part A alone on [0, 0, 5, 0] -> wait: 5 > 1.7 * 1.25 = 2.125 if alone, so add Part B [5, 5, 0, 5]
    # making day carts [5, 5, 5, 5] (no overload) -> Part A MUST stay 100% untouched at slot 4!
    srm_b = [0.0] * 36
    srm_b[1], srm_b[2], srm_b[7] = 100.0, 100.0, 100.0
    ra1, oa1, qa1 = _make_part("99000021", "Vendor H", 105.0, srm_a, req_a)
    rb1, ob1, qb1 = _make_part("99000022", "Vendor H", 500.0, srm_b, [0.0] * 36)
    plan_parts(
        D, [ra1, rb1],
        {"99000021": oa1, "99000022": ob1},
        {"99000021": qa1, "99000022": qb1},
        None, None, False, TR_ALPHA,
        {"99000021": 20.0, "99000022": 20.0},
        {"99000021": "C", "99000022": "C"},
    )
    assert ra1["plan"]["load"] == srm_a
    assert ra1["plan"]["stock"][3] == 105.0   # < 115%, no shortage -> left untouched!

    # Subcase 2: Even when 22-24 IS overloaded ([4, 5, 27, 49] style), Part A (at 22-24 with 2 carts, Inv before 22-24 = 105 < 115)
    # is NOT pulled when a larger Part C (15 carts on 22-24) relieves the overload in 1 move!
    srm_a2 = [0.0] * 36
    srm_a2[7] = 100.0   # 5 carts on 22-24
    req_a2 = [0.0] * 36
    req_a2[7] = 100.0   # Inv(6) = 105 < 115, Inv(7) = 105 >= 0 (no shortage)

    srm_c2 = [0.0] * 36
    srm_c2[1], srm_c2[2], srm_c2[4], srm_c2[7] = 80.0, 100.0, 540.0, 880.0   # [4, 5, 27, 44] + Part A [0,0,0,5] = [4, 5, 27, 49]
    ra2, oa2, qa2 = _make_part("99000023", "Vendor H", 105.0, srm_a2, req_a2)
    rc2, oc2, qc2 = _make_part("99000024", "Vendor H", 1000.0, srm_c2, [0.0] * 36)
    summary2 = plan_parts(
        D, [ra2, rc2],
        {"99000023": oa2, "99000024": oc2},
        {"99000023": qa2, "99000024": qc2},
        None, None, False, TR_ALPHA,
        {"99000023": 20.0, "99000024": 20.0},
        {"99000023": "C", "99000024": "C"},
    )
    assert ra2["plan"]["load"] == srm_a2   # Untouched part below 115% stays untouched!
    d0 = summary2["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert d0["parts_touched"] == 1
    assert d0["touched_parts_detail"][0]["part"] == "099000024"


def test_384_r3a_add_placement_to_lightest_editable_trucks_with_115_rule():
    """#384 R3(a): Urgent ADD goes to the lightest editable trucks in full carts, satisfying 115%."""
    # Subcase 1: Background part holds [10, 2, 12, 10] carts (no overload: avg = 9.25 w/ 3 ADD carts, 1.7*avg = 15.725).
    # Part Z has SRM = [0, 0, 0, 0], ADD = 90 EA (3 carts of 30), abundant stock = 500 -> all 3 ADD carts go onto
    # the lightest editable truck 12-14 (slot 2, 2 -> 5 carts), NOT left on 22-24!
    srm_bg = [0.0] * 36
    srm_bg[1], srm_bg[2], srm_bg[4], srm_bg[7] = 200.0, 40.0, 240.0, 200.0   # box=20 -> [10, 2, 12, 10]
    r_bg, o_bg, q_bg = _make_part("99000031", "Vendor H", 500.0, srm_bg, [0.0] * 36)
    r_z, o_z, q_z = _make_part("99000032", "Vendor H", 500.0, [0.0] * 36, [10.0] * 12 + [0.0] * 24, add=90.0)

    summary = plan_parts(
        D, [r_bg, r_z],
        {"99000031": o_bg, "99000032": o_z},
        {"99000031": q_bg, "99000032": q_z},
        None, None, False, TR_ALPHA,
        {"99000031": 20.0, "99000032": 30.0},
        {"99000031": "C", "99000032": "C"},
    )
    assert r_bg["plan"]["load"] == srm_bg
    assert [r_z["plan"]["load"][t] for t in TR_ALPHA["Vendor H"]] == [0.0, 90.0, 0.0, 0.0]
    d0 = summary["suppliers"]["Vendor H"]["days"][D.isoformat()]
    assert d0["trucks_after"] == [10.0, 5.0, 12.0, 10.0]

    # Subcase 2: 115% constraint on ADD placement — start_sap = 55, req[2] = 100 (requires Inv(1) >= 115 before 12-14!).
    # ADD = 90 EA (3 carts of 30), SRM = [0, 30, 0, 0] (1 cart at 12-14).
    # Background holds [12, 8, 1, 8] carts (lightest truck is 16-18 at 1 cart!).
    # At least 2 ADD carts (60 EA) MUST stay on 10-12 so Inv(1) = 55 + 60 = 115 >= 115 before 12-14;
    # only the 3rd ADD cart (30 EA) moves to lightest editable truck 16-18!
    srm_bg2 = [0.0] * 36
    srm_bg2[1], srm_bg2[2], srm_bg2[4], srm_bg2[7] = 240.0, 160.0, 20.0, 160.0   # [12, 8, 1, 8]
    srm_z2 = [0.0] * 36
    srm_z2[2] = 30.0
    req_z2 = [0.0] * 36
    req_z2[2] = 100.0
    r_bg2, o_bg2, q_bg2 = _make_part("99000033", "Vendor H", 500.0, srm_bg2, [0.0] * 36)
    r_z2, o_z2, q_z2 = _make_part("99000034", "Vendor H", 55.0, srm_z2, req_z2, add=90.0)
    plan_parts(
        D, [r_bg2, r_z2],
        {"99000033": o_bg2, "99000034": o_z2},
        {"99000033": q_bg2, "99000034": q_z2},
        None, None, False, TR_ALPHA,
        {"99000033": 20.0, "99000034": 30.0},
        {"99000033": "C", "99000034": "C"},
    )
    assert [r_z2["plan"]["load"][t] for t in TR_ALPHA["Vendor H"]] == [60.0, 30.0, 30.0, 0.0]
    assert r_z2["plan"]["stock"][1] == 115.0


def test_384_r2_frozen_trucks_neither_source_nor_destination_and_in_day_avg():
    """#384 R2 & R3(b):
    - Frozen truck (`slot_start <= run_ts + 30m`) is neither source (even when > 1.7x avg) nor destination (even when 0).
    - Frozen truck IS included in the denominator/sum of `day_avg`."""
    # Subcase A (Real 11/16 AM geometry): [34, 18, 1, 0, 41] carts at run_ts = 06:46 (slot 0 = 06:00 frozen!).
    # day_avg = 94 / 5 = 18.80 (includes frozen 34!), threshold = 1.7 * 18.80 = 31.96.
    # Frozen slot 0 (34 > 31.96) stays 34! Editable slot 9 (41 > 31.96) unloads 10 carts to lightest editable slot 6 (0 -> 10)!
    srm_f = [0.0] * 36
    srm_f[0], srm_f[2], srm_f[4], srm_f[6], srm_f[9] = 680.0, 360.0, 20.0, 0.0, 820.0  # box=20 -> [34, 18, 1, 0, 41]
    rf, of, qf = _make_part("99000041", "Vendor C", 2000.0, srm_f, [0.0] * 36)
    sum_a = plan_parts(
        D, [rf], {"99000041": of}, {"99000041": qf}, None, None, False, TR_BETA,
        {"99000041": 20.0}, {"99000041": "C"}, level_cfg={"enabled": True, "freeze_departed": True, "safety_margin": 0.15, "freeze_lead_min": 30.0, "overload_factor": 1.7, "max_carts_per_truck": {}}, run_ts=dt.datetime(2026, 11, 16, 6, 46),
    )
    d0_a = sum_a["suppliers"]["Vendor C"]["days"][D.isoformat()]
    assert d0_a["frozen_trucks"] == ["8-10"]
    assert d0_a["day_avg"] == 18.8
    assert d0_a["threshold"] == 31.96
    assert d0_a["trucks_after"] == [34.0, 18.0, 1.0, 10.0, 31.0]
    assert rf["plan"]["load"][0] == 680.0   # Frozen truck untouched!

    # Subcase B (Frozen empty truck): [0 (frozen), 10, 2, 10, 40] at 06:46 -> avg = 62/5 = 12.4, thr = 21.08.
    # Excess on slot 9 = ceil(40 - 21.08) = 19 carts. Slot 0 (0 carts) is FROZEN so it receives 0!
    # Lightest editable is slot 4 (2 carts, room = floor(21.08 - 2) = 19) -> moves all 19 carts to slot 4 -> [0, 10, 21, 10, 21]!
    srm_b = [0.0] * 36
    srm_b[0], srm_b[2], srm_b[4], srm_b[6], srm_b[9] = 0.0, 200.0, 40.0, 200.0, 800.0
    rb, ob, qb = _make_part("99000042", "Vendor C", 2000.0, srm_b, [0.0] * 36)
    sum_b = plan_parts(
        D, [rb], {"99000042": ob}, {"99000042": qb}, None, None, False, TR_BETA,
        {"99000042": 20.0}, {"99000042": "C"}, level_cfg={"enabled": True, "freeze_departed": True, "safety_margin": 0.15, "freeze_lead_min": 30.0, "overload_factor": 1.7, "max_carts_per_truck": {}}, run_ts=dt.datetime(2026, 11, 16, 6, 46),
    )
    assert [rb["plan"]["load"][t] / 20.0 for t in TR_BETA["Vendor C"]] == [0.0, 10.0, 21.0, 10.0, 21.0]
    assert sum_b["suppliers"]["Vendor C"]["days"][D.isoformat()]["carts_moved"] == 19.0


def test_384_r4_fewest_changes_stops_at_1_7x_avg_in_single_part_move():
    """#384 R3(b) & R4: Day 1 carts [4, 5, 27, 49] (avg = 21.25, threshold = 36.125, excess = 13 carts).
    With P1 (15 carts on 22-24), P2 (13 carts on 22-24), P3 (21 carts on 22-24):
      - Unloads ONLY 13 carts (stopping at 36 <= 36.125; does NOT equalize to 21/21/21/22!).
      - Moves to lightest editable truck 10-12 (4 -> 17 carts).
      - Touches ONLY 1 part (preferring P2's exact 13-cart whole slot quantity so 0 split is needed!)."""
    srm_p1, srm_p2, srm_p3 = [0.0] * 36, [0.0] * 36, [0.0] * 36
    # Day 1 (indices 12..23, trucks 13, 14, 16, 19): [4, 5, 27, 49]
    srm_p1[13], srm_p1[16], srm_p1[19] = 80.0, 240.0, 300.0    # box=20 -> [4, 0, 12, 15]
    srm_p2[14], srm_p2[19] = 150.0, 390.0                      # box=30 -> [0, 5, 0, 13]
    srm_p3[16], srm_p3[19] = 300.0, 420.0                      # box=20 -> [0, 0, 15, 21]
    r1, o1, q1 = _make_part("99000101", "Vendor H", 1000.0, srm_p1, [5.0] * 36)
    r2, o2, q2 = _make_part("99000102", "Vendor H", 1000.0, srm_p2, [5.0] * 36)
    r3, o3, q3 = _make_part("99000103", "Vendor H", 1000.0, srm_p3, [5.0] * 36)

    summary = plan_parts(
        D, [r1, r2, r3],
        {"99000101": o1, "99000102": o2, "99000103": o3},
        {"99000101": q1, "99000102": q2, "99000103": q3},
        None, None, False, TR_ALPHA,
        {"99000101": 20.0, "99000102": 30.0, "99000103": 20.0},
        {"99000101": "C", "99000102": "C", "99000103": "C"},
    )
    d1_iso = (D + dt.timedelta(days=1)).isoformat()
    d1 = summary["suppliers"]["Vendor H"]["days"][d1_iso]
    assert d1["trucks_before"] == [4.0, 5.0, 27.0, 49.0]
    assert d1["trucks_after"] == [17.0, 5.0, 27.0, 36.0]
    assert d1["parts_touched"] == 1
    assert d1["carts_moved"] == 13.0
    assert d1["touched_parts_detail"][0]["part"] == "099000102"
    assert r1["plan"]["load"] == srm_p1 and r3["plan"]["load"] == srm_p3


def test_384_r5_shortage_never_created_115_boundary_lookahead_and_step4_fallback(tmp_path, monkeypatch):
    """#384 R5 hard rules:
    1) Pushing off an overloaded truck is REJECTED if it creates a shortage or violates 115% (114 rejected, 115 accepted),
       including Prod.Plan(S)==0 lookahead and emptying an intermediate truck.
    2) When every move off an overloaded truck would cause a shortage, baseline is kept + note emitted;
       Class L Pallet Truck and non-truck 'Other times' stay untouched.
    3) Config guards (safety_margin >= 0.10, overload_factor > 1.0), 'Total carts (boxes)' row, and Step 4 fallback."""
    # 1a. 4-truck setup [8, 0, 1, 0] (avg = 2.25, thr = 3.825 -> truck 1 overloaded at 8 carts!).
    # Make slot 2 frozen or use 3 trucks [1, 4, 7] with [8, 1, 1] (avg = 3.33, thr = 5.67 -> slot 1 overloaded, excess = 3):
    # Test boundary pushing 1 cart (20 EA) from slot 1 -> slot 4 when req[4] = 100 (requires Inv(3) >= 115):
    tr_three = {"Vendor H": [1, 4, 7]}
    srm_b = [0.0] * 36
    srm_b[1], srm_b[4], srm_b[7] = 120.0, 20.0, 20.0   # [6, 1, 1] carts -> avg=2.67, thr=4.53 -> slot 1 overloaded by 2 carts
    req_b = [0.0] * 36
    req_b[4] = 100.0

    # start_sap = 14 -> before move Inv(3) = 134 >= 115; pushing 1 cart (20 EA) drops Inv(3) to 114 < 115 -> REJECTED!
    r_rej, o_rej, q_rej = _make_part("99000201", "Vendor H", 14.0, srm_b, req_b)
    sum_rej = plan_parts(D, [r_rej], {"99000201": o_rej}, {"99000201": q_rej}, None, None, False, tr_three,
                         {"99000201": 20.0}, {"99000201": "C"})
    assert [r_rej["plan"]["load"][t] for t in (1, 4, 7)] == [120.0, 20.0, 20.0]
    assert any("Overloaded truck kept to prevent shortage" in n for n in sum_rej["notes"])

    # start_sap = 15 -> pushing 1 cart drops Inv(3) to 115 >= 115 -> 1 cart ACCEPTED (2nd cart would drop to 95 < 115 -> rejected)!
    r_acc, o_acc, q_acc = _make_part("99000202", "Vendor H", 15.0, srm_b, req_b)
    plan_parts(D, [r_acc], {"99000202": o_acc}, {"99000202": q_acc}, None, None, False, tr_three,
               {"99000202": 20.0}, {"99000202": "C"})
    assert [r_acc["plan"]["load"][t] for t in (1, 4, 7)] == [100.0, 40.0, 20.0]
    assert r_acc["plan"]["stock"][3] == 115.0
    assert all(v >= -1e-9 for v in r_acc["plan"]["stock"])

    # 1b. Zero Prod.Plan(S) at S=4 looks ahead to slot 5 (req[5]=200 -> requires Inv(3) >= 230)
    req_look = [0.0] * 36
    req_look[5] = 200.0
    r_lk, o_lk, q_lk = _make_part("99000203", "Vendor H", 100.0, srm_b, req_look)   # Inv(3)=220 < 230 even before move -> cannot push!
    plan_parts(D, [r_lk], {"99000203": o_lk}, {"99000203": q_lk}, None, None, False, tr_three,
               {"99000203": 20.0}, {"99000203": "C"})
    assert [r_lk["plan"]["load"][t] for t in (1, 4, 7)] == [120.0, 20.0, 20.0]

    # 2. Class L Pallet Truck and non-truck 'Other times' stay untouched
    srm_c = [0.0] * 36
    srm_c[1], srm_c[3] = 500.0, 40.0   # 25 carts at 10-12 + 40 EA at non-truck 14-16
    req_c = [0.0] * 36
    req_c[1] = 500.0                   # immediate consumption -> cannot push without shortage!
    srm_l = [0.0] * 36
    srm_l[5] = 300.0
    rc, oc, qc = _make_part("99000301", "Vendor H", 0.0, srm_c, req_c)
    rl, ol, ql = _make_part("99000302", "Vendor H", 400.0, srm_l, [10.0] * 12 + [0.0] * 24)
    plan_parts(
        D, [rc, rl],
        {"99000301": oc, "99000302": ol},
        {"99000301": qc, "99000302": ql},
        None, None, False, TR_ALPHA,
        {"99000301": 20.0, "99000302": 50.0},
        {"99000301": "C", "99000302": "L"},
    )
    assert rc["plan"]["load"][1] == 500.0 and rc["plan"]["load"][3] == 40.0
    assert rl["plan"]["load"] == srm_l

    # 3. Config guards + 'Total carts (boxes)' + internal_body + Step 4 fallback
    bad_m = tmp_path / "bad_m.json"
    bad_m.write_text(json.dumps({"leveling": {"safety_margin": 0.09}}), encoding="utf-8")
    with pytest.raises(ValueError, match="0.10"):
        leveling_config(bad_m)

    bad_o = tmp_path / "bad_o.json"
    bad_o.write_text(json.dumps({"leveling": {"overload_factor": 1.0}}), encoding="utf-8")
    with pytest.raises(ValueError, match="1.0"):
        leveling_config(bad_o)

    srm_ov = [0.0] * 36
    srm_ov[7] = 200.0   # [0, 0, 0, 10] carts -> avg=2.5, thr=4.25 -> unloads 6 carts to slot 1 -> [4, 2, 0, 4] or [4, 2, 0, 4]
    r_ov, o_ov, q_ov = _make_part("99000401", "Vendor H", 400.0, srm_ov, [0.0] * 36)
    plan_parts(D, [r_ov], {"99000401": o_ov}, {"99000401": q_ov}, None, None, False, TR_ALPHA,
               {"99000401": 20.0}, {"99000401": "C"})
    paths = write_supplier_files(tmp_path, "AM", [r_ov], {"99000401": 20.0}, dt.datetime(2026, 11, 16, 6, 45))
    ws = load_workbook(paths[0])["Truck plan"]
    assert ws.cell(row=8, column=1).value == "Total EA"
    assert ws.cell(row=9, column=1).value == "Total carts (boxes)"
    assert [ws.cell(row=9, column=c).value for c in (5, 6, 7, 8)] == [4, 2, None, 4]

    html_int = mailer.internal_body(
        {"run_date": "2026-11-16", "run_label": "AM", "leveling_summary_text": "1 part-day(s) touched", "notes": []},
        ["Vendor H"],
    )
    assert "Truck leveling (ADD / &gt;1.7x avg)" in html_int
    assert "churn" not in html_int.lower()

    monkeypatch.setattr(lvl, "verify_supplier_day_postcheck", lambda *a, **kw: (False, "injected invariant failure"))
    r_fb, o_fb, q_fb = _make_part("99000402", "Vendor H", 400.0, srm_ov, [0.0] * 36)
    fb_sum = plan_parts(D, [r_fb], {"99000402": o_fb}, {"99000402": q_fb}, None, None, False, TR_ALPHA,
                        {"99000402": 20.0}, {"99000402": "C"})
    assert [r_fb["plan"]["load"][t] for t in TR_ALPHA["Vendor H"]] == [0.0, 0.0, 0.0, 200.0]
    assert fb_sum["suppliers"]["Vendor H"]["fallbacks"] == 3
    assert any("restored un-leveled baseline" in n for n in fb_sum["notes"])
