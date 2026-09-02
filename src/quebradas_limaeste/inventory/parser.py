"""Offline parsers for untrusted documentary inventory content."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

from quebradas_limaeste.inventory.models import (
    MAX_EVIDENCE_TEXT_LENGTH,
    InventoryRecord,
    InventoryValidationError,
    normalize_http_url,
    parse_document_date,
)

MAX_HTML_INPUT_LENGTH = 2_000_000
MISSING_DATE_WARNING = "published_date missing or unparsed"
IGNORED_TAGS = frozenset({"script", "style"})


@dataclass(frozen=True)
class InventoryParseResult:
    """Result of parsing an offline inventory source."""

    records: tuple[InventoryRecord, ...]
    warnings: tuple[str, ...] = ()


@dataclass
class _RecordBuilder:
    index: int
    title: str | None
    url: str | None
    published_date: str | None
    spatial_precision: str | None
    text_parts: list[str] = field(default_factory=list)
    anchor_text_parts: list[str] = field(default_factory=list)


class _InventoryHTMLParser(HTMLParser):
    def __init__(self, *, source_name: str, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_name = source_name
        self.source_url = normalize_http_url(source_url)
        self.records: list[InventoryRecord] = []
        self.warnings: list[str] = []
        self._current: _RecordBuilder | None = None
        self._record_depth = 0
        self._skip_depth = 0
        self._anchor_depth = 0
        self._record_count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        attrs_by_name = {name.lower(): value for name, value in attrs}

        if self._current is None and "data-inventory-record" in attrs_by_name:
            self._record_count += 1
            self._current = _RecordBuilder(
                index=self._record_count,
                title=attrs_by_name.get("data-title"),
                url=attrs_by_name.get("data-url"),
                published_date=attrs_by_name.get("data-published-date"),
                spatial_precision=attrs_by_name.get("data-spatial-precision"),
            )
            self._record_depth = 1
        elif self._current is not None:
            self._record_depth += 1

        if tag_name in IGNORED_TAGS:
            self._skip_depth += 1
            return

        if self._current is None:
            return

        if self._anchor_depth > 0:
            self._anchor_depth += 1
            return

        if tag_name == "a" and self._current.url is None:
            href = attrs_by_name.get("href")
            if href:
                self._current.url = href
                self._anchor_depth = 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if (
            self._current is not None
            and tag.lower() == "a"
            and self._current.url is None
        ):
            attrs_by_name = {name.lower(): value for name, value in attrs}
            self._current.url = attrs_by_name.get("href")

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()

        if self._anchor_depth > 0:
            self._anchor_depth -= 1
            if self._anchor_depth == 0 and self._current is not None:
                anchor_title = _normalize_space(
                    " ".join(self._current.anchor_text_parts)
                )
                if anchor_title and self._current.title is None:
                    self._current.title = anchor_title
                self._current.anchor_text_parts.clear()

        if tag_name in IGNORED_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

        if self._current is None:
            return

        self._record_depth -= 1
        if self._record_depth == 0:
            self._finalize_current()

    def handle_data(self, data: str) -> None:
        if self._current is None or self._skip_depth > 0:
            return
        self._current.text_parts.append(data)
        if self._anchor_depth > 0:
            self._current.anchor_text_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._current is not None:
            self.warnings.append(
                f"skipped record {self._current.index}: unclosed markup"
            )
            self._current = None
            self._record_depth = 0

    def _finalize_current(self) -> None:
        if self._current is None:
            return

        builder = self._current
        self._current = None
        date_value = builder.published_date
        record_warnings = []
        if parse_document_date(date_value) is None:
            record_warnings.append(MISSING_DATE_WARNING)

        try:
            record = InventoryRecord.create(
                source_name=self.source_name,
                source_url=self.source_url,
                document_url=builder.url or "",
                title=builder.title or "",
                published_date=date_value,
                evidence_text=_make_evidence_snippet(builder.text_parts),
                spatial_precision=builder.spatial_precision,
                warnings=record_warnings,
            )
        except InventoryValidationError as exc:
            self.warnings.append(f"skipped record {builder.index}: {exc}")
            return

        self.records.append(record)


def parse_inventory_html(
    html: str,
    *,
    source_name: str,
    source_url: str,
) -> InventoryParseResult:
    """Parse explicit offline inventory markup without network access."""
    if not isinstance(html, str):
        raise InventoryValidationError("html must be a string")
    if len(html) > MAX_HTML_INPUT_LENGTH:
        raise InventoryValidationError("html input is too large")

    parser = _InventoryHTMLParser(source_name=source_name, source_url=source_url)
    parser.feed(html)
    parser.close()
    return InventoryParseResult(
        records=tuple(parser.records),
        warnings=tuple(parser.warnings),
    )


def _make_evidence_snippet(parts: list[str]) -> str:
    text = _normalize_space(" ".join(parts))
    return text[:MAX_EVIDENCE_TEXT_LENGTH]


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
