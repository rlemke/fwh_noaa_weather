# Element Vocabulary (NL → GHCN code)

**Namespace:** `weather.Vocab` ·
**FFL:** `src/noaa_weather/ffl/weather.ffl` (§ `namespace weather.Vocab`) ·
**Handler:** `src/noaa_weather/handlers/vocab/vocab_handlers.py` ·
**Tests:** `src/noaa_weather/handlers/vocab/tests/test_vocab_handlers.py`

## Overview

Vocab is the **semantic-discovery** primitive for this domain — the weather analogue
of `osm.Vocab.ResolveTag`. It maps a natural-language phrase like "max temperature",
"rainfall", or "snow depth" to its 4-char GHCN-Daily element code (`TMAX`, `TMIN`,
`PRCP`, `SNOW`, `SNWD`) so an LLM composing a workflow can fill a facet's
`required_elements` by intent instead of memorising codes. It is a pure, in-process
lookup — no network, no I/O, no MongoDB.

## How it works

`vocab_handlers.py` holds a static `_ELEMENTS` table (code → description + synonym
phrases). `ResolveElement` tokenises the term and scores it against each element's
code + description + synonyms (`_score`: exact token-set match = 1.0, partial overlap
scaled up to 0.95), returning the best code, a rounded confidence, and the
description. `ListElements` returns the whole known set. The supported set mirrors the
parser's `ghcn_parse._ELEMENT_SET`, so vocab can only resolve elements the pipeline can
actually parse.

## Fan-out

**Single-task — no fan-out.** Pure lookups.

## Data & fields

- **Codes + synonyms:** `TMAX` (max/high temperature, highs, hottest, heat),
  `TMIN` (min/low temperature, lows, coldest, cold), `PRCP` (precipitation, precip,
  rain, rainfall, wet), `SNOW` (snow, snowfall, fresh snow), `SNWD` (snow depth,
  snowpack, snow on ground, snow cover).
- `ResolveElement` returns the `weather.Vocab.ElementResolution` schema:
  `{element, confidence, description}`; an unmatched term returns `("", 0.0, "")`.
- `ListElements` returns `elements` (JSON array of `{element, description}`) + `count`.

## External libraries / binaries

- **stdlib only** (`re`, `json`). No network, no pip deps, no binaries. `Json` returns
  are emitted as JSON strings per the fleet convention.

## Facets & workflows

| Facet | Kind | Effect / Cost | Purpose |
|---|---|---|---|
| `ResolveElement` | event | **pure** / free | NL term → GHCN element code + confidence + description |
| `ListElements` | event | **pure** / free | List the known GHCN element codes as JSON |

Both carry `with Effect(kind="pure") with Cost(tier="free")` — the composer should
prefer them freely. They are event facets (need a handler) but do no I/O.

## Cache / output

**No cache, no output artifact** — returns inline. Nothing is written to disk or Mongo.

## Gotchas & notes

- **Scope is exactly the five parsed elements.** Asking for "wind" or "pressure"
  resolves to nothing (returns `("", 0.0, "")`) — those aren't in `_ELEMENT_SET`, and
  resolving them would be a lie about what the pipeline can analyze.
- Confidence is a token-overlap heuristic, not a learned model; treat it as a rough
  tiebreak, not a probability.

## Related specs

- [catalog-discovery](catalog-discovery.md) — the consumer: `required_elements` on
  `DiscoverStations` is what Vocab is meant to fill.
- [trends](trends.md), [quality-control](quality-control.md), [extremes](extremes.md)
  — all operate on the same `_ELEMENT_SET` codes Vocab resolves to.
