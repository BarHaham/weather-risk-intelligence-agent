"""Simulated outages: Sonnet 5.5 is the primary, Haiku 5.5 is tried once when Sonnet is
temporarily unavailable, and the result always says which model actually answered."""
import json
import uuid

import anthropic
import httpx2
import pytest
from anthropic.types import TextBlock, ToolUseBlock
from fastapi import HTTPException

from app import agent, main, store
from app.config import settings
from evals import run as eval_run
from tests.fakes import (HAIKU, REQUEST, SONNET, FakeClient, answer_json, api_error, msg,
                         overloaded)

RETRIES = settings()["llm"]["max_retries"]


def setup_module():
    store.build_from_cache()


def text(t):
    return TextBlock(type="text", text=t, citations=None)


def tool_use(name, args, id_="t1"):
    return ToolUseBlock(type="tool_use", id=id_, name=name, input=args)


def final():
    return msg([text(answer_json())], "end_turn")


def sid():
    return uuid.uuid4().hex


# ---------- the switch ----------

def test_config_defaults_to_sonnet_with_haiku_fallback():
    llm = settings()["llm"]
    assert (llm["model"], llm["fallback_model"]) == (SONNET, HAIKU)
    assert "refusal_fallback" not in llm


def test_overloaded_sonnet_falls_back_to_haiku_once():
    fake = FakeClient([overloaded(), final()])
    out = agent.chat("q", sid(), client=fake)
    assert fake.models == [SONNET, HAIKU]
    assert [r["max_retries"] for r in fake.requests] == [RETRIES, 0]  # Haiku: a single attempt
    m = out["models"]
    assert m["requested"] == SONNET and m["answered_by"] == HAIKU and m["calls"] == [HAIKU]
    assert m["fallback"]["from"] == SONNET and m["fallback"]["to"] == HAIKU
    assert m["fallback"]["reason"].startswith("OverloadedError 529 overloaded_error: Overloaded")
    assert m["fallback"]["simulated"] is False


def test_outage_mid_turn_switches_and_stays_on_haiku():
    fake = FakeClient([
        msg([tool_use("rank_hubs", {"hazard": "overall", "region": None})], "tool_use"),
        overloaded(),
        msg([tool_use("get_score_breakdown", {"hub_id": "dallas"}, "t2")], "tool_use"),
        final(),
    ])
    out = agent.chat("q", sid(), client=fake)
    assert fake.models == [SONNET, SONNET, HAIKU, HAIKU]
    assert [r["max_retries"] for r in fake.requests] == [RETRIES, RETRIES, 0, RETRIES]
    assert out["models"]["calls"] == [SONNET, HAIKU, HAIKU]
    assert out["models"]["fallback"]["at_call"] == 2
    # The retried request is identical, and system/tools never change between models
    # (changing them would invalidate replayed thinking blocks).
    assert fake.requests[1]["messages"] == fake.requests[2]["messages"]
    assert len({json.dumps(r["system"]) for r in fake.requests}) == 1
    assert len({json.dumps(r["tools"], sort_keys=True) for r in fake.requests}) == 1


def test_next_turn_starts_on_sonnet_again():
    s = sid()
    agent.chat("first", s, client=FakeClient([overloaded(), final()]))
    fake = FakeClient([final()])
    out = agent.chat("and Detroit?", s, client=fake)
    assert fake.models == [SONNET]
    assert out["models"]["fallback"] is None


@pytest.mark.parametrize("err", [
    api_error(anthropic.RateLimitError, 429, "rate_limit_error", "Too many requests"),
    api_error(anthropic.ServiceUnavailableError, 503, "api_error", "Service unavailable"),
    api_error(anthropic.InternalServerError, 500, "api_error", "Internal error"),
    api_error(anthropic.RequestTimeoutError, 408, "timeout_error", "Request timeout"),
    anthropic.APIConnectionError(request=REQUEST),
    anthropic.APITimeoutError(request=REQUEST),
], ids=["429", "503", "500", "408", "connection", "timeout"])
def test_temporarily_unavailable_errors_trigger_fallback(err):
    fake = FakeClient([err, final()])
    out = agent.chat("q", sid(), client=fake)
    assert fake.models == [SONNET, HAIKU] and out["models"]["answered_by"] == HAIKU


@pytest.mark.parametrize("err", [
    api_error(anthropic.BadRequestError, 400, "invalid_request_error", "bad"),
    api_error(anthropic.AuthenticationError, 401, "authentication_error", "invalid x-api-key"),
    api_error(anthropic.PermissionDeniedError, 403, "permission_error", "no access"),
    api_error(anthropic.NotFoundError, 404, "not_found_error", "model not found"),
    api_error(anthropic.HTTPNotImplementedError, 501, "api_error", "not implemented"),
], ids=["400", "401", "403", "404", "501"])
def test_other_errors_do_not_fall_back(err):
    fake = FakeClient([err])
    with pytest.raises(type(err)):
        agent.chat("q", sid(), client=fake)
    assert fake.models == [SONNET]


