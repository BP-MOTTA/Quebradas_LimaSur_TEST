"""Scientific quality rules for review-only documentary event candidates."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.document_classification import semantic_text

MAX_CONFIG_BYTES = 65_536
MAX_SNIPPET_CHARACTERS = 360
MAX_TERMS = 50
MAX_DATED_EVENT_DISTANCE = 180
VALIDATION_STATUS = "pending_review"
_STRENGTH_RANK = {"weak": 0, "moderate": 1, "strong": 2}
_DATE_PATTERN = re.compile(
    r"\b(?P<day>\d{1,2})\s+de\s+"
    r"(?P<month>enero|febrero|marzo|abril|mayo|junio|julio|agosto|"
    r"septiembre|setiembre|octubre|noviembre|diciembre)\s+"
    r"(?:de(?:l)?\s+)?(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
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
_TIME_PATTERN = re.compile(
    r"\b(?:a\s+las?\s+)?(?P<hour>\d{1,2}):(?P<minute>\d{2})\s+horas?\b",
    re.IGNORECASE,
)
_EVENT_RULES = (
    (
        "activacion_quebrada",
        re.compile(
            r"\b(?:se\s+)?(?:activ[oó]|activaron|activaci[oó]n(?:\s+de)?)\s+"
            r"las?\s+quebradas?\b",
            re.IGNORECASE,
        ),
    ),
    ("flujo_detritos", re.compile(r"\bflujo\s+de\s+detritos\b", re.IGNORECASE)),
    ("huaico", re.compile(r"\b(?:huaico|huayco)s?\b", re.IGNORECASE)),
    (
        "erosion_fluvial",
        re.compile(r"\berosi[oó]n\s+fluvial\b", re.IGNORECASE),
    ),
    (
        "desborde_rio",
        re.compile(r"\bdesborde\s+del\s+r[ií]o\b", re.IGNORECASE),
    ),
    ("inundacion", re.compile(r"\binundaci[oó]n\b", re.IGNORECASE)),
    (
        "lluvias_intensas",
        re.compile(r"\blluvias?\s+intensas?\b", re.IGNORECASE),
    ),
)


class CandidateQualityError(ValueError):
    """Raised when candidate quality inputs violate their contract."""


@dataclass(frozen=True)
class QualityPolicy:
    canonical_site_id: str
    reported_site_terms: tuple[str, ...]
    required_location_terms: tuple[str, ...]
    excluded_context_terms: tuple[str, ...]
    non_event_zone_terms: tuple[str, ...]
    context_window_characters: int


@dataclass(frozen=True)
class OriginalCandidate:
    candidate_id: str
    source_document_id: str
    source_page: int
    event_date: date | None
    evidence_snippet: str
    matched_terms: tuple[str, ...]
    validation_status: str = VALIDATION_STATUS

    @classmethod
    def create(
        cls,
        *,
        candidate_id: object,
        source_document_id: object,
        source_page: object,
        event_date: object,
        evidence_snippet: object,
        matched_terms: object,
        validation_status: object,
    ) -> OriginalCandidate:
        clean_id = _text(candidate_id, "candidate_id", 120)
        if re.fullmatch(r"[a-z0-9-]+", clean_id) is None:
            raise CandidateQualityError("candidate_id has an invalid format")
        clean_document_id = _text(source_document_id, "source_document_id", 120)
        if re.fullmatch(r"[A-Z0-9_]+", clean_document_id) is None:
            raise CandidateQualityError("source_document_id has an invalid format")
        if (
            isinstance(source_page, bool)
            or not isinstance(source_page, int)
            or not 1 <= source_page <= 10_000
        ):
            raise CandidateQualityError("source_page must be a positive integer")
        parsed_date = _optional_date(event_date)
        if not isinstance(evidence_snippet, str):
            raise CandidateQualityError("evidence_snippet must be a string")
        clean_snippet = evidence_snippet.replace("\x00", "").strip()
        if not clean_snippet or len(clean_snippet) > MAX_SNIPPET_CHARACTERS:
            raise CandidateQualityError("evidence_snippet has an invalid length")
        clean_terms = _terms(matched_terms, "matched_terms")
        if validation_status != VALIDATION_STATUS:
            raise CandidateQualityError("candidate must remain pending_review")
        return cls(
            candidate_id=clean_id,
            source_document_id=clean_document_id,
            source_page=source_page,
            event_date=parsed_date,
            evidence_snippet=clean_snippet,
            matched_terms=clean_terms,
        )


@dataclass(frozen=True)
class AuditedCandidate:
    candidate_id: str
    source_document_id: str
    source_page: int
    event_date: date | None
    event_time: str | None
    reported_quebrada: str | None
    canonical_site_id: str | None
    event_type: str | None
    evidence_snippet: str
    matched_terms: tuple[str, ...]
    site_evidence: str | None
    event_evidence: str | None
    date_evidence: str | None
    candidate_strength: str
    review_reason: str
    validation_status: str = VALIDATION_STATUS

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "source_document_id": self.source_document_id,
            "source_page": self.source_page,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "event_time": self.event_time,
            "reported_quebrada": self.reported_quebrada,
            "canonical_site_id": self.canonical_site_id,
            "event_type": self.event_type,
            "evidence_snippet": self.evidence_snippet,
            "matched_terms": list(self.matched_terms),
            "site_evidence": self.site_evidence,
            "event_evidence": self.event_evidence,
            "date_evidence": self.date_evidence,
            "candidate_strength": self.candidate_strength,
            "review_reason": self.review_reason,
            "validation_status": self.validation_status,
        }


@dataclass(frozen=True)
class ConsolidatedEvent:
    event_cluster_id: str
    canonical_site_id: str | None
    event_date: date | None
    event_time: str | None
    event_type: str | None
    reported_quebrada: str | None
    candidate_strength: str
    supporting_candidates: tuple[str, ...]
    supporting_pages: tuple[int, ...]
    best_evidence_snippet: str
    supporting_snippets: tuple[str, ...]
    validation_status: str = VALIDATION_STATUS

    def to_dict(self) -> dict[str, object]:
        return {
            "event_cluster_id": self.event_cluster_id,
            "canonical_site_id": self.canonical_site_id,
            "event_date": self.event_date.isoformat() if self.event_date else None,
            "event_time": self.event_time,
            "event_type": self.event_type,
            "reported_quebrada": self.reported_quebrada,
            "candidate_strength": self.candidate_strength,
            "supporting_candidates": list(self.supporting_candidates),
            "supporting_pages": list(self.supporting_pages),
            "best_evidence_snippet": self.best_evidence_snippet,
            "validation_status": self.validation_status,
        }


def load_quality_policy(path: Path) -> QualityPolicy:
    """Load strict JSON-compatible YAML for one canonical target site."""
    config_path = Path(path)
    if not config_path.is_file() or config_path.stat().st_size > MAX_CONFIG_BYTES:
        raise CandidateQualityError("quality config is missing or too large")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateQualityError("quality config must be UTF-8 JSON") from exc
    expected = {
        "canonical_site_id",
        "reported_site_terms",
        "required_location_terms",
        "excluded_context_terms",
        "non_event_zone_terms",
        "context_window_characters",
    }
    if not isinstance(raw, dict) or set(raw) != expected:
        raise CandidateQualityError("quality config keys do not match schema")
    canonical_site_id = _text(raw["canonical_site_id"], "canonical_site_id", 120)
    if re.fullmatch(r"[a-z0-9_]+", canonical_site_id) is None:
        raise CandidateQualityError("canonical_site_id has an invalid format")
    window = raw["context_window_characters"]
    if (
        isinstance(window, bool)
        or not isinstance(window, int)
        or not 80 <= window <= 360
    ):
        raise CandidateQualityError("context_window_characters is out of range")
    return QualityPolicy(
        canonical_site_id=canonical_site_id,
        reported_site_terms=_terms(raw["reported_site_terms"], "reported_site_terms"),
        required_location_terms=_terms(
            raw["required_location_terms"], "required_location_terms"
        ),
        excluded_context_terms=_terms(
            raw["excluded_context_terms"], "excluded_context_terms"
        ),
        non_event_zone_terms=_terms(
            raw["non_event_zone_terms"], "non_event_zone_terms"
        ),
        context_window_characters=window,
    )


def audit_candidate(
    candidate: OriginalCandidate,
    *,
    policy: QualityPolicy,
) -> AuditedCandidate | None:
    """Audit one immutable candidate using proximity and exclusion evidence."""
    text = candidate.evidence_snippet
    site_match = _nearest_term(text, policy.reported_site_terms)
    if site_match and _nearest_term(text, policy.excluded_context_terms):
        return None
    date_match = _matching_date(text, candidate.event_date)
    event_type, event_match = _nearest_event(text, site_match, date_match)
    location_match = _nearest_term(text, policy.required_location_terms)
    zone_reason = _non_event_zone(text, policy.non_event_zone_terms)

    reported_quebrada = (
        site_match.group(0)
        if site_match is not None
        else _reported_quebrada(text, event_type, event_match)
    )
    strength, reason = _candidate_strength(
        text,
        site_match=site_match,
        event_match=event_match,
        date_match=date_match,
        location_match=location_match,
        zone_reason=zone_reason,
        window=policy.context_window_characters,
    )
    return AuditedCandidate(
        candidate_id=candidate.candidate_id,
        source_document_id=candidate.source_document_id,
        source_page=candidate.source_page,
        event_date=candidate.event_date,
        event_time=_event_time(
            text,
            date_match=date_match,
            event_match=event_match,
        ),
        reported_quebrada=reported_quebrada,
        canonical_site_id=(policy.canonical_site_id if site_match else None),
        event_type=event_type,
        evidence_snippet=text,
        matched_terms=candidate.matched_terms,
        site_evidence=site_match.group(0) if site_match else None,
        event_evidence=event_match.group(0) if event_match else None,
        date_evidence=date_match.group(0) if date_match else None,
        candidate_strength=strength,
        review_reason=reason,
    )


def consolidate_candidates(
    candidates: tuple[AuditedCandidate, ...],
) -> tuple[ConsolidatedEvent, ...]:
    """Group compatible evidence while retaining every supporting fragment."""
    groups: dict[tuple[str, ...], list[AuditedCandidate]] = {}
    for candidate in candidates:
        key = _cluster_key(candidate)
        groups.setdefault(key, []).append(candidate)

    clusters = []
    for key, members in groups.items():
        best = min(
            members,
            key=lambda item: (
                -_STRENGTH_RANK[item.candidate_strength],
                len(item.evidence_snippet),
                item.candidate_id,
            ),
        )
        times = {item.event_time for item in members if item.event_time}
        cluster_digest = sha256("\x00".join(key).encode()).hexdigest()[:20]
        clusters.append(
            ConsolidatedEvent(
                event_cluster_id=f"indeci-event-{cluster_digest}",
                canonical_site_id=best.canonical_site_id,
                event_date=best.event_date,
                event_time=next(iter(times)) if len(times) == 1 else None,
                event_type=best.event_type,
                reported_quebrada=best.reported_quebrada,
                candidate_strength=best.candidate_strength,
                supporting_candidates=tuple(item.candidate_id for item in members),
                supporting_pages=tuple(
                    sorted({item.source_page for item in members})
                ),
                best_evidence_snippet=best.evidence_snippet,
                supporting_snippets=tuple(
                    item.evidence_snippet for item in members
                ),
            )
        )
    return tuple(clusters)


def _candidate_strength(
    text: str,
    *,
    site_match: re.Match[str] | None,
    event_match: re.Match[str] | None,
    date_match: re.Match[str] | None,
    location_match: re.Match[str] | None,
    zone_reason: str | None,
    window: int,
) -> tuple[str, str]:
    if zone_reason is not None:
        return "weak", f"non_event_zone:{zone_reason}"
    if site_match is None:
        return "weak", "target_site_not_found"
    if event_match is None:
        return "weak", "explicit_event_not_found"
    if _same_sentence(text, site_match, event_match):
        complete = (
            date_match is not None
            and location_match is not None
            and _same_sentence(text, site_match, date_match)
            and _same_sentence(text, site_match, location_match)
        )
        if complete:
            return "strong", "site_event_location_date_same_sentence"
        return "moderate", "site_event_same_sentence_context_incomplete"
    if _same_paragraph(text, site_match, event_match):
        return "moderate", "site_event_same_paragraph"
    if _span_distance(site_match, event_match) <= window:
        return "weak", "site_event_window_only"
    return "weak", "site_event_not_proximate"


def _cluster_key(candidate: AuditedCandidate) -> tuple[str, ...]:
    if (
        candidate.reported_quebrada
        and candidate.event_date
        and candidate.event_type
    ):
        return (
            "compatible",
            candidate.source_document_id,
            semantic_text(candidate.reported_quebrada),
            candidate.event_date.isoformat(),
            candidate.event_type,
        )
    return ("candidate", candidate.candidate_id)


def _nearest_event(
    text: str,
    site_match: re.Match[str] | None,
    date_match: re.Match[str] | None,
) -> tuple[str | None, re.Match[str] | None]:
    matches = [
        (priority, event_type, match)
        for priority, (event_type, pattern) in enumerate(_EVENT_RULES)
        for match in pattern.finditer(text)
    ]
    if not matches:
        return None, None
    if site_match is not None:
        _, event_type, match = min(
            matches,
            key=lambda item: _span_distance(site_match, item[2]),
        )
        return event_type, match
    if date_match is not None:
        following = [
            item
            for item in matches
            if item[2].start() >= date_match.end()
            and _span_distance(date_match, item[2]) <= MAX_DATED_EVENT_DISTANCE
            and _same_sentence(text, date_match, item[2])
        ]
        if following:
            _, event_type, match = min(
                following,
                key=lambda item: (item[0], _span_distance(date_match, item[2])),
            )
            return event_type, match
        _, event_type, match = min(
            matches,
            key=lambda item: _span_distance(date_match, item[2]),
        )
        return event_type, match
    _, event_type, match = matches[0]
    return event_type, match


def _matching_date(text: str, expected: date | None) -> re.Match[str] | None:
    if expected is None:
        return None
    for match in _DATE_PATTERN.finditer(text):
        try:
            parsed = date(
                int(match.group("year")),
                _MONTHS[semantic_text(match.group("month"))],
                int(match.group("day")),
            )
        except (KeyError, ValueError):
            continue
        if parsed == expected:
            return match
    return None


def _nearest_term(text: str, terms: tuple[str, ...]) -> re.Match[str] | None:
    matches = [
        match
        for term in terms
        if (match := re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.I))
    ]
    return min(matches, key=lambda item: item.start()) if matches else None


def _non_event_zone(text: str, terms: tuple[str, ...]) -> str | None:
    configured_match = _nearest_term(text, terms)
    if configured_match is not None:
        return semantic_text(configured_match.group(0))
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return None
    uppercase_ratio = sum(character.isupper() for character in letters) / len(letters)
    normalized = semantic_text(text)
    header_markers = ("informe", "coen", "indeci")
    if uppercase_ratio >= 0.9 and any(
        marker in normalized.split() for marker in header_markers
    ):
        return "repeated_header_or_footer"
    return None


def _reported_quebrada(
    text: str,
    event_type: str | None,
    event_match: re.Match[str] | None,
) -> str | None:
    if event_type != "activacion_quebrada" or event_match is None:
        return None
    tail = text[event_match.end() : event_match.end() + 160]
    boundary = re.search(
        r"\s+en\s+|,\s*(?:adem[aá]s|afect|gener|ocasion|provoc)|[.;]",
        tail,
        re.IGNORECASE,
    )
    value = tail[: boundary.start()] if boundary else tail
    value = value.strip(" :-,\n\t")
    value = re.sub(r"^de\s+", "", value, flags=re.IGNORECASE)
    return value[:120] or None


def _event_time(
    text: str,
    *,
    date_match: re.Match[str] | None,
    event_match: re.Match[str] | None,
) -> str | None:
    matches = list(_TIME_PATTERN.finditer(text))
    if not matches:
        return None
    if date_match is not None:
        matches = [
            item
            for item in matches
            if item.start() >= date_match.end()
            and _span_distance(date_match, item) <= 100
            and _same_sentence(text, date_match, item)
        ]
        anchor = date_match
    elif event_match is not None:
        matches = [
            item
            for item in matches
            if _span_distance(event_match, item) <= 100
            and _same_sentence(text, event_match, item)
        ]
        anchor = event_match
    else:
        return None
    if not matches:
        return None
    match = min(matches, key=lambda item: _span_distance(anchor, item))
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _same_sentence(text: str, left: re.Match[str], right: re.Match[str]) -> bool:
    return _same_segment(text, left, right, re.compile(r"[.!?;\n]+"))


def _same_paragraph(text: str, left: re.Match[str], right: re.Match[str]) -> bool:
    return _same_segment(text, left, right, re.compile(r"\n\s*\n+"))


def _same_segment(
    text: str,
    left: re.Match[str],
    right: re.Match[str],
    separator: re.Pattern[str],
) -> bool:
    for start, end in _segments(text, separator):
        if start <= left.start() < end and start <= right.start() < end:
            return True
    return False


def _segments(text: str, separator: re.Pattern[str]) -> tuple[tuple[int, int], ...]:
    spans = []
    start = 0
    for match in separator.finditer(text):
        spans.append((start, match.start()))
        start = match.end()
    spans.append((start, len(text)))
    return tuple(spans)


def _span_distance(left: re.Match[str], right: re.Match[str]) -> int:
    if left.end() < right.start():
        return right.start() - left.end()
    if right.end() < left.start():
        return left.start() - right.end()
    return 0


def _optional_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise CandidateQualityError("event_date must be an ISO date or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CandidateQualityError("event_date must be an ISO date or null") from exc


def _terms(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not 1 <= len(value) <= MAX_TERMS:
        raise CandidateQualityError(f"{field} has an invalid number of terms")
    terms = tuple(_text(item, field, 120) for item in value)
    if len({semantic_text(term) for term in terms}) != len(terms):
        raise CandidateQualityError(f"{field} contains duplicate terms")
    return terms


def _text(value: object, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise CandidateQualityError(f"{field} must be a string")
    clean = " ".join(value.replace("\x00", "").split())
    if not clean or len(clean) > max_length:
        raise CandidateQualityError(f"{field} has an invalid length")
    return clean
