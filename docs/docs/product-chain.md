# Product Chain 1

## Product Summary

| Field | Description |
| --- | --- |
| **Name** | TERRA Product Chain 1 - Water Contamination Assessment and Forecasting |
| **Release** | Alpha 1 |
| **Description** | An alpha deployment that transforms satellite observations for an area of interest into prepared water-quality time series and short-term water-contamination forecasts. It also provides the first geospatial and forecasting foundations for a future Hydrological and Water-Quality Digital Twin. |
| **Components** | Data Fusion and Preprocessing; ML Model Inference; Hydrological and Water-Quality Digital Twin foundations; Pipeline Orchestration and Result Delivery. |

## Alpha Scope

Alpha 1 is a Dockerized, CLI-based batch pipeline. It retrieves Sentinel-2 and
Sentinel-3 observations through the Copernicus Data Space Ecosystem (CDSE),
selects relevant water tiles, prepares continuous time series, runs the bundled
forecasting model, and exports machine-readable results and plots.

The alpha deployment is a working forecasting chain, but it is not yet a
complete Digital Twin. It does not currently include coupled hydrological
models, real-time state synchronization, scenario simulation, or an interactive
Digital Twin interface.

## Component 1: Data Fusion and Preprocessing for Water Contamination

**Status:** Implemented in Alpha 1

This component prepares satellite-derived observations for model inference:

* defines the area of interest and extracts candidate river tiles;
* selects tiles with sufficient water presence using Sentinel-2 NDWI checks;
* retrieves Sentinel-2 water-quality indicators and Sentinel-3 surface
  temperature observations;
* fuses the observations into a common per-tile feature set;
* fills temporal gaps using Matern Gaussian Process Regression interpolation;
* produces a continuous 5-day cadence;
* applies temporal encoding and robust feature scaling.

The resulting time series provide the input required by the forecasting model.

## Component 2: ML Model Inference for Water Contamination Forecasting

**Status:** Implemented in Alpha 1

This component uses the bundled global multi-feature BiLSTM model to generate
short-term forecasts for:

* Colored Dissolved Organic Matter (CDOM);
* Chlorophyll-a (Chl-a);
* Color;
* Cyanobacteria (Cya);
* Dissolved Organic Carbon (DOC);
* Turbidity;
* Water Quality Index (WQI);
* surface temperature.

The model uses attention and horizon scaling to forecast the prepared
satellite-derived time series. Alpha 1 does not include meteorological inputs,
online model training, or an HTTP inference API.

## Component 3: Hydrological and Water-Quality Digital Twin

**Status:** Foundations only; complete component planned for a future release

Alpha 1 includes initial geospatial foundations through area-of-interest
handling, river tile extraction, water-presence selection, satellite
observations, and water-quality predictions.

The future Digital Twin will integrate these capabilities into a unified,
continuously updated representation of the water system. Its planned scope
includes:

* hydrological and hydraulic models;
* satellite and other observation sources;
* water-quality forecasts;
* model and observation data assimilation;
* scenario simulation and decision-support outputs;
* visualization of current, forecast, and simulated system states.

These Digital Twin capabilities are not yet implemented as an end-to-end
component in Alpha 1.

## Cross-Cutting Capability: Pipeline Orchestration and Result Delivery

**Status:** Implemented in Alpha 1

The `forecast.py` entry point coordinates the implemented components and
records their outcomes. Each run can produce:

* the extracted river-tile GeoJSON and water-selection manifest;
* exact target-date satellite images when available;
* an inference plan and structured pipeline status;
* per-tile and aggregate forecast files in JSON and CSV formats;
* forecast plots.

This capability is documented separately because it connects and deploys the
domain components rather than representing an additional water-science model.

## Product Chain Flow

1. Define the area of interest and target date.
2. Extract river tiles and select tiles containing water.
3. Collect and preprocess Sentinel-2 and Sentinel-3 observations.
4. Run ML model inference for each prepared tile.
5. Export forecasts, status information, and plots.
6. In a future release, combine observations, hydrological models, and
   predictions within the complete Digital Twin.
