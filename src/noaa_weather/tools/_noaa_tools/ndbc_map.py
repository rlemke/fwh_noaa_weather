"""MapLibre points map of every cached NDBC buoy.

Reads the normalized ``ndbc-catalog/stations.json``, optionally joins
yearly summaries from ``buoy-summaries/`` so popups can show the
station's most recent air / sea temp, and writes
``cache/noaa-weather/ndbc-catalog/buoys-map.html`` (+ sidecar).

Each station type gets its own colour. Click a buoy to see its name,
type, owner, lat/lon, and — if summarised — its latest yearly means.
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

from . import ndbc_download, sidecar  # noqa: E402
from .storage import Storage, get_storage  # noqa: E402

logger = logging.getLogger("noaa-weather.ndbc-map")

NAMESPACE = "noaa-weather"
CATALOG_CACHE_TYPE = ndbc_download.CATALOG_CACHE_TYPE
SUMMARY_CACHE_TYPE = "buoy-summaries"
OUTPUT_RELATIVE_PATH = "buoys-map.html"

# One colour per common station type. Anything unknown falls back to
# a neutral grey.
STATION_TYPE_COLORS: dict[str, str] = {
    "buoy": "#1f78b4",     # moored / drifting ocean buoys — blue
    "cman": "#33a02c",     # coastal C-MAN — green
    "dart": "#e31a1c",     # tsunami DART — red
    "nerrs": "#6a3d9a",    # NERRS estuarine reserves — purple
    "oil": "#ff7f00",      # oil platform weather stations — orange
    "other": "#b15928",    # long tail
}
DEFAULT_COLOR = "#7f7f7f"


def rebuild_buoys_map(storage: Storage | None = None) -> Path | None:
    """Stitch the cached catalog (+ any summaries) into a points map.

    Returns the output path or ``None`` if there's no catalog yet.
    """
    s = storage or get_storage()
    try:
        stations = ndbc_download.read_catalog_stations(storage=s)
    except FileNotFoundError:
        logger.info("no NDBC catalog cached — skipping buoys map")
        return None
    except Exception as exc:
        logger.warning("can't read NDBC catalog: %s", exc)
        return None
    if not stations:
        logger.info("empty NDBC catalog — skipping buoys map")
        return None

    summaries = _load_summaries(s)

    features: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    for st in stations:
        stype = (st.get("type") or "").lower()
        type_counts[stype or "other"] = type_counts.get(stype or "other", 0) + 1
        summary_row = summaries.get(st["station_id"])
        props: dict[str, Any] = {
            "station_id": st["station_id"],
            "name": st.get("name") or st["station_id"],
            "type": stype,
            "owner": st.get("owner") or "",
            "lat": st["lat"],
            "lon": st["lon"],
            "met": bool(st.get("met")),
            "currents": bool(st.get("currents")),
            "waterquality": bool(st.get("waterquality")),
            "dart": bool(st.get("dart")),
        }
        if summary_row is not None:
            for k in (
                "year",
                "air_temp_mean",
                "sea_temp_mean",
                "sea_temp_max",
                "wave_height_mean",
                "wave_height_max",
                "high_sst_days",
                "storm_days",
            ):
                if summary_row.get(k) is not None:
                    props[f"latest_{k}"] = summary_row[k]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [st["lon"], st["lat"]],
                },
                "properties": props,
            }
        )

    geojson = {"type": "FeatureCollection", "features": features}
    html = _render_html(geojson, type_counts)

    # Write through the storage abstraction so the map lands in durable storage
    # on any backend (local path or s3://MinIO) — Path/open can't handle s3.
    out_path = s.join(sidecar.cache_dir(NAMESPACE, CATALOG_CACHE_TYPE, s), OUTPUT_RELATIVE_PATH)
    s.write_text_atomic(out_path, html)

    body_bytes = html.encode("utf-8")
    sidecar.write_sidecar(
        NAMESPACE,
        CATALOG_CACHE_TYPE,
        OUTPUT_RELATIVE_PATH,
        kind="file",
        size_bytes=len(body_bytes),
        sha256=hashlib.sha256(body_bytes).hexdigest(),
        tool={"name": "ndbc_map", "version": "1.0"},
        extra={
            "station_count": len(stations),
            "type_counts": type_counts,
            "summaries_used": len(summaries),
        },
        storage=s,
    )
    logger.info(
        "wrote buoys map at %s (%d stations, %d summarised)",
        out_path,
        len(stations),
        len(summaries),
    )
    return out_path


def _load_summaries(storage: Storage) -> dict[str, dict[str, Any]]:
    """Return ``{station_id: latest_yearly_summary}`` for every cached summary."""
    root = Path(sidecar.cache_dir(NAMESPACE, SUMMARY_CACHE_TYPE, storage))
    if not root.is_dir():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for path in root.glob("*.json"):
        if path.name.endswith(".meta.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping %s: %s", path, exc)
            continue
        summaries = payload.get("summaries") or []
        if not summaries:
            continue
        latest = max(summaries, key=lambda r: r.get("year") or 0)
        out[payload.get("station_id") or path.stem] = latest
    return out


def _render_html(
    geojson: dict[str, Any],
    type_counts: dict[str, int],
) -> str:
    title = "NDBC buoys — active station map"
    # Legend: stable order (most common first).
    legend_entries = sorted(
        type_counts.items(), key=lambda kv: (-kv[1], kv[0])
    )
    legend_items = "\n".join(
        f"<li><span class='swatch' style='background:"
        f"{STATION_TYPE_COLORS.get(k, DEFAULT_COLOR)}'></span>"
        f"{html_mod.escape(k or 'unknown')} ({v:,})</li>"
        for k, v in legend_entries
    )

    # MapLibre data-driven color: a ``match`` expression maps each
    # feature's ``type`` property to a colour.
    match_pairs_js = ", ".join(
        f"{json.dumps(k)}, {json.dumps(v)}"
        for k, v in STATION_TYPE_COLORS.items()
    )

    geojson_js = json.dumps(geojson, separators=(",", ":"))

    style = textwrap.dedent(
        """\
        html, body { margin:0; padding:0; height:100%;
                     font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                                  Roboto, sans-serif; }
        #map { position:absolute; top:0; bottom:0; left:0; right:0; }
        header {
          position:absolute; top:10px; left:10px; z-index:10;
          background: rgba(255,255,255,0.96); padding:10px 14px;
          border-radius:6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          max-width:420px; font-size:13px;
        }
        header h1 { margin:0 0 4px; font-size:15px; }
        header p { margin:4px 0; color:#555; }
        header a { color:#1a56db; text-decoration:none; font-weight:600; }
        header a:hover { text-decoration:underline; }
        .legend {
          position:absolute; bottom:20px; left:20px; z-index:10;
          background: rgba(255,255,255,0.96); padding:8px 12px;
          border-radius:6px; box-shadow: 0 2px 6px rgba(0,0,0,0.2);
          font-size:12px;
        }
        .legend h4 { margin:0 0 4px; font-size:12px; color:#555; }
        .legend ul { margin:0; padding:0; list-style:none; }
        .legend li { display:flex; align-items:center; margin:3px 0; }
        .legend .swatch {
          display:inline-block; width:14px; height:14px; margin-right:6px;
          border-radius:50%; border:1px solid rgba(0,0,0,0.2);
        }
        .maplibregl-popup-content { max-width:340px; font-size:12px; }
        .maplibregl-popup-content h4 { margin:0 0 4px; font-size:13px; }
        .maplibregl-popup-content dl { margin:4px 0 0; }
        .maplibregl-popup-content dt { font-weight:600; margin-top:4px; color:#555; }
        .maplibregl-popup-content dd { margin-left:0; margin-bottom:2px; }
        """
    )

    script = textwrap.dedent(
        f"""\
        const GEO = {geojson_js};

        const BASEMAP_TILES = [
          'https://cartodb-basemaps-a.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-b.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-c.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png',
          'https://cartodb-basemaps-d.global.ssl.fastly.net/rastertiles/voyager/{{z}}/{{x}}/{{y}}.png'
        ];
        const BASEMAP_ATTRIB =
          '\\u00a9 <a href=\"https://www.openstreetmap.org/copyright\">OpenStreetMap</a> contributors \\u00a9 <a href=\"https://carto.com/attributions\">CARTO</a> \\u00a9 NOAA NDBC';

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
          center: [-40, 25],
          zoom: 1.5,
          hash: true
        }});

        map.on('load', () => {{
          map.addSource('buoys', {{ type: 'geojson', data: GEO }});
          map.addLayer({{
            id: 'buoys',
            type: 'circle',
            source: 'buoys',
            paint: {{
              'circle-radius': 5,
              'circle-color': [
                'match',
                ['coalesce', ['get', 'type'], 'other'],
                {match_pairs_js},
                {json.dumps(DEFAULT_COLOR)}
              ],
              'circle-stroke-width': 1,
              'circle-stroke-color': '#fff',
              'circle-opacity': 0.92
            }}
          }});

          map.on('click', 'buoys', (e) => {{
            if (!e.features.length) return;
            const p = e.features[0].properties || {{}};
            const esc = (v) => v === undefined || v === null ? '' :
              String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;');
            const rows = [];
            const push = (k, label) => {{
              if (p[k] !== undefined && p[k] !== null && p[k] !== '') {{
                rows.push(`<dt>${{label}}</dt><dd>${{esc(p[k])}}</dd>`);
              }}
            }};
            push('station_id', 'Station ID');
            push('type', 'Type');
            push('owner', 'Owner');
            push('lat', 'Lat');
            push('lon', 'Lon');
            const sensors = ['met','currents','waterquality','dart']
              .filter(k => p[k] === true || p[k] === 'true');
            if (sensors.length) rows.push(`<dt>Sensors</dt><dd>${{sensors.join(', ')}}</dd>`);
            push('latest_year', 'Latest year summary');
            push('latest_air_temp_mean', 'Air temp mean °C');
            push('latest_sea_temp_mean', 'Sea temp mean °C');
            push('latest_sea_temp_max', 'Sea temp max °C');
            push('latest_wave_height_mean', 'Wave height mean m');
            push('latest_wave_height_max', 'Wave height max m');
            push('latest_high_sst_days', 'High-SST days (>28°C)');
            push('latest_storm_days', 'Storm days (wave >4 m)');
            new maplibregl.Popup({{ closeButton: true }})
              .setLngLat(e.lngLat)
              .setHTML(`<h4>${{esc(p.name)}}</h4><dl>${{rows.join('')}}</dl>`)
              .addTo(map);
          }});
          map.on('mouseenter', 'buoys', () => map.getCanvas().style.cursor = 'pointer');
          map.on('mouseleave', 'buoys', () => map.getCanvas().style.cursor = '');
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
          <h1>NDBC buoys — active stations</h1>
          <p>Moored + drifting + coastal observing stations from NOAA's National Data Buoy Center. Each dot is a station; click to see its sensor mix and latest yearly summary (if summarised).</p>
          <p><a href="../climate-report/index.html">&larr; GHCN climate reports</a></p>
        </header>
        <aside class="legend">
          <h4>Station type</h4>
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
