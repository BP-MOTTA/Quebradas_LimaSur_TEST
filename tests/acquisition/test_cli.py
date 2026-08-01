from pathlib import Path

import pandas as pd
import yaml
from typer.testing import CliRunner

from quebradas.acquisition.cli import app


def _offline_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/pilots/cusipata_20230316.yaml").read_text(encoding="utf-8"))
    raw["outputs"] = {
        "sentinel1_catalog": str(tmp_path / "sentinel1_candidates.csv"),
        "sentinel2_catalog": str(tmp_path / "sentinel2_candidates.csv"),
        "scene_pairs": str(tmp_path / "scene_pairs.csv"),
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_cli_offline_generates_three_csv_files(tmp_path: Path) -> None:
    config_path = _offline_config(tmp_path)
    result = CliRunner().invoke(
        app,
        ["catalog", "--config", str(config_path), "--offline-fixtures"],
    )

    assert result.exit_code == 0, result.output
    for name in ["sentinel1_candidates.csv", "sentinel2_candidates.csv", "scene_pairs.csv"]:
        assert (tmp_path / name).exists()

    sentinel1 = pd.read_csv(tmp_path / "sentinel1_candidates.csv")
    sentinel2 = pd.read_csv(tmp_path / "sentinel2_candidates.csv")
    pairs = pd.read_csv(tmp_path / "scene_pairs.csv")
    assert set(sentinel1["accepted"]) == {True, False}
    assert set(sentinel2["accepted"]) == {True, False}
    assert len(pairs) == 1
    assert bool(pairs.loc[0, "recommended"]) is True


def test_cli_real_mode_fails_when_production_aoi_is_missing(tmp_path: Path) -> None:
    config_path = _offline_config(tmp_path)
    result = CliRunner().invoke(app, ["catalog", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "AOI GeoJSON not found" in result.output
