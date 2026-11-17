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
    base_active = [tau for tau in range(len(baseline_load)) if baseline_load[tau] > 1e-9]
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



def _absorb_other_slots_to_fixed_trucks(
    sup_key: str,
    date_iso: str,
    day_dt: dt.date,
    d: int,
    sup_rows: List[Dict[str, Any]],
    t_slots: List[int],
    editable_trucks: List[int],
    all_truck_slots: List[int],
    frozen_slots: Set[int],
    day_baselines: Dict[str, Dict[str, Any]],
    box_qty: Dict[str, float],
    sup_cap: int,
    safety_margin: float,
    pass2_parts_d: Set[str],
    day_move_log: List[Dict[str, Any]],
    day_moves_100pct: List[Dict[str, Any]],
    moves_100pct: List[Dict[str, Any]],
) -> Tuple[int, List[Dict[str, Any]]]:
    """#403-B1 Step 3-0: Assign SRM quantities in non-fixed ('Other') slots on day `d` onto the supplier's
    fixed editable trucks under the same 115% -> 100% coverage rule, keeping day totals unchanged.
    Any quantity that cannot be placed on ANY fixed truck without creating a shortage is left in place and listed."""
    a = d * N_SLOTS
    other_slots = [s for s in range(N_SLOTS) if s not in t_slots and (a + s) not in frozen_slots]
    if not other_slots or not editable_trucks:
        return 0, []

    def t_carts(tau: int) -> float:
        return sum(
            r["plan"]["load"][tau] / float(box_qty[r["key"]])
            for r in sup_rows
            if float(box_qty.get(r["key"]) or 0.0) > 0
        )

    moves_made = 0
    tau_last = editable_trucks[-1]

    for pass_no, pass_margin in [(1, safety_margin), (2, 0.0)]:
        for s_oth in other_slots:
            tau_src = a + s_oth
            for r in sup_rows:
                k = r["key"]
                p = r["plan"]
                bx = float(box_qty.get(k) or 0.0)
                if bx <= 1e-9:
                    continue
                while p["load"][tau_src] - p["extra"][tau_src] > 1e-9:
                    rem_qty = p["load"][tau_src] - p["extra"][tau_src]
                    step_qty = min(bx, rem_qty) if rem_qty < bx - 1e-9 else bx
                    c_map = {tau: t_carts(tau) for tau in editable_trucks}
                    # Prefer editable trucks with room <= sup_cap (lightest first; earlier slot first on ties so Inv never drops),
                    # then fallback to last editable truck overflow if all fixed trucks are already full.
                    under_cap = sorted(
                        [tau for tau in editable_trucks if c_map[tau] + (step_qty / bx) <= float(sup_cap) + 1e-9],
                        key=lambda tau: (0 if tau < tau_src else 1, round(c_map[tau], 6), tau),
                    )
                    ovf_cands = [tau_last] if tau_last not in under_cap else []
                    # Also allow earlier trucks > cap ONLY if tau_last is blocked by shortage before tau_last
                    other_cands = [tau for tau in editable_trucks if tau not in under_cap and tau not in ovf_cands]
                    cand_dsts = under_cap + ovf_cands + other_cands

                    eff_m = 0.0 if (pass_no == 2 or k in pass2_parts_d) else pass_margin
                    placed = False
                    for tau_dst in cand_dsts:
                        cand_load = list(p["load"])
                        cand_load[tau_src] -= step_qty
                        cand_load[tau_dst] += step_qty
                        if cand_load[tau_src] < 1e-9:
                            cand_load[tau_src] = 0.0
                        ok_mv, cand_inv, _ = check_moved_plan_valid(
                            r["start_sap"],
                            cand_load,
                            p["req"],
                            all_truck_slots,
                            day_baselines[k]["load"],
                            day_baselines[k]["stock"],
                            eff_m,
                            target_day=d,
                        )
                        if ok_mv:
                            p["load"] = cand_load
                            p["stock"] = cand_inv
                            p["unfixed"], p["reason"] = compute_unfixed(cand_inv, p["extra_days"])
                            moves_made += 1
                            m_used = 1.00 if pass_no == 2 else round(1.0 + safety_margin, 2)
                            mv_rec = {
                                "pass": f"OTHER_{pass_no}",
                                "margin_used": m_used,
                                "supplier": sup_key,
                                "date": date_iso,
                                "day_label": f"{day_dt:%a %m/%d}",
                                "part": r["part"],
                                "name": r["name"],
                                "from_truck": SLOT_LABELS[s_oth],
                                "to_truck": SLOT_LABELS[tau_dst % N_SLOTS],
                                "carts": round(step_qty / bx, 2),
                                "qty": round(step_qty, 2),
                            }
                            day_move_log.append(mv_rec)
                            if pass_no == 2:
                                pass2_parts_d.add(k)
                                day_moves_100pct.append(mv_rec)
                                moves_100pct.append(mv_rec)
                            placed = True
                            break
                    if not placed:
                        break

    unplaced: List[Dict[str, Any]] = []
    for s_oth in other_slots:
        tau_src = a + s_oth
        for r in sup_rows:
            q_rem = float(r["plan"]["load"][tau_src])
            if q_rem > 1e-9:
                bx = float(box_qty.get(r["key"]) or 0.0)
                unplaced.append({
                    "part": r["part"],
                    "name": r["name"],
                    "slot": SLOT_LABELS[s_oth],
                    "qty": round(q_rem, 2),
                    "carts": round(q_rem / bx, 2) if bx > 0 else 0.0,
                })
    return moves_made, unplaced


