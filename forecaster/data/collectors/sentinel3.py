from data_collection_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()

from data_collection.collectors.sentinel3 import Sentinel3Collection  # noqa: E402,F401
