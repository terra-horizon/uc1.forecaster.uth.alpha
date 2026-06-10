# TERRA Product Chain 1

This deployment is **Alpha 1 of TERRA Product Chain 1** for Use Case 1:
assessment of water contamination in coastal areas and in the water cycle.

It is a Dockerized batch pipeline that transforms Sentinel-2 and Sentinel-3
observations for an area of interest into prepared water-quality time series
and short-term forecasts.

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
* Export inference plans, CSV/JSON forecasts, and plots.

## Current Scope

This alpha release is a CLI-based Dockerized forecasting chain. It does not
expose an HTTP API, include OpenMeteo meteorological inputs, or implement the
complete Hydrological and Water-Quality Digital Twin.

## Main Entry Point

```bash
python forecast.py \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name sperchios_test_run \
  --output-root inference_results
```
