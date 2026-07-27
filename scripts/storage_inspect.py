"""Read-only inspection of the configured UC1 MongoDB and MinIO storage."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forecaster.storage import MongoMinioStore, StorageSettings


COLLECTIONS = ("observations", "preprocessed_features", "forecasts", "tiles", "pipeline_runs")


def main() -> int:
    settings = StorageSettings.from_env()
    if not settings.enabled:
        raise SystemExit("Set TERRA_STORAGE_ENABLED=true before inspecting storage.")

    store = MongoMinioStore(settings)
    try:
        database = store.database
        print(f"MongoDB database: {settings.mongo_database}")
        print("Collections:")
        for name in COLLECTIONS:
            indexes = [index["name"] for index in database[name].list_indexes()]
            count = database[name].count_documents({"aoi_id": settings.aoi_id})
            print(f"- {name}: {count} documents for aoi={settings.aoi_id}; indexes={', '.join(indexes)}")

        prefix = f"terra-uc1/{settings.aoi_id}/"
        response = store.s3.list_objects_v2(Bucket=settings.minio_bucket, Prefix=prefix)
        print(f"MinIO bucket: {settings.minio_bucket}")
        print(f"Prefix: {prefix}")
        print(f"Objects: {response.get('KeyCount', 0)}")
        for item in response.get("Contents", []):
            print(f"- {item['Key']} ({item['Size']} bytes)")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
