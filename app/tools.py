"""Tools the LLM can call. Each one is a thin wrapper over deterministic Python code.

Schemas use strict mode (additionalProperties: false, every property required, optional
values expressed as nullable), so the model's arguments always match the schema.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app import metrics, scoring
from app.config import hub as get_hub
from app.config import hubs, settings
from app.schemas import HAZARD_NAMES
from app.sources.fema_nri import HAZARDS as NRI_NAMES

HUB_IDS = [h["id"] for h in hubs()]
REGIONS = sorted({h["region"] for h in hubs()})


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": list(props), "properties": props}


TOOL_DEFS: list[dict] = [
    {
        "name": "list_hubs",
        "description": "List the company's distribution hubs (id, city, state, US Census region, "
                       "county). Use to resolve city names or region filters. Only these hubs "
                       "are covered.",
        "input_schema": _obj({"region": _nullable({"type": "string", "enum": REGIONS})}),
    },
    {
        "name": "rank_hubs",
        "description": "Rank hubs by the deterministic risk model, highest risk first. hazard="
                       "'overall' ranks by the weighted overall score; a hazard name ranks by "
                       "that hazard's 0-100 sub-score. Optional region filter.",
        "input_schema": _obj({
            "hazard": {"type": "string", "enum": ["overall", *HAZARD_NAMES]},
            "region": _nullable({"type": "string", "enum": REGIONS}),
        }),
    },
    {
        "name": "compare_hubs",
        "description": "Side-by-side hazard sub-scores for 2+ hubs, with every input component "
                       "(raw value, cap, normalised score). Use for 'compare X and Y' questions.",
        "input_schema": _obj({
            "hub_ids": {"type": "array", "items": {"type": "string", "enum": HUB_IDS}},
            "hazards": {"type": "array", "items": {"type": "string", "enum": HAZARD_NAMES}},
        }),
    },
    {
        "name": "get_score_breakdown",
        "description": "Full explanation of one hub's overall risk score: each hazard's score, "
                       "weight, contribution in points, raw inputs, caveats, and FEMA context "
                       "ratings. Use for 'why is X high/low' questions.",
        "input_schema": _obj({"hub_id": {"type": "string", "enum": HUB_IDS}}),
    },
    {
        "name": "weather_stat",
        "description": "Count days in a hub's observed daily weather (Open-Meteo ERA5) that meet a "
                       "condition, for one calendar year or the full history (year=null). Metrics: "
                       "snowfall (> threshold cm, default 0 = any snowfall), heavy_rain (>= mm, "
                       "default 25), hard_freeze (min temp <= C, default -12), high_gust (>= km/h, "
                       "default 60), extreme_heat (max temp >= C, default 38). threshold=null uses "
                       "the default.",
        "input_schema": _obj({
            "hub_id": {"type": "string", "enum": HUB_IDS},
            "metric": {"type": "string", "enum": list(metrics.METRICS)},
            "year": _nullable({"type": "integer"}),
            "threshold": _nullable({"type": "number"}),
        }),
    },
    {
        "name": "get_methodology",
        "description": "The scoring method: data sources, hazard components, weights, "
                       "normalisation caps, thresholds and tiers. Use for 'how is the score "
                       "calculated' or when you need to state assumptions.",
        "input_schema": _obj({}),
    },
]
for t in TOOL_DEFS:
    t["strict"] = True


# ---------- implementations ----------

def list_hubs(region: str | None = None) -> dict:
    rows = [{k: h[k] for k in ("id", "city", "state", "region", "county")}
            for h in hubs() if not region or h["region"] == region]
    return {"hubs": rows, "note": "Only these hubs are covered by the model."}


def rank_hubs(hazard: str = "overall", region: str | None = None) -> dict:
    return {"ranked_by": hazard, "region": region,
            "ranking": scoring.rank(None if hazard == "overall" else hazard, region),
            "method": "Deterministic scores 0-100; see get_methodology."}


def _hazard_view(b, wanted):
    return [{"hazard": h.hazard, "score": h.score, "weight": h.weight,
             "contribution": h.contribution,
             "components": [c.model_dump() for c in h.components]}
            for h in b.hazards if h.hazard in wanted]


def compare_hubs(hub_ids: list[str], hazards: list[str]) -> dict:
    wanted = hazards or HAZARD_NAMES
    out = []
    for hid in dict.fromkeys(hub_ids):
        b = scoring.score_hub(hid)
        out.append({"hub_id": hid, "city": b.city, "overall_score": b.overall_score,
                    "tier": b.tier, "confidence": b.confidence,
                    "hazards": _hazard_view(b, wanted), "fema_context": b.nri_context})
    return {"comparison": out}


def get_score_breakdown(hub_id: str) -> dict:
    return scoring.score_hub(hub_id).model_dump()


def weather_stat(hub_id: str, metric: str, year: int | None = None,
                 threshold: float | None = None) -> dict:
    return metrics.weather_stat(hub_id, metric, year, threshold)


def get_methodology() -> dict:
    cfg = settings()
    return {
        "sources": {
            "observed": "Open-Meteo Historical Weather API (ERA5 reanalysis, ~10 km grid), "
                        f"{cfg['data']['history_start']} to {cfg['data']['history_end']}",
            "hazard_frequency": "FEMA National Risk Index (county level), annual event "
                                "frequency fields (*_AFREQ)",
        },
        "hazards": {hz: {"observed": spec["observed"],
                         "fema_nri": [f"{p} ({NRI_NAMES[p]})" for p in spec["nri"]]}
                    for hz, spec in cfg["hazards"].items()},
        "thresholds": cfg["thresholds"],
        "normalisation": {
            "observed": f"linear 0-100, cap {cfg['normalisation']['observed_days_cap']} days/yr "
                        "(10% of days)",
            "fema_nri": f"linear 0-100, cap = national {cfg['normalisation']['nri_cap_percentile']}"
                        "th percentile of that frequency across all US counties",
            "hazard_score": "mean of the hazard's available components",
        },
        "weights": cfg["weights"],
        "tiers": cfg["tiers"],
        "not_used_in_score": "FEMA risk scores/ratings, expected annual loss, social "
                             "vulnerability, community resilience (context only)",
        "out_of_scope": "wildfire, earthquake, drought, hail, live alerts/forecasts, "
                        "cities that are not company hubs",
    }


REGISTRY: dict[str, Callable[..., Any]] = {
    "list_hubs": list_hubs, "rank_hubs": rank_hubs, "compare_hubs": compare_hubs,
    "get_score_breakdown": get_score_breakdown, "weather_stat": weather_stat,
    "get_methodology": get_methodology,
}


def execute(name: str, args: dict) -> tuple[str, bool]:
    """Run a tool. Returns (json_text, is_error). Never raises."""
    fn = REGISTRY.get(name)
    if fn is None:
        return json.dumps({"error": f"unknown tool {name}"}), True
    try:
        for key in ("hub_id",):
            if key in args and get_hub(args[key]) is None:
                return json.dumps({"error": f"unknown hub '{args[key]}'",
                                   "known_hubs": HUB_IDS}), True
        result = fn(**args)
        is_error = isinstance(result, dict) and "error" in result
        return json.dumps(result, default=str), is_error
    except Exception as e:  # surfaced to the model as a tool error, not a crash
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
