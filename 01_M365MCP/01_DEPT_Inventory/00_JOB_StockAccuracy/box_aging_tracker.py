"""
Box Aging & Mis-scan Tracker (01_Inventory_Team / 00_StockAccuracy)
ACME Manufacturing Plant P001 맞춤형 도메인 규칙 기반 오스캔(Mis-scan) 및 장기 정체 박스 탐지 엔진
"""

import os
import sys
import glob
import re
import math
import datetime
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
from typing import List, Dict, Any, Optional, Tuple, Set
import pandas as pd

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import (
    WMS_DOWNLOAD_DIR,
    ONTOLOGY_JSON_PATH,
    AUDIT_REPORTS_DIR,
    load_scoring_config,
    SCORING_CONFIG,
    SCORING_VERSION,
    get_config_hash
)
CURRENT_DIR = Path(__file__).resolve().parent
import logging
logger = logging.getLogger("BoxAgingTracker")

import importlib.util
_spec_db = importlib.util.spec_from_file_location("stock_acc_db_mgr", CURRENT_DIR / "db_manager.py")
_db_mgr = importlib.util.module_from_spec(_spec_db)
_spec_db.loader.exec_module(_db_mgr)
get_latest_missing_gi_map = _db_mgr.get_latest_missing_gi_map
get_latest_material_net_prices = _db_mgr.get_latest_material_net_prices
get_latest_issued_gr_map = _db_mgr.get_latest_issued_gr_map
get_material_activity_map = _db_mgr.get_material_activity_map
get_active_chronic_mismatch_feed = _db_mgr.get_active_chronic_mismatch_feed
get_active_chain_gap_feed = getattr(_db_mgr, "get_active_chain_gap_feed", lambda *a, **kw: {})
get_audit_cooldown_history = getattr(_db_mgr, "get_audit_cooldown_history", lambda *a, **kw: {})
get_daily_field_instructions = getattr(_db_mgr, "get_daily_field_instructions", lambda *a, **kw: [])

# 소물류 키워드 (작업자가 박스 내 낱개 포장을 손으로 집어가기 쉬운 자재)
SMALL_PARTS_KEYWORDS = [
    "SCREW", "AIMING", "AMING", "PAD", "CUSHION", "NUT", "U-NUT", "CLIP", 
    "WASHER", "RIVET", "BOLT", "PIN", "SPRING", "FASTENER", "SPACER", "GROMMET"
]


def load_ontology_cache() -> Dict[str, Any]:
    """Load material turnover ontology JSON if available."""
    if ONTOLOGY_JSON_PATH.exists():
        try:
            import json
            with open(ONTOLOGY_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def parse_lot_no_date(lot_str: Any) -> Optional[datetime.date]:
    """Parse YYMMDD format lot number to date."""
    if pd.isnull(lot_str):
        return None
    s = str(lot_str).strip()
    # Match 6 digits (YYMMDD)
    m = re.match(r"^(\d{2})(\d{2})(\d{2})$", s)
    if m:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # 2000s threshold
        year = 2000 + yy if yy < 80 else 1900 + yy
        try:
            return datetime.date(year, mm, dd)
        except ValueError:
            return None
    # Match 8 digits (YYYYMMDD)
    m8 = re.match(r"^(\d{4})(\d{2})(\d{2})$", s)
    if m8:
        yyyy, mm, dd = int(m8.group(1)), int(m8.group(2)), int(m8.group(3))
        try:
            return datetime.date(yyyy, mm, dd)
        except ValueError:
            return None
    return None


def parse_floor_buffer_prefixes(
    raw_val: Optional[str] = None,
    fallback_val: Optional[str] = None,
) -> Tuple[Tuple[str, ...], Tuple[str, ...], str]:
    """Parse FLOOR_BUFFER_BIN_PREFIXES (comma/semicolon separated) with fallback to OEM_FLOOR_BIN_PREFIX."""
    val = (raw_val if raw_val is not None else os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "")).strip()
    if not val or val.upper() == "OEM":
        fb = (fallback_val if fallback_val is not None else _get_ops_env("OEM_FLOOR_BIN_PREFIX", "OEM")).strip()
        val = fb or "OEM"
    tokens = [tok.strip().upper() for tok in re.split(r"[,;]", val) if tok and tok.strip()]
    non_smt: List[str] = []
    for tok in tokens:
        if tok not in ("SMT", "SMD") and tok not in non_smt:
            non_smt.append(tok)
    if "OEM" not in non_smt:
        non_smt.append("OEM")
    primary_non_smt = non_smt[0] if non_smt else "OEM"
    all_floor: List[str] = ["SMT", "SMD"]
    for tok in tokens:
        if tok not in all_floor:
            all_floor.append(tok)
    if "OEM" not in all_floor:
        all_floor.append("OEM")
    return tuple(all_floor), tuple(non_smt), primary_non_smt


FLOOR_BUFFER_PREFIXES, OEM_FLOOR_PREFIXES, OEM_FLOOR_BIN_PREFIX = parse_floor_buffer_prefixes()


def resolve_non_smt_floor_label(bin_str_up: str, non_smt_prefixes: Optional[Tuple[str, ...]] = None) -> str:
    pfxs = non_smt_prefixes if non_smt_prefixes is not None else OEM_FLOOR_PREFIXES
    for p in pfxs:
        if bin_str_up.startswith(p):
            return f"{p} Floor"
    return f"{OEM_FLOOR_BIN_PREFIX} Floor"


def is_small_part_for_total_pcs(mat_no: str, desc_upper: str, qty: float) -> bool:
    """#188 5: Scoped small-part predicate for Total PCS display (Qty/Box >= 500 or hardware keywords or 20-*/03-*)."""
    m = str(mat_no or "").strip()
    d = str(desc_upper or "").strip().upper()
    q = float(qty or 0.0)
    return (
        q >= 500.0
        or any(kw in d for kw in SMALL_PARTS_KEYWORDS)
        or m.startswith(("20-", "03-"))
    )


_HQ_DIR = CURRENT_DIR.parent.parent / "00_HQ_TaskManager"
if str(_HQ_DIR) not in sys.path:
    sys.path.insert(0, str(_HQ_DIR))
from wms_mapping import WMS_TO_SAP_SLOC_MAP, map_wms_gi_loc_to_sap

WMS_GI_LOC_TO_SLOC: Dict[str, str] = {
    **WMS_TO_SAP_SLOC_MAP,
    **{v: v for v in WMS_TO_SAP_SLOC_MAP.values()},
}

DEFAULT_FORCED_WEEKLY_REASON = "Suspected missing GR (F) — verify SAP GR"
DEFAULT_FORCED_WEEKLY_ACTION = "GR Missing Check (F)"


def normalize_wms_sloc(raw_loc: Any) -> str:
    """Map WMS GI Loc description or code to canonical SAP SLoc via 00_HQ_TaskManager/wms_mapping.py (#216 B-4)."""
    if raw_loc is None:
        return ""
    if isinstance(raw_loc, float) and math.isnan(raw_loc):
        return ""
    s = str(raw_loc).strip().upper()
    if not s or s == "NAN":
        return ""
    mapped = map_wms_gi_loc_to_sap(s)
    return mapped if mapped else s


