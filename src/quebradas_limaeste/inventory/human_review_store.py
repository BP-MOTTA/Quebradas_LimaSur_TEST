"""Strict CSV loading and protected persistence for INDECI human review."""

from __future__ import annotations

import csv
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.batch_ingestion import (
    BATCH_DOCUMENT_FIELDS,
    BATCH_DOCUMENTS_OUTPUT,
    BATCH_EVENT_CANDIDATES_OUTPUT,
    BATCH_EVENT_CLUSTERS_OUTPUT,
)
from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_FIELDS,
    CONSOLIDATED_FIELDS,
)
from quebradas_limaeste.inventory.human_review_models import (
    CANDIDATE_STRENGTHS,
    DOCUMENT_REVIEW_FIELDS,
    DOCUMENT_REVIEW_OUTPUT,
    EVENT_DATE_REVIEWS,
    EVENT_REVIEWS,
    GOLDEN_CONTROL_ROLES,
    HUMAN_REVIEW_FIELDS,
    HUMAN_REVIEW_OUTPUT,
    INVENTORY_DECISIONS,
    RELEVANCE_STATUSES,
    REPORT_TYPES,
    SITE_REVIEWS,
    SPATIAL_PRECISIONS,
    STRENGTH_RANK,
    AutomaticCandidate,
    DocumentDecision,
    EventDecision,
    HumanReviewError,
    ReviewBatch,
    ReviewCluster,
    ReviewDocument,
    ReviewProtectionError,
    ReviewState,
    bounded_text,
    candidate_sort_key,
    normalize_notes,
    validate_choice,
)
from quebradas_limaeste.inventory.triage import (
    BATCH_SELECTION_OUTPUT,
    SELECTION_FIELDS,
)

MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_ROWS = 5_000

_RELEVANCE_RANK = {
    value: index for index, value in enumerate(RELEVANCE_STATUSES)
}
_DOCUMENT_ID = re.compile(r"[A-Z0-9_]+")
_BATCH_ID = re.compile(r"indeci-batch-[0-9a-f]{16}")
_CANDIDATE_ID = re.compile(r"indeci-candidate-[0-9a-f]{20}")
_EVENT_CLUSTER_ID = re.compile(r"indeci-event-[0-9a-f]{20}")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


def load_review_batch(
    *,
    documents_path: Path = BATCH_DOCUMENTS_OUTPUT,
    selection_path: Path = BATCH_SELECTION_OUTPUT,
    candidates_path: Path = BATCH_EVENT_CANDIDATES_OUTPUT,
    clusters_path: Path = BATCH_EVENT_CLUSTERS_OUTPUT,
    allowed_root: Path,
) -> ReviewBatch:
    """Load and strictly join local automatic outputs without reading PDF text."""
    root = allowed_root.resolve()
    document_rows = _read_csv(
        documents_path, BATCH_DOCUMENT_FIELDS, root=root, label="batch documents"
    )
    selection_rows = _read_csv(
        selection_path, SELECTION_FIELDS, root=root, label="batch selection"
    )
    candidate_rows = _read_csv(
        candidates_path, AUDIT_FIELDS, root=root, label="batch candidates"
    )
    cluster_rows = _read_csv(
        clusters_path, CONSOLIDATED_FIELDS, root=root, label="batch clusters"
    )

    documents_by_id, batch_id = _automatic_documents(document_rows)
    selections = _selected_metadata(selection_rows, batch_id=batch_id)
    if set(selections) != set(documents_by_id):
        raise HumanReviewError(
            "selected documents do not match batch document outputs"
        )
    candidates = _automatic_candidates(candidate_rows, set(documents_by_id))
    clusters = _automatic_clusters(cluster_rows, candidates)

    clusters_by_document: dict[str, list[ReviewCluster]] = {
        document_id: [] for document_id in documents_by_id
    }
    for cluster in clusters:
        clusters_by_document[cluster.document_id].append(cluster)

    documents = []
    for document_id, row in documents_by_id.items():
        selected = selections[document_id]
        if any(
            selected[field] != row[field]
            for field in ("year", "report_number", "title")
        ):
            raise HumanReviewError(
                f"selection metadata conflicts with document {document_id}"
            )
        matched_terms = _json_string_list(row["matched_terms"], "matched_terms")
        document_clusters = tuple(
            sorted(clusters_by_document[document_id], key=_cluster_sort_key)
        )
        automatic = {
            "batch_id": batch_id,
            "document_id": document_id,
            "report_number": selected["report_number"],
            "report_type": selected["report_type"],
            "report_date": selected["report_date"],
            "year": row["year"],
            "title": row["title"],
            "relevance_status": row["relevance_status"],
            "matched_terms": list(matched_terms),
            "page_count": row["page_count"],
            "golden_control": selected["golden_control"],
            "clusters": [item.automatic_fingerprint for item in document_clusters],
        }
        documents.append(
            ReviewDocument(
                batch_id=batch_id,
                document_id=document_id,
                report_number=selected["report_number"],
                report_type=selected["report_type"],
                report_date=selected["report_date"],
                year=_positive_int(row["year"], "year"),
                title=bounded_text(row["title"], "title", maximum=1_000),
                relevance_status=validate_choice(
                    row["relevance_status"],
                    "relevance_status",
                    RELEVANCE_STATUSES,
                ),
                matched_terms=matched_terms,
                page_count=_positive_int(row["page_count"], "page_count"),
                golden_control=bool(selected["golden_control"]),
                golden_control_role=selected["golden_control"],
                clusters=document_clusters,
                automatic_fingerprint=_fingerprint(automatic),
            )
        )
    return ReviewBatch(
        batch_id=batch_id,
        documents=tuple(sorted(documents, key=_document_sort_key)),
    )


