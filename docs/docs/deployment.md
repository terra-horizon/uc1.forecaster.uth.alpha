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

## Local Run

```bash
./scripts/docker-run.sh \
  --bbox 22.433493 38.837552 22.569555 38.894223 \
  --target-date 2026-05-27 \
  --run-name sperchios_test_run \
  --output-root /app/inference_results
```

The helper script names containers as `uc1-forecaster-YYYYMMDD-HHMMSS`, mounts `inference_results/`, and passes `.env` when present. The stopped container remains visible in Docker Desktop by default; set `UC1_REMOVE_CONTAINER=1` for automatic cleanup. Set `UC1_CONTAINER_NAME`, `UC1_IMAGE_NAME`, or `UC1_OUTPUT_DIR` to override the other defaults.
