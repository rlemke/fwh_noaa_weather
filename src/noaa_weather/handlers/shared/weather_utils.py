"""Shared utility functions for the noaa-weather example.

All functions are pure and deterministic — they use hashlib for reproducible
test outputs rather than random data.  When the ``requests`` library is
available and real data exists, download functions fetch from NOAA; otherwise
they fall back to hash-based mock data.
"""

from __future__ import annotations

import csv
import datetime
import hashlib
import html as _html_mod
import io
import json
import logging
import os
import threading
import time
from typing import Any

from facetwork.config import get_output_base

logger = logging.getLogger("noaa.download")

_LOCAL_OUTPUT = get_output_base()
_WEATHER_CACHE_DIR = os.path.join(_LOCAL_OUTPUT, "weather-cache")
_GEOCODE_CACHE_DIR = os.path.join(_LOCAL_OUTPUT, "weather-geocode-cache")

ISD_LITE_URL_TEMPLATE = (
    "https://www.ncei.noaa.gov/pub/data/noaa/isd-lite/{year}/{usaf}-{wban}-{year}.gz"
)

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    import folium

    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# ---------------------------------------------------------------------------
# MongoDB store for weather reports
# ---------------------------------------------------------------------------


def get_weather_db(db: Any = None) -> Any:
    """Return a MongoDB database handle for weather report storage.

    If *db* is already provided (e.g. for testing), return it as-is.
    Otherwise connect via ``AFL_MONGODB_URL`` / ``AFL_EXAMPLES_DATABASE``.

    Example data is stored in a separate database (default ``afl_examples``)
    so that ``db.dropDatabase()`` on the FFL runtime database does not
    destroy cached weather reports and climate trends.
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
    """Lightweight wrapper around two MongoDB collections for weather outputs."""

    def __init__(self, db: Any) -> None:
        self.reports = db["weather_reports"]
        self.batches = db["weather_batch_summaries"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.reports.create_index([("station_id", 1), ("year", 1)], unique=True)
        self.reports.create_index([("updated_at", -1)])
        self.batches.create_index([("batch_id", 1)], unique=True)

    # -- report fields --------------------------------------------------------

    def upsert_report(
        self,
        station_id: str,
        station_name: str,
        year: int,
        location: str,
        report: dict[str, Any],
        daily_stats: list[dict[str, Any]],
    ) -> str:
        """Upsert the core report fields into *weather_reports*."""
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
        """Set *html_content* on the report document."""
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
        """Set *map_content* on the report document."""
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
        """Upsert a batch summary document."""
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
        """Retrieve a single report document."""
        return self.reports.find_one({"station_id": station_id, "year": year}, {"_id": 0})

    def list_reports(self, limit: int = 20) -> list[dict[str, Any]]:
        """List recent reports, newest first."""
        return list(self.reports.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit))


# Per-path download locks
_download_locks: dict[str, threading.Lock] = {}
_lock_guard = threading.Lock()

# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _hash_int(seed: str, lo: int, hi: int) -> int:
    """Deterministic integer from a seed string."""
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % (hi - lo))


def _hash_float(seed: str, lo: float, hi: float) -> float:
    """Deterministic float from a seed string."""
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % 10000) / 10000 * (hi - lo)


# ---------------------------------------------------------------------------
# ISD-Lite parsing
# ---------------------------------------------------------------------------

# ISD-Lite fixed-width column specs (start, end) — 0-indexed
_ISD_COLS = {
    "year": (0, 4),
    "month": (5, 7),
    "day": (8, 10),
    "hour": (11, 13),
    "air_temp": (14, 19),  # scaled ×10
    "dew_point": (20, 25),  # scaled ×10
    "sea_level_pressure": (26, 31),  # scaled ×10
    "wind_direction": (32, 37),
    "wind_speed": (38, 43),  # scaled ×10
    "sky_condition": (44, 49),
    "precip_1h": (50, 55),  # scaled ×10
    "precip_6h": (56, 61),  # scaled ×10
}


def parse_isd_lite_line(line: str) -> dict[str, Any] | None:
    """Parse a single ISD-Lite fixed-width line into a dict.

    Returns None if the line is too short to parse.
    Missing values (-9999) are converted to None.
    Temperature, dew point, pressure, wind speed, and precip are scaled by /10.
    """
    if len(line.rstrip()) < 44:
        return None

    def _field(name: str) -> int | None:
        start, end = _ISD_COLS[name]
        if end > len(line):
            return None
        raw = line[start:end].strip()
        if not raw or raw.lstrip("-").isdigit() is False:
            return None
        val = int(raw)
        return None if val == -9999 else val

    year = _field("year")
    month = _field("month")
    day = _field("day")
    hour = _field("hour")

    if year is None or month is None or day is None or hour is None:
        return None

    air_temp_raw = _field("air_temp")
    dew_point_raw = _field("dew_point")
    slp_raw = _field("sea_level_pressure")
    wind_dir = _field("wind_direction")
    wind_speed_raw = _field("wind_speed")
    sky_raw = _field("sky_condition")

    # Precipitation fields may not be present (line too short)
    precip_1h_raw = _field("precip_1h") if len(line.rstrip()) >= 55 else None
    precip_6h_raw = _field("precip_6h") if len(line.rstrip()) >= 61 else None

    # Choose best available precipitation
    precip = None
    if precip_1h_raw is not None:
        precip = precip_1h_raw / 10.0
    elif precip_6h_raw is not None:
        precip = precip_6h_raw / 10.0

    return {
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "hour": hour,
        "air_temp": air_temp_raw / 10.0 if air_temp_raw is not None else None,
        "dew_point": dew_point_raw / 10.0 if dew_point_raw is not None else None,
        "sea_level_pressure": slp_raw / 10.0 if slp_raw is not None else None,
        "wind_direction": wind_dir,
        "wind_speed": wind_speed_raw / 10.0 if wind_speed_raw is not None else None,
        "precipitation": precip,
        "sky_condition": sky_raw,
    }


def parse_isd_lite_file(path: str) -> list[dict[str, Any]]:
    """Parse an ISD-Lite file (plain text or gzipped) into a list of observations."""
    import gzip as _gzip

    open_fn = _gzip.open if path.endswith(".gz") else open
    records: list[dict[str, Any]] = []
    with open_fn(path, "rt", errors="replace") as f:
        for line in f:
            rec = parse_isd_lite_line(line)
            if rec is not None:
                records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Station inventory
# ---------------------------------------------------------------------------


def parse_station_inventory(csv_text: str) -> list[dict[str, Any]]:
    """Parse isd-history.csv content into a list of station metadata dicts."""
    reader = csv.DictReader(io.StringIO(csv_text))
    stations: list[dict[str, Any]] = []
    for row in reader:
        try:
            lat = float(row.get("LAT", "0") or "0")
            lon = float(row.get("LON", "0") or "0")
            elev = float(row.get("ELEV(M)", "0") or "0")
        except (ValueError, TypeError):
            continue
        stations.append(
            {
                "usaf": row.get("USAF", ""),
                "wban": row.get("WBAN", ""),
                "station_name": row.get("STATION NAME", ""),
                "country": row.get("CTRY", ""),
                "state": row.get("STATE", ""),
                "lat": lat,
                "lon": lon,
                "elevation": elev,
                "begin_date": row.get("BEGIN", ""),
                "end_date": row.get("END", ""),
            }
        )
    return stations


def filter_active_stations(
    stations: list[dict[str, Any]],
    country: str = "US",
    state: str = "",
    max_stations: int = 10,
) -> list[dict[str, Any]]:
    """Filter stations by country/state, keeping the first *max_stations*."""
    filtered = []
    for s in stations:
        if s["country"] != country:
            continue
        if state and s.get("state", "") != state:
            continue
        # Skip stations without valid coordinates
        if s["lat"] == 0.0 and s["lon"] == 0.0:
            continue
        filtered.append(s)
        if len(filtered) >= max_stations:
            break
    return filtered


# ---------------------------------------------------------------------------
# QC utilities
# ---------------------------------------------------------------------------


def compute_missing_pct(observations: list[dict[str, Any]]) -> float:
    """Compute percentage of observations with missing air temperature."""
    if not observations:
        return 100.0
    missing = sum(1 for o in observations if o.get("air_temp") is None)
    return missing / len(observations) * 100


def validate_temperature_range(observations: list[dict[str, Any]]) -> bool:
    """Check that all temperatures are in plausible range -90°C to 60°C."""
    temps = [o["air_temp"] for o in observations if o.get("air_temp") is not None]
    if not temps:
        return False
    return all(-90 <= t <= 60 for t in temps)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def compute_daily_stats(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group hourly observations by date and compute daily aggregates."""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for obs in observations:
        d = obs.get("date", "")
        if d:
            by_date.setdefault(d, []).append(obs)

    daily: list[dict[str, Any]] = []
    for date in sorted(by_date):
        recs = by_date[date]
        temps = [r["air_temp"] for r in recs if r.get("air_temp") is not None]
        winds = [r["wind_speed"] for r in recs if r.get("wind_speed") is not None]
        precips = [r["precipitation"] for r in recs if r.get("precipitation") is not None]

        daily.append(
            {
                "date": date,
                "temp_min": min(temps) if temps else None,
                "temp_max": max(temps) if temps else None,
                "temp_mean": round(sum(temps) / len(temps), 1) if temps else None,
                "precip_total": round(sum(precips), 1) if precips else 0.0,
                "wind_max": max(winds) if winds else None,
                "obs_count": len(recs),
            }
        )
    return daily


