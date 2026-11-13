import sys
from pathlib import Path

JOB03_DIR = Path(__file__).resolve().parents[1]
if str(JOB03_DIR) not in sys.path:
    sys.path.insert(0, str(JOB03_DIR))

from d103_mart_builder import (
    aggregate_mb51_window_for_material,
    decompose_unknown_with_mb51,
    map_d108_verdict_to_03_cause,
)


def test_auto_311_docs_and_wrong_sloc_excluded_from_manual_311():
    rows = [
        # 02_AUTO doc should be excluded
        {"mvt_type": "311", "sloc": "SP31", "mat_doc_no": "4990000001", "doc_header_text": "Backlog_Clear", "qty_entry": 50.0, "username": "AUTO"},
        # 01_AUTO doc (by doc_no) should be excluded
        {"mvt_type": "311", "sloc": "SP31", "mat_doc_no": "4990000002", "doc_header_text": "", "qty_entry": 30.0, "username": "AUTO"},
        # Source leg (-100 from SL01) and non-target SLoc (+40 into SL02) should be excluded
        {"mvt_type": "311", "sloc": "SL01", "mat_doc_no": "4990000003", "doc_header_text": "", "qty_entry": -100.0, "username": "SLBC"},
        {"mvt_type": "311", "sloc": "SL02", "mat_doc_no": "4990000004", "doc_header_text": "", "qty_entry": 40.0, "username": "SAMIT"},
        # Genuine manual 311 into SP31 (+100) should be counted
        {"mvt_type": "311", "sloc": "SP31", "mat_doc_no": "4990000003", "doc_header_text": "", "qty_entry": 100.0, "username": "SLBC"},
    ]
    st = aggregate_mb51_window_for_material(rows, target_slocs={"SP31"}, auto_doc_nos={"4990000002"})
    assert st["raw_mb51_311_manual_qty"] == 100.0
    assert st["manual_311_doc_cnt"] == 1
    assert st["manual_311_users"] == "SLBC"


def test_gr_and_scrap_reversal_netting_and_261_consumption():
    rows = [
        {"mvt_type": "101", "sloc": "SL00", "mat_doc_no": "5001", "doc_header_text": "", "qty_entry": 200.0},
        {"mvt_type": "102", "sloc": "SL00", "mat_doc_no": "5002", "doc_header_text": "", "qty_entry": -50.0},
        {"mvt_type": "131", "sloc": "SP31", "mat_doc_no": "5003", "doc_header_text": "", "qty_entry": 30.0},
        {"mvt_type": "593", "sloc": "SL49", "mat_doc_no": "5004", "doc_header_text": "", "qty_entry": -25.0},
        {"mvt_type": "594", "sloc": "SL49", "mat_doc_no": "5005", "doc_header_text": "", "qty_entry": 5.0},
        {"mvt_type": "261", "sloc": "SP31", "mat_doc_no": "5006", "doc_header_text": "", "qty_entry": -80.0},
        {"mvt_type": "262", "sloc": "SP31", "mat_doc_no": "5007", "doc_header_text": "", "qty_entry": 10.0},
    ]
    st = aggregate_mb51_window_for_material(rows, target_slocs={"SP31", "SL49"}, auto_doc_nos=set())
    assert st["raw_mb51_101_131_gr_qty"] == 180.0  # 200 - 50 + 30
    assert st["raw_mb51_scrap_qty"] == 20.0  # 25 - 5
    assert st["raw_mb51_261_gi_qty"] == 80.0
    assert st["raw_mb51_262_re_qty"] == 10.0
    assert st["raw_mb51_261_total_qty"] == 90.0


