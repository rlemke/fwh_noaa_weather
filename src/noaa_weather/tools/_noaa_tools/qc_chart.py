"""Render a quality-control summary as a chart + HTML page.

Dependency-free: builds raw SVG strings (NO matplotlib — it isn't installed in
the runners). Shared by the ``weather.QC.RenderQCChart`` handler and the
``summarize-quality-flags`` CLI.

Input is the summary dict produced by ``summarize_quality_flags`` (one station)
or ``aggregate_region_qc`` (a region) — both carry ``by_element``
({element: {total, flagged, pct}}) and ``by_flag`` ({letter: {count, label}});
a region summary also carries ``worst_stations``. The headline chart is a
horizontal bar per element of its **flagged %** (which measurements to distrust),
colored by severity.
"""

from __future__ import annotations

from typing import Any

# Fixed element order so the chart reads consistently regardless of dict order.
_ELEMENT_ORDER = ["TMAX", "TMIN", "PRCP", "SNOW", "SNWD"]

# Plain-language meaning of each GHCN-Daily element code, for the chart glossary.
_ELEMENT_MEANINGS = {
    "TMAX": "Daily maximum air temperature",
    "TMIN": "Daily minimum air temperature",
    "PRCP": "Daily precipitation (rain plus melted snow/ice)",
    "SNOW": "Daily snowfall",
    "SNWD": "Snow depth (snow lying on the ground)",
}

# One-line explainer of what the page is, shown above the chart.
_QC_EXPLAINER = (
    "<b>Data quality</b> here means NOAA's quality-control (QC) screening of this "
    "station's daily weather observations. Every value is run through automated "
    "checks (duplicate, gap, climatological outlier, internal consistency, …) "
    "and flagged if it fails; flagged values are dropped from the climate analysis. "
    "The bars below show, for each measurement type, the share of observations that "
    "were flagged — <b>lower is better</b> (more of the record is trustworthy)."
)


def _esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _severity_color(pct: float) -> str:
    """Green (clean) → amber → red (heavily flagged). Thresholds in flagged %."""
    if pct <= 0.1:
        return "#27ae60"
    if pct <= 0.5:
        return "#f1c40f"
    if pct <= 2.0:
        return "#e67e22"
    return "#c0392b"


def _ordered_elements(by_element: dict) -> list[str]:
    known = [e for e in _ELEMENT_ORDER if e in by_element]
    extra = sorted(e for e in by_element if e not in _ELEMENT_ORDER)
    return known + extra


def flagged_pct_bars_svg(
    by_element: dict,
    *,
    title: str = "QC rejection rate by element",
    width: int = 720,
    height: int | None = None,
) -> str:
    """Horizontal bar chart SVG: one bar per element, length = its flagged %."""
    elements = _ordered_elements(by_element or {})
    if not elements:
        h = height or 160
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{h}" '
                f'font-family="sans-serif"><text x="{width // 2}" y="{h // 2}" '
                f'text-anchor="middle" font-size="14" fill="#777">'
                f'{_esc(title)}: no data</text></svg>')

    row_h = 34
    pad_l, pad_r, pad_t, pad_b = 64, 120, 48, 28
    plot_w = width - pad_l - pad_r
    height = height or (pad_t + pad_b + row_h * len(elements))
    maxv = max((by_element[e].get("pct", 0.0) for e in elements), default=0.0) or 1.0

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'font-family="sans-serif">',
         f'<text x="{width / 2:.0f}" y="26" text-anchor="middle" font-size="16" '
         f'font-weight="bold">{_esc(title)}</text>']

    for i, elem in enumerate(elements):
        rec = by_element[elem]
        pct = rec.get("pct", 0.0)
        flagged = rec.get("flagged", 0)
        total = rec.get("total", 0)
        y = pad_t + i * row_h
        bw = (pct / maxv) * plot_w
        p.append(f'<text x="{pad_l - 8}" y="{y + row_h / 2 + 4:.0f}" text-anchor="end" '
                 f'font-size="12" fill="#333">{_esc(elem)}</text>')
        # track + bar
        p.append(f'<rect x="{pad_l}" y="{y + 6:.0f}" width="{plot_w}" height="{row_h - 14}" '
                 f'fill="#f3f3f3"/>')
        p.append(f'<rect x="{pad_l}" y="{y + 6:.0f}" width="{max(bw, 1):.1f}" '
                 f'height="{row_h - 14}" fill="{_severity_color(pct)}">'
                 f'<title>{_esc(elem)}: {flagged}/{total} = {pct}%</title></rect>')
        p.append(f'<text x="{pad_l + plot_w + 8}" y="{y + row_h / 2 + 4:.0f}" '
                 f'font-size="12" fill="#333">{pct}%  ({flagged:,}/{total:,})</text>')

    p.append("</svg>")
    return "\n".join(p)


