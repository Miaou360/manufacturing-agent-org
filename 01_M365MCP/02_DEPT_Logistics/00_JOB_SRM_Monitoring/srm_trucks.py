"""
02_DEPT_Logistics / 00_JOB_SRM_Monitoring - truck loading plan (2-hour view only; user decisions 11/12, #367, #384 & #367b)
===========================================================================================================================
Rules (#384 minimal change from SRM order + #367b per-supplier hard cart cap):
  * Suppliers with fixed truck slots only (data/suppliers.csv, not in git), Plan & Require classification C only
    (several deliveries a day). Class L (once a day) comes on the Pallet Truck and is not reloaded. Daily view: no trucks.
  * Per SRM day (06:00 -> 06:00), the day total (ADD + slots) never changes; only that day's trucks are used.
  * Step 2 (plan_trucks = R1 baseline):
      - Parts with NO shortage on a day (Inv >= 0 across all slots of that day) and ADD == 0 are left strictly as
        SRM ordered, even if below 115% (do not pull just for the margin; #384 R5).
      - Parts with an actual shortage (Inv < 0) or urgent ADD > 0 pull forward in Box_QTY multiples keeping
        (1 + safety_margin) = 115% default of the next truck's arrival-slot requirement; the buffer never creates extra.
      - Trucks whose departure is <= run_time + freeze_lead_min are frozen (neither donor nor recipient; #384 R2).
  * Step 3 (srm_leveling):
      - Supplier WITH `max_carts_per_truck` cap: unload editable trucks > cap to editable trucks staying <= cap
        (Pass 1 at 115%, Pass 2 fallback at 100% = Inv(S-1) >= Req(S) & Inv >= 0; warn only if still > cap).
      - Supplier WITHOUT a cap: unload editable trucks > overload_factor (1.7) x day average at 115% (#384 R3-R5).
"""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from srm_parsers import N_SLOTS, SLOT_LABELS

CONFIG = Path(__file__).resolve().parent / "config" / "trucks.json"
SUPPLIERS = Path(__file__).resolve().parent / "data" / "suppliers.csv"   # supplier names: kept out of git
MIN_SAFETY_MARGIN = 0.10
DEFAULT_SAFETY_MARGIN = 0.15
DEFAULT_FREEZE_LEAD_MIN = 30.0
DEFAULT_OVERLOAD_FACTOR = 1.7
DELAY_BUFFER = 1.0 + DEFAULT_SAFETY_MARGIN
FIRST_TRUCK_REASON = "Before the first truck of the day - earlier or extra truck needed"
PENDING_REASON = "Orders for this day come with the 13:40 SRM update"


