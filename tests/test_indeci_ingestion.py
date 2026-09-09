import json
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
import pytest

from quebradas_limaeste.inventory.candidate_audit import CANDIDATE_OUTPUT
from quebradas_limaeste.inventory.ingestion import (
    DOCUMENT_REGISTRY,
    INGESTION_OUTPUT,
    IngestionError,
    execute_ingest_seeds,
)
from quebradas_limaeste.inventory.pdf_download import TransportResult

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
SEEDS_CONFIG = Path("configs/sources/indeci_seed_documents.yaml")
GOLDEN_URL = (
    "https://portal.indeci.gob.pe/wp-content/uploads/2023/05/"
    "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-"
    "LLUVIAS-INTENSAS-EN-EL-DEPARTAMENTO-DE-LIMA-36-DEE.pdf"
)
NOW = datetime(2026, 9, 8, 18, 0, tzinfo=UTC)


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
        self.calls.append(url)
        result, body = self.outcomes.pop(0)
        if body is not None:
            destination.write_bytes(body)
        return result


def synthetic_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 50, 545, 790),
        "INFORME DE EMERGENCIA N. 1496 - 5/5/2023\n"
        "El 16 de marzo de 2023, se produjo un huaico en Chaclacayo, Lima.\n"
        "El documento sintetico conserva texto adicional para comprobar que "
        "la extraccion supera el umbral minimo configurado. Esta descripcion "
        "no representa una observacion cientifica ni una fuente documental "
        "real y se usa solamente dentro de las pruebas offline.",
        fontsize=10,
    )
    result = document.tobytes()
    document.close()
    return result


def copy_configs(tmp_path):
    config_dir = tmp_path / "configs" / "sources"
    config_dir.mkdir(parents=True)
    config = config_dir / SOURCE_CONFIG.name
    seeds = config_dir / SEEDS_CONFIG.name
    config.write_text(SOURCE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    seeds.write_text(SEEDS_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return config, seeds


def response(status=200):
    return TransportResult(
        http_status=status,
        content_type="application/pdf",
        final_url=GOLDEN_URL,
    )


def test_ingest_seeds_records_provenance_classification_and_candidates(
    tmp_path,
) -> None:
    config, seeds = copy_configs(tmp_path)
    transport = StubTransport([(response(), synthetic_pdf_bytes())])

    payload = execute_ingest_seeds(
        config,
        seeds,
        allowed_root=tmp_path,
        transport=transport,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["documents_requested"] == 1
    assert payload["documents_downloaded"] == 1
    assert payload["duplicates"] == 0
    assert payload["download_errors"] == 0
    assert payload["documents_extracted"] == 1
    assert payload["documents_classified"] == 1
    assert payload["event_candidates"] == 1
    assert payload["items_pending_review"] == 2
    document = payload["documents"][0]
    assert document["document_id"] == "INDECI_IE1496_20230505"
    assert document["source_url"] == GOLDEN_URL
    assert document["final_url"] == GOLDEN_URL
    assert document["sha256"]
    assert document["file_size"] > 0
    assert document["page_count"] == 1
    assert document["http_status"] == 200
    assert document["extraction_status"] == "success"
    assert document["classification"]["relevance_status"] == "relevant"
    candidate = document["event_candidates"][0]
    assert candidate["source_page"] == 1
    assert candidate["event_date"] == "2023-03-16"
    assert candidate["validation_status"] == "pending_review"
    assert Path(document["local_path"]).is_file()
    assert Path(document["text_path"]).is_file()
    assert transport.calls == [GOLDEN_URL]

    run_payload = json.loads((tmp_path / INGESTION_OUTPUT).read_text())
    registry = json.loads((tmp_path / DOCUMENT_REGISTRY).read_text())
    assert run_payload == payload
    assert registry["documents"][0]["sha256"] == document["sha256"]
    candidate_lines = (tmp_path / CANDIDATE_OUTPUT).read_text().splitlines()
    assert len(candidate_lines) == 2
    assert "candidate_id" in candidate_lines[0]
    assert "pending_review" in candidate_lines[1]


def test_ingest_seeds_is_idempotent_by_registered_document(tmp_path) -> None:
    config, seeds = copy_configs(tmp_path)
    execute_ingest_seeds(
        config,
        seeds,
        allowed_root=tmp_path,
        transport=StubTransport([(response(), synthetic_pdf_bytes())]),
        sleep=lambda _: None,
        now=lambda: NOW,
    )
    no_network = StubTransport([])

    payload = execute_ingest_seeds(
        config,
        seeds,
        allowed_root=tmp_path,
        transport=no_network,
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["documents_downloaded"] == 0
    assert payload["duplicates"] == 1
    assert payload["documents_extracted"] == 1
    assert no_network.calls == []


def test_ingest_seeds_records_download_error_without_partial_files(tmp_path) -> None:
    config, seeds = copy_configs(tmp_path)

    payload = execute_ingest_seeds(
        config,
        seeds,
        allowed_root=tmp_path,
        transport=StubTransport([(response(404), None)]),
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    assert payload["documents_requested"] == 1
    assert payload["documents_downloaded"] == 0
    assert payload["download_errors"] == 1
    assert payload["documents"] == []
    assert payload["errors"] == [
        "INDECI_IE1496_20230505: PDF request returned HTTP 404"
    ]
    assert (tmp_path / INGESTION_OUTPUT).is_file()
    assert list(tmp_path.rglob("*.part")) == []


def test_ingestion_rejects_raw_root_symlink_outside_workspace(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    config, seeds = copy_configs(workspace)
    raw_parent = workspace / "data" / "raw"
    raw_parent.mkdir(parents=True)
    (raw_parent / "indeci").symlink_to(outside, target_is_directory=True)

    with pytest.raises(IngestionError, match="outside the workspace"):
        execute_ingest_seeds(
            config,
            seeds,
            allowed_root=workspace,
            transport=StubTransport([(response(), synthetic_pdf_bytes())]),
            sleep=lambda _: None,
            now=lambda: NOW,
        )

    assert list(outside.iterdir()) == []
