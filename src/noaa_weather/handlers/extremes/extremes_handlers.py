"""Extreme-event detection handlers — heat waves, cold snaps, droughts, etc.

Reuses the shared data path (``download_station_csv`` -> ``parse_ghcn_csv``) and
the pure ``_noaa_tools.extremes`` library via the ``ghcn_utils`` shim, so the
``weather.Extremes.DetectStationExtremes`` facet and the ``detect-extremes`` CLI
run identical logic.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Any

from ..shared.ghcn_utils import (
    ExtremeConfig,
    ExtremeEventStore,
    aggregate_region,
    detect_events,
    download_station_csv,
    extremes,
    extremes_chart,
    get_weather_db,
    parse_ghcn_csv,
)
from noaa_weather.tools._noaa_tools import sidecar
from noaa_weather.tools._noaa_tools.storage import LocalStorage

_VIZ_CACHE_TYPE = "extremes-viz"


def _coerce_json(v: Any, default):
    """Accept a JSON string or an already-parsed value (FFL passes Json as str)."""
    if v is None or v == "":
        return default
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default
    return v

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
    state = params.get("state", "")
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

    # Best-effort persist a per-station rollup so AggregateRegionExtremes can read
    # it back by region (same pattern AnalyzeStationClimate -> ComputeRegionTrend
    # uses). Tagged with `location` (the state) for the region filter.
    try:
        ExtremeEventStore(get_weather_db()).upsert_station({
            "station_id": station_id,
            "station_name": station_name,
            "location": state,
            "start_year": start_year,
            "end_year": end_year,
            "event_count": result["event_count"],
            "counts_by_type": result["counts_by_type"],
            "decadal_frequency": result["decadal_frequency"],
        })
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning("Could not persist extreme rollup for %s: %s", station_id, exc)

    _step_log(step_log, f"{summary} ({time.monotonic() - t0:.1f}s)", "success")

    return {
        "events": json.dumps(result["events"]),
        "event_count": result["event_count"],
        "counts_by_type": json.dumps(result["counts_by_type"]),
        "decadal_frequency": json.dumps(result["decadal_frequency"]),
        "summary": summary,
        "station_id": station_id,
    }


def handle_aggregate_region_extremes(params: dict[str, Any]) -> dict[str, Any]:
    """Handle AggregateRegionExtremes — roll up per-station extremes for a region.

    Reads the per-station extreme rollups persisted by DetectStationExtremes
    (filtered by location/state), sums them, and computes per-type decadal trends
    (rising/falling) across the region. `station_count` is the dependency signal
    (pass discovery.station_count) so this waits for the foreach to finish — the
    same pattern as ComputeRegionTrend.
    """
    country = params.get("country", "US")
    state = params.get("state", "")
    region = state or country
    step_log = params.get("_step_log")

    try:
        store = ExtremeEventStore(get_weather_db())
    except Exception as exc:  # noqa: BLE001 - no DB -> empty aggregate, not a crash
        logger.warning("MongoDB unavailable for region aggregate: %s", exc)
        return {"aggregate": json.dumps({"region": region, "station_count": 0, "total_events": 0}),
                "narrative": f"No extreme-event data available for {region}.", "station_count": 0}

    per_station = store.find_for_region(state)
    agg = aggregate_region(per_station, region_label=region)
    try:
        store.upsert_region(state, agg)
    except Exception:  # noqa: BLE001 - best-effort
        pass

    _step_log(step_log, agg["narrative"], "success")
    return {
        "aggregate": json.dumps(agg),
        "narrative": agg["narrative"],
        "station_count": agg["station_count"],
        # top-level so a render step can wire them without nested-JSON access
        "counts_by_type": json.dumps(agg["counts_by_type"]),
        "decadal_frequency": json.dumps(agg["by_type_decade"]),
        "trends": json.dumps(agg["trends"]),
    }


def handle_render_extremes_chart(params: dict[str, Any]) -> dict[str, Any]:
    """Render an extreme-event result as an SVG bar chart + an HTML page.

    Takes the ``decadal_frequency`` ({type:{decade:count}}) and ``counts_by_type``
    that both DetectStationExtremes and AggregateRegionExtremes emit, plus optional
    ``trends``. Writes ``extremes.svg`` + ``extremes.html`` (sidecar-backed) and
    returns their paths.
    """
    title = params.get("title") or "Extreme weather events"
    label = params.get("label") or ""
    counts_by_type = _coerce_json(params.get("counts_by_type"), {})
    decadal_frequency = _coerce_json(params.get("decadal_frequency"), {})
    trends = _coerce_json(params.get("trends"), {})
    summary = params.get("summary") or None
    step_log = params.get("_step_log")

    svg = extremes_chart.decadal_bars_svg(decadal_frequency, title=title, trends=trends)
    html = extremes_chart.extremes_html(title=title, label=label, svg=svg,
                                        counts_by_type=counts_by_type, trends=trends,
                                        summary=summary)

    storage = LocalStorage()
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (label or title)).strip("_") or "extremes"
    out_dir = sidecar.cache_path("noaa-weather", _VIZ_CACHE_TYPE, slug, storage)
    os.makedirs(out_dir, exist_ok=True)

    def _write(name: str, text: str, kind: str) -> str:
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        body = text.encode("utf-8")
        sidecar.write_sidecar("noaa-weather", _VIZ_CACHE_TYPE, f"{slug}/{name}",
                              kind="file", size_bytes=len(body),
                              sha256=hashlib.sha256(body).hexdigest(),
                              tool={"name": "extremes_chart", "version": "1.0"},
                              extra={"content_kind": kind, "label": label}, storage=storage)
        return path

    svg_path = _write("extremes.svg", svg, "svg")
    html_path = _write("extremes.html", html, "html")
    _step_log(step_log, f"Rendered extremes chart for {label or title} -> {html_path}", "success")
    return {"html_path": html_path, "svg_path": svg_path}


_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.DetectStationExtremes": handle_detect_station_extremes,
    f"{NAMESPACE}.AggregateRegionExtremes": handle_aggregate_region_extremes,
    f"{NAMESPACE}.RenderExtremesChart": handle_render_extremes_chart,
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
