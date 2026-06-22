"""Handler-side compatibility shim for the noaa-weather pipeline.

The real implementation lives in ``src/noaa_weather/tools/_noaa_tools/``.
It is shared verbatim by:

- the ``download-ghcn-catalog`` / ``fetch-station-csv`` / ``summarize-
  station`` / ``compute-region-trend`` / ``reverse-geocode`` CLI tools
  (``src/noaa_weather/tools/``), and
- the FFL catalog / ingest / analysis / geocode handlers (this
  package).

Both entry points read and write the same on-disk cache
(``$AFL_DATA_ROOT/cache/noaa-weather/...``) with per-entry
``.meta.json`` sidecars — the tool and the FFL are two surfaces onto
one cache.

This module exposes the legacy ``download_station_catalog`` /
``download_inventory`` / ``download_station_csv`` / ``reverse_geocode_
nominatim`` names by wrapping the new ``_noaa_tools`` APIs. Parser and
analysis functions are re-exported without change.

The MongoDB helpers (``get_weather_db``, ``WeatherReportStore``,
``ClimateStore``) stay here — ``_noaa_tools`` must not import ``pymongo`` so
the tools can run standalone, without a Mongo cluster.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any

# Import the real implementation via the fully-qualified package path so we
# don't compete with other example packages for the bare ``_noaa_tools`` name on
# sys.modules (osm-geocoder ships its own ``_noaa_tools`` under
# ``osm_geocoder/tools/_noaa_tools/``; loading both entry points in the same
# Python process would otherwise hand whichever package imported first).
from noaa_weather.tools._noaa_tools import (  # noqa: F401
    climate_analysis,
    climate_report,
    extremes,
    geocode_nominatim,
    geofabrik_regions,
    ghcn_download,
    ghcn_parse,
    natural_earth,
    ndbc_download,
    ndbc_map,
    ndbc_parse,
)
from noaa_weather.tools._noaa_tools.climate_analysis import (  # noqa: F401
    aggregate_region_trend,
    compute_yearly_summaries,
    simple_linear_regression,
)
from noaa_weather.tools._noaa_tools.extremes import (  # noqa: F401
    ExtremeConfig,
    detect_events,
)
from noaa_weather.tools._noaa_tools.ghcn_parse import (  # noqa: F401
    US_STATE_BOUNDS,
    filter_stations,
    parse_ghcn_csv,
    parse_inventory,
    parse_stations,
    station_country,
    station_in_state,
)

logger = logging.getLogger("ghcn")


# ---------------------------------------------------------------------------
# Download wrappers — preserve legacy return shapes (plain text / file path).
# ---------------------------------------------------------------------------


def download_station_catalog(*, force: bool = False) -> str:
    """Return the cached ``ghcnd-stations.txt`` text, downloading if stale."""
    return ghcn_download.read_catalog_file("stations", force=force)


def download_inventory(*, force: bool = False) -> str:
    """Return the cached ``ghcnd-inventory.txt`` text, downloading if stale."""
    return ghcn_download.read_catalog_file("inventory", force=force)


def download_station_csv(station_id: str, *, force: bool = False) -> str:
    """Return the local path of the cached per-station CSV.

    The underlying download library returns a :class:`DownloadResult`;
    handlers historically expected a path string, so this thin wrapper
    preserves that contract.
    """
    res = ghcn_download.download_station_csv(station_id, force=force)
    return res.absolute_path


def reverse_geocode_nominatim(
    lat: float, lon: float, *, use_mock: bool | None = None
) -> dict[str, Any]:
    """Reverse geocode ``(lat, lon)`` via Nominatim with on-disk cache.

    ``use_mock`` forces the deterministic offline path (useful for tests
    that cannot rely on network or installed ``requests``).
    """
    return geocode_nominatim.reverse_geocode(lat, lon, use_mock=use_mock)


# ---------------------------------------------------------------------------
# MongoDB helpers — stay in the handler layer so ``_noaa_tools`` remains
# database-free (the CLI tools must be runnable without a Mongo cluster).
# ---------------------------------------------------------------------------


def get_weather_db(db: Any = None) -> Any:
    """Return a MongoDB database handle for weather report storage.

    If *db* is provided (e.g. injected from a test), return it unchanged.
    Otherwise connect via ``AFL_MONGODB_URL`` / ``AFL_EXAMPLES_DATABASE``.
    """
    if db is not None:
        return db
    from pymongo import MongoClient

    url = os.environ.get("AFL_MONGODB_URL")
    if not url:
        raise RuntimeError(
            "AFL_MONGODB_URL is not set — cannot connect to MongoDB for weather reports"
        )
    db_name = os.environ.get("AFL_EXAMPLES_DATABASE", "facetwork_examples")
    return MongoClient(url)[db_name]


class WeatherReportStore:
    """Mongo wrapper for ``weather_reports`` and ``weather_batch_summaries``."""

    def __init__(self, db: Any) -> None:
        self.reports = db["weather_reports"]
        self.batches = db["weather_batch_summaries"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.reports.create_index([("station_id", 1), ("year", 1)], unique=True)
        self.reports.create_index([("updated_at", -1)])
        self.batches.create_index([("batch_id", 1)], unique=True)

    def upsert_report(
        self,
        station_id: str,
        station_name: str,
        year: int,
        location: str,
        report: dict[str, Any],
        daily_stats: list[dict[str, Any]],
    ) -> str:
        now = datetime.datetime.now(datetime.UTC)
        self.reports.update_one(
            {"station_id": station_id, "year": year},
            {
                "$set": {
                    "station_name": station_name,
                    "location": location,
                    "report": report,
                    "daily_stats": daily_stats,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return f"weather://{station_id}/{year}"

    def upsert_html(self, station_id: str, year: int, html_content: str) -> str:
        now = datetime.datetime.now(datetime.UTC)
        self.reports.update_one(
            {"station_id": station_id, "year": year},
            {
                "$set": {"html_content": html_content, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return f"weather://{station_id}/{year}"

    def upsert_map(self, station_id: str, year: int, map_content: str) -> str:
        now = datetime.datetime.now(datetime.UTC)
        self.reports.update_one(
            {"station_id": station_id, "year": year},
            {
                "$set": {"map_content": map_content, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return f"weather://{station_id}/{year}"

    def upsert_batch(
        self,
        batch_id: str,
        station_count: int,
        completed: int,
        failed: int,
        results: list[dict[str, Any]],
        summary: str,
    ) -> str:
        now = datetime.datetime.now(datetime.UTC)
        self.batches.update_one(
            {"batch_id": batch_id},
            {
                "$set": {
                    "station_count": station_count,
                    "completed": completed,
                    "failed": failed,
                    "results": results,
                    "summary": summary,
                    "updated_at": now,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        return f"weather://batch/{batch_id}"

    def get_report(self, station_id: str, year: int) -> dict[str, Any] | None:
        return self.reports.find_one({"station_id": station_id, "year": year}, {"_id": 0})

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self.reports.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit))


class ClimateStore:
    """Mongo wrapper for ``climate_state_years`` and ``climate_trends``."""

    def __init__(self, db: Any) -> None:
        self.state_years = db["climate_state_years"]
        self.trends = db["climate_trends"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.state_years.create_index([("state", 1), ("year", 1)], unique=True)
        self.trends.create_index([("state", 1)], unique=True)

    def upsert_state_year(self, data: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.UTC)
        self.state_years.update_one(
            {"state": data["state"], "year": data["year"]},
            {"$set": {**data, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def upsert_trend(self, data: dict[str, Any]) -> None:
        now = datetime.datetime.now(datetime.UTC)
        self.trends.update_one(
            {"state": data["state"]},
            {"$set": {**data, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def get_state_years(
        self, state: str, start_year: int = 0, end_year: int = 9999
    ) -> list[dict[str, Any]]:
        return list(
            self.state_years.find(
                {"state": state, "year": {"$gte": start_year, "$lte": end_year}},
                {"_id": 0},
            ).sort("year", 1)
        )

    def get_trend(self, state: str) -> dict[str, Any] | None:
        return self.trends.find_one({"state": state}, {"_id": 0})

    def list_states(self) -> list[str]:
        return sorted(self.trends.distinct("state"))

    def get_narrative(self, state: str) -> str | None:
        doc = self.trends.find_one({"state": state}, {"_id": 0, "narrative": 1})
        if doc:
            return doc.get("narrative")
        return None


__all__ = [
    # Parser re-exports.
    "US_STATE_BOUNDS",
    "filter_stations",
    "parse_ghcn_csv",
    "parse_inventory",
    "parse_stations",
    "station_country",
    "station_in_state",
    # Analysis re-exports.
    "aggregate_region_trend",
    "compute_yearly_summaries",
    "simple_linear_regression",
    # Extreme-event detection re-exports.
    "extremes",
    "ExtremeConfig",
    "detect_events",
    # Download wrappers (legacy-compatible shapes).
    "download_inventory",
    "download_station_catalog",
    "download_station_csv",
    "reverse_geocode_nominatim",
    # Module re-exports for handlers that need the full surface.
    "climate_analysis",
    "climate_report",
    "geocode_nominatim",
    "geofabrik_regions",
    "ghcn_download",
    "ghcn_parse",
    "natural_earth",
    "ndbc_download",
    "ndbc_map",
    "ndbc_parse",
    # Mongo (handler-only — not part of the ``_noaa_tools`` surface).
    "ClimateStore",
    "WeatherReportStore",
    "get_weather_db",
]
