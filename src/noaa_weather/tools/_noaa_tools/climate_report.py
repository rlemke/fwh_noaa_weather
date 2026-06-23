"""Climate-report core — aggregate cached station data → full report bundle.

Consumed by:
- the ``climate-report`` CLI tool (``tools/climate_report.py``), and
- FFL handlers that need to generate the same artifacts in-process.

Both surfaces call :func:`generate_climate_report` so the terminal and
the runtime produce byte-identical outputs (same aggregates, same SVGs,
same HTML, same sidecars).

Output lives at ``cache/noaa-weather/climate-report/<country>/<region>/``
with every file (``report.json``, ``report.md``, ``report.html``, and
five SVGs) paired with a sibling ``.meta.json`` sidecar per the
cache-layout spec.

Standards followed in the output:

- **WMO 30-year climate normals** (1991–2020 default baseline)
- **Walter-Lieth climograph** (monthly temp line + precip bars)
- **Ed Hawkins' warming stripes** (coloured stripe per year)
- **Annual anomaly bars** relative to the baseline
- **Year × month temperature heatmap**
- **OLS trend line** on annual mean temperatures
"""

from __future__ import annotations

import hashlib
import html as html_mod
import json
import logging
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from . import (  # noqa: E402
    climate_analysis,
    climate_charts,
    geofabrik_regions,
    ghcn_download,
    ghcn_parse,
    report_index,
    sidecar,
    warming_map,
    warming_time_map,
)
from .storage import LocalStorage  # noqa: E402

logger = logging.getLogger("noaa-weather.report")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "climate-report"
DEFAULT_BASELINE: tuple[int, int] = (1991, 2020)
DEFAULT_BULK_THRESHOLD = 500

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class ReportError(RuntimeError):
    """Report generation failed (no matches, scale guard, etc.)."""


def rebuild_report_derived_pages(storage: "Storage | None" = None) -> None:
    """Rebuild the master index + warming-rate choropleth.

    Called once at the end of a batch run (when per-report refresh is
    suppressed) and once per single-region run. Failures are logged
    and swallowed so a flaky side effect never sinks a successful
    regional report.
    """
    s = storage or LocalStorage()
    try:
        index_path = report_index.rebuild_index(storage=s)
        if index_path is not None:
            logger.info("master report index: %s", index_path)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("report-index regen failed: %s", exc)
    try:
        map_path = warming_map.rebuild_warming_map(storage=s)
        if map_path is not None:
            logger.info("warming choropleth: %s", map_path)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("warming-map regen failed: %s", exc)
    try:
        point_path, trend_path = warming_time_map.rebuild_time_maps(storage=s)
        if point_path is not None:
            logger.info("point-in-time map: %s", point_path)
        if trend_path is not None:
            logger.info("running-trend map: %s", trend_path)
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("warming-time-map regen failed: %s", exc)


@dataclass
class ReportBundle:
    """Outcome of a successful ``generate_climate_report`` call."""

    output_dir: Path
    report_json_path: Path
    report_md_path: Path
    report_html_path: Path
    chart_paths: dict[str, Path] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    station_count: int = 0
    narrative: str = ""


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------

