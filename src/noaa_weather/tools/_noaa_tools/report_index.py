"""Master index of every cached climate-report bundle.

Walks ``cache/noaa-weather/climate-report/<country>/<region>/report.html``
entries (via their sidecars) and renders a single ``index.html`` at
``cache/noaa-weather/climate-report/index.html`` that lists every
report grouped continent → country → sub-region, with links to the
HTML, Markdown, JSON, and each SVG chart.

Called as the last step of
:func:`_noaa_tools.climate_report.generate_climate_report` so a fresh
regional run always refreshes the master index.
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

from . import sidecar  # noqa: E402
from .storage import Storage, get_storage  # noqa: E402

logger = logging.getLogger("noaa-weather.report-index")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "climate-report"
INDEX_RELATIVE_PATH = "index.html"

# ---------------------------------------------------------------------------
# FIPS country code → continent + display name.
# ---------------------------------------------------------------------------
#
# Covers the countries the existing FFL workflows target (AnalyzeCanada,
# AnalyzeEurope, AnalyzeSouthAmerica, AnalyzeAfrica, AnalyzeAsia,
# AnalyzeArctic, CacheAntarcticaData, etc.) plus the headline entries
# elsewhere. Unknown codes fall through to the "Other" bucket.

FIPS_COUNTRIES: dict[str, tuple[str, str]] = {
    # North America
    "US": ("North America", "United States"),
    "CA": ("North America", "Canada"),
    "MX": ("North America", "Mexico"),
    "GT": ("North America", "Guatemala"),
    "HO": ("North America", "Honduras"),
    "NU": ("North America", "Nicaragua"),
    "CS": ("North America", "Costa Rica"),
    "PM": ("North America", "Panama"),
    "RQ": ("North America", "Puerto Rico"),
    "BH": ("North America", "Belize"),
    "ES": ("North America", "El Salvador"),
    # South America
    "BR": ("South America", "Brazil"),
    "AR": ("South America", "Argentina"),
    "CI": ("South America", "Chile"),
    "CO": ("South America", "Colombia"),
    "PE": ("South America", "Peru"),
    "VE": ("South America", "Venezuela"),
    "EC": ("South America", "Ecuador"),
    "BO": ("South America", "Bolivia"),
    "PY": ("South America", "Paraguay"),
    "UY": ("South America", "Uruguay"),
    # Europe
    "UK": ("Europe", "United Kingdom"),
    "GM": ("Europe", "Germany"),
    "FR": ("Europe", "France"),
    "SP": ("Europe", "Spain"),
    "IT": ("Europe", "Italy"),
    "NO": ("Europe", "Norway"),
    "SW": ("Europe", "Sweden"),
    "FI": ("Europe", "Finland"),
    "PL": ("Europe", "Poland"),
    "EZ": ("Europe", "Czech Republic"),
    "NL": ("Europe", "Netherlands"),
    "BE": ("Europe", "Belgium"),
    "DA": ("Europe", "Denmark"),
    "SZ": ("Europe", "Switzerland"),
    "PO": ("Europe", "Portugal"),
    "EI": ("Europe", "Ireland"),
    "GR": ("Europe", "Greece"),
    "IC": ("Europe", "Iceland"),
    "AU": ("Europe", "Austria"),
    "HU": ("Europe", "Hungary"),
    "RO": ("Europe", "Romania"),
    # Asia — RS (Russia) is geographically split; we bucket under Asia.
    "CH": ("Asia", "China"),
    "JA": ("Asia", "Japan"),
    "KS": ("Asia", "South Korea"),
    "TH": ("Asia", "Thailand"),
    "VM": ("Asia", "Vietnam"),
    "BM": ("Asia", "Myanmar"),
    "ID": ("Asia", "Indonesia"),
    "IN": ("Asia", "India"),
    "RS": ("Asia", "Russia"),
    "IR": ("Asia", "Iran"),
    "TU": ("Asia", "Turkey"),
    # Africa
    "SF": ("Africa", "South Africa"),
    "NI": ("Africa", "Niger"),
    "KE": ("Africa", "Kenya"),
    "EG": ("Africa", "Egypt"),
    "MO": ("Africa", "Morocco"),
    "AG": ("Africa", "Algeria"),
    "LY": ("Africa", "Libya"),
    "IV": ("Africa", "Côte d'Ivoire"),
    "GH": ("Africa", "Ghana"),
    "SN": ("Africa", "Senegal"),
    "TZ": ("Africa", "Tanzania"),
    "UG": ("Africa", "Uganda"),
    "ET": ("Africa", "Ethiopia"),
    "NG": ("Africa", "Nigeria"),
    # Oceania
    "AS": ("Oceania", "Australia"),
    "NZ": ("Oceania", "New Zealand"),
    "FJ": ("Oceania", "Fiji"),
    # Arctic / Antarctic
    "GL": ("Arctic", "Greenland"),
    "SV": ("Arctic", "Svalbard"),
    "AY": ("Antarctica", "Antarctica"),
}

OTHER_CONTINENT = "Other"


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def rebuild_index(
    storage: Storage | None = None,
) -> Path | None:
    """Walk every cached climate-report bundle and rewrite the master index.

    Returns the path to the written ``index.html``, or ``None`` if no
    reports were found (no file is written in that case to avoid
    littering the cache with empty stubs).
    """
    s = storage or get_storage()
    reports = _discover_reports(s)
    if not reports:
        logger.info("no reports found — skipping index regen")
        return None

    tree = _build_tree(reports)
    html = _render_index(tree, total=len(reports))

    # Write through the storage abstraction (local path or s3://MinIO).
    out_path = s.join(sidecar.cache_dir(NAMESPACE, CACHE_TYPE, s), INDEX_RELATIVE_PATH)
    body_bytes = html.encode("utf-8")
    s.write_text_atomic(out_path, html)

    sidecar.write_sidecar(
        NAMESPACE,
        CACHE_TYPE,
        INDEX_RELATIVE_PATH,
        kind="file",
        size_bytes=len(body_bytes),
        sha256=hashlib.sha256(body_bytes).hexdigest(),
        tool={"name": "report_index", "version": "1.0"},
        extra={
            "report_count": len(reports),
            "continents": sorted(tree.keys()),
        },
        storage=s,
    )
    logger.info("wrote master report index at %s (%d reports)", out_path, len(reports))
    return out_path


# ---------------------------------------------------------------------------
# Discovery.
# ---------------------------------------------------------------------------

def _discover_reports(storage: Storage) -> list[dict[str, Any]]:
    """Find every report bundle under the climate-report cache.

    Storage-aware: enumerates sidecars via ``sidecar.list_entries`` (works on
    local disk AND s3/MinIO), keying off each bundle's ``report.html`` sidecar
    for the region block, and grouping the bundle's other artifacts for the
    index's per-file links. Hrefs are each artifact's ``relative_path`` — already
    relative to the climate-report root where ``index.html`` sits.
    """
    # Group every artifact by its bundle dir (relative_path's parent).
    bundles: dict[str, dict[str, Any]] = {}
    for data in sidecar.list_entries(NAMESPACE, CACHE_TYPE, storage):
        rel = data.get("relative_path", "")
        if "/" not in rel:
            continue  # top-level derived pages (index.html, maps) aren't bundles
        bundle_rel, name = rel.rsplit("/", 1)
        b = bundles.setdefault(bundle_rel, {"files": {}, "html": None})
        b["files"][name] = rel
        if name == "report.html":
            b["html"] = data

    out: list[dict[str, Any]] = []
    for bundle_rel, b in sorted(bundles.items()):
        data = b["html"]
        if data is None:
            continue  # a dir without a report.html sidecar isn't a report bundle
        extra = data.get("extra") or {}
        trend = extra.get("trend") or {}
        out.append(
            {
                "relative_dir": bundle_rel,
                "region": extra.get("region") or {},
                "year_range": extra.get("year_range") or [None, None],
                "baseline": extra.get("baseline") or [None, None],
                "station_count": extra.get("station_count", 0),
                "warming_rate_per_decade": trend.get("warming_rate_per_decade"),
                "precip_change_pct": trend.get("precip_change_pct"),
                "generated_at": data.get("generated_at", ""),
                "files": b["files"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Grouping.
# ---------------------------------------------------------------------------

def _build_tree(
    reports: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group reports into continent → country → [entry, …]."""
    tree: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for r in reports:
        continent, country_name, sub_label = _classify(r)
        tree.setdefault(continent, {}).setdefault(country_name, []).append(
            {**r, "_sub_label": sub_label}
        )
    # Sort countries and entries stable.
    for continent, countries in tree.items():
        for country, entries in countries.items():
            entries.sort(key=lambda e: e["_sub_label"].lower())
    return tree


