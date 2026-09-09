"""Bounded CSV I/O and orchestration for candidate quality audits."""

from __future__ import annotations

import csv
import json
import os
import re
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.candidate_quality import (
    AuditedCandidate,
    CandidateQualityError,
    ConsolidatedEvent,
    OriginalCandidate,
    QualityPolicy,
    audit_candidate,
    consolidate_candidates,
    load_quality_policy,
)

CANDIDATE_OUTPUT = Path("metadata/indeci/events_cusipata_candidates.csv")
AUDIT_OUTPUT = Path("metadata/indeci/events_cusipata_audit.csv")
CONSOLIDATED_OUTPUT = Path("metadata/indeci/events_cusipata_consolidated.csv")
GOLDEN_CONTROLS_OUTPUT = Path("metadata/indeci/golden_controls.json")
QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")
MAX_CSV_BYTES = 10 * 1024 * 1024
MAX_CANDIDATES = 5_000
ORIGINAL_FIELDS = (
    "candidate_id",
    "source_document_id",
    "source_page",
    "event_date",
    "evidence_snippet",
    "matched_terms",
    "validation_status",
)
AUDIT_FIELDS = (
    *ORIGINAL_FIELDS,
    "event_time",
    "reported_quebrada",
    "canonical_site_id",
    "event_type",
    "site_evidence",
    "event_evidence",
    "date_evidence",
    "candidate_strength",
    "review_reason",
)
CONSOLIDATED_FIELDS = (
    "event_cluster_id",
    "canonical_site_id",
    "event_date",
    "event_time",
    "event_type",
    "reported_quebrada",
    "candidate_strength",
    "supporting_candidates",
    "supporting_pages",
    "best_evidence_snippet",
    "validation_status",
)


class CandidateAuditError(RuntimeError):
    """Raised when candidate audit files are unsafe or malformed."""


def write_original_candidates_from_payload(
    payload: dict[str, object],
    *,
    output_path: Path = CANDIDATE_OUTPUT,
    allowed_root: Path,
) -> Path:
    """Export immutable source candidates without applying quality decisions."""
    candidates = _candidates_from_payload(payload)
    rows = [_original_row(candidate) for candidate in candidates]
    destination = _safe_output(output_path, root=allowed_root.resolve())
    _write_csv(destination, ORIGINAL_FIELDS, rows)
    return destination


