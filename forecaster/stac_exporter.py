from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


TARGET_VARIABLES = ("CDOM", "Chl_a", "Color", "Cya", "DOC", "Turb", "WQI")

VARIABLES: dict[str, dict[str, Any]] = {
    "CDOM": {
        "long_name": "Colored Dissolved Organic Matter",
        "units": "mg/L",
        "min": 0,
        "max": 100,
    },
    "Chl_a": {
        "long_name": "Chlorophyll-a",
        "units": "ug/L",
        "min": 0,
        "max": 100,
    },
    "Color": {
        "long_name": "Water Color",
        "units": "Pt-Co",
        "min": 0,
        "max": 300,
    },
    "Cya": {
        "long_name": "Cyanobacteria",
        "units": "cells/mL",
        "min": 0,
        "max": 1_000_000,
    },
    "DOC": {
        "long_name": "Dissolved Organic Carbon",
        "units": "mg/L",
        "min": 0,
        "max": 120,
    },
    "Turb": {
        "long_name": "Turbidity",
        "units": "NTU",
        "min": 0,
        "max": 1_000,
    },
    "WQI": {
        "long_name": "Water Quality Index",
        "min": -1,
        "max": 1,
    },
}

FORECAST_EXTENSION = "https://stac-extensions.github.io/forecast/v0.2.0/schema.json"
ORDER_EXTENSION = "https://stac-extensions.github.io/order/v1.1.0/schema.json"
PROCESSING_EXTENSION = "https://stac-extensions.github.io/processing/v1.2.0/schema.json"
ITEM_ASSETS_EXTENSION = "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_date_to_z(value: str) -> str:
    return f"{value[:10]}T00:00:00Z"


def yyyymmdd(value: str) -> str:
    return value[:10].replace("-", "")


def strict_json_value(value: Any) -> Any:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return numeric


def bbox_geometry(bbox: list[float]) -> dict[str, Any]:
    west, south, east, north = [float(item) for item in bbox]
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [west, south],
                [east, south],
                [east, north],
                [west, north],
                [west, south],
            ]
        ],
    }


def tile_feature(record: dict[str, Any]) -> dict[str, Any]:
    tile_id = str(record["name"])
    bbox = [float(item) for item in record["bbox"]]
    return {
        "type": "Feature",
        "id": tile_id,
        "properties": {"tile": tile_id},
        "geometry": bbox_geometry(bbox),
    }


@dataclass(frozen=True)
class StacExportResult:
    catalog_path: Path
    collection_path: Path
    item_path: Path | None
    item_id: str | None


