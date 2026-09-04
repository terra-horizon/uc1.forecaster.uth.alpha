from datetime import date, timedelta
import json
from pathlib import Path

import pytest

from data_collection.collectors.sentinel3 import Sentinel3Collection, METRIC
from data_collection.models import CollectionRequest
from data_collection.service import CollectionService
from data_collection.storage import write_json
from data_collection.validation import validate_run
from data_collection.remote_storage import Sentinel3Store, CollectorStorageSettings
from test_collection_service import FakeTileExtractor, MultiTileExtractor, FakeCollectorStorage


def measured():
    return {
        "collection_status": "collected", METRIC: 18.5,
        "min_c": 17., "max_c": 20., "stdev_c": 1.,
        "sample_count": 4, "valid_sample_count": 4,
        "quality_flags": ["cloud_mask_not_applied"], "stac_item_ids": ["S3_A"],
    }


class FakeS3:
    calls = []

    def __init__(self, *, bbox, dir):
        self.dir = Path(dir)

    def collect_daily(self, start, end):
        self.calls.append((start, end))
        write_json(self.dir / f"{start}_{end}.json", {"test": True})
        return {(date.fromisoformat(start) + timedelta(days=i)).isoformat(): measured()
                for i in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)}


def request(tmp_path, **kw):
    return CollectionRequest(**{
        "aoi_bbox": [22, 38, 22.01, 38.01], "run_name": "shared",
        "aoi_id": "sperchios", "output_root": tmp_path, "sensor": "sentinel3",
        "history_start": "2020-02-28", "target_date": "2020-03-01",
        "publish": False, **kw,
    })


def service(factory=FakeS3, **kw):
    def forbidden(*args, **kwargs):
        raise AssertionError("Sentinel-2 must never be invoked by S3")
    return CollectionService(
        discovery_factory=forbidden, statistics_factory=forbidden,
        sentinel3_factory=factory, tile_extractor_factory=FakeTileExtractor, **kw)


def test_calendar_independence_leap_day_resume_and_csv(tmp_path):
    FakeS3.calls = []
    first = service().collect(request(tmp_path))
    assert first.status == "success"
    history = json.loads(Path(first.history_json_path).read_text())
    assert [row["observation_date"] for row in history] == ["2020-02-28", "2020-02-29", "2020-03-01"]
    assert METRIC in Path(first.history_csv_path).read_text().splitlines()[0]
    assert validate_run(Path(first.run_dir))["valid"]
    before = Path(first.history_json_path).read_bytes()
    second = service().collect(request(tmp_path, mode="incremental"))
    assert second.new_record_count == 0
    assert len(FakeS3.calls) == 1
    assert Path(second.history_json_path).read_bytes() == before
    assert not (tmp_path / "shared/history").exists()


def test_limit_does_not_claim_complete_and_resumes(tmp_path):
    first = service().collect(request(tmp_path, max_days_per_run=1))
    assert first.status == "partial"
    assert not json.loads(Path(first.state_path).read_text())["backfill_complete"]
    assert service().collect(request(tmp_path)).new_record_count == 2


def test_failures_and_omitted_intervals_stay_retryable(tmp_path):
    class Missing(FakeS3):
        def collect_daily(self, start, end):
            return {}
    first = service(Missing).collect(request(tmp_path))
    assert first.status == "partial"
    assert first.new_record_count == 0
    assert len(first.failed_units) == 3
    assert service().collect(request(tmp_path)).new_record_count == 3


def test_restart_recovers_per_tile_checkpoint(tmp_path):
    first = service().collect(request(tmp_path, max_days_per_run=1))
    # Simulate process loss before the consolidated global export.
    Path(first.history_json_path).unlink()
    FakeS3.calls = []
    service().collect(request(tmp_path))
    assert FakeS3.calls == [("2020-02-29", "2020-03-01")]


def test_dry_run_no_network_no_files(tmp_path):
    assert service().collect(request(tmp_path, dry_run=True)).status == "dry_run"
    assert list(tmp_path.iterdir()) == []


def test_changed_geometry_cannot_reuse_old_temperature_history(tmp_path):
    service().collect(request(tmp_path))
    with pytest.raises(ValueError, match="AOI/tiling changed"):
        service().collect(request(tmp_path, box_size_m=800))


def test_chunk_is_persisted_before_final_publish(tmp_path):
    storage = FakeCollectorStorage()
    result = service(storage=storage).collect(request(tmp_path, publish=True))
    assert result.status == "success"
    assert any(row.get("raw_artifact") for row in storage.observations)
    assert storage.collection_state["backfill_complete"]


def payload(mean=18.5, count=4, nodata=0):
    return {"data": [{
        "interval": {"from": "2020-02-28T00:00:00Z"},
        "outputs": {"data": {"bands": {"B0": {"stats": {
            "mean": mean, "min": 17., "max": 20., "stDev": 1.,
            "sampleCount": count, "noDataCount": nodata,
        }}}}},
    }]}


