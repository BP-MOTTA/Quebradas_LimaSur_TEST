import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from quebradas_limaeste.inventory.batch_ingestion import (
    BATCH_DOCUMENTS_OUTPUT,
    BATCH_EVENT_CANDIDATES_OUTPUT,
    BATCH_EVENT_CLUSTERS_OUTPUT,
    BATCH_REVIEW_QUEUE_OUTPUT,
    BATCH_RUN_OUTPUT,
    BatchIngestionError,
    execute_ingest_batch,
)
from quebradas_limaeste.inventory.pdf_download import TransportResult
from quebradas_limaeste.inventory.triage import SELECTION_FIELDS

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


class StubTransport:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def download(
        self,
        url,
        destination,
        *,
        timeout_seconds,
        user_agent,
        max_bytes,
    ):
        del timeout_seconds, user_agent, max_bytes
        self.calls.append(url)
        status, body = self.outcomes.pop(0)
        if body is not None:
            destination.write_bytes(body)
        return TransportResult(
            http_status=status,
            content_type="application/pdf",
            final_url=url,
        )


def synthetic_pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    if text:
        page.insert_textbox(
            pymupdf.Rect(50, 50, 545, 790),
            text,
            fontsize=10,
        )
    result = document.tobytes()
    document.close()
    return result


def relevant_without_candidate_pdf() -> bytes:
    return synthetic_pdf_bytes(
        "REPORTE SOBRE HUAICO EN CHACLACAYO, LIMA. "
        "Este documento sintetico contiene suficiente texto para validar la "
        "clasificacion documental, pero no incluye una fecha explicita ligada "
        "al evento y por ello no debe generar un candidato fuerte ni afirmar "
        "una observacion cientifica. La revision humana sigue siendo obligatoria."
    )


def strong_candidate_pdf() -> bytes:
    return synthetic_pdf_bytes(
        "REPORTE COMPLEMENTARIO SOBRE CHACLACAYO, LIMA. "
        "El 25 de febrero de 2019, se produjo la activacion de la quebrada "
        "Cusipata en el distrito de Chaclacayo, Lima. Este contenido es una "
        "fixture sintetica offline y no constituye evidencia cientifica ni "
        "documental real. La validacion humana permanece pendiente."
    )


def copy_configs(tmp_path: Path) -> Path:
    config_dir = tmp_path / "configs" / "sources"
    config_dir.mkdir(parents=True)
    source = config_dir / SOURCE_CONFIG.name
    quality = config_dir / QUALITY_CONFIG.name
    source.write_text(SOURCE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    quality.write_text(QUALITY_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return source


def selection_row(index: int, *, selected: bool = True) -> dict[str, object]:
    year = (2017, 2019, 2023, 2024)[index % 4]
    return {
        "batch_id": "indeci-batch-0123456789abcdef",
        "document_id": f"INDECI_RC{1000 + index}_{year}0101",
        "discovery_id": f"indeci-discovery-{index:020x}",
        "year": year,
        "report_type": "reporte_complementario",
        "report_number": str(1000 + index),
        "report_date": f"{year}-01-01",
        "title": f"Documento sintetico {index}",
        "source_connector": "archive_informes",
        "detail_url": f"https://portal.indeci.gob.pe/emergencias/item-{index}/",
        "pdf_url": (
            "https://portal.indeci.gob.pe/wp-content/uploads/"
            f"{year}/01/document-{index}.pdf"
        ),
        "triage_tier": "D",
        "triage_score": 0,
        "triage_reasons": '["low_preliminary_evidence"]',
        "golden_control": "",
        "selected": str(selected).lower(),
        "selection_reason": "selected_within_year_quota",
    }


def write_selection(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=SELECTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def test_batch_rejects_selected_rows_above_configured_limit(tmp_path) -> None:
    config = copy_configs(tmp_path)
    selection = tmp_path / "metadata" / "indeci" / "selection.csv"
    write_selection(selection, [selection_row(index) for index in range(26)])
    transport = StubTransport([])

    with pytest.raises(BatchIngestionError, match="configured maximum"):
        execute_ingest_batch(
            selection,
            config_path=config,
            allowed_root=tmp_path,
            transport=transport,
            sleep=lambda _: None,
            now=lambda: NOW,
        )

    assert transport.calls == []
    assert not (tmp_path / BATCH_RUN_OUTPUT).exists()


def test_batch_continues_after_failure_and_records_duplicate_and_empty_pdf(
    tmp_path,
) -> None:
    config = copy_configs(tmp_path)
    selection = tmp_path / "metadata" / "indeci" / "selection.csv"
    rows = [selection_row(index) for index in range(4)]
    write_selection(selection, rows)
    relevant = relevant_without_candidate_pdf()
    transport = StubTransport(
        [
            (200, relevant),
            (200, relevant),
            (200, synthetic_pdf_bytes("")),
            (404, None),
        ]
    )

    payload = execute_ingest_batch(
        selection,
        config_path=config,
        allowed_root=tmp_path,
        transport=transport,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["documents_selected"] == 4
    assert payload["documents_downloaded"] == 2
    assert payload["documents_failed"] == 1
    assert payload["duplicates"] == 1
    assert payload["ocr_required"] == 1
    assert payload["documents_relevant"] == 2
    assert payload["event_candidates"] == 0
    assert len(payload["errors"]) == 1
    documents = read_csv(tmp_path / BATCH_DOCUMENTS_OUTPUT)
    assert len(documents) == 4
    assert [row["download_status"] for row in documents] == [
        "downloaded",
        "duplicate",
        "downloaded",
        "error",
    ]
    assert documents[2]["extraction_status"] == "empty"
    assert documents[2]["ocr_required"] == "true"
    assert documents[2]["review_required"] == "true"
    assert documents[3]["download_success"] == "false"
    assert "HTTP 404" in documents[3]["error"]
    assert all(
        row["candidate_strength"] != "strong"
        for row in read_csv(tmp_path / BATCH_EVENT_CANDIDATES_OUTPUT)
    )
    assert read_csv(tmp_path / BATCH_EVENT_CLUSTERS_OUTPUT) == []
    review_reasons = {
        row["review_reason"] for row in read_csv(tmp_path / BATCH_REVIEW_QUEUE_OUTPUT)
    }
    assert {"possible_document", "ocr_required", "duplicate_possible"} <= (
        review_reasons
    )
    assert len(transport.calls) == 4


def test_strong_batch_candidate_remains_pending_review(tmp_path) -> None:
    config = copy_configs(tmp_path)
    selection = tmp_path / "metadata" / "indeci" / "selection.csv"
    write_selection(selection, [selection_row(1)])

    payload = execute_ingest_batch(
        selection,
        config_path=config,
        allowed_root=tmp_path,
        transport=StubTransport([(200, strong_candidate_pdf())]),
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["strong_candidates"] == 1
    candidates = read_csv(tmp_path / BATCH_EVENT_CANDIDATES_OUTPUT)
    clusters = read_csv(tmp_path / BATCH_EVENT_CLUSTERS_OUTPUT)
    assert candidates[0]["candidate_strength"] == "strong"
    assert candidates[0]["validation_status"] == "pending_review"
    assert clusters[0]["validation_status"] == "pending_review"
    assert json.loads((tmp_path / BATCH_RUN_OUTPUT).read_text()) == payload
