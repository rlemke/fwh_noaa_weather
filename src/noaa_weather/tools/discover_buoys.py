"""Filter the cached NDBC catalog into a candidate buoy list.

Mirrors ``discover-stations`` (for GHCN) but uses the NDBC active-
stations catalog and buoy-specific filter dimensions (station type,
sensor families, bbox, Geofabrik region).

Usage::

    # All moored buoys with a met sensor
    python discover_buoys.py --type buoy --require met

    # Atlantic US coast, any type
    python discover_buoys.py --bbox 24.0,45.0,-81.0,-65.0

    # Geofabrik region — bbox trimmed to the region's polygon bbox
    python discover_buoys.py --region north-america/us

    # Offline preview
    python discover_buoys.py --use-mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import geofabrik_regions, ndbc_download, ndbc_parse  # noqa: E402


_STATION_TYPES = {"buoy", "cman", "dart", "oil", "other"}


def _parse_bbox(s: str) -> tuple[float, float, float, float]:
    try:
        parts = [float(p) for p in s.split(",")]
    except ValueError as exc:
        raise SystemExit(f"error: --bbox needs 4 comma-separated numbers: {exc}")
    if len(parts) != 4:
        raise SystemExit(
            "error: --bbox needs 4 values (min_lat,max_lat,min_lon,max_lon)"
        )
    return tuple(parts)  # type: ignore[return-value]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--region",
        default="",
        help=(
            "Geofabrik region path. The region's bbox is used as the "
            "spatial filter."
        ),
    )
    parser.add_argument(
        "--bbox",
        default="",
        help="min_lat,max_lat,min_lon,max_lon (overrides --region's bbox).",
    )
    parser.add_argument(
        "--type",
        action="append",
        default=None,
        help=(
            f"Station type to include. Repeatable. Choices: "
            f"{sorted(_STATION_TYPES)}. Default: every type."
        ),
    )
    parser.add_argument(
        "--require",
        action="append",
        default=None,
        choices=["met", "currents", "waterquality", "dart"],
        help="Sensor families that must be active. Repeatable.",
    )
    parser.add_argument(
        "--max-stations", type=int, default=0, help="Cap on stations (0 = no cap)."
    )
    parser.add_argument(
        "--force-catalog",
        action="store_true",
        help="Re-download the NDBC catalog before filtering.",
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help="Deterministic offline data (no network).",
    )
    parser.add_argument(
        "--write-cache",
        action="store_true",
        help=(
            "Write the filtered list back to the catalog cache as "
            "``ndbc-catalog/discovered/<label>.json`` for downstream tools."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    stations = ndbc_download.read_catalog_stations(
        force=args.force_catalog, use_mock=args.use_mock
    )

    # Resolve the bbox — either explicit, or from a Geofabrik region.
    bbox: tuple[float, float, float, float] | None = None
    if args.bbox:
        bbox = _parse_bbox(args.bbox)
    elif args.region:
        try:
            info = geofabrik_regions.resolve_region(
                args.region, use_mock=args.use_mock
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        bbox = info.bbox

    types = set(args.type) if args.type else None
    if types and (unknown := types - _STATION_TYPES):
        print(f"error: unknown --type value(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    require = tuple(args.require or ())
    filtered = ndbc_parse.filter_buoys(
        stations,
        bbox=bbox,
        types=types,
        require_fields=require,
        max_stations=args.max_stations,
    )

    output = {
        "region": args.region,
        "bbox": list(bbox) if bbox else None,
        "types": sorted(types) if types else None,
        "require": list(require),
        "station_count": len(filtered),
        "stations": filtered,
    }
    json.dump(output, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
