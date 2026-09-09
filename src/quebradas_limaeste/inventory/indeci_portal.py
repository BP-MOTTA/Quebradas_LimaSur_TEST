"""Pure parser for INDECI portal result pages treated as untrusted input."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

from quebradas_limaeste.inventory.models import (
    InventoryValidationError,
    normalize_http_url,
)

INDECI_PORTAL_HOST = "portal.indeci.gob.pe"
INDECI_RESULTS_PATH = (
    "/informe/reportes-preliminares-complementarios-emergencias/"
)
INDECI_EMERGENCY_PATH = "/informe/informe-de-emergencia/"
INDECI_ARCHIVE_PATHS = (INDECI_RESULTS_PATH, INDECI_EMERGENCY_PATH)
MAX_PORTAL_HTML_LENGTH = 2_000_000
IGNORED_TAGS = frozenset({"script", "style"})
HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

_COUNT_PATTERN = re.compile(r"alertas\s+encontradas\s*:\s*([\d.,]+)", re.I)
_NUMERIC_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4})(?!\d)"
)
_COMPACT_DATE_PATTERN = re.compile(
    r"(?<![a-z0-9])(?P<day>\d{1,2})(?P<month>[a-z]{3})(?P<year>\d{4})(?![a-z0-9])"
)
_COMPACT_MONTHS = {
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
_DOCUMENT_TYPES = (
    ("reporte complementario", "Reporte Complementario"),
    ("informe de emergencia", "Informe de Emergencia"),
    ("reporte preliminar", "Reporte Preliminar"),
)


class PortalParseError(ValueError):
    """Raised when a response is not a recognizable INDECI results page."""


@dataclass(frozen=True)
class PortalCandidate:
    """Metadata extracted from one INDECI result card without following links."""

    title: str
    detail_url: str | None
    pdf_url: str | None
    document_type: str | None
    report_number: str | None
    report_date: date | None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "detail_url": self.detail_url,
            "pdf_url": self.pdf_url,
            "type": self.document_type,
            "number": self.report_number,
            "report_date": (
                self.report_date.isoformat() if self.report_date is not None else None
            ),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PortalParseResult:
    candidates: tuple[PortalCandidate, ...]
    reported_count: int
    next_page_url: str | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoldenCases:
    golden_2019_found: bool
    golden_2023_found: bool
    golden_2019: PortalCandidate | None = None
    golden_2023: PortalCandidate | None = None


@dataclass
class _CardBuilder:
    title_parts: list[str] = field(default_factory=list)
    title_href: str | None = None
    download_href: str | None = None
    detail_href: str | None = None


class _PortalHTMLParser(HTMLParser):
    def __init__(self, *, page_url: str, archive_path: str) -> None:
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.archive_path = archive_path
        self.candidates: list[PortalCandidate] = []
        self.warnings: list[str] = []
        self.reported_count: int | None = None
        self.next_page_url: str | None = None
        self.saw_results_section = False
        self._results_depth = 0
        self._card_depth = 0
        self._h3_depth = 0
        self._h4_depth = 0
        self._skip_depth = 0
        self._h4_parts: list[str] = []
        self._current: _CardBuilder | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        is_void = tag_name in HTML_VOID_ELEMENTS
        attrs_by_name = {name.lower(): value for name, value in attrs}
        classes = _class_names(attrs_by_name.get("class"))

        if self._results_depth > 0 and not is_void:
            self._results_depth += 1
        elif tag_name == "section" and "alerts-archive" in classes:
            self._results_depth = 1
            self.saw_results_section = True

        if self._current is not None and not is_void:
            self._card_depth += 1
        elif self._results_depth > 0 and tag_name == "article":
            self._current = _CardBuilder()
            self._card_depth = 1

        if self._h4_depth > 0 and not is_void:
            self._h4_depth += 1
        elif tag_name == "h4":
            self._h4_depth = 1
            self._h4_parts.clear()

        if self._current is not None:
            if self._h3_depth > 0 and not is_void:
                self._h3_depth += 1
            elif tag_name == "h3":
                self._h3_depth = 1

            if tag_name == "a":
                href = attrs_by_name.get("href")
                if self._h3_depth > 0 and href:
                    self._current.title_href = href
                if "btn-download" in classes and href:
                    self._current.download_href = href
                if "lnk-view" in classes and href:
                    self._current.detail_href = href

        if (
            tag_name == "a"
            and "next" in classes
            and "page-numbers" in classes
            and attrs_by_name.get("href")
        ):
            try:
                next_page_url, next_archive_path = _safe_archive_page_url(
                    attrs_by_name["href"],
                    base_url=self.page_url,
                )
                if next_archive_path != self.archive_path:
                    raise InventoryValidationError(
                        "pagination URL changed the archive path"
                    )
                self.next_page_url = next_page_url
            except InventoryValidationError as exc:
                self.warnings.append(f"pagination URL rejected: {exc}")

        if tag_name in IGNORED_TAGS:
            self._skip_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name in HTML_VOID_ELEMENTS:
            return

        if self._h3_depth > 0:
            self._h3_depth -= 1

        if self._h4_depth > 0:
            self._h4_depth -= 1
            if self._h4_depth == 0:
                self._parse_reported_count()

        if tag_name in IGNORED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if self._current is not None:
            self._card_depth -= 1
            if self._card_depth == 0:
                self._finalize_card()

        if self._results_depth > 0:
            self._results_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._h4_depth > 0:
            self._h4_parts.append(data)
        if self._current is not None and self._h3_depth > 0:
            self._current.title_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._current is not None:
            self.warnings.append("skipped unclosed result card")
            self._current = None

    def _parse_reported_count(self) -> None:
        text = _normalize_space(" ".join(self._h4_parts))
        match = _COUNT_PATTERN.search(text)
        if match is not None:
            self.reported_count = int(re.sub(r"[.,]", "", match.group(1)))

    def _finalize_card(self) -> None:
        if self._current is None:
            return
        builder = self._current
        self._current = None
        title = _normalize_space(" ".join(builder.title_parts))
        if not title:
            self.warnings.append("skipped result card without title")
            return

        warnings: list[str] = []
        pdf_url = _optional_card_url(
            builder.download_href,
            base_url=self.page_url,
            label="download",
            warnings=warnings,
            expected_path_prefix="/wp-content/uploads/",
        )
        detail_url = _optional_card_url(
            builder.detail_href or builder.title_href,
            base_url=self.page_url,
            label="detail",
            warnings=warnings,
            expected_path_prefix="/emergencias/",
        )
        document_type, report_number, report_date = _extract_metadata(
            title=title,
            pdf_url=pdf_url,
            detail_url=detail_url,
        )
        self.candidates.append(
            PortalCandidate(
                title=title,
                detail_url=detail_url,
                pdf_url=pdf_url,
                document_type=document_type,
                report_number=report_number,
                report_date=report_date,
                warnings=tuple(warnings),
            )
        )


def parse_indeci_portal_html(html: str, *, page_url: str) -> PortalParseResult:
    """Parse one result page without following card or pagination links."""
    if not isinstance(html, str) or not html.strip():
        raise PortalParseError("empty portal response")
    if len(html) > MAX_PORTAL_HTML_LENGTH:
        raise PortalParseError("portal response is too large")

    safe_page_url, archive_path = _safe_archive_page_url(page_url)
    parser = _PortalHTMLParser(
        page_url=safe_page_url,
        archive_path=archive_path,
    )
    parser.feed(html)
    parser.close()

    if not parser.saw_results_section or parser.reported_count is None:
        raise PortalParseError("unexpected portal HTML structure")
    if parser.reported_count > 0 and not parser.candidates:
        raise PortalParseError("portal reported results but no cards were parsed")
    return PortalParseResult(
        candidates=tuple(parser.candidates),
        reported_count=parser.reported_count,
        next_page_url=parser.next_page_url,
        warnings=tuple(parser.warnings),
    )


def detect_golden_cases(candidates: tuple[PortalCandidate, ...]) -> GoldenCases:
    """Locate the two approved golden reports using traceable metadata."""
    golden_2019 = next(
        (candidate for candidate in candidates if _is_golden_2019(candidate)),
        None,
    )
    golden_2023 = next(
        (candidate for candidate in candidates if _is_golden_2023(candidate)),
        None,
    )
    return GoldenCases(
        golden_2019_found=golden_2019 is not None,
        golden_2023_found=golden_2023 is not None,
        golden_2019=golden_2019,
        golden_2023=golden_2023,
    )


def _optional_card_url(
    raw_url: str | None,
    *,
    base_url: str,
    label: str,
    warnings: list[str],
    expected_path_prefix: str,
) -> str | None:
    if not raw_url:
        warnings.append(f"card has no {label} link")
        return None
    try:
        return _safe_portal_url(
            raw_url,
            base_url=base_url,
            expected_path_prefix=expected_path_prefix,
        )
    except InventoryValidationError as exc:
        warnings.append(f"{label} URL rejected: {exc}")
        return None


def _safe_portal_url(
    raw_url: str,
    *,
    base_url: str | None = None,
    expected_path_prefix: str,
) -> str:
    normalized = normalize_http_url(raw_url, base_url=base_url)
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise InventoryValidationError("portal URLs must use https")
    if parsed.hostname != INDECI_PORTAL_HOST:
        raise InventoryValidationError("host is not allowlisted")
    if not parsed.path.startswith(expected_path_prefix):
        raise InventoryValidationError("path is not allowlisted")
    return normalized


def _safe_archive_page_url(
    raw_url: str,
    *,
    base_url: str | None = None,
) -> tuple[str, str]:
    normalized = normalize_http_url(raw_url, base_url=base_url)
    parsed = urlparse(normalized)
    if parsed.scheme != "https":
        raise InventoryValidationError("portal URLs must use https")
    if parsed.hostname != INDECI_PORTAL_HOST:
        raise InventoryValidationError("host is not allowlisted")
    for archive_path in INDECI_ARCHIVE_PATHS:
        path_pattern = rf"^{re.escape(archive_path)}(?:page/\d+/)?$"
        if re.fullmatch(path_pattern, parsed.path) is not None:
            return normalized, archive_path
    raise InventoryValidationError("archive path is not allowlisted")


def _extract_metadata(
    *,
    title: str,
    pdf_url: str | None,
    detail_url: str | None,
) -> tuple[str | None, str | None, date | None]:
    sources = tuple(
        unquote(value) for value in (pdf_url, title, detail_url) if value is not None
    )
    document_type: str | None = None
    report_number: str | None = None
    report_date: date | None = None
    for source in sources:
        normalized = _semantic_text(source)
        source_type, source_number = _extract_type_and_number(normalized)
        document_type = document_type or source_type
        report_number = report_number or source_number
        report_date = report_date or _extract_date(source, normalized)
    return document_type, report_number, report_date


def _extract_type_and_number(value: str) -> tuple[str | None, str | None]:
    for type_key, display_name in _DOCUMENT_TYPES:
        match = re.search(
            rf"\b{type_key}\s+(?:n\s*(?:ro|umero|o)?\s+)?"
            rf"(?P<number>\d{{1,6}})\b",
            value,
        )
        if match is not None:
            return display_name, str(int(match["number"]))
        if type_key in value:
            return display_name, None
    return None, None


def _extract_date(raw_value: str, normalized_value: str) -> date | None:
    numeric = _NUMERIC_DATE_PATTERN.search(raw_value)
    if numeric is not None:
        return _safe_date(
            int(numeric["year"]),
            int(numeric["month"]),
            int(numeric["day"]),
        )

    compact = _COMPACT_DATE_PATTERN.search(normalized_value)
    if compact is None:
        return None
    month = _COMPACT_MONTHS.get(compact["month"])
    if month is None:
        return None
    return _safe_date(int(compact["year"]), month, int(compact["day"]))


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _is_golden_2019(candidate: PortalCandidate) -> bool:
    text = _candidate_search_text(candidate)
    return (
        candidate.document_type == "Reporte Complementario"
        and candidate.report_number == "630"
        and candidate.report_date == date(2019, 3, 3)
        and all(term in text for term in ("huaico", "lurigancho", "chaclacayo", "lima"))
    )


def _is_golden_2023(candidate: PortalCandidate) -> bool:
    text = _candidate_search_text(candidate)
    return (
        candidate.document_type == "Informe de Emergencia"
        and candidate.report_number == "1496"
        and candidate.report_date == date(2023, 5, 5)
        and "lluvias intensas" in text
        and "departamento de lima" in text
    )


def _candidate_search_text(candidate: PortalCandidate) -> str:
    return _semantic_text(
        " ".join(
            value
            for value in (candidate.title, candidate.pdf_url, candidate.detail_url)
            if value is not None
        )
    )


def _semantic_text(value: str) -> str:
    decoded = unquote(value)
    decomposed = unicodedata.normalize("NFKD", decoded).casefold()
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()


def _class_names(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(value.lower().split())


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
