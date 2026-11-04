"""
Configuration for StockAccuracy Team
창고 수신자 목록, 플랜트, 저장위치 및 정기 알림 주기 설정
"""

import os
from pathlib import Path
import os
import sys as _sys
from dotenv import load_dotenv as _load_dotenv
_ENV_LOADED_PATH = None
for _ep in Path(__file__).resolve().parents:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        _ENV_LOADED_PATH = _ep / ".env"
        break
_DUMMY_ENV_VALS = {"P001", "P002", "0000000", "9900000", "Enterprise SSO", "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT", "ACME", "ACME Manufacturing", "ACME Corp", "ACME America", "MAT-0000", "TEST-000"}
def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val
import re as _re_out_guard
_OUT_DUMMY_RE = _re_out_guard.compile(r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)", _re_out_guard.IGNORECASE)
def _assert_clean_ops_output(*texts: str, context: str = "email_output") -> None:
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return
    for _t in texts:
        if _t:
            _m = _OUT_DUMMY_RE.search(str(_t))
            if _m:
                raise RuntimeError(f"[OUTPUT GUARD CRITICAL] Dummy token '{_m.group(0)}' detected in {context} ({__file__})!")
def _verify_ops_env_log(job_name: str = "PIPELINE") -> str:
    _req = ("SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME", "SAP_SSO_WINDOW_TITLE", "WMS_BASE_URL", "COMPANY_NAME", "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT")
    _st = []
    for _k in _req:
        if _k == "OEM_FLOOR_BIN_PREFIX":
            _fb = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "").strip()
            if _fb and _fb not in _DUMMY_ENV_VALS:
                _st.append("FLOOR_BUFFER_BIN_PREFIXES=configured")
                continue
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from typing import Any, Optional, Dict

# Base Paths
INVENTORY_TEAM_DIR = Path(__file__).resolve().parent.parent

# WMS Data & Stacking Paths (누적 저장 폴더)
CURRENT_DIR = Path(__file__).resolve().parent
WMS_DOWNLOAD_DIR = CURRENT_DIR / "wms_daily_stack"
WMS_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

GR_STACK_DIR = CURRENT_DIR / "gr_daily_stack"
GR_STACK_DIR.mkdir(parents=True, exist_ok=True)

GI_STACK_DIR = CURRENT_DIR / "gi_daily_stack"
GI_STACK_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = CURRENT_DIR / "db" / "inventory_history.db"
ONTOLOGY_JSON_PATH = CURRENT_DIR / "ontology" / "material_turnover_ontology.json"
AUDIT_REPORTS_DIR = CURRENT_DIR / "audit_reports"
AUDIT_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# VERIFICATION_MODE & Environment Settings:
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).resolve().parents[2] / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except Exception:
    pass

WMS_BASE_URL = _get_ops_env("WMS_BASE_URL", "https://wms.example.com/WMS_WEB").rstrip("/")
WMS_URL = f"{WMS_BASE_URL}/Login"

GR_URL = f"{WMS_BASE_URL}/GRList"
GI_URL = f"{WMS_BASE_URL}/GiList"
MISSING_GI_URL = f"{WMS_BASE_URL}/MissingGI"

# DAILY_VERIFICATION_MODE: False -> Sends to full production warehouse team.
# WEEKLY_VERIFICATION_MODE: False -> Sends to full production warehouse team on Saturdays (D58).
DAILY_VERIFICATION_MODE = False
WEEKLY_VERIFICATION_MODE = False
VERIFICATION_MODE = False
def is_valid_recipient(addr: str) -> bool:
    """Returns True if addr is a non-empty, non-placeholder email address."""
    if not addr or not isinstance(addr, str):
        return False
    clean = addr.strip().lower()
    return bool(clean) and "@" in clean and "example.com" not in clean

VERIFICATION_RECIPIENT = os.getenv("TEST_EMAIL_RECIPIENT", os.getenv("STOCK_ACCURACY_VERIFY", ""))

_env_to = os.getenv("STOCK_ACCURACY_TO")
_env_cc = os.getenv("STOCK_ACCURACY_CC")

PRODUCTION_RECIPIENTS = {
    # Warehouse Floor Leadership (To)
    "to": [x.strip() for x in _env_to.split(",") if is_valid_recipient(x)] if _env_to is not None else [],
    # Materials Planning (CC)
    "cc": [x.strip() for x in _env_cc.split(",") if is_valid_recipient(x)] if _env_cc is not None else []
}


