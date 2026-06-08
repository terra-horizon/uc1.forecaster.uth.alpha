from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_ROOT = REPO_ROOT / "tests" / "results"
OUTPUT_PATH = REPO_ROOT / "docs" / "docs" / "pipeline-test-scenarios.md"

SCENARIOS = [
    {
        "directory": "successful_inference",
        "title": "Successful end-to-end inference",
        "purpose": "Exercises river extraction, water selection, historical features, and the bundled model.",
        "expected": "`success`; forecast JSON and CSV are created.",
    },
    {
        "directory": "no_river_tiles",
        "title": "No river detected",
        "purpose": "Confirms the pipeline stops before water selection when the AOI has no qualifying river.",
        "expected": "`failed`; error `NO_RIVER_TILES`; inference is not attempted.",
    },
    {
        "directory": "no_water_tiles",
        "title": "River tiles contain no water",
        "purpose": "Confirms river tiles rejected by water screening do not proceed to feature collection.",
        "expected": "`failed`; error `NO_WATER_TILES`; inference is not attempted.",
    },
    {
        "directory": "no_satellite_data",
        "title": "No historical satellite data",
        "purpose": "Confirms selected water tiles without sufficient historical features cannot run inference.",
        "expected": "`failed`; aggregate and per-tile `NO_SATELLITE_DATA` errors.",
    },
    {
        "directory": "target_images_unavailable",
        "title": "Target-date images unavailable",
        "purpose": "Confirms exact-date preview image absence does not prevent inference.",
        "expected": "`success`; warning `TARGET_IMAGES_UNAVAILABLE`; unavailable image metadata is retained.",
    },
    {
        "directory": "partial_satellite_data",
        "title": "Partial historical-data availability",
        "purpose": "Confirms valid tiles run while tiles without historical data report explicit errors.",
        "expected": "`partial`; valid tiles forecast; invalid tiles report `NO_SATELLITE_DATA`.",
    },
    {
        "directory": "inference_failed",
        "title": "Inference failure",
        "purpose": "Confirms a run fails when every prepared tile fails model inference.",
        "expected": "`failed`; aggregate and per-tile `INFERENCE_FAILED` errors.",
    },
    {
        "directory": "invalid_image_configuration",
        "title": "Invalid image configuration",
        "purpose": "Confirms unsupported image products fail validation with supported-product guidance.",
        "expected": "Validation raises `ValueError`; result code `VALIDATION_ERROR` records the failed run.",
    },
    {
        "directory": "live_happy_path",
        "title": "Live Sperchios happy-path smoke",
        "purpose": "Exercises the real OSM, CDSE, preprocessing, and bundled-model integration.",
        "expected": "`success` or `partial`; at least one tile completes inference.",
    },
]


def load_result(directory: str) -> dict | None:
    result_path = RESULTS_ROOT / directory / "latest" / "run" / "pipeline_result.json"
    if not result_path.exists():
        return None
    return json.loads(result_path.read_text(encoding="utf-8"))


def codes(items: list[dict]) -> str:
    values = sorted({str(item.get("code")) for item in items if item.get("code")})
    return ", ".join(f"`{value}`" for value in values) if values else "None"


def artifact_names(result: dict) -> str:
    names = []
    for key, value in result.get("artifacts", {}).items():
        if value:
            names.append(f"`{key}`")
    return ", ".join(sorted(names)) if names else "None"


def render() -> str:
    lines = [
        "# Pipeline Test Scenarios",
        "",
        "This page defines the automated pipeline scenarios, their expected results, and the latest sanitized verified-run snapshot. Raw generated artifacts remain under ignored `tests/results/` folders.",
        "",
        "## Execution Commands",
        "",
        "```bash",
        "python3 -m pytest -q",
        "RUN_LIVE_PIPELINE_TESTS=1 python3 -m pytest -m live -q",
        "python3 scripts/generate-scenario-report.py",
        "```",
        "",
        "## Latest Verified Summary",
        "",
        "| Scenario | Expected | Latest status | Errors | Warnings |",
        "| --- | --- | --- | --- | --- |",
    ]
    loaded = {}
    for scenario in SCENARIOS:
        result = load_result(scenario["directory"])
        loaded[scenario["directory"]] = result
        status = f"`{result['status']}`" if result else "Not executed"
        errors = codes(result.get("errors", [])) if result else "N/A"
        warnings = codes(result.get("warnings", [])) if result else "N/A"
        lines.append(f"| {scenario['title']} | {scenario['expected']} | {status} | {errors} | {warnings} |")

    lines.extend(["", "## Scenario Details", ""])
    for index, scenario in enumerate(SCENARIOS, start=1):
        result = loaded[scenario["directory"]]
        lines.extend(
            [
                f"### {index}. {scenario['title']}",
                "",
                f"**Purpose:** {scenario['purpose']}",
                "",
                f"**Expected result:** {scenario['expected']}",
                "",
            ]
        )
        if result:
            stages = ", ".join(
                f"`{name}: {payload.get('status', 'unknown')}`"
                for name, payload in result.get("stages", {}).items()
            ) or "None"
            lines.extend(
                [
                    f"**Latest verified execution:** `{result.get('status', 'unknown')}` at `{result.get('completed_at', 'unknown')}`.",
                    "",
                    f"**Recorded codes:** errors {codes(result.get('errors', []))}; warnings {codes(result.get('warnings', []))}.",
                    "",
                    f"**Stage outcomes:** {stages}.",
                    "",
                    f"**Artifact categories:** {artifact_names(result)}.",
                    "",
                ]
            )
        else:
            lines.extend(["**Latest verified execution:** Not executed in the current snapshot.", ""])

    lines.extend(
        [
            "## Result Contract",
            "",
            "Each run writes `pipeline_result.json` even when execution fails. The result contains the overall status, stage outcomes, per-tile outcomes, structured warnings and errors, and artifact references. Exact-date target images are supplementary; their absence is a warning rather than an inference failure.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    OUTPUT_PATH.write_text(render(), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
