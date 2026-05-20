"""Marine handlers — NDBC catalog, stdmet, summaries, buoys map.

Thin dispatchers around :mod:`_noaa_tools.ndbc_download`, :mod:`_noaa_tools.ndbc_parse`,
and :mod:`_noaa_tools.ndbc_map`. Same one-code-path story as the GHCN handlers —
a facet invocation from FFL and a CLI run write identical sidecar-backed
artifacts under ``cache/noaa-weather/``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import statistics
import sys
from pathlib import Path
from typing import Any

# Reach _noaa_tools through the existing handler shim (puts tools/ on sys.path).
from ..shared.ghcn_utils import (  # noqa: F401
    ndbc_download,
    ndbc_map,
    ndbc_parse,
)

# The shim handles the sys.path gymnastics already; these imports are
# plain once it's loaded.
from _noaa_tools import sidecar  # noqa: E402
from _noaa_tools.storage import LocalStorage  # noqa: E402

logger = logging.getLogger("weather.marine")
NAMESPACE = "weather.Marine"

BUOY_SUMMARY_CACHE_TYPE = "buoy-summaries"

# Thresholds mirror summarize_buoy.py.
HIGH_SST_C = 28.0
STORM_WAVE_M = 4.0


def _step_log(step_log: Any, msg: str, level: str = "info") -> None:
    if step_log is None:
        return
    if callable(step_log):
        step_log(msg, level)


# ---------------------------------------------------------------------------
# DownloadNdbcCatalog.
# ---------------------------------------------------------------------------

def handle_download_ndbc_catalog(params: dict[str, Any]) -> dict[str, Any]:
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    _step_log(step_log, f"DownloadNdbcCatalog force={force} use_mock={use_mock}")
    res = ndbc_download.download_catalog(force=force, use_mock=use_mock)
    _step_log(
        step_log,
        f"ndbc-catalog: {res.station_count:,} station(s) "
        f"(cached={res.was_cached}, mock={res.used_mock})",
        "success",
    )
    return {
        "xml_path": str(res.xml_path),
        "json_path": str(res.json_path),
        "station_count": res.station_count,
        "was_cached": res.was_cached,
        "used_mock": res.used_mock,
    }


# ---------------------------------------------------------------------------
# DiscoverBuoys.
# ---------------------------------------------------------------------------

def _coerce_list(value: Any) -> list[str]:
    """FFL collection defaults come in as list already, strings as JSON."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return [str(v) for v in parsed] if isinstance(parsed, list) else []
    return []


def _parse_bbox(s: str) -> tuple[float, float, float, float] | None:
    if not s:
        return None
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 4:
        return None
    try:
        return tuple(float(p) for p in parts)  # type: ignore[return-value]
    except ValueError:
        return None


def handle_discover_buoys(params: dict[str, Any]) -> dict[str, Any]:
    region = params.get("region", "") or ""
    bbox_str = params.get("bbox", "") or ""
    types_list = _coerce_list(params.get("types"))
    require_list = _coerce_list(params.get("require_fields"))
    max_stations = int(params.get("max_stations", 0))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    stations = ndbc_download.read_catalog_stations(use_mock=use_mock)

    # Resolve spatial filter — explicit bbox wins, then region → bbox.
    bbox = _parse_bbox(bbox_str)
    if bbox is None and region:
        # Import locally so this module stays importable when the
        # Geofabrik index isn't cached yet.
        from _noaa_tools import geofabrik_regions

        try:
            info = geofabrik_regions.resolve_region(region, use_mock=use_mock)
            bbox = info.bbox
        except KeyError as exc:
            _step_log(step_log, f"region {region!r} not found: {exc}", "error")
            raise

    filtered = ndbc_parse.filter_buoys(
        stations,
        bbox=bbox,
        types=set(types_list) if types_list else None,
        require_fields=tuple(require_list),
        max_stations=max_stations,
    )
    _step_log(
        step_log,
        f"DiscoverBuoys region={region!r} types={types_list} "
        f"require={require_list} → {len(filtered)} station(s)",
        "success",
    )
    return {
        "stations": json.dumps(filtered),
        "station_count": len(filtered),
    }


# ---------------------------------------------------------------------------
# FetchBuoyData — loops over the year range.
# ---------------------------------------------------------------------------

