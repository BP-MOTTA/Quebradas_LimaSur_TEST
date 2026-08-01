from pathlib import Path

from quebradas.acquisition.aoi import load_aoi
from quebradas.acquisition.config import load_config
from quebradas.acquisition.filters import build_catalog
from quebradas.acquisition.stac_client import load_fixture_items


def test_sentinel1_filters_accept_and_reject_expected_scenes() -> None:
    config = load_config(Path("configs/pilots/cusipata_20230316.yaml"))
    aoi = load_aoi(Path("tests/fixtures/aoi_test.geojson"))
    rows = build_catalog(
        load_fixture_items(Path("tests/fixtures/sentinel1_items.json")),
        config,
        aoi,
        "sentinel1",
    )
    by_id = {row["scene_id"]: row for row in rows}

    assert by_id["S1A_PRE_GOOD"]["accepted"] is True
    assert by_id["S1A_POST_GOOD"]["accepted"] is True
    assert by_id["S1A_PRE_GOOD"]["temporal_group"] == "pre_event"
    assert by_id["S1A_POST_GOOD"]["temporal_group"] == "post_event"
    assert "insufficient_aoi_coverage" in by_id["S1A_PRE_PARTIAL"]["rejected_reason"]
    assert "missing_required_polarizations" in by_id["S1A_PRE_POL_MISSING"]["rejected_reason"]
    assert "outside_temporal_window" in by_id["S1A_EVENT_DAY"]["rejected_reason"]


def test_sentinel2_filters_cloud_and_coverage() -> None:
    config = load_config(Path("configs/pilots/cusipata_20230316.yaml"))
    aoi = load_aoi(Path("tests/fixtures/aoi_test.geojson"))
    rows = build_catalog(
        load_fixture_items(Path("tests/fixtures/sentinel2_items.json")),
        config,
        aoi,
        "sentinel2",
    )
    by_id = {row["scene_id"]: row for row in rows}

    assert by_id["S2A_PRE_GOOD"]["accepted"] is True
    assert by_id["S2A_POST_GOOD"]["accepted"] is True
    assert "cloud_cover_too_high" in by_id["S2A_PRE_CLOUDY"]["rejected_reason"]
    assert "insufficient_aoi_coverage" in by_id["S2A_POST_PARTIAL"]["rejected_reason"]
    assert "missing_cloud_cover" in by_id["S2A_PRE_MISSING_CLOUD"]["rejected_reason"]
