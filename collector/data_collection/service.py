from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

from .collectors.sentinel2 import StatisticalCollection
from .credentials import load_local_env_if_present
from .discovery import CDSEStacDiscovery
from .models import CollectionRequest, CollectionResult
from .river_tiles import RiverTileExtractor, RiverTileExtractorConfig
from .remote_storage import CollectorStorageSettings, CollectorStore, Sentinel3Store, build_aoi_definition
from .collectors.sentinel3 import Sentinel3Collection
from .storage import (
    TARGET_COLUMNS,
    CURRENT_COLLECTION_METHOD,
    HistoryStore,
    is_terminal,
    has_complete_metrics,
    read_json,
    safe_float,
    utc_now,
    write_json,
)


class CollectionService:
    def __init__(
        self,
        *,
        discovery_factory: Callable[[int], Any] | None = None,
        statistics_factory: Callable[..., Any] = StatisticalCollection,
        tile_extractor_factory: Callable[[RiverTileExtractorConfig], Any] = RiverTileExtractor,
        storage: CollectorStore | None = None,
        sentinel3_factory: Callable[..., Any] = Sentinel3Collection,
    ):
        self.discovery_factory = discovery_factory or (lambda cloud: CDSEStacDiscovery(max_cloud_coverage=cloud))
        self.statistics_factory = statistics_factory
        self.sentinel3_factory = sentinel3_factory
        self.tile_extractor_factory = tile_extractor_factory
        self.storage = storage
        self.request: CollectionRequest | None = None
        self.run_dir = Path()
        self.log_path = Path()
        self.execution_id = ""

    def collect(self, request: CollectionRequest) -> CollectionResult:
        self.request = request
        self.run_dir = request.output_path / _slugify(request.run_name)
        if request.sensor == "sentinel3":
            self.run_dir = self.run_dir / "sentinel3"
        self.log_path = self.run_dir / "logs" / "collector.jsonl"
        if request.dry_run:
            return self._execute()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with self._run_lock():
            if not request.publish:
                return self._execute()
            load_local_env_if_present()
            aoi_id = request.aoi_id or _slugify(request.run_name)
            self.execution_id = f"collector-run-{utc_now().replace(':', '')}-{uuid.uuid4().hex[:8]}"
            store_type = Sentinel3Store if request.sensor == "sentinel3" else CollectorStore
            storage = self.storage or store_type(CollectorStorageSettings.from_env(aoi_id=aoi_id))
            self.storage = storage
            try:
                storage.initialize()
                storage.ensure_aoi_definition(build_aoi_definition(
                    aoi_id=aoi_id,
                    bbox=list(request.aoi_bbox),
                    projected_crs=request.projected_crs,
                    spacing_m=request.spacing_m,
                    box_size_m=request.box_size_m,
                    min_river_length_m=request.min_river_length_m,
                ))
                self._hydrate_remote_collection()
                storage.record_run({
                    "run_name": request.run_name,
                    "status": "running",
                    "phase": "collection",
                    "mode": request.mode,
                }, run_id=self.execution_id)
                result = self._execute()
                artifacts = self._publish_collection(result)
                storage.record_run({
                    **result.to_dict(),
                    "status": result.status,
                    "phase": "complete",
                    "artifacts": artifacts,
                }, run_id=self.execution_id)
                return result
            except Exception as exc:
                self._record_failed_run(exc)
                raise
            finally:
                storage.close()

    def _hydrate_remote_collection(self) -> None:
        """Restore collector-owned state when the local run directory is new."""
        assert self.storage is not None
        history_path = self.run_dir / "history" / "global_history.json"
        if not history_path.exists():
            records = self.storage.load_observations()
            valid_records = [
                record
                for record in records
                if record.get("tile_id") not in (None, "") and record.get("observation_date") not in (None, "")
            ]
            skipped_records = len(records) - len(valid_records)
            if skipped_records:
                self._log(
                    f"Skipping {skipped_records} malformed remote observation record(s) without tile_id or observation_date.",
                    level="warning",
                )
            records = valid_records
            if records:
                HistoryStore(history_path, self.run_dir / "history" / "global_history.csv").write(records)

        remote_files = {
            "collection/state.json": "collection_state.json",
            "tiles/tile_records.json": "tiles/tile_records.json",
            "tiles/tile_state.json": "tiles/tile_state.json",
            "tiles/river_tiles.geojson": "tiles/river_tiles.geojson",
        }
        for local_relative, remote_relative in remote_files.items():
            path = self.run_dir / local_relative
            if path.exists():
                continue
            value = self.storage.download_json(key=self.storage.aoi_key(relative_path=remote_relative))
            if value is not None:
                write_json(path, value)

    def _record_failed_run(self, exc: Exception) -> None:
        if self.storage is None or not self.execution_id:
            return
        try:
            self.storage.record_run({
                "run_name": self.request.run_name if self.request else None,
                "status": "failed",
                "phase": "collection",
                "error_type": type(exc).__name__,
                "error": str(exc)[:2000],
            }, run_id=self.execution_id)
        except Exception:
            # Preserve the original collection or publishing error.
            pass

    def _publish_collection(self, result: CollectionResult) -> list[dict[str, Any]]:
        """Publish collector-owned canonical data and invocation artifacts."""
        assert self.storage is not None
        artifacts: list[dict[str, Any]] = []

        def add(artifact: dict[str, Any]) -> dict[str, Any]:
            artifacts.append(artifact)
            return artifact

        stable_files = {
            "collection/state.json": ("collection_state.json", "application/json"),
            "tiles/river_tiles.geojson": ("tiles/river_tiles.geojson", "application/geo+json"),
            "tiles/tile_records.json": ("tiles/tile_records.json", "application/json"),
            "tiles/tile_state.json": ("tiles/tile_state.json", "application/json"),
        }
        stable_artifacts: dict[str, dict[str, Any]] = {}
        for local_relative, (remote_relative, content_type) in stable_files.items():
            path = self.run_dir / local_relative
            if path.exists():
                stable_artifacts[local_relative] = add(self.storage.upload_file_if_changed(
                    path,
                    key=self.storage.aoi_key(relative_path=remote_relative),
                    content_type=content_type,
                ))

        collection_state = read_json(self.run_dir / "collection" / "state.json", {})
        if collection_state:
            self.storage.upsert_collection_state(collection_state, run_id=self.execution_id)

        history = read_json(Path(result.history_json_path), []) if result.history_json_path else []
        by_date: dict[str, list[dict[str, Any]]] = {}
        for record in history:
            by_date.setdefault(str(record["observation_date"]), []).append(record)
        observation_artifacts: dict[str, dict[str, Any]] = {}
        for observation_date, records in sorted(by_date.items()):
            observation_artifacts[observation_date] = add(self.storage.upload_json_if_changed(
                records,
                key=self.storage.data_key(relative_path=f"observations/{observation_date}.json"),
            ))
        self.storage.upsert_observations([
            {**record, "artifact": observation_artifacts[str(record["observation_date"])]}
            for record in history
        ], run_id=self.execution_id)

        tiles = read_json(Path(result.tile_records_path), []) if result.tile_records_path else []
        tile_artifact = stable_artifacts.get("tiles/tile_records.json", {})
        self.storage.upsert_tiles([
            {**record, "artifact": tile_artifact} for record in tiles
        ], run_id=self.execution_id)

        run_files = {
            "collection/collection_run_result.json": "application/json",
            "collection/collection_input_manifest.json": "application/json",
            "logs/collector.jsonl": "application/x-ndjson",
        }
        for relative, content_type in run_files.items():
            path = self.run_dir / relative
            if not path.exists():
                continue
            key = self.storage.run_key(run_id=self.execution_id, relative_path=relative)
            if content_type == "application/json":
                add(self.storage.upload_json_file(path, key=key))
            else:
                add(self.storage.upload_file(path, key=key, content_type=content_type))
        return artifacts

    def _execute(self) -> CollectionResult:
        assert self.request is not None
        if self.request.sensor == "sentinel3":
            from .sentinel3_service import execute
            return execute(self)
        request = self.request
        target_date = request.target_date or date.today().isoformat()
        state, migrated = self._load_state()
        history_store = HistoryStore(
            self.run_dir / "history" / "global_history.json",
            self.run_dir / "history" / "global_history.csv",
        )
        raw_history = history_store.load()
        history = upgrade_history_records(raw_history)
        if history != raw_history and not request.dry_run:
            history_store.write(history)
            self._write_per_tile_history(history)
        windows = compute_discovery_windows(request, state, target_date)
        self._log(f"Collection mode: {request.mode}; discovery window count: {len(windows)}.")

        discovered_dates: list[str] = []
        item_ids: dict[str, list[str]] = {}
        warnings = list(state.get("warnings", []))
        discovery = self.discovery_factory(request.max_cloud_coverage)
        for index, (start_date, end_date) in enumerate(windows, start=1):
            self._log(f"Discovering Sentinel-2 dates {index}/{len(windows)}: {start_date} to {end_date}.")
            dates, ids, window_warnings = self._discover_window(discovery, start_date, end_date)
            discovered_dates.extend(dates)
            for observed, values in ids.items():
                item_ids.setdefault(observed, []).extend(values)
            warnings.extend(window_warnings)

        known_dates = sorted(set(state.get("known_stac_dates", [])) | set(discovered_dates))
        if request.mode == "incremental" or state.get("backfill_complete"):
            candidate_dates = sorted(set(discovered_dates) | set(_incomplete_known_dates(known_dates, history, state)))
        else:
            candidate_dates = known_dates

        if request.dry_run:
            missing = _dates_missing_any_unit(candidate_dates, history, state.get("expected_tile_ids", []))
            if request.max_days_per_run:
                missing = missing[: request.max_days_per_run]
            summary = f"Dry run found {len(missing)} date(s) that require collection; no files were written."
            self._log(summary, persist=False)
            return self._result(
                status="dry_run",
                summary=summary,
                available_dates=sorted(set(discovered_dates)),
                missing_dates=missing,
                state=state,
                windows=windows,
                warnings=warnings,
            )

        tile_records, tiles_geojson, tile_records_path = self._extract_tiles()
        expected_tile_ids = [str(tile["name"]) for tile in tile_records]
        state["expected_tile_ids"] = expected_tile_ids
        missing_dates = _dates_missing_any_unit(candidate_dates, history, expected_tile_ids)
        if request.max_days_per_run:
            missing_dates = missing_dates[: request.max_days_per_run]
        self._log(f"{len(missing_dates)} date(s) require collection across {len(expected_tile_ids)} tile(s).")

        failed_units: list[dict[str, Any]] = []
        records_written = 0
        collected_dates: set[str] = set()
        if missing_dates:
            tiles_to_process = [
                tile for tile in tile_records
                if any(not _has_terminal(history, str(tile["name"]), observed) for observed in missing_dates)
            ]
            if request.max_tiles_per_run:
                tiles_to_process = tiles_to_process[: request.max_tiles_per_run]
            for tile_number, tile in enumerate(tiles_to_process, start=1):
                tile_id = str(tile["name"])
                tile_dates = [observed for observed in missing_dates if not _has_terminal(history, tile_id, observed)]
                if not tile_dates:
                    continue
                self._log(f"Collecting tile {tile_number}/{len(tiles_to_process)} ({tile_id}) for {len(tile_dates)} date(s).")
                try:
                    metrics = self._collect_metrics(tile_id, list(tile["bbox"]), tile_dates)
                except Exception as exc:
                    for observed in tile_dates:
                        failed_units.append({
                            "tile_id": tile_id,
                            "observation_date": observed,
                            "retryable": True,
                            "code": "COLLECTION_REQUEST_FAILED",
                            "message": str(exc),
                        })
                    self._log(f"Collection failed for {tile_id}; units remain retryable: {exc}", level="warning")
                    continue
                new_records = [
                    build_history_record(
                        tile_id=tile_id,
                        observed_date=observed,
                        bbox=list(tile["bbox"]),
                        metrics=metrics.get(observed, {}),
                        stac_item_ids=item_ids.get(observed) or state.get("stac_item_ids", {}).get(observed, []),
                        previous=_find_record(history, tile_id, observed),
                    )
                    for observed in tile_dates
                ]
                history, changed = history_store.upsert(new_records)
                records_written += changed
                collected_dates.update(tile_dates)
                self._write_per_tile_history(history)
                state["updated_at"] = utc_now()
                state["failed_units"] = failed_units
                self._save_state(state)

        complete_dates = _complete_dates(known_dates, history, expected_tile_ids)
        state.update({
            "schema_version": "1.0.0",
            "aoi_bbox": list(request.aoi_bbox),
            "history_start": request.history_start,
            "known_stac_dates": known_dates,
            "stac_item_ids": _merge_item_ids(state.get("stac_item_ids", {}), item_ids),
            "completed_dates": complete_dates,
            "last_collected_date": complete_dates[-1] if complete_dates else None,
            "last_checked_date": target_date,
            "last_checked_at": utc_now(),
            "failed_units": failed_units,
            "warnings": warnings[-200:],
            "legacy_state_migrated": migrated or bool(state.get("legacy_state_migrated")),
        })
        if windows and windows[-1][1] >= target_date:
            state["backfill_complete"] = True
        self._save_state(state)

        latest = latest_available_observation(history)
        if missing_dates and records_written:
            summary = f"Collected or updated {records_written} tile-date record(s); latest available observation is {latest or 'none'}."
        elif missing_dates and failed_units:
            summary = f"No records were written; {len(failed_units)} tile-date unit(s) remain retryable."
        else:
            summary = f"No new satellite data required collection; latest available observation is {latest or 'none'}."
        self._log(summary)
        result = self._result(
            status="partial" if failed_units else "success",
            summary=summary,
            available_dates=sorted(set(discovered_dates)),
            missing_dates=missing_dates,
            collected_dates=sorted(collected_dates),
            new_record_count=records_written,
            failed_units=failed_units,
            latest=latest,
            state=state,
            windows=windows,
            warnings=warnings,
            tile_records_path=tile_records_path,
            tiles_geojson_path=tiles_geojson,
        )
        write_json(self.run_dir / "collection" / "collection_run_result.json", result.to_dict())
        # This is the stable handoff descriptor for a separately deployed
        # forecaster.  It contains only routing/geometry metadata; the actual
        # observations remain in the history artifacts named by the result.
        write_json(self.run_dir / "collection" / "collection_input_manifest.json", {
            "schema_version": "1.0.0",
            "aoi_id": request.aoi_id or _slugify(request.run_name),
            "aoi_bbox": list(request.aoi_bbox),
            "projected_crs": request.projected_crs,
            "run_name": request.run_name,
            "history_json_path": result.history_json_path,
            "tile_records_path": result.tile_records_path,
            "collection_result_path": str(self.run_dir / "collection" / "collection_run_result.json"),
        })
        return result

    def _load_state(self) -> tuple[dict[str, Any], bool]:
        state_path = self.run_dir / "collection" / "state.json"
        if state_path.exists():
            return read_json(state_path, {}), False
        legacy = read_json(self.run_dir / "state.json", {})
        if legacy:
            migrated = {
                "schema_version": "1.0.0",
                "created_at": legacy.get("created_at", utc_now()),
                "known_stac_dates": list(legacy.get("known_stac_dates", [])),
                "completed_dates": list(legacy.get("collected_dates", [])),
                "last_collected_date": legacy.get("last_collected_date"),
                "history_start": legacy.get("history_start"),
                "warnings": list(legacy.get("warnings", [])),
                "legacy_state_migrated": True,
                "backfill_complete": bool(legacy.get("collected_dates")),
            }
            self._log("Migrated legacy scheduled collection state without modifying the legacy file.")
            return migrated, True
        return {
            "schema_version": "1.0.0",
            "created_at": utc_now(),
            "known_stac_dates": [],
            "completed_dates": [],
            "failed_units": [],
            "warnings": [],
            "backfill_complete": False,
        }, False

    def _save_state(self, state: dict[str, Any]) -> None:
        state["updated_at"] = utc_now()
        write_json(self.run_dir / "collection" / "state.json", state)

    def _discover_window(self, discovery: Any, start_date: str, end_date: str):
        cache = self.run_dir / "cdse_stac_cache" / f"{start_date}_{end_date}.json"
        legacy_cache = self.run_dir / "stac_cache" / f"{start_date}_{end_date}.json"
        if cache.exists():
            value = read_json(cache, {})
            self._log(f"Using cached CDSE STAC discovery window: {cache}")
            return value.get("available_dates", []), value.get("stac_item_ids", {}), value.get("warnings", [])
        if legacy_cache.exists():
            value = read_json(legacy_cache, {})
            self._log(f"Using legacy cached CDSE STAC discovery window: {legacy_cache}")
            if not self.request.dry_run:
                write_json(cache, value)
            return value.get("available_dates", []), value.get("stac_item_ids", {}), value.get("warnings", [])
        dates, item_ids, warnings = discovery.discover_dates(list(self.request.aoi_bbox), start_date, end_date)
        if not self.request.dry_run:
            write_json(cache, {
                "created_at": utc_now(),
                "aoi_bbox": list(self.request.aoi_bbox),
                "start_date": start_date,
                "end_date": end_date,
                "available_dates": dates,
                "stac_item_ids": item_ids,
                "warnings": warnings,
            })
        return dates, item_ids, warnings

    def _extract_tiles(self) -> tuple[list[dict[str, Any]], Path, Path]:
        tiles_dir = self.run_dir / "tiles"
        geojson_path = tiles_dir / "river_tiles.geojson"
        records_path = tiles_dir / "tile_records.json"
        state_path = tiles_dir / "tile_state.json"
        config_hash = self._tile_config_hash()
        tile_state = read_json(state_path, {})
        if geojson_path.exists() and records_path.exists() and tile_state.get("tile_config_hash") == config_hash:
            self._log("Using cached river tiles.")
            return read_json(records_path, []), geojson_path, records_path
        extractor = self.tile_extractor_factory(RiverTileExtractorConfig(
            aoi_bbox=list(self.request.aoi_bbox),
            projected_crs=self.request.projected_crs,
            spacing_m=self.request.spacing_m,
            box_size_m=self.request.box_size_m,
            min_length_m=self.request.min_river_length_m,
        ))
        extractor.extract_to_geojson(geojson_path)
        geojson = read_json(geojson_path, {})
        records = []
        for index, feature in enumerate(geojson.get("features", [])):
            properties = feature.get("properties") or {}
            records.append({
                "name": str(properties.get("name") or feature.get("id") or f"tile_{index}"),
                "bbox": feature_bbox(feature),
                "feature": feature,
                "size": self.request.box_size_m,
            })
        write_json(records_path, records)
        write_json(state_path, {"tile_config_hash": config_hash, "created_at": utc_now()})
        return records, geojson_path, records_path

    def _tile_config_hash(self) -> str:
        value = {
            "aoi_bbox": list(self.request.aoi_bbox),
            "spacing_m": self.request.spacing_m,
            "box_size_m": self.request.box_size_m,
            "min_river_length_m": self.request.min_river_length_m,
            "projected_crs": self.request.projected_crs,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    def _collect_metrics(self, tile_id: str, bbox: list[float], dates: list[str]) -> dict[str, dict[str, Any]]:
        work_dir = self.run_dir / "collector_work" / tile_id / f"{min(dates)}_{max(dates)}"
        if work_dir.exists():
            shutil.rmtree(work_dir)
        collector = self.statistics_factory(
            time_interval=(min(dates), max(dates)),
            bbox=bbox,
            dir=str(work_dir),
            max_cloud_coverage=self.request.max_cloud_coverage,
        )
        collector.run(str(work_dir / "statistical"), str(work_dir / "csv"))
        failures = list(getattr(collector, "failures", []))
        if failures:
            raise RuntimeError(f"CDSE statistics request failed for {len(failures)} interval(s)")
        csv_path = work_dir / "csv" / "mean_metrics.csv"
        if not csv_path.exists():
            return {}
        frame = pd.read_csv(csv_path)
        if "date" not in frame:
            return {}
        parsed = pd.to_datetime(frame["date"], format="%Y-%m-%d", errors="coerce")
        parsed = parsed.fillna(pd.to_datetime(frame["date"], dayfirst=True, errors="coerce"))
        frame["date"] = parsed.dt.date.astype(str)
        requested = set(dates)
        return {
            str(row["date"]): {column: safe_float(row.get(column)) for column in TARGET_COLUMNS}
            for _, row in frame.iterrows()
            if str(row["date"]) in requested
        }

    def _write_per_tile_history(self, history: list[dict[str, Any]]) -> None:
        by_tile: dict[str, list[dict[str, Any]]] = {}
        for record in history:
            by_tile.setdefault(str(record["tile_id"]), []).append(record)
        for tile_id, records in by_tile.items():
            store = HistoryStore(
                self.run_dir / "history" / "tiles" / tile_id / "history.json",
                self.run_dir / "history" / "tiles" / tile_id / "history.csv",
            )
            store.write(sorted(records, key=lambda record: str(record["observation_date"])))

    def _result(self, *, status: str, summary: str, state: dict[str, Any], windows: list[tuple[str, str]], warnings: list[dict[str, Any]], available_dates=None, missing_dates=None, collected_dates=None, new_record_count=0, failed_units=None, latest=None, tile_records_path=None, tiles_geojson_path=None) -> CollectionResult:
        return CollectionResult(
            status=status,
            run_dir=str(self.run_dir),
            run_summary=summary,
            mode=self.request.mode,
            available_dates=available_dates or [],
            missing_dates=missing_dates or [],
            collected_dates=collected_dates or [],
            new_record_count=new_record_count,
            failed_units=failed_units or [],
            latest_available_observation=latest,
            history_json_path=str(self.run_dir / "history" / "global_history.json"),
            history_csv_path=str(self.run_dir / "history" / "global_history.csv"),
            tile_records_path=str(tile_records_path) if tile_records_path else None,
            tiles_geojson_path=str(tiles_geojson_path) if tiles_geojson_path else None,
            state_path=str(self.run_dir / "collection" / "state.json"),
            discovery_windows=[{"start_date": start, "end_date": end} for start, end in windows],
            warnings=warnings,
        )

    def _log(self, message: str, *, level: str = "info", persist: bool = True) -> None:
        print(f"[DataCollection] {message}", flush=True)
        if persist and not self.request.dry_run:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"timestamp": utc_now(), "level": level, "message": message}) + "\n")

    @contextmanager
    def _run_lock(self) -> Iterator[None]:
        lock = self.run_dir / "collection" / ".run.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"Collection is already running for {self.run_dir}") from exc
        try:
            os.write(descriptor, f"pid={os.getpid()} started={utc_now()}\n".encode())
            os.close(descriptor)
            yield
        finally:
            lock.unlink(missing_ok=True)


