from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

STAC_URL = "https://stac.dataspace.copernicus.eu/v1/"


def search_items(
    *,
    collection: str,
    start_date: date,
    end_date: date,
    intersects: dict[str, Any],
    stac_url: str = STAC_URL,
    max_items: int = 200,
) -> list[Any]:
    try:
        from pystac_client import Client
    except ImportError as exc:
        raise RuntimeError("pystac-client is required for real STAC catalog searches") from exc

    client = Client.open(stac_url)
    search = client.search(
        collections=[collection],
        intersects=intersects,
        datetime=f"{start_date.isoformat()}T00:00:00Z/{end_date.isoformat()}T23:59:59Z",
        max_items=max_items,
    )
    return list(search.items())


def load_fixture_items(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return payload
    if payload.get("type") in {"FeatureCollection", "ItemCollection"}:
        return payload.get("features", [])
    raise ValueError(f"Unsupported STAC fixture format: {path}")
