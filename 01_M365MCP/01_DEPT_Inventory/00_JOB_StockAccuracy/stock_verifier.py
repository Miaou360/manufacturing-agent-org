"""
Stock Verifier Engine (01_Inventory_Team / 00_StockAccuracy)
WMS 실시간 재고, 온톨로지 회전일수 및 다차원 이상 징후를 결합하여
창고 순환실사(Cycle Count) 타깃을 산출하고 전문 영문 HTML 리포트를 생성합니다.
"""

import os
import sys
import datetime
from pathlib import Path
import os
import sys as _sys
from dotenv import load_dotenv as _load_dotenv
_ENV_LOADED_PATH = None
for _ep in Path(__file__).resolve().parents:
    if (_ep / ".env").exists():
        _load_dotenv(_ep / ".env")
        _ENV_LOADED_PATH = _ep / ".env"
        break
_DUMMY_ENV_VALS = {"P001", "P002", "0000000", "9900000", "Enterprise SSO", "https://wms.example.com/WMS_WEB", "wms_user", "OEM", "AUTO_AGENT", "ACME", "ACME Manufacturing", "ACME Corp", "ACME America", "MAT-0000", "TEST-000"}
def _get_ops_env(key: str, test_default: str = "") -> str:
    val = os.getenv(key, "").strip()
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return val if val else test_default
    if not val or val in _DUMMY_ENV_VALS:
        raise RuntimeError(f"[ENV GUARD CRITICAL] Operational env var '{key}' is missing or set to dummy value in {__file__}!")
    return val
import re as _re_out_guard
_OUT_DUMMY_RE = _re_out_guard.compile(r"(\bACME\b|\bP001\b|\bP002\b|example\.com|\bMAT-0000|\bTEST-000|\bAUTO_AGENT\b|\b9900000\d*\b)", _re_out_guard.IGNORECASE)
def _assert_clean_ops_output(*texts: str, context: str = "email_output") -> None:
    if "pytest" in _sys.modules or os.getenv("PYTEST_CURRENT_TEST") or os.getenv("M365_TEST_MODE") == "1":
        return
    for _t in texts:
        if _t:
            _m = _OUT_DUMMY_RE.search(str(_t))
            if _m:
                raise RuntimeError(f"[OUTPUT GUARD CRITICAL] Dummy token '{_m.group(0)}' detected in {context} ({__file__})!")
def _verify_ops_env_log(job_name: str = "PIPELINE") -> str:
    _req = ("SAP_PLANT", "SAP_PLANT_SUB", "SAP_USERNAME", "SAP_SSO_WINDOW_TITLE", "WMS_BASE_URL", "COMPANY_NAME", "OEM_FLOOR_BIN_PREFIX", "SAP_MB1B_RECIPIENT")
    _st = []
    for _k in _req:
        if _k == "OEM_FLOOR_BIN_PREFIX":
            _fb = os.getenv("FLOOR_BUFFER_BIN_PREFIXES", "").strip()
            if _fb and _fb not in _DUMMY_ENV_VALS:
                _st.append("FLOOR_BUFFER_BIN_PREFIXES=configured")
                continue
        _get_ops_env(_k, "TEST")
        _st.append(f"{_k}=configured")
    _msg = f"[ENV GUARD] [{job_name}] .env={'found' if _ENV_LOADED_PATH else 'missing'} | " + ", ".join(_st)
    print(_msg, flush=True)
    return _msg
from typing import List, Dict, Any, Optional

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from config import MONITORED_PLANTS
from box_aging_tracker import BoxAgingTracker


