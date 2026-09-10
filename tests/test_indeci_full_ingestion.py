import csv
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pymupdf
import pytest

from quebradas_limaeste.inventory.discovery import CSV_FIELDS
from quebradas_limaeste.inventory.full_ingestion import (
    ALL_DOCUMENT_FIELDS,
    FullIngestionError,
    duplicate_review_rows,
    execute_full_ingestion,
)
from quebradas_limaeste.inventory.pdf_download import TransportResult
from quebradas_limaeste.inventory.triage import load_discovery_universe

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
NOW = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


class StubTransport:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies
        self.calls: list[str] = []

    def download(
        self,
        url,
        destination,
        *,
        timeout_seconds,
        user_agent,
        max_bytes,
    ):
        self.calls.append(url)
        destination.write_bytes(self.bodies[url])
        return TransportResult(
            http_status=200,
            content_type="application/pdf",
            final_url=url,
        )


def discovery_row(
    index: int,
    *,
    year: int,
    report_type: str,
    report_number: str,
    report_date: date,
    title: str,
) -> dict[str, object]:
    pdf_url = (
        "https://portal.indeci.gob.pe/wp-content/uploads/"
        f"{year}/01/original-document-{index}.pdf"
    )
    digest = sha256(f"pdf\n{pdf_url}".encode()).hexdigest()[:20]
    discovery_id = f"indeci-discovery-{digest}"
    return {
        "discovery_id": discovery_id,
        "source_connector": "archive_informes",
        "title": title,
        "detail_url": f"https://portal.indeci.gob.pe/informe/item-{index}/",
        "pdf_url": pdf_url,
        "report_type": report_type,
        "report_number": report_number,
        "report_date": report_date.isoformat(),
        "year": year,
        "discovered_at_utc": NOW.isoformat(),
        "query_context": "{}",
        "raw_metadata": "{}",
        "discovery_warnings": "[]",
        "discovery_sources_attempted": '["archive_informes"]',
        "discovery_sources_matched": '["archive_informes"]',
        "dedup_status": "unique",
        "dedup_reason": "no_duplicate_signal",
    }


def write_discovery(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox((50, 50, 540, 760), (text + " ") * 8, fontsize=10)
    payload = document.tobytes()
    document.close()
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def copy_configs(tmp_path: Path) -> tuple[Path, Path]:
    config = tmp_path / SOURCE_CONFIG
    quality = tmp_path / "configs/sources/indeci_candidate_quality.yaml"
    config.parent.mkdir(parents=True)
    config.write_bytes(SOURCE_CONFIG.read_bytes())
    quality.write_bytes(
        Path("configs/sources/indeci_candidate_quality.yaml").read_bytes()
    )
    return config.relative_to(tmp_path), quality.relative_to(tmp_path)


def test_full_ingestion_reuses_raw_and_processes_complete_snapshot(tmp_path) -> None:
    config, quality = copy_configs(tmp_path)
    discovery = tmp_path / "metadata" / "indeci" / "discovery.csv"
    rows = [
        discovery_row(
            1,
            year=2019,
            report_type="reporte_complementario",
            report_number="630",
            report_date=date(2019, 3, 3),
            title="Huaico en Chaclacayo",
        ),
        discovery_row(
            2,
            year=2023,
            report_type="informe_emergencia",
            report_number="2000",
            report_date=date(2023, 3, 20),
            title="Lluvias intensas en Lima",
        ),
    ]
    write_discovery(discovery, rows)
    universe = load_discovery_universe(
        discovery,
        config_path=config,
        allowed_root=tmp_path,
    )
    prior = universe[0]
    prior_path = tmp_path / "data" / "raw" / "indeci" / "2019" / (
        f"{prior.document_id}.pdf"
    )
    prior_path.parent.mkdir(parents=True)
    prior_path.write_bytes(
        pdf_bytes(
            "El 25 de febrero de 2019 se reporto un huaico en Chaclacayo y Cusipata."
        )
    )
    second_url = str(rows[1]["pdf_url"])
    transport = StubTransport(
        {
            second_url: pdf_bytes(
                "El 16 de marzo de 2023 se reportaron lluvias intensas en Lima."
            )
        }
    )

    payload = execute_full_ingestion(
        discovery,
        config_path=config,
        quality_config_path=quality,
        documents_output=Path("metadata/indeci/all_documents.csv"),
        candidates_output=Path("metadata/indeci/all_event_candidates.csv"),
        clusters_output=Path("metadata/indeci/all_event_clusters.csv"),
        duplicates_output=Path("metadata/indeci/duplicate_review.csv"),
        run_output=Path("metadata/indeci/full_ingestion_run.json"),
        registry_path=Path("metadata/indeci/document_registry.json"),
        allowed_root=tmp_path,
        expected_documents=2,
        transport=transport,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["total_universe"] == 2
    assert payload["already_available"] == 1
    assert payload["newly_downloaded"] == 1
    assert payload["failed"] == 0
    assert transport.calls == [second_url]
    assert prior_path.exists()
    new_raw = tmp_path / "data/raw/indeci/2023/original-document-2.pdf"
    assert new_raw.exists()
    documents = read_csv(tmp_path / "metadata/indeci/all_documents.csv")
    assert len(documents) == 2
    assert tuple(documents[0]) == ALL_DOCUMENT_FIELDS
    assert {row["review_required"] for row in documents} == {"true"}
    assert {row["golden_control"] for row in documents} >= {"positive_control"}
    assert all("training_label" not in row for row in documents)
    assert read_csv(tmp_path / "metadata/indeci/all_event_candidates.csv")
    assert read_csv(tmp_path / "metadata/indeci/all_event_clusters.csv")


def test_full_ingestion_requires_exact_expected_universe(tmp_path) -> None:
    config, _ = copy_configs(tmp_path)
    discovery = tmp_path / "metadata/indeci/discovery.csv"
    write_discovery(
        discovery,
        [
            discovery_row(
                1,
                year=2019,
                report_type="reporte_complementario",
                report_number="1",
                report_date=date(2019, 1, 1),
                title="Reporte de Lima",
            )
        ],
    )

    with pytest.raises(FullIngestionError, match="expected 131"):
        execute_full_ingestion(
            discovery,
            config_path=config,
            allowed_root=tmp_path,
            transport=StubTransport({}),
            sleep=lambda _: None,
            now=lambda: NOW,
        )


def test_duplicate_review_detects_sha_url_and_report_identity() -> None:
    common = {
        "sha256": "a" * 64,
        "pdf_url": "https://portal.indeci.gob.pe/a.pdf",
        "report_type": "informe_emergencia",
        "report_number": "7",
        "report_date": "2023-01-02",
        "raw_local_path": "/raw/a.pdf",
    }
    rows = [
        {"document_id": "INDECI_IE7_20230102", **common},
        {"document_id": "INDECI_IE7_20230102_ALT", **common},
    ]

    duplicates = duplicate_review_rows(rows)

    assert {row["duplicate_signal"] for row in duplicates} == {
        "sha256",
        "canonical_url",
        "report_identity",
    }
    assert all(row["document_count"] == 2 for row in duplicates)
    assert all(row["review_status"] == "pending_review" for row in duplicates)
