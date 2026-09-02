"""Validated models for documentary inventory records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse, urlunparse

MAX_EVIDENCE_TEXT_LENGTH = 500
VALID_SPATIAL_PRECISION = frozenset({"A", "B", "C", "D", "E"})
OFFLINE_MODE = "offline"

SPANISH_MONTHS = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


class InventoryValidationError(ValueError):
    """Raised when inventory input cannot be safely represented."""


def normalize_http_url(raw_url: str, *, base_url: str | None = None) -> str:
    """Normalize an HTTP(S) URL without fetching it."""
    raw = _clean_required(raw_url, "url")
    if base_url is not None:
        raw = urljoin(normalize_http_url(base_url), raw)

    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise InventoryValidationError("url must use http or https")
    if not parsed.netloc or parsed.hostname is None:
        raise InventoryValidationError("url must include a host")
    if parsed.username or parsed.password:
        raise InventoryValidationError("url must not contain credentials")
    _reject_private_host(parsed.hostname)

    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        fragment="",
    )
    return urlunparse(normalized)


def parse_document_date(value: str | None) -> date | None:
    """Parse known document date formats without inventing missing dates."""
    if value is None:
        return None
    clean = _normalize_space(value).lower().strip(".,")
    if not clean:
        return None

    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(clean, date_format).date()
        except ValueError:
            continue

    match = re.search(
        r"\b(?P<day>\d{1,2})\s+de\s+(?P<month>[a-z]+)\s+de\s+(?P<year>\d{4})\b",
        clean,
    )
    if match is None:
        return None

    month = SPANISH_MONTHS.get(match["month"])
    if month is None:
        return None

    try:
        return date(int(match["year"]), month, int(match["day"]))
    except ValueError:
        return None


@dataclass(frozen=True)
class InventoryRecord:
    """Traceable candidate document record extracted from untrusted content."""

    record_id: str
    source_name: str
    source_url: str
    document_url: str
    title: str
    published_date: date | None = None
    evidence_text: str = ""
    spatial_precision: str | None = None
    requires_human_review: bool = True
    warnings: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        source_name: str,
        source_url: str,
        document_url: str,
        title: str,
        published_date: str | date | None = None,
        evidence_text: str = "",
        spatial_precision: str | None = None,
        warnings: tuple[str, ...] | list[str] = (),
    ) -> InventoryRecord:
        clean_source_name = _clean_required(source_name, "source_name")
        clean_source_url = normalize_http_url(source_url)
        clean_document_url = normalize_http_url(
            document_url,
            base_url=clean_source_url,
        )
        clean_title = _clean_required(title, "title")
        clean_evidence = _clean_optional_text(
            evidence_text,
            "evidence_text",
            max_length=MAX_EVIDENCE_TEXT_LENGTH,
        )
        clean_precision = _validate_spatial_precision(spatial_precision)
        parsed_date = _coerce_document_date(published_date)
        clean_warnings = tuple(_normalize_space(item) for item in warnings if item)

        record_id = _stable_record_id(
            source_name=clean_source_name,
            source_url=clean_source_url,
            document_url=clean_document_url,
            title=clean_title,
            published_date=parsed_date,
        )

        return cls(
            record_id=record_id,
            source_name=clean_source_name,
            source_url=clean_source_url,
            document_url=clean_document_url,
            title=clean_title,
            published_date=parsed_date,
            evidence_text=clean_evidence,
            spatial_precision=clean_precision,
            requires_human_review=True,
            warnings=clean_warnings,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "document_url": self.document_url,
            "title": self.title,
            "published_date": (
                self.published_date.isoformat()
                if self.published_date is not None
                else None
            ),
            "evidence_text": self.evidence_text,
            "spatial_precision": self.spatial_precision,
            "requires_human_review": self.requires_human_review,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class InventoryManifest:
    """Manifest metadata for a generated offline inventory."""

    source_name: str
    source_url: str
    generated_at_utc: datetime
    mode: str
    record_count: int
    warnings: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        source_name: str,
        source_url: str,
        records: list[InventoryRecord] | tuple[InventoryRecord, ...],
        generated_at_utc: datetime | None = None,
        mode: str = OFFLINE_MODE,
        warnings: tuple[str, ...] | list[str] = (),
    ) -> InventoryManifest:
        if mode != OFFLINE_MODE:
            raise InventoryValidationError("only offline mode is enabled")

        generated_at = generated_at_utc or datetime.now(UTC)
        if generated_at.tzinfo is None:
            raise InventoryValidationError("generated_at_utc must be timezone-aware")

        return cls(
            source_name=_clean_required(source_name, "source_name"),
            source_url=normalize_http_url(source_url),
            generated_at_utc=generated_at.astimezone(UTC),
            mode=mode,
            record_count=len(records),
            warnings=tuple(_normalize_space(item) for item in warnings if item),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "source_name": self.source_name,
            "source_url": self.source_url,
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "mode": self.mode,
            "record_count": self.record_count,
            "warnings": list(self.warnings),
        }


def _coerce_document_date(value: str | date | None) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) or value is None:
        return parse_document_date(value)
    raise InventoryValidationError("published_date must be a date, string, or None")


def _stable_record_id(
    *,
    source_name: str,
    source_url: str,
    document_url: str,
    title: str,
    published_date: date | None,
) -> str:
    identity = "\n".join(
        (
            source_name,
            source_url,
            document_url,
            title,
            published_date.isoformat() if published_date is not None else "",
        )
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"indeci-coen-{digest}"


def _validate_spatial_precision(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    clean = value.strip().upper()
    if clean not in VALID_SPATIAL_PRECISION:
        raise InventoryValidationError("spatial_precision must be A, B, C, D, or E")
    return clean


def _reject_private_host(hostname: str) -> None:
    lowered = hostname.lower().strip("[]")
    if lowered in {"localhost", "0", "0.0.0.0"}:
        raise InventoryValidationError("url host must not be local")
    try:
        parsed_ip = ip_address(lowered)
    except ValueError:
        return
    if not parsed_ip.is_global:
        raise InventoryValidationError("url host must be public")


def _clean_required(value: str, field_name: str) -> str:
    clean = _normalize_space(value)
    if not clean:
        raise InventoryValidationError(f"{field_name} is required")
    return clean


def _clean_optional_text(value: str, field_name: str, *, max_length: int) -> str:
    clean = _normalize_space(value)
    if len(clean) > max_length:
        raise InventoryValidationError(f"{field_name} is too long")
    return clean


def _normalize_space(value: str) -> str:
    if not isinstance(value, str):
        raise InventoryValidationError("text values must be strings")
    return re.sub(r"\s+", " ", value).strip()
