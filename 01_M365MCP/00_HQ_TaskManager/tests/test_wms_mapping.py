"""
Unit tests for wms_mapping.py
"""
import pytest
import sys
from pathlib import Path

# Add 00_HQ_TaskManager to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from wms_mapping import (
    map_wms_gi_loc_to_sap,
    is_valid_target_sloc,
    is_accuracy_balance_sloc,
    calc_material_sloc_accuracy,
    WMS_TO_SAP_SLOC_MAP,
    ALLOWED_BALANCE_SLOCS
)

def test_wms_mapping_standard_names():
    assert map_wms_gi_loc_to_sap("Receipt Goods SL") == "SL00"
    assert map_wms_gi_loc_to_sap("Main parts") == "SL01"
    assert map_wms_gi_loc_to_sap("CKD") == "SL02"
    assert map_wms_gi_loc_to_sap("Raw material") == "SL03"
    assert map_wms_gi_loc_to_sap("Pass through") == "SL04"
    assert map_wms_gi_loc_to_sap("DMA") == "SL49"

def test_wms_mapping_casing_and_whitespace():
    assert map_wms_gi_loc_to_sap("  main parts  ") == "SL01"
    assert map_wms_gi_loc_to_sap("RECEIPT GOODS SL") == "SL00"
    assert map_wms_gi_loc_to_sap("ckd") == "SL02"
    assert map_wms_gi_loc_to_sap("dma") == "SL49"

def test_wms_mapping_sloc_passthrough():
    assert map_wms_gi_loc_to_sap("SL01") == "SL01"
    assert map_wms_gi_loc_to_sap("SL00") == "SL00"
    assert map_wms_gi_loc_to_sap("SP31") == "SP31"

def test_wms_mapping_unknown_or_invalid():
    assert map_wms_gi_loc_to_sap("SL06") is None  # SL06 is not whitelisted
    assert map_wms_gi_loc_to_sap("Random Location") is None
    assert map_wms_gi_loc_to_sap(None) is None
    assert map_wms_gi_loc_to_sap("") is None

def test_whitelist_validation():
    assert is_valid_target_sloc("SL01") is True
    assert is_valid_target_sloc("SP31") is True
    assert is_valid_target_sloc("SL06") is False  # Rejected
    assert is_valid_target_sloc("SL99") is False

def test_accuracy_calculation_production_cases():
    # Case: Exact match (0/0 excluded from ZTCODE_STOCK_REC denominator by default)
    assert calc_material_sloc_accuracy(100.0, 100.0) == 100.0
    assert calc_material_sloc_accuracy(0.0, 0.0) is None
    assert calc_material_sloc_accuracy(0.0, 0.0, exclude_zero_pair=False) == 100.0

    # Production Case 1: WMS 36.0, SAP 66.0 -> 54.545%
    assert calc_material_sloc_accuracy(36.0, 66.0) == 54.545

    # Production Case 2: WMS 441.0, SAP 770.0 -> 57.273%
    assert calc_material_sloc_accuracy(441.0, 770.0) == 57.273

    # Production Case 3: WMS 1100.0, SAP 561.0 -> 3.922%
    assert calc_material_sloc_accuracy(1100.0, 561.0) == 3.922

    # Production Case 4: WMS 12000.0, SAP 574.0 -> Clamped to 0.0%
    assert calc_material_sloc_accuracy(12000.0, 574.0) == 0.0

    # Production Case 5: WMS > 0, SAP = 0 -> 0.0%
    assert calc_material_sloc_accuracy(54.0, 0.0) == 0.0

    # Production Case 6: WMS = 0, SAP > 0 -> 0.0%
    assert calc_material_sloc_accuracy(0.0, 2000.0) == 0.0
