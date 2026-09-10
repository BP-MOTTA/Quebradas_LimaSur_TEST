"""Offline, document-first workflow for controlled INDECI human review."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from quebradas_limaeste.inventory.batch_ingestion import (
    BATCH_DOCUMENTS_OUTPUT,
    BATCH_EVENT_CANDIDATES_OUTPUT,
    BATCH_EVENT_CLUSTERS_OUTPUT,
)
from quebradas_limaeste.inventory.human_review_models import (
    DOCUMENT_REVIEW_FIELDS,
    DOCUMENT_REVIEW_OUTPUT,
    EVENT_DATE_REVIEWS,
    EVENT_REVIEWS,
    HUMAN_REVIEW_FIELDS,
    HUMAN_REVIEW_OUTPUT,
    INVENTORY_DECISIONS,
    SITE_REVIEWS,
    SPATIAL_PRECISIONS,
    AutomaticCandidate,
    DocumentDecision,
    EventDecision,
    HumanReviewError,
    ReviewBatch,
    ReviewCluster,
    ReviewDocument,
    ReviewProtectionError,
    ReviewState,
    ReviewSummary,
)
from quebradas_limaeste.inventory.human_review_store import (
    load_review_batch,
    load_review_state,
    refresh_review_staleness,
    save_review_decisions,
    validate_reviewer,
)
from quebradas_limaeste.inventory.triage import BATCH_SELECTION_OUTPUT

__all__ = [
    "DOCUMENT_REVIEW_FIELDS",
    "HUMAN_REVIEW_FIELDS",
    "AutomaticCandidate",
    "DocumentDecision",
    "EventDecision",
    "HumanReviewError",
    "ReviewBatch",
    "ReviewCluster",
    "ReviewDocument",
    "ReviewProtectionError",
    "ReviewState",
    "ReviewSummary",
    "execute_review_batch",
    "execute_review_summary",
    "format_review_summary",
    "load_review_batch",
    "load_review_state",
    "pending_review_documents",
    "refresh_review_staleness",
    "render_review_packet",
    "save_review_decisions",
    "summarize_review",
]


def pending_review_documents(
    batch: ReviewBatch, state: ReviewState
) -> tuple[ReviewDocument, ...]:
    """Return resumable work, excluding controls already validated by design."""
    pending = []
    for document in batch.documents:
        if document.golden_control:
            continue
        missing_document = document.document_id not in state.document_rows
        missing_event = any(
            cluster.event_cluster_id not in state.event_rows
            for cluster in document.clusters
        )
        if missing_document or missing_event:
            pending.append(document)
    return tuple(pending)


def summarize_review(batch: ReviewBatch, state: ReviewState) -> ReviewSummary:
    """Aggregate current non-control review work without inferring decisions."""
    review_documents = [item for item in batch.documents if not item.golden_control]
    document_status = {
        label: {"reviewed": 0, "pending": 0}
        for label in ("relevant", "possible", "irrelevant")
    }
    relevance_labels = {
        "relevant": "relevant",
        "potentially_relevant": "possible",
        "not_relevant": "irrelevant",
    }
    inventory_status = {value: 0 for value in INVENTORY_DECISIONS}
    spatial_precision = {value: 0 for value in SPATIAL_PRECISIONS}
    event_status = {value: 0 for value in (*EVENT_REVIEWS, "pending")}
    stale_ids: set[tuple[str, str]] = set()

    for document in review_documents:
        row = state.document_rows.get(document.document_id)
        label = relevance_labels[document.relevance_status]
        review_status = "reviewed" if row is not None else "pending"
        document_status[label][review_status] += 1
        inventory = row["human_inventory_decision"] if row else "pending"
        precision = row["human_spatial_precision"] if row else "unknown"
        inventory_status[inventory] += 1
        spatial_precision[precision] += 1
        if row and row["review_stale"] == "true":
            stale_ids.add(("document", document.document_id))
        for cluster in document.clusters:
            event_row = state.event_rows.get(cluster.event_cluster_id)
            event_status[
                event_row["human_event_review"] if event_row else "pending"
            ] += 1
            if event_row and event_row["review_stale"] == "true":
                stale_ids.add(("event", cluster.event_cluster_id))

    return ReviewSummary(
        documents_total=len(batch.documents),
        documents_requiring_review=len(review_documents),
        document_status=document_status,
        event_status=event_status,
        inventory_status=inventory_status,
        spatial_precision=spatial_precision,
        candidates_grouped=sum(len(item.candidates) for item in batch.documents),
        golden_controls=sum(item.golden_control for item in batch.documents),
        stale_reviews=len(stale_ids),
    )


def format_review_summary(summary: ReviewSummary) -> str:
    """Render stable, human-readable offline review metrics."""
    lines = [
        "DOCUMENTOS",
        "----------",
        f"total: {summary.documents_total}",
        f"requiring_review: {summary.documents_requiring_review}",
    ]
    for label in ("relevant", "possible", "irrelevant"):
        counts = summary.document_status[label]
        lines.extend(
            (
                f"{label}:",
                f"  reviewed: {counts['reviewed']}",
                f"  pending: {counts['pending']}",
            )
        )
    lines.extend(("", "EVENTOS"))
    lines.extend(
        f"{key}: {summary.event_status[key]}"
        for key in (*EVENT_REVIEWS, "pending")
    )
    lines.extend(("", "INVENTARIO"))
    lines.extend(
        f"{key}: {summary.inventory_status[key]}" for key in INVENTORY_DECISIONS
    )
    lines.extend(("", "PRECISION ESPACIAL"))
    lines.extend(
        f"{key}: {summary.spatial_precision[key]}" for key in SPATIAL_PRECISIONS
    )
    lines.extend(
        (
            "",
            f"candidates_grouped: {summary.candidates_grouped}",
            f"golden_controls: {summary.golden_controls}",
            f"stale_reviews: {summary.stale_reviews}",
        )
    )
    return "\n".join(lines)


def render_review_packet(document: ReviewDocument, state: ReviewState) -> str:
    """Render bounded CSV evidence only; never open or display full PDF text."""
    decision = state.document_rows.get(document.document_id)
    lines = [
        "DOCUMENT",
        "--------",
        f"ID: {_display_text(document.document_id)}",
        f"report_number: {_display_text(document.report_number)}",
        f"report_type: {_display_text(document.report_type)}",
        f"report_date: {_display_text(document.report_date) or 'unknown'}",
        f"year: {document.year}",
        f"title: {_display_text(document.title)}",
        f"automatic classification: {document.relevance_status}",
        "matched_terms: "
        f"{', '.join(map(_display_text, document.matched_terms)) or 'none'}",
        f"page_count: {document.page_count}",
        f"golden_control: {str(document.golden_control).lower()}",
        "",
        "EVIDENCE",
        "--------",
    ]
    evidence_seen: set[tuple[int, str]] = set()
    for candidate in document.candidates:
        evidence = _display_text(candidate.evidence_snippet)
        item = (candidate.source_page, evidence)
        if item not in evidence_seen:
            lines.extend((f"page {candidate.source_page}:", f'"{evidence}"'))
            evidence_seen.add(item)
    if not evidence_seen:
        lines.append("none")
    lines.extend(("", "AUTO CANDIDATES", "---------------"))
    for cluster in document.clusters:
        lines.append(f"cluster: {_display_text(cluster.event_cluster_id)}")
        for candidate in cluster.candidates:
            lines.extend(_candidate_lines(candidate))
    if not document.clusters:
        lines.append("none")
    lines.extend(("", "HUMAN DECISION", "--------------"))
    if decision is None:
        lines.append("pending")
    else:
        lines.extend(
            (
                f"site_review: {decision['human_site_review']}",
                f"inventory_decision: {decision['human_inventory_decision']}",
                f"spatial_precision: {decision['human_spatial_precision']}",
                f"review_stale: {decision['review_stale']}",
            )
        )
    return "\n".join(lines)


def execute_review_batch(
    *,
    documents_path: Path = BATCH_DOCUMENTS_OUTPUT,
    selection_path: Path = BATCH_SELECTION_OUTPUT,
    candidates_path: Path = BATCH_EVENT_CANDIDATES_OUTPUT,
    clusters_path: Path = BATCH_EVENT_CLUSTERS_OUTPUT,
    document_review_path: Path = DOCUMENT_REVIEW_OUTPUT,
    human_review_path: Path = HUMAN_REVIEW_OUTPUT,
    allowed_root: Path,
    summary_only: bool = False,
    reviewer: str | None = None,
    input_func: Callable[[str], str] = input,
    output_func: Callable[[str], None] = print,
) -> dict[str, object]:
    """Print review packets or run a resumable, document-first review session."""
    batch = load_review_batch(
        documents_path=documents_path,
        selection_path=selection_path,
        candidates_path=candidates_path,
        clusters_path=clusters_path,
        allowed_root=allowed_root,
    )
    state = load_review_state(
        batch,
        document_review_path=document_review_path,
        human_review_path=human_review_path,
        allowed_root=allowed_root,
    )
    if summary_only:
        for document in batch.documents:
            if document.relevance_status != "not_relevant":
                output_func(render_review_packet(document, state))
        summary = summarize_review(batch, state)
        output_func(format_review_summary(summary))
        return summary.to_dict()

    refresh_review_staleness(
        batch,
        document_review_path=document_review_path,
        human_review_path=human_review_path,
        allowed_root=allowed_root,
    )
    state = load_review_state(
        batch,
        document_review_path=document_review_path,
        human_review_path=human_review_path,
        allowed_root=allowed_root,
    )
    reviewed_by = validate_reviewer(reviewer or input_func("reviewed_by: "))
    try:
        for document in pending_review_documents(batch, state):
            output_func(render_review_packet(document, state))
            action = _prompt_choice(
                "action", ("review", "skip", "quit"), input_func=input_func
            )
            if action == "quit":
                break
            if action == "skip":
                continue
            document_decision = (
                None
                if document.document_id in state.document_rows
                else _prompt_document_decision(input_func=input_func)
            )
            event_decisions = {
                cluster.event_cluster_id: _prompt_event_decision(
                    cluster, input_func=input_func, output_func=output_func
                )
                for cluster in document.clusters
                if cluster.event_cluster_id not in state.event_rows
            }
            save_review_decisions(
                batch,
                document_id=document.document_id,
                document_decision=document_decision,
                event_decisions=event_decisions,
                reviewer=reviewed_by,
                document_review_path=document_review_path,
                human_review_path=human_review_path,
                allowed_root=allowed_root,
            )
            state = load_review_state(
                batch,
                document_review_path=document_review_path,
                human_review_path=human_review_path,
                allowed_root=allowed_root,
            )
    except (EOFError, KeyboardInterrupt):
        output_func("Review interrupted; completed documents remain saved.")
    summary = summarize_review(batch, state)
    output_func(format_review_summary(summary))
    return summary.to_dict()


def execute_review_summary(
    *,
    documents_path: Path = BATCH_DOCUMENTS_OUTPUT,
    selection_path: Path = BATCH_SELECTION_OUTPUT,
    candidates_path: Path = BATCH_EVENT_CANDIDATES_OUTPUT,
    clusters_path: Path = BATCH_EVENT_CLUSTERS_OUTPUT,
    document_review_path: Path = DOCUMENT_REVIEW_OUTPUT,
    human_review_path: Path = HUMAN_REVIEW_OUTPUT,
    allowed_root: Path,
    output_func: Callable[[str], None] = print,
) -> dict[str, object]:
    """Print aggregate local review state without creating or changing files."""
    batch = load_review_batch(
        documents_path=documents_path,
        selection_path=selection_path,
        candidates_path=candidates_path,
        clusters_path=clusters_path,
        allowed_root=allowed_root,
    )
    state = load_review_state(
        batch,
        document_review_path=document_review_path,
        human_review_path=human_review_path,
        allowed_root=allowed_root,
    )
    summary = summarize_review(batch, state)
    output_func(format_review_summary(summary))
    return summary.to_dict()


def _candidate_lines(candidate: AutomaticCandidate) -> tuple[str, ...]:
    return (
        f"  candidate_id: {_display_text(candidate.candidate_id)}",
        f"  source_page: {candidate.source_page}",
        f"  event_date: {_display_text(candidate.event_date) or 'missing'}",
        f"  event_type: {_display_text(candidate.event_type) or 'unknown'}",
        "  reported_quebrada: "
        f"{_display_text(candidate.reported_quebrada) or 'unknown'}",
        f"  candidate_strength: {candidate.candidate_strength}",
        f"  evidence_snippet: {_display_text(candidate.evidence_snippet)}",
    )


def _prompt_document_decision(
    *, input_func: Callable[[str], str]
) -> DocumentDecision:
    return DocumentDecision.create(
        human_site_review=_prompt_choice(
            "site_review", SITE_REVIEWS, input_func=input_func
        ),
        human_inventory_decision=_prompt_choice(
            "inventory_decision", INVENTORY_DECISIONS, input_func=input_func
        ),
        human_spatial_precision=_prompt_choice(
            "spatial_precision", SPATIAL_PRECISIONS, input_func=input_func
        ),
        human_notes=input_func("review_notes: "),
    )


def _prompt_event_decision(
    cluster: ReviewCluster,
    *,
    input_func: Callable[[str], str],
    output_func: Callable[[str], None],
) -> EventDecision:
    output_func(f"Event cluster: {_display_text(cluster.event_cluster_id)}")
    return EventDecision.create(
        human_site_review=_prompt_choice(
            "site_review", SITE_REVIEWS, input_func=input_func
        ),
        human_event_review=_prompt_choice(
            "event_review", EVENT_REVIEWS, input_func=input_func
        ),
        human_event_date=_prompt_choice(
            "event_date_review", EVENT_DATE_REVIEWS, input_func=input_func
        ),
        human_inventory_decision=_prompt_choice(
            "inventory_decision", INVENTORY_DECISIONS, input_func=input_func
        ),
        human_spatial_precision=_prompt_choice(
            "spatial_precision", SPATIAL_PRECISIONS, input_func=input_func
        ),
        human_notes=input_func("review_notes: "),
    )


def _prompt_choice(
    label: str,
    choices: tuple[str, ...],
    *,
    input_func: Callable[[str], str],
) -> str:
    prompt = f"{label} [{'/'.join(choices)}]: "
    while True:
        value = input_func(prompt).strip()
        if value in choices:
            return value


def _display_text(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
