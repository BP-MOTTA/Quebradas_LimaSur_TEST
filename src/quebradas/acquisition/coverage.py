from __future__ import annotations

from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform


def coverage_fraction(
    scene_geometry: BaseGeometry,
    aoi_geometry: BaseGeometry,
    *,
    source_crs: str = "EPSG:4326",
    metric_crs: str | None = None,
) -> float:
    if scene_geometry.is_empty or aoi_geometry.is_empty:
        return 0.0

    metric = CRS.from_user_input(metric_crs) if metric_crs else estimate_metric_crs(aoi_geometry)
    source = CRS.from_user_input(source_crs)
    transformer = Transformer.from_crs(source, metric, always_xy=True)
    scene_metric = transform(transformer.transform, scene_geometry)
    aoi_metric = transform(transformer.transform, aoi_geometry)

    aoi_area = aoi_metric.area
    if aoi_area <= 0:
        raise ValueError("AOI area must be greater than zero to compute coverage")

    intersection_area = scene_metric.intersection(aoi_metric).area
    fraction = intersection_area / aoi_area
    return max(0.0, min(1.0, fraction))


def estimate_metric_crs(geometry_4326: BaseGeometry) -> CRS:
    centroid = geometry_4326.centroid
    lon = centroid.x
    lat = centroid.y
    zone = int((lon + 180) // 6) + 1
    zone = max(1, min(60, zone))
    epsg_base = 32600 if lat >= 0 else 32700
    return CRS.from_epsg(epsg_base + zone)
