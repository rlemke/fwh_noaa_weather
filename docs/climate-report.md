# Regional Climate Report Bundle

**Namespace:** `weather.Report` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Report`) ·
**Handler:** `src/noaa_weather/handlers/report/report_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/climate_report.py`,
`climate_charts.py`, `warming_map.py`, `warming_time_map.py`,
`report_index.py` · **CLI:** `tools/climate-report.sh`

## Overview

This feature produces the human-facing deliverable: a **self-contained regional
climate report** — JSON aggregates, a Markdown narrative, a standalone HTML page, and
five SVG charts — for a country+state or a Geofabrik region. It wraps the
`climate_report.generate_climate_report` core that the `climate-report.sh` CLI also
calls, so the terminal and the runtime produce byte-identical bundles. The handler
for `GenerateClimateReport` re-runs discovery + download internally, so the report is
always self-contained regardless of what's cached.

## How it works

- **`GenerateClimateReport`** → `climate_report.generate_climate_report(...)`.
  Internally it discovers stations (country+state or region bbox), reads/downloads
  their CSVs, aggregates to annual/monthly climate stats against a WMO baseline, and
  emits the bundle. The handler adopts the same "default `US` = not-explicit when a
  region is set" rule as discovery (`country_explicit`), and translates
  `ReportError` / `KeyError` into a failed step.
- **`ListRegionsUnder`** → `geofabrik_regions.list_regions_under(prefix,
  include_parents)`. Returns a JSON array of Geofabrik region paths so
  `GenerateRegionGroupReport` can `andThen foreach` a "report on every province"
  workflow. `include_parents=false` (default) → leaves only.

Data shape: `region selector → discovered stations → cached CSVs → annual/monthly
aggregates → report.json + report.md + report.html + 5 SVGs`.

## Fan-out

Both facets are single-task. Fan-out is in the workflows:
`GenerateRegionGroupReport` uses `ListRegionsUnder` + `andThen foreach region` →
`GenerateClimateReport`, one report per Geofabrik sub-region (Canada + every province,
Europe + every country, …), with per-region failures caught so one bad region doesn't
poison the batch. See [workflows](workflows.md).

## Data & fields

- **Standards baked into the output:** WMO 30-year normals (1991–2020 default
  baseline, `baseline_start`/`baseline_end`), a Walter-Lieth climograph, Ed Hawkins
  warming stripes, annual anomaly bars, a year×month temperature heatmap, and an OLS
  trend line.
- **Region selection:** `region` (Geofabrik path) OR `country`+`state` — the same
  vocabulary as `DiscoverStations`.
- `chart_paths` is a JSON map of the five SVG names (climograph, annual_trend,
  warming_stripes, heatmap, anomaly_bars) → paths. Returns the
  `weather.types.ClimateReportBundle` fields (output_dir, report_json/md/html,
  chart_paths, station_count, narrative).
- A **bulk guard** (`DEFAULT_BULK_THRESHOLD = 500`) blocks accidental
  huge-region runs unless `override_bulk_guard=true`.

## External libraries / binaries

- **stdlib-only charts.** `climate_charts.py` / `warming_map.py` /
  `warming_time_map.py` emit **raw SVG** (and a MapLibre/SVG warming-rate choropleth
  for the master index) — **no matplotlib** (not installed in the runners), which is
  why the full report now runs inside the fleet.
- **`requests`** (pip, optional) — indirectly, via discovery/download; mock fallback
  applies.
- No binary deps.

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `GenerateClimateReport` | event | external / **expensive** | Full region bundle (JSON + MD + HTML + 5 SVGs) |
| `ListRegionsUnder` | event | io / cheap | Enumerate Geofabrik sub-regions under a prefix |

Workflows: `GenerateRegionReport` (country+state), `GenerateGeofabrikRegionReport`
(region path), `GenerateRegionGroupReport` (every sub-region under a prefix).

## Cache / output

- Bundle at `cache/noaa-weather/climate-report/<country>/<region>/`: `report.json`,
  `report.md`, `report.html`, and five SVGs — each with a `.meta.json` sidecar. A
  master index page + warming-rate choropleth are rebuilt via
  `report_index.py` / `warming_map.py`.
- Written through the storage abstraction; under `FW_STORAGE=s3` the bundle lands in
  MinIO. (Per repo notes, the report handler path is s3-clean; the standalone
  `climate_report.py` CLI still has one `LocalStorage()` dev-tool holdout.)

## Gotchas & notes

- **The report re-does discovery + download internally** — you don't need to run
  `DiscoverStations`/`AnalyzeStationMonthly` first, though doing so warms the CSV
  cache so the report reads rather than re-downloads.
- **`prefix=""` in `ListRegionsUnder` returns every Geofabrik region** (tens of
  thousands) — a footgun; always scope the prefix.
- **`GenerateClimateReport` is expensive-tier** — a large region × long window fans
  out many downloads; mind the bulk guard.

## Related specs

- [catalog-discovery](catalog-discovery.md) / [ingest](ingest.md) — the discovery +
  download the report re-runs internally.
- [trends](trends.md) — `AnalyzeStationMonthly` feeds the same aggregates; the report
  is the rendered form of the trend story.
- [workflows](workflows.md) — the group/region report fan-outs.
- [storage-and-cache](storage-and-cache.md) — the `climate-report` bundle write path.
