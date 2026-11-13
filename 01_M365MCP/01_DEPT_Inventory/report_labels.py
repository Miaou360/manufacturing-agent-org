"""
Shared report language labels & Hangul guard (D111 / handoff #207)
==================================================================
- D111: Any report/email/attachment whose recipients include anyone other than the user
  must be written entirely in English. User-only reports (TEST_EMAIL_RECIPIENT, [TEST], [수시],
  03 user-only analysis) may use Korean.
- `EN`: single source of English display labels for cause codes shared across reports
  (code / DB values stay as-is; only display labels are language-separated).
- `find_hangul()` / `assert_no_hangul()`: used by unit tests (render -> 0 Hangul chars).
- `check_report_language()`: called right before operational dispatch. It NEVER blocks sending;
  on detection it logs a warning and writes `.agents/handoff/ALERT_LANG_<context>_<ts>.md`.
"""

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("ReportLanguageGuard")

HANGUL_RE = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")

_CODES_ROOT = Path(__file__).resolve().parents[2]
ALERT_DIR = _CODES_ROOT / ".agents" / "handoff"

# -----------------------------------------------------------------------------
# English display labels (D108 root cause, D109, D107, common report terms)
# -----------------------------------------------------------------------------
EN: Dict[str, str] = {
    # D108 GI-linked backlog root cause (02 / 01 / 03 shared)
    "D108_D": "Stock shortage (D)",
    "D108_D_D107": "Stock shortage (D) — D107 delete candidate",
    "D108_A": "Book mismatch (A)",
    "D108_F": "Suspected missing GR (F)",
    "D108_F_REVIEW": "Suspected missing GR (F) — human review",
    "D108_B": "Pending GR (B)",
    "D108_ANOMALY": "⚠️ Anomaly",
    "D108_ANOMALY_NEW_AFTER_01": "⚠️ Anomaly — 01 marked resolvable, still remaining (① New error: pending next 01 verdict)",
    "D108_ANOMALY_POSTING_FAILED": "⚠️ Anomaly — 01 marked resolvable, still remaining (③ 01 posting failed)",
    "D108_ANOMALY_POSTED_BUT_REMAINED": "⚠️ Anomaly — 01 marked resolvable, still remaining (③ Remained in ZTCODE_STOCK_REC after posting)",
    "D108_ANOMALY_APPROVED_UNPOSTED": "⚠️ Anomaly — 01 marked resolvable, still remaining (② Approved but unposted)",
    "D108_ANOMALY_D109_RESIDUAL": "⚠️ Anomaly — GI error remained after D109 guard-release posting (retry blocked · human review)",
    "D108_ANOMALY_GUARD_UNPOSTED": "⚠️ Anomaly — 01 marked resolvable, still remaining (② Guard held)",
    "D108_CLEARED": "GI link cleared (re-evaluate)",
    "D108_UNCLASSIFIED": "GI-linked (unclassified)",
    # Severities
    "SEV_CRITICAL": "🔴 Critical",
    "SEV_CRITICAL_REVIEW": "🔴 Critical (Human review)",
    "SEV_TEMPORARY": "🟡 Temporary",
    "SEV_CHECK_BUG": "🔴 Check (Bug candidate)",
    "SEV_CHECK_BUG_REVIEW": "🔴 Check (Bug / Human review)",
    "SEV_CHECK_BUG_POLICY": "🔴 Check (Bug / Policy candidate)",
    "SEV_EXCLUDED": "⚪ Excluded",
    "SEV_CLEARED": "⚪ Cleared",
    # Banners / common
    "CORRECTION": "[Correction]",
    "CORRECTION_DEFAULT_NOTE": "Updated root-cause verdicts for GI error-linked backlogs",
    "SL49_QUALITY_DMA": "SL49 [Quality DMA]",
    "SL49_NOT_COUNTED": "SL49 (Quality DMA · Not a cycle-count target)",
    "SUSTAINED_DEFICIT": "Sustained shortage",
    "NEW_OR_PENDING": "New / pending interface",
    "GR_MISSING_CHECK_F": "GR Missing Check (F)",
    "FORCED_WEEKLY_GR_REASON": "Suspected missing GR (F) — verify SAP GR",
    "CYCLE_30M_DAY": "30-min cycle (Daytime)",
    "CYCLE_1H_NIGHT": "1-hour cycle (Night)",
}


