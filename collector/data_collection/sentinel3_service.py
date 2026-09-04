"""Calendar-driven S3 orchestration, called by CollectionService."""
from datetime import date, datetime, timedelta, timezone
import hashlib

from .collectors.sentinel3 import METHOD, METRIC
from .storage import HistoryStore, read_json, utc_now, write_json

COLUMNS = (
    "schema_version", "sensor", "tile_id", "observation_date", "bbox",
    "collection_status", "collection_method", METRIC, "min_c", "max_c",
    "stdev_c", "sample_count", "valid_sample_count", "stac_item_ids",
    "quality_flags", "asset_paths", "created_at", "updated_at",
    "source", "native_resolution_m", "sampling_grid_degrees", "raw_artifact",
)


def terminal(row):
    return row.get("collection_method") == METHOD and row.get("collection_status") in {
        "collected", "unavailable",
    }


def execute(service):
    request = service.request
    start = date.fromisoformat(request.history_start)
    end = date.fromisoformat(request.target_date or datetime.now(timezone.utc).date().isoformat())
    if start > end or end > datetime.now(timezone.utc).date():
        raise ValueError("Sentinel-3 range must be ordered and cannot include future dates")
    days = [(start + timedelta(days=n)).isoformat() for n in range((end - start).days + 1)]
    global_store = HistoryStore(service.run_dir / "history/global_history.json",
                                service.run_dir / "history/global_history.csv", COLUMNS)
    records = {(row["tile_id"], str(row["observation_date"])[:10]): row
               for row in global_store.load()}
    # Recover per-tile checkpoints left by an interrupted invocation.
    for path in sorted((service.run_dir / "history/tiles").glob("*/history.json")):
        for row in read_json(path, []):
            records[(row["tile_id"], row["observation_date"])] = row
    if request.dry_run:
        return service._result(
            status="dry_run", summary=f"Sentinel-3 calendar range contains {len(days)} days; no network or writes.",
            state={}, windows=[(str(start), str(end))], warnings=[], missing_dates=days,
        )
    tile_state = read_json(service.run_dir / "tiles/tile_state.json", {})
    if tile_state and tile_state.get("tile_config_hash") != service._tile_config_hash():
        raise ValueError("Sentinel-3 AOI/tiling changed; use a new run directory")
    tiles, geojson_path, tile_path = service._extract_tiles()
    if not tiles:
        raise ValueError("Sentinel-3 collection requires at least one river tile")
    tile_ids = [str(tile["name"]) for tile in tiles]
    missing = [day for day in days if any(
        not terminal(records.get((tile, day), {})) for tile in tile_ids)]
    selected_days = missing[:request.max_days_per_run] if request.max_days_per_run else missing
    selected_tiles = [tile for tile in tiles if any(
        not terminal(records.get((str(tile["name"]), day), {})) for day in selected_days)]
    if request.max_tiles_per_run:
        selected_tiles = selected_tiles[:request.max_tiles_per_run]
    failures, changed, collected = [], 0, set()
    for tile in selected_tiles:
        tile_id = str(tile["name"])
        tile_rows = {day: row for (name, day), row in records.items() if name == tile_id}
        pending = [day for day in selected_days if not terminal(records.get((tile_id, day), {}))]
        tile_store = HistoryStore(
            service.run_dir / "history/tiles" / tile_id / "history.json",
            service.run_dir / "history/tiles" / tile_id / "history.csv", COLUMNS)
        for offset in range(0, len(pending), request.discovery_chunk_days):
            # Bound the request's calendar span even when pending days are sparse.
            chunk = pending[offset:offset + request.discovery_chunk_days]
            # Issue contiguous subwindows, never an unbounded sparse-date span.
            groups = []
            for day in chunk:
                if not groups or (date.fromisoformat(day) - date.fromisoformat(groups[-1][-1])).days != 1:
                    groups.append([])
                groups[-1].append(day)
            for group in groups:
                work = service.run_dir / "raw" / tile_id
                service._log(f"Sentinel-3 {tile_id}: {group[0]} to {group[-1]}")
                try:
                    client = service.sentinel3_factory(bbox=list(tile["bbox"]), dir=work)
                    values = client.collect_daily(group[0], group[-1])
                except Exception as exc:
                    failures.extend({"tile_id": tile_id, "observation_date": day,
                                     "retryable": True, "message": str(exc)} for day in group)
                    continue
                raw_path = work / f"{group[0]}_{group[-1]}.json"
                raw_artifact = {}
                if service.storage is not None and raw_path.exists():
                    checksum = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                    raw_artifact = service.storage.upload_file_if_changed(
                        raw_path, key=service.storage.data_key(
                            relative_path=f"raw/{tile_id}/{checksum}.json"),
                        content_type="application/json")
                updates = []
                for day in group:
                    if day not in values:
                        failures.append({"tile_id": tile_id, "observation_date": day,
                                         "retryable": True, "message": "Missing statistics for catalogue scene"})
                        continue
                    now = utc_now()
                    old = records.get((tile_id, day), {})
                    row = {
                        **values[day], "schema_version": "1.0.0", "sensor": "sentinel3",
                        "tile_id": tile_id, "observation_date": day, "bbox": list(tile["bbox"]),
                        "collection_method": METHOD, "source": "sentinel-3-slstr",
                        "native_resolution_m": 1000,
                        "sampling_grid_degrees": [
                            min(0.01, (tile["bbox"][2] - tile["bbox"][0]) / 2),
                            min(0.01, (tile["bbox"][3] - tile["bbox"][1]) / 2),
                        ],
                        "asset_paths": {"raw": str(raw_path.relative_to(service.run_dir))},
                        "created_at": old.get("created_at", now), "updated_at": now,
                    }
                    if raw_artifact:
                        row["raw_artifact"] = raw_artifact
                    updates.append(row)
                # Publish a bounded chunk before checkpointing it locally.
                if service.storage is not None:
                    service.storage.upsert_observations(updates, run_id=service.execution_id)
                for row in updates:
                    records[(tile_id, row["observation_date"])] = row
                    tile_rows[row["observation_date"]] = row
                    collected.add(row["observation_date"])
                changed += len(updates)
                tile_store.write(sorted(
                    tile_rows.values(),
                    key=lambda row: row["observation_date"]))
    history = sorted(records.values(), key=lambda row: (row["tile_id"], row["observation_date"]))
    global_store.write(history)
    remaining = sum(not terminal(records.get((tile, day), {})) for tile in tile_ids for day in days)
    observed = sorted({row["observation_date"] for row in history if row["collection_status"] == "collected"})
    state = {
        "schema_version": "1.0.0", "sensor": "sentinel3",
        "history_start": str(start), "target_date": str(end),
        "expected_tile_ids": tile_ids, "backfill_complete": remaining == 0,
        "pending_unit_count": remaining, "failed_units": failures,
        "known_stac_dates": [], "completed_dates": [],
        "last_checked_date": str(end), "updated_at": utc_now(),
    }
    service._save_state(state)
    result = service._result(
        status="partial" if remaining else "success",
        summary=f"Sentinel-3: wrote {changed} records; {remaining} tile-days remain.",
        state=state, windows=[(str(start), str(end))], warnings=[],
        available_dates=observed, missing_dates=missing, collected_dates=sorted(collected),
        new_record_count=changed, failed_units=failures,
        latest=observed[-1] if observed else None,
        tile_records_path=tile_path, tiles_geojson_path=geojson_path,
    )
    write_json(service.run_dir / "collection/collection_run_result.json", result.to_dict())
    write_json(service.run_dir / "collection/collection_input_manifest.json", {
        "schema_version": "1.0.0", "sensor": "sentinel3",
        "aoi_id": request.aoi_id, "request": request.to_dict(),
        "history_json_path": result.history_json_path,
    })
    return result
