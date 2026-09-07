import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.live_smoke import (
    HttpResponse,
    LiveSmokeConfig,
    LiveSmokeConfigError,
    PortalQuery,
    load_live_smoke_config,
    run_live_smoke,
    write_live_smoke_result,
)

PORTAL_URL = (
    "https://portal.indeci.gob.pe/informe/"
    "reportes-preliminares-complementarios-emergencias/"
)
FIXTURE_BYTES = Path("tests/fixtures/synthetic_indeci_live_page.html").read_bytes()
REPOSITORY_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
EMPTY_PAGE = b"""
<h4>Alertas encontradas: 0</h4>
<section class="list-news alerts-archive"></section>
"""


class StubTransport:
    def __init__(self, outcomes) -> None:
        self.outcomes = list(outcomes)
        self.urls = []

    def get(self, url, *, timeout_seconds, user_agent, max_bytes):
        self.urls.append(url)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_config(
    *,
    max_attempts=2,
    max_pages_per_query=1,
    queries=None,
) -> LiveSmokeConfig:
    return LiveSmokeConfig.create(
        portal_url=PORTAL_URL,
        request_delay_seconds=0.5,
        timeout_seconds=3.0,
        retry_backoff_seconds=0.25,
        max_attempts=max_attempts,
        max_pages_per_query=max_pages_per_query,
        queries=queries
        or (
            PortalQuery.create(title="Chaclacayo", year=2019),
        ),
    )


