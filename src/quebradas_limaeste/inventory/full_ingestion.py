"""Controlled ingestion of one fixed INDECI discovery snapshot."""

from __future__ import annotations

import csv
import json
import os
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.batch_ingestion import (
    BatchProcessDocument,
    BatchSelection,
)
from quebradas_limaeste.inventory.candidate_audit import (
    QUALITY_CONFIG,
    CandidateAuditError,
    execute_candidate_audit,
    write_original_candidates_from_payload,
)
from quebradas_limaeste.inventory.candidate_quality import CandidateQualityError
from quebradas_limaeste.inventory.discovery_models import canonical_document_url
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
    load_ingestion_policy,
)
from quebradas_limaeste.inventory.pdf_download import (
    DownloadError,
    PDFDownloadTransport,
    download_discovered_pdf,
)
from quebradas_limaeste.inventory.review_package import derive_review_fields
from quebradas_limaeste.inventory.triage import (
    SOURCE_CONFIG,
    BatchPolicyError,
    DiscoveryDocument,
    load_discovery_universe,
)

ALL_DOCUMENTS_OUTPUT = Path("metadata/indeci/all_documents.csv")
ALL_EVENT_CANDIDATES_OUTPUT = Path("metadata/indeci/all_event_candidates.csv")
ALL_EVENT_CLUSTERS_OUTPUT = Path("metadata/indeci/all_event_clusters.csv")
DUPLICATE_REVIEW_OUTPUT = Path("metadata/indeci/duplicate_review.csv")
FULL_RUN_OUTPUT = Path("metadata/indeci/full_ingestion_run.json")
ORIGINAL_CANDIDATES_OUTPUT = Path(
    "data/interim/indeci/full_event_candidates_original.csv"
)
EXPECTED_DISCOVERY_DOCUMENTS = 131
MAX_ERROR_LENGTH = 500
ALL_DOCUMENT_FIELDS = (
    "document_id",
    "discovery_id",
    "year",
    "title",
    "report_type",
    "report_number",
    "report_date",
    "source_connector",
    "source_url",
    "pdf_url",
    "raw_local_path",
    "original_filename",
    "sha256",
    "file_size",
    "page_count",
    "downloaded_at_utc",
    "download_status",
    "extraction_status",
    "ocr_required",
    "relevance_status",
    "site_match",
    "geographic_match",
    "event_match",
    "matched_terms",
    "review_required",
    "golden_control",
    "review_priority",
    "review_location",
    "review_event",
    "review_event_date",
    "review_report_date",
    "review_report_type",
    "review_report_number",
    "candidate_strength_max",
    "relevant_pages",
    "event_candidate_count",
    "event_cluster_count",
    "error",
)
DUPLICATE_FIELDS = (
    "duplicate_group_id",
    "duplicate_signal",
    "duplicate_key",
    "document_count",
    "document_ids",
    "raw_local_paths",
    "review_status",
)


class FullIngestionError(RuntimeError):
    """Raised when full ingestion state violates its bounded contract."""


