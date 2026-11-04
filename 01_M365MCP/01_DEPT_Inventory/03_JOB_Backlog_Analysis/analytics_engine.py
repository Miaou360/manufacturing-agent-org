"""
Backlog Analytics Engine (03_Backlog_Analysis)
==============================================
Multi-Dimensional Time-Series Analysis of SAP COGI Raw History:
1. Frequency vs Volume 4-Quadrant Matrix (만성적 vs 돌발적)
2. Resolution Breakdown (가짜 백로그 MB1B 311 해소율 vs 진짜 결품)
3. Aging & Duration Analysis (생산일 대비 체류일수)
4. Turnover Days & Mover Class Fusion (회전일수 온톨로지 결합)
"""

import os
import sys
import json
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import numpy as np

from config import (
    DB_BACKLOG_PATH,
    ONTOLOGY_PATH,
    HIGH_FREQ_PCT_THRESHOLD,
    HIGH_VOL_PERCENTILE,
    TARGET_START_DATE,
    MILESTONE_ANALYSIS_DATE
)


class BacklogAnalyticsEngine:
    def __init__(self, db_path: Path = DB_BACKLOG_PATH, ontology_path: Path = ONTOLOGY_PATH):
        self.db_path = db_path
        self.ontology_path = ontology_path
        self.ontology_map = self._load_ontology()

    def _load_ontology(self) -> Dict[str, Dict[str, Any]]:
        """Loads 1,705 materials turnover ontology master."""
        if not self.ontology_path.exists():
            return {}
        try:
            with open(self.ontology_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data.get("materials", data)
                return {}
        except Exception:
            return {}

    def fetch_raw_history(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Fetches raw backlog snapshots and list of distinct audit dates."""
        if not self.db_path.exists():
            return [], []

        query = """
            SELECT id, history_id, run_date, run_time, material, material_desc, 
                   plant, sloc, mty, qty, uoe, error_code, entry_date, error_date
            FROM raw_backlog_history
            WHERE 1=1
        """
        params = []
        if start_date:
            query += " AND run_date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND run_date <= ?"
            params.append(end_date)

        query += " ORDER BY run_date ASC, run_time ASC, material ASC;"

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        rows = [dict(r) for r in c.fetchall()]

        # Distinct run dates
        c.execute("""
            SELECT DISTINCT run_date FROM raw_backlog_history 
            WHERE 1=1 """ + (" AND run_date >= ?" if start_date else "") + (" AND run_date <= ?" if end_date else "") + """
            ORDER BY run_date ASC;
        """, params)
        distinct_dates = [r[0] for r in c.fetchall()]
        conn.close()

        return rows, distinct_dates

    def fetch_transfer_totals(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None
    ) -> Dict[str, float]:
        """Fetches total transferred quantity per material via MB1B 311."""
        if not self.db_path.exists():
            return {}

        query = """
            SELECT material, SUM(transfer_qty) as total_transferred
            FROM backlog_transfers
            WHERE 1=1
        """
        params = []
        if start_date:
            query += " AND SUBSTR(executed_at, 1, 10) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND SUBSTR(executed_at, 1, 10) <= ?"
            params.append(end_date)
        query += " GROUP BY material;"

        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute(query, params)
        result = {r[0]: float(r[1]) for r in c.fetchall()}
        conn.close()
        return result

    def calculate_aging_days(self, entry_date_str: str, run_date_str: str) -> int:
        """Calculates aging gap between production entry date and audit run date."""
        try:
            # Handles YYYY.MM.DD or YYYY-MM-DD
            clean_entry = entry_date_str.replace(".", "-").strip()
            clean_run = run_date_str.replace(".", "-").strip()
            d_entry = datetime.datetime.strptime(clean_entry[:10], "%Y-%m-%d")
            d_run = datetime.datetime.strptime(clean_run[:10], "%Y-%m-%d")
            gap = (d_run - d_entry).days
            return max(0, gap)
        except Exception:
            return 0

    def analyze(
        self,
        start_date: Optional[str] = TARGET_START_DATE,
        end_date: Optional[str] = MILESTONE_ANALYSIS_DATE
    ) -> Dict[str, Any]:
        """
        Executes complete multi-dimensional analysis on accumulated backlog data.
        """
        raw_rows, distinct_dates = self.fetch_raw_history(start_date=start_date, end_date=end_date)
        transfer_map = self.fetch_transfer_totals(start_date=start_date, end_date=end_date)

        total_dates_count = max(1, len(distinct_dates))

        if not raw_rows:
            return {
                "period": {"start_date": start_date, "end_date": end_date, "total_days": 0, "dates": []},
                "summary": {"total_materials": 0, "total_deficit_qty": 0.0, "total_occurrences": 0},
                "materials": [],
                "quadrants": {},
                "top_bottlenecks": {}
            }

        # 1. Aggregate per material
        mat_groups: Dict[str, Dict[str, Any]] = {}
        for row in raw_rows:
            mat = row["material"]
            r_date = row["run_date"]
            qty = float(row["qty"])
            aging = self.calculate_aging_days(row["entry_date"] or r_date, r_date)

            if mat not in mat_groups:
                mat_groups[mat] = {
                    "material": mat,
                    "material_desc": row["material_desc"] or "",
                    "plant": row["plant"],
                    "slocs": set(),
                    "dates": set(),
                    "quantities": [],
                    "aging_list": [],
                    "error_codes": set(),
                    "raw_entries": []
                }
            mat_groups[mat]["slocs"].add(row["sloc"])
            mat_groups[mat]["dates"].add(r_date)
            mat_groups[mat]["quantities"].append(qty)
            mat_groups[mat]["aging_list"].append(aging)
            if row["error_code"]:
                mat_groups[mat]["error_codes"].add(row["error_code"])
            mat_groups[mat]["raw_entries"].append(row)

        # 2. Compute Volume Threshold (Percentile)
        all_total_qtys = [sum(m["quantities"]) for m in mat_groups.values()]
        if all_total_qtys:
            vol_threshold = float(np.percentile(all_total_qtys, HIGH_VOL_PERCENTILE))
            # Ensure threshold is reasonable
            vol_threshold = max(vol_threshold, 100.0)
        else:
            vol_threshold = 200.0

        analyzed_materials = []
        for mat, g in mat_groups.items():
            occ_days = len(g["dates"])
            freq_pct = round((occ_days / total_dates_count) * 100.0, 1)
            tot_qty = round(sum(g["quantities"]), 1)
            avg_qty = round(float(np.mean(g["quantities"])), 1)
            max_qty = round(max(g["quantities"]), 1)
            avg_aging = round(float(np.mean(g["aging_list"])), 1)
            max_aging = max(g["aging_list"]) if g["aging_list"] else 0

            is_high_freq = freq_pct >= HIGH_FREQ_PCT_THRESHOLD or occ_days >= 3
            is_high_vol = tot_qty >= vol_threshold

            # Quadrant Mapping
            if is_high_freq and is_high_vol:
                q_code = "Q1_CHRONIC_MASSIVE"
                quadrant_name = "만성 대량 결손 (Chronic Massive)"
                quadrant_desc = "생산 차질 및 라인 스탑 최우선 위험 품목"
                action_item = "안전재고 상향 + 입고/보충 주기 전면 재검토"
                priority = "🚨 P1_CRITICAL"
            elif is_high_freq and not is_high_vol:
                q_code = "Q2_CHRONIC_OPERATIONAL"
                quadrant_name = "만성 운영 지체 (Chronic Operational Delay)"
                quadrant_desc = "창고 재고는 있으나 라인 공급 지연 반복 품목"
                action_item = "창고-현장 자동보충(Min-Max) 및 피킹 동선 개선"
                priority = "⚠️ P2_OPERATIONAL"
            elif not is_high_freq and is_high_vol:
                q_code = "Q3_CRITICAL_SURGE"
                quadrant_name = "돌발 대형 결손 (Critical Surge)"
                quadrant_desc = "공급사 납기 지연 또는 대형 불량 의심 품목"
                action_item = "공급사 납기(ETA) 및 선적 품질 집중 추적"
                priority = "⚡ P3_SURGE"
            else:
                q_code = "Q4_MINOR_INFREQUENT"
                quadrant_name = "경미 일시 결손 (Minor Infrequent)"
                quadrant_desc = "일시적 단기 결손 품목"
                action_item = "기본 일일 루틴 자동 해소 유지"
                priority = "ℹ️ P4_ROUTINE"

            # Resolution Breakdown
            transferred_qty = transfer_map.get(mat, 0.0)
            res_rate = round(min(100.0, (transferred_qty / tot_qty) * 100.0), 1) if tot_qty > 0 else 0.0

            # Ontology Fusion
            ont = self.ontology_map.get(mat, {})
            tp = ont.get("turnover_profile", {})
            mover_class = tp.get("mover_class", ont.get("mover_class", "UNREGISTERED"))
            median_turnover = tp.get("median_days", ont.get("turnover_days_median", 0.0))
            std_turnover = tp.get("std_days", ont.get("turnover_days_std", 0.0))

            # Reconciliation Profile Fusion (Synergy with 04_JOB_SystemReconciliation)
            rec_diag = "NOT_EVALUATED"
            rec_action = "MONITOR"
            rec_exposure = 0.0
            rec_wms_qty = 0.0
            rec_sap_qty = 0.0
            try:
                rec_job_dir = self.base_dir.parent / "04_JOB_SystemReconciliation"
                if str(rec_job_dir) not in sys.path:
                    sys.path.insert(0, str(rec_job_dir))
                from db_manager import get_material_accuracy_profile
                prof = get_material_accuracy_profile(mat)
                if prof:
                    rec_diag = prof.get("diagnosis", "NOT_EVALUATED")
                    rec_action = prof.get("action", "MONITOR")
                    rec_exposure = prof.get("financial_exposure", 0.0)
                    rec_wms_qty = prof.get("wms_qty", 0.0)
                    rec_sap_qty = prof.get("sap_qty", 0.0)
            except Exception:
                pass

            is_line_stop_risk = bool(
                rec_diag == "PHANTOM_STOCK_RISK" or 
                (q_code in ["Q1_CHRONIC_MASSIVE", "Q3_CRITICAL_SURGE"] and mover_class in ["FAST_MOVER", "NORMAL"])
            )

            analyzed_materials.append({
                "material": mat,
                "material_desc": g["material_desc"],
                "slocs": sorted(list(g["slocs"])),
                "occurrence_days": occ_days,
                "frequency_pct": freq_pct,
                "total_deficit_qty": tot_qty,
                "avg_deficit_qty": avg_qty,
                "max_deficit_qty": max_qty,
                "transferred_qty": transferred_qty,
                "resolvability_rate_pct": res_rate,
                "avg_aging_days": avg_aging,
                "max_aging_days": max_aging,
                "quadrant_key": q_code,
                "quadrant_name": quadrant_name,
                "wms_sap_diagnosis": rec_diag,
                "wms_sap_action": rec_action,
                "wms_sap_exposure": rec_exposure,
                "wms_rec_qty": rec_wms_qty,
                "wms_sap_qty": rec_sap_qty,

                "priority": priority,
                "action_item": action_item,
                "mover_class": mover_class,
                "median_turnover_days": round(median_turnover, 1),
                "is_line_stop_risk": is_line_stop_risk
            })

        # Sort by total deficit qty descending
        analyzed_materials.sort(key=lambda x: (x["priority"], -x["total_deficit_qty"]))

        # Quadrant Summary
        quadrant_summary = {
            "Q1_CHRONIC_MASSIVE": [m for m in analyzed_materials if m["quadrant_key"] == "Q1_CHRONIC_MASSIVE"],
            "Q2_CHRONIC_OPERATIONAL": [m for m in analyzed_materials if m["quadrant_key"] == "Q2_CHRONIC_OPERATIONAL"],
            "Q3_CRITICAL_SURGE": [m for m in analyzed_materials if m["quadrant_key"] == "Q3_CRITICAL_SURGE"],
            "Q4_MINOR_INFREQUENT": [m for m in analyzed_materials if m["quadrant_key"] == "Q4_MINOR_INFREQUENT"]
        }

        # Top Bottlenecks
        top_by_freq = sorted(analyzed_materials, key=lambda x: -x["occurrence_days"])[:10]
        top_by_vol = sorted(analyzed_materials, key=lambda x: -x["total_deficit_qty"])[:10]
        top_by_aging = sorted(analyzed_materials, key=lambda x: -x["avg_aging_days"])[:10]
        line_stop_hazards = [m for m in analyzed_materials if m["is_line_stop_risk"]]

        return {
            "period": {
                "start_date": start_date or (distinct_dates[0] if distinct_dates else "N/A"),
                "end_date": end_date or (distinct_dates[-1] if distinct_dates else "N/A"),
                "total_audit_days": total_dates_count,
                "audit_dates": distinct_dates
            },
            "thresholds": {
                "high_freq_pct": HIGH_FREQ_PCT_THRESHOLD,
                "high_vol_qty": round(vol_threshold, 1)
            },
            "summary": {
                "total_materials": len(analyzed_materials),
                "total_cumulative_qty": round(sum(m["total_deficit_qty"] for m in analyzed_materials), 1),
                "total_transferred_qty": round(sum(m["transferred_qty"] for m in analyzed_materials), 1),
                "overall_resolvability_pct": round(
                    (sum(m["transferred_qty"] for m in analyzed_materials) / sum(m["total_deficit_qty"] for m in analyzed_materials) * 100.0)
                    if sum(m["total_deficit_qty"] for m in analyzed_materials) > 0 else 0.0, 1
                ),
                "line_stop_hazard_count": len(line_stop_hazards),
                "q1_count": len(quadrant_summary["Q1_CHRONIC_MASSIVE"]),
                "q2_count": len(quadrant_summary["Q2_CHRONIC_OPERATIONAL"]),
                "q3_count": len(quadrant_summary["Q3_CRITICAL_SURGE"]),
                "q4_count": len(quadrant_summary["Q4_MINOR_INFREQUENT"])
            },
            "materials": analyzed_materials,
            "quadrant_summary": quadrant_summary,
            "top_bottlenecks": {
                "by_frequency": top_by_freq,
                "by_volume": top_by_vol,
                "by_aging": top_by_aging,
                "line_stop_hazards": line_stop_hazards
            }
        }