def test_load_config_reads_json_compatible_yaml_and_validates_limits(tmp_path) -> None:
    path = tmp_path / "indeci.yaml"
    path.write_text(
        json.dumps(
            {
                "portal_url": PORTAL_URL,
                "request_delay_seconds": 1.0,
                "timeout_seconds": 10.0,
                "retry_backoff_seconds": 1.0,
                "max_attempts": 2,
                "max_pages_per_query": 1,
                "queries": [
                    {"title": "Chaclacayo", "type": "", "year": 2019},
                    {"title": "1496", "type": "", "year": 2023},
                ],
            }
        ),
        encoding="utf-8",
    )

    config = load_live_smoke_config(path)

    assert config.portal_url == PORTAL_URL
    assert [query.title for query in config.queries] == ["Chaclacayo", "1496"]
    assert config.queries[1].year == 2023

    bad_path = tmp_path / "bad.yaml"
    bad_path.write_text(
        json.dumps(
            {
                "portal_url": "https://example.org/",
                "request_delay_seconds": 0,
                "timeout_seconds": 10,
                "retry_backoff_seconds": 1,
                "max_attempts": 10,
                "max_pages_per_query": 20,
                "queries": [{"title": "all", "year": 2023}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(LiveSmokeConfigError):
        load_live_smoke_config(bad_path)


def test_repository_config_contains_four_single_page_queries() -> None:
    config = load_live_smoke_config(REPOSITORY_CONFIG)

    assert config.max_pages_per_query == 1
    assert [(query.title, query.year) for query in config.queries] == [
        ("Chaclacayo", 2019),
        ("Lurigancho Chaclacayo", 2019),
        ("Chaclacayo", 2023),
        ("1496", 2023),
    ]


def test_config_rejects_query_parameters_in_base_portal_url() -> None:
    with pytest.raises(LiveSmokeConfigError):
        LiveSmokeConfig.create(
            portal_url=f"{PORTAL_URL}?title=unexpected",
            request_delay_seconds=1.0,
            timeout_seconds=10.0,
            retry_backoff_seconds=1.0,
            max_attempts=2,
            max_pages_per_query=1,
            queries=(PortalQuery.create(title="Chaclacayo", year=2019),),
        )


def test_run_live_smoke_is_sequential_bounded_and_never_fetches_pdf() -> None:
    transport = StubTransport(
        [
            HttpResponse(status=200, body=FIXTURE_BYTES, content_type="text/html"),
            HttpResponse(status=200, body=EMPTY_PAGE, content_type="text/html"),
        ]
    )
    sleeps = []
    config = make_config(
        queries=(
            PortalQuery.create(title="Chaclacayo", year=2019),
            PortalQuery.create(title="1496", year=2023),
        )
    )

    result = run_live_smoke(
        config,
        transport=transport,
        sleep=sleeps.append,
        now=lambda: datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )

    assert len(transport.urls) == 2
    assert all("portal.indeci.gob.pe" in url for url in transport.urls)
    assert all(not url.lower().endswith(".pdf") for url in transport.urls)
    assert sleeps == [0.5]
    assert result.pages_requested == 2
    assert result.documents_seen == 3
    assert result.golden_2019_found is True
    assert result.golden_2023_found is True
    assert len(result.queries_attempted) == 2
    assert result.errors == ()


def test_run_live_smoke_retries_429_with_backoff_then_succeeds() -> None:
    transport = StubTransport(
        [
            HttpResponse(status=429, body=b"", content_type="text/html"),
            HttpResponse(status=200, body=EMPTY_PAGE, content_type="text/html"),
        ]
    )
    sleeps = []

    result = run_live_smoke(
        make_config(),
        transport=transport,
        sleep=sleeps.append,
    )

    assert len(transport.urls) == 2
    assert sleeps == [0.5]
    assert result.errors == ()
    assert any("HTTP 429" in warning for warning in result.warnings)


def test_run_live_smoke_rejects_untrusted_next_query_without_requesting_it() -> None:
    page_with_untrusted_next = f"""
    <h4>Alertas encontradas: 0</h4>
    <section class="list-news alerts-archive"></section>
    <a class="next page-numbers" href="{PORTAL_URL}page/2/?unexpected=">
      Siguiente
    </a>
    """.encode()
    transport = StubTransport(
        [
            HttpResponse(
                status=200,
                body=page_with_untrusted_next,
                content_type="text/html",
            )
        ]
    )

    result = run_live_smoke(
        make_config(max_pages_per_query=2),
        transport=transport,
        sleep=lambda _: None,
    )

    assert len(transport.urls) == 1
    assert any("unsupported parameter" in error for error in result.errors)


@pytest.mark.parametrize(
    "next_url",
    [
        f"{PORTAL_URL}page/2/",
        f"{PORTAL_URL}page/2/?title=Chaclacayo&anos_alertas=2023",
    ],
)
def test_run_live_smoke_rejects_pagination_that_changes_filters(next_url) -> None:
    page = f"""
    <h4>Alertas encontradas: 0</h4>
    <section class="list-news alerts-archive"></section>
    <a class="next page-numbers" href="{next_url}">Siguiente</a>
    """.encode()
    transport = StubTransport(
        [HttpResponse(status=200, body=page, content_type="text/html")]
    )

    result = run_live_smoke(
        make_config(max_pages_per_query=2),
        transport=transport,
        sleep=lambda _: None,
    )

    assert len(transport.urls) == 1
    assert any("filters do not match" in error for error in result.errors)


def test_run_live_smoke_follows_pagination_with_the_same_filters() -> None:
    transport = StubTransport(
        [
            HttpResponse(status=200, body=FIXTURE_BYTES, content_type="text/html"),
            HttpResponse(status=200, body=EMPTY_PAGE, content_type="text/html"),
        ]
    )

    result = run_live_smoke(
        make_config(max_pages_per_query=2),
        transport=transport,
        sleep=lambda _: None,
    )

    assert len(transport.urls) == 2
    assert "title=Chaclacayo" in transport.urls[1]
    assert "anos_alertas=2019" in transport.urls[1]
    assert result.errors == ()


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (HttpResponse(status=404, body=b"", content_type="text/html"), "HTTP 404"),
        (TimeoutError("timed out"), "timeout"),
        (HttpResponse(status=200, body=b"", content_type="text/html"), "empty"),
        (
            HttpResponse(status=200, body=b"unexpected", content_type="text/html"),
            "unexpected",
        ),
    ],
)
def test_run_live_smoke_records_errors_without_hiding_them(outcome, expected) -> None:
    transport = StubTransport([outcome, outcome])

    result = run_live_smoke(make_config(), transport=transport, sleep=lambda _: None)

    assert result.golden_2019_found is False
    assert result.golden_2023_found is False
    assert any(expected.lower() in error.lower() for error in result.errors)


def test_write_result_emits_required_json_without_html(tmp_path) -> None:
    result = run_live_smoke(
        make_config(),
        transport=StubTransport(
            [HttpResponse(status=200, body=FIXTURE_BYTES, content_type="text/html")]
        ),
        sleep=lambda _: None,
        now=lambda: datetime(2026, 9, 7, 6, 0, tzinfo=UTC),
    )
    output = tmp_path / "metadata" / "indeci" / "live_smoke_results.json"

    write_live_smoke_result(output, result, allowed_root=tmp_path)

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert set(payload) == {
        "run_id",
        "timestamp",
        "portal_url",
        "queries_attempted",
        "pages_requested",
        "documents_seen",
        "candidates",
        "golden_2019_found",
        "golden_2023_found",
        "warnings",
        "errors",
    }
    assert "html" not in output.read_text(encoding="utf-8").lower()


def test_write_result_rejects_destination_outside_allowed_root(tmp_path) -> None:
    result = run_live_smoke(
        make_config(),
        transport=StubTransport(
            [HttpResponse(status=200, body=EMPTY_PAGE, content_type="text/html")]
        ),
        sleep=lambda _: None,
    )

    with pytest.raises(LiveSmokeConfigError):
        write_live_smoke_result(
            tmp_path.parent / "outside.json",
            result,
            allowed_root=tmp_path,
        )
