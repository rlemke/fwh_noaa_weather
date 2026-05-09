"""Tests for GHCN-Daily handlers (catalog, ingest, analysis, geocode).

All tests run offline — no network access or MongoDB required.
Uses mocks and monkeypatching throughout.
"""

from __future__ import annotations

import csv
import json
import os
import sys

import pytest

# Ensure handlers package is importable
_examples_dir = os.path.join(os.path.dirname(__file__), "..")
if _examples_dir not in sys.path:
    sys.path.insert(0, _examples_dir)

from noaa_weather.handlers.shared.ghcn_utils import (
    ClimateStore,
    WeatherReportStore,
    compute_yearly_summaries,
    filter_stations,
    parse_ghcn_csv,
    parse_inventory,
    parse_stations,
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
# Test data helpers
# ---------------------------------------------------------------------------

SAMPLE_STATIONS_TEXT = """\
USW00094728  40.7789  -73.9692    39.6    NEW YORK CENTRAL PARK OBS
USW00014732  40.7794  -73.8803     3.4    LA GUARDIA AIRPORT
USW00012839  25.7906  -80.3164     8.8    MIAMI INTL AP
CA006158731  43.6772  -79.6306   173.4    TORONTO PEARSON INTL
GME00127786  50.0500    8.6000   112.0    FRANKFURT MAIN
"""

SAMPLE_INVENTORY_TEXT = """\
USW00094728  40.7789  -73.9692 TMAX 1940 2024
USW00094728  40.7789  -73.9692 TMIN 1940 2024
USW00094728  40.7789  -73.9692 PRCP 1940 2024
USW00094728  40.7789  -73.9692 SNOW 1950 2024
USW00014732  40.7794  -73.8803 TMAX 1960 2024
USW00014732  40.7794  -73.8803 TMIN 1960 2024
USW00014732  40.7794  -73.8803 PRCP 1960 2024
USW00012839  25.7906  -80.3164 TMAX 1970 2024
USW00012839  25.7906  -80.3164 TMIN 1970 2024
USW00012839  25.7906  -80.3164 PRCP 1970 2024
CA006158731  43.6772  -79.6306 TMAX 1950 2023
CA006158731  43.6772  -79.6306 TMIN 1950 2023
CA006158731  43.6772  -79.6306 PRCP 1950 2023
GME00127786  50.0500    8.6000 TMAX 1980 2010
GME00127786  50.0500    8.6000 TMIN 1980 2010
"""


def _make_csv_file(tmp_path, station_id: str, rows: list[tuple]) -> str:
    """Create a GHCN CSV file with given rows.

    Each row is (date, element, value, q_flag).
    """
    path = str(tmp_path / f"{station_id}.csv")
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        for date, element, value, q_flag in rows:
            writer.writerow([station_id, date, element, value, "", q_flag, "S", ""])
    return path


# ---------------------------------------------------------------------------
# ghcn_utils — station_country
# ---------------------------------------------------------------------------


class TestStationCountry:
    def test_us_station(self):
        assert station_country("USW00094728") == "US"

    def test_canadian_station(self):
        assert station_country("CA006158731") == "CA"

    def test_german_station(self):
        assert station_country("GME00127786") == "GM"

    def test_short_id(self):
        assert station_country("U") == ""

    def test_empty_id(self):
        assert station_country("") == ""


# ---------------------------------------------------------------------------
# ghcn_utils — station_in_state
# ---------------------------------------------------------------------------


class TestStationInState:
    def test_nyc_in_ny(self):
        assert station_in_state(40.78, -73.97, "NY") is True

    def test_miami_not_in_ny(self):
        assert station_in_state(25.79, -80.32, "NY") is False

    def test_miami_in_fl(self):
        assert station_in_state(25.79, -80.32, "FL") is True

    def test_unknown_state(self):
        assert station_in_state(40.0, -74.0, "ZZ") is False

    def test_case_insensitive(self):
        assert station_in_state(40.78, -73.97, "ny") is True


# ---------------------------------------------------------------------------
# ghcn_utils — parse_stations
# ---------------------------------------------------------------------------


class TestParseStations:
    def test_parse_sample(self):
        stations = parse_stations(SAMPLE_STATIONS_TEXT)
        assert len(stations) == 5
        central_park = stations[0]
        assert central_park["station_id"] == "USW00094728"
        assert central_park["name"] == "NEW YORK CENTRAL PARK OBS"
        assert abs(central_park["lat"] - 40.7789) < 0.001
        assert abs(central_park["lon"] - (-73.9692)) < 0.001
        assert abs(central_park["elevation"] - 39.6) < 2.0

    def test_empty_text(self):
        assert parse_stations("") == []

    def test_short_lines_skipped(self):
        assert parse_stations("short\nline") == []

    def test_malformed_coords_skipped(self):
        text = "USW00094728  notanum  -73.9692    39.6    SOME STATION"
        assert parse_stations(text) == []


# ---------------------------------------------------------------------------
# ghcn_utils — parse_inventory
# ---------------------------------------------------------------------------


class TestParseInventory:
    def test_parse_sample(self):
        inv = parse_inventory(SAMPLE_INVENTORY_TEXT)
        assert "USW00094728" in inv
        assert "TMAX" in inv["USW00094728"]["elements"]
        assert "TMIN" in inv["USW00094728"]["elements"]
        assert "PRCP" in inv["USW00094728"]["elements"]
        assert "SNOW" in inv["USW00094728"]["elements"]
        assert inv["USW00094728"]["first_year"] == 1940
        assert inv["USW00094728"]["last_year"] == 2024

    def test_element_ranges(self):
        inv = parse_inventory(SAMPLE_INVENTORY_TEXT)
        ranges = inv["USW00094728"]["element_ranges"]
        assert ranges["TMAX"] == (1940, 2024)
        assert ranges["SNOW"] == (1950, 2024)

    def test_empty_text(self):
        assert parse_inventory("") == {}

    def test_short_lines_skipped(self):
        assert parse_inventory("too short") == {}

    def test_german_station_limited_elements(self):
        inv = parse_inventory(SAMPLE_INVENTORY_TEXT)
        # GME00127786 only has TMAX and TMIN (no PRCP)
        assert "GME00127786" in inv
        assert inv["GME00127786"]["elements"] == {"TMAX", "TMIN"}


# ---------------------------------------------------------------------------
# ghcn_utils — filter_stations
# ---------------------------------------------------------------------------


class TestFilterStations:
    @pytest.fixture()
    def parsed(self):
        stations = parse_stations(SAMPLE_STATIONS_TEXT)
        inventory = parse_inventory(SAMPLE_INVENTORY_TEXT)
        return stations, inventory

    def test_filter_us_ny(self, parsed):
        stations, inventory = parsed
        result = filter_stations(
            stations, inventory, country="US", state="NY", max_stations=10, min_years=20
        )
        # Only USW00094728 and USW00014732 are in NY bounding box with all elements
        assert len(result) >= 1
        ids = [s["station_id"] for s in result]
        assert "USW00094728" in ids

    def test_filter_canadian(self, parsed):
        stations, inventory = parsed
        result = filter_stations(stations, inventory, country="CA", max_stations=10, min_years=20)
        assert len(result) == 1
        assert result[0]["station_id"] == "CA006158731"

    def test_filter_max_stations(self, parsed):
        stations, inventory = parsed
        result = filter_stations(stations, inventory, country="US", max_stations=1, min_years=10)
        assert len(result) <= 1

    def test_filter_min_years(self, parsed):
        stations, inventory = parsed
        # German station has 31 years (1980-2010) but only TMAX+TMIN
        result = filter_stations(
            stations, inventory, country="GM", min_years=20, required_elements=["TMAX", "TMIN"]
        )
        assert len(result) == 1
        assert result[0]["station_id"] == "GME00127786"

    def test_filter_required_elements_excludes(self, parsed):
        stations, inventory = parsed
        # German station doesn't have PRCP — default required_elements excludes it
        result = filter_stations(stations, inventory, country="GM", min_years=20)
        assert len(result) == 0

    def test_enriched_fields(self, parsed):
        stations, inventory = parsed
        result = filter_stations(
            stations, inventory, country="US", state="NY", max_stations=10, min_years=20
        )
        assert len(result) > 0
        s = result[0]
        assert "first_year" in s
        assert "last_year" in s
        assert "elements" in s
        assert isinstance(s["elements"], list)


# ---------------------------------------------------------------------------
# ghcn_utils — parse_ghcn_csv
# ---------------------------------------------------------------------------


class TestParseGhcnCsv:
    def test_basic_parse(self, tmp_path):
        rows = [
            ("20200101", "TMAX", "250", ""),
            ("20200101", "TMIN", "100", ""),
            ("20200101", "PRCP", "50", ""),
            ("20200102", "TMAX", "200", ""),
            ("20200102", "TMIN", "80", ""),
        ]
        path = _make_csv_file(tmp_path, "USW00094728", rows)
        daily = parse_ghcn_csv(path, 2020, 2020)
        assert len(daily) == 2
        assert daily[0]["date"] == "20200101"
        assert daily[0]["tmax"] == 25.0  # 250 / 10
        assert daily[0]["tmin"] == 10.0
        assert daily[0]["prcp"] == 5.0

    def test_year_filter(self, tmp_path):
        rows = [
            ("20190615", "TMAX", "300", ""),
            ("20200615", "TMAX", "310", ""),
            ("20210615", "TMAX", "320", ""),
        ]
        path = _make_csv_file(tmp_path, "USW00094728", rows)
        daily = parse_ghcn_csv(path, 2020, 2020)
        assert len(daily) == 1
        assert daily[0]["date"] == "20200615"

    def test_quality_flagged_skipped(self, tmp_path):
        rows = [
            ("20200101", "TMAX", "250", ""),
            ("20200101", "TMIN", "100", "X"),  # flagged
        ]
        path = _make_csv_file(tmp_path, "USW00094728", rows)
        daily = parse_ghcn_csv(path, 2020, 2020)
        assert len(daily) == 1
        assert daily[0]["tmin"] is None  # flagged row skipped

    def test_quality_flagged_kept_when_disabled(self, tmp_path):
        rows = [
            ("20200101", "TMAX", "250", ""),
            ("20200101", "TMIN", "100", "X"),
        ]
        path = _make_csv_file(tmp_path, "USW00094728", rows)
        daily = parse_ghcn_csv(path, 2020, 2020, skip_flagged=False)
        assert daily[0]["tmin"] == 10.0

    def test_empty_file(self, tmp_path):
        path = str(tmp_path / "empty.csv")
        with open(path, "w") as f:
            f.write("")
        assert parse_ghcn_csv(path, 2020, 2020) == []

    def test_snow_elements(self, tmp_path):
        rows = [
            ("20200115", "SNOW", "300", ""),
            ("20200115", "SNWD", "1500", ""),
        ]
        path = _make_csv_file(tmp_path, "USW00094728", rows)
        daily = parse_ghcn_csv(path, 2020, 2020)
        assert len(daily) == 1
        assert daily[0]["snow"] == 30.0
        assert daily[0]["snwd"] == 150.0


# ---------------------------------------------------------------------------
# ghcn_utils — compute_yearly_summaries
# ---------------------------------------------------------------------------


class TestComputeYearlySummaries:
    def test_basic_summary(self):
        daily = [
            {
                "date": "20200101",
                "tmax": 5.0,
                "tmin": -3.0,
                "prcp": 2.0,
                "snow": None,
                "snwd": None,
            },
            {
                "date": "20200715",
                "tmax": 35.0,
                "tmin": 22.0,
                "prcp": 0.0,
                "snow": None,
                "snwd": None,
            },
            {
                "date": "20200716",
                "tmax": 36.0,
                "tmin": 23.0,
                "prcp": 5.0,
                "snow": None,
                "snwd": None,
            },
        ]
        summaries = compute_yearly_summaries(daily, "USW00094728", "NY")
        assert len(summaries) == 1
        s = summaries[0]
        assert s["year"] == 2020
        assert s["station_id"] == "USW00094728"
        assert s["state"] == "NY"
        assert s["obs_days"] == 3
        assert s["hot_days"] == 1  # only 36.0 > 35.0 (strict inequality)
        assert s["frost_days"] == 1  # -3.0 < 0.0
        assert s["precip_days"] == 2  # 2.0 and 5.0 > 0.0

    def test_multi_year(self):
        daily = [
            {
                "date": "20200101",
                "tmax": 10.0,
                "tmin": 0.0,
                "prcp": 1.0,
                "snow": None,
                "snwd": None,
            },
            {
                "date": "20210101",
                "tmax": 12.0,
                "tmin": 2.0,
                "prcp": 2.0,
                "snow": None,
                "snwd": None,
            },
        ]
        summaries = compute_yearly_summaries(daily, "USW00094728", "NY")
        assert len(summaries) == 2
        assert summaries[0]["year"] == 2020
        assert summaries[1]["year"] == 2021

    def test_empty_data(self):
        assert compute_yearly_summaries([], "USW00094728", "NY") == []

    def test_temp_mean_calculation(self):
        daily = [
            {
                "date": "20200601",
                "tmax": 30.0,
                "tmin": 20.0,
                "prcp": 0.0,
                "snow": None,
                "snwd": None,
            },
        ]
        summaries = compute_yearly_summaries(daily, "USW00094728", "NY")
        # temp_mean = (30 + 20) / 2 = 25.0
        assert summaries[0]["temp_mean"] == 25.0


# ---------------------------------------------------------------------------
# ghcn_utils — simple_linear_regression
# ---------------------------------------------------------------------------


class TestSimpleLinearRegression:
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

    def test_empty(self):
        slope, intercept = simple_linear_regression([], [])
        assert slope == 0.0
        assert intercept == 0.0

    def test_negative_slope(self):
        slope, intercept = simple_linear_regression([0, 1, 2], [10, 8, 6])
        assert abs(slope - (-2.0)) < 1e-9

    def test_warming_trend(self):
        # Simulate 0.2 C/year warming
        xs = [float(y) for y in range(2000, 2020)]
        ys = [10.0 + 0.2 * (y - 2000) for y in range(2000, 2020)]
        slope, _ = simple_linear_regression(xs, ys)
        assert abs(slope - 0.2) < 1e-6


# ---------------------------------------------------------------------------
# ghcn_utils — reverse_geocode_nominatim (mock fallback)
# ---------------------------------------------------------------------------


class TestReverseGeocodeNominatim:
    """The on-disk cache lives at ``$AFL_DATA_ROOT/cache/noaa-weather/geocode/``
    with a per-entry ``.meta.json`` sidecar (see
    ``agent-spec/cache-layout.agent-spec.yaml``). Tests point
    ``AFL_DATA_ROOT`` at a per-test tmp dir and pass ``use_mock=True``
    to force the deterministic offline code path — no network, no
    dependency on ``requests`` being installed.
    """

    def test_mock_fallback(self, tmp_path, monkeypatch):
        """Offline mode returns a populated hash-based result."""
        from noaa_weather.handlers.shared.ghcn_utils import reverse_geocode_nominatim as _rgn

        monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
        result = _rgn(40.78, -73.97, use_mock=True)
        assert "display_name" in result
        assert "city" in result
        assert "state" in result
        assert "country" in result
        assert result["country"]  # non-empty

    def test_deterministic(self, tmp_path, monkeypatch):
        """Same coordinates always produce same mock result."""
        from noaa_weather.handlers.shared.ghcn_utils import reverse_geocode_nominatim as _rgn

        monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
        r1 = _rgn(40.78, -73.97, use_mock=True)
        r2 = _rgn(40.78, -73.97, use_mock=True)
        assert r1 == r2

    def test_cache_hit(self, tmp_path, monkeypatch):
        """A second call for the same coords reads from the sidecar cache
        without a new live lookup — verified by leaving ``use_mock=True``
        off for the second call: if it tried to go live it would fail
        without network, but the cache short-circuits it."""
        from noaa_weather.handlers.shared.ghcn_utils import reverse_geocode_nominatim as _rgn

        monkeypatch.setenv("AFL_DATA_ROOT", str(tmp_path))
        # First call writes the cache in mock mode.
        r1 = _rgn(12.3456, 78.9012, use_mock=True)
        # Second call without use_mock — must read the cached sidecar,
        # not attempt network.
        r2 = _rgn(12.3456, 78.9012)
        assert r1 == r2

        # And the cached artifact must be where the layout says it is.
        cache_file = (
            tmp_path
            / "cache"
            / "noaa-weather"
            / "geocode"
            / "12.3456_78.9012.json"
        )
        assert cache_file.exists(), f"expected cached artifact at {cache_file}"
        assert cache_file.with_suffix(".json.meta.json").exists(), (
            "expected sibling .meta.json sidecar"
        )


# ---------------------------------------------------------------------------
# Catalog handlers
# ---------------------------------------------------------------------------


class TestCatalogHandlers:
    def test_discover_stations(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_station_catalog",
            lambda: SAMPLE_STATIONS_TEXT,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_inventory",
            lambda: SAMPLE_INVENTORY_TEXT,
        )

        from noaa_weather.handlers.catalog.catalog_handlers import handle_discover_stations

        result = handle_discover_stations(
            {
                "country": "US",
                "state": "NY",
                "max_stations": 10,
                "min_years": 20,
            }
        )
        assert "stations" in result
        assert "station_count" in result
        assert result["station_count"] == len(result["stations"])
        assert result["station_count"] >= 1

    def test_discover_stations_json_elements(self, monkeypatch):
        """required_elements as JSON string is handled."""
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_station_catalog",
            lambda: SAMPLE_STATIONS_TEXT,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_inventory",
            lambda: SAMPLE_INVENTORY_TEXT,
        )

        from noaa_weather.handlers.catalog.catalog_handlers import handle_discover_stations

        result = handle_discover_stations(
            {
                "country": "US",
                "max_stations": 5,
                "min_years": 10,
                "required_elements": '["TMAX", "TMIN"]',
            }
        )
        assert result["station_count"] >= 1

    def test_discover_stations_no_match(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_station_catalog",
            lambda: SAMPLE_STATIONS_TEXT,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_inventory",
            lambda: SAMPLE_INVENTORY_TEXT,
        )

        from noaa_weather.handlers.catalog.catalog_handlers import handle_discover_stations

        result = handle_discover_stations(
            {
                "country": "JP",  # No Japanese stations in sample
                "max_stations": 10,
                "min_years": 20,
            }
        )
        assert result["station_count"] == 0
        assert result["stations"] == []

    def test_handle_dispatch(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_station_catalog",
            lambda: SAMPLE_STATIONS_TEXT,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.catalog.catalog_handlers.download_inventory",
            lambda: SAMPLE_INVENTORY_TEXT,
        )

        from noaa_weather.handlers.catalog.catalog_handlers import handle

        result = handle(
            {
                "_facet_name": "weather.Catalog.DiscoverStations",
                "country": "US",
                "state": "NY",
                "max_stations": 5,
                "min_years": 20,
            }
        )
        assert result["station_count"] >= 1

    def test_register_handlers(self):
        from noaa_weather.handlers.catalog.catalog_handlers import register_handlers

        class FakeRunner:
            def __init__(self):
                self.registered = []

            def register_handler(self, **kwargs):
                self.registered.append(kwargs)

        runner = FakeRunner()
        register_handlers(runner)
        assert len(runner.registered) == 1
        assert runner.registered[0]["facet_name"] == "weather.Catalog.DiscoverStations"

    def test_register_catalog_handlers(self):
        from noaa_weather.handlers.catalog.catalog_handlers import register_catalog_handlers

        class FakePoller:
            def __init__(self):
                self.registered = {}

            def register(self, name, handler):
                self.registered[name] = handler

        poller = FakePoller()
        register_catalog_handlers(poller)
        assert "weather.Catalog.DiscoverStations" in poller.registered


