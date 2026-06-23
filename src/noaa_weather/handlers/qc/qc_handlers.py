"""QC handlers — report how much of a station's GHCN record failed QC.

The climate analysis silently drops Q-flagged observations; this handler
re-reads the same cached CSV and *counts* them, so a reader can see what share
of the underlying data was rejected (and which QC checks tripped) before
trusting a trend. Pure counting lives in
``tools/_noaa_tools/ghcn_qc.summarize_quality_flags``; this layer only handles
the download + JSON shaping.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    download_station_csv,
    summarize_quality_flags,
)

logger = logging.getLogger("weather.qc")
NAMESPACE = "weather.QC"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def _headline(summary: dict[str, Any], station_id: str) -> str:
    """One-line, human-readable credibility statement from the summary."""
    total = summary["total_obs"]
    if total == 0:
        return f"No observations for {station_id} in the requested range."
    pct = summary["flagged_pct"]
    flagged = summary["flagged_obs"]
    parts = [
        f"{pct}% of {total:,} observations for {station_id} failed QC "
        f"({flagged:,} rejected)."
    ]
    # Name the worst element and the most common failing check, when present.
    by_elem = summary["by_element"]
    if by_elem:
        worst_elem, worst_rec = max(
            by_elem.items(), key=lambda kv: (kv[1]["pct"], kv[1]["flagged"])
        )
        if worst_rec["flagged"] > 0:
            parts.append(
                f"Worst element: {worst_elem} at {worst_rec['pct']}%."
            )
    by_flag = summary["by_flag"]
    if by_flag:
        top_letter, top_rec = next(iter(by_flag.items()))
        parts.append(
            f"Most common check: {top_letter} ({top_rec['label']}), "
            f"{top_rec['count']:,} obs."
        )
    return " ".join(parts)


def handle_summarize_quality_flags(params: dict[str, Any]) -> dict[str, Any]:
    """Handle SummarizeQualityFlags — QC rejection rates for one station.

    Downloads the station CSV (cached), counts Q-flagged observations per
    element / year / check letter, and returns the summary as JSON plus a
    short narrative and the headline flagged percentage.
    """
    station_id = params.get("station_id", "")
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2026))
    step_log = params.get("_step_log")

    _step_log(step_log, f"QC summary for {station_id} {start_year}-{end_year}")
    t0 = time.monotonic()

    csv_path = download_station_csv(station_id)
    summary = summarize_quality_flags(csv_path, start_year, end_year)
    narrative = _headline(summary, station_id)

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"{station_id}: {summary['flagged_pct']}% of {summary['total_obs']} "
        f"obs flagged ({elapsed:.1f}s)",
    )

    return {
        "quality_summary": json.dumps(summary),
        "flagged_pct": summary["flagged_pct"],
        "total_obs": summary["total_obs"],
        "narrative": narrative,
        "station_id": station_id,
    }


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.SummarizeQualityFlags": handle_summarize_quality_flags,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH[facet]
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_qc_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
