"""Deterministic metadata triage for bounded INDECI batch ingestion."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.discovery import CSV_FIELDS, PILOT_YEARS
from quebradas_limaeste.inventory.discovery_models import (
    DiscoveryCandidate,
    DiscoveryConfigError,
    normalized_title,
)
from quebradas_limaeste.inventory.document_classification import (
    contains_term,
    semantic_text,
)

BATCH_SELECTION_OUTPUT = Path("metadata/indeci/batch_selection.csv")
SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
MAX_CONFIG_BYTES = 65_536
MAX_DISCOVERY_CSV_BYTES = 10_000_000
MAX_DISCOVERY_ROWS = 5_000
SELECTION_FIELDS = (
    "batch_id",
    "document_id",
    "discovery_id",
    "year",
    "report_type",
    "report_number",
    "report_date",
    "title",
    "source_connector",
    "detail_url",
    "pdf_url",
    "triage_tier",
    "triage_score",
    "triage_reasons",
    "golden_control",
    "selected",
    "selection_reason",
)
_DOCUMENT_TYPE_CODES = {
    "reporte_complementario": "RC",
    "reporte_preliminar": "RP",
    "informe_emergencia": "IE",
}
_DISCOVERY_ID_PATTERN = re.compile(r"indeci-discovery-[0-9a-f]{20}")


class BatchPolicyError(ValueError):
    """Raised when batch configuration or selection input is unsafe."""


@dataclass(frozen=True)
class BatchPolicy:
    """Bounded, explicit policy for the first controlled batch."""

    max_documents: int
    max_per_year: int
    allowed_years: tuple[int, ...]
    high_priority_terms: tuple[str, ...]
    medium_priority_terms: tuple[str, ...]
    regional_terms: tuple[str, ...]
    event_terms: tuple[str, ...]


@dataclass(frozen=True)
class TriageResult:
    """Ordering evidence only; it is not a scientific classification."""

    tier: str
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class _CandidateRow:
    discovery_id: str
    candidate: DiscoveryCandidate


@dataclass(frozen=True)
class DiscoveryDocument:
    """Validated discovery metadata with its stable downstream identity."""

    document_id: str
    discovery_id: str
    candidate: DiscoveryCandidate
    golden_control: str | None


def load_batch_policy(path: Path, *, allowed_root: Path) -> BatchPolicy:
    """Load the strict bounded batch block from JSON-compatible YAML."""
    root = Path(allowed_root).resolve()
    target = _safe_input(path, root=root, max_bytes=MAX_CONFIG_BYTES)
    if target.suffix.lower() not in {".yaml", ".yml"}:
        raise BatchPolicyError("config must use a .yaml or .yml suffix")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BatchPolicyError("config must be UTF-8 JSON-compatible YAML") from exc
    expected_root = {
        "portal_url",
        "request_delay_seconds",
        "timeout_seconds",
        "retry_backoff_seconds",
        "max_attempts",
        "max_pages_per_query",
        "queries",
        "ingestion",
        "discovery",
        "batch",
    }
    if not isinstance(raw, dict) or set(raw) != expected_root:
        raise BatchPolicyError("config keys do not match the batch schema")
    discovery = raw["discovery"]
    if not isinstance(discovery, dict):
        raise BatchPolicyError("discovery block must be an object")
    allowed_years = _integer_tuple(discovery.get("allowed_years"), "allowed_years")
    if allowed_years != PILOT_YEARS:
        raise BatchPolicyError("batch must remain within the four pilot years")

    batch = raw["batch"]
    expected_batch = {
        "max_documents",
        "max_per_year",
        "priority_terms",
        "event_terms",
    }
    if not isinstance(batch, dict) or set(batch) != expected_batch:
        raise BatchPolicyError("batch block has invalid keys")
    max_documents = _bounded_integer(
        batch["max_documents"], "max_documents", minimum=2, maximum=25
    )
    max_per_year = _bounded_integer(
        batch["max_per_year"],
        "max_per_year",
        minimum=1,
        maximum=max_documents,
    )
    priorities = batch["priority_terms"]
    if not isinstance(priorities, dict) or set(priorities) != {
        "high",
        "medium",
        "regional",
    }:
        raise BatchPolicyError("priority_terms has invalid keys")
    return BatchPolicy(
        max_documents=max_documents,
        max_per_year=max_per_year,
        allowed_years=allowed_years,
        high_priority_terms=_term_tuple(priorities["high"], "priority_terms.high"),
        medium_priority_terms=_term_tuple(
            priorities["medium"], "priority_terms.medium"
        ),
        regional_terms=_term_tuple(priorities["regional"], "priority_terms.regional"),
        event_terms=_term_tuple(batch["event_terms"], "event_terms"),
    )


def load_discovery_universe(
    path: Path,
    *,
    config_path: Path = SOURCE_CONFIG,
    allowed_root: Path,
) -> tuple[DiscoveryDocument, ...]:
    """Load one immutable discovery snapshot and derive stable document IDs."""
    root = Path(allowed_root).resolve()
    policy = load_batch_policy(config_path, allowed_root=root)
    rows = _load_discovery_rows(path, root=root, policy=policy)
    document_ids = _document_ids(rows)
    return tuple(
        DiscoveryDocument(
            document_id=document_ids[row.discovery_id],
            discovery_id=row.discovery_id,
            candidate=row.candidate,
            golden_control=_control_name(row.candidate),
        )
        for row in sorted(rows, key=_output_key)
    )


def score_title(title: str, *, policy: BatchPolicy) -> TriageResult:
    """Score title metadata for review order without inferring relevance."""
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise BatchPolicyError("title must be a non-empty string up to 500 characters")
    text = semantic_text(title)
    high = _matched_terms(text, policy.high_priority_terms)
    medium = _matched_terms(text, policy.medium_priority_terms)
    regional = _matched_terms(text, policy.regional_terms)
    events = _matched_terms(text, policy.event_terms)
    if high and events:
        tier, base, location = "A", 400, high
    elif medium and events:
        tier, base, location = "B", 300, medium
    elif regional and events:
        tier, base, location = "C", 200, regional
    else:
        reasons = []
        if high:
            reasons.append(f"high_location:{high[0]}")
        elif medium:
            reasons.append(f"medium_location:{medium[0]}")
        elif regional:
            reasons.append(f"regional_location:{regional[0]}")
        if events:
            reasons.append(f"event:{events[0]}")
        reasons.append("low_preliminary_evidence")
        return TriageResult(
            tier="D",
            score=10 * bool(events) + 5 * bool(high or medium or regional),
            reasons=tuple(reasons),
        )
    reasons = (
        f"tier_{tier.lower()}_location:{location[0]}",
        *(f"event:{term}" for term in events),
    )
    return TriageResult(
        tier=tier,
        score=base + 10 * len(location) + len(events),
        reasons=tuple(reasons),
    )


def execute_select_batch(
    discovery_path: Path,
    *,
    config_path: Path = SOURCE_CONFIG,
    output_path: Path = BATCH_SELECTION_OUTPUT,
    max_documents: int | None = None,
    allowed_root: Path,
) -> dict[str, object]:
    """Select a deterministic year-balanced batch and publish its audit CSV."""
    root = Path(allowed_root).resolve()
    policy = load_batch_policy(config_path, allowed_root=root)
    requested = policy.max_documents if max_documents is None else max_documents
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise BatchPolicyError("max_documents must be a positive integer")
    if requested > policy.max_documents:
        raise BatchPolicyError("max_documents exceeds the configured maximum")
    rows = _load_discovery_rows(discovery_path, root=root, policy=policy)
    controls = _golden_controls(rows)
    if set(controls) != {"positive_control", "negative_ambiguous_control"}:
        raise BatchPolicyError("both golden controls must be available for selection")
    if requested < len(controls):
        raise BatchPolicyError("max_documents is too small for the golden controls")

    triage = {
        row.discovery_id: score_title(row.candidate.title, policy=policy)
        for row in rows
    }
    selected = {row.discovery_id for row in controls.values()}
    selected_reason = {
        row.discovery_id: f"forced_{name}" for name, row in controls.items()
    }
    for year in policy.allowed_years:
        available = [
            row
            for row in rows
            if row.candidate.year == year
            and row.candidate.pdf_url is not None
            and row.discovery_id not in selected
        ]
        available.sort(key=lambda row: _selection_key(row, triage[row.discovery_id]))
        for row in available[: policy.max_per_year]:
            if len(selected) >= requested:
                break
            selected.add(row.discovery_id)
            selected_reason[row.discovery_id] = "selected_within_year_quota"

    batch_id = _batch_id(rows, policy=policy, max_documents=requested)
    document_ids = _document_ids(rows)
    output_rows: list[dict[str, object]] = []
    for row in sorted(rows, key=_output_key):
        candidate = row.candidate
        result = triage[row.discovery_id]
        is_selected = row.discovery_id in selected
        reason = selected_reason.get(row.discovery_id)
        if reason is None:
            if candidate.pdf_url is None:
                reason = "missing_pdf_url"
            elif len(selected) >= requested:
                reason = "batch_limit_reached"
            else:
                reason = "year_quota_reached"
        output_rows.append(
            {
                "batch_id": batch_id,
                "document_id": document_ids[row.discovery_id],
                "discovery_id": row.discovery_id,
                "year": candidate.year,
                "report_type": candidate.report_type or "",
                "report_number": candidate.report_number or "",
                "report_date": (
                    candidate.report_date.isoformat() if candidate.report_date else ""
                ),
                "title": candidate.title,
                "source_connector": candidate.source_connector,
                "detail_url": candidate.detail_url or "",
                "pdf_url": candidate.pdf_url or "",
                "triage_tier": result.tier,
                "triage_score": result.score,
                "triage_reasons": json.dumps(
                    result.reasons, ensure_ascii=True, separators=(",", ":")
                ),
                "golden_control": _control_name(candidate) or "",
                "selected": str(is_selected).lower(),
                "selection_reason": reason,
            }
        )
    target = _safe_output(output_path, root=root)
    _write_selection(target, output_rows)
    chosen = [row for row in output_rows if row["selected"] == "true"]
    return {
        "batch_id": batch_id,
        "documents_available": len(rows),
        "documents_selected": len(chosen),
        "selected_by_year": dict(
            sorted(Counter(str(row["year"]) for row in chosen).items())
        ),
        "selected_by_tier": dict(
            sorted(Counter(str(row["triage_tier"]) for row in chosen).items())
        ),
        "selection_output": str(target.relative_to(root)),
    }


def _load_discovery_rows(
    path: Path,
    *,
    root: Path,
    policy: BatchPolicy,
) -> tuple[_CandidateRow, ...]:
    target = _safe_input(path, root=root, max_bytes=MAX_DISCOVERY_CSV_BYTES)
    try:
        with target.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != CSV_FIELDS:
                raise BatchPolicyError("discovery CSV columns do not match the schema")
            parsed = []
            for index, row in enumerate(reader, start=2):
                if None in row:
                    raise BatchPolicyError(
                        f"discovery CSV row {index} has extra columns"
                    )
                if len(parsed) >= MAX_DISCOVERY_ROWS:
                    raise BatchPolicyError("discovery CSV exceeds the candidate limit")
                parsed.append(_parse_discovery_row(row, index=index, policy=policy))
    except (csv.Error, UnicodeDecodeError) as exc:
        raise BatchPolicyError("discovery CSV is not valid UTF-8 CSV") from exc
    if not parsed:
        raise BatchPolicyError("discovery CSV contains no candidates")
    identifiers = [row.discovery_id for row in parsed]
    if len(set(identifiers)) != len(identifiers):
        raise BatchPolicyError("discovery CSV contains duplicate discovery_id values")
    return tuple(parsed)


def _parse_discovery_row(
    raw: dict[str, str | None],
    *,
    index: int,
    policy: BatchPolicy,
) -> _CandidateRow:
    try:
        row = {key: _restore_cell(value or "") for key, value in raw.items()}
        discovery_id = row["discovery_id"]
        if _DISCOVERY_ID_PATTERN.fullmatch(discovery_id) is None:
            raise BatchPolicyError("discovery_id has an invalid format")
        year = int(row["year"])
        if year not in policy.allowed_years:
            raise BatchPolicyError("candidate year is outside the pilot set")
        report_date = (
            date.fromisoformat(row["report_date"]) if row["report_date"] else None
        )
        discovered_at = datetime.fromisoformat(
            row["discovered_at_utc"].replace("Z", "+00:00")
        )
        candidate = DiscoveryCandidate.create(
            source_connector=row["source_connector"],
            title=row["title"],
            detail_url=row["detail_url"] or None,
            pdf_url=row["pdf_url"] or None,
            report_type=row["report_type"] or None,
            report_number=row["report_number"] or None,
            report_date=report_date,
            year=year,
            discovered_at_utc=discovered_at,
            query_context=_json_object(row["query_context"], "query_context"),
            raw_metadata=_json_object(row["raw_metadata"], "raw_metadata"),
            discovery_warnings=_json_string_list(
                row["discovery_warnings"], "discovery_warnings"
            ),
            discovery_sources_attempted=_json_string_list(
                row["discovery_sources_attempted"], "discovery_sources_attempted"
            ),
            discovery_sources_matched=_json_string_list(
                row["discovery_sources_matched"], "discovery_sources_matched"
            ),
            dedup_status=row["dedup_status"],
            dedup_reason=row["dedup_reason"],
        )
        if candidate.discovery_id != discovery_id:
            raise BatchPolicyError("discovery_id does not match candidate identity")
    except (BatchPolicyError, DiscoveryConfigError, ValueError, TypeError) as exc:
        raise BatchPolicyError(f"invalid discovery row {index}: {exc}") from exc
    return _CandidateRow(discovery_id=discovery_id, candidate=candidate)


def _golden_controls(rows: tuple[_CandidateRow, ...]) -> dict[str, _CandidateRow]:
    controls: dict[str, _CandidateRow] = {}
    for row in sorted(rows, key=_output_key):
        name = _control_name(row.candidate)
        if name and row.candidate.pdf_url is not None:
            if name in controls:
                raise BatchPolicyError(f"multiple {name} candidates were found")
            controls[name] = row
    return controls


def _control_name(candidate: DiscoveryCandidate) -> str | None:
    identity = (candidate.report_type, candidate.report_number, candidate.report_date)
    if identity == ("reporte_complementario", "630", date(2019, 3, 3)):
        return "positive_control"
    if identity == ("informe_emergencia", "1496", date(2023, 5, 5)):
        return "negative_ambiguous_control"
    return None


def _document_ids(rows: tuple[_CandidateRow, ...]) -> dict[str, str]:
    result: dict[str, str] = {}
    used: set[str] = set()
    for row in sorted(rows, key=_output_key):
        candidate = row.candidate
        if candidate.report_type and candidate.report_number and candidate.report_date:
            code = _DOCUMENT_TYPE_CODES[candidate.report_type]
            base = (
                f"INDECI_{code}{candidate.report_number}_"
                f"{candidate.report_date.strftime('%Y%m%d')}"
            )
        else:
            identity = candidate.pdf_url or candidate.detail_url or row.discovery_id
            digest = sha256(identity.encode("utf-8")).hexdigest()[:16].upper()
            base = f"INDECI_DISC_{digest}"
        document_id = base
        if document_id in used:
            digest = sha256(row.discovery_id.encode("ascii")).hexdigest()[:8].upper()
            document_id = f"{base}_{digest}"
        if document_id in used:
            raise BatchPolicyError("candidate document identities are not unique")
        used.add(document_id)
        result[row.discovery_id] = document_id
    return result


def _matched_terms(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    matches: list[str] = []
    normalized: set[str] = set()
    for term in terms:
        signal = semantic_text(term)
        if signal not in normalized and contains_term(text, term):
            normalized.add(signal)
            matches.append(term)
    return tuple(matches)


def _selection_key(row: _CandidateRow, triage: TriageResult) -> tuple[object, ...]:
    return (
        {"A": 0, "B": 1, "C": 2, "D": 3}[triage.tier],
        -triage.score,
        normalized_title(row.candidate.title),
        row.discovery_id,
    )


def _output_key(row: _CandidateRow) -> tuple[object, ...]:
    return (
        row.candidate.year,
        normalized_title(row.candidate.title),
        row.discovery_id,
    )


def _batch_id(
    rows: tuple[_CandidateRow, ...],
    *,
    policy: BatchPolicy,
    max_documents: int,
) -> str:
    payload = {
        "candidate_ids": sorted(row.discovery_id for row in rows),
        "event_terms": policy.event_terms,
        "high_priority_terms": policy.high_priority_terms,
        "max_documents": max_documents,
        "max_per_year": policy.max_per_year,
        "medium_priority_terms": policy.medium_priority_terms,
        "regional_terms": policy.regional_terms,
    }
    digest = sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return f"indeci-batch-{digest}"


def _write_selection(path: Path, rows: list[dict[str, object]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=SELECTION_FIELDS,
        lineterminator="\n",
        quoting=csv.QUOTE_ALL,
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _protect_cell(str(row[key])) for key in SELECTION_FIELDS})
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as output:
        output.write(buffer.getvalue())
        output.flush()
        os.fsync(output.fileno())
        staged = Path(output.name)
    try:
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)


def _json_object(value: str, field: str) -> dict[str, object]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise BatchPolicyError(f"{field} must contain a JSON object")
    return parsed


def _json_string_list(value: str, field: str) -> list[str]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise BatchPolicyError(f"{field} must contain a JSON string list")
    return parsed


def _integer_tuple(value: object, field: str) -> tuple[int, ...]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise BatchPolicyError(f"{field} must contain integers")
    if len(set(value)) != len(value):
        raise BatchPolicyError(f"{field} must not contain duplicates")
    return tuple(sorted(value))


def _bounded_integer(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BatchPolicyError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise BatchPolicyError(f"{field} is outside the allowed range")
    return value


def _term_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise BatchPolicyError(f"{field} must contain 1-24 terms")
    terms = []
    for item in value:
        if not isinstance(item, str):
            raise BatchPolicyError(f"{field} must contain strings")
        clean = re.sub(r"\s+", " ", item).strip()
        if not clean or len(clean) > 100:
            raise BatchPolicyError(f"{field} contains an invalid term")
        terms.append(clean)
    return tuple(terms)


def _safe_input(path: Path, *, root: Path, max_bytes: int) -> Path:
    unresolved = Path(path) if Path(path).is_absolute() else root / path
    if unresolved.is_symlink():
        raise BatchPolicyError("input symlinks are not allowed")
    target = unresolved.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise BatchPolicyError("input path is outside the workspace or missing")
    if target.stat().st_size > max_bytes:
        raise BatchPolicyError("input file exceeds its size limit")
    return target


def _safe_output(path: Path, *, root: Path) -> Path:
    unresolved = Path(path) if Path(path).is_absolute() else root / path
    if unresolved.is_symlink():
        raise BatchPolicyError("output symlinks are not allowed")
    target = unresolved.resolve()
    if not target.is_relative_to(root):
        raise BatchPolicyError("output path is outside the workspace")
    return target


def _protect_cell(value: str) -> str:
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _restore_cell(value: str) -> str:
    if value.startswith("'") and value[1:].lstrip().startswith(("=", "+", "-", "@")):
        return value[1:]
    return value