# ---------------------------------------------------------------------------
# Ingest handlers
# ---------------------------------------------------------------------------


class TestIngestHandlers:
    def test_fetch_station_data(self, tmp_path, monkeypatch):
        rows = [
            ("20200101", "TMAX", "250", ""),
            ("20200101", "TMIN", "100", ""),
            ("20200101", "PRCP", "50", ""),
            ("20210615", "TMAX", "310", ""),
            ("20210615", "TMIN", "200", ""),
        ]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.ingest.ingest_handlers.download_station_csv",
            lambda station_id: csv_path,
        )

        from noaa_weather.handlers.ingest.ingest_handlers import handle_fetch_station_data

        result = handle_fetch_station_data(
            {
                "station_id": "USW00094728",
                "start_year": 2020,
                "end_year": 2021,
            }
        )
        assert result["station_id"] == "USW00094728"
        assert result["record_count"] == 2  # 2 unique dates
        assert result["years_with_data"] == 2

    def test_fetch_empty_range(self, tmp_path, monkeypatch):
        rows = [("20200101", "TMAX", "250", "")]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.ingest.ingest_handlers.download_station_csv",
            lambda station_id: csv_path,
        )

        from noaa_weather.handlers.ingest.ingest_handlers import handle_fetch_station_data

        result = handle_fetch_station_data(
            {
                "station_id": "USW00094728",
                "start_year": 2025,
                "end_year": 2025,
            }
        )
        assert result["record_count"] == 0
        assert result["years_with_data"] == 0

    def test_handle_dispatch(self, tmp_path, monkeypatch):
        rows = [("20200101", "TMAX", "250", "")]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.ingest.ingest_handlers.download_station_csv",
            lambda station_id: csv_path,
        )

        from noaa_weather.handlers.ingest.ingest_handlers import handle

        result = handle(
            {
                "_facet_name": "weather.Ingest.FetchStationData",
                "station_id": "USW00094728",
                "start_year": 2020,
                "end_year": 2020,
            }
        )
        assert result["record_count"] == 1

    def test_register_handlers(self):
        from noaa_weather.handlers.ingest.ingest_handlers import register_handlers

        class FakeRunner:
            def __init__(self):
                self.registered = []

            def register_handler(self, **kwargs):
                self.registered.append(kwargs)

        runner = FakeRunner()
        register_handlers(runner)
        assert len(runner.registered) == 1
        assert runner.registered[0]["facet_name"] == "weather.Ingest.FetchStationData"

    def test_register_ingest_handlers(self):
        from noaa_weather.handlers.ingest.ingest_handlers import register_ingest_handlers

        class FakePoller:
            def __init__(self):
                self.registered = {}

            def register(self, name, handler):
                self.registered[name] = handler

        poller = FakePoller()
        register_ingest_handlers(poller)
        assert "weather.Ingest.FetchStationData" in poller.registered


