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

When `TERRA_STORAGE_ENABLED=true`, configure existing MongoDB and MinIO
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

### Scheduled pipeline arguments

The Docker entrypoint accepts `--bbox` and `--run-name`; `--run-name` is an
operational label such as `uc1-dev` or `uc1-prod`, not part of the storage key.

* `--history-start`: start date for discovery; default `2016-01-01`.
* `--target-date`: collection target date; defaults to today.
* `--backfill-all`: process all historical discovery windows.
* `--max-days-per-run`, `--max-tiles-per-run`: bound a commissioning run.
* `--skip-inference`: collect and persist without creating a forecast.

### Direct inference arguments

`python -m forecaster.inference` supports one-off local inference. In addition
to `--bbox`, `--target-date`, `--output-root`, and `--run-name`, it accepts:

* `--skip-images`: skip exact target-date image downloads.
* `--per-tile-images`: download imagery for every selected tile.
* `--skip-global-image`: skip the global AOI image.
* `--image-keys`: comma-separated target-date image products.
