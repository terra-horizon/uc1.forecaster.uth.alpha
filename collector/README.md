# TERRA UC1 Data Collection

This folder is the standalone collector module for the TERRA UC1 water-quality pipeline. It owns Sentinel-2 discovery, river tiling, statistical metric collection for every tile, incremental history, validation, and collection state.

Run this module independently. It owns collection and publishes its stable
AOI/tile/observation contract for the separately deployed forecaster.

## Install

Create an environment and install the package from this folder:

```bash
cd collector
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[test]"
```

Create the repository-root `.env` from `../.env.example`, then set the CDSE,
MongoDB, and MinIO application credentials. Optional CDSE backup credentials use
`CDSE_BACKUP_CLIENT_ID`, `CDSE_BACKUP_CLIENT_SECRET`, and numbered pairs
through `CDSE_BACKUP_9_*`. Credentials and tokens are never written to output
files or logs.

## Run

### Independent collector image

Build the collector without the forecaster package:

```bash
docker build -t terra-uc1-collector:local collector
```

The image entrypoint is `python collect.py`. Supply the CDSE and storage
variables through your approved runtime secret mechanism and pass `run` plus
the normal collector arguments. The forecaster image is built separately from
the repository-root `Dockerfile` and does not install or import this package.

### Simple launcher (no package installation)

From this `collector/` folder, use the included launcher. It calls the same
collector CLI, so the next developer does not need to know Python module paths:

```bash
python3 collect.py --help
python3 collect.py run --help
python3 collect.py validate --help  
```

The normal command shape is:

```bash
python3 collect.py run --aoi-id AOI_ID --bbox MIN_LON MIN_LAT MAX_LON MAX_LAT --run-name NAME [options]
```

`--aoi-id` is the stable physical-area identifier used by the independent
forecaster to find this collection. `--run-name` identifies the local output
folder and should be reused for incremental runs of the same AOI.

The first automatic run performs a resumable historical backfill. Later runs
discover and collect only incomplete or newly available tile-date units.
Each non-dry run publishes the collector-owned AOI definition, tiles,
observations, checkpoint, invocation artifacts, and run status to MongoDB and
MinIO. Use `--no-publish` only for explicit local/offline operation.

For an orchestrated environment using Docker Compose, the command shape is:

```bash
docker-compose run --rm collector run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto
```

### UTH server network

On the UTH server, MongoDB and MinIO are private Docker services on the
existing `terra-network`. Use the UTH Compose overlay so the collector can
resolve `terra-mongodb` and `terra-minio`; do not use a laptop SSH-tunnel URI
such as `host.docker.internal:37017` there.

```bash
docker compose -f docker-compose.yml -f docker-compose.uth.yml run --rm collector run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto
```

The server-only `.env` must use Docker-service endpoints, for example
`MONGO_URI=mongodb://...@terra-mongodb:27017/...` and
`MINIO_ENDPOINT=http://terra-minio:9000`. Keep `.env` untracked.

Or using the local python launcher directly:

```bash
python3 collect.py run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto
```

Use a bounded run while commissioning the collector:

```bash
python3 collect.py run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto \
  --max-days-per-run 1 \
  --max-tiles-per-run 1
```

Inspect discovery without writing files:

```bash
python3 collect.py run \
  --aoi-id sperchios \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto \
  --dry-run
```

Validate a completed or partial run:

```bash
python3 collect.py validate --run-dir outputs/sperchios
```

## Run inputs

All inputs accepted by `python3 collect.py run` are listed below. Dates
use `YYYY-MM-DD`; the bounding box uses EPSG:4326 coordinate order
`min_lon min_lat max_lon max_lat`.

