from __future__ import annotations

from data_collection.remote_storage import CollectorStorageSettings, CollectorStore, build_aoi_definition


def set_storage_env(monkeypatch):
    monkeypatch.setenv("MONGO_URI", "mongodb://collector:test@mongo.example.test:27017/terra_db?authSource=terra_db")
    monkeypatch.setenv("MINIO_ENDPOINT", "https://minio.example.test")
    monkeypatch.setenv("MINIO_ACCESS_KEY", "collector")
    monkeypatch.setenv("MINIO_SECRET_KEY", "test")
    monkeypatch.setenv("MINIO_BUCKET_NAME", "terra-uc1")


def test_collector_storage_is_aoi_scoped(monkeypatch):
    set_storage_env(monkeypatch)
    store = CollectorStore(CollectorStorageSettings.from_env(aoi_id="sperchios"))

    assert store.settings.mongo_database == "terra_db"
    assert store.aoi_key(relative_path="collection_state.json") == "terra-uc1/sperchios/aoi/collection_state.json"
    assert store.data_key(relative_path="observations/2026-01-01.json") == "terra-uc1/sperchios/observations/2026-01-01.json"


def test_collector_aoi_definition_matches_forecaster_contract():
    definition = build_aoi_definition(
        aoi_id="sperchios",
        bbox=[22.4, 38.8, 22.5, 38.9],
        projected_crs="EPSG:32634",
        spacing_m=400,
        box_size_m=400,
        min_river_length_m=10_000,
    )

    assert definition["aoi_id"] == "sperchios"
    assert len(definition["aoi_definition_hash"]) == 64
