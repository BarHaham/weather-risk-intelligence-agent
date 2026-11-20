"""FastAPI app: the chat endpoint the UI uses, plus read-only data endpoints."""
from __future__ import annotations

import logging
from pathlib import Path

import anthropic
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import access, agent, config, geo, metrics, scoring, store
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
app.mount("/static", StaticFiles(directory=STATIC), name="static")  # us-states.json map outlines


@app.on_event("startup")
def _startup() -> None:
    # Load ANTHROPIC_API_KEY (etc.) from the repo's .env at startup rather than at import, so
    # tests that import this module never pick up a developer's real .env.
    config.load_env_file()
    store.build_from_cache()
    log_chat_access()


log = logging.getLogger("uvicorn.error")  # shown in the server log (Render's log view too)


def log_chat_access() -> None:
    """One line saying whether chat is open, needs the reviewer code, or is off. Never the code."""
    m = access.mode()
    if m == access.DISABLED:
        log.warning("Chat is disabled: running on Render and %s is not set.", access.ACCESS_CODE_VAR)
    elif m == access.CODE_REQUIRED:
        log.info("Chat needs the reviewer access code (%s, sent in the %s header).",
                 access.ACCESS_CODE_VAR, access.HEADER)
    else:
        log.info("Chat is open: %s is not set (local run).", access.ACCESS_CODE_VAR)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():  # for browsers that ask for /favicon.ico before reading the <link rel="icon">
    return FileResponse(STATIC / "favicon-32.png", media_type="image/png")


@app.get("/health")
def health():
    return {"status": "ok",
            "observed_weather_hubs": [h["id"] for h in hubs() if store.has_weather(h["id"])],
            "nri_loaded": bool(store.nri_caps()),
            "llm": {"model": settings()["llm"]["model"],
                    "fallback_model": settings()["llm"].get("fallback_model"),
                    "simulated_outage": sorted(agent.simulated_outage_models())},
            "api_key_source": config.api_key_source,
            "chat_access": access.mode()}  # open / code_required / disabled; never the code


CONVERSATION_EXPIRED = {
    "code": "conversation_expired",
    "message": "This conversation was started with an earlier version of the app and can't be "
               "continued. Start a new conversation to ask again.",
}


ACCESS_REFUSED = {
    access.CHAT_DISABLED: (503, "Chat is turned off on this server: no reviewer access code is "
                                "configured."),
    access.CODE_MISSING: (401, "This chat needs a reviewer access code. Send it in the "
                               f"{access.HEADER} header."),
    access.CODE_INVALID: (401, "That access code isn't valid."),
}


def require_chat_access(x_access_code: str | None = Header(default=None, alias=access.HEADER)):
    """Runs before the /chat handler, for the UI and direct API calls alike: a refused request
    never reaches agent.chat() and so never calls Claude."""
    refused = access.check(x_access_code)
    if refused:
        status, message = ACCESS_REFUSED[refused]
        raise HTTPException(status, {"code": refused, "message": message})


@app.post("/chat", dependencies=[Depends(require_chat_access)])
def chat(req: ChatRequest):
    try:
        return agent.chat(req.message, req.session_id)
    except (anthropic.BadRequestError, agent.FallbackFailedError) as e:
        if agent.is_expired_conversation(e):  # stored reasoning no longer validates
            raise HTTPException(409, CONVERSATION_EXPIRED)
        if isinstance(e, agent.FallbackFailedError):
            # Both errors are named; the status follows the fallback's real error, so an outage
            # (503) can be told apart from a permissions/config problem on the fallback (502).
            raise HTTPException(503 if e.fallback_unavailable else 502, e.describe())
        raise HTTPException(502, f"Anthropic API error: {agent.describe_error(e)}")
    except TypeError as e:  # SDK raises this when no credentials can be resolved at all
        if "authentication" not in str(e).lower():
            raise
        raise HTTPException(503, "No Anthropic credentials found: set ANTHROPIC_API_KEY "
                                 "(in .env or the environment).")
    except anthropic.AuthenticationError as e:
        raise HTTPException(503, "Anthropic credentials missing or invalid: check "
                                 f"ANTHROPIC_API_KEY ({agent.describe_error(e)}).")
    except anthropic.RateLimitError as e:  # only reached when no fallback model is configured
        raise HTTPException(429, f"Anthropic rate limit hit; retry shortly ({agent.describe_error(e)}).")
    except anthropic.APIStatusError as e:
        raise HTTPException(502, f"Anthropic API error: {agent.describe_error(e)}")
    except anthropic.APIConnectionError as e:
        raise HTTPException(502, f"Could not reach the Anthropic API ({agent.describe_error(e)}).")


@app.get("/hubs")
def list_hubs():
    return hubs()


@app.get("/map/hubs")
def map_hubs():
    """Hubs with their position on /static/us-states.json (975x610 SVG) plus score and tier."""
    ranked = {r["hub_id"]: r for r in scoring.rank()}
    out = []
    for h in hubs():
        x, y = geo.albers_usa(h["lon"], h["lat"])
        r = ranked[h["id"]]
        out.append({k: h[k] for k in ("id", "city", "state", "region", "county")} |
                   {"state_name": geo.STATE_NAMES.get(h["state"], h["state"]),
                    "x": round(x, 1), "y": round(y, 1), "overall_score": r["overall_score"],
                    "tier": r["tier"], "rank": r["rank"]})
    return out


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
    result = metrics.weather_stat(get_hub(hub_id)["id"], metric, year, threshold)
    if result.get("invalid_argument"):  # e.g. a negative snowfall threshold
        raise HTTPException(400, result["error"])
    return result


@app.get("/methodology")
def methodology():
    return get_methodology()
