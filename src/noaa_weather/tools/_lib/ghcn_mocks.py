"""Deterministic mock fallbacks for offline / no-network testing.

Activated by :mod:`_lib.ghcn_download` when the ``requests`` library
is unavailable, or by explicit ``use_mock=True`` callers. Tests rely
on these being stable across runs — changes here break fixtures.
"""

from __future__ import annotations

import hashlib


def hash_int(seed: str, lo: int, hi: int) -> int:
    """Deterministic integer in ``[lo, hi)`` from a seed string."""
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % (hi - lo))


def hash_float(seed: str, lo: float, hi: float) -> float:
    """Deterministic float in ``[lo, hi)`` from a seed string."""
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + (h % 10000) / 10000 * (hi - lo)


def mock_station_catalog() -> str:
    """Small representative ``ghcnd-stations.txt`` for offline use."""
    lines = [
        "USW00094728  40.7789  -73.9692    39.6    NEW YORK CENTRAL PARK OBS",
        "USW00014732  40.7794  -73.8803     3.4    LA GUARDIA AIRPORT",
        "USW00014734  40.6833  -74.1694     2.1    NEWARK LIBERTY INTL AP",
        "USW00012839  25.7906  -80.3164     8.8    MIAMI INTL AP",
        "USW00094846  41.9950  -87.9336   201.8    CHICAGO OHARE INTL AP",
        "USW00023234  32.8983  -97.0192   170.7    DALLAS FT WORTH INTL AP",
        "USW00024233  47.4489  -122.3094   132.6    SEATTLE TACOMA INTL AP",
        "USW00023174  33.9381  -118.3894    29.6    LOS ANGELES INTL AP",
        "USW00023174  37.6197  -122.3647     2.4    SAN FRANCISCO INTL AP",
        "USW00014922  44.8831  -93.2289   255.1    MINNEAPOLIS ST PAUL INTL AP",
        "USW00013874  33.6301  -84.4419   315.2    ATLANTA HARTSFIELD INTL AP",
        "USW00014739  42.3606  -71.0106     9.1    BOSTON LOGAN INTL AP",
        "CA006158731  43.6772  -79.6306   173.4    TORONTO PEARSON INTL",
        "GME00127786  50.0500    8.6000   112.0    FRANKFURT MAIN",
        "UK000056225  51.4780   -0.4610    25.0    LONDON HEATHROW",
        "FR000007157  49.0128    2.5494   119.0    PARIS CHARLES DE GAULLE",
        "RSM00027612  55.9722   37.4153   167.0    MOSCOW SHEREMETYEVO",
        "IN022021600  28.5850   77.2060   216.0    NEW DELHI SAFDARJUNG",
    ]
    return "\n".join(lines) + "\n"


def mock_inventory() -> str:
    """Small representative ``ghcnd-inventory.txt`` for offline use."""
    stations = [
        ("USW00094728", 40.7789, -73.9692),
        ("USW00014732", 40.7794, -73.8803),
        ("USW00014734", 40.6833, -74.1694),
        ("USW00012839", 25.7906, -80.3164),
        ("USW00094846", 41.9950, -87.9336),
        ("USW00023234", 32.8983, -97.0192),
        ("USW00024233", 47.4489, -122.3094),
        ("USW00023174", 33.9381, -118.3894),
        ("USW00014922", 44.8831, -93.2289),
        ("USW00013874", 33.6301, -84.4419),
        ("USW00014739", 42.3606, -71.0106),
        ("CA006158731", 43.6772, -79.6306),
        ("GME00127786", 50.0500, 8.6000),
        ("UK000056225", 51.4780, -0.4610),
    ]
    elements = ["TMAX", "TMIN", "PRCP", "SNOW", "SNWD"]
    lines: list[str] = []
    for sid, lat, lon in stations:
        for el in elements:
            start = hash_int(f"{sid}:start", 1940, 1970)
            end = hash_int(f"{sid}:end", 2020, 2024)
            lines.append(f"{sid:<11s} {lat:8.4f} {lon:9.4f} {el:<4s} {start:4d} {end:4d}")
    return "\n".join(lines) + "\n"


def mock_station_csv(
    station_id: str,
    start_year: int,
    end_year: int,
) -> str:
    """Deterministic per-station CSV for offline use.

    Columns match real GHCN: ``ID,DATE,ELEMENT,DATA_VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME``.
    Temperatures follow a rough seasonal curve; precipitation is sparse.
    """
    rows: list[str] = []
    days_per_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            for day in range(1, days_per_month[month - 1] + 1):
                date_str = f"{year:04d}{month:02d}{day:02d}"
                seed = f"{station_id}:{date_str}"

                # Seasonal temperature base (tenths of °C).
                seasonal = -50 + 250 * (1 - abs(month - 7) / 6)
                tmax = int(seasonal + hash_float(seed + ":tmax", 0, 100))
                tmin = int(seasonal - hash_float(seed + ":tmin", 0, 100))

                has_precip = hash_int(seed + ":hp", 0, 10) > 6
                prcp = hash_int(seed + ":prcp", 0, 300) if has_precip else 0

                snow = 0
                if month in (1, 2, 3, 11, 12) and tmin < 0:
                    snow = hash_int(seed + ":snow", 0, 500) if has_precip else 0

                for element, value in [
                    ("TMAX", tmax),
                    ("TMIN", tmin),
                    ("PRCP", prcp),
                    ("SNOW", snow),
                ]:
                    rows.append(f"{station_id},{date_str},{element},{value},,,S,")

    return "\n".join(rows) + "\n"
