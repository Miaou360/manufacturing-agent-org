import os
import sys
from unittest.mock import patch
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import env_guard


def test_env_guard_rejects_dummy_and_missing_in_production_mode():
    """Verify #156 & #176: Missing or dummy env vars (including COMPANY_NAME='ACME Manufacturing'/'ACME') raise RuntimeError outside test mode."""
    with patch.object(env_guard, "is_test_mode", return_value=False), \
         patch.object(env_guard, "load_workspace_dotenv", return_value=None):
        # 1. Dummy value P001 must fail
        with patch.dict(os.environ, {"SAP_PLANT": "P001"}, clear=False):
            with pytest.raises(RuntimeError, match="SAP_PLANT"):
                env_guard.get_ops_env("SAP_PLANT", "P001")

        # 2. Empty value must fail
        with patch.dict(os.environ, {"SAP_USERNAME": ""}, clear=False):
            with pytest.raises(RuntimeError, match="SAP_USERNAME"):
                env_guard.get_ops_env("SAP_USERNAME", "0000000")

        # 3. Dummy SSO title must fail
        with patch.dict(os.environ, {"SAP_SSO_WINDOW_TITLE": "Enterprise SSO"}, clear=False):
            with pytest.raises(RuntimeError, match="SAP_SSO_WINDOW_TITLE"):
                env_guard.get_ops_env("SAP_SSO_WINDOW_TITLE", "Enterprise SSO")

        # 4. Dummy WMS URL must fail
        with patch.dict(os.environ, {"WMS_BASE_URL": "https://wms.example.com/WMS_WEB"}, clear=False):
            with pytest.raises(RuntimeError, match="WMS_BASE_URL"):
                env_guard.get_ops_env("WMS_BASE_URL", "https://wms.example.com/WMS_WEB")

        # 5. Dummy COMPANY_NAME ('ACME Manufacturing' / 'ACME' / 'ACME Corp') must fail (#176)
        for dummy_co in ("ACME Manufacturing", "ACME", "ACME Corp"):
            with patch.dict(os.environ, {"COMPANY_NAME": dummy_co}, clear=False):
                with pytest.raises(RuntimeError, match="COMPANY_NAME"):
                    env_guard.get_ops_env("COMPANY_NAME", "ACME Manufacturing")


def test_env_guard_allows_configured_values_in_production_mode():
    """Verify #156 & #176: Configured real env values pass verify_ops_env without printing secrets."""
    fake_prod_env = {
        "SAP_PLANT": "REAL_PLANT_1",
        "SAP_PLANT_SUB": "REAL_PLANT_2",
        "SAP_USERNAME": "9999999",
        "SAP_SSO_WINDOW_TITLE": "Custom Corp SSO",
        "WMS_BASE_URL": "https://internal-wms.example.org/WEB",
        "COMPANY_NAME": "Real Manufacturing Corp",
        "OEM_FLOOR_BIN_PREFIX": "CUST",
        "SAP_MB1B_RECIPIENT": "RealUser",
    }
    with patch.object(env_guard, "is_test_mode", return_value=False), \
         patch.object(env_guard, "load_workspace_dotenv", return_value=None), \
         patch.dict(os.environ, fake_prod_env, clear=False):
        status = env_guard.verify_ops_env("UNIT_TEST_PROD")
        assert all(v == "configured" for v in status.values())
        assert "COMPANY_NAME" in status


def test_assert_no_dummy_in_output_blocks_leaked_pseudonyms():
    """Verify #176 Item 5: Reverse-direction output guard blocks ACME/P001/example.com in operational outputs."""
    with patch.object(env_guard, "is_test_mode", return_value=False):
        with pytest.raises(RuntimeError, match="OUTPUT GUARD CRITICAL"):
            env_guard.assert_no_dummy_in_output(
                "[ACME Manufacturing] SL01 R-Bin Movement Lag Daily Report", "05_subject"
            )
        with pytest.raises(RuntimeError, match="OUTPUT GUARD CRITICAL"):
            env_guard.assert_no_dummy_in_output(
                "Plant P001 Headlamp | Scan Date: 2026-10-29", "05_body"
            )
        # Clean operational text passes
        env_guard.assert_no_dummy_in_output(
            "[Real Manufacturing Corp] SL01 R-Bin Movement Lag Daily Report - 2026-10-29 (0 Pallets Stalled)",
            "05_subject",
        )
