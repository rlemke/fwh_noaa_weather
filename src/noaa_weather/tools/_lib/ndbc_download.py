"""NDBC download library — active-stations catalog + per-station history.

All downloads land under ``cache/noaa-weather/`` with per-entry
``.meta.json`` sidecars:

- ``cache/noaa-weather/ndbc-catalog/activestations.xml`` (raw upstream)
- ``cache/noaa-weather/ndbc-catalog/stations.json`` (normalised to the
  shape :func:`_lib.ndbc_parse.parse_activestations_xml` returns)
- ``cache/noaa-weather/ndbc-stdmet/<station_id>/<year>.txt.gz``

Same contract as :mod:`_lib.ghcn_download`. Mock fallback is opt-in
via ``use_mock=True``; by default the tool expects live network +
``requests``.
"""

from __future__ import annotations

import gzip
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

from . import ndbc_mocks, ndbc_parse, sidecar  # noqa: E402
from .storage import LocalStorage, Storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("noaa-weather.ndbc")

NAMESPACE = "noaa-weather"
CATALOG_CACHE_TYPE = "ndbc-catalog"
STDMET_CACHE_TYPE = "ndbc-stdmet"

CATALOG_XML_RELATIVE = "activestations.xml"
CATALOG_JSON_RELATIVE = "stations.json"

CATALOG_URL = "https://www.ndbc.noaa.gov/activestations.xml"
STDMET_URL_TEMPLATE = (
    "https://www.ndbc.noaa.gov/data/historical/stdmet/{station_id}h{year}.txt.gz"
)
USER_AGENT = "facetwork-noaa-weather/1.0 (+https://github.com/rlemke/facetwork)"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300
CATALOG_DEFAULT_MAX_AGE_HOURS = 24.0 * 7  # buoy catalog shifts weekly-ish

_lock = threading.Lock()


@dataclass
class CatalogResult:
    xml_path: str
    json_path: str
    station_count: int
    source_url: str
    was_cached: bool
    generated_at: str
    used_mock: bool = False


@dataclass
class StdmetResult:
    station_id: str
    year: int
    absolute_path: str
    relative_path: str
    size_bytes: int
    sha256: str
    source_url: str
    was_cached: bool
    generated_at: str
    used_mock: bool = False


# ---------------------------------------------------------------------------
# Active-stations catalog.
# ---------------------------------------------------------------------------

