"""Aggregate per-station summaries into a regional climate trend.

Reads cached climate summaries for every station in a region (either
from ``cache/noaa-weather/climate-summary/`` if ``--from-cache`` is
set, or from one or more summary JSON files on the command line) and
computes a single trend document: warming rate per decade, precip
change %, per-decade averages, narrative sentence.

Usage::

    # Pass summary files directly
    python compute_region_trend.py --state NY --start-year 1950 --end-year 2020 \\
        summary1.json summary2.json ...

    # Pull every station summary for a state out of the shared cache
    python compute_region_trend.py --state NY --start-year 1950 --end-year 2020 \\
        --from-cache

    # Persist the trend to cache/noaa-weather/region-trend/<country>/<state>.json
    python compute_region_trend.py --state NY --from-cache --write-cache
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import climate_analysis, sidecar  # noqa: E402
from _lib.storage import LocalStorage  # noqa: E402

NAMESPACE = "noaa-weather"
SUMMARY_CACHE_TYPE = "climate-summary"
TREND_CACHE_TYPE = "region-trend"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("summary_files", nargs="*", help="Per-station summary JSON files.")
    parser.add_argument("--country", default="US", help="FIPS country code (default: US).")
    parser.add_argument("--state", default="", help="Region label (state / country name).")
    parser.add_argument("--start-year", type=int, default=1944)
    parser.add_argument("--end-year", type=int, default=2024)
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Load every summary under cache/noaa-weather/climate-summary/ "
        "matching the state (rather than reading files from CLI).",
    )
    parser.add_argument(
        "--write-cache",
        action="store_true",
        help="Write the trend to cache/noaa-weather/region-trend/<country>/<state>.json.",
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

    summaries = _collect_summaries(args)
    if not summaries:
        print("error: no summary records to aggregate", file=sys.stderr)
        return 1

    # Filter to the requested year window here in case the input files
    # covered wider ranges. The region aggregator doesn't reject out-of-range
    # years on its own — we want the caller's window to be authoritative.
    filtered = [
        r for r in summaries
        if isinstance(r.get("year"), int)
        and args.start_year <= r["year"] <= args.end_year
    ]
    trend = climate_analysis.aggregate_region_trend(
        filtered,
        state=args.state,
        start_year=args.start_year,
        end_year=args.end_year,
    )

    if args.write_cache:
        _write_trend_to_cache(trend, country=args.country, state=args.state)

    json.dump(trend, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _collect_summaries(args: argparse.Namespace) -> list[dict]:
    """Return flattened per-year records from all input sources."""
    yearly: list[dict] = []

    if args.from_cache:
        storage = LocalStorage()
        entries = sidecar.list_entries(NAMESPACE, SUMMARY_CACHE_TYPE, storage)
        for entry in entries:
            rel = entry.get("relative_path", "")
            path = sidecar.cache_path(NAMESPACE, SUMMARY_CACHE_TYPE, rel, storage)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, json.JSONDecodeError) as exc:
                logging.warning("Skipping corrupt summary %s: %s", path, exc)
                continue
            if args.state and doc.get("state") != args.state:
                continue
            yearly.extend(doc.get("summaries") or [])

    for p in args.summary_files:
        try:
            with open(p, "r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Skipping %s: %s", p, exc)
            continue
        yearly.extend(doc.get("summaries") or [])

    return yearly


def _write_trend_to_cache(trend: dict, *, country: str, state: str) -> None:
    region_key = state or "ALL"
    relative_path = f"{country}/{region_key}.json"
    storage = LocalStorage()

    body = json.dumps(trend, indent=2, sort_keys=True) + "\n"
    body_bytes = body.encode("utf-8")

    staging_dir = sidecar.staging_dir(NAMESPACE, TREND_CACHE_TYPE, storage)
    os.makedirs(staging_dir, exist_ok=True)
    stage_name = f"{country}_{region_key}.json.stage-{os.getpid()}"
    stage_path = os.path.join(staging_dir, stage_name)
    with open(stage_path, "wb") as f:
        f.write(body_bytes)

    final_path = sidecar.cache_path(NAMESPACE, TREND_CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, TREND_CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            TREND_CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body_bytes),
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            tool={"name": "compute_region_trend", "version": "1.0"},
            extra={
                "country": country,
                "state": state,
                "years_with_data": len(trend.get("years_data", [])),
                "warming_rate_per_decade": trend.get("warming_rate_per_decade"),
            },
            storage=storage,
        )
    print(f"[cache] wrote {final_path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
