"""Report how much of a GHCN station's record failed NOAA quality control.

Downloads the per-station CSV (cached) and COUNTS the Q-flagged observations
the climate analysis silently drops, so the rejection rate is visible: overall
flagged %, plus breakdowns per element, per year, and per QC-check letter.

This is the CLI surface of the ``weather.QC.SummarizeQualityFlags`` facet —
both call the same ``_noaa_tools.ghcn_qc.summarize_quality_flags``.

Usage::

    # One station, full default range
    python summarize_quality_flags.py USW00094728

    # Restrict the year window
    python summarize_quality_flags.py USW00094728 --start-year 1950 --end-year 2024

    # Offline mode (deterministic mock CSV, no network)
    python summarize_quality_flags.py USW00094728 --use-mock
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _noaa_tools import ghcn_download, ghcn_qc, qc_chart  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("station_id", help="GHCN station ID (e.g. USW00094728).")
    parser.add_argument("--start-year", type=int, default=1944)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument(
        "--chart-html",
        metavar="PATH",
        help="Also render a self-contained HTML chart of the per-element flagged "
        "% (+ which-check-tripped table) to this path.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Re-download the CSV even if cached."
    )
    parser.add_argument(
        "--use-mock",
        action="store_true",
        help="Use deterministic mock data instead of a live NOAA fetch.",
    )
    parser.add_argument(
        "--log-level", default="WARNING", help="Python logging level (default: WARNING)."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        res = ghcn_download.download_station_csv(
            args.station_id, force=args.force, use_mock=args.use_mock or None
        )
    except Exception as exc:
        print(f"error: {args.station_id}: {exc}", file=sys.stderr)
        return 1

    summary = ghcn_qc.summarize_quality_flags(
        res.absolute_path, args.start_year, args.end_year
    )
    summary["station_id"] = args.station_id

    if args.chart_html:
        svg = qc_chart.flagged_pct_bars_svg(
            summary["by_element"], title=f"Data quality — {args.station_id}"
        )
        html = qc_chart.qc_html(
            title=f"Data quality: {args.station_id}",
            label=f"{args.start_year}-{args.end_year}",
            svg=svg,
            by_element=summary["by_element"],
            by_flag=summary["by_flag"],
            summary=f"{summary['flagged_pct']}% of {summary['total_obs']:,} "
            f"observations failed QC.",
        )
        with open(args.chart_html, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[chart] wrote {args.chart_html}", file=sys.stderr)

    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
