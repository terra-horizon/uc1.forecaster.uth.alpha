from __future__ import annotations

import csv
import json
import math
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable


TARGET_COLUMNS = ("CDOM", "Chl_a", "Color", "Cya", "DOC", "Turb", "WQI")
CURRENT_COLLECTION_METHOD = "all_valid_pixels_v1"
HISTORY_COLUMNS = (
    "schema_version",
    "tile_id",
    "observation_date",
    "bbox",
    "collection_status",
    "collection_method",
    "water_check_status",
    "water_status",
    "water_pct",
    "cloud_pct",
    "valid_pixels",
    "source_scene_count",
    "stac_item_ids",
    "asset_paths",
    "quality_flags",
    "attempt_count",
    "created_at",
    "updated_at",
    *TARGET_COLUMNS,
)
TERMINAL_STATUSES = {"collected", "unavailable"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_date(value: str | date | datetime) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def safe_float(value: Any) -> float | None:
    if value in (None, "", "NaN"):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _json_default(value: Any) -> str:
    """Serialize BSON-compatible temporal values restored from MongoDB."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def write_json(path: Path, value: Any) -> None:
    _atomic_text(path, json.dumps(value, indent=2, allow_nan=False, default=_json_default) + "\n")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


class HistoryStore:
    def __init__(self, json_path: Path, csv_path: Path):
        self.json_path = json_path
        self.csv_path = csv_path

    def load(self) -> list[dict[str, Any]]:
        value = read_json(self.json_path, [])
        if not isinstance(value, list):
            raise ValueError(f"History must be a JSON array: {self.json_path}")
        return value

    def upsert(self, records: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        existing_rows = self.load()
        existing = {(str(row.get("tile_id")), str(row.get("observation_date"))): row for row in existing_rows}
        changed = 0
        for record in records:
            key = (str(record.get("tile_id")), str(record.get("observation_date")))
            previous = existing.get(key)
            if previous != record:
                changed += 1
            existing[key] = record
        rows = sorted(existing.values(), key=lambda row: (str(row.get("tile_id")), str(row.get("observation_date"))))
        self.write(rows)
        return rows, changed

    def write(self, records: list[dict[str, Any]]) -> None:
        write_json(self.json_path, records)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.csv_path.name}.", dir=self.csv_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=HISTORY_COLUMNS, extrasaction="ignore")
                writer.writeheader()
                for record in records:
                    row = dict(record)
                    for field in ("bbox", "stac_item_ids", "asset_paths", "quality_flags"):
                        row[field] = json.dumps(row.get(field), allow_nan=False, separators=(",", ":"), default=_json_default)
                    writer.writerow({column: row.get(column) for column in HISTORY_COLUMNS})
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.csv_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def is_terminal(record: dict[str, Any]) -> bool:
    status = record.get("collection_status")
    return status in TERMINAL_STATUSES and record.get("collection_method") == CURRENT_COLLECTION_METHOD


def is_usable(record: dict[str, Any]) -> bool:
    return record.get("water_status") == "water" and all(record.get(column) is not None for column in TARGET_COLUMNS)


def has_complete_metrics(record: dict[str, Any]) -> bool:
    return all(record.get(column) is not None for column in TARGET_COLUMNS)
