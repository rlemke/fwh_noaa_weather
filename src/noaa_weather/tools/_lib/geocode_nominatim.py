"""Reverse-geocoding via OSM Nominatim with sidecar-cached results.

Rate limiting: Nominatim's usage policy caps anonymous use at 1 req/sec,
so this module sleeps 1 second after every successful live call. Cache
hits don't sleep.

Cache layout:
    cache/noaa-weather/geocode/<lat_rounded>_<lon_rounded>.json + .meta.json

Lat/lon are rounded to 4 decimal places (~11 m) for the key so nearby
queries coalesce.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from . import ghcn_mocks, sidecar  # noqa: E402
from .storage import LocalStorage, Storage  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("noaa-weather.geocode")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "geocode"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "facetwork-noaa-weather/1.0"
REQUEST_TIMEOUT = 10
RATE_LIMIT_SECONDS = 1.0


def _cache_key(lat: float, lon: float) -> str:
    return f"{lat:.4f}_{lon:.4f}"


def reverse_geocode(
    lat: float,
    lon: float,
    *,
    force: bool = False,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """Reverse geocode ``(lat, lon)`` to a place dict.

    Returns a dict with keys ``display_name``, ``city``, ``state``,
    ``country``, ``county``. Values may be empty strings when Nominatim
    has no data for the location, but the keys are always present.

    Cache is consulted first (unless ``force=True``). On live lookups,
    the result is written to the cache before return; subsequent calls
    skip the network.
    """
    key = _cache_key(lat, lon)
    relative_path = f"{key}.json"
    s = storage or LocalStorage()

    if not force:
        cached = _read_cached(relative_path, s)
        if cached is not None:
            return cached

    # Mock is opt-in. Default (use_mock is None) is a real lookup.
    if use_mock:
        result = _mock_result(lat, lon)
        _write_cached(relative_path, result, s, source="mock")
        return result

    if requests is None:
        raise RuntimeError(
            "reverse_geocode: requests library is not installed. "
            "Install it, run via the .sh wrapper (activates .venv), "
            "or pass use_mock=True (--use-mock at the CLI) if deterministic "
            "mock data is acceptable."
        )

    # No silent fallback on network / API errors — callers need to see the
    # failure so the cache isn't polluted with mock_fallback entries.
    resp = requests.get(
        NOMINATIM_URL,
        params={"lat": lat, "lon": lon, "format": "json"},
        headers={"User-Agent": USER_AGENT},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    addr = data.get("address", {})
    result = {
        "display_name": data.get("display_name", ""),
        "city": addr.get("city", addr.get("town", addr.get("village", ""))),
        "state": addr.get("state", ""),
        "country": addr.get("country", ""),
        "county": addr.get("county", ""),
    }
    _write_cached(relative_path, result, s, source="nominatim")
    # Enforce Nominatim rate limit only after successful live calls.
    time.sleep(RATE_LIMIT_SECONDS)
    return result


def _read_cached(relative_path: str, storage: Storage) -> dict[str, Any] | None:
    if not sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, relative_path, storage):
        return None
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, storage)
    try:
        with open(art_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Corrupt geocode cache %s: %s", art_path, exc)
        return None


def _write_cached(
    relative_path: str,
    result: dict[str, Any],
    storage: Storage,
    *,
    source: str,
) -> None:
    """Write ``result`` + sidecar atomically."""
    import hashlib

    body = json.dumps(result, indent=2, sort_keys=True) + "\n"
    body_bytes = body.encode("utf-8")

    staging_root = sidecar.staging_dir(NAMESPACE, CACHE_TYPE, storage)
    os.makedirs(staging_root, exist_ok=True)
    stage_path = os.path.join(staging_root, f"{relative_path}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body_bytes)

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=len(body_bytes),
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            source={"provider": source},
            tool={"name": "geocode_nominatim", "version": "1.0"},
            storage=storage,
        )


def _mock_result(lat: float, lon: float) -> dict[str, Any]:
    """Deterministic fallback when Nominatim is unavailable."""
    seed = f"geo:{lat}:{lon}"
    return {
        "display_name": f"Location at {lat:.2f}, {lon:.2f}",
        "city": f"City-{ghcn_mocks.hash_int(seed + ':city', 1000, 9999)}",
        "state": f"State-{ghcn_mocks.hash_int(seed + ':state', 10, 99)}",
        "country": "US",
        "county": f"County-{ghcn_mocks.hash_int(seed + ':county', 100, 999)}",
    }
