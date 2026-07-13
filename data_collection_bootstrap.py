from __future__ import annotations

import sys
from pathlib import Path


DATA_COLLECTION_ROOT = Path(__file__).resolve().parent / "data-collection"


def ensure_data_collection_importable() -> Path:
    root = str(DATA_COLLECTION_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    return DATA_COLLECTION_ROOT


ensure_data_collection_importable()
