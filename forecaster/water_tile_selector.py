from data_collection_bootstrap import ensure_data_collection_importable

ensure_data_collection_importable()

from data_collection.water import (  # noqa: E402,F401
    CDSECredentialSet,
    TileConfig,
    WaterTileSelector,
    load_tiles_geojson,
    print_selection_summary,
)
