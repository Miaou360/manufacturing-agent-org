"""
Universal SAP GUI Mutex Lock & Inter-Process Coordinator
========================================================
00_HQ_TaskManager / sap_lock.py

Purpose:
- Coordinates exclusive access to the shared interactive SAP GUI session across:
  1. 01_JOB_GR_GI_Error (24/7 daemon & hourly pipeline)
  2. 02_JOB_Backlog_Clear (COGI 06:10 & 08:15 pipelines)
  3. Ad-hoc manual scripts & background tasks
- Prevents SAP GUI screen hijacking, OK-Code collisions, and modal dialog race conditions.
- Stale lock protection: Automatically detects deceased PIDs or timeouts to prevent deadlocks.
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
LOCK_FILE = BASE_DIR / "sap_gui_session.lock"

logger = logging.getLogger("SAPLock")


class SAPSessionLock:
    """File-backed cross-process mutex lock for interactive SAP GUI operations."""

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

    def acquire(self, owner: str = "Unknown", timeout: float = 180.0, poll_interval: float = 2.0) -> bool:
        """
        Acquires exclusive lock for SAP GUI access.
        Blocks and waits until lock is available or timeout expires.
        """
        start_time = time.time()
        current_pid = os.getpid()

        while time.time() - start_time < timeout:
            if not self.lock_file.exists():
                try:
                    lock_data = {
                        "pid": current_pid,
                        "owner": owner,
                        "timestamp": time.time(),
                        "acquired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    # Atomic creation flag (x mode)
                    with open(self.lock_file, "x", encoding="utf-8") as f:
                        json.dump(lock_data, f)
                    logger.info(f"🔒 [SAP LOCK] Acquired exclusive SAP lock for '{owner}' (PID: {current_pid})")
                    return True
                except FileExistsError:
                    pass

            # If lock file exists, inspect if it's stale or held by self
            lock_info = self._read_lock_info()
            if lock_info.get("pid") == current_pid:
                return True

            if self._is_lock_stale(lock_info):
                logger.warning(f"⚠️ [SAP LOCK] Clearing stale lock from PID {lock_info.get('pid')} ('{lock_info.get('owner')}')")
                try:
                    self.lock_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue

            elapsed = int(time.time() - start_time)
            owner_info = f"PID {lock_info.get('pid')} ({lock_info.get('owner')})"
            if elapsed % 10 == 0 and elapsed > 0:
                logger.info(f"⏳ [SAP LOCK WAITING] Waiting for {owner_info} to release SAP... ({elapsed}/{int(timeout)}s)")

            time.sleep(poll_interval)

        logger.error(f"❌ [SAP LOCK TIMEOUT] Failed to acquire SAP lock within {timeout}s (Currently held by {self._read_lock_info()})")
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
                logger.info(f"🔓 [SAP LOCK] Released SAP lock by '{owner}' (PID: {current_pid})")
            except Exception as e:
                logger.warning(f"⚠️ [SAP LOCK] Could not remove lock file: {e}")
        else:
            logger.warning(f"⚠️ [SAP LOCK] Release refused: lock held by PID {lock_info.get('pid')} ('{recorded_owner}'), cannot release as '{owner}'")


_global_lock = SAPSessionLock()


@contextmanager
def sap_session_lock(owner: str = "Process", timeout: float = 180.0):
    """Context manager for safe, mutual-exclusive SAP GUI execution."""
    acquired = _global_lock.acquire(owner=owner, timeout=timeout)
    try:
        yield acquired
    finally:
        if acquired:
            _global_lock.release(owner=owner)


if __name__ == "__main__":
    print("Testing SAP Session Lock...")
    with sap_session_lock(owner="TestScript", timeout=5) as acq:
        print(f"Acquired: {acq}")
        time.sleep(1)
    print("Test lock released cleanly.")
