"""Two time-axis views of every reported region.

Both maps share the same polygon set as ``warming_map`` (continent-
wide polygons paint under country polygons, etc.) and the same
diverging RdBu palette. What changes is the quantity mapped to
colour:

- ``warming-point-map.html``: the region's **annual anomaly** (°C vs.
  its baseline) for a single year chosen by a slider. Red years were
  hotter than baseline, blue years were cooler.

- ``warming-trend-map.html``: the region's **running OLS warming
  rate** from ``start_year`` up to the slider year, scaled to
  °C/decade. Early years show noisy slopes; slopes converge as the
  window widens.

Each HTML embeds the full per-year data array as feature properties
(``anomaly_<year>``, ``slope_<year>``) so the slider just rebuilds the
MapLibre paint expression. No network roundtrip per slider tick.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from . import (  # noqa: E402
    climate_analysis,
    sidecar,
    warming_map,
)
from .storage import LocalStorage, Storage  # noqa: E402

logger = logging.getLogger("noaa-weather.warming-time-map")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "climate-report"
POINT_MAP_RELATIVE_PATH = "warming-point-map.html"
TREND_MAP_RELATIVE_PATH = "warming-trend-map.html"

# Minimum number of years required before the running OLS slope is
# meaningful. Below this we leave slope_<year> as None so the map
# renders neutral.
MIN_YEARS_FOR_SLOPE = 3


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def rebuild_time_maps(
    storage: Storage | None = None,
) -> tuple[Path | None, Path | None]:
    """Walk report bundles and rewrite both slider-driven map HTMLs.

    Returns ``(point_map_path, trend_map_path)``. Either may be
    ``None`` when there's no data to render (e.g. no reports, or
    reports missing the ``annual`` / ``anomalies`` arrays).
    """
    s = storage or LocalStorage()
    features, year_range = _collect_features(s)
    if not features or year_range is None:
        logger.info("no time-series features — skipping slider maps")
        return None, None

    features.sort(key=lambda f: -f["properties"].get("_area", 0.0))

    year_min, year_max = year_range
    years = list(range(year_min, year_max + 1))

    out_dir = Path(sidecar.cache_dir(NAMESPACE, CACHE_TYPE, s))
    out_dir.mkdir(parents=True, exist_ok=True)

    point_path = out_dir / POINT_MAP_RELATIVE_PATH
    point_html = _render_point_map(features, years)
    point_path.write_text(point_html, encoding="utf-8")
    _write_sidecar(s, POINT_MAP_RELATIVE_PATH, point_html, mode="point")

    trend_path = out_dir / TREND_MAP_RELATIVE_PATH
    trend_html = _render_trend_map(features, years)
    trend_path.write_text(trend_html, encoding="utf-8")
    _write_sidecar(s, TREND_MAP_RELATIVE_PATH, trend_html, mode="trend")

    logger.info(
        "wrote slider maps: %s, %s (%d regions, years %d-%d)",
        point_path,
        trend_path,
        len(features),
        year_min,
        year_max,
    )
    return point_path, trend_path


# ---------------------------------------------------------------------------
# Discovery + per-year math.
# ---------------------------------------------------------------------------

def _collect_features(
    storage: Storage,
) -> tuple[list[dict[str, Any]], tuple[int, int] | None]:
    """Build the GeoJSON feature list + the union of all report year ranges."""
    root = Path(sidecar.cache_dir(NAMESPACE, CACHE_TYPE, storage))
    if not root.is_dir():
        return [], None

    features: list[dict[str, Any]] = []
    year_min = 9999
    year_max = 0

    for sidecar_path in root.rglob("report.html.meta.json"):
        try:
            side = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping unreadable sidecar %s: %s", sidecar_path, exc)
            continue
        extra = side.get("extra") or {}
        region = extra.get("region") or {}

        # Geometry comes from the same resolver the static warming map uses.
        geometry = warming_map._geometry_for_region(region, storage)  # noqa: SLF001
        if geometry is None:
            continue

        # Load the report.json for per-year arrays. Readers that don't
        # have this file (older bundles, hand-curated reports) are
        # silently skipped.
        bundle_dir = sidecar_path.parent
        json_path = bundle_dir / "report.json"
        if not json_path.is_file():
            logger.info("skipping %s: no report.json", bundle_dir)
            continue
        try:
            report = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping %s: %s", json_path, exc)
            continue

        anomalies = report.get("anomalies") or []
        annual = report.get("annual") or []
        if not anomalies and not annual:
            continue

        # Per-year anomaly map — anomaly_<year> = °C deviation from the
        # report's baseline window.
        anomaly_map: dict[int, float | None] = {}
        for row in anomalies:
            yr = row.get("year")
            if isinstance(yr, int):
                anomaly_map[yr] = row.get("anomaly_c")

        # Per-year running OLS slope (°C/decade) using annual temp_mean
        # from start_year up to that year.
        slope_map = _running_slope_map(annual)

        year_range = report.get("year_range") or []
        if len(year_range) == 2 and all(isinstance(y, int) for y in year_range):
            year_min = min(year_min, year_range[0])
            year_max = max(year_max, year_range[1])

        rel_dir = sidecar_path.parent.relative_to(root).as_posix()
        href = f"{rel_dir}/report.html"
        props: dict[str, Any] = {
            "name": region.get("label") or region.get("name") or rel_dir,
            "href": href,
            "year_range": year_range,
            "_area": warming_map._bbox_area(geometry),  # noqa: SLF001
        }
        # Flatten per-year series — keys like ``anomaly_2005`` /
        # ``slope_2005`` so the MapLibre paint expression can pick
        # them up with a string-built key.
        for yr, val in anomaly_map.items():
            if val is None:
                continue
            props[f"anomaly_{yr}"] = round(float(val), 3)
        for yr, val in slope_map.items():
            if val is None:
                continue
            props[f"slope_{yr}"] = round(float(val), 3)

        features.append(
            {"type": "Feature", "geometry": geometry, "properties": props}
        )

    if year_min > year_max:
        return [], None
    return features, (year_min, year_max)


def _running_slope_map(
    annual: list[dict[str, Any]],
) -> dict[int, float | None]:
    """Return ``{year: slope_°C_per_decade}`` for each year of ``annual``.

    Slope is computed from ``annual[0..i]`` via
    :func:`climate_analysis.simple_linear_regression`; years before
    ``MIN_YEARS_FOR_SLOPE`` map to ``None``.
    """
    xs: list[float] = []
    ys: list[float] = []
    out: dict[int, float | None] = {}
    for row in annual:
        yr = row.get("year")
        tm = row.get("temp_mean")
        if not isinstance(yr, int) or tm is None:
            continue
        xs.append(float(yr))
        ys.append(float(tm))
        if len(xs) >= MIN_YEARS_FOR_SLOPE:
            slope, _ = climate_analysis.simple_linear_regression(xs, ys)
            out[yr] = round(slope * 10.0, 3)  # °C/decade
        else:
            out[yr] = None
    return out


def _write_sidecar(
    storage: Storage,
    relative_path: str,
    html: str,
    *,
    mode: str,
) -> None:
    body = html.encode("utf-8")
    sidecar.write_sidecar(
        NAMESPACE,
        CACHE_TYPE,
        relative_path,
        kind="file",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        tool={"name": "warming_time_map", "version": "1.0"},
        extra={"mode": mode},
        storage=storage,
    )


# ---------------------------------------------------------------------------
# HTML rendering — point-in-time map.
# ---------------------------------------------------------------------------

def _render_point_map(
    features: list[dict[str, Any]],
    years: list[int],
) -> str:
    return _render_slider_map(
        title="Annual anomaly by reported region",
        subtitle=(
            "Each polygon is coloured by its annual-mean temperature anomaly "
            "(°C vs. the region's WMO baseline) for the year shown on the "
            "slider. Red = above baseline, blue = below."
        ),
        legend_label="°C anomaly",
        property_prefix="anomaly_",
        features=features,
        years=years,
        color_stops=[
            (-3.0, "#053061"),
            (-1.5, "#2166ac"),
            (-0.5, "#92c5de"),
            (0.0, "#f7f7f7"),
            (0.5, "#f4a582"),
            (1.5, "#b2182b"),
            (3.0, "#67001f"),
        ],
        nav_links=[
            ("Master index →", "index.html"),
            ("All-time warming →", "warming-map.html"),
            ("Running trend (cumulative) →", "warming-trend-map.html"),
        ],
    )


# ---------------------------------------------------------------------------
# HTML rendering — running-trend map.
# ---------------------------------------------------------------------------

def _render_trend_map(
    features: list[dict[str, Any]],
    years: list[int],
) -> str:
    return _render_slider_map(
        title="Running OLS warming rate — start year to selected year",
        subtitle=(
            "Each polygon is coloured by the ordinary-least-squares slope "
            "of its annual-mean temperature (°C/decade) across the window "
            "from the report's start year up to the slider's year. Early "
            "windows (first few years) are noisy; slopes converge as the "
            "window widens."
        ),
        legend_label="°C / decade",
        property_prefix="slope_",
        features=features,
        years=years,
        color_stops=warming_map.WARMING_COLOR_STOPS,
        nav_links=[
            ("Master index →", "index.html"),
            ("All-time warming →", "warming-map.html"),
            ("Point-in-time anomaly →", "warming-point-map.html"),
        ],
    )


# ---------------------------------------------------------------------------
# Shared renderer — both maps use the same slider + MapLibre skeleton.
# ---------------------------------------------------------------------------

def _render_slider_map(
    *,
    title: str,
    subtitle: str,
    legend_label: str,
    property_prefix: str,
    features: list[dict[str, Any]],
    years: list[int],
    color_stops: list[tuple[float, str]],
    nav_links: list[tuple[str, str]],
) -> str:
    year_min = years[0]
    year_max = years[-1]
    default_year = year_max  # most recent year by default

    stops_js = ", ".join(
        f"{val}, {json.dumps(color)}" for val, color in color_stops
    )
    legend_items = "\n".join(
        f'<li><span class="swatch" style="background:{c}"></span>'
        f'{v:+.2f}</li>'
        for v, c in color_stops
    )

    # Strip the z-order helper (``_area``) before inlining — it's
    # private to Python-side sorting and the JS doesn't need it.
    export_features = []
    for f in features:
        props = dict(f["properties"])
        props.pop("_area", None)
        export_features.append({**f, "properties": props})
    geojson_js = json.dumps(
        {"type": "FeatureCollection", "features": export_features},
        separators=(",", ":"),
    )

    nav_html = " · ".join(
        f'<a href="{html_mod.escape(href)}">{html_mod.escape(label)}</a>'
        for label, href in nav_links
    )

    style = textwrap.dedent(
        """\
        html, body { margin: 0; padding: 0; height: 100%;
                     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                                  Roboto, sans-serif; }
        #map { position: absolute; top: 0; bottom: 0; left: 0; right: 0; }
        header {
          position: absolute; top: 10px; left: 10px; z-index: 10;
          background: rgba(255,255,255,0.96); padding: 10px 14px;
          border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          max-width: 440px; font-size: 13px;
        }
        header h1 { margin: 0 0 4px; font-size: 15px; }
        header p { margin: 4px 0; color: #555; }
        header nav { margin-top: 6px; }
        header nav a { color: #1a56db; text-decoration: none; font-weight: 600;
                       margin-right: 4px; }
        header nav a:hover { text-decoration: underline; }
        .year-bar {
          position: absolute; bottom: 20px; left: 50%;
          transform: translateX(-50%); z-index: 10;
          background: rgba(255,255,255,0.96); border-radius: 8px;
          padding: 10px 18px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          min-width: 420px; font-size: 13px; text-align: center;
        }
        .year-bar .label { font-weight: 600; margin-bottom: 4px;
                           font-size: 14px; color: #222; }
        .year-bar input[type=range] { width: 100%; }
        .year-bar .range { display: flex; justify-content: space-between;
                           color: #888; font-size: 11px; margin-top: 2px; }
        .legend {
          position: absolute; bottom: 20px; left: 20px; z-index: 10;
          background: rgba(255,255,255,0.96); padding: 8px 12px;
          border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          font-size: 12px;
        }
        .legend h4 { margin: 0 0 4px; font-size: 12px; color: #555; }
        .legend ul { margin: 0; padding: 0; list-style: none; }
        .legend li { display: flex; align-items: center; margin: 3px 0; }
        .legend .swatch {
          display: inline-block; width: 14px; height: 14px; margin-right: 6px;
          border: 1px solid rgba(0,0,0,0.2);
        }
        .maplibregl-popup-content { max-width: 360px; font-size: 12px; }
        .maplibregl-popup-content h4 { margin: 0 0 4px; font-size: 13px; }
        .maplibregl-popup-content .metric {
          font-size: 14px; font-weight: 600; margin: 2px 0 6px;
        }
        """
    )

    script = textwrap.dedent(
        f"""\
        const GEO = {geojson_js};
        const PROP_PREFIX = {json.dumps(property_prefix)};
        const YEAR_MIN = {year_min};
        const YEAR_MAX = {year_max};

        const BASEMAP_TILES = [
          'https://cartodb-basemaps-a.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-b.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-c.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-d.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'
        ];
        const BASEMAP_ATTRIB =
          '\\u00a9 <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors \\u00a9 <a href=\"https://carto.com/attributions\">CARTO</a>';

        const map = new maplibregl.Map({{
          container: 'map',
          style: {{
            version: 8,
            sources: {{
              basemap: {{
                type: 'raster',
                tiles: BASEMAP_TILES,
                tileSize: 256,
                attribution: BASEMAP_ATTRIB
              }}
            }},
            layers: [{{ id: 'basemap', type: 'raster', source: 'basemap' }}]
          }},
          center: [0, 20],
          zoom: 1.5,
          hash: true
        }});

        // Rebuild the paint expression for the year visible on the slider.
        // We can't use a JS variable inside a paint expression directly,
        // so we rebuild the whole expression on slider change — cheap.
        function paintForYear(year) {{
          const key = PROP_PREFIX + year;
          return [
            'case',
            ['has', key],
            ['interpolate', ['linear'], ['get', key], {stops_js}],
            '#ccccccbf'  // neutral grey for regions without data at this year
          ];
        }}

        let currentYear = {default_year};
        function applyYear(year) {{
          currentYear = year;
          document.getElementById('year-value').textContent = year;
          if (map.getLayer('reports-fill')) {{
            map.setPaintProperty('reports-fill', 'fill-color', paintForYear(year));
          }}
        }}

        map.on('load', () => {{
          map.addSource('reports', {{ type: 'geojson', data: GEO }});
          map.addLayer({{
            id: 'reports-fill',
            type: 'fill',
            source: 'reports',
            paint: {{
              'fill-color': paintForYear(currentYear),
              'fill-opacity': 0.62
            }}
          }});
          map.addLayer({{
            id: 'reports-outline',
            type: 'line',
            source: 'reports',
            paint: {{
              'line-color': '#333',
              'line-width': 0.7,
              'line-opacity': 0.6
            }}
          }});

          map.on('click', 'reports-fill', (e) => {{
            if (!e.features.length) return;
            const f = e.features[0];
            const p = f.properties || {{}};
            const key = PROP_PREFIX + currentYear;
            const raw = p[key];
            const val = typeof raw === 'string' ? parseFloat(raw) : raw;
            const hasVal = val !== undefined && val !== null && !isNaN(val);
            const sign = hasVal && val >= 0 ? '+' : '';
            const display = hasVal
              ? `${{sign}}${{val.toFixed(2)}} ${{PROP_PREFIX === 'slope_' ? '\\u00b0C / decade' : '\\u00b0C'}}`
              : 'no data';
            const yr = p.year_range ? JSON.parse(p.year_range) : null;
            const yrStr = yr ? `<p>Report range: ${{yr[0]}}\\u2013${{yr[1]}}</p>` : '';
            const href = p.href || '';
            const link = href
              ? `<p><a href='${{href}}' target='_blank'>Open report &rarr;</a></p>`
              : '';
            new maplibregl.Popup({{ closeButton: true }})
              .setLngLat(e.lngLat)
              .setHTML(
                `<h4>${{p.name || href}}</h4>` +
                `<div class='metric'>${{currentYear}}: ${{display}}</div>` +
                yrStr + link
              )
              .addTo(map);
          }});
          map.on('mouseenter', 'reports-fill', () => map.getCanvas().style.cursor = 'pointer');
          map.on('mouseleave', 'reports-fill', () => map.getCanvas().style.cursor = '');
        }});

        // Slider wiring.
        const slider = document.getElementById('year-slider');
        slider.addEventListener('input', (e) => applyYear(parseInt(e.target.value, 10)));
        applyYear(currentYear);
        """
    )

    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>{html_mod.escape(title)}</title>
          <link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
          <style>{style}</style>
        </head>
        <body>
        <div id="map"></div>
        <header>
          <h1>{html_mod.escape(title)}</h1>
          <p>{html_mod.escape(subtitle)}</p>
          <nav>{nav_html}</nav>
        </header>
        <aside class="year-bar">
          <div class="label">Year: <span id="year-value">{default_year}</span></div>
          <input id="year-slider" type="range"
                 min="{year_min}" max="{year_max}"
                 step="1" value="{default_year}">
          <div class="range"><span>{year_min}</span><span>{year_max}</span></div>
        </aside>
        <aside class="legend">
          <h4>{html_mod.escape(legend_label)}</h4>
          <ul>{legend_items}</ul>
        </aside>
        <script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
        <script>
        {script}
        </script>
        </body>
        </html>
        """
    )
