# TERRA UC1 - Water Contamination Assessment and Forecasting

**Version**: Alpha 1

This is the Alpha 1 deployment of **Product Chain 1** for **Use Case 1** of the
**TERRA Horizon Project**: *Assessment of water contamination in coastal areas
and in the water cycle.*

Product Chain 1 currently implements:

- **Data Fusion and Preprocessing for Water Contamination**
- **ML Model Inference for Water Contamination Forecasting**
- **Pipeline Orchestration and Result Delivery**

It also provides initial geospatial and forecasting foundations for a future
**Hydrological and Water-Quality Digital Twin**. The complete Digital Twin,
including hydrological models, integrated observations and predictions, data
assimilation, and scenario simulation, is not yet implemented.

See the [Product Chain 1 component documentation](docs/docs/product-chain.md)
for the scope and status of each component.

> **Note:** This version does **not** contain meteorological data (e.g., OpenMeteo). Meteorological integration will be added in future versions.

---

## High-Level Architecture & Features

This alpha pipeline operates as a data-preparation and inference chain for
forecasting water-quality indicators.

### Features
We focus on key water quality indicators extracted primarily from satellite imagery (Sentinel-2 and Sentinel-3):
- **Sentinel-2**
    - **CDOM** (Colored Dissolved Organic Matter)
    - **Chl_a** (Chlorophyll-a)
    - **Color** 
    - **Cya** (Cyanobacteria)
    - **DOC** (Dissolved Organic Carbon)
    - **Turb** (Turbidity)
    - **WQI** (Water Quality Index)

- **Sentinel-3**    
    - **Surface Temperature**

### Model Architecture
The core engine is a **Global Multi-Feature Transfer Model**. At a high level, it relies on:
- **BiLSTM (Bidirectional LSTM) Networks**: To capture complex temporal dynamics in both forward and backward time directions.
- **Attention Mechanisms**: To focus the model on the most critical parts of the time series when making predictions.
- **Velocity & Horizon Scaling**: To dynamically scale forecasted changes over multiple steps into the future.
- **Spatial Context Awareness**: The model evaluates bounding box geometry and spatial metrics to generalize across different geographical areas.

---

## Pre-Processing Mechanisms

Before data reaches the model, it goes through several critical pre-processing stages to handle real-world challenges (like cloud cover and satellite revisit gaps):

1. **Water Tile Selection (NDWI)**: Dynamically extracts valid river and water body tiles by analyzing Normalized Difference Water Index (NDWI) distributions.
2. **Matern GPR (Gaussian Process Regression) Interpolation**: Fills gaps in the raw satellite data to produce a clean, continuous **5-day cadence** time series. It uses a Matern kernel to model the natural temporal smoothness of water quality metrics.
3. **Temporal Encoding**: Injects cyclical time features (Sine/Cosine of the Day of the Year and Month) so the model understands seasonal patterns.
4. **Robust Scaling**: Normalizes the features using robust statistical scalars (medians and quantiles) to prevent extreme outlier data from skewing the predictions.

---

## Execution Model

The collector and forecaster run as independent components:

```text
collector service  ->  MongoDB / MinIO or collection-run directory  ->  forecaster.from_storage
```

- The collector owns Sentinel discovery, tile extraction, and observation
  publication. It writes `collection_state`, `tiles`, `observations`, and its
  `pipeline_runs` lifecycle to MongoDB, with matching JSON/GeoJSON/run
  artifacts in MinIO.
- `forecaster.from_storage` is the operational forecaster. It reads the
  collector's AOI-scoped data and publishes `preprocessed_features`,
  `forecasts`, and its own `pipeline_runs` lifecycle without CDSE access.

The operational flow is:

