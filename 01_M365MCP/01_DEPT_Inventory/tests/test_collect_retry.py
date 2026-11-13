"""
Unit Tests for Collection Retry & Autonomous Recovery Engine (D41 / collect_retry.py)
====================================================================================
Tests User Decision D41 & Claude Review 041 Fixes:
(a) 2 failures then 3rd attempt succeeds -> returns result, 2 retries recorded
(b) Login failure / invalid password -> 0 retries, immediate abort with FatalCollectionError
(c) Time budget exceeded -> raises TimeBudgetExceededError
(d) Paired collection: WMS delay causes diff > 300s -> older snapshot re-collected until synchronized
(e) SAP lock retry hook: released during backoff, re-acquired by same owner, refuses other owner's lock
(f) Targeted driver cleanup: kills only specified driver PID & children, never foreign processes
(g) Selenium session lock: mutex file lock enforces strictly 1 browser instance
(h) Windows file lock (WinError 5 / PermissionError): treated as transient retryable, not fatal
"""

import sys
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

HQ_DIR = Path(__file__).resolve().parent.parent.parent / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from collect_retry import (
    execute_with_retry,
    collect_paired_snapshots_resilient,
    classify_collection_error,
    make_sap_lock_retry_hook,
    cleanup_driver_process,
    RetryableCollectionError,
    FatalCollectionError,
    TimeBudgetExceededError
)
from sap_lock import SAPSessionLock
from selenium_lock import SeleniumSessionLock


def test_two_failures_then_third_attempt_succeeds():
    """
    (a) 2 failures then 3rd succeeds:
    First 2 attempts raise RetryableCollectionError.
    3rd attempt returns valid file path.
    Verifies total attempts=3, retries=2, and backoff sleeps were called.
    """
    mock_collector = MagicMock()
    mock_collector.side_effect = [
        RetryableCollectionError("Temporary network timeout"),
        RetryableCollectionError("SSO window not found in foreground"),
        Path("/mock/WMS_StockList_HL.xlsx")
    ]

    with patch("time.sleep") as mock_sleep, \
         patch("collect_retry.recover_collection_environment") as mock_recover:
        res, metrics = execute_with_retry(
            collector_func=mock_collector,
            job_name="TestJob",
            time_budget_seconds=600.0,
            max_retries=3,
            backoff_delays=[0.01, 0.02, 0.03]
        )

    assert res == Path("/mock/WMS_StockList_HL.xlsx")
    assert metrics["success"] is True
    assert metrics["attempts"] == 3
    assert metrics["retries"] == 2
    assert len(metrics["error_history"]) == 2
    assert mock_sleep.call_count == 2
    assert mock_recover.call_count == 2


def test_fatal_login_error_aborts_immediately_zero_retries():
    """
    (b) Login failure:
    Raises error indicating password/credentials error.
    Must abort immediately without any retries (attempts=1, retries=0).
    """
    mock_collector = MagicMock()
    mock_collector.side_effect = Exception("WMS Authentication failed: Invalid password for user")

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(FatalCollectionError) as exc_info:
            execute_with_retry(
                collector_func=mock_collector,
                job_name="TestAuthJob",
                time_budget_seconds=600.0,
                max_retries=3,
                backoff_delays=[0.01, 0.02, 0.03]
            )

    assert "Fatal security/credential error detected" in str(exc_info.value)
    assert mock_collector.call_count == 1
    assert mock_sleep.call_count == 0  # No retry delay slept


def test_time_budget_exceeded_aborts():
    """
    (c) Time budget exceeded:
    If elapsed time exceeds budget, execution must stop cleanly with TimeBudgetExceededError.
    """
    mock_collector = MagicMock()
    mock_collector.side_effect = RetryableCollectionError("Temporary glitch")

    with patch("time.time", side_effect=[100.0, 105.0, 710.0, 715.0]):
        with pytest.raises(TimeBudgetExceededError):
            execute_with_retry(
                collector_func=mock_collector,
                job_name="TestTimeoutJob",
                time_budget_seconds=600.0,
                max_retries=3,
                backoff_delays=[30.0, 60.0, 120.0]
            )


def test_paired_snapshots_resynchronizes_when_diff_exceeds_300s(tmp_path):
    """
    (d) Fresh snapshot pair rule:
    Initial collection: WMS mtime is 1000s, SAP mtime is 1400s (diff = 400s > 300s).
    Older file (WMS) is automatically re-collected.
    On re-collection: new WMS mtime is 1380s (diff = 20s <= 300s).
    Pair is successfully synchronized and can_execute_transfers is True.
    """
    import os
    wms_file_1 = tmp_path / "wms_1.xlsx"
    wms_file_2 = tmp_path / "wms_2.xlsx"
    sap_file_1 = tmp_path / "sap_1.XLS"

    wms_file_1.write_text("dummy")
    wms_file_2.write_text("dummy")
    sap_file_1.write_text("dummy")

    os.utime(wms_file_1, (1000.0, 1000.0))
    os.utime(sap_file_1, (1400.0, 1400.0))
    os.utime(wms_file_2, (1380.0, 1380.0))

    wms_calls = [
        (wms_file_1, True),
        (wms_file_2, True)
    ]
    sap_calls = [
        (sap_file_1, True)
    ]

    mock_wms = MagicMock(side_effect=wms_calls)
    mock_sap = MagicMock(side_effect=sap_calls)

    result = collect_paired_snapshots_resilient(
        collect_wms_func=mock_wms,
        collect_sap_func=mock_sap,
        max_pair_diff_seconds=300.0,
        time_budget_seconds=600.0,
        max_retries=2
    )

    assert result["can_execute_transfers"] is True
    assert result["is_wms_fresh"] is True
    assert result["is_sap_fresh"] is True
    assert result["time_diff_sec"] == 20.0
    assert result["WMS_STOCK"] == wms_file_2
    assert result["SAP_MB52"] == sap_file_1
    assert mock_wms.call_count == 2
    assert mock_sap.call_count == 1


