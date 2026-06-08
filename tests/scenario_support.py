from __future__ import annotations

import json
import shutil
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from forecast import AOIInferenceConfig, AOIInferencePipeline, PipelineExecutionError


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "tests" / "results"
TARGET_COLS = ["CDOM", "Chl_a", "Color", "Cya", "DOC", "Turb", "WQI"]


def scenario_dir(name: str) -> Path:
    path = RESULTS_ROOT / name / "latest"
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def tile_record(name: str, water_score: float = 25.0) -> dict:
    return {
        "name": name,
        "bbox": [22.45, 38.84, 22.455, 38.845],
        "size": 400,
        "water_score_pct": water_score,
        "selected": water_score > 0,
    }


def water_manifest(selected: list[str], all_tiles: list[str] | None = None) -> dict:
    names = all_tiles or selected
    return {
        "threshold": {"mode": "manual", "value_pct": 0.5},
        "selected_tiles": selected,
        "rejected_tiles": [name for name in names if name not in selected],
        "tiles": [tile_record(name, 25.0 if name in selected else 0.0) for name in names],
    }


def write_feature_csv(path: Path, rows: int = 30) -> str:
    start = date(2025, 12, 1)
    payload = []
    for index in range(rows):
        row = {"date": (start + timedelta(days=index * 5)).isoformat()}
        for offset, column in enumerate(TARGET_COLS, start=1):
            row[column] = float(offset + index * 0.05)
            row[f"{column}_gpr_std"] = 0.1
        payload.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(payload).to_csv(path, index=False)
    return str(path)


class FakeExtractor:
    tile_names = ["tile_0"]

    def __init__(self, config):
        self.config = config

    def extract_to_geojson(self, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
        return [object() for _ in self.tile_names], output_path


class EmptyExtractor(FakeExtractor):
    tile_names = []


class FakeSelector:
    manifest = water_manifest(["tile_0"])

    def __init__(self, **kwargs):
        self.cache_path = Path(kwargs["cache_path"])

    def select_tiles(self):
        self.cache_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        return self.manifest


def available_images(*_args, **_kwargs):
    return {
        "global": {
            "true_color": {
                "status": "available",
                "path": "target_date_images/global/global_true_color.png",
                "requested_date": "2026-05-27",
                "actual_date": "2026-05-27",
                "collection": "sentinel-2-l2a",
                "message": "Image saved.",
            }
        }
    }


def unavailable_images(*_args, **_kwargs):
    return {
        "global": {
            "true_color": {
                "status": "unavailable",
                "path": None,
                "requested_date": "2026-05-27",
                "actual_date": "N/A",
                "collection": "sentinel-2-l2a",
                "message": "No image data available for requested date.",
            }
        }
    }


def fake_forecast(pipeline: AOIInferencePipeline, feature_csvs: dict[str, str], failed: set[str] | None = None) -> dict:
    failed = failed or set()
    forecast_root = pipeline.run_dir / "forecasts"
    forecast_root.mkdir(parents=True, exist_ok=True)
    tiles = {
        name: ({"error": "Controlled inference failure.", "csv_path": csv_path} if name in failed else {"forecast": {"model": []}})
        for name, csv_path in feature_csvs.items()
    }
    forecast_json = forecast_root / "forecasts.json"
    forecast_csv = forecast_root / "forecasts.csv"
    forecast_json.write_text(json.dumps({"tiles": tiles}, indent=2), encoding="utf-8")
    forecast_csv.write_text("tile,date\n", encoding="utf-8")
    return {
        "forecast_json": str(forecast_json),
        "forecast_csv": str(forecast_csv),
        "plot_dir": None,
        "tiles": tiles,
    }


def execute_scenario(
    name: str,
    *,
    extractor=FakeExtractor,
    manifest: dict | None = None,
    feature_tiles: list[str] | None = None,
    image_downloads=available_images,
    inference_failures: set[str] | None = None,
    real_model: bool = False,
):
    output_root = scenario_dir(name)
    selector_manifest = manifest or water_manifest(["tile_0"])
    selected = selector_manifest.get("selected_tiles", [])
    features = {
        tile: write_feature_csv(output_root / "fixtures" / tile / "features.csv")
        for tile in (selected if feature_tiles is None else feature_tiles)
    }

    config = AOIInferenceConfig(
        aoi_bbox=[22.433493, 38.837552, 22.569555, 38.894223],
        target_date="2026-05-27",
        output_root=output_root,
        run_name="run",
        image_keys=("true_color",),
        plot=False,
    )
    pipeline = AOIInferencePipeline(config)

    class ScenarioSelector(FakeSelector):
        pass

    ScenarioSelector.manifest = selector_manifest
    with ExitStack() as stack:
        stack.enter_context(patch("forecast.RiverTileExtractor", extractor))
        stack.enter_context(patch("forecast.WaterTileSelector", ScenarioSelector))
        stack.enter_context(patch.object(AOIInferencePipeline, "download_target_date_images", image_downloads))
        stack.enter_context(patch.object(AOIInferencePipeline, "prepare_feature_csvs", return_value=features))
        if not real_model:
            stack.enter_context(
                patch.object(
                    AOIInferencePipeline,
                    "run_model_inference",
                    autospec=True,
                    side_effect=lambda instance, selected_records, feature_csvs, water_manifest: fake_forecast(
                        instance, feature_csvs, inference_failures
                    ),
                )
            )
        try:
            plan = pipeline.execute()
            error = None
        except PipelineExecutionError as exc:
            plan = None
            error = exc

    result = json.loads(pipeline.result_path.read_text(encoding="utf-8"))
    return pipeline, plan, result, error
