"""
SL01 R-Bin Movement Lag Monitoring Operator
===========================================
01_Inventory_Team / 05_JOB_SL01_R_Bins (Plant P001 Headlamp)

Operational Concept:
- SL01 (Main parts, Plant 1) materials stored in Rack ('R' prefix, Plant 3) bins
  represent in-transit inventory being moved to production lines or carts.
- If inventory remains in SL01 R-bins for >= 2 business days (D98-1), the physical transfer
  or WMS relocation scan is stalled and requires inventory team verification.
- Tracking uses genuine `first_seen_date` across daily WMS snapshots (NOT gr_date).

Usage:
  python sl01_r_bins_operator.py --backfill
  python sl01_r_bins_operator.py --scan
  python sl01_r_bins_operator.py --report
  python sl01_r_bins_operator.py --email [--test]
  python sl01_r_bins_operator.py --all [--test]
"""

import os
import sys
import glob
import re
import argparse
import logging
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
from datetime import datetime
from typing import List, Dict, Optional, Tuple
import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent.parent
HQ_DIR = PROJECT_ROOT / "00_HQ_TaskManager"
STOCK_ACC_DIR = BASE_DIR.parent / "00_JOB_StockAccuracy"
REPORTS_DIR = BASE_DIR / "reports"
DB_DIR = BASE_DIR / "db"

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
DB_DIR.mkdir(parents=True, exist_ok=True)

if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

