from __future__ import annotations

import json
import os

import pytest

from forecast import AOIInferenceConfig, AOIInferencePipeline
from tests.scenario_support import scenario_dir


@pytest.mark.live
def test_live_sperchios_happy_path():
    if os.getenv("RUN_LIVE_PIPELINE_TESTS") != "1":
        pytest.skip("Set RUN_LIVE_PIPELINE_TESTS=1 to run live OSM/CDSE integration.")

    pipeline = AOIInferencePipeline(
        AOIInferenceConfig(
            aoi_bbox=[22.433493, 38.837552, 22.569555, 38.894223],
            target_date="2026-05-27",
            output_root=scenario_dir("live_happy_path"),
            run_name="run",
            plot=False,
        )
    )
    pipeline.execute()

    result = json.loads(pipeline.result_path.read_text(encoding="utf-8"))
    assert result["status"] in {"success", "partial"}
    assert result["stages"]["inference"]["successful_tiles"]
