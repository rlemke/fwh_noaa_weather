"""GHCN-Daily text-format parsers (no I/O).

Pure functions that parse the fixed-width ``ghcnd-stations.txt`` and
``ghcnd-inventory.txt`` files published by NOAA, plus the per-station
CSV (``csv/by_station/<id>.csv``). Downloads live in
:mod:`_lib.ghcn_download`; MongoDB persistence lives in the handler
layer. This module is safe to import from any context — it has no
filesystem, network, or database side effects.

NOAA reference:
    https://www1.ncdc.noaa.gov/pub/data/ghcn/daily/readme.txt

Units (important):
- DATA_VALUE is stored in tenths (temperature in tenths of °C,
  precipitation / snow / snow depth in tenths of mm). Parsers here
  convert to real units before returning.
- Q_FLAG is the quality-control flag; a non-empty value means the
  observation FAILED QC and should typically be skipped.
"""

from __future__ import annotations

import csv
from typing import Any

# ---------------------------------------------------------------------------
# US state bounding boxes (approximate lat/lon).
# ---------------------------------------------------------------------------
#
# Used to filter GHCN stations to a state when the catalog only exposes
# lat/lon. Values are (min_lat, max_lat, min_lon, max_lon).

US_STATE_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    "AL": (30.22, 35.01, -88.47, -84.89),
    "AK": (51.21, 71.39, -179.15, -129.98),
    "AZ": (31.33, 37.00, -114.81, -109.04),
    "AR": (33.00, 36.50, -94.62, -89.64),
    "CA": (32.53, 42.01, -124.41, -114.13),
    "CO": (36.99, 41.00, -109.06, -102.04),
    "CT": (40.98, 42.05, -73.73, -71.79),
    "DE": (38.45, 39.84, -75.79, -75.05),
    "FL": (24.40, 31.00, -87.63, -80.03),
    "GA": (30.36, 35.00, -85.61, -80.84),
    "HI": (18.91, 22.24, -160.25, -154.81),
    "ID": (41.99, 49.00, -117.24, -111.04),
    "IL": (36.97, 42.51, -91.51, -87.02),
    "IN": (37.77, 41.76, -88.10, -84.78),
    "IA": (40.38, 43.50, -96.64, -90.14),
    "KS": (36.99, 40.00, -102.05, -94.59),
    "KY": (36.50, 39.15, -89.57, -81.96),
    "LA": (28.93, 33.02, -94.04, -88.82),
    "ME": (43.06, 47.46, -71.08, -66.95),
    "MD": (37.91, 39.72, -79.49, -75.05),
    "MA": (41.24, 42.89, -73.51, -69.93),
    "MI": (41.70, 48.26, -90.42, -82.41),
    "MN": (43.50, 49.38, -97.24, -89.49),
    "MS": (30.17, 34.99, -91.66, -88.10),
    "MO": (35.99, 40.61, -95.77, -89.10),
    "MT": (44.36, 49.00, -116.05, -104.04),
    "NE": (39.99, 43.00, -104.05, -95.31),
    "NV": (35.00, 42.00, -120.01, -114.04),
    "NH": (42.70, 45.31, -72.56, -70.70),
    "NJ": (38.93, 41.36, -75.56, -73.89),
    "NM": (31.33, 37.00, -109.05, -103.00),
    "NY": (40.50, 45.02, -79.76, -71.86),
    "NC": (33.84, 36.59, -84.32, -75.46),
    "ND": (45.94, 49.00, -104.05, -96.55),
    "OH": (38.40, 41.98, -84.82, -80.52),
    "OK": (33.62, 37.00, -103.00, -94.43),
    "OR": (41.99, 46.29, -124.57, -116.46),
    "PA": (39.72, 42.27, -80.52, -74.69),
    "RI": (41.15, 42.02, -71.86, -71.12),
    "SC": (32.05, 35.22, -83.35, -78.54),
    "SD": (42.48, 45.94, -104.06, -96.44),
    "TN": (34.98, 36.68, -90.31, -81.65),
    "TX": (25.84, 36.50, -106.65, -93.51),
    "UT": (36.99, 42.00, -114.05, -109.04),
    "VT": (42.73, 45.02, -73.44, -71.46),
    "VA": (36.54, 39.47, -83.68, -75.24),
    "WA": (45.54, 49.00, -124.85, -116.92),
    "WV": (37.20, 40.64, -82.64, -77.72),
    "WI": (42.49, 47.08, -92.89, -86.25),
    "WY": (40.99, 45.01, -111.06, -104.05),
}


def station_country(station_id: str) -> str:
    """Return the FIPS country code from a GHCN station ID (first 2 chars)."""
    return station_id[:2] if len(station_id) >= 2 else ""


def station_in_state(lat: float, lon: float, state: str) -> bool:
    """Check if coordinates fall within a US state bounding box.

    ``state`` is a 2-letter abbreviation. Returns False for unknown states.
    """
    bounds = US_STATE_BOUNDS.get(state.upper())
    if bounds is None:
        return False
    min_lat, max_lat, min_lon, max_lon = bounds
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# ---------------------------------------------------------------------------
# Station catalog and inventory parsers (fixed-width).
# ---------------------------------------------------------------------------


