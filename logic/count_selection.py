"""Count list selection driven by ontology scope, cooldown skip, and multi-lane ranking.

Eligibility comes strictly from Bin.count_scope and StorageLocation.count_eligible
applied per box, never from bin-name patterns.
"""

from __future__ import annotations

import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable


def _to_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip()[:10])


def check_recent_match_skip(
    *,
    list_scope: str,
    last_matched_at: str | date | datetime | None,
    as_of_date: str | date | datetime | None,
    previous_barcodes: Iterable[str] | None,
    current_barcodes: Iterable[str] | None,
    cooldown_days: int | None = None,
) -> tuple[bool, str, str | None]:
    """Evaluate whether a bin is eligible (True) or skipped by recent-match cooldown (False).

    Returns (eligible, reason, skip_until_iso_or_none).
    """
    scope = str(list_scope or "DAILY").upper()
    days_limit = int(cooldown_days if cooldown_days is not None else (7 if scope == "DAILY" else 14))
    if not last_matched_at or not as_of_date:
        return True, "no recent matching count on record", None

    matched_day = _to_date(last_matched_at)
    ref_day = _to_date(as_of_date)
    days_since = (ref_day - matched_day).days
    same_barcodes = (
        previous_barcodes is not None
        and current_barcodes is not None
        and set(previous_barcodes) == set(current_barcodes)
    )
    if 0 <= days_since < days_limit and same_barcodes:
        skip_until = (matched_day + timedelta(days=days_limit)).isoformat()
        return (
            False,
            f"matched {days_since}d ago (<{days_limit}d) with unchanged barcode set",
            skip_until,
        )
    return (
        True,
        f"eligible: days_since={days_since}, barcodes_changed={not same_barcodes}",
        None,
    )


@dataclass(frozen=True)
class CountSelectionResult:
    """Output of count selection including auditability of skipped and excluded bins."""

    tasks: list[dict[str, Any]]
    skipped_bins: dict[str, str]
    excluded_by_scope: list[str]
    excluded_by_sloc: list[str]


def _score_bin(
    eligible_boxes: list[dict[str, Any]],
    material_map: dict[str, dict[str, Any]],
    prior_mismatch_bins: set[str],
    bin_code: str,
) -> tuple[float, str]:
    """Compute a deterministic risk score and primary reason for one eligible bin."""
    box_count = len(eligible_boxes)
    distinct_parts = {str(b["material_id"]) for b in eligible_boxes}
    max_ratio = 0.0
    has_small_part = False

    for box in eligible_boxes:
        mat = material_map.get(str(box["material_id"]), {})
        if mat.get("small_part"):
            has_small_part = True
        dwell = float(box.get("dwell_days") or 0.0)
        t_mean = mat.get("turnover_days_trimmed_mean")
        baseline = float(t_mean) if t_mean and float(t_mean) > 0 else 21.0
        max_ratio = max(max_ratio, dwell / baseline)

    score = 20.0 + min(40.0, max_ratio * 15.0) + min(20.0, box_count * 2.5)
    if len(distinct_parts) > 1:
        score += 8.0
    if has_small_part:
        score += 10.0
    if bin_code in prior_mismatch_bins:
        score += 15.0

    if bin_code in prior_mismatch_bins:
        reason = "prior_mismatch_watch"
    elif max_ratio >= 1.5:
        reason = "aged_past_turnover"
    elif has_small_part:
        reason = "small_part_risk"
    elif len(distinct_parts) > 1:
        reason = "multi_part_bin"
    else:
        reason = "high_box_density"

    return round(score, 2), reason


