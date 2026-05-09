"""Download the two GHCN-Daily catalog files into the shared cache.

Outputs land at::

    $AFL_CACHE_ROOT/noaa-weather/catalog/stations.txt   + .meta.json
    $AFL_CACHE_ROOT/noaa-weather/catalog/inventory.txt  + .meta.json

Usage::

    python download_ghcn_catalog.py                 # both files (default)
    python download_ghcn_catalog.py --only stations # just ghcnd-stations.txt
    python download_ghcn_catalog.py --force         # re-download even if cached
    python download_ghcn_catalog.py --max-age-hours 168

Requires network unless ``requests`` is unavailable, in which case
deterministic mock data is written (useful for offline tests).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lib import ghcn_download  # noqa: E402

CHOICES = list(ghcn_download.CATALOG_FILES)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--only",
        choices=CHOICES,
        help=f"Download only one file (default: both {CHOICES!r}).",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-download even if the cache is current."
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=ghcn_download.DEFAULT_CATALOG_MAX_AGE_HOURS,
        help=f"Cache freshness window (default: {ghcn_download.DEFAULT_CATALOG_MAX_AGE_HOURS})",
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help=(
            "Opt in to deterministic mock data instead of a live NOAA fetch. "
            "Default is to fetch real data; if requests is not installed, "
            "the tool errors out rather than silently substituting mock."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level for library output (default: INFO).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    targets = [args.only] if args.only else list(CHOICES)
    failures: list[str] = []
    for kind in targets:
        try:
            res = ghcn_download.download_catalog_file(
                kind,
                force=args.force,
                max_age_hours=args.max_age_hours,
                use_mock=args.use_mock or None,
            )
        except Exception as exc:
            print(f"error: {kind}: {exc}", file=sys.stderr)
            failures.append(kind)
            continue
        status = "cache" if res.was_cached else ("mock" if res.used_mock else "download")
        print(
            f"[{status}] {res.relative_path}  {res.size_bytes:,}B  sha256={res.sha256[:12]}…  "
            f"{res.absolute_path}"
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
