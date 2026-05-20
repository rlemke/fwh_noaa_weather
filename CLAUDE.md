# CLAUDE.md — noaa-weather

This repository is a **standalone Facetwork example package**. The Facetwork
platform (workflow compiler + runtime) lives at
`/Users/ralph_lemke/facetwork`; this repo only contains the NOAA-specific
FFL, handlers, and tools. The two are wired together via the
`facetwork.examples` entry point in `pyproject.toml`.

## Quick orientation

```
fwh_noaa_weather/
├── pyproject.toml                  # declares the facetwork.examples entry point
├── src/noaa_weather/__init__.py    # exports `example: ExamplePackage`
├── src/noaa_weather/handlers/      # event-facet implementations (6 subpackages)
├── src/noaa_weather/ffl/           # top-level FFL workflows
├── src/noaa_weather/tools/         # CLI utilities + _noaa_tools/ (the real implementation)
├── tests/                          # repo-level integration tests
└── agent-spec/                     # cross-cutting design specs
```

## Common operations

```bash
# Register this package with Facetwork's runner
pip install -e .

# From a Facetwork checkout:
scripts/seed-examples --include noaa-weather
scripts/start-runner --example noaa-weather -- --log-format text

# Run as a standalone agent (skip the registry runner path):
PYTHONPATH=src python agent.py

# CLIs (call the same _noaa_tools/ as the handlers — see Tools pattern below):
src/noaa_weather/tools/discover-stations.sh --country US --max 50
src/noaa_weather/tools/fetch-station-csv.sh USC00010008
src/noaa_weather/tools/compute-region-trend.sh --state CA --start 1950 --end 2024
src/noaa_weather/tools/climate-report.sh --state CA

# Tests
pytest tests/ src/noaa_weather/handlers/ -v
```

## Key concepts

### Tools / handlers / cache pattern

Every operation has two surfaces — a CLI under `src/noaa_weather/tools/`
and an FFL handler under `src/noaa_weather/handlers/<domain>/` — and both
call into the **same** implementation in `src/noaa_weather/tools/_noaa_tools/`.
This is the Facetwork canonical pattern (see
`agent-spec/tools-pattern.agent-spec.yaml`).

```
                       ┌────────────────────────┐
   CLI tool ───────────┤                        │
                       │   tools/_noaa_tools/X.py      │ ← single source of truth
   FFL handler ────────┤   (download / parse /  │
   (via shared shim)   │    analysis / mocks)   │
                       └────────────────────────┘
```

The shim lives at `src/noaa_weather/handlers/shared/ghcn_utils.py`. It
adds `tools/` to `sys.path` and re-exports `_noaa_tools` symbols, plus wraps
the new `_noaa_tools` APIs in legacy-shaped helpers (`download_station_catalog`,
`download_station_csv`, `reverse_geocode_nominatim`) so handlers don't
need to track refactors of the underlying library.

The `_noaa_tools` modules **must not** depend on `pymongo` or other handler-only
infrastructure — that way the CLIs run standalone, without a Mongo
cluster. MongoDB-backed storage (`WeatherReportStore`, `ClimateStore`)
lives in the shim.

### Cache layout

Both the CLIs and the FFL handlers read/write the same on-disk cache:

```
$AFL_DATA_ROOT/cache/noaa-weather/
├── ghcn-catalog/                   # ghcnd-stations.txt, ghcnd-inventory.txt
├── ghcn-stations/                  # per-station CSVs (one per station_id)
├── ndbc-catalog/                   # NDBC station list
├── ndbc-stations/                  # per-buoy historical CSVs
├── geocode/                        # Nominatim reverse-lookup cache (lat,lon)
└── reports/                        # rendered HTML + maps (also mirrored to MongoDB)
```

Each cached entry has a `.meta.json` sidecar describing its provenance
(source URL, fetched_at, checksum) — see
`agent-spec/cache-layout.agent-spec.yaml`.

### Handler subpackages

| Subpackage | Domain |
|------------|--------|
| `catalog/` | Station catalog discovery (GHCN inventory + filtering) |
| `ingest/` | Per-station CSV download + parse |
| `analysis/` | Yearly summaries, regional trends, linear regression |
| `geocode/` | Reverse-geocoding via Nominatim |
| `marine/` | NDBC buoy catalog + observations |
| `report/` | Per-station HTML, batch summaries, warming maps |

Each module exposes `register_handlers(runner)` for the RegistryRunner;
all six are wired into `register_all_registry_handlers` in
`src/noaa_weather/handlers/__init__.py`.

## Adding new handlers

1. Add a Python module under `src/noaa_weather/handlers/<domain>/`.
2. Export `register_handlers(runner)` that calls
   `runner.register_handler(facet_name=..., module_uri=f"file://{os.path.abspath(__file__)}", entrypoint=...)`.
3. Wire it into `register_all_registry_handlers` in
   `src/noaa_weather/handlers/__init__.py`.
4. Drop the FFL declaration into `src/noaa_weather/ffl/` (or a domain-specific
   `handlers/<domain>/ffl/` for nested workflows).
5. If the handler does anything non-trivial, factor that work into a
   `tools/_noaa_tools/<name>.py` module, add a CLI wrapper under `tools/`, and
   re-export from `handlers/shared/ghcn_utils.py`.
6. Re-run `scripts/seed-examples --include noaa-weather` so the new flow
   shows up in the dashboard.

## Code review checklist

- For every state transition: "what if this crashes halfway?" Design the recovery path.
- For every download: cache + sidecar + max-age check, with deterministic mock fallback.
- For every retry: max count and backoff. No infinite loops.
- For every error handler: never silently return empty defaults. Fail explicitly or re-raise.
- Keep `_noaa_tools/` free of `pymongo` / handler-only deps so CLIs stay runnable standalone.

## Domain research before implementation

For NOAA / climate work, apply established practices:
- GHCN station IDs are 11 chars (`USCMM######`); inventory is fixed-width text.
- ISD-Lite is fixed-width with `-9999` for missing values; temperatures, pressures, wind speeds are scaled ×10.
- NDBC standard meteorological files are space-delimited with a 2-line header.
- Nominatim has a strict 1 req/sec rate limit — the `_noaa_tools.geocode_nominatim` helper enforces this.
- Reverse-geocoding lat/lon → place: cache aggressively (results never change for a given lat/lon).
