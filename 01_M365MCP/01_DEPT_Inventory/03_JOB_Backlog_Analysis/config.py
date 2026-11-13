"""
Configuration for 03_Backlog_Analysis
=====================================
Target paths, milestone schedules, quadrant thresholds, and notification settings.
"""

import os
from pathlib import Path
from datetime import datetime


CURRENT_DIR = Path(__file__).resolve().parent
INVENTORY_TEAM_DIR = CURRENT_DIR.parent
PROJECT_ROOT = INVENTORY_TEAM_DIR.parent

# Database & Ontology Paths
DB_BACKLOG_PATH = INVENTORY_TEAM_DIR / "02_JOB_Backlog_Clear" / "db" / "backlog_history.db"
ONTOLOGY_PATH = INVENTORY_TEAM_DIR / "00_JOB_StockAccuracy" / "ontology" / "material_turnover_ontology.json"

# Output Report Directory
REPORTS_DIR = CURRENT_DIR / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Milestone Settings
# Data accumulates daily from 2026-10-23 to 2026-10-31.
# First formal analysis run date is 2026-10-31.
MILESTONE_ANALYSIS_DATE = "2026-10-31"
TARGET_START_DATE = "2026-10-23"

try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

def is_valid_recipient(addr: str) -> bool:
    """Returns True if addr is a non-empty, non-placeholder email address."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip().lower()
    return bool(clean) and "@" in clean and "example.com" not in clean

_analysis_to = os.getenv("BACKLOG_ANALYSIS_TO")
# Recipient Settings (Configurable via env)
RECIPIENTS = {
    "to": [x.strip() for x in _analysis_to.split(",") if is_valid_recipient(x)] if _analysis_to is not None else [],
    "cc": []
}

# 4-Quadrant Classification Thresholds
# 1. High Frequency: Appears on >= 40% of recorded days in the period
HIGH_FREQ_PCT_THRESHOLD = 40.0

# 2. High Volume Percentile: Top 25% (75th percentile) of total accumulated deficit quantity
HIGH_VOL_PERCENTILE = 75.0