def _classify(report: dict[str, Any]) -> tuple[str, str, str]:
    """Map a report to (continent, country_display, sub_region_label)."""
    region = report["region"] or {}
    path = region.get("path") or ""
    country = region.get("country") or ""
    state = region.get("state") or ""
    region_name = region.get("name") or ""

    # Geofabrik path wins when present — it's explicit about continent.
    if path:
        parts = [p for p in path.split("/") if p]
        if parts:
            continent_guess = _geofabrik_continent(parts[0])
            if len(parts) == 1:
                # Continent-only path.
                return continent_guess, region_name or parts[0].replace("-", " ").title(), "ALL"
            country_slug = parts[1]
            country_display = _country_display(country, country_slug, region_name)
            if len(parts) == 2:
                return continent_guess, country_display, region_name or "ALL"
            sub = "/".join(parts[2:]).replace("-", " ").title()
            return continent_guess, country_display, region_name or sub

    # Fall back to FIPS country code + state.
    continent, country_display = FIPS_COUNTRIES.get(
        country, (OTHER_CONTINENT, country or "Unknown")
    )
    sub_label = state or "ALL"
    return continent, country_display, sub_label


def _geofabrik_continent(slug: str) -> str:
    """Geofabrik's top-level region slugs → our continent names."""
    return {
        "africa": "Africa",
        "antarctica": "Antarctica",
        "asia": "Asia",
        "australia-oceania": "Oceania",
        "central-america": "North America",
        "europe": "Europe",
        "north-america": "North America",
        "south-america": "South America",
        "russia": "Asia",
    }.get(slug, OTHER_CONTINENT)