def download_catalog(
    *,
    force: bool = False,
    max_age_hours: float = CATALOG_DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> CatalogResult:
    """Fetch the raw XML + write a normalized JSON companion.

    The XML is the authoritative artifact we cache verbatim. The
    JSON is derived at download time so downstream tools can skip
    the XML parse.
    """
    s = storage or LocalStorage()
    xml_abs = sidecar.cache_path(
        NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_XML_RELATIVE, s
    )
    json_abs = sidecar.cache_path(
        NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_JSON_RELATIVE, s
    )

    with _lock:
        if not force:
            side = sidecar.read_sidecar(
                NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_XML_RELATIVE, s
            )
            if side and sidecar.exists_and_valid(
                NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_XML_RELATIVE, s
            ) and sidecar.exists_and_valid(
                NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_JSON_RELATIVE, s
            ):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    json_side = sidecar.read_sidecar(
                        NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_JSON_RELATIVE, s
                    ) or {}
                    station_count = int(
                        (json_side.get("extra") or {}).get("station_count", 0)
                    )
                    logger.info(
                        "ndbc-catalog cache hit (%.1fh old, %d stations)",
                        age or -1.0,
                        station_count,
                    )
                    return CatalogResult(
                        xml_path=xml_abs,
                        json_path=json_abs,
                        station_count=station_count,
                        source_url=CATALOG_URL,
                        was_cached=True,
                        generated_at=side.get("generated_at", ""),
                    )

        if use_mock:
            xml_text = ndbc_mocks.mock_activestations_xml()
            used_mock = True
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock."
                )
            logger.info("downloading NDBC catalog from %s", CATALOG_URL)
            resp = requests.get(
                CATALOG_URL,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                headers={"User-Agent": USER_AGENT, "Accept": "application/xml"},
            )
            resp.raise_for_status()
            xml_text = resp.text
            used_mock = False

        # Parse once to build the normalized JSON + count.
        stations = ndbc_parse.parse_activestations_xml(xml_text)
        json_body = json.dumps(
            {
                "source_url": CATALOG_URL,
                "station_count": len(stations),
                "stations": stations,
            },
            indent=2,
            sort_keys=True,
        ) + "\n"

        xml_bytes = xml_text.encode("utf-8")
        json_bytes = json_body.encode("utf-8")

        # Write both artifacts.
        staging = local_staging_subdir(f"{NAMESPACE}/{CATALOG_CACHE_TYPE}")
        os.makedirs(staging, exist_ok=True)
        xml_stage = os.path.join(staging, f"activestations.xml.stage-{os.getpid()}")
        json_stage = os.path.join(staging, f"stations.json.stage-{os.getpid()}")
        with open(xml_stage, "wb") as f:
            f.write(xml_bytes)
        with open(json_stage, "wb") as f:
            f.write(json_bytes)

        with sidecar.entry_lock(
            NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_XML_RELATIVE, storage=s
        ):
            s.finalize_from_local(xml_stage, xml_abs)
            side_xml = sidecar.write_sidecar(
                NAMESPACE,
                CATALOG_CACHE_TYPE,
                CATALOG_XML_RELATIVE,
                kind="file",
                size_bytes=len(xml_bytes),
                sha256=hashlib.sha256(xml_bytes).hexdigest(),
                source={
                    "publisher": "NOAA NDBC",
                    "url": CATALOG_URL,
                    "used_mock": used_mock,
                },
                tool={"name": "ndbc_download", "version": "1.0"},
                extra={"station_count": len(stations)},
                storage=s,
            )
        with sidecar.entry_lock(
            NAMESPACE, CATALOG_CACHE_TYPE, CATALOG_JSON_RELATIVE, storage=s
        ):
            s.finalize_from_local(json_stage, json_abs)
            sidecar.write_sidecar(
                NAMESPACE,
                CATALOG_CACHE_TYPE,
                CATALOG_JSON_RELATIVE,
                kind="file",
                size_bytes=len(json_bytes),
                sha256=hashlib.sha256(json_bytes).hexdigest(),
                source={
                    "derived_from": {
                        "namespace": NAMESPACE,
                        "cache_type": CATALOG_CACHE_TYPE,
                        "relative_path": CATALOG_XML_RELATIVE,
                    },
                    "used_mock": used_mock,
                },
                tool={"name": "ndbc_download", "version": "1.0"},
                extra={"station_count": len(stations)},
                storage=s,
            )

        return CatalogResult(
            xml_path=xml_abs,
            json_path=json_abs,
            station_count=len(stations),
            source_url=CATALOG_URL,
            was_cached=False,
            generated_at=side_xml["generated_at"],
            used_mock=used_mock,
        )


