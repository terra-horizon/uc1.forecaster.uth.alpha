from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config import CDSE_Credentials
from forecaster.scheduled_pipeline import (
    ScheduledIncrementalPipeline,
    ScheduledPipelineConfig,
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


def test_scheduled_first_run_updates_history_state_and_stac_collection(tmp_path, monkeypatch):
    dates = ["2026-01-01", "2026-01-06"]
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="test_run",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-06",
            run_inference=False,
        ),
        discovery=FakeDiscovery(dates),
    )

    monkeypatch.setattr(pipeline, "_extract_tiles", lambda: (tile_records(), tmp_path / "river_tiles.geojson"))
    monkeypatch.setattr(pipeline, "_build_water_manifest", lambda _path, requested_dates: water_manifest(requested_dates))
    monkeypatch.setattr(
        pipeline,
        "_collect_history_records",
        lambda tile_records, dates, water_manifest, stac_item_ids: [
            build_history_record(
                tile_id="tile_0",
                observed_date=observed,
                bbox=[22.0, 38.0, 22.01, 38.01],
                metrics=metric_values(),
                water_scene=water_manifest["tiles"][0]["scenes"][index],
                stac_item_ids=stac_item_ids[observed],
            )
            for index, observed in enumerate(dates)
        ],
    )

    result = pipeline.execute()

    assert result.status == "success"
    assert result.new_data_available is True
    assert result.forecast_status == "disabled"
    assert result.collected_dates == dates
    history = json.loads((tmp_path / "test_run" / "history" / "global_history.json").read_text())
    assert len(history) == 2
    assert pd.read_csv(tmp_path / "test_run" / "history" / "global_history.csv").shape[0] == 2
    state = json.loads((tmp_path / "test_run" / "state.json").read_text())
    assert state["known_stac_dates"] == dates
    assert state["last_collected_date"] == "2026-01-06"
    assert state["backfill_cursor"] == "2026-01-07"
    collection = json.loads((tmp_path / "test_run" / "stac_catalog" / "collection.json").read_text())
    assert set(collection["item_assets"]) == {"overview", "tile_0"}


def test_scheduled_run_reuses_cached_stac_discovery(tmp_path, monkeypatch):
    dates = ["2026-01-01"]
    discovery = FakeDiscovery(dates)
    pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="cached",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-01",
            run_inference=False,
        ),
        discovery=discovery,
    )
    monkeypatch.setattr(pipeline, "_extract_tiles", lambda: (tile_records(), tmp_path / "river_tiles.geojson"))
    monkeypatch.setattr(pipeline, "_build_water_manifest", lambda _path, requested_dates: water_manifest(requested_dates))
    monkeypatch.setattr(
        pipeline,
        "_collect_history_records",
        lambda tile_records, dates, water_manifest, stac_item_ids: [
            build_history_record(
                tile_id="tile_0",
                observed_date="2026-01-01",
                bbox=[22.0, 38.0, 22.01, 38.01],
                metrics=metric_values(),
                water_scene=water_manifest["tiles"][0]["scenes"][0],
                stac_item_ids=stac_item_ids["2026-01-01"],
            )
        ],
    )

    pipeline.execute()
    cache_path = tmp_path / "cached" / "stac_cache" / "2026-01-01_2026-01-01.json"
    assert cache_path.exists()

    second_discovery = FakeDiscovery(["2026-01-02"])
    second_pipeline = ScheduledIncrementalPipeline(
        ScheduledPipelineConfig(
            aoi_bbox=[22.0, 38.0, 22.01, 38.01],
            run_name="cached",
            output_root=tmp_path,
            history_start="2026-01-01",
            target_date="2026-01-01",
            dry_run=True,
            run_inference=False,
        ),
        discovery=second_discovery,
    )
    second_result = second_pipeline.execute()

    assert second_result.available_dates == []
    assert second_discovery.calls == []


def test_scheduled_dry_run_does_not_write_history_or_state(tmp_path, monkeypatch):
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
        discovery=FakeDiscovery(["2026-01-01"]),
    )
    monkeypatch.setattr(
        pipeline,
        "_extract_tiles",
        lambda: (_ for _ in ()).throw(AssertionError("dry run should not extract tiles")),
    )

    result = pipeline.execute()

    assert result.status == "dry_run"
    assert result.run_summary == "Dry run found new satellite dates that would be collected."
    assert result.new_data_available is True
    assert result.forecast_status == "dry_run"
    assert result.missing_dates == ["2026-01-01"]
    assert not (tmp_path / "dry" / "state.json").exists()
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
