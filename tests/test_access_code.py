"""Reviewer access code: checked on the server before any /chat request reaches Claude.

- REVIEWER_ACCESS_CODE set: /chat needs it in the X-Access-Code header (UI and direct API).
- Not set on Render (RENDER / RENDER_SERVICE_ID): chat is off (503).
- Not set elsewhere: chat is open, so local testing keeps working.

The app is driven over HTTP with FastAPI's TestClient (startup not run, so no .env is read),
and agent.chat is replaced by a recorder: a refused request must never reach it.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import access, agent, config, main, store

CODE = "rev-9f3K_x2Qp7"
OK = {"session_id": "s", "answer": {}, "status": "answered", "tool_calls": [], "models": {},
      "usage": {}}


def setup_module():
    store.build_from_cache()


@pytest.fixture
def calls(monkeypatch):
    """Records every request that got through to the agent (which would call Claude)."""
    seen = []

    def fake_chat(message, session_id=None, client=None):
        seen.append(message)
        return OK

    monkeypatch.setattr(agent, "chat", fake_chat)
    return seen


@pytest.fixture
def client():
    return TestClient(main.app)  # not used as a context manager: startup (and .env) not run


def post(client, headers=None):
    return client.post("/chat", json={"message": "Which hub is riskiest?"}, headers=headers or {})


# ---------- local: no code configured ----------

def test_local_without_a_code_chat_is_open(client, calls):
    r = post(client)
    assert r.status_code == 200 and calls == ["Which hub is riskiest?"]
    assert client.get("/health").json()["chat_access"] == "open"


# ---------- a code is configured ----------

def test_missing_code_is_refused_before_claude(client, calls, monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    r = post(client)
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "access_code_required"
    assert "X-Access-Code" in r.json()["detail"]["message"]
    assert calls == []


@pytest.mark.parametrize("given", ["wrong", CODE.upper(), CODE[:-1], CODE + "x"])
def test_wrong_code_is_refused_before_claude(client, calls, monkeypatch, given):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    r = post(client, {"X-Access-Code": given})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "access_code_invalid"
    assert calls == []


def test_right_code_reaches_the_agent(client, calls, monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    r = post(client, {"X-Access-Code": CODE})
    assert r.status_code == 200 and r.json() == OK and len(calls) == 1
    assert client.get("/health").json()["chat_access"] == "code_required"


def test_spaces_around_the_configured_or_sent_code_are_ignored(client, calls, monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, f"  {CODE}\n")
    assert post(client, {"X-Access-Code": f" {CODE} "}).status_code == 200


def test_header_name_is_case_insensitive(client, calls, monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    assert post(client, {"x-access-code": CODE}).status_code == 200


def test_a_blank_configured_code_counts_as_not_set(client, calls, monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, "   ")
    assert access.configured_code() is None
    assert post(client).status_code == 200  # local: open


def test_code_is_compared_in_constant_time(monkeypatch):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    used = []
    real = access.hmac.compare_digest
    monkeypatch.setattr(access.hmac, "compare_digest", lambda a, b: used.append(1) or real(a, b))
    assert access.check(CODE) is None and access.check("nope") == "access_code_invalid"
    assert len(used) == 2


# ---------- Render ----------

@pytest.mark.parametrize("render_env", [{"RENDER": "true"}, {"RENDER_SERVICE_ID": "srv-abc123"}])
def test_render_without_a_code_turns_chat_off(client, calls, monkeypatch, render_env):
    for k, v in render_env.items():
        monkeypatch.setenv(k, v)
    for headers in ({}, {"X-Access-Code": "anything"}):
        r = post(client, headers)
        assert r.status_code == 503 and r.json()["detail"]["code"] == "chat_disabled"
        assert "no reviewer access code is configured" in r.json()["detail"]["message"]
    assert calls == []
    assert client.get("/health").json()["chat_access"] == "disabled"


def test_render_with_a_code_needs_it(client, calls, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    assert post(client).status_code == 401
    assert post(client, {"X-Access-Code": CODE}).status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["", "false", "0"])
def test_render_flag_must_be_true_to_count(monkeypatch, value):
    monkeypatch.setenv("RENDER", value)
    assert not access.on_render() and access.mode() == "open"


# ---------- what stays open, and what is never shown ----------

def test_data_endpoints_stay_open_and_health_never_shows_the_code(client, monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    assert client.get("/rankings").status_code == 200
    assert client.get("/map/hubs").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200 and CODE not in health.text
    assert CODE not in json.dumps(post(client, {"X-Access-Code": "wrong"}).json())


def test_the_code_can_come_from_a_local_env_file(client, calls, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"{access.ACCESS_CODE_VAR}={CODE}\n", encoding="utf-8")
    config.load_env_file(env)
    assert post(client).status_code == 401
    assert post(client, {"X-Access-Code": CODE}).status_code == 200


def test_startup_log_names_the_mode_but_never_the_code(monkeypatch, caplog):
    monkeypatch.setenv(access.ACCESS_CODE_VAR, CODE)
    with caplog.at_level("INFO", logger="uvicorn.error"):
        main.log_chat_access()
    monkeypatch.delenv(access.ACCESS_CODE_VAR)
    monkeypatch.setenv("RENDER", "true")
    with caplog.at_level("INFO", logger="uvicorn.error"):
        main.log_chat_access()
    assert "needs the reviewer access code" in caplog.text
    assert "Chat is disabled" in caplog.text
    assert CODE not in caplog.text


def test_only_the_chat_route_calls_the_agent():
    # every route that can reach Claude must carry the access check
    import ast
    tree = ast.parse(open(main.__file__, encoding="utf-8").read())
    callers = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
               for n in ast.walk(f) if isinstance(n, ast.Call)
               and ast.unparse(n.func) in ("agent.chat", "agent._client")]
    assert callers == ["chat"]
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/chat")
    assert main.require_chat_access in [d.call for d in route.dependant.dependencies]