def read_catalog_stations(
    *,
    force: bool = False,
    max_age_hours: float = CATALOG_DEFAULT_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> list[dict[str, Any]]:
    """Return the normalized station list, fetching if stale or absent."""
    res = download_catalog(
        force=force,
        max_age_hours=max_age_hours,
        storage=storage,
        use_mock=use_mock,
    )
    with open(res.json_path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("stations") or []


# ---------------------------------------------------------------------------
# Per-station-year stdmet.
# ---------------------------------------------------------------------------

def stdmet_relative_path(station_id: str, year: int) -> str:
    """Cache-relative path for a station's one-year stdmet file."""
    return f"{station_id}/{year}.txt.gz"


def download_stdmet(
    station_id: str,
    year: int,
    *,
    force: bool = False,
    storage: Storage | None = None,
    use_mock: bool = False,
) -> StdmetResult:
    """Download one station-year of stdmet records."""
    relative_path = stdmet_relative_path(station_id, year)
    source_url = STDMET_URL_TEMPLATE.format(station_id=station_id, year=year)
    s = storage or LocalStorage()
    art_path = sidecar.cache_path(NAMESPACE, STDMET_CACHE_TYPE, relative_path, s)

    with _lock:
        if not force and sidecar.exists_and_valid(
            NAMESPACE, STDMET_CACHE_TYPE, relative_path, s
        ):
            side = sidecar.read_sidecar(NAMESPACE, STDMET_CACHE_TYPE, relative_path, s)
            assert side is not None
            logger.info(
                "ndbc-stdmet cache hit %s/%d (%s bytes)",
                station_id,
                year,
                f"{side.get('size_bytes', 0):,}",
            )
            return StdmetResult(
                station_id=station_id,
                year=year,
                absolute_path=art_path,
                relative_path=relative_path,
                size_bytes=side.get("size_bytes", 0),
                sha256=side.get("sha256", ""),
                source_url=source_url,
                was_cached=True,
                generated_at=side.get("generated_at", ""),
            )

        if use_mock:
            text = ndbc_mocks.mock_stdmet_text(station_id, year)
            body_bytes = gzip.compress(text.encode("utf-8"))
            used_mock = True
        else:
            if requests is None:
                raise RuntimeError(
                    "requests library is not installed. Install it, run via "
                    "the .sh wrapper (activates .venv), or pass --use-mock."
                )
            logger.info("downloading %s/%d from %s", station_id, year, source_url)
            t0 = time.monotonic()
            resp = requests.get(
                source_url,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                headers={"User-Agent": USER_AGENT},
                stream=True,
            )
            try:
                if resp.status_code == 404:
                    raise RuntimeError(
                        f"NDBC has no stdmet for {station_id} year {year} "
                        f"({source_url} returned 404)"
                    )
                resp.raise_for_status()
                chunks = list(resp.iter_content(chunk_size=1 << 15))
            finally:
                resp.close()
            elapsed = time.monotonic() - t0
            body_bytes = b"".join(chunks)
            logger.info(
                "stdmet download complete %s/%d: %s bytes in %.1fs",
                station_id,
                year,
                f"{len(body_bytes):,}",
                elapsed,
            )
            used_mock = False

        staging = local_staging_subdir(f"{NAMESPACE}/{STDMET_CACHE_TYPE}/{station_id}")
        os.makedirs(staging, exist_ok=True)
        stage_path = os.path.join(staging, f"{year}.txt.gz.stage-{os.getpid()}")
        with open(stage_path, "wb") as f:
            f.write(body_bytes)

        with sidecar.entry_lock(
            NAMESPACE, STDMET_CACHE_TYPE, relative_path, storage=s
        ):
            s.finalize_from_local(stage_path, art_path)
            side = sidecar.write_sidecar(
                NAMESPACE,
                STDMET_CACHE_TYPE,
                relative_path,
                kind="file",
                size_bytes=len(body_bytes),
                sha256=hashlib.sha256(body_bytes).hexdigest(),
                source={
                    "publisher": "NOAA NDBC",
                    "url": source_url,
                    "used_mock": used_mock,
                },
                tool={"name": "ndbc_download", "version": "1.0"},
                extra={"station_id": station_id, "year": year},
                storage=s,
            )
        return StdmetResult(
            station_id=station_id,
            year=year,
            absolute_path=art_path,
            relative_path=relative_path,
            size_bytes=len(body_bytes),
            sha256=hashlib.sha256(body_bytes).hexdigest(),
            source_url=source_url,
            was_cached=False,
            generated_at=side["generated_at"],
            used_mock=used_mock,
        )


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
