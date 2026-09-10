import csv
from hashlib import sha256
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_FIELDS,
    CONSOLIDATED_FIELDS,
)
from quebradas_limaeste.inventory.full_ingestion import ALL_DOCUMENT_FIELDS
from quebradas_limaeste.inventory.limaeste_filter import (
    CANDIDATE_FILTER_FIELDS,
    DOCUMENT_FILTER_FIELDS,
    FilterEvidence,
    evaluate_relevance,
    execute_limaeste_filter,
    load_filter_policy,
)

FILTER_CONFIG = Path("configs/sources/indeci_limaeste_filter.yaml")


def decision(
    *,
    candidate_fields=(),
    snippets=(),
    matched_terms=(),
    full_text="",
    title="",
):
    policy = load_filter_policy(FILTER_CONFIG, allowed_root=Path.cwd())
    return evaluate_relevance(
        FilterEvidence(
            candidate_fields=tuple(candidate_fields),
            evidence_snippets=tuple(snippets),
            matched_terms=tuple(matched_terms),
            full_text=full_text,
            title=title,
            text_available=True,
        ),
        policy=policy,
    )


@pytest.mark.parametrize(
    ("text", "spatial", "priority"),
    [
        ("Cusipata: se reportó un huaico", "core", "P1"),
        ("Lluvias intensas en Chaclacayo", "core", "P1"),
        ("Se activó la quebrada Quirio", "near", "P2"),
        ("Desborde en la quebrada Huaycoloro", "near", "P2"),
        ("Lluvias intensas en Jicamarca", "comparison", "P3"),
        ("Deslizamiento en Cieneguilla", "comparison", "P3"),
    ],
)
def test_target_events_receive_spatially_ordered_priority(
    text,
    spatial,
    priority,
) -> None:
    result = decision(candidate_fields=[text], snippets=[text])

    assert result.spatial_relevance == spatial
    assert result.event_relevance == "target"
    assert result.review_priority == priority


def test_generic_lima_does_not_raise_priority() -> None:
    result = decision(
        candidate_fields=["lluvias intensas"],
        title="Lluvias intensas en Lima",
    )

    assert result.spatial_relevance == "low"
    assert result.review_priority == "PX"


@pytest.mark.parametrize(
    "title",
    [
        "Incendio forestal en Chaclacayo",
        "Incendio forestal en Lima",
    ],
)
def test_forest_fire_is_excluded_even_near_the_pilot(title) -> None:
    result = decision(title=title, full_text=title)

    assert result.event_relevance == "excluded_topic"
    assert result.review_priority == "PX"


def test_core_location_without_event_remains_reviewable_p4() -> None:
    result = decision(candidate_fields=["Cusipata"])

    assert result.spatial_relevance == "core"
    assert result.event_relevance == "unknown"
    assert result.review_priority == "P4"


def test_huaico_outside_lima_este_is_px() -> None:
    result = decision(
        candidate_fields=["huaico"],
        full_text="Se reportó un huaico en Piura.",
        title="Huaico en Piura",
    )

    assert result.event_relevance == "target"
    assert result.spatial_relevance == "low"
    assert result.review_priority == "PX"


def test_specific_comparison_phrase_suppresses_overlapping_near_name() -> None:
    result = decision(candidate_fields=["Lluvias intensas en San Juan de Lurigancho"])

    assert result.detected_locations == ("SAN-JUAN-DE-LURIGANCHO",)
    assert result.spatial_relevance == "comparison"
    assert result.review_priority == "P3"


def test_full_text_can_supply_location_when_title_is_generic() -> None:
    result = decision(
        candidate_fields=["huaico"],
        full_text="El evento afectó la quebrada Cusipata en Chaclacayo.",
        title="Lluvias intensas en Lima",
    )

    assert result.primary_location == "CUSIPATA"
    assert result.review_priority == "P1"


