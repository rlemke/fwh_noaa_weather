# NOAA Weather — Tools

Standalone command-line utilities for the NOAA GHCN-Daily ingestion + climate-analysis pipeline. Each tool is a self-contained Python program paired with a shell wrapper. FFL handlers in `../handlers/` import the same library code, so the CLI and the runtime exercise one code path and share one cache.

Pattern contract: [`agent-spec/tools-pattern.agent-spec.yaml`](../../../agent-spec/tools-pattern.agent-spec.yaml)
Cache layout contract: [`agent-spec/cache-layout.agent-spec.yaml`](../../../agent-spec/cache-layout.agent-spec.yaml)

## Setup

```bash
# Install every dep the tools need (requests, matplotlib for charts).
# Idempotent — safe to re-run. Targets the repo's .venv.
./tools/install-tools.sh
```

## Standards followed

The climate report output uses established climate-data conventions:

| Convention | Where it shows up |
|---|---|
| **WMO 30-year climate normals** (1991–2020 default baseline) | monthly normals table; anomaly baseline |
| **Walter-Lieth climograph** (monthly temp line + precip bars) | `climograph.svg` |
| **Ed Hawkins' warming stripes** (one coloured stripe per year) | `warming_stripes.svg` |
| **Annual anomaly bars** (red = above normal, blue = below) | `anomaly_bars.svg` |
| **Year × month temperature heatmap** | `heatmap.svg` |
| **OLS trend line on annual mean temps** | `annual_trend.svg` |

## Pipeline

```
    ┌────────────────────────────┐
    │  download-ghcn-catalog     │  ← NOAA GHCN-Daily S3
    │  stations.txt + inventory  │
    └─────────────┬──────────────┘
                  │  catalog/
                  ▼
    ┌────────────────────────────┐
    │  discover-stations         │  (filter by country/state/years)
    │  stations-discovered/      │
    └─────────────┬──────────────┘
                  │
                  ▼
    ┌────────────────────────────┐
    │  fetch-station-csv         │  ← per-station CSV (all years)
    │  station-csv/              │
    └─────────────┬──────────────┘
                  │
                  ▼
    ┌────────────────────────────┐
    │  summarize-station         │  (yearly climate summaries)
    │  climate-summary/          │
    └─────────────┬──────────────┘
                  │
                  ▼
    ┌────────────────────────────┐
    │  compute-region-trend      │  (aggregate across stations)
    │  region-trend/             │
    └─────────────┬──────────────┘
                  │
                  ▼
    ┌────────────────────────────┐
    │  climate-report            │  ← Markdown + HTML + SVG charts
    │  climate-report/<c>/<r>/   │    (climograph, warming stripes,
    └────────────────────────────┘     heatmap, anomaly, trend)

    ┌────────────────────────────┐
    │  reverse-geocode           │  ← OSM Nominatim
    │  geocode/                  │
    └────────────────────────────┘
```

Every arrow is sidecar-mediated: each tool records its SHA-256 (plus tool/version) in a `.meta.json` sibling, so re-running is a no-op when nothing has changed.

## Available tools

| Tool | Input | Output cache | Purpose |
|------|-------|--------------|---------|
| `download-ghcn-catalog` | none | `catalog/{stations,inventory}.txt` | Fetch the two GHCN catalog files from NOAA S3 |
| `discover-stations` | `--country --state --min-years` | `stations-discovered/<country>/<state>.json` (optional) | Filter catalog + inventory into a station candidate list |
| `fetch-station-csv` | `<station_id>...` or `--stations-file` | `station-csv/<station_id>.csv` | Download per-station daily-record CSVs |
| `summarize-station` | `<station_id>` + year range | `climate-summary/<station_id>.json` (optional) | Parse CSV + compute yearly climate summaries |
| `compute-region-trend` | summary files or `--from-cache` | `region-trend/<country>/<state>.json` (optional) | Aggregate station summaries → regional trend + narrative |
| `climate-report` | `--region` / `--country --state` + year range, plus `--all-under PREFIX` / `--include-parents` / `--list` for multi-region batches | `climate-report/<country>/<region>/{report.{json,md,html},*.svg}` + master `climate-report/index.html` regenerated after every run | Full regional climate report: monthly normals (WMO 30-year baseline), annual anomalies, decadal comparison, climograph / warming stripes / heatmap / anomaly / trend SVG charts, self-contained HTML |
| `reverse-geocode` | `<lat> <lon>...` or `--coords-file` | `geocode/<lat>_<lon>.json` | Reverse geocode via Nominatim (rate-limited, cached) |
| `download-ndbc-catalog` | none | `ndbc-catalog/{activestations.xml,stations.json}` | Fetch the NOAA National Data Buoy Center active-stations catalog |
| `discover-buoys` | `--region` / `--bbox` / `--type` / `--require` | stdout JSON | Filter the cached NDBC catalog (ocean buoys + coastal + DART + NERRS) |
| `fetch-buoy-data` | `<station_id>` or `--from-catalog` + `--start-year` / `--end-year` | `ndbc-stdmet/<station_id>/<year>.txt.gz` | Download historical stdmet observations (hourly, per-station-per-year) |
| `summarize-buoy` | `<station_id>` or `--from-cache` | `buoy-summaries/<station_id>.json` | Yearly air-temp / sea-temp / wave-height / storm-day summaries |
| `build-buoys-map` | none | `ndbc-catalog/buoys-map.html` | MapLibre points map of every cached buoy, coloured by station type |

