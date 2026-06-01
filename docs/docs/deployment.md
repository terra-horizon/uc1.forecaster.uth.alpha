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

## Image Publishing

The `Build and Push Docker Image` workflow runs when a tag matching `v*` is pushed.

```bash
git tag v1.0.0
git push origin v1.0.0
```

The resulting image is published to:

```text
ghcr.io/terra-horizon/uc1.forecaster.uth.alpha1:<tag>
```

## Vulnerability Scan

After an image is published, run `Vulnerability Scan (On-Demand)` from GitHub Actions and provide the same tag in the `image_tag` input. The workflow scans both the repository Docker configuration and the published image, then uploads SARIF results to GitHub Code Scanning.

Local scan reports should be written under `local_scans/`, which is ignored by Git.

## Documentation Publishing

The `Deploy Docs (On-Demand)` workflow publishes this documentation through Mike. Provide a documentation version such as `v1.0.0` when running the workflow.
