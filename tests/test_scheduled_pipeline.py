from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from config import CDSE_Credentials
from forecaster.collection_provider import CollectionResult
from forecaster.scheduled_pipeline import (
    ScheduledIncrementalPipeline,
    ScheduledPipelineConfig,
    apply_water_manifest,
    build_history_record,
    build_run_summary,
    compute_discovery_windows,
    compute_missing_dates,
)
from forecaster.stac_exporter import StacCatalogExporter, TARGET_VARIABLES


class FakeDiscovery:
    def __init__(self, dates: list[str]):
        self.dates = dates
        self.calls = []

    def discover_dates(self, bbox, start_date, end_date):
        self.calls.append((start_date, end_date))
        dates = [date for date in self.dates if start_date <= date <= end_date]
        return dates, {date: [f"S2_{date}"] for date in dates}, []


class FakeCollectionProvider:
    def __init__(self, dates: list[str]):
        self.dates = dates
        self.requests = []

    def collect(self, request):
        self.requests.append(request)
        run_dir = Path(request.output_root) / request.run_name
        if request.dry_run:
            return CollectionResult(
                status="dry_run",
                run_dir=str(run_dir),
                run_summary=f"Dry run found {len(self.dates)} date(s) that require collection; no files were written.",
                mode=request.mode,
                available_dates=self.dates,
                missing_dates=self.dates,
                discovery_windows=[{"start_date": request.history_start, "end_date": request.target_date}],
            )

        history = [
            build_history_record(
                tile_id="tile_0",
                observed_date=observed,
                bbox=[22.0, 38.0, 22.01, 38.01],
                metrics=metric_values(),
                stac_item_ids=[f"S2_{observed}"],
            )
            for index, observed in enumerate(self.dates)
        ]
        history_path = run_dir / "history" / "global_history.json"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history), encoding="utf-8")
        pd.DataFrame(history).to_csv(run_dir / "history" / "global_history.csv", index=False)
        records_path = run_dir / "tiles" / "tile_records.json"
        records_path.parent.mkdir(parents=True, exist_ok=True)
        records_path.write_text(json.dumps(tile_records()), encoding="utf-8")
        geojson_path = run_dir / "tiles" / "river_tiles.geojson"
        geojson_path.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [{
                "type": "Feature",
                "id": "tile_0",
                "geometry": {"type": "Polygon", "coordinates": [[[22.0, 38.0], [22.01, 38.0], [22.01, 38.01], [22.0, 38.01], [22.0, 38.0]]]},
                "properties": {"name": "tile_0"},
            }],
        }), encoding="utf-8")
        water_path = run_dir / "water" / f"water_selection_{self.dates[0]}_{self.dates[-1]}.json"
        water_path.parent.mkdir(parents=True, exist_ok=True)
        water_path.write_text(json.dumps(water_manifest(self.dates)), encoding="utf-8")
        collection_state = run_dir / "collection" / "state.json"
        collection_state.parent.mkdir(parents=True, exist_ok=True)
        collection_state.write_text(json.dumps({
            "known_stac_dates": self.dates,
            "completed_dates": self.dates,
            "last_collected_date": self.dates[-1],
        }), encoding="utf-8")
        return CollectionResult(
            status="success",
            run_dir=str(run_dir),
            run_summary=f"Collected {len(history)} tile-date record(s).",
            mode=request.mode,
            available_dates=self.dates,
            missing_dates=self.dates,
            collected_dates=self.dates,
            new_record_count=len(history),
            latest_available_observation=self.dates[-1],
            history_json_path=str(history_path),
            history_csv_path=str(run_dir / "history" / "global_history.csv"),
            tile_records_path=str(records_path),
            tiles_geojson_path=str(geojson_path),
            state_path=str(collection_state),
        )


