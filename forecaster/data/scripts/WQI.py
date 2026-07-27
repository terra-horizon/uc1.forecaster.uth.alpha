from collector_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()
from data_collection.scripts.WQI import wqi  # noqa: E402,F401
