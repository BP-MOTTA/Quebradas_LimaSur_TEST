"""Controlled multi-source discovery for public INDECI document metadata."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from quebradas_limaeste.inventory.discovery_models import (
    DiscoveryCandidate,
    DiscoveryConfigError,
    DiscoveryConnector,
    canonical_document_url,
    normalized_title,
)
from quebradas_limaeste.inventory.live_smoke import PortalQuery

PILOT_YEARS = (2017, 2019, 2023, 2024)
MAX_DISCOVERY_QUERIES = 16
MAX_CONFIG_BYTES = 65_536
CANDIDATES_OUTPUT = Path("metadata/indeci/discovery_candidates.csv")
RUN_OUTPUT = Path("metadata/indeci/discovery_run.json")
CSV_FIELDS = (
    "discovery_id",
    "source_connector",
    "title",
    "detail_url",
    "pdf_url",
    "report_type",
    "report_number",
    "report_date",
    "year",
    "discovered_at_utc",
    "query_context",
    "raw_metadata",
    "discovery_warnings",
    "discovery_sources_attempted",
    "discovery_sources_matched",
    "dedup_status",
    "dedup_reason",
)


@dataclass(frozen=True)
class DiscoveryConfig:
    """Strict bounded configuration for the pilot discovery run."""

    allowed_years: tuple[int, ...]
    max_pages_per_query: int
    queries: tuple[PortalQuery, ...]
    seed_config: Path
    request_delay_seconds: float
    timeout_seconds: float
    retry_backoff_seconds: float
    max_attempts: int


@dataclass(frozen=True)
class DiscoveryRun:
    """Complete in-memory discovery result and its run-level evidence."""

    run_id: str
    started_at_utc: datetime
    finished_at_utc: datetime
    years: tuple[int, ...]
    candidates: tuple[DiscoveryCandidate, ...]
    connector_attempts: tuple[dict[str, object], ...]
    requests: int
    pages: int
    candidates_raw: int
    duplicates: int
    golden_rc630_discovered: bool
    golden_ie1496_discovered: bool
    golden_ie1496_available_as_seed: bool
    golden_rc630_connectors: tuple[str, ...]
    golden_ie1496_connectors: tuple[str, ...]
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    candidates_by_year: dict[str, int]
    candidates_by_connector: dict[str, int]

    def to_manifest(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "started_at_utc": self.started_at_utc.isoformat(),
            "finished_at_utc": self.finished_at_utc.isoformat(),
            "years": list(self.years),
            "connectors_attempted": list(self.connector_attempts),
            "requests": self.requests,
            "pages": self.pages,
            "candidates_raw": self.candidates_raw,
            "candidates_unique": len(self.candidates),
            "duplicates": self.duplicates,
            "candidates_by_year": self.candidates_by_year,
            "candidates_by_connector": self.candidates_by_connector,
            "candidate_sources": [
                {
                    "discovery_id": candidate.discovery_id,
                    "sources_attempted": list(candidate.discovery_sources_attempted),
                    "sources_matched": list(candidate.discovery_sources_matched),
                }
                for candidate in self.candidates
            ],
            "golden_rc630_discovered": self.golden_rc630_discovered,
            "golden_rc630_connectors": list(self.golden_rc630_connectors),
            "golden_ie1496_discovered": self.golden_ie1496_discovered,
            "golden_ie1496_connectors": list(self.golden_ie1496_connectors),
            "golden_ie1496_available_as_seed": (self.golden_ie1496_available_as_seed),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def load_discovery_config(path: Path, *, allowed_root: Path) -> DiscoveryConfig:
    """Load a strict JSON-compatible YAML discovery configuration."""
    config_path = _safe_input(path, root=Path(allowed_root).resolve())
    if config_path.suffix.lower() not in {".yaml", ".yml"}:
        raise DiscoveryConfigError("config must use a .yaml or .yml suffix")
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise DiscoveryConfigError("config file is too large")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DiscoveryConfigError("config must be UTF-8 JSON-compatible YAML") from exc
    if not isinstance(raw, dict):
        raise DiscoveryConfigError("config root must be an object")

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
    if set(raw) != expected_root:
        raise DiscoveryConfigError("config keys do not match the discovery schema")
    discovery = raw["discovery"]
    expected_discovery = {
        "allowed_years",
        "max_pages_per_query",
        "queries",
        "seed_config",
    }
    if not isinstance(discovery, dict) or set(discovery) != expected_discovery:
        raise DiscoveryConfigError("discovery block has invalid keys")

    allowed_years = _year_tuple(discovery["allowed_years"])
    if allowed_years != PILOT_YEARS:
        raise DiscoveryConfigError("allowed_years must match the four pilot years")
    max_pages = discovery["max_pages_per_query"]
    if isinstance(max_pages, bool) or max_pages != 1:
        raise DiscoveryConfigError("discovery max_pages_per_query must be 1")
    queries = _load_queries(discovery["queries"], allowed_years=allowed_years)
    seed_config = _relative_path(discovery["seed_config"], field="seed_config")
    _bounded_number(raw["request_delay_seconds"], "request_delay_seconds", 0.5, 30)
    _bounded_number(raw["timeout_seconds"], "timeout_seconds", 1, 60)
    _bounded_number(raw["retry_backoff_seconds"], "retry_backoff_seconds", 0.1, 30)
    max_attempts = raw["max_attempts"]
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
        raise DiscoveryConfigError("max_attempts must be an integer")
    if not 1 <= max_attempts <= 3:
        raise DiscoveryConfigError("max_attempts is outside the allowed range")
    return DiscoveryConfig(
        allowed_years=allowed_years,
        max_pages_per_query=max_pages,
        queries=queries,
        seed_config=seed_config,
        request_delay_seconds=float(raw["request_delay_seconds"]),
        timeout_seconds=float(raw["timeout_seconds"]),
        retry_backoff_seconds=float(raw["retry_backoff_seconds"]),
        max_attempts=max_attempts,
    )


def deduplicate_candidates(
    candidates: Sequence[DiscoveryCandidate],
) -> tuple[tuple[DiscoveryCandidate, ...], int]:
    """Converge strong matches while preserving conflicting records."""
    unique: list[DiscoveryCandidate] = []
    duplicates = 0
    for candidate in candidates:
        pdf_match = _first_index(
            unique, lambda item, current=candidate: _same_pdf(item, current)
        )
        if pdf_match is not None:
            unique[pdf_match] = _merge(
                unique[pdf_match], candidate, "canonical_pdf_url"
            )
            duplicates += 1
            continue

        identity_matches = [
            index
            for index, item in enumerate(unique)
            if _report_identity(item) is not None
            and _report_identity(item) == _report_identity(candidate)
        ]
        if identity_matches:
            conflicts = [
                index
                for index in identity_matches
                if _conflicting_pdf_urls(unique[index], candidate)
            ]
            if conflicts:
                reason = "conflicting_pdf_urls_same_report_identity"
                for index in conflicts:
                    unique[index] = _mark_ambiguous(unique[index], reason)
                unique.append(_mark_ambiguous(candidate, reason))
                continue
            unique[identity_matches[0]] = _merge(
                unique[identity_matches[0]],
                candidate,
                "report_identity",
            )
            duplicates += 1
            continue

        title_matches = [
            index
            for index, item in enumerate(unique)
            if item.year == candidate.year
            and normalized_title(item.title) == normalized_title(candidate.title)
        ]
        if title_matches:
            index = title_matches[0]
            existing = unique[index]
            if _conflicting_pdf_urls(existing, candidate):
                reason = "conflicting_pdf_urls_same_normalized_title"
                unique[index] = _mark_ambiguous(existing, reason)
                unique.append(_mark_ambiguous(candidate, reason))
                continue
            existing_identity = _report_identity(existing)
            candidate_identity = _report_identity(candidate)
            if (
                existing_identity is not None
                and candidate_identity is not None
                and existing_identity != candidate_identity
            ):
                reason = "conflicting_report_identity_same_normalized_title"
                unique[index] = _mark_ambiguous(existing, reason)
                unique.append(_mark_ambiguous(candidate, reason))
                continue
            unique[index] = _merge(existing, candidate, "normalized_title_secondary")
            duplicates += 1
            continue
        unique.append(candidate)

    return tuple(sorted(unique, key=_candidate_sort_key)), duplicates


def run_discovery(
    *,
    years: tuple[int, ...],
    connectors: Sequence[DiscoveryConnector],
    allowed_years: tuple[int, ...] = PILOT_YEARS,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DiscoveryRun:
    """Run each connector sequentially; one connector failure remains local."""
    selected_years = _validate_requested_years(years, allowed=allowed_years)
    connector_names = tuple(_safe_connector_label(item.name) for item in connectors)
    if len(set(connector_names)) != len(connector_names):
        raise DiscoveryConfigError("connector names must be unique")
    started = _utc_now(now, "discovery start")
    raw_candidates: list[DiscoveryCandidate] = []
    attempts: list[dict[str, object]] = []
    warnings: list[str] = []
    errors: list[str] = []
    connector_counts: Counter[str] = Counter()
    requests = 0
    pages = 0

    for connector, name in zip(connectors, connector_names, strict=True):
        try:
            result = connector.discover(
                years=selected_years,
                discovered_at_utc=started,
            )
        except Exception as exc:
            message = _bounded_message(f"{name}: {exc}")
            errors.append(message)
            attempts.append(
                {
                    "connector": name,
                    "status": "failed",
                    "requests": 0,
                    "pages": 0,
                    "candidates": 0,
                    "warnings": [],
                    "errors": [message],
                }
            )
            continue

        if result.connector != name:
            raise DiscoveryConfigError("connector result name does not match connector")
        if result.requests < 0 or result.pages < 0:
            raise DiscoveryConfigError("connector counters cannot be negative")
        outside_years = [
            candidate.year
            for candidate in result.candidates
            if candidate.year not in selected_years
        ]
        if outside_years:
            raise DiscoveryConfigError(
                "connector returned a candidate outside requested years"
            )
        if any(
            candidate.source_connector != name
            or candidate.discovery_sources_matched != (name,)
            for candidate in result.candidates
        ):
            raise DiscoveryConfigError("connector returned invalid source provenance")
        raw_candidates.extend(result.candidates)
        connector_counts[name] += len(result.candidates)
        requests += result.requests
        pages += result.pages
        connector_warnings = [
            _bounded_message(f"{name}: {warning}") for warning in result.warnings
        ]
        connector_errors = [
            _bounded_message(f"{name}: {error}") for error in result.errors
        ]
        warnings.extend(connector_warnings)
        errors.extend(connector_errors)
        attempts.append(
            {
                "connector": name,
                "status": "partial" if connector_errors else "success",
                "requests": result.requests,
                "pages": result.pages,
                "candidates": len(result.candidates),
                "warnings": connector_warnings,
                "errors": connector_errors,
            }
        )

    unique, duplicates = deduplicate_candidates(raw_candidates)
    attempted_names = tuple(item["connector"] for item in attempts)
    enriched = tuple(
        _with_attempted(candidate, attempted_names) for candidate in unique
    )
    rc630_connectors = _golden_connectors(enriched, golden="rc630")
    ie1496_connectors = _golden_connectors(enriched, golden="ie1496")
    ie1496_seed = any(
        _is_golden(candidate, "ie1496")
        and "seed_discovery" in candidate.discovery_sources_matched
        for candidate in enriched
    )
    year_counts = Counter(str(candidate.year) for candidate in enriched)
    finished = _utc_now(now, "discovery finish")
    return DiscoveryRun(
        run_id=f"indeci-discovery-{started.strftime('%Y%m%dT%H%M%SZ')}",
        started_at_utc=started,
        finished_at_utc=finished,
        years=selected_years,
        candidates=enriched,
        connector_attempts=tuple(attempts),
        requests=requests,
        pages=pages,
        candidates_raw=len(raw_candidates),
        duplicates=duplicates,
        golden_rc630_discovered=bool(rc630_connectors),
        golden_ie1496_discovered=bool(ie1496_connectors),
        golden_ie1496_available_as_seed=ie1496_seed,
        golden_rc630_connectors=rc630_connectors,
        golden_ie1496_connectors=ie1496_connectors,
        warnings=tuple(warnings),
        errors=tuple(errors),
        candidates_by_year=dict(sorted(year_counts.items())),
        candidates_by_connector=dict(sorted(connector_counts.items())),
    )


def write_discovery_outputs(
    result: DiscoveryRun,
    *,
    candidates_output: Path,
    run_output: Path,
    allowed_root: Path,
) -> tuple[Path, Path]:
    """Atomically publish metadata-only CSV and JSON inside the workspace."""
    root = Path(allowed_root).resolve()
    csv_target = _safe_output(candidates_output, root=root)
    json_target = _safe_output(run_output, root=root)
    if csv_target == json_target:
        raise DiscoveryConfigError("discovery output paths must be different")

    csv_buffer = io.StringIO(newline="")
    writer = csv.DictWriter(csv_buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for candidate in result.candidates:
        row = candidate.to_dict()
        for field in (
            "query_context",
            "raw_metadata",
            "discovery_warnings",
            "discovery_sources_attempted",
            "discovery_sources_matched",
        ):
            row[field] = json.dumps(row[field], ensure_ascii=False, sort_keys=True)
        writer.writerow({key: _csv_safe(row[key]) for key in CSV_FIELDS})

    manifest_text = (
        json.dumps(result.to_manifest(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )
    csv_temp = _stage_text(csv_target, csv_buffer.getvalue())
    try:
        json_temp = _stage_text(json_target, manifest_text)
    except OSError:
        csv_temp.unlink(missing_ok=True)
        raise
    try:
        os.replace(csv_temp, csv_target)
        os.replace(json_temp, json_target)
    finally:
        csv_temp.unlink(missing_ok=True)
        json_temp.unlink(missing_ok=True)
    return csv_target, json_target


def execute_discovery(
    config_path: Path,
    *,
    years: tuple[int, ...],
    candidates_output: Path = CANDIDATES_OUTPUT,
    run_output: Path = RUN_OUTPUT,
    allowed_root: Path,
    connectors: Sequence[DiscoveryConnector] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Execute the metadata-only dry run and write its traceable outputs."""
    root = Path(allowed_root).resolve()
    config = load_discovery_config(config_path, allowed_root=root)
    selected_years = _validate_requested_years(years, allowed=config.allowed_years)
    active_connectors = (
        tuple(connectors)
        if connectors is not None
        else _default_connectors(config, config_path=Path(config_path), root=root)
    )
    result = run_discovery(years=selected_years, connectors=active_connectors, now=now)
    csv_path, manifest_path = write_discovery_outputs(
        result,
        candidates_output=candidates_output,
        run_output=run_output,
        allowed_root=root,
    )
    payload = result.to_manifest()
    payload["candidates_output"] = str(csv_path.relative_to(root))
    payload["run_output"] = str(manifest_path.relative_to(root))
    return payload