Every tool supports:
- `--help` — show flags
- `--force` — re-run even if cache is current *(where applicable)*
- `--use-mock` — offline deterministic mode (no network)
- `--log-level` — Python logging level (default INFO)

## Cache layout

All outputs live at `$AFL_CACHE_ROOT/noaa-weather/` (default: `/Volumes/afl_data/cache/noaa-weather/`):

```
cache/noaa-weather/
├── catalog/
│   ├── stations.txt + .meta.json
│   └── inventory.txt + .meta.json
├── stations-discovered/
│   └── <country>/<state>.json + .meta.json
├── station-csv/
│   └── <station_id>.csv + .meta.json
├── climate-summary/
│   └── <station_id>.json + .meta.json
├── region-trend/
│   └── <country>/<state>.json + .meta.json
├── climate-report/
│   └── <country>/<region>/
│       ├── report.{json,md,html} + .meta.json
│       └── {climograph,annual_trend,warming_stripes,heatmap,anomaly_bars}.svg + .meta.json
├── ndbc-catalog/
│   ├── activestations.xml + .meta.json   ← NOAA NDBC raw catalog
│   ├── stations.json       + .meta.json  ← normalized {id, name, type, owner, lat, lon}
│   └── buoys-map.html      + .meta.json  ← MapLibre points map of every cached buoy
├── ndbc-stdmet/
│   └── <station_id>/<year>.txt.gz + .meta.json   ← NOAA standard met obs (hourly)
├── buoy-summaries/
│   └── <station_id>.json + .meta.json    ← yearly buoy climate summaries
├── geofabrik/
│   └── index-v1.json + .meta.json
└── geocode/
    └── <lat>_<lon>.json + .meta.json
```

Each artifact has a sibling `.meta.json` sidecar recording size, SHA-256, source lineage, tool name/version, and generation timestamp. See the [cache-layout spec](../../../agent-spec/cache-layout.agent-spec.yaml) for the full contract.

## Typical workflows

**Canada and every province in a single run:**

```bash
./tools/climate-report.sh \
    --all-under north-america/canada --include-parents \
    --start-year 1950 --end-year 2026 \
    --i-know-this-is-huge
open "$AFL_CACHE_ROOT/noaa-weather/climate-report/index.html"
```

The master index at `climate-report/index.html` auto-regenerates after every report, so each region shows up under its continent/country as soon as it lands. Preview the region set first with `--list`:

```bash
./tools/climate-report.sh --all-under north-america/canada --include-parents --list
```

**Bootstrap a fresh machine and analyze one state:**

```bash
# 1. Download catalogs (first call only — cached 24h)
./tools/download-ghcn-catalog.sh

# 2. Filter catalog for 10 long-running NY stations
./tools/discover-stations.sh --country US --state NY --max-stations 10 --min-years 30

# 3. Download their CSVs (reads the list from stdout if you want, or retype)
./tools/fetch-station-csv.sh USW00094728 USW00014732 USW00094846 ...

# 4. Compute per-station yearly summaries
for sid in USW00094728 USW00014732 USW00094846; do
  ./tools/summarize-station.sh "$sid" --state NY --write-cache
done

# 5. Aggregate to a regional trend
./tools/compute-region-trend.sh --state NY --start-year 1944 --end-year 2024 \
    --from-cache --write-cache
```

**Offline / CI mode (no network, no `requests` required):**

```bash
./tools/download-ghcn-catalog.sh --use-mock
./tools/discover-stations.sh --country US --state NY --use-mock
./tools/fetch-station-csv.sh USW00094728 --use-mock
```

Every tool has a deterministic mock path. Tests in `../tests/` rely on this.

**Marine data via NDBC (ocean buoys + coastal stations):**

GHCN is land-based. For sea-surface temperature, wave heights, and
offshore meteorology, the NOAA National Data Buoy Center publishes a
parallel catalog + per-station-per-year stdmet records. The workflow
mirrors the GHCN tools:

