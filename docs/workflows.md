# Workflows & Fan-out Composition

**Namespaces:** `weather.workflows`, `weather.Cache` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl`
(§ `namespace weather.workflows`, § `namespace weather.Cache`) ·
**Mixins:** `weather.mixins` (`RetryPolicy`, `RateLimit`) · **Handlers:** none of
their own — workflows orchestrate the event facets in the other specs.

## Overview

This is the composition tier: the entry-point workflows that stitch the domain's
event facets into end-to-end pipelines, plus the cache-warmup family and the
continent-scale convenience wrappers. Workflows carry no handler code — they are FFL
`andThen`/`foreach`/`catch` orchestration over the facets documented in
[catalog-discovery](catalog-discovery.md), [ingest](ingest.md), [trends](trends.md),
[quality-control](quality-control.md), [extremes](extremes.md), [marine](marine.md),
[geocode](geocode.md), and [climate-report](climate-report.md).

## How it works

The recurring shape is **discover → fan out → aggregate**:

```
discovery = DiscoverStations(...) andThen foreach station in discovery.stations {
    ... per-station facets (catch { yield <Workflow>(status="partial_failure", ...) }) ...
}
region = <AggregateFacet>(..., station_count = discovery.station_count)
yield <Workflow>(status="completed", ...)
```

`discovery.station_count` is threaded into the aggregate facet as the **dependency
signal** so the region step waits for every fan-out branch to finish. Every
per-station step is wrapped in `catch { yield ... "partial_failure" }` so one bad
station degrades the run rather than failing it. The `Visualize*` variants append a
`Render*Chart` step and return its `html_path`.

## Fan-out

Two levels of fan-out:

- **Per-station** — the `andThen foreach station in discovery.stations` body
  (`AnalyzeStateTrends`, `SummarizeRegionQuality`, `DetectRegionExtremes`,
  `AnalyzeBuoyRegion`, `CacheStateData`). One distributed task per station, claimed
  across the fleet in parallel — the reason a 50-station state finishes in roughly
  one station's wall-clock.
- **Per-region/country** — the continent wrappers `foreach` over a state/country list,
  each delegating to `AnalyzeStateTrends` / `CacheStateData` (which themselves fan out
  per station). So `AnalyzeAllStates` is a fan-out of fan-outs.

## Data & fields

- **`weather.workflows`** — single-station (`AnalyzeStation`), state
  (`AnalyzeStateTrends`), QC (`SummarizeStationQuality`, `SummarizeRegionQuality`,
  `VisualizeStationQuality`, `VisualizeRegionQuality`), extremes
  (`DetectStationExtremeEvents`, `DetectRegionExtremes`, `VisualizeStationExtremes`,
  `VisualizeRegionExtremes`), reports (`GenerateRegionReport`,
  `GenerateGeofabrikRegionReport`, `GenerateRegionGroupReport`), marine
  (`AnalyzeBuoyRegion`), and the continent wrappers (`AnalyzeAllStates`,
  `AnalyzeCanada`, `AnalyzeRussia`, `AnalyzeIndia`, `AnalyzeMexico`,
  `AnalyzeAntarctica`, `AnalyzeSouthAmerica`, `AnalyzeEurope`, `AnalyzeAfrica`,
  `AnalyzeAsia`, `AnalyzeArctic`).
- **`weather.Cache`** — warmup workflows that only discover + download (no analysis):
  `CacheStateData`, `CacheAllUSData`, and the continent set (`CacheCanadaData`,
  `CacheRussiaData`, `CacheEuropeData`, …). Use these to pre-populate the CSV cache.
- Country/state lists are FFL default parameters (FIPS-style country codes, e.g.
  Europe `["UK","GM","FR",...]`); every workflow takes `max_stations`, `start_year`,
  `end_year`.

## External libraries / binaries

None directly — workflows are pure FFL. Their runtime cost is whatever the underlying
facets pull in (`requests`, `pymongo`, etc.); see the per-feature specs.

## Facets & workflows

`weather.mixins` provides the two implicit mixins used across the facet library:
`RetryPolicy(max_retries=3, backoff_ms=2000)` and `RateLimit(requests_per_sec=1,
burst=1)`, with `implicit default_retry` / `default_rate`. These are the `with
RetryPolicy()` / `with RateLimit()` annotations seen on the download/geocode facets.

The catalog.yaml manifest (`src/noaa_weather/catalog.yaml`) is a curated,
machine-readable subset of these workflows (with NL `summary` + `param_schema`) for
reuse-first matching — it intentionally omits the per-continent convenience wrappers,
which all delegate to `AnalyzeStateTrends` / `CacheStateData`.

## Cache / output

Workflows own no cache of their own; their outputs are whatever the composed facets
write (Mongo collections, `cache/noaa-weather/*` artifacts, `*-viz` charts, report
bundles).

## Gotchas & notes

- **`catch` yields terminate the whole workflow** with a `partial_failure` status —
  it's a degrade-and-report, not a per-branch skip; the per-station step logs still
  surface the specific failure.
- **The aggregate step must receive `station_count`** to order correctly after the
  foreach — omitting it can aggregate before the branches finish.
- **Continent wrappers are convenience only** — for a new region prefer parameterising
  `AnalyzeStateTrends` / `CacheStateData` directly.
- **`Cache*` workflows do not analyze** — they only warm the CSV cache; run an
  `Analyze*`/`Generate*` afterwards.

## Related specs

- Every other spec — workflows are the composition surface over all of them. Start
  with [trends](trends.md) (the flagship pipeline) and
  [catalog-discovery](catalog-discovery.md) (the shared first step).
