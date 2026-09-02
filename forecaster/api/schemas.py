"""Public request and response types for the forecaster API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Annotated, Literal

from pydantic import BaseModel, Field, field_validator


ForecastProfile = Literal["stored-forecast"]
JobStatus = Literal["queued", "running", "succeeded", "failed"]


class ForecastJobRequest(BaseModel):
    run_job_id: Annotated[str, Field(min_length=1, max_length=200)]
    triggered_at: datetime
    profile: ForecastProfile
    aoi_id: Annotated[str, Field(min_length=1, max_length=200)]
    run_name: Annotated[str, Field(min_length=1, max_length=200)]
    history_start: date | None = None
    as_of_date: date | None = None

    @field_validator("triggered_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("triggered_at must include a timezone offset")
        return value

    def canonical_payload(self) -> dict[str, Any]:
        """A deterministic JSON-safe request used for idempotency checks."""
        return self.model_dump(mode="json")


class JobError(BaseModel):
    code: str
    message: str


class ForecastJobResponse(BaseModel):
    job_id: str
    run_job_id: str
    profile: ForecastProfile
    aoi_id: str
    status: JobStatus
    submitted_at: str
    started_at: str | None = None
    completed_at: str | None = None
    result: dict[str, Any] | None = None
    error: JobError | None = None


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadinessChecks(BaseModel):
    configuration: Literal["ok", "unavailable"]
    mongodb: Literal["ok", "unavailable", "skipped"]
    minio: Literal["ok", "unavailable", "skipped"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks
