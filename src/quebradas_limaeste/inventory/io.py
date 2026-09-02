"""Local file IO for offline inventory runs."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from quebradas_limaeste.inventory.models import (
    InventoryManifest,
    InventoryRecord,
    InventoryValidationError,
)
from quebradas_limaeste.inventory.parser import MAX_HTML_INPUT_LENGTH

ALLOWED_SOURCE_SUFFIXES = frozenset({".html", ".htm", ".txt"})
CSV_FIELDNAMES = (
    "record_id",
    "source_name",
    "source_url",
    "document_url",
    "title",
    "published_date",
    "evidence_text",
    "spatial_precision",
    "requires_human_review",
    "warnings",
)


@dataclass(frozen=True)
class InventoryOutputPaths:
    inventory_json: Path
    inventory_csv: Path
    manifest_json: Path


def read_source_text(path: Path, *, max_bytes: int = MAX_HTML_INPUT_LENGTH) -> str:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        raise InventoryValidationError("PDF sources are not enabled")
    if suffix not in ALLOWED_SOURCE_SUFFIXES:
        raise InventoryValidationError("source must be an HTML or text file")
    if not source_path.is_file():
        raise InventoryValidationError(f"source file not found: {source_path}")
    if source_path.stat().st_size > max_bytes:
        raise InventoryValidationError("source file is too large")
    return source_path.read_text(encoding="utf-8")


def write_inventory_outputs(
    output_dir: Path,
    *,
    records: tuple[InventoryRecord, ...],
    manifest: InventoryManifest,
) -> InventoryOutputPaths:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)

    paths = InventoryOutputPaths(
        inventory_json=destination / "inventory.json",
        inventory_csv=destination / "inventory.csv",
        manifest_json=destination / "manifest.json",
    )

    record_dicts = [record.to_dict() for record in records]
    paths.inventory_json.write_text(
        json.dumps(record_dicts, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths.manifest_json.write_text(
        json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_inventory_csv(paths.inventory_csv, record_dicts)
    return paths


def _write_inventory_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field_name: _csv_value(row.get(field_name))
                    for field_name in CSV_FIELDNAMES
                }
            )


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return "|".join(str(item) for item in value)
    return str(value)
