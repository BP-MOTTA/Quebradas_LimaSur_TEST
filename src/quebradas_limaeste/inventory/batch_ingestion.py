"""Bounded batch ingestion and review-first triage outputs for INDECI PDFs."""

from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    CandidateAuditError,
    execute_candidate_audit,
    write_original_candidates_from_payload,
)
from quebradas_limaeste.inventory.candidate_quality import CandidateQualityError
from quebradas_limaeste.inventory.ingestion import (
    DOCUMENT_REGISTRY,
    RAW_ROOT,
    load_document_registry,
    process_document,
    upsert_document_registry_record,
    write_document_registry,
)
from quebradas_limaeste.inventory.ingestion_models import (
    IngestionConfigError,
    IngestionPolicy,
    load_ingestion_policy,
    validate_pdf_source_url,
)
from quebradas_limaeste.inventory.pdf_download import (
    DownloadError,
    DownloadResult,
    PDFDownloadTransport,
    download_discovered_pdf,
)
from quebradas_limaeste.inventory.triage import (
    SELECTION_FIELDS,
    SOURCE_CONFIG,
    BatchPolicy,
    BatchPolicyError,
    load_batch_policy,
)

BATCH_DOCUMENTS_OUTPUT = Path("metadata/indeci/batch_documents.csv")
BATCH_EVENT_CANDIDATES_OUTPUT = Path("metadata/indeci/batch_event_candidates.csv")
BATCH_EVENT_CLUSTERS_OUTPUT = Path("metadata/indeci/batch_event_clusters.csv")
BATCH_REVIEW_QUEUE_OUTPUT = Path("metadata/indeci/review_queue.csv")
BATCH_RUN_OUTPUT = Path("metadata/indeci/batch_run.json")
QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")
MAX_SELECTION_BYTES = 10 * 1024 * 1024
MAX_SELECTION_ROWS = 5_000
ABSOLUTE_LARGE_BATCH_LIMIT = 100
BATCH_DOCUMENT_FIELDS = (
    "batch_id",
    "document_id",
    "discovery_id",
    "year",
    "report_number",
    "title",
    "sha256",
    "page_count",
    "download_status",
    "extraction_status",
    "relevance_status",
    "site_match",
    "geographic_match",
    "event_match",
    "matched_terms",
    "review_required",
    "ocr_required",
    "download_success",
    "extraction_success",
    "classification_success",
    "event_extraction_success",
    "error",
)
REVIEW_QUEUE_FIELDS = (
    "batch_id",
    "review_item_id",
    "item_type",
    "document_id",
    "candidate_id",
    "review_reason",
    "validation_status",
)
_REPORT_TYPES = {
    "reporte_complementario",
    "reporte_preliminar",
    "informe_emergencia",
}
_GOLDEN_CONTROLS = {"positive_control", "negative_ambiguous_control"}


class BatchIngestionError(RuntimeError):
    """Raised when batch input, state, or output violates its safety contract."""


@dataclass(frozen=True)
class BatchSelection:
    batch_id: str
    document_id: str
    discovery_id: str
    year: int
    report_type: str | None
    report_number: str | None
    report_date: date | None
    title: str
    source_connector: str
    detail_url: str | None
    pdf_url: str
    golden_control: str | None


@dataclass(frozen=True)
class BatchProcessDocument:
    selection: BatchSelection
    download: DownloadResult
    expected_site_terms: tuple[str, ...]
    expected_region_terms: tuple[str, ...]

    @property
    def document_id(self) -> str:
        return self.selection.document_id

    @property
    def local_path(self) -> Path:
        return self.download.local_path

    @property
    def storage_year(self) -> int:
        return self.selection.year

    def to_dict(self) -> dict[str, object]:
        selected = self.selection
        downloaded = self.download
        return {
            "document_model": "indeci_pdf_v1",
            "document_id": selected.document_id,
            "discovery_id": selected.discovery_id,
            "report_number": selected.report_number,
            "report_type": selected.report_type,
            "report_date": (
                selected.report_date.isoformat() if selected.report_date else None
            ),
            "title": selected.title,
            "source_connector": selected.source_connector,
            "detail_url": selected.detail_url,
            "source_type": "official_discovery",
            "source_url": downloaded.source_url,
            "original_filename": downloaded.original_filename,
            "local_path": str(downloaded.local_path),
            "sha256": downloaded.sha256,
            "file_size": downloaded.file_size,
            "ingested_at_utc": downloaded.downloaded_at_utc.isoformat(),
            "downloaded_at_utc": downloaded.downloaded_at_utc.isoformat(),
            "final_url": downloaded.final_url,
            "http_status": downloaded.http_status,
            "content_type": downloaded.content_type,
            "download_status": downloaded.download_status,
            "warnings": list(downloaded.warnings),
            "golden_control": selected.golden_control,
        }