def test_specific_full_text_location_outweighs_generic_lima_matched_term() -> None:
    result = decision(
        candidate_fields=["lluvias intensas"],
        matched_terms=["Lima", "lluvias intensas"],
        full_text="Las lluvias afectaron el distrito de Chaclacayo.",
        title="Lluvias intensas en Lima",
    )

    assert result.primary_location == "CHACLACAYO"
    assert result.spatial_relevance == "core"
    assert result.review_priority == "P1"


def test_rainfall_relation_requires_explicit_causal_context() -> None:
    explicit = decision(
        snippets=[
            "A consecuencia de las intensas precipitaciones se activó la quebrada."
        ]
    )
    unlinked = decision(
        snippets=["Se activó la quebrada."],
        full_text="En otra sección se registraron precipitaciones.",
    )
    negated = decision(
        snippets=["El deslizamiento no estuvo relacionado con las lluvias."],
    )

    assert explicit.rainfall_related == "true"
    assert unlinked.rainfall_related == "unknown"
    assert negated.rainfall_related == "false"


def document_row(document_id: str, **overrides) -> dict[str, object]:
    row = {field: "" for field in ALL_DOCUMENT_FIELDS}
    row.update(
        {
            "document_id": document_id,
            "year": "2023",
            "title": "Lluvias intensas en Lima",
            "report_type": "informe_emergencia",
            "report_number": "1",
            "report_date": "2023-03-20",
            "raw_local_path": "",
            "sha256": "",
            "page_count": "1",
            "download_status": "duplicate",
            "extraction_status": "success",
            "ocr_required": "false",
            "relevance_status": "potentially_relevant",
            "matched_terms": '["Lima","lluvias intensas"]',
            "review_required": "true",
        }
    )
    row.update(overrides)
    return row


def candidate_row(document_id: str) -> dict[str, object]:
    row = {field: "" for field in AUDIT_FIELDS}
    row.update(
        {
            "candidate_id": "indeci-candidate-00000000000000000001",
            "source_document_id": document_id,
            "source_page": "2",
            "event_date": "2023-03-16",
            "evidence_snippet": (
                "Debido a lluvias intensas se produjo un huaico en Cusipata."
            ),
            "matched_terms": '["Cusipata","lluvias intensas","huaico"]',
            "validation_status": "pending_review",
            "reported_quebrada": "Cusipata",
            "canonical_site_id": "quebrada_cusipata_chaclacayo",
            "event_type": "huaico",
            "site_evidence": "Cusipata",
            "event_evidence": "huaico",
            "date_evidence": "16 de marzo de 2023",
            "candidate_strength": "strong",
            "review_reason": "synthetic_fixture",
        }
    )
    return row


def cluster_row(candidate_id: str) -> dict[str, object]:
    row = {field: "" for field in CONSOLIDATED_FIELDS}
    row.update(
        {
            "event_cluster_id": "indeci-event-00000000000000000001",
            "canonical_site_id": "quebrada_cusipata_chaclacayo",
            "event_date": "2023-03-16",
            "event_type": "huaico",
            "reported_quebrada": "Cusipata",
            "candidate_strength": "strong",
            "supporting_candidates": candidate_id,
            "supporting_pages": "2",
            "best_evidence_snippet": "Huaico en Cusipata.",
            "validation_status": "pending_review",
        }
    )
    return row


