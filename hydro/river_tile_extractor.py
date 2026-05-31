from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import geopandas as gpd
import osmnx as ox
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon, box, mapping
from shapely.ops import linemerge


DEFAULT_RIVER_TAGS = {"waterway": ["river"]}


@dataclass(frozen=True)
class RiverTile:
    name: str
    river_id: str
    tile_index: int
    bbox: list[float]
    center_lon: float
    center_lat: float
    geometry: Polygon
    spacing_m: int
    box_size_m: int

    def to_feature(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": self.name,
            "geometry": mapping(self.geometry),
            "properties": {
                "name": self.name,
                "river_id": self.river_id,
                "tile_index": self.tile_index,
                "bbox": self.bbox,
                "center_lon": self.center_lon,
                "center_lat": self.center_lat,
                "spacing_m": self.spacing_m,
                "box_size_m": self.box_size_m,
            },
        }


@dataclass(frozen=True)
class RiverTileExtractorConfig:
    aoi_bbox: list[float]
    river_tags: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RIVER_TAGS))
    source_crs: str = "EPSG:4326"
    projected_crs: str = "EPSG:32634"
    output_crs: str = "EPSG:4326"
    min_length_m: float = 10_000.0
    spacing_m: int = 400
    box_size_m: int = 400
    tile_name_prefix: str = "tile"


class RiverTileExtractor:
    """Extract fixed-size river-centered tiles from an AOI bbox.

    This is the class-based version of the old `Hydro/main.py::generateTiles`
    workflow. It returns tile objects for in-memory pipelines and can also write
    the same GeoJSON FeatureCollection format used by the forecaster water
    selector.
    """

    def __init__(self, config: RiverTileExtractorConfig):
        self.config = config

    def fetch_rivers(self) -> gpd.GeoDataFrame:
        min_lon, min_lat, max_lon, max_lat = self.config.aoi_bbox
        aoi = box(min_lon, min_lat, max_lon, max_lat)
        rivers = ox.features.features_from_polygon(aoi, self.config.river_tags)
        rivers = rivers[["geometry"]].dropna()
        if rivers.empty:
            return rivers

        if rivers.crs is None:
            rivers = rivers.set_crs(self.config.source_crs)
        rivers = rivers.to_crs(self.config.source_crs)
        return self.filter_rivers(rivers)

    def filter_rivers(self, rivers: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
        if rivers.empty:
            return rivers
        projected = rivers.to_crs(self.config.projected_crs).copy()
        projected["length_m"] = projected.geometry.length
        keep_index = projected[projected["length_m"] >= float(self.config.min_length_m)].index
        return rivers.loc[keep_index].copy()

    def extract_tiles(self, rivers: gpd.GeoDataFrame | None = None) -> list[RiverTile]:
        rivers = self.fetch_rivers() if rivers is None else rivers
        if rivers.empty:
            return []
        if rivers.crs is None:
            rivers = rivers.set_crs(self.config.source_crs)

        projected = rivers.to_crs(self.config.projected_crs)
        tiles: list[RiverTile] = []
        global_index = 0
        for river_index, row in projected.iterrows():
            river_id = self._river_id(river_index)
            line = self._to_line(row.geometry)
            river_tiles = self._tiles_for_line(
                line=line,
                river_id=river_id,
                start_index=global_index,
            )
            tiles.extend(river_tiles)
            global_index += len(river_tiles)
        return tiles

    def to_geojson(self, tiles: Iterable[RiverTile]) -> dict[str, Any]:
        tile_list = list(tiles)
        return {
            "type": "FeatureCollection",
            "aoi_bbox": self.config.aoi_bbox,
            "river_tags": self.config.river_tags,
            "projected_crs": self.config.projected_crs,
            "spacing": {
                "value": int(self.config.spacing_m),
                "unit": "meters",
            },
            "box_size": {
                "value": int(self.config.box_size_m),
                "unit": "meters",
            },
            "tile_count": len(tile_list),
            "features": [tile.to_feature() for tile in tile_list],
        }

    def write_geojson(self, tiles: Iterable[RiverTile], output_path: str | Path) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_geojson(tiles), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def extract_to_geojson(self, output_path: str | Path) -> tuple[list[RiverTile], Path]:
        tiles = self.extract_tiles()
        return tiles, self.write_geojson(tiles, output_path)

    def _tiles_for_line(self, line: LineString, river_id: str, start_index: int) -> list[RiverTile]:
        length = float(line.length)
        if length <= 0:
            return []

        n_points = int(length // int(self.config.spacing_m))
        distances = [i * int(self.config.spacing_m) for i in range(n_points + 1)]
        if not distances:
            distances = [length / 2.0]
        points = [line.interpolate(distance) for distance in distances]

        half = int(self.config.box_size_m) / 2.0
        boxes_projected = [
            box(point.x - half, point.y - half, point.x + half, point.y + half)
            for point in points
        ]
        boxes_wgs84 = gpd.GeoSeries(boxes_projected, crs=self.config.projected_crs).to_crs(self.config.output_crs)
        centers_wgs84 = gpd.GeoSeries(points, crs=self.config.projected_crs).to_crs(self.config.output_crs)

        tiles: list[RiverTile] = []
        for local_index, (geometry, center) in enumerate(zip(boxes_wgs84, centers_wgs84, strict=True)):
            min_lon, min_lat, max_lon, max_lat = geometry.bounds
            tile_index = start_index + local_index
            tiles.append(
                RiverTile(
                    name=f"{self.config.tile_name_prefix}_{tile_index}",
                    river_id=river_id,
                    tile_index=tile_index,
                    bbox=[float(min_lon), float(min_lat), float(max_lon), float(max_lat)],
                    center_lon=float(center.x),
                    center_lat=float(center.y),
                    geometry=geometry,
                    spacing_m=int(self.config.spacing_m),
                    box_size_m=int(self.config.box_size_m),
                )
            )
        return tiles

    @staticmethod
    def _river_id(index_value: Any) -> str:
        if isinstance(index_value, tuple) and index_value:
            return str(index_value[-1])
        return str(index_value)

    @staticmethod
    def _to_line(geometry: Any) -> LineString:
        if isinstance(geometry, LineString):
            return geometry
        if isinstance(geometry, MultiLineString):
            merged = linemerge(geometry)
            if isinstance(merged, LineString):
                return merged
            if isinstance(merged, MultiLineString):
                return max(merged.geoms, key=lambda item: item.length)
        if isinstance(geometry, Polygon):
            return LineString(geometry.exterior.coords)
        if isinstance(geometry, MultiPolygon):
            largest = max(geometry.geoms, key=lambda item: item.area)
            return LineString(largest.exterior.coords)
        raise TypeError(f"Unsupported river geometry type: {geometry.geom_type}")

