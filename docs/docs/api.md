# Forecaster API reference

The UC1 Forecaster API accepts asynchronous, storage-backed forecast jobs.
It is designed for an orchestrator or another application to submit a job,
then poll its durable status without holding an HTTP connection open during
model inference.

The API is packaged in the `forecaster-api` Docker service and can be deployed
on any infrastructure that provides the required MongoDB and S3-compatible
object storage configuration.

## Base URL and interactive documentation

The examples below use a direct local deployment:

```text
http://127.0.0.1:18001
```

When the service is behind a reverse proxy, prepend the proxy path to every
endpoint. For example, a proxy may expose the API as
`https://example.org/forecaster/api/forecast/jobs`.

FastAPI also provides interactive OpenAPI documentation:

```text
GET /docs
GET /openapi.json
```

These paths work directly at the API root. If a reverse proxy exposes the API
under a path prefix, configure the proxy and application root path accordingly
for the interactive documentation, or expose the API on a dedicated hostname.

## Authentication

Health endpoints are public. Job endpoints require the HTTP header:

```http
X-API-Key: <FORECAST_API_TOKEN>
```

Set `FORECAST_API_TOKEN` to a high-entropy secret in the runtime environment.
Do not put it in source control, a request URL, or an application log. A
reverse proxy must forward `X-API-Key` to the API service and should terminate
TLS before traffic leaves the trusted network boundary.

## Health endpoints

### `GET /health/live`

Returns `200 OK` when the HTTP process is running. It does not test MongoDB or
object storage.

```json
{"status":"ok"}
```

### `GET /health/ready`

Checks API configuration, MongoDB connectivity, and object-storage bucket
access. It returns `200 OK` only when all checks pass, otherwise `503 Service
Unavailable`.

```json
{
  "status": "ready",
  "checks": {
    "configuration": "ok",
    "mongodb": "ok",
    "minio": "ok"
  }
}
```

## Submit a forecast job

### `POST /api/forecast/jobs`

Creates a forecast job and returns immediately with `202 Accepted`. The
background worker reads the requested AOI's published collector data from
MongoDB and object storage, executes the stored forecast pipeline, and records
the result.

```bash
curl --request POST http://127.0.0.1:18001/api/forecast/jobs \
  --header "Content-Type: application/json" \
  --header "X-API-Key: $FORECAST_API_TOKEN" \
  --data '{
    "run_job_id": "forecast-sperchios-20260902-001",
    "triggered_at": "2026-09-02T10:30:00+03:00",
    "profile": "stored-forecast",
    "aoi_id": "sperchios",
    "run_name": "sperchios-forecast",
    "history_start": "2016-01-01",
    "as_of_date": "2026-08-25"
  }'
```

#### Request fields

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `run_job_id` | string, 1–200 chars | Yes | Caller-generated idempotency key. Generate a new value for each logically new forecast request. |
| `triggered_at` | ISO 8601 datetime with UTC offset | Yes | Time at which the caller requested the job, for traceability. |
| `profile` | `stored-forecast` | Yes | The only supported server-approved execution profile. |
| `aoi_id` | string, 1–200 chars | Yes | Stable identifier of an AOI already published by the collector. |
| `run_name` | string, 1–200 chars | Yes | Human-readable name for this forecast invocation. |
| `history_start` | `YYYY-MM-DD` | No | Earliest observation date to use. Omit to use the pipeline default. |
| `as_of_date` | `YYYY-MM-DD` | No | Latest observation date to use. Omit to use the latest available usable observation. |

#### Idempotency

`run_job_id` is globally unique within the API job store:

- Submitting an identical payload with the same `run_job_id` returns the same
  job record; it does not start a duplicate inference.
- Reusing `run_job_id` with any different payload returns `409 Conflict`.
- A failed job is retained for auditability. Use a new `run_job_id` for a new
  attempt after correcting the underlying cause.

#### Accepted response

```json
{
  "job_id": "316dd1e32bd64ce58766678c7043ca23",
  "run_job_id": "forecast-sperchios-20260902-001",
  "profile": "stored-forecast",
  "aoi_id": "sperchios",
  "status": "queued",
  "submitted_at": "2026-09-02T07:30:00Z",
  "started_at": null,
  "completed_at": null,
  "result": null,
  "error": null
}
```

## Read job status

### `GET /api/forecast/jobs/{job_id}`

Poll this endpoint with the `job_id` returned by the submission response.

```bash
curl --header "X-API-Key: $FORECAST_API_TOKEN" \
  http://127.0.0.1:18001/api/forecast/jobs/316dd1e32bd64ce58766678c7043ca23
```

