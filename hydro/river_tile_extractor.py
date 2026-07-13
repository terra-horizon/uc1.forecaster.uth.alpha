from data_collection_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()

from data_collection.river_tiles import (  # noqa: E402,F401
    DEFAULT_RIVER_TAGS,
    RiverTile,
    RiverTileExtractor,
    RiverTileExtractorConfig,
)
