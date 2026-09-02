import csv
import json
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.cli import main

FIXTURE_PATH = Path("tests/fixtures/synthetic_indeci_page.html")


def test_cli_inventory_writes_json_csv_and_manifest(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "inventory",
            "--source",
            str(FIXTURE_PATH),
            "--source-name",
            "Synthetic COEN fixture",
            "--source-url",
            "https://coen.example.test/reportes",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert "Wrote 2 records" in capsys.readouterr().out

    records = json.loads((tmp_path / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    csv_rows = list(
        csv.DictReader((tmp_path / "inventory.csv").open(encoding="utf-8"))
    )

    assert len(records) == 2
    assert len(csv_rows) == 2
    assert records[0]["title"] == "Candidate report 001"
    assert records[0]["requires_human_review"] is True
    assert csv_rows[0]["requires_human_review"] == "true"
    assert manifest["mode"] == "offline"
    assert manifest["record_count"] == 2
    assert manifest["warnings"] == ["skipped record 3: url must use http or https"]


@pytest.mark.parametrize("command", ["historical", "live-smoke", "upload-drive"])
def test_cli_blocks_non_offline_modes(command, capsys) -> None:
    exit_code = main([command])

    assert exit_code == 2
    assert "requires separate approval" in capsys.readouterr().err


def test_cli_rejects_pdf_sources(tmp_path, capsys) -> None:
    pdf_path = tmp_path / "synthetic.pdf"
    pdf_path.write_text("not a real PDF", encoding="utf-8")

    exit_code = main(
        [
            "inventory",
            "--source",
            str(pdf_path),
            "--source-name",
            "Synthetic COEN fixture",
            "--source-url",
            "https://coen.example.test/reportes",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 2
    assert "PDF sources are not enabled" in capsys.readouterr().err
