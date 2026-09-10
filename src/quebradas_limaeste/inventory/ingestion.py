"""Controlled orchestration from approved PDF URLs to review candidates."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from quebradas_limaeste.inventory.candidate_audit import (
    CANDIDATE_OUTPUT,
    write_original_candidates_from_payload,
)
from quebradas_limaeste.inventory.document_classification import classify_document
from quebradas_limaeste.inventory.event_candidates import extract_event_candidates
from quebradas_limaeste.inventory.ingestion_models import (
    IngestedDocument,
    IngestionPolicy,
    SeedDocument,
    derive_document_identity,
    derive_seed_document,
    load_ingestion_policy,
    load_seed_documents,
)
from quebradas_limaeste.inventory.pdf_download import (
    CHUNK_SIZE,
    DownloadError,
    DownloadResult,
    PDFDownloadTransport,
    download_seed_pdf,
)
from quebradas_limaeste.inventory.pdf_extract import (
    PDF_SIGNATURE,
    PDFExtractionError,
    extract_pdf_text,
)

INGESTION_OUTPUT = Path("metadata/indeci/ingestion_run.json")
DOCUMENT_REGISTRY = Path("metadata/indeci/document_registry.json")
RAW_ROOT = Path("data/raw/indeci")
INTERIM_ROOT = Path("data/interim/indeci")
MAX_REGISTRY_BYTES = 4 * 1024 * 1024
MAX_REGISTRY_DOCUMENTS = 10_000


class IngestionError(RuntimeError):
    """Raised when local ingestion state is unsafe or inconsistent."""


class ProcessableDocument(Protocol):
    """Document contract consumed by the approved downstream pipeline."""

    document_id: str
    local_path: Path
    storage_year: int
    expected_site_terms: tuple[str, ...]
    expected_region_terms: tuple[str, ...]

    def to_dict(self) -> dict[str, object]: ...


def execute_ingest_seeds(
    config_path: Path,
    seeds_path: Path,
    *,
    output_path: Path = INGESTION_OUTPUT,
    registry_path: Path = DOCUMENT_REGISTRY,
    candidate_output: Path = CANDIDATE_OUTPUT,
    allowed_root: Path,
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Ingest only the documents explicitly listed in the seed configuration."""
    root = allowed_root.resolve()
    policy = load_ingestion_policy(_input_path(config_path, root=root))
    seeds = load_seed_documents(
        _input_path(seeds_path, root=root),
        policy=policy,
    )
    return _execute(
        seeds,
        policy=policy,
        output_path=output_path,
        registry_path=registry_path,
        candidate_output=candidate_output,
        root=root,
        transport=transport,
        sleep=sleep,
        now=now,
    )


