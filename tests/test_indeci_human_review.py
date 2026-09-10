import csv
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.batch_ingestion import BATCH_DOCUMENT_FIELDS
from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_FIELDS,
    CONSOLIDATED_FIELDS,
)
from quebradas_limaeste.inventory.human_review import (
    DOCUMENT_REVIEW_FIELDS,
    HUMAN_REVIEW_FIELDS,
    DocumentDecision,
    EventDecision,
    ReviewProtectionError,
    execute_review_batch,
    format_review_summary,
    load_review_batch,
    load_review_state,
    pending_review_documents,
    refresh_review_staleness,
    save_review_decisions,
    summarize_review,
)
from quebradas_limaeste.inventory.triage import SELECTION_FIELDS

NOW = datetime(2026, 9, 10, 15, 30, tzinfo=UTC)
BATCH_ID = "indeci-batch-0123456789abcdef"
REL = "INDECI_RC1_20190101"
POSSIBLE = "INDECI_IE2_20230101"
IRRELEVANT = "INDECI_RP3_20170101"
GOLDEN = "INDECI_RC630_20190303"
CANDIDATE_REL = "indeci-candidate-00000000000000000001"
CANDIDATE_STRONG = "indeci-candidate-00000000000000000002"
CANDIDATE_WEAK = "indeci-candidate-00000000000000000003"
CLUSTER_REL = "indeci-event-00000000000000000001"
CLUSTER_STRONG = "indeci-event-00000000000000000002"
CLUSTER_WEAK = "indeci-event-00000000000000000003"


def write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def document_row(
    document_id: str,
    *,
    year: int,
    report_number: str,
    relevance: str,
    matched_terms: str,
) -> dict[str, str]:
    row = {field: "" for field in BATCH_DOCUMENT_FIELDS}
    row.update(
        {
            "batch_id": BATCH_ID,
            "document_id": document_id,
            "discovery_id": f"indeci-discovery-{report_number.zfill(20)}",
            "year": str(year),
            "report_number": report_number,
            "title": f"Documento sintetico {document_id}",
            "sha256": report_number.zfill(64),
            "page_count": "2",
            "download_status": "downloaded",
            "extraction_status": "success",
            "relevance_status": relevance,
            "site_match": "true",
            "geographic_match": "true",
            "event_match": "true",
            "matched_terms": matched_terms,
            "review_required": "true",
            "ocr_required": "false",
            "download_success": "true",
            "extraction_success": "true",
            "classification_success": "true",
            "event_extraction_success": "true",
        }
    )
    return row


def selection_row(
    document_id: str,
    *,
    year: int,
    report_type: str,
    report_number: str,
    golden: str = "",
) -> dict[str, str]:
    row = {field: "" for field in SELECTION_FIELDS}
    row.update(
        {
            "batch_id": BATCH_ID,
            "document_id": document_id,
            "discovery_id": f"indeci-discovery-{report_number.zfill(20)}",
            "year": str(year),
            "report_type": report_type,
            "report_number": report_number,
            "report_date": f"{year}-01-01",
            "title": f"Documento sintetico {document_id}",
            "source_connector": "archive_informes",
            "detail_url": "https://portal.indeci.gob.pe/item/",
            "pdf_url": (
                "https://portal.indeci.gob.pe/wp-content/uploads/"
                f"{year}/01/{document_id}.pdf"
            ),
            "triage_tier": "D",
            "triage_score": "0",
            "triage_reasons": '["synthetic_fixture"]',
            "golden_control": golden,
            "selected": "true",
            "selection_reason": "synthetic_fixture",
        }
    )
    return row


def candidate_row(
    candidate_id: str,
    document_id: str,
    *,
    strength: str,
    page: int,
    evidence: str,
) -> dict[str, str]:
    row = {field: "" for field in AUDIT_FIELDS}
    row.update(
        {
            "candidate_id": candidate_id,
            "source_document_id": document_id,
            "source_page": str(page),
            "event_date": "2019-02-25",
            "evidence_snippet": evidence,
            "matched_terms": '["Chaclacayo","huaico"]',
            "validation_status": "pending_review",
            "reported_quebrada": "Cusipata",
            "canonical_site_id": "quebrada_cusipata_chaclacayo",
            "event_type": "huaico",
            "site_evidence": "Cusipata",
            "event_evidence": "huaico",
            "date_evidence": "25 de febrero de 2019",
            "candidate_strength": strength,
            "review_reason": "synthetic_fixture",
        }
    )
    return row


