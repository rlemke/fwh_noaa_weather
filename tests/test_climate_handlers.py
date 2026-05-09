"""Tests for GHCN-Daily climate utilities (ghcn_utils.py)."""

from __future__ import annotations

import os
import sys

import pytest

# Ensure handlers package is importable
_examples_dir = os.path.join(os.path.dirname(__file__), "..")
if _examples_dir not in sys.path:
    sys.path.insert(0, _examples_dir)

from noaa_weather.handlers.shared.ghcn_utils import (
    ClimateStore,
    simple_linear_regression,
    station_country,
    station_in_state,
)

try:
    import mongomock

    HAS_MONGOMOCK = True
except ImportError:
    HAS_MONGOMOCK = False


# ---------------------------------------------------------------------------
# station_country / station_in_state
# ---------------------------------------------------------------------------


class TestStationHelpers:
    def test_station_country_us(self):
        assert station_country("USW00014732") == "US"

    def test_station_country_canada(self):
        assert station_country("CA001012475") == "CA"

    def test_station_country_germany(self):
        assert station_country("GM000010147") == "GM"

    def test_station_country_short(self):
        assert station_country("X") == ""

    def test_station_country_empty(self):
        assert station_country("") == ""

    def test_station_in_state_ny(self):
        # NYC coordinates
        assert station_in_state(40.78, -73.97, "NY") is True

    def test_station_in_state_outside(self):
        # London coordinates — not in NY
        assert station_in_state(51.5, -0.12, "NY") is False

    def test_station_in_state_unknown(self):
        assert station_in_state(40.0, -74.0, "ZZ") is False


# ---------------------------------------------------------------------------
# simple_linear_regression (from ghcn_utils)
# ---------------------------------------------------------------------------


class TestGHCNLinearRegression:
    def test_perfect_line(self):
        slope, intercept = simple_linear_regression([1, 2, 3, 4], [2, 4, 6, 8])
        assert abs(slope - 2.0) < 1e-9
        assert abs(intercept - 0.0) < 1e-9

    def test_flat_line(self):
        slope, intercept = simple_linear_regression([1, 2, 3], [5, 5, 5])
        assert abs(slope) < 1e-9
        assert abs(intercept - 5.0) < 1e-9

    def test_single_point(self):
        slope, intercept = simple_linear_regression([3.0], [7.0])
        assert slope == 0.0
        assert intercept == 7.0

    def test_empty_input(self):
        slope, intercept = simple_linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0

    def test_negative_slope(self):
        slope, intercept = simple_linear_regression([0, 1, 2], [10, 8, 6])
        assert abs(slope - (-2.0)) < 1e-9
        assert abs(intercept - 10.0) < 1e-9

    def test_with_offset(self):
        # y = 0.5x + 3
        xs = [0, 2, 4, 6, 8]
        ys = [3, 4, 5, 6, 7]
        slope, intercept = simple_linear_regression(xs, ys)
        assert abs(slope - 0.5) < 1e-9
        assert abs(intercept - 3.0) < 1e-9


# ---------------------------------------------------------------------------
# ClimateStore
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MONGOMOCK, reason="mongomock not installed")
class TestClimateStore:
    @pytest.fixture()
    def store(self):
        client = mongomock.MongoClient()
        db = client["test_climate"]
        return ClimateStore(db)

    def test_upsert_and_get_state_year(self, store):
        data = {
            "state": "NY",
            "year": 2020,
            "station_count": 3,
            "temp_mean": 12.5,
            "temp_min_avg": 5.0,
            "temp_max_avg": 20.0,
            "precip_annual": 1100.0,
            "hot_days": 10,
            "frost_days": 80,
            "precip_days": 120,
        }
        store.upsert_state_year(data)
        results = store.get_state_years("NY")
        assert len(results) == 1
        assert results[0]["year"] == 2020
        assert results[0]["temp_mean"] == 12.5

    def test_upsert_and_get_trend(self, store):
        trend = {
            "state": "NY",
            "start_year": 1944,
            "end_year": 2024,
            "warming_rate_per_decade": 0.15,
            "precip_change_pct": 5.0,
            "decades": {"1940s": {"avg_temp": 10.0}},
        }
        store.upsert_trend(trend)
        result = store.get_trend("NY")
        assert result is not None
        assert result["warming_rate_per_decade"] == 0.15

    def test_list_states(self, store):
        store.upsert_trend({"state": "NY", "warming_rate_per_decade": 0.1})
        store.upsert_trend({"state": "CA", "warming_rate_per_decade": 0.2})
        states = store.list_states()
        assert states == ["CA", "NY"]

    def test_get_state_years_range(self, store):
        for y in range(2018, 2023):
            store.upsert_state_year({"state": "TX", "year": y, "temp_mean": 20.0 + y - 2018})
        results = store.get_state_years("TX", 2019, 2021)
        years = [r["year"] for r in results]
        assert years == [2019, 2020, 2021]

    def test_get_narrative(self, store):
        store.upsert_trend({"state": "FL", "narrative": "Hot and getting hotter."})
        assert store.get_narrative("FL") == "Hot and getting hotter."

    def test_get_narrative_missing(self, store):
        assert store.get_narrative("ZZ") is None
