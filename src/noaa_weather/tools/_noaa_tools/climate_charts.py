"""SVG climate-chart renderers (matplotlib → inline SVG strings).

Each function produces a standalone SVG string that the report
generator embeds in HTML or writes to a file. Matplotlib is imported
lazily inside each function so ``_noaa_tools`` callers that don't need charts
don't pay the import cost (matplotlib pulls in NumPy + PIL).

Charts follow the visualization conventions referenced in the report:

- climograph           → Walter-Lieth temperature line + precipitation bars
- warming_stripes       → Ed Hawkins' colored-stripe year grid
- annual_trend          → yearly mean-temp scatter + OLS trendline
- year_month_heatmap    → grid with year on x, month on y, temp as colour
- anomaly_bars          → per-year deviation from baseline (red above, blue below)

All charts return a string (the SVG document). The renderer configures
a non-interactive backend ("Agg") so this works in headless runners.
"""

from __future__ import annotations

import io
from typing import Any


MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _import_matplotlib():
    """Lazy matplotlib import with a clear install hint on failure."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return matplotlib, plt
    except ImportError as exc:
        raise RuntimeError(
            "matplotlib is required for chart rendering. Install it via "
            "src/noaa_weather/tools/install-tools.sh, or "
            "`.venv/bin/python -m pip install matplotlib`."
        ) from exc


def _fig_to_svg(fig) -> str:
    """Render a Matplotlib figure to an inline-safe SVG string."""
    buf = io.StringIO()
    fig.savefig(buf, format="svg", bbox_inches="tight")
    import matplotlib.pyplot as plt

    plt.close(fig)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Climograph (Walter-Lieth style).
# ---------------------------------------------------------------------------

def climograph(
    normals: dict[int, dict[str, float | None]],
    *,
    region_label: str,
    baseline: tuple[int, int],
) -> str:
    """Monthly temperature line + precipitation bars.

    ``normals`` is the ``{month: {...}}`` shape returned by
    ``climate_analysis.monthly_climate_normals``.
    """
    _, plt = _import_matplotlib()

    months = list(range(1, 13))
    temps = [normals.get(m, {}).get("temp_mean") for m in months]
    precs = [normals.get(m, {}).get("precip_total") for m in months]

    fig, ax_temp = plt.subplots(figsize=(8, 4))
    ax_prec = ax_temp.twinx()

    # Precipitation bars on the right axis.
    bar_values = [p if p is not None else 0.0 for p in precs]
    ax_prec.bar(months, bar_values, color="#4a90d9", alpha=0.6, label="Precip (mm)")
    ax_prec.set_ylabel("Precipitation (mm)", color="#1f5a99")
    ax_prec.tick_params(axis="y", labelcolor="#1f5a99")

    # Temperature line on the left axis.
    ax_temp.plot(
        months,
        [t if t is not None else float("nan") for t in temps],
        color="#d9534f",
        marker="o",
        linewidth=2,
        label="Temp (°C)",
    )
    ax_temp.set_ylabel("Temperature (°C)", color="#b0302b")
    ax_temp.tick_params(axis="y", labelcolor="#b0302b")
    ax_temp.set_xticks(months)
    ax_temp.set_xticklabels(MONTH_ABBR)
    ax_temp.set_title(
        f"Monthly climate normals — {region_label} ({baseline[0]}–{baseline[1]})"
    )
    ax_temp.grid(True, axis="y", alpha=0.3)

    return _fig_to_svg(fig)


# ---------------------------------------------------------------------------
# Warming stripes (Ed Hawkins convention).
# ---------------------------------------------------------------------------

def warming_stripes(
    annual_rows: list[dict[str, Any]],
    *,
    region_label: str,
) -> str:
    """One vertical coloured stripe per year.

    Colours encode annual mean-temp anomaly (relative to the series
    mean). Red = warmer than average, blue = cooler. This mirrors the
    showyourstripes.info convention.
    """
    _, plt = _import_matplotlib()

    rows = sorted(
        (r for r in annual_rows if r.get("temp_mean") is not None),
        key=lambda r: r["year"],
    )
    if not rows:
        raise ValueError("warming_stripes needs at least one row with temp_mean")

    years = [r["year"] for r in rows]
    temps = [r["temp_mean"] for r in rows]
    mean = sum(temps) / len(temps)
    anomalies = [t - mean for t in temps]

    # Symmetric colour range — the hottest deviation sets both extremes.
    vmax = max(abs(a) for a in anomalies) or 1.0

    fig, ax = plt.subplots(figsize=(10, 2))
    import matplotlib.pyplot as plt_mod
    cmap = plt_mod.get_cmap("RdBu_r")

    for x, (y, a) in enumerate(zip(years, anomalies)):
        # Normalise to [0, 1] for the colormap.
        norm = (a + vmax) / (2 * vmax)
        ax.axvspan(x, x + 1, color=cmap(norm))

    ax.set_xlim(0, len(years))
    # Year ticks roughly every decade.
    step = max(1, len(years) // 10)
    tick_positions = list(range(0, len(years), step)) + [len(years) - 1]
    ax.set_xticks([p + 0.5 for p in tick_positions])
    ax.set_xticklabels([years[p] for p in tick_positions], fontsize=9)
    ax.set_yticks([])
    ax.set_title(
        f"Warming stripes — {region_label}  "
        f"(coldest → warmest, anomaly vs series mean {mean:.2f}°C)",
        fontsize=10,
    )

    return _fig_to_svg(fig)


# ---------------------------------------------------------------------------
# Annual trend with OLS line.
# ---------------------------------------------------------------------------

def annual_trend(
    annual_rows: list[dict[str, Any]],
    *,
    region_label: str,
    slope_per_decade: float | None = None,
) -> str:
    """Year-over-year mean temperature scatter with trend line."""
    _, plt = _import_matplotlib()
    from _noaa_tools.climate_analysis import simple_linear_regression

    rows = sorted(
        (r for r in annual_rows if r.get("temp_mean") is not None),
        key=lambda r: r["year"],
    )
    if not rows:
        raise ValueError("annual_trend needs at least one row with temp_mean")

    years = [r["year"] for r in rows]
    temps = [r["temp_mean"] for r in rows]

    xs = [float(y) for y in years]
    slope, intercept = simple_linear_regression(xs, temps)
    fit = [slope * x + intercept for x in xs]
    if slope_per_decade is None:
        slope_per_decade = slope * 10

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.scatter(years, temps, color="#d9534f", s=18, zorder=2, label="Annual mean")
    ax.plot(
        years,
        fit,
        color="#333",
        linewidth=1.5,
        linestyle="--",
        zorder=1,
        label=f"Trend: {slope_per_decade:+.2f} °C/decade",
    )
    ax.set_xlabel("Year")
    ax.set_ylabel("Annual mean temperature (°C)")
    ax.set_title(f"Annual mean temperature — {region_label}")
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=9)

    return _fig_to_svg(fig)


# ---------------------------------------------------------------------------
# Year × month heatmap.
# ---------------------------------------------------------------------------

def year_month_heatmap(
    monthly_rows: list[dict[str, Any]],
    *,
    region_label: str,
    value_field: str = "temp_mean",
    cmap_name: str = "RdBu_r",
    value_units: str = "°C",
) -> str:
    """Year on the x-axis, month on the y-axis, colour = value.

    Common for spotting seasonal shifts (warmer winters, extreme summers).
    """
    _, plt = _import_matplotlib()

    years = sorted({r["year"] for r in monthly_rows if isinstance(r.get("year"), int)})
    if not years:
        raise ValueError("year_month_heatmap needs at least one valid monthly row")

    # Build a 12 × len(years) matrix (rows=month 1..12, cols=years).
    data = [[float("nan")] * len(years) for _ in range(12)]
    year_idx = {y: i for i, y in enumerate(years)}
    for r in monthly_rows:
        y = r.get("year")
        m = r.get("month")
        v = r.get(value_field)
        if isinstance(y, int) and isinstance(m, int) and 1 <= m <= 12 and v is not None:
            data[m - 1][year_idx[y]] = float(v)

    fig, ax = plt.subplots(figsize=(max(8, 0.15 * len(years)), 4.5))
    import numpy as np

    arr = np.array(data)
    im = ax.imshow(
        arr,
        aspect="auto",
        cmap=cmap_name,
        origin="lower",
        extent=(0, len(years), 0, 12),
    )
    cbar = fig.colorbar(im, ax=ax, shrink=0.8)
    cbar.set_label(f"{value_field} ({value_units})")

    ax.set_yticks([i + 0.5 for i in range(12)])
    ax.set_yticklabels(MONTH_ABBR)
    step = max(1, len(years) // 10)
    ticks = list(range(0, len(years), step)) + [len(years) - 1]
    ax.set_xticks([t + 0.5 for t in ticks])
    ax.set_xticklabels([years[t] for t in ticks], fontsize=9)
    ax.set_title(f"Year × month {value_field} — {region_label}")

    return _fig_to_svg(fig)


# ---------------------------------------------------------------------------
# Anomaly bar chart.
# ---------------------------------------------------------------------------

def anomaly_bars(
    anomaly_rows: list[dict[str, Any]],
    *,
    region_label: str,
    baseline: tuple[int, int],
) -> str:
    """Per-year anomaly bars — red above baseline, blue below.

    ``anomaly_rows`` is the output of
    ``climate_analysis.annual_anomalies``.
    """
    _, plt = _import_matplotlib()

    rows = [r for r in anomaly_rows if r.get("anomaly_c") is not None]
    if not rows:
        raise ValueError("anomaly_bars needs at least one row with anomaly_c")

    rows.sort(key=lambda r: r["year"])
    years = [r["year"] for r in rows]
    anoms = [r["anomaly_c"] for r in rows]
    colors = ["#d9534f" if a >= 0 else "#4a90d9" for a in anoms]

    fig, ax = plt.subplots(figsize=(9, 3.5))
    ax.bar(years, anoms, color=colors, width=0.9)
    ax.axhline(0, color="#333", linewidth=0.8)
    ax.set_xlabel("Year")
    ax.set_ylabel(f"Temperature anomaly vs. {baseline[0]}–{baseline[1]} (°C)")
    ax.set_title(f"Annual temperature anomaly — {region_label}")
    ax.grid(True, axis="y", alpha=0.3)

    return _fig_to_svg(fig)
