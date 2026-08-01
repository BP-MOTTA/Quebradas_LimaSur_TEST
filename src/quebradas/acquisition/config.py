from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError


class ConfigError(ValueError):
    """Raised when the pilot YAML is incomplete or inconsistent."""


class ProjectConfig(BaseModel):
    name: str
    pilot_id: str


class SiteConfig(BaseModel):
    quebrada_id: str
    name: str
    district: str
    region: str
    working_crs: str
    catalog_crs: str
    aoi_path: str


class EventConfig(BaseModel):
    event_id: str
    event_type: str
    date: date


class SearchConfig(BaseModel):
    pre_start: date
    pre_end: date
    post_start: date
    post_end: date


class Sentinel1Config(BaseModel):
    enabled: bool
    collection: str
    acquisition_mode: str
    required_polarizations: list[str]
    minimum_aoi_coverage: float
    require_same_relative_orbit: bool
    require_same_orbit_direction: bool


class Sentinel2Config(BaseModel):
    enabled: bool
    collection: str
    maximum_cloud_cover: float
    minimum_aoi_coverage: float


class OutputsConfig(BaseModel):
    sentinel1_catalog: str
    sentinel2_catalog: str
    scene_pairs: str


class PilotConfig(BaseModel):
    project: ProjectConfig
    site: SiteConfig
    event: EventConfig
    search: SearchConfig
    sentinel1: Sentinel1Config
    sentinel2: Sentinel2Config
    outputs: OutputsConfig


def load_config(path: Path) -> PilotConfig:
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config YAML is invalid: {path}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"Config YAML must contain a mapping: {path}")

    try:
        config = _model_validate(PilotConfig, raw)
    except ValidationError as exc:
        raise ConfigError(f"Config does not match the expected schema: {exc}") from exc

    _validate_dates(config)
    _validate_thresholds(config)
    return config


def _model_validate(model: type[BaseModel], raw: dict[str, Any]) -> Any:
    validator = getattr(model, "model_validate", None)
    if validator is not None:
        return validator(raw)
    return model.parse_obj(raw)


def _validate_dates(config: PilotConfig) -> None:
    search = config.search
    event_date = config.event.date

    if search.pre_start > search.pre_end:
        raise ConfigError("search.pre_start must be on or before search.pre_end")
    if search.post_start > search.post_end:
        raise ConfigError("search.post_start must be on or before search.post_end")
    if search.pre_end >= event_date:
        raise ConfigError("search.pre_end must be before event.date")
    if search.post_start <= event_date:
        raise ConfigError("search.post_start must be after event.date")


def _validate_thresholds(config: PilotConfig) -> None:
    if not 0 <= config.sentinel1.minimum_aoi_coverage <= 1:
        raise ConfigError("sentinel1.minimum_aoi_coverage must be between 0 and 1")
    if not 0 <= config.sentinel2.minimum_aoi_coverage <= 1:
        raise ConfigError("sentinel2.minimum_aoi_coverage must be between 0 and 1")
    if not 0 <= config.sentinel2.maximum_cloud_cover <= 100:
        raise ConfigError("sentinel2.maximum_cloud_cover must be between 0 and 100")
    if not config.sentinel1.required_polarizations:
        raise ConfigError("sentinel1.required_polarizations must not be empty")
