# TERRA Horizon Project - Use Case 1 Forecaster

**Version**: Alpha 1

This is the Alpha 1 Version of the forecaster for **Use Case 1** of the **TERRA Horizon Project**: *Assessment of water contamination in coastal areas and in the water cycle.*

> **Note:** This version does **not** contain meteorological data (e.g., OpenMeteo). Meteorological integration will be added in future versions.

---

## High-Level Architecture & Features

This pipeline operates purely as an inference engine to forecast water quality indicators.

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

## Pipeline Flow

The entire inference process is orchestrated via the central `forecast.py` script. The flow is as follows:

1. **Area Definition**: You define an Area of Interest (AOI bounding box) and a target anchor date.
2. **Tile Extraction**: The pipeline automatically chops the AOI into river tiles.
3. **Validation**: It filters out tiles that lack sufficient water presence.
4. **Data Collection**: It downloads historical Sentinel-2 and Sentinel-3 data for the valid tiles.
5. **Augmentation**: Missing data gaps are interpolated (Matern GPR).
6. **Inference**: The pre-processed 5-day time series is passed to the Global BiLSTM model to forecast the future state of the water quality indicators.
7. **Export**: Predictions are saved as `.json` and `.csv` files, alongside visual plots showing history vs. forecast.

### Example Inference Run

```bash
python forecast.py \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name "sperchios_test_run" \
  --output-root "inference_results"
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
```

For local development, place credentials in a repository-root `.env` file. The file is ignored by Git and excluded from the Docker build context.

---

## Docker Usage

Build the local image:

```bash
docker build -t uc1-forecaster:local .
```

Show the CLI help:

```bash
./scripts/docker-run.sh --help
```

Run inference with credentials from `.env` and write outputs to a mounted host directory:

```bash
./scripts/docker-run.sh \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name sperchios_test_run \
  --output-root /app/inference_results
```

The image runs as a non-root user and uses `forecast.py` as its entrypoint.

The helper script assigns a container name automatically with the format `uc1-forecaster-YYYYMMDD-HHMMSS`, mounts `inference_results/` to `/app/inference_results`, and passes `.env` when the file exists. By default, the stopped container remains visible in Docker Desktop with that generated name. Set `UC1_REMOVE_CONTAINER=1` when you want Docker to remove it automatically after the run.

Override the defaults when needed:

```bash
UC1_CONTAINER_NAME=uc1-forecaster-20260602 UC1_IMAGE_NAME=uc1-forecaster:local ./scripts/docker-run.sh --help
```

Run with automatic cleanup:

```bash
UC1_REMOVE_CONTAINER=1 ./scripts/docker-run.sh --help
```

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
ghcr.io/terra-horizon/uc1.forecaster.uth.alpha1:<tag>
```

---

## Vulnerability Scanning

The reusable vulnerability scan pattern was verified in `terra-aai` and `terra-app-api`; it is not part of `terra-logging`.

This repository includes `.github/workflows/vulnerability-scan-on-demand.yml`. Run it manually from GitHub Actions using an image tag such as `v1.0.0`.

The workflow first tries to scan the published image:

```text
ghcr.io/terra-horizon/uc1.forecaster.uth.alpha1:<image_tag>
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
https://terra-horizon.github.io/uc1.forecaster.uth.alpha1/
```

This setup keeps documentation changes inside this UC1 repository. The central `terra-horizon.github.io` portal is not modified by this repository.