def cluster_row(
    cluster_id: str,
    candidate_id: str,
    *,
    strength: str,
    page: int,
    evidence: str,
) -> dict[str, str]:
    row = {field: "" for field in CONSOLIDATED_FIELDS}
    row.update(
        {
            "event_cluster_id": cluster_id,
            "canonical_site_id": "quebrada_cusipata_chaclacayo",
            "event_date": "2019-02-25",
            "event_type": "huaico",
            "reported_quebrada": "Cusipata",
            "candidate_strength": strength,
            "supporting_candidates": candidate_id,
            "supporting_pages": str(page),
            "best_evidence_snippet": evidence,
            "validation_status": "pending_review",
        }
    )
    return row


def make_inputs(tmp_path: Path, *, changed_evidence: bool = False) -> dict[str, Path]:
    metadata = tmp_path / "metadata" / "indeci"
    paths = {
        "documents": metadata / "batch_documents.csv",
        "selection": metadata / "batch_selection.csv",
        "candidates": metadata / "batch_event_candidates.csv",
        "clusters": metadata / "batch_event_clusters.csv",
        "document_review": metadata / "document_review.csv",
        "human_review": metadata / "human_review.csv",
    }
    documents = [
        document_row(
            REL,
            year=2019,
            report_number="1",
            relevance="relevant",
            matched_terms='["Chaclacayo","huaico"]',
        ),
        document_row(
            POSSIBLE,
            year=2023,
            report_number="2",
            relevance="potentially_relevant",
            matched_terms='["Lima","lluvias intensas"]',
        ),
        document_row(
            IRRELEVANT,
            year=2017,
            report_number="3",
            relevance="not_relevant",
            matched_terms="[]",
        ),
        document_row(
            GOLDEN,
            year=2019,
            report_number="630",
            relevance="relevant",
            matched_terms='["Chaclacayo","huaico"]',
        ),
    ]
    selections = [
        selection_row(
            REL,
            year=2019,
            report_type="reporte_complementario",
            report_number="1",
        ),
        selection_row(
            POSSIBLE,
            year=2023,
            report_type="informe_emergencia",
            report_number="2",
        ),
        selection_row(
            IRRELEVANT,
            year=2017,
            report_type="reporte_preliminar",
            report_number="3",
        ),
        selection_row(
            GOLDEN,
            year=2019,
            report_type="reporte_complementario",
            report_number="630",
            golden="positive_control",
        ),
    ]
    changed = " Evidencia automatica actualizada." if changed_evidence else ""
    candidates = [
        candidate_row(
            CANDIDATE_REL,
            REL,
            strength="weak",
            page=2,
            evidence="El 25 de febrero se reporto un huaico." + changed,
        ),
        candidate_row(
            CANDIDATE_WEAK,
            POSSIBLE,
            strength="weak",
            page=7,
            evidence="Lluvias intensas en Lima.",
        ),
        candidate_row(
            CANDIDATE_STRONG,
            POSSIBLE,
            strength="strong",
            page=3,
            evidence="Activacion de Cusipata en Chaclacayo.",
        ),
    ]
    clusters = [
        cluster_row(
            CLUSTER_REL,
            CANDIDATE_REL,
            strength="weak",
            page=2,
            evidence="El 25 de febrero se reporto un huaico." + changed,
        ),
        cluster_row(
            CLUSTER_WEAK,
            CANDIDATE_WEAK,
            strength="weak",
            page=7,
            evidence="Lluvias intensas en Lima.",
        ),
        cluster_row(
            CLUSTER_STRONG,
            CANDIDATE_STRONG,
            strength="strong",
            page=3,
            evidence="Activacion de Cusipata en Chaclacayo.",
        ),
    ]
    write_csv(paths["documents"], BATCH_DOCUMENT_FIELDS, documents)
    write_csv(paths["selection"], SELECTION_FIELDS, selections)
    write_csv(paths["candidates"], AUDIT_FIELDS, candidates)
    write_csv(paths["clusters"], CONSOLIDATED_FIELDS, clusters)
    return paths


