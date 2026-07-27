# Deployment

This is the **Alpha 1 deployment of TERRA Product Chain 1**. It packages the
implemented data-fusion, preprocessing, ML-inference, orchestration, and
result-delivery capabilities as a CLI Docker image.

The deployment includes foundations for the future Hydrological and
Water-Quality Digital Twin, but it must not be interpreted as a complete
Digital Twin deployment. Hydrological models, data assimilation, scenario
simulation, and an interactive Digital Twin interface are future work.

Release images are published to GitHub Container Registry from release tags.

## Local Build

```bash
docker build -t uc1-forecaster:local .
```

## Configure external dependencies

Copy `.env.example` to `.env` and configure existing CDSE, MongoDB, and MinIO
credentials. The image runs only the forecaster; it does not start MongoDB or
MinIO. The same variables work for remote services, local services, or services
available through a host-side SSH tunnel.

Run the storage preflight before processing data:

```bash
docker compose --env-file .env run --rm --entrypoint python forecaster scripts/storage_health.py
```

## Scheduled run

```bash
docker compose --env-file .env run --rm forecaster \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --run-name uc1-dev
```

Compose mounts `data/inference_runs/` to `/app/data/inference_runs` by default.
The first run discovers missing history; add `--backfill-all` for a full
historical backfill. Normal later runs are incremental and idempotent.

Use a server scheduler to execute the same one-shot Compose command with a
stable `uc1-prod` run label. Store the production `.env` outside source control
and deploy an immutable image tag.

## Direct one-off inference

For debugging without the MongoDB/MinIO persistence workflow:

```bash
docker compose --env-file .env run --rm --entrypoint python forecaster \
  -m forecaster.inference \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name manual-inference
```
