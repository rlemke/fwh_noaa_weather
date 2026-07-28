# FFL Examples — `noaa-weather`

Every numbered scenario is a **complete, compilable FFL file**. Copy one into
`my.ffl` and run it:

```bash
fw ffl run --primary my.ffl \
  --library ~/fw_handlers/fwh_noaa_weather/src/noaa_weather/ffl/weather.ffl \
  --workflow my.weather.<WorkflowName>
```

A runner serving the `weather` namespace must be up
(`fw runner start --domain noaa-weather`). Every block below is compile-checked
against `src/noaa_weather/ffl/weather.ffl`.

New to the language? Start with the
[FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md)
and the [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical).

---

## The building blocks

A discovery facet that returns a **station list**, per-station analysis facets, and
region aggregators that fan the per-station results back in. Signatures are
abridged — the FFL source is authoritative.

| Declaration | Role |
|---|---|
| `weather.Vocab.ResolveElement(term) => (result)` | NL term → GHCN element code (the semantic-lookup primitive) |
| `weather.Catalog.DiscoverStations(country, state, region, max_stations, min_years, …) => (stations: Json, station_count)` | The **fan-out unit** |
| `weather.Ingest.FetchStationData(station_id, start_year, end_year)` | Cache one station's dailies |
| `weather.Analysis.AnalyzeStationClimate(station_id, station_name, lat, lon, start_year, end_year, state)` | Per-station climate summary |
| `weather.QC.SummarizeQualityFlags(station_id, …)` / `AggregateRegionQC(…, station_count)` / `RenderQCChart(summary_json, …)` | Per-station QC → region rollup → chart |
| `weather.Extremes.DetectStationExtremes(station_id, …, heat_wave_tmax_c, …)` / `AggregateRegionExtremes(…)` / `RenderExtremesChart(…)` | Extreme-event detection → rollup → chart |
| `weather.Marine.DiscoverBuoys` / `FetchBuoyData` / `SummarizeBuoy` / `BuildBuoysMap` | The NDBC buoy family |
| `weather.workflows.*` | The shipped entry points (`SummarizeRegionQuality`, `DetectRegionExtremes`, …) |

---

## 1. Run what ships — no FFL to write

```bash
fw ffl seed --include noaa-weather

fw ffl run --primary ~/fw_handlers/fwh_noaa_weather/src/noaa_weather/ffl/weather.ffl \
  --workflow weather.workflows.SummarizeRegionQuality \
  --inputs '{"country": "US", "state": "NY", "max_stations": 5}'
```

Write FFL when you want a different *shape* — your own station selection, extra
per-station steps, different extreme-event thresholds, or your own error handling.

## 2. The smallest workflow you can write

Every FFL workflow needs a `namespace`, a `use` per namespace it calls into, and a
`yield` back to itself.

```ffl
namespace my.weather {

    use weather.QC

    /** Quality summary for one station. */
    workflow OneStationQC(station_id: String = "USW00094728") => (flagged_pct: Double, narrative: String) andThen {

        qc = weather.QC.SummarizeQualityFlags(station_id = $.station_id, start_year = 1950, end_year = 2026)

        yield OneStationQC(flagged_pct = qc.flagged_pct, narrative = qc.narrative)
    }
}
```

Rules visible above: `=>` sits on the **same line** as the closing `)`; references
are always `step.field`; `$.station_id` reads the workflow's parameter.

## 3. Discover, then fan out per station

`andThen foreach v in <list>` turns one step into N runtime steps runners claim in
parallel. The `foreach` hangs off the **`discovery` step**, so inside the body `$`
is that step — `$.station` is the loop variable, and `$$` reaches the workflow's
parameters. (Loop items are Json objects, so `$.station.station_id` walks into one.)