def label(key: str, default: Optional[str] = None) -> str:
    """Returns English display label for `key` (falls back to `default` or the key itself)."""
    return EN.get(key, default if default is not None else key)


# B-3 (#216): Raw SAP material master description columns (MAKTX / Description) may contain
# vendor/OEM Hangul tokens (e.g. '외장', '줄') from source ERP master data. Only cell values
# under these exact English column headers are exempted from the Hangul guard; headers and
# all other columns remain strictly checked.
EXEMPT_RAW_DESC_HEADERS = frozenset({
    "description",
    "mat.desc",
    "mat_desc",
    "mat desc",
    "material description",
    "maktx",
})

_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_HTML_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<(th|td)\b([^>]*)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_exempt_html_description_cells(html_text: str) -> str:
    """Masks ONLY `<td>` bodies under `EXEMPT_RAW_DESC_HEADERS` `<th>` columns so `find_hangul` ignores raw MAKTX."""
    if not html_text or "<table" not in html_text.lower():
        return html_text

    def _clean_table(m_tbl: re.Match) -> str:
        tbl = m_tbl.group(0)
        exempt_cols: set = set()

        def _clean_tr(m_tr: re.Match) -> str:
            nonlocal exempt_cols
            tr_full = m_tr.group(0)
            cells = list(_HTML_CELL_RE.finditer(tr_full))
            if not cells:
                return tr_full
            has_th = any(c.group(1).lower() == "th" for c in cells)
            if has_th:
                new_exempt = set()
                col_idx = 0
                for c in cells:
                    attrs = c.group(2) or ""
                    cs_m = re.search(r'colspan\s*=\s*["\']?(\d+)', attrs, re.IGNORECASE)
                    span = int(cs_m.group(1)) if cs_m else 1
                    hdr_txt = _HTML_TAG_RE.sub("", c.group(3) or "").strip().lower()
                    if span == 1 and hdr_txt in EXEMPT_RAW_DESC_HEADERS:
                        new_exempt.add(col_idx)
                    col_idx += span
                exempt_cols = new_exempt
                return tr_full
            if not exempt_cols:
                return tr_full
            # Data row: mask only single-colspan <td> cells at exempt_cols
            pieces: List[str] = []
            last_pos = 0
            col_idx = 0
            for c in cells:
                attrs = c.group(2) or ""
                cs_m = re.search(r'colspan\s*=\s*["\']?(\d+)', attrs, re.IGNORECASE)
                span = int(cs_m.group(1)) if cs_m else 1
                if span == 1 and col_idx in exempt_cols:
                    pieces.append(tr_full[last_pos:c.start(3)])
                    pieces.append("")
                    last_pos = c.end(3)
                col_idx += span
            pieces.append(tr_full[last_pos:])
            return "".join(pieces)

        return _HTML_TR_RE.sub(_clean_tr, tbl)

    return _HTML_TABLE_RE.sub(_clean_table, html_text)


def find_hangul(text: Any, context: str = "", window: int = 30) -> List[Tuple[str, str]]:
    """Returns list of (context, snippet) for every Hangul run found in `text`."""
    if text is None:
        return []
    s = _strip_exempt_html_description_cells(str(text))
    hits: List[Tuple[str, str]] = []
    for m in re.finditer(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]+", s):
        a, b = max(0, m.start() - window), min(len(s), m.end() + window)
        hits.append((context, s[a:b].replace("\n", " ")))
    return hits


