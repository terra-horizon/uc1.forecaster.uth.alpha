# TERRA UC1 - Water Contamination Assessment and Forecasting

This deployment is **Alpha 1 of TERRA Product Chain 1** for Use Case 1:
assessment of water contamination in coastal areas and in the water cycle.

It is a Dockerized, one-shot scheduled pipeline that transforms Sentinel-2 and
Sentinel-3 observations for an area of interest into prepared water-quality
time series and short-term forecasts. It stores queryable records in MongoDB
and durable JSON/GeoJSON/STAC artifacts in a configured MinIO bucket.

## Product Chain Components

* **Data Fusion and Preprocessing for Water Contamination:** implemented in
  Alpha 1.
* **ML Model Inference for Water Contamination Forecasting:** implemented in
  Alpha 1.
* **Hydrological and Water-Quality Digital Twin:** foundations only; the
  complete Digital Twin is planned for a future release.
* **Pipeline Orchestration and Result Delivery:** implemented as a
  cross-cutting deployment capability.

See [Product Chain 1](product-chain.md) for the scope and status of each
component.

## Capabilities

* Extract river and water tiles from an AOI bounding box.
* Select valid water tiles using NDWI-based checks.
* Collect Sentinel-2 and Sentinel-3 historical metrics and target-date images.
* Interpolate missing observations to a 5-day cadence.
* Run model inference for CDOM, Chl-a, Color, Cya, DOC, Turbidity, WQI, and surface temperature.
* Persist raw observations, model-ready features, forecasts, and run
  provenance through configurable MongoDB and MinIO endpoints.

## Current Scope

This alpha release is a CLI-based Dockerized forecasting chain. It does not
expose an HTTP API, include OpenMeteo meteorological inputs, or implement the
complete Hydrological and Water-Quality Digital Twin.

## Operational Entry Point

The deployment entrypoint is `forecaster.from_storage`. It reads the
collector-published AOI history and tiles, then runs model inference without
calling Sentinel collection APIs.

```bash
docker compose --env-file .env run --rm forecaster \
  --aoi-id sperchios \
  --run-name uc1-dev
```

For a local collector-to-forecaster handoff, add `--collection-run-dir` and
`--no-publish`. See
[Deployment](deployment.md), [Configuration](configuration.md), and the
[repository data contract](https://github.com/terra-horizon/uc1.forecaster.uth.alpha/blob/main/collector/DATA_CONTRACT.md).
