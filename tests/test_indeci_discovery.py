import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.archive_connector import ArchiveDiscoverySettings
from quebradas_limaeste.inventory.archive_emergencias import (
    ArchiveEmergenciasConnector,
)
from quebradas_limaeste.inventory.archive_informes import ArchiveInformesConnector
from quebradas_limaeste.inventory.discovery import (
    CANDIDATES_OUTPUT,
    RUN_OUTPUT,
    DiscoveryConfigError,
    deduplicate_candidates,
    execute_discovery,
    load_discovery_config,
    run_discovery,
    write_discovery_outputs,
)
from quebradas_limaeste.inventory.discovery_models import (
    ConnectorResult,
    DiscoveryCandidate,
)
from quebradas_limaeste.inventory.ingestion_models import SeedDocument
from quebradas_limaeste.inventory.live_smoke import HttpResponse, PortalQuery
from quebradas_limaeste.inventory.seed_discovery import SeedDiscoveryConnector

FIXED_TIME = datetime(2026, 9, 9, 15, 0, tzinfo=UTC)
SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")


def candidate(
    *,
    connector: str,
    title: str,
    number: str | None,
    report_type: str | None,
    report_date: date | None,
    pdf_url: str | None,
    year: int,
) -> DiscoveryCandidate:
    return DiscoveryCandidate.create(
        source_connector=connector,
        title=title,
        detail_url=None,
        pdf_url=pdf_url,
        report_type=report_type,
        report_number=number,
        report_date=report_date,
        year=year,
        discovered_at_utc=FIXED_TIME,
        query_context={"title": "synthetic", "year": year},
        raw_metadata={"fixture": True},
    )


@dataclass
class StubConnector:
    name: str
    candidates: tuple[DiscoveryCandidate, ...] = ()
    requests: int = 0
    pages: int = 0
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    failure: Exception | None = None

    def discover(
        self,
        *,
        years: tuple[int, ...],
        discovered_at_utc: datetime,
    ) -> ConnectorResult:
        del discovered_at_utc
        if self.failure is not None:
            raise self.failure
        selected = tuple(item for item in self.candidates if item.year in years)
        return ConnectorResult(
            connector=self.name,
            candidates=selected,
            requests=self.requests,
            pages=self.pages,
            warnings=self.warnings,
            errors=self.errors,
        )


class RecordingTransport:
    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, *, timeout_seconds, user_agent, max_bytes):
        del timeout_seconds, user_agent, max_bytes
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def archive_settings() -> ArchiveDiscoverySettings:
    return ArchiveDiscoverySettings(
        queries=(PortalQuery.create(title="Chaclacayo", year=2019),),
        request_delay_seconds=0.5,
        timeout_seconds=5.0,
        retry_backoff_seconds=0.1,
        max_attempts=2,
        max_pages_per_query=1,
    )


@pytest.mark.parametrize(
    ("connector_class", "expected_name", "expected_path"),
    [
        (
            ArchiveInformesConnector,
            "archive_informes",
            "/informe/reportes-preliminares-complementarios-emergencias/",
        ),
        (
            ArchiveEmergenciasConnector,
            "archive_emergencias",
            "/informe/informe-de-emergencia/",
        ),
    ],
)
def test_archive_connectors_are_independent_and_never_follow_pdf_links(
    connector_class,
    expected_name,
    expected_path,
) -> None:
    html = Path("tests/fixtures/synthetic_indeci_live_page.html").read_bytes()
    transport = RecordingTransport([HttpResponse(200, html, "text/html")])
    connector = connector_class(
        archive_settings(),
        transport=transport,
        sleep=lambda _: None,
    )

    result = connector.discover(years=(2019,), discovered_at_utc=FIXED_TIME)

    assert connector.name == expected_name
    assert result.connector == expected_name
    assert result.requests == 1
    assert result.pages == 1
    assert result.candidates
    assert expected_path in transport.urls[0]
    assert all("/wp-content/uploads/" not in url for url in transport.urls)


