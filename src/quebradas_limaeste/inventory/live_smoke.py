"""Bounded, explicitly invoked live smoke check for public INDECI metadata."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from quebradas_limaeste.inventory.indeci_portal import (
    INDECI_ARCHIVE_PATHS,
    INDECI_PORTAL_HOST,
    MAX_PORTAL_HTML_LENGTH,
    PortalCandidate,
    PortalParseError,
    detect_golden_cases,
    parse_indeci_portal_html,
)
from quebradas_limaeste.inventory.models import (
    InventoryValidationError,
    normalize_http_url,
)

USER_AGENT = "QuebradasLimaEste-A1-INDECI-LiveSmoke/0.1 (+research metadata-only)"
MAX_CONFIG_BYTES = 65_536
MAX_LIVE_QUERIES = 4
MAX_LIVE_PAGES_PER_QUERY = 2
MAX_RESPONSE_BYTES = MAX_PORTAL_HTML_LENGTH
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
ALLOWED_QUERY_KEYS = frozenset({"title", "tipo_alerta", "anos_alertas"})
HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
OUTPUT_FILENAME = Path("metadata/indeci/live_smoke_results.json")


class LiveSmokeConfigError(ValueError):
    """Raised when live-smoke configuration or output paths are unsafe."""


@dataclass(frozen=True)
class PortalQuery:
    title: str
    alert_type: str
    year: int

    @classmethod
    def create(
        cls,
        *,
        title: str,
        year: int,
        alert_type: str = "",
    ) -> PortalQuery:
        clean_title = _clean_text(title, "query title", max_length=180)
        clean_type = _clean_text(
            alert_type,
            "query type",
            max_length=120,
            allow_empty=True,
        )
        if isinstance(year, bool) or not isinstance(year, int):
            raise LiveSmokeConfigError("query year must be an integer")
        if year < 2012 or year > 2100:
            raise LiveSmokeConfigError("query year must be between 2012 and 2100")
        return cls(title=clean_title, alert_type=clean_type, year=year)

    def to_dict(self) -> dict[str, object]:
        return {"title": self.title, "type": self.alert_type, "year": self.year}

    def to_params(self) -> dict[str, str]:
        return {
            "title": self.title,
            "tipo_alerta": self.alert_type,
            "anos_alertas": str(self.year),
        }


@dataclass(frozen=True)
class LiveSmokeConfig:
    portal_url: str
    request_delay_seconds: float
    timeout_seconds: float
    retry_backoff_seconds: float
    max_attempts: int
    max_pages_per_query: int
    queries: tuple[PortalQuery, ...]

    @classmethod
    def create(
        cls,
        *,
        portal_url: str,
        request_delay_seconds: float,
        timeout_seconds: float,
        retry_backoff_seconds: float,
        max_attempts: int,
        max_pages_per_query: int,
        queries: tuple[PortalQuery, ...],
    ) -> LiveSmokeConfig:
        safe_portal_url = _validate_portal_request_url(portal_url)
        parsed_portal_url = urlparse(safe_portal_url)
        if (
            parsed_portal_url.path not in INDECI_ARCHIVE_PATHS
            or parsed_portal_url.query
        ):
            raise LiveSmokeConfigError(
                "portal_url must be the unfiltered archive base URL"
            )
        _require_range(
            request_delay_seconds,
            "request_delay_seconds",
            minimum=0.5,
            maximum=30.0,
        )
        _require_range(
            timeout_seconds,
            "timeout_seconds",
            minimum=1.0,
            maximum=60.0,
        )
        _require_range(
            retry_backoff_seconds,
            "retry_backoff_seconds",
            minimum=0.1,
            maximum=30.0,
        )
        _require_integer_range(max_attempts, "max_attempts", minimum=1, maximum=3)
        _require_integer_range(
            max_pages_per_query,
            "max_pages_per_query",
            minimum=1,
            maximum=MAX_LIVE_PAGES_PER_QUERY,
        )
        if not isinstance(queries, tuple) or not queries:
            raise LiveSmokeConfigError("queries must contain at least one query")
        if len(queries) > MAX_LIVE_QUERIES:
            raise LiveSmokeConfigError(
                f"queries cannot contain more than {MAX_LIVE_QUERIES} entries"
            )
        if not all(isinstance(query, PortalQuery) for query in queries):
            raise LiveSmokeConfigError("queries must contain PortalQuery values")
        return cls(
            portal_url=safe_portal_url,
            request_delay_seconds=float(request_delay_seconds),
            timeout_seconds=float(timeout_seconds),
            retry_backoff_seconds=float(retry_backoff_seconds),
            max_attempts=max_attempts,
            max_pages_per_query=max_pages_per_query,
            queries=queries,
        )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    content_type: str


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        user_agent: str,
        max_bytes: int,
    ) -> HttpResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Small HTTPS transport that refuses redirects and caps response bytes."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        user_agent: str,
        max_bytes: int,
    ) -> HttpResponse:
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "User-Agent": user_agent,
            },
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise OSError("portal response exceeds configured byte limit")
                return HttpResponse(
                    status=response.status,
                    body=body,
                    content_type=response.headers.get_content_type(),
                )
        except HTTPError as exc:
            content_type = (
                exc.headers.get_content_type() if exc.headers is not None else ""
            )
            return HttpResponse(status=exc.code, body=b"", content_type=content_type)
        except URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise TimeoutError("portal request timed out") from exc
            raise OSError(f"portal request failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise TimeoutError("portal request timed out") from exc


@dataclass(frozen=True)
class LiveSmokeResult:
    run_id: str
    timestamp: datetime
    portal_url: str
    queries_attempted: tuple[dict[str, object], ...]
    pages_requested: int
    documents_seen: int
    candidates: tuple[PortalCandidate, ...]
    golden_2019_found: bool
    golden_2023_found: bool
    warnings: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp.isoformat(),
            "portal_url": self.portal_url,
            "queries_attempted": list(self.queries_attempted),
            "pages_requested": self.pages_requested,
            "documents_seen": self.documents_seen,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "golden_2019_found": self.golden_2019_found,
            "golden_2023_found": self.golden_2023_found,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def load_live_smoke_config(path: Path) -> LiveSmokeConfig:
    """Load a strict JSON-compatible YAML configuration file."""
    config_path = Path(path)
    if config_path.suffix.lower() not in {".yaml", ".yml"}:
        raise LiveSmokeConfigError("config must use a .yaml or .yml suffix")
    if not config_path.is_file():
        raise LiveSmokeConfigError(f"config file not found: {config_path}")
    if config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise LiveSmokeConfigError("config file is too large")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise LiveSmokeConfigError(
            "config must be UTF-8 JSON-compatible YAML"
        ) from exc
    if not isinstance(raw, dict):
        raise LiveSmokeConfigError("config root must be an object")

    expected_keys = {
        "portal_url",
        "request_delay_seconds",
        "timeout_seconds",
        "retry_backoff_seconds",
        "max_attempts",
        "max_pages_per_query",
        "queries",
    }
    optional_keys = {"ingestion", "discovery", "batch"}
    if not expected_keys <= set(raw) or not set(raw) <= expected_keys | optional_keys:
        raise LiveSmokeConfigError("config keys do not match the live-smoke schema")
    raw_queries = raw["queries"]
    if not isinstance(raw_queries, list):
        raise LiveSmokeConfigError("queries must be a list")

    queries: list[PortalQuery] = []
    for index, raw_query in enumerate(raw_queries, start=1):
        if not isinstance(raw_query, dict) or set(raw_query) != {
            "title",
            "type",
            "year",
        }:
            raise LiveSmokeConfigError(f"query {index} has invalid keys")
        queries.append(
            PortalQuery.create(
                title=raw_query["title"],
                alert_type=raw_query["type"],
                year=raw_query["year"],
            )
        )

    return LiveSmokeConfig.create(
        portal_url=raw["portal_url"],
        request_delay_seconds=raw["request_delay_seconds"],
        timeout_seconds=raw["timeout_seconds"],
        retry_backoff_seconds=raw["retry_backoff_seconds"],
        max_attempts=raw["max_attempts"],
        max_pages_per_query=raw["max_pages_per_query"],
        queries=tuple(queries),
    )


def run_live_smoke(
    config: LiveSmokeConfig,
    *,
    transport: HttpTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> LiveSmokeResult:
    """Run bounded GET requests sequentially and parse metadata only."""
    active_transport = transport or UrllibTransport()
    timestamp = now()
    if timestamp.tzinfo is None:
        raise LiveSmokeConfigError("live-smoke timestamp must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)
    warnings: list[str] = []
    errors: list[str] = []
    candidates: list[PortalCandidate] = []
    query_summaries: list[dict[str, object]] = []
    pages_requested = 0
    documents_seen = 0
    has_requested = False

    for query in config.queries:
        request_url: str | None = _build_query_url(config.portal_url, query)
        pages_parsed = 0
        query_requests_before = pages_requested

        while request_url is not None and pages_parsed < config.max_pages_per_query:
            try:
                safe_request_url = _validate_portal_request_url(
                    request_url,
                    expected_query=query,
                )
            except LiveSmokeConfigError as exc:
                errors.append(
                    _request_message(
                        query,
                        pages_parsed + 1,
                        f"rejected request URL: {exc}",
                    )
                )
                break
            response: HttpResponse | None = None
            for attempt in range(1, config.max_attempts + 1):
                if has_requested:
                    backoff = (
                        config.retry_backoff_seconds * (2 ** (attempt - 2))
                        if attempt > 1
                        else 0.0
                    )
                    sleep(max(config.request_delay_seconds, backoff))
                has_requested = True
                pages_requested += 1

                try:
                    response = active_transport.get(
                        safe_request_url,
                        timeout_seconds=config.timeout_seconds,
                        user_agent=USER_AGENT,
                        max_bytes=MAX_RESPONSE_BYTES,
                    )
                except TimeoutError:
                    message = _request_message(query, pages_parsed + 1, "timeout")
                    if attempt < config.max_attempts:
                        warnings.append(
                            f"{message} on attempt "
                            f"{attempt}/{config.max_attempts}; retrying"
                        )
                        continue
                    errors.append(message)
                    response = None
                    break
                except OSError as exc:
                    message = _request_message(
                        query,
                        pages_parsed + 1,
                        f"network error: {exc}",
                    )
                    if attempt < config.max_attempts:
                        warnings.append(
                            f"{message} on attempt "
                            f"{attempt}/{config.max_attempts}; retrying"
                        )
                        continue
                    errors.append(message)
                    response = None
                    break

                if response.status == 200:
                    break
                message = _request_message(
                    query,
                    pages_parsed + 1,
                    f"HTTP {response.status}",
                )
                if (
                    response.status in TRANSIENT_STATUSES
                    and attempt < config.max_attempts
                ):
                    warnings.append(
                        f"{message} on attempt "
                        f"{attempt}/{config.max_attempts}; retrying"
                    )
                    response = None
                    continue
                errors.append(message)
                response = None
                break

            if response is None:
                break
            if response.content_type.lower() not in HTML_CONTENT_TYPES:
                errors.append(
                    _request_message(
                        query,
                        pages_parsed + 1,
                        f"unexpected content type {response.content_type or 'missing'}",
                    )
                )
                break
            if not response.body:
                errors.append(
                    _request_message(query, pages_parsed + 1, "empty response")
                )
                break
            try:
                html = response.body.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(
                    _request_message(query, pages_parsed + 1, "non-UTF-8 response")
                )
                break

            try:
                parsed = parse_indeci_portal_html(html, page_url=request_url)
            except PortalParseError as exc:
                errors.append(
                    _request_message(
                        query,
                        pages_parsed + 1,
                        f"unexpected portal HTML: {exc}",
                    )
                )
                break

            pages_parsed += 1
            documents_seen += len(parsed.candidates)
            candidates.extend(parsed.candidates)
            warnings.extend(
                _request_message(query, pages_parsed, warning)
                for warning in parsed.warnings
            )
            warnings.extend(
                _request_message(query, pages_parsed, warning)
                for candidate in parsed.candidates
                for warning in candidate.warnings
            )
            request_url = parsed.next_page_url

        if request_url is not None and pages_parsed >= config.max_pages_per_query:
            warnings.append(
                f"query {query.title!r}: pagination capped at "
                f"{config.max_pages_per_query} page(s)"
            )
        summary = query.to_dict()
        summary["requests_made"] = pages_requested - query_requests_before
        summary["pages_parsed"] = pages_parsed
        query_summaries.append(summary)

    unique_candidates = _deduplicate_candidates(candidates)
    golden = detect_golden_cases(unique_candidates)
    return LiveSmokeResult(
        run_id=f"indeci-live-smoke-{timestamp.strftime('%Y%m%dT%H%M%SZ')}",
        timestamp=timestamp,
        portal_url=config.portal_url,
        queries_attempted=tuple(query_summaries),
        pages_requested=pages_requested,
        documents_seen=documents_seen,
        candidates=unique_candidates,
        golden_2019_found=golden.golden_2019_found,
        golden_2023_found=golden.golden_2023_found,
        warnings=tuple(warnings),
        errors=tuple(errors),
    )


def write_live_smoke_result(
    path: Path,
    result: LiveSmokeResult,
    *,
    allowed_root: Path,
) -> Path:
    root = Path(allowed_root).resolve()
    raw_target = Path(path)
    if raw_target.is_symlink():
        raise LiveSmokeConfigError("output path must not be a symbolic link")
    target = (
        raw_target.resolve()
        if raw_target.is_absolute()
        else (root / raw_target).resolve()
    )
    if target == root or root not in target.parents:
        raise LiveSmokeConfigError("output path must remain inside the allowed root")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return target


def execute_live_smoke(
    config_path: Path,
    *,
    output_path: Path = OUTPUT_FILENAME,
    allowed_root: Path,
) -> dict[str, object]:
    config = load_live_smoke_config(config_path)
    result = run_live_smoke(config)
    write_live_smoke_result(output_path, result, allowed_root=allowed_root)
    return result.to_dict()


def _build_query_url(portal_url: str, query: PortalQuery) -> str:
    return f"{portal_url}?{urlencode(query.to_params())}"


def _validate_portal_request_url(
    raw_url: str,
    *,
    expected_query: PortalQuery | None = None,
) -> str:
    try:
        normalized = normalize_http_url(raw_url)
    except InventoryValidationError as exc:
        raise LiveSmokeConfigError(str(exc)) from exc
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise LiveSmokeConfigError("portal URL must use https")
    if parsed.hostname != INDECI_PORTAL_HOST:
        raise LiveSmokeConfigError("portal host is not allowlisted")
    if not any(
        re.fullmatch(rf"^{re.escape(path)}(?:page/\d+/)?$", parsed.path)
        for path in INDECI_ARCHIVE_PATHS
    ):
        raise LiveSmokeConfigError("portal path is not allowlisted")
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key not in ALLOWED_QUERY_KEYS for key, _ in query_pairs):
        raise LiveSmokeConfigError("portal query contains an unsupported parameter")
    if expected_query is not None:
        query_params = dict(query_pairs)
        filters_match = (
            len(query_pairs) == len(query_params)
            and query_params.get("title") == expected_query.title
            and query_params.get("anos_alertas") == str(expected_query.year)
            and query_params.get("tipo_alerta", "") == expected_query.alert_type
        )
        if not filters_match:
            raise LiveSmokeConfigError(
                "portal query filters do not match the configured query"
            )
    return normalized


def _deduplicate_candidates(
    candidates: list[PortalCandidate],
) -> tuple[PortalCandidate, ...]:
    unique: list[PortalCandidate] = []
    seen: set[tuple[str, str | None, str | None]] = set()
    for candidate in candidates:
        key = (candidate.title, candidate.detail_url, candidate.pdf_url)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _request_message(query: PortalQuery, page: int, message: str) -> str:
    return f"query {query.title!r} year {query.year} page {page}: {message}"


def _clean_text(
    value: object,
    field_name: str,
    *,
    max_length: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise LiveSmokeConfigError(f"{field_name} must be a string")
    clean = " ".join(value.split())
    if not clean and not allow_empty:
        raise LiveSmokeConfigError(f"{field_name} is required")
    if len(clean) > max_length:
        raise LiveSmokeConfigError(f"{field_name} is too long")
    return clean


def _require_range(
    value: object,
    field_name: str,
    *,
    minimum: float,
    maximum: float,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiveSmokeConfigError(f"{field_name} must be numeric")
    if not minimum <= float(value) <= maximum:
        raise LiveSmokeConfigError(
            f"{field_name} must be between {minimum} and {maximum}"
        )


def _require_integer_range(
    value: object,
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LiveSmokeConfigError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise LiveSmokeConfigError(
            f"{field_name} must be between {minimum} and {maximum}"
        )
