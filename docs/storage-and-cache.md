# Storage, Cache & Sidecars

**Cross-cutting** (no single namespace) ·
**Tools:** `src/noaa_weather/tools/_noaa_tools/storage.py`, `sidecar.py` ·
**Specs:** `agent-spec/cache-layout.agent-spec.yaml`,
`agent-spec/tools-pattern.agent-spec.yaml`

## Overview

This is the plumbing every other feature stands on: a pluggable storage backend
(`local` | `hdfs` | `s3`) and a sidecar-backed cache under
`cache/noaa-weather/<cache_type>/`. Its whole reason to exist in a multi-server fleet
is `FW_STORAGE=s3` — durable cache + outputs go to shared MinIO/S3, so any runner on
any host resolves the same artifacts with **no shared disk**, while readers that need
a real file handle get one via a local read-through cache.

## How it works

`storage.py` defines the abstract `Storage` interface and three implementations
selected by `FW_STORAGE` (default `local`) via `get_storage()`:

- **`LocalStorage`** — POSIX filesystem; `fcntl.flock` advisory locking; atomic
  `write_text_atomic` (tmp + `os.replace`); `finalize_from_local` uses `os.rename`,
  falling back to `cp -X` (skips xattrs/resource forks) on cross-filesystem moves.
- **`HdfsStorage`** — WebHDFS via `facetwork.runtime.storage.HDFSStorageBackend`
  (soft-imported). No locking; in-memory write buffer; directory finalize is
  `NotImplementedError`.
- **`S3Storage`** — delegates to `facetwork.runtime.storage.S3StorageBackend` (the
  same object store the platform writes step payloads to). No dirs/locking (key
  prefixes implicit, PUT atomic-on-close); `rename`/finalize copy-then-delete via
  streamed object I/O.

**Roots:** one `FW_DATA_ROOT` with five derived subtrees — `cache/`, `staging/`,
`tmp/`, `_indexes/`, `locks/` — each individually overridable
(`FW_CACHE_ROOT`, `FW_STAGING_ROOT`, …). Defaults: local `/Volumes/afl_data`, hdfs
`/user/afl`, s3 `s3://afl-cache`.

**localize + local scratch — the two load-bearing rules:**

- `localize(path)` returns a **local** filesystem path: for local backends it's the
  path itself; for s3/hdfs it downloads into `local_scratch_root()/localized/<key>`
  (size-checked, cached) so csv/gzip readers work regardless of where the durable
  artifact lives. `S3Storage.finalize_from_local` *warms* that cache on write, so the
  next `localize` is a no-op.
- **Scratch is always local.** `local_scratch_root()` (`FW_LOCAL_SCRATCH`) and
  `local_staging_subdir` stay on local disk even when `FW_DATA_ROOT=s3://…` — so a
  stage-then-finalize download writes to disk before being finalized onto a (possibly
  remote) backend, and pointing the data root at an object store never poisons staging.

## Fan-out

**N/A** — infrastructure. It's what makes fan-out across a shared-nothing fleet
correct: portable `s3://` URIs in step payloads mean any host can resolve any
artifact.

## Data & fields

**The write protocol** (`sidecar.py`, and followed by every download lib):

1. Stream the response to a staged file under local scratch.
2. Hash (SHA-256) while streaming.
3. `finalize_from_local` — atomically move the staged file into
   `cache/noaa-weather/<cache_type>/<relative_path>`.
4. Write the `.meta.json` sidecar **last** — readers treat a missing sidecar as
   "entry not present", so artifact-before-sidecar ordering is critical.

Sidecar fields: `kind`, `size_bytes`, `sha256`, `generated_at`, `source`
(publisher/url/used_mock), `tool` (name/version), and an `extra` bag. Cache validity =
sidecar exists + artifact exists with matching size + (for expiring types) sidecar
`generated_at` younger than the max-age.

**Cache types in use** (all under `cache/noaa-weather/`): `catalog` (station +
inventory txt, 24 h), `station-csv` (per-station CSVs, no expiry), `geofabrik`
(index-v1.json, 14 d), `geocode` (Nominatim results), `ndbc-catalog`,
`ndbc-stdmet`, `buoy-summaries`, `climate-report`, `qc-viz`, `extremes-viz`.

## External libraries / binaries

- **`requests`** — used by the download libs on top of storage, not by storage itself.
- **`facetwork.runtime.storage`** — provides the S3/HDFS backends (soft-imported; only
  loaded when that backend is selected). The `s3` path needs the Facetwork runtime
  package present.
- `cp` (system binary) — the `LocalStorage` cross-filesystem finalize fallback.
- No `pymongo` here — storage stays DB-free so the CLIs run standalone.

## Cache / output

This *is* the cache/output layer. Every other spec's "Cache / output" section names
cache types that resolve through here.

## Gotchas & notes

- **Enumerate the cache via the sidecar walk, not `Path().iterdir()`** — a local
  directory listing silently finds nothing under `FW_STORAGE=s3`. `SummarizeBuoy` was
  fixed for exactly this (see [marine](marine.md)).
- **Keep `FW_OUTPUT_BASE` / `FW_LOCAL_SCRATCH` local** — object stores don't do
  partial writes, so handlers must stage locally and finalize on close.
- **Sidecar-last is not optional** — writing it before the artifact would let a reader
  treat a half-written file as valid.
- **Mock vs live is independent of backend** — a mock CSV can be cached to s3 just as a
  real one; the `used_mock` sidecar field records which.

## Related specs

- [ingest](ingest.md) — the clearest example of the stage→hash→finalize→sidecar
  protocol.
- [marine](marine.md) — the s3 sidecar-walk fix.
- [quality-control](quality-control.md), [extremes](extremes.md),
  [climate-report](climate-report.md) — all write charts/bundles through this layer.
