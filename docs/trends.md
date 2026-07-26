# Climate Analysis & Regional Trends

**Namespace:** `weather.Analysis` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Analysis`) ·
**Handler:** `src/noaa_weather/handlers/analysis/analysis_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/climate_analysis.py` ·
**Persistence:** `WeatherReportStore` / `ClimateStore` in
`handlers/shared/ghcn_utils.py` · **CLI:** `tools/compute-region-trend.sh`

## Overview

This is the **flagship** feature — the "climate trends" the repo is named for. It
turns raw daily station records into per-year climate summaries, then aggregates
many stations into a **regional multi-decade trend**: warming rate (°C/decade),
precipitation change, and snowfall change, with a plain-English narrative. It is the
canonical example of the package's *per-station → persist → region-readback*
pattern, which QC and extremes both copy: each station's analysis is written to
MongoDB by `AnalyzeStationClimate`, and `ComputeRegionTrend` reads those rows back
and fits the trend. `ComputeRegionTrend`'s `station_count` parameter is the
**dependency signal** — passing `discovery.station_count` makes the trend step wait
for every station's analysis to finish before aggregating.

## How it works

Three facets, all reading the CSV that [ingest](ingest.md) cached:

- **`AnalyzeStationClimate`** — `download_station_csv` → `parse_ghcn_csv` →
  `compute_yearly_summaries(daily, station_id, state)` → per-year dicts
  (temp mean/min/max, precip annual, hot/frost days, and snow metrics). Each year is
  upserted to `weather_reports` via `WeatherReportStore.upsert_report` (keyed
  `station_id`+`year`). Returns `yearly_summaries` as a JSON string.
- **`AnalyzeStationMonthly`** — same read path, but
  `climate_analysis.compute_monthly_summaries` produces per-(year, month) rollups
  matching the `MonthlyClimate` schema. **No Mongo write** — it's an intermediate for
  [climate-report](climate-report.md).
- **`ComputeRegionTrend`** — queries `weather_reports` for the state/year window,
  groups by year, averages across stations, then runs `simple_linear_regression`
  over annual means: `warming_rate_per_decade = slope × 10`. Precip change is
  first-vs-last-year %. Writes yearly aggregates to `climate_state_years` and the
  final trend to `climate_trends`, and composes the narrative.

Data shape: `daily records → per-year summaries (Mongo) → per-year cross-station
averages → OLS slope → ClimateTrend JSON + narrative`.

## Fan-out

`ComputeRegionTrend` itself is **single-task** (one aggregation query). The fan-out
lives in the workflow around it: `AnalyzeStateTrends` runs `DiscoverStations` then
`andThen foreach station` → `FetchStationData` + `AnalyzeStationClimate` +
`ReverseGeocode` in parallel across the fleet, and only then `ComputeRegionTrend`.
The `Analyze*` continent workflows (`AnalyzeAllStates`, `AnalyzeCanada`,
`AnalyzeEurope`, …) fan a further level out, one `AnalyzeStateTrends` per
state/country. See [workflows](workflows.md).

## Data & fields

- **Elements:** `TMAX`/`TMIN` (→ `temp_mean`, `temp_min_avg`, `temp_max_avg`,
  `hot_days`, `frost_days`), `PRCP` (→ `precip_annual`, `precip_days`), `SNOW`
  (→ `snow_annual`) and `SNWD` (→ `snow_depth_max`).
- **Schemas:** `weather.types.YearlyClimate` (per-station-year) and
  `weather.types.ClimateTrend` (the regional result; the handler also emits
  `snow_per_decade_mm`, `snow_change_pct`, `has_snow_data` beyond the declared
  fields).
- **Snowfall is nullable-by-design.** Per-year `snow_annual` is `None` (not `0`) when
  no station logged snow that year, so warm regions / non-snow stations stay out of
  the snow regression. `ComputeRegionTrend` only fits the snow slope over years with
  `snow_annual is not None`; `has_snow_data=False` regions get no snow sentence.
- The narrative reports the regression **slope** for snow (robust), not the noisy
  first-vs-last `snow_change_pct` (kept in the JSON but omitted from the sentence).

## External libraries / binaries

- **`pymongo`** (pip, the `mongodb` extra) — `AnalyzeStationClimate` and
  `ComputeRegionTrend` persist/read through `WeatherReportStore` / `ClimateStore`.
  Mongo lives strictly in the handler layer; `climate_analysis.py` (the regression +
  aggregation core) is stdlib-only so the CLI runs without a cluster.
- `simple_linear_regression` is a hand-rolled OLS in `climate_analysis.py` — no numpy.
- No binary deps.

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `AnalyzeStationClimate` | event | external / moderate | Per-year TMAX/TMIN/PRCP/snow summaries for one station → `weather_reports` |
| `AnalyzeStationMonthly` | event | external / moderate | Per-(year, month) rollups (`MonthlyClimate`), no Mongo write |
| `ComputeRegionTrend` | event | io / cheap | Reads back the station reports, fits the regional warming/precip/snow trend |

Key workflows: `weather.workflows.AnalyzeStation` (single station),
`AnalyzeStateTrends` (discover → foreach → trend), and the continent-scale
`AnalyzeAllStates` / `AnalyzeCanada` / `AnalyzeEurope` / `AnalyzeSouthAmerica` /
`AnalyzeAfrica` / `AnalyzeAsia` / `AnalyzeArctic` / `AnalyzeRussia` / `AnalyzeIndia`
/ `AnalyzeMexico` / `AnalyzeAntarctica` fan-outs.

## Cache / output

- No file artifact — outputs are MongoDB collections: `weather_reports`
  (per-station-year), `climate_state_years` (per-region-year), `climate_trends`
  (one trend doc per state, with `narrative`). The station CSVs it reads live in
  `cache/noaa-weather/station-csv/`.
- If Mongo is unavailable `ComputeRegionTrend` degrades to `_empty_trend` (zeros +
  "No data available"), not a crash.

## Gotchas & notes

- **Region readback needs the reports written first.** `ComputeRegionTrend` only sees
  stations already persisted by `AnalyzeStationClimate`; the `station_count`
  dependency signal exists to enforce that ordering in the workflow. Running the
  trend before the foreach completes yields a partial/empty trend.
- **State tagging.** Reports are filtered by `location == state`, so
  `AnalyzeStationClimate` must be passed the same `state` the trend queries with
  (the `AnalyzeStateTrends` foreach passes `state = $.state`).
- **`precip_days` is currently 0** in the region aggregate (`handle_compute_region_trend`
  hard-codes it) — a known simplification.
- Warm/non-snow regions correctly produce a trend with `has_snow_data=False` and no
  snow line — do not read that as "no data".

## Related specs

- [ingest](ingest.md) — supplies the cached CSV every analysis reads.
- [catalog-discovery](catalog-discovery.md) — supplies the station list + the
  `station_count` dependency signal.
- [quality-control](quality-control.md), [extremes](extremes.md) — the two sibling
  features that reuse this per-station→region-readback pattern.
- [climate-report](climate-report.md) — bundles these aggregates (via
  `AnalyzeStationMonthly`) into an HTML/SVG report.
- [workflows](workflows.md) — the fan-out + continent-scale wrappers.
