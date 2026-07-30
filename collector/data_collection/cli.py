from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import CollectionRequest
from .service import collect
from .validation import validate_run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TERRA UC1 standalone data collection pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="Collect or update Sentinel-2 history")
    run.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("MIN_LON", "MIN_LAT", "MAX_LON", "MAX_LAT"))
    run.add_argument("--run-name", required=True)
    run.add_argument("--aoi-id", help="Stable physical AOI identifier; defaults to a normalized run name.")
    run.add_argument("--output-root", default="outputs")
    run.add_argument("--history-start", default="2016-01-01")
    run.add_argument("--target-date")
    run.add_argument("--mode", choices=("auto", "backfill", "incremental"), default="auto")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--max-days-per-run", type=int)
    run.add_argument("--max-tiles-per-run", type=int)
    run.add_argument("--discovery-chunk-days", type=int, default=31)
    run.add_argument("--spacing-m", type=int, default=400)
    run.add_argument("--box-size-m", type=int, default=400)
    run.add_argument("--min-river-length-m", type=float, default=10_000.0)
    run.add_argument("--projected-crs", default="EPSG:32634")
    run.add_argument("--max-cloud-coverage", type=int, default=30)

    validate = subparsers.add_parser("validate", help="Validate an existing collector run")
    validate.add_argument("--run-dir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "validate":
        report = validate_run(Path(args.run_dir))
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 1

    result = collect(CollectionRequest(
        aoi_bbox=list(args.bbox),
        run_name=args.run_name,
        aoi_id=args.aoi_id,
        output_root=Path(args.output_root),
        history_start=args.history_start,
        target_date=args.target_date,
        mode=args.mode,
        dry_run=args.dry_run,
        max_days_per_run=args.max_days_per_run,
        max_tiles_per_run=args.max_tiles_per_run,
        discovery_chunk_days=args.discovery_chunk_days,
        spacing_m=args.spacing_m,
        box_size_m=args.box_size_m,
        min_river_length_m=args.min_river_length_m,
        projected_crs=args.projected_crs,
        max_cloud_coverage=args.max_cloud_coverage,
    ))
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status in {"success", "dry_run"} else 2