# ---------------------------------------------------------------------------
# Analysis handlers
# ---------------------------------------------------------------------------


class TestAnalysisHandlers:
    def test_analyze_station_climate(self, tmp_path, monkeypatch):
        rows = [
            ("20200101", "TMAX", "50", ""),
            ("20200101", "TMIN", "-30", ""),
            ("20200101", "PRCP", "20", ""),
            ("20200715", "TMAX", "350", ""),
            ("20200715", "TMIN", "220", ""),
            ("20200715", "PRCP", "0", ""),
        ]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.download_station_csv",
            lambda station_id: csv_path,
        )
        # Mock get_weather_db to avoid MongoDB
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: _FakeDb(),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle_analyze_station_climate

        result = handle_analyze_station_climate(
            {
                "station_id": "USW00094728",
                "station_name": "CENTRAL PARK",
                "lat": 40.78,
                "lon": -73.97,
                "start_year": 2020,
                "end_year": 2020,
                "state": "NY",
            }
        )
        assert result["station_id"] == "USW00094728"
        assert result["years_analyzed"] == 1
        summaries = json.loads(result["yearly_summaries"])
        assert len(summaries) == 1
        assert summaries[0]["year"] == 2020
        assert summaries[0]["state"] == "NY"

    def test_analyze_station_no_data(self, tmp_path, monkeypatch):
        # Empty CSV
        path = str(tmp_path / "EMPTY.csv")
        with open(path, "w") as f:
            f.write("")
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.download_station_csv",
            lambda station_id: path,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: _FakeDb(),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle_analyze_station_climate

        result = handle_analyze_station_climate(
            {
                "station_id": "USW00099999",
                "station_name": "NOWHERE",
                "start_year": 2020,
                "end_year": 2020,
                "state": "XX",
            }
        )
        assert result["years_analyzed"] == 0
        assert json.loads(result["yearly_summaries"]) == []

    def test_analyze_station_db_error_raises(self, tmp_path, monkeypatch):
        """MongoDB write failure propagates so the task fails visibly."""
        rows = [
            ("20200101", "TMAX", "250", ""),
            ("20200101", "TMIN", "100", ""),
        ]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.download_station_csv",
            lambda station_id: csv_path,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: (_ for _ in ()).throw(RuntimeError("no mongo")),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle_analyze_station_climate

        with pytest.raises(RuntimeError, match="no mongo"):
            handle_analyze_station_climate(
                {
                    "station_id": "USW00094728",
                    "station_name": "TEST",
                    "start_year": 2020,
                    "end_year": 2020,
                    "state": "NY",
                }
            )

    def test_compute_region_trend_no_db(self, monkeypatch):
        """When MongoDB is unavailable, returns empty trend."""
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: (_ for _ in ()).throw(RuntimeError("no mongo")),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle_compute_region_trend

        result = handle_compute_region_trend(
            {
                "country": "US",
                "state": "NY",
                "start_year": 2000,
                "end_year": 2020,
            }
        )
        trend = json.loads(result["trend"])
        assert trend["warming_rate_per_decade"] == 0.0
        assert "No data" in result["narrative"]

    def test_handle_dispatch_analyze(self, tmp_path, monkeypatch):
        rows = [("20200101", "TMAX", "250", "")]
        csv_path = _make_csv_file(tmp_path, "USW00094728", rows)
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.download_station_csv",
            lambda station_id: csv_path,
        )
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: _FakeDb(),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle

        result = handle(
            {
                "_facet_name": "weather.Analysis.AnalyzeStationClimate",
                "station_id": "USW00094728",
                "station_name": "TEST",
                "start_year": 2020,
                "end_year": 2020,
                "state": "NY",
            }
        )
        assert "yearly_summaries" in result

    def test_handle_dispatch_trend(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: (_ for _ in ()).throw(RuntimeError("no mongo")),
        )

        from noaa_weather.handlers.analysis.analysis_handlers import handle

        result = handle(
            {
                "_facet_name": "weather.Analysis.ComputeRegionTrend",
                "country": "US",
                "state": "CA",
                "start_year": 2000,
                "end_year": 2020,
            }
        )
        assert "trend" in result
        assert "narrative" in result

    def test_register_handlers(self):
        from noaa_weather.handlers.analysis.analysis_handlers import register_handlers

        class FakeRunner:
            def __init__(self):
                self.registered = []

            def register_handler(self, **kwargs):
                self.registered.append(kwargs)

        runner = FakeRunner()
        register_handlers(runner)
        assert len(runner.registered) == 3
        names = {r["facet_name"] for r in runner.registered}
        assert "weather.Analysis.AnalyzeStationClimate" in names
        assert "weather.Analysis.AnalyzeStationMonthly" in names
        assert "weather.Analysis.ComputeRegionTrend" in names

    def test_register_analysis_handlers(self):
        from noaa_weather.handlers.analysis.analysis_handlers import register_analysis_handlers

        class FakePoller:
            def __init__(self):
                self.registered = {}

            def register(self, name, handler):
                self.registered[name] = handler

        poller = FakePoller()
        register_analysis_handlers(poller)
        assert "weather.Analysis.AnalyzeStationClimate" in poller.registered
        assert "weather.Analysis.ComputeRegionTrend" in poller.registered


