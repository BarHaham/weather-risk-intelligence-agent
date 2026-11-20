"""Open-Meteo Historical Weather API (ERA5 reanalysis), daily values per hub.

Raw responses are cached as data/cache/open_meteo_<hub>.json so the app and the evals run
offline and never depend on Open-Meteo's free-tier quota (which is per IP and shared).
"""
from __future__ import annotations

import json
import time

import requests

from app.config import hubs, path, settings

URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = ["snowfall_sum", "precipitation_sum", "temperature_2m_max",
         "temperature_2m_min", "wind_gusts_10m_max"]


class QuotaExceeded(RuntimeError):
    pass


def _get(params: dict, attempts: int = 4) -> requests.Response:
    for i in range(attempts):
        try:
            return requests.get(URL, params=params, timeout=120)
        except requests.ConnectionError:
            if i == attempts - 1:
                raise
            time.sleep(2 ** i * 2)  # 2s, 4s, 8s


def fetch_hub(h: dict) -> dict:
    d = settings()["data"]
    r = _get({
        "latitude": h["lat"], "longitude": h["lon"],
        "start_date": d["history_start"], "end_date": d["history_end"],
        "daily": ",".join(DAILY), "timezone": h["timezone"],
    })
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if body.get("error"):
        reason = body.get("reason", "")
        if "limit" in reason.lower():
            raise QuotaExceeded(reason)
        raise RuntimeError(f"Open-Meteo error for {h['id']}: {reason}")
    r.raise_for_status()
    return body


def cache_file(hub_id: str):
    return path("cache_dir") / f"open_meteo_{hub_id}.json"


def refresh(force: bool = False) -> None:
    path("cache_dir").mkdir(parents=True, exist_ok=True)
    for h in hubs():
        f = cache_file(h["id"])
        if f.exists() and not force:
            print(f"Open-Meteo: {h['id']} already cached")
            continue
        body = fetch_hub(h)
        f.write_text(json.dumps(body))
        print(f"Open-Meteo: cached {h['id']} ({len(body['daily']['time'])} days, "
              f"grid {body['latitude']:.3f},{body['longitude']:.3f})")
        time.sleep(1)  # be polite to a free API
