"""Resumable multi-source INDECI discovery for the 2010-2026 inventory."""

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
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.discovery import (
    deduplicate_candidates,
    run_discovery,
)
from quebradas_limaeste.inventory.discovery_models import (
    DiscoveryCandidate,
    DiscoveryConfigError,
    DiscoveryConnector,
    normalized_title,
)
from quebradas_limaeste.inventory.live_smoke import PortalQuery

HISTORICAL_CONFIG = Path("configs/sources/indeci_historical_cusipata.yaml")
HISTORICAL_ROOT = Path("metadata/indeci/historical")
HISTORICAL_DISCOVERY_OUTPUT = HISTORICAL_ROOT / "discovery_documents.csv"
HISTORICAL_DISCOVERY_RUN_OUTPUT = HISTORICAL_ROOT / "discovery_run.json"
HISTORICAL_CHECKPOINT_ROOT = HISTORICAL_ROOT / "checkpoints"
HISTORICAL_DISCOVERY_FIELDS = (
    "document_id",
    "document_family_id",
    "year",
    "title",
    "report_type",
    "report_number",
    "report_date",
    "source_connector",
    "detail_url",
    "pdf_url",
    "discovery_sources",
    "discovered_at_utc",
    "duplicate_status",
    "duplicate_reason",
    "canonical_document_id",
    "spatial_relevance",
    "event_relevance",
    "preliminary_priority",
    "priority_reason",
    "metadata_insufficient",
)
MAX_CONFIG_BYTES = 65_536
_TYPE_CODES = {
    "reporte_complementario": "RC",
    "reporte_preliminar": "RP",
    "informe_emergencia": "IE",
}


@dataclass(frozen=True)
class HistoricalConfig:
    """Strict policy for one bounded and reproducible historical execution."""

    years: tuple[int, ...]
    blocks: tuple[tuple[int, ...], ...]
    query_terms: tuple[str, ...]
    connector_urls: dict[str, str]
    seed_config: Path
    ingestion_config: Path
    filter_config: Path | None
    quality_config: Path
    request_delay_seconds: float
    timeout_seconds: float
    retry_backoff_seconds: float
    max_attempts: int
    max_pages_per_query: int
    px_seed: str
    px_sample_size: int
    config_sha256: str

    @classmethod
    def for_tests(cls, *, years: tuple[int, ...]) -> HistoricalConfig:
        blocks = tuple((year,) for year in years)
        return cls(
            years=years,
            blocks=blocks,
            query_terms=("Chaclacayo",),
            connector_urls={},
            seed_config=Path("configs/sources/indeci_seed_documents.yaml"),
            ingestion_config=Path("configs/sources/indeci_cusipata.yaml"),
            filter_config=None,
            quality_config=Path("configs/sources/indeci_candidate_quality.yaml"),
            request_delay_seconds=0.5,
            timeout_seconds=1.0,
            retry_backoff_seconds=0.1,
            max_attempts=1,
            max_pages_per_query=1,
            px_seed="test-seed",
            px_sample_size=2,
            config_sha256="test-config",
        )


