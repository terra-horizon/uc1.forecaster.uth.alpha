from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from data_collection_bootstrap import ensure_data_collection_importable
from forecast import (
    AOIInferenceConfig,
    AOIInferencePipeline,
    DEFAULT_FEATURE_CSV_NAME,
    ModelInferenceProfile,
    parse_bbox,
)
from forecaster.collection_provider import CollectionProvider, CollectionRequest, LocalCollectionProvider
from forecaster.core.global_preprocessor import WATER_TARGET_COLS
from forecaster.stac_exporter import StacCatalogExporter

ensure_data_collection_importable()
from data_collection.service import build_history_record as collector_history_record  # noqa: E402
from data_collection.storage import safe_float  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEDULED_OUTPUT_ROOT = REPO_ROOT / "data" / "inference_runs"
TARGET_COLS = tuple(WATER_TARGET_COLS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def compact_key(record: dict[str, Any], *fields: str) -> tuple[str, ...]:
    return tuple(str(record.get(field, "")) for field in fields)


@dataclass(frozen=True)
class ScheduledPipelineConfig:
    aoi_bbox: list[float]
    run_name: str
    output_root: Path = DEFAULT_SCHEDULED_OUTPUT_ROOT
    history_start: str = "2016-01-01"
    target_date: str | None = None
    dry_run: bool = False
    max_days_per_run: int | None = None
    max_tiles_per_run: int | None = None
    discovery_chunk_days: int = 31
    backfill_all: bool = False
    stac_base_url: str | None = None
    spacing_m: int = 400
    box_size_m: int = 400
    min_river_length_m: float = 10_000.0
    projected_crs: str = "EPSG:32634"
    water_threshold: str | float = "distribution"
    water_min_auto_threshold_pct: float = 0.5
    max_cloud_coverage: int = 30
    refresh_water: bool = False
    run_inference: bool = True
    plot: bool = False
    model_profile: ModelInferenceProfile = ModelInferenceProfile()


@dataclass(frozen=True)
class ScheduledPipelineResult:
    status: str
    run_dir: str
    run_summary: str
    available_dates: list[str]
    missing_dates: list[str]
    collected_dates: list[str]
    new_data_available: bool
    forecast_anchor: str | None
    forecast_created: bool
    forecast_status: str
    last_forecast_anchor: str | None
    stac_item_id: str | None
    discovery_windows: list[dict[str, str]]
    warnings: list[dict[str, Any]]


class JsonCsvStore:
    def __init__(self, json_path: str | Path, csv_path: str | Path, key_fields: tuple[str, ...]):
        self.json_path = Path(json_path)
        self.csv_path = Path(csv_path)
        self.key_fields = key_fields

    def load(self) -> list[dict[str, Any]]:
        if not self.json_path.exists():
            return []
        return json.loads(self.json_path.read_text(encoding="utf-8"))

    def upsert(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        existing = {compact_key(record, *self.key_fields): record for record in self.load()}
        for record in records:
            existing[compact_key(record, *self.key_fields)] = record
        rows = sorted(existing.values(), key=lambda record: compact_key(record, *self.key_fields))
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(rows, indent=2, allow_nan=False), encoding="utf-8")
        pd.DataFrame(rows).to_csv(self.csv_path, index=False)
        return rows


class ProcessingState:
    def __init__(self, path: Path, legacy_path: Path):
        self.path = path
        self.legacy_path = legacy_path

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        legacy = json.loads(self.legacy_path.read_text(encoding="utf-8")) if self.legacy_path.exists() else {}
        return {
            "created_at": legacy.get("created_at", utc_now()),
            "last_forecast_anchor": legacy.get("last_forecast_anchor"),
            "forecast_run_ids": list(legacy.get("forecast_run_ids", [])),
            "legacy_state_migrated": bool(legacy),
        }

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = utc_now()
        self.path.write_text(json.dumps(state, indent=2, allow_nan=False), encoding="utf-8")


class ScheduledIncrementalPipeline:
    def __init__(self, config: ScheduledPipelineConfig, *, collection_provider: CollectionProvider | None = None):
        self.config = config
        self.run_dir = Path(config.output_root) / self._slugify(config.run_name)
        self.state = ProcessingState(self.run_dir / "processing" / "state.json", self.run_dir / "state.json")
        self.forecast_store = JsonCsvStore(
            self.run_dir / "forecasts" / "global_forecasts.json",
            self.run_dir / "forecasts" / "global_forecasts.csv",
            ("forecast_run_id", "tile_id", "forecast_date", "step"),
        )
        self.collection_provider = collection_provider or LocalCollectionProvider()

    def execute(self) -> ScheduledPipelineResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state = self.state.load()
        collection_result = self.collection_provider.collect(self._collection_request())
        warnings = list(collection_result.warnings)
        if collection_result.failed_units:
            warnings.append({
                "code": "COLLECTION_PARTIAL",
                "message": f"{len(collection_result.failed_units)} tile-date unit(s) remain retryable.",
                "failed_units": collection_result.failed_units,
            })
        if self.config.dry_run:
            return self._dry_run_result(collection_result, state, warnings)

        history_records = self._load_json(collection_result.history_json_path, [])
        tile_records = self._load_json(collection_result.tile_records_path, [])
        water_manifest = self._load_water_manifest(collection_result.water_manifest_path, tile_records)
        anchor_date = latest_usable_observation_date(history_records)
        self._log(f"Latest usable observation date: {anchor_date or 'none'}.")
        forecast_created, forecast_status, stac_item_id = self._forecast(
            state, anchor_date, tile_records, history_records, water_manifest
        )
        if not forecast_created:
            self._export_collection_stac(tile_records, history_records)
        self.state.save(state)

        new_data_available = collection_result.new_record_count > 0
        forecast_summary = build_run_summary(
            new_data_available=new_data_available,
            forecast_status=forecast_status,
            forecast_anchor=anchor_date,
            last_forecast_anchor=state.get("last_forecast_anchor"),
        )
        run_summary = f"{collection_result.run_summary} {forecast_summary}"
        result = ScheduledPipelineResult(
            status="partial" if collection_result.status == "partial" else "success",
            run_dir=str(self.run_dir),
            run_summary=run_summary,
            available_dates=collection_result.available_dates,
            missing_dates=collection_result.missing_dates,
            collected_dates=collection_result.collected_dates,
            new_data_available=new_data_available,
            forecast_anchor=anchor_date,
            forecast_created=forecast_created,
            forecast_status=forecast_status,
            last_forecast_anchor=state.get("last_forecast_anchor"),
            stac_item_id=stac_item_id,
            discovery_windows=collection_result.discovery_windows,
            warnings=warnings,
        )
        (self.run_dir / "scheduled_run_result.json").write_text(
            json.dumps(asdict(result), indent=2, allow_nan=False), encoding="utf-8"
        )
        self._log(run_summary)
        return result

    def _collection_request(self) -> CollectionRequest:
        return CollectionRequest(
            aoi_bbox=list(self.config.aoi_bbox),
            run_name=self.config.run_name,
            output_root=Path(self.config.output_root),
            history_start=self.config.history_start,
            target_date=self.config.target_date,
            mode="backfill" if self.config.backfill_all else "auto",
            dry_run=self.config.dry_run,
            max_days_per_run=self.config.max_days_per_run,
            max_tiles_per_run=self.config.max_tiles_per_run,
            discovery_chunk_days=self.config.discovery_chunk_days,
            spacing_m=self.config.spacing_m,
            box_size_m=self.config.box_size_m,
            min_river_length_m=self.config.min_river_length_m,
            projected_crs=self.config.projected_crs,
            water_threshold=self.config.water_threshold,
            water_min_auto_threshold_pct=self.config.water_min_auto_threshold_pct,
            max_cloud_coverage=self.config.max_cloud_coverage,
            refresh_water=self.config.refresh_water,
        )

    def _dry_run_result(self, collection_result, state, warnings) -> ScheduledPipelineResult:
        self._log(collection_result.run_summary)
        return ScheduledPipelineResult(
            status="dry_run",
            run_dir=str(self.run_dir),
            run_summary=collection_result.run_summary,
            available_dates=collection_result.available_dates,
            missing_dates=collection_result.missing_dates,
            collected_dates=[],
            new_data_available=bool(collection_result.missing_dates),
            forecast_anchor=None,
            forecast_created=False,
            forecast_status="dry_run",
            last_forecast_anchor=state.get("last_forecast_anchor"),
            stac_item_id=None,
            discovery_windows=collection_result.discovery_windows,
            warnings=warnings,
        )

    @staticmethod
    def _load_json(path: str | None, default: Any) -> Any:
        if not path or not Path(path).exists():
            return default
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def _load_water_manifest(self, path: str | None, tile_records: list[dict[str, Any]]) -> dict[str, Any]:
        if path and Path(path).exists():
            return self._load_json(path, {})
        candidates = sorted((self.run_dir / "water").glob("water_selection_*.json")) if (self.run_dir / "water").exists() else []
        if candidates:
            return json.loads(candidates[-1].read_text(encoding="utf-8"))
        return {
            "selected_tiles": [str(record["name"]) for record in tile_records],
            "rejected_tiles": [],
            "tiles": [
                {"name": record["name"], "bbox": record["bbox"], "size": record.get("size"), "selected": True, "scenes": []}
                for record in tile_records
            ],
        }

    def _forecast(self, state, anchor_date, tile_records, history_records, water_manifest):
        if not self.config.run_inference:
            return False, "disabled", None
        if not anchor_date:
            return False, "no_usable_observation", None
        if anchor_date == state.get("last_forecast_anchor"):
            return False, "already_forecasted", None
        forecast_rows, stac_item_id = self._run_forecast_cycle(
            anchor_date=anchor_date,
            tile_records=tile_records,
            history_records=history_records,
            water_manifest=water_manifest,
        )
        if not forecast_rows:
            return False, "insufficient_usable_history", stac_item_id
        self.forecast_store.upsert(forecast_rows)
        state["last_forecast_anchor"] = anchor_date
        state["forecast_run_ids"] = sorted(set(state.get("forecast_run_ids", [])) | {forecast_rows[0]["forecast_run_id"]})
        return True, "created", stac_item_id

    def _run_forecast_cycle(self, *, anchor_date, tile_records, history_records, water_manifest):
        feature_csvs = self._write_feature_csvs(history_records)
        selected_records = [record for record in water_manifest.get("tiles", []) if str(record.get("name")) in feature_csvs]
        if not selected_records:
            selected_records = [
                {"name": record["name"], "bbox": record["bbox"], "size": record.get("size"), "water_score_pct": 0.0, "selected": True}
                for record in tile_records if str(record["name"]) in feature_csvs
            ]
        if not feature_csvs or not selected_records:
            return [], None
        forecast_run_id = f"forecast_{anchor_date}"
        pipeline = AOIInferencePipeline(AOIInferenceConfig(
            aoi_bbox=list(self.config.aoi_bbox),
            target_date=anchor_date,
            output_root=self.run_dir / "runs",
            run_name=forecast_run_id,
            feature_data_root=self.run_dir / "feature_data",
            download_images=False,
            plot=self.config.plot,
            model_profile=self.config.model_profile,
            max_cloud_coverage=self.config.max_cloud_coverage,
        ))
        pipeline.run_dir.mkdir(parents=True, exist_ok=True)
        payload = pipeline.run_model_inference(
            selected_records=selected_records,
            feature_csvs=feature_csvs,
            water_manifest=water_manifest,
        )
        forecast_rows = flatten_forecast_rows(forecast_run_id, anchor_date, payload.get("tiles", {}))
        stac_result = StacCatalogExporter(self.run_dir / "stac_catalog", stac_base_url=self.config.stac_base_url).export(
            aoi_bbox=list(self.config.aoi_bbox),
            tile_records=tile_records,
            history_records=history_records,
            forecast_rows=forecast_rows,
            anchor_date=anchor_date,
            horizon_days=int(self.config.model_profile.horizon) * int(self.config.model_profile.cadence_days),
            processing_metadata={"version": self.config.model_profile.model_name},
        )
        return forecast_rows, stac_result.item_id

    def _write_feature_csvs(self, history_records: list[dict[str, Any]]) -> dict[str, str]:
        by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in history_records:
            if is_usable_observation(record):
                by_tile.setdefault(str(record["tile_id"]), []).append(record)
        feature_csvs = {}
        for tile_id, records in by_tile.items():
            records = sorted(records, key=lambda item: str(item["observation_date"]))
            if len(records) < int(self.config.model_profile.time_steps):
                continue
            csv_path = self.run_dir / "feature_data" / tile_id / "csv" / DEFAULT_FEATURE_CSV_NAME
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            rows = []
            for record in records:
                row = {"date": record["observation_date"]}
                for column in TARGET_COLS:
                    row[column] = record.get(column)
                    row[f"{column}_gpr_std"] = 0.0
                rows.append(row)
            pd.DataFrame(rows).to_csv(csv_path, index=False)
            feature_csvs[tile_id] = str(csv_path)
        return feature_csvs

    def _export_collection_stac(self, tile_records, history_records) -> None:
        StacCatalogExporter(self.run_dir / "stac_catalog", stac_base_url=self.config.stac_base_url).export(
            aoi_bbox=list(self.config.aoi_bbox),
            tile_records=tile_records,
            history_records=history_records,
            forecast_rows=[],
            anchor_date=None,
            horizon_days=int(self.config.model_profile.horizon) * int(self.config.model_profile.cadence_days),
        )

    @staticmethod
    def _log(message: str) -> None:
        print(f"[ScheduledPipeline] {message}", flush=True)

    @staticmethod
    def _slugify(value: str) -> str:
        slug = "".join(character.lower() if character.isalnum() else "_" for character in value.strip()).strip("_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        return slug or "scheduled_pipeline"


def compute_missing_dates(available_dates: list[str], collected_dates: list[str], max_days: int | None = None) -> list[str]:
    missing = sorted(set(available_dates) - set(collected_dates))
    return missing[: int(max_days)] if max_days is not None else missing


def compute_discovery_windows(*, history_start: str, target_date: str, collected_dates: list[str], state: dict[str, Any], chunk_days: int, backfill_all: bool = False) -> list[tuple[str, str]]:
    target = date.fromisoformat(target_date)
    if collected_dates:
        start = date.fromisoformat(max(collected_dates)) + timedelta(days=1)
        return [] if start > target else [(start.isoformat(), target.isoformat())]
    start = date.fromisoformat(str(state.get("backfill_cursor") or history_start)[:10])
    windows = []
    while start <= target:
        end = min(start + timedelta(days=max(1, int(chunk_days)) - 1), target)
        windows.append((start.isoformat(), end.isoformat()))
        if not backfill_all:
            break
        start = end + timedelta(days=1)
    return windows


def build_run_summary(*, new_data_available: bool, forecast_status: str, forecast_anchor: str | None, last_forecast_anchor: str | None) -> str:
    if not new_data_available and forecast_status == "already_forecasted":
        return f"No new satellite dates were available. Latest usable observation {forecast_anchor} is already forecasted; no collection or inference was needed."
    if not new_data_available and forecast_status == "no_usable_observation":
        return "No new satellite dates were available and no usable observation exists yet; forecast was not run."
    if not new_data_available and forecast_status == "disabled":
        return "No new satellite dates were available. Inference is disabled for this run."
    if forecast_status == "dry_run":
        return "Dry run found new satellite dates that would be collected." if new_data_available else "Dry run found no new satellite dates to collect."
    if new_data_available and forecast_status == "created":
        return f"New satellite data was collected and a forecast was created for anchor {forecast_anchor}."
    if new_data_available and forecast_status == "insufficient_usable_history":
        return "New satellite data was collected, but there is not enough usable history to forecast yet."
    if forecast_status == "created":
        return f"Forecast was created for anchor {forecast_anchor}."
    if forecast_status == "already_forecasted":
        return f"Latest usable observation {forecast_anchor} is already forecasted."
    return f"Scheduled run completed. latest_usable_observation={forecast_anchor or 'none'}, last_forecast_anchor={last_forecast_anchor or 'none'}, forecast_status={forecast_status}."


def latest_usable_observation_date(records: list[dict[str, Any]]) -> str | None:
    usable = sorted({str(record["observation_date"]) for record in records if is_usable_observation(record)})
    return usable[-1] if usable else None


def is_usable_observation(record: dict[str, Any]) -> bool:
    return record.get("water_status") == "water" and all(record.get(column) is not None for column in TARGET_COLS)


def build_history_record(*, tile_id: str, observed_date: str, bbox: list[float], metrics: dict[str, Any], water_scene: dict[str, Any], stac_item_ids: list[str]) -> dict[str, Any]:
    return collector_history_record(
        tile_id=tile_id,
        observed_date=observed_date,
        bbox=bbox,
        metrics=metrics,
        water_scene=water_scene,
        stac_item_ids=stac_item_ids,
    )


def flatten_forecast_rows(forecast_run_id: str, anchor_date: str, tile_payloads: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for tile_id, payload in sorted(tile_payloads.items()):
        if "error" in payload:
            continue
        for row in payload.get("forecast", {}).get("model", []):
            flattened = {
                "forecast_run_id": forecast_run_id,
                "anchor_date": anchor_date,
                "tile_id": tile_id,
                "forecast_date": normalize_date(row.get("date", "")),
                "step": int(row.get("step") or 0),
                "model": "model",
                "created_at": utc_now(),
            }
            flattened.update({column: safe_float(row.get(column)) for column in TARGET_COLS})
            rows.append(flattened)
    return rows


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the scheduled incremental TERRA UC1 pipeline.")
    parser.add_argument("--bbox", nargs="+", required=True, help="AOI bounding box: min_lon min_lat max_lon max_lat.")
    parser.add_argument("--run-name", required=True, help="Stable scheduled run name.")
    parser.add_argument("--output-root", default=str(DEFAULT_SCHEDULED_OUTPUT_ROOT))
    parser.add_argument("--history-start", default="2016-01-01")
    parser.add_argument("--target-date", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-days-per-run", type=int, default=None)
    parser.add_argument("--max-tiles-per-run", type=int, default=None)
    parser.add_argument("--discovery-chunk-days", type=int, default=31)
    parser.add_argument("--backfill-all", action="store_true")
    parser.add_argument("--stac-base-url", default=None)
    parser.add_argument("--skip-inference", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = ScheduledIncrementalPipeline(ScheduledPipelineConfig(
        aoi_bbox=parse_bbox(args.bbox),
        run_name=args.run_name,
        output_root=Path(args.output_root),
        history_start=args.history_start,
        target_date=args.target_date,
        dry_run=args.dry_run,
        max_days_per_run=args.max_days_per_run,
        max_tiles_per_run=args.max_tiles_per_run,
        discovery_chunk_days=args.discovery_chunk_days,
        backfill_all=args.backfill_all,
        stac_base_url=args.stac_base_url,
        run_inference=not args.skip_inference,
    )).execute()
    print(json.dumps(asdict(result), indent=2, allow_nan=False))
    return 0
