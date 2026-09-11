import csv
import json
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.discovery_models import (
    ConnectorResult,
    DiscoveryCandidate,
)
from quebradas_limaeste.inventory.historical_discovery import (
    HISTORICAL_DISCOVERY_FIELDS,
    HistoricalConfig,
    execute_historical_discovery,
    historical_document_ids,
    load_historical_config,
)
from quebradas_limaeste.inventory.historical_inventory import (
    HISTORICAL_DOCUMENT_FIELDS,
    HistoricalCandidate,
    HistoricalProcessedDocument,
    _load_run_checkpoint,
    apply_sha256_deduplication,
    assign_historical_event_clusters,
    build_document_row,
    execute_historical_inventory,
    historical_final_decision,
    historical_prefilter,
    select_px_control_sample,
)
from quebradas_limaeste.inventory.limaeste_filter import (
    load_filter_policy,
)

NOW = datetime(2026, 9, 10, 16, 0, tzinfo=UTC)
HISTORICAL_CONFIG = Path("configs/sources/indeci_historical_cusipata.yaml")
FILTER_CONFIG = Path("configs/sources/indeci_limaeste_filter.yaml")


class StubConnector:
    def __init__(self, name, candidates_by_year=None, fail_years=()) -> None:
        self.name = name
        self.candidates_by_year = candidates_by_year or {}
        self.fail_years = set(fail_years)
        self.calls = []

    def discover(self, *, years, discovered_at_utc):
        year = years[0]
        self.calls.append(year)
        if year in self.fail_years:
            raise OSError("synthetic connector failure")
        return ConnectorResult(
            connector=self.name,
            candidates=tuple(self.candidates_by_year.get(year, ())),
            requests=1,
            pages=1,
        )


def candidate(
    *,
    connector="archive_informes",
    year=2010,
    title="LLUVIAS INTENSAS EN CHACLACAYO",
    url="https://portal.indeci.gob.pe/wp-content/uploads/2010/01/a.pdf",
):
    return DiscoveryCandidate.create(
        source_connector=connector,
        title=title,
        detail_url=None,
        pdf_url=url,
        report_type="reporte_preliminar",
        report_number="10",
        report_date=date(year, 1, 2),
        year=year,
        discovered_at_utc=NOW,
        query_context={"year": year},
        raw_metadata={},
    )


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def test_historical_config_freezes_2010_2026_and_pilot_filter() -> None:
    config = load_historical_config(HISTORICAL_CONFIG, allowed_root=Path.cwd())

    assert config.years == tuple(range(2010, 2027))
    assert config.blocks == (
        (2010, 2011, 2012, 2013, 2014),
        (2015, 2016, 2017, 2018, 2019),
        (2020, 2021, 2022),
        (2023, 2024, 2025, 2026),
    )
    assert config.filter_config == FILTER_CONFIG


def test_discovery_records_empty_year_partial_failure_and_resumes(tmp_path) -> None:
    config = HistoricalConfig.for_tests(years=(2010, 2011))
    first = StubConnector("archive_informes", fail_years=(2011,))
    second = StubConnector("archive_emergencias")

    payload = execute_historical_discovery(
        config=config,
        connectors=(first, second),
        allowed_root=tmp_path,
        now=lambda: NOW,
    )

    assert payload["years_processed"] == [2010, 2011]
    assert payload["documents_unique"] == 0
    assert payload["errors"]
    assert (tmp_path / "metadata/indeci/historical/checkpoints/2010.json").is_file()
    assert (tmp_path / "metadata/indeci/historical/checkpoints/2011.json").is_file()

    resumed_first = StubConnector("archive_informes")
    resumed_second = StubConnector("archive_emergencias")
    resumed = execute_historical_discovery(
        config=config,
        connectors=(resumed_first, resumed_second),
        allowed_root=tmp_path,
        now=lambda: NOW,
    )

    assert resumed["resume_status"] == "complete_output_reused"
    assert resumed_first.calls == []
    assert resumed_second.calls == []


def test_discovery_deduplicates_across_years_and_connectors(tmp_path) -> None:
    config = HistoricalConfig.for_tests(years=(2010, 2011))
    shared_url = "https://portal.indeci.gob.pe/wp-content/uploads/2010/01/shared.pdf"
    informes = StubConnector(
        "archive_informes",
        {2010: (candidate(url=shared_url),)},
    )
    emergencias = StubConnector(
        "archive_emergencias",
        {
            2011: (
                candidate(
                    connector="archive_emergencias",
                    year=2011,
                    url=shared_url,
                ),
            )
        },
    )

    payload = execute_historical_discovery(
        config=config,
        connectors=(informes, emergencias),
        allowed_root=tmp_path,
        now=lambda: NOW,
    )
    rows = read_csv(tmp_path / "metadata/indeci/historical/discovery_documents.csv")

    assert payload["documents_raw"] == 2
    assert payload["documents_unique"] == 1
    assert payload["duplicates"] == 1
    assert rows[0]["duplicate_status"] == "merged"
    assert json.loads(rows[0]["discovery_sources"]) == [
        "archive_emergencias",
        "archive_informes",
    ]
    assert tuple(rows[0]) == HISTORICAL_DISCOVERY_FIELDS