def generate_climate_report(
    *,
    country: str = "US",
    state: str = "",
    region: str = "",
    start_year: int = 1950,
    end_year: int = 2026,
    baseline: tuple[int, int] = DEFAULT_BASELINE,
    min_years: int = 20,
    required_elements: list[str] | None = None,
    max_stations: int = 0,
    force_catalog: bool = False,
    force_download: bool = False,
    use_mock: bool | None = None,
    output_dir: Path | None = None,
    bulk_threshold: int = DEFAULT_BULK_THRESHOLD,
    override_bulk_guard: bool = False,
    country_explicit: bool = False,
    refresh_index: bool = True,
) -> ReportBundle:
    """Run the full report pipeline and write the bundle to the cache.

    Raises
    ------
    KeyError
        If ``region`` is a Geofabrik path that can't be resolved.
    ReportError
        If the filter resolves to zero stations, or more than
        ``bulk_threshold`` without ``override_bulk_guard=True``, or no
        daily data lands in the chosen year range.
    """
    # --- 1. Resolve the station list from the cache. -----------------------
    country_filter = country
    if region and not country_explicit:
        country_filter = ""

    stations_text = ghcn_download.read_catalog_file(
        "stations", force=force_catalog, use_mock=use_mock
    )
    inventory_text = ghcn_download.read_catalog_file(
        "inventory", force=force_catalog, use_mock=use_mock
    )
    stations = ghcn_parse.parse_stations(stations_text)
    inventory = ghcn_parse.parse_inventory(inventory_text)

    bbox = None
    region_info = None
    if region:
        region_info = geofabrik_regions.resolve_region(region, use_mock=use_mock)
        bbox = region_info.bbox
    elif state and country.upper() != "US":
        # Non-US state: try a Geofabrik sub-region lookup (e.g.
        # CA+ontario → north-america/canada/ontario). ``state`` stays
        # in the filter so the tag propagates into the summaries, but
        # the bbox is what actually filters the station set.
        fallback_bbox, resolved_path = geofabrik_regions.resolve_state_bbox(
            country, state, use_mock=use_mock
        )
        if fallback_bbox is not None:
            bbox = fallback_bbox
            logger.info(
                "resolved --state %r in country %r via Geofabrik path %r",
                state,
                country,
                resolved_path,
            )

    cap = max_stations if max_stations > 0 else len(stations)
    # When we resolved a non-US bbox from a Geofabrik path, we don't want
    # ghcn_parse.station_in_state to re-apply its US-only bbox table on
    # top — it would reject every station since the code isn't in
    # US_STATE_BOUNDS. Drop the state filter in that case; the bbox does
    # the spatial work.
    state_filter = state
    if bbox is not None and state and country.upper() != "US":
        state_filter = ""

    filtered = ghcn_parse.filter_stations(
        stations,
        inventory,
        country=country_filter,
        state=state_filter,
        bbox=bbox,
        max_stations=cap,
        min_years=min_years,
        required_elements=required_elements,
    )

    if not filtered:
        raise ReportError(
            "no stations matched the filter — widen --state / --region / --min-years"
        )
    if len(filtered) > bulk_threshold and not override_bulk_guard:
        raise ReportError(
            f"filter resolved to {len(filtered):,} stations, above the "
            f"{bulk_threshold}-station safety threshold. Narrow the filter "
            f"or pass override_bulk_guard=True."
        )

    region_label = _region_label(country=country, state=state, region_info=region_info)
    logger.info(
        "resolved %d station(s) for report %r (years %d-%d)",
        len(filtered),
        region_label,
        start_year,
        end_year,
    )

    # --- 2. Pull daily data, roll up per-station. --------------------------
    annual_rows: list[dict[str, Any]] = []
    monthly_rows: list[dict[str, Any]] = []
    station_meta: list[dict[str, Any]] = []

    for idx, s in enumerate(filtered, 1):
        sid = s["station_id"]
        logger.info(
            "[%d/%d] %s (%s) — downloading + parsing",
            idx,
            len(filtered),
            sid,
            (s.get("name") or "")[:40],
        )
        extra = {
            "name": s.get("name"),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "elevation": s.get("elevation"),
            "first_year": s.get("first_year"),
            "last_year": s.get("last_year"),
            "elements": s.get("elements"),
        }
        res = ghcn_download.download_station_csv(
            sid,
            force=force_download,
            use_mock=use_mock,
            extra_metadata=extra,
        )
        daily = ghcn_parse.parse_ghcn_csv(res.absolute_path, start_year, end_year)
        if not daily:
            continue
        annual_rows.extend(
            climate_analysis.compute_yearly_summaries(daily, station_id=sid, state=state)
        )
        monthly_rows.extend(
            climate_analysis.compute_monthly_summaries(daily, station_id=sid, state=state)
        )
        station_meta.append(
            {
                "station_id": sid,
                "name": s.get("name"),
                "lat": s.get("lat"),
                "lon": s.get("lon"),
                "elevation": s.get("elevation"),
                "first_year": s.get("first_year"),
                "last_year": s.get("last_year"),
            }
        )

    if not annual_rows:
        raise ReportError("no daily data in year range for any resolved station")

    # --- 3. Regional rollups ----------------------------------------------
    regional_annual = _aggregate_annual(annual_rows, state=state)
    regional_monthly = _aggregate_monthly(monthly_rows, state=state)
    normals = climate_analysis.monthly_climate_normals(
        regional_monthly, baseline_start=baseline[0], baseline_end=baseline[1]
    )
    anomalies = climate_analysis.annual_anomalies(
        regional_annual, baseline_start=baseline[0], baseline_end=baseline[1]
    )
    trend = climate_analysis.aggregate_region_trend(
        annual_rows, state=state, start_year=start_year, end_year=end_year,
    )

    report: dict[str, Any] = {
        "region": {
            "country": country_filter,
            "state": state,
            "path": region_info.path if region_info else None,
            "name": region_info.name if region_info else None,
            "bbox": list(region_info.bbox) if region_info else None,
            "label": region_label,
        },
        "year_range": [start_year, end_year],
        "baseline": list(baseline),
        "station_count": len(station_meta),
        "stations": station_meta,
        "annual": regional_annual,
        "monthly": regional_monthly,
        "monthly_normals": {str(m): v for m, v in normals.items()},
        "anomalies": anomalies,
        "trend": {
            "warming_rate_per_decade": trend["warming_rate_per_decade"],
            "precip_change_pct": trend["precip_change_pct"],
            "snow_change_pct": trend.get("snow_change_pct", 0.0),
            "snow_per_decade_mm": trend.get("snow_per_decade_mm", 0.0),
            "has_snow_data": trend.get("has_snow_data", False),
            "decades": trend["decades"],
            "narrative": trend["narrative"],
        },
    }

    # --- 4. Chart generation ----------------------------------------------
    charts: dict[str, str] = {
        "climograph.svg": climate_charts.climograph(
            normals, region_label=region_label, baseline=baseline
        ),
        "annual_trend.svg": climate_charts.annual_trend(
            regional_annual,
            region_label=region_label,
            slope_per_decade=trend["warming_rate_per_decade"],
        ),
        "warming_stripes.svg": climate_charts.warming_stripes(
            regional_annual, region_label=region_label
        ),
        "heatmap.svg": climate_charts.year_month_heatmap(
            regional_monthly, region_label=region_label, value_field="temp_mean"
        ),
        "anomaly_bars.svg": climate_charts.anomaly_bars(
            anomalies, region_label=region_label, baseline=baseline
        ),
    }

    # --- 5. Markdown + HTML -----------------------------------------------
    md = _render_markdown(report, list(charts.keys()))
    html = _render_html(report, charts, md)

    # --- 6. Write bundle to disk + sidecars ------------------------------
    country_dir = country or "ALL"
    region_dir = _region_key(state=state, region_info=region_info)
    out_dir = _resolve_output_dir(
        output_dir=output_dir, country_dir=country_dir, region_dir=region_dir
    )

    json_path, md_path, html_path, chart_paths = _write_outputs(
        out_dir=out_dir,
        country_dir=country_dir,
        region_dir=region_dir,
        report=report,
        md=md,
        html=html,
        charts=charts,
    )

    # Refresh the master index + warming-rate choropleth so every new
    # report shows up in both views. Failures here mustn't sink the
    # whole report — worst case the derived pages are one run stale.
    # Batch callers pass refresh_index=False to skip this per-region;
    # they invoke rebuild_report_derived_pages() once at the end.
    if refresh_index:
        rebuild_report_derived_pages(storage=LocalStorage())

    return ReportBundle(
        output_dir=out_dir,
        report_json_path=json_path,
        report_md_path=md_path,
        report_html_path=html_path,
        chart_paths=chart_paths,
        report=report,
        station_count=len(station_meta),
        narrative=trend["narrative"],
    )


