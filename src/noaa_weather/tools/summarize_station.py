"""Compute yearly climate summaries for one or many stations.

Single-station mode (no bulk flags):
    Downloads (if needed) the station CSV, parses it to the requested
    year range, computes per-year summaries (mean temperature, totals,
    hot/frost days), and emits the JSON on stdout. ``--write-cache``
    also persists it to ``cache/noaa-weather/climate-summary/<id>.json``.

Bulk mode (``--from-catalog`` / ``--region`` / ``--stations-file``):
    Resolves N stations, downloads each CSV if needed, summarizes each,
    and writes to ``cache/noaa-weather/climate-summary/<id>.json``
    unconditionally. One status line per station on stdout.

Usage::

    # Single station (stdout JSON)
    python summarize_station.py USW00094728 --state NY

    # Single station + persist to cache
    python summarize_station.py USW00094728 --state NY --write-cache

    # Bulk: every NY station with ≥30y of data
    python summarize_station.py --from-catalog --region north-america/us \\
        --state NY --min-years 30

    # Bulk: from an explicit list
    python summarize_station.py --stations-file ny-stations.txt --state NY

    # Preview what would run
    python summarize_station.py --from-catalog --region north-america/us \\
        --state NY --dry-run

Bulk-mode scale guard (same 500-station threshold as fetch-station-csv):
Unfiltered catalog queries are rejected unless ``--i-know-this-is-huge``
is passed. Summaries are cheap compared to CSV downloads, but you may
still be re-downloading multi-MB CSVs per station, so the guard stays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import (  # noqa: E402
    climate_analysis,
    geofabrik_regions,
    ghcn_download,
    ghcn_parse,
    sidecar,
)
from _lib.storage import LocalStorage  # noqa: E402

NAMESPACE = "noaa-weather"
CACHE_TYPE = "climate-summary"

# Same rationale as fetch_station_csv.BULK_FETCH_THRESHOLD — even if CSVs are
# cached, unfiltered catalog expansion is almost always a user mistake.
BULK_PROCESS_THRESHOLD = 500


def _read_stations_file(path: Path) -> list[str]:
    ids: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ids.append(line)
    return ids


def _resolve_from_catalog(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    """Mirror fetch_station_csv: filter the catalog and return (id, metadata).

    The metadata dict is the source-lineage context stamped into each
    summary's sidecar, so downstream readers can tell which discovery
    filter produced the summary without re-running it.
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
    region_info = None
    if args.region:
        try:
            region_info = geofabrik_regions.resolve_region(
                args.region, use_mock=args.use_mock or None
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        bbox = region_info.bbox

    country_filter = args.country
    if args.region and not args.country_explicit:
        country_filter = ""

    cap = args.max_stations if args.max_stations > 0 else len(stations)
    filtered = ghcn_parse.filter_stations(
        stations,
        inventory,
        country=country_filter,
        state=args.state_filter,
        bbox=bbox,
        max_stations=cap,
        min_years=args.min_years,
        required_elements=args.required,
    )

    discovery: dict[str, Any] = {
        "country": country_filter,
        "state": args.state_filter,
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


def _summarize_one(
    station_id: str,
    *,
    station_meta: dict[str, Any],
    state_tag: str,
    start_year: int,
    end_year: int,
    force_download: bool,
    use_mock: bool,
) -> tuple[dict[str, Any], ghcn_download.DownloadResult]:
    """Download (if needed), parse, and summarize one station."""
    res = ghcn_download.download_station_csv(
        station_id,
        force=force_download,
        use_mock=use_mock or None,
        extra_metadata=station_meta or None,
    )
    daily = ghcn_parse.parse_ghcn_csv(res.absolute_path, start_year, end_year)
    summaries = climate_analysis.compute_yearly_summaries(
        daily, station_id=station_id, state=state_tag
    )

    output: dict[str, Any] = {
        "station_id": station_id,
        "state": state_tag,
        "start_year": start_year,
        "end_year": end_year,
        "years_analyzed": len(summaries),
        "summaries": summaries,
        "source": {
            "cache_type": ghcn_download.STATION_CSV_CACHE_TYPE,
            "relative_path": res.relative_path,
            "sha256": res.sha256,
        },
    }
    # Pass through the catalog-derived metadata so summary consumers see the
    # same name/lat/lon/year-range the CSV sidecar got.
    if station_meta.get("name"):
        output["station_name"] = station_meta["name"]
    for k in ("lat", "lon", "elevation", "first_year", "last_year"):
        if station_meta.get(k) is not None:
            output[k] = station_meta[k]
    return output, res


def _write_summary_to_cache(
    output: dict[str, Any],
    *,
    station_id: str,
    station_meta: dict[str, Any],
    csv_sha: str,
) -> str:
    relative_path = f"{station_id}.json"
    storage = LocalStorage()

    body = json.dumps(output, indent=2, sort_keys=True) + "\n"
    body_bytes = body.encode("utf-8")

    staging_dir = sidecar.staging_dir(NAMESPACE, CACHE_TYPE, storage)
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{station_id}.json.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body_bytes)

    extra: dict[str, Any] = {
        "station_id": station_id,
        "years_analyzed": output["years_analyzed"],
    }
    for k in ("name", "lat", "lon", "elevation", "first_year", "last_year"):
        v = station_meta.get(k)
        if v is not None:
            extra[k] = v
    if "discovery" in station_meta:
        extra["discovery"] = station_meta["discovery"]

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body_bytes),
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            source={
                "namespace": NAMESPACE,
                "cache_type": ghcn_download.STATION_CSV_CACHE_TYPE,
                "relative_path": f"{station_id}.csv",
                "sha256": csv_sha,
            },
            tool={"name": "summarize_station", "version": "1.0"},
            extra=extra,
            storage=storage,
        )
    return final_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        "station_ids",
        nargs="*",
        help="GHCN station IDs. Omit and use --from-catalog or --stations-file for bulk.",
    )
    parser.add_argument(
        "--stations-file",
        type=Path,
        help="File with one station_id per line (# for comments).",
    )

    # Tagging / year range — apply per-station.
    parser.add_argument(
        "--state",
        default="",
        help=(
            "State code tagged into every summary output (e.g. 'NY'). "
            "In catalog-driven mode, also defaults the --state-filter unless "
            "--state-filter is passed separately."
        ),
    )
    parser.add_argument("--start-year", type=int, default=1944)
    parser.add_argument("--end-year", type=int, default=2024)

    # --- Catalog-driven bulk mode -------------------------------------------
    catalog_group = parser.add_argument_group(
        "catalog-driven mode",
        "Resolve the station list from the cached catalog + inventory. "
        "Flags mirror discover-stations / fetch-station-csv.",
    )
    catalog_group.add_argument(
        "--from-catalog",
        action="store_true",
        help="Summarize every station in the catalog that matches the filter.",
    )
    catalog_group.add_argument(
        "--region",
        default="",
        help=(
            "Geofabrik region path (e.g. 'europe/germany'). "
            "Implies --from-catalog; overrides --country unless --country is explicit."
        ),
    )
    catalog_group.add_argument("--country", default="US", help="FIPS country code (default: US).")
    catalog_group.add_argument(
        "--state-filter",
        default=None,
        help=(
            "State code used to FILTER the catalog (bounding-box match). "
            "Defaults to --state if not passed."
        ),
    )
    catalog_group.add_argument(
        "--min-years", type=int, default=20, help="Minimum years of coverage (default: 20)."
    )
    catalog_group.add_argument(
        "--required",
        action="append",
        default=None,
        help="Required element (repeatable). Default: TMAX TMIN PRCP.",
    )
    catalog_group.add_argument(
        "--max-stations", type=int, default=0, help="Cap on stations. 0 = no cap."
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
            f"Override the {BULK_PROCESS_THRESHOLD}-station safety guard "
            "for catalog-driven expansion."
        ),
    )

    # --- Per-invocation options ---------------------------------------------
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download the CSV even if cached.",
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
        "--write-cache",
        action="store_true",
        help=(
            "Persist the summary JSON to cache/noaa-weather/climate-summary/. "
            "Automatically enabled in bulk mode (>1 station)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved station list without summarizing.",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="Python logging level (default: INFO)."
    )
    args = parser.parse_args()

    args.country_explicit = any(
        a == "--country" or a.startswith("--country=") for a in sys.argv[1:]
    )
    if args.state_filter is None:
        args.state_filter = args.state

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    # --region alone implies --from-catalog.
    if args.region and not args.from_catalog:
        args.from_catalog = True

    targets: list[tuple[str, dict[str, Any]]] = []
    for sid in args.station_ids:
        targets.append((sid, {}))
    if args.stations_file:
        for sid in _read_stations_file(args.stations_file):
            targets.append((sid, {}))
    if args.from_catalog:
        targets.extend(_resolve_from_catalog(args))

    if not targets:
        parser.error(
            "no stations resolved — pass positional IDs, --stations-file, or --from-catalog."
        )

    # Deduplicate, preferring entries that carry metadata.
    seen: dict[str, dict[str, Any]] = {}
    for sid, meta in targets:
        if sid not in seen:
            seen[sid] = meta
        elif not seen[sid] and meta:
            seen[sid] = meta
    targets = list(seen.items())

    # Scale guard — only trips in catalog-driven bulk mode.
    if (
        args.from_catalog
        and len(targets) > BULK_PROCESS_THRESHOLD
        and not args.i_know_this_is_huge
    ):
        print(
            f"error: --from-catalog resolved to {len(targets):,} stations, above the "
            f"{BULK_PROCESS_THRESHOLD}-station safety threshold.\n"
            f"       Narrow with --country / --state-filter / --min-years / --max-stations, "
            f"or pass --i-know-this-is-huge to bypass.",
            file=sys.stderr,
        )
        return 2

    is_bulk = len(targets) > 1
    # In bulk mode, unconditionally persist — stdout can't carry N JSON blobs
    # usefully anyway, and the region-trend tool needs the cache populated.
    write_cache = args.write_cache or is_bulk

    if args.dry_run:
        print(f"# dry-run: would summarize {len(targets):,} station(s)")
        for sid, meta in targets:
            yr = ""
            if "first_year" in meta and "last_year" in meta:
                yr = f"  inv-years={meta['first_year']}-{meta['last_year']}"
            name = f"  {meta['name']}" if meta.get("name") else ""
            print(f"{sid}{yr}{name}")
        return 0

    if is_bulk:
        print(f"# summarizing {len(targets):,} station(s)", file=sys.stderr)

    failures: list[str] = []
    t0 = time.monotonic()
    for idx, (sid, meta) in enumerate(targets, 1):
        try:
            output, res = _summarize_one(
                sid,
                station_meta=meta,
                state_tag=args.state,
                start_year=args.start_year,
                end_year=args.end_year,
                force_download=args.force_download,
                use_mock=args.use_mock,
            )
        except Exception as exc:
            print(f"error: {sid}: {exc}", file=sys.stderr)
            failures.append(sid)
            continue

        if write_cache:
            path = _write_summary_to_cache(
                output, station_id=sid, station_meta=meta, csv_sha=res.sha256
            )
            prefix = f"[{idx}/{len(targets)}] " if is_bulk else ""
            name = f"  {meta['name']}" if meta.get("name") else ""
            print(
                f"{prefix}[summary] {sid}  years={output['years_analyzed']}{name}  {path}"
            )
        else:
            # Single-station, no --write-cache: emit the JSON.
            json.dump(output, sys.stdout, indent=2)
            sys.stdout.write("\n")

    if is_bulk:
        elapsed = time.monotonic() - t0
        ok = len(targets) - len(failures)
        print(
            f"# done: {ok:,} summarized, {len(failures):,} failed in {elapsed:.1f}s",
            file=sys.stderr,
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
