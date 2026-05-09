"""RegistryRunner entry point for the noaa-weather example.

Usage:
    PYTHONPATH=src python agent_registry.py     # from the repo root
"""

from __future__ import annotations

from facetwork.runtime.registry_runner import create_registry_runner
from noaa_weather.handlers import register_all_registry_handlers


def main() -> None:
    """Start the RegistryRunner with all weather handlers."""
    runner = create_registry_runner("noaa-weather", topics=["weather.*"])
    register_all_registry_handlers(runner)
    print(f"NOAA Weather RegistryRunner started with {len(runner.registered_names())} handlers")
    runner.start()


if __name__ == "__main__":
    main()
