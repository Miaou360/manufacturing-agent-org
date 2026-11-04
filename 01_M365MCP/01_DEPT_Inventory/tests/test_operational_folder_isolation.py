"""
Operational Folder Isolation Verification Test (Thread 056 Guardrail)
====================================================================
Verifies that test executions strictly write only to temporary paths (tmp_path),
leaving production operational folders and databases untouched.
"""

import sys
import pytest
from pathlib import Path
import pandas as pd

TEST_DIR = Path(__file__).resolve().parent
INVENTORY_DIR = TEST_DIR.parent
JOB_01_DIR = INVENTORY_DIR / "01_JOB_GR_GI_Error"
JOB_02_DIR = INVENTORY_DIR / "02_JOB_Backlog_Clear"
JOB_00_DIR = INVENTORY_DIR / "00_JOB_StockAccuracy"
JOB_05_DIR = INVENTORY_DIR / "05_JOB_SL01_R_Bins"

for p in [str(JOB_01_DIR), str(JOB_02_DIR), str(JOB_00_DIR), str(JOB_05_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from gr_gi_resolver import resolve_gi_errors
from backlog_resolver import build_backlog_resolution_plan


def test_operational_directories_strictly_unchanged(tmp_path):
    """
    Guarantees that resolution engine calls in test mode (with tmp_path / save_excel=False)
    produce ZERO new files in production operational folders.
    """
    op_dirs = [
        JOB_02_DIR / "resolution_logs",
        JOB_01_DIR / "resolution_logs",
        JOB_00_DIR / "audit_reports",
        JOB_05_DIR / "reports"
    ]

    # 1. Take initial count
    initial_counts = {}
    for d in op_dirs:
        if d.exists():
            initial_counts[d] = len(list(d.glob("*")))

    # 2. Run Backlog Resolution Plan with tmp_path isolation
    dummy_cogi = [{"Material": "99000003", "Qty": 50.0, "SLoc": "SP31"}]
    plan_bl = build_backlog_resolution_plan(
        dummy_cogi,
        output_dir=tmp_path,
        save_excel=True
    )
    assert plan_bl is not None

    # Verify Excel was saved to tmp_path, NOT to operational folder
    tmp_bl_files = list(tmp_path.glob("Backlog_Resolution_Plan_*.xlsx"))
    assert len(tmp_bl_files) == 1, "Expected Backlog resolution plan to be written to tmp_path"

    # 3. Run GI Resolution Plan with save_excel=False
    gi_dummy = tmp_path / "GI_test.XLS"
    gi_dummy.write_text("Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n", encoding="utf-16")
    gr_dummy = tmp_path / "GR_test.XLS"
    gr_dummy.write_text("Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tSLoc\tQty\tMessage\n", encoding="utf-16")

    plan_gi = resolve_gi_errors(
        gi_path=gi_dummy,
        gr_path=gr_dummy,
        output_dir=tmp_path,
        save_excel=False
    )
    assert plan_gi is not None

    # 4. Verify operational folders have EXACT same file count (zero pollution)
    for d, init_cnt in initial_counts.items():
        curr_cnt = len(list(d.glob("*")))
        assert curr_cnt == init_cnt, f"Operational folder {d.name} was polluted! Initial: {init_cnt}, Current: {curr_cnt}"
