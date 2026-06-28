"""Compute yearly climate summaries for one or more NDBC buoys.

Reads cached ``ndbc-stdmet/<id>/<year>.txt.gz`` files, downsamples
hourly → daily, and emits per-year means of air-temp, sea-temp,
pressure, wind speed, wave height, plus daily counts of high-SST /
storm days.

Outputs land at
``$FW_CACHE_ROOT/noaa-weather/buoy-summaries/<station_id>.json`` with
a sibling ``.meta.json`` sidecar.

Usage::

    # Single station, all cached years
    python summarize_buoy.py 46042

    # Explicit year range
    python summarize_buoy.py 46042 --start-year 2015 --end-year 2024

    # Bulk — every station with cached stdmet
    python summarize_buoy.py --from-cache
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import os
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _noaa_tools import ndbc_download, ndbc_parse, sidecar  # noqa: E402
from _noaa_tools.storage import get_storage, local_staging_subdir  # noqa: E402

NAMESPACE = "noaa-weather"
STDMET_CACHE_TYPE = ndbc_download.STDMET_CACHE_TYPE
OUTPUT_CACHE_TYPE = "buoy-summaries"

# Thresholds for interesting-day counts.
HIGH_SST_C = 28.0     # rough tropical-warm threshold
STORM_WAVE_M = 4.0    # rough "rough seas" threshold (WMO storm at sea > 9 m)


def _cached_stdmet_years(station_id: str) -> list[int]:
    """Years with a cached stdmet file for the station (local or s3)."""
    rels = sidecar.list_relative_paths(
        NAMESPACE, STDMET_CACHE_TYPE, under=station_id, storage=get_storage()
    )
    years: list[int] = []
    for rel in rels:
        name = rel.rsplit("/", 1)[-1]
        if not name.endswith(".txt.gz"):
            continue
        try:
            years.append(int(name[: -len(".txt.gz")]))
        except ValueError:
            continue
    return sorted(years)


def _all_cached_stations() -> list[str]:
    """Station IDs that have any cached stdmet (local or s3)."""
    rels = sidecar.list_relative_paths(NAMESPACE, STDMET_CACHE_TYPE, storage=get_storage())
    # relative_path is "<station_id>/<year>.txt.gz" → first segment is the station.
    return sorted({rel.split("/", 1)[0] for rel in rels if "/" in rel})


def _yearly_summary(
    station_id: str,
    year: int,
    stdmet_path: Path,
) -> dict[str, Any] | None:
    """Return one yearly summary dict from a stdmet .txt.gz, or None."""
    try:
        hourly = ndbc_parse.parse_stdmet_gz(str(stdmet_path))
    except (OSError, gzip.BadGzipFile, ValueError) as exc:
        logging.getLogger("summarize-buoy").warning(
            "skipping %s: %s", stdmet_path, exc
        )
        return None
    if not hourly:
        return None
    daily = ndbc_parse.daily_from_hourly(hourly)
    if not daily:
        return None

    # Per-year aggregates.
    def _mean(field: str) -> float | None:
        vals = [d[field] for d in daily if d.get(field) is not None]
        if not vals:
            return None
        return round(statistics.fmean(vals), 2)

    ssts = [d["sea_temp"] for d in daily if d.get("sea_temp") is not None]
    waves = [d["wave_height"] for d in daily if d.get("wave_height") is not None]

    return {
        "station_id": station_id,
        "year": year,
        "daily_count": len(daily),
        "air_temp_mean": _mean("air_temp"),
        "sea_temp_mean": _mean("sea_temp"),
        "sea_temp_max": round(max(ssts), 2) if ssts else None,
        "pressure_mean": _mean("pressure"),
        "wind_speed_mean": _mean("wind_speed"),
        "wave_height_mean": _mean("wave_height"),
        "wave_height_max": round(max(waves), 2) if waves else None,
        "high_sst_days": sum(1 for t in ssts if t > HIGH_SST_C),
        "storm_days": sum(1 for w in waves if w > STORM_WAVE_M),
    }


def _write_summary_cache(
    station_id: str,
    summaries: list[dict[str, Any]],
    station_meta: dict[str, Any],
) -> str:
    relative_path = f"{station_id}.json"
    storage = get_storage()
    output = {
        "station_id": station_id,
        "station_meta": station_meta,
        "years_analyzed": len(summaries),
        "summaries": summaries,
    }
    body = (json.dumps(output, indent=2, sort_keys=True) + "\n").encode("utf-8")

    staging_dir = local_staging_subdir(f"{NAMESPACE}/{OUTPUT_CACHE_TYPE}")  # always local
    stage_path = os.path.join(staging_dir, f"{station_id}.json.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    # NOT Path(): an s3:// URI would collapse to s3:/ under pathlib normalization.
    final_path = sidecar.cache_path(NAMESPACE, OUTPUT_CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, OUTPUT_CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            OUTPUT_CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "namespace": NAMESPACE,
                "cache_type": STDMET_CACHE_TYPE,
                "relative_path_prefix": f"{station_id}/",
            },
            tool={"name": "summarize_buoy", "version": "1.0"},
            extra={
                "station_id": station_id,
                "years_analyzed": len(summaries),
                **{k: station_meta.get(k) for k in ("name", "type", "lat", "lon")},
            },
            storage=storage,
        )
    return final_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("station_ids", nargs="*", help="NDBC station IDs.")
    parser.add_argument(
        "--from-cache",
        action="store_true",
        help="Summarize every station with cached stdmet data.",
    )
    parser.add_argument(
        "--start-year",
        type=int,
        default=0,
        help="First year to include (0 = every cached year).",
    )
    parser.add_argument(
        "--end-year",
        type=int,
        default=0,
        help="Last year to include (0 = every cached year).",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    ids: list[str] = list(args.station_ids)
    if args.from_cache:
        ids.extend(_all_cached_stations())
    if not ids:
        parser.error(
            "no station IDs — pass them positionally or use --from-cache."
        )

    # Dedup, preserve order.
    seen: set[str] = set()
    ids = [s for s in ids if not (s in seen or seen.add(s))]

    # Load catalog once (cheap JSON read) so we can annotate each
    # summary with the station's name / type / lat / lon.
    station_meta: dict[str, dict[str, Any]] = {}
    try:
        stations = ndbc_download.read_catalog_stations()
        for s in stations:
            station_meta[s["station_id"]] = s
    except Exception as exc:
        logging.getLogger("summarize-buoy").warning(
            "couldn't load station metadata: %s", exc
        )

    failures: list[tuple[str, str]] = []
    successes = 0
    for station_id in ids:
        years = _cached_stdmet_years(station_id)
        if args.start_year:
            years = [y for y in years if y >= args.start_year]
        if args.end_year:
            years = [y for y in years if y <= args.end_year]
        if not years:
            print(
                f"[skip] {station_id}: no cached stdmet years in range",
                file=sys.stderr,
            )
            failures.append((station_id, "no cached years"))
            continue

        summaries: list[dict[str, Any]] = []
        _storage = get_storage()
        for year in years:
            rel = ndbc_download.stdmet_relative_path(station_id, year)
            art = sidecar.cache_path(NAMESPACE, STDMET_CACHE_TYPE, rel, _storage)
            path = Path(_storage.localize(art))  # s3:// -> local file; local -> itself
            summary = _yearly_summary(station_id, year, path)
            if summary is not None:
                summaries.append(summary)

        if not summaries:
            print(f"[skip] {station_id}: empty summary", file=sys.stderr)
            failures.append((station_id, "empty summary"))
            continue

        out_path = _write_summary_cache(
            station_id, summaries, station_meta.get(station_id, {})
        )
        print(
            f"[summary] {station_id}  {len(summaries)} year(s) "
            f"→ {out_path}"
        )
        successes += 1

    print(
        f"# done: {successes} ok, {len(failures)} skipped/failed",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
