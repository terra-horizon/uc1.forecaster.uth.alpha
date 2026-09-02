"""Background execution for durable forecast API jobs."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from pathlib import Path
from typing import Any

from forecaster.api.jobs import ForecastJobStore, ForecastJobStoreError, job_lease_seconds
from forecaster.from_storage import StoredForecastConfig, StoredForecastPipeline
from forecaster.storage import StorageConfigurationError, StorageConnectionError


class ForecastJobExecutor:
    def __init__(self, store: ForecastJobStore):
        self.store = store
        self.worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                claimed = await asyncio.to_thread(self.run_once)
            except (ForecastJobStoreError, StorageConfigurationError):
                claimed = False
            await asyncio.sleep(0.2 if claimed else 1.0)

    def run_once(self) -> bool:
        job = self.store.claim_next(worker_id=self.worker_id, lease_seconds=job_lease_seconds())
        if not job:
            return False
        try:
            result = self._run_pipeline(job)
        except StorageConfigurationError:
            self.store.fail(job["job_id"], code="forecast_configuration_error", message="Forecast storage configuration is invalid.")
        except StorageConnectionError:
            self.store.fail(job["job_id"], code="forecast_unavailable", message="Forecast storage is unavailable.")
        except ValueError as exc:
            self.store.fail(job["job_id"], code="forecast_input_error", message=str(exc)[:1000])
        except Exception:
            self.store.fail(job["job_id"], code="forecast_execution_error", message="Forecast execution failed.")
        else:
            self.store.complete(job["job_id"], result)
        return True

    @staticmethod
    def _run_pipeline(job: dict[str, Any]) -> dict[str, Any]:
        request = job["request"]
        return StoredForecastPipeline(StoredForecastConfig(
            aoi_id=request["aoi_id"],
            run_name=request["run_name"],
            output_root=Path("outputs") / "forecast_api" / job["job_id"],
            history_start=request.get("history_start"),
            as_of_date=request.get("as_of_date"),
            publish=True,
            api_job_id=job["job_id"],
            api_run_job_id=job["run_job_id"],
        )).execute()
