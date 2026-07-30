# Architecture

The Alpha 1 deployment separates collection from forecasting. The collector
publishes AOI-scoped observations; `forecaster.from_storage` retrieves them and
runs the model without collector code or CDSE credentials.

## Component Boundaries

| Component | Alpha 1 implementation |
| --- | --- |
| Data Fusion and Preprocessing | External collector: river tiling, Sentinel collection, quality flags, and observation publication. |
| ML Model Inference | Bundled global multi-feature BiLSTM inference and forecast generation. |
| Hydrological and Water-Quality Digital Twin | Geospatial and forecasting foundations only; the complete Digital Twin is not implemented. |
| Pipeline Orchestration and Result Delivery | AOI-addressed retrieval from UTH storage and forecast artifact export through `forecaster.from_storage`. |

## Pipeline

1. The user provides an AOI bounding box and target date.
2. The river tile extractor generates candidate tiles for the AOI.
3. The water tile selector checks water presence using Sentinel-2 NDWI products.
4. The collectors retrieve Sentinel-2 and Sentinel-3 statistics and target-date imagery through CDSE APIs.
5. The augmentation step interpolates missing values to the model cadence.
6. The global preprocessor applies feature scaling and prepares model tensors.
7. The bundled TensorFlow/Keras model generates the forecast horizon.
8. The pipeline writes queryable MongoDB documents plus durable MinIO
   JSON/GeoJSON/STAC artifacts. Local CSV files are staging/compatibility
   outputs rather than the durable data source.

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

## Persistence and external services

The pipeline requires CDSE credentials for Sentinel data access. The scheduled
pipeline also requires existing MongoDB and MinIO services. All endpoints
and application credentials come from runtime environment variables; the image
does not create databases, buckets, users, or SSH tunnels.

MongoDB collections are connected by `aoi_id`, `tile_id`, dates, and run IDs.
MinIO objects are organized beneath `terra-uc1/<aoi-id>/`; MongoDB artifact
references contain the matching bucket, object key, and checksum. The canonical
schema is in
[`collector/DATA_CONTRACT.md`](https://github.com/terra-horizon/uc1.forecaster.uth.alpha/blob/main/collector/DATA_CONTRACT.md).
