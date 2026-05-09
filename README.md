# noaa-weather

A standalone [Facetwork](https://github.com/rlemke/facetwork) example package
providing FFL workflows and handlers for working with NOAA climate data:

- **GHCN catalog** — discover and ingest Global Historical Climatology Network station data
- **NDBC buoys** — fetch and summarise National Data Buoy Center marine observations
- **ISD-Lite** — hourly station observations with offline mocks for tests
- **Climate analysis** — yearly state aggregates, multi-decade trends, linear regressions
- **Reverse geocoding** — Nominatim-backed station-to-place lookup with on-disk cache
- **Reporting** — per-station HTML reports, choropleth warming maps, batch summaries

Discovered by the Facetwork runner via the `facetwork.examples` entry point
declared in `pyproject.toml`. After `pip install -e .`, Facetwork's
`scripts/start-runner --example noaa-weather` and `scripts/seed-examples`
pick this package up automatically.

## Install

```bash
git clone https://github.com/rlemke/fwh_noaa_weather.git ~/fw_handlers/fwh_noaa_weather
cd ~/fw_handlers/fwh_noaa_weather
pip install -e .
```

This registers the package under the `facetwork.examples` entry-point group,
making it discoverable by any Facetwork installation in the same environment.

## Run from a Facetwork checkout

All commands below assume your shell is in the Facetwork checkout and the
noaa-weather package is installed in the same Python environment that runs
Facetwork (`pip install -e ~/fw_handlers/fwh_noaa_weather`).

### Cold start: dashboard + runner together

```bash
scripts/seed-examples --include noaa-weather           # one-time, seeds FFL
scripts/start-runner --example noaa-weather -- --log-format text
```

This brings up the dashboard on `:8080` and a runner that polls for
noaa-weather tasks.

### Add a runner to an already-running stack

If the Facetwork dashboard is already up and you just want another runner
attached to it (after pulling new noaa-weather code, or to scale out):

```bash
scripts/start-runner --example noaa-weather --no-dashboard -- --log-format text
```

## Run standalone

```bash
PYTHONPATH=src python agent.py
```

## Layout

```
fwh_noaa_weather/
├── pyproject.toml                  # facetwork.examples entry point
├── README.md
├── CLAUDE.md                       # guidance for Claude Code in this repo
├── USER_GUIDE.md                   # human-facing walkthrough
├── agent-spec/                     # tools-pattern, cache-layout specs
├── agent.py                        # standalone AgentPoller variant
├── conftest.py                     # pytest fixtures
├── tests/                          # repo-level integration tests
├── scripts/                        # operational scripts (seed-climate-data, …)
└── src/noaa_weather/
    ├── __init__.py                 # exports `example: ExamplePackage`
    ├── handlers/                   # 6 event-facet subpackages
    │   ├── analysis/
    │   ├── catalog/
    │   ├── geocode/
    │   ├── ingest/
    │   ├── marine/
    │   ├── report/
    │   └── shared/                 # ghcn_utils, weather_utils — shims into tools/_lib
    ├── ffl/                        # weather.ffl + compiled JSON
    └── tools/                      # CLI utilities (CLI .py + .sh wrappers + _lib/)
        ├── _lib/                   # download / parse / analysis / mocks / charts
        ├── *.py                    # discover-stations, fetch-station-csv, …
        └── *.sh                    # shell wrappers
```

The `tools/` dir gives every operation a CLI (e.g.
`download-ghcn-catalog.sh`, `fetch-station-csv.sh`,
`compute-region-trend.sh`); the FFL handlers call into the **same**
`tools/_lib/` modules via the `handlers/shared/<domain>_utils.py` shim,
so the two surfaces share one cache and one implementation.

## Required infrastructure

| Service | Purpose |
|---------|---------|
| MongoDB | Facetwork registry + workflow state, plus `weather_reports` / `climate_trends` collections |

The package falls back to deterministic hash-based mocks when `requests`
isn't installed or NOAA endpoints are unreachable, so unit tests run
fully offline. See `USER_GUIDE.md` for the end-to-end walkthrough.

## License

Apache 2.0 — see `LICENSE`.
