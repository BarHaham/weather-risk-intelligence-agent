"""Load config.yaml, hubs.json and the optional .env file; paths are relative to the repo root."""
from __future__ import annotations

import codecs
import io
import json
import os
import warnings
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
API_KEY_VAR = "ANTHROPIC_API_KEY"

# Where ANTHROPIC_API_KEY came from: "environment", ".env" or "missing" (never the value).
api_key_source = "missing"


def _decode_env_bytes(raw: bytes) -> str:
    """Decode a .env file whatever editor or shell wrote it.

    Windows PowerShell 5.1 writes UTF-16 LE with `>` / Out-File and ANSI (cp1252) with
    Set-Content; Notepad may add a UTF-8 BOM.
    """
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    if b"\x00" in raw:
        # BOM-less UTF-16, or UTF-16 appended to a UTF-8 file (PowerShell 5.1's `>>`):
        # keys and API keys are ASCII, so dropping the NULs recovers them.
        return raw.decode("utf-8", errors="replace").replace("\x00", "")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def load_env_file(env_path: Path | str = ENV_FILE) -> dict[str, str]:
    """Load KEY=VALUE pairs from env_path into os.environ, without overriding variables that
    are already set to a non-empty value. Empty values are skipped (an untouched copy of
    .env.example must not set an empty key). Returns the variables it set.
    """
    global api_key_source
    env_path = Path(env_path)
    loaded: dict[str, str] = {}
    if env_path.exists():
        # A broken .env must never stop the app or the evals from starting: warn and go on.
        try:
            text = _decode_env_bytes(env_path.read_bytes())
            values = dotenv_values(stream=io.StringIO(text), interpolate=False)
        except (OSError, UnicodeError, ValueError) as e:
            warnings.warn(f"Could not read {env_path}: {e}", stacklevel=2)
            values = {}
        for key, value in values.items():
            if not key or not value or os.environ.get(key):
                continue
            try:
                os.environ[key] = value
            except ValueError as e:  # e.g. an embedded NUL that survived decoding
                warnings.warn(f"Skipping {key!r} from {env_path.name}: {e}", stacklevel=2)
                continue
            loaded[key] = value
    elif env_path.with_name(env_path.name + ".txt").exists():
        warnings.warn(f"Found {env_path.name}.txt but no {env_path.name}: Notepad may have added "
                      f".txt; rename it to {env_path.name}.", stacklevel=2)
    if API_KEY_VAR in loaded:
        api_key_source = ".env"
    elif os.environ.get(API_KEY_VAR):
        api_key_source = "environment"
    else:
        api_key_source = "missing"
    return loaded


@lru_cache
def settings() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def path(key: str) -> Path:
    return ROOT / settings()["data"][key]


@lru_cache
def hubs() -> list[dict]:
    with open(path("hubs_file"), encoding="utf-8") as f:
        return json.load(f)


def hub(hub_id: str) -> dict | None:
    hub_id = hub_id.strip().lower()
    for h in hubs():
        if h["id"] == hub_id or h["city"].lower() == hub_id:
            return h
    return None
