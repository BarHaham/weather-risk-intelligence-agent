"""Unit tests for the deterministic model. Synthetic inputs only, never real hub outcomes."""
import pytest

from app import metrics
from app.config import settings
from app.scoring import compute_breakdown, normalise, tier
from app.store import percentile

CFG = settings()
HUB = {"id": "test", "city": "Testville", "state": "TS", "region": "Midwest"}
FIELDS = ["HRCN", "CFLD", "IFLD", "WNTW", "ISTM", "CWAV", "TRND", "SWND", "HWAV"]
CAPS = {f"{p}_AFREQ": 10.0 for p in FIELDS}
OBS_FULL = {"snow_days": 18.25, "hard_freeze_days": 0.0, "heavy_rain_days": 36.5,
            "high_gust_days": 73.0, "extreme_heat_days": 0.0}


def nri(value=5.0, **overrides):
    rec = {f"{p}_AFREQ": value for p in FIELDS}
    rec.update(overrides)
    return rec


def test_normalise_linear_and_clipped():
    assert normalise(5, 10) == 50.0
    assert normalise(20, 10) == 100.0
    assert normalise(-1, 10) == 0.0
    assert normalise(None, 10) is None
    assert normalise(3, 0) == 0.0


def test_tier_boundaries_are_inclusive_lower_bounds():
    tiers = CFG["tiers"]
    assert tier(0, tiers) == "Low"
    assert tier(34.9, tiers) == "Low"
    assert tier(35, tiers) == "Moderate"
    assert tier(55, tiers) == "High"
    assert tier(75, tiers) == "Severe"
    assert tier(100, tiers) == "Severe"


def test_weights_sum_to_one():
    assert sum(CFG["weights"].values()) == pytest.approx(1.0)


def test_hazard_score_is_mean_of_components():
    b = compute_breakdown(HUB, OBS_FULL, nri(5.0), CAPS, CFG)
    winter = next(h for h in b.hazards if h.hazard == "winter")
    # snow 18.25/36.5=50, freeze 0, three NRI fields at 5/10=50 -> mean (50+0+50*3)/5 = 40
    assert winter.score == pytest.approx(40.0)
    wind = next(h for h in b.hazards if h.hazard == "wind")
    # gusts 73 days -> clipped 100, two NRI at 50 -> 66.7
    assert wind.score == pytest.approx(66.7, abs=0.05)


def test_overall_is_weighted_sum_and_matches_contributions():
    b = compute_breakdown(HUB, OBS_FULL, nri(5.0), CAPS, CFG)
    expected = sum(CFG["weights"][h.hazard] * h.score for h in b.hazards)
    assert b.overall_score == pytest.approx(expected, abs=0.1)
    assert sum(h.contribution for h in b.hazards) == pytest.approx(b.overall_score, abs=0.3)
    assert b.top_drivers[0] == max(b.hazards, key=lambda h: h.contribution).hazard


def test_null_nri_frequency_counts_as_zero():
    b = compute_breakdown(HUB, OBS_FULL, nri(5.0, HRCN_AFREQ=None), CAPS, CFG)
    hurricane = next(h for h in b.hazards if h.hazard == "hurricane")
    assert hurricane.score == 0.0
    assert hurricane.components[0].raw_value == 0.0


def test_missing_observed_data_uses_nri_only_and_lowers_confidence():
    empty = {k: None for k in OBS_FULL}
    b = compute_breakdown(HUB, empty, nri(5.0), CAPS, CFG)
    assert b.confidence == "medium"
    assert any("Observed weather history is not available" in c for c in b.caveats)
    winter = next(h for h in b.hazards if h.hazard == "winter")
    assert winter.score == pytest.approx(50.0)  # only the three NRI components


def test_missing_nri_gives_low_confidence():
    b = compute_breakdown(HUB, OBS_FULL, None, CAPS, CFG)
    assert b.confidence == "low"


