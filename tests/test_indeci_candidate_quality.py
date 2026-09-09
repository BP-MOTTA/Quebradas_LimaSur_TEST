from datetime import date
from pathlib import Path

from quebradas_limaeste.inventory.candidate_quality import (
    OriginalCandidate,
    audit_candidate,
    consolidate_candidates,
    load_quality_policy,
)

QUALITY_CONFIG = Path("configs/sources/indeci_candidate_quality.yaml")


def make_candidate(
    snippet,
    *,
    candidate_id="indeci-candidate-test-1",
    event_date=date(2023, 3, 14),
):
    return OriginalCandidate.create(
        candidate_id=candidate_id,
        source_document_id="INDECI_IE1496_20230505",
        source_page=1,
        event_date=event_date,
        evidence_snippet=snippet,
        matched_terms=("Cusipata", "Chaclacayo", "activacion de quebrada"),
        validation_status="pending_review",
    )


def test_same_sentence_site_event_location_and_date_is_strong() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "El 14 de marzo de 2023 se activó la quebrada Cusipata en Chaclacayo."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.candidate_strength == "strong"
    assert audited.reported_quebrada == "Cusipata"
    assert audited.canonical_site_id == "quebrada_cusipata_chaclacayo"
    assert audited.event_type == "activacion_quebrada"
    assert audited.site_evidence == "Cusipata"
    assert audited.event_evidence == "se activó la quebrada"
    assert audited.date_evidence == "14 de marzo de 2023"
    assert audited.validation_status == "pending_review"


def test_specific_activation_outweighs_trigger_in_same_dated_fact() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "El 14 de marzo de 2023 se registraron lluvias intensas que causaron "
        "la activación de la quebrada Huaycoloro en Chaclacayo."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.event_type == "activacion_quebrada"
    assert audited.reported_quebrada == "Huaycoloro"
    assert audited.event_evidence == "activación de la quebrada"
    assert audited.candidate_strength == "weak"


def test_specific_erosion_outweighs_rain_trigger_in_same_dated_fact() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "El 21 de marzo de 2023, a consecuencia de las lluvias intensas, "
        "se produjo una erosión fluvial en el sector Malecón Rímac."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.event_type == "erosion_fluvial"
    assert audited.event_evidence == "erosión fluvial"


def test_distant_site_and_event_is_weak() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "Cusipata aparece en una lista de quebradas.\n\n"
        "Antecedentes generales sin relación directa.\n\n"
        "Varias secciones después se menciona un huaico.",
        event_date=None,
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.candidate_strength == "weak"


def test_table_of_contents_candidate_is_never_strong() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "TABLA DE CONTENIDOS\n"
        "El 14 de marzo de 2023 se activó la quebrada Cusipata en Chaclacayo."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.candidate_strength == "weak"
    assert audited.review_reason.startswith("non_event_zone:")


def test_repeated_uppercase_header_is_never_strong() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "INFORME DE EMERGENCIA - 14 DE MARZO DE 2023 - ACTIVACIÓN DE LA "
        "QUEBRADA CUSIPATA - CHACLACAYO"
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.candidate_strength == "weak"
    assert audited.review_reason == "non_event_zone:repeated_header_or_footer"


def test_compatible_fragments_share_event_cluster_without_being_destroyed() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    first = audit_candidate(
        make_candidate(
            "El 14 de marzo de 2023 se activó la quebrada Cusipata "
            "en Chaclacayo.",
            candidate_id="indeci-candidate-test-1",
        ),
        policy=policy,
    )
    second = audit_candidate(
        make_candidate(
            "Chaclacayo reportó que el 14 de marzo de 2023 se activó "
            "la quebrada Cusipata.",
            candidate_id="indeci-candidate-test-2",
        ),
        policy=policy,
    )
    assert first is not None and second is not None

    clusters = consolidate_candidates((first, second))

    assert len(clusters) == 1
    assert clusters[0].supporting_candidates == (
        "indeci-candidate-test-1",
        "indeci-candidate-test-2",
    )
    assert len(clusters[0].supporting_snippets) == 2
    assert first.validation_status == "pending_review"
    assert second.validation_status == "pending_review"


def test_cusipata_in_cusco_quispicanchi_context_is_excluded() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "El 14 de marzo de 2023 se activó la quebrada Cusipata, "
        "provincia de Quispicanchi, Cusco."
    )

    assert audit_candidate(original, policy=policy) is None


def test_event_without_date_has_null_date_evidence_and_is_not_strong() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "Se activó la quebrada Cusipata en Chaclacayo.",
        event_date=None,
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.date_evidence is None
    assert audited.event_date is None
    assert audited.candidate_strength == "moderate"
    assert audited.validation_status == "pending_review"


def test_report_header_time_is_not_assigned_to_dated_event() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "INFORME DE EMERGENCIA / 02:00 HORAS. "
        "El 14 de marzo de 2023 se activó la quebrada Cusipata "
        "en Chaclacayo."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.event_time is None


def test_explicit_time_after_event_date_is_retained() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    original = make_candidate(
        "El 14 de marzo de 2023, a las 17:44 horas, se activó la "
        "quebrada Cusipata en Chaclacayo."
    )

    audited = audit_candidate(original, policy=policy)

    assert audited is not None
    assert audited.event_time == "17:44"


def test_consolidation_never_validates_candidates() -> None:
    policy = load_quality_policy(QUALITY_CONFIG)
    audited = audit_candidate(
        make_candidate(
            "El 14 de marzo de 2023 se activó la quebrada Cusipata "
            "en Chaclacayo."
        ),
        policy=policy,
    )
    assert audited is not None

    clusters = consolidate_candidates((audited,))

    assert clusters[0].validation_status == "pending_review"
