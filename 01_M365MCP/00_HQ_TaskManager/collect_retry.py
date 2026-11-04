"""
Collection Retry & Autonomous Recovery Engine (00_HQ_TaskManager / collect_retry.py)
=====================================================================================
Plant: P001 Headlamp
Standard: User Decision D41 (Bounded Resilient Collection & Environmental Recovery)

Operational Requirements:
1. Bounded Retries: Max 3 retries (total 4 attempts) with exponential backoff [30s, 60s, 120s].
2. Strict Time Budget: 600s (10 min) for 01/02, 900s (15 min) for 00/05. Aborts cleanly if budget exceeded.
3. Strict Error Classification:
   - Retryable: SSO window not found, timeouts, 0-byte/missing file, SAP GUI frozen, WMS page load, network drop.
   - Fatal (Immediate Abort, 0 Retries): Invalid login credentials, password error, account locked, unauthorized.
     Secrets and passwords must NEVER be printed or exposed in logs.
4. Paired Snapshot Freshness Guard (D21/D30):
   - WMS and MB52 snapshots must be within 300s of each other.
   - If one side retries and time difference exceeds 300s, the earlier snapshot is re-collected.
5. Environmental Recovery between Retries:
   - Release SAP GUI cross-process mutex lock and re-acquire.
   - Clean up SAP GUI / SSO blocking error popups (#32770).
   - Terminate orphaned Chrome/chromedriver processes to ensure strictly 1 browser instance (AGENTS.md Rule 3).
"""

import os
import sys
import time
import logging
from pathlib import Path
from typing import Callable, Optional, Dict, Any, List, Tuple

logger = logging.getLogger("CollectRetry")

# Default retry timing parameters
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_DELAYS = [30.0, 60.0, 120.0]
DEFAULT_TIME_BUDGET_01_02 = 600.0  # 10 minutes
DEFAULT_TIME_BUDGET_00_05 = 900.0  # 15 minutes
MAX_SNAPSHOT_PAIR_DIFF_SECONDS = 300.0  # 5 minutes (D21/D30)

FATAL_KEYWORDS = [
    "invalid password",
    "password incorrect",
    "bad password",
    "user locked",
    "account locked",
    "logon failed",
    "login failed",
    "authentication failed",
    "unauthorized",
    "access denied",
    "permission denied",
    "로그온 실패",
    "비밀번호 오류",
    "계정이 잠겼습니다",
    "비밀번호가 올바르지 않습니다",
    "사용자 권한이 없습니다",
]


class CollectionError(Exception):
    """Base exception for data collection failures."""
    pass


class RetryableCollectionError(CollectionError):
    """Temporary or transient error suitable for automatic retry."""
    pass


class FatalCollectionError(CollectionError):
    """Non-recoverable security or credential error. Retrying is strictly prohibited."""
    pass


class TimeBudgetExceededError(CollectionError):
    """Execution stopped because the allocated job run time budget was exceeded."""
    pass


def sanitize_message(msg: str) -> str:
    """Removes sensitive credentials, passwords, or tokens from error strings."""
    if not msg:
        return ""
    text = str(msg)
    # Mask obvious password patterns
    import re
    text = re.sub(r'(password|pwd|pass|secret)\s*[:=]\s*[^\s,;]+', r'\1=***', text, flags=re.IGNORECASE)
    return text


def classify_collection_error(exc: Exception, raw_output: str = "") -> CollectionError:
    """
    Classifies an exception or process output into FatalCollectionError or RetryableCollectionError.
    Never exposes secrets in the resulting error message.
    """
    combined = f"{str(exc)} {raw_output}".lower()

    # Windows file lock / permission error (WinError 5 / PermissionError) is a transient retryable error
    if "winerror 5" in combined or "permissionerror" in combined or "file in use" in combined:
        return RetryableCollectionError(sanitize_message(str(exc)))

    for kw in FATAL_KEYWORDS:
        if kw in combined:
            clean_msg = sanitize_message(str(exc))
            return FatalCollectionError(f"Fatal security/credential error detected ({kw}): {clean_msg}")
    return RetryableCollectionError(sanitize_message(str(exc)))


