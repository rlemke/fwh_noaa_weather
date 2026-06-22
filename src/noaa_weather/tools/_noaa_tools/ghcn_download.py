"""GHCN-Daily download library — catalog + per-station CSVs with sidecars.

All downloads land under ``cache/noaa-weather/`` with per-entry
``.meta.json`` sidecars following the layout in
``agent-spec/cache-layout.agent-spec.yaml``.

Cache layout:

- ``cache/noaa-weather/catalog/stations.txt`` + ``.meta.json``
- ``cache/noaa-weather/catalog/inventory.txt`` + ``.meta.json``
- ``cache/noaa-weather/station-csv/<station_id>.csv`` + ``.meta.json``

Write protocol (per cache-layout spec):

1. Stream the response to a staged file under ``staging/``.
2. Hash while streaming; record the hash.
3. ``finalize_from_local`` to move the staged file into ``cache/`` atomically.
4. Write the sidecar **last** — readers treat a missing sidecar as
   "entry not present", so the artifact-before-sidecar order is critical.

Network is optional. If ``requests`` is unavailable, downloaders fall
back to deterministic mock data from :mod:`_noaa_tools.ghcn_mocks` so test
suites run offline.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Allow ``python _noaa_tools/ghcn_download.py`` and imports from tool scripts that
# put ``tools/`` on sys.path. When used as a package import, this is a no-op.
_TOOLS_ROOT = Path(__file__).resolve().parent.parent
if str(_TOOLS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TOOLS_ROOT))

from . import ghcn_mocks, sidecar  # noqa: E402
from .storage import Storage, get_storage, local_staging_subdir  # noqa: E402

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

logger = logging.getLogger("noaa-weather.download")

NAMESPACE = "noaa-weather"
CATALOG_CACHE_TYPE = "catalog"
STATION_CSV_CACHE_TYPE = "station-csv"

GHCN_S3_BASE = "https://noaa-ghcn-pds.s3.amazonaws.com/"
CATALOG_FILES = {
    "stations": "ghcnd-stations.txt",
    "inventory": "ghcnd-inventory.txt",
}
DEFAULT_CATALOG_MAX_AGE_HOURS = 24.0
CHUNK_SIZE = 1 << 16
USER_AGENT = "facetwork-noaa-weather/1.0"
CONNECT_TIMEOUT = 30
READ_TIMEOUT = 300

# Per-path in-process locks for short contention on the same artifact.
# The per-entry fcntl lock (via sidecar.entry_lock) is still the
# cross-process authority, used on overwrite.
_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()


def _path_lock(path: str) -> threading.Lock:
    with _path_locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


# ---------------------------------------------------------------------------
# Public dataclass.
# ---------------------------------------------------------------------------

@dataclass
class DownloadResult:
    """Outcome of a download or cache hit."""

    cache_type: str
    relative_path: str
    absolute_path: str
    size_bytes: int
    sha256: str
    source_url: str
    was_cached: bool
    generated_at: str
    used_mock: bool = False


# ---------------------------------------------------------------------------
# Catalog (stations.txt, inventory.txt).
# ---------------------------------------------------------------------------

def download_catalog_file(
    kind: str,
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_CATALOG_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> DownloadResult:
    """Download one of the two catalog files (``stations`` or ``inventory``).

    Cache validity: entry is current if its sidecar exists, the artifact
    exists with matching size, and the sidecar's ``generated_at`` is less
    than ``max_age_hours`` old. ``force=True`` always re-downloads.

    ``use_mock`` default is ``True`` iff the ``requests`` library is missing.
    """
    if kind not in CATALOG_FILES:
        raise ValueError(f"kind must be one of {sorted(CATALOG_FILES)}, got {kind!r}")
    relative_path = f"{kind}.txt"
    remote_name = CATALOG_FILES[kind]
    source_url = GHCN_S3_BASE + remote_name

    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, CATALOG_CACHE_TYPE, relative_path, s)

    with _path_lock(art_path):
        if not force:
            side = sidecar.read_sidecar(NAMESPACE, CATALOG_CACHE_TYPE, relative_path, s)
            if side and sidecar.exists_and_valid(
                NAMESPACE, CATALOG_CACHE_TYPE, relative_path, s
            ):
                age = _age_hours(side.get("generated_at"))
                if age is None or age < max_age_hours:
                    logger.info(
                        "Catalog cache hit (%s, %.1fh old)",
                        relative_path,
                        age if age is not None else -1.0,
                    )
                    return DownloadResult(
                        cache_type=CATALOG_CACHE_TYPE,
                        relative_path=relative_path,
                        absolute_path=s.localize(art_path),
                        size_bytes=side.get("size_bytes", 0),
                        sha256=side.get("sha256", ""),
                        source_url=source_url,
                        was_cached=True,
                        generated_at=side.get("generated_at", ""),
                    )
                logger.info(
                    "Catalog cache stale (%.1fh > %.1fh), re-downloading %s",
                    age,
                    max_age_hours,
                    relative_path,
                )

        should_mock = _resolve_use_mock(use_mock)
        if should_mock:
            text = (
                ghcn_mocks.mock_station_catalog()
                if kind == "stations"
                else ghcn_mocks.mock_inventory()
            )
            return _write_catalog(
                kind, text.encode("utf-8"), source_url, s, used_mock=True
            )

        if requests is None:  # pragma: no cover — guarded by _resolve_use_mock
            raise RuntimeError(
                "requests library is not installed. Install it, run via "
                "the .sh wrapper (activates .venv), or pass --use-mock if "
                "deterministic mock data is acceptable."
            )

        logger.info("Downloading catalog %s from %s", kind, source_url)
        resp = requests.get(
            source_url,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return _write_catalog(kind, resp.content, source_url, s, used_mock=False)


def read_catalog_file(
    kind: str,
    *,
    force: bool = False,
    max_age_hours: float = DEFAULT_CATALOG_MAX_AGE_HOURS,
    storage: Storage | None = None,
    use_mock: bool | None = None,
) -> str:
    """Return the catalog text, downloading only if stale or missing."""
    res = download_catalog_file(
        kind,
        force=force,
        max_age_hours=max_age_hours,
        storage=storage,
        use_mock=use_mock,
    )
    with open(res.absolute_path, "r", encoding="utf-8") as f:
        return f.read()


def _write_catalog(
    kind: str,
    body: bytes,
    source_url: str,
    storage: Storage,
    *,
    used_mock: bool,
) -> DownloadResult:
    relative_path = f"{kind}.txt"
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{CATALOG_CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{kind}.txt.stage-{os.getpid()}")

    with open(stage_path, "wb") as f:
        f.write(body)
    digest = hashlib.sha256(body).hexdigest()
    size_bytes = len(body)

    final_path = sidecar.cache_path(NAMESPACE, CATALOG_CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(NAMESPACE, CATALOG_CACHE_TYPE, relative_path, storage=storage):
        storage.finalize_from_local(stage_path, final_path)
        side = sidecar.write_sidecar(
            NAMESPACE,
            CATALOG_CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=size_bytes,
            sha256=digest,
            source={
                "publisher": "NOAA GHCN-Daily",
                "url": source_url,
                "used_mock": used_mock,
            },
            tool={"name": "ghcn_download", "version": "1.0"},
            storage=storage,
        )

    return DownloadResult(
        cache_type=CATALOG_CACHE_TYPE,
        relative_path=relative_path,
        absolute_path=storage.localize(final_path),
        size_bytes=size_bytes,
        sha256=digest,
        source_url=source_url,
        was_cached=False,
        generated_at=side["generated_at"],
        used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Per-station CSVs.
# ---------------------------------------------------------------------------

def download_station_csv(
    station_id: str,
    *,
    force: bool = False,
    storage: Storage | None = None,
    use_mock: bool | None = None,
    mock_years: tuple[int, int] = (2000, 2023),
    extra_metadata: dict[str, Any] | None = None,
) -> DownloadResult:
    """Download ``csv/by_station/<station_id>.csv`` from NOAA.

    CSVs do not expire automatically — once cached, subsequent calls
    return the cache hit unless ``force=True``. The sidecar records the
    SHA-256 so callers that need to detect upstream changes can do so
    by ``force`` + comparing.

    ``extra_metadata`` is merged into the sidecar's ``extra`` field —
    useful for recording inventory-derived facts like ``first_year`` /
    ``last_year`` at the time of download.
    """
    relative_path = f"{station_id}.csv"
    source_url = f"{GHCN_S3_BASE}csv/by_station/{station_id}.csv"

    s = storage or get_storage()
    art_path = sidecar.cache_path(NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, s)

    with _path_lock(art_path):
        if not force and sidecar.exists_and_valid(
            NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, s
        ):
            side = sidecar.read_sidecar(NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, s)
            assert side is not None
            logger.info(
                "Station CSV cache hit %s (%s bytes)", station_id, f"{side.get('size_bytes', 0):,}"
            )
            return DownloadResult(
                cache_type=STATION_CSV_CACHE_TYPE,
                relative_path=relative_path,
                absolute_path=s.localize(art_path),
                size_bytes=side.get("size_bytes", 0),
                sha256=side.get("sha256", ""),
                source_url=source_url,
                was_cached=True,
                generated_at=side.get("generated_at", ""),
            )

        should_mock = _resolve_use_mock(use_mock)
        if should_mock:
            text = ghcn_mocks.mock_station_csv(
                station_id, mock_years[0], mock_years[1]
            )
            return _write_station_csv(
                station_id,
                text.encode("utf-8"),
                source_url,
                s,
                used_mock=True,
                extra_metadata=extra_metadata,
            )

        if requests is None:  # pragma: no cover
            raise RuntimeError(
                "requests library is not installed. Install it, run via "
                "the .sh wrapper (activates .venv), or pass --use-mock if "
                "deterministic mock data is acceptable."
            )

        logger.info("Downloading %s from %s", station_id, source_url)
        t0 = time.monotonic()
        resp = requests.get(
            source_url,
            stream=True,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers={"User-Agent": USER_AGENT},
        )
        try:
            resp.raise_for_status()
            # Stream-and-hash to avoid buffering the whole CSV in memory;
            # some stations are hundreds of MB of history.
            staging_dir = local_staging_subdir(f"{NAMESPACE}/{STATION_CSV_CACHE_TYPE}")
            os.makedirs(staging_dir, exist_ok=True)
            stage_path = os.path.join(
                staging_dir, f"{station_id}.csv.stage-{os.getpid()}"
            )
            h = hashlib.sha256()
            bytes_written = 0
            with open(stage_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        h.update(chunk)
                        bytes_written += len(chunk)
        finally:
            resp.close()

        elapsed = time.monotonic() - t0
        logger.info(
            "Download complete %s: %s bytes in %.1fs",
            station_id,
            f"{bytes_written:,}",
            elapsed,
        )

        final_path = sidecar.cache_path(
            NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, s
        )
        merged_extra: dict[str, Any] = {
            "station_id": station_id,
            "download_duration_seconds": round(elapsed, 2),
        }
        if extra_metadata:
            merged_extra.update(extra_metadata)
        with sidecar.entry_lock(
            NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, storage=s
        ):
            s.finalize_from_local(stage_path, final_path)
            side = sidecar.write_sidecar(
                NAMESPACE,
                STATION_CSV_CACHE_TYPE,
                relative_path,
                kind="file",
                size_bytes=bytes_written,
                sha256=h.hexdigest(),
                source={
                    "publisher": "NOAA GHCN-Daily",
                    "url": source_url,
                    "used_mock": False,
                },
                tool={"name": "ghcn_download", "version": "1.0"},
                extra=merged_extra,
                storage=s,
            )
        return DownloadResult(
            cache_type=STATION_CSV_CACHE_TYPE,
            relative_path=relative_path,
            absolute_path=s.localize(final_path),
            size_bytes=bytes_written,
            sha256=h.hexdigest(),
            source_url=source_url,
            was_cached=False,
            generated_at=side["generated_at"],
        )


def _write_station_csv(
    station_id: str,
    body: bytes,
    source_url: str,
    storage: Storage,
    *,
    used_mock: bool,
    extra_metadata: dict[str, Any] | None = None,
) -> DownloadResult:
    relative_path = f"{station_id}.csv"
    staging_dir = local_staging_subdir(f"{NAMESPACE}/{STATION_CSV_CACHE_TYPE}")
    os.makedirs(staging_dir, exist_ok=True)
    stage_path = os.path.join(staging_dir, f"{station_id}.csv.stage-{os.getpid()}")

    with open(stage_path, "wb") as f:
        f.write(body)
    digest = hashlib.sha256(body).hexdigest()
    size_bytes = len(body)

    merged_extra: dict[str, Any] = {"station_id": station_id}
    if extra_metadata:
        merged_extra.update(extra_metadata)

    final_path = sidecar.cache_path(NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, storage)
    with sidecar.entry_lock(
        NAMESPACE, STATION_CSV_CACHE_TYPE, relative_path, storage=storage
    ):
        storage.finalize_from_local(stage_path, final_path)
        side = sidecar.write_sidecar(
            NAMESPACE,
            STATION_CSV_CACHE_TYPE,
            relative_path,
            kind="file",
            size_bytes=size_bytes,
            sha256=digest,
            source={
                "publisher": "NOAA GHCN-Daily",
                "url": source_url,
                "used_mock": used_mock,
            },
            tool={"name": "ghcn_download", "version": "1.0"},
            extra=merged_extra,
            storage=storage,
        )

    return DownloadResult(
        cache_type=STATION_CSV_CACHE_TYPE,
        relative_path=relative_path,
        absolute_path=storage.localize(final_path),
        size_bytes=size_bytes,
        sha256=digest,
        source_url=source_url,
        was_cached=False,
        generated_at=side["generated_at"],
        used_mock=used_mock,
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _age_hours(generated_at: str | None) -> float | None:
    """Hours since the sidecar's ``generated_at`` timestamp, or ``None``."""
    if not generated_at:
        return None
    from datetime import datetime, timezone

    try:
        ts = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None
    delta = datetime.now(timezone.utc) - ts
    return delta.total_seconds() / 3600.0


def _resolve_use_mock(explicit: bool | None) -> bool:
    """Decide whether to use mock data.

    Default (``explicit`` is None) is ``False``: mock data is opt-in via
    ``use_mock=True``. If ``requests`` is not available and mock is off,
    the caller will raise — we don't silently substitute fake data.
    """
    return bool(explicit)
