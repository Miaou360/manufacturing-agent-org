"""Material, zone, and box turnover profile computation.

Pairs receipts with production issues, computes dwell days and a 10% trimmed
mean per material, assigns mover classes, and computes zone look-back windows.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from statistics import median
from typing import Any, Sequence

import numpy as np

SMALL_PART_KEYWORDS = (
    "bolt",
    "screw",
    "nut",
    "washer",
    "clip",
    "pin",
    "rivet",
    "grommet",
    "o-ring",
    "fastener",
    "retainer",
)


def is_small_part(description: str) -> bool:
    """Heuristic identifying fastener and small hardware parts from description text."""
    lowered = str(description or "").lower()
    return any(keyword in lowered for keyword in SMALL_PART_KEYWORDS)


def compute_trimmed_mean(values: Sequence[float], trim_percent: float = 0.10) -> float:
    """Compute a two-sided trimmed mean (default 10% from each tail)."""
    clean = np.sort(np.asarray([float(v) for v in values if v is not None and np.isfinite(v)], dtype=float))
    n = len(clean)
    if n == 0:
        return 0.0
    if n < 5:
        return float(np.mean(clean))
    k = int(n * trim_percent)
    if 2 * k >= n:
        return float(np.median(clean))
    return float(np.mean(clean[k : n - k]))


def assign_mover_class(trimmed_mean_days: float | None, issued_count: int) -> str:
    """Classify a material into FAST, NORMAL, SLOW, STAGNANT, or NO_ISSUE_YET."""
    if issued_count <= 0 or trimmed_mean_days is None:
        return "NO_ISSUE_YET"
    if trimmed_mean_days <= 7.0:
        return "FAST"
    if trimmed_mean_days <= 21.0:
        return "NORMAL"
    if trimmed_mean_days <= 60.0:
        return "SLOW"
    return "STAGNANT"


def _parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))


def compute_turnover_profiles(
    *,
    materials: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    bins: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    profile_as_of: str,
    trim_percent: float = 0.10,
) -> dict[str, Any]:
    """Compute derived turnover properties for Box, Material, and Zone objects.

    Boxes with status ISSUED_WITHOUT_RECEIPT are excluded from material dwell
    statistics so unmatched issues cannot distort turnover profiles.
    """
    as_of_dt = _parse_dt(profile_as_of)
    bin_to_zone = {str(b["bin_code"]): str(b["zone_code"]) for b in bins}

    updated_boxes: list[dict[str, Any]] = []
    issued_dwell_by_mat: dict[str, list[float]] = defaultdict(list)
    dwell_by_zone: dict[str, list[float]] = defaultdict(list)

    for box in boxes:
        row = dict(box)
        status = str(row.get("status", "IN_STORAGE"))
        dwell = row.get("dwell_days")

        if status == "ISSUED_WITHOUT_RECEIPT":
            row["dwell_days"] = None
        else:
            if dwell is None:
                recv_at = row.get("received_at")
                if recv_at:
                    delta_days = (as_of_dt - _parse_dt(str(recv_at))).total_seconds() / 86400.0
                    dwell = round(max(0.0, delta_days), 2)
            else:
                dwell = round(max(0.0, float(dwell)), 2)
            row["dwell_days"] = dwell

            if status == "ISSUED_TO_PRODUCTION" and dwell is not None:
                issued_dwell_by_mat[str(row["material_id"])].append(float(dwell))
            zone_code = bin_to_zone.get(str(row.get("bin_code", "")))
            if zone_code and dwell is not None:
                dwell_by_zone[zone_code].append(float(dwell))

        updated_boxes.append(row)

    updated_materials: list[dict[str, Any]] = []
    for mat in materials:
        row = dict(mat)
        mat_id = str(row["material_id"])
        samples = issued_dwell_by_mat.get(mat_id, [])
        if samples:
            t_mean = round(compute_trimmed_mean(samples, trim_percent=trim_percent), 2)
            med = round(float(median(samples)), 2)
        else:
            t_mean = None
            med = None
        row["small_part"] = is_small_part(str(row.get("description", "")))
        row["turnover_days_trimmed_mean"] = t_mean
        row["turnover_days_median"] = med
        row["mover_class"] = assign_mover_class(t_mean, len(samples))
        row["profile_as_of"] = profile_as_of
        updated_materials.append(row)

    updated_zones: list[dict[str, Any]] = []
    for zone in zones:
        row = dict(zone)
        z_code = str(row["zone_code"])
        samples = dwell_by_zone.get(z_code, [])
        if samples:
            med = round(float(median(samples)), 2)
            lookback = max(7, min(30, int(round(med))))
        else:
            med = None
            lookback = 14
        row["median_dwell_days"] = med
        row["lookback_days"] = lookback
        updated_zones.append(row)

    return {
        "profile_as_of": profile_as_of,
        "materials": updated_materials,
        "boxes": updated_boxes,
        "zones": updated_zones,
    }
