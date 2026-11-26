"""Tests for the offline `python -m demo` package and `tools/privacy_guard.py`."""

from __future__ import annotations

from pathlib import Path

from demo.__main__ import main as demo_main
from demo.__main__ import run_demo
from tools.privacy_guard import scan_repository


def test_offline_single_job_demo_exercises_all_four_outcomes(tmp_path: Path) -> None:
    res = run_demo(out_dir=tmp_path)
    assert res["cleared_errors"] == 1
    assert res["posted_transfers"] == 1
    assert (tmp_path / "demo_action_log.csv").is_file()
    assert (tmp_path / "demo_summary.md").is_file()

    by_err = {r["error_id"]: r for r in res["clear_results"]}
    assert by_err["ERR-DEMO-01"]["outcome"] == "DONE"
    assert by_err["ERR-DEMO-02"]["outcome"] == "HANDED_TO_PERSON"
    assert "not_receipt_linked" in by_err["ERR-DEMO-02"]["failed_guards"]
    assert by_err["ERR-DEMO-03"]["outcome"] == "HANDED_TO_PERSON"
    assert "protected_movement" in by_err["ERR-DEMO-03"]["failed_guards"]

    by_mat = {r["material_id"]: r for r in res["transfer_results"]}
    assert by_mat["MAT-DEMO-001"]["outcome"] == "DONE"
    assert by_mat["MAT-DEMO-002"]["outcome"] == "HANDED_TO_PERSON"
    assert "nothing_in_flight" in by_mat["MAT-DEMO-002"]["failed_guards"]
    assert by_mat["MAT-DEMO-003"]["outcome"] == "HANDED_TO_PERSON"
    assert "accuracy_not_down" in by_mat["MAT-DEMO-003"]["failed_guards"]


def test_demo_cli_exits_zero(tmp_path: Path) -> None:
    assert demo_main(["--out-dir", str(tmp_path)]) == 0


def test_privacy_guard_passes_repository() -> None:
    assert scan_repository() == []
