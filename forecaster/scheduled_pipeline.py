from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from forecast import (
    AOIInferenceConfig,
    AOIInferencePipeline,
    DEFAULT_FEATURE_CSV_NAME,
    ModelInferenceProfile,
    parse_bbox,
)
from forecaster.core.global_preprocessor import WATER_TARGET_COLS
from forecaster.data.collectors.sentinel2 import StatisticalCollection
from forecaster.stac_exporter import StacCatalogExporter
from forecaster.water_tile_selector import WaterTileSelector
from hydro.river_tile_extractor import RiverTileExtractor, RiverTileExtractorConfig


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCHEDULED_OUTPUT_ROOT = REPO_ROOT / "data" / "inference_runs"
STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
SENTINEL2_COLLECTION = "sentinel-2-l2a"
TARGET_COLS = tuple(WATER_TARGET_COLS)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def parse_metric_dates(values: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(values, format="%Y-%m-%d", errors="coerce")
    if parsed.isna().any():
        fallback = pd.to_datetime(values, dayfirst=True, errors="coerce")
        parsed = parsed.fillna(fallback)
    return parsed


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
        self.write(rows)
        return rows

    def write(self, records: list[dict[str, Any]]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(records, indent=2, allow_nan=False), encoding="utf-8")
        pd.DataFrame(records).to_csv(self.csv_path, index=False)


class ScheduledState:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "created_at": utc_now(),
                "known_stac_dates": [],
                "collected_dates": [],
                "failed_dates": [],
                "warnings": [],
                "last_forecast_anchor": None,
                "forecast_run_ids": [],
                "backfill_cursor": None,
                "last_collected_date": None,
            }
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = utc_now()
        self.path.write_text(json.dumps(state, indent=2, allow_nan=False), encoding="utf-8")


class CDSEStacDiscovery:
    def __init__(
        self,
        *,
        url: str = STAC_SEARCH_URL,
        max_cloud_coverage: int = 30,
        retry_sleep_seconds: int = 180,
        post=requests.post,
    ):
        self.url = url
        self.max_cloud_coverage = max_cloud_coverage
        self.retry_sleep_seconds = retry_sleep_seconds
        self.post = post

    def discover_dates(self, bbox: list[float], start_date: str, end_date: str) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
        payload = {
            "collections": [SENTINEL2_COLLECTION],
            "bbox": [float(item) for item in bbox],
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "limit": 100,
            "query": {"eo:cloud_cover": {"lte": int(self.max_cloud_coverage)}},
        }
        dates: set[str] = set()
        item_ids: dict[str, list[str]] = {}
        warnings: list[dict[str, Any]] = []
        next_href: str | None = self.url
        while next_href:
            response = self._post(next_href, payload)
            if response is None:
                warnings.append({"code": "STAC_DISCOVERY_UNAVAILABLE", "message": "STAC discovery did not return a response."})
                break
            response.raise_for_status()
            body = response.json()
            for feature in body.get("features", []):
                properties = feature.get("properties", {})
                observed = normalize_date(properties.get("datetime") or properties.get("start_datetime") or "")
                if not observed:
                    continue
                dates.add(observed)
                item_ids.setdefault(observed, []).append(str(feature.get("id") or ""))
            next_href = None
            for link in body.get("links", []):
                if link.get("rel") == "next" and link.get("href"):
                    next_href = str(link["href"])
                    break
            payload = body.get("context", {}).get("next") or payload
            if not isinstance(payload, dict):
                payload = {
                    "collections": [SENTINEL2_COLLECTION],
                    "bbox": [float(item) for item in bbox],
                    "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
                    "limit": 100,
                }
        return sorted(dates), item_ids, warnings

    def _post(self, url: str, payload: dict[str, Any], retries: int = 3):
        response = None
        for attempt in range(1, retries + 1):
            try:
                response = self.post(url, json=payload, timeout=120)
            except requests.RequestException as exc:
                if attempt == retries:
                    raise
                print(f"[STAC] Request failed ({exc}); retrying {attempt + 1}/{retries}.")
                time.sleep(min(self.retry_sleep_seconds, 5))
                continue
            if response.status_code in (403, 429) and attempt < retries:
                print(
                    f"[STAC] Rate limited ({response.status_code}); waiting "
                    f"{self.retry_sleep_seconds}s before retry {attempt + 1}/{retries}."
                )
                time.sleep(self.retry_sleep_seconds)
                continue
            return response
        return response


