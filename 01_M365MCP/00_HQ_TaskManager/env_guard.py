"""
Operational Environment Guard (00_HQ_TaskManager / env_guard.py)
================================================================
Fail-fast validation for critical operational environment variables.
Prevents silent fallback to dummy placeholders (e.g., P001, 0000000, Enterprise SSO)
during live scheduled or daemon runs, while permitting dummy defaults under pytest
or explicit M365_TEST_MODE=1.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple
from dotenv import load_dotenv

logger = logging.getLogger("EnvGuard")

DUMMY_PLACEHOLDERS = {
    "P001",
    "P002",
    "0000000",
    "9900000",
    "Enterprise SSO",
    "https://wms.example.com/WMS_WEB",
    "https://wms.example.com/WMS_WEB/",
    "wms_user",
    "OEM",
    "AUTO_AGENT",
    "ACME",
    "ACME Manufacturing",
    "ACME Corp",
    "ACME America",
    "MAT-0000",
    "TEST-000",
}

REQUIRED_OPS_KEYS: Tuple[ Tuple[str, str], ... ] = (
    ("SAP_PLANT", "P001"),
    ("SAP_PLANT_SUB", "P002"),
    ("SAP_USERNAME", "0000000"),
    ("SAP_SSO_WINDOW_TITLE", "Enterprise SSO"),
    ("WMS_BASE_URL", "https://wms.example.com/WMS_WEB"),
    ("COMPANY_NAME", "ACME Manufacturing"),
    ("OEM_FLOOR_BIN_PREFIX", "OEM"),
    ("SAP_MB1B_RECIPIENT", "AUTO_AGENT"),
)

OUTPUT_DUMMY_REGEX = re.compile(
    r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)",
    re.IGNORECASE,
)


def load_workspace_dotenv(start_path: Optional[Path] = None) -> Optional[Path]:
    """Locates and loads 01_M365MCP/.env from start_path, cwd, or module hierarchy."""
    candidates = []
    if start_path:
        p = Path(start_path).resolve()
        candidates.extend([p] + list(p.parents))
    cwd = Path.cwd().resolve()
    candidates.extend([cwd] + list(cwd.parents))
    here = Path(__file__).resolve()
    candidates.extend(list(here.parents))

    seen = set()
    for d in candidates:
        if d in seen:
            continue
        seen.add(d)
        env_f = d / ".env"
        if env_f.is_file():
            load_dotenv(env_f, override=False)
            return env_f
        m365_env = d / "01_M365MCP" / ".env"
        if m365_env.is_file():
            load_dotenv(m365_env, override=False)
            return m365_env
    return None


def is_test_mode() -> bool:
    """Returns True only when running inside pytest or when M365_TEST_MODE=1 is explicitly set."""
    return (
        "pytest" in sys.modules
        or bool(os.getenv("PYTEST_CURRENT_TEST"))
        or os.getenv("M365_TEST_MODE", "").strip() == "1"
    )


def get_ops_env(key: str, test_default: str = "") -> str:
    """
    Retrieves an operational environment variable.
    In production (non-test mode), raises RuntimeError immediately if the variable is
    unset, empty, or equal to a sanitized dummy placeholder.
    """
    load_workspace_dotenv()
    val = os.getenv(key, "").strip()
    if is_test_mode():
        return val if val else test_default

    if not val or val in DUMMY_PLACEHOLDERS:
        msg = (
            f"[ENV GUARD CRITICAL] Required operational environment variable '{key}' is "
            f"{'missing/empty' if not val else 'set to a dummy placeholder'}! "
            f"Refusing to run with fallback default in production mode."
        )
        logger.error(msg)
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)
    return val


def verify_ops_env(job_name: str = "PIPELINE") -> Dict[str, str]:
    """
    Verifies all required operational environment variables at job startup and logs
    configured status without exposing sensitive values.
    """
    env_path = load_workspace_dotenv()
    status_map: Dict[str, str] = {}
    missing_or_dummy = []

    test_mode = is_test_mode()
    for key, test_def in REQUIRED_OPS_KEYS:
        val = os.getenv(key, "").strip()
        if not val or val in DUMMY_PLACEHOLDERS:
            if test_mode:
                status_map[key] = "test_default"
            else:
                status_map[key] = "MISSING_OR_DUMMY"
                missing_or_dummy.append(key)
        else:
            status_map[key] = "configured"

    summary_str = ", ".join(f"{k}={v}" for k, v in status_map.items())
    env_loc = "found" if env_path else "not_found"

    if missing_or_dummy:
        msg = (
            f"[ENV GUARD CRITICAL] [{job_name}] .env={env_loc} | Failed required env check: "
            f"{summary_str}. Missing/dummy keys: {missing_or_dummy}"
        )
        logger.error(msg)
        print(msg, flush=True)
        raise RuntimeError(msg)

    msg = f"[ENV GUARD] [{job_name}] .env={env_loc} | {summary_str}"
    logger.info(msg)
    print(msg, flush=True)
    return status_map


def assert_no_dummy_in_output(text: str, context: str = "operational_output") -> None:
    """
    Reverse-direction operational output guard (#176):
    Fails fast before email/report dispatch if any public-repo dummy/pseudonym token
    (ACME, P001, P002, example.com, MAT-0000, TEST-000, AUTO_AGENT, 9900000*)
    leaks into an operational subject, HTML body, or header when not in test mode.
    """
    if is_test_mode() or not text:
        return
    m = OUTPUT_DUMMY_REGEX.search(text)
    if m:
        msg = (
            f"[OUTPUT GUARD CRITICAL] Dummy/pseudonym token '{m.group(0)}' detected in "
            f"{context}! Aborting operational dispatch."
        )
        logger.error(msg)
        print(msg, file=sys.stderr, flush=True)
        raise RuntimeError(msg)