def test_archive_connector_records_no_results_and_http_failure() -> None:
    empty_html = b"""
    <html><body><h4>Alertas encontradas: 0</h4>
    <section class="alerts-archive"></section></body></html>
    """
    transport = RecordingTransport(
        [
            HttpResponse(429, b"", "text/html"),
            HttpResponse(200, empty_html, "text/html"),
        ]
    )
    connector = ArchiveInformesConnector(
        archive_settings(),
        transport=transport,
        sleep=lambda _: None,
    )

    result = connector.discover(years=(2019,), discovered_at_utc=FIXED_TIME)

    assert result.requests == 2
    assert result.pages == 1
    assert result.candidates == ()
    assert any("HTTP 429" in warning for warning in result.warnings)
    assert any("no results" in warning for warning in result.warnings)
    assert result.errors == ()


@pytest.mark.parametrize(
    ("outcome", "expected_error"),
    [
        (HttpResponse(404, b"", "text/html"), "HTTP 404"),
        (TimeoutError("synthetic timeout"), "timeout"),
        (HttpResponse(200, b"", "text/html"), "empty response"),
        (
            HttpResponse(200, b"<html><body>maintenance</body></html>", "text/html"),
            "unexpected portal HTML",
        ),
        (
            HttpResponse(200, b"{}", "application/json"),
            "unexpected content type",
        ),
    ],
)
def test_archive_connector_records_bounded_failures(outcome, expected_error) -> None:
    responses = [outcome]
    if isinstance(outcome, TimeoutError):
        responses.append(outcome)
    connector = ArchiveInformesConnector(
        archive_settings(),
        transport=RecordingTransport(responses),
        sleep=lambda _: None,
    )

    result = connector.discover(years=(2019,), discovered_at_utc=FIXED_TIME)

    assert result.candidates == ()
    assert any(expected_error in error for error in result.errors)


def test_archive_connector_ignores_pagination_that_changes_query() -> None:
    html = b"""
    <html><body><h4>Alertas encontradas: 0</h4>
    <section class="alerts-archive"></section>
    <a class="next page-numbers"
       href="https://portal.indeci.gob.pe/informe/reportes-preliminares-complementarios-emergencias/page/2/?title=Other&amp;tipo_alerta=&amp;anos_alertas=2019">Next</a>
    </body></html>
    """
    connector = ArchiveInformesConnector(
        archive_settings(),
        transport=RecordingTransport([HttpResponse(200, html, "text/html")]),
        sleep=lambda _: None,
    )

    result = connector.discover(years=(2019,), discovered_at_utc=FIXED_TIME)

    assert result.requests == 1
    assert any("unexpected pagination URL ignored" in item for item in result.warnings)


def test_seed_connector_emits_metadata_without_claiming_index_discovery() -> None:
    seed = SeedDocument(
        document_id="INDECI_IE1496_20230505",
        report_number="1496",
        report_type="informe_emergencia",
        report_date=date(2023, 5, 5),
        source_domain="portal.indeci.gob.pe",
        source_url="https://portal.indeci.gob.pe/wp-content/uploads/ie1496.pdf",
        original_filename="INFORME-DE-EMERGENCIA-N-1496-5MAY2023.pdf",
        expected_site_terms=("Chaclacayo",),
        expected_region_terms=("Lima",),
    )
    connector = SeedDiscoveryConnector((seed,))

    result = connector.discover(years=(2023,), discovered_at_utc=FIXED_TIME)

    assert result.requests == 0
    assert result.pages == 0
    assert result.candidates[0].source_connector == "seed_discovery"
    assert result.candidates[0].query_context["mechanism"] == "known_official_seed"


