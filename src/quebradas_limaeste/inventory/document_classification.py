"""Deterministic review-first classification of extracted INDECI text."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

EVENT_TERMS = (
    "lluvias intensas",
    "lluvia intensa",
    "huaico",
    "huayco",
    "activacion de quebrada",
    "activacion de quebradas",
    "activacion de la quebrada",
    "activacion de las quebradas",
    "se activo la quebrada",
    "se activaron las quebradas",
    "flujo de detritos",
    "deslizamiento",
    "inundacion",
)


@dataclass(frozen=True)
class DocumentClassification:
    """Transparent term matches that always require human review."""

    geographic_match: bool
    site_match: bool
    event_match: bool
    relevance_status: str
    matched_terms: tuple[str, ...]
    review_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "geographic_match": self.geographic_match,
            "site_match": self.site_match,
            "event_match": self.event_match,
            "relevance_status": self.relevance_status,
            "matched_terms": list(self.matched_terms),
            "review_required": self.review_required,
        }


def classify_document(
    text: str,
    *,
    expected_site_terms: tuple[str, ...],
    expected_region_terms: tuple[str, ...],
) -> DocumentClassification:
    """Classify text with explicit, auditable vocabulary matches."""
    semantic_text = _semantic_text(text)
    matched_sites = _matched_config_terms(semantic_text, expected_site_terms)
    matched_regions = _matched_config_terms(semantic_text, expected_region_terms)
    matched_events = tuple(
        term for term in EVENT_TERMS if _contains_term(semantic_text, term)
    )
    site_match = bool(matched_sites)
    region_match = bool(matched_regions)
    event_match = bool(matched_events)
    geographic_match = site_match or region_match
    if site_match and region_match and event_match:
        relevance_status = "relevant"
    elif geographic_match and event_match:
        relevance_status = "potentially_relevant"
    else:
        relevance_status = "not_relevant"
    return DocumentClassification(
        geographic_match=geographic_match,
        site_match=site_match,
        event_match=event_match,
        relevance_status=relevance_status,
        matched_terms=_unique((*matched_sites, *matched_regions, *matched_events)),
    )


def semantic_text(value: str) -> str:
    """Expose the shared accent-insensitive form used by review rules."""
    return _semantic_text(value)


def contains_term(value: str, term: str) -> bool:
    """Match a complete normalized term in already normalized text."""
    return _contains_term(value, term)


def _matched_config_terms(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in terms if _contains_term(text, term))


def _contains_term(text: str, term: str) -> bool:
    normalized_term = _semantic_text(term)
    if not normalized_term:
        return False
    return re.search(rf"(?<!\w){re.escape(normalized_term)}(?!\w)", text) is not None


def _semantic_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    unaccented = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", unaccented).strip()


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))
