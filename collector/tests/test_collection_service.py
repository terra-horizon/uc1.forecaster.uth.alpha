from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from data_collection.collectors.sentinel2 import StatisticalCollection
from data_collection.collectors.sentinel3 import Sentinel3Collection
from data_collection.evalscripts import sentinel2_statistics_all_pixels
from data_collection.models import CollectionRequest
from data_collection.service import CollectionService
from data_collection.storage import HistoryStore
from data_collection.validation import validate_run


DATES = ["2026-01-01", "2026-01-06"]
METRICS = {"CDOM": 1.0, "Chl_a": 2.0, "Color": 3.0, "Cya": 4.0, "DOC": 5.0, "Turb": 6.0, "WQI": 7.0}


def test_history_store_serializes_mongodb_datetimes(tmp_path):
    history_path = tmp_path / "history" / "global_history.json"
    HistoryStore(history_path, tmp_path / "history" / "global_history.csv").write([{
        "tile_id": "tile_0",
        "observation_date": "2026-01-01",
        "created_at": datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, 12, 31),
    }])

    records = json.loads(history_path.read_text(encoding="utf-8"))
    assert records[0]["created_at"] == "2026-01-01T12:30:00Z"
    assert records[0]["updated_at"] == "2026-01-01T12:31:00Z"


class FakeDiscovery:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []

    def discover_dates(self, bbox, start_date, end_date):
        self.calls.append((start_date, end_date))
        dates = [value for value in self.dates if start_date <= value <= end_date]
        return dates, {value: [f"S2_{value}"] for value in dates}, []


class FakeTileExtractor:
    def __init__(self, config):
        self.config = config

    def extract_to_geojson(self, output_path):
        feature = {
            "type": "Feature",
            "id": "tile_0",
            "geometry": {"type": "Polygon", "coordinates": [[[22.0, 38.0], [22.01, 38.0], [22.01, 38.01], [22.0, 38.01], [22.0, 38.0]]]},
            "properties": {"name": "tile_0"},
        }
        payload = {
            "type": "FeatureCollection",
            "aoi_bbox": self.config.aoi_bbox,
            "tile_count": 1,
            "features": [feature],
        }
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return [], path


class MultiTileExtractor(FakeTileExtractor):
    def extract_to_geojson(self, output_path):
        features = []
        for index in range(2):
            offset = index * 0.02
            features.append({
                "type": "Feature",
                "id": f"tile_{index}",
                "geometry": {"type": "Polygon", "coordinates": [[[22.0 + offset, 38.0], [22.01 + offset, 38.0], [22.01 + offset, 38.01], [22.0 + offset, 38.01], [22.0 + offset, 38.0]]]},
                "properties": {"name": f"tile_{index}"},
            })
        payload = {"type": "FeatureCollection", "aoi_bbox": self.config.aoi_bbox, "tile_count": 2, "features": features}
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return [], path


class FakeStatistics:
    def __init__(self, *, time_interval, bbox, dir, max_cloud_coverage):
        self.dates = [value for value in DATES if time_interval[0] <= value <= time_interval[1]]

    def run(self, json_output_folder, csv_output_folder):
        output = Path(csv_output_folder)
        output.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([{"date": value, **METRICS} for value in self.dates]).to_csv(output / "mean_metrics.csv", index=False)


class FailingStatistics(FakeStatistics):
    def run(self, json_output_folder, csv_output_folder):
        raise RuntimeError("temporary CDSE failure")


class EmptyStatistics(FakeStatistics):
    def run(self, json_output_folder, csv_output_folder):
        Path(csv_output_folder).mkdir(parents=True, exist_ok=True)


def service(discovery):
    return CollectionService(
        discovery_factory=lambda _cloud: discovery,
        statistics_factory=FakeStatistics,
        tile_extractor_factory=FakeTileExtractor,
    )


def request(tmp_path, **overrides):
    values = {
        "aoi_bbox": [22.0, 38.0, 22.01, 38.01],
        "run_name": "test",
        "output_root": tmp_path,
        "history_start": "2026-01-01",
        "target_date": "2026-01-06",
        "discovery_chunk_days": 31,
        "publish": False,
    }
    values.update(overrides)
    return CollectionRequest(**values)


