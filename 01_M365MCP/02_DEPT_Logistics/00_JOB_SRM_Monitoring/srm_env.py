"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - .env access (same convention as the 01_DEPT jobs)
=============================================================================================
* load_env()     : nearest parent .env, never overriding variables already set (skipped under pytest unless a
                   start folder is given)
* company_name() : our company name on supplier files and mails = .env COMPANY_NAME. Never written in code (the
                   pre-commit hook blocks .env values); the dummy 'ACME Plant' is used only under tests.
* assert_clean() : live supplier mails must not carry dummy / test tokens.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
_DUMMY_NAMES = {"", "ACME", "ACME Plant", "ACME Corp", "ACME America"}
_DUMMY_OUT = re.compile(r"(\bACME\b|example\.com|\b9900\d{4}\b)", re.IGNORECASE)


def testing() -> bool:
    return "pytest" in sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST"))


def load_env(start: Optional[Path] = None) -> None:
    if start is None and testing():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    here = Path(start) if start else BASE_DIR
    for p in [here, *here.parents]:
        if (p / ".env").is_file():
            load_dotenv(p / ".env", override=False)
            return


def company_name() -> str:
    v = os.getenv("COMPANY_NAME", "").strip()
    if testing():
        return v or "ACME Plant"
    if v in _DUMMY_NAMES:
        raise RuntimeError("COMPANY_NAME is missing or a dummy value in .env (needed for supplier files / mails)")
    return v


def assert_clean(*texts: str) -> None:
    if testing():
        return
    for t in texts:
        m = _DUMMY_OUT.search(t or "")
        if m:
            raise RuntimeError(f"dummy token '{m.group(0)}' in an outgoing supplier mail - not sent")