def load_review_state(
    batch: ReviewBatch,
    *,
    document_review_path: Path = DOCUMENT_REVIEW_OUTPUT,
    human_review_path: Path = HUMAN_REVIEW_OUTPUT,
    allowed_root: Path,
) -> ReviewState:
    """Load curated state without changing or silently repairing any decision."""
    root = allowed_root.resolve()
    document_rows = _read_optional_review_csv(
        document_review_path,
        DOCUMENT_REVIEW_FIELDS,
        root=root,
        label="document review",
        key="document_id",
        row_validator=_validate_document_review_row,
    )
    event_rows = _read_optional_review_csv(
        human_review_path,
        HUMAN_REVIEW_FIELDS,
        root=root,
        label="human event review",
        key="event_cluster_id",
        row_validator=_validate_event_review_row,
    )
    _validate_current_review_links(batch, document_rows, event_rows)
    return ReviewState(document_rows=document_rows, event_rows=event_rows)


def save_review_decisions(
    batch: ReviewBatch,
    *,
    document_id: str,
    document_decision: DocumentDecision | None,
    event_decisions: Mapping[str, EventDecision],
    reviewer: str,
    document_review_path: Path = DOCUMENT_REVIEW_OUTPUT,
    human_review_path: Path = HUMAN_REVIEW_OUTPUT,
    allowed_root: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Append one document review while refusing all human-decision overwrites."""
    root = allowed_root.resolve()
    document_target = _safe_output(document_review_path, root=root)
    event_target = _safe_output(human_review_path, root=root)
    if document_target == event_target:
        raise HumanReviewError("document and event review outputs must differ")
    document = _document_by_id(batch, document_id)
    if document.golden_control:
        raise ReviewProtectionError("golden controls do not require a new review")
    state = load_review_state(
        batch,
        document_review_path=document_target,
        human_review_path=event_target,
        allowed_root=root,
    )
    if document.document_id in state.document_rows and document_decision is not None:
        raise ReviewProtectionError(
            f"document {document.document_id} is already reviewed"
        )
    if document.document_id not in state.document_rows and document_decision is None:
        raise HumanReviewError("a pending document requires a document decision")

    clusters = {item.event_cluster_id: item for item in document.clusters}
    supplied = set(event_decisions)
    unknown = supplied - set(clusters)
    if unknown:
        raise HumanReviewError("event decisions contain an unknown document cluster")
    existing = supplied & set(state.event_rows)
    if existing:
        raise ReviewProtectionError(
            f"event cluster {sorted(existing)[0]} is already reviewed"
        )
    missing = set(clusters) - set(state.event_rows) - supplied
    if missing:
        raise HumanReviewError("all pending event clusters require a decision")

    reviewed_at = _utc_timestamp(now)
    reviewed_by = validate_reviewer(reviewer)
    new_document_rows = list(state.document_rows.values())
    if document_decision is not None:
        new_document_rows.append(
            _document_review_row(
                document, document_decision, reviewed_at, reviewed_by
            )
        )
    new_event_rows = list(state.event_rows.values())
    for cluster in document.clusters:
        decision = event_decisions.get(cluster.event_cluster_id)
        if decision is not None:
            new_event_rows.append(
                _event_review_row(
                    batch.batch_id,
                    cluster,
                    decision,
                    reviewed_at,
                    reviewed_by,
                )
            )

    # Event state is replaced first so an interrupted two-file save remains resumable.
    _write_csv(event_target, HUMAN_REVIEW_FIELDS, new_event_rows)
    _write_csv(document_target, DOCUMENT_REVIEW_FIELDS, new_document_rows)


def refresh_review_staleness(
    batch: ReviewBatch,
    *,
    document_review_path: Path = DOCUMENT_REVIEW_OUTPUT,
    human_review_path: Path = HUMAN_REVIEW_OUTPUT,
    allowed_root: Path,
) -> int:
    """Mark changed automatic evidence stale without modifying human decisions."""
    root = allowed_root.resolve()
    document_target = _safe_output(document_review_path, root=root)
    event_target = _safe_output(human_review_path, root=root)
    state = load_review_state(
        batch,
        document_review_path=document_target,
        human_review_path=event_target,
        allowed_root=root,
    )
    documents = {item.document_id: item for item in batch.documents}
    clusters = {
        cluster.event_cluster_id: cluster
        for document in batch.documents
        for cluster in document.clusters
    }
    changed = 0
    document_rows = []
    for row in state.document_rows.values():
        current = documents.get(row["document_id"])
        stale = current is None or row["automatic_fingerprint"] != (
            current.automatic_fingerprint if current else ""
        )
        updated = dict(row)
        if stale and row["review_stale"] != "true":
            updated["review_stale"] = "true"
            changed += 1
        document_rows.append(updated)
    event_rows = []
    for row in state.event_rows.values():
        current = clusters.get(row["event_cluster_id"])
        stale = current is None or row["automatic_fingerprint"] != (
            current.automatic_fingerprint if current else ""
        )
        updated = dict(row)
        if stale and row["review_stale"] != "true":
            updated["review_stale"] = "true"
            changed += 1
        event_rows.append(updated)
    if changed:
        if event_target.exists():
            _write_csv(event_target, HUMAN_REVIEW_FIELDS, event_rows)
        if document_target.exists():
            _write_csv(document_target, DOCUMENT_REVIEW_FIELDS, document_rows)
    return changed


def validate_reviewer(value: str) -> str:
    return bounded_text(value, "reviewed_by", maximum=100)


def _automatic_documents(
    rows: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], str]:
    if not rows:
        raise HumanReviewError("batch documents CSV is empty")
    batch_ids = {row["batch_id"] for row in rows}
    if len(batch_ids) != 1 or _BATCH_ID.fullmatch(next(iter(batch_ids))) is None:
        raise HumanReviewError("batch documents must contain one valid batch_id")
    batch_id = next(iter(batch_ids))
    documents: dict[str, dict[str, str]] = {}
    for row in rows:
        document_id = row["document_id"]
        if _DOCUMENT_ID.fullmatch(document_id) is None:
            raise HumanReviewError("batch documents contain an invalid document_id")
        if document_id in documents:
            raise HumanReviewError("batch documents contain duplicate document_id")
        validate_choice(row["relevance_status"], "relevance_status", RELEVANCE_STATUSES)
        if row["review_required"] != "true":
            raise HumanReviewError("batch documents must remain pending human review")
        documents[document_id] = row
    return documents, batch_id


def _selected_metadata(
    rows: list[dict[str, str]], *, batch_id: str
) -> dict[str, dict[str, str]]:
    if not rows or {row["batch_id"] for row in rows} != {batch_id}:
        raise HumanReviewError("batch selection does not match the automatic batch")
    selected: dict[str, dict[str, str]] = {}
    for row in rows:
        if row["selected"] == "false":
            continue
        if row["selected"] != "true":
            raise HumanReviewError("batch selection contains an invalid selected value")
        document_id = row["document_id"]
        if document_id in selected:
            raise HumanReviewError("batch selection contains duplicate document_id")
        if row["golden_control"] not in ("", *GOLDEN_CONTROL_ROLES):
            raise HumanReviewError("batch selection contains an invalid golden control")
        validate_choice(row["report_type"], "report_type", REPORT_TYPES)
        _optional_text(row["report_number"], maximum=100)
        _optional_iso_date(row["report_date"], "report_date")
        selected[document_id] = row
    return selected


def _automatic_candidates(
    rows: list[dict[str, str]], document_ids: set[str]
) -> dict[str, AutomaticCandidate]:
    candidates: dict[str, AutomaticCandidate] = {}
    for row in rows:
        candidate_id = _identifier(
            row["candidate_id"], "candidate_id", _CANDIDATE_ID
        )
        if candidate_id in candidates:
            raise HumanReviewError("batch candidates contain duplicate candidate_id")
        document_id = row["source_document_id"]
        if document_id not in document_ids:
            raise HumanReviewError("batch candidate references an unknown document")
        if row["validation_status"] != "pending_review":
            raise HumanReviewError("automatic candidates must remain pending_review")
        strength = validate_choice(
            row["candidate_strength"],
            "candidate_strength",
            CANDIDATE_STRENGTHS,
        )
        candidate = AutomaticCandidate(
            candidate_id=candidate_id,
            document_id=document_id,
            source_page=_positive_int(row["source_page"], "source_page"),
            event_date=_optional_iso_date(row["event_date"], "event_date"),
            event_type=_optional_text(row["event_type"], maximum=100),
            reported_quebrada=_optional_text(
                row["reported_quebrada"], maximum=300
            ),
            canonical_site_id=_optional_text(
                row["canonical_site_id"], maximum=200
            ),
            candidate_strength=strength,
            evidence_snippet=_optional_text(
                row["evidence_snippet"], maximum=4_000
            ),
            automatic_fingerprint=_fingerprint(row),
        )
        candidates[candidate_id] = candidate
    return candidates


def _automatic_clusters(
    rows: list[dict[str, str]], candidates: dict[str, AutomaticCandidate]
) -> tuple[ReviewCluster, ...]:
    clusters = []
    cluster_ids: set[str] = set()
    assigned_candidates: set[str] = set()
    for row in rows:
        cluster_id = _identifier(
            row["event_cluster_id"], "event_cluster_id", _EVENT_CLUSTER_ID
        )
        if cluster_id in cluster_ids:
            raise HumanReviewError("batch clusters contain duplicate event_cluster_id")
        cluster_ids.add(cluster_id)
        if row["validation_status"] != "pending_review":
            raise HumanReviewError("automatic clusters must remain pending_review")
        supporting_ids = tuple(filter(None, row["supporting_candidates"].split("|")))
        if not supporting_ids or any(item not in candidates for item in supporting_ids):
            raise HumanReviewError("batch cluster has invalid supporting candidates")
        if len(set(supporting_ids)) != len(supporting_ids):
            raise HumanReviewError("batch cluster repeats a supporting candidate")
        if assigned_candidates.intersection(supporting_ids):
            raise HumanReviewError(
                "an automatic candidate belongs to multiple clusters"
            )
        assigned_candidates.update(supporting_ids)
        supporting = tuple(candidates[item] for item in supporting_ids)
        document_ids = {item.document_id for item in supporting}
        if len(document_ids) != 1:
            raise HumanReviewError("a batch cluster crosses document boundaries")
        pages = tuple(
            _positive_int(value, "supporting_pages")
            for value in filter(None, row["supporting_pages"].split("|"))
        )
        if not pages:
            raise HumanReviewError("batch cluster has no supporting page")
        if set(pages) != {item.source_page for item in supporting}:
            raise HumanReviewError(
                "batch cluster pages conflict with supporting candidates"
            )
        strength = validate_choice(
            row["candidate_strength"],
            "candidate_strength",
            CANDIDATE_STRENGTHS,
        )
        strongest_support = min(
            supporting,
            key=lambda item: STRENGTH_RANK[item.candidate_strength],
        ).candidate_strength
        if strength != strongest_support:
            raise HumanReviewError("batch cluster strength conflicts with candidates")
        canonical_site_id = _optional_text(row["canonical_site_id"], maximum=200)
        event_date = _optional_iso_date(row["event_date"], "event_date")
        event_type = _optional_text(row["event_type"], maximum=100)
        reported_quebrada = _optional_text(
            row["reported_quebrada"], maximum=300
        )
        evidence_values = {
            (
                item.canonical_site_id,
                item.event_date,
                item.event_type,
                item.reported_quebrada,
            )
            for item in supporting
        }
        if evidence_values != {
            (canonical_site_id, event_date, event_type, reported_quebrada)
        }:
            raise HumanReviewError(
                "batch cluster evidence conflicts with supporting candidates"
            )
        cluster_automatic = {
            **row,
            "candidates": [item.automatic_fingerprint for item in supporting],
        }
        clusters.append(
            ReviewCluster(
                event_cluster_id=cluster_id,
                document_id=next(iter(document_ids)),
                canonical_site_id=canonical_site_id,
                event_date_extracted=event_date,
                event_type=event_type,
                reported_quebrada=reported_quebrada,
                candidate_strength=strength,
                supporting_candidates=supporting_ids,
                supporting_pages=pages,
                best_evidence_snippet=_optional_text(
                    row["best_evidence_snippet"], maximum=4_000
                ),
                candidates=tuple(sorted(supporting, key=candidate_sort_key)),
                automatic_fingerprint=_fingerprint(cluster_automatic),
            )
        )
    if assigned_candidates != set(candidates):
        raise HumanReviewError("not all automatic candidates belong to one cluster")
    return tuple(clusters)


def _validate_document_review_row(row: dict[str, str], row_number: int) -> None:
    _review_common(row, row_number)
    _identifier(row["document_id"], "document_id", _DOCUMENT_ID)
    validate_choice(row["human_site_review"], "human_site_review", SITE_REVIEWS)
    validate_choice(
        row["human_inventory_decision"],
        "human_inventory_decision",
        INVENTORY_DECISIONS,
    )
    validate_choice(
        row["human_spatial_precision"],
        "human_spatial_precision",
        SPATIAL_PRECISIONS,
    )


def _validate_event_review_row(row: dict[str, str], row_number: int) -> None:
    _review_common(row, row_number)
    _identifier(row["document_id"], "document_id", _DOCUMENT_ID)
    _identifier(
        row["event_cluster_id"], "event_cluster_id", _EVENT_CLUSTER_ID
    )
    validate_choice(row["human_site_review"], "human_site_review", SITE_REVIEWS)
    validate_choice(row["human_event_review"], "human_event_review", EVENT_REVIEWS)
    validate_choice(row["human_event_date"], "human_event_date", EVENT_DATE_REVIEWS)
    validate_choice(
        row["human_inventory_decision"],
        "human_inventory_decision",
        INVENTORY_DECISIONS,
    )
    validate_choice(
        row["human_spatial_precision"],
        "human_spatial_precision",
        SPATIAL_PRECISIONS,
    )


def _review_common(row: dict[str, str], row_number: int) -> None:
    if _BATCH_ID.fullmatch(row["batch_id"]) is None:
        raise HumanReviewError(f"review row {row_number} has invalid batch_id")
    if _FINGERPRINT.fullmatch(row["automatic_fingerprint"]) is None:
        raise HumanReviewError(
            f"review row {row_number} has invalid auto fingerprint"
        )
    if row["review_stale"] not in {"true", "false"}:
        raise HumanReviewError(f"review row {row_number} has invalid stale marker")
    _parse_timestamp(row["reviewed_at"])
    validate_reviewer(row["reviewed_by"])
    normalize_notes(row["human_notes"])


def _validate_current_review_links(
    batch: ReviewBatch,
    document_rows: dict[str, dict[str, str]],
    event_rows: dict[str, dict[str, str]],
) -> None:
    current_documents = {item.document_id: item for item in batch.documents}
    current_clusters = {
        cluster.event_cluster_id: cluster
        for document in batch.documents
        for cluster in document.clusters
    }
    for document_id, row in document_rows.items():
        current = current_documents.get(document_id)
        if (
            current
            and row["automatic_fingerprint"] == current.automatic_fingerprint
            and row["golden_control"] != str(current.golden_control).lower()
        ):
            raise HumanReviewError("document review golden-control marker changed")
    for cluster_id, row in event_rows.items():
        current = current_clusters.get(cluster_id)
        if (
            current
            and row["automatic_fingerprint"] == current.automatic_fingerprint
            and row["document_id"] != current.document_id
        ):
            raise HumanReviewError("human event review changed document ownership")


def _document_review_row(
    document: ReviewDocument,
    decision: DocumentDecision,
    reviewed_at: str,
    reviewed_by: str,
) -> dict[str, str]:
    return {
        "batch_id": document.batch_id,
        "document_id": document.document_id,
        "report_number": document.report_number,
        "report_type": document.report_type,
        "report_date": document.report_date,
        "year": str(document.year),
        "title": document.title,
        "relevance_status": document.relevance_status,
        "matched_terms": json.dumps(list(document.matched_terms), ensure_ascii=True),
        "page_count": str(document.page_count),
        "golden_control": str(document.golden_control).lower(),
        "automatic_fingerprint": document.automatic_fingerprint,
        "human_site_review": decision.human_site_review,
        "human_inventory_decision": decision.human_inventory_decision,
        "human_spatial_precision": decision.human_spatial_precision,
        "human_notes": decision.human_notes,
        "reviewed_at": reviewed_at,
        "reviewed_by": reviewed_by,
        "review_stale": "false",
    }


def _event_review_row(
    batch_id: str,
    cluster: ReviewCluster,
    decision: EventDecision,
    reviewed_at: str,
    reviewed_by: str,
) -> dict[str, str]:
    return {
        "batch_id": batch_id,
        "event_cluster_id": cluster.event_cluster_id,
        "document_id": cluster.document_id,
        "canonical_site_id": cluster.canonical_site_id,
        "event_date_extracted": cluster.event_date_extracted,
        "event_type": cluster.event_type,
        "reported_quebrada": cluster.reported_quebrada,
        "candidate_strength": cluster.candidate_strength,
        "supporting_candidates": "|".join(cluster.supporting_candidates),
        "supporting_pages": "|".join(map(str, cluster.supporting_pages)),
        "best_evidence_snippet": cluster.best_evidence_snippet,
        "automatic_fingerprint": cluster.automatic_fingerprint,
        "human_site_review": decision.human_site_review,
        "human_event_review": decision.human_event_review,
        "human_event_date": decision.human_event_date,
        "human_inventory_decision": decision.human_inventory_decision,
        "human_spatial_precision": decision.human_spatial_precision,
        "human_notes": decision.human_notes,
        "reviewed_at": reviewed_at,
        "reviewed_by": reviewed_by,
        "review_stale": "false",
    }


def _read_csv(
    path: Path,
    fields: tuple[str, ...],
    *,
    root: Path,
    label: str,
) -> list[dict[str, str]]:
    source = _safe_input(path, root=root)
    rows = []
    try:
        with source.open(newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if tuple(reader.fieldnames or ()) != fields:
                raise HumanReviewError(f"{label} CSV columns do not match schema")
            for row_number, raw in enumerate(reader, start=2):
                if None in raw:
                    raise HumanReviewError(
                        f"{label} row {row_number} has extra columns"
                    )
                if len(rows) >= MAX_ROWS:
                    raise HumanReviewError(f"{label} CSV exceeds its row limit")
                rows.append(
                    {key: _restore_cell(value or "") for key, value in raw.items()}
                )
    except (OSError, UnicodeError, csv.Error) as exc:
        raise HumanReviewError(f"{label} CSV could not be read") from exc
    return rows


def _read_optional_review_csv(
    path: Path,
    fields: tuple[str, ...],
    *,
    root: Path,
    label: str,
    key: str,
    row_validator: Callable[[dict[str, str], int], None],
) -> dict[str, dict[str, str]]:
    target = _safe_output(path, root=root)
    if not target.exists():
        return {}
    if not target.is_file() or target.stat().st_size > MAX_CSV_BYTES:
        raise HumanReviewError(f"{label} path is invalid or too large")
    rows = _read_csv(target, fields, root=root, label=label)
    indexed = {}
    for row_number, row in enumerate(rows, start=2):
        row_validator(row, row_number)
        identifier = row[key]
        if not identifier or identifier in indexed:
            raise HumanReviewError(f"{label} contains a duplicate or empty {key}")
        indexed[identifier] = row
    return indexed


def _write_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise HumanReviewError("review staging CSV path already exists")
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


def _safe_input(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise HumanReviewError("input symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise HumanReviewError("input path is outside the workspace or missing")
    if candidate.stat().st_size > MAX_CSV_BYTES:
        raise HumanReviewError("input file exceeds its size limit")
    return candidate


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise HumanReviewError("output symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root):
        raise HumanReviewError("output path is outside the workspace")
    return candidate


def _document_sort_key(document: ReviewDocument) -> tuple[int, int, str]:
    strongest = min(
        (STRENGTH_RANK[item.candidate_strength] for item in document.candidates),
        default=len(STRENGTH_RANK),
    )
    return (
        _RELEVANCE_RANK[document.relevance_status],
        strongest,
        document.document_id,
    )


def _cluster_sort_key(cluster: ReviewCluster) -> tuple[int, int, str]:
    return (
        STRENGTH_RANK[cluster.candidate_strength],
        min(cluster.supporting_pages),
        cluster.event_cluster_id,
    )


def _document_by_id(batch: ReviewBatch, document_id: str) -> ReviewDocument:
    for document in batch.documents:
        if document.document_id == document_id:
            return document
    raise HumanReviewError(f"unknown review document {document_id}")


def _identifier(value: str, field: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise HumanReviewError(f"{field} has an invalid format")
    return value


def _optional_text(value: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise HumanReviewError("automatic text field must be text")
    clean = re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
    if len(clean) > maximum:
        raise HumanReviewError("automatic text field exceeds its length limit")
    return clean


def _optional_iso_date(value: str, field: str) -> str:
    clean = _optional_text(value, maximum=10)
    if not clean:
        return clean
    try:
        date.fromisoformat(clean)
    except ValueError as exc:
        raise HumanReviewError(f"{field} must be an ISO date") from exc
    return clean


def _positive_int(value: str, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise HumanReviewError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise HumanReviewError(f"{field} must be positive")
    return parsed


def _json_string_list(value: str, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HumanReviewError(f"{field} must be valid JSON") from exc
    if (
        not isinstance(parsed, list)
        or len(parsed) > 100
        or any(not isinstance(item, str) or len(item) > 300 for item in parsed)
    ):
        raise HumanReviewError(f"{field} must be a bounded string list")
    return tuple(parsed)


def _fingerprint(value: object) -> str:
    serialized = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _utc_timestamp(now: Callable[[], datetime]) -> str:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise HumanReviewError("review timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise HumanReviewError("reviewed_at must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise HumanReviewError("reviewed_at must include a timezone")
    return parsed


def _protect_cell(value: str) -> str:
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _restore_cell(value: str) -> str:
    if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
        return value[1:]
    return value
