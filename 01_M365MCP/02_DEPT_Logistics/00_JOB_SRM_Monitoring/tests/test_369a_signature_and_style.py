"""#369 A tests: configurable supplier mail signature ('Logistics Team') and report-styled Truck Plan attachment."""
import datetime as dt
import sys
from pathlib import Path

from openpyxl import load_workbook

JOB_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOB_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import srm_mailer as mailer  # noqa: E402
from srm_parsers import SLOT_LABELS  # noqa: E402
from srm_plan import plan_parts  # noqa: E402
from srm_supplier_files import write_supplier_files  # noqa: E402
from test_srm_monitoring import _res_row  # noqa: E402
from test_srm_pipeline import D, FakeSender, _env, _inbox  # noqa: E402
import run_srm_pipeline as pipeline  # noqa: E402


def test_mail_signature_lives_in_config_and_renders_max(tmp_path, monkeypatch):
    cfg_on_disk = mailer.load_config()
    assert cfg_on_disk["signature"] == "Logistics Team"
    assert cfg_on_disk["mode"] in ("test", "live")

    # Signature is read from config, never hardcoded inside srm_mailer.py
    mailer_src = Path(mailer.__file__).read_text(encoding="utf-8")
    assert '"Logistics Team"' not in mailer_src and "'HardcodedSig'" not in mailer_src

    zm = _env(monkeypatch, tmp_path)
    _inbox(tmp_path / "inbox")
    snd = FakeSender()
    cfg = {
        "mode": "test",
        "user_env": "SRM_T_USER",
        "send_when_no_change": True,
        "signature": cfg_on_disk["signature"],
        "suppliers": {"Vendor A": {"to_env": "SRM_T_TO_A", "cc_env": "SRM_T_CC_A"}},
    }
    st = pipeline.run_pipeline(
        "AM",
        D,
        skip_download=True,
        sender=snd,
        zm212=zm,
        inbox_root=tmp_path / "inbox",
        out_dir=tmp_path / "out",
        mail_cfg=cfg,
        sleep=lambda s: None,
    )
    assert st["status"] == "OK"
    sup_body = snd.sent[0]["body"]
    assert "<p>Thank you,<br>Logistics Team</p>" in sup_body
    assert "Logistics</p>" not in sup_body


def test_truck_plan_restyled_preserves_layout_sheets_rows_and_semantics(tmp_path):
    tr = {"Vendor A": [SLOT_LABELS.index(x) for x in ("10-12", "12-14", "16-18", "22-24")]}
    req = [0.0] * 36
    req[1:5] = [100.0, 91.0, 99.0, 34.0]
    slots = [0.0] * 36
    slots[4], slots[7] = 63.0, 315.0
    om = {
        "P-0101": {"part": "0P-0101", "vendor": "Vendor A X", "name": "PART ALPHA", "add": 0.0, "slots": slots},
        "P-0102": {"part": "0P-0102", "vendor": "Vendor A X", "name": "PART BETA", "add": 0.0, "slots": [0.0] * 36},
    }
    req2 = [0.0] * 36
    req2[3] = 30.0
    rm = {
        "P-0101": {"part": "0P-0101", "vendor": "Vendor A X", "name": "PART ALPHA", "add": 0.0, "slots": req},
        "P-0102": {"part": "0P-0102", "vendor": "Vendor A X", "name": "PART BETA", "add": 0.0, "slots": req2},
    }
    rows = [_res_row("P-0101", "Vendor A X", 260.0), _res_row("P-0102", "Vendor A X", 0.0)]
    plan_parts(D, rows, om, rm, None, None, False, tr, {"P-0101": 21.0, "P-0102": 10.0}, {"P-0101": "C", "P-0102": "C"})

    paths = write_supplier_files(tmp_path, "AM", rows, {"P-0101": 21.0, "P-0102": 10.0}, dt.datetime(2026, 11, 12, 6, 55))
    wb = load_workbook(paths[0])

    # Sheets, freeze panes, header rows (5-6) and data rows (7-8) unchanged
    assert wb.sheetnames == ["Truck plan", "2-hour detail"]
    ws1 = wb["Truck plan"]
    ws2 = wb["2-hour detail"]
    assert ws1.freeze_panes == "E7"
    assert ws2.freeze_panes == "F7"
    assert [ws1.cell(row=5, column=c).value for c in range(1, 5)] == ["Part #", "Part name", "Box qty", "Stock 06:00 today"]
    assert ws1.cell(row=7, column=1).value == "0P-0101"
    assert ws1.cell(row=8, column=1).value == "0P-0102"
    assert ws1.cell(row=9, column=1).value == "Total EA"
    assert ws1.cell(row=10, column=1).value == "Total carts (boxes)"

    # Unified Segoe UI report style + dark slate header band + full cell grid borders
    assert ws1["A1"].fill.fgColor.rgb == "001E293B"
    assert ws1["A5"].fill.fgColor.rgb == "001E293B"
    assert ws1["E6"].fill.fgColor.rgb == "001F4E78"
    assert ws1["A5"].font.name == "Segoe UI"
    assert ws1["A7"].font.name == "Segoe UI"
    assert ws1["E7"].border.left.style == "medium" and ws1["E7"].border.right.style == "thin"
    assert ws1["F7"].border.left.style == "thin" and ws1["F7"].border.right.style == "thin"
    assert ws2["A5"].font.name == "Segoe UI" and ws2["F7"].border.left.style == "thin"

    # Preserved highlight semantics (yellow changed, pink additional)
    fills = {c.fill.fgColor.rgb for row in ws1.iter_rows(min_row=7, max_row=8) for c in row}
    assert "00FFE699" in fills
    assert "00F4B6C2" in fills
