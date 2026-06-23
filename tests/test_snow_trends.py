"""Snow trends: SNOW/SNWD surfaced in yearly summaries + region trend.

Snow is None (not 0) when a station/year logged no SNOW/SNWD, so warm regions /
non-snow stations stay out of the snow regression entirely.
"""

from __future__ import annotations

from noaa_weather.tools._noaa_tools import climate_analysis as C


def _days(year, *, snow=None, snwd=None, n=12):
    return [{"date": f"{year}{m:02d}15", "tmax": 2.0, "tmin": -5.0, "prcp": 3.0,
             "snow": snow, "snwd": snwd} for m in range(1, n + 1)]


def test_yearly_snow_metrics():
    s = C.compute_yearly_summaries(_days(2000, snow=10.0, snwd=50.0), "X", "NY")[0]
    assert s["snow_annual"] == 120.0          # 10mm * 12 days
    assert s["snow_depth_max"] == 50.0
    assert s["snow_days"] == 12


def test_yearly_snow_none_when_absent():
    s = C.compute_yearly_summaries(_days(2000, snow=None, snwd=None), "X", "FL")[0]
    assert s["snow_annual"] is None
    assert s["snow_depth_max"] is None
    assert s["snow_days"] is None


def test_region_snow_trend_rising():
    ys = (C.compute_yearly_summaries(_days(1990, snow=10.0, snwd=20.0), "X", "NY")
          + C.compute_yearly_summaries(_days(2010, snow=30.0, snwd=60.0), "X", "NY"))
    tr = C.aggregate_region_trend(ys, state="NY", start_year=1990, end_year=2010)
    assert tr["has_snow_data"] is True
    assert tr["snow_per_decade_mm"] > 0 and tr["snow_change_pct"] > 0
    assert "snowfall has increased" in tr["narrative"].lower()
    assert tr["decades"]["1990s"]["avg_snow"] == 120.0


def test_region_no_snow_omits_snow():
    ys = (C.compute_yearly_summaries(_days(1990, snow=None, snwd=None), "X", "FL")
          + C.compute_yearly_summaries(_days(2010, snow=None, snwd=None), "X", "FL"))
    tr = C.aggregate_region_trend(ys, state="FL", start_year=1990, end_year=2010)
    assert tr["has_snow_data"] is False
    assert tr["snow_per_decade_mm"] == 0.0
    assert "snowfall" not in tr["narrative"].lower()
    assert tr["decades"]["1990s"]["avg_snow"] is None