def _overflow_earlier_to_last(
    sup_key: str,
    date_iso: str,
    day_dt: dt.date,
    d: int,
    sup_rows: List[Dict[str, Any]],
    editable_trucks: List[int],
    all_truck_slots: List[int],
    day_baselines: Dict[str, Dict[str, Any]],
    box_qty: Dict[str, float],
    eff_threshold: float,
    safety_margin: float,
    pass2_parts_d: Set[str],
    day_move_log: List[Dict[str, Any]],
    day_moves_100pct: List[Dict[str, Any]],
    moves_100pct: List[Dict[str, Any]],
) -> Tuple[int, bool]:
    """#401a/#401b/#401c Step 3c: push remaining > cap carts from earlier editable trucks to the LAST editable truck
    while keeping every part >= 115% (Pass 1) then >= 100% (Pass 2 fallback), including 1-hop relay via an intermediate
    truck when direct push is blocked by intermediate consumption."""
    a = d * N_SLOTS
    tau_last = editable_trucks[-1]
    extra_moves = 0
    used_ovf = False

    def t_carts(tau: int) -> float:
        return sum(
            r["plan"]["load"][tau] / float(box_qty[r["key"]])
            for r in sup_rows
            if float(box_qty.get(r["key"]) or 0.0) > 0
        )

    # Direct push: tau_src -> tau_last
    for pass_no, pass_margin in [(1, safety_margin), (2, 0.0)]:
        for _ in range(500):
            c_map = {tau: t_carts(tau) for tau in editable_trucks}
            earlier_over = [tau for tau in editable_trucks[:-1] if c_map[tau] > eff_threshold + 1e-9]
            if not earlier_over:
                break
            earlier_over.sort(key=lambda tau: (-round(c_map[tau], 6), -tau))
            touched_now = {
                r["key"]
                for r in sup_rows
                if any(abs(r["plan"]["load"][a + s] - day_baselines[r["key"]]["load"][a + s]) > 1e-9 for s in range(N_SLOTS))
            }
            moved = False
            for tau_src in earlier_over:
                excess = max(1, int(math.ceil(c_map[tau_src] - eff_threshold - 1e-9)))
                best = None
                for r in sup_rows:
                    k = r["key"]
                    bx = float(box_qty.get(k) or 0.0)
                    if bx <= 1e-9:
                        continue
                    p = r["plan"]
                    mov_c = int(math.floor((p["load"][tau_src] - p["extra"][tau_src]) / bx + 1e-9))
                    if mov_c < 1:
                        continue
                    eff_m = 0.0 if (pass_no == 2 or k in pass2_parts_d) else pass_margin
                    chosen_k, chosen_l, chosen_i = 0, None, None
                    for k_try in range(min(mov_c, excess), 0, -1):
                        cl = list(p["load"])
                        cl[tau_src] -= k_try * bx
                        cl[tau_last] += k_try * bx
                        if cl[tau_src] < 1e-9:
                            cl[tau_src] = 0.0
                        ok_mv, ci, _ = check_moved_plan_valid(
                            r["start_sap"], cl, p["req"], all_truck_slots,
                            day_baselines[k]["load"], day_baselines[k]["stock"], eff_m, target_day=d,
                        )
                        if ok_mv:
                            chosen_k, chosen_l, chosen_i = k_try, cl, ci
                            break
                    if chosen_k >= 1 and chosen_l is not None and chosen_i is not None:
                        act = [t_i for t_i in all_truck_slots if p["load"][t_i] > 1e-9]
                        slack = _min_slack_between(p["stock"], p["req"], act, tau_src, tau_last, eff_m)
                        rk = (-chosen_k, -(1 if k in touched_now else 0), -(1 if mov_c == chosen_k else 0), -round(slack, 6), -mov_c, bx, r["part"])
                        if best is None or rk < best[0]:
                            best = (rk, r, chosen_k, chosen_l, chosen_i)
                if best is not None:
                    _, rw, kw, lw, iw = best
                    pw = rw["plan"]
                    pw["load"], pw["stock"] = lw, iw
                    pw["unfixed"], pw["reason"] = compute_unfixed(iw, pw["extra_days"])
                    extra_moves += 1
                    used_ovf = True
                    bxw = float(box_qty[rw["key"]])
                    mu = 1.00 if pass_no == 2 else round(1.0 + safety_margin, 2)
                    rec = {
                        "pass": f"OVF_{pass_no}", "margin_used": mu, "supplier": sup_key, "date": date_iso,
                        "day_label": f"{day_dt:%a %m/%d}", "part": rw["part"], "name": rw["name"],
                        "from_truck": SLOT_LABELS[tau_src % N_SLOTS], "to_truck": SLOT_LABELS[tau_last % N_SLOTS],
                        "carts": kw, "qty": round(kw * bxw, 2),
                    }
                    day_move_log.append(rec)
                    if pass_no == 2:
                        pass2_parts_d.add(rw["key"])
                        day_moves_100pct.append(rec)
                        moves_100pct.append(rec)
                    moved = True
                    break
            if not moved:
                break

    # 1-hop relay: tau_src -> tau_mid (<= cap) + tau_mid -> tau_last
    if any(t_carts(tau) > eff_threshold + 1e-9 for tau in editable_trucks[:-1]):
        for pass_no, pass_margin in [(1, safety_margin), (2, 0.0)]:
            for _ in range(200):
                c_map = {tau: t_carts(tau) for tau in editable_trucks}
                earlier_over = [tau for tau in editable_trucks[:-1] if c_map[tau] > eff_threshold + 1e-9]
                if not earlier_over:
                    break
                earlier_over.sort(key=lambda tau: (-round(c_map[tau], 6), -tau))
                relayed = False
                for tau_src in earlier_over:
                    for tau_mid in editable_trucks[:-1]:
                        if tau_mid == tau_src:
                            continue
                        cand_b = None
                        for rb in sup_rows:
                            kb = rb["key"]
                            bxb = float(box_qty.get(kb) or 0.0)
                            if bxb <= 1e-9 or rb["plan"]["load"][tau_mid] - rb["plan"]["extra"][tau_mid] + 1e-9 < bxb:
                                continue
                            mb = 0.0 if (pass_no == 2 or kb in pass2_parts_d) else pass_margin
                            clb = list(rb["plan"]["load"])
                            clb[tau_mid] -= bxb
                            clb[tau_last] += bxb
                            if clb[tau_mid] < 1e-9:
                                clb[tau_mid] = 0.0
                            okb, cib, _ = check_moved_plan_valid(
                                rb["start_sap"], clb, rb["plan"]["req"], all_truck_slots,
                                day_baselines[kb]["load"], day_baselines[kb]["stock"], mb, target_day=d,
                            )
                            if okb:
                                cand_b = (rb, bxb, clb, cib)
                                break
                        if cand_b is None:
                            continue
                        rb, bxb, clb, cib = cand_b
                        cand_a = None
                        for ra in sup_rows:
                            ka = ra["key"]
                            if ka == rb["key"]:
                                continue
                            bxa = float(box_qty.get(ka) or 0.0)
                            if bxa <= 1e-9 or ra["plan"]["load"][tau_src] - ra["plan"]["extra"][tau_src] + 1e-9 < bxa:
                                continue
                            ma = 0.0 if (pass_no == 2 or ka in pass2_parts_d) else pass_margin
                            cla = list(ra["plan"]["load"])
                            cla[tau_src] -= bxa
                            cla[tau_mid] += bxa
                            if cla[tau_src] < 1e-9:
                                cla[tau_src] = 0.0
                            oka, cia, _ = check_moved_plan_valid(
                                ra["start_sap"], cla, ra["plan"]["req"], all_truck_slots,
                                day_baselines[ka]["load"], day_baselines[ka]["stock"], ma, target_day=d,
                            )
                            if oka:
                                cand_a = (ra, bxa, cla, cia)
                                break
                        if cand_a is not None:
                            ra, bxa, cla, cia = cand_a
                            for rx, bxx, clx, cix, ft, tt in ((rb, bxb, clb, cib, tau_mid, tau_last), (ra, bxa, cla, cia, tau_src, tau_mid)):
                                px = rx["plan"]
                                px["load"], px["stock"] = clx, cix
                                px["unfixed"], px["reason"] = compute_unfixed(cix, px["extra_days"])
                                extra_moves += 1
                                mu = 1.00 if pass_no == 2 else round(1.0 + safety_margin, 2)
                                mrec = {
                                    "pass": f"RELAY_{pass_no}", "margin_used": mu, "supplier": sup_key, "date": date_iso,
                                    "day_label": f"{day_dt:%a %m/%d}", "part": rx["part"], "name": rx["name"],
                                    "from_truck": SLOT_LABELS[ft % N_SLOTS], "to_truck": SLOT_LABELS[tt % N_SLOTS],
                                    "carts": 1, "qty": round(bxx, 2),
                                }
                                day_move_log.append(mrec)
                                if pass_no == 2:
                                    pass2_parts_d.add(rx["key"])
                                    day_moves_100pct.append(mrec)
                                    moves_100pct.append(mrec)
                            used_ovf = True
                            relayed = True
                            break
                    if relayed:
                        break
                if not relayed:
                    break

    return extra_moves, used_ovf


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
    freeze_departed = bool(leveling_cfg.get("freeze_departed", True))
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
    frozen_slots = compute_frozen_slots(run_date, n_days, run_ts, freeze_lead_min) if freeze_departed else set()

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

        for r in sup_rows:
            r["plan"]["max_carts_cap"] = sup_cap
            r["plan"]["frozen_slots"] = set(frozen_slots)
        sup_moves = 0
        sup_fallbacks = 0
        sup_constrained: List[str] = []
        sup_extra_warns: List[str] = []
        sup_day_warns_map: Dict[str, List[str]] = {}
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
            other_carts_before = round(
                sum(
                    r["plan"]["load"][a + s] / float(box_qty[r["key"]])
                    for r in sup_rows
                    if float(box_qty.get(r["key"]) or 0.0) > 0
                    for s in range(N_SLOTS)
                    if s not in t_slots
                ),
                1,
            )
            unplaced_other_d: List[Dict[str, Any]] = []

            if enabled and sup_cap is not None and editable_trucks and other_carts_before > 1e-9:
                oth_mv, unplaced_other_d = _absorb_other_slots_to_fixed_trucks(
                    sup_key, date_iso, dates[d], d, sup_rows, t_slots, editable_trucks,
                    all_truck_slots, frozen_slots, day_baselines, box_qty, sup_cap,
                    safety_margin, pass2_parts_d, day_move_log, day_moves_100pct, moves_100pct,
                )
                sup_moves += oth_mv
                for uo in unplaced_other_d:
                    umsg = (
                        f"Unplaced non-truck SRM order kept at {uo['slot']} to prevent shortage: "
                        f"{sup_key} {dates[d]:%a %m/%d} {uo['part']} — {uo['name']} "
                        f"({uo['carts']} carts / {uo['qty']:,.0f} EA)"
                    )
                    sup_constrained.append(umsg)
                    notes.append(umsg)

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
                                if sup_cap is not None and tau_src == editable_trucks[-1]:
                                    min_last_add = max(float(sup_cap), sum(c_map.values()) - float(sup_cap) * (len(editable_trucks) - 1))
                                    if min_last_add > float(sup_cap) + 1e-9 and c_map[tau_src] <= min_last_add + 1e-9:
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

                # Step 3b: Overload / Cap relief into trucks <= eff_threshold
                blocked_by_shortage = False
                overflow_to_last_used = False
                editable_sum = sum(truck_carts(tau) for tau in editable_trucks)
                tau_last = editable_trucks[-1]
                if sup_cap is not None:
                    eff_threshold = float(sup_cap)
                    min_last_target = max(eff_threshold, editable_sum - eff_threshold * (len(editable_trucks) - 1))
                    passes: List[Tuple[int, float]] = [(1, safety_margin), (2, 0.0)]
                else:
                    eff_threshold = max(threshold, editable_sum / len(editable_trucks))
                    min_last_target = eff_threshold
                    passes = [(1, safety_margin)]

                for pass_no, pass_margin in passes:
                    c_pre = {tau: truck_carts(tau) for tau in editable_trucks}
                    if pass_no == 2:
                        has_over = any(
                            c_pre[tau] > (min_last_target if (sup_cap is not None and tau == tau_last) else threshold) + 1e-9
                            for tau in editable_trucks
                        )
                        has_room = any(c_pre[tau] + 1.0 <= eff_threshold + 1e-9 for tau in editable_trucks)
                        if not (has_over and has_room):
                            break

                    for _ in range(500):
                        c_map = {tau: truck_carts(tau) for tau in editable_trucks}
                        overloaded = [
                            tau for tau in editable_trucks
                            if c_map[tau] > (min_last_target if (sup_cap is not None and tau == tau_last) else threshold) + 1e-9
                        ]
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
                            src_target = min_last_target if (sup_cap is not None and tau_src == tau_last) else eff_threshold
                            if c_map[tau_src] <= src_target + 1e-9:
                                continue
                            excess_carts = max(1, int(math.ceil(c_map[tau_src] - src_target - 1e-9)))
                            dst_trucks = sorted(
                                [tau for tau in editable_trucks if tau != tau_src and c_map[tau] + 1.0 <= eff_threshold + 1e-9],
                                key=lambda tau: (1 if (sup_cap is not None and tau == tau_last) else 0, round(c_map[tau], 6), tau),
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

                if sup_cap is not None and any(truck_carts(tau) > eff_threshold + 1e-9 for tau in editable_trucks[:-1]):
                    ovf_mv, overflow_to_last_used = _overflow_earlier_to_last(
                        sup_key, date_iso, dates[d], d, sup_rows, editable_trucks, all_truck_slots,
                        day_baselines, box_qty, eff_threshold, safety_margin, pass2_parts_d,
                        day_move_log, day_moves_100pct, moves_100pct,
                    )
                    sup_moves += ovf_mv

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
                    allow_last_truck_overflow=overflow_to_last_used or (
                        sup_cap is not None and editable_sum > float(sup_cap) * len(editable_trucks) + 1e-9
                    ),
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

                if sup_cap is None:
                    c_after_uncap = {tau: truck_carts(tau) for tau in editable_trucks}
                    still_over_uncap = [tau for tau in editable_trucks if c_after_uncap[tau] > threshold + 1e-9]
                    if still_over_uncap and blocked_by_shortage:
                        tau_heavy = max(still_over_uncap, key=lambda t_i: (c_after_uncap[t_i], t_i))
                        msg = (
                            f"Overloaded truck kept to prevent shortage: {sup_key} "
                            f"{dates[d]:%a %m/%d} {SLOT_LABELS[tau_heavy % N_SLOTS]} "
                            f"({c_after_uncap[tau_heavy]:.0f} carts > {threshold:.1f} threshold; "
                            f"min editable truck {min(c_after_uncap.values()):.0f} carts)."
                        )
                        sup_constrained.append(msg)
                        notes.append(msg)

            # #401c Final Extra-Truck Trigger & Coverage Info Rule
            if enabled and sup_cap is not None and day_truck_slots:
                n_trk = len(day_truck_slots)
                cap_tot = int(sup_cap) * n_trk
                day_tot_carts = sum(truck_carts(tau) for tau in day_truck_slots)
                day_tot_int = int(round(day_tot_carts))
                day_lbl = f"{dates[d]:%a} {dates[d].month}/{dates[d].day}"
                if day_tot_carts > float(cap_tot) + 1e-9:
                    excess_tot = max(1, day_tot_int - cap_tot)
                    warn_line = (
                        f"Extra truck needed: {day_lbl} total {day_tot_int} carts > {n_trk} trucks \u00d7 {sup_cap} = {cap_tot}. "
                        f"Please arrange an additional truck for {excess_tot} carts."
                    )
                    sup_day_warns_map[date_iso] = [warn_line]
                    sup_extra_warns.append(warn_line)
                    full_msg = f"{sup_key} \u2014 {warn_line}"
                    sup_constrained.append(full_msg)
                    warnings.append(full_msg)
                    notes.append(full_msg)
                    for tau_e in editable_trucks[:-1]:
                        ce = truck_carts(tau_e)
                        if ce > float(sup_cap) + 1e-9:
                            cov_msg = f"Truck {SLOT_LABELS[tau_e % N_SLOTS]} {int(round(ce))} carts (> {sup_cap}) kept for coverage"
                            sup_info.append(cov_msg)
                            info_notes.append(f"{sup_key} {day_lbl}: {cov_msg}")
                else:
                    for tau_e in editable_trucks:
                        ce = truck_carts(tau_e)
                        if ce > float(sup_cap) + 1e-9:
                            cov_msg = f"Truck {SLOT_LABELS[tau_e % N_SLOTS]} {int(round(ce))} carts (> {sup_cap}) kept for coverage"
                            sup_info.append(cov_msg)
                            full_cov = f"{sup_key} {day_lbl}: {cov_msg}"
                            info_notes.append(full_cov)
                            notes.append(full_cov)

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

            over_cap_detail: List[Dict[str, Any]] = []
            if sup_cap is not None and editable_trucks:
                cap_tot_d = float(sup_cap) * len(day_truck_slots)
                day_sum_d = sum(truck_carts(tau) for tau in day_truck_slots)
                for tau_h in editable_trucks:
                    ch = truck_carts(tau_h)
                    if ch > float(sup_cap) + 1e-9:
                        is_last_h = tau_h == editable_trucks[-1]
                        over_cap_detail.append({
                            "slot": SLOT_LABELS[tau_h % N_SLOTS],
                            "carts": round(ch, 1),
                            "excess_carts": max(1, int(round(ch)) - int(sup_cap)),
                            "is_last_truck": is_last_h,
                            "why": "last_truck_overflow" if (is_last_h and day_sum_d > cap_tot_d + 1e-9) else "coverage_required",
                        })

            sup_days_report[date_iso] = {
                "truck_labels": [SLOT_LABELS[t] for t in t_slots],
                "frozen_trucks": [SLOT_LABELS[t] for t in t_slots if (d * N_SLOTS + t) in frozen_slots],
                "over_cap_trucks": over_cap_detail,
                "extra_truck_warnings": list(sup_day_warns_map.get(date_iso, [])),
                "max_carts_cap": sup_cap,
                "day_avg": round(day_avg, 2),
                "threshold": round(threshold, 2),
                "trucks_before": [round(x, 1) for x in carts_before],
                "trucks_after": [round(x, 1) for x in carts_after],
                "other_carts_before": other_carts_before,
                "other_carts_after": round(
                    sum(
                        r["plan"]["load"][a + s] / float(box_qty[r["key"]])
                        for r in sup_rows
                        if float(box_qty.get(r["key"]) or 0.0) > 0
                        for s in range(N_SLOTS)
                        if s not in t_slots
                    ),
                    1,
                ),
                "unplaced_other_slots": unplaced_other_d,
                "parts_touched": len(touched_parts_detail),
                "carts_moved": round(sum(tp["carts_moved"] for tp in touched_parts_detail), 1),
                "moves_100pct_count": len(day_moves_100pct),
                "moves_100pct": day_moves_100pct,
                "move_log": day_move_log,
                "touched_parts_detail": touched_parts_detail,
            }

        for r in sup_rows:
            r["plan"]["day_extra_truck_warnings"] = dict(sup_day_warns_map)

        sup_summaries[sup_key] = {
            "max_carts_cap": sup_cap,
            "moves_accepted": sup_moves,
            "fallbacks": sup_fallbacks,
            "constrained_trucks": sup_constrained,
            "extra_truck_warnings": sup_extra_warns,
            "info_notes": sup_info,
            "days": sup_days_report,
        }

    return {
        "enabled": enabled,
        "freeze_departed": freeze_departed,
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
    allow_last_truck_overflow: bool = False,
) -> Tuple[bool, str]:
    """Step 4 post-check (#384 R5 & #367b): verifies day-total invariance (`SRM + ADD + Extra`), frozen-slot invariance,
    non-truck invariance, destination cap invariance, and Order-basis shortage / margin rules for every part on `day_idx`."""
    a, b = day_idx * N_SLOTS, (day_idx + 1) * N_SLOTS
    if sup_cap is not None and box_qty is not None:
        started_over = editable_started_over_cap or set()
        ed_day = [day_idx * N_SLOTS + t for t in t_slots if (day_idx * N_SLOTS + t) not in frozen_slots]
        tau_last_d = ed_day[-1] if ed_day else None
        for t in t_slots:
            tau = day_idx * N_SLOTS + t
            if tau in frozen_slots or tau in started_over:
                continue
            if allow_last_truck_overflow and tau == tau_last_d:
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
            if s not in t_slots:
                if sup_cap is not None and (a + s) not in frozen_slots:
                    if p["load"][a + s] < -1e-6 or p["load"][a + s] > base["load"][a + s] + 1e-6:
                        return False, f"part {r['part']} invalid non-truck slot {a + s} load {p['load'][a + s]}"
                elif abs(p["load"][a + s] - base["load"][a + s]) > 1e-6:
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
