"""Create and validate the UC1 application collections and indexes.

The selected MongoDB user must already exist and the selected MinIO bucket must
already be provisioned. This command never creates users, buckets, or deletes
data.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forecaster.storage import MongoMinioStore, StorageConfigurationError, StorageConnectionError, StorageSettings


def main() -> int:
    try:
        settings = StorageSettings.from_env()
        if not settings.enabled:
            raise StorageConfigurationError("Set TERRA_STORAGE_ENABLED=true before initializing storage.")
        store = MongoMinioStore(settings)
        store.initialize()
        print(f"Storage initialized for aoi={settings.aoi_id} database={settings.mongo_database} bucket={settings.minio_bucket}")
        return 0
    except (StorageConfigurationError, StorageConnectionError) as exc:
        print(f"Storage initialization failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if "store" in locals():
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
