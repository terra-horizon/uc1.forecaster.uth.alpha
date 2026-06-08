from __future__ import annotations

import json

import pytest

from forecast import AOIInferenceConfig, AOIInferencePipeline
from tests.scenario_support import (
    EmptyExtractor,
    execute_scenario,
    scenario_dir,
    unavailable_images,
    water_manifest,
)


def test_successful_end_to_end_inference_with_bundled_model():
    pipeline, plan, result, error = execute_scenario("successful_inference", real_model=True)

    assert error is None
    assert result["status"] == "success"
    assert result["stages"]["inference"]["successful_tiles"] == ["tile_0"]
    assert pipeline.result_path.exists()
    assert plan["inference"]["forecast_json"]
    assert plan["inference"]["forecast_csv"]


def test_no_river_detected():
    _pipeline, plan, result, error = execute_scenario("no_river_tiles", extractor=EmptyExtractor)

    assert plan is None
    assert error.code == "NO_RIVER_TILES"
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "NO_RIVER_TILES"
    assert "water_selection" not in result["stages"]


def test_river_tiles_contain_no_water():
    _pipeline, plan, result, error = execute_scenario(
        "no_water_tiles",
        manifest=water_manifest([], ["tile_0"]),
    )

    assert plan is None
    assert error.code == "NO_WATER_TILES"
    assert result["errors"][0]["code"] == "NO_WATER_TILES"
    assert "feature_preparation" not in result["stages"]


def test_no_historical_satellite_data():
    _pipeline, plan, result, error = execute_scenario("no_satellite_data", feature_tiles=[])

    assert plan is None
    assert error.code == "NO_SATELLITE_DATA"
    assert result["tiles"]["tile_0"]["errors"][0]["code"] == "NO_SATELLITE_DATA"
    assert "inference" not in result["stages"]


def test_target_date_images_unavailable_is_warning():
    _pipeline, plan, result, error = execute_scenario(
        "target_images_unavailable",
        image_downloads=unavailable_images,
    )

    assert error is None
    assert plan is not None
    assert result["status"] == "success"
    assert result["warnings"][0]["code"] == "TARGET_IMAGES_UNAVAILABLE"
    assert plan["target_date_images"]["downloads"]["global"]["true_color"]["status"] == "unavailable"


def test_partial_historical_data_runs_valid_tile():
    manifest = water_manifest(["tile_0", "tile_1"])
    _pipeline, plan, result, error = execute_scenario(
        "partial_satellite_data",
        manifest=manifest,
        feature_tiles=["tile_0"],
    )

    assert error is None
    assert plan is not None
    assert result["status"] == "partial"
    assert result["tiles"]["tile_0"]["status"] == "success"
    assert result["tiles"]["tile_1"]["errors"][0]["code"] == "NO_SATELLITE_DATA"


def test_inference_fails_for_every_prepared_tile():
    _pipeline, plan, result, error = execute_scenario(
        "inference_failed",
        inference_failures={"tile_0"},
    )

    assert plan is None
    assert error.code == "INFERENCE_FAILED"
    assert result["errors"][0]["code"] == "INFERENCE_FAILED"
    assert result["tiles"]["tile_0"]["errors"][0]["code"] == "INFERENCE_FAILED"


def test_invalid_image_configuration_writes_failed_result():
    output_root = scenario_dir("invalid_image_configuration")
    pipeline = AOIInferencePipeline(
        AOIInferenceConfig(
            aoi_bbox=[22.43, 38.83, 22.56, 38.89],
            target_date="2026-05-27",
            output_root=output_root,
            run_name="run",
            image_keys=("not_a_product",),
        )
    )

    with pytest.raises(ValueError, match="Supported image keys"):
        pipeline.execute()

    result = json.loads(pipeline.result_path.read_text(encoding="utf-8"))
    assert result["status"] == "failed"
    assert result["errors"][0]["code"] == "VALIDATION_ERROR"
