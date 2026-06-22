"""Extreme-event detection handlers — heat waves, cold snaps, droughts, etc.

Reuses the shared data path (``download_station_csv`` -> ``parse_ghcn_csv``) and
the pure ``_noaa_tools.extremes`` library via the ``ghcn_utils`` shim, so the
``weather.Extremes.DetectStationExtremes`` facet and the ``detect-extremes`` CLI
run identical logic.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    ExtremeConfig,
    detect_events,
    download_station_csv,
    extremes,
    parse_ghcn_csv,
)

logger = logging.getLogger("weather.extremes")
NAMESPACE = "weather.Extremes"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if callable(step_log):
        step_log(msg, level)


def handle_detect_station_extremes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DetectStationExtremes — find extreme weather events for one station.

    Downloads the station's GHCN-Daily CSV (cached), parses it to the year range,
    and detects heat waves / cold snaps / wet & dry spells / heavy rain & snow
    days using the supplied (or default) thresholds.
    """
    station_id = params.get("station_id", "")
    if not station_id:
        raise ValueError("DetectStationExtremes: 'station_id' is required "
                         "(resolve one via weather.Catalog.DiscoverStations).")
    station_name = params.get("station_name", "") or station_id
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2026))
    step_log = params.get("_step_log")

    config = ExtremeConfig.from_params(params)
    _step_log(step_log, f"Detecting extremes for {station_name} ({station_id}) {start_year}-{end_year}")
    t0 = time.monotonic()

    csv_path = download_station_csv(station_id)
    daily_data = parse_ghcn_csv(csv_path, start_year, end_year)
    if not daily_data:
        _step_log(step_log, f"No data for {station_id} in {start_year}-{end_year}", "warning")
        return {
            "events": json.dumps([]),
            "event_count": 0,
            "counts_by_type": json.dumps({}),
            "decadal_frequency": json.dumps({}),
            "summary": f"No data for {station_id} in {start_year}-{end_year}.",
            "station_id": station_id,
        }

    result = detect_events(daily_data, config)
    summary = extremes.summarize(result, label=station_name)
    _step_log(step_log, f"{summary} ({time.monotonic() - t0:.1f}s)", "success")

    return {
        "events": json.dumps(result["events"]),
        "event_count": result["event_count"],
        "counts_by_type": json.dumps(result["counts_by_type"]),
        "decadal_frequency": json.dumps(result["decadal_frequency"]),
        "summary": summary,
        "station_id": station_id,
    }


_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.DetectStationExtremes": handle_detect_station_extremes,
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


def register_extremes_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