from wms_mapping import map_wms_gi_loc_to_sap
import db_manager
from db_manager import (
    init_db, upsert_observations, get_active_lagging_observations,
    get_all_active_observations, log_scan_run, get_db_connection
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SL01R_Operator")


from collections import defaultdict

def normalize_bin_name(bin_val: str) -> str:
    """Normalizes bin names by stripping whitespace and collapsing internal spaces (D87-4)."""
    return re.sub(r'\s+', ' ', str(bin_val or '').strip().upper())


def extract_sl01_r_records_from_wms(filepath: Path) -> List[Dict]:
    """
    Extracts rows where GI Loc maps to SL01 and BIN starts with 'R' (D87-1).
    Simultaneously cross-references same-snapshot SL02 stock to annotate
    reference signals adhering to D87-4, D88-2, and D98-3:
      🔴 REFILLED: Another material is currently stored in this bin in SL02
      🟣 DUP ELSEWHERE (candidate): Same material, same qty, AND same Lot in strictly 1 other R-bin in SL02 (D98-3)
      🟠 SAME PART: Same material remains in this bin in SL02 with different barcode
      EMPTY_OR_NO_SL02: No SL02 stock in this bin
    """
    if not filepath.exists():
        logger.error(f"File not found: {filepath}")
        return []

    try:
        df = pd.read_excel(filepath)
    except Exception as e:
        logger.error(f"Error reading {filepath}: {e}")
        return []

    mat_col = next((c for c in df.columns if 'Mat.No' in c or 'Material' in c), None)
    desc_col = next((c for c in df.columns if 'Desc' in c), None)
    loc_col = next((c for c in df.columns if 'GI Loc' in c or 'SLoc' in c), None)
    qty_col = next((c for c in df.columns if 'Qty' in c), None)
    bin_col = next((c for c in df.columns if 'BIN' in c or 'Bin' in c), None)
    bar_col = next((c for c in df.columns if 'Barcode' in c), None)
    lot_col = next((c for c in df.columns if 'Lot No' in c or 'Lot' in c), None)
    vcode_col = next((c for c in df.columns if 'Vendor Code' in c), None)
    vname_col = next((c for c in df.columns if 'Vendor Name' in c), None)

    if not (mat_col and loc_col and qty_col and bin_col and bar_col):
        logger.error("Required columns missing in WMS file.")
        return []

    raw_sl01_items = []
    sl02_stock_by_bin = defaultdict(list)
    sl02_stock_by_mat_qty_lot = defaultdict(list)

    for _, row in df.iterrows():
        raw_loc = str(row[loc_col]).strip()
        mapped_sloc = map_wms_gi_loc_to_sap(raw_loc)
        raw_bin = str(row[bin_col]).strip()
        norm_bin = normalize_bin_name(raw_bin)

        if not norm_bin.startswith("R"):
            continue

        barcode_val = str(row[bar_col]).strip()
        if not barcode_val or barcode_val.lower() == 'nan':
            continue

        try:
            qty_val = float(str(row[qty_col]).replace(',', '').strip())
        except (ValueError, TypeError):
            qty_val = 0.0

        mat_no = str(row[mat_col]).strip()
        mat_desc = str(row[desc_col]).strip() if desc_col and pd.notnull(row[desc_col]) else ""
        lot_val = str(row[lot_col]).strip() if lot_col and pd.notnull(row[lot_col]) else ""
        if lot_val.lower() == "nan":
            lot_val = ""

        # Collect SL02 R-bin stock for cross-reference (D87-4, D88-2, D98-3)
        if mapped_sloc == "SL02":
            sl02_stock_by_bin[norm_bin].append({
                "mat_no": mat_no,
                "barcode": barcode_val,
                "qty": qty_val,
                "lot_no": lot_val,
            })
            sl02_stock_by_mat_qty_lot[(mat_no, qty_val, lot_val)].append(norm_bin)
        elif mapped_sloc == "SL01":
            raw_sl01_items.append({
                "barcode": barcode_val,
                "mat_no": mat_no,
                "mat_desc": mat_desc,
                "bin": norm_bin,
                "gi_loc": raw_loc,
                "qty": qty_val,
                "lot_no": lot_val,
                "vendor_code": str(row[vcode_col]).strip() if vcode_col and pd.notnull(row[vcode_col]) else "",
                "vendor_name": str(row[vname_col]).strip() if vname_col and pd.notnull(row[vname_col]) else "",
            })

    records = []
    for item in raw_sl01_items:
        bin_key = item["bin"]
        sl02_items = sl02_stock_by_bin.get(bin_key, [])

        other_mat_items = [x for x in sl02_items if x["mat_no"] != item["mat_no"]]
        same_mat_items = [x for x in sl02_items if x["mat_no"] == item["mat_no"] and x["barcode"] != item["barcode"]]

        is_refilled = bool(other_mat_items)
        is_same = bool(same_mat_items)

        if other_mat_items:
            other_mats = sorted(list(set(x["mat_no"] for x in other_mat_items)))
            tot_other_qty = sum(x["qty"] for x in other_mat_items)
            if len(other_mats) == 1:
                item["sl02_details"] = f"{other_mats[0]} ({tot_other_qty:,.0f} EA)"
            else:
                item["sl02_details"] = f"{other_mats[0]} +{len(other_mats)-1} ({tot_other_qty:,.0f} EA)"
        elif same_mat_items:
            tot_same_qty = sum(x["qty"] for x in same_mat_items)
            item["sl02_details"] = f"{item['mat_no']} ({tot_same_qty:,.0f} EA, {len(same_mat_items)} plts)"
        else:
            item["sl02_details"] = "-"

        # D98-3: Strict cross-bin candidate in another SL02 R-bin with same mat_no, same qty, AND same lot_no,
        # and only when the number of matching other SL02 R-bins is strictly 1.
        same_lot_other_bins = sorted(list(set(
            b for b in sl02_stock_by_mat_qty_lot.get((item["mat_no"], item["qty"], item["lot_no"]), [])
            if b != bin_key
        )))
        cand_bins = same_lot_other_bins if len(same_lot_other_bins) == 1 else []
        item["candidate_cross_bins"] = cand_bins
        item["candidate_cross_bin"] = cand_bins[0] if cand_bins else ""

        item["is_refilled"] = is_refilled
        item["is_same_remains"] = is_same
        records.append(item)

    logger.info(f"Extracted {len(records)} SL01 R-bin records from {filepath.name} (with SL02 cross-reference)")
    return records


def run_historical_backfill() -> int:
    """
    Scans all available historical WMS daily snapshots in chronological order
    to establish true first_seen_date baseline for every barcode.
    """
    init_db()
    daily_stack = STOCK_ACC_DIR / "wms_daily_stack"
    files = sorted(glob.glob(str(daily_stack / "*.xlsx")))
    if not files:
        logger.warning(f"No WMS snapshots found in {daily_stack}")
        return 0

    logger.info(f"=== Starting Historical Backfill ({len(files)} snapshots) ===")
    total_processed = 0

    for f_path in files:
        p = Path(f_path)
        m = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", p.name)
        if not m:
            continue
        scan_date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        logger.info(f"Processing snapshot: {p.name} (Date: {scan_date_str})...")

        records = extract_sl01_r_records_from_wms(p)
        if records:
            upsert_observations(scan_date_str, records, is_backfill=True)
            total_processed += len(records)

    logger.info(f"=== Backfill Complete: {total_processed} observations processed ===")
    return total_processed


def run_daily_scan(
    snapshot_file: Optional[Path] = None,
    is_fallback: bool = False,
    db_path: Optional[Path] = None
) -> Tuple[int, int, float, str]:
    """
    Executes daily scan against latest WMS snapshot adhering to D87 & D98.
    Updates active observations and clears resolved barcodes.
    If is_fallback=True, database observations are frozen (upsert_observations skipped).
    Returns: (total_active, stale_count, stale_total_qty, scan_date_str)
    """
    target_db = Path(db_path or db_manager.DB_PATH)
    init_db(target_db)
    if not snapshot_file:
        daily_stack = STOCK_ACC_DIR / "wms_daily_stack"
        files = sorted(glob.glob(str(daily_stack / "*.xlsx")), key=os.path.getmtime, reverse=True)
        if not files:
            raise FileNotFoundError(f"No WMS snapshot found in {daily_stack}")
        snapshot_file = Path(files[0])

    m = re.search(r"(\d{4})(\d{2})(\d{2})_\d{6}", snapshot_file.name)
    if m:
        scan_date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    else:
        scan_date_str = datetime.now().strftime("%Y-%m-%d")

    logger.info(f"=== Running SL01 R-Bin Daily Scan for {scan_date_str} ({snapshot_file.name}, Fallback={is_fallback}) ===")

    if not is_fallback:
        records = extract_sl01_r_records_from_wms(snapshot_file)
        new_c, upd_c, clr_c = upsert_observations(scan_date_str, records, is_backfill=False, db_path=target_db)
        logger.info(f"DB Upsert Complete: {new_c} new, {upd_c} updated, {clr_c} cleared.")
    else:
        logger.warning("⚠️ [FALLBACK MODE] Database observations frozen. Skipping upsert_observations to prevent double-counting observation dates.")

    all_active = get_all_active_observations(db_path=target_db)
    stale_items = get_active_lagging_observations(db_path=target_db)
    stale_qty = sum(r["qty"] for r in stale_items)

    refilled_cnt = sum(1 for r in stale_items if "REFILLED" in r.get("sl02_signal", ""))
    dup_cnt = sum(1 for r in stale_items if "DUP_ELSEWHERE" in r.get("sl02_signal", ""))
    same_mat_cnt = sum(1 for r in stale_items if "SAME_MATERIAL_REMAINS" in r.get("sl02_signal", ""))

    logger.info(
        f"Scan Results: {len(all_active)} Active SL01 R-Bins | {len(stale_items)} Stale >=2d (🔴Refilled={refilled_cnt}, 🟣Dup={dup_cnt}, 🟠SamePart={same_mat_cnt}) | Total Stale Qty: {stale_qty:,.1f} EA"
    )
    return (len(all_active), len(stale_items), stale_qty, scan_date_str)


def generate_portrait_excel_report(
    scan_date_str: str,
    is_fallback: bool = False,
    fallback_file_time: Optional[str] = None,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> Path:
    """
    Generates an Excel report for field inventory inspection adhering strictly to D87, D89, and D98:
    - 100% English formatting (Thread 113).
    - Summary KPI cards removed (Table only).
    - Multi-page printing allowed without shrinking (fitToHeight=0, fitToWidth=1).
    - Table header repeated across all pages (ws.print_title_rows = '4:4').
    - Included items: Active SL01 R-bins with dwell days >= 2 business days regardless of signal (D98-1).
    - Ordered by Lag Days DESC, Qty DESC, Part ASC (D98-2); icons (🔴, 🟣, 🟠) kept as reference.
    - 12 Columns: No., Signal, Bin, Part, Description, Qty, Lot, First Seen, Days, Barcode, SL02 in Bin, Other Bin.
    - Signal column: Icon only (🔴, 🟣, 🟠, 🔴🟣, -) with soft background fills for B&W print readiness (D89).
    - Bottom 1-line legend below table (D89/D98).
    """
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    if dry_run:
        if output_dir is not None and Path(output_dir).resolve() != REPORTS_DIR.resolve():
            target_dir = Path(output_dir)
        else:
            target_dir = BASE_DIR / "scratch"
        target_dir.mkdir(parents=True, exist_ok=True)
        report_filename = f"DRYRUN_SL01_R_Bins_Lag_Report_{timestamp}.xlsx"
        target_path = target_dir / report_filename
        canonical_path = None
    else:
        report_filename = f"SL01_R_Bins_Lag_Report_{timestamp}.xlsx"
        target_dir = Path(output_dir or REPORTS_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / report_filename
        canonical_path = target_dir / "SL01_R_Bins_Lag_Report_latest.xlsx"

    target_db = Path(db_path or db_manager.DB_PATH)
    stale_items = get_active_lagging_observations(db_path=target_db)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SL01_R_Bin_Lag"

    # Page setup: Portrait, Letter, Fit to 1 page wide, multiple pages tall (D87-5)
    ws.page_setup.orientation = ws.ORIENTATION_PORTRAIT
    ws.page_setup.paperSize = ws.PAPERSIZE_LETTER
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0  # Multi-page allowed! Never shrink vertically
    ws.print_title_rows = '4:4'    # Repeat table header row on every printed page

    # Color Palette
    NAVY_HEADER = "1F4E78"
    ALERT_RED_BG = "FCE4D6"       # soft peach / light red (D89)
    ALERT_RED_FG = "C00000"
    ALERT_PURPLE_BG = "EDE9FE"    # soft lavender / purple (D89)
    ALERT_PURPLE_FG = "6B21A8"
    ALERT_AMB_BG = "FFF2CC"       # soft warm yellow (D89)
    ALERT_AMB_FG = "B25900"
    ZEBRA_BG = "F9FAFB"
    BORDER_GRAY = "D3D3D3"

    thin_border = Border(
        left=Side(style='thin', color=BORDER_GRAY),
        right=Side(style='thin', color=BORDER_GRAY),
        top=Side(style='thin', color=BORDER_GRAY),
        bottom=Side(style='thin', color=BORDER_GRAY)
    )

    # Title Banner (Row 1-2) - 12 Columns A to L
    ws.merge_cells("A1:L1")
    ws["A1"] = "SL01 R-Bin Movement Lag Monitoring Report"
    ws["A1"].font = Font(name="Calibri", size=15, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill(start_color=NAVY_HEADER, end_color=NAVY_HEADER, fill_type="solid")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")

    ws.merge_cells("A2:L2")
    if is_fallback and fallback_file_time:
        ws["A2"] = f"⚠️ [FALLBACK] WMS Live Download Failed — As of {fallback_file_time} (Obs Frozen) | Gen: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        ws["A2"].font = Font(name="Calibri", size=9, bold=True, color="FFFFFF")
        ws["A2"].fill = PatternFill(start_color="D68100", end_color="D68100", fill_type="solid")
    else:
        ws["A2"] = f"Plant {_get_ops_env('SAP_PLANT', 'P001')} Headlamp | Scan Date: {scan_date_str} | Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}"
        ws["A2"].font = Font(name="Calibri", size=9, italic=True, color="FFFFFF")
        ws["A2"].fill = PatternFill(start_color=NAVY_HEADER, end_color=NAVY_HEADER, fill_type="solid")
    ws["A2"].alignment = Alignment(horizontal="center", vertical="center")

    # Non-blocking sharp-drop warning row (Row 3) when active R-bin count drops >= 50% (#175)
    delta_info = db_manager.get_active_count_delta(scan_date_str, db_path=target_db) if not is_fallback else {"is_sharp_drop": False}
    if delta_info.get("is_sharp_drop"):
        ws.merge_cells("A3:L3")
        ws["A3"] = (
            f"⚠️ Sharp Drop Notice: Active SL01 R-bin count dropped -{delta_info['drop_pct']:.1f}% vs previous scan "
            f"({delta_info['prev_active_count']} → {delta_info['curr_active_count']} pallets; "
            f"{delta_info['cleared_today_count']} cleared today). Please verify physical rack clearance."
        )
        ws["A3"].font = Font(name="Calibri", size=8.5, bold=True, color=ALERT_AMB_FG)
        ws["A3"].fill = PatternFill(start_color=ALERT_AMB_BG, end_color=ALERT_AMB_BG, fill_type="solid")
        ws["A3"].alignment = Alignment(horizontal="center", vertical="center")

    # Table Header (Row 4) - 12 columns 100% English (D89 & Thread 113)
    headers = [
        ("No.", 5, "center"),
        ("Signal", 9, "center"),
        ("Bin", 12, "center"),
        ("Part", 16, "center"),
        ("Description", 28, "left"),
        ("Qty", 10, "right"),
        ("Lot", 12, "center"),
        ("First Seen", 12, "center"),
        ("Days", 8, "center"),
        ("Barcode", 22, "center"),
        ("SL02 in Bin", 24, "left"),
        ("Other Bin", 12, "center"),
    ]

    for col_idx, (h_name, width, align) in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col_idx, value=h_name)
        cell.font = Font(name="Calibri", size=9, bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color=NAVY_HEADER, end_color=NAVY_HEADER, fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
        col_letter = get_column_letter(col_idx)
        ws.column_dimensions[col_letter].width = width

    # Table Data Rows (Row 5+)
    current_row = 5
    if not stale_items:
        ws.merge_cells("A5:L5")
        empty_cell = ws["A5"]
        empty_cell.value = "🎉 No inventory lingering in SL01 R-bins beyond allowed threshold (>= 2 business days). All in-transit transfers settled."
        empty_cell.font = Font(name="Calibri", size=10, italic=True, color="008000")
        empty_cell.alignment = Alignment(horizontal="center", vertical="center")
        empty_cell.fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")
        for c in range(1, 13):
            ws.cell(row=5, column=c).border = thin_border
        current_row += 1
    else:
        for idx, item in enumerate(stale_items, start=1):
            lag = item["days_in_sl01_r"]
            sig = item.get("sl02_signal", "EMPTY_OR_NO_SL02")
            det = item.get("sl02_details", "-")
            other_bin = item.get("sl02_other_bin", "")

            # D89/D98-2: Reference icon only in Signal column, soft background fill for B&W print contrast
            if "REFILLED_AND_DUP" in sig:
                bg_color = ALERT_RED_BG
                sig_text = "🔴🟣"
                sig_fg = ALERT_RED_FG
                is_bold_sig = True
            elif "REFILLED" in sig:
                bg_color = ALERT_RED_BG
                sig_text = "🔴"
                sig_fg = ALERT_RED_FG
                is_bold_sig = True
            elif "DUP_ELSEWHERE" in sig:
                bg_color = ALERT_PURPLE_BG
                sig_text = "🟣"
                sig_fg = ALERT_PURPLE_FG
                is_bold_sig = True
            elif "SAME_MATERIAL_REMAINS" in sig:
                bg_color = ALERT_AMB_BG
                sig_text = "🟠"
                sig_fg = ALERT_AMB_FG
                is_bold_sig = True
            else:
                bg_color = ZEBRA_BG if idx % 2 == 0 else "FFFFFF"
                sig_text = "-"
                sig_fg = "595959"
                is_bold_sig = False

            row_data = [
                (idx, "center", False, "000000"),
                (sig_text, "center", is_bold_sig, sig_fg),
                (item["bin"], "center", True, "000000"),
                (item["mat_no"], "center", True, "000000"),
                (item.get("mat_desc", ""), "left", False, "000000"),
                (item["qty"], "right", False, "000000"),
                (item.get("lot_no", ""), "center", False, "000000"),
                (item["first_seen_date"], "center", False, "000000"),
                (lag, "center", True, ALERT_RED_FG if lag >= 3 else "000000"),
                (item["barcode"], "center", False, "000000"),
                (det, "left", False, "000000"),
                (other_bin if other_bin else "-", "center", True if other_bin else False, ALERT_PURPLE_FG if other_bin else "595959"),
            ]

            for c_idx, (val, align, bold, fg_col) in enumerate(row_data, start=1):
                cell = ws.cell(row=current_row, column=c_idx, value=val)
                cell.font = Font(name="Calibri", size=8.5, bold=bold, color=fg_col)
                cell.fill = PatternFill(start_color=bg_color, end_color=bg_color, fill_type="solid")
                cell.alignment = Alignment(horizontal=align, vertical="center")
                cell.border = thin_border
                if c_idx == 6:
                    cell.number_format = "#,##0"

            current_row += 1

        # D89/D98: One-line English legend below the table
        ws.merge_cells(f"A{current_row+1}:L{current_row+1}")
        legend_cell = ws[f"A{current_row+1}"]
        legend_cell.value = "🔴 bin refilled with other part · 🟣 same part/qty/lot in 1 other R bin (SL02) · 🟠 same part remains in bin"
        legend_cell.font = Font(name="Calibri", size=8.5, italic=True, color="595959")
        legend_cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.freeze_panes = "A5"
    wb.save(target_path)
    if canonical_path is not None:
        wb.save(canonical_path)
    logger.info(f"Report generated: {target_path} (Canonical: {canonical_path})")
    return target_path


def send_outlook_notification(
    report_file: Optional[Path],
    scan_date_str: str,
    test_mode: bool = False,
    override_to: Optional[str] = None,
    is_fallback: bool = False,
    fallback_file_time: Optional[str] = None,
    fallback_expired: bool = False,
    db_path: Optional[Path] = None
) -> bool:
    """
    Dispatches Outlook email via M365 plugin or Outlook COM automation adhering to D88, D89, and D98:
    - 100% English formatting (Thread 113).
    - Single list criterion: Dwell days >= 2 business days (D98-1), sorted by Days DESC -> Qty DESC (D98-2).
    - Signal column: Icon only (🔴, 🟣, 🟠, 🔴🟣, -) as reference indicator.
    - One-line English legend below table.
    - 12 Columns: No., Signal, Bin, Part, Description, Qty, Lot, First Seen, Days, Barcode, SL02 in Bin, Other Bin.
    """
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")

    now = datetime.now()

    # Recipients setup
    if test_mode:
        raw_test_user = (
            os.getenv("TEST_EMAIL_RECIPIENT")
            or os.getenv("STOCK_ACCURACY_VERIFY")
            or os.getenv("GI_ERROR_REPORT_TO")
            or os.getenv("SMTP_USER", "")
        )
        default_user = raw_test_user.replace(";", ",").split(",")[0].strip() if raw_test_user else ""
        to_email = override_to or default_user
        cc_email = ""
        subject_prefix = "[TEST - 05_JOB] "
    else:
        to_email = os.getenv("STOCK_ACCURACY_TO", "")
        cc_email = os.getenv("STOCK_ACCURACY_CC", "")
        subject_prefix = f"[{_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')}] "

    if not to_email or "example.com" in to_email.lower():
        logger.warning("No valid recipient email found (fail closed). Skipping notification.")
        return False

    # Case 1: Fallback snapshot expired (> 24 hours) - Send alert only
    if fallback_expired:
        subject = f"{subject_prefix}🚨 [WMS Download Failed] SL01 R-Bin Daily Monitoring Failed (Source File > 24 Hours)"
        html_body = f"""
        <html>
        <head>
            <style>
                body {{ font-family: 'Segoe UI', Calibri, Arial, sans-serif; font-size: 13px; color: #333; }}
                .alert-header {{ background-color: #C00000; color: white; padding: 12px 16px; border-radius: 4px; }}
                .content {{ margin: 15px 0; line-height: 1.6; }}
            </style>
        </head>
        <body>
            <div class="alert-header">
                <h2 style="margin: 0; font-size: 16px;">🚨 [Urgent] WMS Stock Download Failed & Snapshot Exceeded 24 Hours</h2>
                <div style="font-size: 11px; opacity: 0.9; margin-top: 3px;">
                    Plant {_get_ops_env('SAP_PLANT', 'P001')} Headlamp | Timestamp: {now.strftime('%Y-%m-%d %H:%M:%S')}
                </div>
            </div>
            <div class="content">
                <p>Hello, this is the Automated Inventory Monitoring System.</p>
                <p>
                    The scheduled WMS stock download at 04:15 AM today (<b>{now.strftime('%Y-%m-%d')}</b>) failed, and the latest available fallback snapshot (<b>{fallback_file_time or 'Unknown'}</b>) is <b>older than 24 hours</b>.
                </p>
                <p>
                    To prevent dissemination of distorted lag metrics, <b>today's SL01 R-bin report dispatch has been suppressed</b>.<br>
                    Please verify the WMS web server connectivity or user session status.
                </p>
            </div>
        </body>
        </html>
        """
        _dept_dir = Path(__file__).resolve().parents[1]
        if str(_dept_dir) not in _sys.path:
            _sys.path.insert(0, str(_dept_dir))
        from report_labels import check_report_language
        check_report_language('05_JOB_sl01_r_bins', subject=subject, html_body=html_body, attachments=[], is_user_only=test_mode)
        return _dispatch_email(to_email, cc_email, subject, html_body, attachment=None)

    # Case 2: Normal run or Fallback within 24h
    target_db = Path(db_path or db_manager.DB_PATH)
    stale_items = get_active_lagging_observations(db_path=target_db)
    stale_cnt = len(stale_items)
    stale_qty = sum(r["qty"] for r in stale_items)
    refilled_cnt = sum(1 for r in stale_items if "REFILLED" in r.get("sl02_signal", ""))
    dup_cnt = sum(1 for r in stale_items if "DUP_ELSEWHERE" in r.get("sl02_signal", ""))
    same_mat_cnt = sum(1 for r in stale_items if "SAME_MATERIAL_REMAINS" in r.get("sl02_signal", ""))

    if is_fallback and fallback_file_time:
        subject = f"{subject_prefix}⚠️ [WMS Fallback] SL01 R-Bin Movement Lag Daily Report - {scan_date_str} ({stale_cnt} Pallets Stalled)"
        fallback_banner = f"""
        <div style="background-color: #FFF2CC; border: 2px solid #D68100; border-radius: 4px; padding: 12px 16px; margin: 12px 0; color: #8A4B00;">
            <h3 style="margin: 0 0 6px 0; color: #B25900; font-size: 13px;">⚠️ WMS Live Download Failed — Generated from {fallback_file_time} Snapshot</h3>
            <div style="font-size: 12px; line-height: 1.5;">
                Due to a temporary WMS live download failure at 04:15 AM today ({now.strftime('%Y-%m-%d')}), this report was generated using the most recent snapshot (<b>{fallback_file_time}</b>).<br>
                To avoid distorting lag days, <b>database observation days (days_in_sl01_r) are frozen</b>, and new observation counts will resume upon the next successful live crawl.
            </div>
        </div>
        """
    else:
        subject = f"{subject_prefix}SL01 R-Bin Movement Lag Daily Report - {scan_date_str} ({stale_cnt} Pallets Stalled)"
        fallback_banner = ""

    # Non-blocking sharp-drop warning banner when active R-bin count drops >= 50% (#175)
    delta_info = db_manager.get_active_count_delta(scan_date_str, db_path=target_db) if not is_fallback else {"is_sharp_drop": False}
    if delta_info.get("is_sharp_drop"):
        sharp_drop_banner = f"""
        <div style="background-color: #FFF8E1; border: 2px solid #F59E0B; border-radius: 4px; padding: 12px 16px; margin: 12px 0; color: #92400E;">
            <h3 style="margin: 0 0 6px 0; color: #B45309; font-size: 13px;">⚠️ Active SL01 R-Bin Count Dropped -{delta_info['drop_pct']:.1f}% vs Previous Scan ({delta_info['prev_active_count']} &rarr; {delta_info['curr_active_count']} pallets)</h3>
            <div style="font-size: 12px; line-height: 1.5;">
                A large day-over-day decrease was detected (<b>{delta_info['cleared_today_count']}</b> previously active SL01 R-bin barcode(s) cleared in today's live WMS snapshot).<br>
                Database status updates and report dispatch remain active &mdash; please confirm whether physical rack clearance/consumption occurred or if WMS extract scope changed.
            </div>
        </div>
        """
    else:
        sharp_drop_banner = ""

    # HTML Summary Body with D89/D98 KPI cards & reference signal columns (100% English)
    html_body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: 'Segoe UI', Calibri, Arial, sans-serif; font-size: 13px; color: #333; }}
            .header {{ background-color: #1F4E78; color: white; padding: 12px 16px; border-radius: 4px; }}
            .kpi-container {{ display: flex; margin: 15px 0; gap: 12px; }}
            .kpi-card {{ background: #F8F9FA; border: 1px solid #D9D9D9; border-left: 4px solid #1F4E78; padding: 8px 14px; min-width: 120px; border-radius: 3px; }}
            .kpi-card.alert {{ border-left-color: #C00000; background: #FFF5F5; }}
            .kpi-card.purple {{ border-left-color: #6B21A8; background: #FAF5FF; }}
            .kpi-card.warn {{ border-left-color: #B25900; background: #FFFBF0; }}
            .kpi-val {{ font-size: 17px; font-weight: bold; color: #1F4E78; margin-top: 4px; }}
            .kpi-val.alert {{ color: #C00000; }}
            .kpi-val.purple {{ color: #6B21A8; }}
            .kpi-val.warn {{ color: #B25900; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 12px; }}
            th {{ background-color: #1F4E78; color: white; padding: 7px 8px; border: 1px solid #ddd; text-align: left; font-size: 11.5px; }}
            td {{ padding: 6px 8px; border: 1px solid #ddd; }}
            tr:nth-child(even) {{ background-color: #F9FAFB; }}
            .badge-crit {{ background-color: #FCE4D6; color: #C00000; font-weight: bold; padding: 2px 5px; border-radius: 3px; }}
            .badge-warn {{ background-color: #FFF2CC; color: #B25900; font-weight: bold; padding: 2px 5px; border-radius: 3px; }}
            .badge-purple {{ background-color: #EDE9FE; color: #6B21A8; font-weight: bold; padding: 2px 5px; border-radius: 3px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2 style="margin: 0; font-size: 16px;">SL01 R-Bin Movement Lag Daily Monitoring Report</h2>
            <div style="font-size: 11px; opacity: 0.85; margin-top: 3px;">
                Plant {_get_ops_env('SAP_PLANT', 'P001')} Headlamp | Scan Date: {scan_date_str} | Target: In-transit Racks Lingering &ge; 2 Business Days
            </div>
        </div>

        {fallback_banner}
        {sharp_drop_banner}

        <p style="margin-top: 12px; line-height: 1.5;">
            Dear Inventory & Warehouse Operations Team,<br><br>
            Please review the list below of materials assigned to <b>SL01 (Main Parts, Plant 1)</b> in WMS that are still physically located in <b>Rack ('R' prefix, Plant 3)</b> storage bins for <b>&ge; 2 business days</b> (sorted by dwell days DESC, then quantity DESC).<br>
            Reference icons indicate SL02 rack status: <b>🔴</b> another part is stored in the same bin in SL02; <b>🟣</b> an identical pallet (same part, quantity, and Lot) exists in strictly 1 other R bin under SL02; <b>🟠</b> the same part remains in the bin in SL02.
        </p>

        <div style="margin: 12px 0;">
            <table style="width: auto; margin: 0;">
                <tr>
                    <td class="kpi-card" style="border: 1px solid #ccc; padding: 8px 14px;">
                        <span style="font-size: 11px; color: #666;">Stalled Pallets (&ge;2d)</span><br>
                        <span class="kpi-val alert">{stale_cnt} Plt</span>
                    </td>
                    <td class="kpi-card alert" style="border: 1px solid #fecaca; padding: 8px 14px; margin-left: 8px;">
                        <span style="font-size: 11px; color: #991b1b;">🔴 Refilled</span><br>
                        <span class="kpi-val alert">{refilled_cnt} Plt</span>
                    </td>
                    <td class="kpi-card purple" style="border: 1px solid #ddd6fe; padding: 8px 14px; margin-left: 8px;">
                        <span style="font-size: 11px; color: #6b21a8;">🟣 Dup Elsewhere</span><br>
                        <span class="kpi-val purple">{dup_cnt} Plt</span>
                    </td>
                    <td class="kpi-card warn" style="border: 1px solid #fed7aa; padding: 8px 14px; margin-left: 8px;">
                        <span style="font-size: 11px; color: #9a3412;">🟠 Same Part</span><br>
                        <span class="kpi-val warn">{same_mat_cnt} Plt</span>
                    </td>
                    <td class="kpi-card" style="border: 1px solid #ccc; padding: 8px 14px; margin-left: 8px;">
                        <span style="font-size: 11px; color: #666;">Stalled Total Qty</span><br>
                        <span class="kpi-val alert">{stale_qty:,.0f} EA</span>
                    </td>
                </tr>
            </table>
        </div>

        <h4 style="color: #1F4E78; margin-bottom: 6px;">📋 SL01 R-Bin Lagging Inspection List (Total: {stale_cnt} Pallets / Boxes)</h4>
        <table>
            <thead>
                <tr>
                    <th>No.</th>
                    <th>Signal</th>
                    <th>Bin</th>
                    <th>Part</th>
                    <th>Description</th>
                    <th>Qty</th>
                    <th>Lot</th>
                    <th>First Seen</th>
                    <th>Days</th>
                    <th>Barcode</th>
                    <th>SL02 in Bin</th>
                    <th>Other Bin</th>
                </tr>
            </thead>
            <tbody>
    """

    if not stale_items:
        html_body += """
                <tr>
                    <td colspan="12" style="text-align: center; padding: 12px; color: #2e7d32; font-weight: bold; background-color: #E2EFDA;">
                        🎉 No lagging SL01 R-bin inventory lingering beyond allowed threshold (&ge; 2 business days). All in-transit transfers are up to date.
                    </td>
                </tr>
        """
    else:
        for i, item in enumerate(stale_items, start=1):
            lag = item["days_in_sl01_r"]
            sig = item.get("sl02_signal", "EMPTY_OR_NO_SL02")
            det = item.get("sl02_details", "-")
            other_bin = item.get("sl02_other_bin", "")

            # D89/D98-2: Reference icon only in Signal column
            if "REFILLED_AND_DUP" in sig:
                sig_badge = "<span style='background-color: #fee2e2; color: #991b1b; padding: 2px 6px; border-radius: 3px; font-weight: bold;'>🔴🟣</span>"
            elif "REFILLED" in sig:
                sig_badge = "<span style='background-color: #fee2e2; color: #991b1b; padding: 2px 6px; border-radius: 3px; font-weight: bold;'>🔴</span>"
            elif "DUP_ELSEWHERE" in sig:
                sig_badge = "<span style='background-color: #ede9fe; color: #6b21a8; padding: 2px 6px; border-radius: 3px; font-weight: bold;'>🟣</span>"
            elif "SAME_MATERIAL_REMAINS" in sig:
                sig_badge = "<span style='background-color: #fef3c7; color: #92400e; padding: 2px 6px; border-radius: 3px; font-weight: bold;'>🟠</span>"
            else:
                sig_badge = "<span style='color: #64748b;'>-</span>"

            badge = f"<span class='badge-crit'>{lag}d</span>" if lag >= 3 else f"<span class='badge-warn'>{lag}d</span>"
            other_bin_str = f"<b style='color: #6b21a8;'>{other_bin}</b>" if other_bin else "-"
            html_body += f"""
                <tr>
                    <td style="text-align: center;">{i}</td>
                    <td style="text-align: center;">{sig_badge}</td>
                    <td style="text-align: center; font-weight: bold;">{item['bin']}</td>
                    <td style="text-align: center; font-weight: bold;">{item['mat_no']}</td>
                    <td>{item.get('mat_desc', '')}</td>
                    <td style="text-align: right; font-weight: bold;">{item['qty']:,.0f}</td>
                    <td style="text-align: center;">{item.get('lot_no', '')}</td>
                    <td style="text-align: center;">{item['first_seen_date']}</td>
                    <td style="text-align: center;">{badge}</td>
                    <td style="text-align: center; font-family: monospace;">{item['barcode']}</td>
                    <td>{det}</td>
                    <td style="text-align: center;">{other_bin_str}</td>
                </tr>
            """

    report_name_str = report_file.name if report_file else "No Attachment"
    html_body += f"""
            </tbody>
        </table>

        <p style="font-size: 11px; color: #555; margin-top: 8px;">
            <b>Legend:</b> 🔴 bin refilled with other part &middot; 🟣 same part/qty/lot in 1 other R bin (SL02) &middot; 🟠 same part remains in bin
        </p>

        <p style="font-size: 11px; color: #777; margin-top: 15px;">
            * Days lag is calculated in business days excluding Sundays (D87-2); list includes items lingering &ge; 2 business days (D98-1).<br>
            * Attached Excel report (<code>{report_name_str}</code>) is formatted for multi-page portrait printing.<br>
            * Automated Dispatch: 05_JOB_SL01_R_Bins pipeline (Mon-Sat 04:15 AM).
        </p>
    </body>
    </html>
    """

    _dept_dir = Path(__file__).resolve().parents[1]
    if str(_dept_dir) not in _sys.path:
        _sys.path.insert(0, str(_dept_dir))
    from report_labels import check_report_language
    check_report_language('05_JOB_sl01_r_bins', subject=subject, html_body=html_body, attachments=[report_file] if report_file else [], is_user_only=test_mode)
    return _dispatch_email(to_email, cc_email, subject, html_body, attachment=report_file)


def _dispatch_email(
    to_email: str,
    cc_email: str,
    subject: str,
    html_body: str,
    attachment: Optional[Path] = None
) -> bool:
    """Dispatches email via Outlook COM automation, falling back to SMTP."""
    _assert_clean_ops_output(subject, html_body, context="05_JOB_SL01_R_Bins email")
    try:
        import win32com.client
        outlook = win32com.client.Dispatch("Outlook.Application")
        try:
            mapi = outlook.GetNamespace("MAPI")
            mapi.Logon("", "", False, False)
        except Exception:
            pass
        mail = outlook.CreateItem(0)
        mail.Subject = subject
        mail.HTMLBody = html_body

        to_formatted = "; ".join([r.strip() for r in to_email.replace(",", ";").split(";") if r.strip()])
        mail.To = to_formatted
        if cc_email:
            cc_formatted = "; ".join([r.strip() for r in cc_email.replace(",", ";").split(";") if r.strip()])
            mail.CC = cc_formatted

        if attachment and attachment.exists():
            mail.Attachments.Add(str(attachment.resolve()))

        mail.Send()
        logger.info("Outlook email successfully sent (recipient masked).")
        return True
    except Exception as e_com:
        logger.warning(f"Outlook COM dispatch failed: {e_com}. Attempting SMTP fallback...")
        try:
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.mime.base import MIMEBase
            from email import encoders

            smtp_server = os.getenv("SMTP_SERVER", "smtp.office365.com")
            smtp_port = int(os.getenv("SMTP_PORT", 587))
            smtp_user = os.getenv("SMTP_USER", "")
            smtp_pass = os.getenv("SMTP_PASS", "")

            if not smtp_user:
                logger.error("No SMTP credentials configured. Email aborted.")
                return False

            msg = MIMEMultipart()
            msg["From"] = smtp_user
            msg["To"] = to_email
            if cc_email:
                msg["Cc"] = cc_email
            msg["Subject"] = subject
            msg.attach(MIMEText(html_body, "html"))

            if attachment and attachment.exists():
                with open(attachment, "rb") as f:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f"attachment; filename={attachment.name}")
                msg.attach(part)

            recipients = [r.strip() for r in to_email.split(",") if r.strip()]
            if cc_email:
                recipients += [r.strip() for r in cc_email.split(",") if r.strip()]

            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_pass)
                server.sendmail(smtp_user, recipients, msg.as_string())

            logger.info("SMTP email successfully sent.")
            return True
        except Exception as e_smtp:
            logger.error(f"Failed to dispatch email via SMTP: {e_smtp}")
            return False


def main():
    parser = argparse.ArgumentParser(description="SL01 R-Bin Movement Lag Monitoring Operator")
    parser.add_argument("--backfill", action="store_true", help="Run historical baseline backfill")
    parser.add_argument("--scan", action="store_true", help="Execute daily scan on latest snapshot")
    parser.add_argument("--report", action="store_true", help="Generate Excel portrait report")
    parser.add_argument("--email", action="store_true", help="Dispatch email notification")
    parser.add_argument("--test", action="store_true", help="Test mode (email sent only to user)")
    parser.add_argument("--to", type=str, default=None, help="Override recipient email")
    parser.add_argument("--all", action="store_true", help="Run scan, report, and email in sequence")
    args = parser.parse_args()

    if args.backfill:
        run_historical_backfill()

    if args.scan or args.all:
        tot, stale, qty, s_date = run_daily_scan()
        rep_path = generate_portrait_excel_report(s_date)
        log_scan_run(
            run_id=f"SCAN_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            scan_date=s_date,
            source_file="latest_wms_stock.xlsx",
            total_active=tot,
            stale_count=stale,
            stale_qty=qty,
            report_file=str(rep_path)
        )
        if args.email or args.all:
            send_outlook_notification(rep_path, s_date, test_mode=args.test, override_to=args.to)
    elif args.report:
        s_date = datetime.now().strftime("%Y-%m-%d")
        rep_path = generate_portrait_excel_report(s_date)
        print(f"Report: {rep_path}")
        if args.email:
            send_outlook_notification(rep_path, s_date, test_mode=args.test, override_to=args.to)
    elif args.email:
        s_date = datetime.now().strftime("%Y-%m-%d")
        rep_path = REPORTS_DIR / "SL01_R_Bins_Lag_Report_latest.xlsx"
        if not rep_path.exists():
            rep_path = generate_portrait_excel_report(s_date)
        send_outlook_notification(rep_path, s_date, test_mode=args.test, override_to=args.to)
    elif not args.backfill:
        parser.print_help()


if __name__ == "__main__":
    main()