def load_batch(tmp_path: Path, paths: dict[str, Path]):
    return load_review_batch(
        documents_path=paths["documents"],
        selection_path=paths["selection"],
        candidates_path=paths["candidates"],
        clusters_path=paths["clusters"],
        allowed_root=tmp_path,
    )


def save(
    tmp_path: Path,
    paths: dict[str, Path],
    batch,
    document_id: str,
    document_decision: DocumentDecision,
    event_decisions: dict[str, EventDecision],
) -> None:
    save_review_decisions(
        batch,
        document_id=document_id,
        document_decision=document_decision,
        event_decisions=event_decisions,
        reviewer="researcher-1",
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
        now=lambda: NOW,
    )


def test_review_groups_candidates_by_document_and_orders_priority(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)

    assert [document.document_id for document in batch.documents] == [
        REL,
        GOLDEN,
        POSSIBLE,
        IRRELEVANT,
    ]
    possible = next(item for item in batch.documents if item.document_id == POSSIBLE)
    assert [item.candidate_strength for item in possible.candidates] == [
        "strong",
        "weak",
    ]
    assert len({item.document_id for item in batch.documents}) == 4
    golden = next(item for item in batch.documents if item.document_id == GOLDEN)
    assert golden.golden_control is True


def test_review_preserves_human_decision_and_marks_changed_evidence_stale(
    tmp_path,
) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)
    decision = DocumentDecision.create(
        human_site_review="confirmed",
        human_inventory_decision="include",
        human_spatial_precision="A",
        human_notes="Revision sintetica.",
    )
    event = EventDecision.create(
        human_site_review="confirmed",
        human_event_review="confirmed",
        human_event_date="exact",
        human_inventory_decision="include",
        human_spatial_precision="A",
        human_notes="Evento sintetico.",
    )
    save(tmp_path, paths, batch, REL, decision, {CLUSTER_REL: event})
    original_document = paths["document_review"].read_bytes()
    original_event = paths["human_review"].read_bytes()

    with pytest.raises(ReviewProtectionError, match="already reviewed"):
        save(tmp_path, paths, batch, REL, decision, {CLUSTER_REL: event})
    assert paths["document_review"].read_bytes() == original_document
    assert paths["human_review"].read_bytes() == original_event

    make_inputs(tmp_path, changed_evidence=True)
    changed_batch = load_batch(tmp_path, paths)
    refresh_review_staleness(
        changed_batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )
    state = load_review_state(
        changed_batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )

    assert state.document_rows[REL]["human_inventory_decision"] == "include"
    assert state.document_rows[REL]["human_notes"] == "Revision sintetica."
    assert state.document_rows[REL]["review_stale"] == "true"
    assert state.event_rows[CLUSTER_REL]["human_event_review"] == "confirmed"
    assert state.event_rows[CLUSTER_REL]["review_stale"] == "true"


def test_review_resumes_and_saves_ambiguous_and_spatial_precision(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)
    document_decision = DocumentDecision.create(
        human_site_review="ambiguous",
        human_inventory_decision="pending",
        human_spatial_precision="E",
        human_notes="Ubicacion por revisar.",
    )
    ambiguous = EventDecision.create(
        human_site_review="ambiguous",
        human_event_review="ambiguous",
        human_event_date="approximate",
        human_inventory_decision="pending",
        human_spatial_precision="E",
        human_notes="Fecha aproximada.",
    )
    rejected = EventDecision.create(
        human_site_review="rejected",
        human_event_review="rejected",
        human_event_date="not_applicable",
        human_inventory_decision="exclude",
        human_spatial_precision="unknown",
        human_notes="No corresponde.",
    )
    save(
        tmp_path,
        paths,
        batch,
        POSSIBLE,
        document_decision,
        {CLUSTER_STRONG: ambiguous, CLUSTER_WEAK: rejected},
    )
    state = load_review_state(
        batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )
    pending = pending_review_documents(batch, state)

    assert [item.document_id for item in pending] == [REL, IRRELEVANT]
    assert state.event_rows[CLUSTER_STRONG]["human_event_review"] == "ambiguous"
    assert state.event_rows[CLUSTER_STRONG]["human_spatial_precision"] == "E"