def collect(request: CollectionRequest) -> CollectionResult:
    return CollectionService().collect(request)


def compute_discovery_windows(request: CollectionRequest, state: dict[str, Any], target_date: str) -> list[tuple[str, str]]:
    target = date.fromisoformat(target_date)
    if request.mode == "incremental" or (request.mode == "auto" and state.get("backfill_complete")):
        anchor = state.get("last_checked_date") or state.get("last_collected_date")
        start = date.fromisoformat(anchor) + timedelta(days=1) if anchor else date.fromisoformat(request.history_start)
    else:
        start = date.fromisoformat(request.history_start)
    if start > target:
        return []
    windows = []
    cursor = start
    while cursor <= target:
        end = min(target, cursor + timedelta(days=request.discovery_chunk_days - 1))
        windows.append((cursor.isoformat(), end.isoformat()))
        cursor = end + timedelta(days=1)
    return windows


def build_history_record(*, tile_id: str, observed_date: str, bbox: list[float], metrics: dict[str, Any], stac_item_ids: list[str], previous: dict[str, Any] | None = None, water_scene: dict[str, Any] | None = None) -> dict[str, Any]:
    water_scene = water_scene or {}
    water_pct = safe_float(water_scene.get("water_pct"))
    cloud_pct = safe_float(water_scene.get("cloud_pct"))
    valid_pixels = int(water_scene.get("valid_pixels") or 0)
    water_status = "unknown"
    if water_scene:
        water_status = "water" if water_pct is not None and water_pct > 0 and valid_pixels > 0 else "no_water"
    values = {column: safe_float(metrics.get(column)) for column in TARGET_COLUMNS}
    flags = []
    if cloud_pct is not None and cloud_pct > 30:
        flags.append("cloudy")
    if water_scene and valid_pixels == 0:
        flags.append("no_valid_pixels")
    if any(value is None for value in values.values()):
        flags.append("missing_metrics")
    status = "collected" if all(value is not None for value in values.values()) else "unavailable"
    now = utc_now()
    record = {
        "schema_version": "1.0.0",
        "tile_id": tile_id,
        "observation_date": observed_date,
        "bbox": bbox,
        "collection_status": status,
        "collection_method": CURRENT_COLLECTION_METHOD,
        "water_check_status": "evaluated" if water_scene else "not_performed",
        "water_status": water_status,
        "water_pct": water_pct,
        "cloud_pct": cloud_pct,
        "valid_pixels": valid_pixels,
        "source_scene_count": len(set(stac_item_ids)),
        "stac_item_ids": sorted(set(stac_item_ids)),
        "asset_paths": dict((previous or {}).get("asset_paths") or {}),
        "quality_flags": sorted(set(flags)),
        "attempt_count": int((previous or {}).get("attempt_count") or 0) + 1,
        "created_at": (previous or {}).get("created_at") or now,
        "updated_at": now,
        **values,
    }
    return record


