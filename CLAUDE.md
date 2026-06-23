# CLAUDE.md — noaa-weather

This repository is a **standalone Facetwork example package**. The Facetwork
platform (workflow compiler + runtime) lives at
`/Users/ralph_lemke/facetwork`; this repo only contains the NOAA-specific
FFL, handlers, and tools. The two are wired together via the
`facetwork.examples` entry point in `pyproject.toml`.

## Quick orientation

```
fwh_noaa_weather/
├── pyproject.toml                  # declares the facetwork.examples entry point
├── src/noaa_weather/__init__.py    # exports `example: ExamplePackage`
├── src/noaa_weather/handlers/      # event-facet implementations (one subpackage per domain)
├── src/noaa_weather/ffl/           # top-level FFL workflows
├── src/noaa_weather/tools/         # CLI utilities + _noaa_tools/ (the real implementation)
├── tests/                          # repo-level integration tests
└── agent-spec/                     # cross-cutting design specs
```

## Common operations

```bash
# Register this package with Facetwork's runner
pip install -e .

# From a Facetwork checkout:
scripts/seed-examples --include noaa-weather
scripts/start-runner --example noaa-weather -- --log-format text

# Run as a standalone agent (skip the registry runner path):
PYTHONPATH=src python agent.py

# CLIs (call the same _noaa_tools/ as the handlers — see Tools pattern below):
src/noaa_weather/tools/discover-stations.sh --country US --max 50
src/noaa_weather/tools/fetch-station-csv.sh USC00010008
src/noaa_weather/tools/compute-region-trend.sh --state CA --start 1950 --end 2024
src/noaa_weather/tools/climate-report.sh --state CA

# Tests
pytest tests/ src/noaa_weather/handlers/ -v
```

## Key concepts

### Tools / handlers / cache pattern

Every operation has two surfaces — a CLI under `src/noaa_weather/tools/`
and an FFL handler under `src/noaa_weather/handlers/<domain>/` — and both
call into the **same** implementation in `src/noaa_weather/tools/_noaa_tools/`.
This is the Facetwork canonical pattern (see
`agent-spec/tools-pattern.agent-spec.yaml`).

```
                       ┌────────────────────────┐
   CLI tool ───────────┤                        │
                       │   tools/_noaa_tools/X.py      │ ← single source of truth
   FFL handler ────────┤   (download / parse /  │
   (via shared shim)   │    analysis / mocks)   │
                       └────────────────────────┘
```

The shim lives at `src/noaa_weather/handlers/shared/ghcn_utils.py`. It
adds `tools/` to `sys.path` and re-exports `_noaa_tools` symbols, plus wraps
the new `_noaa_tools` APIs in legacy-shaped helpers (`download_station_catalog`,
`download_station_csv`, `reverse_geocode_nominatim`) so handlers don't
need to track refactors of the underlying library.

The `_noaa_tools` modules **must not** depend on `pymongo` or other handler-only
infrastructure — that way the CLIs run standalone, without a Mongo
cluster. MongoDB-backed storage (`WeatherReportStore`, `ClimateStore`)
lives in the shim.

### Cache layout

Both the CLIs and the FFL handlers read/write the same on-disk cache:

```
$AFL_DATA_ROOT/cache/noaa-weather/
├── ghcn-catalog/                   # ghcnd-stations.txt, ghcnd-inventory.txt
├── ghcn-stations/                  # per-station CSVs (one per station_id)
├── ndbc-catalog/                   # NDBC station list
├── ndbc-stations/                  # per-buoy historical CSVs
├── geocode/                        # Nominatim reverse-lookup cache (lat,lon)
└── reports/                        # rendered HTML + maps (also mirrored to MongoDB)
```

Each cached entry has a `.meta.json` sidecar describing its provenance
(source URL, fetched_at, checksum) — see
`agent-spec/cache-layout.agent-spec.yaml`.

### Storage backends (local / hdfs / s3)

`tools/_noaa_tools/storage.py` selects a backend from `AFL_STORAGE`
(`local` | `hdfs` | `s3`) rooted at `AFL_DATA_ROOT`. The `s3` backend
(`S3Storage`) delegates to `facetwork.runtime.storage.S3StorageBackend`, so a
fleet can share durable cache + outputs in **MinIO / S3** with no shared disk.

- **`localize(path)` is the read-through cache.** Object-store paths
  (`s3://…`, `hdfs://…`) are downloaded into `local_scratch_root()/localized`
  (size-checked) so readers always get a real local file; `S3Storage.finalize_from_local`
  warms that cache on write.
- **Scratch is always local.** `local_scratch_root()` (`AFL_LOCAL_SCRATCH`)
  and `local_staging_subdir` stay on local disk even when `AFL_DATA_ROOT=s3://…`
  — pointing the data root at an object store no longer poisons staging.

Under `AFL_STORAGE=s3`, durable outputs (extreme-event charts, `BuildBuoysMap`,
`warming_map`) and data downloads (`ghcn_download` station CSV + catalog,
`ndbc_download` buoy catalog + stdmet, marine `SummarizeBuoy` persist) land in
shared MinIO, while readers get a real local file via `localize`.

**Known limitations:** none outstanding for s3 — the marine `SummarizeBuoy` read
path and the analysis/discovery CLIs are migrated, and `GenerateClimateReport`'s
charts are now dependency-free raw SVG (no matplotlib), so the full report runs in
the runners. (`climate_report.py`'s own CLI storage is the last `LocalStorage()`
holdout, a dev-tool follow-up.)

### Handler subpackages