def cleanup_driver_process(driver_pid: Optional[int]):
    """
    Safely terminates ONLY the specific chromedriver process and its children created by this job.
    NEVER terminates other jobs' browsers or user's personal Chrome.
    """
    if not driver_pid:
        return
    try:
        import psutil
        parent = psutil.Process(driver_pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.kill()
        logger.info(f"🧹 [DRIVER CLEANUP] Cleaned up specific driver PID {driver_pid} and its children")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass


def recover_collection_environment(job_name: str = "Collector"):
    """
    Performs standard ambient self-healing between retry attempts:
    - Dismisses blocking SAP error popups (#32770) from SAP GUI/wscript without touching locks.
    """
    logger.info(f"🔄 [RECOVERY] Initiating ambient self-healing before retry for '{job_name}'...")
    try:
        from sap_launcher import dismiss_sap_error_popups
        dismiss_sap_error_popups()
    except Exception:
        pass


def make_sap_lock_retry_hook(
    owner: str,
    acquire_timeout: float = 60.0,
    lock_instance: Optional[Any] = None
) -> Callable[[int, Exception, float], None]:
    """
    Creates an on_before_retry hook that safely releases the caller's SAP session lock,
    waits the backoff interval, and re-acquires the lock under the EXACT same owner.
    If re-acquisition fails, raises RetryableCollectionError to cleanly abort the collection.
    """
    def _hook(attempt: int, exc: Exception, backoff: float):
        if lock_instance is not None:
            active_lock = lock_instance
        else:
            from sap_lock import _global_lock
            active_lock = _global_lock

        logger.info(f"🔓 [SAP LOCK RETRY] Temporarily releasing SAP lock held by '{owner}' during {backoff:.0f}s backoff...")
        active_lock.release(owner=owner)
        try:
            recover_collection_environment(job_name=owner)
            time.sleep(backoff)
        finally:
            logger.info(f"🔒 [SAP LOCK RETRY] Re-acquiring SAP lock for '{owner}' (timeout: {acquire_timeout:.0f}s)...")
            acq = active_lock.acquire(owner=owner, timeout=acquire_timeout)
            if not acq:
                raise RetryableCollectionError(f"Failed to re-acquire SAP lock for '{owner}' after retry backoff")
    return _hook


def execute_with_retry(
    collector_func: Callable[[], Any],
    job_name: str,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_01_02,
    max_retries: int = DEFAULT_MAX_RETRIES,
    backoff_delays: Optional[List[float]] = None,
    on_before_retry: Optional[Callable[[int, Exception, float], None]] = None
) -> Tuple[Any, Dict[str, Any]]:
    """
    Executes a collection function with bounded retries, time budget enforcement,
    fatal error immediate abort, and caller-coordinated recovery.

    Returns:
        (result, metrics_dict)
    """
    delays = backoff_delays if backoff_delays is not None else DEFAULT_BACKOFF_DELAYS
    start_time = time.time()
    attempt = 0
    retries_count = 0
    error_history = []

    while True:
        attempt += 1
        elapsed = time.time() - start_time

        # Check time budget before starting attempt
        if elapsed >= time_budget_seconds:
            err_msg = f"Time budget ({time_budget_seconds:.0f}s) exceeded for {job_name} after {elapsed:.1f}s (Attempt {attempt})"
            logger.error(f"⏱️ [TIME BUDGET EXCEEDED] {err_msg}")
            metrics = {
                "job_name": job_name,
                "success": False,
                "attempts": attempt,
                "retries": retries_count,
                "elapsed_seconds": round(elapsed, 2),
                "error": err_msg,
                "error_history": error_history
            }
            raise TimeBudgetExceededError(err_msg)

        try:
            logger.info(f"🚀 [{job_name}] Attempt {attempt}/{max_retries + 1} (Elapsed: {elapsed:.1f}s / Budget: {time_budget_seconds:.0f}s)...")
            res = collector_func()
            total_elapsed = time.time() - start_time
            metrics = {
                "job_name": job_name,
                "success": True,
                "attempts": attempt,
                "retries": retries_count,
                "elapsed_seconds": round(total_elapsed, 2),
                "error": None,
                "error_history": error_history
            }
            logger.info(f"✅ [{job_name}] Succeeded on attempt {attempt} (Total time: {total_elapsed:.1f}s, Retries: {retries_count})")
            return res, metrics

        except FatalCollectionError as e:
            total_elapsed = time.time() - start_time
            logger.error(f"🛑 [{job_name}] FATAL ERROR (Immediate Abort, 0 Retries): {e}")
            metrics = {
                "job_name": job_name,
                "success": False,
                "attempts": attempt,
                "retries": retries_count,
                "elapsed_seconds": round(total_elapsed, 2),
                "error": str(e),
                "error_history": error_history + [str(e)]
            }
            raise e

        except Exception as e:
            classified = classify_collection_error(e)
            if isinstance(classified, FatalCollectionError):
                total_elapsed = time.time() - start_time
                logger.error(f"🛑 [{job_name}] FATAL CREDENTIAL/SECURITY ERROR: {classified}")
                metrics = {
                    "job_name": job_name,
                    "success": False,
                    "attempts": attempt,
                    "retries": retries_count,
                    "elapsed_seconds": round(total_elapsed, 2),
                    "error": str(classified),
                    "error_history": error_history + [str(classified)]
                }
                raise classified

            error_history.append(str(classified))

            if retries_count >= max_retries:
                total_elapsed = time.time() - start_time
                logger.error(f"❌ [{job_name}] Max retries ({max_retries}) exhausted: {classified}")
                metrics = {
                    "job_name": job_name,
                    "success": False,
                    "attempts": attempt,
                    "retries": retries_count,
                    "elapsed_seconds": round(total_elapsed, 2),
                    "error": str(classified),
                    "error_history": error_history
                }
                raise classified

            # Select backoff delay
            delay_idx = min(retries_count, len(delays) - 1)
            backoff = delays[delay_idx]

            # Check if sleeping backoff exceeds time budget
            if (time.time() - start_time) + backoff >= time_budget_seconds:
                total_elapsed = time.time() - start_time
                err_msg = f"Backoff delay ({backoff}s) would exceed time budget ({time_budget_seconds:.0f}s)"
                logger.error(f"⏱️ [TIME BUDGET EXCEEDED] {err_msg}")
                metrics = {
                    "job_name": job_name,
                    "success": False,
                    "attempts": attempt,
                    "retries": retries_count,
                    "elapsed_seconds": round(total_elapsed, 2),
                    "error": err_msg,
                    "error_history": error_history
                }
                raise TimeBudgetExceededError(err_msg)

            retries_count += 1
            logger.warning(f"⚠️ [{job_name}] Attempt {attempt} failed ({classified}). Recovering environment and retrying in {backoff}s...")

            # Run recovery actions
            if on_before_retry:
                try:
                    on_before_retry(retries_count, classified, backoff)
                except Exception as hook_err:
                    logger.warning(f"on_before_retry hook error: {hook_err}")
                    raise hook_err
            else:
                recover_collection_environment(job_name=job_name)
                time.sleep(backoff)


def collect_paired_snapshots_resilient(
    collect_wms_func: Callable[[], Tuple[Optional[Path], bool]],
    collect_sap_func: Callable[[], Tuple[Optional[Path], bool]],
    max_pair_diff_seconds: float = MAX_SNAPSHOT_PAIR_DIFF_SECONDS,
    time_budget_seconds: float = DEFAULT_TIME_BUDGET_01_02,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sap_lock_owner: Optional[str] = None
) -> Dict[str, Any]:
    """
    Collects simultaneous WMS & MB52 snapshots with automatic re-synchronization.
    Guarantees:
    - If one side fails, it is retried.
    - If one side took too long such that abs(mtime_wms - mtime_mb52) > 300s,
      the older snapshot is re-collected so that both snapshots form a synchronized pair.
    - If sap_lock_owner is specified, SAP retries will safely release and re-acquire the lock.
    - Returns structured snapshot dictionary.
    """
    start_time = time.time()
    wms_file, wms_fresh = None, False
    sap_file, sap_fresh = None, False
    wms_needs_resync = True
    sap_needs_resync = True
    re_sync_attempts = 0

    while re_sync_attempts <= max_retries:
        re_sync_attempts += 1
        elapsed = time.time() - start_time
        remaining_budget = max(10.0, time_budget_seconds - elapsed)

        # 1. Collect WMS if not fresh or if needing resync
        if wms_needs_resync or not wms_fresh or wms_file is None:
            wms_res, _ = execute_with_retry(
                collector_func=collect_wms_func,
                job_name="WMS_Collection",
                time_budget_seconds=remaining_budget,
                max_retries=1  # internal step retries
            )
            wms_file, wms_fresh = wms_res
            wms_needs_resync = False

        # 2. Collect SAP if not fresh or if needing resync
        elapsed = time.time() - start_time
        remaining_budget = max(10.0, time_budget_seconds - elapsed)
        if sap_needs_resync or not sap_fresh or sap_file is None:
            sap_hook = make_sap_lock_retry_hook(sap_lock_owner) if sap_lock_owner else None
            sap_res, _ = execute_with_retry(
                collector_func=collect_sap_func,
                job_name="SAP_MB52_Collection",
                time_budget_seconds=remaining_budget,
                max_retries=1,
                on_before_retry=sap_hook
            )
            sap_file, sap_fresh = sap_res
            sap_needs_resync = False

        # 3. Check time difference between freshly collected files
        if wms_file and sap_file and wms_file.exists() and sap_file.exists() and wms_fresh and sap_fresh:
            wms_mtime = wms_file.stat().st_mtime
            sap_mtime = sap_file.stat().st_mtime
            diff_sec = abs(wms_mtime - sap_mtime)

            if diff_sec <= max_pair_diff_seconds:
                logger.info(f"🎯 [PAIR SYNCHRONIZED] WMS & MB52 time diff = {diff_sec:.1f}s (<= {max_pair_diff_seconds}s)")
                return {
                    "WMS_STOCK": wms_file,
                    "SAP_MB52": sap_file,
                    "is_wms_fresh": True,
                    "is_sap_fresh": True,
                    "time_diff_sec": round(diff_sec, 1),
                    "can_execute_transfers": True,
                    "re_sync_attempts": re_sync_attempts
                }
            else:
                logger.warning(f"⚠️ [PAIR DESYNCHRONIZED] Time diff {diff_sec:.1f}s > {max_pair_diff_seconds}s. Resynchronizing older file...")
                # The older file must be re-collected
                if wms_mtime < sap_mtime:
                    wms_needs_resync = True
                else:
                    sap_needs_resync = True
        else:
            # One or both failed to collect fresh
            break

    # If pair cannot be synchronized within threshold
    time_diff = None
    if wms_file and sap_file and wms_file.exists() and sap_file.exists():
        time_diff = abs(wms_file.stat().st_mtime - sap_file.stat().st_mtime)

    logger.warning(f"⚠️ [PAIR FALLBACK] Unable to achieve simultaneous pair <= {max_pair_diff_seconds}s. Transfers strictly blocked.")
    return {
        "WMS_STOCK": wms_file,
        "SAP_MB52": sap_file,
        "is_wms_fresh": wms_fresh,
        "is_sap_fresh": sap_fresh,
        "time_diff_sec": round(time_diff, 1) if time_diff is not None else None,
        "can_execute_transfers": False,
        "re_sync_attempts": re_sync_attempts
    }
