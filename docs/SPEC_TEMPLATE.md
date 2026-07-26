<!-- SPEC TEMPLATE — every docs/<feature>.md follows this shape so the set reads
consistently. Delete this comment in real specs. Keep sections in this order;
omit a section only if it genuinely does not apply (say so in one line rather
than dropping the heading silently). Ground every claim in the actual FFL
docstrings / handler code / tools — do not invent behaviour. -->

# <Feature Name>

**Namespace(s):** `weather.<ns>` · **FFL:** `src/noaa_weather/ffl/weather.ffl` ·
**Handlers:** `src/noaa_weather/handlers/<dir>/*.py` ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/<...>.py` (if any)

## Overview
One or two paragraphs: what this feature is for, the request it answers, and where
it sits in the pipeline (discover → fetch → parse → analyze → render, etc.).

## How it works
The algorithm / data flow, step by step. Name the concrete steps and the shape of
the data at each (fixed-width catalog → station list → per-station CSV → wide daily
records → yearly/monthly aggregates → trend/JSON/HTML). Note the tools/handler
shared-library split (the same `_noaa_tools/` core behind the CLI and the FFL
handler).

## Fan-out
Does it fan out across the fleet? If yes: what is the fan-out unit (per-station /
per-state / per-region / per-country), which workflow drives it (an `andThen
foreach` over what list), and why it reduces wall-clock. If it is single-task, say
"single-task — no fan-out" and why.

## Data & fields
What data it reads and which GHCN-Daily elements / NDBC fields / schema fields it
operates on — be specific (`TMAX`, `TMIN`, `PRCP`, `SNOW`, `SNWD`; `Q_FLAG` letters;
NDBC `sea_temp`/`wave_height`; the `StationInfo`/`YearlyClimate`/`ClimateTrend`
schemas). Name the filter/selection mechanism (inventory element check, US-state
bbox, Geofabrik region bbox, year range). If the feature does no filtering, say so.

## External libraries / binaries
Every non-stdlib dependency this feature relies on and what for — e.g. `requests`
(HTTP downloads, optional — falls back to deterministic mocks), `pymongo`
(handler-layer persistence only), `PyYAML`, `folium`/`shapely` (the `maps` extra).
Distinguish a **binary** dependency from a **pip** one, and note where a dependency
is optional (mock fallback / `maps` extra).

## Facets & workflows
The key event facets and workflows, with signatures and a one-line purpose taken
from the FFL docstrings. Mark event facets (need a handler) vs pure facets, and
note `Effect`/`Cost` mixins where present (`with Effect(kind="pure"|"external"|"io")`
/ `with Cost(tier="free".."expensive")`).

## Cache / output
The cache namespace under `cache/noaa-weather/<cache_type>/` and the cache type,
plus the output artifact(s) and format (station CSV / GeoJSON / HTML report / SVG
chart / MapLibre HTML / buoy summary JSON). Note the `.meta.json` sidecar, and
whether outputs go to local disk, MinIO/S3 (`FW_STORAGE=s3`), or MongoDB.

## Gotchas & notes
Known pitfalls, rate limits, sensitivity caveats, or non-obvious constraints
(worth capturing anything a future maintainer would trip on).

## Related specs
Links to the specs this feature composes with or depends on.