1. **Area Definition**: You define an Area of Interest (AOI bounding box) and a target anchor date.
2. **Tile Extraction**: The pipeline automatically chops the AOI into river tiles.
3. **Validation**: It filters out tiles that lack sufficient water presence.
4. **Data Collection**: It downloads historical Sentinel-2 and Sentinel-3 data for the valid tiles.
5. **Augmentation**: Missing data gaps are interpolated (Matern GPR).
6. **Inference**: The pre-processed 5-day time series is passed to the Global BiLSTM model to forecast the future state of the water quality indicators.
7. **Export**: Predictions are saved as `.json` and `.csv` files, alongside visual plots showing history vs. forecast.

The collector and forecaster exchange a stable AOI, tile, and observation
contract. The forecaster selects tiles with enough usable collected history and
persists its own preprocessing and forecast artifacts to MongoDB and MinIO.

### Forecast from collector data

The normal deployed forecaster reads the collector-published shared storage:

```bash
python -m forecaster.from_storage \
  --aoi-id sperchios \
  --run-name "sperchios_forecast"
```

For explicit local/offline verification only:

```bash
python -m forecaster.from_storage \
  --aoi-id sperchios \
  --run-name "sperchios_test_run" \
  --collection-run-dir outputs/collector/sperchios_collection \
  --no-publish
```

Target-date imagery is requested for the exact anchor date only. If Sentinel-2 or Sentinel-3 imagery is not available on that date, the run records `status: unavailable` and `actual_date: "N/A"` in `inference_plan.json` instead of silently falling back to another date.

---

## Runtime Configuration

The forecaster reads CDSE credentials from environment variables. Do not commit credentials to this repository.

Required:

```text
CDSE_CLIENT_ID
CDSE_CLIENT_SECRET
```

Optional backup credentials are also supported:

```text
CDSE_BACKUP_CLIENT_ID
CDSE_BACKUP_CLIENT_SECRET
CDSE_BACKUP_2_CLIENT_ID
CDSE_BACKUP_2_CLIENT_SECRET
# Additional complete pairs may be supplied through CDSE_BACKUP_9_*
```

For local development, place credentials in a repository-root `.env` file. The file is ignored by Git and excluded from the Docker build context.

Copy the tracked [`.env.example`](.env.example) before editing it. It is the
only configuration template required by this repository: MongoDB and MinIO may
be remote, local, or supplied by another Docker deployment.

---

## Docker Usage

The Docker image runs `forecaster.from_storage` by default. One invocation
reads a published AOI dataset and then exits.

### Build and inspect the image

Build the local image:

```bash
docker build -t uc1-forecaster:local .
```

Show the operational CLI help:

```bash
docker compose run --rm forecaster --help
```

### Always-on forecaster API

`forecaster-api` is the permanent, storage-backed API service. It accepts a
forecast job, returns immediately, and runs the existing independent
forecaster in the background. The caller polls the job status instead of
holding an HTTP connection until model inference completes.

Set a private `FORECAST_API_TOKEN` in `.env`, then start the service locally:

```bash
docker compose --env-file .env up --build -d forecaster-api
curl http://127.0.0.1:18001/health/live
```

Submit a job with the configured key. The collector must already have
successfully published the requested AOI to MongoDB/MinIO.

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
    "history_start": "2016-01-01"
  }'
```

The response is `202 Accepted` with a `job_id`. Poll it until `status` is
`succeeded` or `failed`:

```bash
curl --header "X-API-Key: $FORECAST_API_TOKEN" \
  http://127.0.0.1:18001/api/forecast/jobs/<job_id>
