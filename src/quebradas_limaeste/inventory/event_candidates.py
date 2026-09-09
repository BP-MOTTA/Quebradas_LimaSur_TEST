"""Review-only event candidates from explicit dates and nearby facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from quebradas_limaeste.inventory.document_classification import (
    EVENT_TERMS,
    contains_term,
    semantic_text,
)

MAX_SNIPPET_CHARACTERS = 360
MAX_PREFIX_CHARACTERS = 80
MAX_CANDIDATES = 500
_MONTHS = {
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
_DATE_PATTERN = re.compile(
    r"\b(?:el\s+)?(?P<day>\d{1,2})\s+de\s+"
    r"(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)\s+"
    r"(?:de(?:l)?\s+)?(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_FACT_TERMS = (
    "se registro",
    "se registraron",
    "se produjo",
    "se presento",
    "se presentaron",
    "se reporto",
    "ocurrio",
    "a consecuencia",
    "debido a",
    "causo",
    "causaron",
    "ocasiono",
    "ocasionaron",
    "provoco",
    "afecto",
)


@dataclass(frozen=True)
class EventCandidate:
    """A source-linked hypothesis that cannot become ground truth automatically."""

    source_document_id: str
    source_page: int
    event_date: date
    evidence_snippet: str
    matched_terms: tuple[str, ...]
    validation_status: str = "pending_review"

    def to_dict(self) -> dict[str, object]:
        return {
            "source_document_id": self.source_document_id,
            "source_page": self.source_page,
            "event_date": self.event_date.isoformat(),
            "evidence_snippet": self.evidence_snippet,
            "matched_terms": list(self.matched_terms),
            "validation_status": self.validation_status,
        }


def extract_event_candidates(
    document_id: str,
    page_texts: tuple[str, ...],
    *,
    expected_site_terms: tuple[str, ...],
    expected_region_terms: tuple[str, ...],
) -> tuple[EventCandidate, ...]:
    """Return candidates only when an explicit date is tied to event language."""
    candidates: list[EventCandidate] = []
    seen: set[tuple[int, date, str]] = set()
    geographic_terms = (*expected_site_terms, *expected_region_terms)
    for page_number, page_text in enumerate(page_texts, start=1):
        normalized_page = _normalize_space(page_text)
        for match in _DATE_PATTERN.finditer(normalized_page):
            snippet = _evidence_window(normalized_page, match.start(), match.end())
            semantic_snippet = semantic_text(snippet)
            matched_events = tuple(
                term for term in EVENT_TERMS if contains_term(semantic_snippet, term)
            )
            fact_match = any(
                contains_term(semantic_snippet, term) for term in _FACT_TERMS
            )
            if not matched_events or not fact_match:
                continue
            try:
                event_date = date(
                    int(match.group("year")),
                    _MONTHS[semantic_text(match.group("month"))],
                    int(match.group("day")),
                )
            except (KeyError, ValueError):
                continue
            matched_geography = tuple(
                term
                for term in geographic_terms
                if contains_term(semantic_snippet, term)
            )
            key = (page_number, event_date, snippet)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                EventCandidate(
                    source_document_id=document_id,
                    source_page=page_number,
                    event_date=event_date,
                    evidence_snippet=snippet,
                    matched_terms=tuple(
                        dict.fromkeys((*matched_geography, *matched_events))
                    ),
                )
            )
            if len(candidates) >= MAX_CANDIDATES:
                return tuple(candidates)
    return tuple(candidates)


def _evidence_window(text: str, start: int, end: int) -> str:
    sentence_start = max(text.rfind(mark, 0, start) for mark in ".!?;") + 1
    following = [
        position
        for mark in ".!?;"
        if (position := text.find(mark, end)) >= 0
    ]
    sentence_end = min(following) + 1 if following else len(text)
    left = max(sentence_start, start - MAX_PREFIX_CHARACTERS)
    right = min(sentence_end, left + MAX_SNIPPET_CHARACTERS)
    snippet = text[left:right].strip()
    if left > sentence_start:
        first_space = snippet.find(" ")
        if first_space >= 0:
            snippet = snippet[first_space + 1 :]
    if right < sentence_end:
        last_space = snippet.rfind(" ")
        if last_space >= 0:
            snippet = snippet[:last_space]
    return snippet[:MAX_SNIPPET_CHARACTERS]


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
