"""Global choropleth of every cached climate report, coloured by warming rate.

Every region that has a climate-report bundle in the cache renders as a
polygon on a MapLibre world map. Fill colour encodes
``warming_rate_per_decade``: dark blue for cooling, neutral beige at
zero, dark red for warming — the diverging RdBu ColorBrewer palette
keeps the sign axis intuitive.

Regions are drawn in source order; we sort them by polygon area
descending so continental polygons (North America) paint at the bottom
and sub-country polygons (California, individual states) layer on top.
Click a polygon to open its ``report.html``.

Output lives alongside the report index at
``cache/noaa-weather/climate-report/warming-map.html`` with a sibling
``.meta.json`` sidecar. Called from
:func:`_noaa_tools.climate_report.generate_climate_report` at the same time
the master index regenerates.
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

from . import geofabrik_regions, ghcn_parse, natural_earth, sidecar  # noqa: E402
from .storage import Storage, get_storage  # noqa: E402

logger = logging.getLogger("noaa-weather.warming-map")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "climate-report"
OUTPUT_RELATIVE_PATH = "warming-map.html"

# Diverging RdBu palette (ColorBrewer, reversed so blue = cool). The
# stops are chosen to keep the interesting -0.3..+0.3 °C/decade range
# visually sensitive without clipping the extremes.
WARMING_COLOR_STOPS: list[tuple[float, str]] = [
    (-0.5, "#053061"),   # dark blue — strong cooling
    (-0.25, "#4393c3"),
    (-0.1, "#d1e5f0"),
    (0.0, "#f7f7f7"),    # neutral
    (0.1, "#fddbc7"),
    (0.25, "#d6604d"),
    (0.5, "#67001f"),    # dark red — strong warming
]


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def rebuild_warming_map(storage: Storage | None = None) -> Path | None:
    """Walk every cached climate-report bundle, render the warming map.

    Returns the output HTML path, or None if no reports were found.
    """
    s = storage or get_storage()
    features = _collect_features(s)
    if not features:
        logger.info("no regions with warming data — skipping warming map")
        return None

    # Sort descending by area — largest polygons paint first (bottom),
    # smallest last (top), so sub-regions always render visibly over
    # their parent continents.
    features.sort(key=lambda f: -f["properties"].get("_area", 0.0))

    geojson = {"type": "FeatureCollection", "features": features}
    html = _render_html(geojson)

    # Write through the storage abstraction (local path or s3://MinIO).
    out_path = s.join(sidecar.cache_dir(NAMESPACE, CACHE_TYPE, s), OUTPUT_RELATIVE_PATH)
    s.write_text_atomic(out_path, html)

    body_bytes = html.encode("utf-8")
    sidecar.write_sidecar(
        NAMESPACE,
        CACHE_TYPE,
        OUTPUT_RELATIVE_PATH,
        kind="file",
        size_bytes=len(body_bytes),
        sha256=hashlib.sha256(body_bytes).hexdigest(),
        tool={"name": "warming_map", "version": "1.0"},
        extra={
            "region_count": len(features),
            "palette": "RdBu_r",
            "stops_celsius_per_decade": [c[0] for c in WARMING_COLOR_STOPS],
        },
        storage=s,
    )
    logger.info("wrote warming choropleth at %s (%d regions)", out_path, len(features))
    return out_path


# ---------------------------------------------------------------------------
# Feature collection.
# ---------------------------------------------------------------------------

def _collect_features(storage: Storage) -> list[dict[str, Any]]:
    """For each report, emit one GeoJSON Feature with the warming colour."""
    root = Path(sidecar.cache_dir(NAMESPACE, CACHE_TYPE, storage))
    if not root.is_dir():
        return []

    features: list[dict[str, Any]] = []
    for sidecar_path in root.rglob("report.html.meta.json"):
        try:
            data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping unreadable sidecar %s: %s", sidecar_path, exc)
            continue
        extra = data.get("extra") or {}
        region = extra.get("region") or {}
        trend = extra.get("trend") or {}
        warming = trend.get("warming_rate_per_decade")
        if not isinstance(warming, (int, float)):
            # Bundles predating the trend-in-sidecar change (3e0ec1a)
            # don't carry warming rate; skip them quietly.
            continue

        geometry = _geometry_for_region(region, storage)
        if geometry is None:
            continue

        rel_dir = sidecar_path.parent.relative_to(root).as_posix()
        href = f"{rel_dir}/report.html"
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "name": region.get("label")
                    or region.get("name")
                    or rel_dir,
                    "href": href,
                    "warming": float(warming),
                    "year_range": extra.get("year_range"),
                    "station_count": extra.get("station_count", 0),
                    "narrative": trend.get("narrative", ""),
                    "_area": _bbox_area(geometry),
                },
            }
        )
    return features


def _geometry_for_region(
    region: dict[str, Any], storage: Storage
) -> dict[str, Any] | None:
    """Resolve a region dict to a GeoJSON geometry.

    Preference order:
      1. Geofabrik path → real polygon from the cached index (accurate
         where Geofabrik has a per-region PBF).
      2. Country + state → Natural Earth admin-1 polygon (real state
         boundary worldwide, not just the US).
      3. US-state last-resort → rectangle from
         ``ghcn_parse.US_STATE_BOUNDS`` if Natural Earth wasn't
         reachable or the state isn't in the admin-1 set.
      4. None — report is included in the master index but skipped on
         the map.
    """
    path = region.get("path")
    if path:
        try:
            return geofabrik_regions.get_geometry(path, storage=storage)
        except (KeyError, ValueError) as exc:
            logger.info("skipping %r: %s", path, exc)
            return None

    country = region.get("country") or ""
    state = region.get("state") or ""

    # Natural Earth admin-1 catches the non-US case AND replaces the
    # rectangle for US states with a real polygon.
    if country and state:
        try:
            poly = natural_earth.resolve_state_polygon(
                country, state, storage=storage
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.info("natural-earth lookup failed for %s/%s: %s", country, state, exc)
            poly = None
        if poly is not None:
            return poly

    # Last-resort US-state bbox rectangle. Kept so a first-run map
    # still shows the region even if the Natural Earth GeoJSON isn't
    # cached yet (e.g. offline or pre-install).
    if state:
        bounds = ghcn_parse.US_STATE_BOUNDS.get(state.upper())
        if bounds is not None:
            logger.info(
                "using US_STATE_BOUNDS rectangle for %s — "
                "natural-earth didn't match",
                state,
            )
            return _bbox_to_polygon(bounds)
        logger.info("no polygon for state %r — skipping", state)

    # Country-only reports (no state, no Geofabrik path) are still
    # skipped on the map. Adding a country polygon layer would need
    # either Geofabrik country-level paths or Natural Earth admin-0.
    return None


def _bbox_to_polygon(
    bounds: tuple[float, float, float, float],
) -> dict[str, Any]:
    """(min_lat, max_lat, min_lon, max_lon) → GeoJSON rectangle Polygon."""
    min_lat, max_lat, min_lon, max_lon = bounds
    ring = [
        [min_lon, min_lat],
        [max_lon, min_lat],
        [max_lon, max_lat],
        [min_lon, max_lat],
        [min_lon, min_lat],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _bbox_area(geometry: dict[str, Any]) -> float:
    """Rough "how big is this polygon" proxy used only for z-ordering.

    Uses the geometry's axis-aligned bbox area in squared-degrees. Not
    accurate for real area (doesn't account for latitude projection
    distortion), but fine for deciding which polygon sits on top.
    """
    min_lon = 180.0
    max_lon = -180.0
    min_lat = 90.0
    max_lat = -90.0

    def _visit(pt: list[float]) -> None:
        nonlocal min_lon, max_lon, min_lat, max_lat
        if len(pt) < 2:
            return
        lon, lat = pt[0], pt[1]
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)

    coords = geometry.get("coordinates") or []
    gtype = geometry.get("type")
    if gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                _visit(pt)
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for pt in ring:
                    _visit(pt)

    if min_lon > max_lon or min_lat > max_lat:
        return 0.0
    return (max_lon - min_lon) * (max_lat - min_lat)


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------

def _render_html(geojson: dict[str, Any]) -> str:
    title = "NOAA Climate Reports — Warming Choropleth"
    stops_js = ", ".join(
        f"{val}, {json.dumps(color)}" for val, color in WARMING_COLOR_STOPS
    )
    legend_items = "\n".join(
        f'<li><span class="swatch" style="background:{c}"></span>{v:+.2f} °C/dec</li>'
        for v, c in WARMING_COLOR_STOPS
    )

    style = textwrap.dedent(
        """\
        html, body { margin: 0; padding: 0; height: 100%;
                     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                                  Roboto, sans-serif; }
        #map { position: absolute; top: 0; bottom: 0; left: 0; right: 0; }
        header {
          position: absolute; top: 10px; left: 10px; z-index: 10;
          background: rgba(255,255,255,0.95); padding: 10px 14px;
          border-radius: 6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          max-width: 380px; font-size: 13px;
        }
        header h1 { margin: 0 0 4px; font-size: 15px; }
        header p { margin: 4px 0; color: #555; }
        header a { color: #1a56db; text-decoration: none; font-weight: 600; }
        header a:hover { text-decoration: underline; }
        .legend {
          position: absolute; bottom: 20px; left: 20px; z-index: 10;
          background: rgba(255,255,255,0.95); padding: 8px 12px;
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
        .maplibregl-popup-content .warming {
          font-size: 14px; font-weight: 600; margin: 2px 0 6px;
        }
        """
    )

    geojson_js = json.dumps(geojson, separators=(",", ":"))

    script = textwrap.dedent(
        f"""\
        const GEO = {geojson_js};

        const BASEMAP_TILES = [
          'https://cartodb-basemaps-a.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-b.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-c.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-d.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'
        ];
        const BASEMAP_ATTRIB = '\\u00a9 <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors \\u00a9 <a href=\"https://carto.com/attributions\">CARTO</a>';

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

        map.on('load', () => {{
          map.addSource('reports', {{ type: 'geojson', data: GEO }});

          // Filled polygon with data-driven color.
          map.addLayer({{
            id: 'reports-fill',
            type: 'fill',
            source: 'reports',
            paint: {{
              'fill-color': ['interpolate', ['linear'], ['get', 'warming'], {stops_js}],
              'fill-opacity': 0.55
            }}
          }});
          // Outline so overlapping polygons remain distinguishable.
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
            const w = typeof p.warming === 'string' ? parseFloat(p.warming) : p.warming;
            const wStr = (w >= 0 ? '+' : '') + w.toFixed(2) + ' \\u00b0C/decade';
            const href = p.href || '';
            const title = p.name || href;
            const narrative = p.narrative || '';
            const yr = p.year_range ? JSON.parse(p.year_range) : null;
            const yrStr = yr ? `<p>Years: ${{yr[0]}}\\u2013${{yr[1]}}</p>` : '';
            const stn = p.station_count ? `<p>Stations: ${{p.station_count}}</p>` : '';
            const link = href
              ? `<p><a href='${{href}}' target='_blank'>Open report &rarr;</a></p>`
              : '';
            new maplibregl.Popup({{ closeButton: true }})
              .setLngLat(e.lngLat)
              .setHTML(
                `<h4>${{title}}</h4>` +
                `<div class='warming'>${{wStr}}</div>` +
                (narrative ? `<p>${{narrative}}</p>` : '') +
                yrStr + stn + link
              )
              .addTo(map);
          }});
          map.on('mouseenter', 'reports-fill', () => map.getCanvas().style.cursor = 'pointer');
          map.on('mouseleave', 'reports-fill', () => map.getCanvas().style.cursor = '');
        }});
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
          <h1>Warming rate by reported region</h1>
          <p>Polygons are coloured by the climate report's OLS warming rate per decade. Click a polygon to open its report. Larger regions layer below smaller ones.</p>
          <p><a href="index.html">&larr; Master index</a> · <a href="warming-point-map.html">Point-in-time &rarr;</a> · <a href="warming-trend-map.html">Running trend &rarr;</a></p>
        </header>
        <aside class="legend">
          <h4>°C / decade</h4>
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
