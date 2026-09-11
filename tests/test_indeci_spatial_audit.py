import csv
import json
from hashlib import sha256
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.candidate_audit import AUDIT_FIELDS
from quebradas_limaeste.inventory.full_ingestion import ALL_DOCUMENT_FIELDS
from quebradas_limaeste.inventory.limaeste_filter import (
    DOCUMENT_FILTER_FIELDS,
    load_filter_policy,
)
from quebradas_limaeste.inventory.spatial_audit import (
    SPATIAL_AUDIT_FIELDS,
    detect_normalized_locations,
    execute_spatial_false_negative_audit,
)

FILTER_CONFIG = Path("configs/sources/indeci_limaeste_filter.yaml")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Río Seco", "RIO-SECO"),
        ("Rio Seco", "RIO-SECO"),
        ("Los Cóndores", "LOS-CONDORES"),
        ("Los Condores", "LOS-CONDORES"),
        ("Huascarán", "HUASCARAN"),
        ("Huascaran", "HUASCARAN"),
        ("Lurigancho-Chosica", "LURIGANCHO-CHOSICA"),
        ("Lurigancho   Chosica", "LURIGANCHO-CHOSICA"),
        ("San Bartolomé", "SAN-BARTOLOME"),
        ("San Bartolome", "SAN-BARTOLOME"),
    ],
)
def test_normalized_location_variants_are_detected(text, expected) -> None:
    policy = load_filter_policy(FILTER_CONFIG, allowed_root=Path.cwd())

    detected = detect_normalized_locations(text, policy=policy)

    assert detected == (expected,)


def source_document(document_id: str, **overrides) -> dict[str, object]:
    row = {field: "" for field in ALL_DOCUMENT_FIELDS}
    row.update(
        {
            "document_id": document_id,
            "year": "2023",
            "title": "Reporte en Piura",
            "report_type": "informe_emergencia",
            "report_number": "1",
            "report_date": "2023-03-20",
            "download_status": "duplicate",
            "extraction_status": "success",
            "ocr_required": "false",
            "relevance_status": "not_relevant",
            "matched_terms": "[]",
            "review_required": "true",
        }
    )
    row.update(overrides)
    return row


def filtered_document(document_id: str, **overrides) -> dict[str, object]:
    row = {field: "" for field in DOCUMENT_FILTER_FIELDS}
    row.update(
        {
            "document_id": document_id,
            "year": "2023",
            "title": "Reporte en Piura",
            "report_type": "informe_emergencia",
            "report_number": "1",
            "relevance_status": "not_relevant",
            "spatial_relevance": "low",
            "event_relevance": "unknown",
            "rainfall_related": "unknown",
            "review_priority": "PX",
            "priority_reason": "spatial relevance is low",
            "candidate_count": "0",
            "strong_count": "0",
            "moderate_count": "0",
            "weak_count": "0",
            "review_required": "true",
            "matched_terms": "[]",
            "primary_location": "UNKNOWN",
            "primary_event": "UNKNOWN",
            "report_date": "2023-03-20",
            "local_pdf_status": "missing",
        }
    )
    row.update(overrides)
    return row


def candidate(document_id: str) -> dict[str, object]:
    row = {field: "" for field in AUDIT_FIELDS}
    row.update(
        {
            "candidate_id": "candidate-jicamarca",
            "source_document_id": document_id,
            "source_page": "2",
            "event_date": "2023-03-16",
            "evidence_snippet": "Lluvias intensas en Jicamarca.",
            "matched_terms": '["Jicamarca","lluvias intensas"]',
            "validation_status": "pending_review",
            "event_type": "lluvias_intensas",
            "candidate_strength": "moderate",
        }
    )
    return row


