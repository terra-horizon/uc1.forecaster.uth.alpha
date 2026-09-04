"""Collector-owned MongoDB and MinIO persistence.

This module deliberately has no dependency on the forecaster package. The
collector publishes its AOI definition, tiles, observations, collection
checkpoints, and run metadata; the forecaster consumes those contracts later.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError
from urllib3.exceptions import InsecureRequestWarning


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class StorageConfigurationError(ValueError):
    """Raised when collector storage configuration is incomplete."""


class StorageConnectionError(RuntimeError):
    """Raised when collector storage cannot be reached."""


def _required(name: str, *, hint: str | None = None) -> str:
    value = os.getenv(name)
    if value:
        return value
    message = f"Missing required storage environment variable: {name}."
    if hint:
        message += f" {hint}"
    raise StorageConfigurationError(message)


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class CollectorStorageSettings:
    aoi_id: str
    mongo_uri: str
    mongo_database: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_verify_tls: str | bool = True

    @classmethod
    def from_env(cls, *, aoi_id: str) -> "CollectorStorageSettings":
        mongo_uri = _required(
            "MONGO_URI",
            hint="Set a complete URI including the database and authentication source.",
        )
        parsed_mongo = urlsplit(mongo_uri)
        if parsed_mongo.scheme not in {"mongodb", "mongodb+srv"} or not parsed_mongo.hostname:
            raise StorageConfigurationError("MONGO_URI must be a complete MongoDB URI.")
        database = parsed_mongo.path.lstrip("/")
        if not database:
            raise StorageConfigurationError("MONGO_URI must include a database name.")

        endpoint = _required("MINIO_ENDPOINT", hint="Set the MinIO S3 API HTTP(S) URL.")
        parsed_endpoint = urlsplit(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            raise StorageConfigurationError("MINIO_ENDPOINT must be a complete HTTP(S) URL.")
        verify: str | bool = _as_bool(os.getenv("MINIO_VERIFY_TLS"), default=True)
        if os.getenv("MINIO_CA_BUNDLE"):
            verify = os.environ["MINIO_CA_BUNDLE"]
        return cls(
            aoi_id=str(aoi_id),
            mongo_uri=mongo_uri,
            mongo_database=database,
            minio_endpoint=endpoint,
            minio_access_key=_required("MINIO_ACCESS_KEY"),
            minio_secret_key=_required("MINIO_SECRET_KEY"),
            minio_bucket=_required("MINIO_BUCKET_NAME"),
            minio_verify_tls=verify,
        )

    @property
    def mongo_target(self) -> str:
        parsed = urlsplit(self.mongo_uri)
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{parsed.hostname}{port}/{self.mongo_database}"


def build_aoi_definition(
    *,
    aoi_id: str,
    bbox: list[float],
    projected_crs: str,
    spacing_m: int,
    box_size_m: int,
    min_river_length_m: float,
) -> dict[str, Any]:
    """Build the same stable AOI contract expected by the forecaster."""
    definition = {
        "schema_version": "1.0.0",
        "aoi_id": str(aoi_id),
        "bbox": [float(value) for value in bbox],
        "crs": str(projected_crs),
        "tiling": {
            "spacing_m": int(spacing_m),
            "box_size_m": int(box_size_m),
            "min_river_length_m": float(min_river_length_m),
        },
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    definition["aoi_definition_hash"] = hashlib.sha256(canonical).hexdigest()
    return definition


class CollectorStore:
    """Idempotent collector documents and checksum-addressed MinIO artifacts."""

    def __init__(self, settings: CollectorStorageSettings):
        self.settings = settings
        self.aoi_definition_hash: str | None = None
        self._client: MongoClient | None = None
        self._s3 = None

    @property
    def database(self):
        if self._client is None:
            self._client = MongoClient(
                self.settings.mongo_uri,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
            )
        return self._client[self.settings.mongo_database]

    @property
    def s3(self):
        if self._s3 is None:
            if self.settings.minio_verify_tls is False:
                import urllib3

                urllib3.disable_warnings(InsecureRequestWarning)
            self._s3 = boto3.client(
                "s3",
                endpoint_url=self.settings.minio_endpoint,
                aws_access_key_id=self.settings.minio_access_key,
                aws_secret_access_key=self.settings.minio_secret_key,
                config=Config(signature_version="s3v4"),
                region_name="us-east-1",
                verify=self.settings.minio_verify_tls,
            )
        return self._s3

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def initialize(self) -> None:
        try:
            self.database.command("ping")
        except (PyMongoError, ValueError) as exc:
            raise StorageConnectionError(
                f"MongoDB preflight failed for {self.settings.mongo_target}. Check MONGO_URI and any required host-side tunnel."
            ) from exc
        try:
            self.s3.head_bucket(Bucket=self.settings.minio_bucket)
        except (BotoCoreError, ClientError, EndpointConnectionError, OSError) as exc:
            raise StorageConnectionError(
                f"MinIO preflight failed for endpoint={self.settings.minio_endpoint!r}, bucket={self.settings.minio_bucket!r}."
            ) from exc
        indexes = {
            "observations": [("aoi_id", ASCENDING), ("tile_id", ASCENDING), ("observation_date", ASCENDING)],
            "tiles": [("aoi_id", ASCENDING), ("tile_id", ASCENDING)],
            "collection_state": [("aoi_id", ASCENDING)],
            "pipeline_runs": [("run_id", ASCENDING)],
        }
        for collection, keys in indexes.items():
            self.database[collection].create_index(
                keys,
                unique=True,
                name="unique_" + "_".join(key for key, _ in keys),
            )
        self.database["pipeline_runs"].create_index([("component", ASCENDING), ("aoi_id", ASCENDING), ("updated_at", ASCENDING)])

    def ensure_aoi_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        if str(definition.get("aoi_id")) != self.settings.aoi_id:
            raise ValueError("AOI definition does not match the collector storage AOI.")
        definition_hash = str(definition.get("aoi_definition_hash") or "")
        if not definition_hash:
            raise ValueError("AOI definition is missing aoi_definition_hash.")
        self.aoi_definition_hash = definition_hash
        key = self.aoi_key(relative_path="definition.json")
        existing = self.download_json(key=key)
        if existing is not None and existing.get("aoi_definition_hash") != definition_hash:
            raise ValueError("AOI definition changed for this aoi_id; use a new AOI ID or explicitly migrate its data.")
        return self.upload_json_if_changed(definition, key=key)

    def upsert_observations(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        self._upsert_many("observations", records, ("aoi_id", "tile_id", "observation_date"), run_id=run_id)

    def load_observations(self) -> list[dict[str, Any]]:
        return [
            self._without_id(record)
            for record in self.database["observations"].find({"aoi_id": self.settings.aoi_id}).sort(
                [("tile_id", ASCENDING), ("observation_date", ASCENDING)]
            )
        ]

    def upsert_tiles(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        payload = [{**record, "tile_id": str(record.get("name") or record.get("tile_id"))} for record in records]
        self._upsert_many("tiles", payload, ("aoi_id", "tile_id"), run_id=run_id)

    def upsert_collection_state(self, state: dict[str, Any], *, run_id: str) -> None:
        self._upsert_many("collection_state", [state], ("aoi_id",), run_id=run_id)

    def record_run(self, payload: dict[str, Any], *, run_id: str) -> None:
        now = utc_now()
        document = {
            **payload,
            "run_id": run_id,
            "component": "collector",
            "aoi_id": self.settings.aoi_id,
            "updated_at": now,
        }
        if self.aoi_definition_hash:
            document["aoi_definition_hash"] = self.aoi_definition_hash
        self.database["pipeline_runs"].update_one(
            {"run_id": run_id},
            {"$set": document, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def upload_json(self, value: Any, *, key: str) -> dict[str, Any]:
        encoded = json.dumps(value, indent=2, allow_nan=False).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        self.s3.put_object(
            Bucket=self.settings.minio_bucket,
            Key=key,
            Body=encoded,
            ContentType="application/json",
            Metadata={"sha256": checksum},
        )
        return {"bucket": self.settings.minio_bucket, "key": key, "sha256": checksum, "content_type": "application/json"}

    def upload_file(self, path: Path, *, key: str, content_type: str) -> dict[str, Any]:
        encoded = path.read_bytes()
        checksum = hashlib.sha256(encoded).hexdigest()
        self.s3.put_object(
            Bucket=self.settings.minio_bucket,
            Key=key,
            Body=encoded,
            ContentType=content_type,
            Metadata={"sha256": checksum},
        )
        return {"bucket": self.settings.minio_bucket, "key": key, "sha256": checksum, "content_type": content_type}

    def upload_json_file(self, path: Path, *, key: str) -> dict[str, Any]:
        return self.upload_json(json.loads(path.read_text(encoding="utf-8")), key=key)

    def upload_json_if_changed(self, value: Any, *, key: str) -> dict[str, Any]:
        encoded = json.dumps(value, indent=2, allow_nan=False).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        if self._remote_checksum(key) == checksum:
            return {"bucket": self.settings.minio_bucket, "key": key, "sha256": checksum, "content_type": "application/json", "unchanged": True}
        return self.upload_json(value, key=key)

    def upload_file_if_changed(self, path: Path, *, key: str, content_type: str) -> dict[str, Any]:
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        if self._remote_checksum(key) == checksum:
            return {"bucket": self.settings.minio_bucket, "key": key, "sha256": checksum, "content_type": content_type, "unchanged": True}
        return self.upload_file(path, key=key, content_type=content_type)

    def download_json(self, *, key: str) -> Any | None:
        try:
            response = self.s3.get_object(Bucket=self.settings.minio_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return json.loads(response["Body"].read().decode("utf-8"))

    def aoi_key(self, *, relative_path: str) -> str:
        return f"terra-uc1/{self.settings.aoi_id}/aoi/{relative_path.lstrip('/')}"

    def data_key(self, *, relative_path: str) -> str:
        return f"terra-uc1/{self.settings.aoi_id}/{relative_path.lstrip('/')}"

    def run_key(self, *, run_id: str, relative_path: str) -> str:
        return f"terra-uc1/{self.settings.aoi_id}/runs/{run_id}/{relative_path.lstrip('/')}"

    def _remote_checksum(self, key: str) -> str | None:
        try:
            response = self.s3.head_object(Bucket=self.settings.minio_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return (response.get("Metadata") or {}).get("sha256")

    def _upsert_many(self, collection: str, records: list[dict[str, Any]], fields: tuple[str, ...], *, run_id: str) -> None:
        now = utc_now()
        for record in records:
            document = {**record, "aoi_id": self.settings.aoi_id, "last_run_id": run_id, "updated_at": now}
            created_at = document.pop("created_at", now)
            if self.aoi_definition_hash:
                document["aoi_definition_hash"] = self.aoi_definition_hash
            selector = {field: document[field] for field in fields}
            self.database[collection].update_one(
                selector,
                {"$set": document, "$setOnInsert": {"created_at": created_at}},
                upsert=True,
            )

    @staticmethod
    def _without_id(value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key != "_id"}


class Sentinel3Store(CollectorStore):
    """Reuse AOI geometry while isolating S3 observations and checkpoints."""

    def initialize(self):
        super().initialize()
        self.database["sentinel3_observations"].create_index(
            [("aoi_id", ASCENDING), ("tile_id", ASCENDING), ("observation_date", ASCENDING)],
            unique=True, name="unique_aoi_id_tile_id_observation_date",
        )
        self.database["sentinel3_collection_state"].create_index(
            [("aoi_id", ASCENDING)], unique=True, name="unique_aoi_id",
        )

    def load_observations(self):
        return [self._without_id(row) for row in
                self.database["sentinel3_observations"].find(
                    {"aoi_id": self.settings.aoi_id}).sort(
                    [("tile_id", ASCENDING), ("observation_date", ASCENDING)])]

    def upsert_observations(self, records, *, run_id):
        self._upsert_many("sentinel3_observations", records,
                          ("aoi_id", "tile_id", "observation_date"), run_id=run_id)

    def upsert_collection_state(self, state, *, run_id):
        self._upsert_many("sentinel3_collection_state", [state], ("aoi_id",), run_id=run_id)

    def aoi_key(self, *, relative_path):
        if relative_path == "collection_state.json":
            relative_path = "sentinel3/collection_state.json"
        return super().aoi_key(relative_path=relative_path)

    def data_key(self, *, relative_path):
        return super().data_key(relative_path="sentinel3/" + relative_path.lstrip("/"))

    def record_run(self, payload, *, run_id):
        super().record_run({**payload, "sensor": "sentinel3"}, run_id=run_id)
