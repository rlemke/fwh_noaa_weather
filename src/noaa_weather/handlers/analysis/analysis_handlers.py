"""Analysis handlers — GHCN station climate analysis and region trends."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from ..shared.ghcn_utils import (
    ClimateStore,
    WeatherReportStore,
    climate_analysis,
    compute_yearly_summaries,
    download_station_csv,
    get_weather_db,
    parse_ghcn_csv,
    simple_linear_regression,
)

logger = logging.getLogger("weather.analysis")
NAMESPACE = "weather.Analysis"


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


def handle_analyze_station_climate(params: dict[str, Any]) -> dict[str, Any]:
    """Handle AnalyzeStationClimate — full analysis pipeline for one station.

    Downloads station CSV (cached), parses to year range, computes yearly
    summaries, writes to MongoDB.
    """
    station_id = params.get("station_id", "")
    station_name = params.get("station_name", "")
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2024))
    state = params.get("state", "")
    step_log = params.get("_step_log")

    _step_log(step_log, f"Analyzing {station_id} ({station_name}) {start_year}-{end_year}")
    t0 = time.monotonic()

    # Download and parse
    csv_path = download_station_csv(station_id)
    daily_data = parse_ghcn_csv(csv_path, start_year, end_year)

    if not daily_data:
        _step_log(step_log, f"No data for {station_id} in {start_year}-{end_year}", "warning")
        return {
            "yearly_summaries": json.dumps([]),
            "years_analyzed": 0,
            "station_id": station_id,
        }

    # Compute yearly summaries
    summaries = compute_yearly_summaries(daily_data, station_id, state)

    # Write to MongoDB
    try:
        db = get_weather_db()
        report_store = WeatherReportStore(db)
        for summary in summaries:
            report_store.upsert_report(
                station_id=station_id,
                station_name=station_name,
                year=summary["year"],
                location=state,
                report={
                    "temp_mean": summary.get("temp_mean"),
                    "precip_annual": summary.get("precip_annual"),
                    "hot_days": summary.get("hot_days"),
                    "frost_days": summary.get("frost_days"),
                    "snow_annual": summary.get("snow_annual"),
                    "snow_depth_max": summary.get("snow_depth_max"),
                    "snow_days": summary.get("snow_days"),
                },
                daily_stats=[],
            )
    except Exception as exc:
        logger.error("Failed to write reports for %s: %s", station_id, exc)
        raise

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"Analyzed {len(summaries)} years for {station_id} in {elapsed:.1f}s",
        "success",
    )

    return {
        "yearly_summaries": json.dumps(summaries),
        "years_analyzed": len(summaries),
        "station_id": station_id,
    }


def handle_compute_region_trend(params: dict[str, Any]) -> dict[str, Any]:
    """Handle ComputeRegionTrend — aggregate station data into a regional trend.

    Reads weather_reports from MongoDB for all stations in the state,
    groups by year, computes linear regression for warming rate and
    precipitation change.
    """
    country = params.get("country", "US")
    state = params.get("state", "")
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2024))
    step_log = params.get("_step_log")

    region = state if state else country
    _step_log(step_log, f"Computing trend for {region} ({start_year}-{end_year})")
    t0 = time.monotonic()

    try:
        db = get_weather_db()
    except Exception as exc:
        logger.warning("MongoDB unavailable: %s", exc)
        return _empty_trend(state, start_year, end_year)

    report_store = WeatherReportStore(db)
    climate_store = ClimateStore(db)

    # Query all reports for this state/region
    query = {"year": {"$gte": start_year, "$lte": end_year}}
    if state:
        query["location"] = state
    reports = list(report_store.reports.find(query, {"_id": 0}))

    if not reports:
        _step_log(step_log, f"No reports found for {region}", "warning")
        return _empty_trend(state, start_year, end_year)

    # Group by year
    by_year: dict[int, list[dict]] = {}
    for r in reports:
        yr = r.get("year")
        if yr is not None:
            by_year.setdefault(yr, []).append(r)

    # Compute yearly aggregates
    years_data: list[dict] = []
    for yr in sorted(by_year):
        recs = by_year[yr]
        temps = [
            r["report"]["temp_mean"]
            for r in recs
            if r.get("report", {}).get("temp_mean") is not None
        ]
        precips = [
            r["report"]["precip_annual"]
            for r in recs
            if r.get("report", {}).get("precip_annual") is not None
        ]
        snows = [
            r["report"]["snow_annual"]
            for r in recs
            if r.get("report", {}).get("snow_annual") is not None
        ]

        if not temps:
            continue

        yearly = {
            "state": state,
            "year": yr,
            "station_count": len(recs),
            "temp_mean": round(sum(temps) / len(temps), 2),
            "temp_min_avg": round(min(temps), 2),
            "temp_max_avg": round(max(temps), 2),
            "precip_annual": round(sum(precips) / len(precips), 1) if precips else 0,
            "hot_days": sum(r.get("report", {}).get("hot_days", 0) or 0 for r in recs),
            "frost_days": sum(r.get("report", {}).get("frost_days", 0) or 0 for r in recs),
            "precip_days": 0,
            # None (not 0) when no station reported snow this year — keeps warm
            # regions / non-snow stations out of the snow trend regression.
            "snow_annual": round(sum(snows) / len(snows), 1) if snows else None,
        }
        years_data.append(yearly)

        # Write to climate_state_years
        try:
            climate_store.upsert_state_year(yearly)
        except Exception:
            pass

    # Compute linear trend
    xs = [float(d["year"]) for d in years_data]
    ys_temp = [d["temp_mean"] for d in years_data]
    ys_precip = [d["precip_annual"] for d in years_data]

    slope_temp, _ = simple_linear_regression(xs, ys_temp) if len(xs) >= 2 else (0.0, 0.0)
    warming_per_decade = round(slope_temp * 10, 2)

    if len(ys_precip) >= 2 and ys_precip[0] != 0:
        precip_change_pct = round((ys_precip[-1] - ys_precip[0]) / abs(ys_precip[0]) * 100, 2)
    else:
        precip_change_pct = 0.0

    # Snow trend — only over years that actually recorded snow (snow_annual not
    # None); regions with no snow data get has_snow_data=False and no snow line.
    snow_pairs = [(d["year"], d["snow_annual"]) for d in years_data
                  if d.get("snow_annual") is not None]
    has_snow_data = len(snow_pairs) >= 2
    if has_snow_data:
        xs_snow = [float(y) for y, _ in snow_pairs]
        ys_snow = [s for _, s in snow_pairs]
        slope_snow, _ = simple_linear_regression(xs_snow, ys_snow)
        snow_per_decade_mm = round(slope_snow * 10, 1)
        snow_change_pct = (round((ys_snow[-1] - ys_snow[0]) / abs(ys_snow[0]) * 100, 1)
                           if ys_snow[0] else 0.0)
    else:
        snow_per_decade_mm = 0.0
        snow_change_pct = 0.0

    # Build decades summary
    decades: dict[str, dict] = {}
    for d in years_data:
        decade = f"{(d['year'] // 10) * 10}s"
        dec = decades.setdefault(decade, {"temps": [], "precips": [], "snows": [], "count": 0})
        dec["temps"].append(d["temp_mean"])
        dec["precips"].append(d["precip_annual"])
        if d.get("snow_annual") is not None:
            dec["snows"].append(d["snow_annual"])
        dec["count"] += 1

    decades_summary: dict[str, dict] = {}
    for dec_name, dec_data in decades.items():
        decades_summary[dec_name] = {
            "avg_temp": round(sum(dec_data["temps"]) / len(dec_data["temps"]), 2),
            "avg_precip": round(sum(dec_data["precips"]) / len(dec_data["precips"]), 1)
            if dec_data["precips"]
            else 0,
            "avg_snow": round(sum(dec_data["snows"]) / len(dec_data["snows"]), 1)
            if dec_data["snows"]
            else None,
            "years_with_data": dec_data["count"],
        }

    # Narrative
    direction = "warmed" if warming_per_decade > 0 else "cooled"
    narrative = (
        f"Climate analysis for {region} from {start_year} to {end_year}. "
        f"Temperatures have {direction} at {abs(warming_per_decade)}°C per decade. "
        f"Annual precipitation has {'increased' if precip_change_pct > 0 else 'decreased'} "
        f"by {abs(precip_change_pct)}%."
    )
    if has_snow_data:
        narrative += (
            f" Annual snowfall has {'increased' if snow_per_decade_mm > 0 else 'decreased'} "
            f"by {abs(snow_per_decade_mm)}mm per decade ({abs(snow_change_pct)}%)."
        )

    trend = {
        "state": state,
        "start_year": start_year,
        "end_year": end_year,
        "warming_rate_per_decade": warming_per_decade,
        "precip_change_pct": precip_change_pct,
        "snow_change_pct": snow_change_pct,
        "snow_per_decade_mm": snow_per_decade_mm,
        "has_snow_data": has_snow_data,
        "decades": decades_summary,
    }

    # Write to climate_trends
    try:
        climate_store.upsert_trend({**trend, "narrative": narrative})
    except Exception:
        pass

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"Trend for {region}: {warming_per_decade}°C/decade, {len(years_data)} years in {elapsed:.1f}s",
        "success",
    )

    return {
        "trend": json.dumps(trend),
        "narrative": narrative,
    }


def handle_analyze_station_monthly(params: dict[str, Any]) -> dict[str, Any]:
    """Handle AnalyzeStationMonthly — per-(year, month) rollups for one station.

    Reads the station's cached GHCN CSV (downloading if needed), parses
    to the requested year range, and computes monthly climate summaries.
    Returns ``monthly_rows`` as JSON, since the per-month dict shape
    varies with data availability. No MongoDB write — the monthly shape
    is intended as an intermediate for GenerateClimateReport.
    """
    station_id = params.get("station_id", "")
    station_name = params.get("station_name", "")
    state = params.get("state", "")
    start_year = int(params.get("start_year", 1950))
    end_year = int(params.get("end_year", 2026))
    step_log = params.get("_step_log")

    _step_log(
        step_log,
        f"Monthly analysis for {station_id} ({station_name}) {start_year}-{end_year}",
    )
    t0 = time.monotonic()

    csv_path = download_station_csv(station_id)
    daily_data = parse_ghcn_csv(csv_path, start_year, end_year)

    if not daily_data:
        _step_log(
            step_log,
            f"No data for {station_id} in {start_year}-{end_year}",
            "warning",
        )
        return {
            "monthly_rows": json.dumps([]),
            "months_analyzed": 0,
            "station_id": station_id,
        }

    rows = climate_analysis.compute_monthly_summaries(
        daily_data, station_id=station_id, state=state
    )

    elapsed = time.monotonic() - t0
    _step_log(
        step_log,
        f"Monthly analysis: {len(rows)} months for {station_id} in {elapsed:.1f}s",
        "success",
    )

    return {
        "monthly_rows": json.dumps(rows),
        "months_analyzed": len(rows),
        "station_id": station_id,
    }


def _empty_trend(state: str, start_year: int, end_year: int) -> dict[str, Any]:
    return {
        "trend": json.dumps(
            {
                "state": state,
                "start_year": start_year,
                "end_year": end_year,
                "warming_rate_per_decade": 0.0,
                "precip_change_pct": 0.0,
                "decades": {},
            }
        ),
        "narrative": f"No data available for {state} ({start_year}-{end_year}).",
    }


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.AnalyzeStationClimate": handle_analyze_station_climate,
    f"{NAMESPACE}.AnalyzeStationMonthly": handle_analyze_station_monthly,
    f"{NAMESPACE}.ComputeRegionTrend": handle_compute_region_trend,
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


def register_analysis_handlers(poller) -> None:
    """Register with AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
