"""Observed weather metrics from the daily ERA5 table: day counts and percentages."""
from __future__ import annotations

from collections import Counter

from app import store
from app.config import settings

# metric -> (column, comparison, threshold key in config, unit of threshold, label)
METRICS = {
    "snowfall":     ("snowfall_cm", ">",  "snow_day_cm",    "cm",   "snowfall"),
    "heavy_rain":   ("precip_mm",   ">=", "heavy_rain_mm",  "mm",   "precipitation"),
    "hard_freeze":  ("tmin_c",      "<=", "hard_freeze_c",  "°C",   "minimum temperature"),
    "high_gust":    ("gust_kmh",    ">=", "high_gust_kmh",  "km/h", "max wind gust"),
    "extreme_heat": ("tmax_c",      ">=", "extreme_heat_c", "°C",   "maximum temperature"),
}
# scoring component name -> metric
SCORING_METRICS = {
    "snow_days": "snowfall", "hard_freeze_days": "hard_freeze",
    "heavy_rain_days": "heavy_rain", "high_gust_days": "high_gust",
    "extreme_heat_days": "extreme_heat",
}

ERA5_CAVEAT = ("Open-Meteo ERA5 reanalysis is a ~10 km model grid, not a weather station; it "
               "under-counts local extremes (e.g. Front Range snowfall) and light-snow days can "
               "differ from station records.")

_OPS = {">": lambda v, t: v > t, ">=": lambda v, t: v >= t, "<=": lambda v, t: v <= t}


def default_threshold(metric: str) -> float:
    return float(settings()["thresholds"][METRICS[metric][2]])


def _count(rows, metric: str, threshold: float) -> tuple[int, int, Counter]:
    col, op, *_ = METRICS[metric]
    hit, valid, by_month = 0, 0, Counter()
    for r in rows:
        v = r[col]
        if v is None:
            continue
        valid += 1
        if _OPS[op](v, threshold):
            hit += 1
            by_month[r["date"][:7]] += 1
    return hit, valid, by_month


def weather_stat(hub_id: str, metric: str, year: int | None = None,
                 threshold: float | None = None) -> dict:
    """Days meeting a condition in one calendar year (or the whole history if year is None)."""
    if metric not in METRICS:
        return {"error": f"unknown metric '{metric}'", "valid_metrics": list(METRICS)}
    if threshold is None:
        threshold = default_threshold(metric)
    rows = store.daily(hub_id, year)
    if not rows:
        d = settings()["data"]
        return {"error": "no observed weather data available for this hub/period",
                "hub_id": hub_id, "year": year,
                "available_period": f"{d['history_start']} to {d['history_end']} (if cached)"}
    col, op, _, unit, label = METRICS[metric]
    hit, valid, by_month = _count(rows, metric, threshold)
    gp = store.grid_point(hub_id)
    return {
        "hub_id": hub_id, "metric": metric,
        "condition": f"daily {label} {op} {threshold:g} {unit}",
        "period": str(year) if year else f"{rows[0]['date']} to {rows[-1]['date']}",
        "days_meeting_condition": hit, "days_with_data": valid,
        "percent_of_days": round(100 * hit / valid, 1) if valid else None,
        "by_month": dict(sorted(by_month.items())) if year else None,
        "grid_point": gp,
        "source": "Open-Meteo Historical Weather API (ERA5 reanalysis)",
        "caveat": ERA5_CAVEAT,
    }


def annual_days(hub_id: str) -> dict[str, float | None]:
    """Average days/year meeting each scoring threshold, over full calendar years only."""
    rows = store.daily(hub_id)
    by_year: dict[str, list] = {}
    for r in rows:
        by_year.setdefault(r["date"][:4], []).append(r)
    full_years = {y: rs for y, rs in by_year.items() if len(rs) >= 365}
    out: dict[str, float | None] = {}
    for comp, metric in SCORING_METRICS.items():
        if not full_years:
            out[comp] = None
            continue
        t = default_threshold(metric)
        total = sum(_count(rs, metric, t)[0] for rs in full_years.values())
        out[comp] = round(total / len(full_years), 2)
    out["_years"] = sorted(full_years)  # type: ignore[assignment]
    return out
