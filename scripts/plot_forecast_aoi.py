"""Render a read-only visual summary of a stored forecaster run."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.plot_collected_aoi import load_records


def load_forecasts(aoi_id: str, forecast_run_id: str) -> list[dict]:
    """Read forecast documents without creating indexes or modifying storage."""
    from scripts.plot_collected_aoi import host_usable_mongo_uri
    from dotenv import load_dotenv
    import os
    from forecaster.storage import MongoMinioStore, StorageSettings

    load_dotenv(ROOT / ".env")
    os.environ["MONGO_URI"] = host_usable_mongo_uri(os.environ.get("MONGO_URI", ""))
    store = MongoMinioStore(StorageSettings.from_env(aoi_id=aoi_id))
    try:
        return [
            {key: value for key, value in row.items() if key != "_id"}
            for row in store.database["forecasts"].find(
                {"aoi_id": aoi_id, "forecast_run_id": forecast_run_id}
            ).sort([("step", 1), ("tile_id", 1)])
        ]
    finally:
        store.close()


def tile_map(axis, tiles: list[dict], rows: pd.DataFrame, title: str) -> None:
    by_tile = rows.set_index("tile_id")
    patches, values = [], []
    for tile in tiles:
        tile_id = str(tile.get("tile_id") or tile.get("name"))
        bbox = tile.get("bbox")
        if tile_id not in by_tile.index or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        west, south, east, north = [float(value) for value in bbox]
        patches.append(Rectangle((west, south), east - west, north - south))
        values.append(float(by_tile.loc[tile_id, "WQI"]))
    collection = PatchCollection(patches, edgecolor="#263238", linewidth=0.25, cmap="viridis")
    collection.set_array(pd.Series(values))
    axis.add_collection(collection)
    axis.autoscale()
    axis.set_aspect("equal", adjustable="box")
    axis.set_title(title)
    axis.set_xlabel("Longitude")
    axis.set_ylabel("Latitude")
    axis.grid(alpha=0.2)
    return collection


def render(aoi_id: str, forecast_run_id: str, output: Path) -> dict[str, object]:
    observations, tiles = load_records(aoi_id)
    forecasts = load_forecasts(aoi_id, forecast_run_id)
    if not forecasts:
        raise ValueError(f"No stored forecasts found for {aoi_id!r}, run {forecast_run_id!r}.")
    history = pd.DataFrame(observations)
    forecast = pd.DataFrame(forecasts)
    history["observation_date"] = pd.to_datetime(history["observation_date"], errors="coerce")
    history["WQI"] = pd.to_numeric(history["WQI"], errors="coerce")
    history = history.dropna(subset=["observation_date", "WQI"])
    forecast["forecast_date"] = pd.to_datetime(forecast["forecast_date"], errors="coerce")
    forecast["WQI"] = pd.to_numeric(forecast["WQI"], errors="coerce")
    forecast = forecast.dropna(subset=["forecast_date", "WQI"])
    if forecast.empty:
        raise ValueError("Stored forecast rows contain no numeric WQI values.")

    observed = history.groupby("observation_date")["WQI"].median().sort_index().tail(15)
    projected = forecast.groupby("forecast_date")["WQI"].median().sort_index()
    figure = plt.figure(figsize=(16, 10), constrained_layout=True)
    grid = figure.add_gridspec(2, 2, height_ratios=(0.8, 1.2))
    trend_axis = figure.add_subplot(grid[0, :])
    first_axis = figure.add_subplot(grid[1, 0])
    third_axis = figure.add_subplot(grid[1, 1])

    trend_axis.plot(observed.index, observed.values, "o-", color="#2463a5", label="Observed median WQI")
    trend_axis.plot(projected.index, projected.values, "o--", color="#d97706", linewidth=2.4, label="Forecast median WQI")
    trend_axis.axvline(projected.index.min(), color="#6b7280", linestyle=":", linewidth=1.2, label="Forecast horizon")
    trend_axis.set_title("Observed and forecast median WQI across Sperchios tiles")
    trend_axis.set_xlabel("Date")
    trend_axis.set_ylabel("WQI")
    trend_axis.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%Y"))
    trend_axis.grid(alpha=0.25)
    trend_axis.legend(frameon=False, ncol=3)

    step_one = forecast[forecast["step"] == 1]
    step_three = forecast[forecast["step"] == 3]
    first = tile_map(first_axis, tiles, step_one, f"Step 1 forecast WQI — {step_one['forecast_date'].iloc[0].date().isoformat()}")
    third = tile_map(third_axis, tiles, step_three, f"Step 3 forecast WQI — {step_three['forecast_date'].iloc[0].date().isoformat()}")
    figure.colorbar(first, ax=first_axis, label="Forecast WQI")
    figure.colorbar(third, ax=third_axis, label="Forecast WQI")
    anchor = str(forecast["anchor_date"].iloc[0])[:10]
    figure.suptitle(
        f"TERRA UC1 forecast — {aoi_id}\n"
        f"Anchor {anchor} | {len(forecast):,} forecast rows | {forecast['tile_id'].nunique()} tiles | 3 forecast steps",
        fontsize=16,
        fontweight="bold",
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    return {"forecast_rows": len(forecast), "tiles": int(forecast["tile_id"].nunique()), "dates": [value.date().isoformat() for value in projected.index]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot stored forecast results for an AOI.")
    parser.add_argument("--aoi-id", required=True)
    parser.add_argument("--forecast-run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.aoi_id, args.forecast_run_id, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
