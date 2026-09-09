import csv
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_OUTPUT,
    CONSOLIDATED_OUTPUT,
    execute_candidate_audit,
    write_original_candidates_from_payload,
)

QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")


def candidate_payload():
    return {
        "documents": [
            {
                "document_id": "INDECI_IE1496_20230505",
                "event_candidates": [
                    {
                        "source_document_id": "INDECI_IE1496_20230505",
                        "source_page": 3,
                        "event_date": "2023-03-14",
                        "evidence_snippet": (
                            "El 14 de marzo de 2023 se activó la quebrada "
                            "Cusipata en Chaclacayo."
                        ),
                        "matched_terms": [
                            "Cusipata",
                            "Chaclacayo",
                            "activacion de quebrada",
                        ],
                        "validation_status": "pending_review",
                    }
                ],
            }
        ]
    }


def test_audit_writes_separate_outputs_without_modifying_original_csv(
    tmp_path,
) -> None:
    config_path = tmp_path / "configs" / "sources" / QUALITY_CONFIG.name
    config_path.parent.mkdir(parents=True)
    config_path.write_text(QUALITY_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    input_path = tmp_path / "metadata" / "indeci" / "candidates.csv"
    write_original_candidates_from_payload(
        candidate_payload(),
        output_path=input_path,
        allowed_root=tmp_path,
    )
    original_hash = sha256(input_path.read_bytes()).hexdigest()

    result = execute_candidate_audit(
        input_path,
        config_path=config_path,
        audit_output=tmp_path / AUDIT_OUTPUT,
        consolidated_output=tmp_path / CONSOLIDATED_OUTPUT,
        allowed_root=tmp_path,
    )

    assert sha256(input_path.read_bytes()).hexdigest() == original_hash
    assert result["candidates_original"] == 1
    assert result["strong"] == 1
    assert result["moderate"] == 0
    assert result["weak"] == 0
    assert result["clusters_consolidated"] == 1
    assert result["strong_cusipata"] is True
    assert result["pages_relevant"] == [3]

    with (tmp_path / AUDIT_OUTPUT).open(newline="", encoding="utf-8") as source:
        audit_row = next(csv.DictReader(source))
    assert audit_row["site_evidence"] == "Cusipata"
    assert audit_row["event_evidence"] == "se activó la quebrada"
    assert audit_row["date_evidence"] == "14 de marzo de 2023"
    assert audit_row["validation_status"] == "pending_review"

    with (tmp_path / CONSOLIDATED_OUTPUT).open(
        newline="", encoding="utf-8"
    ) as source:
        reader = csv.DictReader(source)
        consolidated_row = next(reader)
        assert reader.fieldnames == [
            "event_cluster_id",
            "canonical_site_id",
            "event_date",
            "event_time",
            "event_type",
            "reported_quebrada",
            "candidate_strength",
            "supporting_candidates",
            "supporting_pages",
            "best_evidence_snippet",
            "validation_status",
        ]
    assert consolidated_row["validation_status"] == "pending_review"
