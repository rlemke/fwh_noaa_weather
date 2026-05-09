"""Download per-station historical stdmet for one or more NDBC buoys.

Each station_id × year combination lands at
``$AFL_CACHE_ROOT/noaa-weather/ndbc-stdmet/<station_id>/<year>.txt.gz``
with a sibling ``.meta.json`` sidecar.

Usage::

    # Explicit list
    python fetch_buoy_data.py 46042 41001 --start-year 2015 --end-year 2024

    # Bulk resolution from the cached NDBC catalog + filters
    python fetch_buoy_data.py --from-catalog --type buoy --require met \\
        --start-year 2015 --end-year 2024

    # Bulk with Geofabrik bbox
    python fetch_buoy_data.py --from-catalog --region north-america/us \\
        --start-year 2020 --end-year 2024

Bulk expansion is guarded — unfiltered catalog queries are rejected
unless ``--i-know-this-is-huge`` is passed. Each station-year CSV is
small individually (~100 KB–2 MB), but expanded across thousands of
stations × tens of years the transfer becomes hours.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import geofabrik_regions, ndbc_download, ndbc_parse  # noqa: E402


BULK_FETCH_THRESHOLD = 500  # same rationale as GHCN's guard


def _parse_bbox(s: str) -> tuple[float, float, float, float]:
    parts = [float(p) for p in s.split(",")]
    if len(parts) != 4:
        raise SystemExit(
            "error: --bbox needs 4 values (min_lat,max_lat,min_lon,max_lon)"
        )
    return tuple(parts)  # type: ignore[return-value]


def _resolve_from_catalog(args: argparse.Namespace) -> list[str]:
    stations = ndbc_download.read_catalog_stations(
        force=args.force_catalog, use_mock=args.use_mock
    )

    bbox = None
    if args.bbox:
        bbox = _parse_bbox(args.bbox)
    elif args.region:
        try:
            info = geofabrik_regions.resolve_region(
                args.region, use_mock=args.use_mock
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        bbox = info.bbox

    types = set(args.type) if args.type else None
    require = tuple(args.require or ())

    filtered = ndbc_parse.filter_buoys(
        stations,
        bbox=bbox,
        types=types,
        require_fields=require,
        max_stations=args.max_stations,
    )
    return [s["station_id"] for s in filtered]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("station_ids", nargs="*", help="NDBC station IDs.")
    parser.add_argument("--start-year", type=int, default=2015)
    parser.add_argument("--end-year", type=int, default=2024)
    parser.add_argument(
        "--stations-file",
        type=Path,
        help="File with one station_id per line (# for comments).",
    )

    catalog = parser.add_argument_group("catalog-driven mode")
    catalog.add_argument(
        "--from-catalog",
        action="store_true",
        help="Resolve station list from the cached NDBC catalog.",
    )
    catalog.add_argument("--region", default="")
    catalog.add_argument("--bbox", default="")
    catalog.add_argument("--type", action="append", default=None)
    catalog.add_argument("--require", action="append", default=None)
    catalog.add_argument("--max-stations", type=int, default=0)
    catalog.add_argument("--force-catalog", action="store_true")
    catalog.add_argument(
        "--i-know-this-is-huge",
        action="store_true",
        help=f"Override the {BULK_FETCH_THRESHOLD}-station safety guard.",
    )

    parser.add_argument("--force", action="store_true", help="Re-download CSVs.")
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help="Deterministic offline data (no network).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    if args.region or args.bbox or args.type or args.require:
        args.from_catalog = True

    # Resolve the station list from every source passed in.
    ids: list[str] = list(args.station_ids)
    if args.stations_file:
        for raw in args.stations_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                ids.append(line)
    if args.from_catalog:
        ids.extend(_resolve_from_catalog(args))

    # Dedup, preserve order.
    seen: set[str] = set()
    ids = [s for s in ids if not (s in seen or seen.add(s))]

    if not ids:
        parser.error(
            "no station IDs — pass positional IDs, --stations-file, or --from-catalog."
        )

    if (
        args.from_catalog
        and len(ids) > BULK_FETCH_THRESHOLD
        and not args.i_know_this_is_huge
    ):
        print(
            f"error: {len(ids):,} stations, above the {BULK_FETCH_THRESHOLD}-"
            "station safety threshold. Narrow --type / --require / --region "
            "or pass --i-know-this-is-huge.",
            file=sys.stderr,
        )
        return 2

    years = list(range(args.start_year, args.end_year + 1))
    total = len(ids) * len(years)
    print(
        f"# fetching {total:,} station-year(s): {len(ids)} station(s) "
        f"× {len(years)} year(s)",
        file=sys.stderr,
    )

    failures: list[tuple[str, int, str]] = []
    t0 = time.monotonic()
    for idx, sid in enumerate(ids, 1):
        for year in years:
            try:
                res = ndbc_download.download_stdmet(
                    sid, year, force=args.force, use_mock=args.use_mock
                )
            except Exception as exc:
                print(f"error: {sid}/{year}: {exc}", file=sys.stderr)
                failures.append((sid, year, str(exc)))
                continue
            status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
            print(
                f"[{status}] ndbc-stdmet/{res.relative_path}  "
                f"{res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
                f"{res.absolute_path}"
            )

    elapsed = time.monotonic() - t0
    ok = total - len(failures)
    print(
        f"# done: {ok:,} ok, {len(failures):,} failed in {elapsed:.1f}s",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
