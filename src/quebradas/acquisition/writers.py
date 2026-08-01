from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from quebradas.acquisition.schemas import PAIR_FIELDS, SENTINEL1_FIELDS, SENTINEL2_FIELDS


def write_sentinel1_catalog(rows: list[dict[str, Any]], path: Path) -> None:
    _write_rows(rows, path, SENTINEL1_FIELDS)


def write_sentinel2_catalog(rows: list[dict[str, Any]], path: Path) -> None:
    _write_rows(rows, path, SENTINEL2_FIELDS)


def write_scene_pairs(rows: list[dict[str, Any]], path: Path) -> None:
    _write_rows(rows, path, PAIR_FIELDS)


def _write_rows(rows: list[dict[str, Any]], path: Path, fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows = [{field: _csv_value(row.get(field)) for field in fields} for row in rows]
    pd.DataFrame(normalized_rows, columns=fields).to_csv(path, index=False)


def _csv_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value
