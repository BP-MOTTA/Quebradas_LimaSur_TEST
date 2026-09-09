from datetime import date
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.indeci_portal import (
    MAX_PORTAL_HTML_LENGTH,
    PortalParseError,
    detect_golden_cases,
    parse_indeci_portal_html,
)

PORTAL_URL = (
    "https://portal.indeci.gob.pe/informe/"
    "reportes-preliminares-complementarios-emergencias/"
)
EMERGENCY_PORTAL_URL = (
    "https://portal.indeci.gob.pe/informe/informe-de-emergencia/"
)
FIXTURE_PATH = Path("tests/fixtures/synthetic_indeci_live_page.html")


def test_portal_parser_extracts_cards_links_metadata_and_pagination() -> None:
    result = parse_indeci_portal_html(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        page_url=PORTAL_URL,
    )

    assert result.reported_count == 3
    assert len(result.candidates) == 3
    assert result.next_page_url == (
        f"{PORTAL_URL}page/2/?title=Chaclacayo&anos_alertas=2019"
    )

    first = result.candidates[0]
    assert first.document_type == "Reporte Complementario"
    assert first.report_number == "630"
    assert first.report_date == date(2019, 3, 3)
    assert first.detail_url.endswith("/emergencias/synthetic-golden-2019/")
    assert first.pdf_url is not None and first.pdf_url.endswith(".pdf")
    assert "ignore previous instructions" not in first.title

    second = result.candidates[1]
    assert second.document_type == "Informe de Emergencia"
    assert second.report_number == "1496"
    assert second.report_date == date(2023, 5, 5)

    third = result.candidates[2]
    assert third.detail_url is None
    assert third.pdf_url is None
    assert third.warnings == (
        "card has no download link",
        "card has no detail link",
    )


def test_portal_parser_detects_both_golden_cases() -> None:
    result = parse_indeci_portal_html(
        FIXTURE_PATH.read_text(encoding="utf-8"),
        page_url=PORTAL_URL,
    )

    golden = detect_golden_cases(result.candidates)

    assert golden.golden_2019_found is True
    assert golden.golden_2023_found is True
    assert golden.golden_2019 is not None
    assert golden.golden_2019.report_number == "630"
    assert golden.golden_2023 is not None
    assert golden.golden_2023.report_number == "1496"


def test_portal_parser_accepts_explicit_empty_results() -> None:
    result = parse_indeci_portal_html(
        """
        <h4>Alertas encontradas: 0</h4>
        <section class="list-news alerts-archive"></section>
        """,
        page_url=PORTAL_URL,
    )

    assert result.reported_count == 0
    assert result.candidates == ()
    assert result.warnings == ()


def test_portal_parser_accepts_emergency_archive_and_its_pagination() -> None:
    result = parse_indeci_portal_html(
        f"""
        <h4>Alertas encontradas: 0</h4>
        <section class="list-news alerts-archive"></section>
        <a class="next page-numbers"
           href="{EMERGENCY_PORTAL_URL}page/2/?title=1496&amp;anos_alertas=2023">
          Siguiente
        </a>
        """,
        page_url=(
            f"{EMERGENCY_PORTAL_URL}?title=1496&tipo_alerta=&anos_alertas=2023"
        ),
    )

    assert result.reported_count == 0
    assert result.next_page_url == (
        f"{EMERGENCY_PORTAL_URL}page/2/?title=1496&anos_alertas=2023"
    )


@pytest.mark.parametrize(
    "html",
    [
        "",
        "<html><body>maintenance</body></html>",
        "<h4>Alertas encontradas: 2</h4><section></section>",
        (
            "<h4>Alertas encontradas: 2</h4>"
            '<section class="list-news alerts-archive"></section>'
        ),
    ],
)
def test_portal_parser_rejects_empty_or_unexpected_html(html) -> None:
    with pytest.raises(PortalParseError):
        parse_indeci_portal_html(html, page_url=PORTAL_URL)


def test_portal_parser_rejects_oversized_html() -> None:
    with pytest.raises(PortalParseError):
        parse_indeci_portal_html(
            "x" * (MAX_PORTAL_HTML_LENGTH + 1),
            page_url=PORTAL_URL,
        )


def test_portal_parser_rejects_links_outside_indeci_allowlist() -> None:
    result = parse_indeci_portal_html(
        """
        <h4>Alertas encontradas: 1</h4>
        <section class="list-news alerts-archive">
          <article>
            <h3>REPORTE PRELIMINAR N. 1 - 01/01/2020 TEST</h3>
            <a class="btn-download" href="https://example.org/file.pdf">DESCARGAR</a>
            <a class="lnk-view" href="http://127.0.0.1/private">Ver mas</a>
          </article>
        </section>
        """,
        page_url=PORTAL_URL,
    )

    candidate = result.candidates[0]
    assert candidate.pdf_url is None
    assert candidate.detail_url is None
    assert candidate.warnings == (
        "download URL rejected: host is not allowlisted",
        "detail URL rejected: url host must be public",
    )


def test_portal_parser_extracts_number_from_n_dot_ordinal_spelling() -> None:
    result = parse_indeci_portal_html(
        """
        <h4>Alertas encontradas: 1</h4>
        <section class="list-news alerts-archive">
          <article>
            <h3>
              REPORTE COMPLEMENTARIO N.º 11380 - 29/11/2023 TEST
            </h3>
            <a
              class="btn-download"
              href="/wp-content/uploads/synthetic/REPORTE-COMPLEMENTARIO-N.º-11380-29NOV2023-TEST.pdf"
            >DESCARGAR</a>
          </article>
        </section>
        """,
        page_url=PORTAL_URL,
    )

    assert result.candidates[0].report_number == "11380"


def test_portal_parser_rejects_untrusted_pagination_as_data() -> None:
    result = parse_indeci_portal_html(
        """
        <h4>Alertas encontradas: 0</h4>
        <section class="list-news alerts-archive"></section>
        <a class="next page-numbers" href="https://example.org/page/2/">
          Siguiente
        </a>
        """,
        page_url=PORTAL_URL,
    )

    assert result.next_page_url is None
    assert result.warnings == (
        "pagination URL rejected: host is not allowlisted",
    )


def test_portal_parser_rejects_pagination_to_another_allowlisted_archive() -> None:
    result = parse_indeci_portal_html(
        f"""
        <h4>Alertas encontradas: 0</h4>
        <section class="list-news alerts-archive"></section>
        <a class="next page-numbers"
           href="{EMERGENCY_PORTAL_URL}page/2/?title=1496&amp;anos_alertas=2023">
          Siguiente
        </a>
        """,
        page_url=PORTAL_URL,
    )

    assert result.next_page_url is None
    assert result.warnings == (
        "pagination URL rejected: pagination URL changed the archive path",
    )


def test_portal_parser_handles_html_void_elements_without_losing_structure() -> None:
    result = parse_indeci_portal_html(
        """
        <h4>Alertas <br> encontradas: 1</h4>
        <section class="list-news alerts-archive">
          <article>
            <img src="synthetic.jpg">
            <h3>REPORTE PRELIMINAR N. 1 <br> 01/01/2020 TEST</h3>
          </article>
        </section>
        """,
        page_url=PORTAL_URL,
    )

    assert result.reported_count == 1
    assert len(result.candidates) == 1
    assert result.candidates[0].report_number == "1"
