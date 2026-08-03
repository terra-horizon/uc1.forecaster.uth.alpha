# Architecture

The Alpha 1 deployment separates collection from forecasting. The collector
publishes AOI-scoped observations; `forecaster.from_storage` retrieves them and
runs the model without collector code or CDSE credentials.

## Component Boundaries

| Component | Alpha 1 implementation |
| --- | --- |
| Collection | Independent collector: river tiling, Sentinel collection, quality flags, observation publication, collection checkpoint, and collector run lifecycle. |
| Preprocessing and ML Model Inference | Independent forecaster: stored-observation retrieval, model-ready feature publication, bundled global multi-feature BiLSTM inference, forecast publication, and forecaster run lifecycle. |
| Hydrological and Water-Quality Digital Twin | Geospatial and forecasting foundations only; the complete Digital Twin is not implemented. |
| Pipeline Orchestration and Result Delivery | AOI-addressed retrieval from UTH storage and forecast artifact export through `forecaster.from_storage`. |

## Pipeline

1. The collector records a `running` run and restores its previous AOI state.
2. It discovers Sentinel data, generates/reuses river tiles, and calculates observations.
3. It publishes `collection_state`, `tiles`, and `observations`, then records a terminal collector run.
4. The independently invoked forecaster records its own `running` run and reads those MongoDB contracts by `aoi_id`.
5. It creates and publishes model-ready `preprocessed_features`.
6. The bundled TensorFlow/Keras model generates and publishes `forecasts`.
7. The forecaster records its terminal run. MinIO holds matching durable artifacts; local files are staging/compatibility outputs.

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

The collector requires CDSE credentials for Sentinel data access. Both
independent modules require existing MongoDB and MinIO services for normal
published operation. All endpoints
and application credentials come from runtime environment variables; the image
does not create databases, buckets, users, or SSH tunnels.

MongoDB collections are connected by `aoi_id`, `tile_id`, dates, and run IDs.
MinIO objects are organized beneath `terra-uc1/<aoi-id>/`; MongoDB artifact
references contain the matching bucket, object key, and checksum. The canonical
schema is in
[`collector/DATA_CONTRACT.md`](https://github.com/terra-horizon/uc1.forecaster.uth.alpha/blob/main/collector/DATA_CONTRACT.md).
