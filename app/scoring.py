"""Deterministic weather-disruption risk model. No LLM involved.

score(hazard)  = mean of the hazard's available normalised components (0-100)
overall        = sum(weight[hazard] * score(hazard))
normalisation  = linear 0..cap, clipped at 100, with caps fixed by rule (see config.yaml):
                   observed day counts  -> cap 36.5 days/yr (10% of days)
                   FEMA NRI frequencies -> cap = national 99th percentile across all US counties
"""
from __future__ import annotations

from app import metrics, store
from app.config import hub as get_hub
from app.config import hubs, settings
from app.schemas import HAZARD_NAMES, Component, HazardScore, ScoreBreakdown
from app.sources.fema_nri import HAZARDS as NRI_NAMES

OBS_LABELS = {
    "snow_days": "Snow days per year", "hard_freeze_days": "Hard-freeze days per year",
    "heavy_rain_days": "Heavy-rain days per year", "high_gust_days": "High-gust days per year",
    "extreme_heat_days": "Extreme-heat days per year",
}


def normalise(value: float | None, cap: float) -> float | None:
    if value is None:
        return None
    if cap <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * value / cap)), 1)


def tier(score: float, tiers: list[dict]) -> str:
    name = tiers[0]["name"]
    for t in sorted(tiers, key=lambda t: t["min"]):
        if score >= t["min"]:
            name = t["name"]
    return name


def compute_breakdown(hub: dict, observed: dict, nri_record: dict | None,
                      caps: dict[str, float], cfg: dict) -> ScoreBreakdown:
    """Pure function: all inputs passed in, so it can be unit-tested on synthetic data."""
    obs_cap = cfg["normalisation"]["observed_days_cap"]
    weights = cfg["weights"]
    caveats: list[str] = []
    hazards: list[HazardScore] = []
    missing_obs = missing_nri = False

    for hz in HAZARD_NAMES:
        spec = cfg["hazards"][hz]
        comps: list[Component] = []
        for name in spec["observed"]:
            raw = observed.get(name)
            missing_obs |= raw is None
            comps.append(Component(name=name, label=OBS_LABELS[name],
                                   source="Open-Meteo (observed, gridded estimate)", raw_value=raw,
                                   unit="days/yr", cap=obs_cap, normalised=normalise(raw, obs_cap)))
        for prefix in spec["nri"]:
            field = f"{prefix}_AFREQ"
            if nri_record is None:
                raw = None
                missing_nri = True
            else:
                # NRI null frequency = hazard not applicable to the county -> 0 events/yr.
                raw = nri_record.get(field) or 0.0
            cap = caps.get(field, 0.0)
            comps.append(Component(name=field, label=f"{NRI_NAMES[prefix]} events per year (FEMA)",
                                   source="FEMA NRI (annual frequency)",
                                   raw_value=None if raw is None else round(raw, 3),
                                   unit="events/yr", cap=round(cap, 3),
                                   normalised=normalise(raw, cap)))
        avail = [c.normalised for c in comps if c.normalised is not None]
        score = round(sum(avail) / len(avail), 1) if avail else 0.0
        w = weights[hz]
        hazards.append(HazardScore(hazard=hz, score=score, weight=w,
                                   contribution=round(w * score, 1), components=comps))

    overall = round(sum(h.weight * h.score for h in hazards), 1)
    if missing_obs:
        caveats.append("Observed weather history is not available for this hub yet; hazard "
                       "scores use FEMA NRI frequencies only.")
    else:
        caveats.append(metrics.GRID_CAVEAT)
    if missing_nri:
        caveats.append("FEMA NRI record missing for this county; scores use observed weather only.")
    caveats.append("FEMA NRI frequencies describe the whole county, not the hub's exact site.")

    confidence = "low" if missing_nri else ("medium" if missing_obs else "high")
    ctx = {}
    if nri_record:
        ctx = {"county": f"{nri_record.get('COUNTY')}, {nri_record.get('STATEABBRV')}",
               "nri_version": nri_record.get("NRI_VER"),
               "ratings": {NRI_NAMES[p]: nri_record.get(f"{p}_RISKR") for p in NRI_NAMES},
               "note": "FEMA risk ratings combine frequency with the dollar value exposed in the "
                       "whole county; shown for context, not used in the score."}
    return ScoreBreakdown(
        hub_id=hub["id"], city=hub["city"], state=hub["state"], region=hub["region"],
        overall_score=overall, tier=tier(overall, cfg["tiers"]), confidence=confidence,
        hazards=hazards,
        top_drivers=[h.hazard for h in sorted(hazards, key=lambda h: -h.contribution)],
        caveats=caveats, nri_context=ctx)


def score_hub(hub_id: str) -> ScoreBreakdown:
    h = get_hub(hub_id)
    if h is None:
        raise KeyError(hub_id)
    observed = metrics.annual_days(h["id"])
    return compute_breakdown(h, observed, store.nri(h["fips"]), store.nri_caps(), settings())


def rank(hazard: str | None = None, region: str | None = None) -> list[dict]:
    """Hubs sorted by overall score, or by one hazard's sub-score, highest first."""
    rows = []
    for h in hubs():
        if region and h["region"].lower() != region.lower():
            continue
        b = score_hub(h["id"])
        hz = {x.hazard: x.score for x in b.hazards}
        rows.append({"hub_id": b.hub_id, "city": b.city, "region": b.region,
                     "score": hz[hazard] if hazard else b.overall_score,
                     "ranked_by": hazard or "overall",
                     "overall_score": b.overall_score, "tier": b.tier,
                     "confidence": b.confidence})
    rows.sort(key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows
