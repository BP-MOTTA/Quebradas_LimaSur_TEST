"""Offline audit for spatial false negatives in the INDECI filter."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import AUDIT_FIELDS
from quebradas_limaeste.inventory.document_classification import semantic_text
from quebradas_limaeste.inventory.full_ingestion import ALL_DOCUMENT_FIELDS
from quebradas_limaeste.inventory.limaeste_filter import (
    DOCUMENT_FILTER_FIELDS,
    FILTER_CONFIG,
    FILTER_DOCUMENTS_OUTPUT,
    FilterPolicy,
    TermDefinition,
    load_filter_policy,
)

SPATIAL_AUDIT_OUTPUT = Path("metadata/indeci/spatial_false_negative_audit.csv")
SPATIAL_AUDIT_SUMMARY_OUTPUT = Path("metadata/indeci/spatial_audit_summary.json")
SPATIAL_AUDIT_FIELDS = (
    "document_id",
    "location_term",
    "location_group",
    "found_in_title",
    "found_in_matched_terms",
    "found_in_candidate",
    "found_in_evidence",
    "found_in_full_text",
    "current_detected_location",
    "current_spatial_relevance",
    "current_priority",
    "false_negative_suspected",
    "reason",
)
EXPECTED_DOCUMENTS = 131
MAX_CSV_BYTES = 20_000_000
MAX_CANDIDATES = 5_000
_SPATIAL_GROUPS = ("core", "near", "comparison")
_DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class SpatialAuditError(RuntimeError):
    """Raised when local audit inputs violate their bounded contract."""


def detect_normalized_locations(
    value: str,
    *,
    policy: FilterPolicy,
) -> tuple[str, ...]:
    """Find configured locations after conservative text normalization."""
    normalized = semantic_text(value)
    definitions = _location_definitions(policy)
    matches: list[tuple[int, int, int, str]] = []
    for order, (_, definition) in enumerate(definitions):
        for alias in definition.aliases:
            normalized_alias = semantic_text(alias)
            matches.extend(
                (match.start(), match.end(), order, definition.canonical)
                for match in re.finditer(
                    rf"(?<!\w){re.escape(normalized_alias)}(?!\w)", normalized
                )
            )
    accepted: list[tuple[int, int, int, str]] = []
    for match in sorted(
        matches,
        key=lambda item: (-(item[1] - item[0]), item[2], item[0]),
    ):
        if any(match[0] < end and match[1] > start for start, end, _, _ in accepted):
            continue
        accepted.append(match)
    return tuple(dict.fromkeys(item[3] for item in sorted(accepted)))


def execute_spatial_false_negative_audit(
    *,
    documents_path: Path,
    candidates_path: Path,
    filtered_documents_path: Path = FILTER_DOCUMENTS_OUTPUT,
    config_path: Path = FILTER_CONFIG,
    audit_output: Path = SPATIAL_AUDIT_OUTPUT,
    summary_output: Path = SPATIAL_AUDIT_SUMMARY_OUTPUT,
    allowed_root: Path,
    expected_documents: int = EXPECTED_DOCUMENTS,
) -> dict[str, object]:
    """Compare every configured location with all existing evidence sources."""
    root = Path(allowed_root).resolve()
    policy = load_filter_policy(config_path, allowed_root=root)
    documents_source = _safe_input(documents_path, root=root, maximum=MAX_CSV_BYTES)
    candidates_source = _safe_input(candidates_path, root=root, maximum=MAX_CSV_BYTES)
    filtered_source = _safe_input(
        filtered_documents_path, root=root, maximum=MAX_CSV_BYTES
    )
    documents, document_fields = _read_csv(documents_source)
    candidates, candidate_fields = _read_csv(candidates_source)
    filtered_documents, filtered_fields = _read_csv(filtered_source)
    if document_fields != ALL_DOCUMENT_FIELDS:
        raise SpatialAuditError("all_documents CSV columns do not match schema")
    if candidate_fields != AUDIT_FIELDS:
        raise SpatialAuditError("candidate CSV columns do not match schema")
    if filtered_fields != DOCUMENT_FILTER_FIELDS:
        raise SpatialAuditError("filtered document CSV columns do not match schema")
    if (
        len(documents) != expected_documents
        or len(filtered_documents) != expected_documents
    ):
        raise SpatialAuditError(
            "source and filtered inventories must contain the expected documents"
        )
    if len(candidates) > MAX_CANDIDATES:
        raise SpatialAuditError("candidate input exceeds row limit")
    document_ids = [row["document_id"] for row in documents]
    filtered_ids = [row["document_id"] for row in filtered_documents]
    candidate_ids = [row["candidate_id"] for row in candidates]
    if len(document_ids) != len(set(document_ids)):
        raise SpatialAuditError("source inventory contains duplicate document IDs")
    if len(filtered_ids) != len(set(filtered_ids)):
        raise SpatialAuditError("filtered inventory contains duplicate document IDs")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise SpatialAuditError("candidate inventory contains duplicate candidate IDs")
    if set(document_ids) != set(filtered_ids):
        raise SpatialAuditError("source and filtered document IDs do not match")
    known_documents = set(document_ids)
    if any(row["source_document_id"] not in known_documents for row in candidates):
        raise SpatialAuditError("candidate references an unknown document")
    if any(row["validation_status"] != "pending_review" for row in candidates):
        raise SpatialAuditError("candidate decisions must remain pending_review")
    if any(
        row["spatial_relevance"] not in {"core", "near", "comparison", "low", "unknown"}
        or row["review_priority"] not in {"P1", "P2", "P3", "P4", "PX"}
        for row in filtered_documents
    ):
        raise SpatialAuditError("filtered spatial relevance or priority is invalid")

    audit_destination = _safe_output(audit_output, root=root)
    summary_destination = _safe_output(summary_output, root=root)
    sources = {documents_source, candidates_source, filtered_source}
    if (
        audit_destination == summary_destination
        or {
            audit_destination,
            summary_destination,
        }
        & sources
    ):
        raise SpatialAuditError("audit outputs must be distinct from source files")

    filtered_by_id = {row["document_id"]: row for row in filtered_documents}
    candidates_by_document: dict[str, list[dict[str, str]]] = defaultdict(list)
    for candidate in candidates:
        candidates_by_document[candidate["source_document_id"]].append(candidate)
    definitions = _location_definitions(policy)
    rows: list[dict[str, str]] = []
    for document in documents:
        document_id = document["document_id"]
        filtered = filtered_by_id[document_id]
        document_candidates = candidates_by_document[document_id]
        evidence_sources = _evidence_sources(
            document,
            candidates=document_candidates,
            full_text=_read_extracted_text(
                document_id,
                year=document["year"],
                root=root,
                maximum=policy.max_text_bytes,
            ),
        )
        detections = {
            name: set(detect_normalized_locations(value, policy=policy))
            for name, value in evidence_sources.items()
        }
        current = set(filter(None, filtered["detected_locations"].split("|")))
        for group, definition in definitions:
            canonical = definition.canonical
            found = {
                name: canonical in detected for name, detected in detections.items()
            }
            present = any(found.values())
            suspected = present and canonical not in current
            rows.append(
                {
                    "document_id": document_id,
                    "location_term": canonical,
                    "location_group": group,
                    "found_in_title": _boolean(found["title"]),
                    "found_in_matched_terms": _boolean(found["matched_terms"]),
                    "found_in_candidate": _boolean(found["candidate"]),
                    "found_in_evidence": _boolean(found["evidence"]),
                    "found_in_full_text": _boolean(found["full_text"]),
                    "current_detected_location": filtered["primary_location"],
                    "current_spatial_relevance": filtered["spatial_relevance"],
                    "current_priority": filtered["review_priority"],
                    "false_negative_suspected": _boolean(suspected),
                    "reason": _audit_reason(
                        present=present,
                        suspected=suspected,
                        canonical=canonical,
                        primary_location=filtered["primary_location"],
                        current=current,
                    ),
                }
            )

    summary = _summary(
        rows,
        filtered_documents=filtered_documents,
        definitions=definitions,
    )
    _write_csv(audit_destination, rows)
    _write_json(summary_destination, summary)
    false_negative_count = len(summary["false_negatives"])
    return {
        "documents_total": len(documents),
        "locations_searched": len(definitions),
        "audit_rows": len(rows),
        "false_negatives": false_negative_count,
        "audit_output": str(audit_destination),
        "summary_output": str(summary_destination),
    }


def _evidence_sources(
    document: dict[str, str],
    *,
    candidates: list[dict[str, str]],
    full_text: str,
) -> dict[str, str]:
    matched_terms = [*_parse_terms(document["matched_terms"])]
    matched_terms.extend(
        term
        for candidate in candidates
        for term in _parse_terms(candidate["matched_terms"])
    )
    return {
        "title": document["title"],
        "matched_terms": " ".join(matched_terms),
        "candidate": " ".join(
            candidate[field]
            for candidate in candidates
            for field in ("reported_quebrada", "site_evidence", "canonical_site_id")
            if candidate[field]
        ),
        "evidence": " ".join(
            candidate["evidence_snippet"]
            for candidate in candidates
            if candidate["evidence_snippet"]
        ),
        "full_text": full_text,
    }


def _audit_reason(
    *,
    present: bool,
    suspected: bool,
    canonical: str,
    primary_location: str,
    current: set[str],
) -> str:
    if suspected:
        return "detector_false_negative"
    if not present and canonical in current:
        return "current_detection_not_reproduced"
    if not present:
        return "not_present_in_document"
    if canonical == primary_location:
        return "detected_primary"
    return "detected_non_primary"


def _summary(
    rows: list[dict[str, str]],
    *,
    filtered_documents: list[dict[str, str]],
    definitions: tuple[tuple[str, TermDefinition], ...],
) -> dict[str, object]:
    searched = [definition.canonical for _, definition in definitions]
    present = sorted(
        {
            row["location_term"]
            for row in rows
            if any(row[field] == "true" for field in SPATIAL_AUDIT_FIELDS[3:8])
        }
    )
    false_negatives = [
        {
            "document_id": row["document_id"],
            "location_term": row["location_term"],
            "reason": row["reason"],
        }
        for row in rows
        if row["false_negative_suspected"] == "true"
    ]
    spatial = Counter(row["spatial_relevance"] for row in filtered_documents)
    core_near = [
        row
        for row in filtered_documents
        if row["spatial_relevance"] in {"core", "near"}
    ]
    prioritized = [row for row in core_near if row["review_priority"] in {"P1", "P2"}]
    nonpriority = [row for row in core_near if row not in prioritized]
    nonpriority_causes = Counter(_nonpriority_cause(row) for row in nonpriority)
    nonpriority_events = Counter(row["primary_event"] for row in nonpriority)
    golden = {}
    for location in ("JICAMARCA", "HUAYCOLORO", "QUIRIO", "PEDREGAL", "CIENEGUILLA"):
        location_rows = [
            row
            for row in rows
            if row["location_term"] == location
            and any(row[field] == "true" for field in SPATIAL_AUDIT_FIELDS[3:8])
        ]
        location_false_negatives = [
            row for row in location_rows if row["false_negative_suspected"] == "true"
        ]
        status = "not_present_in_corpus"
        if location_rows:
            status = (
                "detector_false_negative"
                if location_false_negatives
                else "present_and_detected"
            )
        golden[location] = {
            "status": status,
            "documents": sorted({row["document_id"] for row in location_rows}),
        }
    return {
        "locations_searched": searched,
        "locations_present": present,
        "locations_absent": sorted(set(searched) - set(present)),
        "false_negatives": false_negatives,
        "documents_core": spatial["core"],
        "documents_near": spatial["near"],
        "documents_comparison": spatial["comparison"],
        "documents_low": spatial["low"],
        "documents_unknown": spatial["unknown"],
        "core_near_documents": len(core_near),
        "core_near_priority_p1_p2": len(prioritized),
        "core_near_not_p1_p2": len(nonpriority),
        "core_near_nonpriority_causes": dict(sorted(nonpriority_causes.items())),
        "core_near_nonpriority_events": dict(sorted(nonpriority_events.items())),
        "rainfall_relation_blocks_priority": 0,
        "location_rule_failures": nonpriority_causes["location_rule"],
        "priority_rule_inconsistencies": nonpriority_causes[
            "priority_rule_inconsistency"
        ],
        "core_near_review": [
            {
                "document_id": row["document_id"],
                "spatial_relevance": row["spatial_relevance"],
                "event_relevance": row["event_relevance"],
                "rainfall_related": row["rainfall_related"],
                "review_priority": row["review_priority"],
                "outcome_reason": (
                    "priority_p1_p2" if row in prioritized else _nonpriority_cause(row)
                ),
            }
            for row in core_near
        ],
        "golden_spatial_checks": golden,
    }


def _nonpriority_cause(row: dict[str, str]) -> str:
    if row["event_relevance"] == "excluded_topic":
        return "excluded_topic"
    if row["event_relevance"] != "target":
        return "absence_of_target_event"
    if row["spatial_relevance"] not in {"core", "near"}:
        return "location_rule"
    return "priority_rule_inconsistency"


def _location_definitions(
    policy: FilterPolicy,
) -> tuple[tuple[str, TermDefinition], ...]:
    return tuple(
        (group, definition)
        for group in _SPATIAL_GROUPS
        for definition in policy.spatial_terms[group]
    )


def _parse_terms(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value[1:] if value.startswith("'") else value or "[]")
    except json.JSONDecodeError as exc:
        raise SpatialAuditError("matched_terms must be valid JSON") from exc
    if not isinstance(payload, list) or any(
        not isinstance(item, str) for item in payload
    ):
        raise SpatialAuditError("matched_terms must be a list of strings")
    return tuple(payload)


def _read_extracted_text(
    document_id: str,
    *,
    year: str,
    root: Path,
    maximum: int,
) -> str:
    if not _DOCUMENT_ID_PATTERN.fullmatch(document_id) or not re.fullmatch(
        r"\d{4}", year
    ):
        raise SpatialAuditError("document text path contains an invalid identifier")
    path = root / "data/interim/indeci" / year / f"{document_id}.txt"
    if not path.exists():
        return ""
    source = _safe_input(path, root=root, maximum=maximum)
    try:
        return source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise SpatialAuditError("extracted text must be valid UTF-8") from exc


def _read_csv(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            fields = tuple(reader.fieldnames or ())
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise SpatialAuditError(f"could not read CSV: {path.name}") from exc
    if any(None in row for row in rows):
        raise SpatialAuditError(f"CSV has rows wider than its header: {path.name}")
    return rows, fields


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    if temporary.exists() or temporary.is_symlink():
        raise SpatialAuditError("audit CSV staging file already exists")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(
                output,
                fieldnames=SPATIAL_AUDIT_FIELDS,
                extrasaction="raise",
                quoting=csv.QUOTE_ALL,
                lineterminator="\n",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        field: _spreadsheet_safe(row.get(field, ""))
                        for field in SPATIAL_AUDIT_FIELDS
                    }
                )
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    if temporary.exists() or temporary.is_symlink():
        raise SpatialAuditError("audit JSON staging file already exists")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _spreadsheet_safe(value: str) -> str:
    value = _CONTROL_CHARACTERS.sub("", value)
    return f"'{value}" if value.startswith(("=", "+", "-", "@")) else value


def _boolean(value: bool) -> str:
    return str(value).lower()


def _safe_input(path: Path, *, root: Path, maximum: int) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise SpatialAuditError("input symlinks are not allowed")
    try:
        target = unresolved.resolve(strict=True)
    except OSError as exc:
        raise SpatialAuditError(f"input does not exist: {path}") from exc
    if not target.is_relative_to(root) or not target.is_file():
        raise SpatialAuditError("input is outside the workspace or not a file")
    if target.stat().st_size > maximum:
        raise SpatialAuditError("input exceeds its size limit")
    return target


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = path if path.is_absolute() else root / path
    if unresolved.is_symlink():
        raise SpatialAuditError("output symlinks are not allowed")
    target = unresolved.resolve()
    if not target.is_relative_to(root):
        raise SpatialAuditError("output is outside the workspace")
    return target