@pytest.mark.skipif(not HAS_MONGOMOCK, reason="mongomock not installed")
class TestComputeRegionTrendWithDb:
    """Tests for ComputeRegionTrend with a real (mock) database."""

    @pytest.fixture(autouse=True)
    def _setup_db(self, monkeypatch):
        self.mock_client = mongomock.MongoClient()
        self.db = self.mock_client["test_ghcn_trend"]
        monkeypatch.setattr(
            "noaa_weather.handlers.analysis.analysis_handlers.get_weather_db",
            lambda: self.db,
        )

    def test_trend_with_reports(self):
        from noaa_weather.handlers.analysis.analysis_handlers import handle_compute_region_trend

        # Seed reports with increasing temperatures
        for year in range(2010, 2020):
            self.db["weather_reports"].insert_one(
                {
                    "station_id": "USW00094728",
                    "year": year,
                    "location": "NY",
                    "report": {
                        "temp_mean": 10.0 + (year - 2010) * 0.2,
                        "precip_annual": 1000.0 + (year - 2010) * 10,
                        "hot_days": 5,
                        "frost_days": 80,
                    },
                }
            )

        result = handle_compute_region_trend(
            {
                "country": "US",
                "state": "NY",
                "start_year": 2010,
                "end_year": 2019,
            }
        )
        trend = json.loads(result["trend"])
        assert trend["warming_rate_per_decade"] > 0
        assert "warmed" in result["narrative"]
        assert trend["state"] == "NY"
        assert "2010s" in trend["decades"]

    def test_trend_empty_location(self):
        from noaa_weather.handlers.analysis.analysis_handlers import handle_compute_region_trend

        result = handle_compute_region_trend(
            {
                "country": "US",
                "state": "ZZ",
                "start_year": 2000,
                "end_year": 2020,
            }
        )
        trend = json.loads(result["trend"])
        assert trend["warming_rate_per_decade"] == 0.0

    def test_trend_writes_climate_collections(self):
        from noaa_weather.handlers.analysis.analysis_handlers import handle_compute_region_trend

        for year in range(2015, 2020):
            self.db["weather_reports"].insert_one(
                {
                    "station_id": "USW00094728",
                    "year": year,
                    "location": "CA",
                    "report": {
                        "temp_mean": 18.0 + (year - 2015) * 0.1,
                        "precip_annual": 400.0,
                        "hot_days": 20,
                        "frost_days": 10,
                    },
                }
            )

        handle_compute_region_trend(
            {
                "country": "US",
                "state": "CA",
                "start_year": 2015,
                "end_year": 2019,
            }
        )

        # Verify climate_state_years was written
        state_years = list(self.db["climate_state_years"].find({"state": "CA"}))
        assert len(state_years) == 5

        # Verify climate_trends was written
        trend_doc = self.db["climate_trends"].find_one({"state": "CA"})
        assert trend_doc is not None
        assert "narrative" in trend_doc


