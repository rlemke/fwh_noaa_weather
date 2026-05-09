"""Ingest handlers — GHCN-Daily CSV download with catalog verification."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    download_station_csv,
    parse_ghcn_csv,
)

logger = logging.getLogger("weather.ingest")
NAMESPACE = "weather.Ingest"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def handle_fetch_station_data(params: dict[str, Any]) -> dict[str, Any]:
    """Handle FetchStationData — download one CSV per station from S3.

    GHCN-Daily stores all years for a station in a single CSV file.
    Downloads once (cached), then filters to the requested year range.
    """
    station_id = params.get("station_id", "")
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2024))
    step_log = params.get("_step_log")

    _step_log(step_log, f"Fetching GHCN data for {station_id} ({start_year}-{end_year})")
    t0 = time.monotonic()

    # Download CSV (cached)
    csv_path = download_station_csv(station_id)

    # Parse and filter to year range
    daily_data = parse_ghcn_csv(csv_path, start_year, end_year)

    # Count unique years with data
    years_seen = set()
    for d in daily_data:
        date_str = d.get("date", "")
        if len(date_str) >= 4:
            years_seen.add(int(date_str[:4]))

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"Fetched {len(daily_data)} daily records across {len(years_seen)} years in {elapsed:.1f}s",
        "success",
    )

    return {
        "record_count": len(daily_data),
        "years_with_data": len(years_seen),
        "station_id": station_id,
    }


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.FetchStationData": handle_fetch_station_data,
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


def register_ingest_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