def _default_connectors(
    config: DiscoveryConfig,
    *,
    config_path: Path,
    root: Path,
) -> tuple[DiscoveryConnector, ...]:
    from quebradas_limaeste.inventory.archive_connector import (
        ArchiveDiscoverySettings,
    )
    from quebradas_limaeste.inventory.archive_emergencias import (
        ArchiveEmergenciasConnector,
    )
    from quebradas_limaeste.inventory.archive_informes import ArchiveInformesConnector
    from quebradas_limaeste.inventory.ingestion_models import (
        IngestionConfigError,
        load_ingestion_policy,
        load_seed_documents,
    )
    from quebradas_limaeste.inventory.seed_discovery import SeedDiscoveryConnector

    config_input = _safe_input(config_path, root=root)
    seed_input = _safe_input(config.seed_config, root=root)
    try:
        policy = load_ingestion_policy(config_input)
        seeds = load_seed_documents(seed_input, policy=policy)
    except IngestionConfigError as exc:
        raise DiscoveryConfigError(str(exc)) from exc
    settings = ArchiveDiscoverySettings(
        queries=config.queries,
        request_delay_seconds=config.request_delay_seconds,
        timeout_seconds=config.timeout_seconds,
        retry_backoff_seconds=config.retry_backoff_seconds,
        max_attempts=config.max_attempts,
        max_pages_per_query=config.max_pages_per_query,
    )
    return (
        ArchiveInformesConnector(settings),
        ArchiveEmergenciasConnector(settings),
        SeedDiscoveryConnector(seeds),
    )


