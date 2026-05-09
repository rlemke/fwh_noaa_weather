"""Tests for the noaa-weather utility functions and legacy weather_utils."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mock_weather_db(monkeypatch):
    """Patch get_weather_db to return a MagicMock for all tests."""
    mock_db = MagicMock()
    monkeypatch.setattr(
        "noaa_weather.handlers.shared.weather_utils.get_weather_db",
        lambda db=None: db if db is not None else mock_db,
    )
    return mock_db


# ---------------------------------------------------------------------------
# TestWeatherUtils — ISD-Lite utility function tests (weather_utils.py)
# ---------------------------------------------------------------------------
class TestWeatherUtils:
    def test_parse_isd_lite_line_valid(self):
        from noaa_weather.handlers.shared.weather_utils import parse_isd_lite_line

        line = "2023 01 15 12   -50   -80 10130   270    30     4    10 -9999"
        rec = parse_isd_lite_line(line)
        assert rec is not None
        assert rec["date"] == "2023-01-15"
        assert rec["hour"] == 12
        assert rec["air_temp"] == -5.0
        assert rec["dew_point"] == -8.0
        assert rec["sea_level_pressure"] == 1013.0
        assert rec["wind_direction"] == 270
        assert rec["wind_speed"] == 3.0
        assert rec["precipitation"] == 1.0

    def test_parse_isd_lite_line_missing(self):
        from noaa_weather.handlers.shared.weather_utils import parse_isd_lite_line

        line = "2023 06 01 00 -9999 -9999 -9999 -9999 -9999 -9999 -9999 -9999"
        rec = parse_isd_lite_line(line)
        assert rec is not None
        assert rec["air_temp"] is None
        assert rec["dew_point"] is None

    def test_parse_isd_lite_line_malformed(self):
        from noaa_weather.handlers.shared.weather_utils import parse_isd_lite_line

        assert parse_isd_lite_line("short") is None
        assert parse_isd_lite_line("") is None

    def test_compute_daily_stats(self):
        from noaa_weather.handlers.shared.weather_utils import compute_daily_stats

        obs = [
            {
                "date": "2023-01-15",
                "hour": 0,
                "air_temp": -5.0,
                "dew_point": -8.0,
                "precipitation": 2.0,
            },
            {
                "date": "2023-01-15",
                "hour": 12,
                "air_temp": 5.0,
                "dew_point": 0.0,
                "precipitation": 0.0,
            },
            {
                "date": "2023-01-16",
                "hour": 0,
                "air_temp": 3.0,
                "dew_point": 1.0,
                "precipitation": None,
            },
        ]
        stats = compute_daily_stats(obs)
        assert len(stats) == 2
        day1 = [s for s in stats if s["date"] == "2023-01-15"][0]
        assert day1["temp_mean"] == 0.0
        assert day1["temp_min"] == -5.0
        assert day1["temp_max"] == 5.0
        assert day1["precip_total"] == 2.0

    def test_compute_annual_summary(self):
        from noaa_weather.handlers.shared.weather_utils import compute_annual_summary

        daily = [
            {
                "date": "2023-07-15",
                "temp_mean": 30.0,
                "temp_min": 22.0,
                "temp_max": 38.0,
                "precip_total": 5.0,
            },
            {
                "date": "2023-01-15",
                "temp_mean": -5.0,
                "temp_min": -10.0,
                "temp_max": 0.0,
                "precip_total": 0.0,
            },
        ]
        summary = compute_annual_summary(daily)
        assert summary["total_days"] == 2
        assert summary["annual_precip"] == 5.0
        assert summary["temp_max"] == 38.0
        assert summary["temp_min"] == -10.0

    def test_compute_missing_pct(self):
        from noaa_weather.handlers.shared.weather_utils import compute_missing_pct

        obs = [
            {"air_temp": 10.0},
            {"air_temp": None},
            {"air_temp": 15.0},
            {"air_temp": None},
        ]
        pct = compute_missing_pct(obs)
        assert pct == 50.0

    def test_validate_temperature_range(self):
        from noaa_weather.handlers.shared.weather_utils import validate_temperature_range

        # Valid range
        assert validate_temperature_range([{"air_temp": 20.0}, {"air_temp": -10.0}]) is True
        # Out of range
        assert validate_temperature_range([{"air_temp": 200.0}]) is False
        assert validate_temperature_range([{"air_temp": -200.0}]) is False
        # All None values → no temps → False
        assert validate_temperature_range([{"air_temp": None}]) is False

    def test_simple_linear_regression(self):
        from noaa_weather.handlers.shared.weather_utils import simple_linear_regression

        slope, intercept = simple_linear_regression([1, 2, 3, 4], [2, 4, 6, 8])
        assert abs(slope - 2.0) < 1e-9
        assert abs(intercept - 0.0) < 1e-9

    def test_linear_regression_flat(self):
        from noaa_weather.handlers.shared.weather_utils import simple_linear_regression

        slope, intercept = simple_linear_regression([1, 2, 3], [5, 5, 5])
        assert abs(slope) < 1e-9

    def test_linear_regression_empty(self):
        from noaa_weather.handlers.shared.weather_utils import simple_linear_regression

        slope, intercept = simple_linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0


# ---------------------------------------------------------------------------
# Compilation test — FFL compiles to JSON
# ---------------------------------------------------------------------------
class TestCompilation:
    def test_weather_afl_compiles(self):
        from facetwork import parse, validate

        afl_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.ffl")
        with open(afl_path) as f:
            source = f.read()
        program = parse(source)
        result = validate(program)
        assert not result.errors, f"Validation errors: {result.errors}"

    def test_weather_json_exists(self):
        json_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.json")
        assert os.path.exists(json_path), (
            "weather.json not found — run: python3 -m afl.cli weather.afl -o weather.json"
        )
        with open(json_path) as f:
            data = json.load(f)
        assert "declarations" in data

    def test_namespaces_present(self):
        from facetwork import parse

        afl_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.ffl")
        with open(afl_path) as f:
            program = parse(f.read())
        ns_names = [ns.name for ns in program.namespaces]
        assert "weather.types" in ns_names
        assert "weather.Catalog" in ns_names
        assert "weather.Ingest" in ns_names
        assert "weather.Analysis" in ns_names
        assert "weather.Geocode" in ns_names
        assert "weather.workflows" in ns_names
        assert "weather.Cache" in ns_names

    def test_event_facets_defined(self):
        from facetwork import parse

        afl_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.ffl")
        with open(afl_path) as f:
            program = parse(f.read())

        event_names = set()
        for ns in program.namespaces:
            for ef in ns.event_facets:
                event_names.add(ef.sig.name)

        assert "DiscoverStations" in event_names
        assert "FetchStationData" in event_names
        assert "AnalyzeStationClimate" in event_names
        assert "ComputeRegionTrend" in event_names
        assert "ReverseGeocode" in event_names

    def test_workflows_defined(self):
        from facetwork import parse

        afl_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.ffl")
        with open(afl_path) as f:
            program = parse(f.read())

        workflow_names = set()
        for ns in program.namespaces:
            for wf in ns.workflows:
                workflow_names.add(wf.sig.name)

        assert "AnalyzeStation" in workflow_names
        assert "AnalyzeStateTrends" in workflow_names
        assert "AnalyzeAllStates" in workflow_names
        assert "CacheStateData" in workflow_names
        assert "CacheAllUSData" in workflow_names
        # International
        assert "AnalyzeCanada" in workflow_names
        assert "AnalyzeEurope" in workflow_names

    def test_schemas_defined(self):
        from facetwork import parse

        afl_path = os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "ffl", "weather.ffl")
        with open(afl_path) as f:
            program = parse(f.read())

        schema_names = set()
        for ns in program.namespaces:
            for s in ns.schemas:
                schema_names.add(s.name)

        assert "StationInfo" in schema_names
        assert "YearlyClimate" in schema_names
        assert "ClimateTrend" in schema_names
        assert "GeoContext" in schema_names


# ---------------------------------------------------------------------------
# WeatherReportStore (from weather_utils.py — still valid)
# ---------------------------------------------------------------------------

try:
    import mongomock

    HAS_MONGOMOCK = True
except ImportError:
    HAS_MONGOMOCK = False


@pytest.mark.skipif(not HAS_MONGOMOCK, reason="mongomock not installed")
class TestWeatherReportStore:
    @pytest.fixture()
    def store(self):
        client = mongomock.MongoClient()
        db = client["test_weather"]
        from noaa_weather.handlers.shared.weather_utils import WeatherReportStore

        return WeatherReportStore(db)

    def test_upsert_and_find(self, store):
        store.upsert_report(
            station_id="USW00014732",
            station_name="LA GUARDIA",
            year=2020,
            location="NY",
            report={"temp_mean": 12.5},
            daily_stats=[],
        )
        recs = list(store.reports.find({"station_id": "USW00014732"}))
        assert len(recs) == 1
        assert recs[0]["year"] == 2020

    def test_upsert_idempotent(self, store):
        for _ in range(3):
            store.upsert_report(
                station_id="USW00014732",
                station_name="TEST",
                year=2020,
                location="NY",
                report={},
                daily_stats=[],
            )
        assert store.reports.count_documents({"station_id": "USW00014732", "year": 2020}) == 1
