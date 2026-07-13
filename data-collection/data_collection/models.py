from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal


CollectionMode = Literal["auto", "backfill", "incremental"]


@dataclass(frozen=True)
class CollectionRequest:
    aoi_bbox: list[float]
    run_name: str
    output_root: Path | str = Path("outputs")
    history_start: str = "2016-01-01"
    target_date: str | None = None
    mode: CollectionMode = "auto"
    dry_run: bool = False
    max_days_per_run: int | None = None
    max_tiles_per_run: int | None = None
    discovery_chunk_days: int = 31
    spacing_m: int = 400
    box_size_m: int = 400
    min_river_length_m: float = 10_000.0
    projected_crs: str = "EPSG:32634"
    water_threshold: str | float = "distribution"
    water_min_auto_threshold_pct: float = 0.5
    max_cloud_coverage: int = 30
    refresh_water: bool = False

    def __post_init__(self) -> None:
        if len(self.aoi_bbox) != 4:
            raise ValueError("aoi_bbox must contain min_lon min_lat max_lon max_lat")
        if self.mode not in {"auto", "backfill", "incremental"}:
            raise ValueError("mode must be auto, backfill, or incremental")
        if self.discovery_chunk_days < 1:
            raise ValueError("discovery_chunk_days must be positive")
        if self.max_days_per_run is not None and self.max_days_per_run < 1:
            raise ValueError("max_days_per_run must be positive")
        if self.max_tiles_per_run is not None and self.max_tiles_per_run < 1:
            raise ValueError("max_tiles_per_run must be positive")

    @property
    def output_path(self) -> Path:
        return Path(self.output_root)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["output_root"] = str(self.output_root)
        return value


@dataclass(frozen=True)
class CollectionResult:
    status: str
    run_dir: str
    run_summary: str
    mode: str
    available_dates: list[str] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)
    collected_dates: list[str] = field(default_factory=list)
    new_record_count: int = 0
    failed_units: list[dict[str, Any]] = field(default_factory=list)
    latest_usable_observation: str | None = None
    history_json_path: str | None = None
    history_csv_path: str | None = None
    tile_records_path: str | None = None
    tiles_geojson_path: str | None = None
    water_manifest_path: str | None = None
    state_path: str | None = None
    discovery_windows: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
