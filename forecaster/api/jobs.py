"""Durable MongoDB job records for the always-on forecaster API."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from forecaster.storage import StorageConfigurationError, StorageSettings


JOB_COLLECTION = "forecast_api_jobs"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ForecastJobStoreError(RuntimeError):
    """MongoDB job persistence could not be used."""


class JobPayloadConflictError(ValueError):
    """A caller reused an idempotency key with different parameters."""


class ForecastJobStore:
    def __init__(self, settings: StorageSettings):
        self.settings = settings
        self.client = MongoClient(
            settings.mongo_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
        self.collection = self.client[settings.mongo_database][JOB_COLLECTION]
        self._indexes_ready = False

    @classmethod
    def from_env(cls) -> "ForecastJobStore":
        # StorageSettings validates every dependency required by a real job,
        # while this fixed value prevents an ambient TERRA_AOI_ID from
        # affecting API-wide job persistence.
        return cls(StorageSettings.from_env(aoi_id="forecast-api"))

    def close(self) -> None:
        self.client.close()

    def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        try:
            self.collection.create_index([("run_job_id", ASCENDING)], unique=True, name="unique_run_job_id")
            self.collection.create_index([("status", ASCENDING), ("submitted_at", ASCENDING)], name="claimable_jobs")
            self.collection.create_index([("lease_expires_at", ASCENDING)], name="job_leases")
            self._indexes_ready = True
        except PyMongoError as exc:
            raise ForecastJobStoreError("Forecast job storage is unavailable.") from exc

    def create_or_get(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self.ensure_indexes()
        now = utc_now()
        value = fingerprint(payload)
        document = {
            "job_id": uuid.uuid4().hex,
            "run_job_id": payload["run_job_id"],
            "request": payload,
            "request_fingerprint": value,
            "profile": payload["profile"],
            "aoi_id": payload["aoi_id"],
            "status": "queued",
            "submitted_at": now,
            "updated_at": now,
            "started_at": None,
            "completed_at": None,
            "lease_expires_at": None,
            "attempt_count": 0,
            "result": None,
            "error": None,
        }
        try:
            self.collection.insert_one(document)
            return self._without_id(document), True
        except DuplicateKeyError:
            existing = self.collection.find_one({"run_job_id": payload["run_job_id"]})
            if not existing:
                raise ForecastJobStoreError("Forecast job storage is unavailable.")
            existing = self._without_id(existing)
            if existing["request_fingerprint"] != value:
                raise JobPayloadConflictError("run_job_id is already associated with a different forecast request.")
            return existing, False
        except PyMongoError as exc:
            raise ForecastJobStoreError("Forecast job storage is unavailable.") from exc

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            document = self.collection.find_one({"job_id": job_id})
            return self._without_id(document) if document else None
        except PyMongoError as exc:
            raise ForecastJobStoreError("Forecast job storage is unavailable.") from exc

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        self.ensure_indexes()
        now = utc_now()
        # Jobs left running by an interrupted container are retried only after
        # their lease expires. One worker claims globally in v1.
        try:
            self.collection.update_many(
                {"status": "running", "lease_expires_at": {"$lt": now}},
                {"$set": {"status": "queued", "lease_expires_at": None, "updated_at": now}},
            )
            lease = (datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)).isoformat(timespec="seconds").replace("+00:00", "Z")
            document = self.collection.find_one_and_update(
                {"status": "queued"},
                {
                    "$set": {
                        "status": "running",
                        "started_at": now,
                        "updated_at": now,
                        "lease_expires_at": lease,
                        "worker_id": worker_id,
                    },
                    "$inc": {"attempt_count": 1},
                },
                sort=[("submitted_at", ASCENDING)],
                return_document=ReturnDocument.AFTER,
            )
            return self._without_id(document) if document else None
        except PyMongoError as exc:
            raise ForecastJobStoreError("Forecast job storage is unavailable.") from exc

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        self._finish(job_id, status="succeeded", result=result, error=None)

    def fail(self, job_id: str, *, code: str, message: str) -> None:
        self._finish(job_id, status="failed", result=None, error={"code": code, "message": message})

    def _finish(self, job_id: str, *, status: str, result: dict[str, Any] | None, error: dict[str, str] | None) -> None:
        now = utc_now()
        try:
            self.collection.update_one(
                {"job_id": job_id, "status": "running"},
                {"$set": {"status": status, "result": result, "error": error, "completed_at": now, "updated_at": now, "lease_expires_at": None}},
            )
        except PyMongoError as exc:
            raise ForecastJobStoreError("Forecast job storage is unavailable.") from exc

    @staticmethod
    def _without_id(document: dict[str, Any] | None) -> dict[str, Any]:
        return {key: value for key, value in (document or {}).items() if key != "_id"}


def job_lease_seconds() -> int:
    value = os.getenv("FORECAST_JOB_LEASE_SECONDS", "86400")
    try:
        seconds = int(value)
    except ValueError as exc:
        raise StorageConfigurationError("FORECAST_JOB_LEASE_SECONDS must be a positive integer.") from exc
    if seconds <= 0:
        raise StorageConfigurationError("FORECAST_JOB_LEASE_SECONDS must be a positive integer.")
    return seconds