# ---------------------------------------------------------------------------
# Geocode handlers
# ---------------------------------------------------------------------------


class TestGeocodeHandlers:
    def test_reverse_geocode(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.geocode.geocode_handlers.reverse_geocode_nominatim",
            lambda lat, lon: {
                "display_name": "Central Park, NY",
                "city": "New York",
                "state": "New York",
                "country": "US",
                "county": "New York County",
            },
        )

        from noaa_weather.handlers.geocode.geocode_handlers import handle_reverse_geocode

        result = handle_reverse_geocode({"lat": 40.78, "lon": -73.97})
        assert "geo" in result
        assert result["geo"]["city"] == "New York"
        assert result["geo"]["country"] == "US"

    def test_handle_dispatch(self, monkeypatch):
        monkeypatch.setattr(
            "noaa_weather.handlers.geocode.geocode_handlers.reverse_geocode_nominatim",
            lambda lat, lon: {
                "display_name": "Test",
                "city": "",
                "state": "",
                "country": "US",
                "county": "",
            },
        )

        from noaa_weather.handlers.geocode.geocode_handlers import handle

        result = handle(
            {
                "_facet_name": "weather.Geocode.ReverseGeocode",
                "lat": 40.78,
                "lon": -73.97,
            }
        )
        assert result["geo"]["display_name"] == "Test"

    def test_register_handlers(self):
        from noaa_weather.handlers.geocode.geocode_handlers import register_handlers

        class FakeRunner:
            def __init__(self):
                self.registered = []

            def register_handler(self, **kwargs):
                self.registered.append(kwargs)

        runner = FakeRunner()
        register_handlers(runner)
        assert len(runner.registered) == 1
        assert runner.registered[0]["facet_name"] == "weather.Geocode.ReverseGeocode"

    def test_register_geocode_handlers(self):
        from noaa_weather.handlers.geocode.geocode_handlers import register_geocode_handlers

        class FakePoller:
            def __init__(self):
                self.registered = {}

            def register(self, name, handler):
                self.registered[name] = handler

        poller = FakePoller()
        register_geocode_handlers(poller)
        assert "weather.Geocode.ReverseGeocode" in poller.registered


