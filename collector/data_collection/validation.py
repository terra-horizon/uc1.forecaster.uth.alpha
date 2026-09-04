from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCHEMA_ROOT = Path(__file__).resolve().parent / "schemas"


def validate_run(run_dir: Path) -> dict[str, Any]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        return {"valid": False, "errors": [f"Install the collector runtime dependencies to validate schemas: {exc}"]}

    checks = [
        (run_dir / "history" / "global_history.json", "global-history.schema.json"),
        (run_dir / "collection" / "state.json", "collection-state.schema.json"),
        (run_dir / "collection" / "collection_run_result.json", "collection-result.schema.json"),
        (run_dir / "tiles" / "river_tiles.geojson", "river-tiles.schema.json"),
    ]
    errors: list[str] = []
    checked: list[str] = []
    for data_path, schema_name in checks:
        if not data_path.exists():
            errors.append(f"Missing required file: {data_path}")
            continue
        value = json.loads(data_path.read_text(encoding="utf-8"))
        if schema_name == "global-history.schema.json":
            if not isinstance(value, list):
                errors.append(f"{data_path}: history must be a JSON array")
            else:
                state_path = run_dir / "collection" / "state.json"
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                record_schema = ("sentinel3-history-record.schema.json"
                                 if state.get("sensor") == "sentinel3"
                                 else "history-record.schema.json")
                schema = json.loads((SCHEMA_ROOT / record_schema).read_text(encoding="utf-8"))
                for index, record in enumerate(value):
                    for error in Draft202012Validator(schema).iter_errors(record):
                        location = "/".join(str(part) for part in error.absolute_path)
                        errors.append(f"{data_path}:{index}/{location}: {error.message}")
        else:
            schema = json.loads((SCHEMA_ROOT / schema_name).read_text(encoding="utf-8"))
            for error in Draft202012Validator(schema).iter_errors(value):
                location = "/".join(str(part) for part in error.absolute_path)
                errors.append(f"{data_path}:{location}: {error.message}")
        checked.append(str(data_path))
    return {"valid": not errors, "checked": checked, "errors": errors}
