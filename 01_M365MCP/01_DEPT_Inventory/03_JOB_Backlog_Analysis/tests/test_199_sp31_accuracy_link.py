import sqlite3
import sys
from pathlib import Path

JOB_DIR = Path(__file__).resolve().parent.parent
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from d103_mart_builder import (
    SP31_01_ACCURACY_LINK_TAG,
    load_01_sp31_accuracy_transfers,
    match_sp31_01_accuracy_link_72h,
)


def _make_mock_01_db(db_path: Path):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE transfer_posting_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            executed_at TEXT,
            material TEXT,
            from_sloc TEXT,
            to_sloc TEXT,
            transfer_qty REAL,
            doc_number TEXT,
            post_status TEXT,
            posting_type TEXT,
            reason TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE gi_error_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            material TEXT,
            mvt TEXT,
            from_sloc TEXT,
            to_sloc TEXT,
            qty REAL
        )
        """
    )
    # 1) Genuine net SP31->SL01 D77 accuracy move (within 72h of 2026-10-29 06:23 EDT)
    conn.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261029_020000', '2026-10-29 06:02:40', '99287459', 'SP31', 'SL01', 109.0, '4990000173', 'POSTED', 'PARTIAL_ACCURACY_REBALANCING', 'D77')
        """
    )
    # 2) Genuine net SP31->SL02 READY move clearing SL02->SL01 (no return to SP31)
    conn.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261028_120000', '2026-10-28 16:04:35', '99363250', 'SP31', 'SL02', 140.0, '4990000168', 'POSTED', 'READY_FOR_EXECUTION', 'Full')
        """
    )
    conn.execute(
        "INSERT INTO gi_error_snapshots (batch_id, material, mvt, from_sloc, to_sloc, qty) VALUES ('RUN_20261028_120000', '99363250', '311', 'SL02', 'SL01', 140.0)"
    )
    # 3) Same-cycle zero-drain roundtrip READY move (SL01->SP31 64 >= 14 -> must NOT be tagged as SP31 drain)
    conn.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261029_020000', '2026-10-29 06:02:28', '99287458', 'SP31', 'SL01', 14.0, '4990000172', 'POSTED', 'READY_FOR_EXECUTION', 'Roundtrip')
        """
    )
    conn.execute(
        "INSERT INTO gi_error_snapshots (batch_id, material, mvt, from_sloc, to_sloc, qty) VALUES ('RUN_20261029_020000', '99287458', '311', 'SL01', 'SP31', 64.0)"
    )
    # 4) Manual CORR_ reversal batch -> must NOT be treated as 01 accuracy transfer
    conn.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('CORR_20261026_160213', '2026-10-28 20:00:00', '99142875', 'SP31', 'SL02', 556.0, '4990000100', 'POSTED', 'CORRECTION_REVERSAL', 'Reversal')
        """
    )
    conn.commit()
    conn.close()


def test_sp31_01_accuracy_link_72h_filters_and_preserves_cause(tmp_path):
    db_p = tmp_path / "gr_gi_history.db"
    _make_mock_01_db(db_p)

    c01 = sqlite3.connect(f"file:{db_p.as_posix()}?mode=ro", uri=True)
    c01.row_factory = sqlite3.Row
    acc_xfers = load_01_sp31_accuracy_transfers(c01)
    c01.close()

    mats = {x["material"] for x in acc_xfers}
    assert mats == {"99287459", "99363250"}

    # Within 72h & SP31 in slocs -> tagged
    hit_d77 = match_sp31_01_accuracy_link_72h("99287459", "SP31", "2026-10-29 06:23:28", acc_xfers)
    assert hit_d77["tag"] == SP31_01_ACCURACY_LINK_TAG
    assert hit_d77["qty_72h"] == 109.0
    assert "4990000173" in hit_d77["detail"]

    # Multi-SLoc including SP31 -> tagged
    hit_rdy = match_sp31_01_accuracy_link_72h("99363250", "SP31,SL49", "2026-10-29 06:23:28", acc_xfers)
    assert hit_rdy["tag"] == SP31_01_ACCURACY_LINK_TAG
    assert hit_rdy["qty_72h"] == 140.0

    # Non-SP31 backlog SLoc (e.g. SL49 only) -> NOT tagged
    miss_sl49 = match_sp31_01_accuracy_link_72h("99287459", "SL49", "2026-10-29 06:23:28", acc_xfers)
    assert miss_sl49["tag"] == "-"

    # Older than 72h -> NOT tagged
    miss_old = match_sp31_01_accuracy_link_72h("99363250", "SP31", "2026-11-02 06:23:28", acc_xfers)
    assert miss_old["tag"] == "-"

    # Posting occurred AFTER snapshot -> NOT tagged (strict causal window <= snap_edt)
    miss_future = match_sp31_01_accuracy_link_72h("99287459", "SP31", "2026-10-29 01:00:00", acc_xfers)
    assert miss_future["tag"] == "-"


def test_sp31_72h_window_uses_edt_executed_at_with_mixed_kst_mb51_docs(tmp_path):
    """
    #200: Even when MB51 contains KST document timestamps (`CPUDT+CPUTM` = EDT + 13h) or
    mismatched SLoc rows, the 72h window and `from->to` / timestamp display MUST strictly use
    `transfer_posting_history.executed_at` (EDT) and `from_sloc`/`to_sloc`, while `mb51_doc_check`
    flags `OK` vs `WARN` for `doc_number` verification only.
    """
    db_01 = tmp_path / "gr_gi_kst_mix.db"
    db_06 = tmp_path / "ops_raw_kst_mix.db"

    conn01 = sqlite3.connect(str(db_01))
    conn01.execute(
        """
        CREATE TABLE transfer_posting_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            executed_at TEXT,
            material TEXT,
            from_sloc TEXT,
            to_sloc TEXT,
            transfer_qty REAL,
            doc_number TEXT,
            post_status TEXT,
            posting_type TEXT,
            reason TEXT
        )
        """
    )
    conn01.execute(
        """
        CREATE TABLE gi_error_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT,
            material TEXT,
            mvt TEXT,
            from_sloc TEXT,
            to_sloc TEXT,
            qty REAL
        )
        """
    )
    # Case A: executed_at UTC 2026-10-29 06:02:40 -> EDT 2026-10-29 02:02:40 (BEFORE snap 06:23:28 EDT -> IN 72h window),
    # even though KST CPUDT+CPUTM is 2026-10-29 15:02:40 (which would look AFTER 06:23:28 if KST were mistakenly used).
    conn01.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261029_020000', '2026-10-29 06:02:40', '99100001', 'SP31', 'SL01', 109.0, '4990000901', 'POSTED', 'PARTIAL_ACCURACY_REBALANCING', 'KST doc 2026-10-29 15:02:40')
        """
    )
    # Case B: executed_at UTC 2026-10-26 09:00:00 -> EDT 2026-10-26 05:00:00 (OLDER than 72h before 2026-10-29 06:23:28 -> OUT of window),
    # even though KST CPUDT+CPUTM is 2026-10-26 18:00:00 (which would look INSIDE [10/26 06:23, 10/29 06:23] if KST were mistakenly used).
    conn01.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261026_050000', '2026-10-26 09:00:00', '99100002', 'SP31', 'SL01', 50.0, '4990000902', 'POSTED', 'PARTIAL_ACCURACY_REBALANCING', 'KST doc 2026-10-26 18:00:00')
        """
    )
    # Case C: SP31->SL02 in transfer_posting_history, while MB51 has mismatched SL01 destination ->
    # output detail MUST preserve SP31->SL02 & EDT executed_at, and mb51_doc_check MUST emit WARN.
    conn01.execute(
        """
        INSERT INTO transfer_posting_history
        (batch_id, executed_at, material, from_sloc, to_sloc, transfer_qty, doc_number, post_status, posting_type, reason)
        VALUES ('RUN_20261027_100000', '2026-10-27 14:02:48', '99100003', 'SP31', 'SL02', 4.0, '4990000903', 'POSTED', 'PARTIAL_ACCURACY_REBALANCING', 'D77')
        """
    )
    conn01.commit()
    conn01.close()

    conn06 = sqlite3.connect(str(db_06))
    conn06.execute(
        """
        CREATE TABLE mb51_raw (
            mat_doc_no TEXT,
            mat_doc_item TEXT,
            material TEXT,
            sloc TEXT,
            counter_sloc TEXT,
            mvt_type TEXT,
            qty_entry REAL,
            entry_date TEXT,
            entry_time TEXT,
            entry_datetime_edt TEXT
        )
        """
    )
    # Case A in MB51: KST 2026-10-29 15:02:40, matching SP31->SL01 109 EA
    conn06.executemany(
        "INSERT INTO mb51_raw VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("4990000901", "1", "99100001", "SP31", "SL01", "311", -109.0, "2026-10-29", "15:02:40", "2026-10-29 02:02:40"),
            ("4990000901", "2", "99100001", "SL01", "SP31", "311", 109.0, "2026-10-29", "15:02:40", "2026-10-29 02:02:40"),
            # Case B in MB51: KST 2026-10-26 18:00:00 (outside 72h EDT window!)
            ("4990000902", "1", "99100002", "SP31", "SL01", "311", -50.0, "2026-10-26", "18:00:00", "2026-10-26 05:00:00"),
            ("4990000902", "2", "99100002", "SL01", "SP31", "311", 50.0, "2026-10-26", "18:00:00", "2026-10-26 05:00:00"),
            # Case C in MB51: intentionally mismatched SL01 instead of SL02
            ("4990000903", "1", "99100003", "SP31", "SL01", "311", -4.0, "2026-10-27", "23:02:46", "2026-10-27 10:02:46"),
            ("4990000903", "2", "99100003", "SL01", "SP31", "311", 4.0, "2026-10-27", "23:02:46", "2026-10-27 10:02:46"),
        ],
    )
    conn06.commit()
    conn06.close()

    c01 = sqlite3.connect(f"file:{db_01.as_posix()}?mode=ro", uri=True)
    c01.row_factory = sqlite3.Row
    c06 = sqlite3.connect(f"file:{db_06.as_posix()}?mode=ro", uri=True)
    c06.row_factory = sqlite3.Row
    acc_xfers = load_01_sp31_accuracy_transfers(c01, c06=c06)
    c01.close()
    c06.close()

    snap_edt = "2026-10-29 06:23:28"

    # Case A: Matched using EDT executed_at (2026-10-29 02:02:40), NOT KST 15:02:40
    hit_a = match_sp31_01_accuracy_link_72h("99100001", "SP31", snap_edt, acc_xfers)
    assert hit_a["tag"] == SP31_01_ACCURACY_LINK_TAG
    assert hit_a["qty_72h"] == 109.0
    assert "2026-10-29 02:02:40 SP31->SL01 109EA" in hit_a["detail"]
    assert hit_a["mb51_doc_check"] == "OK(4990000901)"

    # Case B: Excluded (>72h in EDT: 2026-10-26 05:00:00 < 2026-10-26 06:23:28), despite KST 18:00:00
    miss_b = match_sp31_01_accuracy_link_72h("99100002", "SP31", snap_edt, acc_xfers)
    assert miss_b["tag"] == "-"

    # Case C: Preserves SP31->SL02 and EDT 2026-10-27 10:02:48 from transfer_posting_history, flags WARN on MB51 mismatch
    hit_c = match_sp31_01_accuracy_link_72h("99100003", "SP31", snap_edt, acc_xfers)
    assert hit_c["tag"] == SP31_01_ACCURACY_LINK_TAG
    assert "2026-10-27 10:02:48 SP31->SL02 4EA" in hit_c["detail"]
    assert hit_c["mb51_doc_check"].startswith("WARN: MB51 불일치(4990000903")
