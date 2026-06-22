"""Detect extreme weather events from GHCN-Daily station records.

Pure, dependency-free analysis over the daily-record list produced by
``ghcn_parse.parse_ghcn_csv`` — each record is
``{"date": "YYYY-MM-DD", "tmax", "tmin", "prcp", "snow", "snwd"}`` in °C / mm,
with ``None`` for a missing/QC-flagged value. Shared by BOTH the
``detect-extremes`` CLI and the ``weather.Extremes.DetectStationExtremes``
handler (the tools/handler shared-library contract).

Event types — every threshold is configurable; the defaults are common
climatological conventions a human can reason about:

  - ``heat_wave``  : >= ``heat_wave_min_days`` consecutive days, tmax >= ``heat_wave_tmax_c``
  - ``cold_snap``  : >= ``cold_snap_min_days`` consecutive days, tmin <= ``cold_snap_tmin_c``
  - ``wet_spell``  : >= ``wet_spell_min_days`` consecutive wet days (prcp >= ``wet_day_mm``)
  - ``dry_spell``  : >= ``dry_spell_min_days`` consecutive dry days (prcp < ``wet_day_mm``)
  - ``heavy_rain`` : a single day with prcp >= ``heavy_rain_mm``
  - ``heavy_snow`` : a single day with snow >= ``heavy_snow_mm``

"Consecutive" means consecutive CALENDAR days; a missing value or a gap in the
record breaks a run (we never interpolate). Each event carries its span,
duration, the headline ``peak_value`` (type-specific, see ``_PEAK_META``), and
the year it began — so callers can chart frequency/intensity over decades.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

# Headline metric per event type: how ``peak_value`` is computed over the run.
#   max  -> hottest tmax (heat_wave)        min -> coldest tmin (cold_snap)
#   sum  -> total precip mm (wet_spell)     len -> number of days (dry_spell)
#   day  -> that day's value (heavy_rain / heavy_snow)
_PEAK_META = {
    "heat_wave": ("tmax", "max", "hottest daily high °C"),
    "cold_snap": ("tmin", "min", "coldest daily low °C"),
    "wet_spell": ("prcp", "sum", "total precip mm"),
    "dry_spell": ("prcp", "len", "consecutive dry days"),
    "heavy_rain": ("prcp", "day", "rainfall mm"),
    "heavy_snow": ("snow", "day", "snowfall mm"),
}


@dataclass
class ExtremeConfig:
    """Tunable thresholds. Defaults match common climatological conventions."""
    heat_wave_tmax_c: float = 35.0
    heat_wave_min_days: int = 3
    cold_snap_tmin_c: float = -10.0
    cold_snap_min_days: int = 3
    heavy_rain_mm: float = 50.0
    wet_day_mm: float = 1.0
    wet_spell_min_days: int = 5
    dry_spell_min_days: int = 21
    heavy_snow_mm: float = 100.0

    @classmethod
    def from_params(cls, params: dict | None) -> "ExtremeConfig":
        """Build from a dict of optional overrides; ``None``/absent keys keep the
        default, and numeric strings are coerced — so FFL params pass straight in."""
        cfg = cls()
        if not params:
            return cfg
        for f in cls.__dataclass_fields__:
            v = params.get(f)
            if v is None or v == "":
                continue
            caster = int if isinstance(getattr(cfg, f), int) else float
            try:
                setattr(cfg, f, caster(v))
            except (TypeError, ValueError):
                pass
        return cfg


def _safe_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _consecutive_runs(daily, key, predicate, min_days):
    """Maximal runs of consecutive calendar days where ``predicate(value)`` holds
    and the value is present. Returns a list of runs; each run is ``[(date, value)]``."""
    events, run, prev = [], [], None
    for d in daily:
        dt = _safe_date(d.get("date", ""))
        if dt is None:
            continue
        v = d.get(key)
        ok = v is not None and predicate(v)
        if ok and run and prev is not None and (dt - prev).days == 1:
            run.append((dt, v))
        else:
            if len(run) >= min_days:
                events.append(run)
            run = [(dt, v)] if ok else []
        prev = dt
    if len(run) >= min_days:
        events.append(run)
    return events


def _threshold_days(daily, key, predicate):
    """One ``(date, value)`` per day whose present value satisfies ``predicate``."""
    out = []
    for d in daily:
        dt = _safe_date(d.get("date", ""))
        v = d.get(key)
        if dt is not None and v is not None and predicate(v):
            out.append((dt, v))
    return out


def _peak(values, mode):
    if mode == "max":
        return max(values)
    if mode == "min":
        return min(values)
    if mode == "sum":
        return sum(values)
    return len(values)  # "len"


def _run_event(etype, run):
    _, mode, _ = _PEAK_META[etype]
    dates = [d for d, _ in run]
    vals = [v for _, v in run]
    return {
        "type": etype,
        "start_date": dates[0].isoformat(),
        "end_date": dates[-1].isoformat(),
        "duration_days": len(run),
        "peak_value": round(_peak(vals, mode), 2),
        "year": dates[0].year,
    }


def _day_event(etype, dt, value):
    return {
        "type": etype,
        "start_date": dt.isoformat(),
        "end_date": dt.isoformat(),
        "duration_days": 1,
        "peak_value": round(value, 2),
        "year": dt.year,
    }


def detect_events(daily: list[dict], config: ExtremeConfig | None = None) -> dict:
    """Detect all extreme events in ``daily`` (a parse_ghcn_csv record list).

    Returns ``{events, event_count, counts_by_type, decadal_frequency, config}``.
    ``events`` is sorted by start date; ``decadal_frequency`` is
    ``{type: {decade: count}}`` for charting intensity/frequency over time.
    """
    config = config or ExtremeConfig()
    daily = sorted(daily, key=lambda d: d.get("date", ""))
    events: list[dict] = []

    for run in _consecutive_runs(daily, "tmax",
                                 lambda v: v >= config.heat_wave_tmax_c, config.heat_wave_min_days):
        events.append(_run_event("heat_wave", run))
    for run in _consecutive_runs(daily, "tmin",
                                 lambda v: v <= config.cold_snap_tmin_c, config.cold_snap_min_days):
        events.append(_run_event("cold_snap", run))
    for run in _consecutive_runs(daily, "prcp",
                                 lambda v: v >= config.wet_day_mm, config.wet_spell_min_days):
        events.append(_run_event("wet_spell", run))
    for run in _consecutive_runs(daily, "prcp",
                                 lambda v: v < config.wet_day_mm, config.dry_spell_min_days):
        events.append(_run_event("dry_spell", run))
    for dt, v in _threshold_days(daily, "prcp", lambda v: v >= config.heavy_rain_mm):
        events.append(_day_event("heavy_rain", dt, v))
    for dt, v in _threshold_days(daily, "snow", lambda v: v >= config.heavy_snow_mm):
        events.append(_day_event("heavy_snow", dt, v))

    events.sort(key=lambda e: e["start_date"])

    counts_by_type: dict[str, int] = {}
    decadal: dict[str, dict[str, int]] = {}
    for e in events:
        counts_by_type[e["type"]] = counts_by_type.get(e["type"], 0) + 1
        decade = f"{(e['year'] // 10) * 10}s"
        decadal.setdefault(e["type"], {})
        decadal[e["type"]][decade] = decadal[e["type"]].get(decade, 0) + 1

    return {
        "events": events,
        "event_count": len(events),
        "counts_by_type": counts_by_type,
        "decadal_frequency": decadal,
        "config": asdict(config),
    }


def summarize(result: dict, *, label: str = "this station") -> str:
    """A one-line human narrative of the detected events (handler step-log/report)."""
    c = result["counts_by_type"]
    if not result["event_count"]:
        return f"No extreme events detected for {label} with the given thresholds."
    parts = [f"{n} {t.replace('_', ' ')}{'s' if n != 1 else ''}" for t, n in sorted(c.items())]
    return f"{label}: {result['event_count']} extreme events — " + ", ".join(parts) + "."
