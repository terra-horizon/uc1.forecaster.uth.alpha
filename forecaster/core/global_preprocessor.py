from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler

WATER_TARGET_COLS = [
    "CDOM",
    "Chl_a",
    "Color",
    "Cya",
    "DOC",
    "Turb",
    "WQI",
]

TARGET_COLS = [
    "CDOM",
    "Chl_a",
    "Color",
    "Cya",
    "DOC",
    "Turb",
    "WQI",
    "shortwave_radiation_sum",
    "temperature_2m_mean",
    "rain_sum",
    "s3_surface_temperature",
    "et0_fao_evapotranspiration",
    "wind_speed_10m_max",
]

NON_NEGATIVE_COLS = {
    "CDOM",
    "Chl_a",
    "Color",
    "Cya",
    "DOC",
    "Turb",
    "shortwave_radiation_sum",
    "rain_sum",
    "et0_fao_evapotranspiration",
    "wind_speed_10m_max",
}


NO_OPENMETEO_TARGET_COLS = list(WATER_TARGET_COLS)

SPATIAL_FEATURE_NAMES = [
    "min_lon",
    "min_lat",
    "max_lon",
    "max_lat",
    "centroid_lon",
    "centroid_lat",
    "tile_area_m2",
    "water_score_pct",
]


def _bbox_area_m2(bbox: list[float] | tuple[float, ...]) -> float:
    min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
    lat_mid = np.deg2rad((min_lat + max_lat) / 2.0)
    meters_per_degree_lat = 111_320.0
    meters_per_degree_lon = 111_320.0 * np.cos(lat_mid)
    width_m = abs(max_lon - min_lon) * meters_per_degree_lon
    height_m = abs(max_lat - min_lat) * meters_per_degree_lat
    return float(width_m * height_m)


def spatial_context_from_manifest(
    manifest: dict,
    tiles: list[str] | tuple[str, ...] | None = None,
) -> dict[str, np.ndarray]:
    """Build continuous spatial descriptors from a water-selection manifest."""
    requested_tiles = set(tiles or [])
    contexts: dict[str, np.ndarray] = {}
    for record in manifest.get("tiles") or []:
        tile = str(record.get("name", ""))
        if not tile or (requested_tiles and tile not in requested_tiles):
            continue
        bbox = record.get("bbox")
        if not bbox or len(bbox) != 4:
            raise ValueError(f"Water-selection manifest tile {tile!r} has no valid bbox.")
        min_lon, min_lat, max_lon, max_lat = [float(value) for value in bbox]
        centroid_lon = (min_lon + max_lon) / 2.0
        centroid_lat = (min_lat + max_lat) / 2.0
        size = record.get("size")
        tile_area_m2 = float(size) ** 2 if size not in (None, "") else _bbox_area_m2(bbox)
        water_score_pct = float(record.get("water_score_pct", 0.0))
        contexts[tile] = np.asarray(
            [
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                centroid_lon,
                centroid_lat,
                tile_area_m2,
                water_score_pct,
            ],
            dtype=np.float32,
        )

    missing = sorted(requested_tiles - set(contexts))
    if missing:
        raise ValueError(f"Water-selection manifest is missing spatial context for tiles: {', '.join(missing)}")
    return contexts


def _parse_dates(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, format="%Y-%m-%d", errors="coerce")
    if parsed.isna().any():
        fallback = pd.to_datetime(values, dayfirst=True, errors="coerce")
        parsed = parsed.fillna(fallback)
    return parsed


def prepare_raw_frame(
    csv_path: str | Path,
    target_cols: list[str] | tuple[str, ...] | None = None,
) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, list[str], tuple[int, int]]:
    path = Path(csv_path)
    target_cols = list(target_cols or TARGET_COLS)
    data = pd.read_csv(path)
    if "date" not in data.columns:
        raise ValueError(f"{path} has no date column.")

    missing = [column for column in target_cols if column not in data.columns]
    if missing:
        raise ValueError(f"{path} is missing required target columns: {missing}")

    data["date"] = _parse_dates(data["date"])
    data = data.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    data["delta_days"] = data["date"].diff().dt.total_seconds().div(86400).fillna(0.0)
    data["delta_unit"] = (data["delta_days"] / 5.0).astype("float32")

    dates = pd.to_datetime(data["date"])
    doy = dates.dt.dayofyear.values.astype(np.float32)
    month = dates.dt.month.values.astype(np.float32)
    data["sin_doy"] = np.sin(2 * np.pi * doy / 365.25).astype(np.float32)
    data["cos_doy"] = np.cos(2 * np.pi * doy / 365.25).astype(np.float32)
    data["sin_month"] = np.sin(2 * np.pi * month / 12.0).astype(np.float32)
    data["cos_month"] = np.cos(2 * np.pi * month / 12.0).astype(np.float32)

    data[target_cols] = data[target_cols].interpolate(method="linear", limit_direction="both")
    data[target_cols] = data[target_cols].bfill().ffill().fillna(0.0)
    targets_raw = data[target_cols].values.astype(np.float32)

    df_base = data[target_cols]
    df_diff = df_base.diff().fillna(0.0)
    df_ma7 = df_base.rolling(window=7, min_periods=1).mean()
    df_r7 = (df_base - df_ma7).fillna(0.0)

    gpr_std_cols = [f"{column}_gpr_std" for column in target_cols]
    df_gpr = pd.DataFrame(0.0, index=data.index, columns=gpr_std_cols)
    available_gpr = [column for column in gpr_std_cols if column in data.columns]
    if available_gpr:
        df_gpr[available_gpr] = data[available_gpr].fillna(0.0)

    features_raw = pd.concat([df_base, df_diff, df_r7, df_gpr], axis=1).values.astype(np.float32)
    feature_names = (
        target_cols
        + [f"diff_{column}" for column in target_cols]
        + [f"r7_{column}" for column in target_cols]
        + gpr_std_cols
        + ["sin_doy", "cos_doy", "sin_month", "cos_month"]
        + [f"prev_{column}" for column in target_cols]
    )
    target_indices = (0, len(target_cols))
    return data, features_raw, targets_raw, feature_names, target_indices
