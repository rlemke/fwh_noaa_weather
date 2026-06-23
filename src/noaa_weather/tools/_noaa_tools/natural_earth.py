"""Natural Earth admin-1 polygon resolver.

Gives ``warming_map._geometry_for_region`` a middle option between the
Geofabrik polygon path (accurate but requires a Geofabrik-path report)
and the ``US_STATE_BOUNDS`` bbox fallback (produces rectangles for
every US state). Natural Earth publishes actual state/province
boundaries worldwide as a single ~20 MB GeoJSON — one fetch covers
every country + admin-1 combination we're likely to report on.

Cache layout::

    cache/noaa-weather/natural-earth/
      ne_50m_admin_1_states_provinces.geojson  + .meta.json

Upstream: https://github.com/nvkelso/natural-earth-vector — public
domain. We pull the ``geojson/ne_50m_admin_1_states_provinces.geojson``
file from master via raw.githubusercontent.com.

Match logic:
  1. Translate FIPS country (GHCN convention) → ISO-3166-1 alpha-2
     (Natural Earth convention) via the ``FIPS_TO_ISO`` table.
  2. Find every Natural Earth feature with ``iso_a2`` matching that
     ISO code.
  3. Match the ``state`` argument against ``postal``, ``name``,
     ``name_en``, ``fips``, ``adm1_code`` (case-insensitive; also
     tolerate spaces/hyphens/underscores interchangeably).

Returns the feature's geometry dict (Polygon or MultiPolygon) so the
map renderer can drop it in as-is.
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

logger = logging.getLogger("noaa-weather.natural-earth")

NAMESPACE = "noaa-weather"
CACHE_TYPE = "natural-earth"
ADMIN1_RELATIVE_PATH = "ne_50m_admin_1_states_provinces.geojson"

ADMIN1_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_50m_admin_1_states_provinces.geojson"
)
USER_AGENT = "facetwork-noaa-weather/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
# Natural Earth releases maybe once a year; there's no reason to
# re-fetch often.
DEFAULT_MAX_AGE_HOURS = 24.0 * 180

_lock = threading.Lock()
# Lazily populated in-process cache — indexed by ISO-2 country code
# so state lookups are O(features-per-country).
_features_by_country: dict[str, list[dict[str, Any]]] | None = None


# ---------------------------------------------------------------------------
# FIPS country code (GHCN convention) → ISO-3166-1 alpha-2 (Natural Earth).
# ---------------------------------------------------------------------------
#
# Covers every FIPS code our existing FFL workflows touch. Codes that
# are identical in both conventions are listed for explicitness.

FIPS_TO_ISO: dict[str, str] = {
    # North America
    "US": "US", "CA": "CA", "MX": "MX",
    "GT": "GT", "HO": "HN", "NU": "NI", "CS": "CR", "PM": "PA",
    "RQ": "PR", "BH": "BZ", "ES": "SV",
    # South America
    "BR": "BR", "AR": "AR", "CI": "CL", "CO": "CO", "PE": "PE",
    "VE": "VE", "EC": "EC", "BO": "BO", "PY": "PY", "UY": "UY",
    # Europe
    "UK": "GB", "GM": "DE", "FR": "FR", "SP": "ES", "IT": "IT",
    "NO": "NO", "SW": "SE", "FI": "FI", "PL": "PL", "EZ": "CZ",
    "NL": "NL", "BE": "BE", "DA": "DK", "SZ": "CH", "PO": "PT",
    "EI": "IE", "GR": "GR", "IC": "IS", "AU": "AT", "HU": "HU",
    "RO": "RO",
    # Asia + Russia
    "CH": "CN", "JA": "JP", "KS": "KR", "TH": "TH", "VM": "VN",
    "BM": "MM", "ID": "ID", "IN": "IN", "RS": "RU", "IR": "IR",
    "TU": "TR",
    # Africa
    "SF": "ZA", "NI": "NE", "KE": "KE", "EG": "EG", "MO": "MA",
    "AG": "DZ", "LY": "LY", "IV": "CI", "GH": "GH", "SN": "SN",
    "TZ": "TZ", "UG": "UG", "ET": "ET", "NG": "NG",
    # Oceania
    "AS": "AU", "NZ": "NZ", "FJ": "FJ",
    # Arctic / Antarctic
    "GL": "GL", "SV": "NO",  # Svalbard maps under Norway in Natural Earth
    "AY": "AQ",
}


# ---------------------------------------------------------------------------
# Dataclass.
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    absolute_path: str
    size_bytes: int
    sha256: str
    feature_count: int
    was_cached: bool
    used_mock: bool = False


# ---------------------------------------------------------------------------
# Download + cache.
# ---------------------------------------------------------------------------

def download_admin1(
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> DownloadResult:
    """Fetch the admin-1 states/provinces GeoJSON and cache it with a sidecar.

    Roughly 20 MB — pulled once and reused for every subsequent
    ``resolve_state_polygon`` call in this process (and future
    processes, since the cache is on disk).
    """
    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, s)

    with _lock:
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, s)
            if side and sidecar.exists_and_valid(
                NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, s
            ):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info(
                        "natural-earth admin-1 cache hit (%.1fh old)",
                        age or -1.0,
                    )
                    return DownloadResult(
                        absolute_path=art_path,
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        feature_count=int(
                            (side.get("extra") or {}).get("feature_count", 0)
                        ),
                        was_cached=True,
                    )

        if use_mock:
            body = json.dumps(_mock_admin1()).encode("utf-8")
            return _persist(body, s, used_mock=True)

        if requests is None:
            raise RuntimeError(
                "requests library is not installed. Install it, run via the .sh "
                "wrapper (activates .venv), or pass --use-mock."
            )

        logger.info("downloading natural-earth admin-1 from %s", ADMIN1_URL)
        t0 = time.monotonic()
        resp = requests.get(
            ADMIN1_URL,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        )
        resp.raise_for_status()
        body = resp.content
        elapsed = time.monotonic() - t0
        logger.info(
            "natural-earth admin-1 downloaded: %s bytes in %.1fs",
            f"{len(body):,}",
            elapsed,
        )
        return _persist(body, s, used_mock=False)


def _persist(body: bytes, storage: Storage, *, used_mock: bool) -> DownloadResult:
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(
        staging_dir, f"{ADMIN1_RELATIVE_PATH}.stage-{os.getpid()}"
    )
    with open(stage_path, "wb") as f:
        f.write(body)

    feature_count = 0
    try:
        parsed = json.loads(body)
        feature_count = len(parsed.get("features") or [])
    except json.JSONDecodeError:
        pass

    final_path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, storage)
    with sidecar.entry_lock(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        sidecar.write_sidecar(
            NAMESPACE,
            CACHE_TYPE,
            ADMIN1_RELATIVE_PATH,
            kind="file",
            size_bytes=len(body),
            sha256=hashlib.sha256(body).hexdigest(),
            source={
                "publisher": "Natural Earth (nvkelso/natural-earth-vector)",
                "url": ADMIN1_URL,
                "license": "Public domain",
                "used_mock": used_mock,
            },
            tool={"name": "natural_earth", "version": "1.0"},
            extra={"feature_count": feature_count},
            storage=storage,
        )

    # Bust the in-process cache so the next lookup rebuilds from new data.
    global _features_by_country
    _features_by_country = None

    return DownloadResult(
        absolute_path=final_path,
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        feature_count=feature_count,
        was_cached=False,
        used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Polygon lookup.
# ---------------------------------------------------------------------------

def _load_index(storage: Storage, *, use_mock: bool) -> dict[str, list[dict[str, Any]]]:
    """Load the admin-1 GeoJSON and bucket features by ISO country code.

    Bucketing turns per-state lookup into a small linear scan over one
    country's provinces instead of scanning all 4 000+ admin-1
    features globally. The in-process cache ensures we only pay this
    cost once per run.
    """
    global _features_by_country
    if _features_by_country is not None:
        return _features_by_country

    # Make sure the GeoJSON is cached on disk.
    if not sidecar.exists_and_valid(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, storage):
        download_admin1(storage=storage, use_mock=use_mock)

    path = sidecar.cache_path(NAMESPACE, CACHE_TYPE, ADMIN1_RELATIVE_PATH, storage)
    # localize: s3:// -> a real local file for open(); local -> itself.
    with open(storage.localize(path), "r", encoding="utf-8") as f:
        data = json.load(f)

    bucketed: dict[str, list[dict[str, Any]]] = {}
    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        iso = (props.get("iso_a2") or props.get("adm0_a2") or "").upper()
        if not iso or iso == "-99":  # NE uses -99 for disputed/unmatched
            continue
        bucketed.setdefault(iso, []).append(feat)

    _features_by_country = bucketed
    return _features_by_country


def _normalize(s: str) -> str:
    """Case-insensitive + whitespace / hyphen / underscore-agnostic."""
    return s.strip().lower().replace("_", " ").replace("-", " ").replace("  ", " ")


def resolve_state_polygon(
    country: str,
    state: str,
    *,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> dict[str, Any] | None:
    """Return the admin-1 polygon for a (country, state) pair, or None.

    - ``country`` can be FIPS (``US``, ``GM``) or ISO-3166-1 alpha-2
      (``US``, ``DE``). Both are tried.
    - ``state`` is matched case-insensitively against ``postal``,
      ``name``, ``name_en``, ``fips``, ``adm1_code``. US state 2-letter
      abbreviations land on ``postal``; non-US names (e.g. "Bayern",
      "Ontario") land on ``name``.

    No match → ``None``; callers can then fall back to whatever they
    had before (bbox rectangle in our case).
    """
    if not country or not state:
        return None
    s = storage or get_storage()

    try:
        index = _load_index(s, use_mock=use_mock)
    except Exception as exc:
        logger.info("couldn't load natural-earth admin-1: %s", exc)
        return None

    # Normalize country: try ISO alias first, then FIPS translation.
    candidates: list[str] = []
    c = country.upper()
    if c in index:
        candidates.append(c)
    iso = FIPS_TO_ISO.get(c)
    if iso and iso not in candidates and iso in index:
        candidates.append(iso)
    if not candidates:
        return None

    state_norm = _normalize(state)
    match_keys = ("postal", "name", "name_en", "fips", "adm1_code", "gn_name")
    for iso_code in candidates:
        for feat in index[iso_code]:
            props = feat.get("properties") or {}
            for key in match_keys:
                val = props.get(key)
                if not isinstance(val, str):
                    continue
                if _normalize(val) == state_norm:
                    geom = feat.get("geometry")
                    if isinstance(geom, dict) and geom.get("coordinates"):
                        return geom
    return None


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


# ---------------------------------------------------------------------------
# Mock — small hand-crafted admin-1 set for offline tests.
# ---------------------------------------------------------------------------

def _mock_admin1() -> dict[str, Any]:
    """Tiny FeatureCollection with one or two provinces per common country.

    Enough to exercise the FIPS → ISO translation + postal/name match
    logic without a network dependency. Polygon shapes are simplified
    rectangles close to the real bbox — good enough for the geometry
    to render as something plausibly state-shaped in tests.
    """

    def _rect(min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> dict[str, Any]:
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
        # US — New York, California
        {
            "type": "Feature",
            "properties": {
                "iso_a2": "US",
                "postal": "NY",
                "name": "New York",
                "name_en": "New York",
                "fips": "US36",
                "adm1_code": "USA-3611",
            },
            "geometry": _rect(-79.76, 40.50, -71.86, 45.02),
        },
        {
            "type": "Feature",
            "properties": {
                "iso_a2": "US",
                "postal": "CA",
                "name": "California",
                "name_en": "California",
                "fips": "US06",
                "adm1_code": "USA-3521",
            },
            "geometry": _rect(-124.41, 32.53, -114.13, 42.01),
        },
        # Canada — Ontario
        {
            "type": "Feature",
            "properties": {
                "iso_a2": "CA",
                "postal": "ON",
                "name": "Ontario",
                "name_en": "Ontario",
                "adm1_code": "CAN-701",
            },
            "geometry": _rect(-95.15, 41.68, -74.32, 56.85),
        },
        # Germany — Bayern
        {
            "type": "Feature",
            "properties": {
                "iso_a2": "DE",
                "name": "Bayern",
                "name_en": "Bavaria",
                "adm1_code": "DEU-3022",
            },
            "geometry": _rect(8.98, 47.27, 13.83, 50.56),
        },
    ]
    return {"type": "FeatureCollection", "features": features}
