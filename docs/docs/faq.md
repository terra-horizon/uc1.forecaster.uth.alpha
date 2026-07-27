# FAQ & Known Issues

## Does the image expose an HTTP API?

No. Alpha Version is a CLI image that runs `forecaster.scheduled_pipeline`.

## Are CDSE credentials built into the Docker image?

No. Credentials must be supplied at runtime through environment variables or an ignored `.env` file passed with `--env-file`.

## Where are outputs written?

The scheduled pipeline writes canonical JSON/GeoJSON/STAC objects to the
configured MinIO bucket and queryable documents to MongoDB. It also uses the
mounted `data/inference_runs/` directory for local staging. Direct inference
writes local outputs only.

## Does the image start MongoDB or MinIO?

No. MongoDB and MinIO are external dependencies configured through `.env`. The
operator creates the database user, bucket, and access key; the pipeline creates
its collections and indexes after a successful preflight.

## Can I use an SSH tunnel for MongoDB?

Yes. Start the tunnel outside Docker, then point `MONGO_URI` at the host-side
tunnel address reachable by the container. The image contains no SSH keys or
tunnel process. MinIO uses its configured S3 endpoint directly.

## Does this release include Meteorological Information?

No. Meteorological integration is planned for a later version.
