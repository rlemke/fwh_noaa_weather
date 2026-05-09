"""Geocode handlers — reverse geocode station coordinates via OSM Nominatim."""

from __future__ import annotations

import os
from typing import Any

from ..shared.ghcn_utils import reverse_geocode_nominatim

NAMESPACE = "weather.Geocode"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def handle_reverse_geocode(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ReverseGeocode — reverse geocode lat/lon via Nominatim."""
    lat = float(params.get("lat", 0.0))
    lon = float(params.get("lon", 0.0))
    step_log = params.get("_step_log")

    _step_log(step_log, f"Geocoding ({lat}, {lon})")

    geo = reverse_geocode_nominatim(lat, lon)

    _step_log(step_log, f"Geocoded: {geo.get('display_name', '')}", "success")

    return {"geo": geo}


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.ReverseGeocode": handle_reverse_geocode,
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


def register_geocode_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
