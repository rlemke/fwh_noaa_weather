"""Detect extreme weather events for one or more GHCN stations.

Downloads (if needed) each station's GHCN-Daily CSV, parses it to the requested
year range, and detects heat waves / cold snaps / wet & dry spells / heavy rain
& snow days using the supplied (or default) thresholds. Emits the event catalog
+ per-type counts + decadal frequency as JSON on stdout.

Backed by ``_noaa_tools.extremes`` — the same library the
``weather.Extremes.DetectStationExtremes`` handler uses.

Usage::

    # One station, default thresholds
    python detect_extremes.py USW00094728

    # Custom heat-wave definition + year range
    python detect_extremes.py USW00094728 --start-year 1960 --end-year 2024 \\
        --heat-wave-tmax-c 38 --heat-wave-min-days 2

    # Offline deterministic data (no NOAA fetch)
    python detect_extremes.py USW00094728 --use-mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _noaa_tools import extremes, ghcn_download, ghcn_parse  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("station_ids", nargs="+", help="GHCN station IDs (e.g. USW00094728).")
    p.add_argument("--start-year", type=int, default=1944)
    p.add_argument("--end-year", type=int, default=2026)

    thr = p.add_argument_group("event thresholds", "All optional; defaults are common conventions.")
    thr.add_argument("--heat-wave-tmax-c", type=float, default=35.0,
                     help="Daily high (°C) that counts as a heat-wave day (default 35).")
    thr.add_argument("--heat-wave-min-days", type=int, default=3,
                     help="Consecutive hot days to qualify as a heat wave (default 3).")
    thr.add_argument("--cold-snap-tmin-c", type=float, default=-10.0,
                     help="Daily low (°C) that counts as a cold-snap day (default -10).")
    thr.add_argument("--cold-snap-min-days", type=int, default=3,
                     help="Consecutive cold days to qualify as a cold snap (default 3).")
    thr.add_argument("--heavy-rain-mm", type=float, default=50.0,
                     help="Single-day rainfall (mm) that counts as heavy rain (default 50).")
    thr.add_argument("--wet-day-mm", type=float, default=1.0,
                     help="Rainfall (mm) marking a day as 'wet' for spell detection (default 1).")
    thr.add_argument("--wet-spell-min-days", type=int, default=5,
                     help="Consecutive wet days to qualify as a wet spell (default 5).")
    thr.add_argument("--dry-spell-min-days", type=int, default=21,
                     help="Consecutive dry days to qualify as a dry spell (default 21).")
    thr.add_argument("--heavy-snow-mm", type=float, default=100.0,
                     help="Single-day snowfall (mm) that counts as heavy snow (default 100).")

    p.add_argument("--use-mock", action="store_true",
                   help="Use deterministic mock data instead of a live NOAA fetch.")
    p.add_argument("--force-download", action="store_true", help="Re-download the CSV even if cached.")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s %(name)s %(message)s", stream=sys.stderr)

    config = extremes.ExtremeConfig(
        heat_wave_tmax_c=args.heat_wave_tmax_c, heat_wave_min_days=args.heat_wave_min_days,
        cold_snap_tmin_c=args.cold_snap_tmin_c, cold_snap_min_days=args.cold_snap_min_days,
        heavy_rain_mm=args.heavy_rain_mm, wet_day_mm=args.wet_day_mm,
        wet_spell_min_days=args.wet_spell_min_days, dry_spell_min_days=args.dry_spell_min_days,
        heavy_snow_mm=args.heavy_snow_mm,
    )

    failures = []
    out = []
    for sid in args.station_ids:
        try:
            res = ghcn_download.download_station_csv(
                sid, force=args.force_download, use_mock=args.use_mock or None)
            daily = ghcn_parse.parse_ghcn_csv(res.absolute_path, args.start_year, args.end_year)
            result = extremes.detect_events(daily, config)
            result["station_id"] = sid
            print(extremes.summarize(result, label=sid), file=sys.stderr)
            out.append(result)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {sid}: {exc}", file=sys.stderr)
            failures.append(sid)

    json.dump(out[0] if len(out) == 1 else out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