```bash
# 1. Fetch the active-stations catalog (one XML + one normalized JSON)
./tools/download-ndbc-catalog.sh

# 2. Filter — every moored buoy with a meteorology sensor
./tools/discover-buoys.sh --type buoy --require met

# 3. North-central Pacific, 2020–2024
./tools/fetch-buoy-data.sh --from-catalog --region north-america/us \
    --type buoy --start-year 2020 --end-year 2024

# 4. Yearly summaries (air temp, sea temp, wave height, storm days)
./tools/summarize-buoy.sh --from-cache

# 5. MapLibre points map with station-type legend + click popups
./tools/build-buoys-map.sh
open "$AFL_CACHE_ROOT/noaa-weather/ndbc-catalog/buoys-map.html"
```

Each buoy carries `met` / `currents` / `waterquality` / `dart` sensor
flags and a station type (`buoy`, `cman`, `dart`, `nerrs`). The
discovery filter's `--require` flag accepts any of these. All tools
share the same mock-data story as GHCN — pass `--use-mock` for offline
smoke tests.

## `_lib/` — shared library

The real implementation lives in `_lib/`. Both the CLI tools and the FFL handlers import from it. Every handler shim (`handlers/shared/ghcn_utils.py`) re-exports `_lib/` symbols so FFL code works with familiar names.

| Module | Role |
|--------|------|
| `sidecar.py` | Per-entry `.meta.json` read/write, presence, per-entry locking |
| `storage.py` | LocalStorage / HdfsStorage abstraction + root-path derivation |
| `ghcn_download.py` | GHCN catalog + per-station CSV download with sidecar cache |
| `ghcn_parse.py` | Pure parsers for `ghcnd-stations.txt`, `ghcnd-inventory.txt`, per-station CSVs |
| `climate_analysis.py` | Pure functions: yearly summaries, monthly summaries, climate normals, anomalies, linear regression, region trend |
| `climate_charts.py` | matplotlib → SVG renderers: climograph, annual trend, warming stripes, year × month heatmap, anomaly bars (lazy-imported so non-chart callers don't pay the cost) |
| `geofabrik_regions.py` | Geofabrik `index-v1.json` fetcher + region-path → bbox lookup |
| `geocode_nominatim.py` | OSM Nominatim client with rate limiting + sidecar cache |
| `ghcn_mocks.py` | Deterministic mock fallbacks for offline mode |
| `ndbc_download.py` | NDBC active-stations catalog + stdmet per-station-per-year fetch |
| `ndbc_parse.py` | Pure parsers for `activestations.xml` and stdmet text; hourly → daily downsample |
| `ndbc_map.py` | MapLibre points map for cached buoy stations + joined summaries |
| `ndbc_mocks.py` | Deterministic offline NDBC catalog + stdmet data |

The `parse` and `analysis` modules are fully pure — no I/O, network, or database — so unit tests can exercise them directly.

## Conventions

Every tool here follows [`agent-spec/tools-pattern.agent-spec.yaml`](../../../agent-spec/tools-pattern.agent-spec.yaml). Key rules:

- One `.py` + one `.sh` per tool, no more.
- `stdout` is for structured output (pipe-friendly); `stderr` is for logs.
- Zero dependency on the Facetwork runtime, MongoDB, or the dashboard — tools must run without a cluster.
- MongoDB writes live in the handler layer, not in `_lib/`. `_lib/` returns data; handlers persist.
- Cached artifacts always use the staged-write protocol (stage → finalize → write sidecar). Never bypass `sidecar.py`.

## Handler integration

Handlers in `../handlers/` route through `../handlers/shared/ghcn_utils.py`, which adds `tools/` to `sys.path` and re-exports `_lib/` symbols. When you rename a function in `_lib/`, update the shim; handlers themselves rarely need to change.

MongoDB stores (`WeatherReportStore`, `ClimateStore`) stay in the shim, not in `_lib/` — the CLI tools must be runnable standalone without a Mongo cluster.

Every CLI tool has a matching FFL event facet in `../ffl/weather.ffl`:

| Tool | FFL facet |
|------|-----------|
| `download-ghcn-catalog` + `discover-stations` | `weather.Catalog.DiscoverStations` (now accepts `region: String` for Geofabrik bbox filtering) |
| `fetch-station-csv` | `weather.Ingest.FetchStationData` |
| `summarize-station` | `weather.Analysis.AnalyzeStationClimate`, `AnalyzeStationMonthly` |
| `compute-region-trend` | `weather.Analysis.ComputeRegionTrend` |
| `climate-report` (single region) | `weather.Report.GenerateClimateReport` |
| `climate-report --all-under` (multi-region) | `weather.Report.ListRegionsUnder` + `weather.workflows.GenerateRegionGroupReport` |
| `reverse-geocode` | `weather.Geocode.ReverseGeocode` |

The `GenerateRegionGroupReport` workflow uses `ListRegionsUnder` + `andThen foreach` so the FFL runtime can fan out a Canada-plus-all-provinces batch across the distributed runner fleet — same cache, same code path as the CLI.