def test_parser_preserves_distinct_statistics_and_valid_count():
    row = Sentinel3Collection.parse_daily(payload(), "2020-02-28", "2020-02-28")["2020-02-28"]
    assert row[METRIC] == 18.5
    assert row["min_c"] == 17.
    assert row["stdev_c"] == 1.
    assert row["valid_sample_count"] == 4


def test_invalid_pixels_are_null_and_no_scene_is_distinct(tmp_path):
    rows = Sentinel3Collection.parse_daily(payload(mean="NaN", nodata=4), "2020-02-28", "2020-02-28")
    assert rows["2020-02-28"][METRIC] is None
    client = Sentinel3Collection.__new__(Sentinel3Collection)
    client.dir = tmp_path
    client.discover = lambda *_: {}
    client.get_request = lambda *_: pytest.fail("No statistics needed for confirmed catalogue absence")
    assert client.collect_daily("2016-01-01", "2016-01-02")["2016-01-01"]["quality_flags"] == ["no_catalogue_scene"]


@pytest.mark.parametrize("body", [{}, {"data": [{}]}, payload(mean="NaN"), payload(count=1, nodata=2)])
def test_malformed_stats_fail_instead_of_becoming_nodata(body):
    with pytest.raises((ValueError, KeyError)):
        Sentinel3Collection.parse_daily(body, "2020-02-28", "2020-02-28")


def test_catalogue_pagination_and_dedup(monkeypatch):
    client = Sentinel3Collection.__new__(Sentinel3Collection)
    client.bbox, client.access_token = [22, 38, 22.01, 38.01], "test-token"
    pages = iter([
        {"features": [{"id": "S3_A", "properties": {"datetime": "2020-02-28T12:00:00Z"}}], "context": {"next": 100}},
        {"features": [{"id": "S3_A", "properties": {"datetime": "2020-02-28T12:00:00Z"}}], "context": {}},
    ])
    class Response:
        status_code = 200
        def json(self):
            return next(pages)
    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post", lambda *a, **kw: Response())
    assert client.discover("2020-02-28", "2020-02-28") == {"2020-02-28": ["S3_A"]}


def test_401_is_bounded_and_403_is_not_rate_limiting(monkeypatch):
    client = Sentinel3Collection.__new__(Sentinel3Collection)
    client.access_token, client.api_url = "token", "https://test.invalid"
    client.get_access_token = lambda: "token"
    calls = []
    class Response:
        status_code = 401
    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post",
                        lambda *a, **kw: calls.append(1) or Response())
    with pytest.raises(RuntimeError):
        client.get_request("", ("2020-02-28", "2020-02-28"), [22,38,22.01,38.01])
    assert len(calls) == 3
    Response.status_code = 403
    calls.clear()
    with pytest.raises(RuntimeError, match="403"):
        client.get_request("", ("2020-02-28", "2020-02-28"), [22,38,22.01,38.01])
    assert len(calls) == 1


def test_http_200_interval_processing_error_is_retried(monkeypatch):
    client = Sentinel3Collection.__new__(Sentinel3Collection)
    client.access_token, client.api_url = "token", "https://test.invalid"
    client.get_access_token = lambda: "token"
    responses = iter([
        {"data": [{"error": {"type": "EXECUTION_ERROR", "message": "temporary"}}]},
        {"data": [{"error": {"type": "EXECUTION_ERROR", "message": "temporary"}}]},
        payload(),
    ])
    calls = []
    class Response:
        status_code = 200
        def __init__(self, body):
            self.body = body
        def json(self):
            return self.body
    monkeypatch.setattr("data_collection.collectors.sentinel3.time.sleep", lambda seconds: None)
    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post",
                        lambda *a, **kw: calls.append(1) or Response(next(responses)))
    response = client.get_request("", ("2020-02-28", "2020-02-28"), [22,38,22.01,38.01])
    assert response.json() == payload()
    assert len(calls) == 3


def test_http_200_interval_processing_error_preserves_provider_detail(monkeypatch):
    client = Sentinel3Collection.__new__(Sentinel3Collection)
    client.access_token, client.api_url = "token", "https://test.invalid"
    client.get_access_token = lambda: "token"
    class Response:
        status_code = 200
        def json(self):
            return {"data": [{"error": {"type": "EXECUTION_ERROR", "message": "provider detail"}}]}
    monkeypatch.setattr("data_collection.collectors.sentinel3.time.sleep", lambda seconds: None)
    monkeypatch.setattr("data_collection.collectors.sentinel3.requests.post", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match="provider detail"):
        client.get_request("", ("2020-02-28", "2020-02-28"), [22,38,22.01,38.01])


def test_storage_namespaces_do_not_collide():
    settings = CollectorStorageSettings("sperchios", "mongodb://localhost/db", "db",
                                       "http://localhost:9000", "test", "test", "test")
    store = Sentinel3Store(settings)
    assert store.aoi_key(relative_path="collection_state.json").endswith("/aoi/sentinel3/collection_state.json")
    assert store.aoi_key(relative_path="tiles/tile_records.json").endswith("/aoi/tiles/tile_records.json")
    assert store.data_key(relative_path="observations/2020-02-28.json").endswith("/sentinel3/observations/2020-02-28.json")
