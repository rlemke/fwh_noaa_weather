# noaa-weather — Feature Specifications

This directory holds one **spec per feature** of the noaa-weather domain package.
Each document follows a common shape ([`SPEC_TEMPLATE.md`](SPEC_TEMPLATE.md)) and
states, for that feature: how it works, whether and how it **fans out** across the
fleet, what **data & fields** it operates on, the **external libraries** it relies on,
its **facets & workflows**, and its **cache/output**. Claims are grounded in the FFL
`/** … */` docstrings (`src/noaa_weather/ffl/weather.ffl`), the handler code
(`src/noaa_weather/handlers/`), and the shared tool libraries
(`src/noaa_weather/tools/_noaa_tools/`) — the source of truth for each facet remains
its FFL docstring; these specs are the feature-level narrative over them.

**Start here:** [**Climate Analysis & Regional Trends**](trends.md) — the flagship
feature (the multi-decade warming/precip/snow trend the package is named for) and the
canonical *per-station → persist → region-readback* pattern that QC and extremes both
reuse.

## Ingest pipeline

| Spec | What it covers |
|------|----------------|
| [catalog-discovery.md](catalog-discovery.md) | `weather.Catalog.DiscoverStations` — catalog-first station discovery (GHCN inventory verification, US-state / Geofabrik-region / element / coverage filters). The first step of every fan-out. |
| [ingest.md](ingest.md) | `weather.Ingest.FetchStationData` — one CSV per station from GHCN-Daily S3, stream-and-hash download, tenths→units parse, QC-flag drop. |

## Analysis

| Spec | What it covers |
|------|----------------|
| [trends.md](trends.md) | **Flagship.** `weather.Analysis` — per-station yearly/monthly summaries → regional warming/precip/**snowfall** trend via OLS; the persist-then-region-readback pattern; the `station_count` dependency signal. |
| [quality-control.md](quality-control.md) | `weather.QC` — count the Q-flagged observations the analysis drops (overall %, per element/year/check), observation-weighted region rate + worst-stations, dependency-free SVG/HTML chart. |
| [extremes.md](extremes.md) | `weather.Extremes` — heat waves / cold snaps / wet & dry spells / heavy rain & snow, per-decade frequency + region decadal trends, SVG/HTML chart. |

## Marine

| Spec | What it covers |
|------|----------------|
| [marine.md](marine.md) | `weather.Marine` — NDBC buoy catalog → discover → stdmet fetch → yearly buoy-climate summary → MapLibre points map colour-coded by type. |

## Enrichment & discovery

| Spec | What it covers |
|------|----------------|
| [geocode.md](geocode.md) | `weather.Geocode.ReverseGeocode` — lat/lon → place via OSM Nominatim, 1 req/sec rate limit, aggressive on-disk cache. |
| [vocab.md](vocab.md) | `weather.Vocab` — NL term → GHCN element code (`TMAX`/`TMIN`/`PRCP`/`SNOW`/`SNWD`); pure/free semantic-discovery primitive. |

## Reporting & composition

| Spec | What it covers |
|------|----------------|
| [climate-report.md](climate-report.md) | `weather.Report` — self-contained regional climate report bundle (JSON + Markdown + HTML + 5 SVGs, WMO normals / warming stripes / climograph); Geofabrik sub-region enumeration. |
| [workflows.md](workflows.md) | `weather.workflows` + `weather.Cache` — the entry-point workflows, the discover→fan-out→aggregate shape, `catch` degradation, cache-warmup family, and continent-scale wrappers. |

## Cross-cutting

| Spec | What it covers |
|------|----------------|
| [storage-and-cache.md](storage-and-cache.md) | The `local`/`hdfs`/`s3` storage backends, `FW_STORAGE=s3` shared MinIO, the sidecar cache protocol, `localize` read-through cache, and the always-local scratch rule. |

---

*See also the machine-readable capability manifest at
[`src/noaa_weather/catalog.yaml`](../src/noaa_weather/catalog.yaml) (workflows +
facets by intent), the repo [`CLAUDE.md`](../CLAUDE.md) (domain contract), the
[`USER_GUIDE.md`](../USER_GUIDE.md) (human walkthrough), and the tools/handlers/cache
contract in [`agent-spec/tools-pattern.agent-spec.yaml`](../agent-spec/tools-pattern.agent-spec.yaml).
The live/queryable interface is the MCP `fw_capabilities` / `fw_catalog_search` /
`fw_describe_handler` tools.*