def _merge(
    first: DiscoveryCandidate,
    second: DiscoveryCandidate,
    reason: str,
) -> DiscoveryCandidate:
    warnings = tuple(
        dict.fromkeys(first.discovery_warnings + second.discovery_warnings)
    )
    status = (
        "ambiguous"
        if "ambiguous" in {first.dedup_status, second.dedup_status}
        else "merged"
    )
    final_reason = first.dedup_reason if status == "ambiguous" else reason
    primary = first if first.pdf_url or not second.pdf_url else second
    return DiscoveryCandidate.create(
        source_connector=primary.source_connector,
        title=primary.title
        if len(primary.title) >= len(second.title)
        else second.title,
        detail_url=first.detail_url or second.detail_url,
        pdf_url=first.pdf_url or second.pdf_url,
        report_type=first.report_type or second.report_type,
        report_number=first.report_number or second.report_number,
        report_date=first.report_date or second.report_date,
        year=first.year,
        discovered_at_utc=min(first.discovered_at_utc, second.discovered_at_utc),
        query_context=_combined_observations(first, second, "query_context"),
        raw_metadata=_combined_observations(first, second, "raw_metadata"),
        discovery_warnings=warnings,
        discovery_sources_attempted=(
            first.discovery_sources_attempted + second.discovery_sources_attempted
        ),
        discovery_sources_matched=(
            first.discovery_sources_matched + second.discovery_sources_matched
        ),
        dedup_status=status,
        dedup_reason=final_reason,
    )


