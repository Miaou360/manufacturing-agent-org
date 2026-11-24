"""Rule-based mismatch cause classifier for physical cycle-count discrepancies.

Assigns each count result to one of the five expected causes:
  - LINE_SCAN_MISS  : physical < book while bin is not empty
  - EMPTY           : physical == 0 while book > 0
  - BIN_TO_BIN_MISS : physical > book or field notes indicate a bin move
  - PART_MISSING    : listed part absent in a multi-part bin
  - WRONG_PART      : unlisted or wrong part physically found in the bin
  - UNCLASSIFIED    : not counted, matched, or no clear cause
"""

from __future__ import annotations

import re

CAUSE_CLASSES = frozenset(
    {
        "LINE_SCAN_MISS",
        "EMPTY",
        "BIN_TO_BIN_MISS",
        "PART_MISSING",
        "WRONG_PART",
        "UNCLASSIFIED",
    }
)

_RE_WRONG_PART = re.compile(
    r"\b(?:wrong\s+part|extra\s+part|unlisted\s+part|mixed\s+part)\b",
    re.IGNORECASE,
)
_RE_PART_MISSING = re.compile(
    r"\b(?:not\s+found|part\s+missing|missing\s+from\s+bin|absent)\b",
    re.IGNORECASE,
)
_RE_BIN_TO_BIN = re.compile(
    r"\b(?:bin\s+to\s+bin|other\s+bin|moved\s+from|moved\s+to|adjacent\s+bin)\b",
    re.IGNORECASE,
)
_RE_LINE_SCAN = re.compile(
    r"\b(?:line\s+scan|consumed|pulled\s+to\s+line|not\s+scanned\s+out)\b",
    re.IGNORECASE,
)


def classify_mismatch_cause(
    *,
    book_boxes: int,
    counted_boxes: int | None,
    field_notes: str = "",
    multi_part_bin: bool = False,
) -> str:
    """Classify one count result into one of the six ontology cause classes."""
    if counted_boxes is None:
        return "UNCLASSIFIED"
    if counted_boxes == book_boxes:
        return "UNCLASSIFIED"

    notes = str(field_notes or "")
    if _RE_WRONG_PART.search(notes):
        return "WRONG_PART"
    if counted_boxes == 0 and book_boxes > 0:
        return "EMPTY"
    if _RE_PART_MISSING.search(notes) and (multi_part_bin or counted_boxes < book_boxes):
        return "PART_MISSING"
    if _RE_BIN_TO_BIN.search(notes) or counted_boxes > book_boxes:
        return "BIN_TO_BIN_MISS"
    if _RE_LINE_SCAN.search(notes) or (0 < counted_boxes < book_boxes):
        return "LINE_SCAN_MISS"
    return "UNCLASSIFIED"
