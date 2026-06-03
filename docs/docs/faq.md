# FAQ & Known Issues

## Does the image expose an HTTP API?

No. Alpha Version is a CLI image that runs `forecast.py`.

## Are CDSE credentials built into the Docker image?

No. Credentials must be supplied at runtime through environment variables or an ignored `.env` file passed with `--env-file`.

## Where are outputs written?

Outputs are written under the path passed to `--output-root`. When running in Docker, mount a host directory and point `--output-root` to the mounted container path.

## Does this release include Meteorological Information?

No. Meteorological integration is planned for a later version.
