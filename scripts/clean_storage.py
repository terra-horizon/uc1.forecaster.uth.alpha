"""Guarded cleanup of one AOI's UC1 MongoDB and MinIO data.

Dry-run is the default. Applying requires both --apply and an exact AOI match
with TERRA_AOI_ID, preventing accidental deletion of another study area.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forecaster.storage import MongoMinioStore, StorageSettings


COLLECTIONS = ("observations", "preprocessed_features", "forecasts", "tiles", "pipeline_runs")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--apply", action="store_true", help="Actually delete the selected AOI data.")
    args = parser.parse_args()

    settings = StorageSettings.from_env()
    if args.aoi != settings.aoi_id:
        raise SystemExit(f"Refusing: --aoi={args.aoi!r} does not match TERRA_AOI_ID.")

    store = MongoMinioStore(settings)
    try:
        counts = {name: store.database[name].count_documents({"aoi_id": args.aoi}) for name in COLLECTIONS}
        prefix = f"terra-uc1/{args.aoi}/"
        keys = [
            item["Key"]
            for page in store.s3.get_paginator("list_objects_v2").paginate(Bucket=settings.minio_bucket, Prefix=prefix)
            for item in page.get("Contents", [])
        ]
        print(f"AOI: {args.aoi}")
        print(f"MongoDB: {counts}")
        print(f"MinIO bucket={settings.minio_bucket} prefix={prefix} objects={len(keys)}")
        if not args.apply:
            print("Dry run only. Add --apply to delete exactly this AOI.")
            return 0

        for name in COLLECTIONS:
            result = store.database[name].delete_many({"aoi_id": args.aoi})
            print(f"Deleted MongoDB {name}: {result.deleted_count}")
        for start in range(0, len(keys), 1000):
            batch = keys[start : start + 1000]
            if batch:
                store.s3.delete_objects(
                    Bucket=settings.minio_bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )
        print(f"Deleted MinIO objects: {len(keys)}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