```ffl
namespace my.weather {

    use weather.Catalog
    use weather.QC

    /** One QC task per discovered station, in parallel across the fleet. */
    workflow RegionQC(country: String = "US", state: String = "NY", max_stations: Int = 5,
        start_year: Int = 1950, end_year: Int = 2026) => (status: String) andThen {

        discovery = weather.Catalog.DiscoverStations(
            country = $.country, state = $.state, max_stations = $.max_stations) andThen foreach station in $.stations {

            qc = weather.QC.SummarizeQualityFlags(
                station_id = $.station.station_id,
                station_name = $.station.name,
                state = $$.state,
                start_year = $$.start_year,
                end_year = $$.end_year)

            yield RegionQC(status = "station_done")
        }
    }
}
```

Wall clock is the slowest station, not the sum.

## 4. Fan out, then fan in — the region rollup

The aggregators read the per-station cache, so they need no value from the loop;
pass `discovery.station_count` as the ordering signal and put the rollup **after**
the fan-out block, at workflow level.

```ffl
namespace my.weather {

    use weather.Catalog
    use weather.QC

    /** Per-station QC in parallel, then one region-level rollup. */
    workflow RegionQCRollup(country: String = "US", state: String = "NY", max_stations: Int = 5,
        start_year: Int = 1950, end_year: Int = 2026) => (status: String, narrative: String) andThen {

        discovery = weather.Catalog.DiscoverStations(
            country = $.country, state = $.state, max_stations = $.max_stations) andThen foreach station in $.stations {

            qc = weather.QC.SummarizeQualityFlags(
                station_id = $.station.station_id,
                station_name = $.station.name,
                state = $$.state,
                start_year = $$.start_year,
                end_year = $$.end_year)

            yield RegionQCRollup(status = "station_done", narrative = "")
        }

        region = weather.QC.AggregateRegionQC(
            country = $.country,
            state = $.state,
            start_year = $.start_year,
            end_year = $.end_year,
            station_count = discovery.station_count)

        yield RegionQCRollup(status = "completed", narrative = region.narrative)
    }
}
```

`region` references `discovery.station_count`, which is what sequences it after the
fan-out. That is the `dependency_signal` idiom under another name.

## 5. Tune the extreme-event thresholds

Thresholds are ordinary parameters — a different definition of "heat wave" is a
CLI argument, not a code change.

```ffl
namespace my.weather {

    use weather.Extremes

    /** A stricter heat-wave definition for one station. */
    workflow HotDays(station_id: String = "USW00094728", tmax_c: Double = 38.0, min_days: Int = 4) => (summary: String, events: Int) andThen {

        ex = weather.Extremes.DetectStationExtremes(
            station_id = $.station_id,
            start_year = 1950,
            end_year = 2026,
            heat_wave_tmax_c = $.tmax_c,
            heat_wave_min_days = $.min_days)

        yield HotDays(summary = ex.summary, events = ex.event_count)
    }
}
```

## 6. Chain a chart onto an aggregate

`RenderExtremesChart` takes the aggregate's Json outputs directly — a plain step
reference is all the wiring needed.

```ffl
namespace my.weather {

    use weather.Extremes

    /** Region extremes → rendered chart. */
    workflow ExtremesChart(country: String = "US", state: String = "NY") => (html_path: String) andThen {

        agg = weather.Extremes.AggregateRegionExtremes(
            country = $.country, state = $.state, start_year = 1950, end_year = 2026)

        chart = weather.Extremes.RenderExtremesChart(
            title = "Extreme events",
            label = $.state,
            counts_by_type = agg.counts_by_type,
            decadal_frequency = agg.decadal_frequency,
            trends = agg.trends,
            summary = agg.narrative)

        yield ExtremesChart(html_path = chart.html_path)
    }
}
```

## 7. One dead station shouldn't kill the region — `catch`

`catch` fires when its step errors after retries are exhausted. Inside a `foreach`
it is per-iteration, so the rest of the region proceeds. Note that inside a `catch`
block `$` is the **failing step**, so a sibling step of the outer block (e.g.
`discovery`) is out of scope there — yield constants or `$.`/`$$.` values.

