"""FastAPI application for scheduling UC1 storage-backed forecasts."""

from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

from botocore.exceptions import BotoCoreError, ClientError, EndpointConnectionError
from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pymongo.errors import PyMongoError
from starlette.concurrency import run_in_threadpool

from forecaster.api.jobs import ForecastJobStore, ForecastJobStoreError, JobPayloadConflictError
from forecaster.api.schemas import (
    ForecastJobRequest,
    ForecastJobResponse,
    LivenessResponse,
    ReadinessChecks,
    ReadinessResponse,
)
from forecaster.api.service import ForecastJobExecutor
from forecaster.storage import MongoMinioStore, StorageConfigurationError, StorageSettings


def _job_response(document: dict) -> ForecastJobResponse:
    return ForecastJobResponse(
        job_id=document["job_id"],
        run_job_id=document["run_job_id"],
        profile=document["profile"],
        aoi_id=document["aoi_id"],
        status=document["status"],
        submitted_at=document["submitted_at"],
        started_at=document.get("started_at"),
        completed_at=document.get("completed_at"),
        result=document.get("result"),
        error=document.get("error"),
    )


def get_job_store() -> ForecastJobStore:
    return ForecastJobStore.from_env()


def create_or_get_job(payload: dict) -> tuple[dict, bool]:
    store = get_job_store()
    try:
        return store.create_or_get(payload)
    finally:
        store.close()


def load_job(job_id: str) -> dict | None:
    store = get_job_store()
    try:
        return store.get(job_id)
    finally:
        store.close()


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("FORECAST_API_TOKEN")
    if not expected:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "api_configuration_error", "message": "Forecaster API authentication is not configured."})
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "invalid_api_key", "message": "A valid X-API-Key is required."})


def check_readiness() -> ReadinessResponse:
    try:
        settings = StorageSettings.from_env(aoi_id="forecast-api-health")
    except StorageConfigurationError:
        return ReadinessResponse(status="not_ready", checks=ReadinessChecks(configuration="unavailable", mongodb="skipped", minio="skipped"))

    store = MongoMinioStore(settings)
    mongo_status = "ok"
    minio_status = "ok"
    try:
        try:
            store.database.command("ping")
        except (PyMongoError, ValueError, OSError):
            mongo_status = "unavailable"
        try:
            store.s3.head_bucket(Bucket=settings.minio_bucket)
        except (BotoCoreError, ClientError, EndpointConnectionError, ValueError, OSError):
            minio_status = "unavailable"
    finally:
        store.close()
    ready = mongo_status == "ok" and minio_status == "ok"
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=ReadinessChecks(configuration="ok", mongodb=mongo_status, minio=minio_status))


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    executor: ForecastJobExecutor | None = None
    task: asyncio.Task | None = None
    try:
        executor = ForecastJobExecutor(get_job_store())
        task = asyncio.create_task(executor.run_forever())
    except StorageConfigurationError:
        # Liveness remains available; readiness reports the configuration issue.
        pass
    try:
        yield
    finally:
        if executor:
            executor.stop()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if executor:
            executor.store.close()


app = FastAPI(title="UC1 Forecaster API", version="1.0.0", lifespan=lifespan)


@app.get("/health/live", response_model=LivenessResponse)
async def live() -> LivenessResponse:
    return LivenessResponse()


@app.get("/health/ready", response_model=ReadinessResponse)
async def ready(response: Response) -> ReadinessResponse:
    result = await run_in_threadpool(check_readiness)
    if result.status == "not_ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result


@app.post("/api/forecast/jobs", response_model=ForecastJobResponse, status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
async def submit_job(request: ForecastJobRequest) -> ForecastJobResponse:
    try:
        document, _created = await run_in_threadpool(create_or_get_job, request.canonical_payload())
    except JobPayloadConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"code": "run_job_id_conflict", "message": str(exc), "run_job_id": request.run_job_id}) from exc
    except (ForecastJobStoreError, StorageConfigurationError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "forecast_job_storage_unavailable", "message": "Forecast job storage is unavailable."}) from exc
    return _job_response(document)


@app.get("/api/forecast/jobs/{job_id}", response_model=ForecastJobResponse, dependencies=[Depends(require_api_key)])
async def get_job(job_id: str) -> ForecastJobResponse:
    try:
        document = await run_in_threadpool(load_job, job_id)
    except (ForecastJobStoreError, StorageConfigurationError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"code": "forecast_job_storage_unavailable", "message": "Forecast job storage is unavailable."}) from exc
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "forecast_job_not_found", "message": "Forecast job was not found."})
    return _job_response(document)