def test_historical_document_ids_reuse_pilot_scheme_and_registry() -> None:
    item = DiscoveryCandidate.create(
        source_connector="archive_informes",
        title="Reporte sin fecha completa",
        detail_url=None,
        pdf_url="https://portal.indeci.gob.pe/wp-content/uploads/2010/01/id.pdf",
        report_type="reporte_preliminar",
        report_number="10",
        report_date=None,
        year=2010,
        discovered_at_utc=NOW,
        query_context={},
        raw_metadata={},
    )
    expected = f"INDECI_DISC_{sha256(item.pdf_url.encode()).hexdigest()[:16].upper()}"

    derived = historical_document_ids((item,))
    registered = historical_document_ids(
        (item,), known_by_url={item.pdf_url: "INDECI_EXISTING"}
    )

    assert derived[item.discovery_id] == expected
    assert registered[item.discovery_id] == "INDECI_EXISTING"


def test_prefilter_preserves_pilot_priority_and_keeps_px() -> None:
    policy = load_filter_policy(FILTER_CONFIG, allowed_root=Path.cwd())

    p1 = historical_prefilter(
        "Lluvias intensas causaron un huaico en Cusipata, Chaclacayo",
        policy=policy,
    )
    p2 = historical_prefilter(
        "Activación de quebrada por lluvias intensas en Huaycoloro",
        policy=policy,
    )
    px = historical_prefilter("Incendio urbano en Lima", policy=policy)
    generic = historical_prefilter("LLUVIAS INTENSAS EN LIMA", policy=policy)

    assert (p1.spatial_relevance, p1.priority) == ("core", "P4")
    assert (p2.spatial_relevance, p2.priority) == ("near", "P4")
    assert px.priority == "PX"
    assert generic.priority == "PX"
    assert generic.metadata_insufficient is True


def test_final_historical_evidence_preserves_p1_and_p2_rules() -> None:
    policy = load_filter_policy(FILTER_CONFIG, allowed_root=Path.cwd())
    base = {
        "sha256": "a" * 64,
        "page_count": 1,
        "download_status": "downloaded",
        "extraction_status": "text_extracted",
        "ocr_required": False,
        "matched_terms": (),
        "full_text": "",
    }
    p1_result = HistoricalProcessedDocument(
        document_id="INDECI_P1",
        candidates=(
            HistoricalCandidate(
                candidate_id="p1-candidate",
                document_id="INDECI_P1",
                document_family_id="indeci-family-p1",
                event_date="2023-03-15",
                event_time="",
                event_type="huaico",
                reported_quebrada="Cusipata",
                candidate_strength="strong",
                evidence_snippet="Se reportó un huaico en Cusipata.",
            ),
        ),
        **base,
    )
    p2_result = HistoricalProcessedDocument(
        document_id="INDECI_P2",
        candidates=(
            HistoricalCandidate(
                candidate_id="p2-candidate",
                document_id="INDECI_P2",
                document_family_id="indeci-family-p2",
                event_date="2023-03-15",
                event_time="",
                event_type="huaico",
                reported_quebrada="Huaycoloro",
                candidate_strength="weak",
                evidence_snippet="Se reportó un huaico en Huaycoloro.",
            ),
        ),
        **base,
    )

    p1 = historical_final_decision("Reporte", result=p1_result, policy=policy)
    p2 = historical_final_decision("Reporte", result=p2_result, policy=policy)

    assert (p1.spatial_relevance, p1.review_priority) == ("core", "P1")
    assert (p2.spatial_relevance, p2.review_priority) == ("near", "P2")


def test_px_control_sample_is_deterministic_and_does_not_delete_px() -> None:
    ids = tuple(f"INDECI_DISC_{index:04d}" for index in range(20))

    first = select_px_control_sample(ids, seed="a1.19", sample_size=5)
    second = select_px_control_sample(tuple(reversed(ids)), seed="a1.19", sample_size=5)

    assert first == second
    assert len(first) == 5
    assert set(first) < set(ids)
    assert len(ids) == 20