def load_forced_weekly_gr_check_spec(
    run_date: Optional[str] = None,
    spec_override: Optional[Dict[str, Any]] = None,
    config_path: Optional[Path] = None,
    gi_resolution_logs_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    #199 Item 3: Resolve forced top-pinned Weekly GR Missing Check (Cause F) specification.
    Priority:
      1. Explicit `spec_override` dict (unit tests / programmatic override)
      2. Local git-ignored `ontology/forced_weekly_gr_check.json`
      3. Automatic D109 (`D109_Suspected_Unposted_GR`) discovery from 01_JOB `GI_Resolution_Plan_*.xlsx`
         within the active window `2026-10-30` .. `2026-11-01` (skipped under pytest unless `gi_resolution_logs_dir` is explicit).
    """
    r_date = str(run_date or datetime.date.today().strftime("%Y-%m-%d"))[:10]
    raw_spec: Optional[Dict[str, Any]] = None

    if spec_override is not None:
        raw_spec = dict(spec_override)
    else:
        cfg_file = Path(config_path) if config_path is not None else (CURRENT_DIR / "ontology" / "forced_weekly_gr_check.json")
        if cfg_file.exists():
            try:
                import json as _json_f
                raw_spec = _json_f.loads(cfg_file.read_text(encoding="utf-8"))
            except Exception as e_cfg:
                logger.warning(f"[ForcedWeeklyGR] Failed to parse {cfg_file.name}: {e_cfg}")

    if raw_spec is None:
        # Auto-discover D109_Suspected_Unposted_GR materials from 01_JOB GI_Resolution_Plan files for 2026-10-30..2026-11-01
        in_pytest = ("pytest" in _sys.modules or bool(os.getenv("PYTEST_CURRENT_TEST")))
        if ("2026-10-30" <= r_date <= "2026-11-01") and (gi_resolution_logs_dir is not None or not in_pytest):
            res_dir = Path(gi_resolution_logs_dir) if gi_resolution_logs_dir is not None else (
                CURRENT_DIR.parent / "01_JOB_GR_GI_Error" / "resolution_logs"
            )
            discovered_mats: List[str] = []
            if res_dir.exists():
                plan_files = sorted(
                    res_dir.glob("GI_Resolution_Plan_*.xlsx"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:12]
                for pf in plan_files:
                    try:
                        df_rp = pd.read_excel(pf, dtype={"Material": str})
                        if "Material" in df_rp.columns and "D109_Suspected_Unposted_GR" in df_rp.columns:
                            flag_s = df_rp["D109_Suspected_Unposted_GR"]
                            mask = flag_s.apply(lambda v: bool(v) and str(v).strip().lower() not in ("0", "0.0", "false", "nan", "none", ""))
                            for m_val in df_rp.loc[mask, "Material"].dropna().tolist():
                                m_str = str(m_val).strip()
                                if m_str and m_str not in discovered_mats:
                                    discovered_mats.append(m_str)
                    except Exception:
                        continue
            if discovered_mats:
                raw_spec = {
                    "active_from": "2026-10-30",
                    "active_until": "2026-11-01",
                    "materials": sorted(discovered_mats),
                    "slocs": ["SL00", "SL01"],
                    "action_required": DEFAULT_FORCED_WEEKLY_ACTION,
                    "reason": DEFAULT_FORCED_WEEKLY_REASON,
                }

    if not raw_spec:
        return {
            "enabled": False,
            "materials": set(),
            "materials_clean": set(),
            "slocs": {"SL00", "SL01"},
            "action_required": DEFAULT_FORCED_WEEKLY_ACTION,
            "reason": DEFAULT_FORCED_WEEKLY_REASON,
        }

    active_from = str(raw_spec.get("active_from") or "0000-01-01")[:10]
    active_until = str(raw_spec.get("active_until") or "9999-12-31")[:10]
    if not (active_from <= r_date <= active_until):
        return {
            "enabled": False,
            "materials": set(),
            "materials_clean": set(),
            "slocs": {"SL00", "SL01"},
            "action_required": str(raw_spec.get("action_required") or DEFAULT_FORCED_WEEKLY_ACTION),
            "reason": DEFAULT_FORCED_WEEKLY_REASON if str(raw_spec.get("reason") or "").strip() in ("", "입고 누락 의심(F) — SAP GR 확인용") else str(raw_spec.get("reason")),
        }

    mats_raw = {str(m).strip() for m in (raw_spec.get("materials") or []) if str(m).strip()}
    mats_clean = {m.lstrip("0") for m in mats_raw if m.lstrip("0")}
    slocs_set = {str(s).strip().upper() for s in (raw_spec.get("slocs") or ["SL00", "SL01"]) if str(s).strip()}
    return {
        "enabled": bool(mats_raw),
        "materials": mats_raw,
        "materials_clean": mats_clean,
        "slocs": slocs_set or {"SL00", "SL01"},
        "action_required": str(raw_spec.get("action_required") or DEFAULT_FORCED_WEEKLY_ACTION),
        "reason": DEFAULT_FORCED_WEEKLY_REASON if str(raw_spec.get("reason") or "").strip() in ("", "입고 누락 의심(F) — SAP GR 확인용") else str(raw_spec.get("reason")),
    }


def get_bin_characteristics(bin_val: str, floor_buffer_raw: Optional[str] = None) -> Dict[str, Any]:
    """
    Classify BIN based on ACME Manufacturing warehouse rules:
    - D... : External / Not our warehouse (Excluded)
    - CKD... : CKD dedicated bins (Excluded)
    - EXT_TRL... : Temporary staging / Trailer bins (Excluded from physical cycle count targets)
    - R... : Plant 3 (High-rack warehouse)
    - Non-R : Plant 1 (Production plant)
    - SMT... / SMD... : Supermarket floor buffer (Fast turnover required)
    - CRT... / PRO... : Repack carts / dollies (Fast turnover required)
    - OEM... : Plant 1 OEM dedicated floor buffer bins (Treated like SMT, floor bin, no tier, Daily target / Weekly excluded)
    - Tier : Last digit of standard bin (1, 2 = 1st, 2nd floor -> Hand picking risk)
    """
    b = str(bin_val).strip().upper() if bin_val else ""
    
    # Exclude non-physical / external / trailer staging / S-rack (^S\d+) bins:
    # - D* : External / Supplier warehouse
    # - CKD* : CKD dedicated bins
    # - EXT_TRL* : Temporary staging / Trailer bins (Cross-dock / trailer shipping staging)
    # - S0*..S9* (^S\d+, e.g. S51Z 5) : Non-audit rack permanently excluded per #219
    # - BINSYNTH9953 / NAN / empty
    is_non_physical_or_excluded = (
        not b or 
        b in ("NAN", "BINSYNTH9953", "RESIN") or 
        b.startswith(("EXT_TRL", "D", "CKD")) or
        bool(re.match(r"^S\d+", b))
    )
    is_external = b.startswith("D") or b.startswith("CKD")
    is_temporary_staging = b.startswith("EXT_TRL")
    
    is_plant3 = (bool(re.match(r"^R\d+", b)) or b.startswith("SCR")) and not is_non_physical_or_excluded
    is_plant1 = not is_plant3 and not is_non_physical_or_excluded
    
    if floor_buffer_raw is not None:
        _all_floor_pfxs, _non_smt_pfxs, _ = parse_floor_buffer_prefixes(raw_val=floor_buffer_raw)
    else:
        _all_floor_pfxs, _non_smt_pfxs = FLOOR_BUFFER_PREFIXES, OEM_FLOOR_PREFIXES
    is_supermarket = not is_non_physical_or_excluded and b.startswith(_all_floor_pfxs)
    is_oem_floor = not is_non_physical_or_excluded and (not b.startswith(("SMT", "SMD"))) and b.startswith(_non_smt_pfxs)
    is_repack_cart = not is_non_physical_or_excluded and (b.startswith("CRT") or b.startswith("PRO"))
    
    # Extract tier (last digit) for standard racks only (never for trailers, temporary staging bins, SCR racks, or floor buffers like SMT/OEM/CRT)
    tier = None
    if not is_non_physical_or_excluded and not (is_supermarket or is_repack_cart or b.startswith("SCR")):
        m = re.search(r"(\d)$", b)
        if m:
            tier = int(m.group(1))
            
    is_hand_picking_tier = is_plant1 and (tier in [1, 2])
    
    return {
        "bin": b,
        "is_external": is_external,
        "is_temporary_staging": is_temporary_staging,
        "is_non_physical_or_excluded": is_non_physical_or_excluded,
        "is_plant3": is_plant3,
        "is_plant1": is_plant1,
        "is_supermarket": is_supermarket,
        "is_oem_floor": is_oem_floor,
        "is_repack_cart": is_repack_cart,
        "tier": tier,
        "is_hand_picking_tier": is_hand_picking_tier
    }


class BoxAgingTracker:
    def __init__(self, wms_dir: Optional[Path] = None):
        self.wms_dir = wms_dir or WMS_DOWNLOAD_DIR
        self.ontology = load_ontology_cache()
        try:
            self.missing_gi_map = get_latest_missing_gi_map()
        except Exception:
            self.missing_gi_map = {}
        self.degraded_inputs = {}

        try:
            self.issued_gr_map = get_latest_issued_gr_map()
        except Exception as e:
            logger.warning(f"[TrackerInit] Failed to load issued_gr_map: {e}")
            self.issued_gr_map = {}
            self.degraded_inputs["issued_gr_map"] = str(e)

        try:
            self.material_activity_map = get_material_activity_map(threshold_days=45)
        except Exception as e:
            logger.warning(f"[TrackerInit] Failed to load material_activity_map: {e}")
            self.material_activity_map = {}
            self.degraded_inputs["material_activity_map"] = str(e)

        try:
            self.chronic_feed = get_active_chronic_mismatch_feed()
        except Exception as e:
            logger.warning(f"[TrackerInit] Failed to load chronic_feed: {e}")
            self.chronic_feed = {}
            self.degraded_inputs["chronic_feed"] = str(e)

        try:
            self.chain_gap_feed = get_active_chain_gap_feed()
        except Exception as e:
            logger.warning(f"[TrackerInit] Failed to load chain_gap_feed: {e}")
            self.chain_gap_feed = {}
            self.degraded_inputs["chain_gap_feed"] = str(e)

        try:
            self.net_prices = get_latest_material_net_prices()
        except Exception as e:
            logger.warning(f"[TrackerInit] Failed to load net_prices: {e}")
            self.net_prices = {}
            self.degraded_inputs["net_prices"] = str(e)

        try:
            self.scoring_config = load_scoring_config() or SCORING_CONFIG or {}
        except Exception:
            self.scoring_config = {}

    def find_latest_wms_file(self) -> Optional[Path]:
        """Find the most recent WMS Stock List Excel file."""
        candidates = []
        if self.wms_dir.exists():
            candidates.extend(glob.glob(str(self.wms_dir / "*Stock*List*.*")))
            candidates.extend(glob.glob(str(self.wms_dir / "*Stock*.*")))
        if not candidates:
            logger.warning("[BoxAgingTracker] No WMS Stock file found in wms_dir. Stale desktop fallback forbidden.")
            return None
        return Path(max(candidates, key=os.path.getmtime))


    def load_stock_list(self, file_path: Optional[Path] = None, dry_run: bool = False) -> pd.DataFrame:
        """Load WMS Stock List Excel file with parsed Lot No and Bin info."""
        target = file_path or self.find_latest_wms_file()
        if not target or not target.exists():
            raise FileNotFoundError(f"WMS Stock List file not found: {target}")

        print(f"[BoxAgingTracker] WMS 파일 로딩 중: {target.name}...")
        df = pd.read_excel(target)
        
        # Identify Barcode column
        barcode_col = next((c for c in df.columns if c.strip().lower() == "barcode"), None)
        if not barcode_col:
            raise ValueError(f"파일에 'Barcode' 열이 없습니다. Columns: {df.columns.tolist()[:8]}")

        df = df.dropna(subset=[barcode_col]).copy()
        df["Barcode"] = df[barcode_col].astype(str).str.strip()
        df["Mat.No"] = df["Mat.No"].astype(str).str.strip()
        df["BIN"] = df["BIN"].astype(str).str.strip()
        df["Mat.Desc"] = df["Mat.Desc"].astype(str).str.strip()
        
        # Parse GR Date (창고 실제 입고일 기준 체류일수 계산)
        today_dt = pd.to_datetime(datetime.date.today())
        if "GR Date" in df.columns:
            gr_dt = pd.to_datetime(df["GR Date"].astype(str), format="%Y%m%d", errors="coerce")
            df["GR_Date"] = gr_dt.dt.date
            df["Warehouse_Dwell_Days"] = (today_dt - gr_dt).dt.days.fillna(0).astype(int)
        else:
            df["GR_Date"] = None
            df["Warehouse_Dwell_Days"] = 0

        # Parse Lot No Date (오직 선입선출 FIFO 분석에만 사용)
        if "Lot No" in df.columns:
            df["Lot_Date"] = df["Lot No"].apply(parse_lot_no_date)
        else:
            df["Lot_Date"] = None

        # WMS 스냅샷 누적 테이블(BARCODE_BIN_STAY) 업데이트 및 동일 Bin 체류일수 매핑
        try:
            from db_manager import update_barcode_bin_stay, get_bin_dwell_map, save_wms_stock_daily_agg
            if not dry_run:
                today_str = datetime.date.today().strftime("%Y-%m-%d")
                update_barcode_bin_stay(df, snapshot_date=today_str)
                try:
                    save_wms_stock_daily_agg(df, snapshot_date=today_str)
                except Exception as e_agg:
                    print(f"[WARN] WMS_STOCK_DAILY_AGG 적재 실패 (파이프라인 계속 진행): {e_agg}")
            bin_dwell_map = get_bin_dwell_map(active_only=True)
            df["Bin_Dwell_Days"] = df["Barcode"].apply(lambda b: bin_dwell_map.get(b, {}).get("consecutive_days_in_bin", 1))
        except Exception as e:
            df["Bin_Dwell_Days"] = 1

        return df

    def detect_mis_scans(
        self,
        df: pd.DataFrame,
        mode: str = "Daily",
        chronic_feed_mode: Optional[str] = None,
        random_slots: Optional[int] = None,
        run_date: Optional[str] = None,
        scoring_config: Optional[Dict[str, Any]] = None,
        forced_weekly_spec: Optional[Dict[str, Any]] = None,
        gi_resolution_logs_dir: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """
        Multi-factor Mis-scanning & Inactivity Detection:
        - Daily Mode: Fast floor audit (SMT supermarket, Plant 1 lower-tier small parts, FIFO inversions)
        - Weekly Mode: Comprehensive deep audit (Plant 3 high-racks R..., Fast Mover residual aging, turnover outliers, SMT lingering)
        - Chronic Feed: Synergy with 04_JOB_SystemReconciliation (Mode A feature_only or Mode B reserved_slots)
        - Shadow EV: Computes Expected Loss / Duration alongside rule score for shadow ranking comparison
        """
        cfg = scoring_config or getattr(self, "scoring_config", None) or {}
        w = cfg.get("weights", {})
        t = cfg.get("thresholds", {})
        target_counts_cfg = cfg.get("target_counts", {})
        shadow_cfg = cfg.get("shadow_ev", {})
        chronic_cfg = cfg.get("chronic_mismatch", {})
        random_cfg = cfg.get("random_sampling", {})

        # Slot counts
        daily_top_slots = target_counts_cfg.get("daily", {}).get("top_slots", 15)
        daily_ext_slots = target_counts_cfg.get("daily", {}).get("extended_slots", 25)
        daily_total_slots = daily_top_slots + daily_ext_slots
        weekly_top_slots = target_counts_cfg.get("weekly", {}).get("top_slots", 50)

        # Chronic Mismatch Feed parameters
        resolved_feed_mode = chronic_feed_mode or chronic_cfg.get("mode", "feature_only")
        chronic_points = int(chronic_cfg.get("score_points", 30))
        k_daily = int(chronic_cfg.get("reserved_slots_daily", 3))
        m_weekly = int(chronic_cfg.get("reserved_slots_weekly", 5))

        # Random validation sample slots
        if random_slots is None:
            random_slots = int(random_cfg.get("weekly_slots", 5) if mode.lower() == "weekly" else random_cfg.get("daily_slots", 3))

        # Step 1: Pre-calculate max/newest Lot Date per Material for FIFO checks
        valid_lots = df.dropna(subset=["Lot_Date"])
        latest_lot_per_mat = valid_lots.groupby("Mat.No")["Lot_Date"].max().to_dict()

        # Step 1-B: Pre-calculate Bin-Level Lot Date & Box Counts for Bin Activity & Local Obstruction
        # (특정 바코드가 그 빈에서 나가지 않았더라도, 그 빈의 다른 최신 박스가 나가거나 공존하는지 판별)
        bin_mat_latest_lot = valid_lots.groupby(["BIN", "Mat.No"])["Lot_Date"].max().to_dict()
        bin_mat_box_count = df.groupby(["BIN", "Mat.No"])["Barcode"].count().to_dict()

        # Step 1-C: Pre-load Missing GI Baseline Profile (도메인 편차 및 이상치 학습 마스터)
        missing_gi_map = {}
        try:
            missing_gi_map = get_latest_missing_gi_map(plant=_get_ops_env("SAP_PLANT", "P001"))
        except Exception:
            missing_gi_map = {}

        results = []
        today = datetime.date.today()

        for idx, row in df.iterrows():
            barcode = row["Barcode"]
            mat_no = row["Mat.No"]
            mat_clean = mat_no.lstrip("0")
            mgi_info = missing_gi_map.get(mat_no) or missing_gi_map.get(mat_clean)
            desc = row["Mat.Desc"].upper()
            raw_bin = str(row["BIN"]).strip()
            bin_str = raw_bin if raw_bin and raw_bin.lower() != "nan" else "BINSYNTH9953"
            qty = row.get("Qty.", 0)
            lot_str = str(row.get("Lot No", ""))
            lot_date = row.get("Lot_Date")
            gr_date = row.get("GR_Date")
            wh_dwell_days = int(row.get("Warehouse_Dwell_Days", 0) or 0)
            bin_dwell_days = int(row.get("Bin_Dwell_Days", 1) or 1)
            
            bin_info = get_bin_characteristics(bin_str)
            if bin_info["is_non_physical_or_excluded"] or bin_info["is_external"] or bin_info["is_temporary_staging"]:
                continue
            
            # Check small hardware & box packaging quantity (박스 당량)
            is_small_part_keyword = any(kw in desc for kw in SMALL_PARTS_KEYWORDS)
            is_high_box_qty = (qty is not None and qty >= t.get("small_part_qty_threshold_small", 200))
            is_small_part = is_small_part_keyword or is_high_box_qty
            
            # Lot Age is ONLY for FIFO analysis
            lot_age_days = (today - lot_date).days if lot_date else None
            
            # D102: For CRT*, PRO*, SMT* bins, judge dwell/stagnation/FIFO/turnover by elapsed time in bin (bin_dwell_days)
            # instead of warehouse GR_Date dwell (wh_dwell_days).
            # Per Thread 152, Ghost Stock / Missing GI deficit scoring is exempt from D102 and preserves warehouse-level checks.
            raw_bin_up = bin_str.strip().upper()
            is_bin_dwell_basis = raw_bin_up.startswith(("CRT", "PRO", "SMT"))
            effective_dwell_days = bin_dwell_days if is_bin_dwell_basis else wh_dwell_days

            # Check FIFO Inversion: Is there a newer Lot for this exact Mat.No in stock?
            # For D102 bins (CRT*, PRO*, SMT*), a box that just entered the bin today (bin_dwell_days < 2)
            # is actively being staged/supplied and not stagnant/inverted in the bin.
            latest_lot = latest_lot_per_mat.get(mat_no)
            wh_is_fifo_inversion = False
            is_fifo_inversion = False
            fifo_gap_days = 0
            if lot_date and latest_lot and latest_lot > lot_date:
                fifo_gap_days = (latest_lot - lot_date).days
                if fifo_gap_days >= t.get("fifo_inversion_gap_days", 30):
                    wh_is_fifo_inversion = True
                    if not is_bin_dwell_basis or bin_dwell_days >= 2:
                        is_fifo_inversion = True

            # Pre-fetch Ontology Turnover profile for material
            ont = self.ontology.get(mat_no, {})
            turnover_prof = ont.get("turnover_profile", {})
            std_turnover = turnover_prof.get("trimmed_mean_days")
            mover_class = turnover_prof.get("mover_class")

            # Warehouse-level stagnation/turnover/residual flags preserved for Ghost Stock (D102-exempt per #152)
            wh_is_smt_stagnant = False
            wh_is_repack_stagnant = False
            if bin_info["is_supermarket"]:
                if std_turnover and std_turnover > 0:
                    if wh_dwell_days >= max(t.get("smt_min_wh_dwell_days", 7.0), t.get("smt_turnover_wh_multiplier", 1.5) * std_turnover) or bin_dwell_days >= max(int(t.get("smt_min_bin_dwell_days", 3)), int(t.get("smt_turnover_bin_multiplier", 0.5) * std_turnover)):
                        wh_is_smt_stagnant = True
                else:
                    if wh_dwell_days >= t.get("smt_fallback_wh_dwell_days", 14) or bin_dwell_days >= t.get("smt_fallback_bin_dwell_days", 5):
                        wh_is_smt_stagnant = True
            elif bin_info["is_repack_cart"]:
                if std_turnover and std_turnover > 0:
                    if wh_dwell_days >= max(t.get("repack_min_wh_dwell_days", 5.0), t.get("repack_turnover_wh_multiplier", 1.2) * std_turnover) or bin_dwell_days >= max(int(t.get("repack_min_bin_dwell_days", 2)), int(t.get("repack_turnover_bin_multiplier", 0.4) * std_turnover)):
                        wh_is_repack_stagnant = True
                else:
                    if wh_dwell_days >= t.get("repack_fallback_wh_dwell_days", 10) or bin_dwell_days >= t.get("repack_fallback_bin_dwell_days", 4):
                        wh_is_repack_stagnant = True
            wh_turnover_anomaly = bool(
                std_turnover and wh_dwell_days is not None and
                wh_dwell_days > max(t.get("turnover_moderate_min_dwell_days", 14.0), t.get("turnover_moderate_multiplier", 3.0) * std_turnover)
            )
            wh_fast_mover_residual = False
            if mover_class == "FAST_MOVER":
                if lot_date and latest_lot and latest_lot > lot_date and (latest_lot - lot_date).days >= t.get("fast_mover_latest_lot_gap_days", 14):
                    wh_fast_mover_residual = True
                elif std_turnover and wh_dwell_days and wh_dwell_days >= max(t.get("fast_mover_min_dwell_days", 14), t.get("fast_mover_turnover_multiplier", 5.0) * std_turnover):
                    wh_fast_mover_residual = True

            # Determine Risk Categories
            risk_types = []
            
            # 1. Supermarket / Repack Cart (Turnover Days 연계 동적 정체 판정 + D102 빈 진입 후 경과 기준)
            is_smt_stagnant = False
            is_repack_stagnant = False
            if bin_info["is_supermarket"]:
                risk_types.append("SMT_SUPERMARKET_BUFFER")
                if is_bin_dwell_basis:
                    # SMT* bins (D102): Use bin_dwell_days only (never wh_dwell_days)
                    # Exclude STAGNANT dead-turnover items (std_turnover >= 365d) from fast-supermarket stagnation
                    smt_min_bd = int(t.get("smt_min_bin_dwell_days", 3))
                    if mover_class != "STAGNANT" and (not std_turnover or std_turnover < 365.0):
                        if bin_dwell_days >= smt_min_bd:
                            is_smt_stagnant = True
                            risk_types.append("SMT_STAGNANT")
                else:
                    # OEM* / other supermarket floor bins
                    if wh_is_smt_stagnant:
                        is_smt_stagnant = True
                        risk_types.append("SMT_STAGNANT")

            elif bin_info["is_repack_cart"]:
                # CRT* / PRO* bins (D102): 2-hour supply cart / production repack -> use bin_dwell_days only
                risk_types.append("REPACK_CART_DOLLY")
                repack_min_bd = int(t.get("repack_min_bin_dwell_days", 2))
                if mover_class != "STAGNANT" and (not std_turnover or std_turnover < 365.0):
                    if bin_dwell_days >= repack_min_bd:
                        is_repack_stagnant = True
                        risk_types.append("REPACK_STAGNANT")

            # 2. Plant 1 Hand-picking 1st/2nd Tier
            if bin_info["is_hand_picking_tier"]:
                if is_small_part:
                    risk_types.append("PLANT1_HAND_PICK_SMALL_PART")
                else:
                    risk_types.append("PLANT1_HAND_PICK_TIER_1_2")

            # 3. FIFO Inversion (Lot Date 기준 선입선출 역전)
            if is_fifo_inversion:
                risk_types.append("FIFO_INVERSION")

            # 3-B. GI-Verified FIFO Bypass
            # D102: For CRT*, PRO*, SMT* bins, compare latest_issued_gr_date against bin entry date
            # rather than warehouse GR_Date so boxes freshly moved into CRT/PRO/SMT are not flagged for old rack bypass.
            gi_info = self.issued_gr_map.get(mat_no) or self.issued_gr_map.get(mat_clean) or {}
            latest_issued_gr_date = gi_info.get("latest_issued_gr_date")
            is_proven_fifo_bypass = False
            fifo_bypass_gap_days = 0
            eff_bypass_ref_date = (today - datetime.timedelta(days=max(0, bin_dwell_days - 1))) if is_bin_dwell_basis else gr_date
            if eff_bypass_ref_date and latest_issued_gr_date and latest_issued_gr_date > eff_bypass_ref_date:
                fifo_bypass_gap_days = (latest_issued_gr_date - eff_bypass_ref_date).days
                if fifo_bypass_gap_days >= t.get("fifo_bypass_gap_days", 14):
                    is_proven_fifo_bypass = True
                    risk_types.append("PROVEN_FIFO_BYPASS_GI_VERIFIED")
                    if bin_info["is_plant1"]:
                        risk_types.append("FIFO_BYPASS_PLANT1_FLOOR")
                    else:
                        risk_types.append("FIFO_BYPASS_PLANT3_REPLENISH")

            # 3-C. Check Dead / Obsolete Stock (최근 45일간 입고/출고 이력 전무 품목) 및 Slow Stock
            mat_act = self.material_activity_map.get(mat_no) or self.material_activity_map.get(mat_clean) or {}
            is_dead_stock = False
            days_inactive = mat_act.get("days_since_activity", 0)
            if mat_act and not mat_act.get("is_active", True) and days_inactive >= t.get("dead_stock_inactive_days", 45):
                is_dead_stock = True
                risk_types.append("OBSOLETE_DEAD_STOCK")

            # 슬로우 스톡 (Slow Mover / Infrequent Stock) 판정:
            # 1) 온톨로지 회전 분류가 SLOW_MOVER, STAGNANT, 또는 PENDING_FIRST_GI (장기간 출고 극소)
            # 2) 표준 회전일수(Turnover)가 25일 이상인 저회전 자재
            # 3) 최근 30일 이상 입출고 활동이 없는 자재
            is_slow_stock = (
                mover_class in ["SLOW_MOVER", "STAGNANT", "PENDING_FIRST_GI"] or 
                (std_turnover is not None and std_turnover >= t.get("slow_stock_turnover_days", 25.0)) or
                (days_inactive >= t.get("slow_stock_inactive_days", 30))
            )
            if is_slow_stock:
                risk_types.append("SLOW_STOCK_DEMOTE")

            # 4. Ontology Turnover anomaly: 체류일수가 표준 회전일의 3배 초과 (D102: CRT/PRO/SMT는 bin_dwell_days 사용)
            if std_turnover and effective_dwell_days is not None:
                if effective_dwell_days > max(t.get("turnover_moderate_min_dwell_days", 14.0), t.get("turnover_moderate_multiplier", 3.0) * std_turnover):
                    risk_types.append("TURNOVER_ANOMALY")

            # 5. Weekly Target Additions:
            # - Plant 3 High-Rack / High Tier (tiers 3-5) Aging (GR Date 기준 체류일수)
            is_high_rack = bin_info["is_plant3"] or (bin_info["tier"] is not None and bin_info["tier"] >= 3)
            if is_high_rack and std_turnover and wh_dwell_days and std_turnover > 0:
                if (wh_dwell_days / std_turnover >= t.get("high_rack_turnover_multiplier", 3.0)) or (wh_dwell_days >= t.get("high_rack_min_dwell_days", 30)):
                    risk_types.append("HIGH_RACK_AGING")

            # - Fast Mover Lingering Residual Barcode
            is_fast_mover_residual = False
            if mover_class == "FAST_MOVER":
                if (not is_bin_dwell_basis or bin_dwell_days >= 2) and lot_date and latest_lot and latest_lot > lot_date and (latest_lot - lot_date).days >= t.get("fast_mover_latest_lot_gap_days", 14):
                    is_fast_mover_residual = True
                elif std_turnover and effective_dwell_days and effective_dwell_days >= max(t.get("fast_mover_min_dwell_days", 14), t.get("fast_mover_turnover_multiplier", 5.0) * std_turnover):
                    is_fast_mover_residual = True
            if is_fast_mover_residual:
                risk_types.append("FAST_MOVER_RESIDUAL")

            # 6. Bin Activity & Same-Bin Lot Spread (동일 빈 내 출고 활성도 & 신규 로트 대비 방치)
            bin_latest_lot = bin_mat_latest_lot.get((raw_bin, mat_no))
            same_bin_boxes = bin_mat_box_count.get((raw_bin, mat_no), 1)
            is_same_bin_obstruction = False
            bin_lot_spread_days = 0
            if lot_date and bin_latest_lot and bin_latest_lot > lot_date:
                bin_lot_spread_days = (bin_latest_lot - lot_date).days
                if bin_lot_spread_days >= t.get("same_bin_spread_days", 20):
                    is_same_bin_obstruction = True
                    risk_types.append("SAME_BIN_FIFO_OBSTRUCTION")

            # 7. Missing GI Cross-Validation (BOM 소모 vs 라인 출고 차이 연계)
            mat_clean = mat_no.lstrip("0")
            mgi = self.missing_gi_map.get(mat_no) or self.missing_gi_map.get(mat_clean) or {}
            diff_flag = mgi.get("diff_flag", "")
            diff_box = float(mgi.get("diff_box", 0.0) or 0.0)
            target_bins_str = mgi.get("target_bins", "")
            is_unit_offset = mgi.get("is_unit_offset_item", False)
            baseline_diff = mgi.get("baseline_diff_box", 0.0)
            anomaly_diff = mgi.get("anomaly_diff_box", diff_box)

            if diff_flag == "UNDER":
                # 출고/포장 단위 불일치 상시 편차 품목인 경우
                if is_unit_offset:
                    # 상시 편차(Baseline) 대비 순수 초과 결손이 2박스 이상일 때만 진성 Ghost Stock으로 판정
                    if anomaly_diff >= t.get("unit_offset_min_anomaly_diff", 2.0):
                        risk_types.append("CRITICAL_GHOST_SUSPECT")
                    elif diff_box >= t.get("general_missing_gi_diff_box", 1.0):
                        risk_types.append("UNIT_PACK_OFFSET_NORMAL")
                else:
                    # 일반 품목: 1박스 이상 결손 시 Ghost Stock 의심
                    # D102-exempt per #152: Ghost/Missing GI deficit score is never reduced by bin_dwell_days
                    if diff_box >= t.get("general_missing_gi_diff_box", 1.0):
                        if (raw_bin in target_bins_str) or wh_is_fifo_inversion or is_same_bin_obstruction or wh_turnover_anomaly or ("HIGH_RACK_AGING" in risk_types) or wh_fast_mover_residual or wh_is_smt_stagnant or wh_is_repack_stagnant or is_smt_stagnant or is_repack_stagnant:
                            risk_types.append("CRITICAL_GHOST_SUSPECT")
                        else:
                            risk_types.append("MISSING_GI_UNDER_DEFICIT")
            elif diff_flag == "OVER":
                # 라인 출고가 충분한 상태에서 오래된 박스가 남아있다면 실물 후열 매몰(선입선출 미준수)
                if is_fifo_inversion or is_same_bin_obstruction:
                    risk_types.append("FIFO_PHYSICAL_TRAP")

            # Calculate Dynamic Risk Score & Action Type
            risk_score = 0
            score_breakdown = {}
            action_type = "Physical Audit"
            reasons = []

            # 0. Common High-Confidence Missing GI & Same-Bin Rules (Both Daily & Weekly)
            if "CRITICAL_GHOST_SUSPECT" in risk_types or "CRITICAL_PHANTOM_SUSPECT" in risk_types:
                scaled_add = min(w.get("critical_ghost_max_add", 25), int(max(0.0, anomaly_diff) * w.get("critical_ghost_scale_factor", 0.5)))
                pts = w.get("critical_ghost_base", 65) + scaled_add
                risk_score += pts
                score_breakdown["CRITICAL_GHOST_SUSPECT"] = pts
                action_type = "Ghost Stock Audit"
                if is_unit_offset:
                    reasons.append(f"Ghost Stock Deficit ({diff_box:.0f} Bx, Base {baseline_diff:.0f}, +{scaled_add}pt)")
                else:
                    reasons.append(f"Ghost Stock ({diff_box:.0f} Bx Deficit, +{scaled_add}pt)")
            elif "UNIT_PACK_OFFSET_NORMAL" in risk_types:
                pts = w.get("unit_pack_offset_normal", 10)
                risk_score += pts
                score_breakdown["UNIT_PACK_OFFSET_NORMAL"] = pts
                action_type = "Packaging Unit Check"
                reasons.append(f"Unit Offset Baseline ({baseline_diff:.0f} Bx)")

            # GI-Verified FIFO Bypass (실제 생산 출고 이력으로 증명된 FIFO 바이패스: Plant 1 현장 vs Plant 3 보충 구분)
            if "FIFO_BYPASS_PLANT1_FLOOR" in risk_types:
                pts = w.get("fifo_bypass_plant1_floor", 40)
                risk_score += pts
                score_breakdown["FIFO_BYPASS_PLANT1_FLOOR"] = pts
                if action_type == "Physical Audit":
                    action_type = "FIFO Bypass Audit"
                reasons.append(f"Floor FIFO Bypass ({fifo_bypass_gap_days}d Newer Issued)")
            elif "FIFO_BYPASS_PLANT3_REPLENISH" in risk_types:
                pts = w.get("fifo_bypass_plant3_replenish", 40)
                risk_score += pts
                score_breakdown["FIFO_BYPASS_PLANT3_REPLENISH"] = pts
                if action_type == "Physical Audit":
                    action_type = "Replenish Bypass"
                reasons.append(f"High-Rack Replenish Bypass ({fifo_bypass_gap_days}d Newer Issued)")
            elif "PROVEN_FIFO_BYPASS_GI_VERIFIED" in risk_types:
                pts = w.get("proven_fifo_bypass_gi_verified", 40)
                risk_score += pts
                score_breakdown["PROVEN_FIFO_BYPASS_GI_VERIFIED"] = pts
                if action_type == "Physical Audit":
                    action_type = "FIFO Bypass Audit"
                reasons.append(f"GI FIFO Bypass ({fifo_bypass_gap_days}d Newer Issued)")

            elif "SAME_BIN_FIFO_OBSTRUCTION" in risk_types:
                pts = w.get("same_bin_fifo_obstruction", 35)
                risk_score += pts
                score_breakdown["SAME_BIN_FIFO_OBSTRUCTION"] = pts
                if action_type == "Physical Audit":
                    action_type = "Bin FIFO Sort"
                reasons.append(f"Same-Bin Spread ({bin_lot_spread_days}d)")
            elif "FIFO_PHYSICAL_TRAP" in risk_types:
                pts = w.get("fifo_physical_trap", 30)
                risk_score += pts
                score_breakdown["FIFO_PHYSICAL_TRAP"] = pts
                if action_type == "Physical Audit":
                    action_type = "Rear Pallet Check"
                reasons.append("FIFO Buried Box")

            if mode.lower() == "weekly":
                # =========================================================
                # WEEKLY SCORING: High-Rack & Inventory Deep Audit
                # =========================================================
                # 1. Fast Mover Lingering Residual (+55)
                if "FAST_MOVER_RESIDUAL" in risk_types:
                    pts = w.get("fast_mover_residual", 55)
                    risk_score += pts
                    score_breakdown["FAST_MOVER_RESIDUAL"] = pts
                    if action_type == "Physical Audit":
                        action_type = "Fast Mover Audit"
                    reasons.append("Fast Mover Residual")

                # 2. Plant 3 High-Rack / High-Tier Aging (+50)
                if "HIGH_RACK_AGING" in risk_types:
                    pts = w.get("high_rack_aging", 50)
                    risk_score += pts
                    score_breakdown["HIGH_RACK_AGING"] = pts
                    if action_type == "Physical Audit":
                        action_type = "High-Rack Audit"
                    reasons.append("High-Rack Aging")

                # 3. Extreme Turnover Outliers (+45) (GR Date 기준 창고 체류일수 사용)
                if std_turnover and wh_dwell_days and std_turnover > 0:
                    ratio = wh_dwell_days / std_turnover
                    if ratio >= t.get("turnover_extreme_multiplier", 10.0) and wh_dwell_days >= t.get("turnover_weekly_extreme_min_dwell", 45):
                        pts = w.get("turnover_anomaly_extreme", 45)
                        risk_score += pts
                        score_breakdown["TURNOVER_ANOMALY_EXTREME"] = pts
                        if action_type == "Physical Audit":
                            action_type = "Turnover Audit"
                        reasons.append("Turnover Outlier")
                    elif ratio >= t.get("turnover_high_multiplier", 5.0) and wh_dwell_days >= t.get("turnover_weekly_high_min_dwell", 25):
                        pts = w.get("turnover_anomaly_high", 30)
                        risk_score += pts
                        score_breakdown["TURNOVER_ANOMALY_HIGH"] = pts
                        reasons.append("Turnover Outlier")

                # 4. SMT Floor Lingering (+35) (표준 회전일수 초과 정체 품목만 점수 부과)
                if "SMT_STAGNANT" in risk_types or "REPACK_STAGNANT" in risk_types:
                    pts = w.get("smt_stagnant_weekly", 35)
                    risk_score += pts
                    score_breakdown["SMT_REPACK_STAGNANT"] = pts
                    _is_non_smt_fb = bool(bin_info["is_oem_floor"] and not raw_bin_up.startswith(("SMT", "SMD")))
                    reasons.append("Floor Buffer Stagnant" if ("SMT_STAGNANT" in risk_types and _is_non_smt_fb) else "SMT Stagnant")

                # 5. FIFO Inversion (+30, only if not already scored by GI-verified)
                if "FIFO_INVERSION" in risk_types and "PROVEN_FIFO_BYPASS_GI_VERIFIED" not in risk_types:
                    pts = w.get("fifo_inversion_weekly", 30)
                    risk_score += pts
                    score_breakdown["FIFO_INVERSION"] = pts
                    reasons.append("FIFO Inversion (Stock)")

                # 6. BINSYNTH9953 Bin (+20)
                if bin_str == "BINSYNTH9953":
                    pts = w.get("BINSYNTH9953_bin", 20)
                    risk_score += pts
                    score_breakdown["BINSYNTH9953_BIN"] = pts
                    reasons.append("BINSYNTH9953 Bin")

            else:
                # =========================================================
                # DAILY SCORING: 15-Minute Floor Rapid Audit
                # =========================================================
                # 1. SMT Floor & Repack Carts (동적 Turnover 기준 차등 배점 + D102 빈 체류 기준)
                if "SMT_STAGNANT" in risk_types:
                    pts = w.get("smt_stagnant_daily", 45)
                    risk_score += pts
                    score_breakdown["SMT_STAGNANT"] = pts
                    if action_type == "Physical Audit":
                        action_type = "SMT Audit"
                    _is_non_smt_fb = bool(bin_info["is_oem_floor"] and not raw_bin_up.startswith(("SMT", "SMD")))
                    _stag_lbl = "Floor Buffer Stagnant" if _is_non_smt_fb else "SMT Stagnant"
                    reasons.append(f"{_stag_lbl} ({effective_dwell_days}d/Turn {std_turnover:.0f}d)" if std_turnover and std_turnover > 0 else f"{_stag_lbl} ({effective_dwell_days}d)")
                elif "SMT_SUPERMARKET_BUFFER" in risk_types:
                    # 정상 회전 범위 내의 버퍼: 기본 관리 배점만 부여하여 최우선 순위 독점 방지
                    pts = w.get("smt_supermarket_buffer", 10)
                    risk_score += pts
                    score_breakdown["SMT_SUPERMARKET_BUFFER"] = pts

                if "REPACK_STAGNANT" in risk_types:
                    pts = w.get("repack_stagnant_daily", 40)
                    risk_score += pts
                    score_breakdown["REPACK_STAGNANT"] = pts
                    if action_type == "Physical Audit":
                        action_type = "Repack Cart Audit"
                    reasons.append(f"Repack Stagnant ({effective_dwell_days}d/Turn {std_turnover:.0f}d)" if std_turnover and std_turnover > 0 else f"Repack Stagnant ({effective_dwell_days}d)")
                elif "REPACK_CART_DOLLY" in risk_types:
                    # 정상 회전 범위 내 대차: 기본 배점만 부여
                    pts = w.get("repack_cart_dolly", 10)
                    risk_score += pts
                    score_breakdown["REPACK_CART_DOLLY"] = pts

                # 2. Plant 1 Accessible Small Hardware (회전일수, 체류일수 및 박스 당량 결합 동적 배점)
                if "PLANT1_HAND_PICK_SMALL_PART" in risk_types:
                    action_type = "Box Presence Check"
                    turn_ref = std_turnover if (std_turnover and std_turnover > 0) else 7.0
                    # 기본 체류/회전 배점
                    if wh_dwell_days < max(t.get("small_part_dwell_fresh_min", 3.0), t.get("small_part_dwell_fresh_ratio", 0.3) * turn_ref):
                        pts = w.get("small_part_dwell_fresh", 15)
                        risk_score += pts
                        score_breakdown["SMALL_PART_DWELL_FRESH"] = pts
                        reasons.append("Small Part Fresh")
                    elif wh_dwell_days >= max(t.get("small_part_dwell_stagnant_min", 7.0), t.get("small_part_dwell_stagnant_ratio", 1.0) * turn_ref):
                        pts = w.get("small_part_dwell_stagnant", 50)
                        risk_score += pts
                        score_breakdown["SMALL_PART_DWELL_STAGNANT"] = pts
                        reasons.append(f"Small Part Stagnant ({wh_dwell_days}d/Turn {turn_ref:.0f}d)")
                    else:
                        pts = w.get("small_part_dwell_aging", 30)
                        risk_score += pts
                        score_breakdown["SMALL_PART_DWELL_AGING"] = pts
                        reasons.append(f"Small Part Aging ({wh_dwell_days}d)")

                    # 박스 당량(Quantity per Box) 가산점: 당량이 클수록 낱개 미스캔 누락 위험 급증
                    if qty >= t.get("small_part_qty_threshold_large", 5000):
                        pts = w.get("small_part_pack_qty_5000", 30)
                        risk_score += pts
                        score_breakdown["SMALL_PART_HIGH_PACK_QTY"] = pts
                        reasons.append(f"High Pack Qty ({qty:,.0f} pcs, +30pt)")
                    elif qty >= t.get("small_part_qty_threshold_medium", 1000):
                        pts = w.get("small_part_pack_qty_1000", 20)
                        risk_score += pts
                        score_breakdown["SMALL_PART_HIGH_PACK_QTY"] = pts
                        reasons.append(f"High Pack Qty ({qty:,.0f} pcs, +20pt)")
                    elif qty >= t.get("small_part_qty_threshold_small", 200):
                        pts = w.get("small_part_pack_qty_200", 10)
                        risk_score += pts
                        score_breakdown["SMALL_PART_MED_PACK_QTY"] = pts
                        reasons.append(f"Med Pack Qty ({qty:,.0f} pcs, +10pt)")

                    # 명시적 소물류 키워드(SCREW, AMING, AIMING, PAD 등) 추가 가산 (+15pt)
                    if is_small_part_keyword:
                        pts = w.get("small_part_keyword_bonus", 15)
                        risk_score += pts
                        score_breakdown["SMALL_PART_KEYWORD"] = pts

                elif "PLANT1_HAND_PICK_TIER_1_2" in risk_types:
                    pts = w.get("plant1_hand_pick_tier_1_2", 15)
                    risk_score += pts
                    score_breakdown["PLANT1_HAND_PICK_TIER_1_2"] = pts

                # 3. FIFO Inversion (Separated by Plant 1 vs Plant 3, only if not already scored by GI-verified)
                if "FIFO_INVERSION" in risk_types and "PROVEN_FIFO_BYPASS_GI_VERIFIED" not in risk_types:
                    if bin_info["is_plant1"]:
                        pts = w.get("fifo_inversion_plant1", 30)
                        risk_score += pts
                        score_breakdown["FIFO_INVERSION_PLANT1"] = pts
                        reasons.append("FIFO Inversion (Stock)")
                    else:
                        pts = w.get("fifo_inversion_plant3", 15)
                        risk_score += pts
                        score_breakdown["FIFO_INVERSION_PLANT3"] = pts
                        reasons.append("FIFO Inversion (Stock)")
                    if action_type == "Physical Audit":
                        action_type = "FIFO Review"

                # 4. Scaled Turnover Anomaly (D102: CRT/PRO/SMT는 bin_dwell_days 사용)
                if std_turnover and effective_dwell_days and std_turnover > 0:
                    ratio = effective_dwell_days / std_turnover
                    if ratio >= t.get("turnover_extreme_multiplier", 10.0):
                        pts = w.get("turnover_anomaly_extreme", 45)
                        risk_score += pts
                        score_breakdown["TURNOVER_ANOMALY_EXTREME"] = pts
                        reasons.append("Turnover Anomaly")
                    elif ratio >= t.get("turnover_high_multiplier", 5.0):
                        pts = w.get("turnover_anomaly_high", 30)
                        risk_score += pts
                        score_breakdown["TURNOVER_ANOMALY_HIGH"] = pts
                        reasons.append("Turnover Anomaly")
                    elif ratio >= t.get("turnover_moderate_multiplier", 3.0) and effective_dwell_days > t.get("turnover_moderate_min_dwell_days", 14):
                        pts = w.get("turnover_anomaly_moderate", 15)
                        risk_score += pts
                        score_breakdown["TURNOVER_ANOMALY_MODERATE"] = pts
                        reasons.append("Turnover Anomaly")

                # 5. Missing GI Spike & Deficit Anomaly (WMS 주간 결손 학습 모델 연계)
                if mgi_info:
                    mgi_class = mgi_info.get("classification")
                    anomaly_boxes = mgi_info.get("anomaly_diff_box", 0.0)
                    is_unit_offset = mgi_info.get("is_unit_offset_item", False)
                    target_bins_str = str(mgi_info.get("target_bins", ""))

                    if is_unit_offset:
                        # 상시 포장단위 편차 품목: 가짜 결손 오탐 방지 (Ghost Stock 감점/면제)
                        pass
                    elif mgi_class == "SPORADIC_VOLATILE_DEFICIT" or anomaly_boxes >= 2.0:
                        # 급성 돌발 결손 또는 2박스 이상 이상치 결손: 유령재고/오스캔 진성 의심군 (+20pt)
                        pts = w.get("missing_gi_spike", 20)
                        risk_score += pts
                        score_breakdown["MISSING_GI_SPIKE"] = pts
                        reasons.append(f"Missing GI Spike ({anomaly_boxes:+.1f} boxes, +{pts}pt)")
                        risk_types.append("MISSING_GI_SPIKE")
                    elif mgi_class == "CHRONIC_UNSCANNED_LINE_ISSUE":
                        # 만성 무스캔 라인 직투입 품목: 현장 라인 공정 누수 관리용
                        reasons.append("Line Direct Unscanned Item")

                    # WMS Missing GI가 지정한 핵심 보관 빈(#1~#9)과 일치 시 추가 가산 (+10pt)
                    if raw_bin and raw_bin in target_bins_str:
                        pts = w.get("missing_gi_target_bin_match", 10)
                        risk_score += pts
                        score_breakdown["MISSING_GI_TARGET_BIN_MATCH"] = pts
                        reasons.append(f"Missing GI Target Bin Match (+{pts}pt)")

            # Obsolete / Dead Stock (최근 45일간 입고/출고 전무 단종·부동 품목)
            # Daily 모드에서는 생산 소모가 없는 부동 자재가 15분 긴급 실사 목록을 독점하지 않도록 상한 20점으로 제한
            # Weekly 모드에서도 점수 상한을 35점으로 제한하여 양산 활성 품목을 추월하지 못하도록 관리
            if is_dead_stock:
                if mode.lower() == "daily":
                    cap = w.get("obsolete_dead_stock_cap_daily", 20)
                    pre_cap = risk_score
                    risk_score = min(risk_score, cap)
                    if risk_score < pre_cap:
                        score_breakdown["OBSOLETE_DEAD_STOCK_CAP"] = risk_score - pre_cap
                    action_type = "Obsolete Stock Check"
                    reasons = [f"Dead Stock ({days_inactive}d Inactive)"]
                else:
                    cap = w.get("obsolete_dead_stock_cap_weekly", 35)
                    pre_cap = risk_score
                    risk_score = min(risk_score, cap)
                    if risk_score < pre_cap:
                        score_breakdown["OBSOLETE_DEAD_STOCK_CAP"] = risk_score - pre_cap
                    action_type = "Obsolete Stock Review"
                    reasons.append(f"Dead Stock ({days_inactive}d Inactive)")
            elif is_slow_stock:
                # Slow Stock 우선순위 감점 (Turnover Days 및 출고 빈도 기반 슬로우 스톡 디스카운트)
                # 회전이 극히 느린 슬로우 스톡은 일상 양산 라인에 즉각적인 결품/중단 충격을 주지 않으므로,
                # Daily 및 Weekly 실사 모두에서 활성 양산 품목(Fast Mover, Normal)에 우선권을 양보하도록 점수 감점 (-30점)
                slow_discount = w.get("slow_stock_demote", 30)
                min_score = w.get("slow_stock_min_score", 10)
                pre_disc = risk_score
                risk_score = max(min_score, risk_score - slow_discount)
                applied_discount = risk_score - pre_disc  # negative number
                score_breakdown["SLOW_STOCK_DEMOTE"] = applied_discount
                reasons.append(f"Slow Stock (-{slow_discount}pt)")

            # 6. ZTCODE_STOCK_REC Chronic System Mismatch (04_JOB_SystemReconciliation 만성 불일치 연계)
            ztcode_stock_rec_diff_qty = 0.0
            ztcode_stock_rec_consecutive_days = 0
            ztcode_stock_rec_exposure = 0.0
            chronic_info = self.chronic_feed.get(mat_no) or self.chronic_feed.get(mat_clean)
            if chronic_info:
                ztcode_stock_rec_diff_qty = float(chronic_info.get("current_diff_qty", 0.0))
                ztcode_stock_rec_consecutive_days = int(chronic_info.get("consecutive_days", 0))
                ztcode_stock_rec_exposure = float(chronic_info.get("current_financial_exposure", 0.0))

                risk_score += chronic_points
                score_breakdown["CHRONIC_SYSTEM_MISMATCH"] = chronic_points
                risk_types.append("CHRONIC_SYSTEM_MISMATCH")
                reasons.append(f"Chronic System Mismatch ({ztcode_stock_rec_consecutive_days}d, ${ztcode_stock_rec_exposure:,.0f}, {ztcode_stock_rec_diff_qty:+.0f})")

            # 7. GR Chain Gap Feature (D90-3 Amended by Thread 146: Shadow weight = 0 initially)
            chain_gap_flag = 0
            chain_gap_days = 0
            cg_label = ""
            cg_info = getattr(self, "chain_gap_feed", {}).get(mat_no) or getattr(self, "chain_gap_feed", {}).get(mat_clean)
            if cg_info:
                chain_gap_flag = 1
                chain_gap_days = int(cg_info.get("consecutive_days", 1) or 1)
                cg_pts = int(w.get("gr_chain_gap", 0) or 0)
                cg_label = f"GR Chain Gap ({chain_gap_days}d)" if chain_gap_days > 1 else "GR Chain Gap"
                if cg_pts > 0:
                    risk_score += cg_pts
                    score_breakdown["GR_CHAIN_GAP"] = cg_pts
                    risk_types.append("GR_CHAIN_GAP")
                    reasons.append(cg_label)
                elif risk_types:
                    # Shadow weight = 0: do not create standalone 0-score suspects; annotate existing suspects only
                    score_breakdown["GR_CHAIN_GAP"] = 0
                    risk_types.append("GR_CHAIN_GAP")
                    reasons.append(cg_label)

            # -----------------------------------------------------------------
            # Shadow Expected Value (EV) Metric Calculation (Notion EV Framework)
            # Priority_EV = Expected_Loss / Est_Minutes
            # Expected_Loss = P(Error) * (Qty * Net_Price)
            # -----------------------------------------------------------------
            # 1. p_error_proxy: Monotonic logistic transformation of rule risk score
            # PLACEHOLDER: Temporary proxy mapping rule risk score to probability [0, 1].
            # Will be replaced by calibrated ML classification model P(discrepancy | x).
            log_k = float(shadow_cfg.get("logistic_k", 0.05))
            log_x0 = float(shadow_cfg.get("logistic_x0", 50.0))
            p_floor = float(shadow_cfg.get("min_p", 0.01))
            p_ceil = float(shadow_cfg.get("max_p", 0.99))
            z_val = max(-50.0, min(50.0, -log_k * (risk_score - log_x0)))
            p_error_proxy = round(max(p_floor, min(p_ceil, 1.0 / (1.0 + math.exp(z_val)))), 4)

            # 2. net_price: Read-only lookup from 04_JOB accuracy_material_snapshot (net_price / price_unit)
            net_price = self.net_prices.get(mat_no) or self.net_prices.get(mat_clean)
            if net_price is not None and net_price <= 0:
                net_price = None

            # 3. expected_loss: p_error_proxy * (Qty * net_price)
            if net_price is not None:
                expected_loss = round(p_error_proxy * (float(qty or 0.0) * net_price), 2)
            else:
                expected_loss = None

            # 4. est_minutes: Default cycle count duration by bin characteristic
            # PLACEHOLDER: Fixed domain estimates by bin type. Will be replaced by empirical stopwatch measurements.
            est_mins_map = shadow_cfg.get("est_minutes", {})
            raw_bin_up = raw_bin.upper()
            if bin_info["is_repack_cart"] or raw_bin_up.startswith(("CRT", "DOLLY")):
                est_minutes = float(est_mins_map.get("repack", 1.0))
                bin_type_name = "Repack"
            elif bin_info["is_supermarket"] or raw_bin_up.startswith(("SMT",) + OEM_FLOOR_PREFIXES):
                est_minutes = float(est_mins_map.get("smt", 1.5))
                bin_type_name = resolve_non_smt_floor_label(raw_bin_up) if (bin_info.get("is_oem_floor") or (not raw_bin_up.startswith(("SMT", "SMD")) and raw_bin_up.startswith(OEM_FLOOR_PREFIXES))) else "SMT"
            elif bin_info["is_plant3"] or (bin_info["tier"] is not None and bin_info["tier"] >= 3):
                est_minutes = float(est_mins_map.get("high_rack", 5.0))
                bin_type_name = "High-Rack"
            elif bin_info["tier"] in [1, 2] or bin_info["is_hand_picking_tier"]:
                est_minutes = float(est_mins_map.get("tier_1_2", 2.0))
                bin_type_name = "Tier 1-2"
            else:
                est_minutes = float(est_mins_map.get("default_floor", 2.0))
                bin_type_name = "Floor"

            # 5. priority_ev: expected_loss / est_minutes
            if expected_loss is not None and est_minutes > 0:
                priority_ev = round(expected_loss / est_minutes, 2)
            else:
                priority_ev = None

            if risk_types and reasons:
                dedup_reasons = list(dict.fromkeys(reasons))
                top_reasons = dedup_reasons[:2]
                if chain_gap_flag and cg_label and cg_label not in top_reasons:
                    top_reasons.append(cg_label)
                results.append({
                    "Risk_Score": risk_score,
                    "Action_Type": action_type,
                    "Barcode": barcode,
                    "Mat_No": mat_no,
                    "Description": desc,
                    "BIN": bin_str,
                    "Plant": "Plant 3" if bin_info["is_plant3"] else "Plant 1",
                    "Tier": bin_info["tier"] or 0,
                    "Qty": qty,
                    "GR_Date": str(gr_date) if gr_date else "",
                    "Warehouse_Dwell_Days": wh_dwell_days,
                    "Bin_Dwell_Days": bin_dwell_days,
                    "Lot_No": lot_str,
                    "Lot_Date": str(lot_date) if lot_date else "",
                    "Lot_Age_Days": lot_age_days or 0,
                    "Ontology_Turnover_Days": std_turnover or 0.0,
                    "Mover_Class": mover_class or "UNKNOWN",
                    "Missing_GI_Flag": diff_flag or "BALANCED",
                    "Missing_GI_Diff_Box": diff_box,
                    "Latest_Issued_GR_Date": str(latest_issued_gr_date) if latest_issued_gr_date else "",
                    "FIFO_Bypass_Gap_Days": fifo_bypass_gap_days,
                    "Same_Bin_Lot_Spread_Days": bin_lot_spread_days,
                    "Risk_Categories": ", ".join(risk_types),
                    "Primary_Reason": " / ".join(top_reasons),
                    "Score_Breakdown": score_breakdown,
                    "ZTCODE_STOCK_REC_Diff_Qty": ztcode_stock_rec_diff_qty,
                    "ZTCODE_STOCK_REC_Consecutive_Days": ztcode_stock_rec_consecutive_days,
                    "ZTCODE_STOCK_REC_Exposure": ztcode_stock_rec_exposure,
                    "Has_Chronic": (chronic_info is not None),
                    "Chain_Gap_Flag": chain_gap_flag,
                    "Chain_Gap_Days": chain_gap_days,
                    "Net_Price": net_price,
                    "P_Error_Proxy": p_error_proxy,
                    "Expected_Loss": expected_loss,
                    "Est_Minutes": est_minutes,
                    "Priority_EV": priority_ev,
                    "Bin_Type": bin_type_name
                })

        # Build full bin -> sorted WMS barcodes & full parts map for D99 cooldown and D101 full-bin label
        full_bin_barcodes_map: Dict[str, List[str]] = {}
        full_bin_parts_map: Dict[str, Set[str]] = {}
        full_bin_qty_map: Dict[str, float] = {}
        small_part_188_materials: Set[str] = set()
        if not df.empty and "BIN" in df.columns:
            for _, raw_r in df.iterrows():
                rb_str = str(raw_r.get("BIN", "")).strip()
                if not rb_str:
                    continue
                rbc_str = str(raw_r.get("Barcode", "")).strip() if "Barcode" in df.columns else ""
                rmat_str = str(raw_r.get("Mat.No", "")).strip() if "Mat.No" in df.columns else ""
                if rbc_str and rbc_str.lower() != "nan":
                    full_bin_barcodes_map.setdefault(rb_str, []).append(rbc_str)
                if rmat_str and rmat_str.lower() != "nan":
                    full_bin_parts_map.setdefault(rb_str, set()).add(rmat_str)
                rqty_val = float(raw_r.get("Qty.", 0) or 0) if "Qty." in df.columns else 0.0
                full_bin_qty_map[rb_str] = full_bin_qty_map.get(rb_str, 0.0) + rqty_val
                rdesc_str = str(raw_r.get("Mat.Desc", "")).strip().upper() if "Mat.Desc" in df.columns else ""
                rb_char = get_bin_characteristics(rb_str)
                if not rb_char["is_non_physical_or_excluded"]:
                    _rb_tier = rb_char.get("tier") or 0
                    _is_p1_f = rb_char["is_plant1"] and ((_rb_tier in [0, 1, 2]) or rb_char["is_supermarket"] or rb_char["is_repack_cart"] or rb_str.upper().startswith("DOLLY"))
                    if (_is_p1_f or mode.lower() == "weekly") and rmat_str and rmat_str.lower() != "nan":
                        if is_small_part_for_total_pcs(rmat_str, rdesc_str, rqty_val):
                            small_part_188_materials.add(rmat_str)
            for k_b in list(full_bin_barcodes_map.keys()):
                full_bin_barcodes_map[k_b] = sorted(list(set(full_bin_barcodes_map[k_b])))

        # D99 Cooldown evaluation (Daily mode only; Random 3 slots exempt per D99-3)
        cooldown_excluded_bins: Dict[str, str] = {}
        cooldown_excluded_details: List[Dict[str, Any]] = []
        cooldown_returned_details: List[Dict[str, Any]] = []
        open_mismatches: List[Dict[str, Any]] = []
        field_instructions: List[str] = []

        if mode.lower() != "weekly":
            target_r_date = run_date or datetime.date.today().strftime("%Y-%m-%d")
            fi_override = getattr(self, "field_instructions_override", None)
            if fi_override is not None:
                field_instructions = list(fi_override)[:5]
            else:
                try:
                    field_instructions = get_daily_field_instructions(
                        target_date_str=target_r_date,
                        max_lines=5,
                        db_path=getattr(self, "db_path", None),
                    )
                except Exception as e_fi:
                    logger.warning(f"[D112 FieldInstructions] Failed to load field instructions: {e_fi}")
                    field_instructions = []
            cd_hist = getattr(self, "cooldown_history", None)
            if cd_hist is None:
                try:
                    cd_hist = get_audit_cooldown_history(
                        target_date_str=target_r_date,
                        max_business_days=3,
                        db_path=getattr(self, "db_path", None)
                    )
                except Exception as e_cd:
                    logger.warning(f"[D99 Cooldown] Failed to load cooldown history: {e_cd}")
                    cd_hist = {}

            for cd_bin, cd_info in cd_hist.items():
                curr_bcs = set(full_bin_barcodes_map.get(cd_bin, []))
                aud_bcs = set(cd_info.get("audited_barcodes") or set())
                curr_total_pcs = float(full_bin_qty_map.get(cd_bin, 0.0))
                raw_aud_pcs = cd_info.get("audited_total_pcs")
                aud_total_pcs = float(raw_aud_pcs) if raw_aud_pcs is not None else None

                bcs_unchanged = (curr_bcs == aud_bcs) and bool(aud_bcs or not curr_bcs)
                pcs_unchanged = (aud_total_pcs is None) or (abs(curr_total_pcs - aud_total_pcs) < 1e-6)
                is_unchanged = bcs_unchanged and pcs_unchanged

                is_disc = int(cd_info.get("is_discrepancy") or 0)
                adj_status = str(cd_info.get("wms_adjustment_status") or "NOT_ADJUSTED").strip().upper()
                el_days = int(cd_info.get("elapsed_business_days") or 1)
                a_date = str(cd_info.get("audit_date") or "")
                lbl_q = int(round(float(cd_info.get("label_qty") or 0)))
                raw_cnt_q = cd_info.get("counted_qty")
                cnt_q = int(round(float(raw_cnt_q))) if raw_cnt_q is not None else None
                p_nums = str(cd_info.get("part_numbers") or "")

                if is_unchanged:
                    if is_disc == 0 or adj_status == "ADJUSTED":
                        reason_tag = "COOLDOWN_ADJUSTED_MATCH" if (is_disc == 1 and adj_status == "ADJUSTED") else "COOLDOWN_MATCH"
                        reason_str = f"{reason_tag} ({a_date}, {el_days}d, unchanged)"
                        cooldown_excluded_bins[cd_bin] = reason_str
                        cooldown_excluded_details.append({
                            "BIN": cd_bin,
                            "Type": "ADJUSTED_MATCH" if reason_tag == "COOLDOWN_ADJUSTED_MATCH" else "MATCH",
                            "Audit_Date": a_date,
                            "Elapsed_Days": el_days,
                            "Reason": reason_str
                        })
                    else:
                        cnt_disp = str(cnt_q) if cnt_q is not None else "PCS Mismatch"
                        reason_str = f"COOLDOWN_OPEN_MISMATCH ({a_date}, {el_days}d, {lbl_q}->{cnt_disp})"
                        cooldown_excluded_bins[cd_bin] = reason_str
                        cooldown_excluded_details.append({
                            "BIN": cd_bin,
                            "Type": "OPEN_MISMATCH",
                            "Audit_Date": a_date,
                            "Elapsed_Days": el_days,
                            "Reason": reason_str
                        })
                        open_mismatches.append({
                            "Bin": cd_bin,
                            "Part #": p_nums,
                            "Label QTY": lbl_q,
                            "Actual QTY": cnt_q if cnt_q is not None else "PCS Mismatch",
                            "Diff QTY": (cnt_q - lbl_q) if cnt_q is not None else "",
                            "Audit Date": a_date,
                            "Elapsed Days": el_days,
                            "Status": "Awaiting WMS Adjustment / Movement"
                        })
                else:
                    if bcs_unchanged and not pcs_unchanged and aud_total_pcs is not None:
                        change_desc = f"PCS adjusted ({aud_total_pcs:g}->{curr_total_pcs:g} pcs, {len(curr_bcs)} boxes)"
                    else:
                        added_cnt = len(curr_bcs - aud_bcs)
                        removed_cnt = len(aud_bcs - curr_bcs)
                        change_desc = f"+{added_cnt}/-{removed_cnt} boxes ({len(aud_bcs)}->{len(curr_bcs)})"
                    cooldown_returned_details.append({
                        "BIN": cd_bin,
                        "Prev_Type": "MISMATCH" if is_disc == 1 else "MATCH",
                        "Audit_Date": a_date,
                        "Elapsed_Days": el_days,
                        "Change": change_desc
                    })

            open_mismatches.sort(key=lambda x: (x["Audit Date"], x["Bin"]), reverse=True)

        # Annotate individual barcode results with D99-4 cooldown flags before building suspects_df
        for item in results:
            b_it = item["BIN"]
            if b_it in cooldown_excluded_bins:
                item["Cooldown_Excluded"] = 1
                item["Cooldown_Reason"] = cooldown_excluded_bins[b_it]
            else:
                item["Cooldown_Excluded"] = 0
                item["Cooldown_Reason"] = ""

        # Sort individual barcodes by highest risk score, then warehouse dwell days
        results.sort(key=lambda x: (x["Risk_Score"], x["Warehouse_Dwell_Days"]), reverse=True)
        suspects_df = pd.DataFrame(results)

        # ----------------- Bin-Level Deduplication & Aggregation -----------------
        bin_groups = {}
        for item in results:
            b = item["BIN"]
            if b not in bin_groups:
                bin_groups[b] = {
                    "BIN": b,
                    "Plant": item["Plant"],
                    "Tier": item["Tier"],
                    "Bin_Type": item.get("Bin_Type", "Floor"),
                    "max_risk_score": item["Risk_Score"],
                    "max_wh_dwell": item["Warehouse_Dwell_Days"],
                    "max_bin_dwell": item["Bin_Dwell_Days"],
                    "barcodes": [],
                    "part_numbers": set(),
                    "descriptions": set(),
                    "total_qty": 0.0,
                    "actions": set(),
                    "reasons": [],
                    "has_chronic": False,
                    "max_chronic_exposure": 0.0,
                    "chronic_materials": set(),
                    "has_chain_gap": False,
                    "chain_gap_days": 0,
                    "items": []
                }
            else:
                bin_groups[b]["max_risk_score"] = max(bin_groups[b]["max_risk_score"], item["Risk_Score"])
                bin_groups[b]["max_wh_dwell"] = max(bin_groups[b]["max_wh_dwell"], item["Warehouse_Dwell_Days"])
                bin_groups[b]["max_bin_dwell"] = max(bin_groups[b]["max_bin_dwell"], item["Bin_Dwell_Days"])

            bin_groups[b]["barcodes"].append(item["Barcode"])
            bin_groups[b]["part_numbers"].add(item["Mat_No"])
            bin_groups[b]["descriptions"].add(item["Description"])
            bin_groups[b]["total_qty"] += item["Qty"]
            bin_groups[b]["actions"].add(item["Action_Type"])
            bin_groups[b]["items"].append(item)
            for r_item in item["Primary_Reason"].split(" / "):
                if r_item not in bin_groups[b]["reasons"]:
                    bin_groups[b]["reasons"].append(r_item)

            if item.get("Has_Chronic"):
                bin_groups[b]["has_chronic"] = True
                bin_groups[b]["max_chronic_exposure"] = max(
                    bin_groups[b]["max_chronic_exposure"],
                    float(item.get("ZTCODE_STOCK_REC_Exposure", 0.0))
                )
                bin_groups[b]["chronic_materials"].add(item["Mat_No"])

            if item.get("Chain_Gap_Flag"):
                bin_groups[b]["has_chain_gap"] = True
                bin_groups[b]["chain_gap_days"] = max(
                    bin_groups[b]["chain_gap_days"],
                    int(item.get("Chain_Gap_Days", 1) or 1)
                )

        # Format aggregated bin items
        aggregated_bins = []
        for b, g in bin_groups.items():
            b_str = str(b).strip().upper()
            # Exclude BINSYNTH9953 non-physical, EXT_TRL trailer/staging, D external, CKD, and ^S\d+ bins
            if (
                not b_str or 
                b_str in ("NAN", "BINSYNTH9953") or 
                b_str.startswith(("EXT_TRL", "D", "CKD")) or
                bool(re.match(r"^S\d+", b_str))
            ):
                continue

            # Action priority resolution (Ghost Stock Audit is top priority)
            if b_str.startswith(("CRT", "PRO")):
                primary_action = "Cart PCS Check"
            elif "Ghost Stock Audit" in g["actions"] or "Phantom Box Audit" in g["actions"]:
                primary_action = "Ghost Stock Audit"
            elif "FIFO Bypass Audit" in g["actions"]:
                primary_action = "FIFO Bypass Audit"
            elif "Fast Mover Audit" in g["actions"]:
                primary_action = "Fast Mover Audit"
            elif "High-Rack Audit" in g["actions"]:
                primary_action = "High-Rack Audit"
            elif "Box Presence Check" in g["actions"]:
                primary_action = "Box Presence Check"
            elif "Bin FIFO Sort" in g["actions"]:
                primary_action = "Bin FIFO Sort"
            elif "Rear Pallet Check" in g["actions"]:
                primary_action = "Rear Pallet Check"
            elif "Packaging Unit Check" in g["actions"]:
                primary_action = "Packaging Unit Check"
            elif "SMT Audit" in g["actions"]:
                primary_action = "SMT Audit"
            elif "Repack Cart Audit" in g["actions"]:
                primary_action = "Repack Cart Audit"
            elif "Turnover Audit" in g["actions"]:
                primary_action = "Turnover Audit"
            elif "FIFO Review" in g["actions"]:
                primary_action = "FIFO Review"
            else:
                primary_action = list(g["actions"])[0] if g["actions"] else "Physical Audit"

            # Concise reason: top 2 unique reasons (+ GR Chain Gap display when present per D90-3)
            base_reasons = list(g["reasons"][:2])
            if g.get("has_chain_gap") and not any("GR Chain Gap" in r for r in base_reasons):
                cg_bin_lbl = f"GR Chain Gap ({g['chain_gap_days']}d)" if g.get("chain_gap_days", 0) > 1 else "GR Chain Gap"
                base_reasons.append(cg_bin_lbl)
            simple_reason = " / ".join(base_reasons) if base_reasons else "Audit Required"

            # Bin-Level Shadow EV Calculation
            bin_char = get_bin_characteristics(b)
            est_mins_map = shadow_cfg.get("est_minutes", {})
            if bin_char["is_repack_cart"] or b_str.startswith(("CRT", "DOLLY")):
                bin_est_minutes = float(est_mins_map.get("repack", 1.0))
                bin_type_name = "Repack"
            elif bin_char["is_supermarket"] or b_str.startswith(("SMT",) + OEM_FLOOR_PREFIXES):
                bin_est_minutes = float(est_mins_map.get("smt", 1.5))
                bin_type_name = resolve_non_smt_floor_label(b_str) if (bin_char.get("is_oem_floor") or (not b_str.startswith(("SMT", "SMD")) and b_str.startswith(OEM_FLOOR_PREFIXES))) else "SMT"
            elif bin_char["is_plant3"] or (g["Tier"] is not None and g["Tier"] >= 3):
                bin_est_minutes = float(est_mins_map.get("high_rack", 5.0))
                bin_type_name = "High-Rack"
            elif g["Tier"] in [1, 2] or bin_char["is_hand_picking_tier"]:
                bin_est_minutes = float(est_mins_map.get("tier_1_2", 2.0))
                bin_type_name = "Tier 1-2"
            else:
                bin_est_minutes = float(est_mins_map.get("default_floor", 2.0))
                bin_type_name = "Floor"

            z_b = max(-50.0, min(50.0, -log_k * (g["max_risk_score"] - log_x0)))
            bin_p_error = round(max(p_floor, min(p_ceil, 1.0 / (1.0 + math.exp(z_b)))), 4)

            priced_items = [it for it in g["items"] if it.get("Net_Price") is not None]
            if priced_items:
                bin_val = round(sum(float(it.get("Qty", 0.0) or 0.0) * float(it["Net_Price"]) for it in priced_items), 2)
                bin_exp_loss = round(bin_p_error * bin_val, 2)
                bin_prio_ev = round(bin_exp_loss / bin_est_minutes, 2) if bin_est_minutes > 0 else 0.0
            else:
                bin_val = None
                bin_exp_loss = None
                bin_prio_ev = None

            all_bin_bcs = full_bin_barcodes_map.get(b, g["barcodes"])
            all_bin_parts = sorted(list(full_bin_parts_map.get(b, g["part_numbers"])))
            tot_qty_full = float(full_bin_qty_map.get(b, g["total_qty"]))
            show_total_pcs = bool(b_str.startswith(("CRT", "PRO")) or any(p in small_part_188_materials for p in all_bin_parts))
            total_pcs_disp = int(round(tot_qty_full)) if show_total_pcs else ""
            aggregated_bins.append({
                "BIN": b,
                "Plant": g["Plant"],
                "Tier": g["Tier"],
                "Bin_Type": bin_type_name,
                "Risk_Score": g["max_risk_score"],
                "Label_QTY": len(all_bin_bcs),
                "Suspect_Barcode_Count": len(g["barcodes"]),
                "Parts_List": ", ".join(all_bin_parts),
                "Part_Count": len(all_bin_parts),
                "Label_Qty_Basis": "ALL_BIN_BARCODES",
                "Action_Required": primary_action,
                "Reason": simple_reason,
                "Warehouse_Dwell_Days": g["max_wh_dwell"],
                "Bin_Dwell_Days": g["max_bin_dwell"],
                "Total_Qty": tot_qty_full,
                "Show_Total_PCS": show_total_pcs,
                "Total_PCS": total_pcs_disp,
                "Barcodes": g["barcodes"],
                "WMS_Barcodes": all_bin_bcs,
                "Cooldown_Excluded": 1 if b in cooldown_excluded_bins else 0,
                "Cooldown_Reason": cooldown_excluded_bins.get(b, ""),
                "has_chronic": g["has_chronic"],
                "max_chronic_exposure": g["max_chronic_exposure"],
                "chronic_materials": sorted(list(g["chronic_materials"])),
                "P_Error_Proxy": bin_p_error,
                "Est_Bin_Value": bin_val,
                "Expected_Loss": bin_exp_loss,
                "Est_Minutes": bin_est_minutes,
                "Priority_EV": bin_prio_ev,
                "selection_method": "RULE_SCORE",
                "list_type": "TOP"
            })

        # Sort Bins by highest risk score, then barcode count
        aggregated_bins.sort(key=lambda x: (x["Risk_Score"], x["Label_QTY"]), reverse=True)
        # Exclude BINSYNTH9953 non-physical, EXT_TRL trailer/staging, D external, CKD, and ^S\d+ bins
        physical_bins = [
            b for b in aggregated_bins 
            if not get_bin_characteristics(b.get("BIN", ""))["is_non_physical_or_excluded"]
        ]

        # Record rule-based rank for each physical bin
        for r_idx, b in enumerate(physical_bins, 1):
            b["Rule_Rank"] = r_idx

        # Compute Shadow EV Top 15 Ranking (Notion EV Framework)
        shadow_sorted = sorted(
            physical_bins,
            key=lambda x: (x.get("Priority_EV") is not None, x.get("Priority_EV") or 0.0, x["Risk_Score"]),
            reverse=True
        )
        shadow_ev_top15 = []
        for s_rank, b in enumerate(shadow_sorted[:15], 1):
            rule_r = b.get("Rule_Rank", s_rank)
            shift = rule_r - s_rank
            shift_str = f"+{shift}" if shift > 0 else str(shift)
            shadow_ev_top15.append({
                "Shadow_Rank": s_rank,
                "Rule_Rank": rule_r,
                "Rank_Shift": shift_str,
                "BIN": b["BIN"],
                "Plant": b["Plant"],
                "Tier": b["Tier"],
                "Bin_Type": b.get("Bin_Type", "Floor"),
                "Parts_List": b["Parts_List"],
                "Part_Count": b["Part_Count"],
                "Label_QTY": b["Label_QTY"],
                "Total_Qty": b["Total_Qty"],
                "Rule_Risk_Score": b["Risk_Score"],
                "P_Error_Proxy": b.get("P_Error_Proxy"),
                "Est_Bin_Value": b.get("Est_Bin_Value"),
                "Expected_Loss": b.get("Expected_Loss"),
                "Est_Minutes": b.get("Est_Minutes"),
                "Priority_EV": b.get("Priority_EV"),
                "Action_Required": b["Action_Required"],
                "Reason": b["Reason"]
            })

        forced_weekly_bins: List[Dict[str, Any]] = []
        if mode.lower() == "weekly":
            # #199 Item 3: Forced top-pinned Weekly SAP GR verification bins (SL00 & SL01, separate from 50-bin cap)
            f_spec = load_forced_weekly_gr_check_spec(
                run_date=run_date,
                spec_override=forced_weekly_spec,
                gi_resolution_logs_dir=gi_resolution_logs_dir,
            )
            if f_spec.get("enabled") and not df.empty and "BIN" in df.columns and "Mat.No" in df.columns:
                sloc_col = next((c for c in ("GI Loc", "SLoc", "SLOC") if c in df.columns), None)
                forced_bin_meta: Dict[str, Dict[str, Any]] = {}
                for _, f_row in df.iterrows():
                    f_mat = str(f_row.get("Mat.No", "")).strip()
                    if not f_mat or f_mat.lower() == "nan":
                        continue
                    f_mat_clean = f_mat.lstrip("0")
                    if f_mat not in f_spec["materials"] and f_mat_clean not in f_spec["materials_clean"]:
                        continue
                    if sloc_col is not None:
                        f_sloc = normalize_wms_sloc(f_row.get(sloc_col))
                        if f_sloc not in f_spec["slocs"]:
                            continue
                    elif forced_weekly_spec is not None and "SL01" in f_spec["slocs"]:
                        f_sloc = "SL01"
                    else:
                        continue
                    f_bin = str(f_row.get("BIN", "")).strip()
                    f_bchar = get_bin_characteristics(f_bin)
                    if f_bchar["is_non_physical_or_excluded"] or f_bchar["is_external"] or f_bchar["is_temporary_staging"]:
                        continue
                    f_wh_d = int(f_row.get("Warehouse_Dwell_Days", 0) or 0)
                    f_bin_d = int(f_row.get("Bin_Dwell_Days", 1) or 1)
                    if f_bin not in forced_bin_meta:
                        forced_bin_meta[f_bin] = {
                            "matched_mats": {f_mat},
                            "slocs": {f_sloc},
                            "max_wh_dwell": f_wh_d,
                            "max_bin_dwell": f_bin_d,
                        }
                    else:
                        forced_bin_meta[f_bin]["matched_mats"].add(f_mat)
                        forced_bin_meta[f_bin]["slocs"].add(f_sloc)
                        forced_bin_meta[f_bin]["max_wh_dwell"] = max(forced_bin_meta[f_bin]["max_wh_dwell"], f_wh_d)
                        forced_bin_meta[f_bin]["max_bin_dwell"] = max(forced_bin_meta[f_bin]["max_bin_dwell"], f_bin_d)

                phys_by_bin = {b["BIN"]: b for b in physical_bins}
                sorted_forced_bins = sorted(
                    forced_bin_meta.keys(),
                    key=lambda bk: (min(forced_bin_meta[bk]["matched_mats"]), bk),
                )
                for fb_code in sorted_forced_bins:
                    fb_meta = forced_bin_meta[fb_code]
                    all_fb_bcs = full_bin_barcodes_map.get(fb_code, [])
                    all_fb_parts = sorted(list(full_bin_parts_map.get(fb_code, fb_meta["matched_mats"])))
                    tot_fb_qty = float(full_bin_qty_map.get(fb_code, 0.0))
                    if fb_code in phys_by_bin:
                        fb_entry = dict(phys_by_bin[fb_code])
                    else:
                        fb_char = get_bin_characteristics(fb_code)
                        fb_up = fb_code.strip().upper()
                        est_mins_map = shadow_cfg.get("est_minutes", {})
                        if fb_char["is_repack_cart"] or fb_up.startswith(("CRT", "DOLLY")):
                            fb_est_mins = float(est_mins_map.get("repack", 1.0))
                            fb_type_name = "Repack"
                        elif fb_char["is_supermarket"] or fb_up.startswith(("SMT",) + OEM_FLOOR_PREFIXES):
                            fb_est_mins = float(est_mins_map.get("smt", 1.5))
                            fb_type_name = resolve_non_smt_floor_label(fb_up) if (fb_char.get("is_oem_floor") or (not fb_up.startswith(("SMT", "SMD")) and fb_up.startswith(OEM_FLOOR_PREFIXES))) else "SMT"
                        elif fb_char["is_plant3"] or ((fb_char.get("tier") or 0) >= 3):
                            fb_est_mins = float(est_mins_map.get("high_rack", 5.0))
                            fb_type_name = "High-Rack"
                        elif (fb_char.get("tier") or 0) in [1, 2] or fb_char["is_hand_picking_tier"]:
                            fb_est_mins = float(est_mins_map.get("tier_1_2", 2.0))
                            fb_type_name = "Tier 1-2"
                        else:
                            fb_est_mins = float(est_mins_map.get("default_floor", 2.0))
                            fb_type_name = "Floor"
                        fb_entry = {
                            "BIN": fb_code,
                            "Plant": "Plant 3" if fb_char["is_plant3"] else "Plant 1",
                            "Tier": fb_char.get("tier") or 0,
                            "Bin_Type": fb_type_name,
                            "Risk_Score": 0,
                            "Suspect_Barcode_Count": len(all_fb_bcs),
                            "Warehouse_Dwell_Days": fb_meta["max_wh_dwell"],
                            "Bin_Dwell_Days": fb_meta["max_bin_dwell"],
                            "Barcodes": list(all_fb_bcs),
                            "Cooldown_Excluded": 0,
                            "Cooldown_Reason": "",
                            "has_chronic": False,
                            "max_chronic_exposure": 0.0,
                            "chronic_materials": [],
                            "P_Error_Proxy": None,
                            "Est_Bin_Value": None,
                            "Expected_Loss": None,
                            "Est_Minutes": fb_est_mins,
                            "Priority_EV": None,
                        }
                    fb_entry.update({
                        "Label_QTY": len(all_fb_bcs),
                        "Parts_List": ", ".join(all_fb_parts),
                        "Part_Count": len(all_fb_parts),
                        "Label_Qty_Basis": "ALL_BIN_BARCODES",
                        "Total_Qty": tot_fb_qty,
                        "Show_Total_PCS": True,
                        "Total_PCS": int(round(tot_fb_qty)),
                        "WMS_Barcodes": list(all_fb_bcs),
                        "Action_Required": f_spec["action_required"],
                        "Reason": f_spec["reason"],
                        "Forced_Materials": sorted(list(fb_meta["matched_mats"])),
                        "Forced_SLocs": sorted(list(fb_meta["slocs"])),
                        "list_type": "FORCED_F_CHECK",
                        "selection_method": "FORCED_WEEKLY_GR_CHECK",
                    })
                    forced_weekly_bins.append(fb_entry)

            forced_bin_codes_set = {fb["BIN"] for fb in forced_weekly_bins}

            # Weekly: Deep Audit (50 Bins, separate from forced top-pinned bins)
            # Per D61, D62, D63:
            # - Plant 1 Floor bins (SMT, CRT, PRO, DOLLY, Tier 1, Tier 2, Tier 0) EXCLUDED (handled daily)
            # - Plant 1 High-Rack bins (Tier >= 3) INCLUDED
            # - Plant 3 ALL bins (R*, SCR*) INCLUDED (no floor concept in Plant 3)
            weekly_eligible_bins = []
            for b in physical_bins:
                if b.get("BIN") in forced_bin_codes_set:
                    continue
                b_code = str(b.get("BIN", "")).strip().upper()
                info = get_bin_characteristics(b_code)
                if info["is_plant3"]:
                    weekly_eligible_bins.append(b)
                elif info["is_plant1"]:
                    b_tier = info.get("tier") or 0
                    is_p1_floor = (b_tier in [1, 2]) or b_code.startswith((("SMT", "CRT", "PRO", "DOLLY") + OEM_FLOOR_PREFIXES)) or (b_tier == 0)
                    if not is_p1_floor and b_tier >= 3:
                        weekly_eligible_bins.append(b)

            if resolved_feed_mode == "reserved_slots":
                # Mode B: Reserve up to M slots for highest financial exposure chronic bins
                chronic_cands = [b for b in weekly_eligible_bins if b.get("has_chronic")]
                chronic_cands.sort(key=lambda x: (x.get("max_chronic_exposure", 0.0), x["Risk_Score"]), reverse=True)
                m_res = min(m_weekly, len(chronic_cands))
                reserved_bins = chronic_cands[:m_res]
                reserved_codes = {b["BIN"] for b in reserved_bins}
                for b in reserved_bins:
                    b["list_type"] = "CHRONIC_FEED"
                    b["selection_method"] = "CHRONIC_MISMATCH_FEED"
                    if "Chronic Mismatch" not in b["Action_Required"] and b["Action_Required"] != "Cart PCS Check":
                        b["Action_Required"] = f"Chronic Mismatch ({b['Action_Required']})"
                
                remaining = [b for b in weekly_eligible_bins if b["BIN"] not in reserved_codes]
                remaining.sort(key=lambda x: (x["Risk_Score"], x["Label_QTY"]), reverse=True)
                rule_top = remaining[:(weekly_top_slots - m_res)]
                for b in rule_top:
                    b["list_type"] = "TOP"
                    b["selection_method"] = "RULE_SCORE"
                top_bins = forced_weekly_bins + reserved_bins + rule_top
                extended_bins = []
            else:
                # Mode A: "feature_only" (Natural Unified Ranking)
                reg_top_bins = weekly_eligible_bins[:weekly_top_slots]
                extended_bins = []
                for b in reg_top_bins:
                    b["list_type"] = "TOP"
                    b["selection_method"] = "RULE_SCORE"
                top_bins = forced_weekly_bins + reg_top_bins
        else:
            # Daily: 15-Minute Rapid Floor Audit
            # 작업자가 15분 만에 도보로 즉시 실사할 수 있는 1·2층 핸드피킹 랙, SMT 슈퍼마켓, Repack 대차만 선별 (Plant 1 전용)
            # Tier >= 3인 상단 하이랙 및 Plant 3(R*, SCR*)은 Weekly Deep Audit으로 분리
            floor_walkable_bins = [
                b for b in physical_bins 
                if b["BIN"] not in cooldown_excluded_bins and
                   not (b["BIN"].startswith("R") or b["BIN"].startswith("SCR")) and
                   ((b.get("Tier") in [1, 2]) or 
                    b["BIN"].startswith("SMT") or 
                    b["BIN"].startswith("CRT") or 
                    b["BIN"].startswith("DOLLY") or 
                    b["BIN"].startswith(OEM_FLOOR_PREFIXES) or
                    (b.get("Tier") == 0))
            ]
            high_rack_bins = [
                b for b in physical_bins
                if b["BIN"] not in cooldown_excluded_bins and b not in floor_walkable_bins
            ]

            if resolved_feed_mode == "reserved_slots":
                # Mode B: Reserve up to K slots for floor-walkable chronic bins
                chronic_cands = [b for b in floor_walkable_bins if b.get("has_chronic")]
                chronic_cands.sort(key=lambda x: (x.get("max_chronic_exposure", 0.0), x["Risk_Score"]), reverse=True)
                k_res = min(k_daily, len(chronic_cands))
                reserved_bins = chronic_cands[:k_res]
                reserved_codes = {b["BIN"] for b in reserved_bins}
                for b in reserved_bins:
                    b["list_type"] = "CHRONIC_FEED"
                    b["selection_method"] = "CHRONIC_MISMATCH_FEED"
                    if "Chronic Mismatch" not in b["Action_Required"] and b["Action_Required"] != "Cart PCS Check":
                        b["Action_Required"] = f"Chronic Mismatch ({b['Action_Required']})"
                
                remaining = [b for b in floor_walkable_bins if b["BIN"] not in reserved_codes]
                remaining.sort(key=lambda x: (x["Risk_Score"], x["Label_QTY"]), reverse=True)
                rule_top = remaining[:(daily_top_slots - k_res)]
                for b in rule_top:
                    b["list_type"] = "TOP"
                    b["selection_method"] = "RULE_SCORE"
                top_bins = reserved_bins + rule_top

                extended_cands = remaining[(daily_top_slots - k_res):(daily_total_slots - k_res)]
                for b in extended_cands:
                    b["list_type"] = "EXTENDED"
                    b["selection_method"] = "RULE_SCORE"
                extended_bins = extended_cands
            else:
                # Mode A: "feature_only" (Natural Unified Ranking)
                top_bins = floor_walkable_bins[:daily_top_slots]
                extended_bins = floor_walkable_bins[daily_top_slots:daily_total_slots]
                if len(top_bins) < daily_top_slots and high_rack_bins:
                    needed = daily_top_slots - len(top_bins)
                    top_bins.extend(high_rack_bins[:needed])
                for b in top_bins:
                    b["list_type"] = "TOP"
                    b["selection_method"] = "RULE_SCORE"
                for b in extended_bins:
                    b["list_type"] = "EXTENDED"
                    b["selection_method"] = "RULE_SCORE"

        ghost_count = len([r for r in results if "CRITICAL_GHOST_SUSPECT" in r["Risk_Categories"] or "CRITICAL_PHANTOM_SUSPECT" in r["Risk_Categories"]])

        # Stratified Random Sampling (Selection bias correction & model validation)
        excluded_for_random = {b["BIN"] for b in top_bins} | {b["BIN"] for b in extended_bins}
        random_bins = self.select_stratified_random_bins(
            df=df,
            mode=mode,
            excluded_bins=excluded_for_random,
            sample_size=random_slots,
            run_date=run_date
        )

        # Zero-tolerance validation: ensure no EXT_TRL, CKD, D, ^S\d+, or non-physical bin is in top, extended, or random
        for cat_name, bin_list in [("top_bins", top_bins), ("extended_bins", extended_bins), ("random_bins", random_bins)]:
            for item in bin_list:
                b_code = str(item.get("BIN", "")).strip().upper()
                if b_code.startswith(("EXT_TRL", "D", "CKD")) or bool(re.match(r"^S\d+", b_code)) or b_code in ("", "NAN", "BINSYNTH9953"):
                    raise ValueError(f"CRITICAL GUARDRAIL VIOLATION: Excluded bin '{b_code}' found in {cat_name}!")
                if mode.lower() != "weekly" and (b_code.startswith("R") or b_code.startswith("SCR")):
                    raise ValueError(f"CRITICAL GUARDRAIL VIOLATION: Plant 3/SCR bin '{b_code}' found in Daily {cat_name}!")
                if mode.lower() == "weekly" and item.get("selection_method") != "FORCED_WEEKLY_GR_CHECK":
                    b_info = get_bin_characteristics(b_code)
                    if b_info["is_plant1"]:
                        b_tier = b_info.get("tier") or 0
                        is_p1_floor = (b_tier in [1, 2]) or b_code.startswith((("SMT", "CRT", "PRO", "DOLLY") + OEM_FLOOR_PREFIXES)) or (b_tier == 0)
                        if is_p1_floor:
                            raise ValueError(f"CRITICAL GUARDRAIL VIOLATION: Plant 1 floor bin '{b_code}' found in Weekly {cat_name}!")

        return {
            "mode": mode,
            "scoring_version": cfg.get("scoring_version", SCORING_VERSION),
            "config_hash": get_config_hash(),
            "total_barcodes": len(df),
            "total_suspects": len(results),
            "total_suspect_bins": len(aggregated_bins),
            "ghost_suspect_count": ghost_count,
            "phantom_suspect_count": ghost_count,  # backward compatibility alias
            "proven_fifo_bypass_count": len([r for r in results if "PROVEN_FIFO_BYPASS_GI_VERIFIED" in r["Risk_Categories"]]),
            "same_bin_obstruction_count": len([r for r in results if "SAME_BIN_FIFO_OBSTRUCTION" in r["Risk_Categories"]]),
            "supermarket_cart_count": len([r for r in results if "SMT_SUPERMARKET_BUFFER" in r["Risk_Categories"] or "REPACK_CART_DOLLY" in r["Risk_Categories"]]),
            "plant1_small_part_count": len([r for r in results if "PLANT1_HAND_PICK_SMALL_PART" in r["Risk_Categories"]]),
            "fifo_inversion_count": len([r for r in results if "FIFO_INVERSION" in r["Risk_Categories"]]),
            "turnover_anomaly_count": len([r for r in results if "TURNOVER_ANOMALY" in r["Risk_Categories"]]),
            "high_rack_count": len([r for r in results if "HIGH_RACK_AGING" in r["Risk_Categories"]]),
            "fast_mover_residual_count": len([r for r in results if "FAST_MOVER_RESIDUAL" in r["Risk_Categories"]]),
            "forced_weekly_bins": forced_weekly_bins,
            "top_bins": top_bins,
            "extended_bins": extended_bins,
            "random_bins": random_bins,
            "shadow_ev_ranking": shadow_ev_top15,
            "open_mismatches": open_mismatches,
            "field_instructions": field_instructions,
            "cooldown_excluded_bins": cooldown_excluded_details,
            "cooldown_returned_bins": cooldown_returned_details,
            "suspects": suspects_df
        }

    def select_stratified_random_bins(
        self,
        df: pd.DataFrame,
        mode: str = "Daily",
        excluded_bins: Optional[Set[str]] = None,
        sample_size: Optional[int] = None,
        seed: Optional[int] = None,
        run_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Stratified Random Sampling for unbiased model validation and exploration:
        - Daily: Constrained to floor-walkable bins (Tier 1-2, SMT, CRT, DOLLY, Tier 0)
        - Weekly: Entire physical warehouse bins (including High-Rack Tier 3+)
        - Strata: (Plant, Tier_Group)
        - Calculates inclusion_probability (pi_h = n_h / N_h) for inverse probability weighting (IPW)
        - Reproducible seed strategy based on run_date
        """
        import random
        from config import (
            RANDOM_SLOTS_DAILY,
            RANDOM_SLOTS_WEEKLY,
            RANDOM_SEED_STRATEGY,
            get_random_seed
        )

        is_weekly = (mode.lower() == "weekly")
        n_slots = sample_size if sample_size is not None else (RANDOM_SLOTS_WEEKLY if is_weekly else RANDOM_SLOTS_DAILY)
        if n_slots <= 0:
            return []

        resolved_seed = seed
        if resolved_seed is None:
            r_date = run_date or datetime.date.today().strftime("%Y-%m-%d")
            resolved_seed = get_random_seed(r_date, strategy=RANDOM_SEED_STRATEGY)

        rng = random.Random(resolved_seed)
        excluded = set(excluded_bins or [])

        # Build physical bin records from raw stock list df
        bin_records = {}
        small_part_rand_mats: Set[str] = set()
        for _, r_sp in df.iterrows():
            _rb_s = str(r_sp.get("BIN", "")).strip()
            if not _rb_s:
                continue
            _rb_c = get_bin_characteristics(_rb_s)
            if _rb_c["is_non_physical_or_excluded"]:
                continue
            _rt = _rb_c.get("tier") or 0
            _is_p1f = _rb_c["is_plant1"] and ((_rt in [0, 1, 2]) or _rb_c["is_supermarket"] or _rb_c["is_repack_cart"] or _rb_s.upper().startswith("DOLLY"))
            if _is_p1f or is_weekly:
                _rm = str(r_sp.get("Mat.No", "")).strip()
                _rd = str(r_sp.get("Mat.Desc", "")).strip().upper()
                _rq = float(r_sp.get("Qty.", 0) or 0)
                if _rm and _rm.lower() != "nan" and is_small_part_for_total_pcs(_rm, _rd, _rq):
                    small_part_rand_mats.add(_rm)
        for _, r in df.iterrows():
            b = str(r.get("BIN", "")).strip()
            b_up = b.upper()
            if not b or b_up in ("", "NAN", "BINSYNTH9953") or b_up.startswith(("EXT_TRL", "D", "CKD")) or bool(re.match(r"^S\d+", b_up)):
                continue
            if b in excluded:
                continue

            info = get_bin_characteristics(b)
            if info["is_non_physical_or_excluded"]:
                continue
            tier = info.get("tier") or 0
            is_floor = (tier in [1, 2]) or b_up.startswith((("SMT", "CRT", "PRO", "DOLLY") + OEM_FLOOR_PREFIXES)) or (tier == 0)

            # Physical constraint check (D61, D62, D63):
            if is_weekly:
                # Weekly: Plant 1 Floor excluded; Plant 1 Tier 3+ and Plant 3 all allowed
                if info["is_plant1"] and is_floor:
                    continue
            else:
                # Daily: Plant 1 Floor only (Plant 3 excluded)
                if not is_floor or info["is_plant3"]:
                    continue

            plant = "Plant 3" if info["is_plant3"] else "Plant 1"
            if tier == 0 or b_up.startswith((("SMT", "CRT", "DOLLY") + OEM_FLOOR_PREFIXES)):
                tier_grp = "Floor"
            elif tier in [1, 2]:
                tier_grp = "Tier 1-2"
            else:
                tier_grp = "Tier 3+"

            if b not in bin_records:
                bin_records[b] = {
                    "BIN": b,
                    "Plant": plant,
                    "Tier": tier,
                    "Tier_Group": tier_grp,
                    "boxes": 0,
                    "qty": 0.0,
                    "parts": set(),
                    "barcodes": [],
                    "wh_dwell": 0,
                    "bin_dwell": 1
                }
            bin_records[b]["boxes"] += 1
            bin_records[b]["qty"] += float(r.get("Qty.", 0) or 0)
            bc_val = str(r.get("Barcode", "")).strip()
            if bc_val and bc_val.lower() != "nan":
                bin_records[b]["barcodes"].append(bc_val)
            mat_val = str(r.get("Mat.No", "")).strip()
            if mat_val and mat_val.lower() != "nan":
                bin_records[b]["parts"].add(mat_val)
            bin_records[b]["wh_dwell"] = max(bin_records[b]["wh_dwell"], int(r.get("Warehouse_Dwell_Days", 0) or 0))
            bin_records[b]["bin_dwell"] = max(bin_records[b]["bin_dwell"], int(r.get("Bin_Dwell_Days", 1) or 1))

        if not bin_records:
            return []

        # Partition into strata
        strata = {}
        for b, meta in bin_records.items():
            key = (meta["Plant"], meta["Tier_Group"])
            strata.setdefault(key, []).append(b)

        strata_keys = sorted(strata.keys())
        allocations = {k: 0 for k in strata_keys}

        # Allocate sample slots across strata
        if n_slots >= len(strata_keys):
            for k in strata_keys:
                allocations[k] = 1
            rem = n_slots - len(strata_keys)
            tot_pop = sum(len(strata[k]) for k in strata_keys)
            remainders = []
            for k in strata_keys:
                exact_quota = rem * (len(strata[k]) / tot_pop)
                allocations[k] += int(exact_quota)
                remainders.append((exact_quota - int(exact_quota), k))
            remainders.sort(reverse=True)
            for i in range(rem - sum(int(rem * (len(strata[k]) / tot_pop)) for k in strata_keys)):
                allocations[remainders[i][1]] += 1
        else:
            sorted_by_pop = sorted(strata_keys, key=lambda k: len(strata[k]), reverse=True)
            for k in sorted_by_pop[:n_slots]:
                allocations[k] = 1

        # Draw samples and compute inclusion probabilities
        selected_samples = []
        for k in strata_keys:
            n_h = allocations[k]
            N_h = len(strata[k])
            if n_h == 0 or N_h == 0:
                continue
            pi_h = n_h / N_h
            sorted_stratum_bins = sorted(strata[k])
            chosen_bins = rng.sample(sorted_stratum_bins, min(n_h, N_h))
            for b in chosen_bins:
                meta = bin_records[b]
                _b_up_s = str(b).strip().upper()
                _show_pcs_s = bool(_b_up_s.startswith(("CRT", "PRO")) or any(p in small_part_rand_mats for p in meta["parts"]))
                selected_samples.append({
                    "BIN": b,
                    "Plant": meta["Plant"],
                    "Tier": meta["Tier"],
                    "Risk_Score": 0.0,
                    "Label_QTY": meta["boxes"],
                    "Parts_List": ", ".join(sorted(meta["parts"])),
                    "Part_Count": len(meta["parts"]),
                    "Action_Required": "Cart PCS Check" if _b_up_s.startswith(("CRT", "PRO")) else "Random Verification",
                    "Reason": "Random sample (model validation)",
                    "Warehouse_Dwell_Days": meta["wh_dwell"],
                    "Bin_Dwell_Days": meta["bin_dwell"],
                    "Total_Qty": meta["qty"],
                    "Show_Total_PCS": _show_pcs_s,
                    "Total_PCS": int(round(meta["qty"])) if _show_pcs_s else "",
                    "WMS_Barcodes": sorted(list(set(meta["barcodes"]))),
                    "Label_Qty_Basis": "ALL_BIN_BARCODES",
                    "selection_method": "RANDOM_SAMPLE",
                    "list_type": "RANDOM",
                    "inclusion_probability": round(pi_h, 6),
                    "stratum": f"{meta['Plant']} - {meta['Tier_Group']}",
                    "stratum_population": N_h,
                    "stratum_sample_size": n_h
                })

        # Sort randomly chosen bins by Plant, then BIN name for efficient floor audit
        selected_samples.sort(key=lambda x: (x["Plant"], x["BIN"]))
        return selected_samples

    def export_audit_excel(
        self,
        analysis_result: Dict[str, Any],
        output_path: Optional[Path] = None,
        mode: str = "Daily",
        dry_run: bool = False,
    ) -> Path:
        """Export multi-sheet Excel file for warehouse physical cycle counts (Daily or Weekly)."""
        df = analysis_result.get("suspects", pd.DataFrame())
        today_str = datetime.date.today().strftime("%Y%m%d")
        file_prefix = "WMS_Weekly_Cycle_Count_List" if mode.lower() == "weekly" else "WMS_Cycle_Count_Audit_List"
        if dry_run:
            ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            if output_path is not None and Path(output_path).parent.resolve() != AUDIT_REPORTS_DIR.resolve():
                scratch_dir = Path(output_path).parent
                scratch_dir.mkdir(parents=True, exist_ok=True)
                raw_name = Path(output_path).name
                fname = raw_name if raw_name.startswith("DRYRUN_") else f"DRYRUN_{raw_name}"
                target_path = scratch_dir / fname
            else:
                scratch_dir = CURRENT_DIR / "scratch"
                scratch_dir.mkdir(parents=True, exist_ok=True)
                target_path = scratch_dir / f"DRYRUN_{file_prefix}_{ts_str}.xlsx"
        else:
            target_path = output_path or (AUDIT_REPORTS_DIR / f"{file_prefix}_{today_str}.xlsx")
        
        with pd.ExcelWriter(target_path, engine="openpyxl") as writer:
            # Sheet 1: Cycle Count Target Bins (Aggregated for floor/reach truck counts + Random Validation)
            all_target_bins = (
                analysis_result.get("top_bins", []) +
                analysis_result.get("extended_bins", []) +
                analysis_result.get("random_bins", [])
            )
            if all_target_bins:
                field_inst = (
                    [str(x).strip() for x in (analysis_result.get("field_instructions") or []) if str(x).strip()][:5]
                    if mode.lower() != "weekly"
                    else []
                )
                start_row = (len(field_inst) + 1) if field_inst else 0
                df_bins = pd.DataFrame([{
                    "Rank": i,
                    "Bin #": b["BIN"],
                    "Part #": b["Parts_List"],
                    "Label QTY": b["Label_QTY"],
                    "Total PCS": b.get("Total_PCS", ""),
                    # [FIELD AUDIT INPUT / 현장 실사 입력란 (세로 1페이지 초간단 5대 컬럼)]
                    "Counted QTY": ""
                } for i, b in enumerate(all_target_bins, 1)])
                df_bins.to_excel(writer, sheet_name="Cycle_Count_Target_Bins", index=False, startrow=start_row)
                if field_inst:
                    ws_target = writer.sheets["Cycle_Count_Target_Bins"]
                    ws_target.cell(row=1, column=1, value="Field Instructions")
                    for fi_idx, fi_line in enumerate(field_inst, start=2):
                        ws_target.cell(row=fi_idx, column=1, value=f"• {fi_line}")

            # D99-2: Open_Mismatches Sheet (Recently audited mismatched bins excluded until WMS changes, up to 3 business days)
            if mode.lower() != "weekly":
                open_mm = analysis_result.get("open_mismatches", [])
                if open_mm:
                    df_open_mm = pd.DataFrame(open_mm)
                else:
                    df_open_mm = pd.DataFrame(columns=[
                        "Bin", "Part #", "Label QTY", "Actual QTY", "Diff QTY", "Audit Date", "Elapsed Days", "Status"
                    ])
                df_open_mm.to_excel(writer, sheet_name="Open_Mismatches", index=False)

            # Manager-only Comparison Sheet: Shadow EV Ranking (EV vs Rule Rank)
            shadow_ranking = analysis_result.get("shadow_ev_ranking", [])
            shadow_cfg = getattr(self, "scoring_config", {}).get("shadow_ev", {})
            include_shadow = shadow_cfg.get("include_in_admin_excel", True) and bool(shadow_ranking)
            if include_shadow:
                df_shadow = pd.DataFrame([{
                    "Shadow Rank": r["Shadow_Rank"],
                    "Rule Rank": r["Rule_Rank"],
                    "Rank Shift": r["Rank_Shift"],
                    "Bin #": r["BIN"],
                    "Plant": r["Plant"],
                    "Tier": r["Tier"],
                    "Bin Type": r["Bin_Type"],
                    "Part #": r["Parts_List"],
                    "Label QTY": r["Label_QTY"],
                    "Total Qty": r["Total_Qty"],
                    "Rule Risk Score": r["Rule_Risk_Score"],
                    "P(Error) Proxy": r["P_Error_Proxy"],
                    "Est Bin Value ($)": r["Est_Bin_Value"],
                    "Expected Loss ($)": r["Expected_Loss"],
                    "Est Minutes": r["Est_Minutes"],
                    "Priority EV ($/min)": r["Priority_EV"],
                    "Action Required": r["Action_Required"],
                    "Reason": r["Reason"]
                } for r in shadow_ranking])
                df_shadow.to_excel(writer, sheet_name="Shadow_EV_Ranking", index=False)

            # Dedicated Sheet: MissingGI Ghost Suspects (생산 무스캔 소진 Ghost Stock 의심)
            if not df.empty:
                df_ghost = df[df["Risk_Categories"].str.contains("CRITICAL_GHOST_SUSPECT|CRITICAL_PHANTOM_SUSPECT|MISSING_GI_UNDER_DEFICIT", regex=True, na=False)]
                if not df_ghost.empty:
                    df_ghost.head(300).to_excel(writer, sheet_name="MissingGI_Ghost_Suspects", index=False)

                # Dedicated Sheet: Same-Bin FIFO Obstruction (동일 빈 내 신규 입고 대비 방치)
                df_samebin = df[df["Risk_Categories"].str.contains("SAME_BIN_FIFO_OBSTRUCTION", na=False)]
                if not df_samebin.empty:
                    df_samebin.head(200).to_excel(writer, sheet_name="SameBin_FIFO_Obstruction", index=False)

            if mode.lower() == "weekly":
                # =========================================================
                # WEEKLY EXCEL SHEETS
                # =========================================================
                if not df.empty:
                    # Sheet 4: High-Rack Aging Master (Plant 3 & High Tier 3-5)
                    df_hr = df[df["Risk_Categories"].str.contains("HIGH_RACK_AGING", na=False)]
                    df_hr.to_excel(writer, sheet_name="HighRack_Aging_Master", index=False)

                    # Sheet 5: Fast Mover Residual Bins
                    df_fm = df[df["Risk_Categories"].str.contains("FAST_MOVER_RESIDUAL", na=False)]
                    df_fm.to_excel(writer, sheet_name="FastMover_Residual_Bins", index=False)

                    # Sheet 6: Turnover Outliers All
                    df_outlier = df[df["Risk_Categories"].str.contains("TURNOVER_ANOMALY", na=False)]
                    df_outlier.to_excel(writer, sheet_name="Turnover_Outliers_All", index=False)

                    # Sheet 7: Complete Suspects Top 500
                    df.head(500).to_excel(writer, sheet_name="All_Suspects_Top500", index=False)
            else:
                # =========================================================
                # DAILY EXCEL SHEETS
                # =========================================================
                if not df.empty:
                    # Sheet 4: Plant 1 Small Parts (Box presence check only)
                    df_small = df[df["Risk_Categories"].str.contains("PLANT1_HAND_PICK_SMALL_PART", na=False)]
                    df_small.to_excel(writer, sheet_name="Plant1_Small_Parts_BoxCheck", index=False)
                    
                    # Sheet 5: SMT Floor & Repack Stagnation
                    df_buff = df[df["Risk_Categories"].str.contains("SMT_SUPERMARKET_BUFFER|REPACK_CART_DOLLY", regex=True, na=False)]
                    df_buff.to_excel(writer, sheet_name="SMT_Floor_Stagnation", index=False)
                    
                    # Sheet 6: FIFO Inversions (Including GI-Verified Bypass)
                    df_fifo = df[df["Risk_Categories"].str.contains("FIFO_INVERSION|PROVEN_FIFO_BYPASS_GI_VERIFIED", regex=True, na=False)]
                    df_fifo.to_excel(writer, sheet_name="FIFO_Inversion_Master", index=False)
                    
                    # Sheet 7: Complete Suspects Top 500
                    df.head(500).to_excel(writer, sheet_name="All_Suspects_Top500", index=False)
        
        # Apply field clipboard print setup and professional styling
        try:
            self._apply_excel_formatting_and_print_setup(target_path)
        except Exception as e:
            print(f"[BoxAgingTracker] Warning: Excel formatting post-processing failed: {e}")

        print(f"[BoxAgingTracker] [{mode}] 실사 엑셀 파일 생성 완료: {target_path.name} ({target_path.stat().st_size:,} bytes)")
        return target_path

    def _apply_excel_formatting_and_print_setup(self, excel_path: Path) -> None:
        """
        Applies professional typography, visual sectioning, cell borders,
        and printer-friendly page setups (landscape, fit-to-page-width, repeat headers)
        optimized for warehouse field clipboard clip & handwriting.
        """
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        wb = openpyxl.load_workbook(excel_path)
        
        # Color palette
        NAVY_HEADER = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")     # System info (#1e293b slate-800)
        EMERALD_HEADER = PatternFill(start_color="065F46", end_color="065F46", fill_type="solid")  # Field input (#065f46 emerald-800)
        WHITE_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        REGULAR_FONT = Font(name="Calibri", size=10.5)
        BOLD_FONT = Font(name="Calibri", size=10.5, bold=True)
        
        THIN_BORDER = Border(
            left=Side(style="thin", color="94A3B8"),
            right=Side(style="thin", color="94A3B8"),
            top=Side(style="thin", color="94A3B8"),
            bottom=Side(style="thin", color="94A3B8")
        )
        
        # 1. Format Sheet: Cycle_Count_Target_Bins (Primary Field Audit Sheet - 5-Column Portrait 1-Page Fit)
        if "Cycle_Count_Target_Bins" in wb.sheetnames:
            ws = wb["Cycle_Count_Target_Bins"]
            
            # Detect table header row (Row 1 normally; Row N+2 when D112 Field Instructions precede the table)
            header_row = 1
            for cand_r in range(1, min(12, ws.max_row + 1)):
                if str(ws.cell(row=cand_r, column=1).value or "").strip() == "Rank":
                    header_row = cand_r
                    break

            # Print Setup: Exact 1-Page Portrait Fit (세로 1페이지 완벽 맞춤)
            ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
            ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
            ws.sheet_properties.pageSetUpPr.fitToPage = True
            ws.page_setup.fitToWidth = 1
            ws.page_setup.fitToHeight = 1  # 세로 1페이지 완벽 맞춤
            ws.print_title_rows = f"$1:${header_row}"
            ws.views.sheetView[0].showGridLines = True
            ws.page_margins.left = 0.25
            ws.page_margins.right = 0.25
            ws.page_margins.top = 0.25
            ws.page_margins.bottom = 0.25
            
            max_col = max(ws.max_column, 6)
            max_row = ws.max_row

            # Optional D112 Field Instructions Banner above Table Header (#219: 14pt bold + prominent amber fill)
            if header_row > 1:
                FI_TITLE_FILL = PatternFill(start_color="FDE68A", end_color="FDE68A", fill_type="solid")
                FI_BODY_FILL = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
                FI_TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="78350F")
                FI_BODY_FONT = Font(name="Calibri", size=14, bold=True, color="78350F")
                FI_BORDER = Border(
                    left=Side(style="medium", color="D97706"),
                    right=Side(style="medium", color="D97706"),
                    top=Side(style="medium", color="D97706"),
                    bottom=Side(style="thin", color="F59E0B"),
                )
                ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max_col)
                ws.row_dimensions[1].height = 24
                for col in range(1, max_col + 1):
                    c_title = ws.cell(row=1, column=col)
                    c_title.fill = FI_TITLE_FILL
                    c_title.font = FI_TITLE_FONT
                    c_title.alignment = Alignment(horizontal="left", vertical="center")
                    c_title.border = FI_BORDER
                for fi_r in range(2, header_row):
                    txt_val = str(ws.cell(row=fi_r, column=1).value or "")
                    ws.merge_cells(start_row=fi_r, start_column=1, end_row=fi_r, end_column=max_col)
                    ws.row_dimensions[fi_r].height = 38 if len(txt_val) > 68 else 24
                    for col in range(1, max_col + 1):
                        c_fi = ws.cell(row=fi_r, column=col)
                        c_fi.fill = FI_BODY_FILL
                        c_fi.font = FI_BODY_FONT
                        c_fi.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                        c_fi.border = FI_BORDER

            # Header Row
            ws.row_dimensions[header_row].height = 24
            
            for col in range(1, max_col + 1):
                cell = ws.cell(row=header_row, column=col)
                cell.font = WHITE_FONT
                # Columns 1-4 are System Target; Column 5 is Field Audit Input
                cell.fill = EMERALD_HEADER if col >= 6 else NAVY_HEADER
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = THIN_BORDER
                
            # Data Rows: 16.5pt height (14.2pt when >58 rows or when Field Instructions precede 43+ rows) for 1-page portrait fit
            data_row_height = 14.2 if (max_row > 58 or (header_row > 1 and max_row > 46)) else 16.5
            for row in range(header_row + 1, max_row + 1):
                ws.row_dimensions[row].height = data_row_height
                for col in range(1, max_col + 1):
                    cell = ws.cell(row=row, column=col)
                    cell.font = REGULAR_FONT
                    cell.border = THIN_BORDER
                    
                    # Alignment
                    if col in [1, 2, 4, 5, 6]:
                        if col == 5 and isinstance(cell.value, (int, float)):
                            cell.number_format = "#,##0"
                        cell.alignment = Alignment(horizontal="center", vertical="center")
                    else:
                        cell.alignment = Alignment(horizontal="left", vertical="center")
                        
                    # Bold Bin # and Rank
                    if col in [1, 2]:
                        cell.font = BOLD_FONT
                        
            # Column Widths optimized for 5-column portrait 1-page width (Total ~77)
            col_widths = {




                1: 7.0,   # Rank
                2: 14.0,  # Bin #
                3: 22.0,  # Part #
                4: 10.5,  # Label QTY
                5: 12.0,  # Total PCS
                6: 16.0   # Counted QTY
            }
            for c_idx, width in col_widths.items():
                col_letter = get_column_letter(c_idx)
                ws.column_dimensions[col_letter].width = width

        # 2. Format Other Sheets for Clean Display
        for sname in wb.sheetnames:
            if sname == "Cycle_Count_Target_Bins":
                continue
            ws = wb[sname]
            ws.views.sheetView[0].showGridLines = True
            ws.row_dimensions[1].height = 24
            for col in range(1, ws.max_column + 1):
                cell = ws.cell(row=1, column=col)
                cell.font = WHITE_FONT
                cell.fill = NAVY_HEADER
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border = THIN_BORDER
                col_letter = get_column_letter(col)
                ws.column_dimensions[col_letter].width = max(12, min(35, len(str(cell.value or "")) + 5))

        wb.save(excel_path)


