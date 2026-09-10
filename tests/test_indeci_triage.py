import csv
import json
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from quebradas_limaeste.inventory.discovery import CSV_FIELDS
from quebradas_limaeste.inventory.triage import (
    BATCH_SELECTION_OUTPUT,
    BatchPolicyError,
    execute_select_batch,
    load_batch_policy,
    score_title,
)

SOURCE_CONFIG = Path("configs/sources/indeci_cusipata.yaml")


def discovery_row(
    index: int,
    *,
    year: int,
    title: str,
    report_type: str = "reporte_complementario",
    report_number: str | None = None,
    report_date: date | None = None,
    source_connector: str = "archive_informes",
) -> dict[str, object]:
    number = report_number or str(1000 + index)
    day = (index % 20) + 1
    published = report_date or date(year, 1, day)
    pdf_url = (
        "https://portal.indeci.gob.pe/wp-content/uploads/"
        f"{year}/01/document-{index}.pdf"
    )
    discovery_id = sha256(f"pdf\n{pdf_url}".encode()).hexdigest()[:20]
    return {
        "discovery_id": f"indeci-discovery-{discovery_id}",
        "source_connector": source_connector,
        "title": title,
        "detail_url": f"https://portal.indeci.gob.pe/emergencias/item-{index}/",
        "pdf_url": pdf_url,
        "report_type": report_type,
        "report_number": number,
        "report_date": published.isoformat(),
        "year": year,
        "discovered_at_utc": "2026-09-10T03:54:23+00:00",
        "query_context": "{}",
        "raw_metadata": "{}",
        "discovery_warnings": "[]",
        "discovery_sources_attempted": json.dumps([source_connector]),
        "discovery_sources_matched": json.dumps([source_connector]),
        "dedup_status": "unique",
        "dedup_reason": "no_duplicate_signal",
    }


def write_discovery(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def copy_config(tmp_path: Path, *, max_documents: int, max_per_year: int) -> Path:
    payload = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    payload["batch"]["max_documents"] = max_documents
    payload["batch"]["max_per_year"] = max_per_year
    path = tmp_path / "configs" / "sources" / "indeci.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_repository_batch_policy_is_bounded_and_explicit() -> None:
    policy = load_batch_policy(SOURCE_CONFIG, allowed_root=Path.cwd())

    assert policy.max_documents == 25
    assert policy.max_per_year == 6
    assert policy.high_priority_terms == ("Chaclacayo",)
    assert "Lurigancho-Chosica" in policy.medium_priority_terms
    assert "Lima" in policy.regional_terms
    assert "movimiento en masa" in policy.event_terms


@pytest.mark.parametrize(
    ("title", "expected_tier"),
    [
        ("Huaico en Chaclacayo", "A"),
        ("Flujo de detritos en Lurigancho-Chosica", "B"),
        ("Lluvias intensas en Lima", "C"),
        ("Incendio urbano en Lima", "D"),
        ("Activación de quebrada sin ubicación", "D"),
    ],
)
def test_triage_tiers_prioritize_but_do_not_classify_relevance(
    title,
    expected_tier,
) -> None:
    policy = load_batch_policy(SOURCE_CONFIG, allowed_root=Path.cwd())

    result = score_title(title, policy=policy)

    assert result.tier == expected_tier
    assert result.score >= 0
    assert result.reasons


def test_batch_selection_is_deterministic_balanced_and_forces_goldens(
    tmp_path,
) -> None:
    config = copy_config(tmp_path, max_documents=11, max_per_year=2)
    discovery = tmp_path / "metadata" / "indeci" / "discovery.csv"
    rows = []
    index = 1
    for year in (2017, 2019, 2023, 2024):
        for suffix in range(5):
            rows.append(
                discovery_row(
                    index,
                    year=year,
                    title=f"Huaico en Chaclacayo muestra {year}-{suffix}",
                )
            )
            index += 1
    rows.append(
        discovery_row(
            index,
            year=2019,
            title="Huaico en Lurigancho y Chaclacayo",
            report_number="630",
            report_date=date(2019, 3, 3),
        )
    )
    index += 1
    rows.append(
        discovery_row(
            index,
            year=2023,
            title="Lluvias intensas en Lima",
            report_type="informe_emergencia",
            report_number="1496",
            report_date=date(2023, 5, 5),
            source_connector="seed_discovery",
        )
    )
    write_discovery(discovery, rows)
    output = tmp_path / BATCH_SELECTION_OUTPUT

    first = execute_select_batch(
        discovery,
        config_path=config,
        output_path=output,
        max_documents=11,
        allowed_root=tmp_path,
    )
    first_bytes = output.read_bytes()
    second = execute_select_batch(
        discovery,
        config_path=config,
        output_path=output,
        max_documents=11,
        allowed_root=tmp_path,
    )

    assert first == second
    assert output.read_bytes() == first_bytes
    assert first["documents_selected"] == 10
    assert first["selected_by_year"] == {
        "2017": 2,
        "2019": 3,
        "2023": 3,
        "2024": 2,
    }
    with output.open(newline="", encoding="utf-8") as source:
        selected = [row for row in csv.DictReader(source) if row["selected"] == "true"]
    controls = {row["golden_control"]: row for row in selected if row["golden_control"]}
    assert controls["positive_control"]["document_id"] == "INDECI_RC630_20190303"
    assert controls["negative_ambiguous_control"]["document_id"] == (
        "INDECI_IE1496_20230505"
    )
    assert {row["year"] for row in selected} == {"2017", "2019", "2023", "2024"}


def test_selection_rejects_limit_above_configured_maximum(tmp_path) -> None:
    config = copy_config(tmp_path, max_documents=5, max_per_year=2)
    discovery = tmp_path / "metadata" / "indeci" / "discovery.csv"
    write_discovery(discovery, [discovery_row(1, year=2019, title="Huaico")])

    with pytest.raises(BatchPolicyError, match="configured maximum"):
        execute_select_batch(
            discovery,
            config_path=config,
            output_path=tmp_path / BATCH_SELECTION_OUTPUT,
            max_documents=6,
            allowed_root=tmp_path,
        )