def write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_audit_distinguishes_absence_from_detector_false_negative(tmp_path) -> None:
    config = tmp_path / FILTER_CONFIG
    config.parent.mkdir(parents=True)
    config.write_bytes(FILTER_CONFIG.read_bytes())
    raw = tmp_path / "data/raw/indeci/2023/immutable.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF-1.4\nimmutable synthetic raw\n%%EOF\n")
    raw_hash = sha256(raw.read_bytes()).hexdigest()
    documents_path = tmp_path / "metadata/indeci/all_documents.csv"
    candidates_path = tmp_path / "metadata/indeci/all_event_candidates.csv"
    filtered_path = tmp_path / "metadata/indeci/geographic_event_filter_documents.csv"
    jicamarca_id = "INDECI_JICAMARCA"
    huaycoloro_id = "INDECI_HUAYCOLORO"
    absent_id = "INDECI_ABSENT"
    write_csv(
        documents_path,
        ALL_DOCUMENT_FIELDS,
        [
            source_document(jicamarca_id),
            source_document(huaycoloro_id),
            source_document(absent_id, raw_local_path=str(raw), sha256=raw_hash),
        ],
    )
    write_csv(candidates_path, AUDIT_FIELDS, [candidate(jicamarca_id)])
    write_csv(
        filtered_path,
        DOCUMENT_FILTER_FIELDS,
        [
            filtered_document(
                jicamarca_id,
                spatial_relevance="comparison",
                event_relevance="target",
                review_priority="P3",
                priority_reason="comparison location with target event evidence",
                detected_locations="JICAMARCA",
                primary_location="JICAMARCA",
            ),
            filtered_document(huaycoloro_id),
            filtered_document(
                absent_id,
                raw_local_path=str(raw),
                sha256=raw_hash,
                local_pdf_status="available",
            ),
        ],
    )
    text_root = tmp_path / "data/interim/indeci/2023"
    text_root.mkdir(parents=True)
    (text_root / f"{huaycoloro_id}.txt").write_text(
        "Se inspeccionó la quebrada Huaycoloro.", encoding="utf-8"
    )
    (text_root / f"{absent_id}.txt").write_text(
        "No se menciona ninguna ubicación configurada.", encoding="utf-8"
    )
    before = sha256(raw.read_bytes()).hexdigest()

    payload = execute_spatial_false_negative_audit(
        documents_path=documents_path,
        candidates_path=candidates_path,
        filtered_documents_path=filtered_path,
        config_path=config.relative_to(tmp_path),
        audit_output=Path("metadata/indeci/spatial_false_negative_audit.csv"),
        summary_output=Path("metadata/indeci/spatial_audit_summary.json"),
        allowed_root=tmp_path,
        expected_documents=3,
    )

    assert payload["documents_total"] == 3
    assert payload["false_negatives"] == 1
    audit_path = tmp_path / "metadata/indeci/spatial_false_negative_audit.csv"
    with audit_path.open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    assert tuple(rows[0]) == SPATIAL_AUDIT_FIELDS
    assert len(rows) == 69
    by_key = {(row["document_id"], row["location_term"]): row for row in rows}
    jicamarca = by_key[(jicamarca_id, "JICAMARCA")]
    assert jicamarca["found_in_evidence"] == "true"
    assert jicamarca["current_detected_location"] == "JICAMARCA"
    assert jicamarca["false_negative_suspected"] == "false"
    huaycoloro = by_key[(huaycoloro_id, "HUAYCOLORO")]
    assert huaycoloro["found_in_full_text"] == "true"
    assert huaycoloro["false_negative_suspected"] == "true"
    assert huaycoloro["reason"] == "detector_false_negative"
    absent = by_key[(absent_id, "QUIRIO")]
    assert not any(absent[field] == "true" for field in SPATIAL_AUDIT_FIELDS[3:8])
    assert absent["reason"] == "not_present_in_document"
    summary = json.loads(
        (tmp_path / "metadata/indeci/spatial_audit_summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["locations_present"] == ["HUAYCOLORO", "JICAMARCA"]
    assert "QUIRIO" in summary["locations_absent"]
    assert summary["false_negatives"] == [
        {
            "document_id": huaycoloro_id,
            "location_term": "HUAYCOLORO",
            "reason": "detector_false_negative",
        }
    ]
    assert summary["documents_comparison"] == 1
    assert summary["documents_low"] == 2
    assert summary["core_near_nonpriority_events"] == {}
    assert summary["rainfall_relation_blocks_priority"] == 0
    assert summary["location_rule_failures"] == 0
    assert summary["priority_rule_inconsistencies"] == 0
    assert sha256(raw.read_bytes()).hexdigest() == before