def _flag_rows(by_flag: dict) -> str:
    rows = []
    for letter, rec in (by_flag or {}).items():
        count = rec.get("count", 0) if isinstance(rec, dict) else rec
        label = rec.get("label", "") if isinstance(rec, dict) else ""
        rows.append(f"<tr><td style='text-align:center'><code>{_esc(letter)}</code></td>"
                    f"<td>{_esc(label)}</td><td style='text-align:right'>{count:,}</td></tr>")
    if not rows:
        return ""
    return ("<h2 style='font-size:15px'>Which QC check tripped</h2>"
            "<table cellpadding='6' style='border-collapse:collapse'>"
            "<thead><tr><th>Flag</th><th style='text-align:left'>Meaning</th>"
            "<th>Observations</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _worst_station_rows(worst_stations: list) -> str:
    if not worst_stations:
        return ""
    rows = []
    for w in worst_stations:
        name = w.get("station_name") or ""
        rows.append(f"<tr><td><code>{_esc(w.get('station_id', ''))}</code></td>"
                    f"<td>{_esc(name)}</td>"
                    f"<td style='text-align:right'>{w.get('flagged_pct', 0)}%</td>"
                    f"<td style='text-align:right'>{w.get('flagged_obs', 0):,}/"
                    f"{w.get('total_obs', 0):,}</td></tr>")
    return ("<h2 style='font-size:15px'>Worst stations</h2>"
            "<table cellpadding='6' style='border-collapse:collapse'>"
            "<thead><tr><th style='text-align:left'>Station</th>"
            "<th style='text-align:left'>Name</th><th>Flagged %</th>"
            "<th>Flagged/Total</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _element_key_rows(by_element: dict) -> str:
    """Glossary table explaining the element codes shown on the chart."""
    elements = _ordered_elements(by_element or {})
    if not elements:
        return ""
    rows = [
        f"<tr><td><code>{_esc(e)}</code></td>"
        f"<td>{_esc(_ELEMENT_MEANINGS.get(e, 'GHCN-Daily element'))}</td></tr>"
        for e in elements
    ]
    return ("<h2 style='font-size:15px'>What the codes mean</h2>"
            "<table cellpadding='6' style='border-collapse:collapse'>"
            "<thead><tr><th>Code</th><th style='text-align:left'>Measurement</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def qc_html(
    *,
    title: str,
    label: str,
    svg: str,
    by_element: dict | None = None,
    by_flag: dict | None = None,
    worst_stations: list | None = None,
    summary: str | None = None,
) -> str:
    """Self-contained HTML page embedding the SVG chart + flag/worst-station tables.

    Includes a plain-language explainer of what "data quality" means and a
    glossary of the element codes (TMAX/SNWD/…), so the page is readable without
    prior GHCN knowledge.
    """
    summ = f"<p style='color:#555'>{_esc(summary)}</p>" if summary else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)}</title>"
            f"<style>body{{font-family:sans-serif;margin:32px;max-width:880px}}"
            f"h2{{margin-top:28px}}th,td{{border-bottom:1px solid #eee}}"
            f".intro{{color:#444;background:#f7f9fb;border-left:3px solid #cdd7e0;"
            f"padding:10px 14px;line-height:1.5}}"
            f"code{{background:#f6f6f6;padding:1px 4px;border-radius:3px}}</style></head><body>"
            f"<h1>{_esc(title)}</h1><p style='color:#888'>{_esc(label)}</p>{summ}"
            f"<p class='intro'>{_QC_EXPLAINER}</p>"
            f"{svg}{_element_key_rows(by_element or {})}"
            f"{_flag_rows(by_flag or {})}{_worst_station_rows(worst_stations or [])}"
            f"</body></html>")
