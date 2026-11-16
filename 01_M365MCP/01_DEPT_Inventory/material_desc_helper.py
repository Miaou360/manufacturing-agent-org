"""Shared Material Description Lookup & Formatting Helper (Public Synthetic Edition)."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Union

DEPT_DIR = Path(__file__).resolve().parent
NO_DESC_FALLBACK = "(no description)"
_INVALID = {"", "none", "null", "nan", "n/a", "na", "-", "--", "(no description)"}


def set_data_dept_dir(_dept_dir: Path) -> None:
    return None


def _clean_desc(val: Any) -> str:
    s = str(val or "").strip()
    return s if s.lower() not in _INVALID else ""


def get_material_desc(
    materials: Union[str, Iterable[Any]],
    row_descs: Optional[Mapping[str, Any]] = None,
    **_kwargs: Any,
) -> Union[str, Dict[str, str]]:
    """Resolves material descriptions from `row_descs` with `(no description)` fallback."""
    rd = {str(k).strip(): _clean_desc(v) for k, v in (row_descs or {}).items()}
    if isinstance(materials, str):
        k = materials.strip()
        return rd.get(k) or rd.get(k.lstrip("0")) or NO_DESC_FALLBACK
    out: Dict[str, str] = {}
    for m in materials:
        k = str(m or "").strip()
        if k:
            out[k] = rd.get(k) or rd.get(k.lstrip("0")) or NO_DESC_FALLBACK
    return out


def format_material_with_desc(mat_no: Any, desc: Any = None, sep: str = " — ") -> str:
    m = str(mat_no or "").strip()
    d = _clean_desc(desc) or get_material_desc(m)
    return f"{m}{sep}{d}" if m else d


def format_material_html(mat_no: Any, desc: Any = None) -> str:
    m = str(mat_no or "").strip()
    d = _clean_desc(desc) or get_material_desc(m)
    return f"<b>{html.escape(m)}</b> — {html.escape(d)}"