class StockVerifier:
    def __init__(self):
        self.tracker = BoxAgingTracker()

    def run_full_stock_analysis(
        self,
        plant: str = _get_ops_env("SAP_PLANT", "P001"),
        mode: str = "Daily",
        dry_run: bool = False,
        run_date: Optional[str] = None,
        wms_file: Optional[Path] = None,
    ) -> Dict[str, Any]:
        """Load latest WMS Stock List, run multi-factor mis-scan & aging analysis, and generate audit Excel."""
        latest_file = Path(wms_file) if wms_file is not None else self.tracker.find_latest_wms_file()
        if not latest_file or not latest_file.exists():
            raise FileNotFoundError("Latest WMS Stock List file not found in wms_daily_stack.")

        df = self.tracker.load_stock_list(latest_file, dry_run=dry_run)
        analysis = self.tracker.detect_mis_scans(df, mode=mode, run_date=run_date)
        excel_path = self.tracker.export_audit_excel(analysis, mode=mode, dry_run=dry_run)

        return {
            "mode": mode,
            "plant": plant,
            "source_file": latest_file.name,
            "audit_excel_path": excel_path,
            "total_barcodes": analysis["total_barcodes"],
            "total_suspects": analysis["total_suspects"],
            "total_suspect_bins": analysis.get("total_suspect_bins", 0),
            "ghost_suspect_count": analysis.get("ghost_suspect_count", analysis.get("phantom_suspect_count", 0)),
            "phantom_suspect_count": analysis.get("phantom_suspect_count", 0),
            "same_bin_obstruction_count": analysis.get("same_bin_obstruction_count", 0),
            "supermarket_cart_count": analysis["supermarket_cart_count"],
            "plant1_small_part_count": analysis["plant1_small_part_count"],
            "fifo_inversion_count": analysis["fifo_inversion_count"],
            "turnover_anomaly_count": analysis["turnover_anomaly_count"],
            "high_rack_count": analysis.get("high_rack_count", 0),
            "fast_mover_residual_count": analysis.get("fast_mover_residual_count", 0),
            "forced_weekly_bins": analysis.get("forced_weekly_bins", []),
            "field_instructions": analysis.get("field_instructions", []),
            "top_bins": analysis["top_bins"],
            "extended_bins": analysis["extended_bins"],
            "random_bins": analysis.get("random_bins", []),
            "shadow_ev_ranking": analysis.get("shadow_ev_ranking", []),
            "scoring_version": analysis.get("scoring_version"),
            "config_hash": analysis.get("config_hash"),
            "suspects_df": analysis["suspects"]
        }

    def generate_html_report(self, analysis: Dict[str, Any], mode: str = "Daily") -> str:
        """Render a streamlined, clean English HTML report showing only the prioritized target bin table."""
        plant = analysis.get("plant", _get_ops_env("SAP_PLANT", "P001"))
        top_bins = analysis.get("top_bins", [])
        extended_bins = analysis.get("extended_bins", [])
        random_bins = analysis.get("random_bins", [])
        all_target_bins = top_bins + extended_bins + random_bins
        audit_file_name = Path(analysis.get("audit_excel_path", "")).name
        now_str = datetime.datetime.now().strftime("%B %d, %Y")
        is_weekly = mode.lower() == "weekly"

        # Color badges for Action Types
        def get_action_badge(action: str) -> str:
            if "GR Missing Check" in action:
                bg, border, text = "#ffe4e6", "#e11d48", "#881337" # Rose Alert (#199 Forced SAP GR Check)
            elif "Ghost Stock" in action or "Phantom Box" in action:
                bg, border, text = "#fff1f2", "#f43f5e", "#9f1239" # Crimson Red
            elif "Box Presence" in action:
                bg, border, text = "#fef3c7", "#f59e0b", "#92400e" # Amber / Golden Warning (Clearly distinct from Ghost Stock)
            elif "FIFO Bypass" in action or "Replenish" in action:
                bg, border, text = "#eef2ff", "#6366f1", "#4338ca" # Indigo Blue
            elif "Fast Mover" in action:
                bg, border, text = "#ecfeff", "#06b6d4", "#0e7490" # Cyan
            elif "High-Rack" in action:
                bg, border, text = "#f5f3ff", "#8b5cf6", "#5b21b6" # Purple
            elif "Bin FIFO Sort" in action:
                bg, border, text = "#eff6ff", "#3b82f6", "#1d4ed8" # Vibrant Blue
            elif "Rear Pallet Check" in action:
                bg, border, text = "#f1f5f9", "#64748b", "#334155" # Slate Gray
            elif "Packaging Unit" in action:
                bg, border, text = "#f0fdf4", "#22c55e", "#15803d" # Green
            elif "SMT Audit" in action:
                bg, border, text = "#fffbeb", "#f59e0b", "#92400e" # Orange
            elif "Cart PCS Check" in action:
                bg, border, text = "#fdf4ff", "#d946ef", "#86198f" # Fuchsia / Magenta PCS Check
            elif "Repack" in action:
                bg, border, text = "#faf5ff", "#c084fc", "#6b21a8" # Violet
            elif "Turnover" in action:
                bg, border, text = "#fff7ed", "#fb923c", "#c2410c" # Rust
            elif "FIFO" in action:
                bg, border, text = "#eff6ff", "#60a5fa", "#1e40af" # Light Blue
            elif "Random" in action:
                bg, border, text = "#ecfdf5", "#10b981", "#047857" # Emerald Green for Random Verification
            else:
                bg, border, text = "#f3f4f6", "#9ca3af", "#374151"
            return f'<span style="background-color: {bg}; border: 1px solid {border}; color: {text}; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 700; white-space: nowrap;">{action}</span>'

        # Render Target Bin Rows
        table_rows_html = []
        for rank, b in enumerate(all_target_bins, 1):
            badge = get_action_badge(b["Action_Required"])
            row_bg = "#ffffff" if rank % 2 == 1 else "#f8fafc"
            wh_dwell = b.get("Warehouse_Dwell_Days", 0)
            bin_dwell = b.get("Bin_Dwell_Days", 1)
            raw_pcs = b.get("Total_PCS", "")
            pcs_disp = f"{int(raw_pcs):,} pcs" if raw_pcs not in ("", None) else ""
            table_rows_html.append(f"""
            <tr style="background-color: {row_bg}; border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 9px 10px; font-weight: 700; color: #1e293b; text-align: center;">#{rank}</td>
                <td style="padding: 9px 10px; font-weight: 700; color: #0284c7; font-family: monospace; font-size: 13px;">{b['BIN']}</td>
                <td style="padding: 9px 10px; font-family: monospace; font-size: 12px; font-weight: 600; color: #1e293b;">{b['Parts_List']}</td>
                <td style="padding: 9px 10px; text-align: center; font-weight: 700; color: #0f172a; font-size: 13px;">{b['Label_QTY']}</td>
                <td style="padding: 9px 10px; text-align: right; font-weight: 700; color: #0f766e; font-size: 12px; white-space: nowrap;">{pcs_disp}</td>
                <td style="padding: 9px 10px; text-align: center; font-size: 12px; font-weight: 600; color: #334155;">{wh_dwell}d <span style="font-size: 10px; color: #64748b;">({bin_dwell}d in bin)</span></td>
                <td style="padding: 9px 10px; text-align: center;">{badge}</td>
                <td style="padding: 9px 10px; color: #475569; font-size: 12px; font-weight: 500;">{b['Reason']}</td>
            </tr>
            """)

        main_table_body = "".join(table_rows_html)

        # D112 (#209 / #210): R1-only English "Field Instructions" at top of Daily email (omitted when empty or Weekly)
        field_instructions = (
            [str(x).strip() for x in (analysis.get("field_instructions") or []) if str(x).strip()][:5]
            if not is_weekly
            else []
        )
        field_instructions_html = ""
        if field_instructions:
            import html as _html_esc
            li_items = "".join(
                f'<li style="margin: 6px 0; line-height: 1.5; font-size: 16px; font-weight: 700; word-break: break-word; overflow-wrap: anywhere;">{_html_esc.escape(line)}</li>'
                for line in field_instructions
            )
            field_instructions_html = f"""
                <!-- D112 Field Instructions (R1 Repeat-Short Bins) -->
                <div style="background: #fef3c7; border: 2px solid #f59e0b; border-left: 6px solid #d97706; padding: 14px 18px; margin: 14px 20px 0 20px; border-radius: 6px;">
                    <div style="font-size: 20px; font-weight: 800; color: #78350f; margin-bottom: 8px; line-height: 1.25;">
                        Field Instructions
                    </div>
                    <ul style="margin: 0; padding-left: 22px; font-size: 16px; color: #78350f; font-weight: 700; word-break: break-word; overflow-wrap: anywhere;">
                        {li_items}
                    </ul>
                </div>
            """

        # Schedule Announcement Banner (D111: 100% English + dynamic local TZ EST/EDT)
        _tz_abbr = datetime.datetime.now().astimezone().strftime("%Z") or "EST"
        if _tz_abbr not in ("EST", "EDT"):
            _tz_abbr = "EST" if datetime.datetime.now().astimezone().utcoffset() == datetime.timedelta(hours=-5) else "EDT"
        if not is_weekly:
            schedule_banner = f"""
                <!-- Schedule Announcement Banner -->
                <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-left: 4px solid #2563eb; padding: 11px 16px; margin: 14px 20px 0 20px; border-radius: 4px;">
                    <div style="font-size: 12px; font-weight: 700; color: #1e3a8a;">
                        📢 Schedule Notice: Daily calculation runs automatically at 06:50 AM {_tz_abbr} (dispatched before 07:20 AM {_tz_abbr})
                    </div>
                </div>
            """
        else:
            schedule_banner = f"""
                <!-- Schedule Announcement Banner -->
                <div style="background: #f8fafc; border: 1px solid #cbd5e1; border-left: 4px solid #2563eb; padding: 11px 16px; margin: 14px 20px 0 20px; border-radius: 4px;">
                    <div style="font-size: 12px; font-weight: 700; color: #1e3a8a;">
                        📢 Schedule Notice: Weekly calculation runs every Saturday at 06:50 AM {_tz_abbr} (dispatched before 07:20 AM {_tz_abbr})
                    </div>
                </div>
            """

        target_summary_desc = f"Prioritized Target Bins ({len(all_target_bins)} Bins"
        parts_desc = []
        forced_cnt = sum(1 for b in top_bins if b.get("selection_method") == "FORCED_WEEKLY_GR_CHECK")
        reg_top_cnt = len(top_bins) - forced_cnt
        if forced_cnt > 0:
            parts_desc.append(f"Forced F-Check {forced_cnt}")
        if reg_top_cnt > 0:
            parts_desc.append(f"Top {reg_top_cnt}")
        if extended_bins:
            parts_desc.append(f"Ext {len(extended_bins)}")
        if random_bins:
            parts_desc.append(f"Random {len(random_bins)}")
        if parts_desc:
            target_summary_desc += f": {' + '.join(parts_desc)})"
        else:
            target_summary_desc += ")"

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
        </head>
        <body style="margin: 0; padding: 0; background-color: #f1f5f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;">
            <div style="max-width: 960px; margin: 16px auto; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.08); border: 1px solid #cbd5e1;">
                
                <!-- Compact Header -->
                <div style="background: linear-gradient(135deg, #0f172a, #1e3a8a, #2563eb); color: #ffffff; padding: 16px 22px; display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <h1 style="margin: 0; font-size: 18px; font-weight: 800; letter-spacing: -0.2px;">[{mode}] Cycle Count List - Plant {plant}</h1>
                        <p style="margin: 3px 0 0 0; font-size: 12px; opacity: 0.85;">{now_str} | {target_summary_desc}</p>
                    </div>
                    <div style="text-align: right;">
                        <span style="background: rgba(255, 255, 255, 0.2); padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 700;">Sorted by Risk Score</span>
                    </div>
                </div>

                {field_instructions_html}

                {schedule_banner}

                <!-- Main Target Table Directly -->
                <div style="padding: 14px 20px 16px 20px;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px; border: 1px solid #cbd5e1; border-radius: 6px; overflow: hidden;">
                        <thead>
                            <tr style="background: #f8fafc; color: #334155; font-size: 11px; text-transform: uppercase; font-weight: 700; border-bottom: 2px solid #cbd5e1;">
                                <th style="padding: 9px 10px; width: 45px; text-align: center;">Rank</th>
                                <th style="padding: 9px 10px; width: 95px; text-align: left;">Bin #</th>
                                <th style="padding: 9px 10px; text-align: left;">Part #</th>
                                <th style="padding: 9px 10px; width: 80px; text-align: center;">Label QTY</th>
                                <th style="padding: 9px 10px; width: 95px; text-align: right;">Total PCS</th>
                                <th style="padding: 9px 10px; width: 120px; text-align: center;">Stock Age (Bin Stay)</th>
                                <th style="padding: 9px 10px; width: 145px; text-align: center;">Action Required</th>
                                <th style="padding: 9px 10px; width: 220px; text-align: left;">Reason</th>
                            </tr>
                        </thead>
                        <tbody>
                            {main_table_body}
                        </tbody>
                    </table>

                    <!-- Compact Attachment Notice -->
                    <div style="margin-top: 14px; background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 6px; padding: 10px 14px; display: flex; justify-content: space-between; align-items: center;">
                        <div style="font-size: 12px; font-weight: 700; color: #166534;">
                            📎 Attached Master Excel: <span style="font-family: monospace; color: #15803d;">{audit_file_name}</span>
                        </div>
                        <div style="font-size: 11px; color: #16a34a; font-weight: 500;">
                            Full multi-sheet verification dataset included
                        </div>
                    </div>
                </div>

                <!-- Minimal Footer -->
                <div style="background-color: #f8fafc; border-top: 1px solid #e2e8f0; padding: 10px 20px; font-size: 11px; color: #94a3b8; text-align: right;">
                    Materials Planning & Inventory Automation Team | {_get_ops_env('COMPANY_NAME', 'ACME Manufacturing')} Plant {_get_ops_env('SAP_PLANT', 'P001')}
                </div>

            </div>
        </body>
        </html>
        """
        return html


if __name__ == "__main__":
    verifier = StockVerifier()
    res = verifier.run_full_stock_analysis(mode="Weekly")
    html = verifier.generate_html_report(res, mode="Weekly")
    print("Weekly HTML Report Generated. Length:", len(html))
    print("Weekly Audit File:", res["audit_excel_path"])
    print(f"Weekly Target Bins Count: {len(res['top_bins'])}")