# ---------------------------------------------------------------------------
# WeatherReportStore (mongomock)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MONGOMOCK, reason="mongomock not installed")
class TestWeatherReportStore:
    @pytest.fixture()
    def store(self):
        client = mongomock.MongoClient()
        db = client["test_weather"]
        return WeatherReportStore(db)

    def test_upsert_and_get_report(self, store):
        uri = store.upsert_report(
            station_id="USW00094728",
            station_name="CENTRAL PARK",
            year=2020,
            location="NY",
            report={"temp_mean": 12.5, "precip_annual": 1100.0},
            daily_stats=[],
        )
        assert uri == "weather://USW00094728/2020"
        doc = store.get_report("USW00094728", 2020)
        assert doc is not None
        assert doc["station_name"] == "CENTRAL PARK"
        assert doc["report"]["temp_mean"] == 12.5

    def test_upsert_overwrites(self, store):
        store.upsert_report("S1", "Name1", 2020, "NY", {"v": 1}, [])
        store.upsert_report("S1", "Name2", 2020, "NY", {"v": 2}, [])
        doc = store.get_report("S1", 2020)
        assert doc["station_name"] == "Name2"
        assert doc["report"]["v"] == 2

    def test_list_reports(self, store):
        store.upsert_report("S1", "A", 2020, "NY", {}, [])
        store.upsert_report("S2", "B", 2021, "CA", {}, [])
        reports = store.list_reports(limit=10)
        assert len(reports) == 2

    def test_get_report_missing(self, store):
        assert store.get_report("NONEXISTENT", 9999) is None

    def test_upsert_html(self, store):
        store.upsert_report("S1", "A", 2020, "NY", {}, [])
        uri = store.upsert_html("S1", 2020, "<h1>Report</h1>")
        assert uri == "weather://S1/2020"
        doc = store.get_report("S1", 2020)
        assert doc["html_content"] == "<h1>Report</h1>"

    def test_upsert_map(self, store):
        store.upsert_report("S1", "A", 2020, "NY", {}, [])
        uri = store.upsert_map("S1", 2020, "<div>Map</div>")
        assert uri == "weather://S1/2020"
        doc = store.get_report("S1", 2020)
        assert doc["map_content"] == "<div>Map</div>"

    def test_upsert_batch(self, store):
        uri = store.upsert_batch("batch-1", 5, 4, 1, [{"id": "S1"}], "Done")
        assert uri == "weather://batch/batch-1"


