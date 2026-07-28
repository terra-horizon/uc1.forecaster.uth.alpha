"""Read-only UC1 storage health check."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forecaster.storage import MongoMinioStore, StorageConfigurationError, StorageConnectionError, StorageSettings


def main() -> int:
    try:
        settings = StorageSettings.from_env()
        store = MongoMinioStore(settings)
        store.initialize()
        print(f"Storage healthy: aoi={settings.aoi_id} mongo={settings.mongo_target} bucket={settings.minio_bucket}")
        return 0
    except (StorageConfigurationError, StorageConnectionError) as exc:
        print(f"Storage unhealthy: {exc}", file=sys.stderr)
        return 2
    finally:
        if "store" in locals():
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