# ---------------------------------------------------------------------------
# Aggregation helpers (per-station rows → per-year / per-month regional).
# ---------------------------------------------------------------------------

def _aggregate_annual(
    per_station_yearly: list[dict[str, Any]], *, state: str
) -> list[dict[str, Any]]:
    by_year: dict[int, list[dict[str, Any]]] = {}
    for r in per_station_yearly:
        y = r.get("year")
        if isinstance(y, int):
            by_year.setdefault(y, []).append(r)
    out: list[dict[str, Any]] = []
    for y in sorted(by_year):
        recs = by_year[y]
        temps = [r["temp_mean"] for r in recs if r.get("temp_mean") is not None]
        precips = [r["precip_annual"] for r in recs if r.get("precip_annual") is not None]
        snows = [r["snow_annual"] for r in recs if r.get("snow_annual") is not None]
        hot = sum(r.get("hot_days", 0) or 0 for r in recs)
        frost = sum(r.get("frost_days", 0) or 0 for r in recs)
        if not temps:
            continue
        mins = [r["temp_min_avg"] for r in recs if r.get("temp_min_avg") is not None]
        maxs = [r["temp_max_avg"] for r in recs if r.get("temp_max_avg") is not None]
        out.append(
            {
                "state": state,
                "year": y,
                "station_count": len(recs),
                "temp_mean": round(sum(temps) / len(temps), 2),
                "temp_min_avg": round(sum(mins) / len(mins), 2) if mins else None,
                "temp_max_avg": round(sum(maxs) / len(maxs), 2) if maxs else None,
                "precip_annual": round(sum(precips) / len(precips), 1) if precips else 0.0,
                "snow_annual": round(sum(snows) / len(snows), 1) if snows else None,
                "hot_days": hot,
                "frost_days": frost,
            }
        )
    return out


