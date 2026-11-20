"""Typed contracts: scoring outputs and the JSON schema the LLM's final answer must follow."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import hubs

HazardName = Literal["winter", "flood", "hurricane", "wind", "heat"]
HAZARD_NAMES = ["winter", "flood", "hurricane", "wind", "heat"]


# ---------- deterministic scoring outputs ----------

class Component(BaseModel):
    name: str                 # e.g. "snow_days" or "WNTW_AFREQ"
    label: str                # human-readable
    source: Literal["Open-Meteo ERA5 (observed)", "FEMA NRI (annual frequency)"]
    raw_value: float | None   # None = data unavailable
    unit: str
    cap: float                # value that maps to 100
    normalised: float | None  # 0-100


class HazardScore(BaseModel):
    hazard: HazardName
    score: float              # 0-100, mean of available components
    weight: float
    contribution: float       # weight * score, points of the overall score
    components: list[Component]


class ScoreBreakdown(BaseModel):
    hub_id: str
    city: str
    state: str
    region: str
    overall_score: float
    tier: str
    confidence: Literal["low", "medium", "high"]
    hazards: list[HazardScore]
    top_drivers: list[str]    # hazards ordered by contribution, largest first
    caveats: list[str]
    nri_context: dict         # FEMA ratings etc. - shown, never scored


# ---------- LLM final answer ----------

class HubRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hub_id: str
    hazard: Literal["overall", "winter", "flood", "hurricane", "wind", "heat"]
    score: float


class Figure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    value: float
    unit: str
    source_tool: str


class AgentAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    in_scope: bool
    answer: str
    hubs: list[HubRef]
    figures: list[Figure]
    reasoning_steps: list[str]
    data_sources: list[str]
    assumptions: list[str]
    confidence: Literal["low", "medium", "high"]
    follow_up_suggestions: list[str] = Field(max_length=3)


def answer_json_schema() -> dict:
    """Hand-written (not Pydantic-generated) so it only uses keywords structured outputs accepts."""
    hub_ids = [h["id"] for h in hubs()]
    s = lambda: {"type": "string"}  # noqa: E731
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["in_scope", "answer", "hubs", "figures", "reasoning_steps",
                     "data_sources", "assumptions", "confidence", "follow_up_suggestions"],
        "properties": {
            "in_scope": {"type": "boolean",
                         "description": "false if the question is outside weather-risk for our hubs"},
            "answer": {"type": "string", "description": "Markdown answer for the analyst"},
            "hubs": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["hub_id", "hazard", "score"],
                "properties": {
                    "hub_id": {"type": "string", "enum": hub_ids},
                    "hazard": {"type": "string",
                               "enum": ["overall", "winter", "flood", "hurricane", "wind", "heat"]},
                    "score": {"type": "number"}}}},
            "figures": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["label", "value", "unit", "source_tool"],
                "properties": {"label": s(), "value": {"type": "number"}, "unit": s(),
                               "source_tool": s()}}},
            "reasoning_steps": {"type": "array", "items": s()},
            "data_sources": {"type": "array", "items": s()},
            "assumptions": {"type": "array", "items": s()},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "follow_up_suggestions": {"type": "array", "items": s()},
        },
    }