def _country_display(fips: str, geofabrik_slug: str, region_name: str) -> str:
    """Best-effort country display name: FIPS map > region_name > slug."""
    if fips and fips in FIPS_COUNTRIES:
        return FIPS_COUNTRIES[fips][1]
    if region_name:
        return region_name
    return geofabrik_slug.replace("-", " ").title() or "Unknown"


# ---------------------------------------------------------------------------
# HTML rendering.
# ---------------------------------------------------------------------------

def _render_index(
    tree: dict[str, dict[str, list[dict[str, Any]]]],
    *,
    total: int,
) -> str:
    style = textwrap.dedent(
        """\
        :root { color-scheme: light dark; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               max-width: 1100px; margin: 2em auto; padding: 0 1.2em; color: #222; }
        header { display: flex; align-items: baseline; gap: 1em; border-bottom: 2px solid #333;
                 padding-bottom: 0.5em; margin-bottom: 1em; }
        header h1 { margin: 0; }
        header .count { color: #666; font-size: 0.95em; }
        h2.continent { margin-top: 1.6em; border-bottom: 1px solid #999; padding-bottom: 0.2em; }
        h3.country { margin-top: 1em; color: #444; font-size: 1.05em; }
        table { border-collapse: collapse; width: 100%; margin-top: 0.4em; }
        th, td { text-align: left; padding: 5px 10px; border-bottom: 1px solid #eee;
                 vertical-align: top; }
        th { background: #f7f7f7; font-weight: 600; font-size: 0.9em; color: #555; }
        td.num { text-align: right; font-variant-numeric: tabular-nums; }
        td.region a { font-weight: 600; color: #1a56db; text-decoration: none; }
        td.region a:hover { text-decoration: underline; }
        .aux { margin-top: 3px; font-size: 0.85em; color: #666; }
        .aux a { margin-right: 8px; color: #1a56db; text-decoration: none; }
        .aux a:hover { text-decoration: underline; }
        .aux a.svg { color: #6a737d; }
        footer { margin-top: 2em; color: #888; font-size: 0.85em; text-align: center; }
        """
    ).strip()

    # Continent order: put the most-populated continents first so users
    # don't have to scroll for common cases, and "Other" always at the end.
    continent_order = [
        "North America", "Europe", "Asia",
        "South America", "Africa", "Oceania",
        "Arctic", "Antarctica",
    ]
    ordered_continents = [c for c in continent_order if c in tree]
    ordered_continents += [c for c in tree if c not in continent_order and c != OTHER_CONTINENT]
    if OTHER_CONTINENT in tree:
        ordered_continents.append(OTHER_CONTINENT)

    parts: list[str] = []
    parts.append("<!doctype html><html lang='en'><head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<title>NOAA Weather — Climate Reports Index</title>")
    parts.append(f"<style>{style}</style>")
    parts.append("</head><body>")
    parts.append("<header>")
    parts.append("<h1>NOAA Climate Reports</h1>")
    parts.append(
        f"<span class='count'>{total:,} report"
        f"{'s' if total != 1 else ''} across "
        f"{len(ordered_continents)} continent"
        f"{'s' if len(ordered_continents) != 1 else ''} · "
        f"<a href='warming-map.html'>Warming map</a> · "
        f"<a href='warming-point-map.html'>Point-in-time</a> · "
        f"<a href='warming-trend-map.html'>Running trend</a></span>"
    )
    parts.append("</header>")

    for continent in ordered_continents:
        countries = tree[continent]
        parts.append(f"<h2 class='continent'>{html_mod.escape(continent)}</h2>")
        for country_name in sorted(countries):
            entries = countries[country_name]
            parts.append(f"<h3 class='country'>{html_mod.escape(country_name)}</h3>")
            parts.append("<table>")
            parts.append(
                "<thead><tr>"
                "<th>Region</th><th>Years</th><th class='num'>Stations</th>"
                "<th>Warming</th><th>Updated</th>"
                "</tr></thead><tbody>"
            )
            for entry in entries:
                parts.append(_render_row(entry))
            parts.append("</tbody></table>")

    parts.append(
        "<footer>"
        "Master index regenerated on every climate-report run. "
        "See <a href='../../../agent-spec/tools-pattern.agent-spec.yaml'>"
        "tools-pattern spec</a> for the underlying cache contract."
        "</footer>"
    )
    parts.append("</body></html>")
    return "\n".join(parts)


