from pathlib import Path

from quebradas.acquisition.aoi import load_aoi
from quebradas.acquisition.config import load_config
from quebradas.acquisition.filters import build_catalog
from quebradas.acquisition.pairing import build_sentinel1_pairs, sentinel1_compatible
from quebradas.acquisition.stac_client import load_fixture_items


def _sentinel1_rows() -> list[dict]:
    config = load_config(Path("configs/pilots/cusipata_20230316.yaml"))
    aoi = load_aoi(Path("tests/fixtures/aoi_test.geojson"))
    return build_catalog(
        load_fixture_items(Path("tests/fixtures/sentinel1_items.json")),
        config,
        aoi,
        "sentinel1",
    )


def test_pairing_keeps_compatible_pre_post_pair() -> None:
    config = load_config(Path("configs/pilots/cusipata_20230316.yaml"))
    pairs = build_sentinel1_pairs(_sentinel1_rows(), config)

    assert len(pairs) == 1
    assert pairs[0]["pre_scene_id"] == "S1A_PRE_GOOD"
    assert pairs[0]["post_scene_id"] == "S1A_POST_GOOD"
    assert pairs[0]["recommended"] is True


def test_pairing_rejects_incompatible_relative_orbit() -> None:
    config = load_config(Path("configs/pilots/cusipata_20230316.yaml"))
    rows = {row["scene_id"]: row for row in _sentinel1_rows()}

    compatible = sentinel1_compatible(
        rows["S1A_PRE_GOOD"],
        rows["S1A_POST_ORBIT_MISMATCH"],
        config,
    )
    assert compatible is False