class StacCatalogExporter:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        collection_id: str = "water-pollution",
        stac_base_url: str | None = None,
    ):
        self.output_dir = Path(output_dir)
        self.collection_id = collection_id
        self.stac_base_url = stac_base_url.rstrip("/") if stac_base_url else None

    def export(
        self,
        *,
        aoi_bbox: list[float],
        tile_records: list[dict[str, Any]],
        history_records: list[dict[str, Any]],
        forecast_rows: list[dict[str, Any]],
        anchor_date: str | None = None,
        horizon_days: int = 15,
        processing_metadata: dict[str, Any] | None = None,
    ) -> StacExportResult:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        geometries_path = self.output_dir / "geometries.json"
        catalog_path = self.output_dir / "catalog.json"
        collection_path = self.output_dir / "collection.json"

        tile_ids = sorted({str(record["name"]) for record in tile_records})
        geometries = {
            "type": "FeatureCollection",
            "features": [tile_feature(record) for record in sorted(tile_records, key=lambda item: str(item["name"]))],
        }
        geometries_path.write_text(json.dumps(geometries, indent=2, allow_nan=False), encoding="utf-8")

        all_dates = sorted(
            {
                str(record.get("observation_date") or record.get("forecast_date"))[:10]
                for record in [*history_records, *forecast_rows]
                if record.get("observation_date") or record.get("forecast_date")
            }
        )
        temporal_start = iso_date_to_z(all_dates[0]) if all_dates else None
        temporal_end = iso_date_to_z(all_dates[-1]) if all_dates else None

        self._write_catalog(catalog_path)
        self._write_collection(collection_path, aoi_bbox, tile_ids, temporal_start, temporal_end)

        item_path: Path | None = None
        item_id: str | None = None
        if anchor_date and forecast_rows:
            item_id = self._item_id(all_dates[0], all_dates[-1])
            item_dir = self.output_dir / "items" / item_id
            item_dir.mkdir(parents=True, exist_ok=True)
            item_path = item_dir / "item.json"
            self._write_item(
                item_dir=item_dir,
                item_id=item_id,
                aoi_bbox=aoi_bbox,
                tile_ids=tile_ids,
                history_records=history_records,
                forecast_rows=forecast_rows,
                start_date=all_dates[0],
                end_date=all_dates[-1],
                anchor_date=anchor_date,
                horizon_days=horizon_days,
                processing_metadata=processing_metadata or {},
            )

        self.validate(collection_path=collection_path, geometries_path=geometries_path, item_path=item_path)
        return StacExportResult(
            catalog_path=catalog_path,
            collection_path=collection_path,
            item_path=item_path,
            item_id=item_id,
        )

    def _write_catalog(self, path: Path) -> None:
        payload = {
            "type": "Catalog",
            "id": "terra-water-pollution",
            "stac_version": "1.1.0",
            "description": "Local STAC catalog for TERRA water pollution scheduled pipeline outputs.",
            "links": [
                {"rel": "self", "href": self._href(path), "type": "application/json"},
                {"rel": "child", "href": self._href(self.output_dir / "collection.json"), "type": "application/json"},
            ],
        }
        path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    def _write_collection(
        self,
        path: Path,
        aoi_bbox: list[float],
        tile_ids: list[str],
        temporal_start: str | None,
        temporal_end: str | None,
    ) -> None:
        item_assets = {
            "overview": {
                "title": "Overview",
                "description": "Per-tile overview snapshot for quick rendering.",
                "type": "application/json",
                "roles": ["data"],
            }
        }
        for tile_id in tile_ids:
            item_assets[tile_id] = {
                "title": tile_id.replace("_", " ").title(),
                "description": f"Water quality time series for {tile_id}.",
                "type": "application/json",
                "roles": ["data"],
            }

        payload = {
            "id": self.collection_id,
            "type": "Collection",
            "stac_version": "1.1.0",
            "stac_extensions": [ITEM_ASSETS_EXTENSION],
            "title": "Water Pollution",
            "description": "TERRA water pollution scheduled observations and forecasts.",
            "license": "proprietary",
            "keywords": ["TERRA", "water quality", "forecast", "Sentinel-2"],
            "providers": [
                {
                    "name": "TERRA",
                    "description": "Producer and processor of water-quality scheduled pipeline outputs.",
                    "roles": ["producer", "processor"],
                    "url": "https://terra-horizon.eu/",
                }
            ],
            "extent": {
                "spatial": {"bbox": [[float(item) for item in aoi_bbox]]},
                "temporal": {"interval": [[temporal_start, temporal_end]]},
            },
            "summaries": {
                "variables": list(TARGET_VARIABLES),
                "tiles": tile_ids,
                "forecast:duration": ["P15D"],
            },
            "links": [
                {"rel": "root", "href": self._href(self.output_dir / "catalog.json"), "type": "application/json"},
                {"rel": "self", "href": self._href(path), "type": "application/json"},
                {"rel": "items", "href": self._href(self.output_dir / "items"), "type": "application/json"},
            ],
            "assets": {
                "geometries": {
                    "href": self._href(self.output_dir / "geometries.json"),
                    "type": "application/geo+json",
                    "title": "Tile geometries",
                    "roles": ["metadata"],
                }
            },
            "item_assets": item_assets,
        }
        path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    def _write_item(
        self,
        *,
        item_dir: Path,
        item_id: str,
        aoi_bbox: list[float],
        tile_ids: list[str],
        history_records: list[dict[str, Any]],
        forecast_rows: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        anchor_date: str,
        horizon_days: int,
        processing_metadata: dict[str, Any],
    ) -> None:
        tiles_dir = item_dir / "tiles"
        tiles_dir.mkdir(parents=True, exist_ok=True)
        overview_path = item_dir / "overview.json"
        item_path = item_dir / "item.json"

        assets = {
            "overview": {
                "href": self._href(overview_path),
                "title": "Overview",
                "description": "Per-tile WQI overview snapshot.",
                "type": "application/json",
                "roles": ["data"],
            }
        }
        for tile_id in tile_ids:
            tile_path = tiles_dir / f"{tile_id}.json"
            self._write_tile_json(tile_path, tile_id, history_records, forecast_rows, start_date, end_date)
            assets[tile_id] = {
                "href": self._href(tile_path),
                "title": tile_id.replace("_", " ").title(),
                "description": f"Water quality time series for {tile_id}.",
                "type": "application/json",
                "roles": ["data"],
            }

        self._write_overview(overview_path, tile_ids, history_records, forecast_rows, anchor_date)
        properties = {
            "datetime": iso_date_to_z(end_date),
            "start_datetime": iso_date_to_z(start_date),
            "end_datetime": iso_date_to_z(end_date),
            "forecast:reference_datetime": iso_date_to_z(anchor_date),
            "forecast:duration": f"P{int(horizon_days)}D",
            "title": f"Water Quality Processing {start_date}/{end_date}",
            "order:status": "succeeded",
            "processing:lineage": "Digital twin water-quality scheduled pipeline, observations plus forecast.",
            "processing:datetime": utc_now(),
            "processing:software": {"terra-uc1-forecaster": str(processing_metadata.get("version", "scheduled"))},
        }
        for key in ("processing:level", "processing:facility", "processing:version"):
            if processing_metadata.get(key):
                properties[key] = processing_metadata[key]

        payload = {
            "type": "Feature",
            "stac_version": "1.1.0",
            "stac_extensions": [ORDER_EXTENSION, PROCESSING_EXTENSION, FORECAST_EXTENSION],
            "id": item_id,
            "collection": self.collection_id,
            "geometry": bbox_geometry(aoi_bbox),
            "bbox": [float(item) for item in aoi_bbox],
            "properties": properties,
            "links": [
                {"rel": "root", "href": self._href(self.output_dir / "catalog.json"), "type": "application/json"},
                {"rel": "collection", "href": self._href(self.output_dir / "collection.json"), "type": "application/json"},
                {"rel": "self", "href": self._href(item_path), "type": "application/json"},
            ],
            "assets": assets,
        }
        item_path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    def _write_tile_json(
        self,
        path: Path,
        tile_id: str,
        history_records: list[dict[str, Any]],
        forecast_rows: list[dict[str, Any]],
        start_date: str,
        end_date: str,
    ) -> None:
        rows: dict[str, dict[str, Any]] = {}
        for record in history_records:
            if str(record.get("tile_id")) != tile_id:
                continue
            rows[str(record["observation_date"])[:10]] = {
                column: strict_json_value(record.get(column))
                for column in TARGET_VARIABLES
            }
        for record in forecast_rows:
            if str(record.get("tile_id")) != tile_id:
                continue
            rows[str(record["forecast_date"])[:10]] = {
                column: strict_json_value(record.get(column))
                for column in TARGET_VARIABLES
            }

        payload = {
            "geometry_id": tile_id,
            "start": iso_date_to_z(start_date),
            "end": iso_date_to_z(end_date),
            "variables": VARIABLES,
            "data": [
                {
                    "date": iso_date_to_z(row_date),
                    "values": {column: rows[row_date].get(column) for column in TARGET_VARIABLES},
                }
                for row_date in sorted(rows)
            ],
        }
        path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    def _write_overview(
        self,
        path: Path,
        tile_ids: list[str],
        history_records: list[dict[str, Any]],
        forecast_rows: list[dict[str, Any]],
        anchor_date: str,
    ) -> None:
        by_tile: dict[str, Any] = {}
        for tile_id in tile_ids:
            candidates = [
                record
                for record in history_records
                if str(record.get("tile_id")) == tile_id and str(record.get("observation_date"))[:10] <= anchor_date
            ]
            candidates.sort(key=lambda record: str(record.get("observation_date")))
            value = candidates[-1].get("WQI") if candidates else None
            if value is None:
                forecast_candidates = [
                    record
                    for record in forecast_rows
                    if str(record.get("tile_id")) == tile_id and str(record.get("forecast_date"))[:10] == anchor_date
                ]
                value = forecast_candidates[-1].get("WQI") if forecast_candidates else None
            by_tile[tile_id] = {"WQI": strict_json_value(value)}

        payload = {"date": iso_date_to_z(anchor_date), "tiles": by_tile}
        path.write_text(json.dumps(payload, indent=2, allow_nan=False), encoding="utf-8")

    def validate(self, *, collection_path: Path, geometries_path: Path, item_path: Path | None = None) -> None:
        collection = json.loads(collection_path.read_text(encoding="utf-8"))
        geometries = json.loads(geometries_path.read_text(encoding="utf-8"))
        geometry_ids = {str(feature["id"]) for feature in geometries.get("features", [])}
        asset_ids = set(collection.get("item_assets", {})) - {"overview"}
        if geometry_ids != asset_ids:
            raise ValueError("Collection item_assets tile keys must match geometries.json ids.")

        if not item_path:
            return

        item = json.loads(item_path.read_text(encoding="utf-8"))
        allowed_assets = set(collection.get("item_assets", {}))
        item_assets = set(item.get("assets", {}))
        if not item_assets.issubset(allowed_assets):
            raise ValueError("Item assets must be declared in Collection item_assets.")
        if "overview" not in item_assets:
            raise ValueError("Item must include an overview asset.")

        item_dir = item_path.parent
        overview = json.loads((item_dir / "overview.json").read_text(encoding="utf-8"))
        overview_tiles = set(overview.get("tiles", {}))
        tile_assets = item_assets - {"overview"}
        if overview_tiles != tile_assets:
            raise ValueError("overview.json tile keys must match Item tile assets.")

        for tile_id in tile_assets:
            tile_payload = json.loads((item_dir / "tiles" / f"{tile_id}.json").read_text(encoding="utf-8"))
            if tile_payload.get("geometry_id") != tile_id:
                raise ValueError(f"{tile_id}.json geometry_id does not match its asset key.")
            dates = [row.get("date") for row in tile_payload.get("data", [])]
            if dates != sorted(dates):
                raise ValueError(f"{tile_id}.json records must be sorted chronologically.")
            declared = set(tile_payload.get("variables", {}))
            for row in tile_payload.get("data", []):
                if set(row.get("values", {})) - declared:
                    raise ValueError(f"{tile_id}.json contains values not declared in variables.")

    def _href(self, path: Path) -> str:
        if self.stac_base_url:
            relative = path.resolve().relative_to(self.output_dir.resolve()).as_posix()
            return f"{self.stac_base_url}/{relative}"
        return path.relative_to(path.parent if path.name == "catalog.json" else self.output_dir).as_posix()

    def _item_id(self, start_date: str, end_date: str) -> str:
        return f"{self.collection_id}-processing-{yyyymmdd(start_date)}-{yyyymmdd(end_date)}"