def _render_row(entry: dict[str, Any]) -> str:
    sub = entry.get("_sub_label") or "ALL"
    year_range = entry.get("year_range") or [None, None]
    years = f"{year_range[0]}–{year_range[1]}" if all(year_range) else "—"
    station_count = entry.get("station_count") or 0
    generated_at = entry.get("generated_at") or ""
    generated_short = generated_at[:10] if generated_at else ""

    files = entry.get("files") or {}
    html_href = files.get("report.html", "")
    region_cell = (
        f"<a href='{html_mod.escape(html_href)}'>{html_mod.escape(str(sub))}</a>"
        if html_href
        else html_mod.escape(str(sub))
    )

    # Aux links: md, json, each svg.
    aux_parts: list[str] = []
    for name, href in sorted(files.items()):
        if name == "report.html":
            continue
        label = name
        css = "svg" if name.endswith(".svg") else ""
        aux_parts.append(
            f"<a class='{css}' href='{html_mod.escape(href)}'>{html_mod.escape(label)}</a>"
        )
    aux = f"<div class='aux'>{' '.join(aux_parts)}</div>" if aux_parts else ""

    warming = entry.get("warming_rate_per_decade")
    if isinstance(warming, (int, float)):
        warming_cell = f"{warming:+.2f} °C/dec"
    else:
        warming_cell = "—"

    return (
        "<tr>"
        f"<td class='region'>{region_cell}{aux}</td>"
        f"<td>{html_mod.escape(years)}</td>"
        f"<td class='num'>{station_count:,}</td>"
        f"<td>{warming_cell}</td>"
        f"<td>{html_mod.escape(generated_short)}</td>"
        "</tr>"
    )
