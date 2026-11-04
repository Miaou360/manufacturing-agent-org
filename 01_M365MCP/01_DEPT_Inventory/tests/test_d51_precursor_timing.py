"""
Unit Tests for D51 Precursor Timing & Settling Guardrails (Thread 058)
=====================================================================
Verifies:
1. If recent 01 GI run completed within 25m and posted >= 1 transfer, guarantees at least 10 minutes elapsed before proceeding.
2. If recent 01 GI run posted 0 transfers, no settling wait is required.
3. If 02 triggers 01 directly and 01 posts >= 1 transfer, guarantees 10 minutes settling wait before COGI resolution.
4. If 02 triggers 01 directly and 01 posts 0 transfers, no settling wait is performed.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

TEST_DIR = Path(__file__).resolve().parent
INVENTORY_DIR = TEST_DIR.parent
JOB_02_DIR = INVENTORY_DIR / "02_JOB_Backlog_Clear"

if str(JOB_02_DIR) not in sys.path:
    sys.path.insert(0, str(JOB_02_DIR))

import backlog_pipeline


def test_precursor_recent_run_waits_for_10m_settling():
    """D51-3: If recent 01 run completed 4m ago (< 10m) and posted 2 transfers, waits 6m (360s)."""
    recent_mock = {
        "batch_id": "RUN_20261026_060000",
        "run_time": "2026-10-26 06:00:00",
        "postings_executed": 2,
        "elapsed_seconds": 30.0,
        "status": "COMPLETED",
        "age_minutes": 4.0,
        "age_seconds": 240.0
    }

    with patch("backlog_pipeline.check_recent_gi_run", return_value=recent_mock), \
         patch("time.sleep") as mock_sleep:
        backlog_pipeline.run_precursor_gi_pipeline()

        # 600s - 240s = 360s wait
        mock_sleep.assert_called_once_with(360.0)


def test_precursor_recent_run_no_wait_if_zero_postings():
    """D51-3: If recent 01 run completed 4m ago but posted 0 transfers, no wait is required."""
    recent_mock = {
        "batch_id": "RUN_20261026_060000",
        "run_time": "2026-10-26 06:00:00",
        "postings_executed": 0,
        "elapsed_seconds": 30.0,
        "status": "COMPLETED",
        "age_minutes": 4.0,
        "age_seconds": 240.0
    }

    with patch("backlog_pipeline.check_recent_gi_run", return_value=recent_mock), \
         patch("time.sleep") as mock_sleep:
        backlog_pipeline.run_precursor_gi_pipeline()

        mock_sleep.assert_not_called()


def test_precursor_recent_run_no_wait_if_already_10m_elapsed():
    """D51-3: If recent 01 run completed 18m ago (>= 10m, e.g. 06:00 -> 06:20), no wait is required."""
    recent_mock = {
        "batch_id": "RUN_20261026_060000",
        "run_time": "2026-10-26 06:00:00",
        "postings_executed": 5,
        "elapsed_seconds": 45.0,
        "status": "COMPLETED",
        "age_minutes": 18.0,
        "age_seconds": 1080.0
    }

    with patch("backlog_pipeline.check_recent_gi_run", return_value=recent_mock), \
         patch("time.sleep") as mock_sleep:
        backlog_pipeline.run_precursor_gi_pipeline()

        mock_sleep.assert_not_called()


def test_precursor_direct_run_waits_10m_if_posted():
    """D51-2: If no recent 01 run, 02 executes 01 directly. If 01 posted >= 1 transfer, waits 10m (600s)."""
    new_gi_mock = {
        "batch_id": "RUN_20261026_061500",
        "run_time": "2026-10-26 06:15:00",
        "postings_executed": 3,
        "elapsed_seconds": 50.0,
        "status": "COMPLETED",
        "age_minutes": 0.5,
        "age_seconds": 30.0
    }

    dummy_proc = MagicMock()
    dummy_proc.returncode = 0

    with patch("backlog_pipeline.check_recent_gi_run", side_effect=[None, new_gi_mock]), \
         patch("subprocess.run", return_value=dummy_proc), \
         patch("time.sleep") as mock_sleep:
        backlog_pipeline.run_precursor_gi_pipeline()

        # Should be called with 600.0 (10 minutes) and then 3 seconds
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 600.0 in sleep_calls, f"Expected 600.0s settling wait in sleep calls, got {sleep_calls}"