def execute_full_ingestion(
    discovery_path: Path,
    *,
    config_path: Path = SOURCE_CONFIG,
    quality_config_path: Path = QUALITY_CONFIG,
    documents_output: Path = ALL_DOCUMENTS_OUTPUT,
    candidates_output: Path = ALL_EVENT_CANDIDATES_OUTPUT,
    clusters_output: Path = ALL_EVENT_CLUSTERS_OUTPUT,
    duplicates_output: Path = DUPLICATE_REVIEW_OUTPUT,
    run_output: Path = FULL_RUN_OUTPUT,
    registry_path: Path = DOCUMENT_REGISTRY,
    original_candidates_output: Path = ORIGINAL_CANDIDATES_OUTPUT,
    allowed_root: Path,
    expected_documents: int = EXPECTED_DISCOVERY_DOCUMENTS,
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Process every row in the approved snapshot while isolating document errors."""
    root = Path(allowed_root).resolve()
    config = _safe_input(config_path, root=root, maximum=65_536)
    quality_config = _safe_input(quality_config_path, root=root, maximum=65_536)
    try:
        policy = load_ingestion_policy(config)
        universe = load_discovery_universe(
            discovery_path,
            config_path=config,
            allowed_root=root,
        )
    except (IngestionConfigError, BatchPolicyError) as exc:
        raise FullIngestionError(str(exc)) from exc
    if len(universe) != expected_documents:
        raise FullIngestionError(
            f"discovery snapshot expected {expected_documents} documents; "
            f"found {len(universe)}"
        )
    outputs = tuple(
        _safe_output(path, root=root)
        for path in (
            documents_output,
            candidates_output,
            clusters_output,
            duplicates_output,
            run_output,
            registry_path,
            original_candidates_output,
        )
    )
    if len(set(outputs)) != len(outputs):
        raise FullIngestionError("full-ingestion outputs must use different paths")
    (
        documents_target,
        candidates_target,
        clusters_target,
        duplicates_target,
        run_target,
        registry_target,
        original_target,
    ) = outputs
    started = _utc_now(now)
    records = load_document_registry(
        registry_path=registry_target,
        allowed_root=root,
    )
    initially_available = {
        item.document_id
        for item in universe
        if _is_available(item, records=records, root=root)
    }
    processed: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    warnings: list[str] = []
    network_attempts = 0

    for item in universe:
        if item.document_id not in initially_available:
            if network_attempts:
                sleep(policy.request_delay_seconds)
            network_attempts += 1
        try:
            selected = _batch_selection(item)
            download = download_discovered_pdf(
                document_id=item.document_id,
                source_url=item.candidate.pdf_url or "",
                storage_year=item.candidate.year,
                policy=policy,
                raw_root=root / RAW_ROOT,
                existing_records=records,
                transport=transport,
                sleep=sleep,
                now=now,
                preserve_original_filename=True,
            )
        except (DownloadError, FullIngestionError) as exc:
            message = _bounded_message(exc)
            errors.append(f"{item.document_id}: {message}")
            rows.append(_document_row(item, error=message))
            continue
        warnings.extend(
            f"{item.document_id}: {_bounded_message(warning)}"
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
            f"{item.document_id}: {_bounded_message(warning)}"
            for warning in extraction_warnings
        )
        message = _bounded_message(extraction_error) if extraction_error else ""
        if message:
            errors.append(f"{item.document_id}: {message}")
        records = upsert_document_registry_record(records, document)
        processed.append(document)
        rows.append(_document_row(item, document=document, error=message))

    write_document_registry(
        records,
        registry_path=registry_target,
        allowed_root=root,
    )
    try:
        write_original_candidates_from_payload(
            {"documents": processed},
            output_path=original_target,
            allowed_root=root,
        )
        audit = execute_candidate_audit(
            original_target,
            config_path=quality_config,
            audit_output=candidates_target,
            consolidated_output=clusters_target,
            allowed_root=root,
        )
    except (CandidateAuditError, CandidateQualityError) as exc:
        raise FullIngestionError(str(exc)) from exc
    candidate_rows = _read_csv(candidates_target)
    cluster_rows = _read_csv(clusters_target)
    _require_pending_review(candidate_rows, source="candidate")
    _require_pending_review(cluster_rows, source="cluster")
    _add_review_metadata(rows, candidate_rows=candidate_rows, cluster_rows=cluster_rows)
    duplicates = duplicate_review_rows(rows)
    _write_csv(documents_target, ALL_DOCUMENT_FIELDS, rows)
    _write_csv(duplicates_target, DUPLICATE_FIELDS, duplicates)
    finished = _utc_now(now)
    payload = _manifest(
        started=started,
        finished=finished,
        universe=universe,
        rows=rows,
        audit=audit,
        initially_available=initially_available,
        duplicates=duplicates,
        errors=errors,
        warnings=warnings,
        outputs={
            "documents": documents_target,
            "candidates": candidates_target,
            "clusters": clusters_target,
            "duplicate_review": duplicates_target,
        },
        root=root,
    )
    _write_json(run_target, payload)
    return payload


def duplicate_review_rows(
    documents: Iterable[dict[str, object]],
) -> list[dict[str, object]]:
    """Return every non-destructive duplicate signal requiring human review."""
    rows = tuple(documents)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        digest = str(row.get("sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            groups[("sha256", digest)].append(row)
        url = str(row.get("pdf_url") or "")
        if url:
            groups[("canonical_url", canonical_document_url(url))].append(row)
        identity = tuple(
            str(row.get(field) or "")
            for field in ("report_type", "report_number", "report_date")
        )
        if all(identity):
            groups[("report_identity", "|".join(identity))].append(row)
    result = []
    for (signal, key), members in sorted(groups.items()):
        document_ids = sorted({str(row["document_id"]) for row in members})
        if len(document_ids) < 2:
            continue
        paths = sorted(
            {
                str(row.get("raw_local_path") or "")
                for row in members
                if row.get("raw_local_path")
            }
        )
        group_identity = f"{signal}\x00{key}"
        result.append(
            {
                "duplicate_group_id": (
                    "indeci-duplicate-"
                    f"{sha256(group_identity.encode()).hexdigest()[:20]}"
                ),
                "duplicate_signal": signal,
                "duplicate_key": key,
                "document_count": len(document_ids),
                "document_ids": "|".join(document_ids),
                "raw_local_paths": "|".join(paths),
                "review_status": "pending_review",
            }
        )
    return result


def _batch_selection(item: DiscoveryDocument) -> BatchSelection:
    candidate = item.candidate
    if candidate.pdf_url is None:
        raise FullIngestionError(f"{item.document_id} has no PDF URL")
    return BatchSelection(
        batch_id="indeci-full-discovery",
        document_id=item.document_id,
        discovery_id=item.discovery_id,
        year=candidate.year,
        report_type=candidate.report_type,
        report_number=candidate.report_number,
        report_date=candidate.report_date,
        title=candidate.title,
        source_connector=candidate.source_connector,
        detail_url=candidate.detail_url,
        pdf_url=candidate.pdf_url,
        golden_control=item.golden_control,
    )


def _document_row(
    item: DiscoveryDocument,
    *,
    document: dict[str, object] | None = None,
    error: str,
) -> dict[str, object]:
    candidate = item.candidate
    processed = document or {}
    classification = processed.get("classification")
    if not isinstance(classification, dict):
        classification = {}
    return {
        "document_id": item.document_id,
        "discovery_id": item.discovery_id,
        "year": candidate.year,
        "title": candidate.title,
        "report_type": candidate.report_type or "",
        "report_number": candidate.report_number or "",
        "report_date": (
            candidate.report_date.isoformat() if candidate.report_date else ""
        ),
        "source_connector": candidate.source_connector,
        "source_url": candidate.detail_url or candidate.pdf_url or "",
        "pdf_url": candidate.pdf_url or "",
        "raw_local_path": processed.get("local_path", ""),
        "original_filename": processed.get("original_filename", ""),
        "sha256": processed.get("sha256", ""),
        "file_size": processed.get("file_size", 0),
        "page_count": processed.get("page_count", 0),
        "downloaded_at_utc": processed.get("downloaded_at_utc", ""),
        "download_status": processed.get("download_status", "error"),
        "extraction_status": processed.get("extraction_status", "not_started"),
        "ocr_required": _bool_text(processed.get("ocr_required", False)),
        "relevance_status": classification.get("relevance_status", "unclassified"),
        "site_match": _bool_text(classification.get("site_match", False)),
        "geographic_match": _bool_text(
            classification.get("geographic_match", False)
        ),
        "event_match": _bool_text(classification.get("event_match", False)),
        "matched_terms": json.dumps(
            classification.get("matched_terms", []),
            ensure_ascii=True,
            separators=(",", ":"),
        ),
        "review_required": "true",
        "golden_control": item.golden_control or "",
        "review_priority": "",
        "review_location": "",
        "review_event": "",
        "review_event_date": "",
        "review_report_date": "",
        "review_report_type": "",
        "review_report_number": "",
        "candidate_strength_max": "",
        "relevant_pages": "",
        "event_candidate_count": 0,
        "event_cluster_count": 0,
        "error": error,
    }


def _add_review_metadata(
    documents: list[dict[str, object]],
    *,
    candidate_rows: list[dict[str, str]],
    cluster_rows: list[dict[str, str]],
) -> None:
    candidates_by_document: dict[str, list[dict[str, str]]] = defaultdict(list)
    candidate_document: dict[str, str] = {}
    for row in candidate_rows:
        document_id = row["source_document_id"]
        candidates_by_document[document_id].append(row)
        candidate_document[row["candidate_id"]] = document_id
    clusters_by_document: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in cluster_rows:
        document_ids = {
            candidate_document[candidate_id]
            for candidate_id in row["supporting_candidates"].split("|")
            if candidate_id in candidate_document
        }
        for document_id in document_ids:
            clusters_by_document[document_id].append(row)
    for document in documents:
        document_id = str(document["document_id"])
        document.update(
            derive_review_fields(
                document,
                candidates=candidates_by_document[document_id],
                clusters=clusters_by_document[document_id],
            )
        )


def _is_available(
    item: DiscoveryDocument,
    *,
    records: list[dict[str, object]],
    root: Path,
) -> bool:
    pdf_url = item.candidate.pdf_url or ""
    for record in records:
        if (
            record.get("document_id") == item.document_id
            and record.get("source_url") == pdf_url
        ):
            local = Path(str(record.get("local_path") or ""))
            path = local if local.is_absolute() else root / local
            return path.is_file() and not path.is_symlink()
    canonical = root / RAW_ROOT / str(item.candidate.year) / f"{item.document_id}.pdf"
    return canonical.is_file() and not canonical.is_symlink()


def _manifest(
    *,
    started: datetime,
    finished: datetime,
    universe: tuple[DiscoveryDocument, ...],
    rows: list[dict[str, object]],
    audit: dict[str, object],
    initially_available: set[str],
    duplicates: list[dict[str, object]],
    errors: list[str],
    warnings: list[str],
    outputs: dict[str, Path],
    root: Path,
) -> dict[str, object]:
    relevance = Counter(str(row["relevance_status"]) for row in rows)
    priorities = Counter(str(row["review_priority"]) for row in rows)
    failed_ids = {str(error).split(":", maxsplit=1)[0] for error in errors}
    goldens = sorted(
        str(row["golden_control"]) for row in rows if row["golden_control"]
    )
    return {
        "run_id": f"indeci-full-{started.strftime('%Y%m%dT%H%M%SZ')}",
        "started_at_utc": started.isoformat(),
        "finished_at_utc": finished.isoformat(),
        "total_universe": len(universe),
        "already_available": len(initially_available),
        "newly_downloaded": sum(
            row["download_status"] == "downloaded" for row in rows
        ),
        "failed": len(failed_ids),
        "download_failed": sum(row["download_status"] == "error" for row in rows),
        "ocr_required": sum(row["ocr_required"] == "true" for row in rows),
        "duplicate_sha": sum(
            row["duplicate_signal"] == "sha256" for row in duplicates
        ),
        "documents_relevant": relevance["relevant"],
        "documents_possible": relevance["potentially_relevant"],
        "documents_irrelevant": relevance["not_relevant"],
        "documents_unclassified": relevance["unclassified"],
        "documents_excluded_geography": sum(
            row["relevance_status"] != "unclassified"
            and row["geographic_match"] == "false"
            for row in rows
        ),
        "priority_p1": priorities["P1"],
        "priority_p2": priorities["P2"],
        "priority_p3": priorities["P3"],
        "priority_p4": priorities["P4"],
        "event_candidates": audit["candidates_original"],
        "strong_candidates": audit["strong"],
        "moderate_candidates": audit["moderate"],
        "weak_candidates": audit["weak"],
        "event_clusters": audit["clusters_consolidated"],
        "documents_by_year": dict(
            sorted(Counter(str(item.candidate.year) for item in universe).items())
        ),
        "golden_controls": goldens,
        "outputs": {
            name: str(path.relative_to(root)) for name, path in outputs.items()
        },
        "errors": errors,
        "warnings": warnings,
    }


def _require_pending_review(rows: list[dict[str, str]], *, source: str) -> None:
    if any(row.get("validation_status") != "pending_review" for row in rows):
        raise FullIngestionError(f"{source} output contains a non-pending decision")


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as source:
            return [dict(row) for row in csv.DictReader(source)]
    except (OSError, UnicodeError, csv.Error) as exc:
        raise FullIngestionError("generated CSV could not be read") from exc


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: Iterable[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise FullIngestionError("staging CSV path already exists")
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
                    {field: _protect_cell(str(row.get(field, ""))) for field in fields}
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
        raise FullIngestionError("staging JSON path already exists")
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        with part.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def _safe_input(path: Path, *, root: Path, maximum: int) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise FullIngestionError("input symlinks are not allowed")
    target = unresolved.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise FullIngestionError("input path is outside the workspace or missing")
    if target.stat().st_size > maximum:
        raise FullIngestionError("input exceeds its size limit")
    return target


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise FullIngestionError("output symlinks are not allowed")
    target = unresolved.resolve()
    if not target.is_relative_to(root):
        raise FullIngestionError("output path is outside the workspace")
    return target


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FullIngestionError("full-ingestion timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_message(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\x00", " ")).strip()[
        :MAX_ERROR_LENGTH
    ]


def _bool_text(value: object) -> str:
    return str(bool(value)).lower()


def _protect_cell(value: str) -> str:
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value
