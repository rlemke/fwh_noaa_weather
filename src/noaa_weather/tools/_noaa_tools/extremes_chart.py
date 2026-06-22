"""Render extreme-event results as a chart + HTML page.

Dependency-free: builds raw SVG strings (NO matplotlib — it isn't installed in the
runners, and a grouped bar chart is simple to emit directly). Shared by the
``weather.Extremes.RenderExtremesChart`` handler and any CLI.

Input is the ``decadal_frequency`` map (``{event_type: {decade: count}}``) that
both ``detect_events`` (per station) and ``aggregate_region`` (region, as
``by_type_decade``) produce, plus optional per-type ``trends`` (rising/falling)
to annotate the legend.
"""

from __future__ import annotations

# Stable, intuitive colors per event type; unknown types fall back to the cycle.
_COLORS = {
    "heat_wave": "#e74c3c",
    "cold_snap": "#3498db",
    "heavy_rain": "#2980b9",
    "wet_spell": "#16a085",
    "dry_spell": "#e67e22",
    "heavy_snow": "#95a5a6",
}
_CYCLE = ["#8e44ad", "#27ae60", "#f1c40f", "#c0392b", "#2c3e50"]


def _esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _color(etype: str, idx: int) -> str:
    return _COLORS.get(etype, _CYCLE[idx % len(_CYCLE)])


def _decade_num(d: str) -> int:
    try:
        return int(d.rstrip("s"))
    except ValueError:
        return 0


def _label(etype: str) -> str:
    return etype.replace("_", " ")


def decadal_bars_svg(decadal_frequency: dict, *, title: str = "Extreme events by decade",
                     trends: dict | None = None, width: int = 760, height: int = 380) -> str:
    """Grouped bar chart SVG: x = decades, one colored bar per event type per decade."""
    decades = sorted({d for m in decadal_frequency.values() for d in m}, key=_decade_num)
    types = sorted(decadal_frequency.keys())
    if not decades or not types:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                f'font-family="sans-serif"><text x="{width // 2}" y="{height // 2}" '
                f'text-anchor="middle" font-size="14" fill="#777">'
                f'{_esc(title)}: no events</text></svg>')

    maxv = max((decadal_frequency[t].get(d, 0) for t in types for d in decades), default=0) or 1
    pad_l, pad_r, pad_t, pad_b = 48, 180, 44, 44
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b
    group_w = plot_w / len(decades)
    bar_w = group_w / (len(types) + 1)

    p = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
         f'font-family="sans-serif">',
         f'<text x="{width / 2:.0f}" y="24" text-anchor="middle" font-size="16" '
         f'font-weight="bold">{_esc(title)}</text>']

    # y gridlines + labels (5 ticks)
    for i in range(6):
        v = maxv * i / 5
        y = pad_t + plot_h - (plot_h * i / 5)
        p.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w:.1f}" y2="{y:.1f}" '
                 f'stroke="#eee" stroke-width="1"/>')
        p.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="10" '
                 f'fill="#666">{v:.0f}</text>')

    # bars + decade labels
    for di, d in enumerate(decades):
        gx = pad_l + di * group_w
        for ti, t in enumerate(types):
            v = decadal_frequency[t].get(d, 0)
            bh = (v / maxv) * plot_h
            x = gx + ti * bar_w + bar_w * 0.5
            y = pad_t + plot_h - bh
            p.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w * 0.9:.1f}" '
                     f'height="{bh:.1f}" fill="{_color(t, ti)}"><title>{_esc(_label(t))} '
                     f'{_esc(d)}: {v}</title></rect>')
        p.append(f'<text x="{gx + group_w / 2:.1f}" y="{pad_t + plot_h + 16:.1f}" '
                 f'text-anchor="middle" font-size="11" fill="#333">{_esc(d)}</text>')

    # legend (with trend annotation if provided)
    trends = trends or {}
    for ti, t in enumerate(types):
        ly = pad_t + ti * 20
        lx = width - pad_r + 16
        p.append(f'<rect x="{lx}" y="{ly}" width="12" height="12" fill="{_color(t, ti)}"/>')
        lbl = _label(t)
        tr = trends.get(t)
        if tr:
            lbl += f"  ({tr.get('direction', '')} {tr.get('per_decade_change', 0):+g}/dec)"
        p.append(f'<text x="{lx + 18}" y="{ly + 11}" font-size="11" fill="#333">{_esc(lbl)}</text>')

    p.append("</svg>")
    return "\n".join(p)


def extremes_html(*, title: str, label: str, svg: str, counts_by_type: dict,
                  trends: dict | None = None, summary: str | None = None) -> str:
    """Self-contained HTML page embedding the SVG chart + a counts/trend table."""
    trends = trends or {}
    rows = []
    for t in sorted(counts_by_type):
        tr = trends.get(t, {})
        rows.append(f"<tr><td>{_esc(_label(t))}</td><td style='text-align:right'>"
                    f"{counts_by_type[t]}</td><td>{_esc(tr.get('direction', '—'))}</td>"
                    f"<td style='text-align:right'>{_esc(tr.get('per_decade_change', ''))}</td></tr>")
    table = ("<table cellpadding='6' style='border-collapse:collapse;margin-top:16px'>"
             "<thead><tr><th style='text-align:left'>Event type</th><th>Total</th>"
             "<th>Decadal trend</th><th>per decade</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table>")
    summ = f"<p style='color:#555'>{_esc(summary)}</p>" if summary else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_esc(title)}</title>"
            f"<style>body{{font-family:sans-serif;margin:32px;max-width:880px}}"
            f"th,td{{border-bottom:1px solid #eee}}</style></head><body>"
            f"<h1>{_esc(title)}</h1><p style='color:#888'>{_esc(label)}</p>{summ}"
            f"{svg}{table}</body></html>")
