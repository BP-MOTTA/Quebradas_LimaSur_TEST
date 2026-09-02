from pathlib import Path

import pytest

from quebradas_limaeste.inventory.models import InventoryValidationError
from quebradas_limaeste.inventory.parser import parse_inventory_html

FIXTURE_PATH = Path("tests/fixtures/synthetic_indeci_page.html")


def test_parse_html_extracts_synthetic_records_without_following_links() -> None:
    result = parse_inventory_html(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
    )

    assert len(result.records) == 2
    assert [record.title for record in result.records] == [
        "Candidate report 001",
        "Candidate report without date",
    ]
    assert result.records[0].published_date.isoformat() == "2023-03-16"
    assert result.records[1].published_date is None
    assert result.records[1].warnings == ("published_date missing or unparsed",)
    assert result.records[0].requires_human_review is True
    assert result.records[0].document_url == (
        "https://coen.example.test/avisos/reporte-001"
    )
    assert all(
        "ignore previous instructions" not in record.evidence_text
        for record in result.records
    )
    assert result.warnings == ("skipped record 3: url must use http or https",)


def test_parse_inventory_html_rejects_oversized_input() -> None:
    with pytest.raises(InventoryValidationError):
        parse_inventory_html(
            "x" * 2_000_001,
            source_name="Synthetic COEN fixture",
            source_url="https://coen.example.test/reportes",
        )


def test_parse_inventory_html_validates_source_url_before_parsing() -> None:
    with pytest.raises(InventoryValidationError):
        parse_inventory_html(
            "<article data-inventory-record></article>",
            source_name="Synthetic COEN fixture",
            source_url="http://127.0.0.1/reportes",
        )
