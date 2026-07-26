# Marine Buoys (NDBC)

**Namespace:** `weather.Marine` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Marine`) ·
**Handler:** `src/noaa_weather/handlers/marine/marine_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/ndbc_download.py`,
`ndbc_parse.py`, `ndbc_map.py`, `ndbc_mocks.py` ·
**CLI:** `tools/discover-buoys.sh`, `fetch-buoy-data.sh`, `summarize-buoy.sh`,
`build-buoys-map.sh`

## Overview

The marine feature is the ocean counterpart to the land-based GHCN chain: it
discovers **NOAA National Data Buoy Center (NDBC)** buoys and coastal stations,
downloads their standard-meteorological (`stdmet`) history, summarises each into a
yearly buoy-climate JSON, and renders a MapLibre points map colour-coded by station
type. It parallels the catalog → ingest → analysis → map structure but for sea/air
temperature, waves, wind, and pressure.

## How it works

Five facets, thin dispatchers over `ndbc_*` tool libs (same one-code-path story as
GHCN — a facet call and a CLI run write identical sidecar-backed artifacts):

- **`DownloadNdbcCatalog`** — `ndbc_download.download_catalog` fetches
  `activestations.xml`, normalises it to `stations.json`
  (`ndbc_parse.parse_activestations_xml`), both sidecar-backed. `force`/`use_mock`
  supported.
- **`DiscoverBuoys`** — reads the cached catalog, resolves a spatial filter (explicit
  `bbox` wins, else `region` → Geofabrik bbox), and `ndbc_parse.filter_buoys` filters
  by `types` and `require_fields`. Returns the filtered list as JSON for `foreach`.
- **`FetchBuoyData`** — loops `[start_year, end_year]` calling
  `ndbc_download.download_stdmet` per year; individual-year failures (404s on
  pre-install years) are counted (`failed`) but don't abort.
- **`SummarizeBuoy`** — enumerates the station's cached stdmet years through the
  storage sidecar walk (works on local *and* s3), `localize`s each `.txt.gz` for the
  gzip reader, computes daily-from-hourly means and per-year buoy-climate stats, and
  writes one summary JSON.
- **`BuildBuoysMap`** — `ndbc_map.rebuild_buoys_map` renders the MapLibre HTML from
  the catalog (+ any per-station summaries for popups).

Data shape: `activestations.xml → stations.json → filtered buoys → per-year stdmet
.txt.gz → yearly summary JSON → MapLibre HTML`.

## Fan-out

`AnalyzeBuoyRegion` fans out: `DownloadNdbcCatalog` → `DiscoverBuoys` → `andThen
foreach station` → `FetchBuoyData` + `SummarizeBuoy` in parallel → `BuildBuoysMap`
(gated by `discovery.station_count` via `BuildBuoysMap(dependency_signal=…)`).
Per-station failures are caught so one bad buoy doesn't poison the batch.

## Data & fields

- **Station types:** `buoy`, `cman`, `dart`, `nerrs`, `oil`, `other` (the
  `weather.types.BuoyStation` schema `type`); sensor-family booleans `met`,
  `currents`, `waterquality`, `dart` say which payloads a station reports (`met` is
  the closest GHCN analogue). `require_fields` filters on those.
- **stdmet fields summarised:** `air_temp`, `sea_temp`, `pressure`, `wind_speed`,
  `wave_height` → per-year means + `sea_temp_max` / `wave_height_max`, plus counts
  `high_sst_days` (SST > 28 °C) and `storm_days` (wave > 4 m) — thresholds
  `HIGH_SST_C` / `STORM_WAVE_M` in the handler.
- NDBC stdmet files are space-delimited with a 2-line header (parsed by
  `ndbc_parse.parse_stdmet_gz`).

## External libraries / binaries

- **`requests`** (pip, optional) — catalog + stdmet HTTP downloads; `ndbc_mocks`
  provides opt-in offline data (`use_mock=True`).
- **`gzip`** (stdlib) — stdmet files are gzipped.
- The buoys map is **self-contained MapLibre HTML** (CARTO Voyager basemap) written
  as raw text — no folium/shapely required for this path.

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `DownloadNdbcCatalog` | event | external / moderate | Fetch + normalise the active-stations catalog |
| `DiscoverBuoys` | event | io / cheap | Filter the catalog to a candidate buoy set (region/bbox/type/fields) |
| `FetchBuoyData` | event | external / **expensive** | Download every stdmet year for one station |
| `SummarizeBuoy` | event | io / cheap | Yearly buoy-climate summary from cached stdmet |
| `BuildBuoysMap` | event | io / cheap | MapLibre points map, colour-coded by type |

Workflow: `weather.workflows.AnalyzeBuoyRegion` (the full 5-step marine recipe).

## Cache / output

- `cache/noaa-weather/ndbc-catalog/activestations.xml` + `stations.json` +
  `buoys-map.html`
- `cache/noaa-weather/ndbc-stdmet/<station_id>/<year>.txt.gz`
- `cache/noaa-weather/buoy-summaries/<station_id>.json`
- All sidecar-backed; under `FW_STORAGE=s3` they land in MinIO (`SummarizeBuoy` stages
  to local scratch then `finalize_from_local`; the stdmet read path `localize`s each
  gz).

## Gotchas & notes

- **`SummarizeBuoy` must enumerate the cache via the sidecar walk, not
  `Path().iterdir()`** — a local directory listing silently finds nothing under
  `FW_STORAGE=s3`. This is a load-bearing fix (per the repo's s3 audit).
- `FetchBuoyData` is the one **expensive**-tier facet — a wide year range × many
  stations is a lot of HTTP; pre-install years 404 harmlessly.
- The map's station count is re-read from the catalog sidecar's `extra.station_count`
  (a crude but cheap count).

## Related specs

- [catalog-discovery](catalog-discovery.md) — the GHCN analogue; shares the Geofabrik
  region → bbox resolution.
- [visualization / storage-and-cache](storage-and-cache.md) — the map/summary write
  path and s3 localize behaviour.
- [workflows](workflows.md) — the `AnalyzeBuoyRegion` fan-out.