def test_decompose_unknown_exact_cap_and_gr_dedup():
    # Suppose reduced_qty after 02/01 = 150, attr_gr = 40 (so rem_after_gr = 110).
    # Window MB51 has: manual 311 = 50, raw GR = 70 (net available beyond attr_gr = 30), scrap = 10, 261 = 100.
    st = {
        "raw_mb51_311_manual_qty": 50.0,
        "raw_mb51_101_131_gr_qty": 70.0,
        "raw_mb51_scrap_qty": 10.0,
        "raw_mb51_261_total_qty": 100.0,
    }
    dec = decompose_unknown_with_mb51(rem_after_gr=110.0, attr_gr=40.0, mb51_stats=st)
    assert dec["attr_mb51_311_manual"] == 50.0
    assert dec["attr_mb51_101_131_gr"] == 30.0  # 70 - 40 attr_gr
    assert dec["attr_mb51_scrap"] == 10.0
    assert dec["attr_mb51_261_consumed"] == 20.0  # capped to remaining 110 - 50 - 30 - 10 = 20
    assert dec["attr_unknown_residual"] == 0.0
    assert round(sum(dec.values()), 2) == 110.0


def test_decompose_unknown_leaves_residual_when_no_mb51():
    st = {
        "raw_mb51_311_manual_qty": 0.0,
        "raw_mb51_101_131_gr_qty": 0.0,
        "raw_mb51_scrap_qty": 0.0,
        "raw_mb51_261_total_qty": 12.0,
    }
    dec = decompose_unknown_with_mb51(rem_after_gr=20.0, attr_gr=0.0, mb51_stats=st)
    assert dec["attr_mb51_261_consumed"] == 12.0
    assert dec["attr_unknown_residual"] == 8.0
    assert round(sum(dec.values()), 2) == 20.0


def test_map_d108_verdict_to_03_cause_reclassifies_all_branches():
    # 1. D + D107 candidate
    m_d = map_d108_verdict_to_03_cause({
        "d108_code": "D",
        "d107_status": "CANDIDATE",
        "gi_link_summary": "mvt 311 SL01->SP31 50 EA",
        "gi_01_status": "INSUFFICIENT_SURPLUS_OR_ACCURACY_GUARD",
        "gi_01_plan_time": "2026-10-29 06:02:00",
        "is_bug_candidate": False,
        "d108_detail": "No stock anywhere",
    })
    assert m_d["primary"] == "D"
    assert m_d["detailed_code"] == "D"
    assert m_d["severity"] == "CRITICAL (🔴)"
    assert "D107 삭제 후보" in m_d["d_sub_note"]

    # 2. A (book discrepancy) & B (GR wait) & F (missing GR)
    m_a = map_d108_verdict_to_03_cause({"d108_code": "A", "d107_status": "EXCLUDED", "is_bug_candidate": False})
    assert m_a["primary"] == "A" and m_a["severity"] == "CRITICAL (🔴)"

    m_b = map_d108_verdict_to_03_cause({"d108_code": "B", "d107_status": "EXCLUDED", "is_bug_candidate": False})
    assert m_b["primary"] == "B" and m_b["severity"] == "TEMPORARY (🟡)"

    m_f = map_d108_verdict_to_03_cause({"d108_code": "F", "d107_status": "EXCLUDED", "is_bug_candidate": False})
    assert m_f["primary"] == "F" and m_f["severity"] == "CRITICAL (🔴)"

    # 3. B-2: No linked GI error (None or GI_LINK_GONE) -> returns None so 03 falls through to normal causes!
    assert map_d108_verdict_to_03_cause(None) is None
    assert map_d108_verdict_to_03_cause({"d108_code": "GI_LINK_GONE"}) is None

    # Actual new GI error after 01 collection -> OTHER_GI_NEW_1 (TEMPORARY)
    m_new = map_d108_verdict_to_03_cause({"d108_code": "ANOMALY_NEW_AFTER_01", "d107_status": "EXCLUDED", "is_bug_candidate": False})
    assert m_new["primary"] == "OTHER"
    assert m_new["detailed_code"] == "OTHER_GI_NEW_1"
    assert m_new["severity"] == "TEMPORARY (🟡)"
    assert m_new["is_bug_candidate"] is False

    m_guard = map_d108_verdict_to_03_cause({
        "d108_code": "ANOMALY_GUARD_UNPOSTED",
        "d107_status": "EXCLUDED",
        "is_bug_candidate": True,
        "gi_link_summary": "mvt 311 SL01->SP31 1,100 EA",
        "gi_01_status": "SKIPPED_PENDING_INTERFACE_SL00",
    })
    assert m_guard["primary"] == "OTHER"
    assert m_guard["detailed_code"] == "OTHER_GI_GUARD_2"
    assert m_guard["severity"] == "CRITICAL (🔴)"
    assert m_guard["is_bug_candidate"] is True



