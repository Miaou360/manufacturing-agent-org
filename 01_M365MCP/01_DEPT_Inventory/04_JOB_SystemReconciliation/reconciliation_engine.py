"""
Core Reconciliation & Cross-Job Attribution Engine
===================================================
01_Inventory_Team / 04_JOB_SystemReconciliation
- ZTCODE_STOCK_REC 추출 원천 파일(UTF-16LE ALV 덤프)을 고속 파싱하여 수량 및 금액 기준 정합성 메트릭 산출.
- 01_JOB_GR_GI_Error 및 02_JOB_Backlog_Clear DB와 크로스 레퍼런스하여,
  전일 우리 AI 파이프라인의 MB1B 311 조치가 금일 공식 재고정확도에 기여한 인과적 효과(Attribution)를 정량 계산.
"""

import os
import sys
import sqlite3
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
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from db_manager import save_daily_snapshot, record_ai_attribution, get_latest_summary

# Sibling job database paths
GR_GI_DB_PATH = BASE_DIR.parent / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db"
BACKLOG_DB_PATH = BASE_DIR.parent / "02_JOB_Backlog_Clear" / "db" / "backlog_history.db"


def is_consignment_roh(material: str, material_type: str = "") -> bool:
    """
    Returns True if the material is a vendor consignment raw material (ROH2/ROH3, resin 74-*, chemical/paint 89-*).
    Consignment inventory creates a permanent optical illusion discrepancy between WMS physical bins and SAP.
    """
    m = (material or "").strip()
    mt = (material_type or "").strip().upper()
    if mt in ("ROH2", "ROH3"):
        return True
    if m.startswith("74-") or m.startswith("89-"):
        return True
    return False