def parse_stations(text: str) -> list[dict[str, Any]]:
    """Parse ``ghcnd-stations.txt`` fixed-width into a list of dicts.

    Column layout:
        ID         1-11    NAMEXX       42-..
        LATITUDE  13-20    ELEVATION    32-37
        LONGITUDE 22-30

    Returns dicts with keys ``station_id``, ``name``, ``lat``, ``lon``,
    ``elevation``. Malformed lines are skipped silently.
    """
    stations: list[dict[str, Any]] = []
    for line in text.splitlines():
        if len(line) < 38:
            continue
        try:
            station_id = line[0:11].strip()
            lat = float(line[12:20].strip())
            lon = float(line[21:30].strip())
            elev_str = line[31:37].strip()
            elevation = float(elev_str) if elev_str else 0.0
            name = line[41:].strip() if len(line) > 41 else ""
        except (ValueError, IndexError):
            continue
        stations.append(
            {
                "station_id": station_id,
                "name": name,
                "lat": lat,
                "lon": lon,
                "elevation": elevation,
            }
        )
    return stations


def parse_inventory(text: str) -> dict[str, dict[str, Any]]:
    """Parse ``ghcnd-inventory.txt`` into ``{station_id: {...}}``.

    Each value has keys ``elements`` (set of 4-char codes),
    ``first_year`` / ``last_year`` (int, covering the union of all
    elements for that station), and ``element_ranges`` (per-element
    ``(first, last)`` tuple).
    """
    inventory: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        if len(line) < 45:
            continue
        try:
            station_id = line[0:11].strip()
            element = line[31:35].strip()
            first_year = int(line[36:40].strip())
            last_year = int(line[41:45].strip())
        except (ValueError, IndexError):
            continue

        if station_id not in inventory:
            inventory[station_id] = {
                "elements": set(),
                "first_year": first_year,
                "last_year": last_year,
                "element_ranges": {},
            }

        entry = inventory[station_id]
        entry["elements"].add(element)
        entry["element_ranges"][element] = (first_year, last_year)
        if first_year < entry["first_year"]:
            entry["first_year"] = first_year
        if last_year > entry["last_year"]:
            entry["last_year"] = last_year

    return inventory


def filter_stations(
    stations: list[dict[str, Any]],
    inventory: dict[str, dict[str, Any]],
    *,
    country: str = "US",
    state: str = "",
    bbox: tuple[float, float, float, float] | None = None,
    max_stations: int = 10,
    min_years: int = 20,
    required_elements: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter stations by country, state, bbox, data coverage, and elements.

    Filters compose — every one that's set must pass.

    - ``country``: FIPS code prefix (first 2 chars of station_id). Pass
      ``""`` to skip the country filter (useful with ``bbox`` when
      coordinates are the authoritative region constraint).
    - ``state``: US state abbreviation; uses bounding-box match.
    - ``bbox``: ``(min_lat, max_lat, min_lon, max_lon)``. Intended for
      Geofabrik-derived region boxes.

    Returns stations sorted by data coverage (most years first), each
    enriched with ``first_year``, ``last_year``, ``elements`` from the
    inventory. Capped at ``max_stations``.
    """
    if required_elements is None:
        required_elements = ["TMAX", "TMIN", "PRCP"]

    candidates: list[dict[str, Any]] = []
    for stn in stations:
        sid = stn["station_id"]
        if country and station_country(sid) != country:
            continue
        if state and not station_in_state(stn["lat"], stn["lon"], state):
            continue
        if bbox is not None:
            min_lat, max_lat, min_lon, max_lon = bbox
            if not (min_lat <= stn["lat"] <= max_lat and min_lon <= stn["lon"] <= max_lon):
                continue

        inv = inventory.get(sid)
        if inv is None:
            continue
        if not all(el in inv["elements"] for el in required_elements):
            continue

        year_span = inv["last_year"] - inv["first_year"] + 1
        if year_span < min_years:
            continue

        candidates.append(
            {
                **stn,
                "first_year": inv["first_year"],
                "last_year": inv["last_year"],
                "elements": sorted(inv["elements"]),
            }
        )

    candidates.sort(key=lambda s: (-(s["last_year"] - s["first_year"]), s["station_id"]))
    return candidates[:max_stations]


# ---------------------------------------------------------------------------
# Per-station CSV parser.
# ---------------------------------------------------------------------------

_ELEMENT_SET = {"TMAX", "TMIN", "PRCP", "SNOW", "SNWD"}


def parse_ghcn_csv(
    path: str,
    start_year: int,
    end_year: int,
    *,
    skip_flagged: bool = True,
) -> list[dict[str, Any]]:
    """Parse a per-station GHCN CSV and pivot to wide daily format.

    Source CSV columns:
        ID, DATE, ELEMENT, DATA_VALUE, M_FLAG, Q_FLAG, S_FLAG, OBS_TIME

    Returned dicts have keys ``date``, ``tmax``, ``tmin``, ``prcp``,
    ``snow``, ``snwd``. Temperature values are real °C; precipitation
    and snow values are real mm. Q-flagged rows are skipped by default.
    """
    by_date: dict[str, dict[str, float | None]] = {}

    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            try:
                date_str = row[1]
                element = row[2]
                data_value_str = row[3]
                q_flag = row[5] if len(row) > 5 else ""
            except IndexError:
                continue

            if date_str == "DATE":
                continue
            if len(date_str) < 4:
                continue
            try:
                year = int(date_str[:4])
            except ValueError:
                continue
            if year < start_year or year > end_year:
                continue

            if skip_flagged and q_flag.strip():
                continue
            if element not in _ELEMENT_SET:
                continue

            try:
                raw_value = int(data_value_str)
            except (ValueError, TypeError):
                continue

            # Tenths → real units.
            value = raw_value / 10.0
            by_date.setdefault(date_str, {})[element] = value

    daily: list[dict[str, Any]] = []
    for date_str in sorted(by_date):
        vals = by_date[date_str]
        daily.append(
            {
                "date": date_str,
                "tmax": vals.get("TMAX"),
                "tmin": vals.get("TMIN"),
                "prcp": vals.get("PRCP"),
                "snow": vals.get("SNOW"),
                "snwd": vals.get("SNWD"),
            }
        )
    return daily
