"""Reviewer access code for the chat endpoint (the only route that calls Claude).

- REVIEWER_ACCESS_CODE set (non-empty): every /chat request must send it in the X-Access-Code
  header, wherever the app runs.
- Not set, on Render: chat is turned off, so a public deploy never spends API credits by
  accident.
- Not set, anywhere else (local runs, tests): chat is open, as before.

The code is compared in constant time and never logged or returned.
"""
from __future__ import annotations

import hmac
import os
from typing import Literal

ACCESS_CODE_VAR = "REVIEWER_ACCESS_CODE"
HEADER = "X-Access-Code"

OPEN, CODE_REQUIRED, DISABLED = "open", "code_required", "disabled"
Mode = Literal["open", "code_required", "disabled"]

CHAT_DISABLED, CODE_MISSING, CODE_INVALID = "chat_disabled", "access_code_required", "access_code_invalid"


def on_render() -> bool:
    """Render sets RENDER=true and RENDER_SERVICE_ID on its services."""
    return (os.environ.get("RENDER", "").strip().lower() in ("true", "1", "yes")
            or bool(os.environ.get("RENDER_SERVICE_ID", "").strip()))


def configured_code() -> str | None:
    code = os.environ.get(ACCESS_CODE_VAR, "").strip()
    return code or None


def mode() -> Mode:
    if configured_code():
        return CODE_REQUIRED
    return DISABLED if on_render() else OPEN


def check(provided: str | None) -> str | None:
    """None if a chat request with this header value may reach Claude, else the refusal code:
    chat_disabled, access_code_required or access_code_invalid."""
    code = configured_code()
    if code is None:
        return CHAT_DISABLED if on_render() else None
    given = (provided or "").strip()
    if not given:
        return CODE_MISSING
    if not hmac.compare_digest(given.encode("utf-8"), code.encode("utf-8")):
        return CODE_INVALID
    return None
