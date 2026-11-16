"""
Shared Excel Report Style Tokens & Builders for 02_DEPT_Logistics (#369 A)
==========================================================================
Matches the exact visual language of 01_DEPT_Inventory's executive Excel
attachments (`backlog_3q_onepage_renderer.py`, `d107_accuracy_maximizing_planner.py`,
and `line_supply_reconciliation.py`) so supplier Truck Plan workbooks and
future 02_JOB Delivery Check workbooks share a single unified look:
  - Font family: Segoe UI throughout (header 10pt/9.5pt bold, body 9.5pt)
  - Header bands: Dark Slate `#1E293B` (primary) + Deep Report Blue `#1F4E78` (slot subheader)
  - Summary / Day-Total bands: `#334155` header, `#F1F5F9` Day Total column, `#E2E8F0` totals/subheader
  - Grid borders: Full thin `#CBD5E1` cell grid + medium `#475569` section/total dividers
  - Semantic cell fills preserved: `#FFE699` (changed), `#F4B6C2` (additional), `#FCE4D6` (pallet/other)
"""
from __future__ import annotations

from typing import List, Sequence, Tuple

FONT_FAMILY = "Segoe UI"

# Palette aligned 1:1 with backlog_3q_onepage_renderer.py & d107_accuracy_maximizing_planner.py
HDR_PRIMARY = "1E293B"      # Dark slate primary header & title banner
HDR_SECONDARY = "1F4E78"    # Deep report blue (truck time subheader)
HDR_TOTAL = "334155"        # Slate-700 (Day total header)
HDR_SUBTLE = "E2E8F0"       # Slate-200 (Pallet/Other subheader & summary footer band)
META_BAND = "F1F5F9"        # Slate-100 (metadata subtitle strip & Day total body column)
ZEBRA_FILL = "F8FAFC"       # Subtle alternating body row fill

BODY_TEXT = "0F172A"        # Slate-900 primary body text
SUBTLE_TEXT = "334155"      # Slate-700 subtitle / legend / summary text
MUTED_TEXT = "64748B"       # Slate-500 italic reference text
STOCK_POS_TEXT = "334155"   # Clean dark slate for non-negative stock rows
WHITE = "FFFFFF"

BORDER_THIN_HEX = "CBD5E1"  # Crisp full-cell grid border
BORDER_MID_HEX = "475569"   # Section / Day-total medium divider

# Preserved semantic status colors (locked by tests & user legend)
YELLOW = "FFE699"
PINK = "F4B6C2"
ORANGE = "FCE4D6"
LIGHT_BLUE = "E0F2FE"
RED = "C00000"
GREEN = "15803D"
AMBER = "B45309"

NUM_FMT = '#,##0;[Red]-#,##0;""'
STOCK_FMT = '#,##0;[Red]-#,##0;0'
BOX_TOTAL_FMT = '#,##0.0;;""'


def make_styles():
    """Returns reusable openpyxl style primitives for report workbooks."""
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    fill = lambda hex_code: PatternFill(start_color=hex_code, end_color=hex_code, fill_type="solid")
    thin = Side(style="thin", color=BORDER_THIN_HEX)
    mid = Side(style="medium", color=BORDER_MID_HEX)
    grid_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    return {
        "fill": fill,
        "thin": thin,
        "mid": mid,
        "grid_border": grid_border,
        "font": lambda size=9.5, bold=False, italic=False, color=BODY_TEXT: Font(
            name=FONT_FAMILY, size=size, bold=bold, italic=italic, color=color
        ),
        "align_center": Alignment(horizontal="center", vertical="center", wrap_text=True),
        "align_left": Alignment(horizontal="left", vertical="center", wrap_text=False),
        "align_left_wrap": Alignment(horizontal="left", vertical="center", wrap_text=True),
        "align_right": Alignment(horizontal="right", vertical="center"),
    }


def apply_title_and_legend_band(
    ws,
    title: str,
    subtitle: str,
    legend: Sequence[Tuple[str, str]],
    legend_cols: Sequence[int],
    last_col: int,
) -> None:
    """
    Styles rows 1-3 as an executive report title + metadata + legend band
    without changing row indices (Row 4 remains spacer; table starts at Row 5).
    """
    from openpyxl.styles import Alignment, Border

    st = make_styles()
    fill, font, thin = st["fill"], st["font"], st["thin"]

    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = 90

    span_cols = max(last_col, max(legend_cols, default=1) + 4)
    for c in range(1, span_cols + 1):
        c1 = ws.cell(row=1, column=c)
        c1.fill = fill(HDR_PRIMARY)
        c2 = ws.cell(row=2, column=c)
        c2.fill = fill(META_BAND)
        c2.border = Border(bottom=thin)

    ws["A1"] = title
    ws["A1"].font = font(size=12, bold=True, color=WHITE)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24

    ws["A2"] = subtitle
    ws["A2"].font = font(size=9.5, bold=False, color=SUBTLE_TEXT)
    ws["A2"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[2].height = 19

    ws.row_dimensions[3].height = 20
    ws.row_dimensions[4].height = 8
    for col, (swatch_hex, text) in zip(legend_cols, legend):
        sw = ws.cell(row=3, column=col, value="")
        sw.fill = fill(swatch_hex)
        sw.border = st["grid_border"]
        lbl = ws.cell(row=3, column=col + 1, value=text)
        lbl.font = font(size=9.0, bold=False, color=SUBTLE_TEXT)
        lbl.alignment = Alignment(horizontal="left", vertical="center", wrap_text=False)