def _combined_observations(
    first: DiscoveryCandidate,
    second: DiscoveryCandidate,
    field: str,
) -> dict[str, object]:
    observations: list[dict[str, object]] = []
    for candidate in (first, second):
        value = getattr(candidate, field)
        if set(value) == {"observations"} and isinstance(value["observations"], list):
            observations.extend(value["observations"])
        else:
            observations.append(
                {"source_connector": candidate.source_connector, "value": value}
            )
    return {"observations": observations[:32]}


def _mark_ambiguous(
    candidate: DiscoveryCandidate,
    reason: str,
) -> DiscoveryCandidate:
    return DiscoveryCandidate.create(
        source_connector=candidate.source_connector,
        title=candidate.title,
        detail_url=candidate.detail_url,
        pdf_url=candidate.pdf_url,
        report_type=candidate.report_type,
        report_number=candidate.report_number,
        report_date=candidate.report_date,
        year=candidate.year,
        discovered_at_utc=candidate.discovered_at_utc,
        query_context=candidate.query_context,
        raw_metadata=candidate.raw_metadata,
        discovery_warnings=candidate.discovery_warnings,
        discovery_sources_attempted=candidate.discovery_sources_attempted,
        discovery_sources_matched=candidate.discovery_sources_matched,
        dedup_status="ambiguous",
        dedup_reason=reason,
    )


