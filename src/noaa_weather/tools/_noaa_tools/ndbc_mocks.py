"""Deterministic offline NDBC data for tests + `--use-mock` CLI runs.

Produces a small but realistic-ish fake `activestations.xml` (5
stations spread across the Atlantic / Pacific / Great Lakes) plus a
synthetic stdmet text file that mirrors the real column layout. All
values are deterministic hashes of ``(station_id, date)`` so test
fixtures remain stable.
"""

from __future__ import annotations

import hashlib


def hash_int(seed: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % (hi - lo))


def hash_float(seed: str, lo: float, hi: float) -> float:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % 10000) / 10000 * (hi - lo)


# ---------------------------------------------------------------------------
# Mock active-stations XML.
# ---------------------------------------------------------------------------

def mock_activestations_xml() -> str:
    """Small representative ``activestations.xml`` for offline use.

    Covers the station types our filters care about: ocean moored
    buoys (Atlantic + Pacific), a drifting buoy, a C-MAN coastal
    station, a Great Lakes buoy, and a tsunami DART — enough
    diversity that ``discover-buoys`` filtering has something to
    exercise.
    """
    stations = [
        # id, lat, lon, name, type, owner, met, currents, waterquality, dart
        ("41001", 34.68, -72.66, "E150 - East Hatteras",
         "buoy", "NDBC", "y", "n", "n", "n"),
        ("46042", 36.79, -122.40, "Monterey - 27NM West",
         "buoy", "NDBC", "y", "y", "n", "n"),
        ("44025", 40.25, -73.16, "Long Island - 30NM SSW of Islip",
         "buoy", "NDBC", "y", "n", "n", "n"),
        ("45001", 47.30, -86.00, "Lake Superior - North Central",
         "buoy", "Environment Canada", "y", "n", "n", "n"),
        ("tplm2", 38.90, -76.44, "Thomas Point Light, MD",
         "cman", "NDBC", "y", "n", "y", "n"),
        ("42001", 25.90, -89.67, "East Gulf",
         "buoy", "NDBC", "y", "y", "n", "n"),
        ("21413", 30.47, 152.00, "Northwest of Wake Island",
         "dart", "NDBC", "n", "n", "n", "y"),
    ]
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<stations created="2026-04-23T00:00:00Z">',
    ]
    for sid, lat, lon, name, type_, owner, met, cur, wq, dart in stations:
        attrs = (
            f'id="{sid}" lat="{lat}" lon="{lon}" '
            f'name="{name}" type="{type_}" owner="{owner}" '
            f'met="{met}" currents="{cur}" waterquality="{wq}" dart="{dart}"'
        )
        lines.append(f'  <station {attrs}/>')
    lines.append('</stations>')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Mock stdmet (hourly) for one station-year.
# ---------------------------------------------------------------------------

def mock_stdmet_text(station_id: str, year: int, *, hours_per_day: int = 4) -> str:
    """Deterministic stdmet text for one station-year.

    Emits ``hours_per_day`` observations per day (every 6 hours by
    default) for the entire year. Values are hashed by
    ``(station_id, YYYYMMDDhh)`` so fixtures are reproducible. The
    column layout exactly matches modern NDBC stdmet (post-2006).
    """
    # Header rows (modern layout).
    out = [
        "#YY  MM DD hh mm WDIR WSPD GST  WVHT   DPD   APD MWD   PRES  ATMP  WTMP  DEWP  VIS  TIDE",
        "#yr  mo dy hr mn degT m/s  m/s  m     sec   sec degT  hPa   degC  degC  degC  mi   ft",
    ]

    hour_step = max(1, 24 // hours_per_day)
    for month in range(1, 13):
        days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
        for day in range(1, days_in_month + 1):
            for hour in range(0, 24, hour_step):
                seed = f"{station_id}:{year:04d}{month:02d}{day:02d}{hour:02d}"
                # Coastal/ocean temps stay in a narrower range than
                # continental land.
                base_air = 12 + 8 * (1 - abs(month - 7) / 6)
                atmp = round(base_air + hash_float(seed + ":atmp", -3, 3), 1)
                # SST lags air temp seasonally — a crude approximation.
                wtmp = round(base_air - 1 + hash_float(seed + ":wtmp", -2, 2), 1)
                pres = round(1013 + hash_float(seed + ":pres", -5, 5), 1)
                wspd = round(hash_float(seed + ":wspd", 2, 14), 1)
                wvht = round(hash_float(seed + ":wvht", 0.5, 3.5), 2)
                gust = round(wspd + hash_float(seed + ":gst", 1, 4), 1)
                row = (
                    f"{year:4d}{month:3d}{day:3d}{hour:3d} 00 "
                    f"{hash_int(seed + ':wdir', 0, 360):4d} "
                    f"{wspd:4.1f} {gust:4.1f} "
                    f"{wvht:5.2f} {hash_float(seed + ':dpd', 3, 12):5.1f} "
                    f"{hash_float(seed + ':apd', 2, 8):5.1f} "
                    f"{hash_int(seed + ':mwd', 0, 360):3d} "
                    f"{pres:6.1f} {atmp:5.1f} {wtmp:5.1f} "
                    f"{atmp - hash_float(seed + ':dewp', 1, 4):5.1f} "
                    f"{hash_float(seed + ':vis', 5, 15):4.1f} 0.0"
                )
                out.append(row)
    return "\n".join(out) + "\n"
