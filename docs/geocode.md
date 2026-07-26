# Reverse Geocoding

**Namespace:** `weather.Geocode` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Geocode`) ·
**Handler:** `src/noaa_weather/handlers/geocode/geocode_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/geocode_nominatim.py` ·
**CLI:** `tools/reverse-geocode.sh`

## Overview

Stations arrive as bare `(lat, lon)`. This feature turns those coordinates into a
human place (`display_name`, `city`, `state`, `country`, `county`) via OSM Nominatim,
so a report or map can say "Sacramento, California" instead of a station code. It is a
small, cache-heavy helper used inside the `AnalyzeStation` / `AnalyzeStateTrends`
workflows — reverse-geocode results never change for a given point, so the on-disk
cache is aggressive.

## How it works

`handle_reverse_geocode` → `reverse_geocode_nominatim(lat, lon)` (shim) →
`geocode_nominatim.reverse_geocode`:

1. Round lat/lon to 4 decimals (~11 m) for the cache key so nearby queries coalesce.
2. Consult `cache/noaa-weather/geocode/<lat>_<lon>.json` (unless `force`) — a cache
   hit returns immediately with no network and no sleep.
3. On a live lookup, GET the Nominatim reverse endpoint, **sleep 1 second** (the
   anonymous-use rate limit), and write the result to the sidecar-backed cache before
   returning.

Data shape: `(lat, lon) → place dict (GeoContext)`. Keys are always present even when
Nominatim has no data (values may be empty strings).

## Fan-out

**Single-task per coordinate.** It runs inside the per-station `foreach` body of the
analysis workflows, so parallelism is the workflow's, not this facet's.

## Data & fields

- Returns the `weather.types.GeoContext` schema: `display_name`, `city`, `state`,
  `country`, `county`.
- No filtering — it's a point lookup.

## External libraries / binaries

- **`requests`** (pip, optional) — the Nominatim HTTP call; `ghcn_mocks` /
  `use_mock=True` give a deterministic offline path for tests. No binary deps.

## Facets & workflows

| Facet | Kind | Effect / Cost | Signature |
|---|---|---|---|
| `ReverseGeocode` | event | external / cheap | `(lat: Double, lon: Double) => (geo: GeoContext)` — with `RateLimit()` (1 req/sec, burst 1) |

Used as the `geo = ReverseGeocode(...)` step in `weather.workflows.AnalyzeStation` and
in the `AnalyzeStateTrends` foreach body.

## Cache / output

- `cache/noaa-weather/geocode/<lat_rounded>_<lon_rounded>.json` + `.meta.json`. On s3
  it lands in MinIO like any other cached artifact.
- No Mongo write.

## Gotchas & notes

- **Nominatim caps anonymous use at 1 req/sec** — the tool enforces a 1 s sleep after
  every *live* call (cache hits don't sleep). The `with RateLimit()` mixin documents
  this at the facet level. Do not remove the sleep or run wide un-cached fan-outs
  against the public endpoint.
- **Results are effectively immutable** for a given rounded point — that's why the
  cache never expires here.

## Related specs

- [trends](trends.md) — the workflows that call `ReverseGeocode` per station.
- [storage-and-cache](storage-and-cache.md) — the `geocode` cache layout.
