"""
Unit tests for D104 / Thread 166, 167, 168:
- `ops_raw.db` schema initialization (`mb51_raw`, `mvt_type_dict`, `collect_runs`, `collect_watermark`)
- 35 `USER_LISTED` movement types seeded with `qty_direction IS NULL` (including 261/262 and 901/902)
- Automatic `UNDEFINED` registration when unseen movement types (e.g., 551, 701) appear in MB51
- `qty_direction` auto-inference (`+`, `-`, `BOTH`) from observed MB51 rows
- Blackout window detection (`is_in_blackout_window`)
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import sys

JOB_DIR = Path(__file__).resolve().parents[1]
if str(JOB_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_DIR))

from db_manager import (
    EDT_TZ,
    ensure_mvt_type_registered,
    get_connection,
    init_ops_raw_db,
    is_in_blackout_window,
    update_watermark,
)
from mb51_collector import (
    compute_daily_catchup_dates,
    convert_sap_kst_to_edt,
    run_daily_scheduled_mb51,
)


def test_ops_raw_schema_and_mvt_seed(tmp_path: Path):
    test_db = tmp_path / "test_ops_raw.db"
    init_ops_raw_db(test_db)

    conn = get_connection(test_db, read_only=False)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"mb51_raw", "mvt_type_dict", "collect_runs", "collect_watermark"}.issubset(tables)

        # Verify 35 seeded USER_LISTED movement types (31 from 167 + 261/262 + 901/902 from 168)
        rows = conn.execute("SELECT * FROM mvt_type_dict WHERE definition_status = 'USER_LISTED'").fetchall()
        assert len(rows) == 35
        codes = {r["mvt_code"] for r in rows}
        assert {"101", "102", "122", "261", "262", "311", "312", "343", "553", "593", "601", "901", "902", "953"}.issubset(codes)

        # Verify qty_direction is NULL initially (per Thread 167 instruction)
        assert all(r["qty_direction"] is None for r in rows)

        # Verify unseen movement type (e.g. 701) auto-registers as UNDEFINED
        with conn:
            is_new = ensure_mvt_type_registered(conn, "701", "2026-10-27", "+")
            assert is_new is True
            is_new_again = ensure_mvt_type_registered(conn, "701", "2026-10-28", "-")
            assert is_new_again is False

            # Also verify 311 direction updates to BOTH when both + and - lines are seen
            ensure_mvt_type_registered(conn, "311", "2026-10-27", "-")
            ensure_mvt_type_registered(conn, "311", "2026-10-27", "+")

        r_701 = conn.execute("SELECT * FROM mvt_type_dict WHERE mvt_code = '701'").fetchone()
        assert r_701["definition_status"] == "UNDEFINED"
        assert r_701["qty_direction"] == "BOTH"
        assert r_701["sample_count"] == 2

        r_311 = conn.execute("SELECT * FROM mvt_type_dict WHERE mvt_code = '311'").fetchone()
        assert r_311["definition_status"] == "USER_LISTED"
        assert r_311["qty_direction"] == "BOTH"
    finally:
        conn.close()


def test_watermark_and_blackout_window(tmp_path: Path):
    test_db = tmp_path / "test_wm.db"
    init_ops_raw_db(test_db)

    update_watermark("sap_mb51", "2026-10-01", "RUN_MB51_20261001", "SUCCESS", test_db)
    update_watermark("sap_mb51", "2026-10-02", "RUN_MB51_20261002", "SUCCESS", test_db)

    conn = get_connection(test_db, read_only=True)
    wm = conn.execute("SELECT * FROM collect_watermark WHERE source_name='sap_mb51'").fetchone()
    conn.close()
    assert wm["backfill_start_date"] == "2026-10-01"
    assert wm["last_completed_date"] == "2026-10-02"

    # Check blackout window at 02:15 EDT (scheduled MB51 time -> should be CLEAR)
    blocked_0215, reason_0215 = is_in_blackout_window(datetime(2026, 10, 29, 2, 15, tzinfo=EDT_TZ))
    assert blocked_0215 is False
    assert reason_0215 == "CLEAR"

    # Check blackout window at 06:20 EDT (morning peak -> should be BLOCKED)
    blocked_0620, _ = is_in_blackout_window(datetime(2026, 10, 29, 6, 20, tzinfo=EDT_TZ))
    assert blocked_0620 is True

    # Check blackout window at 04:00 EDT (05 scan -> should be BLOCKED)
    blocked_0400, _ = is_in_blackout_window(datetime(2026, 10, 29, 4, 0, tzinfo=EDT_TZ))
    assert blocked_0400 is True

    # Daytime 30-min inter-cycle windows (:05~:22 and :35~:52 -> CLEAR; :23~:34 and :53~:04 -> BLOCKED)
    assert is_in_blackout_window(datetime(2026, 10, 29, 9, 10, tzinfo=EDT_TZ))[0] is False
    assert is_in_blackout_window(datetime(2026, 10, 29, 9, 40, tzinfo=EDT_TZ))[0] is False
    assert is_in_blackout_window(datetime(2026, 10, 29, 9, 25, tzinfo=EDT_TZ))[0] is True
    assert is_in_blackout_window(datetime(2026, 10, 29, 9, 55, tzinfo=EDT_TZ))[0] is True

    # Verify Thread 172 KST CPUDT watermark and completed_edt_date (= last_completed_cpudt_kst - 1 day)
    assert wm["last_completed_cpudt_kst"] == "2026-10-02"
    assert wm["completed_edt_date"] == "2026-10-01"


def test_convert_sap_kst_to_edt_dst_transition():
    # Before US DST ends (EDT = UTC-4, KST = UTC+9 -> 13h difference)
    assert convert_sap_kst_to_edt("2026-10-28", "05:02:41") == "2026-10-27 16:02:41"
    # After US DST ends on 2026-11-01 02:00 (EST = UTC-5, KST = UTC+9 -> 14h difference)
    assert convert_sap_kst_to_edt("2026-11-02", "05:02:41") == "2026-11-01 15:02:41"


def test_daily_0215_catchup_range_and_cutoff_preserves_watermark(tmp_path: Path, monkeypatch):
    test_db = tmp_path / "test_daily_0215.db"
    init_ops_raw_db(test_db)

    # 1) First scheduled run on 2026-10-30 02:15 EDT (= 2026-10-30 15:15 KST -> closed KST yesterday = 2026-10-29)
    update_watermark("sap_mb51", "2026-10-28", "RUN_20261028", "SUCCESS", test_db)
    now_1030_0215 = datetime(2026, 10, 30, 2, 15, tzinfo=EDT_TZ)
    dates, wm_str, closed_kst = compute_daily_catchup_dates(test_db, now_edt=now_1030_0215, max_days=3)
    assert wm_str == "2026-10-28"
    assert closed_kst == "2026-10-29"
    assert dates == ["2026-10-29"]  # Never touches 10/01..10/19

    # 2) Multi-day lag capped at max_days=3
    test_db_lag = tmp_path / "test_daily_lag.db"
    init_ops_raw_db(test_db_lag)
    update_watermark("sap_mb51", "2026-10-24", "RUN_20261024", "SUCCESS", test_db_lag)
    dates_lag, _, _ = compute_daily_catchup_dates(test_db_lag, now_edt=now_1030_0215, max_days=3)
    assert dates_lag == ["2026-10-25", "2026-10-26", "2026-10-27"]

    # 3) Cutoff at 02:50 EDT stops cleanly and preserves watermark at 2026-10-28
    import mb51_collector
    monkeypatch.setattr(mb51_collector, "_is_job01_0200_still_running", lambda *a, **k: (False, "CLEAR"))
    now_after_cutoff = datetime(2026, 10, 30, 2, 50, 5, tzinfo=EDT_TZ)
    res_cut = run_daily_scheduled_mb51(
        cutoff_hhmm="02:50",
        max_days=3,
        db_path=test_db,
        now_fn=lambda: now_after_cutoff,
        extract_fn=lambda **kw: (_ for _ in ()).throw(AssertionError("Should not extract after 02:50 cutoff")),
    )
    assert res_cut["status"] == "CUTOFF_STOPPED"
    assert res_cut["watermark_after"] == "2026-10-28"

    # 4) SAP timeout/failure preserves watermark at 2026-10-28
    res_fail = run_daily_scheduled_mb51(
        cutoff_hhmm="02:50",
        max_days=3,
        db_path=test_db,
        now_fn=lambda: now_1030_0215,
        extract_fn=lambda **kw: (_ for _ in ()).throw(TimeoutError("SAP timeout at 02:50")),
    )
    assert res_fail["status"] == "FAILED"
    assert res_fail["watermark_after"] == "2026-10-28"
    conn = get_connection(test_db, read_only=True)
    wm_after = conn.execute("SELECT last_completed_cpudt_kst FROM collect_watermark WHERE source_name='sap_mb51'").fetchone()[0]
    conn.close()
    assert wm_after == "2026-10-28"

def test_thread_203_daemon_alive_cycle_completed_proceeds_immediately(tmp_path: Path, monkeypatch):
    """
    Thread 203 regression test:
    1) Even when `hourly_gi_pipeline.py --daemon` is alive in process table, if `sap_session_lock` is free
       and `pipeline_run_metrics` shows `RUN_20261030_020000` = `COMPLETED`, `_is_job01_0200_still_running`
       returns `(False, 'CLEAR')` immediately (no 35-min hang to 02:50 cutoff).
    2) When `sap_session_lock` is held OR `pipeline_run_metrics` / `gi_error_snapshots` shows an active
       unfinished cycle in the current slot, `_is_job01_0200_still_running` returns `(True, ...)` and
       `run_daily_scheduled_mb51` waits until the cycle completes.
    """
    import sqlite3
    import psutil
    import mb51_collector
    from mb51_collector import _is_job01_0200_still_running

    # Simulate resident 24H daemon process in psutil.process_iter
    class _FakeProc:
        def __init__(self, cmdline):
            self.info = {"pid": 99999, "name": "python.exe", "cmdline": cmdline}

    monkeypatch.setattr(
        psutil,
        "process_iter",
        lambda *a, **kw: [
            _FakeProc(["python.exe", "hourly_gi_pipeline.py", "--daemon"]),
            _FakeProc(["python.exe", "pipeline_monitor.py"]),
        ],
    )

    class _FakeLock:
        def __init__(self, held: bool = False, owner: str = ""):
            self._held = held
            self._owner = owner

        def _read_lock_info(self):
            return {"pid": 12345, "owner": self._owner} if self._held else {}

        def _is_lock_stale(self, info):
            return not self._held

    job01_db = tmp_path / "gr_gi_history.db"
    c01 = sqlite3.connect(str(job01_db))
    c01.execute("CREATE TABLE pipeline_run_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT, status TEXT)")
    c01.execute("CREATE TABLE gi_error_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT)")
    c01.execute("INSERT INTO pipeline_run_metrics (batch_id, status) VALUES ('RUN_20261030_020000', 'COMPLETED')")
    c01.commit()
    c01.close()

    now_0215 = datetime(2026, 10, 30, 2, 15, 1, tzinfo=EDT_TZ)

    # Case 1: Daemon process alive + lock free + 02:00 cycle COMPLETED -> immediate CLEAR!
    busy, reason = _is_job01_0200_still_running(
        now_edt=now_0215, job01_db_path=job01_db, lock_obj=_FakeLock(held=False)
    )
    assert busy is False
    assert reason == "CLEAR"

    # Case 2: SAP session lock held -> waits!
    busy_lk, reason_lk = _is_job01_0200_still_running(
        now_edt=now_0215,
        job01_db_path=job01_db,
        lock_obj=_FakeLock(held=True, owner="01_JOB_GR_GI_Error_RUN_20261030_020000"),
    )
    assert busy_lk is True
    assert "SAP_LOCK_HELD" in reason_lk

    # Case 3: Current slot started in pipeline_run_metrics with status='RUNNING' -> waits!
    c01 = sqlite3.connect(str(job01_db))
    c01.execute("DELETE FROM pipeline_run_metrics")
    c01.execute("INSERT INTO pipeline_run_metrics (batch_id, status) VALUES ('RUN_20261030_020002', 'RUNNING')")
    c01.commit()
    c01.close()
    busy_run, reason_run = _is_job01_0200_still_running(
        now_edt=now_0215, job01_db_path=job01_db, lock_obj=_FakeLock(held=False)
    )
    assert busy_run is True
    assert "01_CYCLE_IN_PROGRESS(RUN_20261030_020002:RUNNING)" == reason_run

    # Case 4: STEP 1 snapshot written in gi_error_snapshots without terminal pipeline_run_metrics row -> waits!
    c01 = sqlite3.connect(str(job01_db))
    c01.execute("DELETE FROM pipeline_run_metrics")
    c01.execute("INSERT INTO gi_error_snapshots (batch_id) VALUES ('RUN_20261030_020005')")
    c01.commit()
    c01.close()
    busy_snap, reason_snap = _is_job01_0200_still_running(
        now_edt=now_0215, job01_db_path=job01_db, lock_obj=_FakeLock(held=False)
    )
    assert busy_snap is True
    assert "01_CYCLE_IN_PROGRESS(RUN_20261030_020005:SNAPSHOT_ACTIVE)" == reason_snap

    # Case 5: End-to-end wait -> release transition in run_daily_scheduled_mb51
    ops_db = tmp_path / "ops_raw_203.db"
    init_ops_raw_db(ops_db)
    update_watermark("sap_mb51", "2026-10-28", "RUN_20261028", "SUCCESS", ops_db)

    fake_lock = _FakeLock(held=True, owner="01_JOB_GR_GI_Error_RUN_20261030_020005")
    sleep_calls = []

    def _on_sleep(sec):
        sleep_calls.append(sec)
        # Simulate 01_JOB finishing after 1st poll: release lock + write COMPLETED
        fake_lock._held = False
        conn_fin = sqlite3.connect(str(job01_db))
        conn_fin.execute("INSERT INTO pipeline_run_metrics (batch_id, status) VALUES ('RUN_20261030_020005', 'COMPLETED')")
        conn_fin.commit()
        conn_fin.close()

    monkeypatch.setattr(
        mb51_collector,
        "_is_job01_0200_still_running",
        lambda dt=None: _is_job01_0200_still_running(now_edt=dt, job01_db_path=job01_db, lock_obj=fake_lock),
    )
    monkeypatch.setattr(
        mb51_collector,
        "parse_and_ingest_mb51_file",
        lambda **kw: {
            "raw_row_count": 100,
            "inserted_row_count": 95,
            "dedup_skipped_count": 5,
            "new_undefined_mvt_count": 0,
            "new_undefined_mvt_codes": [],
        },
    )

    dummy_xls = tmp_path / "dummy.XLS"
    dummy_xls.write_text("ok", encoding="utf-8")
    res_ok = run_daily_scheduled_mb51(
        cutoff_hhmm="02:50",
        max_days=3,
        db_path=ops_db,
        now_fn=lambda: now_0215,
        extract_fn=lambda **kw: (dummy_xls, 4.2),
        sleep_fn=_on_sleep,
    )
    assert len(sleep_calls) == 1
    assert res_ok["status"] == "SUCCESS"
    assert res_ok["completed_dates"] == ["2026-10-29"]
    assert res_ok["watermark_after"] == "2026-10-29"


def test_203_backfill_waits_for_active_job01_and_preserves_higher_watermark(tmp_path: Path, monkeypatch):
    """
    Thread #203 backfill safety regression test:
    1) `run_cpudt_backfill` checks both `is_in_blackout_window` AND `_is_job01_0200_still_running`
       before launching SAP `/nMB51` during daytime between-cycle windows.
    2) Backfilling older dates (`2026-10-01`) when `collect_watermark` is already at `2026-11-01`
       monotonically preserves `last_completed_cpudt_kst = '2026-11-01'`.
    """
    import mb51_collector

    ops_db = tmp_path / "ops_backfill_203.db"
    init_ops_raw_db(ops_db)
    update_watermark("sap_mb51", "2026-11-01", "MB51_20261102_021733", "SUCCESS", ops_db)

    raw_dir = tmp_path / "raw_exports"
    raw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mb51_collector, "RAW_EXPORTS_DIR", raw_dir)
    monkeypatch.setattr(mb51_collector, "_write_cpudt_backfill_report", lambda dbp: {"ok": True})

    # Simulate 09:40 EST (between 09:30 and 10:00 cycles: blackout CLEAR, 1st poll 01_JOB overrun, 2nd poll CLEAR)
    est_0940 = datetime(2026, 11, 2, 9, 40, tzinfo=EDT_TZ)
    monkeypatch.setattr(mb51_collector, "now_edt_dt", lambda: est_0940)
    monkeypatch.setattr(mb51_collector, "now_edt_str", lambda: "2026-11-02 09:40:00")

    busy_states = [(True, "SAP_LOCK_HELD(pid=4321, owner=01_JOB)"), (False, "CLEAR")]
    monkeypatch.setattr(mb51_collector, "_is_job01_0200_still_running", lambda *a, **kw: busy_states.pop(0))

    sleeps = []
    monkeypatch.setattr(mb51_collector.time, "sleep", lambda s: sleeps.append(s))

    dummy_xls = raw_dir / "MB51_TEST_CPUDT_20261001_20261001_094015.XLS"
    dummy_xls.write_text("dummy", encoding="utf-8")
    monkeypatch.setattr(mb51_collector, "extract_mb51_sap_file", lambda **kw: (dummy_xls, 12.3))

    def _fake_ingest(**kw):
        update_watermark("sap_mb51", kw["target_date_to"], "MB51_BACKFILL_20261001", "SUCCESS", kw["db_path"])
        return {
            "raw_row_count": 50,
            "inserted_row_count": 50,
            "dedup_skipped_count": 0,
            "new_undefined_mvt_count": 0,
        }

    monkeypatch.setattr(mb51_collector, "parse_and_ingest_mb51_file", _fake_ingest)

    mb51_collector.run_cpudt_backfill("2026-10-01", "2026-10-01", cutoff_hhmm="09:52", db_path=ops_db)
    assert 15.0 in sleeps  # Waited once for active 01_JOB lock before extracting!

    conn = get_connection(ops_db, read_only=True)
    wm = conn.execute("SELECT * FROM collect_watermark WHERE source_name='sap_mb51'").fetchone()
    conn.close()
    assert wm["last_completed_cpudt_kst"] == "2026-11-01"
    assert wm["last_completed_date"] == "2026-11-01"