def test_first_run_writes_incremental_contract_and_validates(tmp_path):
    result = service(FakeDiscovery(DATES)).collect(request(tmp_path))

    assert result.status == "success"
    assert result.new_record_count == 2
    history = json.loads(Path(result.history_json_path).read_text())
    assert [(row["tile_id"], row["observation_date"]) for row in history] == [("tile_0", "2026-01-01"), ("tile_0", "2026-01-06")]
    assert all(row["collection_status"] == "collected" for row in history)
    assert all(row["water_check_status"] == "not_performed" for row in history)
    assert all(row["water_status"] == "unknown" for row in history)
    assert pd.read_csv(result.history_csv_path).shape[0] == len(history)
    assert (Path(result.run_dir) / "cdse_stac_cache" / "2026-01-01_2026-01-06.json").exists()
    assert not (Path(result.run_dir) / "stac_cache").exists()
    assert validate_run(Path(result.run_dir))["valid"] is True


def test_legacy_stac_cache_is_reused_and_promoted(tmp_path):
    run_dir = tmp_path / "test"
    legacy_cache = run_dir / "stac_cache" / "2026-01-01_2026-01-06.json"
    legacy_cache.parent.mkdir(parents=True)
    legacy_cache.write_text(json.dumps({
        "available_dates": DATES,
        "stac_item_ids": {value: [f"S2_{value}"] for value in DATES},
        "warnings": [],
    }), encoding="utf-8")
    discovery = FakeDiscovery([])

    result = service(discovery).collect(request(tmp_path))

    assert result.status == "success"
    assert discovery.calls == []
    assert (run_dir / "cdse_stac_cache" / "2026-01-01_2026-01-06.json").exists()


def test_incremental_rerun_is_noop_and_does_not_duplicate(tmp_path):
    first_discovery = FakeDiscovery(DATES)
    service(first_discovery).collect(request(tmp_path))
    second_discovery = FakeDiscovery(DATES)
    result = service(second_discovery).collect(request(tmp_path, mode="incremental"))

    assert result.new_record_count == 0
    assert result.missing_dates == []
    history = json.loads(Path(result.history_json_path).read_text())
    assert len(history) == 2


def test_dry_run_writes_nothing(tmp_path):
    result = service(FakeDiscovery(DATES)).collect(request(tmp_path, dry_run=True))

    assert result.status == "dry_run"
    assert result.missing_dates == DATES
    assert not (tmp_path / "test").exists()


def test_legacy_state_and_history_are_migrated(tmp_path):
    run_dir = tmp_path / "test"
    run_dir.mkdir(parents=True)
    (run_dir / "state.json").write_text(json.dumps({
        "created_at": "2026-01-01T00:00:00Z",
        "known_stac_dates": ["2026-01-01"],
        "collected_dates": ["2026-01-01"],
        "last_collected_date": "2026-01-01",
    }), encoding="utf-8")
    history_dir = run_dir / "history"
    history_dir.mkdir()
    legacy_record = {
        "tile_id": "tile_0", "observation_date": "2026-01-01", "bbox": [22.0, 38.0, 22.01, 38.01],
        "water_status": "water", "water_pct": 20.0, "cloud_pct": 1.0, "valid_pixels": 100,
        "source_scene_count": 1, "stac_item_ids": ["S2_OLD"], "asset_paths": {}, "quality_flags": [],
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", **METRICS,
    }
    (history_dir / "global_history.json").write_text(json.dumps([legacy_record]), encoding="utf-8")

    result = service(FakeDiscovery(["2026-01-06"])).collect(request(tmp_path))
    state = json.loads(Path(result.state_path).read_text())

    assert state["legacy_state_migrated"] is True
    assert json.loads((run_dir / "state.json").read_text())["last_collected_date"] == "2026-01-01"
    assert len(json.loads(Path(result.history_json_path).read_text())) == 2


def test_initial_token_failure_rotates_to_backup(monkeypatch, capsys):
    class Response:
        def __init__(self, status_code, token=None):
            self.status_code = status_code
            self._token = token

        def json(self):
            return {"access_token": self._token}

    responses = iter([Response(401), Response(200, "backup-token")])
    monkeypatch.setattr("data_collection.collectors.sentinel2.requests.post", lambda *args, **kwargs: next(responses))
    collector = StatisticalCollection.__new__(StatisticalCollection)
    collector.token_url = "https://example.test/token"
    collector.credential_sets = [
        {"label": "primary", "client_id": "primary-client", "client_secret": "primary-secret"},
        {"label": "backup", "client_id": "backup-client", "client_secret": "backup-secret"},
    ]
    collector.credential_index = 0
    collector.client_id = "primary-client"
    collector.client_secret = "primary-secret"

    assert collector.get_access_token() == "backup-token"
    assert collector.credential_index == 1
    output = capsys.readouterr().out
    assert "primary-secret" not in output
    assert "backup-secret" not in output


