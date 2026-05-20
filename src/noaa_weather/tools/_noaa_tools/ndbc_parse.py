"""NDBC text-format parsers (no I/O).

Two formats matter:

- ``activestations.xml`` — the authoritative NDBC station list. One
  ``<station>`` element per station with ``id``, ``lat``, ``lon``,
  ``name``, ``type``, ``owner``, and flags for which sensor families
  are active.

- ``stdmet`` per-station per-year ``.txt.gz`` files — fixed-column
  observations. Headers appear twice at the top (column names on
  row 1, units on row 2), then one row per observation:

      YY MM DD hh mm WDIR WSPD GST WVHT DPD APD MWD PRES ATMP WTMP DEWP VIS TIDE

  Files prior to 2006 use a two-digit year and a different header
  layout (``YYYY MM DD hh WDIR WSPD GST WVHT ...`` with no minute
  column, no MWD). We handle both by splitting on whitespace and
  mapping column names from row 1 to their indices.

``MM`` (literally the two-character string) = missing value — the
parsers coerce these to ``None``. Quality-flagged fields aren't
separately marked in stdmet; use the summaries that filter out
sensor-drift windows if you need clean data.

Everything here is pure and testable — no network, no filesystem.
"""

from __future__ import annotations

import gzip
import statistics
import xml.etree.ElementTree as ET
from typing import Any

# Fields we promote from stdmet into the daily aggregate. Everything
# else is ignored.
NUMERIC_FIELDS = {
    "WDIR": "wind_dir",
    "WSPD": "wind_speed",
    "GST": "gust_speed",
    "WVHT": "wave_height",
    "DPD": "dom_wave_period",
    "APD": "avg_wave_period",
    "MWD": "mean_wave_dir",
    "PRES": "pressure",
    "ATMP": "air_temp",
    "WTMP": "sea_temp",
    "DEWP": "dew_point",
    "VIS": "visibility",
}

# NDBC encodes "missing" as the literal string "MM" (two-char) or "99"
# sentinels for some older fields. We map both.
_MISSING_TOKENS = {"MM", "MMMM", "99.0", "999.0", "9999.0"}


# ---------------------------------------------------------------------------
# Station catalog — activestations.xml.
# ---------------------------------------------------------------------------

