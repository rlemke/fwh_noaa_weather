"""Download per-station GHCN CSVs into the shared cache.

Outputs land at ``$AFL_CACHE_ROOT/noaa-weather/station-csv/<station_id>.csv``
with a sibling ``.meta.json`` sidecar that records the inventory's
``first_year`` / ``last_year`` (when discovered via ``--from-catalog``).

Usage::

    # Explicit station IDs
    python fetch_station_csv.py USW00094728
    python fetch_station_csv.py USW00094728 USW00014732 USW00094846

    # From a file (one station_id per line, # for comments)
    python fetch_station_csv.py --stations-file my-stations.txt

    # Resolve the station list from the cached catalog + inventory.
    # Applies the same filters as discover-stations.
    python fetch_station_csv.py --from-catalog --country US --state NY --min-years 30
    python fetch_station_csv.py --from-catalog --country CA --max-stations 20

    # Force re-download even if cached
    python fetch_station_csv.py USW00094728 --force

    # Offline mode (deterministic, no network)
    python fetch_station_csv.py USW00094728 --use-mock
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import geofabrik_regions, ghcn_download, ghcn_parse  # noqa: E402

# Safety threshold for bulk catalog-driven fetches. NOAA has ~120k stations
# with multi-MB CSVs each; letting --from-catalog silently expand to all of
# them is a footgun. Callers crossing this line must pass --i-know-this-is-huge.
BULK_FETCH_THRESHOLD = 500


def _read_stations_file(path: Path) -> list[str]:
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def _resolve_from_catalog(
    args: argparse.Namespace,
) -> list[tuple[str, dict[str, Any]]]:
    """Load the catalog + inventory, apply filters, return (id, metadata) pairs.

    The metadata dict is merged into each station's sidecar ``extra`` so
    downstream tools can see the inventory's recorded year range without
    re-reading the catalog.
    """
    stations_text = ghcn_download.read_catalog_file(
        "stations", force=args.force_catalog, use_mock=args.use_mock or None
    )
    inventory_text = ghcn_download.read_catalog_file(
        "inventory", force=args.force_catalog, use_mock=args.use_mock or None
    )
    stations = ghcn_parse.parse_stations(stations_text)
    inventory = ghcn_parse.parse_inventory(inventory_text)

    bbox: tuple[float, float, float, float] | None = None
    region_info: geofabrik_regions.RegionInfo | None = None
    if args.region:
        region_info = geofabrik_regions.resolve_region(
            args.region, use_mock=args.use_mock or None
        )
        bbox = region_info.bbox
        logger = logging.getLogger("noaa-weather.fetch")
        logger.info(
            "Geofabrik region %r resolved: bbox=%s name=%r",
            region_info.path,
            bbox,
            region_info.name,
        )

    # When a region is explicit, the bbox is authoritative — skip the
    # FIPS country filter unless the caller set it to non-default.
    country_filter = args.country
    if args.region and not args.country_explicit:
        country_filter = ""

    # max_stations=0 → no cap. filter_stations() takes the first N from a
    # list sorted by data coverage, so a very large N is equivalent to "all".
    cap = args.max_stations if args.max_stations > 0 else len(stations)
    filtered = ghcn_parse.filter_stations(
        stations,
        inventory,
        country=country_filter,
        state=args.state,
        bbox=bbox,
        max_stations=cap,
        min_years=args.min_years,
        required_elements=args.required,
    )

    discovery: dict[str, Any] = {
        "country": country_filter,
        "state": args.state,
        "min_years": args.min_years,
        "required_elements": args.required or ["TMAX", "TMIN", "PRCP"],
    }
    if region_info is not None:
        discovery["region"] = {
            "path": region_info.path,
            "name": region_info.name,
            "bbox": list(region_info.bbox),
        }

    out: list[tuple[str, dict[str, Any]]] = []
    for s in filtered:
        out.append(
            (
                s["station_id"],
                {
                    "name": s.get("name"),
                    "lat": s.get("lat"),
                    "lon": s.get("lon"),
                    "elevation": s.get("elevation"),
                    "first_year": s.get("first_year"),
                    "last_year": s.get("last_year"),
                    "elements": s.get("elements"),
                    "discovery": discovery,
                },
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("station_ids", nargs="*", help="GHCN station IDs to fetch.")
    parser.add_argument(
        "--stations-file",
        type=Path,
        help="File with one station_id per line (lines starting with # are comments).",
    )

    # --- Catalog-driven bulk mode -------------------------------------------
    catalog_group = parser.add_argument_group(
        "catalog-driven mode",
        "Resolve the station list from the cached catalog + inventory. "
        "Flags mirror discover-stations so the filter is consistent across tools.",
    )
    catalog_group.add_argument(
        "--from-catalog",
        action="store_true",
        help="Fetch every station in the catalog that matches the filter below.",
    )
    catalog_group.add_argument(
        "--region",
        default="",
        help=(
            "Geofabrik region path (e.g. 'europe/germany', 'north-america/us/california'). "
            "The region's bounding box is the spatial filter; overrides --country unless "
            "--country is passed explicitly. Use with --from-catalog."
        ),
    )
    catalog_group.add_argument("--country", default="US", help="FIPS country code (default: US).")
    catalog_group.add_argument(
        "--state", default="", help="2-letter US state abbreviation (empty = no state filter)."
    )
    catalog_group.add_argument(
        "--min-years",
        type=int,
        default=20,
        help="Minimum years of inventory coverage (default: 20).",
    )
    catalog_group.add_argument(
        "--required",
        action="append",
        default=None,
        help="Required element (TMAX/TMIN/PRCP/…). Repeatable. Default: TMAX TMIN PRCP.",
    )
    catalog_group.add_argument(
        "--max-stations",
        type=int,
        default=0,
        help="Cap on stations to fetch. 0 = no cap (default).",
    )
    catalog_group.add_argument(
        "--force-catalog",
        action="store_true",
        help="Re-download the catalog + inventory even if cached.",
    )
    catalog_group.add_argument(
        "--i-know-this-is-huge",
        action="store_true",
        help=(
            f"Override the {BULK_FETCH_THRESHOLD}-station safety guard. "
            "Required when --from-catalog resolves to more stations than that "
            "(each CSV is multi-MB — a full country can be hundreds of GB)."
        ),
    )

    # --- Per-download options -----------------------------------------------
    parser.add_argument(
        "--force", action="store_true", help="Re-download CSVs even if cached."
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help=(
            "Opt in to deterministic mock data instead of a live NOAA fetch. "
            "Default is real data; errors out if requests is not installed."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved station list without downloading anything.",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default: INFO)."
    )
    args = parser.parse_args()

    # argparse doesn't natively track "was this flag explicit?" — inspect
    # argv so --region can demote the default --country without clobbering
    # a user who passed both.
    args.country_explicit = any(
        a == "--country" or a.startswith("--country=") for a in sys.argv[1:]
    )

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # --region alone implies catalog resolution — otherwise the flag would
    # silently do nothing.
    if args.region and not args.from_catalog:
        args.from_catalog = True

    # Assemble (id, per-station-extra-metadata) pairs from every input source.
    targets: list[tuple[str, dict[str, Any]]] = []
    for sid in args.station_ids:
        targets.append((sid, {}))
    if args.stations_file:
        for sid in _read_stations_file(args.stations_file):
            targets.append((sid, {}))
    if args.from_catalog:
        try:
            targets.extend(_resolve_from_catalog(args))
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    if not targets:
        parser.error(
            "no station IDs resolved — pass them positionally, via --stations-file, "
            "or via --from-catalog"
        )

    # Deduplicate preserving first-seen metadata (positional args + file come
    # first, so their empty-dict metadata stays; --from-catalog only fills in
    # a year range for IDs we haven't already recorded).
    seen: dict[str, dict[str, Any]] = {}
    for sid, extra in targets:
        if sid not in seen:
            seen[sid] = extra
        elif not seen[sid] and extra:
            seen[sid] = extra
    targets = list(seen.items())

    # Scale guard — only trips in catalog-driven bulk mode.
    if (
        args.from_catalog
        and len(targets) > BULK_FETCH_THRESHOLD
        and not args.i_know_this_is_huge
    ):
        print(
            f"error: --from-catalog resolved to {len(targets):,} stations, above the "
            f"{BULK_FETCH_THRESHOLD}-station safety threshold.\n"
            f"       Narrow with --country / --state / --min-years / --max-stations, "
            f"or pass --i-know-this-is-huge to bypass.",
            file=sys.stderr,
        )
        return 2

    if args.dry_run:
        print(f"# dry-run: would fetch {len(targets):,} station(s)")
        for sid, extra in targets:
            yr_hint = ""
            if "first_year" in extra and "last_year" in extra:
                yr_hint = f"  years={extra['first_year']}-{extra['last_year']}"
            print(f"{sid}{yr_hint}")
        return 0

    # Emit a header when bulk-fetching so progress is readable in a log.
    if len(targets) > 10:
        print(f"# fetching {len(targets):,} station(s)", file=sys.stderr)

    failures: list[str] = []
    t0 = time.monotonic()
    for idx, (sid, extra) in enumerate(targets, 1):
        try:
            res = ghcn_download.download_station_csv(
                sid,
                force=args.force,
                use_mock=args.use_mock or None,
                extra_metadata=extra or None,
            )
        except Exception as exc:
            print(f"error: {sid}: {exc}", file=sys.stderr)
            failures.append(sid)
            continue
        status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
        prefix = f"[{idx}/{len(targets)}] " if len(targets) > 10 else ""
        yr_hint = ""
        if "first_year" in extra and "last_year" in extra:
            yr_hint = f"  years={extra['first_year']}-{extra['last_year']}"
        print(
            f"{prefix}[{status}] {sid}  {res.size_bytes:,}B  "
            f"sha256={res.sha256[:12]}…{yr_hint}  {res.absolute_path}"
        )

    if len(targets) > 10:
        elapsed = time.monotonic() - t0
        print(
            f"# done: {len(targets) - len(failures):,} ok, {len(failures):,} failed "
            f"in {elapsed:.1f}s",
            file=sys.stderr,
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
