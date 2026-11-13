import os
import sys
from datetime import datetime
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sap_launcher
import hourly_gi_pipeline


def test_a_vbs_task_xml_allows_battery_and_interactive_token():
    """Verify #154 Improvement A: XML scheduled task allows battery execution and uses InteractiveToken."""
    xml_str = sap_launcher._build_vbs_task_xml(
        r"C:\Windows\System32\cscript.exe",
        '//Nologo "C:\\temp\\test.vbs"',
    )
    root = ET.fromstring(xml_str)
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    disallow_bat = root.find(".//t:Settings/t:DisallowStartIfOnBatteries", ns)
    stop_bat = root.find(".//t:Settings/t:StopIfGoingOnBatteries", ns)
    logon_type = root.find(".//t:Principals/t:Principal/t:LogonType", ns)

    assert disallow_bat is not None and disallow_bat.text == "false"
    assert stop_bat is not None and stop_bat.text == "false"
    assert logon_type is not None and logon_type.text == "InteractiveToken"


def test_b_get_next_schedule_target_startup_grace_window():
    """Verify #154 Improvement B: Startup within <=60s after a scheduled slot runs that slot immediately."""
    # Day slot: 11:30:00, daemon started at 11:30:03.694
    started_at_day = datetime(2026, 10, 28, 11, 30, 3, 694000)
    target_grace, label_grace = hourly_gi_pipeline.get_next_schedule_target(
        started_at_day, grace_seconds=60.0
    )
    assert target_grace == datetime(2026, 10, 28, 11, 30, 0)
    assert "30-min cycle" in label_grace

    # Subsequent cycles (grace_seconds=0.0) should target the next 12:00:00 slot
    target_no_grace, _ = hourly_gi_pipeline.get_next_schedule_target(
        started_at_day, grace_seconds=0.0
    )
    assert target_no_grace == datetime(2026, 10, 28, 12, 0, 0)

    # Night slot: 21:00:00, daemon started at 21:00:45
    started_at_night = datetime(2026, 10, 28, 21, 0, 45)
    target_night_grace, label_night = hourly_gi_pipeline.get_next_schedule_target(
        started_at_night, grace_seconds=60.0
    )
    assert target_night_grace == datetime(2026, 10, 28, 21, 0, 0)
    assert "1-hour cycle" in label_night

    # Outside 60s grace (e.g., 11:31:05 -> next slot is 12:00:00 even with grace_seconds=60.0)
    started_late = datetime(2026, 10, 28, 11, 31, 5)
    target_late, _ = hourly_gi_pipeline.get_next_schedule_target(
        started_late, grace_seconds=60.0
    )
    assert target_late == datetime(2026, 10, 28, 12, 0, 0)


def test_c_ensure_sap_open_never_taskkills_active_session_on_healthcheck_timeout():
    """Verify #154 Improvement C: When SAP_FRONTEND_SESSION is open and healthcheck times out, do not taskkill."""
    failed_probe = {"alive": False, "title": "", "tcode": "", "error": "COM probe timed out"}
    with patch.object(sap_launcher.win32gui, "FindWindow", return_value=12345), \
         patch.object(sap_launcher.win32gui, "IsWindowVisible", return_value=True), \
         patch.object(sap_launcher, "probe_sap_health", side_effect=[failed_probe, failed_probe]) as mock_probe, \
         patch.object(sap_launcher, "dismiss_sap_error_popups", return_value=0) as mock_dismiss, \
         patch.object(sap_launcher.time, "sleep"), \
         patch.object(sap_launcher.subprocess, "run") as mock_subproc_run:
        try:
            sap_launcher.ensure_sap_open()
            assert False, "Expected RuntimeError when SAP healthcheck fails twice with open window"
        except RuntimeError as e:
            assert "Skipping forced termination" in str(e)

        assert mock_probe.call_count == 2
        assert mock_dismiss.call_count == 2
        for call in mock_subproc_run.call_args_list:
            args = call[0][0] if call[0] else []
            assert "taskkill" not in [str(a).lower() for a in args]