class FakeRemoteStorage:
    """In-memory boundary double for scheduled MongoDB/MinIO persistence."""

    def __init__(self, *, aoi_id: str = "test-aoi"):
        self.settings = SimpleNamespace(aoi_id=aoi_id)
        self.initialized = False
        self.aoi_definitions: list[dict] = []
        self.uploaded: list[str] = []
        self.observations: list[dict] = []
        self.tiles: list[dict] = []
        self.features: list[dict] = []
        self.forecasts: list[dict] = []
        self.runs: list[dict] = []

    def initialize(self):
        self.initialized = True

    def ensure_aoi_definition(self, definition):
        self.aoi_definitions.append(definition)
        return self._artifact(self.aoi_key(relative_path="definition.json"))

    def load_observations(self):
        return []

    def download_json(self, *, key):
        return None

    def upload_json_if_changed(self, value, *, key):
        self.uploaded.append(key)
        return self._artifact(key)

    def upload_json_file(self, path, *, key):
        self.uploaded.append(key)
        return self._artifact(key)

    def upload_file_if_changed(self, path, *, key, content_type):
        self.uploaded.append(key)
        return self._artifact(key)

    def upload_file(self, path, *, key, content_type):
        self.uploaded.append(key)
        return self._artifact(key)

    def upsert_observations(self, rows, *, run_id):
        self.observations.extend(rows)

    def upsert_tiles(self, rows, *, run_id):
        self.tiles.extend(rows)

    def upsert_features(self, rows, *, run_id):
        self.features.extend(rows)

    def upsert_forecasts(self, rows, *, run_id):
        self.forecasts.extend(rows)

    def record_run(self, payload, *, run_id):
        self.runs.append({**payload, "run_id": run_id})

    def aoi_key(self, *, relative_path):
        return f"terra-uc1/{self.settings.aoi_id}/aoi/{relative_path}"

    def data_key(self, *, relative_path):
        return f"terra-uc1/{self.settings.aoi_id}/{relative_path}"

    def run_key(self, *, run_id, relative_path):
        return f"terra-uc1/{self.settings.aoi_id}/runs/{run_id}/{relative_path}"

    @staticmethod
    def _artifact(key):
        return {"bucket": "test-bucket", "key": key, "sha256": "test"}


def tile_records():
    return [
        {
            "name": "tile_0",
            "bbox": [22.0, 38.0, 22.01, 38.01],
            "size": 400,
        }
    ]


def water_manifest(dates: list[str]):
    return {
        "selected_tiles": ["tile_0"],
        "rejected_tiles": [],
        "tiles": [
            {
                "name": "tile_0",
                "bbox": [22.0, 38.0, 22.01, 38.01],
                "size": 400,
                "water_score_pct": 12.0,
                "selected": True,
                "scenes": [
                    {
                        "date": observed,
                        "valid_pixels": 100,
                        "sample_count": 100,
                        "no_data_count": 0,
                        "water_pct": 25.0,
                        "cloud_pct": 1.0,
                    }
                    for observed in dates
                ],
            }
        ],
    }


def metric_values(seed: float = 1.0):
    return {column: seed + index for index, column in enumerate(TARGET_VARIABLES)}


def test_compute_missing_dates_limits_oldest_first():
    assert compute_missing_dates(
        available_dates=["2026-01-01", "2026-01-06", "2026-01-11"],
        collected_dates=["2026-01-01"],
        max_days=1,
    ) == ["2026-01-06"]


def test_compute_discovery_windows_uses_one_chunk_for_initial_backfill_by_default():
    assert compute_discovery_windows(
        history_start="2016-01-01",
        target_date="2016-12-31",
        collected_dates=[],
        state={},
        chunk_days=31,
    ) == [("2016-01-01", "2016-01-31")]


def test_compute_discovery_windows_can_scan_full_backfill():
    assert compute_discovery_windows(
        history_start="2016-01-01",
        target_date="2016-02-05",
        collected_dates=[],
        state={},
        chunk_days=31,
        backfill_all=True,
    ) == [("2016-01-01", "2016-01-31"), ("2016-02-01", "2016-02-05")]


def test_compute_discovery_windows_uses_last_collected_date_for_daily_runs():
    assert compute_discovery_windows(
        history_start="2016-01-01",
        target_date="2026-07-07",
        collected_dates=["2026-07-05"],
        state={"backfill_cursor": "2017-01-01"},
        chunk_days=31,
    ) == [("2026-07-06", "2026-07-07")]


def test_build_run_summary_explains_noop_forecast():
    summary = build_run_summary(
        new_data_available=False,
        forecast_status="already_forecasted",
        forecast_anchor="2026-07-03",
        last_forecast_anchor="2026-07-03",
    )

    assert "No new satellite dates" in summary
    assert "already forecasted" in summary


