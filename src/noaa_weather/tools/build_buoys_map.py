"""Stitch cached NDBC station data into a MapLibre points map.

Reads ``ndbc-catalog/stations.json`` (written by
``download-ndbc-catalog``) and any cached per-station summaries
(``buoy-summaries/``) and emits
``$AFL_CACHE_ROOT/noaa-weather/ndbc-catalog/buoys-map.html``.

Usage::

    python build_buoys_map.py
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import ndbc_map  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    out = ndbc_map.rebuild_buoys_map()
    if out is None:
        print(
            "error: no cached NDBC catalog — run ./download-ndbc-catalog.sh first.",
            file=sys.stderr,
        )
        return 1
    print(f"[buoys-map] {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
