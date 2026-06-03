# Deployment

UC1 is packaged as a CLI Docker image and published to GitHub Container Registry from release tags.

## Local Build

```bash
docker build -t uc1-forecaster:local .
```

## Local Run

```bash
./scripts/docker-run.sh \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name sperchios_test_run \
  --output-root /app/inference_results
```

The helper script names containers as `uc1-forecaster-YYYYMMDD-HHMMSS`, mounts `inference_results/`, and passes `.env` when present. The stopped container remains visible in Docker Desktop by default; set `UC1_REMOVE_CONTAINER=1` for automatic cleanup. Set `UC1_CONTAINER_NAME`, `UC1_IMAGE_NAME`, or `UC1_OUTPUT_DIR` to override the other defaults.

