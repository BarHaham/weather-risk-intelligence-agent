"""Agent-loop mechanics with a fake Anthropic client (no network, no API key).

Checks: tool calls are executed and returned in one user message, the history is replayed
on the next request, the final JSON is validated, and follow-ups see earlier turns.
"""
import json
import uuid

from anthropic.types import TextBlock, ToolUseBlock

from app import agent, store
from tests.fakes import SONNET, FakeClient, answer_json, msg


def setup_module():
    store.build_from_cache()


def text(t):
    return TextBlock(type="text", text=t, citations=None)


def test_tool_round_then_validated_answer():
    fake = FakeClient([
        msg([ToolUseBlock(type="tool_use", id="t1", name="rank_hubs",
                          input={"hazard": "overall", "region": None})], "tool_use"),
        msg([text(answer_json())], "end_turn"),
    ])
    out = agent.chat("Which hub is riskiest?", uuid.uuid4().hex, client=fake)
    assert out["answer"]["answer"] == "Houston ranks first."
    assert out["tool_calls"] == [{"tool": "rank_hubs", "input": {"hazard": "overall", "region": None},
                                  "is_error": False}]
    assert out["models"] == {"requested": SONNET, "answered_by": SONNET,
                             "calls": [SONNET, SONNET], "fallback": None}
    second = fake.requests[1]["messages"]
    assert second[-1]["role"] == "user" and second[-1]["content"][0]["type"] == "tool_result"
    assert json.loads(second[-1]["content"][0]["content"])["ranked_by"] == "overall"


def test_follow_up_replays_history():
    sid = uuid.uuid4().hex
    agent.chat("first", sid, client=FakeClient([msg([text(answer_json())], "end_turn")]))
    fake = FakeClient([msg([text(answer_json())], "end_turn")])
    agent.chat("and Detroit?", sid, client=fake)
    sent = fake.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "first" and sent[-1]["content"] == "and Detroit?"


def test_invalid_json_gets_one_repair_attempt():
    fake = FakeClient([
        msg([text('{"answer": "missing fields"}')], "end_turn"),
        msg([text(answer_json(answer="fixed"))], "end_turn"),
    ])
    out = agent.chat("q", uuid.uuid4().hex, client=fake)
    assert out["answer"]["answer"] == "fixed"
    assert "did not validate" in fake.requests[1]["messages"][-1]["content"]


def test_unknown_tool_input_is_reported_as_tool_error():
    fake = FakeClient([
        msg([ToolUseBlock(type="tool_use", id="t1", name="get_score_breakdown",
                          input={"hub_id": "seattle"})], "tool_use"),
        msg([text(answer_json(in_scope=False))], "end_turn"),
    ])
    out = agent.chat("Seattle?", uuid.uuid4().hex, client=fake)
    assert out["tool_calls"][0]["is_error"] is True
    assert fake.requests[1]["messages"][-1]["content"][0]["is_error"] is True