```

`run_job_id` is the caller's idempotency key. Retrying the identical request
returns the same job; reusing it with different parameters returns `409`.
Only the server-approved `stored-forecast` profile is accepted. Paths,
collector-run directories, publishing controls, and arbitrary model arguments
are intentionally not exposed by the API.

The port is bound to loopback only. Reverse-proxy exposure is a separate
server deployment step and must retain `X-API-Key` authentication.

### Storage configuration

The container only needs application-level environment variables. It does not
contain infrastructure addresses, SSH keys, tunnels, root credentials, or
MongoDB/MinIO servers. The configured endpoints may be remote, locally hosted,
on another Docker network, or reached through a host-side SSH tunnel.

| Variable | Purpose |
| --- | --- |
| `MONGO_URI` | Complete MongoDB URI, including the database and authentication source. |
| `MINIO_ENDPOINT` | MinIO S3 API URL, for example `http://minio:9000`. |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | MinIO application credentials. |
| `MINIO_BUCKET_NAME` | Existing MinIO bucket used by the pipeline. |
| `MINIO_VERIFY_TLS` | Keep `true` by default; set `false` only for a trusted development endpoint with a self-signed certificate. |
| `MINIO_CA_BUNDLE` | Optional path to a trusted CA certificate inside the container. |
| `TERRA_AOI_ID` | Stable physical study-area identity; it is not an environment label. |

### Connect to storage services

Copy `.env.example` to `.env` and replace its placeholders. The app uses the
same configuration regardless of where MongoDB and MinIO are hosted.

Before running the forecaster, the operator must provision a MongoDB database
and application user, plus a MinIO bucket and application access key. The
pipeline creates its collections and indexes automatically, but never creates
infrastructure users, databases, buckets, or access keys.

Run a read-only preflight before processing data:

```bash
docker compose --env-file .env run --rm --entrypoint python forecaster scripts/storage_health.py
```

For a host-side SSH tunnel, start the tunnel on the host and set `MONGO_URI`
to a URI whose host is `host.docker.internal` (Docker Desktop) and whose port
is the tunnel's local port. MinIO does not require a tunnel unless your own
network policy requires one. The application never creates a tunnel itself.

If MongoDB or MinIO is unreachable, the preflight and pipeline exit with a
credential-safe error naming the failed target and the relevant configuration
variables. Credentials are never included in those messages.

The image runs as a non-root user and uses `forecaster.from_storage` as
its entrypoint. It writes queryable records to MongoDB and JSON/GeoJSON/STAC
artifacts to MinIO.

### Orchestrating the Decoupled Pipeline (Collector & Forecaster)

The pipeline is intentionally decoupled into two separate tasks: Data Collection and Forecasting. An external orchestrator (like cron or Airflow) should run these sequentially. 

**Step 1: Data Collection (from the `collector/` directory)**
First, the orchestrator triggers the collector to backfill or fetch new daily data. It passes the bounding box and stable AOI ID:

```bash
cd collector
docker-compose run --rm collector run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto
```

**Step 2: Forecasting (from the repository root)**
Once collection succeeds, the orchestrator runs the independent forecaster. The forecaster strictly acts as a consumer: it does **not** attempt to collect data, and it automatically looks up the bounding box from the database using the `--aoi-id`.

```bash
cd ..
docker-compose --env-file .env run --rm --entrypoint "python -m forecaster.from_storage" forecaster \
  --aoi-id sperchios \
  --run-name sperchios-forecast \
  --history-start 2016-01-01
```

If the database is missing data or the collector failed, the forecaster will exit cleanly with an error (e.g., `ValueError: AOI has observations, but no tile has the 24 usable input records required by the model.`) so the orchestrator can handle the failure. 

**Simulating Past Dates**
By default, the forecaster anchors its predictions to the absolute newest date it finds in the database. To force the forecaster to simulate a past date as "today" (ignoring any collected data newer than that date), pass the `--as-of-date` flag:

```bash
docker-compose --env-file .env run --rm --entrypoint "python -m forecaster.from_storage" forecaster \
  --aoi-id sperchios \
  --run-name sperchios-forecast \
  --as-of-date 2026-07-15
```

### Forecaster CLI Parameters

The independent forecaster (`forecaster.from_storage`) accepts the following parameters:

| Parameter | Description |
| --- | --- |
| `--aoi-id ID` | Required. Stable AOI ID used by the collector when publishing data. Used to retrieve the bounding box and dataset from the DB. |
| `--run-name NAME` | Required. Name for this forecast-only run (e.g. `sperchios-forecast`). |
| `--output-root PATH` | Local staging root mounted into the container (defaults to `outputs/forecasts`). |
| `--history-start YYYY-MM-DD` | Optional. Earliest ISO observation date to retrieve from the database. |
| `--as-of-date YYYY-MM-DD` | Optional. Force the model to simulate a past date as "today" by only using observations on or before this date. |
| `--collection-run-dir PATH` | Optional. Read a completed standalone collector run directly from local files instead of shared storage. |
| `--no-publish` | Optional. Keep forecast outputs local and do not require storage credentials (useful with `--collection-run-dir`). |
| `--plot` | Optional. Generate visualization plots of the forecast trajectories. |
| `--stac-base-url URL` | Optional. Base URL used in exported STAC links. |

### Storage layout

Stable AOI assets are stored once under `aoi/`. Canonical observations,
preprocessed features, and forecasts are stored under their AOI-level data
prefixes. Execution results and logs are stored under `runs/<run-id>/`.

```text
terra-uc1/<aoi-id>/
├── aoi/                 # tiles, GeoJSON, STAC geometry, collection state
├── observations/        # raw observation JSON keyed by date
├── preprocessed/        # model-ready feature JSON
├── forecasts/           # forecast JSON keyed by forecast run
└── runs/<run-id>/       # result, state, logs, and run provenance
```

MongoDB stores queryable records with unique AOI/tile/date indexes. Each
record stores an artifact reference containing the MinIO bucket, object key,
and SHA-256 checksum. The complete contract is documented in
[`collector/DATA_CONTRACT.md`](collector/DATA_CONTRACT.md).

Inspect objects through your MinIO console or any S3-compatible client using
the endpoint and application credentials configured in `.env`.

---

## Container Publishing

The repository includes `.github/workflows/docker-publish.yml`, following the TERRA GHCR publishing pattern.

The workflow runs when a tag matching `v*` is pushed:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The image is published as:

```text
ghcr.io/terra-horizon/uc1.forecaster.uth.alpha:<tag>
```

---

## Vulnerability Scanning

The reusable vulnerability scan pattern was verified in `terra-aai` and `terra-app-api`; it is not part of `terra-logging`.

This repository includes `.github/workflows/vulnerability-scan-on-demand.yml`. Run it manually from GitHub Actions using an image tag such as `v1.0.0`.

The workflow first tries to scan the published image:

```text
ghcr.io/terra-horizon/uc1.forecaster.uth.alpha:<image_tag>
```

If that tag has not been published to GHCR yet, the workflow builds the image from the current checkout and scans the local workflow image instead.

The workflow scans:

- the repository Docker configuration with Trivy config scanning;
- the published GHCR image with Trivy image scanning for `CRITICAL` and `HIGH` operating system and library vulnerabilities.

Results are always uploaded as workflow artifacts. They are also uploaded to GitHub Code Scanning when repository security settings allow it. Private repositories may require GitHub Advanced Security for Code Scanning ingestion.

For local scan runs, store generated reports under `local_scans/`. That directory is ignored by Git so local SARIF/table outputs do not get committed.

---

## Documentation

UC1 component documentation lives under `docs/` and is configured with MkDocs, Mike, and the Material theme.

Run a local docs preview from the repository root:

```bash
pip install mkdocs mkdocs-material mike neoteroi-mkdocs pymdown-extensions
mkdocs serve -f docs/mkdocs.yml
```

The on-demand documentation deployment workflow publishes to:

```text
https://terra-horizon.github.io/uc1.forecaster.uth.alpha/
```

This setup keeps documentation changes inside this UC1 repository. The central `terra-horizon.github.io` portal is not modified by this repository.

---

## License

This project is licensed under the [Apache License 2.0](LICENSE).