def test_identical_candidate_from_two_connectors_converges_by_pdf_url() -> None:
    first = candidate(
        connector="archive_informes",
        title="Reporte complementario 630",
        number="630",
        report_type="reporte_complementario",
        report_date=date(2019, 3, 3),
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/rc630.pdf",
        year=2019,
    )
    second = candidate(
        connector="archive_emergencias",
        title="REPORTE COMPLEMENTARIO N 630",
        number="630",
        report_type="reporte_complementario",
        report_date=date(2019, 3, 3),
        pdf_url="https://PORTAL.INDECI.GOB.PE/wp-content/uploads/rc630.pdf#page=1",
        year=2019,
    )

    unique, duplicates = deduplicate_candidates((first, second))

    assert duplicates == 1
    assert len(unique) == 1
    assert unique[0].dedup_status == "merged"
    assert unique[0].dedup_reason == "canonical_pdf_url"
    assert unique[0].discovery_sources_matched == (
        "archive_emergencias",
        "archive_informes",
    )


def test_same_report_identity_with_different_pdf_urls_is_not_merged() -> None:
    common = {
        "title": "Informe de emergencia 1496",
        "number": "1496",
        "report_type": "informe_emergencia",
        "report_date": date(2023, 5, 5),
        "year": 2023,
    }
    first = candidate(
        connector="archive_informes",
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/a.pdf",
        **common,
    )
    second = candidate(
        connector="archive_emergencias",
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/b.pdf",
        **common,
    )

    unique, duplicates = deduplicate_candidates((first, second))

    assert duplicates == 0
    assert len(unique) == 2
    assert {item.dedup_status for item in unique} == {"ambiguous"}
    assert {item.dedup_reason for item in unique} == {
        "conflicting_pdf_urls_same_report_identity"
    }


def test_connector_failure_and_empty_source_do_not_abort_discovery() -> None:
    empty = StubConnector(name="archive_emergencias", warnings=("no results",))
    failed = StubConnector(
        name="archive_informes",
        failure=TimeoutError("synthetic timeout"),
    )
    seed = StubConnector(name="seed_discovery")

    result = run_discovery(
        years=(2017, 2019, 2023, 2024),
        connectors=(failed, empty, seed),
        now=lambda: FIXED_TIME,
    )
    manifest = result.to_manifest()

    assert manifest["candidates_raw"] == 0
    assert manifest["candidates_unique"] == 0
    assert manifest["connectors_attempted"][0]["status"] == "failed"
    assert manifest["connectors_attempted"][1]["status"] == "success"
    assert "synthetic timeout" in manifest["errors"][0]
    assert "no results" in manifest["warnings"][0]


def test_goldens_keep_index_discovery_separate_from_seed_availability() -> None:
    rc630 = candidate(
        connector="archive_informes",
        title="Huaico en Lurigancho y Chaclacayo",
        number="630",
        report_type="reporte_complementario",
        report_date=date(2019, 3, 3),
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/rc630.pdf",
        year=2019,
    )
    ie1496 = candidate(
        connector="seed_discovery",
        title="Lluvias intensas en Lima",
        number="1496",
        report_type="informe_emergencia",
        report_date=date(2023, 5, 5),
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/ie1496.pdf",
        year=2023,
    )

    result = run_discovery(
        years=(2019, 2023),
        connectors=(
            StubConnector(name="archive_informes", candidates=(rc630,)),
            StubConnector(name="archive_emergencias"),
            StubConnector(name="seed_discovery", candidates=(ie1496,)),
        ),
        now=lambda: FIXED_TIME,
    )
    manifest = result.to_manifest()

    assert manifest["golden_rc630_discovered"] is True
    assert manifest["golden_rc630_connectors"] == ["archive_informes"]
    assert manifest["golden_ie1496_discovered"] is False
    assert manifest["golden_ie1496_connectors"] == []
    assert manifest["golden_ie1496_available_as_seed"] is True
    assert manifest["candidate_sources"][0]["sources_attempted"] == [
        "archive_emergencias",
        "archive_informes",
        "seed_discovery",
    ]