def load_historical_config(path: Path, *, allowed_root: Path) -> HistoricalConfig:
    """Load and verify the versioned historical policy and frozen filter digest."""
    root = Path(allowed_root).resolve()
    source = _safe_input(path, root=root, maximum=MAX_CONFIG_BYTES)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryConfigError("historical config must be UTF-8 JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "period",
        "network",
        "connectors",
        "query_terms",
        "pipeline",
        "px_control",
    }:
        raise DiscoveryConfigError("historical config keys do not match schema")

    period = _object(raw["period"], {"start_year", "end_year", "blocks"}, "period")
    start, end = period["start_year"], period["end_year"]
    if (start, end) != (2010, 2026):
        raise DiscoveryConfigError("historical period must be 2010-2026")
    years = tuple(range(start, end + 1))
    blocks = _blocks(period["blocks"], years=years)
    network = _object(
        raw["network"],
        {
            "request_delay_seconds",
            "timeout_seconds",
            "retry_backoff_seconds",
            "max_attempts",
            "max_pages_per_query",
        },
        "network",
    )
    connectors = _object(
        raw["connectors"],
        {"archive_informes", "archive_emergencias", "seed_config"},
        "connectors",
    )
    pipeline = _object(
        raw["pipeline"],
        {
            "ingestion_config",
            "filter_config",
            "filter_config_sha256",
            "quality_config",
        },
        "pipeline",
    )
    px = _object(raw["px_control"], {"seed", "sample_size"}, "px_control")
    query_terms = _terms(raw["query_terms"])
    filter_config = _relative_path(pipeline["filter_config"], "filter_config")
    filter_source = _safe_input(filter_config, root=root, maximum=MAX_CONFIG_BYTES)
    expected_digest = _digest(pipeline["filter_config_sha256"], "filter_config_sha256")
    if sha256(filter_source.read_bytes()).hexdigest() != expected_digest:
        raise DiscoveryConfigError("frozen Lima Este filter digest does not match")
    max_attempts = _integer(network["max_attempts"], "max_attempts", 1, 3)
    max_pages = _integer(network["max_pages_per_query"], "max_pages_per_query", 1, 1)
    sample_size = _integer(px["sample_size"], "sample_size", 1, 100)
    seed = _text(px["seed"], "px seed", 100)
    return HistoricalConfig(
        years=years,
        blocks=blocks,
        query_terms=query_terms,
        connector_urls={
            "archive_informes": _text(
                connectors["archive_informes"], "archive_informes", 500
            ),
            "archive_emergencias": _text(
                connectors["archive_emergencias"], "archive_emergencias", 500
            ),
        },
        seed_config=_relative_path(connectors["seed_config"], "seed_config"),
        ingestion_config=_relative_path(
            pipeline["ingestion_config"], "ingestion_config"
        ),
        filter_config=filter_config,
        quality_config=_relative_path(pipeline["quality_config"], "quality_config"),
        request_delay_seconds=_number(
            network["request_delay_seconds"], "request_delay_seconds", 0.5, 30
        ),
        timeout_seconds=_number(network["timeout_seconds"], "timeout_seconds", 1, 60),
        retry_backoff_seconds=_number(
            network["retry_backoff_seconds"], "retry_backoff_seconds", 0.1, 30
        ),
        max_attempts=max_attempts,
        max_pages_per_query=max_pages,
        px_seed=seed,
        px_sample_size=sample_size,
        config_sha256=sha256(source.read_bytes()).hexdigest(),
    )


def execute_historical_discovery(
    config_path: Path = HISTORICAL_CONFIG,
    *,
    config: HistoricalConfig | None = None,
    connectors: Sequence[DiscoveryConnector] | None = None,
    discovery_output: Path = HISTORICAL_DISCOVERY_OUTPUT,
    run_output: Path = HISTORICAL_DISCOVERY_RUN_OUTPUT,
    checkpoint_root: Path = HISTORICAL_CHECKPOINT_ROOT,
    allowed_root: Path,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, object]:
    """Discover each year independently and reuse completed checkpoints."""
    root = Path(allowed_root).resolve()
    policy = config or load_historical_config(config_path, allowed_root=root)
    csv_target = _safe_output(discovery_output, root=root)
    run_target = _safe_output(run_output, root=root)
    checkpoints = _safe_output(checkpoint_root, root=root)
    reused = _completed_output(run_target, csv_target, policy=policy)
    if reused is not None:
        reused["resume_status"] = "complete_output_reused"
        return reused
    active = (
        tuple(connectors)
        if connectors is not None
        else _default_connectors(policy, root=root)
    )
    all_candidates: list[DiscoveryCandidate] = []
    yearly: dict[str, dict[str, object]] = {}
    all_errors: list[str] = []
    all_warnings: list[str] = []
    requests = pages = raw_count = 0
    for year in policy.years:
        checkpoint = checkpoints / f"{year}.json"
        saved = _load_checkpoint(checkpoint, year=year, policy=policy)
        if saved is None:
            result = run_discovery(
                years=(year,),
                connectors=active,
                allowed_years=policy.years,
                now=now,
            )
            saved = {
                "schema_version": 1,
                "config_sha256": policy.config_sha256,
                "year": year,
                "requests": result.requests,
                "pages": result.pages,
                "documents_raw": result.candidates_raw,
                "duplicates": result.duplicates,
                "warnings": list(result.warnings),
                "errors": list(result.errors),
                "candidates": [_candidate_payload(item) for item in result.candidates],
            }
            _write_json(checkpoint, saved)
        candidates = tuple(
            _candidate_from_payload(item) for item in saved["candidates"]
        )
        all_candidates.extend(candidates)
        requests += int(saved["requests"])
        pages += int(saved["pages"])
        raw_count += int(saved["documents_raw"])
        all_errors.extend(str(item) for item in saved["errors"])
        all_warnings.extend(str(item) for item in saved["warnings"])
        yearly[str(year)] = {
            "documents_raw": int(saved["documents_raw"]),
            "documents_checkpointed": len(candidates),
            "requests": int(saved["requests"]),
            "pages": int(saved["pages"]),
            "errors": len(saved["errors"]),
        }

    unique, duplicates = deduplicate_candidates(all_candidates)
    rows = _discovery_rows(unique, policy=policy, root=root)
    _write_csv(csv_target, HISTORICAL_DISCOVERY_FIELDS, rows)
    run_timestamp = _utc_now(now).strftime("%Y%m%dT%H%M%SZ")
    manifest = {
        "run_id": f"indeci-historical-discovery-{run_timestamp}",
        "schema_version": 1,
        "config_sha256": policy.config_sha256,
        "period_start": policy.years[0],
        "period_end": policy.years[-1],
        "years_processed": list(policy.years),
        "connectors": [item.name for item in active],
        "requests": requests,
        "pages": pages,
        "documents_raw": raw_count,
        "documents_unique": len(unique),
        "duplicates": raw_count - len(unique),
        "documents_by_year": dict(
            sorted(Counter(str(item.year) for item in unique).items())
        ),
        "year_stats": yearly,
        "warnings": all_warnings,
        "errors": all_errors,
        "discovery_output": str(csv_target.relative_to(root)),
        "resume_status": "completed_from_checkpoints",
    }
    _write_json(run_target, manifest)
    return manifest