| Input | Required | Default | Meaning |
| --- | --- | --- | --- |
| `--bbox MIN_LON MIN_LAT MAX_LON MAX_LAT` | Yes | — | Area of interest used for STAC discovery and river-tile extraction. Four decimal-degree coordinates in west, south, east, north order. |
| `--aoi-id ID` | No | Normalized `--run-name` | Stable physical AOI identifier published in the collector manifest. Use the same ID when running the independent forecaster. |
| `--run-name NAME` | Yes | — | Stable local run identifier. It is normalized into the output directory name; reuse the same name for incremental updates of the same AOI and tile configuration. |
| `--output-root PATH` | No | `outputs` | Parent directory for the collector run directory. The collector writes to `PATH/<normalized-run-name>/`. |
| `--history-start DATE` | No | `2016-01-01` | First date considered during a historical backfill. |
| `--target-date DATE` | No | Current local date | Last date considered in this invocation. Use it to make a bounded historical run reproducible. |
| `--mode {auto,backfill,incremental}` | No | `auto` | Controls discovery windows and state reuse; see [Modes](#modes). |
| `--dry-run` | No | Off | Discovers available and missing dates without writing collector files or requesting statistics. |
| `--max-days-per-run N` | No | No limit | Limits the number of missing observation dates collected in one invocation. `N` must be positive. |
| `--max-tiles-per-run N` | No | No limit | Limits the number of river tiles processed in one invocation. `N` must be positive. |
| `--discovery-chunk-days N` | No | `31` | Number of calendar days per CDSE STAC discovery request. `N` must be positive. This changes request chunking, not the returned record schema. |
| `--spacing-m N` | No | `400` | Distance in metres between generated river-tile centres. Changing it changes the tile set. |
| `--box-size-m N` | No | `400` | Width and height in metres of each generated square tile. Changing it changes the tile set. |
| `--min-river-length-m N` | No | `10000.0` | Minimum river-geometry length in metres required to generate tiles. |
| `--projected-crs CRS` | No | `EPSG:32634` | Projected CRS used for metre-based river length and tile calculations. Select a CRS appropriate for the AOI. |
| `--max-cloud-coverage N` | No | `30` | Maximum Sentinel-2 cloud-cover percentage accepted by CDSE discovery and statistics requests. |
| `--no-publish` | No | Off | Keep the collector contract local; do not connect to or write MongoDB/MinIO. |

Changing the AOI, tile parameters, or projected CRS while reusing a run name
invalidates the cached tile set. Use a new run name or output root when keeping
the previous local staging artifacts is important.

## Validation input

Validate an existing run without contacting CDSE:

| Command | Required input | Result |
| --- | --- | --- |
| `python -m data_collection validate --run-dir PATH` | `--run-dir`: existing collector run directory | Validates the required JSON and GeoJSON artifacts against the bundled schemas. It exits `0` when valid and `1` when invalid. |

## Credential and environment inputs

The collector reads credentials at runtime. They are never written to output
files or logs.

| Variable | Required | Meaning |
| --- | --- | --- |
| `CDSE_CLIENT_ID` | Yes | Primary Copernicus Data Space Ecosystem client ID. |
| `CDSE_CLIENT_SECRET` | Yes | Primary Copernicus Data Space Ecosystem client secret. |
| `CDSE_BACKUP_CLIENT_ID`, `CDSE_BACKUP_CLIENT_SECRET` | No | First fallback credential pair. `CDSE_FALLBACK_CLIENT_ID` and `CDSE_FALLBACK_CLIENT_SECRET` are accepted aliases. |
| `CDSE_BACKUP_2_CLIENT_ID` … `CDSE_BACKUP_9_CLIENT_ID` and matching secrets | No | Additional fallback credential pairs. A pair is used only when both values are set. |
| `DATA_COLLECTION_ENV_FILE` | No | Explicit path to an environment file. If unset, the collector searches for `.env` from the current directory, collector directory, then repository root. |
| `MONGO_URI` | Yes unless `--no-publish` | Complete application MongoDB URI including database and authentication source. |
| `MINIO_ENDPOINT` | Yes unless `--no-publish` | Complete MinIO S3 API HTTP(S) endpoint. |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | Yes unless `--no-publish` | MinIO application credentials. |
| `MINIO_BUCKET_NAME` | Yes unless `--no-publish` | Existing bucket receiving collector artifacts. |
| `MINIO_VERIFY_TLS`, `MINIO_CA_BUNDLE` | No | TLS verification controls; verification defaults to enabled. |

## Modes

- `auto`: when no completed state exists, discovers from `--history-start` to
  `--target-date`; after backfill completion, starts after the last checked
  date and retries incomplete known units.
- `backfill`: explicitly discovers the complete interval from
  `--history-start` to `--target-date`, regardless of existing completion
  state.
- `incremental`: starts after the last checked date (or `--history-start` when
  no checkpoint exists) and retries known incomplete units.

Collection progress is tracked per `tile_id + observation_date`. A date is complete only after every expected tile has a terminal `collected` or `unavailable` record. Network and authentication failures remain retryable and are reported separately.

## Outputs

For `--output-root outputs --run-name sperchios`, the collector writes under
`outputs/sperchios/`:

| Path | Purpose |
| --- | --- |
| `collection/collection_run_result.json` | Machine-readable result for the invocation: status, discovered dates, collected dates, retryable failures, warnings, and artifact paths. |
| `collection/state.json` | Incremental checkpoint: known STAC dates, completion status, expected tiles, retryable failures, and last checked date. |
| `history/global_history.json` | Canonical local collector history, one raw record per tile and observation date. |
| `history/global_history.csv` | CSV compatibility view of the same history. |
| `history/tiles/<tile_id>/history.json` and `.csv` | Per-tile views of the history. |
| `tiles/river_tiles.geojson` | Generated river-tile geometry. |
| `tiles/tile_records.json` | Tile names, bounding boxes, geometry metadata, and size. |
| `tiles/tile_state.json` | Tile-configuration hash used to decide whether cached tiles remain valid. |
| `cdse_stac_cache/<start>_<end>.json` | Cached CDSE discovery responses for each discovery window. |
| `logs/collector.jsonl` | Structured collector log events. |
| `collector_work/` | Per-request temporary statistical inputs and CSV outputs used while records are assembled. |

Collector records use `water_check_status: not_performed` and
`water_status: unknown`; the forecaster enriches copies before inference. See
[DATA_CONTRACT.md](DATA_CONTRACT.md) for record schemas, MongoDB/MinIO mapping,
relationships, and durable-storage semantics.

The Python integration boundary is:

```python
from data_collection import CollectionRequest, collect

result = collect(CollectionRequest(...))
```

`CollectionRequest.publish` defaults to `True`. Set it to `False` only for a
deliberately local integration. The collector package contains its own storage
client and never imports the forecaster.

The forecaster is independent of this module and consumes the published
collection contract through a run directory or shared storage.

## Tests

```bash
python3 -m pytest -q
```

Live CDSE calls are not part of the default test suite. See
[`DATA_CONTRACT.md`](DATA_CONTRACT.md) for the collector-to-forecaster and
storage data model.