def select_count_tasks(
    *,
    run_id: str,
    list_date: str,
    bins: list[dict[str, Any]],
    boxes: list[dict[str, Any]],
    storage_locations: list[dict[str, Any]],
    materials: list[dict[str, Any]] | None = None,
    recent_matches: dict[str, dict[str, Any]] | None = None,
    prior_mismatch_bins: set[str] | None = None,
    list_scope: str = "DAILY",
    risk_limit: int = 12,
    random_limit: int = 4,
    high_rack_limit: int = 10,
    seed: int = 7,
) -> CountSelectionResult:
    """Select ranked count tasks across RISK, RANDOM_BASELINE, and HIGH_RACK lanes."""
    scope = list_scope.upper()
    cooldown_days = 7 if scope == "DAILY" else 14
    rng = random.Random(seed)

    eligible_slocs = {
        str(s["sloc"]) for s in storage_locations if bool(s.get("count_eligible"))
    }
    material_map = {str(m["material_id"]): m for m in (materials or ())}
    match_history = recent_matches or {}
    mismatch_set = set(prior_mismatch_bins or ())

    eligible_boxes_by_bin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    any_box_by_bin: dict[str, int] = defaultdict(int)
    for box in boxes:
        if str(box.get("status", "IN_STORAGE")) != "IN_STORAGE":
            continue
        bcode = str(box["bin_code"])
        any_box_by_bin[bcode] += 1
        if str(box.get("sloc")) in eligible_slocs:
            eligible_boxes_by_bin[bcode].append(box)

    excluded_by_scope: list[str] = []
    excluded_by_sloc: list[str] = []
    skipped_bins: dict[str, str] = {}
    candidates: list[dict[str, Any]] = []

    for b in sorted(bins, key=lambda x: str(x["bin_code"])):
        bcode = str(b["bin_code"])
        bscope = str(b.get("count_scope", "")).upper()
        if bscope != scope:
            excluded_by_scope.append(bcode)
            continue

        elig_boxes = eligible_boxes_by_bin.get(bcode, [])
        if not elig_boxes:
            if any_box_by_bin.get(bcode, 0) > 0:
                excluded_by_sloc.append(bcode)
            continue

        existing_skip = b.get("skip_until")
        if existing_skip and str(existing_skip) > list_date:
            skipped_bins[bcode] = str(existing_skip)
            continue

        hist = match_history.get(bcode)
        if hist:
            curr_barcodes = sorted(str(x["barcode"]) for x in elig_boxes)
            eligible, _, skip_until = check_recent_match_skip(
                list_scope=scope,
                cooldown_days=cooldown_days,
                last_matched_at=hist.get("matched_at"),
                as_of_date=list_date,
                previous_barcodes=hist.get("barcodes"),
                current_barcodes=curr_barcodes,
            )
            if not eligible and skip_until:
                skipped_bins[bcode] = skip_until
                continue

        score, reason = _score_bin(elig_boxes, material_map, mismatch_set, bcode)
        part_ids = sorted({str(x["material_id"]) for x in elig_boxes})
        candidates.append(
            {
                "bin_code": bcode,
                "rack_level": int(b.get("rack_level") or 1),
                "risk_score": score,
                "reason": reason,
                "book_boxes": len(elig_boxes),
                "materials": ", ".join(part_ids),
            }
        )

    candidates.sort(key=lambda c: (-c["risk_score"], c["bin_code"]))
    tasks: list[dict[str, Any]] = []
    rank = 0

    if scope == "WEEKLY":
        for cand in candidates[:high_rack_limit]:
            rank += 1
            tasks.append(
                {
                    "run_id": run_id,
                    "bin_code": cand["bin_code"],
                    "list_date": list_date,
                    "lane": "HIGH_RACK",
                    "rank": rank,
                    "risk_score": cand["risk_score"],
                    "reason": cand["reason"],
                    "book_boxes": cand["book_boxes"],
                    "materials": cand["materials"],
                }
            )
    else:
        risk_picks = candidates[:risk_limit]
        remaining = candidates[risk_limit:]
        for cand in risk_picks:
            rank += 1
            tasks.append(
                {
                    "run_id": run_id,
                    "bin_code": cand["bin_code"],
                    "list_date": list_date,
                    "lane": "RISK",
                    "rank": rank,
                    "risk_score": cand["risk_score"],
                    "reason": cand["reason"],
                    "book_boxes": cand["book_boxes"],
                    "materials": cand["materials"],
                }
            )
        if remaining and random_limit > 0:
            sample_n = min(random_limit, len(remaining))
            rand_picks = sorted(rng.sample(remaining, sample_n), key=lambda c: c["bin_code"])
            for cand in rand_picks:
                rank += 1
                tasks.append(
                    {
                        "run_id": run_id,
                        "bin_code": cand["bin_code"],
                        "list_date": list_date,
                        "lane": "RANDOM_BASELINE",
                        "rank": rank,
                        "risk_score": cand["risk_score"],
                        "reason": "random_baseline_control",
                        "book_boxes": cand["book_boxes"],
                        "materials": cand["materials"],
                    }
                )

    return CountSelectionResult(
        tasks=tasks,
        skipped_bins=skipped_bins,
        excluded_by_scope=sorted(excluded_by_scope),
        excluded_by_sloc=sorted(excluded_by_sloc),
    )
