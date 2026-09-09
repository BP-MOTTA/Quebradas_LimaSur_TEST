from datetime import date
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.ingestion_models import (
    IngestionConfigError,
    derive_seed_document,
    load_ingestion_policy,
    load_seed_documents,
)

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")
SEEDS_CONFIG = Path("configs/sources/indeci_seed_documents.yaml")
GOLDEN_URL = (
    "https://portal.indeci.gob.pe/wp-content/uploads/2023/05/"
    "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-"
    "LLUVIAS-INTENSAS-EN-EL-DEPARTAMENTO-DE-LIMA-36-DEE.pdf"
)


def test_repository_ingestion_policy_is_bounded_and_allowlisted() -> None:
    policy = load_ingestion_policy(SOURCE_CONFIG)

    assert policy.allowed_domains == ("portal.indeci.gob.pe",)
    assert policy.timeout_seconds == 15.0
    assert policy.max_attempts == 2
    assert policy.max_pdf_bytes == 50 * 1024 * 1024
    assert policy.expected_site_terms == ("Chaclacayo",)
    assert policy.expected_region_terms == ("Lima",)


def test_repository_seed_preserves_verified_golden_metadata() -> None:
    policy = load_ingestion_policy(SOURCE_CONFIG)
    seeds = load_seed_documents(SEEDS_CONFIG, policy=policy)

    assert len(seeds) == 1
    seed = seeds[0]
    assert seed.document_id == "INDECI_IE1496_20230505"
    assert seed.report_number == "1496"
    assert seed.report_type == "informe_emergencia"
    assert seed.report_date == date(2023, 5, 5)
    assert seed.source_domain == "portal.indeci.gob.pe"
    assert seed.source_url == GOLDEN_URL
    assert seed.expected_site_terms == ("Chaclacayo",)
    assert seed.expected_region_terms == ("Lima",)


def test_direct_url_derives_identity_without_inventing_document_metadata() -> None:
    policy = load_ingestion_policy(SOURCE_CONFIG)

    seed = derive_seed_document(GOLDEN_URL, policy=policy)

    assert seed.document_id == "INDECI_IE1496_20230505"
    assert seed.report_number == "1496"
    assert seed.report_date == date(2023, 5, 5)
    assert seed.expected_site_terms == ("Chaclacayo",)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/report.pdf",
        "https://portal.indeci.gob.pe/not-uploads/report.pdf",
        "https://user:pass@portal.indeci.gob.pe/wp-content/uploads/report.pdf",
        "https://portal.indeci.gob.pe/wp-content/uploads/unparseable.pdf",
        "https://portal.indeci.gob.pe:8443/wp-content/uploads/2023/05/"
        "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-x.pdf",
        "https://portal.indeci.gob.pe/wp-content/uploads/../uploads/2023/05/"
        "INFORME-DE-EMERGENCIA-N%C2%BA-1496-5MAY2023-x.pdf",
    ],
)
def test_direct_url_rejects_untrusted_or_unidentifiable_urls(url) -> None:
    policy = load_ingestion_policy(SOURCE_CONFIG)

    with pytest.raises(IngestionConfigError):
        derive_seed_document(url, policy=policy)
