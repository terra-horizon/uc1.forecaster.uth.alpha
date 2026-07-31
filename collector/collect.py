#!/usr/bin/env python3
"""Convenience launcher for the standalone TERRA UC1 data collector.

Run from this collector folder, for example:
    python3 collect.py run --aoi-id sperchios --run-name sperchios-collection \
      --bbox 22.433493 38.837552 22.569555 38.894223 --target-date 2026-07-30
"""

from __future__ import annotations

from data_collection.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
