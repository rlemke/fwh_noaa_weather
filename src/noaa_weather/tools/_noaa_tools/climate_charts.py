"""Climate-chart renderers — dependency-free inline SVG strings.

Each function produces a standalone ``<svg>…</svg>`` string the report generator
embeds in HTML or writes to a file. **No matplotlib** (and no NumPy/PIL): the
charts are emitted as raw SVG, exactly like ``extremes_chart`` / ``qc_chart`` —
so ``GenerateClimateReport`` runs in the headless runners with no extra deps.

Charts follow the visualization conventions referenced in the report:

- climograph           → Walter-Lieth temperature line + precipitation bars
- warming_stripes       → Ed Hawkins' colored-stripe year grid
- annual_trend          → yearly mean-temp points + OLS trendline
- year_month_heatmap    → grid with year on x, month on y, temp as colour
- anomaly_bars          → per-year deviation from baseline (red above, blue below)

All charts return a string (the SVG document).
"""

from __future__ import annotations

from typing import Any

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# Diverging blue→white→red scale (matplotlib's "RdBu_r"): cool=blue, warm=red.
_RDBU_R_STOPS = [
    (0.00, (33, 102, 172)),    # #2166ac deep blue
    (0.25, (103, 169, 207)),   # #67a9cf
    (0.50, (247, 247, 247)),   # #f7f7f7 white
    (0.75, (239, 138, 98)),    # #ef8a62
    (1.00, (178, 24, 43)),     # #b2182b deep red
]


def _esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _rdbu_r(t: float) -> str:
    """Map t in [0,1] to an #rrggbb on the blue→white→red diverging scale."""
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    for (t0, c0), (t1, c1) in zip(_RDBU_R_STOPS, _RDBU_R_STOPS[1:]):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            r = round(c0[0] + (c1[0] - c0[0]) * f)
            g = round(c0[1] + (c1[1] - c0[1]) * f)
            b = round(c0[2] + (c1[2] - c0[2]) * f)
            return f"#{r:02x}{g:02x}{b:02x}"
    return "#b2182b"


def _svg_open(width: int, height: int, title: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="sans-serif">',
        f'<text x="{width / 2:.0f}" y="22" text-anchor="middle" font-size="15" '
        f'font-weight="bold">{_esc(title)}</text>',
    ]


def _empty_svg(width: int, height: int, msg: str) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'font-family="sans-serif"><text x="{width // 2}" y="{height // 2}" '
            f'text-anchor="middle" font-size="13" fill="#777">{_esc(msg)}</text></svg>')


def _yticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        hi = lo + 1.0
    return [lo + (hi - lo) * i / n for i in range(n + 1)]


# ---------------------------------------------------------------------------
# Climograph (Walter-Lieth style): temp line (left axis) + precip bars (right).
# ---------------------------------------------------------------------------