def write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_filter_outputs_documents_candidates_and_summary_without_touching_raw(
    tmp_path,
) -> None:
    config = tmp_path / FILTER_CONFIG
    config.parent.mkdir(parents=True)
    config.write_bytes(FILTER_CONFIG.read_bytes())
    raw = tmp_path / "data/raw/indeci/2023/original.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF-1.4\nimmutable synthetic raw\n%%EOF\n")
    raw_hash = sha256(raw.read_bytes()).hexdigest()
    documents = tmp_path / "metadata/indeci/all_documents.csv"
    candidates = tmp_path / "metadata/indeci/all_event_candidates.csv"
    clusters = tmp_path / "metadata/indeci/all_event_clusters.csv"
    core_id = "INDECI_IE1_20230320"
    fire_id = "INDECI_IE2_20230320"
    missing_id = "INDECI_IE3_20230320"
    rows = [
        document_row(
            core_id,
            raw_local_path=str(raw),
            sha256=raw_hash,
            matched_terms='["Cusipata","Chaclacayo","huaico"]',
        ),
        document_row(
            fire_id,
            title="Incendio forestal en Chaclacayo",
            report_number="2",
            relevance_status="not_relevant",
            matched_terms='["Chaclacayo"]',
        ),
        document_row(
            missing_id,
            title="Huaico en Piura",
            report_number="3",
            matched_terms='["huaico"]',
        ),
    ]
    write_csv(documents, ALL_DOCUMENT_FIELDS, rows)
    event = candidate_row(core_id)
    write_csv(candidates, AUDIT_FIELDS, [event])
    write_csv(clusters, CONSOLIDATED_FIELDS, [cluster_row(event["candidate_id"])])
    text_root = tmp_path / "data/interim/indeci/2023"
    text_root.mkdir(parents=True)
    (text_root / f"{core_id}.txt").write_text(
        "Debido a lluvias intensas se produjo un huaico en Cusipata.",
        encoding="utf-8",
    )
    (text_root / f"{fire_id}.txt").write_text(
        "Incendio forestal en Chaclacayo.",
        encoding="utf-8",
    )
    before = (raw.name, sha256(raw.read_bytes()).hexdigest())

    payload = execute_limaeste_filter(
        documents_path=documents,
        candidates_path=candidates,
        clusters_path=clusters,
        config_path=config.relative_to(tmp_path),
        documents_output=Path("metadata/indeci/geographic_event_filter_documents.csv"),
        candidates_output=Path(
            "metadata/indeci/geographic_event_filter_candidates.csv"
        ),
        summary_output=Path("metadata/indeci/geographic_summary.csv"),
        summary_text_output=Path("metadata/indeci/geographic_summary.txt"),
        allowed_root=tmp_path,
        expected_documents=3,
    )

    assert payload["documents_total"] == 3
    assert payload["missing_local_pdf"] == 2
    assert payload["priorities"] == {"P1": 1, "P2": 0, "P3": 0, "P4": 0, "PX": 2}
    with (tmp_path / "metadata/indeci/geographic_event_filter_documents.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        filtered_documents = list(csv.DictReader(source))
    with (tmp_path / "metadata/indeci/geographic_event_filter_candidates.csv").open(
        newline="", encoding="utf-8"
    ) as source:
        filtered_candidates = list(csv.DictReader(source))
    assert tuple(filtered_documents[0]) == DOCUMENT_FILTER_FIELDS
    assert tuple(filtered_candidates[0]) == CANDIDATE_FILTER_FIELDS
    assert [row["review_priority"] for row in filtered_documents] == [
        "P1",
        "PX",
        "PX",
    ]
    assert filtered_documents[0]["relevance_status"] == "potentially_relevant"
    assert filtered_documents[0]["matched_terms"] == (
        '["Cusipata","Chaclacayo","huaico"]'
    )
    assert filtered_candidates[0]["event_cluster_id"] == (
        "indeci-event-00000000000000000001"
    )
    assert filtered_candidates[0]["rainfall_related"] == "true"
    assert "training_label" not in filtered_documents[0]
    assert "training_label" not in filtered_candidates[0]
    assert (tmp_path / "metadata/indeci/geographic_summary.csv").is_file()
    summary_text = (tmp_path / "metadata/indeci/geographic_summary.txt").read_text(
        encoding="utf-8"
    )
    assert "CUSIPATA\n- documents: 1\n- event candidates: 1" in summary_text
    assert "QUIRIO\n- documents: 0\n- event candidates: 0" in summary_text
    assert (raw.name, sha256(raw.read_bytes()).hexdigest()) == before