def test_review_summary_counts_documents_events_inventory_and_precision(
    tmp_path,
) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)
    save(
        tmp_path,
        paths,
        batch,
        REL,
        DocumentDecision.create(
            human_site_review="confirmed",
            human_inventory_decision="include",
            human_spatial_precision="A",
            human_notes="",
        ),
        {
            CLUSTER_REL: EventDecision.create(
                human_site_review="confirmed",
                human_event_review="confirmed",
                human_event_date="exact",
                human_inventory_decision="include",
                human_spatial_precision="A",
                human_notes="",
            )
        },
    )
    save(
        tmp_path,
        paths,
        batch,
        POSSIBLE,
        DocumentDecision.create(
            human_site_review="ambiguous",
            human_inventory_decision="pending",
            human_spatial_precision="E",
            human_notes="",
        ),
        {
            CLUSTER_STRONG: EventDecision.create(
                human_site_review="ambiguous",
                human_event_review="ambiguous",
                human_event_date="approximate",
                human_inventory_decision="pending",
                human_spatial_precision="E",
                human_notes="",
            ),
            CLUSTER_WEAK: EventDecision.create(
                human_site_review="rejected",
                human_event_review="rejected",
                human_event_date="not_applicable",
                human_inventory_decision="exclude",
                human_spatial_precision="unknown",
                human_notes="",
            ),
        },
    )
    state = load_review_state(
        batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )

    summary = summarize_review(batch, state)
    rendered = format_review_summary(summary)

    assert summary.documents_total == 4
    assert summary.documents_requiring_review == 3
    assert summary.document_status["relevant"] == {"reviewed": 1, "pending": 0}
    assert summary.document_status["possible"] == {"reviewed": 1, "pending": 0}
    assert summary.document_status["irrelevant"] == {"reviewed": 0, "pending": 1}
    assert summary.event_status == {
        "confirmed": 1,
        "rejected": 1,
        "ambiguous": 1,
        "pending": 0,
    }
    assert summary.inventory_status == {"include": 1, "exclude": 0, "pending": 2}
    assert summary.spatial_precision == {
        "A": 1,
        "B": 0,
        "C": 0,
        "D": 0,
        "E": 1,
        "unknown": 1,
    }
    assert summary.candidates_grouped == 3
    assert summary.golden_controls == 1
    assert "training_label" not in DOCUMENT_REVIEW_FIELDS
    assert "training_label" not in HUMAN_REVIEW_FIELDS
    assert "EVENTOS\nconfirmed: 1" in rendered


