#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="${UC1_IMAGE_NAME:-uc1-forecaster:local}"
CONTAINER_NAME="${UC1_CONTAINER_NAME:-uc1-forecaster-$(date +%Y%m%d-%H%M%S)}"
OUTPUT_DIR="${UC1_OUTPUT_DIR:-$PWD/inference_results}"

mkdir -p "$OUTPUT_DIR"

docker_args=(-v "$OUTPUT_DIR:/app/inference_results")

if [[ "${UC1_REMOVE_CONTAINER:-0}" == "1" ]]; then
  docker_args+=(--rm)
fi

if [[ -f .env ]]; then
  docker_args+=(--env-file .env)
fi

if [[ -n "${UC1_DOCKER_ARGS:-}" ]]; then
  # Optional escape hatch for rare Docker-only flags, e.g. '--entrypoint sh'.
  read -r -a extra_docker_args <<< "$UC1_DOCKER_ARGS"
  docker_args+=("${extra_docker_args[@]}")
fi

exec docker run --name "$CONTAINER_NAME" "${docker_args[@]}" "$IMAGE_NAME" "$@"
