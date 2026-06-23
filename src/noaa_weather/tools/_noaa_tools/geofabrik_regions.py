"""Resolve Geofabrik region paths to lat/lon bounding boxes.

Lets noaa-weather tools filter GHCN stations by the same region
vocabulary the OSM tools use (``europe/germany``, ``north-america/us/
california``, etc.) without having to pull in the full osm-geocoder
library.

Design notes:

- The authoritative list of Geofabrik regions + geometries lives at
  ``https://download.geofabrik.de/index-v1.json`` (~1–2 MB). We cache
  it like any other artifact: ``cache/noaa-weather/geofabrik/
  index-v1.json`` with a ``.meta.json`` sidecar.
- A region's "path" is built by walking parents back to root:
  ``europe/germany/berlin`` is the feature whose id is ``berlin`` whose
  parent is ``germany`` whose parent is ``europe``. We index every
  feature by its full path for O(1) lookup.
- A bbox is min/max of the polygon's vertex coordinates. Faster and
  simpler than shapely point-in-polygon, sloppy at the edges — that's
  a known tradeoff documented in the tools README.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from . import sidecar  # noqa: E402
from .storage import Storage, get_storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("noaa-weather.geofabrik")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "geofabrik"
INDEX_RELATIVE_PATH = "index-v1.json"
INDEX_URL = "https://download.geofabrik.de/index-v1.json"
USER_AGENT = "facetwork-noaa-weather/1.0"
DEFAULT_MAX_AGE_HOURS = 24.0 * 14  # two weeks — the region list rarely shifts
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 120


# (min_lat, max_lat, min_lon, max_lon). Same shape as ghcn_parse.US_STATE_BOUNDS
# so downstream filtering code can be uniform.
Bbox = tuple[float, float, float, float]


@dataclass
class RegionInfo:
    """A resolved Geofabrik region."""

    path: str
    name: str
    iso_alpha2: list[str]
    parent: str | None
    bbox: Bbox


_cache_lock = threading.Lock()
_cached_index: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------

def resolve_region(
    path: str,
    *,
    force: bool = False,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> RegionInfo:
    """Look up a Geofabrik region by its full path.

    ``path`` examples: ``europe/germany``, ``north-america/us/california``.
    Leading / trailing slashes are tolerated.
    """
    key = path.strip().strip("/").lower()
    index = _load_index(force=force, storage=storage, use_mock=use_mock)
    feature = index.get(key)
    if feature is None:
        close = _suggest_close(index, key)
        hint = f" (did you mean: {', '.join(close)}?)" if close else ""
        raise KeyError(f"Geofabrik region {path!r} not found{hint}")
    return _to_region_info(key, feature)


def region_bbox(
    path: str,
    *,
    force: bool = False,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> Bbox:
    """Shortcut returning just the bbox tuple."""
    return resolve_region(
        path, force=force, storage=storage, use_mock=use_mock
    ).bbox


# FIPS country-code → Geofabrik path prefix. Used by ``resolve_state_bbox``
# to try sub-country lookups when the caller passes ``--country XX --state
# YY`` for non-US countries. Limited to countries whose Geofabrik index
# actually carries sub-region PBFs at time of writing — the lookup
# gracefully falls through to ``None`` for anything else.
FIPS_TO_GEOFABRIK_PREFIX: dict[str, str] = {
    "US": "north-america/us",
    "CA": "north-america/canada",
    "MX": "north-america/mexico",
    "GM": "europe/germany",
    "FR": "europe/france",
    "UK": "europe/great-britain",
    "IT": "europe/italy",
    "SP": "europe/spain",
    "PL": "europe/poland",
    "RS": "russia",
    "AS": "australia-oceania/australia",
    "NZ": "australia-oceania/new-zealand",
    "BR": "south-america/brazil",
    "IN": "asia/india",
    "CH": "asia/china",
    "JA": "asia/japan",
}


def resolve_state_bbox(
    country: str,
    state: str,
    *,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> tuple[Bbox | None, str]:
    """Return ``(bbox, resolved_path)`` for a country+state if resolvable.

    Tries Geofabrik sub-region lookup (``<prefix>/<state-slug>``) for
    non-US countries whose Geofabrik index has per-state PBFs. Examples:

    - ``("CA", "Ontario")`` → ``north-america/canada/ontario`` (if that
      path exists in the cached index).
    - ``("GM", "Bayern")``  → ``europe/germany/bayern``.
    - ``("AS", "NSW")``     → no match (would need the full
      ``new-south-wales`` slug — callers are better off passing
      ``--region australia-oceania/australia/new-south-wales``
      directly).

    Returns ``(None, "")`` when the state can't be resolved — callers
    should then fall back to the caller-specified filter path (empty
    station set for country+state mode) or raise.

    The US state short path stays with ``ghcn_parse.US_STATE_BOUNDS``;
    this helper is a fallback for everything else. Callers pick which
    lookup to try first.
    """
    prefix = FIPS_TO_GEOFABRIK_PREFIX.get(country.upper())
    if not prefix:
        return None, ""
    # Geofabrik region slugs are lowercase with hyphens.
    slug = state.strip().lower().replace(" ", "-").replace("_", "-")
    if not slug:
        return None, ""
    path = f"{prefix}/{slug}"
    try:
        info = resolve_region(path, storage=storage, use_mock=use_mock)
    except KeyError:
        return None, ""
    return info.bbox, path


def point_in_bbox(lat: float, lon: float, bbox: Bbox) -> bool:
    """True if ``(lat, lon)`` falls inside the bbox."""
    min_lat, max_lat, min_lon, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def get_geometry(
    path: str,
    *,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> dict[str, Any]:
    """Return the raw GeoJSON geometry dict for a Geofabrik region.

    Used by the warming-map choropleth, which needs the actual polygon
    (not just the bbox that :func:`resolve_region` returns). The
    returned dict is a GeoJSON ``{"type": "Polygon" | "MultiPolygon",
    "coordinates": [...]}`` object. Raises ``KeyError`` on unknown
    paths — same contract as ``resolve_region``.
    """
    key = path.strip().strip("/").lower()
    index = _load_index(force=False, storage=storage, use_mock=use_mock)
    feature = index.get(key)
    if feature is None:
        raise KeyError(f"Geofabrik region {path!r} not found")
    geom = feature.get("geometry") or {}
    if not geom.get("type") or not geom.get("coordinates"):
        raise ValueError(f"Geofabrik feature for {path!r} has no geometry")
    return geom


def list_regions_under(
    prefix: str,
    *,
    include_parents: bool = False,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> list[str]:
    """Return every Geofabrik region path under ``prefix``, sorted.

    - ``prefix`` = ``""`` → every region in the index.
    - ``prefix`` = ``"north-america/canada"`` → every path that is
      either ``north-america/canada`` (only if ``include_parents``)
      or starts with ``north-america/canada/``.
    - ``include_parents=False`` (default) additionally strips any
      region that has a child in the result set — i.e. keeps only
      leaf regions. ``include_parents=True`` returns every matching
      path, including the prefix itself and every intermediate level.

    Useful for "report on Canada and every province" workflows:
    ``list_regions_under("north-america/canada", include_parents=True)``.
    """
    index = _load_index(force=False, storage=storage, use_mock=use_mock)
    p = prefix.strip().strip("/").lower()
    if p:
        pref = p + "/"
        matched = [
            path for path in index
            if path == p or path.startswith(pref)
        ]
    else:
        matched = list(index)

    if include_parents:
        return sorted(matched)

    # Leaves only — drop paths that are prefixes of another matched path.
    matched_set = set(matched)
    leaves: list[str] = []
    for path in matched:
        child_prefix = path + "/"
        if any(
            other != path and other.startswith(child_prefix)
            for other in matched_set
        ):
            continue
        leaves.append(path)
    return sorted(leaves)


# ---------------------------------------------------------------------------
# Index fetch / cache.
# ---------------------------------------------------------------------------

def _load_index(
    *,
    force: bool,
    storage: Storage | None,
    use_mock: bool | None,
) -> dict[str, Any]:
    """Return ``{full_path: feature_dict}``, fetching/caching as needed."""
    global _cached_index

    with _cache_lock:
        if not force and _cached_index is not None:
            return _cached_index

        s = storage or get_storage()
        should_mock = _resolve_use_mock(use_mock)

        art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, INDEX_RELATIVE_PATH, s)

        if (
            not force
            and not should_mock
            and sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, INDEX_RELATIVE_PATH, s)
        ):
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, INDEX_RELATIVE_PATH, s)
            age = _age_hours(side.get("generated_at") if side else None)
            if age is None or age < DEFAULT_MAX_AGE_HOURS:
                logger.info(
                    "Geofabrik index cache hit (%.1fh old)",
                    age if age is not None else -1.0,
                )
                # localize: s3:// -> a real local file for open(); local -> itself.
                with open(s.localize(art_path), "r", encoding="utf-8") as f:
                    raw = json.load(f)
                _cached_index = _build_index(raw)
                return _cached_index

        if should_mock:
            raw = _mock_index()
            _persist(raw, s, used_mock=True)
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock if "
                    "deterministic mock data is acceptable."
                )
            logger.info("Downloading Geofabrik index from %s", INDEX_URL)
            resp = requests.get(
                INDEX_URL,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            raw = resp.json()
            _persist(raw, s, used_mock=False)

        _cached_index = _build_index(raw)
        return _cached_index


def _persist(raw: dict[str, Any], storage: Storage, *, used_mock: bool) -> None:
    body = json.dumps(raw).encode("utf-8")
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{INDEX_RELATIVE_PATH}.stage-{os.getpid()}")
    with open(stage_path, "wb") as f:
        f.write(body)

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, INDEX_RELATIVE_PATH, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, INDEX_RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            INDEX_RELATIVE_PATH,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={"publisher": "Geofabrik", "url": INDEX_URL, "used_mock": used_mock},
            tool={"name": "geofabrik_regions", "version": "1.0"},
            storage=storage,
        )


def _build_index(raw: dict[str, Any]) -> dict[str, Any]:
    """Walk the GeoJSON FeatureCollection and map every feature to its full path.

    A feature's full path is its ``id`` prefixed by its parents' ids, joined
    with ``/``. The mapping is lowercase so callers can be case-insensitive.
    """
    features = raw.get("features", [])
    by_id: dict[str, dict[str, Any]] = {}
    for feat in features:
        props = feat.get("properties") or {}
        fid = props.get("id")
        if fid:
            by_id[fid] = feat

    path_index: dict[str, dict[str, Any]] = {}
    for fid, feat in by_id.items():
        full = _full_path(fid, by_id)
        if full is None:
            continue
        path_index[full.lower()] = feat
    return path_index


def _full_path(fid: str, by_id: dict[str, dict[str, Any]]) -> str | None:
    """Walk parents back to the root. Returns None on a cycle or bad parent."""
    parts: list[str] = []
    seen: set[str] = set()
    current: str | None = fid
    while current:
        if current in seen:
            return None  # cycle
        seen.add(current)
        parts.append(current)
        feat = by_id.get(current)
        if feat is None:
            break
        parent = (feat.get("properties") or {}).get("parent")
        current = parent
    return "/".join(reversed(parts))


def _to_region_info(full_path: str, feature: dict[str, Any]) -> RegionInfo:
    props = feature.get("properties") or {}
    iso = props.get("iso3166-1:alpha2") or []
    if isinstance(iso, str):
        iso = [iso]
    return RegionInfo(
        path=full_path,
        name=props.get("name", ""),
        iso_alpha2=list(iso),
        parent=props.get("parent"),
        bbox=_geometry_bbox(feature.get("geometry") or {}),
    )


def _geometry_bbox(geom: dict[str, Any]) -> Bbox:
    """Compute bbox from GeoJSON Polygon / MultiPolygon coordinates.

    Geofabrik geometries are polygons or multipolygons of country-scale
    shapes — typically thousands of vertices. Linear scan is fine.
    """
    min_lat = 90.0
    max_lat = -90.0
    min_lon = 180.0
    max_lon = -180.0
    updated = False

    def _visit(pt: list[float]) -> None:
        nonlocal min_lat, max_lat, min_lon, max_lon, updated
        if len(pt) < 2:
            return
        lon, lat = pt[0], pt[1]
        min_lat = min(min_lat, lat)
        max_lat = max(max_lat, lat)
        min_lon = min(min_lon, lon)
        max_lon = max(max_lon, lon)
        updated = True

    coords = geom.get("coordinates") or []
    gtype = geom.get("type", "")
    if gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                _visit(pt)
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                for pt in ring:
                    _visit(pt)
    else:
        # Unknown geometry type — Geofabrik sometimes publishes just a
        # bbox property. Fall back to that if the geometry is unusable.
        pass

    if not updated:
        raise ValueError("Geofabrik feature has no usable polygon coordinates")
    return (min_lat, max_lat, min_lon, max_lon)


def _suggest_close(index: dict[str, Any], query: str) -> list[str]:
    """Cheap suggestion list for typos — no difflib overhead."""
    tail = query.rsplit("/", 1)[-1]
    if not tail:
        return []
    return sorted(
        path for path in index if path.rsplit("/", 1)[-1] == tail
    )[:5]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _age_hours(generated_at: str | None) -> float | None:
    if not generated_at:
        return None
    from datetime import datetime, timezone

    try:
        ts = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def _resolve_use_mock(explicit: bool | None) -> bool:
    """Mock is opt-in. Default (``explicit`` is None) is False."""
    return bool(explicit)


def _mock_index() -> dict[str, Any]:
    """Small hand-crafted index for offline tests.

    Covers just enough regions that the mock GHCN stations
    (USW* in North America, CA* in Canada, GME* in Germany, UK*, FR*,
    RS*, IN*) can be filtered by Geofabrik path.
    """

    def _poly(min_lat: float, max_lat: float, min_lon: float, max_lon: float) -> dict[str, Any]:
        """GeoJSON rectangle (lon/lat ordering per spec)."""
        return {
            "type": "Polygon",
            "coordinates": [
                [
                    [min_lon, min_lat],
                    [max_lon, min_lat],
                    [max_lon, max_lat],
                    [min_lon, max_lat],
                    [min_lon, min_lat],
                ]
            ],
        }

    features = [
        # Continents.
        {
            "type": "Feature",
            "properties": {"id": "europe", "name": "Europe", "parent": None},
            "geometry": _poly(35.0, 72.0, -25.0, 45.0),
        },
        {
            "type": "Feature",
            "properties": {"id": "north-america", "name": "North America", "parent": None},
            "geometry": _poly(14.0, 84.0, -170.0, -50.0),
        },
        {
            "type": "Feature",
            "properties": {"id": "asia", "name": "Asia", "parent": None},
            "geometry": _poly(1.0, 80.0, 25.0, 180.0),
        },
        # Countries (parent: continent).
        {
            "type": "Feature",
            "properties": {
                "id": "germany",
                "name": "Germany",
                "parent": "europe",
                "iso3166-1:alpha2": ["DE"],
            },
            "geometry": _poly(47.3, 55.1, 5.9, 15.0),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "great-britain",
                "name": "Great Britain",
                "parent": "europe",
                "iso3166-1:alpha2": ["GB"],
            },
            "geometry": _poly(49.9, 60.9, -8.7, 1.8),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "france",
                "name": "France",
                "parent": "europe",
                "iso3166-1:alpha2": ["FR"],
            },
            "geometry": _poly(41.3, 51.1, -5.2, 9.6),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "russia",
                "name": "Russia",
                "parent": "europe",
                "iso3166-1:alpha2": ["RU"],
            },
            "geometry": _poly(41.2, 82.0, 19.6, 180.0),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "us",
                "name": "United States",
                "parent": "north-america",
                "iso3166-1:alpha2": ["US"],
            },
            "geometry": _poly(24.4, 49.4, -125.0, -66.9),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "canada",
                "name": "Canada",
                "parent": "north-america",
                "iso3166-1:alpha2": ["CA"],
            },
            "geometry": _poly(41.7, 83.1, -141.0, -52.6),
        },
        {
            "type": "Feature",
            "properties": {
                "id": "india",
                "name": "India",
                "parent": "asia",
                "iso3166-1:alpha2": ["IN"],
            },
            "geometry": _poly(6.7, 35.6, 68.1, 97.4),
        },
        # A sub-country region to exercise multi-level paths.
        {
            "type": "Feature",
            "properties": {
                "id": "new-york",
                "name": "New York",
                "parent": "us",
                "iso3166-2": ["US-NY"],
            },
            "geometry": _poly(40.5, 45.02, -79.76, -71.86),
        },
    ]
    return {"type": "FeatureCollection", "features": features}
