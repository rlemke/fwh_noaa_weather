# Station CSV Ingest

**Namespace:** `weather.Ingest` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Ingest`) ·
**Handler:** `src/noaa_weather/handlers/ingest/ingest_handlers.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/ghcn_download.py`,
`ghcn_parse.py`, `ghcn_mocks.py` · **CLI:** `tools/fetch-station-csv.sh`

## Overview

Ingest is the download tier: it pulls **one CSV per station** from the GHCN-Daily
public S3 bucket and warms the on-disk cache so every downstream analysis reads a
local file. The key data-model fact it exploits is that GHCN-Daily stores *all years
for a station in a single CSV* (`csv/by_station/<ID>.csv`) — so there is no per-year
loop, no random access, one download per station. It is the shared prerequisite for
[analysis](trends.md), [quality-control](quality-control.md), and
[extremes](extremes.md), each of which re-reads the same cached CSV.

## How it works

`handle_fetch_station_data` (`ingest_handlers.py`):

1. `download_station_csv(station_id)` (shim → `ghcn_download.download_station_csv`)
   returns a local path — a cache hit if the sidecar is valid, otherwise a
   **stream-and-hash** download: response streamed to a staged file under local
   scratch, SHA-256 computed while streaming, then `finalize_from_local` moves it
   atomically into `cache/noaa-weather/station-csv/<id>.csv` and the `.meta.json`
   sidecar is written **last** (readers treat a missing sidecar as "not present").
2. `parse_ghcn_csv(csv_path, start_year, end_year)` pivots the long CSV
   (`ID,DATE,ELEMENT,DATA_VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME`) into wide daily
   records, converting tenths → real units and skipping Q-flagged rows.
3. Counts records and distinct years and returns them.

Data shape: `long per-element CSV → wide daily records`. The count is the useful
signal; the durable side effect is the warmed CSV cache.

## Fan-out

**Single-task per station.** The facet downloads one station. Fan-out over many
stations happens one level up, in the `andThen foreach` workflows
(`CacheStateData`, `AnalyzeStateTrends`, …) — see [workflows](workflows.md).

## Data & fields

- Reads the five recognized elements `{TMAX, TMIN, PRCP, SNOW, SNWD}` (`_ELEMENT_SET`);
  other elements in the CSV are ignored.
- **Units:** `DATA_VALUE` is stored in tenths — tenths of °C for temperature, tenths
  of mm for `PRCP`/`SNOW`/`SNWD`; `parse_ghcn_csv` divides by 10 to return real
  °C / mm.
- **QC:** rows with a non-empty `Q_FLAG` are dropped by default (`skip_flagged=True`);
  the [quality-control](quality-control.md) feature exists precisely to *count* what
  ingest silently drops.
- **Year range:** `[start_year, end_year]` (defaults `1944`–`2026`) filters records
  during the parse (the download itself is whole-file).

## External libraries / binaries

- **`requests`** (pip, optional) — HTTP GET against
  `https://noaa-ghcn-pds.s3.amazonaws.com/`. If absent (or `use_mock=True`),
  `ghcn_mocks.mock_station_csv` produces deterministic offline data so tests and
  air-gapped runs work. `_resolve_use_mock` makes mock **opt-in**: with `requests`
  missing and mock off, the download raises rather than silently faking data.
- No binary dependency.

## Facets & workflows

| Facet | Kind | Effect / Cost | Signature |
|---|---|---|---|
| `FetchStationData` | event | external / moderate | `(station_id, start_year=1944, end_year=2026) => (record_count: Int, years_with_data: Int, station_id: String)` — with `RetryPolicy()` |

Consumed directly by workflow `weather.Cache.CacheStateData` (+ the whole `Cache*`
warmup family) and as the first step of `AnalyzeStation` / the `AnalyzeStateTrends`
foreach body.

## Cache / output

- Writes `cache/noaa-weather/station-csv/<station_id>.csv` + `.meta.json` (SHA-256,
  size, source URL, `used_mock`). CSVs **do not expire** — once cached, subsequent
  calls are cache hits unless `force=True`.
- Under `FW_STORAGE=s3` the CSV lands in MinIO/S3; readers get a real local file via
  `localize()` (the download result's `absolute_path` is always local-resolved). See
  [storage-and-cache](storage-and-cache.md).

## Gotchas & notes

- **One CSV can be hundreds of MB** of history — hence the stream-and-hash path
  rather than buffering in memory. Staging is always on local disk even when the
  durable cache is remote.
- **Mock data is deterministic but obvious** — the framework CLAUDE memory notes a
  uniform record count + zero Q-flags is the tell for a mock CSV; pass `--force` /
  live network for real data.
- **`record_count == 0`** usually means the station has no data in the requested year
  range, not a download failure — downstream analysis handlers guard for this and
  return empty summaries.

## Related specs

- [catalog-discovery](catalog-discovery.md) — produces the station IDs to fetch.
- [analysis-trends](trends.md), [quality-control](quality-control.md),
  [extremes](extremes.md) — all re-read the CSV this feature caches.
- [storage-and-cache](storage-and-cache.md) — the stage→finalize→sidecar write
  protocol and the `localize` read-through cache.