class ScheduledIncrementalPipeline:
    def __init__(self, config: ScheduledPipelineConfig, *, discovery: CDSEStacDiscovery | None = None):
        self.config = config
        self.run_dir = Path(config.output_root) / self._slugify(config.run_name)
        self.state = ScheduledState(self.run_dir / "state.json")
        self.history_store = JsonCsvStore(
            self.run_dir / "history" / "global_history.json",
            self.run_dir / "history" / "global_history.csv",
            ("tile_id", "observation_date"),
        )
        self.forecast_store = JsonCsvStore(
            self.run_dir / "forecasts" / "global_forecasts.json",
            self.run_dir / "forecasts" / "global_forecasts.csv",
            ("forecast_run_id", "tile_id", "forecast_date", "step"),
        )
        self.discovery = discovery or CDSEStacDiscovery(max_cloud_coverage=config.max_cloud_coverage)

    def execute(self) -> ScheduledPipelineResult:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state = self.state.load()
        warnings = list(state.get("warnings", []))
        target_date = self.config.target_date or date.today().isoformat()

        history_records = self.history_store.load()
        collected_dates = sorted(
            set(state.get("collected_dates", []))
            | {str(record["observation_date"]) for record in history_records}
        )
        discovery_windows = compute_discovery_windows(
            history_start=self.config.history_start,
            target_date=target_date,
            collected_dates=collected_dates,
            state=state,
            chunk_days=self.config.discovery_chunk_days,
            backfill_all=self.config.backfill_all,
        )
        if not discovery_windows:
            self._log(
                "No STAC discovery needed; local history is already current "
                f"through {target_date}."
            )
            available_dates, stac_item_ids, discovery_warnings = [], {}, []
        else:
            available_dates = []
            stac_item_ids = {}
            discovery_warnings = []
            self._log(
                f"Discovery will scan {len(discovery_windows)} window(s). "
                f"Use --discovery-chunk-days to adjust chunk size."
            )
            for index, (discovery_start, discovery_end) in enumerate(discovery_windows, start=1):
                self._log(
                    "Discovering Sentinel-2 STAC dates "
                    f"window {index}/{len(discovery_windows)}: {discovery_start} to {discovery_end}..."
                )
                window_dates, window_item_ids, window_warnings = self._discover_available_dates(
                    discovery_start=discovery_start,
                    discovery_end=discovery_end,
                    write_cache=not self.config.dry_run,
                )
                available_dates.extend(window_dates)
                for observed_date, item_ids in window_item_ids.items():
                    stac_item_ids.setdefault(observed_date, []).extend(item_ids)
                discovery_warnings.extend(window_warnings)
                self._log(f"Window {index}/{len(discovery_windows)} found {len(window_dates)} available date(s).")
            available_dates = sorted(set(available_dates))
            stac_item_ids = {key: sorted(set(values)) for key, values in stac_item_ids.items()}
            warnings.extend(discovery_warnings)
            self._log(
                f"STAC discovery found {len(available_dates)} available date(s) in this window; "
                f"{len(collected_dates)} date(s) are already in local history."
            )
            self._log(
                "Backfill/discovery cursor is chunked; repeated scheduled runs resume from state "
                "instead of rediscovering all history."
            )

        missing_dates = compute_missing_dates(
            available_dates=available_dates,
            collected_dates=collected_dates,
            max_days=self.config.max_days_per_run,
        )
        self._log(f"Missing dates selected for this run: {len(missing_dates)}.")

        if self.config.dry_run:
            run_summary = build_run_summary(
                new_data_available=bool(missing_dates),
                forecast_status="dry_run",
                forecast_anchor=None,
                last_forecast_anchor=state.get("last_forecast_anchor"),
            )
            self._log(run_summary)
            self._log("Dry run complete; no tiles, observations, forecasts, or STAC files were written.")
            return ScheduledPipelineResult(
                status="dry_run",
                run_dir=str(self.run_dir),
                run_summary=run_summary,
                available_dates=available_dates,
                missing_dates=missing_dates,
                collected_dates=[],
                new_data_available=bool(missing_dates),
                forecast_anchor=None,
                forecast_created=False,
                forecast_status="dry_run",
                last_forecast_anchor=state.get("last_forecast_anchor"),
                stac_item_id=None,
                discovery_windows=windows_to_dicts(discovery_windows),
                warnings=warnings,
            )

        self._log("Extracting river tiles for the AOI...")
        tile_records, tiles_geojson = self._extract_tiles()
        self._log(f"Extracted {len(tile_records)} tile record(s).")

        state["aoi_bbox"] = list(self.config.aoi_bbox)
        state["history_start"] = self.config.history_start
        state["known_stac_dates"] = sorted(set(state.get("known_stac_dates", [])) | set(available_dates))
        state["last_checked_at"] = utc_now()
        state["warnings"] = warnings[-200:]
        if discovery_windows:
            state["last_discovery_window"] = {"start_date": discovery_windows[-1][0], "end_date": discovery_windows[-1][1]}
            if not available_dates:
                state["backfill_cursor"] = next_date(discovery_windows[-1][1])

        collected_this_run: list[str] = []
        if missing_dates:
            tile_records = tile_records[: self.config.max_tiles_per_run] if self.config.max_tiles_per_run else tile_records
            self._log(
                f"Running water screening for {len(tile_records)} tile(s) "
                f"across {len(missing_dates)} missing date(s)..."
            )
            water_manifest = self._build_water_manifest(tiles_geojson, missing_dates)
            self._log("Collecting missing tile observations and writing history records...")
            new_records = self._collect_history_records(
                tile_records=tile_records,
                dates=missing_dates,
                water_manifest=water_manifest,
                stac_item_ids=stac_item_ids,
            )
            self._log(f"Collected {len(new_records)} tile-date record(s); updating JSON/CSV stores...")
            history_records = self.history_store.upsert(new_records)
            self._write_per_tile_history(history_records)
            collected_this_run = missing_dates
            collected_dates = sorted({str(record["observation_date"]) for record in history_records})
            state["collected_dates"] = sorted(set(state.get("collected_dates", [])) | set(collected_this_run))
            state["last_collected_date"] = collected_dates[-1] if collected_dates else None
            if discovery_windows:
                state["backfill_cursor"] = next_date(max(collected_this_run))
        else:
            self._log("No missing dates; reusing the latest available water manifest if present.")
            water_manifest = self._latest_water_manifest(tile_records)

        anchor_date = latest_usable_observation_date(history_records)
        self._log(f"Latest usable observation date: {anchor_date or 'none'}.")
        forecast_created = False
        forecast_status = "not_run"
        stac_item_id = None
        if self.config.run_inference and anchor_date and anchor_date != state.get("last_forecast_anchor"):
            self._log(f"Running forecast cycle anchored at {anchor_date}...")
            forecast_rows, stac_item_id = self._run_forecast_cycle(
                anchor_date=anchor_date,
                tile_records=tile_records,
                history_records=history_records,
                water_manifest=water_manifest,
            )
            if forecast_rows:
                self._log(f"Writing {len(forecast_rows)} forecast row(s) to global forecast stores...")
                self.forecast_store.upsert(forecast_rows)
                forecast_created = True
                forecast_status = "created"
                state["last_forecast_anchor"] = anchor_date
                state["forecast_run_ids"] = sorted(set(state.get("forecast_run_ids", [])) | {forecast_rows[0]["forecast_run_id"]})
            else:
                forecast_status = "insufficient_usable_history"
                self._log("Forecast cycle did not produce rows; likely not enough usable history yet.")
        elif self.config.run_inference and anchor_date == state.get("last_forecast_anchor"):
            forecast_status = "already_forecasted"
            self._log(
                "No new forecast needed: latest usable observation "
                f"{anchor_date} is already forecasted."
            )
        elif not self.config.run_inference:
            forecast_status = "disabled"
            self._log("Inference disabled by CLI flag; skipping forecast.")
        elif not anchor_date:
            forecast_status = "no_usable_observation"
            self._log("No forecast can run yet: no usable water observation is available in local history.")

        if not forecast_created:
            self._log("Exporting local STAC catalog/collection without a new forecast item...")
            StacCatalogExporter(
                self.run_dir / "stac_catalog",
                stac_base_url=self.config.stac_base_url,
            ).export(
                aoi_bbox=list(self.config.aoi_bbox),
                tile_records=tile_records,
                history_records=history_records,
                forecast_rows=[],
                anchor_date=None,
                horizon_days=int(self.config.model_profile.horizon) * int(self.config.model_profile.cadence_days),
            )

        self.state.save(state)
        run_summary = build_run_summary(
            new_data_available=bool(missing_dates),
            forecast_status=forecast_status,
            forecast_anchor=anchor_date,
            last_forecast_anchor=state.get("last_forecast_anchor"),
        )
        self._log(run_summary)
        self._log(f"Scheduled run complete. Output directory: {self.run_dir}")
        result = ScheduledPipelineResult(
            status="success",
            run_dir=str(self.run_dir),
            run_summary=run_summary,
            available_dates=available_dates,
            missing_dates=missing_dates,
            collected_dates=collected_this_run,
            new_data_available=bool(missing_dates),
            forecast_anchor=anchor_date,
            forecast_created=forecast_created,
            forecast_status=forecast_status,
            last_forecast_anchor=state.get("last_forecast_anchor"),
            stac_item_id=stac_item_id,
            discovery_windows=windows_to_dicts(discovery_windows),
            warnings=warnings,
        )
        (self.run_dir / "scheduled_run_result.json").write_text(
            json.dumps(asdict(result), indent=2, allow_nan=False),
            encoding="utf-8",
        )
        return result

    @staticmethod
    def _log(message: str) -> None:
        print(f"[ScheduledPipeline] {message}", flush=True)

    def _extract_tiles(self) -> tuple[list[dict[str, Any]], Path]:
        tiles_dir = self.run_dir / "tiles"
        tiles_geojson = tiles_dir / "river_tiles.geojson"
        tile_config_hash = self._tile_config_hash()
        tile_records_path = tiles_dir / "tile_records.json"
        tile_state_path = tiles_dir / "tile_state.json"
        if tiles_geojson.exists() and tile_records_path.exists() and tile_state_path.exists():
            tile_state = json.loads(tile_state_path.read_text(encoding="utf-8"))
            if tile_state.get("tile_config_hash") == tile_config_hash:
                self._log("Using cached river tiles; AOI/tile configuration has not changed.")
                return json.loads(tile_records_path.read_text(encoding="utf-8")), tiles_geojson

        extractor = RiverTileExtractor(
            RiverTileExtractorConfig(
                aoi_bbox=list(self.config.aoi_bbox),
                projected_crs=self.config.projected_crs,
                spacing_m=self.config.spacing_m,
                box_size_m=self.config.box_size_m,
                min_length_m=self.config.min_river_length_m,
            )
        )
        _tiles, _ = extractor.extract_to_geojson(tiles_geojson)
        geojson = json.loads(tiles_geojson.read_text(encoding="utf-8"))
        records = []
        for index, feature in enumerate(geojson.get("features", [])):
            properties = feature.get("properties") or {}
            tile_name = str(properties.get("name") or feature.get("id") or f"tile_{index}")
            bbox = feature_bbox(feature)
            records.append({"name": tile_name, "bbox": bbox, "feature": feature, "size": self.config.box_size_m})
        tiles_dir.mkdir(parents=True, exist_ok=True)
        tile_records_path.write_text(json.dumps(records, indent=2, allow_nan=False), encoding="utf-8")
        tile_state_path.write_text(
            json.dumps(
                {
                    "tile_config_hash": tile_config_hash,
                    "created_at": utc_now(),
                    "aoi_bbox": list(self.config.aoi_bbox),
                    "spacing_m": self.config.spacing_m,
                    "box_size_m": self.config.box_size_m,
                    "min_river_length_m": self.config.min_river_length_m,
                    "projected_crs": self.config.projected_crs,
                },
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        return records, tiles_geojson

    def _discover_available_dates(
        self,
        *,
        discovery_start: str,
        discovery_end: str,
        write_cache: bool = True,
    ) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
        cache_path = self._stac_cache_path(discovery_start, discovery_end)
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            self._log(f"Using cached STAC discovery window: {cache_path}")
            return (
                list(cached.get("available_dates", [])),
                dict(cached.get("stac_item_ids", {})),
                list(cached.get("warnings", [])),
            )

        available_dates, stac_item_ids, warnings = self.discovery.discover_dates(
            bbox=list(self.config.aoi_bbox),
            start_date=discovery_start,
            end_date=discovery_end,
        )
        if write_cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {
                        "created_at": utc_now(),
                        "aoi_bbox": list(self.config.aoi_bbox),
                        "start_date": discovery_start,
                        "end_date": discovery_end,
                        "available_dates": available_dates,
                        "stac_item_ids": stac_item_ids,
                        "warnings": warnings,
                    },
                    indent=2,
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
        return available_dates, stac_item_ids, warnings

    def _build_water_manifest(self, tiles_geojson: Path, dates: list[str]) -> dict[str, Any]:
        if not dates:
            return {"selected_tiles": [], "rejected_tiles": [], "tiles": []}
        start_date = min(dates)
        end_date = max(dates)
        cache_path = self.run_dir / "water" / f"water_selection_{start_date}_{end_date}.json"
        selector = WaterTileSelector(
            geojson_path=tiles_geojson,
            cache_path=cache_path,
            water_check_interval=(start_date, end_date),
            reference_last_n=0,
            threshold=self.config.water_threshold,
            min_auto_threshold_pct=self.config.water_min_auto_threshold_pct,
            max_cloud_coverage=self.config.max_cloud_coverage,
            refresh=self.config.refresh_water,
        )
        return selector.select_tiles()

    def _stac_cache_path(self, start_date: str, end_date: str) -> Path:
        return self.run_dir / "stac_cache" / f"{start_date}_{end_date}.json"

    def _tile_config_hash(self) -> str:
        payload = {
            "aoi_bbox": list(self.config.aoi_bbox),
            "spacing_m": self.config.spacing_m,
            "box_size_m": self.config.box_size_m,
            "min_river_length_m": self.config.min_river_length_m,
            "projected_crs": self.config.projected_crs,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _latest_water_manifest(self, tile_records: list[dict[str, Any]]) -> dict[str, Any]:
        candidates = sorted((self.run_dir / "water").glob("water_selection_*.json")) if (self.run_dir / "water").exists() else []
        if candidates:
            return json.loads(candidates[-1].read_text(encoding="utf-8"))
        return {
            "selected_tiles": [str(record["name"]) for record in tile_records],
            "rejected_tiles": [],
            "tiles": [
                {
                    "name": str(record["name"]),
                    "bbox": record.get("bbox"),
                    "size": record.get("size"),
                    "water_score_pct": 0.0,
                    "selected": True,
                    "scenes": [],
                }
                for record in tile_records
            ],
        }

    def _collect_history_records(
        self,
        *,
        tile_records: list[dict[str, Any]],
        dates: list[str],
        water_manifest: dict[str, Any],
        stac_item_ids: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        water_index = build_water_index(water_manifest)
        for tile in tile_records:
            tile_id = str(tile["name"])
            metrics = self._collect_tile_metrics(tile_id, list(tile["bbox"]), dates)
            for observed_date in dates:
                metric_values = metrics.get(observed_date, {})
                water_scene = water_index.get((tile_id, observed_date), {})
                record = build_history_record(
                    tile_id=tile_id,
                    observed_date=observed_date,
                    bbox=list(tile["bbox"]),
                    metrics=metric_values,
                    water_scene=water_scene,
                    stac_item_ids=stac_item_ids.get(observed_date, []),
                )
                records.append(record)
        return records

    def _collect_tile_metrics(self, tile_id: str, bbox: list[float], dates: list[str]) -> dict[str, dict[str, Any]]:
        if not dates:
            return {}
        work_dir = self.run_dir / "collector_work" / tile_id / f"{min(dates)}_{max(dates)}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        collector = StatisticalCollection(
            time_interval=(min(dates), max(dates)),
            bbox=bbox,
            dir=str(work_dir),
            max_cloud_coverage=int(self.config.max_cloud_coverage),
        )
        collector.run(
            json_output_folder=str(work_dir / "statistical"),
            csv_output_folder=str(work_dir / "csv"),
        )
        metrics_csv = work_dir / "csv" / "mean_metrics.csv"
        if not metrics_csv.exists():
            return {}
        data = pd.read_csv(metrics_csv)
        if "date" not in data.columns:
            return {}
        data["date"] = parse_metric_dates(data["date"]).dt.date.astype(str)
        metrics: dict[str, dict[str, Any]] = {}
        for _, row in data.iterrows():
            row_date = str(row["date"])
            if row_date not in set(dates):
                continue
            metrics[row_date] = {
                column: safe_float(row.get(column))
                for column in TARGET_COLS
                if column in data.columns
            }
        return metrics

    def _write_per_tile_history(self, history_records: list[dict[str, Any]]) -> None:
        tile_root = self.run_dir / "history" / "tiles"
        by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in history_records:
            by_tile.setdefault(str(record["tile_id"]), []).append(record)
        for tile_id, records in by_tile.items():
            store = JsonCsvStore(tile_root / tile_id / "history.json", tile_root / tile_id / "history.csv", ("tile_id", "observation_date"))
            store.write(sorted(records, key=lambda item: str(item["observation_date"])))

    def _run_forecast_cycle(
        self,
        *,
        anchor_date: str,
        tile_records: list[dict[str, Any]],
        history_records: list[dict[str, Any]],
        water_manifest: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str | None]:
        feature_csvs = self._write_feature_csvs(history_records)
        selected_records = [record for record in water_manifest.get("tiles", []) if str(record.get("name")) in feature_csvs]
        if not selected_records:
            selected_records = [
                {
                    "name": record["name"],
                    "bbox": record["bbox"],
                    "size": record.get("size"),
                    "water_score_pct": 0.0,
                    "selected": True,
                }
                for record in tile_records
                if str(record["name"]) in feature_csvs
            ]
        if not feature_csvs or not selected_records:
            return [], None

        forecast_run_id = f"forecast_{anchor_date}"
        pipeline = AOIInferencePipeline(
            AOIInferenceConfig(
                aoi_bbox=list(self.config.aoi_bbox),
                target_date=anchor_date,
                output_root=self.run_dir / "runs",
                run_name=forecast_run_id,
                feature_data_root=self.run_dir / "feature_data",
                download_images=False,
                plot=self.config.plot,
                model_profile=self.config.model_profile,
                max_cloud_coverage=self.config.max_cloud_coverage,
            )
        )
        pipeline.run_dir.mkdir(parents=True, exist_ok=True)
        payload = pipeline.run_model_inference(
            selected_records=selected_records,
            feature_csvs=feature_csvs,
            water_manifest=water_manifest,
        )
        forecast_rows = flatten_forecast_rows(forecast_run_id, anchor_date, payload.get("tiles", {}))
        stac_result = StacCatalogExporter(
            self.run_dir / "stac_catalog",
            stac_base_url=self.config.stac_base_url,
        ).export(
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
        feature_csvs: dict[str, str] = {}
        by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in history_records:
            if is_usable_observation(record):
                by_tile.setdefault(str(record["tile_id"]), []).append(record)
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

    @staticmethod
    def _slugify(value: str) -> str:
        keep = [character.lower() if character.isalnum() else "_" for character in value.strip()]
        slug = "".join(keep).strip("_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        return slug or "scheduled_pipeline"


def compute_missing_dates(available_dates: list[str], collected_dates: list[str], max_days: int | None = None) -> list[str]:
    missing = sorted(set(available_dates) - set(collected_dates))
    if max_days is not None:
        return missing[: int(max_days)]
    return missing


def compute_discovery_windows(
    *,
    history_start: str,
    target_date: str,
    collected_dates: list[str],
    state: dict[str, Any],
    chunk_days: int,
    backfill_all: bool = False,
) -> list[tuple[str, str]]:
    target = date.fromisoformat(target_date)
    if collected_dates:
        start = date.fromisoformat(max(collected_dates)) + timedelta(days=1)
        if start > target:
            return []
        return [(start.isoformat(), target.isoformat())]

    cursor = state.get("backfill_cursor") or history_start
    start = date.fromisoformat(str(cursor)[:10])
    if start > target:
        return []
    span = max(1, int(chunk_days))
    windows = []
    while start <= target:
        end = min(start + timedelta(days=span - 1), target)
        windows.append((start.isoformat(), end.isoformat()))
        if not backfill_all:
            break
        start = end + timedelta(days=1)
    return windows


def next_date(value: str) -> str:
    return (date.fromisoformat(value[:10]) + timedelta(days=1)).isoformat()


def windows_to_dicts(windows: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"start_date": start, "end_date": end} for start, end in windows]


def build_run_summary(
    *,
    new_data_available: bool,
    forecast_status: str,
    forecast_anchor: str | None,
    last_forecast_anchor: str | None,
) -> str:
    if not new_data_available and forecast_status == "already_forecasted":
        return (
            "No new satellite dates were available. Latest usable observation "
            f"{forecast_anchor} is already forecasted; no collection or inference was needed."
        )
    if not new_data_available and forecast_status == "no_usable_observation":
        return "No new satellite dates were available and no usable observation exists yet; forecast was not run."
    if not new_data_available and forecast_status == "disabled":
        return "No new satellite dates were available. Inference is disabled for this run."
    if not new_data_available and forecast_status == "dry_run":
        return "Dry run found no new satellite dates to collect."
    if new_data_available and forecast_status == "created":
        return f"New satellite data was collected and a forecast was created for anchor {forecast_anchor}."
    if new_data_available and forecast_status == "insufficient_usable_history":
        return "New satellite data was collected, but there is not enough usable history to forecast yet."
    if forecast_status == "dry_run":
        return "Dry run found new satellite dates that would be collected."
    if forecast_status == "created":
        return f"Forecast was created for anchor {forecast_anchor}."
    if forecast_status == "already_forecasted":
        return f"Latest usable observation {forecast_anchor} is already forecasted."
    return (
        "Scheduled run completed. "
        f"latest_usable_observation={forecast_anchor or 'none'}, "
        f"last_forecast_anchor={last_forecast_anchor or 'none'}, "
        f"forecast_status={forecast_status}."
    )


def latest_usable_observation_date(records: list[dict[str, Any]]) -> str | None:
    usable = sorted({str(record["observation_date"]) for record in records if is_usable_observation(record)})
    return usable[-1] if usable else None


def is_usable_observation(record: dict[str, Any]) -> bool:
    if record.get("water_status") != "water":
        return False
    return all(record.get(column) is not None for column in TARGET_COLS)


def build_water_index(manifest: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for tile in manifest.get("tiles", []):
        tile_id = str(tile.get("name"))
        for scene in tile.get("scenes", []) or []:
            scene_date = normalize_date(scene.get("date", ""))
            if scene_date:
                index[(tile_id, scene_date)] = scene
    return index


def build_history_record(
    *,
    tile_id: str,
    observed_date: str,
    bbox: list[float],
    metrics: dict[str, Any],
    water_scene: dict[str, Any],
    stac_item_ids: list[str],
) -> dict[str, Any]:
    water_pct = safe_float(water_scene.get("water_pct"))
    cloud_pct = safe_float(water_scene.get("cloud_pct"))
    valid_pixels = int(water_scene.get("valid_pixels") or 0)
    water_status = "unknown"
    if water_scene:
        water_status = "water" if water_pct and water_pct > 0 and valid_pixels > 0 else "no_water"
    flags = []
    if cloud_pct is not None and cloud_pct > 30:
        flags.append("cloudy")
    if valid_pixels == 0:
        flags.append("no_valid_pixels")
    if any(metrics.get(column) is None for column in TARGET_COLS):
        flags.append("missing_metrics")

    record = {
        "tile_id": tile_id,
        "observation_date": observed_date,
        "bbox": bbox,
        "water_status": water_status,
        "water_pct": water_pct,
        "cloud_pct": cloud_pct,
        "valid_pixels": valid_pixels,
        "source_scene_count": 1 if water_scene else 0,
        "stac_item_ids": stac_item_ids,
        "asset_paths": {},
        "quality_flags": sorted(set(flags)),
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    for column in TARGET_COLS:
        record[column] = safe_float(metrics.get(column))
    return record


def flatten_forecast_rows(forecast_run_id: str, anchor_date: str, tile_payloads: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
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
            for column in TARGET_COLS:
                flattened[column] = safe_float(row.get(column))
            rows.append(flattened)
    return rows


def feature_bbox(feature: dict[str, Any]) -> list[float]:
    coordinates = feature.get("geometry", {}).get("coordinates", [[]])[0]
    lons = [float(point[0]) for point in coordinates]
    lats = [float(point[1]) for point in coordinates]
    return [min(lons), min(lats), max(lons), max(lats)]


def safe_float(value: Any) -> float | None:
    if value in (None, "", "NaN"):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(numeric):
        return None
    return numeric


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the scheduled incremental TERRA UC1 pipeline.")
    parser.add_argument("--bbox", nargs="+", required=True, help="AOI bounding box: min_lon min_lat max_lon max_lat.")
    parser.add_argument("--run-name", required=True, help="Stable scheduled run name.")
    parser.add_argument("--output-root", default=str(DEFAULT_SCHEDULED_OUTPUT_ROOT))
    parser.add_argument("--history-start", default="2016-01-01")
    parser.add_argument("--target-date", default=None, help="Override scheduler date for testing/backfills.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-days-per-run", type=int, default=None)
    parser.add_argument("--max-tiles-per-run", type=int, default=None)
    parser.add_argument(
        "--discovery-chunk-days",
        type=int,
        default=31,
        help="Maximum STAC discovery window for first historical backfill chunks.",
    )
    parser.add_argument(
        "--backfill-all",
        action="store_true",
        help="For first historical setup, scan all discovery chunks through the target date in one run.",
    )
    parser.add_argument("--stac-base-url", default=None)
    parser.add_argument("--skip-inference", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
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
        )
    )
    result = pipeline.execute()
    print(json.dumps(asdict(result), indent=2, allow_nan=False))
    return 0
