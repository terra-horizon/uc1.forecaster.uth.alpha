"""Canonical AOI identity and definition fingerprint helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def build_aoi_definition(
    *,
    aoi_id: str,
    bbox: list[float],
    projected_crs: str,
    spacing_m: int,
    box_size_m: int,
    min_river_length_m: float,
) -> dict[str, Any]:
    """Return a stable AOI definition and its canonical fingerprint."""
    definition = {
        "schema_version": "1.0.0",
        "aoi_id": str(aoi_id),
        "bbox": [float(value) for value in bbox],
        "crs": str(projected_crs),
        "tiling": {
            "spacing_m": int(spacing_m),
            "box_size_m": int(box_size_m),
            "min_river_length_m": float(min_river_length_m),
        },
    }
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    definition["aoi_definition_hash"] = hashlib.sha256(canonical).hexdigest()
    return definition
