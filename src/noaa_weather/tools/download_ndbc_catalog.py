"""Download the NDBC active-stations catalog.

Writes two artifacts under ``$AFL_CACHE_ROOT/noaa-weather/ndbc-catalog/``:

- ``activestations.xml`` — the upstream verbatim
- ``stations.json``      — normalized ``{station_id, name, type, owner,
                           lat, lon, met, currents, waterquality, dart}``

Each has its own sidecar. The XML lineage and record count are recorded
so downstream tools can detect upstream changes without re-parsing.

Usage::

    python download_ndbc_catalog.py
    python download_ndbc_catalog.py --force
    python download_ndbc_catalog.py --use-mock      # offline deterministic
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import ndbc_download  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--force", action="store_true", help="Re-download.")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=ndbc_download.CATALOG_DEFAULT_MAX_AGE_HOURS,
    )
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

    try:
        res = ndbc_download.download_catalog(
            force=args.force,
            max_age_hours=args.max_age_hours,
            use_mock=args.use_mock,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
    print(
        f"[{status}] ndbc-catalog/activestations.xml  "
        f"{res.station_count:,} stations  → {res.xml_path}"
    )
    print(f"          ndbc-catalog/stations.json      → {res.json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