# ---------- when Haiku fails too: keep both real errors ----------

def test_both_down_raises_with_both_errors_and_stores_nothing():
    s = sid()
    fake = FakeClient([overloaded(), overloaded("Haiku overloaded too")])
    with pytest.raises(agent.FallbackFailedError) as exc:
        agent.chat("q", s, client=fake)
    e = exc.value
    assert fake.models == [SONNET, HAIKU]  # Haiku tried exactly once
    assert isinstance(e.primary_error, anthropic.OverloadedError)
    assert isinstance(e.fallback_error, anthropic.OverloadedError)
    assert e.__cause__ is e.fallback_error and e.fallback_unavailable
    assert "Haiku overloaded too" in e.describe()
    assert store.load_messages(s) == []  # a failed turn leaves no partial history


def test_haiku_permission_error_is_kept_distinct_from_an_outage():
    fake = FakeClient([overloaded(), api_error(anthropic.PermissionDeniedError, 403,
                                               "permission_error",
                                               "Your API key does not have access to claude-haiku-5-5")])
    with pytest.raises(agent.FallbackFailedError) as exc:
        agent.chat("q", sid(), client=fake)
    e = exc.value
    assert isinstance(e.fallback_error, anthropic.PermissionDeniedError)
    assert e.fallback_error.status_code == 403 and not e.fallback_unavailable
    assert ("claude-haiku-5-5 fallback failed: PermissionDeniedError 403 permission_error: "
            "Your API key does not have access to claude-haiku-5-5") in e.describe()
    assert "claude-sonnet-5-5 unavailable (OverloadedError 529" in e.describe()


def test_haiku_failing_later_in_the_turn_reports_both_errors():
    fake = FakeClient([overloaded(),
                       msg([tool_use("rank_hubs", {"hazard": "overall", "region": None})], "tool_use"),
                       api_error(anthropic.ServiceUnavailableError, 503, "api_error", "down")])
    with pytest.raises(agent.FallbackFailedError) as exc:
        agent.chat("q", sid(), client=fake)
    assert isinstance(exc.value.primary_error, anthropic.OverloadedError)
    assert isinstance(exc.value.fallback_error, anthropic.ServiceUnavailableError)


@pytest.mark.parametrize("fallback_err,status", [
    (overloaded("still down"), 503),
    (api_error(anthropic.PermissionDeniedError, 403, "permission_error", "no access"), 502),
    (api_error(anthropic.NotFoundError, 404, "not_found_error", "model not found"), 502),
])
def test_chat_endpoint_status_follows_the_fallback_error(monkeypatch, fallback_err, status):
    def boom(message, session_id):
        raise agent.FallbackFailedError(SONNET, overloaded(), HAIKU, fallback_err)
    monkeypatch.setattr(agent, "chat", boom)
    with pytest.raises(HTTPException) as exc:
        main.chat(main.ChatRequest(message="hi"))
    assert exc.value.status_code == status
    assert type(fallback_err).__name__ in exc.value.detail and "OverloadedError 529" in exc.value.detail


# ---------- simulated outage switch (WRIA_SIMULATE_OUTAGE) ----------

def test_simulated_outage_env_forces_the_fallback(monkeypatch):
    monkeypatch.setenv(agent.SIMULATE_ENV, SONNET)
    fake = FakeClient([final()])
    out = agent.chat("q", sid(), client=fake)
    assert fake.models == [HAIKU]  # Sonnet was never called
    fb = out["models"]["fallback"]
    assert fb["simulated"] is True and fb["reason"].endswith("[simulated]")
    assert "OverloadedError 529" in fb["reason"]


@pytest.mark.parametrize("value", ["", "  ", ","])
def test_empty_simulation_value_means_off(monkeypatch, value):
    monkeypatch.setenv(agent.SIMULATE_ENV, value)
    fake = FakeClient([final()])
    agent.chat("q", sid(), client=fake)
    assert fake.models == [SONNET]


# ---------- refusals and cut-offs are not stored ----------

@pytest.mark.parametrize("stop,content", [
    ("refusal", []),
    ("max_tokens", [tool_use("rank_hubs", {"hazard": "overall", "region": None})]),
])
def test_refusal_and_max_tokens_messages_are_not_stored(stop, content):
    s = sid()
    out = agent.chat("q", s, client=FakeClient([msg(content, stop)]))
    assert out["answer"]["in_scope"] is False
    assert [m["role"] for m in store.load_messages(s)] == ["user"]
    # the next turn sends a valid history: no empty assistant turn, no dangling tool_use
    fake = FakeClient([final()])
    agent.chat("again", s, client=fake)
    assert all(m["role"] == "user" for m in fake.requests[0]["messages"])


