"""Controlled historical download selection and inventory projections."""

from __future__ import annotations

import csv
import json
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.batch_ingestion import (
    BatchProcessDocument,
    BatchSelection,
)
from quebradas_limaeste.inventory.candidate_quality import (
    OriginalCandidate,
    audit_candidate,
    load_quality_policy,
)
from quebradas_limaeste.inventory.discovery_models import canonical_document_url
from quebradas_limaeste.inventory.document_classification import semantic_text
from quebradas_limaeste.inventory.historical_discovery import (
    HISTORICAL_CONFIG,
    HISTORICAL_DISCOVERY_FIELDS,
    HISTORICAL_DISCOVERY_OUTPUT,
    HISTORICAL_ROOT,
    HistoricalConfig,
    load_historical_config,
)
from quebradas_limaeste.inventory.ingestion import (
    DOCUMENT_REGISTRY,
    load_document_registry,
    process_document,
    upsert_document_registry_record,
    write_document_registry,
)
from quebradas_limaeste.inventory.ingestion_models import load_ingestion_policy
from quebradas_limaeste.inventory.limaeste_filter import (
    FilterEvidence,
    FilterPolicy,
    evaluate_relevance,
    load_filter_policy,
)
from quebradas_limaeste.inventory.pdf_download import (
    PDFDownloadTransport,
    download_discovered_pdf,
)

HISTORICAL_DOCUMENT_FIELDS = (
    "document_id",
    "document_family_id",
    "year",
    "report_type",
    "report_number",
    "report_date",
    "event_date",
    "title",
    "source_url",
    "pdf_url",
    "sha256",
    "page_count",
    "spatial_relevance",
    "event_relevance",
    "rainfall_related",
    "review_priority",
    "priority_reason",
    "human_validation_status",
    "duplicate_status",
    "duplicate_reason",
    "canonical_document_id",
    "download_status",
    "extraction_status",
    "ocr_required",
)
HISTORICAL_CANDIDATE_FIELDS = (
    "candidate_id",
    "event_cluster_id",
    "document_id",
    "document_family_id",
    "event_date",
    "event_time",
    "event_type",
    "reported_quebrada",
    "district",
    "source_page",
    "evidence_snippet",
    "candidate_strength",
    "spatial_relevance",
    "review_priority",
    "validation_status",
)
HISTORICAL_CLUSTER_FIELDS = (
    "event_cluster_id",
    "document_family_id",
    "event_date",
    "event_time",
    "event_type",
    "reported_quebrada",
    "candidate_strength",
    "supporting_candidates",
    "supporting_documents",
    "validation_status",
)
YEAR_SUMMARY_FIELDS = (
    "year",
    "documents_discovered",
    "documents_unique",
    "P1",
    "P2",
    "P3",
    "P4",
    "PX",
    "documents_downloaded",
    "event_candidates",
    "event_clusters",
    "strong",
    "moderate",
    "weak",
    "errors",
)
LOCATION_SUMMARY_FIELDS = ("location", "documents", "candidate_events", "years_present")
HISTORICAL_DOCUMENTS_OUTPUT = HISTORICAL_ROOT / "all_documents.csv"
HISTORICAL_CANDIDATES_OUTPUT = HISTORICAL_ROOT / "event_candidates.csv"
HISTORICAL_CLUSTERS_OUTPUT = HISTORICAL_ROOT / "event_clusters.csv"
HISTORICAL_YEAR_SUMMARY_OUTPUT = HISTORICAL_ROOT / "year_summary.csv"
HISTORICAL_LOCATION_SUMMARY_OUTPUT = HISTORICAL_ROOT / "location_summary.csv"
HISTORICAL_RUN_OUTPUT = HISTORICAL_ROOT / "historical_run.json"
HISTORICAL_RUN_CHECKPOINT_ROOT = HISTORICAL_ROOT / "run_checkpoints"
_LOCATION_SUMMARY = (
    "CUSIPATA",
    "SAN-BARTOLOME",
    "CHACLACAYO",
    "LURIGANCHO-CHOSICA",
    "QUIRIO",
    "PEDREGAL",
    "HUASCARAN",
    "HUAYCOLORO",
    "JICAMARCA",
    "CIENEGUILLA",
    "otras",
)
_PRIORITIES = ("P1", "P2", "P3", "P4", "PX")
_DOCUMENT_ID = re.compile(r"[A-Z0-9_]{1,120}\Z")
_FAMILY_ID = re.compile(r"indeci-family-[a-z0-9-]{1,120}\Z")


@dataclass(frozen=True)
class HistoricalPrefilter:
    spatial_relevance: str
    event_relevance: str
    priority: str
    reason: str
    metadata_insufficient: bool


@dataclass(frozen=True)
class HistoricalCandidate:
    candidate_id: str
    document_id: str
    document_family_id: str
    event_date: str
    event_time: str
    event_type: str
    reported_quebrada: str
    candidate_strength: str
    source_page: int = 0
    evidence_snippet: str = ""
    matched_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class HistoricalProcessedDocument:
    document_id: str
    sha256: str
    page_count: int
    download_status: str
    extraction_status: str
    ocr_required: bool
    matched_terms: tuple[str, ...]
    full_text: str
    candidates: tuple[HistoricalCandidate, ...]
    warnings: tuple[str, ...] = ()
    error: str = ""


