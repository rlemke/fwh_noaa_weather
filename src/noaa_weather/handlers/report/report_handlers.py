"""Report handlers — regional climate-report bundle generation.

Wraps :func:`_lib.climate_report.generate_climate_report`, which is the
same core that the ``climate-report.sh`` CLI calls. The runtime and the
terminal share one code path and write to one cache.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    climate_report as report_core,
    geofabrik_regions,
)

logger = logging.getLogger("weather.report")
NAMESPACE = "weather.Report"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def handle_generate_climate_report(params: dict[str, Any]) -> dict[str, Any]:
    """Handle GenerateClimateReport — produce the full region bundle.

    Output bundle at ``cache/noaa-weather/climate-report/<country>/<region>/``:
    ``report.json``, ``report.md``, ``report.html`` + five SVGs
    (climograph, annual_trend, warming_stripes, heatmap, anomaly_bars).
    Every file has a ``.meta.json`` sidecar per the cache-layout spec.
    """
    country = params.get("country", "US")
    state = params.get("state", "") or ""
    region = params.get("region", "") or ""
    start_year = int(params.get("start_year", 1950))
    end_year = int(params.get("end_year", 2026))
    baseline_start = int(params.get("baseline_start", 1991))
    baseline_end = int(params.get("baseline_end", 2020))
    min_years = int(params.get("min_years", 20))
    max_stations = int(params.get("max_stations", 0))
    required_elements = params.get("required_elements") or ["TMAX", "TMIN", "PRCP"]
    if isinstance(required_elements, str):
        required_elements = json.loads(required_elements)
    override_bulk_guard = bool(params.get("override_bulk_guard", False))
    step_log = params.get("_step_log")

    # The registry runner can't tell us whether ``country`` was
    # defaulted; the CLI uses argv inspection. For the handler we
    # adopt the same rule the catalog handler uses: treat the default
    # "US" as not-explicit when a region is set. Any other value wins.
    country_explicit = bool(country) and country != "US"

    label = region or (f"{country}/{state}" if state else (country or "ALL"))
    _step_log(
        step_log,
        f"Generating climate report for {label} ({start_year}-{end_year}, "
        f"baseline {baseline_start}-{baseline_end})",
    )
    t0 = time.monotonic()

    try:
        bundle = report_core.generate_climate_report(
            country=country,
            state=state,
            region=region,
            start_year=start_year,
            end_year=end_year,
            baseline=(baseline_start, baseline_end),
            min_years=min_years,
            required_elements=required_elements,
            max_stations=max_stations,
            override_bulk_guard=override_bulk_guard,
            country_explicit=country_explicit,
        )
    except (report_core.ReportError, KeyError) as exc:
        _step_log(step_log, f"Report failed: {exc}", "error")
        raise

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"Report ready ({bundle.station_count} stations) in {elapsed:.1f}s: "
        f"{bundle.output_dir}",
        "success",
    )

    return {
        "output_dir": str(bundle.output_dir),
        "report_json": str(bundle.report_json_path),
        "report_md": str(bundle.report_md_path),
        "report_html": str(bundle.report_html_path),
        "chart_paths": json.dumps({k: str(v) for k, v in bundle.chart_paths.items()}),
        "station_count": bundle.station_count,
        "narrative": bundle.narrative,
    }


def handle_list_regions_under(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ListRegionsUnder — enumerate Geofabrik sub-regions.

    Wraps :func:`_lib.geofabrik_regions.list_regions_under`. The
    returned ``regions`` is a JSON array of path strings so FFL
    workflows can use ``andThen foreach`` to fan out reports across
    the set without any further marshalling.
    """
    prefix = params.get("prefix", "") or ""
    include_parents = bool(params.get("include_parents", False))
    step_log = params.get("_step_log")

    try:
        regions = geofabrik_regions.list_regions_under(
            prefix, include_parents=include_parents
        )
    except KeyError as exc:
        _step_log(step_log, f"ListRegionsUnder failed: {exc}", "error")
        raise

    _step_log(
        step_log,
        f"ListRegionsUnder prefix={prefix!r} "
        f"include_parents={include_parents} → {len(regions)} region(s)",
        "success",
    )
    return {
        "regions": json.dumps(regions),
        "region_count": len(regions),
    }


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.GenerateClimateReport": handle_generate_climate_report,
    f"{NAMESPACE}.ListRegionsUnder": handle_list_regions_under,
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


def register_report_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