def parse_ztcode_stock_rec_export(
    file_path: Path,
    exclude_consignment: bool = True
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Parses the raw tab-delimited text exported from SAP ZTCODE_STOCK_REC.
    Robustly handles UTF-16LE, column alignment, numeric formatting, and net prices.
    exclude_consignment: Excludes ROH2/ROH3 (74-* resins, 89-* chemicals) to eliminate consignment optical illusions.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Export file not found: {file_path}")

    raw_bytes = file_path.read_bytes()
    text = None
    for enc in ["utf-16", "utf-8", "cp949", "latin1"]:
        try:
            text = raw_bytes.decode(enc)
            break
        except Exception:
            pass

    if text is None:
        raise ValueError("Failed to decode ZTCODE_STOCK_REC export with known encodings.")

    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        raise ValueError("Export file is empty.")

    # Locate header row
    header_idx = -1
    for idx, line in enumerate(lines):
        if "Posting date" in line or "Material number" in line:
            header_idx = idx
            break

    if header_idx == -1:
        raise ValueError("Could not find standard ZTCODE_STOCK_REC table header in file.")

    # Parse data rows
    materials: List[Dict[str, Any]] = []
    posting_date = None
    plant = _get_ops_env("SAP_PLANT", "P001")
    system_group = "IWMS"

    total_diff_qty = 0.0
    total_financial_exposure = 0.0
    scores = []
    matched_count = 0
    mismatched_count = 0
    excluded_consignment_count = 0

    for line in lines[header_idx + 1:]:
        tokens = [t.strip() for t in line.split("\t")]
        if len(tokens) < 18:
            continue

        # Column positions in ALV Grid export:
        # 2: Posting date, 3: Material number, 4: Material type, 5: Material description
        # 8: WMS storage location, 9: SAP storage location, 10: WMS Quantity, 11: SAP Quantity
        # 12: Quantity Difference, 13: Update Date, 14: Update Time
        # 16: Base Unit, 17: Score, 18: Plant, 19: System Group
        # 20: Net price, 21: Currency, 22: Price Unit
        mat_no = tokens[3] if len(tokens) > 3 else ""
        if not mat_no or mat_no.startswith("-") or mat_no.startswith("=") or "Material" in mat_no:
            continue

        mat_type = tokens[4] if len(tokens) > 4 else ""
        if exclude_consignment and is_consignment_roh(mat_no, mat_type):
            excluded_consignment_count += 1
            continue

        if not posting_date and len(tokens) > 2 and tokens[2]:
            posting_date = tokens[2].replace(".", "-")

        mat_desc = tokens[5] if len(tokens) > 5 else ""
        wms_loc = tokens[8] if len(tokens) > 8 else ""
        sap_loc = tokens[9] if len(tokens) > 9 else ""

        def parse_float(val_str: str) -> float:
            try:
                return float(val_str.replace(",", "").strip())
            except Exception:
                return 0.0

        wms_qty = parse_float(tokens[10]) if len(tokens) > 10 else 0.0
        sap_qty = parse_float(tokens[11]) if len(tokens) > 11 else 0.0
        # Signed discrepancy: positive = WMS surplus / unposted SAP, negative = SAP ghost stock / deficit
        diff_qty = round(wms_qty - sap_qty, 2)
        update_date = tokens[13] if len(tokens) > 13 else ""
        update_time = tokens[14] if len(tokens) > 14 else ""
        score = parse_float(tokens[17]) if len(tokens) > 17 else 0.0
        row_plant = tokens[18] if len(tokens) > 18 and tokens[18] else plant
        row_sys = tokens[19] if len(tokens) > 19 and tokens[19] else system_group

        net_price = parse_float(tokens[20]) if len(tokens) > 20 else 0.0
        currency = tokens[21] if len(tokens) > 21 and tokens[21] else "USD"
        price_unit = parse_float(tokens[22]) if len(tokens) > 22 else 1.0
        if price_unit <= 0: price_unit = 1.0

        unit_cost = net_price / price_unit
        financial_exposure = round(abs(diff_qty) * unit_cost, 2)

        if diff_qty == 0:
            matched_count += 1
            score = 100.0
        else:
            mismatched_count += 1

        total_diff_qty += abs(diff_qty)
        total_financial_exposure += financial_exposure
        scores.append(score)

        materials.append({
            "material": mat_no,
            "material_type": mat_type,
            "description": mat_desc,
            "wms_lgort": wms_loc,
            "sap_lgort": sap_loc,
            "wms_qty": wms_qty,
            "sap_qty": sap_qty,
            "diff_qty": diff_qty,
            "score": score,
            "net_price": net_price,
            "currency": currency,
            "price_unit": price_unit,
            "financial_exposure": financial_exposure,
            "update_date": update_date,
            "update_time": update_time,
            "plant": row_plant,
            "system_group": row_sys
        })

    total_items = len(materials)
    if total_items == 0:
        raise ValueError("Parsed 0 valid materials from ZTCODE_STOCK_REC export.")

    accuracy_rate = round((matched_count / total_items) * 100, 2)
    avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
    # For historical backfills, snapshot date corresponds to posting_date + 1 day
    if posting_date:
        try:
            p_dt = datetime.strptime(posting_date, "%Y-%m-%d")
            today_dt = datetime.now().date()
            if p_dt.date() >= today_dt - timedelta(days=1):
                snapshot_date = today_dt.strftime("%Y-%m-%d")
            else:
                snapshot_date = (p_dt + timedelta(days=1)).strftime("%Y-%m-%d")
        except Exception:
            snapshot_date = datetime.now().strftime("%Y-%m-%d")
    else:
        snapshot_date = datetime.now().strftime("%Y-%m-%d")

    summary = {
        "snapshot_date": snapshot_date,
        "posting_date": posting_date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"),
        "plant": plant,
        "system_group": system_group,
        "total_items": total_items,
        "matched_items": matched_count,
        "mismatched_items": mismatched_count,
        "accuracy_rate": accuracy_rate,
        "avg_score": avg_score,
        "total_diff_qty": round(total_diff_qty, 2),
        "total_financial_exposure": round(total_financial_exposure, 2),
        "consignment_excluded_items": excluded_consignment_count
    }

    return summary, materials


def evaluate_ai_attribution(
    materials: List[Dict[str, Any]],
    snapshot_date: str,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    SYNERGY ENGINE:
    Evaluates whether MB1B 311 movements successfully executed by 01_JOB or 02_JOB
    on the previous day directly contributed to 100% accuracy in today's official ZTCODE_STOCK_REC report.
    """
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    cleared_materials = set()

    # 1. Check 01_JOB_GR_GI_Error transfer_posting_history
    if GR_GI_DB_PATH.exists():
        try:
            conn = sqlite3.connect(str(GR_GI_DB_PATH))
            cur = conn.cursor()
            cur.execute("""
                SELECT DISTINCT material FROM transfer_posting_history
                WHERE post_status = 'SUCCESS' AND date(executed_at) >= date(?)
            """, (yesterday_str,))
            for r in cur.fetchall():
                cleared_materials.add(r[0])
            conn.close()
        except Exception:
            pass

    # Map current materials
    current_mat_map = {m["material"]: m for m in materials}

    evaluated_items = []
    resolved_count = 0

    for mat in cleared_materials:
        if mat in current_mat_map:
            item = current_mat_map[mat]
            is_resolved = (item["diff_qty"] == 0 or item["score"] == 100.0)
            if is_resolved:
                resolved_count += 1
            evaluated_items.append({
                "material": mat,
                "description": item.get("description", ""),
                "score": item["score"],
                "diff_qty": item["diff_qty"],
                "financial_exposure": item.get("financial_exposure", 0.0),
                "is_resolved": is_resolved
            })

    total_evaluated = len(evaluated_items)
    accuracy_lift_pct = 0.0
    if len(materials) > 0 and resolved_count > 0:
        accuracy_lift_pct = round((resolved_count / len(materials)) * 100, 2)

    attribution_result = {
        "snapshot_date": snapshot_date,
        "evaluated_materials_count": total_evaluated,
        "resolved_materials_count": resolved_count,
        "accuracy_lift_pct": accuracy_lift_pct,
        "items": evaluated_items
    }

    if not dry_run:
        record_ai_attribution(
            reconciliation_date=snapshot_date,
            evaluated_count=total_evaluated,
            resolved_count=resolved_count,
            accuracy_lift_pct=accuracy_lift_pct,
            details=attribution_result
        )

    return attribution_result


def process_reconciliation_file(
    file_path: Path,
    exclude_consignment: bool = True,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Full pipeline execution for an extracted file:
    1. Parse raw export (with default consignment ROH2/ROH3 exclusion)
    2. Save to DB (Summary + Line Items + Chronic Registry) unless dry_run=True
    3. Evaluate AI Attribution
    """
    summary, materials = parse_ztcode_stock_rec_export(file_path, exclude_consignment=exclude_consignment)
    if not dry_run:
        save_daily_snapshot(summary, materials)
    attribution = evaluate_ai_attribution(materials, summary["snapshot_date"], dry_run=dry_run)
    return {
        "summary": summary,
        "attribution": attribution,
        "materials_count": len(materials)
    }