def test_statistics_tries_every_credential_before_exhausting_retries(monkeypatch):
    class Response:
        def __init__(self, status_code):
            self.status_code = status_code

    responses = iter([Response(403), Response(403), Response(403), Response(403), Response(200)])
    monkeypatch.setattr("data_collection.collectors.sentinel2.requests.post", lambda *args, **kwargs: next(responses))

    collector = StatisticalCollection.__new__(StatisticalCollection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "primary-token"
    collector.max_cloud_coverage = 30
    collector.credential_index = 0
    collector.credential_sets = [
        {"label": label, "client_id": f"{label}-id", "client_secret": f"{label}-secret"}
        for label in ("primary", "backup", "backup_2", "backup_3", "backup_4")
    ]

    def switch_to_next_credentials(_status_code):
        if collector.credential_index + 1 >= len(collector.credential_sets):
            return False
        collector.credential_index += 1
        collector.access_token = f"token-{collector.credential_index}"
        return True

    collector._switch_to_next_credentials = switch_to_next_credentials

    response = collector.get_request("//VERSION=3", ("2026-06-01", "2026-06-01"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 200
    assert collector.credential_index == 4


def test_statistics_uses_next_midnight_for_daily_aggregation(monkeypatch):
    class Response:
        status_code = 200

    payloads = []

    def post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("data_collection.collectors.sentinel2.requests.post", post)
    collector = StatisticalCollection.__new__(StatisticalCollection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "token"
    collector.max_cloud_coverage = 30

    response = collector.get_request("//VERSION=3", ("2026-07-01", "2026-07-01"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 200
    payload = payloads[0]
    assert payload["input"]["data"][0]["dataFilter"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
    }
    assert payload["aggregation"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
    }


def test_statistics_uses_day_after_interval_end_for_multi_day_aggregation(monkeypatch):
    class Response:
        status_code = 200

    payloads = []

    def post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("data_collection.collectors.sentinel2.requests.post", post)
    collector = StatisticalCollection.__new__(StatisticalCollection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "token"
    collector.max_cloud_coverage = 30

    response = collector.get_request("//VERSION=3", ("2026-07-01", "2026-07-13"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 200
    payload = payloads[0]
    assert payload["input"]["data"][0]["dataFilter"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-14T00:00:00Z",
    }
    assert payload["aggregation"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-14T00:00:00Z",
    }


def test_statistics_prints_cdse_500_response_body(monkeypatch, capsys):
    class Response:
        status_code = 500
        headers = {"x-request-id": "cdse-request-123"}
        text = '{"error":{"message":"internal processing failure"}}'

    monkeypatch.setattr("data_collection.collectors.sentinel2.requests.post", lambda *_args, **_kwargs: Response())
    collector = StatisticalCollection.__new__(StatisticalCollection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "token"
    collector.max_cloud_coverage = 30

    response = collector.get_request("//VERSION=3", ("2026-07-01", "2026-07-01"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 500
    output = capsys.readouterr().out
    assert "status=500" in output
    assert "request_id=cdse-request-123" in output
    assert 'body={"error":{"message":"internal processing failure"}}' in output


def test_sentinel3_statistics_uses_next_midnight_for_daily_aggregation(monkeypatch):
    class Response:
        status_code = 200

    payloads = []

    def post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post", post)
    collector = Sentinel3Collection.__new__(Sentinel3Collection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "token"

    response = collector.get_request("//VERSION=3", ("2026-07-01", "2026-07-01"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 200
    payload = payloads[0]
    assert payload["input"]["data"][0]["dataFilter"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
    }
    assert payload["aggregation"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-02T00:00:00Z",
    }


def test_sentinel3_statistics_uses_day_after_interval_end_for_multi_day_aggregation(monkeypatch):
    class Response:
        status_code = 200

    payloads = []

    def post(*_args, **kwargs):
        payloads.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post", post)
    collector = Sentinel3Collection.__new__(Sentinel3Collection)
    collector.api_url = "https://example.test/statistics"
    collector.access_token = "token"

    response = collector.get_request("//VERSION=3", ("2026-07-01", "2026-07-13"), [22.0, 38.0, 22.1, 38.1])

    assert response.status_code == 200
    payload = payloads[0]
    assert payload["input"]["data"][0]["dataFilter"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-14T00:00:00Z",
    }
    assert payload["aggregation"]["timeRange"] == {
        "from": "2026-07-01T00:00:00Z",
        "to": "2026-07-14T00:00:00Z",
    }


def test_retryable_failure_is_not_written_as_no_data_and_resumes(tmp_path):
    failing = CollectionService(
        discovery_factory=lambda _cloud: FakeDiscovery(DATES),
        statistics_factory=FailingStatistics,
        tile_extractor_factory=FakeTileExtractor,
    )
    first = failing.collect(request(tmp_path))

    assert first.status == "partial"
    assert len(first.failed_units) == 2
    assert not Path(first.history_json_path).exists()

    resumed = service(FakeDiscovery(DATES)).collect(request(tmp_path))
    assert resumed.status == "success"
    assert resumed.new_record_count == 2


def test_genuine_no_data_is_terminal_with_strict_nulls(tmp_path):
    collector = CollectionService(
        discovery_factory=lambda _cloud: FakeDiscovery(DATES),
        statistics_factory=EmptyStatistics,
        tile_extractor_factory=FakeTileExtractor,
    )
    result = collector.collect(request(tmp_path))
    raw = Path(result.history_json_path).read_text()
    history = json.loads(raw)

    assert all(record["collection_status"] == "unavailable" for record in history)
    assert all(record["CDOM"] is None for record in history)
    assert "NaN" not in raw


def test_collector_collects_every_tile_without_water_screening(tmp_path):
    collector = CollectionService(
        discovery_factory=lambda _cloud: FakeDiscovery(DATES),
        statistics_factory=FakeStatistics,
        tile_extractor_factory=MultiTileExtractor,
    )
    result = collector.collect(request(tmp_path))
    history = json.loads(Path(result.history_json_path).read_text())

    assert {(record["tile_id"], record["observation_date"]) for record in history} == {
        ("tile_0", "2026-01-01"),
        ("tile_0", "2026-01-06"),
        ("tile_1", "2026-01-01"),
        ("tile_1", "2026-01-06"),
    }
    assert all(record["water_check_status"] == "not_performed" for record in history)
    assert not (Path(result.run_dir) / "water").exists()
    assert "waterMask" not in sentinel2_statistics_all_pixels


class FakeCollectorStorage:
    def __init__(self):
        self.uploads = {}
        self.observations = []
        self.tiles = []
        self.collection_state = None
        self.runs = []
        self.closed = False

    def initialize(self):
        pass

    def ensure_aoi_definition(self, definition):
        self.definition = definition
        return {"key": self.aoi_key(relative_path="definition.json")}

    def load_observations(self):
        return []

    def download_json(self, *, key):
        return None

    def aoi_key(self, *, relative_path):
        return f"terra-uc1/test/aoi/{relative_path}"

    def data_key(self, *, relative_path):
        return f"terra-uc1/test/{relative_path}"

    def run_key(self, *, run_id, relative_path):
        return f"terra-uc1/test/runs/{run_id}/{relative_path}"

    def upload_file_if_changed(self, path, *, key, content_type):
        self.uploads[key] = Path(path).read_text()
        return {"key": key, "content_type": content_type}

    def upload_json_if_changed(self, value, *, key):
        self.uploads[key] = value
        return {"key": key, "content_type": "application/json"}

    def upload_json_file(self, path, *, key):
        self.uploads[key] = json.loads(Path(path).read_text())
        return {"key": key, "content_type": "application/json"}

    def upload_file(self, path, *, key, content_type):
        self.uploads[key] = Path(path).read_text()
        return {"key": key, "content_type": content_type}

    def upsert_observations(self, records, *, run_id):
        self.observations.extend(records)

    def upsert_tiles(self, records, *, run_id):
        self.tiles.extend(records)

    def upsert_collection_state(self, state, *, run_id):
        self.collection_state = state

    def record_run(self, payload, *, run_id):
        self.runs.append({**payload, "run_id": run_id})

    def close(self):
        self.closed = True


def test_collector_publishes_its_contract_and_run_lifecycle(tmp_path):
    storage = FakeCollectorStorage()
    collector = CollectionService(
        discovery_factory=lambda _cloud: FakeDiscovery(DATES),
        statistics_factory=FakeStatistics,
        tile_extractor_factory=FakeTileExtractor,
        storage=storage,
    )

    result = collector.collect(request(tmp_path, aoi_id="test", publish=True))

    assert result.status == "success"
    assert len(storage.observations) == 2
    assert len(storage.tiles) == 1
    assert storage.collection_state["last_checked_date"] == "2026-01-06"
    assert [run["status"] for run in storage.runs] == ["running", "success"]
    assert all(run["run_id"].startswith("collector-run-") for run in storage.runs)
    assert "terra-uc1/test/observations/2026-01-01.json" in storage.uploads
    assert any(key.endswith("collection/collection_run_result.json") for key in storage.uploads)
    assert storage.closed is True