def handle_fetch_buoy_data(params: dict[str, Any]) -> dict[str, Any]:
    station_id = params.get("station_id", "")
    start_year = int(params.get("start_year", 2015))
    end_year = int(params.get("end_year", 2024))
    force = bool(params.get("force", False))
    use_mock = bool(params.get("use_mock", False))
    step_log = params.get("_step_log")

    if not station_id:
        raise ValueError("station_id is required")

    years = list(range(start_year, end_year + 1))
    _step_log(
        step_log,
        f"FetchBuoyData {station_id} years={years[0]}-{years[-1]}",
    )

    fetched = 0
    failed = 0
    for year in years:
        try:
            ndbc_download.download_stdmet(
                station_id, year, force=force, use_mock=use_mock
            )
            fetched += 1
        except Exception as exc:
            logger.warning("%s/%d: %s", station_id, year, exc)
            failed += 1

    _step_log(
        step_log,
        f"FetchBuoyData {station_id}: {fetched} fetched, {failed} failed",
        "success" if failed == 0 else "warning",
    )
    return {
        "station_id": station_id,
        "requested": len(years),
        "fetched": fetched,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# SummarizeBuoy — replicates summarize_buoy.py's core without spawning a
# subprocess. Writes to buoy-summaries/.
# ---------------------------------------------------------------------------

def _yearly_summary(station_id: str, year: int, stdmet_path: Path) -> dict[str, Any] | None:
    try:
        hourly = ndbc_parse.parse_stdmet_gz(str(stdmet_path))
    except (OSError, gzip.BadGzipFile, ValueError) as exc:
        logger.warning("%s/%d: %s", station_id, year, exc)
        return None
    if not hourly:
        return None
    daily = ndbc_parse.daily_from_hourly(hourly)
    if not daily:
        return None

    def _mean(field: str) -> float | None:
        vals = [d[field] for d in daily if d.get(field) is not None]
        if not vals:
            return None
        return round(statistics.fmean(vals), 2)

    ssts = [d["sea_temp"] for d in daily if d.get("sea_temp") is not None]
    waves = [d["wave_height"] for d in daily if d.get("wave_height") is not None]
    return {
        "station_id": station_id,
        "year": year,
        "daily_count": len(daily),
        "air_temp_mean": _mean("air_temp"),
        "sea_temp_mean": _mean("sea_temp"),
        "sea_temp_max": round(max(ssts), 2) if ssts else None,
        "pressure_mean": _mean("pressure"),
        "wind_speed_mean": _mean("wind_speed"),
        "wave_height_mean": _mean("wave_height"),
        "wave_height_max": round(max(waves), 2) if waves else None,
        "high_sst_days": sum(1 for t in ssts if t > HIGH_SST_C),
        "storm_days": sum(1 for w in waves if w > STORM_WAVE_M),
    }


def handle_summarize_buoy(params: dict[str, Any]) -> dict[str, Any]:
    station_id = params.get("station_id", "")
    start_year = int(params.get("start_year", 0))
    end_year = int(params.get("end_year", 0))
    step_log = params.get("_step_log")

    if not station_id:
        raise ValueError("station_id is required")

    storage = LocalStorage()
    stdmet_root = Path(sidecar.cache_dir(
        "noaa-weather", ndbc_download.STDMET_CACHE_TYPE, storage
    )) / station_id
    if not stdmet_root.is_dir():
        _step_log(step_log, f"no cached stdmet for {station_id}", "warning")
        return {
            "station_id": station_id,
            "summary_path": "",
            "years_analyzed": 0,
        }

    # Years actually present on disk, clipped to requested window.
    years: list[int] = []
    for entry in stdmet_root.iterdir():
        if not entry.name.endswith(".txt.gz"):
            continue
        stem = entry.name[: -len(".txt.gz")]
        try:
            years.append(int(stem))
        except ValueError:
            continue
    if start_year:
        years = [y for y in years if y >= start_year]
    if end_year:
        years = [y for y in years if y <= end_year]
    years.sort()

    summaries: list[dict[str, Any]] = []
    for year in years:
        rel = ndbc_download.stdmet_relative_path(station_id, year)
        path = Path(sidecar.cache_path(
            "noaa-weather", ndbc_download.STDMET_CACHE_TYPE, rel, storage
        ))
        summary = _yearly_summary(station_id, year, path)
        if summary is not None:
            summaries.append(summary)

    # Pull the station's catalog metadata so the summary JSON carries
    # name / type / lat / lon — matches what the CLI emits.
    station_meta: dict[str, Any] = {}
    try:
        for s in ndbc_download.read_catalog_stations(use_mock=False):
            if s.get("station_id") == station_id:
                station_meta = s
                break
    except Exception:
        pass

    if not summaries:
        _step_log(step_log, f"no summaries for {station_id}", "warning")
        return {
            "station_id": station_id,
            "summary_path": "",
            "years_analyzed": 0,
        }

    relative_path = f"{station_id}.json"
    output = {
        "station_id": station_id,
        "station_meta": station_meta,
        "years_analyzed": len(summaries),
        "summaries": summaries,
    }
    body = (json.dumps(output, indent=2, sort_keys=True) + "\n").encode("utf-8")
    staging_dir = sidecar.staging_dir("noaa-weather", BUOY_SUMMARY_CACHE_TYPE, storage)
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{station_id}.json.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    final_path = Path(sidecar.cache_path(
        "noaa-weather", BUOY_SUMMARY_CACHE_TYPE, relative_path, storage
    ))
    with sidecar.entry_lock(
        "noaa-weather", BUOY_SUMMARY_CACHE_TYPE, relative_path, storage=storage
    ):
        storage.finalize_from_local(stage_path, str(final_path))
        sidecar.write_sidecar(
            "noaa-weather",
            BUOY_SUMMARY_CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "namespace": "noaa-weather",
                "cache_type": ndbc_download.STDMET_CACHE_TYPE,
                "relative_path_prefix": f"{station_id}/",
            },
            tool={"name": "summarize_buoy", "version": "1.0"},
            extra={
                "station_id": station_id,
                "years_analyzed": len(summaries),
                **{k: station_meta.get(k) for k in ("name", "type", "lat", "lon")},
            },
            storage=storage,
        )

    _step_log(
        step_log,
        f"SummarizeBuoy {station_id}: {len(summaries)} year(s) → {final_path}",
        "success",
    )
    return {
        "station_id": station_id,
        "summary_path": str(final_path),
        "years_analyzed": len(summaries),
    }


