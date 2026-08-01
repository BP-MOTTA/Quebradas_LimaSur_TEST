from pathlib import Path

import pytest
import yaml

from quebradas.acquisition.config import ConfigError, load_config

CONFIG_PATH = Path("configs/pilots/cusipata_20230316.yaml")


def test_load_config_valid() -> None:
    config = load_config(CONFIG_PATH)

    assert config.project.pilot_id == "CUSIPATA_20230316"
    assert config.event.date.isoformat() == "2023-03-16"
    assert config.sentinel1.collection == "sentinel-1-grd"
    assert config.sentinel2.maximum_cloud_cover == 30


def test_load_config_rejects_invalid_temporal_windows(tmp_path: Path) -> None:
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    raw["search"]["pre_end"] = "2023-03-16"
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigError, match="pre_end"):
        load_config(path)
