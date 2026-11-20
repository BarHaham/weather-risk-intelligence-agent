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


def test_snowfall_threshold_is_strictly_greater():
    rows = [{"date": "2025-01-01", "snowfall_cm": 0.0},
            {"date": "2025-01-02", "snowfall_cm": 0.07},
            {"date": "2025-01-03", "snowfall_cm": 1.0},
            {"date": "2025-01-04", "snowfall_cm": None}]
    hit, valid, _ = metrics._count(rows, "snowfall", 0.0)
    assert (hit, valid) == (2, 3)          # 0.0 is not a snow day; None is excluded
    hit, _, _ = metrics._count(rows, "snowfall", 1.0)
    assert hit == 0                        # "> 1 cm" excludes exactly 1.0
