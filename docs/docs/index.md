# TERRA UC1 Forecaster

The UC1 Forecaster is the Alpha 1 inference engine for TERRA Use Case 1: assessment of water contamination in coastal areas and in the water cycle.

The service forecasts water quality indicators for an area of interest using Sentinel-2 and Sentinel-3 products, water tile selection, gap interpolation, and a bundled global multi-feature BiLSTM model.

## Capabilities

* Extract river and water tiles from an AOI bounding box.
* Select valid water tiles using NDWI-based checks.
* Collect Sentinel-2 and Sentinel-3 historical metrics and target-date images.
* Interpolate missing observations to a 5-day cadence.
* Run model inference for CDOM, Chl-a, Color, Cya, DOC, Turbidity, WQI, and surface temperature.
* Export inference plans, CSV/JSON forecasts, and plots.

## Current Scope

This release is a CLI-based Dockerized inference component. It does not expose an HTTP API and does not include OpenMeteo meteorological inputs.

## Main Entry Point

```bash
python forecast.py \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name sperchios_test_run \
  --output-root inference_results
```
