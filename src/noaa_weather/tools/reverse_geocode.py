"""Reverse-geocode one or more lat/lon pairs via OSM Nominatim.

Results are cached at
``$AFL_CACHE_ROOT/noaa-weather/geocode/<lat_4dp>_<lon_4dp>.json``
with a sidecar. Nominatim rate-limits anonymous use to 1 req/sec, so
live lookups sleep between calls — cache hits don't.

Usage::

    python reverse_geocode.py 40.7789 -73.9692
    python reverse_geocode.py 40.7789 -73.9692 41.995 -87.9336
    python reverse_geocode.py --coords-file coords.txt
    python reverse_geocode.py 40.7789 -73.9692 --use-mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import geocode_nominatim  # noqa: E402


def _parse_pairs(args_nums: list[str]) -> list[tuple[float, float]]:
    if len(args_nums) % 2 != 0:
        raise SystemExit(
            f"error: need an even count of lat/lon numbers, got {len(args_nums)}"
        )
    pairs: list[tuple[float, float]] = []
    for i in range(0, len(args_nums), 2):
        try:
            pairs.append((float(args_nums[i]), float(args_nums[i + 1])))
        except ValueError as exc:
            raise SystemExit(f"error: cannot parse {args_nums[i : i + 2]!r}: {exc}")
    return pairs


def _parse_file(path: Path) -> list[tuple[float, float]]:
    pairs: list[tuple[float, float]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Accept "lat,lon" or "lat lon".
        parts = line.replace(",", " ").split()
        if len(parts) != 2:
            logging.warning("Skipping malformed line: %r", raw)
            continue
        try:
            pairs.append((float(parts[0]), float(parts[1])))
        except ValueError:
            logging.warning("Skipping malformed line: %r", raw)
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "coords",
        nargs="*",
        help="Pairs of lat lon numbers (even count).",
    )
    parser.add_argument(
        "--coords-file",
        type=Path,
        help="File with one 'lat,lon' or 'lat lon' per line.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-query even if cached."
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help=(
            "Opt in to deterministic mock data instead of a live Nominatim lookup. "
            "Default is real data; errors out if requests is not installed or "
            "the lookup fails."
        ),
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default: INFO)."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    pairs: list[tuple[float, float]] = _parse_pairs(args.coords)
    if args.coords_file:
        pairs.extend(_parse_file(args.coords_file))

    if not pairs:
        parser.error("no coordinates provided — pass them positionally or via --coords-file")

    results = []
    for lat, lon in pairs:
        geo = geocode_nominatim.reverse_geocode(
            lat, lon, force=args.force, use_mock=args.use_mock or None
        )
        results.append({"lat": lat, "lon": lon, **geo})

    json.dump({"count": len(results), "results": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
