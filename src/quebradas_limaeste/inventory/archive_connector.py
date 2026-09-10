"""Shared bounded HTTP mechanics for independent INDECI archive connectors."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlparse

from quebradas_limaeste.inventory.discovery_models import (
    ConnectorResult,
    DiscoveryCandidate,
    DiscoveryConfigError,
)
from quebradas_limaeste.inventory.indeci_portal import (
    INDECI_ARCHIVE_PATHS,
    INDECI_PORTAL_HOST,
    MAX_PORTAL_HTML_LENGTH,
    PortalParseError,
    parse_indeci_portal_html,
)
from quebradas_limaeste.inventory.live_smoke import (
    HTML_CONTENT_TYPES,
    TRANSIENT_STATUSES,
    HttpResponse,
    HttpTransport,
    PortalQuery,
    UrllibTransport,
)
from quebradas_limaeste.inventory.models import (
    InventoryValidationError,
    normalize_http_url,
)

USER_AGENT = "QuebradasLimaEste-A1-INDECI-Discovery/0.1 (+metadata-only)"


@dataclass(frozen=True)
class ArchiveDiscoverySettings:
    """Request limits shared by the two separately identified archives."""

    queries: tuple[PortalQuery, ...]
    request_delay_seconds: float
    timeout_seconds: float
    retry_backoff_seconds: float
    max_attempts: int
    max_pages_per_query: int


class ArchiveDiscoveryConnector:
    """Sequential one-page metadata collector for one official archive."""

    def __init__(
        self,
        *,
        name: str,
        portal_url: str,
        settings: ArchiveDiscoverySettings,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if re.fullmatch(r"archive_[a-z]+", name) is None:
            raise DiscoveryConfigError("archive connector name is invalid")
        self.name = name
        self.portal_url = _archive_base_url(portal_url)
        self.settings = _validated_settings(settings)
        self.transport = transport or UrllibTransport()
        self.sleep = sleep

    def discover(
        self,
        *,
        years: tuple[int, ...],
        discovered_at_utc: datetime,
    ) -> ConnectorResult:
        candidates: list[DiscoveryCandidate] = []
        warnings: list[str] = []
        errors: list[str] = []
        requests = 0
        pages = 0
        has_requested = False

        for query in self.settings.queries:
            if query.year not in years:
                continue
            request_url = _query_url(self.portal_url, query)
            response: HttpResponse | None = None
            for attempt in range(1, self.settings.max_attempts + 1):
                if has_requested:
                    backoff = (
                        self.settings.retry_backoff_seconds * (2 ** (attempt - 2))
                        if attempt > 1
                        else 0.0
                    )
                    self.sleep(max(self.settings.request_delay_seconds, backoff))
                has_requested = True
                requests += 1
                try:
                    response = self.transport.get(
                        request_url,
                        timeout_seconds=self.settings.timeout_seconds,
                        user_agent=USER_AGENT,
                        max_bytes=MAX_PORTAL_HTML_LENGTH,
                    )
                except TimeoutError:
                    message = _query_message(query, "timeout")
                    if attempt < self.settings.max_attempts:
                        warnings.append(_retry_message(message, attempt, self.settings))
                        continue
                    errors.append(message)
                    response = None
                    break
                except OSError as exc:
                    message = _query_message(query, f"network error: {exc}")
                    if attempt < self.settings.max_attempts:
                        warnings.append(_retry_message(message, attempt, self.settings))
                        continue
                    errors.append(message)
                    response = None
                    break

                if response.status == 200:
                    break
                message = _query_message(query, f"HTTP {response.status}")
                if (
                    response.status in TRANSIENT_STATUSES
                    and attempt < self.settings.max_attempts
                ):
                    warnings.append(_retry_message(message, attempt, self.settings))
                    response = None
                    continue
                errors.append(message)
                response = None
                break

            if response is None:
                continue
            if response.content_type.lower() not in HTML_CONTENT_TYPES:
                errors.append(
                    _query_message(
                        query,
                        f"unexpected content type {response.content_type or 'missing'}",
                    )
                )
                continue
            if not response.body:
                errors.append(_query_message(query, "empty response"))
                continue
            try:
                html = response.body.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(_query_message(query, "non-UTF-8 response"))
                continue
            try:
                parsed = parse_indeci_portal_html(html, page_url=request_url)
            except PortalParseError as exc:
                errors.append(_query_message(query, f"unexpected portal HTML: {exc}"))
                continue

            pages += 1
            warnings.extend(
                _query_message(query, warning) for warning in parsed.warnings
            )
            if parsed.reported_count == 0:
                warnings.append(_query_message(query, "no results"))
            if parsed.next_page_url is not None:
                if _same_query_pagination(parsed.next_page_url, query, self.portal_url):
                    warnings.append(
                        _query_message(
                            query,
                            "pagination capped at 1 page; remaining results "
                            "not requested",
                        )
                    )
                else:
                    warnings.append(
                        _query_message(query, "unexpected pagination URL ignored")
                    )

            for portal_candidate in parsed.candidates:
                try:
                    candidates.append(
                        DiscoveryCandidate.create(
                            source_connector=self.name,
                            title=portal_candidate.title,
                            detail_url=portal_candidate.detail_url,
                            pdf_url=portal_candidate.pdf_url,
                            report_type=portal_candidate.document_type,
                            report_number=portal_candidate.report_number,
                            report_date=portal_candidate.report_date,
                            year=query.year,
                            discovered_at_utc=discovered_at_utc,
                            query_context={
                                "archive_url": self.portal_url,
                                "title": query.title,
                                "year": query.year,
                                "page": 1,
                            },
                            raw_metadata={
                                "reported_count": parsed.reported_count,
                            },
                            discovery_warnings=portal_candidate.warnings,
                        )
                    )
                except DiscoveryConfigError as exc:
                    warnings.append(
                        _query_message(query, f"candidate metadata rejected: {exc}")
                    )

        return ConnectorResult(
            connector=self.name,
            candidates=tuple(candidates),
            requests=requests,
            pages=pages,
            warnings=tuple(warnings),
            errors=tuple(errors),
        )


def _validated_settings(value: ArchiveDiscoverySettings) -> ArchiveDiscoverySettings:
    if not isinstance(value, ArchiveDiscoverySettings):
        raise DiscoveryConfigError("archive settings are invalid")
    if not value.queries or not all(
        isinstance(query, PortalQuery) for query in value.queries
    ):
        raise DiscoveryConfigError("archive queries are invalid")
    if value.max_pages_per_query != 1:
        raise DiscoveryConfigError("archive discovery is capped at one page per query")
    if not 1 <= value.max_attempts <= 3:
        raise DiscoveryConfigError("archive max_attempts is outside the allowed range")
    for number, minimum, maximum, field in (
        (value.request_delay_seconds, 0.5, 30, "request_delay_seconds"),
        (value.timeout_seconds, 1, 60, "timeout_seconds"),
        (value.retry_backoff_seconds, 0.1, 30, "retry_backoff_seconds"),
    ):
        if isinstance(number, bool) or not isinstance(number, int | float):
            raise DiscoveryConfigError(f"{field} must be numeric")
        if not minimum <= float(number) <= maximum:
            raise DiscoveryConfigError(f"{field} is outside the allowed range")
    return value


def _archive_base_url(value: str) -> str:
    try:
        normalized = normalize_http_url(value)
    except InventoryValidationError as exc:
        raise DiscoveryConfigError(str(exc)) from exc
    parsed = urlparse(normalized)
    if (
        parsed.scheme != "https"
        or parsed.hostname != INDECI_PORTAL_HOST
        or parsed.port not in {None, 443}
        or parsed.path not in INDECI_ARCHIVE_PATHS
        or parsed.query
    ):
        raise DiscoveryConfigError("archive URL is not allowlisted")
    return normalized


def _query_url(portal_url: str, query: PortalQuery) -> str:
    return f"{portal_url}?{urlencode(query.to_params())}"


def _same_query_pagination(
    value: str,
    query: PortalQuery,
    portal_url: str,
) -> bool:
    try:
        normalized = normalize_http_url(value)
    except InventoryValidationError:
        return False
    parsed = urlparse(normalized)
    base = urlparse(portal_url)
    path_pattern = rf"^{re.escape(base.path)}page/[1-9]\d*/$"
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == INDECI_PORTAL_HOST
        and re.fullmatch(path_pattern, parsed.path)
        and dict(parse_qsl(parsed.query, keep_blank_values=True)) == query.to_params()
    )


def _query_message(query: PortalQuery, message: str) -> str:
    clean = re.sub(r"\s+", " ", message).strip()[:350]
    return f"query {query.title!r}/{query.year}: {clean}"


def _retry_message(
    message: str,
    attempt: int,
    settings: ArchiveDiscoverySettings,
) -> str:
    return f"{message} on attempt {attempt}/{settings.max_attempts}; retrying"
