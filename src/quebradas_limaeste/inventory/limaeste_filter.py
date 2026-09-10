"""Offline geographic and event relevance filter for INDECI records."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_FIELDS,
    CONSOLIDATED_FIELDS,
)
from quebradas_limaeste.inventory.document_classification import semantic_text
from quebradas_limaeste.inventory.full_ingestion import ALL_DOCUMENT_FIELDS

FILTER_CONFIG = Path("configs/sources/indeci_limaeste_filter.yaml")
FILTER_DOCUMENTS_OUTPUT = Path("metadata/indeci/geographic_event_filter_documents.csv")
FILTER_CANDIDATES_OUTPUT = Path(
    "metadata/indeci/geographic_event_filter_candidates.csv"
)
GEOGRAPHIC_SUMMARY_OUTPUT = Path("metadata/indeci/geographic_summary.csv")
GEOGRAPHIC_SUMMARY_TEXT_OUTPUT = Path("metadata/indeci/geographic_summary.txt")
EXPECTED_DOCUMENTS = 131
MAX_CSV_BYTES = 20_000_000
MAX_CANDIDATES = 5_000
MAX_CLUSTERS = 5_000

DOCUMENT_FILTER_FIELDS = (
    "document_id",
    "year",
    "title",
    "report_type",
    "report_number",
    "relevance_status",
    "spatial_relevance",
    "event_relevance",
    "rainfall_related",
    "review_priority",
    "priority_reason",
    "detected_locations",
    "detected_event_terms",
    "candidate_count",
    "strong_count",
    "moderate_count",
    "weak_count",
    "review_required",
    "matched_terms",
    "primary_location",
    "primary_event",
    "event_date",
    "report_date",
    "review_report_type",
    "review_report_number",
    "relevant_pages",
    "raw_local_path",
    "sha256",
    "local_pdf_status",
    "golden_control",
)
CANDIDATE_FILTER_FIELDS = (
    "candidate_id",
    "event_cluster_id",
    "document_id",
    "event_date",
    "event_type",
    "reported_quebrada",
    "source_page",
    "evidence_snippet",
    "candidate_strength",
    "spatial_relevance",
    "event_relevance",
    "rainfall_related",
    "review_priority",
    "priority_reason",
)
GEOGRAPHIC_SUMMARY_FIELDS = (
    "location",
    "year",
    "event_type",
    "review_priority",
    "document_count",
    "candidate_count",
)

_SPATIAL_LEVELS = ("core", "near", "comparison", "low")
_PRIORITIES = ("P1", "P2", "P3", "P4", "PX")
_EVENT_LEVELS = ("target", "possible_target", "excluded_topic", "unknown")
_RAINFALL_LEVELS = ("true", "false", "unknown")
_CANONICAL_PATTERN = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*\Z")
_DOCUMENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]+\Z")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_STRENGTH_RANK = {"": -1, "weak": 0, "moderate": 1, "strong": 2}


class LimaEsteFilterError(RuntimeError):
    """Raised when filter inputs or outputs violate the offline contract."""


@dataclass(frozen=True)
class TermDefinition:
    canonical: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class FilterPolicy:
    spatial_terms: dict[str, tuple[TermDefinition, ...]]
    target_event_terms: tuple[TermDefinition, ...]
    excluded_topic_terms: tuple[TermDefinition, ...]
    rainfall_terms: tuple[str, ...]
    response_terms: tuple[str, ...]
    causal_markers: tuple[str, ...]
    negative_relation_phrases: tuple[str, ...]
    context_window_characters: int
    max_text_bytes: int


@dataclass(frozen=True)
class FilterEvidence:
    candidate_fields: tuple[str, ...] = ()
    evidence_snippets: tuple[str, ...] = ()
    matched_terms: tuple[str, ...] = ()
    full_text: str = ""
    title: str = ""
    text_available: bool = False


@dataclass(frozen=True)
class FilterDecision:
    spatial_relevance: str
    event_relevance: str
    rainfall_related: str
    review_priority: str
    priority_reason: str
    detected_locations: tuple[str, ...]
    detected_event_terms: tuple[str, ...]
    primary_location: str
    primary_event: str


def load_filter_policy(
    path: Path = FILTER_CONFIG,
    *,
    allowed_root: Path,
) -> FilterPolicy:
    """Load a bounded, JSON-compatible YAML policy from the repository."""
    source = _safe_input(path, root=Path(allowed_root).resolve(), maximum=65_536)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LimaEsteFilterError("filter config must be valid UTF-8 JSON") from exc
    expected = {
        "spatial_terms",
        "target_event_terms",
        "excluded_topic_terms",
        "rainfall_terms",
        "response_terms",
        "causal_markers",
        "negative_relation_phrases",
        "context_window_characters",
        "max_text_bytes",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise LimaEsteFilterError("filter config keys do not match the schema")
    spatial_payload = payload["spatial_terms"]
    if (
        not isinstance(spatial_payload, dict)
        or tuple(spatial_payload) != _SPATIAL_LEVELS
    ):
        raise LimaEsteFilterError(
            "spatial_terms must define core, near, comparison, low"
        )
    spatial_terms = {
        level: _term_definitions(spatial_payload[level], label="location")
        for level in _SPATIAL_LEVELS
    }
    all_locations = [
        term.canonical for level in _SPATIAL_LEVELS for term in spatial_terms[level]
    ]
    if len(all_locations) != len(set(all_locations)):
        raise LimaEsteFilterError("spatial locations must be unique")
    context = payload["context_window_characters"]
    maximum = payload["max_text_bytes"]
    if not isinstance(context, int) or not 40 <= context <= 2_000:
        raise LimaEsteFilterError("context_window_characters is out of bounds")
    if not isinstance(maximum, int) or not 1_024 <= maximum <= 20_000_000:
        raise LimaEsteFilterError("max_text_bytes is out of bounds")
    return FilterPolicy(
        spatial_terms=spatial_terms,
        target_event_terms=_term_definitions(
            payload["target_event_terms"], label="event"
        ),
        excluded_topic_terms=_term_definitions(
            payload["excluded_topic_terms"], label="event"
        ),
        rainfall_terms=_string_terms(payload["rainfall_terms"], "rainfall_terms"),
        response_terms=_string_terms(payload["response_terms"], "response_terms"),
        causal_markers=_string_terms(payload["causal_markers"], "causal_markers"),
        negative_relation_phrases=_string_terms(
            payload["negative_relation_phrases"], "negative_relation_phrases"
        ),
        context_window_characters=context,
        max_text_bytes=maximum,
    )


def evaluate_relevance(
    evidence: FilterEvidence,
    *,
    policy: FilterPolicy,
) -> FilterDecision:
    """Classify untrusted evidence with transparent deterministic rules."""
    sources = (
        " ".join(evidence.candidate_fields),
        " ".join(evidence.evidence_snippets),
        " ".join(evidence.matched_terms),
        evidence.full_text,
        evidence.title,
    )
    location_definitions = tuple(
        term for level in _SPATIAL_LEVELS for term in policy.spatial_terms[level]
    )
    locations_by_source = tuple(
        _detected_terms(value, location_definitions) for value in sources
    )
    detected_locations = _unique(
        item for matches in locations_by_source for item in matches
    )
    location_levels = {
        term.canonical: level
        for level, definitions in policy.spatial_terms.items()
        for term in definitions
    }
    ranked_locations = (
        (
            _SPATIAL_LEVELS.index(location_levels[location]),
            source_index,
            location_index,
            location,
        )
        for source_index, matches in enumerate(locations_by_source)
        for location_index, location in enumerate(matches)
    )
    primary_location = min(ranked_locations, default=(0, 0, 0, "UNKNOWN"))[3]
    if primary_location != "UNKNOWN":
        spatial_relevance = location_levels[primary_location]
    elif evidence.text_available or any(sources):
        spatial_relevance = "low"
    else:
        spatial_relevance = "unknown"

    targets = tuple(
        _detected_terms(value, policy.target_event_terms) for value in sources
    )
    excluded = tuple(
        _detected_terms(value, policy.excluded_topic_terms) for value in sources
    )
    detected_event_terms = _unique(
        item
        for source_index in range(len(sources))
        for item in (*targets[source_index], *excluded[source_index])
    )
    event_relevance = _event_relevance(targets, excluded)
    if event_relevance == "excluded_topic":
        primary_event = next(
            (item for matches in excluded for item in matches), "UNKNOWN"
        )
    else:
        primary_event = next(
            (item for matches in targets for item in matches), "UNKNOWN"
        )
    rainfall_related = _rainfall_relation(evidence, policy=policy)
    priority, reason = _review_priority(spatial_relevance, event_relevance)
    return FilterDecision(
        spatial_relevance=spatial_relevance,
        event_relevance=event_relevance,
        rainfall_related=rainfall_related,
        review_priority=priority,
        priority_reason=reason,
        detected_locations=detected_locations,
        detected_event_terms=detected_event_terms,
        primary_location=primary_location,
        primary_event=primary_event,
    )


def execute_limaeste_filter(
    *,
    documents_path: Path,
    candidates_path: Path,
    clusters_path: Path,
    config_path: Path = FILTER_CONFIG,
    documents_output: Path = FILTER_DOCUMENTS_OUTPUT,
    candidates_output: Path = FILTER_CANDIDATES_OUTPUT,
    summary_output: Path = GEOGRAPHIC_SUMMARY_OUTPUT,
    summary_text_output: Path = GEOGRAPHIC_SUMMARY_TEXT_OUTPUT,
    allowed_root: Path,
    expected_documents: int = EXPECTED_DOCUMENTS,
) -> dict[str, object]:
    """Apply the relevance policy to the complete offline inventory."""
    root = Path(allowed_root).resolve()
    policy = load_filter_policy(config_path, allowed_root=root)
    document_source = _safe_input(documents_path, root=root, maximum=MAX_CSV_BYTES)
    candidate_source = _safe_input(candidates_path, root=root, maximum=MAX_CSV_BYTES)
    cluster_source = _safe_input(clusters_path, root=root, maximum=MAX_CSV_BYTES)
    documents, document_fields = _read_csv(document_source)
    candidates, candidate_fields = _read_csv(candidate_source)
    clusters, cluster_fields = _read_csv(cluster_source)
    if document_fields != ALL_DOCUMENT_FIELDS:
        raise LimaEsteFilterError("all_documents CSV columns do not match schema")
    if candidate_fields != AUDIT_FIELDS or cluster_fields != CONSOLIDATED_FIELDS:
        raise LimaEsteFilterError(
            "candidate or cluster CSV columns do not match schema"
        )
    if len(documents) != expected_documents:
        raise LimaEsteFilterError(
            f"filter expected {expected_documents} documents; found {len(documents)}"
        )
    if len(candidates) > MAX_CANDIDATES or len(clusters) > MAX_CLUSTERS:
        raise LimaEsteFilterError("candidate or cluster input exceeds row limit")
    document_ids = [row["document_id"] for row in documents]
    if len(document_ids) != len(set(document_ids)):
        raise LimaEsteFilterError("all_documents contains duplicate document_id values")
    if any(row["validation_status"] != "pending_review" for row in candidates):
        raise LimaEsteFilterError("candidate decisions must remain pending_review")
    if any(row["validation_status"] != "pending_review" for row in clusters):
        raise LimaEsteFilterError("cluster decisions must remain pending_review")
    known_documents = set(document_ids)
    documents_by_id = {row["document_id"]: row for row in documents}
    if any(row["source_document_id"] not in known_documents for row in candidates):
        raise LimaEsteFilterError("candidate references an unknown document")
    candidates_by_document: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in candidates:
        candidates_by_document[row["source_document_id"]].append(row)
    cluster_by_candidate = _cluster_index(clusters, candidates=candidates)

    destinations = tuple(
        _safe_output(path, root=root)
        for path in (
            documents_output,
            candidates_output,
            summary_output,
            summary_text_output,
        )
    )
    if len(set(destinations)) != len(destinations):
        raise LimaEsteFilterError("filter outputs must use different paths")
    if any(
        path in {document_source, candidate_source, cluster_source}
        for path in destinations
    ):
        raise LimaEsteFilterError("filter outputs must not overwrite source files")

    decisions: dict[str, tuple[FilterDecision, str]] = {}
    filtered_documents: list[dict[str, object]] = []
    for document in documents:
        document_id = document["document_id"]
        document_candidates = candidates_by_document[document_id]
        full_text = _read_extracted_text(
            document_id,
            year=document["year"],
            root=root,
            maximum=policy.max_text_bytes,
        )
        matched = _parse_terms(document["matched_terms"])
        evidence = _document_evidence(
            document,
            candidates=document_candidates,
            matched_terms=matched,
            full_text=full_text,
        )
        decision = evaluate_relevance(evidence, policy=policy)
        local_pdf_status = _local_pdf_status(document, root=root)
        decisions[document_id] = (decision, full_text or "")
        filtered_documents.append(
            _document_output_row(
                document,
                candidates=document_candidates,
                decision=decision,
                matched_terms=matched,
                local_pdf_status=local_pdf_status,
            )
        )

    filtered_candidates = []
    for candidate in candidates:
        document_id = candidate["source_document_id"]
        document = documents_by_id[document_id]
        _, full_text = decisions[document_id]
        matched = _unique(
            (
                *_parse_terms(document["matched_terms"]),
                *_parse_terms(candidate["matched_terms"]),
            )
        )
        candidate_decision = evaluate_relevance(
            _candidate_evidence(
                document,
                candidate=candidate,
                matched_terms=matched,
                full_text=full_text,
            ),
            policy=policy,
        )
        filtered_candidates.append(
            _candidate_output_row(
                candidate,
                cluster_id=cluster_by_candidate.get(candidate["candidate_id"], ""),
                decision=candidate_decision,
            )
        )

    summary_rows = _summary_rows(filtered_documents)
    _write_csv(destinations[0], DOCUMENT_FILTER_FIELDS, filtered_documents)
    _write_csv(destinations[1], CANDIDATE_FILTER_FIELDS, filtered_candidates)
    _write_csv(destinations[2], GEOGRAPHIC_SUMMARY_FIELDS, summary_rows)
    _write_text(destinations[3], _summary_text(filtered_documents))
    priority_counts = Counter(row["review_priority"] for row in filtered_documents)
    spatial_counts = Counter(row["spatial_relevance"] for row in filtered_documents)
    event_counts = Counter(row["event_relevance"] for row in filtered_documents)
    rainfall_counts = Counter(row["rainfall_related"] for row in filtered_documents)
    return {
        "documents_total": len(filtered_documents),
        "candidates_total": len(filtered_candidates),
        "priorities": {key: priority_counts[key] for key in _PRIORITIES},
        "spatial_relevance": {
            key: spatial_counts[key] for key in (*_SPATIAL_LEVELS, "unknown")
        },
        "event_relevance": {key: event_counts[key] for key in _EVENT_LEVELS},
        "rainfall_related": {key: rainfall_counts[key] for key in _RAINFALL_LEVELS},
        "missing_local_pdf": sum(
            row["local_pdf_status"] == "missing" for row in filtered_documents
        ),
        "outputs": {
            "documents": str(destinations[0]),
            "candidates": str(destinations[1]),
            "summary": str(destinations[2]),
            "summary_text": str(destinations[3]),
        },
    }


def _event_relevance(
    targets: tuple[tuple[str, ...], ...],
    excluded: tuple[tuple[str, ...], ...],
) -> str:
    if targets[0] or targets[1]:
        return "target"
    if excluded[0] or excluded[1] or excluded[4]:
        return "excluded_topic"
    if targets[2]:
        return "target"
    if excluded[2]:
        return "excluded_topic"
    if targets[3] or targets[4]:
        return "possible_target"
    if excluded[3]:
        return "excluded_topic"
    return "unknown"


def _review_priority(spatial: str, event: str) -> tuple[str, str]:
    if event == "excluded_topic":
        return "PX", "principal topic is explicitly excluded"
    if spatial in {"low", "unknown"}:
        return "PX", f"spatial relevance is {spatial}"
    if event == "target":
        priority = {"core": "P1", "near": "P2", "comparison": "P3"}[spatial]
        return priority, f"{spatial} location with target event evidence"
    return "P4", f"{spatial} location with {event} event evidence"


def _rainfall_relation(evidence: FilterEvidence, *, policy: FilterPolicy) -> str:
    states = {
        state
        for value in (*evidence.evidence_snippets, evidence.full_text, evidence.title)
        if (state := _rainfall_state(value, policy=policy)) != "unknown"
    }
    if states == {"true"}:
        return "true"
    if states == {"false"}:
        return "false"
    return "unknown"


def _rainfall_state(value: str, *, policy: FilterPolicy) -> str:
    normalized = semantic_text(value)
    if not normalized:
        return "unknown"
    if any(_term_spans(normalized, term) for term in policy.negative_relation_phrases):
        return "false"
    rainfall = [
        span for term in policy.rainfall_terms for span in _term_spans(normalized, term)
    ]
    responses = [
        span for term in policy.response_terms for span in _term_spans(normalized, term)
    ]
    markers = [
        span for term in policy.causal_markers for span in _term_spans(normalized, term)
    ]
    for rain_start, rain_end in rainfall:
        for response_start, response_end in responses:
            if (
                min(abs(rain_start - response_end), abs(response_start - rain_end))
                > policy.context_window_characters
            ):
                continue
            left = min(rain_start, response_start) - 80
            right = max(rain_end, response_end) + 80
            if any(start >= left and end <= right for start, end in markers):
                return "true"
    return "unknown"


def _detected_terms(
    value: str,
    definitions: tuple[TermDefinition, ...],
) -> tuple[str, ...]:
    normalized = semantic_text(value)
    matches: list[tuple[int, int, int, str]] = []
    for order, definition in enumerate(definitions):
        for alias in definition.aliases:
            matches.extend(
                (start, end, order, definition.canonical)
                for start, end in _term_spans(normalized, alias)
            )
    accepted: list[tuple[int, int, int, str]] = []
    for match in sorted(
        matches, key=lambda item: (-(item[1] - item[0]), item[2], item[0])
    ):
        if any(match[0] < end and match[1] > start for start, end, _, _ in accepted):
            continue
        accepted.append(match)
    return _unique(item[3] for item in sorted(accepted))


def _term_spans(normalized_text: str, term: str) -> tuple[tuple[int, int], ...]:
    normalized_term = semantic_text(term)
    if not normalized_term:
        return ()
    return tuple(
        match.span()
        for match in re.finditer(
            rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized_text
        )
    )


def _document_evidence(
    document: dict[str, str],
    *,
    candidates: list[dict[str, str]],
    matched_terms: tuple[str, ...],
    full_text: str | None,
) -> FilterEvidence:
    return FilterEvidence(
        candidate_fields=tuple(
            row[field]
            for row in candidates
            for field in (
                "reported_quebrada",
                "site_evidence",
                "event_type",
                "event_evidence",
            )
            if row[field]
        ),
        evidence_snippets=tuple(
            row["evidence_snippet"] for row in candidates if row["evidence_snippet"]
        ),
        matched_terms=matched_terms,
        full_text=full_text or "",
        title=document["title"],
        text_available=full_text is not None,
    )


def _candidate_evidence(
    document: dict[str, str],
    *,
    candidate: dict[str, str],
    matched_terms: tuple[str, ...],
    full_text: str,
) -> FilterEvidence:
    return FilterEvidence(
        candidate_fields=tuple(
            candidate[field]
            for field in (
                "reported_quebrada",
                "site_evidence",
                "event_type",
                "event_evidence",
            )
            if candidate[field]
        ),
        evidence_snippets=(candidate["evidence_snippet"],),
        matched_terms=matched_terms,
        full_text=full_text,
        title=document["title"],
        text_available=bool(full_text),
    )


def _document_output_row(
    document: dict[str, str],
    *,
    candidates: list[dict[str, str]],
    decision: FilterDecision,
    matched_terms: tuple[str, ...],
    local_pdf_status: str,
) -> dict[str, object]:
    strengths = Counter(row["candidate_strength"] for row in candidates)
    best = max(
        candidates,
        key=lambda row: (
            _STRENGTH_RANK.get(row["candidate_strength"], -1),
            row["event_date"],
            row["candidate_id"],
        ),
        default=None,
    )
    pages = sorted(
        {int(row["source_page"]) for row in candidates if row["source_page"].isdigit()}
    )
    return {
        "document_id": document["document_id"],
        "year": document["year"],
        "title": document["title"],
        "report_type": document["report_type"],
        "report_number": document["report_number"],
        "relevance_status": document["relevance_status"],
        "spatial_relevance": decision.spatial_relevance,
        "event_relevance": decision.event_relevance,
        "rainfall_related": decision.rainfall_related,
        "review_priority": decision.review_priority,
        "priority_reason": decision.priority_reason,
        "detected_locations": "|".join(decision.detected_locations),
        "detected_event_terms": "|".join(decision.detected_event_terms),
        "candidate_count": len(candidates),
        "strong_count": strengths["strong"],
        "moderate_count": strengths["moderate"],
        "weak_count": strengths["weak"],
        "review_required": document["review_required"],
        "matched_terms": document["matched_terms"],
        "primary_location": decision.primary_location,
        "primary_event": decision.primary_event,
        "event_date": best["event_date"] if best else document["review_event_date"],
        "report_date": document["report_date"],
        "review_report_type": document["review_report_type"],
        "review_report_number": document["review_report_number"],
        "relevant_pages": "|".join(map(str, pages)),
        "raw_local_path": document["raw_local_path"],
        "sha256": document["sha256"],
        "local_pdf_status": local_pdf_status,
        "golden_control": document["golden_control"],
    }


def _candidate_output_row(
    candidate: dict[str, str],
    *,
    cluster_id: str,
    decision: FilterDecision,
) -> dict[str, object]:
    return {
        "candidate_id": candidate["candidate_id"],
        "event_cluster_id": cluster_id,
        "document_id": candidate["source_document_id"],
        "event_date": candidate["event_date"],
        "event_type": candidate["event_type"],
        "reported_quebrada": candidate["reported_quebrada"],
        "source_page": candidate["source_page"],
        "evidence_snippet": candidate["evidence_snippet"],
        "candidate_strength": candidate["candidate_strength"],
        "spatial_relevance": decision.spatial_relevance,
        "event_relevance": decision.event_relevance,
        "rainfall_related": decision.rainfall_related,
        "review_priority": decision.review_priority,
        "priority_reason": decision.priority_reason,
    }


def _cluster_index(
    clusters: list[dict[str, str]],
    *,
    candidates: list[dict[str, str]],
) -> dict[str, str]:
    known = {row["candidate_id"] for row in candidates}
    result: dict[str, str] = {}
    for cluster in clusters:
        for candidate_id in filter(None, cluster["supporting_candidates"].split("|")):
            if candidate_id not in known:
                raise LimaEsteFilterError("cluster references an unknown candidate")
            if candidate_id in result:
                raise LimaEsteFilterError("candidate belongs to multiple clusters")
            result[candidate_id] = cluster["event_cluster_id"]
    return result


def _summary_rows(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for row in documents:
        key = tuple(
            str(row[field])
            for field in (
                "primary_location",
                "year",
                "primary_event",
                "review_priority",
            )
        )
        counts[key][0] += 1
        counts[key][1] += int(row["candidate_count"])
    return [
        {
            "location": key[0],
            "year": key[1],
            "event_type": key[2],
            "review_priority": key[3],
            "document_count": values[0],
            "candidate_count": values[1],
        }
        for key, values in sorted(counts.items())
    ]


def _summary_text(documents: list[dict[str, object]]) -> str:
    main_locations = (
        "CUSIPATA",
        "SAN-BARTOLOME",
        "CHACLACAYO",
        "LURIGANCHO-CHOSICA",
        "QUIRIO",
        "PEDREGAL",
        "HUAYCOLORO",
        "JICAMARCA",
        "CIENEGUILLA",
    )
    document_counts = Counter(str(row["primary_location"]) for row in documents)
    candidate_counts = Counter()
    for row in documents:
        candidate_counts[str(row["primary_location"])] += int(row["candidate_count"])
    lines = ["RESUMEN GEOGRAFICO INDECI / LIMA ESTE", ""]
    for location in main_locations:
        lines.extend(
            (
                location,
                f"- documents: {document_counts[location]}",
                f"- event candidates: {candidate_counts[location]}",
                "",
            )
        )
    other_documents = sum(
        count
        for location, count in document_counts.items()
        if location not in main_locations
    )
    other_candidates = sum(
        count
        for location, count in candidate_counts.items()
        if location not in main_locations
    )
    lines.extend(
        (
            "OTHER LOCATIONS",
            f"- documents: {other_documents}",
            f"- event candidates: {other_candidates}",
        )
    )
    lines.extend(("", "PRIORIDADES"))
    priorities = Counter(str(row["review_priority"]) for row in documents)
    lines.extend(f"{priority}: {priorities[priority]}" for priority in _PRIORITIES)
    return "\n".join(lines) + "\n"


def _local_pdf_status(document: dict[str, str], *, root: Path) -> str:
    value = document["raw_local_path"]
    if not value:
        return "missing"
    raw_root = (root / "data/raw/indeci").resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise LimaEsteFilterError("raw PDF must not be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(raw_root):
        raise LimaEsteFilterError("raw PDF escapes data/raw/indeci")
    if not resolved.is_file():
        return "missing"
    return "available"


def _read_extracted_text(
    document_id: str,
    *,
    year: str,
    root: Path,
    maximum: int,
) -> str | None:
    if not _DOCUMENT_ID_PATTERN.fullmatch(document_id) or not re.fullmatch(
        r"\d{4}", year
    ):
        raise LimaEsteFilterError("document text path contains an invalid identifier")
    path = root / "data/interim/indeci" / year / f"{document_id}.txt"
    if not path.exists():
        return None
    source = _safe_input(path, root=root, maximum=maximum)
    try:
        return source.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise LimaEsteFilterError("extracted text must be valid UTF-8") from exc


def _term_definitions(value: object, *, label: str) -> tuple[TermDefinition, ...]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise LimaEsteFilterError(f"{label} terms must be a non-empty bounded list")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {label, "aliases"}:
            raise LimaEsteFilterError(f"invalid {label} term definition")
        canonical = item[label]
        if not isinstance(canonical, str) or not _CANONICAL_PATTERN.fullmatch(
            canonical
        ):
            raise LimaEsteFilterError(f"invalid canonical {label}")
        aliases = _string_terms(item["aliases"], f"{label} aliases", deduplicate=True)
        result.append(TermDefinition(canonical=canonical, aliases=aliases))
    canonicals = [term.canonical for term in result]
    if len(canonicals) != len(set(canonicals)):
        raise LimaEsteFilterError(f"canonical {label} terms must be unique")
    return tuple(result)


def _string_terms(
    value: object,
    label: str,
    *,
    deduplicate: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise LimaEsteFilterError(f"{label} must be a non-empty bounded list")
    if any(
        not isinstance(item, str) or not item.strip() or len(item) > 120
        for item in value
    ):
        raise LimaEsteFilterError(f"{label} contains an invalid value")
    normalized = tuple(semantic_text(item) for item in value)
    if len(normalized) != len(set(normalized)) and not deduplicate:
        raise LimaEsteFilterError(f"{label} contains duplicate values")
    if not deduplicate:
        return tuple(value)
    return tuple(dict(zip(normalized, value, strict=True)).values())


def _parse_terms(value: str) -> tuple[str, ...]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError as exc:
        raise LimaEsteFilterError("matched_terms must be valid JSON") from exc
    if not isinstance(payload, list) or any(
        not isinstance(item, str) for item in payload
    ):
        raise LimaEsteFilterError("matched_terms must be a list of strings")
    return tuple(payload)


def _read_csv(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            fields = tuple(reader.fieldnames or ())
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as exc:
        raise LimaEsteFilterError(f"could not read CSV: {path.name}") from exc
    if any(None in row for row in rows):
        raise LimaEsteFilterError(
            f"CSV contains rows wider than its header: {path.name}"
        )
    return rows, fields


def _write_csv(
    path: Path,
    fields: tuple[str, ...],
    rows: list[dict[str, object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    if temporary.exists() or temporary.is_symlink():
        raise LimaEsteFilterError(f"staging file already exists: {temporary.name}")
    try:
        with temporary.open("x", newline="", encoding="utf-8") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="raise")
            writer.writeheader()
            writer.writerows(
                {field: _spreadsheet_safe(row.get(field, "")) for field in fields}
                for row in rows
            )
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.part")
    if temporary.exists() or temporary.is_symlink():
        raise LimaEsteFilterError(f"staging file already exists: {temporary.name}")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _spreadsheet_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    value = _CONTROL_CHARACTERS.sub("", value)
    if value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _safe_input(path: Path, *, root: Path, maximum: int) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise LimaEsteFilterError("input must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise LimaEsteFilterError(f"input does not exist: {path}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise LimaEsteFilterError("input must be a regular file inside allowed_root")
    if resolved.stat().st_size > maximum:
        raise LimaEsteFilterError(f"input exceeds size limit: {path}")
    return resolved


def _safe_output(path: Path, *, root: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise LimaEsteFilterError("output must not be a symbolic link")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise LimaEsteFilterError("output escapes allowed_root")
    return resolved


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
