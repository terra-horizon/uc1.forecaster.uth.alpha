from __future__ import annotations

import argparse
import ast
import json
import re
from dotenv import load_dotenv
load_dotenv()
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tensorflow as tf
from keras import ops as K

from hydro.river_tile_extractor import RiverTileExtractor, RiverTileExtractorConfig
from forecaster.core.global_preprocessor import (
    NON_NEGATIVE_COLS,
    SPATIAL_FEATURE_NAMES,
    prepare_raw_frame,
    spatial_context_from_manifest,
)

# Dummy losses for model loading to avoid missing references
class ForecastWeightedLoss(tf.keras.losses.Loss):
    def call(self, y_true, y_pred): return 0.0

class HorizonWeightedLoss(tf.keras.losses.Loss):
    def call(self, y_true, y_pred): return 0.0

from forecaster.data.collectors.sentinel2 import ImageCollection
from forecaster.data.collectors.sentinel2 import StatisticalCollection
from forecaster.data.data_augmentation_5d_v2 import DataAugmentation
from forecaster.models.multi_feature_model_v15 import HorizonVelocityScale
from forecaster.water_tile_selector import WaterTileSelector, print_selection_summary


REPO_ROOT = Path(__file__).resolve().parent
FORECASTER_DIR = REPO_ROOT / "forecaster"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "inference_runs"
DEFAULT_MODEL_ROOT = REPO_ROOT / "forecaster" / "models" / "default_model"

DEFAULT_IMAGE_KEYS = ("true_color", "chla", "cdom", "turb", "doc", "cya", "surface_temperature")
DEFAULT_FEATURE_CSV_NAME = "5D_mean_metrics_interpolated_time_based.csv"


@dataclass(frozen=True)
class ModelInferenceProfile:
    model_name: str = "H3_D5_matern_direct_v15_global_no_openmeteo"
    time_steps: int = 24
    horizon: int = 3
    cadence_days: int = 5
    lstm_units: int = 96
    dropout: float = 0.2
    weight_decay: float = 1e-5
    learning_rate: float = 3e-4
    batch_size: int = 32
    loss: str = "mse"
    uses_openmeteo: bool = False
    uses_tile_id: bool = False
    uses_spatial_context: bool = False


@dataclass(frozen=True)
class AOIInferenceConfig:
    aoi_bbox: list[float]
    target_date: str
    output_root: Path = DEFAULT_OUTPUT_ROOT
    run_name: str | None = None
    spacing_m: int = 400
    box_size_m: int = 400
    min_river_length_m: float = 10_000.0
    projected_crs: str = "EPSG:32634"
    water_threshold: str | float = "distribution"
    water_check_lookback_days: int = 365
    water_min_auto_threshold_pct: float = 0.5
    history_padding_days: int = 45
    refresh_water: bool = False
    download_images: bool = True
    per_tile_images: bool = False
    global_image: bool = True
    image_keys: tuple[str, ...] = DEFAULT_IMAGE_KEYS
    run_inference: bool = True
    collect_features: bool = True
    plot: bool = True
    plot_feature: str = "WQI"
    model_root: Path = DEFAULT_MODEL_ROOT
    model_path: Path | None = None
    scalers_path: Path | None = None
    feature_data_root: Path | None = None
    max_cloud_coverage: int = 30
    model_profile: ModelInferenceProfile = ModelInferenceProfile()


