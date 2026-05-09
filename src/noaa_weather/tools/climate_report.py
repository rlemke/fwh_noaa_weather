"""Generate a regional climate report: JSON + Markdown + HTML + SVG charts.

Thin CLI over :func:`_lib.climate_report.generate_climate_report`. Both
this tool and the FFL handler ``weather.Report.GenerateClimateReport``
call the same core function, so the terminal run and the runtime
produce identical output and share the same cache.

Output bundle at ``cache/noaa-weather/climate-report/<country>/<region>/``:

  report.json / report.md / report.html + 5 SVG charts
  (climograph, annual_trend, warming_stripes, heatmap, anomaly_bars)

Usage::

    climate-report.sh --country US --state NY --start-year 1950 --end-year 2026
    climate-report.sh --region europe/germany --start-year 1950 --end-year 2026
    climate-report.sh --region europe/germany --dry-run
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import geofabrik_regions, ghcn_download, ghcn_parse  # noqa: E402
from _lib.climate_report import (  # noqa: E402
    DEFAULT_BASELINE,
    DEFAULT_BULK_THRESHOLD,
    ReportError,
    generate_climate_report,
    rebuild_report_derived_pages,
)
from _lib.storage import LocalStorage  # noqa: E402


def _parse_baseline(s: str) -> tuple[int, int]:
    try:
        start_s, end_s = s.split("-")
        start, end = int(start_s), int(end_s)
    except (ValueError, TypeError):
        raise SystemExit(f"error: --baseline must be START-END, got {s!r}")
    if start >= end:
        raise SystemExit(f"error: --baseline start must be < end (got {s!r})")
    return start, end


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("--country", default="US", help="FIPS country code (default: US).")
    parser.add_argument("--state", default="", help="State code (tags + bbox-filters).")
    parser.add_argument(
        "--region",
        default="",
        help="Geofabrik region path (overrides country filter unless explicit).",
    )
    parser.add_argument(
        "--all-under",
        default="",
        metavar="PREFIX",
        help=(
            "Expand into every Geofabrik region under a prefix. "
            "Example: --all-under north-america/canada reports on "
            "every province. Combine with --include-parents to also "
            "include the prefix region itself."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Expand into EVERY Geofabrik region in the cached index. "
            "Equivalent to --all-under \"\" (empty prefix, matches any "
            "path). Heads up: ~40,000 regions × per-region catalog + "
            "filter overhead is a multi-day run even with --jobs 4, "
            "and most regions have no matching GHCN stations. Combine "
            "with --include-parents to also include continent / "
            "country-level aggregates."
        ),
    )
    parser.add_argument(
        "--include-parents",
        action="store_true",
        help=(
            "With --all-under, also report on the prefix region and "
            "every intermediate level (not just leaves)."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help=(
            "Print the resolved Geofabrik region set and exit without "
            "generating reports. Use with --all-under to preview."
        ),
    )
    parser.add_argument("--min-years", type=int, default=20)
    parser.add_argument(
        "--required",
        action="append",
        default=None,
        help="Required element (repeatable). Default: TMAX TMIN PRCP.",
    )
    parser.add_argument("--max-stations", type=int, default=0, help="Cap on stations. 0 = no cap.")
    parser.add_argument("--start-year", type=int, default=1950)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument(
        "--baseline",
        default=f"{DEFAULT_BASELINE[0]}-{DEFAULT_BASELINE[1]}",
        help=(
            f"Normals baseline window, inclusive. Format: START-END. "
            f"Default WMO standard: {DEFAULT_BASELINE[0]}-{DEFAULT_BASELINE[1]}."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Write outputs here instead of the namespaced cache path.",
    )
    parser.add_argument("--force-catalog", action="store_true")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--use-mock", action="store_true")
    parser.add_argument(
        "--i-know-this-is-huge",
        action="store_true",
        help=f"Override the {DEFAULT_BULK_THRESHOLD}-station safety guard.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List the stations that would feed the report, but don't aggregate.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        metavar="N",
        help=(
            "Run multi-region batches with up to N regions in parallel. "
            "Default: 4. Set to 1 to force serial. Ignored for single-region runs."
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    baseline = _parse_baseline(args.baseline)
    country_explicit = any(
        a == "--country" or a.startswith("--country=") for a in sys.argv[1:]
    )

    # --- Resolve which region(s) to report on --------------------------------
    regions = _resolve_region_set(args)

    if args.list:
        return _print_region_list(regions)

    # Dry-run — show the stations that would feed each report without
    # actually generating charts / writing bundles.
    if args.dry_run:
        return _dry_run(args, country_explicit, regions)

    # --- Generate a report per resolved region ------------------------------
    is_batch = len(regions) > 1
    jobs = max(1, int(args.jobs or 1))

    def _label(region: str) -> str:
        return region if region else (
            f"{args.country}/{args.state}" if args.state else (args.country or "ALL")
        )

    def _run_one(region: str) -> tuple[str, Exception | None, str]:
        """Generate a single report; return (label, err, output_dir)."""
        try:
            bundle = generate_climate_report(
                country=args.country,
                state=args.state,
                region=region,
                start_year=args.start_year,
                end_year=args.end_year,
                baseline=baseline,
                min_years=args.min_years,
                required_elements=args.required,
                max_stations=args.max_stations,
                force_catalog=args.force_catalog,
                force_download=args.force_download,
                use_mock=args.use_mock or None,
                # In batch mode every region writes to its canonical
                # cache dir — --output-dir applies only when there's
                # exactly one region to generate.
                output_dir=args.output_dir if not is_batch else None,
                override_bulk_guard=args.i_know_this_is_huge,
                country_explicit=country_explicit,
                # Batch runs rebuild the master index + warming map
                # once at the end instead of N times (those regens
                # walk the whole climate-report tree and would clobber
                # each other if run concurrently).
                refresh_index=not is_batch,
            )
        except (KeyError, ReportError) as exc:
            return _label(region), exc, ""
        return _label(region), None, str(bundle.output_dir)

    failures: list[tuple[str, str]] = []
    successes: list[str] = []
    t0 = time.monotonic()

    if not is_batch or jobs == 1:
        # Serial path — keeps stdout ordering readable for single-region
        # and explicit --jobs 1 runs.
        for idx, region in enumerate(regions, 1):
            if is_batch:
                print(
                    f"\n=== [{idx}/{len(regions)}] {_label(region)} ===",
                    file=sys.stderr,
                )
            label, err, out_dir = _run_one(region)
            if err is not None:
                print(f"[fail] {label}: {err}", file=sys.stderr)
                failures.append((label, str(err)))
            else:
                print(f"[report] {out_dir}")
                successes.append(label)
    else:
        # Parallel path — up to `jobs` regions in flight. Output
        # interleaves across workers but each line still prefixes with
        # the region label so the log remains greppable.
        print(
            f"# batch: {len(regions)} regions, up to {jobs} in parallel",
            file=sys.stderr,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            fut_to_region = {
                pool.submit(_run_one, region): region for region in regions
            }
            done = 0
            for fut in concurrent.futures.as_completed(fut_to_region):
                done += 1
                label, err, out_dir = fut.result()
                prefix = f"[{done}/{len(regions)}]"
                if err is not None:
                    print(f"{prefix} [fail] {label}: {err}", file=sys.stderr)
                    failures.append((label, str(err)))
                else:
                    print(f"{prefix} [report] {out_dir}")
                    successes.append(label)

    # --- Post-batch: rebuild derived pages once, print summary --------------
    if is_batch:
        try:
            rebuild_report_derived_pages(storage=LocalStorage())
        except Exception as exc:  # pragma: no cover — defensive
            print(
                f"warning: master index / warming map regen failed: {exc}",
                file=sys.stderr,
            )
        elapsed = time.monotonic() - t0
        print(
            f"\n# done: {len(successes)} ok, {len(failures)} failed "
            f"in {elapsed:.1f}s",
            file=sys.stderr,
        )
        if failures:
            print("# failures:", file=sys.stderr)
            for label, err in failures:
                print(f"#   {label}: {err}", file=sys.stderr)

    return 1 if failures else 0


def _resolve_region_set(args: argparse.Namespace) -> list[str]:
    """Return the list of Geofabrik region paths this invocation covers.

    Semantics:
      - ``--all-under`` wins when set, optionally combined with ``--region``
        (the single region is appended to the expanded set).
      - ``--region`` alone → single-element list.
      - Neither → empty string marker (legacy country/state mode); the
        caller loops exactly once with no Geofabrik region.
    """
    if args.all:
        # Every region in the Geofabrik index. Includes parents by
        # definition — you can't have leaves without their ancestors.
        expanded = geofabrik_regions.list_regions_under(
            "",
            include_parents=True,
            use_mock=args.use_mock or None,
        )
        if args.region and args.region not in expanded:
            expanded.append(args.region)
            expanded.sort()
        if not expanded:
            print(
                "error: --all resolved to zero regions — is the "
                "Geofabrik index cached?",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return expanded

    if args.all_under:
        try:
            expanded = geofabrik_regions.list_regions_under(
                args.all_under,
                include_parents=args.include_parents,
                use_mock=args.use_mock or None,
            )
        except KeyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(2)
        if args.region and args.region not in expanded:
            expanded.append(args.region)
            expanded.sort()
        if not expanded:
            print(
                f"error: --all-under {args.all_under!r} matched zero "
                f"regions in the Geofabrik index",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return expanded
    if args.region:
        return [args.region]
    # Legacy country/state mode — single iteration with no Geofabrik region.
    return [""]


def _print_region_list(regions: list[str]) -> int:
    """Print the resolved region set to stdout (one per line)."""
    print(f"# resolved {len(regions)} region(s):")
    for r in regions:
        if r:
            print(r)
        else:
            print("(country/state mode — no Geofabrik region)")
    return 0


def _dry_run(
    args: argparse.Namespace,
    country_explicit: bool,
    regions: list[str],
) -> int:
    """For each resolved region, list the stations that would feed it."""
    # Load catalog + inventory once, reuse across regions.
    stations_text = ghcn_download.read_catalog_file(
        "stations", force=args.force_catalog, use_mock=args.use_mock or None
    )
    inventory_text = ghcn_download.read_catalog_file(
        "inventory", force=args.force_catalog, use_mock=args.use_mock or None
    )
    stations = ghcn_parse.parse_stations(stations_text)
    inventory = ghcn_parse.parse_inventory(inventory_text)
    cap = args.max_stations if args.max_stations > 0 else len(stations)

    grand_total = 0
    for region in regions:
        country_filter = args.country
        if region and not country_explicit:
            country_filter = ""
        bbox = None
        if region:
            try:
                region_info = geofabrik_regions.resolve_region(
                    region, use_mock=args.use_mock or None
                )
            except KeyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            bbox = region_info.bbox
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
        label = region or (
            f"{args.country}/{args.state}" if args.state else (args.country or "ALL")
        )
        print(f"# dry-run {label}: {len(filtered):,} station(s)")
        for s in filtered:
            print(
                f"  {s['station_id']}  {s.get('name', '')}  "
                f"inv-years={s.get('first_year')}-{s.get('last_year')}"
            )
        grand_total += len(filtered)

    if len(regions) > 1:
        print(
            f"# total: {len(regions)} region(s), {grand_total:,} station(s)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
