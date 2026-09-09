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
    execute_ingest_file,
    execute_ingest_seeds,
    execute_ingest_url,
)
from quebradas_limaeste.inventory.pdf_download import TransportResult

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
SEEDS_CONFIG = Path("configs/sources/indeci_seed_documents.yaml")
GOLDEN_URL = (
    "https://portal.indeci.gob.pe/wp-content/uploads/2023/05/"
    "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-"
    "LLUVIAS-INTENSAS-EN-EL-DEPARTAMENTO-DE-LIMA-36-DEE.pdf"
)
POSITIVE_URL = (
    "https://portal.indeci.gob.pe/wp-content/uploads/2019/02/"
    "REPORTE-COMPLEMENTARIO-N%C2%BA-630-03MAR2019-HUAICO-EN-EL-"
    "DISTRITO-DE-LURIGANCHO-Y-CHACLACAYO-LIMA-3-1.pdf"
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


def synthetic_positive_pdf_bytes() -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_textbox(
        pymupdf.Rect(50, 50, 545, 790),
        "REPORTE COMPLEMENTARIO N. 630 - 03/03/2019 / COEN - INDECI / "
        "09:00 HORAS (Reporte N. 03) HUAICO EN EL DISTRITO DE LURIGANCHO "
        "Y CHACLACAYO - LIMA. I. HECHOS:\n"
        "El 25 de febrero de 2019, a las 10:00 horas aproximadamente, a "
        "consecuencia de las fuertes precipitaciones pluviales, se produjo "
        "la activación de las quebradas Los Cóndores, La Floresta, Cusipata "
        "y Huascarán en el distrito de Chaclacayo.\n"
        "Este contenido es una fixture sintética offline y no constituye "
        "evidencia científica ni documental real.",
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


def test_ingest_file_records_local_provenance_without_copying_source(
    tmp_path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    config, _ = copy_configs(workspace)
    source = outside / (
        "REPORTE-COMPLEMENTARIO-Nº-630-03MAR2019-HUAICO-EN-CHACLACAYO.pdf"
    )
    source.write_bytes(synthetic_positive_pdf_bytes())

    payload = execute_ingest_file(
        config,
        source,
        allowed_root=workspace,
        now=lambda: NOW,
    )

    document = payload["documents"][0]
    assert document["document_model"] == "indeci_pdf_v1"
    assert document["document_id"] == "INDECI_RC630_20190303"
    assert document["report_date"] == "2019-03-03"
    assert document["source_type"] == "local_file"
    assert document["original_path"] == str(source.resolve())
    assert document["sha256"]
    assert document["file_size"] == source.stat().st_size
    assert document["page_count"] == 1
    assert document["ingested_at_utc"] == NOW.isoformat()
    assert "source_url" not in document
    assert document["event_candidates"][0]["event_date"] == "2019-02-25"
    assert "Cusipata" in document["event_candidates"][0]["evidence_snippet"]
    assert "Chaclacayo" in document["event_candidates"][0]["evidence_snippet"]
    assert not list(workspace.rglob("*.pdf"))


def test_ingest_file_and_url_share_the_same_downstream_document_model(
    tmp_path,
) -> None:
    body = synthetic_positive_pdf_bytes()
    local_workspace = tmp_path / "local-workspace"
    url_workspace = tmp_path / "url-workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    local_config, _ = copy_configs(local_workspace)
    url_config, _ = copy_configs(url_workspace)
    source = outside / (
        "REPORTE-COMPLEMENTARIO-Nº-630-03MAR2019-HUAICO-EN-CHACLACAYO.pdf"
    )
    source.write_bytes(body)

    local_payload = execute_ingest_file(
        local_config,
        source,
        source_url=POSITIVE_URL,
        allowed_root=local_workspace,
        now=lambda: NOW,
    )
    url_payload = execute_ingest_url(
        url_config,
        POSITIVE_URL,
        allowed_root=url_workspace,
        transport=StubTransport(
            [
                (
                    TransportResult(200, "application/pdf", POSITIVE_URL),
                    body,
                )
            ]
        ),
        sleep=lambda _: None,
        now=lambda: NOW,
    )

    local_document = local_payload["documents"][0]
    url_document = url_payload["documents"][0]
    assert local_document["source_url"] == POSITIVE_URL
    common_fields = (
        "document_model",
        "document_id",
        "report_number",
        "report_type",
        "report_date",
        "sha256",
        "file_size",
        "page_count",
        "extraction_status",
        "ocr_required",
        "classification",
        "event_candidates",
    )
    assert {key: local_document[key] for key in common_fields} == {
        key: url_document[key] for key in common_fields
    }


def test_ingest_file_rejects_symlinked_pdf(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    outside.mkdir()
    config, _ = copy_configs(workspace)
    source = outside / "REPORTE-COMPLEMENTARIO-Nº-630-03MAR2019.pdf"
    source.write_bytes(synthetic_positive_pdf_bytes())
    link = outside / "REPORTE-COMPLEMENTARIO-Nº-631-03MAR2019.pdf"
    link.symlink_to(source)

    with pytest.raises(IngestionError, match="symlink"):
        execute_ingest_file(
            config,
            link,
            allowed_root=workspace,
            now=lambda: NOW,
        )
