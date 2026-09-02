from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from forecaster.from_storage import StoredForecastConfig, StoredForecastPipeline


class FakeStorage:
    def __init__(self, records):
        self.settings = SimpleNamespace(aoi_id="partner-aoi")
        self.records = records
        self.uploads = {}
        self.forecasts = []
        self.features = []
        self.runs = []
        self.closed = False

    def initialize(self):
        return None

    def aoi_key(self, *, relative_path):
        return f"terra-uc1/partner-aoi/aoi/{relative_path}"

    def data_key(self, *, relative_path):
        return f"terra-uc1/partner-aoi/{relative_path}"

    def download_json(self, *, key):
        if key.endswith("aoi/definition.json"):
            return {"aoi_id": "partner-aoi", "bbox": [22.0, 38.0, 22.1, 38.1], "aoi_definition_hash": "abc"}
        return None

    def load_tiles(self):
        return [{"tile_id": "tile_0", "bbox": [22.0, 38.0, 22.01, 38.01], "size": 400}]

    def load_observations(self, *, start_date=None, end_date=None):
        return [
            record for record in self.records
            if (not start_date or record["observation_date"] >= start_date)
            and (not end_date or record["observation_date"] <= end_date)
        ]

    def upload_json_if_changed(self, value, *, key):
        self.uploads[key] = value
        return {"key": key}

    def upsert_features(self, rows, *, run_id):
        self.features.extend(rows)

    def upsert_forecasts(self, rows, *, run_id):
        self.forecasts.extend(rows)

    def record_run(self, payload, *, run_id):
        self.runs.append({**payload, "run_id": run_id})

    def close(self):
        self.closed = True


def observations():
    metrics = {"CDOM": 1.0, "Chl_a": 2.0, "Color": 3.0, "Cya": 4.0, "DOC": 5.0, "Turb": 6.0, "WQI": 7.0}
    return [
        {"tile_id": "tile_0", "observation_date": f"2026-01-{day:02d}", "collection_status": "collected", "water_status": "water", **metrics}
        for day in range(1, 25)
    ]


def test_forecast_from_storage_uses_only_published_aoi_data(tmp_path, monkeypatch):
    storage = FakeStorage(observations())

    def fake_inference(self, *, selected_records, feature_csvs, water_manifest):
        assert [record["name"] for record in selected_records] == ["tile_0"]
        assert set(feature_csvs) == {"tile_0"}
        return {"tiles": {"tile_0": {"forecast": {"model": [{"date": "2026-01-29", "step": 1, "CDOM": 1.1, "Chl_a": 2.1, "Color": 3.1, "Cya": 4.1, "DOC": 5.1, "Turb": 6.1, "WQI": 7.1}]}}}}

    monkeypatch.setattr("forecaster.from_storage.AOIInferencePipeline.run_model_inference", fake_inference)
    result = StoredForecastPipeline(
        StoredForecastConfig(
            aoi_id="partner-aoi", run_name="external-ingestion", output_root=tmp_path,
            api_job_id="api-job-1", api_run_job_id="caller-run-1",
        ),
        storage=storage,
    ).execute()

    assert result["source"] == "shared_storage"
    assert result["forecast_anchor"] == "2026-01-24"
    assert result["forecast_row_count"] == 1
    assert storage.forecasts[0]["tile_id"] == "tile_0"
    assert storage.features[0]["tile_id"] == "tile_0"
    assert [run["status"] for run in storage.runs] == ["running", "success"]
    assert storage.closed is True
    assert "terra-uc1/partner-aoi/preprocessed/features/tile_0.json" in storage.uploads
    assert (Path(tmp_path) / "external_ingestion" / "forecast_run_result.json").exists()
    assert all(run["api_job_id"] == "api-job-1" for run in storage.runs)
    assert result["api_run_job_id"] == "caller-run-1"


def test_forecast_from_storage_respects_as_of_date(tmp_path, monkeypatch):
    storage = FakeStorage(observations())
    monkeypatch.setattr(
        "forecaster.from_storage.AOIInferencePipeline.run_model_inference",
        lambda *args, **kwargs: {"tiles": {"tile_0": {"forecast": {"model": [{"date": "2026-01-25", "step": 1, "CDOM": 1, "Chl_a": 1, "Color": 1, "Cya": 1, "DOC": 1, "Turb": 1, "WQI": 1}]}}}},
    )
    result = StoredForecastPipeline(
        StoredForecastConfig(aoi_id="partner-aoi", run_name="as-of", output_root=tmp_path, as_of_date="2026-01-24"),
        storage=storage,
    ).execute()
    assert result["forecast_anchor"] == "2026-01-24"


def test_forecast_from_completed_collector_run_can_stay_local(tmp_path, monkeypatch):
    collector_run = tmp_path / "collector-run"
    (collector_run / "collection").mkdir(parents=True)
    (collector_run / "history").mkdir()
    (collector_run / "tiles").mkdir()
    (collector_run / "collection" / "collection_input_manifest.json").write_text(
        '{"schema_version":"1.0.0","aoi_id":"partner-aoi","aoi_bbox":[22.0,38.0,22.1,38.1]}', encoding="utf-8"
    )
    import json
    (collector_run / "history" / "global_history.json").write_text(json.dumps(observations()), encoding="utf-8")
    (collector_run / "tiles" / "tile_records.json").write_text(
        '[{"name":"tile_0","bbox":[22.0,38.0,22.01,38.01],"size":400}]', encoding="utf-8"
    )
    monkeypatch.setattr(
        "forecaster.from_storage.AOIInferencePipeline.run_model_inference",
        lambda *args, **kwargs: {"tiles": {"tile_0": {"forecast": {"model": [{"date": "2026-01-25", "step": 1, "CDOM": 1, "Chl_a": 1, "Color": 1, "Cya": 1, "DOC": 1, "Turb": 1, "WQI": 1}]}}}},
    )
    result = StoredForecastPipeline(
        StoredForecastConfig(
            aoi_id="partner-aoi", run_name="local-handoff", output_root=tmp_path,
            collection_run_dir=collector_run, publish=False,
        )
    ).execute()
    assert result["forecast_anchor"] == "2026-01-24"
    assert (tmp_path / "local_handoff" / "forecasts" / "global_forecasts.json").exists()


def test_forecast_records_failed_run(tmp_path):
    storage = FakeStorage([])

    import pytest
    with pytest.raises(ValueError, match="No usable collected observations"):
        StoredForecastPipeline(
            StoredForecastConfig(aoi_id="partner-aoi", run_name="failure", output_root=tmp_path),
            storage=storage,
        ).execute()

    assert [run["status"] for run in storage.runs] == ["running", "failed"]
    assert storage.closed is True