if __name__ == "__main__":
    tracker = BoxAgingTracker()
    latest_file = tracker.find_latest_wms_file()
    if latest_file:
        print(f"최신 파일 분석: {latest_file.name}")
        df = tracker.load_stock_list(latest_file)
        res = tracker.detect_mis_scans(df)
        print(f"\n[분석 결과 요약]")
        print(f"- 전체 바코드: {res['total_barcodes']:,}건")
        print(f"- 점검 대상 바코드: {res['total_suspects']:,}건")
        print(f"  1) SMT 슈퍼마켓 및 CRT/PRO 대차 정체: {res['supermarket_cart_count']:,}건")
        print(f"  2) Plant 1 1·2층 소물류(Screw, Nut 등) 수기 반출 의심: {res['plant1_small_part_count']:,}건")
        print(f"  3) 동일 품번 내 선입선출(FIFO) 역전 방치 품목: {res['fifo_inversion_count']:,}건")
        
        if not res["suspects"].empty:
            print("\n[Plant 1 소물류 수기 피킹 의심 샘플 Top 5]")
            small_sample = res["suspects"][res["suspects"]["Risk_Categories"].str.contains("PLANT1_HAND_PICK_SMALL_PART")]
            print(small_sample[["Barcode", "Mat_No", "Description", "BIN", "Qty", "Primary_Reason"]].head(5).to_string())

            print("\n[FIFO 역전 샘플 Top 5]")
            fifo_sample = res["suspects"][res["suspects"]["Risk_Categories"].str.contains("FIFO_INVERSION")]
            print(fifo_sample[["Barcode", "Mat_No", "Description", "BIN", "Lot_No", "Primary_Reason"]].head(5).to_string())
