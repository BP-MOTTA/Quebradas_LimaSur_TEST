"""Models and closed vocabularies for INDECI human review."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DOCUMENT_REVIEW_OUTPUT = Path("metadata/indeci/document_review.csv")
HUMAN_REVIEW_OUTPUT = Path("metadata/indeci/human_review.csv")
MAX_NOTES_LENGTH = 2_000

DOCUMENT_REVIEW_FIELDS = (
    "batch_id",
    "document_id",
    "report_number",
    "report_type",
    "report_date",
    "year",
    "title",
    "relevance_status",
    "matched_terms",
    "page_count",
    "golden_control",
    "automatic_fingerprint",
    "human_site_review",
    "human_inventory_decision",
    "human_spatial_precision",
    "human_notes",
    "reviewed_at",
    "reviewed_by",
    "review_stale",
)
HUMAN_REVIEW_FIELDS = (
    "batch_id",
    "event_cluster_id",
    "document_id",
    "canonical_site_id",
    "event_date_extracted",
    "event_type",
    "reported_quebrada",
    "candidate_strength",
    "supporting_candidates",
    "supporting_pages",
    "best_evidence_snippet",
    "automatic_fingerprint",
    "human_site_review",
    "human_event_review",
    "human_event_date",
    "human_inventory_decision",
    "human_spatial_precision",
    "human_notes",
    "reviewed_at",
    "reviewed_by",
    "review_stale",
)

SITE_REVIEWS = ("confirmed", "rejected", "ambiguous")
EVENT_REVIEWS = ("confirmed", "rejected", "ambiguous")
EVENT_DATE_REVIEWS = ("exact", "approximate", "missing", "not_applicable")
INVENTORY_DECISIONS = ("include", "exclude", "pending")
SPATIAL_PRECISIONS = ("A", "B", "C", "D", "E", "unknown")
RELEVANCE_STATUSES = ("relevant", "potentially_relevant", "not_relevant")
CANDIDATE_STRENGTHS = ("strong", "moderate", "weak")
GOLDEN_CONTROL_ROLES = ("positive_control", "negative_ambiguous_control")
REPORT_TYPES = (
    "reporte_complementario",
    "reporte_preliminar",
    "informe_emergencia",
)

STRENGTH_RANK = {
    value: index for index, value in enumerate(CANDIDATE_STRENGTHS)
}


class HumanReviewError(RuntimeError):
    """Raised when automatic inputs or review state violate the workflow contract."""


class ReviewProtectionError(HumanReviewError):
    """Raised when an operation could alter an existing human decision."""


@dataclass(frozen=True)
class AutomaticCandidate:
    candidate_id: str
    document_id: str
    source_page: int
    event_date: str
    event_type: str
    reported_quebrada: str
    canonical_site_id: str
    candidate_strength: str
    evidence_snippet: str
    automatic_fingerprint: str


@dataclass(frozen=True)
class ReviewCluster:
    event_cluster_id: str
    document_id: str
    canonical_site_id: str
    event_date_extracted: str
    event_type: str
    reported_quebrada: str
    candidate_strength: str
    supporting_candidates: tuple[str, ...]
    supporting_pages: tuple[int, ...]
    best_evidence_snippet: str
    candidates: tuple[AutomaticCandidate, ...]
    automatic_fingerprint: str


@dataclass(frozen=True)
class ReviewDocument:
    batch_id: str
    document_id: str
    report_number: str
    report_type: str
    report_date: str
    year: int
    title: str
    relevance_status: str
    matched_terms: tuple[str, ...]
    page_count: int
    golden_control: bool
    golden_control_role: str
    clusters: tuple[ReviewCluster, ...]
    automatic_fingerprint: str

    @property
    def candidates(self) -> tuple[AutomaticCandidate, ...]:
        """Return all automatic candidates in scientific review order."""
        candidates = [item for cluster in self.clusters for item in cluster.candidates]
        return tuple(sorted(candidates, key=candidate_sort_key))


@dataclass(frozen=True)
class ReviewBatch:
    batch_id: str
    documents: tuple[ReviewDocument, ...]


@dataclass(frozen=True)
class ReviewState:
    document_rows: dict[str, dict[str, str]]
    event_rows: dict[str, dict[str, str]]


@dataclass(frozen=True)
class DocumentDecision:
    human_site_review: str
    human_inventory_decision: str
    human_spatial_precision: str
    human_notes: str

    @classmethod
    def create(
        cls,
        *,
        human_site_review: str,
        human_inventory_decision: str,
        human_spatial_precision: str,
        human_notes: str,
    ) -> DocumentDecision:
        return cls(
            human_site_review=validate_choice(
                human_site_review, "human_site_review", SITE_REVIEWS
            ),
            human_inventory_decision=validate_choice(
                human_inventory_decision,
                "human_inventory_decision",
                INVENTORY_DECISIONS,
            ),
            human_spatial_precision=validate_choice(
                human_spatial_precision,
                "human_spatial_precision",
                SPATIAL_PRECISIONS,
            ),
            human_notes=normalize_notes(human_notes),
        )


@dataclass(frozen=True)
class EventDecision:
    human_site_review: str
    human_event_review: str
    human_event_date: str
    human_inventory_decision: str
    human_spatial_precision: str
    human_notes: str

    @classmethod
    def create(
        cls,
        *,
        human_site_review: str,
        human_event_review: str,
        human_event_date: str,
        human_inventory_decision: str,
        human_spatial_precision: str,
        human_notes: str,
    ) -> EventDecision:
        return cls(
            human_site_review=validate_choice(
                human_site_review, "human_site_review", SITE_REVIEWS
            ),
            human_event_review=validate_choice(
                human_event_review, "human_event_review", EVENT_REVIEWS
            ),
            human_event_date=validate_choice(
                human_event_date, "human_event_date", EVENT_DATE_REVIEWS
            ),
            human_inventory_decision=validate_choice(
                human_inventory_decision,
                "human_inventory_decision",
                INVENTORY_DECISIONS,
            ),
            human_spatial_precision=validate_choice(
                human_spatial_precision,
                "human_spatial_precision",
                SPATIAL_PRECISIONS,
            ),
            human_notes=normalize_notes(human_notes),
        )


@dataclass(frozen=True)
class ReviewSummary:
    documents_total: int
    documents_requiring_review: int
    document_status: dict[str, dict[str, int]]
    event_status: dict[str, int]
    inventory_status: dict[str, int]
    spatial_precision: dict[str, int]
    candidates_grouped: int
    golden_controls: int
    stale_reviews: int

    def to_dict(self) -> dict[str, object]:
        return {
            "documents_total": self.documents_total,
            "documents_requiring_review": self.documents_requiring_review,
            "document_status": self.document_status,
            "event_status": self.event_status,
            "inventory_status": self.inventory_status,
            "spatial_precision": self.spatial_precision,
            "candidates_grouped": self.candidates_grouped,
            "golden_controls": self.golden_controls,
            "stale_reviews": self.stale_reviews,
        }


def candidate_sort_key(candidate: AutomaticCandidate) -> tuple[int, int, str]:
    return (
        STRENGTH_RANK[candidate.candidate_strength],
        candidate.source_page,
        candidate.candidate_id,
    )


def validate_choice(value: str, field: str, choices: tuple[str, ...]) -> str:
    if value not in choices:
        raise HumanReviewError(f"{field} must be one of {', '.join(choices)}")
    return value


def normalize_notes(value: str) -> str:
    if not isinstance(value, str):
        raise HumanReviewError("human_notes must be text")
    clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value).strip()
    if len(clean) > MAX_NOTES_LENGTH:
        raise HumanReviewError("human_notes exceeds its length limit")
    return clean


def bounded_text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise HumanReviewError(f"{field} must be text")
    clean = re.sub(r"\s+", " ", value.replace("\x00", " ")).strip()
    if not clean or len(clean) > maximum:
        raise HumanReviewError(f"{field} has an invalid length")
    return clean
