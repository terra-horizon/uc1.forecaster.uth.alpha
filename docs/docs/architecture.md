# Architecture

The Alpha 1 deployment of TERRA Product Chain 1 is organized as a batch
inference pipeline around `forecast.py`.

## Component Boundaries

| Component | Alpha 1 implementation |
| --- | --- |
| Data Fusion and Preprocessing | River tile extraction, water selection, Sentinel-2 and Sentinel-3 collection, gap interpolation, temporal encoding, and scaling. |
| ML Model Inference | Bundled global multi-feature BiLSTM inference and forecast generation. |
| Hydrological and Water-Quality Digital Twin | Geospatial and forecasting foundations only; the complete Digital Twin is not implemented. |
| Pipeline Orchestration and Result Delivery | Stage coordination, status recording, and artifact export through `forecast.py`. |

## Pipeline

1. The user provides an AOI bounding box and target date.
2. The river tile extractor generates candidate tiles for the AOI.
3. The water tile selector checks water presence using Sentinel-2 NDWI products.
4. The collectors retrieve Sentinel-2 and Sentinel-3 statistics and target-date imagery through CDSE APIs.
5. The augmentation step interpolates missing values to the model cadence.
6. The global preprocessor applies feature scaling and prepares model tensors.
7. The bundled TensorFlow/Keras model generates the forecast horizon.
8. The pipeline writes an inference plan, forecast outputs, and optional plots.

The river tile extractor provides an initial hydrological geospatial context,
but Alpha 1 does not run hydrological or hydraulic simulation models. Sentinel
observations and ML forecasts will become inputs to the future complete
Digital Twin.

## Model Artifacts

The runtime image includes `forecaster/models/default_model`, containing:

* the Keras model checkpoint;
* global feature scalers;
* model metadata;
* training history;
* dataset summary;
* tile ID mapping.

These artifacts are treated as runtime dependencies for Alpha 1 inference.

## External Services

The pipeline requires CDSE credentials for Sentinel data access. Credentials are read from environment variables and must not be committed to the repository or Docker image.
