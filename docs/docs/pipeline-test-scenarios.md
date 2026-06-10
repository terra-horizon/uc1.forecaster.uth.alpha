# Pipeline Test Scenarios

This page defines the automated pipeline scenarios, their expected results, and the latest sanitized verified-run snapshot. Raw generated artifacts remain under ignored `tests/results/` folders.

## How to Read the Results

A pipeline status of `failed` does **not** mean that the automated test failed. Several scenarios deliberately provide invalid or unavailable inputs to verify that the pipeline stops safely, records a structured error code, and does not continue into an invalid stage. Those rows are marked **Passed: expected outcome observed** when the intended failure was handled correctly.

The `Latest pipeline status` column reports the behavior of the pipeline under the scenario. The `Verification` column reports whether that behavior matched the test expectation.

## Execution Commands

```bash
python3 -m pytest -q
RUN_LIVE_PIPELINE_TESTS=1 python3 -m pytest -m live -q
python3 scripts/generate-scenario-report.py
```

## Latest Verified Summary

| Scenario | Expected pipeline outcome | Latest pipeline status | Verification | Errors | Warnings |
| --- | --- | --- | --- | --- | --- |
| Successful end-to-end inference | `success`; forecast JSON and CSV are created. | `success` | **Passed**: expected outcome observed | None | None |
| No river detected | Handled failure: `failed`; error `NO_RIVER_TILES`; inference is not attempted. | `failed` | **Passed**: expected outcome observed | `NO_RIVER_TILES` | None |
| River tiles contain no water | Handled failure: `failed`; error `NO_WATER_TILES`; inference is not attempted. | `failed` | **Passed**: expected outcome observed | `NO_WATER_TILES` | None |
| No historical satellite data | Handled failure: `failed`; aggregate and per-tile `NO_SATELLITE_DATA` errors. | `failed` | **Passed**: expected outcome observed | `NO_SATELLITE_DATA` | None |
| Target-date images unavailable | `success`; warning `TARGET_IMAGES_UNAVAILABLE`; unavailable image metadata is retained. | `success` | **Passed**: expected outcome observed | None | `TARGET_IMAGES_UNAVAILABLE` |
| Partial historical-data availability | `partial`; valid tiles forecast; invalid tiles report `NO_SATELLITE_DATA`. | `partial` | **Passed**: expected outcome observed | None | None |
| Inference failure | Handled failure: `failed`; aggregate and per-tile `INFERENCE_FAILED` errors. | `failed` | **Passed**: expected outcome observed | `INFERENCE_FAILED` | None |
| Invalid image configuration | Handled failure: validation raises `ValueError`; result code `VALIDATION_ERROR` records the failed run. | `failed` | **Passed**: expected outcome observed | `VALIDATION_ERROR` | None |
| Live Sperchios happy-path smoke | `success` or `partial`; at least one tile completes inference. | `success` | **Passed**: expected outcome observed | None | None |

## Scenario Details

### 1. Successful end-to-end inference

**Purpose:** Exercises river extraction, water selection, historical features, and the bundled model.

**Expected result:** `success`; forecast JSON and CSV are created.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `success` at `2026-06-08T21:43:08.116268+00:00`.

**Recorded codes:** errors None; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: success`, `feature_preparation: success`, `inference: success`.

**Artifact categories:** `forecast_csv`, `forecast_json`, `inference_plan`, `pipeline_result`, `river_tiles`, `water_manifest`.

### 2. No river detected

**Purpose:** Confirms the pipeline stops before water selection when the AOI has no qualifying river.

**Expected result:** Handled failure: `failed`; error `NO_RIVER_TILES`; inference is not attempted.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `failed` at `2026-06-08T21:43:08.119538+00:00`.

**Recorded codes:** errors `NO_RIVER_TILES`; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: failed`.

**Artifact categories:** `pipeline_result`, `river_tiles`, `water_manifest`.

### 3. River tiles contain no water

**Purpose:** Confirms river tiles rejected by water screening do not proceed to feature collection.