# ---------- the real SDK HTTP path (MockTransport, no sockets) ----------

def sdk_client(handler):
    return anthropic.Anthropic(api_key="test-key", base_url="http://stub.invalid",
                               http_client=anthropic.DefaultHttpxClient(
                                   transport=httpx2.MockTransport(handler)))


def message_json(model, content, stop="end_turn"):
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 7}}


def overloaded_response():
    return httpx2.Response(529, headers={"retry-after-ms": "1", "x-should-retry": "true"},
                           json={"type": "error", "error": {"type": "overloaded_error",
                                                            "message": "Overloaded"}})


def test_sdk_http_outage_retries_sonnet_then_tries_haiku_once():
    hits = {SONNET: 0, HAIKU: 0}

    def handler(request):
        body = json.loads(request.content)
        # SDK 2.x sends the API key as a bearer token
        assert request.headers.get("authorization") == "Bearer test-key"
        assert "fallbacks" not in body and "anthropic-beta" not in request.headers
        hits[body["model"]] += 1
        if body["model"] == SONNET:
            return overloaded_response()
        return httpx2.Response(200, json=message_json(HAIKU, [{"type": "text", "text": answer_json()}]))

    out = agent.chat("q", sid(), client=sdk_client(handler))
    assert hits == {SONNET: RETRIES + 1, HAIKU: 1}  # SDK retried Sonnet, then one Haiku attempt
    assert out["models"]["answered_by"] == HAIKU
    assert out["models"]["fallback"]["reason"] == "OverloadedError 529 overloaded_error: Overloaded"


def test_sdk_http_both_down_tries_haiku_exactly_once():
    hits = {SONNET: 0, HAIKU: 0}

    def handler(request):
        hits[json.loads(request.content)["model"]] += 1
        return overloaded_response()

    with pytest.raises(agent.FallbackFailedError) as exc:
        agent.chat("q", sid(), client=sdk_client(handler))
    assert hits == {SONNET: RETRIES + 1, HAIKU: 1}
    assert isinstance(exc.value.fallback_error, anthropic.OverloadedError)


# ---------- eval results show which model answered ----------

DENVER_CASE = {"id": "denver_snow_last_year",
               "turns": ["What percentage of days in Denver last year had snowfall?"],
               "tools": ["weather_stat"], "in_scope": True,
               "figure": {"hub_id": "denver", "metric": "snowfall", "year": 2025,
                          "threshold": None, "field": "percent_of_days", "tol": 0.5}}


def test_eval_case_reports_haiku_after_simulated_outage(monkeypatch, tmp_path):
    monkeypatch.setenv(agent.SIMULATE_ENV, SONNET)
    fake = FakeClient([
        msg([tool_use("weather_stat", {"hub_id": "denver", "metric": "snowfall",
                                       "year": 2025, "threshold": None})], "tool_use"),
        msg([text(answer_json(
            answer="11.8% of days (43 of 365) in 2025 had snowfall.",
            figures=[{"label": "Denver 2025 snow days", "value": 11.8, "unit": "%",
                      "source_tool": "weather_stat"}]))], "end_turn"),
    ])
    monkeypatch.setattr(agent, "_client", lambda: fake)
    monkeypatch.setattr(eval_run, "HERE", tmp_path)  # never touch evals/results/latest.json
    r = eval_run.run_case(DENVER_CASE)
    assert r["passed"], r["checks"]
    assert r["answered_by"] == HAIKU and fake.models == [HAIKU, HAIKU]
    line = eval_run.format_case_line(r)
    assert line.startswith("PASS  denver_snow_last_year")
    assert ("answered_by=claude-haiku-5-5 [fallback from claude-sonnet-5-5: OverloadedError 529 "
            "overloaded_error: Simulated outage") in line
    assert line.isascii()


def test_summary_line_counts_models_and_names_fallback_cases():
    results = [
        {"id": "a", "answered_by": SONNET, "models": [{"fallback": None}]},
        {"id": "b", "answered_by": HAIKU, "models": [{"fallback": {"from": SONNET}}]},
        {"id": "c", "answered_by": SONNET, "models": [{"fallback": None}]},
        {"id": "d", "answered_by": None, "models": []},
    ]
    assert eval_run.summary_line(results) == (
        "Answered by: claude-sonnet-5-5 x2, claude-haiku-5-5 x1, no answer x1 (fallback: b)")


def test_failed_case_keeps_both_errors_in_the_report():
    e = agent.FallbackFailedError(SONNET, overloaded(), HAIKU,
                                  api_error(anthropic.PermissionDeniedError, 403,
                                            "permission_error", "no access"))
    r = eval_run.failed_case({"id": "x"}, e.describe())
    line = eval_run.format_check_lines(r)[0]
    assert "PermissionDeniedError 403 permission_error: no access" in line
    assert "OverloadedError 529" in line
