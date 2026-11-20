"""Isolate every test from the developer's environment.

Every ANTHROPIC_* variable the SDK reads (API key, auth token, base URL, custom headers, ...)
and every WRIA_* switch (e.g. a leftover WRIA_SIMULATE_OUTAGE) is removed for each test, and
the whole os.environ is snapshotted and restored afterwards, so values a test loads from a
temporary .env cannot leak into other tests.
"""
import os

import pytest

from app import config

ISOLATED_PREFIXES = ("ANTHROPIC_", "WRIA_")


@pytest.fixture(autouse=True)
def isolated_env():
    saved_env = dict(os.environ)
    saved_source = config.api_key_source
    for key in [k for k in os.environ if k.startswith(ISOLATED_PREFIXES)]:
        del os.environ[key]
    yield
    os.environ.clear()
    os.environ.update(saved_env)
    config.api_key_source = saved_source
