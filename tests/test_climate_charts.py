"""The climate-report charts must render as raw SVG with NO matplotlib/numpy,
so GenerateClimateReport runs in the dependency-light runners.
"""

from __future__ import annotations

import builtins
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "tools")
)


def _block_mpl():
    """Make any matplotlib/numpy import raise, to prove they aren't used."""
    real = builtins.__import__

    def guard(name, *a, **k):
        if name.split(".")[0] in ("matplotlib", "numpy"):
            raise ImportError(f"{name} must not be imported by climate_charts")
        return real(name, *a, **k)

    builtins.__import__ = guard
    return real


def _restore(real):
    builtins.__import__ = real


def _sample():
    normals = {m: {"temp_mean": -2 + m * 2.0, "precip_total": 40 + (m % 4) * 15}
               for m in range(1, 13)}
    annual = [{"year": y, "temp_mean": 10 + 0.03 * (y - 1950) + ((y % 3) - 1) * 0.4}
              for y in range(1950, 2021)]
    monthly = [{"year": y, "month": m, "temp_mean": -3 + m * 2 + 0.02 * (y - 1950)}
               for y in range(1950, 2021) for m in range(1, 13)]
    anom = [{"year": y, "anomaly_c": (y - 1985) * 0.02 + ((y % 5) - 2) * 0.2}
            for y in range(1950, 2021)]
    return normals, annual, monthly, anom


def test_all_charts_render_svg_without_matplotlib():
    real = _block_mpl()
    try:
        from _noaa_tools import climate_charts as cc
        normals, annual, monthly, anom = _sample()
        charts = {
            "climograph": cc.climograph(normals, region_label="MN", baseline=(1991, 2020)),
            "annual_trend": cc.annual_trend(annual, region_label="MN", slope_per_decade=0.3),
            "warming_stripes": cc.warming_stripes(annual, region_label="MN"),
            "heatmap": cc.year_month_heatmap(monthly, region_label="MN"),
            "anomaly_bars": cc.anomaly_bars(anom, region_label="MN", baseline=(1991, 2020)),
        }
    finally:
        _restore(real)

    for name, svg in charts.items():
        assert svg.startswith("<svg"), name
        assert svg.rstrip().endswith("</svg>"), name
        assert "MN" in svg, name
    assert "matplotlib" not in sys.modules
    assert "numpy" not in sys.modules


def test_diverging_colormap_endpoints_and_midpoint():
    from _noaa_tools import climate_charts as cc
    assert cc._rdbu_r(0.0) == "#2166ac"   # cool / blue
    assert cc._rdbu_r(1.0) == "#b2182b"   # warm / red
    assert cc._rdbu_r(0.5) == "#f7f7f7"   # neutral / white
    # Clamps out-of-range inputs rather than raising.
    assert cc._rdbu_r(-5.0) == "#2166ac"
    assert cc._rdbu_r(5.0) == "#b2182b"


def test_empty_inputs_raise_cleanly():
    from _noaa_tools import climate_charts as cc
    import pytest
    for fn, kwargs in [
        (cc.warming_stripes, {"region_label": "X"}),
        (cc.annual_trend, {"region_label": "X"}),
        (cc.anomaly_bars, {"region_label": "X", "baseline": (1991, 2020)}),
    ]:
        with pytest.raises(ValueError):
            fn([], **kwargs)
