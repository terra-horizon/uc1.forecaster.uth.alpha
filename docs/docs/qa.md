# Quality Assurance

## Unit Tests

Run the repository test suite with:

```bash
python3 -m pytest -q
```

## Docker Checks

Build the image:

```bash
docker build -t uc1-forecaster:local .
```

Verify the CLI entrypoint:

```bash
./scripts/docker-run.sh --help
```

Verify that local secrets are not copied into the image:

```bash
UC1_REMOVE_CONTAINER=1 UC1_DOCKER_ARGS="--entrypoint sh" ./scripts/docker-run.sh -c 'test ! -f /app/.env'
```

## Vulnerability Checks

The repository includes an on-demand Trivy workflow that scans:

* Dockerfile and infrastructure configuration;
* the published GHCR image for operating system and library vulnerabilities.

The workflow uploads SARIF reports to GitHub Code Scanning.

Local scan reports should be stored under `local_scans/`; the directory is ignored by Git.