def test_b5_strict_311_excludes_411_after_obs_and_caps_per_sloc():
    rows = [
        # 411 consignment conversion in SP11 must NOT be counted as MB51_311_MANUAL
        {"mvt_type": "411", "sloc": "SP11", "counter_sloc": "SP11", "mat_doc_no": "4991", "doc_header_text": "", "qty_entry": 5000.0, "entry_datetime_edt": "2026-10-25 11:37:48"},
        # 311 after t1_edt must be excluded
        {"mvt_type": "311", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4992", "doc_header_text": "", "qty_entry": 200.0, "entry_datetime_edt": "2026-10-28 08:20:00"},
        # 311 into SP31 (+400) with 312 reversal (-100) before t1_edt -> net +300 into SP31, 0 into SL49
        {"mvt_type": "311", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4993", "doc_header_text": "", "qty_entry": 400.0, "entry_datetime_edt": "2026-10-28 07:30:00"},
        {"mvt_type": "312", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4994", "doc_header_text": "", "qty_entry": -100.0, "entry_datetime_edt": "2026-10-28 07:35:00"},
    ]
    # Suppose SP31 reduced by 120 EA and SL49 reduced by 10 EA (total rem=130 EA).
    # Because SL49 had 0 manual 311 inflow, per_sloc_cap caps manual 311 at 120 EA (leaving 10 EA residual for SL49)!
    st = aggregate_mb51_window_for_material(
        rows,
        target_slocs={"SP31", "SL49", "SP11"},
        auto_doc_nos=set(),
        per_sloc_cap={"SP31": 120.0, "SL49": 10.0},
        t0_edt="2026-10-28 06:21:28",
        t1_edt="2026-10-28 08:15:27",
    )
    assert st["raw_mb51_411_consign_qty"] == 0.0  # 2026-10-25 < t0_edt
    assert st["raw_mb51_311_unclipped_qty"] == 300.0  # 400 - 100 (after-t1 200 excluded)
    assert st["raw_mb51_311_manual_qty"] == 120.0  # capped to SP31's 120 EA reduction!
    dec = decompose_unknown_with_mb51(rem_after_gr=130.0, attr_gr=0.0, mb51_stats=st)
    assert dec["attr_mb51_311_manual"] == 120.0
    assert dec["attr_unknown_residual"] == 10.0


def test_190_b1_dedup_by_doc_and_zeile_preserves_multi_line_and_drops_join_dups():
    rows = [
        # ZEILE 2 (+1 EA) duplicated twice (e.g., join duplicate) -> must count ONCE
        {"mvt_type": "311", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4909999901", "mat_doc_item": "2", "mat_doc_year": "2026", "doc_header_text": "", "qty_entry": 1.0, "entry_datetime_edt": "2026-10-27 08:23:37"},
        {"mvt_type": "311", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4909999901", "mat_doc_item": "2", "mat_doc_year": "2026", "doc_header_text": "", "qty_entry": 1.0, "entry_datetime_edt": "2026-10-27 08:23:37"},
        # ZEILE 4 (+1 EA) is a distinct SAP document item in the same doc -> must ALSO count
        {"mvt_type": "311", "sloc": "SP31", "counter_sloc": "SL01", "mat_doc_no": "4909999901", "mat_doc_item": "4", "mat_doc_year": "2026", "doc_header_text": "", "qty_entry": 1.0, "entry_datetime_edt": "2026-10-27 08:23:37"},
    ]
    st = aggregate_mb51_window_for_material(rows, target_slocs={"SP31"}, auto_doc_nos=set())
    assert st["raw_mb51_311_manual_qty"] == 2.0
    assert len(st["manual_311_raw_docs"]) == 2
    assert [d["mat_doc_item"] for d in st["manual_311_raw_docs"]] == ["2", "4"]

