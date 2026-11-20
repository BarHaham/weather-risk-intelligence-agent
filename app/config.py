"""Load config.yaml and hubs.json once; paths are resolved relative to the repo root."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache
def settings() -> dict:
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def path(key: str) -> Path:
    return ROOT / settings()["data"][key]


@lru_cache
def hubs() -> list[dict]:
    with open(path("hubs_file")) as f:
        return json.load(f)


def hub(hub_id: str) -> dict | None:
    hub_id = hub_id.strip().lower()
    for h in hubs():
        if h["id"] == hub_id or h["city"].lower() == hub_id:
            return h
    return None
