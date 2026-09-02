from datetime import UTC, date, datetime

import pytest

from quebradas_limaeste.inventory.models import (
    InventoryManifest,
    InventoryRecord,
    InventoryValidationError,
    normalize_http_url,
    parse_document_date,
)


def test_record_preserves_traceability_and_requires_review_by_default() -> None:
    record = InventoryRecord.create(
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
        document_url="/avisos/reporte-001",
        title="  Candidate report  ",
        published_date="16 de marzo de 2023",
        evidence_text="  local fixture evidence  ",
        spatial_precision="C",
    )

    assert record.record_id.startswith("indeci-coen-")
    assert record.source_url == "https://coen.example.test/reportes"
    assert record.document_url == "https://coen.example.test/avisos/reporte-001"
    assert record.title == "Candidate report"
    assert record.published_date == date(2023, 3, 16)
    assert record.evidence_text == "local fixture evidence"
    assert record.requires_human_review is True
    assert record.to_dict()["published_date"] == "2023-03-16"


def test_record_ids_are_deterministic_for_same_traceable_fields() -> None:
    first = InventoryRecord.create(
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
        document_url="/avisos/reporte-001",
        title="Candidate report",
        published_date="2023-03-16",
    )
    second = InventoryRecord.create(
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
        document_url="/avisos/reporte-001",
        title="Candidate report",
        published_date="2023-03-16",
    )

    assert first.record_id == second.record_id


def test_unsupported_or_credentialed_urls_are_rejected() -> None:
    with pytest.raises(InventoryValidationError):
        normalize_http_url("javascript:alert(1)", base_url="https://coen.example.test")

    with pytest.raises(InventoryValidationError):
        normalize_http_url(
            "https://user:pass@coen.example.test/reporte",
            base_url="https://coen.example.test",
        )


def test_invalid_precision_and_oversized_evidence_are_rejected() -> None:
    with pytest.raises(InventoryValidationError):
        InventoryRecord.create(
            source_name="Synthetic COEN fixture",
            source_url="https://coen.example.test/reportes",
            document_url="/avisos/reporte-001",
            title="",
        )

    with pytest.raises(InventoryValidationError):
        InventoryRecord.create(
            source_name="Synthetic COEN fixture",
            source_url="https://coen.example.test/reportes",
            document_url="/avisos/reporte-001",
            title="Candidate report",
            spatial_precision="Z",
        )

    with pytest.raises(InventoryValidationError):
        InventoryRecord.create(
            source_name="Synthetic COEN fixture",
            source_url="https://coen.example.test/reportes",
            document_url="/avisos/reporte-001",
            title="Candidate report",
            evidence_text="x" * 501,
        )


def test_date_parser_accepts_known_formats_without_inventing_missing_dates() -> None:
    assert parse_document_date("2023-03-16") == date(2023, 3, 16)
    assert parse_document_date("16/03/2023") == date(2023, 3, 16)
    assert parse_document_date("16 de marzo de 2023") == date(2023, 3, 16)
    assert parse_document_date("") is None
    assert parse_document_date(None) is None


def test_manifest_is_offline_and_counts_records() -> None:
    record = InventoryRecord.create(
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
        document_url="/avisos/reporte-001",
        title="Candidate report",
    )

    manifest = InventoryManifest.create(
        source_name="Synthetic COEN fixture",
        source_url="https://coen.example.test/reportes",
        records=[record],
        generated_at_utc=datetime(2026, 9, 2, 16, 0, tzinfo=UTC),
    )

    assert manifest.mode == "offline"
    assert manifest.record_count == 1
    assert manifest.to_dict()["generated_at_utc"] == "2026-09-02T16:00:00+00:00"

    with pytest.raises(InventoryValidationError):
        InventoryManifest.create(
            source_name="Synthetic COEN fixture",
            source_url="https://coen.example.test/reportes",
            records=[],
            mode="live-smoke",
        )
