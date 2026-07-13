# TERRA UC1 Data Collection

This folder is the standalone data-collection module for the TERRA UC1 water-quality pipeline. It owns Sentinel-2 discovery, river tiling, water and cloud screening, statistical metric collection, incremental history, validation, and collection state. It does not contain preprocessing, model inference, forecast persistence, or forecast STAC export.

## Install

Create an environment and install the package from this folder:

```bash
cd data-collection
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e ".[test]"
cp .env.example .env
```

Set `CDSE_CLIENT_ID` and `CDSE_CLIENT_SECRET` in `.env`. Optional backup credentials use `CDSE_BACKUP_CLIENT_ID`, `CDSE_BACKUP_CLIENT_SECRET`, and numbered pairs through `CDSE_BACKUP_9_*`. Credentials and tokens are never written to output files or logs.

## Run

The first automatic run performs a resumable historical backfill. Later runs discover and collect only incomplete or newly available tile-date units.

```bash
python -m data_collection run \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto
```

Use a bounded run while commissioning the collector:

```bash
python -m data_collection run \
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
python -m data_collection run \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name sperchios \
  --output-root outputs \
  --history-start 2016-01-01 \
  --mode auto \
  --dry-run
```

Validate a completed or partial run:

```bash
python -m data_collection validate --run-dir outputs/sperchios
```

## Modes

- `auto`: backfill when collection state is absent, then incrementally update.
- `backfill`: explicitly discover the complete interval from `--history-start`.
- `incremental`: discover dates after the last checked date and retry known incomplete units.

Collection progress is tracked per `tile_id + observation_date`. A date is complete only after every expected tile has a terminal `collected` or `unavailable` record. Network and authentication failures remain retryable and are reported separately.

## Outputs

Each run writes collection state and result metadata, global and per-tile JSON/CSV history, river-tile GeoJSON, water-screening manifests, STAC discovery caches, and JSONL logs. See [DATA_CONTRACT.md](DATA_CONTRACT.md) and `data_collection/schemas/` for the exchange contract.

The Python integration boundary is:

```python
from data_collection import CollectionRequest, collect

result = collect(CollectionRequest(...))
```

The forecasting repository currently calls the same service through `LocalCollectionProvider`. A future HTTP client can implement the same request/result contract without changing preprocessing or forecasting.

## Tests

```bash
python3 -m pytest -q
```

Live CDSE calls are not part of the default test suite.
