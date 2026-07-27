from __future__ import annotations

import sys
from pathlib import Path


COLLECTOR_ROOT = Path(__file__).resolve().parent / "collector"


def ensure_data_collection_importable() -> Path:
    root = str(COLLECTOR_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return COLLECTOR_ROOT


ensure_data_collection_importable()