def test_scheduled_first_run_uses_collection_provider_and_exports_stac(tmp_path):
    dates = ["2026-01-01", "2026-01-06"]
    provider = FakeCollectionProvider(dates)
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="test_run",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-06",
            run_inference=False,
        ),
        collection_provider=provider,
        storage=FakeRemoteStorage(),
    )

    result = pipeline.execute()

    assert result.status == "success"
    assert result.new_data_available is True
    assert result.forecast_status == "disabled"
    assert result.collected_dates == dates
    history = json.loads((tmp_path / "test_run" / "history" / "global_history.json").read_text())
    assert len(history) == 2
    assert pd.read_csv(tmp_path / "test_run" / "history" / "global_history.csv").shape[0] == 2
    state = json.loads((tmp_path / "test_run" / "collection" / "state.json").read_text())
    assert state["known_stac_dates"] == dates
    assert state["last_collected_date"] == "2026-01-06"
    assert provider.requests[0].mode == "auto"
    collection = json.loads((tmp_path / "test_run" / "stac_catalog" / "collection.json").read_text())
    assert set(collection["item_assets"]) == {"overview", "tile_0"}


def test_scheduled_maps_backfill_flag_to_collection_provider(tmp_path):
    provider = FakeCollectionProvider(["2026-01-01"])
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="cached",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-01",
            run_inference=False,
            backfill_all=True,
        ),
        collection_provider=provider,
        storage=FakeRemoteStorage(),
    )
    pipeline.execute()
    assert provider.requests[0].mode == "backfill"


def test_scheduled_pipeline_persists_aoi_observations_tiles_and_run_snapshot(tmp_path):
    storage = FakeRemoteStorage()
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="persistent",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-06",
            run_inference=False,
        ),
        collection_provider=FakeCollectionProvider(["2026-01-01", "2026-01-06"]),
        storage=storage,
    )

    result = pipeline.execute()

    assert result.status == "success"
    assert storage.initialized is True
    assert storage.aoi_definitions[0]["aoi_id"] == "test-aoi"
    assert [(row["tile_id"], row["observation_date"]) for row in storage.observations] == [
        ("tile_0", "2026-01-01"),
        ("tile_0", "2026-01-06"),
    ]
    assert [row["name"] for row in storage.tiles] == ["tile_0"]
    assert storage.runs[0]["run_name"] == "persistent"
    assert storage.runs[0]["status"] == "success"
    assert "terra-uc1/test-aoi/aoi/tiles/river_tiles.geojson" in storage.uploaded
    assert "terra-uc1/test-aoi/observations/2026-01-01.json" in storage.uploaded
    assert "terra-uc1/test-aoi/observations/2026-01-06.json" in storage.uploaded
    assert any(key.endswith("/scheduled_run_result.json") for key in storage.uploaded)


def test_scheduled_dry_run_does_not_write_history_or_state(tmp_path):
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="dry",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-01",
            dry_run=True,
            run_inference=False,
        ),
        collection_provider=FakeCollectionProvider(["2026-01-01"]),
        storage=FakeRemoteStorage(),
    )

    result = pipeline.execute()

    assert result.status == "dry_run"
    assert "Dry run found 1 date" in result.run_summary
    assert result.new_data_available is True
    assert result.forecast_status == "dry_run"
    assert result.missing_dates == ["2026-01-01"]
    assert not (tmp_path / "dry" / "collection" / "state.json").exists()
    assert not (tmp_path / "dry" / "history" / "global_history.json").exists()


