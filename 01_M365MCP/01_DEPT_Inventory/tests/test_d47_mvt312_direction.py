"""
Test Suite for User Decision D47 (Movement 312 Reverse Flow Standard)
=====================================================================
Validates:
(a) gi_flow('311', from_st, to_st) -> (from_st, to_st) (src=from_st, dst=to_st)
(b) gi_flow('312', from_st, to_st) -> (to_st, from_st) (src=to_st, dst=from_st)
(c) gi_flow for 901, 902, and unknown movements -> (None, None)
(d) extract_active_gi_error_map correctly sets SrcSLocs and DstSLocs for 311 and 312
(e) gr_gi_resolver assigns deficit_loc as TOSt for movement 312
"""

import sys
from pathlib import Path
import pandas as pd
import pytest

HQ_DIR = Path(__file__).resolve().parent.parent.parent / "00_HQ_TaskManager"
if str(HQ_DIR) not in sys.path:
    sys.path.insert(0, str(HQ_DIR))

JOB01_DIR = Path(__file__).resolve().parent.parent / "01_JOB_GR_GI_Error"
if str(JOB01_DIR) not in sys.path:
    sys.path.insert(0, str(JOB01_DIR))

from ztcode_stock_guard import gi_flow, extract_active_gi_error_map
import gr_gi_resolver


def test_gi_flow_311_standard_direction():
    """Mvt 311: normal direction where FromSt is source (deficit) and TOSt is destination."""
    src, dst = gi_flow("311", "SL01", "SP31")
    assert src == "SL01"
    assert dst == "SP31"


def test_gi_flow_312_reversal_direction():
    """Mvt 312: reversal direction where TOSt is source (deficit) and FromSt is destination."""
    src, dst = gi_flow("312", "SL00", "SP31")
    assert src == "SP31"
    assert dst == "SL00"


def test_gi_flow_non_transfer_movements():
    """Movements 901, 902, 261, etc. do not represent 2-step warehouse transfers -> (None, None)."""
    assert gi_flow("901", "SL01", "SP31") == (None, None)
    assert gi_flow("902", "SL01", "SP31") == (None, None)
    assert gi_flow("261", "SL01", "SP31") == (None, None)
    assert gi_flow("", "SL01", "SP31") == (None, None)


def test_extract_active_gi_error_map_populates_src_dst_slocs(tmp_path):
    """ZTCODE_GI_ERR GI parser correctly registers SrcSLocs and DstSLocs per D47."""
    gi_file = tmp_path / "GI_Error_D47_test.XLS"
    content = (
        "Date\tVendor\tMaterial Number\tMaterial Desc\tMvt\tFromSt\tTOSt\tQty\tMessage\n"
        "2026.10.26\tV1\tMAT_311\tPart 311\t311\tSL01\tSP31\t10.0\tDeficit\n"
        "2026.10.26\tV2\tMAT_312\tPart 312\t312\tSL00\tSP31\t20.0\tDeficit\n"
    )
    gi_file.write_text(content, encoding="utf-16")

    gi_map = extract_active_gi_error_map(gi_file)

    assert "MAT_311" in gi_map
    assert gi_map["MAT_311"]["SrcSLocs"] == {"SL01"}
    assert gi_map["MAT_311"]["DstSLocs"] == {"SP31"}

    assert "MAT_312" in gi_map
    # Under D47: 312 has deficit at TOSt (SP31) and destination at FromSt (SL00)
    assert gi_map["MAT_312"]["SrcSLocs"] == {"SP31"}
    assert gi_map["MAT_312"]["DstSLocs"] == {"SL00"}


def test_gr_gi_resolver_mvt312_deficit_loc():
    """Verify gr_gi_resolver uses gi_flow so 312 deficit_loc is TOSt."""
    r_311 = {"Material": "M1", "Mvt": "311", "FromSt": "SL01", "TOSt": "SP31"}
    s_311, d_311 = gr_gi_resolver.gi_flow(r_311["Mvt"], r_311["FromSt"], r_311["TOSt"])
    assert s_311 == "SL01"
    assert d_311 == "SP31"

    r_312 = {"Material": "M2", "Mvt": "312", "FromSt": "SL00", "TOSt": "SP31"}
    s_312, d_312 = gr_gi_resolver.gi_flow(r_312["Mvt"], r_312["FromSt"], r_312["TOSt"])
    assert s_312 == "SP31"
    assert d_312 == "SL00"
