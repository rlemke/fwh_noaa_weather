"""NOAA weather example package — Facetwork workflows + handlers for
GHCN station data, NDBC marine buoys, ISD-Lite hourly observations,
climate trend analysis, and HTML/map reporting.

Discovered by the Facetwork runner via the ``facetwork.domains`` entry
point declared in ``pyproject.toml``::

    [project.entry-points."facetwork.domains"]
    noaa-weather = "noaa_weather:domain"

Once ``pip install -e .`` has been run from this repository, Facetwork's
``scripts/start-runner --example noaa-weather`` and
``scripts/seed-examples`` will pick this package up automatically — no
edits to the Facetwork repository required.
"""

from __future__ import annotations

from pathlib import Path

from facetwork.domains import DomainPackage

from .handlers import register_all_registry_handlers

domain = DomainPackage(
    name="noaa-weather",
    ffl_dir=Path(__file__).parent / "ffl",
    register_handlers=register_all_registry_handlers,
)
