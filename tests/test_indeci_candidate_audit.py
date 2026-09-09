import csv
import json
from hashlib import sha256
from pathlib import Path

from quebradas_limaeste.inventory.candidate_audit import (
    AUDIT_OUTPUT,
    CONSOLIDATED_OUTPUT,
    GOLDEN_CONTROLS_OUTPUT,
    execute_candidate_audit,
    execute_golden_control_comparison,
    write_original_candidates_from_payload,
)

QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")


def candidate_payload(
    document_id="INDECI_IE1496_20230505",
    snippet=(
        "El 14 de marzo de 2023 se activó la quebrada "
        "Cusipata en Chaclacayo."
    ),
):
    return {
        "documents": [
            {
                "document_id": document_id,
                "event_candidates": [
                    {
                        "source_document_id": document_id,
                        "source_page": 3,
                        "event_date": "2023-03-14",
                        "evidence_snippet": snippet,
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


def test_golden_control_comparison_contains_only_document_ids_and_counts(
    tmp_path,
) -> None:
    config_path = tmp_path / "configs" / "sources" / QUALITY_CONFIG.name
    config_path.parent.mkdir(parents=True)
    config_path.write_text(QUALITY_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    metadata = tmp_path / "metadata" / "indeci"
    negative_candidates = metadata / "negative_candidates.csv"
    positive_candidates = metadata / "positive_candidates.csv"
    write_original_candidates_from_payload(
        candidate_payload(
            snippet="El 14 de marzo de 2023 se activó la quebrada Huaycoloro."
        ),
        output_path=negative_candidates,
        allowed_root=tmp_path,
    )
    write_original_candidates_from_payload(
        candidate_payload(document_id="INDECI_RC630_20190303"),
        output_path=positive_candidates,
        allowed_root=tmp_path,
    )
    execute_candidate_audit(
        negative_candidates,
        config_path=config_path,
        audit_output=metadata / "negative_audit.csv",
        consolidated_output=metadata / "negative_consolidated.csv",
        allowed_root=tmp_path,
    )
    execute_candidate_audit(
        positive_candidates,
        config_path=config_path,
        audit_output=metadata / "positive_audit.csv",
        consolidated_output=metadata / "positive_consolidated.csv",
        allowed_root=tmp_path,
    )

    result = execute_golden_control_comparison(
        metadata / "negative_audit.csv",
        metadata / "positive_audit.csv",
        output_path=tmp_path / GOLDEN_CONTROLS_OUTPUT,
        allowed_root=tmp_path,
    )

    assert result == {
        "negative_control": {
            "document_id": "INDECI_IE1496_20230505",
            "strong": 0,
            "moderate": 0,
            "weak": 1,
        },
        "positive_control": {
            "document_id": "INDECI_RC630_20190303",
            "strong": 1,
            "moderate": 0,
            "weak": 0,
        },
    }
    saved = json.loads((tmp_path / GOLDEN_CONTROLS_OUTPUT).read_text())
    assert saved == result
    assert "evidence" not in json.dumps(saved)
