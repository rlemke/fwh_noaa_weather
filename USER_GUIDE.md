# NOAA Weather Station Analysis — User Guide

> See also: [Examples Guide](../doc/GUIDE.md) | [README](../README.md)

## When to Use This Example

Use this as your starting point if you are:
- Building **real HTTP data pipelines** that download, parse, and analyze external data from AWS S3
- Using a **catalog-first approach** where metadata tells you what data exists before downloading
- Learning **andThen foreach** (batch processing), **catch** (error recovery), and **workflow composition** (workflows calling workflows)
- Persisting analysis results to **MongoDB** for cross-handler data sharing
- Using **RegistryRunner** with per-namespace handler modules

## What You'll Learn

1. How GHCN-Daily data is discovered and downloaded from AWS S3 using a catalog-first approach
2. How `andThen foreach` fans out across stations for parallel fetch + analysis
3. How `catch` blocks recover from download failures without aborting the workflow
4. How workflow composition scales from a single station to all 50 US states (and internationally)
5. How `ClimateStore` and `WeatherReportStore` persist results to MongoDB for cross-handler aggregation
6. How linear regression computes warming rates, precipitation, and snowfall trends across decades
7. How OSM Nominatim reverse geocoding enriches station data with location context

## Step-by-Step Walkthrough

### 1. The Problem

Given a region (country and optional state), discover GHCN-Daily weather stations with sufficient data coverage, download each station's CSV from AWS S3, compute per-year climate summaries (temperature, precipitation, snowfall, extreme days), aggregate across stations into a regional trend with linear regression (warming rate, precipitation change, and snowfall change per decade), and produce a narrative summary. Handle download failures gracefully so partial results are still useful.

### 2. The Data Source — GHCN-Daily on AWS S3

GHCN-Daily (Global Historical Climatology Network - Daily) is hosted on AWS S3 at `https://noaa-ghcn-pds.s3.amazonaws.com/`. Three key files drive the pipeline:

| File | Purpose |
|------|---------|
| `ghcnd-stations.txt` | Station catalog — ID, lat, lon, elevation, name for 100K+ stations worldwide |
| `ghcnd-inventory.txt` | Per-station element/year ranges — tells you what data exists before downloading |
| `csv/by_station/{ID}.csv` | Actual observations — one CSV per station containing ALL years (no per-year loops) |

The catalog-first approach means `DiscoverStations` reads the station and inventory files to verify a station has the required elements (TMAX, TMIN, PRCP) and minimum years of coverage before any data download begins.

### 3. Schemas — Typed Data Structures

Four schemas in `weather.types` define the data flowing between steps:

| Schema | Purpose |
|--------|---------|
| `StationInfo` | Station identity, location, elevation, country, state, elements, year range |
| `YearlyClimate` | Per-year climate summary (temp mean/min/max, precipitation, hot/frost/precip days) |
| `ClimateTrend` | Regional trend output (warming rate per decade, precipitation change, decade summaries) |
| `GeoContext` | Reverse geocode result (display name, city, state, country, county) |

### 4. Event Facets — The Processing Steps

Five event facets across four namespaces:

| Namespace | Event Facet | Purpose |
|-----------|-------------|---------|
| `weather.Catalog` | `DiscoverStations` | Download and parse station/inventory catalogs, filter by country/state/coverage |
| `weather.Ingest` | `FetchStationData` | Download one CSV per station from S3, filter to year range |
| `weather.Analysis` | `AnalyzeStationClimate` | Compute per-year climate summaries from downloaded CSV, write to MongoDB |
| `weather.Analysis` | `ComputeRegionTrend` | Aggregate station results, run linear regression, generate narrative |
| `weather.Geocode` | `ReverseGeocode` | Reverse geocode station coordinates via OSM Nominatim |

### 5. The AnalyzeStation Workflow

The simplest workflow analyzes a single station in one `andThen` block with three concurrent steps:

```afl
workflow AnalyzeStation(station_id: String, station_name: String, lat: Double, lon: Double,
    start_year: Int = 1944, end_year: Int = 2024) => (status: String, detail: String) andThen {
    fetch = FetchStationData(station_id = $.station_id, start_year = $.start_year, end_year = $.end_year) catch {
        yield AnalyzeStation(status = "download_failed", detail = "Download failed for " ++ $.station_id)
    }
    climate = AnalyzeStationClimate(station_id = $.station_id, station_name = $.station_name,
        lat = $.lat, lon = $.lon, start_year = $.start_year, end_year = $.end_year)
    geo = ReverseGeocode(lat = $.lat, lon = $.lon)
    yield AnalyzeStation(status = "completed", detail = "Analyzed " ++ fetch.station_id ++ ": " ++ climate.years_analyzed ++ " years")
}
```

- **FetchStationData** downloads the station CSV from S3 (cached locally). If the download fails, the `catch` block yields immediately with an error status instead of aborting.
- **AnalyzeStationClimate** parses the CSV, computes yearly summaries, and writes results to MongoDB.
- **ReverseGeocode** enriches the station with location context from OSM Nominatim.

All three steps run concurrently. The `yield` at the end waits for all to complete.

### 6. The AnalyzeStateTrends Workflow

This workflow composes discovery, per-station analysis, and regional trend computation:

```afl
workflow AnalyzeStateTrends(country: String = "US", state: String = "NY", max_stations: Int = 5,
    start_year: Int = 1944, end_year: Int = 2024) => (status: String, narrative: String) andThen {
    discovery = DiscoverStations(country = $.country, state = $.state, max_stations = $.max_stations)
        andThen foreach station in discovery.stations {
            fetch = FetchStationData(station_id = $.station.station_id, ...) catch { ... }
            climate = AnalyzeStationClimate(station_id = $.station.station_id, ...) catch { ... }
            geo = ReverseGeocode(lat = $.station.lat, lon = $.station.lon)
        }
    trend = ComputeRegionTrend(country = $.country, state = $.state, ...)
    yield AnalyzeStateTrends(status = "completed", narrative = trend.narrative)
}
```

1. **DiscoverStations** finds up to `max_stations` stations with sufficient coverage
2. **andThen foreach** fans out — for each discovered station, fetch data, analyze climate, and geocode run concurrently
3. Per-station `catch` blocks allow individual station failures without aborting the batch
4. **ComputeRegionTrend** waits for all station analyses to complete (via the `station_count` dependency signal), then aggregates results from MongoDB, runs linear regression, and generates a narrative

### 7. Scaling Up — AnalyzeAllStates and International Workflows

Workflows compose into larger workflows:

| Workflow | Scope |
|----------|-------|
| `AnalyzeAllStates` | All 50 US states via `foreach state in $.states`, each calling `AnalyzeStateTrends` |
| `AnalyzeCanada` | 13 provinces/territories |
| `AnalyzeEurope` | 18 European countries |
| `AnalyzeSouthAmerica` | 10 South American countries |
| `AnalyzeAfrica` | 13 African countries |
| `AnalyzeAsia` | 7 Asian countries |
| `AnalyzeArctic` | 3 Arctic territories |
| `AnalyzeRussia` | Russia (single-country) |
| `AnalyzeIndia` | India (single-country) |
| `AnalyzeMexico` | Mexico (single-country) |
| `AnalyzeAntarctica` | Antarctica research stations |

Each international workflow reuses `AnalyzeStateTrends` with the appropriate FIPS country code, demonstrating that the same pipeline works globally with no code changes.

### 8. Cache Warmup Workflows

Cache warmup workflows pre-download GHCN-Daily CSVs without running analysis, useful for priming the local file cache before running full analysis:

| Workflow | Scope |
|----------|-------|
| `CacheStateData` | One state — discover stations, fetch each CSV |
| `CacheAllUSData` | All 50 US states |
| `CacheCanadaData` | 13 Canadian provinces |
| `CacheEuropeData`, `CacheAfricaData`, `CacheAsiaData`, ... | International regions |

### 9. Extreme-Event Detection and Visualization

Linear trends capture the slow warming signal; the `weather.Extremes`
namespace surfaces the **discrete extreme events** in a station's record —
heat waves, cold snaps, wet & dry spells, and heavy rain/snow days — and
asks whether they are getting more or less frequent over the decades.

Three event facets:

| Namespace | Event Facet | Purpose |
|-----------|-------------|---------|
| `weather.Extremes` | `DetectStationExtremes` | One station → per-event catalog (type, start/end, duration, peak value), per-type counts, and per-decade frequency. Persists a per-station rollup to MongoDB. |
| `weather.Extremes` | `AggregateRegionExtremes` | Reads back a region's per-station rollups → region totals + per-type decadal **trends** (rising/falling). |
| `weather.Extremes` | `RenderExtremesChart` | Grouped SVG bar chart (per-decade frequency by event type, with trend annotations) + a self-contained HTML page. |

Every detection threshold is a documented, defaulted parameter — to get the
standard events you only supply `station_id`:

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `heat_wave_tmax_c` | 35 | daily high (°C) marking a heat-wave day |
| `heat_wave_min_days` | 3 | consecutive hot days to qualify |
| `cold_snap_tmin_c` | -10 | daily low (°C) marking a cold-snap day |
| `cold_snap_min_days` | 3 | consecutive cold days to qualify |
| `heavy_rain_mm` | 50 | single-day rainfall counted as heavy |
| `wet_day_mm` | 1 | rainfall marking a day "wet" for spells |
| `wet_spell_min_days` | 5 | consecutive wet days to qualify |
| `dry_spell_min_days` | 21 | consecutive dry days to qualify |
| `heavy_snow_mm` | 100 | single-day snowfall counted as heavy |

Four workflows compose these facets:

| Workflow | Pipeline |
|----------|----------|
| `DetectStationExtremeEvents` | single station → `DetectStationExtremes` |
| `DetectRegionExtremes` | `DiscoverStations` → foreach `DetectStationExtremes` → `AggregateRegionExtremes` |
| `VisualizeStationExtremes` | detect → `RenderExtremesChart` → return `html_path` |
| `VisualizeRegionExtremes` | discover → foreach detect → aggregate → `RenderExtremesChart` → return `html_path` |

`DetectRegionExtremes` reuses the same two-phase **persist + readback**
pattern as `AnalyzeStationClimate` → `ComputeRegionTrend`: each station's
detection persists a rollup via `ExtremeEventStore`, and the aggregator reads
them back by location, gated by `station_count` so it waits for every station
to finish before computing decadal trends.

