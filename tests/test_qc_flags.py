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

from _noaa_tools.ghcn_qc import (  # noqa: E402
    QFLAG_MEANINGS,
    aggregate_region_qc,
    summarize_quality_flags,
)

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


# ---------------------------------------------------------------------------
# Region-level aggregation
# ---------------------------------------------------------------------------


def _station_rollup(station_id, name, total, flagged, by_element, by_flag):
    return {
        "station_id": station_id,
        "station_name": name,
        "total_obs": total,
        "flagged_obs": flagged,
        "flagged_pct": round(100.0 * flagged / total, 2) if total else 0.0,
        "by_element": by_element,
        "by_flag": by_flag,
    }


def test_region_rate_is_observation_weighted_not_a_mean_of_pcts():
    # Big clean station + tiny dirty station. A naive mean of percentages would
    # report ~50%; the observation-weighted rate is 2/10002 ≈ 0.02%.
    big = _station_rollup(
        "USBIG", "Big", 10000, 0,
        {"TMAX": {"total": 10000, "flagged": 0, "pct": 0.0}}, {},
    )
    tiny = _station_rollup(
        "USTINY", "Tiny", 2, 2,
        {"TMAX": {"total": 2, "flagged": 2, "pct": 100.0}},
        {"O": {"count": 2, "label": QFLAG_MEANINGS["O"]}},
    )
    agg = aggregate_region_qc([big, tiny], region_label="NY")

    assert agg["station_count"] == 2
    assert agg["total_obs"] == 10002
    assert agg["flagged_obs"] == 2
    assert agg["flagged_pct"] == 0.02  # weighted, not 50
    assert agg["by_element"]["TMAX"] == {"total": 10002, "flagged": 2, "pct": 0.02}
    assert agg["by_flag"]["O"]["count"] == 2
    # Worst station surfaces the tiny dirty record.
    assert agg["worst_stations"][0]["station_id"] == "USTINY"
    assert agg["worst_stations"][0]["flagged_pct"] == 100.0


def test_region_sums_flags_across_stations():
    a = _station_rollup(
        "USA", "A", 100, 3,
        {"PRCP": {"total": 100, "flagged": 3, "pct": 3.0}},
        {"G": {"count": 3, "label": QFLAG_MEANINGS["G"]}},
    )
    b = _station_rollup(
        "USB", "B", 100, 5,
        {"PRCP": {"total": 100, "flagged": 5, "pct": 5.0}},
        {"G": {"count": 2, "label": QFLAG_MEANINGS["G"]},
         "O": {"count": 3, "label": QFLAG_MEANINGS["O"]}},
    )
    agg = aggregate_region_qc([a, b], region_label="NY")

    assert agg["by_flag"]["G"]["count"] == 5  # 3 + 2
    assert agg["by_flag"]["O"]["count"] == 3
    # Sorted by descending count → G (5) before O (3).
    assert next(iter(agg["by_flag"])) == "G"
    assert agg["by_element"]["PRCP"]["flagged"] == 8


def test_region_empty_reports_zeros_not_none():
    agg = aggregate_region_qc([], region_label="NV")
    assert agg["station_count"] == 0
    assert agg["total_obs"] == 0
    assert agg["flagged_pct"] == 0.0
    assert agg["by_flag"] == {}
    assert agg["worst_stations"] == []