def get_recipients(mode: str = "Daily") -> dict:
    """Returns safe recipient mapping based on verification status and mode."""
    if mode.lower() == "daily":
        if DAILY_VERIFICATION_MODE:
            return {
                "to": [VERIFICATION_RECIPIENT],
                "cc": []
            }
        return PRODUCTION_RECIPIENTS
    elif mode.lower() == "weekly":
        if WEEKLY_VERIFICATION_MODE:
            return {
                "to": [VERIFICATION_RECIPIENT],
                "cc": []
            }
        return PRODUCTION_RECIPIENTS

    if VERIFICATION_MODE:
        return {
            "to": [VERIFICATION_RECIPIENT],
            "cc": []
        }
    return PRODUCTION_RECIPIENTS


# Backward compatibility alias
WAREHOUSE_RECIPIENTS = get_recipients()

# Monitoring Plants & Storage Locations
SAP_PLANT = _get_ops_env("SAP_PLANT", "P001")
MONITORED_PLANTS = [SAP_PLANT]
DEFAULT_STORAGE_LOCATIONS = ["SL00", "SL06", "SL11"]

# Notification & Pipeline Schedule
DAILY_PIPELINE_SCHEDULE_TIME = "06:50" # 매일 06:50 EST 자동 실행 (07:00 실사 리스트 산출용, D38)
DEFAULT_SCHEDULE_TIMES = ["06:50"] 
DEFAULT_INTERVAL_HOURS = 4

# Alert Threshold
ALERT_ON_ZERO_DIFFERENCE = False # 차이가 0일 때도 정상 보고서를 보낼지 여부

# -----------------------------------------------------------------------------
# Scoring Configuration & Hash (External YAML Matrix)
# -----------------------------------------------------------------------------
SCORING_CONFIG_PATH = CURRENT_DIR / "scoring_config.yaml"


def load_scoring_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads scoring weights, thresholds, and shadow EV parameters from YAML."""
    target_path = Path(config_path) if config_path else SCORING_CONFIG_PATH
    if not target_path.exists():
        return {}
    try:
        import yaml
        with open(target_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[config] Warning: Failed to load scoring config from {target_path}: {e}")
        return {}


def get_config_hash(config_path: Optional[Path] = None) -> str:
    """Computes a reproducible SHA256 hash of the scoring config file."""
    target_path = Path(config_path) if config_path else SCORING_CONFIG_PATH
    if not target_path.exists():
        return "v1.0_default"
    try:
        import hashlib
        with open(target_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return "v1.0_default"


SCORING_CONFIG = load_scoring_config()
SCORING_VERSION = SCORING_CONFIG.get("scoring_version", "v1.0_rules")
SCORING_CONFIG_HASH = get_config_hash()

# -----------------------------------------------------------------------------
# 04_JOB_SystemReconciliation Synergy: Chronic Mismatch Feed Config
# -----------------------------------------------------------------------------
_cm_conf = SCORING_CONFIG.get("chronic_mismatch", {})
CHRONIC_FEED_MODE = os.getenv("CHRONIC_FEED_MODE", _cm_conf.get("mode", "feature_only"))
CHRONIC_MISMATCH_POINTS = int(os.getenv("CHRONIC_MISMATCH_POINTS", str(_cm_conf.get("score_points", 30))))
CHRONIC_RESERVED_DAILY_SLOTS = int(os.getenv("CHRONIC_RESERVED_DAILY_SLOTS", str(_cm_conf.get("reserved_slots_daily", 3))))
CHRONIC_RESERVED_WEEKLY_SLOTS = int(os.getenv("CHRONIC_RESERVED_WEEKLY_SLOTS", str(_cm_conf.get("reserved_slots_weekly", 5))))

# -----------------------------------------------------------------------------
# Random Validation Sampling Config (Unbiased Evaluation & Exploration)
# -----------------------------------------------------------------------------
_rnd_conf = SCORING_CONFIG.get("random_sampling", {})
RANDOM_SLOTS_DAILY = int(os.getenv("RANDOM_SLOTS_DAILY", str(_rnd_conf.get("daily_slots", 3))))
RANDOM_SLOTS_WEEKLY = int(os.getenv("RANDOM_SLOTS_WEEKLY", str(_rnd_conf.get("weekly_slots", 5))))
RANDOM_SEED_STRATEGY = os.getenv("RANDOM_SEED_STRATEGY", _rnd_conf.get("seed_strategy", "run_date"))


def get_random_seed(run_date: Any, strategy: Optional[str] = None) -> Optional[int]:
    """Generates a reproducible integer seed based on run_date or configured strategy."""
    strat = strategy or RANDOM_SEED_STRATEGY
    if strat == "fixed":
        return 42
    if strat == "none":
        return None
    
    date_clean = str(run_date).replace("-", "").replace("_", "")
    digits = "".join(c for c in date_clean if c.isdigit())
    if len(digits) >= 8:
        return int(digits[:8])
    import hashlib
    return int(hashlib.md5(date_clean.encode("utf-8")).hexdigest()[:8], 16)

