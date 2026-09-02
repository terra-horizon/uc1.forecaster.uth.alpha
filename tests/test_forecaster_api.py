from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from forecaster.api.main import app, check_readiness
from forecaster.api.schemas import ReadinessResponse
from forecaster.api.service import ForecastJobExecutor


VALID_REQUEST = {
    "run_job_id": "forecast-sperchios-001",
    "triggered_at": "2026-09-02T10:30:00+03:00",
    "profile": "stored-forecast",
    "aoi_id": "sperchios",
    "run_name": "sperchios-forecast",
    "history_start": "2016-01-01",
    "as_of_date": "2026-08-25",
}


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FakeJobStore:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.claimed = False

    def close(self) -> None:
        return None

    def create_or_get(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        existing = next((value for value in self.documents.values() if value["run_job_id"] == payload["run_job_id"]), None)
        if existing:
            from forecaster.api.jobs import JobPayloadConflictError

            if existing["request_fingerprint"] != _fingerprint(payload):
                raise JobPayloadConflictError("run_job_id is already associated with a different forecast request.")
            return existing, False
        job_id = f"job-{len(self.documents) + 1}"
        document = {
            "job_id": job_id,
            "run_job_id": payload["run_job_id"],
            "request": payload,
            "request_fingerprint": _fingerprint(payload),
            "profile": payload["profile"],
            "aoi_id": payload["aoi_id"],
            "status": "queued",
            "submitted_at": "2026-09-02T08:00:00Z",
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
        }
        self.documents[job_id] = document
        return document, True

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.documents.get(job_id)

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        if self.claimed:
            return None
        document = next((value for value in self.documents.values() if value["status"] == "queued"), None)
        if not document:
            return None
        self.claimed = True
        document["status"] = "running"
        document["started_at"] = "2026-09-02T08:00:01Z"
        return document

    def complete(self, job_id: str, result: dict[str, Any]) -> None:
        self.documents[job_id].update(status="succeeded", completed_at="2026-09-02T08:00:02Z", result=result)

    def fail(self, job_id: str, *, code: str, message: str) -> None:
        self.documents[job_id].update(status="failed", completed_at="2026-09-02T08:00:02Z", error={"code": code, "message": message})


async def _client_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def test_submit_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORECAST_API_TOKEN", "test-token")
    response = asyncio.run(_client_request("POST", "/api/forecast/jobs", json=VALID_REQUEST))
    assert response.status_code == 401


def test_submit_is_idempotent_and_status_is_pollable(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeJobStore()
    monkeypatch.setenv("FORECAST_API_TOKEN", "test-token")
    monkeypatch.setattr("forecaster.api.main.get_job_store", lambda: store)
    headers = {"X-API-Key": "test-token"}

    first = asyncio.run(_client_request("POST", "/api/forecast/jobs", json=VALID_REQUEST, headers=headers))
    second = asyncio.run(_client_request("POST", "/api/forecast/jobs", json=VALID_REQUEST, headers=headers))
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert first.json()["status"] == "queued"

    status_response = asyncio.run(_client_request("GET", f"/api/forecast/jobs/{first.json()['job_id']}", headers=headers))
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"


def test_reused_run_job_id_with_changed_payload_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeJobStore()
    monkeypatch.setenv("FORECAST_API_TOKEN", "test-token")
    monkeypatch.setattr("forecaster.api.main.get_job_store", lambda: store)
    headers = {"X-API-Key": "test-token"}
    asyncio.run(_client_request("POST", "/api/forecast/jobs", json=VALID_REQUEST, headers=headers))

    response = asyncio.run(_client_request("POST", "/api/forecast/jobs", json={**VALID_REQUEST, "as_of_date": "2026-08-24"}, headers=headers))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "run_job_id_conflict"


def test_request_validation_rejects_unknown_profile_and_naive_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORECAST_API_TOKEN", "test-token")
    headers = {"X-API-Key": "test-token"}
    unknown_profile = asyncio.run(_client_request("POST", "/api/forecast/jobs", json={**VALID_REQUEST, "profile": "anything"}, headers=headers))
    naive_time = asyncio.run(_client_request("POST", "/api/forecast/jobs", json={**VALID_REQUEST, "triggered_at": "2026-09-02T10:30:00"}, headers=headers))
    assert unknown_profile.status_code == 422
    assert naive_time.status_code == 422


def test_executor_runs_claimed_job_and_persists_result(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeJobStore()
    document, _ = store.create_or_get(VALID_REQUEST)
    monkeypatch.setenv("FORECAST_JOB_LEASE_SECONDS", "60")
    class FakePipeline:
        def __init__(self, _config: Any) -> None:
            pass

        def execute(self) -> dict[str, Any]:
            return {"status": "success", "forecast_run_id": "forecast_2026-08-25", "api_job_id": document["job_id"]}

    monkeypatch.setattr("forecaster.api.service.StoredForecastPipeline", FakePipeline)

    assert ForecastJobExecutor(store).run_once() is True
    completed = store.get(document["job_id"])
    assert completed and completed["status"] == "succeeded"
    assert completed["result"]["forecast_run_id"] == "forecast_2026-08-25"
    assert ForecastJobExecutor(store).run_once() is False


def test_liveness_does_not_require_storage() -> None:
    response = asyncio.run(_client_request("GET", "/health/live"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readiness_reports_missing_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    from forecaster.storage import StorageConfigurationError

    monkeypatch.setattr(
        "forecaster.api.main.StorageSettings.from_env",
        lambda **_kwargs: (_ for _ in ()).throw(StorageConfigurationError("missing")),
    )
    result = check_readiness()
    assert result.status == "not_ready"
    assert result.checks.configuration == "unavailable"
    assert result.checks.mongodb == "skipped"


def test_readiness_reports_dependency_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeDatabase:
        def command(self, _command: str) -> None:
            raise OSError("MongoDB unavailable")

    class FakeS3:
        def head_bucket(self, **_kwargs: Any) -> None:
            raise OSError("MinIO unavailable")

    class FakeStore:
        database = FakeDatabase()
        s3 = FakeS3()

        def __init__(self, _settings: Any) -> None:
            pass

        def close(self) -> None:
            return None

    class FakeSettings:
        minio_bucket = "terra-uc1"

    monkeypatch.setattr("forecaster.api.main.StorageSettings.from_env", lambda **_kwargs: FakeSettings())
    monkeypatch.setattr("forecaster.api.main.MongoMinioStore", FakeStore)
    result = check_readiness()
    assert result.status == "not_ready"
    assert result.checks.configuration == "ok"
    assert result.checks.mongodb == "unavailable"
    assert result.checks.minio == "unavailable"