def export_original_candidates_from_run(
    run_path: Path,
    *,
    output_path: Path = CANDIDATE_OUTPUT,
    allowed_root: Path,
) -> Path:
    """Export a prior immutable ingestion manifest without rewriting it."""
    root = allowed_root.resolve()
    source = _safe_input(run_path, root=root, max_bytes=MAX_CSV_BYTES)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateAuditError("ingestion run must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise CandidateAuditError("ingestion run root must be an object")
    return write_original_candidates_from_payload(
        payload,
        output_path=output_path,
        allowed_root=root,
    )


def execute_candidate_audit(
    input_path: Path,
    *,
    config_path: Path = QUALITY_CONFIG,
    audit_output: Path = AUDIT_OUTPUT,
    consolidated_output: Path = CONSOLIDATED_OUTPUT,
    allowed_root: Path,
) -> dict[str, object]:
    """Audit a candidate CSV and emit separate non-destructive outputs."""
    root = allowed_root.resolve()
    source = _safe_input(input_path, root=root, max_bytes=MAX_CSV_BYTES)
    config = _safe_input(config_path, root=root, max_bytes=65_536)
    audit_destination = _safe_output(audit_output, root=root)
    consolidated_destination = _safe_output(consolidated_output, root=root)
    if source in {audit_destination, consolidated_destination}:
        raise CandidateAuditError("audit outputs must not overwrite the input")
    if audit_destination == consolidated_destination:
        raise CandidateAuditError("audit outputs must use different paths")

    policy = load_quality_policy(config)
    originals = read_original_candidates(source, allowed_root=root)
    audited = tuple(
        result
        for candidate in originals
        if (result := audit_candidate(candidate, policy=policy)) is not None
    )
    clusters = consolidate_candidates(audited)
    _write_csv(
        audit_destination,
        AUDIT_FIELDS,
        [_audited_row(candidate) for candidate in audited],
    )
    _write_csv(
        consolidated_destination,
        CONSOLIDATED_FIELDS,
        [_consolidated_row(cluster) for cluster in clusters],
    )
    counts = {
        strength: sum(item.candidate_strength == strength for item in audited)
        for strength in ("strong", "moderate", "weak")
    }
    relevant_pages = sorted(
        {
            item.source_page
            for item in audited
            if item.candidate_strength in {"strong", "moderate"}
        }
    )
    return {
        "candidates_original": len(originals),
        "candidates_excluded": len(originals) - len(audited),
        **counts,
        "clusters_consolidated": len(clusters),
        "pages_audited": sorted({item.source_page for item in audited}),
        "pages_relevant": relevant_pages,
        "strong_cusipata": _has_strong_target(audited, policy),
        "audit_output": str(audit_destination),
        "consolidated_output": str(consolidated_destination),
        "audited_candidates": [item.to_dict() for item in audited],
    }


def execute_golden_control_comparison(
    negative_audit: Path,
    positive_audit: Path,
    *,
    output_path: Path = GOLDEN_CONTROLS_OUTPUT,
    allowed_root: Path,
) -> dict[str, object]:
    """Compare two audited controls without retaining their evidence text."""
    root = allowed_root.resolve()
    negative_source = _safe_input(
        negative_audit,
        root=root,
        max_bytes=MAX_CSV_BYTES,
    )
    positive_source = _safe_input(
        positive_audit,
        root=root,
        max_bytes=MAX_CSV_BYTES,
    )
    if negative_source == positive_source:
        raise CandidateAuditError("golden controls require two audit files")
    destination = _safe_output(output_path, root=root)
    if destination in {negative_source, positive_source}:
        raise CandidateAuditError("golden-control output must not overwrite an input")
    payload: dict[str, object] = {
        "negative_control": _audit_control_summary(negative_source),
        "positive_control": _audit_control_summary(positive_source),
    }
    _write_json(destination, payload)
    return payload


def read_original_candidates(
    path: Path,
    *,
    allowed_root: Path,
) -> tuple[OriginalCandidate, ...]:
    """Read strict candidate rows while restoring protected spreadsheet cells."""
    source = _safe_input(path, root=allowed_root.resolve(), max_bytes=MAX_CSV_BYTES)
    try:
        with source.open(newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if tuple(reader.fieldnames or ()) != ORIGINAL_FIELDS:
                raise CandidateAuditError("candidate CSV columns do not match schema")
            candidates = []
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise CandidateAuditError(
                        f"candidate CSV row {row_number} has extra columns"
                    )
                if len(candidates) >= MAX_CANDIDATES:
                    raise CandidateAuditError("candidate CSV exceeds its row limit")
                candidates.append(_candidate_from_row(row, row_number=row_number))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CandidateAuditError("candidate CSV could not be read") from exc
    identifiers = [candidate.candidate_id for candidate in candidates]
    if len(set(identifiers)) != len(identifiers):
        raise CandidateAuditError("candidate CSV contains duplicate candidate_id")
    return tuple(candidates)


def _audit_control_summary(path: Path) -> dict[str, object]:
    counts = {strength: 0 for strength in ("strong", "moderate", "weak")}
    document_ids: set[str] = set()
    try:
        with path.open(newline="", encoding="utf-8") as input_file:
            reader = csv.DictReader(input_file)
            if tuple(reader.fieldnames or ()) != AUDIT_FIELDS:
                raise CandidateAuditError("audit CSV columns do not match schema")
            for row_number, row in enumerate(reader, start=2):
                if None in row:
                    raise CandidateAuditError(
                        f"audit CSV row {row_number} has extra columns"
                    )
                if row_number > MAX_CANDIDATES + 1:
                    raise CandidateAuditError("audit CSV exceeds its row limit")
                restored = {
                    key: _restore_cell(value or "") for key, value in row.items()
                }
                document_id = restored["source_document_id"]
                if re.fullmatch(r"[A-Z0-9_]+", document_id) is None:
                    raise CandidateAuditError(
                        f"audit CSV row {row_number} has invalid document_id"
                    )
                if restored["validation_status"] != "pending_review":
                    raise CandidateAuditError(
                        f"audit CSV row {row_number} is not pending_review"
                    )
                strength = restored["candidate_strength"]
                if strength not in counts:
                    raise CandidateAuditError(
                        f"audit CSV row {row_number} has invalid strength"
                    )
                counts[strength] += 1
                document_ids.add(document_id)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CandidateAuditError("audit CSV could not be read") from exc
    if len(document_ids) != 1:
        raise CandidateAuditError("audit CSV must contain exactly one document_id")
    return {"document_id": document_ids.pop(), **counts}


def _candidates_from_payload(
    payload: dict[str, object],
) -> tuple[OriginalCandidate, ...]:
    documents = payload.get("documents")
    if not isinstance(documents, list) or len(documents) > MAX_CANDIDATES:
        raise CandidateAuditError("ingestion payload has invalid documents")
    candidates = []
    for document in documents:
        if not isinstance(document, dict):
            raise CandidateAuditError("ingestion payload document is invalid")
        document_id = document.get("document_id")
        events = document.get("event_candidates")
        if not isinstance(events, list):
            raise CandidateAuditError("ingestion payload candidates are invalid")
        for event in events:
            if not isinstance(event, dict):
                raise CandidateAuditError("ingestion candidate is invalid")
            candidate_id = _candidate_id(document_id, event)
            try:
                candidates.append(
                    OriginalCandidate.create(
                        candidate_id=candidate_id,
                        source_document_id=event.get(
                            "source_document_id", document_id
                        ),
                        source_page=event.get("source_page"),
                        event_date=event.get("event_date"),
                        evidence_snippet=event.get("evidence_snippet"),
                        matched_terms=event.get("matched_terms"),
                        validation_status=event.get("validation_status"),
                    )
                )
            except CandidateQualityError as exc:
                raise CandidateAuditError(
                    f"invalid ingestion candidate: {exc}"
                ) from exc
            if len(candidates) > MAX_CANDIDATES:
                raise CandidateAuditError("ingestion payload exceeds candidate limit")
    return tuple(candidates)


def _candidate_from_row(
    row: dict[str, str | None],
    *,
    row_number: int,
) -> OriginalCandidate:
    restored = {key: _restore_cell(value or "") for key, value in row.items()}
    try:
        matched_terms = json.loads(restored["matched_terms"])
        return OriginalCandidate.create(
            candidate_id=restored["candidate_id"],
            source_document_id=restored["source_document_id"],
            source_page=int(restored["source_page"]),
            event_date=restored["event_date"] or None,
            evidence_snippet=restored["evidence_snippet"],
            matched_terms=matched_terms,
            validation_status=restored["validation_status"],
        )
    except (CandidateQualityError, json.JSONDecodeError, ValueError) as exc:
        raise CandidateAuditError(f"candidate CSV row {row_number} is invalid") from exc


def _candidate_id(document_id: object, event: dict[str, object]) -> str:
    parts = (
        str(document_id),
        str(event.get("source_page")),
        str(event.get("event_date") or ""),
        str(event.get("evidence_snippet")),
    )
    digest = sha256("\x00".join(parts).encode()).hexdigest()[:20]
    return f"indeci-candidate-{digest}"


def _has_strong_target(
    candidates: tuple[AuditedCandidate, ...],
    policy: QualityPolicy,
) -> bool:
    return any(
        candidate.candidate_strength == "strong"
        and candidate.canonical_site_id == policy.canonical_site_id
        and candidate.event_type == "activacion_quebrada"
        and candidate.site_evidence is not None
        and candidate.date_evidence is not None
        for candidate in candidates
    )


def _original_row(candidate: OriginalCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "source_document_id": candidate.source_document_id,
        "source_page": candidate.source_page,
        "event_date": candidate.event_date.isoformat() if candidate.event_date else "",
        "evidence_snippet": candidate.evidence_snippet,
        "matched_terms": json.dumps(list(candidate.matched_terms), ensure_ascii=True),
        "validation_status": candidate.validation_status,
    }


def _audited_row(candidate: AuditedCandidate) -> dict[str, object]:
    row = _original_row(
        OriginalCandidate.create(
            candidate_id=candidate.candidate_id,
            source_document_id=candidate.source_document_id,
            source_page=candidate.source_page,
            event_date=candidate.event_date,
            evidence_snippet=candidate.evidence_snippet,
            matched_terms=candidate.matched_terms,
            validation_status=candidate.validation_status,
        )
    )
    row.update(
        {
            "event_time": candidate.event_time or "",
            "reported_quebrada": candidate.reported_quebrada or "",
            "canonical_site_id": candidate.canonical_site_id or "",
            "event_type": candidate.event_type or "",
            "site_evidence": candidate.site_evidence or "",
            "event_evidence": candidate.event_evidence or "",
            "date_evidence": candidate.date_evidence or "",
            "candidate_strength": candidate.candidate_strength,
            "review_reason": candidate.review_reason,
        }
    )
    return row


def _consolidated_row(cluster: ConsolidatedEvent) -> dict[str, object]:
    return {
        "event_cluster_id": cluster.event_cluster_id,
        "canonical_site_id": cluster.canonical_site_id or "",
        "event_date": cluster.event_date.isoformat() if cluster.event_date else "",
        "event_time": cluster.event_time or "",
        "event_type": cluster.event_type or "",
        "reported_quebrada": cluster.reported_quebrada or "",
        "candidate_strength": cluster.candidate_strength,
        "supporting_candidates": "|".join(cluster.supporting_candidates),
        "supporting_pages": "|".join(map(str, cluster.supporting_pages)),
        "best_evidence_snippet": cluster.best_evidence_snippet,
        "validation_status": cluster.validation_status,
    }


def _write_csv(
    path: Path,
    fieldnames: tuple[str, ...],
    rows: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise CandidateAuditError("staging CSV path already exists")
    try:
        with part.open("x", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=fieldnames,
                extrasaction="raise",
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {key: _protect_cell(str(row.get(key, ""))) for key in fieldnames}
                )
            output.flush()
            os.fsync(output.fileno())
        part.replace(path)
    finally:
        if part.exists():
            part.unlink()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(f"{path.name}.part")
    if part.exists() or part.is_symlink():
        raise CandidateAuditError("staging JSON path already exists")
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    try:
        with part.open("x", encoding="utf-8", newline="\n") as output:
            output.write(serialized)
            output.flush()
            os.fsync(output.fileno())
        part.replace(path)
    finally:
        if part.exists():
            part.unlink()


def _safe_input(path: Path, *, root: Path, max_bytes: int) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise CandidateAuditError("input symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise CandidateAuditError("input path is outside the workspace or missing")
    if candidate.stat().st_size > max_bytes:
        raise CandidateAuditError("input file exceeds its size limit")
    return candidate


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise CandidateAuditError("output symlinks are not allowed")
    candidate = unresolved.resolve()
    if not candidate.is_relative_to(root):
        raise CandidateAuditError("output path is outside the workspace")
    return candidate


def _protect_cell(value: str) -> str:
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _restore_cell(value: str) -> str:
    if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
        return value[1:]
    return value
