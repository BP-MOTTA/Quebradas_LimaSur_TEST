from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pyproj import CRS, Transformer
from shapely.geometry import mapping, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union


class AoiError(ValueError):
    """Raised when an AOI GeoJSON cannot be used safely."""


@dataclass(frozen=True)
class Aoi:
    path: Path
    source_crs: CRS
    catalog_crs: CRS
    geometry_original: BaseGeometry
    geometry_catalog: BaseGeometry

    @property
    def intersects(self) -> dict[str, Any]:
        return mapping(self.geometry_catalog)


def load_aoi(
    path: Path,
    *,
    target_crs: str = "EPSG:4326",
    default_crs: str = "EPSG:4326",
) -> Aoi:
    if not path.exists():
        raise AoiError(f"AOI GeoJSON not found: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AoiError(f"AOI GeoJSON is not valid JSON: {path}") from exc

    geometry = _extract_geometry(payload)
    if geometry.is_empty or not geometry.is_valid:
        raise AoiError(f"AOI geometry is empty or invalid: {path}")

    source_crs = _extract_crs(payload, default_crs)
    catalog_crs = CRS.from_user_input(target_crs)
    if source_crs == catalog_crs:
        geometry_catalog = geometry
    else:
        transformer = Transformer.from_crs(source_crs, catalog_crs, always_xy=True)
        geometry_catalog = transform(transformer.transform, geometry)

    return Aoi(
        path=path,
        source_crs=source_crs,
        catalog_crs=catalog_crs,
        geometry_original=geometry,
        geometry_catalog=geometry_catalog,
    )


def _extract_geometry(payload: dict[str, Any]) -> BaseGeometry:
    geojson_type = payload.get("type")

    if geojson_type == "FeatureCollection":
        geometries = [
            shape(feature.get("geometry"))
            for feature in payload.get("features", [])
            if feature.get("geometry") is not None
        ]
        if not geometries:
            raise AoiError("AOI FeatureCollection does not contain geometries")
        return unary_union(geometries)

    if geojson_type == "Feature":
        raw_geometry = payload.get("geometry")
        if raw_geometry is None:
            raise AoiError("AOI Feature does not contain geometry")
        return shape(raw_geometry)

    if "coordinates" in payload:
        return shape(payload)

    raise AoiError("AOI GeoJSON must be a FeatureCollection, Feature, or geometry")


def _extract_crs(payload: dict[str, Any], default_crs: str) -> CRS:
    crs_payload = payload.get("crs")
    if crs_payload is None:
        return CRS.from_user_input(default_crs)

    try:
        if isinstance(crs_payload, str):
            return CRS.from_user_input(crs_payload)
        if crs_payload.get("type") == "name":
            name = crs_payload.get("properties", {}).get("name")
            return CRS.from_user_input(name)
    except Exception as exc:  # noqa: BLE001 - pyproj raises several CRS parsing errors.
        raise AoiError(f"AOI CRS is invalid: {crs_payload}") from exc

    raise AoiError(f"AOI CRS is unsupported: {crs_payload}")