The charts are rendered as **dependency-free raw SVG** — no matplotlib (which
isn't installed in the runners), so the visualize workflows run on a stock
runner.

```json
// VisualizeRegionExtremes
{"country": "US", "state": "TX", "max_stations": 5, "start_year": 1950, "end_year": 2024}
```

## Running on the Dashboard

### 1. Seed and start the runner

```bash
# From repo root
source .venv/bin/activate

# Seed the NOAA weather workflows into MongoDB
scripts/publish src/noaa_weather/ffl/weather.ffl

# Register handlers and start runner + dashboard
scripts/start-runner --example noaa-weather -- --log-format text
```

### 2. Submit a workflow

Open `http://localhost:8080` in a browser. Navigate to the NOAA weather flow, select `AnalyzeStateTrends`, and submit with inputs:

```json
{
    "country": "US",
    "state": "NY",
    "max_stations": 5,
    "start_year": 1944,
    "end_year": 2024
}
```

Or for a single station via `AnalyzeStation`:

```json
{
    "station_id": "USW00094728",
    "station_name": "NEW YORK CENTRAL PARK OBS",
    "lat": 40.7789,
    "lon": -73.9692,
    "start_year": 2000,
    "end_year": 2024
}
```

Or via curl:

```bash
# Find the flow ID
FLOW_ID=$(python3 -c "
from afl.runtime.mongo_store import MongoStore
store = MongoStore('mongodb://afl-mongodb:27017')
for f in store.get_flows():
    if 'noaa' in f.name.lower() or 'weather' in f.name.lower():
        print(f.uuid); break
")

# Submit
curl -X POST "http://localhost:8080/flows/$FLOW_ID/run/AnalyzeStateTrends" \
  -d 'inputs_json={"country":"US","state":"NY","max_stations":5,"start_year":1944,"end_year":2024}'
```

### 3. Monitor progress

The dashboard shows each step progressing through states: `Created -> Ready -> Running -> Completed`. A typical `AnalyzeStateTrends` run for one state with 5 stations completes in under a minute, producing:
- 5 station CSV downloads (cached on subsequent runs)
- 5 per-station climate analyses written to MongoDB
- 5 reverse geocode lookups
- 1 regional trend with warming rate and precipitation change narrative

## Key Concepts

### Catalog-First Discovery
`DiscoverStations` reads `ghcnd-stations.txt` and `ghcnd-inventory.txt` before downloading any observation data. This avoids wasted downloads by verifying that a station has the required elements (TMAX, TMIN, PRCP) and minimum years of coverage. Both catalog files are cached locally with a 24-hour TTL.

### One CSV Per Station
GHCN-Daily stores all years for a station in a single CSV file at `csv/by_station/{ID}.csv`. There is no per-year download loop. The handler downloads the file once (cached by station ID), then filters records to the requested year range during parsing.

### CSV Format and QC Filtering
GHCN-Daily CSV columns are `ID,DATE,ELEMENT,DATA_VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME`. The parser pivots from long format (one row per element per day) to wide format (one dict per day with `tmax`, `tmin`, `prcp`, `snow`, `snwd`). Rows with a non-empty `Q_FLAG` (failed NOAA quality control) are skipped automatically.

### ClimateStore and WeatherReportStore
`AnalyzeStationClimate` writes per-station yearly summaries to the `weather_reports` MongoDB collection. `ComputeRegionTrend` reads these reports to aggregate across stations, then writes yearly state-level data to `climate_state_years` and the final trend to `climate_trends`. This cross-handler data sharing via MongoDB is what enables the two-phase pattern: analyze stations individually, then aggregate.

### Linear Regression for Trends
`ComputeRegionTrend` uses ordinary least-squares regression on yearly temperature means to compute `warming_rate_per_decade` (slope * 10). Precipitation change is computed as the percentage difference between first and last observed values. Results include per-decade summaries with average temperature and precipitation.

### Catch for Error Recovery
Download failures in `FetchStationData` and analysis failures in `AnalyzeStationClimate` don't abort the workflow. The `catch` block yields a partial-failure status, allowing batch workflows to continue processing other stations.

### String Concatenation
The `++` operator builds detail messages: `"Analyzed " ++ fetch.station_id ++ ": " ++ climate.years_analyzed ++ " years"`.

### Mock Fallbacks
When the `requests` library is not installed (e.g., in test environments), all HTTP-dependent functions fall back to deterministic hash-based mock data. This keeps tests fast and CI independent of network access.

## Adapting for Your Use Case

### Analyze a different country

Change the `country` parameter to any FIPS country code supported by GHCN-Daily:
```json
{"country": "CA", "state": "ON", "max_stations": 10}
```

The pipeline works identically for international stations. GHCN station IDs encode the country in their first two characters (e.g., `CA` for Canada, `GM` for Germany, `UK` for United Kingdom).

### Add a new analysis facet

1. Define the event facet in the `weather.Analysis` namespace:
```afl
event facet ComputeExtremes(station_id: String, start_year: Int, end_year: Int)
    => (hottest_day: String, coldest_day: String, wettest_day: String)
```

2. Write a handler in `handlers/analysis/analysis_handlers.py` that reads from the cached CSV and computes the extremes.

3. Add the step to the `AnalyzeStation` workflow's `andThen` block.

### Use a different data source

Replace the `FetchStationData` handler's download logic with your data source (ERA5, local CSV files, etc.) while keeping the same FFL workflow structure and return schema (`record_count`, `years_with_data`, `station_id`).

### Adjust station selection criteria

`DiscoverStations` accepts `min_years` (minimum years of data coverage) and `required_elements` (list of GHCN elements that must be present). Increase `min_years` for higher-quality stations or add elements like `SNOW` and `SNWD` for snow analysis.

## Next Steps

- **[hiv-drug-resistance](../hiv-drug-resistance/)** — bioinformatics pipeline with `andThen when` branching, `catch` error recovery, and `andThen foreach` batch processing
- **[devops-deploy](../devops-deploy/)** — deployment pipeline with `andThen when` conditional branching and nested conditions
- **[event-driven-etl](../event-driven-etl/)** — extract/transform/load pipeline with schemas, foreach, and map literals
