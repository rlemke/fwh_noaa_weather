"""AgentPoller entry point for the noaa-weather example (legacy).

Usage:
    PYTHONPATH=src python agent.py     # from the repo root
"""

from __future__ import annotations

from facetwork.runtime.agent_poller import AgentPoller, AgentPollerConfig
from noaa_weather.handlers import register_all_handlers


def main() -> None:
    """Start the AgentPoller with all weather handlers."""
    poller = AgentPoller(config=AgentPollerConfig(service_name="noaa-weather"))
    register_all_handlers(poller)
    print("NOAA Weather AgentPoller started")
    poller.run()


if __name__ == "__main__":
    main()