@pytest.mark.skipif(not HAS_MONGOMOCK, reason="mongomock not installed")
class TestClimateStoreGhcn:
    @pytest.fixture()
    def store(self):
        client = mongomock.MongoClient()
        db = client["test_climate_ghcn"]
        return ClimateStore(db)

    def test_upsert_and_get_state_year(self, store):
        store.upsert_state_year({"state": "NY", "year": 2020, "temp_mean": 12.5})
        results = store.get_state_years("NY")
        assert len(results) == 1
        assert results[0]["temp_mean"] == 12.5

    def test_upsert_and_get_trend(self, store):
        store.upsert_trend({"state": "NY", "warming_rate_per_decade": 0.15})
        result = store.get_trend("NY")
        assert result is not None
        assert result["warming_rate_per_decade"] == 0.15

    def test_list_states(self, store):
        store.upsert_trend({"state": "NY", "warming_rate_per_decade": 0.1})
        store.upsert_trend({"state": "CA", "warming_rate_per_decade": 0.2})
        assert store.list_states() == ["CA", "NY"]

    def test_get_narrative(self, store):
        store.upsert_trend({"state": "FL", "narrative": "Hot."})
        assert store.get_narrative("FL") == "Hot."

    def test_get_narrative_missing(self, store):
        assert store.get_narrative("ZZ") is None


# ---------------------------------------------------------------------------
# Fake DB for non-mongomock tests
# ---------------------------------------------------------------------------


class _FakeCollection:
    """Minimal collection mock that silently accepts writes."""

    def __init__(self):
        self._docs: list[dict] = []

    def create_index(self, *args, **kwargs):
        pass

    def update_one(self, filter_doc, update_doc, upsert=False):
        pass

    def find_one(self, *args, **kwargs):
        return None

    def find(self, *args, **kwargs):
        return _FakeCursor([])

    def insert_one(self, doc):
        self._docs.append(doc)

    def distinct(self, field):
        return []


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    def limit(self, n):
        return self._docs[:n]

    def __iter__(self):
        return iter(self._docs)

    def __list__(self):
        return self._docs


class _FakeDb:
    """Minimal DB mock that returns fake collections."""

    def __init__(self):
        self._collections: dict[str, _FakeCollection] = {}

    def __getitem__(self, name):
        if name not in self._collections:
            self._collections[name] = _FakeCollection()
        return self._collections[name]
