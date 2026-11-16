"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - Minimal-Change Truck Load Leveling (#367 / #384 / #367b)
====================================================================================================
Rules R1-R5 (#384) + Per-Supplier Hard Cart Cap (#367b, notes 388 & 389):
  R1 Baseline = this run's SRM order distribution (`srm_trucks.plan_trucks`, incl. urgent pull-forwards on parts
     that actually run short). No memory of earlier mails.
  R2 Departed trucks frozen (`slot_start <= run_ts + freeze_lead_min`, default 30 min): neither source nor
     destination; their quantity = baseline. A frozen truck over the cap is reported as information only (never warned).
  R3 Two overload regimes:
     - Supplier WITH `max_carts_per_truck` cap (e.g. 5-truck supplier = 33):
       * Step 3a: urgent ADD goes to lightest editable trucks, never exceeding `cap`, keeping 115%.
       * Step 3b Pass 1: unload editable trucks `> cap` to editable trucks staying `<= cap`, keeping 115%.
       * Step 3b Pass 2 (only if an editable truck is still `> cap` and another editable truck has room `< cap`):
         retry remaining moves with 100% margin (`Inv(S-1) >= Req(S)` and `Inv >= 0` everywhere; `margin_used = 1.00`).
       * Warn ONLY if an editable truck is still `> cap` after Pass 2 (`Truck over <cap> carts: ... — needed to prevent
         a shortage, extra truck may be required`).
     - Supplier WITHOUT a cap (e.g. 4-truck supplier):
       * Unload editable trucks `> overload_factor * day_average` (default `1.7x`) keeping 115% only (never 100%).
  R4 Fewest changes - move to the lightest editable truck first; prefer the part that frees the most carts in one move.
  R5 Shortage hard rule - never create `Inv < 0` anywhere in the horizon or worsen an existing shortage. Step 4
     independent post-check restores baseline on any violation.
