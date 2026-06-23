"""QC handlers — report how much of a station's GHCN record failed QC.

The climate analysis silently drops Q-flagged observations; this handler
re-reads the same cached CSV and *counts* them, so a reader can see what share
of the underlying data was rejected (and which QC checks tripped) before
trusting a trend. Pure counting lives in
``tools/_noaa_tools/ghcn_qc.summarize_quality_flags``; this layer only handles
the download + JSON shaping.
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
    QCSummaryStore,
    aggregate_region_qc,
    download_station_csv,
    get_weather_db,
    summarize_quality_flags,
)
from noaa_weather.tools._noaa_tools import qc_chart, sidecar
from noaa_weather.tools._noaa_tools.storage import get_storage

_VIZ_CACHE_TYPE = "qc-viz"


def _coerce_json(value: Any, default: Any) -> Any:
    """Accept a JSON string or an already-parsed value (FFL passes Json as str)."""
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default
    return value

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
    station_name = params.get("station_name", "")
    state = params.get("state", "")
    start_year = int(params.get("start_year", 1944))
    end_year = int(params.get("end_year", 2026))
    step_log = params.get("_step_log")

    _step_log(step_log, f"QC summary for {station_id} {start_year}-{end_year}")
    t0 = time.monotonic()

    csv_path = download_station_csv(station_id)
    summary = summarize_quality_flags(csv_path, start_year, end_year)
    # Make the summary self-describing so a downstream RenderQCChart can title
    # the chart with the station's name/location, not just the bare ID.
    summary["station_id"] = station_id
    summary["station_name"] = station_name
    narrative = _headline(summary, station_id)

    # Best-effort persist a per-station rollup so AggregateRegionQC can read it
    # back by region (same pattern as DetectStationExtremes -> AggregateRegion-
    # Extremes). Tagged with `location` (the state) for the region filter; we
    # store the counts (not just %) so the region rollup re-weights correctly.
    try:
        QCSummaryStore(get_weather_db()).upsert_station({
            "station_id": station_id,
            "station_name": station_name,
            "location": state,
            "start_year": start_year,
            "end_year": end_year,
            "total_obs": summary["total_obs"],
            "flagged_obs": summary["flagged_obs"],
            "flagged_pct": summary["flagged_pct"],
            "by_element": summary["by_element"],
            "by_flag": summary["by_flag"],
        })
    except Exception as exc:  # noqa: BLE001 - persistence is best-effort
        logger.warning("Could not persist QC rollup for %s: %s", station_id, exc)

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


def _region_headline(agg: dict[str, Any], region: str) -> str:
    """One-line region credibility statement from the aggregate."""
    n = agg["station_count"]
    total = agg["total_obs"]
    if n == 0 or total == 0:
        return f"No quality-control data available for {region}."
    parts = [
        f"Across {n} station(s) in {region}, {agg['flagged_pct']}% of "
        f"{total:,} observations failed QC ({agg['flagged_obs']:,} rejected)."
    ]
    by_flag = agg["by_flag"]
    if by_flag:
        top_letter, top_rec = next(iter(by_flag.items()))
        parts.append(
            f"Most common check: {top_letter} ({top_rec['label']}), "
            f"{top_rec['count']:,} obs."
        )
    worst = agg["worst_stations"]
    if worst:
        w = worst[0]
        label = w["station_name"] or w["station_id"]
        parts.append(f"Worst station: {label} at {w['flagged_pct']}%.")
    return " ".join(parts)


def handle_aggregate_region_qc(params: dict[str, Any]) -> dict[str, Any]:
    """Handle AggregateRegionQC — roll per-station QC rates up to a region.

    Reads the per-station QC rollups persisted by SummarizeQualityFlags
    (filtered by location/state), and computes one observation-weighted region
    rejection rate plus per-element / per-check breakdowns and a worst-stations
    ranking. `station_count` is the dependency signal (pass
    discovery.station_count) so this waits for the foreach to finish — the same
    pattern as ComputeRegionTrend / AggregateRegionExtremes.
    """
    country = params.get("country", "US")
    state = params.get("state", "")
    region = state or country
    step_log = params.get("_step_log")

    try:
        store = QCSummaryStore(get_weather_db())
    except Exception as exc:  # noqa: BLE001 - no DB -> empty aggregate, not a crash
        logger.warning("MongoDB unavailable for region QC aggregate: %s", exc)
        empty = {"region": region, "station_count": 0, "total_obs": 0,
                 "flagged_obs": 0, "flagged_pct": 0.0}
        return {"region_summary": json.dumps(empty),
                "narrative": f"No quality-control data available for {region}.",
                "flagged_pct": 0.0, "station_count": 0}

    per_station = store.find_for_region(state)
    agg = aggregate_region_qc(per_station, region_label=region)
    narrative = _region_headline(agg, region)
    try:
        store.upsert_region(state, agg)
    except Exception:  # noqa: BLE001 - best-effort
        pass

    _step_log(step_log, narrative, "success")
    return {
        "region_summary": json.dumps(agg),
        "narrative": narrative,
        "flagged_pct": agg["flagged_pct"],
        "station_count": agg["station_count"],
    }


def handle_render_qc_chart(params: dict[str, Any]) -> dict[str, Any]:
    """Render a QC summary as a per-element bar chart (SVG) + an HTML page.

    Takes the ``summary_json`` either SummarizeQualityFlags (`quality_summary`)
    or AggregateRegionQC (`region_summary`) emits — both carry ``by_element`` and
    ``by_flag``; a region summary also carries ``worst_stations``. Writes
    ``qc.svg`` + ``qc.html`` (sidecar-backed, through the storage abstraction so
    it lands in durable storage on any backend) and returns their paths.
    """
    summary = _coerce_json(params.get("summary_json"), {})
    step_log = params.get("_step_log")

    # Title/label the chart by the most human subject available in the summary —
    # the station's NAME (its location), else the region, else the bare ID — so
    # the output identifies WHERE the data is from, not just a station code.
    # The passed title/label are only fallbacks when the summary lacks identity.
    sid = summary.get("station_id", "")
    sname = summary.get("station_name", "")
    region = summary.get("region", "")
    subject = sname or region or sid
    if subject:
        if sname and sid and sname != sid:
            title = f"Data quality: {sname} ({sid})"
        else:
            title = f"Data quality: {subject}"
        label = subject
    else:
        title = params.get("title") or "Data quality"
        label = params.get("label") or ""

    by_element = summary.get("by_element", {})
    by_flag = summary.get("by_flag", {})
    worst_stations = summary.get("worst_stations", [])  # region-only; [] for a station
    pct = summary.get("flagged_pct", 0.0)
    total = summary.get("total_obs", 0)
    narrative = (
        f"{pct}% of {total:,} observations failed QC."
        if total else "No observations to chart."
    )

    svg = qc_chart.flagged_pct_bars_svg(by_element, title=title)
    html = qc_chart.qc_html(title=title, label=label, svg=svg, by_element=by_element,
                            by_flag=by_flag, worst_stations=worst_stations, summary=narrative)

    storage = get_storage()
    # Key the output PATH on the stable id (station_id / region), not the display
    # name — so the artifact URL stays constant even as the title shows the name.
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (sid or region or label or title)).strip("_") or "qc"
    out_dir = sidecar.cache_path("noaa-weather", _VIZ_CACHE_TYPE, slug, storage)

    def _write(name: str, text: str, kind: str) -> str:
        path = storage.join(out_dir, name)
        body = text.encode("utf-8")
        storage.write_text_atomic(path, text)
        sidecar.write_sidecar("noaa-weather", _VIZ_CACHE_TYPE, f"{slug}/{name}",
                              kind="file", size_bytes=len(body),
                              sha256=hashlib.sha256(body).hexdigest(),
                              tool={"name": "qc_chart", "version": "1.0"},
                              extra={"content_kind": kind, "label": label}, storage=storage)
        return path

    svg_path = _write("qc.svg", svg, "svg")
    html_path = _write("qc.html", html, "html")
    _step_log(step_log, f"Rendered QC chart for {label or title} -> {html_path}", "success")
    return {"html_path": html_path, "svg_path": svg_path, "narrative": narrative}


# Dispatch table
_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.SummarizeQualityFlags": handle_summarize_quality_flags,
    f"{NAMESPACE}.AggregateRegionQC": handle_aggregate_region_qc,
    f"{NAMESPACE}.RenderQCChart": handle_render_qc_chart,
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
