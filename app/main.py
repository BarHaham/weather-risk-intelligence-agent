"""FastAPI app: the chat endpoint the UI uses, plus read-only data endpoints."""
from __future__ import annotations

import os
from pathlib import Path

import anthropic
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import agent, metrics, scoring, store
from app.config import hub as get_hub
from app.config import hubs, settings
from app.tools import get_methodology


class UTF8JSONResponse(JSONResponse):
    # Declare the charset: without it, Windows PowerShell 5.1 decodes JSON as ISO-8859-1
    # and shows non-ASCII characters (°, em dash, ≤) as mojibake.
    media_type = "application/json; charset=utf-8"


app = FastAPI(title="Weather Risk Intelligence Agent", version="0.1.0",
              default_response_class=UTF8JSONResponse)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request, exc: StarletteHTTPException):
    # Error responses bypass default_response_class; give them the same charset.
    return UTF8JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    store.build_from_cache()


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health():
    return {"status": "ok",
            "observed_weather_hubs": [h["id"] for h in hubs() if store.has_weather(h["id"])],
            "nri_loaded": bool(store.nri_caps()),
            "llm_model": settings()["llm"]["model"],
            "anthropic_api_key_env_set": bool(os.environ.get("ANTHROPIC_API_KEY"))}


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        return agent.chat(req.message, req.session_id)
    except TypeError as e:  # SDK raises this when no credentials can be resolved at all
        if "authentication" not in str(e).lower():
            raise
        raise HTTPException(503, "No Anthropic credentials found: set ANTHROPIC_API_KEY.")
    except anthropic.AuthenticationError:
        raise HTTPException(503, "Anthropic credentials missing or invalid: set ANTHROPIC_API_KEY.")
    except anthropic.RateLimitError:
        raise HTTPException(429, "Anthropic rate limit hit; retry shortly.")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Anthropic API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise HTTPException(502, "Could not reach the Anthropic API.")


@app.get("/hubs")
def list_hubs():
    return hubs()


@app.get("/rankings")
def rankings(hazard: str | None = None, region: str | None = None):
    if hazard and hazard not in settings()["weights"]:
        raise HTTPException(400, f"hazard must be one of {list(settings()['weights'])}")
    return scoring.rank(hazard, region)


@app.get("/hubs/{hub_id}/score")
def hub_score(hub_id: str):
    if get_hub(hub_id) is None:
        raise HTTPException(404, f"unknown hub {hub_id}")
    return scoring.score_hub(hub_id)


@app.get("/hubs/{hub_id}/weather")
def hub_weather(hub_id: str, metric: str = "snowfall", year: int | None = None,
                threshold: float | None = Query(None)):
    if get_hub(hub_id) is None:
        raise HTTPException(404, f"unknown hub {hub_id}")
    return metrics.weather_stat(get_hub(hub_id)["id"], metric, year, threshold)


@app.get("/methodology")
def methodology():
    return get_methodology()