class AOIInferencePipeline:
    """Build the AOI-to-water-tile plan and execute the full model inference pipeline."""

    def __init__(self, config: AOIInferenceConfig):
        self.config = config
        self.run_dir = self._run_dir()
        self.tiles_geojson_path = self.run_dir / "river_tiles.geojson"
        self.water_manifest_path = self.run_dir / "water_selection.json"
        self.plan_path = self.run_dir / "inference_plan.json"

    def execute(self) -> dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.validate_image_keys()

        extractor = RiverTileExtractor(
            RiverTileExtractorConfig(
                aoi_bbox=list(self.config.aoi_bbox),
                projected_crs=self.config.projected_crs,
                spacing_m=self.config.spacing_m,
                box_size_m=self.config.box_size_m,
                min_length_m=self.config.min_river_length_m,
            )
        )
        tiles, _ = extractor.extract_to_geojson(self.tiles_geojson_path)

        water_start, water_end = self.water_check_interval()
        selector = WaterTileSelector(
            geojson_path=self.tiles_geojson_path,
            cache_path=self.water_manifest_path,
            water_check_interval=(water_start, water_end),
            reference_last_n=0,
            threshold=self.config.water_threshold,
            min_auto_threshold_pct=self.config.water_min_auto_threshold_pct,
            refresh=self.config.refresh_water,
        )
        water_manifest = selector.select_tiles()
        print_selection_summary(water_manifest)

        selected_records = [
            record
            for record in water_manifest.get("tiles", [])
            if record.get("name") in set(water_manifest.get("selected_tiles", []))
        ]
        image_downloads = self.download_target_date_images(selected_records) if self.config.download_images else {}
        history_start, history_end = self.required_history_interval()
        feature_csvs: dict[str, str] = {}
        forecast_payload: dict[str, Any] | None = None
        if self.config.run_inference:
            feature_csvs = self.prepare_feature_csvs(selected_records, history_start, history_end)
            forecast_payload = self.run_model_inference(
                selected_records=selected_records,
                feature_csvs=feature_csvs,
                water_manifest=water_manifest,
            )
        plan = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "aoi_bbox": list(self.config.aoi_bbox),
            "target_date": self.config.target_date,
            "run_dir": str(self.run_dir),
            "tiles_geojson": str(self.tiles_geojson_path),
            "water_manifest": str(self.water_manifest_path),
            "tile_count": len(tiles),
            "selected_water_tile_count": len(selected_records),
            "selected_tiles": [record["name"] for record in selected_records],
            "selected_tile_records": selected_records,
            "history_interval": {
                "start_date": history_start,
                "end_date": history_end,
                "cadence_days": self.config.model_profile.cadence_days,
                "time_steps": self.config.model_profile.time_steps,
            },
            "water_check_interval": {
                "start_date": water_start,
                "end_date": water_end,
                "threshold": self.config.water_threshold,
            },
            "target_date_images": {
                "enabled": self.config.download_images,
                "date": self.config.target_date,
                "keys": list(self.config.image_keys),
                "downloads": image_downloads,
            },
            "inference": {
                "enabled": self.config.run_inference,
                "model_root": str(self.config.model_root),
                "feature_csvs": feature_csvs,
                "forecast_json": forecast_payload.get("forecast_json") if forecast_payload else None,
                "forecast_csv": forecast_payload.get("forecast_csv") if forecast_payload else None,
                "plot_dir": forecast_payload.get("plot_dir") if forecast_payload else None,
            },
            "model_profile": asdict(self.config.model_profile),
        }
        self.plan_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        print(f"Inference pipeline completed. Plan written to {self.plan_path}")
        return plan

    def required_history_interval(self) -> tuple[str, str]:
        profile = self.config.model_profile
        end = date.fromisoformat(self.config.target_date)
        required_span_days = (int(profile.time_steps) - 1) * int(profile.cadence_days)
        start = end - timedelta(days=required_span_days + int(self.config.history_padding_days))
        return start.isoformat(), end.isoformat()

    def water_check_interval(self) -> tuple[str, str]:
        end = date.fromisoformat(self.config.target_date)
        start = end - timedelta(days=int(self.config.water_check_lookback_days))
        return start.isoformat(), end.isoformat()

    def validate_image_keys(self) -> None:
        supported = set(ImageCollection.supported_keys())
        requested = set(self.config.image_keys)
        unknown = sorted(requested - supported)
        if unknown:
            supported_text = ", ".join(ImageCollection.supported_keys())
            print(f"[ImageCollection] Unknown image keys: {', '.join(unknown)}")
            print(f"[ImageCollection] Supported image keys: {supported_text}")
            raise ValueError(f"Unknown image keys: {', '.join(unknown)}. Supported image keys: {supported_text}.")

    @staticmethod
    def unavailable_image_records(
        image_keys: tuple[str, ...],
        requested_date: str,
        message: str,
    ) -> dict[str, dict[str, str | None]]:
        return {
            key: {
                "status": "unavailable",
                "path": None,
                "requested_date": requested_date,
                "actual_date": "N/A",
                "collection": ImageCollection.collection_label_for_key(key),
                "message": message,
            }
            for key in image_keys
        }

    def download_target_date_images(self, selected_records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, str | None]]]:
        if not selected_records and not self.config.global_image:
            return {}

        image_root = self.run_dir / "target_date_images"
        target_day = date.fromisoformat(self.config.target_date).isoformat()
        time_from = f"{target_day}T00:00:00Z"
        time_to = f"{target_day}T23:59:59Z"
        downloads: dict[str, dict[str, dict[str, str | None]]] = {}

        print(
            f"\n=== Target-Date Image Download ({target_day}) ===\n"
            f"Products: {', '.join(self.config.image_keys)}"
        )

        if self.config.global_image:
            print(f"\n=== Downloading global target-date images for AOI ===")
            tile_image_dir = image_root / "global"
            try:
                image_collector = ImageCollection(
                    bbox=list(self.config.aoi_bbox),
                    dir=str(tile_image_dir),
                    tile_name="global",
                    tile_size=1000,
                    time_from=time_from,
                    time_to=time_to,
                )
                results = image_collector.run(list(self.config.image_keys))
                downloads["global"] = results
            except Exception as exc:
                print(f"[WARN] Target-date image download failed for global AOI: {exc}")
                downloads["global"] = self.unavailable_image_records(
                    self.config.image_keys,
                    target_day,
                    str(exc),
                )

        if self.config.per_tile_images and selected_records:
            print(f"Tiles: {len(selected_records)}")
            for record in selected_records:
                tile_name = str(record["name"])
                bbox = record.get("bbox")
                if not bbox or len(bbox) != 4:
                    print(f"[WARN] Skipping image download for {tile_name}: missing bbox.")
                    downloads[tile_name] = self.unavailable_image_records(
                        self.config.image_keys,
                        target_day,
                        "Missing tile bbox.",
                    )
                    continue

                tile_image_dir = image_root / tile_name
                print(f"\n=== Downloading target-date images for {tile_name} ===")
                try:
                    image_collector = ImageCollection(
                        bbox=list(bbox),
                        dir=str(tile_image_dir),
                        tile_name=tile_name,
                        tile_size=400,
                        time_from=time_from,
                        time_to=time_to,
                    )
                    results = image_collector.run(list(self.config.image_keys))
                    downloads[tile_name] = results
                except Exception as exc:
                    print(f"[WARN] Target-date image download failed for {tile_name}: {exc}")
                    downloads[tile_name] = self.unavailable_image_records(
                        self.config.image_keys,
                        target_day,
                        str(exc),
                    )

        return downloads

    def prepare_feature_csvs(
        self,
        selected_records: list[dict[str, Any]],
        history_start: str,
        history_end: str,
    ) -> dict[str, str]:
        feature_root = Path(self.config.feature_data_root) if self.config.feature_data_root else self.run_dir / "feature_data"
        csvs: dict[str, str] = {}
        if not selected_records:
            return csvs

        print(
            f"\n=== Feature Preparation for Model Inference ===\n"
            f"Interval: {history_start} to {history_end} | cadence: 5D | OpenMeteo: disabled"
        )
        for record in selected_records:
            tile_name = str(record["name"])
            tile_dir = feature_root / tile_name
            tile_csv_dir = tile_dir / "csv"
            output_csv = tile_csv_dir / DEFAULT_FEATURE_CSV_NAME

            if output_csv.exists():
                print(f"[CACHE] Features already collected for {tile_name}, skipping download.")
                csvs[tile_name] = str(output_csv)
                continue

            bbox = record.get("bbox")
            if not bbox or len(bbox) != 4:
                print(f"[WARN] Skipping feature collection for {tile_name}: missing bbox.")
                continue

            tile_stat_dir = tile_dir / "statistical"
            tile_plot_dir = tile_dir / "plots" / "interpolation"
            tile_csv_dir.mkdir(parents=True, exist_ok=True)
            tile_stat_dir.mkdir(parents=True, exist_ok=True)
            tile_plot_dir.mkdir(parents=True, exist_ok=True)

            print(f"\n=== Collecting no-OpenMeteo 5D features for {tile_name} ===")
            collector = StatisticalCollection(
                time_interval=(history_start, history_end),
                bbox=list(bbox),
                dir=str(tile_dir),
                max_cloud_coverage=int(self.config.max_cloud_coverage),
            )
            collector.run(
                json_output_folder=str(tile_stat_dir),
                csv_output_folder=str(tile_csv_dir),
            )

            mean_metrics = tile_csv_dir / "mean_metrics.csv"
            if not mean_metrics.exists():
                print(f"[WARN] {tile_name}: mean_metrics.csv was not created; skipping inference for this tile.")
                continue

            augmentor = DataAugmentation(
                input_path=str(mean_metrics),
                output_path=str(output_csv),
                summary_plot_path=str(tile_plot_dir / "5D_all_features_time_based_interpolation.png"),
                per_feature_dir=str(tile_plot_dir),
                freq="5D",
            )
            augmentor.run()
            csvs[tile_name] = str(output_csv)

        return csvs

    def run_model_inference(
        self,
        selected_records: list[dict[str, Any]],
        feature_csvs: dict[str, str],
        water_manifest: dict[str, Any],
    ) -> dict[str, Any]:
        if not feature_csvs:
            print("[WARN] No feature CSVs available for model inference.")
            return {
                "forecast_json": None,
                "forecast_csv": None,
                "plot_dir": None,
                "tiles": {},
            }

        model_root = Path(self.config.model_root)
        metadata = self._read_model_metadata(model_root)
        scalers_path = self._resolve_scalers_path(model_root, metadata)
        model_path = self._resolve_model_path(model_root, metadata)
        scalers = joblib.load(scalers_path)
        model = self._load_model(model_path)

        target_cols = list(scalers["target_cols"])
        time_steps = int(scalers.get("time_steps") or self.config.model_profile.time_steps)
        horizon = int(scalers.get("horizon") or self.config.model_profile.horizon)
        selected_by_name = {str(record["name"]): record for record in selected_records}
        model_uses_tile_id = self._model_uses_input(model, "tile_id")
        model_uses_spatial = self._model_uses_input(model, "spatial_context")
        if model_uses_tile_id:
            raise ValueError(
                "The selected model requires tile_id embeddings. That is not valid for new AOI tiles. "
                "Use a tile-agnostic or spatial-context global model."
            )

        spatial_contexts = {}
        if model_uses_spatial:
            spatial_contexts = spatial_context_from_manifest(water_manifest, tiles=list(feature_csvs))
            if scalers.get("spatial_scaler") is None:
                raise ValueError("Model expects spatial_context, but global_scalers.joblib has no spatial_scaler.")

        forecast_root = self.run_dir / "forecasts"
        plot_root = forecast_root / "plots"
        forecast_root.mkdir(parents=True, exist_ok=True)
        if self.config.plot:
            plot_root.mkdir(parents=True, exist_ok=True)

        all_rows = []
        tile_payloads: dict[str, Any] = {}
        for tile_name, csv_path in sorted(feature_csvs.items()):
            if tile_name not in selected_by_name:
                continue
            print(f"\n=== Running global model inference for {tile_name} ===")
            try:
                payload = self._predict_tile(
                    tile_name=tile_name,
                    csv_path=Path(csv_path),
                    model=model,
                    scalers=scalers,
                    target_cols=target_cols,
                    time_steps=time_steps,
                    horizon=horizon,
                    spatial_raw=spatial_contexts.get(tile_name),
                )
            except Exception as exc:
                print(f"[WARN] Inference failed for {tile_name}: {exc}")
                tile_payloads[tile_name] = {"error": str(exc), "csv_path": csv_path}
                continue

            tile_out_dir = forecast_root / tile_name
            tile_out_dir.mkdir(parents=True, exist_ok=True)
            (tile_out_dir / "forecast.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tile_payloads[tile_name] = payload
            for row in payload["forecast"]["model"]:
                all_rows.append({"tile": tile_name, **row})

            if self.config.plot:
                self._plot_tile_forecast(payload, plot_root / f"{tile_name}_{self.config.plot_feature}.png")

        forecast_json = forecast_root / "forecasts.json"
        forecast_csv = forecast_root / "forecasts.csv"
        aggregate_payload = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model_root": str(model_root),
            "model_path": str(model_path),
            "scalers_path": str(scalers_path),
            "target_date": self.config.target_date,
            "time_steps": time_steps,
            "horizon": horizon,
            "target_cols": target_cols,
            "tiles": tile_payloads,
        }
        forecast_json.write_text(json.dumps(aggregate_payload, indent=2), encoding="utf-8")
        pd.DataFrame(all_rows).to_csv(forecast_csv, index=False)
        print(f"\nForecasts written to {forecast_json}")
        print(f"Forecast CSV written to {forecast_csv}")
        if self.config.plot:
            print(f"Forecast plots written to {plot_root}")
        return {
            "forecast_json": str(forecast_json),
            "forecast_csv": str(forecast_csv),
            "plot_dir": str(plot_root) if self.config.plot else None,
            "tiles": tile_payloads,
        }

    def _predict_tile(
        self,
        tile_name: str,
        csv_path: Path,
        model,
        scalers: dict[str, Any],
        target_cols: list[str],
        time_steps: int,
        horizon: int,
        spatial_raw: np.ndarray | None,
    ) -> dict[str, Any]:
        data, features_raw, targets_raw, _, _ = prepare_raw_frame(csv_path, target_cols=target_cols)
        if len(data) < time_steps:
            raise ValueError(f"{csv_path} has {len(data)} rows, but model needs W={time_steps}.")

        feature_scaler = scalers["feature_scaler"]
        target_scaler = scalers["target_scaler"]
        scaled_features = feature_scaler.transform(features_raw).astype(np.float32)
        scaled_targets = target_scaler.transform(targets_raw).astype(np.float32)
        prev_targets = np.roll(scaled_targets, 1, axis=0)
        prev_targets[0] = scaled_targets[0]
        time_features = data[["sin_doy", "cos_doy", "sin_month", "cos_month"]].values.astype(np.float32)
        features_full = np.concatenate([scaled_features, time_features, prev_targets], axis=1).astype(np.float32)

        inputs: dict[str, np.ndarray] = {"X": features_full[-time_steps:][None, ...]}
        if self._model_uses_input(model, "spatial_context"):
            if spatial_raw is None:
                raise ValueError(f"Model expects spatial_context, but none was provided for {tile_name}.")
            spatial_scaled = scalers["spatial_scaler"].transform(np.asarray(spatial_raw, dtype=np.float32).reshape(1, -1))
            inputs["spatial_context"] = spatial_scaled.astype(np.float32)

        pred_scaled = model.predict(inputs, verbose=0)[0][:horizon]
        pred_real = target_scaler.inverse_transform(pred_scaled.reshape(-1, len(target_cols))).reshape(pred_scaled.shape)
        pred_real = self._clip_physical(pred_real, target_cols)

        last_date = pd.to_datetime(data["date"].iloc[-1])
        gaps = data["date"].diff().dt.total_seconds().div(86400).dropna()
        gaps = gaps[gaps > 0]
        step_days = float(gaps.median()) if not gaps.empty else float(self.config.model_profile.cadence_days)
        y_t = data.iloc[-1]
        y_t_prev = data.iloc[-2] if len(data) >= 2 else data.iloc[-1]

        model_rows = []
        persistence_rows = []
        linear_rows = []
        for step in range(len(pred_real)):
            forecast_date = (last_date + pd.Timedelta(days=step_days * (step + 1))).date().isoformat()
            model_row = {"date": forecast_date, "step": step + 1}
            persistence_row = {"date": forecast_date, "step": step + 1}
            linear_row = {"date": forecast_date, "step": step + 1}
            for target_index, column in enumerate(target_cols):
                model_row[column] = float(pred_real[step, target_index])
                persistence_row[column] = float(y_t[column])
                velocity = float(y_t[column] - y_t_prev[column])
                linear_value = float(y_t[column] + (step + 1) * velocity)
                if column in NON_NEGATIVE_COLS:
                    linear_value = max(linear_value, 0.0)
                linear_row[column] = linear_value
            model_rows.append(model_row)
            persistence_rows.append(persistence_row)
            linear_rows.append(linear_row)

        history_cols = ["date"] + target_cols
        history = data[history_cols].tail(max(time_steps, 48)).copy()
        history["date"] = history["date"].dt.date.astype(str)
        return {
            "tile": tile_name,
            "csv_path": str(csv_path),
            "last_input_date": last_date.date().isoformat(),
            "step_days": step_days,
            "target_cols": target_cols,
            "history": history.to_dict(orient="records"),
            "forecast": {
                "model": model_rows,
                "persistence": persistence_rows,
                "linear": linear_rows,
            },
        }

    def _plot_tile_forecast(self, payload: dict[str, Any], output_path: Path) -> None:
        feature = self.config.plot_feature
        if feature not in payload["target_cols"]:
            feature = "WQI"
        history = pd.DataFrame(payload["history"])
        forecast = pd.DataFrame(payload["forecast"]["model"])
        persistence = pd.DataFrame(payload["forecast"]["persistence"])
        linear = pd.DataFrame(payload["forecast"]["linear"])
        history["date"] = pd.to_datetime(history["date"])
        forecast["date"] = pd.to_datetime(forecast["date"])
        persistence["date"] = pd.to_datetime(persistence["date"])
        linear["date"] = pd.to_datetime(linear["date"])

        plt.figure(figsize=(11, 5))
        plt.plot(history["date"], history[feature], color="#2563eb", linewidth=1.8, label="History")
        plt.plot(forecast["date"], forecast[feature], "o-", color="#dc2626", linewidth=2.0, label="Forecast")
        plt.plot(persistence["date"], persistence[feature], "x--", color="#16a34a", linewidth=1.4, label="Persistence")
        plt.plot(linear["date"], linear[feature], ":", color="#9333ea", linewidth=1.5, label="Linear velocity")
        plt.title(f"{feature} forecast - {payload['tile']}")
        plt.xlabel("Date")
        plt.ylabel(feature)
        plt.grid(True, alpha=0.25)
        plt.legend()
        plt.tight_layout()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        plt.savefig(output_path, dpi=160)
        plt.close()

    @staticmethod
    def _clip_physical(values: np.ndarray, target_cols: list[str]) -> np.ndarray:
        clipped = values.copy()
        for index, column in enumerate(target_cols):
            if column in NON_NEGATIVE_COLS:
                clipped[..., index] = np.maximum(clipped[..., index], 0.0)
        return clipped

    @staticmethod
    def _model_uses_input(model, input_name: str) -> bool:
        return any(getattr(input_tensor, "name", "").split(":")[0].endswith(input_name) for input_tensor in model.inputs)

    def _read_model_metadata(self, model_root: Path) -> dict[str, Any]:
        metadata_path = model_root / "global_metadata.json"
        if not metadata_path.exists():
            return {}
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _resolve_model_path(self, model_root: Path, metadata: dict[str, Any]) -> Path:
        if self.config.model_path:
            return Path(self.config.model_path)
        
        # We handle cases where the model is simply the only .keras file
        candidates = sorted((model_root / ".keras").glob("**/*.keras"))
        if candidates:
            return candidates[0]
        raise FileNotFoundError(f"No global model checkpoint found under {model_root}")

    def _resolve_scalers_path(self, model_root: Path, metadata: dict[str, Any]) -> Path:
        if self.config.scalers_path:
            return Path(self.config.scalers_path)
        
        fallback = model_root / "global_scalers.joblib"
        if fallback.exists():
            return fallback
        raise FileNotFoundError(f"No global_scalers.joblib found under {model_root}")

    @staticmethod
    def _resolve_repo_path(value: str | Path) -> Path:
        path = Path(value)
        if isinstance(value, str) and value.startswith("/"):
            return REPO_ROOT / value.lstrip("/")
        return path

    @staticmethod
    def _load_model(model_path: Path):
        import builtins
        builtins.K = K
        builtins.tf = tf
        
        custom_objects = {
            "tf": tf,
            "K": K,
            "HorizonVelocityScale": HorizonVelocityScale,
            "ForecastWeightedLoss": ForecastWeightedLoss,
            "HorizonWeightedLoss": HorizonWeightedLoss,
        }
        return tf.keras.models.load_model(
            model_path,
            compile=False,
            safe_mode=False,
            custom_objects=custom_objects,
        )

    def _run_dir(self) -> Path:
        if self.config.run_name:
            run_name = self._slugify(self.config.run_name)
        else:
            bbox_token = "_".join(f"{value:.5f}" for value in self.config.aoi_bbox)
            run_name = self._slugify(f"aoi_{bbox_token}_{self.config.target_date}")
        return Path(self.config.output_root) / run_name

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower())
        return slug.strip("_") or "aoi_inference"


