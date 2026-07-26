# Quality-Control Surfacing

**Namespace:** `weather.QC` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.QC`) ·
**Handler:** `src/noaa_weather/handlers/qc/qc_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/ghcn_qc.py`,
`qc_chart.py` · **Persistence:** `QCSummaryStore`
(`handlers/shared/ghcn_utils.py`) · **CLI:** `tools/summarize-quality-flags.sh`

## Overview

The climate [analysis](trends.md) silently drops every observation whose GHCN
`Q_FLAG` is non-empty (i.e. failed a NOAA quality-control check). That keeps trends
clean but hides *how much* of a station's record was rejected — exactly the number a
careful reader wants before trusting a trend. This feature re-reads the same cached
CSV and **counts** the flagged observations instead of skipping them: overall %, per
element, per year, and per QC-check letter. It then rolls per-station rates up to one
**observation-weighted** region rate with a worst-stations ranking, and renders it as
a dependency-free SVG/HTML chart. In short: a data-credibility layer over the trend
pipeline.

## How it works

Three facets, mirroring the analysis pattern:

- **`SummarizeQualityFlags`** — `download_station_csv` →
  `ghcn_qc.summarize_quality_flags(path, start, end)`. That pure function re-reads
  the CSV columns, counts each recognized-element observation, marks it flagged when
  `Q_FLAG` is non-empty, and tallies `by_element` / `by_year` / `by_flag` (splitting
  multi-letter flags) plus a `worst` (element, year) cell ranking. The handler tags
  the summary with `station_id`/`station_name`, builds a one-line `_headline`, and
  best-effort persists a per-station rollup (with counts, not just %) via
  `QCSummaryStore.upsert_station` tagged by `location`.
- **`AggregateRegionQC`** — reads back the per-station rollups for the state
  (`QCSummaryStore.find_for_region`) and calls `ghcn_qc.aggregate_region_qc`, which
  sums counts across stations for an **observation-weighted** region % (a tiny
  station can't swing the headline), sums the per-element/per-check breakdowns, and
  builds `worst_stations`. `station_count` is the dependency signal.
- **`RenderQCChart`** — takes either emitter's summary JSON and draws a horizontal
  per-element flagged-% bar (`qc_chart.flagged_pct_bars_svg`) inside a self-contained
  HTML page (`qc_chart.qc_html`) with a which-check-tripped table and, for a region, a
  worst-stations table.

Data shape: `cached CSV → counted flag summary (JSON) → Mongo rollup → region
aggregate (JSON) → SVG + HTML`.

## Fan-out

`SummarizeQualityFlags` is single-task per station; `AggregateRegionQC` and
`RenderQCChart` are single-task. Fan-out is in the workflows: `SummarizeRegionQuality`
/ `VisualizeRegionQuality` run `DiscoverStations` → `andThen foreach` →
`SummarizeQualityFlags` → `AggregateRegionQC`. See [workflows](workflows.md).

## Data & fields

- **Elements counted:** `{TMAX, TMIN, PRCP, SNOW, SNWD}` (`_QC_ELEMENT_SET`) — the
  same set the analysis consumes, so the denominator is "the data we actually use".
- **`Q_FLAG` letters** are mapped to human labels in `QFLAG_MEANINGS` (D=duplicate,
  G=gap, I=internal consistency, K=streak, O=climatological outlier, S=spatial,
  T=temporal, X=bounds, Z=Datzilla, …).
- **Summary fields:** `total_obs`, `flagged_obs`, `flagged_pct`, `by_element`,
  `by_year`, `by_flag` (`{count, label}`), `worst`. Region aggregate adds
  `station_count` + `worst_stations`. All-clean stations report **zeros, never
  `None`** (an empty summary still answers "how much was rejected? none").

## External libraries / binaries

- **`pymongo`** (pip, `mongodb` extra) — `QCSummaryStore` persist/readback in the
  handler layer only; `ghcn_qc.py` is pure stdlib (`csv`) with no I/O beyond reading
  the path it is handed.
- Charts are **dependency-free raw SVG** (`qc_chart.py`) — no matplotlib (not
  installed in the runners).

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `SummarizeQualityFlags` | event | external / moderate | Per-station QC rejection rate (overall %, per element/year/check) |
| `AggregateRegionQC` | event | io / cheap | Observation-weighted region rejection rate + worst-stations ranking |
| `RenderQCChart` | event | io / cheap | Per-element flagged-% SVG bar + self-contained HTML page |

Workflows: `SummarizeStationQuality`, `SummarizeRegionQuality`,
`VisualizeStationQuality`, `VisualizeRegionQuality`.

## Cache / output

- **Charts:** written through the storage abstraction to
  `cache/noaa-weather/qc-viz/<slug>/qc.svg` + `qc.html` (each sidecar-backed). Under
  `FW_STORAGE=s3` they land in MinIO. The output path is keyed on the stable
  station_id/region slug (so the URL stays constant) even though the title shows the
  human name.
- **Rollups:** MongoDB `weather_qc_stations` (per station) + `weather_qc_regions`
  (per region).

## Gotchas & notes

- **This does not change the trend** — it's a parallel credibility read over the same
  CSV. A high flagged-% doesn't invalidate a trend, it contextualizes it.
- **Region rate is observation-weighted, not a mean of percentages** — deliberately,
  so one small noisy station can't dominate the headline.
- Persistence is **best-effort**: a Mongo outage logs a warning and the per-station
  facet still returns its summary; `AggregateRegionQC` returns an empty aggregate
  rather than crashing.
- `RenderQCChart` prefers the summary's own `station_name`/`region` for the title and
  only falls back to the passed `title`/`label`.

## Related specs

- [ingest](ingest.md) — the CSV whose dropped rows this feature counts.
- [trends](trends.md) — the analysis that drops the flagged rows; QC is its
  credibility companion and reuses its per-station→region-readback pattern.
- [extremes](extremes.md) — the other feature built on the same pattern.
- [storage-and-cache](storage-and-cache.md) — the `qc-viz` chart write path.
