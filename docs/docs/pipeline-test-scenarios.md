# Pipeline Test Scenarios

The default suite is deterministic and offline. It must run on a fresh clone
without CDSE, MongoDB, MinIO, or a developer's `.env` file. The current checked
result is **40 passed, 1 skipped**; the skipped test is the explicit live CDSE
smoke test.

## Run the checks

```bash
python3 -m pytest -q
RUN_LIVE_PIPELINE_TESTS=1 python3 -m pytest -m live -q
```

The second command is opt-in. It requires valid CDSE credentials and does not
prove the configured UTH MongoDB/MinIO deployment.

## Offline scenarios

| Area | Scenario | Expected result |
| --- | --- | --- |
| Direct inference | Bundled model | A forecast JSON and CSV are created. |
| Direct inference | No river tiles | Controlled `NO_RIVER_TILES` failure; downstream stages do not run. |
| Direct inference | No water tiles | Controlled `NO_WATER_TILES` failure. |
| Direct inference | No historical data | Controlled `NO_SATELLITE_DATA` failure. |
| Direct inference | Target image unavailable | Inference succeeds with `TARGET_IMAGES_UNAVAILABLE` warning. |
| Direct inference | Partial historical data | Valid tile forecasts; unavailable tile is recorded as partial. |
| Direct inference | Model failure | Controlled `INFERENCE_FAILED` failure. |
| Direct inference | Invalid image key | Controlled `VALIDATION_ERROR` result. |
| Collector | First collection run | JSON schema validation succeeds and the incremental contract is written. |
| Collector | Re-run and legacy cache | No duplicate records; legacy cache is promoted safely. |
| Scheduled pipeline | Incremental orchestration | Collection mode, dry run, backfill flag, state, water history, and STAC exports behave deterministically. |
| Storage settings | Configuration parsing | Generic Mongo URI, MinIO endpoint, disabled mode, AOI hash, and storage keys are validated. |
| Scheduled storage | Mocked persistence boundary | AOI definition, observations, tiles, run snapshot, and MinIO object keys are passed to the storage boundary. |

## What is deliberately not simulated

The default suite does not make real CDSE calls or connect to a real MongoDB or
MinIO endpoint. Those are deployment checks because they require operator
credentials, network access, a provisioned bucket, and a provisioned database
user.

Before a release deployment, run:

```bash
docker compose --env-file .env run --rm --entrypoint python forecaster scripts/storage_health.py
docker compose --env-file .env run --rm forecaster --help
```

Then perform a narrow, credentialed scheduled run and inspect the resulting
MongoDB documents and MinIO objects. See
[`collector/DATA_CONTRACT.md`](https://github.com/terra-horizon/uc1.forecaster.uth.alpha/blob/main/collector/DATA_CONTRACT.md)
for the expected data model.