def _default_connectors(
    config: HistoricalConfig, *, root: Path
) -> tuple[DiscoveryConnector, ...]:
    from quebradas_limaeste.inventory.archive_connector import (
        ArchiveDiscoveryConnector,
        ArchiveDiscoverySettings,
    )
    from quebradas_limaeste.inventory.ingestion_models import (
        load_ingestion_policy,
        load_seed_documents,
    )
    from quebradas_limaeste.inventory.seed_discovery import SeedDiscoveryConnector

    queries = tuple(
        PortalQuery(title=term, alert_type="", year=year)
        for year in config.years
        for term in config.query_terms
    )
    settings = ArchiveDiscoverySettings(
        queries=queries,
        request_delay_seconds=config.request_delay_seconds,
        timeout_seconds=config.timeout_seconds,
        retry_backoff_seconds=config.retry_backoff_seconds,
        max_attempts=config.max_attempts,
        max_pages_per_query=config.max_pages_per_query,
    )
    ingestion_path = _safe_input(config.ingestion_config, root=root, maximum=65_536)
    seed_path = _safe_input(config.seed_config, root=root, maximum=65_536)
    ingestion = load_ingestion_policy(ingestion_path)
    seeds = load_seed_documents(seed_path, policy=ingestion)
    return (
        ArchiveDiscoveryConnector(
            name="archive_informes",
            portal_url=config.connector_urls["archive_informes"],
            settings=settings,
        ),
        ArchiveDiscoveryConnector(
            name="archive_emergencias",
            portal_url=config.connector_urls["archive_emergencias"],
            settings=settings,
        ),
        SeedDiscoveryConnector(seeds),
    )


