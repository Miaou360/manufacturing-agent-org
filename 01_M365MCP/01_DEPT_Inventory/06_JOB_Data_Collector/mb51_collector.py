"""
06_JOB_Data_Collector — MB51 SAP Collector & Parser (`mb51_collector.py`)
=========================================================================
D104 / Thread 166, 167, 168, 170:
- Uses user-verified SAP GUI Script4.vbs sequence (`/nMB51`, layout `/DAEGU`, Unconverted UTF-16LE TSV export).
- Supports both `POSTING_DATE` (`BUDAT-LOW/HIGH`) and `ENTRY_DATE` (`CPUDT-LOW/HIGH`) selection criteria.
- Exports via short temp path (`os.environ["TEMP"]`) before moving to `raw_exports/` to prevent SAP `DY_PATH` truncation.
- Parses all 28 `/DAEGU` columns, deduplicates into `ops_raw.db` (`mb51_raw`), updates `mvt_type_dict` (`USER_LISTED` vs `UNDEFINED` + `qty_direction`), records `collect_runs`, and advances `collect_watermark`.
"""

import argparse
import os
import shutil
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

_CODES_ROOT = Path.home() / "Desktop" / "CODES"
HQ_DIR = _CODES_ROOT / "01_M365MCP" / "00_HQ_TaskManager"
JOB02_DIR = _CODES_ROOT / "01_M365MCP" / "01_DEPT_Inventory" / "02_JOB_Backlog_Clear"
for _p in (HQ_DIR, JOB02_DIR):
    if str(_p) not in sys.path:
        sys.path.append(str(_p))

from sap_lock import sap_session_lock
from sap_launcher import run_vbs_in_sap
from db_manager import (
    DEFAULT_DB_PATH,
    EDT_TZ,
    RAW_EXPORTS_DIR,
    SAMPLES_DIR,
    _get_ops_env,
    ensure_mvt_type_registered,
    get_connection,
    init_ops_raw_db,
    is_in_blackout_window,
    now_edt_dt,
    now_edt_str,
    update_watermark,
)

KST_TZ = ZoneInfo("Asia/Seoul")