def load_suppliers(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """data/suppliers.csv (not in git - supplier names; the pre-commit hook blocks them):
    match  = case-insensitive part of the SRM supplier name      trucks = SRM slot names separated by spaces
    mail   = yes / no (gets the truck plan mail)                  to_env / cc_env = .env key names of the addresses
    max_carts = optional integer per-truck cart cap for this supplier."""
    p = path or SUPPLIERS
    if not p.exists():
        return []
    rows: List[Dict[str, Any]] = []
    with p.open(encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            match = (r.get("match") or "").strip()
            if not match:
                continue
            slots = (r.get("trucks") or "").split()
            bad = [s for s in slots if s not in SLOT_LABELS]
            if bad:
                raise ValueError(f"data/suppliers.csv: unknown SRM slot {bad} for '{match}' (use e.g. 10-12)")
            raw_cap = (r.get("max_carts") or r.get("max_carts_per_truck") or "").strip()
            cap_val: Optional[int] = None
            if raw_cap:
                cap_val = int(raw_cap)
                if cap_val < 1:
                    raise ValueError(f"data/suppliers.csv: max_carts must be >= 1 for '{match}', got {raw_cap!r}")
            rec: Dict[str, Any] = {
                "match": match,
                "trucks": sorted(SLOT_LABELS.index(s) for s in slots),
                "mail": (r.get("mail") or "").strip().lower() in ("yes", "y", "true", "1"),
                "to_env": (r.get("to_env") or "").strip(),
                "cc_env": (r.get("cc_env") or "").strip(),
            }
            if cap_val is not None:
                rec["max_carts"] = cap_val
            rows.append(rec)
    return rows


def load_trucks(path: Optional[Path] = None) -> Dict[str, List[int]]:
    return {r["match"]: r["trucks"] for r in load_suppliers(path) if r["trucks"]}


def load_supplier_caps(path: Optional[Path] = None) -> Dict[str, int]:
    return {r["match"]: int(r["max_carts"]) for r in load_suppliers(path) if r.get("max_carts") is not None}


PM_EXTRA_MODES = ("next_run_day", "tomorrow", "all")
RUN_DAYS = ("MonSat", "Daily")


def _truck_config(path: Optional[Path] = None) -> Dict[str, Any]:
    p = path or CONFIG
    return json.loads(p.read_text(encoding="utf-8-sig")) if p.exists() else {}


def pm_additional_days(path: Optional[Path] = None) -> str:
    """Days of the 13:40 run that may get an additional quantity (config/trucks.json)."""
    v = _truck_config(path).get("pm_additional_days", "all")
    if v not in PM_EXTRA_MODES:
        raise ValueError(f"config/trucks.json pm_additional_days = {v!r}; expected one of {', '.join(PM_EXTRA_MODES)}")
    return v


def run_days(path: Optional[Path] = None) -> str:
    """'MonSat' (user 11/12) or 'Daily' - the days of the scheduled runs."""
    v = _truck_config(path).get("run_days", "MonSat")
    if v not in RUN_DAYS:
        raise ValueError(f"config/trucks.json run_days = {v!r}; expected one of {', '.join(RUN_DAYS)}")
    return v


def _validate_caps_dict(raw_caps: Any, where: str) -> Dict[str, int]:
    if raw_caps is None:
        return {}
    if not isinstance(raw_caps, dict):
        raise ValueError(f"config/trucks.json {where} must be a JSON object mapping supplier/schedule key -> int")
    out: Dict[str, int] = {}
    for k, v in raw_caps.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v or int(v) < 1:
            raise ValueError(f"config/trucks.json {where}[{k!r}] = {v!r}; expected integer >= 1")
        out[str(k).strip()] = int(v)
    return out


def _resolve_config_caps(raw_caps: Dict[str, int], suppliers_rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Resolves `max_carts_per_truck` keys against `data/suppliers.csv` rows so supplier names stay out of git (#367b):
    - If a key is a space-separated SRM truck slot schedule (e.g. '8-10 12-14 16-18 20-22 02-04'), translates it to the
      matching `r['match']` supplier key(s) in `data/suppliers.csv` (leaves no wildcard schedule key if unmatched).
    - Direct supplier substring keys (e.g. in unit tests or custom configs) are kept as-is."""
    resolved: Dict[str, int] = {}
    for k, cap in raw_caps.items():
        toks = k.split()
        if toks and all(tok in SLOT_LABELS for tok in toks):
            slot_sig = sorted(SLOT_LABELS.index(tok) for tok in toks)
            for r in suppliers_rows:
                if r.get("trucks") == slot_sig:
                    resolved[r["match"]] = int(cap)
        else:
            resolved[k] = int(cap)
    return resolved


def leveling_config(path: Optional[Path] = None, suppliers_path: Optional[Path] = None) -> Dict[str, Any]:
    """Reads and validates `leveling` section of config/trucks.json (#367 / #384 / #367b).
    safety_margin is strictly enforced >= 0.10 (10%); overload_factor must be > 1.0;
    max_carts_per_truck maps supplier key (resolved via data/suppliers.csv) -> int >= 1."""
    cfg_root = _truck_config(path)
    raw = cfg_root.get("leveling")
    sup_rows = load_suppliers(suppliers_path) if (path is None or suppliers_path is not None) else []
    csv_caps = {r["match"]: int(r["max_carts"]) for r in sup_rows if r.get("max_carts") is not None}
    top_caps = _resolve_config_caps(
        _validate_caps_dict(cfg_root.get("max_carts_per_truck"), "max_carts_per_truck"),
        sup_rows,
    )
    if raw is None:
        merged_caps = {**csv_caps, **top_caps}
        return {
            "enabled": True,
            "safety_margin": DEFAULT_SAFETY_MARGIN,
            "freeze_lead_min": DEFAULT_FREEZE_LEAD_MIN,
            "overload_factor": DEFAULT_OVERLOAD_FACTOR,
            "max_carts_per_truck": merged_caps,
        }
    if not isinstance(raw, dict):
        raise ValueError("config/trucks.json leveling must be a JSON object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"config/trucks.json leveling.enabled = {enabled!r}; expected boolean")
    margin = float(raw.get("safety_margin", DEFAULT_SAFETY_MARGIN))
    if margin < MIN_SAFETY_MARGIN - 1e-9:
        raise ValueError(
            f"config/trucks.json leveling.safety_margin = {margin!r}; must be >= {MIN_SAFETY_MARGIN:.2f} (10%)"
        )
    lead = float(raw.get("freeze_lead_min", DEFAULT_FREEZE_LEAD_MIN))
    if lead < -1e-9:
        raise ValueError(f"config/trucks.json leveling.freeze_lead_min = {lead!r}; must be >= 0")
    overload = float(raw.get("overload_factor", DEFAULT_OVERLOAD_FACTOR))
    if overload <= 1.0 + 1e-9:
        raise ValueError(f"config/trucks.json leveling.overload_factor = {overload!r}; must be > 1.0")
    lvl_caps = _resolve_config_caps(
        _validate_caps_dict(raw.get("max_carts_per_truck"), "leveling.max_carts_per_truck"),
        sup_rows,
    )
    merged_caps = {**csv_caps, **top_caps, **lvl_caps}
    return {
        "enabled": enabled,
        "safety_margin": margin,
        "freeze_lead_min": lead,
        "overload_factor": overload,
        "max_carts_per_truck": merged_caps,
    }


def cap_for_supplier(
    sup_key: str,
    vendor: str,
    t_slots: List[int],
    max_carts_cfg: Optional[Dict[str, int]],
) -> Optional[int]:
    """Resolves a supplier's per-truck cart cap (#367b) by case-insensitive substring match against `sup_key` or `vendor`
    (the exact same key resolution rule as `trucks_for`)."""
    if not max_carts_cfg:
        return None
    sk_low = (sup_key or "").lower()
    v_low = (vendor or "").lower()
    for name, cap in max_carts_cfg.items():
        nl = name.strip().lower()
        if nl and (nl in sk_low or nl in v_low):
            return int(cap)
    return None


def trucks_for(vendor: str, cfg: Dict[str, List[int]]) -> Optional[List[int]]:
    v = (vendor or "").lower()
    for name, slots in cfg.items():
        if name.lower() in v:
            return slots
    return None


def _boxes(qty: float, box: float) -> float:
    return qty if box <= 0 else math.ceil(qty / box - 1e-9) * box


def plan_trucks(
    start: float,
    add: Any,
    slots: List[float],
    req: List[float],
    truck_slots: List[int],
    box: float,
    extra_days: Optional[set] = None,
    delay_buffer: float = DELAY_BUFFER,
    frozen_slots: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """slots/req: n_days x 12 values; add: urgent ADD for day 0 (float) or one value per day (list).
    frozen_slots: horizon slot indices whose trucks have already departed or depart <= run_time + lead;
    frozen trucks keep their original SRM slot load (neither donor nor recipient).
    Per #384 R5: if a day has no shortage (Inv >= 0 across all 12 slots) and ADD == 0, SRM orders on that day
    stay completely untouched (even if below delay_buffer = 115%)."""
    n_days = len(slots) // N_SLOTS
    q = list(slots)
    extra = [0.0] * len(q)
    add_by_slot = [0.0] * len(q)
    moves: List[Dict[str, Any]] = []
    trucks = [d * N_SLOTS + t for d in range(n_days) for t in truck_slots]
    frozen = set(frozen_slots or ())
    adds = list(add) if isinstance(add, (list, tuple)) else [add]
    pool = {d: (adds[d] if d < len(adds) else 0.0) for d in range(n_days)}
    for d in range(n_days):
        a_d, b_d = d * N_SLOTS, (d + 1) * N_SLOTS
        sim_inv = start + sum(q[:a_d]) - sum(req[:a_d])
        day_has_shortage = False
        for i_s in range(a_d, b_d):
            sim_inv += q[i_s] - req[i_s]
            if sim_inv < -1e-9:
                day_has_shortage = True
                break
        if not day_has_shortage and pool.get(d, 0.0) <= 1e-9:
            continue

        day_trucks = [t for t in trucks if t // N_SLOTS == d]
        editable_day_trucks = [t for t in day_trucks if t not in frozen]
        for pos, t in enumerate(day_trucks):
            if t in frozen:
                continue
            nxt = next((x for x in trucks if x > t), n_days * N_SLOTS)
            inv = start + sum(q[:t]) - sum(req[:t])
            need = sum(req[t:nxt]) - (inv + q[t])
            buffer = delay_buffer * req[nxt] if nxt < len(req) else 0.0
            if need + buffer <= 1e-9:
                continue
            want = _boxes(need + buffer, box)
            hard = _boxes(need, box) if need > 1e-9 else 0.0
            take = min(want, pool.get(d, 0.0))
            if take > 0:
                q[t] += take
                add_by_slot[t] += take
                pool[d] -= take
                moves.append({"from": "ADD", "to": t, "qty": take})
                want -= take
            if day_has_shortage:
                for later in day_trucks[pos + 1:]:
                    if later in frozen:
                        continue
                    if want <= 1e-9:
                        break
                    take = min(want, q[later] - extra[later])
                    if take > 0:
                        q[later] -= take
                        q[t] += take
                        moves.append({"from": later, "to": t, "qty": take})
                        want -= take
            covered = _boxes(need + buffer, box) - want
            missing = max(0.0, hard - covered)
            if missing > 1e-9 and (extra_days is None or d in extra_days):
                q[t] += missing
                extra[t] += missing
                moves.append({"from": "EXTRA", "to": t, "qty": missing})
        for later in reversed(day_trucks):
            for mv in [m for m in moves if m["from"] == later]:
                x = min(extra[later], mv["qty"])
                if x > 1e-9:
                    extra[later] -= x
                    extra[mv["to"]] += x
                    mv["qty"] -= x
                    moves.append({"from": "EXTRA", "to": mv["to"], "qty": x, "relabel_of": later})
        if pool.get(d, 0.0) > 1e-9 and (editable_day_trucks or day_trucks):
            target_last = editable_day_trucks[-1] if editable_day_trucks else day_trucks[-1]
            q[target_last] += pool[d]
            add_by_slot[target_last] += pool[d]
            moves.append({"from": "ADD", "to": target_last, "qty": pool[d]})
            pool[d] = 0.0
    inv, unfixed, reason = start, None, None
    for i in range(n_days * N_SLOTS):
        inv += q[i] - req[i]
        if inv < -1e-9 and unfixed is None:
            unfixed = i
            d_short = i // N_SLOTS
            reason = (FIRST_TRUCK_REASON if extra_days is None or d_short in extra_days else PENDING_REASON)
    return {
        "slots": q,
        "extra": extra,
        "add_by_slot": add_by_slot,
        "moves": moves,
        "unfixed": unfixed,
        "reason": reason,
    }


def daily_extras(start: float, supply: List[float], req: List[float], box: float,
                 extra_days: Optional[set] = None) -> List[float]:
    """Once-a-day items (class L) and suppliers without truck slots: minimum extra quantity (Box_QTY multiples)
    delivered at the start of a day so the stock never goes below zero. Not in SRM - buyer to confirm."""
    extra = [0.0] * len(supply)
    inv = start
    for d in range(len(supply) // N_SLOTS):
        lo, run = 0.0, inv
        for i in range(d * N_SLOTS, (d + 1) * N_SLOTS):
            run += supply[i] - req[i]
            lo = min(lo, run)
        if lo < -1e-9 and (extra_days is None or d in extra_days):
            extra[d * N_SLOTS] = _boxes(-lo, box)
            run += extra[d * N_SLOTS]
        inv = run
    return extra