def _discovery_rows(
    candidates: Sequence[DiscoveryCandidate], *, policy: HistoricalConfig, root: Path
) -> list[dict[str, object]]:
    from quebradas_limaeste.inventory.historical_inventory import historical_prefilter
    from quebradas_limaeste.inventory.limaeste_filter import load_filter_policy

    filter_policy = (
        load_filter_policy(policy.filter_config, allowed_root=root)
        if policy.filter_config is not None
        else None
    )
    document_ids = historical_document_ids(
        candidates,
        known_by_url=_registered_document_ids(root),
    )
    rows = []
    for item in candidates:
        prefilter = historical_prefilter(item.title, policy=filter_policy)
        document_id = document_ids[item.discovery_id]
        rows.append(
            {
                "document_id": document_id,
                "document_family_id": _family_id(item),
                "year": item.year,
                "title": item.title,
                "report_type": item.report_type or "",
                "report_number": item.report_number or "",
                "report_date": item.report_date.isoformat() if item.report_date else "",
                "source_connector": item.source_connector,
                "detail_url": item.detail_url or "",
                "pdf_url": item.pdf_url or "",
                "discovery_sources": json.dumps(
                    item.discovery_sources_matched,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                "discovered_at_utc": item.discovered_at_utc.isoformat(),
                "duplicate_status": item.dedup_status,
                "duplicate_reason": item.dedup_reason,
                "canonical_document_id": document_id,
                "spatial_relevance": prefilter.spatial_relevance,
                "event_relevance": prefilter.event_relevance,
                "preliminary_priority": prefilter.priority,
                "priority_reason": prefilter.reason,
                "metadata_insufficient": str(prefilter.metadata_insufficient).lower(),
            }
        )
    return rows


def historical_document_ids(
    candidates: Sequence[DiscoveryCandidate],
    *,
    known_by_url: dict[str, str] | None = None,
) -> dict[str, str]:
    """Derive IDs with the existing pilot scheme and preserve registered URLs."""
    result: dict[str, str] = {}
    used: set[str] = set()
    known = known_by_url or {}
    for item in sorted(candidates, key=lambda value: (value.year, value.discovery_id)):
        registered = known.get(item.pdf_url or "")
        if registered:
            base = registered
        elif item.report_type and item.report_number and item.report_date:
            base = (
                f"INDECI_{_TYPE_CODES[item.report_type]}{item.report_number}_"
                f"{item.report_date.strftime('%Y%m%d')}"
            )
        else:
            identity = item.pdf_url or item.detail_url or item.discovery_id
            digest = sha256(identity.encode()).hexdigest()[:16].upper()
            base = f"INDECI_DISC_{digest}"
        document_id = base
        if document_id in used:
            suffix = sha256(item.discovery_id.encode()).hexdigest()[:8].upper()
            document_id = f"{base}_{suffix}"
        used.add(document_id)
        result[item.discovery_id] = document_id
    return result


def _registered_document_ids(root: Path) -> dict[str, str]:
    from quebradas_limaeste.inventory.ingestion import (
        load_document_registry,
    )

    records = load_document_registry(allowed_root=root)
    result: dict[str, str] = {}
    for record in records:
        source_url = record.get("source_url")
        document_id = record.get("document_id")
        if not isinstance(source_url, str) or not isinstance(document_id, str):
            continue
        previous = result.setdefault(source_url, document_id)
        if previous != document_id:
            raise DiscoveryConfigError(
                "document registry maps one URL to multiple document IDs"
            )
    return result


def _family_id(candidate: DiscoveryCandidate) -> str:
    identity = normalized_title(candidate.title)
    if not identity:
        identity = candidate.discovery_id
    return f"indeci-family-{sha256(identity.encode()).hexdigest()[:20]}"


def _candidate_payload(candidate: DiscoveryCandidate) -> dict[str, object]:
    payload = candidate.to_dict()
    return payload


def _candidate_from_payload(raw: object) -> DiscoveryCandidate:
    if not isinstance(raw, dict):
        raise DiscoveryConfigError("checkpoint candidate is invalid")
    try:
        report_date = (
            date.fromisoformat(raw["report_date"]) if raw["report_date"] else None
        )
        timestamp = datetime.fromisoformat(
            str(raw["discovered_at_utc"]).replace("Z", "+00:00")
        )
        return DiscoveryCandidate.create(
            source_connector=raw["source_connector"],
            title=raw["title"],
            detail_url=raw["detail_url"],
            pdf_url=raw["pdf_url"],
            report_type=raw["report_type"],
            report_number=raw["report_number"],
            report_date=report_date,
            year=raw["year"],
            discovered_at_utc=timestamp,
            query_context=raw["query_context"],
            raw_metadata=raw["raw_metadata"],
            discovery_warnings=raw["discovery_warnings"],
            discovery_sources_attempted=raw["discovery_sources_attempted"],
            discovery_sources_matched=raw["discovery_sources_matched"],
            dedup_status=raw["dedup_status"],
            dedup_reason=raw["dedup_reason"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DiscoveryConfigError("checkpoint candidate is invalid") from exc


def _load_checkpoint(
    path: Path, *, year: int, policy: HistoricalConfig
) -> dict[str, object] | None:
    if not path.exists():
        return None
    source = _safe_input(path, root=path.parents[4], maximum=20_000_000)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryConfigError(f"invalid historical checkpoint for {year}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("config_sha256") != policy.config_sha256
        or payload.get("year") != year
        or not isinstance(payload.get("candidates"), list)
    ):
        raise DiscoveryConfigError(f"historical checkpoint mismatch for {year}")
    return payload


def _completed_output(
    run_path: Path, csv_path: Path, *, policy: HistoricalConfig
) -> dict[str, object] | None:
    if not run_path.exists() and not csv_path.exists():
        return None
    if not run_path.is_file() or not csv_path.is_file():
        raise DiscoveryConfigError("historical discovery output is incomplete")
    try:
        payload = json.loads(run_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DiscoveryConfigError("historical discovery manifest is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("config_sha256") != policy.config_sha256
        or payload.get("years_processed") != list(policy.years)
    ):
        raise DiscoveryConfigError(
            "existing historical discovery cannot be overwritten"
        )
    return payload


def _blocks(value: object, *, years: tuple[int, ...]) -> tuple[tuple[int, ...], ...]:
    if not isinstance(value, list) or not value:
        raise DiscoveryConfigError("historical blocks must be a non-empty list")
    blocks = []
    for item in value:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or any(isinstance(year, bool) or not isinstance(year, int) for year in item)
            or item[0] > item[1]
        ):
            raise DiscoveryConfigError("historical block is invalid")
        blocks.append(tuple(range(item[0], item[1] + 1)))
    if tuple(year for block in blocks for year in block) != years:
        raise DiscoveryConfigError(
            "historical blocks must cover each year exactly once"
        )
    return tuple(blocks)


def _terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise DiscoveryConfigError("historical query_terms must contain 1-24 terms")
    terms = tuple(_text(item, "query term", 100) for item in value)
    if len({normalized_title(item) for item in terms}) != len(terms):
        raise DiscoveryConfigError("historical query_terms must be unique")
    return terms


def _object(value: object, keys: set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise DiscoveryConfigError(f"historical {label} block has invalid keys")
    return value


def _relative_path(value: object, field: str) -> Path:
    text = _text(value, field, 300)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise DiscoveryConfigError(f"{field} must be a workspace-relative path")
    return path


def _digest(value: object, field: str) -> str:
    text = _text(value, field, 64)
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise DiscoveryConfigError(f"{field} must be a SHA-256 digest")
    return text


def _text(value: object, field: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise DiscoveryConfigError(f"{field} must be a string")
    clean = " ".join(value.replace("\x00", "").split())
    if not clean or len(clean) > maximum:
        raise DiscoveryConfigError(f"{field} has an invalid length")
    return clean


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise DiscoveryConfigError(f"{field} is outside the allowed range")
    return value


def _number(value: object, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DiscoveryConfigError(f"{field} must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise DiscoveryConfigError(f"{field} is outside the allowed range")
    return result


def _safe_input(path: Path, *, root: Path, maximum: int) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise DiscoveryConfigError("historical input must not be a symlink")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DiscoveryConfigError(f"historical input does not exist: {path}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise DiscoveryConfigError("historical input must be inside allowed_root")
    if resolved.stat().st_size > maximum:
        raise DiscoveryConfigError("historical input exceeds its size limit")
    return resolved


def _safe_output(path: Path, *, root: Path) -> Path:
    candidate = path if path.is_absolute() else root / path
    if candidate.is_symlink():
        raise DiscoveryConfigError("historical output must not be a symlink")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise DiscoveryConfigError("historical output escapes allowed_root")
    return resolved


def _write_csv(
    path: Path, fields: tuple[str, ...], rows: list[dict[str, object]]
) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _cell(row.get(field, "")) for field in fields})
    _write_text(path, buffer.getvalue())


def _write_json(path: Path, payload: dict[str, object]) -> None:
    _write_text(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def _utc_now(now: Callable[[], datetime]) -> datetime:
    value = now()
    if value.tzinfo is None:
        raise DiscoveryConfigError("historical timestamp must be timezone-aware")
    return value.astimezone(UTC)