def execute_ingest_batch(
    selection_path: Path,
    *,
    config_path: Path = SOURCE_CONFIG,
    quality_config_path: Path = QUALITY_CONFIG,
    documents_output: Path = BATCH_DOCUMENTS_OUTPUT,
    candidates_output: Path = BATCH_EVENT_CANDIDATES_OUTPUT,
    clusters_output: Path = BATCH_EVENT_CLUSTERS_OUTPUT,
    review_queue_output: Path = BATCH_REVIEW_QUEUE_OUTPUT,
    run_output: Path = BATCH_RUN_OUTPUT,
    registry_path: Path = DOCUMENT_REGISTRY,
    allowed_root: Path,
    allow_large_batch: bool = False,
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Ingest selected rows while isolating document-level failures."""
    root = allowed_root.resolve()
    config = _safe_input(config_path, root=root, max_bytes=65_536)
    quality_config = _safe_input(
        quality_config_path,
        root=root,
        max_bytes=65_536,
    )
    try:
        policy = load_ingestion_policy(config)
        batch_policy = load_batch_policy(config, allowed_root=root)
    except (IngestionConfigError, BatchPolicyError) as exc:
        raise BatchIngestionError(str(exc)) from exc
    selections = _load_selected_rows(
        selection_path,
        root=root,
        policy=policy,
        batch_policy=batch_policy,
    )
    _enforce_batch_limit(
        len(selections),
        configured_maximum=batch_policy.max_documents,
        allow_large_batch=allow_large_batch,
    )
    outputs = _validated_outputs(
        root=root,
        paths=(
            documents_output,
            candidates_output,
            clusters_output,
            review_queue_output,
            run_output,
            registry_path,
        ),
    )
    (
        documents_target,
        candidates_target,
        clusters_target,
        review_target,
        run_target,
        registry_target,
    ) = outputs
    started = _utc_now(now)
    records = load_document_registry(
        registry_path=registry_target,
        allowed_root=root,
    )
    processed_documents: list[dict[str, object]] = []
    document_rows: list[dict[str, object]] = []
    errors: list[str] = []
    warnings: list[str] = []

    for index, selected in enumerate(selections):
        if index:
            sleep(policy.request_delay_seconds)
        try:
            download = download_discovered_pdf(
                document_id=selected.document_id,
                source_url=selected.pdf_url,
                storage_year=selected.year,
                policy=policy,
                raw_root=root / RAW_ROOT,
                existing_records=records,
                transport=transport,
                sleep=sleep,
                now=now,
            )
        except DownloadError as exc:
            message = _bounded_message(exc)
            errors.append(f"{selected.document_id}: {message}")
            document_rows.append(_failed_document_row(selected, error=message))
            continue

        warnings.extend(
            f"{selected.document_id}: {_bounded_message(warning)}"
            for warning in download.warnings
        )
        source = BatchProcessDocument(
            selection=selected,
            download=download,
            expected_site_terms=policy.expected_site_terms,
            expected_region_terms=policy.expected_region_terms,
        )
        try:
            document, extraction_warnings, extraction_error = process_document(
                source,
                policy=policy,
                root=root,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            extraction_error = _bounded_message(exc)
            document = source.to_dict()
            document.update(
                {
                    "page_count": 0,
                    "extraction_status": "error",
                    "ocr_required": False,
                    "classification": None,
                    "event_candidates": [],
                }
            )
            extraction_warnings = ()
        warnings.extend(
            f"{selected.document_id}: {_bounded_message(warning)}"
            for warning in extraction_warnings
        )
        if extraction_error:
            message = _bounded_message(extraction_error)
            errors.append(f"{selected.document_id}: {message}")
        records = upsert_document_registry_record(records, document)
        processed_documents.append(document)
        document_rows.append(
            _document_row(selected, document=document, error=extraction_error or "")
        )

    write_document_registry(
        records,
        registry_path=registry_target,
        allowed_root=root,
    )
    _write_csv(documents_target, BATCH_DOCUMENT_FIELDS, document_rows)
    original_candidates = (
        root
        / "data"
        / "interim"
        / "indeci"
        / f"{selections[0].batch_id}_event_candidates_original.csv"
    )
    try:
        write_original_candidates_from_payload(
            {"documents": processed_documents},
            output_path=original_candidates,
            allowed_root=root,
        )
        audit = execute_candidate_audit(
            original_candidates,
            config_path=quality_config,
            audit_output=candidates_target,
            consolidated_output=clusters_target,
            allowed_root=root,
        )
    except (CandidateAuditError, CandidateQualityError) as exc:
        raise BatchIngestionError(f"candidate audit failed: {exc}") from exc

    review_rows = _review_queue_rows(
        selections[0].batch_id,
        document_rows=document_rows,
        audited_candidates=audit["audited_candidates"],
    )
    _write_csv(review_target, REVIEW_QUEUE_FIELDS, review_rows)
    ocr_count = sum(row["ocr_required"] == "true" for row in document_rows)
    if ocr_count:
        warnings.append(
            f"ocr_not_run: {ocr_count} document(s) require separate OCR approval"
        )
    payload = _batch_manifest(
        started=started,
        finished=_utc_now(now),
        batch_id=selections[0].batch_id,
        selections=selections,
        document_rows=document_rows,
        audit=audit,
        review_rows=review_rows,
        errors=errors,
        warnings=warnings,
    )
    _write_json(run_target, payload)
    return payload


def format_batch_summary(payload: dict[str, object]) -> str:
    """Render the stable human-readable metrics requested for batch review."""
    years = payload.get("documents_by_year")
    if not isinstance(years, dict):
        raise BatchIngestionError("batch manifest documents_by_year is invalid")
    lines = [
        "Documents",
        "---------",
        f"selected: {_metric(payload, 'documents_selected')}",
        f"relevant: {_metric(payload, 'documents_relevant')}",
        f"possible: {_metric(payload, 'documents_possible')}",
        f"irrelevant: {_metric(payload, 'documents_irrelevant')}",
        f"excluded_geography: {_metric(payload, 'documents_excluded_geography')}",
        "",
        "Event candidates",
        "----------------",
        f"strong: {_metric(payload, 'strong_candidates')}",
        f"moderate: {_metric(payload, 'moderate_candidates')}",
        f"weak: {_metric(payload, 'weak_candidates')}",
        "",
        "Review",
        "------",
        f"pending: {_metric(payload, 'items_pending_review')}",
        f"ocr_required: {_metric(payload, 'ocr_required')}",
        "",
        "Years",
        "-----",
    ]
    for year in (2017, 2019, 2023, 2024):
        count = years.get(str(year), 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise BatchIngestionError("batch manifest contains an invalid year count")
        lines.append(f"{year}: {count}")
    return "\n".join(lines)


def load_batch_manifest(
    path: Path = BATCH_RUN_OUTPUT,
    *,
    allowed_root: Path,
) -> dict[str, object]:
    """Load a bounded local batch manifest for offline summary output."""
    source = _safe_input(path, root=allowed_root.resolve(), max_bytes=4 * 1024 * 1024)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BatchIngestionError("batch manifest must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise BatchIngestionError("batch manifest root must be an object")
    return payload


def _load_selected_rows(
    path: Path,
    *,
    root: Path,
    policy: IngestionPolicy,
    batch_policy: BatchPolicy,
) -> tuple[BatchSelection, ...]:
    source = _safe_input(path, root=root, max_bytes=MAX_SELECTION_BYTES)
    rows: list[BatchSelection] = []
    batch_ids: set[str] = set()
    try:
        with source.open(newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if tuple(reader.fieldnames or ()) != SELECTION_FIELDS:
                raise BatchIngestionError(
                    "batch selection CSV columns do not match the schema"
                )
            for row_number, raw in enumerate(reader, start=2):
                if None in raw:
                    raise BatchIngestionError(
                        f"batch selection row {row_number} has extra columns"
                    )
                if row_number > MAX_SELECTION_ROWS + 1:
                    raise BatchIngestionError("batch selection exceeds its row limit")
                restored = {
                    key: _restore_cell(value or "") for key, value in raw.items()
                }
                batch_ids.add(restored["batch_id"])
                if restored["selected"] == "false":
                    continue
                if restored["selected"] != "true":
                    raise BatchIngestionError(
                        f"batch selection row {row_number} has invalid selected value"
                    )
                rows.append(
                    _selected_row(
                        restored,
                        row_number=row_number,
                        policy=policy,
                        batch_policy=batch_policy,
                    )
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise BatchIngestionError("batch selection CSV could not be read") from exc
    if len(batch_ids) != 1 or not rows:
        raise BatchIngestionError(
            "batch selection must contain one batch_id and selected rows"
        )
    batch_id = batch_ids.pop()
    if re.fullmatch(r"indeci-batch-[0-9a-f]{16}", batch_id) is None:
        raise BatchIngestionError("batch_id has an invalid format")
    if any(row.batch_id != batch_id for row in rows):
        raise BatchIngestionError("selected rows do not share one batch_id")
    document_ids = [row.document_id for row in rows]
    urls = [row.pdf_url for row in rows]
    if len(set(document_ids)) != len(document_ids):
        raise BatchIngestionError("selected document_id values must be unique")
    if len(set(urls)) != len(urls):
        raise BatchIngestionError("selected PDF URLs must be unique")
    return tuple(rows)


def _selected_row(
    row: dict[str, str],
    *,
    row_number: int,
    policy: IngestionPolicy,
    batch_policy: BatchPolicy,
) -> BatchSelection:
    try:
        if re.fullmatch(r"indeci-batch-[0-9a-f]{16}", row["batch_id"]) is None:
            raise BatchIngestionError("batch_id has an invalid format")
        if re.fullmatch(r"[A-Z0-9_]{1,120}", row["document_id"]) is None:
            raise BatchIngestionError("document_id has an invalid format")
        if re.fullmatch(r"indeci-discovery-[0-9a-f]{20}", row["discovery_id"]) is None:
            raise BatchIngestionError("discovery_id has an invalid format")
        year = int(row["year"])
        if year not in batch_policy.allowed_years:
            raise BatchIngestionError("year is outside the pilot set")
        report_type = row["report_type"] or None
        if report_type is not None and report_type not in _REPORT_TYPES:
            raise BatchIngestionError("report_type is invalid")
        report_number = row["report_number"] or None
        if report_number is not None and not report_number.isdigit():
            raise BatchIngestionError("report_number is invalid")
        report_date = (
            date.fromisoformat(row["report_date"]) if row["report_date"] else None
        )
        if report_date is not None and report_date.year != year:
            raise BatchIngestionError("report_date and year do not match")
        title = _text(row["title"], "title", 500)
        connector = _text(row["source_connector"], "source_connector", 80)
        if re.fullmatch(r"[a-z][a-z0-9_]*", connector) is None:
            raise BatchIngestionError("source_connector is invalid")
        normalized_url, _ = validate_pdf_source_url(row["pdf_url"], policy=policy)
        if row["triage_tier"] not in {"A", "B", "C", "D"}:
            raise BatchIngestionError("triage_tier is invalid")
        triage_score = int(row["triage_score"])
        if not 0 <= triage_score <= 1_000:
            raise BatchIngestionError("triage_score is invalid")
        reasons = json.loads(row["triage_reasons"])
        if not isinstance(reasons, list) or not all(
            isinstance(reason, str) and 0 < len(reason) <= 200 for reason in reasons
        ):
            raise BatchIngestionError("triage_reasons is invalid")
        golden = row["golden_control"] or None
        if golden is not None and golden not in _GOLDEN_CONTROLS:
            raise BatchIngestionError("golden_control is invalid")
        detail_url = row["detail_url"] or None
        if detail_url is not None:
            _text(detail_url, "detail_url", 2_000)
        _text(row["selection_reason"], "selection_reason", 200)
    except (
        BatchIngestionError,
        IngestionConfigError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise BatchIngestionError(
            f"batch selection row {row_number} is invalid: {exc}"
        ) from exc
    return BatchSelection(
        batch_id=row["batch_id"],
        document_id=row["document_id"],
        discovery_id=row["discovery_id"],
        year=year,
        report_type=report_type,
        report_number=report_number,
        report_date=report_date,
        title=title,
        source_connector=connector,
        detail_url=detail_url,
        pdf_url=normalized_url,
        golden_control=golden,
    )


def _enforce_batch_limit(
    selected: int,
    *,
    configured_maximum: int,
    allow_large_batch: bool,
) -> None:
    if selected > ABSOLUTE_LARGE_BATCH_LIMIT:
        raise BatchIngestionError("batch exceeds the absolute safety limit")
    if selected > configured_maximum and not allow_large_batch:
        raise BatchIngestionError("selected rows exceed the configured maximum")


def _failed_document_row(
    selected: BatchSelection,
    *,
    error: str,
) -> dict[str, object]:
    return {
        "batch_id": selected.batch_id,
        "document_id": selected.document_id,
        "discovery_id": selected.discovery_id,
        "year": selected.year,
        "report_number": selected.report_number or "",
        "title": selected.title,
        "sha256": "",
        "page_count": 0,
        "download_status": "error",
        "extraction_status": "not_started",
        "relevance_status": "unclassified",
        "site_match": "false",
        "geographic_match": "false",
        "event_match": "false",
        "matched_terms": "[]",
        "review_required": "true",
        "ocr_required": "false",
        "download_success": "false",
        "extraction_success": "false",
        "classification_success": "false",
        "event_extraction_success": "false",
        "error": error,
    }


def _document_row(
    selected: BatchSelection,
    *,
    document: dict[str, object],
    error: str,
) -> dict[str, object]:
    classification = document.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    events = document.get("event_candidates")
    event_success = isinstance(events, list) and bool(classification)
    extraction_status = str(document.get("extraction_status", "error"))
    return {
        "batch_id": selected.batch_id,
        "document_id": selected.document_id,
        "discovery_id": selected.discovery_id,
        "year": selected.year,
        "report_number": selected.report_number or "",
        "title": selected.title,
        "sha256": document.get("sha256", ""),
        "page_count": document.get("page_count", 0),
        "download_status": document.get("download_status", "error"),
        "extraction_status": extraction_status,
        "relevance_status": classification.get("relevance_status", "unclassified"),
        "site_match": _bool_text(classification.get("site_match", False)),
        "geographic_match": _bool_text(classification.get("geographic_match", False)),
        "event_match": _bool_text(classification.get("event_match", False)),
        "matched_terms": json.dumps(
            classification.get("matched_terms", []),
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        "review_required": "true",
        "ocr_required": _bool_text(document.get("ocr_required", False)),
        "download_success": "true",
        "extraction_success": _bool_text(extraction_status == "success"),
        "classification_success": _bool_text(bool(classification)),
        "event_extraction_success": _bool_text(event_success),
        "error": error,
    }


def _review_queue_rows(
    batch_id: str,
    *,
    document_rows: list[dict[str, object]],
    audited_candidates: object,
) -> list[dict[str, object]]:
    if not isinstance(audited_candidates, list):
        raise BatchIngestionError("audited candidate payload is invalid")
    entries: set[tuple[str, str, str, str]] = set()
    for document in document_rows:
        document_id = str(document["document_id"])
        reasons = []
        if document["download_status"] == "duplicate":
            reasons.append("duplicate_possible")
        if document["ocr_required"] == "true":
            reasons.append("ocr_required")
        if document["error"] or document["relevance_status"] in {
            "relevant",
            "potentially_relevant",
        }:
            reasons.append("possible_document")
        for reason in reasons:
            entries.add(("document", document_id, "", reason))
    for candidate in audited_candidates:
        if not isinstance(candidate, dict):
            raise BatchIngestionError("audited candidate is invalid")
        document_id = str(candidate.get("source_document_id", ""))
        candidate_id = str(candidate.get("candidate_id", ""))
        strength = candidate.get("candidate_strength")
        if strength not in {"strong", "moderate", "weak"}:
            raise BatchIngestionError("audited candidate strength is invalid")
        entries.add(
            (
                "event_candidate",
                document_id,
                candidate_id,
                f"{strength}_event_candidate",
            )
        )
        if not candidate.get("event_date"):
            entries.add(
                ("event_candidate", document_id, candidate_id, "date_ambiguous")
            )
        if not candidate.get("canonical_site_id"):
            entries.add(
                ("event_candidate", document_id, candidate_id, "site_ambiguous")
            )
    rows = []
    for item_type, document_id, candidate_id, reason in sorted(entries):
        identity = "\x00".join((batch_id, item_type, document_id, candidate_id, reason))
        rows.append(
            {
                "batch_id": batch_id,
                "review_item_id": (
                    f"indeci-review-{sha256(identity.encode()).hexdigest()[:20]}"
                ),
                "item_type": item_type,
                "document_id": document_id,
                "candidate_id": candidate_id,
                "review_reason": reason,
                "validation_status": "pending_review",
            }
        )
    return rows


def _batch_manifest(
    *,
    started: datetime,
    finished: datetime,
    batch_id: str,
    selections: tuple[BatchSelection, ...],
    document_rows: list[dict[str, object]],
    audit: dict[str, object],
    review_rows: list[dict[str, object]],
    errors: list[str],
    warnings: list[str],
) -> dict[str, object]:
    status_counts = Counter(str(row["relevance_status"]) for row in document_rows)
    return {
        "run_id": f"indeci-batch-run-{started.strftime('%Y%m%dT%H%M%SZ')}",
        "batch_id": batch_id,
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "documents_selected": len(selections),
        "documents_downloaded": sum(
            row["download_status"] == "downloaded" for row in document_rows
        ),
        "documents_failed": sum(bool(row["error"]) for row in document_rows),
        "duplicates": sum(
            row["download_status"] == "duplicate" for row in document_rows
        ),
        "ocr_required": sum(row["ocr_required"] == "true" for row in document_rows),
        "documents_relevant": status_counts["relevant"],
        "documents_possible": status_counts["potentially_relevant"],
        "documents_irrelevant": status_counts["not_relevant"],
        "documents_excluded_geography": sum(
            row["classification_success"] == "true"
            and row["geographic_match"] == "false"
            for row in document_rows
        ),
        "event_candidates": audit["candidates_original"],
        "strong_candidates": audit["strong"],
        "moderate_candidates": audit["moderate"],
        "weak_candidates": audit["weak"],
        "event_clusters": audit["clusters_consolidated"],
        "items_pending_review": len(review_rows),
        "documents_by_year": dict(
            sorted(Counter(str(item.year) for item in selections).items())
        ),
        "errors": errors,
        "warnings": warnings,
    }


def _validated_outputs(*, root: Path, paths: tuple[Path, ...]) -> tuple[Path, ...]:
    outputs = tuple(_safe_output(path, root=root) for path in paths)
    if len(set(outputs)) != len(outputs):
        raise BatchIngestionError("batch output paths must be different")
    return outputs


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise BatchIngestionError("staging CSV path already exists")
    try:
        with part.open("x", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=fields,
                extrasaction="raise",
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {key: _protect_cell(str(row.get(key, ""))) for key in fields}
                )
            output.flush()
            os.fsync(output.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise BatchIngestionError("staging JSON path already exists")
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        with part.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _safe_input(path: Path, *, root: Path, max_bytes: int) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise BatchIngestionError("input symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise BatchIngestionError("input path is outside the workspace or missing")
    if candidate.stat().st_size > max_bytes:
        raise BatchIngestionError("input file exceeds its size limit")
    return candidate


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise BatchIngestionError("output symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root):
        raise BatchIngestionError("output path is outside the workspace")
    return candidate


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise BatchIngestionError(f"{field} must be a string")
    clean = re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
    if not clean or len(clean) > maximum:
        raise BatchIngestionError(f"{field} has an invalid length")
    return clean


def _bounded_message(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\x00", " ")).strip()[:500]


def _bool_text(value: object) -> str:
    return str(bool(value)).lower()


def _metric(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BatchIngestionError(f"batch manifest metric {name} is invalid")
    return value


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BatchIngestionError("batch timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _protect_cell(value: str) -> str:
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _restore_cell(value: str) -> str:
    if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
        return value[1:]
    return value
