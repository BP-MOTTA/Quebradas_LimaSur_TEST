"""Validated models shared by controlled INDECI discovery connectors."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

from quebradas_limaeste.inventory.indeci_portal import INDECI_PORTAL_HOST
from quebradas_limaeste.inventory.models import (
    InventoryValidationError,
    normalize_http_url,
)

MAX_TITLE_LENGTH = 500
MAX_CONTEXT_BYTES = 8_192
MAX_WARNING_LENGTH = 500
REPORT_TYPES = frozenset(
    {"reporte_complementario", "reporte_preliminar", "informe_emergencia"}
)
DEDUP_STATUSES = frozenset({"unique", "merged", "ambiguous"})
_REPORT_TYPE_ALIASES = {
    "reporte complementario": "reporte_complementario",
    "reporte_complementario": "reporte_complementario",
    "reporte preliminar": "reporte_preliminar",
    "reporte_preliminar": "reporte_preliminar",
    "informe de emergencia": "informe_emergencia",
    "informe_emergencia": "informe_emergencia",
}


class DiscoveryConfigError(ValueError):
    """Raised when discovery input or output cannot be handled safely."""


@dataclass(frozen=True)
class DiscoveryCandidate:
    """Metadata-only document candidate emitted by one discovery connector."""

    discovery_id: str
    source_connector: str
    title: str
    detail_url: str | None
    pdf_url: str | None
    report_type: str | None
    report_number: str | None
    report_date: date | None
    year: int
    discovered_at_utc: datetime
    query_context: dict[str, object]
    raw_metadata: dict[str, object]
    discovery_warnings: tuple[str, ...] = ()
    discovery_sources_attempted: tuple[str, ...] = ()
    discovery_sources_matched: tuple[str, ...] = ()
    dedup_status: str = "unique"
    dedup_reason: str = "no_duplicate_signal"

    @classmethod
    def create(
        cls,
        *,
        source_connector: str,
        title: str,
        detail_url: str | None,
        pdf_url: str | None,
        report_type: str | None,
        report_number: str | None,
        report_date: date | None,
        year: int,
        discovered_at_utc: datetime,
        query_context: dict[str, object],
        raw_metadata: dict[str, object],
        discovery_warnings: tuple[str, ...] | list[str] = (),
        discovery_sources_attempted: tuple[str, ...] | list[str] = (),
        discovery_sources_matched: tuple[str, ...] | list[str] | None = None,
        dedup_status: str = "unique",
        dedup_reason: str = "no_duplicate_signal",
    ) -> DiscoveryCandidate:
        connector = _connector_name(source_connector)
        clean_title = _clean_text(title, "title", MAX_TITLE_LENGTH)
        safe_detail = _official_url(detail_url, field_name="detail_url")
        safe_pdf = _official_url(
            pdf_url,
            field_name="pdf_url",
            required_path_prefix="/wp-content/uploads/",
        )
        clean_type = _report_type(report_type)
        clean_number = _report_number(report_number)
        clean_date = _report_date(report_date)
        if (
            isinstance(year, bool)
            or not isinstance(year, int)
            or not 2010 <= year <= 2100
        ):
            raise DiscoveryConfigError("year must be an integer between 2010 and 2100")
        if (
            not isinstance(discovered_at_utc, datetime)
            or discovered_at_utc.tzinfo is None
        ):
            raise DiscoveryConfigError("discovered_at_utc must be timezone-aware")
        clean_context = _json_object(query_context, "query_context")
        clean_metadata = _json_object(raw_metadata, "raw_metadata")
        clean_warnings = _warnings(discovery_warnings)
        attempted = _connector_names(discovery_sources_attempted)
        matched = _connector_names(
            (connector,)
            if discovery_sources_matched is None
            else discovery_sources_matched
        )
        if connector not in matched:
            raise DiscoveryConfigError("source_connector must be listed as matched")
        if dedup_status not in DEDUP_STATUSES:
            raise DiscoveryConfigError("dedup_status is invalid")
        clean_reason = _clean_text(dedup_reason, "dedup_reason", 200)
        timestamp = discovered_at_utc.astimezone(UTC)
        discovery_id = _stable_discovery_id(
            pdf_url=safe_pdf,
            detail_url=safe_detail,
            report_type=clean_type,
            report_number=clean_number,
            report_date=clean_date,
            title=clean_title,
            year=year,
        )
        return cls(
            discovery_id=discovery_id,
            source_connector=connector,
            title=clean_title,
            detail_url=safe_detail,
            pdf_url=safe_pdf,
            report_type=clean_type,
            report_number=clean_number,
            report_date=clean_date,
            year=year,
            discovered_at_utc=timestamp,
            query_context=clean_context,
            raw_metadata=clean_metadata,
            discovery_warnings=clean_warnings,
            discovery_sources_attempted=attempted,
            discovery_sources_matched=matched,
            dedup_status=dedup_status,
            dedup_reason=clean_reason,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "discovery_id": self.discovery_id,
            "source_connector": self.source_connector,
            "title": self.title,
            "detail_url": self.detail_url,
            "pdf_url": self.pdf_url,
            "report_type": self.report_type,
            "report_number": self.report_number,
            "report_date": (
                self.report_date.isoformat() if self.report_date is not None else None
            ),
            "year": self.year,
            "discovered_at_utc": self.discovered_at_utc.isoformat(),
            "query_context": self.query_context,
            "raw_metadata": self.raw_metadata,
            "discovery_warnings": list(self.discovery_warnings),
            "discovery_sources_attempted": list(self.discovery_sources_attempted),
            "discovery_sources_matched": list(self.discovery_sources_matched),
            "dedup_status": self.dedup_status,
            "dedup_reason": self.dedup_reason,
        }


@dataclass(frozen=True)
class ConnectorResult:
    """Bounded result from one connector, including partial-failure evidence."""

    connector: str
    candidates: tuple[DiscoveryCandidate, ...]
    requests: int
    pages: int
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class DiscoveryConnector(Protocol):
    name: str

    def discover(
        self,
        *,
        years: tuple[int, ...],
        discovered_at_utc: datetime,
    ) -> ConnectorResult: ...


def canonical_document_url(value: str) -> str:
    """Canonicalize an already validated document URL for deduplication."""
    try:
        normalized = normalize_http_url(value)
    except InventoryValidationError as exc:
        raise DiscoveryConfigError(str(exc)) from exc
    parsed = urlparse(normalized)
    try:
        port = parsed.port
    except ValueError as exc:
        raise DiscoveryConfigError("URL port is invalid") from exc
    path = quote(parsed.path, safe="/%-._~!$&'()*+,;=:@")
    path = re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group().upper(), path)
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    default_port = 443 if parsed.scheme == "https" else 80
    netloc = parsed.hostname if port in {None, default_port} else parsed.netloc
    return urlunparse(
        parsed._replace(netloc=netloc, path=path, query=query, fragment="")
    )


def normalized_title(value: str) -> str:
    """Produce a conservative title signal; it never establishes identity alone."""
    decomposed = unicodedata.normalize("NFKD", value).casefold()
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _official_url(
    value: str | None,
    *,
    field_name: str,
    required_path_prefix: str | None = None,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DiscoveryConfigError(f"{field_name} must be a string or null")
    try:
        normalized = canonical_document_url(value)
    except DiscoveryConfigError as exc:
        raise DiscoveryConfigError(f"{field_name}: {exc}") from exc
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != INDECI_PORTAL_HOST
        or parsed.port not in {None, 443}
    ):
        raise DiscoveryConfigError(f"{field_name} must use the official HTTPS host")
    if required_path_prefix is not None and not parsed.path.startswith(
        required_path_prefix
    ):
        raise DiscoveryConfigError(f"{field_name} path is not allowlisted")
    return normalized


def _stable_discovery_id(
    *,
    pdf_url: str | None,
    detail_url: str | None,
    report_type: str | None,
    report_number: str | None,
    report_date: date | None,
    title: str,
    year: int,
) -> str:
    if pdf_url is not None:
        identity = f"pdf\n{canonical_document_url(pdf_url)}"
    elif report_type and report_number and report_date:
        identity = (
            f"identity\n{report_type}\n{report_number}\n{report_date.isoformat()}"
        )
    elif detail_url is not None:
        identity = f"detail\n{canonical_document_url(detail_url)}"
    else:
        identity = f"title\n{year}\n{normalized_title(title)}"
    return f"indeci-discovery-{sha256(identity.encode()).hexdigest()[:20]}"


def _report_type(value: str | None) -> str | None:
    if value is None:
        return None
    clean = _clean_text(value, "report_type", 60).casefold()
    normalized = _REPORT_TYPE_ALIASES.get(clean)
    if normalized not in REPORT_TYPES:
        raise DiscoveryConfigError("report_type is not recognized")
    return normalized


def _report_number(value: str | None) -> str | None:
    if value is None:
        return None
    clean = _clean_text(value, "report_number", 20)
    if not clean.isdigit():
        raise DiscoveryConfigError("report_number must contain only digits")
    return clean.lstrip("0") or "0"


def _report_date(value: date | None) -> date | None:
    if value is None:
        return None
    if not isinstance(value, date) or isinstance(value, datetime):
        raise DiscoveryConfigError("report_date must be a date or null")
    return value


def _connector_name(value: str) -> str:
    clean = _clean_text(value, "source_connector", 80)
    if re.fullmatch(r"[a-z][a-z0-9_]*", clean) is None:
        raise DiscoveryConfigError("source_connector has an invalid format")
    return clean


def _connector_names(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, tuple | list):
        raise DiscoveryConfigError("connector provenance must be a list or tuple")
    return tuple(sorted({_connector_name(value) for value in values}))


def _warnings(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not isinstance(values, tuple | list):
        raise DiscoveryConfigError("discovery_warnings must be a list or tuple")
    if len(values) > 100:
        raise DiscoveryConfigError("too many discovery warnings")
    return tuple(
        _clean_text(value, "discovery_warning", MAX_WARNING_LENGTH) for value in values
    )


def _json_object(value: dict[str, object], field_name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DiscoveryConfigError(f"{field_name} must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise DiscoveryConfigError(f"{field_name} must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise DiscoveryConfigError(f"{field_name} exceeds the size limit")
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):
        raise DiscoveryConfigError(f"{field_name} must be an object")
    return decoded


def _clean_text(value: str, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise DiscoveryConfigError(f"{field_name} must be a string")
    clean = re.sub(r"\s+", " ", value).strip()
    if not clean or len(clean) > max_length:
        raise DiscoveryConfigError(f"{field_name} is empty or too long")
    return clean
