"""Run the UC1 forecasting model from data supplied by an external collector.

This module is deliberately collector-free: it reads the AOI definition, tile
definitions and observation records already published to shared storage.  The
collector may run in another repository, container, or partner service.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from forecaster.core.global_preprocessor import WATER_TARGET_COLS
from forecaster.inference import AOIInferenceConfig, AOIInferencePipeline, DEFAULT_FEATURE_CSV_NAME, ModelInferenceProfile
from forecaster.stac_exporter import StacCatalogExporter
from forecaster.storage import MongoMinioStore, StorageConnectionError, StorageSettings


TARGET_COLS = tuple(WATER_TARGET_COLS)


@dataclass(frozen=True)
class StoredForecastConfig:
    aoi_id: str
    run_name: str
    output_root: Path = Path("outputs/forecasts")
    as_of_date: str | None = None
    history_start: str | None = None
    collection_run_dir: Path | None = None
    publish: bool = True
    plot: bool = False
    stac_base_url: str | None = None
    model_profile: ModelInferenceProfile = ModelInferenceProfile()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "_" for char in value).strip("_") or "forecast"


def _has_metrics(record: dict[str, Any]) -> bool:
    return all(record.get(column) is not None for column in TARGET_COLS)


def _usable(record: dict[str, Any]) -> bool:
    return (
        _has_metrics(record)
        and str(record.get("collection_status", "collected")) == "collected"
        and str(record.get("water_status", "water")) != "no_water"
        and "cloudy" not in set(record.get("quality_flags") or [])
    )


def _flatten_forecast_rows(forecast_run_id: str, anchor_date: str, tile_payloads: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for tile_id, payload in sorted(tile_payloads.items()):
        if "error" in payload:
            continue
        for prediction in payload.get("forecast", {}).get("model", []):
            row = {
                "forecast_run_id": forecast_run_id,
                "anchor_date": anchor_date,
                "tile_id": tile_id,
                "forecast_date": str(prediction.get("date", ""))[:10],
                "step": int(prediction.get("step") or 0),
                "model": "model",
                "created_at": _utc_now(),
            }
            row.update({column: prediction.get(column) for column in TARGET_COLS})
            rows.append(row)
    return rows


class StoredForecastPipeline:
    """Forecast from the canonical AOI-scoped collector data in MongoDB/MinIO."""

    def __init__(self, config: StoredForecastConfig, *, storage: MongoMinioStore | None = None):
        self.config = config
        self.storage = storage or (
            MongoMinioStore(StorageSettings.from_env(aoi_id=config.aoi_id))
            if config.collection_run_dir is None or config.publish else None
        )
        self.run_dir = Path(config.output_root) / _slug(config.run_name)
        self.execution_id = f"forecast-run-{_utc_now().replace(':', '')}"

    def execute(self) -> dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if self.storage is not None:
            self.storage.initialize()
        if self.config.collection_run_dir:
            definition, tiles, records = self._load_local_collection_input()
        else:
            assert self.storage is not None
            definition = self._load_definition()
            tiles = self._load_tiles()
            records = self.storage.load_observations(
                start_date=self.config.history_start,
                end_date=self.config.as_of_date,
            )
        anchor_date = self._anchor_date(records)
        if not anchor_date:
            raise ValueError(f"No usable collected observations are available for AOI {self.config.aoi_id!r}.")

        usable = [record for record in records if str(record.get("observation_date")) <= anchor_date and _usable(record)]
        feature_csvs = self._write_feature_csvs(usable)
        water_manifest = self._water_manifest(tiles, feature_csvs)
        selected_tiles = water_manifest["tiles"]
        if not selected_tiles:
            raise ValueError(
                f"AOI {self.config.aoi_id!r} has observations, but no tile has the "
                f"{self.config.model_profile.time_steps} usable input records required by the model."
            )

        forecast_run_id = f"forecast_{anchor_date}"
        inference = AOIInferencePipeline(AOIInferenceConfig(
            aoi_bbox=list(definition["bbox"]),
            target_date=anchor_date,
            output_root=self.run_dir / "runs",
            run_name=forecast_run_id,
            feature_data_root=self.run_dir / "feature_data",
            download_images=False,
            plot=self.config.plot,
            model_profile=self.config.model_profile,
        ))
        inference.run_dir.mkdir(parents=True, exist_ok=True)
        payload = inference.run_model_inference(
            selected_records=selected_tiles,
            feature_csvs=feature_csvs,
            water_manifest=water_manifest,
        )
        rows = _flatten_forecast_rows(forecast_run_id, anchor_date, payload.get("tiles", {}))
        if not rows:
            raise ValueError("The model produced no forecast rows for the stored collector data.")

        forecast_dir = self.run_dir / "forecasts"
        forecast_dir.mkdir(parents=True, exist_ok=True)
        (forecast_dir / "global_forecasts.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        pd.DataFrame(rows).to_csv(forecast_dir / "global_forecasts.csv", index=False)
        stac_result = StacCatalogExporter(self.run_dir / "stac_catalog", stac_base_url=self.config.stac_base_url).export(
            aoi_bbox=list(definition["bbox"]),
            tile_records=tiles,
            history_records=records,
            forecast_rows=rows,
            anchor_date=anchor_date,
            horizon_days=int(self.config.model_profile.horizon) * int(self.config.model_profile.cadence_days),
            processing_metadata={"version": self.config.model_profile.model_name},
        )
        if self.storage is not None and self.config.publish:
            artifact = self.storage.upload_json_if_changed(
                rows,
                key=self.storage.data_key(relative_path=f"forecasts/{forecast_run_id}.json"),
            )
            self.storage.upsert_forecasts([{**row, "artifact": artifact} for row in rows], run_id=self.execution_id)
        result = {
            "status": "success",
            "aoi_id": self.config.aoi_id,
            "run_name": self.config.run_name,
            "forecast_run_id": forecast_run_id,
            "forecast_anchor": anchor_date,
            "input_observation_count": len(records),
            "usable_observation_count": len(usable),
            "forecast_row_count": len(rows),
            "forecast_tiles": sorted(feature_csvs),
            "stac_item_id": stac_result.item_id,
            "source": "collector_run_directory" if self.config.collection_run_dir else "shared_storage",
        }
        (self.run_dir / "forecast_run_result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if self.storage is not None and self.config.publish:
            self.storage.record_run(result, run_id=self.execution_id)
        return result

    def _load_local_collection_input(self) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Read one completed standalone collector run without importing it."""
        assert self.config.collection_run_dir is not None
        root = Path(self.config.collection_run_dir)
        manifest_path = root / "collection" / "collection_input_manifest.json"
        if not manifest_path.exists():
            raise ValueError(f"Collector input manifest is missing: {manifest_path}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if str(manifest.get("aoi_id")) != self.config.aoi_id:
            raise ValueError(f"Collector manifest AOI does not match --aoi-id {self.config.aoi_id!r}.")
        history_path = root / "history" / "global_history.json"
        tiles_path = root / "tiles" / "tile_records.json"
        if not history_path.exists() or not tiles_path.exists():
            raise ValueError("Collector run is incomplete: history/global_history.json and tiles/tile_records.json are required.")
        records = json.loads(history_path.read_text(encoding="utf-8"))
        if self.config.history_start:
            records = [row for row in records if str(row.get("observation_date", "")) >= self.config.history_start]
        if self.config.as_of_date:
            records = [row for row in records if str(row.get("observation_date", "")) <= self.config.as_of_date]
        definition = {"aoi_id": self.config.aoi_id, "bbox": manifest.get("aoi_bbox")}
        return definition, json.loads(tiles_path.read_text(encoding="utf-8")), records

    def _load_definition(self) -> dict[str, Any]:
        assert self.storage is not None
        definition = self.storage.download_json(key=self.storage.aoi_key(relative_path="definition.json"))
        if not isinstance(definition, dict) or str(definition.get("aoi_id")) != self.config.aoi_id:
            raise ValueError(f"AOI definition is missing or incompatible for {self.config.aoi_id!r}.")
        if not isinstance(definition.get("bbox"), list) or len(definition["bbox"]) != 4:
            raise ValueError(f"AOI definition for {self.config.aoi_id!r} has no valid bbox.")
        self.storage.aoi_definition_hash = definition.get("aoi_definition_hash")
        return definition

    def _load_tiles(self) -> list[dict[str, Any]]:
        assert self.storage is not None
        tiles = self.storage.load_tiles()
        if not tiles:
            tiles = self.storage.download_json(key=self.storage.aoi_key(relative_path="tiles/tile_records.json")) or []
        normalized = []
        for tile in tiles:
            name = str(tile.get("name") or tile.get("tile_id") or "")
            bbox = tile.get("bbox")
            if name and isinstance(bbox, list) and len(bbox) == 4:
                normalized.append({**tile, "name": name, "tile_id": name})
        if not normalized:
            raise ValueError(f"No usable tile definitions are available for AOI {self.config.aoi_id!r}.")
        return normalized

    def _anchor_date(self, records: list[dict[str, Any]]) -> str | None:
        dates = sorted(str(record["observation_date"])[:10] for record in records if _usable(record))
        return dates[-1] if dates else None

    def _write_feature_csvs(self, records: list[dict[str, Any]]) -> dict[str, str]:
        by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            by_tile.setdefault(str(record["tile_id"]), []).append(record)
        output: dict[str, str] = {}
        for tile_id, values in by_tile.items():
            values.sort(key=lambda record: str(record["observation_date"]))
            if len(values) < int(self.config.model_profile.time_steps):
                continue
            rows = []
            for record in values:
                row = {"date": str(record["observation_date"])[:10]}
                for column in TARGET_COLS:
                    row[column] = record[column]
                    row[f"{column}_gpr_std"] = 0.0
                rows.append(row)
            path = self.run_dir / "feature_data" / tile_id / "csv" / DEFAULT_FEATURE_CSV_NAME
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(rows).to_csv(path, index=False)
            output[tile_id] = str(path)
        return output

    @staticmethod
    def _water_manifest(tiles: list[dict[str, Any]], feature_csvs: dict[str, str]) -> dict[str, Any]:
        selected = [
            {"name": tile["name"], "bbox": tile["bbox"], "size": tile.get("size"), "water_score_pct": 0.0, "selected": True}
            for tile in tiles if tile["name"] in feature_csvs
        ]
        return {"selected_tiles": [tile["name"] for tile in selected], "rejected_tiles": [], "tiles": selected}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forecast from collector data already published to shared storage.")
    parser.add_argument("--aoi-id", required=True, help="Stable AOI ID used by the collector when publishing data.")
    parser.add_argument("--run-name", required=True, help="Name for this forecast-only run.")
    parser.add_argument("--output-root", default="outputs/forecasts")
    parser.add_argument("--as-of-date", help="Use observations on or before this ISO date; defaults to the latest usable observation.")
    parser.add_argument("--history-start", help="Optional earliest ISO observation date to retrieve.")
    parser.add_argument("--collection-run-dir", help="Read a completed standalone collector run instead of shared storage.")
    parser.add_argument("--no-publish", action="store_true", help="With --collection-run-dir, keep forecast outputs local and do not require storage credentials.")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--stac-base-url")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = StoredForecastPipeline(StoredForecastConfig(
            aoi_id=args.aoi_id,
            run_name=args.run_name,
            output_root=Path(args.output_root),
            as_of_date=args.as_of_date,
            history_start=args.history_start,
            collection_run_dir=Path(args.collection_run_dir) if args.collection_run_dir else None,
            publish=not args.no_publish,
            plot=args.plot,
            stac_base_url=args.stac_base_url,
        )).execute()
    except (StorageConnectionError, ValueError) as exc:
        print(f"Forecast input failed: {exc}")
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