def execute_ingest_url(
    config_path: Path,
    url: str,
    *,
    output_path: Path = INGESTION_OUTPUT,
    registry_path: Path = DOCUMENT_REGISTRY,
    candidate_output: Path = CANDIDATE_OUTPUT,
    allowed_root: Path,
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Ingest one explicit URL after deriving identity from its filename."""
    root = allowed_root.resolve()
    policy = load_ingestion_policy(_input_path(config_path, root=root))
    seed = derive_seed_document(url, policy=policy)
    return _execute(
        (seed,),
        policy=policy,
        output_path=output_path,
        registry_path=registry_path,
        candidate_output=candidate_output,
        root=root,
        transport=transport,
        sleep=sleep,
        now=now,
    )


def execute_ingest_file(
    config_path: Path,
    file_path: Path,
    *,
    source_url: str | None = None,
    output_path: Path = INGESTION_OUTPUT,
    candidate_output: Path = CANDIDATE_OUTPUT,
    allowed_root: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Ingest one explicit local PDF without copying it into the workspace."""
    root = allowed_root.resolve()
    policy = load_ingestion_policy(_input_path(config_path, root=root))
    started = _utc_now(now)
    source = _prepare_local_document(
        file_path,
        policy=policy,
        source_url=source_url,
        ingested_at=started,
    )
    document, extraction_warnings, extraction_error = process_document(
        source,
        policy=policy,
        root=root,
    )
    warnings = [
        f"{source.document_id}: {warning}"
        for warning in (*source.warnings, *extraction_warnings)
    ]
    errors = (
        [f"{source.document_id}: {extraction_error}"]
        if extraction_error is not None
        else []
    )
    return _write_run_outputs(
        started=started,
        requested=1,
        downloaded=0,
        duplicates=0,
        download_errors=0,
        documents=[document],
        warnings=warnings,
        errors=errors,
        output_path=output_path,
        candidate_output=candidate_output,
        root=root,
        now=now,
    )


def _execute(
    seeds: Sequence[SeedDocument],
    *,
    policy: IngestionPolicy,
    output_path: Path,
    registry_path: Path,
    candidate_output: Path,
    root: Path,
    transport: PDFDownloadTransport | None,
    sleep: Callable[[float], None],
    now: Callable[[], datetime],
) -> dict[str, object]:
    started = _utc_now(now)
    run_output = _output_path(output_path, root=root)
    registry_output = _output_path(registry_path, root=root)
    candidate_destination = _output_path(candidate_output, root=root)
    if len({run_output, registry_output, candidate_destination}) != 3:
        raise IngestionError("ingestion output paths must be different")
    raw_root = _output_path(RAW_ROOT, root=root)
    registry = _load_registry(registry_output)
    records = list(registry["documents"])
    documents: list[dict[str, object]] = []
    warnings: list[str] = []
    errors: list[str] = []
    downloaded = 0
    duplicates = 0
    download_errors = 0

    for seed in seeds:
        try:
            download = download_seed_pdf(
                seed,
                policy=policy,
                raw_root=raw_root,
                existing_records=records,
                transport=transport,
                sleep=sleep,
                now=now,
            )
        except DownloadError as exc:
            download_errors += 1
            errors.append(f"{seed.document_id}: {exc}")
            continue

        if download.download_status == "downloaded":
            downloaded += 1
        else:
            duplicates += 1
        warnings.extend(
            f"{seed.document_id}: {warning}" for warning in download.warnings
        )
        source = _prepare_downloaded_document(seed, download)
        document, extraction_warnings, extraction_error = process_document(
            source,
            policy=policy,
            root=root,
        )
        warnings.extend(
            f"{seed.document_id}: {warning}" for warning in extraction_warnings
        )
        if extraction_error is not None:
            errors.append(f"{seed.document_id}: {extraction_error}")
        records = _upsert_registry_record(records, document)
        documents.append(document)

    if documents:
        _write_json(registry_output, {"documents": records})
    return _write_run_outputs(
        started=started,
        requested=len(seeds),
        downloaded=downloaded,
        duplicates=duplicates,
        download_errors=download_errors,
        documents=documents,
        warnings=warnings,
        errors=errors,
        output_path=run_output,
        candidate_output=candidate_destination,
        root=root,
        now=now,
    )


def _prepare_downloaded_document(
    seed: SeedDocument,
    download: DownloadResult,
) -> IngestedDocument:
    return IngestedDocument(
        document_id=seed.document_id,
        report_number=seed.report_number,
        report_type=seed.report_type,
        report_date=seed.report_date,
        source_type="official_url",
        source_url=download.source_url,
        original_filename=download.original_filename,
        local_path=download.local_path,
        sha256=download.sha256,
        file_size=download.file_size,
        ingested_at_utc=download.downloaded_at_utc,
        downloaded_at_utc=download.downloaded_at_utc,
        expected_site_terms=seed.expected_site_terms,
        expected_region_terms=seed.expected_region_terms,
        final_url=download.final_url,
        http_status=download.http_status,
        content_type=download.content_type,
        download_status=download.download_status,
        warnings=download.warnings,
    )


def _prepare_local_document(
    file_path: Path,
    *,
    policy: IngestionPolicy,
    source_url: str | None,
    ingested_at: datetime,
) -> IngestedDocument:
    unresolved = file_path if file_path.is_absolute() else Path.cwd() / file_path
    if unresolved.is_symlink():
        raise IngestionError("local PDF symlinks are not allowed")
    try:
        source = unresolved.resolve(strict=True)
    except OSError as exc:
        raise IngestionError("local PDF is missing or inaccessible") from exc
    if not source.is_file():
        raise IngestionError("local PDF is not a regular file")
    file_size = source.stat().st_size
    if file_size > policy.max_pdf_bytes:
        raise IngestionError("local PDF exceeds configured byte limit")
    with source.open("rb") as input_file:
        if input_file.read(len(PDF_SIGNATURE)) != PDF_SIGNATURE:
            raise IngestionError("local file does not have a PDF signature")

    identity = derive_document_identity(source.name)
    normalized_url: str | None = None
    if source_url is not None:
        seed = derive_seed_document(source_url, policy=policy)
        expected = (
            seed.document_id,
            seed.report_number,
            seed.report_type,
            seed.report_date,
        )
        if identity != expected:
            raise IngestionError("local PDF filename and source URL do not match")
        normalized_url = seed.source_url
    document_id, number, report_type, report_date = identity
    return IngestedDocument(
        document_id=document_id,
        report_number=number,
        report_type=report_type,
        report_date=report_date,
        source_type="local_file",
        source_url=normalized_url,
        original_path=source,
        original_filename=source.name,
        local_path=source,
        sha256=_file_sha256(source),
        file_size=file_size,
        ingested_at_utc=ingested_at,
        expected_site_terms=policy.expected_site_terms,
        expected_region_terms=policy.expected_region_terms,
    )


def process_document(
    source: ProcessableDocument,
    *,
    policy: IngestionPolicy,
    root: Path,
) -> tuple[dict[str, object], tuple[str, ...], str | None]:
    document = source.to_dict()
    text_path = (
        root
        / INTERIM_ROOT
        / str(source.storage_year)
        / f"{source.document_id}.txt"
    )
    try:
        extraction = extract_pdf_text(
            source.local_path,
            text_path=text_path,
            minimum_text_characters=policy.minimum_text_characters,
            allowed_root=root,
            source_root=source.local_path.parent,
        )
    except PDFExtractionError as exc:
        document.update(
            {
                "page_count": 0,
                "text_path": str(text_path),
                "extraction_status": "error",
                "ocr_required": False,
                "classification": None,
                "event_candidates": [],
            }
        )
        return document, (), str(exc)

    document.update(extraction.to_dict())
    if extraction.ocr_required:
        document["classification"] = None
        document["event_candidates"] = []
        return document, extraction.warnings, None

    classification = classify_document(
        extraction.full_text,
        expected_site_terms=source.expected_site_terms,
        expected_region_terms=source.expected_region_terms,
    )
    candidates = extract_event_candidates(
        source.document_id,
        extraction.page_texts,
        expected_site_terms=source.expected_site_terms,
        expected_region_terms=source.expected_region_terms,
    )
    document["classification"] = classification.to_dict()
    document["event_candidates"] = [candidate.to_dict() for candidate in candidates]
    return document, extraction.warnings, None


def _write_run_outputs(
    *,
    started: datetime,
    requested: int,
    downloaded: int,
    duplicates: int,
    download_errors: int,
    documents: list[dict[str, object]],
    warnings: list[str],
    errors: list[str],
    output_path: Path,
    candidate_output: Path,
    root: Path,
    now: Callable[[], datetime],
) -> dict[str, object]:
    run_output = _output_path(output_path, root=root)
    candidate_destination = _output_path(candidate_output, root=root)
    if run_output == candidate_destination:
        raise IngestionError("ingestion JSON and candidate CSV must be different")
    extracted = 0
    classified = 0
    events = 0
    reviews = 0
    for item in documents:
        if item.get("extraction_status") != "error":
            extracted += 1
        classification = item.get("classification")
        candidates = item.get("event_candidates")
        if not isinstance(candidates, list):
            raise IngestionError("processed document candidates are invalid")
        events += len(candidates)
        reviews += len(candidates)
        if isinstance(classification, dict):
            classified += 1
            reviews += int(bool(classification.get("review_required")))
    finished = _utc_now(now)
    payload: dict[str, object] = {
        "run_id": f"indeci-ingestion-{started.strftime('%Y%m%dT%H%M%SZ')}",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "documents_requested": requested,
        "documents_downloaded": downloaded,
        "duplicates": duplicates,
        "download_errors": download_errors,
        "documents_extracted": extracted,
        "documents_classified": classified,
        "event_candidates": events,
        "items_pending_review": reviews,
        "documents": documents,
        "warnings": warnings,
        "errors": errors,
    }
    _write_json(run_output, payload)
    write_original_candidates_from_payload(
        payload,
        output_path=candidate_destination,
        allowed_root=root,
    )
    return payload


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _input_path(path: Path, *, root: Path) -> Path:
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise IngestionError("configuration path is outside the workspace or missing")
    if candidate.is_symlink():
        raise IngestionError("configuration symlinks are not allowed")
    return candidate


def _output_path(path: Path, *, root: Path) -> Path:
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    if not candidate.is_relative_to(root):
        raise IngestionError("output path is outside the workspace")
    if candidate.is_symlink():
        raise IngestionError("output symlinks are not allowed")
    return candidate


def _load_registry(path: Path) -> dict[str, list[dict[str, object]]]:
    if not path.exists():
        return {"documents": []}
    if not path.is_file() or path.stat().st_size > MAX_REGISTRY_BYTES:
        raise IngestionError("document registry is not a bounded regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IngestionError("document registry is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"documents"}:
        raise IngestionError("document registry has invalid keys")
    documents = payload["documents"]
    if (
        not isinstance(documents, list)
        or len(documents) > MAX_REGISTRY_DOCUMENTS
        or not all(isinstance(item, dict) for item in documents)
    ):
        raise IngestionError("document registry has invalid documents")
    return {"documents": documents}


def load_document_registry(
    *,
    registry_path: Path = DOCUMENT_REGISTRY,
    allowed_root: Path,
) -> list[dict[str, object]]:
    """Read the bounded shared registry for an approved ingestion workflow."""
    root = allowed_root.resolve()
    target = _output_path(registry_path, root=root)
    return list(_load_registry(target)["documents"])


def _upsert_registry_record(
    records: list[dict[str, object]],
    document: dict[str, object],
) -> list[dict[str, object]]:
    document_id = document["document_id"]
    retained = [item for item in records if item.get("document_id") != document_id]
    retained.append(document)
    if len(retained) > MAX_REGISTRY_DOCUMENTS:
        raise IngestionError("document registry exceeds its item limit")
    return retained


def upsert_document_registry_record(
    records: list[dict[str, object]],
    document: dict[str, object],
) -> list[dict[str, object]]:
    """Return registry records with one validated document identity replaced."""
    return _upsert_registry_record(records, document)


def write_document_registry(
    records: list[dict[str, object]],
    *,
    registry_path: Path = DOCUMENT_REGISTRY,
    allowed_root: Path,
) -> Path:
    """Atomically publish bounded shared registry records inside the workspace."""
    if len(records) > MAX_REGISTRY_DOCUMENTS or not all(
        isinstance(item, dict) for item in records
    ):
        raise IngestionError("document registry has invalid documents")
    root = allowed_root.resolve()
    target = _output_path(registry_path, root=root)
    _write_json(target, {"documents": records})
    return target


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part_path = path.with_name(f"{path.name}.part")
    if part_path.exists() or part_path.is_symlink():
        raise IngestionError("staging JSON path already exists")
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        with part_path.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        part_path.replace(path)
    finally:
        if part_path.exists():
            part_path.unlink()


def _utc_now(now: Callable[[], datetime]) -> datetime:
    timestamp = now()
    if timestamp.tzinfo is None:
        raise IngestionError("ingestion timestamp must be timezone-aware")
    return timestamp.astimezone(UTC)
