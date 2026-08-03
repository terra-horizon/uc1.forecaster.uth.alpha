# Configuration

Runtime configuration is provided through CLI arguments and environment variables.

## Required collection credentials

```text
CDSE_CLIENT_ID
CDSE_CLIENT_SECRET
```

## Optional Backup Credentials

The pipeline can rotate through backup CDSE credentials when configured:

```text
CDSE_BACKUP_CLIENT_ID
CDSE_BACKUP_CLIENT_SECRET
CDSE_BACKUP_2_CLIENT_ID
CDSE_BACKUP_2_CLIENT_SECRET
```

Backup credentials may continue through `CDSE_BACKUP_9_CLIENT_ID` and `CDSE_BACKUP_9_CLIENT_SECRET`.

## Local `.env`

For local development, place credentials in a repository-root `.env` file. The file is ignored by Git and excluded from the Docker build context.

```text
CDSE_CLIENT_ID=...
CDSE_CLIENT_SECRET=...
```

## Storage endpoints

The scheduled pipeline requires existing MongoDB and MinIO
services in the same root `.env` file:

```text
TERRA_AOI_ID=example-aoi
MONGO_URI=mongodb://user:password@host:27017/terra_db?authSource=terra_db
MINIO_ENDPOINT=https://minio.example.org
MINIO_ACCESS_KEY=...
MINIO_SECRET_KEY=...
MINIO_BUCKET_NAME=terra-uc1
MINIO_VERIFY_TLS=true
```

The operator provisions the MongoDB application user and the MinIO bucket and
access key. The pipeline creates its own MongoDB collections and indexes after
a successful connection check. It does not provision infrastructure or create
SSH tunnels. Set `MINIO_VERIFY_TLS=false` only for a trusted development MinIO
endpoint with a self-signed certificate; use `MINIO_CA_BUNDLE` when a custom CA
should be trusted instead.

`TERRA_AOI_ID` is the durable physical study-area identity. Keep it stable for
the same bbox and tiling configuration; use separate storage endpoints or
buckets for isolated development and production deployments rather than adding
an environment label to the AOI ID.

Run the read-only preflight before a first scheduled run:

```bash
docker compose --env-file .env run --rm --entrypoint python forecaster scripts/storage_health.py
```

## CLI Arguments

### Collector arguments

Run `python3 collector/collect.py run` with `--bbox`, `--aoi-id`, and
`--run-name`. The AOI ID is the shared storage identity; the run name is a
local operational label.

* `--history-start`: start date for discovery; default `2016-01-01`.
* `--target-date`: collection target date; defaults to today.
* `--mode auto|backfill|incremental`: select discovery behavior.
* `--max-days-per-run`, `--max-tiles-per-run`: bound a commissioning run.
* `--no-publish`: explicit local-only operation without MongoDB/MinIO writes.

### Forecaster arguments

`python -m forecaster.from_storage` reads a collector-published AOI. It accepts:

* `--aoi-id`: stable AOI identity used to find the data.
* `--as-of-date`: optional upper bound for the observation anchor.
* `--collection-run-dir`: an explicit completed collector output directory.
* `--no-publish`: keep a local handoff verification entirely local.