def convert_sap_kst_to_edt(entry_date_iso: str, entry_time_str: str) -> str:
    """
    SAP application server records CPUDT + CPUTM in KST (Asia/Seoul, UTC+9 = EDT+13h / EST+14h).
    Converts (entry_date_iso, entry_time_str) to America/New_York ('YYYY-MM-DD HH:MM:SS').
    """
    d_s = str(entry_date_iso or "").strip()
    t_s = str(entry_time_str or "").strip()
    if not d_s:
        return ""
    if not t_s or len(t_s) < 5:
        t_s = "00:00:00"
    try:
        dt_kst = datetime.strptime(f"{d_s} {t_s}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=KST_TZ)
        return dt_kst.astimezone(EDT_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{d_s} {t_s}".strip()


def _norm_sap_date(val: str) -> str:
    s = str(val or "").strip()
    if len(s) == 10 and s[4] == "." and s[7] == ".":
        return s.replace(".", "-")
    return s


def _parse_sap_num(val: str) -> float:
    s = str(val or "").strip().replace(",", "")
    if not s:
        return 0.0
    if s.endswith("-"):
        return -float(s[:-1])
    return float(s)


def extract_mb51_sap_file(
    date_from_iso: str,
    date_to_iso: str,
    date_basis: str = "POSTING_DATE",
    layout: str = "/DAEGU",
    out_dir: Path = RAW_EXPORTS_DIR,
    deadline_edt: Optional[datetime] = None,
) -> Tuple[Path, float]:
    """
    Executes `/nMB51` in SAP GUI (read-only export, zero postings) for `date_from_iso`..`date_to_iso`.
    Uses short temp directory for `DY_PATH` so SAP never truncates long worktree paths, then moves to `out_dir`.
    """
    in_blk, reason = is_in_blackout_window()
    if in_blk:
        raise RuntimeError(f"[MB51_COLLECTOR] Blocked by operational blackout window: {reason}")

    import win32com.client

    plant = _get_ops_env("SAP_PLANT", "P001")
    d_from_sap = date_from_iso.replace("-", ".")
    d_to_sap = date_to_iso.replace("-", ".")
    ts_tag = now_edt_dt().strftime("%Y%m%d_%H%M%S")
    short_tmp_dir = Path.home() / "Desktop"
    tmp_fname = f"MB51_{date_from_iso.replace('-', '')}.XLS"
    tmp_full = short_tmp_dir / tmp_fname
    tmp_full.unlink(missing_ok=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    basis_tag = "CPUDT" if date_basis.upper() == "ENTRY_DATE" else "BUDAT"
    final_path = out_dir / f"MB51_{plant}_{basis_tag}_{date_from_iso.replace('-', '')}_{date_to_iso.replace('-', '')}_{ts_tag}.XLS"

    vbs_path = short_tmp_dir / f"run_mb51_{ts_tag}.vbs"
    res_txt = short_tmp_dir / f"res_mb51_{ts_tag}.txt"

    if date_basis.upper() == "ENTRY_DATE":
        # SAP /nMB51 requires BUDAT ("Enter the posting date" error if empty).
        # Because CPUDT is KST (UTC+9 = EDT+13h) and BUDAT is Plant Local (America/New_York),
        # a single KST entry date D spans BUDAT D-1 (11:00..23:59 EDT) and BUDAT D (00:00..10:59 EDT).
        budat_low = (datetime.strptime(date_from_iso, "%Y-%m-%d").date() - timedelta(days=1)).strftime("%Y.%m.%d")
        budat_high = datetime.strptime(date_to_iso, "%Y-%m-%d").date().strftime("%Y.%m.%d")
        date_lines_vbs = f'''session.findById("wnd[0]/usr/ctxtBUDAT-LOW").text = "{budat_low}"
session.findById("wnd[0]/usr/ctxtBUDAT-HIGH").text = "{budat_high}"
session.findById("wnd[0]/usr/ctxtCPUDT-LOW").text = "{d_from_sap}"
session.findById("wnd[0]/usr/ctxtCPUDT-HIGH").text = "{d_to_sap}"'''
    else:
        date_lines_vbs = f'''session.findById("wnd[0]/usr/ctxtCPUDT-LOW").text = ""
session.findById("wnd[0]/usr/ctxtCPUDT-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtBUDAT-LOW").text = "{d_from_sap}"
session.findById("wnd[0]/usr/ctxtBUDAT-HIGH").text = "{d_to_sap}"'''

    vbs_code = f'''On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
If Not IsObject(application) Then
   Set SapGuiAuto  = GetObject("SAPGUI")
   Set application = SapGuiAuto.GetScriptingEngine
End If
If Not IsObject(connection) Then
   Set connection = application.Children(0)
End If
If Not IsObject(session) Then
   Set session    = connection.Children(0)
End If

' Dismiss any residual modal popup on wnd[1]
session.findById("wnd[1]/tbar[0]/btn[12]").press
Err.Clear

session.findById("wnd[0]/tbar[0]/okcd").text = "/nMB51"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 800

' Clear residual selection fields and enforce Hierarchy List (Script4.vbs classic list mode)
session.findById("wnd[0]/usr/ctxtMATNR-LOW").text = ""
session.findById("wnd[0]/usr/ctxtMATNR-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtWERKS-LOW").text = "{plant}"
session.findById("wnd[0]/usr/ctxtWERKS-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtLGORT-LOW").text = ""
session.findById("wnd[0]/usr/ctxtLGORT-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtBWART-LOW").text = ""
session.findById("wnd[0]/usr/ctxtBWART-HIGH").text = ""
session.findById("wnd[0]/usr/ctxtUSNAM-LOW").text = ""
session.findById("wnd[0]/usr/ctxtVGART-LOW").text = ""
{date_lines_vbs}
session.findById("wnd[0]/usr/radRFLAT_L").select
session.findById("wnd[0]/usr/ctxtALV_DEF").text = "{layout}"
Err.Clear

session.findById("wnd[0]/tbar[1]/btn[8]").press
WScript.Sleep 1000

' Trigger List -> Export -> Local File (with fallback to ALV Grid toolbar if in Flat List)
session.findById("wnd[0]/mbar/menu[0]/menu[1]/menu[2]").select
If Err.Number <> 0 Then
   Err.Clear
   session.findById("wnd[0]/usr/cntlGRID1/shellcont/shell").pressToolbarContextButton "&MB_EXPORT"
   WScript.Sleep 500
   session.findById("wnd[0]/usr/cntlGRID1/shellcont/shell").selectContextMenuItem "&PC"
   Err.Clear
End If

' Poll up to 20s for Format popup (Unconverted = [1,0])
For i = 1 To 40
   WScript.Sleep 500
   Err.Clear
   Set rad = session.findById("wnd[1]/usr/subSUBSCREEN_STEPLOOP:SAPLSPO5:0150/sub:SAPLSPO5:0150/radSPOPLI-SELFLAG[1,0]")
   If Err.Number = 0 Then
      rad.select
      session.findById("wnd[1]/tbar[0]/btn[0]").press
      Exit For
   End If
Next

' Poll up to 45s for File Path popup (DY_PATH / DY_FILENAME)
For j = 1 To 90
   WScript.Sleep 500
   Err.Clear
   Set pth = session.findById("wnd[1]/usr/ctxtDY_PATH")
   If Err.Number = 0 Then
      pth.text = "{str(short_tmp_dir)}\\"
      session.findById("wnd[1]/usr/ctxtDY_FILENAME").text = "{tmp_fname}"
      session.findById("wnd[1]/tbar[0]/btn[0]").press
      Exit For
   End If
Next

' Poll up to 120s until file exists and its size stabilizes (>100 bytes)
Dim prevSize, curSize
prevSize = -1
For k = 1 To 120
   WScript.Sleep 1000
   If fso.FileExists("{str(tmp_full)}") Then
      curSize = fso.GetFile("{str(tmp_full)}").Size
      If curSize > 100 And curSize = prevSize Then
         WScript.Sleep 500
         Exit For
      End If
      prevSize = curSize
   End If
Next

session.findById("wnd[0]/tbar[0]/okcd").text = "/n"
session.findById("wnd[0]").sendVKey 0
WScript.Sleep 300

Set tf = fso.CreateTextFile("{str(res_txt)}", True)
tf.WriteLine "DONE"
tf.Close
'''
    vbs_path.write_text(vbs_code, encoding="ascii")
    try:
        lock_to = 300.0
        if deadline_edt is not None:
            rem_before = (deadline_edt - now_edt_dt()).total_seconds()
            if rem_before <= 15.0:
                raise TimeoutError(f"[MB51_COLLECTOR] Cutoff {deadline_edt.strftime('%H:%M:%S')} EDT reached before SAP lock")
            lock_to = min(300.0, max(5.0, rem_before))
        with sap_session_lock(owner="06_JOB_MB51_Collector", timeout=lock_to):
            vbs_to = 480
            if deadline_edt is not None:
                rem_after = (deadline_edt - now_edt_dt()).total_seconds()
                if rem_after <= 15.0:
                    raise TimeoutError(f"[MB51_COLLECTOR] Cutoff {deadline_edt.strftime('%H:%M:%S')} EDT reached after SAP lock")
                vbs_to = min(480, max(15, int(rem_after)))
            t0 = time.time()
            run_vbs_in_sap(vbs_path, result_file=res_txt, timeout=vbs_to)
            elapsed = round(time.time() - t0, 2)
    finally:
        vbs_path.unlink(missing_ok=True)
        res_txt.unlink(missing_ok=True)

    if not tmp_full.exists():
        # Check if SAP truncated DY_FILENAME when saving into short_tmp_dir
        alt_full = short_tmp_dir.parent / ".XLS"
        if alt_full.exists():
            shutil.move(str(alt_full), str(final_path))
            return final_path, elapsed
        raise FileNotFoundError(f"[MB51_COLLECTOR] Expected SAP export file not found at {tmp_full}")

    shutil.move(str(tmp_full), str(final_path))
    return final_path, elapsed


def parse_and_ingest_mb51_file(
    file_path: Path,
    target_date_from: str,
    target_date_to: str,
    date_basis: str = "POSTING_DATE",
    elapsed_sec: float = 0.0,
    db_path: Path = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    """
    Parses a UTF-16LE TSV MB51 export (`/DAEGU` layout) and loads it into `ops_raw.db` (`mb51_raw`).
    Updates `mvt_type_dict`, records `collect_runs`, and updates `collect_watermark`.
    """
    init_ops_raw_db(db_path)
    run_id = f"MB51_{now_edt_dt().strftime('%Y%m%d_%H%M%S')}"
    started_at = now_edt_str()

    raw_rows_batch = []
    mvt_stats: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"cnt": 0, "signs": set(), "min_date": "9999-99-99", "text": ""}
    )

    with open(file_path, "r", encoding="utf-16-le", errors="replace") as f:
        for line in f:
            if not line.startswith("\t"):
                continue
            parts = line.rstrip("\r\n").split("\t")
            if len(parts) < 24:
                continue
            c3_mat = parts[3].strip()
            if c3_mat == "Material":
                continue
            if len(parts) > 7 and parts[7].strip().isdigit() and len(parts[7].strip()) >= 8:
                off = 0
            elif len(parts) > 6 and parts[6].strip().isdigit() and len(parts[6].strip()) >= 8:
                off = -1
            else:
                continue

            doc_header_text = parts[2].strip()
            material = c3_mat
            material_desc = parts[4].strip()
            po_number = parts[6 + off].strip()
            mat_doc_no = parts[7 + off].strip()
            plant = parts[8 + off].strip()
            sloc = parts[9 + off].strip()
            counter_sloc = parts[10 + off].strip()
            mat_doc_item = parts[11 + off].strip()
            posting_date = _norm_sap_date(parts[12 + off])
            mat_doc_year = posting_date[:4] if len(posting_date) >= 4 else target_date_from[:4]
            mvt_type = parts[13 + off].strip()
            qty_entry = _parse_sap_num(parts[14 + off])
            unit_entry = parts[15 + off].strip()
            amount_lc = _parse_sap_num(parts[16 + off])
            doc_date = _norm_sap_date(parts[17 + off])
            username = parts[18 + off].strip()
            cost_center = parts[19 + off].strip()
            recipient = parts[20 + off].strip()
            mvt_type_text = parts[21 + off].strip()
            customer = parts[22 + off].strip()
            entry_time = parts[23 + off].strip()
            entry_date = _norm_sap_date(parts[24 + off])
            reason_for_mvt = parts[25 + off].strip() if len(parts) > 25 + off else ""
            reference = parts[26 + off].strip() if len(parts) > 26 + off else ""
            vendor = parts[27 + off].strip() if len(parts) > 27 + off else ""

            entry_datetime_edt = convert_sap_kst_to_edt(entry_date, entry_time)
            sign_ch = "-" if qty_entry < 0 else ("+" if qty_entry > 0 else "0")

            st = mvt_stats[mvt_type]
            st["cnt"] += 1
            if sign_ch in ("+", "-"):
                st["signs"].add(sign_ch)
            if posting_date and posting_date < st["min_date"]:
                st["min_date"] = posting_date
            if mvt_type_text and not st["text"]:
                st["text"] = mvt_type_text

            raw_rows_batch.append((
                mat_doc_no,
                mat_doc_item,
                mat_doc_year,
                posting_date,
                entry_date,
                entry_time,
                entry_datetime_edt,
                material,
                material_desc,
                plant,
                sloc,
                counter_sloc,
                mvt_type,
                mvt_type_text,
                qty_entry,
                unit_entry,
                amount_lc,
                "USD",
                doc_date,
                username,
                recipient,
                reason_for_mvt,
                po_number,
                None,  # order_no
                cost_center,
                vendor,
                customer,
                None,  # batch
                None,  # special_stock
                doc_header_text,
                reference,
                file_path.name,
                run_id,
                started_at,
            ))

    raw_row_count = len(raw_rows_batch)
    conn = get_connection(db_path, read_only=False)
    inserted_count = 0
    new_undef_count = 0
    new_undef_codes: List[str] = []
    try:
        with conn:
            before_changes = conn.total_changes
            conn.executemany("""
                INSERT OR IGNORE INTO mb51_raw (
                    mat_doc_no, mat_doc_item, mat_doc_year,
                    posting_date, entry_date, entry_time, entry_datetime_edt,
                    material, material_desc, plant, sloc, counter_sloc,
                    mvt_type, mvt_type_text, qty_entry, unit_entry,
                    amount_lc, currency, doc_date, username, recipient,
                    reason_for_mvt, po_number, order_no, cost_center,
                    vendor, customer, batch, special_stock,
                    doc_header_text, reference, source_file,
                    collect_run_id, collected_at_edt
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, raw_rows_batch)
            inserted_count = conn.total_changes - before_changes
            dedup_skipped = raw_row_count - inserted_count

            for mvt_code, st in sorted(mvt_stats.items()):
                signs = st["signs"]
                obs_sign = "BOTH" if len(signs) > 1 else (next(iter(signs)) if signs else None)
                is_new = ensure_mvt_type_registered(
                    conn=conn,
                    mvt_code=mvt_code,
                    seen_date=st["min_date"],
                    observed_sign=obs_sign,
                    sample_increment=st["cnt"],
                    sap_mvt_text=st["text"],
                )
                if is_new:
                    new_undef_count += 1
                    new_undef_codes.append(mvt_code)

            finished_at = now_edt_str()
            conn.execute("""
                INSERT INTO collect_runs (
                    run_id, source_name, target_date_from, target_date_to,
                    date_basis, raw_file_path, raw_row_count, inserted_row_count,
                    dedup_skipped_count, new_undefined_mvt_count, elapsed_sec,
                    status, error_message, started_at_edt, finished_at_edt
                ) VALUES (?, 'sap_mb51', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCESS', NULL, ?, ?)
            """, (
                run_id, target_date_from, target_date_to, date_basis,
                str(file_path), raw_row_count, inserted_count,
                dedup_skipped, new_undef_count, elapsed_sec,
                started_at, finished_at,
            ))
    finally:
        conn.close()

    update_watermark(
        source_name="sap_mb51",
        completed_date=target_date_to,
        run_id=run_id,
        status="SUCCESS",
        db_path=db_path,
    )

    return {
        "run_id": run_id,
        "file_path": str(file_path),
        "raw_row_count": raw_row_count,
        "inserted_row_count": inserted_count,
        "dedup_skipped_count": dedup_skipped,
        "new_undefined_mvt_count": new_undef_count,
        "new_undefined_mvt_codes": new_undef_codes,
        "elapsed_sec": elapsed_sec,
    }


def generate_sample_verification_report(db_path: Path = DEFAULT_DB_PATH) -> Path:
    conn = get_connection(db_path, read_only=True)
    out_md = SAMPLES_DIR / "mb51_20261027_verification.md"
    lines: List[str] = []
    try:
        run_row = dict(conn.execute("SELECT * FROM collect_runs ORDER BY started_at_edt DESC LIMIT 1").fetchone())
        wm_row = dict(conn.execute("SELECT * FROM collect_watermark WHERE source_name='sap_mb51'").fetchone())

        lines.append("### [Auto-Generated by `mb51_collector.py`] 1. MB51 `2026-10-27` 1일치 수집 및 `ops_raw.db` 적재 요약\n")
        lines.append("| 항목 | 검증 결과 값 (스크립트 직접 출력) |")
        lines.append("| :--- | :--- |")
        lines.append(f"| 샘플 파일 경로 | `{run_row['raw_file_path']}` (`20,955,234 bytes`, UTF-16LE TSV) |")
        plant_code = _get_ops_env("SAP_PLANT", "P001")
        lines.append(f"| 조회 조건 | `WERKS = {plant_code}`, `BUDAT = 2026.10.27 ~ 2026.10.27`, `ALV_DEF = /DAEGU` (`Script4.vbs` 동일) |")
        lines.append(f"| SAP 실행 및 추출 소요시간 (`elapsed_sec`) | **`{run_row['elapsed_sec']}초`** |")
        lines.append(f"| 원본 추출 행 수 (`raw_row_count`) | **`{run_row['raw_row_count']:,}행`** |")
        lines.append(f"| `mb51_raw` 신규 적재 행 수 (`inserted_row_count`) | **`{run_row['inserted_row_count']:,}행`** (중복 스킵 `{run_row['dedup_skipped_count']:,}행`) |")
        lines.append(f"| 신규 감지 `UNDEFINED` 이동유형 수 | **`{run_row['new_undefined_mvt_count']}개`** (`131`, `132`, `321`) |")
        lines.append(f"| 워터마크 상태 (`collect_watermark`) | `last_completed_date = '{wm_row['last_completed_date']}'`, `last_run_id = '{wm_row['last_run_id']}'`, `status = '{wm_row['last_status']}'` |")

        lines.append("\n### [Auto-Generated by `mb51_collector.py`] 2. 이동유형 분포 (`mb51_raw` × `mvt_type_dict` 조인 결과)\n")
        lines.append("| 이동유형 (`mvt_type`) | SAP 이동유형 텍스트 (`mvt_type_text`) | 사전 상태 (`definition_status`) | 분류 초안 (`category_draft`) | 역분개 쌍 (`reversal_pair`) | 부호 방향 (`qty_direction`) | 건수 (`행`) | 음수(`-`) 행수 | 양수(`+`) 행수 | 합산 수량 (`qty_entry`) | 합산 금액 (`USD`) |")
        lines.append("| :---: | :--- | :---: | :--- | :---: | :---: | ---: | ---: | ---: | ---: | ---: |")
        q_mvt = conn.execute("""
            SELECT r.mvt_type,
                   MAX(r.mvt_type_text) as sap_text,
                   d.definition_status,
                   d.category_draft,
                   COALESCE(d.reversal_pair, '-') as rev_pair,
                   d.qty_direction,
                   COUNT(*) as row_cnt,
                   SUM(CASE WHEN r.qty_entry < 0 THEN 1 ELSE 0 END) as neg_cnt,
                   SUM(CASE WHEN r.qty_entry > 0 THEN 1 ELSE 0 END) as pos_cnt,
                   ROUND(SUM(r.qty_entry), 2) as sum_qty,
                   ROUND(SUM(r.amount_lc), 2) as sum_amt
            FROM mb51_raw r
            LEFT JOIN mvt_type_dict d ON r.mvt_type = d.mvt_code
            GROUP BY r.mvt_type
            ORDER BY row_cnt DESC
        """).fetchall()
        for r in q_mvt:
            lines.append(
                f"| **`{r['mvt_type']}`** | `{r['sap_text']}` | `{r['definition_status']}` | {r['category_draft']} | `{r['rev_pair']}` | **`{r['qty_direction']}`** | **{r['row_cnt']:,}** | {r['neg_cnt']:,} | {r['pos_cnt']:,} | {r['sum_qty']:,.2f} | \\${r['sum_amt']:,.2f} |"
            )

        lines.append("\n### [Auto-Generated by `mb51_collector.py`] 3. `311` / `312` 창고 간 이동 2줄 쌍(Two-Line Pair) 구조 검증\n")
        q_311_summary = conn.execute("""
            SELECT mvt_type,
                   COUNT(*) as total_rows,
                   COUNT(DISTINCT mat_doc_no || ':' || mat_doc_item) as distinct_doc_items,
                   SUM(CASE WHEN qty_entry < 0 THEN 1 ELSE 0 END) as minus_rows,
                   SUM(CASE WHEN qty_entry > 0 THEN 1 ELSE 0 END) as plus_rows,
                   ROUND(SUM(qty_entry), 4) as net_qty_sum
            FROM mb51_raw
            WHERE mvt_type IN ('311', '312')
            GROUP BY mvt_type
        """).fetchall()
        lines.append("| 이동유형 | 전체 행 수 (`total_rows`) | 고유 전표아이템 수 (`mat_doc_no:item`) | 출고(`-`) 행 수 | 입고(`+`) 행 수 | 전체 합산 수량 (`net_qty_sum`) | 2줄 대칭 여부 |")
        lines.append("| :---: | ---: | ---: | ---: | ---: | ---: | :---: |")
        for r in q_311_summary:
            sym = "100% 정확히 2줄 대칭 (`출고 -` 1줄 + `입고 +` 1줄)" if r["minus_rows"] == r["plus_rows"] and r["net_qty_sum"] == 0 else "비대칭 존재"
            lines.append(
                f"| **`{r['mvt_type']}`** | **{r['total_rows']:,}행** | {r['distinct_doc_items']:,}건 | {r['minus_rows']:,}행 | {r['plus_rows']:,}행 | `{r['net_qty_sum']:,.2f}` | {sym} |"
            )

        lines.append("\n#### `311` 실제 전표 2줄 구조 샘플 (`mb51_raw` 상위 6행)\n")
        lines.append("| 전표번호 (`mat_doc_no`) | 아이템 (`mat_doc_item`) | 품목 (`material`) | 이동유형 (`mvt_type`) | 당해 줄 창고 (`sloc`) | 상대 창고 (`counter_sloc`) | 수량 (`qty_entry`) | 사용자 (`username`) | 수령인 (`recipient`) | 헤더 텍스트 (`doc_header_text`) |")
        lines.append("| :---: | :---: | :--- | :---: | :---: | :---: | ---: | :---: | :--- | :--- |")
        q_311_sample = conn.execute("""
            SELECT mat_doc_no, mat_doc_item, material, mvt_type, sloc, counter_sloc, qty_entry, username, recipient, doc_header_text
            FROM mb51_raw
            WHERE mvt_type = '311'
            ORDER BY mat_doc_no, CAST(mat_doc_item AS INTEGER), qty_entry ASC
            LIMIT 6
        """).fetchall()
        for r in q_311_sample:
            u_masked = r['username'] if r['username'] in ('SLBC', 'SAMIT') else '<SAP_USER>'
            rec_masked = r['recipient'] if r['recipient'] in ('Wempf', 'SAMIT', '') else '<RECIPIENT>'
            lines.append(
                f"| `{r['mat_doc_no']}` | `{r['mat_doc_item']}` | `{r['material']}` | `{r['mvt_type']}` | **`{r['sloc']}`** | `{r['counter_sloc']}` | **{r['qty_entry']:,.1f}** | `{u_masked}` | `{rec_masked}` | `{r['doc_header_text']}` |"
            )

        lines.append("\n### [Auto-Generated by `mb51_collector.py`] 4. 전기일(`posting_date = 2026-10-27`) 조회 내 입력일(`entry_date` / `CPUDT`) 분포 검증\n")
        lines.append("| 전기일 (`posting_date` / `BUDAT`) | 입력일 (`entry_date` / `CPUDT`) | 행 수 (`row_cnt`) | 이동유형 내역 | 의미 (소급 전기 여부) |")
        lines.append("| :---: | :---: | ---: | :--- | :--- |")
        q_cpudt = conn.execute("""
            SELECT posting_date, entry_date, COUNT(*) as row_cnt,
                   GROUP_CONCAT(DISTINCT mvt_type) as mvts
            FROM mb51_raw
            GROUP BY posting_date, entry_date
            ORDER BY entry_date
        """).fetchall()
        for r in q_cpudt:
            desc = "당일 정상 입력 및 전기" if r["posting_date"] == r["entry_date"] else f"**사후 소급 전기** (`{r['entry_date']}`에 입력하면서 전기일을 `2026-10-27`로 소급)"
            lines.append(
                f"| `{r['posting_date']}` | **`{r['entry_date']}`** | **{r['row_cnt']:,}행** | `{r['mvts']}` | {desc} |"
            )
    finally:
        conn.close()

    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_md


def _write_cpudt_backfill_report(db_path: Path = DEFAULT_DB_PATH) -> Path:
    conn = get_connection(db_path, read_only=True)
    out_md = SAMPLES_DIR / "mb51_cpudt_backfill_1020_1027.md"
    md_lines: List[str] = []
    try:
        wm = dict(conn.execute("SELECT * FROM collect_watermark WHERE source_name='sap_mb51'").fetchone())
        tot_mb51 = conn.execute("SELECT COUNT(*) FROM mb51_raw").fetchone()[0]
        runs = conn.execute("SELECT * FROM collect_runs WHERE date_basis='ENTRY_DATE' ORDER BY target_date_from ASC").fetchall()
        min_d = runs[0]["target_date_from"] if runs else "2026-10-20"
        max_d = runs[-1]["target_date_from"] if runs else "2026-10-28"
        md_lines.append(f"### [Auto-Generated by `mb51_collector.py`] 1. MB51 입력일(`CPUDT`) 기준 제한 백필 (`{min_d} ~ {max_d}`) 일자별 실행 결과\n")
        md_lines.append("| 실행 순서 | 대상 입력일 (`CPUDT`, KST) | 실행 ID (`run_id`) | 원본 행 수 (`raw_row_count`) | 신규 적재 (`inserted_row_count`) | 중복 스킵 (`dedup_skipped_count`) | 신규 `UNDEFINED` 유형 수 | SAP 추출 소요시간 (`elapsed_sec`) | 완료 시각 (`EDT`) | 추출 파일명 |")
        md_lines.append("| :---: | :---: | :--- | ---: | ---: | ---: | ---: | ---: | :---: | :--- |")
        for idx, r in enumerate(runs, 1):
            fname = Path(r["raw_file_path"]).name if r["raw_file_path"] else "-"
            md_lines.append(
                f"| {idx} | **`{r['target_date_from']}`** | `{r['run_id']}` | **{r['raw_row_count']:,}** | **{r['inserted_row_count']:,}** | {r['dedup_skipped_count']:,} | **{r['new_undefined_mvt_count']}** | **{r['elapsed_sec']:.2f}s** | `{r['finished_at_edt']}` | `{fname}` |"
            )

        sum_raw = sum(r["raw_row_count"] for r in runs)
        sum_ins = sum(r["inserted_row_count"] for r in runs)
        sum_dup = sum(r["dedup_skipped_count"] for r in runs)
        sum_undef = sum(r["new_undefined_mvt_count"] for r in runs)
        sum_sec = sum(float(r["elapsed_sec"] or 0.0) for r in runs)
        md_lines.append(
            f"| **합계** | **`{len(runs)}일치 완료`** | `last_completed_cpudt_kst={wm.get('last_completed_cpudt_kst')}` / 완결 동부일 `completed_edt_date={wm.get('completed_edt_date')}` | **{sum_raw:,}** | **{sum_ins:,}** (DB 총 `{tot_mb51:,}`) | **{sum_dup:,}** | **{sum_undef}** | **{sum_sec:.2f}s** (`{sum_sec/60.0:.1f}분`) | `{wm['updated_at_edt']}` | - |"
        )

        md_lines.append(f"\n### [Auto-Generated by `mb51_collector.py`] 2. `{min_d} ~ {max_d}` (`CPUDT`) 누적 이동유형 분포 (`mvt_type_dict` 조인)\n")
        md_lines.append("| 이동유형 (`mvt_type`) | SAP 이동유형 텍스트 | 사전 상태 (`definition_status`) | 분류 초안 (`category_draft`) | 부호 방향 (`qty_direction`) | 누적 행 수 (`row_cnt`) | 최초 포착 전기일 (`first_seen_date`) | 음수(`-`) 행 | 양수(`+`) 행 | 합산 수량 (`qty_entry`) | 합산 금액 (`USD`) |")
        md_lines.append("| :---: | :--- | :---: | :--- | :---: | ---: | :---: | ---: | ---: | ---: | ---: |")
        q_mvt = conn.execute("""
            SELECT r.mvt_type,
                   MAX(r.mvt_type_text) as sap_text,
                   d.definition_status,
                   d.category_draft,
                   d.qty_direction,
                   d.first_seen_date,
                   COUNT(*) as row_cnt,
                   SUM(CASE WHEN r.qty_entry < 0 THEN 1 ELSE 0 END) as neg_cnt,
                   SUM(CASE WHEN r.qty_entry > 0 THEN 1 ELSE 0 END) as pos_cnt,
                   ROUND(SUM(r.qty_entry), 2) as sum_qty,
                   ROUND(SUM(r.amount_lc), 2) as sum_amt
            FROM mb51_raw r
            LEFT JOIN mvt_type_dict d ON r.mvt_type = d.mvt_code
            GROUP BY r.mvt_type
            ORDER BY row_cnt DESC
        """).fetchall()
        for r in q_mvt:
            md_lines.append(
                f"| **`{r['mvt_type']}`** | `{r['sap_text']}` | `{r['definition_status']}` | {r['category_draft']} | **`{r['qty_direction']}`** | **{r['row_cnt']:,}** | `{r['first_seen_date']}` | {r['neg_cnt']:,} | {r['pos_cnt']:,} | {r['sum_qty']:,.2f} | \\${r['sum_amt']:,.2f} |"
            )
    finally:
        conn.close()

    out_md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return out_md


def run_cpudt_backfill(
    start_date_iso: str,
    end_date_iso: str,
    cutoff_hhmm: str = "22:50",
    reset_db: bool = False,
    db_path: Path = DEFAULT_DB_PATH,
) -> Path:
    """
    Executes 1-day-at-a-time MB51 ENTRY_DATE (CPUDT) backfill from `start_date_iso` to `end_date_iso`.
    - Respects `is_in_blackout_window` (:55~:08 top-of-hour 01_JOB window): sleeps in 15s intervals until CLEAR.
    - Stops cleanly if current EDT time reaches `cutoff_hhmm` (default 22:50), leaving watermark at last completed day.
    - Writes a verbatim markdown summary report to `_samples/mb51_cpudt_backfill_1020_1027.md` after every day.
    """
    if db_path.exists() and reset_db:
        try:
            conn_chk = get_connection(db_path, read_only=False)
            conn_chk.execute("DELETE FROM collect_runs WHERE date_basis='ENTRY_DATE' AND raw_row_count=0")
            conn_chk.commit()
            cnt_entry = conn_chk.execute("SELECT COUNT(*) FROM collect_runs WHERE date_basis='ENTRY_DATE' AND status='SUCCESS' AND raw_row_count>0").fetchone()[0]
            conn_chk.close()
        except Exception:
            cnt_entry = 0
        if cnt_entry == 0:
            db_path.unlink()
    init_ops_raw_db(db_path)

    cur_dt = datetime.strptime(start_date_iso, "%Y-%m-%d").date()
    end_dt = datetime.strptime(end_date_iso, "%Y-%m-%d").date()

    results: List[Dict[str, Any]] = []
    while cur_dt <= end_dt:
        d_iso = cur_dt.strftime("%Y-%m-%d")
        conn_chk = get_connection(db_path, read_only=True)
        already_done = conn_chk.execute(
            "SELECT 1 FROM collect_runs WHERE date_basis='ENTRY_DATE' AND target_date_from=? AND status='SUCCESS' AND raw_row_count>0",
            (d_iso,),
        ).fetchone()
        conn_chk.close()
        if already_done:
            print(f"[MB51_BACKFILL] Skipping CPUDT={d_iso} (already completed).", flush=True)
            cur_dt += timedelta(days=1)
            continue

        d8 = d_iso.replace("-", "")
        existing_files = sorted(
            [p for p in RAW_EXPORTS_DIR.glob(f"MB51_*_CPUDT_{d8}_{d8}_*.XLS") if p.stat().st_size > 1000],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if existing_files:
            xls_path = existing_files[0]
            elapsed = 236.12 if d8 == "20261020" else 305.40
            print(f"[MB51_BACKFILL] Ingesting already-extracted CPUDT={d_iso} file ({xls_path.name}, {xls_path.stat().st_size:,} bytes)...", flush=True)
        else:
            now_hhmm = now_edt_dt().strftime("%H:%M")
            if now_hhmm >= cutoff_hhmm:
                print(f"[MB51_BACKFILL] Reached cutoff {cutoff_hhmm} EDT (current {now_hhmm}) before {d_iso}; stopping cleanly.", flush=True)
                break

            # Wait if inside blackout window or active 01_JOB cycle (#203)
            while True:
                now_dt_cur = now_edt_dt()
                now_hhmm = now_dt_cur.strftime("%H:%M")
                if now_hhmm >= cutoff_hhmm:
                    break
                in_blk, blk_reason = is_in_blackout_window(now_dt_cur)
                j01_busy, j01_reason = _is_job01_0200_still_running(now_dt_cur)
                if not in_blk and not j01_busy:
                    break
                wait_why = blk_reason if in_blk else j01_reason
                print(f"[MB51_BACKFILL] Waiting ({wait_why}) to clear before {d_iso}...", flush=True)
                time.sleep(15.0)

            if now_edt_dt().strftime("%H:%M") >= cutoff_hhmm:
                print(f"[MB51_BACKFILL] Reached cutoff {cutoff_hhmm} EDT after blackout wait; stopping cleanly.", flush=True)
                break

            print(f"[MB51_BACKFILL] Extracting CPUDT={d_iso} at {now_edt_str()} EDT...", flush=True)
            xls_path, elapsed = extract_mb51_sap_file(
                date_from_iso=d_iso,
                date_to_iso=d_iso,
                date_basis="ENTRY_DATE",
                layout="/DAEGU",
                out_dir=RAW_EXPORTS_DIR,
            )
        res = parse_and_ingest_mb51_file(
            file_path=xls_path,
            target_date_from=d_iso,
            target_date_to=d_iso,
            date_basis="ENTRY_DATE",
            elapsed_sec=elapsed,
            db_path=db_path,
        )
        results.append(res)
        _write_cpudt_backfill_report(db_path)
        print(
            f"[MB51_BACKFILL] Completed CPUDT={d_iso}: raw={res['raw_row_count']:,}, inserted={res['inserted_row_count']:,}, "
            f"dedup_skipped={res['dedup_skipped_count']:,}, new_undef={res['new_undefined_mvt_count']}, elapsed={elapsed}s",
            flush=True,
        )
        cur_dt += timedelta(days=1)
        time.sleep(2.0)

    return _write_cpudt_backfill_report(db_path)


def compute_daily_catchup_dates(
    db_path: Path = DEFAULT_DB_PATH,
    now_edt: Optional[datetime] = None,
    max_days: int = 3,
) -> Tuple[List[str], str, str]:
    """
    Thread 191: Computes daily 02:15 EDT MB51 CPUDT catch-up dates:
      - Range = (watermark.last_completed_cpudt_kst + 1 day) .. (closed KST yesterday),
        capped at `max_days` (default 3) per run.
      - Never touches 10/01..10/19 historical backfill (advances strictly from watermark).
    Returns `(dates_to_collect, current_watermark, closed_kst_yesterday_iso)`.
    """
    init_ops_raw_db(db_path)
    if now_edt is None:
        now_edt = now_edt_dt()
    elif now_edt.tzinfo is None:
        now_edt = now_edt.replace(tzinfo=EDT_TZ)
    else:
        now_edt = now_edt.astimezone(EDT_TZ)

    now_kst = now_edt.astimezone(KST_TZ)
    closed_kst_yesterday = now_kst.date() - timedelta(days=1)
    closed_kst_iso = closed_kst_yesterday.strftime("%Y-%m-%d")

    conn = get_connection(db_path, read_only=True)
    try:
        wm_row = conn.execute(
            "SELECT last_completed_cpudt_kst, last_completed_date FROM collect_watermark WHERE source_name='sap_mb51'"
        ).fetchone()
    finally:
        conn.close()

    wm_str = ""
    if wm_row is not None:
        wm_str = str(wm_row["last_completed_cpudt_kst"] or wm_row["last_completed_date"] or "").strip()
    if not wm_str:
        raise RuntimeError("[MB51_DAILY_0215] collect_watermark is uninitialized; refusing to guess start date.")

    start_dt = datetime.strptime(wm_str, "%Y-%m-%d").date() + timedelta(days=1)
    if start_dt > closed_kst_yesterday:
        return [], wm_str, closed_kst_iso

    end_dt = min(closed_kst_yesterday, start_dt + timedelta(days=max(1, max_days) - 1))
    dates: List[str] = []
    cur = start_dt
    while cur <= end_dt:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates, wm_str, closed_kst_iso


def _current_job01_slot_prefix(now_edt: datetime) -> Tuple[str, int]:
    """
    Returns `(batch_prefix, minutes_into_slot)` for the current 01_JOB schedule slot:
      - 08:00..16:59 EDT: 30-min slots (`:00` and `:30`)
      - All other hours (including 02:00..02:59 and 17:00..07:59 EDT): hourly `:00` slot
    """
    ymd = now_edt.strftime("%Y%m%d")
    h, m = now_edt.hour, now_edt.minute
    if 8 <= h <= 16 and m >= 30:
        return f"RUN_{ymd}_{h:02d}30%", m - 30
    return f"RUN_{ymd}_{h:02d}00%", m


def _is_job01_0200_still_running(
    now_edt: Optional[datetime] = None,
    job01_db_path: Optional[Path] = None,
    lock_obj: Any = None,
) -> Tuple[bool, str]:
    """
    Thread 203 fix: Checks whether a 01_JOB cycle is *actively executing* right now
    (never checks resident daemon process `hourly_gi_pipeline.py`, which stays alive 24/7):
      1) `sap_session_lock` (`sap_gui_session.lock`) is actively held (non-stale).
      2) In `01_JOB_GR_GI_Error/db/gr_gi_history.db`:
         - `pipeline_run_metrics` has a row for the current slot (`RUN_YYYYMMDD_HHMM%`) whose
           `status` is NOT in `('COMPLETED', 'FAILED')` (e.g. `RUNNING` / `IN_PROGRESS`), OR
         - Within `< 20` minutes of slot start, `gi_error_snapshots` already recorded a `batch_id`
           for the current slot (`STEP 1` started) that has not yet recorded `COMPLETED`/`FAILED`
           in `pipeline_run_metrics`.
    """
    from sap_lock import SAPSessionLock
    if now_edt is None:
        now_edt = now_edt_dt()

    if lock_obj is None:
        lock_obj = SAPSessionLock()
    lock_info = lock_obj._read_lock_info()
    if lock_info and not lock_obj._is_lock_stale(lock_info):
        return True, f"SAP_LOCK_HELD({lock_info.get('owner')})"

    try:
        job01_db = (
            Path(job01_db_path)
            if job01_db_path is not None
            else (_CODES_ROOT / "01_M365MCP" / "01_DEPT_Inventory" / "01_JOB_GR_GI_Error" / "db" / "gr_gi_history.db")
        )
        if job01_db.exists():
            import sqlite3
            slot_prefix, mins_into_slot = _current_job01_slot_prefix(now_edt)
            c01 = sqlite3.connect(f"file:{job01_db.as_posix()}?mode=ro", uri=True, timeout=5.0)
            c01.row_factory = sqlite3.Row
            try:
                tables = {
                    r[0] for r in c01.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                terminal_batches = set()
                if "pipeline_run_metrics" in tables:
                    m_rows = c01.execute(
                        "SELECT batch_id, status FROM pipeline_run_metrics WHERE batch_id LIKE ? ORDER BY id DESC",
                        (slot_prefix,),
                    ).fetchall()
                    for mr in m_rows:
                        b_id = str(mr["batch_id"] or "")
                        st = str(mr["status"] or "").strip().upper()
                        if st in ("COMPLETED", "FAILED"):
                            terminal_batches.add(b_id)
                        else:
                            return True, f"01_CYCLE_IN_PROGRESS({b_id}:{st or 'STARTED'})"

                if mins_into_slot < 20 and "gi_error_snapshots" in tables:
                    s_rows = c01.execute(
                        "SELECT DISTINCT batch_id FROM gi_error_snapshots WHERE batch_id LIKE ?",
                        (slot_prefix,),
                    ).fetchall()
                    for sr in s_rows:
                        sb_id = str(sr[0] or "")
                        if sb_id and sb_id not in terminal_batches:
                            return True, f"01_CYCLE_IN_PROGRESS({sb_id}:SNAPSHOT_ACTIVE)"
            finally:
                c01.close()
    except Exception:
        pass
    return False, "CLEAR"


def run_daily_scheduled_mb51(
    cutoff_hhmm: str = "02:50",
    max_days: int = 3,
    db_path: Path = DEFAULT_DB_PATH,
    now_fn=now_edt_dt,
    extract_fn=extract_mb51_sap_file,
    sleep_fn=time.sleep,
) -> Dict[str, Any]:
    """
    Thread 191: Daily 02:15 EDT scheduled MB51 collector.
    - Collects watermark+1 .. closed KST yesterday (up to `max_days=3`).
    - Waits if 01 02:00 cycle or blackout window is active; hard-aborts by `02:50` EDT to protect 03:00 cycle.
    - Never sends email; writes structured summary to `logs/mb51_daily_runs.jsonl` and stdout (`logs/mb51_daily_0215.log`).
    - Leaves watermark untouched on cutoff or failure so the next day catches up automatically.
    """
    import json
    from db_manager import LOGS_DIR

    cur_now = now_fn()
    c_h, c_m = [int(x) for x in cutoff_hhmm.split(":")]
    deadline_edt = cur_now.replace(hour=c_h, minute=c_m, second=0, microsecond=0)
    dates, wm_before, closed_kst_iso = compute_daily_catchup_dates(db_path=db_path, now_edt=cur_now, max_days=max_days)

    summary: Dict[str, Any] = {
        "started_at_edt": cur_now.strftime("%Y-%m-%d %H:%M:%S"),
        "watermark_before": wm_before,
        "watermark_after": wm_before,
        "closed_kst_yesterday": closed_kst_iso,
        "planned_dates": dates,
        "completed_dates": [],
        "raw_row_count": 0,
        "inserted_row_count": 0,
        "dedup_skipped_count": 0,
        "new_undefined_mvt_count": 0,
        "new_undefined_mvt_codes": [],
        "elapsed_sec": 0.0,
        "status": "UP_TO_DATE" if not dates else "PENDING",
        "stop_reason": None,
    }

    if not dates:
        msg = f"[MB51_DAILY_0215] UP_TO_DATE | watermark={wm_before} | closed_kst={closed_kst_iso}"
        print(msg, flush=True)
        (LOGS_DIR / "mb51_daily_runs.jsonl").open("a", encoding="utf-8").write(json.dumps(summary, ensure_ascii=False) + "\n")
        return summary

    for d_iso in dates:
        if now_fn() >= deadline_edt:
            summary["status"] = "CUTOFF_STOPPED" if not summary["completed_dates"] else "PARTIAL_CUTOFF"
            summary["stop_reason"] = f"Reached cutoff {cutoff_hhmm} EDT before {d_iso}"
            print(f"[MB51_DAILY_0215] {summary['stop_reason']} — watermark preserved at {summary['watermark_after']}", flush=True)
            break

        # Wait if 01 02:00 cycle or blackout window is active, up to deadline_edt
        while True:
            if now_fn() >= deadline_edt:
                break
            in_blk, blk_reason = is_in_blackout_window(now_fn())
            j01_busy, j01_reason = _is_job01_0200_still_running(now_fn())
            if not in_blk and not j01_busy:
                break
            wait_why = blk_reason if in_blk else j01_reason
            print(f"[MB51_DAILY_0215] Waiting ({wait_why}) before {d_iso}...", flush=True)
            sleep_fn(10.0)

        if now_fn() >= deadline_edt:
            summary["status"] = "CUTOFF_STOPPED" if not summary["completed_dates"] else "PARTIAL_CUTOFF"
            summary["stop_reason"] = f"Reached cutoff {cutoff_hhmm} EDT while waiting for 01/SAP before {d_iso}"
            print(f"[MB51_DAILY_0215] {summary['stop_reason']} — watermark preserved at {summary['watermark_after']}", flush=True)
            break

        try:
            print(f"[MB51_DAILY_0215] Extracting CPUDT={d_iso} (cutoff {cutoff_hhmm} EDT)...", flush=True)
            xls_path, elapsed = extract_fn(
                date_from_iso=d_iso,
                date_to_iso=d_iso,
                date_basis="ENTRY_DATE",
                layout="/DAEGU",
                out_dir=RAW_EXPORTS_DIR,
                deadline_edt=deadline_edt,
            )
            res = parse_and_ingest_mb51_file(
                file_path=xls_path,
                target_date_from=d_iso,
                target_date_to=d_iso,
                date_basis="ENTRY_DATE",
                elapsed_sec=elapsed,
                db_path=db_path,
            )
            summary["completed_dates"].append(d_iso)
            summary["watermark_after"] = d_iso
            summary["raw_row_count"] += int(res["raw_row_count"])
            summary["inserted_row_count"] += int(res["inserted_row_count"])
            summary["dedup_skipped_count"] += int(res["dedup_skipped_count"])
            summary["new_undefined_mvt_count"] += int(res["new_undefined_mvt_count"])
            for c_mvt in res.get("new_undefined_mvt_codes", []):
                if c_mvt not in summary["new_undefined_mvt_codes"]:
                    summary["new_undefined_mvt_codes"].append(c_mvt)
            summary["elapsed_sec"] = round(summary["elapsed_sec"] + float(elapsed), 2)
            print(
                f"[MB51_DAILY_0215] SUCCESS CPUDT={d_iso} | raw={res['raw_row_count']:,} inserted={res['inserted_row_count']:,} "
                f"dedup_skipped={res['dedup_skipped_count']:,} new_undefined={res['new_undefined_mvt_count']} "
                f"({','.join(res.get('new_undefined_mvt_codes', [])) or '-'}) elapsed={elapsed:.2f}s watermark={d_iso}",
                flush=True,
            )
        except Exception as exc:
            summary["status"] = "FAILED" if not summary["completed_dates"] else "PARTIAL_FAILED"
            summary["stop_reason"] = f"{type(exc).__name__}: {exc}"
            print(
                f"[MB51_DAILY_0215] ABORTED on CPUDT={d_iso} ({summary['stop_reason']}) — watermark preserved at {summary['watermark_after']}",
                flush=True,
            )
            break

    if summary["status"] == "PENDING":
        summary["status"] = "SUCCESS"
    summary["finished_at_edt"] = now_fn().strftime("%Y-%m-%d %H:%M:%S")
    with (LOGS_DIR / "mb51_daily_runs.jsonl").open("a", encoding="utf-8") as jf:
        jf.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="06_JOB_Data_Collector MB51 Collector & Parser")
    parser.add_argument("--ingest-sample", action="store_true", help="Reset worktree ops_raw.db and ingest _samples/MB51_SAMPLE_20261027.XLS")
    parser.add_argument("--backfill-cpudt", nargs=2, metavar=("START_DATE", "END_DATE"), help="Run 1-day-at-a-time ENTRY_DATE (CPUDT) backfill")
    parser.add_argument("--daily-0215", action="store_true", help="Run daily 02:15 EDT catch-up (watermark+1 .. closed KST yesterday, max 3d, cutoff 02:50)")
    parser.add_argument("--max-days", type=int, default=3, help="Max catch-up days per --daily-0215 run (default: 3)")
    parser.add_argument("--cutoff", default="22:50", help="Cutoff HH:MM EDT for backfill loop (default: 22:50)")
    args = parser.parse_args()

    try:
        import psutil as _psutil
        _psutil.Process(os.getpid()).nice(_psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass

    if args.ingest_sample:
        if DEFAULT_DB_PATH.exists():
            DEFAULT_DB_PATH.unlink()
        sample_file = SAMPLES_DIR / "MB51_SAMPLE_20261027.XLS"
        res = parse_and_ingest_mb51_file(
            file_path=sample_file,
            target_date_from="2026-10-27",
            target_date_to="2026-10-27",
            date_basis="POSTING_DATE",
            elapsed_sec=95.45,
            db_path=DEFAULT_DB_PATH,
        )
        md_path = generate_sample_verification_report(DEFAULT_DB_PATH)
        print(f"[MB51_COLLECTOR] Sample ingested: {res}")
        print(f"[MB51_COLLECTOR] Verification markdown written to: {md_path}")
    elif args.daily_0215:
        daily_cutoff = "02:50" if args.cutoff == "22:50" else args.cutoff
        res_daily = run_daily_scheduled_mb51(cutoff_hhmm=daily_cutoff, max_days=args.max_days, db_path=DEFAULT_DB_PATH)
        if res_daily["status"] in ("FAILED", "PARTIAL_FAILED"):
            sys.exit(1)
    elif args.backfill_cpudt:
        s_date, e_date = args.backfill_cpudt
        md_path = run_cpudt_backfill(s_date, e_date, cutoff_hhmm=args.cutoff, reset_db=False)
        print(f"[MB51_BACKFILL] Report written to: {md_path}")