def climograph(
    normals: dict[int, dict[str, float | None]],
    *,
    region_label: str,
    baseline: tuple[int, int],
) -> str:
    """Monthly temperature line + precipitation bars (dual y-axis)."""
    months = list(range(1, 13))
    temps = [normals.get(m, {}).get("temp_mean") for m in months]
    precs = [normals.get(m, {}).get("precip_total") for m in months]

    W, H = 760, 400
    pad_l, pad_r, pad_t, pad_b = 56, 60, 46, 40
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    title = f"Monthly climate normals — {region_label} ({baseline[0]}–{baseline[1]})"
    p = _svg_open(W, H, title)

    tvals = [t for t in temps if t is not None]
    pmax = max([x for x in precs if x is not None] or [1.0]) or 1.0
    tlo, thi = (min(tvals), max(tvals)) if tvals else (0.0, 1.0)
    if thi == tlo:
        thi = tlo + 1.0

    def px(m: int) -> float:  # month center x
        return pad_l + (m - 0.5) * (plot_w / 12)

    def py_t(v: float) -> float:  # temp value -> y (left axis)
        return pad_t + plot_h - (v - tlo) / (thi - tlo) * plot_h

    def py_p(v: float) -> float:  # precip value -> y (right axis)
        return pad_t + plot_h - (v / pmax) * plot_h

    # Left (temp) gridlines + ticks.
    for v in _yticks(tlo, thi):
        y = py_t(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                 f'stroke="#eee"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#b0302b">{v:.0f}</text>')
    # Right (precip) ticks.
    for v in _yticks(0, pmax):
        y = py_p(v)
        p.append(f'<text x="{pad_l + plot_w + 6}" y="{y + 4:.1f}" font-size="10" '
                 f'fill="#1f5a99">{v:.0f}</text>')

    # Precip bars.
    bw = plot_w / 12 * 0.6
    for m, v in zip(months, precs):
        if v is None:
            continue
        y = py_p(v)
        p.append(f'<rect x="{px(m) - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" '
                 f'height="{pad_t + plot_h - y:.1f}" fill="#4a90d9" opacity="0.6">'
                 f'<title>{MONTH_ABBR[m - 1]}: {v:.0f} mm</title></rect>')

    # Temp line (connect consecutive non-None) + markers.
    prev = None
    for m, v in zip(months, temps):
        if v is None:
            prev = None
            continue
        x, y = px(m), py_t(v)
        if prev is not None:
            p.append(f'<line x1="{prev[0]:.1f}" y1="{prev[1]:.1f}" x2="{x:.1f}" '
                     f'y2="{y:.1f}" stroke="#d9534f" stroke-width="2"/>')
        p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#d9534f">'
                 f'<title>{MONTH_ABBR[m - 1]}: {v:.1f}°C</title></circle>')
        prev = (x, y)

    # Month labels + axis captions.
    for m in months:
        p.append(f'<text x="{px(m):.1f}" y="{pad_t + plot_h + 16:.0f}" text-anchor="middle" '
                 f'font-size="10" fill="#333">{MONTH_ABBR[m - 1]}</text>')
    p.append(f'<text x="14" y="{pad_t + plot_h / 2:.0f}" font-size="10" fill="#b0302b" '
             f'transform="rotate(-90 14 {pad_t + plot_h / 2:.0f})" text-anchor="middle">'
             f'Temperature (°C)</text>')
    p.append(f'<text x="{W - 12}" y="{pad_t + plot_h / 2:.0f}" font-size="10" fill="#1f5a99" '
             f'transform="rotate(-90 {W - 12} {pad_t + plot_h / 2:.0f})" text-anchor="middle">'
             f'Precipitation (mm)</text>')
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------------------
# Warming stripes (Ed Hawkins convention).
# ---------------------------------------------------------------------------