def historical_prefilter(
    title: str, *, policy: FilterPolicy | None
) -> HistoricalPrefilter:
    """Apply frozen filter rules to title metadata only, without excluding rows."""
    if policy is None:
        return HistoricalPrefilter(
            spatial_relevance="unknown",
            event_relevance="unknown",
            priority="PX",
            reason="filter policy unavailable during injected discovery test",
            metadata_insufficient=True,
        )
    decision = evaluate_relevance(FilterEvidence(title=title), policy=policy)
    normalized = semantic_text(title)
    metadata_insufficient = (
        decision.review_priority == "PX"
        and decision.event_relevance in {"target", "possible_target"}
        and "lima" in normalized.split()
    )
    return HistoricalPrefilter(
        spatial_relevance=decision.spatial_relevance,
        event_relevance=decision.event_relevance,
        priority=decision.review_priority,
        reason=decision.priority_reason,
        metadata_insufficient=metadata_insufficient,
    )


def select_px_control_sample(
    document_ids: tuple[str, ...], *, seed: str, sample_size: int
) -> tuple[str, ...]:
    """Choose a stable PX sample without mutating or filtering the universe."""
    if not seed or sample_size < 0:
        raise ValueError("PX sample policy is invalid")
    unique = tuple(sorted(set(document_ids)))
    ordered = sorted(
        unique,
        key=lambda item: (sha256(f"{seed}\x00{item}".encode()).hexdigest(), item),
    )
    return tuple(sorted(ordered[:sample_size]))


def build_document_row(
    *,
    document_id: str,
    document_family_id: str,
    year: int,
    title: str,
    report_type: str,
    report_number: str,
    report_date: str,
    source_url: str,
    pdf_url: str,
    prefilter: HistoricalPrefilter,
    event_date: str = "",
    sha256_digest: str = "",
    page_count: int = 0,
    spatial_relevance: str | None = None,
    event_relevance: str | None = None,
    rainfall_related: str = "unknown",
    review_priority: str | None = None,
    priority_reason: str | None = None,
    download_status: str = "not_selected",
    extraction_status: str = "not_started",
    ocr_required: bool = False,
    duplicate_status: str = "unique",
    duplicate_reason: str = "no_duplicate_signal",
    canonical_document_id: str | None = None,
) -> dict[str, object]:
    """Project one document without conflating report and event dates."""
    return {
        "document_id": document_id,
        "document_family_id": document_family_id,
        "year": year,
        "report_type": report_type,
        "report_number": report_number,
        "report_date": report_date,
        "event_date": event_date,
        "title": title,
        "source_url": source_url,
        "pdf_url": pdf_url,
        "sha256": sha256_digest,
        "page_count": page_count,
        "spatial_relevance": spatial_relevance or prefilter.spatial_relevance,
        "event_relevance": event_relevance or prefilter.event_relevance,
        "rainfall_related": rainfall_related,
        "review_priority": review_priority or prefilter.priority,
        "priority_reason": priority_reason or prefilter.reason,
        "human_validation_status": "pending_review",
        "duplicate_status": duplicate_status,
        "duplicate_reason": duplicate_reason,
        "canonical_document_id": canonical_document_id or document_id,
        "download_status": download_status,
        "extraction_status": extraction_status,
        "ocr_required": str(ocr_required).lower(),
    }