def collect_excel_hangul(xlsx_path: Any) -> List[Tuple[str, str]]:
    """Scans Excel sheet names, column headers and cell values for Hangul (exempting raw SAP Description cells per B-3)."""
    hits: List[Tuple[str, str]] = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
        for ws in wb.worksheets:
            hits += find_hangul(ws.title, f"{Path(str(xlsx_path)).name}:sheet_name")
            exempt_cols: set = set()
            header_locked = False
            for row in ws.iter_rows(values_only=True):
                non_empty_strs = [str(v).strip().lower() for v in row if v is not None and str(v).strip()]
                if not header_locked and len(non_empty_strs) >= 2:
                    matched_desc = {
                        idx
                        for idx, v in enumerate(row)
                        if isinstance(v, str) and v.strip().lower() in EXEMPT_RAW_DESC_HEADERS
                    }
                    # Treat as table header row if it's the first multi-column row or contains a Description header
                    if matched_desc or any(
                        k in non_empty_strs
                        for k in ("rank", "bin #", "bin", "material", "part #", "mat.no", "barcode")
                    ):
                        exempt_cols = matched_desc
                        header_locked = True
                        # Always inspect header row cells themselves (never exempt Korean column titles)
                        for v in row:
                            if v is not None and isinstance(v, str) and HANGUL_RE.search(v):
                                hits += find_hangul(v, f"{Path(str(xlsx_path)).name}:{ws.title}:header")
                        continue
                for idx, v in enumerate(row):
                    if idx in exempt_cols:
                        continue
                    if v is not None and isinstance(v, str) and HANGUL_RE.search(v):
                        hits += find_hangul(v, f"{Path(str(xlsx_path)).name}:{ws.title}")
        wb.close()
    except Exception as e:  # pragma: no cover - guard must never break dispatch
        logger.warning(f"[D111] Excel language scan skipped for {xlsx_path}: {e}")
    return hits


def assert_no_hangul(texts: Dict[str, Any], attachments: Iterable[Any] = ()) -> None:
    """Test helper: raises AssertionError listing every Hangul occurrence."""
    hits: List[Tuple[str, str]] = []
    for ctx, t in texts.items():
        hits += find_hangul(t, ctx)
    for p in attachments or ():
        hits += collect_excel_hangul(p)
    assert not hits, f"[D111] Hangul found in operational report ({len(hits)}): {hits[:10]}"


def check_report_language(
    context: str,
    subject: Any = "",
    html_body: Any = "",
    attachments: Iterable[Any] = (),
    is_user_only: bool = False,
    alert_dir: Optional[Path] = None,
) -> List[Tuple[str, str]]:
    """
    Pre-dispatch D111 check for operational recipients. Never raises, never blocks sending.
    On detection: logs a warning and writes `ALERT_LANG_<context>_<ts>.md` in the handoff folder.
    """
    if is_user_only:
        return []
    hits: List[Tuple[str, str]] = []
    try:
        hits += find_hangul(subject, "subject")
        hits += find_hangul(html_body, "html_body")
        for p in attachments or ():
            if p and str(p).lower().endswith((".xlsx", ".xlsm")) and Path(str(p)).exists():
                hits += collect_excel_hangul(p)
        if hits:
            logger.warning(f"[D111 LANGUAGE ALERT] {context}: {len(hits)} Hangul occurrence(s) in operational report (dispatch continues)")
            out_dir = alert_dir or ALERT_DIR
            out_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_ctx = re.sub(r"[^A-Za-z0-9_-]+", "_", context)[:40]
            lines = [
                f"# ALERT_LANG — {context} ({ts})",
                "",
                "D111: operational report contained Hangul. Dispatch was NOT blocked.",
                "",
                "| Location | Snippet |",
                "| :--- | :--- |",
            ]
            for ctx, snip in hits[:50]:
                lines.append(f"| {ctx} | {snip.replace('|', '/')} |")
            (out_dir / f"ALERT_LANG_{safe_ctx}_{ts}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:  # pragma: no cover
        logger.warning(f"[D111] Language guard error ignored for {context}: {e}")
    return hits

