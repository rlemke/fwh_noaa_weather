"""Storage backend selection + S3Storage wiring (no live MinIO needed).

The s3 round-trip against real MinIO is covered out-of-band; here we assert the
backend dispatch, the data_root defaults, and that S3Storage implements the full
Storage interface and delegates to facetwork's S3StorageBackend.
"""

from __future__ import annotations

import pytest

from noaa_weather.tools._noaa_tools import storage as st


def test_get_storage_dispatch(monkeypatch):
    monkeypatch.delenv("FW_STORAGE", raising=False)
    assert isinstance(st.get_storage("local"), st.LocalStorage)
    assert st.get_storage("s3").name == "s3"
    assert isinstance(st.get_storage("s3"), st.S3Storage)
    with pytest.raises(ValueError, match="s3"):
        st.get_storage("bogus")


def test_default_backend_follows_env(monkeypatch):
    monkeypatch.setenv("FW_STORAGE", "s3")
    assert st.default_backend() == "s3"
    assert isinstance(st.get_storage(), st.S3Storage)


def test_data_root_s3_default(monkeypatch):
    monkeypatch.delenv("FW_DATA_ROOT", raising=False)
    assert st.data_root("s3") == st.S3_DEFAULT_ROOT
    assert st.data_root("local") == st.LOCAL_DEFAULT_ROOT
    monkeypatch.setenv("FW_DATA_ROOT", "s3://my-bucket")
    assert st.data_root("s3") == "s3://my-bucket"


def test_s3_storage_implements_interface():
    s = st.get_storage("s3")
    for m in ("exists", "size", "mkdir_p", "unlink", "rename", "read_text",
              "write_text_atomic", "open_write_binary", "lock",
              "finalize_from_local", "finalize_dir_from_local"):
        assert callable(getattr(s, m)), m
    assert s.supports_locking is False


def test_local_round_trip(tmp_path):
    s = st.get_storage("local")
    p = str(tmp_path / "sub" / "a.txt")
    s.write_text_atomic(p, "hello")
    assert s.exists(p) and s.read_text(p) == "hello" and s.size(p) == 5


def test_local_staging_is_truly_local_even_with_s3_data_root(monkeypatch, tmp_path):
    # FW_DATA_ROOT=s3 must NOT poison local staging (downloads stage to disk).
    monkeypatch.setenv("FW_DATA_ROOT", "s3://afl-cache")
    monkeypatch.setenv("FW_LOCAL_SCRATCH", str(tmp_path))
    d = st.local_staging_subdir("noaa-weather/station-csv")
    assert d.startswith(str(tmp_path)) and "://" not in d
    assert st.local_scratch_root() == str(tmp_path)


def test_localize_identity_for_local():
    assert st.get_storage("local").localize("/var/data/x.csv") == "/var/data/x.csv"


def test_localized_path_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("FW_LOCAL_SCRATCH", str(tmp_path))
    lp = st._localized_path("s3://afl-cache/cache/noaa-weather/station-csv/USW1.csv")
    assert lp == str(tmp_path / "localized" / "afl-cache/cache/noaa-weather/station-csv/USW1.csv")
