"""Isolate every test from the developer's environment.

A leftover WRIA_SIMULATE_OUTAGE, ANTHROPIC_BASE_URL or ANTHROPIC_API_KEY in the shell (or values
a test loads from a temporary .env) must not leak into other tests, so the whole os.environ is
snapshotted and restored around each test.
"""
import os

import pytest

from app import config

ISOLATED = ["WRIA_SIMULATE_OUTAGE", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN"]


@pytest.fixture(autouse=True)
def isolated_env():
    saved_env = dict(os.environ)
    saved_source = config.api_key_source
    for key in ISOLATED:
        os.environ.pop(key, None)
    yield
    os.environ.clear()
    os.environ.update(saved_env)
    config.api_key_source = saved_source