def _aggregate_monthly(
    per_station_monthly: list[dict[str, Any]], *, state: str
) -> list[dict[str, Any]]:
    by_ym: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for r in per_station_monthly:
        y = r.get("year")
        m = r.get("month")
        if isinstance(y, int) and isinstance(m, int):
            by_ym.setdefault((y, m), []).append(r)

    out: list[dict[str, Any]] = []
    for (y, m) in sorted(by_ym):
        recs = by_ym[(y, m)]
        temps = [r["temp_mean"] for r in recs if r.get("temp_mean") is not None]
        mins = [r["temp_min_avg"] for r in recs if r.get("temp_min_avg") is not None]
        maxs = [r["temp_max_avg"] for r in recs if r.get("temp_max_avg") is not None]
        precs = [r["precip_total"] for r in recs if r.get("precip_total") is not None]
        if not temps:
            continue
        out.append(
            {
                "state": state,
                "year": y,
                "month": m,
                "station_count": len(recs),
                "temp_mean": round(sum(temps) / len(temps), 2),
                "temp_min_avg": round(sum(mins) / len(mins), 2) if mins else None,
                "temp_max_avg": round(sum(maxs) / len(maxs), 2) if maxs else None,
                "precip_total": round(sum(precs) / len(precs), 1) if precs else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Markdown + HTML renderers.
# ---------------------------------------------------------------------------

def _render_markdown(report: dict[str, Any], chart_files: list[str]) -> str:
    region = report["region"]["label"]
    yrs = report["year_range"]
    base = report["baseline"]
    trend = report["trend"]
    annual = report["annual"]
    normals = report["monthly_normals"]

    lines: list[str] = []
    lines.append(f"# Climate report — {region}")
    lines.append("")
    lines.append(f"- **Year range**: {yrs[0]}–{yrs[1]}")
    lines.append(
        f"- **Baseline (climate normals)**: {base[0]}–{base[1]} (WMO 30-year standard)"
    )
    lines.append(f"- **Stations contributing**: {report['station_count']}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(trend["narrative"])
    lines.append("")
    lines.append(f"- **Warming rate**: {trend['warming_rate_per_decade']:+.2f} °C / decade")
    lines.append(
        f"- **Precipitation change** (first vs last year in range): "
        f"{trend['precip_change_pct']:+.1f} %"
    )
    if trend.get("has_snow_data"):
        lines.append(
            f"- **Snowfall change**: {trend['snow_per_decade_mm']:+.1f} mm / decade "
            f"({trend['snow_change_pct']:+.1f} %)"
        )
    lines.append("")
    lines.append("## Charts")
    lines.append("")
    for f in chart_files:
        lines.append(f"- [{f}]({f})")
    lines.append("")
    lines.append("## Monthly climate normals")
    lines.append("")
    lines.append("| Month | Mean °C | Min °C | Max °C | Precip (mm) | Years |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for m in range(1, 13):
        n = normals[str(m)]
        lines.append(
            f"| {MONTH_ABBR[m - 1]} "
            f"| {_fmt(n['temp_mean'])} "
            f"| {_fmt(n['temp_min_avg'])} "
            f"| {_fmt(n['temp_max_avg'])} "
            f"| {_fmt(n['precip_total'])} "
            f"| {n['years_counted']} |"
        )
    lines.append("")
    lines.append("## Decadal comparison")
    lines.append("")
    lines.append("| Decade | Avg temp °C | Avg precip (mm) | Avg snow (mm) | Years w/ data |")
    lines.append("|---|---:|---:|---:|---:|")
    for dec, vals in sorted(trend["decades"].items()):
        lines.append(
            f"| {dec} | {vals['avg_temp']} | {vals['avg_precip']} "
            f"| {_fmt(vals.get('avg_snow'))} | {vals['years_with_data']} |"
        )
    lines.append("")
    lines.append("## Annual time series")
    lines.append("")
    lines.append(
        "| Year | Mean °C | Min °C | Max °C | Precip (mm) | Snow (mm) | Hot days | Frost days | Stations |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in annual:
        lines.append(
            f"| {r['year']} "
            f"| {_fmt(r.get('temp_mean'))} "
            f"| {_fmt(r.get('temp_min_avg'))} "
            f"| {_fmt(r.get('temp_max_avg'))} "
            f"| {_fmt(r.get('precip_annual'))} "
            f"| {_fmt(r.get('snow_annual'))} "
            f"| {r.get('hot_days', 0)} "
            f"| {r.get('frost_days', 0)} "
            f"| {r.get('station_count', 0)} |"
        )
    lines.append("")
    lines.append("## Stations contributing")
    lines.append("")
    lines.append("| Station ID | Name | Lat | Lon | Elev m | Inv. years |")
    lines.append("|---|---|---:|---:|---:|---|")
    for s in report["stations"]:
        lines.append(
            f"| {s['station_id']} | {s.get('name') or ''} "
            f"| {_fmt(s.get('lat'))} | {_fmt(s.get('lon'))} "
            f"| {_fmt(s.get('elevation'))} "
            f"| {s.get('first_year')}-{s.get('last_year')} |"
        )
    lines.append("")
    return "\n".join(lines)


def _render_html(
    report: dict[str, Any],
    charts: dict[str, str],
    markdown_text: str,
) -> str:
    region = report["region"]["label"]
    # Strip SVG XML prologs so each SVG nests cleanly inside HTML.
    embedded: list[tuple[str, str]] = []
    for name, svg in charts.items():
        body = svg
        if "<svg" in body:
            body = body[body.index("<svg"):]
        embedded.append((name, body))

    md_html = _markdown_to_html(markdown_text)

    style = textwrap.dedent(
        """
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
               max-width: 960px; margin: 2em auto; padding: 0 1em; color: #222; }
        h1 { border-bottom: 2px solid #333; padding-bottom: 0.2em; }
        h2 { border-bottom: 1px solid #bbb; padding-bottom: 0.15em; margin-top: 2em; }
        table { border-collapse: collapse; margin: 0.8em 0; }
        th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left; }
        th { background: #f3f3f3; }
        td.num { text-align: right; font-variant-numeric: tabular-nums; }
        .chart { margin: 1.2em 0; }
        .chart figcaption { font-size: 0.9em; color: #555; margin-top: 0.3em; }
        code { background: #f5f5f5; padding: 1px 4px; border-radius: 3px; }
        """
    ).strip()

    chart_block_parts = []
    for name, svg in embedded:
        caption = html_mod.escape(name)
        chart_block_parts.append(
            f'<figure class="chart" id="{html_mod.escape(name)}">'
            f'{svg}'
            f'<figcaption>{caption}</figcaption>'
            f'</figure>'
        )
    chart_block = "\n".join(chart_block_parts)

    title = html_mod.escape(f"Climate report — {region}")
    return textwrap.dedent(
        f"""\
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>{title}</title>
          <style>{style}</style>
        </head>
        <body>
        {md_html}
        <h2 id="embedded-charts">Embedded charts</h2>
        {chart_block}
        </body>
        </html>
        """
    )


def _markdown_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue

        if stripped.startswith("# "):
            out.append(f"<h1>{_inline(stripped[2:])}</h1>")
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(f"<h2>{_inline(stripped[3:])}</h2>")
            i += 1
            continue

        if stripped.startswith("- "):
            out.append("<ul>")
            while i < len(lines) and lines[i].strip().startswith("- "):
                out.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("</ul>")
            continue

        if stripped.startswith("|") and i + 1 < len(lines) and "---" in lines[i + 1]:
            header_cells = [c.strip() for c in stripped.strip("|").split("|")]
            sep_cells = lines[i + 1].strip().strip("|").split("|")
            aligns = ["right" if "---:" in c else "left" for c in sep_cells]
            out.append("<table>")
            out.append("<thead><tr>")
            for c in header_cells:
                out.append(f"<th>{_inline(c)}</th>")
            out.append("</tr></thead>")
            out.append("<tbody>")
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                out.append("<tr>")
                for j, c in enumerate(cells):
                    klass = ' class="num"' if j < len(aligns) and aligns[j] == "right" else ""
                    out.append(f"<td{klass}>{_inline(c)}</td>")
                out.append("</tr>")
                i += 1
            out.append("</tbody></table>")
            continue

        buf = [stripped]
        i += 1
        while (
            i < len(lines)
            and lines[i].strip()
            and not lines[i].strip().startswith(("#", "-", "|"))
        ):
            buf.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_inline(' '.join(buf))}</p>")
    return "\n".join(out)


def _inline(text: str) -> str:
    escaped = html_mod.escape(text)
    out: list[str] = []
    in_b = False
    buf = ""
    i = 0
    while i < len(escaped):
        if escaped[i : i + 2] == "**":
            if buf:
                out.append(buf)
                buf = ""
            out.append("</strong>" if in_b else "<strong>")
            in_b = not in_b
            i += 2
        else:
            buf += escaped[i]
            i += 1
    if buf:
        out.append(buf)
    return "".join(out)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


# ---------------------------------------------------------------------------
# Output path + write helpers.
# ---------------------------------------------------------------------------

def _region_label(*, country: str, state: str, region_info) -> str:
    if region_info is not None:
        return region_info.name
    if state:
        return f"{country}/{state}"
    return country or "ALL"


def _region_key(*, state: str, region_info) -> str:
    if region_info is not None:
        return region_info.path.replace("/", "__")
    return state or "ALL"


def _resolve_output_dir(
    *,
    output_dir: Path | None,
    country_dir: str,
    region_dir: str,
) -> Path:
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir
    relative_dir = f"{country_dir}/{region_dir}"
    abs_dir = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_dir, LocalStorage())
    Path(abs_dir).mkdir(parents=True, exist_ok=True)
    return Path(abs_dir)


def _write_outputs(
    *,
    out_dir: Path,
    country_dir: str,
    region_dir: str,
    report: dict[str, Any],
    md: str,
    html: str,
    charts: dict[str, str],
) -> tuple[Path, Path, Path, dict[str, Path]]:
    storage = LocalStorage()

    trend = report.get("trend") or {}
    trend_summary = {
        "warming_rate_per_decade": trend.get("warming_rate_per_decade"),
        "precip_change_pct": trend.get("precip_change_pct"),
        "narrative": trend.get("narrative"),
    }

    def _write(name: str, text: str, content_kind: str) -> Path:
        file_path = out_dir / name
        file_path.write_text(text, encoding="utf-8")
        body_bytes = text.encode("utf-8")
        relative_path = f"{country_dir}/{region_dir}/{name}"
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body_bytes),
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            tool={"name": "climate_report", "version": "1.0"},
            extra={
                "content_kind": content_kind,
                "region": report["region"],
                "year_range": report["year_range"],
                "baseline": report["baseline"],
                "station_count": report["station_count"],
                "trend": trend_summary,
            },
            storage=storage,
        )
        return file_path

    json_path = _write(
        "report.json",
        json.dumps(report, indent=2, sort_keys=True, default=_json_default) + "\n",
        "json",
    )
    md_path = _write("report.md", md + ("\n" if not md.endswith("\n") else ""), "markdown")
    html_path = _write("report.html", html, "html")
    chart_paths: dict[str, Path] = {}
    for chart_name, svg in charts.items():
        chart_paths[chart_name] = _write(chart_name, svg, "svg")

    return json_path, md_path, html_path, chart_paths


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    raise TypeError(f"not JSON-serializable: {type(obj).__name__}")
