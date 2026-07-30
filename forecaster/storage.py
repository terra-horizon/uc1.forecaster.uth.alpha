"""MongoDB and MinIO persistence for the scheduled UC1 pipeline.

Filesystem artifacts remain the local staging format.  This module mirrors the
validated JSON contracts to MinIO and provides MongoDB records for querying and
DataFrame reconstruction.
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class StorageConfigurationError(ValueError):
    """Raised when the storage environment is incomplete or inconsistent."""


class StorageConnectionError(RuntimeError):
    """Raised when MongoDB or MinIO cannot be reached during preflight."""


def _required(name: str, *, hint: str | None = None) -> str:
    value = os.getenv(name)
    if not value:
        message = f"Missing required storage environment variable: {name}."
        if hint:
            message += f" {hint}"
        raise StorageConfigurationError(message)
    return value


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class StorageSettings:
    aoi_id: str
    mongo_uri: str
    mongo_database: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_verify_tls: str | bool = True

    @classmethod
    def from_env(cls, *, aoi_id: str | None = None) -> "StorageSettings":
        """Load shared storage settings, optionally for an explicit AOI."""
        aoi_id = aoi_id or os.getenv("TERRA_AOI_ID")
        if not aoi_id:
            raise StorageConfigurationError("Set TERRA_AOI_ID to the physical study area, for example 'my-river'.")

        mongo_uri = _required(
            "MONGO_URI",
            hint="Set it to a complete URI, for example mongodb://user:password@host:27017/terra_db?authSource=terra_db.",
        )

        parsed_mongo_uri = urlsplit(mongo_uri)
        if parsed_mongo_uri.scheme not in {"mongodb", "mongodb+srv"} or not parsed_mongo_uri.hostname:
            raise StorageConfigurationError("MONGO_URI must be a complete MongoDB URI, for example mongodb://user:password@host:27017/terra_db?authSource=terra_db.")
        database = parsed_mongo_uri.path.lstrip("/")
        if not database:
            raise StorageConfigurationError("MONGO_URI must include a database name, for example .../terra_db?authSource=terra_db.")

        endpoint = _required(
            "MINIO_ENDPOINT",
            hint="Set it to the MinIO S3 API URL, for example http://minio:9000 or https://minio.example.org.",
        )
        parsed_endpoint = urlsplit(endpoint)
        if parsed_endpoint.scheme not in {"http", "https"} or not parsed_endpoint.hostname:
            raise StorageConfigurationError("MINIO_ENDPOINT must be a complete HTTP(S) URL, for example https://minio.example.org.")
        verify: str | bool = _as_bool(os.getenv("MINIO_VERIFY_TLS"), default=True)
        if os.getenv("MINIO_CA_BUNDLE"):
            verify = os.environ["MINIO_CA_BUNDLE"]
        return cls(
            aoi_id=aoi_id,
            mongo_uri=mongo_uri,
            mongo_database=database,
            minio_endpoint=endpoint,
            minio_access_key=_required(
                "MINIO_ACCESS_KEY", hint="Set the application access key for MinIO."
            ),
            minio_secret_key=_required(
                "MINIO_SECRET_KEY", hint="Set the application secret key for MinIO."
            ),
            minio_bucket=_required("MINIO_BUCKET_NAME"),
            minio_verify_tls=verify,
        )

    @property
    def mongo_target(self) -> str:
        """A credential-free target suitable for logs and errors."""
        parsed = urlsplit(self.mongo_uri)
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ":<invalid-port>"
        database = parsed.path.lstrip("/") or "(no database)"
        return f"{parsed.scheme}://{parsed.hostname}{port}/{database}"


class MongoMinioStore:
    """Idempotent MongoDB documents plus immutable MinIO JSON artifacts."""

    def __init__(self, settings: StorageSettings):
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
            # Set MINIO_VERIFY_TLS=false only for a development endpoint that
            # cannot provide a trusted certificate.
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
                f"MongoDB preflight failed for {self.settings.mongo_target}. Check MONGO_URI; "
                "if it uses an SSH tunnel, start the tunnel on the host before running the container."
            ) from exc
        try:
            self.s3.head_bucket(Bucket=self.settings.minio_bucket)
        except (BotoCoreError, ClientError, EndpointConnectionError, OSError) as exc:
            raise StorageConnectionError(
                f"MinIO preflight failed for endpoint={self.settings.minio_endpoint!r}, bucket={self.settings.minio_bucket!r}. "
                "Check MINIO_ENDPOINT, credentials, bucket access, and MINIO_VERIFY_TLS/MINIO_CA_BUNDLE."
            ) from exc
        indexes = {
            "observations": [("aoi_id", ASCENDING), ("tile_id", ASCENDING), ("observation_date", ASCENDING)],
            "preprocessed_features": [("aoi_id", ASCENDING), ("tile_id", ASCENDING), ("feature_date", ASCENDING), ("preprocessing_schema_version", ASCENDING)],
            "forecasts": [("aoi_id", ASCENDING), ("forecast_run_id", ASCENDING), ("tile_id", ASCENDING), ("forecast_date", ASCENDING), ("step", ASCENDING)],
            "tiles": [("aoi_id", ASCENDING), ("tile_id", ASCENDING)],
            "pipeline_runs": [("run_id", ASCENDING)],
        }
        for collection, keys in indexes.items():
            self.database[collection].create_index(keys, unique=True, name="unique_" + "_".join(key for key, _ in keys))
        self.database["pipeline_runs"].create_index([("aoi_id", ASCENDING), ("updated_at", ASCENDING)])
        self.database["forecasts"].create_index([("aoi_id", ASCENDING), ("forecast_date", ASCENDING)])

    def ensure_aoi_definition(self, definition: dict[str, Any]) -> dict[str, Any]:
        """Persist the AOI manifest and reject incompatible definitions."""
        if str(definition.get("aoi_id")) != self.settings.aoi_id:
            raise ValueError("AOI manifest does not match TERRA_AOI_ID.")
        definition_hash = str(definition.get("aoi_definition_hash") or "")
        if not definition_hash:
            raise ValueError("AOI manifest is missing aoi_definition_hash.")
        self.aoi_definition_hash = definition_hash
        key = self.aoi_key(relative_path="definition.json")
        existing = self.download_json(key=key)
        if existing is not None and existing.get("aoi_definition_hash") != definition_hash:
            raise ValueError(
                "AOI definition changed for this aoi_id; create a new AOI id or explicitly migrate the AOI data."
            )
        return self.upload_json_if_changed(definition, key=key)

    def load_observations(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return canonical collector observations for this AOI and interval."""
        query: dict[str, Any] = {"aoi_id": self.settings.aoi_id}
        bounds = {operator: value for operator, value in (("$gte", start_date), ("$lte", end_date)) if value}
        if bounds:
            query["observation_date"] = bounds
        return [
            self._without_id(row)
            for row in self.database["observations"].find(query).sort([("tile_id", 1), ("observation_date", 1)])
        ]

    def load_tiles(self) -> list[dict[str, Any]]:
        """Return the collector's stable tile definitions for this AOI."""
        return [
            self._without_id(row)
            for row in self.database["tiles"].find({"aoi_id": self.settings.aoi_id}).sort([("tile_id", 1)])
        ]

    def upsert_observations(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        self._upsert_many("observations", records, ("aoi_id", "tile_id", "observation_date"), run_id=run_id)

    def upsert_tiles(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        payload = [{**record, "tile_id": str(record.get("name") or record.get("tile_id"))} for record in records]
        self._upsert_many("tiles", payload, ("aoi_id", "tile_id"), run_id=run_id)

    def upsert_features(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        self._upsert_many("preprocessed_features", records, ("aoi_id", "tile_id", "feature_date", "preprocessing_schema_version"), run_id=run_id)

    def upsert_forecasts(self, records: list[dict[str, Any]], *, run_id: str) -> None:
        self._upsert_many("forecasts", records, ("aoi_id", "forecast_run_id", "tile_id", "forecast_date", "step"), run_id=run_id)

    def record_run(self, payload: dict[str, Any], *, run_id: str) -> None:
        now = _utc_now()
        document = {**payload, "run_id": run_id, "aoi_id": self.settings.aoi_id, "updated_at": now}
        created_at = document.pop("created_at", now)
        if self.aoi_definition_hash:
            document["aoi_definition_hash"] = self.aoi_definition_hash
        self.database["pipeline_runs"].update_one(
            {"run_id": run_id},
            {"$set": document, "$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )

    def upload_json(self, value: Any, *, key: str) -> dict[str, Any]:
        encoded = json.dumps(value, indent=2, allow_nan=False).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        self.s3.put_object(Bucket=self.settings.minio_bucket, Key=key, Body=encoded, ContentType="application/json", Metadata={"sha256": checksum})
        return {"bucket": self.settings.minio_bucket, "key": key, "sha256": checksum, "content_type": "application/json"}

    def upload_json_file(self, path: Path, *, key: str) -> dict[str, Any]:
        return self.upload_json(json.loads(path.read_text(encoding="utf-8")), key=key)

    def upload_file(self, path: Path, *, key: str, content_type: str) -> dict[str, Any]:
        """Upload a non-JSON artifact such as GeoJSON or JSONL."""
        encoded = path.read_bytes()
        checksum = hashlib.sha256(encoded).hexdigest()
        self.s3.put_object(
            Bucket=self.settings.minio_bucket,
            Key=key,
            Body=encoded,
            ContentType=content_type,
            Metadata={"sha256": checksum},
        )
        return {
            "bucket": self.settings.minio_bucket,
            "key": key,
            "sha256": checksum,
            "content_type": content_type,
        }

    def upload_json_if_changed(self, value: Any, *, key: str) -> dict[str, Any]:
        """Upload a canonical JSON object only when its checksum changed."""
        encoded = json.dumps(value, indent=2, allow_nan=False).encode("utf-8")
        checksum = hashlib.sha256(encoded).hexdigest()
        if self._remote_checksum(key) == checksum:
            return {
                "bucket": self.settings.minio_bucket,
                "key": key,
                "sha256": checksum,
                "content_type": "application/json",
                "unchanged": True,
            }
        return self.upload_json(value, key=key)

    def upload_file_if_changed(self, path: Path, *, key: str, content_type: str) -> dict[str, Any]:
        encoded = path.read_bytes()
        checksum = hashlib.sha256(encoded).hexdigest()
        if self._remote_checksum(key) == checksum:
            return {
                "bucket": self.settings.minio_bucket,
                "key": key,
                "sha256": checksum,
                "content_type": content_type,
                "unchanged": True,
            }
        return self.upload_file(path, key=key, content_type=content_type)

    def download_json(self, *, key: str) -> Any | None:
        """Read an optional JSON object, returning None when it is absent."""
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

    def key(self, *, run_id: str, relative_path: str) -> str:
        """Backward-compatible alias for the new run-scoped key."""
        return self.run_key(run_id=run_id, relative_path=relative_path)

    def _remote_checksum(self, key: str) -> str | None:
        try:
            response = self.s3.head_object(Bucket=self.settings.minio_bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return (response.get("Metadata") or {}).get("sha256")

    def _upsert_many(self, collection: str, records: list[dict[str, Any]], fields: tuple[str, ...], *, run_id: str) -> None:
        now = _utc_now()
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
