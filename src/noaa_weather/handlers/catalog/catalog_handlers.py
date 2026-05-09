"""Catalog handlers — station discovery with GHCN inventory verification."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    download_inventory,
    download_station_catalog,
    filter_stations,
    geofabrik_regions,
    parse_inventory,
    parse_stations,
)

logger = logging.getLogger("weather.catalog")
NAMESPACE = "weather.Catalog"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def handle_discover_stations(params: dict[str, Any]) -> dict[str, Any]:
    """Handle DiscoverStations — catalog-aware station discovery.

    Downloads ghcnd-stations.txt and ghcnd-inventory.txt, filters by
    country/state/region/min_years/required_elements, and returns
    stations with verified data coverage.

    ``region``, when set, resolves a Geofabrik region path
    (``europe/germany``, ``north-america/us/california``) to a bbox
    and uses that as the spatial filter. When ``region`` is set and
    ``country`` is left at its default (``"US"``), the country filter
    is suppressed so the bbox is authoritative.
    """
    country = params.get("country", "US")
    state = params.get("state", "")
    region = params.get("region", "") or ""
    max_stations = int(params.get("max_stations", 10))
    min_years = int(params.get("min_years", 20))
    required_elements = params.get("required_elements", ["TMAX", "TMIN", "PRCP"])
    if isinstance(required_elements, str):
        required_elements = json.loads(required_elements)
    step_log = params.get("_step_log")

    # Resolve the Geofabrik region early so a bad path fails fast with a
    # clear error rather than silently returning an empty station list.
    bbox = None
    region_info = None
    if region:
        try:
            region_info = geofabrik_regions.resolve_region(region)
        except KeyError as exc:
            _step_log(step_log, f"Region lookup failed: {exc}", "error")
            raise
        bbox = region_info.bbox
        # bbox is authoritative — drop the default country filter unless
        # the caller explicitly asked for both. The registry runner can't
        # tell us whether ``country`` was defaulted, so we treat ``"US"``
        # (the default) as not-explicit when ``region`` is set. Other
        # values win.
        if country == "US":
            country = ""

    region_label = (
        region_info.name if region_info is not None
        else (f"{country}/{state}" if state else (country or "ALL"))
    )
    _step_log(
        step_log,
        f"Discovering GHCN stations for {region_label} "
        f"(max {max_stations}, min {min_years} years)",
    )
    t0 = time.monotonic()

    stations_text = download_station_catalog()
    inventory_text = download_inventory()

    all_stations = parse_stations(stations_text)
    inventory = parse_inventory(inventory_text)

    _step_log(
        step_log, f"Catalog loaded: {len(all_stations)} stations, {len(inventory)} with inventory"
    )

    filtered = filter_stations(
        all_stations,
        inventory,
        country=country,
        state=state,
        bbox=bbox,
        max_stations=max_stations,
        min_years=min_years,
        required_elements=required_elements,
    )

    elapsed = time.monotonic() - t0
    names = ", ".join(s.get("name", s.get("station_id", "?"))[:30] for s in filtered[:5])
    _step_log(
        step_log,
        f"Found {len(filtered)} stations in {elapsed:.1f}s: {names}{'...' if len(filtered) > 5 else ''}",
        "success",
    )

    return {
        "stations": filtered,
        "station_count": len(filtered),
    }


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.DiscoverStations": handle_discover_stations,
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


def register_catalog_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