def warming_stripes(annual_rows: list[dict[str, Any]], *, region_label: str) -> str:
    """One vertical coloured stripe per year (red=warm / blue=cool vs series mean)."""
    rows = sorted((r for r in annual_rows if r.get("temp_mean") is not None),
                  key=lambda r: r["year"])
    if not rows:
        raise ValueError("warming_stripes needs at least one row with temp_mean")

    years = [r["year"] for r in rows]
    temps = [r["temp_mean"] for r in rows]
    mean = sum(temps) / len(temps)
    anomalies = [t - mean for t in temps]
    vmax = max(abs(a) for a in anomalies) or 1.0

    W, H = 1000, 170
    pad_l, pad_r, pad_t, pad_b = 8, 8, 40, 26
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    title = (f"Warming stripes — {region_label} "
             f"(coldest → warmest, anomaly vs series mean {mean:.2f}°C)")
    p = _svg_open(W, H, title)

    sw = plot_w / len(years)
    for i, (y, a) in enumerate(zip(years, anomalies)):
        x = pad_l + i * sw
        p.append(f'<rect x="{x:.2f}" y="{pad_t}" width="{sw + 0.5:.2f}" height="{plot_h}" '
                 f'fill="{_rdbu_r((a + vmax) / (2 * vmax))}">'
                 f'<title>{y}: {a:+.2f}°C</title></rect>')

    step = max(1, len(years) // 10)
    for i in list(range(0, len(years), step)) + [len(years) - 1]:
        x = pad_l + (i + 0.5) * sw
        p.append(f'<text x="{x:.1f}" y="{pad_t + plot_h + 16:.0f}" text-anchor="middle" '
                 f'font-size="9" fill="#333">{years[i]}</text>')
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------------------
# Annual trend with OLS line.
# ---------------------------------------------------------------------------

def annual_trend(
    annual_rows: list[dict[str, Any]],
    *,
    region_label: str,
    slope_per_decade: float | None = None,
) -> str:
    """Year-over-year mean temperature points with an OLS trend line."""
    from _noaa_tools.climate_analysis import simple_linear_regression

    rows = sorted((r for r in annual_rows if r.get("temp_mean") is not None),
                  key=lambda r: r["year"])
    if not rows:
        raise ValueError("annual_trend needs at least one row with temp_mean")

    years = [r["year"] for r in rows]
    temps = [r["temp_mean"] for r in rows]
    xs = [float(y) for y in years]
    slope, intercept = simple_linear_regression(xs, temps)
    fit = [slope * x + intercept for x in xs]
    if slope_per_decade is None:
        slope_per_decade = slope * 10

    W, H = 760, 400
    pad_l, pad_r, pad_t, pad_b = 56, 20, 46, 42
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    p = _svg_open(W, H, f"Annual mean temperature — {region_label}")

    y0, y1 = years[0], years[-1]
    vlo = min(min(temps), min(fit))
    vhi = max(max(temps), max(fit))
    if vhi == vlo:
        vhi = vlo + 1.0

    def px(yr: float) -> float:
        return pad_l + (0 if y1 == y0 else (yr - y0) / (y1 - y0)) * plot_w

    def py(v: float) -> float:
        return pad_t + plot_h - (v - vlo) / (vhi - vlo) * plot_h

    for v in _yticks(vlo, vhi):
        y = py(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                 f'stroke="#eee"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#666">{v:.1f}</text>')

    # Trend line.
    p.append(f'<line x1="{px(y0):.1f}" y1="{py(fit[0]):.1f}" x2="{px(y1):.1f}" '
             f'y2="{py(fit[-1]):.1f}" stroke="#333" stroke-width="1.5" '
             f'stroke-dasharray="6 4"/>')
    # Points.
    for yr, v in zip(years, temps):
        p.append(f'<circle cx="{px(yr):.1f}" cy="{py(v):.1f}" r="2.6" fill="#d9534f">'
                 f'<title>{yr}: {v:.2f}°C</title></circle>')

    # x ticks (~every decade).
    span = max(1, y1 - y0)
    step = max(1, round(span / 10))
    yr = y0
    while yr <= y1:
        p.append(f'<text x="{px(yr):.1f}" y="{pad_t + plot_h + 16:.0f}" text-anchor="middle" '
                 f'font-size="10" fill="#333">{yr}</text>')
        yr += step
    # Legend.
    p.append(f'<rect x="{pad_l + 8}" y="{pad_t + 6}" width="200" height="22" fill="#fff" '
             f'opacity="0.85" stroke="#ddd"/>')
    p.append(f'<text x="{pad_l + 16}" y="{pad_t + 21}" font-size="11" fill="#333">'
             f'Trend: {slope_per_decade:+.2f} °C/decade</text>')
    p.append("</svg>")
    return "\n".join(p)


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
    """Year on the x-axis, month on the y-axis, colour = value (Jan at bottom)."""
    years = sorted({r["year"] for r in monthly_rows if isinstance(r.get("year"), int)})
    if not years:
        raise ValueError("year_month_heatmap needs at least one valid monthly row")

    cells: dict[tuple[int, int], float] = {}
    for r in monthly_rows:
        y, m, v = r.get("year"), r.get("month"), r.get(value_field)
        if isinstance(y, int) and isinstance(m, int) and 1 <= m <= 12 and v is not None:
            cells[(y, m)] = float(v)
    if not cells:
        return _empty_svg(760, 360, f"Year × month {value_field} — {region_label}: no data")
    vlo, vhi = min(cells.values()), max(cells.values())
    span = (vhi - vlo) or 1.0

    cbar_w = 56
    W = max(760, 40 * 1 + len(years) * 14 + 70 + cbar_w)
    H = 360
    pad_l, pad_t, pad_b = 44, 44, 36
    plot_w = W - pad_l - cbar_w - 24
    plot_h = H - pad_t - pad_b
    cw, ch = plot_w / len(years), plot_h / 12
    p = _svg_open(W, H, f"Year × month {value_field} — {region_label}")

    yidx = {y: i for i, y in enumerate(years)}
    for (y, m), v in cells.items():
        x = pad_l + yidx[y] * cw
        # Jan (m=1) at the bottom → row from the bottom up.
        yy = pad_t + plot_h - m * ch
        p.append(f'<rect x="{x:.2f}" y="{yy:.2f}" width="{cw + 0.4:.2f}" height="{ch + 0.4:.2f}" '
                 f'fill="{_rdbu_r((v - vlo) / span)}"><title>{MONTH_ABBR[m - 1]} {y}: '
                 f'{v:.1f} {value_units}</title></rect>')

    for m in range(1, 13):
        yy = pad_t + plot_h - (m - 0.5) * ch
        p.append(f'<text x="{pad_l - 6}" y="{yy + 3:.1f}" text-anchor="end" font-size="9" '
                 f'fill="#333">{MONTH_ABBR[m - 1]}</text>')
    step = max(1, len(years) // 10)
    for i in list(range(0, len(years), step)) + [len(years) - 1]:
        x = pad_l + (i + 0.5) * cw
        p.append(f'<text x="{x:.1f}" y="{pad_t + plot_h + 14:.0f}" text-anchor="middle" '
                 f'font-size="9" fill="#333">{years[i]}</text>')

    # Colorbar (vertical gradient, warm at top).
    cx = W - cbar_w + 8
    steps = 24
    for s in range(steps):
        frac = s / (steps - 1)
        seg_h = plot_h / steps
        yy = pad_t + plot_h - (s + 1) * seg_h
        p.append(f'<rect x="{cx}" y="{yy:.2f}" width="16" height="{seg_h + 0.5:.2f}" '
                 f'fill="{_rdbu_r(frac)}"/>')
    p.append(f'<text x="{cx + 20}" y="{pad_t + 8}" font-size="9" fill="#333">{vhi:.1f}</text>')
    p.append(f'<text x="{cx + 20}" y="{pad_t + plot_h}" font-size="9" fill="#333">{vlo:.1f}</text>')
    p.append(f'<text x="{cx + 20}" y="{pad_t + plot_h / 2:.0f}" font-size="9" fill="#666">'
             f'{value_units}</text>')
    p.append("</svg>")
    return "\n".join(p)


# ---------------------------------------------------------------------------
# Anomaly bar chart.
# ---------------------------------------------------------------------------

def anomaly_bars(
    anomaly_rows: list[dict[str, Any]],
    *,
    region_label: str,
    baseline: tuple[int, int],
) -> str:
    """Per-year anomaly bars — red above baseline, blue below."""
    rows = sorted((r for r in anomaly_rows if r.get("anomaly_c") is not None),
                  key=lambda r: r["year"])
    if not rows:
        raise ValueError("anomaly_bars needs at least one row with anomaly_c")

    years = [r["year"] for r in rows]
    anoms = [r["anomaly_c"] for r in rows]

    W, H = 760, 360
    pad_l, pad_r, pad_t, pad_b = 50, 16, 46, 40
    plot_w, plot_h = W - pad_l - pad_r, H - pad_t - pad_b
    p = _svg_open(W, H, f"Annual temperature anomaly — {region_label}")

    amax = max(abs(a) for a in anoms) or 1.0
    zero_y = pad_t + plot_h / 2  # symmetric around 0

    def py(v: float) -> float:
        return zero_y - (v / amax) * (plot_h / 2)

    for v in _yticks(-amax, amax):
        y = py(v)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
                 f'stroke="#f0f0f0"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#666">{v:+.1f}</text>')

    bw = plot_w / len(years)
    for i, (yr, a) in enumerate(zip(years, anoms)):
        x = pad_l + i * bw
        y = py(a)
        top = min(y, zero_y)
        h = abs(y - zero_y)
        color = "#d9534f" if a >= 0 else "#4a90d9"
        p.append(f'<rect x="{x:.2f}" y="{top:.2f}" width="{max(bw * 0.85, 1):.2f}" '
                 f'height="{h:.2f}" fill="{color}"><title>{yr}: {a:+.2f}°C</title></rect>')

    p.append(f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{pad_l + plot_w}" y2="{zero_y:.1f}" '
             f'stroke="#333" stroke-width="0.8"/>')
    step = max(1, len(years) // 10)
    for i in list(range(0, len(years), step)) + [len(years) - 1]:
        x = pad_l + (i + 0.5) * bw
        p.append(f'<text x="{x:.1f}" y="{pad_t + plot_h + 16:.0f}" text-anchor="middle" '
                 f'font-size="9" fill="#333">{years[i]}</text>')
    p.append(f'<text x="14" y="{pad_t + plot_h / 2:.0f}" font-size="10" fill="#666" '
             f'transform="rotate(-90 14 {pad_t + plot_h / 2:.0f})" text-anchor="middle">'
             f'Anomaly vs {baseline[0]}–{baseline[1]} (°C)</text>')
    p.append("</svg>")
    return "\n".join(p)
