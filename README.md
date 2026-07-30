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
  publication.
- `forecaster.from_storage` is the operational forecaster. It reads the
  collector's AOI-scoped data and produces forecasts without CDSE access.

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
persists its own forecast artifacts to MongoDB and MinIO.

### Forecast from collector data

This is optional and does not replace the scheduled pipeline in deployment:

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

### Run the storage-backed forecaster in Docker

Pass the AOI published by the collector:

```bash
docker compose --env-file .env run --rm forecaster \
  --aoi-id sperchios \
  --run-name sperchios-forecast
```

The forecaster reads collected data from the configured UTH MongoDB/MinIO
storage and persists forecast results there.

### Backfill and incremental runs

The scheduled CLI defaults to `--history-start 2016-01-01` and uses today as
the target date when `--target-date` is omitted. A full historical backfill
requires `--backfill-all`, which processes the interval in discovery chunks:

```bash
docker compose --env-file .env run --rm forecaster \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name uc1-dev \
  --history-start 2016-01-01 \
  --backfill-all
```

For another deployment, use the same image and its own `.env`, with a stable
run label such as `uc1-prod`.

### Daily scheduling

The container is intentionally one-shot. Schedule the same Compose command
externally, for example with cron, after the historical backfill has completed:

```cron
15 2 * * * cd /path/to/uc1 && docker compose --env-file /secure/path/uc1.env run --rm forecaster --bbox 22.433493 38.837552 22.569555 38.894223 --run-name uc1-prod --output-root /app/data/inference_runs >> /var/log/uc1-forecast.log 2>&1
```

Each scheduled invocation restores the AOI state, checks MongoDB for existing
tile/date records, collects only missing or retryable units, and updates
MongoDB and MinIO idempotently.

After the backfill, a normal invocation performs an incremental update. It
checks existing MongoDB observations and collects only missing or retryable
tile/date units:

```bash
docker compose --env-file .env run --rm forecaster \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name uc1-dev
```

Use a bounded commissioning run:

```bash
docker compose --env-file .env run --rm forecaster \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name uc1-dev \
  --history-start 2026-06-05 \
  --target-date 2026-06-30 \
  --max-days-per-run 1 \
  --max-tiles-per-run 1 \
  --skip-inference
```

### Scheduled CLI parameters

| Parameter | Description |
| --- | --- |
| `--bbox MIN_LON MIN_LAT MAX_LON MAX_LAT` | Required AOI bounding box. |
| `--run-name NAME` | Required stable run label, for example `uc1-dev` or `uc1-prod`. |
| `--output-root PATH` | Local staging root mounted into the container. |
| `--history-start YYYY-MM-DD` | Start date for discovery; defaults to `2016-01-01`. |
| `--target-date YYYY-MM-DD` | End/target date; defaults to today. |
| `--backfill-all` | Process all discovery windows from the history start to the target date. |
| `--discovery-chunk-days N` | Size of each discovery window; defaults to `31`. |
| `--max-days-per-run N` | Limit missing dates processed in one invocation. |
| `--max-tiles-per-run N` | Limit tiles processed in one invocation. |
| `--dry-run` | Discover and report missing dates without collecting or writing data products. |
| `--skip-inference` | Collect/update data without running the forecast model. |
| `--stac-base-url URL` | Optional base URL used in exported STAC links. |

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