def compute_annual_summary(daily_stats: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute annual summary from daily stats."""
    total_precip = sum(d.get("precip_total", 0) or 0 for d in daily_stats)
    all_mins = [d["temp_min"] for d in daily_stats if d.get("temp_min") is not None]
    all_maxs = [d["temp_max"] for d in daily_stats if d.get("temp_max") is not None]
    return {
        "total_days": len(daily_stats),
        "annual_precip": round(total_precip, 1),
        "temp_min": min(all_mins) if all_mins else None,
        "temp_max": max(all_maxs) if all_maxs else None,
    }


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


def reverse_geocode_nominatim(lat: float, lon: float) -> dict[str, Any]:
    """Reverse geocode via OSM Nominatim with filesystem cache and rate limiting.

    Falls back to hash-based mock if requests is unavailable or the API fails.
    """
    cache_key = f"{lat:.4f}_{lon:.4f}"
    cache_path = os.path.join(_GEOCODE_CACHE_DIR, f"{cache_key}.json")

    # Check cache first
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)

    if HAS_REQUESTS:
        try:
            import time

            url = f"https://nominatim.openstreetmap.org/reverse?lat={lat}&lon={lon}&format=json"
            resp = requests.get(url, headers={"User-Agent": "Facetwork/0.34"}, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                addr = data.get("address", {})
                result = {
                    "display_name": data.get("display_name", ""),
                    "city": addr.get("city", addr.get("town", addr.get("village", ""))),
                    "state": addr.get("state", ""),
                    "country": addr.get("country", ""),
                    "county": addr.get("county", ""),
                }
                os.makedirs(_GEOCODE_CACHE_DIR, exist_ok=True)
                with open(cache_path, "w") as f:
                    json.dump(result, f)
                time.sleep(1)  # Rate limit: 1 req/sec
                return result
        except Exception:
            pass

    # Hash-based mock fallback
    seed = f"geo:{lat}:{lon}"
    return {
        "display_name": f"Location at {lat:.2f}, {lon:.2f}",
        "city": f"City-{_hash_int(seed + ':city', 1000, 9999)}",
        "state": f"State-{_hash_int(seed + ':state', 10, 99)}",
        "country": "US",
        "county": f"County-{_hash_int(seed + ':county', 100, 999)}",
    }


# ---------------------------------------------------------------------------
# Downloads
# ---------------------------------------------------------------------------


def _get_lock(path: str) -> threading.Lock:
    """Get or create a per-path download lock."""
    with _lock_guard:
        if path not in _download_locks:
            _download_locks[path] = threading.Lock()
        return _download_locks[path]


def download_station_inventory(
    cache_path: str | None = None,
    max_age_hours: float = 24.0,
) -> str:
    """Download isd-history.csv from NOAA, returning the CSV text.

    Uses a file cache at *cache_path*.  Skips re-download if the cached file
    is less than *max_age_hours* old.  Returns hash-based mock if requests
    is unavailable.
    """
    if cache_path is None:
        cache_path = os.path.join(_WEATHER_CACHE_DIR, "isd-history.csv")

    lock = _get_lock(cache_path)
    with lock:
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
            age_s = time.time() - os.path.getmtime(cache_path)
            if age_s < max_age_hours * 3600:
                logger.info(
                    "Station inventory cache hit (%s, %.1fh old)",
                    cache_path,
                    age_s / 3600,
                )
                with open(cache_path) as f:
                    return f.read()
            logger.info(
                "Station inventory cache stale (%.1fh > %.1fh), re-downloading",
                age_s / 3600,
                max_age_hours,
            )

        if HAS_REQUESTS:
            try:
                url = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200:
                    text = resp.text
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "w") as f:
                        f.write(text)
                    return text
            except Exception:
                pass

    # Mock fallback — return a small CSV with deterministic stations
    return _mock_station_csv()


def _mock_station_csv() -> str:
    """Generate a small mock isd-history.csv for testing."""
    header = '"USAF","WBAN","STATION NAME","CTRY","STATE","LAT","LON","ELEV(M)","BEGIN","END"'
    rows = [
        '"725030","14732","LA GUARDIA AIRPORT","US","NY","40.779","-73.880","3.4","19730101","20231231"',
        '"724050","13743","JOHN F KENNEDY INTL AP","US","NY","40.639","-73.762","3.4","19730101","20231231"',
        '"725020","14734","NEWARK LIBERTY INTL AP","US","NJ","40.683","-74.169","2.1","19730101","20231231"',
        '"722020","12839","MIAMI INTL AP","US","FL","25.791","-80.316","8.8","19730101","20231231"',
        '"725300","94846","CHICAGO OHARE INTL AP","US","IL","41.995","-87.934","201.8","19580101","20231231"',
        '"722740","23234","DALLAS FT WORTH INTL AP","US","TX","32.898","-97.019","170.7","19730101","20231231"',
        '"727930","24233","SEATTLE TACOMA INTL AP","US","WA","47.449","-122.309","132.6","19480101","20231231"',
        '"723860","23183","LOS ANGELES INTL AP","US","CA","33.938","-118.389","29.6","19440101","20231231"',
        '"724940","23174","SAN FRANCISCO INTL AP","US","CA","37.620","-122.365","2.4","19730101","20231231"',
        '"726580","14922","MINNEAPOLIS ST PAUL INTL AP","US","MN","44.883","-93.229","255.1","19380101","20231231"',
        '"722190","13874","ATLANTA HARTSFIELD INTL AP","US","GA","33.630","-84.442","315.2","19730101","20231231"',
        '"725090","14735","BOSTON LOGAN INTL AP","US","MA","42.361","-71.010","9.1","19360101","20231231"',
        # International stations
        '"712000","99999","TORONTO PEARSON INTL","CA","ON","43.677","-79.631","173.4","19530101","20231231"',
        '"035270","99999","LONDON HEATHROW","UK","","51.478","-0.461","25.0","19480101","20231231"',
        '"103910","99999","FRANKFURT MAIN","GM","","50.050","8.600","112.0","19490101","20231231"',
        '"071490","99999","PARIS CHARLES DE GAULLE","FR","","49.013","2.549","119.0","19740101","20231231"',
        '"260390","99999","MOSCOW SHEREMETYEVO","RS","","55.972","37.415","167.0","19600101","20231231"',
        '"428270","99999","NEW DELHI SAFDARJUNG","IN","","28.585","77.206","216.0","19440101","20231231"',
        '"837490","99999","BRASILIA","BR","","15.869","-47.921","1061.0","19620101","20231231"',
        '"476620","99999","TOKYO INTL","JA","","35.553","139.781","8.0","19560101","20231231"',
        '"042180","99999","NUUK","GL","","64.175","-51.748","80.0","19580101","20231231"',
        '"890010","99999","MCMURDO STATION","AY","","77.850","166.667","24.0","19560101","20231231"',
        '"766920","99999","MEXICO CITY INTL","MX","","19.436","-99.072","2238.0","19440101","20231231"',
        '"688160","99999","JOHANNESBURG INTL","SF","","26.139","28.246","1694.0","19520101","20231231"',
        '"545110","99999","BEIJING","CH","","39.933","116.283","55.0","19450101","20231231"',
        '"875760","99999","BUENOS AIRES","AR","","34.822","-58.536","20.0","19470101","20231231"',
        '"082210","99999","MADRID BARAJAS","SP","","40.472","-3.561","609.0","19510101","20231231"',
    ]
    return header + "\n" + "\n".join(rows) + "\n"


def download_isd_lite(usaf: str, wban: str, year: int, cache_dir: str | None = None) -> str:
    """Download an ISD-Lite file and return the local path.

    Returns a cached path if already downloaded.  Falls back to generating
    a mock file if requests is unavailable.
    """
    if cache_dir is None:
        cache_dir = os.path.join(_WEATHER_CACHE_DIR, "isd-lite")
    station_id = f"{usaf}-{wban}"
    filename = f"{usaf}-{wban}-{year}.gz"
    local_path = os.path.join(cache_dir, filename)

    lock = _get_lock(local_path)
    with lock:
        if os.path.exists(local_path) and os.path.getsize(local_path) > 0:
            size = os.path.getsize(local_path)
            logger.info("Cache hit %s-%d (%s bytes)", station_id, year, f"{size:,}")
            return local_path

        if HAS_REQUESTS:
            url = ISD_LITE_URL_TEMPLATE.format(year=year, usaf=usaf, wban=wban)
            logger.info("Downloading %s-%d from %s", station_id, year, url)
            t0 = time.monotonic()
            try:
                resp = requests.get(url, timeout=30)
                elapsed = time.monotonic() - t0
                if resp.status_code == 200:
                    size = len(resp.content)
                    os.makedirs(cache_dir, exist_ok=True)
                    with open(local_path, "wb") as f:
                        f.write(resp.content)
                    logger.info(
                        "Download complete %s-%d: %s bytes in %.1fs",
                        station_id,
                        year,
                        f"{size:,}",
                        elapsed,
                    )
                    return local_path
                logger.warning(
                    "Download failed %s-%d: HTTP %d in %.1fs",
                    station_id,
                    year,
                    resp.status_code,
                    elapsed,
                )
            except Exception as exc:
                elapsed = time.monotonic() - t0
                logger.warning(
                    "Download error %s-%d: %s in %.1fs",
                    station_id,
                    year,
                    exc,
                    elapsed,
                )

    if HAS_REQUESTS:
        # Real download was attempted but failed — raise instead of
        # silently returning mock data in production.
        raise RuntimeError(
            f"Failed to download ISD-Lite data for {station_id}-{year} "
            f"from {ISD_LITE_URL_TEMPLATE.format(year=year, usaf=usaf, wban=wban)}"
        )

    # Mock fallback — only when requests is not installed (test/dev)
    logger.info("Using mock data for %s-%d (requests not installed)", station_id, year)
    mock_path = os.path.join(cache_dir, f"{station_id}-{year}-mock.txt")
    os.makedirs(cache_dir, exist_ok=True)
    with open(mock_path, "w") as f:
        f.write(_generate_mock_isd_lite(station_id, year))
    return mock_path


def _generate_mock_isd_lite(station_id: str, year: int) -> str:
    """Generate mock ISD-Lite data for a station-year (deterministic)."""
    lines: list[str] = []
    for month in range(1, 13):
        days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        for day in range(1, days_in_month + 1):
            for hour in range(0, 24, 3):  # Every 3 hours for mock
                seed = f"{station_id}:{year}:{month}:{day}:{hour}"
                # Base temp varies by month (seasonal pattern)
                seasonal_base = -5 + 25 * (1 - abs(month - 7) / 6)
                temp = int((_hash_float(seed + ":temp", -5, 5) + seasonal_base) * 10)
                dew = temp - _hash_int(seed + ":dew", 20, 100)
                slp = _hash_int(seed + ":slp", 10050, 10350)
                wdir = _hash_int(seed + ":wdir", 0, 360)
                wspd = _hash_int(seed + ":wspd", 0, 150)
                sky = _hash_int(seed + ":sky", 0, 8)
                precip = (
                    _hash_int(seed + ":precip", 0, 50)
                    if _hash_int(seed + ":has_precip", 0, 10) > 7
                    else 0
                )

                line = f"{year:4d} {month:02d} {day:02d} {hour:02d}{temp:6d}{dew:6d}{slp:6d}{wdir:6d}{wspd:6d}{sky:6d}{precip:6d} -9999"
                lines.append(line)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def generate_station_report(
    station_id: str,
    station_name: str,
    year: int,
    location: str,
    daily_stats: list[dict[str, Any]],
    annual_precip: float,
    narrative: str,
    db: Any = None,
) -> dict[str, Any]:
    """Generate a station report and upsert to MongoDB."""
    summary = compute_annual_summary(daily_stats)
    temp_min = summary.get("temp_min")
    temp_max = summary.get("temp_max")
    temp_range = f"{temp_min}°C to {temp_max}°C" if temp_min is not None else "N/A"

    report_data = {
        "total_days": summary["total_days"],
        "annual_precip": annual_precip,
        "temp_range": temp_range,
        "narrative": narrative,
    }

    store = WeatherReportStore(get_weather_db(db))
    report_id = store.upsert_report(
        station_id,
        station_name,
        year,
        location,
        report_data,
        daily_stats,
    )

    return {
        "station_id": station_id,
        "station_name": station_name,
        "year": year,
        "location": location,
        "total_days": summary["total_days"],
        "annual_precip": annual_precip,
        "temp_range": temp_range,
        "narrative": narrative,
        "report_id": report_id,
    }


def generate_batch_summary(
    batch_id: str,
    station_count: int,
    results: list[dict[str, Any]],
    db: Any = None,
) -> tuple[str, int, int, str]:
    """Generate a batch summary and upsert to MongoDB.

    Returns (report_id, completed_count, failed_count, summary_text).
    """
    completed = sum(1 for r in results if r.get("status") == "completed")
    failed = station_count - completed

    summary_text = f"Batch {batch_id}: {completed}/{station_count} completed, {failed} failed"

    store = WeatherReportStore(get_weather_db(db))
    report_id = store.upsert_batch(
        batch_id,
        station_count,
        completed,
        failed,
        results,
        summary_text,
    )

    return report_id, completed, failed, summary_text


# ---------------------------------------------------------------------------
# Narrative fallback
# ---------------------------------------------------------------------------


def generate_narrative_fallback(
    station_name: str,
    year: int,
    daily_stats: list[dict[str, Any]],
    geo_context: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Generate a deterministic narrative summary (prompt block fallback).

    Returns (narrative_text, highlights_list).
    """
    if not daily_stats:
        return f"No data available for {station_name} in {year}.", []

    # Find extremes
    hottest = max(
        (d for d in daily_stats if d.get("temp_max") is not None),
        key=lambda d: d["temp_max"],
        default=None,
    )
    coldest = min(
        (d for d in daily_stats if d.get("temp_min") is not None),
        key=lambda d: d["temp_min"],
        default=None,
    )
    wettest = max(
        (d for d in daily_stats if d.get("precip_total") is not None),
        key=lambda d: d["precip_total"],
        default=None,
    )

    highlights: list[dict[str, str]] = []
    parts: list[str] = []

    location = ""
    if geo_context:
        city = geo_context.get("city", "")
        state = geo_context.get("state", "")
        if city and state:
            location = f" near {city}, {state}"

    parts.append(f"{station_name}{location} recorded {len(daily_stats)} days of data in {year}.")

    if hottest:
        parts.append(f"The hottest day was {hottest['date']} at {hottest['temp_max']}°C.")
        highlights.append(
            {"type": "hottest", "date": hottest["date"], "value": f"{hottest['temp_max']}°C"}
        )

    if coldest:
        parts.append(f"The coldest day was {coldest['date']} at {coldest['temp_min']}°C.")
        highlights.append(
            {"type": "coldest", "date": coldest["date"], "value": f"{coldest['temp_min']}°C"}
        )

    if wettest and wettest.get("precip_total", 0) > 0:
        parts.append(
            f"The wettest day was {wettest['date']} with {wettest['precip_total']}mm of precipitation."
        )
        highlights.append(
            {"type": "wettest", "date": wettest["date"], "value": f"{wettest['precip_total']}mm"}
        )

    return " ".join(parts), highlights


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


def render_html_report(
    station_id: str,
    station_name: str,
    year: int,
    location: str,
    daily_stats: list[dict[str, Any]],
    annual_precip: float,
    temp_range: str,
    narrative: str,
    db: Any = None,
) -> str:
    """Generate a self-contained HTML report and upsert to MongoDB.

    Returns a ``weather://`` report ID.
    """
    total_days = len(daily_stats)
    esc = _html_mod.escape

    rows = ""
    for d in daily_stats:
        rows += (
            "<tr>"
            f"<td>{esc(str(d.get('date', '')))}</td>"
            f"<td>{d.get('temp_min', '')}</td>"
            f"<td>{d.get('temp_max', '')}</td>"
            f"<td>{d.get('temp_mean', '')}</td>"
            f"<td>{d.get('precip_total', '')}</td>"
            f"<td>{d.get('wind_max', '')}</td>"
            f"<td>{d.get('obs_count', '')}</td>"
            "</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{esc(station_name)} — {year} Weather Report</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 2em; color: #333; }}
h1 {{ color: #1a5276; }}
.summary {{ background: #eaf2f8; padding: 1em; border-radius: 6px; margin-bottom: 1em; }}
.summary span {{ margin-right: 2em; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1em; }}
th, td {{ border: 1px solid #bbb; padding: 6px 10px; text-align: right; }}
th {{ background: #2980b9; color: #fff; }}
tr:nth-child(even) {{ background: #f2f2f2; }}
td:first-child, th:first-child {{ text-align: left; }}
.narrative {{ margin-top: 1.5em; line-height: 1.6; }}
</style></head><body>
<h1>{esc(station_name)} — {year} Weather Report</h1>
<p><em>{esc(location)}</em> | Station {esc(station_id)}</p>
<div class="summary">
<span><strong>Annual Precip:</strong> {annual_precip} mm</span>
<span><strong>Temp Range:</strong> {esc(temp_range)}</span>
<span><strong>Days:</strong> {total_days}</span>
</div>
<div class="narrative"><p>{esc(narrative)}</p></div>
<table>
<tr><th>Date</th><th>Min °C</th><th>Max °C</th><th>Mean °C</th><th>Precip mm</th><th>Wind Max m/s</th><th>Obs</th></tr>
{rows}</table>
</body></html>"""

    store = WeatherReportStore(get_weather_db(db))
    return store.upsert_html(station_id, year, html)


# ---------------------------------------------------------------------------
# Station map
# ---------------------------------------------------------------------------


def render_station_map(
    station_id: str,
    station_name: str,
    lat: float,
    lon: float,
    year: int,
    temp_range: str,
    db: Any = None,
) -> str:
    """Generate an interactive folium map and upsert to MongoDB.

    Returns a ``weather://`` report ID, or empty string if folium is unavailable.
    """
    if not HAS_FOLIUM:
        return ""

    m = folium.Map(location=[lat, lon], zoom_start=10)

    popup_html = (
        f"<b>{_html_mod.escape(station_name)}</b><br>"
        f"ID: {_html_mod.escape(station_id)}<br>"
        f"Year: {year}<br>"
        f"Temp range: {_html_mod.escape(temp_range)}"
    )
    folium.Marker(
        location=[lat, lon],
        popup=folium.Popup(popup_html, max_width=250),
        tooltip=station_name,
    ).add_to(m)

    title_html = (
        f'<div style="position:fixed;top:10px;left:60px;z-index:9999;'
        f"background:white;padding:8px 14px;border-radius:6px;"
        f'box-shadow:0 2px 6px rgba(0,0,0,.3);font-family:Arial,sans-serif;">'
        f"<b>{_html_mod.escape(station_name)}</b> — {year}</div>"
    )
    m.get_root().html.add_child(folium.Element(title_html))

    map_content = m.get_root().render()

    store = WeatherReportStore(get_weather_db(db))
    return store.upsert_map(station_id, year, map_content)


# ---------------------------------------------------------------------------
# Linear regression (pure Python, no numpy)
# ---------------------------------------------------------------------------


def simple_linear_regression(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Ordinary least-squares regression.  Returns (slope, intercept).

    With fewer than 2 points, returns (0.0, ys[0]) or (0.0, 0.0).
    """
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return 0.0, ys[0]

    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_xx = sum(x * x for x in xs)

    denom = n * sum_xx - sum_x * sum_x
    if abs(denom) < 1e-12:
        return 0.0, sum_y / n

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


# ---------------------------------------------------------------------------
# Climate store (state-year summaries and trends)
# ---------------------------------------------------------------------------


class ClimateStore:
    """MongoDB wrapper for climate_state_years and climate_trends collections."""

    def __init__(self, db: Any) -> None:
        self.state_years = db["climate_state_years"]
        self.trends = db["climate_trends"]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        self.state_years.create_index([("state", 1), ("year", 1)], unique=True)
        self.trends.create_index([("state", 1)], unique=True)

    def upsert_state_year(self, data: dict[str, Any]) -> None:
        """Upsert a yearly climate summary."""
        now = datetime.datetime.now(datetime.UTC)
        self.state_years.update_one(
            {"state": data["state"], "year": data["year"]},
            {"$set": {**data, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def upsert_trend(self, data: dict[str, Any]) -> None:
        """Upsert a climate trend document."""
        now = datetime.datetime.now(datetime.UTC)
        self.trends.update_one(
            {"state": data["state"]},
            {"$set": {**data, "updated_at": now}, "$setOnInsert": {"created_at": now}},
            upsert=True,
        )

    def get_state_years(
        self, state: str, start_year: int = 0, end_year: int = 9999
    ) -> list[dict[str, Any]]:
        """Query yearly climate data for a state within a year range."""
        return list(
            self.state_years.find(
                {"state": state, "year": {"$gte": start_year, "$lte": end_year}},
                {"_id": 0},
            ).sort("year", 1)
        )

    def get_trend(self, state: str) -> dict[str, Any] | None:
        """Retrieve the trend document for a state."""
        return self.trends.find_one({"state": state}, {"_id": 0})

    def list_states(self) -> list[str]:
        """Return distinct state codes that have trend data."""
        return sorted(self.trends.distinct("state"))

    def get_narrative(self, state: str) -> str | None:
        """Retrieve the narrative from the trend document."""
        doc = self.trends.find_one({"state": state}, {"_id": 0, "narrative": 1})
        if doc:
            return doc.get("narrative")
        return None
