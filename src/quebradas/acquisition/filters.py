from __future__ import annotations

from datetime import date, datetime
from typing import Any

from shapely.geometry import shape

from quebradas.acquisition.aoi import Aoi
from quebradas.acquisition.config import PilotConfig
from quebradas.acquisition.coverage import coverage_fraction
from quebradas.acquisition.schemas import SENTINEL1_FIELDS, SENTINEL2_FIELDS

TemporalGroup = str | None


def build_catalog(
    items: list[Any],
    config: PilotConfig,
    aoi: Aoi,
    sensor: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in items:
        raw_item = _item_to_dict(item)
        row = normalize_item(raw_item, sensor)
        row["temporal_group"] = temporal_group(row["acquisition_datetime"], config)

        reasons: list[str] = []
        if row["temporal_group"] is None:
            reasons.append("outside_temporal_window")

        _add_coverage(row, raw_item, aoi, reasons)

        if sensor == "sentinel1":
            reasons.extend(_sentinel1_rejections(row, config))
            fields = SENTINEL1_FIELDS
        elif sensor == "sentinel2":
            reasons.extend(_sentinel2_rejections(row, config))
            fields = SENTINEL2_FIELDS
        else:
            raise ValueError(f"Unsupported sensor: {sensor}")

        row["accepted"] = not reasons
        row["rejected_reason"] = ";".join(reasons) if reasons else None
        rows.append({field: row.get(field) for field in fields})

    return rows


def normalize_item(item: dict[str, Any], sensor: str) -> dict[str, Any]:
    properties = item.get("properties", {}) or {}
    geometry = item.get("geometry")
    acquisition_datetime = _parse_datetime(
        properties.get("datetime") or properties.get("start_datetime")
    )

    base = {
        "scene_id": item.get("id"),
        "collection": item.get("collection"),
        "acquisition_datetime": acquisition_datetime,
        "platform": properties.get("platform"),
        "footprint_wkt": _geometry_wkt(geometry),
        "aoi_coverage": None,
        "temporal_group": None,
        "accepted": False,
        "rejected_reason": None,
        "stac_item_url": _self_href(item),
    }

    if sensor == "sentinel1":
        base.update(
            {
                "product_type": _first_present(
                    properties,
                    "sar:product_type",
                    "product_type",
                    "s1:product_type",
                ),
                "instrument_mode": _first_present(
                    properties,
                    "sar:instrument_mode",
                    "instrument_mode",
                    "s1:instrument_mode",
                ),
                "polarizations": _normalize_polarizations(
                    _first_present(properties, "sar:polarizations", "polarizations")
                ),
                "orbit_direction": _first_present(
                    properties,
                    "sat:orbit_state",
                    "orbit_direction",
                    "s1:orbit_state",
                ),
                "absolute_orbit": _first_present(
                    properties,
                    "sat:absolute_orbit",
                    "absolute_orbit",
                    "s1:absolute_orbit",
                ),
                "relative_orbit": _first_present(
                    properties,
                    "sat:relative_orbit",
                    "relative_orbit",
                    "s1:relative_orbit",
                ),
            }
        )
    elif sensor == "sentinel2":
        base.update(
            {
                "processing_level": _first_present(
                    properties,
                    "processing:level",
                    "processing_level",
                    "s2:processing_level",
                ),
                "mgrs_tile": _first_present(properties, "s2:mgrs_tile", "mgrs_tile", "mgrs:tile"),
                "cloud_cover": _first_present(properties, "eo:cloud_cover", "cloud_cover"),
            }
        )
    else:
        raise ValueError(f"Unsupported sensor: {sensor}")

    return base


def temporal_group(acquisition_datetime: datetime | None, config: PilotConfig) -> TemporalGroup:
    if acquisition_datetime is None:
        return None

    acquired_date = acquisition_datetime.date()
    if config.search.pre_start <= acquired_date <= config.search.pre_end:
        return "pre_event"
    if config.search.post_start <= acquired_date <= config.search.post_end:
        return "post_event"
    return None


def _add_coverage(
    row: dict[str, Any], item: dict[str, Any], aoi: Aoi, reasons: list[str]
) -> None:
    geometry = item.get("geometry")
    if geometry is None:
        reasons.append("missing_geometry")
        return

    try:
        scene_geometry = shape(geometry)
    except Exception:  # noqa: BLE001 - shapely reports geometry parsing issues inconsistently.
        reasons.append("invalid_geometry")
        return

    if scene_geometry.is_empty or not scene_geometry.is_valid:
        reasons.append("invalid_geometry")
        return

    row["aoi_coverage"] = coverage_fraction(scene_geometry, aoi.geometry_catalog)


def _sentinel1_rejections(row: dict[str, Any], config: PilotConfig) -> list[str]:
    reasons: list[str] = []

    if row["collection"] != config.sentinel1.collection:
        reasons.append("unexpected_collection")
    if row["acquisition_datetime"] is None:
        reasons.append("missing_acquisition_datetime")

    mode = row.get("instrument_mode")
    if mode is None:
        reasons.append("missing_instrument_mode")
    elif mode != config.sentinel1.acquisition_mode:
        reasons.append("instrument_mode_mismatch")

    polarizations = _polarization_set(row.get("polarizations"))
    required = {pol.upper() for pol in config.sentinel1.required_polarizations}
    if polarizations is None:
        reasons.append("missing_polarizations")
    elif not required.issubset(polarizations):
        reasons.append("missing_required_polarizations")

    coverage = row.get("aoi_coverage")
    if coverage is None:
        reasons.append("missing_aoi_coverage")
    elif coverage < config.sentinel1.minimum_aoi_coverage:
        reasons.append("insufficient_aoi_coverage")

    return reasons


def _sentinel2_rejections(row: dict[str, Any], config: PilotConfig) -> list[str]:
    reasons: list[str] = []

    if row["collection"] != config.sentinel2.collection:
        reasons.append("unexpected_collection")
    if row["acquisition_datetime"] is None:
        reasons.append("missing_acquisition_datetime")

    cloud_cover = row.get("cloud_cover")
    if cloud_cover is None:
        reasons.append("missing_cloud_cover")
    elif float(cloud_cover) > config.sentinel2.maximum_cloud_cover:
        reasons.append("cloud_cover_too_high")

    coverage = row.get("aoi_coverage")
    if coverage is None:
        reasons.append("missing_aoi_coverage")
    elif coverage < config.sentinel2.minimum_aoi_coverage:
        reasons.append("insufficient_aoi_coverage")

    return reasons


def _item_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        return item
    to_dict = getattr(item, "to_dict", None)
    if to_dict is None:
        raise TypeError(f"Unsupported STAC item type: {type(item)!r}")
    return to_dict()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    if isinstance(value, str):
        normalized = value.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _geometry_wkt(geometry: dict[str, Any] | None) -> str | None:
    if geometry is None:
        return None
    try:
        geom = shape(geometry)
    except Exception:  # noqa: BLE001
        return None
    if geom.is_empty or not geom.is_valid:
        return None
    return geom.wkt


def _self_href(item: dict[str, Any]) -> str | None:
    for link in item.get("links", []) or []:
        if link.get("rel") == "self":
            return link.get("href")
    return None


def _first_present(properties: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = properties.get(key)
        if value is not None:
            return value
    return None


def _normalize_polarizations(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [part.strip().upper() for part in value.replace(";", ",").split(",")]
    else:
        parts = [str(part).strip().upper() for part in value]
    parts = [part for part in parts if part]
    return ",".join(parts) if parts else None


def _polarization_set(value: str | None) -> set[str] | None:
    if value is None:
        return None
    return {part.strip().upper() for part in value.split(",") if part.strip()}
