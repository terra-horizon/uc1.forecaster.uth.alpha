# Configuration

Runtime configuration is provided through CLI arguments and environment variables.

## Required Environment Variables

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

## CLI Arguments

Common arguments:

* `--bbox`: AOI bounding box in EPSG:4326 as min longitude, min latitude, max longitude, max latitude.
* `--target-date`: inference anchor date in `YYYY-MM-DD` format.
* `--output-root`: directory where inference outputs are written.
* `--run-name`: optional stable name for the run directory.
* `--skip-images`: skip exact target-date image downloads.
* `--per-tile-images`: download imagery for every selected tile.
* `--skip-global-image`: skip the global AOI image.
* `--image-keys`: comma-separated target-date image products.