def test_report_date_remains_distinct_from_event_date() -> None:
    row = build_document_row(
        document_id="INDECI_RP10_20100102",
        document_family_id="indeci-family-one",
        year=2010,
        title="Huayco en Chaclacayo",
        report_type="reporte_preliminar",
        report_number="10",
        report_date="2010-01-02",
        source_url="https://portal.indeci.gob.pe/informe/a/",
        pdf_url="",
        prefilter=historical_prefilter(
            "Huayco en Chaclacayo",
            policy=load_filter_policy(FILTER_CONFIG, allowed_root=Path.cwd()),
        ),
        event_date="2010-01-01",
    )

    assert row["report_date"] == "2010-01-02"
    assert row["event_date"] == "2010-01-01"
    assert tuple(row) == HISTORICAL_DOCUMENT_FIELDS
    assert "training_label" not in row


def test_same_document_family_does_not_create_multiple_physical_events() -> None:
    candidates = (
        HistoricalCandidate(
            candidate_id="candidate-a",
            document_id="INDECI_RP10_20100102",
            document_family_id="indeci-family-one",
            event_date="2010-01-01",
            event_time="10:00",
            event_type="huaico",
            reported_quebrada="Cusipata",
            candidate_strength="strong",
        ),
        HistoricalCandidate(
            candidate_id="candidate-b",
            document_id="INDECI_RC11_20100103",
            document_family_id="indeci-family-one",
            event_date="2010-01-01",
            event_time="10:00",
            event_type="huaico",
            reported_quebrada="Cusipata",
            candidate_strength="moderate",
        ),
    )

    assignments, clusters = assign_historical_event_clusters(candidates)

    assert len(clusters) == 1
    assert assignments["candidate-a"] == assignments["candidate-b"]
    assert clusters[0]["validation_status"] == "pending_review"
    assert all("training_label" not in cluster for cluster in clusters)


def test_sha256_deduplication_retains_rows_and_sets_canonical_family() -> None:
    rows = [
        {
            "document_id": "INDECI_A",
            "document_family_id": "indeci-family-a",
            "duplicate_status": "unique",
            "duplicate_reason": "no_duplicate_signal",
            "canonical_document_id": "INDECI_A",
        },
        {
            "document_id": "INDECI_B",
            "document_family_id": "indeci-family-b",
            "duplicate_status": "unique",
            "duplicate_reason": "no_duplicate_signal",
            "canonical_document_id": "INDECI_B",
        },
    ]
    processed = {
        document_id: HistoricalProcessedDocument(
            document_id=document_id,
            sha256="c" * 64,
            page_count=1,
            download_status="downloaded",
            extraction_status="text_extracted",
            ocr_required=False,
            matched_terms=(),
            full_text="",
            candidates=(),
        )
        for document_id in ("INDECI_A", "INDECI_B")
    }

    duplicates = apply_sha256_deduplication(rows, processed=processed)

    assert duplicates == 1
    assert len(rows) == 2
    assert {row["canonical_document_id"] for row in rows} == {"INDECI_A"}
    assert {row["document_family_id"] for row in rows} == {"indeci-family-a"}
    assert all(row["duplicate_reason"] == "sha256" for row in rows)


def test_historical_run_keeps_px_and_continues_after_document_failure(tmp_path) -> None:
    config = HistoricalConfig.for_tests(years=(2010,))
    policy_path = tmp_path / FILTER_CONFIG
    policy_path.parent.mkdir(parents=True)
    policy_path.write_bytes(FILTER_CONFIG.read_bytes())
    config = HistoricalConfig(
        **{
            **config.__dict__,
            "filter_config": FILTER_CONFIG,
            "px_sample_size": 2,
        }
    )
    discovery_path = tmp_path / "metadata/indeci/historical/discovery_documents.csv"
    discovery_path.parent.mkdir(parents=True)
    rows = []
    for document_id, title in (
        ("INDECI_DISC_GOOD", "Lluvias intensas en Lima"),
        ("INDECI_DISC_BAD", "Incendio urbano en Lima"),
    ):
        rows.append(
            {
                "document_id": document_id,
                "document_family_id": (
                    "indeci-family-good"
                    if document_id.endswith("GOOD")
                    else "indeci-family-bad"
                ),
                "year": 2010,
                "title": title,
                "report_type": "",
                "report_number": "",
                "report_date": "",
                "source_connector": "archive_informes",
                "detail_url": "",
                "pdf_url": (
                    "https://portal.indeci.gob.pe/wp-content/uploads/2010/01/"
                    f"{document_id.lower()}.pdf"
                ),
                "discovery_sources": '["archive_informes"]',
                "discovered_at_utc": NOW.isoformat(),
                "duplicate_status": "unique",
                "duplicate_reason": "no_duplicate_signal",
                "canonical_document_id": document_id,
                "spatial_relevance": "low",
                "event_relevance": (
                    "possible_target" if "Lluvias" in title else "excluded_topic"
                ),
                "preliminary_priority": "PX",
                "priority_reason": "spatial relevance is low",
                "metadata_insufficient": str("Lluvias" in title).lower(),
            }
        )
    with discovery_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=HISTORICAL_DISCOVERY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    calls = []

    def processor(row):
        calls.append(row["document_id"])
        if row["document_id"] == "INDECI_DISC_BAD":
            raise RuntimeError("synthetic PDF failure")
        return HistoricalProcessedDocument(
            document_id=row["document_id"],
            sha256="a" * 64,
            page_count=2,
            download_status="downloaded",
            extraction_status="text_extracted",
            ocr_required=False,
            matched_terms=("Lima", "lluvias intensas"),
            full_text="Lluvias intensas en Lima sin mención de una quebrada local.",
            candidates=(),
        )

    payload = execute_historical_inventory(
        config=config,
        discovery_path=discovery_path.relative_to(tmp_path),
        allowed_root=tmp_path,
        processor=processor,
        now=lambda: NOW,
    )
    documents = read_csv(tmp_path / "metadata/indeci/historical/all_documents.csv")
    year_summary = read_csv(tmp_path / "metadata/indeci/historical/year_summary.csv")

    assert payload["documents_unique"] == 2
    assert payload["download_failures"] == 1
    assert len(documents) == 2
    assert {row["review_priority"] for row in documents} == {"PX"}
    assert all(row["human_validation_status"] == "pending_review" for row in documents)
    assert year_summary[0]["year"] == "2010"
    assert year_summary[0]["errors"] == "1"
    assert set(calls) == {"INDECI_DISC_GOOD", "INDECI_DISC_BAD"}