| Subpackage | Domain |
|------------|--------|
| `catalog/` | Station catalog discovery (GHCN inventory + filtering) |
| `ingest/` | Per-station CSV download + parse |
| `analysis/` | Yearly summaries + regional trends (temperature, precipitation, **snowfall**) via linear regression. Per-year snow metrics (`snow_annual`, `snow_depth_max`, `snow_days`) are `None`, not 0, when a station/year logged no SNOW/SNWD, so warm regions / non-snow stations stay out of the snow regression; `ComputeRegionTrend` emits `snow_per_decade_mm` + `snow_change_pct` + `has_snow_data` and adds a snowfall sentence to the narrative only when snow data exists. |
| `geocode/` | Reverse-geocoding via Nominatim |
| `marine/` | NDBC buoy catalog + observations |
| `report/` | Per-station HTML, batch summaries, warming maps |
| `extremes/` | Extreme-event detection (heat waves, cold snaps, wet/dry spells, heavy rain/snow) + SVG/HTML charts |
| `qc/` | Quality-control surfacing — `SummarizeQualityFlags` re-reads the cached CSV and **counts** the Q-flagged observations the analysis silently drops (overall %, per element, per year, per QC-check letter), so a reader can see what share of the record was rejected. `AggregateRegionQC` rolls the per-station rollups (persisted via `QCSummaryStore`, same persist+readback pattern as `ComputeRegionTrend`) up to ONE **observation-weighted** region rate + worst-stations ranking. `RenderQCChart` draws a dependency-free SVG bar of per-element flagged % (+ which-check-tripped / worst-stations tables) in a self-contained HTML page (no matplotlib). Pure counting/aggregation in `tools/_noaa_tools/ghcn_qc.py`, chart in `tools/_noaa_tools/qc_chart.py`. |

Each module exposes `register_handlers(runner)` for the RegistryRunner;
all of them are wired into `register_all_registry_handlers` in
`src/noaa_weather/handlers/__init__.py`.

### Extreme-event detection (`weather.Extremes`)

Beyond linear warming trends, the `weather.Extremes` namespace surfaces the
discrete extreme events in a station's record. The detection itself lives in
the pure `tools/_noaa_tools/extremes.py` library (no I/O); rendering lives in
`tools/_noaa_tools/extremes_chart.py` (dependency-free **raw SVG** — no
matplotlib, which isn't installed in the runners).

| Facet | Role |
|-------|------|
| `weather.Extremes.DetectStationExtremes` | One station → heat waves, cold snaps, wet & dry spells, heavy rain/snow days. Every threshold is a documented, defaulted parameter (`heat_wave_tmax_c=35`, `heat_wave_min_days=3`, `cold_snap_tmin_c=-10`, `cold_snap_min_days=3`, `heavy_rain_mm=50`, `wet_day_mm=1`, `wet_spell_min_days=5`, `dry_spell_min_days=21`, `heavy_snow_mm=100`). Returns the per-event catalog (type, span, duration, peak_value), per-type counts, and per-decade frequency. Persists a per-station rollup via `ExtremeEventStore`. |
| `weather.Extremes.AggregateRegionExtremes` | Reads back the per-station rollups for a region (by location, gated on `station_count`) into region totals + per-type **decadal trends** (rising/falling). Same Mongo persist+readback pattern as `AnalyzeStationClimate` → `ComputeRegionTrend`. |
| `weather.Extremes.RenderExtremesChart` | Renders a grouped SVG bar chart (per-decade frequency by event type, with trend annotations) + a self-contained HTML page; returns `html_path` + `svg_path`. |

Workflows: `weather.workflows.DetectStationExtremeEvents` (single station),
`weather.workflows.DetectRegionExtremes` (DiscoverStations → foreach
DetectStationExtremes → AggregateRegionExtremes), and the visualizing variants
`weather.workflows.VisualizeStationExtremes` / `VisualizeRegionExtremes`
(detect/aggregate → RenderExtremesChart → return `html_path`).

## Adding new handlers

1. Add a Python module under `src/noaa_weather/handlers/<domain>/`.
2. Export `register_handlers(runner)` that calls
   `runner.register_handler(facet_name=..., module_uri=f"file://{os.path.abspath(__file__)}", entrypoint=...)`.
3. Wire it into `register_all_registry_handlers` in
   `src/noaa_weather/handlers/__init__.py`.
4. Drop the FFL declaration into `src/noaa_weather/ffl/` (or a domain-specific
   `handlers/<domain>/ffl/` for nested workflows).
5. If the handler does anything non-trivial, factor that work into a
   `tools/_noaa_tools/<name>.py` module, add a CLI wrapper under `tools/`, and
   re-export from `handlers/shared/ghcn_utils.py`.
6. Re-run `scripts/seed-examples --include noaa-weather` so the new flow
   shows up in the dashboard.

## Code review checklist

- For every state transition: "what if this crashes halfway?" Design the recovery path.
- For every download: cache + sidecar + max-age check, with deterministic mock fallback.
- For every retry: max count and backoff. No infinite loops.
- For every error handler: never silently return empty defaults. Fail explicitly or re-raise.
- Keep `_noaa_tools/` free of `pymongo` / handler-only deps so CLIs stay runnable standalone.

## Domain research before implementation

For NOAA / climate work, apply established practices:
- GHCN station IDs are 11 chars (`USCMM######`); inventory is fixed-width text.
- ISD-Lite is fixed-width with `-9999` for missing values; temperatures, pressures, wind speeds are scaled ×10.
- NDBC standard meteorological files are space-delimited with a 2-line header.
- Nominatim has a strict 1 req/sec rate limit — the `_noaa_tools.geocode_nominatim` helper enforces this.
- Reverse-geocoding lat/lon → place: cache aggressively (results never change for a given lat/lon).