def test_score_does_not_depend_on_other_hubs():
    """Fixed caps: the same inputs always give the same score, whatever else is in the set."""
    a = compute_breakdown(HUB, OBS_FULL, nri(5.0), CAPS, CFG)
    b = compute_breakdown(HUB, OBS_FULL, nri(5.0), CAPS, CFG)
    assert a.overall_score == b.overall_score


def test_percentile_linear_interpolation():
    assert percentile([0, 10], 50) == 5
    assert percentile(list(range(101)), 99) == 99
    assert percentile([3.0], 99) == 3.0


SNOW_ROWS = [{"date": "2025-01-01", "snowfall_cm": 0.0},
             {"date": "2025-01-02", "snowfall_cm": 0.07},
             {"date": "2025-01-03", "snowfall_cm": 0.99},
             {"date": "2025-01-04", "snowfall_cm": 1.0},   # exactly 1 cm
             {"date": "2025-01-05", "snowfall_cm": 1.5},
             {"date": "2025-01-06", "snowfall_cm": None}]


def test_snowfall_rule_any_snowfall_is_strictly_above_zero():
    hit, valid, _ = metrics._count(SNOW_ROWS, "snowfall", 0.0)
    assert (hit, valid) == (4, 5)          # 0.0 is not a snow day; None is excluded


def test_snowfall_threshold_counts_a_day_with_exactly_that_amount():
    hit, _, _ = metrics._count(SNOW_ROWS, "snowfall", 1.0)
    assert hit == 2                        # 1.0 (exactly 1 cm) and 1.5; not 0.99
    hit, _, _ = metrics._count(SNOW_ROWS, "snowfall", 1.5)
    assert hit == 1


def test_snowfall_comparison_and_condition_wording():
    assert metrics.comparison("snowfall", 0) == ">"
    assert metrics.comparison("snowfall", 1) == ">="
    assert metrics.condition_text("snowfall", 0) == "daily snowfall > 0 cm (any snowfall)"
    assert metrics.condition_text("snowfall", 1) == "daily snowfall >= 1 cm (at least 1 cm)"
    assert metrics.condition_text("snowfall", 2.5) == "daily snowfall >= 2.5 cm (at least 2.5 cm)"
    with pytest.raises(ValueError, match="0 or more"):
        metrics.comparison("snowfall", -1)
    # other metrics are unchanged, and negative thresholds stay valid where they make sense
    assert metrics.comparison("hard_freeze", -12) == "<="
    assert metrics.comparison("heavy_rain", 25) == ">="


def test_weather_stat_uses_the_rule_and_the_gridded_caveat(monkeypatch):
    monkeypatch.setattr(metrics.store, "daily", lambda hub_id, year=None: SNOW_ROWS)
    monkeypatch.setattr(metrics.store, "grid_point", lambda hub_id: None)
    at_least = metrics.weather_stat("denver", "snowfall", 2025, 1)
    assert at_least["condition"] == "daily snowfall >= 1 cm (at least 1 cm)"
    assert (at_least["days_meeting_condition"], at_least["days_with_data"]) == (2, 5)
    assert at_least["by_month"] == {"2025-01": 2}
    default = metrics.weather_stat("denver", "snowfall", 2025, None)
    assert default["condition"] == "daily snowfall > 0 cm (any snowfall)"
    assert default["days_meeting_condition"] == 4
    for text in (at_least["caveat"], at_least["source"]):
        assert "ERA5" not in text and "10 km" not in text
    assert "gridded estimates for the area" in at_least["caveat"]
    assert "can differ from station records" in at_least["caveat"]


def test_negative_snowfall_threshold_is_rejected(monkeypatch):
    called = []
    monkeypatch.setattr(metrics.store, "daily", lambda *a, **k: called.append(1) or SNOW_ROWS)
    r = metrics.weather_stat("denver", "snowfall", 2025, -1)
    assert r["error"] == "snowfall threshold must be 0 or more (got -1)"
    assert r["invalid_argument"] == "threshold" and "days_meeting_condition" not in r
    assert not called                      # rejected before any data is read or counted