def _with_attempted(
    candidate: DiscoveryCandidate,
    attempted: tuple[str, ...],
) -> DiscoveryCandidate:
    return DiscoveryCandidate.create(
        source_connector=candidate.source_connector,
        title=candidate.title,
        detail_url=candidate.detail_url,
        pdf_url=candidate.pdf_url,
        report_type=candidate.report_type,
        report_number=candidate.report_number,
        report_date=candidate.report_date,
        year=candidate.year,
        discovered_at_utc=candidate.discovered_at_utc,
        query_context=candidate.query_context,
        raw_metadata=candidate.raw_metadata,
        discovery_warnings=candidate.discovery_warnings,
        discovery_sources_attempted=attempted,
        discovery_sources_matched=candidate.discovery_sources_matched,
        dedup_status=candidate.dedup_status,
        dedup_reason=candidate.dedup_reason,
    )


def _golden_connectors(
    candidates: tuple[DiscoveryCandidate, ...],
    *,
    golden: str,
) -> tuple[str, ...]:
    sources = {
        source
        for candidate in candidates
        if _is_golden(candidate, golden)
        for source in candidate.discovery_sources_matched
        if source != "seed_discovery"
    }
    return tuple(sorted(sources))


def _is_golden(candidate: DiscoveryCandidate, golden: str) -> bool:
    expected = {
        "rc630": ("630", "reporte_complementario", date(2019, 3, 3)),
        "ie1496": ("1496", "informe_emergencia", date(2023, 5, 5)),
    }[golden]
    return (
        candidate.report_number,
        candidate.report_type,
        candidate.report_date,
    ) == expected


def _report_identity(
    candidate: DiscoveryCandidate,
) -> tuple[str, str, date] | None:
    if candidate.report_type and candidate.report_number and candidate.report_date:
        return (
            candidate.report_type,
            candidate.report_number,
            candidate.report_date,
        )
    return None


def _conflicting_pdf_urls(
    first: DiscoveryCandidate,
    second: DiscoveryCandidate,
) -> bool:
    return bool(
        first.pdf_url
        and second.pdf_url
        and canonical_document_url(first.pdf_url)
        != canonical_document_url(second.pdf_url)
    )