def parse_bbox(value: str | list[str]) -> list[float]:
    raw = " ".join(value).strip() if isinstance(value, list) else value.strip()
    if raw.startswith("["):
        parsed = ast.literal_eval(raw)
        if not isinstance(parsed, (list, tuple)):
            raise argparse.ArgumentTypeError("--bbox must be a list of four numbers.")
        values = list(parsed)
    else:
        values = raw.replace(",", " ").split()
    if len(values) != 4:
        raise argparse.ArgumentTypeError("--bbox must contain four values: [min_lon, min_lat, max_lon, max_lat].")
    try:
        return [float(item) for item in values]
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--bbox values must be numeric.") from exc


def parse_image_keys(value: str | tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, tuple):
        keys = value
    else:
        keys = tuple(key.strip() for key in value.split(",") if key.strip())
    supported = set(ImageCollection.supported_keys())
    unknown = sorted(set(keys) - supported)
    if unknown:
        supported_text = ", ".join(ImageCollection.supported_keys())
        raise argparse.ArgumentTypeError(
            f"unknown image product(s): {', '.join(unknown)}. Supported image keys: {supported_text}"
        )
    return keys


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create an AOI river-tile and water-selection inference plan.")
    parser.add_argument(
        "--bbox",
        nargs="+",
        required=True,
        metavar="[MIN_LON,MIN_LAT,MAX_LON,MAX_LAT]",
        help="AOI bounding box in EPSG:4326, e.g. '[22.03898, 38.765311, 22.783071, 38.969536]'.",
    )
    parser.add_argument("--target-date", required=True, help="Inference anchor date, YYYY-MM-DD.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--run-name", default=None)
    parser.add_argument("--spacing", type=int, default=400)
    parser.add_argument("--box-size", type=int, default=400)
    parser.add_argument("--min-river-length", type=float, default=10_000.0)
    parser.add_argument("--projected-crs", default="EPSG:32634")
    parser.add_argument("--water-threshold", default="distribution")
    parser.add_argument("--water-check-lookback-days", type=int, default=365)
    parser.add_argument("--history-padding-days", type=int, default=45)
    parser.add_argument("--refresh-water", action="store_true")
    parser.add_argument("--skip-images", action="store_true", help="Do not download any exact target-date images.")
    parser.add_argument("--per-tile-images", action="store_true", help="Download images for every individual tile.")
    parser.add_argument("--skip-global-image", action="store_true", help="Do not download the global AOI image.")
    parser.add_argument(
        "--image-keys",
        default=DEFAULT_IMAGE_KEYS,
        type=parse_image_keys,
        help="Comma-separated target-date image products to download.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    bbox = parse_bbox(args.bbox)
    planner = AOIInferencePipeline(
        AOIInferenceConfig(
            aoi_bbox=bbox,
            target_date=args.target_date,
            output_root=Path(args.output_root),
            run_name=args.run_name,
            spacing_m=args.spacing,
            box_size_m=args.box_size,
            min_river_length_m=args.min_river_length,
            projected_crs=args.projected_crs,
            water_threshold=args.water_threshold,
            water_check_lookback_days=args.water_check_lookback_days,
            history_padding_days=args.history_padding_days,
            refresh_water=args.refresh_water,
            download_images=not args.skip_images,
            per_tile_images=args.per_tile_images,
            global_image=not args.skip_global_image,
            image_keys=args.image_keys,
        )
    )
    planner.execute()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
