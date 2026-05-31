from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np
import requests


FORECASTER_DIR = Path(__file__).resolve().parent
REPO_ROOT = FORECASTER_DIR.parent
DEFAULT_CACHE_PATH = FORECASTER_DIR / "data" / "water_selection" / "water_selection.json"

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
STATS_URL = "https://sh.dataspace.copernicus.eu/api/v1/statistics"
SWBM_SOURCE_URL = "https://custom-scripts.sentinel-hub.com/sentinel-2/simple_water_bodies_mapping-swbm/"

SWBM_EVALSCRIPT = """
//VERSION=3
var MNDWI_thr = 0.1;
var NDWI_thr = 0.2;
var SWI_thr = 0.03;

function setup() {
  return {
    input: [{
      bands: ["B02", "B03", "B04", "B05", "B08", "B11", "SCL", "dataMask"]
    }],
    output: [
      { id: "eobrowserStats", bands: 2, sampleType: "FLOAT32" },
      { id: "dataMask", bands: 1 }
    ]
  };
}

function isCloud(scl) {
  return [8, 9].includes(scl);
}

function evaluatePixel(p) {
  let mndwi = index(p.B03, p.B11);
  let ndwi = index(p.B03, p.B08);
  let swi = index(p.B05, p.B11);
  let cloud = isCloud(p.SCL);
  let water = (!cloud && (mndwi > MNDWI_thr || ndwi > NDWI_thr || swi > SWI_thr)) ? 1 : 0;

  return {
    eobrowserStats: [water, cloud ? 1 : 0],
    dataMask: [p.dataMask]
  };
}
"""


@dataclass(frozen=True)
class TileConfig:
    name: str
    bbox: list[float]
    size: int | float | None = None


@dataclass(frozen=True)
class CDSECredentialSet:
    label: str
    client_id: str
    client_secret: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _repo_path(path: Path) -> str:
    try:
        return "/" + str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def _polygon_to_bbox(coords: list) -> list[float]:
    ring = coords[0]
    lons = [point[0] for point in ring]
    lats = [point[1] for point in ring]
    return [min(lons), min(lats), max(lons), max(lats)]


def load_tiles_geojson(path: str | Path) -> list[TileConfig]:
    geojson_path = Path(path)
    with geojson_path.open(encoding="utf-8") as fh:
        geojson = json.load(fh)

    box_size = geojson.get("box_size", {})
    box_size_value = box_size.get("value")
    tiles: list[TileConfig] = []
    for index, feature in enumerate(geojson.get("features", [])):
        geometry = feature.get("geometry", {})
        if geometry.get("type") != "Polygon":
            continue
        name = (
            feature.get("properties", {}).get("name")
            or feature.get("id")
            or f"tile_{index}"
        )
        tiles.append(
            TileConfig(
                name=str(name),
                bbox=_polygon_to_bbox(geometry.get("coordinates", [])),
                size=box_size_value,
            )
        )
    return tiles


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