# ---------------------------------------------------------------------------
# BuildBuoysMap.
# ---------------------------------------------------------------------------

def handle_build_buoys_map(params: dict[str, Any]) -> dict[str, Any]:
    step_log = params.get("_step_log")
    out = ndbc_map.rebuild_buoys_map()
    if out is None:
        _step_log(step_log, "no cached NDBC catalog — map skipped", "warning")
        return {"html_path": "", "station_count": 0}
    # Crude station count — re-read the catalog sidecar.
    storage = LocalStorage()
    side = sidecar.read_sidecar(
        "noaa-weather", ndbc_download.CATALOG_CACHE_TYPE,
        ndbc_download.CATALOG_JSON_RELATIVE, storage,
    ) or {}
    station_count = int((side.get("extra") or {}).get("station_count", 0))
    _step_log(step_log, f"BuildBuoysMap → {out}", "success")
    return {"html_path": str(out), "station_count": station_count}


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------

_DISPATCH: dict[str, Any] = {
    f"{NAMESPACE}.DownloadNdbcCatalog": handle_download_ndbc_catalog,
    f"{NAMESPACE}.DiscoverBuoys": handle_discover_buoys,
    f"{NAMESPACE}.FetchBuoyData": handle_fetch_buoy_data,
    f"{NAMESPACE}.SummarizeBuoy": handle_summarize_buoy,
    f"{NAMESPACE}.BuildBuoysMap": handle_build_buoys_map,
}


def handle(payload: dict) -> dict:
    """RegistryRunner entrypoint."""
    facet = payload["_facet_name"]
    handler = _DISPATCH[facet]
    return handler(payload)


def register_handlers(runner) -> None:
    """Register with a RegistryRunner."""
    for facet_name in _DISPATCH:
        runner.register_handler(
            facet_name=facet_name,
            module_uri=f"file://{os.path.abspath(__file__)}",
            entrypoint="handle",
        )


def register_marine_handlers(poller) -> None:
    """Register with an AgentPoller."""
    for facet_name, handler in _DISPATCH.items():
        poller.register(facet_name, handler)
