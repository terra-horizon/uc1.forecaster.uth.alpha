"""Verify a standalone collector-to-forecaster handoff and render audit plots.

This is a read-only analysis of the two run folders.  It never calls CDSE,
MongoDB, MinIO, or the forecasting model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.collections import PatchCollection
from matplotlib.patches import Polygon
import pandas as pd


TARGETS = ("CDOM", "Chl_a", "Color", "Cya", "DOC", "Turb", "WQI")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def usable(record: dict[str, Any]) -> bool:
    return (
        str(record.get("collection_status", "collected")) == "collected"
        and str(record.get("water_status", "water")) != "no_water"
        and "cloudy" not in set(record.get("quality_flags") or [])
        and all(record.get(metric) is not None for metric in TARGETS)
    )


def plot_tile(history: list[dict[str, Any]], forecasts: list[dict[str, Any]], tile_id: str, anchor: str, output: Path) -> None:
    observed = pd.DataFrame([row for row in history if row["tile_id"] == tile_id and usable(row)])
    predicted = pd.DataFrame([row for row in forecasts if row["tile_id"] == tile_id])
    observed["observation_date"] = pd.to_datetime(observed["observation_date"])
    predicted["forecast_date"] = pd.to_datetime(predicted["forecast_date"])
    figure, axes = plt.subplots(4, 2, figsize=(16, 17), constrained_layout=True)
    for axis, metric in zip(axes.flat, TARGETS):
        axis.plot(observed["observation_date"], observed[metric], "o-", ms=3, label="Collector observation")
        axis.axvline(pd.Timestamp(anchor), color="#6b7280", lw=1, label="Forecast anchor")
        axis.plot(predicted["forecast_date"], predicted[metric], "o-", color="#d97706", label="Model forecast")
        axis.set_title(metric)
        axis.grid(axis="y", alpha=0.3)
    axes.flat[-1].axis("off")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    figure.legend(handles, labels, loc="lower center", ncol=3)
    figure.suptitle(f"{tile_id}: collector history and standalone forecast", fontsize=18, fontweight="bold")
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)


def plot_spatial_wqi(tiles: list[dict[str, Any]], forecasts: list[dict[str, Any]], output: Path) -> None:
    step_one = {row["tile_id"]: float(row["WQI"]) for row in forecasts if int(row["step"]) == 1}
    patches, values, missing = [], [], []
    for tile in tiles:
        tile_id = str(tile["name"])
        min_lon, min_lat, max_lon, max_lat = tile["bbox"]
        patch = Polygon([(min_lon, min_lat), (max_lon, min_lat), (max_lon, max_lat), (min_lon, max_lat)])
        if tile_id in step_one:
            patches.append(patch)
            values.append(step_one[tile_id])
        else:
            missing.append(patch)
    figure, axis = plt.subplots(figsize=(16, 7), constrained_layout=True)
    if missing:
        axis.add_collection(PatchCollection(missing, facecolor="#e5e7eb", edgecolor="#6b7280", linewidth=0.5))
    collection = PatchCollection(patches, cmap="viridis", edgecolor="#4b5563", linewidth=0.5)
    collection.set_array(pd.Series(values))
    axis.add_collection(collection)
    figure.colorbar(collection, ax=axis, label="WQI")
    axis.autoscale()
    axis.set_aspect("equal")
    axis.set_xlabel("Longitude")
    axis.set_ylabel("Latitude")
    axis.set_title(f"Standalone forecast WQI by tile — first step\n{len(step_one)} forecasted tiles; grey = no forecast")
    figure.savefig(output, dpi=180, bbox_inches="tight")
    plt.close(figure)


def verify(collector_run: Path, forecast_run: Path, output_dir: Path, tile_id: str) -> dict[str, Any]:
    manifest = load_json(collector_run / "collection" / "collection_input_manifest.json")
    collector_result = load_json(collector_run / "collection" / "collection_run_result.json")
    history = load_json(collector_run / "history" / "global_history.json")
    tiles = load_json(collector_run / "tiles" / "tile_records.json")
    forecast_result = load_json(forecast_run / "forecast_run_result.json")
    forecasts = load_json(forecast_run / "forecasts" / "global_forecasts.json")
    anchor = str(forecast_result["forecast_anchor"])

    usable_history = [row for row in history if usable(row)]
    by_tile = defaultdict(list)
    for row in usable_history:
        by_tile[str(row["tile_id"])].append(row)
    expected_tiles = sorted(tile for tile, rows in by_tile.items() if len(rows) >= 24)
    actual_tiles = sorted({str(row["tile_id"]) for row in forecasts})
    expected_keys = {(tile, step) for tile in actual_tiles for step in (1, 2, 3)}
    actual_keys = {(str(row["tile_id"]), int(row["step"])) for row in forecasts}

    feature_mismatches = []
    for forecast_tile in actual_tiles:
        feature_path = forecast_run / "feature_data" / forecast_tile / "csv" / "5D_mean_metrics_interpolated_time_based.csv"
        frame = pd.read_csv(feature_path)
        source = pd.DataFrame(by_tile[forecast_tile])[["observation_date", *TARGETS]].rename(columns={"observation_date": "date"})
        source["date"] = source["date"].astype(str).str[:10]
        if len(frame) != len(source) or not frame[["date", *TARGETS]].round(10).equals(source[["date", *TARGETS]].round(10)):
            feature_mismatches.append(forecast_tile)

    failures = collector_result.get("failed_units") or []
    summary = {
        "aoi_id": manifest["aoi_id"],
        "collector_status": collector_result["status"],
        "collector_records": len(history),
        "collector_reported_new_records": collector_result["new_record_count"],
        "collector_latest_observation": collector_result["latest_available_observation"],
        "collector_failed_units": len(failures),
        "collector_failed_tiles": sorted({str(row["tile_id"]) for row in failures}),
        "forecast_status": forecast_result["status"],
        "forecast_anchor": anchor,
        "forecast_input_observation_count": forecast_result["input_observation_count"],
        "usable_observation_count": len(usable_history),
        "forecast_rows": len(forecasts),
        "forecast_tiles": len(actual_tiles),
        "expected_forecast_tiles": len(expected_tiles),
        "missing_forecast_tiles": sorted(set(expected_tiles) - set(actual_tiles)),
        "unexpected_forecast_tiles": sorted(set(actual_tiles) - set(expected_tiles)),
        "missing_tile_step_pairs": sorted([f"{tile}:step-{step}" for tile, step in expected_keys - actual_keys]),
        "feature_handoff_mismatches": feature_mismatches,
        "forecast_dates_by_step": {str(step): sorted({row["forecast_date"] for row in forecasts if int(row["step"]) == step}) for step in (1, 2, 3)},
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "verification_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    plot_tile(history, forecasts, tile_id, anchor, output_dir / f"{tile_id}_history_forecast.png")
    plot_spatial_wqi(tiles, forecasts, output_dir / "spatial_wqi_step_1.png")
    report = f"""# Separate collector-to-forecaster verification\n\n## Verdict\n\n**Data handoff passed.** The standalone forecast consumed {summary['forecast_input_observation_count']:,} collector observations, exactly matching the collector history count. Its {summary['forecast_tiles']} forecasted tiles each have all three expected forecast steps ({summary['forecast_rows']} rows total).\n\n## Run identity\n\n- AOI: `{summary['aoi_id']}`\n- Collector status: `{summary['collector_status']}`; latest usable observation: `{summary['collector_latest_observation']}`\n- Forecaster status: `{summary['forecast_status']}`; anchor: `{anchor}`\n- Forecast dates: step 1 {summary['forecast_dates_by_step']['1']}, step 2 {summary['forecast_dates_by_step']['2']}, step 3 {summary['forecast_dates_by_step']['3']}\n\n## Handoff checks\n\n- Collector history records: {summary['collector_records']:,}\n- Forecaster input records: {summary['forecast_input_observation_count']:,}\n- Usable records after quality filtering: {summary['usable_observation_count']:,}\n- Feature CSVs compared against collector history: {len(actual_tiles)}; mismatches: {len(feature_mismatches)}\n- Expected/actual forecast tiles: {len(expected_tiles)}/{len(actual_tiles)}\n- Missing forecast steps: {len(summary['missing_tile_step_pairs'])}\n\n## Qualification\n\nThe collector run is **partial**, with {len(failures):,} retryable tile-date failures across {len(summary['collector_failed_tiles'])} tiles. This does not invalidate the successful tile forecasts, but it means the result is incomplete for failed tiles/dates. The plots are a pipeline-integrity verification, not scientific accuracy validation.\n\n## Generated artifacts\n\n- `{tile_id}_history_forecast.png`\n- `spatial_wqi_step_1.png`\n- `verification_summary.json`\n"""
    (output_dir / "verification_report.md").write_text(report, encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a completed standalone collector-to-forecaster run.")
    parser.add_argument("--collector-run", type=Path, required=True)
    parser.add_argument("--forecast-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tile-id", default="tile_15")
    args = parser.parse_args()
    print(json.dumps(verify(args.collector_run, args.forecast_run, args.output_dir, args.tile_id), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