**Expected result:** Handled failure: `failed`; error `NO_WATER_TILES`; inference is not attempted.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `failed` at `2026-06-08T21:43:08.121529+00:00`.

**Recorded codes:** errors `NO_WATER_TILES`; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: failed`.

**Artifact categories:** `pipeline_result`, `river_tiles`, `water_manifest`.

### 4. No historical satellite data

**Purpose:** Confirms selected water tiles without sufficient historical features cannot run inference.

**Expected result:** Handled failure: `failed`; aggregate and per-tile `NO_SATELLITE_DATA` errors.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `failed` at `2026-06-08T21:43:08.123330+00:00`.

**Recorded codes:** errors `NO_SATELLITE_DATA`; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: success`, `feature_preparation: failed`.

**Artifact categories:** `pipeline_result`, `river_tiles`, `water_manifest`.

### 5. Target-date images unavailable

**Purpose:** Confirms exact-date preview image absence does not prevent inference.

**Expected result:** `success`; warning `TARGET_IMAGES_UNAVAILABLE`; unavailable image metadata is retained.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `success` at `2026-06-08T21:43:08.126519+00:00`.

**Recorded codes:** errors None; warnings `TARGET_IMAGES_UNAVAILABLE`.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: warning`, `feature_preparation: success`, `inference: success`.

**Artifact categories:** `forecast_csv`, `forecast_json`, `inference_plan`, `pipeline_result`, `river_tiles`, `water_manifest`.

### 6. Partial historical-data availability

**Purpose:** Confirms valid tiles run while tiles without historical data report explicit errors.

**Expected result:** `partial`; valid tiles forecast; invalid tiles report `NO_SATELLITE_DATA`.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `partial` at `2026-06-08T21:43:08.129054+00:00`.

**Recorded codes:** errors None; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: success`, `feature_preparation: partial`, `inference: success`.

**Artifact categories:** `forecast_csv`, `forecast_json`, `inference_plan`, `pipeline_result`, `river_tiles`, `water_manifest`.

### 7. Inference failure

**Purpose:** Confirms a run fails when every prepared tile fails model inference.

**Expected result:** Handled failure: `failed`; aggregate and per-tile `INFERENCE_FAILED` errors.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `failed` at `2026-06-08T21:43:08.131837+00:00`.

**Recorded codes:** errors `INFERENCE_FAILED`; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: success`, `feature_preparation: success`, `inference: failed`.

**Artifact categories:** `pipeline_result`, `river_tiles`, `water_manifest`.

### 8. Invalid image configuration

**Purpose:** Confirms unsupported image products fail validation with supported-product guidance.

**Expected result:** Handled failure: validation raises `ValueError`; result code `VALIDATION_ERROR` records the failed run.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `failed` at `2026-06-08T21:43:08.132853+00:00`.

**Recorded codes:** errors `VALIDATION_ERROR`; warnings None.

**Stage outcomes:** `validation: failed`.

**Artifact categories:** `pipeline_result`, `river_tiles`, `water_manifest`.

### 9. Live Sperchios happy-path smoke

**Purpose:** Exercises the real OSM, CDSE, preprocessing, and bundled-model integration.

**Expected result:** `success` or `partial`; at least one tile completes inference.

**Scenario verification:** **Passed**: expected outcome observed.

**Latest verified execution:** `success` at `2026-06-08T21:41:18.205321+00:00`.

**Recorded codes:** errors None; warnings None.

**Stage outcomes:** `validation: success`, `river_tiles: success`, `water_selection: success`, `target_date_images: success`, `feature_preparation: success`, `inference: success`.

**Artifact categories:** `forecast_csv`, `forecast_json`, `inference_plan`, `pipeline_result`, `river_tiles`, `water_manifest`.

## Result Contract

Each run writes `pipeline_result.json` even when execution fails. Expected failure scenarios demonstrate that the pipeline handles invalid or unavailable inputs in a controlled, observable way. The result contains the overall status, stage outcomes, per-tile outcomes, structured warnings and errors, and artifact references. Exact-date target images are supplementary; their absence is a warning rather than an inference failure.
