# Station Catalog Discovery

**Namespace:** `weather.Catalog` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Catalog`) ·
**Handler:** `src/noaa_weather/handlers/catalog/catalog_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/ghcn_parse.py`,
`ghcn_download.py`, `geofabrik_regions.py` · **CLI:** `tools/discover-stations.sh`

## Overview

Discovery is the front door of the GHCN-Daily pipeline: it answers *"which stations
exist for this place, and which of them actually have the data I need?"* before any
per-station CSV is downloaded. It is a **catalog-first** design — the two small NOAA
index files (`ghcnd-stations.txt`, `ghcnd-inventory.txt`) are consulted so downstream
`FetchStationData` only ever pulls CSVs that are known to carry the requested elements
over enough years. Every fan-out workflow in the package (`AnalyzeStateTrends`,
`SummarizeRegionQuality`, `DetectRegionExtremes`, the `Cache*` warmup family) opens
with `DiscoverStations`, and its `station_count` return doubles as the dependency
signal that gates the region aggregation step.

## How it works

`handle_discover_stations` (`catalog_handlers.py`):

1. **Resolve region (optional).** If `region` is set, `geofabrik_regions.resolve_region`
   turns a Geofabrik path (`europe/germany`, `north-america/us/california`) into a
   lat/lon **bbox**; a bad path raises `KeyError` and the step fails fast with a
   logged error rather than silently returning `[]`. When `region` is set and
   `country` is left at its default `"US"`, the country filter is suppressed so the
   bbox is authoritative (the registry runner can't see whether `country` was
   defaulted, so default-`US` is treated as not-explicit).
2. **Load the catalog** via the shim wrappers `download_station_catalog()` /
   `download_inventory()` → `ghcn_download.read_catalog_file(...)` (cached text, 24 h
   max-age).
3. **Parse.** `parse_stations` reads the fixed-width `ghcnd-stations.txt`
   (ID cols 1–11, lat 13–20, lon 22–30, elevation 32–37, name 42+) into
   `{station_id, name, lat, lon, elevation}`; `parse_inventory` reads
   `ghcnd-inventory.txt` into `{station_id: {elements: set, first_year, last_year,
   element_ranges}}`.
4. **Filter + rank.** `filter_stations` applies every set filter (they compose) and
   sorts by data coverage (widest year span first), capped at `max_stations`.

Data shape: `fixed-width catalog text → station dicts + inventory dict → filtered,
inventory-enriched station list (JSON)`. The CLI `discover-stations.sh` and the FFL
facet run the identical `ghcn_parse` core.

## Fan-out

**Single-task — no fan-out.** Discovery is one Mongo-free pass over two small index
files; it is the step that *produces* the list the workflows then fan out over
(`andThen foreach station in discovery.stations`). See
[workflows](workflows.md).

## Data & fields

- **Country filter:** FIPS prefix — the first 2 chars of the station ID
  (`station_country`). Pass `""` to skip (used when a bbox is authoritative).
- **US state filter:** bounding-box match against `US_STATE_BOUNDS` (a 50-state
  lat/lon box table in `ghcn_parse.py`) via `station_in_state`.
- **Region filter:** Geofabrik-derived bbox `(min_lat, max_lat, min_lon, max_lon)`.
- **Coverage filter:** `min_years` (default 20) against the inventory's
  `last_year - first_year + 1`.
- **Element filter:** `required_elements` (default `["TMAX","TMIN","PRCP"]`) — a
  station passes only if the inventory lists *all* requested 4-char element codes.
  The recognized set is `{TMAX, TMIN, PRCP, SNOW, SNWD}` (`_ELEMENT_SET`).

Returned station dicts are enriched with `first_year`, `last_year`, and the sorted
`elements` list from the inventory — matching the `weather.types.StationInfo` schema.

## External libraries / binaries

- **`requests`** (pip, optional) — used only inside `ghcn_download` /
  `geofabrik_regions` to fetch the index files; both fall back to deterministic
  mocks (`ghcn_mocks`) when it is absent and `use_mock` is set. No binary deps.
- **stdlib only** for parsing (`csv`, string slicing). No `pymongo` — discovery is
  DB-free.

## Facets & workflows

| Facet | Kind | Effect / Cost | Signature (abridged) |
|---|---|---|---|
| `DiscoverStations` | event | external / moderate | `(country="US", state="", region="", max_stations=10, min_years=20, required_elements=["TMAX","TMIN","PRCP"]) => (stations: Json, station_count: Int)` — with `RetryPolicy()` |

`region` accepts any Geofabrik path and, when set, its bbox becomes the spatial
filter and overrides `country` unless `country` is passed explicitly.

## Cache / output

- Reads `cache/noaa-weather/catalog/stations.txt` + `inventory.txt` (each with a
  `.meta.json` sidecar, 24 h max-age) and `cache/noaa-weather/geofabrik/
  index-v1.json` (14-day max-age).
- No output artifact of its own — returns the station list inline as JSON. No Mongo
  write.

## Gotchas & notes

- **State filter is bbox-based, not political.** `station_in_state` tests a
  rectangular lat/lon box, so a station just across a state line can be mis-included;
  known tradeoff (fast, catalog-only). The Geofabrik bbox has the same
  vertex-extent-box sloppiness.
- **`country="US"` is treated as "not explicit" when `region` is set** — pass a
  different country, or omit `region`, if you truly want to intersect both.
- **Element filter is AND, not OR.** Requiring `["TMAX","TMIN","PRCP","SNOW"]` drops
  every station that never logged snow — usually not what you want for a warm region.
- **A bad Geofabrik path raises** (fail-fast) rather than returning an empty list.

## Related specs

- [ingest](ingest.md) — the next step: download the CSV for each discovered station.
- [analysis-trends](trends.md) — the flagship consumer of the discovered
  station list.
- [workflows](workflows.md) — the fan-out workflows that open with `DiscoverStations`.
- [storage-and-cache](storage-and-cache.md) — the catalog/geofabrik cache layout.
