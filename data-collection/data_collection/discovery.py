from __future__ import annotations

import time
from typing import Any

import requests

from .storage import normalize_date


STAC_SEARCH_URL = "https://stac.dataspace.copernicus.eu/v1/search"
SENTINEL2_COLLECTION = "sentinel-2-l2a"


class CDSEStacDiscovery:
    def __init__(self, max_cloud_coverage: int = 30, post=requests.post, retry_sleep_seconds: int = 5):
        self.max_cloud_coverage = max_cloud_coverage
        self.post = post
        self.retry_sleep_seconds = retry_sleep_seconds

    def discover_dates(self, bbox: list[float], start_date: str, end_date: str) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
        payload: dict[str, Any] = {
            "collections": [SENTINEL2_COLLECTION],
            "bbox": [float(value) for value in bbox],
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "limit": 100,
            "query": {"eo:cloud_cover": {"lte": int(self.max_cloud_coverage)}},
        }
        dates: set[str] = set()
        item_ids: dict[str, list[str]] = {}
        warnings: list[dict[str, Any]] = []
        url: str | None = STAC_SEARCH_URL
        while url:
            response = self._request(url, payload)
            if response is None:
                warnings.append({"code": "STAC_DISCOVERY_UNAVAILABLE", "message": "STAC discovery returned no response."})
                break
            response.raise_for_status()
            body = response.json()
            for feature in body.get("features", []):
                properties = feature.get("properties") or {}
                observed = normalize_date(properties.get("datetime") or properties.get("start_datetime") or "")
                if observed:
                    dates.add(observed)
                    item_ids.setdefault(observed, []).append(str(feature.get("id") or ""))
            next_link = next((link for link in body.get("links", []) if link.get("rel") == "next"), None)
            url = str(next_link["href"]) if next_link and next_link.get("href") else None
            if url:
                payload = next_link.get("body") or payload
        return sorted(dates), {key: sorted(set(values)) for key, values in item_ids.items()}, warnings

    def _request(self, url: str, payload: dict[str, Any], retries: int = 3):
        for attempt in range(1, retries + 1):
            try:
                response = self.post(url, json=payload, timeout=120)
            except requests.RequestException:
                if attempt == retries:
                    raise
                time.sleep(self.retry_sleep_seconds)
                continue
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == retries:
                return response
            retry_after = response.headers.get("Retry-After")
            time.sleep(min(int(retry_after), 60) if retry_after and retry_after.isdigit() else self.retry_sleep_seconds)
        return None