```ffl
namespace my.weather {

    use weather.Catalog
    use weather.QC

    /** Best-effort region QC. */
    workflow BestEffortRegionQC(country: String = "US", state: String = "NY", max_stations: Int = 5) => (status: String) andThen {

        discovery = weather.Catalog.DiscoverStations(
            country = $.country, state = $.state, max_stations = $.max_stations) andThen foreach station in $.stations {

            qc = weather.QC.SummarizeQualityFlags(
                station_id = $.station.station_id, state = $$.state) catch {
                yield BestEffortRegionQC(status = "station_failed")
            }

            yield BestEffortRegionQC(status = "station_done")
        }
    }
}
```

## 8. Branch on a result — `when`

A `when` block hangs off the step it inspects: inside a case `$` is that step and
`$$` reaches the workflow. Every `when` needs a default case, last.

```ffl
namespace my.weather {

    use weather.Catalog
    use weather.QC

    /** Don't bother aggregating a region with almost no stations. */
    workflow GuardedRegionQC(country: String = "US", state: String = "NY", min_stations: Int = 3) => (status: String, narrative: String) andThen {

        discovery = weather.Catalog.DiscoverStations(
            country = $.country, state = $.state) andThen when {
            case $.station_count >= $$.min_stations => {
                region = weather.QC.AggregateRegionQC(
                    country = $$.country, state = $$.state, station_count = $.station_count)
                yield GuardedRegionQC(status = "completed", narrative = region.narrative)
            }
            case _ => {
                yield GuardedRegionQC(status = "too_few_stations", narrative = "")
            }
        }
    }
}
```

## 9. Call-time mixins — this domain has its own

`weather.mixins` declares `RetryPolicy` and `RateLimit`, attached to the network
facets. Any mixin can be added or overridden at a **call site**:

```ffl
namespace my.weather {

    use weather.Ingest
    use weather.mixins

    /** A long backfill: more time, more retries. */
    workflow PatientFetch(station_id: String) => (records: Int) andThen {

        f = weather.Ingest.FetchStationData(
            station_id = $.station_id, start_year = 1900, end_year = 2026) with RetryPolicy() with Timeout(minutes = 60)

        yield PatientFetch(records = f.record_count)
    }
}
```

## 10. Reuse the shipped workflows

```ffl
namespace my.weather {

    use weather.workflows

    /** Wrap a shipped workflow and reshape its result. */
    workflow RegionHeadline(state: String = "NY") => (headline: String) andThen {

        run = weather.workflows.SummarizeRegionQuality(country = "US", state = $.state, max_stations = 5)

        yield RegionHeadline(headline = run.status ++ ": " ++ run.narrative)
    }
}
```

---

## Cheat sheet

| You want to… | Write |
|---|---|
| Read a workflow/step parameter | `$.name` (`$$.name` one level out) |
| Read a field of a Json loop variable | `$.station.station_id` |
| Read a previous step's result | `stepname.field` |
| Fan out from a facet's list result | `step = Discover(…) andThen foreach v in $.field { … }` (then `$$` = workflow) |
| Fan in after a fan-out | put the aggregate **after** the block and reference `step.count` |
| Override a mixin for one call | `… with RetryPolicy() with Timeout(minutes = 60)` |
| Handle a step failure | `step = Facet(…) catch { yield … }` |
| Branch | `step = Facet(…) andThen when { case <bool> => { … } case _ => { … } }` |
| Concatenate strings | `a ++ b` |

**Validate before you run:** `afl my.ffl --check` or MCP `fw_validate`. Every error
carries a `rule_id` — fetch `fw://docs/rules/{rule_id}` for a wrong/right pair.

## See also

- [`docs/README.md`](README.md) — per-feature specs for this domain
- [FFL grammar](https://github.com/rlemke/facetwork/blob/main/docs/reference/language/grammar.md) ·
  [canonical examples](https://github.com/rlemke/facetwork/tree/main/examples/canonical) ·
  [relative `$`-scoping](https://github.com/rlemke/facetwork/blob/main/docs/architecture/ffl-relative-scoping.md)
- `src/noaa_weather/ffl/weather.ffl` — the source of truth for every signature above
