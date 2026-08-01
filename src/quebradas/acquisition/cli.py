from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer

from quebradas.acquisition.aoi import AoiError, load_aoi
from quebradas.acquisition.config import ConfigError, PilotConfig, load_config
from quebradas.acquisition.filters import build_catalog
from quebradas.acquisition.pairing import build_sentinel1_pairs
from quebradas.acquisition.stac_client import load_fixture_items, search_items
from quebradas.acquisition.writers import (
    write_scene_pairs,
    write_sentinel1_catalog,
    write_sentinel2_catalog,
)

app = typer.Typer(no_args_is_help=True)


class CatalogError(RuntimeError):
    """Raised for controlled catalog command failures."""


@dataclass(frozen=True)
class CatalogResult:
    sentinel1_rows: list[dict[str, Any]]
    sentinel2_rows: list[dict[str, Any]]
    pairs: list[dict[str, Any]]
    outputs: dict[str, Path]


@app.callback()
def main() -> None:
    """Catalogo satelital para pilotos de quebradas."""


@app.command()
def catalog(
    config: Annotated[Path, typer.Option("--config", help="Ruta al YAML del piloto.")],
    sensor: Annotated[str, typer.Option("--sensor", help="sentinel1, sentinel2 o all.")] = "all",
    offline_fixtures: Annotated[
        bool,
        typer.Option(
            "--offline-fixtures",
            help="Usa fixtures locales y no realiza solicitudes de red.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="Muestra conteos por salida."),
    ] = False,
) -> None:
    try:
        result = run_catalog(
            config_path=config,
            sensor=sensor,
            offline_fixtures=offline_fixtures,
            verbose=verbose,
        )
    except (CatalogError, ConfigError, AoiError, RuntimeError, ValueError) as exc:
        typer.echo(f"catalog failed: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if verbose:
        typer.echo(f"sentinel1 rows: {len(result.sentinel1_rows)}")
        typer.echo(f"sentinel2 rows: {len(result.sentinel2_rows)}")
        typer.echo(f"scene pairs: {len(result.pairs)}")


def run_catalog(
    *,
    config_path: Path,
    sensor: str = "all",
    offline_fixtures: bool = False,
    verbose: bool = False,
) -> CatalogResult:
    selected_sensors = _selected_sensors(sensor)
    config = load_config(config_path)

    if offline_fixtures:
        project_root = _project_root()
        aoi_path = project_root / "tests" / "fixtures" / "aoi_test.geojson"
        aoi = load_aoi(
            aoi_path,
            target_crs=config.site.catalog_crs,
            default_crs=config.site.catalog_crs,
        )
    else:
        aoi_path = _resolve_project_path(config.site.aoi_path, config_path)
        aoi = load_aoi(
            aoi_path,
            target_crs=config.site.catalog_crs,
            default_crs=config.site.catalog_crs,
        )

    sentinel1_rows: list[dict[str, Any]] = []
    sentinel2_rows: list[dict[str, Any]] = []

    if "sentinel1" in selected_sensors and config.sentinel1.enabled:
        sentinel1_items = _items_for_sensor(
            config=config,
            sensor="sentinel1",
            offline_fixtures=offline_fixtures,
            intersects=aoi.intersects,
        )
        sentinel1_rows = build_catalog(sentinel1_items, config, aoi, "sentinel1")

    if "sentinel2" in selected_sensors and config.sentinel2.enabled:
        sentinel2_items = _items_for_sensor(
            config=config,
            sensor="sentinel2",
            offline_fixtures=offline_fixtures,
            intersects=aoi.intersects,
        )
        sentinel2_rows = build_catalog(sentinel2_items, config, aoi, "sentinel2")

    pairs = build_sentinel1_pairs(sentinel1_rows, config) if sentinel1_rows else []
    outputs = _output_paths(config, config_path)
    if "sentinel1" in selected_sensors:
        write_sentinel1_catalog(sentinel1_rows, outputs["sentinel1_catalog"])
    if "sentinel2" in selected_sensors:
        write_sentinel2_catalog(sentinel2_rows, outputs["sentinel2_catalog"])
    write_scene_pairs(pairs, outputs["scene_pairs"])

    if verbose:
        typer.echo(f"AOI source CRS: {aoi.source_crs.to_string()}")
        typer.echo(f"AOI catalog CRS: {aoi.catalog_crs.to_string()}")

    return CatalogResult(
        sentinel1_rows=sentinel1_rows,
        sentinel2_rows=sentinel2_rows,
        pairs=pairs,
        outputs=outputs,
    )


def _selected_sensors(sensor: str) -> set[str]:
    normalized = sensor.lower()
    if normalized == "all":
        return {"sentinel1", "sentinel2"}
    if normalized in {"sentinel1", "sentinel2"}:
        return {normalized}
    raise CatalogError("--sensor must be one of: sentinel1, sentinel2, all")


def _items_for_sensor(
    *,
    config: PilotConfig,
    sensor: str,
    offline_fixtures: bool,
    intersects: dict[str, Any],
) -> list[Any]:
    if offline_fixtures:
        fixture_name = "sentinel1_items.json" if sensor == "sentinel1" else "sentinel2_items.json"
        return load_fixture_items(_project_root() / "tests" / "fixtures" / fixture_name)

    sensor_config = config.sentinel1 if sensor == "sentinel1" else config.sentinel2
    items: list[Any] = []
    for start_date, end_date in (
        (config.search.pre_start, config.search.pre_end),
        (config.search.post_start, config.search.post_end),
    ):
        items.extend(
            search_items(
                collection=sensor_config.collection,
                start_date=start_date,
                end_date=end_date,
                intersects=intersects,
            )
        )
    return items


def _output_paths(config: PilotConfig, config_path: Path) -> dict[str, Path]:
    return {
        "sentinel1_catalog": _resolve_project_path(config.outputs.sentinel1_catalog, config_path),
        "sentinel2_catalog": _resolve_project_path(config.outputs.sentinel2_catalog, config_path),
        "scene_pairs": _resolve_project_path(config.outputs.scene_pairs, config_path),
    }


def _resolve_project_path(path_value: str, config_path: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path

    cwd_candidate = Path.cwd() / path
    if cwd_candidate.exists() or path.parts[:1] in {("data",), ("metadata",), ("tests",)}:
        return cwd_candidate

    return config_path.parent / path


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]
