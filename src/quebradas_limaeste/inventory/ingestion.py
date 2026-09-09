"""Controlled orchestration from approved PDF URLs to review candidates."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    CANDIDATE_OUTPUT,
    write_original_candidates_from_payload,
)
from quebradas_limaeste.inventory.document_classification import classify_document
from quebradas_limaeste.inventory.event_candidates import extract_event_candidates
from quebradas_limaeste.inventory.ingestion_models import (
    IngestionPolicy,
    SeedDocument,
    derive_seed_document,
    load_ingestion_policy,
    load_seed_documents,
)
from quebradas_limaeste.inventory.pdf_download import (
    DownloadError,
    PDFDownloadTransport,
    download_seed_pdf,
)
from quebradas_limaeste.inventory.pdf_extract import (
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
    raw_root = _output_path(RAW_ROOT, root=root)
    registry = _load_registry(registry_output)
    records = list(registry["documents"])
    documents: list[dict[str, object]] = []
    warnings: list[str] = []
    errors: list[str] = []
    downloaded = 0
    duplicates = 0
    download_errors = 0
    extracted = 0
    classified = 0
    candidate_count = 0
    pending_review = 0

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
        document = download.to_dict()
        text_path = (
            root
            / INTERIM_ROOT
            / str(seed.report_date.year)
            / f"{seed.document_id}.txt"
        )
        try:
            extraction = extract_pdf_text(
                download.local_path,
                text_path=text_path,
                minimum_text_characters=policy.minimum_text_characters,
                allowed_root=root,
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
            errors.append(f"{seed.document_id}: {exc}")
        else:
            extracted += 1
            warnings.extend(
                f"{seed.document_id}: {warning}" for warning in extraction.warnings
            )
            classification = classify_document(
                extraction.full_text,
                expected_site_terms=seed.expected_site_terms,
                expected_region_terms=seed.expected_region_terms,
            )
            candidates = extract_event_candidates(
                seed.document_id,
                extraction.page_texts,
                expected_site_terms=seed.expected_site_terms,
                expected_region_terms=seed.expected_region_terms,
            )
            classified += 1
            candidate_count += len(candidates)
            pending_review += int(classification.review_required) + len(candidates)
            document.update(extraction.to_dict())
            document["classification"] = classification.to_dict()
            document["event_candidates"] = [
                candidate.to_dict() for candidate in candidates
            ]
        records = _upsert_registry_record(records, document)
        documents.append(document)

    finished = _utc_now(now)
    payload: dict[str, object] = {
        "run_id": f"indeci-ingestion-{started.strftime('%Y%m%dT%H%M%SZ')}",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "documents_requested": len(seeds),
        "documents_downloaded": downloaded,
        "duplicates": duplicates,
        "download_errors": download_errors,
        "documents_extracted": extracted,
        "documents_classified": classified,
        "event_candidates": candidate_count,
        "items_pending_review": pending_review,
        "documents": documents,
        "warnings": warnings,
        "errors": errors,
    }
    if documents:
        _write_json(registry_output, {"documents": records})
    _write_json(run_output, payload)
    write_original_candidates_from_payload(
        payload,
        output_path=candidate_output,
        allowed_root=root,
    )
    return payload


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
