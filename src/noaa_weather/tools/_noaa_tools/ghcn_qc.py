"""GHCN-Daily quality-control surfacing (no I/O).

The daily parser (:mod:`_noaa_tools.ghcn_parse`) silently drops every
observation whose ``Q_FLAG`` is non-empty — i.e. any value that failed one of
NOAA's quality-control checks. That keeps the climate analysis clean, but it
hides *how much* of a station's record was rejected, which is exactly the
number a careful reader wants before trusting a trend.

This module re-reads the same per-station CSV and **counts** the flagged
observations instead of skipping them: overall, per element, per year, and
broken down by the specific QC check that tripped (the flag letter). It is a
pure function with no filesystem, network, or database side effects beyond
reading the file path it is handed.

NOAA reference (Q_FLAG meanings):
    https://www1.ncdc.noaa.gov/pub/data/ghcn/daily/readme.txt
"""

from __future__ import annotations

import csv
from typing import Any

# The same five elements the climate analysis consumes. Restricting the QC
# summary to these keeps the "% of the data we actually use that was rejected"
# story honest — counting flags on elements we never read would inflate the
# denominator with data irrelevant to the trends.
_QC_ELEMENT_SET = {"TMAX", "TMIN", "PRCP", "SNOW", "SNWD"}

# Q_FLAG letter → which QC check failed (verbatim from the GHCN-Daily readme).
QFLAG_MEANINGS: dict[str, str] = {
    "D": "failed duplicate check",
    "G": "failed gap check",
    "I": "failed internal consistency check",
    "K": "failed streak/frequent-value check",
    "L": "failed check on length of multiday period",
    "M": "failed megaconsistency check",
    "N": "failed naught check",
    "O": "failed climatological outlier check",
    "R": "failed lagged range check",
    "S": "failed spatial consistency check",
    "T": "failed temporal consistency check",
    "W": "temperature too warm for snow",
    "X": "failed bounds check",
    "Z": "flagged by an official Datzilla investigation",
}


def _pct(flagged: int, total: int) -> float:
    """Flagged share of total, as a rounded percentage (0.0 when no data)."""
    if total <= 0:
        return 0.0
    return round(100.0 * flagged / total, 2)