def test_review_batch_summary_is_read_only_and_prints_compact_packets(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    output = []

    payload = execute_review_batch(
        documents_path=paths["documents"],
        selection_path=paths["selection"],
        candidates_path=paths["candidates"],
        clusters_path=paths["clusters"],
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
        summary_only=True,
        output_func=output.append,
    )

    rendered = "\n".join(output)
    assert payload["documents_total"] == 4
    assert payload["documents_requiring_review"] == 3
    assert "DOCUMENT\n--------" in rendered
    assert "AUTO CANDIDATES\n---------------" in rendered
    assert CANDIDATE_STRONG in rendered
    assert not paths["document_review"].exists()
    assert not paths["human_review"].exists()


@pytest.mark.parametrize("precision", ["A", "B", "C", "D", "E", "unknown"])
def test_document_decision_accepts_all_spatial_precision_values(precision) -> None:
    decision = DocumentDecision.create(
        human_site_review="ambiguous",
        human_inventory_decision="pending",
        human_spatial_precision=precision,
        human_notes="",
    )

    assert decision.human_spatial_precision == precision


def test_review_csv_neutralizes_formula_notes_and_restores_human_text(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)
    formula_note = '=HYPERLINK("https://example.test","review")'
    save(
        tmp_path,
        paths,
        batch,
        REL,
        DocumentDecision.create(
            human_site_review="confirmed",
            human_inventory_decision="include",
            human_spatial_precision="A",
            human_notes=formula_note,
        ),
        {
            CLUSTER_REL: EventDecision.create(
                human_site_review="confirmed",
                human_event_review="confirmed",
                human_event_date="exact",
                human_inventory_decision="include",
                human_spatial_precision="A",
                human_notes=formula_note,
            )
        },
    )

    with paths["document_review"].open(newline="", encoding="utf-8") as source:
        raw_row = next(csv.DictReader(source))
    state = load_review_state(
        batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )

    assert raw_row["human_notes"].startswith("'=")
    assert state.document_rows[REL]["human_notes"] == formula_note


def test_review_rejects_malformed_automatic_candidate_id(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    with paths["candidates"].open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    rows[0]["candidate_id"] = "../../not-a-candidate"
    write_csv(paths["candidates"], AUDIT_FIELDS, rows)

    with pytest.raises(RuntimeError, match="candidate_id"):
        load_batch(tmp_path, paths)


def test_review_rejects_document_selection_metadata_mismatch(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    with paths["selection"].open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    rows[0]["title"] = "Otro documento"
    write_csv(paths["selection"], SELECTION_FIELDS, rows)

    with pytest.raises(RuntimeError, match="metadata conflicts"):
        load_batch(tmp_path, paths)


def test_review_rejects_cluster_evidence_conflicting_with_candidate(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    with paths["clusters"].open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    rows[0]["event_date"] = "2020-01-01"
    write_csv(paths["clusters"], CONSOLIDATED_FIELDS, rows)

    with pytest.raises(RuntimeError, match="conflicts with supporting candidates"):
        load_batch(tmp_path, paths)


def test_summary_mode_preserves_existing_review_files_byte_for_byte(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    batch = load_batch(tmp_path, paths)
    save(
        tmp_path,
        paths,
        batch,
        REL,
        DocumentDecision.create(
            human_site_review="confirmed",
            human_inventory_decision="include",
            human_spatial_precision="A",
            human_notes="",
        ),
        {
            CLUSTER_REL: EventDecision.create(
                human_site_review="confirmed",
                human_event_review="confirmed",
                human_event_date="exact",
                human_inventory_decision="include",
                human_spatial_precision="A",
                human_notes="",
            )
        },
    )
    before_document = paths["document_review"].read_bytes()
    before_event = paths["human_review"].read_bytes()

    execute_review_batch(
        documents_path=paths["documents"],
        selection_path=paths["selection"],
        candidates_path=paths["candidates"],
        clusters_path=paths["clusters"],
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
        summary_only=True,
        output_func=lambda _message: None,
    )

    assert paths["document_review"].read_bytes() == before_document
    assert paths["human_review"].read_bytes() == before_event


def test_interactive_review_saves_completed_document_before_quit(tmp_path) -> None:
    paths = make_inputs(tmp_path)
    answers = iter(
        (
            "reviewer-1",
            "review",
            "confirmed",
            "include",
            "A",
            "Documento revisado.",
            "confirmed",
            "confirmed",
            "exact",
            "include",
            "A",
            "Evento revisado.",
            "quit",
        )
    )

    payload = execute_review_batch(
        documents_path=paths["documents"],
        selection_path=paths["selection"],
        candidates_path=paths["candidates"],
        clusters_path=paths["clusters"],
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
        input_func=lambda _prompt: next(answers),
        output_func=lambda _message: None,
    )
    batch = load_batch(tmp_path, paths)
    state = load_review_state(
        batch,
        document_review_path=paths["document_review"],
        human_review_path=paths["human_review"],
        allowed_root=tmp_path,
    )

    assert payload["document_status"]["relevant"] == {
        "reviewed": 1,
        "pending": 0,
    }
    assert state.document_rows[REL]["reviewed_by"] == "reviewer-1"
    assert state.event_rows[CLUSTER_REL]["human_event_review"] == "confirmed"
    assert [item.document_id for item in pending_review_documents(batch, state)] == [
        POSSIBLE,
        IRRELEVANT,
    ]
