"""
Universal Selenium Browser Mutex Lock & Inter-Process Coordinator
==================================================================
00_HQ_TaskManager / selenium_lock.py
Plant: P001 Headlamp
Standard: AGENTS.md Rule 3 (Strictly 1 Browser Instance at all times)

Purpose:
- Coordinates exclusive access to Selenium Chrome browser sessions across:
  1. 00_JOB_StockAccuracy (WMS Stock List & Missing GI crawlers)
  2. 01_JOB_GR_GI_Error (WMS real-time stock snapshot crawler)
  3. 05_JOB_SL01_R_Bins (04:15 AM decoupled WMS stock crawler)
  4. Ad-hoc manual crawlers & background tasks
- Prevents concurrent browser execution, port collisions, memory spikes,
  and WMS server IP rate-limiting / session termination.
- Stale lock protection: Automatically detects dead PIDs or timeouts to prevent deadlocks.
"""

import os
import sys
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager
import psutil

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
LOCK_FILE = BASE_DIR / "selenium_browser_session.lock"

logger = logging.getLogger("SeleniumLock")


class SeleniumSessionLock:
    """File-backed cross-process mutex lock for Selenium Chrome browser operations."""

    def __init__(self, lock_file: Path = LOCK_FILE):
        self.lock_file = lock_file

    def _read_lock_info(self) -> dict:
        if not self.lock_file.exists():
            return {}
        try:
            return json.loads(self.lock_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _is_lock_stale(self, lock_info: dict, max_age_seconds: float = 300.0) -> bool:
        """Determines if existing lock belongs to a dead process or timed out."""
        if not lock_info:
            return True
        pid = lock_info.get("pid")
        if not pid or not psutil.pid_exists(pid):
            return True

        # Verify if process is actually still running
        try:
            p = psutil.Process(pid)
            if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
                return True
        except Exception:
            return True

        # Check maximum allowable lock age (default 5 minutes)
        lock_ts = lock_info.get("timestamp", 0)
        if time.time() - lock_ts > max_age_seconds:
            return True

        return False

    def acquire(self, owner: str = "Unknown", timeout: float = 120.0, poll_interval: float = 2.0) -> bool:
        """
        Acquires exclusive lock for Selenium browser access.
        Blocks and waits until lock is available or timeout expires.
        """
        start_time = time.time()
        current_pid = os.getpid()

        while time.time() - start_time < timeout:
            if not self.lock_file.exists():
                try:
                    lock_data = {
                        "owner": owner,
                        "pid": current_pid,
                        "timestamp": time.time(),
                        "acquired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    self.lock_file.write_text(json.dumps(lock_data, indent=2), encoding="utf-8")
                    logger.info(f"🔒 [SELENIUM LOCK] Acquired Selenium lock for '{owner}' (PID: {current_pid})")
                    return True
                except Exception as e:
                    logger.warning(f"⚠️ [SELENIUM LOCK COLLISION] Race condition while writing lock: {e}")

            # Lock file exists - check if stale
            lock_info = self._read_lock_info()
            if self._is_lock_stale(lock_info):
                stale_owner = lock_info.get("owner", "Unknown")
                stale_pid = lock_info.get("pid", "None")
                logger.warning(f"⚠️ [SELENIUM STALE LOCK] Reclaiming stale Selenium lock from '{stale_owner}' (PID: {stale_pid})")
                try:
                    self.lock_file.unlink(missing_ok=True)
                    continue
                except Exception:
                    pass

            elapsed = int(time.time() - start_time)
            owner_info = f"'{lock_info.get('owner', 'Unknown')}' (PID: {lock_info.get('pid', 'Unknown')})"
            if elapsed > 0 and elapsed % 10 == 0:
                logger.info(f"⏳ [SELENIUM LOCK WAITING] Waiting for {owner_info} to finish crawl... ({elapsed}/{int(timeout)}s)")

            time.sleep(poll_interval)

        logger.error(f"❌ [SELENIUM LOCK TIMEOUT] Failed to acquire Selenium lock within {timeout}s (Held by {self._read_lock_info()})")
        return False

    def release(self, owner: str = "Unknown"):
        """Releases the lock if held by current process and matching owner, or stale."""
        current_pid = os.getpid()
        lock_info = self._read_lock_info()

        if not self.lock_file.exists():
            return

        is_stale = self._is_lock_stale(lock_info)
        is_same_pid = (lock_info.get("pid") == current_pid)
        recorded_owner = lock_info.get("owner")
        is_owner_match = (owner in ("Unknown", recorded_owner) or not recorded_owner)

        if (is_same_pid and is_owner_match) or is_stale:
            try:
                self.lock_file.unlink(missing_ok=True)
                logger.info(f"🔓 [SELENIUM LOCK] Released Selenium lock by '{owner}' (PID: {current_pid})")
            except Exception as e:
                logger.warning(f"⚠️ [SELENIUM LOCK] Could not remove lock file: {e}")
        else:
            logger.warning(f"⚠️ [SELENIUM LOCK] Release refused: lock held by PID {lock_info.get('pid')} ('{recorded_owner}'), cannot release as '{owner}'")


_global_selenium_lock = SeleniumSessionLock()


@contextmanager
def selenium_session_lock(owner: str = "Process", timeout: float = 120.0):
    """Context manager for safe, mutual-exclusive Selenium browser execution."""
    acquired = _global_selenium_lock.acquire(owner=owner, timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            _global_selenium_lock.release(owner=owner)
