"""Independent Sentinel-3 SLSTR L1B daily statistics.

S8 measures top-of-atmosphere brightness temperature, not retrieved surface
temperature. The Statistics API uses a most-recent daily nadir mosaic; its
spatial statistics are not an average of all daily overpasses.
"""
from __future__ import annotations

import math
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from .. import credentials
from ..storage import write_json

METHOD = "slstr_s8_bt_daily_most_recent_v1"
METRIC = "s3_s8_brightness_temperature_c"
EVALSCRIPT = """
//VERSION=3
function setup() {
  return {
    input: [{bands: ["S8", "dataMask"]}],
    output: [{id: "data", bands: 1, sampleType: "FLOAT32"},
             {id: "dataMask", bands: 1}],
    mosaicking: "SIMPLE"
  };
}
function evaluatePixel(s) {
  var valid = s.dataMask && isFinite(s.S8) && s.S8 > 0;
  return {data: [valid ? s.S8 - 273.15 : 0], dataMask: [valid ? 1 : 0]};
}
"""


class Sentinel3Collection:
    def __init__(self, time_interval=None, bbox=None, dir="data"):
        self.time_interval = time_interval
        self.bbox = list(bbox or [])
        self.dir = Path(dir)
        self.token_url = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
        self.api_url = "https://sh.dataspace.copernicus.eu/statistics/v1"
        self.credential_sets = credentials.get_credential_sets()
        self.access_token = None

    def get_access_token(self):
        for pair in self.credential_sets:
            try:
                response = requests.post(self.token_url, data={
                    "grant_type": "client_credentials",
                    "client_id": pair["client_id"],
                    "client_secret": pair["client_secret"],
                }, timeout=30)
                if response.status_code == 200:
                    token = response.json().get("access_token")
                    if token:
                        return token
            except (requests.RequestException, ValueError):
                continue
        raise RuntimeError("Sentinel-3 authentication failed; no usable CDSE credential")

    def get_request(self, evalscript, time_interval, bbox, retries=3):
        start = date.fromisoformat(time_interval[0])
        end = date.fromisoformat(time_interval[1]) + timedelta(days=1)
        if start >= end:
            raise ValueError("Invalid Sentinel-3 interval")
        interval = {"from": f"{start}T00:00:00Z", "to": f"{end}T00:00:00Z"}
        payload = {
            "input": {
                "bounds": {"bbox": bbox},
                "data": [{
                    "type": "sentinel-3-slstr",
                    "dataFilter": {"timeRange": interval, "view": "NADIR", "mosaickingOrder": "mostRecent"},
                    "processing": {"upsampling": "NEAREST"},
                }],
            },
            "aggregation": {
                "timeRange": interval,
                "aggregationInterval": {"of": "P1D"},
                "evalscript": evalscript,
                # Explicit WGS84 sampling grid (~1 km). Counts describe this
                # grid, not independent native pixels within 400 m river tiles.
                "resx": min(0.01, (bbox[2] - bbox[0]) / 2),
                "resy": min(0.01, (bbox[3] - bbox[1]) / 2),
            },
        }
        for attempt in range(retries):
            if not self.access_token:
                self.access_token = self.get_access_token()
            try:
                response = requests.post(self.api_url, headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                }, json=payload, timeout=120)
            except requests.RequestException:
                if attempt + 1 == retries:
                    raise RuntimeError("Sentinel-3 network retries exhausted") from None
                continue
            if response.status_code == 200:
                try:
                    body = response.json()
                    interval_errors = [item.get("error") for item in body.get("data", [])
                                       if isinstance(item, dict) and item.get("error")]
                except (ValueError, AttributeError):
                    interval_errors = []
                if not interval_errors:
                    return response
                # Sentinel Hub can report transient processing failures inside
                # an otherwise successful HTTP 200 response. Retry the complete
                # bounded interval so no tile-days are silently skipped.
                if attempt + 1 < retries:
                    time.sleep(2 ** attempt)
                    continue
                detail = str(interval_errors[0]).replace("\n", " ")[:500]
                raise RuntimeError(
                    f"Sentinel-3 interval processing retries exhausted: {detail}")
            if response.status_code == 401:
                self.access_token = None
            elif response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < retries:
                    try:
                        delay = float(response.headers.get("Retry-After", 2 ** attempt))
                    except ValueError:
                        delay = 2 ** attempt
                    time.sleep(min(60, max(0, delay)))
            else:
                # Forbidden and malformed requests are not rate limiting.
                raise RuntimeError(f"Sentinel-3 request rejected: HTTP {response.status_code}")
        raise RuntimeError(f"Sentinel-3 request retries exhausted: HTTP {response.status_code}")

    def collect_daily(self, start, end):
        scenes = self.discover(start, end)
        if not scenes:
            payload = {"data": []}
        else:
            response = self.get_request(EVALSCRIPT, (start, end), self.bbox)
            payload = response.json()
        rows = self.parse_daily(payload, start, end)
        cursor = date.fromisoformat(start)
        while cursor <= date.fromisoformat(end):
            observed = cursor.isoformat()
            if observed not in scenes and observed not in rows:
                rows[observed] = {
                    "collection_status": "unavailable", METRIC: None,
                    "min_c": None, "max_c": None, "stdev_c": None,
                    "sample_count": 0, "valid_sample_count": 0,
                    "quality_flags": ["no_catalogue_scene"],
                }
            if observed in rows:
                rows[observed]["stac_item_ids"] = scenes.get(observed, [])
            cursor += timedelta(days=1)
        write_json(self.dir / f"{start}_{end}.json", {"scenes": scenes, "statistics": payload})
        return rows

    def discover(self, start, end):
        """Independent, paginated S3 catalogue query; never consult S2 dates."""
        url = "https://sh.dataspace.copernicus.eu/api/v1/catalog/1.0.0/search"
        query = {
            "collections": ["sentinel-3-slstr"], "bbox": self.bbox,
            "datetime": f"{start}T00:00:00Z/{end}T23:59:59Z", "limit": 100,
        }
        scenes, seen = {}, set()
        while True:
            for attempt in range(3):
                if not self.access_token:
                    self.access_token = self.get_access_token()
                try:
                    response = requests.post(url, json=query, headers={
                        "Authorization": f"Bearer {self.access_token}",
                    }, timeout=60)
                except requests.RequestException:
                    if attempt == 2:
                        raise RuntimeError("Sentinel-3 catalogue network retries exhausted") from None
                    continue
                if response.status_code == 200:
                    break
                if response.status_code == 401:
                    self.access_token = None
                elif response.status_code == 429 or response.status_code >= 500:
                    time.sleep(2 ** attempt)
                else:
                    raise RuntimeError(f"Sentinel-3 catalogue HTTP {response.status_code}")
            else:
                raise RuntimeError("Sentinel-3 catalogue retries exhausted")
            body = response.json()
            if not isinstance(body.get("features"), list):
                raise ValueError("Malformed Sentinel-3 catalogue response")
            for feature in body["features"]:
                observed = feature["properties"]["datetime"][:10]
                if not start <= observed <= end:
                    raise ValueError("Sentinel-3 catalogue date outside requested range")
                scenes.setdefault(observed, []).append(feature["id"])
            cursor = body.get("context", {}).get("next")
            if cursor is None:
                return {day: sorted(set(ids)) for day, ids in scenes.items()}
            if str(cursor) in seen:
                raise ValueError("Repeated Sentinel-3 catalogue cursor")
            seen.add(str(cursor))
            query["next"] = cursor

    @staticmethod
    def parse_daily(payload, start, end):
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Malformed Sentinel-3 statistics response")
        rows = {}
        for item in payload["data"]:
            if item.get("error"):
                raise ValueError("Sentinel-3 interval processing failed")
            observed = date.fromisoformat(item["interval"]["from"][:10]).isoformat()
            if not start <= observed <= end or observed in rows:
                raise ValueError("Unexpected/duplicate Sentinel-3 interval")
            stats = item["outputs"]["data"]["bands"]["B0"]["stats"]
            sample_count = int(stats["sampleCount"])
            nodata_count = int(stats["noDataCount"])
            if sample_count < 0 or not 0 <= nodata_count <= sample_count:
                raise ValueError("Invalid Sentinel-3 sample counts")
            valid = sample_count - nodata_count
            values = {}
            for name in ("mean", "min", "max", "stDev"):
                value = stats.get(name)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    value = None
                values[name] = value if value is not None and math.isfinite(value) else None
            if valid and any(value is None for value in values.values()):
                raise ValueError("Valid Sentinel-3 samples have missing/nonfinite statistics")
            rows[observed] = {
                "collection_status": "collected" if valid else "unavailable",
                METRIC: values["mean"] if valid else None,
                "min_c": values["min"] if valid else None,
                "max_c": values["max"] if valid else None,
                "stdev_c": values["stDev"] if valid else None,
                "sample_count": sample_count,
                "valid_sample_count": valid,
                "quality_flags": ["cloud_mask_not_applied"] + ([] if valid else ["no_valid_samples"]),
            }
        # An omitted interval is ambiguous, including an HTTP 200 empty data
        # array. Leave it retryable instead of declaring archival absence.
        return rows