The `status` field is one of:

| Status | Meaning |
| --- | --- |
| `queued` | Stored and waiting for the worker. |
| `running` | Claimed by the worker and executing. |
| `succeeded` | Forecast artifacts and result records were persisted. |
| `failed` | The job ended with an error; inspect the `error` object. |

### Successful job response

On success, `result` includes execution metadata such as `forecast_run_id`,
`forecast_anchor`, `forecast_row_count`, `forecast_tiles`, and API provenance.

```json
{
  "job_id": "316dd1e32bd64ce58766678c7043ca23",
  "status": "succeeded",
  "completed_at": "2026-09-02T07:30:27Z",
  "result": {
    "status": "success",
    "component": "forecaster",
    "aoi_id": "sperchios",
    "forecast_run_id": "forecast_2026-08-25",
    "forecast_anchor": "2026-08-25",
    "forecast_row_count": 261,
    "source": "shared_storage"
  },
  "error": null
}
```

### Failed job response

A job that begins execution but cannot complete still returns `200 OK` from
the status endpoint. Its `status` is `failed` and its `error` object identifies
the failure class without exposing credentials.

```json
{
  "job_id": "example-job-id",
  "status": "failed",
  "result": null,
  "error": {
    "code": "forecast_execution_error",
    "message": "Forecast execution failed."
  }
}
```

## HTTP errors

| HTTP status | Error code | Meaning | Caller action |
| --- | --- | --- | --- |
| `401` | `invalid_api_key` | The API key is missing or invalid. | Supply the configured `X-API-Key`; do not retry indefinitely. |
| `404` | `forecast_job_not_found` | No job exists for the supplied `job_id`. | Check that the ID was copied from the submission response. |
| `409` | `run_job_id_conflict` | An idempotency key was reused with a different request payload. | Keep the original payload or generate a new `run_job_id`. |
| `422` | FastAPI validation response | A request field is missing or invalid, for example a timezone-free `triggered_at`. | Correct the request and submit it with a new idempotency key. |
| `503` | `api_configuration_error` | The server has no configured API token. | Operator action required; do not retry. |
| `503` | `forecast_job_storage_unavailable` | The durable MongoDB job store is unavailable. | Retry with backoff after storage recovers. |

Job-level failures are returned in the status response rather than as an HTTP
failure. Current job error codes include:

| Job error code | Meaning |
| --- | --- |
| `forecast_configuration_error` | Required storage or runtime configuration is invalid. |
| `forecast_unavailable` | MongoDB or object storage could not be reached. |
| `forecast_input_error` | The AOI data does not satisfy the forecast pipeline's input requirements. |
| `forecast_execution_error` | Inference or post-processing failed unexpectedly. |

## Persistence and execution model

The API stores durable job records in MongoDB. Completed forecast records are
also upserted in MongoDB and the forecast JSON artifact is published to the
configured S3-compatible object store. Local container storage is only a
working area; it is not the system of record.

Version 1 runs one worker per API service. A claimed job has a lease controlled
by `FORECAST_JOB_LEASE_SECONDS` (default: `86400`). If the worker is interrupted
before completion, an expired lease makes the job eligible for a later retry.

## Docker configuration

The API requires the standard storage variables plus its API token:

| Variable | Required | Purpose |
| --- | --- | --- |
| `FORECAST_API_TOKEN` | Yes for job endpoints | Shared secret used by `X-API-Key`. |
| `FORECAST_JOB_LEASE_SECONDS` | No | Positive worker lease duration in seconds; defaults to `86400`. |
| `MONGO_URI` | Yes | Application MongoDB URI including database and authentication source. |
| `MINIO_ENDPOINT` | Yes | S3-compatible object-storage endpoint. |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | Yes | Application object-storage credentials. |
| `MINIO_BUCKET_NAME` | Yes | Existing bucket containing pipeline artifacts. |
| `MINIO_VERIFY_TLS` | No | TLS verification setting; defaults to `true`. |
| `MINIO_CA_BUNDLE` | No | Optional CA bundle path inside the container. |
| `TERRA_AOI_ID` | No for API jobs | Optional default AOI identity used by the CLI pipeline. Each API job supplies its own required `aoi_id`. |

Start locally after creating a private `.env` file:

```bash
docker compose --env-file .env up --build -d forecaster-api
docker compose --env-file .env ps
curl --fail http://127.0.0.1:18001/health/ready
```

The Compose configuration binds the API to loopback by default. Choose an
appropriate authenticated TLS reverse proxy when exposing it outside the host.
