import os
import tempfile
import time
from pathlib import Path
from datetime import date, timedelta
import boto3
from pymongo import MongoClient
from data_collection.service import CollectionService
from data_collection.models import CollectionRequest
from data_collection.storage import write_json
from data_collection.validation import validate_run
from data_collection.collectors.sentinel3 import METRIC

assert os.environ["MONGO_URI"] == "mongodb://mongo:27017/collector_s3_validation"
assert os.environ["MINIO_ENDPOINT"] == "http://minio:9000"
assert os.environ["MINIO_BUCKET_NAME"] == "s3-validation"

mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=1000)
s3 = boto3.client("s3", endpoint_url=os.environ["MINIO_ENDPOINT"],
                 aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
                 aws_secret_access_key=os.environ["MINIO_SECRET_KEY"])
for attempt in range(30):
    try:
        mongo.admin.command("ping")
        s3.list_buckets()
        break
    except Exception:
        time.sleep(1)
else:
    raise RuntimeError("Local validation storage did not become ready")
s3.create_bucket(Bucket="s3-validation")
db = mongo.get_default_database()
db.observations.insert_one({"aoi_id": "validation", "tile_id": "tile_0",
                           "observation_date": "2020-01-01", "CDOM": 123})
db.collection_state.insert_one({"aoi_id": "validation", "backfill_complete": True})
s2_snapshot = db.observations.find_one()
s2_state = db.collection_state.find_one()
s3.put_object(Bucket="s3-validation", Key="terra-uc1/validation/aoi/collection_state.json", Body=b'{"sentinel2":true}')

class Tiles:
    def __init__(self, config):
        self.config = config
    def extract_to_geojson(self, path):
        write_json(path, {"type": "FeatureCollection", "aoi_bbox": self.config.aoi_bbox,
                         "tile_count": 1, "features": [{
            "type": "Feature", "properties": {"name": "tile_0"},
            "geometry": {"type": "Polygon", "coordinates": [
                [[22,38],[22.01,38],[22.01,38.01],[22,38.01],[22,38]]]},
        }]})

class S3Fixture:
    def __init__(self, *, bbox, dir):
        self.dir = Path(dir)
    def collect_daily(self, start, end):
        write_json(self.dir / f"{start}_{end}.json", {"fixture": True})
        return {
            (date.fromisoformat(start) + timedelta(days=n)).isoformat(): {
                "collection_status": "collected", METRIC: 20.,
                "min_c": 19., "max_c": 21., "stdev_c": .5,
                "sample_count": 4, "valid_sample_count": 4,
                "quality_flags": ["cloud_mask_not_applied"], "stac_item_ids": ["S3_fixture"],
            } for n in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
        }

root = Path(tempfile.mkdtemp(prefix="s3-storage-"))
def run(limit=None, output=root):
    return CollectionService(tile_extractor_factory=Tiles, sentinel3_factory=S3Fixture).collect(
        CollectionRequest(aoi_bbox=[22,38,22.01,38.01], aoi_id="validation",
                          run_name="validation", output_root=output, sensor="sentinel3",
                          history_start="2020-01-01", target_date="2020-01-02",
                          max_days_per_run=limit, publish=True))

assert run(1).status == "partial"
assert db.sentinel3_observations.count_documents({}) == 1
assert not db.sentinel3_collection_state.find_one()["backfill_complete"]
assert run().new_record_count == 1
assert run().new_record_count == 0
# Empty local directory must hydrate only S3 records/state from Mongo/MinIO.
hydrated = run(output=Path(tempfile.mkdtemp(prefix="s3-hydrated-")))
assert hydrated.new_record_count == 0
report = validate_run(Path(hydrated.run_dir))
assert report["valid"], report
assert db.sentinel3_observations.count_documents({}) == 2
assert db.observations.find_one() == s2_snapshot
assert db.collection_state.find_one() == s2_state
assert s3.get_object(Bucket="s3-validation", Key="terra-uc1/validation/aoi/collection_state.json")["Body"].read() == b'{"sentinel2":true}'
assert db.sentinel3_collection_state.find_one()["backfill_complete"]
for row in db.sentinel3_observations.find():
    assert row["raw_artifact"]["key"].startswith("terra-uc1/validation/sentinel3/raw/")
    s3.head_object(Bucket="s3-validation", Key=row["raw_artifact"]["key"])
print("PASS: real Mongo/MinIO persistence, raw artifacts, incremental resume, hydration, deduplication, S2 isolation")