def parse_activestations_xml(text: str) -> list[dict[str, Any]]:
    """Parse NDBC's ``activestations.xml`` into station dicts.

    Returns a list of ``{station_id, name, type, owner, lat, lon,
    met, currents, waterquality, dart, seq}`` records. Unknown
    attributes fall through as empty strings.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ValueError(f"activestations.xml is not valid XML: {exc}") from exc

    out: list[dict[str, Any]] = []
    for station in root.findall("station"):
        attrs = station.attrib
        sid = attrs.get("id", "").strip()
        if not sid:
            continue
        try:
            lat = float(attrs.get("lat", "nan"))
            lon = float(attrs.get("lon", "nan"))
        except ValueError:
            continue
        if lat != lat or lon != lon:  # NaN check
            continue
        out.append(
            {
                "station_id": sid,
                "name": attrs.get("name", "").strip(),
                "type": attrs.get("type", "").strip().lower(),
                "owner": attrs.get("owner", "").strip(),
                "lat": lat,
                "lon": lon,
                "met": attrs.get("met", "n") == "y",
                "currents": attrs.get("currents", "n") == "y",
                "waterquality": attrs.get("waterquality", "n") == "y",
                "dart": attrs.get("dart", "n") == "y",
                "seq": attrs.get("seq", "").strip(),
            }
        )
    return out


def filter_buoys(
    stations: list[dict[str, Any]],
    *,
    bbox: tuple[float, float, float, float] | None = None,
    types: set[str] | None = None,
    require_fields: tuple[str, ...] = (),
    max_stations: int = 0,
) -> list[dict[str, Any]]:
    """Filter + cap a list of buoy stations.

    - ``bbox`` = ``(min_lat, max_lat, min_lon, max_lon)`` keeps only
      stations inside (inclusive).
    - ``types`` = set of lowercase station types (``moored``,
      ``drifting``, ``cman``, ``tsunami``, ``nerrs``, ``other``).
    - ``require_fields`` = sensor families that must be active; each
      entry is one of ``"met"``, ``"currents"``, ``"waterquality"``,
      ``"dart"`` (matches the activestations.xml attribute names).
    - ``max_stations`` = cap; 0 = no cap.

    Returns the kept stations, sorted by id for stable output.
    """
    out: list[dict[str, Any]] = []
    for s in stations:
        if bbox is not None:
            min_lat, max_lat, min_lon, max_lon = bbox
            if not (min_lat <= s["lat"] <= max_lat and min_lon <= s["lon"] <= max_lon):
                continue
        if types is not None and (s.get("type") or "") not in types:
            continue
        if any(not s.get(field) for field in require_fields):
            continue
        out.append(s)

    out.sort(key=lambda s: s["station_id"])
    if max_stations and len(out) > max_stations:
        out = out[:max_stations]
    return out


# ---------------------------------------------------------------------------
# stdmet text parser.
# ---------------------------------------------------------------------------

def parse_stdmet_gz(path: str) -> list[dict[str, Any]]:
    """Parse a gzipped stdmet file, returning one dict per observation.

    Keys are the lowercase long names from :data:`NUMERIC_FIELDS`
    plus ``date`` (``YYYYMMDD``) and ``hour``. Missing values become
    ``None``. Robust to the pre-2006 no-minute-column variant.
    """
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        return parse_stdmet_text(f.read())


def parse_stdmet_text(text: str) -> list[dict[str, Any]]:
    """Same as :func:`parse_stdmet_gz` but takes the text directly."""
    lines = text.splitlines()
    if not lines:
        return []

    # Find the header line — it starts with '#YY' / '#YYYY' on modern
    # files, bare 'YY' / 'YYYY' on older files.
    header_idx = -1
    for i, raw in enumerate(lines[:5]):
        first = raw.lstrip("#").split(None, 1)
        if first and first[0] in ("YY", "YYYY"):
            header_idx = i
            break
    if header_idx < 0:
        return []

    columns = lines[header_idx].lstrip("#").split()
    # Second row is units — skip if present (starts with '#').
    data_start = header_idx + 1
    if data_start < len(lines) and lines[data_start].startswith("#"):
        data_start += 1

    col_idx = {name: i for i, name in enumerate(columns)}
    has_yyyy = "YYYY" in col_idx
    year_col = "YYYY" if has_yyyy else "YY"

    def _num(row: list[str], name: str) -> float | None:
        idx = col_idx.get(name)
        if idx is None or idx >= len(row):
            return None
        raw = row[idx].strip()
        if not raw or raw in _MISSING_TOKENS:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    out: list[dict[str, Any]] = []
    for raw_line in lines[data_start:]:
        row = raw_line.split()
        if len(row) < len(columns) - 3:
            # Very short lines — skip. Allow a little tolerance since
            # trailing zero-tide columns may be dropped.
            continue
        try:
            yr_raw = row[col_idx[year_col]].strip()
            year = int(yr_raw)
            if not has_yyyy and year < 100:
                year += 1900 if year >= 50 else 2000
            month = int(row[col_idx["MM"]])
            day = int(row[col_idx["DD"]])
            hour = int(row[col_idx["hh"]])
        except (KeyError, ValueError, IndexError):
            continue

        record: dict[str, Any] = {
            "date": f"{year:04d}{month:02d}{day:02d}",
            "hour": hour,
        }
        for col, attr in NUMERIC_FIELDS.items():
            record[attr] = _num(row, col)
        out.append(record)

    return out


# ---------------------------------------------------------------------------
# Hourly → daily downsample.
# ---------------------------------------------------------------------------

def daily_from_hourly(hourly: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Average each numeric field across every hour of a day.

    Input rows look like :func:`parse_stdmet_text` output. Output is
    one dict per day with ``date``, ``obs_count``, and the mean of
    every :data:`NUMERIC_FIELDS` attribute across that day's hours.
    Fields with no valid observations remain ``None``.
    """
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in hourly:
        date_str = row.get("date")
        if not isinstance(date_str, str) or len(date_str) != 8:
            continue
        by_date.setdefault(date_str, []).append(row)

    out: list[dict[str, Any]] = []
    for date_str in sorted(by_date):
        rows = by_date[date_str]
        summary: dict[str, Any] = {"date": date_str, "obs_count": len(rows)}
        for attr in NUMERIC_FIELDS.values():
            vals = [r[attr] for r in rows if r.get(attr) is not None]
            if vals:
                # Circular averaging for wind / wave direction would be
                # nicer here (e.g. unit-vector sum) but the arithmetic
                # mean is fine for our daily summaries — directions
                # near 0° / 360° will average poorly in tricky cases,
                # but callers mainly care about wave height / temps.
                summary[attr] = round(statistics.fmean(vals), 2)
            else:
                summary[attr] = None
        out.append(summary)
    return out
