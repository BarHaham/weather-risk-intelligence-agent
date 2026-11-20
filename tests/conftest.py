"""Isolate every test from the developer's environment.

Every ANTHROPIC_* variable the SDK reads (API key, auth token, base URL, custom headers, ...),
every WRIA_* switch (e.g. a leftover WRIA_SIMULATE_OUTAGE), REVIEWER_ACCESS_CODE and Render's
RENDER / RENDER_SERVICE_ID are removed for each test, and
the whole os.environ is snapshotted and restored afterwards, so values a test loads from a
temporary .env cannot leak into other tests.
"""
import os

import pytest

from app import config

ISOLATED_PREFIXES = ("ANTHROPIC_", "WRIA_")
# The reviewer access code and Render's own variables decide whether /chat is open, needs a
# code, or is off; a value in the developer's shell must not change the results.
ISOLATED_NAMES = ("REVIEWER_ACCESS_CODE", "RENDER", "RENDER_SERVICE_ID")


@pytest.fixture(autouse=True)
def isolated_env():
    saved_env = dict(os.environ)
    saved_source = config.api_key_source
    for key in [k for k in os.environ if k.startswith(ISOLATED_PREFIXES) or k in ISOLATED_NAMES]:
        del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(saved_env)
    config.api_key_source = saved_source