"""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List, Optional, Set, Tuple

from srm_parsers import N_SLOTS, SLOT_LABELS
from srm_projection import slot_start
from srm_trucks import (
    DEFAULT_OVERLOAD_FACTOR,
    FIRST_TRUCK_REASON,
    MIN_SAFETY_MARGIN,
    PENDING_REASON,
    cap_for_supplier,
)


def compute_frozen_slots(
    run_date: dt.date,
    n_days: int,
    run_ts: Optional[dt.datetime],
    freeze_lead_min: float = 30.0,
) -> Set[int]:
    """Horizon slot indices whose truck departure (`slot_start`) is `<= run_ts + freeze_lead_min` (#384 R2)."""
    if run_ts is None:
        return set()
    cutoff = run_ts + dt.timedelta(minutes=float(freeze_lead_min))
    return {
        i for i in range(n_days * N_SLOTS)
        if slot_start(run_date, i // N_SLOTS, i % N_SLOTS) <= cutoff
    }


def effective_req(req: List[float], slot_idx: int, next_truck_slot: Optional[int] = None) -> float:
    """Requirement for gap check at truck slot S (#369 B1 & #370 §2): `Prod.Plan(S)` if `> 0`, else first non-zero
    slot at or after `S` before the following active truck (`next_truck_slot`, or end of horizon if none)."""
    if slot_idx < 0 or slot_idx >= len(req):
        return 0.0
    if req[slot_idx] > 1e-9:
        return float(req[slot_idx])
    upper = min(len(req), next_truck_slot) if next_truck_slot is not None else len(req)
    for u in range(slot_idx + 1, upper):
        if req[u] > 1e-9:
            return float(req[u])
    return 0.0


def recompute_inv(start_sap: float, load: List[float], req: List[float]) -> List[float]:
    """End-of-slot stock on Order/Load basis (`Inv(s) = Inv(s-1) + Load(s) - Prod.Plan(s)`, #368)."""
    inv = float(start_sap)
    out: List[float] = []
    for a, b in zip(load, req):
        inv += float(a) - float(b)
        out.append(inv)
    return out


def compute_unfixed(stock: List[float], extra_days: Set[int]) -> Tuple[Optional[int], Optional[str]]:
    for idx, val in enumerate(stock):
        if val < -1e-9:
            reason = FIRST_TRUCK_REASON if (idx // N_SLOTS) in extra_days else PENDING_REASON
            return idx, reason
    return None, None


def check_moved_plan_valid(
    start_sap: float,
    cand_load: List[float],
    req: List[float],
    all_truck_slots: List[int],
    baseline_load: List[float],
    baseline_inv: List[float],
    safety_margin: float,
    target_day: Optional[int] = None,
) -> Tuple[bool, List[float], str]:
    """Evaluates a candidate `cand_load` over the full horizon (#384 R5, #370 §2 & #367b).
    - `safety_margin`: `>= 0.10` in normal Pass 1 (e.g. `0.15` = 115%), or `0.0` in #367b Pass 2 cap fallback (100%).
    - Absolute shortage rule: never create `Inv < 0` or worsen an existing `< 0` slot anywhere in the horizon.
    - Margin rule on moved parts: every active truck gap must keep `Inv(S-1) >= (1 + safety_margin) * Req(S)`,
      except where `baseline` was already below `(1 + safety_margin)` at that boundary and `cand_inv` does not worsen it."""
    if safety_margin < -1e-9:
        raise ValueError(f"safety_margin {safety_margin} < 0.0")

    cand_inv = recompute_inv(start_sap, cand_load, req)

    for t, (c_val, b_val) in enumerate(zip(cand_inv, baseline_inv)):
        if b_val >= -1e-9:
            if c_val < -1e-9:
                return False, cand_inv, f"shortage at slot {t} ({c_val:.2f} < 0)"
        else:
            if c_val < b_val - 1e-9:
                return False, cand_inv, f"worsened existing shortage at slot {t} ({c_val:.2f} < {b_val:.2f})"

    if all(c_val >= b_val - 1e-9 for c_val, b_val in zip(cand_inv, baseline_inv)):
        return True, cand_inv, "ok"

    active_trucks = [tau for tau in all_truck_slots if cand_load[tau] > 1e-9]
    base_active = [tau for tau in all_truck_slots if baseline_load[tau] > 1e-9]
    if not active_trucks:
        return True, cand_inv, "ok"

    factor = 1.0 + float(safety_margin)
    n_days = len(cand_load) // N_SLOTS
    for d in range(n_days):
        if target_day is not None and d != target_day:
            continue
        cand_d = [tau for tau in active_trucks if tau // N_SLOTS == d]
        base_d = [tau for tau in base_active if tau // N_SLOTS == d]
        if cand_d and base_d and cand_d[0] > base_d[0]:
            s_first = cand_d[0]
            if s_first > 0:
                idx_f = active_trucks.index(s_first)
                nxt_after_f = active_trucks[idx_f + 1] if idx_f + 1 < len(active_trucks) else None
                min_inv = factor * effective_req(req, s_first, nxt_after_f)
                if cand_inv[s_first - 1] + 1e-9 < min_inv:
                    return False, cand_inv, (
                        f"delayed first truck {s_first} inv_before {cand_inv[s_first - 1]:.2f} < {min_inv:.2f}"
                    )

    base_pairs = {(base_active[i - 1], base_active[i]) for i in range(1, len(base_active))}

    for idx in range(1, len(active_trucks)):
        prev_t = active_trucks[idx - 1]
        s_next = active_trucks[idx]
        if s_next <= 0:
            continue
        d_prev = prev_t // N_SLOTS
        d_next = s_next // N_SLOTS

        if (
            target_day is not None
            and d_prev != target_day
            and d_next != target_day
            and (prev_t, s_next) in base_pairs
            and abs(cand_inv[s_next - 1] - baseline_inv[s_next - 1]) <= 1e-9
        ):
            continue

        nxt_after = active_trucks[idx + 1] if idx + 1 < len(active_trucks) else None
        min_inv = factor * effective_req(req, s_next, nxt_after)
        if cand_inv[s_next - 1] + 1e-9 < min_inv:
            b_next = next((tau for tau in base_active if tau >= s_next), None)
            if b_next is not None and cand_inv[s_next - 1] >= baseline_inv[s_next - 1] - 1e-9:
                if s_next == b_next:
                    b_idx = base_active.index(s_next)
                    base_bound = base_active[b_idx + 1] if b_idx + 1 < len(base_active) else None
                else:
                    base_bound = b_next
                base_min_inv = factor * effective_req(req, s_next, base_bound)
                if baseline_inv[s_next - 1] + 1e-9 < base_min_inv and min_inv <= base_min_inv + 1e-9:
                    continue
            return False, cand_inv, f"gap ({prev_t}->{s_next}) inv_before {cand_inv[s_next - 1]:.2f} < {min_inv:.2f}"

    return True, cand_inv, "ok"


def _min_slack_between(
    inv: List[float],
    req: List[float],
    active_trucks: List[int],
    t_start: int,
    t_end: int,
    safety_margin: float,
) -> float:
    seg = inv[t_start:t_end]
    slack = min(seg) if seg else 0.0
    factor = 1.0 + safety_margin
    for idx_s, s in enumerate(active_trucks):
        if t_start < s <= t_end and s > 0:
            nxt_s = active_trucks[idx_s + 1] if idx_s + 1 < len(active_trucks) else None
            slack = min(slack, inv[s - 1] - factor * effective_req(req, s, nxt_s))
    return slack


def level_all_suppliers(
    run_date: dt.date,
    res6: List[Dict[str, Any]],
    trucks_cfg: Dict[str, List[int]],
    box_qty: Dict[str, float],
    leveling_cfg: Dict[str, Any],
    *,
    run_ts: Optional[dt.datetime] = None,
) -> Dict[str, Any]:
    """Applies #384 R1-R5 + #367b per-supplier hard cart cap across all suppliers with fixed trucks (Class C only)."""
    enabled = bool(leveling_cfg.get("enabled", True))
    safety_margin = float(leveling_cfg.get("safety_margin", 0.15))
    if safety_margin < MIN_SAFETY_MARGIN - 1e-9:
        raise ValueError(f"safety_margin {safety_margin} must be >= {MIN_SAFETY_MARGIN}")
    freeze_lead_min = float(leveling_cfg.get("freeze_lead_min", 30.0))
    overload_factor = float(leveling_cfg.get("overload_factor", DEFAULT_OVERLOAD_FACTOR))
    if overload_factor <= 1.0 + 1e-9:
        raise ValueError(f"overload_factor {overload_factor} must be > 1.0")
    max_carts_cfg = dict(leveling_cfg.get("max_carts_per_truck") or {})

    if not res6:
        return {"enabled": enabled, "notes": [], "warnings": [], "info_notes": [], "moves_100pct": [], "suppliers": {}}

    sample_plan = next((r["plan"] for r in res6 if r.get("plan")), None)
    if not sample_plan:
        return {"enabled": enabled, "notes": [], "warnings": [], "info_notes": [], "moves_100pct": [], "suppliers": {}}

    dates: List[dt.date] = sample_plan["dates"]
    n_days = len(dates)
    frozen_slots = compute_frozen_slots(run_date, n_days, run_ts, freeze_lead_min)

    by_supplier: Dict[str, Tuple[List[int], List[Dict[str, Any]]]] = {}
    for r in res6:
        p = r.get("plan")
        if not p or not p.get("trucks") or p.get("cls") != "C":
            continue
        v_low = (r.get("vendor") or "").lower()
        sup_key = next((name for name in trucks_cfg if name.lower() in v_low), r["vendor"])
        by_supplier.setdefault(sup_key, (list(p["trucks"]), []))[1].append(r)

    notes: List[str] = []
    warnings: List[str] = []
    info_notes: List[str] = []
    moves_100pct: List[Dict[str, Any]] = []
    sup_summaries: Dict[str, Any] = {}

    for sup_key, (t_slots, sup_rows) in sorted(by_supplier.items()):
        sample_vendor = sup_rows[0].get("vendor", sup_key) if sup_rows else sup_key
        sup_cap = cap_for_supplier(sup_key, sample_vendor, t_slots, max_carts_cfg)
        all_truck_slots = [d * N_SLOTS + t for d in range(n_days) for t in t_slots]

        expected_day_totals: Dict[Tuple[str, int], float] = {}
        for r in sup_rows:
            k = r["key"]
            p = r["plan"]
            for d in range(n_days):
                a, b = d * N_SLOTS, (d + 1) * N_SLOTS
                expected_day_totals[(k, d)] = sum(p["srm"][a:b]) + p["add"][d] + sum(p["extra"][a:b])

        def truck_carts(tau: int) -> float:
            tot = 0.0
            for r in sup_rows:
                bx = float(box_qty.get(r["key"]) or 0.0)
                if bx > 0:
                    tot += r["plan"]["load"][tau] / bx
            return tot

        sup_moves = 0
        sup_fallbacks = 0
        sup_constrained: List[str] = []
        sup_info: List[str] = []
        sup_days_report: Dict[str, Any] = {}

        for d in range(n_days):
            a, b = d * N_SLOTS, (d + 1) * N_SLOTS
            date_iso = dates[d].isoformat()
            day_truck_slots = [d * N_SLOTS + t for t in t_slots]
            editable_trucks = [tau for tau in day_truck_slots if tau not in frozen_slots]

            carts_before = [truck_carts(tau) for tau in day_truck_slots]
            day_avg = (sum(carts_before) / len(day_truck_slots)) if day_truck_slots else 0.0
            threshold = float(sup_cap) if sup_cap is not None else (overload_factor * day_avg)

            day_backup = {
                r["key"]: (list(r["plan"]["load"]), list(r["plan"]["stock"]), r["plan"]["unfixed"], r["plan"]["reason"])
                for r in sup_rows
            }
            day_baselines = {
                r["key"]: {
                    "load": list(r["plan"]["load"]),
                    "extra": list(r["plan"]["extra"]),
                    "stock": list(r["plan"]["stock"]),
                    "unfixed": r["plan"]["unfixed"],
                    "reason": r["plan"]["reason"],
                }
                for r in sup_rows
            }
            editable_started_over_cap = {
                tau for tau in editable_trucks
                if sup_cap is not None and truck_carts(tau) > float(sup_cap) + 1e-9
            }

            pass2_parts_d: Set[str] = set()
            day_moves_100pct: List[Dict[str, Any]] = []
            day_move_log: List[Dict[str, Any]] = []

            if enabled and sup_cap is not None:
                for tau_f in day_truck_slots:
                    if tau_f in frozen_slots:
                        cf = truck_carts(tau_f)
                        if cf > float(sup_cap) + 1e-9:
                            imsg = (
                                f"Frozen (departed) truck over {sup_cap} carts (info only): {sup_key} "
                                f"{dates[d]:%a %m/%d} {SLOT_LABELS[tau_f % N_SLOTS]} {cf:.0f} carts"
                            )
                            sup_info.append(imsg)
                            info_notes.append(imsg)

            if enabled and len(editable_trucks) >= 2:
                # Step 3a (R3a): ADD distribution -> lightest editable trucks (never above cap if capped; always 115%)
                add_avail: Dict[str, Dict[int, float]] = {}
                for r in sup_rows:
                    p = r["plan"]
                    if p["add"][d] > 1e-9:
                        abs_slot = p.get("add_by_slot") or [0.0] * len(p["load"])
                        add_avail[r["key"]] = {tau: float(abs_slot[tau]) for tau in editable_trucks}

                if add_avail:
                    for _ in range(2000):
                        c_map = {tau: truck_carts(tau) for tau in editable_trucks}
                        add_cands: List[Tuple[Any, int, int, Dict[str, Any]]] = []
                        for r in sup_rows:
                            k = r["key"]
                            if k not in add_avail:
                                continue
                            bx = float(box_qty.get(k) or 0.0)
                            if bx <= 1e-9:
                                continue
                            p = r["plan"]
                            for tau_src in editable_trucks:
                                if add_avail[k].get(tau_src, 0.0) + 1e-9 < bx:
                                    continue
                                if p["load"][tau_src] - p["extra"][tau_src] + 1e-9 < bx:
                                    continue
                                src_over_cap = sup_cap is not None and c_map[tau_src] > float(sup_cap) + 1e-9
                                for tau_dst in editable_trucks:
                                    if tau_dst == tau_src:
                                        continue
                                    if sup_cap is not None and c_map[tau_dst] + 1.0 > float(sup_cap) + 1e-9:
                                        continue
                                    if not src_over_cap and c_map[tau_src] - c_map[tau_dst] <= 1.0 + 1e-9:
                                        continue
                                    sort_k = (
                                        round(c_map[tau_dst], 6),
                                        tau_dst,
                                        -round(c_map[tau_src], 6),
                                        -tau_src,
                                        -round(add_avail[k][tau_src] / bx, 6),
                                        r["part"],
                                    )
                                    add_cands.append((sort_k, tau_src, tau_dst, r))

                        if not add_cands:
                            break
                        add_cands.sort(key=lambda x: x[0])
                        moved_add = False
                        for _, tau_src, tau_dst, r in add_cands:
                            k = r["key"]
                            p = r["plan"]
                            bx = float(box_qty[k])
                            cand_load = list(p["load"])
                            cand_load[tau_src] -= bx
                            cand_load[tau_dst] += bx
                            if cand_load[tau_src] < 1e-9:
                                cand_load[tau_src] = 0.0
                            ok_mv, cand_inv, _ = check_moved_plan_valid(
                                r["start_sap"],
                                cand_load,
                                p["req"],
                                all_truck_slots,
                                day_baselines[k]["load"],
                                day_baselines[k]["stock"],
                                safety_margin,
                                target_day=d,
                            )
                            if ok_mv:
                                p["load"] = cand_load
                                p["stock"] = cand_inv
                                p["unfixed"], p["reason"] = compute_unfixed(cand_inv, p["extra_days"])
                                add_avail[k][tau_src] = max(0.0, add_avail[k][tau_src] - bx)
                                add_avail[k][tau_dst] = add_avail[k].get(tau_dst, 0.0) + bx
                                sup_moves += 1
                                day_move_log.append({
                                    "pass": "ADD",
                                    "margin_used": round(1.0 + safety_margin, 2),
                                    "part": r["part"],
                                    "name": r["name"],
                                    "from_truck": SLOT_LABELS[tau_src % N_SLOTS],
                                    "to_truck": SLOT_LABELS[tau_dst % N_SLOTS],
                                    "carts": 1,
                                    "qty": bx,
                                })
                                moved_add = True
                                break
                        if not moved_add:
                            break

                # Step 3b: Overload / Cap relief
                blocked_by_shortage = False
                editable_sum = sum(truck_carts(tau) for tau in editable_trucks)
                if sup_cap is not None:
                    eff_threshold = float(sup_cap)
                    passes: List[Tuple[int, float]] = [(1, safety_margin), (2, 0.0)]
                else:
                    eff_threshold = max(threshold, editable_sum / len(editable_trucks))
                    passes = [(1, safety_margin)]

                for pass_no, pass_margin in passes:
                    c_pre = {tau: truck_carts(tau) for tau in editable_trucks}
                    if pass_no == 2:
                        has_over = any(c_pre[tau] > threshold + 1e-9 for tau in editable_trucks)
                        has_room = any(c_pre[tau] + 1.0 <= eff_threshold + 1e-9 for tau in editable_trucks)
                        if not (has_over and has_room):
                            break

                    for _ in range(500):
                        c_map = {tau: truck_carts(tau) for tau in editable_trucks}
                        overloaded = [tau for tau in editable_trucks if c_map[tau] > threshold + 1e-9]
                        if not overloaded:
                            break
                        overloaded.sort(key=lambda tau: (-round(c_map[tau], 6), -tau))

                        moved_overload = False
                        touched_now = {
                            r["key"]
                            for r in sup_rows
                            if any(
                                abs(r["plan"]["load"][a + s] - day_baselines[r["key"]]["load"][a + s]) > 1e-9
                                for s in range(N_SLOTS)
                            )
                        }

                        for tau_src in overloaded:
                            if c_map[tau_src] <= eff_threshold + 1e-9:
                                continue
                            excess_carts = max(1, int(math.ceil(c_map[tau_src] - eff_threshold - 1e-9)))
                            dst_trucks = sorted(
                                [tau for tau in editable_trucks if tau != tau_src and c_map[tau] + 1.0 <= eff_threshold + 1e-9],
                                key=lambda tau: (round(c_map[tau], 6), tau),
                            )
                            for tau_dst in dst_trucks:
                                dst_room = int(math.floor(eff_threshold - c_map[tau_dst] + 1e-9))
                                if dst_room < 1:
                                    continue
                                cap_k = min(excess_carts, dst_room)
                                is_push = tau_dst > tau_src
                                best_choice: Optional[Tuple[Any, Dict[str, Any], int, List[float], List[float]]] = None

                                for r in sup_rows:
                                    k = r["key"]
                                    bx = float(box_qty.get(k) or 0.0)
                                    if bx <= 1e-9:
                                        continue
                                    p = r["plan"]
                                    movable_qty = p["load"][tau_src] - p["extra"][tau_src]
                                    movable_carts = int(math.floor(movable_qty / bx + 1e-9))
                                    if movable_carts < 1:
                                        continue
                                    eff_part_margin = 0.0 if (pass_no == 2 or k in pass2_parts_d) else pass_margin
                                    max_try = min(movable_carts, cap_k)
                                    chosen_k = 0
                                    chosen_load: Optional[List[float]] = None
                                    chosen_inv: Optional[List[float]] = None
                                    for k_try in range(max_try, 0, -1):
                                        qty_mv = k_try * bx
                                        cand_load = list(p["load"])
                                        cand_load[tau_src] -= qty_mv
                                        cand_load[tau_dst] += qty_mv
                                        if cand_load[tau_src] < 1e-9:
                                            cand_load[tau_src] = 0.0
                                        ok_mv, cand_inv, _ = check_moved_plan_valid(
                                            r["start_sap"],
                                            cand_load,
                                            p["req"],
                                            all_truck_slots,
                                            day_baselines[k]["load"],
                                            day_baselines[k]["stock"],
                                            eff_part_margin,
                                            target_day=d,
                                        )
                                        if ok_mv:
                                            chosen_k = k_try
                                            chosen_load = cand_load
                                            chosen_inv = cand_inv
                                            break
                                        else:
                                            blocked_by_shortage = True

                                    if chosen_k >= 1 and chosen_load is not None and chosen_inv is not None:
                                        if is_push:
                                            act = [t_i for t_i in all_truck_slots if p["load"][t_i] > 1e-9]
                                            slack = _min_slack_between(
                                                p["stock"], p["req"], act, tau_src, tau_dst, eff_part_margin
                                            )
                                        else:
                                            slack = 0.0
                                        rank_key = (
                                            -chosen_k,
                                            -(1 if k in touched_now else 0),
                                            -(1 if movable_carts == chosen_k else 0),
                                            -round(slack, 6) if is_push else 0.0,
                                            -movable_carts,
                                            bx,
                                            r["part"],
                                        )
                                        if best_choice is None or rank_key < best_choice[0]:
                                            best_choice = (rank_key, r, chosen_k, chosen_load, chosen_inv)

                                if best_choice is not None:
                                    _, r_win, k_win, load_win, inv_win = best_choice
                                    p_win = r_win["plan"]
                                    p_win["load"] = load_win
                                    p_win["stock"] = inv_win
                                    p_win["unfixed"], p_win["reason"] = compute_unfixed(inv_win, p_win["extra_days"])
                                    sup_moves += 1
                                    bx_win = float(box_qty[r_win["key"]])
                                    m_used = 1.00 if pass_no == 2 else round(1.0 + safety_margin, 2)
                                    mv_rec = {
                                        "pass": pass_no,
                                        "margin_used": m_used,
                                        "supplier": sup_key,
                                        "date": date_iso,
                                        "day_label": f"{dates[d]:%a %m/%d}",
                                        "part": r_win["part"],
                                        "name": r_win["name"],
                                        "from_truck": SLOT_LABELS[tau_src % N_SLOTS],
                                        "to_truck": SLOT_LABELS[tau_dst % N_SLOTS],
                                        "carts": k_win,
                                        "qty": round(k_win * bx_win, 2),
                                    }
                                    day_move_log.append(mv_rec)
                                    if pass_no == 2:
                                        pass2_parts_d.add(r_win["key"])
                                        day_moves_100pct.append(mv_rec)
                                        moves_100pct.append(mv_rec)
                                    moved_overload = True
                                    break
                            if moved_overload:
                                break
                        if not moved_overload:
                            break

                c_after_check = {tau: truck_carts(tau) for tau in editable_trucks}
                still_over = [tau for tau in editable_trucks if c_after_check[tau] > threshold + 1e-9]
                if sup_cap is not None:
                    for tau_heavy in sorted(still_over):
                        cap_total = sup_cap * len(editable_trucks)
                        extra_ctx = (
                            f" (editable day total {editable_sum:.0f} carts > {len(editable_trucks)}x{sup_cap}={cap_total})"
                            if editable_sum > cap_total + 1e-9
                            else ""
                        )
                        msg = (
                            f"Truck over {sup_cap} carts: {sup_key} {dates[d]:%a %m/%d} "
                            f"{SLOT_LABELS[tau_heavy % N_SLOTS]} {c_after_check[tau_heavy]:.0f} carts "
                            f"\u2014 needed to prevent a shortage, extra truck may be required{extra_ctx}"
                        )
                        sup_constrained.append(msg)
                        warnings.append(msg)
                        notes.append(msg)
                elif still_over and blocked_by_shortage:
                    tau_heavy = max(still_over, key=lambda t_i: (c_after_check[t_i], t_i))
                    msg = (
                        f"Overloaded truck kept to prevent shortage: {sup_key} "
                        f"{dates[d]:%a %m/%d} {SLOT_LABELS[tau_heavy % N_SLOTS]} "
                        f"({c_after_check[tau_heavy]:.0f} carts > {threshold:.1f} threshold; "
                        f"min editable truck {min(c_after_check.values()):.0f} carts)."
                    )
                    sup_constrained.append(msg)
                    notes.append(msg)

                part_margins_d = {
                    r["key"]: (0.0 if r["key"] in pass2_parts_d else safety_margin)
                    for r in sup_rows
                }
                ok_step4, fail_detail = verify_supplier_day_postcheck(
                    sup_rows,
                    day_baselines,
                    expected_day_totals,
                    t_slots,
                    frozen_slots,
                    d,
                    safety_margin,
                    part_margins=part_margins_d,
                    sup_cap=sup_cap,
                    editable_started_over_cap=editable_started_over_cap,
                    box_qty=box_qty,
                )
                if not ok_step4:
                    for r in sup_rows:
                        k = r["key"]
                        b_load, b_stock, b_unf, b_rsn = day_backup[k]
                        r["plan"]["load"] = b_load
                        r["plan"]["stock"] = b_stock
                        r["plan"]["unfixed"] = b_unf
                        r["plan"]["reason"] = b_rsn
                    sup_fallbacks += 1
                    notes.append(
                        f"Leveling post-check fallback on {sup_key} {dates[d]:%a %m/%d}: "
                        f"restored un-leveled baseline ({fail_detail})."
                    )
            elif enabled and sup_cap is not None and len(editable_trucks) == 1:
                tau_only = editable_trucks[0]
                c_only = truck_carts(tau_only)
                if c_only > float(sup_cap) + 1e-9:
                    msg = (
                        f"Truck over {sup_cap} carts: {sup_key} {dates[d]:%a %m/%d} "
                        f"{SLOT_LABELS[tau_only % N_SLOTS]} {c_only:.0f} carts "
                        f"\u2014 needed to prevent a shortage, extra truck may be required (only 1 editable truck left)"
                    )
                    sup_constrained.append(msg)
                    warnings.append(msg)
                    notes.append(msg)

            carts_after = [truck_carts(tau) for tau in day_truck_slots]
            touched_parts_detail: List[Dict[str, Any]] = []
            for r in sup_rows:
                k = r["key"]
                p = r["plan"]
                base_l = day_baselines[k]["load"]
                if any(abs(p["load"][a + s] - base_l[a + s]) > 1e-9 for s in range(N_SLOTS)):
                    bx = float(box_qty.get(k) or 0.0)
                    ea_delta = 0.5 * sum(abs(p["load"][a + s] - base_l[a + s]) for s in range(N_SLOTS))
                    c_moved = (ea_delta / bx) if bx > 1e-9 else 0.0
                    m_part = 1.00 if k in pass2_parts_d else round(1.0 + safety_margin, 2)
                    touched_parts_detail.append({
                        "part": r["part"],
                        "name": r["name"],
                        "box_qty": bx,
                        "add": float(p["add"][d]),
                        "before": [float(base_l[d * N_SLOTS + t]) for t in t_slots],
                        "after": [float(p["load"][d * N_SLOTS + t]) for t in t_slots],
                        "carts_moved": round(c_moved, 2),
                        "margin_used": m_part,
                    })

            sup_days_report[date_iso] = {
                "truck_labels": [SLOT_LABELS[t] for t in t_slots],
                "frozen_trucks": [SLOT_LABELS[t] for t in t_slots if (d * N_SLOTS + t) in frozen_slots],
                "max_carts_cap": sup_cap,
                "day_avg": round(day_avg, 2),
                "threshold": round(threshold, 2),
                "trucks_before": [round(x, 1) for x in carts_before],
                "trucks_after": [round(x, 1) for x in carts_after],
                "parts_touched": len(touched_parts_detail),
                "carts_moved": round(sum(tp["carts_moved"] for tp in touched_parts_detail), 1),
                "moves_100pct_count": len(day_moves_100pct),
                "moves_100pct": day_moves_100pct,
                "move_log": day_move_log,
                "touched_parts_detail": touched_parts_detail,
            }

        sup_summaries[sup_key] = {
            "max_carts_cap": sup_cap,
            "moves_accepted": sup_moves,
            "fallbacks": sup_fallbacks,
            "constrained_trucks": sup_constrained,
            "info_notes": sup_info,
            "days": sup_days_report,
        }

    return {
        "enabled": enabled,
        "safety_margin": safety_margin,
        "freeze_lead_min": freeze_lead_min,
        "overload_factor": overload_factor,
        "max_carts_per_truck": max_carts_cfg,
        "frozen_slots_count": len(frozen_slots),
        "suppliers": sup_summaries,
        "warnings": warnings,
        "info_notes": info_notes,
        "moves_100pct": moves_100pct,
        "notes": notes,
    }


def verify_supplier_day_postcheck(
    sup_rows: List[Dict[str, Any]],
    baselines: Dict[str, Dict[str, Any]],
    expected_day_totals: Dict[Tuple[str, int], float],
    t_slots: List[int],
    frozen_slots: Set[int],
    day_idx: int,
    safety_margin: float,
    *,
    part_margins: Optional[Dict[str, float]] = None,
    sup_cap: Optional[int] = None,
    editable_started_over_cap: Optional[Set[int]] = None,
    box_qty: Optional[Dict[str, float]] = None,
) -> Tuple[bool, str]:
    """Step 4 post-check (#384 R5 & #367b): verifies day-total invariance (`SRM + ADD + Extra`), frozen-slot invariance,
    non-truck invariance, destination cap invariance, and Order-basis shortage / margin rules for every part on `day_idx`."""
    a, b = day_idx * N_SLOTS, (day_idx + 1) * N_SLOTS
    if sup_cap is not None and box_qty is not None:
        started_over = editable_started_over_cap or set()
        for t in t_slots:
            tau = day_idx * N_SLOTS + t
            if tau in frozen_slots or tau in started_over:
                continue
            tot_c = sum(
                r["plan"]["load"][tau] / float(box_qty[r["key"]])
                for r in sup_rows
                if float(box_qty.get(r["key"]) or 0.0) > 0
            )
            if tot_c > float(sup_cap) + 1e-6:
                return False, f"truck slot {tau} pushed above cap {sup_cap} ({tot_c:.2f} carts)"

    for r in sup_rows:
        k = r["key"]
        p = r["plan"]
        base = baselines[k]
        n_days = len(p["load"]) // N_SLOTS
        all_truck_slots = [d * N_SLOTS + t for d in range(n_days) for t in t_slots]

        day_sum = sum(p["load"][a:b])
        exp_sum = expected_day_totals[(k, day_idx)]
        if abs(day_sum - exp_sum) > 1e-6:
            return False, f"part {r['part']} day total {day_sum} != expected {exp_sum}"

        for tau in range(a, b):
            if tau in frozen_slots and abs(p["load"][tau] - base["load"][tau]) > 1e-6:
                return False, f"part {r['part']} modified frozen slot {tau}"

        for s in range(N_SLOTS):
            if s not in t_slots and abs(p["load"][a + s] - base["load"][a + s]) > 1e-6:
                return False, f"part {r['part']} modified non-truck slot {a + s}"

        re_inv = recompute_inv(r["start_sap"], p["load"], p["req"])
        for t_i, (c_v, b_v) in enumerate(zip(re_inv, base["stock"])):
            if abs(c_v - p["stock"][t_i]) > 1e-6:
                return False, f"part {r['part']} stock out of sync at slot {t_i}"
            if b_v >= -1e-9 and c_v < -1e-9:
                return False, f"part {r['part']} new Order-basis shortage at slot {t_i}"
            if b_v < -1e-9 and c_v < b_v - 1e-6:
                return False, f"part {r['part']} worsened unfixed stock at slot {t_i}"

        if any(abs(x - y) > 1e-9 for x, y in zip(p["load"][a:b], base["load"][a:b])):
            eff_m = part_margins.get(k, safety_margin) if part_margins is not None else safety_margin
            ok_g, _, why = check_moved_plan_valid(
                r["start_sap"],
                p["load"],
                p["req"],
                all_truck_slots,
                base["load"],
                base["stock"],
                eff_m,
                target_day=day_idx,
            )
            if not ok_g:
                return False, f"part {r['part']} {why}"

    return True, "ok"
