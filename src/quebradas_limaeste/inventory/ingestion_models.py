"""Validated configuration and seed models for controlled PDF ingestion."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from quebradas_limaeste.inventory.models import (
    InventoryValidationError,
    normalize_http_url,
)

MAX_CONFIG_BYTES = 65_536
PDF_UPLOAD_PATH = "/wp-content/uploads/"
_DOCUMENT_TYPES = (
    ("informe de emergencia", "informe_emergencia", "IE"),
    ("reporte complementario", "reporte_complementario", "RC"),
    ("reporte preliminar", "reporte_preliminar", "RP"),
)
_MONTHS = {
    "ene": 1,
    "feb": 2,
    "mar": 3,
    "abr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "ago": 8,
    "sep": 9,
    "set": 9,
    "oct": 10,
    "nov": 11,
    "dic": 12,
}


class IngestionConfigError(ValueError):
    """Raised when ingestion configuration cannot be trusted."""


@dataclass(frozen=True)
class IngestionPolicy:
    """Network and extraction limits shared by direct and seed ingestion."""

    allowed_domains: tuple[str, ...]
    expected_site_terms: tuple[str, ...]
    expected_region_terms: tuple[str, ...]
    request_delay_seconds: float
    timeout_seconds: float
    retry_backoff_seconds: float
    max_attempts: int
    max_pdf_bytes: int
    minimum_text_characters: int


@dataclass(frozen=True)
class SeedDocument:
    """Traceable known document whose identity is confirmed by its URL."""

    document_id: str
    report_number: str
    report_type: str
    report_date: date
    source_domain: str
    source_url: str
    original_filename: str
    expected_site_terms: tuple[str, ...]
    expected_region_terms: tuple[str, ...]

    @classmethod
    def create(
        cls,
        *,
        document_id: object,
        report_number: object,
        report_type: object,
        report_date: object,
        source_domain: object,
        source_url: object,
        expected_site_terms: object,
        expected_region_terms: object,
        policy: IngestionPolicy,
    ) -> SeedDocument:
        clean_document_id = _required_text(
            document_id,
            "document_id",
            max_length=100,
        )
        if re.fullmatch(r"[A-Z0-9_]+", clean_document_id) is None:
            raise IngestionConfigError("document_id has an invalid format")
        clean_number = _required_text(
            report_number,
            "report_number",
            max_length=12,
        )
        if not clean_number.isdigit():
            raise IngestionConfigError("report_number must contain only digits")
        clean_type = _required_text(report_type, "report_type", max_length=60)
        parsed_date = _parse_date(report_date)
        clean_domain = _required_text(
            source_domain,
            "source_domain",
            max_length=253,
        ).lower()
        if clean_domain not in policy.allowed_domains:
            raise IngestionConfigError("source_domain is not allowlisted")
        normalized_url, original_filename = _validate_pdf_url(
            source_url,
            allowed_domains=policy.allowed_domains,
        )
        if urlparse(normalized_url).hostname != clean_domain:
            raise IngestionConfigError("source_domain does not match source_url")

        identity = _derive_identity(normalized_url)
        expected_identity = (
            clean_document_id,
            clean_number,
            clean_type,
            parsed_date,
        )
        if identity != expected_identity:
            raise IngestionConfigError(
                "seed metadata does not match the official PDF filename"
            )
        return cls(
            document_id=clean_document_id,
            report_number=clean_number,
            report_type=clean_type,
            report_date=parsed_date,
            source_domain=clean_domain,
            source_url=normalized_url,
            original_filename=original_filename,
            expected_site_terms=_term_list(
                expected_site_terms,
                "expected_site_terms",
            ),
            expected_region_terms=_term_list(
                expected_region_terms,
                "expected_region_terms",
            ),
        )


@dataclass(frozen=True)
class IngestedDocument:
    """One validated PDF source ready for the shared processing pipeline."""

    document_id: str
    report_number: str
    report_type: str
    report_date: date
    source_type: str
    original_filename: str
    local_path: Path
    sha256: str
    file_size: int
    ingested_at_utc: datetime
    expected_site_terms: tuple[str, ...]
    expected_region_terms: tuple[str, ...]
    source_url: str | None = None
    original_path: Path | None = None
    final_url: str | None = None
    downloaded_at_utc: datetime | None = None
    http_status: int | None = None
    content_type: str | None = None
    download_status: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "document_model": "indeci_pdf_v1",
            "document_id": self.document_id,
            "report_number": self.report_number,
            "report_type": self.report_type,
            "report_date": self.report_date.isoformat(),
            "source_type": self.source_type,
            "ingested_at_utc": self.ingested_at_utc.isoformat(),
            "sha256": self.sha256,
            "file_size": self.file_size,
            "original_filename": self.original_filename,
            "local_path": str(self.local_path),
            "warnings": list(self.warnings),
        }
        optional: tuple[tuple[str, object | None], ...] = (
            ("source_url", self.source_url),
            ("original_path", str(self.original_path) if self.original_path else None),
            ("final_url", self.final_url),
            (
                "downloaded_at_utc",
                self.downloaded_at_utc.isoformat()
                if self.downloaded_at_utc
                else None,
            ),
            ("http_status", self.http_status),
            ("content_type", self.content_type),
            ("download_status", self.download_status),
        )
        result.update({key: value for key, value in optional if value is not None})
        return result


def load_ingestion_policy(path: Path) -> IngestionPolicy:
    """Load the approved ingestion block from the existing source config."""
    raw = _load_json_object(path)
    required_root_keys = {
        "portal_url",
        "request_delay_seconds",
        "timeout_seconds",
        "retry_backoff_seconds",
        "max_attempts",
        "max_pages_per_query",
        "queries",
        "ingestion",
    }
    if set(raw) != required_root_keys:
        raise IngestionConfigError("source config keys do not match ingestion schema")
    ingestion = raw["ingestion"]
    if not isinstance(ingestion, dict) or set(ingestion) != {
        "allowed_domains",
        "expected_site_terms",
        "expected_region_terms",
        "max_pdf_bytes",
        "minimum_text_characters",
    }:
        raise IngestionConfigError("ingestion policy has invalid keys")

    allowed_domains = _domain_list(ingestion["allowed_domains"])
    _number_range(
        raw["request_delay_seconds"],
        "request_delay_seconds",
        0.5,
        30.0,
    )
    _number_range(raw["timeout_seconds"], "timeout_seconds", 1.0, 60.0)
    _number_range(
        raw["retry_backoff_seconds"],
        "retry_backoff_seconds",
        0.1,
        30.0,
    )
    _integer_range(raw["max_attempts"], "max_attempts", 1, 3)
    _integer_range(
        ingestion["max_pdf_bytes"],
        "max_pdf_bytes",
        1_000_000,
        200_000_000,
    )
    _integer_range(
        ingestion["minimum_text_characters"],
        "minimum_text_characters",
        1,
        10_000,
    )
    return IngestionPolicy(
        allowed_domains=allowed_domains,
        expected_site_terms=_term_list(
            ingestion["expected_site_terms"],
            "expected_site_terms",
        ),
        expected_region_terms=_term_list(
            ingestion["expected_region_terms"],
            "expected_region_terms",
        ),
        request_delay_seconds=float(raw["request_delay_seconds"]),
        timeout_seconds=float(raw["timeout_seconds"]),
        retry_backoff_seconds=float(raw["retry_backoff_seconds"]),
        max_attempts=raw["max_attempts"],
        max_pdf_bytes=ingestion["max_pdf_bytes"],
        minimum_text_characters=ingestion["minimum_text_characters"],
    )


def load_seed_documents(
    path: Path,
    *,
    policy: IngestionPolicy,
) -> tuple[SeedDocument, ...]:
    """Load a bounded list of explicitly approved official documents."""
    raw = _load_json_object(path)
    if set(raw) != {"documents"} or not isinstance(raw["documents"], list):
        raise IngestionConfigError("seed config must contain a documents list")
    if not raw["documents"] or len(raw["documents"]) > 20:
        raise IngestionConfigError("seed config must contain between 1 and 20 items")

    expected_keys = {
        "document_id",
        "report_number",
        "report_type",
        "report_date",
        "source_domain",
        "url",
        "expected_site_terms",
        "expected_region_terms",
    }
    seeds: list[SeedDocument] = []
    for index, item in enumerate(raw["documents"], start=1):
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise IngestionConfigError(f"seed document {index} has invalid keys")
        seeds.append(
            SeedDocument.create(
                document_id=item["document_id"],
                report_number=item["report_number"],
                report_type=item["report_type"],
                report_date=item["report_date"],
                source_domain=item["source_domain"],
                source_url=item["url"],
                expected_site_terms=item["expected_site_terms"],
                expected_region_terms=item["expected_region_terms"],
                policy=policy,
            )
        )
    if len({seed.document_id for seed in seeds}) != len(seeds):
        raise IngestionConfigError("seed document_id values must be unique")
    if len({seed.source_url for seed in seeds}) != len(seeds):
        raise IngestionConfigError("seed URLs must be unique")
    return tuple(seeds)


def derive_seed_document(url: str, *, policy: IngestionPolicy) -> SeedDocument:
    """Derive direct-ingestion identity only from traceable URL metadata."""
    normalized_url, _ = _validate_pdf_url(
        url,
        allowed_domains=policy.allowed_domains,
    )
    document_id, number, report_type, report_date = _derive_identity(normalized_url)
    domain = urlparse(normalized_url).hostname
    if domain is None:
        raise IngestionConfigError("source URL has no host")
    return SeedDocument.create(
        document_id=document_id,
        report_number=number,
        report_type=report_type,
        report_date=report_date.isoformat(),
        source_domain=domain,
        source_url=normalized_url,
        expected_site_terms=policy.expected_site_terms,
        expected_region_terms=policy.expected_region_terms,
        policy=policy,
    )


def derive_document_identity(filename: str) -> tuple[str, str, str, date]:
    """Derive INDECI identity from an official-style PDF filename."""
    if not isinstance(filename, str) or not filename or len(filename) > 500:
        raise IngestionConfigError("PDF filename is invalid")
    decoded_filename = unquote(filename)
    if (
        PurePosixPath(decoded_filename).name != decoded_filename
        or "\\" in decoded_filename
        or not decoded_filename.lower().endswith(".pdf")
    ):
        raise IngestionConfigError("PDF filename is invalid")
    return _derive_identity_from_text(decoded_filename)


def _derive_identity(url: str) -> tuple[str, str, str, date]:
    filename = unquote(PurePosixPath(urlparse(url).path).name)
    return derive_document_identity(filename)


def _derive_identity_from_text(filename: str) -> tuple[str, str, str, date]:
    semantic = _semantic_text(filename)
    type_match: tuple[str, str] | None = None
    number: str | None = None
    for label, report_type, code in _DOCUMENT_TYPES:
        match = re.search(
            rf"\b{label}\s+n(?:o|ro|umero)?\s+(\d{{1,6}})\b",
            semantic,
        )
        if match is not None:
            type_match = (report_type, code)
            number = str(int(match.group(1)))
            break
    date_match = re.search(
        r"(?<![a-z0-9])(\d{1,2})"
        r"(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)"
        r"(\d{4})(?![a-z0-9])",
        semantic,
    )
    if type_match is None or number is None or date_match is None:
        raise IngestionConfigError(
            "PDF filename does not expose type, number, and date"
        )
    try:
        report_date = date(
            int(date_match.group(3)),
            _MONTHS[date_match.group(2)],
            int(date_match.group(1)),
        )
    except ValueError as exc:
        raise IngestionConfigError("PDF filename contains an invalid date") from exc
    report_type, code = type_match
    document_id = f"INDECI_{code}{number}_{report_date.strftime('%Y%m%d')}"
    return document_id, number, report_type, report_date


def _validate_pdf_url(
    value: object,
    *,
    allowed_domains: tuple[str, ...],
) -> tuple[str, str]:
    if not isinstance(value, str):
        raise IngestionConfigError("source URL must be a string")
    try:
        normalized = normalize_http_url(value)
    except InventoryValidationError as exc:
        raise IngestionConfigError(str(exc)) from exc
    parsed = urlparse(normalized)
    if parsed.hostname not in allowed_domains:
        raise IngestionConfigError("source URL host is not allowlisted")
    try:
        port = parsed.port
    except ValueError as exc:
        raise IngestionConfigError("source URL port is invalid") from exc
    allowed_port = 443 if parsed.scheme == "https" else 80
    if port not in {None, allowed_port}:
        raise IngestionConfigError("source URL port is not allowed")
    decoded_path = parsed.path
    for _ in range(3):
        decoded_path = unquote(decoded_path)
    path_parts = PurePosixPath(decoded_path).parts
    if ".." in path_parts or "." in path_parts or "\\" in decoded_path:
        raise IngestionConfigError("source URL path contains unsafe segments")
    if not decoded_path.startswith(PDF_UPLOAD_PATH):
        raise IngestionConfigError("source URL path is not allowlisted")
    if parsed.query:
        raise IngestionConfigError("source URL must not contain query parameters")
    original_filename = PurePosixPath(decoded_path).name
    if (
        not original_filename.lower().endswith(".pdf")
        or "/" in original_filename
        or "\\" in original_filename
    ):
        raise IngestionConfigError("source URL must identify a PDF filename")
    return normalized, original_filename


def _load_json_object(path: Path) -> dict[str, object]:
    config_path = Path(path)
    if config_path.suffix.lower() not in {".yaml", ".yml"}:
        raise IngestionConfigError("config must use a .yaml or .yml suffix")
    if not config_path.is_file():
        raise IngestionConfigError(f"config file not found: {config_path}")
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise IngestionConfigError("config file is too large")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise IngestionConfigError("config must be UTF-8 JSON-compatible YAML") from exc
    if not isinstance(raw, dict):
        raise IngestionConfigError("config root must be an object")
    return raw


def _required_text(value: object, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise IngestionConfigError(f"{field} must be a string")
    clean = " ".join(value.split())
    if not clean:
        raise IngestionConfigError(f"{field} is required")
    if len(clean) > max_length:
        raise IngestionConfigError(f"{field} is too long")
    return clean


def _term_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value or len(value) > 20:
        raise IngestionConfigError(f"{field} must contain between 1 and 20 terms")
    terms = tuple(_required_text(item, field, max_length=100) for item in value)
    if len({term.casefold() for term in terms}) != len(terms):
        raise IngestionConfigError(f"{field} must not contain duplicates")
    return terms


def _domain_list(value: object) -> tuple[str, ...]:
    domains = tuple(term.lower() for term in _term_list(value, "allowed_domains"))
    for domain in domains:
        try:
            parsed = urlparse(normalize_http_url(f"https://{domain}/"))
        except InventoryValidationError as exc:
            raise IngestionConfigError(
                "allowed_domains contains an invalid host"
            ) from exc
        if parsed.hostname != domain or parsed.port is not None:
            raise IngestionConfigError("allowed_domains contains an invalid host")
    return domains


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise IngestionConfigError("report_date must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IngestionConfigError("report_date must be an ISO date string") from exc


def _number_range(
    value: object,
    field: str,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IngestionConfigError(f"{field} must be numeric")
    if not minimum <= float(value) <= maximum:
        raise IngestionConfigError(f"{field} is outside the allowed range")


def _integer_range(
    value: object,
    field: str,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IngestionConfigError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise IngestionConfigError(f"{field} is outside the allowed range")


def _semantic_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()
