"""Root conftest for the noaa-weather repo.

The package is installed editable (``pip install -e .``) so handler
modules resolve via the standard ``noaa_weather.handlers.*`` path —
no ``sys.path`` gymnastics required.
"""

from __future__ import annotations
