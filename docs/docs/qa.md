# Quality Assurance

## Unit Tests

Run the repository test suite with:

```bash
python3 -m pytest -q
```

The default suite includes deterministic pipeline scenarios that control the
external OSM and CDSE boundaries. Generated raw results are written under
`tests/results/<scenario>/latest/` and are ignored by Git.

Several scenarios deliberately produce a `failed` pipeline status to verify
that invalid or unavailable inputs are handled safely and recorded with the
expected structured error code. When those expected outcomes are observed, the
automated scenario test passes.

## Live Pipeline Smoke Test

Run the credentialed Sperchios happy-path integration test explicitly:

```bash
RUN_LIVE_PIPELINE_TESTS=1 python3 -m pytest -m live -q
```

The live test uses real OSM and CDSE services, so its duration and target-date
image availability depend on external systems.

## Scenario Report

Regenerate the sanitized committed scenario report after executing the tests:

```bash
python3 scripts/generate-scenario-report.py
```

The [Pipeline Test Scenarios](pipeline-test-scenarios.md) page documents each
scenario's expected result and latest verified execution. Validate the complete
documentation site before publication:

```bash
mkdocs build --strict --config-file docs/mkdocs.yml
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
* the published GHCR image for operating system and library vulnerabilities, or a local workflow-built image when the requested tag is not published yet.

The workflow always uploads SARIF reports as workflow artifacts. It also uploads to GitHub Code Scanning when the repository has the required security features enabled.

Local scan reports should be stored under `local_scans/`; the directory is ignored by Git.
