"""Tests for GHCN quality-control flag surfacing.

The daily parser drops Q-flagged observations; ``summarize_quality_flags``
counts them so the rejection rate is visible. These tests build a tiny CSV in
the exact GHCN column order and assert the per-element / per-year / per-flag
breakdowns.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "src", "noaa_weather", "tools"),
)

from _noaa_tools.ghcn_qc import QFLAG_MEANINGS, summarize_quality_flags  # noqa: E402

# GHCN CSV columns: ID, DATE, ELEMENT, DATA_VALUE, M_FLAG, Q_FLAG, S_FLAG, OBS_TIME
HEADER = "ID,DATE,ELEMENT,DATA_VALUE,M_FLAG,Q_FLAG,S_FLAG,OBS_TIME\n"


def _write_csv(tmp_path, rows: list[tuple[str, str, str, str, str]]) -> str:
    """rows = (date, element, value, m_flag, q_flag) → write a station CSV."""
    path = os.path.join(str(tmp_path), "station.csv")
    with open(path, "w") as f:
        f.write(HEADER)
        for date, element, value, m_flag, q_flag in rows:
            f.write(f"USC00000001,{date},{element},{value},{m_flag},{q_flag},,\n")
    return path


def test_flagged_share_overall_and_per_element(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("20000101", "TMAX", "100", "", ""),   # clean
            ("20000102", "TMAX", "110", "", "O"),  # flagged (outlier)
            ("20000103", "TMIN", "10", "", ""),    # clean
            ("20000104", "PRCP", "50", "", "G"),   # flagged (gap)
        ],
    )
    s = summarize_quality_flags(path, 1990, 2010)

    assert s["total_obs"] == 4
    assert s["flagged_obs"] == 2
    assert s["flagged_pct"] == 50.0
    # TMAX: 2 obs, 1 flagged → 50%; PRCP: 1 obs, 1 flagged → 100%.
    assert s["by_element"]["TMAX"] == {"total": 2, "flagged": 1, "pct": 50.0}
    assert s["by_element"]["PRCP"] == {"total": 1, "flagged": 1, "pct": 100.0}
    assert s["by_element"]["TMIN"]["flagged"] == 0


def test_by_flag_breakdown_carries_human_labels(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("20010101", "TMAX", "100", "", "O"),
            ("20010102", "TMAX", "100", "", "O"),
            ("20010103", "PRCP", "50", "", "G"),
        ],
    )
    s = summarize_quality_flags(path, 1990, 2010)

    assert s["by_flag"]["O"]["count"] == 2
    assert s["by_flag"]["O"]["label"] == QFLAG_MEANINGS["O"]
    assert s["by_flag"]["G"]["count"] == 1
    # Sorted by descending count, so the outlier check (2) leads.
    assert next(iter(s["by_flag"])) == "O"


def test_clean_station_reports_zeros_not_none(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            ("20000101", "TMAX", "100", "", ""),
            ("20000102", "TMIN", "10", "", ""),
        ],
    )
    s = summarize_quality_flags(path, 1990, 2010)

    assert s["total_obs"] == 2
    assert s["flagged_obs"] == 0
    assert s["flagged_pct"] == 0.0
    assert s["by_flag"] == {}
    assert s["worst"] == []


def test_worst_cells_ranked_and_year_filter_applies(tmp_path):
    path = _write_csv(
        tmp_path,
        [
            # 2000: PRCP fully rejected; 2001: TMAX partly rejected.
            ("20000101", "PRCP", "50", "", "G"),
            ("20000102", "PRCP", "60", "", "G"),
            ("20010101", "TMAX", "100", "", "O"),
            ("20010102", "TMAX", "100", "", ""),
            # Out of range — must be excluded entirely.
            ("19800101", "TMAX", "100", "", "O"),
        ],
    )
    s = summarize_quality_flags(path, 1990, 2010)

    assert s["total_obs"] == 4  # the 1980 row is filtered out
    # Worst cell is PRCP/2000 at 100%, ahead of TMAX/2001 at 50%.
    assert s["worst"][0]["element"] == "PRCP"
    assert s["worst"][0]["year"] == 2000
    assert s["worst"][0]["pct"] == 100.0
    assert s["worst"][1]["element"] == "TMAX"
    assert s["worst"][1]["pct"] == 50.0
    assert "1980" not in s["by_year"]
