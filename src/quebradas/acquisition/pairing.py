from __future__ import annotations

from datetime import date, datetime
from typing import Any

from quebradas.acquisition.config import PilotConfig
from quebradas.acquisition.schemas import PAIR_FIELDS


def build_sentinel1_pairs(rows: list[dict[str, Any]], config: PilotConfig) -> list[dict[str, Any]]:
    pre_rows = [
        row
        for row in rows
        if row.get("accepted") is True and row.get("temporal_group") == "pre_event"
    ]
    post_rows = [
        row
        for row in rows
        if row.get("accepted") is True and row.get("temporal_group") == "post_event"
    ]

    pairs: list[dict[str, Any]] = []
    for pre in pre_rows:
        for post in post_rows:
            if not sentinel1_compatible(pre, post, config):
                continue
            pair = _pair_row(pre, post, config.event.date)
            pairs.append(pair)

    pairs.sort(
        key=lambda pair: (
            pair["pre_days_to_event"],
            pair["post_days_after_event"],
            -pair["pair_aoi_coverage"],
            pair["pre_scene_id"] or "",
            pair["post_scene_id"] or "",
        )
    )
    for index, pair in enumerate(pairs, start=1):
        pair["rank"] = index
        pair["recommended"] = index == 1
    return [{field: pair.get(field) for field in PAIR_FIELDS} for pair in pairs]


def sentinel1_compatible(
    pre: dict[str, Any],
    post: dict[str, Any],
    config: PilotConfig,
) -> bool:
    if config.sentinel1.require_same_relative_orbit and not _same_when_available(
        pre.get("relative_orbit"), post.get("relative_orbit")
    ):
        return False
    if config.sentinel1.require_same_orbit_direction and not _same_when_available(
        pre.get("orbit_direction"), post.get("orbit_direction")
    ):
        return False
    if not _same_when_available(pre.get("instrument_mode"), post.get("instrument_mode")):
        return False

    pre_pols = _polarization_set(pre.get("polarizations"))
    post_pols = _polarization_set(post.get("polarizations"))
    if pre_pols is not None and post_pols is not None and pre_pols != post_pols:
        return False

    return True


def _pair_row(pre: dict[str, Any], post: dict[str, Any], event_date: date) -> dict[str, Any]:
    pre_datetime = _as_datetime(pre.get("acquisition_datetime"))
    post_datetime = _as_datetime(post.get("acquisition_datetime"))
    pre_days = (event_date - pre_datetime.date()).days
    post_days = (post_datetime.date() - event_date).days
    pre_coverage = float(pre.get("aoi_coverage") or 0)
    post_coverage = float(post.get("aoi_coverage") or 0)

    return {
        "sensor": "sentinel1",
        "pre_scene_id": pre.get("scene_id"),
        "post_scene_id": post.get("scene_id"),
        "pre_acquisition_datetime": pre_datetime,
        "post_acquisition_datetime": post_datetime,
        "pre_days_to_event": pre_days,
        "post_days_after_event": post_days,
        "relative_orbit": pre.get("relative_orbit") or post.get("relative_orbit"),
        "orbit_direction": pre.get("orbit_direction") or post.get("orbit_direction"),
        "instrument_mode": pre.get("instrument_mode") or post.get("instrument_mode"),
        "polarizations": pre.get("polarizations") or post.get("polarizations"),
        "pre_aoi_coverage": pre_coverage,
        "post_aoi_coverage": post_coverage,
        "pair_aoi_coverage": min(pre_coverage, post_coverage),
        "rank": None,
        "recommended": False,
    }


def _same_when_available(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return True
    return left == right


def _polarization_set(value: str | None) -> set[str] | None:
    if value is None:
        return None
    return {part.strip().upper() for part in value.split(",") if part.strip()}


def _as_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    raise TypeError(f"Pairing requires acquisition datetime, got {value!r}")