class WaterTileSelector:
    def __init__(
        self,
        geojson_path: str | Path,
        cache_path: str | Path = DEFAULT_CACHE_PATH,
        water_check_interval: tuple[str, str] = ("2024-01-01", "2026-01-01"),
        reference_last_n: int = 10,
        threshold: str | float = "auto",
        min_auto_threshold_pct: float = 0.5,
        reference_quantile: float = 10.0,
        threshold_factor: float = 0.75,
        scene_quantile: float = 75.0,
        max_cloud_pct: float = 30.0,
        max_cloud_coverage: int = 30,
        refresh: bool = False,
    ):
        self.geojson_path = Path(geojson_path)
        self.cache_path = Path(cache_path)
        self.water_check_interval = water_check_interval
        self.reference_last_n = int(reference_last_n)
        self.threshold = threshold
        self.min_auto_threshold_pct = float(min_auto_threshold_pct)
        self.reference_quantile = float(reference_quantile)
        self.threshold_factor = float(threshold_factor)
        self.scene_quantile = float(scene_quantile)
        self.max_cloud_pct = float(max_cloud_pct)
        self.max_cloud_coverage = int(max_cloud_coverage)
        self.refresh = bool(refresh)
        self._credential_sets_cache: list[CDSECredentialSet] | None = None
        self._credential_index = 0
        self._access_tokens: dict[int, str] = {}
        self._reported_no_backup_credentials = False

    @property
    def params_key(self) -> dict:
        return {
            "geojson_path": str(self.geojson_path),
            "water_check_interval": list(self.water_check_interval),
            "reference_last_n": self.reference_last_n,
            "threshold": str(self.threshold),
            "min_auto_threshold_pct": self.min_auto_threshold_pct,
            "reference_quantile": self.reference_quantile,
            "threshold_factor": self.threshold_factor,
            "scene_quantile": self.scene_quantile,
            "max_cloud_pct": self.max_cloud_pct,
            "max_cloud_coverage": self.max_cloud_coverage,
            "swbm_source_url": SWBM_SOURCE_URL,
        }

    @property
    def score_params_key(self) -> dict:
        return {
            "geojson_path": str(self.geojson_path),
            "water_check_interval": list(self.water_check_interval),
            "scene_quantile": self.scene_quantile,
            "max_cloud_pct": self.max_cloud_pct,
            "max_cloud_coverage": self.max_cloud_coverage,
            "swbm_source_url": SWBM_SOURCE_URL,
        }

    def select_tiles(self) -> dict:
        cached = _read_json(self.cache_path)
        if (
            cached
            and not self.refresh
            and cached.get("params") == self.params_key
            and cached.get("tiles")
        ):
            print(f"[WaterTileSelector] Using cached water selection: {self.cache_path}")
            return cached

        tiles = load_tiles_geojson(self.geojson_path)
        if not tiles:
            raise ValueError(f"No polygon tiles found in {self.geojson_path}")

        reference_tiles = tiles[-self.reference_last_n :] if self.reference_last_n else []
        reference_names = {tile.name for tile in reference_tiles}

        if cached and not self.refresh and self._can_reuse_cached_scores(cached, tiles):
            print(
                "[WaterTileSelector] Reusing cached SWBM water scores and "
                "recomputing tile selection for the requested threshold mode."
            )
            tile_records = deepcopy(cached["tiles"])
            for record in tile_records:
                record["is_reference_tile"] = record["name"] in reference_names
            manifest = self._build_manifest(tile_records, reference_names)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            print(f"[WaterTileSelector] Saved water selection manifest: {self.cache_path}")
            return manifest

        tile_records = []

        print(
            f"[WaterTileSelector] Screening {len(tiles)} tiles with SWBM "
            f"({self.water_check_interval[0]} to {self.water_check_interval[1]})..."
        )
        for index, tile in enumerate(tiles, start=1):
            scene_stats = self._query_tile(tile)
            score = self._score_tile(scene_stats)
            tile_records.append(
                {
                    "name": tile.name,
                    "bbox": tile.bbox,
                    "size": tile.size,
                    "water_score_pct": score,
                    "valid_scene_count": len(
                        [
                            scene
                            for scene in scene_stats
                            if scene["valid_pixels"] > 0 and scene["cloud_pct"] <= self.max_cloud_pct
                        ]
                    ),
                    "scene_count": len(scene_stats),
                    "is_reference_tile": tile.name in reference_names,
                    "scenes": scene_stats,
                }
            )
            print(f"  {index:>3}/{len(tiles)} {tile.name:<12} water_score={score:6.2f}%")

        manifest = self._build_manifest(tile_records, reference_names)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"[WaterTileSelector] Saved water selection manifest: {self.cache_path}")
        return manifest

    def _can_reuse_cached_scores(self, cached: dict, tiles: list[TileConfig]) -> bool:
        cached_tiles = cached.get("tiles") or []
        if [record.get("name") for record in cached_tiles] != [tile.name for tile in tiles]:
            return False

        params = cached.get("params") or {}
        comparable = {
            key: params.get(key)
            for key in (
                "geojson_path",
                "water_check_interval",
                "scene_quantile",
                "max_cloud_pct",
                "max_cloud_coverage",
                "swbm_source_url",
            )
        }
        return comparable == self.score_params_key

    def _threshold_mode(self) -> str:
        raw = str(self.threshold).strip().lower()
        if raw in {"auto", "distribution"}:
            return raw
        return "manual"

    def _build_manifest(self, tile_records: list[dict], reference_names: set[str]) -> dict:
        threshold_pct = self._resolve_threshold(tile_records, reference_names)
        selected_tiles = [
            record["name"]
            for record in tile_records
            if record["water_score_pct"] >= threshold_pct
        ]
        rejected_tiles = [
            record["name"]
            for record in tile_records
            if record["water_score_pct"] < threshold_pct
        ]

        for record in tile_records:
            record["selected"] = record["name"] in selected_tiles
            record["threshold_pct"] = threshold_pct

        threshold_payload = {
            "mode": self._threshold_mode(),
            "value_pct": threshold_pct,
        }
        if self._threshold_mode() == "auto":
            threshold_payload.update(
                {
                    "reference_tile_names": sorted(reference_names),
                    "reference_quantile": self.reference_quantile,
                    "threshold_factor": self.threshold_factor,
                }
            )
        elif self._threshold_mode() == "distribution":
            threshold_payload.update(
                {
                    "method": "otsu_log1p_water_score_pct",
                    "description": (
                        "Otsu two-class split over log1p(water_score_pct), "
                        "floored by min_auto_threshold_pct."
                    ),
                }
            )

        manifest = {
            "created_at": _utc_now(),
            "geojson_path": str(self.geojson_path),
            "geojson_repo_path": _repo_path(self.geojson_path),
            "params": self.params_key,
            "threshold": threshold_payload,
            "selected_tiles": selected_tiles,
            "rejected_tiles": rejected_tiles,
            "tiles": tile_records,
        }
        return manifest

    def _resolve_threshold(self, records: list[dict], reference_names: set[str]) -> float:
        mode = self._threshold_mode()
        if mode == "manual":
            return float(self.threshold)
        if mode == "distribution":
            return self._distribution_threshold(records)

        reference_scores = [
            record["water_score_pct"]
            for record in records
            if record["name"] in reference_names
        ]
        if not reference_scores or max(reference_scores) <= 0:
            return self.min_auto_threshold_pct

        reference_floor = float(np.percentile(reference_scores, self.reference_quantile))
        return max(self.min_auto_threshold_pct, self.threshold_factor * reference_floor)

    def _distribution_threshold(self, records: list[dict]) -> float:
        scores = np.array(
            [
                max(0.0, float(record.get("water_score_pct") or 0.0))
                for record in records
                if np.isfinite(float(record.get("water_score_pct") or 0.0))
            ],
            dtype=float,
        )
        if scores.size < 2 or float(np.max(scores)) <= 0.0:
            return self.min_auto_threshold_pct

        transformed = np.log1p(scores)
        unique_values = np.sort(np.unique(transformed))
        if unique_values.size < 2:
            return max(self.min_auto_threshold_pct, float(np.expm1(unique_values[0])))

        best_score = -1.0
        best_threshold = float(np.median(unique_values))
        for left_edge, right_edge in zip(unique_values[:-1], unique_values[1:]):
            candidate = float((left_edge + right_edge) / 2.0)
            left = transformed[transformed <= candidate]
            right = transformed[transformed > candidate]
            if left.size == 0 or right.size == 0:
                continue
            left_weight = left.size / transformed.size
            right_weight = right.size / transformed.size
            between_class_variance = (
                left_weight * right_weight * float((left.mean() - right.mean()) ** 2)
            )
            if between_class_variance > best_score:
                best_score = between_class_variance
                best_threshold = candidate

        return max(self.min_auto_threshold_pct, float(np.expm1(best_threshold)))

    def _score_tile(self, scenes: Iterable[dict]) -> float:
        valid_water_scores = [
            scene["water_pct"]
            for scene in scenes
            if scene["valid_pixels"] > 0 and scene["cloud_pct"] <= self.max_cloud_pct
        ]
        if not valid_water_scores:
            return 0.0
        return float(np.percentile(valid_water_scores, self.scene_quantile))

    def _credential_sets(self) -> list[CDSECredentialSet]:
        if self._credential_sets_cache is not None:
            return self._credential_sets_cache

        from config import CDSE_Credentials

        credentials_payload = CDSE_Credentials.get_credential_sets()
        credentials: list[CDSECredentialSet] = []
        seen_client_ids: set[str] = set()
        for payload in credentials_payload:
            label = payload.get("label")
            client_id = payload.get("client_id")
            client_secret = payload.get("client_secret")
            if not client_id or not client_secret or client_id in seen_client_ids:
                continue
            credentials.append(
                CDSECredentialSet(
                    label=str(label or "credentials"),
                    client_id=client_id,
                    client_secret=client_secret,
                )
            )
            seen_client_ids.add(client_id)

        if not credentials:
            raise RuntimeError(
                "No CDSE credentials configured. Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET. "
                "Optional backup credentials can be set with CDSE_BACKUP_CLIENT_ID and "
                "CDSE_BACKUP_CLIENT_SECRET."
            )

        self._credential_sets_cache = credentials
        print(
            "[WaterTileSelector] Configured CDSE credential sets: "
            + ", ".join(
                f"{credential.label} ({self._redact_client_id(credential.client_id)})"
                for credential in credentials
            )
        )
        return credentials

    def _current_credential(self) -> CDSECredentialSet:
        credentials = self._credential_sets()
        self._credential_index = min(self._credential_index, len(credentials) - 1)
        return credentials[self._credential_index]

    @staticmethod
    def _redact_client_id(client_id: str) -> str:
        if len(client_id) <= 10:
            return "***"
        return f"{client_id[:8]}...{client_id[-4:]}"

    def _get_access_token(self) -> str:
        if self._credential_index in self._access_tokens:
            return self._access_tokens[self._credential_index]

        credentials = self._current_credential()
        response = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        self._access_tokens[self._credential_index] = token
        print(
            f"[WaterTileSelector] Using {credentials.label} CDSE credentials "
            f"({self._redact_client_id(credentials.client_id)})."
        )
        return token

    def _switch_to_next_credentials(self, status_code: int, tile_name: str) -> bool:
        credentials = self._credential_sets()
        if self._credential_index + 1 >= len(credentials):
            if not self._reported_no_backup_credentials:
                print(
                    f"[WaterTileSelector] Received {status_code} for {tile_name}, but no unused "
                    "backup CDSE credentials are configured. Set CDSE_BACKUP_CLIENT_ID and "
                    "CDSE_BACKUP_CLIENT_SECRET, or add them to an ignored .env file."
                )
                self._reported_no_backup_credentials = True
            return False

        current = credentials[self._credential_index]
        self._credential_index += 1
        next_credentials = credentials[self._credential_index]
        print(
            f"[WaterTileSelector] Received {status_code} for {tile_name} using "
            f"{current.label} credentials; switching to {next_credentials.label} "
            f"({self._redact_client_id(next_credentials.client_id)})."
        )
        return True

    def _query_tile(self, tile: TileConfig, retries: int = 3) -> list[dict]:
        start_date, end_date = self.water_check_interval
        payload = {
            "input": {
                "bounds": {"bbox": tile.bbox},
                "data": [
                    {
                        "type": "sentinel-2-l2a",
                        "dataFilter": {
                            "timeRange": {
                                "from": f"{start_date}T00:00:00Z",
                                "to": f"{end_date}T23:59:59Z",
                            },
                            "maxCloudCoverage": self.max_cloud_coverage,
                        },
                    }
                ],
            },
            "aggregation": {
                "timeRange": {
                    "from": f"{start_date}T00:00:00Z",
                    "to": f"{end_date}T23:59:59Z",
                },
                "aggregationInterval": {"of": "P1D"},
                "evalscript": SWBM_EVALSCRIPT,
            },
        }

        for attempt in range(1, retries + 1):
            response = requests.post(
                STATS_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self._get_access_token()}",
                },
                json=payload,
                timeout=120,
            )
            if response.status_code == 401:
                self._access_tokens.pop(self._credential_index, None)
                continue
            if response.status_code in (429, 403) and self._switch_to_next_credentials(response.status_code, tile.name):
                continue
            if response.status_code in (429, 403) and attempt < retries:
                wait_seconds = 180
                print(
                    f"[WaterTileSelector] Rate limited ({response.status_code}) for {tile.name}; "
                    f"waiting {wait_seconds}s before retry {attempt + 1}/{retries}."
                )
                time.sleep(wait_seconds)
                continue
            response.raise_for_status()
            return self._parse_stats_response(response.json())

        response.raise_for_status()
        return []

    @staticmethod
    def _parse_stats_response(payload: dict) -> list[dict]:
        scenes: list[dict] = []
        for item in payload.get("data", []):
            interval = item.get("interval", {})
            outputs = item.get("outputs", {})
            stats_output = outputs.get("eobrowserStats") or outputs.get("data") or {}
            bands = stats_output.get("bands", {})
            water_stats = bands.get("B0", {}).get("stats", {})
            cloud_stats = bands.get("B1", {}).get("stats", {})
            sample_count = int(water_stats.get("sampleCount") or 0)
            no_data_count = int(water_stats.get("noDataCount") or 0)
            valid_pixels = max(sample_count - no_data_count, 0)
            water_mean = float(water_stats.get("mean") or 0.0)
            cloud_mean = float(cloud_stats.get("mean") or 0.0)
            scenes.append(
                {
                    "date": str(interval.get("from", ""))[:10],
                    "valid_pixels": valid_pixels,
                    "sample_count": sample_count,
                    "no_data_count": no_data_count,
                    "water_pct": max(0.0, min(100.0, water_mean * 100.0)),
                    "cloud_pct": max(0.0, min(100.0, cloud_mean * 100.0)),
                }
            )
        return scenes


def print_selection_summary(manifest: dict) -> None:
    selected = manifest.get("selected_tiles", [])
    rejected = manifest.get("rejected_tiles", [])
    threshold = manifest.get("threshold", {}).get("value_pct")
    print("\n=== Water Tile Selection ===")
    print(f"Threshold: {threshold:.3f}%" if threshold is not None else "Threshold: unknown")
    print(f"Selected tiles ({len(selected)}): {', '.join(selected) if selected else '(none)'}")
    print(f"Rejected tiles ({len(rejected)}): {', '.join(rejected) if rejected else '(none)'}")

    print("\nSelected tile scores:")
    tile_records = {record["name"]: record for record in manifest.get("tiles", [])}
    for tile_name in selected:
        record = tile_records.get(tile_name, {})
        print(
            f"  {tile_name:<12} water_score={record.get('water_score_pct', 0.0):6.2f}% "
            f"valid_scenes={record.get('valid_scene_count', 0)}"
        )
