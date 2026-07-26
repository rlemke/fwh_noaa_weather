# Extreme-Event Detection

**Namespace:** `weather.Extremes` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Extremes`) ·
**Handler:** `src/noaa_weather/handlers/extremes/extremes_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/extremes.py`,
`extremes_chart.py` · **Persistence:** `ExtremeEventStore`
(`handlers/shared/ghcn_utils.py`) · **Tests:**
`src/noaa_weather/handlers/extremes/tests/test_extremes.py`

## Overview

Beyond linear warming trends, this feature surfaces the **discrete extreme events**
in a station's record — heat waves, cold snaps, wet & dry spells, and heavy rain/snow
days — each with its span, intensity, and per-decade frequency. It then rolls
per-station events up to a region and computes per-type **decadal trends** (is this
region seeing *more* heat waves / *fewer* cold snaps over time?), and renders the
result as an SVG/HTML chart. It reuses the shared download+parse path and the same
per-station → persist → region-readback pattern as [trends](trends.md).

## How it works

- **`DetectStationExtremes`** — `download_station_csv` → `parse_ghcn_csv` →
  `detect_events(daily, ExtremeConfig.from_params(params))`. The pure
  `extremes.detect_events` scans the sorted daily records: `_consecutive_runs` finds
  maximal runs of consecutive **calendar** days meeting a predicate (a gap or missing
  value breaks a run — no interpolation), `_threshold_days` finds single heavy
  rain/snow days. Each event carries `type`, `start_date`, `end_date`,
  `duration_days`, a type-specific `peak_value` (`_PEAK_META`: hottest tmax / coldest
  tmin / total precip / dry-day count / that day's value), and its start `year`.
  Returns `events`, `counts_by_type`, and `decadal_frequency` (`{type:{decade:count}}`).
  The handler best-effort persists a per-station rollup via
  `ExtremeEventStore.upsert_station` tagged by `location`.
- **`AggregateRegionExtremes`** — reads back the region's rollups
  (`find_for_region`) and calls `extremes.aggregate_region`, which sums counts +
  the per-type-per-decade matrix and fits an OLS `_slope` per event type → a
  `per_decade_change` + `direction` (rising/falling/flat) trend, plus a narrative.
- **`RenderExtremesChart`** — `extremes_chart.decadal_bars_svg` (grouped per-decade
  frequency bars with trend annotations) + `extremes_chart.extremes_html`.

Data shape: `daily records → event catalog + decadal frequency → Mongo rollup →
region totals + decadal trends → SVG + HTML`.

## Fan-out

Each facet is single-task. Fan-out is in `DetectRegionExtremes` /
`VisualizeRegionExtremes`: `DiscoverStations` → `andThen foreach station` →
`DetectStationExtremes` → `AggregateRegionExtremes`, gated by
`discovery.station_count`. See [workflows](workflows.md).

## Data & fields

- **Event types & default thresholds** (every one a documented, defaulted FFL
  parameter, so a human runs it with just `station_id`):
  - `heat_wave` — ≥ `heat_wave_min_days` (3) consecutive days with `tmax ≥
    heat_wave_tmax_c` (35 °C)
  - `cold_snap` — ≥ `cold_snap_min_days` (3) days with `tmin ≤ cold_snap_tmin_c`
    (−10 °C)
  - `wet_spell` — ≥ `wet_spell_min_days` (5) days with `prcp ≥ wet_day_mm` (1 mm)
  - `dry_spell` — ≥ `dry_spell_min_days` (21) days with `prcp < wet_day_mm`
  - `heavy_rain` — a single day with `prcp ≥ heavy_rain_mm` (50 mm)
  - `heavy_snow` — a single day with `snow ≥ heavy_snow_mm` (100 mm)
- Elements used: `TMAX`, `TMIN`, `PRCP`, `SNOW` (from the wide daily records; `None`
  values break runs).
- `ExtremeConfig.from_params` coerces numeric strings and keeps defaults for
  absent/`None` keys — so FFL params pass straight in.

## External libraries / binaries

- **`pymongo`** (pip, `mongodb` extra) — `ExtremeEventStore` persist/readback,
  handler-layer only. `extremes.py` is pure stdlib (dataclasses, `datetime`), and the
  region `_slope` is a self-contained OLS (no numpy).
- Charts are **dependency-free raw SVG** (`extremes_chart.py`) — no matplotlib.

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `DetectStationExtremes` | event | external / moderate | One station → event catalog, counts_by_type, decadal_frequency (with `RetryPolicy()`) |
| `AggregateRegionExtremes` | event | io / cheap | Region totals + per-type decadal trends |
| `RenderExtremesChart` | event | io / cheap | Per-decade frequency SVG bars + HTML page |

Workflows: `DetectStationExtremeEvents`, `DetectRegionExtremes`,
`VisualizeStationExtremes`, `VisualizeRegionExtremes`.

## Cache / output

- **Charts:** `cache/noaa-weather/extremes-viz/<slug>/extremes.svg` + `extremes.html`
  (sidecar-backed, written through `get_storage()` so they honor `FW_STORAGE` and land
  in MinIO under s3).
- **Rollups:** MongoDB `weather_extreme_events` (per station) +
  `weather_extreme_regions` (per region).

## Gotchas & notes

- **"Consecutive" means calendar-consecutive** — a single missing/flagged day ends a
  spell; runs are never bridged across gaps. This is deliberate (no interpolation) but
  means sparse records under-count long spells.
- **`DetectStationExtremes` requires `station_id`** and raises a clear `ValueError`
  pointing at `DiscoverStations` if it's missing.
- Region trend direction is the sign of the per-decade OLS slope; a region with only
  one decade of data gets a flat (0) trend.
- Persistence is best-effort (same as QC); a Mongo outage doesn't fail detection.

## Related specs

- [ingest](ingest.md) — the CSV the detector reads.
- [trends](trends.md) — the sibling analysis; extremes reuse its
  per-station→region-readback pattern.
- [quality-control](quality-control.md) — the other pattern-sharing feature.
- [catalog-discovery](catalog-discovery.md) — supplies the station list + dependency
  signal.
- [workflows](workflows.md) — the region fan-out + visualize variants.