def summarize_quality_flags(
    path: str,
    start_year: int,
    end_year: int,
    *,
    elements: set[str] | None = None,
    worst_limit: int = 10,
) -> dict[str, Any]:
    """Count Q-flagged GHCN observations to surface a station's QC rejection rate.

    Re-reads the per-station CSV (same columns as :func:`parse_ghcn_csv`:
    ``ID, DATE, ELEMENT, DATA_VALUE, M_FLAG, Q_FLAG, S_FLAG, OBS_TIME``) and,
    for every recognized element observation in ``[start_year, end_year]``,
    counts it as flagged when ``Q_FLAG`` is non-empty.

    Returns a dict with the overall flagged share plus three breakdowns —
    per element, per year, and per QC-check letter — and a ``worst`` list of
    the highest-rejection (element, year) cells. Counts are ``int``; ``*_pct``
    fields are rounded percentages. All-clean stations report zeros, never
    ``None`` (an empty summary still answers "how much was rejected? none").
    """
    elem_set = _QC_ELEMENT_SET if elements is None else set(elements)

    total = 0
    flagged = 0
    by_element: dict[str, dict[str, int]] = {}
    by_year: dict[int, dict[str, int]] = {}
    by_flag: dict[str, int] = {}
    # (element, year) -> {"total", "flagged"} for the worst-cell ranking.
    cells: dict[tuple[str, int], dict[str, int]] = {}

    with open(path, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            date_str = row[1]
            element = row[2]
            q_flag = row[5].strip()

            if date_str == "DATE":
                continue
            if len(date_str) < 4 or element not in elem_set:
                continue
            try:
                year = int(date_str[:4])
            except ValueError:
                continue
            if year < start_year or year > end_year:
                continue

            total += 1
            elem_rec = by_element.setdefault(element, {"total": 0, "flagged": 0})
            year_rec = by_year.setdefault(year, {"total": 0, "flagged": 0})
            cell = cells.setdefault((element, year), {"total": 0, "flagged": 0})
            elem_rec["total"] += 1
            year_rec["total"] += 1
            cell["total"] += 1

            if q_flag:
                flagged += 1
                elem_rec["flagged"] += 1
                year_rec["flagged"] += 1
                cell["flagged"] += 1
                # A Q_FLAG can in principle carry more than one letter; count
                # each distinct check that tripped.
                for letter in set(q_flag):
                    by_flag[letter] = by_flag.get(letter, 0) + 1

    by_element_out = {
        elem: {
            "total": rec["total"],
            "flagged": rec["flagged"],
            "pct": _pct(rec["flagged"], rec["total"]),
        }
        for elem, rec in sorted(by_element.items())
    }
    by_year_out = {
        str(year): {
            "total": rec["total"],
            "flagged": rec["flagged"],
            "pct": _pct(rec["flagged"], rec["total"]),
        }
        for year, rec in sorted(by_year.items())
    }
    by_flag_out = {
        letter: {
            "count": count,
            "label": QFLAG_MEANINGS.get(letter, "unknown flag"),
        }
        for letter, count in sorted(by_flag.items(), key=lambda kv: (-kv[1], kv[0]))
    }
    worst = sorted(
        (
            {
                "element": elem,
                "year": year,
                "total": rec["total"],
                "flagged": rec["flagged"],
                "pct": _pct(rec["flagged"], rec["total"]),
            }
            for (elem, year), rec in cells.items()
            if rec["flagged"] > 0
        ),
        key=lambda c: (-c["pct"], -c["flagged"], c["element"], c["year"]),
    )[:worst_limit]

    return {
        "start_year": start_year,
        "end_year": end_year,
        "total_obs": total,
        "flagged_obs": flagged,
        "flagged_pct": _pct(flagged, total),
        "by_element": by_element_out,
        "by_year": by_year_out,
        "by_flag": by_flag_out,
        "worst": worst,
    }


def aggregate_region_qc(
    per_station: list[dict[str, Any]],
    *,
    region_label: str = "",
    worst_limit: int = 10,
) -> dict[str, Any]:
    """Roll per-station QC summaries up into one region-wide rejection rate.

    Each input is a per-station rollup as persisted by the QC handler — it must
    carry ``total_obs`` / ``flagged_obs`` and the ``by_element`` / ``by_flag``
    breakdowns (the same shapes :func:`summarize_quality_flags` returns). The
    region flagged % is **observation-weighted** (summed counts, not a mean of
    per-station percentages) so a tiny station can't swing the headline, and the
    same per-element / per-check breakdowns are summed across stations. Returns
    zeros (never ``None``) for an empty region.

    Also returns ``worst_stations`` — the stations with the highest rejection
    rate — so a reader can see whether the region's flags are spread evenly or
    concentrated in a few bad records.
    """
    station_count = len(per_station)
    total = sum(int(s.get("total_obs", 0)) for s in per_station)
    flagged = sum(int(s.get("flagged_obs", 0)) for s in per_station)

    by_element: dict[str, dict[str, int]] = {}
    by_flag: dict[str, int] = {}
    for s in per_station:
        for elem, rec in (s.get("by_element") or {}).items():
            acc = by_element.setdefault(elem, {"total": 0, "flagged": 0})
            acc["total"] += int(rec.get("total", 0))
            acc["flagged"] += int(rec.get("flagged", 0))
        for letter, rec in (s.get("by_flag") or {}).items():
            # Persisted rollups store {count, label}; tolerate a bare int too.
            count = rec.get("count", 0) if isinstance(rec, dict) else int(rec)
            by_flag[letter] = by_flag.get(letter, 0) + int(count)

    by_element_out = {
        elem: {
            "total": rec["total"],
            "flagged": rec["flagged"],
            "pct": _pct(rec["flagged"], rec["total"]),
        }
        for elem, rec in sorted(by_element.items())
    }
    by_flag_out = {
        letter: {
            "count": count,
            "label": QFLAG_MEANINGS.get(letter, "unknown flag"),
        }
        for letter, count in sorted(by_flag.items(), key=lambda kv: (-kv[1], kv[0]))
    }
    worst_stations = sorted(
        (
            {
                "station_id": s.get("station_id", ""),
                "station_name": s.get("station_name", ""),
                "total_obs": int(s.get("total_obs", 0)),
                "flagged_obs": int(s.get("flagged_obs", 0)),
                "flagged_pct": s.get(
                    "flagged_pct", _pct(int(s.get("flagged_obs", 0)), int(s.get("total_obs", 0)))
                ),
            }
            for s in per_station
            if int(s.get("flagged_obs", 0)) > 0
        ),
        key=lambda c: (-c["flagged_pct"], -c["flagged_obs"], c["station_id"]),
    )[:worst_limit]

    return {
        "region": region_label,
        "station_count": station_count,
        "total_obs": total,
        "flagged_obs": flagged,
        "flagged_pct": _pct(flagged, total),
        "by_element": by_element_out,
        "by_flag": by_flag_out,
        "worst_stations": worst_stations,
    }