def test_sap_session_lock_released_and_reacquired_by_same_owner(tmp_path):
    """
    (e) SAP lock retry hook:
    Verifies that:
    1. The lock is released during backoff.
    2. The lock is re-acquired under the EXACT SAME owner after backoff.
    3. Another owner trying to release the lock is refused (other owner's lock not touched).
    4. If re-acquisition fails, raises RetryableCollectionError.
    """
    lock_file = tmp_path / "test_sap.lock"
    custom_lock = SAPSessionLock(lock_file=lock_file)

    # 1. Acquire initial lock as 01_JOB
    owner = "01_JOB_GR_GI_Error_Batch1"
    acq = custom_lock.acquire(owner=owner, timeout=5.0)
    assert acq is True
    assert lock_file.exists()
    assert custom_lock._read_lock_info()["owner"] == owner

    # 2. Refuse release if requested by different owner
    custom_lock.release(owner="Foreign_Owner")
    assert lock_file.exists()  # Not released!
    assert custom_lock._read_lock_info()["owner"] == owner

    # 3. Create hook with custom lock instance
    hook = make_sap_lock_retry_hook(owner=owner, acquire_timeout=5.0, lock_instance=custom_lock)

    # 4. Trigger hook (simulate retry backoff)
    with patch("time.sleep"):
        hook(1, RetryableCollectionError("test"), 0.01)

    # 5. Verify lock was re-acquired under the EXACT SAME owner
    assert lock_file.exists()
    assert custom_lock._read_lock_info()["owner"] == owner

    # 6. Verify that if acquire fails, it raises RetryableCollectionError
    with patch.object(custom_lock, "acquire", return_value=False):
        with pytest.raises(RetryableCollectionError) as exc_info:
            with patch("time.sleep"):
                hook(2, RetryableCollectionError("test"), 0.01)
        assert "Failed to re-acquire SAP lock" in str(exc_info.value)


def test_driver_cleanup_targets_only_specified_pid():
    """
    (f) Targeted driver cleanup:
    Verifies that cleanup_driver_process kills ONLY the specified driver PID and its children,
    never foreign chromedrivers or personal Chrome.
    """
    with patch("psutil.Process") as mock_proc_cls:
        mock_parent = MagicMock()
        mock_child1 = MagicMock()
        mock_child2 = MagicMock()
        mock_parent.children.return_value = [mock_child1, mock_child2]
        mock_proc_cls.return_value = mock_parent

        cleanup_driver_process(12345)

        mock_proc_cls.assert_called_once_with(12345)
        mock_child1.kill.assert_called_once()
        mock_child2.kill.assert_called_once()
        mock_parent.kill.assert_called_once()


def test_selenium_session_lock_mutual_exclusion(tmp_path):
    """
    (g) Selenium session lock:
    Enforces strictly 1 browser across all jobs via mutex file lock.
    """
    lock_file = tmp_path / "test_selenium.lock"
    lock1 = SeleniumSessionLock(lock_file=lock_file)
    lock2 = SeleniumSessionLock(lock_file=lock_file)

    acq1 = lock1.acquire(owner="Crawler_A", timeout=1.0)
    assert acq1 is True

    # Lock2 cannot acquire while Lock1 holds it
    acq2 = lock2.acquire(owner="Crawler_B", timeout=1.0, poll_interval=0.2)
    assert acq2 is False

    # Foreign owner cannot release Crawler_A's lock
    lock2.release(owner="Crawler_B")
    assert lock_file.exists()
    assert lock1._read_lock_info()["owner"] == "Crawler_A"

    # Crawler_A releases its lock
    lock1.release(owner="Crawler_A")
    assert not lock_file.exists()


def test_classify_collection_error_windows_file_lock():
    """
    (h) Windows file lock (WinError 5 / PermissionError):
    Must be classified as RetryableCollectionError, NOT FatalCollectionError.
    """
    err = PermissionError("[WinError 5] Access is denied: 'C:\\\\some\\\\file.xlsx'")
    classified = classify_collection_error(err)
    assert isinstance(classified, RetryableCollectionError)
    assert not isinstance(classified, FatalCollectionError)
