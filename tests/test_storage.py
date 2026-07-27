from __future__ import annotations

import pytest

from forecaster.aoi import build_aoi_definition
from forecaster.storage import MongoMinioStore, StorageConfigurationError, StorageSettings


def _set_common(monkeypatch) -> None:
    monkeypatch.setenv("TERRA_STORAGE_ENABLED", "true")
    monkeypatch.setenv("TERRA_AOI_ID", "sperchios")
    monkeypatch.setenv("MONGO_URI", "mongodb://terra_app:test-password@mongo.example.test:27017/terra_db?authSource=terra_db")
    monkeypatch.setenv("MINIO_ENDPOINT", "https://minio.example.test")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "terra_app")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test-password")
    monkeypatch.setenv("MINIO_BUCKET_NAME", "terra-uc1")


def test_settings_use_generic_uri_and_endpoint(monkeypatch):
    _set_common(monkeypatch)

    settings = StorageSettings.from_env()

    assert settings.mongo_uri == "mongodb://terra_app:test-password@mongo.example.test:27017/terra_db?authSource=terra_db"
    assert settings.mongo_database == "terra_db"
    assert settings.mongo_target == "mongodb://mongo.example.test:27017/terra_db"
    assert settings.minio_endpoint == "https://minio.example.test"
    assert settings.aoi_id == "sperchios"


def test_storage_requires_complete_mongo_uri(monkeypatch):
    _set_common(monkeypatch)
    monkeypatch.setenv("MONGO_URI", "not-a-mongo-uri")

    with pytest.raises(StorageConfigurationError, match="MONGO_URI must be a complete"):
        StorageSettings.from_env()


def test_storage_defaults_to_disabled(monkeypatch):
    monkeypatch.delenv("TERRA_STORAGE_ENABLED", raising=False)
    settings = StorageSettings.from_env(default_aoi_id="sperchios")
    assert not settings.enabled


def test_storage_keys_separate_aoi_data_and_run_names(monkeypatch):
    _set_common(monkeypatch)
    store = MongoMinioStore(StorageSettings.from_env())

    assert store.aoi_key(relative_path="tiles/river_tiles.geojson") == "terra-uc1/sperchios/aoi/tiles/river_tiles.geojson"
    assert store.data_key(relative_path="observations/2026-06-06.json") == "terra-uc1/sperchios/observations/2026-06-06.json"
    assert store.run_key(run_id="run-123", relative_path="scheduled_run_result.json") == "terra-uc1/sperchios/runs/run-123/scheduled_run_result.json"


def test_aoi_definition_hash_is_stable_and_changes_with_tiling():
    first = build_aoi_definition(
        aoi_id="sperchios",
        bbox=[22.4, 38.8, 22.5, 38.9],
        projected_crs="EPSG:32634",
        spacing_m=400,
        box_size_m=400,
        min_river_length_m=10_000,
    )
    same = build_aoi_definition(
        aoi_id="sperchios",
        bbox=[22.4, 38.8, 22.5, 38.9],
        projected_crs="EPSG:32634",
        spacing_m=400,
        box_size_m=400,
        min_river_length_m=10_000,
    )
    changed = build_aoi_definition(
        aoi_id="sperchios",
        bbox=[22.4, 38.8, 22.5, 38.9],
        projected_crs="EPSG:32634",
        spacing_m=500,
        box_size_m=400,
        min_river_length_m=10_000,
    )

    assert first["aoi_definition_hash"] == same["aoi_definition_hash"]
    assert first["aoi_definition_hash"] != changed["aoi_definition_hash"]
