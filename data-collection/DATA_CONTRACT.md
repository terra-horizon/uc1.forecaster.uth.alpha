# Data Collection Contract

## Stable identifiers

- Observation upsert key: `tile_id + observation_date`.
- Dates: UTC calendar dates in `YYYY-MM-DD` format.
- Geometry: WGS84 bounding boxes and GeoJSON polygons.
- Schema version: `1.0.0`.

## Global history

`history/global_history.json` is a plain JSON array. The same rows are written to `history/global_history.csv` with a fixed column order. Each record contains:

- Tile, observation date, bbox, timestamps, and collection attempt count.
- `collection_status`: `collected` or `unavailable`.
- `water_status`: `water`, `no_water`, or `unknown`.
- Water percentage, cloud percentage, valid pixels, source scene count, and STAC item IDs.
- Asset paths and quality flags.
- `CDOM`, `Chl_a`, `Color`, `Cya`, `DOC`, `Turb`, and `WQI`.

Missing numeric values are JSON `null`, never `NaN`. Dry, cloudy, no-water, and genuine no-data observations remain visible as `unavailable` records. Transport and authentication failures are not converted into no-data records; they appear in `failed_units` and remain eligible for retry.

## Request and result

`CollectionRequest` is the producer input contract. `CollectionResult` reports the run status, discovered and selected dates, written records, retryable failures, latest usable observation, warnings, and paths to generated artifacts.

The JSON representations are defined by:

- `collection-request.schema.json`
- `collection-result.schema.json`
- `collection-state.schema.json`
- `global-history.schema.json`
- `history-record.schema.json`
- `river-tiles.schema.json`
- `water-selection.schema.json`

The current connector is local Python. A future HTTP service should accept and return these same request/result shapes and make the referenced history artifacts available to the processing pipeline.