@pytest.mark.parametrize(
    "title",
    [
        "Lluvias intensas en el distrito de Chaclacayo",
        "Activacion de quebrada Cusipata en Cusco",
    ],
)
def test_discovery_retains_generic_and_out_of_area_titles_for_later_classification(
    title,
) -> None:
    found = candidate(
        connector="archive_emergencias",
        title=title,
        number=None,
        report_type=None,
        report_date=None,
        pdf_url=None,
        year=2024,
    )

    result = run_discovery(
        years=(2024,),
        connectors=(StubConnector(name="archive_emergencias", candidates=(found,)),),
        now=lambda: FIXED_TIME,
    )

    assert [item.title for item in result.candidates] == [title]


def test_repository_discovery_config_is_bounded_to_pilot_years() -> None:
    config = load_discovery_config(SOURCE_CONFIG, allowed_root=Path.cwd())

    assert config.allowed_years == (2017, 2019, 2023, 2024)
    assert config.max_pages_per_query == 1
    assert {query.year for query in config.queries} == set(config.allowed_years)
    assert len(config.queries) <= 16
    assert config.seed_config == Path("configs/sources/indeci_seed_documents.yaml")


@pytest.mark.parametrize("years", [(2018,), (2017, 2025), (), (2019, 2019)])
def test_discovery_rejects_years_outside_unique_pilot_set(years) -> None:
    with pytest.raises(DiscoveryConfigError):
        run_discovery(years=years, connectors=(), now=lambda: FIXED_TIME)


def test_execute_discovery_writes_only_ignored_metadata_outputs(tmp_path) -> None:
    config_path = tmp_path / "configs" / "sources" / "indeci.yaml"
    config_path.parent.mkdir(parents=True)
    repository_config = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    seed_path = tmp_path / "configs" / "sources" / "seeds.yaml"
    repository_config["discovery"]["seed_config"] = str(seed_path.relative_to(tmp_path))
    config_path.write_text(json.dumps(repository_config), encoding="utf-8")
    seed_path.write_text(json.dumps({"documents": []}), encoding="utf-8")
    raw_dir = tmp_path / "data" / "raw" / "indeci"
    raw_dir.mkdir(parents=True)
    marker = raw_dir / "keep.txt"
    marker.write_text("immutable", encoding="utf-8")

    payload = execute_discovery(
        config_path,
        years=(2019,),
        candidates_output=CANDIDATES_OUTPUT,
        run_output=RUN_OUTPUT,
        allowed_root=tmp_path,
        connectors=(StubConnector(name="archive_informes"),),
        now=lambda: FIXED_TIME,
    )

    assert payload["candidates_unique"] == 0
    assert (tmp_path / CANDIDATES_OUTPUT).is_file()
    assert (tmp_path / RUN_OUTPUT).is_file()
    assert marker.read_text(encoding="utf-8") == "immutable"
    assert list(tmp_path.rglob("*.pdf")) == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/report.pdf",
        "https://portal.indeci.gob.pe:444/wp-content/uploads/report.pdf",
    ],
)
def test_discovery_candidate_rejects_non_official_urls(url) -> None:
    with pytest.raises(DiscoveryConfigError):
        candidate(
            connector="archive_informes",
            title="Untrusted",
            number=None,
            report_type=None,
            report_date=None,
            pdf_url=url,
            year=2019,
        )


def test_candidate_csv_neutralizes_spreadsheet_formulas(tmp_path) -> None:
    unsafe_title = candidate(
        connector="archive_informes",
        title='=HYPERLINK("https://example.test")',
        number=None,
        report_type=None,
        report_date=None,
        pdf_url=None,
        year=2019,
    )
    result = run_discovery(
        years=(2019,),
        connectors=(
            StubConnector(name="archive_informes", candidates=(unsafe_title,)),
        ),
        now=lambda: FIXED_TIME,
    )

    csv_path, _ = write_discovery_outputs(
        result,
        candidates_output=CANDIDATES_OUTPUT,
        run_output=RUN_OUTPUT,
        allowed_root=tmp_path,
    )

    assert "'=HYPERLINK" in csv_path.read_text(encoding="utf-8")
