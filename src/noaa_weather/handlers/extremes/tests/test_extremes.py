"""Offline tests for extreme-event detection (pure lib + handler seam mocked)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from noaa_weather.handlers.extremes import extremes_handlers as eh
from noaa_weather.tools._noaa_tools.extremes import (
    ExtremeConfig,
    detect_events,
)


def _seq(start: str, values: list[dict]) -> list[dict]:
    """Build consecutive daily records from a per-day override list."""
    d0 = date.fromisoformat(start)
    out = []
    for i, v in enumerate(values):
        rec = {"date": (d0 + timedelta(days=i)).isoformat(),
               "tmax": None, "tmin": None, "prcp": None, "snow": None, "snwd": None}
        rec.update(v)
        out.append(rec)
    return out


# ---- pure detection -------------------------------------------------------

def test_heat_wave_run_and_peak():
    out = detect_events(_seq("2001-06-01", [{"tmax": 36.0}] * 4))
    assert out["counts_by_type"].get("heat_wave") == 1
    hw = next(e for e in out["events"] if e["type"] == "heat_wave")
    assert hw["duration_days"] == 4 and hw["peak_value"] == 36.0
    assert hw["start_date"] == "2001-06-01" and hw["year"] == 2001


def test_gap_and_min_days_break_the_run():
    # two 2-day hot stretches with a gap -> neither reaches the 3-day minimum
    daily = _seq("2001-06-01", [{"tmax": 36.0}] * 2) + _seq("2001-06-05", [{"tmax": 36.0}] * 2)
    out = detect_events(daily)
    assert out["counts_by_type"].get("heat_wave", 0) == 0


def test_cold_snap():
    out = detect_events(_seq("2002-01-10", [{"tmin": -12.0}] * 3))
    cs = next(e for e in out["events"] if e["type"] == "cold_snap")
    assert cs["duration_days"] == 3 and cs["peak_value"] == -12.0


def test_dry_and_wet_spells():
    dry = detect_events(_seq("2002-01-01", [{"prcp": 0.0}] * 21))
    assert dry["counts_by_type"]["dry_spell"] == 1
    assert next(e for e in dry["events"] if e["type"] == "dry_spell")["duration_days"] == 21
    wet = detect_events(_seq("2003-04-01", [{"prcp": 5.0}] * 5))
    we = next(e for e in wet["events"] if e["type"] == "wet_spell")
    assert we["duration_days"] == 5 and we["peak_value"] == 25.0  # total mm


def test_heavy_rain_and_snow_are_per_day():
    out = detect_events(_seq("2004-07-01", [{"prcp": 60.0}, {"snow": 120.0}]))
    assert out["counts_by_type"]["heavy_rain"] == 1
    assert out["counts_by_type"]["heavy_snow"] == 1
    hr = next(e for e in out["events"] if e["type"] == "heavy_rain")
    assert hr["duration_days"] == 1 and hr["peak_value"] == 60.0


def test_missing_values_dont_count():
    # None prcp must not register as a dry day
    out = detect_events(_seq("2005-01-01", [{"prcp": None}] * 30))
    assert out["event_count"] == 0


def test_decadal_frequency_buckets_by_decade():
    daily = _seq("1995-06-01", [{"tmax": 36.0}] * 3) + _seq("2005-06-01", [{"tmax": 36.0}] * 3)
    out = detect_events(daily)
    assert out["decadal_frequency"]["heat_wave"] == {"1990s": 1, "2000s": 1}


def test_config_from_params_coerces_and_ignores_blanks():
    cfg = ExtremeConfig.from_params({"heat_wave_tmax_c": "38", "heat_wave_min_days": 2,
                                     "cold_snap_tmin_c": None, "heavy_rain_mm": ""})
    assert cfg.heat_wave_tmax_c == 38.0 and cfg.heat_wave_min_days == 2
    assert cfg.cold_snap_tmin_c == -10.0 and cfg.heavy_rain_mm == 50.0  # defaults kept


def test_custom_threshold_changes_detection():
    daily = _seq("2006-06-01", [{"tmax": 33.0}] * 3)
    assert detect_events(daily)["event_count"] == 0                  # default 35 -> none
    out = detect_events(daily, ExtremeConfig(heat_wave_tmax_c=32.0))  # lower bar -> 1
    assert out["counts_by_type"]["heat_wave"] == 1


# ---- handler (download/parse seam mocked) ---------------------------------

_DAILY = (_seq("2010-06-01", [{"tmax": 37.0}] * 4)
          + _seq("2010-08-01", [{"prcp": 70.0}]))


def test_handler_returns_changeset_and_summary(monkeypatch):
    monkeypatch.setattr(eh, "download_station_csv", lambda sid, **k: "/tmp/x.csv")
    monkeypatch.setattr(eh, "parse_ghcn_csv", lambda path, s, e: list(_DAILY))
    out = eh.handle_detect_station_extremes({
        "station_id": "USW00094728", "station_name": "NYC", "start_year": 2010, "end_year": 2011,
    })
    assert out["station_id"] == "USW00094728"
    assert out["event_count"] == 2
    counts = json.loads(out["counts_by_type"])
    assert counts["heat_wave"] == 1 and counts["heavy_rain"] == 1
    events = json.loads(out["events"])
    assert {e["type"] for e in events} == {"heat_wave", "heavy_rain"}
    assert "NYC" in out["summary"]


def test_handler_thresholds_flow_through(monkeypatch):
    monkeypatch.setattr(eh, "download_station_csv", lambda sid, **k: "/tmp/x.csv")
    monkeypatch.setattr(eh, "parse_ghcn_csv", lambda path, s, e:
                        _seq("2010-06-01", [{"tmax": 33.0}] * 3))
    # default 35°C -> nothing; a human-supplied lower bar -> a heat wave
    base = eh.handle_detect_station_extremes({"station_id": "X"})
    assert base["event_count"] == 0
    tuned = eh.handle_detect_station_extremes({"station_id": "X", "heat_wave_tmax_c": 32})
    assert json.loads(tuned["counts_by_type"])["heat_wave"] == 1


def test_handler_empty_data(monkeypatch):
    monkeypatch.setattr(eh, "download_station_csv", lambda sid, **k: "/tmp/x.csv")
    monkeypatch.setattr(eh, "parse_ghcn_csv", lambda path, s, e: [])
    out = eh.handle_detect_station_extremes({"station_id": "X", "start_year": 2000, "end_year": 2001})
    assert out["event_count"] == 0 and "No data" in out["summary"]


def test_handler_requires_station_id():
    with pytest.raises(ValueError):
        eh.handle_detect_station_extremes({})


def test_dispatch_and_registration():
    from unittest.mock import MagicMock
    assert set(eh._DISPATCH) == {
        "weather.Extremes.DetectStationExtremes",
        "weather.Extremes.AggregateRegionExtremes",
        "weather.Extremes.RenderExtremesChart",
    }
    runner = MagicMock()
    eh.register_handlers(runner)
    registered = {c.kwargs["facet_name"] for c in runner.register_handler.call_args_list}
    assert registered == set(eh._DISPATCH)


# ---- region-level aggregation --------------------------------------------

from noaa_weather.tools._noaa_tools.extremes import aggregate_region  # noqa: E402


def test_aggregate_region_sums_and_trends():
    a = {"counts_by_type": {"heat_wave": 6, "cold_snap": 6},
         "decadal_frequency": {"heat_wave": {"1990s": 1, "2000s": 2, "2010s": 3},
                               "cold_snap": {"1990s": 3, "2000s": 2, "2010s": 1}}}
    b = {"counts_by_type": {"heat_wave": 3},
         "decadal_frequency": {"heat_wave": {"1990s": 1, "2000s": 1, "2010s": 1}}}
    agg = aggregate_region([a, b], region_label="NY")
    assert agg["station_count"] == 2
    assert agg["counts_by_type"]["heat_wave"] == 9 and agg["counts_by_type"]["cold_snap"] == 6
    assert agg["by_type_decade"]["heat_wave"] == {"1990s": 2, "2000s": 3, "2010s": 4}
    assert agg["trends"]["heat_wave"]["direction"] == "rising"
    assert agg["trends"]["cold_snap"]["direction"] == "falling"
    assert "NY" in agg["narrative"]


def test_aggregate_region_empty():
    agg = aggregate_region([], region_label="ZZ")
    assert agg["station_count"] == 0 and agg["total_events"] == 0


class _FakeStore:
    def __init__(self, docs):
        self._docs = docs
        self.saved = None
    def __call__(self, db):           # used as ExtremeEventStore(get_weather_db())
        return self
    def find_for_region(self, loc):
        return [d for d in self._docs if not loc or d.get("location") == loc]
    def upsert_region(self, loc, agg):
        self.saved = (loc, agg)
    def upsert_station(self, doc):
        self.saved = doc


def test_detect_handler_persists_rollup(monkeypatch):
    store = _FakeStore([])
    monkeypatch.setattr(eh, "get_weather_db", lambda: object())
    monkeypatch.setattr(eh, "ExtremeEventStore", store)
    monkeypatch.setattr(eh, "download_station_csv", lambda sid, **k: "/tmp/x.csv")
    monkeypatch.setattr(eh, "parse_ghcn_csv", lambda p, s, e: list(_DAILY))
    out = eh.handle_detect_station_extremes({"station_id": "X", "state": "NY",
                                             "start_year": 2010, "end_year": 2011})
    assert out["event_count"] == 2
    assert store.saved["station_id"] == "X" and store.saved["location"] == "NY"
    assert store.saved["event_count"] == 2 and "decadal_frequency" in store.saved


def test_aggregate_region_handler_reads_back(monkeypatch):
    docs = [
        {"station_id": "A", "location": "NY", "counts_by_type": {"heat_wave": 6},
         "decadal_frequency": {"heat_wave": {"1990s": 1, "2000s": 2, "2010s": 3}}},
        {"station_id": "B", "location": "NY", "counts_by_type": {"heat_wave": 3},
         "decadal_frequency": {"heat_wave": {"1990s": 1, "2000s": 1, "2010s": 1}}},
    ]
    monkeypatch.setattr(eh, "get_weather_db", lambda: object())
    monkeypatch.setattr(eh, "ExtremeEventStore", _FakeStore(docs))
    out = eh.handle_aggregate_region_extremes({"country": "US", "state": "NY",
                                               "start_year": 1990, "end_year": 2020})
    assert out["station_count"] == 2
    agg = json.loads(out["aggregate"])
    assert agg["counts_by_type"]["heat_wave"] == 9
    assert agg["trends"]["heat_wave"]["direction"] == "rising"
    # top-level wiring fields for the render step
    assert json.loads(out["counts_by_type"])["heat_wave"] == 9
    assert json.loads(out["decadal_frequency"])["heat_wave"] == {"1990s": 2, "2000s": 3, "2010s": 4}
    assert json.loads(out["trends"])["heat_wave"]["direction"] == "rising"


# ---- visualization --------------------------------------------------------

from noaa_weather.tools._noaa_tools import extremes_chart  # noqa: E402


def test_decadal_bars_svg_has_bars_and_legend():
    svg = extremes_chart.decadal_bars_svg(
        {"heat_wave": {"1990s": 1, "2000s": 3}, "cold_snap": {"1990s": 2}},
        title="T", trends={"heat_wave": {"direction": "rising", "per_decade_change": 1.0}})
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<rect") >= 3            # 2 heat-wave + 1 cold-snap bars
    assert "rising" in svg and "1990s" in svg


def test_decadal_bars_svg_empty():
    svg = extremes_chart.decadal_bars_svg({}, title="None")
    assert "<svg" in svg and "no events" in svg


def test_extremes_html_structure():
    html = extremes_chart.extremes_html(
        title="Extremes", label="USW1", svg="<svg></svg>",
        counts_by_type={"heat_wave": 3}, trends={"heat_wave": {"direction": "rising"}},
        summary="hot")
    assert "<h1>Extremes</h1>" in html and "USW1" in html
    assert "heat wave" in html and "<svg>" in html and "hot" in html


def test_render_handler_writes_files(tmp_path, monkeypatch):
    monkeypatch.setattr(eh, "LocalStorage", lambda: object())
    monkeypatch.setattr(eh.sidecar, "cache_path", lambda ns, ct, rel, storage: str(tmp_path))
    monkeypatch.setattr(eh.sidecar, "write_sidecar", lambda *a, **k: None)
    out = eh.handle_render_extremes_chart({
        "title": "NYC extremes", "label": "USW00094728",
        "counts_by_type": '{"heat_wave": 3, "cold_snap": 2}',
        "decadal_frequency": '{"heat_wave": {"1990s": 1, "2000s": 2}, "cold_snap": {"1990s": 2}}',
        "trends": '{"heat_wave": {"direction": "rising", "per_decade_change": 1.0}}',
        "summary": "rising heat",
    })
    assert out["html_path"].endswith("extremes.html") and out["svg_path"].endswith("extremes.svg")
    html = open(out["html_path"]).read()
    assert "NYC extremes" in html and "<rect" in html and "rising" in html


def test_render_handler_coerces_dict_inputs(tmp_path, monkeypatch):
    # FFL may hand the Json params through already-parsed (dict), not as a string
    monkeypatch.setattr(eh, "LocalStorage", lambda: object())
    monkeypatch.setattr(eh.sidecar, "cache_path", lambda ns, ct, rel, storage: str(tmp_path))
    monkeypatch.setattr(eh.sidecar, "write_sidecar", lambda *a, **k: None)
    out = eh.handle_render_extremes_chart({
        "title": "T", "label": "L",
        "counts_by_type": {"heat_wave": 1},
        "decadal_frequency": {"heat_wave": {"2000s": 1}},
    })
    assert open(out["svg_path"]).read().count("<rect") >= 1


def test_aggregate_region_handler_no_db(monkeypatch):
    def _boom():
        raise RuntimeError("no mongo")
    monkeypatch.setattr(eh, "get_weather_db", _boom)
    out = eh.handle_aggregate_region_extremes({"country": "US", "state": "NY",
                                               "start_year": 1990, "end_year": 2020})
    assert out["station_count"] == 0