def assign_historical_event_clusters(
    candidates: tuple[HistoricalCandidate, ...],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Cluster compatible evidence within a documentary family."""
    groups: dict[tuple[str, ...], list[HistoricalCandidate]] = {}
    for candidate in candidates:
        if (
            candidate.event_date
            and candidate.event_type
            and candidate.reported_quebrada
        ):
            key = (
                candidate.document_family_id,
                candidate.event_date,
                candidate.event_time,
                semantic_text(candidate.reported_quebrada),
                candidate.event_type,
            )
        else:
            key = (candidate.document_family_id, candidate.candidate_id)
        groups.setdefault(key, []).append(candidate)

    assignments: dict[str, str] = {}
    clusters: list[dict[str, object]] = []
    rank = {"weak": 0, "moderate": 1, "strong": 2}
    for key, members in sorted(groups.items()):
        cluster_id = (
            f"indeci-event-{sha256(chr(0).join(key).encode()).hexdigest()[:20]}"
        )
        best = min(
            members,
            key=lambda item: (
                -rank.get(item.candidate_strength, -1),
                item.candidate_id,
            ),
        )
        for member in members:
            assignments[member.candidate_id] = cluster_id
        clusters.append(
            {
                "event_cluster_id": cluster_id,
                "document_family_id": best.document_family_id,
                "event_date": best.event_date,
                "event_time": best.event_time,
                "event_type": best.event_type,
                "reported_quebrada": best.reported_quebrada,
                "candidate_strength": best.candidate_strength,
                "supporting_candidates": "|".join(
                    sorted(member.candidate_id for member in members)
                ),
                "supporting_documents": "|".join(
                    sorted({member.document_id for member in members})
                ),
                "validation_status": "pending_review",
            }
        )
    return assignments, clusters


def execute_historical_inventory(
    config_path: Path = HISTORICAL_CONFIG,
    *,
    config: HistoricalConfig | None = None,
    discovery_path: Path = HISTORICAL_DISCOVERY_OUTPUT,
    documents_output: Path = HISTORICAL_DOCUMENTS_OUTPUT,
    candidates_output: Path = HISTORICAL_CANDIDATES_OUTPUT,
    clusters_output: Path = HISTORICAL_CLUSTERS_OUTPUT,
    year_summary_output: Path = HISTORICAL_YEAR_SUMMARY_OUTPUT,
    location_summary_output: Path = HISTORICAL_LOCATION_SUMMARY_OUTPUT,
    run_output: Path = HISTORICAL_RUN_OUTPUT,
    checkpoint_root: Path = HISTORICAL_RUN_CHECKPOINT_ROOT,
    registry_path: Path = DOCUMENT_REGISTRY,
    allowed_root: Path,
    processor: Callable[[dict[str, str]], HistoricalProcessedDocument] | None = None,
    transport: PDFDownloadTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Download selected historical rows and publish review-only inventories."""
    root = Path(allowed_root).resolve()
    policy = config or load_historical_config(config_path, allowed_root=root)
    outputs = tuple(
        _safe_output(path, root=root)
        for path in (
            documents_output,
            candidates_output,
            clusters_output,
            year_summary_output,
            location_summary_output,
            run_output,
            checkpoint_root,
        )
    )
    if len(set(outputs)) != len(outputs):
        raise RuntimeError("historical outputs must use distinct paths")
    (
        documents_target,
        candidates_target,
        clusters_target,
        year_target,
        location_target,
        run_target,
        checkpoints,
    ) = outputs
    reused = _completed_run(run_target, documents_target, policy=policy)
    if reused is not None:
        reused["resume_status"] = "complete_output_reused"
        return reused

    rows = _read_discovery(discovery_path, root=root, years=policy.years)
    discovery_stats = _read_discovery_manifest(
        Path(discovery_path), root=root, policy=policy
    )
    filter_policy = load_filter_policy(policy.filter_config, allowed_root=root)
    prefilters = {
        row["document_id"]: historical_prefilter(row["title"], policy=filter_policy)
        for row in rows
    }
    _verify_prefilters(rows, prefilters=prefilters)
    eligible_px = tuple(
        row["document_id"]
        for row in rows
        if prefilters[row["document_id"]].priority == "PX" and row["pdf_url"]
    )
    px_sample = set(
        select_px_control_sample(
            eligible_px,
            seed=policy.px_seed,
            sample_size=policy.px_sample_size,
        )
    )
    selection_reason = {
        row["document_id"]: (
            f"priority_{prefilters[row['document_id']].priority}"
            if prefilters[row["document_id"]].priority != "PX"
            else (
                "px_metadata_insufficient_sample"
                if prefilters[row["document_id"]].metadata_insufficient
                else "px_control_sample"
            )
        )
        for row in rows
        if row["pdf_url"]
        and (
            prefilters[row["document_id"]].priority != "PX"
            or row["document_id"] in px_sample
        )
    }

    default_processor = None
    if processor is None:
        default_processor = _DefaultProcessor(
            config=policy,
            root=root,
            registry_path=registry_path,
            transport=transport,
            sleep=sleep,
            now=now,
        )
        active_processor = default_processor
    else:
        active_processor = processor

    processed: dict[str, HistoricalProcessedDocument] = {}
    errors: list[str] = []
    warnings: list[str] = []
    px_false_negatives = 0
    px_processed: set[str] = set()
    stop_px = False
    for year in policy.years:
        year_rows = sorted(
            (row for row in rows if int(row["year"]) == year),
            key=lambda row: (
                _priority_rank(prefilters[row["document_id"]].priority),
                row["document_id"],
            ),
        )
        checkpoint = checkpoints / f"{year}.json"
        restored = _load_run_checkpoint(
            checkpoint,
            year=year,
            policy=policy,
            root=root,
        )
        if restored is not None:
            for item in restored:
                result = _processed_from_dict(item)
                processed[result.document_id] = result
                warnings.extend(
                    f"{result.document_id}: {warning}" for warning in result.warnings
                )
                if result.error:
                    errors.append(f"{result.document_id}: {result.error}")
                if prefilters[result.document_id].priority == "PX":
                    px_processed.add(result.document_id)
                    source_row = next(
                        row
                        for row in year_rows
                        if row["document_id"] == result.document_id
                    )
                    final = historical_final_decision(
                        source_row["title"],
                        result=result,
                        policy=filter_policy,
                    )
                    if final.review_priority != "PX":
                        px_false_negatives += 1
                        stop_px = True
            continue
        year_processed: list[HistoricalProcessedDocument] = []
        for row in year_rows:
            document_id = row["document_id"]
            if document_id not in selection_reason:
                continue
            is_px = prefilters[document_id].priority == "PX"
            if is_px and stop_px:
                continue
            try:
                result = active_processor(row)
                if result.document_id != document_id:
                    raise RuntimeError("processor returned a different document_id")
            except Exception as exc:
                message = _bounded_message(exc)
                result = HistoricalProcessedDocument(
                    document_id=document_id,
                    sha256="",
                    page_count=0,
                    download_status="failed",
                    extraction_status="not_started",
                    ocr_required=False,
                    matched_terms=(),
                    full_text="",
                    candidates=(),
                    error=message,
                )
            processed[document_id] = result
            year_processed.append(result)
            warnings.extend(f"{document_id}: {item}" for item in result.warnings)
            if result.error:
                errors.append(f"{document_id}: {result.error}")
            if is_px:
                px_processed.add(document_id)
                final = historical_final_decision(
                    row["title"], result=result, policy=filter_policy
                )
                if final.review_priority != "PX":
                    px_false_negatives += 1
                    stop_px = True
                    warnings.append(
                        f"PX false negative detected in {document_id}; "
                        "remaining PX expansion stopped"
                    )
        _write_json(
            checkpoint,
            {
                "schema_version": 1,
                "config_sha256": policy.config_sha256,
                "year": year,
                "documents": [_processed_to_dict(item) for item in year_processed],
            },
        )
    if default_processor is not None:
        default_processor.persist_registry()

    sha_duplicates = apply_sha256_deduplication(rows, processed=processed)
    family_by_document = {row["document_id"]: row["document_family_id"] for row in rows}
    all_candidates = tuple(
        replace(
            candidate,
            document_family_id=family_by_document[candidate.document_id],
        )
        for result in processed.values()
        for candidate in result.candidates
    )
    assignments, cluster_rows = assign_historical_event_clusters(all_candidates)
    candidate_rows = _candidate_rows(
        all_candidates,
        assignments=assignments,
        rows=rows,
        processed=processed,
        policy=filter_policy,
    )
    document_rows, locations_by_document = _document_rows(
        rows,
        processed=processed,
        prefilters=prefilters,
        policy=filter_policy,
    )
    _write_csv(documents_target, HISTORICAL_DOCUMENT_FIELDS, document_rows)
    _write_csv(candidates_target, HISTORICAL_CANDIDATE_FIELDS, candidate_rows)
    _write_csv(clusters_target, HISTORICAL_CLUSTER_FIELDS, cluster_rows)
    year_rows = _year_summary(
        years=policy.years,
        discovery_rows=rows,
        documents=document_rows,
        candidates=candidate_rows,
        clusters=cluster_rows,
        errors=errors,
        discovery_stats=discovery_stats,
    )
    _write_csv(year_target, YEAR_SUMMARY_FIELDS, year_rows)
    location_rows = _location_summary(
        documents=document_rows,
        candidates=candidate_rows,
        locations_by_document=locations_by_document,
    )
    _write_csv(location_target, LOCATION_SUMMARY_FIELDS, location_rows)
    priorities = Counter(str(row["review_priority"]) for row in document_rows)
    strengths = Counter(str(row["candidate_strength"]) for row in candidate_rows)
    manifest = {
        "run_id": f"indeci-historical-{_utc_now(now).strftime('%Y%m%dT%H%M%SZ')}",
        "schema_version": 1,
        "config_sha256": policy.config_sha256,
        "period_start": policy.years[0],
        "period_end": policy.years[-1],
        "years_processed": list(policy.years),
        "connectors": discovery_stats.get("connectors")
        or sorted(
            {
                connector
                for row in rows
                for connector in json.loads(row["discovery_sources"])
            }
        ),
        "requests": int(discovery_stats.get("requests", 0)),
        "pages": int(discovery_stats.get("pages", 0)),
        "documents_raw": int(discovery_stats.get("documents_raw", len(rows))),
        "documents_unique": len(rows) - sha_duplicates,
        "duplicates": (int(discovery_stats.get("duplicates", 0)) + sha_duplicates),
        "downloads": sum(
            row["download_status"] in {"downloaded", "reused"} for row in document_rows
        ),
        "download_failures": sum(
            row["download_status"] == "failed" for row in document_rows
        ),
        "ocr_required": sum(row["ocr_required"] == "true" for row in document_rows),
        **{priority: priorities[priority] for priority in _PRIORITIES},
        "event_candidates": len(candidate_rows),
        "event_clusters": len(cluster_rows),
        "strong": strengths["strong"],
        "moderate": strengths["moderate"],
        "weak": strengths["weak"],
        "px_total": len(eligible_px),
        "px_sampled": len(px_processed),
        "px_false_negatives": px_false_negatives,
        "warnings": warnings,
        "errors": errors,
        "outputs": {
            "documents": str(documents_target.relative_to(root)),
            "candidates": str(candidates_target.relative_to(root)),
            "clusters": str(clusters_target.relative_to(root)),
            "year_summary": str(year_target.relative_to(root)),
            "location_summary": str(location_target.relative_to(root)),
        },
        "resume_status": "completed_from_checkpoints",
    }
    _write_json(run_target, manifest)
    return manifest


class _DefaultProcessor:
    def __init__(
        self,
        *,
        config: HistoricalConfig,
        root: Path,
        registry_path: Path,
        transport: PDFDownloadTransport | None,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
    ) -> None:
        self.root = root
        self.registry_path = registry_path
        self.transport = transport
        self.sleep = sleep
        self.now = now
        self.ingestion = load_ingestion_policy(root / config.ingestion_config)
        self.quality = load_quality_policy(root / config.quality_config)
        self.records = load_document_registry(
            registry_path=registry_path,
            allowed_root=root,
        )

    def __call__(self, row: dict[str, str]) -> HistoricalProcessedDocument:
        document_id = row["document_id"]
        downloaded = download_discovered_pdf(
            document_id=document_id,
            source_url=row["pdf_url"],
            storage_year=int(row["year"]),
            policy=self.ingestion,
            raw_root=self.root / "data/raw/indeci",
            existing_records=self.records,
            transport=self.transport,
            sleep=self.sleep,
            now=self.now,
            preserve_original_filename=True,
        )
        report_date = (
            date.fromisoformat(row["report_date"]) if row["report_date"] else None
        )
        selection = BatchSelection(
            batch_id="indeci-historical",
            document_id=document_id,
            discovery_id=f"indeci-discovery-{sha256(row['pdf_url'].encode()).hexdigest()[:20]}",
            year=int(row["year"]),
            report_type=row["report_type"] or None,
            report_number=row["report_number"] or None,
            report_date=report_date,
            title=row["title"],
            source_connector=row["source_connector"],
            detail_url=row["detail_url"] or None,
            pdf_url=row["pdf_url"],
            golden_control=None,
        )
        source = BatchProcessDocument(
            selection=selection,
            download=downloaded,
            expected_site_terms=self.ingestion.expected_site_terms,
            expected_region_terms=self.ingestion.expected_region_terms,
        )
        document, extraction_warnings, extraction_error = process_document(
            source,
            policy=self.ingestion,
            root=self.root,
        )
        self.records = upsert_document_registry_record(self.records, document)
        write_document_registry(
            self.records,
            registry_path=self.registry_path,
            allowed_root=self.root,
        )
        classification = document.get("classification")
        matched_terms = ()
        if isinstance(classification, dict):
            values = classification.get("matched_terms", [])
            if isinstance(values, list):
                matched_terms = tuple(str(item) for item in values)
        full_text = _read_text(document.get("text_path"), root=self.root)
        candidates = tuple(
            item
            for raw in document.get("event_candidates", [])
            if isinstance(raw, dict)
            and (item := _audit_event_candidate(raw, row=row, quality=self.quality))
            is not None
        )
        return HistoricalProcessedDocument(
            document_id=document_id,
            sha256=str(document.get("sha256") or ""),
            page_count=int(document.get("page_count") or 0),
            download_status=(
                "reused" if downloaded.download_status != "downloaded" else "downloaded"
            ),
            extraction_status=str(document.get("extraction_status") or "not_started"),
            ocr_required=bool(document.get("ocr_required", False)),
            matched_terms=matched_terms,
            full_text=full_text,
            candidates=candidates,
            warnings=tuple((*downloaded.warnings, *extraction_warnings)),
            error=extraction_error or "",
        )

    def persist_registry(self) -> None:
        write_document_registry(
            self.records,
            registry_path=self.registry_path,
            allowed_root=self.root,
        )


def _audit_event_candidate(
    raw: dict[str, object], *, row: dict[str, str], quality
) -> HistoricalCandidate | None:
    identity = json.dumps(
        {
            "document_id": row["document_id"],
            "page": raw.get("source_page"),
            "date": raw.get("event_date"),
            "snippet": raw.get("evidence_snippet"),
        },
        ensure_ascii=True,
        sort_keys=True,
    )
    original = OriginalCandidate.create(
        candidate_id=f"indeci-candidate-{sha256(identity.encode()).hexdigest()[:20]}",
        source_document_id=row["document_id"],
        source_page=raw.get("source_page"),
        event_date=raw.get("event_date"),
        evidence_snippet=raw.get("evidence_snippet"),
        matched_terms=raw.get("matched_terms"),
        validation_status=raw.get("validation_status"),
    )
    audited = audit_candidate(original, policy=quality)
    if audited is None:
        return None
    return HistoricalCandidate(
        candidate_id=audited.candidate_id,
        document_id=row["document_id"],
        document_family_id=row["document_family_id"],
        event_date=audited.event_date.isoformat() if audited.event_date else "",
        event_time=audited.event_time or "",
        event_type=audited.event_type or "",
        reported_quebrada=audited.reported_quebrada or "",
        candidate_strength=audited.candidate_strength,
        source_page=audited.source_page,
        evidence_snippet=audited.evidence_snippet,
        matched_terms=audited.matched_terms,
    )


def historical_final_decision(
    title: str, *, result: HistoricalProcessedDocument, policy: FilterPolicy
):
    return evaluate_relevance(
        FilterEvidence(
            candidate_fields=tuple(
                value
                for candidate in result.candidates
                for value in (candidate.reported_quebrada, candidate.event_type)
                if value
            ),
            evidence_snippets=tuple(
                candidate.evidence_snippet
                for candidate in result.candidates
                if candidate.evidence_snippet
            ),
            matched_terms=result.matched_terms,
            full_text=result.full_text,
            title=title,
            text_available=bool(result.full_text),
        ),
        policy=policy,
    )


def _document_rows(
    rows: list[dict[str, str]],
    *,
    processed: dict[str, HistoricalProcessedDocument],
    prefilters: dict[str, HistoricalPrefilter],
    policy: FilterPolicy,
) -> tuple[list[dict[str, object]], dict[str, tuple[str, ...]]]:
    output = []
    locations: dict[str, tuple[str, ...]] = {}
    for row in sorted(rows, key=lambda item: (int(item["year"]), item["document_id"])):
        document_id = row["document_id"]
        result = processed.get(document_id)
        decision = (
            historical_final_decision(row["title"], result=result, policy=policy)
            if result
            else None
        )
        best = max(
            result.candidates if result else (),
            key=lambda item: (
                {"weak": 0, "moderate": 1, "strong": 2}.get(
                    item.candidate_strength, -1
                ),
                item.event_date,
            ),
            default=None,
        )
        output.append(
            build_document_row(
                document_id=document_id,
                document_family_id=row["document_family_id"],
                year=int(row["year"]),
                title=row["title"],
                report_type=row["report_type"],
                report_number=row["report_number"],
                report_date=row["report_date"],
                event_date=best.event_date if best else "",
                source_url=row["detail_url"] or row["pdf_url"],
                pdf_url=row["pdf_url"],
                prefilter=prefilters[document_id],
                sha256_digest=result.sha256 if result else "",
                page_count=result.page_count if result else 0,
                spatial_relevance=(decision.spatial_relevance if decision else None),
                event_relevance=(decision.event_relevance if decision else None),
                rainfall_related=(decision.rainfall_related if decision else "unknown"),
                review_priority=(decision.review_priority if decision else None),
                priority_reason=(decision.priority_reason if decision else None),
                download_status=result.download_status if result else "not_selected",
                extraction_status=result.extraction_status if result else "not_started",
                ocr_required=result.ocr_required if result else False,
                duplicate_status=row["duplicate_status"],
                duplicate_reason=row["duplicate_reason"],
                canonical_document_id=row["canonical_document_id"],
            )
        )
        locations[document_id] = decision.detected_locations if decision else ()
    return output, locations


def apply_sha256_deduplication(
    rows: list[dict[str, str]],
    *,
    processed: dict[str, HistoricalProcessedDocument],
) -> int:
    """Mark exact downloaded duplicates while retaining every metadata row."""
    by_digest: dict[str, list[str]] = defaultdict(list)
    rows_by_id = {row["document_id"]: row for row in rows}
    for document_id, result in processed.items():
        if re.fullmatch(r"[0-9a-f]{64}", result.sha256):
            by_digest[result.sha256].append(document_id)
    duplicate_count = 0
    for document_ids in by_digest.values():
        if len(document_ids) < 2:
            continue
        canonical = min(document_ids)
        canonical_family = rows_by_id[canonical]["document_family_id"]
        for document_id in sorted(document_ids):
            row = rows_by_id[document_id]
            row["canonical_document_id"] = canonical
            row["document_family_id"] = canonical_family
            row["duplicate_status"] = "merged"
            row["duplicate_reason"] = "sha256"
            if document_id != canonical:
                duplicate_count += 1
    return duplicate_count


def _candidate_rows(
    candidates: tuple[HistoricalCandidate, ...],
    *,
    assignments: dict[str, str],
    rows: list[dict[str, str]],
    processed: dict[str, HistoricalProcessedDocument],
    policy: FilterPolicy,
) -> list[dict[str, object]]:
    source_rows = {row["document_id"]: row for row in rows}
    decisions = {
        document_id: historical_final_decision(
            source_rows[document_id]["title"], result=result, policy=policy
        )
        for document_id, result in processed.items()
    }
    return [
        {
            "candidate_id": candidate.candidate_id,
            "event_cluster_id": assignments[candidate.candidate_id],
            "document_id": candidate.document_id,
            "document_family_id": candidate.document_family_id,
            "event_date": candidate.event_date,
            "event_time": candidate.event_time,
            "event_type": candidate.event_type,
            "reported_quebrada": candidate.reported_quebrada,
            "district": decisions[candidate.document_id].primary_location,
            "source_page": candidate.source_page,
            "evidence_snippet": candidate.evidence_snippet,
            "candidate_strength": candidate.candidate_strength,
            "spatial_relevance": decisions[candidate.document_id].spatial_relevance,
            "review_priority": decisions[candidate.document_id].review_priority,
            "validation_status": "pending_review",
        }
        for candidate in sorted(candidates, key=lambda item: item.candidate_id)
    ]


def _year_summary(
    *,
    years: tuple[int, ...],
    discovery_rows: list[dict[str, str]],
    documents: list[dict[str, object]],
    candidates: list[dict[str, object]],
    clusters: list[dict[str, object]],
    errors: list[str],
    discovery_stats: dict[str, object],
) -> list[dict[str, object]]:
    document_year = {str(row["document_id"]): int(row["year"]) for row in documents}
    candidate_year = {
        str(row["candidate_id"]): document_year[str(row["document_id"])]
        for row in candidates
    }
    cluster_years = defaultdict(set)
    for cluster in clusters:
        for candidate_id in str(cluster["supporting_candidates"]).split("|"):
            if candidate_id in candidate_year:
                cluster_years[str(cluster["event_cluster_id"])].add(
                    candidate_year[candidate_id]
                )
    error_ids = {item.split(":", maxsplit=1)[0] for item in errors}
    output = []
    raw_year_stats = discovery_stats.get("year_stats", {})
    if not isinstance(raw_year_stats, dict):
        raw_year_stats = {}
    for year in years:
        year_documents = [row for row in documents if int(row["year"]) == year]
        year_candidates = [
            row for row in candidates if document_year[str(row["document_id"])] == year
        ]
        priorities = Counter(str(row["review_priority"]) for row in year_documents)
        strengths = Counter(str(row["candidate_strength"]) for row in year_candidates)
        output.append(
            {
                "year": year,
                "documents_discovered": int(
                    (
                        raw_year_stats.get(str(year), {})
                        if isinstance(raw_year_stats.get(str(year), {}), dict)
                        else {}
                    ).get(
                        "documents_raw",
                        sum(int(row["year"]) == year for row in discovery_rows),
                    )
                ),
                "documents_unique": len(year_documents),
                **{priority: priorities[priority] for priority in _PRIORITIES},
                "documents_downloaded": sum(
                    row["download_status"] in {"downloaded", "reused"}
                    for row in year_documents
                ),
                "event_candidates": len(year_candidates),
                "event_clusters": sum(
                    year in member_years for member_years in cluster_years.values()
                ),
                "strong": strengths["strong"],
                "moderate": strengths["moderate"],
                "weak": strengths["weak"],
                "errors": sum(
                    str(row["document_id"]) in error_ids for row in year_documents
                ),
            }
        )
    return output


def _location_summary(
    *,
    documents: list[dict[str, object]],
    candidates: list[dict[str, object]],
    locations_by_document: dict[str, tuple[str, ...]],
) -> list[dict[str, object]]:
    document_year = {str(row["document_id"]): str(row["year"]) for row in documents}
    result = []
    for location in _LOCATION_SUMMARY:
        if location == "otras":
            document_ids = {
                document_id
                for document_id, locations in locations_by_document.items()
                if not set(locations) & set(_LOCATION_SUMMARY[:-1])
            }
        else:
            document_ids = {
                document_id
                for document_id, locations in locations_by_document.items()
                if location in locations
            }
        result.append(
            {
                "location": location,
                "documents": len(document_ids),
                "candidate_events": sum(
                    str(row["document_id"]) in document_ids for row in candidates
                ),
                "years_present": "|".join(
                    sorted({document_year[item] for item in document_ids})
                ),
            }
        )
    return result


def _verify_prefilters(
    rows: list[dict[str, str]], *, prefilters: dict[str, HistoricalPrefilter]
) -> None:
    for row in rows:
        decision = prefilters[row["document_id"]]
        expected = (
            decision.spatial_relevance,
            decision.event_relevance,
            decision.priority,
            str(decision.metadata_insufficient).lower(),
        )
        observed = (
            row["spatial_relevance"],
            row["event_relevance"],
            row["preliminary_priority"],
            row["metadata_insufficient"],
        )
        if observed != expected:
            raise RuntimeError(
                f"historical discovery prefilter mismatch: {row['document_id']}"
            )


def _read_discovery(
    path: Path, *, root: Path, years: tuple[int, ...]
) -> list[dict[str, str]]:
    source = _safe_input(path, root=root, maximum=50_000_000)
    try:
        with source.open(newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if tuple(reader.fieldnames or ()) != HISTORICAL_DISCOVERY_FIELDS:
                raise RuntimeError("historical discovery columns do not match schema")
            rows = []
            for index, raw in enumerate(reader, start=2):
                if None in raw or len(rows) >= 20_000:
                    raise RuntimeError(
                        "historical discovery row limit or width invalid"
                    )
                row = {key: _restore_cell(value or "") for key, value in raw.items()}
                _validate_discovery_row(row, index=index, years=years)
                rows.append(row)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RuntimeError("historical discovery CSV could not be read") from exc
    identifiers = [row["document_id"] for row in rows]
    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError("historical discovery contains duplicate document_id")
    return rows


def _read_discovery_manifest(
    discovery_path: Path,
    *,
    root: Path,
    policy: HistoricalConfig,
) -> dict[str, object]:
    source = discovery_path if discovery_path.is_absolute() else root / discovery_path
    manifest = source.with_name("discovery_run.json")
    if not manifest.exists():
        return {}
    target = _safe_input(manifest, root=root, maximum=10_000_000)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("historical discovery manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("config_sha256") != policy.config_sha256
        or payload.get("years_processed") != list(policy.years)
    ):
        raise RuntimeError("historical discovery manifest does not match config")
    return payload


def _validate_discovery_row(
    row: dict[str, str], *, index: int, years: tuple[int, ...]
) -> None:
    try:
        if _DOCUMENT_ID.fullmatch(row["document_id"]) is None:
            raise ValueError("invalid document_id")
        if _FAMILY_ID.fullmatch(row["document_family_id"]) is None:
            raise ValueError("invalid document_family_id")
        if int(row["year"]) not in years:
            raise ValueError("year outside historical period")
        if row["preliminary_priority"] not in _PRIORITIES:
            raise ValueError("invalid preliminary priority")
        if row["metadata_insufficient"] not in {"true", "false"}:
            raise ValueError("invalid metadata_insufficient")
        if row["pdf_url"]:
            canonical_document_url(row["pdf_url"])
        sources = json.loads(row["discovery_sources"])
        if not isinstance(sources, list) or not all(
            isinstance(item, str) for item in sources
        ):
            raise ValueError("invalid discovery_sources")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid historical discovery row {index}: {exc}") from exc


def _processed_to_dict(value: HistoricalProcessedDocument) -> dict[str, object]:
    return {
        "document_id": value.document_id,
        "sha256": value.sha256,
        "page_count": value.page_count,
        "download_status": value.download_status,
        "extraction_status": value.extraction_status,
        "ocr_required": value.ocr_required,
        "matched_terms": list(value.matched_terms),
        "full_text": value.full_text,
        "candidates": [candidate.__dict__ for candidate in value.candidates],
        "warnings": list(value.warnings),
        "error": value.error,
    }


def _processed_from_dict(raw: object) -> HistoricalProcessedDocument:
    if not isinstance(raw, dict):
        raise RuntimeError("historical run checkpoint document is invalid")
    try:
        candidates = tuple(HistoricalCandidate(**item) for item in raw["candidates"])
        return HistoricalProcessedDocument(
            document_id=raw["document_id"],
            sha256=raw["sha256"],
            page_count=raw["page_count"],
            download_status=raw["download_status"],
            extraction_status=raw["extraction_status"],
            ocr_required=raw["ocr_required"],
            matched_terms=tuple(raw["matched_terms"]),
            full_text=raw["full_text"],
            candidates=candidates,
            warnings=tuple(raw["warnings"]),
            error=raw["error"],
        )
    except (KeyError, TypeError) as exc:
        raise RuntimeError("historical run checkpoint document is invalid") from exc


def _load_run_checkpoint(
    path: Path,
    *,
    year: int,
    policy: HistoricalConfig,
    root: Path,
):
    if not path.exists():
        return None
    source = _safe_input(path, root=root, maximum=20_000_000)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"historical run checkpoint {year} is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("config_sha256") != policy.config_sha256
        or payload.get("year") != year
        or not isinstance(payload.get("documents"), list)
    ):
        raise RuntimeError(f"historical run checkpoint {year} does not match")
    return payload["documents"]


def _completed_run(path: Path, documents: Path, *, policy: HistoricalConfig):
    if not path.exists() and not documents.exists():
        return None
    if not path.is_file() or not documents.is_file():
        raise RuntimeError("historical run outputs are incomplete")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("historical run manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("config_sha256") != policy.config_sha256
        or payload.get("years_processed") != list(policy.years)
    ):
        raise RuntimeError("existing historical run cannot be overwritten")
    return payload


def _read_text(value: object, *, root: Path) -> str:
    if not value:
        return ""
    path = Path(str(value))
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise RuntimeError("historical extracted text must not be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or resolved.stat().st_size > 20_000_000:
        raise RuntimeError("historical extracted text is outside bounds")
    return resolved.read_text(encoding="utf-8")


def _priority_rank(priority: str) -> int:
    return {"P1": 0, "P2": 1, "P3": 2, "P4": 3, "PX": 4}[priority]


def _safe_input(path: Path, *, root: Path, maximum: int) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise RuntimeError("historical input must not be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise RuntimeError("historical input must be inside allowed_root")
    if resolved.stat().st_size > maximum:
        raise RuntimeError("historical input exceeds its size limit")
    return resolved


def _safe_output(path: Path, *, root: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise RuntimeError("historical output must not be a symlink")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise RuntimeError("historical output escapes allowed_root")
    return resolved


def _write_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    if temporary.exists() or temporary.is_symlink():
        raise RuntimeError(f"historical staging file exists: {temporary.name}")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(
                {field: _cell(row.get(field, "")) for field in fields} for row in rows
            )
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
        staged = Path(output.name)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _cell(value: object) -> object:
    if isinstance(value, str):
        value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)
        if value.lstrip().startswith(("=", "+", "-", "@")):
            return f"'{value}"
    return value


def _restore_cell(value: str) -> str:
    return (
        value[1:]
        if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@"))
        else value
    )


def _bounded_message(value: object) -> str:
    return " ".join(str(value).replace("\x00", "").split())[:500]


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None:
        raise RuntimeError("historical timestamp must be timezone-aware")
    return value.astimezone(UTC)