def latest_available_observation(history: list[dict[str, Any]]) -> str | None:
    dates = sorted(str(record["observation_date"]) for record in history if has_complete_metrics(record))
    return dates[-1] if dates else None


def upgrade_history_records(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    upgraded = []
    for original in history:
        record = dict(original)
        record.setdefault("schema_version", "1.0.0")
        record.setdefault("collection_status", "collected" if has_complete_metrics(record) else "unavailable")
        record.setdefault("collection_method", "water_masked_legacy")
        record.setdefault("water_check_status", "evaluated" if record.get("water_status") in {"water", "no_water"} else "not_performed")
        record.setdefault("attempt_count", 1)
        record.setdefault("asset_paths", {})
        record.setdefault("quality_flags", [])
        record.setdefault("stac_item_ids", [])
        for column in TARGET_COLUMNS:
            record[column] = safe_float(record.get(column))
        upgraded.append(record)
    return upgraded


def feature_bbox(feature: dict[str, Any]) -> list[float]:
    ring = feature.get("geometry", {}).get("coordinates", [[]])[0]
    return [min(point[0] for point in ring), min(point[1] for point in ring), max(point[0] for point in ring), max(point[1] for point in ring)]


def _slugify(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in value.strip()).strip("_")


def _find_record(history: list[dict[str, Any]], tile_id: str, observed: str) -> dict[str, Any] | None:
    return next((record for record in history if str(record.get("tile_id")) == tile_id and str(record.get("observation_date")) == observed), None)


def _has_terminal(history: list[dict[str, Any]], tile_id: str, observed: str) -> bool:
    record = _find_record(history, tile_id, observed)
    return bool(record and is_terminal(record))


def _dates_missing_any_unit(dates: list[str], history: list[dict[str, Any]], expected_tiles: list[str]) -> list[str]:
    if not expected_tiles:
        existing_dates = {str(record.get("observation_date")) for record in history if is_terminal(record)}
        return [observed for observed in dates if observed not in existing_dates]
    return [observed for observed in dates if any(not _has_terminal(history, tile, observed) for tile in expected_tiles)]


def _complete_dates(dates: list[str], history: list[dict[str, Any]], expected_tiles: list[str]) -> list[str]:
    if not expected_tiles:
        return []
    return [observed for observed in dates if all(_has_terminal(history, tile, observed) for tile in expected_tiles)]


def _incomplete_known_dates(dates: list[str], history: list[dict[str, Any]], state: dict[str, Any]) -> list[str]:
    return _dates_missing_any_unit(dates, history, list(state.get("expected_tile_ids", [])))


def _merge_item_ids(existing: dict[str, list[str]], new: dict[str, list[str]]) -> dict[str, list[str]]:
    keys = set(existing) | set(new)
    return {key: sorted(set(existing.get(key, [])) | set(new.get(key, []))) for key in sorted(keys)}
