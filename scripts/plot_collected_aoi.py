"""Render a presentation-ready summary of collector data stored in MongoDB.

This utility is read-only: it never creates indexes, writes database records,
or accesses CDSE. It only writes the requested PNG file.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from dotenv import load_dotenv
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from forecaster.storage import MongoMinioStore, StorageSettings


METRICS = ("WQI", "Chl_a", "Cya", "Turb")


def host_usable_mongo_uri(uri: str) -> str:
    """Use the existing local tunnel when the Docker-only hostname is configured."""
    parsed = urlsplit(uri)
    if parsed.hostname != "host.docker.internal":
        return uri
    return urlunsplit(parsed._replace(netloc=parsed.netloc.replace("host.docker.internal", "127.0.0.1")))


def load_records(aoi_id: str) -> tuple[list[dict], list[dict]]:
    load_dotenv(ROOT / ".env")
    os.environ["MONGO_URI"] = host_usable_mongo_uri(os.environ.get("MONGO_URI", ""))
    store = MongoMinioStore(StorageSettings.from_env(aoi_id=aoi_id))
    try:
        return store.load_observations(), store.load_tiles()
    finally:
        store.close()


def numeric_frame(records: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError("No collector observations were found for this AOI.")
    frame["observation_date"] = pd.to_datetime(frame["observation_date"], errors="coerce")
    frame = frame.dropna(subset=["observation_date", "tile_id"])
    for metric in METRICS:
        if metric in frame:
            frame[metric] = pd.to_numeric(frame[metric], errors="coerce")
    return frame


def render(aoi_id: str, output: Path) -> dict[str, object]:
    records, tiles = load_records(aoi_id)
    frame = numeric_frame(records)
    latest_date = frame["observation_date"].max()
    metric_columns = [metric for metric in METRICS if metric in frame]
    measured = frame[frame[metric_columns].notna().any(axis=1)] if metric_columns else frame.iloc[0:0]
    latest_measured_date = measured["observation_date"].max() if not measured.empty else None
    dates = frame.groupby("observation_date")["tile_id"].nunique().sort_index()
    latest = (
        measured[measured["observation_date"] == latest_measured_date].copy()
        if latest_measured_date is not None
        else frame.iloc[0:0].copy()
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    figure = plt.figure(figsize=(16, 11), constrained_layout=True)
    grid = figure.add_gridspec(2, 2, height_ratios=(1, 1.35))
    coverage_axis = figure.add_subplot(grid[0, 0])
    series_axis = figure.add_subplot(grid[0, 1])
    map_axis = figure.add_subplot(grid[1, :])

    coverage_axis.bar(dates.index, dates.values, width=3.2, color="#2e6f95")
    coverage_axis.set_title("Collected tile coverage by acquisition date")
    coverage_axis.set_ylabel("Tiles with an observation")
    coverage_axis.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    coverage_axis.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    coverage_axis.grid(axis="y", alpha=0.25)

    plotted = 0
    for metric in METRICS:
        if metric not in frame or not frame[metric].notna().any():
            continue
        grouped = frame.groupby("observation_date")[metric].median().dropna()
        series_axis.plot(grouped.index, grouped.values, marker="o", markersize=3, linewidth=1.6, label=f"Median {metric}")
        plotted += 1
    series_axis.set_title("Median collected water-quality metrics across tiles")
    series_axis.set_ylabel("Metric value (native units)")
    series_axis.xaxis.set_major_locator(mdates.MonthLocator(interval=1))
    series_axis.xaxis.set_major_formatter(mdates.DateFormatter("%b\n%Y"))
    series_axis.grid(alpha=0.25)
    if plotted:
        series_axis.legend(fontsize=9, frameon=False)
    else:
        series_axis.text(0.5, 0.5, "No numeric metrics available", ha="center", va="center", transform=series_axis.transAxes)

    map_metric = next(
        (metric for metric in METRICS if metric in latest and latest[metric].notna().any()),
        None,
    )
    tile_by_id = {str(tile.get("tile_id") or tile.get("name")): tile for tile in tiles}
    patches, values = [], []
    for tile_id, row in latest.set_index("tile_id").iterrows():
        tile = tile_by_id.get(str(tile_id))
        bbox = tile.get("bbox") if tile else row.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        west, south, east, north = [float(item) for item in bbox]
        patches.append(Rectangle((west, south), east - west, north - south))
        values.append(float(row[map_metric]) if map_metric and pd.notna(row[map_metric]) else float("nan"))
    if patches:
        collection = PatchCollection(patches, edgecolor="#263238", linewidth=0.25, cmap="viridis")
        collection.set_array(pd.Series(values))
        map_axis.add_collection(collection)
        map_axis.autoscale()
        if map_metric and any(pd.notna(values)):
            figure.colorbar(collection, ax=map_axis, label=f"Latest observed {map_metric}")
    map_axis.set_aspect("equal", adjustable="box")
    metric_label = map_metric or "no numeric metric available"
    map_date_label = latest_measured_date.date().isoformat() if latest_measured_date is not None else "n/a"
    map_axis.set_title(f"Latest collected {metric_label} by tile — {map_date_label}")
    map_axis.set_xlabel("Longitude")
    map_axis.set_ylabel("Latitude")
    map_axis.grid(alpha=0.2)

    figure.suptitle(
        f"TERRA UC1 collector data — {aoi_id}\n"
        f"{len(frame):,} records | {frame['tile_id'].nunique()} tiles | "
        f"{frame['observation_date'].min().date().isoformat()} to {latest_date.date().isoformat()}",
        fontsize=16,
        fontweight="bold",
    )
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)
    return {"records": len(frame), "tiles": int(frame["tile_id"].nunique()), "latest_date": latest_date.date().isoformat()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Plot collector observations stored for an AOI.")
    parser.add_argument("--aoi-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(render(args.aoi_id, args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
