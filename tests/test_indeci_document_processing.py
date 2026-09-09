from datetime import date

import pymupdf

from quebradas_limaeste.inventory.document_classification import classify_document
from quebradas_limaeste.inventory.event_candidates import extract_event_candidates
from quebradas_limaeste.inventory.pdf_extract import extract_pdf_text


def create_synthetic_pdf(path, page_texts) -> None:
    document = pymupdf.open()
    for text in page_texts:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 790), text, fontsize=10)
    document.save(path)
    document.close()


def test_extract_classify_and_generate_pending_candidates(tmp_path) -> None:
    pdf_path = tmp_path / "synthetic.pdf"
    text_path = tmp_path / "interim" / "synthetic.txt"
    create_synthetic_pdf(
        pdf_path,
        [
            "INFORME DE EMERGENCIA N. 1496 - 5/5/2023\n"
            "LLUVIAS INTENSAS EN EL DEPARTAMENTO DE LIMA\n"
            "I. HECHOS\n"
            "El 12 de marzo de 2023, se registraron lluvias intensas que "
            "causaron la activacion de la quebrada Huaycoloro en Chaclacayo, "
            "Lima.",
            "El 16 de marzo de 2023, se produjo un huaico en Chaclacayo, Lima.",
        ],
    )

    extraction = extract_pdf_text(
        pdf_path,
        text_path=text_path,
        minimum_text_characters=40,
        allowed_root=tmp_path,
    )
    classification = classify_document(
        extraction.full_text,
        expected_site_terms=("Chaclacayo",),
        expected_region_terms=("Lima",),
    )
    candidates = extract_event_candidates(
        "INDECI_IE1496_20230505",
        extraction.page_texts,
        expected_site_terms=("Chaclacayo",),
        expected_region_terms=("Lima",),
    )

    assert extraction.page_count == 2
    assert extraction.extraction_status == "success"
    assert extraction.ocr_required is False
    assert "\f" in text_path.read_text(encoding="utf-8")
    assert classification.geographic_match is True
    assert classification.site_match is True
    assert classification.event_match is True
    assert classification.relevance_status == "relevant"
    assert classification.review_required is True
    assert "Chaclacayo" in classification.matched_terms
    assert len(candidates) == 2
    assert [candidate.event_date for candidate in candidates] == [
        date(2023, 3, 12),
        date(2023, 3, 16),
    ]
    assert [candidate.source_page for candidate in candidates] == [1, 2]
    assert all(
        candidate.validation_status == "pending_review" for candidate in candidates
    )
    assert all(len(candidate.evidence_snippet) <= 360 for candidate in candidates)


def test_empty_extraction_marks_ocr_required_without_running_ocr(tmp_path) -> None:
    pdf_path = tmp_path / "empty.pdf"
    create_synthetic_pdf(pdf_path, [""])

    extraction = extract_pdf_text(
        pdf_path,
        text_path=tmp_path / "interim" / "empty.txt",
        minimum_text_characters=20,
        allowed_root=tmp_path,
    )

    assert extraction.page_count == 1
    assert extraction.full_text == ""
    assert extraction.extraction_status == "empty"
    assert extraction.ocr_required is True


def test_report_date_is_not_used_as_event_date() -> None:
    page_texts = (
        "INFORME DE EMERGENCIA N. 1496 - 5/5/2023 - "
        "LLUVIAS INTENSAS EN EL DEPARTAMENTO DE LIMA",
    )

    candidates = extract_event_candidates(
        "INDECI_IE1496_20230505",
        page_texts,
        expected_site_terms=("Chaclacayo",),
        expected_region_terms=("Lima",),
    )

    assert candidates == ()