def test_px_false_negative_stops_remaining_control_expansion(tmp_path) -> None:
    config = HistoricalConfig.for_tests(years=(2010,))
    policy_path = tmp_path / FILTER_CONFIG
    policy_path.parent.mkdir(parents=True)
    policy_path.write_bytes(FILTER_CONFIG.read_bytes())
    config = HistoricalConfig(
        **{**config.__dict__, "filter_config": FILTER_CONFIG, "px_sample_size": 3}
    )
    discovery_path = tmp_path / "metadata/indeci/historical/discovery_documents.csv"
    discovery_path.parent.mkdir(parents=True)
    rows = []
    for index in range(3):
        document_id = f"INDECI_DISC_PX{index}"
        rows.append(
            {
                field: value
                for field, value in zip(
                    HISTORICAL_DISCOVERY_FIELDS,
                    (
                        document_id,
                        "indeci-family-px",
                        2010,
                        "Reporte general",
                        "",
                        "",
                        "",
                        "archive_informes",
                        "",
                        "https://portal.indeci.gob.pe/wp-content/uploads/2010/01/"
                        f"px{index}.pdf",
                        '["archive_informes"]',
                        NOW.isoformat(),
                        "unique",
                        "no_duplicate_signal",
                        document_id,
                        "low",
                        "unknown",
                        "PX",
                        "spatial relevance is low",
                        "false",
                    ),
                    strict=True,
                )
            }
        )
    with discovery_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=HISTORICAL_DISCOVERY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    calls = []

    def processor(row):
        calls.append(row["document_id"])
        return HistoricalProcessedDocument(
            document_id=row["document_id"],
            sha256="b" * 64,
            page_count=1,
            download_status="downloaded",
            extraction_status="text_extracted",
            ocr_required=False,
            matched_terms=("Cusipata", "huaico"),
            full_text=(
                "El 1 de enero de 2010 se reportó un huaico en Cusipata, "
                "Chaclacayo, debido a lluvias intensas."
            ),
            candidates=(),
        )

    payload = execute_historical_inventory(
        config=config,
        discovery_path=discovery_path.relative_to(tmp_path),
        allowed_root=tmp_path,
        processor=processor,
        now=lambda: NOW,
    )

    assert payload["px_false_negatives"] == 1
    assert payload["px_sampled"] == 1
    assert len(calls) == 1
    assert any("PX false negative" in item for item in payload["warnings"])


def test_historical_run_checkpoint_rejects_symlink(tmp_path) -> None:
    config = HistoricalConfig.for_tests(years=(2010,))
    outside = tmp_path.parent / "outside-historical-checkpoint.json"
    outside.write_text("{}", encoding="utf-8")
    checkpoint = tmp_path / "metadata/indeci/historical/run_checkpoints/2010.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.symlink_to(outside)

    try:
        with pytest.raises(RuntimeError, match="must not be a symlink"):
            _load_run_checkpoint(
                checkpoint,
                year=2010,
                policy=config,
                root=tmp_path,
            )
    finally:
        outside.unlink(missing_ok=True)