def _same_pdf(first: DiscoveryCandidate, second: DiscoveryCandidate) -> bool:
    return bool(
        first.pdf_url
        and second.pdf_url
        and canonical_document_url(first.pdf_url)
        == canonical_document_url(second.pdf_url)
    )


def _first_index(
    values: list[DiscoveryCandidate],
    predicate: Callable[[DiscoveryCandidate], bool],
) -> int | None:
    return next((index for index, value in enumerate(values) if predicate(value)), None)


def _load_queries(
    value: object,
    *,
    allowed_years: tuple[int, ...],
) -> tuple[PortalQuery, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DISCOVERY_QUERIES:
        raise DiscoveryConfigError(
            f"discovery queries must contain 1-{MAX_DISCOVERY_QUERIES} entries"
        )
    queries: list[PortalQuery] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict) or set(item) != {"title", "year"}:
            raise DiscoveryConfigError(f"discovery query {index} has invalid keys")
        try:
            query = PortalQuery.create(title=item["title"], year=item["year"])
        except (TypeError, ValueError) as exc:
            raise DiscoveryConfigError(str(exc)) from exc
        if query.year not in allowed_years:
            raise DiscoveryConfigError(f"discovery query {index} uses a non-pilot year")
        queries.append(query)
    identities = {(query.title.casefold(), query.year) for query in queries}
    if len(identities) != len(queries):
        raise DiscoveryConfigError("discovery queries must be unique")
    if {query.year for query in queries} != set(allowed_years):
        raise DiscoveryConfigError("discovery queries must cover every pilot year")
    return tuple(queries)


def _year_tuple(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise DiscoveryConfigError("allowed_years must be a list")
    return _validate_requested_years(tuple(value), allowed=PILOT_YEARS)


def _validate_requested_years(
    years: tuple[int, ...],
    *,
    allowed: tuple[int, ...] = PILOT_YEARS,
) -> tuple[int, ...]:
    if not isinstance(years, tuple) or not years:
        raise DiscoveryConfigError("years must be a non-empty tuple")
    if any(isinstance(year, bool) or not isinstance(year, int) for year in years):
        raise DiscoveryConfigError("years must contain integers")
    if len(set(years)) != len(years):
        raise DiscoveryConfigError("years must not contain duplicates")
    if any(year not in allowed for year in years):
        raise DiscoveryConfigError("years must remain within the pilot set")
    return tuple(sorted(years))


def _safe_input(path: Path, *, root: Path) -> Path:
    raw_path = Path(path)
    if raw_path.is_symlink():
        raise DiscoveryConfigError("input paths must not be symbolic links")
    target = (
        raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    )
    if target == root or root not in target.parents:
        raise DiscoveryConfigError("input path must remain inside the allowed root")
    if not target.is_file():
        raise DiscoveryConfigError(f"input file not found: {path}")
    return target


def _safe_output(path: Path, *, root: Path) -> Path:
    raw_path = Path(path)
    if raw_path.is_symlink():
        raise DiscoveryConfigError("output paths must not be symbolic links")
    target = (
        raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
    )
    if target == root or root not in target.parents:
        raise DiscoveryConfigError("output path must remain inside the allowed root")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _relative_path(value: object, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryConfigError(f"{field} must be a non-empty path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise DiscoveryConfigError(f"{field} must be a workspace-relative path")
    return path


def _bounded_number(
    value: object,
    field: str,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DiscoveryConfigError(f"{field} must be numeric")
    if not minimum <= float(value) <= maximum:
        raise DiscoveryConfigError(f"{field} is outside the allowed range")


def _utc_now(now: Callable[[], datetime], field: str) -> datetime:
    value = now()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DiscoveryConfigError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _safe_connector_label(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) is None
    ):
        raise DiscoveryConfigError("connector name is invalid")
    return value


def _bounded_message(value: object) -> str:
    clean = re.sub(r"\s+", " ", str(value)).strip()
    return clean[:500]


def _candidate_sort_key(candidate: DiscoveryCandidate) -> tuple[object, ...]:
    return (
        candidate.year,
        candidate.report_type or "",
        candidate.report_number or "",
        normalized_title(candidate.title),
        candidate.discovery_id,
    )


def _csv_safe(value: object) -> object:
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def _stage_text(target: Path, text: str) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        return Path(handle.name)
