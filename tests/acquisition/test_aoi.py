from pathlib import Path

import pytest
from pyproj import CRS

from quebradas.acquisition.aoi import AoiError, load_aoi


def test_missing_aoi_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(AoiError, match="AOI GeoJSON not found"):
        load_aoi(tmp_path / "missing.geojson")


def test_geojson_without_crs_defaults_to_catalog_crs() -> None:
    aoi = load_aoi(Path("tests/fixtures/aoi_test.geojson"))

    assert aoi.source_crs == CRS.from_epsg(4326)
    assert aoi.geometry_original.equals(aoi.geometry_catalog)


def test_invalid_geojson_geometry_raises(tmp_path: Path) -> None:
    path = tmp_path / "invalid.geojson"
    path.write_text(
        """
        {
          "type": "Feature",
          "geometry": {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 1], [1, 0], [0, 1], [0, 0]]]
          },
          "properties": {}
        }
        """,
        encoding="utf-8",
    )

    with pytest.raises(AoiError, match="empty or invalid"):
        load_aoi(path)


def test_reprojects_aoi_to_catalog_crs(tmp_path: Path) -> None:
    path = tmp_path / "utm_aoi.geojson"
    path.write_text(
        """
        {
          "type": "FeatureCollection",
          "crs": {"type": "name", "properties": {"name": "EPSG:32718"}},
          "features": [
            {
              "type": "Feature",
              "properties": {"fixture": true},
              "geometry": {
                "type": "Polygon",
                "coordinates": [[
                  [300000, 8670000],
                  [300100, 8670000],
                  [300100, 8670100],
                  [300000, 8670100],
                  [300000, 8670000]
                ]]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    aoi = load_aoi(path, target_crs="EPSG:4326")

    assert aoi.source_crs == CRS.from_epsg(32718)
    assert aoi.catalog_crs == CRS.from_epsg(4326)
    assert aoi.geometry_catalog.bounds != aoi.geometry_original.bounds