def test_stac_exporter_writes_valid_item_assets(tmp_path):
    history = [
        build_history_record(
            tile_id="tile_0",
            observed_date="2026-01-01",
            bbox=[22.0, 38.0, 22.01, 38.01],
            metrics=metric_values(),
            water_scene={"date": "2026-01-01", "water_pct": 30.0, "cloud_pct": 0.0, "valid_pixels": 100},
            stac_item_ids=["S2_A"],
        )
    ]
    forecast = [
        {
            "forecast_run_id": "forecast_2026-01-01",
            "anchor_date": "2026-01-01",
            "tile_id": "tile_0",
            "forecast_date": "2026-01-06",
            "step": 1,
            **metric_values(2.0),
        }
    ]

    result = StacCatalogExporter(tmp_path).export(
        aoi_bbox=[22.0, 38.0, 22.01, 38.01],
        tile_records=tile_records(),
        history_records=history,
        forecast_rows=forecast,
        anchor_date="2026-01-01",
    )

    assert result.item_id == "water-pollution-processing-20260101-20260106"
    item = json.loads(result.item_path.read_text())
    assert "forecast:reference_datetime" in item["properties"]
    assert "processing:software" in item["properties"]
    tile_payload = json.loads((result.item_path.parent / "tiles" / "tile_0.json").read_text())
    assert tile_payload["geometry_id"] == "tile_0"
    assert [row["date"] for row in tile_payload["data"]] == ["2026-01-01T00:00:00Z", "2026-01-06T00:00:00Z"]
    assert set(tile_payload["data"][0]["values"]) == set(TARGET_VARIABLES)


def test_cdse_credentials_discovers_backup_sets(monkeypatch):
    monkeypatch.setattr(CDSE_Credentials, "_load_local_env_if_present", lambda: None)
    monkeypatch.setenv("CDSE_CLIENT_ID", "primary_id")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "primary_secret")
    monkeypatch.setenv("CDSE_BACKUP_CLIENT_ID", "backup_id")
    monkeypatch.setenv("CDSE_BACKUP_CLIENT_SECRET", "backup_secret")
    monkeypatch.setenv("CDSE_BACKUP_2_CLIENT_ID", "backup_2_id")
    monkeypatch.setenv("CDSE_BACKUP_2_CLIENT_SECRET", "backup_2_secret")

    credentials = CDSE_Credentials.get_credential_sets()

    assert [credential["label"] for credential in credentials] == ["primary", "backup", "backup_2"]


def test_forecaster_enriches_history_with_water_without_mutating_collector_record():
    source = build_history_record(
        tile_id="tile_0",
        observed_date="2026-01-01",
        bbox=[22.0, 38.0, 22.01, 38.01],
        metrics=metric_values(),
        stac_item_ids=["S2_A"],
    )
    manifest = water_manifest(["2026-01-01"])
    manifest["evaluated_dates"] = ["2026-01-01"]

    evaluated = apply_water_manifest([source], manifest)

    assert source["water_check_status"] == "not_performed"
    assert source["water_status"] == "unknown"
    assert evaluated[0]["water_check_status"] == "evaluated"
    assert evaluated[0]["water_status"] == "water"


def test_forecaster_water_history_queries_only_unchecked_dates(tmp_path, monkeypatch):
    calls = []

    class FakeSelector:
        def __init__(self, **kwargs):
            self.interval = kwargs["water_check_interval"]

        def select_tiles(self):
            calls.append(self.interval)
            return water_manifest(list(self.interval))

        def build_manifest_from_tiles(self, records):
            payload = water_manifest([])
            payload["tiles"] = records
            payload["selected_tiles"] = ["tile_0"]
            return payload

    monkeypatch.setattr("forecaster.scheduled_pipeline.WaterTileSelector", FakeSelector)
    pipeline = ScheduledIncrementalPipeline(ScheduledPipelineConfig(
        aoi_bbox=[22.0, 38.0, 22.01, 38.01],
        run_name="water_cache",
        output_root=tmp_path,
        run_inference=False,
    ), storage=FakeRemoteStorage())
    geojson = tmp_path / "tiles.geojson"
    geojson.write_text(json.dumps({"type": "FeatureCollection", "features": []}), encoding="utf-8")
    history = [
        build_history_record(
            tile_id="tile_0",
            observed_date=observed,
            bbox=[22.0, 38.0, 22.01, 38.01],
            metrics=metric_values(),
            stac_item_ids=[f"S2_{observed}"],
        )
        for observed in ["2026-01-01", "2026-01-06"]
    ]

    first = pipeline._build_inference_water_manifest(str(geojson), history, "2026-01-06")
    second = pipeline._build_inference_water_manifest(str(geojson), history, "2026-01-06")

    assert calls == [("2026-01-01", "2026-01-06")]
    assert first["evaluated_dates"] == ["2026-01-01", "2026-01-06"]
    assert second["evaluated_dates"] == first["evaluated_dates"]
